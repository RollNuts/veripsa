#!/usr/bin/env python3
"""RECALL COST of the ubiquitous-config-key stoplist: of the config-coupled pairs that are
ubiquitous-key-ONLY (the ones the stoplist would demote), how many are a STRONG real coupling
(co-change >=3 AND lift >=2) that is ALSO not covered by any other graph edge (import/call/schema)
or by a specific config key? Those — and only those — are the recall casualties.

Run: python3 measure_config_recall_cost.py /repo [/repo2 ...]
"""
from __future__ import annotations
import os, sys, subprocess
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X
from _cg_config import _is_ubiquitous_config_key

MAX_COMMIT_FILES = int(os.environ.get("BACKTEST_MAX_COMMIT_FILES", "30"))


def commit_touchsets(repo, files):
    out = subprocess.run(["git", "-C", repo, "log", "--no-merges", "--name-only", "--pretty=format:@@%H"],
                         capture_output=True, text=True).stdout
    touch, idx, cur, T = {}, -1, [], set()

    def flush():
        if cur and len(cur) <= MAX_COMMIT_FILES:
            for f in cur:
                touch.setdefault(f, set()).add(idx)
            T.add(idx)
    for line in out.splitlines():
        if line.startswith("@@"):
            flush(); cur = []; idx += 1
        elif idx >= 0 and line in files:
            cur.append(line)
    flush()
    return touch, len(T)


def analyze(repo):
    g = X.build_graph(repo)
    files = {n["path"] for n in g["nodes"] if n["kind"] == "file"}

    # config keys shared per pair
    by_key = {}
    for e in g["edges"]:
        if e["kind"] == "reads_config" and e["src"] in files:
            by_key.setdefault(e["dst"], set()).add(e["src"])
    pair_keys = {}
    for key, fs in by_key.items():
        fs = sorted(fs)
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                pair_keys.setdefault(frozenset((fs[i], fs[j])), set()).add(key)

    # other (non-config) graph edges between files — so a "demoted" pair still has signal if it is
    # connected by import/call/schema. Import: src->dst (both files). Schema: shared queries/alters dst.
    other_edge = set()
    by_res = {}
    for e in g["edges"]:
        k = e["kind"]
        if k == "imports" and e["dst"] in files and e["src"] in files:
            other_edge.add(frozenset((e["src"], e["dst"])))
        elif k in ("queries", "alters") and e["src"] in files:
            by_res.setdefault(e["dst"], set()).add(e["src"])
    for fs in by_res.values():
        fs = sorted(fs)
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                other_edge.add(frozenset((fs[i], fs[j])))

    touch, T = commit_touchsets(repo, files)

    def strong(pair):
        a, b = tuple(pair)
        sa, sb = touch.get(a), touch.get(b)
        if not sa or not sb:
            return False
        nco = len(sa & sb)
        if nco < 3:
            return False
        lift = (nco * T) / (len(sa) * len(sb))
        return lift >= 2.0

    ubiq_only_pairs = [p for p, ks in pair_keys.items() if not any(not _is_ubiquitous_config_key(k) for k in ks)]
    # recall casualty = ubiq-only pair that is STRONG co-change AND has no other edge AND no specific key
    casualties = [p for p in ubiq_only_pairs if strong(p) and p not in other_edge]
    demoted = len(ubiq_only_pairs)
    # how many demoted pairs are STILL covered by another edge (so demotion costs nothing)?
    demoted_but_covered = sum(1 for p in ubiq_only_pairs if p in other_edge)
    return {
        "repo": os.path.basename(repo.rstrip("/")),
        "total_config_pairs": len(pair_keys),
        "demoted": demoted,
        "demoted_but_covered_by_other_edge": demoted_but_covered,
        "recall_casualties": len(casualties),
        "casualty_examples": [(sorted(tuple(p)), sorted(pair_keys[p])) for p in casualties[:8]],
    }


def main():
    repos = [os.path.abspath(p) for p in sys.argv[1:]] or [ROOT]
    print("\n=== RECALL COST of the ubiquitous-config-key demotion ===")
    tot_demoted = tot_cas = 0
    for r in [analyze(x) for x in repos]:
        print("\n" + "=" * 80)
        print(f"{r['repo']}: {r['total_config_pairs']} config pairs total")
        print(f"  DEMOTED (ubiq-key-only): {r['demoted']}  "
              f"({r['demoted']/max(r['total_config_pairs'],1)*100:.1f}% of config pairs)")
        print(f"  of those, still covered by import/call/schema edge: {r['demoted_but_covered_by_other_edge']}")
        print(f"  RECALL CASUALTIES (strong co-change, ubiq-only, no other edge): {r['recall_casualties']}")
        for ex, keys in r["casualty_examples"]:
            print(f"      ! {ex}  keys={keys}")
        tot_demoted += r["demoted"]; tot_cas += r["recall_casualties"]
    print("\n" + "#" * 80)
    print(f"AGGREGATE: demoted {tot_demoted} ubiq-only config pairs; RECALL CASUALTIES = {tot_cas}")
    print("A recall casualty is a REAL coupling (strong co-change) that the stoplist would silence")
    print("with no other edge backing it. 0 casualties => the demotion is recall-safe on real repos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
