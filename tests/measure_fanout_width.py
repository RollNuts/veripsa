#!/usr/bin/env python3
"""WHY does uncorroborated co-change vary by repo? Hypothesis: it's the NAMESPACE FAN-OUT WIDTH.
A `using N` in a dir with 2 files fans out to 1 extra file (likely a real sibling you use); a `using N`
in a dir with 40 files fans out to 39 files you touch nothing in (pure noise). Measure uncorroborated
namespace-edge co-change BUCKETED by the target directory's file count (fan-out width)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402
import tests.measure_namespace_edge_cochange as M  # noqa: E402


def _dir_width(graph):
    files = [n["path"] for n in graph["nodes"] if n.get("kind") == "file"]
    w = {}
    for p in files:
        d = p.rsplit("/", 1)[0] if "/" in p else ""
        if p.endswith(".cs"):
            w[d] = w.get(d, 0) + 1
        elif p.endswith(".go") and not p.endswith("_test.go"):
            w[d] = w.get(d, 0) + 1
    return w


def main():
    for rd in sys.argv[1:]:
        label = os.path.basename(rd.rstrip("/"))
        g = X.build_graph(rd)
        width = _dir_width(g)
        corro, uncorro = M._classify(g)
        change, co, n_total = M._cochange_index(rd)
        # bucket uncorroborated by target dir width
        buckets = {"2": [], "3-5": [], "6-10": [], "11-20": [], "21+": []}

        def bk(n):
            if n <= 2:
                return "2"
            if n <= 5:
                return "3-5"
            if n <= 10:
                return "6-10"
            if n <= 20:
                return "11-20"
            return "21+"
        for (s, d) in uncorro:
            tgtdir = d.rsplit("/", 1)[0] if "/" in d else ""
            buckets[bk(width.get(tgtdir, 0))].append((s, d))
        ch, ct, cr = M._rate(corro, change, co, n_total)
        suffix = ".cs" if any(s.endswith(".cs") for s, _ in corro + uncorro) else ".go"
        bh, bt, br = M._baseline_rate(g, change, co, n_total, suffix)
        print(f"\n[{label}] baseline={br:.3%}  corroborated co-change={cr:.3%} (x{cr/br:.1f})" if br else f"\n[{label}] baseline=0")
        print(f"  UNCORROBORATED by target-dir fan-out width:")
        for name, pairs in buckets.items():
            h, t, r = M._rate(pairs, change, co, n_total)
            mult = f"x{r/br:.1f}" if br else "n/a"
            print(f"    width {name:6s}: {t:6d} edges  co-change {h}/{t} = {r:.3%}  ({mult} baseline)")


if __name__ == "__main__":
    raise SystemExit(main())
