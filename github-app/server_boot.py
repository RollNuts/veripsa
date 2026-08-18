"""The BOOT / CONFIG / RUNTIME-WIRING concern — extracted from the server god-file (the #2 coupling hotspot:
coupling 242 = fan-in 43 + fan-out 199; serve() was 245 lines inlining the HTTP Handler + the env/startup block).

serve() used to do THREE different jobs in one 245-line function: (1) read+validate the live config from the
environment and construct the real GitHub client; (2) WIRE the runtime — the durable inbox store, the per-event
processor, the in-process worker, the boot-reconcile thread, the delivery recovery loop, and the proactive
watchdog; and (3) define the HTTP Handler + bind+run the server. This module owns (1) and (2) — the env/config
read and the store/queue/watchdog wiring — so serve() can become a THIN composition root: load config → wire the
runtime → build the handler (server_http.py) → run it. It changes for DIFFERENT reasons than the request-routing
HTTP layer (server_http.py) or the event-type router (webhook_handlers.py): it changes when a startup knob,
durability/recovery wiring, or a background loop's wiring changes.

Symbols (MOVED here from serve() UNCHANGED — same env vars, same defaults, same opt-out switches, same order):
  load_config(dsn?)          read VERIPSA_DSN/GH_* + every VERIPSA_* knob, load the PEM, build GitHubREST → BootConfig
  wire_runtime(cfg)          stand up store + processor + worker + boot-reconcile + recovery loop + watchdog → Runtime

What deliberately STAYS in server.py (NOT moved here):
  * the EMPTY-SECRET STARTUP GUARD stays INLINE in serve() — it is the fail-closed security boundary the
    perimeter gate pins by inspect.getsource(server.serve) (it asserts GH_WEBHOOK_SECRET / SystemExit /
    VERIPSA_ALLOW_UNSIGNED appear in serve()'s OWN source). load_config() returns the resolved secret; serve()
    refuses to start on an empty one. (Keeping the guard in serve() also keeps the refusal on the entry path
    that test_app_deploy_resolution.py's S.serve(0) exercises directly.)
  * _scoped_db (the single-connection DB runner) stays in server.py — test_external_resilience /
    test_suspend_handler / test_pauseack_* / test_tamper_account_authority monkeypatch server._scoped_db to inject
    mid-event connection drops; the `_server()._scoped_db` seam in event_processor.py reaches it through that name.
    _make_db (the connect-per-query sibling) now lives in server_dbops.py with its sibling lock-and-stats plumbing
    and is RE-EXPORTED on server.py, so `_server()._make_db(dsn)` below still resolves unchanged. This module
    RECEIVES the already-built `db` (the connect-per-query runner) on the config.

DESIGN (mirrors event_queue.py / event_processor.py / ingest.py / health_watchdog.py): this module imports
NOTHING from server.py at LOAD time (no circular import — server.py imports THIS). The server-resident seams the
wiring needs at runtime — the per-event PROCESSOR factory make_db_processor, the event→(repo,account) routing
_event_account_key / _event_repo, the push-coalescing key _branch_from_ref, the boot reconcile boot_reconcile,
and the graph_freshness_all seam the watchdog samples — are resolved at CALL time off the `server` module via the
same lazy `_server()` idiom event_processor.py uses, so a test that monkeypatches any of them (or the wiring
collaborators DeliveryStore / EventQueue / run_watchdog / start_instance_liveness_loop / start_recovery_loop,
all re-exported on server) is still
seen. env_int (the validated env-knob reader) and AlertSink import directly from their own modules (no cycle).

Behavior-preserving extraction: a pure move. No env var, default, opt-out switch, thread name, daemon flag,
print line, or wiring ORDER changed — only the home of the config-read + the wiring. serve() calls these two in
the same order the inline block ran, between the (unchanged, inline) startup guard and the (extracted) HTTP run.
"""
from __future__ import annotations

import math
import os
import inspect
import threading
from typing import Any, Callable, NamedTuple, Optional

# env_int: the VALIDATED env-knob reader (fail SAFE on a typo'd cap — a non-int or out-of-range value raises a
# loud, actionable refusal that names the var + value + range, instead of crashing bare or misbehaving on a
# 0/negative cap). Same standalone/package dual-import idiom server.py uses. Imported at LOAD time: env_config.py
# imports nothing from server (no cycle).
try:
    from env_config import env_int  # noqa: E402
except ImportError:  # imported as a package
    from .env_config import env_int  # noqa: E402
# start_site_keepwarm: the ops stopgap that keeps the public site's sleeping instance tier awake. Self-contained
# (stdlib + env_int only), imports nothing from server.py, and is a no-op unless VERIPSA_KEEPWARM_URL is set.
try:
    from site_keepwarm import start_site_keepwarm  # noqa: E402
