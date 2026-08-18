#!/usr/bin/env python3
"""COMBINED RECALL — the one honest number: of the file-pairs that REAL INCIDENTS show were coupled, what
fraction does each detector catch, and what is the dominant MISS?

This is the product's #1 credibility measurement. `tests/recall_measure.py` already measures recall, but
against a CO-CHANGE proxy ground truth (pairs that co-change a lot = coupling). This tool's ground truth is
STRONGER and DIFFERENT: it is **correction incidents**. A commit whose message is a revert/fix/hotfix/bug/
regression AND touches ≥2 files is a CORRECTION that had to touch those files TOGETHER — i.e. a coupling that
actually CAUSED rework. The unordered file-pairs inside such commits are the incident pairs: coupling that
genuinely hurt, not merely coupling that co-moved. (A co-change is "these move together"; an incident is "a
fix had to touch both" — the latter is closer to the rework Veripsa exists to prevent.)

We then ask, of those incident pairs, the recall of:
  (a) GRAPH-ONLY    — the static code-graph adjacency, WITH the hub-dampening + ≤3 def fan-out + the
                      representative stoplist copied from recall_measure.py (NOT a looser flattering
                      adjacency; but NOT the exact shipping stoplist either — that lives in
                      db/schema/70_social.sql, synced to the backtest by gate 154. This is a close
                      approximation; see the NOTE in recall_measure.py).
  (b) COCHANGE-ONLY — the SHIPPING co-change detector's positive pairs (_cg_cochange.cochange_pairs: lift-
                      positive, support-floored, giant-commit-skipped). We do not re-average it; we call the
                      same function the product ships.
  (c) COMBINED      — the UNION of the two detectors (what a customer actually sees when both run).

Then it NAMES the dominant MISS: of incident pairs NEITHER detector covers, a factual, content-free
breakdown (same-dir vs cross-dir; both-files-have-no-history; whether ANY graph edge connects them) so we
know WHAT we are blind to. The number tells us whether extractor work moves the needle and is the sellable
credibility metric.

CONTENT-FREE by construction: only file PATHS, the per-commit GROUPING of paths, COUNTS, and a boolean
correction-pattern match on the commit SUBJECT (not the body, not the diff) ever leave the repo. No file
contents are ever read.

HONEST BOUNDARY (printed at the end): the incident ground truth is itself a PROXY. The correction-message
heuristic is imperfect (some fixes are not labelled "fix"; some "fix" commits are unrelated cleanups), and
not all real coupling ever surfaces as a revert/fix that touches both files. So neither 100% nor any single
number is "the truth"; the value is a consistent, reproducible measurement of where we stand and what we
miss.

Run:  python3 tests/combined_recall.py /repo/with/history [/repo2 ...]
      (defaults to this repo if no path given). Exit 0 always — this is a measurement, not a pass/fail bar.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X            # noqa: E402  (the SHIPPING extractor)
import _cg_cochange                       # noqa: E402  (the SHIPPING co-change detector)

# ---------------------------------------------------------------------------------------------------------
# GRAPH ADJACENCY — mirrored VERBATIM from tests/recall_measure.py so the number is what the SHIPPING product
# would actually warn on (hub dampening + ≤3 def fan-out + stoplist), not the raw graph. Do NOT loosen this:
# a looser adjacency would flatter the recall number. (Copied verbatim; keep in sync with recall_measure.py.)
# ---------------------------------------------------------------------------------------------------------
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


def graph_adjacency(g, files):
    """Undirected file pairs the SHIPPING engine treats as coupled, WITH hub + resource dampening (mirrors
    core._claim_adjacency / recall_measure.adjacency(apply_damp=True)). Returns set(frozenset(a,b))."""
    hubs = hub_files(g, files)
    rhubs = res_hubs(g, files)
    defs = {}
    for n in g["nodes"]:
        if n.get("kind") in ("def", "class") and n.get("name"):
            nm = n["name"]
            if not nm.startswith("__") and nm.lower() not in STOP:
                defs.setdefault(nm, set()).add(n["path"])
    defs_ok = {nm: fs for nm, fs in defs.items() if len(fs) <= 3}   # engine's ≤3 fan-out
    by_dst, pairs = {}, set()

    def add(a, b):
        if a != b and a in files and b in files:
            pairs.add(frozenset((a, b)))

    for e in g["edges"]:
        k = e["kind"]
        if k == "imports" and e["dst"] in files:
            if e["dst"] in hubs:
                continue                                  # hub dampening: don't couple to a hub you import
            add(e["src"], e["dst"])
        elif k == "calls" and e["src"] in files:
            for f in defs_ok.get(e["dst"], ()):
                if f in hubs:
                    continue                              # don't couple to a hub you merely call into
                add(e["src"], f)
        elif k in ("queries", "alters", "reads_config") and e["src"] in files:
            if e["dst"] in rhubs:
                continue
            by_dst.setdefault(e["dst"], set()).add(e["src"])
    for _dst, fs in by_dst.items():
        fs = list(fs)
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                add(fs[i], fs[j])
    return pairs


# ---------------------------------------------------------------------------------------------------------
# CO-CHANGE DETECTOR positives — the SHIPPING detector itself (_cg_cochange.cochange_pairs), not a re-average.
# ---------------------------------------------------------------------------------------------------------
def cochange_adjacency(repo, files):
    """The shipping co-change detector's positive pairs (lift-positive, support-floored, giant-commit-skipped),
    restricted to file pairs both present at HEAD. Returns set(frozenset(a,b))."""
    pairs = set()
    for pr in _cg_cochange.cochange_pairs(repo):     # ships with: window=800, max_commit_files=40,
        a, b = pr["a"], pr["b"]                       # min_support=3, min_prob=0.3, min_lift=2.0
        if a in files and b in files and a != b:
            pairs.add(frozenset((a, b)))
    return pairs


# ---------------------------------------------------------------------------------------------------------
# INCIDENT GROUND TRUTH — correction commits (revert/fix/bug/hotfix/regression) touching ≥2 files. Each such
# commit's unordered file-pairs are incident pairs: coupling a CORRECTION had to touch together. Content-free:
# we match a boolean pattern on the commit SUBJECT only and read --name-only paths; never the diff/body.
# ---------------------------------------------------------------------------------------------------------
# word-boundary, case-insensitive. revert(s|ed), fix(es|ed), bug(s|fix), hotfix, regression. These are the
# universal "this commit corrected something that broke" markers; matched on the subject line only.
_CORRECTION = re.compile(
    r"\b(?:revert(?:s|ed)?|fix(?:e[sd]|ing)?|bug(?:fix|s)?|hot[\- ]?fix(?:es|ed)?|regression(?:s)?|"
    r"broke(?:n)?|breakage|patch(?:ed|es)?)\b",
    re.IGNORECASE,
)
_REC = "\x01"   # a byte git never emits inside a path/subject — reliable record split.


def incident_pairs(repo, files, max_commit_files=MAX_COMMIT_FILES):
    """Build incident ground-truth pairs. For each non-merge commit whose SUBJECT matches the correction
    pattern AND that touches ≥2 (and ≤max_commit_files, to skip mass mechanical reverts) tracked files, every
    unordered pair of those tracked files is an incident pair. Returns (set(frozenset(a,b)), n_incident_commits).
    Content-free: subject is used ONLY for the boolean pattern; only paths/counts are retained."""
    # %s = subject only (NOT the body, NOT the diff). --name-only = paths only. Record-delimited so we can pair
    # each subject with its file list deterministically.
    out = subprocess.run(
        ["git", "-C", repo, "log", "--no-merges", "--name-only", f"--pretty=format:{_REC}%s"],
        capture_output=True, text=True).stdout
    pairs: set = set()
    n_commits = 0
    is_correction = False
    cur: list = []

    def flush():
        nonlocal n_commits
        if is_correction and 2 <= len(cur) <= max_commit_files:
            n_commits += 1
            fl = sorted(set(cur))
            for i in range(len(fl)):
                for j in range(i + 1, len(fl)):
                    pairs.add(frozenset((fl[i], fl[j])))

    for line in out.split("\n"):
        if line.startswith(_REC):
            flush()
            is_correction = bool(_CORRECTION.search(line[len(_REC):]))
            cur = []
        elif line.strip() in files:                  # restrict to files present at HEAD (graph universe)
            cur.append(line.strip())
    flush()
    return pairs, n_commits


# ---------------------------------------------------------------------------------------------------------
# MISS CLASSIFIER — of incident pairs NEITHER detector covers, characterize them (content-free).
# ---------------------------------------------------------------------------------------------------------
def classify_misses(missed, graph_pairs, file_history):
    """Bucket the incident pairs neither detector covered. `file_history` = {path: bool has any commit history}.
    Returns a dict of factual counts. Content-free (dir names + extensions + booleans only).
    `graph_pairs` here is the RAW-or-dampened graph set used only to answer 'is there ANY graph edge at all'."""
    buckets = {
        "no_history": 0,        # at least one file has no commit history → co-change structurally cannot see it
        "no_graph_edge_xdir": 0,  # no graph edge AND files in different dirs (the hardest blind spot)
        "no_graph_edge_samedir": 0,  # no graph edge but same dir (folder cue exists, our detectors don't fire)
        "graph_edge_but_missed": 0,  # a graph edge EXISTS but dampening/fan-out dropped it (recoverable)
    }
    ext_counts: dict = {}
    for k in missed:
        a, b = tuple(k)
        if not (file_history.get(a) and file_history.get(b)):
            buckets["no_history"] += 1
            continue
        if k in graph_pairs:
            buckets["graph_edge_but_missed"] += 1
        else:
            same_dir = os.path.dirname(a) == os.path.dirname(b)
            buckets["no_graph_edge_samedir" if same_dir else "no_graph_edge_xdir"] += 1
        for f in (a, b):
            ext = os.path.splitext(f)[1].lower() or "(none)"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
    # the single dominant bucket (factual, for the one-line "what we're blind to")
    dominant = max(buckets.items(), key=lambda kv: kv[1]) if missed else ("(none)", 0)
    top_exts = sorted(ext_counts.items(), key=lambda kv: -kv[1])[:4]
    return {"buckets": buckets, "dominant": dominant, "top_exts": top_exts, "total_missed": len(missed)}


# ---------------------------------------------------------------------------------------------------------
def analyze(repo):
    g = X.build_graph(repo)
    files = {n["path"] for n in g["nodes"] if n["kind"] == "file"}

    graph_pairs = graph_adjacency(g, files)            # detector (a): static graph, dampened (shipping)
    # raw-graph edge existence (for the miss classifier's "is there ANY edge"): a graph WITHOUT dampening.
    raw_edge = set()
    for e in g["edges"]:
        if e["src"] in files and e["dst"] in files and e["src"] != e["dst"]:
            raw_edge.add(frozenset((e["src"], e["dst"])))

    cochange_pairs = cochange_adjacency(repo, files)   # detector (b): shipping co-change
    combined = graph_pairs | cochange_pairs            # detector (c): UNION

    incidents, n_inc_commits = incident_pairs(repo, files)

    # file → has any commit history? (a file co-change can never see needs history on BOTH endpoints)
    log_files = subprocess.run(
        ["git", "-C", repo, "log", "--no-merges", "--name-only", "--pretty=format:"],
        capture_output=True, text=True).stdout
    have_history = {p for p in (ln.strip() for ln in log_files.split("\n")) if p in files}
    file_history = {f: (f in have_history) for f in files}

    def recall(detector):
        if not incidents:
            return 0, 0.0
        hit = sum(1 for k in incidents if k in detector)
        return hit, hit / len(incidents) * 100.0

    g_hit, g_rec = recall(graph_pairs)
    c_hit, c_rec = recall(cochange_pairs)
    u_hit, u_rec = recall(combined)

    missed = {k for k in incidents if k not in combined}
    miss = classify_misses(missed, raw_edge, file_history)

    return {
        "repo": os.path.basename(repo.rstrip("/")), "files": len(files),
        "graph_pairs": len(graph_pairs), "cochange_pairs": len(cochange_pairs), "combined_pairs": len(combined),
        "n_incidents": len(incidents), "n_inc_commits": n_inc_commits,
        "g_hit": g_hit, "g_rec": g_rec, "c_hit": c_hit, "c_rec": c_rec, "u_hit": u_hit, "u_rec": u_rec,
        "miss": miss,
    }


def _print_report(r):
    print("\n" + "=" * 90)
    print(f"{r['repo']}  —  {r['files']} files | incident pairs: {r['n_incidents']} "
          f"(from {r['n_inc_commits']} correction commits)")
    print("=" * 90)
    print(f"  detector pair counts:  graph {r['graph_pairs']}   co-change {r['cochange_pairs']}   "
          f"UNION {r['combined_pairs']}")
    if r["n_incidents"] == 0:
        print("  NO incident pairs found (no correction commit touched ≥2 tracked files) — recall undefined "
              "for this repo. This is HONEST 'not evaluated', not 0%.")
        return
    print(f"  RECALL against incident ground truth ({r['n_incidents']} pairs):")
    print(f"    graph-only     {r['g_rec']:5.1f}%   ({r['g_hit']}/{r['n_incidents']})")
    print(f"    co-change-only {r['c_rec']:5.1f}%   ({r['c_hit']}/{r['n_incidents']})")
    print(f"    COMBINED       {r['u_rec']:5.1f}%   ({r['u_hit']}/{r['n_incidents']})   "
          f"← the union; the lift over the better single detector is the second detector's value")
    m = r["miss"]
    dom_name, dom_n = m["dominant"]
    print(f"  DOMINANT MISS — of the {m['total_missed']} incident pairs NEITHER detector covers:")
    b = m["buckets"]
    print(f"    no-history (a file has no commit history; co-change is structurally blind): {b['no_history']}")
    print(f"    no graph edge, cross-dir (both detectors fundamentally blind):              {b['no_graph_edge_xdir']}")
    print(f"    no graph edge, same-dir (only a folder cue exists):                         {b['no_graph_edge_samedir']}")
    print(f"    graph edge EXISTS but dampening/fan-out dropped it (recoverable):           {b['graph_edge_but_missed']}")
    print(f"    → DOMINANT MISS CLASS: {dom_name} ({dom_n} pairs)")
    if m["top_exts"]:
        print("    file extensions among missed pairs: " +
              ", ".join(f"{e} ×{n}" for e, n in m["top_exts"]))


def main():
    repos = [os.path.abspath(p) for p in sys.argv[1:]] or [ROOT]
    print("\n=== COMBINED RECALL (graph-dampened ∪ co-change vs CORRECTION-INCIDENT ground truth) ===")
    rows = [analyze(r) for r in repos]
    for r in rows:
        _print_report(r)
    print("\nHONEST BOUNDARY: the incident ground truth is itself a PROXY. The correction-message heuristic")
    print("(revert/fix/bug/hotfix/regression on the commit subject) is imperfect — some real fixes are not")
    print("labelled, some labelled commits are unrelated cleanups — and not all real coupling ever surfaces")
    print("as a revert/fix touching BOTH files. So no single number is 'the truth'; this is a consistent,")
    print("reproducible measurement of where we stand and what we are blind to. Content-free throughout")
    print("(paths, per-commit path groupings, counts, and a boolean pattern on the commit subject only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
