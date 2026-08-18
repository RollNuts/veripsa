#!/usr/bin/env python3
"""Veripsa GitHub App — the ACK-FAST EVENT QUEUE (the live webhook critical path), split out of server.py so
the chronic-collision hotspot is finer-grained (a self-contained, behavior-IDENTICAL module: NO logic change).

One or a small fixed pool of daemon workers drains a BOUNDED,
STARVATION-FREE, multi-tenant-FAIR queue. Repository work is keyed by stable
repository id; account-wide lifecycle work is an exclusive barrier. Production
allows two independent repositories from one account while reserving a worker
for another account. do_POST verifies the HMAC, ENQUEUEs, and acks 202
immediately (within GitHub's ~10s timeout); the pool runs the injected
`process`.

DEPENDENCY INJECTION (this module imports NOTHING from server.py — no circular import). The three server-side
seams the queue needs are passed in as callables/values at construction:
  * account_of(payload)      → the STABLE tenant key (server's _event_account_key) — drives per-account fair drain.
  * repo_of(payload)         → the repo coordinate (server's _event_repo) — for the failure log line.
  * branch_from_ref(ref)     → the branch name from a push ref (server's _branch_from_ref) — for push coalescing.
  * process / per_account_cap → the live per-event processor + the noisy-neighbor cap default.
The classes depend ONLY on these injected callables + the stdlib (queue, threading, collections, time, inspect).
"""
from __future__ import annotations

import collections
import inspect
import os
import queue
import threading
import time

# Per-event outcome ringbuffer + the windowed failure-ratio signal it feeds — lives in health_watchdog so
# the /healthz + /alarmz + watchdog tick can read it without reaching back into the worker. Imported here
# LAZILY (call-time, guarded) so this module stays import-cycle-clean: server.py imports BOTH event_queue
# and health_watchdog; neither imports the other directly. A failed import is fail-open — the worker keeps
# processing events; only the windowed failure-ratio signal goes dark (an honest degradation). Same
# dual-import idiom (`from x` then `from .x`) the rest of the App uses.
def _record_outcome(outcome: str) -> None:
    """Forward one event outcome to the health_watchdog ringbuffer. Fail-open: any error is swallowed so
    a watchdog defect can never break event processing (the worker outlives the observability)."""
    try:
        try:
            from health_watchdog import record_outcome as _ro  # type: ignore
        except ImportError:
            from .health_watchdog import record_outcome as _ro  # type: ignore
        _ro(outcome)
    except Exception:
        pass

# env_int: the VALIDATED env-knob reader. A 0/negative VERIPSA_PER_ACCOUNT_QUEUE_CAP is the WORST silent
# misconfig in the App — len(bucket) >= 0 is always true → every submit() raises queue.Full → EVERY webhook
# 503s → the App processes NOTHING while /healthz stays green. Reading it through env_int refuses a <1 (or a
# non-int) cap LOUDLY at start instead of running silently dead.
try:
    from env_config import env_int  # noqa: E402
except ImportError:  # imported as a package
    from .env_config import env_int  # noqa: E402

# One monotonic wall-clock budget is opened when the worker dequeues an event and remains active across every
# in-process retry. Collaborators (GitHub REST, graph extraction, DB waits) read the same ContextVar-backed
# deadline, so their local retries cannot multiply into minutes while occupying a scarce worker/repository lane.
try:
    import event_budget  # noqa: E402
    from event_budget import EventBudgetExceeded  # noqa: E402
except ImportError:  # imported as a package
    from . import event_budget  # type: ignore  # noqa: E402
    from .event_budget import EventBudgetExceeded  # noqa: E402

# PushCoalescer: the FORCE-PUSH COALESCING collaborator, split out so this hotspot is finer-grained. EventQueue
# owns one instance and forwards its coalescing seams (_register_push / _push_coalesce / _latest_push / ...) to it,
# so the split is invisible to callers + tests. Imports NOTHING from server.py (no circular import).
try:
    from push_coalescer import PushCoalescer  # noqa: E402
except ImportError:  # imported as a package
    from .push_coalescer import PushCoalescer  # noqa: E402

try:
    from check_delivery_observer import (  # noqa: E402
        begin_check_delivery_observation,
        end_check_delivery_observation,
    )
except ImportError:  # imported as a package
    from .check_delivery_observer import (  # noqa: E402
        begin_check_delivery_observation,
        end_check_delivery_observation,
    )


# IN-PROCESS RETRY DEFAULTS. Once do_POST has returned 202, GitHub does NOT automatically redeliver a later worker
# exception (the durable inbox is the cross-RESTART backstop; this is the in-MEMORY one for a transient blip —
# a momentary DB/network error — so a single bad moment isn't counted a terminal loss). retry_attempts = the
# MAX total runs of the processor for one event (1 = the historical no-retry behaviour: one failure is terminal).
# Defaulted to the SAME knob the durable store's max_attempts reads (VERIPSA_EVENT_RETRY_ATTEMPTS) so the
# in-memory and durable retry budgets agree by default.
_EVENT_RETRY_ATTEMPTS = env_int("VERIPSA_EVENT_RETRY_ATTEMPTS", 3, min_value=1)
# Base backoff (seconds) between in-process retries; retry K (1-based) sleeps base*K. 0 disables the sleep
# (tests pass 0). Kept SMALL — this is a transient-blip retry on one pool lane, not a long redelivery wait.
_EVENT_RETRY_BASE_SECONDS = env_int("VERIPSA_EVENT_RETRY_BASE_SECONDS", 1, min_value=0)

# Bounded in-process receipts for authenticated deployment probes. A reusable canary can legitimately render
# byte-identical Check output, so GitHub may no-op the PATCH and leave the Check Run id/timestamps unchanged. The
# deployment gate still needs proof that THIS synthetic delivery reached a terminal worker outcome. Keep only
# content-free delivery ids + outcome tags, in memory, with a hard cap (no payloads, repo names, or source bodies).
_DELIVERY_RECEIPT_CAP = 1024
_WORKER_CLAIM_OUTCOME = "_veripsa_worker_claim_outcome"
_REPOSITORY_SCOPED_EVENTS = frozenset(
    ("repository", "pull_request", "push", "check_suite", "check_run", "merge_group")
)


def _bounded_error_code(exc: BaseException) -> str:
    """Return diagnosable error metadata without logging payload-derived exception text."""
    raw_name = type(exc).__name__
    name = "".join(
        char if char.isascii() and (char.isalnum() or char == "_") else "_"
        for char in raw_name
    )[:64].lower() or "unknown"
    pgcode = getattr(exc, "pgcode", None)
    if (isinstance(pgcode, str) and len(pgcode) == 5 and pgcode.isascii()
            and pgcode.isalnum() and pgcode.upper() == pgcode):
        return f"exception_{name}_sqlstate_{pgcode}"
    return f"exception_{name}"


def _delivery_presence(delivery) -> str:
    """Provider delivery identifiers are capabilities; the random trace id is the correlation surface."""
    return "present" if delivery else "missing"


def _repository_lane(item) -> str | None:
    """Repository-id lane for safe parallelism; every ambiguous shape is an account-wide barrier."""
    try:
        event_type, payload = item[0], item[1]
        if event_type not in _REPOSITORY_SCOPED_EVENTS or not isinstance(payload, dict):
            return None
        repository = payload.get("repository")
        if not isinstance(repository, dict):
            return None
        repository_id = repository.get("id")
        if repository_id in (None, "") or isinstance(repository_id, (bool, dict, list, tuple, set)):
            return None
        lane = str(repository_id)
        # Match the durable SQL classifier exactly. GitHub repository ids are positive decimal
        # integers. Accepting a malformed value here as a narrow lane while PostgreSQL treats it
        # as a wide barrier would let live and recovery scheduling disagree after a restart.
        if not lane or len(lane) > 32 or not lane.isascii() or not lane.isdigit() or lane.startswith("0"):
            return None
        return lane
    except Exception:
        return None