except ImportError:  # imported as a package
    from .site_keepwarm import start_site_keepwarm  # noqa: E402
# AlertSink: the content-free PUSH-alert channel the watchdog fires through (Slack/Discord/generic webhook + always
# stdout). It imports nothing from server.py, so it is safe at LOAD time here.
try:
    from alerts import AlertSink  # noqa: E402
except ImportError:  # imported as a package
    from .alerts import AlertSink  # noqa: E402
# GitHubREST: the live App-JWT client load_config() constructs from GH_APP_ID / GH_PRIVATE_KEY / GH_INSTALLATION_ID.
# Imports nothing from server.py.
try:
    from github_rest import GitHubREST  # noqa: E402
except ImportError:  # imported as a package
    from .github_rest import GitHubREST  # noqa: E402
try:
    from failed_delivery_recovery import (FailedDeliveryRecoveryStore,  # noqa: E402
                                          start_failed_delivery_recovery)
except ImportError:  # imported as a package
    from .failed_delivery_recovery import (FailedDeliveryRecoveryStore,  # noqa: E402
                                           start_failed_delivery_recovery)
# schema_contract: the BOOT-TIME SCHEMA↔RUNTIME CONTRACT. Python ahead of the DB schema can fail every event
# while a shallow process probe remains green. The check below queries pg_catalog for the exact function,
# arity, column, and type contract; a missing entry flips health to 503 so the new build is not promoted.
# Imports nothing from server.py (no cycle).
try:
    from schema_contract import check_schema_contract, set_boot_result  # noqa: E402
except ImportError:  # imported as a package
    from .schema_contract import check_schema_contract, set_boot_result  # noqa: E402
try:
    from nonblocking_stdio import install_nonblocking_stdio  # noqa: E402
except ImportError:  # imported as a package
    from .nonblocking_stdio import install_nonblocking_stdio  # noqa: E402


def _server():
    """The server module, resolved at CALL time (NOT at load — server.py imports THIS, so a load-time
    `import server` here would be a circular import). server.py is fully loaded by the time load_config /
    wire_runtime run (serve() calls them), so this just returns the already-imported module. Mirrors
    event_processor._server / ingest._server. Used to reach the server-resident SEAMS the wiring needs so a
    test's monkeypatch on them is still honored: the per-event PROCESSOR factory make_db_processor, the
    event→(repo,account) routing _event_account_key / _event_repo, the push-coalescing key _branch_from_ref,
    boot_reconcile, the graph_freshness_all seam the watchdog samples, and the wiring collaborators
    DeliveryStore / EventQueue / run_watchdog / start_instance_liveness_loop / start_recovery_loop
    (all re-exported on server)."""
    try:
        import server  # type: ignore
    except ImportError:  # imported as a package
        from . import server  # type: ignore
    return server


def _watchdog_interval_seconds() -> float:
    """Validated watchdog cadence. Non-positive / malformed values fail safe to the shipped 30s default; tiny
    positive values clamp to 1s so an env typo cannot spin the monitor thread."""
    raw = os.environ.get("VERIPSA_WATCHDOG_INTERVAL", "30")
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        return 30.0
    if not math.isfinite(interval) or interval <= 0.0:
        return 30.0
    return max(1.0, interval)


# Bump whenever boot reconciliation gains a new state-convergence responsibility. The persisted version bypasses
# the ordinary "ran recently" throttle exactly once after a deploy, so a newly shipped self-heal is not postponed
# for up to an hour by the previous binary's timestamp. Content-free and monotonic by convention.
_BOOT_RECONCILE_VERSION = 3


class BootConfig(NamedTuple):
    """The resolved live config — everything read from the environment ONCE at startup. serve() consults
    `secret` for its inline empty-secret guard; wire_runtime consumes the rest."""
    secret: str                 # GH_WEBHOOK_SECRET (serve()'s inline guard refuses an empty one unless opted in)
    dsn: str                    # VERIPSA_DSN (postgres as veripsa_app)
    gh: Any                     # the live GitHubREST client (App-JWT auth)
    db: Callable                # the connect-per-query runner (server._make_db(dsn)) — startup work only


class Runtime(NamedTuple):
    """The wired runtime serve() hands to the HTTP layer: the in-process scheduler `worker`, the durable `store`
    (always present for repository-offboarding authority), and whether every event should persist through it.
    VERIPSA_DURABLE_INBOX=0 disables ordinary-event persistence only; it cannot disable the deletion boundary."""
    worker: Any
    store: Any
    persist_all: bool


