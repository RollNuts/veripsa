#!/usr/bin/env python3
"""CO-CHANGE FAIRNESS gate — the dedicated co-change pool must give the SAME per-tenant fairness the MAIN event
worker has (audit finding P2-1).

THE GAP this gate locks shut: the main event worker (event_queue.py _FairQueue) is multi-tenant FAIR — per-account
round-robin drain + a per-account cap — so one noisy tenant can't starve the others. The dedicated CO-CHANGE pool
(cochange.py) USED to be a plain ThreadPoolExecutor(max_workers=1): an UNBOUNDED, strict-FIFO queue with NEITHER
guard. A noisy tenant onboarding a big org (one populate_cochange_async per repo) or force-pushing a busy monorepo
(one increment_cochange_async per push) piled slow blobless-clone tasks into the single FIFO ahead of EVERY other
tenant → a 2nd customer's co-change (the advisory 2nd signal) went stale until the noisy tenant's clones finished.

The fix gave the co-change pool the SAME mechanism (REUSING event_queue._FairQueue as the backlog store, drained by
a single daemon worker that keeps clone serialization): per-account round-robin + a per-account cap
(VERIPSA_PER_ACCOUNT_COCHANGE_CAP, default 50). This gate proves it the way test_server.py proves the event
worker's fairness — at the scheduler level, DB-free + clone-free (so it is fast + deterministic, not a timing race):

  (a) ANTI-STARVATION: tenant A floods the pool, tenant B submits ONE task LAST. Under strict FIFO B drains dead
      last (behind all of A); under fair round-robin B gets a turn within the first couple — its co-change is NOT
      stuck behind A's monorepo burst.
  (b) PER-ACCOUNT CAP + DROP CONTENT-FREE / FAIL-OPEN: past the cap, A's further submits are SKIPPED content-free
      — the dispatch NEVER raises, returns a resolved Future carrying a skipped marker (co-change is advisory; a
      dropped populate self-heals on the next push) — while a DIFFERENT tenant's submit is still accepted.
  (c) END-TO-END through the REAL dispatcher (populate_cochange_async): A floods past the cap and B is dispatched
      LAST through the actual live entry point; B's task still runs early (fair), and A's over-cap dispatches are
      dropped content-free (no raise) — proving the fairness lives in the real submit path, not only the bare class.

Run:  python3 tests/test_cochange_fairness.py    (no Postgres, no git — pure scheduler-level, like the main-worker
                                                  fairness facets in tests/test_server.py)
"""
from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import cochange as COCH  # noqa: E402

checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def _is_skipped(fut) -> bool:
    """A submit dropped over the per-account cap returns a PRE-RESOLVED Future carrying a content-free skipped
    marker (never an exception, never a raise). True iff this Future is that drop."""
    try:
        return bool(fut.done() and isinstance(fut.result(0), dict) and fut.result(0).get("skipped"))
    except Exception:
        return False


class _FlakyGh:
    """The only gh seam the scheduler touches at SUBMIT time: installation_account_id() (the owning-account key it
    buckets by). `boom=True` makes it raise — to prove account resolution is best-effort + fail-open (a degraded
    gh still gets a fair bucket, never a crash)."""

    def __init__(self, account, boom=False):
        self._account, self._boom = account, boom

    def installation_account_id(self):
        if self._boom:
            raise RuntimeError("account resolution unavailable")
        return self._account


