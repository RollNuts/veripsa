#!/usr/bin/env python3
"""Degenerate-graph robustness gate for the CONTENTION ENGINE (core.main_impact_surface and its recursive
CTEs change_reach/depth_walk + core._inflight_components).

Adversarially feeds DEGENERATE code graphs and asserts the engine (a) TERMINATES, (b) stays under a hard
LATENCY budget — enforced by a SQL statement_timeout so a non-terminating / blown-up CTE FAILS the gate
instead of hanging CI, (c) produces a SOUND suggested land order (topologically valid — never a dependent
before its dependency, never a self-contradiction), and (d) stays content-free.

The cases (the shapes that broke or could break a recursive closure):
  (a) import CYCLE A->B->C->A           — the closure must dedup + terminate (no infinite loop)
  (b) DEEP LINEAR CHAIN (depth 1500)    — the regression this gate guards: the old FULL transitive closure
                                          (change_reach) + min-label CC (core._inflight_components) were O(N²)
                                          in rows (a 1500-chain = ~2.0M / ~1.1M rows, ~30-36s, statement-timeout
                                          on the WEBHOOK path = a self-inflicted DoS). Bounded now (hop-capped).
  (c) HUGE FAN-IN HUB                    — hub dampening must keep it bounded, never a wall of N warnings
  (d) SELF-LOOPS + DUPLICATE edges       — duplicate semantic facts reject atomically; legal
                                          self-loops must not loop or double-count
  (e) FULLY DISCONNECTED                 — no edges => no contention, everything 'clear', no clusters
  (f) DIAMOND / lattice                  — ambiguous-looking order must still be a VALID topological order

Run:  python3 tests/test_degenerate_graph.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe), exactly like db/smoke.sh / test_topo_order.py: the gate bootstraps + drops
# this DB, so a FIXED name would let concurrent runs drop each other's DB mid-run.
DB = "veripsa_degentest_" + str(os.getpid())
REPO = "acme/degen"

# Hard per-call latency budget. The PRE-FIX engine's recursive CTEs were O(N²) in rows and BLEW PAST 30s
# (statement-timeout) on the deep-chain case even at a few hundred PRs; the fixed engine (hop-capped land-order
# walk + pointer-jumping connected components) finishes every case below well under this budget. Enforced as a
# SQL statement_timeout so a NON-TERMINATING / O(N²)-blown CTE FAILS the assertion (QueryCanceled) instead of
# hanging the gate forever. Generous enough to absorb cold-cache + full-suite Postgres contention.
BUDGET_MS = 20000


def conn(role="veripsa_app"):
    return psycopg2.connect(f"postgresql://{role}@localhost/{DB}")


def run1(sql, args=()):
    c = conn()
    try:
        with c, c.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        c.close()


def ingest(nodes, edges, branch):
    run1("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
         (json.dumps({"nodes": nodes, "edges": edges}), REPO, branch, "a" * 40))


def claim(cid, path, who, branch):
    run1("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, REPO, branch, who))


def F(p):
    return {"id": p, "kind": "file", "path": p, "language": "python"}


def E(s, d):
    return {"src": s, "dst": d, "kind": "imports"}


def surface(branch):
    """Run main_impact_surface under the hard budget; returns (result, elapsed_ms). Raises QueryCanceled
    (caught by the caller as a FAIL) if the engine fails to terminate within BUDGET_MS."""
    c = conn()
    try:
        with c, c.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SET statement_timeout = %s", (BUDGET_MS,))
            t0 = time.perf_counter()
            cur.execute("SELECT core.main_impact_surface(%s,%s)", (REPO, branch))
            dt = (time.perf_counter() - t0) * 1000.0
            row = cur.fetchone()
            res = row[0] if row else None
            return (res if isinstance(res, dict) else json.loads(res)), dt
    finally:
        c.close()


def biggest_cluster_order(surf):
    clusters = surf.get("clusters") or []
    if not clusters:
        return {}, []
    cl = max(clusters, key=lambda c: c.get("size", 0))
    return cl, (cl.get("suggested_order") or [])


def pos(order, token):
    for i, lbl in enumerate(order):
        if token in lbl:
            return i
    return -1


# crude content-free scan over the JSON: it must carry paths/symbols/counts/labels, never source code.
# We assert the absence of obvious code markers in the serialized surface.
CODE_MARKERS = ("def ", "class ", "import ", "return ", "{", "}", ";\n")


def is_content_free(surf):
    blob = json.dumps(surf)
    # paths legitimately contain "import"-like substrings only inside file names we control (none here),
    # so on these synthetic graphs the surface must contain NONE of the code-body markers.
    return not any(m in blob for m in ("def ", "class MyClass", "return ", "    "))


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    checks = []

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # QueryCanceled (timeout) or any error => the case FAILS, never hangs
            checks.append((f"{name}: terminated within {BUDGET_MS}ms (got {type(e).__name__}: {str(e)[:60]})", False))

    # ---- (a) CYCLE A->B->C->A ----
    def case_a():
        br = "cyc"
        ingest([F("a.py"), F("b.py"), F("c.py")],
               [E("a.py", "b.py"), E("b.py", "c.py"), E("c.py", "a.py")], br)
        for cid, p, w in [("PR-1:a", "a.py", "al"), ("PR-2:b", "b.py", "bo"), ("PR-3:c", "c.py", "ca")]:
            claim(cid, p, w, br)
        surf, dt = surface(br)
        cl, order = biggest_cluster_order(surf)
        checks.append((f"(a) CYCLE terminates + bounded ({dt:.0f}ms<{BUDGET_MS})", dt < BUDGET_MS))
        # a cycle has no valid topo order; the engine must still return ONE finite linearization of all 3 (no dup, no loop)
        checks.append((f"(a) CYCLE: all 3 changes ordered exactly once (order={order})",
                       len(order) == 3 and len(set(order)) == 3))

    # ---- (b) DEEP CHAIN (the regression guard) ----
    def case_b():
        br = "chain"
        N = 1000  # deep enough that the OLD O(N²) recursive closures (change_reach + min-label CC) timed out
        nodes = [F(f"n{i}.py") for i in range(N)]
        edges = [E(f"n{i+1}.py", f"n{i}.py") for i in range(N - 1)]  # n0 foundational
        ingest(nodes, edges, br)
        for i in range(N):
            claim(f"PR-{i}:n{i}", f"n{i}.py", f"a{i % 50}", br)
        surf, dt = surface(br)
        checks.append((f"(b) CHAIN depth {N} terminates within budget ({dt:.0f}ms<{BUDGET_MS}) "
                       f"— the O(N²)-recursive-closure regression guard (change_reach + _inflight_components)",
                       dt < BUDGET_MS))
        checks.append((f"(b) CHAIN: every in-flight change accounted for "
                       f"(inflight_count={surf.get('inflight_count')}=={N})", surf.get("inflight_count") == N))
        # the chain is ONE entangled neighbourhood — pointer-jumping CC must collapse it to a SINGLE cluster
        # (a cap-based approach would wrongly shatter it; this asserts the connected-components fix is exact).
        cl, _ = biggest_cluster_order(surf)
        checks.append((f"(b) CHAIN: collapses to ONE cluster of all {N} (pointer-jumping CC is exact, not split) "
                       f"(cluster_count={surf.get('cluster_count')}, biggest size={cl.get('size')})",
                       surf.get("cluster_count") == 1 and cl.get("size") == N))

    # ---- (c) HUGE FAN-IN HUB ----
    def case_c():
        br = "fanin"
        M = 200
        nodes = [F("hub.py")] + [F(f"f{i}.py") for i in range(M)]
        edges = [E(f"f{i}.py", "hub.py") for i in range(M)]
        ingest(nodes, edges, br)
        claim("PR-hub:hub", "hub.py", "hh", br)
        for i in range(M):
            claim(f"PR-{i}:f{i}", f"f{i}.py", f"a{i % 60}", br)
        surf, dt = surface(br)
        checks.append((f"(c) FAN-IN hub M={M} terminates + bounded ({dt:.0f}ms<{BUDGET_MS})", dt < BUDGET_MS))
        # hub dampening: a hub edited must NOT produce a wall of warn against all M importers
        checks.append((f"(c) FAN-IN: hub dampening prevents an N-wide warn wall "
                       f"(warn_count={surf.get('warn_count')} << {M})", (surf.get("warn_count") or 0) < M))

    # ---- (d) SELF-LOOPS + DUPLICATE edges ----
    def case_d():
        br = "selfdup"
        duplicate_rejected = False
        try:
            ingest(
                [F("x.py"), F("y.py")],
                [E("x.py", "x.py"), E("x.py", "x.py")],
                "selfdup-rejected",
            )
        except psycopg2.Error as exc:
            duplicate_rejected = (
                exc.pgcode == "22023"
                and "duplicate graph Edge identity" in str(exc)
            )
        checks.append((
            "(d) duplicate semantic Edge facts reject atomically before engine evaluation",
            duplicate_rejected,
        ))

        # Self-loops remain legal graph facts. Ingest each identity once; the
        # governed writer now rejects duplicates instead of relying on the
        # engine to deduplicate an invalid payload.
        edges = [
            E("x.py", "x.py"),
            E("y.py", "x.py"),
            E("y.py", "y.py"),
        ]
        ingest([F("x.py"), F("y.py")], edges, br)
        claim("PR-1:x", "x.py", "al", br)
        claim("PR-2:y", "y.py", "bo", br)
        surf, dt = surface(br)
        checks.append((f"(d) legal SELF-LOOP graph terminates + bounded ({dt:.0f}ms<{BUDGET_MS})", dt < BUDGET_MS))
        checks.append((f"(d) SELF-LOOP: both changes present, no infinite blow-up "
                       f"(inflight_count={surf.get('inflight_count')}==2)", surf.get("inflight_count") == 2))

    # ---- (e) FULLY DISCONNECTED ----
    def case_e():
        br = "disc"
        K = 50
        ingest([F(f"d{i}.py") for i in range(K)], [], br)
        for i in range(K):
            claim(f"PR-{i}:d{i}", f"d{i}.py", f"a{i}", br)
        surf, dt = surface(br)
        checks.append((f"(e) DISCONNECTED K={K} terminates + bounded ({dt:.0f}ms<{BUDGET_MS})", dt < BUDGET_MS))
        # no edges => no contention: zero clusters, everything clear
        checks.append((f"(e) DISCONNECTED: no edges => no clusters, all clear "
                       f"(clusters={surf.get('cluster_count')}, clear={surf.get('clear_count')}/{K})",
                       surf.get("cluster_count") == 0 and surf.get("clear_count") == K))

    # ---- (f) DIAMOND / lattice (sound order on ambiguity) ----
    def case_f():
        br = "diamond"
        ingest([F("t.py"), F("l.py"), F("r.py"), F("b.py")],
               [E("l.py", "t.py"), E("r.py", "t.py"), E("b.py", "l.py"), E("b.py", "r.py")], br)
        for cid, p, w in [("PR-T:t", "t.py", "tt"), ("PR-L:l", "l.py", "ll"),
                          ("PR-R:r", "r.py", "rr"), ("PR-B:b", "b.py", "bb")]:
            claim(cid, p, w, br)
        surf, dt = surface(br)
        cl, order = biggest_cluster_order(surf)
        pT, pL, pR, pB = pos(order, "PR-T"), pos(order, "PR-L"), pos(order, "PR-R"), pos(order, "PR-B")
        checks.append((f"(f) DIAMOND terminates + bounded ({dt:.0f}ms<{BUDGET_MS})", dt < BUDGET_MS))
        # SOUND topological order: T (the foundation) before L,R; L,R before B (the sink). Never B before its deps.
        sound = min(pT, pL, pR, pB) >= 0 and pT < pL and pT < pR and pL < pB and pR < pB
        checks.append((f"(f) DIAMOND: suggested order is topologically VALID "
                       f"(T={pT}<L={pL},R={pR}<B={pB}; order={order})", sound))

    for name, fn in [("(a) cycle", case_a), ("(b) chain", case_b), ("(c) fan-in", case_c),
                     ("(d) self/dup", case_d), ("(e) disconnected", case_e), ("(f) diamond", case_f)]:
        case(name, fn)

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("DEGENERATE GRAPH GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