# FAIRNESS GUARD (the noisy-neighbor cap). A fixed-small keyed pool drains the queue, so admission and lane order
# are the fairness story on a multi-tenant box. Two bounds, both per-ACCOUNT (the stable GitHub owner id — the same
# tenant key make_db_processor routes RLS by), never per-installation (ephemeral):
#   * _PER_ACCOUNT_QUEUE_CAP — the MOST queued events ONE account may hold at once. A single tenant whose fleet
#     force-pushes a big monorepo would otherwise enqueue a burst that (a) fills the global bound and 503s EVERY
#     other tenant's deliveries out, and (b) sits at the head of a FIFO so every other tenant waits minutes. Cap
#     it: past the cap that ONE account's further submits are rejected (it 503s → Core recovery redelivers ITS event
#     later) while every other tenant's room in the global bound is preserved. A free/runaway tenant can hold at
#     most this slice, never the whole instance.
#   * the DRAIN keeps one FIFO per repository and an account-wide reader/writer barrier. That preserves lifecycle
#     causality while letting independent repositories and tenants progress. A bounded live burst gives recovery
#     a turn; an already-running event is never pre-empted.
# Both env-configurable upward for a bigger instance / paid tier. Default sized for the starter box's 1000 global.
_PER_ACCOUNT_QUEUE_CAP = env_int("VERIPSA_PER_ACCOUNT_QUEUE_CAP", 100, min_value=1)

# Fresh work normally wins across accounts, but recovery must still make bounded progress so a preclaimed durable
# row cannot outlive its processing lease under continuous traffic. This is an internal scheduling constant, not
# an operator procedure: after this many live dequeues, one queued recovery head gets a turn.
_LIVE_BURST_LIMIT = 32


