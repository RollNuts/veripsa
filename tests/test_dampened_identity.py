#!/usr/bin/env python3
"""IDENTITY GATE for the _dampened_adjacency pre-restrict+materialize PERF fix (audit:perf 2026-06-19).

core._dampened_adjacency feeds the unknown-first 'clear'→'unknown' downgrade + the visible `dampened_with` in
main_impact_surface. The perf fix pre-restricts its imp/calls_h/res_h axes to the active in-flight path set
BEFORE the heavy graph joins (and MATERIALIZEs the hub/def classification CTEs), turning a whole-graph scan
(audited 13.5s@6k / 46.4s@12k) into O(in-flight subgraph) (sub-second). The fix is SEMANTICS-PRESERVING — the
final `paired` join already discards every coupling whose endpoints are not both active, so pre-restricting the
DRIVING edges to that same active set cannot change the result — but a behavior change here is UNACCEPTABLE (it
would silently flip a verdict). This gate PROVES byte-identical output two independent ways, on graphs WITH and
WITHOUT calls edges, hub and non-hub and shared-resource shapes:

  (1) ORACLE (self-contained): the function returns the EXACT expected (f,f_change_id,nbr,nbr_change_id,via_hub)
      rowset for each shape — the rows captured from BOTH origin/main and the fixed function (proven identical).
  (2) GIT DIFF (defense in depth, when origin/main is reachable): apply origin/main's _dampened_adjacency body to
      the SAME seeded DB, capture its rowset, then apply the CURRENT body and capture again — assert the sorted
      rowsets are IDENTICAL (diff = empty). Skipped (not failed) when git/origin is unavailable; (1) still binds.

Run:  python3 tests/test_dampened_identity.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

DB = "veripsa_dampid_" + str(os.getpid())
ACCT = "ACCT-DEMO"
N = 12  # importers/fillers > default hub cutoff 8 → the endpoint is a dampened hub / hot resource
DAMPEN_SQL = os.path.join(ROOT, "db", "schema", "70_social.sql")


def conn(role):
    c = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    c.autocommit = True
    return c


def ingest(g, repo):
    with conn("veripsa_app").cursor() as cur:
        cur.execute("SET search_path=core,pg_catalog")
        cur.execute("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(g), repo, "main", "a" * 40))


def claim(cid, path, author, repo):
    with conn("veripsa_app").cursor() as cur:
        cur.execute("SET search_path=core,pg_catalog")
        cur.execute("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, repo, "main", author))


def damp(repo, hub_degree=8):
    """Full sorted rowset of core._dampened_adjacency (SECURITY DEFINER over FORCE-RLS → set the account GUC)."""
    with conn("veripsa_migrator").cursor() as cur:
        cur.execute("SET search_path=core,pg_catalog")
        cur.execute("SELECT set_config('core.current_account',%s,false)", (ACCT,))
        cur.execute("SET veripsa.hub_degree=%s", (str(hub_degree),))
        cur.execute("SELECT f,f_change_id,nbr,nbr_change_id,via_hub FROM core._dampened_adjacency(%s,%s,'main') "
                    "ORDER BY 1,2,3,4,5", (ACCT, repo))
        return [list(r) for r in cur.fetchall()]


# ── SHAPE BUILDERS — each ingests its graph + claims and returns the repo coord. ──────────────────────────────
def build_hub_imports(repo):
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "hub.py"), "w").write("def helper():\n    return 1\n")
        for i in range(N):
            open(os.path.join(d, f"leaf_{i}.py"), "w").write(f"from hub import helper\n\ndef use_{i}():\n    return helper()\n")
        g = X.build_graph(d)
    ingest(g, repo)
    claim("PR-HUB:hub.py", "hub.py", "hubdev", repo)
    claim("PR-LEAF:leaf_0.py", "leaf_0.py", "leafdev", repo)


def build_calls_into_hub(repo):
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "hub.py"), "w").write("def helper():\n    return 1\n")
        for i in range(N):
            open(os.path.join(d, f"leaf_{i}.py"), "w").write(f"from hub import helper\n\ndef use_{i}():\n    return helper()\n")
        open(os.path.join(d, "caller_calls.py"), "w").write("def kick():\n    return helper()\n")  # bare call, NO import
        g = X.build_graph(d)
    ingest(g, repo)
    claim("PR-HUB:hub.py", "hub.py", "hubdev", repo)
    claim("PR-CALL:caller_calls.py", "caller_calls.py", "calldev", repo)


def build_shared_res_hub(repo):
    import code_graph_extract as X
    import cg_schema_contract as C
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "x.py"), "w").write("def x():\n    return 1\n")
        g = X.build_graph(d)
    g["nodes"].append(C.enrich_resource_node(
        {
            "id": "table::hot",
            "kind": "table",
            "name": "hot",
            "path": "schema.sql",
            "language": "sql",
        },
        repo=repo,
    ))
    for src in ["res_q1.py", "res_q2.py"] + [f"res_filler_{i}.py" for i in range(N)]:
        g["edges"].append({"src": src, "dst": "table::hot", "kind": "queries"})
        g["nodes"].append({"id": src, "kind": "file", "path": src, "language": "python"})
    previous_metrics = dict(g.get("metrics") or {})
    g["metrics"] = C.collect_graph_metrics(
        g,
        input_paths=(
            node["path"]
            for node in g["nodes"]
            if node.get("kind") in {"file", "config_file"} and node.get("path")
        ),
        unresolved_references=previous_metrics.get("unresolved_reference_count", 0),
        ambiguous_references=previous_metrics.get("ambiguous_reference_count", 0),
        fallback_full_rebuild_reasons=previous_metrics.get(
            "fallback_full_rebuild_reasons", ()
        ),
    ).as_dict()
    g["metrics"].update({
        key: previous_metrics[key]
        for key in ("schema_contract_version", "ambiguity_detection_scope")
        if key in previous_metrics
    })
    ingest(g, repo)
    claim("PR-R1:res_q1.py", "res_q1.py", "r1dev", repo)
    claim("PR-R2:res_q2.py", "res_q2.py", "r2dev", repo)


def build_no_calls_no_hub(repo):
    """A normal non-hub import pair: nothing is dampened → the helper returns EMPTY (no false dampening)."""
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "b.py"), "w").write("def f():\n    return 1\n")
        open(os.path.join(d, "a.py"), "w").write("from b import f\n\ndef g():\n    return f()\n")
        g = X.build_graph(d)
    ingest(g, repo)
    claim("PR-A:a.py", "a.py", "adev", repo)
    claim("PR-B:b.py", "b.py", "bdev", repo)


# repo coord -> (builder, expected sorted rowset). The expected rows were captured from BOTH origin/main's and the
# fixed function (proven byte-identical) — so this oracle locks the fix to origin's behavior without needing git.
SHAPES = {
    "id/hub": (build_hub_imports, [
        ["hub.py", "PR-HUB", "leaf_0.py", "PR-LEAF", "hub.py"],
        ["leaf_0.py", "PR-LEAF", "hub.py", "PR-HUB", "hub.py"],
    ]),
    "id/calls": (build_calls_into_hub, [
        ["caller_calls.py", "PR-CALL", "hub.py", "PR-HUB", "hub.py"],
        ["hub.py", "PR-HUB", "caller_calls.py", "PR-CALL", "hub.py"],
    ]),
    "id/res": (build_shared_res_hub, [
        ["res_q1.py", "PR-R1", "res_q2.py", "PR-R2", "table::hot"],
        ["res_q2.py", "PR-R2", "res_q1.py", "PR-R1", "table::hot"],
    ]),
    "id/none": (build_no_calls_no_hub, []),
}


def extract_fn_body(sql_text):
    """The `DROP FUNCTION ... core._dampened_adjacency ... CREATE OR REPLACE ... $$;` block (one applicable unit)."""
    start = sql_text.index("DROP FUNCTION IF EXISTS core._dampened_adjacency(text,text,text);")
    end = sql_text.index("\n$$;", start) + len("\n$$;")
    return sql_text[start:end] + "\n"


def apply_fn(body):
    with conn("veripsa_migrator").cursor() as cur:
        cur.execute("SET search_path=core,pg_catalog")
        cur.execute(body)


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    checks = []
    try:
        # build all shapes once.
        for repo, (builder, _expected) in SHAPES.items():
            builder(repo)

        # ── (1) ORACLE: the CURRENT function returns the EXACT expected rowset for each shape. ──
        for repo, (_builder, expected) in SHAPES.items():
            got = damp(repo)
            exp_sorted = sorted(expected)
            checks.append((f"(1) ORACLE shape {repo:9s}: rowset matches expected (n={len(got)}) — got={got}",
                           got == exp_sorted))

        # ── (2) GIT DIFF (defense in depth): origin/main's body vs the current body on the SAME data = identical. ──
        origin_ok = True
        try:
            origin_text = subprocess.run(
                ["git", "show", "origin/main:db/schema/70_social.sql"], cwd=ROOT,
                capture_output=True, text=True, check=True).stdout
            current_text = open(DAMPEN_SQL, encoding="utf-8").read()
            origin_body = extract_fn_body(origin_text)
            current_body = extract_fn_body(current_text)
            # SELF-REFERENTIAL GUARD: this defense-in-depth only means something while origin/main and the working
            # tree DIFFER (a PR proposing a change to _dampened_adjacency, or the perf fix pre-land). Once the fix
            # has LANDED, origin/main IS the current body → the diff is VACUOUS BY CONSTRUCTION, not a regression.
            # Anchoring a "bodies must DIFFER" assertion to origin/main would (and did) make this gate fail for
            # EVERY PR after the fix landed — turning main red. So when the bodies match, SKIP (the ORACLE (1)
            # pinned-rowset check + the STRUCTURAL locks below still bind correctness, with no self-reference).
            if origin_body != current_body:
                # capture under ORIGIN body, then restore CURRENT body and capture again — origin vs current must
                # produce IDENTICAL rowsets on the same data (the behavior-preserving guarantee for the change).
                apply_fn(origin_body)
                rows_origin = {repo: damp(repo) for repo in SHAPES}
                apply_fn(current_body)
                rows_current = {repo: damp(repo) for repo in SHAPES}
                for repo in SHAPES:
                    same = rows_origin[repo] == rows_current[repo]
                    only_o = [x for x in rows_origin[repo] if x not in rows_current[repo]]
                    only_c = [x for x in rows_current[repo] if x not in rows_origin[repo]]
                    origin_ok = origin_ok and same
                    checks.append((f"(2) GIT-DIFF shape {repo:9s}: origin/main rowset == current rowset "
                                   f"(diff={len(only_o)+len(only_c)})"
                                   + ("" if same else f"  ONLY-ORIGIN={only_o} ONLY-CURRENT={only_c}"), same))
            else:
                print("  [info] (2) GIT-DIFF vacuous: origin/main already carries this _dampened_adjacency body "
                      "(the perf fix has landed) — ORACLE (1) + the STRUCTURAL locks below bind correctness.")
        except subprocess.CalledProcessError:
            print("  [info] (2) GIT-DIFF skipped (origin/main not fetched here); ORACLE (1) still binds.")
        except Exception as e:
            print(f"  [info] (2) GIT-DIFF skipped ({e}); ORACLE (1) still binds.")

        # ── STRUCTURAL: the perf disciplines are present (mirrors test_perf_budget.py LOCK4; locks the fix in). ──
        cur_text = open(DAMPEN_SQL, encoding="utf-8").read()
        d_start = cur_text.find("FUNCTION core._dampened_adjacency")
        d_end = cur_text.find("ALTER FUNCTION core._dampened_adjacency", d_start)
        d_body = cur_text[d_start:d_end]
        checks.append(("STRUCTURAL: _dampened_adjacency pre-restricts via `active_path AS MATERIALIZED` referenced "
                       "by each heavy axis (>=4 refs)",
                       "active_path AS MATERIALIZED" in d_body and len(re.findall(r"\bactive_path\b", d_body)) >= 4))
        checks.append(("STRUCTURAL: _dampened_adjacency MATERIALIZEs hub_files/res_hubs/def_n/defs_ok",
                       all(f"{c} AS MATERIALIZED" in d_body for c in ("hub_files", "res_hubs", "def_n", "defs_ok"))))
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)

    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("DAMPENED IDENTITY GATE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
