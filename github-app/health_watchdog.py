#!/usr/bin/env python3
"""Veripsa GitHub App — the HEALTH + WATCHDOG concern (extracted from server.py to cut its out-degree).

This is the operational OBSERVABILITY/MONITORING unit, lifted whole out of the webhook server so that the
server hotspot file is finer-grained (the same move that already split out event_queue.py / webhook.py):

  health_snapshot(worker)            content-free worker health (the /healthz + /readyz body, no DB)
  db_usage_sample(db, cap_mb)        the whole-DB size/cap/percent the operator db-usage alert watches
  watchdog_tick(sink, worker, db, …) ONE monitor sample: snapshot + DB probe + fire/resolve the proactive
                                     alerts (worker-health, graph-freshness, operator db-usage)
  run_watchdog(sink, worker, db, …)  the background loop that turns PULL /healthz signals into PUSH alerts

DESIGN (mirrors event_queue.py): this module imports NOTHING from server.py at load time (no circular
import — server.py imports THIS). The one server-resident seam the tick needs — `graph_freshness_all`
(which stays in server.py because it is also used by serve()'s /freshz + by self_heal_main_graph) — is an
INJECTED argument (`freshness_fn`); when not injected it is resolved LAZILY (a call-time import, after
server.py has finished loading) so the existing behavior is preserved unchanged. The alert EVALUATORS are
imported from alerts.py (the pure evaluator module), the same dual standalone/package idiom server.py uses.

Behavior-preserving extraction: a pure move + re-import. No logic, signatures, or alert semantics changed.
"""
from __future__ import annotations

import collections
import os
import threading

try:
    from nonblocking_stdio import dropped_writes as _dropped_log_writes
except ImportError:  # imported as a package
    from .nonblocking_stdio import dropped_writes as _dropped_log_writes
try:
    from alerts import dropped_alert_webhooks as _dropped_alert_webhooks
except ImportError:  # imported as a package
    from .alerts import dropped_alert_webhooks as _dropped_alert_webhooks

# env_int / ConfigError — the ONE validated env reader (fail-safe-on-misconfig), used for the cost-sample cadence
# knob. Same dual standalone/package import idiom the rest of the App uses.
try:
    from env_config import env_int, ConfigError  # type: ignore  # noqa: E402
except ImportError:  # imported as a package
    from .env_config import env_int, ConfigError  # type: ignore  # noqa: E402
try:
    from runtime_protocols import (DURABLE_RETRY_PROTOCOL,
                                   REPOSITORY_OFFBOARDING_PROTOCOL)
except ImportError:  # imported as a package
    from .runtime_protocols import (DURABLE_RETRY_PROTOCOL,
                                    REPOSITORY_OFFBOARDING_PROTOCOL)

# The alert EVALUATORS (pure functions over a snapshot — alerts.py). Same standalone/package dual-import idiom
# the rest of the App uses (run as `python3 github-app/server.py` OR imported as a package). _db_size_cap_mb is
# imported HERE at module top (not lazily inside the tick) so it can NEVER be the unhandled error that kills the
# watchdog loop — a module-load failure surfaces at import, the loud + correct place, not silently mid-tick.
try:
    from alerts import (evaluate as alert_evaluate,  # noqa: E402
                        evaluate_graph_freshness as alert_graph_freshness,
                        evaluate_db_usage as alert_db_usage,
                        evaluate_cost as alert_cost,
                        evaluate_delivery_depth as alert_delivery_depth,
                        evaluate_account_convergence_depth as alert_account_convergence_depth,
                        evaluate_app_reachability as alert_app_reachability,
                        evaluate_freshness_blind as alert_freshness_blind,
                        _db_size_cap_mb as _alert_db_size_cap_mb)
except ImportError:  # imported as a package
    from .alerts import (evaluate as alert_evaluate,  # noqa: E402
                        evaluate_graph_freshness as alert_graph_freshness,
                        evaluate_db_usage as alert_db_usage,
                        evaluate_cost as alert_cost,
                        evaluate_delivery_depth as alert_delivery_depth,
                        evaluate_account_convergence_depth as alert_account_convergence_depth,
                        evaluate_app_reachability as alert_app_reachability,
                        evaluate_freshness_blind as alert_freshness_blind,
                        _db_size_cap_mb as _alert_db_size_cap_mb)

import time as _time  # module-level: the loop's liveness heartbeat + the watchdog_last_tick_seconds accessor

# HONEST BUILD LABEL: where /healthz gets the "which build is live" string. It used to be a HAND-EDITED env
# (VERIPSA_VERSION="render-12") that nobody bumped — so right after a deploy /healthz LIED about which code was
# running (it confused even the operator). We now derive it from the DEPLOYED GIT COMMIT, so it is automatic +
# always honest. Resolution order (first that exists wins):
#   1. VERIPSA_BUILD_SHA env   — set by the Dockerfile from the GIT_SHA build arg (the build's commit, short sha)
#   2. /app/BUILD_SHA file     — fallback the Dockerfile bakes from `git rev-parse --short HEAD` if .git is present
#   3. RENDER_GIT_COMMIT env   — the commit Render is RUNNING, injected automatically into every git-backed
#                                service's RUNTIME env by Render (no Dockerfile build-arg, no render.yaml
#                                interpolation needed). Shortened to a 12-char prefix here (RENDER_GIT_COMMIT is
#                                the full 40-char sha) so the label stays a short, content-free token. This is
#                                the rebuild-free safety net: even if the build-arg path above never wires up
#                                (e.g. render.yaml's `${RENDER_GIT_COMMIT}` value interpolation silently no-ops
#                                for a docker-runtime service), /healthz STILL names the real running commit.
#   4. VERIPSA_VERSION env     — the legacy MANUAL label (kept ONLY as a last-resort human override)
#   5. "dev"                   — nothing resolved (e.g. a bare `python3 server.py` with no build metadata)
# The three DERIVED sources (1–3) STRICTLY beat the legacy manual label (4): a deploy can never be masked by a
# stale hand-bumped env. Content-free (a short commit sha — no secrets, no customer data) and it NEVER crashes:
# every read is guarded, any error degrades to the next source (ultimately "dev"). A short sha points at the commit.
_BUILD_SHA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "BUILD_SHA")
_RENDER_SHA_LEN = 12  # how many chars of RENDER_GIT_COMMIT (a full 40-char sha) to surface — short + content-free


