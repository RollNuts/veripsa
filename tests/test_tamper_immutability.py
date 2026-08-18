#!/usr/bin/env python3
"""TAMPER IMMUTABILITY GATE — the ledger truly cannot be erased or rewritten by ANY path except the three
named, account-pinned, un-forgeable gate erases.

Veripsa's whole product claim is a faithful, un-forgeable, APPEND-ONLY record. The append-only protection on
core.event / core.statement is enforced by `assert_append_only` / `assert_statement_immutable`, wired as
`BEFORE DELETE OR UPDATE … FOR EACH ROW` triggers. Row triggers are airtight for DELETE/UPDATE — but a row
trigger NEVER fires for TRUNCATE (a statement-level op that skips row triggers). So before this gate, a single

    TRUNCATE core.event;            -- or: TRUNCATE core.statement;

would WIPE the entire immutable ledger in ONE statement, with ZERO rows touched by the append-only trigger and
ZERO the append-only guarantee — silently defeating "a recorded fact is permanent". WHAT COULD HAVE BEEN WIPED: every
landed/push/collision_held/drift fact across all of a tenant's history (the effect ledger the product sells) AND
every stated meaning / supersede-lineage record. This gate proves the hole is CLOSED by a statement-level
`BEFORE TRUNCATE` guard (assert_no_truncate), and proves the rest of the immutability contract end-to-end:

  1. TRUNCATE on core.event / core.statement is REFUSED — for a tenant role (no privilege) AND for the table
     OWNER (the migrator, who DOES hold TRUNCATE but is stopped by the BEFORE TRUNCATE trigger). The owner is
     not a superuser and cannot flip session_replication_role, so the trigger genuinely stops it; the only
     residual is a true superuser/DBA (trusted at that level — a superuser can drop any guard regardless).
  2. COVERAGE — every permanent-FACT table (event, statement) carries BOTH the row-immutability trigger AND the
     no-truncate trigger; the LIVE/mutable tables (claim, code graph, intent, …) correctly carry neither (a
     missing trigger on a fact table would be a tamper hole; a present one on a mutable table would be a bug).
  3. The append-only ROW triggers still hold: a plain DELETE and a non-visibility UPDATE are both refused.
  4. The THREE named, account-pinned gate erases are the ONLY ways a row leaves — and each is account-pinned:
        • demo_maintenance (ACCT-DEMO-only fixture teardown),
        • retention prune (gated, account-scoped, telemetry-only),
        • account erasure (right-to-deletion, App-only, account-scoped).
     We prove EXCEPTION-TOKEN ABUSE is impossible: arming a token for account X can NEVER delete account Y's
     rows (the trigger compares the token to the row's OWN account_id), AND reaching the exception at all still
     requires the gate (an un-armed DELETE is refused; a buyer seat cannot even call the erase fn).

Run:  python3 tests/test_tamper_immutability.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run. Per-PID, exactly
# like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_account_erasure.py.
DB = "veripsa_tampertest_" + str(os.getpid())
REPO = "acme/tamper"

# the permanent-FACT tables that MUST be append-only + truncate-proof (the immutable streams). Everything else
# in core is live/mutable (claim/lock, code graph, intent, identity, saas config) and must NOT carry these.
LEDGER_TABLES = ["event", "statement"]
MUTABLE_TABLES = ["claim", "code_node", "code_edge", "graph_version", "intent",
                  "account", "agent", "policy", "store_connection", "grant", "follow", "webhook_delivery",
                  "workspace", "workspace_member"]   # consent rows flip state (accept/revoke) → mutable, NOT append-only


def conn_for(role):
    return psycopg2.connect(f"postgresql://{role}@localhost/{DB}")


def run_as(role, sql, args=(), fetch=True, pin_account=None):
    conn = conn_for(role)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            if pin_account is not None:
                cur.execute("SELECT set_config('core.current_account', %s, true)", (pin_account,))
            cur.execute(sql, args)
            if fetch:
                row = cur.fetchone()
                return row[0] if row else None
            return None
    finally:
        conn.close()


def expect_refused(role, sql, args=(), pin_account=None):
    """Run `sql` and return the error string if it was REFUSED, else None (= it unexpectedly SUCCEEDED)."""
    conn = conn_for(role)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            if pin_account is not None:
                cur.execute("SELECT set_config('core.current_account', %s, true)", (pin_account,))
            cur.execute(sql, args)
        conn.commit()
        return None
    except psycopg2.Error as e:
        conn.rollback()
        return str(e)
    finally:
        conn.close()


def owner_count(account, sql_tail, args=()):
    """Owner-read with RLS pinned to `account` (FORCE RLS still walls the owner to that account)."""
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cur.execute(sql_tail, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def seed_ledger(account, repo, ev_prefix):
    """Seed ONE immutable event + ONE statement for `account` via the gate (the only legit write path)."""
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cur.execute("SELECT core.mark_governed_write('event')")
            cur.execute(
                "INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path) "
                "VALUES (%s,%s,'landed','AG-X',%s,'main','a.py') ON CONFLICT DO NOTHING",
                (ev_prefix + "-EV", account, repo))
            cur.execute("SELECT core.mark_governed_write('statement')")
            cur.execute(
                "INSERT INTO core.statement(statement_id,account_id,agent_id,utterance,about_repo,about_branch,about_path) "
                "VALUES (%s,%s,'AG-X','owns auth',%s,'main','a.py') ON CONFLICT DO NOTHING",
                (ev_prefix + "-ST", account, repo))
    finally:
        conn.close()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    checks = []

    # ── COVERAGE: enumerate triggers from the catalog. Every LEDGER table must carry BOTH the row-immutability
    # trigger (assert_append_only/assert_statement_immutable) AND the no-truncate trigger (assert_no_truncate).
    # Every MUTABLE table must carry NEITHER. A drift either way is a hole, so we assert the WHOLE map.
    trg = {}
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT c.relname, p.proname, (t.tgtype & 32) <> 0 AS is_truncate
                FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_proc p ON p.oid=t.tgfoid
                WHERE c.relnamespace='core'::regnamespace AND NOT t.tgisinternal
                  AND p.proname IN ('assert_append_only','assert_statement_immutable','assert_no_truncate')""")
            for relname, proname, is_truncate in cur.fetchall():
                trg.setdefault(relname, {"row_immut": False, "no_truncate": False})
                if proname == "assert_no_truncate":
                    trg[relname]["no_truncate"] = True
                else:
                    trg[relname]["row_immut"] = True
    finally:
        conn.close()

    for t in LEDGER_TABLES:
        st = trg.get(t, {})
        checks.append((f"coverage: core.{t} (a permanent-fact table) carries the ROW-immutability trigger",
                       st.get("row_immut") is True))
        checks.append((f"coverage: core.{t} (a permanent-fact table) carries the NO-TRUNCATE trigger (the fix)",
                       st.get("no_truncate") is True))
    # the immutable streams are EXACTLY these two — no fact table is silently unprotected, and no mutable table
    # wrongly wears the immutability triggers.
    checks.append((f"coverage: the no-truncate guard is on EXACTLY the ledger tables {sorted(LEDGER_TABLES)} "
                   f"(none missing, none stray): {sorted(k for k,v in trg.items() if v['no_truncate'])}",
                   sorted(k for k, v in trg.items() if v["no_truncate"]) == sorted(LEDGER_TABLES)))
    for t in MUTABLE_TABLES:
        st = trg.get(t, {})
        checks.append((f"coverage: live/mutable core.{t} correctly carries NEITHER immutability trigger",
                       not st.get("row_immut") and not st.get("no_truncate")))

    # ── PRECONDITION: the owner (migrator) is NOT a superuser and cannot flip session_replication_role to
    # 'replica' — both would let a caller skip triggers. This is WHY a BEFORE TRUNCATE trigger genuinely stops
    # even the table owner (the only role that holds the TRUNCATE privilege).
    is_super = owner_count("ACCT-DEMO", "SELECT rolsuper FROM pg_roles WHERE rolname='veripsa_migrator'")
    checks.append(("precondition: veripsa_migrator is NOT a superuser (so a BEFORE TRUNCATE trigger binds it)",
                   is_super is False))
    err_repl = expect_refused("veripsa_migrator", "SET session_replication_role = 'replica'")
    checks.append((f"precondition: the owner cannot SET session_replication_role='replica' to skip triggers: "
                   f"{(err_repl or 'NOT REFUSED')[:50]}", err_repl is not None))

    # ── PRIVILEGE: no TENANT role holds TRUNCATE on the ledger; only the owner does (and the trigger stops it).
    for role in ("veripsa_writer", "veripsa_app", "veripsa_demo_agent", "veripsa_demo_steward", "veripsa_reader"):
        has = owner_count("ACCT-DEMO",
                          "SELECT has_table_privilege(%s,'core.event','TRUNCATE') OR has_table_privilege(%s,'core.statement','TRUNCATE')",
                          (role, role))
        checks.append((f"privilege: tenant role {role} holds NO TRUNCATE on the ledger tables", has is False))
    owner_has = owner_count("ACCT-DEMO", "SELECT has_table_privilege('veripsa_migrator','core.event','TRUNCATE')")
    checks.append(("privilege: only the OWNER (migrator) holds TRUNCATE (documented residual — stopped by the trigger)",
                   owner_has is True))

    # seed both ledgers for ACCT-DEMO and a neighbour ACCT-ACME (proves cross-tenant safety of every exception).
    seed_ledger("ACCT-DEMO", REPO, "D")
    run_as("veripsa_migrator", "SELECT 1", pin_account=None)  # no-op; keep connections tidy
    # provision the neighbour account + a writer credential so we can seed it.
    run_as("veripsa_migrator",
           "SELECT core.provision_seat('ACCT-ACME','Acme Inc','AG-ACME','acme','veripsa_acme_agent')")
    seed_ledger("ACCT-ACME", "acme/x", "A")

    base_demo_ev = owner_count("ACCT-DEMO", "SELECT count(*) FROM core.event WHERE account_id='ACCT-DEMO'")
    base_demo_st = owner_count("ACCT-DEMO", "SELECT count(*) FROM core.statement WHERE account_id='ACCT-DEMO'")
    checks.append((f"seed: ACCT-DEMO has an immutable footprint (event={base_demo_ev}, statement={base_demo_st})",
                   base_demo_ev == 1 and base_demo_st == 1))

    # ── (1) THE FIX — TRUNCATE is refused on BOTH ledgers, for a TENANT role (no privilege) AND the OWNER (the
    # trigger). After every attempt the rows are still present (nothing wiped).
    for tbl in LEDGER_TABLES:
        err_t_tenant = expect_refused("veripsa_demo_agent", f"TRUNCATE core.{tbl}")
        checks.append((f"TRUNCATE core.{tbl} by a TENANT seat is refused: {(err_t_tenant or 'NOT REFUSED')[:60]}",
                       err_t_tenant is not None))
        err_t_owner = expect_refused("veripsa_migrator", f"TRUNCATE core.{tbl}")
        checks.append((f"TRUNCATE core.{tbl} by the OWNER is refused by the BEFORE TRUNCATE trigger: {(err_t_owner or 'NOT REFUSED')[:60]}",
                       err_t_owner is not None and "append-only" in err_t_owner))
    # also prove the multi-table form (TRUNCATE a,b) and TRUNCATE … CASCADE are both stopped — no shotgun wipe.
    err_multi = expect_refused("veripsa_migrator", "TRUNCATE core.event, core.statement")
    checks.append((f"TRUNCATE core.event, core.statement (multi-table) is refused: {(err_multi or 'NOT REFUSED')[:50]}",
                   err_multi is not None and "append-only" in err_multi))
    err_casc = expect_refused("veripsa_migrator", "TRUNCATE core.event CASCADE")
    checks.append((f"TRUNCATE core.event CASCADE is refused: {(err_casc or 'NOT REFUSED')[:50]}",
                   err_casc is not None and "append-only" in err_casc))
    # nothing was wiped by any of the above.
    after_demo_ev = owner_count("ACCT-DEMO", "SELECT count(*) FROM core.event WHERE account_id='ACCT-DEMO'")
    after_demo_st = owner_count("ACCT-DEMO", "SELECT count(*) FROM core.statement WHERE account_id='ACCT-DEMO'")
    checks.append((f"the ledger is INTACT after every TRUNCATE attempt (event={after_demo_ev}, statement={after_demo_st})",
                   after_demo_ev == 1 and after_demo_st == 1))

    # ── (2) THE ROW TRIGGERS STILL HOLD — a plain DELETE and a content UPDATE are refused on both ledgers.
    err_del_ev = expect_refused("veripsa_migrator", "DELETE FROM core.event WHERE account_id='ACCT-DEMO'",
                                pin_account="ACCT-DEMO")
    checks.append((f"plain DELETE on core.event is refused (append-only row trigger): {(err_del_ev or 'NOT REFUSED')[:50]}",
                   err_del_ev is not None and "append-only" in err_del_ev))
    err_del_st = expect_refused("veripsa_migrator", "DELETE FROM core.statement WHERE account_id='ACCT-DEMO'",
                                pin_account="ACCT-DEMO")
    checks.append((f"plain DELETE on core.statement is refused (immutable row trigger): {(err_del_st or 'NOT REFUSED')[:50]}",
                   err_del_st is not None and "append-only" in err_del_st))
    err_upd = expect_refused("veripsa_migrator",
                             "UPDATE core.event SET kind='tampered' WHERE account_id='ACCT-DEMO'",
                             pin_account="ACCT-DEMO")
    checks.append((f"a content UPDATE on core.event is refused (only visibility may change): {(err_upd or 'NOT REFUSED')[:50]}",
                   err_upd is not None and "append-only" in err_upd))

    # ── (3) EXCEPTION-TOKEN ABUSE is impossible. Arm the RETENTION token for ACCT-ACME, then try to DELETE
    # ACCT-DEMO's rows in the SAME transaction (cross-account abuse). The trigger compares the token to the
    # row's OWN account_id, so the foreign rows are refused — a token for X can never erase Y. (Run pinned to
    # ACCT-DEMO so RLS admits the rows → only the trigger can refuse them.)
    cross = expect_refused(
        "veripsa_migrator",
        "SELECT core.mark_retention_prune('ACCT-ACME'); DELETE FROM core.event WHERE account_id='ACCT-DEMO'",
        pin_account="ACCT-DEMO")
    checks.append((f"exception-token abuse: a retention token for ACCT-ACME canNOT delete ACCT-DEMO's rows: {(cross or 'NOT REFUSED')[:50]}",
                   cross is not None and "append-only" in cross))
    # same for the ERASURE token: armed for ACCT-ACME, it cannot reach ACCT-DEMO's statements.
    cross_er = expect_refused(
        "veripsa_migrator",
        "SELECT core.mark_account_erasure('ACCT-ACME'); DELETE FROM core.statement WHERE account_id='ACCT-DEMO'",
        pin_account="ACCT-DEMO")
    checks.append((f"exception-token abuse: an erasure token for ACCT-ACME canNOT delete ACCT-DEMO's statements: {(cross_er or 'NOT REFUSED')[:50]}",
                   cross_er is not None and "append-only" in cross_er))
    # the demo_maintenance bypass is ACCT-DEMO-PINNED in BOTH dimensions: armed with ACCT-ACME (a non-demo
    # account) it does nothing (the bypass also requires account = 'ACCT-DEMO'), so the DELETE is refused.
    cross_demo = expect_refused(
        "veripsa_migrator",
        "SELECT set_config('core.demo_maintenance_token','ACCT-ACME',true); DELETE FROM core.event WHERE account_id='ACCT-ACME'",
        pin_account="ACCT-ACME")
    checks.append((f"the demo bypass is ACCT-DEMO-only: armed for ACCT-ACME it does NOT erase ACCT-ACME: {(cross_demo or 'NOT REFUSED')[:50]}",
                   cross_demo is not None and "append-only" in cross_demo))
    # nothing the abuse attempts touched got deleted.
    checks.append(("no exception-token abuse deleted anything (ACCT-DEMO + ACCT-ACME ledgers intact)",
                   owner_count("ACCT-DEMO", "SELECT count(*) FROM core.event WHERE account_id='ACCT-DEMO'") == 1
                   and owner_count("ACCT-ACME", "SELECT count(*) FROM core.event WHERE account_id='ACCT-ACME'") == 1))

    # ── (4) REACHING the exception requires the GATE — a buyer seat cannot even call the erase fn, and an
    # un-armed DELETE is refused (already shown in (2)). App-delegation only.
    err_seat = expect_refused("veripsa_demo_agent", "SELECT core.erase_account_with_authority()")
    checks.append((f"a buyer seat cannot call erase_account_with_authority (App-delegation only): {(err_seat or 'NOT REFUSED')[:50]}",
                   err_seat is not None))

    # ── (5) THE THREE NAMED ERASES STILL WORK (so the truncate guard didn't break any sanctioned deletion).
    # (5a) RETENTION prune — the gated, account-scoped, telemetry-only row-DELETE removes the 'landed' event for
    # ACCT-DEMO but leaves the statement record immutable.
    pruned = run_as("veripsa_demo_steward", "SELECT core.prune_events_with_authority(now()+interval '1 day')",
                    pin_account=None)
    pruned = pruned if isinstance(pruned, dict) else __import__("json").loads(pruned)
    checks.append((f"retention prune (a sanctioned row-DELETE via the gate) still works (pruned={pruned.get('pruned')})",
                   bool(pruned.get("ok")) and pruned.get("pruned", 0) >= 1))
    checks.append(("retention prune erased the operational event but left the statement record immutable",
                   owner_count("ACCT-DEMO", "SELECT count(*) FROM core.event WHERE account_id='ACCT-DEMO'") == 0
                   and owner_count("ACCT-DEMO", "SELECT count(*) FROM core.statement WHERE account_id='ACCT-DEMO'") == 1))
    # (5b) ACCOUNT ERASURE — the App runs the full account hard-delete in its own (ACCT-DEMO) context; the
    # immutable statement stream for ACCT-DEMO goes via the account-pinned erasure token (not TRUNCATE).
    er = run_as("veripsa_app", "SELECT core.erase_account_with_authority()", pin_account=None)
    er = er if isinstance(er, dict) else __import__("json").loads(er)
    checks.append((f"account erasure (the right-to-deletion row-DELETE via the gate) still works (ok={er.get('ok')})",
                   bool(er.get("ok")) and er.get("account") == "ACCT-DEMO"))
    checks.append(("account erasure cleared ACCT-DEMO's immutable statement stream (account-pinned, not TRUNCATE)",
                   owner_count("ACCT-DEMO", "SELECT count(*) FROM core.statement WHERE account_id='ACCT-DEMO'") == 0))
    # (5c) the NEIGHBOUR is untouched + STILL truncate-proof + append-only after all of the above.
    checks.append(("the neighbour ACCT-ACME's ledger is untouched by every erase above",
                   owner_count("ACCT-ACME", "SELECT count(*) FROM core.event WHERE account_id='ACCT-ACME'") == 1
                   and owner_count("ACCT-ACME", "SELECT count(*) FROM core.statement WHERE account_id='ACCT-ACME'") == 1))
    err_after_trunc = expect_refused("veripsa_migrator", "TRUNCATE core.event")
    checks.append((f"the ledger is STILL truncate-proof after the sanctioned erases ran: {(err_after_trunc or 'NOT REFUSED')[:50]}",
                   err_after_trunc is not None and "append-only" in err_after_trunc))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("TAMPER IMMUTABILITY GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
