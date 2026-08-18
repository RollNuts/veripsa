#!/usr/bin/env python3
"""CODE-NODE INDEX gate — core._claim_adjacency's code_node lookup by `node_id` must stay an INDEX SCAN, not a
SEQ SCAN. This locks the fix from the perf scan (audit:perf 2026-06-23).

THE GAP THIS GUARDS (EXPLAIN-ANALYZE-measured): core._claim_adjacency (THE A→B blast-radius engine, run ONCE per
main_impact_surface — the SYNCHRONOUS GitHub-App check/render path) joins core.code_node on its `node_id` key in
node_file / edited_nodes (resolving a code_edge endpoint — ce.src — to the file that owns it, for the in_adj /
out_adj import-confirmation probes). core.code_node carried ONLY code_node_coord_path (…, path) — NO index on
node_id. So that join had to SEQ SCAN the whole coordinate's node set: at scale / on a freshly-ingested (cold-stats)
tenant the planner picks a NESTED LOOP and re-seq-scans code_node PER probed edge = O(edges × nodes). MEASURED on a
seeded graph: `code_node WHERE node_id=<value>` planned as Seq Scan (cost 71.65, 1826 rows removed by filter for a
SINGLE match) — the path behind _claim_adjacency's own recorded 8.3s@20k-node/200-claim blow-up and the gate-160
cold-stats spike. The fix is a pure ADDITIVE index, core.code_node (account_id, repo, branch, node_id): the RLS
account pin + coordinate + the join key, so the planner uses the full predicate. It flips the lookup to an INDEX
SCAN (MEASURED: cost 0.38..8.41, ~0.02ms, Index Cond on node_id) — byte-identical result, no behavior change.

TWO LOCKS (both must hold):
  1. STRUCTURAL (deterministic, machine-independent): core.code_node carries an index whose columns are EXACTLY
     (account_id, repo, branch, node_id) in that order — the RLS pin + coordinate + the node_id join key. A future
     edit that drops it (or reorders the columns so the planner can't serve the coordinate+node_id predicate)
     re-opens the seq-scan blow-up — fail loudly, not silently slow. Read from pg_index on a real bootstrapped DB
     (so a schema-file typo that fails to CREATE the index is caught too), not from the .sql text.
  2. BEHAVIOURAL (EXPLAIN, on a seeded > 200-node graph): the code_node access that _claim_adjacency uses to
     resolve a node_id (the node_file join `code_node.node_id = code_edge.src`) is an INDEX SCAN using THIS index,
     NOT a Seq Scan. We seed > 200 file nodes (the task's "over-200-node graph"), then EXPLAIN the exact inlined
     join under the NESTED-LOOP plan (enable_hashjoin/mergejoin off) — the cold/at-scale plan where the per-probe
     node_id lookup is the cost. Asserting "Index Scan using code_node_coord_nodeid" on the node_id access locks in
     that the seq-scan cannot regress: drop the index and this EXPLAIN reverts to "Seq Scan on … code_node".

Run:  python3 tests/test_node_index.py
"""
from __future__ import annotations

import os
import random
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

DB = "veripsa_nodeidx_" + str(os.getpid())   # PER-PID (parallel-safe), like every gate here
ACCT = "ACCT-DEMO"
REPO = "perf/nodeidx"
BRANCH = "main"
INDEX_NAME = "code_node_coord_nodeid"
# the EXACT column order the index must carry: RLS account pin + coordinate + the _claim_adjacency join key.
EXPECTED_COLS = ["account_id", "repo", "branch", "node_id"]
N_FILES = 400   # > 200 FILE nodes alone (the task's "over-200-node graph"); ~1800 nodes total with symbols.


