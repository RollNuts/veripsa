#!/usr/bin/env python3
"""Import-resolution FAN-OUT BOUND gate (no DB): caps the cross-file N² edge blow-up in the resolver.

WHY THIS GATE EXISTS (DoS / scale, verified MED): the resolver is RECALL-BIASED — a BARE single-segment
import (`import util`) fans out to EVERY same-basename file (so a real single coupling is never missed).
But that fan-out was UNCAPPED across files. A repo (adversarial, or just a big monorepo) with N files all
named `util.py` in distinct dirs + N importer files each doing `import util` makes EACH bare import resolve
to all N files → N² `imports` edges. MEASURED on the bare resolver:
    N=50 → 2,500 edges   N=100 → 10,000   N=200 → 40,000   N=500 → 250,000 edges = 17.9 MB ingest payload.
Time stays low but the edge count / ingest payload is QUADRATIC and was UNBOUNDED — the per-FILE
`_PER_FILE_EDGE_CAP` (code_graph_extract) runs BEFORE resolution, so it never bounds this CROSS-file
fan-out. A handful of such pushes inflate the graph + ingest payload and can stall the single webhook
worker / blow ingest size limits.

THE FIX (recall-safe): `_cg_resolve._MAX_BARE_FANOUT` (=8) caps the fan-out DEGREE of a BARE single-segment
import. A bare basename that resolves to MORE than the cap distinct files is LOW-PRECISION wallpaper
(`import util` that could mean any of 9+ `util.py` is not a confident single coupling), so the over-cap
ambiguous fan-out is COLLAPSED to one inert raw edge rather than discarded or fanned out. 1-8 candidates
still retain every candidate, now explicitly marked ambiguous; multi-segment / dotted / relative imports
are untouched (they are already precise and never bare_single). The cap (8) sits above the codebase's confident-coupling band (_HUB_THRESHOLD=3)
and below its absurd-blow-up line (audit_realrepos _FANOUT_ABSURD=50).

This gate proves THREE things, all offline + content-free (paths / module names only, no repo content):
  1. BOUNDED — the N-same-basename adversarial repo's resolved edges are bounded by N×cap, NOT N²
     (regression back to the uncapped fan-out re-introduces the quadratic blow-up and fails here).
  2. NORMAL RETAINED — a real low-ambiguity import (1, 3, and exactly-cap candidates) STILL retains
     every same-basename candidate, with ambiguity made explicit for multi-target cases.
  3. PRECISE COLLAPSE — only the OVER-cap ambiguous bare basename collapses to one inert raw edge,
     and the cap is BARE-single-segment-only (a multi-segment dotted import is never capped).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _cg_resolve as R  # noqa: E402


def _resolved_count(nodes, edges):
    """Resolve and count file→file `imports` edges (dst is a repo FILE PATH, i.e. resolved)."""
    out = R._resolve_imports([dict(n) for n in nodes], [dict(e) for e in edges])
    return sum(1 for e in out if e["kind"] == "imports" and e["dst"].endswith(".py") and "/" in e["dst"])


def _resolve_edges(file_paths, src, dst):
    """Every edge the resolver emits for one import."""
    nodes = [{"kind": "file", "path": p} for p in file_paths]
    edges = [{"src": src, "dst": dst, "kind": "imports"}]
    out = R._resolve_imports([dict(n) for n in nodes], [dict(e) for e in edges])
    return [
        e for e in out
        if e["kind"] == "imports" and e["src"] == src
    ]


def _resolve_one(file_paths, src, dst):
    """The SET of destinations emitted for one import."""
    return frozenset(
        edge["dst"] for edge in _resolve_edges(file_paths, src, dst)
    )


def bound_check():
    """The N-same-basename adversarial repo: N files all named util.py + N importers each `import util`.
    UNCAPPED this is N² resolved edges; with the cap (each importer is over-cap so its fan-out drops) the
    resolved edge count is BOUNDED by N×cap. We assert resolved <= N×cap for every N, AND that at the
    scales that previously exploded the count is no longer quadratic (a hard ceiling far below N²)."""
    cap = R._MAX_BARE_FANOUT
    ok = True
    rows = []
    for N in (50, 100, 200, 500):
        nodes = [{"kind": "file", "path": f"dir{i}/util.py"} for i in range(N)]
        edges = [{"src": f"imp{j}.py", "dst": "util", "kind": "imports"} for j in range(N)]
        resolved = _resolved_count(nodes, edges)
        bound = N * cap
        within = resolved <= bound
        not_quadratic = resolved < N * N            # strictly below the old uncapped count
        ok = ok and within and not_quadratic
        rows.append((N, resolved, N * N, bound, within and not_quadratic))
    for N, resolved, quad, bound, row_ok in rows:
        print(f"    N={N:4d}: resolved={resolved:7d}  (uncapped was N²={quad:8d}; "
              f"bound N×cap={bound})  {'OK' if row_ok else 'FAIL'}")
    return (f"adversarial N-same-basename repo: resolved `imports` edges are BOUNDED by N×cap "
            f"(cap={cap}), not N² — the uncapped cross-file fan-out DoS is closed", ok)


def normal_unchanged_check():
    """A real low-ambiguity bare import STILL fans out to every same-basename file: 1, 3, and EXACTLY-cap
    candidates all remain in the graph (recall preserved). Multi-candidate edges carry explicit
    ambiguity so persistence/query layers cannot mistake them for definitive adjacency."""
    cap = R._MAX_BARE_FANOUT
    ok = True
    for k in (1, 3, cap):                            # 1, 3, and exactly the cap → must all fully fan out
        files = [f"d{i}/util.py" for i in range(k)]
        emitted = _resolve_edges(files, "consumer.py", "util")
        got = frozenset(edge["dst"] for edge in emitted)
        expected = frozenset(files)
        statuses = {edge.get("reference_status") for edge in emitted}
        expected_statuses = {None} if k == 1 else {"ambiguous"}
        good = got == expected and statuses == expected_statuses
        ok = ok and good
        print(
            f"    {k} candidate(s): retained {len(got)}/{k}, "
            f"statuses={statuses!r}  "
            f"{'OK' if good else 'FAIL — got ' + str(sorted(got))}"
        )
    # An exactly-cap fan-out is the boundary that stays enumerated; one more
    # (cap+1) must collapse to one raw ambiguity edge.
    over = cap + 1
    files = [f"d{i}/util.py" for i in range(over)]
    emitted = _resolve_edges(files, "consumer.py", "util")
    got = frozenset(edge["dst"] for edge in emitted)
    over_collapses = (
        got == frozenset({"util"})
        and len(emitted) == 1
        and emitted[0].get("reference_status") == "ambiguous"
    )
    ok = ok and over_collapses
    print(f"    cap+1={over} candidates: collapsed to inert ambiguous raw 'util'  "
          f"{'OK' if over_collapses else 'FAIL — got ' + str(emitted)}")
    return ("normal resolution retained: a bare import with 1..cap same-basename matches keeps every "
            "candidate (ambiguity explicit); only the over-cap fan-out collapses", ok)


def precise_drop_check():
    """The cap is BARE-single-segment-only and recall-safe in spirit:
      (a) a DOTTED / multi-segment import that names a SPECIFIC path is NEVER capped — it resolves to its
          one path-suffix target even when many same-basename files exist (it was never a fan-out);
      (b) the over-cap bare import COLLAPSES to one INERT raw edge with explicit ambiguity status.
    A regression that capped multi-segment imports (real recall loss) or that emitted a partial/arbitrary
    subset for the over-cap case (nondeterministic phantom edges) fails here."""
    cap = R._MAX_BARE_FANOUT
    n = cap + 5
    # (a) Many `util.py` files, but a DOTTED `dir3.util` must still resolve to its ONE path target.
    files = [f"dir{i}/util.py" for i in range(n)]
    dotted = _resolve_one(files, "consumer.py", "dir3.util")
    dotted_ok = dotted == frozenset({"dir3/util.py"})
    print(f"    dotted `dir3.util` over {n} same-basename files → {sorted(dotted)}  "
          f"{'OK' if dotted_ok else 'FAIL'}")
    # (b) The over-cap BARE import drops to inert (the raw module name), never a partial fan-out.
    bare_edges = _resolve_edges(files, "consumer.py", "util")
    bare = frozenset(edge["dst"] for edge in bare_edges)
    bare_inert = (
        bare == frozenset({"util"})
        and len(bare_edges) == 1
        and bare_edges[0].get("reference_status") == "ambiguous"
    )
    print(f"    bare `util` over {n} same-basename files → {'inert ambiguous (raw kept)' if bare_inert else bare_edges}  "
          f"{'OK' if bare_inert else 'FAIL'}")
    return ("the fan-out cap is bare-single-segment-only and precise: a dotted/multi-segment import is "
            "never capped (resolves to its one target), and the over-cap bare import collapses cleanly "
            "to inert ambiguity evidence (no partial/phantom fan-out)", dotted_ok and bare_inert)


def main():
    print(f"  fan-out cap _MAX_BARE_FANOUT = {R._MAX_BARE_FANOUT}")
    checks = [bound_check(), normal_unchanged_check(), precise_drop_check()]
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("RESOLVE FANOUT BOUND GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
