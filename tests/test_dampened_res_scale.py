#!/usr/bin/env python3
"""Self-contained scale and compatibility gate for the dampened shared-resource axis.

``core._dampened_adjacency`` recovers an honest ``unknown`` when two in-flight
files both touch a hot table/config/API/IaC resource that normal adjacency
dampens.  Its resource axis must first materialize the active files' resource
edges, then self-join only that small set.  Joining the complete hot-resource
fan-in is O(resource fan-in²) and previously took ~23.5 seconds.

The gate deliberately has no live ``origin/main`` dependency.  That comparison
made the same commit take different paths before and after merge and let a
pre-test warm PostgreSQL's shared buffers.  Instead this gate proves:

1. the first timed query is the current function on a fresh-session,
   production-keyed, mixed-axis 9k-file coordinate and remains below 4s;
2. an explicit stored-semantic-key graph and its legacy-NULL compatibility
   twin return the identical sorted rowset;
3. the SQL still materializes active resource edges before its self-join.
4. an in-process work deadline unwinds open DB connections early enough for
   the scratch database cleanup even when the suite's ephemeral PG is disabled.

Any current-function statement timeout is a hard failure.  No warm-up, retry
after timeout, or timeout/ceiling increase is permitted.

Run:  python3 tests/test_dampened_res_scale.py
"""
from __future__ import annotations

import os
import random
import re
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

DB = "veripsa_resscale_" + str(os.getpid())
ACCT = "ACCT-DEMO"
BRANCH = "main"
DAMPEN_SQL = os.path.join(ROOT, "db", "schema", "70_social.sql")
TIMEOUT_RUNNER = os.path.join(ROOT, "scripts", "run_with_timeout.py")

# The first production-keyed mixed-axis call must remain below this absolute
# ceiling. The old unrestricted resource self-join ran this shape at ~9s+,
# while #982's redundant single-definer rescan exceeded the hard timeout.
RES_CEILING_S = 4.0
# A small mixed-axis pair proves stored-key/legacy-NULL output identity without
# contaminating the first, load-bearing production performance measurement.
COMPAT_FILES = 500
COMPAT_PRS = 100
PERF_FILES = 9000
PERF_PRS = 100
N_HOT_TABLES = 24
N_HOT_CONFIG = 16
FIXTURE_DIRS = (
    "core",
    "api",
    "web",
    "models",
    "services",
    "util",
    "handlers",
    "jobs",
    "auth",
    "db",
)
CONNECT_TIMEOUT_S = 5
LOCK_TIMEOUT_MS = 5_000
SEED_TIMEOUT_MS = 90_000
QUERY_TIMEOUT_MS = 12_000
BOOTSTRAP_TIMEOUT_S = 90
CLEANUP_TIMEOUT_S = 15
# PostgreSQL's server-side statement timeout cannot bound time spent sending
# one oversized frontend message: the server arms the statement only after it
# has received the complete message.  Keep every generated fixture INSERT small
# enough that Python regains control between writes as well as between SQL
# statements.  Both limits are contracts; neither is an advisory target.
SEED_BATCH_ROWS = 1_000
SEED_STATEMENT_MAX_BYTES = 512_000
# The standard gate wrapper allows 600s.  Arm an earlier in-process deadline
# after bootstrap so SIGALRM unwinds psycopg2 connections through ``finally``.
# The remaining 260s covers one delayed 90s seed statement, a separately
# bounded 90s rollback round-trip, the 25s cleanup subprocess envelope, and
# 55s of process-level margin. This matters when a developer intentionally
# disables the suite's ephemeral PostgreSQL cluster.
WORK_TIMEOUT_S = 240


class GateWorkTimeout(TimeoutError):
    """The in-process deadline reserved enough time for deterministic cleanup."""


def _raise_gate_work_timeout(_signal_number: int, _frame: object) -> None:
    raise GateWorkTimeout


