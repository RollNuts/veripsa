#!/usr/bin/env python3
"""BILLING ROLE — the least-privilege, plan-set-ONLY write seam for a separate entitlement component.

THE NEED: a separately deployed future Marketplace/entitlement handler
verifies the billing authority, resolves (org/install → gh_account_id, plan), and must then flip that ONE customer's Core
plan so Core's coverage/abuse gate (_account_over_quota / _plan_file_limit) matches the VERIFIED entitlement. Today
the web has only a READ-ONLY Core connection and its persistEntitlement is a stubbed no-op waiting on THIS seam.
It needs a WRITE-capable-for-billing connection that can call ONLY the plan setters
— NOT the App's full write surface, NOT any table, NOT any other authority fn, NOT any read surface.

THE SURFACE (additive):
  * role veripsa_billing  — NOLOGIN in db/roles.sql (LOGIN+password out-of-band on prod, exactly like
                            example_platform_reader). Its ENTIRE reachable surface = USAGE on schema core +
                            EXECUTE on core.set_account_plan_with_authority(text,text,timestamptz) and its
                            installation-keyed sibling. NO table grant, NO other *_with_authority fn, NO read
                            surface. The WRITE-side mirror of the platform reader.
  * the existing App grant (GRANT … TO veripsa_app) is UNCHANGED — veripsa_billing is ADDED alongside it.

LEAST-PRIVILEGE NOW ENFORCED (audit iter-5 P3). Postgres grants EXECUTE to PUBLIC by DEFAULT on every CREATE
FUNCTION, and the READ surfaces (account_surface / main_impact_surface / split_candidates / board_surface /
effect_surface / the *_surface lenses / coordinate_* / change_* / the internal helpers) historically did NOT
strip it — so veripsa_billing could EXECUTE ~40 core fns via PUBLIC, while the doc/this test CLAIMED a tiny
surface. The PUBLIC default is now stripped schema-wide (db/schema/99_least_privilege.sql) + on the named read
surfaces explicitly; the explicit role grants survive (reader/writer/steward/app), so NO legit path breaks. This
gate now asserts the COMPLEMENT against the live catalog: veripsa_billing has EXECUTE on EXACTLY the two plan
setters and FALSE on EVERY OTHER core function — so the least-privilege claim is ENFORCED, not merely asserted.

WHY EXECUTE IS ENOUGH (and the most it can do): set_account_plan_with_authority is SECURITY DEFINER — it runs as
the migrator owner, resolves 'ACCT-GH-'||<gh_account_id>, pins core.current_account to that account, and arms the
governed write (mark_governed_write) ITSELF before writing core.account.plan. So a caller needs ONLY EXECUTE; the
caller's OWN role never touches a table, never arms a token. (It also carries the F1 ownership-authority check:
a no-installation FIRST entitlement — exactly the billing-webhook case, which pins no tenant — is ALLOWED, with
the platform's verified Marketplace/entitlement authority.)

WHAT THIS GATE PROVES (live, on the ephemeral test Postgres — never grep; the only truth is the running DB):

  1. ALLOW + ENFORCED — connecting AS veripsa_billing, it CAN SELECT core.set_account_plan_with_authority(
     'ACCT-GH-123','pro') (it returns the resolved account), the plan column is then 'pro', AND the abuse gate
     reflects it: core._account_over_quota('ACCT-GH-123') = NULL for a footprint
     that is over the tightened free line but still within the Pro plan line. So the seam end-to-end:
     veripsa_billing calls it → the plan is set → the wall is lifted.

  2. DENY-TABLE-READ — veripsa_billing CANNOT SELECT * FROM core.account / core.code_node / core.code_edge /
     core.event — permission denied (no table grant). It cannot read the plan it just set by reading the table,
     nor read the moat (the code graph).

  3. DENY-TABLE-WRITE — veripsa_billing CANNOT INSERT / UPDATE / DELETE any core table (permission denied) — it
     cannot set a plan by writing core.account directly, bypassing the gated, tenant-pinned, normalizing setter.

  4. DENY-OTHER-AUTHORITY — veripsa_billing CANNOT EXECUTE another *_with_authority fn:
     transfer_repo_coordinate_with_authority(text,text) / set_plan_limit_with_authority(text,int) — permission
     denied. The plan SETTER is its ONLY authority reach; it cannot transfer a repo or move a plan-file limit.

  5. PERIMETER INTACT — a buyer LOGIN role (veripsa_demo_agent3, a real connecting seat that inherits
     veripsa_writer) STILL cannot call the setter — permission denied — so the buyer-self-upgrade perimeter the
     marketplace gate established is unbroken by the new role (a buyer cannot upgrade its OWN account).

  6. COMPLEMENT (audit iter-5 P3) — the least-privilege claim ENFORCED against the live catalog: enumerate EVERY
     core function and require veripsa_billing to have EXECUTE on EXACTLY the two plan setters and FALSE on EVERY
     OTHER one (no PUBLIC-default leak across the ~40 read surfaces it used to reach). A new fn that ships
     PUBLIC-EXECUTE-able lands RED here, so the claim cannot silently regress.

  CONTROL: has_function_privilege / has_table_privilege confirm veripsa_billing's grants are EXACTLY the one
  EXECUTE (+ schema USAGE) and NOTHING else — the denials are a real privilege difference, not dead probes; the
  owner (migrator) has it all; and the App keeps EXECUTE on the setter (its path is untouched).

Run:  python3 tests/test_billing_role.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# PROCESS-UNIQUE (parallel-safe): this gate bootstraps + drops its own DB, like db/smoke.sh and the sibling
# security gates — a FIXED name would let concurrent runs drop each other's DB mid-run.
DB = "veripsa_billrole_" + str(os.getpid())

BILLING = "veripsa_billing"
# a real buyer LOGIN seat (created + LOGIN in db/roles.sql, inherits veripsa_writer) — the canonical buyer
# reachability probe (the writer/reader privilege classes are NOLOGIN, so a connecting role is the honest test).
BUYER = "veripsa_demo_agent3"
# the GitHub account id the billing webhook would carry; resolves to ACCT-GH-123 the same way the App resolves it.
GH_ID = "123"
ACCT = "ACCT-GH-" + GH_ID

# ── installation-keyed billing (set_account_plan_for_installation_with_authority) ──────────────────────────────
# The web platform holds the org's APP INSTALLATION id (not the numeric gh account id), so it sets the plan BY
# INSTALLATION and Core resolves installation→account authoritatively via core.installation_account. A KNOWN
# installation (one enter_installation_with_authority has routed → a real live map row) is billable; an UNKNOWN or
# REVOKED one is a clean no-op that mints/changes NOTHING. INST_KNOWN is entered by the App below so its row exists;
# INST_UNKNOWN never is.
INST_KNOWN = "inst-known-555"
INST_KNOWN_ACCT = "ACCT-GH-" + INST_KNOWN   # enter_installation_with_authority maps a fresh install to 'ACCT-GH-'||<inst>
INST_UNKNOWN = "inst-never-seen-999"
INST_UNKNOWN_ACCT = "ACCT-GH-" + INST_UNKNOWN  # the account that must NOT exist (no phantom from an unknown install)

# the OTHER authority fns veripsa_billing must NOT be able to call (App-delegation only — never the billing role).
OTHER_AUTHORITY = [
    "core.transfer_repo_coordinate_with_authority(text,text)",
    "core.set_plan_limit_with_authority(text,int)",
]
# the core tables veripsa_billing must have ZERO privilege on (the plan target itself + the moat + the ledger).
CORE_TABLES = ["account", "code_node", "code_edge", "event"]

DENIED = ("permission denied",)

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, passed))


def psql_mig(sql):
    """As the migrator (owner) — ground truth + fixtures + has_*_privilege probes."""
    dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr)


def psql_admin(sql):
    """As the cluster ADMIN/superuser (ADMIN_DSN) — the ONE thing the migrator cannot do: ALTER ROLE ... LOGIN.
    veripsa_billing ships NOLOGIN; on PROD the operator grants LOGIN+password out-of-band — here the gate grants
    LOGIN (peer auth, no password) IN ITS EPHEMERAL DB to exercise the deny/allow surface AS the billing role.
    Same authority + idiom test_platform_reader.py uses for example_platform_reader."""
    dsn = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr)


def psql_as(role, sql):
    """Run sql AS `role` (peer auth on localhost) — combined stdout+stderr so a permission-denied is visible."""
    dsn = f"postgresql://{role}@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr)


def last_value(out):
    """The LAST non-empty line — a multi-statement `SET ...; SELECT ...` prints a 'SET' ack first."""
    lines = [ln for ln in out.splitlines() if ln.strip() != ""]
    return lines[-1].strip() if lines else ""


def _plan_of(account):
    """Ground-truth plan column for `account` (migrator, pinned — account is FORCE-RLS)."""
    return last_value(psql_mig(f"SET search_path=core; SET core.current_account='{account}'; "
                               f"SELECT plan FROM core.account WHERE account_id='{account}';"))


def _over_quota(account):
    """core._account_over_quota('{account}') with the account pinned (its callers always pin it first).
    Returns the over-dimension string, or 'NULL' when under the line (= allow)."""
    return last_value(psql_mig(f"SET search_path=core; SET core.current_account='{account}'; "
                               f"SELECT COALESCE(core._account_over_quota('{account}'),'NULL');"))


def _account_exists(account):
    """Does an account ROW exist? (migrator, pinned — account is FORCE-RLS). Used to prove an UNKNOWN installation
    mints NO phantom account. Returns True/False."""
    return last_value(psql_mig(f"SET search_path=core; SET core.current_account='{account}'; "
                               f"SELECT EXISTS(SELECT 1 FROM core.account WHERE account_id='{account}');")) == "t"


def _ingest_as_app(inst, repo, sha, name):
    """As veripsa_app: enter a real installation (pins the tenant) then ingest one node — the legit live write
    path, so the account gets a real footprint over the tightened free line. Returns the last output line."""
    g = '{"nodes":[{"id":"n-%s","kind":"file","path":"%s.py","name":"%s"}],"edges":[]}' % (name, name, name)
    return last_value(psql_as("veripsa_app", "SET search_path=core; "
                              f"SELECT core.enter_installation_with_authority('{inst}'); "
                              f"SELECT core.ingest_graph_with_authority('{g}'::jsonb,'{repo}','main','{sha}');"))


def bootstrap():
    """roles + schema.sql + demo seats (db/bootstrap_local.sh); TIGHTEN the free line so the abuse wall bites on
    a small footprint (so the paid-override is a REAL lift, not a vacuous under-cap pass); a code_node graph row
    so the DENY-TABLE-READ probe on the moat is not vacuous; then ALTER veripsa_billing LOGIN in THIS ephemeral
    DB only (prod sets LOGIN+password out-of-band) so the gate can connect AS the billing role."""
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("[FAIL] bootstrap (roles + schema.sql + seats)")
        print((r.stdout + r.stderr)[-2000:])
        sys.exit(2)

    # TIGHTEN the line so the wall BITES after ONE stored graph unit (the over-quota check is a PRE-WRITE
    # "already over?" test, so the first crossing write still lands; the next is refused). graph_units line = 0 →
    # after one node is stored the account is over. Same technique as test_marketplace_billing.py.
    # graph_units is now a PER-PLAN HARD line (core._plan_graph_units_limit, NOT _free_line): the FREE baseline
    # below is walled via the per-plan FREE line=0; once lifted to 'pro' its tiny footprint is under the pro line
    # (80000) so the override lift is real. repos/events stay on _free_line (free-tier) and are wide-open.
    psql_mig("SET search_path=core; "
             "SELECT core.set_free_line_with_authority('free_max_repos',100000); "
             "SELECT core.set_free_line_with_authority('free_max_events',1000000); "
             "SELECT core.set_plan_graph_units_limit_with_authority('free',0);")

    # A graph row so the DENY-TABLE-READ SELECT probe on core.code_node is not vacuous (the moat genuinely has a
    # row to be denied). code_node is FORCE-RLS + governed-write: pin the account + arm mark_governed_write in ONE
    # txn (both txn-local). Same fixture idiom as test_platform_reader.py.
    g = psql_mig("SET search_path=core; "
                 "BEGIN; "
                 "SELECT set_config('core.current_account','ACCT-DEMO',true); "
                 "SELECT core.mark_governed_write('code_node'); "
                 "INSERT INTO core.code_node(account_id,node_id,node_kind,path,name,language,repo) "
                 "VALUES ('ACCT-DEMO','N1','file','app/models.py','app/models.py','python','demoorg/demo-repo'); "
                 "COMMIT;")
    if "ERROR" in g:
        print("[FAIL] could not seed a code_node row (the DENY-TABLE-READ moat control would be vacuous):")
        print(g[-1500:])
        sys.exit(2)

    # LOGIN for the billing role so the gate can connect AS it (prod sets LOGIN+password out-of-band; roles.sql
    # ships it NOLOGIN). ALTER ROLE needs the cluster admin/superuser (the migrator lacks CREATEROLE) — same
    # authority db/roles.sql uses for the local fixtures. Peer auth on localhost → no password for the gate.
    login = psql_admin(f"ALTER ROLE {BILLING} LOGIN;")
    if "ERROR" in login or "permission denied" in login:
        print(f"[FAIL] could not grant LOGIN to {BILLING} via ADMIN_DSN (the gate cannot connect as the billing "
              "role to exercise its surface):")
        print(login[-1500:])
        sys.exit(2)


def drop():
    # reset the cluster-global role back to NOLOGIN (it ships NOLOGIN in roles.sql; leaving a stray LOGIN bit set
    # on a shared cluster after the gate is untidy). Best-effort.
    subprocess.run(["psql", os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres"),
                    "-tAc", f"ALTER ROLE {BILLING} NOLOGIN;"], capture_output=True, text=True)
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


def main():
    print("VERIPSA BILLING ROLE — least-privilege plan-set-only write seam for the separate web platform")
    print(f"(scratch DB: {DB})")
    bootstrap()

    # ── ESTABLISH THE WALL ON A FREE BASELINE: a free account with a footprint over the tightened line is WALLED,
    #    so the paid-override below is a REAL lift (not a vacuous empty-account pass). Seed via the App gate. ─────
    _ingest_as_app("inst-free", "freeorg/repo", "1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa", "freeone")  # crosses, lands
    _ingest_as_app("inst-free", "freeorg/repo2", "2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb", "freetwo")  # walled
    add("BASELINE: a FREE account with a footprint over the tightened line IS over-quota (the wall really bites — "
        "so the paid lift below is real)", _over_quota("ACCT-GH-inst-free") in ("graph_units", "repos", "events"))
    # give ACCT (the billing target) the SAME over-the-line footprint while still free, so lifting it is meaningful.
    _ingest_as_app(GH_ID, "buyerorg/repo", "3333cccc3333cccc3333cccc3333cccc3333cccc", "buyerone")
    _ingest_as_app(GH_ID, "buyerorg/repo2", "4444dddd4444dddd4444dddd4444dddd4444dddd", "buyertwo")
    add(f"BASELINE: {ACCT} is FREE + over-quota BEFORE the billing role sets a plan (a real wall to lift)",
        _plan_of(ACCT) == "free" and _over_quota(ACCT) != "NULL")

    # ── 1. ALLOW + ENFORCED — AS veripsa_billing, SELECT the setter; the plan is set; the wall is lifted. ───────
    set_out = psql_as(BILLING, f"SET search_path=core; SELECT core.set_account_plan_with_authority('{GH_ID}','pro');")
    add("1 ALLOW: veripsa_billing CAN SELECT core.set_account_plan_with_authority('123','pro') — no permission "
        "denied", "permission denied" not in set_out and "ERROR" not in set_out)
    add(f"1 ALLOW: the setter returned the resolved account ({ACCT}) to the billing caller", last_value(set_out) == ACCT)
    add("1 ENFORCED: core.account.plan is now 'pro' (set through the gated SECURITY DEFINER setter, by a role with "
        "ONLY EXECUTE on it — it never touched a table)", _plan_of(ACCT) == "pro")
    add("1 ENFORCED: core._account_over_quota now returns NULL for this footprint — it is over the FREE line but "
        "inside the Pro plan line (the coverage/abuse gate matches the entitlement)", _over_quota(ACCT) == "NULL")

    # ── 2. DENY-TABLE-READ — veripsa_billing cannot SELECT any core table (no table grant). ─────────────────────
    for tbl in CORE_TABLES:
        out = psql_as(BILLING, f"SET search_path=core; SELECT count(*) FROM core.{tbl};")
        add(f"2 DENY-READ: veripsa_billing is DENIED SELECT on core.{tbl} (no table grant — it cannot even read "
            f"the plan it set, nor the moat)", any(m in out for m in DENIED))
        hp = last_value(psql_mig(f"SELECT has_table_privilege('{BILLING}','core.{tbl}','SELECT');"))
        add(f"2 DENY-READ: has_table_privilege(veripsa_billing, core.{tbl}, SELECT) is FALSE", hp == "f")
    # CONTROL: the owner (migrator) CAN read core.account (>=1 row) → the denial is a real privilege difference.
    owner_acct = last_value(psql_mig(f"SET search_path=core; SET core.current_account='{ACCT}'; "
                                     f"SELECT count(*) FROM core.account WHERE account_id='{ACCT}';"))
    add("2 CONTROL: the owner (migrator) CAN read core.account (>=1 row) — the billing role's denial is a real "
        "privilege difference, not a missing table", owner_acct.isdigit() and int(owner_acct) >= 1)

    # ── 3. DENY-TABLE-WRITE — veripsa_billing cannot INSERT / UPDATE / DELETE any core table. ───────────────────
    # The most tempting bypass is a direct UPDATE of core.account.plan (skipping the gated, normalizing setter).
    upd = psql_as(BILLING, f"SET search_path=core; UPDATE core.account SET plan='enterprise' WHERE account_id='{ACCT}';")
    add("3 DENY-WRITE: veripsa_billing is DENIED UPDATE on core.account (it cannot set a plan by writing the table "
        "directly, bypassing the gated setter)", any(m in upd for m in DENIED))
    ins = psql_as(BILLING, "SET search_path=core; INSERT INTO core.account(account_id,display_name) VALUES ('ACCT-X','x');")
    add("3 DENY-WRITE: veripsa_billing is DENIED INSERT on core.account", any(m in ins for m in DENIED))
    dele = psql_as(BILLING, f"SET search_path=core; DELETE FROM core.account WHERE account_id='{ACCT}';")
    add("3 DENY-WRITE: veripsa_billing is DENIED DELETE on core.account", any(m in dele for m in DENIED))
    # the denied direct UPDATE changed nothing — the plan is still exactly what the gated setter wrote ('pro').
    add("3 DENY-WRITE: the denied direct UPDATE wrote NOTHING — core.account.plan is still 'pro' (the gated "
        "setter's value)", _plan_of(ACCT) == "pro")
    for verb in ("INSERT", "UPDATE", "DELETE"):
        hp = last_value(psql_mig(f"SELECT has_table_privilege('{BILLING}','core.account','{verb}');"))
        add(f"3 DENY-WRITE: has_table_privilege(veripsa_billing, core.account, {verb}) is FALSE", hp == "f")

    # ── 4. DENY-OTHER-AUTHORITY — veripsa_billing cannot EXECUTE another *_with_authority fn. ───────────────────
    transfer = psql_as(BILLING, "SET search_path=core; "
                                "SELECT core.transfer_repo_coordinate_with_authority('ACCT-GH-999','o/r');")
    add("4 DENY-AUTHORITY: veripsa_billing is DENIED transfer_repo_coordinate_with_authority (App-delegation only "
        "— the billing role cannot transfer/purge a repo coordinate)", any(m in transfer for m in DENIED))
    setlimit = psql_as(BILLING, "SET search_path=core; SELECT core.set_plan_limit_with_authority('free',0);")
    add("4 DENY-AUTHORITY: veripsa_billing is DENIED set_plan_limit_with_authority (it cannot move a plan's "
        "file-coverage limit — the plan SETTER is its ONLY authority reach)", any(m in setlimit for m in DENIED))
    for fn in OTHER_AUTHORITY:
        hp = last_value(psql_mig(f"SELECT has_function_privilege('{BILLING}','{fn}','EXECUTE');"))
        add(f"4 DENY-AUTHORITY: has_function_privilege(veripsa_billing, {fn}) is FALSE", hp == "f")

    # ── 5. PERIMETER INTACT — a buyer LOGIN role still cannot call the setter (no buyer self-upgrade). ──────────
    buyer = psql_as(BUYER, f"SET search_path=core; SELECT core.set_account_plan_with_authority('{GH_ID}','enterprise');")
    add("5 PERIMETER: a buyer LOGIN role (veripsa_demo_agent3) is STILL DENIED the setter — permission denied "
        "(the buyer-self-upgrade perimeter is intact; the new role did not widen it)", any(m in buyer for m in DENIED))
    add("5 PERIMETER: the buyer's denied call changed NOTHING — core.account.plan is still 'pro'",
        _plan_of(ACCT) == "pro")
    hp_buyer = last_value(psql_mig("SELECT has_function_privilege('veripsa_demo_agent3',"
                                   "'core.set_account_plan_with_authority(text,text,timestamptz)','EXECUTE');"))
    add("5 PERIMETER: has_function_privilege(buyer, set_account_plan_with_authority) is FALSE", hp_buyer == "f")

    # ── 6. INSTALLATION-KEYED SETTER — set_account_plan_for_installation_with_authority resolves installation→account
    #    authoritatively via core.installation_account (Core owns the map), sets the plan via the ONE setter path, and
    #    NO-OPs (minting/changing nothing) on an unknown or revoked installation. The web platform holds the
    #    INSTALLATION id, not the gh account id, so this is its real entry point. ─────────────────────────────────
    inst_sig = "core.set_account_plan_for_installation_with_authority(text,text,timestamptz)"

    # (a) seed a KNOWN installation the way prod does: the App enters it (enter_installation_with_authority writes the
    #     authoritative core.installation_account row + maps it to ACCT-GH-<inst>), then ingest over the tightened free
    #     line so the wall really bites — so lifting it by setting a plan BY INSTALLATION is a REAL lift.
    _ingest_as_app(INST_KNOWN, "knownorg/repo", "5555eeee5555eeee5555eeee5555eeee5555eeee", "knownone")  # crosses, lands
    _ingest_as_app(INST_KNOWN, "knownorg/repo2", "6666ffff6666ffff6666ffff6666ffff6666ffff", "knowntwo")  # walled
    add("6 KNOWN-BASELINE: the entered installation maps to its account and is FREE + over-quota before the "
        "installation-keyed setter runs (a real wall to lift)",
        _plan_of(INST_KNOWN_ACCT) == "free" and _over_quota(INST_KNOWN_ACCT) != "NULL")

    # (b) ALLOW + RESOLVE + ENFORCED — AS veripsa_billing, set the plan BY INSTALLATION with a CAPITAL 'Pro' (the #401
    #     case test): the setter resolves the installation to its account, returns it, the plan is the canonical
    #     LOWERCASE 'pro', and the abuse wall lifts (the SAME footprint that walled this FREE account → NULL).
    inst_set = psql_as(BILLING, f"SET search_path=core; "
                                f"SELECT core.set_account_plan_for_installation_with_authority('{INST_KNOWN}','Pro');")
    add("6 ALLOW: veripsa_billing CAN SELECT set_account_plan_for_installation_with_authority(known_inst,'Pro') — "
        "no permission denied", "permission denied" not in inst_set and "ERROR" not in inst_set)
    add(f"6 RESOLVE: the installation-keyed setter resolved the installation→account authoritatively and returned it "
        f"({INST_KNOWN_ACCT})", last_value(inst_set) == INST_KNOWN_ACCT)
    add("6 #401 LOWERCASE: a CAPITAL 'Pro' set BY INSTALLATION is stored canonical-lowercase 'pro' (the single "
        "setter path's lower(btrim()) holds through the installation entry point)", _plan_of(INST_KNOWN_ACCT) == "pro")
    add("6 ENFORCED: core._account_over_quota for the resolved account is now NULL for this footprint — setting the plan BY "
        "INSTALLATION lifted the SAME footprint that walled it FREE (the abuse gate matches the entitlement)",
        _over_quota(INST_KNOWN_ACCT) == "NULL")

    # (c) REVOKED installation ⇒ unresolved NO-OP that CHANGES NOTHING. The routing row is retained after uninstall/
    #     suspend for reinstall continuity, but revoked_at marks the link not-live, so billing must refuse to apply a
    #     plan through that dead link.
    psql_mig("SET search_path=core; "
             f"UPDATE core.installation_account SET revoked_at=now() WHERE installation_id='{INST_KNOWN}';")
    revoked = psql_as(BILLING, f"SET search_path=core; "
                              f"SELECT COALESCE(core.set_account_plan_for_installation_with_authority('{INST_KNOWN}',"
                              f"'enterprise'),'NULL');")
    add("6 REVOKED: a revoked installation resolves to NULL (no billing through an uninstalled/suspended link)",
        last_value(revoked) == "NULL")
    add("6 REVOKED: the revoked installation-keyed no-op changed NOTHING — the plan is still 'pro'",
        _plan_of(INST_KNOWN_ACCT) == "pro")
    psql_mig("SET search_path=core; "
             f"UPDATE core.installation_account SET revoked_at=NULL WHERE installation_id='{INST_KNOWN}';")
    add("6 RE-LIVE: clearing revoked_at makes the known installation live again for the remaining perimeter probes",
        last_value(psql_mig("SET search_path=core; "
                            f"SELECT core.installation_is_live('{INST_KNOWN}');")) == "t")

    # (d) UNKNOWN installation ⇒ unresolved NO-OP that MINTS NOTHING (billing, not onboarding — it must NOT call
    #     enter_installation_with_authority, which would lazily provision a phantom account + map row).
    assert not _account_exists(INST_UNKNOWN_ACCT), "fixture: the unknown-installation account must not pre-exist"
    unk = psql_as(BILLING, f"SET search_path=core; "
                           f"SELECT COALESCE(core.set_account_plan_for_installation_with_authority('{INST_UNKNOWN}','pro'),'NULL');")
    add("6 UNKNOWN: an unknown installation resolves to NULL (a content-free unresolved no-op — no account to bill)",
        last_value(unk) == "NULL")
    add("6 NO-PHANTOM: the unknown installation minted NO account row (ACCT-GH-<unknown> does NOT exist — a purchase "
        "never conjures a tenant; it did not call the lazily-provisioning enter_installation_with_authority)",
        not _account_exists(INST_UNKNOWN_ACCT))
    add("6 NO-PHANTOM: the unknown installation minted NO core.installation_account map row either",
        last_value(psql_mig("SET search_path=core; "
                            f"SELECT EXISTS(SELECT 1 FROM core.installation_account WHERE installation_id='{INST_UNKNOWN}');")) == "f")

    # (e) PERIMETER — a buyer LOGIN role STILL cannot call the installation-keyed setter (no buyer self-upgrade via
    #     the new entry point either).
    buyer_inst = psql_as(BUYER, f"SET search_path=core; "
                                f"SELECT core.set_account_plan_for_installation_with_authority('{INST_KNOWN}','enterprise');")
    add("6 PERIMETER: a buyer LOGIN role (veripsa_demo_agent3) is STILL DENIED the installation-keyed setter — "
        "permission denied (no buyer self-upgrade through the new entry point)", any(m in buyer_inst for m in DENIED))
    add("6 PERIMETER: the buyer's denied installation-keyed call changed NOTHING — the plan is still 'pro'",
        _plan_of(INST_KNOWN_ACCT) == "pro")

    # (f) grant surface — veripsa_billing HAS EXECUTE on the installation-keyed setter; the buyer does NOT.
    add("6 GRANT: veripsa_billing HAS EXECUTE on set_account_plan_for_installation_with_authority (its installation "
        "entry point)", last_value(psql_mig(f"SELECT has_function_privilege('{BILLING}','{inst_sig}','EXECUTE');")) == "t")
    add("6 GRANT: has_function_privilege(buyer, set_account_plan_for_installation_with_authority) is FALSE",
        last_value(psql_mig(f"SELECT has_function_privilege('{BUYER}','{inst_sig}','EXECUTE');")) == "f")

    # ── CONTROL — veripsa_billing's grants are EXACTLY one EXECUTE (+ schema USAGE); App keeps its grant. ───────
    sig = "core.set_account_plan_with_authority(text,text,timestamptz)"
    add("CONTROL: veripsa_billing HAS EXECUTE on the plan setter (its one intended write reach)",
        last_value(psql_mig(f"SELECT has_function_privilege('{BILLING}','{sig}','EXECUTE');")) == "t")
    add("CONTROL: veripsa_billing HAS USAGE on schema core (so it can reach the setter)",
        last_value(psql_mig(f"SELECT has_schema_privilege('{BILLING}','core','USAGE');")) == "t")
    add("CONTROL: the owner (migrator) also HAS EXECUTE on the setter",
        last_value(psql_mig(f"SELECT has_function_privilege('veripsa_migrator','{sig}','EXECUTE');")) == "t")
    # the App's path is UNTOUCHED — it keeps EXECUTE on the setter (veripsa_billing is ADDED alongside, not instead).
    add("CONTROL: the App (veripsa_app) STILL HAS EXECUTE on the setter (its path is unchanged; the billing role "
        "is added alongside)", last_value(psql_mig(f"SELECT has_function_privilege('veripsa_app','{sig}','EXECUTE');")) == "t")
    # and veripsa_billing has NO CREATE on schema core (it cannot plant objects) — a tight least-privilege control.
    add("CONTROL: veripsa_billing has NO CREATE on schema core (USAGE only, never CREATE)",
        last_value(psql_mig(f"SELECT has_schema_privilege('{BILLING}','core','CREATE');")) == "f")

    # ── COMPLEMENT (audit iter-5 P3): the least-privilege claim ENFORCED against the catalog, not just documented.
    #    Postgres grants EXECUTE to PUBLIC by default on every CREATE FUNCTION; the read surfaces did not strip it,
    #    so veripsa_billing could EXECUTE ~40 core fns via PUBLIC while this gate CLAIMED a two-fn surface. The
    #    PUBLIC default is now stripped schema-wide (db/schema/99_least_privilege.sql); the explicit role grants
    #    survive. Assert the COMPLEMENT directly: enumerate EVERY core function and require veripsa_billing to have
    #    EXECUTE on EXACTLY the two plan setters and FALSE on EVERY OTHER one. A NEW core fn that ships PUBLIC-
    #    EXECUTE-able (the default reopening) lands RED here — the claim can never silently regress.
    SETTERS = {"set_account_plan_with_authority", "set_account_plan_for_installation_with_authority"}
    # all core functions (proname) the billing role CAN execute, one per line, via the live catalog.
    reach_out = psql_mig(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        f"WHERE n.nspname='core' AND has_function_privilege('{BILLING}', p.oid, 'EXECUTE') ORDER BY 1;")
    reachable = {ln.strip() for ln in reach_out.splitlines() if ln.strip() and "ERROR" not in ln}
    # total core fn count (so the complement is provably non-vacuous — there ARE many other fns to be denied).
    total_fns = last_value(psql_mig(
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='core';"))
    leaked = sorted(reachable - SETTERS)            # any non-setter fn billing can still reach = a least-priv hole
    missing_setters = sorted(SETTERS - reachable)    # a setter it should reach but cannot = a broken seam
    add(f"COMPLEMENT: veripsa_billing can EXECUTE EXACTLY the two plan setters and NOTHING else "
        f"(reachable={sorted(reachable)}; of {total_fns} core fns) — the least-privilege claim is ENFORCED, not "
        f"merely documented", reachable == SETTERS)
    add(f"COMPLEMENT: veripsa_billing has EXECUTE=FALSE on every NON-setter core fn (no PUBLIC-default leak) — "
        f"leaked={leaked}", not leaked)
    add(f"COMPLEMENT: veripsa_billing DOES retain EXECUTE on both plan setters (the seam still works) — "
        f"missing={missing_setters}", not missing_setters)
    # NON-VACUOUS control: there really ARE many other core fns (so "FALSE on every other" is a meaningful denial,
    # not an artifact of an empty schema). The owner has them all (a real privilege difference).
    add(f"COMPLEMENT CONTROL: the schema has many core fns ({total_fns} >= 50) so the complement denial is "
        f"non-vacuous", total_fns.isdigit() and int(total_fns) >= 50)
    owner_reach = psql_mig(
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='core' AND has_function_privilege('veripsa_migrator', p.oid, 'EXECUTE');")
    add("COMPLEMENT CONTROL: the owner (migrator) CAN execute (far) more than two core fns — the billing denial "
        "is a real privilege difference, not a missing schema",
        last_value(owner_reach).isdigit() and int(last_value(owner_reach)) > len(SETTERS))

    # ── verdict ──────────────────────────────────────────────────────────────────────────────────────────────
    drop()
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    failed = [label for label, ok in checks if not ok]
    print(f"\n-- {passed}/{total} assertions passed "
          "(billing role ALLOW the setter + ENFORCED on the wall; DENY table read/write, other authority fns; "
          "buyer perimeter intact) --")
    if failed:
        print(f"\n[FAIL] {len(failed)} assertion(s) FAILED — the billing write seam or its least-privilege wall "
              "has a hole:")
        for f in failed[:40]:
            print(f"   - {f}")
        print("\nBILLING ROLE GATE: FAIL")
        sys.exit(1)
    print("\nHONEST: veripsa_billing can ONLY call core.set_account_plan_with_authority (the plan is set + the "
          "abuse gate reflects it), and CANNOT read or write any core table, call any other authority fn, or let "
          "a buyer self-upgrade — its entire reach is the one gated setter (USAGE on schema core + EXECUTE on it).")
    print("BILLING ROLE GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never leak the scratch DB on an unexpected error
        drop()
        print(f"\n[FAIL] unexpected error: {e}")
        print("BILLING ROLE GATE: FAIL")
        sys.exit(1)
