#!/usr/bin/env python3
"""cochange — the co-change (logical-coupling) SECOND signal: store + populate + the per-PR partner read.

Lifted OUT of ingest.py (a 1000-line god-file) as a cohesive leaf: co-change is a self-contained, ADVISORY
detector — its own dedicated thread pool, its own blobless-clone feeder, its own gated SQL — that the structural
graph ingest does not depend on. PRs #235/#236/#237/#238/#240 ALL fought over this block inside ingest.py, so by
Veripsa's own size×churn signal it is exactly the slice to split off (dogfooded). Behavior-preserving PURE MOVE:
no logic, signatures, return shapes, fail-open semantics, or content-free guarantees changed — only the home of
the symbols. ingest.py re-imports them all by their original names, so every caller + test is unchanged.

The cluster: ingest_cochange (gated store) · _cochange_pairs_via_clone (the slow blobless clone + extractor,
holding no lock) · populate_cochange (sync seed) · the _CochangeScheduler (the per-tenant-FAIR single-worker pool)
+ _cochange_populate_task + populate_cochange_async (OFF-worker async seed — clone holds nothing, then a brief
tenant-pinned/locked store) ·
cochange_partners_for (the per-PR read). _server() is the SAME call-time seam ingest.py uses to reach server.py's
lock/scoped-db helpers without a load-time cycle (copied here, not imported, so cochange.py is a pure leaf)."""
from __future__ import annotations

import collections
import json
import os
import tempfile
import threading

# env_int: the SAME validated env-knob reader event_queue uses for VERIPSA_PER_ACCOUNT_QUEUE_CAP. A 0/negative
# co-change cap is the same silent-misconfig class (a per-account bucket "full" at length 0 → every submit dropped
# → co-change silently never populates while /healthz stays green). Reading it through env_int refuses a <1 / non-int
# cap LOUDLY at start instead of running silently dead — identical posture to the main worker.
try:
    from env_config import env_int  # noqa: E402
except ImportError:  # imported as a package
    from .env_config import env_int  # noqa: E402

# _FairQueue: the PROVEN per-account round-robin + per-account cap + global bound the MAIN event worker already
# uses (event_queue.py). The co-change pool gets the SAME mechanism by REUSING it as the backlog store (see
# _CochangeScheduler) — no second, subtly-different fairness implementation to drift. Account-keyed already: it
# calls account_of(item[1]), so we shape each queued item so item[1] IS the pre-resolved account key.
try:
    from event_queue import _FairQueue  # noqa: E402
except ImportError:  # imported as a package
    from .event_queue import _FairQueue  # noqa: E402

# FAIRNESS GUARD for the co-change pool — the per-tenant slice cap, the EXACT counterpart of event_queue's
# _PER_ACCOUNT_QUEUE_CAP. The dedicated co-change pool is a SINGLE worker draining clones (serialized on purpose),
# so without a per-account bound one noisy tenant onboarding a big org (one populate per repo) or force-pushing a
# busy monorepo (one increment per push) piles slow blobless-clone tasks into the FIFO ahead of every other tenant
# → a 2nd customer's co-change (the advisory 2nd signal) goes stale until the noisy tenant's clones finish. Cap how
# many co-change tasks ONE account may hold queued at once; past it that account's further submits are SKIPPED
# content-free + fail-open (co-change is advisory — a dropped populate self-heals on the next push), while every
# OTHER tenant's room is preserved. Smaller than the event cap by default: co-change tasks are heavy (a clone
# each), serialized one-at-a-time, and advisory — a tenant rarely needs 50 clones queued to stay fresh. min_value=1
# via env_int so a bad knob fails LOUD at startup (same as the event worker), never a silently-disabled path.
_PER_ACCOUNT_COCHANGE_CAP = env_int("VERIPSA_PER_ACCOUNT_COCHANGE_CAP", 50, min_value=1)

# Round-2 concurrency follow-up — MEMORY GUARD layered on top of the per-account fairness cap above. The
# per-account cap protects against a tenant flooding ACROSS repos (one onboard per repo on a big org); this knob
# adds the orthogonal protection against a tenant flooding ONE repo (rapid-fire pushes to the same monorepo, or a
# webhook redelivery storm targeting one repo). Without it the single-worker pool can queue many in-flight + queued
# clones of the SAME (account, repo) until the per-account cap is hit, each clone allocating its own tempdir on the
# small free-tier instance memory. Cap the count of in-flight + queued tasks per (account, repo): past it a new
# submit is DROPPED content-free + fail-open (co-change is advisory — a dropped populate/increment self-heals on
# the next push), exactly like the per-account cap. Default 3 (a small handful is plenty for the advisory signal:
# clones are serialized one-at-a-time so even bursts settle quickly). KILL SWITCH: VERIPSA_COCHANGE_PER_TENANT_CAP=0
# disables the per-(account,repo) cap (only the per-account cap remains). min_value=0 via env_int so a 0 still fails
# loud on misconfig of OTHER kinds (non-int, negative) — never a silently-broken cap, but 0 is intentional disable.
_COCHANGE_PER_TENANT_CAP = env_int("VERIPSA_COCHANGE_PER_TENANT_CAP", 3, min_value=0)


