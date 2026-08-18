#!/usr/bin/env python3
"""CO-CHANGE HONEST DENOMINATOR — graph_insights_for_installation exposes support_n + observed_m so the platform
dashboard can show "N of M observed commits · X%" instead of a BARE % that LIES on a thin sample.

THE BUG THIS LOCKS: the coupled-pair surface (core.graph_insights_for_installation.coupled) rendered only
{a, b, pct}, where pct = round(strength*100) and strength = max(co/n_a, co/n_b) — a DIRECTIONAL conditional
probability with NO denominator attached. So a coupling backed by a SINGLE shared commit (co=1, n_a=1 →
strength=1.0) reads "100%", byte-identical to a coupling backed by 40 shared commits out of 55 — the bare % cannot
tell a one-off coincidence from a thick, trustworthy coupling. A reader sees "100%" and over-trusts noise.

THE FIX (this gate's subject): each coupled pair ALSO carries
    support_n  = co            — N: the number of commits in which BOTH files changed (the co-occurrence support).
    observed_m = n_a + n_b - co — M: the number of commits in which EITHER file changed (the UNION) = the commits
                                 in which the pair was OBSERVABLE. By inclusion-exclusion |A∪B| = |A|+|B|-|A∩B|
                                 and |A∩B| = co. "N of M observed commits" then reads, honestly, "of the M commits
                                 that touched either file, N touched both" — and the sample size (M) is now VISIBLE,
                                 so a thin pair can no longer masquerade as a strong one.

WHY THE UNION, NOT n_total: M is deliberately the pair-local UNION, not n_total (the whole repo's commit count).
n_total would make "N of M" read "N of <every commit in the repo>", understating the rate for two files that
simply change less often than the repo as a whole — dishonest in the other direction. The union is the set of
commits in which THIS pair could have co-changed, which is the statistically-correct base for the rate.

WHAT THIS GATE PROVES (live, on the ephemeral test Postgres — never grep; the only truth is the running DB):

  D1 PRESENT      — every coupled pair carries integer support_n AND observed_m (the shape is {a,b,pct,support_n,
                    observed_m}).
  D2 EXACT MATH   — for hand-checked seeded rows, support_n == co and observed_m == n_a + n_b - co (the union),
                    and observed_m is NOT n_total — the denominator is the union, not the whole-repo count.
  D3 N<=M ALWAYS  — observed_m >= support_n >= 1 for every pair (a union can never be below its intersection), so
                    the honest "N of M" can never read above 100%.
  D4 DISTINGUISHABLE — a LOW-support pair and a HIGH-support pair that share the SAME bare pct are TOLD APART by
                    the denominator: identical pct (the old surface would show them as equal) but support_n/
                    observed_m of 3/5 vs 30/50 — the thin coupling is now self-evidently thin. This is the whole
                    point: the % alone lies, the denominator makes the sample size honest.
  D5 1-OF-1 FLOORED — a genuine 1-of-1 (co=1) is below this surface's co>=3 floor and never appears at all (the
                    floor is the first line of defense); the denominator is the SECOND, for the thin pairs that DO
                    clear the floor (co=3 backed by a tiny union still reads "3 of 4", not a bare "75%").

Run:  python3 tests/test_cochange_denominator.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# PROCESS-UNIQUE (parallel-safe): bootstrap + drop our OWN DB, like the sibling reader/security gates — a FIXED
# name would let concurrent runs drop each other's DB mid-run.
DB = "veripsa_ccdenom_" + str(os.getpid())

READER = "example_platform_reader"

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, passed))


def psql_mig(sql):
    """As the migrator (owner) — fixtures (FORCE-RLS co_change needs the account pin + governed token)."""
    dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def psql_admin(sql):
    """The cluster admin/superuser (ADMIN_DSN) — the ONE thing the migrator cannot do: ALTER ROLE ... LOGIN."""
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


def insert_node(account, repo, branch, node_id, kind, path, name):
    return psql_mig(
        "SET search_path=core; BEGIN; "
        f"SELECT set_config('core.current_account','{account}',true); "
        "SELECT core.mark_governed_write('code_node'); "
        "INSERT INTO core.code_node(account_id,node_id,node_kind,path,name,language,repo,branch) "
        f"VALUES ('{account}','{node_id}','{kind}','{path}','{name}','python','{repo}','{branch}'); "
        "COMMIT;")


def insert_cochange(account, repo, a, b, co, n_a, n_b, strength, lift, n_total):
    return psql_mig(
        "SET search_path=core; BEGIN; "
        f"SELECT set_config('core.current_account','{account}',true); "
        "SELECT core.mark_governed_write('co_change'); "
        "INSERT INTO core.co_change(account_id,repo,path_a,path_b,co,n_a,n_b,strength,lift,n_total) "
        f"VALUES ('{account}','{repo}','{a}','{b}',{co},{n_a},{n_b},{strength},{lift},{n_total}) "
        "ON CONFLICT DO NOTHING; COMMIT;")


# ── THE FIXTURE: ONE repo, FOUR floor-passing co-change pairs hand-built to exercise the denominator. ──────────
# Each tuple: (a, b, co, n_a, n_b, strength, lift, expect_support_n, expect_observed_m). a<b (canonical).
# strength is set so pct = round(strength*100); the LOW and HIGH pairs share pct=75 to prove the denominator —
# not the bare % — is what tells a thin coupling from a thick one. n_total is the WHOLE-repo count (=200) — M must
# NOT equal it (M is the pair-local union n_a+n_b-co). observed_m is hand-checked = n_a + n_b - co.
PAIRS = [
    # HIGH support, pct 75: 30 of (40+40-30)=50.  Thick, trustworthy coupling.
    ("app/high_a.py", "app/high_b.py", 30, 40, 40, 0.75, 5.0, 30, 50),
    # LOW support, SAME pct 75: 3 of (4+4-3)=5.  Thin coupling — clears the co>=3 floor but is backed by 5 commits.
    ("app/low_a.py", "app/low_b.py", 3, 4, 4, 0.75, 6.0, 3, 5),
    # ASYMMETRIC: co=8, n_a=10, n_b=20 → union 10+20-8=22; strength=max(0.8,0.4)=0.8 → pct 80. Proves M uses the
    # UNION (22), which differs sharply from either single-file count (10 or 20) and from n_total (200).
    ("app/asym_a.py", "app/asym_b.py", 8, 10, 20, 0.80, 3.0, 8, 22),
    # NEAR-TOTAL: co=9, n_a=10, n_b=10 → union 11; strength=0.9 → pct 90. observed_m (11) only just exceeds
    # support_n (9) — a genuinely strong coupling reads "9 of 11", honestly near-100% AND well-supported.
    ("app/tight_a.py", "app/tight_b.py", 9, 10, 10, 0.90, 4.0, 9, 11),
]
N_TOTAL = 200


def bootstrap():
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("[FAIL] bootstrap (roles + schema.sql + seats)")
        print((r.stdout + r.stderr)[-2000:])
        sys.exit(2)

    # route the demo installation 111 → ACCT-DEMO.
    psql_mig("SET search_path=core; "
             "INSERT INTO core.installation_account(installation_id,account_id) VALUES ('111','ACCT-DEMO') "
             "ON CONFLICT DO NOTHING;")

    REPO = "demoorg/denom-repo"
    BR = "main"
    # at least one FILE node so the repo is the account's busiest (graph_insights picks busiest by node count).
    for a, b, *_ in PAIRS:
        for p in (a, b):
            g = insert_node("ACCT-DEMO", REPO, BR, "N" + p, "file", p, "fn")
            if "ERROR" in g:
                print("[FAIL] could not seed a code_node row:"); print(g[-1500:]); sys.exit(2)
    for a, b, co, na, nb, s, lift, _en, _em in PAIRS:
        cc = insert_cochange("ACCT-DEMO", REPO, a, b, co, na, nb, s, lift, N_TOTAL)
        if "ERROR" in cc:
            print("[FAIL] could not seed a co_change row:"); print(cc[-1500:]); sys.exit(2)

    login = psql_admin("ALTER ROLE example_platform_reader LOGIN;")
    if "ERROR" in login or "permission denied" in login:
        print("[FAIL] could not grant LOGIN to example_platform_reader via ADMIN_DSN:")
        print(login[-1500:]); sys.exit(2)


def drop():
    subprocess.run(["psql", os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres"),
                    "-tAc", "ALTER ROLE example_platform_reader NOLOGIN;"], capture_output=True, text=True)
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


def main():
    print("VERIPSA CO-CHANGE HONEST DENOMINATOR — support_n + observed_m on graph_insights_for_installation.coupled")
    print(f"(scratch DB: {DB})")
    bootstrap()

    out = None
    try:
        out = json.loads(last_value(psql_as(READER, "SET search_path=core; "
              "SELECT core.graph_insights_for_installation('111')::text;")))
    except Exception:
        out = None
    coupled = out.get("coupled") if isinstance(out, dict) else None
    add("SETUP: graph_insights_for_installation('111') returns the denom-repo with 4 coupled pairs",
        isinstance(out, dict) and out.get("repo") == "demoorg/denom-repo"
        and isinstance(coupled, list) and len(coupled) == 4)
    by_pair = {(c.get("a"), c.get("b")): c for c in (coupled or [])}

    # ── D1 PRESENT — every coupled pair carries integer support_n AND observed_m. ─────────────────────────────
    d1 = (isinstance(coupled, list) and len(coupled) == 4
          and all(isinstance(c, dict) and {"a", "b", "pct", "support_n", "observed_m"}.issubset(set(c.keys()))
                  and isinstance(c.get("support_n"), int) and isinstance(c.get("observed_m"), int)
                  for c in coupled))
    add("D1 PRESENT: every coupled pair carries integer support_n AND observed_m ({a,b,pct,support_n,observed_m})",
        d1)

    # ── D2 EXACT MATH — support_n == co and observed_m == n_a+n_b-co (the union), per hand-checked seed row. ───
    d2 = True
    for a, b, co, na, nb, s, lift, en, em in PAIRS:
        c = by_pair.get((a, b))
        ok = isinstance(c, dict) and c.get("support_n") == en and c.get("observed_m") == em
        d2 = d2 and ok
        add(f"D2 EXACT: {a}↔{b} → support_n={en} (=co) and observed_m={em} (=n_a+n_b-co = {na}+{nb}-{co}, the UNION)",
            ok)
    # observed_m must NOT be the whole-repo n_total (=200) for ANY pair — the denominator is the pair-local union.
    add("D2 NOT-GLOBAL: no pair's observed_m equals n_total (200) — M is the pair-local UNION, never the whole-repo "
        "commit count",
        isinstance(coupled, list) and all((c.get("observed_m") or 0) != N_TOTAL for c in coupled))

    # ── D3 N<=M ALWAYS — observed_m >= support_n >= 1 (a union can never be below its intersection). ──────────
    add("D3 N<=M: every pair has observed_m >= support_n >= 1 (the honest 'N of M' can never read above 100%)",
        isinstance(coupled, list) and len(coupled) == 4
        and all((c.get("support_n") or 0) >= 1 and (c.get("observed_m") or 0) >= (c.get("support_n") or 0)
                for c in coupled))

    # ── D4 DISTINGUISHABLE — the LOW and HIGH pairs share the SAME bare pct but DIFFER in (support_n, observed_m). ─
    high = by_pair.get(("app/high_a.py", "app/high_b.py"))
    low = by_pair.get(("app/low_a.py", "app/low_b.py"))
    same_pct = (isinstance(high, dict) and isinstance(low, dict) and high.get("pct") == low.get("pct"))
    add("D4 SAME-PCT: the LOW and HIGH pairs render the IDENTICAL bare pct (75) — the old {a,b,pct}-only surface "
        "would have shown them as EQUAL (this is the lie the denominator fixes)",
        same_pct and high.get("pct") == 75)
    distinguishable = (isinstance(high, dict) and isinstance(low, dict)
                       and (high.get("support_n"), high.get("observed_m")) == (30, 50)
                       and (low.get("support_n"), low.get("observed_m")) == (3, 5)
                       and high.get("support_n") > low.get("support_n")
                       and high.get("observed_m") > low.get("observed_m"))
    add("D4 DISTINGUISHABLE: despite the identical pct, support_n/observed_m tell them apart — HIGH '30 of 50' vs "
        "LOW '3 of 5' (the thin coupling is now self-evidently thin)",
        distinguishable)

    # ── D5 1-OF-1 FLOORED — a real 1-of-1 (co=1) never reaches this floor-gated surface (co>=3); the denominator
    #     is the SECOND line of defense, for thin pairs (co=3) that DO clear the floor. We re-seed a 1-of-1 and a
    #     co=2 pair (both below the co>=3 floor) and confirm they NEVER surface — the floor + denominator compose.
    one_of_one = insert_cochange("ACCT-DEMO", "demoorg/denom-repo", "app/noise_a.py", "app/noise_b.py",
                                 1, 1, 1, 1.0, 5.0, N_TOTAL)   # strength 1.0 → would render "100%" if it surfaced
    two = insert_cochange("ACCT-DEMO", "demoorg/denom-repo", "app/coinc_a.py", "app/coinc_b.py",
                          2, 2, 2, 1.0, 5.0, N_TOTAL)
    if "ERROR" in one_of_one or "ERROR" in two:
        print("[FAIL] could not seed the sub-floor 1-of-1 / co=2 rows:")
        print((one_of_one + "\n" + two)[-1500:]); sys.exit(2)
    out2 = None
    try:
        out2 = json.loads(last_value(psql_as(READER, "SET search_path=core; "
               "SELECT core.graph_insights_for_installation('111')::text;")))
    except Exception:
        out2 = None
    coupled2 = out2.get("coupled") if isinstance(out2, dict) else []
    pair_set2 = {(c.get("a"), c.get("b")) for c in (coupled2 or [])}
    add("D5 FLOORED: a genuine 1-of-1 (co=1, strength=1.0 — the classic bare-'100%' masquerade) is BELOW the "
        "co>=3 floor and NEVER surfaces (the floor is the first defense; the denominator the second)",
        ("app/noise_a.py", "app/noise_b.py") not in pair_set2
        and ("app/coinc_a.py", "app/coinc_b.py") not in pair_set2)
    add("D5 STILL-4: the 4 floor-passing pairs are unaffected by the sub-floor noise (still exactly 4)",
        isinstance(coupled2, list) and len(coupled2) == 4)

    # ── CONTENT-FREE — support_n/observed_m are COUNTS; no body/secret/name string leaks (paths + counts only). ─
    raw = last_value(psql_as(READER, "SET search_path=core; "
                                     "SELECT core.graph_insights_for_installation('111')::text;"))
    allowed = set()
    for a, b, *_ in PAIRS:
        allowed.add(a); allowed.add(b)
    leaf_ok = isinstance(coupled, list) and all(
        c.get("a") in allowed and c.get("b") in allowed for c in coupled)
    add("CONTENT-FREE: every coupled string leaf is an expected file PATH; support_n/observed_m are integer COUNTS "
        "(no node names / bodies / secrets)",
        raw != "" and leaf_ok and "fn" not in json.loads(raw).get("repo", ""))

    # ── verdict ───────────────────────────────────────────────────────────────────────────────────────────────
    drop()
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    failed = [label for label, ok in checks if not ok]
    print(f"\n-- {passed}/{total} assertions passed "
          "(support_n+observed_m present · exact union math · N<=M · low-vs-high distinguishable · 1-of-1 floored) --")
    if failed:
        print(f"\n[FAIL] {len(failed)} assertion(s) FAILED — the honest co-change denominator has a hole:")
        for f in failed[:40]:
            print(f"   - {f}")
        print("\nCO-CHANGE DENOMINATOR GATE: FAIL")
        sys.exit(1)
    print("\nHONEST: graph_insights_for_installation.coupled now carries support_n (co = commits where BOTH changed) "
          "and observed_m (n_a+n_b-co = commits where EITHER changed = the observation basis), so the dashboard "
          "renders 'N of M observed commits · X%' — a thin coupling can no longer hide behind a bare 100%.")
    print("CO-CHANGE DENOMINATOR GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never leak the scratch DB on an unexpected error
        drop()
        print(f"\n[FAIL] unexpected error: {e}")
        print("CO-CHANGE DENOMINATOR GATE: FAIL")
        sys.exit(1)
