#!/usr/bin/env python3
"""CROSS-TIER ROUTE↔CALL coupling probe (MEASURE-FIRST, standalone, content-free).

WHY (PR #268 finding): on the AI-era full-stack customer (amateur+AI monorepos) 83% of the
coupling Veripsa MISSES is "cross-dir, no graph edge", and ~39% of THAT is backend↔frontend
(`.py↔.ts`, `.go↔.svelte`, `src↔src-tauri`). A single-language structural graph cannot see it
and co-change only partly does. The coupling is the **API contract**: a backend file DEFINES a
route string, a frontend file ISSUES a request to that same route. Change the route/contract on
one side and the other breaks. This probe extracts both sides and MATCHES them — a candidate
cross-tier coupling between a backend file and a frontend file — so we can MEASURE whether the
signal is real (co-change) and precise (false-positive rate) before deciding to build it.

INTEGRATION NOTE (PR #270 → product): the signal proved real + precise, so the extraction +
matching + specificity floors were PROMOTED into the SHIPPED extractor as the `_cg_routes` graph
producer (a root module, COPY'd into the image). This probe now IMPORTS that proven logic from
`_cg_routes` (the single source of truth) and keeps ONLY the MEASUREMENT path (co-change lift vs a
random cross-tier baseline, panel discovery/clone). The producer and the probe can never drift —
they run the identical extract/normalize/floor/match code.

CONTENT-FREE: this reads only URL/route PATH STRINGS and file paths. Never request/response
bodies, never source semantics beyond the literal route token.

PRECISION-SAFE: a ubiquitous/short path (`/`, `/api`, `/health`, `/login`) must NOT couple
everything. The "specificity floor" (default ON) drops bare/short/ubiquitous routes and keeps
only SPECIFIC routes. When a match is ambiguous it is NOT emitted. (See _cg_routes.is_specific.)

Run (measurement):  python3 tests/cross_tier_route_probe.py /repo [/repo2 ...]
Run (discover+clone+measure AI-era full-stack panel):  python3 tests/cross_tier_route_probe.py --panel
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

# The PROVEN extraction + matching + specificity-floor logic now lives in the SHIPPED root module
# `_cg_routes` (so the build_graph producer can run it inside the image without a `tests/` import).
# This probe imports it from there — one source of truth, no drift between probe and producer.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _cg_routes import (  # noqa: E402  (re-exported for the measurement path + any external caller)
    BACKEND_EXT, FRONTEND_EXT, ROUTE_DEF_EXT, REQUEST_EXT,
    extract_route_defs, extract_request_urls, normalize_route,
    is_specific, match_routes, match_pairs, scan_repo, _ext,
)


# ---------------------------------------------------------------------------------------------
# Measurement: co-change lift of matched pairs vs random cross-tier pairs
# ---------------------------------------------------------------------------------------------
def _tier(rel: str) -> str:
    """'backend' | 'frontend' | 'other' by extension (for the random-baseline cross-tier control)."""
    e = _ext(rel)
    if e in BACKEND_EXT:
        return "backend"
    if e in (".svelte", ".vue", ".jsx", ".tsx"):
        return "frontend"
    if e in (".ts", ".js"):
        return "either"   # ambiguous; counts as cross-tier vs backend
    return "other"


def commit_touchsets(repo: str, files: set, max_commit_files: int = 50):
    out = subprocess.run(
        ["git", "-C", repo, "log", "--no-merges", "--name-only", "--pretty=format:@@%H"],
        capture_output=True, text=True).stdout
    touch, idx, cur, T = {}, -1, [], set()

    def flush():
        if cur and len(cur) <= max_commit_files:
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


def cochange_lift(repo: str, pair_files: set, touch: dict, T: int):
    """lift = (co-changes * total-commits) / (changes_a * changes_b). >1 = co-change above base rate."""
    a, b = tuple(pair_files)
    sa, sb = touch.get(a), touch.get(b)
    if not sa or not sb or T == 0:
        return None
    nco = len(sa & sb)
    if len(sa) == 0 or len(sb) == 0:
        return None
    return (nco * T) / (len(sa) * len(sb))


def measure_repo(repo: str, specificity_floor: bool = True, seed: int = 1) -> dict:
    import random
    repo = os.path.abspath(repo)
    scan = scan_repo(repo, specificity_floor=specificity_floor)
    pairs = scan["pairs"]
    # universe of files with route/request signal (for history)
    all_rel = set(scan["defs"]) | set(scan["reqs"])
    touch, T = commit_touchsets(repo, all_rel)

    matched_lifts, matched_co = [], 0
    for pf in pairs:
        lf = cochange_lift(repo, pf, touch, T)
        if lf is not None:
            matched_lifts.append(lf)
            a, b = tuple(pf)
            if len(touch.get(a, set()) & touch.get(b, set())) > 0:
                matched_co += 1

    # RANDOM cross-tier control: backend-file × frontend-file pairs that did NOT match a route,
    # sampled, with the SAME history requirement. This is the apples-to-apples baseline.
    backend_files = [f for f in all_rel if f in scan["defs"]]
    frontend_files = [f for f in all_rel if f in scan["reqs"]]
    matched_set = set(pairs)
    rnd = random.Random(seed)
    rand_pool = []
    for bf in backend_files:
        for ff in frontend_files:
            if bf == ff:
                continue
            if frozenset((bf, ff)) in matched_set:
                continue
            if _tier(bf) == _tier(ff):   # keep it CROSS-tier
                continue
            rand_pool.append(frozenset((bf, ff)))
    rnd.shuffle(rand_pool)
    rand_lifts, rand_co, sampled = [], 0, 0
    for pf in rand_pool:
        if sampled >= 2000:
            break
        lf = cochange_lift(repo, pf, touch, T)
        if lf is not None:
            rand_lifts.append(lf)
            a, b = tuple(pf)
            if len(touch.get(a, set()) & touch.get(b, set())) > 0:
                rand_co += 1
            sampled += 1

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    return {
        "repo": os.path.basename(repo.rstrip("/")),
        "T": T,
        "backend_files": len(backend_files), "frontend_files": len(frontend_files),
        "matched_pairs": len(pairs),
        "matched_with_history": len(matched_lifts),
        "matched_mean_lift": mean(matched_lifts),
        "matched_cochange_rate": (matched_co / len(matched_lifts)) if matched_lifts else 0.0,
        "random_sampled": len(rand_lifts),
        "random_mean_lift": mean(rand_lifts),
        "random_cochange_rate": (rand_co / len(rand_lifts)) if rand_lifts else 0.0,
        "pairs": {tuple(sorted(k)): sorted(v) for k, v in pairs.items()},
    }


# ---------------------------------------------------------------------------------------------
# AI-era full-stack panel discovery + clone
# ---------------------------------------------------------------------------------------------
def discover_panel(limit: int = 40) -> list:
    """Use `gh search repos` to find recent (AI-era) repos, then KEEP only those with BOTH a
    backend dir and a frontend dir (full-stack)."""
    candidates: list = []
    for query in [
        "fastapi react in:name,description,readme",
        "flask react fullstack",
        "fastapi nextjs",
        "go react fullstack",
        "express react app",
        "fastapi svelte",
        "tauri app",
    ]:
        r = subprocess.run(
            ["gh", "search", "repos", query, "--created", ">=2024-06-01",
             "--stars", "20..3000", "--limit", "25", "--json", "fullName"],
            capture_output=True, text=True)
        if r.returncode != 0:
            continue
        try:
            for item in json.loads(r.stdout):
                candidates.append(item["fullName"])
        except json.JSONDecodeError:
            continue
    # dedupe preserving order
    seen, out = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
        if len(out) >= limit:
            break
    return out


def clone_and_measure_panel(limit: int = 30, specificity_floor: bool = True) -> list:
    repos = discover_panel(limit)
    rows = []
    with tempfile.TemporaryDirectory() as workdir:
        for full in repos:
            dst = os.path.join(workdir, full.replace("/", "__"))
            cl = subprocess.run(
                ["git", "clone", "--depth", "400", "--filter=blob:none",
                 f"https://github.com/{full}.git", dst],
                capture_output=True, text=True)
            if cl.returncode != 0:
                continue
            try:
                scan = scan_repo(dst, specificity_floor=specificity_floor)
                if not scan["defs"] or not scan["reqs"]:
                    continue   # not actually full-stack with detectable routes
                m_on = measure_repo(dst, specificity_floor=True)
                m_off = measure_repo(dst, specificity_floor=False)
                m_on["full"] = full
                m_on["matched_pairs_nofloor"] = m_off["matched_pairs"]
                rows.append(m_on)
            except Exception as e:   # noqa: BLE001 — measurement must never crash the panel
                sys.stderr.write(f"[skip {full}] {e}\n")
    return rows


def _print_rows(rows: list) -> None:
    print("\n=== CROSS-TIER ROUTE↔CALL coupling — co-change lift vs random (specificity floor ON) ===")
    print(f"{'repo':32} {'commits':>7} {'be/fe':>8} {'pairs':>6} {'m.lift':>7} {'m.co%':>6} "
          f"{'r.lift':>7} {'r.co%':>6}")
    agg_ml, agg_rl, agg_mc, agg_rc = [], [], [], []
    tot_pairs = tot_pairs_nofloor = 0
    for r in rows:
        print(f"{r.get('full', r['repo'])[:32]:32} {r['T']:>7} "
              f"{r['backend_files']}/{r['frontend_files']:>4} {r['matched_pairs']:>6} "
              f"{r['matched_mean_lift']:>7.2f} {r['matched_cochange_rate']*100:>5.0f}% "
              f"{r['random_mean_lift']:>7.2f} {r['random_cochange_rate']*100:>5.0f}%")
        if r["matched_with_history"]:
            agg_ml.append(r["matched_mean_lift"]); agg_mc.append(r["matched_cochange_rate"])
        if r["random_sampled"]:
            agg_rl.append(r["random_mean_lift"]); agg_rc.append(r["random_cochange_rate"])
        tot_pairs += r["matched_pairs"]
        tot_pairs_nofloor += r.get("matched_pairs_nofloor", r["matched_pairs"])

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0
    print("-" * 90)
    print(f"PANEL MEAN  matched lift={mean(agg_ml):.2f}  matched co-change={mean(agg_mc)*100:.0f}%   "
          f"|| random lift={mean(agg_rl):.2f}  random co-change={mean(agg_rc)*100:.0f}%")
    if mean(agg_rl) > 0:
        print(f"LIFT RATIO (matched / random) = {mean(agg_ml)/mean(agg_rl):.1f}×")
    print(f"SPECIFICITY FLOOR effect on candidate count: {tot_pairs_nofloor} (no floor) -> {tot_pairs} (floor) "
          f"= dropped {tot_pairs_nofloor-tot_pairs} bare/ubiquitous candidates")


def main(argv: list) -> int:
    if "--panel" in argv:
        rows = clone_and_measure_panel()
        _print_rows(rows)
        return 0
    repos = [a for a in argv if not a.startswith("--")]
    if not repos:
        print("usage: cross_tier_route_probe.py /repo [/repo2 ...] | --panel")
        return 2
    rows = [measure_repo(r) for r in repos]
    _print_rows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
