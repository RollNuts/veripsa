#!/usr/bin/env python3
"""OWNER COMPAT-SHADOW SURFACE gate (compat lane PR-5) — the owner-only, content-free shadow-report lens.

The shadow compatibility lane (PR-2/PR-3) records 'compat_finding' events behind per-tenant FORCE RLS and
emits only per-process log lines — so the weekly GTM review had NO cumulative, owner-readable view of what
the shadow lane has observed. core.owner_compat_shadow_surface() (db/schema/95_owner.sql) is that lens:
the same owner-only cross-tenant model + lock as owner_cost_surface / owner_graph_freshness_surface, a pure
read over the existing event kind, STRICTLY AGGREGATE output. github-app/cost_report.py renders it as the
COMPATIBILITY SHADOW section of the founder report (the owner-DSN-authenticated operator surface — never an
HTTP probe; /freshz//healthz//statusz are unauthenticated and must not carry it).

Proves (real scratch DB, two tenants, findings recorded through the REAL recorder — the S3a 9-arg
classification-stamping signature plus one legacy 7-arg row):
  (1) ZEROS WHEN EMPTY: before any finding exists the surface returns all-zero counts + null timestamps —
      a dormant lane reads as zeros (INCLUDING every S3a split total), never an error;
  (2) AGGREGATES CORRECT + CROSS-TENANT + S3a TAXONOMY SPLIT (correction §3 lane 3): after recording a
      MIXED-CLASS fixture under BOTH accounts, the lens returns the exact per-class totals —
      contract_deltas_total / rebase_needed_total / divergent_definitions_total / incompatibilities_total
      (the evidence class ONLY) / unclassified_total (the legacy 7-arg row) — which SUM exactly to
      observations_total (RENAMED from the conflating findings_total); incompatibilities_by_detail and
      head_pairs_with_incompatibility cover the evidence class only; observations and incompatibilities are
      NEVER conflated (the split is driven by the fact_class COLUMN, never by parsing reason strings);
  (3) CONTENT-FREE (strictly aggregate): the serialized output carries NO path, NO repo name, NO branch,
      NO symbol, NO PR number, NO SHA fragment, NO fingerprint — and exactly the expected key set (the new
      S3a keys add class codes/detail codes ONLY);
  (4) OWNER-ONLY: a buyer/tenant role (veripsa_demo_agent — inherits veripsa_writer, NOT veripsa_app)
      cannot execute it (no cross-tenant leak path);
  (5) BOUNDED: p_cap=1 visits ONE account, flags capped=true with the exact account_count — the
      owner-sweep bound-honesty pattern;
  (6) RENDERER: cost_report.render_compat_shadow is pure + never raises — None degrades to an honest
      all-zeros block, a real surface renders the split (observations visibly separate from evidence-backed
      incompatibilities), a PRE-S3a surface (old findings_total key) still renders its grand total, and the
      poison tokens stay absent from the text.

Run:  python3 tests/test_owner_compat_shadow_surface.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402

import cost_report  # noqa: E402  (github-app/cost_report.py — the operator surface that renders the lens)

# PROCESS-UNIQUE (parallel-safe) scratch DB — the test_compat_finding_event / smoke.sh pattern.
DB = "veripsa_compatshadow_" + str(os.getpid())

# Distinctive poison tokens: if ANY of these ever appears in the surface output, content leaked.
REPO_A1 = "acme/leakrepo-zz9"
REPO_A2 = "acme/leaklib-zz9"
REPO_B = "acme/leakother-zz9"
PATH_POISON = "src/leakpath_zz9.py"
BRANCH = "main"
SHA_P1, SHA_C1 = "a" * 40, "b" * 40
SHA_P2, SHA_C2 = "c" * 40, "d" * 40
SHA_P3, SHA_C3 = "e" * 40, "f" * 40
SHA_P4, SHA_C4 = "1a" * 20, "1b" * 20
FP1 = "fpleak1" + "0" * 25
FP2 = "fpleak2" + "0" * 25
FP3 = "fpleak3" + "0" * 25
FP4 = "fpleak4" + "0" * 25
FP5 = "fpleak5" + "0" * 25
FP6 = "fpleak6" + "0" * 25
FP7 = "fpleak7" + "0" * 25
# Reason vocabulary v2 (S1/S2) + one retired legacy code on the legacy 7-arg row.
RULE_DELTA_ADD = "contract_delta:required_arg_added"
RULE_DELTA_OPT = "contract_delta:optional_to_required"
RULE_REBASE = "rebase_needed"
RULE_DIVERGENT = "divergent_definitions"
RULE_MISM_POS = "consumer_call_mismatch:positional_shortfall"
RULE_MISM_REM = "consumer_call_mismatch:callee_removed"
RULE_LEGACY = "breaking:required_arg_added"
# S3a classification codes + the detector stamp (mirrors _compat_analysis constants).
CLASS_DELTA = "contract_delta_observation"
CLASS_REBASE = "rebase_needed_observation"
CLASS_DIVERGENT = "divergent_definition_observation"
CLASS_INCOMPAT = "evidence_backed_incompatibility"
DET = "python-call-compat/py-call-v1"

EXPECTED_KEYS = {
    # S3a taxonomy split: the grand total is RENAMED observations_total; the per-class totals + the
    # evidence-only breakdowns are first-class keys (all counts — content-free).
    "observations_total", "contract_deltas_total", "rebase_needed_total", "divergent_definitions_total",
    "incompatibilities_total", "unclassified_total", "incompatibilities_by_detail",
    "head_pairs_with_incompatibility",
    "repos_observed", "head_pairs_observed", "accounts_with_findings",
    "findings_by_rule", "first_observed_at", "last_observed_at",
    "account_count", "capped", "cap", "accounts_scanned",
}

checks = []


def chk(c, label):
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    checks.append(bool(c))


def app_conn_for_installation(installation_id):
    """A connection that has ENTERED `installation_id` as the App (veripsa_app) — the live per-event shape."""
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", (installation_id,))
        account = cur.fetchone()[0]

    def run(sql, args=()):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    return run, account


def _j(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", (r.stdout + r.stderr)[-1500:])
        return 1

    app_a, acct_a = app_conn_for_installation("INST-CSHADOW-A")
    app_b, acct_b = app_conn_for_installation("INST-CSHADOW-B")
    chk(bool(acct_a) and bool(acct_b) and acct_a != acct_b,
        f"setup: two installations route to two distinct accounts ({acct_a} / {acct_b})")

    # ── (1) ZEROS WHEN EMPTY: a dormant lane reads as zeros (incl. every S3a split key), never an error ─
    z = _j(app_a("SELECT core.owner_compat_shadow_surface()"))
    chk(z.get("observations_total") == 0 and z.get("repos_observed") == 0
        and z.get("head_pairs_observed") == 0 and z.get("accounts_with_findings") == 0
        and z.get("findings_by_rule") == {} and z.get("first_observed_at") is None
        and z.get("last_observed_at") is None,
        f"empty lane → all-zero aggregates + null timestamps (got observations_total={z.get('observations_total')})")
    chk(z.get("contract_deltas_total") == 0 and z.get("rebase_needed_total") == 0
        and z.get("divergent_definitions_total") == 0 and z.get("incompatibilities_total") == 0
        and z.get("unclassified_total") == 0 and z.get("incompatibilities_by_detail") == {}
        and z.get("head_pairs_with_incompatibility") == 0,
        "empty lane → every S3a split total zero + empty by-detail map (dormant lane can never error)")
    chk(set(z.keys()) == EXPECTED_KEYS, f"empty output carries exactly the expected key set ({sorted(z.keys())})")

    # ── record a MIXED-CLASS fixture through the REAL recorder (9-arg S3a signature + one legacy 7-arg) ─
    rec9 = "SELECT core.record_compat_finding_with_authority(%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    rec7 = "SELECT core.record_compat_finding_with_authority(%s,%s,%s,%s,%s,%s,%s)"
    evs = [
        # tenant A — pair (P1,C1) on REPO_A1: two contract deltas + one rebase observation
        app_a(rec9, (REPO_A1, BRANCH, PATH_POISON, SHA_P1, SHA_C1, FP1, RULE_DELTA_ADD, CLASS_DELTA, DET)),
        app_a(rec9, (REPO_A1, BRANCH, PATH_POISON, SHA_P1, SHA_C1, FP2, RULE_DELTA_OPT, CLASS_DELTA, DET)),
        app_a(rec9, (REPO_A1, BRANCH, PATH_POISON, SHA_P1, SHA_C1, FP3, RULE_REBASE, CLASS_REBASE, DET)),
        # tenant A — pair (P2,C2) on REPO_A2: one divergence observation + one evidence-backed mismatch
        app_a(rec9, (REPO_A2, BRANCH, PATH_POISON, SHA_P2, SHA_C2, FP4, RULE_DIVERGENT, CLASS_DIVERGENT, DET)),
        app_a(rec9, (REPO_A2, BRANCH, PATH_POISON, SHA_P2, SHA_C2, FP5, RULE_MISM_POS, CLASS_INCOMPAT, DET)),
        # tenant A — pair (P4,C4) on REPO_A1: a LEGACY 7-arg row (pre-S3a shape → unclassified)
        app_a(rec7, (REPO_A1, BRANCH, PATH_POISON, SHA_P4, SHA_C4, FP6, RULE_LEGACY)),
        # tenant B — pair (P3,C3) on REPO_B: one evidence-backed mismatch
        app_b(rec9, (REPO_B, BRANCH, PATH_POISON, SHA_P3, SHA_C3, FP7, RULE_MISM_REM, CLASS_INCOMPAT, DET)),
    ]
    chk(all(isinstance(e, str) and e.startswith("EV-COMPAT-") for e in evs),
        "recorded the 7-row mixed-class fixture through the real recorder (6 under A incl. 1 legacy, 1 under B)")

    # ── (2) AGGREGATES CORRECT + CROSS-TENANT + the S3a TAXONOMY SPLIT ──────────────────────────────────
    s = _j(app_a("SELECT core.owner_compat_shadow_surface()"))
    chk(s.get("observations_total") == 7,
        f"observations_total (RENAMED from findings_total) sees EVERY tenant's rows, ALL classes "
        f"(7 = 6 from A + 1 from B; got {s.get('observations_total')})")
    chk("findings_total" not in s, "the conflating findings_total key is GONE (renamed, not duplicated)")
    chk(s.get("contract_deltas_total") == 2 and s.get("rebase_needed_total") == 1
        and s.get("divergent_definitions_total") == 1 and s.get("unclassified_total") == 1,
        f"observation classes split exactly (deltas={s.get('contract_deltas_total')} "
        f"rebase={s.get('rebase_needed_total')} divergent={s.get('divergent_definitions_total')} "
        f"unclassified(legacy)={s.get('unclassified_total')})")
    chk(s.get("incompatibilities_total") == 2,
        f"incompatibilities_total counts ONLY the evidence_backed_incompatibility class "
        f"(2 mismatches; got {s.get('incompatibilities_total')})")
    chk(s.get("contract_deltas_total") + s.get("rebase_needed_total") + s.get("divergent_definitions_total")
        + s.get("incompatibilities_total") + s.get("unclassified_total") == s.get("observations_total"),
        "the class totals SUM exactly to observations_total (nothing double-counted, nothing dropped)")
    chk(s.get("incompatibilities_total") < s.get("observations_total")
        and s.get("incompatibilities_total") == 2 and s.get("observations_total") == 7,
        "observations vs incompatibilities NEVER conflated: the breakage-claim count stays the evidence "
        "class only while the grand total carries every class")
    chk(s.get("incompatibilities_by_detail") == {RULE_MISM_POS: 1, RULE_MISM_REM: 1},
        f"incompatibilities_by_detail = per-detail counts of the evidence class ONLY "
        f"({s.get('incompatibilities_by_detail')})")
    chk(s.get("head_pairs_with_incompatibility") == 2,
        f"head_pairs_with_incompatibility = 2 (one evidence pair per tenant; the observation-only pairs "
        f"are excluded; got {s.get('head_pairs_with_incompatibility')})")
    chk(s.get("repos_observed") == 3,
        f"repos_observed = 3 distinct repos as a COUNT only (got {s.get('repos_observed')})")
    chk(s.get("head_pairs_observed") == 4,
        f"head_pairs_observed = 4 distinct head pairs (pair1 shared by three rows; got {s.get('head_pairs_observed')})")
    chk(s.get("accounts_with_findings") == 2,
        f"accounts_with_findings = 2 (got {s.get('accounts_with_findings')})")
    rules = s.get("findings_by_rule") or {}
    chk(rules == {RULE_DELTA_ADD: 1, RULE_DELTA_OPT: 1, RULE_REBASE: 1, RULE_DIVERGENT: 1,
                  RULE_MISM_POS: 1, RULE_MISM_REM: 1, RULE_LEGACY: 1},
        f"findings_by_rule carries exact per-rule-id counts across tenants, all classes ({rules})")
    first, last = s.get("first_observed_at"), s.get("last_observed_at")
    chk(bool(first) and bool(last) and str(first) <= str(last),
        f"first/last observed timestamps present and ordered ({first} <= {last})")
    chk(s.get("account_count") == 2 and s.get("accounts_scanned") == 2 and s.get("capped") is False,
        f"bound honesty at default cap: 2 accounts, all scanned, not capped "
        f"(count={s.get('account_count')} scanned={s.get('accounts_scanned')} capped={s.get('capped')})")

    # ── (3) CONTENT-FREE — strictly aggregate: no path/repo/branch/symbol/PR/SHA/fingerprint ────────────
    blob = json.dumps(s)
    poisons = {
        "path": "leakpath", "repo name": "leakrepo", "repo name 2": "leaklib", "repo name 3": "leakother",
        "org prefix": "acme/", "path prefix": "src/", "branch": BRANCH,
        "producer sha": "a" * 7, "consumer sha": "b" * 7, "sha pair 2": "c" * 7, "sha pair 3": "e" * 7,
        "sha pair 4": "1a1a1a1", "fingerprint": "fpleak", "event id": "EV-COMPAT",
    }
    leaked = [name for name, tok in poisons.items() if tok in blob]
    chk(not leaked, f"NO leakage in the surface output — paths/repos/branches/SHAs/fingerprints absent "
                    f"(the S3a split keys add class/detail CODES only; leaked: {leaked or 'none'})")
    chk(set(s.keys()) == EXPECTED_KEYS,
        "output carries EXACTLY the aggregate key set (no per-finding / per-repo / per-PR rows)")

    # ── (4) OWNER-ONLY: a buyer/tenant role cannot execute the lens ─────────────────────────────────────
    tconn = psycopg2.connect(f"postgresql://veripsa_demo_agent@localhost/{DB}")
    tconn.autocommit = True
    refused = False
    try:
        with tconn.cursor() as c:
            c.execute("SET search_path=core")
            c.execute("SELECT core.owner_compat_shadow_surface()")
    except psycopg2.errors.InsufficientPrivilege:
        refused = True
    except Exception as e:
        refused = "permission denied" in str(e).lower()
    chk(refused, "OWNER-ONLY: a buyer/tenant role (veripsa_demo_agent) cannot execute the cross-tenant lens")

    # ── (5) BOUNDED: p_cap=1 visits one account and flags the top-cap view honestly ─────────────────────
    b = _j(app_a("SELECT core.owner_compat_shadow_surface(1)"))
    first_acct = min(acct_a, acct_b)          # deterministic account_id order — the scanned one
    expect_n, expect_inc = (6, 1) if first_acct == acct_a else (1, 1)
    chk(b.get("accounts_scanned") == 1 and b.get("capped") is True and b.get("account_count") == 2
        and b.get("observations_total") == expect_n and b.get("incompatibilities_total") == expect_inc,
        f"p_cap=1 → one account scanned, capped=true, totals (incl. the split) cover the scanned set only "
        f"(scanned={b.get('accounts_scanned')} capped={b.get('capped')} obs={b.get('observations_total')} "
        f"inc={b.get('incompatibilities_total')} expected {expect_n}/{expect_inc})")

    # ── (6) RENDERER (pure, no DB beyond the dict already fetched): never raises, zeros on None ─────────
    txt_none = cost_report.render_compat_shadow(None)
    chk("COMPATIBILITY SHADOW" in txt_none and "Observations recorded (all classes) : 0" in txt_none
        and "Incompatibilities (evidence-backed) : 0" in txt_none
        and "never public proof" in txt_none and "zero observed" in txt_none,
        "render_compat_shadow(None) = honest all-zeros block (split included) with the internal-only banner")
    txt = cost_report.render_compat_shadow(s)
    chk("Observations recorded (all classes) : 7" in txt
        and "Incompatibilities (evidence-backed) : 2" in txt
        and "Head pairs w/ incompatibility       : 2" in txt
        and RULE_DELTA_ADD in txt and RULE_MISM_POS in txt and "never public proof" in txt,
        "render_compat_shadow(surface) renders the S3a split — observations visibly separate from "
        "evidence-backed incompatibilities — + rule ids + internal-only banner")
    chk(not any(tok in txt for tok in poisons.values()),
        "rendered text carries NO poison token (paths/repos/branches/SHAs/fingerprints absent)")
    old_shape = cost_report.render_compat_shadow({"findings_total": 5})
    chk("Observations recorded (all classes) : 5" in old_shape
        and "Incompatibilities (evidence-backed) : 0" in old_shape,
        "render_compat_shadow tolerates a PRE-S3a surface (old findings_total key) — grand total kept, "
        "split honestly zero")
    try:
        junk = cost_report.render_compat_shadow({"findings_by_rule": "x", "capped": 1, "findings_total": "?",
                                                 "incompatibilities_by_detail": ["not", "a", "dict"]})
        ok_junk = isinstance(junk, str) and "COMPATIBILITY SHADOW" in junk
    except Exception:
        ok_junk = False
    chk(ok_junk, "render_compat_shadow never raises on a junk-shaped surface")

    print("OWNER-COMPAT-SHADOW GATE:", "PASS" if all(checks) else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