def _server():
    """The server module, resolved at CALL time (NOT at load — server.py imports the ingest cluster, so a
    load-time `import server` here would be a circular import). server.py is fully loaded by the time any of these
    functions runs. Used only to reach the server-side SEAMS (_take_repo_lock / _scoped_db) — not part of the
    co-change concern, and not monkeypatched, so a fresh call-time lookup is always correct. (Same idiom as
    ingest._server().)"""
    try:
        import server as _s  # call-time import → no module-load circularity
    except ImportError:  # imported as a package
        from . import server as _s  # type: ignore
    return _s


def ingest_cochange(db, repo: str, pairs) -> dict:
    """Store the content-free co-change pairs for a repo (DELETE+reINSERT). `pairs` come from
    _cg_cochange.cochange_pairs — paths + counts only. Never raises: co-change is an ADVISORY second signal; on
    any error the structural graph still works, so a failure is reported content-free, never fatal to ingest."""
    try:
        res = db("SELECT core.ingest_cochange_with_authority(%s,%s)", (json.dumps(pairs or []), repo))
        if isinstance(res, str):
            res = json.loads(res)
        return res or {"ok": False}
    except Exception as e:
        return {"ok": False, "cochange_error": str(e)[:200]}


def _cochange_pairs_via_clone(gh, repo: str, branch: str, window: int = 800):
    """The SLOW half of co-change populate — a BLOBLESS, NO-CHECKOUT clone (github_rest.history_clone: git
    metadata + paths only, never a file body) + the precision-disciplined extractor. Holds NO DB connection and
    NO lock (that is the whole point — see populate_cochange_async): a slow clone can never stall the event
    worker or hold a repo lock. Returns the content-free pairs (paths + counts), or None on any clone/extract
    trouble (advisory signal → never raises)."""
    import _cg_cochange as CC
    try:
        with tempfile.TemporaryDirectory() as d:
            clone_dir = os.path.join(d, "h")
            gh.history_clone(repo, branch, clone_dir)
            # STORE at the PERSIST floor (raw repeated co-changes, no prob/lift pruning) so the per-push increment
            # can ACCUMULATE onto it; the customer-facing precision floor (co≥5, lift≥2) is applied at READ
            # (co_change_partners_with_authority), not here. PR #314 proper fix — see _cg_cochange floor comment.
            return CC.cochange_pairs(clone_dir, window=window, min_support=CC.PERSIST_MIN_SUPPORT,
                                     min_prob=0.0, min_lift=0.0)    # extract BEFORE the temp dir is torn down
    except Exception as e:
        print(f"co-change clone/extract repo={repo}@{branch} skipped (advisory 2nd signal): {str(e)[:160]}", flush=True)
        return None


def populate_cochange(db, gh, repo: str, branch: str, window: int = 800) -> dict:
    """SEED the content-free co-change signal for a repo from its COMMIT HISTORY, SYNCHRONOUSLY over the given
    `db` (the clone + extract + gated store on one connection). The graph is built from a tarball — a SNAPSHOT
    with no history — so "files that change together" (the coupling no call/import/schema edge can show) needs a
    real clone. Content-free end to end: only paths + counts leave the clone or reach the table.

    NOTE: the LIVE onboarding path does NOT call this — it uses populate_cochange_async so a slow clone never
    sits on the event worker (see backfill_repo STEP 1b). This sync form is the direct unit (gated by
    tests/test_cochange_populate.py) and the backfill-CLI / fake-db fallback. FAIL-OPEN: any trouble is reported
    content-free, never raised."""
    pairs = _cochange_pairs_via_clone(gh, repo, branch, window)
    if pairs is None:
        return {"ok": False, "cochange_error": "clone/extract failed (advisory signal skipped)"}
    res = ingest_cochange(db, repo, pairs)
    res = res if isinstance(res, dict) else {"ok": True}
    res["pairs_found"] = len(pairs)
    return res


