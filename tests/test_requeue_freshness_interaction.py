#!/usr/bin/env python3
"""INTERACTION gate — lease RE-QUEUE × freshness-gated demotion × low-value soften compose without a silent miss.

A WAVE of independently-correct fixes landed on `main` from concurrent sessions, several touching the SAME hot
paths (the gate's claim lifecycle + the contention engine). Each was proven INDIVIDUALLY; the danger is a pair /
triple that COMPOSES into a regression. The #127-soften × #122-freshness seam is already locked by
test_soften_freshness_interaction.py. This gate locks the OTHER live seam in that family: the claim-LIFECYCLE
transitions (lease lapse → auto-promote, self-heartbeat resync, reopen) that move a claim between
holder/waiter/expired — driven by THESE fixes — must not let the freshness/soften engine drop a real collision:

  - LEASE RE-QUEUE / self-heartbeat (the self-eviction fix): a holder whose lease JUST lapsed but re-synchronizes
    (the resync that PROVES it is alive) renews its OWN lane FIRST, so the stale-sweep that follows never evicts
    it onto the back of the very queue it leads. AND a holder that does NOT resync (crashed) is swept and its
    OLDEST waiter auto-promoted to active (the highway clears).
  - FRESHNESS-GATED demotion (#122): a same-symbol collision is demoted to 'disjoint' (dropped) ONLY when the
    file's graph content-hash == BOTH sides' base hash (spans provably aligned to the diff lines). A STALE graph
    keeps the file-level 'serialize' (recall held) and drops the confident symbol NAME (no wrong-symbol claim).
  - LOW-VALUE soften (#127): a surviving collision only on append-mostly runner files softens to 'serialize_soft'
    — UNLESS it is a proven 'same'-symbol collision (then it stays HARD regardless of the file's name/glob).

THE COMPOSITION RISK this gate rules out (recall + precision + content-free), against the REAL gate + engine:

  (1) AUTO-PROMOTE preserves the freshness keys: when a crashed holder is swept and its waiter is promoted to the
      active lane, the promoted claim's touched_ranges + base_content_hash SURVIVE the state UPDATE — so a later
      collision on that lane is still evaluated at SYMBOL granularity with a valid freshness proof, not silently
      coarsened. (A promote that nulled the keys would degrade every post-promote verdict.)
  (2) SELF-HEARTBEAT RESYNC keeps the collision: a holder re-syncing after its lease lapsed keeps its lane (no
      self-eviction) and the waiter behind it STILL serializes — the re-queue fix does not silently free the lane.
  (3) STALE RESYNC holds recall + drops the symbol name: when a holder re-syncs with a base hash that no longer
      matches the graph (the file moved under it; graph not re-ingested), the collision is STILL KEPT 'serialize'
      (no silent miss) and the confident symbol name correctly drops to NULL (freshness unprovable) — the engine
      falls back to the honest line-range locus. The #122 guarantee survives the #182 re-queue.
  (4) SOFTEN GUARD IS ROLE-SYMMETRIC across an auto-promote: a proven same-symbol collision on an owner-globbed
      low-value (*.py) file stays HARD 'serialize' EVEN AFTER the holder/waiter roles SWAP via auto-promote +
      reopen — the symovl symmetric (waiter↔holder) match keeps the soften-guard firing in either orientation,
      so the #127×#122 silent-miss cannot be reintroduced by merely reordering the lane.
  (5) CONTENT-FREE throughout: no diff body / secret reaches the engine surface across every transition.

Run:  python3 tests/test_requeue_freshness_interaction.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a per-PID name lets concurrent runs /
# sibling agents not drop each other's DB mid-run (matches db/smoke.sh, run_gates, test_soften_freshness).
DB = "veripsa_requeuefresh_" + str(os.getpid())
REPO = "acme/requeuefresh"
ACCT = "ACCT-DEMO"

# A SECRET-LOOKING token that lives ONLY in a (hypothetical) diff body. If it ever reaches the engine surface the
# content-free contract is broken — the pipeline must derive its signal from line ranges + symbol spans, never bytes.
SECRET = "SUPER_SECRET_REQUEUE_91pdq"

GRAPH_HASH = "b0" * 20   # the hash the graph was INGESTED at (the symbol spans are valid for THIS version)
STALE_BASE = "c1" * 20   # a base hash that DIFFERS from the graph hash → freshness unverifiable (the stale case)


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    APP = f"postgresql://veripsa_app@localhost/{DB}"
    MIG = f"postgresql://veripsa_migrator@localhost/{DB}"

    def app(sql, args=()):
        conn = psycopg2.connect(APP)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    def mig(sql, args=()):
        # migrator connection with the account pinned + a governed-write token, for the lease-lapse fixtures only
        # (we never bypass the gate for the ASSERTIONS — those go through the real app surface).
        conn = psycopg2.connect(MIG)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.current_account',%s,true)", (ACCT,))
                cur.execute("SELECT set_config('core.governed_write_token','claim',true)")
                cur.execute(sql, args)
                try:
                    row = cur.fetchone()
                    return row[0] if row else None
                except psycopg2.ProgrammingError:
                    return None
        finally:
            conn.close()

    def ingest(graph):
        app("SELECT core.ingest_graph_with_authority(%s::jsonb,%s,%s,%s)",
            (json.dumps(graph), REPO, "main", "a" * 40))

    def claim(cid, path, author, ranges, base_hash):
        rj = json.dumps(ranges) if ranges else None
        app("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s)",
            (cid, path, REPO, "main", author, rj, base_hash))

    def set_policy(key, value):
        app("SELECT core.set_policy_with_authority(%s,%s)", (key, value))

    def reset_claims():
        mig("UPDATE core.claim SET claim_state='released', released_at=now() WHERE claim_state IN ('active','waiting')")

    def lapse(change_id):
        # push a change's lease into the past → it reads as a crashed/silent holder to the next expire sweep.
        mig("UPDATE core.claim SET lease_expires_at = now() - interval '1 hour' WHERE change_id=%s", (change_id,))

    def claim_states(path):
        return mig("SELECT COALESCE(string_agg(change_id||'/'||claim_state, ',' ORDER BY change_id),'') "
                   "FROM core.claim WHERE target_path=%s AND claim_state IN ('active','waiting')", (path,))

    def keys(change_id, path):
        # the freshness keys ON the claim (ranges present? base hash) — to prove they SURVIVE a state transition.
        return mig("SELECT (touched_ranges IS NOT NULL)::text||'|'||COALESCE(base_content_hash,'<null>') "
                   "FROM core.claim WHERE change_id=%s AND target_path=%s "
                   "AND claim_state IN ('active','waiting') LIMIT 1", (change_id, path))

    def impact():
        imp = app("SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
        if isinstance(imp, str):
            imp = json.loads(imp)
        return {c["change_id"]: c for c in imp.get("changes", [])}, imp

    checks = []

    # The graph: pl.py (a REAL python source: alpha 1-3, beta 7-9), all at GRAPH_HASH. pl.py is NOT a built-in
    # low-value file — the owner will glob it low-value (*.py) to exercise the soften × role-swap seam.
    graph = {"nodes": [
        {"id": "pl.py", "kind": "file", "path": "pl.py", "language": "python", "content_hash": GRAPH_HASH},
        {"id": "pl.py::alpha", "kind": "def", "name": "alpha", "path": "pl.py", "language": "python",
         "start_line": 1, "end_line": 3, "content_hash": GRAPH_HASH},
        {"id": "pl.py::beta", "kind": "def", "name": "beta", "path": "pl.py", "language": "python",
         "start_line": 7, "end_line": 9, "content_hash": GRAPH_HASH},
        {"id": "run_gates.sh", "kind": "file", "path": "run_gates.sh", "language": "shell", "content_hash": "cc" * 20},
    ]}
    ingest(graph)

    # ── (1) AUTO-PROMOTE preserves the freshness keys. PR-A holds alpha (fresh), PR-B waits on alpha (fresh) =
    #    a real same-symbol KEEP. Crash PR-A (lapse, no resync) → the next surface call RE-QUEUES PR-A to 'waiting'
    #    (lease lapse ≠ PR closed — #182: a still-open holder's serialize is never silently cleared) and auto-
    #    promotes PR-B to active. PR-B's ranges + base hash must SURVIVE the promote so a future verdict stays fine. ─
    reset_claims()
    claim("PR-A:pl.py", "pl.py", "alice", [[1, 3]], GRAPH_HASH)
    claim("PR-B:pl.py", "pl.py", "bob", [[1, 3]], GRAPH_HASH)
    lapse("PR-A")
    _ch, _imp = impact()   # this call runs expire_stale_claims() → re-queues PR-A to waiting, promotes PR-B
    st = claim_states("pl.py")
    promoted_keys = keys("PR-B", "pl.py")
    checks.append((f"(1) a crashed STILL-OPEN holder is RE-QUEUED to waiting behind its now-promoted waiter "
                   f"(#182: its serialize is never silently cleared) (states='{st}')",
                   st == "PR-A/waiting,PR-B/active"))
    checks.append((f"(1) the auto-promoted claim KEEPS its freshness keys across the state UPDATE "
                   f"(ranges_present|base_hash='{promoted_keys}')",
                   promoted_keys == f"true|{GRAPH_HASH}"))

    # ── (2) SELF-HEARTBEAT RESYNC keeps the collision (the self-eviction fix): PR-X holds alpha, PR-Y waits on
    #    alpha (both fresh). Lapse PR-X, then PR-X RE-SYNCS — its self-heartbeat renews BEFORE the sweep, so it
    #    keeps its lane and PR-Y STILL serializes behind it (the lane is NOT silently freed). ──────────────────────
    reset_claims()
    claim("PR-X:pl.py", "pl.py", "xena", [[1, 3]], GRAPH_HASH)
    claim("PR-Y:pl.py", "pl.py", "yuri", [[1, 3]], GRAPH_HASH)
    lapse("PR-X")
    claim("PR-X:pl.py", "pl.py", "xena", [[1, 3]], GRAPH_HASH)   # resync (the alive proof)
    st = claim_states("pl.py")
    ch, imp = impact()
    prY = ch.get("PR-Y", {})
    checks.append((f"(2) a holder that resyncs after its lease lapsed KEEPS its lane (no self-eviction); the "
                   f"waiter still serializes behind it (states='{st}', PR-Y verdict='{prY.get('verdict')}')",
                   st == "PR-X/active,PR-Y/waiting" and prY.get("verdict") == "serialize"))

    # ── (3) STALE RESYNC holds recall + drops the symbol NAME. Re-establish PR-X holder / PR-Y waiter on alpha,
    #    then PR-X resyncs with a base hash that NO LONGER matches the graph (the file moved under it; graph not
    #    re-ingested). The collision must STAY 'serialize' (recall) but the confident symbol name drops to NULL
    #    (freshness unprovable) → the locus falls back to the honest line range. The #122 guarantee survives #182. ─
    reset_claims()
    claim("PR-X:pl.py", "pl.py", "xena", [[1, 3]], GRAPH_HASH)
    claim("PR-Y:pl.py", "pl.py", "yuri", [[1, 3]], GRAPH_HASH)
    lapse("PR-X")
    claim("PR-X:pl.py", "pl.py", "xena", [[1, 3]], STALE_BASE)   # resync with a base hash != the graph hash
    ch, imp = impact()
    prY = ch.get("PR-Y", {})
    cps = prY.get("collision_points", [])
    cp = cps[0] if cps else {}
    checks.append((f"(3) a STALE resync keeps the collision HARD (recall held) — PR-Y is still 'serialize' "
                   f"(verdict='{prY.get('verdict')}', serialize_count={imp.get('serialize_count')})",
                   prY.get("verdict") == "serialize" and imp.get("serialize_count") == 1))
    checks.append((f"(3) the confident symbol NAME drops to NULL under the stale resync (no wrong-symbol claim); "
                   f"the locus falls back to the line range (symbol={cp.get('symbol')!r}, "
                   f"line_lo={cp.get('line_lo')}, line_hi={cp.get('line_hi')})",
                   cp.get("symbol") is None and cp.get("line_lo") == 1 and cp.get("line_hi") == 3))

    # ── (4) SOFTEN GUARD IS ROLE-SYMMETRIC across an auto-promote. Glob pl.py low-value (*.py). PR-M holds alpha,
    #    PR-N waits on alpha (both fresh) = a proven same-symbol collision → must be HARD 'serialize' (the guard
    #    refuses to soften a proven same-symbol pair). Crash PR-M, auto-promote PR-N to active, then PR-M reopens
    #    as the waiter — the holder/waiter roles have SWAPPED. The verdict must STILL be HARD 'serialize'. ─────────
    set_policy("low_value_collision_globs", "*.py")
    reset_claims()
    claim("PR-M:pl.py", "pl.py", "mary", [[1, 3]], GRAPH_HASH)
    claim("PR-N:pl.py", "pl.py", "nora", [[1, 3]], GRAPH_HASH)
    _ch, imp0 = impact()
    checks.append((f"(4) low_value glob is set, yet a proven same-symbol collision is HARD 'serialize', NOT "
                   f"softened (serialize={imp0.get('serialize_count')}, soft={imp0.get('serialize_soft_count')})",
                   imp0.get("serialize_count") == 1 and imp0.get("serialize_soft_count") == 0))
    lapse("PR-M")
    impact()                                            # sweep PR-M, promote PR-N to active
    claim("PR-M:pl.py", "pl.py", "mary", [[1, 3]], GRAPH_HASH)   # PR-M reopens — now the WAITER (roles swapped)
    st = claim_states("pl.py")
    ch, imp = impact()
    checks.append((f"(4) after the holder/waiter roles SWAP via auto-promote, the proven same-symbol collision "
                   f"on the globbed low-value file STILL stays HARD 'serialize' (states='{st}', "
                   f"serialize={imp.get('serialize_count')}, soft={imp.get('serialize_soft_count')})",
                   st in ("PR-M/waiting,PR-N/active",)
                   and imp.get("serialize_count") == 1 and imp.get("serialize_soft_count") == 0))

    # ── (5) CONTENT-FREE: the secret token never reaches the engine surface across all the transitions above. ────
    surface_blob = json.dumps(imp)
    checks.append(("(5) CONTENT-FREE: the secret token never appears in the engine surface output across the "
                   "re-queue / resync / role-swap transitions", SECRET not in surface_blob))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("RE-QUEUE×FRESHNESS INTERACTION GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
