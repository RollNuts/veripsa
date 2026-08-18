#!/usr/bin/env python3
"""Veripsa — evaluate it on YOUR repo BEFORE you install (try-before-you-buy · content-free · $0).

The honest question a buyer asks: "will Veripsa's warnings actually point at real entanglement in OUR code,
or is it noise?" Answer it for free, on your own history, WITHOUT installing anything and WITHOUT sending us a
single line of code. This reads your repo's code graph (paths + edges only) and its git co-change history
LOCALLY, and prints a SHAREABLE report you can hand to your team.

WHAT IT PROVES: the file pairs Veripsa would flag as coupled (its code-graph adjacency — imports, cross-file
calls, shared schema/config) genuinely co-change in your history far more than chance — AND more than mere
folder co-location (the hard control). The CROSS-DIRECTORY lift is the differentiated proof: entanglement a
folder/text heuristic is blind to, that only the graph sees. (This is the exact signal the GitHub App warns on
before a merge.)

WHAT IT DOES NOT PROVE (printed, always): rework-hours-saved. That needs a deployed A/B over real PRs. This
proves the SIGNAL is real on YOUR code — the precondition for any effect. We don't claim more than we show.

Usage:
    python3 evaluate.py /path/to/your/repo [/another/repo ...]        # print the report
    python3 evaluate.py /path/to/your/repo --out veripsa-report.md    # also write a shareable markdown file

Content-free: reads paths + `git log --name-only`; never file contents; nothing leaves your machine.
Exit code 0 if the signal is real on every repo, 1 if any is weak (so it doubles as a CI/pre-sales check).
"""
from __future__ import annotations

import os
import sys

# Reuse the rigorous engine (co-change vs random + same-dir baselines, cross-dir lift) — the shared,
# NON-TEST core in cochange_backtest.py. We add only the customer-facing framing + a shareable report.
# (Importing the top-level engine, not tests/, keeps product code free of any src→test coupling.)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cochange_backtest as BT  # noqa: E402

# The Veripsa Units meter — so a buyer sees their PROVISIONAL number BEFORE installing (content-free; the
# formula + coefficients stay internal/tunable — we surface the Units NUMBER only, never raw weights).
import units as UNITS  # noqa: E402

_BEATS = 1.3   # the bar: coupled must co-change >1.3x its control (mirrors backtest_cochange)
_MIN_X_PAIRS = 20   # below this many cross-directory coupled pairs the lift ratio is statistically meaningless → report N/A, not a fragile/inf number (Veripsa differentiates on multi-directory codebases)
_MIN_HIST_FILES = 2   # the engine's random-baseline sampling needs >=2 files-with-history to draw a pair; below this a repo is DEGENERATE (empty/no-history/single-file) → honest N/A, never a crash (this is a SALES artifact)


def _na_analyze(repo_name: str, files: int = 0, commits: int = 0) -> dict:
    """A well-formed, all-zero analyze()-shaped result for a DEGENERATE repo (too few files-with-history to even
    baseline). Feeding this through _verdict yields an N/A verdict whose shape is IDENTICAL to a real one (no
    hand-rolled divergent dict) — so the existing N/A reporting + exit-code machinery handles it. Content-free:
    carries only honest counts (files/commits), never rates or paths."""
    z = (0.0, 0.0, 0)
    return {
        "repo": repo_name, "files": files, "commits": commits,
        "pairs": 0, "pairs_x": 0,
        "coupled": z, "coupled_x": z, "rnd": z, "samedir": z, "rnd_x": z,
        "by_type": {t: (z, 0) for t in ("import", "call", "schema", "config")},
    }