# A DEDICATED single-worker scheduler for co-change population — OFF the event worker. co-change is ADVISORY, and
# its feeder is a git clone (I/O-bound, up to history_clone's timeout). Running it inline on the single webhook
# worker would stall EVERY queued PR/push behind a slow clone AND hold the per-repo advisory lock for the clone's
# whole duration (a same-repo event would lock-timeout → fail → redeliver). So we hand population to this scheduler
# instead: the live path returns immediately.
#
# FAIR DRAIN (multi-tenant) — the SAME model the main event worker has. A single worker drains the backlog, so the
# ORDER it drains in is the whole fairness story on a multi-tenant box. A strict global FIFO (the prior plain
# ThreadPoolExecutor queue) let ONE tenant onboarding a big org / force-pushing a busy monorepo pile slow clones at
# the head of the queue → every OTHER tenant's co-change went stale behind that burst. So the backlog is a
# _FairQueue (the proven event-worker mechanism, REUSED): per-account FIFO sub-queues drained ROUND-ROBIN + a
# per-account cap (_PER_ACCOUNT_COCHANGE_CAP). A flooding tenant gets exactly one turn per rotation and can hold at
# most its slice — it can neither starve nor crowd out the others. STILL ONE worker (clones stay serialized on
# purpose: no thread storm on a big-org onboard, at most ONE extra DB connection at a time — the managed Postgres
# connection budget is safe); only the BACKLOG order changes from FIFO to fair round-robin.
#
# Lazy daemon worker (created on first submit; daemon → never blocks graceful shutdown). Created once, module-level.
_COCHANGE_SCHED = None
_COCHANGE_SCHED_LOCK = threading.Lock()

# A marker object index [1] uses for tasks with NO resolvable account (degraded gh / resolution error): they share
# one fair bucket so even keyless tasks still get a round-robin turn and can never monopolize — the SAME best-effort
# '' bucket _FairQueue falls back to for a keyless event. A distinct sentinel (not "") keeps it readable in logs.
_NO_ACCT = "cochange:no-account"


