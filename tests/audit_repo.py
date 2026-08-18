#!/usr/bin/env python3
"""Import-graph QUALITY auditor — point it at any repo, get a precision/recall report.

This is the 'run it on your repo before buying' sales hook AND our regression guard for extraction quality.
It is NOT a hermetic gate (it needs a real checkout), so it is run by hand, not by run_gates.sh:

  python3 tests/audit_repo.py <path-to-a-repo-checkout>

It measures, content-free, on the RESOLVED file→file import edges Veripsa would use for blast-radius:
  • resolved internal edges          — the real cross-file couplings found (recall signal)
  • basename fan-outs                — one import resolving to MULTIPLE same-basename files (a measured number)
  • cross-tree src↔test couplings    — a `src/` file 'importing' a `tests/` file (a measured number)
  • relative-import recall           — a RELATIVE code import (./x, ../x to a source file) that did NOT resolve
                                       (a missed coupling — but asset imports .css/.png/.svg are excluded,
                                       they are correctly not code nodes)

THE VERDICT REFLECTS WHAT THE CUSTOMER ACTUALLY EXPERIENCES (post-dampening) — NOT the raw substrate.
The resolver is DELIBERATELY recall-biased: an ambiguous bare name fans out to EVERY same-basename match,
and the live engine then DAMPENS that recall before any customer warn. A naive raw count over-reports —
it makes Veripsa look worse than what ships. So, mirroring tests/audit_realrepos.py AND the engine's own
_claim_adjacency (db/schema/70_social.sql), the verdict counts only the edges that SURVIVE dampening:

  • SAME-STEM fan-out (e.g. `models` → many `models.py`, or a KMP symbol per source-set) is correct recall,
    NOT a precision bug — the SAME logical symbol / a duplicate-named module. MEASURED, never flagged.
    Only a DIFFERENT-STEM fan-out (one import → files with different stems) is a genuine mis-resolution.
  • A HUB target (a file imported by > the hub cutoff distinct files) is DROPPED by the engine
    (`ce.dst NOT IN hub_files`), so a fan-out / src→test edge INTO a hub never reaches a customer.
    Such edges are MEASURED, never flagged.
  • A src→test edge is a real FALSE edge only when the test file is the SOLE same-basename resolution for
    that importer (no correct non-test sibling co-resolved) AND the target is not a hub. A recall-biased
    extra alongside a correct sibling is by design, dropped before warn.

So a CLEAN verdict == the engine ships clean on this repo; ISSUES names only genuine surviving false edges
(different-stem fan-outs or sole-non-hub src→test). The raw counts are still printed as detail.
(Found+fixed two Python precision bugs this way on flask: 12 false src→test edges → 0; commit 1d63d3f.)
"""
from __future__ import annotations

import collections
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402

_CODE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rb", ".php", ".cs", ".rs",
             ".c", ".h", ".cpp", ".cc", ".hpp", ".java", ".kt", ".swift")
_TEST_SEG = ("test", "tests", "spec", "__tests__", "examples", "example")
# The live engine treats a file imported by MORE than this many DISTINCT files as a HUB and DROPS edges into
# it (db/schema/70_social.sql `hub_files`: `HAVING count(DISTINCT src) > veripsa.hub_degree`, GUC default 8),
# so a fan-out / src→test edge whose TARGET is a hub never reaches a customer warn. Mirror that cutoff here so
# the CLI verdict == post-dampening reality. (Tunable per deployment via the GUC; 8 is the shipped default.)
_HUB_DEGREE = 8


def _noext(p: str) -> str:
    for x in (".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".py", ".go", ".rb", ".php", ".cs", ".rs",
              ".kt", ".swift", ".java"):
        if p.endswith(x):
            return p[: -len(x)]
    return os.path.splitext(p)[0]


def _is_test_path(p: str) -> bool:
    return any(seg in _TEST_SEG for seg in p.split("/"))