def build_version() -> str:
    """The honest build label /healthz reports: the deployed git commit, derived (never hand-bumped). Prefers the
    build-arg env, then the baked BUILD_SHA file, then Render's runtime-injected RENDER_GIT_COMMIT (shortened),
    then the legacy VERIPSA_VERSION override, then 'dev'. The derived sources STRICTLY beat the legacy label.
    Content-free + total (any failure degrades to the next source; the final fallback is always 'dev')."""
    # 1. the build arg the Dockerfile turns into an env (the cleanest, no-filesystem path).
    sha = (os.environ.get("VERIPSA_BUILD_SHA") or "").strip()
    if sha:
        return sha
    # 2. the file the Dockerfile bakes from `git rev-parse` when .git is present (fallback if no build arg).
    try:
        with open(_BUILD_SHA_FILE, "r", encoding="utf-8") as f:
            sha = f.read().strip()
        if sha:
            return sha
    except Exception:
        pass  # file absent/unreadable — fall through (never crash on the version read)
    # 3. RENDER_GIT_COMMIT — the commit Render is RUNNING, injected automatically into the runtime env. This is the
    #    rebuild-free safety net that does NOT depend on the Dockerfile build-arg or render.yaml interpolation
    #    wiring up. Shorten the full 40-char sha to a content-free prefix; still a derived source, so it beats the
    #    legacy manual label below.
    render_sha = (os.environ.get("RENDER_GIT_COMMIT") or "").strip()
    if render_sha:
        return render_sha[:_RENDER_SHA_LEN]
    # 4. the legacy MANUAL label — kept only as a last-resort human override, no longer the primary source.
    legacy = (os.environ.get("VERIPSA_VERSION") or "").strip()
    if legacy:
        return legacy
    # 5. nothing resolved — an honest placeholder, never a stale lie.
    return "dev"

# WATCHDOG LIVENESS: nothing monitored the monitor. The wall-clock of the watchdog's last completed tick is
# stamped here every loop iteration; /healthz reads it (watchdog_last_tick_seconds = age) so a watchdog that has
# silently stopped ticking (despite the loop now being crash-proof) is VISIBLE instead of leaving /healthz green
# while all proactive alerting is dead. A single float write/read is atomic in CPython (no lock needed). None
# until the first tick completes (e.g. before serve() starts the loop, or in a unit test of watchdog_tick alone).
_LAST_WATCHDOG_TICK_AT: float | None = None


def watchdog_last_tick_seconds() -> float | None:
    """Age (seconds) of the watchdog's last completed tick, or None if it has not ticked yet. Surfaced on
    /healthz so a stalled/dead watchdog is observable — the 'who watches the watcher' liveness signal."""
    return None if _LAST_WATCHDOG_TICK_AT is None else round(_time.time() - _LAST_WATCHDOG_TICK_AT, 1)


# APP-JWT REACHABILITY: current clients probe the configured App identity with one cached GET /app point read
# each TTL (legacy/test clients retain the bounded installation-map fallback). Stamp the result here so /readyz
# can SURFACE it without doing its own GitHub call per probe; readiness may be polled aggressively. None until
# the watchdog first probes it (no gh client / before the loop starts / a unit test of a single tick). A single
# reference write/read is atomic in CPython (no lock). DISTINCT from the DB-side app_identity_ok /readyz check:
# that proves the DB role resolves its identity; THIS proves the configured GitHub App identity is reachable.
_APP_JWT_REACHABLE: bool | None = None


def app_jwt_reachable() -> bool | None:
    """The watchdog's last App-JWT reachability observation (True/False), or None if it has not probed yet.
    Surfaced ADVISORY on /readyz: a False here means the systemic-403 blindness is live, but it never BLOCKS
    readiness (the worker + DB can be perfectly healthy while the App-JWT is the problem; blocking ready would
    pointlessly fail a deploy the operator must fix out-of-band). The watchdog already PAGES on it (app_jwt_
    unreachable); this is the at-a-glance probe surface."""
    return _APP_JWT_REACHABLE