class _CochangeScheduler:
    """The co-change pool with the main event worker's per-tenant FAIRNESS. ONE daemon worker drains a _FairQueue
    backlog (per-account round-robin + per-account cap + global bound — the exact proven mechanism, reused) and runs
    each task, fulfilling its Future so callers keep the `.result()` contract the ThreadPoolExecutor gave them.

    WHY a scheduler around _FairQueue rather than handing _FairQueue straight to the pool: a ThreadPoolExecutor owns
    its OWN (strict-FIFO, unbounded) work queue and gives no seam to reorder admission per-tenant or to cap a
    tenant's slice. So we keep the single-worker serialization but put OUR _FairQueue in front as the backlog and
    drain it ourselves — same spirit as EventQueue wrapping _FairQueue with its single daemon worker.

    submit(account_key, fn, *args) → a Future:
      * under the per-account cap → enqueued FAIR; the drainer runs fn(*args) in round-robin order and sets the
        Future's result/exception. KEEP serialization: exactly one task runs at a time (max one extra DB conn).
      * OVER the per-account cap (or the global bound) → the task is DROPPED content-free + FAIL-OPEN: nothing is
        enqueued, ONE content-free line is logged, and a PRE-RESOLVED Future carrying a skipped marker is returned
        (NEVER raises — co-change is advisory; a dropped populate self-heals on the next push). The return shape is
        the same Future-with-a-dict every caller already handles.

    _FairQueue is account-keyed via account_of(item[1]); we shape each item as (future, account_key, fn, args) so
    item[1] is the pre-resolved account key, and inject account_of=lambda key: key (identity)."""

    def __init__(self, per_account_cap: int, maxsize: int = 10000, per_tenant_cap: int = 0):
        # maxsize is the global backlog bound (a hard ceiling so a pathological multi-tenant flood can't grow the
        # backlog without limit); generous vs the per-account cap since the per-account cap is the real fairness
        # lever. _FairQueue raises queue.Full at EITHER bound — submit() turns that into a content-free skip.
        # per_tenant_cap (0 = disabled) is the orthogonal MEMORY guard: bound in-flight + queued tasks per
        # (account, repo) so a single tenant flooding ONE repo (push storm / webhook redelivery) cannot allocate
        # many concurrent clone tempdirs on the small free-tier memory before the per-account cap kicks in.
        from concurrent.futures import Future  # local import: keep module load cheap (pool is lazy)
        self._Future = Future
        self._q = _FairQueue(maxsize=maxsize, per_account_cap=per_account_cap, account_of=lambda key: key)
        self._per_tenant_cap = max(0, int(per_tenant_cap))
        # (account_key, repo) → count of tasks queued OR currently running. Guarded by _tenant_lock so the
        # check + increment in submit and the decrement in _run cannot race. Kept tiny: keys are pruned when
        # their count hits 0 so an idle deploy holds nothing.
        self._tenant_counts: dict = {}
        self._tenant_lock = threading.Lock()
        self._worker = None

    def _ensure_worker(self):
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="veripsa-cochange", daemon=True)
            self._worker.start()

    def _try_reserve_tenant_slot(self, key, repo):
        """Atomic check + increment of the per-(account, repo) in-flight + queued counter. Returns True if the slot
        was reserved (caller MUST eventually call _release_tenant_slot), False if this (account, repo) is already
        at the per-tenant cap. A cap of 0 disables the gate entirely (always returns True, never bookkeeps) so the
        kill switch path is zero-overhead and the existing per-account fairness is the sole gate."""
        if self._per_tenant_cap <= 0 or repo is None:
            return True
        tkey = (key, repo)
        with self._tenant_lock:
            cur = self._tenant_counts.get(tkey, 0)
            if cur >= self._per_tenant_cap:
                return False
            self._tenant_counts[tkey] = cur + 1
            return True

    def _release_tenant_slot(self, key, repo):
        """Decrement the per-(account, repo) counter. Called from the worker AFTER fn(*args) finishes (success or
        exception) so the slot covers BOTH queued and in-flight time. Prunes the key when it hits 0 so an idle
        deploy holds nothing. Never raises (a missing key — e.g. cap=0 path or repo=None — is a silent no-op)."""
        if self._per_tenant_cap <= 0 or repo is None:
            return
        tkey = (key, repo)
        with self._tenant_lock:
            cur = self._tenant_counts.get(tkey, 0)
            if cur <= 1:
                self._tenant_counts.pop(tkey, None)
            else:
                self._tenant_counts[tkey] = cur - 1

    def submit(self, account_key, fn, *args, repo=None):
        """Enqueue fn(*args) FAIR-ly and return a Future, or — over this account's cap / this (account, repo)'s
        per-tenant cap / the global bound — drop it content-free + fail-open and return a pre-resolved skipped
        Future. Never raises.

        `repo` (kwarg, default None) is the per-(account, repo) key for the memory-guard cap (Round-2 follow-up):
        pass the repo full_name so a noisy tenant flooding ONE repo cannot pile many concurrent clone tempdirs on
        the small free-tier instance memory. When omitted (or when VERIPSA_COCHANGE_PER_TENANT_CAP=0) the
        per-(account, repo) gate is bypassed and only the per-account fairness cap applies — keeps the existing
        fairness-only call shape working unchanged."""
        import queue as _queue
        key = account_key or _NO_ACCT
        fut = self._Future()
        # GATE 1 (memory guard): per-(account, repo) cap. Check + reserve BEFORE the _FairQueue put so an over-cap
        # task never consumes a per-account fairness slot for an account that already has its cap of THIS repo
        # in flight. A reservation must be released on every exit path that does NOT enqueue (the per-account-cap
        # drop below) AND on task completion in _run.
        if not self._try_reserve_tenant_slot(key, repo):
            print(f"co-change task skipped (account={key} repo={repo} over per-(account,repo) cap "
                  f"{self._per_tenant_cap}; advisory 2nd signal, self-heals on next push)", flush=True)
            fut.set_result({"ok": False, "skipped": "over per-tenant co-change cap"})
            return fut
        self._ensure_worker()                       # start the drainer before the first item is visible
        try:
            # Carry `repo` in the queued tuple so the worker can release the per-(account, repo) slot on completion
            # (whether or not the worker even knows the cap is active — release is a no-op when repo is None / cap=0).
            self._q.put_nowait((fut, key, fn, args, repo))
        except _queue.Full:
            # OVER the per-account cap (the noisy-neighbor guard) or the global bound. Content-free + fail-open:
            # co-change is advisory; this account's further submits are dropped (a dropped populate self-heals on
            # the next push). Log ONE content-free line (account key = public GitHub owner id; never code/bodies),
            # return a RESOLVED Future carrying a skipped marker so callers' .result()/bool() keep working.
            # Release the per-(account, repo) reservation we took above — the task never enters the backlog so it
            # must not consume a slot until the worker eventually frees it (the worker never sees it).
            self._release_tenant_slot(key, repo)
            print(f"co-change task skipped (account={key} over per-account fairness cap "
                  f"{self._q._per_account_cap}; advisory 2nd signal, self-heals on next push)", flush=True)
            fut.set_result({"ok": False, "skipped": "over per-account co-change fairness cap"})
            return fut
        return fut

    def _run(self):
        while True:
            item = self._q.get()
            # Tolerate the legacy 4-tuple shape (no repo) any in-test scheduler that bypasses submit might push —
            # the per-(account, repo) cap is then a no-op for that task, but the worker never crashes on the older
            # shape. Production code path always emits the 5-tuple.
            if len(item) == 5:
                fut, _key, fn, args, _repo = item
            else:
                fut, _key, fn, args = item
                _repo = None
            try:
                fut.set_result(fn(*args))
            except Exception as e:                  # one bad task must never kill the drainer (the whole pool)
                # Mirror the proven content-free, never-crash store: never let a task's exception escape the worker.
                # set_exception so a caller awaiting .result() still sees it; the result-shape callers (fire-and-
                # forget) are unaffected. Belt-and-braces: if the Future was already resolved, swallow.
                try:
                    fut.set_exception(e)
                except Exception:
                    pass
            finally:
                # Release the per-(account, repo) slot AFTER fn returns (success OR exception) so the slot covers
                # BOTH queued and in-flight time — that is what the memory guard is for (the clone is the cost).
                self._release_tenant_slot(_key, _repo)
                self._q.task_done()


