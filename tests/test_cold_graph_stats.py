#!/usr/bin/env python3
"""COLD-GRAPH-STATS gate — the FIRST main_impact_surface on a FRESH/LARGE graph must be BOUNDED, not a
multi-minute CPU-bound hang. This locks the root fix from the scale audit (#169, 2026-06-18).

THE REGRESSION THIS GUARDS (audit #169 residual): the live ingest path (make_db_processor → handle_event →
ingest_push → _full_ingest → core.ingest_graph_with_authority) does a BULK DELETE+INSERT of the WHOLE
coordinate into core.code_node / core.code_edge, but NEVER refreshed the PLANNER STATISTICS. Autovacuum's
ANALYZE runs on a DELAY, so the FIRST core.main_impact_surface after a fresh/large ingest planned the O(edges)
RECURSIVE adjacency over EMPTY/STALE stats (n_live_tup≈0, last_analyze NULL) → a mis-planned multi-minute
CPU-bound scan. Measured on a 13.6k-node / 18.4k-edge graph: ~121 SECONDS cold vs ~0.5s after ANALYZE —
byte-identical result. The webhook worker would block for minutes on a single big repo's first PR.

THE FIX (root, not band-aid): make_db_processor, AFTER the event's body txn COMMITS, issues a targeted
`ANALYZE core.code_node, core.code_edge` on the same (advisory-locked) connection flipped back to autocommit —
ONLY when a bulk load actually happened (pg_stat n_mod_since_analyze past a threshold). It must run OUTSIDE the
body txn because ANALYZE cannot run inside a transaction block, and the event body runs in ONE txn (#106). It is
content-free (touches only Postgres' own statistics) and never-crash (the graph is already correct; only the
plan is cold — a failed ANALYZE must not fail the delivery).

TWO LOCKS (both must hold), driven through the REAL live processor (make_db_processor) so the fix is exercised
exactly as in production — a real push-to-main event, a real bulk ingest, then the real brain call:
  1. STATS PRESENT: right after the live ingest, pg_stat_user_tables for code_node + code_edge shows the stats
     are FRESH — last_analyze populated, n_live_tup > 0, n_mod_since_analyze reset to 0. (Without the fix these
     stay NULL / 0 until autovacuum eventually catches up.)
  2. FIRST-CALL BOUNDED: the FIRST main_impact_surface on this fresh large graph completes well under a generous
     ceiling (the cold regression was ~121s; the fix runs ~0.5s). The ceiling catches the order-of-magnitude
     hang, not a slow runner.

Run:  python3 tests/test_cold_graph_stats.py
"""
from __future__ import annotations

import io
import json
import os
import random
import subprocess
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import psycopg2  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402

DB = "veripsa_coldstats_" + str(os.getpid())   # PER-PID (parallel-safe), like every gate here
REPO = "cold/bigrepo"
REPO_ID = 930_001
BRANCH = "main"
N_FILES = 3000

# Generous ABSOLUTE ceiling for the FIRST brain call on a fresh large graph. The cold-stats regression took
# ~121s; the fix runs in ~0.5s. 20s is two orders of magnitude under the regression yet ample headroom over the
# fix — it flags the multi-minute hang, never a slow CI runner.
FIRST_CALL_CEILING_S = 20.0


