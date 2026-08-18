#!/usr/bin/env python3
"""ALGORITHMIC-COMPLEXITY / PATHOLOGICAL-INPUT DoS gate — a single CRAFTED repo / file / PR, SHAPED to blow up
the extractor or engine in time / memory / stack, must NEVER take down the shared instance. It must degrade to
an honest file-level 'unknown', never hang or OOM.

Premise (audit:dos 2026-06-18): prior passes covered VOLUME (throughput) and QUERY PLANS (perf indexes). This
is different — a malicious (or merely weird) customer repo crafted to be pathological WITHIN the volume caps:

  * SYMBOL EXPLOSION   — ONE 1.5 MB file of ~150k one-line `def f():0` slips UNDER the per-file SIZE cap, yet
    used to explode build_graph's node list (~150k nodes, ~400 MB peak RSS, a ~19 MB JSON ingest payload).
    BOUND: a PER-FILE SYMBOL/EDGE cap (_PER_FILE_SYMBOL_CAP) → the file degrades to a bare file node (still
    visible for direct collision, honestly file-level), the output/payload stay tiny.
  * DEEP NESTING       — a deeply-nested expression makes ast.parse hit the C-recursion guard. It must be
    CAUGHT (RecursionError / SyntaxError) and degrade to a bare file node, never crash the ingest.
  * touched_ranges     — a PR file with THOUSANDS of tiny disjoint diff hunks → a huge ranges list the engine
    joins against every file symbol (O(ranges × symbols)). BOUND: a hunk cap at parse (App) AND a ranges cap
    in the DB (_ranges_from_jsonb, the authoritative boundary) → above the cap, NO ranges = file-level fallback.
  * ADVERSARIAL STRINGS — a megabyte-long symbol name + the customer-surface scrub regex (_INTERNAL_LABEL_RE)
    must be LINEAR (no ReDoS / catastrophic backtracking).

This gate is PURE + OFFLINE (no DB, no network, no deploy): it crafts the pathological inputs in memory / a
temp dir and feeds them through the REAL guards, asserting each is BOUNDED in time and output. Without the
guards a craft hangs / OOMs / ships a huge payload; with them every path degrades honestly.

Run:  python3 tests/test_dos_pathological_inputs.py     (no DB needed)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import code_graph_extract as X  # noqa: E402
import github_rest as GH        # noqa: E402
import render_safe as RS        # noqa: E402

FAIL = 0


def check(cond: bool, label: str):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def test_symbol_explosion_bounded():
    """ONE 1.5 MB file of ~150k one-line defs (under the SIZE cap) must NOT explode the graph — the per-file
    symbol cap degrades it to a bare file node with a tiny payload, fast and within bounded memory."""
    d = tempfile.mkdtemp(prefix="dos_sym_")
    line = "def f():0\n"
    n = 1_499_000 // len(line)        # pack to just under _FILE_SIZE_CAP (1.5 MB)
    with open(os.path.join(d, "big.py"), "w") as fh:
        fh.write(line * n)
    sz = os.path.getsize(os.path.join(d, "big.py"))
    check(sz <= X._FILE_SIZE_CAP, f"fixture is under the size cap ({sz} <= {X._FILE_SIZE_CAP}) — a symbol, not a size, attack")

    t0 = time.time()
    g = X.build_graph(d)
    dt = time.time() - t0
    nodes = len(g["nodes"])
    payload_bytes = len(json.dumps([{"id": x["id"], "name": x.get("name")} for x in g["nodes"]]))

    check(nodes <= X._PER_FILE_SYMBOL_CAP + 5,
          f"node count bounded to ~cap ({nodes} <= {X._PER_FILE_SYMBOL_CAP}) — the 150k-symbol explosion is dropped")
    # the bare-file degrade collapses it to ONE node + a tiny payload (was ~150k nodes / ~19 MB JSON unbounded)
    check(nodes <= 5, f"a pathological symbol-explosion file degrades to a bare file node ({nodes} nodes)")
    check(payload_bytes < 100_000, f"ingest payload stays tiny ({payload_bytes} B), never the ~19 MB explosion")
    check(dt < 30, f"build_graph stays bounded in time on the craft ({dt:.1f}s)")


def test_schema_config_passes_share_source_guards():
    """The schema + config passes must read through the SAME size/binary/generated/symlink guards as the
    code pass — NOT their own independent os.walk that reads every .sql/.py/config file in FULL. So an
    OVERSIZED .sql, a binary renamed to .sql, and an OVERSIZED config are all filtered out BEFORE those
    passes parse them (no table / config_key nodes minted from a file that bypassed the caps)."""
    # (a) an OVERSIZED .sql — a `.sql` has a HIGHER cap than other files (_SCHEMA_FILE_SIZE_CAP, 12 MB) so a
    #     legitimately large whole-DB schema dump (Rails/GitLab db/structure.sql) is parsed for recall
    #     (see test_schema_large_file.py). The DoS guarantee for that exception is NOT "skip all big .sql" —
    #     it is BOUNDED: a `.sql` ABOVE the schema cap is still size-filtered (never parsed), and a `.sql`
    #     UNDER the cap parses in BOUNDED TIME (a2). A pathological multi-MB .sql can't be parsed unboundedly.
    d = tempfile.mkdtemp(prefix="dos_guard_sql_")
    with open(os.path.join(d, "big.sql"), "w") as fh:
        fh.write("CREATE TABLE bigtab (id int);\n")
        fh.write("x" * (X._SCHEMA_FILE_SIZE_CAP + 100_000))   # over the SCHEMA cap → still size-filtered
    g = X.build_graph(d)
    tabs = [n for n in g["nodes"] if n.get("kind") == "table"]
    check(len(tabs) == 0, f"a .sql OVER the schema cap ({X._SCHEMA_FILE_SIZE_CAP}B) is size-filtered (table nodes={len(tabs)}, want 0)")

    # (a2) a LARGE .sql JUST UNDER the schema cap IS parsed (recall) but stays BOUNDED IN TIME — the actual
    #      DoS guarantee for the schema exception (linear parse + _MAX_TABLES output cap), not a time-blowup.
    d_big = tempfile.mkdtemp(prefix="dos_guard_sql_big_")
    ddl = "".join(f"CREATE TABLE t_{i} (id bigint, a text, b text);\n" for i in range(2000))
    pad_to = X._SCHEMA_FILE_SIZE_CAP - 500_000               # ~11.5 MB, just under the cap
    body = ddl + ("\n/* " + "z" * max(pad_to - len(ddl), 0) + " */\n")
    with open(os.path.join(d_big, "schema.sql"), "w") as fh:
        fh.write(body)
    assert os.path.getsize(os.path.join(d_big, "schema.sql")) > X._FILE_SIZE_CAP  # over the GENERAL cap
    _t0 = time.time(); g_big = X.build_graph(d_big); _dt = time.time() - _t0
    big_tabs = [n for n in g_big["nodes"] if n.get("kind") == "table"]
    check(len(big_tabs) > 0, f"a ~11.5MB .sql UNDER the schema cap IS parsed (tables={len(big_tabs)}, recall win)")
    check(_dt < 10.0, f"...and stays BOUNDED IN TIME ({_dt:.1f}s < 10s — linear parse, not a time-DoS)")

    # (b) a BINARY renamed to .sql (NUL byte in the first chunk) — must be binary-skipped, not regex-scanned.
    d2 = tempfile.mkdtemp(prefix="dos_guard_bin_")
    with open(os.path.join(d2, "binary.sql"), "wb") as fh:
        fh.write(b"CREATE TABLE secret (id int);\n\x00\x01\x02binary-garbage-here")
    g2 = X.build_graph(d2)
    tabs2 = [n for n in g2["nodes"] if n.get("kind") == "table"]
    check(len(tabs2) == 0, f"a BINARY .sql (NUL byte) is binary-filtered out of the schema pass (table nodes={len(tabs2)}, want 0)")

    # (c) an OVERSIZED config (.json) — pre-fix the config pass re-walked + read it in FULL; post-fix filtered.
    d3 = tempfile.mkdtemp(prefix="dos_guard_cfg_")
    with open(os.path.join(d3, "big.json"), "w") as fh:
        json.dump({"db.pool_size": 5, "padding": "y" * (X._FILE_SIZE_CAP + 100_000)}, fh)
    g3 = X.build_graph(d3)
    keys3 = [n for n in g3["nodes"] if n.get("kind") == "config_key"]
    check(len(keys3) == 0, f"an OVERSIZED config is size-filtered out of the config pass (config_key nodes={len(keys3)}, want 0)")

    # control: a NORMAL small .sql + .json are STILL fully picked up (the guards only drop the pathological).
    d4 = tempfile.mkdtemp(prefix="dos_guard_ok_")
    with open(os.path.join(d4, "schema.sql"), "w") as fh:
        fh.write("CREATE TABLE orders (id int);\n")
    with open(os.path.join(d4, "app.json"), "w") as fh:
        json.dump({"db.pool_size": 5}, fh)
    g4 = X.build_graph(d4)
    ok_tabs = {n["name"] for n in g4["nodes"] if n.get("kind") == "table"}
    ok_keys = {n["name"] for n in g4["nodes"] if n.get("kind") == "config_key"}
    check("orders" in ok_tabs, f"a normal .sql is STILL read by the schema pass (tables={sorted(ok_tabs)})")
    check("db.pool_size" in ok_keys, f"a normal .json is STILL read by the config pass (keys={sorted(ok_keys)})")


def test_config_key_output_cap():
    """The config pass must CAP its config_key output (the config analogue of _MAX_TABLES) — a crafted
    config with thousands of distinct keys cannot mint unbounded config_key nodes + the keyset behind
    unbounded reads_config edges. Above _MAX_CONFIG_KEYS the mint stops; a normal config is never clipped."""
    import _cg_config as CFG

    cap = CFG._MAX_CONFIG_KEYS
    d = tempfile.mkdtemp(prefix="dos_cfgkeys_")
    # cap + 5000 DISTINCT compound (dotted ⇒ _specific_key) keys, kept UNDER the size cap so it is the KEY
    # COUNT under test, not the file size (a distinct-key flood, the config analogue of the table flood).
    obj = {"app.key_%d" % i: 1 for i in range(cap + 5_000)}
    with open(os.path.join(d, "flood.json"), "w") as fh:
        json.dump(obj, fh)
    sz = os.path.getsize(os.path.join(d, "flood.json"))
    check(sz <= X._FILE_SIZE_CAP, f"the key-flood fixture is under the size cap ({sz} <= {X._FILE_SIZE_CAP}) — a KEY-count, not size, attack")
    t0 = time.time()
    g = X.build_graph(d)
    dt = time.time() - t0
    keys = [n for n in g["nodes"] if n.get("kind") == "config_key"]
    check(len(keys) <= CFG._MAX_CONFIG_KEYS_PER_FILE, f"config_key count from one file is bounded by the per-file cap ({len(keys)} <= {CFG._MAX_CONFIG_KEYS_PER_FILE}) — the single-file key flood is truncated, not unbounded (a tighter bound than the global {cap}-key cap, which still backstops a many-file flood)")
    check(dt < 30, f"build_graph stays bounded in time on the config-key flood ({dt:.1f}s)")

    # a NORMAL config (a few dozen keys) is NOT clipped by the ceiling (the cap must not regress real repos).
    d2 = tempfile.mkdtemp(prefix="dos_cfgkeys_ok_")
    with open(os.path.join(d2, "ok.json"), "w") as fh:
        json.dump({"app.key_%d" % i: 1 for i in range(50)}, fh)
    g2 = X.build_graph(d2)
    keys2 = [n for n in g2["nodes"] if n.get("kind") == "config_key"]
    check(len(keys2) == 50, f"a normal 50-key config is NOT clipped (got {len(keys2)}, want 50)")


def test_rails_ddl_block_scan_linear():
    """A pathological Rails MIGRATION (a deeply / monotonically nested run of Ruby block openers with
    interleaved column-DDL calls) must scan in LINEAR time. The block-aware Rails column-DDL detector
    (`_rails_column_ddl_refs`, which suppresses an `add_column`-in-`create_table`-block first arg so it
    is not mis-minted as a table — the a1dacde precision fix) maintains a block STACK; testing the
    enclosing context with a per-line `any(stack)` re-scan is O(stack-depth) PER LINE → O(n²) in the
    file's line count. At the 1.5 MB size cap a craft of ~300k minimal block-opener lines made
    build_graph spend ~90s in that one re-scan (measured) — a single weird .rb DoSing the shared
    instance UNDER the size cap, exactly the SYMBOL-not-SIZE attack class. The fix maintains the
    table-DSL-frame COUNT in O(1) per open/close, so the per-line test is O(1) and the whole scan O(n).
    BOUND here: build_graph must stay WELL under the 30s wall (with margin, not riding the edge)."""
    d = tempfile.mkdtemp(prefix="dos_rails_ddl_")
    sub = os.path.join(d, "db", "migrate")
    os.makedirs(sub)
    # Worst case for an `any(stack)` re-scan: each line is a minimal generic block opener (`x do`) so
    # the block stack grows monotonically toward N and the unconditional per-line `if not any(stack)`
    # test costs O(depth) — summed over N lines that is O(N²). We pack a believable column-DDL
    # migration body (a run of openers grows the stack, then a real `add_column :t,:c` the block-aware
    # detector must classify) to just UNDER the per-file size cap, maximizing the LINE COUNT (the n in
    # O(n²)). Pre-fix this craft made build_graph spend ~60s in that one re-scan (measured); the fix
    # maintains the table-DSL-frame count in O(1) per open/close, so the same craft is ~2s.
    opener = "x do\n"                   # a minimal Ruby block opener; NOT a table-DSL opener; grows the stack
    col = "add_column :t,:c\n"          # a column-DDL call the block-aware detector must classify
    unit = opener * 9 + col             # 9 openers (deep stack) + one column-DDL — a believable, dense body
    n = (X._FILE_SIZE_CAP - 5_000) // len(unit)
    body = unit * n
    path = os.path.join(sub, "20260620000001_pathological_block_nesting.rb")
    with open(path, "w") as fh:
        fh.write(body)
    sz = os.path.getsize(path)
    nlines = body.count("\n")
    check(sz <= X._FILE_SIZE_CAP,
          f"the pathological Rails migration is under the size cap ({sz} <= {X._FILE_SIZE_CAP}) — a block-NESTING (not size) attack")

    t0 = time.time()
    g = X.build_graph(d)
    dt = time.time() - t0
    # The block-aware detector suppresses the column-DDL first arg only when an enclosing TABLE-DSL
    # block is open; here the openers are plain `reversible do` (NOT create_table), so `:t` is a real
    # top-level migration table and is minted — the point under test is the TIME, not the table count.
    check(dt < 30,
          f"build_graph stays bounded in time on a {nlines:,}-line pathological Rails block-nesting migration ({dt:.1f}s) — the per-line block-stack test is O(1), not an O(n) any(stack) re-scan")
    check(dt < 10,
          f"...and with MARGIN (well under the 30s wall, not riding the edge): {dt:.1f}s")


def test_normal_file_unaffected():
    """The cap must not change a NORMAL file: its symbols are still fully extracted (the cap is far above a
    real file's symbol count — degrade only on the pathological)."""
    d = tempfile.mkdtemp(prefix="dos_norm_")
    with open(os.path.join(d, "ok.py"), "w") as fh:
        fh.write("import os\n\n\ndef a():\n    return os.getcwd()\n\n\nclass C:\n    def m(self):\n        return a()\n")
    g = X.build_graph(d)
    syms = [x for x in g["nodes"] if x["kind"] in ("def", "class")]
    check(len(syms) == 3, f"a normal file is fully extracted, unaffected by the cap (got {len(syms)} symbols, want 3)")
    check(g["files_parsed"] == 1, "the normal file is parsed (not degraded)")


def test_deep_nesting_degrades_not_crash():
    """A deeply-nested input makes ast.parse hit the C-recursion guard. It must be CAUGHT and degrade to a
    bare file node — never an uncaught RecursionError / crash of the ingest."""
    for shape, body in (("nested-calls", "f(" * 200_000 + "0" + ")" * 200_000),
                        ("nested-brackets", "[" * 60_000 + "0" + "]" * 60_000)):
        d = tempfile.mkdtemp(prefix=f"dos_nest_{shape}_")
        with open(os.path.join(d, "deep.py"), "w") as fh:
            fh.write("x = " + body + "\n")
        sz = os.path.getsize(os.path.join(d, "deep.py"))
        try:
            t0 = time.time()
            g = X.build_graph(d)        # must NOT raise
            dt = time.time() - t0
            # degrades to the bare file node (or skipped), never a partial explosion, never a crash
            check(len(g["nodes"]) <= 5 and dt < 30,
                  f"{shape}: deeply-nested input degraded cleanly ({len(g['nodes'])} nodes, {dt:.1f}s), no crash")
        except RecursionError:
            check(False, f"{shape}: build_graph leaked a RecursionError (must be caught + degrade)")


def test_touched_ranges_cap_app():
    """The App's hunk-header parser must cap a PR file with thousands of tiny disjoint hunks: above the cap it
    returns NO ranges (→ file-level fallback), never a huge ranges list that seeds the O(ranges × symbols) join."""
    # a synthetic unified diff with N tiny disjoint hunks (header-only; content-free by construction)
    n = 10_000
    patch = "\n".join(f"@@ -{i*4+1},1 +{i*4+1},1 @@" for i in range(n))
    t0 = time.time()
    ranges = GH.changed_line_ranges_from_patch(patch)
    dt = time.time() - t0
    check(ranges == [], f"a {n}-hunk patch yields NO ranges (file-level fallback), not a {n}-element list")
    check(dt < 5, f"parsing a pathological-hunk patch stays bounded ({dt:.2f}s)")
    # a normal patch (a few hunks) still parses to ranges (the cap is far above a real PR)
    ok_patch = "@@ -1,3 +1,4 @@\n@@ -20,2 +21,2 @@\n"
    ok = GH.changed_line_ranges_from_patch(ok_patch)
    check(len(ok) == 2 and ok[0] == [1, 3], f"a normal 2-hunk patch still maps finer to BASE-side ranges (got {ok})")


def test_adversarial_strings_linear():
    """A megabyte-long symbol name + the customer-surface scrub regex must be LINEAR (no ReDoS). A giant name
    is held as one node id (the DB ingest caps its length); the scrub on a long input must not backtrack."""
    # giant symbol name → one node, bounded time (the DB length filter drops the over-long id at ingest)
    d = tempfile.mkdtemp(prefix="dos_name_")
    with open(os.path.join(d, "n.py"), "w") as fh:
        fh.write("def " + "a" * 1_000_000 + "():0\n")
    t0 = time.time()
    g = X.build_graph(d)
    check(time.time() - t0 < 10, "a megabyte-long symbol name does not blow up extraction time")
    # the customer-surface scrub regex (_INTERNAL_LABEL_RE) must be linear on a long input (no catastrophic backtrack)
    t0 = time.time()
    RS._safe_agent("veripsa_" + "a" * 500_000)
    RS._safe_agent("x" * 500_000 + "_veripsa")
    check(time.time() - t0 < 2, "the internal-label scrub regex is linear on long input (no ReDoS)")
    _ = g  # graph built without error


def main():
    print("=== ALGORITHMIC-COMPLEXITY / PATHOLOGICAL-INPUT DoS gate (offline) ===")
    print("-- symbol explosion (one 1.5 MB file, ~150k tiny defs) --")
    test_symbol_explosion_bounded()
    print("-- schema/config passes share the source-path guards (oversized/binary/generated filtered) --")
    test_schema_config_passes_share_source_guards()
    print("-- config_key output cap (distinct-key flood truncated, not unbounded) --")
    test_config_key_output_cap()
    print("-- pathological Rails block-DDL nesting scans linearly (no any(stack) O(n^2) re-scan) --")
    test_rails_ddl_block_scan_linear()
    print("-- a normal file is unaffected by the cap --")
    test_normal_file_unaffected()
    print("-- deep nesting / recursion (ast.parse C-stack) degrades, never crashes --")
    test_deep_nesting_degrades_not_crash()
    print("-- touched_ranges explosion (thousands of tiny hunks) capped to file-level --")
    test_touched_ranges_cap_app()
    print("-- adversarial strings (megabyte name, ReDoS) stay linear --")
    test_adversarial_strings_linear()
    print("------------------------------------------------------------")
    if FAIL == 0:
        print("DOS PATHOLOGICAL-INPUT GATE: PASS")
        return 0
    print("DOS PATHOLOGICAL-INPUT GATE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