def _cochange_scheduler():
    """The lazily-created module-level co-change scheduler (the fair single-worker pool). Double-checked under a
    lock so two concurrent first-submits (onboard + a push racing) don't build two schedulers / two workers."""
    global _COCHANGE_SCHED
    if _COCHANGE_SCHED is None:
        with _COCHANGE_SCHED_LOCK:
            if _COCHANGE_SCHED is None:
                _COCHANGE_SCHED = _CochangeScheduler(per_account_cap=_PER_ACCOUNT_COCHANGE_CAP,
                                                     per_tenant_cap=_COCHANGE_PER_TENANT_CAP)
    return _COCHANGE_SCHED


def _resolve_cochange_account(gh):
    """Best-effort owning-account key for fair BUCKETING at submit time — the SAME stable key the pool task itself
    derives (gh.installation_account_id()), resolved here only to choose the tenant's fair bucket. STRICTLY
    best-effort + fail-open: any trouble (a degraded gh, a resolution error) → None → the task shares the keyless
    fair bucket (still a round-robin turn, never a monopoly). This is ONLY the fairness key; the AUTHORITATIVE
    fail-closed account resolution + cross-tenant guard still happens INSIDE the pool task before any write, so a
    None here never causes a mis-routed or cross-tenant store — it only affects which fair bucket the task queues
    in. Never raises."""
    try:
        return gh.installation_account_id() or None
    except Exception:
        return None


def _repository_generation_allowed(db, repo: str, repository_id) -> bool:
    """Revalidate an async task against the stable repository object that scheduled it.

    The repo advisory lock serializes the eventual write with offboarding, but a queued task can acquire that lock
    after a delete/recreate boundary. The stable id keeps predecessor work from being stamped into the replacement
    generation. Callers without an id retain the DB writer's active-tombstone guard and cannot bypass a current
    removal; they simply cannot make the stronger cross-generation assertion.
    """
    if repository_id in (None, ""):
        return True
    allowed = db(
        "SELECT core.repository_event_allowed_with_authority(%s,%s)",
        (repo, str(repository_id)),
    )
    if isinstance(allowed, bool):
        return allowed
    if isinstance(allowed, str):
        return allowed.strip().lower() in ("t", "true", "1")
    return False


def _cochange_populate_task(
        gh, repo: str, branch: str, window: int = 800, repository_id=None) -> dict:
    """The off-worker co-change populate, end to end: (1) clone + extract holding NO connection/lock (the slow
    part); (2) a BRIEF tenant-pinned, repo-locked store of the resulting pairs on a DEDICATED connection. The pin
    + lock copy the EXACT proven pattern self_heal_main_graph uses for off-event-path DB work: derive the owning
    account from gh.installation_account_id() (the SAME stable key the live path uses → 'ACCT-GH-'||<owner_id>),
    take the SAME per-(account,repo) advisory lock, pin the tenant, write through the gated path, release in a
    finally. FAIL-CLOSED on an unresolved account (never an unrouted / cross-tenant write). FAIL-OPEN otherwise:
    co-change is advisory; any trouble is logged content-free and the rest of the product is untouched.

    The lock is taken ONLY around the quick store — NOT the clone — so this pool thread never holds a repo lock
    while a (slow) clone runs, and the store serializes with the live path for that repo exactly like boot does."""
    try:
        pairs = _cochange_pairs_via_clone(gh, repo, branch, window)   # SLOW; holds nothing
        if pairs is None:
            return {"ok": False, "cochange_error": "clone/extract failed"}
        dsn = os.environ.get("VERIPSA_DSN")
        if not dsn:                                                    # no live DB (unit/degraded) → nothing to store
            return {"ok": False, "skipped": "no VERIPSA_DSN"}
        account_key = gh.installation_account_id()                    # owning-account id == the live tenant key
        if not account_key:                                          # FAIL CLOSED — never write into an unrouted tenant
            return {"ok": False, "cochange_error": "could not resolve owning account (no cross-tenant write)"}
        import psycopg2
        _S = _server()
        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                # autocommit makes the DB gate's xact lock statement-local. Keep the same stable-object lock at
                # SESSION scope through generation check + store, before the mutable coordinate lock.
                _S._take_repository_id_lock(cur, repository_id)
                _S._take_repo_lock(cur, account_key, repo)            # SAME per-(account,repo) lock the live path holds
                cur.execute("SELECT core.enter_existing_installation_with_authority(%s)", (account_key,))
                route = cur.fetchone()
                if not route or route[0] in (None, ""):
                    return {"ok": False, "skipped": "installation route is absent or revoked"}
            db = _S._scoped_db(conn)
            # RESURRECTION GUARD (audit iter-4 P1): AFTER the per-repo lock + tenant pin, BEFORE the write, re-validate
            # the account is still LIVE. This task clones holding nothing, then locks per-repo + writes — but an
            # installation.deleted uninstall purge runs ACCOUNT-WIDE holding NO lock, so the per-repo lock can NOT
            # serialize against it (the lock-key asymmetry). Without this, a populate racing the uninstall would
            # re-create co_change rows (private file PATHS) into a tenant we just purged. assert_account_live raises
            # if the account is tombstoned (purged/erased) → caught by the outer except → skipped content-free.
            db("SELECT core.assert_account_live_with_authority()")
            if not _repository_generation_allowed(db, repo, repository_id):
                return {"ok": False, "skipped": "repository generation no longer current"}
            res = ingest_cochange(db, repo, pairs)                    # gated store on the pinned, locked connection
            res = res if isinstance(res, dict) else {"ok": True}
            res["pairs_found"] = len(pairs)
            return res
        finally:
            conn.close()                                              # releases the session advisory lock too
    except Exception as e:
        print(f"co-change populate (async) repo={repo}@{branch} skipped (advisory 2nd signal): {str(e)[:160]}", flush=True)
        return {"ok": False, "cochange_error": str(e)[:200]}