# ── PER-EVENT OUTCOME RINGBUFFER + WINDOWED FAILURE-RATIO SIGNAL ────────────────────────────────────────
# WHY this exists: the 2026-06-25 incident — a deploy whose Python called a NEW SQL signature against a
# prod DB still on the OLD signature — failed EVERY PR event with SQLSTATE 42883, yet /healthz stayed
# 200 the whole hour (queue_depth=0 because events failed FAST; worker_alive=True). The running totals
# `processed/failed/retried` were the only failure signal and they are CUMULATIVE — an 8% steady-state
# failure ratio over the WINDOW is invisible when you can only see the totals. So we maintain a tiny,
# content-free ringbuffer of (timestamp, outcome) for the last RING_CAP events and expose:
#   failure_ratio_window()  — failed / (processed + failed) within the last `window_seconds`
#   /alarmz                 — 503 when that ratio is over VERIPSA_FAILURE_RATIO_THRESHOLD, else 200
# An EXTERNAL alerter (UptimeRobot / Render's own healthcheck wired at /alarmz) turns an elevated ratio
# into a page — independently of /healthz's worker-liveness contract. CONTENT-FREE: only the outcome tag
# 'processed' | 'failed' | 'retried' is recorded; never the event type, repo, payload, or error string.
#
# THREADING: the ringbuffer is written by the worker thread (record_outcome from event_queue) and read by
# the HTTP handler threads (/healthz, /alarmz) + the watchdog tick. A `collections.deque(maxlen=…)` plus
# a single lock around append/iterate is enough — the worker calls record_outcome AT MOST once per event
# (microseconds), and the read paths sweep the deque (≤RING_CAP=1000 → trivial). Per-process state — a
# restart resets the ringbuffer (the failure ratio starts unknown on a cold worker, which is HONEST: we
# have no window yet).

_OUTCOME_RING_CAP = 1000   # last N events; bounds memory + read cost (a sweep is O(N), N=1000 ≈ ~microseconds)
_OUTCOME_RING: "collections.deque[tuple[float, str]]" = collections.deque(maxlen=_OUTCOME_RING_CAP)
_OUTCOME_LOCK = threading.Lock()
# Times the watchdog tick observed the failure ratio CROSSING the threshold from below — surfaced on
# /healthz so an operator can see "we have alarmed N times this process-uptime" without polling /alarmz.
# Edge-triggered (only counts a fresh crossing) so a sustained outage does not inflate the counter every
# tick. Written by the watchdog thread only; a single int read is atomic in CPython.
_ALARM_STATE_COUNT: int = 0
_ALARM_PREV_OVER: bool = False  # was the LAST observed ratio over threshold? (for edge-trigger)


def _failure_ratio_window_seconds() -> float:
    """Window size for the failure-ratio signal, read at CALL time so an operator can rebind
    VERIPSA_FAILURE_RATIO_WINDOW_SEC on the fly (and so tests can patch it without restart). Default 300s
    (5 minutes) — long enough to smooth a single-event blip, short enough to catch a deploy-broke-all-PRs
    incident within minutes. Floor at 1s so a typo cannot make the window zero (which would divide-by-zero
    in tests of the bare evaluator)."""
    try:
        return max(1.0, float(os.environ.get("VERIPSA_FAILURE_RATIO_WINDOW_SEC", "300")))
    except (TypeError, ValueError):
        return 300.0


def _failure_ratio_threshold() -> float:
    """Threshold above which /alarmz returns 503 and the watchdog counts an alarm crossing. Read at CALL
    time (VERIPSA_FAILURE_RATIO_THRESHOLD, default 0.05 = 5%) so an operator can tune the sensitivity
    without restart. Clamped to [0.0, 1.0]; a misconfigured value falls back to the 5% default."""
    try:
        v = float(os.environ.get("VERIPSA_FAILURE_RATIO_THRESHOLD", "0.05"))
    except (TypeError, ValueError):
        return 0.05
    if v != v or v < 0.0 or v > 1.0:  # NaN or out of range → safe default
        return 0.05
    return v


def record_outcome(outcome: str, *, now: float | None = None) -> None:
    """Record ONE event outcome ('processed' | 'failed' | 'retried') in the ringbuffer. Called by the
    worker (event_queue._run) at each terminal event outcome. Content-free: only the tag + a timestamp
    are stored. Unknown tags are dropped (defensive — never widen what the ratio counts). Cheap: one
    deque.append under a lock. Designed to be CALLED FROM ANY THREAD safely. `now` is for tests."""
    if outcome not in ("processed", "failed", "retried"):
        return
    t = now if now is not None else _time.time()
    with _OUTCOME_LOCK:
        _OUTCOME_RING.append((t, outcome))


def _outcome_window_counts(now: float, window_seconds: float) -> tuple[int, int, int]:
    """Sweep the ringbuffer and return (processed, failed, retried) within the last `window_seconds`.
    Holds the lock only for the snapshot (a list() of the deque) — the arithmetic happens outside the
    critical section. Total work per call ≤ RING_CAP comparisons + 3 counter bumps."""
    cutoff = now - window_seconds
    with _OUTCOME_LOCK:
        snap = list(_OUTCOME_RING)
    p = f = r = 0
    for t, kind in snap:
        if t < cutoff:
            continue
        if kind == "processed":
            p += 1
        elif kind == "failed":
            f += 1
        elif kind == "retried":
            r += 1
    return p, f, r