def big_graph(seed: int = 7) -> dict:
    """A deterministic, content-free synthetic graph the size of a real-ish repo: ~3000 files + def/class
    symbols, hub files (high fan-in) + random imports + contains edges. Mirrors tests/test_perf_budget.py's
    generator (same shape the brain's O(edges) adjacency chews). Returned in the {nodes,edges} shape
    code_graph_extract.build_graph produces — so we can feed it straight through the live ingest path."""
    random.seed(seed)
    dirs = ["core", "api", "web", "models", "services", "util", "handlers", "jobs", "auth", "db"]

    def fpath(i):
        return f"src/{dirs[i % len(dirs)]}/mod{(i // len(dirs)) % 20}/file{i}.py"

    files = [fpath(i) for i in range(N_FILES)]
    nodes, sym_index = [], {}
    for i, p in enumerate(files):
        nodes.append({"id": p, "kind": "file", "path": p, "language": "python"})
        k = max(1, int(random.gauss(4, 1.5)))
        line, syms = 1, []
        for j in range(k):
            s = line
            e = line + random.randint(8, 40)
            line = e + random.randint(1, 4)
            name = f"sym_{i}_{j}"
            kind = "class" if (j == 0 and random.random() < 0.2) else "def"
            nodes.append({"id": f"{p}#{name}", "kind": kind, "path": p, "name": name, "start_line": s, "end_line": e})
            syms.append(name)
        sym_index[p] = syms
    edges = set()
    hubs = random.sample(range(N_FILES), 12)
    for h in hubs:
        for imp in random.sample(range(N_FILES), random.randint(15, 40)):
            if imp != h:
                edges.add((files[imp], files[h], "imports"))
    for i in range(N_FILES):
        for _ in range(random.randint(1, 4)):
            t = random.randrange(N_FILES)
            if t != i:
                edges.add((files[i], files[t], "imports"))
        for name in sym_index[files[i]]:
            edges.add((files[i], f"{files[i]}#{name}", "contains"))
    edges = [{"src": s, "dst": d, "kind": k} for (s, d, k) in edges]
    return {"nodes": nodes, "edges": edges, "_hubs": [files[h] for h in hubs]}


class _FakeGH:
    """Minimal GitHub client for the push path.

    The graph itself comes from the monkeypatched build_graph below, but the
    tarball must carry the same document paths: production records its
    input_file_count from this extracted tree and the DB verifies that count
    against the graph's file/config_file paths.
    """
    def __init__(self, document_paths):
        self.document_paths = tuple(document_paths)

    def for_installation(self, installation_id):
        return self

    def repo_default_branch_head(self, repo):
        return BRANCH, "a" * 40

    def repo_current_identity(self, repo):
        return {"id": REPO_ID, "full_name": repo, "owner_id": 777}

    def upsert_check(self, repo, sha, conclusion, title, summary):
        return {"id": 1, "sha": sha, "conclusion": conclusion}

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"# placeholder (the real graph comes from the patched extractor)\n"
            prefix = "repo-" + sha[:7] + "/"
            for path in self.document_paths:
                ti = tarfile.TarInfo(prefix + path)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        return buf.getvalue()