class _FairQueue:
    """A bounded keyed queue with account barriers and bounded live-over-recovery priority.

    Repository-scoped work keeps a strict FIFO per stable repository id and may execute concurrently across
    repositories. Account-wide lifecycle/billing/ambiguous work is a writer barrier: it waits for all earlier
    repository lanes and blocks all later work. Across accounts, live work drains before recovery except for a
    bounded recovery turn. A noisy tenant therefore cannot block other tenants, fresh Checks do not sit behind an
    entire recovery backlog, and durable recovery cannot starve under continuous traffic.

    Replaces the strict-FIFO queue.Queue. SAME external contract the EventQueue relied on:
      * put_nowait((item)) → raises queue.Full when the GLOBAL bound is hit (→ 503; Core recovery redelivers),
      * get() → blocks until an item is available, returns the next item in round-robin order,
      * task_done() / unfinished_tasks → drives wait_idle() + graceful drain exactly as queue.Queue did,
      * qsize() → total queued across all accounts.
    PLUS a PER-ACCOUNT cap shared across both classes: put_nowait raises queue.Full once ONE account already holds
    per_account_cap items,
    even if the global bound has room — so a runaway/free tenant can't consume the whole instance and 503 the
    others. One lock + condition variable coordinates the fixed-small pool.

    INJECTED: account_of(item_payload) → the tenant key. The queue itself holds no server knowledge — server.py
    passes _event_account_key so the SAME stable owner id that make_db_processor routes RLS by drives the fair
    drain. (If omitted, every item shares one '' bucket: still a valid bounded queue, but NO cross-tenant
    fairness — callers that need fairness MUST inject the real key fn.)"""

    def __init__(self, maxsize: int, per_account_cap: int, account_of=None, lane_of=None,
                 max_active_per_account: int = 1,
                 live_burst_limit: int = _LIVE_BURST_LIMIT):
        self._maxsize = maxsize
        self._per_account_cap = max(1, per_account_cap)
        self._account_of_fn = account_of
        self._lane_of_fn = lane_of
        self._max_active_per_account = max(1, int(max_active_per_account))
        # An account appears in one order deque for its next executable lane. Freeze the repository lane at
        # admission: payloads are immutable queue facts, and repeatedly reclassifying them while scanning ready
        # accounts both wastes CPU and could make ordering depend on an accidental caller-side mutation.
        self._buckets: dict = {}  # account_key -> deque[(sequence, recovered, item, repository_lane)]
        self._live_order: collections.deque = collections.deque()
        self._recovery_order: collections.deque = collections.deque()
        self._live_burst_limit = max(1, int(live_burst_limit))
        self._live_burst = 0
        self._total = 0
        self._unfinished = 0
        self._admission_sequence = 0
        # A lane remains active from get() until the SAME worker calls task_done(). Same-repo followers and work
        # behind an account-wide barrier remain absent from ready orders. The thread→lane lease also preserves
        # queue.Queue's argument-free task_done() contract for EventQueue and existing tests.
        self._active_wide_accounts: set = set()
        self._active_repo_lanes: dict[str, set[str]] = {}
        # Stable repository ids survive rename and account transfer. The DB enforces one global stable-id lane,
        # so live scheduling must not treat the old/new account buckets as independent and overlap the transfer.
        self._active_repository_ids: set[str] = set()
        # lane -> globally ordered queued admission sequences. The prior implementation found an earlier
        # same-repository item by rescanning every account bucket for every candidate (quadratic/cubic under a
        # 1000-item multi-account backlog). Admission order is monotonic, so one deque per stable id makes the
        # global FIFO-head test O(1) and is removed exactly when that item is dequeued.
        self._queued_repository_sequences: dict[str, collections.deque[int]] = {}
        self._leased_by_thread: dict[int, tuple[str, str | None]] = {}
        self._ready_class: dict[str, bool] = {}
        self._cv = threading.Condition()
        self._deque = collections.deque        # bound the factory once

    def _account_of(self, item) -> str:
        # item is (event_type, payload, delivery). The tenant key is the STABLE owner account id (same key
        # make_db_processor routes RLS by). Best-effort: a malformed payload with no account → a shared '' bucket
        # (still gets its own fair round-robin turn, so even keyless events never monopolize). No injected key fn
        # (account_of=None) → every item shares the '' bucket (a plain bounded FIFO, no cross-tenant fairness).
        if self._account_of_fn is None:
            return ""
        try:
            return self._account_of_fn(item[1]) or ""
        except Exception:
            return ""

    def _lane_of(self, item) -> str | None:
        """Stable repository id for repository-scoped work; None is a conservative account-wide barrier."""
        if self._lane_of_fn is None:
            return None
        try:
            lane = self._lane_of_fn(item)
            return str(lane) if lane not in (None, "") else None
        except Exception:
            return None

    def _candidate(self, acct: str):
        """First executable item under the account's reader/writer-style causal barrier."""
        bucket = self._buckets.get(acct)
        if not bucket or acct in self._active_wide_accounts:
            return None
        active_repos = self._active_repo_lanes.get(acct, set())
        if len(active_repos) >= self._max_active_per_account:
            return None
        for index, (sequence, recovered, item, lane) in enumerate(bucket):
            if lane is None:
                # Account-wide work is a barrier: it waits for every earlier repo run, and nothing behind it may
                # overtake. Because dequeued work is removed, index==0 proves all earlier queued work is gone.
                if index == 0 and not active_repos:
                    return index, recovered, item, lane
                return None
            if (
                lane not in active_repos
                and lane not in self._active_repository_ids
                and not self._has_earlier_repository(sequence, lane)
            ):
                return index, recovered, item, lane
            # Same-repo followers stay FIFO, but a different repository before the first wide barrier may proceed.
        return None

    def _has_earlier_repository(self, sequence: int, lane: str) -> bool:
        """Global stable-id FIFO across a rename/account transfer."""
        queued = self._queued_repository_sequences.get(lane)
        return bool(queued and queued[0] < sequence)

    def _append_repository_sequence(self, lane: str | None, sequence: int) -> None:
        if lane is not None:
            self._queued_repository_sequences.setdefault(lane, collections.deque()).append(sequence)

    def _remove_repository_sequence(self, lane: str | None, sequence: int) -> None:
        if lane is None:
            return
        queued = self._queued_repository_sequences.get(lane)
        if not queued or queued[0] != sequence:
            raise RuntimeError("fair queue repository sequence index drifted")
        queued.popleft()
        if not queued:
            self._queued_repository_sequences.pop(lane, None)

    def _remove_ready(self, acct: str) -> None:
        prior = self._ready_class.pop(acct, None)
        if prior is None:
            return
        order = self._recovery_order if prior else self._live_order
        try:
            order.remove(acct)
        except ValueError:
            pass

    def _refresh_ready(self, acct: str) -> None:
        candidate = self._candidate(acct)
        if candidate is None:
            self._remove_ready(acct)
            return
        recovered = bool(candidate[1])
        prior = self._ready_class.get(acct)
        if prior is not None and prior == recovered:
            return
        self._remove_ready(acct)
        (self._recovery_order if recovered else self._live_order).append(acct)
        self._ready_class[acct] = recovered

    def _refresh_all_ready(self) -> None:
        # A global stable repository lease can make another account unready (or release it). Recompute under the
        # existing condition lock; account counts are bounded by the queue's fixed capacity.
        for acct in tuple(set(self._buckets) | set(self._ready_class)):
            self._refresh_ready(acct)

    def put_nowait(self, item, *, recovered: bool = False) -> None:
        """Enqueue or raise queue.Full. Full when EITHER the global bound is hit OR this item's account already
        holds per_account_cap queued items (the noisy-neighbor cap — protects every OTHER tenant's global room)."""
        acct = self._account_of(item)
        with self._cv:
            if self._total >= self._maxsize:
                raise queue.Full
            bucket = self._buckets.get(acct)
            if bucket is not None and len(bucket) >= self._per_account_cap:
                raise queue.Full                  # this ONE account is over its slice → it 503s, others unaffected
            if bucket is None:
                bucket = self._buckets[acct] = self._deque()
            self._admission_sequence += 1
            lane = self._lane_of(item)
            bucket.append((self._admission_sequence, bool(recovered), item, lane))
            self._append_repository_sequence(lane, self._admission_sequence)
            self._total += 1
            self._unfinished += 1
            self._refresh_ready(acct)
            self._cv.notify()

    def put_nowait_prepared(self, preview_item, prepare, *, recovered: bool = False) -> None:
        """Capacity-check a preview, then build and expose the final item atomically.

        EventQueue uses this to allocate a delivery generation only after the queue has accepted the submit, while
        still guaranteeing that generation exists before the worker can dequeue the item. A queue-full duplicate
        therefore cannot steal receipt authority from an already-running delivery.
        """
        acct = self._account_of(preview_item)
        with self._cv:
            if self._total >= self._maxsize:
                raise queue.Full
            bucket = self._buckets.get(acct)
            if bucket is not None and len(bucket) >= self._per_account_cap:
                raise queue.Full
            item = prepare()
            if bucket is None:
                bucket = self._buckets[acct] = self._deque()
            self._admission_sequence += 1
            lane = self._lane_of(item)
            bucket.append((self._admission_sequence, bool(recovered), item, lane))
            self._append_repository_sequence(lane, self._admission_sequence)
            self._total += 1
            self._unfinished += 1
            self._refresh_ready(acct)
            self._cv.notify()

    def can_put_nowait(self, item) -> bool:
        """Non-mutating capacity check for recovery.

        The durable recovery loop uses this before it claims a DB row. That keeps a full in-memory/per-account
        queue from burning durable attempts on rows it cannot actually submit yet.
        """
        acct = self._account_of(item)
        with self._cv:
            if self._total >= self._maxsize:
                return False
            bucket = self._buckets.get(acct)
            return bucket is None or len(bucket) < self._per_account_cap

    def get(self, *, activate: bool = False):
        """Block until an item is available; return the next item in ROUND-ROBIN account order (work-conserving:
        never idles while any item exists). Rotates the account cursor so no account is served twice before every
        other active account got a turn — the anti-starvation core."""
        with self._cv:
            # _total can be non-zero while every queued item belongs to an account already executing on another
            # worker. Wait for task_done() to release one of those lanes instead of violating account serialization.
            while not self._live_order and not self._recovery_order:
                self._cv.wait()
            # Prefer a live-headed account across tenants, except for the bounded recovery turn. An account whose
            # FIFO head is recovered stays in the recovery order even if newer live work is queued behind it.
            choose_recovery = bool(self._recovery_order) and (
                not self._live_order or self._live_burst >= self._live_burst_limit
            )
            order = self._recovery_order if choose_recovery else self._live_order
            acct = order.popleft()
            self._ready_class.pop(acct, None)
            bucket = self._buckets[acct]
            candidate = self._candidate(acct)
            if candidate is None:
                # Readiness is maintained under this same lock; this is defensive against future scheduler drift.
                self._refresh_ready(acct)
                self._cv.notify()
                raise RuntimeError("fair queue readiness drifted from executable account state")
            index, recovered, item, lane = candidate
            sequence = bucket[index][0]
            del bucket[index]
            self._remove_repository_sequence(lane, sequence)
            self._total -= 1
            self._live_burst = 0 if recovered else self._live_burst + 1
            if activate:
                if lane is None:
                    self._active_wide_accounts.add(acct)
                else:
                    self._active_repo_lanes.setdefault(acct, set()).add(lane)
                    self._active_repository_ids.add(lane)
                self._leased_by_thread[threading.get_ident()] = (acct, lane)
            if not bucket:
                self._buckets.pop(acct, None)
            self._refresh_all_ready()
            return item

    def task_done(self) -> None:
        with self._cv:
            lease = self._leased_by_thread.pop(threading.get_ident(), None)
            if lease is not None:
                acct, lane = lease
                if lane is None:
                    self._active_wide_accounts.discard(acct)
                else:
                    active_repos = self._active_repo_lanes.get(acct)
                    if active_repos is not None:
                        active_repos.discard(lane)
                        if not active_repos:
                            self._active_repo_lanes.pop(acct, None)
                    self._active_repository_ids.discard(lane)
                bucket = self._buckets.get(acct)
                if not bucket:
                    self._buckets.pop(acct, None)
                self._refresh_all_ready()
            if self._unfinished > 0:
                self._unfinished -= 1
            if self._unfinished == 0:
                self._cv.notify_all()              # wake any wait_idle()-style waiter
            else:
                # A released keyed lane may be the only ready work while other workers are sleeping.
                self._cv.notify()

    @property
    def unfinished_tasks(self) -> int:
        with self._cv:
            return self._unfinished

    def qsize(self) -> int:
        with self._cv:
            return self._total

    def active_count(self) -> int:
        with self._cv:
            return len(self._active_wide_accounts) + sum(len(v) for v in self._active_repo_lanes.values())


