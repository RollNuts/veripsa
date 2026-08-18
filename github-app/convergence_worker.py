#!/usr/bin/env python3
"""Foreground owner of durable policy/code-graph convergence work.

The webhook web service persists convergence requests and returns to live traffic. This process is the only
production runtime that drains those requests, so graph extraction CPU/memory and its longer wall budget cannot
starve the ack-fast webhook workers. One claim is executed at a time; SIGTERM stops new claims and lets the
current bounded turn finish before process exit.
"""
from __future__ import annotations

import os
import re
import secrets
import signal
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

try:
    from env_config import env_int
except ImportError:  # imported as a package
    from .env_config import env_int
try:
    from nonblocking_stdio import install_nonblocking_stdio as _install_nonblocking_stdio
except ImportError:  # imported as a package
    from .nonblocking_stdio import install_nonblocking_stdio as _install_nonblocking_stdio


_WORKER_ROLE = "convergence-worker"
_EX_CONFIG = 78
_LIVENESS_EXIT = 70
# This is a cross-generation database ABI, not an operator tuning ceiling.
# The claim functions clamp every caller (including an old rollback image) to
# 300 seconds so the observed 787-900 second dead lease cannot return.  Letting
# this process accept a larger value would be unsafe: it could still be alive
# after the database makes its lease reclaimable at 300 seconds.
_MAX_STALE_RECLAIM_SECONDS = 300
_STALE_RECLAIM_MARGIN_SECONDS = 30
_DEFAULT_TURN_HARD_SECONDS = 270
_DEFAULT_HEARTBEAT_SECONDS = 30
_DEFAULT_POLL_FAILURE_HARD_SECONDS = 120
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_DRAIN_COUNTERS = (
    "drained",
    "graph_drained",
    "policy_drained",
    "policy_sliced",
    "change_sliced",
    "superseded",
    "quota_deferred",
    "tail_rearmed",
    "skipped_dead",
    "failed",
)


class WorkerConfigurationError(RuntimeError):
    """A required worker setting is absent or contradicts the isolated-worker role."""


class _TurnWallAlarm:
    """Process-wide real-time backstop for one foreground convergence turn.

    The thread monitor below is useful while Python threads are scheduled, but
    it cannot run while the whole process is stopped (for example by
    ``SIGSTOP``). ``ITIMER_REAL`` keeps advancing across that suspension. Its
    pending ``SIGALRM`` is delivered when the process resumes, and the handler
    uses ``os._exit`` so no stale worker bytecode or remote write can continue
    after the lease-safe turn wall.

    ``signal_api`` and ``exit_process`` are injectable only to exercise the
    safety boundary without terminating the test process.
    """

    def __init__(
            self, hard_seconds: int, *, boot_id: str = "", git_sha: str = "",
            signal_api=signal, exit_process=os._exit, write_stderr=os.write):
        self._hard_seconds = int(hard_seconds)
        self._signal = signal_api
        self._exit_process = exit_process
        self._write_stderr = write_stderr
        self._installed = False
        self._armed = False
        self._previous_handler = None
        self._failure_message = (
            "CONVERGENCE TURN ALARM FAILED: "
            f"hard_seconds={self._hard_seconds}; "
            "forcing container replacement "
            f"{_marker_context(boot_id, git_sha)}\n"
        ).encode("utf-8", errors="replace")

    def _handle_alarm(self, _signum, _frame) -> None:
        # Logging must never delay or defeat the terminal safety action.
        try:
            self._write_stderr(2, self._failure_message)
        except Exception:
            pass
        self._exit_process(_LIVENESS_EXIT)

    def install(self) -> None:
        if self._installed:
            return
        if self._hard_seconds <= 0:
            raise WorkerConfigurationError(
                "convergence turn wall alarm requires a positive hard bound")
        if not all(
                hasattr(self._signal, name)
                for name in ("SIGALRM", "ITIMER_REAL", "signal", "setitimer")):
            raise WorkerConfigurationError(
                "convergence worker requires SIGALRM/ITIMER_REAL support")
        try:
            self._previous_handler = self._signal.signal(
                self._signal.SIGALRM, self._handle_alarm)
        except (AttributeError, OSError, RuntimeError, ValueError) as exc:
            raise WorkerConfigurationError(
                "convergence worker could not install its process-wide "
                f"turn alarm ({type(exc).__name__})") from None
        self._installed = True

    def arm(self) -> None:
        if not self._installed:
            raise WorkerConfigurationError(
                "convergence turn wall alarm was not installed")
        if self._armed:
            raise WorkerConfigurationError(
                "convergence turn wall alarm was already armed")
        try:
            previous = self._signal.setitimer(
                self._signal.ITIMER_REAL, float(self._hard_seconds), 0.0)
        except (AttributeError, OSError, RuntimeError, ValueError) as exc:
            raise WorkerConfigurationError(
                "convergence worker could not arm its process-wide "
                f"turn alarm ({type(exc).__name__})") from None
        # This dedicated worker owns ITIMER_REAL. Refuse to silently displace a
        # timer installed by unexpected process composition.
        if (
                isinstance(previous, tuple)
                and len(previous) >= 1
                and float(previous[0]) > 0.0):
            try:
                self._signal.setitimer(
                    self._signal.ITIMER_REAL,
                    float(previous[0]),
                    float(previous[1]) if len(previous) > 1 else 0.0,
                )
            finally:
                raise WorkerConfigurationError(
                    "convergence worker found an unexpected active ITIMER_REAL")
        self._armed = True

    def disarm(self) -> None:
        if not self._armed:
            return
        try:
            self._signal.setitimer(self._signal.ITIMER_REAL, 0.0, 0.0)
        finally:
            self._armed = False

    @contextmanager
    def guard_turn(self):
        """Arm immediately before one turn and always disarm on unwind."""
        self.arm()
        try:
            yield
        finally:
            self.disarm()

    def close(self) -> None:
        """Remove both the outstanding timer and this process handler."""
        if not self._installed:
            return
        try:
            self.disarm()
        finally:
            try:
                self._signal.signal(
                    self._signal.SIGALRM, self._previous_handler)
            finally:
                self._installed = False