def audit(root: str) -> dict:
    g = X.build_graph(root)
    files = [n["path"] for n in g["nodes"] if n.get("kind") == "file"]
    fp = set(files)
    code_files = [p for p in files if p.endswith(_CODE_EXT)]
    imp = [e for e in g["edges"] if e["kind"] == "imports"]
    resolved = [(e["src"], e["dst"]) for e in imp if e["dst"] in fp]            # file→file (the real couplings)

    # HUB files = the engine's `hub_files`: a file imported by > _HUB_DEGREE DISTINCT files. The engine drops
    # edges INTO these (the wall-of-noise guard), so any fan-out / src→test edge whose TARGET is a hub is
    # dampened before any customer warn. Computed here so the verdict reflects post-dampening reality.
    indeg = collections.defaultdict(set)
    for s, d in resolved:
        indeg[d].add(s)
    hub_files = {d for d, srcs in indeg.items() if len(srcs) > _HUB_DEGREE}

    # basename fan-out smell — EXCLUDE package markers (__init__/index): `import pkg` and `from pkg.sub`
    # legitimately resolve to TWO __init__/index files from DIFFERENT module names (not one import fanning out).
    fan = collections.defaultdict(set)
    for s, d in resolved:
        stem = _noext(os.path.basename(d))
        if stem in ("__init__", "index"):
            continue
        fan[(s, os.path.basename(d))].add(d)
    fanouts = {k: sorted(v) for k, v in fan.items() if len(v) > 1}
    # DIFFERENT-STEM fan-out = the genuine mis-resolution (one import → files with DIFFERENT stems). A
    # SAME-STEM fan-out (one import → N files all sharing the basename: duplicate-named modules, a KMP symbol
    # per source-set) is the resolver's deliberate recall, NOT a precision bug — and the engine dampens any
    # widely-imported (hub) target. Mirror tests/audit_realrepos.py: only diff-stem counts as a real false edge.
    # A diff-stem fan-out whose NON-hub targets all collapse to <=1 distinct stem is dampened in practice too,
    # so subtract hub targets before classifying (an import that fans out only INTO hubs reaches no warn).
    diff_stem_fanouts = {}
    for k, v in fanouts.items():
        surviving = [x for x in v if x not in hub_files]        # hub targets are dropped by the engine
        if len({_noext(os.path.basename(x)) for x in surviving}) > 1:
            diff_stem_fanouts[k] = v

    src2test = [(s, d) for s, d in resolved if not _is_test_path(s) and _is_test_path(d)]
    # A production→test edge is a real FALSE edge (reaches a customer) only when BOTH:
    #   • it is the SOLE same-basename resolution for that importer — no correct NON-test sibling co-resolved
    #     (a recall-biased extra alongside a correct sibling is by design, dropped before warn); AND
    #   • the target is NOT a hub (the engine drops edges into hubs).
    # Anything else is a dampened recall-biased extra, MEASURED but not flagged.
    src2test_real = []
    for s, d in src2test:
        siblings = fan.get((s, os.path.basename(d)), {d})
        has_nontest_sibling = any(not _is_test_path(x) for x in siblings)
        if not has_nontest_sibling and d not in hub_files:
            src2test_real.append((s, d))

    # relative-import recall: a code import to a code file that did not resolve (assets excluded)
    file_noext = {_noext(p): p for p in fp}
    rel_miss = []
    for e in imp:
        d = (e["dst"] or "")
        if d.startswith(("./", "../")) and d.endswith(_CODE_EXT) and e["dst"] not in fp:
            nd = _noext(os.path.normpath(os.path.join(os.path.dirname(e["src"]), d)))
            if nd in file_noext:
                rel_miss.append((e["src"], d, file_noext[nd]))

    return {
        "files": len(files), "code_files": len(code_files),
        "import_edges": len(imp), "resolved_internal": len(resolved),
        "hub_files": sorted(hub_files),
        "basename_fanouts": fanouts, "diff_stem_fanouts": diff_stem_fanouts,
        "src_to_test": src2test, "src_to_test_real": src2test_real,
        "relative_recall_misses": rel_miss,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python3 tests/audit_repo.py <repo-checkout-path>"); return 2
    root = sys.argv[1]
    if not os.path.isdir(root):
        print(f"not a directory: {root}"); return 2
    r = audit(root)
    n_fan, n_diff = len(r["basename_fanouts"]), len(r["diff_stem_fanouts"])
    n_s2t, n_s2t_real = len(r["src_to_test"]), len(r["src_to_test_real"])
    n_rel = len(r["relative_recall_misses"])
    print(f"== Veripsa import-graph audit: {root} ==")
    print(f"  files={r['files']} (code={r['code_files']})  import_edges={r['import_edges']}  "
          f"resolved_internal={r['resolved_internal']}  hub_files={len(r['hub_files'])} (in-degree>{_HUB_DEGREE})")
    # RAW counts are shown as DETAIL; the engine-reality (post-dampening) count drives the verdict.
    print(f"  basename fan-outs (raw)            : {n_fan}  →  different-stem (real false): {n_diff}")
    for (s, b), v in list(r["diff_stem_fanouts"].items())[:8]:
        print(f"      [REAL] {s} -> *{b}: {v}")
    same_stem = [(k, v) for k, v in r["basename_fanouts"].items() if k not in r["diff_stem_fanouts"]]
    for (s, b), v in same_stem[:4]:
        print(f"      [recall-biased, dampened] {s} -> *{b}: {v}")
    print(f"  src->test couplings (raw)          : {n_s2t}  →  real (sole, non-hub): {n_s2t_real}")
    real_set = set(r["src_to_test_real"])
    for s, d in r["src_to_test"][:8]:
        tag = "[REAL false edge]" if (s, d) in real_set else "[recall-biased, dampened]"
        print(f"      {tag} {s} -> {d}")
    print(f"  relative-import recall misses      : {n_rel}")
    for s, d, f in r["relative_recall_misses"][:8]:
        print(f"      {s} -> {d}  (should be {f})")

    # VERDICT == what the customer actually experiences (post-dampening), NOT the raw substrate. Only edges
    # that SURVIVE the engine's dampening count: different-stem fan-outs and sole-non-hub src→test false edges.
    # Same-stem fan-outs and recall-biased extras (which the engine drops before any warn) are detail, not a fault.
    real_false_edges = n_diff + n_s2t_real
    if r["resolved_internal"] == 0:
        verdict = "NO-COUPLINGS"
    elif real_false_edges == 0 and n_rel == 0:
        verdict = "CLEAN"
    else:
        verdict = "ISSUES"
    print(f"  VERDICT: {verdict}", end="")
    if verdict == "CLEAN" and (n_fan or n_s2t):
        print(f"  (engine-reality: {n_fan} raw fan-out + {n_s2t} raw src→test are recall-biased & "
              f"hub-dampened before any customer warn — 0 survive)")
    elif verdict == "ISSUES":
        print(f"  ({real_false_edges} edge(s) survive dampening: {n_diff} different-stem fan-out + "
              f"{n_s2t_real} sole-non-hub src→test"
              + (f" + {n_rel} relative recall miss" if n_rel else "") + ")")
    else:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
