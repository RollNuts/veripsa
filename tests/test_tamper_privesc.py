#!/usr/bin/env python3
"""TAMPER-RESISTANCE — the SECURITY DEFINER search_path hijack (a classic Postgres privesc; SECURITY-critical).

Veripsa's write path is a wall of SECURITY DEFINER gate fns owned by `veripsa_migrator`. A SECURITY DEFINER
fn runs with the OWNER's privileges, so it is the most powerful surface in the system — and it carries a
textbook footgun:

  THE THREAT. A SECURITY DEFINER fn that does NOT pin a safe `search_path` resolves UNQUALIFIED names
  (functions, operators, casts, tables) against the CALLER's search_path. If a tenant role can CREATE an
  object in a schema that sits EARLIER on that path (the classic target = `public`, where PUBLIC has
  historically held CREATE), it can SHADOW a name the definer fn calls — e.g. plant a `public.now()` or a
  `public.=` operator — and that tenant-authored code then runs AS `veripsa_migrator`. That is arbitrary
  SQL as the schema owner: forge any claim / any 'landed' event / any installation→account routing row
  (cross-tenant takeover) / any credential (identity impersonation). It defeats the ENTIRE tamper posture
  (grants + forgery trigger + RLS) in one move, because the attacker IS the owner for the duration.

  THE FIX (two independent layers, belt-and-suspenders):
    1. PIN every SECURITY DEFINER fn to a trusted path — the project standard is
       `SET search_path = core, pg_catalog` (stored by PG as proconfig `search_path=core, pg_catalog`).
       A pinned fn ignores the caller's path entirely, so a shadow object is never consulted.
    2. SLAM THE SOURCE — PUBLIC must not be able to CREATE in `public`, so there is nowhere to plant a
       shadow even if a fn were ever unpinned. (`REVOKE CREATE ON SCHEMA public FROM PUBLIC`.)

THIS GATE is the regression guard for BOTH layers, proven against the LIVE catalog (pg_proc / pg_namespace),
not grep — the catalog is the only truth about which fns are actually SECURITY DEFINER and what each one
actually pins:

  A. CATALOG (the hole list). Enumerate EVERY SECURITY DEFINER fn whose proconfig lacks a `search_path=`
     entry. That set must be EMPTY. If a FUTURE fn ships SECURITY DEFINER and forgets the pin, this count
     goes non-zero and the gate FAILS LOUD, naming the offender — the privesc door cannot silently reopen.
  B. PROBE-VALIDITY. There must be a non-trivial number of SECURITY DEFINER fns AND they must all carry the
     standard pin spelling — so the "zero unpinned" result above is real, not a query that matched nothing.
  C. NO PLACE TO PLANT. For every TENANT role (the App + demo agents + steward + the NOLOGIN writer/reader
     capability classes), assert it CANNOT CREATE in `public` OR `core` — by has_schema_privilege AND by a
     LIVE `CREATE FUNCTION` attempt that must be refused ("permission denied for schema public"). This is
     what makes the unpinned-fn risk moot even if layer (1) ever regressed.
  D. POSTURE. The `public` schema's ACL shows PUBLIC holds USAGE only (no CREATE 'C').

HONEST-EMPTY is the goal AND the expected result here: the audit found ZERO unpinned SECURITY DEFINER fns
(the "~93 SECDEF / ~73 SET" lead was grep counting-noise — `SECURITY DEFINER` appears ~21× in design
COMMENTS, not fn headers; the catalog shows 72 SECDEF fns, all 72 pinned). The fix added is therefore
defense-in-depth (the explicit `REVOKE CREATE ON SCHEMA public FROM PUBLIC`, which is the PG15+ default but
states it version-independently) + this guard so a future regression in EITHER layer fails the release.

Run:  python3 tests/test_tamper_privesc.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name would let concurrent
# runs (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run. Per-PID,
# exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_tamper_grants.py.
DB = "veripsa_tamperprivesc_" + str(os.getpid())
ADMIN = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")

# The project's standard pin (how the already-pinned SECDEF fns spell it; PG stores it in proconfig as the
# RHS string below). A new SECDEF fn must use THIS so the catalog assertion sees it.
EXPECTED_PIN = "search_path=core, pg_catalog"

# The TENANT roles a connection can authenticate as (NEVER the migrator/owner) — the surface an attacker
# controls. If ANY of these could CREATE in a schema on the definer fns' resolution path, an unpinned fn
# would be hijackable. (Same role set the other tamper gates adversarially test.)
TENANT_ROLES = [
    "veripsa_app",            # THE PRODUCT — the hosted App's service identity (writer + act_for delegation)
    "veripsa_demo_agent",     # a demo tenant writer (ACCT-DEMO), inherits veripsa_writer
    "veripsa_demo_agent2",    # a 2nd demo tenant writer (ACCT-DEMO)
    "veripsa_demo_agent3",    # a 3rd demo tenant writer (ACCT-DEMO, a vanished seat)
    "veripsa_acme_agent",     # a tenant writer in a SECOND account (ACCT-ACME)
    "veripsa_demo_steward",   # the demo steward seat (reads + breaks lanes, never edits files)
]
# The NOLOGIN capability CLASSES are reached by inheritance, never connected-as, so they can't run a live
# CREATE — but has_schema_privilege still resolves their effective (inherited) grant, so we assert them too.
CAPABILITY_CLASSES = ["veripsa_writer", "veripsa_reader"]

# Schemas a definer fn could resolve an unqualified name against. `public` is the textbook hijack target
# (default-on-path, historically PUBLIC-creatable); `core` is where the fns live (the pin lists it first).
HIJACK_SCHEMAS = ["public", "core"]

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, passed))


def psql_mig(sql):
    """Query as the migrator (owner) — used for the catalog truth + has_schema_privilege probes."""
    mig = f"postgresql://veripsa_migrator@localhost/{DB}"
    r = subprocess.run(["psql", mig, "-v", "ON_ERROR_STOP=0", "-tAc", sql],
                       capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def psql_as(role, sql):
    """Run sql AS `role` (peer auth on localhost) and return combined stdout+stderr — for the LIVE
    CREATE attempt that must be refused."""
    dsn = f"postgresql://{role}@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql],
                       capture_output=True, text=True)
    return (r.stdout + r.stderr)


def bootstrap():
    """roles + schema.sql + the demo seats — the standard local instance (db/bootstrap_local.sh)."""
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("[FAIL] bootstrap (roles + schema.sql + seats)")
        print((r.stdout + r.stderr)[-2000:])
        sys.exit(2)
    # a SECOND account so veripsa_acme_agent resolves to its own tenant (one of the roles we test); and the
    # 3rd demo agent's seat, mirroring test_tamper_grants.py so every tenant role is fully provisioned.
    psql_mig("SET search_path=core; "
             "SELECT core.provision_seat('ACCT-ACME','Acme Co','AG-ACME','acme','veripsa_acme_agent'); "
             "SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-A3','vanished','veripsa_demo_agent3');")


def drop():
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


# A live CREATE that is refused for lack of schema CREATE privilege says one of these. (We DON'T accept a
# generic error — it must be the GRANT-layer denial, so a column/syntax error can't masquerade as security.)
CREATE_DENIED_MARKERS = ("permission denied for schema",)


def main():
    print("VERIPSA TAMPER-RESISTANCE — SECURITY DEFINER search_path HIJACK")
    print(f"(scratch DB: {DB})")
    bootstrap()

    # ── A. THE HOLE LIST (catalog truth). EVERY SECURITY DEFINER fn whose proconfig lacks a `search_path=`
    #    entry. This set MUST be empty; if a future SECDEF fn forgets the pin it appears here and we FAIL,
    #    naming it. (Exclude the system schemas; Veripsa's fns are all in `core`.) ─────────────────────────
    unpinned = psql_mig(
        "SELECT string_agg(n.nspname||'.'||p.proname||'('||"
        "pg_get_function_identity_arguments(p.oid)||')', ' | ' ORDER BY 1) "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog','information_schema') "
        "AND (p.proconfig IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM unnest(p.proconfig) c WHERE c LIKE 'search_path=%'));")
    add("HOLE LIST: ZERO SECURITY DEFINER fns lack a pinned search_path "
        "(a future fn that forgets the pin fails here, naming itself)",
        unpinned == "")
    if unpinned:
        print(f"     UNPINNED (hijackable as the owner): {unpinned}")

    # ── B. PROBE-VALIDITY: the audit actually saw fns (not a query that matched nothing), AND every SECDEF
    #    fn carries the STANDARD pin spelling. ────────────────────────────────────────────────────────────
    total_secdef = psql_mig(
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog','information_schema');")
    add(f"PROBE-VALIDITY: the schema has SECURITY DEFINER fns to audit — got {total_secdef} "
        "(a 0 here would make the 'zero unpinned' result a false-empty)",
        total_secdef.isdigit() and int(total_secdef) >= 20)

    pinned = psql_mig(
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog','information_schema') "
        "AND EXISTS (SELECT 1 FROM unnest(p.proconfig) c WHERE c LIKE 'search_path=%');")
    add(f"PROBE-VALIDITY: every SECURITY DEFINER fn IS pinned — {pinned} pinned of {total_secdef} total "
        "(pinned == total proves the hole list is genuinely empty)",
        pinned == total_secdef and total_secdef.isdigit() and int(total_secdef) > 0)

    # Every distinct pin must be the project standard (a fn pinning to some OTHER, attacker-influenceable
    # path would be a different hole; this catches a mis-spelled / unsafe pin).
    distinct_pins = psql_mig(
        "SELECT string_agg(DISTINCT cfg, ' || ') FROM ("
        "  SELECT array_to_string(p.proconfig, ', ') AS cfg "
        "  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "  WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog','information_schema')) s;")
    add(f"PROBE-VALIDITY: every SECDEF pin is the project standard ('{EXPECTED_PIN}') — got '{distinct_pins}'",
        distinct_pins == EXPECTED_PIN)

    # ── C. NO PLACE TO PLANT A SHADOW. The pin is layer 1; this is layer 2 — even an unpinned fn couldn't be
    #    hijacked if no tenant can CREATE on the resolution path. Assert BOTH by has_schema_privilege AND a
    #    LIVE CREATE attempt. ─────────────────────────────────────────────────────────────────────────────
    for role in TENANT_ROLES + CAPABILITY_CLASSES:
        for sch in HIJACK_SCHEMAS:
            hp = psql_mig(f"SELECT has_schema_privilege('{role}','{sch}','CREATE');")
            add(f"NO-PLANT[{role} → {sch}]: has_schema_privilege CREATE is false "
                "(no schema on a definer fn's path is tenant-writable)",
                hp == "f")

    # LIVE proof for the connectable tenant roles (the capability classes are NOLOGIN, covered by the
    # has_schema_privilege assertion above): a real CREATE FUNCTION in each hijack schema must be REFUSED
    # by the GRANT layer — not by a syntax/name error. This is the actual exploit step, blocked.
    for role in TENANT_ROLES:
        for sch in HIJACK_SCHEMAS:
            out = psql_as(role, f"CREATE FUNCTION {sch}.veripsa_shadow_{os.getpid()}() "
                                f"RETURNS int LANGUAGE sql AS 'SELECT 1';")
            add(f"NO-PLANT (LIVE)[{role} → {sch}]: a real CREATE FUNCTION is refused by the GRANT layer "
                "(can't plant a shadow object to hijack a definer fn)",
                any(m in out for m in CREATE_DENIED_MARKERS))

    # ── D. POSTURE: the `public` schema ACL shows PUBLIC holds USAGE only — never CREATE. (PG stores the
    #    PUBLIC grant as the empty-grantee entry `=.../...`; CREATE would show as 'C'.) This is the PG15+
    #    default AND now stated explicitly via REVOKE CREATE ON SCHEMA public FROM PUBLIC. ─────────────────
    public_acl = psql_mig("SELECT array_to_string(nspacl,' | ') FROM pg_namespace WHERE nspname='public';")
    # the PUBLIC entry is the one whose grantee (before '=') is empty: "=<privs>/<grantor>"
    public_entry = next((e for e in public_acl.split(" | ") if e.lstrip().startswith("=")), "")
    add(f"POSTURE: PUBLIC holds NO CREATE on schema public — its ACL entry is '{public_entry.strip()}' "
        "(USAGE 'U' only, no 'C'); the source of any shadow plant is closed",
        public_entry != "" and "C" not in public_entry.split("/")[0])

    # ── E. CONTROL (probe sanity): the migrator (owner) DOES hold CREATE on both schemas — so the 'false'
    #    results above are a real privilege difference, not a broken has_schema_privilege call. ────────────
    for sch in HIJACK_SCHEMAS:
        owner_hp = psql_mig(f"SELECT has_schema_privilege('veripsa_migrator','{sch}','CREATE');")
        add(f"CONTROL: the OWNER veripsa_migrator HAS CREATE on schema {sch} — got '{owner_hp}' "
            "(proves the tenant 'false' results are a real difference, not a dead probe)",
            owner_hp == "t")

    # ── verdict ─────────────────────────────────────────────────────────────────────────────────────────
    drop()
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    failed = [label for label, ok in checks if not ok]
    print(f"\n-- {passed}/{total} privesc assertions passed "
          f"(catalog hole-list + probe-validity + {len(TENANT_ROLES)} tenant roles × "
          f"{len(HIJACK_SCHEMAS)} schemas no-plant [has_priv + LIVE] + posture + control) --")
    if failed:
        print(f"\n[FAIL] {len(failed)} privesc assertion(s) FAILED — a SECURITY DEFINER fn may be "
              f"search_path-hijackable to run tenant code AS the owner:")
        for f in failed[:40]:
            print(f"   - {f}")
        print("\nTAMPER PRIVESC GATE: FAIL")
        sys.exit(1)
    print(f"\nHONEST-EMPTY: all {total_secdef} SECURITY DEFINER fns pin '{EXPECTED_PIN}' (zero unpinned), "
          "and no tenant role can CREATE in `public` or `core` — the search_path hijack is closed on BOTH "
          "the pin layer AND the plant-a-shadow layer.")
    print("TAMPER PRIVESC GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never leak the scratch DB on an unexpected error
        drop()
        print(f"\n[FAIL] unexpected error: {e}")
        print("TAMPER PRIVESC GATE: FAIL")
        sys.exit(1)