def populate_cochange_async(gh, repo: str, branch: str, window: int = 800, repository_id=None):
    """Hand co-change population to the dedicated FAIR pool and RETURN IMMEDIATELY (the live onboarding path) — the
    event worker is never held by the clone. The task is bucketed by the owning ACCOUNT (resolved best-effort at
    submit time, the SAME stable tenant key the task itself pins by) so a tenant onboarding a big org (one populate
    per repo) gets per-tenant round-robin turns + a per-account cap and can't starve other tenants' co-change.
    Returns the Future (prod fire-and-forgets it; a test awaits it) — or, over this account's fairness cap, a
    pre-resolved skipped Future (fail-open). Fail-open throughout: a scheduler that can't even accept the task must
    never abort onboarding."""
    try:
        task_args = (gh, repo, branch, window)
        if repository_id not in (None, ""):
            task_args += (repository_id,)
        return _cochange_scheduler().submit(_resolve_cochange_account(gh),
                                            _cochange_populate_task, *task_args, repo=repo)
    except Exception as e:
        print(f"co-change populate dispatch repo={repo}@{branch} skipped: {str(e)[:120]}", flush=True)
        return None


def push_commit_filesets(payload) -> list:
    """The PER-PUSH co-change input, extracted content-free from a `push` webhook payload's `commits[]` — one
    file SET per commit (added ∪ modified ∪ removed), NO clone. This is EXACTLY the shape _cg_cochange.fold_commits
    folds (the same per-commit grouping `_git_log_commits` produces from a clone), so the increment counts a push's
    commits byte-identically to a batch over them. The webhook push payload already carries each commit's changed-
    file lists, so the live per-push path can keep the co-change signal CURRENT between full backfills WITHOUT a
    git clone.

    CONTENT-FREE: only PATHS leave the payload — never a commit message, a body, an author, a diff. NEVER-RAISES
    (fail-open): a malformed/missing/oddly-typed payload, commits array, commit object, or path entry contributes
    NOTHING and is skipped — co-change is the advisory 2nd signal and must never break push handling. A commit's
    own added/modified/removed are merged into ONE set (a path listed twice counts once, exactly as the clone
    path's set does). Empty commits / a commit with no paths yield an empty set (fold_commits then skips them).

    NOTE on idempotency: this returns one set PER commit object in the payload, so the CALLER must de-duplicate a
    REDELIVERED push by commit sha before folding (a redelivery carries the SAME commits[] → folding twice would
    double-count). push_commit_filesets itself does not dedupe across deliveries — it is a pure payload reader."""
    out: list = []
    try:
        commits = (payload or {}).get("commits") if isinstance(payload, dict) else None
        if not isinstance(commits, list):
            return []
        for commit in commits:
            if not isinstance(commit, dict):
                continue
            fs = set()
            for key in ("added", "modified", "removed"):
                lst = commit.get(key)
                if isinstance(lst, list):
                    for p in lst:
                        if isinstance(p, str) and p:
                            fs.add(p)
            out.append(fs)           # one set per commit (may be empty → fold_commits skips it)
    except Exception:
        return []                    # advisory 2nd signal: a payload we cannot read yields no increment, never a crash
    return out


def cochange_all(db, repo: str, branch: str = None, limit: int = 5000) -> list:
    """READ BACK the repo's WHOLE stored co-change pair set ({a,b,co,n_a,n_b,n_total,strength,lift}) — the SEED
    the per-push increment folds onto (via _cg_cochange.seed_counters). Mirrors cochange_partners_for's gated-read
    shape exactly (governed SECURITY DEFINER surface, tenant re-pinned, FORCE-RLS walled). Never raises: co-change
    is the advisory 2nd signal — an unreadable seed yields no increment, never a crash. Content-free (paths +
    counts only)."""
    try:
        res = db("SELECT core.co_change_all_with_authority(%s,%s,%s)", (repo, branch, int(limit)))
        if isinstance(res, str):
            res = json.loads(res)
        return res or []
    except Exception:
        return []


