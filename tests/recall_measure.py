#!/usr/bin/env python3
"""AUDIT3 RECALL — what fraction of REAL coupling does the LIVE engine warn on, and what does it MISS?

Two ground truths of coupling on each real repo:
  (1) STATIC  — known A->B import pairs constructed from the extracted graph: assert the engine's
                adjacency (WITH hub dampening, exactly as core._claim_adjacency runs it) detects them.
  (2) EMPIRICAL — file pairs that historically co-change a LOT (the standard empirical proxy for real
                coupling): of those, what fraction does the engine's dampened adjacency cover (recall)?

NOTE: this mirrors core._claim_adjacency's hub dampening + the ≤3 def fan-out and uses a REPRESENTATIVE
stoplist (below) that APPROXIMATES the shipping engine's — it is NOT a verbatim copy of it. The exact
shipping stoplist lives in db/schema/70_social.sql (kept in lockstep with the co-change backtest by the
stoplist-sync gate, gates.d/154-stoplist_sync.gate); this STOP set predates the #365 builtin block, so the
number here CLOSELY ESTIMATES — does not exactly replicate — what the SHIPPING product warns on. (A true
4-way sync of this STOP into gate 154 is an optional deeper fix; these numbers back no customer claim — the
listing's "several-fold co-change" comes from the synced backtest, not this tool.) The gap between
raw-graph adjacency and dampened adjacency is the dampening's recall cost, quantified.

Honest boundary: this measures the static-code-graph's recall against co-change. Co-change is a proxy
(it also captures coupling the code graph fundamentally can't see: value-coupling, registry/DI, etc.),
so 100% is neither expected nor the goal. The POINT is to put a NUMBER on it + name the dominant miss.

Run: python3 recall_measure.py /repo [/repo2 ...]
"""
from __future__ import annotations
import os, sys, subprocess
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X

STOP = {
    "main", "run", "setup", "teardown", "handle", "dispatch", "register", "wrap",
    "to_s", "to_str", "tostring", "str", "repr", "inspect", "format", "print", "println", "puts", "log", "warn", "debug", "trace",
    "equals", "eql?", "hash", "hashcode", "compareto", "compare", "cmp",
    "empty?", "blank?", "present?", "nil?", "valid?", "include?", "contains", "respond_to?", "key?", "has_key?",
    "to_a", "to_h", "to_sym", "to_i", "to_json",
    "map", "each", "collect", "select", "reject", "filter", "reduce", "merge", "flatten", "zip",
    "freeze", "dup", "clone", "tap", "then", "send", "call", "apply", "yield",
    "it", "its", "describe", "context", "before", "after", "around", "expect", "should", "assert", "refute", "let", "subject", "mock", "stub",
}
HUB_DEGREE = int(os.environ.get("HUB_DEGREE", "8"))
MAX_COMMIT_FILES = int(os.environ.get("BACKTEST_MAX_COMMIT_FILES", "30"))


def hub_files(g, files):
    """files imported by > HUB_DEGREE distinct files (resolved to a real file) = dampened hubs."""
    indeg = {}
    for e in g["edges"]:
        if e["kind"] == "imports" and e["dst"] in files:
            indeg.setdefault(e["dst"], set()).add(e["src"])
    return {d for d, srcs in indeg.items() if len(srcs) > HUB_DEGREE}


def res_hubs(g, files):
    indeg = {}
    for e in g["edges"]:
        if e["kind"] in ("queries", "alters", "reads_config"):
            indeg.setdefault(e["dst"], set()).add(e["src"])
    return {d for d, srcs in indeg.items() if len(srcs) > HUB_DEGREE}


def adjacency(g, files, apply_damp):
    """Undirected file pairs the engine would treat as coupled. apply_damp=True mirrors the LIVE engine
    (hub + resource dampening); False = the raw graph (the upper bound). Returns {frozenset(a,b): types}."""
    hubs = hub_files(g, files) if apply_damp else set()
    rhubs = res_hubs(g, files) if apply_damp else set()
    defs = {}
    for n in g["nodes"]:
        if n.get("kind") in ("def", "class") and n.get("name"):
            nm = n["name"]
            if not nm.startswith("__") and nm.lower() not in STOP:
                defs.setdefault(nm, set()).add(n["path"])
    defs_ok = {nm: fs for nm, fs in defs.items() if len(fs) <= 3}
    by_dst, pairs = {}, {}

    def add(a, b, t):
        if a != b and a in files and b in files:
            pairs.setdefault(frozenset((a, b)), set()).add(t)

    for e in g["edges"]:
        k = e["kind"]
        if k == "imports" and e["dst"] in files:
            if apply_damp and e["dst"] in hubs:
                continue                                  # hub dampening: don't couple to a hub you import
            add(e["src"], e["dst"], "import")
        elif k == "calls" and e["src"] in files:
            for f in defs_ok.get(e["dst"], ()):
                if apply_damp and f in hubs:
                    continue                              # don't couple to a hub you merely call into
                add(e["src"], f, "call")
        elif k in ("queries", "alters", "reads_config") and e["src"] in files:
            if apply_damp and e["dst"] in rhubs:
                continue
            by_dst.setdefault(("schema" if k != "reads_config" else "config", e["dst"]), set()).add(e["src"])
    for (t, _), fs in by_dst.items():
        fs = list(fs)
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                add(fs[i], fs[j], t)
    return pairs


