#!/usr/bin/env python3
"""Go package-resolution PERF + Ruby bare-require PRECISION gate (no DB, no network).

WHY THIS GATE EXISTS (two measured defects caught by a dogfood perf + precision audit):

1. GO PERF (Risk 1, HIGH — measured 19.6s of kubernetes build_graph in import resolution):
   `_resolve_go_pkg` scanned ALL go_dirs linearly for EVERY import edge — O(go_dirs x imports).
   kubernetes = ~2,895 pkg dirs x ~63,447 imports = ~183M comparisons. The O(1) suffix index
   (`by_suffix`) built for OTHER languages was NOT used for Go. On kubernetes this consumed
   ~19.6s of build_graph's wall time per push — a backpressure multiplier on the single webhook
   worker that drains events serially.

   FIX: build a Go suffix index (`_go_pkg_by_suffix`) once at the start of `_resolve_imports`
   — mapping every path-segment tail of each package directory to that directory — so the
   per-edge resolution is a dict lookup sequence (O(1) per edge) not a linear scan.
   IDENTICAL SEMANTICS: the suffix keys are exactly the strings the old `endswith('/' + d)`
   test accepted; the most-specific-dir precision guard is preserved.

2. RUBY PRECISION (#2, HIGH — measured 5,188 spurious edges on Rails):
   A bare single-segment Ruby `require 'test_helper'` resolved to EVERY local `.rb` file
   named `test_helper.rb` (18 files in Rails -> 3,474 spurious edges from bare requires
   of `test_helper`; total 28 bare-require names x multi-target = 5,188 spurious edges).
   Multi-segment requires (`require 'active_support/log_subscriber'`) were ALREADY
   segment-anchored + precise — those are UNAFFECTED.

   FIX: after the family filter, when the src is a `.rb` file and the import is bare
   single-segment (no '/'), only accept a UNIQUE local match (exactly one `.rb` file).
   If multiple `.rb` files share the same basename the import is genuinely ambiguous
   (Ruby's $LOAD_PATH is runtime, not static directory layout) — suppress the fan-out
   and treat it as external/unresolvable (zero spurious edges).
   RECALL COST: near-zero. A repo with exactly ONE local `helper.rb` still resolves to it.

CONTENT-FREE: only repo file paths and module names. No file bodies read.
"""
import os
import sys
import tempfile
import time
from collections.abc import Mapping

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _cg_resolve as R  # noqa: E402
import code_graph_extract as X  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _resolve_one(file_paths, src, dst):
    """Resolve one import edge and return the frozenset of dst values emitted."""
    nodes = [{"kind": "file", "path": p} for p in file_paths]
    edges = [{"src": src, "dst": dst, "kind": "imports"}]
    out = R._resolve_imports([dict(n) for n in nodes], [dict(e) for e in edges])
    return frozenset(e["dst"] for e in out if e["kind"] == "imports" and e["src"] == src)


def _resolve_all(file_paths, edges_raw):
    """Resolve a list of edges (list of (src, dst) tuples) and return resolved list."""
    nodes = [{"kind": "file", "path": p} for p in file_paths]
    edges = [{"src": s, "dst": d, "kind": "imports"} for s, d in edges_raw]
    return R._resolve_imports([dict(n) for n in nodes], [dict(e) for e in edges])


def _build_graph(files):
    """Build a real graph for a tiny path->content fixture."""
    with tempfile.TemporaryDirectory(prefix="resolve_uncertainty_") as root:
        for rel, body in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return X.build_graph(root)


# ---------------------------------------------------------------------------
# GO PERF checks
# ---------------------------------------------------------------------------

