#!/usr/bin/env python3
"""SAME-EVENT RE-INGEST STATS gate — the FIRST main_impact_surface in the SAME transaction as a self-heal
full re-ingest must be BOUNDED, not the stale-stats nested-loop blow-up. Locks the slow-ingest incident fix
(2026-06-21): the recurring 600-900s single-worker wedge that the existing cold-stats guards did NOT catch.

THE REGRESSION THIS GUARDS (the gap the prior #169 fix left open). The live App's PR path
(github-app/webhook_handlers._handle_pull_request_event) SELF-HEALS a stale graph by re-ingesting main
(ingest.self_heal_main_graph → core.ingest_graph_with_authority = a bulk DELETE+INSERT of the WHOLE coordinate
into core.code_node / core.code_edge) and THEN computes core.main_impact_surface in the SAME body transaction.
The cold/stale-stats guard inside main_impact_surface only re-ANALYZEd on the PG never-analyzed sentinel
(pg_class.reltuples = -1) OR a FLUSHED n_mod_since_analyze. After a re-ingest of an ALREADY-analyzed
coordinate, NEITHER holds:
  • reltuples is NOT -1 — a DELETE+INSERT reuses the heap, so pg_class keeps the PRIOR analyze's (stale,
    possibly tiny/old) reltuples; it never reverts to the -1 sentinel.
  • n_mod_since_analyze is still 0 — Postgres flushes the cumulative stats collector ASYNCHRONOUSLY (~1s), so
    read inside the SAME txn that just did the bulk write it has not yet reflected the churn.
So the guard was SKIPPED and the surface planned core._claim_adjacency (the O(graph) blast-radius scan) +
the heavy per-change correlated joins on STALE cardinalities → the planner picked NESTED LOOPS over hash
joins → an O(N²) blow-up. MEASURED on this repo's real ~13k-edge graph at 60 in-flight PRs: ~7000ms with
stale stats vs ~0.3s once correctly analyzed (~23x); under a rapid push burst (the surface recomputed per PR
across many in-flight PRs) that compounds into the multi-minute statement-timeout wedge that blocks the whole
single-threaded event queue (the symptom: "the App isn't watching core").

THE FIX (root, behaviour-preserving — db/schema/80_contention.sql): the surface's cold-stats guard ALSO fires
when the SYNCHRONOUS session flag core.graph_bulk_loaded='1' is set. core.ingest_graph_with_authority sets that
GUC inside the body txn (synchronous — readable on the same session/txn immediately, unlike the async stats
collector), so it is the precise, lag-free "the graph tables were just rewritten — their stats are stale
regardless of pg_class" signal. The guard ANALYZEs the graph tables BEFORE the heavy joins and clears the flag
(one ANALYZE per bulk-load). Content-free (statistics only); a WARM steady-state webhook still pays nothing.

THE LOCK (driven through the EXACT incident sequence — re-ingest then surface in ONE txn, as the App does):
  1. SAME-TXN BOUNDED: with the planner stats forced STALE (the post-re-ingest reality) and the bulk-loaded
     flag set, the FIRST main_impact_surface in the same txn completes well under a generous ceiling — enforced
     by a SQL statement_timeout so a non-terminating / O(N²)-blown plan FAILS the gate (QueryCanceled) instead
     of hanging CI. (Without the fix this hits the stale-stats nested-loop blow-up and times out.)
  2. FLAG CLEARED: after that first surface the synchronous flag is cleared (so a 2nd in-session call does not
     redundantly re-ANALYZE), and the second call stays fast.

Run:  python3 tests/test_same_event_reingest_stats.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

DB = "veripsa_sameevent_" + str(os.getpid())   # PER-PID (parallel-safe), like every gate here
REPO = "acme/reingest"
BRANCH = "main"
N_FILES = 2500
N_PR = 60

# Hard per-call ceiling for the FIRST surface in the same txn as the re-ingest. The stale-stats nested-loop
# blow-up measured ~7000ms at 60 PRs on a ~13k-edge graph (and compounds to minutes under a burst); the fix
# runs sub-second. 10s flags the order-of-magnitude blow-up with ample headroom over the fix + CI contention.
SAME_TXN_CEILING_MS = 10000


def big_graph(seed: int = 11) -> dict:
    """Deterministic, content-free synthetic graph the size of a real-ish repo: ~2500 files + def/class symbols,
    hub files (high fan-in) + random imports + contains edges. Same shape (and generator) as
    tests/test_cold_graph_stats.py / test_perf_budget.py — exactly what the brain's O(edges) adjacency chews."""
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
    # all symbol names, to draw CALL targets from (calls resolve by symbol NAME → the defs_ok CTE rescan that
    # mis-plans to a nested loop on stale stats; the dominant edge kind in a real code graph).
    all_syms = [s for syms in sym_index.values() for s in syms]
    for i in range(N_FILES):
        for _ in range(random.randint(1, 4)):
            t = random.randrange(N_FILES)
            if t != i:
                edges.add((files[i], files[t], "imports"))
        # CALL edges: this file calls ~3x as many symbols (by bare name) as it imports — matching a real graph's
        # calls-dominated shape (the source of the O(N²) call-resolution nested loop the fix prevents).
        for _ in range(random.randint(2, 5)):
            edges.add((files[i], random.choice(all_syms), "calls"))
        for name in sym_index[files[i]]:
            edges.add((files[i], f"{files[i]}#{name}", "contains"))
    edges = [{"src": s, "dst": d, "kind": k} for (s, d, k) in edges]
    return {"nodes": nodes, "edges": edges, "_files": files, "_hubs": [files[h] for h in hubs]}