def failure_ratio_window(now: float | None = None, window_seconds: float | None = None) -> dict:
    """The windowed failure-ratio signal: returns a dict {window_seconds, processed, failed, retried,
    ratio, sample_size, threshold}. `ratio` is failed / (processed + failed); when the sample is empty
    (cold start / no events in window) it returns None — an honest 'we do not know yet' that /alarmz
    treats as healthy (we cannot page on no data). Retried events DO NOT count in the ratio (they are
    intermediate states already covered by 'processed' or 'failed' on their final outcome).

    Pure read — never raises; safe to call from any thread. The window + threshold are read at CALL
    time so an env rebind takes effect on the next probe with no restart."""
    if now is None:
        now = _time.time()
    if window_seconds is None:
        window_seconds = _failure_ratio_window_seconds()
    p, f, r = _outcome_window_counts(now, float(window_seconds))
    sample_size = p + f
    ratio = (f / sample_size) if sample_size > 0 else None
    return {
        "window_seconds": float(window_seconds),
        "processed": p,
        "failed": f,
        "retried": r,
        "sample_size": sample_size,
        "ratio": ratio,
        "threshold": _failure_ratio_threshold(),
    }


def alarm_state_count() -> int:
    """How many TIMES this process has observed the windowed failure ratio crossing the alarm threshold
    from below (edge-triggered, watchdog-driven). Surfaced on /healthz so an operator can see "we have
    alarmed N times this uptime" without polling /alarmz. Read by HTTP threads; single-int read is
    atomic in CPython."""
    return _ALARM_STATE_COUNT


def _update_alarm_state(ratio: float | None, threshold: float) -> bool:
    """EDGE-TRIGGERED alarm-state update: increment the counter ONLY when the ratio crosses the threshold
    from below (was <=threshold OR unknown → now >threshold). A sustained outage thus counts ONCE per
    enter→exit cycle, not once per tick (a 5-minute outage on a 30s loop would otherwise inflate the
    counter to 10). Called by watchdog_tick. Returns True iff a fresh crossing was observed."""
    global _ALARM_STATE_COUNT, _ALARM_PREV_OVER
    over = ratio is not None and ratio > threshold
    crossed = over and not _ALARM_PREV_OVER
    if crossed:
        _ALARM_STATE_COUNT += 1
    _ALARM_PREV_OVER = over
    return crossed


def _reset_outcome_ring_for_test() -> None:
    """Test-only: clear the ringbuffer + alarm state. Tests of the bare evaluator/edge-trigger need a
    clean slate between scenarios; production code never calls this."""
    global _ALARM_STATE_COUNT, _ALARM_PREV_OVER
    with _OUTCOME_LOCK:
        _OUTCOME_RING.clear()
    _ALARM_STATE_COUNT = 0
    _ALARM_PREV_OVER = False


def health_snapshot(worker, store=None) -> dict:
    """Content-free health + observability for the ack-fast worker (no secrets, no customer data — just
    counts). `healthy` means every configured pool worker is alive: if one dies,
    /healthz fails closed instead of hiding reduced/noisy-neighbour isolation.
    queue_depth / queue_maxsize watch for a backlog; processed/failed are running
    totals. When the durable store is supplied, its dedicated owner-heartbeat
    daemon and last successful beat are surfaced independently from worker
    liveness."""
    alive = worker.is_alive()
    # Oldest in-flight age across the fixed pool (None = all lanes idle). This
    # surfaces one alive-but-stuck lane that thread liveness alone calls green.
    # Older workers may lack inflight_age() — tolerate it.
    try:
        inflight = worker.inflight_age()
    except AttributeError:
        inflight = None
    # in-process retries: transient failures re-run before being counted a terminal loss. Distinct from `failed`
    # (a retried-then-succeeded event increments `retried`, never `failed`). Older workers may lack it — tolerate.
    try:
        retried = worker.retried()
    except AttributeError:
        retried = 0
    try:
        worker_count = int(worker.worker_count())
    except (AttributeError, TypeError, ValueError):
        worker_count = 1
    try:
        alive_workers = int(worker.alive_workers())
    except (AttributeError, TypeError, ValueError):
        alive_workers = 1 if alive else 0
    try:
        inflight_count = int(worker.inflight_count())
    except (AttributeError, TypeError, ValueError):
        inflight_count = 1 if isinstance(inflight, (int, float)) else 0
    try:
        per_account_workers = int(worker.per_account_workers())
    except (AttributeError, TypeError, ValueError):
        per_account_workers = 1
    # Durable claim outcomes are intentionally outside processed/failed: ordered deferral and cross-instance
    # deduplication ran no handler, while missing/unclaimable authority is surfaced separately and also fails loud.
    # Older workers lack these additive counters during a rolling deploy, so default each to zero.
    claim_counts = {}
    for name in ("claim_deferred", "claim_duplicates", "claim_missing", "claim_unclaimable"):
        try:
            claim_counts[name] = int(getattr(worker, name)())
        except (AttributeError, TypeError, ValueError):
            claim_counts[name] = 0
    # WINDOWED FAILURE-RATIO SIGNAL — the 2026-06-25 incident gap (see record_outcome above): cumulative
    # processed/failed hide a sustained 8% failure ratio because both totals climb together. Surface the
    # ratio over the last `window_seconds` so /healthz lets the operator SEE today's 8% (not just the
    # raw counts) and so an external alerter pointed at /alarmz can page on it. Fail-open: any error
    # collapses the field to None (the block is simply omitted; never breaks the snapshot).
    try:
        fratio = failure_ratio_window()
    except Exception:
        fratio = None
    delivery_liveness = None
    if store is not None:
        try:
            sample = store.liveness_snapshot()
            if isinstance(sample, dict):
                delivery_liveness = sample
        except Exception:
            delivery_liveness = None
    snapshot = {
        "service": "veripsa-webhook",
        "version": build_version(),  # the git commit baked at build time — automatic + always honest (no hand-bump)
        # Rollback compatibility contract for repository lifecycle handling. Protocol 2 preserves stable repository
        # ids in the durable inbox and can resolve legacy ID-less removals without deleting a same-name replacement.
        "repository_offboarding_protocol": REPOSITORY_OFFBOARDING_PROTOCOL,
        # Protocol 3 workers consume the DB-owned absolute retry-window
        # remainder. Older /5 workers cannot replay a row once that window is
        # present, so rollback automation must never call such an image fully
        # recovered after the protocol-3 schema has been published.
        "durable_retry_protocol": DURABLE_RETRY_PROTOCOL,
        "healthy": alive,
        "worker_alive": alive,
        "worker_count": worker_count,
        "alive_workers": alive_workers,
        "per_account_workers": per_account_workers,
        "inflight_count": inflight_count,
        "queue_depth": worker.qsize(),
        "queue_maxsize": worker.maxsize(),
        "processed": worker.processed(),
        "failed": worker.failed(),
        "retried": retried,
        "dropped_log_writes": _dropped_log_writes(),
        "dropped_alert_webhooks": _dropped_alert_webhooks(),
        **claim_counts,
        "inflight_age_seconds": round(inflight, 1) if isinstance(inflight, (int, float)) else None,
        "uptime_seconds": round(worker.uptime(), 1),
        "failure_ratio_5min": fratio,         # windowed signal — the field name is fixed at "5min" to match
        #                                       the default window (300s); the actual window is in fratio.window_seconds
        "alarm_state": alarm_state_count(),   # how many times we crossed the threshold this uptime (edge-triggered)
    }
    if delivery_liveness is not None:
        snapshot["delivery_liveness"] = delivery_liveness
        liveness_healthy = delivery_liveness.get("healthy")
        if isinstance(liveness_healthy, bool):
            # The event threads must not remain green after the daemon that
            # maintains their cross-instance lease authority dies or stops
            # beating. Legacy/fake stores omit the snapshot and preserve the
            # historical worker-only contract.
            snapshot["healthy"] = bool(
                snapshot["healthy"] and liveness_healthy)
    return snapshot