class _TurnLiveness:
    """Content-free monotonic age of the one foreground drain turn."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._started_at: float | None = None

    def begin(self) -> None:
        with self._lock:
            self._started_at = float(self._clock())

    def finish(self) -> None:
        with self._lock:
            self._started_at = None

    def age_seconds(self) -> float | None:
        with self._lock:
            started = self._started_at
        return None if started is None else max(0.0, float(self._clock()) - started)


class _PollHealth:
    """Thread-safe completed-turn proof shared with the heartbeat.

    A successful aggregate ``depth()`` query does not prove that the
    foreground claim/drain path can make progress.  Every process boot starts
    unverified.  Only a validated, completed drain turn verifies the path and
    advances ``turn_seq``; a failed turn disarms heartbeat emission until a
    later completed turn re-arms it.
    """

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._verified = False
        self._failed_at: float | None = None
        self._turn_seq = 0

    def fail(self) -> None:
        with self._lock:
            self._verified = False
            if self._failed_at is None:
                self._failed_at = float(self._clock())

    def complete_turn(self) -> tuple[bool, int]:
        """Record one valid foreground completion and return recovery/sequence."""
        with self._lock:
            recovered = self._failed_at is not None
            self._turn_seq += 1
            self._verified = True
            self._failed_at = None
            return recovered, self._turn_seq

    def snapshot(self) -> tuple[bool, float, int]:
        with self._lock:
            verified = self._verified
            failed_at = self._failed_at
            turn_seq = self._turn_seq
        failed_age = (
            0.0
            if failed_at is None
            else max(0.0, float(self._clock()) - failed_at)
        )
        return verified, failed_age, turn_seq


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise WorkerConfigurationError(f"{name} is required")
    return value


def _private_key() -> str:
    value = _required_env("GH_PRIVATE_KEY")
    candidate = Path(value)
    try:
        is_file = candidate.is_file()
    except OSError:
        is_file = False
    if is_file:
        try:
            value = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkerConfigurationError(
                f"GH_PRIVATE_KEY file is unreadable ({type(exc).__name__})") from None
    if not value.strip():
        raise WorkerConfigurationError("GH_PRIVATE_KEY is empty")
    return value


def _runtime_identity() -> tuple[str, str]:
    """Return one process boot nonce and the exact deployed Git commit.

    Render's instance id can survive long enough for logs from more than one
    process/deploy to be queryable together.  Every release-readiness marker
    therefore carries both a random per-process boot id and the immutable
    commit SHA.  Production refuses an unbound marker instead of allowing a
    predecessor process to authorize the web cutover.
    """
    raw_sha = (
        os.environ.get("RENDER_GIT_COMMIT", "")
        or os.environ.get("VERIPSA_BUILD_SHA", "")
    ).strip().lower()
    if _FULL_SHA.fullmatch(raw_sha) is None:
        raw_sha = (
            "invalid"
            if os.environ.get("RENDER", "").strip().lower() == "true"
            else "0" * 40
        )
    return secrets.token_hex(16), raw_sha


def _marker_context(boot_id: str, git_sha: str) -> str:
    return f"boot={boot_id} sha={git_sha}"


def _load_runtime_dependencies():
    """Load the heavy runtime only after the foreground entrypoint has been selected.

    Keeping these imports behind one seam makes startup behavior testable without a DB/GitHub connection and
    avoids importing the webhook composition root merely to parse this module.
    """
    try:
        from github_rest import GitHubREST
        from ingest import converge_main_graph_strict, graph_extraction_liveness
        from policy_refresh_queue import PolicyRefreshStore, _drain_policy_refreshes
        from schema_contract import check_schema_contract
        from server_boot import _boot_reconcile_throttled
        from server_dbops import _make_db
    except ImportError:  # imported as a package
        from .github_rest import GitHubREST
        from .ingest import converge_main_graph_strict, graph_extraction_liveness
        from .policy_refresh_queue import PolicyRefreshStore, _drain_policy_refreshes
        from .schema_contract import check_schema_contract
        from .server_boot import _boot_reconcile_throttled
        from .server_dbops import _make_db
    return SimpleNamespace(
        GitHubREST=GitHubREST,
        PolicyRefreshStore=PolicyRefreshStore,
        drain=_drain_policy_refreshes,
        graph_refresh_strict=converge_main_graph_strict,
        graph_extraction_liveness=graph_extraction_liveness,
        check_schema_contract=check_schema_contract,
        boot_reconcile_throttled=_boot_reconcile_throttled,
        make_db=_make_db,
    )


def _lower_process_priority() -> None:
    """Best-effort CPU priority reduction; isolation must not depend on host nice support."""
    adjustment = env_int("VERIPSA_CONVERGENCE_NICE", 10, min_value=0, max_value=19)
    try:
        effective = os.nice(adjustment)
        print(f"convergence worker priority: nice={effective}", flush=True)
    except (AttributeError, OSError) as exc:
        print(
            f"convergence worker priority: unchanged ({type(exc).__name__}; resource isolation still active)",
            flush=True,
        )


def _summary_has_activity(summary: object) -> bool:
    if not isinstance(summary, dict) or not summary:
        raise RuntimeError("convergence drain returned no structured counters")
    for key in _DRAIN_COUNTERS:
        value = summary.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(
                f"convergence drain counter {key!r} is missing or malformed")
    return any(int(summary[key]) > 0 for key in _DRAIN_COUNTERS)


def _accept_drain_summary(
        summary: object, poll_health: _PollHealth) -> tuple[bool, bool, int]:
    """Validate and sequence a completed foreground call before re-arming."""
    active = _summary_has_activity(summary)
    recovered, turn_seq = poll_health.complete_turn()
    return active, recovered, turn_seq


def _start_graph_liveness_monitor(
        stop: threading.Event, liveness, *, hard_seconds: int, interval_seconds: int,
        boot_id: str = "", git_sha: str = "", exit_process=os._exit) -> threading.Thread:
    """Replace this container if the extractor slot/janitor crosses its absolute process bound.

    Render does not provide HTTP health probes for background workers. A live-but-wedged PID would therefore
    retain the singleton graph slot forever unless the process self-reports terminal liveness. The monitor reads
    only the content-free in-process health snapshot and uses os._exit so a deadlocked foreground cleanup cannot
    intercept or postpone replacement.
    """
    def _monitor() -> None:
        while not stop.is_set():
            try:
                snapshot = liveness(float(hard_seconds))
            except Exception as exc:
                print(
                    f"convergence liveness sample unavailable ({type(exc).__name__})",
                    flush=True,
                )
            else:
                if isinstance(snapshot, dict) and snapshot.get("healthy") is False:
                    print(
                        "CONVERGENCE LIVENESS FAILED: "
                        f"stuck={bool(snapshot.get('stuck'))} "
                        f"locked={bool(snapshot.get('locked'))} "
                        f"background_owned={bool(snapshot.get('background_owned'))} "
                        f"hard_seconds={snapshot.get('hard_seconds')}; "
                        "forcing container replacement "
                        f"{_marker_context(boot_id, git_sha)}",
                        flush=True,
                    )
                    exit_process(_LIVENESS_EXIT)
                    return
            if stop.wait(interval_seconds):
                return

    monitor = threading.Thread(
        target=_monitor,
        name="veripsa-convergence-liveness",
        daemon=True,
    )
    monitor.start()
    return monitor


def _start_turn_liveness_monitor(
        stop: threading.Event, turn: _TurnLiveness, *, hard_seconds: int,
        interval_seconds: int, boot_id: str = "", git_sha: str = "",
        exit_process=os._exit) -> threading.Thread:
    """Replace a PID whose complete convergence turn crosses its lease-safe wall.

    Extractor health alone cannot see a hang in account routing, GitHub metadata, planner refresh, or Check
    posting. The durable row remains claimed and exact-epoch fenced; a forced exit before the stale-reclaim wall
    lets Render replace the process and the next owner safely resume instead of reproducing a 787-second green
    stall.
    """
    def _monitor() -> None:
        while not stop.is_set():
            age = turn.age_seconds()
            if isinstance(age, (int, float)) and age >= float(hard_seconds):
                print(
                    "CONVERGENCE TURN FAILED: "
                    f"age_seconds={int(age)} hard_seconds={int(hard_seconds)}; "
                    "forcing container replacement "
                    f"{_marker_context(boot_id, git_sha)}",
                    flush=True,
                )
                exit_process(_LIVENESS_EXIT)
                return
            if stop.wait(interval_seconds):
                return

    monitor = threading.Thread(
        target=_monitor,
        name="veripsa-convergence-turn-liveness",
        daemon=True,
    )
    monitor.start()
    return monitor


def _start_scheduler_heartbeat_monitor(
        stop: threading.Event, store, poll_health: _PollHealth, *,
        boot_id: str, git_sha: str,
        interval_seconds: int) -> threading.Thread:
    """Emit fresh, target-bound proof of DB access and foreground progress.

    This monitor uses ``depth()``, which owns a short fresh connection.  The
    slow graph phase closes its DB connection before GitHub/tarball/extractor
    work, so the probe neither shares a transaction nor waits for a claimed
    repository turn. A DB probe is not sufficient evidence by itself: every
    boot starts unverified, and after a foreground ``drain`` failure healthy
    heartbeats remain disarmed until a later real drain succeeds. Every marker
    carries the monotonic completed-turn sequence so a release gate can reject
    repeated depth-only heartbeats. Persistent poll failure replaces the
    process before a 787-second silent stall can recur.
    """
    context = _marker_context(boot_id, git_sha)

    def _monitor() -> None:
        while not stop.wait(interval_seconds):
            try:
                snapshot = store.depth()
                if not isinstance(snapshot, dict) or not snapshot:
                    raise RuntimeError("scheduler probe returned no evidence")
            except Exception as exc:
                print(
                    "convergence worker: scheduler heartbeat failed "
                    f"({type(exc).__name__}) {context}",
                    flush=True,
                )
            else:
                poll_healthy, _failed_age, turn_seq = poll_health.snapshot()
                if not poll_healthy or turn_seq < 1:
                    # Do not let a successful aggregate query conceal a broken
                    # claim/drain path. In particular, no scheduler=ok marker
                    # is emitted before the first completed foreground turn or
                    # while recovery is still pending.
                    continue
                print(
                    "convergence worker: HEARTBEAT scheduler=ok "
                    f"turn_seq={turn_seq} {context}",
                    flush=True,
                )

    monitor = threading.Thread(
        target=_monitor,
        name="veripsa-convergence-heartbeat",
        daemon=True,
    )
    monitor.start()
    return monitor


def _start_poll_failure_monitor(
        stop: threading.Event, poll_health: _PollHealth, *,
        hard_seconds: int, interval_seconds: int,
        boot_id: str = "", git_sha: str = "",
        exit_process=os._exit) -> threading.Thread:
    """Replace a worker whose real foreground drain cannot recover.

    This is independent of the 30-second scheduler heartbeat and its DB
    ``depth()`` call, so the configured 120-second wall is enforced within the
    five-second liveness sampling interval even when that aggregate query is
    itself slow.
    """
    context = _marker_context(boot_id, git_sha)

    def _monitor() -> None:
        while not stop.wait(interval_seconds):
            poll_healthy, failed_age, _turn_seq = poll_health.snapshot()
            if poll_healthy:
                continue
            if failed_age >= float(hard_seconds):
                print(
                    "CONVERGENCE POLL FAILED: "
                    f"age_seconds={int(failed_age)} "
                    f"hard_seconds={int(hard_seconds)}; "
                    f"forcing container replacement {context}",
                    flush=True,
                )
                exit_process(_LIVENESS_EXIT)
                return

    monitor = threading.Thread(
        target=_monitor,
        name="veripsa-convergence-poll-liveness",
        daemon=True,
    )
    monitor.start()
    return monitor


def _start_boot_reconcile(
        stop: threading.Event, dependencies, dsn: str, gh, *,
        delay_seconds: int, cap: int, min_interval_min: int,
        force: bool, deadline_seconds: int) -> threading.Thread | None:
    """Run deployment inventory/replay only on the isolated worker service.

    The existing database advisory lock and persisted throttle select at most
    one process across the two Render instances. The sweep is count- and
    wall-bounded and never blocks the foreground globally-fair claim loop;
    therefore boot repair cannot consume the web process, HTTP/GitHub ingress
    threads, or both convergence worker turns.
    """
    if os.environ.get("VERIPSA_BOOT_RECONCILE", "1") == "0":
        return None
    reconcile = getattr(dependencies, "boot_reconcile_throttled", None)
    make_db = getattr(dependencies, "make_db", None)
    if not callable(reconcile) or not callable(make_db):
        raise WorkerConfigurationError(
            "convergence runtime lacks the isolated boot-reconcile dependency")

    def _run() -> None:
        if stop.wait(delay_seconds):
            return
        try:
            reconcile(
                make_db(dsn),
                gh,
                cap,
                dsn,
                min_interval_min,
                force,
                deadline_seconds,
            )
        except Exception as exc:
            print(
                "convergence worker: boot reconcile failed "
                f"({type(exc).__name__}); durable live/outbox recovery remains active",
                flush=True,
            )

    thread = threading.Thread(
        target=_run,
        name="veripsa-worker-boot-reconcile",
        daemon=True,
    )
    thread.start()
    return thread


def run(stop: threading.Event, *, dependencies=None) -> int:
    """Validate, construct, and continuously drain until `stop` is set.

    The schema assertion is stricter than the web emergency surface: a skipped contract is a startup refusal for
    this writer. Running a graph writer against an unverified schema would turn a safe backlog into corrupt or
    silently-unfinishable work.
    """
    # Install the lightweight non-blocking pipe writer before the first log
    # record. The remaining runtime imports are intentionally delayed until
    # after STARTING fences any predecessor process generation.
    if dependencies is None:
        _install_nonblocking_stdio()
    else:
        dependencies.install_nonblocking_stdio()
    boot_id, git_sha = _runtime_identity()
    marker_context = _marker_context(boot_id, git_sha)
    # This is deliberately the first runtime marker. Render can keep an
    # instance label queryable across a process restart; STARTING changes the
    # authoritative boot before any role/schema/secret check so a fresh
    # failure can never inherit the predecessor boot's heartbeat.
    print(f"convergence worker: STARTING {marker_context}", flush=True)
    if git_sha == "invalid":
        raise WorkerConfigurationError(
            "RENDER_GIT_COMMIT must be an exact 40-character commit SHA")
    # Dependency/import failure is also a process-generation failure. Load the
    # runtime only after STARTING has fenced any predecessor heartbeat.
    dependencies = dependencies or _load_runtime_dependencies()

    role = os.environ.get("VERIPSA_RUNTIME_ROLE", _WORKER_ROLE)
    if role != _WORKER_ROLE:
        raise WorkerConfigurationError(
            f"VERIPSA_RUNTIME_ROLE must be {_WORKER_ROLE!r} for this entrypoint")
    # The service-level secret is needed by Render's separate preDeploy
    # command, never by a tenant-scoped worker.  The configured dockerCommand
    # removes it before exec (including from /proc/<pid>/environ); fail closed
    # if an operator or platform override bypasses that command boundary.
    if os.environ.get("OWNER_DSN"):
        raise WorkerConfigurationError(
            "OWNER_DSN is preDeploy-only and must not exist in the convergence runtime environment"
        )
    if os.environ.get("VERIPSA_POLICY_REFRESH", "1") == "0":
        print(
            "convergence worker: PAUSED by VERIPSA_POLICY_REFRESH=0; no durable turn will be claimed",
            flush=True,
        )
        stop.wait()
        print("convergence worker: STOPPED cleanly while paused", flush=True)
        return 0

    dsn = _required_env("VERIPSA_DSN")
    app_id = _required_env("GH_APP_ID")

    contract = dependencies.check_schema_contract(dsn)
    if contract.skipped or not contract.healthy:
        violations = tuple(getattr(contract, "violations", ()) or ())
        for violation in violations:
            print(
                "SCHEMA CONTRACT VIOLATION: "
                f"{getattr(violation, 'name', 'unknown')} "
                f"({getattr(violation, 'kind', 'unknown')}) "
                f"{marker_context}",
                flush=True,
            )
        state = "SKIPPED" if contract.skipped else "FAILED"
        print(
            f"convergence worker: schema contract {state}; "
            f"refusing to claim durable work {marker_context}",
            flush=True,
        )
        return _EX_CONFIG
    print(
        f"convergence worker: schema contract PASS ({int(contract.checked)} expectations)",
        flush=True,
    )

    private_key = _private_key()
    installation_id = os.environ.get("GH_INSTALLATION_ID", "")
    gh = dependencies.GitHubREST(app_id, private_key, installation_id)

    interval = env_int(
        "VERIPSA_POLICY_REFRESH_INTERVAL", 15, min_value=1, max_value=3600)
    burst_limit = env_int(
        "VERIPSA_POLICY_REFRESH_DRAIN_LIMIT", 50, min_value=1, max_value=1000)
    coord_cap = env_int(
        "VERIPSA_POLICY_REFRESH_COORD_CAP", 200, min_value=1, max_value=100000)
    liveness_hard = env_int(
        "VERIPSA_CONVERGENCE_LIVENESS_HARD_SECONDS", 240, min_value=30, max_value=3600)
    liveness_interval = env_int(
        "VERIPSA_CONVERGENCE_LIVENESS_INTERVAL_SECONDS", 5, min_value=1, max_value=60)
    heartbeat_interval = env_int(
        "VERIPSA_CONVERGENCE_HEARTBEAT_SECONDS",
        _DEFAULT_HEARTBEAT_SECONDS,
        min_value=5,
        max_value=60,
    )
    boot_reconcile_delay = env_int(
        "VERIPSA_BOOT_RECONCILE_START_DELAY_SEC", 120,
        min_value=0, max_value=900)
    boot_reconcile_cap = env_int(
        "VERIPSA_BOOT_RECONCILE_CAP", 200, min_value=1, max_value=1000)
    boot_reconcile_interval = env_int(
        "VERIPSA_BOOT_RECONCILE_MIN_INTERVAL_MIN", 60,
        min_value=0, max_value=1440)
    boot_reconcile_deadline = env_int(
        "VERIPSA_BOOT_RECONCILE_DEADLINE_SEC", 120,
        min_value=1, max_value=900)
    boot_reconcile_force = (
        os.environ.get("VERIPSA_BOOT_RECONCILE_FORCE", "0") == "1")
    poll_failure_hard = env_int(
        "VERIPSA_CONVERGENCE_POLL_FAILURE_HARD_SECONDS",
        _DEFAULT_POLL_FAILURE_HARD_SECONDS,
        min_value=30,
        max_value=_DEFAULT_TURN_HARD_SECONDS,
    )
    graph_timeout = env_int(
        "VERIPSA_GRAPH_EXTRACT_TIMEOUT_SECONDS", 60, min_value=1, max_value=900)
    turn_hard = env_int(
        "VERIPSA_CONVERGENCE_TURN_HARD_SECONDS",
        _DEFAULT_TURN_HARD_SECONDS,
        min_value=60,
        max_value=3600,
    )
    if turn_hard < liveness_hard:
        raise WorkerConfigurationError(
            "VERIPSA_CONVERGENCE_TURN_HARD_SECONDS must be >= extractor liveness wall")
    if poll_failure_hard > turn_hard:
        raise WorkerConfigurationError(
            "VERIPSA_CONVERGENCE_POLL_FAILURE_HARD_SECONDS must be <= complete turn wall")
    minimum_stale = max(graph_timeout, liveness_hard, turn_hard) + _STALE_RECLAIM_MARGIN_SECONDS
    if minimum_stale > _MAX_STALE_RECLAIM_SECONDS:
        raise WorkerConfigurationError(
            "graph/liveness wall is too large for the bounded convergence reclaim contract")
    stale_seconds = env_int(
        "VERIPSA_POLICY_REFRESH_STALE_SECONDS", 300,
        min_value=minimum_stale, max_value=_MAX_STALE_RECLAIM_SECONDS)
    # env_int trusts an unset default. Validate the cross-knob relation explicitly too, so raising the extractor
    # wall cannot silently make a live turn eligible for duplicate reclaim or restore a 787-second dead lease.
    if not minimum_stale <= stale_seconds <= _MAX_STALE_RECLAIM_SECONDS:
        raise WorkerConfigurationError(
            "VERIPSA_POLICY_REFRESH_STALE_SECONDS must exceed the graph/liveness wall by "
            f"{_STALE_RECLAIM_MARGIN_SECONDS}s and remain <= "
            f"{_MAX_STALE_RECLAIM_SECONDS}s")
    store = dependencies.PolicyRefreshStore(dsn, stale_seconds=stale_seconds)
    scheduler_probe = store.depth()
    if not isinstance(scheduler_probe, dict) or not scheduler_probe:
        raise WorkerConfigurationError(
            "account convergence scheduler probe returned no structured evidence")
    _lower_process_priority()
    monitor_stop = threading.Event()
    monitor = _start_graph_liveness_monitor(
        monitor_stop,
        dependencies.graph_extraction_liveness,
        hard_seconds=liveness_hard,
        interval_seconds=liveness_interval,
        boot_id=boot_id,
        git_sha=git_sha,
    )
    turn_liveness = _TurnLiveness()
    turn_monitor = _start_turn_liveness_monitor(
        monitor_stop,
        turn_liveness,
        hard_seconds=turn_hard,
        interval_seconds=liveness_interval,
        boot_id=boot_id,
        git_sha=git_sha,
    )
    poll_health = _PollHealth()
    poll_monitor = _start_poll_failure_monitor(
        monitor_stop,
        poll_health,
        hard_seconds=poll_failure_hard,
        interval_seconds=liveness_interval,
        boot_id=boot_id,
        git_sha=git_sha,
    )
    print(
        "convergence worker: READY "
        f"(serial graph turns, burst={burst_limit}, interval={interval}s, "
        f"liveness_hard={liveness_hard}s, turn_hard={turn_hard}s, "
        f"stale_reclaim={stale_seconds}s) {marker_context}",
        flush=True,
    )
    # This depth-only startup marker is diagnostic, never release evidence.
    # The heartbeat remains disarmed until the foreground loop completes and
    # validates its first real drain turn.
    print(
        "convergence worker: POLL_READY "
        "(scheduler query completed; foreground turn unverified) "
        f"{marker_context}",
        flush=True,
    )
    heartbeat_monitor = _start_scheduler_heartbeat_monitor(
        monitor_stop,
        store,
        poll_health,
        boot_id=boot_id,
        git_sha=git_sha,
        interval_seconds=heartbeat_interval,
    )
    boot_reconcile_thread = _start_boot_reconcile(
        stop,
        dependencies,
        dsn,
        gh,
        delay_seconds=boot_reconcile_delay,
        cap=boot_reconcile_cap,
        min_interval_min=boot_reconcile_interval,
        force=boot_reconcile_force,
        deadline_seconds=boot_reconcile_deadline,
    )
    # Keep the content-free thread monitor above as an observable first line
    # of defence, and add a process-wide real-time deadline for SIGSTOP and
    # scheduler-starvation cases in which that monitor cannot run.
    turn_wall_alarm = _TurnWallAlarm(
        turn_hard, boot_id=boot_id, git_sha=git_sha)

    try:
        # Signal handlers can only be installed by Python's main thread. The
        # production entrypoint is deliberately foreground-owned; fail closed
        # before claiming anything if process composition violates that
        # contract.
        turn_wall_alarm.install()
        while not stop.is_set():
            attempted = False
            for _ in range(burst_limit):
                if stop.is_set():
                    break
                try:
                    # One claim per call is load-bearing for graceful shutdown: after SIGTERM no second
                    # repository can begin. ITIMER_REAL is armed before any
                    # turn bytecode and remains live across SIGSTOP; the
                    # extractor and complete-turn thread monitors remain
                    # active as independently observable backstops.
                    with turn_wall_alarm.guard_turn():
                        turn_liveness.begin()
                        try:
                            summary = dependencies.drain(
                                store,
                                gh,
                                dsn,
                                limit=1,
                                coord_cap=coord_cap,
                                graph_refresh_strict=dependencies.graph_refresh_strict,
                            )
                            # Validate the foreground ABI before re-arming health.
                            # A malformed/empty response is a poll failure, not an
                            # idle successful turn.
                            active, recovered, turn_seq = _accept_drain_summary(
                                summary, poll_health)
                            if recovered:
                                print(
                                    "convergence worker: POLL_RECOVERED "
                                    "scheduler=ok "
                                    f"turn_seq={turn_seq} {marker_context}",
                                    flush=True,
                                )
                        finally:
                            turn_liveness.finish()
                    attempted = attempted or active
                    if not active:
                        break
                except WorkerConfigurationError:
                    # Losing the process-wide deadline is a safety-boundary
                    # failure, not durable work eligible for an interval retry.
                    raise
                except Exception as exc:
                    # The outbox owns retry state. Keep the foreground process alive, log only a content-free
                    # class, and wait before claiming again instead of creating a crash/restart hot loop.
                    poll_health.fail()
                    print(
                        "convergence worker tick failed "
                        f"({type(exc).__name__}); retrying after interval "
                        f"{marker_context}",
                        flush=True,
                    )
                    break
            if stop.is_set():
                break
            if attempted:
                print("convergence worker: burst complete", flush=True)
            stop.wait(interval)
    finally:
        turn_wall_alarm.close()
        monitor_stop.set()
        monitor.join(timeout=max(1.0, min(5.0, float(liveness_interval) + 0.5)))
        turn_monitor.join(timeout=max(1.0, min(5.0, float(liveness_interval) + 0.5)))
        poll_monitor.join(timeout=max(1.0, min(5.0, float(liveness_interval) + 0.5)))
        heartbeat_monitor.join(
            timeout=max(1.0, min(5.0, float(heartbeat_interval) + 0.5)))
        if boot_reconcile_thread is not None:
            boot_reconcile_thread.join(timeout=1.0)

    print("convergence worker: STOPPED cleanly; no new durable turn claimed", flush=True)
    return 0


def _install_signal_handlers(stop: threading.Event) -> None:
    def _request_stop(signum, _frame):
        if not stop.is_set():
            print(
                f"convergence worker: signal {int(signum)} received; finishing current bounded turn",
                flush=True,
            )
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)


def main() -> int:
    stop = threading.Event()
    _install_signal_handlers(stop)
    try:
        return run(stop)
    except WorkerConfigurationError as exc:
        print(f"convergence worker configuration refused: {exc}", flush=True)
        return _EX_CONFIG
    except Exception as exc:
        print(
            f"convergence worker startup failed ({type(exc).__name__}); refusing to claim work",
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
