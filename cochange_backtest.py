#!/usr/bin/env python3
"""EFFECT backtest ENGINE — does Veripsa warn about files that are ACTUALLY entangled? (PO: 動く≠効果)

This is the reusable, NON-TEST core of the co-change backtest. It is imported by BOTH the product / sales
tooling (evaluate.py — the "try Veripsa on YOUR repo before you install" report) AND the gated harness
(tests/backtest_cochange.py — the thin CLI/runner that proves the signal on real history). Keeping the engine in
a top-level product module means product code never imports from tests/ (the auditor's src→test smell).

'It detects' ≠ 'it has effect'. Veripsa's value = warning on a structural A→B coupling BEFORE merge prevents
real downstream rework. The necessary core of that claim is testable for free against real history: if the
file pairs Veripsa flags as coupled (its graph adjacency — imports, cross-file calls, shared table/config)
genuinely CO-CHANGE in the repo's commit history far more than chance, the warnings point at real
entanglement (a steer worth taking), not noise. Co-change is the standard empirical proxy for real coupling.

RIGOR (the obvious rebuttal: "coupled files are just in the same folder"): we beat TWO baselines —
  * random        — any two files (the loose control).
  * same-directory — two files in the SAME folder (the HARD control: co-location alone).
and we report the CROSS-DIRECTORY coupled pairs separately — coupling a folder heuristic CANNOT see, where
the code graph is the only thing that knows A and B are entangled. That cross-dir lift is the differentiated
proof. Also broken down per coupling type (import / call / schema / config).

This does NOT prove rework-hours-saved (needs a deployed A/B over time) — it proves the SIGNAL IS REAL, the
precondition for any effect. Honest boundary printed (by the CLI in tests/backtest_cochange.py).
"""
from __future__ import annotations

import os
import random
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402

# MUST mirror core._claim_adjacency's stoplist (universal protocol/builtin/test-DSL method names — not
# dependencies). Domain names are untouched.
# NOTE on the BUILTIN block + min-length: in the SQL engine these (and the <=2-char guard) drop noise from the
# import-UNCONFIRMED single-definer branch ONLY (import-confirmed couplings on these names survive). This coarse
# backtest model does not separate imported vs un-imported call pairs (coupled_pairs fans a call to all <=3
# definers regardless of import), so it applies the same name-set GLOBALLY — a faithful approximation because the
# noise these builtin/short names create here is overwhelmingly the un-imported single-definer match the engine
# now drops; removing it keeps the cross-directory lift honest (drops false couples, not real co-changing ones).
STOP = {
    "main", "run", "setup", "teardown", "handle", "dispatch", "register", "wrap",
    "to_s", "to_str", "tostring", "str", "repr", "inspect", "format", "print", "println", "puts", "log", "warn", "debug", "trace",
    "equals", "eql?", "hash", "hashcode", "compareto", "compare", "cmp",
    "empty?", "blank?", "present?", "nil?", "valid?", "include?", "contains", "respond_to?", "key?", "has_key?",
    "to_a", "to_h", "to_sym", "to_i", "to_json",
    "map", "each", "collect", "select", "reject", "filter", "reduce", "merge", "flatten", "zip",
    "freeze", "dup", "clone", "tap", "then", "send", "call", "apply", "yield",
    "it", "its", "describe", "context", "before", "after", "around", "expect", "should", "assert", "refute", "let", "subject", "mock", "stub",
    # BUILTIN / STDLIB / container / IO method names (audit 2026-06-21, flask/nestjs/hugo) — the bare attr tail of
    # Promise.all / JSON.stringify / dict.pop / Object.assign / strings.HasPrefix / sync.Once.Do / json.NewDecoder.
    # CRUD-domain verbs (create/build/find/new/save/update/delete/process/validate) are DELIBERATELY EXCLUDED — and
    # so are the DOMAIN-INTENT verbs get/set/add/read/write/parse/decode/encode/load (RECALL audit r4 2026-06-21:
    # #365 over-reached and listed these; they carry real cross-file intent → restored). ONLY pure language/stdlib
    # method tails remain. MUST stay byte-identical to the SQL #365 BUILTIN block (gate asserts the two are in sync).
    "pop", "push", "shift", "unshift", "slice", "splice", "all", "any", "stringify",
    "keys", "values", "items", "entries", "has", "join", "split", "assign",
    "hasprefix", "hassuffix", "do", "newdecoder", "newencoder", "marshal", "unmarshal",
    "sprintf", "printf", "fprintf", "sprint", "sprintln", "close", "open", "dumps", "loads",
}

# MIN-NAME-LENGTH guard (mirrors the SQL `char_length(ce.dst) > 2`): a call target <=2 chars (m/a/f/x) is never a
# meaningful cross-file coupling anchor — drop it from the resolvable-definer set so it cannot fan out a false couple.
MIN_NAME_LEN = 3


