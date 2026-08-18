#!/usr/bin/env python3
"""ENGINE ROBUSTNESS gate — build_graph must never crash and must stay bounded on
ANY pathological customer repo: oversized files, binary/non-UTF-8 with code extensions,
zero-byte files, deep/wide directory trees, symlink loops, mid-walk file races,
permission-denied files, unexpected per-file extractor exceptions, giant .gitattributes,
and duplicate-file-node scenarios.

Each case asserts: (1) build_graph returns (no exception), (2) output is bounded
(node count, edge count, elapsed time all within sane limits), (3) the fix being
tested is correct where a pre-fix gap existed.

Run:   python3 tests/test_engine_robustness.py    (no DB / no network)
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import code_graph_extract as X  # noqa: E402

FAIL = 0


def check(cond: bool, label: str):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _build(d, *, timeout_s=30):
    """Run build_graph on tmpdir d, return (graph, elapsed). Asserts no exception."""
    t0 = time.time()
    g = X.build_graph(d)
    return g, time.time() - t0


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_huge_file_already_guarded():
    """A 50 MB file with a .py extension is skipped by the existing _FILE_SIZE_CAP (1.5 MB) guard.
    This case was already handled before the robustness PR; this test confirms it stays handled."""
    d = tempfile.mkdtemp(prefix="rob_huge_")
    with open(os.path.join(d, "huge.py"), "wb") as fh:
        fh.write(b"def f(): pass\n" * 3_500_000)   # ~50 MB
    sz = os.path.getsize(os.path.join(d, "huge.py"))
    check(sz > X._FILE_SIZE_CAP, f"fixture is OVER the size cap ({sz} > {X._FILE_SIZE_CAP})")
    g, dt = _build(d)
    check(len(g["nodes"]) == 0, f"huge file yields 0 nodes (size-capped, skipped entirely, got {len(g['nodes'])})")
    check(dt < 5, f"huge file skipped fast ({dt:.2f}s)")


def test_giant_single_line_file():
    """A file just under the size cap but consisting of one giant line (1.4 MB string literal).
    No symbol explosion risk (one string is one expression), but the tokenizer / ast must
    not hang on the single long token."""
    d = tempfile.mkdtemp(prefix="rob_longline_")
    with open(os.path.join(d, "longline.py"), "w") as fh:
        fh.write('x = "' + "a" * 1_400_000 + '"\n')
    sz = os.path.getsize(os.path.join(d, "longline.py"))
    check(sz < X._FILE_SIZE_CAP, f"fixture is under size cap ({sz} < {X._FILE_SIZE_CAP})")
    g, dt = _build(d)
    check(len(g["nodes"]) <= 5 and dt < 30,
          f"giant single-line file: {len(g['nodes'])} nodes, {dt:.2f}s — bounded, no hang")


def test_binary_file_with_code_extension():
    """A binary file (NUL bytes) given a .py or .go extension is skipped by the binary probe.
    Was already handled; this confirms it stays robust."""
    d = tempfile.mkdtemp(prefix="rob_bin_")
    with open(os.path.join(d, "binary.py"), "wb") as fh:
        fh.write(b"\x00\x01\x02\x03" * 200)
    with open(os.path.join(d, "binary.go"), "wb") as fh:
        fh.write(b"ELF\x00\x01\x02" * 200)
    g, dt = _build(d)
    check(len(g["nodes"]) == 0, f"binary files with code ext: 0 nodes (binary-probed, skipped, got {len(g['nodes'])})")
    check(dt < 5, f"binary probe fast ({dt:.2f}s)")


def test_zero_byte_files():
    """Zero-byte files with code extensions (.py, .js, .sql) are valid (empty modules) and
    must appear as file nodes without crashing. Their parsing yields no symbols; they still
    need a node for direct-collision detection."""
    d = tempfile.mkdtemp(prefix="rob_zero_")
    open(os.path.join(d, "empty.py"), "w").close()
    open(os.path.join(d, "empty.js"), "w").close()
    open(os.path.join(d, "empty.sql"), "w").close()
    g, dt = _build(d)
    paths = {n["path"] for n in g["nodes"]}
    check("empty.py" in paths, f"zero-byte .py still gets a file node ({paths})")
    check("empty.js" in paths, f"zero-byte .js still gets a file node ({paths})")
    check("empty.sql" in paths, f"zero-byte .sql still gets a file node ({paths})")
    # each file appears at most ONCE (dedup guard)
    from collections import Counter
    by_id = Counter(n["id"] for n in g["nodes"] if n.get("kind") == "file")
    dups = {k: v for k, v in by_id.items() if v > 1}
    check(not dups, f"no duplicate file nodes after dedup (dups={dups})")
    check(dt < 5, f"zero-byte files handled fast ({dt:.2f}s)")


def test_non_utf8_encoding():
    """Source files with non-UTF-8 encodings (latin-1, shift-jis with PEP-263 cookie) are
    decoded via _read_source_text and their symbols extracted. Was already handled."""
    d = tempfile.mkdtemp(prefix="rob_enc_")
    with open(os.path.join(d, "latin.py"), "wb") as fh:
        fh.write(b"# -*- coding: latin-1 -*-\ndef greet():\n    return 'Bonjour l\xe9on'\n")
    with open(os.path.join(d, "sjis.py"), "wb") as fh:
        fh.write(b"# -*- coding: shift_jis -*-\ndef greet2():\n    pass\n")
    g, dt = _build(d)
    sym_names = {n.get("name") for n in g["nodes"] if n.get("kind") == "def"}
    check("greet" in sym_names, f"latin-1 .py: symbol 'greet' extracted (got {sym_names})")
    check("greet2" in sym_names, f"shift-jis .py: symbol 'greet2' extracted (got {sym_names})")
    check(dt < 10, f"non-UTF-8 files extracted fast ({dt:.2f}s)")


def test_deep_directory_tree():
    """A deep directory tree (200 levels, each directory segment 2 chars to stay under OS path limits)
    is walked by os.walk and source files are found. Was already handled by followlinks=False."""
    d = tempfile.mkdtemp(prefix="rob_deep_")
    cur = d
    # 200 directories deep; place a file every 40 levels
    for i in range(200):
        cur = os.path.join(cur, f"x{i}")
        os.makedirs(cur, exist_ok=True)
        if i % 40 == 0:
            with open(os.path.join(cur, f"f{i}.py"), "w") as fh:
                fh.write(f"def fn{i}(): pass\n")
    g, dt = _build(d)
    check(len(g["nodes"]) >= 5, f"deep tree: source files found in nested dirs ({len(g['nodes'])} nodes)")
    check(dt < 30, f"deep tree walked fast ({dt:.2f}s)")


def test_symlink_loop():
    """A directory symlink pointing to an ancestor (a classic symlink loop) must NOT cause
    infinite recursion. os.walk with followlinks=False skips symlinked dirs; the dirnames
    prune in _iter_source_files also explicitly prunes symlinked dirs."""
    d = tempfile.mkdtemp(prefix="rob_sym_")
    subdir = os.path.join(d, "a", "b", "c")
    os.makedirs(subdir)
    with open(os.path.join(subdir, "code.py"), "w") as fh:
        fh.write("def real(): pass\n")
    # loop: c/back -> ../../a (ancestor)
    os.symlink(os.path.join(d, "a"), os.path.join(subdir, "back"))
    g, dt = _build(d)
    check("a/b/c/code.py" in {n["path"] for n in g["nodes"]},
          f"symlink loop: real file still found ({[n['path'] for n in g['nodes']]})")
    check(dt < 10, f"symlink loop terminated fast ({dt:.2f}s)")
    # real file node appears exactly once
    sym_file_count = sum(1 for n in g["nodes"] if n["path"] == "a/b/c/code.py" and n.get("kind") == "file")
    check(sym_file_count == 1, f"real file appears exactly once (got {sym_file_count})")


def test_file_disappears_mid_walk():
    """A file that exists when os.walk lists it but is deleted before build_graph reads it.
    _passes_file_guards handles OSError from os.path.getsize → treats file as skipped.
    The other files in the directory must still be processed."""
    d = tempfile.mkdtemp(prefix="rob_race_")
    for i in range(5):
        with open(os.path.join(d, f"normal{i}.py"), "w") as fh:
            fh.write(f"def fn{i}(): pass\n")
    # deleted before build_graph runs — simulates the race
    ghost = os.path.join(d, "ghost.py")
    with open(ghost, "w") as fh:
        fh.write("def phantom(): pass\n")
    os.unlink(ghost)
    g, dt = _build(d)
    check(len(g["nodes"]) >= 5, f"race: remaining files still processed ({len(g['nodes'])} nodes)")
    check(not any("ghost" in n.get("id", "") for n in g["nodes"]),
          "race: vanished file leaves no node")
    check(dt < 10, f"race: no hang on missing file ({dt:.2f}s)")


def test_permission_denied_file():
    """A file with mode 000 (no read permission) must be skipped (treated like binary → unreadable).
    The remaining files in the directory must still be processed."""
    d = tempfile.mkdtemp(prefix="rob_perm_")
    with open(os.path.join(d, "normal.py"), "w") as fh:
        fh.write("def ok(): pass\n")
    denied = os.path.join(d, "denied.py")
    with open(denied, "w") as fh:
        fh.write("def secret(): pass\n")
    os.chmod(denied, 0o000)
    try:
        g, dt = _build(d)
        normal_found = any(n.get("path", "").endswith("normal.py") for n in g["nodes"])
        denied_found = any("denied" in n.get("id", "") for n in g["nodes"])
        check(normal_found, f"permission-denied: normal.py still found in graph")
        check(not denied_found, "permission-denied: denied.py not included (no node for unreadable file)")
        check(dt < 10, f"permission-denied: no hang ({dt:.2f}s)")
    finally:
        os.chmod(denied, 0o644)  # restore so tempdir cleanup works


def test_unexpected_extractor_exception_preserves_document_contract():
    """An unexpected per-file extractor exception degrades to a bare file node.

    The outer dispatch guard exists specifically so one grammar/parser defect cannot
    abort a repository build. The persisted observability contract also requires
    ``input_file_count`` to equal the distinct file/config_file path set, so silently
    skipping the failed file would turn that safety fallback into a DB-rejected full
    ingest. Keep the document fact, drop only structural detail, and count the failure.
    """
    with tempfile.TemporaryDirectory(prefix="rob_extractor_exception_") as d:
        path = os.path.join(d, "boom.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("def retained_at_file_level():\n    return True\n")

        original = X.extract_file_py

        def _raise_unexpected(*_args, **_kwargs):
            raise RuntimeError("synthetic per-file extractor failure")

        X.extract_file_py = _raise_unexpected
        try:
            g, dt = _build(d)
        finally:
            X.extract_file_py = original

        documents = {
            node.get("path")
            for node in g["nodes"]
            if node.get("kind") in {"file", "config_file"}
            and node.get("path")
        }
        file_nodes = [
            node for node in g["nodes"]
            if node.get("kind") == "file" and node.get("path") == "boom.py"
        ]
        metrics = g.get("metrics") or {}

        check(
            len(file_nodes) == 1
            and file_nodes[0].get("id") == "boom.py"
            and file_nodes[0].get("language") == "python"
            and file_nodes[0].get("analysis_status") == "failed"
            and bool(file_nodes[0].get("content_hash")),
            "unexpected extractor exception keeps one canonical, failed, hashed bare "
            f"file node ({file_nodes})",
        )
        check(
            g.get("files_failed") == 1
            and g.get("files_parsed") == 0
            and not g["edges"],
            "unexpected extractor exception records one failure and emits no "
            "fabricated structural detail",
        )
        check(
            metrics.get("input_file_count") == len(documents) == 1
            and metrics.get("node_kind_counts", {}).get("file") == 1,
            "failed-file observability matches the persisted document set "
            f"(input={metrics.get('input_file_count')}, documents={documents})",
        )
        check(dt < 5, f"unexpected extractor exception degrades fast ({dt:.2f}s)")


def test_general_parser_loss_stays_path_local_in_the_stored_graph():
    """The stored status is local; the query wall promotes only in-flight peers.

    Persistently poisoning every document would make one unrelated unsupported
    file turn all future reviews Unknown.  The DB query boundary has the
    in-flight set needed to protect the possible opposite endpoint without that
    permanent precision loss.
    """
    with tempfile.TemporaryDirectory(prefix="rob_coordinate_incomplete_") as d:
        fixtures = {
            "broken.py": "def lost_definition():\n    return True\n",
            "consumer.py": "lost_definition()\n",
            "opposite.py": "def opposite_endpoint():\n    return True\n",
            "settings.json": '{"FEATURE_FLAG_ALPHA": true}\n',
        }
        for rel, body in fixtures.items():
            with open(os.path.join(d, rel), "w", encoding="utf-8") as fh:
                fh.write(body)

        original = X.extract_file_py

        def _fail_one(path, rel):
            if rel == "broken.py":
                raise RuntimeError("synthetic unbounded structural loss")
            return original(path, rel)

        X.extract_file_py = _fail_one
        try:
            graph, dt = _build(d)
        finally:
            X.extract_file_py = original

        statuses = {
            node.get("path"): node.get("analysis_status")
            for node in graph["nodes"]
            if node.get("kind") in {"file", "config_file"}
            and node.get("path") in fixtures
        }
        check(
            statuses.get("broken.py") == "failed"
            and all(
                statuses.get(path) is None
                for path in fixtures
                if path != "broken.py"
            ),
            "one general parser failure remains path-local in storage while "
            f"normal peer documents remain complete ({statuses!r})",
        )
        check(
            graph.get("files_failed") == 1,
            "path-local uncertainty preserves the one actual hard failure "
            f"counter (got {graph.get('files_failed')!r})",
        )
        check(dt < 5, f"path-local uncertainty degrades fast ({dt:.2f}s)")


def test_tree_sitter_partial_tree_is_incomplete_not_failed():
    """tree-sitter error recovery yields partial evidence, never a complete parse."""
    from tree_sitter import Parser

    languages = X._ts_languages()
    check(
        "javascript" in languages,
        "JavaScript grammar is available for the partial-tree contract",
    )
    if "javascript" not in languages:
        return

    with tempfile.TemporaryDirectory(prefix="rob_partial_tree_") as d:
        broken_path = os.path.join(d, "broken.js")
        with open(broken_path, "w", encoding="utf-8") as fh:
            fh.write("export function broken( { const value = ;\n")
        parser = Parser(languages["javascript"])
        direct_nodes, _direct_edges, ok = X.extract_file_ts(
            broken_path, "broken.js", "javascript", parser
        )
        check(
            ok is False
            and direct_nodes[0].get("analysis_status") == "incomplete",
            "tree-sitter root.has_error is classified as partial/incomplete "
            f"(ok={ok!r}, node={direct_nodes[0]!r})",
        )

        with open(os.path.join(d, "peer.py"), "w", encoding="utf-8") as fh:
            fh.write("def peer():\n    return True\n")
        graph, dt = _build(d)
        statuses = {
            node.get("path"): node.get("analysis_status")
            for node in graph["nodes"]
            if node.get("kind") == "file"
        }
        check(
            statuses.get("broken.js") == "incomplete"
            and statuses.get("peer.py") is None,
            "partial tree remains locally incomplete without poisoning an "
            f"unrelated persisted peer ({statuses!r})",
        )
        check(
            graph.get("files_failed") == 0
            and graph.get("files_skipped") >= 1,
            "partial tree is counted as bounded/incomplete, not hard failed "
            f"(failed={graph.get('files_failed')}, skipped={graph.get('files_skipped')})",
        )
        check(dt < 5, f"partial-tree uncertainty degrades fast ({dt:.2f}s)")


def test_bare_parser_fallbacks_are_incomplete():
    """Unavailable/unsupported parsers must not turn missing edges into Clear."""
    with tempfile.TemporaryDirectory(prefix="rob_incomplete_parser_") as d:
        fixtures = {
            "service.go": "package service\nfunc Run() {}\n",
            "legacy.scala": "object Legacy { def run = 1 }\n",
            "Widget.vue": "<script>export const ready = true</script>\n",
        }
        for rel, body in fixtures.items():
            with open(os.path.join(d, rel), "w", encoding="utf-8") as fh:
                fh.write(body)

        original_languages = X._ts_languages
        X._ts_languages = lambda: {}
        try:
            g, dt = _build(d)
        finally:
            X._ts_languages = original_languages

        statuses = {
            node.get("path"): node.get("analysis_status")
            for node in g["nodes"]
            if node.get("kind") == "file"
            and node.get("path") in fixtures
        }
        check(
            statuses
            == {path: "incomplete" for path in fixtures},
            "grammar-unavailable Go, unsupported Scala, and unavailable Vue "
            f"SFC parser retain incomplete file nodes ({statuses!r})",
        )
        check(
            g.get("files_parsed") == 0
            and g.get("files_failed") == 0
            and g.get("files_skipped") == len(fixtures),
            "incomplete bare-node fallbacks preserve parsed/failed/skipped "
            "counter semantics",
        )
        check(dt < 5, f"incomplete parser fallbacks degrade fast ({dt:.2f}s)")


def test_line_and_symbol_caps_are_incomplete():
    """Both bounded-detail fallbacks preserve the path and expose uncertainty."""
    with tempfile.TemporaryDirectory(prefix="rob_incomplete_caps_") as d:
        for rel in ("linecap.py", "symbolcap.py"):
            with open(os.path.join(d, rel), "w", encoding="utf-8") as fh:
                fh.write("def retained_only_at_file_level():\n    return True\n")

        original_line_check = X._has_too_many_lines
        original_extract = X.extract_file_py
        original_symbol_cap = X._PER_FILE_SYMBOL_CAP

        def synthetic_extract(path, rel):
            if rel == "symbolcap.py":
                return (
                    [
                        {
                            "id": rel,
                            "kind": "file",
                            "path": rel,
                            "language": "python",
                        },
                        {
                            "id": f"{rel}::too_many",
                            "kind": "def",
                            "path": rel,
                            "name": "too_many",
                        },
                    ],
                    [],
                    True,
                )
            return original_extract(path, rel)

        X._has_too_many_lines = (
            lambda path: os.path.basename(path) == "linecap.py"
        )
        X.extract_file_py = synthetic_extract
        X._PER_FILE_SYMBOL_CAP = 1
        try:
            g, dt = _build(d)
        finally:
            X._has_too_many_lines = original_line_check
            X.extract_file_py = original_extract
            X._PER_FILE_SYMBOL_CAP = original_symbol_cap

        statuses = {
            node.get("path"): node.get("analysis_status")
            for node in g["nodes"]
            if node.get("kind") == "file"
            and node.get("path") in {"linecap.py", "symbolcap.py"}
        }
        check(
            statuses
            == {
                "linecap.py": "incomplete",
                "symbolcap.py": "incomplete",
            },
            "line-count and per-file symbol caps retain incomplete file nodes "
            f"({statuses!r})",
        )
        check(
            not any(
                node.get("kind") in {"def", "class"}
                for node in g["nodes"]
            )
            and not g["edges"],
            "bounded files retain no fabricated structural detail",
        )
        check(
            g.get("files_parsed") == 0
            and g.get("files_failed") == 0
            and g.get("files_skipped") == 2,
            "cap fallbacks remain skipped/noded rather than parse failures",
        )
        check(dt < 5, f"line/symbol cap fallbacks degrade fast ({dt:.2f}s)")


def test_dedicated_contract_formats_are_not_incomplete():
    """A bare main-parser node is normal when a dedicated pass owns the format."""
    with tempfile.TemporaryDirectory(prefix="rob_dedicated_contracts_") as d:
        fixtures = {
            "schema.sql": "CREATE TABLE accounts (id integer);\n",
            "schema.prisma": (
                "model LedgerEntry {\n"
                "  id Int @id\n"
                "}\n"
            ),
            "infra.tf": 'resource "aws_s3_bucket" "logs" {}\n',
            "schema.graphql": "type Viewer { id: ID! }\n",
            "query.gql": "query ViewerQuery { viewer { id } }\n",
            "events.proto": (
                'syntax = "proto3";\n'
                "message AuditEvent { string id = 1; }\n"
                "service AuditService {}\n"
            ),
        }
        for rel, body in fixtures.items():
            with open(os.path.join(d, rel), "w", encoding="utf-8") as fh:
                fh.write(body)

        original_languages = X._ts_languages
        X._ts_languages = lambda: {}
        try:
            g, dt = _build(d)
        finally:
            X._ts_languages = original_languages

        statuses = {
            node.get("path"): node.get("analysis_status")
            for node in g["nodes"]
            if node.get("kind") == "file"
            and node.get("path") in fixtures
        }
        resource_kinds = {
            node.get("kind")
            for node in g["nodes"]
        }
        check(
            statuses == {path: None for path in fixtures},
            "SQL/Prisma/Terraform/GraphQL/gql/protobuf bare main-parser "
            f"nodes stay normal under their dedicated passes ({statuses!r})",
        )
        check(
            {"table", "iac_resource", "api_type", "api_message"}
            <= resource_kinds,
            "dedicated passes still emit schema, IaC and API contract resources "
            f"(kinds={sorted(resource_kinds)!r})",
        )
        check(
            g.get("files_failed") == 0,
            "dedicated contract formats do not fabricate parser failures",
        )
        check(dt < 5, f"dedicated contract passes complete fast ({dt:.2f}s)")


def test_analysis_status_dedup_priority():
    """Node dedup preserves the strongest uncertainty independent of order."""
    base = {
        "id": "partial.py",
        "kind": "file",
        "path": "partial.py",
        "language": "python",
    }
    incomplete_vs_ambiguous = X._assemble_nodes(
        [
            {**base, "analysis_status": "ambiguous"},
            {**base, "analysis_status": "incomplete"},
        ],
        [],
    )
    failed_vs_incomplete = X._assemble_nodes(
        [
            {**base, "analysis_status": "incomplete"},
            {**base, "analysis_status": "failed"},
        ],
        [],
    )
    check(
        incomplete_vs_ambiguous[0].get("analysis_status") == "incomplete"
        and failed_vs_incomplete[0].get("analysis_status") == "failed",
        "analysis-status dedup priority is failed > incomplete > ambiguous > "
        "absent",
    )


def test_edge_dedup_prefers_resolved_evidence():
    """Exact evidence must beat an ambiguous duplicate in either input order."""
    exact = {
        "src": "consumer.py",
        "dst": "target.py",
        "kind": "imports",
        "substrate": "imports",
    }
    ambiguous = {
        **exact,
        "reference_status": "ambiguous",
        "ambiguous_reference": True,
        "ambiguity_key": "target",
    }
    ambiguous_first = X._deduplicate_edges([ambiguous, exact])
    exact_first = X._deduplicate_edges([exact, ambiguous])
    legacy_ambiguous = {
        key: value
        for key, value in ambiguous.items()
        if key != "reference_status"
    }
    legacy_only = X._deduplicate_edges([legacy_ambiguous])
    legacy_first = X._deduplicate_edges([legacy_ambiguous, exact])
    check(
        ambiguous_first == [exact]
        and exact_first == [exact]
        and legacy_first == [exact]
        and legacy_only[0].get("reference_status") == "ambiguous",
        "edge dedup is order-independent and resolved evidence outranks "
        "canonical or legacy ambiguity; legacy-only evidence is normalized "
        f"({ambiguous_first!r}, {exact_first!r}, {legacy_first!r}, "
        f"{legacy_only!r})",
    )


def test_local_zero_candidate_reference_status():
    """Only syntax-proven local imports become explicit unresolved evidence."""
    py_source = {"kind": "file", "path": "pkg/consumer.py"}
    py_edges = [
        {
            "src": "pkg/consumer.py",
            "dst": "./missing/x",
            "kind": "imports",
            "resolution_probe": "member",
        },
        {
            "src": "pkg/consumer.py",
            "dst": "./missing",
            "kind": "imports",
        },
    ]
    missing_relative = X._resolve_imports([py_source], py_edges)
    missing_by_dst = {edge["dst"]: edge for edge in missing_relative}
    check(
        missing_by_dst["./missing"].get("reference_status") == "unresolved"
        and "./missing/x" not in missing_by_dst
        and all(
            "resolution_probe" not in edge
            for edge in missing_relative
        ),
        "zero-candidate Python relative import marks its module root unresolved "
        "and drops the redundant member probe; resolver-only metadata is "
        "stripped",
    )

    resolved_relative = X._resolve_imports(
        [py_source, {"kind": "file", "path": "pkg/missing.py"}],
        py_edges,
    )
    check(
        any(
            edge.get("dst") == "pkg/missing.py"
            and edge.get("reference_status") is None
            for edge in resolved_relative
        )
        and all(
            edge.get("reference_status") != "unresolved"
            for edge in resolved_relative
        ),
        "an exact later Python target removes relative-reference uncertainty",
    )

    web_source = {"kind": "file", "path": "src/consumer.ts"}
    external_and_alias_edges = [
        {"src": "src/consumer.ts", "dst": "@/missing", "kind": "imports"},
        {"src": "src/consumer.ts", "dst": "~/other", "kind": "imports"},
        {"src": "src/consumer.ts", "dst": "@scope/pkg", "kind": "imports"},
        {"src": "src/consumer.ts", "dst": "react", "kind": "imports"},
        {
            "src": "src/consumer.ts",
            "dst": "vendor.package",
            "kind": "imports",
        },
    ]
    missing_aliases = X._resolve_imports(
        [web_source], external_and_alias_edges
    )
    missing_alias_by_dst = {edge["dst"]: edge for edge in missing_aliases}
    check(
        missing_alias_by_dst["@/missing"].get("reference_status")
        == "unresolved"
        and missing_alias_by_dst["~/other"].get("reference_status")
        == "unresolved",
        "zero-candidate @/ and ~/ local aliases are explicit unresolved evidence",
    )
    check(
        all(
            missing_alias_by_dst[dst].get("reference_status") is None
            for dst in ("@scope/pkg", "react", "vendor.package")
        ),
        "ordinary bare/dotted/scoped external packages remain statusless",
    )

    resolved_aliases = X._resolve_imports(
        [
            web_source,
            {"kind": "file", "path": "src/missing.ts"},
            {"kind": "file", "path": "src/other.ts"},
        ],
        external_and_alias_edges,
    )
    check(
        {
            edge.get("dst")
            for edge in resolved_aliases
            if edge.get("reference_status") is None
        }
        >= {"src/missing.ts", "src/other.ts", "@scope/pkg", "react", "vendor.package"}
        and all(
            edge.get("reference_status") != "unresolved"
            for edge in resolved_aliases
        ),
        "exact alias targets resolve normally while external package evidence "
        "stays inert",
    )

    # A resolved sibling is not evidence for a different missing import. This
    # is the Python ImportFrom shape that exposed prefix-family conflation:
    # ``./foo`` is an ancestor of the resolved ``./foo/bar`` and is therefore
    # satisfied, while ``./foo/missing`` is its sibling and must remain an
    # explicit unresolved reference. The symbol tail below the resolved module
    # is inert rather than a second missing file.
    mixed_edges = [
        {
            "src": "pkg/consumer.py",
            "dst": "./foo/missing",
            "kind": "imports",
            "resolution_probe": "member",
        },
        {
            "src": "pkg/consumer.py",
            "dst": "./foo",
            "kind": "imports",
        },
        {
            "src": "pkg/consumer.py",
            "dst": "./foo/bar/thing",
            "kind": "imports",
            "resolution_probe": "member",
        },
        {
            "src": "pkg/consumer.py",
            "dst": "./foo/bar",
            "kind": "imports",
        },
    ]
    mixed = X._resolve_imports(
        [
            py_source,
            {"kind": "file", "path": "pkg/foo/bar.py"},
        ],
        mixed_edges,
    )
    mixed_by_dst = {edge["dst"]: edge for edge in mixed}
    check(
        mixed_by_dst["./foo/missing"].get("reference_status")
        == "unresolved"
        and mixed_by_dst["./foo"].get("reference_status") is None
        and "./foo/bar/thing" not in mixed_by_dst
        and mixed_by_dst["pkg/foo/bar.py"].get("reference_status") is None,
        "resolved ancestor/descendant references do not mask an unresolved "
        "sibling",
    )

    with tempfile.TemporaryDirectory(prefix="rob_relative_mixed_") as d:
        pkg_dir = os.path.join(d, "pkg")
        os.makedirs(os.path.join(pkg_dir, "foo"))
        with open(os.path.join(pkg_dir, "consumer.py"), "w") as fh:
            fh.write(
                "from .foo import missing\n"
                "from .foo.bar import thing\n"
            )
        with open(os.path.join(pkg_dir, "foo", "bar.py"), "w") as fh:
            fh.write("thing = 1\n")
        graph = X.build_graph(d)
        graph_imports = [
            edge
            for edge in graph["edges"]
            if edge.get("kind") == "imports"
            and edge.get("src") == "pkg/consumer.py"
        ]
        graph_by_dst = {edge["dst"]: edge for edge in graph_imports}
        check(
            graph_by_dst["./foo/missing"].get("reference_status")
            == "unresolved"
            and graph_by_dst["./foo"].get("reference_status") is None
            and "./foo/bar/thing" not in graph_by_dst
            and graph_by_dst["pkg/foo/bar.py"].get("reference_status") is None,
            "build_graph preserves unresolved Python siblings while resolving "
            "the concrete relative module",
        )

    alias_family_edges = [
        {"src": "src/consumer.ts", "dst": "@/foo", "kind": "imports"},
        {
            "src": "src/consumer.ts",
            "dst": "@/foo/member",
            "kind": "imports",
            "resolution_probe": "member",
        },
    ]
    resolved_alias_family = X._resolve_imports(
        [web_source, {"kind": "file", "path": "src/foo.ts"}],
        alias_family_edges,
    )
    check(
        any(edge.get("dst") == "src/foo.ts" for edge in resolved_alias_family)
        and all(
            edge.get("reference_status") != "unresolved"
            for edge in resolved_alias_family
        ),
        "a resolved @/ alias module keeps its possible symbol tail statusless",
    )

    web_nested_nodes = [
        web_source,
        {"kind": "file", "path": "src/foo/bar.ts"},
    ]
    web_nested_edges = [
        {"src": "src/consumer.ts", "dst": "./foo", "kind": "imports"},
        {
            "src": "src/consumer.ts",
            "dst": "./foo/bar",
            "kind": "imports",
        },
        {"src": "src/consumer.ts", "dst": "@/other", "kind": "imports"},
        {
            "src": "src/consumer.ts",
            "dst": "@/other/bar",
            "kind": "imports",
        },
    ]
    web_nested = X._resolve_imports(
        web_nested_nodes
        + [{"kind": "file", "path": "src/other/bar.ts"}],
        web_nested_edges,
    )
    web_nested_by_dst = {edge.get("dst"): edge for edge in web_nested}
    check(
        web_nested_by_dst["./foo"].get("reference_status")
        == "unresolved"
        and web_nested_by_dst["@/other"].get("reference_status")
        == "unresolved"
        and web_nested_by_dst["src/foo/bar.ts"].get(
            "reference_status"
        )
        is None
        and web_nested_by_dst["src/other/bar.ts"].get(
            "reference_status"
        )
        is None,
        "a resolved web descendant does not satisfy a separate missing "
        "relative or @/ parent module",
    )

    with tempfile.TemporaryDirectory(prefix="rob_web_nested_import_") as d:
        os.makedirs(os.path.join(d, "src", "foo"))
        with open(os.path.join(d, "src", "consumer.ts"), "w") as fh:
            fh.write(
                "import root from './foo';\n"
                "import bar from './foo/bar';\n"
                "export const value = [root, bar];\n"
            )
        with open(os.path.join(d, "src", "foo", "bar.ts"), "w") as fh:
            fh.write("export default 'bar';\n")
        graph = X.build_graph(d)
        imports = [
            edge
            for edge in graph["edges"]
            if edge.get("kind") == "imports"
            and edge.get("src") == "src/consumer.ts"
        ]
        by_dst = {edge.get("dst"): edge for edge in imports}
        check(
            by_dst["./foo"].get("reference_status") == "unresolved"
            and by_dst["src/foo/bar.ts"].get("reference_status") is None,
            "build_graph keeps a missing web parent import unresolved when "
            "only its descendant module exists",
        )


def test_relative_same_stem_resolution_is_deterministic():
    """Extensionless local imports are family-filtered and order-independent."""
    source = {"kind": "file", "path": "src/consumer.ts"}
    py_target = {"kind": "file", "path": "src/foo.py"}
    ts_target = {"kind": "file", "path": "src/foo.ts"}
    edge = {"src": "src/consumer.ts", "dst": "./foo", "kind": "imports"}
    forward = X._resolve_imports([source, py_target, ts_target], [edge])
    reverse = X._resolve_imports([ts_target, py_target, source], [edge])
    check(
        forward == reverse
        and len(forward) == 1
        and forward[0].get("dst") == "src/foo.ts"
        and forward[0].get("reference_status") is None,
        "foo.py + foo.ts resolves a TypeScript relative import to foo.ts in "
        "either Node order",
    )

    js_target = {"kind": "file", "path": "src/foo.js"}
    web_forward = X._resolve_imports(
        [source, js_target, ts_target],
        [edge],
    )
    web_reverse = X._resolve_imports(
        [ts_target, js_target, source],
        [edge],
    )
    check(
        web_forward == web_reverse
        and [item["dst"] for item in web_forward]
        == ["src/foo.js", "src/foo.ts"]
        and all(
            item.get("reference_status") == "ambiguous"
            for item in web_forward
        ),
        "same-family foo.js + foo.ts ambiguity is explicit, sorted, and "
        "independent of Node order",
    )

    explicit = X._resolve_imports(
        [source, py_target, ts_target],
        [{"src": "src/consumer.ts", "dst": "./foo.py", "kind": "imports"}],
    )
    check(
        len(explicit) == 1
        and explicit[0].get("dst") == "src/foo.py"
        and explicit[0].get("reference_status") is None,
        "an explicit relative extension remains an exact coordinate across "
        "language families",
    )

    rust_source = {"kind": "file", "path": "src/consumer.rs"}
    rust_target = {"kind": "file", "path": "src/foo.rs"}
    go_decoy = {"kind": "file", "path": "src/foo.go"}
    rust_edges = [
        {"src": "src/consumer.rs", "dst": "self::foo", "kind": "imports"},
        {"src": "src/consumer.rs", "dst": "super::foo", "kind": "imports"},
    ]
    rust_forward = X._resolve_imports(
        [rust_source, go_decoy, rust_target],
        rust_edges,
    )
    rust_reverse = X._resolve_imports(
        [rust_target, go_decoy, rust_source],
        rust_edges,
    )
    check(
        rust_forward == rust_reverse
        and len(rust_forward) == 2
        and all(edge.get("dst") == "src/foo.rs" for edge in rust_forward)
        and all(
            edge.get("reference_status") is None for edge in rust_forward
        ),
        "Rust self::/super:: same-stem resolution ignores a .go decoy in "
        "either Node order",
    )

    rust_missing_edges = [
        {
            "src": "src/consumer.rs",
            "dst": "self::missing",
            "kind": "imports",
        },
        {
            "src": "src/consumer.rs",
            "dst": "super::other_missing",
            "kind": "imports",
        },
        {
            "src": "src/consumer.rs",
            "dst": "crate::root_missing",
            "kind": "imports",
        },
        {
            "src": "src/consumer.rs",
            "dst": "serde",
            "kind": "imports",
        },
    ]
    rust_missing = X._resolve_imports(
        [rust_source],
        rust_missing_edges,
    )
    rust_missing_by_dst = {
        edge.get("dst"): edge for edge in rust_missing
    }
    check(
        all(
            rust_missing_by_dst[raw].get("reference_status")
            == "unresolved"
            for raw in (
                "self::missing",
                "super::other_missing",
                "crate::root_missing",
            )
        )
        and rust_missing_by_dst["serde"].get("reference_status") is None,
        "zero-candidate Rust self::/super::/crate:: roots are proven local "
        "unresolved evidence while an external bare crate stays statusless",
    )

    with tempfile.TemporaryDirectory(prefix="rob_rust_root_missing_") as d:
        os.makedirs(os.path.join(d, "src"))
        with open(os.path.join(d, "src", "consumer.rs"), "w") as fh:
            fh.write(
                "use self::missing;\n"
                "use super::other_missing;\n"
                "use crate::root_missing;\n"
                "use serde;\n"
                "fn main() {}\n"
            )
        graph = X.build_graph(d)
        imports = [
            edge
            for edge in graph["edges"]
            if edge.get("kind") == "imports"
            and edge.get("src") == "src/consumer.rs"
        ]
        by_dst = {edge.get("dst"): edge for edge in imports}
        check(
            all(
                by_dst[raw].get("reference_status") == "unresolved"
                for raw in (
                    "self::missing",
                    "super::other_missing",
                    "crate::root_missing",
                )
            )
            and by_dst["serde"].get("reference_status") is None,
            "build_graph preserves Rust rooted misses as unresolved and bare "
            "external crates as inert",
        )


def test_own_package_same_stem_resolution_is_deterministic():
    """Own-package inference cannot be captured by a cross-family same stem."""
    manifest = {"kind": "config_file", "path": "package.json"}
    consumer = {"kind": "file", "path": "src/consumer.ts"}
    entry_py = {"kind": "file", "path": "src/index.py"}
    entry_ts = {"kind": "file", "path": "src/index.ts"}
    sub_ts = {"kind": "file", "path": "src/sub.ts"}
    own_edges = [
        {"src": "src/consumer.ts", "dst": "acme", "kind": "imports"},
        {"src": "src/consumer.ts", "dst": "acme/sub", "kind": "imports"},
    ]
    forward = X._resolve_imports(
        [manifest, consumer, entry_py, entry_ts, sub_ts],
        own_edges,
    )
    reverse = X._resolve_imports(
        [sub_ts, entry_ts, entry_py, consumer, manifest],
        own_edges,
    )
    check(
        forward == reverse
        and {edge.get("dst") for edge in forward}
        == {"src/index.ts", "src/sub.ts"}
        and all(
            edge.get("reference_status") is None for edge in forward
        ),
        "npm own-package entry/subpath resolution keeps the TypeScript "
        "candidates despite same-stem Python decoys and reversed Node order",
    )

    entry_and_missing = X._resolve_imports(
        [manifest, consumer, entry_ts, sub_ts],
        [
            {
                "src": "src/consumer.ts",
                "dst": "acme",
                "kind": "imports",
            },
            {
                "src": "src/consumer.ts",
                "dst": "acme/missing",
                "kind": "imports",
            },
            {
                "src": "src/consumer.ts",
                "dst": "acme/sub",
                "kind": "imports",
            },
            {
                "src": "src/consumer.ts",
                "dst": "acme/value",
                "kind": "imports",
                "resolution_probe": "member",
            },
            {
                "src": "src/consumer.ts",
                "dst": "acme/value",
                "kind": "imports",
            },
        ],
    )
    entry_and_missing_by_dst = {
        edge.get("dst"): edge for edge in entry_and_missing
    }
    check(
        entry_and_missing_by_dst["src/index.ts"].get(
            "reference_status"
        )
        is None
        and entry_and_missing_by_dst["src/sub.ts"].get(
            "reference_status"
        )
        is None
        and entry_and_missing_by_dst["acme/missing"].get(
            "reference_status"
        )
        == "unresolved"
        and entry_and_missing_by_dst["acme/value"].get(
            "reference_status"
        )
        == "unresolved"
        and "resolution_probe"
        not in entry_and_missing_by_dst["acme/value"],
        "a resolved own-package bare entry cannot suppress a separate missing "
        "own subpath; a redundant member probe cannot erase an exact missing "
        "import with the same identity",
    )

    false_external_edges = [
        {"src": "src/consumer.ts", "dst": "react", "kind": "imports"},
        {
            "src": "src/consumer.ts",
            "dst": "react/foo",
            "kind": "imports",
        },
    ]
    false_external = X._resolve_imports(
        [
            manifest,
            consumer,
            entry_ts,
            {"kind": "file", "path": "src/foo.py"},
        ],
        false_external_edges,
    )
    check(
        {edge.get("dst") for edge in false_external}
        == {"react", "react/foo"}
        and all(
            edge.get("reference_status") is None for edge in false_external
        ),
        "a cross-family local subpath cannot corroborate an external package "
        "name or redirect its bare import to the own entry",
    )

    workspace_nodes = [
        {"kind": "config_file", "path": "packages/a/package.json"},
        {"kind": "config_file", "path": "packages/b/package.json"},
        {"kind": "file", "path": "consumer.ts"},
        {"kind": "file", "path": "packages/a/src/index.ts"},
        {"kind": "file", "path": "packages/a/src/sub.ts"},
        {"kind": "file", "path": "packages/b/src/index.ts"},
        {"kind": "file", "path": "packages/b/src/sub.ts"},
    ]
    workspace_edges = [
        {"src": "consumer.ts", "dst": "acme", "kind": "imports"},
        {"src": "consumer.ts", "dst": "acme/sub", "kind": "imports"},
    ]
    workspace_forward = X._resolve_imports(
        workspace_nodes,
        workspace_edges,
    )
    workspace_reverse = X._resolve_imports(
        list(reversed(workspace_nodes)),
        workspace_edges,
    )
    check(
        workspace_forward == workspace_reverse
        and {edge.get("dst") for edge in workspace_forward}
        == {
            "packages/a/src/index.ts",
            "packages/a/src/sub.ts",
            "packages/b/src/index.ts",
            "packages/b/src/sub.ts",
        }
        and all(
            edge.get("reference_status") == "ambiguous"
            for edge in workspace_forward
        ),
        "all npm roots corroborating one inferred own name are merged into "
        "deterministic explicit ambiguity",
    )

    python_nodes = [
        {"kind": "config_file", "path": "pyproject.toml"},
        {"kind": "file", "path": "consumer.py"},
        {"kind": "file", "path": "acme/__init__.py"},
        {"kind": "file", "path": "src/acme/__init__.py"},
    ]
    python_edge = {
        "src": "consumer.py",
        "dst": "acme",
        "kind": "imports",
    }
    python_forward = X._resolve_imports(python_nodes, [python_edge])
    python_reverse = X._resolve_imports(
        list(reversed(python_nodes)),
        [python_edge],
    )
    check(
        python_forward == python_reverse
        and [edge.get("dst") for edge in python_forward]
        == ["acme/__init__.py", "src/acme/__init__.py"]
        and all(
            edge.get("reference_status") == "ambiguous"
            for edge in python_forward
        ),
        "multiple Python package roots merge deterministically instead of "
        "last-Node-wins entry selection",
    )

    python_member_nodes = [
        {"kind": "config_file", "path": "pyproject.toml"},
        {"kind": "file", "path": "consumer.py"},
        {"kind": "file", "path": "acme/__init__.py"},
    ]
    python_member_edges = [
        {"src": "consumer.py", "dst": "acme", "kind": "imports"},
        {
            "src": "consumer.py",
            "dst": "acme.member",
            "kind": "imports",
            "resolution_probe": "member",
        },
        {
            "src": "consumer.py",
            "dst": "acme.missing",
            "kind": "imports",
        },
    ]
    python_members = X._resolve_imports(
        python_member_nodes,
        python_member_edges,
    )
    python_members_by_dst = {
        edge.get("dst"): edge for edge in python_members
    }
    check(
        python_members_by_dst["acme/__init__.py"].get(
            "reference_status"
        )
        is None
        and "acme.member" not in python_members_by_dst
        and python_members_by_dst["acme.missing"].get(
            "reference_status"
        )
        == "unresolved",
        "Python dotted comparison keys drop only an ImportFrom member probe "
        "beneath a resolved package; a separate dotted module miss remains "
        "unresolved",
    )

    polyglot_nodes = [
        {"kind": "config_file", "path": "package.json"},
        {"kind": "config_file", "path": "pyproject.toml"},
        {"kind": "file", "path": "src/consumer.ts"},
        {"kind": "file", "path": "src/index.ts"},
        {"kind": "file", "path": "src/sub.ts"},
        {"kind": "file", "path": "src/acme/__init__.py"},
    ]
    polyglot_forward = X._resolve_imports(polyglot_nodes, own_edges)
    polyglot_reverse = X._resolve_imports(
        list(reversed(polyglot_nodes)),
        own_edges,
    )
    check(
        polyglot_forward == polyglot_reverse
        and {edge.get("dst") for edge in polyglot_forward}
        == {"src/index.ts", "src/sub.ts"}
        and all(
            edge.get("reference_status") is None
            for edge in polyglot_forward
        ),
        "a Python package record and npm package record with the same logical "
        "name merge; a TypeScript importer selects only web-family targets",
    )

    no_entry_nodes = [
        manifest,
        consumer,
        sub_ts,
        {"kind": "file", "path": "tests/acme.ts"},
        {
            "kind": "file",
            "path": "vendor/acme/missing.ts",
        },
    ]
    no_entry_edges = [
        {"src": "src/consumer.ts", "dst": "acme", "kind": "imports"},
        {
            "src": "src/consumer.ts",
            "dst": "acme/sub",
            "kind": "imports",
        },
        {
            "src": "src/consumer.ts",
            "dst": "acme/missing",
            "kind": "imports",
        },
    ]
    no_entry = X._resolve_imports(no_entry_nodes, no_entry_edges)
    no_entry_by_dst = {edge.get("dst"): edge for edge in no_entry}
    check(
        no_entry_by_dst["acme"].get("reference_status") == "unresolved"
        and no_entry_by_dst["acme/missing"].get("reference_status")
        == "unresolved"
        and no_entry_by_dst["src/sub.ts"].get("reference_status") is None
        and "tests/acme.ts" not in no_entry_by_dst
        and "vendor/acme/missing.ts" not in no_entry_by_dst,
        "known npm package bare/no-entry and missing-subpath references stay "
        "strictly unresolved while a valid subpath resolves, with no generic "
        "basename/suffix fallthrough",
    )

    undeclared_scoped_edge = {
        "src": "src/consumer.ts",
        "dst": "@org/lib/x",
        "kind": "imports",
    }
    undeclared_scoped = X._resolve_imports(
        [
            consumer,
            {
                "kind": "file",
                "path": "vendor/org/lib/x.ts",
            },
        ],
        [undeclared_scoped_edge],
    )
    check(
        len(undeclared_scoped) == 1
        and undeclared_scoped[0].get("dst")
        == "vendor/org/lib/x.ts"
        and undeclared_scoped[0].get("reference_status") == "ambiguous",
        "an undeclared scoped package with one coincident local suffix is "
        "explicit ambiguity, never a confident local resolution",
    )

    with tempfile.TemporaryDirectory(prefix="rob_own_package_stem_") as d:
        src_dir = os.path.join(d, "src")
        os.makedirs(src_dir)
        with open(os.path.join(d, "package.json"), "w") as fh:
            fh.write('{"name":"acme","version":"1.0.0"}\n')
        with open(os.path.join(src_dir, "consumer.ts"), "w") as fh:
            fh.write(
                "import acme from 'acme';\n"
                "import { value } from 'acme';\n"
                "import exactValue from 'acme/value';\n"
                "import { sub } from 'acme/sub';\n"
                "export const result = [acme, value, exactValue, sub];\n"
            )
        with open(os.path.join(src_dir, "index.py"), "w") as fh:
            fh.write("VALUE = 'python decoy'\n")
        with open(os.path.join(src_dir, "index.ts"), "w") as fh:
            fh.write(
                "export default 'typescript';\n"
                "export const value = 'member';\n"
            )
        with open(os.path.join(src_dir, "sub.ts"), "w") as fh:
            fh.write("export const sub = 'typescript';\n")
        graph = X.build_graph(d)
        imports = [
            edge
            for edge in graph["edges"]
            if edge.get("kind") == "imports"
            and edge.get("src") == "src/consumer.ts"
        ]
        check(
            {edge.get("dst") for edge in imports}
            >= {"src/index.ts", "src/sub.ts"}
            and not any(
                edge.get("dst") == "src/index.py" for edge in imports
            )
            and len(
                [
                    edge
                    for edge in imports
                    if edge.get("dst") == "acme/value"
                    and edge.get("reference_status") == "unresolved"
                ]
            )
            == 1
            and all("resolution_probe" not in edge for edge in imports),
            "build_graph resolves a real npm own package to its web-family "
            "entry and subpath, never the Python same-stem entry",
        )

    with tempfile.TemporaryDirectory(prefix="rob_external_package_stem_") as d:
        src_dir = os.path.join(d, "src")
        os.makedirs(src_dir)
        with open(os.path.join(d, "package.json"), "w") as fh:
            fh.write('{"name":"acme","version":"1.0.0"}\n')
        with open(os.path.join(src_dir, "consumer.ts"), "w") as fh:
            fh.write(
                "import React from 'react';\n"
                "import thing from 'react/foo';\n"
                "export const value = [React, thing];\n"
            )
        with open(os.path.join(src_dir, "index.ts"), "w") as fh:
            fh.write("export default 'acme';\n")
        with open(os.path.join(src_dir, "foo.py"), "w") as fh:
            fh.write("VALUE = 'python decoy'\n")
        graph = X.build_graph(d)
        imports = [
            edge
            for edge in graph["edges"]
            if edge.get("kind") == "imports"
            and edge.get("src") == "src/consumer.ts"
        ]
        check(
            {edge.get("dst") for edge in imports}
            >= {"react", "react/foo"}
            and not any(
                edge.get("dst") == "src/index.ts" for edge in imports
            ),
            "build_graph keeps external react references inert when only a "
            "cross-family local subpath exists",
        )

    with tempfile.TemporaryDirectory(prefix="rob_own_workspace_merge_") as d:
        with open(os.path.join(d, "package.json"), "w") as fh:
            fh.write('{"private":true,"workspaces":["packages/*"]}\n')
        with open(os.path.join(d, "consumer.ts"), "w") as fh:
            fh.write(
                "import acme from 'acme';\n"
                "import { sub } from 'acme/sub';\n"
                "export const value = [acme, sub];\n"
            )
        for member in ("a", "b"):
            member_src = os.path.join(d, "packages", member, "src")
            os.makedirs(member_src)
            with open(
                os.path.join(d, "packages", member, "package.json"),
                "w",
            ) as fh:
                fh.write(
                    '{"name":"acme","version":"1.0.0"}\n'
                )
            with open(os.path.join(member_src, "index.ts"), "w") as fh:
                fh.write(f"export default '{member}';\n")
            with open(os.path.join(member_src, "sub.ts"), "w") as fh:
                fh.write(f"export const sub = '{member}';\n")
        graph = X.build_graph(d)
        imports = [
            edge
            for edge in graph["edges"]
            if edge.get("kind") == "imports"
            and edge.get("src") == "consumer.ts"
            and edge.get("dst", "").startswith("packages/")
        ]
        check(
            {edge.get("dst") for edge in imports}
            == {
                "packages/a/src/index.ts",
                "packages/a/src/sub.ts",
                "packages/b/src/index.ts",
                "packages/b/src/sub.ts",
            }
            and all(
                edge.get("reference_status") == "ambiguous"
                for edge in imports
            ),
            "build_graph retains every corroborating npm workspace target as "
            "explicit ambiguity",
        )

    with tempfile.TemporaryDirectory(prefix="rob_python_package_merge_") as d:
        with open(os.path.join(d, "pyproject.toml"), "w") as fh:
            fh.write("[project]\nname = 'acme'\nversion = '1.0.0'\n")
        with open(os.path.join(d, "consumer.py"), "w") as fh:
            fh.write("import acme\n")
        for rel in ("acme", "src/acme"):
            package_dir = os.path.join(d, rel)
            os.makedirs(package_dir)
            with open(os.path.join(package_dir, "__init__.py"), "w") as fh:
                fh.write("VALUE = 1\n")
        graph = X.build_graph(d)
        imports = [
            edge
            for edge in graph["edges"]
            if edge.get("kind") == "imports"
            and edge.get("src") == "consumer.py"
        ]
        check(
            [edge.get("dst") for edge in imports]
            == ["acme/__init__.py", "src/acme/__init__.py"]
            and all(
                edge.get("reference_status") == "ambiguous"
                for edge in imports
            ),
            "build_graph merges multiple Python package entries into "
            "deterministic explicit ambiguity",
        )

    with tempfile.TemporaryDirectory(prefix="rob_python_member_probe_") as d:
        os.makedirs(os.path.join(d, "acme"))
        with open(os.path.join(d, "pyproject.toml"), "w") as fh:
            fh.write("[project]\nname = 'acme'\nversion = '1.0.0'\n")
        with open(os.path.join(d, "consumer.py"), "w") as fh:
            fh.write(
                "from acme import member\n"
                "import acme.missing\n"
            )
        with open(os.path.join(d, "acme", "__init__.py"), "w") as fh:
            fh.write("member = 1\n")
        graph = X.build_graph(d)
        imports = [
            edge
            for edge in graph["edges"]
            if edge.get("kind") == "imports"
            and edge.get("src") == "consumer.py"
        ]
        by_dst = {edge.get("dst"): edge for edge in imports}
        check(
            by_dst["acme/__init__.py"].get("reference_status") is None
            and "acme.member" not in by_dst
            and by_dst["acme.missing"].get("reference_status")
            == "unresolved",
            "build_graph uses transient dotted Python probe comparison "
            "without mutating raw evidence or hiding a separate missing "
            "module",
        )

    with tempfile.TemporaryDirectory(prefix="rob_polyglot_package_merge_") as d:
        src_dir = os.path.join(d, "src")
        os.makedirs(os.path.join(src_dir, "acme"))
        with open(os.path.join(d, "package.json"), "w") as fh:
            fh.write('{"name":"acme","version":"1.0.0"}\n')
        with open(os.path.join(d, "pyproject.toml"), "w") as fh:
            fh.write("[project]\nname = 'acme'\nversion = '1.0.0'\n")
        with open(os.path.join(src_dir, "consumer.ts"), "w") as fh:
            fh.write(
                "import acme from 'acme';\n"
                "import { sub } from 'acme/sub';\n"
                "export const value = [acme, sub];\n"
            )
        with open(os.path.join(src_dir, "index.ts"), "w") as fh:
            fh.write("export default 'typescript';\n")
        with open(os.path.join(src_dir, "sub.ts"), "w") as fh:
            fh.write("export const sub = 'typescript';\n")
        with open(
            os.path.join(src_dir, "acme", "__init__.py"),
            "w",
        ) as fh:
            fh.write("VALUE = 'python'\n")
        graph = X.build_graph(d)
        imports = [
            edge
            for edge in graph["edges"]
            if edge.get("kind") == "imports"
            and edge.get("src") == "src/consumer.ts"
        ]
        check(
            {edge.get("dst") for edge in imports}
            >= {"src/index.ts", "src/sub.ts"}
            and not any(
                edge.get("dst") == "src/acme/__init__.py"
                for edge in imports
            ),
            "build_graph resolves a polyglot shared package name by importer "
            "family instead of globally suppressing npm behind Python",
        )

    with tempfile.TemporaryDirectory(prefix="rob_own_no_entry_") as d:
        src_dir = os.path.join(d, "src")
        os.makedirs(src_dir)
        os.makedirs(os.path.join(d, "tests"))
        os.makedirs(os.path.join(d, "vendor", "acme"))
        with open(os.path.join(d, "package.json"), "w") as fh:
            fh.write('{"name":"acme","version":"1.0.0"}\n')
        with open(os.path.join(src_dir, "consumer.ts"), "w") as fh:
            fh.write(
                "import acme from 'acme';\n"
                "import { sub } from 'acme/sub';\n"
                "import missing from 'acme/missing';\n"
                "export const value = [acme, sub, missing];\n"
            )
        with open(os.path.join(src_dir, "sub.ts"), "w") as fh:
            fh.write("export const sub = 'typescript';\n")
        with open(os.path.join(d, "tests", "acme.ts"), "w") as fh:
            fh.write("export default 'decoy';\n")
        with open(
            os.path.join(d, "vendor", "acme", "missing.ts"),
            "w",
        ) as fh:
            fh.write("export default 'decoy';\n")
        graph = X.build_graph(d)
        imports = [
            edge
            for edge in graph["edges"]
            if edge.get("kind") == "imports"
            and edge.get("src") == "src/consumer.ts"
        ]
        by_dst = {edge.get("dst"): edge for edge in imports}
        check(
            by_dst["acme"].get("reference_status") == "unresolved"
            and by_dst["acme/missing"].get("reference_status")
            == "unresolved"
            and by_dst["src/sub.ts"].get("reference_status") is None
            and "tests/acme.ts" not in by_dst
            and "vendor/acme/missing.ts" not in by_dst
            and graph.get("metrics", {}).get(
                "unresolved_reference_count", 0
            ) >= 2,
            "build_graph records known own-package no-entry/missing-subpath "
            "evidence as unresolved and exposes it in observability",
        )

    with tempfile.TemporaryDirectory(prefix="rob_scoped_no_entry_") as d:
        member_src = os.path.join(d, "packages", "lib", "src")
        os.makedirs(member_src)
        os.makedirs(os.path.join(d, "vendor", "org", "lib"))
        with open(
            os.path.join(d, "packages", "lib", "package.json"),
            "w",
        ) as fh:
            fh.write('{"name":"@org/lib","version":"1.0.0"}\n')
        with open(os.path.join(d, "consumer.ts"), "w") as fh:
            fh.write(
                "import lib from '@org/lib';\n"
                "import { sub } from '@org/lib/sub';\n"
                "import missing from '@org/lib/missing';\n"
                "export const value = [lib, sub, missing];\n"
            )
        with open(os.path.join(member_src, "sub.ts"), "w") as fh:
            fh.write("export const sub = 'typescript';\n")
        with open(
            os.path.join(d, "vendor", "org", "lib", "missing.ts"),
            "w",
        ) as fh:
            fh.write("export default 'decoy';\n")
        graph = X.build_graph(d)
        imports = [
            edge
            for edge in graph["edges"]
            if edge.get("kind") == "imports"
            and edge.get("src") == "consumer.ts"
        ]
        by_dst = {edge.get("dst"): edge for edge in imports}
        check(
            by_dst["@org/lib"].get("reference_status") == "unresolved"
            and by_dst["@org/lib/missing"].get("reference_status")
            == "unresolved"
            and by_dst["packages/lib/src/sub.ts"].get(
                "reference_status"
            )
            is None
            and "vendor/org/lib/missing.ts" not in by_dst,
            "declared scoped workspace no-entry/missing-subpath references "
            "are unresolved local evidence with no generic suffix fallthrough",
        )
        with open(os.path.join(member_src, "index.ts"), "w") as fh:
            fh.write("export default 'lib';\n")
        graph_with_entry = X.build_graph(d)
        imports_with_entry = [
            edge
            for edge in graph_with_entry["edges"]
            if edge.get("kind") == "imports"
            and edge.get("src") == "consumer.ts"
        ]
        entry_by_dst = {
            edge.get("dst"): edge for edge in imports_with_entry
        }
        check(
            entry_by_dst["packages/lib/src/index.ts"].get(
                "reference_status"
            )
            is None
            and entry_by_dst["@org/lib/missing"].get(
                "reference_status"
            )
            == "unresolved",
            "a resolved scoped workspace entry cannot suppress a separate "
            "missing scoped subpath",
        )


def test_giant_gitattributes_bounded():
    """A .gitattributes file exceeding _GITATTRIBUTES_SIZE_CAP (1 MB) must be skipped entirely —
    not read, not parsed. Before this fix the file was read in full + split into lines, causing
    a 20 MB .gitattributes to take ~16 s just for the gitattributes walk (called 2x per
    build_graph run = ~32 s total). After the fix: the size check fires and the walk moves on.

    Correctness check: a real (small) .gitattributes with linguist-generated rules is STILL
    honored (the cap only skips degenerate/adversarial files, not real ones)."""
    d = tempfile.mkdtemp(prefix="rob_gitattr_")
    with open(os.path.join(d, ".gitattributes"), "w") as fh:
        # ~21 MB .gitattributes (500 k rules) — degenerate / adversarial
        for i in range(500_000):
            fh.write(f"path/to/file{i}.pb.go linguist-generated\n")
    sz = os.path.getsize(os.path.join(d, ".gitattributes"))
    with open(os.path.join(d, "a.py"), "w") as fh:
        fh.write("def f(): pass\n")
    check(sz > X._GITATTRIBUTES_SIZE_CAP,
          f"fixture exceeds _GITATTRIBUTES_SIZE_CAP ({sz} > {X._GITATTRIBUTES_SIZE_CAP})")
    g, dt = _build(d)
    check(dt < 2,
          f"giant .gitattributes skipped fast ({dt:.2f}s — was ~32s before fix for 2 walks)")
    # a.py is still found (the .gitattributes being skipped doesn't kill the whole walk)
    check(any(n["path"] == "a.py" for n in g["nodes"]),
          "giant .gitattributes skipped but normal source file still extracted")

    # CORRECTNESS: a normal .gitattributes with a real linguist-generated rule is STILL honored.
    d2 = tempfile.mkdtemp(prefix="rob_gitattr_ok_")
    with open(os.path.join(d2, ".gitattributes"), "w") as fh:
        fh.write("generated.pb.go linguist-generated\n")
    with open(os.path.join(d2, "generated.pb.go"), "w") as fh:
        fh.write("package p\nfunc G() {}\n")
    with open(os.path.join(d2, "real.go"), "w") as fh:
        fh.write("package p\nfunc R() {}\n")
    g2, _ = _build(d2)
    paths2 = {n["path"] for n in g2["nodes"]}
    check("real.go" in paths2, "normal .gitattributes: non-generated file still included")
    check("generated.pb.go" not in paths2,
          "normal .gitattributes: linguist-generated file excluded as expected")


def test_symlinked_gitattributes_is_ignored():
    """A repo-controlled `.gitattributes` symlink must never import policy from
    outside the repository or become persisted reconstruction context."""
    with tempfile.TemporaryDirectory(prefix="rob_gitattr_repo_") as repo:
        with tempfile.TemporaryDirectory(prefix="rob_gitattr_external_") as external:
            external_rules = os.path.join(external, "rules")
            with open(external_rules, "w") as fh:
                fh.write("kept.py linguist-generated\n")
            os.symlink(external_rules, os.path.join(repo, ".gitattributes"))
            with open(os.path.join(repo, "kept.py"), "w") as fh:
                fh.write("def retained():\n    return True\n")

            matchers, attribute_files = (
                X._gitattributes_generated_inventory(repo)
            )
            graph, _elapsed = _build(repo)
            paths = {
                node.get("path")
                for node in graph["nodes"]
                if node.get("path")
            }
            check(
                matchers == [] and attribute_files == [],
                "symlinked .gitattributes contributes no external matcher "
                "or persisted context file",
            )
            check(
                "kept.py" in paths,
                "external symlink target cannot mark an in-repo source generated",
            )
            check(
                ".gitattributes" not in paths,
                "symlinked .gitattributes is not persisted as config_file",
            )
            check(
                graph.get("metrics", {}).get("input_file_count") == 1,
                "symlinked .gitattributes is absent from input observability",
            )


def test_gitattributes_attribute_state_is_independent():
    """Generated and vendored are independent Git attribute state machines.

    A rule for one attribute must never erase the other, and ``!attr`` restores
    the unspecified state (therefore restoring the generated-name heuristic).
    """
    check(
        X._is_generated(
            "src/cross.gen.ts",
            [
                ("", "src/cross.gen.ts", "linguist-generated", True),
                ("", "src/cross.gen.ts", "linguist-vendored", False),
            ],
        ),
        "gitattributes: -vendored does not erase generated=set",
    )
    check(
        X._is_generated(
            "src/heuristic.gen.ts",
            [
                (
                    "",
                    "src/heuristic.gen.ts",
                    "linguist-vendored",
                    False,
                )
            ],
        ),
        "gitattributes: -vendored alone does not suppress generated-name heuristic",
    )
    check(
        not X._is_generated(
            "src/carveout.gen.ts",
            [
                (
                    "",
                    "src/carveout.gen.ts",
                    "linguist-generated",
                    False,
                )
            ],
        ),
        "gitattributes: -generated explicitly carves out generated-name heuristic",
    )
    check(
        X._is_generated(
            "src/reset.gen.ts",
            [
                ("", "src/reset.gen.ts", "linguist-generated", False),
                ("", "src/reset.gen.ts", "linguist-generated", None),
            ],
        ),
        "gitattributes: !generated restores unspecified state and heuristic",
    )
    check(
        not X._is_generated(
            "src/ordinary.ts",
            [
                ("", "src/ordinary.ts", "linguist-generated", True),
                ("", "src/ordinary.ts", "linguist-generated", None),
            ],
        ),
        "gitattributes: !generated clears an earlier set on a non-heuristic path",
    )
    check(
        X._is_generated(
            "src/vendored.gen.ts",
            [
                ("", "src/vendored.gen.ts", "linguist-vendored", True),
                ("", "src/vendored.gen.ts", "linguist-generated", False),
            ],
        ),
        "gitattributes: vendored=set remains exclusion despite -generated",
    )


def test_gitattributes_git_pattern_oracle():
    """Quoted patterns and bracket negation must agree with ``git check-attr``.

    Git is only an optional test oracle; production parsing remains a bounded
    pure-Python scan.  The fixed expected map always runs so a minimal runtime
    without Git still locks the contract.
    """
    body = (
        '"src/space file.py" linguist-generated\n'
        '"src/escaped\\\\ file.py" linguist-vendored\n'
        "src/bang[!0].py linguist-generated\n"
        "src/caret[^0].py linguist-vendored\n"
        "src/range[0-2].py linguist-generated\n"
        "src/carve.gen.ts -linguist-generated\n"
        "src/reset.ts linguist-generated\n"
        "src/reset.ts !linguist-generated\n"
        # An unquoted backslash does not quote the field-separating space in
        # an attributes file. Git and the bounded parser both leave this path
        # unspecified; spaces must use a C-quoted pattern.
        "src/plain\\ space.py linguist-generated\n"
        "/anchored.py linguist-generated\n"
        "glob/src/**.py linguist-vendored\n"
        "pathological/foo//bar linguist-generated\n"
        "**/leading.match linguist-generated\n"
        "middle/**/leaf.match linguist-vendored\n"
        "trailing/** linguist-generated\n"
        "triple/***.js linguist-vendored\n"
        "!forbidden.py linguist-generated\n"
        "\\!literal.py linguist-vendored\n"
        '"!quoted-forbidden.py" linguist-generated\n'
        '"\\\\!quoted-literal.py" linguist-vendored\n'
        "posix/d[[:digit:]].py linguist-generated\n"
        "posix/a[[:alpha:]].py linguist-vendored\n"
        "posix/s[[:space:]].py linguist-generated\n"
        "posix/n[![:digit:]].py linguist-vendored\n"
        "posix/alnum-[[:alnum:]].x linguist-generated\n"
        "posix/blank-[[:blank:]].x linguist-vendored\n"
        "posix/cntrl-[[:cntrl:]].x linguist-generated\n"
        "posix/graph-[[:graph:]].x linguist-vendored\n"
        "posix/lower-[[:lower:]].x linguist-generated\n"
        "posix/print-[[:print:]].x linguist-vendored\n"
        "posix/punct-[[:punct:]].x linguist-generated\n"
        "posix/upper-[[:upper:]].x linguist-vendored\n"
        "posix/xdigit-[[:xdigit:]].x linguist-generated\n"
    )
    expected = {
        ("src/space file.py", "linguist-generated"): "set",
        ("src/space file.py", "linguist-vendored"): "unspecified",
        ("src/spaceXfile.py", "linguist-generated"): "unspecified",
        ("src/escaped file.py", "linguist-generated"): "unspecified",
        ("src/escaped file.py", "linguist-vendored"): "set",
        ("src/escaped\\ file.py", "linguist-vendored"): "unspecified",
        ("src/bang1.py", "linguist-generated"): "set",
        ("src/bang0.py", "linguist-generated"): "unspecified",
        ("src/bang!.py", "linguist-generated"): "set",
        ("src/caret1.py", "linguist-vendored"): "set",
        ("src/caret0.py", "linguist-vendored"): "unspecified",
        ("src/caret^.py", "linguist-vendored"): "set",
        ("src/range1.py", "linguist-generated"): "set",
        ("src/range8.py", "linguist-generated"): "unspecified",
        ("src/carve.gen.ts", "linguist-generated"): "unset",
        ("src/reset.ts", "linguist-generated"): "unspecified",
        ("src/plain space.py", "linguist-generated"): "unspecified",
        ("anchored.py", "linguist-generated"): "set",
        ("d/anchored.py", "linguist-generated"): "unspecified",
        ("glob/src/x.py", "linguist-vendored"): "set",
        ("glob/src/d/x.py", "linguist-vendored"): "unspecified",
        ("pathological/foo/bar", "linguist-generated"): "unspecified",
        ("pathological/foo//bar", "linguist-generated"): "unspecified",
        ("leading.match", "linguist-generated"): "set",
        ("d/leading.match", "linguist-generated"): "set",
        ("middle/leaf.match", "linguist-vendored"): "set",
        ("middle/d/leaf.match", "linguist-vendored"): "set",
        ("trailing/x", "linguist-generated"): "set",
        ("trailing/d/x", "linguist-generated"): "set",
        ("trailing", "linguist-generated"): "unspecified",
        ("triple/x.js", "linguist-vendored"): "set",
        ("triple/d/x.js", "linguist-vendored"): "unspecified",
        ("!forbidden.py", "linguist-generated"): "unspecified",
        ("!literal.py", "linguist-vendored"): "set",
        ("!quoted-forbidden.py", "linguist-generated"): "unspecified",
        ("!quoted-literal.py", "linguist-vendored"): "set",
        ("posix/d7.py", "linguist-generated"): "set",
        ("posix/da.py", "linguist-generated"): "unspecified",
        ("posix/aZ.py", "linguist-vendored"): "set",
        ("posix/a7.py", "linguist-vendored"): "unspecified",
        ("posix/s .py", "linguist-generated"): "set",
        ("posix/sX.py", "linguist-generated"): "unspecified",
        ("posix/nA.py", "linguist-vendored"): "set",
        ("posix/n7.py", "linguist-vendored"): "unspecified",
        ("posix/alnum-A.x", "linguist-generated"): "set",
        ("posix/alnum-_.x", "linguist-generated"): "unspecified",
        ("posix/blank-\t.x", "linguist-vendored"): "set",
        ("posix/blank-A.x", "linguist-vendored"): "unspecified",
        ("posix/cntrl-\t.x", "linguist-generated"): "set",
        ("posix/cntrl-A.x", "linguist-generated"): "unspecified",
        ("posix/graph-!.x", "linguist-vendored"): "set",
        ("posix/graph- .x", "linguist-vendored"): "unspecified",
        ("posix/lower-a.x", "linguist-generated"): "set",
        ("posix/lower-A.x", "linguist-generated"): "unspecified",
        ("posix/print- .x", "linguist-vendored"): "set",
        ("posix/print-\t.x", "linguist-vendored"): "unspecified",
        ("posix/punct-!.x", "linguist-generated"): "set",
        ("posix/punct-A.x", "linguist-generated"): "unspecified",
        ("posix/upper-A.x", "linguist-vendored"): "set",
        ("posix/upper-a.x", "linguist-vendored"): "unspecified",
        ("posix/xdigit-f.x", "linguist-generated"): "set",
        ("posix/xdigit-g.x", "linguist-generated"): "unspecified",
    }
    paths = list(dict.fromkeys(path for path, _attribute in expected))
    attributes = ("linguist-generated", "linguist-vendored")
    state_label = {True: "set", False: "unset", None: "unspecified"}

    with tempfile.TemporaryDirectory(prefix="rob_gitattr_oracle_") as repo:
        attributes_path = os.path.join(repo, ".gitattributes")
        with open(attributes_path, "w", encoding="utf-8") as fh:
            fh.write(body)
        matchers, attribute_files = X._gitattributes_generated_inventory(repo)

        ours = {}
        for path in paths:
            for attribute in attributes:
                state = None
                for base, pattern, name, candidate_state in matchers:
                    if (
                        name == attribute
                        and X._matches_gitattr(path, base, pattern)
                    ):
                        state = candidate_state
                ours[(path, attribute)] = state_label[state]

        pure_mismatches = {
            key: (ours.get(key), wanted)
            for key, wanted in expected.items()
            if ours.get(key) != wanted
        }
        check(
            attribute_files == [attributes_path],
            "gitattributes syntax fixture is accepted as persisted context",
        )
        check(
            not pure_mismatches,
            "gitattributes quoted/escaped-space, [!x]/[^x], range and "
            f"state semantics match fixed expectations ({pure_mismatches})",
        )
        check(
            X._is_generated("src/space file.py", matchers)
            and X._is_generated("src/escaped file.py", matchers)
            and not X._is_generated("src/carve.gen.ts", matchers)
            and not X._is_generated("src/plain space.py", matchers),
            "gitattributes syntax flows through the generated-file predicate",
        )
        check(
            not X._matches_gitattr(
                "posix/unknown-A.x",
                "",
                "posix/unknown-[[:emoji:]].x",
            ),
            "unknown POSIX bracket class fails closed (never a false match)",
        )

        git = shutil.which("git")
        if git is None:
            check(
                True,
                "git check-attr unavailable; fixed pure-Python expectations "
                "remain enforced",
            )
            return

        git_env = os.environ.copy()
        git_env["GIT_CONFIG_NOSYSTEM"] = "1"
        git_env["GIT_CONFIG_GLOBAL"] = os.devnull
        initialized = subprocess.run(
            [git, "init", "-q", repo],
            env=git_env,
            capture_output=True,
        )
        oracle = subprocess.run(
            [
                git,
                "-C",
                repo,
                "-c",
                "core.ignoreCase=false",
                "check-attr",
                "-z",
                "--stdin",
                *attributes,
            ],
            input=("\0".join(paths) + "\0").encode("utf-8"),
            env=git_env,
            capture_output=True,
        )
        fields = oracle.stdout.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        oracle_states = {}
        if len(fields) % 3 == 0:
            for offset in range(0, len(fields), 3):
                path, attribute, value = (
                    field.decode("utf-8") for field in fields[offset:offset + 3]
                )
                oracle_states[(path, attribute)] = value
        oracle_mismatches = {
            key: (ours.get(key), oracle_states.get(key))
            for key in expected
            if ours.get(key) != oracle_states.get(key)
        }
        warning_lines = [
            line
            for line in oracle.stderr.decode("utf-8", "replace").splitlines()
            if line
        ]
        warnings_are_only_negative_pattern_notice = all(
            "Negative patterns are ignored in git attributes" in line
            or "for literal leading exclamation" in line
            for line in warning_lines
        )
        check(
            initialized.returncode == 0
            and oracle.returncode == 0
            and warnings_are_only_negative_pattern_notice
            and not oracle_mismatches,
            "bounded parser/matcher agrees with hermetic git check-attr "
            f"oracle ({oracle_mismatches}; stderr={oracle.stderr[:160]!r})",
        )


def test_sql_file_node_dedup():
    """A .sql file previously received TWO `kind='file'` nodes: one from _iter_source_files
    (language='unknown') and one from _schema_graph (language='sql'). This caused duplicate ids
    in the DB ingest. After the fix: exactly one file node per .sql, with the correct language
    ('sql') AND the content_hash from the source-pass node preserved."""
    d = tempfile.mkdtemp(prefix="rob_sqldup_")
    with open(os.path.join(d, "schema.sql"), "w") as fh:
        fh.write("CREATE TABLE orders (id int);\nCREATE TABLE users (id int);\n")
    g, dt = _build(d)

    # exactly one file node for schema.sql
    file_nodes = [n for n in g["nodes"] if n.get("id") == "schema.sql" and n.get("kind") == "file"]
    check(len(file_nodes) == 1,
          f"SQL file node dedup: exactly 1 file node (was 2 before fix, got {len(file_nodes)})")

    if file_nodes:
        fn = file_nodes[0]
        check(fn.get("language") == "sql",
              f"SQL file node: language='sql' preserved (got '{fn.get('language')}')")
        check("content_hash" in fn and fn["content_hash"] is not None,
              f"SQL file node: content_hash preserved from source pass (got {fn.get('content_hash')})")

    # table nodes from schema pass are ALSO present (dedup only affects file nodes)
    tables = {n["name"] for n in g["nodes"] if n.get("kind") == "table"}
    check("orders" in tables and "users" in tables,
          f"SQL file node dedup: schema table nodes still present ({sorted(tables)})")

    # overall uniqueness: no duplicate ids at all
    from collections import Counter
    by_id = Counter(n["id"] for n in g["nodes"])
    dups = {k: v for k, v in by_id.items() if v > 1}
    check(not dups, f"no duplicate node ids in graph after dedup (dups={dups})")

    check(dt < 5, f"SQL dedup: fast ({dt:.2f}s)")


def test_gitattributes_walk_single():
    """_gitattributes_generated_matchers is now computed ONCE in build_graph and passed to both
    _iter_source_files and _iter_config_files, instead of each computing it independently. This
    reduces the walk from 3× to 1× per build_graph call. Verify: the function signature accepts
    attr_matchers= and the output is identical to the direct-walk result."""
    d = tempfile.mkdtemp(prefix="rob_once_")
    with open(os.path.join(d, "a.py"), "w") as fh:
        fh.write("def f(): pass\n")
    with open(os.path.join(d, ".gitattributes"), "w") as fh:
        fh.write("generated.pb.go linguist-generated\n")
    # _iter_source_files accepts attr_matchers= (pre-computed) → same result as without it
    matchers = X._gitattributes_generated_matchers(d)
    result_precomputed = list(X._iter_source_files(d, attr_matchers=matchers))
    result_standalone  = list(X._iter_source_files(d))
    check(len(result_precomputed) == len(result_standalone),
          f"_iter_source_files with pre-computed matchers == standalone ({len(result_precomputed)} files)")
    check(sorted(p for p, _ in result_precomputed) == sorted(p for p, _ in result_standalone),
          f"_iter_source_files: same files yielded in both modes")
    # same for _iter_config_files
    cfg_pre = list(X._iter_config_files(d, attr_matchers=matchers))
    cfg_std = list(X._iter_config_files(d))
    check(len(cfg_pre) == len(cfg_std),
          f"_iter_config_files with pre-computed matchers == standalone ({len(cfg_pre)} files)")


def test_pathological_sql_content():
    """A .sql file with a very large number of CREATE TABLE statements (5 000 tables, under the
    size cap). The schema pass has its own _MAX_TABLES cap; this test confirms (a) build_graph
    returns without crashing, (b) the table count is bounded by _MAX_TABLES, and (c) the file
    node is not duplicated."""
    import _cg_schema
    max_tables = getattr(_cg_schema, "_MAX_TABLES", None)
    d = tempfile.mkdtemp(prefix="rob_sql_flood_")
    with open(os.path.join(d, "schema.sql"), "w") as fh:
        for i in range(5_000):
            fh.write(f"CREATE TABLE t{i} (id int, val varchar(255));\n")
    g, dt = _build(d)
    tables = [n for n in g["nodes"] if n.get("kind") == "table"]
    file_nodes = [n for n in g["nodes"] if n.get("id") == "schema.sql" and n.get("kind") == "file"]
    check(len(file_nodes) == 1, f"SQL flood: exactly 1 file node (got {len(file_nodes)})")
    if max_tables is not None:
        check(len(tables) <= max_tables,
              f"SQL flood: table count bounded by _MAX_TABLES ({len(tables)} <= {max_tables})")
    check(dt < 30, f"SQL flood: build_graph bounded in time ({dt:.2f}s)")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    print("=== ENGINE ROBUSTNESS gate (offline, no DB / no network) ===")
    print("-- huge file (50 MB) already guarded by size cap --")
    test_huge_file_already_guarded()
    print("-- giant single-line file (1.4 MB, under cap) --")
    test_giant_single_line_file()
    print("-- binary / non-UTF-8 file with code extension --")
    test_binary_file_with_code_extension()
    print("-- zero-byte files with code extensions --")
    test_zero_byte_files()
    print("-- non-UTF-8 encodings (latin-1 / shift-jis with PEP-263 cookie) --")
    test_non_utf8_encoding()
    print("-- deep directory tree (200 levels) --")
    test_deep_directory_tree()
    print("-- symlink loop (dir symlink pointing to ancestor) --")
    test_symlink_loop()
    print("-- file disappears mid-walk (race condition) --")
    test_file_disappears_mid_walk()
    print("-- permission-denied file --")
    test_permission_denied_file()
    print("-- unexpected per-file extractor exception --")
    test_unexpected_extractor_exception_preserves_document_contract()
    print("-- general parser loss remains path-local in storage --")
    test_general_parser_loss_stays_path_local_in_the_stored_graph()
    print("-- tree-sitter partial syntax remains incomplete, not failed --")
    test_tree_sitter_partial_tree_is_incomplete_not_failed()
    print("-- unavailable/unsupported parser bare-node uncertainty --")
    test_bare_parser_fallbacks_are_incomplete()
    print("-- line/per-file cap bare-node uncertainty --")
    test_line_and_symbol_caps_are_incomplete()
    print("-- dedicated contract formats remain complete --")
    test_dedicated_contract_formats_are_not_incomplete()
    print("-- analysis-status dedup priority --")
    test_analysis_status_dedup_priority()
    print("-- resolved-vs-ambiguous edge dedup precedence --")
    test_edge_dedup_prefers_resolved_evidence()
    print("-- local zero-candidate reference uncertainty --")
    test_local_zero_candidate_reference_status()
    print("-- deterministic same-stem relative import resolution --")
    test_relative_same_stem_resolution_is_deterministic()
    print("-- deterministic same-stem own-package resolution --")
    test_own_package_same_stem_resolution_is_deterministic()
    print("-- giant .gitattributes (size cap, was unbounded) --")
    test_giant_gitattributes_bounded()
    print("-- symlinked .gitattributes cannot escape repository root --")
    test_symlinked_gitattributes_is_ignored()
    print("-- independent .gitattributes generated/vendored state --")
    test_gitattributes_attribute_state_is_independent()
    print("-- .gitattributes syntax vs git check-attr oracle --")
    test_gitattributes_git_pattern_oracle()
    print("-- SQL file node dedup (was 2 file nodes, now 1) --")
    test_sql_file_node_dedup()
    print("-- gitattributes walk computed once per build_graph call --")
    test_gitattributes_walk_single()
    print("-- pathological SQL content (5 000 tables, bounded by _MAX_TABLES) --")
    test_pathological_sql_content()
    print("--------------------------------------------------------------")
    if FAIL == 0:
        print("ROBUSTNESS GATE: PASS")
        return 0
    print("ROBUSTNESS GATE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