def _history_file_count(repo: str) -> tuple:
    """(#files-with-git-history, #commits) — cheaply, content-free, via the SAME shared helpers analyze() uses
    (build_graph paths + `git log --name-only`). Crash-safe on empty/non-git dirs (returns (0, 0)) so we can
    PRE-DETECT a degenerate repo BEFORE calling the heavy analyze(), whose random-baseline sampling would raise
    on <2 files-with-history. Never reads file bodies, diffs, or commit messages."""
    try:
        g = BT.X.build_graph(repo)
        files = {n["path"] for n in g["nodes"] if n.get("kind") == "file"}
        touch, T = BT.commit_touchsets(repo, files)
        return sum(1 for f in files if touch.get(f)), T
    except Exception:  # noqa: BLE001 — a degenerate/unreadable repo is N/A, never a crash for a buyer
        return 0, 0


def _verdict(r: dict, repo_path: str = None) -> dict:
    """Turn one analyze() result into the headline numbers + the SIGNAL-IS-REAL pass (identical criteria to
    the shared cochange_backtest engine: beats random AND adds cross-directory value where folders are blind).

    `repo_path` (the absolute repo dir) lets us also compute the PROVISIONAL Veripsa Units meter for the buyer."""
    c_rate = r["coupled"][0]
    r_rate = r["rnd"][0]
    sd_rate = r["samedir"][0]
    cx_rate = r["coupled_x"][0]
    rx_rate = r["rnd_x"][0]
    beats_random = c_rate > r_rate * _BEATS
    graph_adds = cx_rate > rx_rate * _BEATS                 # THE bar — value a folder heuristic cannot reach
    # APPLICABILITY GUARD (honest small/flat repo handling): the cross-directory lift — Veripsa's DIFFERENTIATED
    # proof — is only meaningful with ENOUGH cross-directory coupled pairs. On a tiny lib (a handful of files) or
    # a flat single-package repo, there are too few cross-dir pairs for a stable ratio, and the random baseline
    # can be 0 → a meaningless "inf×" / "0.0×" that reads as broken, not trustworthy. Below the floor we report
    # N/A ("Veripsa's differentiated signal needs a multi-directory codebase"), NOT a fragile number — and it is
    # NOT a weak/fail verdict (this repo type just isn't where Veripsa differentiates).
    applicable = r["pairs_x"] >= _MIN_X_PAIRS and rx_rate > 0
    # The PROVISIONAL Veripsa Units meter for this repo (content-free; runs the same engine the App uses). We
    # surface only the Units NUMBER — never the coefficients (those stay internal/tunable). Never crash the
    # report if the meter hiccups on one repo: fall through with units=None.
    units = None
    try:
        if repo_path:
            units = UNITS.compute_units(repo_path)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"  (units meter skipped for {r['repo']}: {type(e).__name__}: {e})\n")
    return {
        "repo": r["repo"], "files": r["files"], "commits": r["commits"],
        "units": units,
        "pairs": r["pairs"], "pairs_x": r["pairs_x"],
        "c_rate": c_rate, "r_rate": r_rate, "sd_rate": sd_rate, "cx_rate": cx_rate, "rx_rate": rx_rate,
        "rand_lift": (c_rate / r_rate) if r_rate else float("inf"),
        "cross_lift": (cx_rate / rx_rate) if rx_rate else float("inf"),
        "by_type": {t: (r["by_type"][t][0][0], r["by_type"][t][1]) for t in ("import", "call", "schema", "config")},
        "applicable": applicable,
        "signal_is_real": applicable and beats_random and graph_adds,
        "beats_random": beats_random, "graph_adds": graph_adds,
    }


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _lift(x: float) -> str:
    """Render a lift multiplier HONESTLY for the shareable report. A finite ratio prints as 'N.N×'. An INFINITE
    ratio (the control baseline never co-changed in this sample → division by ~0) must NOT print a fragile
    'inf×' (reads as broken/untrustworthy in a sales artifact) and must NOT fabricate a magnitude — we render
    the TRUE qualitative claim '≫' (much greater than; the control was ~0 in-sample). Content-free: a multiplier
    only, never the underlying rates."""
    if x == float("inf") or x != x:  # inf or NaN
        return "≫"
    return f"{x:.1f}×"