def coupled_pairs(g, files):
    """Veripsa's graph adjacency as undirected file pairs → {frozenset(A,B): set(types)} (what it warns on)."""
    defs = {}
    for n in g["nodes"]:
        if n.get("kind") in ("def", "class") and n.get("name"):
            nm = n["name"]
            if not nm.startswith("__") and len(nm) >= MIN_NAME_LEN and nm.lower() not in STOP:
                defs.setdefault(nm, set()).add(n["path"])
    defs_ok = {nm: fs for nm, fs in defs.items() if len(fs) <= 3}  # engine's ≤3 fan-out
    by_dst, pairs = {}, {}

    def add(a, b, t):
        if a != b and a in files and b in files:
            pairs.setdefault(frozenset((a, b)), set()).add(t)

    for e in g["edges"]:
        k = e["kind"]
        if k == "imports" and e["dst"] in files:
            add(e["src"], e["dst"], "import")
        elif k == "calls" and e["src"] in files:
            for f in defs_ok.get(e["dst"], ()):
                add(e["src"], f, "call")
        elif k in ("queries", "alters", "reads_config") and e["src"] in files:
            by_dst.setdefault(("schema" if k != "reads_config" else "config", e["dst"]), set()).add(e["src"])
    for (t, _), fs in by_dst.items():
        fs = list(fs)
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                add(fs[i], fs[j], t)
    return pairs


# Sweeping/mechanical commits (mass formatting, dependency bumps, license headers, generated code, tree-wide
# refactors) are NOT coupling-driven co-change, yet a handful of them dominate the pair count and inflate the
# RANDOM baseline — burying the real signal. Measured on tikv: 1% of commits touch >50 files but produce 84%
# of all co-change pairs. Exclude commits touching more than this many TRACKED files. Configurable; default 30
# is ~10x the typical commit (medians observed: 3) so normal feature/coupling commits are kept.
_MAX_COMMIT_FILES = int(os.environ.get("BACKTEST_MAX_COMMIT_FILES", "30"))


def commit_touchsets(repo, files, max_commit_files=_MAX_COMMIT_FILES):
    """file → set(commit_index) it was touched in; plus T = #commits that touched ≥1 tracked file (the lift
    universe). Non-merge commits, restricted to files present at HEAD. Commits touching > max_commit_files
    tracked files are EXCLUDED as mechanical mass-edits (not real coupling co-change)."""
    out = subprocess.run(["git", "-C", repo, "log", "--no-merges", "--name-only", "--pretty=format:@@%H"],
                         capture_output=True, text=True).stdout
    touch, idx, touching = {}, -1, set()
    cur = []

    def flush():                                    # commit the buffered commit unless it is a mass-edit
        if cur and len(cur) <= max_commit_files:
            for f in cur:
                touch.setdefault(f, set()).add(idx)
            touching.add(idx)

    for line in out.splitlines():
        if line.startswith("@@"):
            flush(); cur = []; idx += 1
        elif idx >= 0 and line in files:
            cur.append(line)
    flush()
    return touch, len(touching)


