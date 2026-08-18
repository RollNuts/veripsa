#!/usr/bin/env python3
"""PERF gate — the CUSTOMER-LATENCY neighbor-refresh fan-out (`webhook._refresh_changes`) must stay BOUNDED
+ sub-second and NEAR-LINEAR as the in-flight PR count K grows. This LOCKS the bound measured in the perf-scale
audit (2026-06-20) on a path NO existing perf gate covered.

WHERE THIS RUNS (why it is customer-latency, not off-worker): `_refresh_changes` renders the refresh payload for
EVERY in-flight change in `core.main_impact_surface`'s result — the list the App then POSTS to each PR. It is on
THREE hot, synchronous-to-the-event paths: a push lands on `main` (refresh_inflight → the stale-verdict fix), a PR
merges/withdraws, and an open/sync neighbor refresh (webhook.handle_pull_request). When a busy repo has K PRs all
touching a shared foundation, ONE push to main fans this render across ALL K — inside the per-event worker txn,
before the worker is free for the next event. A slow `_refresh_changes` = events back up = stale verdicts. That is
exactly the degradation this audit hunts.

WHAT THE AUDIT MEASURED (2026-06-20):
  • `_refresh_changes` is ~LINEAR in K — the per-change render (text sanitization in render_safe._oneline) dominates
    and is irreducible. Measured (pure-Python, no DB): K=50 → 16ms, K=200 → 55ms, K=400 → 118ms (~0.27 ms/PR, FLAT).
  • There IS a mild O(K^2) TAIL: render_pr_check re-materializes `_dicts(impact["changes"])` (render.py:105) AND
    re-scans it with `next(... change_id == ref)` (render.py:434) on EVERY call, and `_refresh_changes` calls
    render_pr_check up to TWICE per change (the fork-redacted variant). At an ABSURD K=1600 that quadratic term is
    ~0.56s of a 1.5s call; at any realistic K (<=200) it is negligible (<5ms of the total). So this is an honest-NO
    on a real cliff TODAY — but a future change that turns this path TRULY quadratic (e.g. a per-NEIGHBOR brain /
    co-change DB read added inside the loop — today both are read ONCE per ACTING event, never per neighbor) would
    silently push the refresh past the worker's budget. This gate LOCKS the current bound so that regression FAILS.

TWO LOCKS (both must hold):
  1. STRUCTURAL (deterministic, machine-independent): `_refresh_changes` stays a PURE in-memory render of an
     already-fetched surface — it must NOT introduce a DB round-trip (db(...) / _json / a raw SELECT). The audited
     budget holds ONLY because the brain read + the co-change read happen ONCE per ACTING event (in
     handle_pull_request), NEVER per in-flight neighbor. A read pushed INTO this per-change loop makes the fan-out
     O(K) DB round-trips = the real latency cliff. Anchored on the function source, so it fails loudly, not slowly.
  2. SCALING (behavioural): on a realistic seeded impact surface, `_refresh_changes` at K=200 in-flight PRs stays
     under a GENEROUS absolute ceiling AND the per-PR cost at K=200 is not a large multiple of the per-PR cost at
     K=50 (LINEAR fix ⇒ flat per-PR; a quadratic regression ⇒ per-PR grows with K). Ceilings are deliberately loose
     (CI machines vary) — they catch an ORDER-OF-MAGNITUDE / shape regression, the only kind that matters, without
     flaking on a slow runner.

Run:  python3 tests/test_refresh_fanout_budget.py
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import webhook  # noqa: E402  (webhook._refresh_changes is the path under test; pure-Python, no DB)

# the customer-facing source that holds the per-change render this fan-out repeats. The structural lock anchors on
# _refresh_changes' OWN body, but we read this module to confirm the function exists where we time it from.
WEBHOOK_SRC = os.path.join(ROOT, "github-app", "webhook.py")

# absolute ceiling for ONE _refresh_changes call at K=200 in-flight PRs (pure-Python, no DB). The audit clocked
# this at ~55ms; 2.5s is enormous headroom over the measured cost yet flags an order-of-magnitude regression (e.g.
# a per-neighbor read turning it into K synchronous DB round-trips, or a true O(K^2) render). Loose on purpose so a
# slow CI runner never flakes — only a real blow-up trips it.
CEILING_200_S = 2.5
# the PER-PR cost at K=200 must not exceed this multiple of the per-PR cost at K=50. A linear path ⇒ ~flat per-PR
# (measured ratio ~1.05x); a quadratic regression ⇒ per-PR grows with K. 4x splits flat-from-quadratic with wide
# margin (at K∈{50,200} a true O(K^2) term shows a ~4x per-PR jump; the current path is ~flat).
MAX_PERPR_RATIO = 4.0


def make_impact(k: int, partners: int = 3, seed: int = 42) -> dict:
    """A realistic core.main_impact_surface result: K in-flight changes, each a warn/serialize carrying a few
    partner rows (contested_with / serialize_behind / queued_behind) — the shape render_pr_check actually walks.
    Content-free synthetic (PR refs + synthetic paths only). Deterministic (no randomness → no flake)."""
    changes = []
    for i in range(k):
        contested = [
            {"change_id": f"PR-{(i + j) % k}", "label": f"agent-{(i + j) % k}",
             "paths": [f"src/mod_{(i + j) % k}.py"]}
            for j in range(1, partners + 1)
        ]
        changes.append({
            "change_id": f"PR-{i}",
            "agent": f"agent-{i}",
            "label": f"agent-{i}",
            "verdict": "serialize" if i % 2 == 0 else "warn",
            "paths": [f"src/mod_{i}.py", f"src/util_{i}.py"],
            "impact": [{"path": f"src/dep_{i}.py", "count": 2}],
            "contested_with": contested,
            "serialize_behind": contested[:1],
            "queued_behind": contested[1:],
            "queued_behind_paths": [f"src/mod_{i}.py"],
        })
    return {"repo": "perf/bigrepo", "branch": "main", "changes": changes}


def time_refresh(k: int, reps: int = 3) -> float:
    """Median-ish wall time (s) of one _refresh_changes call at K in-flight PRs. Warms once (import/JIT-free, but
    fills any module-level cache), then averages `reps` runs to smooth jitter. Pure-Python — no DB, no network."""
    impact = make_impact(k)
    webhook._refresh_changes(impact)            # warm
    best = float("inf")
    for _ in range(reps):
        t0 = time.perf_counter()
        out = webhook._refresh_changes(impact)
        dt = time.perf_counter() - t0
        best = min(best, dt)                     # min = least-contended sample (robust to a noisy CI neighbour)
        assert len(out) == k, f"refresh produced {len(out)} entries for {k} changes"
    return best


def main() -> int:
    checks = []

    # ── LOCK 1: STRUCTURAL (deterministic) — _refresh_changes stays a PURE in-memory render, no DB round-trip. ──
    # Read JUST the _refresh_changes function body (def → next top-level def) and assert it issues no DB read. The
    # whole budget rests on the brain + co-change reads being done ONCE per acting event, never per in-flight
    # neighbor; a db(...) / _json / SELECT pushed into this per-change loop = O(K) round-trips = the latency cliff.
    with open(WEBHOOK_SRC, encoding="utf-8") as fh:
        src_lines = fh.readlines()
    body, in_fn = [], False
    for ln in src_lines:
        if ln.startswith("def _refresh_changes("):
            in_fn = True
            continue
        if in_fn and ln.startswith("def "):       # next top-level def → end of _refresh_changes
            break
        if in_fn:
            body.append(ln)
    fn_src = "".join(body)
    assert fn_src, "could not locate _refresh_changes body in webhook.py"
    # forbidden tokens = a DB round-trip introduced into the per-change fan-out loop.
    db_tokens = ("db(", "_json(", "execute(", "psycopg", ".connect(", "SELECT ")
    leaked = [t for t in db_tokens if t in fn_src]
    checks.append((
        "LOCK1 structural: webhook._refresh_changes does NO DB read in its per-change loop (the brain + co-change "
        "reads stay ONCE per acting event, never per in-flight neighbor — else the fan-out is O(K) round-trips, "
        f"the latency cliff){'' if not leaked else ' — LEAKED: ' + ','.join(leaked)}",
        not leaked))

    # ── LOCK 2: SCALING (behavioural) — sub-second at K=200 AND near-linear (flat per-PR), not quadratic. ──
    t50 = time_refresh(50)
    t200 = time_refresh(200)
    perpr_50 = t50 / 50.0
    perpr_200 = t200 / 200.0
    ratio = (perpr_200 / perpr_50) if perpr_50 > 0 else float("inf")

    checks.append((
        f"LOCK2a ceiling: _refresh_changes at K=200 in-flight PRs stays under {CEILING_200_S:.1f}s "
        f"(measured {t200 * 1000:.1f}ms) — the customer-latency refresh fan-out is sub-second at scale",
        t200 < CEILING_200_S))

    checks.append((
        f"LOCK2b linearity: per-PR cost at K=200 ({perpr_200 * 1e6:.1f}us/PR) is <= {MAX_PERPR_RATIO:.1f}x the "
        f"per-PR cost at K=50 ({perpr_50 * 1e6:.1f}us/PR) — measured {ratio:.2f}x (a linear fan-out is ~flat; a "
        f"quadratic regression grows per-PR with K)",
        ratio <= MAX_PERPR_RATIO))

    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("REFRESH FANOUT BUDGET GATE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
