#!/usr/bin/env python3
"""OVER-QUOTA RE-INGEST COLD-STATS gate — the FIRST main_impact_surface for an OVER-QUOTA account whose
self-heal re-ingest takes the EARLY quota-refusal RETURN must STILL be bounded, carried PURELY by the
disk-vs-stats cold-stats arm (NOT the core.graph_bulk_loaded flag, which is never set on this path).

WHY THIS IS A SEPARATE GATE FROM tests/test_same_event_reingest_stats.py (gate 160). That gate proves the
SAME-EVENT self-heal path on a NON-over-quota account: the re-ingest actually WRITES, so
core.ingest_graph_with_authority reaches its TAIL and sets the synchronous session flag
core.graph_bulk_loaded='1' — and main_impact_surface's cold-stats guard fires via the FLAG arm. That test
therefore gives ZERO isolated coverage to the THIRD (disk-vs-stats) arm added in #418, because the flag arm
short-circuits first. This gate exercises the one path where the flag arm is DEAD.

THE GAP #418 CLOSED (the path this gate locks). core.ingest_graph_with_authority sets core.graph_bulk_loaded
at its TAIL, AFTER the _refuse_if_over_quota wall (db/schema/30_gate.sql). So for an account that is ALREADY
over its plan's graph_units line, the live App's PR self-heal re-ingest (ingest.self_heal_main_graph →
ingest_graph_with_authority) takes the EARLY refusal RETURN (returns quota_exceeded, writes nothing) — the
flag is NEVER set. Yet the App STILL computes main_impact_surface against the STORED graph (self_heal returns
healed=False on the refusal and PR analysis proceeds against the stored graph by design). With the graph's
planner stats stale (reltuples not -1, n_mod_since_analyze async-lagged to 0), NEITHER of the other two
cold-stats arms trips → the surface mis-plans core._claim_adjacency (the O(graph) blast-radius scan) into the
O(N^2) nested-loop wedge that pins the single-threaded event worker for the full statement_timeout.

THE #418 FIX (db/schema/80_contention.sql, the THIRD arm): also force the ANALYZE when the relation's ACTUAL
on-disk page count (pg_relation_size, an O(1) stat() — no scan) exceeds what pg_class.relpages claims by a
wide margin (relpages*4+32). A bulk DELETE+INSERT that grew the heap leaves relpages at the prior small/old
value until ANALYZE/autovacuum catches up, with a stale-SMALL reltuples alongside it — the exact mis-plan
trigger — so this synchronous, catalog-only proxy ("the heap is far bigger than the planner believes")
re-ANALYZEs BEFORE the heavy joins. Content-free (catalog sizes only). A WARM, freshly-analyzed table has
relpages == actual pages, so the arm is FALSE and a steady-state webhook pays nothing.

THE LOCK (driven through the EXACT over-quota incident sequence, on ONE connection in ONE body txn):
  1. EARLY-REFUSAL CONFIRMED: with the plan's graph_units line zeroed (so the already-stored account is over
     quota), the re-ingest RETURNs quota_exceeded — proving it took the early-refusal path, NOT a write.
  2. FLAG DEAD: core.graph_bulk_loaded is NOT '1' before the surface call — proving the flag arm cannot help
     here, so whatever keeps the surface fast is the disk-vs-stats arm alone.
  3. SURFACE BOUNDED: with stats forced stale-small AND relpages forced small (the post-reingest reality the
     disk-vs-stats arm detects), the FIRST main_impact_surface in the same txn completes well under a generous
     ceiling — enforced by a SQL statement_timeout so a non-terminating / O(N^2)-blown plan FAILS as a
     QueryCanceled instead of hanging CI. (Without the #418 arm this hits the stale-stats nested-loop blow-up
     and times out — verified by removing the arm.)

Run:  python3 tests/test_overquota_reingest_coldstats.py   (needs local Postgres with the veripsa roles)
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

DB = "veripsa_oqreingest_" + str(os.getpid())   # PER-PID (parallel-safe), like every gate here
REPO = "acme/overquota"
BRANCH = "main"
N_FILES = 2500
N_PR = 60

# Hard per-call ceiling for the surface on the over-quota re-ingest path. The stale-stats nested-loop blow-up
# measured ~7000-11000ms on a real-ish graph (and compounds to the multi-minute queue wedge under a push
# burst); the #418 disk-vs-stats arm runs sub-second. 10s flags the order-of-magnitude blow-up with ample
# headroom over the fix + CI contention — the same ceiling gate 160 uses.
SAME_TXN_CEILING_MS = 10000


def big_graph(seed: int = 11) -> dict:
    """Deterministic, content-free synthetic graph the size of a real-ish repo: ~2500 files + def/class symbols,
    hub files (high fan-in) + random imports + contains/calls edges. Same shape as the cold-stats / perf gates —
    exactly what the brain's O(edges) adjacency chews when the planner stats are stale. Self-contained here (this
    gate does not import the sibling reingest test) so the two gate files share no source line."""
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
    all_syms = [s for syms in sym_index.values() for s in syms]
    for i in range(N_FILES):
        for _ in range(random.randint(1, 4)):
            t = random.randrange(N_FILES)
            if t != i:
                edges.add((files[i], files[t], "imports"))
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
    # This gate drives the over-quota wall through veripsa_app, which routes to ACCT-DEMO — and ACCT-DEMO is on the
    # DEV-ONLY quota-exemption allowlist (core._dev_exempt_account_ids; the PO's dogfood/demo account, so the
    # dogfood loop is never walled). That exemption is correct for the product but would short-circuit THIS gate's
    # whole premise (the account must hit the wall). So opt ACCT-DEMO OUT of the dev exemption for this ephemeral
    # gate DB only — leaving the OTHER default-exempt accounts in place — so the over-quota self-heal path is
    # genuinely exercised. (set_dev_exempt_accounts_with_authority is App-grantable; same gated knob as the line.)
    _ex = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    _ex.autocommit = True
    with _ex.cursor() as cur:
        cur.execute("SET search_path=core,pg_catalog")
        cur.execute("SELECT core.set_dev_exempt_accounts_with_authority(%s)",
                    ("ACCT-GH-42424242,ACCT-GH-43434343",))   # drop ACCT-DEMO so the wall bites for this gate
    _ex.close()
    checks = []
    g = big_graph()
    files, hubs = g.pop("_files"), g.pop("_hubs")
    graph = {"nodes": g["nodes"], "edges": g["edges"]}

    admin_dsn = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
    # 1) Ingest the graph as veripsa_app, establishing the coordinate + the account it routes to (UNDER quota so
    #    the first ingest succeeds and the footprint is stored — graph_version.node_count/edge_count are summed
    #    by the quota check).
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
        print("OVER-QUOTA REINGEST COLD-STATS GATE: FAIL")
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

    # 3) Put the account OVER QUOTA by zeroing the free plan's graph_units line (the gated, App-grantable owner
    #    knob — set_plan_graph_units_limit_with_authority is GRANTed to veripsa_app). The account already stored
    #    a multi-thousand-unit graph above, so with the line at 0 it is now over the graph_units wall → the next
    #    ingest_graph_with_authority takes the EARLY _refuse_if_over_quota RETURN (writes nothing, never reaches
    #    the tail that sets core.graph_bulk_loaded). This is the EXACT state the live over-quota account is in.
    zeroed = None
    z = conn("veripsa_app")
    with z.cursor() as cur:
        cur.execute("SELECT core.set_plan_graph_units_limit_with_authority('free', 0)")
        zeroed = cur.fetchone()[0]
    z.close()

    # 4) Force the planner stats STALE-SMALL with relpages forced to 1 and the cumulative mod counter reset to 0
    #    — the post-re-ingest reality the disk-vs-stats arm is designed to catch: reltuples stale-small, relpages
    #    far below the real on-disk page count, n_mod_since_analyze not yet async-flushed. This makes the prior
    #    two arms (reltuples=-1, n_mod_since_analyze) BOTH false; only the #418 disk-vs-stats arm can trip. (Done
    #    via the superuser; test-only stat surgery — the product path reaches the identical state via a real bulk
    #    DELETE+INSERT that grows the heap while relpages lags.)
    with sup.cursor() as cur:
        cur.execute("ANALYZE core.code_node, core.code_edge, core.claim")
        cur.execute("UPDATE pg_class SET reltuples=10, relpages=1 WHERE relnamespace='core'::regnamespace "
                    "AND relname IN ('code_node','code_edge')")
        cur.execute("DELETE FROM pg_statistic WHERE starelid IN (SELECT oid FROM pg_class "
                    "WHERE relnamespace='core'::regnamespace AND relname IN ('code_node','code_edge'))")
        cur.execute("SELECT pg_stat_reset_single_table_counters(c.oid) FROM pg_class c "
                    "WHERE c.relnamespace='core'::regnamespace AND c.relname IN ('code_node','code_edge')")

    # ── THE LOCK: reproduce the over-quota incident sequence on ONE connection in ONE body txn. The self-heal
    # re-ingest hits the early quota refusal (flag NEVER set), then the App computes the surface against the
    # stored graph. A SQL statement_timeout caps the surface so the pre-fix stale-stats nested-loop blow-up FAILS
    # as a QueryCanceled instead of hanging the gate. ──
    refused = False
    refusal_dim = None
    flag_set_before_surface = None
    surface_ms = None
    bounded = False
    err = ""
    body = conn("veripsa_app", autocommit=False)
    try:
        with body.cursor() as cur:
            cur.execute("SET search_path=core,pg_catalog")
            cur.execute("SET statement_timeout=%s", (SAME_TXN_CEILING_MS,))
            # the self-heal full re-ingest — OVER QUOTA → early _refuse_if_over_quota RETURN, writes nothing,
            # core.graph_bulk_loaded is NEVER set on this path.
            cur.execute("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, BRANCH, "b" * 40))
            res = json.loads(cur.fetchone()[0])
            refused = bool(res.get("quota_exceeded"))
            refusal_dim = res.get("dimension")
            # the flag MUST NOT be set (this is what makes the flag arm dead on this path — proving the surface's
            # speed below comes from the disk-vs-stats arm, not the flag).
            cur.execute("SELECT current_setting('core.graph_bulk_loaded', true)")
            flag_set_before_surface = ((cur.fetchone()[0] or "0") == "1")
            # FIRST surface in the SAME txn — the call that wedged prod for the over-quota account.
            t0 = time.perf_counter()
            cur.execute("SELECT length(core.main_impact_surface(%s,%s)::text)", (REPO, BRANCH))
            n = cur.fetchone()[0]
            surface_ms = (time.perf_counter() - t0) * 1000.0
            bounded = surface_ms < SAME_TXN_CEILING_MS and n > 0
            body.commit()
    except Exception as e:
        err = str(e)[:160]
        try:
            body.rollback()
        except Exception:
            pass
    finally:
        body.close()

    # LOCK 1 — the re-ingest took the EARLY quota-refusal path (so the flag-setting tail was never reached).
    checks.append((
        f"early-refusal CONFIRMED: with the free graph_units line zeroed ({zeroed}), the self-heal re-ingest of "
        f"an over-quota account RETURNed quota_exceeded (dimension={refusal_dim!r}) — it took the early "
        f"_refuse_if_over_quota RETURN and never reached the tail that sets core.graph_bulk_loaded",
        refused and refusal_dim == "graph_units"))

    # LOCK 2 — the flag arm is DEAD on this path (so the surface's speed below is the disk-vs-stats arm alone).
    checks.append((
        f"flag arm DEAD: core.graph_bulk_loaded is NOT set before the surface "
        f"({'SET — unexpected' if flag_set_before_surface else 'unset, as required'}) — so the cold-stats guard "
        f"cannot fire via the flag arm here; only the disk-vs-stats arm can keep the surface bounded",
        flag_set_before_surface is False))

    # LOCK 3 — the surface stays bounded PURELY via the disk-vs-stats arm.
    if bounded:
        checks.append((
            f"surface BOUNDED via disk-vs-stats arm: the FIRST main_impact_surface for the over-quota account "
            f"(stale stats + relpages forced small, flag UNSET) ran in {surface_ms:.0f}ms over {N_PR} in-flight "
            f"PRs on a {len(graph['nodes'])}-node/{len(graph['edges'])}-edge graph — under the "
            f"{SAME_TXN_CEILING_MS}ms ceiling; the pre-#418 stale-stats nested-loop regression times out here "
            f"(verified by removing the arm)", True))
    else:
        checks.append((
            f"surface BOUNDED via disk-vs-stats arm: the FIRST main_impact_surface for the over-quota account "
            f"HUNG past {SAME_TXN_CEILING_MS}ms (statement_timeout fired) — the #418 disk-vs-stats cold-stats arm "
            f"is NOT carrying this path: {err or f'{surface_ms}ms'}", False))

    sup.close()
    subprocess.run(["dropdb", DB], capture_output=True, text=True)

    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("OVER-QUOTA REINGEST COLD-STATS GATE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