def commit_touchsets(repo, files):
    out = subprocess.run(["git", "-C", repo, "log", "--no-merges", "--name-only", "--pretty=format:@@%H"],
                         capture_output=True, text=True).stdout
    touch, idx, cur = {}, -1, []
    T = set()

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
    raw = adjacency(g, files, apply_damp=False)
    live = adjacency(g, files, apply_damp=True)
    touch, T = commit_touchsets(repo, files)

    # --- (2) EMPIRICAL ground truth: pairs that co-change a LOT (lift >= 2 AND co-change >= 3 commits)
    # over files BOTH with history. These are real, repeated co-movements — the coupling we should warn on.
    hist = [f for f in files if touch.get(f)]
    gt = []   # ground-truth coupled pairs (a,b)
    # restrict candidate universe to pairs that ever co-change (cheap) then threshold by lift.
    co = {}
    # build per-commit -> files (bounded), invert from touch
    commit_files = {}
    for f, cs in touch.items():
        for ci in cs:
            commit_files.setdefault(ci, []).append(f)
    for ci, fs in commit_files.items():
        if len(fs) < 2:
            continue
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                co[frozenset((fs[i], fs[j]))] = co.get(frozenset((fs[i], fs[j])), 0) + 1
    for key, nco in co.items():
        a, b = tuple(key)
        sa, sb = touch[a], touch[b]
        lift = (nco * T) / (len(sa) * len(sb))
        if nco >= 3 and lift >= 2.0:          # strong, repeated co-change = real coupling
            gt.append(key)

    def covered(pairset, gtset):
        return sum(1 for k in gtset if k in pairset)

    n_gt = len(gt)
    raw_cov = covered(raw, gt)
    live_cov = covered(live, gt)
    # miss classification on the LIVE-missed ground-truth pairs
    missed_live = [k for k in gt if k not in live]
    # how many of those WOULD raw catch (i.e. dampening killed them)?
    damp_killed = sum(1 for k in missed_live if k in raw)
    # of the rest (raw also misses): cross-dir vs same-dir, and whether ANY edge connects them
    def dirof(p):
        return os.path.dirname(p)
    no_edge_xdir = sum(1 for k in missed_live if k not in raw and len(set(dirof(x) for x in k)) == 2)
    no_edge_samedir = sum(1 for k in missed_live if k not in raw and len(set(dirof(x) for x in k)) == 1)

    # --- (1) STATIC ground truth: every direct import A->B (resolved) is a known coupling; live MUST
    # detect it UNLESS the target is a dampened hub. Count import pairs + how many dampening dropped.
    import_pairs = {frozenset((e["src"], e["dst"])) for e in g["edges"]
                    if e["kind"] == "imports" and e["dst"] in files and e["src"] != e["dst"]}
    import_live = sum(1 for k in import_pairs if k in live)
    import_damped = sum(1 for k in import_pairs if k in raw and k not in live)

    return {
        "repo": os.path.basename(repo.rstrip("/")), "files": len(files), "commits": T,
        "raw_pairs": len(raw), "live_pairs": len(live),
        "dampened_pairs": len(raw) - len(live),
        "n_gt": n_gt, "raw_cov": raw_cov, "live_cov": live_cov,
        "damp_killed_gt": damp_killed,
        "miss_noedge_xdir": no_edge_xdir, "miss_noedge_samedir": no_edge_samedir,
        "import_pairs": len(import_pairs), "import_live": import_live, "import_damped": import_damped,
    }


def main():
    repos = [os.path.abspath(p) for p in sys.argv[1:]] or [ROOT]
    rows = [analyze(r) for r in repos]
    print("\n=== AUDIT3 RECALL (live engine WITH hub dampening, cutoff %d) ===" % HUB_DEGREE)
    for r in rows:
        gt = r["n_gt"] or 1
        raw_rec = r["raw_cov"] / gt * 100
        live_rec = r["live_cov"] / gt * 100
        print("\n" + "=" * 88)
        print(f"{r['repo']}  —  {r['files']} files, {r['commits']} commits")
        print("=" * 88)
        print(f"  adjacency pairs: raw graph {r['raw_pairs']:>5}   live (dampened) {r['live_pairs']:>5}   "
              f"→ DAMPENING DROPS {r['dampened_pairs']} pairs ({r['dampened_pairs']/max(r['raw_pairs'],1)*100:.0f}%)")
        print(f"  STATIC: {r['import_pairs']} direct import pairs — live WARNS on {r['import_live']}, "
              f"DAMPENING suppresses {r['import_damped']} real direct imports from 'warn' "
              f"(post-AUDIT3 these render honest 'unknown' + dampened_with when both endpoints are in-flight — NOT a silent 'clear')")
        print(f"  EMPIRICAL ground truth (co-change ≥3 commits AND lift ≥2): {r['n_gt']} pairs")
        print(f"    RAW-graph recall  {raw_rec:5.1f}%   ({r['raw_cov']}/{r['n_gt']})")
        print(f"    LIVE recall (shipping, dampened) {live_rec:5.1f}%   ({r['live_cov']}/{r['n_gt']})")
        print(f"    → hub dampening costs {raw_rec - live_rec:.1f} pts of recall on real co-change "
              f"({r['damp_killed_gt']} GT pairs)")
        print(f"  DOMINANT MISS CLASS of the {r['n_gt']-r['live_cov']} live-missed GT pairs:")
        print(f"    dampening-killed (edge existed): {r['damp_killed_gt']}")
        print(f"    no-edge cross-dir (graph blind): {r['miss_noedge_xdir']}")
        print(f"    no-edge same-dir (graph blind):  {r['miss_noedge_samedir']}")
    print("\nHONEST BOUNDARY: recall is measured against co-change, a PROXY for real coupling that also")
    print("captures couplings a static code graph cannot see (value/registry/DI). 100% is not the target;")
    print("the dampening-killed column is the part Veripsa HAD and threw into a confident 'clear'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