def push_commit_shafilesets(payload) -> list:
    """The PER-PUSH co-change input WITH each commit's sha — (sha, fileset) pairs, content-free, NO clone. Same
    reader contract as push_commit_filesets (added∪modified∪removed per commit, paths only, never-raises) but it
    ALSO carries each commit's id so the persisted SHA-DEDUPE (fold a commit at most once, ever — across webhook
    REDELIVERIES) can key on it. A commit with no usable id falls back to a content-free synthetic key derived
    from its OWN path set + index, so it is never silently dropped (still folded once per delivery; a redelivery
    produces the SAME synthetic key → still deduped). Empty commits / unreadable payloads yield no entries."""
    out: list = []
    try:
        commits = (payload or {}).get("commits") if isinstance(payload, dict) else None
        if not isinstance(commits, list):
            return []
        import hashlib
        filesets = push_commit_filesets(payload)                  # one set per commit, in order (content-free)
        for i, fs in enumerate(filesets):
            sha = None
            try:
                c = commits[i] if i < len(commits) else None
                sid = c.get("id") if isinstance(c, dict) else None
                if isinstance(sid, str) and sid and all(ch in "0123456789abcdefABCDEF" for ch in sid) and len(sid) <= 64:
                    sha = sid
            except Exception:
                sha = None
            if sha is None:                                       # no usable hex id → a content-free synthetic hex key
                sha = hashlib.sha1((str(i) + "|" + "\x00".join(sorted(fs))).encode()).hexdigest()
            out.append((sha, fs))
    except Exception:
        return []                                                 # advisory 2nd signal: unreadable → no increment, never a crash
    return out


def _cochange_increment_task(gh, repo: str, branch: str, sha_filesets, repository_id=None) -> dict:
    """The PER-PUSH co-change increment, persisted OFF the event worker — keep the empirical-coupling signal
    CURRENT on a main push WITHOUT a clone. End to end: (1) resolve the owning account (FAIL CLOSED on an
    unresolved account — never an unrouted / cross-tenant write); (2) on a DEDICATED connection, take the SAME
    per-(account,repo) advisory lock + pin the SAME tenant as the live path and the async populate (the exact
    self_heal_main_graph / _cochange_populate_task pattern); (3) gated SHA-DEDUPE against the persisted folded-
    commit ledger (co_change_filter_unseen_commits_with_authority) — fold each commit AT MOST ONCE EVER, so a
    webhook REDELIVERY of the same push is a true no-op; (4) READ BACK the stored pairs, fold ONLY the unseen
    commits' file SETS onto the reconstructed counters via _cg_cochange.cochange_pairs_incremental (the SHARED
    fold_commits + emit_pairs → byte-identical to a batch over the same commits), and re-ingest the merged set
    through the SAME gated DELETE+reINSERT writer the backfill uses.

    `sha_filesets` is the content-free output of push_commit_shafilesets(payload) — (commit_sha, path SET) per
    commit, no clone. The ledger dedupe is ATOMIC (INSERT … ON CONFLICT … RETURNING under the repo lock), so two
    deliveries of the same push never both fold a commit.

    FAIL-OPEN: any trouble is logged content-free and the rest of the product is untouched — the structural
    verdict/check still posts. A push with NO non-empty commit file-sets is a true NO-OP (nothing to fold)."""
    try:
        items = [(sha, fs) for (sha, fs) in (sha_filesets or []) if fs]   # drop empty sets (fold_commits skips them anyway)
        if not items:
            return {"ok": True, "skipped": "no commit file-sets (nothing to fold)"}
        dsn = os.environ.get("VERIPSA_DSN")
        if not dsn:                                                # no live DB (unit/degraded) → nothing to persist
            return {"ok": False, "skipped": "no VERIPSA_DSN"}
        account_key = gh.installation_account_id()                 # owning-account id == the live tenant key
        if not account_key:                                       # FAIL CLOSED — never write into an unrouted tenant
            return {"ok": False, "cochange_error": "could not resolve owning account (no cross-tenant write)"}
        import _cg_cochange as CC
        import psycopg2
        _S = _server()
        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                _S._take_repository_id_lock(cur, repository_id)
                _S._take_repo_lock(cur, account_key, repo)         # SAME per-(account,repo) lock the live path holds
                cur.execute("SELECT core.enter_existing_installation_with_authority(%s)", (account_key,))
                route = cur.fetchone()
                if not route or route[0] in (None, ""):
                    return {"ok": False, "skipped": "installation route is absent or revoked"}
            db = _S._scoped_db(conn)
            # RESURRECTION GUARD (audit iter-4 P1): re-validate the account is still LIVE after the per-repo lock +
            # tenant pin and BEFORE any write (the sha-dedupe ledger + the pair re-ingest below). The uninstall purge
            # is account-wide and holds NO lock, so this per-repo lock can't serialize against it — without this, a
            # per-push fold racing the uninstall would re-write co_change file PATHS + seen-commit shas into a purged
            # tenant. Raises if tombstoned (purged/erased) → caught by the outer except → skipped content-free.
            db("SELECT core.assert_account_live_with_authority()")
            if not _repository_generation_allowed(db, repo, repository_id):
                return {"ok": False, "skipped": "repository generation no longer current"}
            # SHA-DEDUPE (persisted, atomic): record + return ONLY the commits never folded before for this repo.
            # A redelivery's shas are already present → unseen is empty → fold nothing (true idempotent no-op).
            shas = [sha for (sha, _fs) in items]
            unseen = db("SELECT core.co_change_filter_unseen_commits_with_authority(%s,%s)", (repo, shas))
            unseen = json.loads(unseen) if isinstance(unseen, str) else (unseen or [])
            unseen_set = set(unseen)
            fold_fs = [fs for (sha, fs) in items if sha in unseen_set]
            if not fold_fs:                                        # every commit already folded (a redelivery) → no-op
                return {"ok": True, "skipped": "all commits already folded (redelivery no-op)",
                        "commits_in_push": len(items), "commits_folded": 0}
            seed = cochange_all(db, repo, branch)                  # READ BACK the stored pairs (the seed — raw counts)
            # Fold at the PERSIST floor (raw, no prob/lift pruning) so a coupling that ACCUMULATES across pushes is
            # retained in the store and grows toward the render floor — instead of a sub-floor push being dropped and
            # the next push folding onto an empty seed. The render floor (co≥5, lift≥2) is applied at READ. PR #314.
            merged = CC.cochange_pairs_incremental(seed, fold_fs, min_support=CC.PERSIST_MIN_SUPPORT,
                                                   min_prob=0.0, min_lift=0.0)  # byte-identical to a batch over these commits
            res = ingest_cochange(db, repo, merged)                # gated DELETE+reINSERT of the merged set
            res = res if isinstance(res, dict) else {"ok": True}
            res["seed_pairs"] = len(seed)
            res["merged_pairs"] = len(merged)
            res["commits_folded"] = len(fold_fs)
            res["commits_in_push"] = len(items)
            return res
        finally:
            conn.close()                                           # releases the session advisory lock too
    except Exception as e:
        print(f"co-change per-push increment repo={repo}@{branch} skipped (advisory 2nd signal): {str(e)[:160]}", flush=True)
        return {"ok": False, "cochange_error": str(e)[:200]}


