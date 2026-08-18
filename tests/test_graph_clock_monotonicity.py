#!/usr/bin/env python3
"""Gate: a payload-less graph write must never ERASE the coordinate's delivery-order clock.

THE PRODUCTION DEFECT THIS PINS (measured 2026-07-25, example-org/geometry-app@main):

    05:01 stale 6aa4370 -> re-ingested @HEAD dc9d9d1 (mode=full files=244 edges=21873)
    05:31 stale 280cdb1 -> re-ingested @HEAD dc9d9d1 (mode=full files=244 edges=21873)
    06:10 stale 1bce8f3 -> re-ingested @HEAD dc9d9d1 (mode=full files=244 edges=21873)
    06:23 stale 1bce8f3 -> re-ingested @HEAD dc9d9d1 (mode=full files=244 edges=21873)

HEAD never moved, yet `stored` was never HEAD on the next event — the SAME whole-repo ingest was paid four
times in 90 minutes. The loop:

  1. `self_heal_main_graph` re-ingests with `payload=None`, so `p_captured_at` is NULL.
  2. The writer's ON CONFLICT arm set `captured_at = EXCLUDED.captured_at` unconditionally -> the stored clock
     became NULL.
  3. The reordered-delivery guard only fires when BOTH the incoming and the stored `captured_at` are non-NULL,
     so with a NULL stored clock it was DISARMED.
  4. A backlogged OLDER push then overwrote the freshly-healed HEAD (graph REGRESSION).
  5. The next event read `behind` again -> another full re-ingest. Closed loop; the queue never drains.

This is throughput-critical, not cosmetic: it is why one worker could not sustain the product's own target
workload (parallel agents pushing to repos).

Two independent guarantees, both proven against a REAL Postgres with the real SECURITY DEFINER writers:
  A. CLOCK PRESERVED — a payload-less write keeps the stored clock instead of nulling it.
  B. GUARD STAYS ARMED — after such a write, an OLDER push is still REFUSED as a reordered delivery.

Run:  python3 tests/test_graph_clock_monotonicity.py
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

DB = "veripsa_graphclock_" + str(os.getpid())
ADMIN = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"

ACCT = "ACCT-GH-9001"
REPO = "org/clockrepo"
BRANCH = "main"
GRAPH = json.dumps({"nodes": [{"path": "a.py", "symbol": "a", "kind": "file"}], "edges": []})

OLD_SHA, OLD_AT = "1bce8f3aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "2026-07-25T05:00:00+00:00"
NEW_SHA, NEW_AT = "dc9d9d1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "2026-07-25T06:00:00+00:00"


def _app(sql, args=()):
    """Run as the least-privilege app role, with the tenant pinned exactly like the live path."""
    conn = psycopg2.connect(APP_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, false)", (ACCT,))
            cur.execute("SELECT set_config('core.installation_account', %s, false)", (ACCT,))
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _ingest(sha, captured_at):
    """The REAL writer the live path calls (SECURITY DEFINER, tenant-pinned)."""
    return _app("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s,%s)",
                (GRAPH, REPO, BRANCH, sha, captured_at))


def _stored():
    conn = psycopg2.connect(ADMIN, dbname=DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT commit_sha, captured_at FROM core.graph_version "
                        "WHERE repo=%s AND branch=%s", (REPO, BRANCH))
            r = cur.fetchone()
            return (r[0], r[1]) if r else (None, None)
    finally:
        conn.close()


def main() -> int:
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        print("GRAPH CLOCK MONOTONICITY GATE: FAIL (bootstrap)")
        return 1
    try:
        # A real push lands HEAD with its commit time — the coordinate now has a delivery-order clock.
        _ingest(NEW_SHA, NEW_AT)
        sha, cap = _stored()
        check("baseline: a push with a payload stores both the sha and the clock", sha == NEW_SHA and cap is not None)

        # (A) A PAYLOAD-LESS write (the self-heal shape) must NOT erase that clock.
        _ingest(NEW_SHA, None)
        sha, cap_after = _stored()
        check("payload-less re-ingest keeps the sha", sha == NEW_SHA)
        check("payload-less re-ingest does NOT erase the delivery-order clock (the regression-loop root)",
              cap_after is not None)

        # (B) With the clock preserved, the reordered-delivery guard is still ARMED: a backlogged OLDER push
        #     must be refused, so the freshly-healed HEAD cannot regress.
        out = _ingest(OLD_SHA, OLD_AT)
        parsed = json.loads(out) if isinstance(out, str) else (out or {})
        sha_final, _ = _stored()
        check("an OLDER backlogged push is REFUSED as a reordered delivery",
              bool(parsed.get("stale")) is True)
        check("the healed HEAD did NOT regress to the older sha (graph monotonicity holds)",
              sha_final == NEW_SHA)

        # A genuinely NEWER push must still land — the guard must not freeze the coordinate.
        newer_sha, newer_at = "eeee111" + "0" * 33, "2026-07-25T07:00:00+00:00"
        _ingest(newer_sha, newer_at)
        sha_newer, _ = _stored()
        check("a genuinely NEWER push still lands (the guard refuses only reordered/older deliveries)",
              sha_newer == newer_sha)

        # Same-sha refresh at the same coordinate stays allowed (re-ingest for a version restamp).
        _ingest(newer_sha, None)
        sha_same, cap_same = _stored()
        check("a same-sha payload-less refresh is still allowed and still keeps the clock",
              sha_same == newer_sha and cap_same is not None)
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)

    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"GRAPH CLOCK MONOTONICITY GATE: FAIL ({len(failed)} of {len(results)})")
        for n in failed:
            print("  -", n)
        return 1
    print(f"GRAPH CLOCK MONOTONICITY GATE: PASS ({len(results)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
