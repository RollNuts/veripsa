#!/usr/bin/env python3
"""MEASURE config-coupling precision: are UBIQUITOUS-key-only config couplings spurious
(no co-change) vs SPECIFIC-key config couplings (elevated co-change)?

A config coupling = a pair of code files that BOTH read the same config key (a `reads_config`
edge to the same dst). The hypothesis (parallel to the proven ubiquitous-CALL-name fix): two
unrelated files both reading `timeout`/`debug`/`level` are NOT really coupled, whereas two files
reading `STRIPE_WEBHOOK_SECRET` / `DATABASE_POOL_SIZE` ARE.

Ground truth = co-change (lift), exactly the proxy recall_measure.py uses. For each config-coupled
file pair we compute lift; we then split pairs by whether they are coupled ONLY via ubiquitous keys
vs via at least one specific key, and compare the co-change distributions.

A key is treated as "ubiquitous" by a content-free shape test (the candidate stoplist), NOT by a
hand-list of these particular repos — so the measurement tests the rule, not the repos.

Run: python3 measure_config_precision.py /repo [/repo2 ...]
"""
from __future__ import annotations
import os, sys, subprocess
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X
from _cg_config import _is_ubiquitous_config_key  # candidate rule under test

HUB_DEGREE = int(os.environ.get("HUB_DEGREE", "8"))
MAX_COMMIT_FILES = int(os.environ.get("BACKTEST_MAX_COMMIT_FILES", "30"))


def res_hubs_config(edges, files):
    """config keys read by > HUB_DEGREE distinct files = resource hubs (engine already dampens these)."""
    indeg = {}
    for e in edges:
        if e["kind"] == "reads_config" and e["src"] in files:
            indeg.setdefault(e["dst"], set()).add(e["src"])
    return {d for d, srcs in indeg.items() if len(srcs) > HUB_DEGREE}


def config_pairs(g, files):
    """{frozenset(a,b): set(config_keys shared)} for code files coupled by a shared reads_config key.
    Mirrors the SQL pairing (files sharing the same dst). NO dampening here — we WANT the raw set so we
    can classify which pairs are ubiquitous-only."""
    by_key = {}
    for e in g["edges"]:
        if e["kind"] == "reads_config" and e["src"] in files:
            by_key.setdefault(e["dst"], set()).add(e["src"])
    pairs = {}
    for key, fs in by_key.items():
        fs = sorted(fs)
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                pairs.setdefault(frozenset((fs[i], fs[j])), set()).add(key)
    return pairs


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


def lift_of(pair, touch, T):
    a, b = tuple(pair)
    sa, sb = touch.get(a), touch.get(b)
    if not sa or not sb:
        return None, 0
    nco = len(sa & sb)
    if nco == 0:
        return 0.0, 0
    return (nco * T) / (len(sa) * len(sb)), nco