def facet_a_anti_starvation():
    # ONE worker drains the backlog, so the DRAIN ORDER is the whole fairness story. A floods 30 tasks, then B
    # submits ONE last. Strict FIFO ⇒ B is position 30; fair round-robin ⇒ B comes out within the first couple.
    sched = COCH._CochangeScheduler(per_account_cap=1000, maxsize=10000)   # cap high so THIS facet tests only order
    release = threading.Event()
    order, olock = [], threading.Lock()

    def task(owner):
        release.wait(3.0)                                  # hold turn 1 so the whole burst enqueues before any drains
        with olock:
            order.append(owner)
        return {"ok": True, "owner": owner}

    f_hold = sched.submit("A", task, "A")                  # worker grabs this + blocks on release
    time.sleep(0.05)
    a_futs = [sched.submit("A", task, "A") for _ in range(30)]   # A floods while the worker is held
    b_fut = sched.submit("B", task, "B")                   # B arrives LAST in wall-clock order
    release.set()
    for f in [f_hold, *a_futs, b_fut]:
        try:
            f.result(timeout=5)
        except Exception:
            pass
    b_pos = order.index("B") if "B" in order else 999
    # B within the first COUPLE of turns (<=2, like the main worker's end-to-end EventQueue assertion) — its
    # position is tiny + INDEPENDENT of A's flood size (30), the whole point of round-robin. Position 2 here is
    # exact fairness: turn 0 is the in-flight task the worker already grabbed, turn 1 is A's one round-robin pick,
    # turn 2 is B — NOT stuck behind all 30 of A. Strict FIFO would put B at 30.
    chk(b_pos <= 2,
        f"anti-starvation: a flooding tenant (A x30) does NOT starve tenant B's later co-change task "
        f"(B drained at position {b_pos}/{len(order)}; strict FIFO would be 30)")


def facet_b_cap_drop_failopen():
    # PER-ACCOUNT CAP: past the cap, ONE tenant's further submits are dropped CONTENT-FREE + FAIL-OPEN (no raise),
    # while a DIFFERENT tenant's submit is still accepted (a flood can't crowd every other tenant out).
    sched = COCH._CochangeScheduler(per_account_cap=5, maxsize=10000)
    block = threading.Event()                              # keep the worker busy so the backlog actually FILLS

    def task(owner):
        block.wait(3.0)
        return {"ok": True, "owner": owner}

    hold = sched.submit("A", task, "A")                    # worker grabs + blocks → the rest pile into the backlog
    time.sleep(0.05)
    a_futs, raised = [], False
    try:
        a_futs = [sched.submit("A", task, "A") for _ in range(40)]
    except Exception:
        raised = True                                      # a drop must NEVER raise out of submit
    a_skipped = sum(1 for f in a_futs if _is_skipped(f))
    a_accepted = sum(1 for f in a_futs if not _is_skipped(f))
    b_fut = sched.submit("B", task, "B")                   # a DIFFERENT tenant: still accepted (its bucket is empty)
    chk((not raised) and a_skipped > 0 and a_accepted <= 5 and not _is_skipped(b_fut),
        f"per-account cap: A's over-cap submits are DROPPED content-free + fail-open (no raise; accepted={a_accepted} "
        f"<=cap5, skipped={a_skipped}, raised={raised}) while tenant B is still accepted (B skipped={_is_skipped(b_fut)})")
    # the dropped Futures still satisfy .result() (the contract every caller relies on) with an advisory marker.
    a_drop = next((f for f in a_futs if _is_skipped(f)), None)
    chk(a_drop is not None and a_drop.result(0).get("ok") is False,
        f"per-account cap: a dropped submit returns a RESOLVED advisory Future (.result() works) "
        f"({a_drop.result(0) if a_drop else None})")
    block.set()
    for f in [hold, *a_futs, b_fut]:
        try:
            f.result(timeout=5)
        except Exception:
            pass