def gen_seed(repo: str, seed: int = 42, n_files: int = N_FILES) -> str:
    """A deterministic, content-free synthetic graph the size of a real-ish repo — file + def/class symbols, hub
    files (high fan-in) + imports/contains/calls edges + in-flight claims, the shape core._claim_adjacency chews.
    Mirrors tests/test_perf_budget.gen_seed (same generator family). Triggers disabled ONLY to bulk-load (the gate
    under test only READS)."""
    random.seed(seed)
    dirs = ["core", "api", "web", "models", "services", "util", "handlers", "jobs", "auth", "db"]

    def fpath(i):
        return f"src/{dirs[i % len(dirs)]}/mod{(i // len(dirs)) % 20}/file{i}.py"

    files = [fpath(i) for i in range(n_files)]
    out = [
        "SET search_path=core,pg_catalog;",
        f"SELECT set_config('core.current_account','{ACCT}',false);",
        "BEGIN;",
        "ALTER TABLE core.code_node DISABLE TRIGGER trg_governed_code_node;",
        "ALTER TABLE core.code_edge DISABLE TRIGGER trg_governed_code_edge;",
        "ALTER TABLE core.graph_version DISABLE TRIGGER trg_governed_graph_version;",
        "ALTER TABLE core.claim DISABLE TRIGGER trg_governed_claim;",
    ]
    nodes = []
    sym_index = {}
    for i, p in enumerate(files):
        nodes.append(f"('{ACCT}','{repo}','{BRANCH}','{p}','file','{p}',NULL,'python',NULL,NULL)")
        k = max(1, int(random.gauss(4, 1.5)))
        line = 1
        syms = []
        for j in range(k):
            s = line
            e = line + random.randint(8, 40)
            line = e + random.randint(1, 4)
            name = f"sym_{i}_{j}"
            kind = "class" if (j == 0 and random.random() < 0.2) else "def"
            nodes.append(f"('{ACCT}','{repo}','{BRANCH}','{p}#{name}','{kind}','{p}','{name}','python',{s},{e})")
            syms.append(name)
        sym_index[p] = syms
    out.append("INSERT INTO core.code_node (account_id,repo,branch,node_id,node_kind,path,name,language,start_line,end_line) VALUES")
    out.append(",\n".join(nodes) + ";")
    edges = set()
    hubs = random.sample(range(n_files), 12)
    for h in hubs:
        for imp in random.sample(range(n_files), random.randint(15, 40)):
            if imp != h:
                edges.add((files[imp], files[h], "imports"))
    for i in range(n_files):
        for _ in range(random.randint(1, 4)):
            t = random.randrange(n_files)
            if t != i:
                edges.add((files[i], files[t], "imports"))
        for name in sym_index[files[i]]:
            edges.add((files[i], f"{files[i]}#{name}", "contains"))
        # CALLS edges (dst = bare symbol NAME, the extractor shape) so the in_adj/out_adj node_id probes fire.
        for _ in range(random.randint(2, 5)):
            target = files[random.randrange(n_files)]
            tsyms = sym_index.get(target, [])
            if target != files[i] and tsyms:
                edges.add((files[i], random.choice(tsyms), "calls"))
    out.append("INSERT INTO core.code_edge (account_id,repo,branch,src,dst,edge_kind) VALUES")
    out.append(",\n".join(f"('{ACCT}','{repo}','{BRANCH}','{s}','{d}','{k}')" for (s, d, k) in edges) + ";")
    out.append(
        f"INSERT INTO core.graph_version (account_id,repo,branch,node_count,edge_count) "
        f"VALUES ('{ACCT}','{repo}','{BRANCH}',{len(nodes)},{len(edges)}) "
        f"ON CONFLICT (account_id,repo,branch) DO UPDATE SET node_count=EXCLUDED.node_count;")
    # in-flight claims (active+waiting) so _claim_adjacency's `edited` set is non-trivial and the join actually runs.
    claim_rows = []
    for k, i in enumerate(random.sample(range(n_files), 30)):
        p = files[i]
        a, b = ("AG-A", "AG-B") if k % 2 == 0 else ("AG-B", "AG-A")
        claim_rows.append(f"('{ACCT}','{repo}','{BRANCH}','PR-{k}:{p}','{a}','PR-{k}','{p}','active',NULL)")
        claim_rows.append(f"('{ACCT}','{repo}','{BRANCH}','PR-{k+1000}:{p}','{b}','PR-{k+1000}','{p}','waiting',NULL)")
    out.append("INSERT INTO core.claim (account_id,repo,branch,claim_id,agent_id,change_id,target_path,claim_state,touched_ranges) VALUES")
    out.append(",\n".join(claim_rows) + ";")
    for t in ("code_node", "code_edge", "graph_version", "claim"):
        out.append(f"ALTER TABLE core.{t} ENABLE TRIGGER trg_governed_{t};")
    out.append("COMMIT;")
    out.append("ANALYZE core.code_node; ANALYZE core.code_edge; ANALYZE core.claim;")
    return "\n".join(out), len(nodes)


# the EXACT inlined node_file join core._claim_adjacency uses to resolve a node_id to its file (the in_adj probe:
# `node_file sf ON sf.node_id = ce.src`). We EXPLAIN this directly (the function is SECURITY DEFINER → a Function
# Scan that hides its internals from EXPLAIN at the call site), forcing the NESTED-LOOP plan so the per-probe
# node_id lookup — the cold/at-scale cost — is the access we assert on. defs/def_n/defs_ok mirror the function so
# the surrounding shape (and thus the planner's join choices) match the real body.
EXPLAIN_NODE_ID_PROBE = f"""
EXPLAIN (ANALYZE, VERBOSE)
WITH
edited AS (SELECT DISTINCT target_path AS path, agent_id, change_id FROM core.claim
   WHERE account_id='{ACCT}' AND repo='{REPO}' AND branch='{BRANCH}' AND claim_state='active'),
node_file AS (SELECT node_id, path FROM core.code_node
   WHERE account_id='{ACCT}' AND repo='{REPO}' AND branch='{BRANCH}'),
defs AS MATERIALIZED (SELECT name AS nm, path FROM core.code_node
   WHERE account_id='{ACCT}' AND repo='{REPO}' AND branch='{BRANCH}'
     AND node_kind IN ('def','class') AND name IS NOT NULL),
def_n AS MATERIALIZED (SELECT nm, count(DISTINCT path) AS n FROM defs GROUP BY nm),
defs_ok AS MATERIALIZED (SELECT d.nm, d.path FROM defs d JOIN def_n c ON c.nm=d.nm WHERE c.n <= 3),
edited_def_names AS (SELECT DISTINCT d.nm, d.path FROM defs_ok d JOIN edited e ON e.path=d.path)
SELECT edn.path AS ff, sf.path AS nb FROM core.code_edge ce
  JOIN edited_def_names edn ON edn.nm=ce.dst
  JOIN node_file sf ON sf.node_id=ce.src
  JOIN def_n dn ON dn.nm=ce.dst
 WHERE ce.account_id='{ACCT}' AND ce.repo='{REPO}' AND ce.branch='{BRANCH}' AND ce.edge_kind='calls';
"""