class _ObservedGoDirs(Mapping):
    """Read-only mapping that counts executed scans and point lookups.

    The performance contract is algorithmic: the reference resolver scans every
    package directory per import, while the production resolver probes only path
    suffixes. Counting those operations keeps the gate deterministic on a shared
    CI runner where a scheduler pause can arbitrarily inflate one tiny timing
    window. ``Mapping`` also routes future ``items()``/``keys()`` scans through
    ``__iter__``, so a regression cannot evade the scan counter.
    """

    def __init__(self, values):
        self._values = dict(values)
        self.iterations = 0
        self.lookups = 0

    def __getitem__(self, key):
        self.lookups += 1
        return self._values[key]

    def __iter__(self):
        for key in self._values:
            self.iterations += 1
            yield key

    def __len__(self):
        return len(self._values)

    def get(self, key, default=None):
        self.lookups += 1
        return self._values.get(key, default)


def go_index_correctness():
    """The indexed resolver (_resolve_go_pkg_indexed) returns BYTE-IDENTICAL results to
    the linear _resolve_go_pkg on a variety of import paths, including ambiguous suffixes."""
    # A synthetic universe: several package dirs at different depths, sharing some tail segments.
    go_files = [
        "svc/internal/auth/auth.go",
        "svc/internal/auth/token.go",
        "svc/auth/auth.go",          # shorter path, same tail `auth` — most-specific must win
        "svc/api/handler.go",
        "pkg/util/util.go",
        "pkg/util/logger.go",
        "cmd/main.go",
        "internal/cache/cache.go",
    ]
    go_dirs = R._go_pkg_dirs(go_files)
    go_suffix = R._go_pkg_by_suffix(go_dirs)

    test_imports = [
        "github.com/org/svc/internal/auth",      # -> svc/internal/auth/ (most specific)
        "github.com/org/svc/auth",                # -> svc/auth/ (shorter, but exact)
        "github.com/org/svc/api/handler",         # -> no dir named 'handler' (file, not dir)
        "github.com/org/pkg/util",                # -> pkg/util/
        "github.com/org/internal/cache",          # -> internal/cache/
        "fmt",                                    # single-segment stdlib -> empty
        "go.uber.org/zap",                        # external -> empty
        "github.com/org/svc/nonexistent",         # no such dir -> empty
    ]

    ok = True
    for imp in test_imports:
        slow = R._resolve_go_pkg(imp, go_dirs)
        fast = R._resolve_go_pkg_indexed(imp, go_suffix, go_dirs)
        if slow != fast:
            ok = False
            print(f"  [go_index MISMATCH] import={imp!r}")
            print(f"      slow (linear): {sorted(slow)}")
            print(f"      fast (indexed): {sorted(fast)}")
    return ("Go indexed resolver returns byte-identical results to the linear scan on "
            "deep-nested and ambiguous-suffix fixtures", ok)


def go_index_most_specific():
    """When two dirs share a common suffix tail, the MOST-SPECIFIC (deepest) dir wins —
    same semantics as the max-depth selection in _resolve_go_pkg."""
    go_files = [
        "svc/internal/auth/auth.go",    # 3-segment dir
        "auth/auth.go",                  # 1-segment dir — the SAME tail `auth` matches both
    ]
    go_dirs = R._go_pkg_dirs(go_files)
    go_suffix = R._go_pkg_by_suffix(go_dirs)

    imp = "github.com/org/svc/internal/auth"

    slow = R._resolve_go_pkg(imp, go_dirs)
    fast = R._resolve_go_pkg_indexed(imp, go_suffix, go_dirs)

    # Both must return ONLY the most-specific dir (svc/internal/auth/), NOT the shallower `auth/`
    expected = {"svc/internal/auth/auth.go"}
    ok = slow == expected == fast
    if not ok:
        print(f"  most-specific: slow={sorted(slow)} fast={sorted(fast)} expected={sorted(expected)}")
    return ("Go most-specific-dir precision: a deep svc/internal/auth resolves to its dir, "
            "never the shallower `auth/` (max-depth guard preserved)", ok)


