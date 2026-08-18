#!/usr/bin/env python3
"""MEASUREMENT (not a gate): do UNCORROBORATED namespace import edges carry real co-change coupling,
or are they at the random baseline (spurious)?

A namespace import edge F -> G (F has `using N` / a Go pkg import N; G is a .cs/.go file in namespace N,
reached ONLY because it shares the directory) is:
  * CORROBORATED   when F ALSO calls a symbol that G defines (F has a `calls` edge whose dst name is a
                   symbol G `contains`) — F genuinely uses something from G.
  * UNCORROBORATED when F uses NOTHING from G — G is in the namespace dir but F never calls into it.

For each set we compute the co-change RATE (fraction of those file pairs that meet the engine's own
co-change bar: >= min_support co-commits AND lift >= min_lift over the repo's git history) and compare to
a RANDOM baseline (the co-change rate of random same-language file pairs from the same repo). If
uncorroborated edges sit at baseline AND corroborated ones are clearly elevated, demoting uncorroborated
ones is recall-safe. If uncorroborated edges carry meaningful co-change, demoting LOSES recall.

Content-free: only paths, counts, edge kinds, symbol NAMES. Reuses _cg_cochange (the product's own engine).
"""
from __future__ import annotations

import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X            # noqa: E402
import _cg_cochange as CC                 # noqa: E402
import _cg_resolve as R                   # noqa: E402

# Same co-change bar the product uses to call a pair "coupled" (cochange_pairs defaults).
MIN_SUPPORT = 3
MIN_LIFT = 2.0
WINDOW = 1500
MAX_COMMIT_FILES = 40


def _namespace_edges(graph):
    """Re-derive WHICH resolved import edges came from a NAMESPACE/PACKAGE directory fan-out (C# `using`,
    Go pkg import) vs a precise single-file import. We replay the resolver's dir-maps: an edge F->G is a
    namespace-fanout edge iff G is one of the .cs/.go files _resolve_csharp_ns/_resolve_go_pkg would link
    for SOME import raw in F whose resolution set has size > 1 (a single-file resolution is precise, not a
    fan-out). We approximate the membership test directly: G's directory is a namespace dir holding >1
    same-language file AND the edge is to a .cs/.go file. We then split by corroboration. This identifies
    the population the task targets (namespace dir fan-out) without re-parsing imports."""
    nodes = graph["nodes"]
    edges = graph["edges"]
    files = [n["path"] for n in nodes if n.get("kind") == "file"]
    # directory -> count of same-lang files (the fan-out width). >1 = a namespace dir that fans out.
    cs_dir_n: dict = {}
    go_dir_n: dict = {}
    for p in files:
        d = p.rsplit("/", 1)[0] if "/" in p else ""
        if p.endswith(".cs"):
            cs_dir_n[d] = cs_dir_n.get(d, 0) + 1
        elif p.endswith(".go") and not p.endswith("_test.go"):
            go_dir_n[d] = go_dir_n.get(d, 0) + 1

    def _is_ns_target(g):
        d = g.rsplit("/", 1)[0] if "/" in g else ""
        if g.endswith(".cs"):
            return cs_dir_n.get(d, 0) > 1
        if g.endswith(".go") and not g.endswith("_test.go"):
            return go_dir_n.get(d, 0) > 1
        return False

    ns_edges = []
    for e in edges:
        if e.get("kind") != "imports":
            continue
        src, dst = e.get("src"), e.get("dst")
        if not src or not dst or src == dst:
            continue
        # only consider resolved file->file import edges where the importer + target are .cs or .go and
        # the target lives in a multi-file (fan-out) directory.
        if not (src.endswith(".cs") or src.endswith(".go")):
            continue
        if not _is_ns_target(dst):
            continue
        ns_edges.append((src, dst))
    return ns_edges


def _corroboration(graph):
    """For each file: the set of symbol NAMES it `contains` (defines), and the set of symbol NAMES it
    `calls`. F corroborates G iff (F.calls ∩ G.contains) is non-empty."""
    contains: dict = {}
    calls: dict = {}
    for e in graph["edges"]:
        if e["kind"] == "contains":
            name = e["dst"].split("::", 1)[1] if "::" in e["dst"] else e["dst"]
            contains.setdefault(e["src"], set()).add(name)
        elif e["kind"] == "calls":
            calls.setdefault(e["src"], set()).add(e["dst"])
    return contains, calls


def _classify(graph):
    ns_edges = _namespace_edges(graph)
    contains, calls = _corroboration(graph)
    corro, uncorro = [], []
    for (src, dst) in ns_edges:
        if calls.get(src, set()) & contains.get(dst, set()):
            corro.append((src, dst))
        else:
            uncorro.append((src, dst))
    return corro, uncorro


