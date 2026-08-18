#!/usr/bin/env python3
"""RECALL INTEGRITY — additive, high-trust measurement views layered on top of the legacy panel.

WHY THIS EXISTS (the measurement gaps this closes — none of the legacy tools are changed):
  The AI-era panel (`tests/aiera_recall_panel.py`) reports ONE pooled MICRO-average of graph/co-change/
  combined recall over whatever panel repos happen to be present locally. Issue #721's whole history shows
  that number is NOT reproducible run-to-run and NOT robust:

    (1) NO EXACT-SHA PIN. Every prior workflow re-cloned the panel at run time, so the incident denominator
        drifted (18028 -> 18049 -> 18051 -> 18110) and the *evaluable set itself* changed (the 4 Go repos are
        0-evaluable at one clone depth and top-of-panel at another). A/B claims were all hedged "live-panel
        comparison, not pinned A/B". => We pin each repo's exact HEAD SHA + evaluable-incident count and make
        drift LOUD (a present repo whose HEAD != the pinned SHA is reported, never silently folded in).

    (2) MICRO IS DOMINATED BY ONE REPO'S BIG COMMITS. On the pinned 2026-07-20 panel, pipeshub-ai is
        6591/10741 = 61% of all incident pairs at a low combined, so the "one honest number" (micro 20.7%) is
        ~pipeshub's number; macro is 31.1% and median 29.8%. A single giant correction commit contributes
        C(n,2) pairs, so big commits dominate a pair-micro-average. => We add (a) MACRO + MEDIAN as first-class
        views and (b) a COMMIT-SIZE-WEIGHTED recall where each correction commit has total weight 1 (each of
        its C(n,2) pairs weighs 1/C(n,2)), so a 20-file commit cannot outvote twenty 2-file commits.

    (3) THE GROUND TRUTH MIXES CLEAN 2-file fixes WITH 30-file mechanical sweeps. => We add a HIGH-CONFIDENCE
        correction panel: 2..5 file corrections only, mechanical (rename/format/lint/bump/regenerate) subjects
        excluded, with per-pair SUPPORT (how many distinct corrections a pair recurs across).

    (4) "MISS" CONFLATES "product would say CLEAR" WITH "product would say UNKNOWN". The engine drops
        hub/fan-out-dampened edges from a WARN, but a real extracted edge that was dampened renders as an
        honest 'unknown' (never a false 'clear') for two in-flight changes (core._dampened_adjacency). => We
        add an UNKNOWN-AWARE view splitting the uncovered incident pairs into "edge exists but dampened =
        at-least-unknown" vs "no edge at all = truly clear-blind", so the REAL blind spot (anchor-less pairs)
        is separated from the part the product already refuses to call clear.

  The legacy micro number is preserved verbatim (imported from combined_recall). These are STRICTLY ADDITIONAL
  high-trust indicators, per the issue's rule: never delete/loosen a legacy metric to raise a number.

CONTENT-FREE (inherited from combined_recall verbatim): only file PATHS, the per-commit GROUPING of paths,
COUNTS, SHAs, and a boolean pattern on the commit SUBJECT (never body, never diff) ever leave a repo. The
git-free LOGIC functions at the top take synthetic inputs and are what the gate proves.

Run:  python3 tests/recall_integrity.py [/repo ...]     # measure present panel repos (or given repos)
Exit 0 always — a measurement, not a pass/fail bar (the LOGIC is gated by the recall-integrity gate).
"""
from __future__ import annotations

import math
import os
import re
import statistics
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import combined_recall as CR   # noqa: E402  (legacy incident ground truth + dampened graph + shipping co-change)

