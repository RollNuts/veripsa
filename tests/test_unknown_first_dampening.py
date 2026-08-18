#!/usr/bin/env python3
"""UNKNOWN-FIRST INVARIANT GATE (AUDIT3) — the launch-critical regression guard.

THE INVARIANT: Veripsa never renders a CONFIDENT 'clear' for two in-flight changes that are REALLY,
DIRECTLY coupled by an extracted import edge. If hub dampening suppresses that edge (to avoid the
importer-wall noise), the verdict must be 'unknown' (honest) with the suppressed coupling SURFACED in
`dampened_with` — NEVER a silent 'clear', and NEVER a noise-exploding 'warn'.

WHY this exists: the audit PROVED a silent false-'clear'. A PR editing flask/globals.py + a PR editing one
of its 17 importers BOTH rendered 'clear' (empty contested_with, no signal) because dampening dropped the
real import edge and the verdict fell through to 'clear'. That is exactly the "missed collision rendered
safe" failure Veripsa sells against. This gate reproduces the minimal shape and locks the fix so the silent
'clear' can never come back, while proving the noise suppression is still intact (it stays 'unknown', not a
20-way 'warn').

Builds the shape against the REAL gate (db functions), tunes the hub cutoff via the GUC, asserts:
  1. cutoff RAISED above the hub's in-degree (no dampening) → the coupling WARNS (the edge is real).
  2. cutoff at default (dampening on) → the importer PR is 'unknown', NOT 'clear', NOT 'warn'.
  3. ...and its dampened_with names the hub-editing PR + the via_hub path (silence made visible).
  4. the HUB-editing PR is ALSO 'unknown' (symmetric — its change affects in-flight importers).
  5. NO EXPLOSION: a hub edit with NO in-flight importers stays 'clear' (the fix does not over-fire).
  6. a non-hub direct coupling is unaffected (still 'warn').

SYMMETRY ACROSS ALL THREE HUB-DROP AXES (2026-06-18): _claim_adjacency drops hub noise on imports AND
calls-into-hub AND shared-resource (res_hubs), but this guard originally recovered ONLY imports — so two
in-flight changes coupled ONLY via a calls-into-a-hub-file or a shared HOT table fell through to the same
silent 'clear'. This gate now also asserts (proven to FAIL pre-fix = a real silent miss, not a vacuous guard):
  (b) CALLS-INTO-HUB: a caller that calls a symbol DEFINED in a hub file (no import; the definer file is the
      hub) + the hub, both in-flight → 'unknown' with via_hub = the hub file (was a silent 'clear').
  (c) SHARED RES-HUB: two PRs coupled ONLY by both touching a HOT shared table (∈ res_hubs) → both 'unknown'
      with via_hub = the hot resource (was a silent 'clear').
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
# Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_server.py.
DB = "veripsa_uf_dampen_" + str(os.getpid())
REPO = "acme/uf"
N = 12   # importers of the hub (> default cutoff 8 → hub.py is a dampened hub)


def db(role, sql, args=(), hub_degree=None):
    conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            if hub_degree is not None:
                cur.execute("SET veripsa.hub_degree = %s", (str(hub_degree),))
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def build_graph():
    import code_graph_extract as X
    import cg_schema_contract as C
    with tempfile.TemporaryDirectory() as d:
        # the HUB: hub.py imported by N leaves (in-degree N > 8 = dampened)
        with open(os.path.join(d, "hub.py"), "w") as fh:
            fh.write("def helper():\n    return 1\n")
        for i in range(N):
            with open(os.path.join(d, f"leaf_{i}.py"), "w") as fh:
                fh.write(f"from hub import helper\n\ndef use_{i}():\n    return helper()\n")
        # a SOLO hub-like file edited alone (its importers NOT in-flight) — must stay clear (no over-fire)
        with open(os.path.join(d, "lonely_hub.py"), "w") as fh:
            fh.write("def lonely():\n    return 9\n")
        for i in range(N):
            with open(os.path.join(d, f"quiet_{i}.py"), "w") as fh:
                fh.write(f"from lonely_hub import lonely\n\ndef q_{i}():\n    return lonely()\n")
        # a NORMAL (non-hub) coupling: pair_a imports pair_b (1 importer → not a hub) → must still warn
        with open(os.path.join(d, "pair_b.py"), "w") as fh:
            fh.write("def only_one():\n    return 2\n")
        with open(os.path.join(d, "pair_a.py"), "w") as fh:
            fh.write("from pair_b import only_one\n\ndef go():\n    return only_one()\n")
        # ── CALLS-INTO-HUB axis (FIX1 b): a caller that calls helper() — DEFINED ONLY in the hub (unambiguous) —
        #    WITHOUT importing it. _claim_adjacency drops this calls coupling because the DEFINER FILE (hub.py) is
        #    a hub (out_adj/in_adj `path NOT IN hub_files`). With the caller + hub both in-flight, the old guard
        #    only recovered the IMPORT axis → this fell through to a silent 'clear'. Now recovered via calls_h.
        with open(os.path.join(d, "caller_calls.py"), "w") as fh:
            fh.write("def kick():\n    return helper()\n")   # bare call, NO import of hub → coupling is calls-only
        g = X.build_graph(d)
        # ── SHARED RES-HUB axis (FIX1 c): a HOT table touched (queries/alters) by > the cutoff distinct files, so
        #    res_adj drops the coupling (`ce.dst NOT IN res_hubs`). Hand-appended schema subgraph (the exact
        #    code_edge rows the extractor mints for .sql/ORM, content-free: a table NAME + edges) — robust and
        #    minimal vs. authoring >8 real ORM/SQL files. res_q1.py + res_q2.py are the two IN-FLIGHT co-touchers;
        #    the rest are the filler that makes `hot` a res_hub (> default cutoff 8 distinct srcs).
        g["nodes"].append(C.enrich_resource_node(
            {
                "id": "table::hot",
                "kind": "table",
                "name": "hot",
                "path": "schema.sql",
                "language": "sql",
            },
            repo=REPO,
        ))
        for src in ["res_q1.py", "res_q2.py"] + [f"res_filler_{i}.py" for i in range(N)]:
            g["edges"].append({"src": src, "dst": "table::hot", "kind": "queries"})
            # each toucher also needs a FILE node so it is a real path the engine sees (content-free file node).
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
        return g


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    graph = build_graph()
    db("veripsa_app", "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", "a" * 40))

    def claim(cid, path, author):
        db("veripsa_app", "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, REPO, "main", author))

    # in-flight: the hub + ONE of its importers (different authors) — the silent-clear shape.
    claim("PR-HUB:hub.py", "hub.py", "hubdev")
    claim("PR-LEAF:leaf_0.py", "leaf_0.py", "leafdev")
    # the over-fire control: ONLY the lonely hub edited; its importers are NOT in-flight.
    claim("PR-LONELY:lonely_hub.py", "lonely_hub.py", "lonedev")
    # the non-hub control: pair_a + pair_b (different authors) — must still warn.
    claim("PR-PA:pair_a.py", "pair_a.py", "padev")
    claim("PR-PB:pair_b.py", "pair_b.py", "pbdev")
    # CALLS-INTO-HUB (FIX1 b): the caller (calls helper(), defined only in hub.py) + the hub, both in-flight,
    # DIFFERENT authors. _claim_adjacency drops this calls coupling (definer file hub.py is a hub) → without the
    # fix it is a silent 'clear'; now it must be 'unknown' with the hub surfaced as via_hub.
    claim("PR-CALL:caller_calls.py", "caller_calls.py", "calldev")
    # SHARED RES-HUB (FIX1 c): two files both query the HOT table `hot` (a res_hub), DIFFERENT authors.
    # res_adj drops this coupling (`hot` ∈ res_hubs) → without the fix a silent 'clear'; now 'unknown' via `hot`.
    claim("PR-RES1:res_q1.py", "res_q1.py", "res1dev")
    claim("PR-RES2:res_q2.py", "res_q2.py", "res2dev")

    def impact(hub_degree):
        imp = db("veripsa_app", "SELECT core.main_impact_surface(%s,%s)", (REPO, "main"), hub_degree=hub_degree)
        if isinstance(imp, str):
            imp = json.loads(imp)
        return {c["change_id"]: c for c in imp.get("changes", [])}

    undamped = impact(hub_degree=100)   # cutoff above N → no dampening
    damped = impact(hub_degree=8)       # default → hub.py dampened (in-degree N > 8)

    leaf_undamped = undamped.get("PR-LEAF", {})
    leaf = damped.get("PR-LEAF", {})
    hub = damped.get("PR-HUB", {})
    lonely = damped.get("PR-LONELY", {})
    pa = damped.get("PR-PA", {})
    leaf_dw = leaf.get("dampened_with", []) or []
    hub_dw = hub.get("dampened_with", []) or []
    # FIX1 b (calls-into-hub) + c (shared res-hub):
    call_undamped = undamped.get("PR-CALL", {})
    call = damped.get("PR-CALL", {})
    call_dw = call.get("dampened_with", []) or []
    res1_undamped = undamped.get("PR-RES1", {})
    res1 = damped.get("PR-RES1", {})
    res2 = damped.get("PR-RES2", {})
    res1_dw = res1.get("dampened_with", []) or []

    checks = []
    # 1) the edge is REAL: without dampening the leaf<->hub coupling warns.
    checks.append((f"without dampening the leaf<->hub coupling WARNS — the edge is real (got '{leaf_undamped.get('verdict')}')",
                   leaf_undamped.get("verdict") == "warn"))
    # 2) THE INVARIANT: with dampening the importer PR is NEVER a silent 'clear'.
    checks.append((f"INVARIANT: the dampened importer PR is NOT a silent 'clear' (got '{leaf.get('verdict')}')",
                   leaf.get("verdict") != "clear"))
    checks.append((f"INVARIANT: the dampened importer PR does NOT explode to 'warn' (got '{leaf.get('verdict')}')",
                   leaf.get("verdict") != "warn"))
    checks.append((f"INVARIANT: the dampened importer PR is honest 'unknown' (got '{leaf.get('verdict')}')",
                   leaf.get("verdict") == "unknown"))
    # 3) the suppressed coupling is SURFACED (silence made visible).
    checks.append((f"VISIBLE: importer PR's dampened_with names the hub-editing PR + via_hub (got {leaf_dw})",
                   any(("hubdev" in str(e.get("by", "")) and e.get("via_hub") == "hub.py") for e in leaf_dw)))
    # 4) symmetric: the hub-editing PR is ALSO unknown (its change affects in-flight importers).
    checks.append((f"SYMMETRIC: the hub-editing PR is also 'unknown' with the importer surfaced (got '{hub.get('verdict')}', dw={len(hub_dw)})",
                   hub.get("verdict") == "unknown" and any("leafdev" in str(e.get("by", "")) for e in hub_dw)))
    # 5) NO OVER-FIRE: a hub edited with NO in-flight importers stays clear.
    checks.append((f"NO OVER-FIRE: a hub with NO in-flight importers stays 'clear' (got '{lonely.get('verdict')}')",
                   lonely.get("verdict") == "clear"))
    # 6) non-hub coupling unaffected.
    checks.append((f"non-hub direct coupling STILL warns (got '{pa.get('verdict')}')",
                   pa.get("verdict") == "warn"))

    # ── FIX1 (b) CALLS-INTO-HUB: a calls coupling whose DEFINER FILE is a hub was a silent 'clear'; now 'unknown'.
    # the edge is REAL: without dampening the caller<->hub calls coupling warns.
    checks.append((f"(b) without dampening the calls-into-hub coupling WARNS — the edge is real (got '{call_undamped.get('verdict')}')",
                   call_undamped.get("verdict") == "warn"))
    checks.append((f"(b) INVARIANT: the dampened CALLS-into-hub PR is NOT a silent 'clear' (got '{call.get('verdict')}')",
                   call.get("verdict") != "clear"))
    checks.append((f"(b) the CALLS-into-hub PR is honest 'unknown', not exploded to 'warn' (got '{call.get('verdict')}')",
                   call.get("verdict") == "unknown"))
    checks.append((f"(b) VISIBLE: its dampened_with names the hub-editing PR + via_hub=hub.py (got {call_dw})",
                   any(("hubdev" in str(e.get("by", "")) and e.get("via_hub") == "hub.py") for e in call_dw)))
    # symmetric: the hub PR's dampened_with ALSO surfaces the caller (the hub change affects the in-flight caller).
    checks.append((f"(b) SYMMETRIC: the hub PR also surfaces the in-flight caller in dampened_with (got {hub_dw})",
                   any("calldev" in str(e.get("by", "")) for e in hub_dw)))

    # ── FIX1 (c) SHARED RES-HUB: two PRs coupled ONLY by both touching a HOT shared table were a silent 'clear'.
    checks.append((f"(c) without dampening the shared-resource coupling WARNS — the edge is real (got '{res1_undamped.get('verdict')}')",
                   res1_undamped.get("verdict") == "warn"))
    checks.append((f"(c) INVARIANT: the res-hub-coupled PR is NOT a silent 'clear' (got '{res1.get('verdict')}')",
                   res1.get("verdict") != "clear"))
    checks.append((f"(c) the res-hub-coupled PR is honest 'unknown' (got '{res1.get('verdict')}')",
                   res1.get("verdict") == "unknown"))
    checks.append((f"(c) the OTHER res-hub PR is ALSO 'unknown' (symmetric) (got '{res2.get('verdict')}')",
                   res2.get("verdict") == "unknown"))
    checks.append((f"(c) VISIBLE: its dampened_with names the co-toucher + via_hub=the hot resource (got {res1_dw})",
                   any(("res2dev" in str(e.get("by", "")) and "hot" in str(e.get("via_hub", ""))) for e in res1_dw)))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("UNKNOWN-FIRST DAMPENING GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
