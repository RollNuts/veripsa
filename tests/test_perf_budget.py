#!/usr/bin/env python3
"""PERF BUDGET gate — the GitHub-App BRAIN path (PR open → main_impact_surface) must stay BOUNDED + sub-second
as the in-flight PR count grows. This locks the fix from the perf audit (2026-06-18).

THE REGRESSION THIS GUARDS (audit:perf): main_impact_surface's `shared_foundation` field is a CORRELATED
per-change subquery that joins each in-flight change against the `hotspot` CTE. `hotspot` is the FAN-IN
aggregation — a full scan of every `imports` edge with count(DISTINCT src). If that CTE is INLINED (not
materialized), Postgres re-runs the WHOLE fan-in scan ONCE PER in-flight change = O(edges × changes). On a
3000-file / 8k-import graph that made the brain path ~4.2s at 80 PRs and ~11.7s at 200 PRs — past any webhook
timeout. Forcing a SINGLE evaluation (`hotspot AS MATERIALIZED`) makes the cost O(graph), not O(graph × PRs):
measured ~0.9s at 200 PRs (a 13× speedup), semantics byte-identical.

TWO LOCKS (both must hold):
  1. STRUCTURAL (deterministic, machine-independent): the engine source keeps `hotspot AS MATERIALIZED`.
     A future edit that drops the keyword re-opens the O(edges × changes) blow-up — fail loudly, not silently
     slow. This is the real budget anchor (no wall-clock flake).
  2. SCALING (behavioural): on a realistic seeded graph, going from 10 → 200 in-flight PRs must NOT blow up.
     We assert the per-call time at 200 PRs stays under a GENEROUS absolute ceiling AND that the 200-PR time
     is not a large multiple of the 10-PR time (the inlined regression is ~LINEAR in PRs; the fix is ~flat).
     Ceilings are deliberately loose (CI machines vary) — they catch an ORDER-OF-MAGNITUDE regression, the
     only kind that matters here, without flaking on a slow runner.

Run:  python3 tests/test_perf_budget.py
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PER-PID (parallel-safe): the gate bootstraps + drops this DB; a fixed name lets concurrent runs drop each
# other's DB mid-run. Mirrors db/smoke.sh (veripsa_smoke_$$) and every other test here.
DB = "veripsa_perfbudget_" + str(os.getpid())
ACCT = "ACCT-DEMO"
REPO = "perf/bigrepo"
BRANCH = "main"
ENGINE_SQL = os.path.join(ROOT, "db", "schema", "80_contention.sql")
# _dampened_adjacency (the AUDIT-2026-06-19 brain-path culprit) lives in the social module.
DAMPEN_SQL = os.path.join(ROOT, "db", "schema", "70_social.sql")
TIMEOUT_RUNNER = os.path.join(ROOT, "scripts", "run_with_timeout.py")

# absolute ceiling for one brain-path call at 200 in-flight PRs. The fix runs this in ~0.9s; the regression
# ran it in ~11.7s. 6s is generous headroom over the fix yet an order of magnitude under the regression —
# it flags the blow-up, not a slow runner.
CEILING_200_S = 6.0
# the 200-PR call must not be more than this multiple of the 10-PR call. Fix ratio ~2x (flat-ish, O(graph));
# the inlined regression is ~linear in PRs → ~20x. 6x splits them with wide margin.
MAX_SCALE_RATIO = 6.0

N_FILES = 3000
SYMS_PER = 4
CONNECT_TIMEOUT_S = 5
LOCK_TIMEOUT_MS = 5_000
SEED_TIMEOUT_MS = 60_000
QUERY_TIMEOUT_MS = 12_000
BOOTSTRAP_TIMEOUT_S = 90
CLEANUP_TIMEOUT_S = 15


def _set_session_timeout(cur, milliseconds: int) -> None:
    cur.execute(
        "SELECT set_config('statement_timeout', %s, false)",
        (f"{milliseconds}ms",),
    )
    cur.execute(
        "SELECT set_config('lock_timeout', %s, false)",
        (f"{LOCK_TIMEOUT_MS}ms",),
    )


def _drop_database() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, TIMEOUT_RUNNER, str(CLEANUP_TIMEOUT_S), "--", "dropdb", DB],
            capture_output=True,
            text=True,
            timeout=CLEANUP_TIMEOUT_S + 10,
        )
    except subprocess.TimeoutExpired:
        print(f"cleanup exceeded {CLEANUP_TIMEOUT_S}s")
        return False
    if result.returncode != 0:
        print(f"cleanup failed: {result.stderr[-400:]}")
        return False
    return True


def gen_seed(n_prs: int, repo: str, seed: int = 42, n_files: int = N_FILES, with_calls: bool = False) -> str:
    """Deterministic, content-free synthetic graph + in-flight set as one SQL batch (triggers disabled ONLY to
    bulk-load — the SURFACE under test only READS). Hub files (high fan-in) + landed churn + PRs ON those hubs,
    so `shared_foundation` is NON-empty and the correlated hotspot path is actually exercised.

    The dedicated tests/test_dampened_res_scale.py owns the large calls/resource graph; this gate intentionally
    keeps only the bounded 3k-file PR-count scaling shape."""
    random.seed(seed)
    rk = "".join(ch for ch in repo if ch.isalnum())  # repo-unique key for globally-unique event ids
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
        "ALTER TABLE core.event DISABLE TRIGGER trg_governed_event;",
    ]
    # nodes: file + def/class symbols with spans
    nodes = []
    sym_index = {}
    for i, p in enumerate(files):
        nodes.append(f"('{ACCT}','{repo}','{BRANCH}','{p}','file','{p}',NULL,'python',NULL,NULL)")
        k = max(1, int(random.gauss(SYMS_PER, 1.5)))
        line = 1
        syms = []
        for j in range(k):
            s = line
            e = line + random.randint(8, 40)
            line = e + random.randint(1, 4)
            name = f"sym_{i}_{j}"
            kind = "class" if (j == 0 and random.random() < 0.2) else "def"
            nodes.append(f"('{ACCT}','{repo}','{BRANCH}','{p}#{name}','{kind}','{p}','{name}','python',{s},{e})")
            syms.append((name, s, e))
        sym_index[p] = syms
    if with_calls:  # a HOT shared table (res_hub) touched by many files — drives _dampened_adjacency's res_h axis.
        nodes.append(f"('{ACCT}','{repo}','{BRANCH}','table::hot','table','schema.sql','hot','sql',NULL,NULL)")
    out.append("INSERT INTO core.code_node (account_id,repo,branch,node_id,node_kind,path,name,language,start_line,end_line) VALUES")
    out.append(",\n".join(nodes) + ";")
    # edges: hubs imported by many + normal imports + contains
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
        for (name, _s, _e) in sym_index[files[i]]:
            edges.add((files[i], f"{files[i]}#{name}", "contains"))
    if with_calls:
        # CALLS edges: a `calls` edge's dst is the bare symbol NAME (the extractor's shape — calls_h joins
        # `defs_ok d ON d.nm=ce.dst`). Each file calls a handful of symbols defined in OTHER files, MANY of them
        # defined in HUB files (so calls_h's `definer file IS a hub` axis fires). On the UN-restricted regression
        # _dampened_adjacency's calls_h scans EVERY one of these × the (re-CTE-scanned, un-materialized) defs_ok /
        # def_n / correlated import-confirmation EXISTS — the audited 13.5s@6k / 46.4s@12k whole-graph blow-up; the
        # fix restricts the driving rows to the in-flight slice + folds the classification CTEs once. Density scales
        # with the graph so the SIZE sweep grows this hot input proportionally (the superlinear regression shape).
        hubset = [files[h] for h in hubs]
        for i in range(n_files):
            for _ in range(random.randint(6, 12)):
                # bias toward calling a symbol DEFINED IN A HUB (so the dropped calls-into-hub axis is exercised).
                target = random.choice(hubset) if random.random() < 0.5 else files[random.randrange(n_files)]
                tsyms = sym_index.get(target, [])
                if target != files[i] and tsyms:
                    (nm, _s, _e) = random.choice(tsyms)
                    edges.add((files[i], nm, "calls"))   # dst = bare symbol NAME (extractor shape)
        # QUERIES/ALTERS to the HOT table from many files (a res_hub) + a few normal tables (queries/alters axis).
        for i in random.sample(range(n_files), min(n_files, max(60, n_files // 30))):
            edges.add((files[i], "table::hot", "queries"))
        for i in random.sample(range(n_files), min(n_files, 40)):
            edges.add((files[i], "table::hot", "alters"))
    out.append("INSERT INTO core.code_edge (account_id,repo,branch,src,dst,edge_kind) VALUES")
    out.append(",\n".join(f"('{ACCT}','{repo}','{BRANCH}','{s}','{d}','{k}')" for (s, d, k) in edges) + ";")
    out.append(
        f"INSERT INTO core.graph_version (account_id,repo,branch,node_count,edge_count) "
        f"VALUES ('{ACCT}','{repo}','{BRANCH}',{len(nodes)},{len(edges)}) "
        f"ON CONFLICT (account_id,repo,branch) DO UPDATE SET node_count=EXCLUDED.node_count;")
    # landed churn so the hub files qualify as hotspots (fan_in>=5 AND churn>=3)
    ev = []
    n = 0
    for h in hubs:
        for _ in range(random.randint(3, 8)):
            n += 1
            ev.append(f"('{ACCT}','EV-{rk}-{n}','landed','AG-A','{repo}','{BRANCH}','{files[h]}', now() - (random()*20||' days')::interval)")
    out.append("INSERT INTO core.event (account_id,event_id,kind,agent_id,repo,branch,path,occurred_at) VALUES")
    out.append(",\n".join(ev) + ";")
    # claims: n_prs PRs. Put the FIRST claims ON hub files (active+waiting) so shared_foundation is non-empty,
    # then spread the rest across random files (active+waiting pairs = realistic contention). When with_calls, the
    # active hub claims also sit at the head of import/call/resource edges, so the dampened inflight×inflight axes fire.
    claim_rows = []
    pr = 0
    # hub files FIRST (so shared_foundation is non-empty), then the rest — DEDUPED so no path gets two active
    # claims (claim_one_active is a per-path unique index).
    seen = set()
    targets = []
    for p in [files[h] for h in hubs] + [files[i] for i in random.sample(range(n_files), n_files)]:
        if p not in seen:
            seen.add(p)
            targets.append(p)
    for p in targets:
        if pr >= n_prs:
            break
        a, b = ("AG-A", "AG-B") if pr % 2 == 0 else ("AG-B", "AG-A")
        ch_h, ch_w = f"PR-{pr}", f"PR-{pr+1000}"
        pr += 2
        claim_rows.append(f"('{ACCT}','{repo}','{BRANCH}','{ch_h}:{p}','{a}','{ch_h}','{p}','active',NULL)")
        claim_rows.append(f"('{ACCT}','{repo}','{BRANCH}','{ch_w}:{p}','{b}','{ch_w}','{p}','waiting',NULL)")
    out.append("INSERT INTO core.claim (account_id,repo,branch,claim_id,agent_id,change_id,target_path,claim_state,touched_ranges) VALUES")
    out.append(",\n".join(claim_rows) + ";")
    for t in ("code_node", "code_edge", "graph_version", "claim", "event"):
        out.append(f"ALTER TABLE core.{t} ENABLE TRIGGER trg_governed_{t};")
    out.append("COMMIT;")
    out.append("ANALYZE core.code_node; ANALYZE core.code_edge; ANALYZE core.claim; ANALYZE core.event;")
    return "\n".join(out)


def main() -> int:
    checks = []

    # ── LOCK 1: STRUCTURAL (deterministic) — the engine keeps the materialization that bounds the cost. ──
    with open(ENGINE_SQL, encoding="utf-8") as fh:
        src = fh.read()
    has_mat = "hotspot AS MATERIALIZED" in src
    checks.append((
        "LOCK1 structural: main_impact_surface keeps `hotspot AS MATERIALIZED` (else the fan-in scan re-runs "
        "per in-flight change = O(edges×changes), the audited 4.2s→11.7s blow-up)", has_mat))

    # ── LOCK 3: STRUCTURAL (deterministic) — main_impact_surface computes contention components INLINE off its
    # already-materialized `adj` CTE, so the WHOLE-GRAPH adjacency (core._claim_adjacency, O(edges)) is evaluated
    # ONCE per call. The audited regression (audit:scale 2026-06-18): the surface used to call
    # core._inflight_components, which RE-RAN _claim_adjacency internally with the IDENTICAL (account,repo,branch)
    # args → the surface paid the whole-graph scan TWICE. _claim_adjacency self-time on a real 3.4k-file / 110k-edge
    # Django graph is ~0.8–0.9s, so the duplicate cost ~1.1s of a 2.48s call at only 10 in-flight PRs (its cost is
    # GRAPH size, not PR count — invisible to the synthetic perf seed but dominant on a real monorepo, and the cold
    # first call hung for minutes). Re-introducing the helper call re-opens the 2× scan — fail loudly here. The
    # anchor: the surface's body must NOT reference core._inflight_components (the inline `comp_cc`/`comps` replaced
    # it). We scope to the main_impact_surface definition so split_candidates / other functions are unaffected.
    mis_start = src.find("FUNCTION core.main_impact_surface")
    mis_end = src.find("ALTER FUNCTION core.main_impact_surface", mis_start)
    mis_body = src[mis_start:mis_end] if (mis_start >= 0 and mis_end > mis_start) else ""
    # Match an actual CALL — `core._inflight_components(` — not a mention in a comment (the inline replacement's
    # comment legitimately names the helper it is identical to). A regression re-adds the call; that has the paren.
    no_double_adj = bool(mis_body) and not re.search(r"\bcore\._inflight_components\s*\(", mis_body)
    checks.append((
        "LOCK3 structural: main_impact_surface does NOT call core._inflight_components (it computes the contention "
        "components inline off the shared `adj` CTE, so core._claim_adjacency — the O(edges) whole-graph scan — runs "
        "ONCE per call, not twice; the audited audit:scale 2.48s→1.33s halving on a real Django-class graph)", no_double_adj))

    # ── LOCK 4: STRUCTURAL (deterministic) — core._dampened_adjacency (the unknown-first hub-dampening guard,
    # called ONCE per main_impact_surface) keeps the two disciplines that bound it. The audit (2026-06-19) clocked
    # the un-restricted form at 13,256ms on a 6k-file graph (46,400ms at 12k) returning 0 rows: imp/calls_h/res_h
    # were computed over the ENTIRE graph (every `calls` edge × def node, every resource edge) and only intersected
    # with the in-flight `active` set at the very END — while its fast sibling core._claim_adjacency restricts to
    # `edited` (its active set) BEFORE the heavy joins and MATERIALIZEs its hub CTEs (363ms on the same graph).
    # The fix mirrors that. Two anchors (both must hold, scoped to the _dampened_adjacency body only):
    #   (4a) PRE-RESTRICTION: the imp/calls_h/res_h axes restrict their DRIVING edges to the active in-flight path
    #        set (`active_path`) before the joins. We require `active_path` to be defined AND referenced by each of
    #        the three axes (so a future edit that drops the early intersection — re-opening the whole-graph scan —
    #        fails loudly). Semantics-preserving: `paired` already discards non-active endpoints, so this only
    #        narrows the rows scanned, never the result (proven byte-identical by tests/test_dampened_identity.py).
    #   (4b) MATERIALIZED: the hub/def classification CTEs are folded once (re-scanned per reference otherwise).
    with open(DAMPEN_SQL, encoding="utf-8") as fh:
        dsrc = fh.read()
    d_start = dsrc.find("FUNCTION core._dampened_adjacency")
    d_end = dsrc.find("ALTER FUNCTION core._dampened_adjacency", d_start)
    d_body = dsrc[d_start:d_end] if (d_start >= 0 and d_end > d_start) else ""
    has_active_path = "active_path AS MATERIALIZED" in d_body
    # each heavy axis must filter its driving rows on the active path set BEFORE the joins. We count the references
    # to `active_path` inside the body: the definition + at least one filter in EACH of imp / calls_h / res_h.
    n_active_path_refs = len(re.findall(r"\bactive_path\b", d_body))
    pre_restricted = bool(d_body) and has_active_path and n_active_path_refs >= 4  # 1 def + >=1 per the 3 axes
    checks.append((
        f"LOCK4a structural: core._dampened_adjacency pre-restricts imp/calls_h/res_h to the active in-flight path "
        f"set (`active_path AS MATERIALIZED`, referenced {n_active_path_refs}x) BEFORE the heavy joins — the "
        f"semantics-preserving fix that turns the audited whole-graph 13.5s/46.4s scan into O(in-flight subgraph)",
        pre_restricted))
    # the hub/res/def classification CTEs must be MATERIALIZED (folded once, not re-scanned per `IN (...)` probe).
    has_hub_mat = "hub_files AS MATERIALIZED" in d_body and "res_hubs AS MATERIALIZED" in d_body
    has_def_mat = "def_n AS MATERIALIZED" in d_body and "defs_ok AS MATERIALIZED" in d_body
    checks.append((
        "LOCK4b structural: core._dampened_adjacency MATERIALIZEs its hub/res/def CTEs (hub_files, res_hubs, def_n, "
        "defs_ok) — the fan-in + ambiguity aggregates fold once instead of re-scanning per reference (mirrors "
        "_claim_adjacency's hub_files/res_hubs and 80_contention.sql's hotspot)", has_hub_mat and has_def_mat))

    # ── LOCK 5: STRUCTURAL (deterministic) — core._claim_adjacency (THE A→B blast-radius engine, called once per
    # main_impact_surface — the SYNCHRONOUS check-render path) MATERIALIZEs its def/class classification CTEs.
    # The audit (2026-06-19, audit:scale r2) clocked the un-materialized form at 8.3s on a 20k-node / 200-in-flight
    # graph: def_n/defs_ok are GLOBAL same-name ambiguity counts re-referenced many times (edited_def_names, out_adj's
    # defs_ok+def_n joins, in_adj, the import-confirmation NOT EXISTS); un-materialized, Postgres re-evaluates the
    # whole def/class scan per reference. The exact re-CTE-scan LOCK4b already pins for the SIBLING _dampened_adjacency
    # — but _claim_adjacency was the BLIND SPOT (this gate timed _dampened in isolation, treated _claim as "unrelated
    # O(graph)"). Folding once is byte-identical (global counts, not restricted to `edited`). Scoped to the
    # _claim_adjacency body so a future edit dropping the fold fails loudly here, not silently in production latency.
    c_start = dsrc.find("FUNCTION core._claim_adjacency")
    c_end = dsrc.find("ALTER FUNCTION core._claim_adjacency", c_start)
    c_body = dsrc[c_start:c_end] if (c_start >= 0 and c_end > c_start) else ""
    claim_def_mat = bool(c_body) and "defs AS MATERIALIZED" in c_body \
        and "def_n AS MATERIALIZED" in c_body and "defs_ok AS MATERIALIZED" in c_body
    checks.append((
        "LOCK5 structural: core._claim_adjacency MATERIALIZEs its def/class CTEs (defs, def_n, defs_ok) — the global "
        "same-name ambiguity aggregates fold ONCE instead of re-scanning per reference (the audited audit:scale r2 "
        "8.3s→sub-second on a 20k-node/200-in-flight graph; mirrors LOCK4b's identical pin on the _dampened sibling)",
        claim_def_mat))

    # ── LOCK 2: SCALING (behavioural) — bounded as PRs grow, on a realistic seeded graph. ──
    try:
        r = subprocess.run(
            [sys.executable, TIMEOUT_RUNNER, str(BOOTSTRAP_TIMEOUT_S), "--", "bash", "db/bootstrap_local.sh", DB],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=BOOTSTRAP_TIMEOUT_S + 10,
        )
    except subprocess.TimeoutExpired:
        print(f"bootstrap exceeded {BOOTSTRAP_TIMEOUT_S}s")
        _drop_database()
        return 1
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        _drop_database()
        return 1
    try:
        # two INDEPENDENT repo coordinates (COEXISTENCE: ingesting one never touches the other) — so we measure
        # 10-PR and 200-PR loads without deleting between them (event is append-only; no wipe needed).
        REPO10, REPO200 = "perf/repo10", "perf/repo200"

        def seed_repo(n_prs, repo, *, seed=42):
            conn = psycopg2.connect(
                f"postgresql://veripsa_migrator@localhost/{DB}",
                connect_timeout=CONNECT_TIMEOUT_S,
            )
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("SET search_path=core,pg_catalog")
                    _set_session_timeout(cur, SEED_TIMEOUT_MS)
                    cur.execute(gen_seed(n_prs, repo, seed=seed))
            finally:
                conn.close()

        def time_brain(repo, repeats=4):
            conn = psycopg2.connect(
                f"postgresql://veripsa_demo_steward@localhost/{DB}",
                connect_timeout=CONNECT_TIMEOUT_S,
            )
            best = None
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("SET search_path=core")
                    _set_session_timeout(cur, QUERY_TIMEOUT_MS)
                    cur.execute("SELECT length(core.main_impact_surface(%s,%s)::text)", (repo, BRANCH))  # warm
                    for _ in range(repeats):
                        t0 = time.perf_counter()
                        cur.execute("SELECT core.main_impact_surface(%s,%s)", (repo, BRANCH))
                        cur.fetchone()
                        best = min(best, time.perf_counter() - t0) if best else (time.perf_counter() - t0)
            finally:
                conn.close()
            return best

        def shared_foundation_nonempty(repo):
            conn = psycopg2.connect(
                f"postgresql://veripsa_demo_steward@localhost/{DB}",
                connect_timeout=CONNECT_TIMEOUT_S,
            )
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("SET search_path=core")
                    _set_session_timeout(cur, QUERY_TIMEOUT_MS)
                    cur.execute(
                        "SELECT count(*) FROM (SELECT jsonb_array_elements(core.main_impact_surface(%s,%s)->'changes') c) x "
                        "WHERE jsonb_array_length(x.c->'shared_foundation') > 0", (repo, BRANCH))
                    return cur.fetchone()[0]
            finally:
                conn.close()

        seed_repo(10, REPO10)
        t10 = time_brain(REPO10)
        nonempty = shared_foundation_nonempty(REPO10)
        seed_repo(200, REPO200)
        t200 = time_brain(REPO200)

        checks.append((
            f"the correlated hotspot path is actually exercised (shared_foundation non-empty on {nonempty} change(s))",
            nonempty >= 1))
        checks.append((
            f"LOCK2a bounded: brain path at 200 in-flight PRs is under {CEILING_200_S:.0f}s "
            f"(measured {t200*1000:.0f}ms; the audited regression was ~11700ms)", t200 < CEILING_200_S))
        ratio = (t200 / t10) if t10 else 0.0
        checks.append((
            f"LOCK2b scaling: 200-PR time is < {MAX_SCALE_RATIO:.0f}x the 10-PR time "
            f"(measured {ratio:.1f}x: 10PR={t10*1000:.0f}ms 200PR={t200*1000:.0f}ms; the inlined "
            f"regression grows ~linearly in PRs)", ratio < MAX_SCALE_RATIO))

        # ── LOCK 3 BEHAVIOURAL (audit:scale) — on a REAL surface call, the O(edges) whole-graph adjacency
        # (core._claim_adjacency) is evaluated AT MOST ONCE. The structural LOCK3 above catches the source-level
        # regression (the surface calling _inflight_components again); this catches it BEHAVIOURALLY via Postgres'
        # own per-function call counter, so even a rename/alias that re-introduces a second whole-graph scan fails.
        # Enable track_functions at the DB level, reset stats, run EXACTLY ONE surface call, read the call count.
        # track_functions is superuser-only → set it via the same admin DSN bootstrap uses (default local superuser);
        # if no superuser is reachable (a locked-down CI), the behavioural arm is skipped and LOCK3 STRUCTURAL — the
        # deterministic source anchor above — still binds. Never a false PASS: skip is explicit, not a silent green.
        adj_calls = None
        try:
            admin_dsn = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
            admin = psycopg2.connect(admin_dsn, connect_timeout=CONNECT_TIMEOUT_S)
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute(f'ALTER DATABASE "{DB}" SET track_functions=%s', ("all",))
            # reset function stats via the admin DSN (pg_stat_reset is privileged) — must target THIS db.
            admin2 = psycopg2.connect(
                admin_dsn.rsplit("/", 1)[0] + "/" + DB,
                connect_timeout=CONNECT_TIMEOUT_S,
            )
            admin2.autocommit = True
            with admin2.cursor() as cur:
                cur.execute("SELECT pg_stat_reset()")
            admin2.close()
            # fresh connection picks up the DB-level GUC; run EXACTLY ONE isolated surface call as the steward.
            run = psycopg2.connect(
                f"postgresql://veripsa_demo_steward@localhost/{DB}",
                connect_timeout=CONNECT_TIMEOUT_S,
            )
            with run, run.cursor() as cur:
                cur.execute("SET search_path=core,pg_catalog")
                _set_session_timeout(cur, QUERY_TIMEOUT_MS)
                cur.execute("SELECT core.main_impact_surface(%s,%s)", (REPO200, BRANCH))
                cur.fetchone()
            run.close()
            rstat = psycopg2.connect(
                f"postgresql://veripsa_migrator@localhost/{DB}",
                connect_timeout=CONNECT_TIMEOUT_S,
            )
            with rstat, rstat.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(max(calls),0) FROM pg_stat_user_functions s JOIN pg_proc p ON p.oid=s.funcid "
                    "WHERE p.proname='_claim_adjacency'")
                adj_calls = cur.fetchone()[0]
            rstat.close()
        except psycopg2.errors.QueryCanceled:
            raise
        except Exception as e:  # track_functions / pg_stat unavailable on this build → skip behaviourally, LOCK3 structural still holds
            print(f"  [info] LOCK3 behavioural skipped (pg_stat unavailable: {e})")
        if adj_calls is not None:
            checks.append((
                f"LOCK3 behavioural: core._claim_adjacency runs AT MOST ONCE per main_impact_surface call "
                f"(measured {adj_calls} call(s) for one surface call; the audit:scale regression ran it 2x = a "
                f"duplicate O(edges) whole-graph scan, ~1.1s wasted on a real Django-class graph)", adj_calls <= 1))

    except psycopg2.errors.QueryCanceled:
        checks.append((
            f"bounded execution: seed/query exceeded its hard statement timeout "
            f"(seed={SEED_TIMEOUT_MS // 1000}s query={QUERY_TIMEOUT_MS // 1000}s) instead of hanging the release job",
            False,
        ))
    finally:
        if not _drop_database():
            checks.append((f"cleanup failed or exceeded {CLEANUP_TIMEOUT_S}s", False))

    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("PERF BUDGET GATE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
