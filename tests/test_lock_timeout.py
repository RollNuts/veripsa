#!/usr/bin/env python3
"""LOCK-TIMEOUT gate — EVERY per-(account,repo) advisory-lock site must arm lock_timeout + statement_timeout
BEFORE the blocking lock, so no site can wedge forever on a contended/severed lock holder.

The audited gap (round-5, two agents converged): only the live per-event processor SET the timeouts before
_take_repo_lock; the OFF-worker lock sites (the co-change populate pool, the boot self-heal reconcile, the
install/uninstall fan-out) opened a fresh connection and took the blocking pg_advisory_lock with NEITHER set —
the exact unbounded-wait class the live path was hardened against (a leaked connection + a stalled feature). The
fix moves the arming INTO _take_repo_lock (server_dbops, the single source of truth for the lock), so every site
is guarded by construction and can never drift back.

Proves on the REAL server_dbops._take_repo_lock (no DB — a recording cursor, deterministic):
  (1) it issues `SET lock_timeout` AND `SET statement_timeout` BEFORE the `pg_advisory_lock` call;
  (2) both are armed to a POSITIVE bound (never 0 = Postgres 'unlimited', the hole this guards).

Run:  python3 tests/test_lock_timeout.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import server_dbops as D  # noqa: E402

checks = []


def chk(c, label):
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    checks.append(bool(c))


class RecCur:
    """A recording cursor: captures the SQL strings _take_repo_lock issues, in order. No DB needed."""
    def __init__(self):
        self.sql = []

    def execute(self, q, args=None):
        self.sql.append(q)


def main() -> int:
    cur = RecCur()
    D._take_repo_lock(cur, "ACCT-GH-42", "org/app")
    sql = cur.sql

    lock_i = next((i for i, q in enumerate(sql) if "SET lock_timeout" in q), None)
    stmt_i = next((i for i, q in enumerate(sql) if "SET statement_timeout" in q), None)
    adv_i = next((i for i, q in enumerate(sql) if "pg_advisory_lock" in q), None)

    chk(lock_i is not None and stmt_i is not None and adv_i is not None,
        f"_take_repo_lock issues lock_timeout + statement_timeout + the advisory lock (got {sql})")
    chk(lock_i is not None and adv_i is not None and lock_i < adv_i and stmt_i < adv_i,
        "both timeouts are armed BEFORE the blocking pg_advisory_lock (so the wait is bounded, never unbounded)")

    def _val(q):
        m = re.search(r"=\s*(\d+)", q)
        return int(m.group(1)) if m else 0
    lock_ms = _val(sql[lock_i]) if lock_i is not None else 0
    stmt_ms = _val(sql[stmt_i]) if stmt_i is not None else 0
    chk(lock_ms > 0 and stmt_ms > 0,
        f"both timeouts are a POSITIVE bound, never 0/unlimited (lock_timeout={lock_ms}ms, statement_timeout={stmt_ms}ms)")

    print("LOCK-TIMEOUT GATE:", "PASS" if all(checks) else "FAIL")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
