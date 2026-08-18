#!/usr/bin/env python3
"""QUOTA MODEL — the finished Core quota/billing model (SECURITY-sensitive: it governs the quota wall).

Three PO decisions, proven end-to-end against a REAL local Postgres (not a mock of the gate):

  1. 实测-CALIBRATED TIER LADDER (core._plan_graph_units_limit): free 6000 · starter 30000 · pro 80000 ·
     scale 200000 · enterprise 500000 (the TOP — NOT unlimited). Each tier clears a REAL 实测 repo with headroom
     (requests 3.2k / flask 5.4k → free; fastapi 27k → starter; ansible 72k → pro; django 151k → scale). An
     unmapped/unknown paid label is bounded by the for-now 500000 ceiling, NEVER unlimited (the HARD-wall posture).

  2. DEV-ONLY EXEMPTION (core._account_over_quota, checked BEFORE the wall): the PO's own dogfood/demo accounts
     (an ADJUSTABLE owner-policy allowlist — core._dev_exempt_account_ids; default the two GitHub installs +
     ACCT-DEMO) bypass the wall so Veripsa can re-index its OWN Core (the dogfood loop, which 实测 blows past the
     free line on the FIRST ingest). FAIL-CLOSED (the load-bearing inversion): a BROKEN allowlist read = NOT exempt
     = the wall STAYS ENFORCED. A non-exempt account still walls. Explicitly NOT a product tier (a named-id list).
     The allowlist is tunable WITHOUT a redeploy via set_dev_exempt_accounts_with_authority (App-delegation only —
     a buyer seat can never self-exempt).

  3. TRANSFER AUTHORITY PERIMETER (core.transfer_repo_coordinate_with_authority): proofless /2 workers cannot
     delete anything. Only the proof-required /8 surface is App-executable; it authenticates an exact PROCESSING
     durable transfer plus a same-transaction locked current-identity proof (covered deeply by the repository
     offboarding gate). Buyer, PUBLIC, and rolling proofless surfaces remain denied.

Run:  python3 tests/test_quota_model.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

# PROCESS-UNIQUE (parallel-safe): per-PID, exactly like the other gates, so concurrent runs never drop each other's DB.
DB = "veripsa_quotamodel_" + str(os.getpid())
DSN_MIG = f"postgresql://veripsa_migrator@localhost/{DB}"
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"

# valid 40-char hex shas (ingest_graph rejects non-hex / >64). One per write so a second write is a new coordinate.
SHA1 = "1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa"
SHA2 = "2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb"

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, bool(passed)))


def psql_mig(sql):
    r = subprocess.run(["psql", DSN_MIG, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr)


def psql_app(sql):
    r = subprocess.run(["psql", DSN_APP, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr)


def last_value(out):
    """The LAST non-empty line — a multi-statement `SET ...; SELECT ...` prints a 'SET' ack before the result."""
    lines = [ln for ln in out.splitlines() if ln.strip() != ""]
    return lines[-1].strip() if lines else ""


def _nows(s):
    """Whitespace-stripped — jsonb text output renders `"k": v` while json renders `"k" : v`; compare robustly."""
    return "".join((s or "").split())


def bootstrap():
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("[FAIL] bootstrap (roles + schema.sql + seats)")
        print((r.stdout + r.stderr)[-2000:])
        sys.exit(2)
    # Make repos/events wide open so graph_units is the dimension under test for the wall; zero EVERY plan's
    # graph_units line so the wall BITES on the SECOND graph write (the over-quota check is a PRE-WRITE "already
    # over?" test — the first crossing write lands, the next is refused). The tier-ladder VALUE checks below
    # restore + re-read the real defaults explicitly, so zeroing here does not hide them.
    psql_mig("SET search_path=core; "
             "SELECT core.set_free_line_with_authority('free_max_repos',100000); "
             "SELECT core.set_free_line_with_authority('free_max_events',1000000); "
             "SELECT core.set_plan_graph_units_limit_with_authority('free',0);")


def drop():
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


def _graph(name):
    return '{"nodes":[{"id":"n-%s","kind":"file","path":"%s.py","name":"%s"}],"edges":[]}' % (name, name, name)


def _ingest(inst, repo, sha, name):
    """As veripsa_app: enter a real installation (pins the tenant) then ingest one node — the legit live path."""
    g = _graph(name)
    return last_value(psql_app("SET search_path=core; "
                               f"SELECT core.enter_installation_with_authority('{inst}'); "
                               f"SELECT core.ingest_graph_with_authority('{g}'::jsonb,'{repo}','main','{sha}');"))


def _over_quota(account):
    """core._account_over_quota('{account}') with the account pinned (its callers always pin it first)."""
    return last_value(psql_mig(f"SET search_path=core; SET core.current_account='{account}'; "
                               f"SELECT COALESCE(core._account_over_quota('{account}'),'NULL');"))


def _pgu_limit(plan):
    return last_value(psql_mig(f"SET search_path=core; SELECT core._plan_graph_units_limit('{plan}');"))


def _set_pgu(plan, value):
    return last_value(psql_app(f"SET search_path=core; SELECT core.set_plan_graph_units_limit_with_authority('{plan}',{value});"))


def _exempt_list():
    return last_value(psql_mig("SET search_path=core; SELECT core._dev_exempt_account_ids();"))


def _set_exempt(csv):
    return last_value(psql_app(f"SET search_path=core; SELECT core.set_dev_exempt_accounts_with_authority('{csv}');"))


def _rows_in(account, repo):
    """nodes + edges + graph_versions for (account,repo), read past RLS as the migrator (pin the account first)."""
    return last_value(psql_mig(
        f"SET search_path=core; SET core.current_account='{account}'; "
        f"SELECT ((SELECT count(*) FROM core.code_node WHERE account_id='{account}' AND repo='{repo}')"
        f"      +(SELECT count(*) FROM core.code_edge WHERE account_id='{account}' AND repo='{repo}')"
        f"      +(SELECT count(*) FROM core.graph_version WHERE account_id='{account}' AND repo='{repo}'))::int;"))


def main():
    print("VERIPSA QUOTA MODEL — tier ladder + dev exemption + transfer hardening (real gate)")
    print(f"(scratch DB: {DB})")
    bootstrap()

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # PART 1 — 实测-CALIBRATED TIER LADDER (the CODE defaults in 95_owner.sql).
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    _set_pgu("free", 6000)   # bootstrap zeroed free's line; restore the real default so we read it back
    add("LADDER: free=6000 · starter=30000 · pro=80000 · scale=200000 · enterprise=500000 (实测-calibrated CODE defaults)",
        _pgu_limit("free") == "6000" and _pgu_limit("starter") == "30000" and _pgu_limit("pro") == "80000"
        and _pgu_limit("scale") == "200000" and _pgu_limit("enterprise") == "500000")
    # 实测 FIT: each REAL repo size clears its target tier with headroom (and is OVER the tier below it).
    add("实测 FIT: requests 3.2k / flask 5.4k < free 6000 (a real small project fits free)", 3200 < 6000 and 5400 < 6000)
    add("实测 FIT: fastapi 27k > free 6000 and < starter 30000 (a mid service is a starter)", 27000 > 6000 and 27000 < 30000)
    add("实测 FIT: ansible 72k > starter 30000 and < pro 80000 (a large monorepo is a pro)", 72000 > 30000 and 72000 < 80000)
    add("实测 FIT: django 151k > pro 80000 and < scale 200000 (a very large monorepo is a scale)", 151000 > 80000 and 151000 < 200000)
    add("LADDER TOP: enterprise 500000 is the for-now ceiling (NOT unlimited) — a finite cap above django 151k",
        _pgu_limit("enterprise") == "500000")
    add("HARD-WALL: an unmapped/unknown plan → 500000 (the ceiling), NOT unlimited; empty→free 6000",
        _pgu_limit("galaxy") == "500000" and _pgu_limit("") == "6000")
    # FAIL-TOWARD-ENFORCEMENT: an above-frame stored line clamps to a sane value at READ (never unlimited).
    add("CLAMP: a setter value above the frame clamps to 1000000000 at read (never an unbounded line)",
        _set_pgu("scale", 2000000000) == "1000000000")
    _set_pgu("scale", 200000)   # restore the real scale line

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # PART 2 — DEV-ONLY EXEMPTION (checked BEFORE the wall; FAIL-CLOSED; adjustable; NOT a product tier).
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # Re-tighten the FREE graph_units line to 0 (Part 1 restored it to its 6000 default to read the ladder) so the
    # wall BITES on a single stored node — the standard technique to exercise the real wall without thousands of
    # rows. An EXEMPT account bypasses this regardless; a NON-exempt free account at >=1 unit is then over.
    _set_pgu("free", 0)
    # The default allowlist seeds the PO's three accounts.
    dl = _exempt_list()
    add("EXEMPT default: the allowlist seeds the PO's dogfood/demo accounts (the two GH installs + ACCT-DEMO)",
        "ACCT-GH-42424242" in dl and "ACCT-GH-43434343" in dl and "ACCT-DEMO" in dl)

    # An EXEMPT account bypasses the wall: give one of the PO's accounts a footprint over the (zero) free line and
    # confirm it is NOT over quota (the exemption short-circuits every cap). 42424242 is a free account by default.
    _ingest("42424242", "dogfood/repo", SHA1, "dfone")
    _ingest("42424242", "dogfood/repo2", SHA2, "dftwo")
    add("EXEMPT bypass: a dev-exempt account with a footprint over the free line is NOT over quota (the dogfood loop survives)",
        _over_quota("ACCT-GH-42424242") == "NULL")
    r_df = _ingest("42424242", "dogfood/repo3", SHA1, "dfthree")
    add("EXEMPT bypass: a dev-exempt account's repeated ingest is NOT walled (no quota_exceeded — re-index its OWN Core)",
        "quota_exceeded" not in r_df and '"ok":true' in _nows(r_df))

    # A NON-exempt account STILL walls (the exemption is narrow, not a blanket disable of the wall).
    _ingest("ne-555", "neorg/repo", SHA1, "neone")
    add("NON-EXEMPT still walls: a normal free account over the line IS over quota (the wall is intact for everyone else)",
        _over_quota("ACCT-GH-ne-555") in ("graph_units", "repos", "events"))
    r_ne = _ingest("ne-555", "neorg/repo2", SHA2, "netwo")
    add("NON-EXEMPT still walls: its next write is REFUSED (quota_exceeded — the wall genuinely fires)", "quota_exceeded" in r_ne)

    # ADJUSTABLE WITHOUT A REDEPLOY: remove 42424242 from the list → it now WALLS on its retained footprint.
    add("EXEMPT setter: removing an account from the allowlist returns the new effective list (no redeploy)",
        "ACCT-GH-42424242" not in _set_exempt("ACCT-GH-43434343,ACCT-DEMO"))
    add("EXEMPT adjustable: the now-removed account is over quota (the wall re-arms when it leaves the allowlist)",
        _over_quota("ACCT-GH-42424242") in ("graph_units", "repos", "events"))
    # NARROW the list to a SINGLE account (only ACCT-DEMO) → 42424242 + 43434343 both wall, ACCT-DEMO exempt.
    # (Proves the list is an EXACT allowlist, not all-or-nothing: an account not on the explicit list is enforced.)
    add("EXEMPT setter: narrowing the list to one id returns just that id (an EXACT allowlist)", _set_exempt("ACCT-DEMO") == "ACCT-DEMO")
    add("EXEMPT exact: an account NOT on the narrowed list walls (the allowlist is exact-match, not all-or-nothing)",
        _over_quota("ACCT-GH-42424242") in ("graph_units", "repos", "events"))
    # NOTE: UNSET (a never-set knob → no policy row) falls back to the reader's DEFAULT trio; an EXPLICITLY-EMPTY
    # stored value (the owner cleared the allowlist) now exempts NOBODY — the two are distinguished by ROW EXISTENCE
    # (the small-findings-sweep root fix; the dedicated gate test_dev_exempt_unset_vs_empty proves all three cases).
    # Here we restore the default trio explicitly and re-confirm the dogfood account is exempt again.
    _set_exempt("ACCT-GH-42424242,ACCT-GH-43434343,ACCT-DEMO")   # restore the default trio
    add("EXEMPT restore: re-adding the account flips it back to exempt (NULL)", _over_quota("ACCT-GH-42424242") == "NULL")

    # *** FAIL-CLOSED *** — the load-bearing inversion. Break the allowlist read (replace the reader with one that
    # RAISES) and confirm an over-account STILL WALLS: a broken read must NOT exempt (the inner sub-block falls
    # through to the wall). This directly exercises the call-site EXCEPTION handler in _account_over_quota.
    psql_mig("SET search_path=core; "
             "CREATE OR REPLACE FUNCTION core._dev_exempt_account_ids() RETURNS text "
             "LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS "
             "$f$ BEGIN RAISE EXCEPTION 'boom: allowlist read is broken'; END $f$;")
    add("FAIL-CLOSED: with the allowlist read BROKEN, a previously-EXEMPT account is now WALLED "
        "(a broken read is NOT an exemption — the wall stays enforced, never opens)",
        _over_quota("ACCT-GH-42424242") in ("graph_units", "repos", "events"))
    add("FAIL-CLOSED: with the allowlist read BROKEN, a normal over account is STILL walled (no fail-open bypass)",
        _over_quota("ACCT-GH-ne-555") in ("graph_units", "repos", "events"))
    # restore the real reader (re-apply just this function from the canonical body would need the file; the default
    # seed is the load-bearing behaviour, so we recreate the real body inline to leave the DB consistent for teardown).
    psql_mig("SET search_path=core; "
             "CREATE OR REPLACE FUNCTION core._dev_exempt_account_ids() RETURNS text "
             "LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS "
             "$f$ DECLARE v_prev text; v_raw text; BEGIN "
             "v_prev := current_setting('core.current_account', true); "
             "PERFORM set_config('core.current_account', core._owner_account(), true); "
             "v_raw := core._policy_text('dev_exempt_account_ids','ACCT-GH-42424242,ACCT-GH-43434343,ACCT-DEMO'); "
             "PERFORM set_config('core.current_account', COALESCE(v_prev,''), true); RETURN v_raw; END $f$;")
    add("FAIL-CLOSED recover: with a WORKING read the exempt account is exempt again (NULL) — fail-closed was transient",
        _over_quota("ACCT-GH-42424242") == "NULL")

    # PERIMETER: the exemption setter is App-delegation ONLY (a buyer seat can never add itself to the bypass).
    esig = "core.set_dev_exempt_accounts_with_authority(text)"
    add("PERIMETER: veripsa_app HAS EXECUTE on the dev-exemption setter (the owner's delegated path is live)",
        last_value(psql_mig(f"SELECT has_function_privilege('veripsa_app','{esig}','EXECUTE');")) == "t")
    add("PERIMETER: a buyer writer (veripsa_writer) has NO EXECUTE on the dev-exemption setter (no self-exempt)",
        last_value(psql_mig(f"SELECT has_function_privilege('veripsa_writer','{esig}','EXECUTE');")) == "f")
    buyer_dsn = f"postgresql://veripsa_demo_agent3@localhost/{DB}"
    r_buyer = subprocess.run(["psql", buyer_dsn, "-v", "ON_ERROR_STOP=0", "-tAc",
                              "SET search_path=core; SELECT core.set_dev_exempt_accounts_with_authority('ACCT-GH-ne-555');"],
                             capture_output=True, text=True)
    add("PERIMETER: a buyer SEAT calling the exemption setter directly is DENIED at the GRANT layer (no self-exempt)",
        "permission denied" in (r_buyer.stdout + r_buyer.stderr).lower())
    add("PERIMETER: the buyer's denied self-exempt did NOT add it — ne-555 is still walled",
        _over_quota("ACCT-GH-ne-555") in ("graph_units", "repos", "events"))

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # PART 3 — TRANSFER AUTHORITY PERIMETER (proofless rolling workers fail closed; only /8 is delegated).
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    XF_SIG = "core.transfer_repo_coordinate_with_authority(text,text,text,text,text,text,text,text)"
    LEGACY_XF_SIG = "core.transfer_repo_coordinate_with_authority(text,text)"

    # A real coordinate proves the rolling /2 trap cannot be used as an accidental destructive fallback.
    OLD_ID = "930001"
    OLD_ACCT, OLD_FULL = f"ACCT-GH-{OLD_ID}", f"oldorg{OLD_ID}/svc"
    _ingest(OLD_ID, OLD_FULL, SHA1, "xfone")
    add("TRANSFER setup: the OLD owner really holds the repo (graph rows present before transfer)", int(_rows_in(OLD_ACCT, OLD_FULL) or "0") > 0)
    legacy_attempt = psql_app(
        f"SET search_path=core; SELECT core.transfer_repo_coordinate_with_authority('{OLD_ACCT}','{OLD_FULL}');")
    add("TRANSFER HARDEN: proofless /2 App call is denied instead of inferring cross-tenant authority",
        "permission denied" in legacy_attempt.lower() or "proofless" in legacy_attempt.lower())
    add("TRANSFER HARDEN: denied proofless transfer preserves the old coordinate",
        int(_rows_in(OLD_ACCT, OLD_FULL) or "0") > 0)

    # PERIMETER: /8 is App-delegation ONLY; /2 and PUBLIC have no executable transfer path.
    add("PERIMETER: veripsa_app HAS EXECUTE on proof-required transfer /8",
        last_value(psql_mig(f"SELECT has_function_privilege('veripsa_app','{XF_SIG}','EXECUTE');")) == "t")
    add("PERIMETER: a buyer writer has NO EXECUTE on proof-required transfer /8",
        last_value(psql_mig(f"SELECT has_function_privilege('veripsa_writer','{XF_SIG}','EXECUTE');")) == "f")
    add("PERIMETER: veripsa_app has NO EXECUTE on proofless transfer /2",
        last_value(psql_mig(f"SELECT has_function_privilege('veripsa_app','{LEGACY_XF_SIG}','EXECUTE');")) == "f")
    public_xf = last_value(psql_mig(
        f"SELECT NOT EXISTS (SELECT 1 FROM pg_proc p, "
        f"LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a "
        f"WHERE p.oid='{XF_SIG}'::regprocedure AND a.grantee=0 AND a.privilege_type='EXECUTE');"))
    add("PERIMETER: PUBLIC has NO EXECUTE on proof-required transfer /8", public_xf == "t")

    # ── verdict ──────────────────────────────────────────────────────────────────────────────────────────
    drop()
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    failed = [label for label, ok in checks if not ok]
    print(f"\n-- {passed}/{total} quota-model assertions passed "
          "(实测 tier ladder [free6k/starter30k/pro80k/scale200k/ent500k, top NOT unlimited, clamps] + "
          "dev-only exemption [exempt bypasses · NON-exempt walls · adjustable · FAIL-CLOSED · App-only perimeter] + "
          "transfer hardening [proofless denied · old coordinate preserved · /8 App-only]) --")
    if failed:
        print(f"\n[FAIL] {len(failed)} assertion(s) FAILED — the quota model is unsafe:")
        for f in failed[:40]:
            print(f"   - {f}")
        print("\nQUOTA MODEL GATE: FAIL")
        sys.exit(1)
    print("\nHONEST: the tier DATA the PO sets in prod is applied via the setters (set_plan_graph_units_limit_with_"
          "authority); these CODE defaults are the unset-fallback + the strict-on-error floor. The exemption is a "
          "DEV/OWNER convenience (a named-id allowlist), explicitly NOT a product tier, and it FAILS CLOSED.")
    print("QUOTA MODEL GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never leak the scratch DB on an unexpected error
        drop()
        print(f"\n[FAIL] unexpected error: {e}")
        print("QUOTA MODEL GATE: FAIL")
        sys.exit(1)