def _cochange_index(repo_dir):
    """Build a fast lookup: does file pair {a,b} clear the engine's co-change bar (support>=MIN_SUPPORT,
    lift>=MIN_LIFT)? Reuse the product's own _git_log_commits + counting (NOT cochange_pairs's min_prob
    filter — we want the raw support/lift bar so corroborated/uncorroborated are judged identically)."""
    import collections
    commits = CC._git_log_commits(repo_dir, WINDOW, timeout=120)
    change: collections.Counter = collections.Counter()
    co: collections.Counter = collections.Counter()
    n_total = 0
    for fileset in commits:
        n = len(fileset)
        if n == 0 or n > MAX_COMMIT_FILES:
            continue
        n_total += 1
        fl = sorted(fileset)
        for f in fl:
            change[f] += 1
        for i in range(n):
            for j in range(i + 1, n):
                co[(fl[i], fl[j])] += 1
    return change, co, n_total


def _pair_coupled(a, b, change, co, n_total):
    key = (a, b) if a < b else (b, a)
    c = co.get(key, 0)
    if c < MIN_SUPPORT:
        return False
    na, nb = change.get(a, 0), change.get(b, 0)
    if not (na and nb):
        return False
    lift = (c * n_total) / (na * nb)
    return lift >= MIN_LIFT


def _rate(pairs, change, co, n_total):
    if not pairs:
        return (0, 0, 0.0)
    hit = sum(1 for (a, b) in pairs if _pair_coupled(a, b, change, co, n_total))
    return (hit, len(pairs), hit / len(pairs))


def _baseline_rate(graph, change, co, n_total, lang_suffix, n_samples=4000, seed=17):
    """Random same-language file-pair co-change rate (the spurious floor). Only sample among files that
    appear in the git history window (else baseline is artificially 0 for files never touched)."""
    touched = set(change.keys())
    files = sorted({n["path"] for n in graph["nodes"]
                    if n.get("kind") == "file" and n["path"].endswith(lang_suffix) and n["path"] in touched})
    if len(files) < 2:
        return (0, 0, 0.0)
    rng = random.Random(seed)
    hit = tot = 0
    for _ in range(n_samples):
        a, b = rng.sample(files, 2)
        tot += 1
        if _pair_coupled(a, b, change, co, n_total):
            hit += 1
    return (hit, tot, (hit / tot) if tot else 0.0)


def measure_repo(repo_dir, label):
    graph = X.build_graph(repo_dir)
    corro, uncorro = _classify(graph)
    change, co, n_total = _cochange_index(repo_dir)
    # which language dominates the namespace edges (for the baseline pool)?
    cs = sum(1 for (s, _) in corro + uncorro if s.endswith(".cs"))
    go = sum(1 for (s, _) in corro + uncorro if s.endswith(".go"))
    suffix = ".cs" if cs >= go else ".go"
    c_rate = _rate(corro, change, co, n_total)
    u_rate = _rate(uncorro, change, co, n_total)
    b_rate = _baseline_rate(graph, change, co, n_total, suffix)
    out = {
        "label": label, "n_total_commits": n_total, "lang": suffix,
        "corro_edges": len(corro), "uncorro_edges": len(uncorro),
        "corro_cochange": c_rate, "uncorro_cochange": u_rate, "baseline": b_rate,
    }
    return out


def main():
    repos = sys.argv[1:]
    if not repos:
        print("usage: measure_namespace_edge_cochange.py <repo_dir> [<repo_dir> ...]")
        return 2
    print(f"co-change bar: support>={MIN_SUPPORT}, lift>={MIN_LIFT}, window={WINDOW}, max_commit_files={MAX_COMMIT_FILES}")
    print("=" * 100)
    for rd in repos:
        label = os.path.basename(rd.rstrip("/"))
        try:
            r = measure_repo(rd, label)
        except Exception as exc:  # noqa: BLE001
            print(f"{label}: ERROR {type(exc).__name__}: {exc}")
            continue
        ch, ct, cr = r["corro_cochange"]
        uh, ut, ur = r["uncorro_cochange"]
        bh, bt, br = r["baseline"]
        print(f"\n[{r['label']}]  lang={r['lang']}  commits_in_window={r['n_total_commits']}")
        print(f"  CORROBORATED   namespace edges: {r['corro_edges']:5d}  co-change {ch}/{ct} = {cr:.3%}")
        print(f"  UNCORROBORATED namespace edges: {r['uncorro_edges']:5d}  co-change {uh}/{ut} = {ur:.3%}")
        print(f"  RANDOM baseline (same-lang)   : {bt:5d} samples  co-change {bh}/{bt} = {br:.3%}")
        if br > 0:
            print(f"  lift over baseline: corroborated x{cr/br:.1f}   uncorroborated x{ur/br:.1f}" if br else "")
    print("\n" + "=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