def facet_c_end_to_end_real_dispatcher():
    # END-TO-END through the REAL live entry point populate_cochange_async — proves the fairness lives in the actual
    # dispatch path, not only the bare scheduler. We monkeypatch the SLOW pooled task (_cochange_populate_task) with
    # a fast, gated stand-in so the gate needs no clone/DB; the REAL dispatcher (account resolution at submit + the
    # fair scheduler + the single drainer) runs unchanged. Use a FRESH scheduler so other facets' state can't leak.
    fresh = COCH._CochangeScheduler(per_account_cap=5, maxsize=10000)
    orig_sched_fn = COCH._cochange_scheduler
    orig_task = COCH._cochange_populate_task
    COCH._cochange_scheduler = lambda: fresh               # the real dispatcher resolves the scheduler through this
    release = threading.Event()
    order, olock = [], threading.Lock()

    def fake_task(gh, repo, branch, window=800):
        release.wait(3.0)
        with olock:
            order.append(gh.installation_account_id())
        return {"ok": True, "repo": repo}

    COCH._cochange_populate_task = fake_task
    try:
        ghA, ghB = _FlakyGh("A"), _FlakyGh("B")
        hold = COCH.populate_cochange_async(ghA, "A/seed", "main")   # worker grabs + blocks
        time.sleep(0.05)
        raised = False
        a_futs = []
        try:
            a_futs = [COCH.populate_cochange_async(ghA, f"A/r{i}", "main") for i in range(40)]
        except Exception:
            raised = True                                  # the live dispatcher must NEVER raise (fail-open)
        b_fut = COCH.populate_cochange_async(ghB, "B/r0", "main")    # B onboards LAST
        a_skipped = sum(1 for f in a_futs if _is_skipped(f))
        chk((not raised) and a_skipped > 0 and not _is_skipped(b_fut),
            f"end-to-end (real populate_cochange_async): A's over-cap onboards drop content-free + fail-open "
            f"(no raise; A skipped={a_skipped}) while B's onboard is accepted")
        release.set()
        for f in [hold, *a_futs, b_fut]:
            try:
                f.result(timeout=5)
            except Exception:
                pass
        b_pos = order.index("B") if "B" in order else 999
        # <=2 (same bound as the main worker's end-to-end EventQueue fairness assertion): B is serviced within the
        # first couple of turns, INDEPENDENT of A's flood — turn 0 = in-flight task, turn 1 = A's round-robin pick,
        # turn 2 = B. The rest of A's (uncapped) backlog drains AFTER B's turn, never starving it.
        chk(b_pos <= 2,
            f"end-to-end (real populate_cochange_async): tenant B's co-change runs EARLY despite A's flood "
            f"(B at drain position {b_pos}/{len(order)}) — not starved behind the noisy tenant")

        # FAIL-OPEN account resolution: a degraded gh whose installation_account_id() RAISES must still be dispatched
        # (best-effort key → keyless fair bucket), never crash the live onboarding path.
        boom_release = threading.Event()
        COCH._cochange_populate_task = lambda gh, repo, branch, window=800: (boom_release.wait(2.0) or {"ok": True})
        boom_fut, boom_raised = None, False
        try:
            boom_fut = COCH.populate_cochange_async(_FlakyGh(None, boom=True), "C/r0", "main")
        except Exception:
            boom_raised = True
        boom_release.set()
        chk((not boom_raised) and boom_fut is not None,
            f"fail-open: a degraded gh (installation_account_id raises) is still dispatched, never crashes onboarding "
            f"(raised={boom_raised}, dispatched={boom_fut is not None})")
        if boom_fut is not None:
            try:
                boom_fut.result(timeout=5)
            except Exception:
                pass
    finally:
        COCH._cochange_scheduler = orig_sched_fn
        COCH._cochange_populate_task = orig_task


def facet_d_cap_knob():
    # The cap is a VALIDATED env knob (env_int, min_value=1) so a bad value fails LOUD at startup — the SAME posture
    # as the event worker's VERIPSA_PER_ACCOUNT_QUEUE_CAP (never a silently-disabled co-change path).
    from env_config import ConfigError, env_int
    chk(isinstance(COCH._PER_ACCOUNT_COCHANGE_CAP, int) and COCH._PER_ACCOUNT_COCHANGE_CAP >= 1,
        f"cap knob: VERIPSA_PER_ACCOUNT_COCHANGE_CAP is a sane int (default {COCH._PER_ACCOUNT_COCHANGE_CAP})")
    bad_failed = False
    os.environ["VERIPSA_PER_ACCOUNT_COCHANGE_CAP"] = "0"   # 0 is the worst silent misconfig (every submit dropped)
    try:
        env_int("VERIPSA_PER_ACCOUNT_COCHANGE_CAP", 50, min_value=1)
    except ConfigError:
        bad_failed = True
    finally:
        os.environ.pop("VERIPSA_PER_ACCOUNT_COCHANGE_CAP", None)
    chk(bad_failed,
        "cap knob: a 0/negative VERIPSA_PER_ACCOUNT_COCHANGE_CAP is REFUSED loudly at startup (env_int, min_value=1) "
        "— never a silently-disabled co-change path")


def main() -> int:
    print("CO-CHANGE FAIRNESS gate (per-tenant round-robin + per-account cap on the co-change pool; P2-1)")
    facet_a_anti_starvation()
    facet_b_cap_drop_failopen()
    facet_c_end_to_end_real_dispatcher()
    facet_d_cap_knob()
    ok = all(checks)
    print("CO-CHANGE FAIRNESS GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