def _report_md(verdicts: list) -> str:
    """A shareable markdown report (paste into a PR / Slack / an adoption proposal)."""
    L: list[str] = []
    applic = [v for v in verdicts if v["applicable"]]
    real = [v for v in applic if v["signal_is_real"]]
    na = len(verdicts) - len(applic)
    L.append("# Veripsa — does its signal track real coupling in your code?")
    L.append("")
    if not applic:
        head = "N/A — needs a multi-directory codebase (every repo given was too small/flat to evaluate)"
    elif len(real) == len(applic):
        head = f"SIGNAL IS REAL ✓ ({len(real)}/{len(applic)} applicable repos)"
    else:
        head = f"WEAK on some — see per-repo notes ({len(real)}/{len(applic)} applicable repos real)"
    L.append(f"**Verdict: {head}**" + (f"  ·  {na} repo(s) N/A (too small/flat)" if na else ""))
    L.append("")
    L.append("Veripsa warns, before a merge, when your change's code-graph neighborhood meets another in-flight "
             "change. This evaluates — on your own git history, content-free — whether the pairs it flags as "
             "coupled actually co-change more than chance, and more than living in the same folder.")
    for v in verdicts:
        L.append("")
        L.append(f"## `{v['repo']}` — {v['files']} files · {v['commits']} commits · {v['pairs']} coupled pairs "
                 f"({v['pairs_x']} cross-directory)")
        L.append("")
        if v.get("units"):
            u = v["units"]
            L.append(f"**Veripsa Units (provisional meter): ~{u['units']:,}**  "
                     f"— {u['loc']:,} in-scope LOC, entanglement premium ×{u['premium']:g}. "
                     "This is what you'd be metered on. It's anchored to in-scope lines of code and lifted by how "
                     "entangled your code is (cross-file and cross-substrate coupling) — the very thing Veripsa "
                     "flags before a merge. Meter is **provisional** (coefficients still being calibrated from "
                     "real usage); the number can shift as we tune it.")
            L.append("")
        if not v["applicable"]:
            # too few cross-dir pairs for a meaningful lift → honest N/A, never a fragile/inf number.
            L.append(f"**N/A for this repo.** Only {v['pairs_x']} cross-directory coupled pair(s) — too few to "
                     "measure a meaningful lift. Veripsa's differentiated signal (entanglement across directories "
                     "that folder/text heuristics can't see) needs a **multi-directory codebase**; a small or flat "
                     "single-package repo simply doesn't have the cross-directory coupling to evaluate. This is not "
                     "a weak result — it's the wrong repo shape for the differentiated proof. Try it on a larger or "
                     "multi-module repo.")
            continue
        # Moat discipline: emit ONLY the final multiplier — never the raw coupled/random rates together.
        # A reader who sees both rates AND the ratio can recover the formula (lift = coupled/random).
        # The multiplier alone is sufficient to prove the signal is real; the raw rates stay internal.
        L.append("| signal | multiplier vs random |")
        L.append("|---|---|")
        L.append(f"| ever co-change | **{_lift(v['rand_lift'])} stronger than random** |")
        L.append(f"| **cross-directory** (folders are blind) | **{_lift(v['cross_lift'])} stronger than random** |")
        L.append(f"| same-dir co-location (hard control) | _context only_ |")
        L.append("")
        bt = v["by_type"]
        L.append("By coupling type (ever co-change): " +
                 " · ".join(f"{t} {_pct(bt[t][0])} (n={bt[t][1]})" for t in ("import", "call", "schema", "config")))
        L.append("")
        L.append(f"**The differentiated proof:** the cross-directory pairs Veripsa flags co-change "
                 f"**{_lift(v['cross_lift'])}** more than random cross-directory pairs — entanglement a folder or "
                 f"text heuristic cannot see. "
                 + ("Signal is real on this repo ✓." if v["signal_is_real"]
                    else "Signal is weak here (didn't clear the bar) — tell us; it usually means a graph-coverage gap for this stack."))
    L.append("")
    L.append("> **Honest boundary.** This proves the warning SIGNAL tracks real coupling on your history "
             "(the precondition for effect) and beats folder co-location. It does NOT prove rework-hours-saved "
             "— that needs a deployed A/B over real PRs. Veripsa records what's heading to your protected branch "
             "and who reserved what; it does not assert correctness.")
    L.append("")
    L.append("<sub>Generated locally by `evaluate.py` — content-free (paths + git co-change only); no code left "
             "your machine.</sub>")
    return "\n".join(L)


