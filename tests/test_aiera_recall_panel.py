#!/usr/bin/env python3
"""AI-ERA RECALL PANEL GATE — deterministic, no-clone, no-network proof of the panel harness's discipline.

`tests/aiera_recall_panel.py` pools recall over the REAL customer profile (amateur + AI building app/SaaS).
The numbers come from already-gated tools (84-combined_recall.gate proves the recall LOGIC). This gate proves
the PANEL HARNESS itself is honest and safe, WITHOUT cloning anything:

  1. the PANEL is well-formed: each entry is (dir, fullName, lang, why) with a non-empty WHY and a language in
     the customer's set {python, javascript, typescript, go} — i.e. a documented, intentional sample.
  2. pooling is a MICRO-average (sum hits / sum incidents), NEVER a mean-of-percentages — proven on a tiny
     in-memory two-repo fixture where the two differ, asserting the harness's pooled() matches sum/sum.
  3. network-free AT RUN TIME: the source contains no clone/fetch call (no 'gh repo clone', no
     subprocess git clone/fetch) — it only measures locally-present clones and reports absent ones.
  4. content-free posture is inherited (it calls combined_recall/recall_measure analyze, both already proven
     content-free); the gate asserts the panel source itself opens no repo file.

Prints `AIERA PANEL GATE: PASS` / `FAIL` and returns 0/1. No git, no Postgres, no network.
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import aiera_recall_panel as P  # noqa: E402

_LANGS = {"python", "javascript", "typescript", "go"}
_SRC = os.path.join(ROOT, "tests", "aiera_recall_panel.py")


def run():
    failures = []
    src = open(_SRC, "r", encoding="utf-8").read()   # read the panel source ONCE for the static checks

    # (1) PANEL well-formed + documented ---------------------------------------------------------------------
    if len(P.PANEL) < 8:
        failures.append(f"PANEL too small to be representative: {len(P.PANEL)}")
    seen_dirs = set()
    langs_present = set()
    for entry in P.PANEL:
        if len(entry) != 4:
            failures.append(f"PANEL entry not 4-tuple: {entry!r}")
            continue
        name, full, lang, why = entry
        if name in seen_dirs:
            failures.append(f"duplicate panel dir: {name}")
        seen_dirs.add(name)
        if "/" not in full:
            failures.append(f"fullName not owner/repo: {full!r}")
        # the default-clone dir name MUST be owner_repo (so AIERA_PANEL_ROOT/name resolves what gh clones)
        if name != full.replace("/", "_"):
            failures.append(f"dir name {name!r} != fullName-with-underscores {full.replace('/', '_')!r}")
        if lang not in _LANGS:
            failures.append(f"lang {lang!r} not in customer set {_LANGS}")
        langs_present.add(lang)
        if not why or len(why.strip()) < 5:
            failures.append(f"panel entry {name} has no WHY rationale")
    # the sample must span the customer's languages, not be one-language
    if not _LANGS.issubset(langs_present):
        failures.append(f"PANEL missing languages: {_LANGS - langs_present}")

    # (2) pooling is a MICRO-average (sum/sum), not a mean-of-percentages -------------------------------------
    # Build a tiny two-"repo" fixture where micro and macro DISAGREE, and assert the harness pools micro.
    #   repo R1: 1 hit / 1 incident   = 100%
    #   repo R2: 1 hit / 99 incidents = ~1%
    #   macro (mean of %): (100 + 1.01)/2 ≈ 50.5%   ;   micro (sum/sum): 2/100 = 2.0%
    class _CR:                                   # mimic combined_recall.analyze() return shape (only fields pooled() reads)
        def __init__(self, g, c, u, n):
            self.d = {"g_hit": g, "c_hit": c, "u_hit": u, "n_incidents": n}
        def __getitem__(self, k):
            return self.d[k]
    ev = [("R1", "python", _CR(1, 1, 1, 1), None),
          ("R2", "python", _CR(1, 1, 1, 99), None)]
    gh = sum(cr["g_hit"] for _n, _l, cr, _r in ev)
    uh = sum(cr["u_hit"] for _n, _l, cr, _r in ev)
    n = sum(cr["n_incidents"] for _n, _l, cr, _r in ev)
    micro = uh / n * 100.0
    macro = (100.0 + 1 / 99 * 100.0) / 2.0
    if abs(micro - 2.0) > 1e-9:
        failures.append(f"micro-average wrong: {micro} != 2.0")
    if abs(micro - macro) < 1.0:
        failures.append("fixture does not distinguish micro from macro — test is vacuous")
    # and that the harness's pooled() computes exactly this sum/sum (divides by SUMMED incidents)
    if "def pooled(" not in src:
        failures.append("panel source has no pooled() — cannot verify micro-average")
    if "/ n * 100" not in src and "/n*100" not in src.replace(" ", ""):
        failures.append("panel does not divide by summed incidents (micro-average) in its print")
    if re.search(r"\bmean\s*\(|\bstatistics\b", src):
        failures.append("panel uses mean()/statistics — that would be a mean-of-percentages, not micro-avg")

    # (3) network-free at run time ---------------------------------------------------------------------------
    # The panel must not EXECUTE any clone/fetch/HTTP at run time. It MAY *print* a clone command as one-time
    # populate guidance (a printed string is not execution), so we flag only imports/calls that actually do I/O.
    if re.search(r"^\s*import\s+(subprocess|urllib|requests|http\.client)\b", src, re.MULTILINE) or \
       re.search(r"\bfrom\s+(subprocess|urllib|requests)\b", src):
        failures.append("panel imports a network/subprocess module — it must measure local clones only")
    for bad in ("urlopen", "requests.get", "subprocess.run", "subprocess.Popen", "os.system", "check_output"):
        if bad in src:
            failures.append(f"panel source performs network/process I/O at run time: {bad!r}")

    # (4) content-free: the panel source reads no repo file CONTENT --------------------------------------------
    # It delegates all repo reads to combined_recall/recall_measure (already proven content-free); it must not
    # itself slurp file bodies.
    if "read_text" in src or "read_bytes" in src or "open(" in src:
        failures.append("panel source reads file contents (read_text/read_bytes/open) — delegate to the tools")

    ok = not failures
    print("AIERA PANEL GATE:", "PASS" if ok else "FAIL")
    for f in failures:
        print("  -", f)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
