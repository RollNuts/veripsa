#!/usr/bin/env python3
"""CO-CHANGE OWNER gate — the managed-Postgres deploy path must leave core.co_change / core.co_change_seen_commit
OWNED BY veripsa_migrator, so the SECURITY DEFINER fns that touch them (uninstall purge / GDPR erase / the
co-change 2nd-detector read+write) actually FUNCTION. This is the gate the existing suite STRUCTURALLY lacked.

THE BUG THIS LOCKS (verified HIGH, audit #328-owner):
Every other core table reassigns its owner right after CREATE — 20_core.sql does
`ALTER TABLE core.<t> OWNER TO veripsa_migrator` for claim/code_node/code_edge/graph_version/event/intent. The
two co-change tables (85_cochange.sql) were the ONLY core tables that did NOT. That is invisible on the LOCAL
path because bootstrap_local.sh / run_gates apply the schema AS veripsa_migrator directly, so the table's owner
(= the CREATE-ing role) is already veripsa_migrator and the missing ALTER is a no-op. But the MANAGED-Postgres
path is different: the operator does `GRANT veripsa_migrator TO <db-owner>` and applies the schema AS <db-owner>
(a MEMBER of veripsa_migrator) — OUR Render prod, per render.yaml. Postgres makes a freshly-CREATEd table owned
by the CURRENT role (<db-owner>), NOT the role it is a member of. So co_change ends up owned by <db-owner> while
the 5 SECURITY DEFINER fns run AS veripsa_migrator (ALTER FUNCTION … OWNER TO veripsa_migrator) → every one of
them hits `permission denied for table co_change`:
  - purge_account_working_set_with_authority (uninstall) → silently no-ops  → the "we purge on uninstall"
    privacy claim is BROKEN (over-retention of the customer's private file paths).
  - erase_account_with_authority (GDPR Art.17 / CCPA) → RAISES → a "delete ALL my data" request FAILS.
  - ingest_cochange_with_authority + co_change_filter_unseen_commits_with_authority → co-change never persists.
  - co_change_partners_with_authority + co_change_all_with_authority → reads fail → the 2nd detector is DEAD,
    SILENTLY (these run fail-open in the App, so the customer just never sees the logical-coupling signal).

THE FIX (85_cochange.sql): an `ALTER TABLE … OWNER TO veripsa_migrator` immediately after each CREATE TABLE,
mirroring the exact 20_core.sql pattern. Idempotent + migration-safe: a member CAN reassign a table to a role it
is a member of, AND re-applying the schema REASSIGNS an already-mis-owned table — so a PO-gated prod REDEPLOY of
the fixed schema repairs the LIVE managed deploy in place.

MEASURE-FIRST: this gate applies the schema AS A veripsa_migrator-MEMBER owner (NOT veripsa_migrator — that is
the whole point; applying as veripsa_migrator would mask the bug exactly as the rest of the suite does), then
asserts the AFTER-fix reality:
  (1) co_change + co_change_seen_commit are owned by veripsa_migrator (the fix);
  (2) the DEFINER fns FUNCTION as the App: ingest + read-back + filter-unseen all work (co-change alive);
  (3) purge_account_working_set returns ok:true AND actually DELETES the co_change rows (privacy honored);
  (4) erase_account returns ok:true AND its deletion receipt counts the co_change rows deleted (GDPR complete);
  (5) the moat is intact — a NON-App raw INSERT into co_change is still refused by the forgery gate (owning the
      table did not weaken the governed-write wall).
Content-free throughout (paths + counts only). Marker: "COCHANGE OWNER GATE: PASS".

Run:  python3 tests/test_cochange_owner.py   (needs local Postgres with the veripsa roles; uses the ephemeral
cluster env exported by run_gates / db/_ephemeral_pg.sh when run under the gate suite).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
from _lifecycle_fixture import absent_installation_proof, seed_processing_uninstall  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate creates + drops this DB and a per-run member-owner role, so a FIXED
# name lets concurrent runs (parallel CI shards / several agents each running run_gates) drop each other's DB /
# role mid-run. Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), the other
# tests/*.py. The member-owner role is ALSO per-PID so two concurrent runs never share/ALTER one another's role
# (roles are cluster-global; a shared name would race on pg_authid exactly like db/roles.sql warns about).
PID = str(os.getpid())
DB = "veripsa_ccownertest_" + PID
OWNER = "veripsa_ccdeploy_" + PID   # the MANAGED-PG db-owner: a MEMBER of veripsa_migrator, NOT veripsa_migrator
REPO = "acme/ccowner"


def _psql(dsn, *args):
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", *args],
                       cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or "")[-800:]


def admin_dsn():
    # the ephemeral cluster exports ADMIN_DSN (OS-superuser @ the maintenance db over trust auth). Fall back to
    # the ambient local postmaster's maintenance db when run standalone (VERIPSA_EPHEMERAL_PG=0 / direct invoke).
    return os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")


def app_conn():
    """A connection AS veripsa_app, having ENTERED the demo installation (the live per-event shape)."""
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", ("inst-ccowner",))
    return conn


def app_call(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", ("inst-ccowner",))
        cur.execute(sql, args)
        row = cur.fetchone()
        out = row[0] if row else None
        return out if isinstance(out, (dict, list)) or out is None else json.loads(out)


def owner_of(table):
    conn = psycopg2.connect(f"postgresql://{OWNER}@localhost/{DB}"); conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='core' AND c.relname=%s", (table,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def cochange_rowcount(account):
    """Owner-read of core.co_change for `account` (FORCE RLS walls even the owner to the pinned account). The
    set_config is TXN-LOCAL (is_local=true), so the pin + the count MUST run in ONE transaction — under
    autocommit the pin would be discarded before the count and FORCE RLS would then hide every row (read 0)."""
    conn = psycopg2.connect(f"postgresql://{OWNER}@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:   # `with conn` = one transaction (committed/rolled-back on exit)
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cur.execute("SELECT count(*) FROM core.co_change WHERE account_id=%s", (account,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def seed_activation_delivery(key):
    conn = psycopg2.connect(f"postgresql://{OWNER}@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "INSERT INTO core.webhook_delivery(delivery_key,event_type,account_key,payload,status,received_at) "
                "VALUES (%s,'installation','ACCT-DEMO',%s::jsonb,'processing',clock_timestamp())",
                (key, json.dumps({
                    "action": "created",
                    "installation": {"id": "B-DEMO", "account": {"id": "ACCT-DEMO"}},
                })),
            )
    finally:
        conn.close()


def expect_refused(role, sql, args=(), pin_account=None):
    conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
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


def main() -> int:
    admin = admin_dsn()

    # ── STAND UP THE MANAGED-PG SHAPE. roles.sql (idempotent), then a per-PID member-owner role with
    # `GRANT veripsa_migrator TO <owner>`, then createdb -O <owner> and apply the schema AS <owner>. This is the
    # render.yaml managed path — and the WHOLE point: applying as veripsa_migrator (what bootstrap_local does)
    # would make the table owner already-correct and HIDE the bug, which is why the rest of the suite missed it.
    ok, err = _psql(admin, "-f", "db/roles.sql")
    if not ok:
        print("roles.sql failed:\n", err); return 1
    ok, err = _psql(admin, "-c", f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{OWNER}') THEN CREATE ROLE {OWNER} LOGIN;
          ELSE ALTER ROLE {OWNER} LOGIN; END IF;
        END $$;
        GRANT veripsa_migrator TO {OWNER};""")
    if not ok:
        print("member-owner role setup failed:\n", err); return 1

    subprocess.run(["dropdb", DB], capture_output=True)
    cr = subprocess.run(["createdb", DB, "-O", OWNER], capture_output=True, text=True)
    if cr.returncode != 0:
        print("createdb -O member-owner failed:\n", cr.stderr); return 1
    # apply the schema AS THE MEMBER-OWNER (not veripsa_migrator) — the bug-reproducing condition.
    ok, err = _psql(f"postgresql://{OWNER}@localhost/{DB}", "-f", "db/schema.sql")
    if not ok:
        print("schema.sql failed (applied as member-owner):\n", err); return 1

    # provision the App service identity → ACCT-DEMO + register its installation, so the App resolves a tenant for
    # the ingest/purge/erase calls below (exactly the seam bootstrap_local + the deploy gate set up).
    ok, err = _psql(f"postgresql://{OWNER}@localhost/{DB}", "-c", """SET search_path=core;
        SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-APP','Veripsa App','veripsa_app');
        INSERT INTO core.installation_account(installation_id, account_id)
          VALUES ('inst-ccowner','ACCT-DEMO') ON CONFLICT (installation_id) DO NOTHING;""")
    if not ok:
        print("provision/installation seed failed:\n", err); return 1

    checks = []

    # ── (1) THE FIX: both co-change tables are owned by veripsa_migrator even though a MEMBER applied the schema.
    own_cc = owner_of("co_change")
    own_seen = owner_of("co_change_seen_commit")
    checks.append((f"core.co_change is owned by veripsa_migrator (applied by member '{OWNER}'; got '{own_cc}')",
                   own_cc == "veripsa_migrator"))
    checks.append((f"core.co_change_seen_commit is owned by veripsa_migrator (got '{own_seen}')",
                   own_seen == "veripsa_migrator"))
    # CONTROL: a table that ALWAYS had the OWNER line (claim) is veripsa_migrator too — proves the harness itself
    # is sound (the member-owner apply does the right thing for the already-fixed tables, isolating the co_change fix).
    checks.append(("control: core.claim (already had the OWNER line) is owned by veripsa_migrator",
                   owner_of("claim") == "veripsa_migrator"))

    # ── (2) THE 2ND DETECTOR FUNCTIONS as the App: write + read-back + filter-unseen all succeed (no permission
    # denied). Before the fix every one of these RAISED `permission denied for table co_change[_seen_commit]`.
    app = app_conn()
    try:
        ingested = app_call(app, "SELECT core.ingest_cochange_with_authority(%s::jsonb, %s)",
                            (json.dumps([{"a": "x.py", "b": "y.py", "co": 7, "n_a": 8, "n_b": 9,
                                          "strength": 0.8, "lift": 3, "n_total": 50}]), REPO))
        checks.append((f"ingest_cochange works (ok={ (ingested or {}).get('ok') }, pairs={ (ingested or {}).get('pairs') })",
                       bool(ingested) and ingested.get("ok") is True and ingested.get("pairs") == 1))
        read_all = app_call(app, "SELECT core.co_change_all_with_authority(%s)", (REPO,))
        checks.append((f"co_change_all read-back returns the stored pair (rows={ len(read_all or []) })",
                       isinstance(read_all, list) and len(read_all) == 1
                       and read_all[0].get("a") == "x.py" and read_all[0].get("b") == "y.py"))
        partners = app_call(app, "SELECT core.co_change_partners_with_authority(%s, %s)",
                            (REPO, ["x.py"]))
        checks.append((f"co_change_partners read works (returns a list, len={ len(partners or []) })",
                       isinstance(partners, list)))
        unseen = app_call(app, "SELECT core.co_change_filter_unseen_commits_with_authority(%s, %s)",
                          (REPO, ["abc123", "def456"]))
        checks.append((f"co_change_filter_unseen_commits records + returns the new shas (got { unseen })",
                       isinstance(unseen, list) and sorted(unseen) == ["abc123", "def456"]))
        # idempotent ledger really persisted (rows landed in co_change_seen_commit, owned-table write succeeded).
        again = app_call(app, "SELECT core.co_change_filter_unseen_commits_with_authority(%s, %s)",
                         (REPO, ["abc123", "def456"]))
        checks.append((f"a re-delivered push is a true no-op (sha-dedupe persisted; got { again })",
                       again == []))

        # ── (3) UNINSTALL PURGE actually forgets the co-change working set (privacy claim honored, not a silent
        # no-op). co_change has a row now (from the ingest), so a working purge must DELETE it and report ok:true.
        before_rows = cochange_rowcount("ACCT-DEMO")
        uninstall_key = "ccowner-uninstall"
        deleted_installation_id = "A-DEMO"
        seed_processing_uninstall(
            f"postgresql://{OWNER}@localhost/{DB}",
            uninstall_key,
            "ACCT-DEMO",
            deleted_installation_id,
        )
        purge = app_call(
            app,
            "WITH delivery_context AS MATERIALIZED ("
            " SELECT set_config('core.current_delivery_key',%s,true)"
            ") SELECT core.purge_account_working_set_with_authority(%s::jsonb)"
            " FROM delivery_context",
            (
                uninstall_key,
                json.dumps(absent_installation_proof("ACCT-DEMO", deleted_installation_id)),
            ),
        )
        after_rows = cochange_rowcount("ACCT-DEMO")
        purged = (purge or {}).get("purged", {})
        checks.append((f"purge_account_working_set returns ok:true (got ok={ (purge or {}).get('ok') })",
                       bool(purge) and purge.get("ok") is True))
        checks.append((f"the uninstall purge DELETES the co_change rows (before={before_rows}, after={after_rows}, "
                       f"manifest cochange={purged.get('cochange')}, cochange_seen={purged.get('cochange_seen')})",
                       before_rows >= 1 and after_rows == 0
                       and purged.get("cochange") == before_rows and purged.get("cochange_seen") >= 1))
    finally:
        app.close()

    # ── (4) GDPR ERASE succeeds AND its deletion receipt counts the co_change rows. Re-ingest a row first (the
    # purge above already cleared it), so the erase has something to delete and the receipt is non-zero.
    app2 = app_conn()
    try:
        # section 3's purge tombstoned ACCT-DEMO (the small-findings co-change/uninstall race fix: a co-change write
        # on a tombstoned account is now refused IN-TXN). A GDPR erase is requested on a LIVE account, so REACTIVATE
        # first (the genuine-reinstall step that clears the tombstone) before re-ingesting the row the erase deletes.
        activation_key = "ccowner-reinstall"
        seed_activation_delivery(activation_key)
        activation_proof = json.dumps({"installation_id": "B-DEMO", "account_id": "ACCT-DEMO",
                                       "created_at": "2099-01-01T00:00:00Z", "suspended": False})
        app_call(app2, "SELECT core.reactivate_account_with_authority(%s,%s::jsonb)",
                 (activation_key, activation_proof))
        app_call(app2, "SELECT core.ingest_cochange_with_authority(%s::jsonb, %s)",
                 (json.dumps([{"a": "x.py", "b": "y.py", "co": 7, "n_a": 8, "n_b": 9,
                               "strength": 0.8, "lift": 3, "n_total": 50}]), REPO))
        cc_before_erase = cochange_rowcount("ACCT-DEMO")
        erase = app_call(app2, "SELECT core.erase_account_with_authority()")
        erased = (erase or {}).get("erased", {})
        checks.append((f"erase_account returns ok:true (no permission-denied RAISE; got ok={ (erase or {}).get('ok') })",
                       bool(erase) and erase.get("ok") is True and erase.get("account") == "ACCT-DEMO"))
        checks.append((f"the GDPR erase receipt counts the co_change rows deleted (before={cc_before_erase}, "
                       f"receipt co_change={erased.get('co_change')})",
                       cc_before_erase >= 1 and erased.get("co_change") == cc_before_erase))
    finally:
        app2.close()

    # ── (5) MOAT INTACT: owning the table did NOT weaken the governed-write wall. A NON-App raw INSERT into
    # co_change (no forgery token armed) is STILL refused by trg_governed_co_change. Pin the account so RLS admits
    # the row → only the forgery trigger can refuse it (proves the wall, not just RLS). veripsa_demo_agent is a
    # LOGIN fixture that INHERITS veripsa_writer (the base write capability) — a genuine non-App writer.
    err_forge = expect_refused("veripsa_demo_agent",
                               "INSERT INTO core.co_change(account_id,repo,path_a,path_b,co,n_a,n_b,strength,lift,n_total) "
                               "VALUES ('ACCT-DEMO',%s,'a.py','b.py',1,1,1,1,1,1)",
                               args=(REPO,), pin_account="ACCT-DEMO")
    checks.append((f"a non-App raw INSERT into co_change is STILL forgery-refused (got: {(err_forge or 'NOT REFUSED')[:70]})",
                   err_forge is not None))

    ok_all = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok_all = ok_all and bool(cond)
    print("COCHANGE OWNER GATE:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        # drop the per-PID DB AND the per-PID member-owner role (cluster-global; clean up so a re-run is fresh).
        subprocess.run(["dropdb", DB], capture_output=True)
        subprocess.run(["psql", admin_dsn(), "-q", "-c", f"DROP ROLE IF EXISTS {OWNER}"], capture_output=True)
