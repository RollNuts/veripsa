#!/usr/bin/env python3
"""CO-CHANGE PER-TENANT CAP gate — the per-(account, repo) memory-guard cap (Round-2 concurrency follow-up).

THE GAP this gate locks shut: the co-change pool already has the per-tenant FAIRNESS cap
(VERIPSA_PER_ACCOUNT_COCHANGE_CAP) — a noisy tenant onboarding a big org can no longer starve other tenants. But
that cap is per-ACCOUNT (across all repos), so a tenant flooding ONE repo (rapid pushes / a webhook redelivery
storm targeting one monorepo) can still queue many clones of the SAME (account, repo) up to the per-account cap.
Each clone allocates its own tempdir on the small free-tier instance memory — the per-account cap (default 50)
is too generous to be a MEMORY guard.

THE FIX adds an ORTHOGONAL per-(account, repo) cap (VERIPSA_COCHANGE_PER_TENANT_CAP, default 3) that bounds
in-flight + queued tasks per (account, repo). Past it a new submit is DROPPED content-free + fail-open (co-change
is advisory — a dropped populate/increment self-heals on the next push), exactly like the existing per-account cap.
Kill switch: VERIPSA_COCHANGE_PER_TENANT_CAP=0 disables the gate (only the per-account cap remains active).

Run:  python3 tests/test_cochange_per_tenant_cap.py    (scheduler-level, DB-free + clone-free — deterministic)
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
    """A submit dropped over either cap returns a PRE-RESOLVED Future carrying a content-free skipped marker."""
    try:
        return bool(fut.done() and isinstance(fut.result(0), dict) and fut.result(0).get("skipped"))
    except Exception:
        return False


def _skipped_reason(fut):
    try:
        return fut.result(0).get("skipped") if fut.done() and isinstance(fut.result(0), dict) else None
    except Exception:
        return None


def facet_a_per_tenant_cap_drops():
    # The MEMORY guard: ONE (account, repo) past the per-tenant cap drops content-free + fail-open. We use a
    # per-tenant cap of 3 (the production default) and a generous per-account cap so this facet isolates the
    # per-(account, repo) gate, not the per-account fairness gate.
    sched = COCH._CochangeScheduler(per_account_cap=1000, maxsize=10000, per_tenant_cap=3)
    block = threading.Event()                              # keep the worker busy so the backlog actually FILLS

    def task(owner):
        block.wait(3.0)
        return {"ok": True, "owner": owner}

    hold = sched.submit("A", task, "A", repo="A/r1")       # worker grabs + blocks → backlog fills
    time.sleep(0.05)
    futs, raised = [], False
    try:
        # Same (account, repo) = ("A", "A/r1"). hold counts as 1 in-flight; 2 more fit (cap 3); the rest drop.
        futs = [sched.submit("A", task, "A", repo="A/r1") for _ in range(20)]
    except Exception:
        raised = True                                      # a drop must NEVER raise out of submit
    skipped = [f for f in futs if _is_skipped(f)]
    accepted = [f for f in futs if not _is_skipped(f)]
    chk((not raised) and len(accepted) == 2 and len(skipped) == 18,
        f"per-tenant cap: same (account, repo) over the cap DROPS content-free + fail-open (no raise; "
        f"accepted={len(accepted)} == cap-1 in-flight = 2, skipped={len(skipped)}, raised={raised})")
    if skipped:
        chk(_skipped_reason(skipped[0]) == "over per-tenant co-change cap",
            f"per-tenant cap: the dropped Future carries the per-tenant marker (got {_skipped_reason(skipped[0])!r})")
    # A DIFFERENT repo for the SAME account is unaffected — the gate is per-(account, repo), not per-account.
    other_repo = sched.submit("A", task, "A", repo="A/r2")
    chk(not _is_skipped(other_repo),
        "per-tenant cap: a DIFFERENT repo for the SAME account is still accepted (cap is per-(account, repo))")
    # A DIFFERENT account on the SAME (logical) repo name is unaffected too.
    other_acct = sched.submit("B", task, "B", repo="A/r1")
    chk(not _is_skipped(other_acct),
        "per-tenant cap: a DIFFERENT account on the same repo path is still accepted")
    block.set()
    for f in [hold, *futs, other_repo, other_acct]:
        try:
            f.result(timeout=5)
        except Exception:
            pass


def facet_b_slot_released_on_completion():
    # The cap is per (account, repo) IN-FLIGHT + QUEUED. After tasks COMPLETE the slot must be released, so the
    # next burst on the same (account, repo) is accepted again (the cap is not a permanent quota).
    sched = COCH._CochangeScheduler(per_account_cap=1000, maxsize=10000, per_tenant_cap=3)
    block1 = threading.Event()

    def task(owner):
        block1.wait(3.0)
        return {"ok": True, "owner": owner}

    hold = sched.submit("A", task, "A", repo="A/r1")
    futs = [sched.submit("A", task, "A", repo="A/r1") for _ in range(10)]
    block1.set()
    for f in [hold, *futs]:
        try:
            f.result(timeout=5)
        except Exception:
            pass
    # Wait for the worker to settle (the slot release runs in the worker thread after fn returns).
    time.sleep(0.2)
    # Second burst on the same (account, repo) — the slot was released, so the first cap-many are accepted again.
    block2 = threading.Event()

    def task2(owner):
        block2.wait(3.0)
        return {"ok": True, "owner": owner}

    hold2 = sched.submit("A", task2, "A", repo="A/r1")
    futs2 = [sched.submit("A", task2, "A", repo="A/r1") for _ in range(5)]
    accepted2 = [f for f in futs2 if not _is_skipped(f)]
    chk(not _is_skipped(hold2) and len(accepted2) == 2,
        f"per-tenant cap: COMPLETED tasks release the slot (second burst on same (account, repo) is accepted "
        f"again — hold2 accepted={not _is_skipped(hold2)}, follow-up accepted={len(accepted2)} == cap-1)")
    block2.set()
    for f in [hold2, *futs2]:
        try:
            f.result(timeout=5)
        except Exception:
            pass


def facet_c_kill_switch():
    # per_tenant_cap=0 = KILL SWITCH — the gate is disabled entirely (only the per-account fairness cap applies),
    # so a flood of the SAME (account, repo) past 3 is accepted (no per-tenant drop). This is the env=0 escape
    # hatch: an operator can turn the cap off without redeploying code.
    sched = COCH._CochangeScheduler(per_account_cap=1000, maxsize=10000, per_tenant_cap=0)
    block = threading.Event()

    def task(owner):
        block.wait(3.0)
        return {"ok": True, "owner": owner}

    hold = sched.submit("A", task, "A", repo="A/r1")
    futs = [sched.submit("A", task, "A", repo="A/r1") for _ in range(10)]
    per_tenant_drops = [f for f in futs if _skipped_reason(f) == "over per-tenant co-change cap"]
    chk(not _is_skipped(hold) and len(per_tenant_drops) == 0,
        f"kill switch: per_tenant_cap=0 disables the per-(account, repo) gate (per-tenant drops={len(per_tenant_drops)})")
    block.set()
    for f in [hold, *futs]:
        try:
            f.result(timeout=5)
        except Exception:
            pass


def facet_d_cap_knob():
    # The cap is a VALIDATED env knob (env_int, min_value=0 so 0 is the explicit disable, not silent-broken).
    # A NON-INT or NEGATIVE value still fails LOUD at startup (same posture as the fairness cap).
    from env_config import ConfigError, env_int
    chk(isinstance(COCH._COCHANGE_PER_TENANT_CAP, int) and COCH._COCHANGE_PER_TENANT_CAP >= 0,
        f"cap knob: VERIPSA_COCHANGE_PER_TENANT_CAP is a sane int (default {COCH._COCHANGE_PER_TENANT_CAP})")
    # 0 is the intentional disable, NOT a refusal — env_int(min_value=0) accepts it.
    os.environ["VERIPSA_COCHANGE_PER_TENANT_CAP"] = "0"
    zero_accepted = False
    try:
        v = env_int("VERIPSA_COCHANGE_PER_TENANT_CAP", 3, min_value=0)
        zero_accepted = (v == 0)
    except ConfigError:
        zero_accepted = False
    finally:
        os.environ.pop("VERIPSA_COCHANGE_PER_TENANT_CAP", None)
    chk(zero_accepted,
        "cap knob: VERIPSA_COCHANGE_PER_TENANT_CAP=0 is ACCEPTED (kill-switch path; min_value=0)")
    # A NEGATIVE value is still rejected loudly — never a silently-broken cap.
    neg_failed = False
    os.environ["VERIPSA_COCHANGE_PER_TENANT_CAP"] = "-1"
    try:
        env_int("VERIPSA_COCHANGE_PER_TENANT_CAP", 3, min_value=0)
    except ConfigError:
        neg_failed = True
    finally:
        os.environ.pop("VERIPSA_COCHANGE_PER_TENANT_CAP", None)
    chk(neg_failed,
        "cap knob: a NEGATIVE VERIPSA_COCHANGE_PER_TENANT_CAP is refused LOUDLY at startup")


def facet_e_per_account_cap_still_works():
    # Belt-and-braces: the existing per-ACCOUNT fairness cap still drops content-free when the per-tenant cap is
    # DISABLED. Proves the new per-tenant gate is additive, not a replacement.
    sched = COCH._CochangeScheduler(per_account_cap=5, maxsize=10000, per_tenant_cap=0)
    block = threading.Event()

    def task(owner):
        block.wait(3.0)
        return {"ok": True, "owner": owner}

    hold = sched.submit("A", task, "A", repo="A/r1")
    futs = [sched.submit("A", task, "A", repo=f"A/r{i}") for i in range(40)]
    fairness_drops = [f for f in futs if _skipped_reason(f) == "over per-account co-change fairness cap"]
    chk(len(fairness_drops) > 0,
        f"per-account fairness cap still active when per-tenant cap disabled (fairness drops={len(fairness_drops)})")
    block.set()
    for f in [hold, *futs]:
        try:
            f.result(timeout=5)
        except Exception:
            pass


def main() -> int:
    print("CO-CHANGE PER-TENANT CAP gate (per-(account, repo) memory guard; Round-2 follow-up)")
    facet_a_per_tenant_cap_drops()
    facet_b_slot_released_on_completion()
    facet_c_kill_switch()
    facet_d_cap_knob()
    facet_e_per_account_cap_still_works()
    ok = all(checks)
    print("CO-CHANGE PER-TENANT CAP GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