# ---------------------------------------------------------------------------------------------------------
# PINNED PANEL MANIFEST — the exact HEAD SHA + evaluable-incident count of every panel repo AT THE MOMENT the
# 2026-07-20 baseline was frozen, so any later run can prove it is measuring the SAME corpus (not a drifted
# reclone). full_name -> (head_sha, evaluable_incident_pairs, language). evaluable_incident_pairs is the count
# combined_recall.incident_pairs() produced on that SHA; 0 means "present but 0 correction-incident pairs" (the
# repo contributes files but no ground truth — e.g. the Go repos on this clone). Content-free: SHAs + counts.
# ---------------------------------------------------------------------------------------------------------
PANEL_MANIFEST = {
    "pipeshub-ai/pipeshub-ai":                       ("710607735c7c", 6591, "python"),
    "dw-dengwei/daily-arXiv-ai-enhanced":            ("bad86729eaaf", 2,    "python"),
    "TheBlewish/Automated-AI-Web-Researcher-Ollama": ("2f96502c58d8", 0,    "python"),
    "qingchencloud/clawpanel":                       ("62d852c2b305", 1485, "javascript"),
    "bytebase/dbhub":                                ("ddf19e183be6", 286,  "typescript"),
    "keenthemes/reui":                               ("93cea255c5a2", 50,   "typescript"),
    "Mouseww/anything-analyzer":                     ("771d4f6bdd85", 53,   "typescript"),
    "rishikanthc/Scriberr":                          ("bdb8838b8b9e", 950,  "typescript"),
    "robinebers/openusage":                          ("9d2bf09f10e2", 1290, "typescript"),
    "supabase-community/database-build":             ("de2c18d36f7f", 34,   "typescript"),
    "kenn-io/agentsview":                            (None,           0,    "go"),
    "lejianwen/rustdesk-api":                        (None,           0,    "go"),
    "OpenMind/OM1":                                  (None,           0,    "go"),
    "PatchMon/PatchMon":                             (None,           0,    "go"),
}

# DEV / HOLDOUT SPLIT (issue #721 discipline: a detector may be tuned ONLY on DEV; HOLDOUT is reserved for the
# FINAL before/after and is NEVER inspected during detector design). Stratified by language + repo shape.
# CAVEAT — a measurement finding, not an oversight: the pinned EVALUABLE set is typescript-heavy with only ONE
# evaluable javascript repo (clawpanel) and TWO python repos (pipeshub-ai dominant at 61% of pairs; daily-arXiv
# trivial at 2 pairs), and the 4 go repos are 0-evaluable on this clone. A language-BALANCED holdout is
# therefore impossible today: DEV must keep the js + both py repos to retain ANY per-language dev signal, so
# HOLDOUT is all-typescript. Expanding the panel with more evaluable non-ts repos is the real fix (baseline doc).
HOLDOUT_REPOS = frozenset({
    "bytebase/dbhub",                 # ts, small backend+frontend MCP/db tool
    "robinebers/openusage",           # ts, mid usage-metering SaaS
    "Mouseww/anything-analyzer",      # ts, tiny analyzer app
})
DEV_REPOS = frozenset(
    full for full, (_sha, ev, _lang) in PANEL_MANIFEST.items()
    if ev > 0 and full not in HOLDOUT_REPOS
)

# universal correction-marker (mirrors combined_recall._CORRECTION verbatim: revert/fix/bug/hotfix/regression/
# broke/breakage/patch on the commit SUBJECT only).
_CORRECTION = CR._CORRECTION
_REC = CR._REC