def _should_start_inprocess_convergence() -> bool:
    """Return whether this web-process composition root may own the legacy drainer.

    An unset role deliberately preserves the predecessor/local embedding contract:
    the existing VERIPSA_POLICY_REFRESH kill switch remains authoritative.  A new
    production web image is role-aware and must never compete with the dedicated
    convergence workers.  Any other explicit role means the web entrypoint was
    misrouted, so fail before partially wiring runtime state.
    """
    role = os.environ.get("VERIPSA_RUNTIME_ROLE", "").strip()
    enabled = os.environ.get("VERIPSA_POLICY_REFRESH", "1") != "0"
    if not role:
        return enabled
    if role == "web":
        return False
    raise RuntimeError(
        f"server web entrypoint refuses VERIPSA_RUNTIME_ROLE={role!r}"
    )


def load_config(dsn: Optional[str] = None) -> BootConfig:
    """Read the live config from the environment ONCE: the webhook secret, the DSN, the loaded private key, and
    the constructed GitHubREST client + the connect-per-query db runner. Pure config assembly — no server is
    started and (deliberately) NO empty-secret guard here: that fail-closed refusal stays INLINE in serve() so
    the perimeter gate's inspect.getsource(server.serve) still sees it. `dsn` defaults to os.environ['VERIPSA_DSN']
    (the live path); an explicit dsn is accepted for symmetry/testability."""
    secret = os.environ.get("GH_WEBHOOK_SECRET", "")
    dsn = dsn if dsn is not None else os.environ["VERIPSA_DSN"]
    pkey = os.environ.get("GH_PRIVATE_KEY", "")
    if pkey and os.path.exists(pkey):
        pkey = open(pkey).read()
    gh = GitHubREST(os.environ.get("GH_APP_ID", ""), pkey, os.environ.get("GH_INSTALLATION_ID", ""))
    db = _server()._make_db(dsn)
    return BootConfig(secret=secret, dsn=dsn, gh=gh, db=db)


def _boot_reconcile_throttled_once(db: Callable, gh: Any, cap: int, dsn: Optional[str],
                                   min_interval_min: int, force: bool,
                                   deadline_seconds: int = 0) -> None:
    """The throttled wrapper around boot_reconcile (perf follow-up, Round-2). Runs IN the boot thread, so a DB
    hiccup at boot never blocks /healthz coming up. Behaviour-preserving for the initial install / first-deploy
    case: no last-run row on file → no skip (the reconcile runs). Behaviour-preserving for the OFF / FORCED
    paths: `min_interval_min == 0` OR `force == True` → never skips. Behaviour-preserving for a CALLER that
    doesn't have the throttle SQL (e.g. older schema, a test fixture): the read fails soft (a warn line, then
    the reconcile runs) — degraded but never broken.
    Resolves S.boot_reconcile lazily through _server() so tests that monkeypatch it (the same seam wire_runtime
    uses) are still honoured. Behaviour for tests that pass a fake `db` runner: the throttle's `db(...)` call
    goes through the SAME runner, so a fake that no-ops returns nothing → no row → no skip → reconcile runs."""
    # OFF / FORCED → always run, never read/write the timestamp (matches the previous unconditional behaviour).
    if min_interval_min > 0 and not force:
        try:
            row = db("SELECT core.read_boot_reconcile_last_run_with_authority()")
            # the connect-per-query runner returns the single scalar; normalise to a dict (psycopg2 already
            # decodes jsonb to a Python dict, but a test fake might pass a string — accept either).
            if isinstance(row, str):
                import json as _json
                row = _json.loads(row)
            if isinstance(row, dict) and row.get("found") and row.get("age_seconds") is not None:
                age_s = int(row["age_seconds"])
                cap_s = int(min_interval_min) * 60
                prior_fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
                prior_version = prior_fields.get("reconcile_version")
                if age_s < cap_s and prior_version == _BOOT_RECONCILE_VERSION:
                    print(f"boot reconcile SKIPPED — last run {age_s // 60} minutes ago "
                          f"(< VERIPSA_BOOT_RECONCILE_MIN_INTERVAL_MIN={min_interval_min}min). "
                          f"VERIPSA_BOOT_RECONCILE_FORCE=1 to override.", flush=True)
                    return
                if age_s < cap_s:
                    print(f"boot reconcile version changed {prior_version!r} → {_BOOT_RECONCILE_VERSION}; "
                          "bypassing recent-run throttle once", flush=True)
        except Exception as e:
            # FAIL-OPEN (degraded-but-running): a transient DB hiccup or an older schema without the throttle
            # fns must NOT strand the self-heal — log it content-free and run the reconcile (the original
            # contract). The next boot tries again.
            print(f"boot reconcile throttle read failed ({str(e)[:160]}) — running reconcile anyway", flush=True)

    S = _server()
    # deadline_seconds=0 → unlimited (behaviour-preserving). Passed positionally would break monkeypatched tests
    # that take 4 args; kwarg keeps the resolved seam back-compat (S.boot_reconcile signature has it as a kwarg
    # with a behaviour-preserving default, so a fake that takes (db,gh,cap,dsn) still resolves cleanly).
    if deadline_seconds and deadline_seconds > 0:
        result = S.boot_reconcile(db, gh, cap, dsn, deadline_seconds=deadline_seconds)
    else:
        result = S.boot_reconcile(db, gh, cap, dsn)
    retryable_cursor_failure = (
        isinstance(result, dict)
        and (
            result.get("cursor_healthy") is False
            or bool(result.get("error"))
        )
    )

    # Stamp the timestamp AFTER the reconcile finishes (success OR best-effort skipping rows). The reconcile
    # itself is fault-tolerant per-repo and only RAISES on a corrupted schema; in practice it always returns
    # a dict (possibly with 0 reconciled). Persist the content-free counts so the owner can see what the last
    # sweep covered. FAIL-OPEN on the write too: a stamp failure is logged + skipped (the next boot will
    # re-reconcile, which is exactly the pre-throttle behaviour).
    if min_interval_min > 0 and not force and not retryable_cursor_failure:
        try:
            import json as _json
            fields = {"reconcile_version": _BOOT_RECONCILE_VERSION}
            if isinstance(result, dict):
                # carry forward only the content-free counts (no repo names / ids)
                for k in ("reconciled", "failed", "deferred", "repos", "installations"):
                    if k in result:
                        fields[k] = result[k]
            db("SELECT core.mark_boot_reconcile_run_with_authority(%s::jsonb)", (_json.dumps(fields),))
        except Exception as e:
            print(f"boot reconcile timestamp stamp failed ({str(e)[:160]}) — "
                  f"throttle will re-run next boot", flush=True)
    elif min_interval_min > 0 and not force and retryable_cursor_failure:
        print(
            "boot reconcile throttle stamp skipped — durable route/cursor "
            "did not complete; the next worker boot may retry immediately",
            flush=True,
        )