def fixture_path(index: int) -> str:
    directory = FIXTURE_DIRS[index % len(FIXTURE_DIRS)]
    return f"src/{directory}/mod{(index // len(FIXTURE_DIRS)) % 20}/file{index}.py"


EXPLICIT_HUB_PATH = fixture_path(0)
EXPLICIT_CALLER_PATH = fixture_path(1)


def _set_session_timeout(cur, milliseconds: int) -> None:
    cur.execute(
        "SELECT set_config('statement_timeout', %s, false)",
        (f"{milliseconds}ms",),
    )
    cur.execute(
        "SELECT set_config('lock_timeout', %s, false)",
        (f"{LOCK_TIMEOUT_MS}ms",),
    )


def _connect(milliseconds: int):
    """Connect with bounds active before the first SQL statement."""
    return psycopg2.connect(
        dbname=DB,
        user="veripsa_migrator",
        host="localhost",
        connect_timeout=CONNECT_TIMEOUT_S,
        options=(
            f"-c statement_timeout={milliseconds}ms "
            f"-c lock_timeout={LOCK_TIMEOUT_MS}ms"
        ),
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


def _append_batched_insert(
    statements: list[str],
    prefix: str,
    rows: list[str],
) -> None:
    """Append deterministic INSERT batches bounded by both rows and UTF-8 bytes."""
    if not rows:
        raise ValueError(f"fixture INSERT has no rows: {prefix}")

    prefix_bytes = len((prefix + "\n").encode("utf-8"))
    separator_bytes = len(",\n".encode("utf-8"))
    terminator_bytes = len(";".encode("utf-8"))
    batch: list[str] = []
    batch_bytes = prefix_bytes + terminator_bytes

    def flush() -> None:
        nonlocal batch, batch_bytes
        statement = prefix + "\n" + ",\n".join(batch) + ";"
        encoded_bytes = len(statement.encode("utf-8"))
        if len(batch) > SEED_BATCH_ROWS:
            raise AssertionError(
                f"fixture INSERT batch has {len(batch)} rows "
                f"(max {SEED_BATCH_ROWS})"
            )
        if encoded_bytes > SEED_STATEMENT_MAX_BYTES:
            raise AssertionError(
                f"fixture INSERT batch has {encoded_bytes} bytes "
                f"(max {SEED_STATEMENT_MAX_BYTES})"
            )
        statements.append(statement)
        batch = []
        batch_bytes = prefix_bytes + terminator_bytes

    for row in rows:
        row_bytes = len(row.encode("utf-8"))
        added_bytes = row_bytes + (separator_bytes if batch else 0)
        if batch and (
            len(batch) >= SEED_BATCH_ROWS
            or batch_bytes + added_bytes > SEED_STATEMENT_MAX_BYTES
        ):
            flush()
            added_bytes = row_bytes
        if batch_bytes + added_bytes > SEED_STATEMENT_MAX_BYTES:
            raise ValueError(
                "one fixture INSERT row exceeds the statement byte contract: "
                f"{batch_bytes + added_bytes} > {SEED_STATEMENT_MAX_BYTES}"
            )
        batch.append(row)
        batch_bytes += added_bytes
    if batch:
        flush()


def _bounded_seed_statements(seed_sql: str) -> list[str]:
    """Fail before libpq if any generated statement bypasses the byte contract."""
    statements = [
        statement for statement in seed_sql.split(";") if statement.strip()
    ]
    for statement in statements:
        encoded_bytes = len(statement.encode("utf-8"))
        if encoded_bytes > SEED_STATEMENT_MAX_BYTES:
            raise ValueError(
                "generated seed statement exceeds the libpq message contract: "
                f"{encoded_bytes} > {SEED_STATEMENT_MAX_BYTES}"
            )
    return statements


def gen_seed(
    n_prs: int,
    repo: str,
    n_files: int,
    seed: int = 7,
    *,
    stored_semantic_keys: bool = True,
    include_code_axes: bool = True,
) -> str:
    """Build a deterministic content-free hot-resource coordinate.

    Semantic keys are computed in the initial INSERT with the production SQL
    helper. ``stored_semantic_keys=False`` models a pre-v1 coordinate without
    an indexed-row UPDATE or dead-tuple artifact. ``include_code_axes=False``
    isolates the resource-axis performance contract from calls/imports.

    ``reseed`` executes the generated statements one-by-one so the in-process
    work deadline regains Python control after at most one statement timeout.
    The fixture repository key is therefore restricted to the same
    semicolon-free shape used by every caller in this gate.
    """
    if ";" in repo:
        raise ValueError("fixture repository key must not contain a semicolon")
    random.seed(seed)
    rk = "".join(ch for ch in repo if ch.isalnum())

    def sql_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def key_sql(value: str) -> str:
        if not stored_semantic_keys:
            return "NULL"
        return f"core._semantic_ref_key({sql_literal(value)})"

    files = [fixture_path(i) for i in range(n_files)]
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
    nodes = []
    sym_index = {}
    for i, p in enumerate(files):
        nodes.append(
            f"('{ACCT}','{repo}','{BRANCH}','{p}','file','{p}',"
            f"NULL,'python',NULL,NULL,NULL,{key_sql(p)})"
        )
        k = max(1, int(random.gauss(4, 1.5))) if include_code_axes else 0
        line = 1
        syms = []
        for j in range(k):
            s = line
            e = line + random.randint(8, 40)
            line = e + random.randint(1, 4)
            name = f"sym_{i}_{j}"
            kind = "class" if (j == 0 and random.random() < 0.2) else "def"
            nodes.append(
                f"('{ACCT}','{repo}','{BRANCH}','{p}#{name}','{kind}',"
                f"'{p}','{name}','python',{s},{e},NULL,{key_sql(name)})"
            )
            syms.append((name, s, e))
        sym_index[p] = syms
    hot_tables = [f"hot_table_{t}" for t in range(N_HOT_TABLES)]
    hot_config = [f"hot_config_{c}" for c in range(N_HOT_CONFIG)]
    for t in hot_tables:
        nodes.append(
            f"('{ACCT}','{repo}','{BRANCH}','table::{t}','table',"
            f"'schema.sql','{t}','sql',NULL,NULL,'{t}',{key_sql(t)})"
        )
    for c in hot_config:
        nodes.append(
            f"('{ACCT}','{repo}','{BRANCH}','config::{c}','config_key',"
            f"'app.yaml','{c}','yaml',NULL,NULL,'{c}',{key_sql(c)})"
        )
    _append_batched_insert(
        out,
        "INSERT INTO core.code_node "
        "(account_id,repo,branch,node_id,node_kind,path,name,language,"
        "start_line,end_line,canonical_key,semantic_key) VALUES",
        nodes,
    )

    edges = set()
    # file0 is a deterministic import hub used by the calls_h non-vacuity
    # oracle. The remaining hubs retain the randomized scale shape.
    hubs = [0] + random.sample(range(1, n_files), 11)
    if include_code_axes:
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
        hubset = [files[h] for h in hubs]
        for i in range(n_files):
            for _ in range(random.randint(4, 9)):
                target = (
                    random.choice(hubset)
                    if random.random() < 0.5
                    else files[random.randrange(n_files)]
                )
                tsyms = sym_index.get(target, [])
                if target != files[i] and tsyms:
                    (nm, _s, _e) = random.choice(tsyms)
                    edges.add((files[i], nm, "calls"))
        # Exercise the exact #982 arm: two active paths, a unique symbol
        # defined in a real import hub, and an unimported caller. Removing the
        # import is deliberate; import-confirmed calls take the other OR arm.
        edges.discard((EXPLICIT_CALLER_PATH, EXPLICIT_HUB_PATH, "imports"))
        explicit_symbol = sym_index[EXPLICIT_HUB_PATH][0][0]
        edges.add((EXPLICIT_CALLER_PATH, explicit_symbol, "calls"))
    # MANY files query/alter each hot table; many read each hot config key — each resource >> the hub cutoff ⇒ res_hub.
    touchers_per = max(50, n_files // 15)
    for t in hot_tables:
        for i in random.sample(range(n_files), min(n_files, touchers_per)):
            edges.add(
                (files[i], t, "queries" if random.random() < 0.8 else "alters")
            )
    for c in hot_config:
        for i in random.sample(range(n_files), min(n_files, touchers_per)):
            edges.add((files[i], c, "reads_config"))
    edge_rows = [
        f"('{ACCT}','{repo}','{BRANCH}','{s}','{d}','{k}',{key_sql(d)})"
        for (s, d, k) in sorted(edges)
    ]
    _append_batched_insert(
        out,
        "INSERT INTO core.code_edge "
        "(account_id,repo,branch,src,dst,edge_kind,semantic_dst_key) VALUES",
        edge_rows,
    )
    out.append(
        f"INSERT INTO core.graph_version (account_id,repo,branch,node_count,edge_count) "
        f"VALUES ('{ACCT}','{repo}','{BRANCH}',{len(nodes)},{len(edges)}) "
        f"ON CONFLICT (account_id,repo,branch) DO UPDATE SET node_count=EXCLUDED.node_count;")
    ev = []
    n = 0
    for h in hubs:
        for _ in range(random.randint(3, 8)):
            n += 1
            ev.append(f"('{ACCT}','EV-{rk}-{n}','landed','AG-A','{repo}','{BRANCH}','{files[h]}', now() - (random()*20||' days')::interval)")
    _append_batched_insert(
        out,
        "INSERT INTO core.event "
        "(account_id,event_id,kind,agent_id,repo,branch,path,occurred_at) VALUES",
        ev,
    )
    # claims BIASED onto resource co-touchers (so res_h fires inflight×inflight) then spread, deduped per path.
    res_touchers = sorted({s for (s, d, k) in edges if k in ("queries", "alters", "reads_config")})
    random.shuffle(res_touchers)
    claim_rows = []
    pr = 0
    seen = set()
    targets = []
    explicit_call_pair = (
        [EXPLICIT_CALLER_PATH, EXPLICIT_HUB_PATH]
        if include_code_axes
        else []
    )
    for p in (
        explicit_call_pair
        + res_touchers
        + [files[h] for h in hubs]
        + [files[i] for i in random.sample(range(n_files), n_files)]
    ):
        if p not in seen:
            seen.add(p)
            targets.append(p)
    for p in targets:
        if pr >= n_prs:
            break
        a, b = ("AG-A", "AG-B") if pr % 2 == 0 else ("AG-B", "AG-A")
        ch_h, ch_w = f"PR-{pr}", f"PR-{pr+100000}"
        pr += 2
        claim_rows.append(f"('{ACCT}','{repo}','{BRANCH}','{ch_h}:{p}','{a}','{ch_h}','{p}','active',NULL)")
        claim_rows.append(f"('{ACCT}','{repo}','{BRANCH}','{ch_w}:{p}','{b}','{ch_w}','{p}','waiting',NULL)")
    _append_batched_insert(
        out,
        "INSERT INTO core.claim "
        "(account_id,repo,branch,claim_id,agent_id,change_id,target_path,"
        "claim_state,touched_ranges) VALUES",
        claim_rows,
    )
    for t in ("code_node", "code_edge", "graph_version", "claim", "event"):
        out.append(f"ALTER TABLE core.{t} ENABLE TRIGGER trg_governed_{t};")
    out.append("COMMIT;")
    out.append("ANALYZE core.code_node; ANALYZE core.code_edge; ANALYZE core.claim; ANALYZE core.event;")
    return "\n".join(out)


def reseed(
    n_prs,
    repo,
    n_files,
    seed=7,
    *,
    stored_semantic_keys=True,
    include_code_axes=True,
):
    conn = _connect(SEED_TIMEOUT_MS)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core,pg_catalog")
            _set_session_timeout(cur, SEED_TIMEOUT_MS)
            seed_sql = gen_seed(
                n_prs,
                repo,
                n_files=n_files,
                seed=seed,
                stored_semantic_keys=stored_semantic_keys,
                include_code_axes=include_code_axes,
            )
            # Python signal handlers are not guaranteed to run while PQexec
            # blocks.  Every generated INSERT is therefore row/byte bounded,
            # and this final generic check rejects any future unbatched
            # statement before libpq sees it.
            for statement in _bounded_seed_statements(seed_sql):
                cur.execute(statement)
    finally:
        conn.close()


def damp(repo):
    """Full sorted rowset of core._dampened_adjacency (SECURITY DEFINER over FORCE-RLS → set the account GUC)."""
    c = _connect(QUERY_TIMEOUT_MS)
    try:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("SET search_path=core,pg_catalog")
            _set_session_timeout(cur, QUERY_TIMEOUT_MS)
            cur.execute(
                "SELECT set_config('core.current_account',%s,false)", (ACCT,)
            )
            cur.execute(
                "SELECT f,f_change_id,nbr,nbr_change_id,via_hub "
                "FROM core._dampened_adjacency(%s,%s,%s) "
                "ORDER BY 1,2,3,4,5",
                (ACCT, repo, BRANCH),
            )
            return [list(r) for r in cur.fetchall()]
    finally:
        c.close()


def damp_count_cold(repo):
    """One bounded call with count and exact calls_h oracles on a fresh session."""
    c = _connect(QUERY_TIMEOUT_MS)
    try:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("SET search_path=core,pg_catalog")
            _set_session_timeout(cur, QUERY_TIMEOUT_MS)
            cur.execute(
                "SELECT set_config('core.current_account',%s,false)",
                (ACCT,),
            )
            t0 = time.perf_counter()
            cur.execute(
                """
                WITH rows AS MATERIALIZED (
                    SELECT f,f_change_id,nbr,nbr_change_id,via_hub
                      FROM core._dampened_adjacency(%s,%s,%s)
                )
                SELECT count(*),
                       count(*) FILTER (
                           WHERE f=%s AND f_change_id=%s
                             AND nbr=%s AND nbr_change_id=%s
                             AND via_hub=%s
                       ),
                       count(*) FILTER (
                           WHERE f=%s AND f_change_id=%s
                             AND nbr=%s AND nbr_change_id=%s
                             AND via_hub=%s
                       )
                  FROM rows
                """,
                (
                    ACCT,
                    repo,
                    BRANCH,
                    EXPLICIT_CALLER_PATH,
                    "PR-0",
                    EXPLICIT_HUB_PATH,
                    "PR-2",
                    EXPLICIT_HUB_PATH,
                    EXPLICIT_HUB_PATH,
                    "PR-2",
                    EXPLICIT_CALLER_PATH,
                    "PR-0",
                    EXPLICIT_HUB_PATH,
                ),
            )
            rows, forward_rows, reverse_rows = cur.fetchone()
            return (
                time.perf_counter() - t0,
                rows,
                forward_rows,
                reverse_rows,
            )
    finally:
        c.close()


def stored_key_shape(repo):
    """Read back exact stored-key/null counts and edge kinds for one coordinate."""
    c = _connect(QUERY_TIMEOUT_MS)
    try:
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("SET search_path=core,pg_catalog")
            _set_session_timeout(cur, QUERY_TIMEOUT_MS)
            cur.execute(
                "SELECT set_config('core.current_account',%s,false)", (ACCT,)
            )
            cur.execute(
                """
                SELECT count(*),
                       count(*) FILTER (WHERE semantic_key IS NULL)
                  FROM core.code_node
                 WHERE account_id=%s AND repo=%s AND branch=%s
                """,
                (ACCT, repo, BRANCH),
            )
            node_total, node_null = cur.fetchone()
            cur.execute(
                """
                SELECT count(*),
                       count(*) FILTER (WHERE semantic_dst_key IS NULL),
                       array_agg(DISTINCT edge_kind ORDER BY edge_kind)
                  FROM core.code_edge
                 WHERE account_id=%s AND repo=%s AND branch=%s
                """,
                (ACCT, repo, BRANCH),
            )
            edge_total, edge_null, edge_kinds = cur.fetchone()
            return {
                "node_total": node_total,
                "node_null": node_null,
                "edge_total": edge_total,
                "edge_null": edge_null,
                "edge_kinds": tuple(edge_kinds or ()),
            }
    finally:
        c.close()


def main() -> int:
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
    checks = []
    phase = "schema structural contract"
    previous_alarm_handler = None
    try:
        previous_alarm_handler = signal.signal(
            signal.SIGALRM,
            _raise_gate_work_timeout,
        )
        signal.alarm(WORK_TIMEOUT_S)
        current_text = open(DAMPEN_SQL, encoding="utf-8").read()

        d_start = current_text.find("FUNCTION core._dampened_adjacency")
        d_end = current_text.find(
            "ALTER FUNCTION core._dampened_adjacency", d_start
        )
        d_body = (
            current_text[d_start:d_end]
            if d_start >= 0 and d_end > d_start
            else ""
        )
        c_start = current_text.find("FUNCTION core._claim_adjacency")
        c_end = current_text.find("ALTER FUNCTION core._claim_adjacency", c_start)
        c_body = (
            current_text[c_start:c_end]
            if c_start >= 0 and c_end > c_start
            else ""
        )
        has_active_res = (
            "active_res_edge AS MATERIALIZED" in d_body
            and "res_touch AS MATERIALIZED" in d_body
        )
        res_self_join_on_active = bool(
            re.search(r"res_touch\s+t1\s+JOIN\s+res_touch\s+t2", d_body)
        )
        no_code_edge_selfjoin = "JOIN core.code_edge e2 ON" not in d_body
        checks.append(
            (
                "STRUCTURAL: res axis materializes active resource edges before "
                "res_touch self-joins (never code_edge×code_edge fan-in)",
                has_active_res
                and res_self_join_on_active
                and no_code_edge_selfjoin,
            )
        )
        checks.append(
            (
                "STRUCTURAL: single-definer arms do not re-scan global defs_ok "
                "through the redundant correlated d2 join",
                "JOIN defs_ok d2" not in d_body
                and "JOIN defs_ok d2" not in c_body,
            )
        )

        # The load-bearing current-function measurement runs first. No
        # comparison/oracle query is allowed to warm this coordinate.
        perf_repo = f"resperf/stored{PERF_FILES}"
        phase = "production-keyed mixed-axis performance seed"
        reseed(
            PERF_PRS,
            perf_repo,
            PERF_FILES,
            seed=99,
            stored_semantic_keys=True,
            include_code_axes=True,
        )
        phase = "production-keyed mixed-axis first query and calls_h oracle"
        (
            t_fixed,
            n_rows,
            forward_calls_h_rows,
            reverse_calls_h_rows,
        ) = damp_count_cold(perf_repo)
        # The explicit pair is deliberately unimported, so these two exact
        # rows can only come from the unique-definer calls_h arm fixed by #982.
        # They are counted inside the same materialized first query: no warm
        # retry and no second 9k full-rowset execution.
        checks.append(
            (
                "(1) dampened adjacency is non-vacuous on the 9k production "
                "mixed-axis seed "
                f"(dampened rows={n_rows})",
                n_rows > 0,
            )
        )
        checks.append(
            (
                "(1) first current production-keyed mixed-axis query stays below "
                f"{RES_CEILING_S:.1f}s on {PERF_FILES} files "
                f"(first fresh-session call={t_fixed * 1000:.0f}ms)",
                t_fixed < RES_CEILING_S,
            )
        )
        checks.append(
            (
                "(1) explicit unimported caller and active unique-definer hub "
                "produce each exact calls_h direction once "
                f"(forward={forward_calls_h_rows} reverse={reverse_calls_h_rows})",
                forward_calls_h_rows == 1 and reverse_calls_h_rows == 1,
            )
        )
        phase = "production-keyed mixed-axis readback"
        perf_shape = stored_key_shape(perf_repo)
        checks.append(
            (
                "(1) production perf coordinate stores every semantic key "
                f"(nodes={perf_shape['node_total']} "
                f"edges={perf_shape['edge_total']})",
                perf_shape["node_total"] > 0
                and perf_shape["edge_total"] > 0
                and perf_shape["node_null"] == 0
                and perf_shape["edge_null"] == 0,
            )
        )
        checks.append(
            (
                "(1) production perf coordinate exercises code and resource "
                f"axes (edge kinds={perf_shape['edge_kinds']!r})",
                perf_shape["edge_kinds"]
                == (
                    "alters",
                    "calls",
                    "contains",
                    "imports",
                    "queries",
                    "reads_config",
                ),
            )
        )

        # Self-contained semantic compatibility oracle: identical graph/seed,
        # differing only in whether exact semantic digests are stored.
        stored_repo = "rescompat/stored"
        legacy_repo = "rescompat/legacy-null"
        phase = "stored-key compatibility seed"
        reseed(
            COMPAT_PRS,
            stored_repo,
            COMPAT_FILES,
            seed=17,
            stored_semantic_keys=True,
            include_code_axes=True,
        )
        phase = "stored-key compatibility readback/query"
        stored_shape = stored_key_shape(stored_repo)
        stored_rows = damp(stored_repo)

        phase = "legacy-null compatibility seed"
        reseed(
            COMPAT_PRS,
            legacy_repo,
            COMPAT_FILES,
            seed=17,
            stored_semantic_keys=False,
            include_code_axes=True,
        )
        phase = "legacy-null compatibility readback/query"
        legacy_shape = stored_key_shape(legacy_repo)
        legacy_rows = damp(legacy_repo)

        checks.append(
            (
                "(2) compatibility fixtures prove stored-key vs legacy-NULL "
                "row shape (no accidental mixed generation)",
                stored_shape["node_total"] == legacy_shape["node_total"] > 0
                and stored_shape["edge_total"] == legacy_shape["edge_total"] > 0
                and stored_shape["node_null"] == 0
                and stored_shape["edge_null"] == 0
                and legacy_shape["node_null"] == legacy_shape["node_total"]
                and legacy_shape["edge_null"] == legacy_shape["edge_total"]
                and stored_shape["edge_kinds"] == legacy_shape["edge_kinds"],
            )
        )
        checks.append(
            (
                "(2) stored semantic keys and legacy-NULL fallback return the "
                f"identical non-empty sorted rowset (rows={len(stored_rows)})",
                bool(stored_rows) and stored_rows == legacy_rows,
            )
        )
    except psycopg2.errors.QueryCanceled:
        checks.append(
            (
                f"bounded execution during {phase}: seed/query exceeded its "
                f"hard statement timeout (seed={SEED_TIMEOUT_MS // 1000}s "
                f"query={QUERY_TIMEOUT_MS // 1000}s)",
                False,
            )
        )
    except GateWorkTimeout:
        checks.append(
            (
                f"bounded execution during {phase}: gate work exceeded "
                f"{WORK_TIMEOUT_S}s before its reserved cleanup window",
                False,
            )
        )
    finally:
        try:
            try:
                signal.alarm(0)
            finally:
                if previous_alarm_handler is not None:
                    signal.signal(signal.SIGALRM, previous_alarm_handler)
        finally:
            if not _drop_database():
                checks.append(
                    (f"cleanup failed or exceeded {CLEANUP_TIMEOUT_S}s", False)
                )

    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("DAMPENED RES SCALE GATE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