def analyze(repo):
    g = X.build_graph(repo)
    files = {n["path"] for n in g["nodes"] if n["kind"] == "file"}
    pairs = config_pairs(g, files)
    touch, T = commit_touchsets(repo, files)

    ubiq_only, has_specific = [], []   # each entry: (lift, nco)
    # also: which keys actually drive ubiquitous-only pairs (for the report)
    ubiq_keys_used, spec_keys_used = {}, {}
    for pair, keys in pairs.items():
        nonubiq = {k for k in keys if not _is_ubiquitous_config_key(k)}
        lift, nco = lift_of(pair, touch, T)
        if lift is None:
            continue   # a file with no history — cannot judge (UNKNOWN, excluded honestly)
        if nonubiq:
            has_specific.append((lift, nco))
            for k in nonubiq:
                spec_keys_used[k] = spec_keys_used.get(k, 0) + 1
        else:
            ubiq_only.append((lift, nco))
            for k in keys:
                ubiq_keys_used[k] = ubiq_keys_used.get(k, 0) + 1

    def stats(rows):
        n = len(rows)
        if n == 0:
            return dict(n=0, mean_lift=0, frac_cochange=0, frac_strong=0, median_lift=0)
        lifts = sorted(r[0] for r in rows)
        cochange = sum(1 for l, c in rows if c > 0)
        strong = sum(1 for l, c in rows if c >= 3 and l >= 2.0)   # the recall_measure "real coupling" bar
        return dict(n=n,
                    mean_lift=sum(lifts) / n,
                    median_lift=lifts[n // 2],
                    frac_cochange=cochange / n,
                    frac_strong=strong / n)

    return {
        "repo": os.path.basename(repo.rstrip("/")), "files": len(files), "commits": T,
        "total_config_pairs": len(pairs),
        "ubiq_only": stats(ubiq_only),
        "has_specific": stats(has_specific),
        "top_ubiq_keys": sorted(ubiq_keys_used.items(), key=lambda kv: -kv[1])[:12],
        "top_spec_keys": sorted(spec_keys_used.items(), key=lambda kv: -kv[1])[:12],
    }


def main():
    repos = [os.path.abspath(p) for p in sys.argv[1:]] or [ROOT]
    rows = [analyze(r) for r in repos]
    print("\n=== CONFIG-COUPLING PRECISION MEASUREMENT (ground truth = co-change lift) ===")
    agg_u = dict(n=0, co=0, strong=0, liftsum=0.0)
    agg_s = dict(n=0, co=0, strong=0, liftsum=0.0)
    for r in rows:
        u, s = r["ubiq_only"], r["has_specific"]
        print("\n" + "=" * 90)
        print(f"{r['repo']}  —  {r['files']} files, {r['commits']} commits, {r['total_config_pairs']} config-coupled pairs")
        print("=" * 90)
        print(f"  UBIQUITOUS-KEY-ONLY couplings: {u['n']:>5} pairs   "
              f"mean lift {u['mean_lift']:.2f}   median {u['median_lift']:.2f}   "
              f"co-change>0 {u['frac_cochange']*100:.0f}%   STRONG(co>=3,lift>=2) {u['frac_strong']*100:.1f}%")
        print(f"  HAS-A-SPECIFIC-KEY couplings:  {s['n']:>5} pairs   "
              f"mean lift {s['mean_lift']:.2f}   median {s['median_lift']:.2f}   "
              f"co-change>0 {s['frac_cochange']*100:.0f}%   STRONG(co>=3,lift>=2) {s['frac_strong']*100:.1f}%")
        print(f"  top ubiquitous keys driving ubiq-only pairs: {[k for k,_ in r['top_ubiq_keys']]}")
        print(f"  top specific keys:                           {[k for k,_ in r['top_spec_keys']]}")
        agg_u["n"] += u["n"]; agg_u["liftsum"] += u["mean_lift"] * u["n"]
        agg_u["strong"] += u["frac_strong"] * u["n"]; agg_u["co"] += u["frac_cochange"] * u["n"]
        agg_s["n"] += s["n"]; agg_s["liftsum"] += s["mean_lift"] * s["n"]
        agg_s["strong"] += s["frac_strong"] * s["n"]; agg_s["co"] += s["frac_cochange"] * s["n"]

    print("\n" + "#" * 90)
    print("AGGREGATE across repos")
    print("#" * 90)
    if agg_u["n"]:
        print(f"  UBIQUITOUS-ONLY: {agg_u['n']} pairs  mean lift {agg_u['liftsum']/agg_u['n']:.2f}  "
              f"co-change {agg_u['co']/agg_u['n']*100:.0f}%  STRONG {agg_u['strong']/agg_u['n']*100:.1f}%")
    if agg_s["n"]:
        print(f"  HAS-SPECIFIC:    {agg_s['n']} pairs  mean lift {agg_s['liftsum']/agg_s['n']:.2f}  "
              f"co-change {agg_s['co']/agg_s['n']*100:.0f}%  STRONG {agg_s['strong']/agg_s['n']*100:.1f}%")
    print("\nINTERPRETATION: if UBIQUITOUS-ONLY pairs have markedly LOWER lift / strong-co-change than")
    print("HAS-SPECIFIC pairs, the ubiquitous-key config couplings are spurious and a content-free")
    print("ubiquitous-key STOPLIST (recall-safe: only drops ubiq-ONLY pairs) is justified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
