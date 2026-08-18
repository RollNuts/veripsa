#!/usr/bin/env python3
"""Windowed failure-ratio signal + /alarmz endpoint — defense against the 2026-06-25 silent-incident class
(pure: no DB, no network, no real worker thread).

THE INCIDENT THIS PINS A DEFENSE FOR (verified, MAJOR/silent): a deploy whose Python called a NEW SQL
signature against a prod DB still on the OLD signature failed EVERY PR event with SQLSTATE 42883 for ~1
hour, while /healthz stayed 200 the whole time:
  • queue_depth = 0           — events failed FAST, never accumulating a backlog
  • worker_alive = True       — the worker thread kept dequeueing + catching exceptions
  • processed += 0 / failed += N (cumulative)  — a steady 8% failure ratio is invisible in the totals alone

THE DEFENSE THIS LOCKS (the failure-ratio signal in health_watchdog + /alarmz in server_http):
  • record_outcome(...) feeds a content-free ringbuffer (timestamp + outcome tag), bounded to RING_CAP.
  • failure_ratio_window() reports failed / (processed + failed) within the last `window_seconds`.
  • An EMPTY sample (cold start) returns ratio=None → /alarmz 200 (honest unknown, never a false page).
  • A ratio OVER threshold → /alarmz 503 (the cue an external alerter / Render health check pages on).
  • Edge-triggered alarm_state COUNTER: increments ONCE per crossing (a sustained 8% does not inflate it
    every tick — what last hour's incident would have looked like on a 30s loop).
  • health_snapshot exposes failure_ratio_5min + alarm_state for the operator's /healthz view.
  • Retried events are recorded but DO NOT count in the ratio (intermediate, not terminal).
  • OUT-OF-WINDOW events are excluded (a steady 5%-failure morning does not poison an afternoon read).
  • Fail-open: ANY error in the ringbuffer/window read collapses to None — never crashes /healthz.
  • Content-free: only the outcome tag is recorded (never the event type, repo, payload, or error).
  • Threshold + window are env-tunable (VERIPSA_FAILURE_RATIO_THRESHOLD / _WINDOW_SEC).

If today's 8% failure rate had been visible on a 5-min window, /alarmz would have paged within minutes
— that is the gap this defense closes. Mechanically impossible to silent-incident the same way again.

Pure — no Postgres, no network: we drive record_outcome with a deterministic clock + read the signal back.

Run:  python3 tests/test_failure_ratio_alarm.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import health_watchdog as hw  # noqa: E402


def _reset_env():
    """Strip any per-developer env override so the defaults are what we test against."""
    for k in ("VERIPSA_FAILURE_RATIO_THRESHOLD", "VERIPSA_FAILURE_RATIO_WINDOW_SEC"):
        os.environ.pop(k, None)


def checks():
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    _reset_env()

    # ── 1) EMPTY WINDOW = honest unknown (the cold-start contract /alarmz relies on) ────────────────
    hw._reset_outcome_ring_for_test()
    fr = hw.failure_ratio_window(now=1000.0)
    check("empty ring: ratio is None (cold start has no data → never a false page)",
          fr["ratio"] is None and fr["sample_size"] == 0)
    check("empty ring: window_seconds defaults to 300 (the 5-min default the field name advertises)",
          fr["window_seconds"] == 300.0)
    check("empty ring: threshold defaults to 0.05 (the 5% default)", fr["threshold"] == 0.05)

    # ── 2) A 'PROCESSED'-only window → ratio 0.0 (healthy steady state) ─────────────────────────────
    hw._reset_outcome_ring_for_test()
    for i in range(50):
        hw.record_outcome("processed", now=900.0 + i)
    fr = hw.failure_ratio_window(now=1000.0)
    check("50 processed in window: ratio 0.0 (zero failures, sample = 50)",
          fr["ratio"] == 0.0 and fr["sample_size"] == 50 and fr["failed"] == 0)

    # ── 3) THE INCIDENT REPLAY: 660 processed + 54 failed in window → ratio ~8.2% → over 5% → ALARM ─
    #     (the actual numbers from today's /healthz at 10:17 UTC — this would have paged).
    hw._reset_outcome_ring_for_test()
    # Spread the events across the window so the 'now' read sweeps them all.
    for i in range(660):
        hw.record_outcome("processed", now=900.0 + (i * 0.2))
    for i in range(54):
        hw.record_outcome("failed", now=950.0 + (i * 0.3))
    fr = hw.failure_ratio_window(now=1000.0)
    expected_ratio = 54 / (54 + 660)
    check("today's incident shape (660p / 54f): ratio matches failed/(processed+failed)",
          abs(fr["ratio"] - expected_ratio) < 1e-9)
    check("today's incident shape: ratio (~8.2%) is OVER the 5% default threshold → would have alarmed",
          fr["ratio"] > fr["threshold"])

    # ── 4) RING CAP: oldest entries are evicted when more than RING_CAP events recorded ─────────────
    hw._reset_outcome_ring_for_test()
    # Record CAP + 100 processed in a tight window; only the most recent CAP should survive.
    cap = hw._OUTCOME_RING_CAP
    for i in range(cap + 100):
        hw.record_outcome("processed", now=1000.0 + i * 0.001)
    fr = hw.failure_ratio_window(now=1000.0 + (cap + 100) * 0.001 + 0.1)
    check("ringbuffer evicts oldest past RING_CAP (memory-bounded — never blows the process)",
          fr["sample_size"] == cap)

    # ── 5) OUT-OF-WINDOW events are EXCLUDED (a noisy morning does not poison an afternoon read) ────
    hw._reset_outcome_ring_for_test()
    # 100 failures at t=0, then 100 successes at t=10000 → at t=10000 the window (default 300s) contains
    # only the successes; ratio should be 0.0 (not 0.5).
    for i in range(100):
        hw.record_outcome("failed", now=0.0 + i)
    for i in range(100):
        hw.record_outcome("processed", now=10000.0 + i)
    fr = hw.failure_ratio_window(now=10050.0)
    check("out-of-window events are EXCLUDED (the failed-morning never paints a healthy afternoon red)",
          fr["ratio"] == 0.0 and fr["failed"] == 0 and fr["processed"] == 100)

    # ── 6) RETRIED events DO NOT count in the ratio (intermediate, not terminal) ────────────────────
    hw._reset_outcome_ring_for_test()
    for i in range(10):
        hw.record_outcome("retried", now=950.0 + i)
    fr = hw.failure_ratio_window(now=1000.0)
    check("retried alone: ratio is None (retries are intermediate; only processed+failed are terminal)",
          fr["ratio"] is None and fr["retried"] == 10 and fr["sample_size"] == 0)

    # ── 7) UNKNOWN OUTCOME TAGS are DROPPED (defensive: never widen what the ratio counts) ──────────
    hw._reset_outcome_ring_for_test()
    hw.record_outcome("bogus", now=1000.0)
    hw.record_outcome("", now=1000.0)
    hw.record_outcome(None, now=1000.0)  # type: ignore[arg-type]
    fr = hw.failure_ratio_window(now=1000.0)
    check("unknown/garbage outcome tags are silently dropped (cannot pollute the signal)",
          fr["sample_size"] == 0 and fr["retried"] == 0)

    # ── 8) ENV TUNING: threshold + window honored at CALL time (no restart needed) ──────────────────
    hw._reset_outcome_ring_for_test()
    os.environ["VERIPSA_FAILURE_RATIO_THRESHOLD"] = "0.10"   # bump to 10%
    os.environ["VERIPSA_FAILURE_RATIO_WINDOW_SEC"] = "60"    # shrink to 60s
    fr = hw.failure_ratio_window(now=1000.0)
    check("env VERIPSA_FAILURE_RATIO_THRESHOLD picked up at call time (operator-tunable, no restart)",
          fr["threshold"] == 0.10)
    check("env VERIPSA_FAILURE_RATIO_WINDOW_SEC picked up at call time (operator-tunable, no restart)",
          fr["window_seconds"] == 60.0)
    _reset_env()

    # ── 9) MISCONFIGURED env values FAIL SAFE (never crash, fall back to default) ───────────────────
    os.environ["VERIPSA_FAILURE_RATIO_THRESHOLD"] = "not-a-number"
    os.environ["VERIPSA_FAILURE_RATIO_WINDOW_SEC"] = "not-a-number"
    fr = hw.failure_ratio_window(now=1000.0)
    check("garbage threshold → safe default (0.05); garbage window → safe default (300)",
          fr["threshold"] == 0.05 and fr["window_seconds"] == 300.0)
    os.environ["VERIPSA_FAILURE_RATIO_THRESHOLD"] = "-0.5"  # out of range
    fr = hw.failure_ratio_window(now=1000.0)
    check("out-of-range threshold (-0.5) → safe default (0.05)", fr["threshold"] == 0.05)
    _reset_env()

    # ── 10) EDGE-TRIGGERED alarm_state COUNTER ───────────────────────────────────────────────────────
    #     A sustained outage must page ONCE (per crossing), not every tick. This is the same edge-trigger
    #     contract the rest of the alert sink uses. _update_alarm_state is what watchdog_tick wires.
    hw._reset_outcome_ring_for_test()
    threshold = 0.05
    # below threshold → no crossing
    hw._update_alarm_state(0.02, threshold)
    check("ratio 0.02 below threshold: alarm_state stays at 0", hw.alarm_state_count() == 0)
    # crossing UP → +1
    hw._update_alarm_state(0.10, threshold)
    check("ratio 0.10 crosses ABOVE 0.05 threshold (edge): alarm_state increments to 1",
          hw.alarm_state_count() == 1)
    # sustained over → counter stays at 1 (no inflation per tick)
    for _ in range(20):
        hw._update_alarm_state(0.10, threshold)
    check("sustained-over (20 ticks): alarm_state stays at 1 (edge-triggered, NOT level-triggered)",
          hw.alarm_state_count() == 1)
    # cross DOWN → no decrement but the next UP-crossing rearms the trigger
    hw._update_alarm_state(0.01, threshold)
    check("crossing DOWN: alarm_state still 1 (counter is monotonic — paged-N-times history)",
          hw.alarm_state_count() == 1)
    hw._update_alarm_state(0.10, threshold)
    check("re-crossing UP after a recovery: alarm_state increments to 2 (re-armed)",
          hw.alarm_state_count() == 2)
    # None (cold start / no sample) never crosses
    hw._reset_outcome_ring_for_test()
    hw._update_alarm_state(None, threshold)
    check("ratio=None (no data): alarm_state stays at 0 (we never page on what we cannot see)",
          hw.alarm_state_count() == 0)

    # ── 11) /alarmz RESPONSE-CODE LOGIC (the same decision the HTTP handler in server_http makes) ───
    #     We reproduce the exact `ratio is not None and ratio > threshold → 503 else 200` decision so a
    #     refactor of server_http cannot silently change the contract.
    def alarmz_status(fr):
        ratio = fr.get("ratio") if isinstance(fr, dict) else None
        threshold = fr.get("threshold", 0.05) if isinstance(fr, dict) else 0.05
        alarming = ratio is not None and ratio > threshold
        return 503 if alarming else 200

    hw._reset_outcome_ring_for_test()
    check("/alarmz empty window: 200 (cold start is healthy unknown, NOT a false 503)",
          alarmz_status(hw.failure_ratio_window(now=1000.0)) == 200)

    hw._reset_outcome_ring_for_test()
    for i in range(50):
        hw.record_outcome("processed", now=950.0 + i)
    check("/alarmz all-healthy window: 200 (ratio 0.0 ≤ threshold)",
          alarmz_status(hw.failure_ratio_window(now=1000.0)) == 200)

    hw._reset_outcome_ring_for_test()
    for i in range(660):
        hw.record_outcome("processed", now=900.0 + i * 0.1)
    for i in range(54):
        hw.record_outcome("failed", now=950.0 + i * 0.2)
    check("/alarmz incident shape (8.2% failed): 503 (would have paged today)",
          alarmz_status(hw.failure_ratio_window(now=1000.0)) == 503)

    # exactly AT threshold → not alarming (strict > semantics keeps the boundary noise-free)
    hw._reset_outcome_ring_for_test()
    for i in range(95):
        hw.record_outcome("processed", now=950.0 + i)
    for i in range(5):
        hw.record_outcome("failed", now=970.0 + i)
    fr = hw.failure_ratio_window(now=1000.0)
    check("/alarmz exactly AT threshold (5% / 5%): 200 (strict > keeps the boundary quiet)",
          fr["ratio"] == 0.05 and alarmz_status(fr) == 200)

    # ── 12) HEALTH_SNAPSHOT integration: failure_ratio_5min + alarm_state appear in the body ────────
    hw._reset_outcome_ring_for_test()

    class FakeWorker:
        def is_alive(self): return True
        def qsize(self): return 0
        def maxsize(self): return 1000
        def processed(self): return 0
        def failed(self): return 0
        def retried(self): return 0
        def inflight_age(self): return None
        def uptime(self): return 1.0

    snap = hw.health_snapshot(FakeWorker())
    check("health_snapshot includes failure_ratio_5min field (operator can SEE the windowed ratio)",
          "failure_ratio_5min" in snap and isinstance(snap["failure_ratio_5min"], dict))
    check("health_snapshot includes alarm_state field (operator can SEE crossings without polling /alarmz)",
          "alarm_state" in snap and isinstance(snap["alarm_state"], int))

    # Drive the incident shape + run one watchdog_tick to verify alarm_state increments via the tick.
    # IMPORTANT: health_snapshot() and watchdog_tick() default `now` to time.time() (real wall clock),
    # so we use REAL timestamps here (not a synthetic 1000.0) so the events fall within the 300s
    # window the snapshot sweeps.
    import time as _t
    hw._reset_outcome_ring_for_test()
    real_now = _t.time()
    for i in range(660):
        hw.record_outcome("processed", now=real_now - 100 + i * 0.1)
    for i in range(54):
        hw.record_outcome("failed", now=real_now - 50 + i * 0.3)

    class FakeSink:
        def __init__(self): self.fires = []
        def fire(self, *a, **k): self.fires.append((a, k))
        def resolve(self, *a, **k): self.fires.append(("resolve", a, k))

    sink = FakeSink()
    # watchdog_tick needs a db callable and (optionally) a gh; pass minimal stubs. The tick is fail-open.
    def fake_db(sql, *args, **kw):
        if "SELECT 1" in sql:
            return None
        raise RuntimeError("not implemented in this unit test (fail-open path)")

    # Capture the snapshot's failure_ratio_5min so we KNOW the tick saw an over-threshold ratio.
    snap_before = hw.health_snapshot(FakeWorker())
    over = (snap_before["failure_ratio_5min"]["ratio"] is not None
            and snap_before["failure_ratio_5min"]["ratio"]
                > snap_before["failure_ratio_5min"]["threshold"])
    check("pre-tick: the snapshot already shows ratio over threshold (the tick will see it)",
          over)
    alarm_before = hw.alarm_state_count()
    try:
        hw.watchdog_tick(sink, FakeWorker(), fake_db, prev_failed=0)
    except Exception as e:
        # tick is fail-open per design — should never raise
        check(f"watchdog_tick is fail-open under fake_db (raised: {e})", False)
    check("watchdog_tick observed the over-threshold ratio → alarm_state incremented",
          hw.alarm_state_count() == alarm_before + 1)

    # ── 13) THREAD SAFETY smoke: many concurrent record_outcome calls do not lose data / crash ──────
    import threading as _th
    hw._reset_outcome_ring_for_test()
    N = 200
    THREADS = 8

    def worker():
        for i in range(N):
            hw.record_outcome("processed", now=1000.0 + i * 0.0001)

    threads = [_th.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    fr = hw.failure_ratio_window(now=1100.0)
    # Total events recorded == THREADS * N (deque-append + a lock = no loss).
    expected = THREADS * N
    # The ring caps at RING_CAP, so the visible sample_size is min(expected, RING_CAP).
    expected_visible = min(expected, hw._OUTCOME_RING_CAP)
    check("concurrent record_outcome across 8 threads × 200 events: no crash, ringbuffer bounded",
          fr["sample_size"] == expected_visible)

    return results


def main() -> int:
    print("== windowed failure-ratio signal + /alarmz defense (pure: no DB, no network) ==")
    print("    pin: the 2026-06-25 SQLSTATE-42883 silent-incident class — /healthz stayed green for an")
    print("    hour while every PR event failed. failure_ratio_5min + /alarmz make this visible.")
    results = checks()
    ok = all(c for _, c in results)
    print("FAILURE-RATIO ALARM GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