def conn(role="veripsa_app", autocommit=True):
    c = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    c.autocommit = autocommit
    return c


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1
    checks = []
    g = big_graph()
    files, hubs = g.pop("_files"), g.pop("_hubs")
    graph = {"nodes": g["nodes"], "edges": g["edges"]}

    # ── Identity: drive ingest + claims + the brain all as veripsa_app (the hosted-App identity — it is a member
    # of veripsa_writer so it may ingest, and has the act-for-author grant for the per-PR claims). This mirrors
    # dogfood_nscale.py's single-role driver and the real App, where the same connection ingests then predicts.
    # The superuser connection is for RLS-bypassing inspection + the test-only stale-stat surgery below. ──
    admin_dsn = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
    # 1) Ingest the graph as veripsa_app, establishing the coordinate + the account it routes to.
    w = conn("veripsa_app")
    with w.cursor() as cur:
        cur.execute("SET search_path=core,pg_catalog")
        cur.execute("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, BRANCH, "a" * 40))
    w.close()

    sup = psycopg2.connect(admin_dsn, dbname=DB)
    sup.autocommit = True
    with sup.cursor() as cur:
        cur.execute("SELECT account_id FROM core.graph_version WHERE repo=%s AND branch=%s ORDER BY ingested_at DESC LIMIT 1", (REPO, BRANCH))
        row = cur.fetchone()
        acct = row[0] if row else None
    if not acct:
        print("  [FAIL] could not resolve the ingest account")
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
        print("SAME-EVENT REINGEST STATS GATE: FAIL")
        return 1

    # 2) Seed N in-flight PR claims on hub-heavy files so the adjacency the brain traverses is non-trivial (an
    #    empty surface would prove nothing). One file per PR, a handful of authors.
    seed = conn("veripsa_app")
    with seed.cursor() as cur:
        cur.execute("SET search_path=core,pg_catalog")
        seed_files = (hubs + files)[:N_PR]
        for i, p in enumerate(seed_files):
            cur.execute("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
                        (f"PR-{i}:{p}", p, REPO, BRANCH, f"author{i % 7}"))
    seed.close()

    # 3) Force the planner stats STALE-SMALL with the cumulative mod counter reset to 0 — EXACTLY the state the
    #    graph tables are in inside the body txn right after a self-heal re-ingest of an already-analyzed
    #    coordinate (reltuples kept the prior/old picture, n_mod_since_analyze not yet async-flushed). This is
    #    what makes the prior guard's two arms both FALSE. (We do this via the superuser; it is test-only stat
    #    surgery, not a product path — the product path reaches the identical state via the async-flush reality.)
    with sup.cursor() as cur:
        cur.execute("ANALYZE core.code_node, core.code_edge, core.claim")
        cur.execute("UPDATE pg_class SET reltuples=10, relpages=1 WHERE relnamespace='core'::regnamespace "
                    "AND relname IN ('code_node','code_edge')")
        cur.execute("DELETE FROM pg_statistic WHERE starelid IN (SELECT oid FROM pg_class "
                    "WHERE relnamespace='core'::regnamespace AND relname IN ('code_node','code_edge'))")
        cur.execute("SELECT pg_stat_reset_single_table_counters(c.oid) FROM pg_class c "
                    "WHERE c.relnamespace='core'::regnamespace AND c.relname IN ('code_node','code_edge')")

    # ── LOCK 1: the FIRST main_impact_surface in the SAME txn as a re-ingest is BOUNDED. We reproduce the exact
    # incident sequence on ONE connection in ONE body txn: ingest_graph_with_authority (the self-heal bulk
    # re-load — it sets core.graph_bulk_loaded='1' synchronously) immediately followed by main_impact_surface.
    # A SQL statement_timeout caps the surface so the pre-fix stale-stats nested-loop blow-up FAILS as a
    # QueryCanceled instead of hanging the gate. ──
    first_ms = second_ms = None
    flag_cleared = False
    bounded = False
    err = ""
    body = conn("veripsa_app", autocommit=False)
    try:
        with body.cursor() as cur:
            cur.execute("SET search_path=core,pg_catalog")
            cur.execute("SET statement_timeout=%s", (SAME_TXN_CEILING_MS,))
            # the self-heal full re-ingest (bulk DELETE+INSERT of the whole coordinate) → sets graph_bulk_loaded
            cur.execute("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, BRANCH, "b" * 40))
            # FIRST surface in the SAME txn — the call that wedged prod
            t0 = time.perf_counter()
            cur.execute("SELECT length(core.main_impact_surface(%s,%s)::text)", (REPO, BRANCH))
            n = cur.fetchone()[0]
            first_ms = (time.perf_counter() - t0) * 1000.0
            bounded = first_ms < SAME_TXN_CEILING_MS
            # the synchronous flag must be cleared after the first call (so a 2nd surface doesn't re-ANALYZE)
            cur.execute("SELECT current_setting('core.graph_bulk_loaded', true)")
            flag_cleared = ((cur.fetchone()[0] or "0") != "1")
            # SECOND surface — flag cleared, stats now fresh → stays fast (no redundant re-ANALYZE)
            t1 = time.perf_counter()
            cur.execute("SELECT length(core.main_impact_surface(%s,%s)::text)", (REPO, BRANCH))
            second_ms = (time.perf_counter() - t1) * 1000.0
            body.commit()
    except Exception as e:
        err = str(e)[:160]
        try:
            body.rollback()
        except Exception:
            pass
    finally:
        body.close()

    if bounded:
        checks.append((
            f"LOCK1 same-txn bounded: re-ingest THEN main_impact_surface in ONE txn (stale stats + "
            f"graph_bulk_loaded set) ran in {first_ms:.0f}ms over {N_PR} in-flight PRs on a {len(graph['nodes'])}-"
            f"node/{len(graph['edges'])}-edge graph — under the {SAME_TXN_CEILING_MS}ms ceiling; the stale-stats "
            f"nested-loop regression was ~7000ms+ and compounds to the 600s queue wedge", True))
    else:
        checks.append((
            f"LOCK1 same-txn bounded: the FIRST main_impact_surface in the same txn as the re-ingest HUNG past "
            f"{SAME_TXN_CEILING_MS}ms (statement_timeout fired) — the same-event cold-stats wedge is NOT fixed: "
            f"{err or f'{first_ms}ms'}", False))

    checks.append((
        f"LOCK2 flag cleared + 2nd call fast: after the first surface, core.graph_bulk_loaded is cleared "
        f"({'yes' if flag_cleared else 'NO'}) and the 2nd same-txn surface ran in "
        f"{(second_ms if second_ms is not None else -1):.0f}ms (no redundant re-ANALYZE)",
        flag_cleared and (second_ms is not None and second_ms < SAME_TXN_CEILING_MS)))

    sup.close()
    subprocess.run(["dropdb", DB], capture_output=True, text=True)

    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("SAME-EVENT REINGEST STATS GATE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