def go_scale_complexity():
    """Execute both resolvers and prove their different operation bounds.

    A one-shot wall-clock ratio is not a safe release authority on a shared runner:
    the indexed section is only a few milliseconds, so one scheduler preemption can
    turn a normal >20x observation into a false <10x failure. Instead this test counts
    the actual mapping operations made by the production functions. The reference
    must scan every directory for every import; the indexed path may scan the
    directory universe at most once to build an index, then may perform no more
    point lookups than the imports' total path depth.

    Timing remains in the output as diagnostic evidence, but cannot decide release.
    Content-free: synthetic paths only."""
    n_dirs = 300
    n_imports = 3000

    # Build a universe: n_dirs Go package dirs, each with 3 .go files
    go_files = []
    for i in range(n_dirs):
        d = f"pkg/sub{i % 10}/group{i % 30}/pkg{i}"
        go_files += [f"{d}/file{j}.go" for j in range(3)]

    go_dirs = R._go_pkg_dirs(go_files)
    dirs_list = list(go_dirs.keys())

    # Half are local hits and half are unique external/missing imports. Misses
    # are the critical adversarial path: a tempting "index miss -> linear
    # fallback" silently restores O(dirs x imports) on ordinary dependencies.
    import_raws = [
        (
            f"github.com/org/repo/{dirs_list[(i * 7) % len(dirs_list)]}"
            if i % 2 == 0
            else f"github.com/external/dependency/missing{i}"
        )
        for i in range(n_imports)
    ]

    # SLOW: execute the before-state reference. Its defining regression is the
    # complete directory scan performed once per import.
    linear_dirs = _ObservedGoDirs(go_dirs)
    t0 = time.perf_counter()
    slow_results = [R._resolve_go_pkg(raw, linear_dirs) for raw in import_raws]
    t_slow = time.perf_counter() - t0

    # FAST: execute the production indexed path. It may probe each path suffix,
    # but it must never iterate the directory universe.
    indexed_dirs = _ObservedGoDirs(go_dirs)
    go_suffix = R._go_pkg_by_suffix(indexed_dirs)
    t1 = time.perf_counter()
    fast_results = [
        R._resolve_go_pkg_indexed(raw, go_suffix, indexed_dirs)
        for raw in import_raws
    ]
    t_fast = time.perf_counter() - t1

    expected_linear_scans = len(go_dirs) * len(import_raws)
    indexed_lookup_ceiling = len(go_dirs) + sum(
        len(raw.strip().strip("\"'").split("/")) for raw in import_raws
    )
    indexed_operations = indexed_dirs.iterations + indexed_dirs.lookups
    operation_ratio = (
        linear_dirs.iterations / indexed_operations
        if indexed_operations
        else float("inf")
    )
    observed_time_ratio = t_slow / t_fast if t_fast > 0 else float("inf")
    ok = (
        slow_results == fast_results
        and linear_dirs.iterations == expected_linear_scans
        and indexed_dirs.iterations <= len(go_dirs)
        and 0 < indexed_dirs.lookups <= indexed_lookup_ceiling
        and operation_ratio >= 25.0
    )
    print(
        f"  Go scale: {len(go_dirs)} dirs x {len(import_raws)} imports: "
        f"linear_scans={linear_dirs.iterations} "
        f"indexed_build_scans={indexed_dirs.iterations} "
        f"indexed_lookups={indexed_dirs.lookups} "
        f"operation_reduction={operation_ratio:.1f}x; "
        f"diagnostic_wall_clock linear={t_slow:.3f}s indexed={t_fast:.3f}s "
        f"observed_speedup={observed_time_ratio:.1f}x (non-authoritative)"
    )
    return (
        "Go index scans the directory universe at most once and executes >=25x "
        f"fewer candidate-directory operations than the linear reference "
        f"({operation_ratio:.1f}x on "
        f"{len(go_dirs)} dirs x {len(import_raws)} edges)",
        ok,
    )


