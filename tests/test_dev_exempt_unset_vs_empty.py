#!/usr/bin/env python3
"""DEV-EXEMPT UNSET-vs-EXPLICITLY-EMPTY gate (small-findings root-fix sweep, finding 2).

THE FOOTGUN (root-fixed here): the dev-exemption allowlist (core._dev_exempt_account_ids — the PO's own dogfood/demo
accounts that bypass the free-tier quota wall) was read via the generic core._policy_text, which collapses BOTH "no
policy row" AND "row present but blank" onto its DEFAULT. So storing an EMPTY allowlist — the owner's explicit
"exempt NOBODY" — silently fell through to the DEFAULT TRIO and RE-EXEMPTED the PO's own three accounts instead of
nobody. That is the WRONG direction for a wall knob: CLEARING the exemption must REMOVE exemptions, never resurrect
them. (It is still fail-closed for everyone else, but it means an operator who deliberately cleared the dogfood
bypass would unknowingly leave the PO's accounts un-walled.)

THE ROOT FIX: _dev_exempt_account_ids now reads core.policy DIRECTLY (owner-pinned, FORCE-RLS scoped to the owner
row) and branches on ROW EXISTENCE (the plpgsql FOUND flag), NOT on blank-vs-non-blank:
  * NO row (never set)        → UNSET → the default dogfood trio.
  * row present (explicitly set) → use it VERBATIM. An EMPTY value = "exempt NOBODY" (honored AS empty).

PROVES, against a REAL local Postgres:
  (1) UNSET (a fresh DB, knob never written) → the default trio (the documented unset seed).
  (2) EXPLICITLY-EMPTY (the owner cleared the allowlist) → EXEMPT NOBODY (the empty string), NOT the trio.
  (3) a SET list → EXACTLY that list (exact-match allowlist).
  (4) END-TO-END at the quota wall: after clearing the allowlist, a previously-exempt PO account is NOW WALLED
      (the corrected direction); a re-set list re-exempts exactly those ids.
  (5) FAIL-CLOSED is preserved: a broken allowlist read still walls (the call-site inversion is unchanged).

PROCESS-UNIQUE scratch DB (parallel-safe). Run:  python3 tests/test_dev_exempt_unset_vs_empty.py
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

DB = "veripsa_devexempt_" + str(os.getpid())
DSN_MIG = f"postgresql://veripsa_migrator@localhost/{DB}"
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"

TRIO = "ACCT-GH-42424242,ACCT-GH-43434343,ACCT-DEMO"
SHA1 = "1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa"
SHA2 = "2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb"

checks = []


def add(label, passed):
    print(("  [PASS] " if passed else "  [FAIL] ") + label)
    checks.append(bool(passed))


def scalar(dsn, sql):
    """Run ONE fully-qualified SELECT (no `SET search_path` echo to pollute -tA output) and return its single value
    EXACTLY — including the empty string (the load-bearing distinction this gate tests). -tA prints just the value
    + a trailing newline, so the raw stdout minus that newline IS the value (empty string stays empty)."""
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    if r.returncode != 0 or (r.stderr and "ERROR" in r.stderr):
        return "<<error>> " + (r.stderr or "").strip()
    out = r.stdout
    if out.endswith("\n"):
        out = out[:-1]
    return out


def exempt_list():
    return scalar(DSN_MIG, "SELECT core._dev_exempt_account_ids();")


def set_exempt(csv):
    csv = csv.replace("'", "''")
    return scalar(DSN_APP, f"SELECT core.set_dev_exempt_accounts_with_authority('{csv}');")


def over_quota(acct):
    # core._account_over_quota reads core.current_account for the exemption check; pin it then call (one txn via a
    # DO-less compound is impossible in -tA, so use a single SELECT that sets the GUC inline via a CTE-free wrapper).
    # set_config(...,true) is txn-local; -c runs in one implicit txn, so the pin holds for the same-statement subselect.
    return scalar(DSN_MIG,
                  f"SELECT COALESCE(core._account_over_quota('{acct}')::text,'NULL') "
                  f"FROM (SELECT set_config('core.current_account','{acct}',true)) _p;")


def policy_rowcount():
    # core.policy is FORCE-RLS; an unpinned read sees zero rows even as the owner. Pin the owner account inline
    # (txn-local set_config in the same -c statement) so the count reflects the real owner row state.
    return scalar(DSN_MIG,
                  "SELECT count(*)::text FROM core.policy "
                  "WHERE account_id = core._owner_account() AND policy_key='dev_exempt_account_ids' "
                  "AND (SELECT set_config('core.current_account', core._owner_account(), true)) IS NOT NULL;")


def ingest(install, repo, sha):
    """Drive a real ingest as the App tenant `install` so the account accrues graph_units (to cross the free line).
    Uses a held connection so enter_installation's txn-local pin survives into the ingest call (psycopg2, one txn)."""
    import json
    import tempfile
    import code_graph_extract as X
    import psycopg2
    with tempfile.TemporaryDirectory() as d:
        # enough nodes/edges to clear the free graph_units line (6000) for a non-exempt free account.
        for i in range(60):
            open(os.path.join(d, f"m{i}.py"), "w").write(
                f"import m{(i+1) % 60}\nimport m{(i+2) % 60}\n\ndef f{i}():\n    return f{(i+1) % 60}() + f{(i+2) % 60}()\n")
        g = X.build_graph(d)
    conn = psycopg2.connect(DSN_APP)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (install,))
            cur.execute("SELECT core.ingest_graph_with_authority(%s,%s,'main',%s)", (json.dumps(g), repo, sha))
    finally:
        conn.close()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    print("DEV-EXEMPT — UNSET vs EXPLICITLY-EMPTY vs SET (small-findings root fix, finding 2)")

    # ── (1) UNSET: a fresh DB has NO policy row for the knob → the reader returns the documented default trio.
    add("UNSET (knob never written) has NO policy row", policy_rowcount() == "0")
    add("(1) UNSET → the default dogfood trio (the documented unset seed)", exempt_list() == TRIO)

    # ── (2) EXPLICITLY-EMPTY: the owner CLEARS the allowlist → a policy row EXISTS with '' → exempt NOBODY (NOT the trio).
    ret = set_exempt("")                                       # store the explicit-empty list
    add("EXPLICIT-EMPTY now has a policy row (the owner SET it, even to empty)", policy_rowcount() == "1")
    add("(2) EXPLICITLY-EMPTY setter returns the empty list (exempt nobody)", ret == "")
    add("(2) EXPLICITLY-EMPTY read → exempt NOBODY (empty), NOT the default trio (the ROOT FIX)",
        exempt_list() == "")

    # ── (3) a SET list → EXACTLY that list (exact-match allowlist), distinguishable from both unset and empty.
    add("(3) a SET list returns exactly that list", set_exempt("ACCT-GH-12345,ACCT-GH-67890") == "ACCT-GH-12345,ACCT-GH-67890")
    add("(3) the SET list reads back exactly", exempt_list() == "ACCT-GH-12345,ACCT-GH-67890")

    # ── (4) END-TO-END at the quota wall. Seed a PO trio account (42424242) over the free line, then:
    #        - with the DEFAULT trio (re-set explicitly) it is EXEMPT (NULL);
    #        - after the owner CLEARS the allowlist (explicit-empty) it is WALLED (the corrected direction);
    #        - re-setting a list that INCLUDES it re-exempts it.
    # Zero the FREE graph_units line so ANY graph write trips the wall (the same deterministic technique
    # test_quota_model uses) — the exemption (not the line size) is what this gate exercises end-to-end.
    scalar(DSN_APP, "SELECT core.set_plan_graph_units_limit_with_authority('free',0);")
    set_exempt(TRIO)                                           # restore the default trio explicitly
    ingest("42424242", "po/repo", SHA1)                       # accrue graph_units (any amount is now over the zeroed line)
    add("(4a) with the trio allowlist, the over-line PO account is EXEMPT (NULL)", over_quota("ACCT-GH-42424242") == "NULL")
    set_exempt("")                                             # the owner clears the allowlist (explicit-empty)
    add("(4b) ROOT FIX end-to-end: after CLEARING the allowlist, the PO account is now WALLED (not silently re-exempted)",
        over_quota("ACCT-GH-42424242") in ("graph_units", "repos", "events"))
    set_exempt("ACCT-GH-42424242")                           # re-exempt exactly this id
    add("(4c) re-setting a list that includes the account re-exempts it (NULL)", over_quota("ACCT-GH-42424242") == "NULL")

    # ── (5) FAIL-CLOSED preserved: a broken allowlist read still walls (the call-site inversion is unchanged by this fix).
    subprocess.run(["psql", DSN_MIG, "-v", "ON_ERROR_STOP=1", "-q", "-c",
                    "SET search_path=core; "
                    "CREATE OR REPLACE FUNCTION core._dev_exempt_account_ids() RETURNS text "
                    "LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS "
                    "$f$ BEGIN RAISE EXCEPTION 'boom'; END $f$;"], capture_output=True, text=True)
    add("(5) FAIL-CLOSED: a broken allowlist read still WALLS the previously-exempt account (no fail-open)",
        over_quota("ACCT-GH-42424242") in ("graph_units", "repos", "events"))

    print()
    if all(checks):
        print("DEV-EXEMPT UNSET-VS-EMPTY GATE: PASS")
        return 0
    print(f"DEV-EXEMPT UNSET-VS-EMPTY GATE: FAIL ({sum(1 for c in checks if not c)} of {len(checks)} failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
