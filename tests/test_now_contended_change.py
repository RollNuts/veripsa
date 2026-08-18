#!/usr/bin/env python3
"""NOW-CONTENDED-BY-CHANGE gate — the live "Now" banner must reflect what the GATE actually serializes, which
is keyed on the CHANGE (PR/branch), NOT on the author.

THE HOLE (this gate is the proof it is closed): `now_for_installation`'s `contended` list (the red "N files are
colliding right now" banner) used `HAVING count(DISTINCT agent_id) >= 2` — it only lit up when TWO DISTINCT
GitHub logins held/awaited a lane. But the gate (`_place_claim`) serializes a 2nd change on a held lane via
`NOT (agent_id = holder AND change_id = holder)` — i.e. it excludes only same-agent-AND-same-change. So ONE
author's two PRs on the same file DO collide: the 2nd is sent to 'waiting' and a collision_held fires. AI fleets
very commonly commit under a SINGLE shared login (one human running many agent sessions; one service account),
so the agent-based banner showed "All clear" at the exact moment Veripsa was holding one of your PRs in line —
the dashboard contradicting the gate, and hiding the product doing its job.

THE FIX: `HAVING count(DISTINCT change_id) >= 2` (+ a `changes_count` field). The banner now surfaces any file
with ≥2 in-flight CHANGES on its lane — author-agnostic, consistent with the gate. `agents` is kept as a
secondary cross-agent signal.

Proven on the REAL gate + REAL surface (db/schema.sql), no mocks:
  (A) SAME-LOGIN — two PRs by ONE author on one file: the 2nd is 'waiting' (gate serialized it) AND the file
      now appears in contended with changes_count==2, waiting==1, agents==1.
  (B) CONTROL (load-bearing) — that same row has agents==1, so the OLD `count(DISTINCT agent_id) >= 2` would
      have EXCLUDED it (empty banner). The fix genuinely changes the verdict, it is not a no-op.
  (C) CROSS-AGENT regression — two DIFFERENT authors on a file still surface (agents==2, changes_count==2).
  (D) NO FALSE ALARM — a file with a SINGLE in-flight change never appears.

Needs local Postgres with the veripsa roles. Run:  python3 tests/test_now_contended_change.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): per-PID so concurrent run_gates shards never drop each other's DB mid-run.
DB = "veripsa_nowcontended_" + str(os.getpid())
REPO = "acme/now"
INSTALL_ID = "606060"   # → enter_installation provisions account ACCT-GH-606060 (pinned on the App connection)

FAIL = 0


def check(cond: bool, label: str):
    global FAIL
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        FAIL += 1


def declare(app, change_id, path, author):
    """Reserve a lane exactly as the App's per-path declare does: act_for the PR author, base branch 'main'
    (webhook passes the PR's BASE branch), on the persistent App connection that already pinned the installation."""
    with app.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
                    (f"{change_id}:{path}", path, REPO, "main", author))


def claim_state(mig, change_id, path):
    # set_config(...,is_local=true) is TRANSACTION-scoped, so the pin + SELECT must share a txn (FORCE RLS on).
    with mig, mig.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT set_config('core.current_account', %s, true)", ("ACCT-GH-" + INSTALL_ID,))
        cur.execute("""SELECT claim_state FROM core.claim
                       WHERE change_id=%s AND target_path=%s AND repo=%s AND branch='main'
                       ORDER BY claimed_at DESC LIMIT 1""", (change_id, path, REPO))
        row = cur.fetchone()
        return row[0] if row else None


def contended_by_path(app):
    """now_for_installation's `contended`, keyed by path. The REAL read-surface (content-free, routed)."""
    with app.cursor() as cur:
        cur.execute("SELECT core.now_for_installation(%s)", (INSTALL_ID,))
        raw = cur.fetchone()[0]
    now = raw if isinstance(raw, dict) else json.loads(raw)
    return {c["path"]: c for c in now.get("contended", [])}


def probes():
    app = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    app.autocommit = True
    mig = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with app.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (INSTALL_ID,))

        print("-- (A) SAME-LOGIN: two PRs by ONE author on server.py (base=main) --")
        declare(app, "PR-101", "server.py", "octo")
        declare(app, "PR-102", "server.py", "octo")   # same author, different change → must serialize
        check(claim_state(mig, "PR-101", "server.py") == "active", "PR-101 holds server.py = 'active'")
        check(claim_state(mig, "PR-102", "server.py") == "waiting",
              "PR-102 (same author, 2nd PR) is sent to 'waiting' = the gate serialized it")

        # (D) no-false-alarm control: a single in-flight change must NOT be contended.
        declare(app, "PR-900", "solo.py", "octo")

        # (C) cross-agent: two DIFFERENT authors on routes.py.
        print("-- (C) CROSS-AGENT: two authors on routes.py --")
        declare(app, "PR-201", "routes.py", "alice")
        declare(app, "PR-202", "routes.py", "bob")

        c = contended_by_path(app)

        print("-- (A) the same-login file now surfaces on the banner --")
        srv = c.get("server.py")
        check(srv is not None, "server.py APPEARS in contended (the false-clear is CLOSED)")
        if srv:
            check(srv.get("changes_count") == 2, "server.py changes_count == 2 (two in-flight PRs)")
            check(srv.get("waiting") == 1, "server.py waiting == 1 (one PR held in line)")
            check(sorted(srv.get("changes", [])) == ["PR-101", "PR-102"],
                  "server.py changes lists BOTH PRs (linkable on the banner)")
            # (B) CONTROL — load-bearing: agents==1, so the OLD count(DISTINCT agent_id)>=2 would EXCLUDE this row.
            check(srv.get("agents") == 1,
                  "server.py agents == 1 → the OLD agent-based HAVING would have MISSED it (fix is real, not a no-op)")

        print("-- (C) cross-agent still surfaces (regression) --")
        rt = c.get("routes.py")
        check(rt is not None and rt.get("changes_count") == 2 and rt.get("agents") == 2,
              "routes.py surfaces with changes_count==2 AND agents==2 (cross-agent unaffected)")

        print("-- (D) a single in-flight change is NOT a collision --")
        check("solo.py" not in c, "solo.py (one PR) is NOT in contended (no false alarm)")
    finally:
        app.close()
        mig.close()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1
    try:
        probes()
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)

    print()
    if FAIL == 0:
        print("NOW CONTENDED-BY-CHANGE GATE: PASS")
        return 0
    print(f"NOW CONTENDED-BY-CHANGE GATE: FAIL ({FAIL} failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