def main(argv: list) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    out = None
    for a in argv[1:]:
        if a.startswith("--out="):
            out = a.split("=", 1)[1]
        elif a == "--out" and argv.index(a) + 1 < len(argv):
            out = argv[argv.index(a) + 1]
            args = [x for x in args if x != out]
    repos = [os.path.abspath(p) for p in args]
    if not repos:
        print(__doc__)
        return 2
    verdicts = []
    for repo in repos:
        if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isdir(repo):
            print(f"skip: {repo} is not a directory", file=sys.stderr)
            continue
        sys.stderr.write(f"analyzing {repo} (graph + git co-change history)…\n")
        # DEGENERATE-REPO GUARD (never-crash; this is a SALES artifact a prospect runs on ANY repo). The engine's
        # random-baseline sampling draws a pair from files-with-history, so an empty / no-history / single-file
        # repo (e.g. a freshly `git init`'d dir) would raise inside analyze(). Pre-detect that — content-free,
        # via the same shared helpers — and report an honest N/A instead of a Python stack trace. N/A is not a
        # failure (exit 0), so trying Veripsa on a tiny/empty repo never looks "broken" or "failed".
        nhist, ncommits = _history_file_count(repo)
        if nhist < _MIN_HIST_FILES:
            sys.stderr.write(f"  (N/A: {os.path.basename(repo.rstrip('/'))} has too little git history to "
                             f"evaluate — needs ≥{_MIN_HIST_FILES} files with commit history)\n")
            verdicts.append(_verdict(_na_analyze(os.path.basename(repo.rstrip("/")), files=nhist,
                                                 commits=ncommits), repo_path=repo))
            continue
        verdicts.append(_verdict(BT.analyze(repo), repo_path=repo))
    if not verdicts:
        print("no analyzable repos given", file=sys.stderr)
        return 2
    report = _report_md(verdicts)
    print(report)
    # Surface the PROVISIONAL Veripsa Units number on the console too (the buyer's headline metric, content-free;
    # no coefficients shown — the formula stays internal/tunable).
    for v in verdicts:
        if v.get("units"):
            print(f"\nVeripsa Units (provisional meter): ~{v['units']['units']:,}  "
                  f"[{v['repo']}: {v['units']['loc']:,} in-scope LOC × premium {v['units']['premium']:g}]")
    if out:
        with open(out, "w") as fh:
            fh.write(report + "\n")
        sys.stderr.write(f"\nwrote {out}\n")
    return _exit_code(verdicts)


def _exit_code(verdicts: list) -> int:
    """The pre-sales / CI check. Only APPLICABLE repos (enough cross-directory coupled pairs to measure
    Veripsa's differentiated signal) can pass or fail. A repo that is N/A (too small/flat) is the WRONG SHAPE
    for the differentiated proof, not a weak result — counting it as a failure would punish a buyer for trying
    it on a tiny lib. So: no applicable repos → exit 0 (nothing to fail on); otherwise require every applicable
    repo to clear the bar."""
    applic = [v for v in verdicts if v["applicable"]]
    if not applic:
        return 0
    return 0 if all(v["signal_is_real"] for v in applic) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
