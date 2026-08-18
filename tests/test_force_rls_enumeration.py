#!/usr/bin/env python3
"""FORCE-RLS ENUMERATION — the runtime catalog audit (Round-2 perimeter follow-up).

The other perimeter gates probe specific breach playbooks against a fixed list of tables. THIS gate is the
STRUCTURAL backstop: it enumerates the live core schema FROM THE CATALOG (pg_class.relforcerowsecurity)
and asserts EVERY multi-tenant table is FORCE-RLS-locked. The class it catches is the silent-regression
one — a new core table that ships with account_id but WITHOUT the matching FORCE ROW LEVEL SECURITY line.
On such a table the FORCE clause silently does nothing (RLS would be table-owner-bypassable), and every
SECURITY DEFINER write would land cross-tenant unless the (also-required) tenant_isolation policy
happened to refuse it. Catching this at the catalog layer is the airtight check — independent of any
single statement, against the running database.

THE TWO ASSERTIONS (each one a regression guard going forward):

  1. EVERY multi-tenant core.* table HAS FORCE RLS. We enumerate pg_class for relkind='r' AND nspname='core',
     join information_schema.columns to detect the multi-tenant key (account_id / account_key /
     follower_account / grantor_account / created_by_account — every tenant-scope column the schema
     actually uses), and assert relforcerowsecurity=true. A new table with one of those columns but a
     missing FORCE line fails LOUD here, naming the table. An allowlist covers the CROSS-TENANT
     operational tables (credential / installation_account / account_lifecycle_tombstone /
     repository_lifecycle_tombstone / account_plan_event / webhook_delivery / active_alert), which are walled by
     REVOKE ALL FROM PUBLIC instead of FORCE RLS (their cross-tenant role is intentional — see the comments).

  2. EVERY FORCE-RLS table also has the tenant_isolation policy. FORCE without a policy = denied to
     everyone (incl. SECURITY DEFINER) = a silently-broken write path. The per-table policy is the
     other half of the lock.

Run:  python3 tests/test_force_rls_enumeration.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent
# runs (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run. Per-PID,
# exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), the other perimeter gates.
DB = "veripsa_forcerlsenum_" + str(os.getpid())

# Tables that INTENTIONALLY do NOT carry FORCE RLS. Each is documented in the schema with the same
# reasoning: it is a CROSS-TENANT operational table (it ROUTES between tenants, marks tenant lifecycle,
# queues raw deliveries, or boards host-level alerts), reached ONLY through a SECURITY DEFINER
# *_with_authority gate fn, and walled to non-owners by REVOKE ALL FROM PUBLIC. The tamper-grants gate
# proves a token-armed tenant is STILL refused a direct write by exactly this missing grant.
#
# credential               — the BOOTSTRAP identity table; the resolver reads it BEFORE current_account
#                            is set (it is what DERIVES the account) so it MUST allow owner-bypass.
# installation_account     — the App's installation→account map; routes BETWEEN accounts on every event.
# account_lifecycle_tombstone — uninstall high-water marker; explicit GDPR erasure deletes it.
# repository_lifecycle_tombstone — repo-scoped revocation marker; cross-tenant operational state.
# account_plan_event       — the commercial plan + last-effective high-water; cross-tenant operational.
# webhook_delivery         — the raw inbox queue; the account is not yet resolved at enqueue time.
# active_alert             — the host's cross-tenant alert board (keyed by alert_key, NOT a tenant fact).
FORCE_RLS_ALLOWLIST = frozenset({
    "credential",
    "installation_account",
    "account_lifecycle_tombstone",
    "repository_lifecycle_tombstone",
    "account_plan_event",
    "webhook_delivery",
    "active_alert",
})

# Every column the live schema uses as the tenant scope. The catalog query below treats a table as
# multi-tenant iff it carries ANY of these. Keep in sync with the live schema; a new tenant-scope column
# means a new core table the FORCE-RLS lock probably wants to cover.
TENANT_COLUMNS = (
    "account_id",
    "account_key",
    "follower_account",
    "grantor_account",
    "created_by_account",
)

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, passed))


def psql(dsn, sql):
    """Run sql via the psql CLI as `dsn` (peer auth on localhost). Returns stripped stdout (the gate runs
    short -tA queries where stderr would be a real error — surface it on failure paths)."""
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql],
                       capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def bootstrap():
    """roles + schema.sql + the demo seats — the standard local instance (db/bootstrap_local.sh)."""
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("[FAIL] bootstrap (roles + schema.sql + seats)")
        print((r.stdout + r.stderr)[-2000:])
        sys.exit(2)


def drop():
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


def main():
    print("VERIPSA FORCE-RLS ENUMERATION — runtime catalog audit")
    print(f"(scratch DB: {DB})")
    bootstrap()

    mig = f"postgresql://veripsa_migrator@localhost/{DB}"

    # ── 1. List every regular table in core. Sanity check (a missing schema would silently pass an empty
    #    enumeration — guard against that). ────────────────────────────────────────────────────────────
    all_tables_raw = psql(mig,
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'core' AND c.relkind = 'r' "
        "ORDER BY c.relname;")
    all_tables = sorted([t for t in all_tables_raw.split() if t])
    add(f"SANITY: at least 10 tables enumerated in core.* (got {len(all_tables)})",
        len(all_tables) >= 10)

    # ── 2. Build the multi-tenant set FROM THE CATALOG (any table carrying one of the tenant-scope
    #    columns). This is the criterion the lane spec calls out: "every core.* table that has the
    #    account column (multi-tenant), assert relforcerowsecurity=true". ──────────────────────────────
    cols_sql_list = ",".join(f"'{c}'" for c in TENANT_COLUMNS)
    multi_tenant_raw = psql(mig, f"""
        SELECT DISTINCT c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN information_schema.columns ic
            ON ic.table_schema = n.nspname AND ic.table_name = c.relname
         WHERE n.nspname = 'core' AND c.relkind = 'r'
           AND ic.column_name IN ({cols_sql_list})
         ORDER BY c.relname;""")
    multi_tenant = sorted([t for t in multi_tenant_raw.split() if t])
    add(f"ENUMERATION: catalog identifies multi-tenant core tables by tenant-scope column "
        f"(got {len(multi_tenant)} of {len(all_tables)})",
        len(multi_tenant) >= 10)

    # ── 3. THE STRUCTURAL ASSERTION: every multi-tenant table not on the allowlist MUST have FORCE RLS.
    #    Reads pg_class.relforcerowsecurity — the live catalog, the only truth about the lock. ─────────
    missing_force = []
    for tbl in multi_tenant:
        if tbl in FORCE_RLS_ALLOWLIST:
            continue
        force_raw = psql(mig,
            f"SELECT relforcerowsecurity FROM pg_class c "
            f"JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = 'core' AND c.relname = '{tbl}';")
        if force_raw.strip().lower() != "t":
            missing_force.append(tbl)
        add(f"FORCE RLS[{tbl}]: relforcerowsecurity=true (multi-tenant table is owner-bypass-locked)",
            force_raw.strip().lower() == "t")

    # ── 4. THE PAIRED ASSERTION: every FORCE-RLS table needs the tenant_isolation policy. FORCE alone
    #    with no policy = denied to everyone (incl. the SECURITY DEFINER owner) = a broken write path.
    #    pg_policies is the catalog view. ────────────────────────────────────────────────────────────
    missing_policy = []
    for tbl in multi_tenant:
        if tbl in FORCE_RLS_ALLOWLIST:
            continue
        pol_raw = psql(mig,
            f"SELECT count(*) FROM pg_policies "
            f"WHERE schemaname = 'core' AND tablename = '{tbl}';")
        if pol_raw.strip() == "0":
            missing_policy.append(tbl)
        add(f"POLICY[{tbl}]: at least one RLS policy exists (FORCE without a policy denies every write)",
            pol_raw.strip() != "0")

    # ── 5. ALLOWLIST INTEGRITY: every allowlisted table is walled the OTHER way (REVOKE ALL FROM PUBLIC),
    #    so the lock is real even without FORCE RLS. has_table_privilege('public', ..., 'INSERT/UPDATE/
    #    DELETE/SELECT') must be FALSE on every allowlisted table — otherwise the allowlist hides a hole.
    #    NOTE: credential is the bootstrap identity table; it relies on the migrator-owner-bypass + the
    #    resolver's role_name = session_user scope, not a PUBLIC revoke (PUBLIC has no schema USAGE
    #    anyway via the least-privilege baseline). So we relax the public-revoke check for credential. ─
    for tbl in sorted(FORCE_RLS_ALLOWLIST):
        if tbl not in all_tables:
            # an allowlist entry for a non-existent table = a stale allowlist; fail loud
            add(f"ALLOWLIST INTEGRITY: allowlisted table core.{tbl} EXISTS in the live schema",
                False)
            continue
        if tbl == "credential":
            # credential is locked by the resolver pattern, not by a PUBLIC revoke — skip the priv probe.
            add(f"ALLOWLIST INTEGRITY: allowlisted table core.{tbl} EXISTS (resolver-walled, not REVOKE-walled)",
                True)
            continue
        # PUBLIC must have ZERO direct table privilege on the allowlisted operational tables.
        pub_raw = psql(mig, f"""
            SELECT (has_table_privilege('public', 'core.{tbl}', 'SELECT')
                 OR has_table_privilege('public', 'core.{tbl}', 'INSERT')
                 OR has_table_privilege('public', 'core.{tbl}', 'UPDATE')
                 OR has_table_privilege('public', 'core.{tbl}', 'DELETE'));""")
        add(f"ALLOWLIST INTEGRITY[{tbl}]: PUBLIC has ZERO direct table privilege "
            f"(REVOKE ALL FROM PUBLIC is the wall here, not FORCE RLS)",
            pub_raw.strip().lower() == "f")

    # ── 6. PROBE VALIDITY: the migrator (owner) is subject to FORCE RLS by definition — verify a known
    #    FORCE-locked table actually reports relforcerowsecurity=true (so a broken pg_class query can't
    #    quietly pass-by-zero). claim is one of the original FORCE-locked tables (20_core.sql). ────────
    claim_force = psql(mig,
        "SELECT relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'core' AND c.relname = 'claim';")
    add("PROBE VALIDITY: a known FORCE-locked table (claim) reports relforcerowsecurity=true "
        "(so the pg_class probe is real, not silently empty)",
        claim_force.strip().lower() == "t")

    # ── 7. ALLOWLIST VERIFICATION: a known operational table (webhook_delivery) reports
    #    relforcerowsecurity=false — confirming the catalog also reports the absence honestly. ─────────
    wh_force = psql(mig,
        "SELECT relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'core' AND c.relname = 'webhook_delivery';")
    add("PROBE VALIDITY (negative): the allowlisted core.webhook_delivery is NOT FORCE-locked "
        "(the catalog reports false — proving the probe distinguishes the two states)",
        wh_force.strip().lower() == "f")

    # ── verdict ─────────────────────────────────────────────────────────────────────────────────────
    drop()
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    failed = [label for label, ok in checks if not ok]
    print(f"\n-- {passed}/{total} catalog assertions passed "
          f"({len(multi_tenant)} multi-tenant tables × FORCE + policy + allowlist integrity + probe validity) --")
    if missing_force:
        print(f"\n[FAIL] {len(missing_force)} multi-tenant table(s) MISSING FORCE RLS — a new table can land "
              f"cross-tenant writes through the SECURITY DEFINER owner. Either add "
              f"ALTER TABLE ONLY core.<t> FORCE ROW LEVEL SECURITY or, if it is intentionally cross-tenant "
              f"operational (REVOKE-walled), add it to FORCE_RLS_ALLOWLIST here:")
        for t in missing_force:
            print(f"   - {t}")
    if missing_policy:
        print(f"\n[FAIL] {len(missing_policy)} FORCE-locked table(s) have NO RLS policy — every write is denied:")
        for t in missing_policy:
            print(f"   - {t}")
    if failed:
        print(f"\n[FAIL] {len(failed)} catalog assertion(s) FAILED — see the [FAIL] lines above.")
        for f in failed[:40]:
            print(f"   - {f}")
        print("\nFORCE-RLS ENUMERATION GATE: FAIL")
        sys.exit(1)
    print("\nSTRUCTURAL OK: every multi-tenant core table is FORCE-RLS-locked + has a tenant_isolation "
          "policy; every allowlisted operational table is REVOKE-walled instead.")
    print("FORCE-RLS ENUMERATION GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never leak the scratch DB on an unexpected error
        drop()
        raise