def go_edge_count_unchanged():
    """Full _resolve_imports produces the SAME number of resolved Go edges with or without
    the index (the fix is purely a speedup, not a semantics change). We run the
    production path on a mixed hit/miss fixture, compare it with the reference,
    and prove misses never fall back to the linear helper."""
    # Build a fixture with 30 Go dirs and 200 import edges: 100 local hits plus
    # 100 unique external/missing paths.
    go_files = [f"pkg/sub{i}/file{j}.go" for i in range(30) for j in range(4)]
    go_dirs = R._go_pkg_dirs(go_files)
    dirs_list = list(go_dirs.keys())

    edges = [
        {
            "src": f"cmd/main{k}.go",
            "dst": (
                f"github.com/org/repo/{dirs_list[(k * 3) % len(dirs_list)]}"
                if k % 2 == 0
                else f"github.com/external/dependency/missing{k}"
            ),
            "kind": "imports",
        }
        for k in range(200)
    ]
    nodes = [{"kind": "file", "path": p} for p in go_files]

    # REFERENCE: use _resolve_go_pkg (linear) directly — bypass the index
    ref_cands = {}
    for e in edges:
        raw = e["dst"]
        ref_cands[raw] = R._resolve_go_pkg(raw, go_dirs)

    # ACTUAL: run full _resolve_imports and spy on the production wiring. A
    # helper-only complexity test would miss a call-site regression that swapped
    # the live path back to the linear resolver while leaving the helper intact.
    calls = {"build": 0, "indexed": 0, "linear": 0}
    original_build = R._go_pkg_by_suffix
    original_indexed = R._resolve_go_pkg_indexed
    original_linear = R._resolve_go_pkg

    def observed_build(*args, **kwargs):
        calls["build"] += 1
        return original_build(*args, **kwargs)

    def observed_indexed(*args, **kwargs):
        calls["indexed"] += 1
        return original_indexed(*args, **kwargs)

    def observed_linear(*args, **kwargs):
        calls["linear"] += 1
        return original_linear(*args, **kwargs)

    R._go_pkg_by_suffix = observed_build
    R._resolve_go_pkg_indexed = observed_indexed
    R._resolve_go_pkg = observed_linear
    try:
        result = R._resolve_imports(nodes, edges)
    finally:
        R._go_pkg_by_suffix = original_build
        R._resolve_go_pkg_indexed = original_indexed
        R._resolve_go_pkg = original_linear

    # Compare: for each edge, does the indexed resolver produce the same dst set?
    ok = True
    for e in edges:
        raw = e["dst"]
        expected = ref_cands[raw]
        got = {r["dst"] for r in result if r["src"] == e["src"] and r.get("dst", "").endswith(".go")}
        if expected != got:
            ok = False
            print(f"  edge count mismatch for {raw!r}: expected={sorted(expected)} got={sorted(got)}")
            break

    wiring_ok = (
        calls["build"] == 1
        and calls["indexed"] == len(edges)
        and calls["linear"] == 0
    )
    if not wiring_ok:
        print(f"  production Go resolver wiring mismatch: {calls!r}")
    return (
        "Go import edge count is identical and production _resolve_imports builds the "
        "index once, routes every local-hit and external-miss Go edge through it, and "
        "never calls the linear scan on a 30-dir x 200-edge fixture",
        ok and wiring_ok,
    )


def go_directory_fanout_is_exact():
    """Multiple files in one Go package are exact targets, not ambiguity."""
    files = [
        "internal/auth/auth.go",
        "internal/auth/audit.go",
        "cmd/app/main.go",
    ]
    nodes = [{"kind": "file", "path": path} for path in files]
    edges = [{
        "src": "cmd/app/main.go",
        "dst": "example.com/project/internal/auth",
        "kind": "imports",
    }]
    ambiguous_paths = set()
    result = R._resolve_imports(
        nodes,
        edges,
        ambiguous_paths_out=ambiguous_paths,
    )
    targets = {
        edge["dst"]
        for edge in result
        if edge.get("src") == "cmd/app/main.go"
    }
    ok = (
        targets
        == {"internal/auth/auth.go", "internal/auth/audit.go"}
        and all(
            edge.get("reference_status") is None
            and edge.get("ambiguous_reference") is not True
            for edge in result
        )
        and not ambiguous_paths
    )
    return (
        "Go package-directory multi-file fan-out stays exact and emits no "
        "ambiguity side-channel paths",
        ok,
    )