def db_usage_sample(db, cap_mb: int) -> dict | None:
    """Read core.db_usage_surface(cap_mb) over the open `db` callable — the whole-DB size + cap + percent the
    operator DB-usage alert watches. Cheap (ONE size query — pg_database_size + a bounded top-N relation scan).
    Content-free (sizes/counts only — never a row). Returns the surface dict, or None on any error (fail-open —
    the watchdog must never crash on a size read). The cap is passed in so the surface's pct matches the alert's."""
    try:
        raw = db("SELECT core.db_usage_surface(%s)", (int(cap_mb),))
    except Exception as e:
        print(f"watchdog db-usage sample skipped: {str(e)[:120]}", flush=True)
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            import json as _j
            v = _j.loads(raw)
            return v if isinstance(v, dict) else None
        except Exception:
            return None
    return None


def cost_sample(db) -> dict | None:
    """Read core.owner_cost_surface() over the open `db` callable — the SAME content-free aggregate cost_report.py
    prints (whole-DB bytes + the per-account free-line rollup). This is the signal the PER-ACCOUNT cost alerts
    (alerts.evaluate_cost → db_size_high + account_over_line) watch — DISTINCT from db_usage_sample's whole-DB
    percent (alerts.evaluate_db_usage → db_usage_high): one tenant can cross its free line while the whole DB is
    nowhere near its cap, and vice-versa. HEAVIER than the other samples (it visits every tenant's RLS wall —
    see core.owner_cost_surface), which is why the tick runs it on a SLOW cadence, not every loop. The live
    watchdog's `db` runs as veripsa_app, which IS granted EXECUTE on owner_cost_surface (the SAME grant as
    db_usage_surface — db/schema/95_owner.sql), so no separate OWNER DSN is needed here (cost_report.py prefers
    VERIPSA_OWNER_DSN only because a founder may run it under a more-restricted role). Content-free (ids + counts/
    bytes only — the surface itself is the moat boundary). Returns the surface dict, or None on ANY error
    (fail-open — the watchdog must never crash on a cost read)."""
    try:
        raw = db("SELECT core.owner_cost_surface()")
    except Exception as e:
        print(f"watchdog cost sample skipped: {str(e)[:120]}", flush=True)
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            import json as _j
            v = _j.loads(raw)
            return v if isinstance(v, dict) else None
        except Exception:
            return None
    return None


def account_convergence_depth_sample(db) -> dict | None:
    """Read the generation-21 global account-convergence aggregate.

    This is one content-free SECURITY DEFINER aggregate over the indexed
    scheduler router, never an account/repository enumeration. The injected
    ``db`` callable owns a short-lived connection per query, so the watchdog
    does not share a long transaction with convergence work. Missing schema,
    query failure, or an undecodable result is Unknown (``None``), never a
    synthetic empty sample that could falsely resolve a standing alert.
    """
    try:
        raw = db(
            "SELECT core.account_convergence_depth_with_authority()"
        )
    except Exception as exc:
        # Exception text may contain connection metadata. The type is enough
        # for an operator to distinguish a query failure without leaking it.
        print(
            "watchdog account-convergence sample skipped: "
            f"{type(exc).__name__}",
            flush=True,
        )
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            import json as _j
            value = _j.loads(raw)
            return value if isinstance(value, dict) else None
        except Exception:
            return None
    return None