def _boot_reconcile_throttled(db: Callable, gh: Any, cap: int, dsn: Optional[str],
                              min_interval_min: int, force: bool,
                              deadline_seconds: int = 0) -> None:
    """Fleet-wide single-flight wrapper for the complete throttle → reconcile → stamp pass.

    Rolling deploys briefly run two instances.  The persisted recent-run timestamp alone is a racy read-before-
    write hint, so both instances could start the same expensive sweep.  A dedicated session advisory lock covers
    the entire pass; `force` bypasses only the time throttle, never this singleton.  Production always supplies a
    DSN.  The dsn-less path is retained for pure unit/legacy callers that cannot open a held session.
    """
    if not dsn:
        return _boot_reconcile_throttled_once(
            db, gh, cap, dsn, min_interval_min, force, deadline_seconds)

    lock_conn = None
    try:
        import psycopg2
        lock_conn = psycopg2.connect(dsn)
        lock_conn.autocommit = True
        with lock_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtext('core.boot_reconcile'),hashtext('fleet'))")
            row = cur.fetchone()
            acquired = bool(row and row[0] is True)
    except Exception as exc:
        if lock_conn is not None:
            try:
                lock_conn.close()
            except Exception:
                pass
        print(f"boot reconcile singleflight unavailable ({type(exc).__name__}) — skipped", flush=True)
        return None
    if not acquired:
        lock_conn.close()
        print("boot reconcile SKIPPED — another instance owns the fleet sweep", flush=True)
        return None
    try:
        return _boot_reconcile_throttled_once(
            db, gh, cap, dsn, min_interval_min, force, deadline_seconds)
    finally:
        # Session close is the authoritative unlock, including an exception halfway through the sweep.
        lock_conn.close()


def _boot_reconcile_after_live_grace(delay_seconds: int, db: Callable, gh: Any, cap: int,
                                     dsn: Optional[str], min_interval_min: int, force: bool,
                                     deadline_seconds: int = 0) -> None:
    """Yield startup to HTTP/live durable work before the best-effort background sweep."""
    if delay_seconds > 0:
        threading.Event().wait(delay_seconds)
    _boot_reconcile_throttled(
        db, gh, cap, dsn, min_interval_min, force, deadline_seconds)