def csharp_competing_directories_are_ambiguous():
    """Path depth cannot choose between unparsed C# project candidates."""

    def resolve(files, raw):
        nodes = [{"kind": "file", "path": path} for path in files]
        edges = [{
            "src": "Web/Home.cs",
            "dst": raw,
            "kind": "imports",
        }]
        ambiguous_paths = set()
        result = R._resolve_imports(
            nodes,
            edges,
            ambiguous_paths_out=ambiguous_paths,
        )
        direct_files, directory_count = R._resolve_csharp_ns_detail(
            raw,
            R._cs_ns_dirs(files),
        )
        return result, direct_files, directory_count, ambiguous_paths

    equal_files = [
        "ProjA/App/Services/A.cs",
        "ProjB/App/Services/B.cs",
        "Web/Home.cs",
    ]
    equal_targets = {
        "ProjA/App/Services/A.cs",
        "ProjB/App/Services/B.cs",
    }
    (
        equal_result,
        equal_direct,
        equal_count,
        equal_side_channel,
    ) = resolve(equal_files, "App.Services")
    equal_edges = [
        edge for edge in equal_result
        if edge.get("src") == "Web/Home.cs"
        and edge.get("dst") in equal_targets
    ]

    unequal_files = [
        "ProjA/App/Services/A.cs",
        "Company/ProjB/App/Services/B.cs",
        "Web/Home.cs",
    ]
    unequal_targets = {
        "ProjA/App/Services/A.cs",
        "Company/ProjB/App/Services/B.cs",
    }
    (
        unequal_result,
        unequal_direct,
        unequal_count,
        unequal_side_channel,
    ) = resolve(unequal_files, "App.Services")
    unequal_edges = [
        edge for edge in unequal_result
        if edge.get("src") == "Web/Home.cs"
        and edge.get("dst") in unequal_targets
    ]

    exact_files = [
        "App/Services/A.cs",
        "Company/ProjB/App/Services/B.cs",
        "Web/Home.cs",
    ]
    (
        exact_result,
        exact_direct,
        exact_count,
        exact_side_channel,
    ) = resolve(exact_files, "App.Services")
    exact_edges = [
        edge for edge in exact_result
        if edge.get("src") == "Web/Home.cs"
    ]

    typed_files = [
        "ProjA/App/Services/Tools.cs",
        "Company/ProjB/App/Services/Tools.cs",
        "Web/Home.cs",
    ]
    typed_targets = {
        "ProjA/App/Services/Tools.cs",
        "Company/ProjB/App/Services/Tools.cs",
    }
    (
        typed_result,
        typed_direct,
        typed_count,
        typed_side_channel,
    ) = resolve(typed_files, "App.Services.Tools")
    typed_edges = [
        edge for edge in typed_result
        if edge.get("src") == "Web/Home.cs"
        and edge.get("dst") in typed_targets
    ]

    def all_ambiguous(edges, key):
        return all(
            edge.get("reference_status") == "ambiguous"
            and edge.get("ambiguous_reference") is True
            and edge.get("ambiguity_key") == key
            for edge in edges
        )

    ok = (
        equal_direct == equal_targets
        and equal_count == 2
        and len(equal_edges) == 2
        and all_ambiguous(equal_edges, "App.Services")
        and unequal_direct == unequal_targets
        and unequal_count == 2
        and len(unequal_edges) == 2
        and all_ambiguous(unequal_edges, "App.Services")
        and exact_direct == {"App/Services/A.cs"}
        and exact_count == 1
        and len(exact_edges) == 1
        and exact_edges[0].get("dst") == "App/Services/A.cs"
        and exact_edges[0].get("reference_status") is None
        and typed_direct == typed_targets
        and typed_count == 2
        and len(typed_edges) == 2
        and all_ambiguous(typed_edges, "App.Services.Tools")
        and not equal_side_channel
        and not unequal_side_channel
        and not exact_side_channel
        and not typed_side_channel
    )
    if not ok:
        print(
            "  C# directory classification mismatch: "
            f"equal=({equal_direct!r}, {equal_count}, {equal_edges!r}); "
            f"unequal=({unequal_direct!r}, {unequal_count}, "
            f"{unequal_edges!r}); exact=({exact_direct!r}, "
            f"{exact_count}, {exact_edges!r}); typed=({typed_direct!r}, "
            f"{typed_count}, {typed_edges!r})"
        )
    return (
        "C# equal/unequal-depth suffix directories and type-qualified "
        "multi-directory matches are ambiguous; an exact canonical namespace "
        "directory wins confidently",
        ok,
    )


