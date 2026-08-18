#!/usr/bin/env python3
"""Account-erasure gate — RIGHT-TO-DELETION reconciled with the append-only / immutable ledgers.

purge_repo_with_authority forgets only the content-free WORKING SET (graph + live claims) for ONE repo and by
design RETAINS the append-only event ledger, the statement records, and the account/agent identity rows (incl.
'GH-<login>' author agents = PERSONAL DATA). That is not enough when a customer says "delete ALL my data / we are
offboarding". core.erase_account_with_authority() is the controlled HARD DELETE that reconciles
"immutable audit trail" with "erase this tenant":

  • account-scoped + un-forgeable: identity from the CONNECTION ROLE (never a caller arg) → a tenant can only
    erase its OWN account, never a victim's; App-delegation only (granted to veripsa_app, refused to a buyer seat).
  • complete: deletes the tenant's footprint across EVERY per-account table, BOTH sides of the social follow graph,
    the cross-account routing rows (installation_account, credential), the agents, and the account row itself.
  • immutability preserved for OTHER tenants: the append-only triggers are NEVER dropped. The token-gated DELETE on
    core.event / core.statement permits only the erased account's rows; a SECOND tenant's ledger stays append-only
    THROUGHOUT (a plain DELETE on it is still refused), and a plain (un-armed) DELETE is still refused everywhere.
  • returns a per-table count manifest = an auditable deletion receipt.

Run:  python3 tests/test_account_erasure.py   (needs local Postgres with the veripsa roles)
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
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
# Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_server.py.
DB = "veripsa_erasuretest_" + str(os.getpid())
REPO = "acme/erase"


def conn_for(role):
    return psycopg2.connect(f"postgresql://{role}@localhost/{DB}")


def run_as(role, sql, args=(), fetch=True):
    conn = conn_for(role)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            if fetch:
                row = cur.fetchone()
                return row[0] if row else None
            return None
    finally:
        conn.close()


def owner_count(account, sql_tail, args=()):
    """Read as the migrator (table owner) with RLS pinned to `account` (FORCE RLS still walls the owner to
    exactly that account — which is what we assert)."""
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


def expect_refused(role, sql, args=(), pin_account=None):
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


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    checks = []

    # ── provision TWO tenants. ACCT-DEMO is the one we will ERASE; ACCT-ACME is the RETAINED neighbour whose
    # immutability + data must be entirely untouched. Register both in the installation→account routing map.
    # Seed ACCT-DEMO with a full footprint: immutable ledger rows (event: landed + the curated collision_held,
    # statement), live working set (claim, code_node/edge, graph_version, intent), saas config (policy,
    # store_connection), an OUTBOUND grant (ACCT-DEMO is grantor), and a 'GH-login' author agent (personal data).
    # ACCT-ACME (the retained neighbour) ALSO holds an INBOUND grant ON ACCT-DEMO's agent (ACCT-ACME grantor,
    # AG-DEMO grantee): grantee_agent REFERENCES core.agent RESTRICT, so if the erase fails to clear this
    # cross-tenant row, the DELETE of ACCT-DEMO's agents raises foreign_key_violation and the WHOLE erase aborts
    # (the tenant could never be erased — GDPR/CCPA failure). Seed a follow edge in BOTH directions across the two
    # tenants so we exercise the both-sides follow erase.
    seed = """
    SET search_path=core;
    SELECT core.provision_seat('ACCT-DEMO','Demo Inc','AG-DEMO','demo-writer','veripsa_demo_agent');
    SELECT core.provision_seat('ACCT-ACME','Acme Inc','AG-ACME','acme-writer','veripsa_acme_agent');
    INSERT INTO core.installation_account(installation_id, account_id) VALUES
      ('inst-demo','ACCT-DEMO'),('inst-acme','ACCT-ACME') ON CONFLICT (installation_id) DO NOTHING;

    -- ACCT-DEMO footprint
    SELECT set_config('core.current_account','ACCT-DEMO', true);
    SELECT core.mark_governed_write('agent');
    INSERT INTO core.agent(agent_id, account_id, display_name) VALUES ('GH-octocat','ACCT-DEMO','octocat') ON CONFLICT DO NOTHING;
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path) VALUES
      ('EV-D-LAND','ACCT-DEMO','landed','GH-octocat',%(repo)s,'main','a.py'),
      ('EV-D-HELD','ACCT-DEMO','collision_held','GH-octocat',%(repo)s,'main','b.py');
    SELECT core.mark_governed_write('statement');
    INSERT INTO core.statement(statement_id,account_id,agent_id,utterance,about_repo,about_branch,about_path)
      VALUES ('ST-D-1','ACCT-DEMO','GH-octocat','this module owns auth',%(repo)s,'main','a.py');
    SELECT core.mark_governed_write('claim');
    INSERT INTO core.claim(claim_id,account_id,agent_id,repo,branch,target_path) VALUES ('CLM-D','ACCT-DEMO','AG-DEMO',%(repo)s,'main','a.py');
    SELECT core.mark_governed_write('code_node');
    INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path) VALUES ('ACCT-DEMO',%(repo)s,'main','N1','file','a.py');
    SELECT core.mark_governed_write('code_edge');
    INSERT INTO core.code_edge(account_id,repo,branch,src,dst,edge_kind) VALUES ('ACCT-DEMO',%(repo)s,'main','N1','N1','contains');
    SELECT core.mark_governed_write('graph_version');
    INSERT INTO core.graph_version(account_id,repo,branch,node_count,edge_count) VALUES ('ACCT-DEMO',%(repo)s,'main',1,1);
    SELECT core.mark_governed_write('intent');
    INSERT INTO core.intent(intent_id,account_id,agent_id,work_ref,summary,scope_in) VALUES ('IN-D','ACCT-DEMO','AG-DEMO','PR-1','do a thing',ARRAY['a.py']);
    SELECT core.mark_governed_write('policy');
    INSERT INTO core.policy(account_id,policy_key,policy_value) VALUES ('ACCT-DEMO','lease_minutes','30');
    SELECT core.mark_governed_write('store_connection');
    INSERT INTO core.store_connection(connection_id,account_id,provider,target) VALUES ('CN-D','ACCT-DEMO','github',%(repo)s);
    SELECT core.mark_governed_write('grant');
    INSERT INTO core.grant(grant_id,grantor_account,grantee_agent,scope) VALUES ('GR-D','ACCT-DEMO','AG-ACME',ARRAY['read']);
    SELECT core.mark_governed_write('follow');
    INSERT INTO core.follow(follower_account,followed_account) VALUES ('ACCT-DEMO','ACCT-ACME');   -- demo follows acme

    -- ACCT-ACME footprint (the RETAINED neighbour). It also FOLLOWS demo (the both-sides edge), and holds an
    -- immutable event we will prove stays append-only THROUGHOUT the erase of the other tenant.
    SELECT set_config('core.current_account','ACCT-ACME', true);
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path) VALUES ('EV-A-LAND','ACCT-ACME','landed','AG-ACME','acme/x','main','z.py');
    SELECT core.mark_governed_write('statement');
    INSERT INTO core.statement(statement_id,account_id,agent_id,utterance,about_repo,about_branch,about_path)
      VALUES ('ST-A-1','ACCT-ACME','AG-ACME','acme keeps this',  'acme/x','main','z.py');
    -- INBOUND grant: ACCT-ACME (grantor) delegates to AG-DEMO (ACCT-DEMO's agent = grantee). This is the
    -- cross-tenant row whose grantee_agent RESTRICT-references ACCT-DEMO's agent — the row that, left behind,
    -- makes erasing ACCT-DEMO abort on foreign_key_violation. (FK referential checks bypass RLS on the referenced
    -- table, so this INSERT under current_account=ACCT-ACME validates AG-DEMO even though it is not RLS-visible.)
    SELECT core.mark_governed_write('grant');
    INSERT INTO core.grant(grant_id,grantor_account,grantee_agent,scope) VALUES ('GR-A-IN','ACCT-ACME','AG-DEMO',ARRAY['read']);
    SELECT core.mark_governed_write('follow');
    INSERT INTO core.follow(follower_account,followed_account) VALUES ('ACCT-ACME','ACCT-DEMO');    -- acme follows demo (the dangling-reference side)
    """
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(seed, {"repo": REPO})
    finally:
        conn.close()

    # NOTE: bootstrap_local.sh already provisions veripsa_app's credential → ACCT-DEMO (agent AG-APP), so the App
    # service identity resolves to ACCT-DEMO for the erase call below (the host runs the erase in the offboarding
    # tenant's own context). No extra provisioning needed.

    def demo_total():
        # sum every per-account row count for ACCT-DEMO across its tables (owner-read, RLS pinned to ACCT-DEMO).
        sql = """SELECT
          (SELECT count(*) FROM core.event WHERE account_id='ACCT-DEMO')
        + (SELECT count(*) FROM core.statement WHERE account_id='ACCT-DEMO')
        + (SELECT count(*) FROM core.claim WHERE account_id='ACCT-DEMO')
        + (SELECT count(*) FROM core.code_node WHERE account_id='ACCT-DEMO')
        + (SELECT count(*) FROM core.code_edge WHERE account_id='ACCT-DEMO')
        + (SELECT count(*) FROM core.graph_version WHERE account_id='ACCT-DEMO')
        + (SELECT count(*) FROM core.intent WHERE account_id='ACCT-DEMO')
        + (SELECT count(*) FROM core.policy WHERE account_id='ACCT-DEMO')
        + (SELECT count(*) FROM core.store_connection WHERE account_id='ACCT-DEMO')
        + (SELECT count(*) FROM core.grant WHERE grantor_account='ACCT-DEMO')
        + (SELECT count(*) FROM core.agent WHERE account_id='ACCT-DEMO')
        + (SELECT count(*) FROM core.account WHERE account_id='ACCT-DEMO')"""
        return owner_count("ACCT-DEMO", sql)

    # installation_account + credential have no per-account RLS read for a count via owner_count's pin; read raw.
    def raw_count(sql):
        conn = conn_for("veripsa_migrator")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(sql)
                return cur.fetchone()[0]
        finally:
            conn.close()

    before_total = demo_total()
    checks.append((f"seed: ACCT-DEMO has a full per-account footprint (rows={before_total})", before_total >= 13))
    checks.append(("seed: a 'GH-octocat' author agent (personal data) exists for ACCT-DEMO",
                   owner_count("ACCT-DEMO", "SELECT count(*) FROM core.agent WHERE agent_id='GH-octocat'") == 1))
    # follow has FORCE RLS keyed on follower_account, so count it pinned to the FOLLOWER side. ACCT-ACME→ACCT-DEMO
    # is visible only when current_account=ACCT-ACME (acme is the follower).
    checks.append(("seed: ACCT-ACME holds a follow edge ON ACCT-DEMO (the dangling-reference side)",
                   owner_count("ACCT-ACME", "SELECT count(*) FROM core.follow WHERE follower_account='ACCT-ACME' AND followed_account='ACCT-DEMO'") == 1))
    # grant has FORCE RLS keyed on grantor_account, so the INBOUND grant ON ACCT-DEMO's agent is visible only when
    # current_account=ACCT-ACME (acme is the grantor). It RESTRICT-references AG-DEMO → it is the FK that blocks the
    # erase if not cleared.
    checks.append(("seed: ACCT-ACME holds a grant whose grantee is ACCT-DEMO's agent (the RESTRICT-FK row)",
                   owner_count("ACCT-ACME", "SELECT count(*) FROM core.grant WHERE grantor_account='ACCT-ACME' AND grantee_agent='AG-DEMO'") == 1))

    # ── (0) NEGATIVE: a buyer seat CANNOT run the erase (App-delegation only).
    err_seat = expect_refused("veripsa_demo_agent", "SELECT core.erase_account_with_authority()")
    checks.append((f"a buyer seat cannot run the account erase (granted to veripsa_app only): {(err_seat or 'NOT REFUSED')[:60]}",
                   err_seat is not None))

    # ── (1) NEGATIVE: while NO erasure token is armed, a plain DELETE on the immutable ledger is STILL refused
    # (the append-only trigger is never weakened — erasure is the ONLY thing that lets these rows go, and only
    # for its own account). Run as owner with ACCT-ACME pinned so RLS admits the statement → only the trigger
    # can refuse it.
    err_plain = expect_refused("veripsa_migrator", "DELETE FROM core.event WHERE account_id='ACCT-ACME'",
                               pin_account="ACCT-ACME")
    checks.append((f"a plain DELETE on core.event is still refused by the append-only trigger: {(err_plain or 'NOT REFUSED')[:60]}",
                   err_plain is not None and "append-only" in err_plain))

    # ── (2) THE ERASE: run as the App service identity (resolves to ACCT-DEMO via its credential). It hard-deletes
    # the whole footprint and returns a per-table manifest.
    res = run_as("veripsa_app", "SELECT core.erase_account_with_authority()")
    res = res if isinstance(res, dict) else json.loads(res)
    erased = res.get("erased", {})
    checks.append((f"erase ran for ACCT-DEMO (ok={res.get('ok')}, account={res.get('account')})",
                   bool(res.get("ok")) and res.get("account") == "ACCT-DEMO"))
    checks.append((f"manifest counts the immutable streams erased (events={erased.get('events')}, statements={erased.get('statements')})",
                   erased.get("events") == 2 and erased.get("statements") == 1))
    checks.append((f"manifest counts the identity rows erased (agents={erased.get('agents')}, accounts={erased.get('accounts')})",
                   erased.get("agents") >= 2 and erased.get("accounts") == 1))
    checks.append((f"manifest counts BOTH sides of the follow graph erased (follows={erased.get('follows')})",
                   erased.get("follows") == 2))
    # BOTH directions of the delegation graph: the OUTBOUND grant ACCT-DEMO issued (grants=1) AND the INBOUND grant
    # another tenant held ON ACCT-DEMO's agent (grants_inbound=1). The inbound count proves the RESTRICT-FK row that
    # would otherwise abort the erase was cleared by the grant_erasable cross-tenant policy.
    checks.append((f"manifest counts BOTH directions of the grant graph erased (grants={erased.get('grants')}, grants_inbound={erased.get('grants_inbound')})",
                   erased.get("grants") == 1 and erased.get("grants_inbound") == 1))

    # ── (3) COMPLETE: every ACCT-DEMO row is gone, across every table (incl. the immutable ledgers + the account).
    after_total = demo_total()
    checks.append((f"ACCT-DEMO's entire per-account footprint is gone (rows={after_total})", after_total == 0))
    checks.append(("the immutable event ledger rows for ACCT-DEMO are gone",
                   owner_count("ACCT-DEMO", "SELECT count(*) FROM core.event WHERE account_id='ACCT-DEMO'") == 0))
    checks.append(("the immutable statement records for ACCT-DEMO are gone",
                   owner_count("ACCT-DEMO", "SELECT count(*) FROM core.statement WHERE account_id='ACCT-DEMO'") == 0))
    checks.append(("the 'GH-octocat' personal-data agent is gone",
                   owner_count("ACCT-DEMO", "SELECT count(*) FROM core.agent WHERE agent_id='GH-octocat'") == 0))
    checks.append(("the account row itself is hard-deleted (not merely closed)",
                   owner_count("ACCT-DEMO", "SELECT count(*) FROM core.account WHERE account_id='ACCT-DEMO'") == 0))
    checks.append(("the installation→account routing row is cleared (a reinstall provisions cleanly)",
                   raw_count("SELECT count(*) FROM core.installation_account WHERE account_id='ACCT-DEMO'") == 0))
    # the dangling-reference side (ACCT-ACME→ACCT-DEMO) must be gone: count it pinned to ACCT-ACME (the follower).
    # ACCT-DEMO's own outbound follow is gone with its account. Together: no edge anywhere names the erased account.
    checks.append(("no follow edge anywhere still references the erased account (no dangling reference)",
                   owner_count("ACCT-ACME", "SELECT count(*) FROM core.follow WHERE follower_account='ACCT-ACME' AND followed_account='ACCT-DEMO'") == 0
                   and owner_count("ACCT-DEMO", "SELECT count(*) FROM core.follow WHERE follower_account='ACCT-DEMO'") == 0))
    # the INBOUND grant ACCT-ACME held ON ACCT-DEMO's agent must be gone (it RESTRICT-referenced the now-deleted
    # AG-DEMO; the whole erase reaching here at all is the proof the FK no longer blocks). Count it pinned to the
    # grantor side (ACCT-ACME). No grant anywhere still references an erased-account agent.
    checks.append(("the cross-tenant grant ON the erased account's agent is gone (no RESTRICT-FK dangling reference)",
                   owner_count("ACCT-ACME", "SELECT count(*) FROM core.grant WHERE grantor_account='ACCT-ACME' AND grantee_agent='AG-DEMO'") == 0))

    # ── (4) THE NEIGHBOUR IS UNTOUCHED + STILL IMMUTABLE. ACCT-ACME keeps all its data, and its event ledger is
    # STILL append-only (a plain DELETE on it is refused) — the erase never dropped the trigger.
    checks.append(("ACCT-ACME's event ledger is untouched by the erase",
                   owner_count("ACCT-ACME", "SELECT count(*) FROM core.event WHERE account_id='ACCT-ACME'") == 1))
    checks.append(("ACCT-ACME's statement records are untouched by the erase",
                   owner_count("ACCT-ACME", "SELECT count(*) FROM core.statement WHERE account_id='ACCT-ACME'") == 1))
    checks.append(("ACCT-ACME's account row is untouched",
                   owner_count("ACCT-ACME", "SELECT count(*) FROM core.account WHERE account_id='ACCT-ACME'") == 1))
    err_after = expect_refused("veripsa_migrator", "DELETE FROM core.event WHERE account_id='ACCT-ACME'",
                               pin_account="ACCT-ACME")
    checks.append((f"ACCT-ACME's ledger is STILL append-only after the erase (plain DELETE refused): {(err_after or 'NOT REFUSED')[:60]}",
                   err_after is not None and "append-only" in err_after))

    # ── (5) the erasure token does not leak past the call: a fresh session has it disarmed, so a plain DELETE on
    # ACCT-ACME's ledger is refused (already shown in (4), which ran in its own connection → proves no leak).

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("ACCOUNT ERASURE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
