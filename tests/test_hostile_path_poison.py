#!/usr/bin/env python3
"""NEVER-CRASH: a HOSTILE changed-file PATH must never poison a PR's whole event.

THE DEFECT (audit: never-crash 2026-06-20). The gate caps a claim's `target_path` at 1024 chars
(db/schema/20_core.sql `claim_path_len`; db/schema/30_gate.sql `_place_claim` RAISEs 23514 'target_path too
long (max 1024)' above it) and psycopg2 refuses a string literal carrying a NUL (0x00) byte. The brain takes
each changed file path from GitHub's Files API and passes it STRAIGHT to declare_claim / act_for_claim
(webhook.py) — and `_code_paths` (webhook_handlers.py), the shared filter every path flows through, did NFC +
dedup + non-code drop but NEVER bounded path length nor stripped a NUL. So a PR that adds ONE file whose path
is > 1024 chars (a legitimate, attacker-craftable filename — git allows paths to 4096 bytes, and GitHub's API
returns the real name) makes the per-path claim loop RAISE mid-event. That exception ESCAPES handle_event:

  * it ABORTS the shared per-event transaction → the PR coordinates NOTHING (a false clear / silent miss),
  * the worker counts the event 'failed' and re-raises → GitHub redelivers → it re-crashes DETERMINISTICALLY
    = a POISON EVENT (exactly the class the _bounded_claim_id collision guard already defends, but on the
    target_path itself, which was left unbounded).

A crafted over-length (or NUL-bearing) filename in ANY single PR file thus SILENTLY SUPPRESSES Veripsa on that
whole PR — the App stops protecting that repo for that change, invisibly.

THE FIX (recall-safe, content-free): `_code_paths` now DROPS any path the gate would reject — one whose NFC
form exceeds the gate's 1024-char cap, or that contains a NUL byte. Dropping ONE pathological path (a clean
miss on that file, the REST of the PR still analyzed) is strictly safer than crashing the whole event and
poisoning every redelivery. A real source path is far under 1024 chars and carries no NUL, so valid input is
UNCHANGED (no path a customer would ever commit is affected).

This gate proves BOTH:
  * UNIT: _code_paths drops an over-cap / NUL path while keeping its valid siblings, and is a no-op on every
    realistic path (valid input unchanged).
  * END-TO-END (real DB, FakeGitHub, the LIVE handle_event over a SHARED non-autocommit txn like the live
    processor): a PR whose file set includes an over-cap path and a NUL path does NOT raise, the shared txn
    stays USABLE (not left aborted = no silent poison), and the PR's valid sibling file IS still claimed.

Run:  python3 tests/test_hostile_path_poison.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import psycopg2  # noqa: E402
from _server_harness import DB, REPO, SHA, FakeGitHub, make_db, pr_payload  # noqa: E402

FAIL = 0
NUL = chr(0)
# The gate's claim target_path cap (db/schema/20_core.sql claim_path_len / 30_gate.sql _place_claim). Anything
# longer RAISEs 23514 inside the per-path claim loop. The brain must never feed the gate a path over this.
GATE_PATH_CAP = 1024


def check(cond: bool, label: str):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


# ---------------------------------------------------------------------------
# UNIT: _code_paths drops what the gate would reject; no-op on realistic input
# ---------------------------------------------------------------------------
def test_code_paths_unit():
    import webhook_handlers as H

    over_cap = "src/" + ("x" * (GATE_PATH_CAP + 50)) + ".py"     # > 1024 chars → gate would RAISE 23514
    at_cap = "src/" + ("y" * (GATE_PATH_CAP - len("src/.py"))) + ".py"  # exactly 1024 → still accepted (boundary)
    nul_path = "backend/au" + NUL + "th.py"                       # NUL → psycopg2 refuses the bind literal
    valid = "backend/auth.py"

    out = H._code_paths([valid, over_cap, nul_path])
    check(valid in out, "valid sibling KEPT alongside a hostile path")
    check(over_cap not in out, "over-1024-char path DROPPED (gate would RAISE 'target_path too long')")
    check(nul_path not in out, "NUL-bearing path DROPPED (psycopg2 refuses a NUL string literal)")
    check(all(len(p) <= GATE_PATH_CAP and NUL not in p for p in out),
          "every surviving path is gate-safe (<=1024 chars, no NUL)")

    # BOUNDARY: a path EXACTLY at the cap is still accepted (the gate's CHECK is <=1024, not <1024) — the fix
    # must not over-prune a legitimate boundary path.
    out2 = H._code_paths([at_cap])
    check(len(at_cap) == GATE_PATH_CAP and at_cap in out2,
          f"path EXACTLY at the {GATE_PATH_CAP} cap is KEPT (gate accepts <=cap)")

    # VALID-INPUT UNCHANGED: a normal realistic file list is returned identically (modulo the existing NFC/dedup
    # /non-code drop the function already did) — the fix adds NO behavior on any path a customer would commit.
    normal = ["backend/auth.py", "backend/api.py", "frontend/src/components/Button.tsx", "lib/util.go"]
    check(H._code_paths(normal) == normal, "realistic path list returned UNCHANGED (valid input untouched)")


# ---------------------------------------------------------------------------
# END-TO-END: the LIVE handle_event over a shared txn must not poison the PR
# ---------------------------------------------------------------------------
def test_handle_event_no_poison():
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        global FAIL
        FAIL = 1
        return
    try:
        # Seed main's graph (the prediction baseline) via a connect-per-query runner, exactly like test_server.
        db_seed = make_db("veripsa_app")
        sys.path.insert(0, ROOT)
        import code_graph_extract as X
        graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
        db_seed("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", SHA))

        import server as S

        # A SHARED, single non-autocommit connection — the SAME transaction model the live event_processor uses
        # (event_processor._scoped_db over one conn, committed once at the end). This is the model the poison
        # lived in: a mid-loop RAISE there aborts the txn for everything after it.
        conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(4242),))
        conn.autocommit = False

        def shared_db(sql, args=()):
            with conn.cursor() as cur:
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None

        over_cap = "src/" + ("x" * (GATE_PATH_CAP + 50)) + ".py"
        nul_path = "backend/au" + NUL + "th.py"
        valid = "backend/auth.py"
        gh = FakeGitHub({77: [valid, over_cap, nul_path]})

        raised = None
        try:
            S.handle_event("pull_request", pr_payload("opened", 77, "alice"), shared_db, gh)
        except Exception as e:
            raised = e
        check(raised is None,
              f"handle_event does NOT raise on a hostile-path PR (was: {type(raised).__name__ if raised else 'OK'})")

        # The shared txn must still be USABLE — a poison event leaves it aborted so every later statement on the
        # event (the pause-ack overlay, the check/comment post) silently fails ("current transaction is aborted").
        txn_ok = False
        try:
            txn_ok = shared_db("SELECT 1") == 1
        except Exception:
            txn_ok = False
        check(txn_ok, "shared per-event txn stays USABLE after the hostile-path PR (no silent poison)")

        # COMMIT the event's writes (the live processor commits one txn per event), then read the claim back past
        # RLS via the migrator role (the App role writes through gates only — it has no raw table grant; the
        # 'permission denied for table claim' moat). The event pinned the tenant by owner_id 4242 → ACCT-GH-4242.
        if txn_ok:
            conn.commit()
        conn.close()
        mig = make_db("veripsa_migrator")
        claimed = mig("""SELECT set_config('core.current_account',%s,true);
            SELECT count(*)::int FROM core.claim WHERE repo=%s AND target_path=%s
                   AND claim_state IN ('active','waiting')""",
                      ("ACCT-GH-4242", REPO, valid))
        check(claimed == 1,
              f"the PR's VALID file IS still claimed (count={claimed}) — only the hostile path was dropped")
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], cwd=ROOT, capture_output=True, text=True)


def main() -> int:
    print("test_code_paths_unit")
    test_code_paths_unit()
    print("test_handle_event_no_poison")
    test_handle_event_no_poison()
    if FAIL:
        print("HOSTILE PATH POISON GATE: FAIL")
        return 1
    print("HOSTILE PATH POISON GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
