#!/usr/bin/env python3
"""TAMPER-RESISTANCE — the GRANT barrier (security-critical, the un-forgeable-record moat).

Veripsa's PRODUCT is a faithful, un-forgeable record. Every core table wears a two-layer write block:

  1. a FORGERY TRIGGER (assert_governed_write, BEFORE INSERT/UPDATE) that refuses any write unless the
     session GUC `core.governed_write_token` equals the table's tag, and
  2. the real barrier: the TABLE GRANT. A tenant role holds NO direct write grant on ANY core table; the
     ONLY write path is a `*_with_authority` SECURITY DEFINER gate fn (owned by veripsa_migrator) that
     arms the token AS THE OWNER, then writes.

THE THREAT (verified mechanism). `core.mark_governed_write(tag)` is just
    set_config('core.governed_write_token', tag, true)
on a CUSTOM GUC. A custom GUC can be set by ANY role calling set_config itself — so the forgery TOKEN is
NOT a real barrier on its own. The design comment is explicit: "a direct INSERT is refused for lack of a
table grant AND by the forgery trigger." If ANY tenant role held a stray write/TRUNCATE grant on ANY core
table, it could arm the token itself (raw set_config, bypassing the EXECUTE-revoked fn) and FORGE a row —
a fabricated claim / a fabricated 'landed' event / a forged installation→account routing row (cross-tenant
takeover) / a forged credential (identity impersonation). The record would no longer be un-forgeable.

THIS GATE proves the grant barrier is AIRTIGHT. For EVERY core table × EVERY tenant role × EVERY write verb
(INSERT / UPDATE / DELETE / TRUNCATE), it has the tenant ARM EVERY forgery + bypass token ITSELF
(governed_write_token, retention_token, account_erasure_token, demo_maintenance_token) to DEFEAT the
trigger — and asserts the write is STILL refused, by "permission denied for TABLE" (the missing grant),
NOT merely by the (token-defeated) forgery trigger. It also proves a tenant cannot call the token-arming
fns, cannot GRANT itself privileges, and that the legit gate path still works for the right role.

HONEST-EMPTY is the goal: every table's grant barrier holds against a token-armed tenant. If a single
"permission denied for table" is missing (i.e. a stray grant lets a token-armed direct write through),
this gate FAILS — that is a real forgery hole.

Run:  python3 tests/test_tamper_grants.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
from psycopg2 import errorcodes  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name would let concurrent
# runs (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run. Per-PID,
# exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_tenant_isolation.py.
DB = "veripsa_tampergrants_" + str(os.getpid())
ADMIN = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")

# The registered core tables (the db/smoke.sh MANIFEST — the no-乱立 allowlist). Enumerated here so a
# NEW core table that ships WITHOUT this gate covering it makes the manifest cross-check below FAIL loud.
CORE_TABLES = [
    "account", "account_lifecycle_tombstone", "account_plan_event", "active_alert", "agent",
    "boot_reconcile_state", "claim", "co_change",
    "co_change_seen_commit", "code_edge", "code_node", "credential", "event", "follow",
    "github_delivery_recovery", "github_delivery_recovery_scan", "grant", "graph_convergence_lease",
    "graph_version", "installation_account", "intent", "policy", "policy_refresh_outbox", "statement", "store_connection",
    "repository_lifecycle_activation", "repository_lifecycle_tombstone", "webhook_delivery", "webhook_worker_instance", "workspace", "workspace_member",
]

# The TENANT roles a connection can authenticate as (NEVER the migrator/owner). These are the roles a buyer
# / a demo agent / a steward actually connect with — the surface an attacker controls.
TENANT_ROLES = [
    "veripsa_app",            # THE PRODUCT — the hosted App's service identity (writer + act_for delegation)
    "veripsa_demo_agent",     # a demo tenant writer (ACCT-DEMO), inherits veripsa_writer
    "veripsa_demo_agent2",    # a 2nd demo tenant writer (ACCT-DEMO)
    "veripsa_demo_agent3",    # a 3rd demo tenant writer (ACCT-DEMO, a vanished seat)
    "veripsa_acme_agent",     # a tenant writer in a SECOND account (ACCT-ACME)
    "veripsa_demo_steward",   # the demo steward seat (reads + breaks lanes, never edits files)
]
# veripsa_reader / veripsa_writer are NOLOGIN capability CLASSES (reached by inheritance, never connected-as),
# so a connection can't authenticate as them. They are covered by the catalog assertion (effective-priv == 0).

# A minimal, schema-valid row per table so the write REACHES the grant/trigger check (a column/constraint
# error would mask the security verdict). account_id is ACCT-DEMO so the (also-armed) account-scoped tokens
# would match if the grant ever let the row through. Values are content-free placeholders.
INSERT_COLS = {
    "account":              ("(account_id) VALUES ('ACCT-FORGE')"),
    "account_lifecycle_tombstone": ("(account_id, reason) VALUES ('ACCT-DEMO','uninstall_purge')"),
    "account_plan_event":   ("(account_id, last_effective_at) VALUES ('ACCT-DEMO', now())"),
    "repository_lifecycle_tombstone": ("(account_id, repository_id, repo, reason) VALUES "
                                         "('ACCT-DEMO','123','owner/repo','repository_deleted')"),
    "repository_lifecycle_activation": ("(account_id, repository_id, repo) VALUES "
                                          "('ACCT-DEMO','124','owner/active')"),
    "active_alert":         ("(alert_key, level, message) VALUES ('graph_stale','warning','forge')"),
    "agent":                ("(agent_id, account_id) VALUES ('AG-FORGE','ACCT-DEMO')"),
    "boot_reconcile_state": ("(kind) VALUES ('boot_reconcile')"),
    "claim":                ("(claim_id, account_id, agent_id, target_path) VALUES ('FORGE','ACCT-DEMO','AG-A','x')"),
    "co_change":            ("(account_id, repo, path_a, path_b, co, n_a, n_b, strength) VALUES ('ACCT-DEMO','r','a','b',1,1,1,0.5)"),
    "co_change_seen_commit": ("(account_id, repo, commit_sha) VALUES ('ACCT-DEMO','r','abc123')"),
    "code_edge":            ("(account_id, src, dst, edge_kind) VALUES ('ACCT-DEMO','a','b','calls')"),
    "code_node":            ("(account_id, node_id, node_kind, path) VALUES ('ACCT-DEMO','n','file','p')"),
    "credential":           ("(role_name, agent_id, account_id) VALUES ('veripsa_demo_agent','AG-A','ACCT-DEMO')"),
    "event":                ("(account_id, event_id, kind, agent_id) VALUES ('ACCT-DEMO','EV-FORGE','push','AG-A')"),
    "follow":               ("(follower_account, followed_account) VALUES ('ACCT-DEMO','ACCT-OTHER')"),
    "github_delivery_recovery": ("(delivery_guid,latest_delivery_id,latest_delivered_at,latest_status_code,"
                                   "latest_status,latest_recovery_class,window_expires_at) VALUES "
                                   "('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',1,now(),500,'Other','redeliverable',now()+interval '3 days')"),
    "github_delivery_recovery_scan": ("(singleton) VALUES (true)"),
    "grant":                ("(grant_id, grantor_account, grantee_agent, scope) VALUES ('G-FORGE','ACCT-DEMO','AG-A',ARRAY['read'])"),
    "graph_convergence_lease": ("(account_id,slot,lease_epoch,request_epoch,repository_id,repo,branch,"
                                 "target_sha,claimed_by,claimed_until) VALUES "
                                 "('ACCT-DEMO',1,1,1,'123','owner/repo','main','abcdef0','forge',now())"),
    "graph_version":        ("(account_id, repo, branch) VALUES ('ACCT-DEMO','r','main')"),
    "installation_account": ("(installation_id, account_id) VALUES ('INST-FORGE','ACCT-DEMO')"),
    "intent":               ("(intent_id, account_id, agent_id, work_ref, summary, scope_in) VALUES ('I-FORGE','ACCT-DEMO','AG-A','w','s',ARRAY['p'])"),
    "policy":               ("(account_id, policy_key, policy_value) VALUES ('ACCT-DEMO','k','v')"),
    "policy_refresh_outbox": ("(account_id, policy_epoch) VALUES ('ACCT-DEMO', 1)"),
    "statement":            ("(statement_id, account_id, agent_id, utterance) VALUES ('ST-FORGE','ACCT-DEMO','AG-A','u')"),
    "store_connection":     ("(connection_id, account_id, provider, target) VALUES ('CN-FORGE','ACCT-DEMO','s3','t')"),
    "webhook_delivery":     ("(delivery_key, event_type, payload) VALUES ('D-FORGE','push','{}'::jsonb)"),
    "webhook_worker_instance": ("(instance_id) VALUES ('FORGE-INST')"),
    "workspace":            ("(workspace_id, created_by_account) VALUES ('WS-FORGE','ACCT-DEMO')"),
    "workspace_member":     ("(workspace_id, account_id, repo) VALUES ('WS-FORGE','ACCT-DEMO','r')"),
}

# For the UPDATE vector we SET a column to ITSELF (a true no-op, WHERE false → no row touched). Most core
# tables have an `account_id` column; the only two that DON'T are `follow` and `grant` — give them an explicit
# self-SET so the UPDATE PARSES and reaches the grant gate (a column error would mask the security verdict).
_SELF_SET = {
    "follow": "follower_account=follower_account",
    "grant": "grantor_account=grantor_account",
    "webhook_delivery": "delivery_key=delivery_key",
    "workspace": "created_by_account=created_by_account",   # no account_id column (keyed by created_by_account)
    "active_alert": "alert_key=alert_key",   # no account_id column (operator board) — explicit self-SET so the UPDATE parses
    "boot_reconcile_state": "kind=kind",   # no account_id column (host throttle) — explicit self-SET so the UPDATE parses
    "webhook_worker_instance": "instance_id=instance_id",   # no account_id column (host worker board) — explicit self-SET so the UPDATE parses
    "github_delivery_recovery_scan": "singleton=singleton",   # no account_id column (global scan state) — explicit self-SET so the UPDATE parses
    "github_delivery_recovery": "delivery_guid=delivery_guid",   # no account_id column (delivery metadata) — explicit self-SET so the UPDATE parses
}

# Every token a tenant could arm to defeat a trigger: the per-table forgery token (set per-table below) +
# the THREE named DELETE/UPDATE-bypass tokens (retention / account-erasure / demo-maintenance). The whole
# point: even with ALL of these armed BY THE TENANT, the missing table grant must still refuse the write.
ARM_BYPASS_TOKENS = """
  SELECT set_config('core.current_account','ACCT-DEMO',true);
  SELECT set_config('core.retention_token','ACCT-DEMO',true);
  SELECT set_config('core.account_erasure_token','ACCT-DEMO',true);
  SELECT set_config('core.demo_maintenance_token','ACCT-DEMO',true);