def main() -> int:
    checks = []
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1
    try:
        # ── LOCK 1: STRUCTURAL — the index EXISTS on a real bootstrapped DB with EXACTLY the right columns/order. ──
        # Read from pg_index/pg_attribute (the live catalog), so a schema-file typo that fails to CREATE the index,
        # or a column reorder, is caught — not just a text match in the .sql.
        admin = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(
                "SELECT a.attname "
                "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                "JOIN pg_class t ON t.oid=i.indrelid "
                "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum = ANY(i.indkey) "
                "WHERE c.relname=%s AND t.relname='code_node' AND t.relnamespace='core'::regnamespace "
                "ORDER BY array_position(i.indkey, a.attnum)", (INDEX_NAME,))
            cols = [row[0] for row in cur.fetchall()]
        admin.close()
        cols_ok = cols == EXPECTED_COLS
        checks.append((
            f"LOCK1 structural: core.code_node carries index `{INDEX_NAME}` with columns EXACTLY {EXPECTED_COLS} "
            f"(the RLS account pin + coordinate + the node_id join key) — measured {cols or 'MISSING'}; a drop or "
            f"reorder re-opens the _claim_adjacency seq-scan blow-up", cols_ok))

        # seed a > 200-node graph (the task's over-200-node graph) for the behavioural EXPLAIN.
        seed_sql, n_nodes = gen_seed(REPO)
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core,pg_catalog")
            cur.execute(seed_sql)
        conn.close()
        checks.append((
            f"the seeded graph is over 200 nodes (the task's over-200-node graph: {n_nodes} code_node rows)",
            n_nodes > 200))

        # ── LOCK 2: BEHAVIOURAL — _claim_adjacency's node_id lookup is an INDEX SCAN using THIS index, not a Seq Scan.
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        plan_lines = []
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core,pg_catalog")
            cur.execute(f"SELECT set_config('core.current_account','{ACCT}',false)")
            # force the NESTED-LOOP plan = the cold/at-scale reality where the per-probe node_id lookup is the cost
            # (a small warm graph would otherwise hash-join code_node once, hiding the per-probe seq-scan the index
            # fixes). With the index present the node_id probe is an Index Scan; without it, it is a Seq Scan.
            cur.execute("SET enable_hashjoin=off")
            cur.execute("SET enable_mergejoin=off")
            cur.execute(EXPLAIN_NODE_ID_PROBE)
            plan_lines = [row[0] for row in cur.fetchall()]
        conn.close()
        plan = "\n".join(plan_lines)

        # the node_id access (Index Cond includes `node_id = ce.src`) must be an INDEX SCAN using our index.
        node_id_index_scan = False
        for ln in plan_lines:
            s = ln.strip()
            if s.startswith("->") and "Index Scan" in s and INDEX_NAME in s:
                node_id_index_scan = True
                break
        # the Index Cond proving it is the NODE_ID predicate being served (not some unrelated index use).
        index_cond_is_nodeid = ("node_id = ce.src" in plan) or ("(code_node.node_id = ce.src)" in plan)
        # NEGATIVE anchor: the code_node node_id access must NOT have fallen back to a Seq Scan (the regression).
        # (`defs` legitimately scans code_node by node_kind — a DIFFERENT access path; we only forbid a Seq Scan
        # that carries the node_id join, which manifests as a Seq Scan whose parent join key is code_node.node_id.
        # Simplest robust proxy: the node_id Index Scan above is present AND its Index Cond is the node_id pred.)
        behavioural_ok = node_id_index_scan and index_cond_is_nodeid
        checks.append((
            f"LOCK2 behavioural: _claim_adjacency's code_node lookup by node_id (node_file join "
            f"`code_node.node_id = code_edge.src`) is an INDEX SCAN using `{INDEX_NAME}` (NOT a Seq Scan) on the "
            f"seeded > 200-node graph, under the cold/at-scale nested-loop plan — index_scan={node_id_index_scan} "
            f"index_cond_is_nodeid={index_cond_is_nodeid}", behavioural_ok))
        if not behavioural_ok:
            print("  ---- EXPLAIN (node_id probe) ----")
            for ln in plan_lines:
                print("   ", ln)
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)

    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("CODE NODE INDEX GATE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
