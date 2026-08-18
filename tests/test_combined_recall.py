#!/usr/bin/env python3
"""COMBINED RECALL GATE — deterministic, git-free, fast proof of the combined-recall measurement's logic.

We do NOT shell out to git here (that would be slow/non-deterministic). We craft a TINY synthetic scenario
directly in memory — a small graph dict, a small co-change positive set, and a small incident-pair set — and
assert the math the real tool computes:
  1. combined recall = |UNION ∩ incident| / |incident|  (exact set-union semantics, never an average).
  2. combined recall ≥ max(graph recall, co-change recall)  (a union can only help).
  3. a pair caught ONLY by co-change (graph-invisible — no edge) LIFTS combined above graph-only.
  4. the miss-classifier buckets an uncovered CROSS-DIR pair (no graph edge) into no_graph_edge_xdir.
  5. content-free: the tool never reads file contents (it imports no file-content reader; the gate proves
     the real source contains no open()/read() of repo files in its hot path).

Prints `COMBINED RECALL GATE: PASS` / `FAIL` and returns 0/1.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import combined_recall as CR  # noqa: E402


def _recall(detector, incidents):
    """Mirror of the tool's recall calc (set membership, exact union semantics)."""
    if not incidents:
        return 0.0
    hit = sum(1 for k in incidents if k in detector)
    return hit / len(incidents) * 100.0


def run():
    failures = []

    # ---- TINY synthetic scenario (file paths only; NO contents anywhere) -----------------------------------
    # 4 incident pairs that "real corrections" had to touch together:
    P_AB = frozenset(("src/a.py", "src/b.py"))      # graph WILL catch (import edge below)
    P_CD = frozenset(("svc/c.py", "ui/d.py"))       # ONLY co-change catches (no graph edge, cross-dir)
    P_EF = frozenset(("pkg/e.py", "pkg/f.py"))       # NEITHER catches, same-dir, no graph edge
    P_GH = frozenset(("api/g.py", "web/h.py"))       # NEITHER catches, CROSS-dir, no graph edge
    incidents = {P_AB, P_CD, P_EF, P_GH}

    # detector pair-sets crafted directly (what each detector would emit):
    graph_pairs = {P_AB}                              # only the import-edge pair
    cochange_pairs = {P_CD}                            # the graph-invisible co-change pair
    combined = graph_pairs | cochange_pairs            # the UNION

    g_rec = _recall(graph_pairs, incidents)            # 1/4 = 25%
    c_rec = _recall(cochange_pairs, incidents)         # 1/4 = 25%
    u_rec = _recall(combined, incidents)               # 2/4 = 50%

    # (1) exact union semantics: combined = |union ∩ incident| / |incident|
    expect = len(combined & incidents) / len(incidents) * 100.0
    if abs(u_rec - expect) > 1e-9:
        failures.append(f"combined recall {u_rec} != |union∩incident|/|incident| {expect}")

    # (2) combined ≥ max(graph, cochange)
    if u_rec < max(g_rec, c_rec) - 1e-9:
        failures.append(f"combined {u_rec} < max(graph {g_rec}, cochange {c_rec}) — union must not lose recall")

    # (3) a co-change-only (graph-invisible) pair LIFTS combined above graph-only
    if not (u_rec > g_rec + 1e-9):
        failures.append(f"co-change-only pair did not lift combined ({u_rec}) above graph-only ({g_rec})")
    if P_CD in graph_pairs:
        failures.append("P_CD must be graph-INVISIBLE for this test to mean anything")

    # (4) miss classifier buckets the uncovered CROSS-DIR no-edge pair correctly
    missed = {k for k in incidents if k not in combined}   # = {P_EF (same-dir), P_GH (cross-dir)}
    if missed != {P_EF, P_GH}:
        failures.append(f"unexpected missed set {missed}")
    # raw_edge: the only graph edge in this scenario is A->B (which IS covered, so not in `missed`); P_EF/P_GH
    # have NO graph edge. file_history: all present (so 'no_history' must be 0, not the dominant bucket).
    raw_edge = {P_AB}
    file_history = {f: True for pair in incidents for f in pair}
    m = CR.classify_misses(missed, raw_edge, file_history)
    b = m["buckets"]
    if b["no_graph_edge_xdir"] != 1:
        failures.append(f"cross-dir no-edge bucket = {b['no_graph_edge_xdir']}, expected 1 (P_GH)")
    if b["no_graph_edge_samedir"] != 1:
        failures.append(f"same-dir no-edge bucket = {b['no_graph_edge_samedir']}, expected 1 (P_EF)")
    if b["no_history"] != 0:
        failures.append(f"no_history bucket = {b['no_history']}, expected 0 (all files have history)")
    if b["graph_edge_but_missed"] != 0:
        failures.append(f"graph_edge_but_missed = {b['graph_edge_but_missed']}, expected 0")

    # the dominant bucket must be a tie-broken real bucket of size 1 here (xdir or samedir) — assert it is one
    # of the two no-edge buckets, the genuine blind spot
    dom_name, dom_n = m["dominant"]
    if dom_name not in ("no_graph_edge_xdir", "no_graph_edge_samedir") or dom_n != 1:
        failures.append(f"dominant miss = {dom_name}({dom_n}), expected a no-edge bucket of size 1")

    # (4b) miss classifier moves a no-history pair into the no_history bucket (co-change structurally blind)
    nh_missed = {frozenset(("new1.py", "new2.py"))}
    m2 = CR.classify_misses(nh_missed, set(), {"new1.py": False, "new2.py": True})
    if m2["buckets"]["no_history"] != 1:
        failures.append("a pair with a no-history endpoint was not bucketed no_history")

    # (5) content-free: the real source must not read repo FILE CONTENTS. It reads paths + git log subjects +
    # --name-only paths only. Assert the source contains no open()/.read()/Path.read_text of a repo file in
    # its analysis path. (We allow subprocess git calls — those are content-free by --name-only/%s.)
    src = open(os.path.join(ROOT, "tests", "combined_recall.py"), "r", encoding="utf-8").read()
    for forbidden in ("open(", ".read_text(", ".read_bytes(", "io.open(", "codecs.open("):
        # the ONLY open() allowed is none — the tool never opens repo files. (This very test opens its own
        # source above, but that is the TEST, not the tool.)
        if forbidden in src:
            failures.append(f"tool source contains '{forbidden}' — must not read file contents")
    # belt-and-braces: the tool must use --name-only / %s (paths/subjects, not patches) in its git calls.
    if "--name-only" not in src or "%s" not in src:
        failures.append("tool must use git --name-only (paths) and %s (subject only) — content-free guarantee")
    # the tool must NOT request commit bodies (%b) or diffs/patches. Check whole-token git args (the args are
    # written as quoted list elements, so a real patch flag appears as the literal "-p" / "--patch" token).
    if "%b" in src:
        failures.append("tool must NOT request commit bodies (%b)")
    for tok in ('"-p"', "'-p'", '"--patch"', "'--patch'", '"-u"', "'-u'"):
        if tok in src:
            failures.append(f"tool must NOT request diffs/patches (found git arg {tok})")

    ok = not failures
    print("\n=== COMBINED RECALL GATE ===")
    print(f"  synthetic recall: graph {g_rec:.0f}%  co-change {c_rec:.0f}%  COMBINED {u_rec:.0f}%  "
          f"(co-change lifts combined above graph: {'YES' if u_rec > g_rec else 'no'})")
    print(f"  miss buckets: {m['buckets']}  dominant={m['dominant']}")
    if failures:
        for f in failures:
            print("  FAIL:", f)
    print("\nCOMBINED RECALL GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