class EventQueue:
    """ACK-FAST webhook processing. GitHub times a delivery out after ~10s but does not retry it automatically; a full-repo
    clone+extract (cold start / large push) can exceed that. GitHub rate-limit
    waits are durably scheduled outside the pool rather than slept here. So
    do_POST verifies the signature, ENQUEUES, and acks 202 IMMEDIATELY; a fixed-small daemon pool drains the queue
    and runs handle_event. Same-repository work remains strictly serialized; different repositories may use
    separate workers, while account-wide lifecycle/billing/unknown work forms an exclusive barrier. A per-event
    exception is caught + logged, never killing the worker. The queue is BOUNDED (a backlog can't blow memory);
    a full queue → submit() returns False → the caller 503s → Core recovery redelivers (idempotently).

    FAIR DRAIN (multi-tenant): a strict global FIFO would let ONE tenant's force-push
    burst (a fleet hammering a big monorepo = many slow full-ingest events) head-of-line-block every OTHER
    tenant — their verdicts arriving minutes late. So the backing store is a _FairQueue: strict repository lanes
    behind account-wide reader/writer barriers, live work preferred across tenants, a bounded recovery turn, and
    one shared per-account cap. A flooding tenant can hold at most its slice of the global bound.

    DEPENDENCY INJECTION (so this module imports NOTHING from server.py): `process` is the per-event processor;
    `account_of` / `repo_of` / `branch_from_ref` are the server seams (_event_account_key / _event_repo /
    _branch_from_ref) the fair drain, failure log, and push-coalescing need. server.py wires them at the one
    construction site; tests inject the real key fns where they exercise fairness / coalescing / logging."""

    def __init__(self, db, gh, process, *, account_of=None, repo_of=None, branch_from_ref=None,
                 maxsize: int = 1000, per_account_cap: int = _PER_ACCOUNT_QUEUE_CAP,
                 worker_count: int = 1,
                 per_account_workers: int = 1,
                 retry_attempts: int = _EVENT_RETRY_ATTEMPTS, retry_base_seconds: int = _EVENT_RETRY_BASE_SECONDS):
        self._db, self._gh, self._process = db, gh, process
        self._account_of = account_of            # _event_account_key — the stable tenant key (fair drain)
        self._repo_of = repo_of                  # _event_repo — repo coordinate (failure log line)
        self._branch_from_ref = branch_from_ref  # _branch_from_ref — branch from a push ref (coalescing)
        self._maxsize = maxsize
        # IN-PROCESS RETRY: the MAX total runs of the processor for one event (1 = no retry — one failure is
        # terminal, the historical behaviour). >1 re-runs a FAILED processor in-process up to this many times,
        # waiting retry_base*K between tries, before counting the event a single terminal 'failed'. This is the
        # in-memory companion to the durable inbox: a transient blip is retried HERE (no GitHub redelivery, which
        # a 202'd event won't get anyway); a crash/restart is covered by the durable store's recovery loop.
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_base_seconds = max(0, int(retry_base_seconds))
        self._worker_count = max(1, min(4, int(worker_count)))
        # Reserve at least one worker for another account whenever a real pool exists. Direct one-worker queues
        # necessarily use a cap of one and preserve their historical serialized behavior.
        requested_per_account = max(1, int(per_account_workers))
        self._per_account_workers = (
            1 if self._worker_count == 1
            else min(requested_per_account, self._worker_count - 1)
        )
        self._q = _FairQueue(
            maxsize=maxsize,
            per_account_cap=per_account_cap,
            account_of=account_of,
            lane_of=_repository_lane,
            max_active_per_account=self._per_account_workers,
        )
        # Direct EventQueue construction remains one-worker compatible. Production server_boot explicitly passes
        # a validated fixed-small pool (default 3, max 4); this is keyed concurrency, never unbounded fan-out.
        self._threads: list[threading.Thread] = []
        self._thread = None                    # compatibility alias for the first worker
        self._start_lock = threading.Lock()
        self._started_at = time.time()
        self._processed = 0
        self._failed = 0
        self._retried = 0
        # Durable-claim outcomes are separate from handler success/failure. In particular, a generation deferred
        # behind an older same-account row and a cross-instance duplicate are NOT processed successes, while a
        # missing durable row IS a terminal loss. Content-free running totals make those states externally visible.
        self._claim_deferred = 0
        self._claim_duplicates = 0
        self._claim_missing = 0
        self._claim_unclaimable = 0
        self._counter_lock = threading.Lock()
        self._delivery_receipts = collections.deque(maxlen=_DELIVERY_RECEIPT_CAP)
        self._delivery_receipt_generation = 0
        self._delivery_pending_generations = collections.OrderedDict()
        self._delivery_phases = {}
        self._delivery_receipt_lock = threading.Lock()
        # Crash recovery may have hundreds of durable rows after a deploy. Keep at most one recovery per repository
        # lane queued/in-flight; an account-wide recovery is an exclusive barrier for that account.
        self._recovery_accounts = set()
        # delivery key -> (account, repository-id-or-None) for the entire recovered queue/in-flight lifetime.
        self._recovery_delivery_reservations = {}
        self._recovery_lock = threading.Lock()
        # STUCK-WORKER OBSERVABILITY: the monotonic clock at which the worker DEQUEUED the event it is currently
        # processing — None when the worker is IDLE (blocked on an empty queue = healthy, not stuck). A worker can
        # be is_alive()==True yet WEDGED inside one event forever (a hung network call / deadlock / infinite loop):
        # the thread never dies, so worker_dead stays quiet, /healthz stays 200, and `processed` silently freezes
        # — a LYING GREEN. inflight_age() turns "how long has the worker been stuck on ONE event" into a number the
        # health snapshot + watchdog can alert on. Written by the worker thread, read by the health GET → lock it.
        self._inflight_since: dict[int, float] = {}
        self._inflight_lock = threading.Lock()
        self._clock = time.monotonic   # monotonic: immune to wall-clock jumps (NTP step / DST) that would skew age
        # FORCE-PUSH COALESCING (complements _FairQueue's cross-tenant fairness): delegated to the PushCoalescer
        # collaborator, which owns the _latest_push / _coalesced maps + their lock and the skip/full/normal decision.
        # The coalescing seams (_register_push / _push_coalesce / _latest_push / ...) are forwarded to it below, so
        # the split is invisible to callers + tests. It needs branch_from_ref to derive the (repo,branch) key.
        self._coalescer = PushCoalescer(branch_from_ref=branch_from_ref)
        # Only hand the coalesce predicate to a `process` that accepts it (handle_event / make_db_processor do; a
        # test's bare 4-arg process does not) — stay backward-compatible, never force an unexpected kwarg.
        try:
            _params = inspect.signature(process).parameters
            self._coalesce_supported = "coalesce" in _params or any(p.kind == p.VAR_KEYWORD for p in _params.values())
        except (TypeError, ValueError):
            self._coalesce_supported = False
        # DeliveryStore.wrap_processor marks the commit boundary explicitly: when that wrapper RETURNS normally,
        # the handler transaction has already committed. A bare processor has no such guarantee and keeps the
        # defensive post-return deadline check below.
        self._commits_before_return = bool(getattr(process, "_veripsa_commits_before_return", False))
        # A durable wrapper owns retry generations in Postgres. Re-running it inline after release minted another
        # durable claim inside the same dequeue, multiplying EventQueue's retry ceiling by the durable ceiling
        # (and, for installation fanout, by the per-repository work ceiling). One dequeue is therefore exactly one
        # durable attempt; the recovery scheduler is the only authority that may enqueue the next generation.
        # Bare/in-memory processors retain their short inline transient retry contract.
        self._one_durable_attempt_per_dequeue = bool(
            getattr(process, "_veripsa_one_durable_attempt_per_dequeue", False)
        )

    def start(self):
        with self._start_lock:
            if not self._threads:
                for index in range(self._worker_count):
                    thread = threading.Thread(
                        target=self._run,
                        name=f"veripsa-webhook-worker-{index + 1}",
                        daemon=True,
                    )
                    self._threads.append(thread)
                self._thread = self._threads[0]
                for thread in self._threads:
                    thread.start()
        return self

    def _increment(self, name: str) -> None:
        with self._counter_lock:
            setattr(self, name, int(getattr(self, name)) + 1)

    def submit(self, event_type: str, payload: dict, delivery: str | None = None, *, register_push: bool = True,
               recovered: bool = False) -> bool:
        """Enqueue without blocking. True = accepted; False = queue full (caller 503s; Core recovery redelivers).
        False also when THIS event's account is over its per-account cap (the noisy-neighbor guard) — that one
        tenant 503s + Core redelivers its own event later, while every other tenant's deliveries keep flowing.

        ORDERING (the coalesce visibility invariant): for a push we record its sha in _latest_push BEFORE
        put_nowait makes the item dequeuable — never after. The worker drains on its OWN thread and can get() +
        _push_coalesce() the instant put_nowait's notify fires; if we registered AFTER, the worker could decide
        on a _latest_push that does NOT yet contain the very push it is deciding, and conclude 'skip' against a
        stale-but-older latest. For the NEWEST push in a burst (nothing queued behind it to do the 'full'
        rebuild) that 'skip' would leave a PERMANENTLY stale graph — exactly what _push_coalesce promises can
        never happen. Registering first closes that window: once an item is dequeuable, _latest_push already
        reflects it. If the enqueue is then rejected (queue.Full → 503), we roll the registration back so a push
        that never entered the queue can't masquerade as a queued 'latest' (Core recovery handles the 503'd push).

        register_push=False (the DURABLE-RECOVERY path — start_recovery_loop): enqueue the push WITHOUT touching
        _latest_push. A recovery re-submit replays a delivery that may be STALE (an OLDER sha than a push that
        arrived live since the row was persisted). If it registered, it could OVERWRITE the newer live push's
        _latest_push entry, making the worker coalesce that newer push to 'skip' against a stale latest with
        NOTHING queued behind it to rebuild — the exact permanently-stale-graph the live path guards. So a
        recovered push never advances the newest-push tracking: when IT is processed, _push_coalesce reads the
        LIVE latest — 'skip' iff a newer push is genuinely queued (correct: the newer one rebuilds; the DB-level
        STAGE-3 monotonicity guard in ingest_push also refuses to regress the graph), else 'normal' (it is the
        only push for that branch → re-ingest). The recovered push can neither regress nor mask a newer one."""
        # FORCE-PUSH COALESCING register-before-enqueue (delegated to PushCoalescer): for a LIVE push record its
        # sha as the (repo,branch) latest BEFORE put_nowait makes the item dequeuable, capturing a rollback token.
        # register_push=False (the DURABLE-RECOVERY path) skips this so a replayed/stale push never advances the
        # newest-push tracking. If the enqueue is then rejected (queue.Full → 503) we roll the registration back.
        token = None
        recovery_account = None
        failed_delivery_retry = False
        delivery_key = str(delivery)[:200] if delivery else ""
        # Serialize the tiny in-memory admission window with recovery reservation. DB I/O never happens under
        # this lock. A live submit that arrives after recovery reserved the same durable key is already safely
        # represented by that recovery job, so accept it without enqueueing a duplicate generation.
        with self._recovery_lock:
            reserved_account = self._recovery_delivery_reservations.get(delivery_key) if delivery_key else None
            if not recovered:
                if reserved_account is not None:
                    self._coalescer.rollback(token)
                    return True
                if delivery_key:
                    # X-GitHub-Delivery identifies one immutable delivery. Pending and successfully-terminal
                    # generations are idempotent aliases. The terminal check also closes the DB→memory handoff
                    # race where enqueue returned queued=true, but the original worker finished before this call.
                    # A FAILED terminal is deliberately retryable (durable manual/DLQ redelivery).
                    with self._delivery_receipt_lock:
                        if delivery_key in self._delivery_pending_generations:
                            self._coalescer.rollback(token)
                            return True
                        for recorded_key, _, outcome in reversed(self._delivery_receipts):
                            if recorded_key == delivery_key:
                                if outcome != "failed":
                                    self._coalescer.rollback(token)
                                    return True
                                failed_delivery_retry = True
                                break
            if recovered:
                recovery_account = self._recovery_lane(event_type, payload)
                if reserved_account is not None:
                    if reserved_account != recovery_account:
                        self._coalescer.rollback(token)
                        return False
                    # A reservation with no pending generation is the reserve-before-DB-claim handoff. Once a
                    # generation exists, the same recovered key is already queued/in-flight and must not be added
                    # again. Keep the mapping in both cases until the owning item reaches terminal cleanup.
                    with self._delivery_receipt_lock:
                        if delivery_key in self._delivery_pending_generations:
                            self._coalescer.rollback(token)
                            return False
                elif self._recovery_lane_conflicts(recovery_account):
                    self._coalescer.rollback(token)
                    return False
                else:
                    self._recovery_accounts.add(recovery_account)
                    if delivery_key:
                        self._recovery_delivery_reservations[delivery_key] = recovery_account
            # Bind a generation to the queue item itself while live admission still excludes a competing recovery
            # reservation. Register a live push only AFTER all same-delivery alias checks, but still BEFORE queue
            # publication: an old duplicate must never transiently regress _latest_push, while a real new push must
            # remain visible to a worker as soon as it can dequeue the item.
            preview_item = (event_type, payload, delivery)
            try:
                # A same-id retry after terminal failure is an OLD delivery replay, just like durable recovery.
                # Never let it advance latest-push tracking past a newer live push already queued behind/ahead of
                # it; DB graph-SHA monotonicity remains the final regression guard when the retry executes.
                token = self._coalescer.register_pending(payload) \
                    if (event_type == "push" and register_push and not failed_delivery_retry) else None
                self._q.put_nowait_prepared(
                    preview_item,
                    lambda: (event_type, payload, delivery, self._begin_delivery_generation(delivery), recovery_account),
                    recovered=recovered,
                )
            except queue.Full:
                self._coalescer.rollback(token)                    # this push never entered the queue
                if recovery_account is not None:
                    if self._recovery_delivery_reservations.get(delivery_key) == recovery_account:
                        self._recovery_delivery_reservations.pop(delivery_key, None)
                    self._recovery_accounts.discard(recovery_account)
                return False
            except Exception:
                self._coalescer.rollback(token)
                if recovery_account is not None:
                    if self._recovery_delivery_reservations.get(delivery_key) == recovery_account:
                        self._recovery_delivery_reservations.pop(delivery_key, None)
                    self._recovery_accounts.discard(recovery_account)
                raise
        return True

    def _account_key(self, payload: dict) -> str:
        if self._account_of is None:
            return ""
        try:
            return self._account_of(payload) or ""
        except Exception:
            return ""

    def _recovery_lane(self, event_type: str, payload: dict) -> tuple[str, str | None]:
        return self._account_key(payload), _repository_lane((event_type, payload, None))

    def _recovery_lane_conflicts(self, lane: tuple[str, str | None]) -> bool:
        account, repository_id = lane
        if repository_id is None:
            return any(existing_account == account for existing_account, _ in self._recovery_accounts)
        return (
            (account, None) in self._recovery_accounts
            or any(existing_repository_id == repository_id
                   for _existing_account, existing_repository_id in self._recovery_accounts)
        )

    def can_accept(self, event_type: str, payload: dict, delivery: str | None = None) -> bool:
        """Return whether submit would fit the current global/per-account queue bounds without mutating state."""
        return self._q.can_put_nowait((event_type, payload, delivery))

    def can_accept_recovery(self, event_type: str, payload: dict, delivery: str | None = None) -> bool:
        """Non-mutating recovery preview used by legacy callers/tests; reserve_recovery is authoritative."""
        lane = self._recovery_lane(event_type, payload)
        key = str(delivery)[:200] if delivery else ""
        with self._recovery_lock:
            if self._recovery_lane_conflicts(lane) or (key and key in self._recovery_delivery_reservations):
                return False
            if key:
                with self._delivery_receipt_lock:
                    if key in self._delivery_pending_generations:
                        return False
        return self.can_accept(event_type, payload, delivery)

    def reserve_recovery(self, event_type: str, payload: dict, delivery: str) -> bool:
        """Atomically reserve one durable key/account before its DB claim.

        This closes the live-enqueue/recovery race: either the live generation is already queued and recovery
        declines, or recovery reserves first and the later live submit becomes an accepted alias of that durable
        job instead of appending a duplicate at the account tail.
        """
        key = str(delivery)[:200] if delivery else ""
        if not key:
            return False
        lane = self._recovery_lane(event_type, payload)
        with self._recovery_lock:
            if self._recovery_lane_conflicts(lane) or key in self._recovery_delivery_reservations:
                return False
            if not self._q.can_put_nowait((event_type, payload, delivery)):
                return False
            with self._delivery_receipt_lock:
                if key in self._delivery_pending_generations:
                    return False
            self._recovery_accounts.add(lane)
            self._recovery_delivery_reservations[key] = lane
            return True

    def cancel_recovery_reservation(self, payload: dict, delivery: str) -> None:
        """Release a reservation when the durable claim did not succeed."""
        key = str(delivery)[:200] if delivery else ""
        with self._recovery_lock:
            lane = self._recovery_delivery_reservations.pop(key, None)
            if lane is not None:
                self._recovery_accounts.discard(lane)

    # ── FORCE-PUSH COALESCING seams — forwarded to the PushCoalescer collaborator. Kept as methods/attributes on
    #    EventQueue (rather than making callers reach into _coalescer) so the extraction is invisible to the tests
    #    and any caller that exercises coalescing directly (_register_push / _push_coalesce / _latest_push / ...).
    def _register_push(self, payload: dict) -> None:
        """Record the LATEST queued push sha per (repo,branch). Forwarded to PushCoalescer.register."""
        self._coalescer.register(payload)

    def _push_registry_key(self, payload: dict):
        """((repo, branch), sha) from a push payload, or None when not coalesceable. Forwarded to PushCoalescer."""
        return self._coalescer.registry_key(payload)

    def _prune_push_state(self, payload: dict) -> None:
        """LIFETIME RECLAIM for the coalescing maps after a push finishes. Forwarded to PushCoalescer.prune."""
        self._coalescer.prune(payload)

    def _push_coalesce(self, repo: str, branch: str, sha: str) -> str:
        """The skip/full/normal coalescing decision for a main-branch push. Forwarded to PushCoalescer.coalesce."""
        return self._coalescer.coalesce(repo, branch, sha)

    def _stable_coalesce(self, cache: dict):
        """Per-event memoizing wrapper over the coalesce decision (for the in-process retry of a push). Forwarded
        to PushCoalescer.stable_coalesce."""
        return self._coalescer.stable_coalesce(cache)

    @property
    def _latest_push(self) -> dict:
        """The (repo,branch) -> latest-queued-sha map, owned by the PushCoalescer. Exposed read-only here so the
        coalesce-visibility tests that inspect eq._latest_push.get((repo,branch)) keep working post-extraction."""
        return self._coalescer._latest_push

    @property
    def _coalesced(self) -> set:
        """The set of (repo,branch) with a skipped push pending a full re-ingest, owned by the PushCoalescer."""
        return self._coalescer._coalesced

    @property
    def _push_lock(self):
        """The lock guarding the coalescing maps, owned by the PushCoalescer."""
        return self._coalescer._push_lock

    def _run(self):
        while True:
            event_type, payload, delivery, receipt_generation, recovery_account = self._q.get(activate=True)
            self._mark_delivery_processing(delivery, receipt_generation)
            check_target = self._delivery_check_target(event_type, payload)
            # MARK the start of in-flight processing (after get() returns — so blocking on an EMPTY queue counts as
            # idle, never stuck). Cleared in the finally below. The pair is what inflight_age() reads to tell a
            # wedged worker (long in-flight age) from a healthy idle one (no in-flight event).
            with self._inflight_lock:
                self._inflight_since[threading.get_ident()] = self._clock()
            # IN-PROCESS RETRY (202-then-fail safety): a 202'd event won't be redelivered by GitHub, so a transient
            # processor failure (a momentary DB/network blip) is retried HERE — up to retry_attempts TOTAL runs —
            # before being counted a single terminal 'failed'. The coalesce decision is memoized for the event so a
            # retry replays the SAME (possibly destructive 'full') decision instead of recomputing it. A durable
            # delivery is different: its wrapper releases one exact DB generation and the recovery scheduler owns
            # every later generation. It is never reclaimed inline, so in-memory and durable ceilings cannot
            # multiply.
            # Cancellation and intentional deferral never inline-retry.
            _coalesce_cache: dict = {}
            _coalesce = self._stable_coalesce(_coalesce_cache)
            # ONE deadline per dequeued delivery, not one deadline per processor attempt. Install it immediately
            # inside the try/finally that owns this queue item so reset + task_done are guaranteed together. Keep
            # it active across every attempt; a fresh event dequeued afterwards receives a fresh budget.
            event_budget_token = event_budget.begin()
            event_account_token = event_budget.bind_account(
                self._account_key(payload))
            try:
                attempt = 0
                while True:
                    # A failed attempt must not authorize the terminal delivery receipt. Start a fresh exact-target
                    # observation for every retry so only the attempt that actually succeeds can report its Check
                    # write/no-op outcome; an earlier failed attempt cannot describe the terminal successful one.
                    check_observation = check_observation_token = None
                    try:
                        # Check before counting/starting an attempt. In particular, a backoff that consumed the
                        # remaining allowance must not launch one last processor run beyond the event deadline.
                        event_budget.raise_if_expired()
                        attempt += 1
                        if delivery and check_target is not None:
                            check_observation, check_observation_token = begin_check_delivery_observation(*check_target)
                        try:
                            if self._coalesce_supported:
                                result = self._process(event_type, payload, self._db, self._gh, coalesce=_coalesce)
                            else:
                                result = self._process(event_type, payload, self._db, self._gh)
                        finally:
                            if check_observation_token is not None:
                                end_check_delivery_observation(check_observation_token)
                        claim_outcome = result.get(_WORKER_CLAIM_OUTCOME) if isinstance(result, dict) else None
                        if claim_outcome in ("deferred", "duplicate"):
                            # No handler ran. Remove this in-memory receipt generation without minting a false
                            # terminal success: deferred work remains queued durably for ordered recovery, while a
                            # duplicate is already owned/finalised by another worker generation.
                            if claim_outcome == "deferred":
                                self._increment("_claim_deferred")
                            else:
                                self._increment("_claim_duplicates")
                            self._abandon_delivery_generation(delivery, receipt_generation)
                            print(f"webhook worker {claim_outcome.upper()}: event={event_type} "
                                  f"delivery={_delivery_presence(delivery)} "
                                  f"reason={result.get('claim_reason') or 'unknown'}",
                                  flush=True)
                            break
                        if claim_outcome in ("missing", "unclaimable"):
                            # Missing durable authority is real loss, not a clean no-op. Other impossible/terminal
                            # claim states are equally not a processed event. Count once and expose a failed receipt
                            # without burning in-process retries on a condition retry cannot repair.
                            if claim_outcome == "missing":
                                self._increment("_claim_missing")
                            else:
                                self._increment("_claim_unclaimable")
                            self._increment("_failed")
                            self._record_delivery_outcome(delivery, "failed", generation=receipt_generation)
                            _record_outcome("failed")
                            print(f"webhook worker FAILED claim: event={event_type} "
                                  f"delivery={_delivery_presence(delivery)} "
                                  f"outcome={claim_outcome} reason={result.get('claim_reason') or 'unknown'}",
                                  flush=True)
                            break
                        # A collaborator that returns after consuming the hard deadline must not be recorded as a
                        # successful delivery. Killable graph work is terminated at its boundary; this check keeps
                        # accounting correct for every other cooperative/late-returning stage. The durable wrapper
                        # is the deliberate exception: its normal return is a private capability proving the DB
                        # transaction already committed, so failing it here would produce "committed + failed" and
                        # authorize a contradictory retry. Durable claim outcomes above are classified first
                        # because they did not run/commit a handler.
                        if not self._commits_before_return:
                            event_budget.raise_if_expired()
                        self._increment("_processed")
                        check_outcome = check_observation.outcome if check_observation is not None else None
                        receipt = f"check_{check_outcome}" if check_outcome in ("noop", "updated") else "processed"
                        self._record_delivery_outcome(delivery, receipt, generation=receipt_generation)
                        _record_outcome("processed")            # ringbuffer feed → /healthz failure_ratio_5min + /alarmz
                        break                                   # success → stop retrying
                    except EventBudgetExceeded as e:
                        # EventBudgetExceeded is cancellation (outside
                        # Exception), but it terminates only THIS queue
                        # generation. DeliveryStore has already attempted its
                        # exact-lease release in the reserved tail. Never
                        # multiply the expired work with an inline retry, and
                        # never let cancellation kill the daemon worker.
                        self._increment("_failed")
                        self._record_delivery_outcome(
                            delivery, "failed", generation=receipt_generation)
                        _record_outcome("failed")
                        try:
                            repo = (self._repo_of(payload) if self._repo_of else None) or "?"
                            acct = (self._account_of(payload) if self._account_of else None) or "?"
                        except Exception:
                            repo, acct = "?", "?"
                        try:
                            print(f"webhook worker CANCELLED: event={event_type} repo={repo} account={acct} "
                                  f"delivery={_delivery_presence(delivery)} attempts={attempt} "
                                  f"error_code={_bounded_error_code(e)}", flush=True)
                        except Exception:
                            pass
                        break
                    except Exception as e:                      # one bad event must never kill the worker
                        # Cancellation is owned by the explicit branch above.
                        # Retry an ordinary transient failure only when its
                        # complete intended backoff fits inside the remaining
                        # delivery budget. The durable inbox remains the
                        # later/restart retry backstop.
                        requested_wait = self._retry_base_seconds * attempt
                        remaining = event_budget.remaining()
                        bounded_wait = requested_wait if remaining is None else min(
                            float(requested_wait), max(0.0, remaining)
                        )
                        durable_generation = (
                            self._one_durable_attempt_per_dequeue
                            and isinstance(payload, dict)
                            and bool(payload.get("_veripsa_delivery_key"))
                        )
                        can_retry = (
                            attempt < self._retry_attempts
                            and not durable_generation
                            and event_budget.can_retry(bounded_wait)
                            # If capping changed the backoff, there is not enough budget for the configured wait
                            # plus another processor attempt. Fail now instead of sleeping to the deadline.
                            and bounded_wait >= requested_wait
                        )
                        if can_retry:
                            self._increment("_retried")
                            _record_outcome("retried")           # ringbuffer feed (intermediate, not in the ratio)
                            # OBSERVABILITY: a retry is not yet a loss — log it so a flapping event is visible
                            # before it either succeeds or exhausts its budget. Content-free (no payload bodies).
                            try:
                                _repo = (self._repo_of(payload) if self._repo_of else None) or "?"
                                _acct = (self._account_of(payload) if self._account_of else None) or "?"
                            except Exception:
                                _repo, _acct = "?", "?"
                            print(f"webhook worker RETRY {attempt}/{self._retry_attempts}: event={event_type} "
                                  f"repo={_repo} account={_acct} delivery={_delivery_presence(delivery)} "
                                  f"error_code={_bounded_error_code(e)}", flush=True)
                            if bounded_wait:
                                # bounded_wait is never larger than the event remainder. The pre-attempt expiry
                                # check above catches clock drift at wake-up without launching work over budget.
                                time.sleep(bounded_wait)
                            continue
                        # budget exhausted → this IS a terminal loss (counted ONCE) → the durable store's
                        # recovery loop is the cross-restart backstop; for an in-memory-only queue GitHub will
                        # not redeliver a 202'd event, so the loss is real and must be loud.
                        self._increment("_failed")
                        self._record_delivery_outcome(delivery, "failed", generation=receipt_generation)
                        _record_outcome("failed")               # ringbuffer feed → drives failure_ratio_5min + /alarmz
                        # OBSERVABILITY: name the REPO + account in the failure line — "which customer / which
                        # repo failed ingest?" is the first question when debugging a runtime failure, and without it
                        # the log only says "a push failed". Content-free (repo full_name + GitHub account id =
                        # public git metadata; never code/bodies). Best-effort: a malformed payload yields '?'.
                        try:
                            repo = (self._repo_of(payload) if self._repo_of else None) or "?"
                            acct = (self._account_of(payload) if self._account_of else None) or "?"
                        except Exception:
                            repo, acct = "?", "?"
                        print(f"webhook worker FAILED: event={event_type} repo={repo} account={acct} "
                              f"delivery={_delivery_presence(delivery)} attempts={attempt} "
                              f"error_code={_bounded_error_code(e)}", flush=True)
                        break
            finally:
                try:
                    # LIFETIME RECLAIM: a fully-processed push whose sha is still the latest queued for its
                    # (repo,branch) has spent its coalescing bookkeeping — drop it so _latest_push/_coalesced don't
                    # grow one entry per (repo,branch) forever (the unbounded leak). Safe on the failure path too:
                    # a failed delivery retry is treated like recovery and does not re-register an old sha; if
                    # something newer is queued its marker remains, otherwise the retry safely takes the normal
                    # ingest path. No-op for non-push events. Never let cleanup mask the event's own outcome.
                    if event_type == "push":
                        try:
                            self._prune_push_state(payload)
                        except Exception:
                            pass
                    with self._inflight_lock:   # event done (or failed) → worker is no longer in-flight on it
                        self._inflight_since.pop(threading.get_ident(), None)
                    if recovery_account is not None:
                        with self._recovery_lock:
                            recovery_key = str(delivery)[:200] if delivery else ""
                            if self._recovery_delivery_reservations.get(recovery_key) == recovery_account:
                                self._recovery_delivery_reservations.pop(recovery_key, None)
                            self._recovery_accounts.discard(recovery_account)
                finally:
                    # Pair the ContextVar token and queue accounting even if future cleanup code regresses.
                    try:
                        try:
                            event_budget.reset_account(
                                event_account_token)
                        finally:
                            event_budget.end(event_budget_token)
                    finally:
                        self._q.task_done()

    def qsize(self) -> int:
        return self._q.qsize()

    def maxsize(self) -> int:
        return self._maxsize

    def processed(self) -> int:
        with self._counter_lock:
            return self._processed

    def failed(self) -> int:
        with self._counter_lock:
            return self._failed

    def retried(self) -> int:
        """How many in-process RETRIES the worker has done (a re-run of a transiently-failed event). Distinct from
        `failed` (terminal losses): a retried-then-succeeded event counts here but NOT in failed."""
        with self._counter_lock:
            return self._retried

    def claim_deferred(self) -> int:
        with self._counter_lock:
            return self._claim_deferred

    def claim_duplicates(self) -> int:
        with self._counter_lock:
            return self._claim_duplicates

    def claim_missing(self) -> int:
        with self._counter_lock:
            return self._claim_missing

    def claim_unclaimable(self) -> int:
        with self._counter_lock:
            return self._claim_unclaimable

    @staticmethod
    def _delivery_check_target(event_type: str, payload: dict) -> tuple[str, str] | None:
        """Exact repo/head target whose Check write can authorize a deployment receipt."""
        if not isinstance(payload, dict):
            return None
        repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
        repo = repository.get("full_name")
        if event_type == "check_suite":
            suite = payload.get("check_suite") if isinstance(payload.get("check_suite"), dict) else {}
            sha = suite.get("head_sha")
        elif event_type == "pull_request":
            pull = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
            head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
            sha = head.get("sha")
        else:
            return None
        return (str(repo), str(sha)) if repo and sha else None

    def _begin_delivery_generation(self, delivery: str | None) -> int | None:
        """Reserve the newest generation for a submitted delivery id."""
        if not delivery:
            return None
        key = str(delivery)[:200]
        with self._delivery_receipt_lock:
            self._delivery_receipt_generation += 1
            generation = self._delivery_receipt_generation
            self._delivery_pending_generations[key] = generation
            self._delivery_phases[key] = (generation, "queued")
            self._delivery_pending_generations.move_to_end(key)
            while len(self._delivery_pending_generations) > _DELIVERY_RECEIPT_CAP:
                evicted_key, _ = self._delivery_pending_generations.popitem(last=False)
                self._delivery_phases.pop(evicted_key, None)
            return generation

    def _mark_delivery_processing(self, delivery: str | None, generation: int | None) -> None:
        if not delivery or generation is None:
            return
        key = str(delivery)[:200]
        with self._delivery_receipt_lock:
            if self._delivery_pending_generations.get(key) == generation:
                self._delivery_phases[key] = (generation, "processing")

    def _record_delivery_outcome(self, delivery: str | None, outcome: str,
                                 *, generation: int | None = None) -> None:
        """Record one terminal content-free delivery receipt for the authenticated deploy probe."""
        if not delivery or outcome not in ("processed", "check_noop", "check_updated", "failed"):
            return
        key = str(delivery)[:200]
        with self._delivery_receipt_lock:
            if generation is None:  # direct operator/test insertion: create a standalone completed generation
                self._delivery_receipt_generation += 1
                generation = self._delivery_receipt_generation
            else:
                latest = self._delivery_pending_generations.get(key)
                if latest != generation:
                    return  # a newer same-id submit exists; this older completion is not its receipt
                self._delivery_pending_generations.pop(key, None)
                self._delivery_phases.pop(key, None)
            self._delivery_receipts.append((key, generation, outcome))

    def _abandon_delivery_generation(self, delivery: str | None, generation: int | None) -> None:
        """Drop a non-terminal memory generation without manufacturing a success receipt.

        Deferred work still exists in the durable inbox and will receive a new generation when recovery admits it;
        a duplicate is owned elsewhere. In both cases this worker must stop reporting queued/processing for a job it
        no longer owns while leaving any newer same-id generation untouched.
        """
        if not delivery or generation is None:
            return
        key = str(delivery)[:200]
        with self._delivery_receipt_lock:
            if self._delivery_pending_generations.get(key) == generation:
                self._delivery_pending_generations.pop(key, None)
                self._delivery_phases.pop(key, None)
                # This generation is the newest observation for the immutable delivery id. Once it yields, an older
                # terminal receipt must not reappear as current (for example failed -> recovered -> deferred).
                # Remove only this key's bounded content-free history; unrelated deployment receipts stay intact.
                self._delivery_receipts = collections.deque(
                    (receipt for receipt in self._delivery_receipts if receipt[0] != key),
                    maxlen=_DELIVERY_RECEIPT_CAP,
                )

    def delivery_outcome(self, delivery: str | None) -> str | None:
        """Return the newest terminal outcome for a delivery id, or None while unknown/pending.

        Linear reverse lookup is intentionally bounded by _DELIVERY_RECEIPT_CAP and happens only on an
        authenticated post-deploy probe, never on normal webhook processing.
        """
        if not delivery:
            return None
        key = str(delivery)[:200]
        with self._delivery_receipt_lock:
            if key in self._delivery_pending_generations:
                return None
            for recorded_key, _, outcome in reversed(self._delivery_receipts):
                if recorded_key == key:
                    return outcome
        return None

    def delivery_status(self, delivery: str | None) -> str:
        """Return queued/processing/terminal for a known generation, or pending when it is unknown.

        ``delivery_outcome`` intentionally keeps its legacy terminal-only API. This richer private probe lets a
        deployment distinguish 139 seconds of fair-queue waiting from 112 seconds of real Check processing and
        apply a separate bounded timeout to each stage.
        """
        if not delivery:
            return "pending"
        key = str(delivery)[:200]
        with self._delivery_receipt_lock:
            phase = self._delivery_phases.get(key)
            latest = self._delivery_pending_generations.get(key)
            if phase is not None and phase[0] == latest and phase[1] in ("queued", "processing"):
                return phase[1]
            for recorded_key, _, outcome in reversed(self._delivery_receipts):
                if recorded_key == key:
                    return outcome
        return "pending"

    def uptime(self) -> float:
        return time.time() - self._started_at

    def is_alive(self) -> bool:
        with self._start_lock:
            return len(self._threads) == self._worker_count and all(t.is_alive() for t in self._threads)

    def worker_count(self) -> int:
        return self._worker_count

    def per_account_workers(self) -> int:
        return self._per_account_workers

    def alive_workers(self) -> int:
        with self._start_lock:
            return sum(1 for thread in self._threads if thread.is_alive())

    def inflight_count(self) -> int:
        with self._inflight_lock:
            return len(self._inflight_since)

    def metrics(self) -> dict:
        """Content-free pool state for health/diagnostics."""
        return {
            "worker_count": self.worker_count(),
            "per_account_workers": self.per_account_workers(),
            "alive_workers": self.alive_workers(),
            "inflight_count": self.inflight_count(),
            "queue_depth": self.qsize(),
            "processed": self.processed(),
            "failed": self.failed(),
        }

    def inflight_age(self) -> float | None:
        """Age of the oldest event currently executing in the fixed pool.

        None means every lane is idle. A value beyond the delivery ceiling
        means one live thread failed to cross a cancellation boundary; other
        keyed lanes may still progress. Monotonic and lock-guarded.
        """
        with self._inflight_lock:
            since = min(self._inflight_since.values()) if self._inflight_since else None
        if since is None:
            return None
        age = self._clock() - since
        return age if age >= 0 else 0.0   # monotonic can't go backwards, but never report a negative age

    def stuck_worker_count(self, threshold_seconds: float) -> int:
        """Count pool lanes beyond one hard event ceiling.

        This lets liveness distinguish one isolated pathological repository
        from total pool exhaustion. Only counts cross the health boundary;
        thread ids, account keys, repositories, and delivery ids never do.
        """
        threshold = max(0.0, float(threshold_seconds))
        now = self._clock()
        with self._inflight_lock:
            started = tuple(self._inflight_since.values())
        return sum(
            1 for since in started
            if max(0.0, now - since) >= threshold
        )

    def wait_idle(self, timeout: float = 2.0) -> bool:
        """Block until the queue is fully drained (the worker called task_done for every item) or timeout. Used
        by tests; not on the hot path."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.005)
        return False