# MECHANICAL-COMMIT markers (content-free, subject only): a "fix" whose subject is a rename/format/lint/typo/
# dependency-bump/regenerate is a mechanical sweep, not a logic correction — excluded from the high-confidence
# panel so a format sweep that says "fix lint" cannot mint incident pairs. Word-boundary, case-insensitive.
_MECHANICAL = re.compile(
    r"\b(?:rename[sd]?|reformat(?:ted)?|format(?:ting|ted)?|prettier|eslint|gofmt|rubocop|black|isort|"
    r"whitespace|indent(?:ation)?|lint(?:ing)?|typo[s]?|spelling|bump|dependen\w*|regenerate[sd]?|"
    r"re-?gen(?:erated)?|codegen|generated|vendor(?:ed)?|lockfile|lock[\- ]?file)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------------------------------------
# PURE, GIT-FREE LOGIC (this is what the gate proves deterministically; no git / Postgres / network).
# ---------------------------------------------------------------------------------------------------------


def is_mechanical(subject: str) -> bool:
    """Content-free: does this commit SUBJECT read as a mechanical sweep (rename/format/lint/bump/regen)?"""
    return bool(_MECHANICAL.search(subject or ""))


def pairs_of(files):
    """All unordered pairs of a deduped file list, as frozensets. Deterministic."""
    fl = sorted(set(files))
    out = []
    for i in range(len(fl)):
        for j in range(i + 1, len(fl)):
            out.append(frozenset((fl[i], fl[j])))
    return out


def weighted_ground_truth(commit_filelists):
    """COMMIT-SIZE-WEIGHTED ground truth. `commit_filelists` = list of per-correction-commit file lists.
    Each commit contributes TOTAL weight 1, split evenly across its C(n,2) pairs (weight 1/C(n,2) each); a
    pair recurring across commits ACCUMULATES weight. Returns (pair_weight: dict, total_weight: float).
    total_weight == number of contributing commits (each adds exactly 1). Pure/deterministic."""
    pair_weight: dict = {}
    total = 0.0
    for files in commit_filelists:
        ps = pairs_of(files)
        if not ps:
            continue
        w = 1.0 / len(ps)
        for p in ps:
            pair_weight[p] = pair_weight.get(p, 0.0) + w
        total += 1.0
    return pair_weight, total


def weighted_recall(pair_weight, total_weight, detector_pairs):
    """Fraction of TOTAL correction-commit weight the detector covers. Each commit counts equally regardless
    of size, so one giant commit's C(n,2) pairs cannot dominate. Returns a percentage in [0,100]."""
    if total_weight <= 0:
        return 0.0
    covered = sum(w for p, w in pair_weight.items() if p in detector_pairs)
    return covered / total_weight * 100.0


def unknown_aware_split(incidents, combined, raw_edge):
    """Split incident pairs into three HONEST tiers (the "would the product falsely say CLEAR?" question):
      warned     = in `combined` (graph-dampened ∪ shipping co-change) — the product surfaces a signal.
      unknown    = NOT warned but a RAW extracted edge exists (dampened/fan-out-dropped) — the product renders
                   at-least-'unknown' for two in-flight endpoints (core._dampened_adjacency), never a false clear.
      clear_blind= NO edge at all AND not co-change — the TRUE blind spot (anchor-less value coupling).
    Returns counts dict. Pure set math; deterministic. `raw_edge` = every extracted src<->dst edge (undampened)."""
    warned = unknown = clear_blind = 0
    for k in incidents:
        if k in combined:
            warned += 1
        elif k in raw_edge:
            unknown += 1
        else:
            clear_blind += 1
    n = len(incidents)
    return {
        "n": n, "warned": warned, "unknown": unknown, "clear_blind": clear_blind,
        "warned_pct": (warned / n * 100.0) if n else 0.0,
        "not_clear_pct": ((warned + unknown) / n * 100.0) if n else 0.0,   # avoids a FALSE clear
        "clear_blind_pct": (clear_blind / n * 100.0) if n else 0.0,
    }


def macro(values):
    """Mean of per-repo percentages (each repo counts equally — the anti-dote to one big repo dominating)."""
    return statistics.mean(values) if values else 0.0


def median(values):
    return statistics.median(values) if values else 0.0


# ---------------------------------------------------------------------------------------------------------
# GIT-BACKED helpers (content-free: --name-only paths + %s subject only; never body/diff). Mirror
# combined_recall.incident_pairs' git usage exactly.
# ---------------------------------------------------------------------------------------------------------


def correction_commits(repo, files):
    """Return a list of (subject, [tracked files]) for every non-merge CORRECTION commit, restricted to files
    present at HEAD (the graph universe). Content-free: %s subject (for the boolean pattern) + --name-only
    paths only — never the commit body, never a diff. The caller applies size / mechanical filters."""
    out = subprocess.run(
        ["git", "-C", repo, "log", "--no-merges", "--name-only", f"--pretty=format:{_REC}%s"],
        capture_output=True, text=True).stdout
    records = []
    subject = None
    is_corr = False
    cur: list = []

    def flush():
        if is_corr and len(cur) >= 2:
            records.append((subject, sorted(set(cur))))

    for line in out.split("\n"):
        if line.startswith(_REC):
            flush()
            subject = line[len(_REC):]
            is_corr = bool(_CORRECTION.search(subject))
            cur = []
        elif line.strip() in files:
            cur.append(line.strip())
    flush()
    return records


def head_sha(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


# ---------------------------------------------------------------------------------------------------------
def analyze_integrity(repo):
    """All additive high-trust metrics for one repo. Reuses combined_recall's graph/co-change/incident logic
    verbatim (legacy micro numbers unchanged) and layers weighted / high-confidence / unknown-aware on top."""
    g = CR.X.build_graph(repo)
    files = {n["path"] for n in g["nodes"] if n["kind"] == "file"}
    graph_pairs = CR.graph_adjacency(g, files)
    cochange_pairs = CR.cochange_adjacency(repo, files)
    combined = graph_pairs | cochange_pairs
    raw_edge = set()
    for e in g["edges"]:
        if e["src"] in files and e["dst"] in files and e["src"] != e["dst"]:
            raw_edge.add(frozenset((e["src"], e["dst"])))

    incidents, _ = CR.incident_pairs(repo, files)                     # LEGACY ground truth (2..30 files)

    records = correction_commits(repo, files)
    legacy_lists = [fl for _s, fl in records if 2 <= len(fl) <= CR.MAX_COMMIT_FILES]
    pair_weight, total_weight = weighted_ground_truth(legacy_lists)   # weighted over the SAME universe

    # high-confidence: 2..5 file, non-mechanical corrections; support = #distinct corrections a pair recurs in.
    hc_support: dict = {}
    hc_commits = 0
    for subject, fl in records:
        if not (2 <= len(fl) <= 5) or is_mechanical(subject):
            continue
        hc_commits += 1
        for p in pairs_of(fl):
            hc_support[p] = hc_support.get(p, 0) + 1
    hc_pairs = set(hc_support)

    def rec(det, universe):
        if not universe:
            return 0, 0.0
        hit = sum(1 for k in universe if k in det)
        return hit, hit / len(universe) * 100.0

    g_hit, g_rec = rec(graph_pairs, incidents)
    c_hit, c_rec = rec(cochange_pairs, incidents)
    u_hit, u_rec = rec(combined, incidents)
    hc_g = rec(graph_pairs, hc_pairs)[1]
    hc_c = rec(cochange_pairs, hc_pairs)[1]
    hc_u = rec(combined, hc_pairs)[1]

    return {
        "repo": os.path.basename(repo.rstrip("/")), "files": len(files),
        "n_incidents": len(incidents),
        "g_hit": g_hit, "c_hit": c_hit, "u_hit": u_hit,
        "g_rec": g_rec, "c_rec": c_rec, "u_rec": u_rec,
        "w_graph": weighted_recall(pair_weight, total_weight, graph_pairs),
        "w_cochange": weighted_recall(pair_weight, total_weight, cochange_pairs),
        "w_combined": weighted_recall(pair_weight, total_weight, combined),
        "w_total_commits": total_weight,
        "hc_pairs": len(hc_pairs), "hc_commits": hc_commits,
        "hc_graph": hc_g, "hc_cochange": hc_c, "hc_combined": hc_u,
        "hc_recur": sum(1 for v in hc_support.values() if v >= 2),
        "unknown_aware": unknown_aware_split(incidents, combined, raw_edge),
    }


# ---------------------------------------------------------------------------------------------------------
def check_manifest(present_map):
    """present_map: {full_name: head_sha_or_None}. Report drift vs PANEL_MANIFEST. Content-free (SHAs only)."""
    rows = []
    for full, (pin_sha, pin_ev, lang) in PANEL_MANIFEST.items():
        cur = present_map.get(full)
        if cur is None:
            rows.append((full, "absent", pin_sha, None))
        elif pin_sha is None:
            rows.append((full, "present (unpinned)", pin_sha, cur[:12]))
        elif cur.startswith(pin_sha) or cur[:12] == pin_sha:
            rows.append((full, "pinned-match", pin_sha, cur[:12]))
        else:
            rows.append((full, "DRIFT", pin_sha, cur[:12]))
    return rows


def _present_root():
    return os.environ.get("AIERA_PANEL_ROOT", "/tmp")


def _local_dir(full):
    return full.replace("/", "_")


def main():
    args = [os.path.abspath(p) for p in sys.argv[1:]]
    root = _present_root()
    if args:
        targets = [(os.path.basename(p), None, p) for p in args]
    else:
        targets = []
        for full in PANEL_MANIFEST:
            p = os.path.join(root, _local_dir(full))
            if os.path.isdir(os.path.join(p, ".git")):
                targets.append((full, PANEL_MANIFEST[full][2], p))

    print("\n=== RECALL INTEGRITY (additive high-trust views; legacy micro preserved) ===")
    if not targets:
        print("NO panel repos present locally (AIERA_PANEL_ROOT=%s). Honest 'not measured'." % root)
        return 0

    present_map = {full: head_sha(p) for full, _l, p in targets if _l is not None}
    print("\nMANIFEST DRIFT CHECK (pinned 2026-07-20 exact-SHA corpus):")
    for full, status, pin, cur in check_manifest(present_map):
        print(f"  {status:<20} {full:<46} pinned={pin} head={cur}")

    rows = []
    for full, lang, p in targets:
        try:
            r = analyze_integrity(p)
            r["lang"] = lang or "?"
            rows.append(r)
        except Exception as e:
            print(f"  [skip] {full}: {e!r}")
    ev = [r for r in rows if r["n_incidents"] > 0]
    if not ev:
        print("No evaluable repos (0 incident pairs). Honest 'not evaluated'.")
        return 0

    def pooled(bucket, hk, nk):
        return sum(r[hk] for r in bucket), sum(r[nk] for r in bucket)

    gh = sum(r["g_hit"] for r in ev); ch = sum(r["c_hit"] for r in ev)
    uh = sum(r["u_hit"] for r in ev); n = sum(r["n_incidents"] for r in ev)
    print("\nLEGACY MICRO (verbatim from combined_recall — the comparison-stable number):")
    print(f"  graph {gh/n*100:5.1f}%   co-change {ch/n*100:5.1f}%   COMBINED {uh/n*100:5.1f}%   (n={n})")
    print("\nADDITIVE HIGH-TRUST VIEWS (each repo weighted equally / big commits down-weighted):")
    print(f"  MACRO  (mean of per-repo)   graph {macro([r['g_rec'] for r in ev]):5.1f}%   "
          f"co-change {macro([r['c_rec'] for r in ev]):5.1f}%   COMBINED {macro([r['u_rec'] for r in ev]):5.1f}%")
    print(f"  MEDIAN (per-repo)           graph {median([r['g_rec'] for r in ev]):5.1f}%   "
          f"co-change {median([r['c_rec'] for r in ev]):5.1f}%   COMBINED {median([r['u_rec'] for r in ev]):5.1f}%")
    wt = sum(r["w_total_commits"] for r in ev)
    wg = sum(r["w_graph"] * r["w_total_commits"] for r in ev) / max(wt, 1)
    wc = sum(r["w_cochange"] * r["w_total_commits"] for r in ev) / max(wt, 1)
    wu = sum(r["w_combined"] * r["w_total_commits"] for r in ev) / max(wt, 1)
    print(f"  COMMIT-WEIGHTED (1/C(n,2))  graph {wg:5.1f}%   co-change {wc:5.1f}%   COMBINED {wu:5.1f}%   "
          f"(commits={int(wt)})")
    hn = sum(r["hc_pairs"] for r in ev)
    hg = sum(r["hc_graph"] * r["hc_pairs"] for r in ev) / max(hn, 1)
    hcc = sum(r["hc_cochange"] * r["hc_pairs"] for r in ev) / max(hn, 1)
    hu = sum(r["hc_combined"] * r["hc_pairs"] for r in ev) / max(hn, 1)
    print(f"  HIGH-CONFIDENCE (2..5 files, non-mechanical)  graph {hg:5.1f}%   co-change {hcc:5.1f}%   "
          f"COMBINED {hu:5.1f}%   (hc_pairs={hn})")

    W = sum(r["unknown_aware"]["warned"] for r in ev)
    U = sum(r["unknown_aware"]["unknown"] for r in ev)
    Cb = sum(r["unknown_aware"]["clear_blind"] for r in ev)
    print("\nUNKNOWN-AWARE (of the SAME incident pairs — 'would the product falsely say CLEAR?'):")
    print(f"  warned (signal)              {W/n*100:5.1f}%  ({W})")
    print(f"  + at-least-unknown (dampened edge exists, never a false clear)  {(W+U)/n*100:5.1f}%  (+{U})")
    print(f"  clear-blind (NO edge at all — the TRUE anchor-less blind spot)  {Cb/n*100:5.1f}%  ({Cb})")

    print("\nPER-LANGUAGE (micro):")
    for L in sorted(set(r["lang"] for r in ev)):
        b = [r for r in ev if r["lang"] == L]
        gh2 = sum(r["g_hit"] for r in b); uh2 = sum(r["u_hit"] for r in b); n2 = sum(r["n_incidents"] for r in b)
        print(f"  {L:<12} repos={len(b)} inc={n2:<6} graph {gh2/n2*100:5.1f}%  COMBINED {uh2/n2*100:5.1f}%")

    print("\nHONEST BOUNDARY: incident ground truth is a PROXY (correction-message heuristic). These are")
    print("ADDITIVE views; the legacy micro number above is unchanged and stays the comparison anchor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
