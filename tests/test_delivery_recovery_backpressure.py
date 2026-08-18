#!/usr/bin/env python3
"""Durable delivery recovery backpressure.

Production reserves each delivery/lane in EventQueue before submitting an
unclaimed recovery preview. The reservation prevents duplicate local enqueue,
while delaying the DB claim until worker dequeue prevents a 20-item memory
backlog from consuming retry windows and pinning durable lanes before execution.
Legacy workers without atomic reservations retain claim-before-submit.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import delivery_queue as DQ  # noqa: E402
import event_processor as EP  # noqa: E402
import health_watchdog as HW  # noqa: E402
import psycopg2  # noqa: E402
from event_queue import EventQueue, _FairQueue  # noqa: E402


class FakeStore:
    def __init__(self, rows=1):
        self.status = "queued"
        self.claims = 0
        self.finishes = 0
        self.releases = 0
        self.defers = 0
        self.finished_leases = []
        self.released_leases = []
        self.deferred_leases = []
        self.defer_reasons = []
        self.pending_limits = []
        self.rows = rows
        self.queued_keys = {f"delivery-{i}" for i in range(1, rows + 1)}
        self.processing_keys = set()
        self.lease_generation = 0
        self.leases = {f"delivery-{i}": 0 for i in range(1, rows + 1)}
        self.payload = {
            "number": 569,
            "installation": {"account": {"id": 42, "login": "RollNuts", "type": "Organization"}},
            "repository": {"full_name": "RollNuts/veripsa", "owner": {"id": 42}},
            "pull_request": {
                "base": {"ref": "main", "sha": "a" * 40},
                "head": {"ref": "dogfood/post-deploy-canary", "sha": "b" * 40},
                "draft": True,
            },
        }

    def pending(self, limit=100):
        self.pending_limits.append(limit)
        if self.rows > 1:
            return [{"key": key, "event_type": "pull_request", "payload": self.payload}
                    for key in sorted(self.queued_keys)[:limit]]
        if self.status == "queued":
            return [{"key": "delivery-1", "event_type": "pull_request", "payload": self.payload}]
        return []

    def claim(self, key):
        self.claims += 1
        if self.rows > 1:
            if key in self.queued_keys:
                self.queued_keys.remove(key)
                self.processing_keys.add(key)
                self.leases[key] += 1
                return {"claimed": True, "event_type": "pull_request", "payload": self.payload,
                        "lease_generation": self.leases[key]}
            return {"claimed": False}
        if key.startswith("delivery-") and self.status == "queued":
            self.status = "processing"
            self.lease_generation += 1
            return {"claimed": True, "event_type": "pull_request", "payload": self.payload,
                    "lease_generation": self.lease_generation}
        if self.status == "processing":
            return {"claimed": False, "reason": "already_owned", "status": "processing"}
        if self.status == "done":
            return {"claimed": False, "reason": "already_finished", "status": "done"}
        return {"claimed": False, "reason": "not_claimable", "status": self.status}

    def finish(self, key, lease_generation):
        self.finishes += 1
        self.finished_leases.append(lease_generation)
        if (key == "delivery-1" and self.status == "processing"
                and lease_generation == self.lease_generation):
            self.status = "done"
            return True
        return False

    def release(self, key, error, lease_generation):
        self.releases += 1
        self.released_leases.append(lease_generation)
        if lease_generation == self.lease_generation:
            self.status = "queued"
            return "queued"
        return "missing"

    def defer(self, key, not_before, reason, lease_generation):
        self.defers += 1
        self.deferred_leases.append(lease_generation)
        self.defer_reasons.append(reason)
        if (key == "delivery-1" and self.status == "processing"
                and lease_generation == self.lease_generation):
            self.status = "queued"
            return True
        return False


class RecordingWorker:
    def __init__(self, accept=True, depth=0):
        self.accept = accept
        self.depth = depth
        self.submitted = []

    def can_accept(self, event_type, payload, delivery=None):
        return self.accept

    def submit(self, event_type, payload, delivery=None, **kwargs):
        if not self.accept:
            return False
        self.submitted.append((event_type, payload, delivery, kwargs))
        self.depth += 1
        return True

    def qsize(self):
        return self.depth


class RecoveryAwareWorker(RecordingWorker):
    def __init__(self):
        super().__init__()
        self.recovery_accounts = set()

    @staticmethod
    def _account(payload):
        return str(payload.get("installation", {}).get("account", {}).get("id", ""))

    def can_accept_recovery(self, event_type, payload, delivery=None):
        return self._account(payload) not in self.recovery_accounts

    def submit(self, event_type, payload, delivery=None, **kwargs):
        if kwargs.get("recovered"):
            account = self._account(payload)
            if account in self.recovery_accounts:
                return False
            self.recovery_accounts.add(account)
        return super().submit(event_type, payload, delivery, **kwargs)


class MixedAccountStore:
    def __init__(self):
        self.claims = 0
        self.pending_limits = []
        self.rows = []
        for index, account in enumerate([42, 42, 42, 42, 42, 99], 1):
            payload = {
                "number": index,
                "installation": {"account": {"id": account}},
                "repository": {"full_name": f"acct-{account}/repo", "owner": {"id": account}},
            }
            self.rows.append({"key": f"mixed-{index}", "event_type": "pull_request", "payload": payload})

    def pending(self, limit=100):
        self.pending_limits.append(limit)
        return list(self.rows[:limit])

    def claim(self, key):
        self.claims += 1
        row = next((row for row in self.rows if row["key"] == key), None)
        if row is None:
            return {"claimed": False}
        self.rows.remove(row)
        return {"claimed": True, "event_type": row["event_type"], "payload": row["payload"],
                "lease_generation": 1}

    def release(self, key, error, lease_generation):
        raise AssertionError("mixed-account submit should not need release")


def check(label, passed):
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    return bool(passed)


def main() -> int:
    checks = []

    marketplace_raw = {
        "action": "purchased",
        "effective_date": "2026-07-16T12:34:56+00:00",
        "marketplace_purchase": {"account": {"id": 777}, "plan": {"id": 3, "name": "pro"}},
        "ignored_secret": "must-not-persist",
    }
    marketplace_sanitized = DQ.sanitize_payload("marketplace_purchase", marketplace_raw)
    marketplace_invalid = DQ.sanitize_payload(
        "marketplace_purchase", dict(marketplace_raw, effective_date="not-a-timestamp"),
    )
    checks.append(check(
        "durable Marketplace routing retains validated HWM time and derives its signed billing account",
        marketplace_sanitized.get("effective_date") == "2026-07-16T12:34:56+00:00"
        and "ignored_secret" not in marketplace_sanitized
        and "effective_date" not in marketplace_invalid
        and EP._event_account_key(marketplace_raw) == "777",
    ))

    store = FakeStore()
    worker = RecordingWorker()
    first = DQ._recover_pending_deliveries(store, worker, limit=100)
    second = DQ._recover_pending_deliveries(store, worker, limit=100)
    checks.append(check(
        "recovery claims before submit, so the same queued row is not submitted again on the next tick",
        first == {"submitted": 1, "skipped_full": 0, "skipped_claim": 0, "skipped_backlog": 0}
        and second == {"submitted": 0, "skipped_full": 0, "skipped_claim": 0, "skipped_backlog": 0}
        and store.claims == 1
        and len(worker.submitted) == 1
        and worker.submitted[0][1].get("_veripsa_delivery_preclaimed") is True
        and worker.submitted[0][1].get("_veripsa_delivery_lease_generation") == 1,
    ))

    blocked = FakeStore()
    blocked_worker = RecordingWorker(accept=False)
    blocked_res = DQ._recover_pending_deliveries(blocked, blocked_worker, limit=100)
    checks.append(check(
        "recovery checks memory capacity before claim, so a full queue does not burn durable attempts",
        blocked_res == {"submitted": 0, "skipped_full": 1, "skipped_claim": 0, "skipped_backlog": 0}
        and blocked.claims == 0
        and blocked.releases == 0
        and blocked.status == "queued",
    ))

    busy = FakeStore(rows=10)
    busy_worker = RecordingWorker(depth=20)
    busy_res = DQ._recover_pending_deliveries(busy, busy_worker, limit=100, max_queue_depth=20)
    checks.append(check(
        "recovery backs off when the live worker queue is already at the recovery headroom cap",
        busy_res == {"submitted": 0, "skipped_full": 0, "skipped_claim": 0, "skipped_backlog": 1}
        and busy.claims == 0
        and busy.pending_limits == [],
    ))

    paced = FakeStore(rows=10)
    paced_worker = RecordingWorker(depth=18)
    paced_res = DQ._recover_pending_deliveries(paced, paced_worker, limit=100, max_queue_depth=20)
    checks.append(check(
        "recovery only claims worker headroom, so old durable rows cannot flood ahead of live webhooks",
        paced_res == {"submitted": 2, "skipped_full": 0, "skipped_claim": 0, "skipped_backlog": 1}
        and paced.claims == 2
        and paced.pending_limits == [100]
        and len(paced_worker.submitted) == 2
        and paced_worker.qsize() == 20,
    ))

    mixed = MixedAccountStore()
    mixed_worker = RecoveryAwareWorker()
    mixed_res = DQ._recover_pending_deliveries(mixed, mixed_worker, limit=100, max_queue_depth=20)
    checks.append(check(
        "one owner's recovery backlog cannot hide a later tenant in the bounded pending scan",
        mixed_res == {"submitted": 2, "skipped_full": 4, "skipped_claim": 0, "skipped_backlog": 0}
        and mixed.claims == 2
        and mixed.pending_limits == [100]
        and mixed_worker.recovery_accounts == {"42", "99"}
        and len(mixed_worker.submitted) == 2,
    ))

    low_limit = MixedAccountStore()
    low_limit_worker = RecoveryAwareWorker()
    low_limit_worker.recovery_accounts.add("42")
    low_limit_res = DQ._recover_pending_deliveries(low_limit, low_limit_worker, limit=1, max_queue_depth=20)
    checks.append(check(
        "a legal low recovery limit still scans past a busy owner to one free tenant",
        low_limit_res == {"submitted": 1, "skipped_full": 5, "skipped_claim": 0, "skipped_backlog": 0}
        and low_limit.pending_limits == [21]
        and low_limit.claims == 1
        and len(low_limit_worker.submitted) == 1
        and low_limit_worker.recovery_accounts == {"42", "99"},
    ))

    class ClaimRaisingStore(FakeStore):
        def claim(self, key):
            self.claims += 1
            raise RuntimeError("temporary claim failure")

    claim_error_store = ClaimRaisingStore()
    claim_error_owner = DQ.DeliveryStore("postgresql://unused")
    claim_error_owner.claim = claim_error_store.claim
    claim_error_owner.finish = claim_error_store.finish
    claim_error_owner.release = claim_error_store.release
    claim_error_worker = EventQueue(
        None, None, claim_error_owner.wrap_processor(lambda *_args, **_kwargs: None),
        account_of=lambda payload: str(payload.get("installation", {}).get("account", {}).get("id", "")),
        retry_attempts=1,
    )
    claim_admission = DQ._recover_pending_deliveries(
        claim_error_store, claim_error_worker, limit=100,
    )
    checks.append(check(
        "production recovery reserves/enqueues without starting the DB claim or retry window",
        claim_admission["submitted"] == 1
        and claim_error_store.claims == 0
        and claim_error_store.status == "queued"
        and not claim_error_worker.can_accept_recovery(
            "pull_request", claim_error_store.payload, "delivery-1",
        ),
    ))

    bare_store = FakeStore()
    bare_event_queue = EventQueue(
        None, None, lambda *_args, **_kwargs: None,
        account_of=lambda payload: str(
            payload.get("installation", {}).get("account", {}).get("id", "")
        ),
        retry_attempts=1,
    )
    bare_result = DQ._recover_pending_deliveries(
        bare_store, bare_event_queue, limit=100,
    )
    checks.append(check(
        "a bare EventQueue reservation is not mistaken for dequeue-claim authority",
        bare_result["submitted"] == 1
        and bare_store.claims == 1
        and bare_store.status == "processing",
    ))

    class SubmitRaisingWorker:
        _one_durable_attempt_per_dequeue = True

        def __init__(self):
            self.reserved = False
            self.cancelled = False

        def reserve_recovery(self, event_type, payload, delivery):
            self.reserved = True
            return True

        def cancel_recovery_reservation(self, payload, delivery):
            self.cancelled = True
            self.reserved = False

        @staticmethod
        def qsize():
            return 0

        @staticmethod
        def submit(*args, **kwargs):
            raise RuntimeError("temporary memory admission failure")

    submit_error_store = FakeStore()
    submit_error_worker = SubmitRaisingWorker()
    submit_raised = False
    try:
        DQ._recover_pending_deliveries(submit_error_store, submit_error_worker, limit=100)
    except RuntimeError:
        submit_raised = True
    checks.append(check(
        "an unclaimed submit exception cancels memory admission without mutating the durable row",
        submit_raised
        and submit_error_worker.cancelled
        and not submit_error_worker.reserved
        and submit_error_store.claims == 0
        and submit_error_store.releases == 0
        and submit_error_store.status == "queued",
    ))

    class WindowQueueStore:
        """Twenty independent durable rows with observable window start."""

        def __init__(self, rows=20):
            self.rows = {}
            self.claims = 0
            for index in range(rows):
                key = f"window-{index:02d}"
                account = 1000 + index
                self.rows[key] = {
                    "status": "queued",
                    "attempts": 0,
                    "window": None,
                    "lease": 0,
                    "payload": {
                        "installation": {"account": {"id": account}},
                        "repository": {
                            "id": str(9000 + index),
                            "full_name": f"acct-{account}/repo-{index}",
                            "owner": {"id": account},
                        },
                        "ref": "refs/heads/main",
                        "after": f"{index + 1:040x}",
                    },
                }

        def pending(self, limit=100):
            return [
                {"key": key, "event_type": "push", "payload": row["payload"]}
                for key, row in self.rows.items()
                if row["status"] == "queued"
            ][:limit]

        def claim(self, key):
            row = self.rows[key]
            self.claims += 1
            if row["status"] != "queued":
                return {"claimed": False, "reason": "already_owned", "status": row["status"]}
            row["status"] = "processing"
            row["attempts"] += 1
            row["lease"] += 1
            row["window"] = time.monotonic() + 120.0
            return {
                "claimed": True,
                "event_type": "push",
                "payload": row["payload"],
                "lease_generation": row["lease"],
                DQ._DELIVERY_RETRY_DEADLINE_MONOTONIC: row["window"],
            }

        def finish(self, key, lease_generation):
            row = self.rows[key]
            if row["status"] == "processing" and row["lease"] == lease_generation:
                row["status"] = "done"
                return True
            return False

        def release(self, key, error, lease_generation):
            row = self.rows[key]
            if row["status"] == "processing" and row["lease"] == lease_generation:
                row["status"] = "queued"
                return "queued"
            return "ownership_lost"

    window_store = WindowQueueStore()
    window_owner = DQ.DeliveryStore("postgresql://unused")
    window_owner.claim = window_store.claim
    window_owner.finish = window_store.finish
    window_owner.release = window_store.release
    first_wave_started = threading.Event()
    release_first_wave = threading.Event()
    active_lock = threading.Lock()
    active = 0

    def window_handler(_event_type, _payload, _db, _gh, coalesce=None):
        nonlocal active
        with active_lock:
            active += 1
            if active == 3:
                first_wave_started.set()
        release_first_wave.wait(3.0)

    window_worker = EventQueue(
        None, None, window_owner.wrap_processor(window_handler),
        account_of=lambda payload: str(
            payload.get("installation", {}).get("account", {}).get("id", "")
        ),
        worker_count=3,
        per_account_workers=2,
        retry_attempts=3,
        retry_base_seconds=0,
    )
    window_admission = DQ._recover_pending_deliveries(
        window_store, window_worker, limit=100, max_queue_depth=20,
    )
    before_start = [
        (row["status"], row["attempts"], row["window"])
        for row in window_store.rows.values()
    ]
    window_worker.start()
    three_started = first_wave_started.wait(1.5)
    while three_started and window_store.claims < 3:
        time.sleep(0.005)
    # Snapshot values before releasing the first wave; the fake store mutates
    # its row dicts in place as later workers drain.
    during = [dict(row) for row in window_store.rows.values()]
    processing = [row for row in during if row["status"] == "processing"]
    still_queued = [row for row in during if row["status"] == "queued"]
    release_first_wave.set()
    window_drained = window_worker.wait_idle(4.0)
    checks.append(check(
        "twenty slow recovery previews start no window while queued; only three dequeued workers claim",
        window_admission == {
            "submitted": 20, "skipped_full": 0,
            "skipped_claim": 0, "skipped_backlog": 0,
        }
        and all(state == "queued" and attempts == 0 and window is None
                for state, attempts, window in before_start)
        and three_started
        and len(processing) == 3
        and all(row["attempts"] == 1 and row["window"] is not None
                for row in processing)
        and len(still_queued) == 17
        and all(row["attempts"] == 0 and row["window"] is None
                for row in still_queued)
        and window_drained
        and all(row["status"] == "done" for row in window_store.rows.values()),
    ))

    wrapped_store = FakeStore()
    wrapped_store.status = "processing"
    wrapped_store.lease_generation = 1
    seen = {}

    def processor(event_type, payload, db, gh, coalesce=None):
        seen["event_type"] = event_type
        seen["payload"] = payload
        return {"ok": True}

    owner = DQ.DeliveryStore("postgresql://unused")
    owner.claim = wrapped_store.claim
    owner.finish = wrapped_store.finish
    owner.release = wrapped_store.release
    wrapped = owner.wrap_processor(processor)
    payload = DQ.with_delivery_key(
        wrapped_store.payload, "delivery-1", preclaimed=True, lease_generation=1,
    )
    result = wrapped("pull_request", payload, None, None)
    checks.append(check(
        "preclaimed recovery payload skips the second claim but still finishes after processing",
        result == {"ok": True}
        and wrapped_store.claims == 0
        and wrapped_store.finishes == 1
        and wrapped_store.status == "done"
        and seen["event_type"] == "pull_request"
        and seen["payload"].get("_veripsa_delivery_key") == "delivery-1"
        and "_veripsa_delivery_preclaimed" not in seen["payload"]
        and "_veripsa_delivery_lease_generation" not in seen["payload"],
    ))

    # Account-lifecycle contention is attempt-neutral only when SQL positively marks that exact advisory-lock
    # boundary. A generic 55P03/body lock, a statement timeout, or non-durable work must stay fail-loud.
    class MarkedLifecycleLock(psycopg2.errors.LockNotAvailable):
        @property
        def pgcode(self):
            return "55P03"

        @property
        def diag(self):
            return SimpleNamespace(
                constraint_name=EP._ACCOUNT_LIFECYCLE_LOCK_CONSTRAINT)

    class UnmarkedLock(MarkedLifecycleLock):
        @property
        def diag(self):
            return SimpleNamespace(constraint_name="some_other_lock")

    class RaisingCursor:
        def __init__(self, error):
            self.error = error

        def execute(self, *_args, **_kwargs):
            raise self.error

    marker_store = FakeStore()
    marker_store.status = "processing"
    marker_store.lease_generation = 1
    marker_owner = DQ.DeliveryStore("postgresql://unused")
    marker_owner.claim = marker_store.claim
    marker_owner.finish = marker_store.finish
    marker_owner.release = marker_store.release
    marker_owner.defer = marker_store.defer

    def marked_processor(_event_type, body, *_args, **_kwargs):
        EP._installation_admission(
            RaisingCursor(MarkedLifecycleLock("private DB detail must not persist")),
            "A-42", None, delivery_key=body.get("_veripsa_delivery_key"),
        )

    marker_payload = DQ.with_delivery_key(
        marker_store.payload, "delivery-1", preclaimed=True, lease_generation=1)
    marker_result = marker_owner.wrap_processor(marked_processor)(
        "check_suite", marker_payload, None, None)
    checks.append(check(
        "marked account-lifecycle contention defers durably without finish, release, or attempt failure",
        marker_result.get(DQ._WORKER_CLAIM_OUTCOME) == "deferred"
        and marker_store.status == "queued"
        and marker_store.defers == 1
        and marker_store.deferred_leases == [1]
        and marker_store.releases == 0
        and marker_store.finishes == 0
        and marker_store.defer_reasons == ["account lifecycle convergence is busy"]
        and "_veripsa_delivery_preclaimed" not in marker_payload
        and "_veripsa_delivery_lease_generation" not in marker_payload,
    ))

    unmarked_store = FakeStore()
    unmarked_store.status = "processing"
    unmarked_store.lease_generation = 1
    unmarked_owner = DQ.DeliveryStore("postgresql://unused")
    unmarked_owner.claim = unmarked_store.claim
    unmarked_owner.finish = unmarked_store.finish
    unmarked_owner.release = unmarked_store.release
    unmarked_owner.defer = unmarked_store.defer

    def unmarked_processor(_event_type, body, *_args, **_kwargs):
        EP._installation_admission(
            RaisingCursor(UnmarkedLock("canceling statement due to lock timeout")),
            "A-42", None, delivery_key=body.get("_veripsa_delivery_key"),
        )

    unmarked_payload = DQ.with_delivery_key(
        unmarked_store.payload, "delivery-1", preclaimed=True, lease_generation=1)
    unmarked_raised = False
    try:
        unmarked_owner.wrap_processor(unmarked_processor)(
            "check_suite", unmarked_payload, None, None)
    except psycopg2.errors.LockNotAvailable:
        unmarked_raised = True
    checks.append(check(
        "unmarked 55P03 remains an ordinary failure even when its text resembles lock contention",
        unmarked_raised and unmarked_store.releases == 1 and unmarked_store.defers == 0,
    ))

    nondurable_raised = False
    statement_timeout_raised = False
    try:
        EP._installation_admission(
            RaisingCursor(MarkedLifecycleLock("marked but non-durable")), "A-42", None)
    except psycopg2.errors.LockNotAvailable:
        nondurable_raised = True
    try:
        EP._installation_admission(
            RaisingCursor(psycopg2.errors.QueryCanceled("statement timeout")),
            "A-42", None, delivery_key="durable-key")
    except psycopg2.errors.QueryCanceled:
        statement_timeout_raised = True
    checks.append(check(
        "non-durable marked contention and SQLSTATE 57014 are never hidden as scheduled deferrals",
        nondurable_raised and statement_timeout_raised,
    ))

    saved_id_lock = EP._take_repository_id_lock
    saved_repo_lock = EP._take_repo_lock
    repo_durable_deferred = False
    repo_nondurable_raised = False
    try:
        EP._take_repository_id_lock = lambda *_args, **_kwargs: None

        def busy_repo(*_args, **_kwargs):
            raise UnmarkedLock("explicit repository advisory lock busy")

        EP._take_repo_lock = busy_repo
        try:
            EP._take_live_repository_locks(
                object(), "123", "42", "acme/app", take_coordinate=True,
                delivery_key="durable-key")
        except DQ.IntentionalDeliveryDeferral:
            repo_durable_deferred = True
        try:
            EP._take_live_repository_locks(
                object(), "123", "42", "acme/app", take_coordinate=True,
                delivery_key=None)
        except psycopg2.errors.LockNotAvailable:
            repo_nondurable_raised = True
    finally:
        EP._take_repository_id_lock = saved_id_lock
        EP._take_repo_lock = saved_repo_lock
    checks.append(check(
        "only the explicit durable repository-lock wait becomes attempt-neutral",
        repo_durable_deferred and repo_nondurable_raised,
    ))

    retry_store = FakeStore()
    retry_store.status = "processing"
    retry_store.lease_generation = 1
    runs = {"count": 0}

    def flaky_processor(event_type, payload, db, gh, coalesce=None):
        runs["count"] += 1
        if runs["count"] == 1:
            raise RuntimeError("transient processor failure")
        return {"ok": True}

    retry_owner = DQ.DeliveryStore("postgresql://unused")
    retry_owner.claim = retry_store.claim
    retry_owner.finish = retry_store.finish
    retry_owner.release = retry_store.release
    retry_wrapped = retry_owner.wrap_processor(flaky_processor)
    retry_payload = DQ.with_delivery_key(
        retry_store.payload, "delivery-1", preclaimed=True, lease_generation=1,
    )
    first_error = False
    try:
        retry_wrapped("pull_request", retry_payload, None, None)
    except RuntimeError:
        first_error = True
    retry_result = retry_wrapped("pull_request", retry_payload, None, None)
    checks.append(check(
        "preclaimed marker is cleared after release, so an in-memory retry re-claims the durable row",
        first_error
        and retry_result == {"ok": True}
        and retry_store.releases == 1
        and retry_store.released_leases == [1]
        and retry_store.claims == 1
        and retry_store.finishes == 1
        and retry_store.finished_leases == [2]
        and retry_store.status == "done"
        and "_veripsa_delivery_preclaimed" not in retry_payload
        and "_veripsa_delivery_lease_generation" not in retry_payload,
    ))

    # release runs on a separate connection. If it raises before commit, the old processing lease may still be
    # present; the in-memory retry must classify that durable owner and must not execute the handler again.
    release_error_store = FakeStore()
    release_error_store.status = "processing"
    release_error_store.lease_generation = 1
    release_error_runs = {"count": 0}

    def always_fails(*_args, **_kwargs):
        release_error_runs["count"] += 1
        raise RuntimeError("processor rolled back")

    release_error_owner = DQ.DeliveryStore("postgresql://unused")
    release_error_owner.claim = release_error_store.claim
    release_error_owner.finish = release_error_store.finish

    def release_before_commit_raises(_key, _error, _lease):
        raise RuntimeError("release failed before commit")

    release_error_owner.release = release_before_commit_raises
    release_error_wrapped = release_error_owner.wrap_processor(always_fails)
    release_error_payload = DQ.with_delivery_key(
        release_error_store.payload, "delivery-1", preclaimed=True, lease_generation=1,
    )
    release_raised = False
    try:
        release_error_wrapped("pull_request", release_error_payload, None, None)
    except RuntimeError:
        release_raised = True
    release_retry = release_error_wrapped("pull_request", release_error_payload, None, None)
    checks.append(check(
        "a release exception clears the stale preclaim; retry classifies the still-owned row without rerunning work",
        release_raised
        and release_error_runs["count"] == 1
        and release_error_store.claims == 1
        and release_retry.get("_veripsa_worker_claim_outcome") == "duplicate"
        and "_veripsa_delivery_preclaimed" not in release_error_payload
        and "_veripsa_delivery_lease_generation" not in release_error_payload,
    ))

    # The harder ambiguity is commit-then-lost-ACK: release changed the row to queued but raised to the caller.
    # Clearing first makes retry claim generation 2; retaining generation 1 would run unowned and fail to finish.
    ack_store = FakeStore()
    ack_store.status = "processing"
    ack_store.lease_generation = 1
    ack_runs = {"count": 0}

    def ack_flaky(*_args, **_kwargs):
        ack_runs["count"] += 1
        if ack_runs["count"] == 1:
            raise RuntimeError("processor rolled back")
        return {"ok": True}

    ack_owner = DQ.DeliveryStore("postgresql://unused")
    ack_owner.claim = ack_store.claim
    ack_owner.finish = ack_store.finish

    def release_commits_then_raises(key, error, lease_generation):
        result = ack_store.release(key, error, lease_generation)
        assert result == "queued"
        raise RuntimeError("release commit ACK lost")

    ack_owner.release = release_commits_then_raises
    ack_wrapped = ack_owner.wrap_processor(ack_flaky)
    ack_payload = DQ.with_delivery_key(
        ack_store.payload, "delivery-1", preclaimed=True, lease_generation=1,
    )
    ack_raised = False
    try:
        ack_wrapped("pull_request", ack_payload, None, None)
    except RuntimeError:
        ack_raised = True
    ack_result = ack_wrapped("pull_request", ack_payload, None, None)
    checks.append(check(
        "release commit-ACK ambiguity retries through a fresh generation and exact-lease finish",
        ack_raised
        and ack_result == {"ok": True}
        and ack_runs["count"] == 2
        and ack_store.claims == 1
        and ack_store.released_leases == [1]
        and ack_store.finished_leases == [2]
        and ack_store.status == "done",
    ))

    def claim_result_processor(claim_result):
        claim_owner = DQ.DeliveryStore("postgresql://unused")
        claim_owner.claim = lambda _key: dict(claim_result)
        claim_owner.finish = lambda _key, _lease: (_ for _ in ()).throw(
            AssertionError("an unclaimed delivery must never finish"))
        claim_owner.release = lambda _key, _error, _lease: (_ for _ in ()).throw(
            AssertionError("a classified false claim must not release"))
        return claim_owner.wrap_processor(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("an unclaimed delivery must never run its handler")))

    deferred_worker = EventQueue(
        None, None,
        claim_result_processor({"claimed": False, "reason": "blocked_by_earlier", "status": "queued"}),
        retry_attempts=1,
    ).start()
    deferred_worker._record_delivery_outcome("claim-deferred", "failed")
    deferred_payload = DQ.with_delivery_key(FakeStore().payload, "claim-deferred")
    deferred_worker.submit("installation", deferred_payload, "claim-deferred")
    deferred_drained = deferred_worker.wait_idle(2.0)
    checks.append(check(
        "a newer same-account live generation deferred by durable order is not reported as processed or failed",
        deferred_drained
        and deferred_worker.processed() == 0
        and deferred_worker.failed() == 0
        and deferred_worker.claim_deferred() == 1
        and deferred_worker.claim_duplicates() == 0
        and deferred_worker.delivery_status("claim-deferred") == "pending"
        and deferred_worker.delivery_outcome("claim-deferred") is None,
    ))

    duplicate_worker = EventQueue(
        None, None,
        claim_result_processor({"claimed": False, "reason": "already_owned", "status": "processing"}),
        retry_attempts=1,
    ).start()
    duplicate_worker._record_delivery_outcome("claim-duplicate", "failed")
    duplicate_payload = DQ.with_delivery_key(FakeStore().payload, "claim-duplicate")
    duplicate_worker.submit("pull_request", duplicate_payload, "claim-duplicate")
    duplicate_drained = duplicate_worker.wait_idle(2.0)
    checks.append(check(
        "a legitimate cross-worker duplicate is counted separately from handler success and durability loss",
        duplicate_drained
        and duplicate_worker.processed() == 0
        and duplicate_worker.failed() == 0
        and duplicate_worker.claim_duplicates() == 1
        and duplicate_worker.claim_missing() == 0
        and duplicate_worker.delivery_status("claim-duplicate") == "pending"
        and duplicate_worker.delivery_outcome("claim-duplicate") is None,
    ))

    missing_worker = EventQueue(
        None, None,
        claim_result_processor({"claimed": False, "reason": "missing", "status": "missing"}),
        retry_attempts=3,
    ).start()
    missing_payload = DQ.with_delivery_key(FakeStore().payload, "claim-missing")
    missing_worker.submit("pull_request", missing_payload, "claim-missing")
    missing_drained = missing_worker.wait_idle(2.0)
    missing_health = HW.health_snapshot(missing_worker)
    checks.append(check(
        "a missing durable row fails loud once instead of becoming a green processed skip or pointless retry loop",
        missing_drained
        and missing_worker.processed() == 0
        and missing_worker.failed() == 1
        and missing_worker.retried() == 0
        and missing_worker.claim_missing() == 1
        and missing_worker.delivery_outcome("claim-missing") == "failed"
        and missing_health.get("claim_missing") == 1
        and missing_health.get("claim_deferred") == 0
        and missing_health.get("claim_duplicates") == 0,
    ))

    def account_of(payload):
        return str(payload.get("installation", {}).get("account", {}).get("id", ""))

    def event(account, number):
        return (
            "pull_request",
            {
                "number": number,
                "installation": {"account": {"id": account}},
                "repository": {"full_name": f"acct-{account}/repo", "owner": {"id": account}},
            },
            f"delivery-{account}-{number}",
        )

    priority = _FairQueue(maxsize=20, per_account_cap=20, account_of=account_of)
    priority.put_nowait(event(42, 90), recovered=True)
    priority.put_nowait(event(99, 90), recovered=True)
    priority.put_nowait(event(77, 1))
    priority.put_nowait(event(77, 2))
    priority.put_nowait(event(88, 1))
    priority_order = [
        (item[1]["installation"]["account"]["id"], item[1]["number"])
        for item in (priority.get() for _ in range(5))
    ]
    checks.append(check(
        "live-headed accounts drain before other accounts' recovery with live FIFO and round-robin fairness",
        priority_order == [(77, 1), (88, 1), (77, 2), (42, 90), (99, 90)],
    ))

    causal = _FairQueue(maxsize=10, per_account_cap=10, account_of=account_of)
    causal.put_nowait(event(42, 90), recovered=True)
    causal.put_nowait(event(42, 1))
    causal_order = [causal.get()[1]["number"], causal.get()[1]["number"]]
    checks.append(check(
        "one account keeps a single recovery/live FIFO so lifecycle and PR state cannot replay backward",
        causal_order == [90, 1],
    ))

    def state_event(event_type, action, delivery):
        return (
            event_type,
            {
                "action": action,
                "installation": {"account": {"id": 42}},
                "repository": {"full_name": "acct-42/repo", "owner": {"id": 42}},
            },
            delivery,
        )

    lifecycle = _FairQueue(maxsize=10, per_account_cap=10, account_of=account_of)
    lifecycle.put_nowait(state_event("installation", "deleted", "install-deleted"), recovered=True)
    lifecycle.put_nowait(state_event("installation", "created", "install-created"))
    lifecycle_order = [lifecycle.get()[1]["action"], lifecycle.get()[1]["action"]]
    pr_state = _FairQueue(maxsize=10, per_account_cap=10, account_of=account_of)
    pr_state.put_nowait(state_event("pull_request", "closed", "pr-closed"), recovered=True)
    pr_state.put_nowait(state_event("pull_request", "reopened", "pr-reopened"))
    pr_order = [pr_state.get()[1]["action"], pr_state.get()[1]["action"]]
    checks.append(check(
        "recovered delete/close cannot run after newer create/reopen for the same account",
        lifecycle_order == ["deleted", "created"] and pr_order == ["closed", "reopened"],
    ))

    def push_event(account, sha, delivery):
        return (
            "push",
            {
                "installation": {"account": {"id": account}},
                "repository": {"full_name": f"acct-{account}/repo", "owner": {"id": account}},
                "ref": "refs/heads/main",
                "after": sha,
            },
            delivery,
        )

    push_priority = _FairQueue(maxsize=10, per_account_cap=10, account_of=account_of)
    push_priority.put_nowait(push_event(42, "a" * 40, "old-recovered"), recovered=True)
    push_priority.put_nowait(push_event(42, "b" * 40, "new-live"))
    push_order = [push_priority.get()[2], push_priority.get()[2]]
    checks.append(check(
        "same-account recovered OLD push stays before live NEW push; graph monotonicity remains the backstop",
        push_order == ["old-recovered", "new-live"],
    ))

    progress = _FairQueue(
        maxsize=10, per_account_cap=10, account_of=account_of, live_burst_limit=2,
    )
    progress.put_nowait(event(42, 90), recovered=True)
    progress.put_nowait(event(77, 1))
    progress.put_nowait(event(77, 2))
    progress.put_nowait(event(77, 3))
    progress_order = [
        (item[1]["installation"]["account"]["id"], item[1]["number"])
        for item in (progress.get() for _ in range(4))
    ]
    checks.append(check(
        "continuous live traffic gives queued recovery a bounded turn instead of starving its durable lease",
        progress_order == [(77, 1), (77, 2), (42, 90), (77, 3)],
    ))

    bounded = _FairQueue(maxsize=20, per_account_cap=2, account_of=account_of)
    bounded.put_nowait(event(42, 90), recovered=True)
    bounded.put_nowait(event(42, 1))
    cap_held = False
    try:
        bounded.put_nowait(event(42, 2))
    except queue.Full:
        cap_held = True
    checks.append(check(
        "live and recovery tiers share one per-account capacity bound",
        cap_held and bounded.qsize() == 2,
    ))

    started = threading.Event()
    release = threading.Event()
    seen_order = []

    def priority_processor(event_type, payload, db, gh, coalesce=None):
        seen_order.append(payload["number"])
        if payload["number"] == 90 and payload["installation"]["account"]["id"] == 42:
            started.set()
            release.wait(2.0)

    priority_worker = EventQueue(
        None, None, priority_processor, account_of=account_of, retry_attempts=1,
    ).start()
    first_recovery = event(42, 90)
    second_recovery = event(99, 91)
    live = event(77, 1)
    priority_worker.submit(*first_recovery, recovered=True)
    first_started = started.wait(1.0)
    priority_worker.submit(*second_recovery, recovered=True)
    priority_worker.submit(*live)
    live_waited_for_current = seen_order == [90]
    release.set()
    drained = priority_worker.wait_idle(3.0)
    checks.append(check(
        "an in-flight recovery finishes safely, then queued live work runs before the next recovery account",
        first_started and live_waited_for_current and drained and seen_order == [90, 1, 91],
    ))

    ok = sum(1 for p in checks if p)
    print(f"\n-- {ok}/{len(checks)} delivery recovery backpressure checks passed --")
    if ok != len(checks):
        print("DELIVERY RECOVERY BACKPRESSURE: FAIL")
        return 1
    print("DELIVERY RECOVERY BACKPRESSURE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
