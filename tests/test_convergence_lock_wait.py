#!/usr/bin/env python3
"""Gate: the LIVE per-event path arms a SHORT convergence lock wait.

ROOT of a recurring prod wedge (processed=0 under a multi-agent push storm): the single webhook worker BLOCKS
the full VERIPSA_DB_LOCK_TIMEOUT_MS (30s) inside a contended per-repo / account-lifecycle pg_advisory_lock
before it can defer. Lock keys are disjoint per repo/account, so this is head-of-line TIME monopolization of the
one worker — one hot repo's 30s waits starve every OTHER lane to a standstill. The fix: the two LIVE event flows
arm a SHORT wait (_CONVERGENCE_LOCK_WAIT_MS, default 1s) so a contended convergence lock DEFERS fast and frees the
worker; BACKGROUND lock sites (boot reconcile, co-change pool, install fan-out) keep the long wait unchanged.

Proves BOTH:
  (unit, fake cursor)  the SHORT wait is armed on the live path and the LONG default on background acquisitions.
  (real Postgres)      a genuinely CONTENDED live lock raises 55P03 FAST (bounded by the short wait, not 30s),
                       while an UNCONTENDED acquisition still succeeds — the exact "defer, don't block" behaviour.

Run:  python3 tests/test_convergence_lock_wait.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import psycopg2  # noqa: E402
import server_dbops as SD  # noqa: E402
import event_processor as EP  # noqa: E402

DB = "veripsa_convlock_" + str(os.getpid())
ADMIN = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"


class FakeCur:
    """Records execute() calls; returns nothing (the lock SQL is fire-and-forget here)."""
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((str(sql), params))


def _last_lock_timeout(cur):
    """The last integer ms value the cursor SET lock_timeout to, or None."""
    val = None
    for sql, _ in cur.sql:
        s = sql.strip()
        if s.startswith("SET lock_timeout ="):
            val = int(s.split("=", 1)[1].strip())
    return val


def _all_lock_timeouts(cur):
    return [int(s.split("=", 1)[1].strip())
            for s, _ in cur.sql if s.strip().startswith("SET lock_timeout =")]


def unit_checks(check):
    # 1) _arm_lock_session: explicit short wait vs the global default.
    c = FakeCur(); SD._arm_lock_session(c, 250)
    check("_arm_lock_session(250) arms lock_timeout=250", _last_lock_timeout(c) == 250)
    c = FakeCur(); SD._arm_lock_session(c)
    check("_arm_lock_session() default = VERIPSA_DB_LOCK_TIMEOUT_MS", _last_lock_timeout(c) == SD._LOCK_TIMEOUT_MS)
    check("background default wait stays long (>=30s), unchanged", SD._LOCK_TIMEOUT_MS >= 30_000)

    # 2) both lock helpers thread the optional short wait; default keeps the long wait.
    c = FakeCur(); SD._take_repo_lock(c, "acct-1", "org/app", lock_timeout_ms=250)
    check("_take_repo_lock threads the short wait", _last_lock_timeout(c) == 250)
    c = FakeCur(); SD._take_repo_lock(c, "acct-1", "org/app")
    check("_take_repo_lock default keeps the long wait", _last_lock_timeout(c) == SD._LOCK_TIMEOUT_MS)
    c = FakeCur(); SD._take_repository_id_lock(c, "12345", lock_timeout_ms=250)
    check("_take_repository_id_lock threads the short wait", _last_lock_timeout(c) == 250)

    # 3) the live wrapper arms the SHORT wait on BOTH the repo-id and coordinate acquisitions, NEVER the long default.
    c = FakeCur()
    EP._take_live_repository_locks(c, "12345", "acct-1", "org/app",
                                   take_coordinate=True, delivery_key="k", lock_wait_ms=250)
    tos = _all_lock_timeouts(c)
    check("live path arms the short wait on every acquisition", bool(tos) and all(v == 250 for v in tos))
    check("live path never arms the 30s default wait", SD._LOCK_TIMEOUT_MS not in tos)

    # 4) the knob exists, defaults short, is a fail-loud positive int, and == the long default is its own kill switch.
    check("_CONVERGENCE_LOCK_WAIT_MS defaults short (< 30s default)", EP._CONVERGENCE_LOCK_WAIT_MS < 30_000)
    check("_CONVERGENCE_LOCK_WAIT_MS is a positive int",
          isinstance(EP._CONVERGENCE_LOCK_WAIT_MS, int) and EP._CONVERGENCE_LOCK_WAIT_MS >= 1)

    # 5) MALFORMED ENV IS FAIL-LOUD, never a dangerous silent value. 0 would mean "unlimited" in Postgres — the
    #    exact unbounded-hang this guards against — and a non-int must not fall back to some arbitrary default.
    from env_config import env_int, ConfigError
    for bad in ("0", "-1", "abc", "1e3", ""):
        prev = os.environ.get("VERIPSA_CONVERGENCE_LOCK_WAIT_MS")
        os.environ["VERIPSA_CONVERGENCE_LOCK_WAIT_MS"] = bad
        try:
            env_int("VERIPSA_CONVERGENCE_LOCK_WAIT_MS", 1_000, min_value=1)
            loud = False
        except ConfigError:
            loud = True
        except Exception:
            loud = False
        finally:
            if prev is None:
                os.environ.pop("VERIPSA_CONVERGENCE_LOCK_WAIT_MS", None)
            else:
                os.environ["VERIPSA_CONVERGENCE_LOCK_WAIT_MS"] = prev
        check(f"malformed knob {bad!r} fails LOUD (ConfigError), never a silent unbounded/absurd wait", loud)

    # 6) the kill switch restores the exact previous behaviour: knob == the long default -> live wait IS the long wait.
    c = FakeCur()
    EP._take_live_repository_locks(c, "12345", "acct-1", "org/app",
                                   take_coordinate=True, delivery_key="k",
                                   lock_wait_ms=SD._LOCK_TIMEOUT_MS)
    check("kill switch: knob == VERIPSA_DB_LOCK_TIMEOUT_MS restores the old blocking wait",
          _all_lock_timeouts(c) and all(v == SD._LOCK_TIMEOUT_MS for v in _all_lock_timeouts(c)))


def _admin(sql, args=()):
    conn = psycopg2.connect(ADMIN, dbname=DB)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, args)
    finally:
        conn.close()


def db_checks(check):
    """Real-Postgres proof: a contended live convergence lock defers FAST (bounded by the short wait), an
    uncontended one still succeeds. Requires the veripsa roles + schema (db/bootstrap_local.sh)."""
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        check("db bootstrap (real-lock contention proof)", False)
        print("  bootstrap failed:\n", r.stderr[-800:])
        return
    holder = psycopg2.connect(APP_DSN)
    probe = psycopg2.connect(APP_DSN)
    try:
        holder.autocommit = True
        probe.autocommit = True
        acct, repo = "acct-CONTEND", "org/hot-repo"
        # Holder session takes the SAME per-(account,repo) advisory lock the live path uses.
        with holder.cursor() as hc:
            hc.execute("SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))", SD._repo_lock_args(acct, repo))

        # Probe drives the LIVE acquisition with a short 250ms wait against the held lock: must raise 55P03 FAST.
        raised, elapsed_ms = None, None
        with probe.cursor() as pc:
            t0 = time.monotonic()
            try:
                SD._take_repo_lock(pc, acct, repo, lock_timeout_ms=250)
            except psycopg2.errors.LockNotAvailable as exc:
                raised = exc
            elapsed_ms = (time.monotonic() - t0) * 1000.0
        check("contended live lock raises 55P03 (LockNotAvailable), not a silent block", raised is not None)
        check("contended live lock defers FAST (< 5s, bounded by the ~250ms short wait — NOT the 30s default)",
              elapsed_ms is not None and elapsed_ms < 5_000)
        probe.rollback()  # clear the aborted txn from the failed lock statement

        # Same probe, an UNCONTENDED coordinate: the short wait must NOT block a free lock — it acquires immediately.
        ok_fast = False
        with probe.cursor() as pc:
            t0 = time.monotonic()
            SD._take_repo_lock(pc, "acct-FREE", "org/other", lock_timeout_ms=250)
            ok_fast = (time.monotonic() - t0) * 1000.0 < 5_000
        check("an UNCONTENDED live lock still acquires (the short wait only bites real contention)", ok_fast)
        with probe.cursor() as pc:
            SD._release_repo_lock(pc, "acct-FREE", "org/other")

        # ── CROSS-REPOSITORY STARVATION REGRESSION (the actual incident) ────────────────────────────────
        # The single worker interleaves lanes. When lane A (a hot repo) is chronically contended, the worker
        # pays the lock wait for A on EVERY cycle before it can serve lane B (an UNRELATED repo). With the old
        # 30s wait that per-cycle tax starved every other lane (processed=0 for many minutes). Model one worker
        # cycle — try contended A, then serve free B — and measure how long UNRELATED lane B waits for service.
        # Deterministic (no sleeps): the cost is the lock wait itself. "long" is modelled at 3s to keep the gate
        # fast; production's default is 30s, so the real starvation factor is 10x this measurement.
        LONG_MS, SHORT_MS = 3_000, 250
        hot = ("acct-HOT", "org/hot")
        cold = ("acct-COLD", "org/cold")
        with holder.cursor() as hc:  # a peer session holds the HOT lane's lock for the whole scenario
            hc.execute("SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))", SD._repo_lock_args(*hot))

        def cycle_cost_ms(wait_ms):
            """One worker cycle: contended HOT lane (defers) then UNRELATED COLD lane (must be served).
            Returns ms until the COLD lane's lock was acquired — i.e. how long an unrelated repo waited."""
            t0 = time.monotonic()
            with probe.cursor() as pc:
                try:
                    SD._take_repo_lock(pc, *hot, lock_timeout_ms=wait_ms)
                except psycopg2.errors.LockNotAvailable:
                    pass
            probe.rollback()
            with probe.cursor() as pc:
                SD._take_repo_lock(pc, *cold, lock_timeout_ms=wait_ms)
            ms = (time.monotonic() - t0) * 1000.0
            with probe.cursor() as pc:
                SD._release_repo_lock(pc, *cold)
            return ms

        long_ms = cycle_cost_ms(LONG_MS)
        short_ms = cycle_cost_ms(SHORT_MS)
        check(f"BEFORE-shape: a contended hot lane delays an UNRELATED repo by ~the long wait "
              f"(measured {long_ms:.0f}ms >= {LONG_MS * 0.8:.0f}ms)", long_ms >= LONG_MS * 0.8)
        check(f"AFTER: the SHORT wait keeps the unrelated repo's service time small "
              f"(measured {short_ms:.0f}ms < 1500ms)", short_ms < 1_500)
        check(f"cross-repo starvation regression: short wait serves an unrelated repo >=3x sooner than the "
              f"long wait ({long_ms:.0f}ms -> {short_ms:.0f}ms)", short_ms * 3 <= long_ms)
        with holder.cursor() as hc:
            hc.execute("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))", SD._repo_lock_args(*hot))
    finally:
        holder.close()
        probe.close()
        subprocess.run(["dropdb", DB], capture_output=True, text=True)


def main() -> int:
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    print("== unit: short live wait vs long background wait (fake cursor) ==")
    unit_checks(check)
    print("== real Postgres: contended live lock defers fast, uncontended acquires ==")
    db_checks(check)

    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"CONVERGENCE LOCK WAIT GATE: FAIL ({len(failed)} of {len(results)})")
        for n in failed:
            print("  -", n)
        return 1
    print(f"CONVERGENCE LOCK WAIT GATE: PASS ({len(results)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
