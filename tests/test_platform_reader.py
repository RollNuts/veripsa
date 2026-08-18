#!/usr/bin/env python3
"""PLATFORM READER — the least-privilege, content-free EFFECT read path for the SEPARATE example platform.

THE NEED: effect_surface() resolves the tenant from the SESSION identity (App role / a credentialed seat), so a
dashboard reader connecting as its OWN role cannot pin an arbitrary installation. The platform must read a
SPECIFIC installation's EFFECT — read-only, content-free, least privilege — WITHOUT being able to touch the moat
(the code graph / file contents) or write anything.

THE SURFACE (additive, in db/schema/30_gate.sql + db/schema/40_surfaces.sql):
  * core._effect_for_account(text)        — the shared body BOTH effect_surface() (session account) and
                                            effect_for_installation() (routed account) call (no drift). Internal,
                                            PUBLIC-revoked.
  * core.effect_for_installation(text)    — the SAME content-free jsonb as effect_surface(), but for the account
                                            behind an explicit installation id (from the no-RLS routing table).
                                            Unmapped id → NULL.
  * core.list_installation_ids()          — installation ids ONLY (no account ids), so the reader can enumerate
                                            without a raw SELECT on core.installation_account.
  * core.list_installation_accounts()     — owner-admin account roster: known installation id + owning account id
                                            + live flag for churn awareness
                                            only, so admin can group by GitHub account without repo details.
  * core.owner_account_usage_surface(int) — bounded owner-admin activation counts per GitHub account. This is an
                                            allowlisted projection; the broader owner_cost_surface stays denied.
  * core.installation_is_live(text)       — boolean liveness for a specific installation id, so the platform can
                                            fail closed before/after purchase without reading the routing table.
  * core.repos_for_installation(text) /
    core.repo_insights_for_installation(text,text,int) /
    core.file_insights_for_installation(text,text,text) /
    core.now_for_installation(text)       — content-free operational summaries for the routed installation only.
  * role example_platform_reader          — NOLOGIN in roles.sql (LOGIN+password out-of-band on prod); its ENTIRE
                                            reachable surface = USAGE on schema core + EXECUTE on the intended
                                            content-free platform functions only.

WHAT THIS GATE PROVES (live, on the ephemeral test Postgres — never grep; the only truth is the running DB):

  EXTRACT IDENTITY — effect_surface()'s output is BYTE-IDENTICAL to the inlined effect for the same account
     (the extraction into _effect_for_account did not change what effect_surface returns), incl. the
     collisions_occurred value (now inlined, not via collisions_on_main).

  A. ALLOW — example_platform_reader (granted the platform fns + schema USAGE) CAN call
     effect_for_installation(<id>), list_installation_ids(), and installation_is_live(<id>), and gets content-free
     output (the same jsonb the App sees for that tenant). Unknown installation id → NULL/false (reveals nothing).

  B. DENY-WRITE — the reader CANNOT write: record_push_with_authority is permission-denied (it is granted to
     veripsa_app only). No effect ledger row appears for the reader's attempt.

  C. DENY-GRAPH — the reader CANNOT read the moat: SELECT on core.code_node / core.code_edge is permission-denied
     (no table grant), and effect_for_installation for a DIFFERENT tenant cannot leak that tenant's graph either
     (the surface only ever returns content-free counts/paths, never node/edge rows).

  D. DENY-DIRECT-INTERNAL — the reader CANNOT call the internal _effect_for_account(<arbitrary account>) directly
     (PUBLIC-revoked), so it cannot bypass the installation→account resolution to read an arbitrary tenant.

  CONTROL: has_function_privilege / has_table_privilege confirm the reader's grants are EXACTLY the intended
  platform EXECUTEs and NOTHING on the graph tables / write fn — the denials are a real privilege difference, not dead
  probes, and the owner (migrator) has it all.

Run:  python3 tests/test_platform_reader.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# PROCESS-UNIQUE (parallel-safe): this gate bootstraps + drops its own DB, like db/smoke.sh and the sibling
# security gates — a FIXED name would let concurrent runs drop each other's DB mid-run.
DB = "veripsa_platreader_" + str(os.getpid())

SHA = "abc123def456abc123def456abc123def456abcd"

READER = "example_platform_reader"
# capability/seat classes that must NOT be able to call the platform-only fns (the reader is its own role).
NON_READER_BUYERS = ["veripsa_writer", "veripsa_reader", "veripsa_demo_agent"]

DENIED = ("permission denied",)

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, passed))


def psql_mig(sql):
    """As the migrator (owner) — ground truth + fixtures + has_*_privilege probes."""
    dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def psql_admin(sql):
    """As the cluster ADMIN/superuser (ADMIN_DSN, default postgresql://localhost/postgres) — for the ONE thing
    the migrator cannot do: ALTER ROLE ... LOGIN. db/roles.sql sets LOGIN for the local fixture roles the same
    way (it is run under ADMIN_DSN). example_platform_reader ships NOLOGIN; on PROD the operator grants
    LOGIN+password out-of-band — here the gate grants LOGIN (peer auth, no password) IN ITS EPHEMERAL DB to
    exercise the deny/allow surface AS the reader."""
    dsn = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def psql_as(role, sql):
    """Run sql AS `role` (peer auth on localhost) — combined stdout+stderr so a permission-denied is visible."""
    dsn = f"postgresql://{role}@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr)


def last_value(out):
    """The LAST non-empty line — a multi-statement `SET ...; SELECT ...` prints a 'SET' ack first."""
    lines = [ln for ln in out.splitlines() if ln.strip() != ""]
    return lines[-1].strip() if lines else ""


def bootstrap():
    """roles + schema.sql + demo seats (db/bootstrap_local.sh); a SECOND tenant ACCT-ACME with its own seat +
    a routed installation 222→ACCT-ACME; seed REAL content-free effect facts in BOTH tenants through the gate
    (so effect_for_installation has something non-empty to return, and the cross-tenant probe is live); and a
    graph row in core.code_node so the DENY-GRAPH probe is not vacuous. Finally, ALTER the platform reader LOGIN
    (in THIS ephemeral DB only — prod sets LOGIN+password out-of-band) so we can connect as it."""
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("[FAIL] bootstrap (roles + schema.sql + seats)")
        print((r.stdout + r.stderr)[-2000:])
        sys.exit(2)

    # the demo account already has an App seat (AG-APP/veripsa_app). Route an installation 111→ACCT-DEMO so the
    # App path + effect_for_installation resolve to the demo tenant.
    psql_mig("SET search_path=core; "
             "INSERT INTO core.installation_account(installation_id,account_id,account_login,account_type) "
             "VALUES ('111','ACCT-DEMO','demo-owner','User') "
             "ON CONFLICT DO NOTHING;")
    # second tenant ACCT-ACME with its own login seat + a routed installation.
    psql_mig("SET search_path=core; "
             "SELECT core.provision_seat('ACCT-ACME','Acme Co','AG-ACME','acme','veripsa_acme_agent');")
    psql_mig("SET search_path=core; "
             "INSERT INTO core.installation_account(installation_id,account_id,account_login,account_type) "
             "VALUES ('222','ACCT-ACME','acme-co','Organization') "
             "ON CONFLICT DO NOTHING;")
    # A non-live historical link under the same account. Owner-admin needs this
    # for install/churn awareness, while operational enumeration must stay
    # live-only.
    psql_mig("SET search_path=core; "
             "INSERT INTO core.installation_account(installation_id,account_id,account_login,account_type,revoked_at) "
             "VALUES ('333','ACCT-ACME','acme-renamed','Organization',now()) "
             "ON CONFLICT (installation_id) DO UPDATE SET revoked_at=EXCLUDED.revoked_at;")

    # Seed REAL effect facts through the App gate (the only legit write path), routed per installation:
    #   DEMO (inst 111): one push (a landing) + one warn (an exposure flagged before merge).
    seed_demo = psql_as("veripsa_app",
                        "SET search_path=core; SELECT core.enter_installation_with_authority('111'); "
                        f"SELECT core.record_push_with_authority('demoorg/demo-repo','main','{SHA}'); "
                        # record_warn_with_authority(p_path, p_repo, p_branch, p_detail) — PATH first.
                        "SELECT core.record_warn_with_authority('app/models.py','demoorg/demo-repo','main','exposes auth/login.py');")
    if "permission denied" in seed_demo or "ERROR" in seed_demo:
        print("[FAIL] could not seed DEMO effect facts through the gate:")
        print(seed_demo[-1500:])
        sys.exit(2)
    #   ACME (inst 222): one warn — so the two tenants are distinguishable + the cross-tenant probe is live.
    seed_acme = psql_as("veripsa_app",
                        "SET search_path=core; SELECT core.enter_installation_with_authority('222'); "
                        "SELECT core.record_warn_with_authority('acme/secret.py','acmeorg/acme-repo','main','ACME-PRIVATE-EXPOSURE');")
    if "permission denied" in seed_acme or "ERROR" in seed_acme:
        print("[FAIL] could not seed ACME effect facts through the gate:")
        print(seed_acme[-1500:])
        sys.exit(2)

    # Simulate a GitHub account rename/update observed from a webhook payload.
    # The stable account id stays ACCT-ACME; only the public display metadata changes.
    meta_update = psql_as("veripsa_app",
                          "SET search_path=core; SELECT core.enter_installation_with_authority('222'); "
                          "SELECT core.note_installation_account_metadata_with_authority('222','acme-renamed','Organization');")
    if "permission denied" in meta_update or "ERROR" in meta_update:
        print("[FAIL] could not update installation account metadata through the gate:")
        print(meta_update[-1500:])
        sys.exit(2)

    # A graph row so the DENY-GRAPH SELECT probe is not vacuous (the table genuinely has a row to be denied).
    # core.code_node columns: account_id, node_id, node_kind, path, name, language, repo (defaults '' NOT NULL).
    # core.code_node is FORCE-RLS + governed-write, so a direct INSERT must (a) pin core.current_account (RLS
    # WITH CHECK) and (b) arm the forgery token via mark_governed_write — both txn-local, so do it in ONE txn.
    g = psql_mig("SET search_path=core; "
                 "BEGIN; "
                 "SELECT set_config('core.current_account','ACCT-DEMO',true); "
                 "SELECT core.mark_governed_write('code_node'); "
                 "INSERT INTO core.code_node(account_id,node_id,node_kind,path,name,language,repo) "
                 "VALUES ('ACCT-DEMO','N1','file','app/models.py','app/models.py','python','demoorg/demo-repo'); "
                 "COMMIT;")
    if "ERROR" in g:
        print("[FAIL] could not seed a code_node row (the DENY-GRAPH control would be vacuous):")
        print(g[-1500:])
        sys.exit(2)

    # A co-change row ABOVE the customer render floor (co>=5, lift>=2) for the demo repo, so
    # repo_insights_for_installation.couplings has REAL floor-passing data to return. co_change is FORCE-RLS +
    # governed-write (same moat as code_node): pin the account + arm mark_governed_write in ONE txn. Canonical
    # order path_a < path_b ('app/auth.py' < 'app/models.py').
    cc = psql_mig("SET search_path=core; "
                  "BEGIN; "
                  "SELECT set_config('core.current_account','ACCT-DEMO',true); "
                  "SELECT core.mark_governed_write('co_change'); "
                  "INSERT INTO core.co_change(account_id,repo,path_a,path_b,co,n_a,n_b,strength,lift,n_total) "
                  "VALUES ('ACCT-DEMO','demoorg/demo-repo','app/auth.py','app/models.py',6,8,7,0.75,3.0,100) "
                  "ON CONFLICT DO NOTHING; "
                  "COMMIT;")
    if "ERROR" in cc:
        print("[FAIL] could not seed a co_change row (repo_insights couplings would be vacuous):")
        print(cc[-1500:])
        sys.exit(2)

    # LOGIN for the reader so the gate can connect AS it (prod sets LOGIN+password out-of-band; roles.sql ships
    # it NOLOGIN). ALTER ROLE needs the cluster admin/superuser (the migrator lacks CREATEROLE) — same authority
    # db/roles.sql uses to LOGIN the local fixtures. Peer auth on localhost → no password needed for the gate.
    login = psql_admin("ALTER ROLE example_platform_reader LOGIN;")
    if "ERROR" in login or "permission denied" in login:
        print("[FAIL] could not grant LOGIN to example_platform_reader via ADMIN_DSN (the gate cannot connect "
              "as the reader to exercise its surface):")
        print(login[-1500:])
        sys.exit(2)


def drop():
    # reset the cluster-global role back to NOLOGIN (it ships NOLOGIN in roles.sql; leaving a stray LOGIN bit set
    # on a shared cluster after the gate is untidy). Best-effort — ignore errors (e.g. role already gone).
    subprocess.run(["psql", os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres"),
                    "-tAc", "ALTER ROLE example_platform_reader NOLOGIN;"], capture_output=True, text=True)
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


def main():
    print("example platform READER — least-privilege content-free EFFECT read path for the separate platform")
    print(f"(scratch DB: {DB})")
    bootstrap()

    # ── EXTRACT IDENTITY: effect_surface() (as the App, routed to DEMO) == _effect_for_account('ACCT-DEMO'). ──
    #    Proves the extraction into the shared body did not change effect_surface's output (byte-identical jsonb).
    via_surface = psql_as("veripsa_app",
                          "SET search_path=core; SELECT core.enter_installation_with_authority('111'); "
                          "SELECT core.effect_surface()::text;")
    via_surface_json = last_value(via_surface)
    via_account = last_value(psql_mig("SET search_path=core; SET core.current_account='ACCT-DEMO'; "
                                      "SELECT core._effect_for_account('ACCT-DEMO')::text;"))
    same = False
    try:
        same = (via_surface_json != "" and json.loads(via_surface_json) == json.loads(via_account))
    except Exception:
        same = False
    add("EXTRACT: effect_surface() output is byte-identical to _effect_for_account(account) for the same tenant "
        "(the extraction is behavior-preserving)", same)
    # and effect_for_installation('111') must equal effect_surface() for that same routed tenant.
    via_inst = last_value(psql_as("veripsa_app",
                                  "SET search_path=core; SELECT core.effect_for_installation('111')::text;"))
    same_inst = False
    try:
        same_inst = (via_inst != "" and json.loads(via_inst) == json.loads(via_surface_json))
    except Exception:
        same_inst = False
    add("EXTRACT: effect_for_installation('111') == effect_surface() for the installation's tenant "
        "(same content-free shape, resolved by id instead of session)", same_inst)

    # ── A. ALLOW — the reader CAN call both platform fns and gets content-free output. ───────────────────────
    r_inst = last_value(psql_as(READER, "SET search_path=core; SELECT core.effect_for_installation('111')::text;"))
    r_obj = None
    try:
        r_obj = json.loads(r_inst)
    except Exception:
        r_obj = None
    add("A ALLOW: example_platform_reader CAN call effect_for_installation('111') and gets a jsonb object",
        isinstance(r_obj, dict))
    # content-free shape: the documented keys, and the recent[] entries carry ONLY
    # {kind,by,path,repo,branch,at,change} — change is the PR id (content-free), never file contents / node-edge rows.
    expected_keys = {"prevented_clobbers", "warns_issued", "steered", "landings", "interventions",
                     "collisions_occurred", "recent"}
    add("A ALLOW: the reader's output has the content-free effect keys (prevented_clobbers/warns_issued/landings"
        "/interventions/collisions_occurred/recent)",
        isinstance(r_obj, dict) and expected_keys.issubset(set(r_obj.keys())))
    recent_ok = (isinstance(r_obj, dict) and isinstance(r_obj.get("recent"), list)
                 and all(isinstance(it, dict)
                         and set(it.keys()) <= {"kind", "by", "path", "repo", "branch", "at", "change"}
                         and (it.get("change") is None
                              or (isinstance(it.get("change"), str) and 0 < len(it["change"]) <= 64))
                         for it in r_obj.get("recent", [])))
    add("A ALLOW: every recent[] entry is content-free {kind,by,path,repo,branch,at,change} ONLY (change=PR id; no contents/graph)",
        recent_ok)
    # it reflects the DEMO facts we seeded (a landing + a warn) — proving it read the right tenant, not empty.
    add("A ALLOW: the reader sees the DEMO tenant's seeded facts (>=1 landing AND >=1 warn) — a live, non-empty read",
        isinstance(r_obj, dict) and (r_obj.get("landings", 0) >= 1) and (r_obj.get("warns_issued", 0) >= 1))

    r_ids = psql_as(READER, "SET search_path=core; SELECT core.list_installation_ids();")
    add("A ALLOW: example_platform_reader CAN call list_installation_ids() and sees the routed ids (111 & 222)",
        ("111" in r_ids) and ("222" in r_ids) and ("333" not in r_ids) and "permission denied" not in r_ids)
    r_accounts = None
    try:
        r_accounts = json.loads(last_value(psql_as(READER, "SET search_path=core; "
            "SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY account_id, installation_id),'[]'::jsonb)::text "
            "FROM core.list_installation_accounts() t;")))
    except Exception:
        r_accounts = None
    add("A ALLOW: example_platform_reader CAN call list_installation_accounts() and sees live account-level rows",
        isinstance(r_accounts, list)
        and {"installation_id": "111", "account_id": "ACCT-DEMO",
             "account_login": "demo-owner", "account_type": "User", "live": True} in r_accounts
        and {"installation_id": "222", "account_id": "ACCT-ACME",
             "account_login": "acme-renamed", "account_type": "Organization", "live": True} in r_accounts)
    add("A ALLOW: list_installation_accounts includes non-live rows for owner churn awareness, marked live=false",
        isinstance(r_accounts, list)
        and {"installation_id": "333", "account_id": "ACCT-ACME",
             "account_login": "acme-renamed", "account_type": "Organization", "live": False} in r_accounts)
    add("A ALLOW: list_installation_accounts entries are content-free {installation_id,account_id,account_login,account_type,live} ONLY",
        isinstance(r_accounts, list)
        and all(isinstance(x, dict)
                and set(x.keys()) <= {"installation_id", "account_id", "account_login", "account_type", "live"}
                for x in r_accounts))

    usage_surface = None
    try:
        usage_surface = json.loads(last_value(psql_as(
            READER,
            "SET search_path=core; SELECT core.owner_account_usage_surface(200)::text;")))
    except Exception:
        usage_surface = None
    usage_accounts = usage_surface.get("accounts", []) if isinstance(usage_surface, dict) else []
    usage_by_id = {
        row.get("account_id"): row
        for row in usage_accounts
        if isinstance(row, dict) and isinstance(row.get("account_id"), str)
    }
    usage_keys = {
        "account_id", "repos", "events", "events_7d", "active_agents"
    }
    add("A ALLOW: reader CAN call owner_account_usage_surface and gets bounded account-level activation rows",
        isinstance(usage_surface, dict)
        and set(usage_surface.keys()) == {"accounts", "accounts_scanned", "cap", "capped"}
        and usage_surface.get("capped") is False
        and usage_surface.get("cap") == 200
        and usage_surface.get("accounts_scanned") == 2
        and {"ACCT-DEMO", "ACCT-ACME"}.issubset(set(usage_by_id)))
    add("A ALLOW: account usage rows expose ONLY account id + coarse activation counts (no repo/path/graph detail)",
        bool(usage_accounts)
        and all(isinstance(row, dict)
                and set(row.keys()) == usage_keys
                and isinstance(row.get("account_id"), str)
                and isinstance(row.get("repos"), int)
                and isinstance(row.get("events"), int)
                and isinstance(row.get("events_7d"), int)
                and isinstance(row.get("active_agents"), int)
                for row in usage_accounts))
    high_cap_surface = json.loads(last_value(psql_as(
        READER,
        "SET search_path=core; SELECT core.owner_account_usage_surface(2147483647)::text;")))
    low_cap_surface = json.loads(last_value(psql_as(
        READER,
        "SET search_path=core; SELECT core.owner_account_usage_surface(-1)::text;")))
    add("A BOUNDED: a caller-supplied huge cap is clamped to the Platform maximum of 200",
        high_cap_surface.get("cap") == 200
        and high_cap_surface.get("accounts_scanned") == 2
        and len(high_cap_surface.get("accounts", [])) == 2)
    add("A BOUNDED: a non-positive caller cap is clamped to one account without becoming unbounded",
        low_cap_surface.get("cap") == 1
        and low_cap_surface.get("accounts_scanned") == 1
        and low_cap_surface.get("capped") is True
        and len(low_cap_surface.get("accounts", [])) == 1)
    usage_def = psql_mig(
        "SELECT pg_get_functiondef('core.owner_account_usage_surface(int)'::regprocedure);")
    add("A BOUNDED: the broad owner lens result is MATERIALIZED once per Platform request",
        "WITH source AS MATERIALIZED" in usage_def)
    add("A ALLOW: the seeded tenants have real non-zero Core activity, so admin can distinguish use from install-only",
        usage_by_id.get("ACCT-DEMO", {}).get("events", 0) >= 1
        and usage_by_id.get("ACCT-ACME", {}).get("events", 0) >= 1)
    broad_owner_surface = psql_as(
        READER, "SET search_path=core; SELECT core.owner_cost_surface(200)::text;")
    add("A LEAST-PRIVILEGE: reader is still DENIED the broader owner_cost_surface operator/cost payload",
        any(m in broad_owner_surface for m in DENIED))
    public_usage = psql_mig(
        "SELECT count(*) FROM pg_proc p "
        "CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a "
        "WHERE p.oid='core.owner_account_usage_surface(int)'::regprocedure "
        "AND a.grantee=0 AND a.privilege_type='EXECUTE';")
    add("A LEAST-PRIVILEGE: PUBLIC has no EXECUTE on owner_account_usage_surface",
        public_usage == "0")
    live_111 = last_value(psql_as(READER, "SET search_path=core; SELECT core.installation_is_live('111');"))
    live_unknown = last_value(psql_as(READER, "SET search_path=core; SELECT core.installation_is_live('999');"))
    add("A ALLOW: example_platform_reader CAN call installation_is_live('111') and gets true for a live install",
        live_111 == "t")
    add("A ALLOW: installation_is_live('999') returns false for an UNKNOWN installation id (fail-closed)",
        live_unknown == "f")

    # unknown installation id → NULL (reveals nothing).
    r_unknown = last_value(psql_as(READER, "SET search_path=core; "
                                           "SELECT COALESCE(core.effect_for_installation('999')::text,'NULL');"))
    add("A ALLOW: an UNKNOWN installation id returns NULL (an unmapped id reveals nothing)", r_unknown == "NULL")

    # ── A2. ALLOW — the reader CAN call the REPO-LEVEL substance fns, all content-free. ──────────────────────
    repos_obj = None
    try:
        repos_obj = json.loads(last_value(psql_as(READER, "SET search_path=core; SELECT core.repos_for_installation('111')::text;")))
    except Exception:
        repos_obj = None
    add("A2 ALLOW: reader CAN call repos_for_installation('111') and sees the demo repo with a last_at",
        isinstance(repos_obj, list) and any(isinstance(x, dict) and x.get("repo") == "demoorg/demo-repo" and x.get("last_at")
                                            for x in (repos_obj or [])))
    add("A2 ALLOW: repos_for_installation entries are content-free {repo,last_at} ONLY",
        isinstance(repos_obj, list) and all(isinstance(x, dict) and set(x.keys()) <= {"repo", "last_at"} for x in (repos_obj or [])))

    ins = None
    try:
        ins = json.loads(last_value(psql_as(READER, "SET search_path=core; "
                                                    "SELECT core.repo_insights_for_installation('111','demoorg/demo-repo')::text;")))
    except Exception:
        ins = None
    add("A2 ALLOW: reader CAN call repo_insights_for_installation → {repo,recent,hotspots,couplings}",
        isinstance(ins, dict) and {"repo", "recent", "hotspots", "couplings"}.issubset(set(ins.keys())))
    add("A2 ALLOW: recent[] content-free {kind,path,branch,at} ONLY + reflects the seeded warn (app/models.py)",
        isinstance(ins, dict) and isinstance(ins.get("recent"), list)
        and all(isinstance(it, dict) and set(it.keys()) <= {"kind", "path", "branch", "at"} for it in ins.get("recent", []))
        and any(it.get("path") == "app/models.py" for it in ins.get("recent", [])))
    add("A2 ALLOW: hotspots[] content-free {path,recent,prior,total,first_at,last_at} ONLY + contended path (recent>0)",
        isinstance(ins, dict) and isinstance(ins.get("hotspots"), list)
        and all(isinstance(it, dict) and set(it.keys()) <= {"path", "recent", "prior", "total", "first_at", "last_at"} for it in ins.get("hotspots", []))
        and any(it.get("path") == "app/models.py" and (it.get("recent") or 0) >= 1 for it in ins.get("hotspots", [])))
    add("A2 ALLOW: repo_insights carries the window (window_hours) used for the hotspot counts",
        isinstance(ins, dict) and isinstance(ins.get("window_hours"), int) and ins.get("window_hours") >= 1)
    add("A2 ALLOW: couplings[] content-free {a,b,strength,lift,co} ONLY + surfaces the floor-passing pair",
        isinstance(ins, dict) and isinstance(ins.get("couplings"), list)
        and all(isinstance(it, dict) and set(it.keys()) <= {"a", "b", "strength", "lift", "co"} for it in ins.get("couplings", []))
        and any(it.get("a") == "app/auth.py" and it.get("b") == "app/models.py" for it in ins.get("couplings", [])))
    r_ins_unknown = last_value(psql_as(READER, "SET search_path=core; "
                                               "SELECT COALESCE(core.repo_insights_for_installation('999','x/y')::text,'NULL');"))
    add("A2 ALLOW: repo_insights for an UNKNOWN installation → NULL", r_ins_unknown == "NULL")

    # ── A3. ALLOW — file-level detail: the individual timestamped events + this file's co-change partners. ─────
    fins = None
    try:
        fins = json.loads(last_value(psql_as(READER, "SET search_path=core; "
              "SELECT core.file_insights_for_installation('111','demoorg/demo-repo','app/models.py')::text;")))
    except Exception:
        fins = None
    add("A3 ALLOW: reader CAN call file_insights → {repo,path,total,first_at,last_at,events,partners}",
        isinstance(fins, dict)
        and {"repo", "path", "total", "first_at", "last_at", "events", "partners"}.issubset(set(fins.keys())))
    add("A3 ALLOW: events[] content-free {at,kind,change} ONLY (kind warned|serialized, change a bounded PR id or null) + reflects the seeded warn (total>=1)",
        isinstance(fins, dict) and isinstance(fins.get("events"), list) and (fins.get("total") or 0) >= 1
        and all(isinstance(it, dict) and set(it.keys()) <= {"at", "kind", "change"}
                and it.get("kind") in ("warned", "serialized")
                and (it.get("change") is None or (isinstance(it.get("change"), str) and 0 < len(it["change"]) <= 64))
                for it in fins.get("events", [])))
    add("A3 ALLOW: partners[] content-free {partner,strength,lift,co} ONLY + includes the floor-passing partner",
        isinstance(fins, dict) and isinstance(fins.get("partners"), list)
        and all(isinstance(it, dict) and set(it.keys()) <= {"partner", "strength", "lift", "co"} for it in fins.get("partners", []))
        and any(it.get("partner") == "app/auth.py" for it in fins.get("partners", [])))
    add("A3 ALLOW: file_insights for an UNKNOWN installation → NULL",
        last_value(psql_as(READER, "SET search_path=core; "
                   "SELECT COALESCE(core.file_insights_for_installation('999','x/y','z')::text,'NULL');")) == "NULL")

    # ── A4. ALLOW — the "now / needs attention" read (current in-flight contention). ─────────────────────────
    now = None
    try:
        now = json.loads(last_value(psql_as(READER, "SET search_path=core; "
              "SELECT core.now_for_installation('111')::text;")))
    except Exception:
        now = None
    add("A4 ALLOW: reader CAN call now_for_installation → {in_flight,contended} (in_flight = DISTINCT live changes, an int)",
        isinstance(now, dict) and {"in_flight", "contended"}.issubset(set(now.keys()))
        and isinstance(now.get("in_flight"), int))
    add("A4 ALLOW: contended[] is content-free {repo,path,agents,waiting,since,changes} ONLY",
        isinstance(now, dict) and isinstance(now.get("contended"), list)
        and all(isinstance(it, dict) and set(it.keys()) <= {"repo", "path", "agents", "waiting", "since", "changes"} for it in now.get("contended", [])))
    add("A4 ALLOW: contended[].changes is a content-free, bounded list of PR ids (no body)",
        isinstance(now, dict)
        and all(isinstance(it.get("changes", []), list)
                and all(isinstance(ch, str) and 0 < len(ch) <= 64 for ch in it.get("changes", []))
                for it in now.get("contended", [])))
    add("A4 ALLOW: now_for_installation for an UNKNOWN installation → NULL",
        last_value(psql_as(READER, "SET search_path=core; "
                   "SELECT COALESCE(core.now_for_installation('999')::text,'NULL');")) == "NULL")

    # ── B. DENY-WRITE — the reader cannot write through the gate. ────────────────────────────────────────────
    w = psql_as(READER, "SET search_path=core; "
                        f"SELECT core.record_push_with_authority('demoorg/demo-repo','main','{SHA}');")
    add("B DENY-WRITE: the reader is DENIED record_push_with_authority (permission denied — granted to the App "
        "only, never a reader)", any(m in w for m in DENIED))
    hp_write = psql_mig(f"SELECT has_function_privilege('{READER}',"
                        f"'core.record_push_with_authority(text,text,text,text)','EXECUTE');")
    add("B DENY-WRITE: has_function_privilege(reader, record_push_with_authority) is FALSE", hp_write == "f")

    # ── C. DENY-GRAPH — the reader cannot read the moat (code_node / code_edge). ─────────────────────────────
    g_node = psql_as(READER, "SET search_path=core; SELECT count(*) FROM core.code_node;")
    add("C DENY-GRAPH: the reader is DENIED SELECT on core.code_node (the moat — no table grant)",
        any(m in g_node for m in DENIED))
    g_edge = psql_as(READER, "SET search_path=core; SELECT count(*) FROM core.code_edge;")
    add("C DENY-GRAPH: the reader is DENIED SELECT on core.code_edge (the moat — no table grant)",
        any(m in g_edge for m in DENIED))
    for tbl in ("code_node", "code_edge"):
        hp_tbl = psql_mig(f"SELECT has_table_privilege('{READER}','core.{tbl}','SELECT');")
        add(f"C DENY-GRAPH: has_table_privilege(reader, core.{tbl}, SELECT) is FALSE", hp_tbl == "f")
    # CONTROL: the owner (migrator) DOES see the graph row → the denial is a real privilege difference.
    owner_node = last_value(psql_mig("SET search_path=core; SET core.current_account='ACCT-DEMO'; "
                                     "SELECT count(*) FROM core.code_node WHERE account_id='ACCT-DEMO';"))
    add("C CONTROL: the owner (migrator) CAN read core.code_node (>=1 row) — the reader's denial is a real "
        "privilege difference, not a missing table", owner_node.isdigit() and int(owner_node) >= 1)

    # ── D. DENY-DIRECT-INTERNAL — the reader cannot call _effect_for_account(<arbitrary account>) directly. ──
    d = psql_as(READER, "SET search_path=core; SELECT core._effect_for_account('ACCT-ACME')::text;")
    add("D DENY-DIRECT: the reader is DENIED the internal _effect_for_account (PUBLIC-revoked) — it cannot bypass "
        "the installation→account resolution to read an arbitrary tenant", any(m in d for m in DENIED))
    hp_internal = psql_mig(f"SELECT has_function_privilege('{READER}','core._effect_for_account(text)','EXECUTE');")
    add("D DENY-DIRECT: has_function_privilege(reader, _effect_for_account) is FALSE", hp_internal == "f")

    # ── CONTROL — the reader's grants are exactly the intended platform EXECUTEs (and the owner has them too). ─
    for fn in ("core.effect_for_installation(text)", "core.list_installation_ids()",
               "core.list_installation_accounts()",
               "core.owner_account_usage_surface(int)",
               "core.installation_is_live(text)",
               "core.repos_for_installation(text)", "core.repo_insights_for_installation(text,text,int)",
               "core.file_insights_for_installation(text,text,text)",
               "core.now_for_installation(text)"):
        hp = psql_mig(f"SELECT has_function_privilege('{READER}','{fn}','EXECUTE');")
        add(f"CONTROL: the reader HAS EXECUTE on {fn} (its intended surface)", hp == "t")
        hp_owner = psql_mig(f"SELECT has_function_privilege('veripsa_migrator','{fn}','EXECUTE');")
        add(f"CONTROL: the owner (migrator) also HAS EXECUTE on {fn}", hp_owner == "t")
    # the OTHER buyer/seat classes must NOT have the platform fns (the reader is the only non-App grantee).
    for role in NON_READER_BUYERS:
        hp = psql_mig(f"SELECT has_function_privilege('{role}','core.effect_for_installation(text)','EXECUTE');")
        add(f"CONTROL: buyer/seat class {role} has NO EXECUTE on effect_for_installation (reader-only + App)",
            hp == "f")
        hp_usage = psql_mig(
            f"SELECT has_function_privilege('{role}','core.owner_account_usage_surface(int)','EXECUTE');")
        add(f"CONTROL: buyer/seat class {role} has NO EXECUTE on owner_account_usage_surface",
            hp_usage == "f")
    # schema USAGE present (so the reader can reach the fns at all).
    usage = psql_mig(f"SELECT has_schema_privilege('{READER}','core','USAGE');")
    add("CONTROL: the reader HAS USAGE on schema core (so it can reach its platform fns)", usage == "t")

    # ── verdict ──────────────────────────────────────────────────────────────────────────────────────────────
    drop()
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    failed = [label for label, ok in checks if not ok]
    print(f"\n-- {passed}/{total} assertions passed "
          "(extraction byte-identical + reader ALLOW platform fns content-free + DENY write/graph/internal) --")
    if failed:
        print(f"\n[FAIL] {len(failed)} assertion(s) FAILED — the platform read path or its least-privilege wall "
              "has a hole:")
        for f in failed[:40]:
            print(f"   - {f}")
        print("\nPLATFORM READER GATE: FAIL")
        sys.exit(1)
    print("\nHONEST: effect_surface() is byte-identical after extraction; example_platform_reader can read a "
          "specific installation's content-free effect (and enumerate ids) but CANNOT write, read the code "
          "graph, or call the internal account-parameterized helper directly.")
    print("PLATFORM READER GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never leak the scratch DB on an unexpected error
        drop()
        print(f"\n[FAIL] unexpected error: {e}")
        print("PLATFORM READER GATE: FAIL")
        sys.exit(1)
