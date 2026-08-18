#!/usr/bin/env python3
"""SEAT METERING gate — the founder's seat/MRR line must meter REAL HUMANS, not a structural $0.

THE BUG THIS LOCKS (measured on origin/main): a "seat" is a distinct HUMAN author active in 30d
(core._seat_count → agent_kind='human'). But every PR author was provisioned with the table DEFAULT
agent_kind ('ai') and NO code path ever set 'human' — so _seat_count was STRUCTURALLY 0 for every
tenant, and owner_cost_surface read active_agents=0 / paid_seats=0 (MRR = $0) no matter how many real
humans were shipping. This gate drives the REAL hosted gate path and asserts the seat line meters the
HUMAN PR authors and EXCLUDES bots — it FAILS on origin/main (count 0) and PASSES after the fix.

What it proves, end-to-end over the real DB (no mocks):
  • a HUMAN PR author (act_for_claim) is provisioned agent_kind='human' → counts as a seat.
  • a HUMAN landing author (record_landing) is provisioned 'human' too (the merge/push path).
  • a BOT author (the webhook's user.type=='Bot' → p_author_is_bot=true) is stamped 'ai' → NOT a seat
    (the AI-fleet wedge: bots are free). Bots can't be told from the stored login ('[bot]' is stripped),
    so the bot signal MUST come from the gate's bot flag — this asserts it does.
  • SELF-HEAL: an author already stored under the OLD default ('ai') is CORRECTED to 'human' on its next
    claim — so existing mis-metered tenants recover without a migration (but a bot stays 'ai', no false flip).
  • _seat_count(30), pinned to the account, == the human count and EXCLUDES the bot (the load-bearing 0→N).
  • owner_cost_surface() reflects the same real seat count (active_agents / paid_seats_total).
  • the App/seat agents (AG-*) are NEVER flipped (only 'GH-' author rows self-heal).

RLS TRAP (documented): core.agent is FORCE-RLS; a direct read/UPDATE needs a session-level pin
(set_config('core.current_account', <acct>, false)) on the SAME connection, or every row is hidden
(empty != none). The seat fns pin internally; this test pins its own direct reads via the migrator.

Run:  python3 tests/test_seat_metering.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# drop each other's DB mid-run. Per-PID, exactly like db/smoke.sh / run_gates / test_tenant_isolation.
DB = "veripsa_seatmeter_" + str(os.getpid())
INSTALL = "5150"
ACCT = "ACCT-GH-" + INSTALL


def app_conn():
    """A connection ENTERED into the installation's tenant — exactly the live per-event processor's context."""
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", (INSTALL,))
    return conn


def run(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        try:
            r = cur.fetchone()
            return r[0] if r else None
        except psycopg2.ProgrammingError:
            return None


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    app = app_conn()
    # MIGRATOR connection for the RLS-guarded direct reads of core.agent / _seat_count (owns the table + the
    # fn; SESSION-level pin so the FORCE-RLS policy admits this account's rows — the RLS TRAP in the docstring).
    mig = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    mig.autocommit = True
    with mig.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT set_config('core.current_account', %s, false)", (ACCT,))

    def agent_kind(agent_id):
        return run(mig, "SELECT agent_kind FROM core.agent WHERE agent_id=%s", (agent_id,))

    def seat_count():
        return run(mig, "SELECT core._seat_count(30)")

    def cost_surface_for_account():
        s = run(app, "SELECT core.owner_cost_surface()")
        s = s if isinstance(s, dict) else json.loads(s)
        row = next((a for a in s.get("accounts", []) if a["account_id"] == ACCT), {})
        return row.get("active_agents"), s.get("paid_seats_total"), s.get("over_seat_line_count")

    # ── seed a tiny graph at the shared coordinate ──────────────────────────────────────────────────────────
    REPO, BR = "acme/billing", "main"
    g = {"nodes": [{"id": p, "kind": "file", "path": p, "language": "python"}
                   for p in ["a.py", "b.py", "c.py", "d.py", "e.py"]], "edges": []}
    run(app, "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(g), REPO, BR, "a" * 40))

    # ── THREE HUMAN authors via the REAL hosted gate path (bot flag defaults false = human) ─────────────────
    # alice + bob: PR claims (act_for_claim). carol: a LANDING author (record_landing) — proves BOTH provisioning
    # sites stamp 'human'. (3 distinct humans; the default free seat line is 2, so this crosses the value line.)
    run(app, "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-alice:a.py", "a.py", REPO, BR, "alice"))
    run(app, "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-bob:b.py", "b.py", REPO, BR, "bob"))
    run(app, "SELECT core.record_landing_with_authority(%s,%s,%s,%s,%s)", (REPO, BR, "c" * 40, ["c.py"], "carol"))

    # ── a BOT author (the webhook's pull_request.user.type=='Bot' → p_author_is_bot=true, the 8th arg). The
    # '[bot]' marker is stripped from the login on the way in, so WITHOUT the flag this row would default to 'ai'
    # anyway — but that is exactly the structural bug; here we assert the flag is what keeps it free, by ALSO
    # proving the SELF-HEAL would otherwise flip an 'ai' author to 'human'. dependabot[bot] → dependabotbot. ──
    run(app, "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
        ("PR-bot:d.py", "d.py", REPO, BR, "dependabotbot", None, None, True))

    # ── assertions: the kinds are stamped right at the gate ─────────────────────────────────────────────────
    check("a HUMAN PR author (act_for) is provisioned agent_kind='human' (was 'ai' on main)",
          agent_kind("GH-alice") == "human")
    check("a second HUMAN PR author is 'human' too", agent_kind("GH-bob") == "human")
    check("a HUMAN LANDING author (record_landing) is provisioned 'human'", agent_kind("GH-carol") == "human")
    check("a BOT author (webhook user.type=='Bot') is stamped 'ai' (free — the AI-fleet wedge)",
          agent_kind("GH-dependabotbot") == "ai")

    # ── the load-bearing seat count: meters humans, EXCLUDES bots (the measured 0→N) ────────────────────────
    sc = seat_count()
    check(f"_seat_count(30) == 3 (the 3 humans; was a STRUCTURAL 0 on origin/main) — got {sc}", sc == 3)

    active, paid_total, over_count = cost_surface_for_account()
    check(f"owner_cost_surface active_agents == 3 (the founder's seat line is no longer $0) — got {active}",
          active == 3)
    # free seat line defaults to 2 → 3 humans = 1 paid seat = AT/over the line (a conversion candidate)
    check(f"owner_cost_surface paid_seats_total == 1 (3 humans − 2 free) — got {paid_total}", paid_total == 1)
    check(f"owner_cost_surface over_seat_line_count >= 1 (the account hit the value moment) — got {over_count}",
          (over_count or 0) >= 1)
    # the bot did NOT inflate the count: 4 author rows exist, only 3 are seats
    n_authors = run(mig, "SELECT count(*)::int FROM core.agent WHERE agent_id LIKE 'GH-%%'")
    check(f"4 author agents exist but only 3 are seats — the bot is excluded (authors={n_authors})",
          n_authors == 4 and sc == 3)

    # ── SELF-HEAL: an author stored under the OLD default ('ai') recovers to 'human' on its next claim ───────
    # Simulate a legacy mis-metered author (what origin/main produced for EVERY author): insert GH-erin as 'ai'.
    run(mig, "SELECT core.mark_governed_write('agent'); INSERT INTO core.agent(agent_id,account_id,display_name,agent_kind) "
             "VALUES ('GH-erin',%s,'erin','ai') ON CONFLICT (agent_id) DO NOTHING", (ACCT,))
    check("(setup) a legacy author is stored mis-metered as 'ai'", agent_kind("GH-erin") == "ai")
    run(app, "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-erin:e.py", "e.py", REPO, BR, "erin"))
    check("SELF-HEAL: a legacy 'ai'-stamped human author is CORRECTED to 'human' on its next claim (no migration)",
          agent_kind("GH-erin") == "human")
    check("after self-heal the seat count rises to 4 (the recovered human now meters)", seat_count() == 4)

    # a bot that re-claims as a bot STAYS 'ai' (the self-heal never falsely flips a bot to human)
    run(app, "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
        ("PR-bot2:a.py", "a.py", REPO, BR, "dependabotbot", None, None, True))
    check("a bot re-claiming as a bot stays 'ai' (no false self-heal to human)",
          agent_kind("GH-dependabotbot") == "ai")
    check("the seat count is unchanged by the bot's re-claim (still 4 humans)", seat_count() == 4)

    # ── the App/seat (AG-*) agents are NEVER flipped (only 'GH-' author rows self-heal) ─────────────────────
    # provision a seat agent in THIS account, stamp it 'ai' (an App identity is an automation), then drive a
    # claim and assert the AG- row is untouched (the self-heal guard is 'GH-%' only — never the service identity).
    run(mig, "SELECT core.mark_governed_write('agent'); INSERT INTO core.agent(agent_id,account_id,display_name,agent_kind) "
             "VALUES ('AG-SVC',%s,'service','ai') ON CONFLICT (agent_id) DO NOTHING", (ACCT,))
    run(app, "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-alice:b.py", "b.py", REPO, BR, "alice"))
    check("an App/seat agent (AG-*) is NEVER flipped to 'human' by the self-heal (service identity protected)",
          agent_kind("AG-SVC") == "ai")

    ok = all(c for _, c in checks)
    print("SEAT METERING GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
