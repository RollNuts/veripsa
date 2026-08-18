#!/usr/bin/env python3
"""RESERVE-BRANCH-LANES ATOMIC-ROLLBACK gate (small-findings root-fix sweep, finding 5).

THE CONCERN: webhook_handlers.reserve_branch_lanes loops over a push's changed paths and calls
core.act_for_claim_with_authority once PER PATH, with NO per-path try/except — it RELIES on make_db_processor's
atomic-per-event transaction (conn.autocommit=False around handle_event; commit on success, ROLLBACK + re-raise on
any error). THE ROOT QUESTION (this gate answers it LIVE, not by inspection): under a REAL gate error injected
MID-LOOP, does a mid-loop raise cleanly ROLL BACK every earlier path's lane reservation (no partial commit) and
re-raise so GitHub redelivers? If yes, NO per-path guard is needed — and adding one would be WRONG (it would
swallow the raise and let a PARTIAL lane reservation commit, defeating the all-or-nothing design). If the injection
showed a partial commit, a guard would be required.

THE INJECTION (a REAL gate error, not a mock): we replace core.act_for_claim_with_authority with a thin wrapper
that RAISES (in-SQL) when the target_path contains a poison marker and DELEGATES to the real function otherwise.
Then we push a multi-path commit whose poison path sits in the MIDDLE: reserve_branch_lanes reserves the first
path's lane (inside the event's body txn), then the poison path RAISES — and we assert that the WHOLE event rolled
back: ZERO lanes for the branch (the earlier path's reservation was discarded too), and the event re-raised
(counted failed → GitHub redelivers cleanly).

PROVES, against a REAL local Postgres, through the REAL make_db_processor:
  (A) a clean baseline: a normal multi-path push reserves a lane per code path (the loop works).
  (B) with a gate error injected MID-LOOP, the event RAISES (re-raised by the processor → failed → redeliver).
  (C) ATOMIC: ZERO lanes are reserved for the branch (NO partial reservation persisted) — the all-or-nothing
      body txn rolled back the earlier path's lane too.
  (D) a clean RETRY after removing the injected fault reserves all lanes (the redelivery re-runs from a clean
      baseline — idempotent), confirming the rollback left no half-state to wedge the retry.

PROCESS-UNIQUE scratch DB (parallel-safe). Run:  python3 tests/test_reserve_lanes_atomic.py
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import server as S  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402

DB = "veripsa_reservelanes_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
ACCOUNT_ID = 808
TENANT = f"ACCT-GH-{ACCOUNT_ID}"
checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


class FakeGitHub:
    """The minimal recording GitHub client reserve_branch_lanes' push path needs (it makes NO GitHub calls itself;
    the processor only reads installation/account ids from the payload). Methods exist for handle_event's safety."""
    def installation_account_id(self):
        return str(ACCOUNT_ID)

    def __getattr__(self, _name):
        def _noop(*a, **k):
            return None
        return _noop


def _push(repo, branch, sha, files):
    commits = [{"added": files, "modified": [], "removed": []}]
    return {"ref": f"refs/heads/{branch}", "after": sha, "installation": {"id": 4242},
            "repository": {"full_name": repo, "default_branch": "main", "owner": {"id": ACCOUNT_ID}},
            "commits": commits, "pusher": {"name": "dev"}}


def admin(sql, args=(), pin=TENANT):
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            if pin:
                cur.execute("SELECT set_config('core.current_account', %s, true)", (pin,))
            cur.execute(sql, args)
            try:
                row = cur.fetchone()
            except psycopg2.ProgrammingError:
                row = None
            conn.commit()
            return row[0] if row else None
    finally:
        conn.close()


def ddl(sql):
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql)
    finally:
        conn.close()


def lane_count(repo, cid):
    return admin("""SELECT count(*)::int FROM core.claim
                      WHERE repo=%s AND change_id=%s AND claim_state IN ('active','waiting')""", (repo, cid)) or 0