# ---------------------------------------------------------------------------
# RUBY PRECISION checks
# ---------------------------------------------------------------------------

def ruby_bare_single_ambiguous_suppressed():
    """A bare single-segment require with MULTIPLE local .rb files of that name is
    suppressed (zero rb->rb edges). This is the primary fix: no spurious fan-out."""
    files = [
        "test/test_helper.rb",
        "spec/test_helper.rb",
        "lib/test_helper.rb",
        "actionmailbox/test/test_helper.rb",
    ]
    edges = [("test/user_test.rb", "test_helper")]

    result = _resolve_all(files, edges)
    rb_rb = [e for e in result
             if e["src"] == "test/user_test.rb" and e["dst"].endswith(".rb")]
    raw = [
        e for e in result
        if e["src"] == "test/user_test.rb" and e["dst"] == "test_helper"
    ]
    ok = (
        len(rb_rb) == 0
        and len(raw) == 1
        and raw[0].get("reference_status") == "ambiguous"
        and raw[0].get("ambiguous_reference") is True
        and raw[0].get("ambiguity_key") == "test_helper"
    )
    if not ok:
        print(
            "  bare 'test_helper' ambiguity evidence: "
            f"resolved={[e['dst'] for e in rb_rb]}, raw={raw!r}"
        )
    return ("Ruby bare 'test_helper' with 4 local .rb targets: spurious fan-out suppressed "
            "and one inert ambiguity edge retained (0 rb->rb edges, was 4)", ok)


def ruby_collapsed_candidates_reach_side_channel():
    """Collapsed raw evidence still identifies every affected local document."""
    files = [
        "test/test_helper.rb",
        "spec/test_helper.rb",
        "lib/test_helper.rb",
        "actionmailbox/test/test_helper.rb",
    ]
    nodes = [{"kind": "file", "path": path} for path in files]
    edges = [{
        "src": "test/user_test.rb",
        "dst": "test_helper",
        "kind": "imports",
    }]
    ambiguous_paths = set()
    result = R._resolve_imports(
        nodes,
        edges,
        ambiguous_paths_out=ambiguous_paths,
    )
    expected = {"test/user_test.rb", *files}
    ok = (
        ambiguous_paths == expected
        and len(result) == 1
        and result[0].get("dst") == "test_helper"
        and result[0].get("reference_status") == "ambiguous"
    )
    return (
        "Ruby collapsed raw ambiguity reports importer plus every local "
        "candidate through the transient path side channel",
        ok,
    )


def build_graph_stamps_ruby_candidate_endpoints():
    """The production build stamps source and collapsed Ruby candidates."""
    graph = _build_graph({
        "main.rb": "require 'helper'\n",
        "a/helper.rb": "module AHelper; end\n",
        "b/helper.rb": "module BHelper; end\n",
    })
    paths = {"main.rb", "a/helper.rb", "b/helper.rb"}
    statuses = {
        node.get("path"): node.get("analysis_status")
        for node in graph["nodes"]
        if node.get("kind") == "file" and node.get("path") in paths
    }
    raw = [
        edge for edge in graph["edges"]
        if edge.get("src") == "main.rb"
        and edge.get("dst") == "helper"
        and edge.get("kind") == "imports"
    ]
    ok = (
        statuses == {path: "ambiguous" for path in paths}
        and len(raw) == 1
        and raw[0].get("reference_status") == "ambiguous"
        and (graph.get("metrics") or {}).get(
            "ambiguous_reference_count"
        ) == 1
    )
    if not ok:
        print(
            "  Ruby build_graph ambiguity mismatch: "
            f"statuses={statuses!r}, raw={raw!r}, "
            f"metrics={graph.get('metrics')!r}"
        )
    return (
        "build_graph stamps the Ruby importer and every collapsed candidate "
        "ambiguous, with one observed ambiguity",
        ok,
    )


