#!/usr/bin/env python3
"""The recall-decisive test (mirrors PR #252): of the UNCORROBORATED namespace edges that DO co-change
(real coupling), how many are ALSO carried by some OTHER structural edge between the same file pair —
a precise (single-file) import, a call the engine WOULD resolve, or a shared resource? If the namespace
edge is the SOLE structural carrier, demoting it = a guaranteed silent miss for that real coupling."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402
import tests.measure_namespace_edge_cochange as M  # noqa: E402


def main():
    for rd in sys.argv[1:]:
        label = os.path.basename(rd.rstrip("/"))
        g = X.build_graph(rd)
        _, uncorro = M._classify(g)
        change, co, n_total = M._cochange_index(rd)
        ns_set = set(uncorro)            # the namespace-fanout uncorroborated edges (a,b) directed src->dst

        # All OTHER structural edges between file pairs (undirected), EXCLUDING namespace-fanout edges.
        # imports that are NOT namespace fan-out = precise single-file imports. Plus any calls/shared res.
        # We approximate "other import carrier" as an import edge that is NOT in the namespace-fanout set.
        # calls/res are name/resource edges (dst is a symbol/resource, not a file) so they don't directly
        # give a file-pair; we instead ask: is there ANY resolved imports edge between the pair that is not
        # a namespace fan-out edge, OR a corroborating call (already excluded by definition of uncorro).
        other_import_pairs = set()
        for e in g["edges"]:
            if e["kind"] != "imports":
                continue
            s, d = e.get("src"), e.get("dst")
            if not s or not d or s == d:
                continue
            if (s, d) in ns_set:          # this IS a namespace fan-out edge — not an independent carrier
                continue
            other_import_pairs.add(frozenset((s, d)))

        recall_bearing = [(a, b) for (a, b) in uncorro
                          if M._pair_coupled(a, b, change, co, n_total)]
        sole = 0
        also = 0
        for (a, b) in recall_bearing:
            if frozenset((a, b)) in other_import_pairs:
                also += 1
            else:
                sole += 1
        print(f"[{label}] uncorroborated recall-bearing pairs (co-change & lift>=2): {len(recall_bearing)}  "
              f"| ALSO carried by another import edge: {also}  | SOLE carrier (namespace edge only): {sole}")


if __name__ == "__main__":
    raise SystemExit(main())