def install_poison():
    """Replace core.act_for_claim_with_authority with a wrapper that RAISES on a poison path, delegating to the real
    one otherwise. We rename the original first so the wrapper can call it for the non-poison paths (a REAL gate
    error injected mid-loop — the raise happens INSIDE the event's body txn, exactly as a true gate error would).
    The signature carries the trailing p_is_draft (PO 2026-06-25 scout-window) — 9 args total."""
    ddl("ALTER FUNCTION core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text,boolean,boolean) "
        "RENAME TO act_for_claim_real_lanetest")
    ddl("""CREATE OR REPLACE FUNCTION core.act_for_claim_with_authority(
              p_claim_id text, p_target_path text, p_repo text, p_branch text, p_author text,
              p_ranges jsonb DEFAULT NULL, p_base_hash text DEFAULT NULL, p_author_is_bot boolean DEFAULT false,
              p_is_draft boolean DEFAULT NULL)
            RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $f$
            BEGIN
              IF p_target_path LIKE '%POISON%' THEN
                RAISE EXCEPTION 'injected gate error (mid-loop fault) on poison path' USING ERRCODE='40001';
              END IF;
              RETURN core.act_for_claim_real_lanetest(p_claim_id, p_target_path, p_repo, p_branch, p_author,
                                                      p_ranges, p_base_hash, p_author_is_bot, p_is_draft);
            END $f$;""")
    ddl("GRANT EXECUTE ON FUNCTION core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text,boolean,boolean) TO veripsa_app")


def remove_poison():
    ddl("DROP FUNCTION core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text,boolean,boolean)")
    ddl("ALTER FUNCTION core.act_for_claim_real_lanetest(text,text,text,text,text,jsonb,text,boolean,boolean) "
        "RENAME TO act_for_claim_with_authority")
    ddl("GRANT EXECUTE ON FUNCTION core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text,boolean,boolean) TO veripsa_app")


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    seed_live_installation(
        DSN_APP,
        f"postgresql://veripsa_migrator@localhost/{DB}",
        ACCOUNT_ID,
        4242,
    )

    print("RESERVE-BRANCH-LANES ATOMIC ROLLBACK (small-findings root fix, finding 5)")
    gh = FakeGitHub()
    proc = S.make_db_processor(DSN_APP)
    REPO = "acme/lanes"
    BR_CID = "BR-feat/atomic"                                   # reserve_branch_lanes' work-unit id for branch feat/atomic

    # ── (A) CLEAN BASELINE: a normal multi-path push reserves one lane per CODE path (the loop works end-to-end).
    proc("push", _push(REPO, "feat/atomic", "a" * 40, ["backend/api.py", "backend/db.py", "backend/svc.py"]), None, gh)
    base_lanes = lane_count(REPO, BR_CID)
    chk(base_lanes == 3, f"(A) clean baseline: a 3-code-path push reserves 3 lanes for the branch (got {base_lanes})")

    # reset: a fresh branch coordinate so the injection test starts from zero lanes for ITS change id.
    BR2 = "BR-feat/inject"

    # ── (B)+(C) INJECT a gate error MID-LOOP: the poison path sits BETWEEN two real paths. reserve_branch_lanes
    #     reserves the first path's lane inside the body txn, then the poison path RAISES.
    install_poison()
    raised = False
    try:
        proc("push", _push(REPO, "feat/inject", "b" * 40,
                           ["backend/first.py", "backend/POISON_mid.py", "backend/third.py"]), None, gh)
    except Exception as e:
        raised = True
        print(f"    (injected) event raised as expected: {str(e)[:90]}")
    chk(raised, "(B) the mid-loop gate error RE-RAISED out of make_db_processor (→ counted failed → GitHub redelivers)")
    inj_lanes = lane_count(REPO, BR2)
    chk(inj_lanes == 0,
        f"(C) ATOMIC ROLLBACK: ZERO lanes reserved for the branch — the earlier path's reservation was rolled back too "
        f"(no partial lane reservation persisted; got {inj_lanes})")
    # also: the whole-repo live-claim count for this branch's change id is zero — nothing half-committed anywhere.
    any_first = admin("SELECT count(*)::int FROM core.claim WHERE repo=%s AND target_path=%s AND claim_state IN ('active','waiting')",
                      (REPO, "backend/first.py"))
    chk(any_first == 0, f"(C) the FIRST path (processed before the poison) also has NO surviving claim — the body txn rolled back as a unit (got {any_first})")

    # ── (D) CLEAN RETRY after removing the fault: the redelivery re-runs from a clean baseline and reserves all lanes
    #     (idempotent; the rollback left no half-state to wedge the retry).
    remove_poison()
    proc("push", _push(REPO, "feat/inject", "c" * 40,
                       ["backend/first.py", "backend/second.py", "backend/third.py"]), None, gh)
    retry_lanes = lane_count(REPO, BR2)
    chk(retry_lanes == 3, f"(D) clean RETRY after the fault clears reserves all 3 lanes (no half-state wedged the retry; got {retry_lanes})")

    print()
    if all(checks):
        print("RESERVE-LANES ATOMIC GATE: PASS")
        return 0
    print(f"RESERVE-LANES ATOMIC GATE: FAIL ({sum(1 for c in checks if not c)} of {len(checks)} failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