def build_graph_stamps_overcap_candidate_endpoints():
    """An over-cap raw edge remains bounded without hiding affected files."""
    cap = R._MAX_BARE_FANOUT
    files = {
        "consumer.py": "import util\n",
        **{
            f"d{i}/util.py": f"VALUE_{i} = {i}\n"
            for i in range(cap + 1)
        },
    }
    graph = _build_graph(files)
    paths = set(files)
    statuses = {
        node.get("path"): node.get("analysis_status")
        for node in graph["nodes"]
        if node.get("kind") == "file" and node.get("path") in paths
    }
    raw = [
        edge for edge in graph["edges"]
        if edge.get("src") == "consumer.py"
        and edge.get("dst") == "util"
        and edge.get("kind") == "imports"
    ]
    resolved = [
        edge for edge in graph["edges"]
        if edge.get("src") == "consumer.py"
        and edge.get("kind") == "imports"
        and edge.get("dst", "").endswith("/util.py")
    ]
    ok = (
        statuses == {path: "ambiguous" for path in paths}
        and len(raw) == 1
        and raw[0].get("reference_status") == "ambiguous"
        and not resolved
        and (graph.get("metrics") or {}).get(
            "ambiguous_reference_count"
        ) == 1
    )
    if not ok:
        print(
            "  over-cap build_graph ambiguity mismatch: "
            f"statuses={statuses!r}, raw={raw!r}, "
            f"resolved={resolved!r}, metrics={graph.get('metrics')!r}"
        )
    return (
        "build_graph collapses over-cap fan-out while stamping importer plus "
        "all candidate files ambiguous",
        ok,
    )


def ruby_bare_single_unique_resolves():
    """A bare single-segment require with EXACTLY ONE local .rb match still resolves.
    This is the recall-safe case: a small repo with one helper.rb must still couple."""
    # Only one helper.rb, one decoy helper.py (cross-language, filtered)
    files = ["lib/helper.rb", "app/helper.py"]
    got = _resolve_one(files, "main.rb", "helper")
    expected = frozenset({"lib/helper.rb"})
    ok = got == expected
    if not ok:
        print(f"  unique match 'helper': got={sorted(got)} expected={sorted(expected)}")
    return ("Ruby bare 'helper' with exactly ONE local .rb: still resolves (recall-safe, "
            "existing precision test case unbroken)", ok)


def ruby_multi_segment_still_resolves():
    """Multi-segment Ruby requires (require 'active_support/test_helper') MUST still resolve
    via the path-suffix index. The fix targets only bare SINGLE-segment requires."""
    files = [
        "test/test_helper.rb",
        "spec/test_helper.rb",
        "lib/test_helper.rb",
        "activesupport/lib/active_support/test_helper.rb",
    ]
    # Multi-segment: 'active_support/test_helper' -> should resolve to activesupport/.../test_helper.rb
    got = _resolve_one(files, "test/user_test.rb", "active_support/test_helper")
    expected = frozenset({"activesupport/lib/active_support/test_helper.rb"})
    ok = got == expected
    if not ok:
        print(f"  multi-seg 'active_support/test_helper': got={sorted(got)} expected={sorted(expected)}")
    return ("Ruby multi-segment 'active_support/test_helper' still resolves via suffix index "
            "(fix is bare-single-only, multi-segment unaffected)", ok)


def ruby_bare_no_local_match_stays_inert():
    """A bare single-segment require with NO local .rb file matching stays inert (as before)."""
    files = ["lib/utils.rb", "test/helper_test.rb"]
    got = _resolve_one(files, "main.rb", "zeitwerk")    # external gem, no local zeitwerk.rb
    expected = frozenset({"zeitwerk"})
    ok = got == expected
    if not ok:
        print(f"  bare 'zeitwerk' (no local .rb): got={sorted(got)}")
    return ("Ruby bare 'zeitwerk' (external gem, no local .rb): stays inert (no invented edge)", ok)