def _resolve_freshness_fn(freshness_fn):
    """The freshness sampler `graph_freshness_all(db, gh) -> list` lives in server.py (it is also used by
    serve()'s /freshz and by self_heal_main_graph), so this module cannot import it at load time without a
    circular import. Resolve it LAZILY: the caller may INJECT it (the live serve() path does), else import it
    from server at call-time (server.py is fully loaded by the time a tick runs). Returns None if unresolvable
    (fail-open: the freshness alert is simply skipped — never a crash)."""
    if freshness_fn is not None:
        return freshness_fn
    try:
        from server import graph_freshness_all  # call-time import → no module-load circularity
    except ImportError:
        try:
            from .server import graph_freshness_all
        except ImportError:
            return None
    return graph_freshness_all


def app_jwt_reachability_probe(gh, db) -> tuple[bool | None, int | None]:
    """Probe the App-JWT identity and how many routed installations Core knows.

    Current GitHubREST clients use one cached ``GET /app`` point read, whose
    returned App id must match configuration. This stays O(1) as the tenant
    fleet grows. Older/test clients may expose only the predecessor bounded
    installation-map probe; that compatibility branch remains fail-closed.

    These are the inputs alerts.evaluate_app_reachability watches.
    Returns (reachable, installation_rows):
      reachable        — True iff the cached App registration point-read
                         succeeds with the configured App id. False on a
                         current failure/mismatch; None when no probe exists.
      installation_rows— content-free count from core.list_installation_ids() (installation ids only, no accounts,
                         no bodies; the function is granted to veripsa_app). None on any DB error (we then do not
                         page — we cannot prove there is anything to serve).
    Fail-open: ANY error degrades to (None/False, None) — the probe must never take the watchdog tick down. The
    reachability boolean is deliberately crossed with installation_rows by the evaluator, NOT here, so this stays a
    thin sampler. Content-free (a boolean + a count; never a tenant id,
    org name, token, or DSN)."""
    if gh is None or not (
        hasattr(gh, "app_registration_reachability")
        or hasattr(gh, "app_installations_reachability")
    ):
        return None, None
    # count routed installations (content-free: ids only, via the SECURITY DEFINER enumerator granted to veripsa_app)
    rows = None
    try:
        rows = db("SELECT count(*) FROM core.list_installation_ids()")
        rows = int(rows) if isinstance(rows, (int, float)) else None
    except Exception as e:
        print(f"watchdog app-reachability install-count skipped: {str(e)[:120]}", flush=True)
        rows = None
    # Prefer the constant-time App registration identity probe. The legacy
    # install-map branch retains its empty-with-routes mismatch detection for
    # rolling/test clients that do not yet expose the point read.
    try:
        point_probe = getattr(gh, "app_registration_reachability", None)
        uses_point_probe = callable(point_probe)
        probe = (
            point_probe()
            if uses_point_probe
            else gh.app_installations_reachability()
        )
        raw_reachable = probe.get("reachable") if isinstance(probe, dict) else None
        if raw_reachable is True:
            if uses_point_probe:
                reachable = True
            else:
                installs_count = (
                    probe.get("installations_count")
                    if isinstance(probe, dict)
                    else None
                )
                reachable = not (
                    installs_count == 0
                    and isinstance(rows, int)
                    and rows >= 1
                )
        elif raw_reachable is False:
            reachable = False
        else:
            reachable = None
    except Exception as e:
        print(f"watchdog app-reachability probe: App identity reachability failed: {str(e)[:120]}", flush=True)
        reachable = False
    return reachable, rows


