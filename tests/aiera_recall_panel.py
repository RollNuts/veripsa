#!/usr/bin/env python3
"""AI-ERA RECALL PANEL — pool combined/graph/co-change recall over the REAL customer profile, reproducibly.

WHY THIS EXISTS (the measurement gap this closes):
  `tests/combined_recall.py` and `tests/recall_measure.py` each measure recall on ONE repo passed as an
  argument. Historically we pointed them at flask/django/zustand — PRE-AI, skilled-team libraries. But the
  PO's actual customer is "an amateur + an AI building an app/SaaS NOW (2024-2026, indie)". Those repos have
  a STRUCTURALLY DIFFERENT shape (full-stack monorepo: backend + frontend + native in one repo, big
  multi-file AI commits), so the recall number on flask does NOT predict the number on the customer's repo.

  This panel pins (a) the customer-representative repo list, (b) a pooled MICRO-average (sum hits / sum
  incidents — never a mean-of-percentages, which a tiny repo would distort), and (c) the dominant-miss
  breakdown, so any future round can re-measure "where do we stand on the REAL customer profile, and what
  coupling are we still blind to" with ONE command instead of re-deriving the panel each time.

HISTORICAL round6 baseline (SUPERSEDED — quote the PINNED 2026-07-20 numbers below; 12 evaluable repos, 14,589 pairs):
  GRAPH-only  13.6%   CO-CHANGE-only  15.8%   COMBINED  24.9%   (micro-avg; macro/median per-repo ~41%/34%).
RE-MEASURED 2026-07-20 (current main extractor; 9 evaluable repos, 10,741 pairs actually present locally):
  GRAPH-only  16.5%   CO-CHANGE-only  6.5%   COMBINED  20.7%.  Per-language: JS 25.8% / Python 15.6% /
  TS 30.5%.  GRAPH-only ROSE (16.5 vs 13.6 — recent contract-graph detectors help). The lower pooled
  COMBINED vs the 24.9% baseline is SAMPLE COMPOSITION, not a regression: this local sample is python-heavy
  (pipeshub-ai alone = 6,591/10,741 = 61% of pairs at 15.6% combined), and python co-change lift is only
  2.1%, which drags the pool. The DOMINANT MISS is UNCHANGED at no-graph-edge CROSS-DIR 83.6% — the AI-era
  signature below still holds. (Go repos + one Python repo yield 0 evaluable incident pairs on this panel.)
  DOMINANT MISS = no-graph-edge, CROSS-DIR (83% of the uncovered incident pairs). Content-free breakdown of
  those: ~61% SAME-extension (.py<->.py, .go<->.go = sibling feature-modules / value-coupling / registry/DI
  that share NO import/call/schema edge) and ~39% CROSS-extension dominated by BACKEND<->FRONTEND
  (.py<->.ts, .go<->.jsx, .go<->.svelte, src<->src-tauri = the client/server contract). This is the AI-era
  signature: the deepest coupling in an AI-built full-stack monorepo is the cross-tier contract + sibling-
  module co-evolution — neither shows up as a within-language structural edge, so the single-language code
  graph cannot see it, and co-change only partly catches it (AI ships large multi-file commits that wash out
  lift, and brand-new features have no history yet). The NEXT recall target this names is value/contract
  coupling (cross-tier + sibling-module), NOT more extractor precision (round6 proved that mature).

  Pre-AI baseline for contrast (flask/django/zustand): per-repo COMBINED 31% / 8% / 82%. flask's dominant
  miss is SAME-dir (one tight package); django is a huge outlier monorepo. The AI-era profile's miss is
  decisively CROSS-dir cross-tier — a different blind spot than the pre-AI baseline had.

CONTENT-FREE: only paths, per-commit path groupings, counts, and a boolean correction-pattern on the commit
SUBJECT ever leave a repo (inherited verbatim from combined_recall.py / recall_measure.py). No file bodies.

NETWORK-FREE AT RUN TIME: this does NOT clone. It measures whatever panel repos are already present locally
(default search root /tmp; override with AIERA_PANEL_ROOT). Repos absent locally are reported as 'absent
(clone to measure)' and skipped — so this is safe to run anywhere without flaky network dependence.

  To populate the panel locally (one-time, content-free — git history only):
    for r in <fullName ...>; do gh repo clone "$r" /tmp/"${r//\//_}" -- --depth 3000; done

Run:  python3 tests/aiera_recall_panel.py            # pool over locally-present panel repos
      AIERA_PANEL_ROOT=/path python3 tests/aiera_recall_panel.py
Exit 0 always — this is a measurement, not a pass/fail bar (the LOGIC is gated by 84-combined_recall.gate).
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import combined_recall as CR   # noqa: E402  (incident ground truth + dampened graph + shipping co-change)
import recall_measure as RM    # noqa: E402  (co-change ground truth + dampening recall-cost)

# ---------------------------------------------------------------------------------------------------------
# THE PANEL — AI-era / indie repos (created >= 2024-06, 20..3000 stars) across the languages the customer
# uses. Each entry: (local_dir_name, github_fullName, language_bucket, one-line WHY it represents the
# customer). The dir name is the `gh repo clone owner/repo /tmp/owner_repo` default (slashes -> underscores).
# This list is the artifact's POINT: a pinned, documented customer-representative sample, not an ad-hoc pick.
# ---------------------------------------------------------------------------------------------------------
PANEL = [
    ("pipeshub-ai_pipeshub-ai",                       "pipeshub-ai/pipeshub-ai",                       "python",     "full-stack AI workplace-search app (python backend + ts/tsx frontend monorepo)"),
    ("dw-dengwei_daily-arXiv-ai-enhanced",            "dw-dengwei/daily-arXiv-ai-enhanced",            "python",     "small indie AI pipeline (python + js glue)"),
    ("TheBlewish_Automated-AI-Web-Researcher-Ollama", "TheBlewish/Automated-AI-Web-Researcher-Ollama", "python",     "single-author AI research tool (tiny, recent)"),
    ("qingchencloud_clawpanel",                       "qingchencloud/clawpanel",                       "javascript", "indie JS control-panel app"),
    ("bytebase_dbhub",                                "bytebase/dbhub",                                "typescript", "TS MCP server / db tool (backend+frontend)"),
    ("keenthemes_reui",                               "keenthemes/reui",                               "typescript", "TS/TSX React component library (frontend-heavy)"),
    ("Mouseww_anything-analyzer",                     "Mouseww/anything-analyzer",                     "typescript", "indie TS analyzer app (ts backend + tsx UI)"),
    ("rishikanthc_Scriberr",                          "rishikanthc/Scriberr",                          "typescript", "self-hosted transcription app (svelte/ts front + go back)"),
    ("robinebers_openusage",                          "robinebers/openusage",                          "typescript", "indie usage-metering SaaS (ts + tsx + js)"),
    ("supabase-community_database-build",             "supabase-community/database-build",             "typescript", "in-browser postgres app (tsx/ts)"),
    ("kenn-io_agentsview",                            "kenn-io/agentsview",                            "go",         "AI-agent observability app (go backend + ts frontend)"),
    ("lejianwen_rustdesk-api",                        "lejianwen/rustdesk-api",                        "go",         "go API server for rustdesk (go + js/ts admin UI)"),
    ("OpenMind_OM1",                                  "OpenMind/OM1",                                  "go",         "go agent runtime (cmd/internal/plugins layout)"),
    ("PatchMon_PatchMon",                             "PatchMon/PatchMon",                             "go",         "self-hosted patch-monitoring app (go backend + jsx frontend)"),
]


def _present(name):
    root = os.environ.get("AIERA_PANEL_ROOT", "/tmp")
    p = os.path.join(root, name)
    return p if os.path.isdir(os.path.join(p, ".git")) else None


def _pair_key(a, b, by_dir=True):
    if by_dir:
        aa = os.path.dirname(a) or "."
        bb = os.path.dirname(b) or "."
    else:
        aa = os.path.splitext(a)[1].lower() or "(none)"
        bb = os.path.splitext(b)[1].lower() or "(none)"
    return " <-> ".join(sorted((aa, bb)))


def _inc(d, key, n=1):
    d[key] = d.get(key, 0) + n


def _top(d, limit=8):
    return sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def _xdir_no_edge_sample(repo):
    """Return a content-free sample of the dominant AI-era miss class for one repo.

    This deliberately reports only paths/counts/buckets. It reuses the already-gated combined_recall helpers
    and does not read source bodies or diffs. The goal is not another detector; it is to reveal which
    cross-directory no-edge buckets dominate before deciding the next narrow detector slice.
    """
    g = CR.X.build_graph(repo)
    files = {n["path"] for n in g["nodes"] if n["kind"] == "file"}
    graph_pairs = CR.graph_adjacency(g, files)
    cochange_pairs = CR.cochange_adjacency(repo, files)
    combined = graph_pairs | cochange_pairs
    incidents, _n_inc_commits = CR.incident_pairs(repo, files)

    raw_edge = set()
    for e in g["edges"]:
        if e["src"] in files and e["dst"] in files and e["src"] != e["dst"]:
            raw_edge.add(frozenset((e["src"], e["dst"])))

    dir_pairs, ext_pairs = {}, {}
    examples = []
    total = 0
    for k in incidents:
        if k in combined or k in raw_edge:
            continue
        a, b = sorted(tuple(k))
        if os.path.dirname(a) == os.path.dirname(b):
            continue
        total += 1
        _inc(dir_pairs, _pair_key(a, b, by_dir=True))
        _inc(ext_pairs, _pair_key(a, b, by_dir=False))
        if len(examples) < 5:
            examples.append(f"{a} <-> {b}")

    return {
        "total": total,
        "dir_pairs": _top(dir_pairs),
        "ext_pairs": _top(ext_pairs),
        "examples": examples,
    }


def main():
    print("\n=== AI-ERA RECALL PANEL (real customer profile: amateur + AI building app/SaaS, 2024-2026) ===")
    print("Reproducible pooled recall over a pinned, documented customer-representative repo sample.")
    print("Content-free + network-free at run time; measures only locally-present clones.\n")

    rows, absent = [], []
    for name, full, lang, why in PANEL:
        p = _present(name)
        if not p:
            absent.append((full, lang))
            continue
        try:
            cr = CR.analyze(p)
            rm = RM.analyze(p)
            sample = _xdir_no_edge_sample(p)
            rows.append((name, lang, cr, rm, sample))
        except Exception as e:                                  # never crash the panel on one bad repo
            print(f"  [skip] {name}: {e!r}")

    if not rows:
        print("NO panel repos present locally. This is HONEST 'not measured', not 0%.")
        print("Populate (one-time, content-free — git history only), then re-run:")
        for _name, full, lang, _why in PANEL:
            print(f"    gh repo clone {full} /tmp/{full.replace('/', '_')} -- --depth 3000   # {lang}")
        return 0

    # ---- per-repo (combined_recall incident ground truth) ----
    print("PER-REPO (correction-incident ground truth):")
    print(f"  {'repo':<42}{'lang':<11}{'files':>6}{'inc':>6}{'graph%':>8}{'cochg%':>8}{'COMB%':>8}  dominant_miss")
    for name, lang, cr, _rm, _sample in sorted(rows, key=lambda r: r[1]):
        if cr["n_incidents"] == 0:
            print(f"  {name:<42}{lang:<11}{cr['files']:>6}{0:>6}   (no incident pairs — not evaluated)")
            continue
        dm = cr["miss"]["dominant"]
        print(f"  {name:<42}{lang:<11}{cr['files']:>6}{cr['n_incidents']:>6}"
              f"{cr['g_rec']:>8.1f}{cr['c_rec']:>8.1f}{cr['u_rec']:>8.1f}  {dm[0]} ({dm[1]})")

    ev = [(n, l, cr, rm, sample) for (n, l, cr, rm, sample) in rows if cr["n_incidents"] > 0]

    def pooled(bucket):
        gh = sum(cr["g_hit"] for _n, _l, cr, _r, _s in bucket)
        ch = sum(cr["c_hit"] for _n, _l, cr, _r, _s in bucket)
        uh = sum(cr["u_hit"] for _n, _l, cr, _r, _s in bucket)
        n = sum(cr["n_incidents"] for _n, _l, cr, _r, _s in bucket)
        return gh, ch, uh, n

    print("\nPER-LANGUAGE POOLED (micro-average over incident pairs):")
    for L in sorted(set(l for _n, l, _c, _r, _s in ev)):
        b = [r for r in ev if r[1] == L]
        gh, ch, uh, n = pooled(b)
        if n:
            print(f"  {L:<12} repos={len(b):<2} incidents={n:<6} "
                  f"graph {gh/n*100:5.1f}%  co-change {ch/n*100:5.1f}%  COMBINED {uh/n*100:5.1f}%")

    gh, ch, uh, n = pooled(ev)
    print("\nPOOLED ACROSS THE AI-ERA PANEL (micro-average over incident pairs):")
    print(f"  evaluable repos={len(ev)}   total incident pairs={n}")
    if n:
        print(f"    GRAPH-only     {gh/n*100:5.1f}%  ({gh}/{n})")
        print(f"    CO-CHANGE-only {ch/n*100:5.1f}%  ({ch}/{n})")
        print(f"    COMBINED       {uh/n*100:5.1f}%  ({uh}/{n})   ← the one honest number for this profile")

    # ---- pooled dominant-miss breakdown ----
    B = {"no_history": 0, "no_graph_edge_xdir": 0, "no_graph_edge_samedir": 0, "graph_edge_but_missed": 0}
    tm = 0
    for _n, _l, cr, _r, _s in ev:
        for k in B:
            B[k] += cr["miss"]["buckets"][k]
        tm += cr["miss"]["total_missed"]
    print(f"\nPOOLED DOMINANT MISS (of {tm} incident pairs NEITHER detector covers):")
    for k, v in sorted(B.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<26}{v:>7}  ({v/max(tm,1)*100:4.1f}%)")
    print("  → the AI-era blind spot is no-edge CROSS-DIR coupling: cross-tier backend<->frontend contracts")
    print("    and sibling feature-module co-evolution that share NO within-language structural edge.")

    # ---- pooled content-free xdir no-edge sampler ----
    repo_dir_pairs, ext_pairs = {}, {}
    print("\nXDIR NO-EDGE MISS SAMPLER (content-free; top buckets before another detector):")
    for name, _l, _cr, _rm, sample in ev:
        for key, count in sample["dir_pairs"]:
            _inc(repo_dir_pairs, f"{name}: {key}", count)
        for key, count in sample["ext_pairs"]:
            _inc(ext_pairs, key, count)
    print("  top repo+directory buckets:")
    for key, count in _top(repo_dir_pairs, limit=10):
        print(f"    {count:>6}  {key}")
    print("  top extension buckets:")
    for key, count in _top(ext_pairs, limit=8):
        print(f"    {count:>6}  {key}")
    print("  example missed path pairs per most-affected repos:")
    for name, _l, _cr, _rm, sample in sorted(ev, key=lambda r: -r[4]["total"])[:5]:
        print(f"    {name}: total_xdir_no_edge={sample['total']}")
        for pair in sample["examples"][:3]:
            print(f"      - {pair}")

    # ---- pooled dampening recall-cost (recall_measure co-change ground truth) ----
    tg = sum(rm["n_gt"] for _n, _l, _c, rm, _s in rows)
    traw = sum(rm["raw_cov"] for _n, _l, _c, rm, _s in rows)
    tlive = sum(rm["live_cov"] for _n, _l, _c, rm, _s in rows)
    print(f"\nDAMPENING RECALL-COST (co-change ground truth, lift>=2 & support>=3; {tg} GT pairs):")
    if tg:
        print(f"    RAW-graph recall {traw/tg*100:5.1f}%   LIVE (dampened) {tlive/tg*100:5.1f}%   "
              f"→ hub dampening costs {(traw - tlive)/tg*100:.1f} pts on this profile")

    if absent:
        print(f"\nABSENT locally ({len(absent)} — clone to include): " +
              ", ".join(f"{f} [{l}]" for f, l in absent))

    print("\nHONEST BOUNDARY: incident & co-change ground truths are PROXIES (message heuristic imperfect;")
    print("not all coupling surfaces as a fix/co-change). No single number is 'truth'; this is a consistent,")
    print("reproducible measurement of where we stand on the REAL customer profile and what we are blind to.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
