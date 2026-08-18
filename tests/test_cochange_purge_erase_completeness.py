#!/usr/bin/env python3
"""CO-CHANGE PURGE/ERASE COMPLETENESS regression gate.

The resolved data-rights contract is strict: both the uninstall working-set purge and the right-to-erasure hard
delete must remove the tenant's content-free co_change paths and co_change_seen_commit ledger, report matching
deletion counts, and leave a second tenant untouched. A recurrence is a hard failure, never a documented-pass
branch. The code-graph, durable webhook inbox, uninstall-lifecycle fence, and account-key enumeration checks below
remain part of the same complete tenant-forgetting proof. Uninstall retains its active fence; explicit GDPR erasure
deletes that account-keyed marker as well as the tenant data.

EXTENDED (audit P1 — durable webhook inbox). The SAME completeness contract now covers core.webhook_delivery,
a NEW account_key-bearing table the purge+erase originally missed (it carries account_key + the private-repo
full_name on every row, retained even on a 'done' row after the payload is cleared to {}). STRICT here: both the
uninstall purge AND the GDPR hard-delete erase forget the tenant's webhook rows and REPORT the count in their
manifest; the account_key KEY-FORMAT FOOTGUN is proven END-TO-END — the stored key is the BARE GitHub owner id
(e.g. '9999', server's _event_account_key returns repository.owner.id verbatim) while the erase pins
'ACCT-GH-9999', so the DELETE must STRIP the 'ACCT-GH-' prefix (substr(v_account,9)) to match, and a naive
account_key=v_account would silent-false zero-match. Plus an ENUMERATION guard: every core table with an
account_key column must be referenced by erase_account_with_authority, so the NEXT such table cannot silently
reopen the gap. Tenant-scope stays strict (the neighbour's webhook row is untouched).

Run:  python3 tests/test_cochange_purge_erase_completeness.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe), like db/smoke.sh / run_gates / the other lifecycle tests.
DB = "veripsa_cochangepurge_" + str(os.getpid())
REPO = "acme/cc"
checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def conn_for(role):
    return psycopg2.connect(f"postgresql://{role}@localhost/{DB}")


def run_as(role, sql, args=()):
    conn = conn_for(role)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            try:
                row = cur.fetchone()
            except psycopg2.ProgrammingError:
                return None
            return row[0] if row else None
    finally:
        conn.close()


def run_as_delivery(role, delivery_key, sql, args=()):
    """Execute a lifecycle gate in the same session that carries its exact durable delivery authority."""
    conn = conn_for(role)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_delivery_key',%s,false)", (delivery_key,))
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def owner_count(account, sql_tail, args=()):
    """Owner read with RLS pinned to `account` (FORCE RLS still walls the owner to exactly that account)."""
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


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    # ── Provision TWO tenants. ACCT-DEMO = the one we PURGE then ERASE. ACCT-ACME = the retained neighbour whose
    # co_change data must be entirely UNTOUCHED (tenant-scope: never an over-broad cross-tenant delete). Seed each
    # tenant's co_change + co_change_seen_commit + a code-graph row through the GATED ingest fns (the real write
    # path). co_change carries real PATHS (path_a/path_b) = the per-tenant private-repo file structure.
    seed = """
    SET search_path=core;
    SELECT core.provision_seat('ACCT-DEMO','Demo Inc','AG-DEMO','demo-writer','veripsa_demo_agent');
    SELECT core.provision_seat('ACCT-ACME','Acme Inc','AG-ACME','acme-writer','veripsa_acme_agent');
    INSERT INTO core.installation_account(installation_id, account_id) VALUES
      ('inst-demo','ACCT-DEMO'),('inst-acme','ACCT-ACME') ON CONFLICT (installation_id) DO NOTHING;

    -- ACCT-DEMO working set: a code-graph node (so the existing-invariant assertions have something to purge) +
    -- two co_change pairs (real file paths) + two folded commit shas.
    SELECT set_config('core.current_account','ACCT-DEMO', true);
    SELECT core.mark_governed_write('code_node');
    INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path) VALUES
      ('ACCT-DEMO',%(repo)s,'main','N1','file','app/auth.py');
    SELECT core.mark_governed_write('graph_version');
    INSERT INTO core.graph_version(account_id,repo,branch,node_count,edge_count) VALUES ('ACCT-DEMO',%(repo)s,'main',1,0);
    SELECT core.mark_governed_write('co_change');
    INSERT INTO core.co_change(account_id,repo,path_a,path_b,co,n_a,n_b,strength,lift,n_total) VALUES
      ('ACCT-DEMO',%(repo)s,'app/auth.py','app/session.py',8,10,9,0.8,3.2,40),
      ('ACCT-DEMO',%(repo)s,'app/billing.py','app/ledger.py',5,6,7,0.7,2.1,40);
    SELECT core.mark_governed_write('co_change_seen_commit');
    INSERT INTO core.co_change_seen_commit(account_id,repo,commit_sha) VALUES
      ('ACCT-DEMO',%(repo)s,'aa11'),('ACCT-DEMO',%(repo)s,'bb22');

    -- DURABLE WEBHOOK INBOX rows (audit P1 — the table the erase/purge originally MISSED). It carries account_key
    -- (here the FULL account id form 'ACCT-DEMO', matching v_account directly — the prefix-strip path is exercised
    -- in its own block at the end) + repo (private-repo full_name) on every row, retained even on a DONE row after
    -- the payload is cleared to {}. core.webhook_delivery has NO per-account RLS (cross-tenant, App-written), so
    -- the migrator seeds it directly. Seed BOTH a 'done' and a 'failed' DEMO row + one for the neighbour ACME.
    INSERT INTO core.webhook_delivery(delivery_key,event_type,account_key,repo,payload,status,done_at) VALUES
      ('wd-demo-done','push','ACCT-DEMO',%(repo)s,'{}'::jsonb,'done', now()),
      ('wd-demo-failed','push','ACCT-DEMO',%(repo)s,'{}'::jsonb,'failed', NULL);

    -- ACCT-ACME working set (the retained neighbour): its OWN co_change row must survive both operations.
    SELECT set_config('core.current_account','ACCT-ACME', true);
    SELECT core.mark_governed_write('co_change');
    INSERT INTO core.co_change(account_id,repo,path_a,path_b,co,n_a,n_b,strength,lift,n_total) VALUES
      ('ACCT-ACME','acme/x','svc/a.py','svc/b.py',4,5,6,0.6,1.9,30);
    SELECT core.mark_governed_write('co_change_seen_commit');
    INSERT INTO core.co_change_seen_commit(account_id,repo,commit_sha) VALUES ('ACCT-ACME','acme/x','cc33');
    -- the neighbour's OWN durable-inbox row (account_key='ACCT-ACME') — must be UNTOUCHED by DEMO's purge/erase.
    INSERT INTO core.webhook_delivery(delivery_key,event_type,account_key,repo,payload,status,done_at) VALUES
      ('wd-acme-done','push','ACCT-ACME','acme/x','{}'::jsonb,'done', now());
    """
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(seed, {"repo": REPO})
    finally:
        conn.close()

    def cc(account):
        return owner_count(account, "SELECT count(*) FROM core.co_change WHERE account_id=%s", (account,))

    def seen(account):
        return owner_count(account, "SELECT count(*) FROM core.co_change_seen_commit WHERE account_id=%s", (account,))

    def wd(account_key):
        # core.webhook_delivery has NO per-account RLS (cross-tenant) and is keyed by account_key (a bare string),
        # so count it directly as the migrator — no RLS pin needed. account_key is the key the purge/erase match on.
        return run_as("veripsa_migrator", "SELECT count(*) FROM core.webhook_delivery WHERE account_key=%s", (account_key,))

    def tomb(account):
        # core.account_lifecycle_tombstone: the uninstall resurrection marker, written only via SECURITY DEFINER
        # authority fns (no per-account RLS). Explicit right-to-erasure deletes it with the account identifiers.
        return run_as("veripsa_migrator", "SELECT count(*) FROM core.account_lifecycle_tombstone WHERE account_id=%s", (account,))

    chk(cc("ACCT-DEMO") == 2 and seen("ACCT-DEMO") == 2,
        f"seed: ACCT-DEMO holds co_change working-set data (pairs={cc('ACCT-DEMO')}, seen_commits={seen('ACCT-DEMO')})")
    chk(cc("ACCT-ACME") == 1 and seen("ACCT-ACME") == 1,
        f"seed: ACCT-ACME (retained neighbour) holds its OWN co_change data (pairs={cc('ACCT-ACME')}, seen={seen('ACCT-ACME')})")
    chk(wd("ACCT-DEMO") == 2 and wd("ACCT-ACME") == 1,
        f"seed: ACCT-DEMO holds durable webhook-inbox rows (rows={wd('ACCT-DEMO')}) + neighbour ACME holds its own (rows={wd('ACCT-ACME')})")

    # bootstrap_local.sh provisions veripsa_app's credential → ACCT-DEMO, so the App service identity resolves to
    # ACCT-DEMO for both the purge and the erase (the host runs them in the offboarding tenant's own context).

    # ── (1) UNINSTALL PURGE (installation.deleted → purge_account_working_set_with_authority). Destructive
    # admission is bound to the exact processing delivery and the App-JWT observation that its generation is absent.
    uninstall_key = "wd-demo-uninstall"
    deleted_generation = "A-DEMO"
    run_as(
        "veripsa_migrator",
        "INSERT INTO core.webhook_delivery(delivery_key,event_type,account_key,payload,status,received_at) "
        "VALUES (%s,'installation','ACCT-DEMO',%s::jsonb,'processing',clock_timestamp())",
        (uninstall_key, json.dumps({
            "action": "deleted",
            "installation": {"id": deleted_generation, "account": {"id": "ACCT-DEMO"}},
        })),
    )
    delete_proof = json.dumps({
        "state": "absent",
        "deleted_installation_id": deleted_generation,
        "account_id": "ACCT-DEMO",
    })
    res_p = run_as_delivery(
        "veripsa_app", uninstall_key,
        "SELECT core.purge_account_working_set_with_authority(%s::jsonb)", (delete_proof,))
    res_p = res_p if isinstance(res_p, dict) else json.loads(res_p)
    chk(bool(res_p.get("ok")) and res_p.get("account_wide"),
        f"uninstall purge ran account-wide (ok={res_p.get('ok')})")
    # STRICT existing invariant: the code graph IS purged (a regression here is a hard FAIL).
    chk(owner_count("ACCT-DEMO", "SELECT count(*) FROM core.code_node WHERE account_id='ACCT-DEMO'") == 0,
        "uninstall purge forgets the code graph (code_node) account-wide [STRICT existing invariant]")

    # STRICT resolved invariant: the uninstall purge removes both co-change tables and reports exact counts.
    cc_after_purge, seen_after_purge = cc("ACCT-DEMO"), seen("ACCT-DEMO")
    chk(cc_after_purge == 0 and seen_after_purge == 0,
        f"uninstall purge forgets the co_change working set account-wide [STRICT] "
        f"(pair survivors={cc_after_purge}, seen-commit survivors={seen_after_purge})")
    purged = res_p.get("purged", {})
    chk(int(purged.get("cochange", -1)) == 2 and int(purged.get("cochange_seen", -1)) == 2,
        f"uninstall purge receipt reports both co-change deletions [STRICT] "
        f"(pairs={purged.get('cochange')}, seen_commits={purged.get('cochange_seen')})")

    # DURABLE WEBHOOK INBOX (audit P1) — STRICT: the uninstall purge must forget the tenant's webhook_delivery rows
    # too (privacy parity), and report the count in its manifest. Strict (not a known-gap branch) because the fix
    # lands WITH this gate. Tenant-scope is asserted below (ACME untouched).
    chk(wd("ACCT-DEMO") == 1,
        f"uninstall purge forgets settled durable inbox rows and retains only its processing owner [STRICT] "
        f"(processing survivor={wd('ACCT-DEMO')})")
    chk(int(res_p.get("purged", {}).get("webhook_deliveries", -1)) == 2,
        f"uninstall purge manifest reports webhook_deliveries deleted "
        f"(reported={res_p.get('purged', {}).get('webhook_deliveries')})")
    finished = run_as(
        "veripsa_app", "SELECT core.finish_webhook_delivery_with_authority(%s,%s)", (uninstall_key, 0))
    chk(finished is True and wd("ACCT-DEMO") == 0,
        "worker finalization scrubs the driving uninstall receipt's account/repository/payload identifiers")
    # RESURRECTION TOMBSTONE (audit iter-4 P1) — STRICT: the account-wide uninstall purge must record a tombstone so a
    # BACKGROUND per-repo writer racing the (lock-free) account-wide purge is refused by assert_account_live and can
    # not silently re-populate the working set we just forgot. (DEMO's account row is RETAINED by the purge, so RLS
    # alone would not block such a re-write — the tombstone is the guard.) Tenant-scope (ACME untouched) asserted below.
    chk(tomb("ACCT-DEMO") == 1,
        f"uninstall purge SETS the resurrection tombstone for the purged account [STRICT] (count={tomb('ACCT-DEMO')})")

    # ── (2) RIGHT-TO-ERASURE HARD DELETE (the "delete ALL my data" path). Re-seed the co_change the purge already
    # cleared if the fix is present; if not (the gap), the rows are still there and the erase must take them.
    # (Re-seed unconditionally so the erase is always exercised against present rows, regardless of (1)'s state.)
    reseed = """
    SET search_path=core;
    SELECT set_config('core.current_account','ACCT-DEMO', true);
    SELECT core.mark_governed_write('co_change');
    INSERT INTO core.co_change(account_id,repo,path_a,path_b,co,n_a,n_b,strength,lift,n_total) VALUES
      ('ACCT-DEMO',%(repo)s,'app/auth.py','app/session.py',8,10,9,0.8,3.2,40)
      ON CONFLICT (account_id,repo,path_a,path_b) DO NOTHING;
    SELECT core.mark_governed_write('co_change_seen_commit');
    INSERT INTO core.co_change_seen_commit(account_id,repo,commit_sha) VALUES ('ACCT-DEMO',%(repo)s,'aa11')
      ON CONFLICT (account_id,repo,commit_sha) DO NOTHING;
    -- re-seed a durable webhook-inbox row too (the purge cleared DEMO's, if the fix is present), so the erase is
    -- always exercised against a present row regardless of the purge's effect.
    INSERT INTO core.webhook_delivery(delivery_key,event_type,account_key,repo,payload,status,done_at) VALUES
      ('wd-demo-erase','push','ACCT-DEMO',%(repo)s,'{}'::jsonb,'done', now())
      ON CONFLICT (delivery_key) DO NOTHING;
    """
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(reseed, {"repo": REPO})
    finally:
        conn.close()
    cc_before_erase, seen_before_erase = cc("ACCT-DEMO"), seen("ACCT-DEMO")
    chk(cc_before_erase >= 1 and seen_before_erase >= 1,
        f"erase setup: ACCT-DEMO holds co_change again before the erase "
        f"(pairs={cc_before_erase}, seen_commits={seen_before_erase})")
    chk(wd("ACCT-DEMO") >= 1, f"erase setup: ACCT-DEMO holds a durable webhook-inbox row before the erase (rows={wd('ACCT-DEMO')})")

    res_e = run_as("veripsa_app", "SELECT core.erase_account_with_authority()")
    res_e = res_e if isinstance(res_e, dict) else json.loads(res_e)
    erased = res_e.get("erased", {})
    chk(bool(res_e.get("ok")) and res_e.get("account") == "ACCT-DEMO",
        f"erase ran for ACCT-DEMO (ok={res_e.get('ok')})")
    # STRICT existing invariant: the account row itself is hard-deleted (a regression here is a hard FAIL).
    chk(owner_count("ACCT-DEMO", "SELECT count(*) FROM core.account WHERE account_id='ACCT-DEMO'") == 0,
        "erase hard-deletes the account row [STRICT existing invariant]")

    # STRICT resolved invariant: hard erase removes both co-change tables and its receipt matches the seeded rows.
    cc_after_erase, seen_after_erase = cc("ACCT-DEMO"), seen("ACCT-DEMO")
    chk(cc_after_erase == 0 and seen_after_erase == 0,
        f"erase hard-deletes the co_change working set [STRICT] "
        f"(pair survivors={cc_after_erase}, seen-commit survivors={seen_after_erase})")
    chk(int(erased.get("co_change", -1)) == cc_before_erase
        and int(erased.get("cochange_seen", -1)) == seen_before_erase,
        f"erase receipt reports both co-change deletions [STRICT] "
        f"(pairs={erased.get('co_change')}/{cc_before_erase}, "
        f"seen_commits={erased.get('cochange_seen')}/{seen_before_erase})")

    # DURABLE WEBHOOK INBOX on the ERASE path (audit P1) — STRICT: the GDPR Art.17 / CCPA hard delete must take the
    # tenant's webhook_delivery rows too, and the deletion RECEIPT must report them (an incomplete receipt was the
    # defect). Strict because the fix lands with this gate.
    chk(wd("ACCT-DEMO") == 0,
        f"erase hard-deletes the durable webhook-inbox rows (account_key) too [STRICT] (survivors={wd('ACCT-DEMO')})")
    chk("webhook_deliveries" in erased and int(erased.get("webhook_deliveries", -1)) >= 1,
        f"the erase manifest counts webhook_deliveries in its deletion receipt [STRICT] "
        f"(reported={erased.get('webhook_deliveries')})")
    # PRIVACY BOUNDARY on the ERASE path — STRICT: DEMO was purged first, so an account-id-bearing uninstall marker
    # exists going in. Hard erasure must remove that marker too and report the deletion. Resurrection prevention is
    # now structural: delayed ordinary/background work uses enter_existing_installation_with_authority and cannot
    # provision an absent route; only an App-JWT-proven activation may select the provisioning path.
    chk(tomb("ACCT-DEMO") == 0,
        f"erase deletes the account lifecycle marker instead of retaining a raw account id [STRICT] "
        f"(survivors={tomb('ACCT-DEMO')})")
    chk(int(erased.get("account_tombstones", -1)) == 1,
        f"erase manifest reports the deleted uninstall marker [STRICT] "
        f"(reported={erased.get('account_tombstones')})")

    # ── (3) TENANT-SCOPE: the retained neighbour ACCT-ACME's co_change is UNTOUCHED by either operation (the
    # purge/erase must NEVER be an over-broad cross-tenant delete). This is a STRICT assertion in BOTH the
    # fix-applied and the known-gap world (whatever the functions do to DEMO, they must not touch ACME).
    chk(cc("ACCT-ACME") == 1 and seen("ACCT-ACME") == 1,
        f"tenant-scope: ACCT-ACME's co_change is entirely UNTOUCHED (pairs={cc('ACCT-ACME')}, seen={seen('ACCT-ACME')}) "
        f"— never an over-broad cross-tenant delete")
    chk(wd("ACCT-ACME") == 1,
        f"tenant-scope: ACCT-ACME's durable webhook-inbox row is entirely UNTOUCHED by DEMO's purge+erase "
        f"(rows={wd('ACCT-ACME')}) — the account_key match never spilled across tenants")
    chk(tomb("ACCT-ACME") == 0,
        f"tenant-scope: ACCT-ACME is NOT tombstoned by DEMO's purge/erase (the uninstall marker is per-account) "
        f"(count={tomb('ACCT-ACME')})")

    # ── (4) account_key KEY-FORMAT FOOTGUN — the PRODUCTION path proof. In production webhook_delivery.account_key
    # is the BARE GitHub owner id STRING (server's _event_account_key returns repository.owner.id verbatim, e.g.
    # '9999'), while the account the erase pins is 'ACCT-GH-'||<id> ('ACCT-GH-9999'). A naive account_key=v_account
    # would match ZERO rows (a silent-false erase that leaves the private-repo full_names behind). The fix strips
    # the 'ACCT-GH-' prefix (substr(v_account,9)) to match the bare id. Prove it END-TO-END: a real GH-shaped tenant
    # (account_id 'ACCT-GH-9999', via enter_installation_with_authority's own prefixing) with a webhook row keyed by
    # the bare '9999' must be erased — i.e. the strip actually matches. (Above used the full-form 'ACCT-DEMO' key,
    # which exercises the v_account branch; THIS exercises the prefix-strip branch — the real production shape.)
    setup_gh = """
    SET search_path=core;
    -- a real GH-shaped tenant whose account_id is exactly 'ACCT-GH-9999' (the 'ACCT-GH-'||<id> form the live
    -- enter_installation_with_authority produces). provision_seat creates the account + agent + a writer credential.
    SELECT core.provision_seat('ACCT-GH-9999','GH Owner 9999','AG-GH-9999','gh9999-writer','veripsa_gh9999_agent');
    INSERT INTO core.installation_account(installation_id, account_id)
      VALUES ('inst-9999','ACCT-GH-9999') ON CONFLICT (installation_id) DO NOTHING;
    -- re-point the App service identity (veripsa_app) at ACCT-GH-9999 so the erase runs in THAT tenant's context.
    -- (DEMO's earlier erase already deleted the veripsa_app credential that pointed at ACCT-DEMO, so INSERT it.)
    INSERT INTO core.credential(role_name, agent_id, account_id)
      VALUES ('veripsa_app','AG-GH-9999','ACCT-GH-9999')
      ON CONFLICT (role_name) DO UPDATE SET account_id=EXCLUDED.account_id, agent_id=EXCLUDED.agent_id;
    -- the webhook row keyed by the BARE gh id '9999' (the production account_key shape), NOT 'ACCT-GH-9999'.
    INSERT INTO core.webhook_delivery(delivery_key,event_type,account_key,repo,payload,status,done_at) VALUES
      ('wd-gh-9999','push','9999','owner9999/repo','{}'::jsonb,'done', now())
      ON CONFLICT (delivery_key) DO NOTHING;
    """
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(setup_gh)
    finally:
        conn.close()
    chk(wd("9999") == 1, f"key-format setup: a webhook row keyed by the BARE gh id '9999' exists (rows={wd('9999')})")
    res_gh = run_as("veripsa_app", "SELECT core.erase_account_with_authority()")
    res_gh = res_gh if isinstance(res_gh, dict) else json.loads(res_gh)
    chk(res_gh.get("account") == "ACCT-GH-9999",
        f"key-format: erase pinned the GH-shaped account 'ACCT-GH-9999' (account={res_gh.get('account')})")
    chk(wd("9999") == 0,
        f"key-format FOOTGUN CLOSED: erasing 'ACCT-GH-9999' deleted the row keyed by the BARE id '9999' "
        f"(the 'ACCT-GH-' prefix-strip matched; survivors={wd('9999')}) — NOT a silent-false zero-match erase")
    chk(int(res_gh.get("erased", {}).get("webhook_deliveries", -1)) == 1,
        f"key-format: the erase receipt counts the stripped-key webhook delete "
        f"(reported={res_gh.get('erased', {}).get('webhook_deliveries')})")

    # ── (5) ENUMERATE-ALL-account_key-bearing-tables GUARD (forward-looking — the audit's explicit ask). The defect
    # was a NEW per-tenant table (webhook_delivery) that the erase forgot. To stop the NEXT such table silently
    # reopening the gap: introspect EVERY core.* table that has an `account_key` column and assert the erase
    # function's body references each one. The instant someone adds another account_key-bearing table without
    # wiring it into the erase, THIS check goes red. (We check the erase body; the purge is covered by the strict
    # survival assertions above — the erase is the GDPR-completeness contract the receipt manifests.)
    cols = conn_for("veripsa_migrator")
    try:
        with cols, cols.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT table_name FROM information_schema.columns "
                        "WHERE table_schema='core' AND column_name='account_key' ORDER BY table_name")
            ak_tables = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT pg_get_functiondef('core.erase_account_with_authority()'::regprocedure)")
            erase_body = cur.fetchone()[0]
    finally:
        cols.close()
    missing = [t for t in ak_tables if f"core.{t}" not in erase_body]
    chk(len(ak_tables) >= 1 and not missing,
        f"erase-completeness ENUMERATION: every account_key-bearing core table is referenced by "
        f"erase_account_with_authority (tables={ak_tables}, missing={missing}) — a NEW such table can't silently "
        f"reopen the GDPR-erase gap")

    ok = all(checks)
    # Emit the PASS marker iff every resolved deletion, receipt, and tenant-scope assertion held.
    print("CO-CHANGE PURGE/ERASE COMPLETENESS GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