def watchdog_tick(sink, worker, db, prev_failed: int, gh=None, freshness_fn=None, run_cost: bool = False,
                  store=None) -> int:
    """ONE watchdog sample: build a health snapshot, probe DB reachability, and fire/resolve proactive alerts
    (see alerts.evaluate). When a `gh` client is supplied, ALSO sample graph FRESHNESS (the stored main-graph vs
    main HEAD) and fire/resolve the stale-graph alert (alerts.evaluate_graph_freshness) — so a SILENT drift goes
    loud even before the next PR self-heals it. When the DB is reachable it ALSO samples OPERATOR DB-USAGE (the
    whole-instance size vs the configured storage cap) and fires/resolves db_usage_high (alerts.evaluate_db_usage)
    — the AWS-billing-alarm equivalent so a silent disk-fill / surprise bill goes loud. When `run_cost` is set
    (and the DB is reachable) it ALSO samples the PER-ACCOUNT cost surface (core.owner_cost_surface) and fires/
    resolves db_size_high + account_over_line (alerts.evaluate_cost) — so a tenant crossing the free line PAGES at
    runtime instead of being visible only when the founder runs cost_report.py by hand (pull-not-push). This is
    gated (not every tick) because owner_cost_surface visits every tenant's RLS wall — heavier than the other
    samples — so the loop runs it on a SLOW cadence (see run_watchdog's cost_every). Pure-ish (side effects: the
    alerts + a cheap `SELECT 1` + the freshness/size/cost reads); fail-open — a probe/freshness/size/cost error
    never propagates. Returns the failed count for the next tick. Content-free.

    `freshness_fn` is the injected graph_freshness_all seam (signature graph_freshness_all(db, gh) -> list); it
    lives in server.py and is resolved lazily when not injected (see _resolve_freshness_fn).
    `run_cost` lets the caller (run_watchdog) decide WHICH ticks pay for the heavier cost sample, keeping this
    function pure/testable (the tick itself holds no cadence state)."""
    try:
        snap = health_snapshot(worker, store=store)
    except Exception:
        return prev_failed
    # WINDOWED FAILURE-RATIO ALARM-STATE UPDATE: edge-trigger the alarm-state counter the operator sees
    # on /healthz. The /alarmz endpoint returns 503 on the SAME condition, but it is pulled — this gives
    # us a heartbeat-driven crossing record even when nobody is polling /alarmz right now. Fail-open: any
    # error never propagates (the alarm-state is best-effort observability, not a correctness boundary).
    try:
        fr = snap.get("failure_ratio_5min") if isinstance(snap, dict) else None
        if isinstance(fr, dict):
            _update_alarm_state(fr.get("ratio"), fr.get("threshold", _failure_ratio_threshold()))
    except Exception:
        pass
    db_ok = True
    try:
        db("SELECT 1")
    except Exception:
        db_ok = False
    try:
        result = alert_evaluate(sink, snap, db_ok, prev_failed)
    except Exception:
        result = snap.get("failed", prev_failed)
    # GRAPH-FRESHNESS ALERT (best-effort, fail-open): only when DB is reachable + a gh client is wired (the live
    # serve() path). A freshness sample = the DB surface read + one HEAD resolve per repo. Any error is swallowed
    # (the freshness alert must never take the watchdog down — same fail-open discipline as the rest of the tick).
    # We CAPTURE the freshness list (not just pass it through) so the freshness_blind check below can read how many
    # coordinates went behind=unknown this tick — the SYMPTOM of the systemic App-JWT blindness probed just after.
    _fresh_sample = None
    if gh is not None and db_ok:
        try:
            _freshness = _resolve_freshness_fn(freshness_fn)
            if _freshness is not None:
                _fresh_sample = _freshness(db, gh)
                alert_graph_freshness(sink, _fresh_sample)
        except Exception as e:
            print(f"watchdog freshness sample skipped: {str(e)[:120]}", flush=True)
    # APP-JWT REACHABILITY + FRESHNESS-BLIND ALERTS (best-effort, fail-open): use the constant-time App identity
    # point probe (legacy clients use the install-map fallback), crossed with the content-free routed-install count.
    # A failure is loud through app_jwt_unreachable; when most sampled coordinates are simultaneously Unknown,
    # freshness_blind identifies systemic blindness rather than a missed push. Only when a gh client is wired + DB
    # reachable; any error is swallowed (same fail-open discipline).
    app_reachable = None
    if gh is not None and db_ok:
        try:
            app_reachable, install_rows = app_jwt_reachability_probe(gh, db)
            alert_app_reachability(sink, app_reachable, install_rows)
            # STAMP the reachability so /readyz can surface it without its own GitHub call. Only stamp a DEFINITE
            # observation (True/False); a None (probe not taken) leaves the prior value untouched.
            if app_reachable is not None:
                global _APP_JWT_REACHABLE
                _APP_JWT_REACHABLE = app_reachable
        except Exception as e:
            print(f"watchdog app-reachability sample skipped: {str(e)[:120]}", flush=True)
        # freshness_blind: a high fraction of behind=unknown WHILE the App-JWT is unreachable = blind (not stale).
        try:
            if isinstance(_fresh_sample, list):
                _total = len(_fresh_sample)
                _behind_none = sum(1 for f in _fresh_sample
                                   if isinstance(f, dict) and f.get("behind") is None)
                alert_freshness_blind(sink, _total, _behind_none, app_reachable)
        except Exception as e:
            print(f"watchdog freshness-blind sample skipped: {str(e)[:120]}", flush=True)
    # OPERATOR DB-USAGE ALERT (best-effort, fail-open): only needs the DB reachable (no GitHub call). One bounded
    # size query → the whole-instance footprint vs the configured cap. UNCAPPED (VERIPSA_DB_SIZE_CAP_MB unset/0)
    # makes the evaluator a silent no-op; a misconfigured cap fails SAFE (caught → uncapped). Any error swallowed.
    if db_ok:
        try:
            cap_mb = _alert_db_size_cap_mb()   # imported at module top (not lazily) — see the top-of-file note
            alert_db_usage(sink, db_usage_sample(db, cap_mb), cap_mb=cap_mb)
        except Exception as e:
            print(f"watchdog db-usage sample skipped: {str(e)[:120]}", flush=True)
    # ACCOUNT-CONVERGENCE DEPTH (generation 21): one global, aggregate-only
    # scheduler query on EVERY DB-healthy tick. This is deliberately independent
    # of PolicyRefreshStore and of the slower O(N) cost sampler: a due graph/
    # policy turn must warn at 120s and page at 300s even when queue depth is
    # only 11. A missing/malformed sample is Unknown and the evaluator performs
    # no false resolve.
    if db_ok:
        try:
            alert_account_convergence_depth(
                sink,
                account_convergence_depth_sample(db),
            )
        except Exception as exc:
            print(
                "watchdog account-convergence evaluation skipped: "
                f"{type(exc).__name__}",
                flush=True,
            )
    # PER-ACCOUNT COST ALERT (best-effort, fail-open, SLOW cadence): only when the DB is reachable AND this is a
    # designated cost tick (run_cost — owner_cost_surface visits every tenant, so the loop pays for it rarely, not
    # every 30s). Fires db_size_high (whole-DB bytes ≥ the absolute threshold — fires even when no percent-cap is
    # configured, the common starter case db_usage_high stays silent for) and account_over_line (a tenant crossed
    # its free line — the billing/limit cue that ONLY cost_report.py surfaced before, pull-not-push). Any error is
    # swallowed (a cost read must never take the watchdog down — same fail-open discipline as the rest of the tick).
    if db_ok and run_cost:
        try:
            alert_cost(sink, cost_sample(db))
        except Exception as e:
            print(f"watchdog cost sample skipped: {str(e)[:120]}", flush=True)
    # DURABLE WEBHOOK INBOX DEPTH ALERT (audit P1, best-effort, fail-open): the P0 durability boundary had NO
    # observability — webhook_delivery_depth_with_authority() existed but nothing called it, so a growing durable
    # 'queued' backlog or any accumulating 'failed' (dead-lettered) row was SILENT. When a durable `store` is wired
    # (the live serve() path), sample its depth on its OWN short-lived connection (independent of the per-tick `db`
    # probe above) and fire/resolve delivery_dead_letter + delivery_backlog. A depth read error is swallowed (the
    # inbox-depth alert must never take the watchdog down — same fail-open discipline as every other sub-check).
    if store is not None:
        try:
            # Pass the store's ACTUAL stale window so the lane-frozen narration names an honest reclaim ETA
            # (an env-overridden store must not have the alert quote the shipped default).
            alert_delivery_depth(sink, store.depth(),
                                 stale_seconds=getattr(store, "stale_seconds", None))
        except Exception as e:
            print(f"watchdog delivery-depth sample skipped: {str(e)[:120]}", flush=True)
    return result


