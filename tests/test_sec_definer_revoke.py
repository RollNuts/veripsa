#!/usr/bin/env python3
"""SEC DEFINER REVOKE — the cross-tenant breach gate for three SECURITY DEFINER helpers that trust a
caller-set identity (the `core.current_account` GUC / a `p_account` argument) instead of re-deriving it.

THE BREACH (verified, reproduced before the fix):
  Postgres grants EXECUTE to PUBLIC by DEFAULT on every CREATE FUNCTION. Three SECURITY DEFINER fns were
  MISSING the `REVOKE EXECUTE ... FROM PUBLIC` that every sibling gate fn has (30_gate.sql alone carries 21
  such REVOKEs). So any role holding the PUBLIC default — a future customer DB seat (a veripsa_writer-class
  buyer role) — could call them DIRECTLY, and they read/write under the CALLER-SET account, crossing tenants:

    (1) core._conclude_change_tombstone(text,text,text,text,text) — arms a claim write. A seat could
        `SET core.current_account='ACCT-VICTIM'` and call it to forge a `<change>:__concluded__` tombstone in
        a VICTIM tenant → core.change_concluded() returns true → the webhook SKIPS analysis → a CROSS-TENANT
        SILENT MISS (Veripsa stops watching the victim's PR). Reproduced: the forged row landed in the victim.

    (2) core._policy_int(text,int,int,int) and (3) core._policy_text(text,text) — read core.policy WHERE
        account_id = the caller-set core.current_account GUC, without re-pinning. A seat pinning the GUC to a
        victim read the victim's tuning value back. Reproduced: returned a victim's value (999 / a secret glob).

THE FIX (this gate guards it):
  * REVOKE EXECUTE ... FROM PUBLIC on all three (the norm — callable only via the in-schema SECURITY DEFINER
    callers that run as the migrator owner and re-derive identity from the connection role).
  * DEFENSE-IN-DEPTH for (1): it now RE-DERIVES the writing account/agent via
    core.establish_session_write_context() (the pattern every sibling gate fn uses) and writes under THAT,
    never the p_account/p_agent argument — so even an in-schema caller can never steer the write to a forged
    account. The two legit callers (land_change / release_change) already run this exact derivation and pass
    the SAME values, so the legit owner path is byte-identical and UNAFFECTED.

WHAT THIS GATE PROVES (live, against a 2-tenant fixture DEMO≠ACME with a real victim policy row, on the
ephemeral test Postgres — never grep; the only truth is the running database):

  A. DENIAL — a non-owner buyer seat (veripsa_demo_agent: PUBLIC + veripsa_writer, tenant ACCT-DEMO) is
     DENIED (permission denied for function) calling each of the three fns directly, and
     has_function_privilege is FALSE for every buyer/seat capability class. CONTROL: the migrator (owner) HAS
     it — the denials are a real privilege difference, not a dead probe.
  B. BREACH CLOSED — that buyer, forging current_account=ACCT-ACME, cannot forge a tombstone in the victim
     tenant (zero victim rows) and cannot read the victim's policy values (denied, not the leaked value).
  C. LEGIT PATH INTACT — the App (veripsa_app), on a genuinely-routed installation, lands a change whose
     `opened` was never seen → the internal _conclude_change_tombstone STILL writes the tombstone in the App's
     own resolved tenant, and core.change_concluded() reports it (the order-independence backstop still works).
  D. RE-DERIVE — the App forging current_account to a DIFFERENT tenant still writes into its routed tenant,
     not the forged one (the write follows the connection-role identity, never the caller GUC).

Run:  python3 tests/test_sec_definer_revoke.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# PROCESS-UNIQUE (parallel-safe): this gate bootstraps + drops its own DB, so a FIXED name would let concurrent
# runs (parallel CI shards / several agents running run_gates) drop each other's DB mid-run. Per-PID, exactly
# like db/smoke.sh, run_gates, and the sibling security gates.
DB = "veripsa_secrevoke_" + str(os.getpid())

# a valid hex commit sha (land_change rejects non-hex / >64 chars)
SHA = "abc123def456abc123def456abc123def456abcd"
SHA2 = "abc123def456abc123def456abc123def456abce"

# the three SECURITY DEFINER fns this gate guards — EXACT signatures (the REVOKE/has_function_privilege key).
SEC_DEFINER_FNS = [
    "core._conclude_change_tombstone(text,text,text,text,text)",
    "core._policy_int(text,int,int,int)",
    "core._policy_text(text,text)",
]
# the capability classes / seat roles that must NOT be able to call them (a buyer never reaches an internal fn).
BUYER_ROLES = ["veripsa_writer", "veripsa_reader", "veripsa_demo_agent", "veripsa_acme_agent"]

# A GRANT-layer denial says EXACTLY this — we don't accept a generic error (a syntax/type error must never
# masquerade as a security denial).
DENIED = ("permission denied",)

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, passed))


def psql_mig(sql):
    """As the migrator (owner) — ground truth + has_function_privilege probes."""
    dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def psql_as(role, sql):
    """Run sql AS `role` (peer auth on localhost) — combined stdout+stderr so a permission-denied is visible."""
    dsn = f"postgresql://{role}@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr)


def last_value(out):
    """The LAST non-empty line — a multi-statement `SET ...; SELECT ...` prints a 'SET' ack before the result,
    so a scalar probe must read the final line, not the whole blob."""
    lines = [ln for ln in out.splitlines() if ln.strip() != ""]
    return lines[-1].strip() if lines else ""


def bootstrap():
    """roles + schema.sql + the demo seats (db/bootstrap_local.sh), then a SECOND tenant ACCT-ACME (the
    victim) with its own login seat, a routed installation 222→ACCT-ACME (so the App path resolves to ACME),
    and a real victim policy row seeded THROUGH THE GATE (the only legit write path)."""
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("[FAIL] bootstrap (roles + schema.sql + seats)")
        print((r.stdout + r.stderr)[-2000:])
        sys.exit(2)
    psql_mig("SET search_path=core; "
             "SELECT core.provision_seat('ACCT-ACME','Acme Co','AG-ACME','acme','veripsa_acme_agent');")
    psql_mig("SET search_path=core; "
             "INSERT INTO core.installation_account(installation_id,account_id) VALUES ('222','ACCT-ACME') "
             "ON CONFLICT DO NOTHING;")
    # the VICTIM seeds its OWN policy through the gate (identity from its connection role → lands in ACCT-ACME).
    seed = psql_as("veripsa_acme_agent",
                   "SET search_path=core; "
                   "SELECT core.set_policy_with_authority('lease_minutes','999'); "
                   "SELECT core.set_policy_with_authority('low_value_collision_globs','ACME-SECRET-GLOB');")
    if "permission denied" in seed or "ERROR" in seed:
        print("[FAIL] could not seed the victim policy row through the gate:")
        print(seed[-1500:])
        sys.exit(2)


def drop():
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


def main():
    print("VERIPSA SEC DEFINER REVOKE — cross-tenant breach gate for 3 caller-trusting SECURITY DEFINER fns")
    print(f"(scratch DB: {DB})")
    bootstrap()

    # ── PROBE-VALIDITY: the victim row really exists in ACCT-ACME (so the read probe is not vacuous). ────────
    victim_row = last_value(psql_mig("SET search_path=core; SET core.current_account='ACCT-ACME'; "
                                     "SELECT string_agg(policy_key||':'||policy_value,',' ORDER BY policy_key) "
                                     "FROM core.policy WHERE account_id='ACCT-ACME';"))
    add("FIXTURE: the victim tenant ACCT-ACME has its real seeded policy rows (the read probe is live)",
        "lease_minutes:999" in victim_row and "low_value_collision_globs:ACME-SECRET-GLOB" in victim_row)

    # ── A. DENIAL — has_function_privilege FALSE for every buyer/seat class; migrator (owner) HAS it. ───────
    for fn in SEC_DEFINER_FNS:
        for role in BUYER_ROLES:
            hp = psql_mig(f"SELECT has_function_privilege('{role}','{fn}','EXECUTE');")
            add(f"A DENIAL: buyer/seat class {role} has NO EXECUTE on {fn} (PUBLIC default stripped)",
                hp == "f")
        # CONTROL: the owner (migrator) still has it — proves the denials are a real privilege difference.
        hp_owner = psql_mig(f"SELECT has_function_privilege('veripsa_migrator','{fn}','EXECUTE');")
        add(f"A CONTROL: the owner (veripsa_migrator) HAS EXECUTE on {fn} (the buyer denials are a real "
            f"privilege difference, not a dead probe)", hp_owner == "t")

    # ── B. BREACH CLOSED — a buyer forging current_account to the victim can't forge a tombstone there ──────
    #    nor read the victim's policy values (every direct call is DENIED at the GRANT layer).
    out = psql_as("veripsa_demo_agent",
                  "SET search_path=core; SET core.current_account='ACCT-ACME'; "
                  "SELECT core._conclude_change_tombstone('ACCT-ACME','GH-attacker','PR-VICTIM-42',"
                  "'acmeorg/acme-repo','main');")
    add("B BREACH: a buyer forging current_account=ACCT-ACME and calling _conclude_change_tombstone directly "
        "is DENIED (permission denied for function)", any(m in out for m in DENIED))
    forged = last_value(psql_mig("SET search_path=core; SET core.current_account='ACCT-ACME'; "
                                 "SELECT count(*) FROM core.claim "
                                 "WHERE account_id='ACCT-ACME' AND claim_id='PR-VICTIM-42:__concluded__';"))
    add("B BREACH: NO forged tombstone landed in the victim tenant (the cross-tenant silent-miss is closed)",
        forged == "0")

    out_int = psql_as("veripsa_demo_agent",
                      "SET search_path=core; SET core.current_account='ACCT-ACME'; "
                      "SELECT core._policy_int('lease_minutes',30,5,1440);")
    add("B BREACH: a buyer forging current_account=ACCT-ACME and calling _policy_int directly is DENIED "
        "(it cannot read the victim's numeric tuning value)",
        any(m in out_int for m in DENIED) and "999" not in out_int)
    out_txt = psql_as("veripsa_demo_agent",
                      "SET search_path=core; SET core.current_account='ACCT-ACME'; "
                      "SELECT core._policy_text('low_value_collision_globs','DEFAULT');")
    add("B BREACH: a buyer forging current_account=ACCT-ACME and calling _policy_text directly is DENIED "
        "(it cannot read the victim's config string)",
        any(m in out_txt for m in DENIED) and "ACME-SECRET-GLOB" not in out_txt)

    # ── C. LEGIT PATH INTACT — the App lands a change with no prior `opened` → the internal tombstone is still
    #    written (the REVOKE + re-derive do not break the order-independence backstop). ───────────────────────
    landed = last_value(psql_as("veripsa_app",
                                "SET search_path=core; SELECT core.enter_installation_with_authority('222'); "
                                f"SELECT (core.land_change_on_main_with_authority('PR-LEGIT-7',"
                                f"'acmeorg/acme-repo','{SHA}','claude-opus-4-8','main'))->>'landed';"))
    add("C LEGIT: the App (veripsa_app, routed installation) lands a change successfully (the legit owner path "
        "still runs the internal _conclude_change_tombstone)", landed == "true")
    tomb = last_value(psql_mig("SET search_path=core; SET core.current_account='ACCT-ACME'; "
                               "SELECT account_id||'|'||claim_state FROM core.claim "
                               "WHERE claim_id='PR-LEGIT-7:__concluded__';"))
    add("C LEGIT: the internal tombstone was written in the App's OWN resolved tenant "
        "(ACCT-ACME, state=released) — the re-derive does not break the legit caller",
        tomb == "ACCT-ACME|released")
    concl = last_value(psql_as("veripsa_app",
                               "SET search_path=core; SELECT core.enter_installation_with_authority('222'); "
                               "SELECT core.change_concluded('acmeorg/acme-repo','PR-LEGIT-7');"))
    add("C LEGIT: core.change_concluded() reports the legit change concluded (the order-independence backstop "
        "still functions end-to-end through the owner path)", concl == "t")

    # ── D. RE-DERIVE — the App forging current_account to a DIFFERENT tenant still writes into its ROUTED
    #    tenant, never the forged one (identity follows the connection role, not the caller GUC). ─────────────
    psql_as("veripsa_app",
            "SET search_path=core; SELECT core.enter_installation_with_authority('222'); "
            "SET core.current_account='ACCT-DEMO'; "
            f"SELECT core.land_change_on_main_with_authority('PR-LEGIT-8','acmeorg/acme-repo','{SHA2}',"
            "'claude-opus-4-8','main');")
    in_acme = last_value(psql_mig("SET search_path=core; SET core.current_account='ACCT-ACME'; "
                                  "SELECT count(*) FROM core.claim WHERE claim_id='PR-LEGIT-8:__concluded__';"))
    in_demo = last_value(psql_mig("SET search_path=core; SET core.current_account='ACCT-DEMO'; "
                                  "SELECT count(*) FROM core.claim WHERE claim_id='PR-LEGIT-8:__concluded__';"))
    add("D RE-DERIVE: the App forging current_account=ACCT-DEMO still wrote the tombstone into its ROUTED "
        "tenant (ACCT-ACME), not the forged one (identity is re-derived from the connection role)",
        in_acme == "1" and in_demo == "0")

    # ── verdict ────────────────────────────────────────────────────────────────────────────────────────────
    drop()
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    failed = [label for label, ok in checks if not ok]
    print(f"\n-- {passed}/{total} assertions passed "
          "(buyer/seat DENIED on all 3 fns + breach closed + legit App path intact + re-derive follows the "
          "connection role) --")
    if failed:
        print(f"\n[FAIL] {len(failed)} assertion(s) FAILED — the SECURITY DEFINER perimeter has a hole:")
        for f in failed[:40]:
            print(f"   - {f}")
        print("\nSEC DEFINER REVOKE GATE: FAIL")
        sys.exit(1)
    print("\nHONEST: the three caller-trusting SECURITY DEFINER fns are no longer PUBLIC-callable; a buyer/seat "
          "role cannot forge a cross-tenant tombstone (silent-miss) or read another tenant's policy; the legit "
          "App path still writes the order-independence tombstone in its own routed tenant.")
    print("SEC DEFINER REVOKE GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never leak the scratch DB on an unexpected error
        drop()
        print(f"\n[FAIL] unexpected error: {e}")
        print("SEC DEFINER REVOKE GATE: FAIL")
        sys.exit(1)