"""

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, passed))


def psql(dsn, sql, allow_dangerous=False):
    """Run sql via the psql CLI as `dsn` (so we exercise the SAME peer-auth role path smoke.sh uses) and
    return combined stdout+stderr. allow_dangerous: the repo's pre-exec hook blocks TRUNCATE; this is a
    PID-unique THROWAWAY test DB and TRUNCATE-refusal is exactly what we audit, so opt in for those calls."""
    env = dict(os.environ)
    if allow_dangerous:
        env["ALLOW_DANGEROUS_COMMANDS"] = "1"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql],
                       capture_output=True, text=True, env=env)
    return (r.stdout + r.stderr)


def bootstrap():
    """roles + schema.sql + the demo seats — the standard local instance (db/bootstrap_local.sh)."""
    env = dict(os.environ)
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT,
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print("[FAIL] bootstrap (roles + schema.sql + seats)")
        print((r.stdout + r.stderr)[-2000:])
        sys.exit(2)
    # a SECOND account + agent in a different tenant, so veripsa_acme_agent resolves to its own account
    # (it is one of the tenant roles we adversarially test; without a credential it would 42501 on identity).
    mig = f"postgresql://veripsa_migrator@localhost/{DB}"
    psql(mig, "SET search_path=core; "
              "SELECT core.provision_seat('ACCT-ACME','Acme Co','AG-ACME','acme','veripsa_acme_agent'); "
              "SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-A3','vanished','veripsa_demo_agent3');")


def drop():
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


# Strings that prove the write was refused by the GRANT (the real barrier), not merely by the (token-defeated)
# forgery trigger. A bare "forgery block" alone would mean the grant was PRESENT and only the token saved us —
# which is the exact hole this gate exists to catch. We require a GRANT-layer denial.
GRANT_DENIED_MARKERS = ("permission denied for table", "permission denied for relation")


def main():
    print("VERIPSA TAMPER-RESISTANCE — GRANT BARRIER")
    print(f"(scratch DB: {DB})")
    bootstrap()

    # ── 0. MANIFEST cross-check: the tables THIS gate covers must equal the live core table set. A new core
    #    table that shipped without being added here (so it might ship without a grant audit) fails LOUD. ──
    mig = f"postgresql://veripsa_migrator@localhost/{DB}"
    # Default privileges must make a brand-new function non-PUBLIC immediately, not only after
    # schema/99_least_privilege.sql runs. schema.sql is statement-autocommit, so this is the boundary that keeps
    # a deploy interruption between CREATE FUNCTION and a later REVOKE from exposing a SECURITY DEFINER surface.
    psql(
        mig,
        "CREATE FUNCTION core._default_acl_probe() RETURNS void "
        "LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS 'BEGIN NULL; END';",
    )
    default_acl_probe = psql(
        mig,
        "SELECT EXISTS ("
        "SELECT 1 FROM pg_proc p, "
        "LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) acl "
        "WHERE p.oid='core._default_acl_probe()'::regprocedure "
        "AND acl.grantee=0 AND acl.privilege_type='EXECUTE');",
    ).strip()
    psql(mig, "DROP FUNCTION core._default_acl_probe();")
    add(
        "DEFAULT ACL: a newly-created function starts with PUBLIC EXECUTE denied before any per-function revoke",
        default_acl_probe == "f",
    )

    live = psql(mig, "SELECT string_agg(tablename, ' ' ORDER BY tablename) "
                     "FROM pg_tables WHERE schemaname='core'").strip()
    live_set = sorted(live.split())
    add(f"MANIFEST: this gate covers exactly the live core table set ({len(live_set)} tables) — "
        f"a new uncovered table fails here",
        live_set == sorted(CORE_TABLES))
    if live_set != sorted(CORE_TABLES):
        print(f"     covered : {sorted(CORE_TABLES)}")
        print(f"     live    : {live_set}")

    # ── 1. THE ADVERSARIAL MATRIX: every table × every tenant role × every write verb, with ALL tokens
    #    armed BY THE TENANT. Assert a GRANT-layer denial every time. ─────────────────────────────────────
    for role in TENANT_ROLES:
        dsn = f"postgresql://{role}@localhost/{DB}"
        for tbl in CORE_TABLES:
            tag = tbl
            # INSERT — arm the per-table forgery token + all bypass tokens, THEN direct INSERT.
            ins = psql(dsn, f"SET search_path=core; {ARM_BYPASS_TOKENS} "
                            f"SELECT set_config('core.governed_write_token','{tag}',true); "
                            f"INSERT INTO core.\"{tbl}\" {INSERT_COLS[tbl]};")
            add(f"INSERT[{role} → {tbl}]: token-armed direct INSERT refused by GRANT (not just trigger)",
                any(m in ins for m in GRANT_DENIED_MARKERS))

            # UPDATE — token armed; a no-op WHERE false still trips the grant check before any row is seen.
            # SET a column to ITSELF (a true no-op) — most tables have account_id, a few don't (use _self_set
            # so the UPDATE PARSES and reaches the grant gate; a column error would mask the security verdict).
            upd = psql(dsn, f"SET search_path=core; {ARM_BYPASS_TOKENS} "
                            f"SELECT set_config('core.governed_write_token','{tag}',true); "
                            f"UPDATE core.\"{tbl}\" SET account_id=account_id WHERE false;"
                            if tbl not in _SELF_SET
                            else f"SET search_path=core; {ARM_BYPASS_TOKENS} "
                                 f"SELECT set_config('core.governed_write_token','{tag}',true); "
                                 f"UPDATE core.\"{tbl}\" SET {_SELF_SET[tbl]} WHERE false;")
            add(f"UPDATE[{role} → {tbl}]: token-armed direct UPDATE refused by GRANT",
                any(m in upd for m in GRANT_DENIED_MARKERS))

            # DELETE — token armed (incl. the named DELETE-bypass tokens that would let the gate prune/erase).
            dele = psql(dsn, f"SET search_path=core; {ARM_BYPASS_TOKENS} "
                             f"DELETE FROM core.\"{tbl}\" WHERE false;")
            add(f"DELETE[{role} → {tbl}]: token-armed direct DELETE refused by GRANT "
                f"(retention/erasure/demo tokens don't help — no grant)",
                any(m in dele for m in GRANT_DENIED_MARKERS))

            # TRUNCATE — the bulk-wipe vector (a whole-ledger erase). Hook-guarded; throwaway DB.
            trunc = psql(dsn, f'SET search_path=core; TRUNCATE core."{tbl}";', allow_dangerous=True)
            add(f"TRUNCATE[{role} → {tbl}]: direct TRUNCATE refused by GRANT (no truncate grant)",
                any(m in trunc for m in GRANT_DENIED_MARKERS))

    # ── 2. The token-arming surface itself is locked down (defense-in-depth: a tenant can't even call the
    #    fns that arm the forgery / erasure tokens — EXECUTE revoked from PUBLIC). ─────────────────────────
    agent = f"postgresql://veripsa_demo_agent@localhost/{DB}"
    add("LOCKDOWN: tenant cannot call core.mark_governed_write (EXECUTE revoked from PUBLIC)",
        "permission denied for function mark_governed_write"
        in psql(agent, "SET search_path=core; SELECT core.mark_governed_write('claim');"))
    add("LOCKDOWN: tenant cannot call core.mark_account_erasure (EXECUTE revoked from PUBLIC)",
        "permission denied for function mark_account_erasure"
        in psql(agent, "SET search_path=core; SELECT core.mark_account_erasure('ACCT-DEMO');"))

    # ── 3. A tenant cannot GRANT itself privileges on a core table (it doesn't own them; only the migrator
    #    does). If this ever succeeded, the whole grant barrier would be self-defeatable. ──────────────────
    for tbl in ("claim", "event", "installation_account", "credential"):
        g = psql(agent, f'GRANT INSERT ON core."{tbl}" TO veripsa_demo_agent;')
        add(f"NO-SELF-GRANT: tenant cannot GRANT itself INSERT on core.{tbl} (it isn't the owner)",
            any(m in g for m in GRANT_DENIED_MARKERS) or "must be owner" in g)

    # ── 4. AUTHORITATIVE CATALOG: the airtight proof, independent of any single statement — EVERY tenant
    #    role (incl. the NOLOGIN capability classes veripsa_writer/reader) has ZERO effective (direct OR
    #    inherited) INSERT/UPDATE/DELETE/TRUNCATE privilege on ALL core tables. The migrator (owner) has
    #    them all — proving the probe actually detects grants (no false-empty). ────────────────────────────
    all_roles = TENANT_ROLES + ["veripsa_writer", "veripsa_reader"]
    for role in all_roles:
        n = psql(mig, f"""
          SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='core' AND c.relkind='r'
            AND (has_table_privilege('{role}', c.oid, 'INSERT')
              OR has_table_privilege('{role}', c.oid, 'UPDATE')
              OR has_table_privilege('{role}', c.oid, 'DELETE')
              OR has_table_privilege('{role}', c.oid, 'TRUNCATE'));""").strip()
        add(f"CATALOG: {role} holds ZERO write priv (direct OR inherited) on ALL {len(CORE_TABLES)} core tables — got {n}",
            n == "0")
    owner_n = psql(mig, """
      SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='core' AND c.relkind='r'
        AND has_table_privilege('veripsa_migrator', c.oid, 'INSERT');""").strip()
    add(f"CATALOG (probe-validity): the OWNER veripsa_migrator HAS INSERT on all {len(CORE_TABLES)} core "
        f"tables — got {owner_n} (so the zero-priv result above is real, not a broken query)",
        owner_n == str(len(CORE_TABLES)))

    # ── 5. The LEGIT gate path still works for the right role (the barrier blocks forgery, NOT the product).
    #    The App/agent writes ONLY through *_with_authority fns; prove a representative gate write succeeds. ─
    legit = psql(agent, "SET search_path=core; "
                        "SELECT (core.declare_claim_with_authority('LEGIT-1','src/ok.py','acme/app','main')->>'granted');")
    add("LEGIT PATH: the tenant's claim through the gate fn (declare_claim_with_authority) SUCCEEDS — "
        "the moat blocks forgery, not the product", "true" in legit)
    legit_stmt = psql(agent, "SET search_path=core; "
                             "SELECT core.record_statement_with_authority('a note','src/ok.py','acme/app','main');")
    add("LEGIT PATH: the tenant's statement through the gate fn (record_statement_with_authority) SUCCEEDS",
        "ERROR" not in legit_stmt and legit_stmt.strip() != "")

    # ── verdict ─────────────────────────────────────────────────────────────────────────────────────────
    drop()
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    failed = [label for label, ok in checks if not ok]
    print(f"\n-- {passed}/{total} adversarial assertions passed "
          f"({len(TENANT_ROLES)} tenant roles × {len(CORE_TABLES)} tables × 4 verbs + lockdown/catalog/legit) --")
    if failed:
        print(f"\n[FAIL] {len(failed)} forgery-barrier assertion(s) FAILED — a stray grant may let a "
              f"token-armed tenant FORGE the un-forgeable record:")
        for f in failed[:40]:
            print(f"   - {f}")
        print("\nTAMPER GRANTS GATE: FAIL")
        sys.exit(1)
    print("\nHONEST-EMPTY: every core table's GRANT barrier holds against a token-armed tenant on every "
          "write vector (INSERT/UPDATE/DELETE/TRUNCATE), for every tenant role.")
    print("TAMPER GRANTS GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never leak the scratch DB on an unexpected error
        drop()
        print(f"\n[FAIL] unexpected error: {e}")
        print("TAMPER GRANTS GATE: FAIL")
        sys.exit(1)