def ruby_no_cross_language_edge():
    """A Ruby bare require must NOT resolve to a same-named Python/JS/other-language file —
    the family filter applies before the Ruby unique-guard, so the guard sees only .rb cands."""
    files = ["lib/helper.py", "src/helper.js", "test/helper.go"]
    got = _resolve_one(files, "main.rb", "helper")
    # No .rb file exists — should stay inert (the raw module name)
    expected = frozenset({"helper"})
    ok = got == expected
    if not ok:
        print(f"  cross-language 'helper' from .rb: got={sorted(got)}")
    return ("Ruby bare 'helper' with only non-ruby matches: stays inert (no cross-language edge)", ok)


def ruby_many_bare_requires_mass_precision():
    """Simulate a Rails-like scenario: 28 bare require names, each with multiple local .rb matches.
    Verify that the total number of resolved rb->rb edges from bare requires is ZERO after the fix.
    Multi-segment requires in the same batch still resolve normally."""
    # 10 bare require names, each with 3 matching .rb files (= 30 spurious edges suppressed)
    bare_names = [f"helper_{i}" for i in range(10)]
    files = [f"dir{j}/helper_{i}.rb" for i in range(10) for j in range(3)]
    files += ["lib/multi/log_helper.rb"]    # a multi-segment target

    edges_raw = (
        [("test/test_file.rb", name) for name in bare_names] +   # 10 bare (each ambiguous)
        [("test/test_file.rb", "multi/log_helper")]               # 1 multi-segment (resolves)
    )

    result = _resolve_all(files, edges_raw)

    # Bare require edges: all should be suppressed (kept as raw module name, not .rb path)
    bare_rb_rb = [
        e for e in result
        if e["src"] == "test/test_file.rb"
        and e["dst"].endswith(".rb")
        and not e["dst"].endswith("log_helper.rb")   # exclude the legitimate multi-seg resolve
    ]
    multi_resolved = [
        e for e in result
        if e["src"] == "test/test_file.rb" and e["dst"].endswith("log_helper.rb")
    ]
    ambiguity_evidence = [
        e for e in result
        if e["src"] == "test/test_file.rb"
        and e["dst"] in bare_names
        and e.get("reference_status") == "ambiguous"
    ]

    ok = (
        len(bare_rb_rb) == 0
        and len(multi_resolved) == 1
        and len(ambiguity_evidence) == len(bare_names)
    )
    print(f"  mass precision: bare rb->rb fan-out={len(bare_rb_rb)} (expect 0); "
          f"ambiguity evidence={len(ambiguity_evidence)} (expect {len(bare_names)}); "
          f"multi-seg resolved={len(multi_resolved)} (expect 1)")
    return ("Rails-like mass precision: 10 bare multi-match requires produce 0 spurious rb->rb "
            "edges, retain 10 ambiguity facts; multi-segment require still resolves (1 edge)", ok)


# ---------------------------------------------------------------------------
# combined MAIN
# ---------------------------------------------------------------------------

def main():
    checks = [
        # Go PERF
        go_index_correctness(),
        go_index_most_specific(),
        go_scale_complexity(),
        go_edge_count_unchanged(),
        go_directory_fanout_is_exact(),
        csharp_competing_directories_are_ambiguous(),
        # Ruby PRECISION
        ruby_bare_single_ambiguous_suppressed(),
        ruby_collapsed_candidates_reach_side_channel(),
        build_graph_stamps_ruby_candidate_endpoints(),
        build_graph_stamps_overcap_candidate_endpoints(),
        ruby_bare_single_unique_resolves(),
        ruby_multi_segment_still_resolves(),
        ruby_bare_no_local_match_stays_inert(),
        ruby_no_cross_language_edge(),
        ruby_many_bare_requires_mass_precision(),
    ]
    ok = True
    for name, cond in checks:
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {name}")
        ok = ok and bool(cond)
    print("RESOLVE-GO-RUBY GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