def increment_cochange_async(gh, repo: str, branch: str, payload, repository_id=None):
    """Hand the PER-PUSH co-change increment to the dedicated FAIR pool and RETURN IMMEDIATELY (the live push path)
    — the event worker is never held by the read/fold/store (and certainly not by a clone: there isn't one). Reads
    the push's content-free (commit_sha, file SET) pairs from `payload` (push_commit_shafilesets); the AUTHORITATIVE
    sha-dedupe is the persisted ledger inside the pooled task (idempotent across webhook REDELIVERIES), so this is
    a pure dispatcher. The task is bucketed by the owning ACCOUNT (resolved best-effort at submit time) so a tenant
    force-pushing a busy monorepo (one increment per push) gets per-tenant round-robin turns + a per-account cap and
    can't starve other tenants' co-change. Returns the Future (prod fire-and-forgets it; a test awaits it) — or, over
    this account's fairness cap, a pre-resolved skipped Future — or None when there is nothing to do. Fail-open: a
    scheduler that can't even accept the task must not abort push handling."""
    try:
        sha_filesets = push_commit_shafilesets(payload)
        if not any(fs for (_sha, fs) in sha_filesets):
            return None                                            # no commit file-sets → nothing to fold
        task_args = (gh, repo, branch, sha_filesets)
        if repository_id not in (None, ""):
            task_args += (repository_id,)
        return _cochange_scheduler().submit(_resolve_cochange_account(gh),
                                            _cochange_increment_task, *task_args, repo=repo)
    except Exception as e:
        print(f"co-change per-push dispatch repo={repo}@{branch} skipped: {str(e)[:120]}", flush=True)
        return None


def cochange_partners_for(db, repo: str, paths, limit: int = 3, min_prob: float = 0.4):
    """For a PR's edited `paths`, the strongest co-change PARTNER files NOT in the edit — the content-free
    "you touched A; B historically comes with it" hint, [{edited, partner, prob, co, n}, …]. Never raises (the
    advisory line is dropped on error; the rest of the comment is unaffected)."""
    try:
        res = db("SELECT core.co_change_partners_with_authority(%s,%s,%s,%s)",
                 (repo, list(paths or []), int(limit), float(min_prob)))
        if isinstance(res, str):
            res = json.loads(res)
        return res or []
    except Exception:
        return []