def push_payload(sha: str) -> dict:
    return {"ref": "refs/heads/main", "after": sha,
            "installation": {"id": 4242},
            "repository": {"id": REPO_ID, "full_name": REPO, "default_branch": "main",
                           "owner": {"id": 777, "login": "cold"}}}


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1
    seed_live_installation(
        f"postgresql://veripsa_app@localhost/{DB}",
        f"postgresql://veripsa_migrator@localhost/{DB}",
        777,
        4242,
    )
    checks = []
    g = big_graph()
    hubs = g.pop("_hubs")
    document_paths = sorted({
        node["path"]
        for node in g["nodes"]
        if node.get("kind") in {"file", "config_file"} and node.get("path")
    })
    dsn = f"postgresql://veripsa_app@localhost/{DB}"

    import server as S
    import code_graph_extract as X
    import policy_refresh_queue as PR

    # Feed the LARGE synthetic graph through the LIVE path: _full_ingest calls X.build_graph(root) on the
    # extracted tarball — patch it to return our big graph (content-free, deterministic). Everything else (the
    # connection, the per-repo advisory lock, enter_installation, the body txn + commit, AND the post-commit
    # ANALYZE under test) runs exactly as in production via make_db_processor.
    orig_build = X.build_graph
    X.build_graph = lambda *a, **k: {"nodes": g["nodes"], "edges": g["edges"]}
    try:
        sha = "a" * 40
        proc = S.make_db_processor(dsn)
        t_ingest = time.perf_counter()
        gh = _FakeGH(document_paths)
        proc("push", push_payload(sha), None, gh)
        drained = PR._drain_policy_refreshes(
            PR.PolicyRefreshStore(dsn), gh, dsn, limit=1,
            graph_refresh_strict=S.converge_main_graph_strict)
        assert drained.get("graph_drained") == 1, f"graph convergence failed: {drained!r}"
        ingest_ms = (time.perf_counter() - t_ingest) * 1000.0
    finally:
        X.build_graph = orig_build

    # ── LOCK 1: STATS PRESENT right after the live ingest (the fix's post-commit ANALYZE ran). ──
    # We assert on pg_class.reltuples — the SYNCHRONOUS signal ANALYZE updates immediately (NOT
    # pg_stat_user_tables.last_analyze / n_live_tup, which the stats collector flushes ASYNCHRONOUSLY ~1s
    # later and so would flake right after the event). reltuples is -1 on a NEVER-analyzed table (Postgres'
    # sentinel) and the real positive row-count after an ANALYZE of populated data — so reltuples > 0 on BOTH
    # graph tables proves the post-ingest ANALYZE ran. Without the fix these stay -1 until autovacuum lags in.
    admin = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(
            "SELECT relname, reltuples FROM pg_class "
            "WHERE relnamespace='core'::regnamespace AND relname IN ('code_node','code_edge') ORDER BY relname")
        reltuples = {row[0]: row[1] for row in cur.fetchall()}
        # graph_version is RLS-protected — pin the account the push routed to (owner.id=777 → ACCT-GH-777) to read it.
        cur.execute("SELECT set_config('core.current_account','ACCT-GH-777',false)")
        cur.execute("SELECT node_count, edge_count FROM core.graph_version "
                    "WHERE repo=%s AND branch=%s ORDER BY ingested_at DESC LIMIT 1", (REPO, BRANCH))
        gv = cur.fetchone()
    admin.close()

    rt_node = reltuples.get("code_node")
    rt_edge = reltuples.get("code_edge")
    stats_fresh = (rt_node or 0) > 0 and (rt_edge or 0) > 0
    checks.append((
        f"LOCK1 stats-present: after the LIVE bulk ingest ({gv}), the planner has FRESH row-count stats — "
        f"pg_class.reltuples code_node={rt_node} code_edge={rt_edge} (both > 0); without the post-ingest "
        f"ANALYZE these stay -1 (never-analyzed sentinel) and the planner is BLIND", stats_fresh))

    # ── LOCK 2: the FIRST main_impact_surface on this fresh large graph is BOUNDED (not the ~121s cold hang). ──
    # Seed a handful of in-flight claims on hub files so the brain has real adjacency to traverse (otherwise the
    # call is trivially empty and proves nothing). Done as veripsa_app under the same account the push routed to.
    seed = psycopg2.connect(dsn)
    seed.autocommit = True
    with seed.cursor() as cur:
        cur.execute("SET search_path=core,pg_catalog")
        for i, p in enumerate(hubs[:8]):
            cur.execute("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
                        (f"PR-{2*i}:{p}", p, REPO, BRANCH, "alice"))
            cur.execute("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
                        (f"PR-{2*i+1}:{p}", p, REPO, BRANCH, "bob"))
    seed.close()

    brain = psycopg2.connect(dsn)
    brain.autocommit = True
    first_ms = None
    try:
        with brain.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SET statement_timeout=%s", (int(FIRST_CALL_CEILING_S * 1000),))   # cap so a hang ERRORS, not blocks
            t0 = time.perf_counter()
            cur.execute("SELECT length(core.main_impact_surface(%s,%s)::text)", (REPO, BRANCH))
            n = cur.fetchone()[0]
            first_ms = (time.perf_counter() - t0) * 1000.0
            bounded = True
    except Exception as e:
        first_ms = None
        bounded = False
        n = 0
        err = str(e)[:160]
    finally:
        brain.close()

    if bounded:
        checks.append((
            f"LOCK2 first-call bounded: the FIRST main_impact_surface on the fresh large graph "
            f"({gv} graph) ran in {first_ms:.0f}ms (result {n} bytes) — under the {FIRST_CALL_CEILING_S:.0f}s "
            f"ceiling; the cold-stats regression was ~121000ms", first_ms is not None and first_ms < FIRST_CALL_CEILING_S * 1000))
    else:
        checks.append((
            f"LOCK2 first-call bounded: the FIRST main_impact_surface HUNG past the {FIRST_CALL_CEILING_S:.0f}s "
            f"ceiling (statement_timeout fired) — the cold-stats hang is NOT fixed: {err}", False))

    subprocess.run(["dropdb", DB], capture_output=True, text=True)

    ok = True
    print(f"  (live ingest of {gv} graph through make_db_processor took {ingest_ms:.0f}ms incl. the post-commit ANALYZE)")
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("COLD GRAPH STATS GATE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