def wire_runtime(cfg: BootConfig) -> Runtime:
    """Stand up the runtime around the proven brain in live-first order:
      1. DURABLE WEBHOOK INBOX (root durability boundary, VERIPSA_DURABLE_INBOX=0 = ordinary-event kill switch):
         DeliveryStore persists before the 202; the processor is WRAPPED so the worker claims → processes →
         finishes that durable row. Repository deletion/deselection always keeps this boundary because its stable-
         ID and receive-order authority is the durable row itself.
      2. the worker-instance LIVENESS loop, synchronously registered before work can be accepted and independent
         from the recovery kill-switch.
      3. the in-process WORKER (the _FairQueue scheduler) fed FROM the store, processing each event over ONE
         connection under a per-repo advisory lock.
      4. BOOT RECOVERY loop (opt-out VERIPSA_DELIVERY_RECOVERY=0): re-submit persisted-but-unfinished deliveries.
      5. BOOT RECONCILE (background, opt-out VERIPSA_BOOT_RECONCILE=0), only after a live-start grace and under
         fleet-wide single-flight. It is a best-effort convergence sweep, never startup-priority work.
      6. GitHub failed-delivery recovery (App-JWT metadata scan; DB-backed, bounded, fixed-default).
      7. the proactive WATCHDOG (turns the pull-based /healthz signals into PUSH alerts), with the
         graph_freshness_all seam + the durable store injected so it samples freshness + the delivery backlog.
    Returns the (worker, store) the HTTP layer needs. The server-resident collaborators are resolved via the
    lazy `_server()` seam so a test's monkeypatch on any of them (or on boot_reconcile / make_db_processor /
    the routing keys / run_watchdog) is honored exactly as before the split."""
    # A stalled Render log drain must not acquire an implicit global worker
    # lock through print(..., flush=True). The installer is a no-op for local
    # terminals/files and replaces production pipe streams exactly once.
    install_nonblocking_stdio()
    gh, dsn, db = cfg.gh, cfg.dsn, cfg.db

    # BOOT-TIME SCHEMA↔RUNTIME CONTRACT — fail closed when Python is ahead of the DB schema. Query pg_catalog
    # for the exact (function, arity) + (table, column, type) the runtime depends on; on any
    # mismatch flip schema_contract._SCHEMA_HEALTHY=False so /healthz 503s. Render's deploy gate fails → previous
    # live commit stays serving. This runs BEFORE any background thread (boot-reconcile, recovery loop, watchdog)
    # is spawned so a violation cannot race a worker. The check is content-free (pg_catalog only) and a single
    # short-lived connection — cheap at boot, $0 per webhook. Kill switch: VERIPSA_SCHEMA_CONTRACT=0 → SKIP and log.
    _contract = check_schema_contract(dsn)
    _contract_check_error = bool(getattr(
        _contract,
        "check_error",
        any(getattr(v, "kind", None) == "check_error"
            for v in _contract.violations),
    ))
    if _contract_check_error:
        # A transport/deadline/permission failure did not prove immutable
        # schema drift.  Never latch that transient result into a live HTTP
        # process for Render's full health window: exit before any runtime
        # collaborator or listener is started so the platform can restart a
        # fresh bounded check.  The result contains only fixed phase/count/
        # timing metadata; exception messages (which can contain a DSN) are
        # deliberately absent.
        print(
            "schema contract: CHECK ERROR — restarting before HTTP bind "
            f"(phase={getattr(_contract, 'phase', 'unknown')},"
            f"checked={_contract.checked},"
            f"elapsed_ms={getattr(_contract, 'elapsed_ms', 0)})",
            flush=True,
        )
        raise SystemExit(
            "FATAL: transient schema contract check failure; "
            "restart required before HTTP bind"
        )

    # Only a completed assertion is stable process health state.  A genuine
    # mismatch remains intentionally bound as /healthz 503 so its exact,
    # content-free catalog differences are diagnosable during the failed
    # deploy.  Healthy and kill-switch results retain their prior behavior.
    set_boot_result(_contract)
    if _contract.skipped:
        print("schema contract: SKIPPED (VERIPSA_SCHEMA_CONTRACT=0 — emergency kill switch)", flush=True)
    elif _contract.healthy:
        print(f"schema contract: PASS (checked {_contract.checked} expectations: "
              f"functions+columns matched the live DB; "
              f"elapsed_ms={getattr(_contract, 'elapsed_ms', 0)})", flush=True)
    else:
        # LOUD refusal — name every violation in the log so an operator sees EXACTLY what is wrong (a missing
        # function, a wrong arity, a missing column). /healthz now 503s; do NOT raise here — let the boot finish
        # so the probe can serve its diagnostic body. Render restarts on a failing health check → the deploy is
        # rejected → previous live commit keeps serving. The class of "Python ahead of DB schema" deploys is now
        # mechanically impossible without the operator's deliberate kill-switch override.
        for v in _contract.violations:
            print(f"SCHEMA CONTRACT VIOLATION: {v.name} expected {v.expected}, got {v.actual} ({v.kind})", flush=True)
        print(f"schema contract: REFUSING TO START — {len(_contract.violations)} violation(s). "
              f"/healthz will return 503 until the live DB matches the contract. "
              f"Kill switch (use ONLY for an emergency forward-compat hotfix): VERIPSA_SCHEMA_CONTRACT=0.",
              flush=True)

    _start_inprocess_convergence = _should_start_inprocess_convergence()
    S = _server()

    # Legacy/local embedding may prepare (but does not yet start) the best-effort boot reconcile here. Production
    # web never prepares or starts it: the dedicated convergence worker owns the sweep. For an unset/non-web role,
    # live recovery below must accept work first; the thread starts after recovery and waits a bounded grace.
    #
    # PER-WAKE THROTTLE (perf follow-up, Round-2 — Render free-tier cold-start cap): the keep-warm pinger keeps
    # the service alive past Render's 15-min idle, so real cold starts are RARE — but boot_reconcile still
    # scans tenants × open PRs on every boot (rolling deploys, platform recycles, flapping restarts), and when
    # wakes happen close together the sweep re-runs even though the previous one finished minutes ago and the
    # LIVE webhook path has been the primary self-heal since. So we persist the last-run timestamp (a host-level
    # kv row written through core.mark_boot_reconcile_run_with_authority) and SKIP the next reconcile when the
    # previous one finished within VERIPSA_BOOT_RECONCILE_MIN_INTERVAL_MIN minutes (default 60). Behaviour-
    # preserving for the initial install / first-deploy case (no row → no skip — the reconcile always runs). The
    # kill switch VERIPSA_BOOT_RECONCILE_FORCE=1 disables the throttle (always run, e.g. to verify an emergency
    # restart actually reconciled). The check + the timestamp write run INSIDE the background thread so they
    # never block /healthz coming up (a DB hiccup at boot can't strand readiness).
    _boot_args = None
    _runtime_role = os.environ.get("VERIPSA_RUNTIME_ROLE", "").strip()
    _boot_reconcile_enabled = os.environ.get("VERIPSA_BOOT_RECONCILE", "1") != "0"
    # Production web owns only request ingress, durable delivery recovery, and keyed event execution. Even a
    # count-capped boot inventory may enumerate hundreds of repositories and replay thousands of open PRs through
    # the App-wide GitHub rate limit; running it in this process makes adding accounts degrade live response
    # despite graph clone/extract having moved out. The convergence-worker entrypoint starts the same fleet-wide
    # throttled sweep instead. An unset role preserves the explicit local/legacy embedding contract.
    if _boot_reconcile_enabled and _runtime_role != "web":
        _cap = env_int("VERIPSA_BOOT_RECONCILE_CAP", 200, min_value=1)
        _min_interval_min = env_int("VERIPSA_BOOT_RECONCILE_MIN_INTERVAL_MIN", 60, min_value=0)
        _force = os.environ.get("VERIPSA_BOOT_RECONCILE_FORCE", "0") == "1"
        # Default 120s covers the deploy health/canary window observed in production. 0 exists for deterministic
        # test harnesses and an explicit emergency override; normal operation should keep at least 60s.
        _start_delay_sec = env_int(
            "VERIPSA_BOOT_RECONCILE_START_DELAY_SEC", 120, min_value=0, max_value=900)
        # PER-WAKE WALL-CLOCK BUDGET (defense-in-depth on top of `cap` + the skip-if-recent throttle): bound the
        # TIME the sweep can spend, so a small number of huge repos / slow GitHub responses cannot drag the boot
        # self-heal past a webhook-quiet window. Production/local wiring always supplies a positive bound.
        # boot_reconcile breaks BETWEEN repos once elapsed >= budget; the remaining repos are reported `deferred`
        # and pick up their own next live webhook (the same safety-net contract `cap`-truncation already has).
        _deadline_sec = env_int(
            "VERIPSA_BOOT_RECONCILE_DEADLINE_SEC", 120, min_value=1, max_value=900)
        _boot_args = (
            _start_delay_sec, db, gh, _cap, dsn, _min_interval_min, _force, _deadline_sec)
    elif _boot_reconcile_enabled:
        print(
            "boot reconcile: delegated to dedicated convergence worker (runtime role=web)",
            flush=True,
        )

    # DURABLE WEBHOOK INBOX — the root durability boundary. do_POST PERSISTS a SANITIZED delivery to
    # core.webhook_delivery BEFORE the 202; the worker's processor is WRAPPED so it claims → processes → finishes
    # that durable row (release-only-on-PROCESSOR-failure for a bounded retry); start_recovery_loop re-submits
    # queued + stale-'processing' rows on boot. So a crash / Render 2-instance rolling deploy / OOM after the 202
    # can no longer LOSE the event (GitHub does NOT redeliver a 202'd delivery). The in-memory _FairQueue below
    # stays the in-PROCESS scheduler (cross-tenant fairness + push coalescing), now fed FROM the store. The
    # VERIPSA_DURABLE_INBOX=0 kill switch reverts ORDINARY events to the legacy memory-only path. It cannot disable
    # repository deletion/deselection persistence: those handlers authenticate stable identity and receive order
    # from the processing durable row, and GitHub does not automatically redeliver failed webhooks. The store is
    # therefore always constructed and the processor always understands keyed payloads; server_http chooses which
    # events to persist. The store talks to the DB on its own short-lived autocommit connections.
    _durable = os.environ.get("VERIPSA_DURABLE_INBOX", "1") != "0"
    store = S.DeliveryStore(dsn)
    _processor = store.wrap_processor(S.make_db_processor(dsn))
    print("durable inbox: " + (
        "ON (persist-before-202 + boot recovery)" if _durable else
        "OFF for ordinary ingress (offboarding + existing recovery remain durable)"), flush=True)

    # the worker processes each event over ONE connection under a per-repo advisory lock (pooled + safe even
    # across instances / scale-out — see make_db_processor). The connect-per-query `db` is for startup only.
    # FIXED-SMALL KEYED WORKER POOL: one slow account must not head-of-line block every other tenant. Same-repository
    # work stays strict FIFO; different repositories under one account may use two lanes, while account-wide
    # lifecycle/unknown-identity events remain barriers. This is intentionally capped at four rather than derived
    # from CPU/queue depth: graph extraction and DB work are resource-sensitive, so unbounded concurrency would
    # merely move the incident into memory/connection pressure. Direct EventQueue callers retain their historical
    # default of one; production uses three workers and always reserves one for a different account.
    _worker_count = env_int("VERIPSA_EVENT_WORKER_COUNT", 3, min_value=1, max_value=4)
    _requested_per_account_workers = env_int(
        "VERIPSA_EVENT_PER_ACCOUNT_WORKERS", 2, min_value=1, max_value=3)
    _per_account_workers = (
        1 if _worker_count == 1
        else min(_requested_per_account_workers, _worker_count - 1)
    )
    _worker_kwargs = dict(
        process=_processor,
        account_of=S._event_account_key,
        repo_of=S._event_repo,
        branch_from_ref=S._branch_from_ref,
    )
    # Preserve the long-standing monkeypatch seam for minimal test/embedding workers whose constructor predates
    # worker_count; the real production EventQueue always declares it and therefore always receives the value.
    try:
        _event_queue_signature = inspect.signature(S.EventQueue)
        _accepts_extra = any(
            p.kind == p.VAR_KEYWORD for p in _event_queue_signature.parameters.values())
        if "worker_count" in _event_queue_signature.parameters or _accepts_extra:
            _worker_kwargs["worker_count"] = _worker_count
        if "per_account_workers" in _event_queue_signature.parameters or _accepts_extra:
            _worker_kwargs["per_account_workers"] = _per_account_workers
    except (TypeError, ValueError):
        _worker_kwargs["worker_count"] = _worker_count
        _worker_kwargs["per_account_workers"] = _per_account_workers
    worker = S.EventQueue(None, gh, **_worker_kwargs)
    # LEASE AUTHORITY MUST EXIST BEFORE THE FIRST CLAIM. A rolling peer's
    # reaper treats an absent stable owner as dead, so registering after
    # worker.start() leaves a real double-execution window. This synchronous
    # first beat fails boot closed; the dedicated daemon remains active even
    # when VERIPSA_DELIVERY_RECOVERY=0.
    S.start_instance_liveness_loop(store)
    worker.start()
    print(f"webhook workers: {_worker_count} fixed keyed workers, "
          f"max {_per_account_workers} active per account", flush=True)

    # BOOT RECOVERY: re-submit deliveries that were persisted but never finished (a crash/deploy mid-flight, or a
    # row left 'queued' because a prior boot's in-memory queue was full). register_push=False inside the loop so a
    # replayed (possibly stale) push can't regress/mask a newer live push's coalescing. Background + idempotent
    # (claim is owned-row + attempt-bounded; finish clears the payload to {}), so it is safe to run every boot.
    if os.environ.get("VERIPSA_DELIVERY_RECOVERY", "1") != "0":
        S.start_recovery_loop(store, worker)

    # G4 POLICY-CHANGE REFRESH: when an owner tunes a policy knob, every canonical policy writer enqueues a
    # content-free refresh row for that tenant IN THE SAME TXN as the policy commit (core._enqueue_policy_refresh).
    # This background drainer claims those rows and re-derives the tenant's OPEN in-flight PRs under the NEW policy
    # (recompute main_impact_surface + re-post via the idempotent _post_refreshes) — all OUTSIDE any outbox txn, so
    # a stale posted Check no longer sits on an open PR until its author happens to push again. gh.for_account is
    # the live-install generation fence (a dead install is skipped, never posted to). The kill switch reverts to
    # the pre-G4 behavior (rows accumulate coalesced, drained on re-enable). Production intentionally leaves
    # VERIPSA_POLICY_REFRESH=1 on the role-aware web service: this new image skips by role, while rolling back to
    # a predecessor image (which does not know roles) immediately restores its legacy drainer. GEN-AGNOSTIC: a
    # tick against a DB that has not yet had the schema applied degrades to a logged no-op (the store swallows
    # undefined-table/function).
    if _start_inprocess_convergence:
        S.start_policy_refresh_loop(
            S.PolicyRefreshStore(dsn), gh, dsn,
            graph_refresh_strict=S.converge_main_graph_strict,
        )
    else:
        print("policy convergence: delegated to dedicated worker (runtime role=web)", flush=True)

    # BOOT CONVERGENCE LAST: live ingress and durable recovery now own startup priority. The grace lets the HTTP
    # listener bind and the exact-SHA deployment canary complete before any repo sweep can contend. The wrapper
    # then enforces one fleet-wide sweep across rolling instances.
    if _boot_args is not None:
        threading.Thread(target=_boot_reconcile_after_live_grace, args=_boot_args,
                         name="veripsa-boot-reconcile", daemon=True).start()

    # PROACTIVE ALERTING: a background watchdog turns the PULL-based /healthz signals into PUSH notifications.
    # Without it, a dead worker / queue backlog / failed-event spike / DB outage is SILENT until a customer
    # complains (Render's health check only restarts a fully-dead worker; it pages nobody and sees no backlog/
    # DB-outage). Fires a content-free alert to VERIPSA_ALERT_WEBHOOK_URL (Slack/Discord/generic) AND always
    # to stdout (so it works log-only with no webhook). Fail-open: alerting can never take the server down.
    # DURABLE ALERT BOARD (the PO's "an alert logged but ignored is meaningless"): persist every fire/resolve to the
    # owner-readable core.active_alert table so the live alert set is visible via core.active_alerts_with_authority()
    # even with NO outbound webhook configured (the App default — otherwise an active alert lived only in the Render
    # log + the in-memory sink). The persist callable runs on the SAME connect-per-query `db` runner the watchdog
    # uses (veripsa_app, which is GRANTed the raise/clear fns), content-free (the watchdog's fields are counts only).
    # FAIL-OPEN inside the sink (_persist_safe swallows any error), so a board write never takes alerting down.
    import json as _json
    def _persist_alert(action, key, level, message, fields):
        if action == "resolve":
            db("SELECT core.clear_active_alert_with_authority(%s)", (key,))
        else:
            db("SELECT core.raise_active_alert_with_authority(%s,%s,%s,%s::jsonb)",
               (key, level, message, _json.dumps(fields or {})))
    alert_sink = AlertSink(persist=_persist_alert)
    print(f"alerting: webhook {'configured' if alert_sink.configured() else 'NOT set (log-only — ALERT[ lines in stdout)'}"
          f"; durable board: core.active_alert (owner-readable via active_alerts_with_authority)", flush=True)

    # FAILED GITHUB DELIVERY RECOVERY: GitHub itself does not retry failed App webhooks.  This low-priority loop
    # lists App-level delivery METADATA only (never detail/body), records scan/retry authority durably, and issues
    # at most one ACK-ambiguous POST per precommitted candidate.  Fixed defaults by design: no secret, cron, or
    # operator knob can silently leave this correctness path dark.  Marketplace/Sponsors are not exposed by the
    # App delivery endpoint and remain explicitly out of scope.
    failed_delivery_store = FailedDeliveryRecoveryStore(dsn)
    start_failed_delivery_recovery(failed_delivery_store, gh, alert_sink)

    _wd_interval = _watchdog_interval_seconds()
    # INJECT the graph_freshness_all seam (it stays in server.py — also used by /freshz + self_heal_main_graph)
    # into the watchdog explicitly on the live path; health_watchdog otherwise resolves it lazily. ALSO inject the
    # durable `store` (audit P1) so the watchdog samples core.webhook_delivery depth each tick and alerts on a
    # DEAD-LETTERED ('failed') row or a non-draining persisted 'queued' backlog — the P0 durability boundary that
    # was previously unobserved. The store remains wired when ordinary persistence is killed so any mandatory
    # repository-offboarding row stays observable.
    threading.Thread(target=S.run_watchdog,
                     kwargs=dict(sink=alert_sink, worker=worker, db=db, interval=_wd_interval,
                                 gh=gh, freshness_fn=S.graph_freshness_all, store=store),
                     name="veripsa-watchdog", daemon=True).start()

    # SITE KEEP-WARM (ops stopgap, OFF unless VERIPSA_KEEPWARM_URL is set): the public site runs on a
    # tier that sleeps, and the pinger that covered it was a scheduled GitHub Actions workflow that has
    # been rejected at the billing gate on every run since 2026-07-19. This process is the only
    # continuously-running paid tier we have, so it is the only place a reliable sub-15-minute cadence
    # exists. Deliberately last and fully isolated: it starts nothing the event path depends on, and it
    # cannot fail the boot. See site_keepwarm.py for why this should be deleted, not kept.
    start_site_keepwarm()

    return Runtime(worker=worker, store=store, persist_all=_durable)