def _pair_stats(pairlist, touch, T):
    """(ever-co-change rate, median lift) over a list of (a,b) pairs both with history."""
    lifts, anyco = [], 0
    for a, b in pairlist:
        sa, sb = touch.get(a), touch.get(b)
        if not sa or not sb:
            continue
        nb = len(sa & sb)
        anyco += 1 if nb else 0
        lifts.append((nb * T) / (len(sa) * len(sb)) if nb else 0.0)
    lifts.sort()
    rate = anyco / len(lifts) if lifts else 0.0
    med = lifts[len(lifts) // 2] if lifts else 0.0
    return rate, med, len(lifts)


def analyze(repo):
    g = X.build_graph(repo)
    files = {n["path"] for n in g["nodes"] if n["kind"] == "file"}
    pairs = coupled_pairs(g, files)
    touch, T = commit_touchsets(repo, files)
    hist = [f for f in files if touch.get(f)]
    samedir = {}
    for f in hist:
        samedir.setdefault(os.path.dirname(f), []).append(f)

    def dirof(p):
        return os.path.dirname(p)

    coupled = [tuple(p) for p in pairs if all(touch.get(f) for f in p)]
    coupled_x = [(a, b) for a, b in coupled if dirof(a) != dirof(b)]   # cross-directory (folder-blind) subset
    random.seed(7)

    # baseline: random any-pairs (same count as coupled), excluding coupled.
    # `len(hist) >= 2` GUARD: random.sample(hist, 2) raises ValueError on a DEGENERATE repo with <2
    # files-with-history (empty / non-git / single-file). Below the floor the random baseline is
    # undefined, so skip sampling (rnd stays empty -> a zero baseline) instead of crashing. analyze()
    # must be robust for EVERY caller (evaluate.py sales tool, sibling_symbol_probe, gate 100) -- the
    # root fix, not a guard at one call site.
    rnd, seen = [], set()
    tries = 0
    while len(rnd) < len(coupled) and len(hist) >= 2 and tries < len(coupled) * 60 + 100:
        tries += 1
        a, b = random.sample(hist, 2)
        key = frozenset((a, b))
        if key in pairs or key in seen:
            continue
        seen.add(key); rnd.append((a, b))
    # baseline: same-directory random pairs (the HARD control — co-location alone)
    sd_pool = [d for d, fs in samedir.items() if len(fs) >= 2]
    sd = []
    tries = 0
    while len(sd) < len(coupled) and sd_pool and tries < len(coupled) * 60 + 100:
        tries += 1
        d = random.choice(sd_pool)
        a, b = random.sample(samedir[d], 2)
        if frozenset((a, b)) in pairs:
            continue
        sd.append((a, b))
    # baseline: cross-directory random pairs (fair control for the cross-dir coupled subset)
    rx, seen = [], set()
    tries = 0
    while len(rx) < max(len(coupled_x), 1) and len(hist) >= 2 and tries < (len(coupled_x) + 50) * 80:
        tries += 1
        a, b = random.sample(hist, 2)
        if dirof(a) == dirof(b) or frozenset((a, b)) in pairs or frozenset((a, b)) in seen:
            continue
        seen.add(frozenset((a, b))); rx.append((a, b))

    by_type = {}
    for t in ("import", "call", "schema", "config"):
        tp = [tuple(p) for p, ts in pairs.items() if t in ts and all(touch.get(f) for f in p)]
        by_type[t] = (_pair_stats(tp, touch, T), len(tp))

    return {
        "repo": os.path.basename(repo.rstrip("/")), "files": len(files), "commits": T,
        "pairs": len(coupled), "pairs_x": len(coupled_x),
        "coupled": _pair_stats(coupled, touch, T),
        "coupled_x": _pair_stats(coupled_x, touch, T),
        "rnd": _pair_stats(rnd, touch, T),
        "samedir": _pair_stats(sd, touch, T),
        "rnd_x": _pair_stats(rx, touch, T),
        "by_type": by_type,
    }


def main(argv=None) -> int:
    argv = sys.argv if argv is None else argv
    repos = [os.path.abspath(p) for p in argv[1:]] or [ROOT]
    rows = []
    overall_pass = True
    for repo in repos:
        r = analyze(repo)
        rows.append(r)
        c_rate, c_med, _ = r["coupled"]
        rr, _, _ = r["rnd"][0], r["rnd"][1], r["rnd"][2]
        sd_rate = r["samedir"][0]
        cx_rate, cx_med, cx_n = r["coupled_x"]
        rx_rate = r["rnd_x"][0]
        print("\n" + "=" * 84)
        print(f"{r['repo']}  —  {r['files']} files, {r['commits']} commits, {r['pairs']} coupled pairs "
              f"({r['pairs_x']} cross-dir)")
        print("=" * 84)
        beats_rand = c_rate > rr * 1.3
        graph_adds = cx_rate > rx_rate * 1.3       # THE bar: value where folders are blind (cross-dir)
        print(f"  EVER co-change   coupled {c_rate*100:5.1f}%   vs random {rr*100:5.1f}%   "
              f"→ beats random: {'YES' if beats_rand else 'no'} ({(c_rate/rr if rr else 0):.1f}x)")
        print(f"  (same-dir co-location baseline {sd_rate*100:.1f}% — already strong; folder proximity is a known "
              f"cue, NOT what we sell against)")
        print(f"  ► DIFFERENTIATED CATCH — CROSS-DIRECTORY coupling (folder/text heuristics are BLIND here):")
        print(f"       coupled {cx_rate*100:5.1f}%  vs cross-dir random {rx_rate*100:5.1f}%   "
              f"→ {(cx_rate/rx_rate if rx_rate else float('inf')):.1f}x   (n={cx_n} pairs)")
        print("  by coupling type (ever co-change):  " +
              "   ".join(f"{t} {r['by_type'][t][0][0]*100:.0f}% (n={r['by_type'][t][1]})" for t in ("import", "call", "schema", "config")))
        rp = beats_rand and graph_adds
        overall_pass = overall_pass and rp
        print("  SIGNAL-IS-REAL:", "PASS — graph finds real cross-folder entanglement" if rp else "WEAK")

    if len(rows) > 1:
        print("\n" + "#" * 84)
        print("AGGREGATE (mean across repos)")
        n = len(rows)
        m = lambda f: sum(f(r) for r in rows) / n
        print(f"  coupled ever-co-change {m(lambda r: r['coupled'][0])*100:.1f}%   "
              f"random {m(lambda r: r['rnd'][0])*100:.1f}%   same-dir {m(lambda r: r['samedir'][0])*100:.1f}%")
        print(f"  cross-dir coupled {m(lambda r: r['coupled_x'][0])*100:.1f}%   "
              f"vs cross-dir random {m(lambda r: r['rnd_x'][0])*100:.1f}%")
    print("\nHONEST BOUNDARY: co-change proves the warning SIGNAL tracks real coupling (necessary for effect),")
    print("and beats co-location — it does NOT prove rework-hours-saved (needs a deployed A/B over real PRs).")
    print(f"(co-change universe excludes commits touching >{_MAX_COMMIT_FILES} tracked files — mechanical "
          "mass-edits, not coupling; set BACKTEST_MAX_COMMIT_FILES to change.)")
    print("\nALL REPOS SIGNAL-IS-REAL:", "PASS" if overall_pass else "WEAK")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