def run_watchdog(sink, worker, db, interval: float = 30.0, stop=None, gh=None, freshness_fn=None,
                 cost_interval: float | None = None, store=None):
    """The background MONITOR loop: every `interval` seconds, take a watchdog_tick. This is what turns the
    PULL-based /healthz signals into PUSH-based alerts — without it, a dead worker / backlog / failed spike /
    DB outage / STALE GRAPH / a tenant over the free line / a DEAD-LETTERED webhook delivery is silent until a
    human looks. Daemon thread; one DB ping per tick (cheap). When a `gh` client is passed, each tick also samples
    graph freshness. When a durable `store` is passed (the live serve() path), each tick also samples the DURABLE
    WEBHOOK INBOX depth (core.webhook_delivery) and fires/resolves delivery_dead_letter (any 'failed' row — a real
    invisible loss) + delivery_backlog (the persisted 'queued' pile is not draining) — the P0 durability boundary
    that previously had NO observability (webhook_delivery_depth_with_authority existed but nothing called it).

    The PER-ACCOUNT cost sample (owner_cost_surface — heavier: it visits every tenant) runs on a SLOW cadence,
    NOT every tick: `cost_interval` seconds (env VERIPSA_WATCHDOG_COST_INTERVAL, default 3600 = 1h), turned into
    "every Nth tick" so a 30s health loop still pages on a free-line crossing within the hour without re-scanning
    every tenant every 30s. The FIRST tick always runs it (a fresh deploy with an already-over-line tenant pages
    promptly, not an hour later).

    NEVER RAISES OUT — structurally. The whole loop body is wrapped: a watchdog_tick is already fail-open per
    sub-check, but a defect ANYWHERE in the body (a sink bug, an unexpected snapshot shape, an import that goes
    bad) must NOT silently kill THIS thread — that would stop ALL proactive alerting while /healthz stayed green
    (nothing watches the monitor). On any tick error we log `watchdog tick error: …` and continue to the next
    tick; the loop survives. Each COMPLETED iteration stamps a liveness heartbeat (watchdog_last_tick_seconds) so
    a watchdog that DID stop is observable on /healthz rather than failing silent."""
    global _LAST_WATCHDOG_TICK_AT
    if cost_interval is None:
        try:
            cost_interval = env_int("VERIPSA_WATCHDOG_COST_INTERVAL", 3600, min_value=1)
        except ConfigError:
            cost_interval = 3600  # a typo'd cadence FAILS SAFE → the shipped 1h default (never a crash on the loop)
    # how many ticks between cost samples (≥1): a slow cadence layered on the fast health loop. interval can be
    # tiny in tests; guard the division and floor at 1 so we never divide by zero or skip the sample forever.
    cost_every = max(1, round(float(cost_interval) / interval)) if interval and interval > 0 else 1
    prev_failed = 0
    tick = 0
    while stop is None or not stop():
        try:
            run_cost = (tick % cost_every == 0)  # tick 0 (first pass) → True, then every cost_every-th tick
            # Pass `store` only when wired (the live serve() path) so a watchdog_tick that predates the param — or a
            # test that monkeypatches it with the older signature — is never handed an unexpected kwarg. The durable
            # inbox-depth alert (delivery_dead_letter / delivery_backlog) fires only when a store is present.
            _kw = dict(gh=gh, freshness_fn=freshness_fn, run_cost=run_cost)
            if store is not None:
                _kw["store"] = store
            prev_failed = watchdog_tick(sink, worker, db, prev_failed, **_kw)
            _LAST_WATCHDOG_TICK_AT = _time.time()   # heartbeat: a tick completed → the monitor is alive
        except Exception as e:
            # the monitor must outlive any single bad tick — log + continue, never let the thread die.
            try:
                print(f"watchdog tick error: {str(e)[:200]}", flush=True)
            except Exception:
                pass
        tick += 1                                   # advance EVERY iteration (even a raising one) so the cost
        #                                             cadence keeps progressing and no tick is retried forever
        _time.sleep(interval)
