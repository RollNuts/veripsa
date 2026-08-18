#!/usr/bin/env python3
"""Offline gate for Render web/graph resource isolation and worker shutdown/config contracts."""
from __future__ import annotations

import importlib.util
import io
import os
import re
import sys
import threading
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
WORKER_PATH = ROOT / "github-app" / "convergence_worker.py"
sys.path.insert(0, str(ROOT / "github-app"))
spec = importlib.util.spec_from_file_location("convergence_worker_under_test", WORKER_PATH)
CW = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(CW)
import server_boot as SB  # noqa: E402


fail = 0


def check(condition, message):
    global fail
    print(("  [PASS] " if condition else "  [FAIL] ") + message)
    if not condition:
        fail = 1


class FakeGitHub:
    calls = []

    def __init__(self, app_id, private_key, installation_id):
        self.calls.append((app_id, private_key, installation_id))


class FakeStore:
    calls = []
    depth_calls = 0

    def __init__(self, dsn, **kwargs):
        self.calls.append((dsn, kwargs))

    def depth(self):
        type(self).depth_calls += 1
        return {"pending": 0}


drain_calls = []
stop = threading.Event()
strict_callback = object()
turn_alarm_events = []


class FakeTurnWallAlarm:
    def __init__(self, hard_seconds, **_kwargs):
        turn_alarm_events.append(("init", hard_seconds))

    def install(self):
        turn_alarm_events.append("install")

    @contextmanager
    def guard_turn(self):
        turn_alarm_events.append("arm")
        try:
            yield
        finally:
            turn_alarm_events.append("disarm")

    def close(self):
        turn_alarm_events.append("close")


def fake_drain(store, gh, dsn, **kwargs):
    turn_alarm_events.append("drain")
    drain_calls.append((store, gh, dsn, kwargs))
    stop.set()
    return {
        "drained": 1,
        "graph_drained": 1,
        "policy_drained": 0,
        "policy_sliced": 0,
        "change_sliced": 0,
        "superseded": 0,
        "quota_deferred": 0,
        "tail_rearmed": 0,
        "skipped_dead": 0,
        "failed": 0,
    }


deps = SimpleNamespace(
    GitHubREST=FakeGitHub,
    PolicyRefreshStore=FakeStore,
    drain=fake_drain,
    graph_refresh_strict=strict_callback,
    graph_extraction_liveness=lambda hard_seconds: {"healthy": True},
    check_schema_contract=lambda dsn: SimpleNamespace(
        healthy=True, skipped=False, checked=7, violations=()),
    install_nonblocking_stdio=lambda: None,
    boot_reconcile_throttled=lambda *_a, **_k: None,
    make_db=lambda _dsn: object(),
)

names = (
    "VERIPSA_RUNTIME_ROLE",
    "VERIPSA_POLICY_REFRESH",
    "VERIPSA_DSN",
    "GH_APP_ID",
    "GH_PRIVATE_KEY",
    "GH_INSTALLATION_ID",
    "VERIPSA_POLICY_REFRESH_INTERVAL",
    "VERIPSA_POLICY_REFRESH_DRAIN_LIMIT",
    "VERIPSA_POLICY_REFRESH_COORD_CAP",
    "VERIPSA_CONVERGENCE_LIVENESS_HARD_SECONDS",
    "VERIPSA_CONVERGENCE_TURN_HARD_SECONDS",
    "VERIPSA_CONVERGENCE_LIVENESS_INTERVAL_SECONDS",
    "VERIPSA_CONVERGENCE_HEARTBEAT_SECONDS",
    "VERIPSA_CONVERGENCE_POLL_FAILURE_HARD_SECONDS",
    "VERIPSA_GRAPH_EXTRACT_TIMEOUT_SECONDS",
    "VERIPSA_POLICY_REFRESH_STALE_SECONDS",
    "VERIPSA_BOOT_RECONCILE",
    "VERIPSA_BOOT_RECONCILE_START_DELAY_SEC",
    "VERIPSA_BOOT_RECONCILE_CAP",
    "VERIPSA_BOOT_RECONCILE_MIN_INTERVAL_MIN",
    "VERIPSA_BOOT_RECONCILE_DEADLINE_SEC",
    "VERIPSA_BOOT_RECONCILE_FORCE",
    "OWNER_DSN",
    "RENDER",
    "RENDER_GIT_COMMIT",
)
saved = {name: os.environ.get(name) for name in names}
saved_nice = CW._lower_process_priority
saved_turn_wall_alarm = CW._TurnWallAlarm
run_output = io.StringIO()
try:
    os.environ.pop("OWNER_DSN", None)
    os.environ.update({
        "VERIPSA_RUNTIME_ROLE": "convergence-worker",
        "VERIPSA_POLICY_REFRESH": "1",
        "VERIPSA_DSN": "postgresql://example/veripsa",
        "GH_APP_ID": "123",
        "GH_PRIVATE_KEY": "test-private-key",
        "GH_INSTALLATION_ID": "",
        "VERIPSA_POLICY_REFRESH_INTERVAL": "1",
        "VERIPSA_POLICY_REFRESH_DRAIN_LIMIT": "9",
        "VERIPSA_POLICY_REFRESH_COORD_CAP": "17",
        "VERIPSA_CONVERGENCE_LIVENESS_HARD_SECONDS": "240",
        "VERIPSA_CONVERGENCE_TURN_HARD_SECONDS": "270",
        "VERIPSA_CONVERGENCE_LIVENESS_INTERVAL_SECONDS": "1",
        "VERIPSA_CONVERGENCE_HEARTBEAT_SECONDS": "5",
        "VERIPSA_CONVERGENCE_POLL_FAILURE_HARD_SECONDS": "120",
        "VERIPSA_GRAPH_EXTRACT_TIMEOUT_SECONDS": "180",
        "VERIPSA_POLICY_REFRESH_STALE_SECONDS": "300",
    })
    CW._lower_process_priority = lambda: None
    CW._TurnWallAlarm = FakeTurnWallAlarm
    with redirect_stdout(run_output):
        result = CW.run(stop, dependencies=deps)
finally:
    CW._lower_process_priority = saved_nice
    CW._TurnWallAlarm = saved_turn_wall_alarm
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

check(result == 0, "foreground worker exits cleanly after its stop Event is set")
check(FakeGitHub.calls == [("123", "test-private-key", "")],
      "worker constructs one GitHub App client without requiring a webhook secret")
check(
    FakeStore.calls == [
        ("postgresql://example/veripsa", {"stale_seconds": 300})
    ],
    "worker constructs the durable store with a 300s bounded abandoned-turn reclaim",
)
check(
    CW._MAX_STALE_RECLAIM_SECONDS == 300,
    "worker configuration cannot outlive the database's cross-generation 300s reclaim fence",
)
check(
    FakeStore.depth_calls == 1,
    "worker records one depth-only startup diagnostic before foreground verification",
)
check(
    "POLL_READY" in run_output.getvalue()
    and "foreground turn unverified" in run_output.getvalue()
    and "HEARTBEAT scheduler=ok" not in run_output.getvalue(),
    "startup depth/POLL_READY is explicitly unverified and cannot emit cutover evidence",
)
check(
    len(drain_calls) == 1
    and drain_calls[0][3]["limit"] == 1
    and drain_calls[0][3]["coord_cap"] == 17
    and drain_calls[0][3]["graph_refresh_strict"] is strict_callback,
    "each interruptible claim uses the strict graph callback and never starts a second turn after SIGTERM",
)
check(
    turn_alarm_events == [
        ("init", 270),
        "install",
        "arm",
        "drain",
        "disarm",
        "close",
    ],
    "every foreground drain is enclosed by the installed 270s process alarm and disarmed before shutdown",
)
boot_calls = []
boot_stop = threading.Event()
prior_boot_switch = os.environ.get("VERIPSA_BOOT_RECONCILE")
try:
    os.environ["VERIPSA_BOOT_RECONCILE"] = "1"
    boot_thread = CW._start_boot_reconcile(
        boot_stop,
        SimpleNamespace(
            make_db=lambda dsn: ("db", dsn),
            boot_reconcile_throttled=lambda *args: boot_calls.append(args),
        ),
        "postgresql://example/veripsa",
        "gh-client",
        delay_seconds=0,
        cap=23,
        min_interval_min=61,
        force=False,
        deadline_seconds=121,
    )
    boot_thread.join(timeout=2)
finally:
    if prior_boot_switch is None:
        os.environ.pop("VERIPSA_BOOT_RECONCILE", None)
    else:
        os.environ["VERIPSA_BOOT_RECONCILE"] = prior_boot_switch
check(
    boot_thread is not None
    and not boot_thread.is_alive()
    and boot_calls == [(
        ("db", "postgresql://example/veripsa"),
        "gh-client",
        23,
        "postgresql://example/veripsa",
        61,
        False,
        121,
    )],
    "boot inventory/replay is owned by the isolated convergence worker with a bounded wall",
)
wire_source = __import__("inspect").getsource(SB.wire_runtime)
check(
    '_runtime_role != "web"' in wire_source
    and "delegated to dedicated convergence worker" in wire_source,
    "role-aware web never starts repository inventory or open-PR boot replay",
)
malformed_summary_refused = False
malformed_health = CW._PollHealth(clock=lambda: 100.0)
malformed_health.fail()
try:
    CW._accept_drain_summary({}, malformed_health)
except RuntimeError:
    malformed_summary_refused = True
check(
    malformed_summary_refused
    and malformed_health.snapshot()[0] is False,
    "a missing/malformed drain counter ABI cannot re-arm or reset failed worker health",
)


def valid_idle_summary():
    return {
        "drained": 0,
        "graph_drained": 0,
        "policy_drained": 0,
        "policy_sliced": 0,
        "change_sliced": 0,
        "superseded": 0,
        "quota_deferred": 0,
        "tail_rearmed": 0,
        "skipped_dead": 0,
        "failed": 0,
    }


class RepeatedTickStop:
    def __init__(self, ticks):
        self.remaining = ticks

    def wait(self, _seconds):
        if self.remaining <= 0:
            return True
        self.remaining -= 1
        return False


blocked_health = CW._PollHealth()
blocked_started = threading.Event()
blocked_release = threading.Event()
blocked_completed = []


def blocking_drain(*, limit):
    check(limit == 1, "blocked-drain fixture preserves one-claim turn scope")
    blocked_started.set()
    blocked_release.wait(timeout=2)
    return valid_idle_summary()


def run_blocked_foreground():
    summary = blocking_drain(limit=1)
    blocked_completed.append(
        CW._accept_drain_summary(summary, blocked_health))


blocked_thread = threading.Thread(target=run_blocked_foreground)
blocked_thread.start()
check(blocked_started.wait(timeout=1), "blocked-drain fixture entered the foreground turn")
blocked_depth_calls = []
blocked_output = io.StringIO()
with redirect_stdout(blocked_output):
    blocked_heartbeat = CW._start_scheduler_heartbeat_monitor(
        RepeatedTickStop(4),
        SimpleNamespace(
            depth=lambda: blocked_depth_calls.append(1) or {"pending": 11}),
        blocked_health,
        boot_id="1" * 32,
        git_sha="a" * 40,
        interval_seconds=1,
    )
    blocked_heartbeat.join(timeout=1)
check(
    not blocked_heartbeat.is_alive()
    and len(blocked_depth_calls) == 4
    and "HEARTBEAT scheduler=ok" not in blocked_output.getvalue()
    and blocked_health.snapshot() == (False, 0.0, 0),
    "repeated successful depth intervals cannot publish readiness while the first drain is blocked",
)
blocked_release.set()
blocked_thread.join(timeout=1)
released_output = io.StringIO()
with redirect_stdout(released_output):
    released_heartbeat = CW._start_scheduler_heartbeat_monitor(
        RepeatedTickStop(1),
        SimpleNamespace(depth=lambda: {"pending": 11}),
        blocked_health,
        boot_id="1" * 32,
        git_sha="a" * 40,
        interval_seconds=1,
    )
    released_heartbeat.join(timeout=1)
check(
    not blocked_thread.is_alive()
    and blocked_completed == [(False, False, 1)]
    and "HEARTBEAT scheduler=ok turn_seq=1" in released_output.getvalue(),
    "only the completed valid drain arms a heartbeat with monotonic turn_seq=1",
)

signal_stop = threading.Event()
registered = {}
saved_signal = CW.signal.signal
try:
    CW.signal.signal = lambda number, handler: registered.__setitem__(number, handler)
    CW._install_signal_handlers(signal_stop)
    registered[CW.signal.SIGTERM](CW.signal.SIGTERM, None)
finally:
    CW.signal.signal = saved_signal
check(signal_stop.is_set() and CW.signal.SIGINT in registered,
      "SIGTERM requests a clean stop and SIGINT shares the same foreground shutdown path")

liveness_stop = threading.Event()
liveness_exit = []
liveness_hard = []
liveness_thread = CW._start_graph_liveness_monitor(
    liveness_stop,
    lambda hard: (
        liveness_hard.append(hard)
        or {"healthy": False, "stuck": True, "locked": True,
            "background_owned": True, "hard_seconds": 206.0}
    ),
    hard_seconds=180,
    interval_seconds=5,
    exit_process=lambda code: liveness_exit.append(code),
)
liveness_thread.join(timeout=1)
check(
    not liveness_thread.is_alive()
    and liveness_hard == [180.0]
    and liveness_exit == [70],
    "an unhealthy extractor snapshot at the validated 180s worker bound forces nonzero container replacement",
)
normal_monitor_stop = threading.Event()
normal_monitor_stop.set()
normal_samples = []
normal_thread = CW._start_graph_liveness_monitor(
    normal_monitor_stop,
    lambda hard: normal_samples.append(hard) or {"healthy": True},
    hard_seconds=180,
    interval_seconds=1,
    exit_process=lambda code: liveness_exit.append(code),
)
normal_thread.join(timeout=1)
check(not normal_thread.is_alive() and not normal_samples,
      "the daemon liveness monitor exits without sampling after normal worker stop")

turn_now = [100.0]
turn_liveness = CW._TurnLiveness(clock=lambda: turn_now[0])
turn_liveness.begin()
turn_now[0] = 371.0
turn_exit = []
turn_stop = threading.Event()
turn_thread = CW._start_turn_liveness_monitor(
    turn_stop,
    turn_liveness,
    hard_seconds=270,
    interval_seconds=1,
    exit_process=lambda code: turn_exit.append(code),
)
turn_thread.join(timeout=1)
check(
    not turn_thread.is_alive() and turn_exit == [70],
    "a complete convergence turn crossing 270s forces replacement even outside the extractor slot",
)


class FakeSignalAPI:
    SIGALRM = 14
    ITIMER_REAL = 0

    def __init__(self):
        self.previous_handler = object()
        self.handlers = {self.SIGALRM: self.previous_handler}
        self.timer = (0.0, 0.0)
        self.timer_calls = []

    def signal(self, number, handler):
        previous = self.handlers.get(number)
        self.handlers[number] = handler
        return previous

    def setitimer(self, which, seconds, interval=0.0):
        previous = self.timer
        self.timer = (float(seconds), float(interval))
        self.timer_calls.append((which, float(seconds), float(interval)))
        return previous


fake_signal_api = FakeSignalAPI()
alarm_exits = []
alarm_writes = []
turn_alarm = CW._TurnWallAlarm(
    270,
    boot_id="1" * 32,
    git_sha="a" * 40,
    signal_api=fake_signal_api,
    exit_process=lambda code: alarm_exits.append(code),
    write_stderr=lambda fd, value: alarm_writes.append((fd, value)),
)
turn_alarm.install()
guard_exception = False
try:
    with turn_alarm.guard_turn():
        check(
            fake_signal_api.timer == (270.0, 0.0),
            "ITIMER_REAL is armed at the exact lease-safe wall while foreground work runs",
        )
        fake_signal_api.handlers[fake_signal_api.SIGALRM](
            fake_signal_api.SIGALRM, None)
        raise RuntimeError("fixture unwind")
except RuntimeError:
    guard_exception = True
check(
    guard_exception
    and fake_signal_api.timer == (0.0, 0.0)
    and alarm_exits == [70]
    and len(alarm_writes) == 1
    and b"CONVERGENCE TURN ALARM FAILED" in alarm_writes[0][1],
    "the SIGALRM handler forces exit code 70 and exceptional turn unwind reliably disarms the timer",
)
with turn_alarm.guard_turn():
    pass
turn_alarm.close()
check(
    [call[1] for call in fake_signal_api.timer_calls] == [
        270.0, 0.0, 270.0, 0.0,
    ]
    and fake_signal_api.handlers[fake_signal_api.SIGALRM]
    is fake_signal_api.previous_handler,
    "each turn rearms ITIMER_REAL and clean shutdown restores the predecessor signal handler",
)


class OneTickStop:
    def __init__(self):
        self.calls = 0

    def wait(self, _seconds):
        self.calls += 1
        return self.calls > 1


poll_clock = [100.0]
poll_health = CW._PollHealth(clock=lambda: poll_clock[0])
poll_health.fail()
poll_exit = []
poll_output = io.StringIO()
with redirect_stdout(poll_output):
    heartbeat_thread = CW._start_scheduler_heartbeat_monitor(
        OneTickStop(),
        SimpleNamespace(depth=lambda: {"pending": 7}),
        poll_health,
        boot_id="1" * 32,
        git_sha="a" * 40,
        interval_seconds=1,
    )
    heartbeat_thread.join(timeout=1)
    poll_clock[0] = 221.0
    poll_thread = CW._start_poll_failure_monitor(
        OneTickStop(),
        poll_health,
        hard_seconds=120,
        interval_seconds=5,
        boot_id="1" * 32,
        git_sha="a" * 40,
        exit_process=lambda code: poll_exit.append(code),
    )
    poll_thread.join(timeout=1)
check(
    poll_exit == [70]
    and "CONVERGENCE POLL FAILED:" in poll_output.getvalue()
    and "HEARTBEAT scheduler=ok" not in poll_output.getvalue(),
    "a foreground poll failure disarms DB-only heartbeats and its independent 5s monitor replaces at 120s",
)

pause_stop = threading.Event()
pause_stop.set()
pause_touched = []
pause_deps = SimpleNamespace(
    install_nonblocking_stdio=lambda: None,
    check_schema_contract=lambda dsn: pause_touched.append("contract"),
)
pause_saved = {
    "VERIPSA_RUNTIME_ROLE": os.environ.get("VERIPSA_RUNTIME_ROLE"),
    "VERIPSA_POLICY_REFRESH": os.environ.get("VERIPSA_POLICY_REFRESH"),
    "OWNER_DSN": os.environ.get("OWNER_DSN"),
}
try:
    os.environ.pop("OWNER_DSN", None)
    os.environ["VERIPSA_RUNTIME_ROLE"] = "convergence-worker"
    os.environ["VERIPSA_POLICY_REFRESH"] = "0"
    pause_result = CW.run(pause_stop, dependencies=pause_deps)
finally:
    for name, value in pause_saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
check(pause_result == 0 and not pause_touched,
      "the existing kill switch parks the foreground process without schema/GitHub/DB work or a restart loop")

owner_boundary_touched = []
owner_boundary_refused = False
owner_boundary_saved = {
    name: os.environ.get(name)
    for name in (
        "VERIPSA_RUNTIME_ROLE",
        "VERIPSA_POLICY_REFRESH",
        "OWNER_DSN",
    )
}
try:
    os.environ.update({
        "VERIPSA_RUNTIME_ROLE": "convergence-worker",
        "VERIPSA_POLICY_REFRESH": "0",
        "OWNER_DSN": "postgresql://owner-secret-must-not-reach-runtime",
    })
    with redirect_stdout(io.StringIO()):
        try:
            CW.run(
                threading.Event(),
                dependencies=SimpleNamespace(
                    install_nonblocking_stdio=lambda: owner_boundary_touched.append(
                        "stdio"),
                    check_schema_contract=lambda _dsn: owner_boundary_touched.append(
                        "schema"),
                ),
            )
        except CW.WorkerConfigurationError:
            owner_boundary_refused = True
finally:
    for name, value in owner_boundary_saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
check(
    owner_boundary_refused
    and owner_boundary_touched == ["stdio"],
    "worker runtime refuses leaked OWNER_DSN before pause, schema, GitHub, store, or claim work",
)

invalid_identity_output = io.StringIO()
invalid_identity_refused = False
identity_saved = {
    name: os.environ.get(name)
    for name in (
        "VERIPSA_RUNTIME_ROLE",
        "VERIPSA_POLICY_REFRESH",
        "RENDER",
        "RENDER_GIT_COMMIT",
    )
}
try:
    os.environ.update({
        "VERIPSA_RUNTIME_ROLE": "convergence-worker",
        "VERIPSA_POLICY_REFRESH": "0",
        "RENDER": "true",
        "RENDER_GIT_COMMIT": "not-a-commit",
    })
    with redirect_stdout(invalid_identity_output):
        try:
            CW.run(
                threading.Event(),
                dependencies=SimpleNamespace(
                    install_nonblocking_stdio=lambda: None),
            )
        except CW.WorkerConfigurationError:
            invalid_identity_refused = True
finally:
    for name, value in identity_saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
check(
    invalid_identity_refused
    and re.search(
        r"convergence worker: STARTING "
        r"boot=[0-9a-f]{32} sha=invalid",
        invalid_identity_output.getvalue(),
    ) is not None,
    "every Render boot is fenced by STARTING before an invalid target identity refuses startup",
)


def refuses_contract(skipped, healthy):
    local_stop = threading.Event()
    touched = []
    bad_deps = SimpleNamespace(
        GitHubREST=lambda *args: touched.append("github"),
        PolicyRefreshStore=lambda *args: touched.append("store"),
        drain=lambda *args, **kwargs: touched.append("drain"),
        graph_refresh_strict=strict_callback,
        check_schema_contract=lambda dsn: SimpleNamespace(
            healthy=healthy, skipped=skipped, checked=0, violations=()),
        install_nonblocking_stdio=lambda: None,
    )
    local_saved = {name: os.environ.get(name) for name in names}
    try:
        os.environ.pop("OWNER_DSN", None)
        os.environ.update({
            "VERIPSA_RUNTIME_ROLE": "convergence-worker",
            "VERIPSA_POLICY_REFRESH": "1",
            "VERIPSA_DSN": "postgresql://example/veripsa",
            "GH_APP_ID": "123",
            "GH_PRIVATE_KEY": "test-private-key",
        })
        return CW.run(local_stop, dependencies=bad_deps), touched
    finally:
        for name, value in local_saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


failed_result, failed_touched = refuses_contract(False, False)
skipped_result, skipped_touched = refuses_contract(True, True)
check(failed_result == 78 and not failed_touched,
      "a failed schema contract exits nonzero before GitHub/store construction or claims")
check(skipped_result == 78 and not skipped_touched,
      "the web emergency schema-contract skip cannot arm the convergence writer")

role_saved = {
    name: os.environ.get(name)
    for name in ("VERIPSA_RUNTIME_ROLE", "VERIPSA_POLICY_REFRESH")
}
unknown_role_refused = False
try:
    os.environ["VERIPSA_RUNTIME_ROLE"] = "web"
    os.environ["VERIPSA_POLICY_REFRESH"] = "1"
    role_aware_web_starts = SB._should_start_inprocess_convergence()
    predecessor_web_starts = (
        os.environ.get("VERIPSA_POLICY_REFRESH", "1") != "0"
    )
    os.environ.pop("VERIPSA_RUNTIME_ROLE", None)
    legacy_unset_starts = SB._should_start_inprocess_convergence()
    os.environ["VERIPSA_RUNTIME_ROLE"] = "misspelled-web"
    try:
        SB._should_start_inprocess_convergence()
    except RuntimeError:
        unknown_role_refused = True
finally:
    for name, value in role_saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
check(
    not role_aware_web_starts
    and predecessor_web_starts
    and legacy_unset_starts,
    "role-aware web delegates with refresh=1, while an old-image rollback and local unset-role embed drain",
)
check(unknown_role_refused,
      "an unknown explicit role fails closed before the web runtime can be wired")

health_spec = importlib.util.spec_from_file_location(
    "container_healthcheck_under_test", ROOT / "github-app" / "container_healthcheck.py")
HC = importlib.util.module_from_spec(health_spec)
assert health_spec and health_spec.loader
health_spec.loader.exec_module(HC)
check(
    HC.check(
        role="convergence-worker",
        opener=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("worker healthcheck must not open HTTP")),
    ) == 0,
    "Docker HEALTHCHECK bypasses the absent HTTP port for the foreground worker role",
)
check(HC.check(role="misspelled-worker") == 1,
      "an unknown runtime role fails the container healthcheck instead of bypassing liveness")

render = (ROOT / "render.yaml").read_text(encoding="utf-8")
web_block, worker_tail = render.split("  - type: worker", 1)
worker_block, cron_tail = worker_tail.split("  - type: cron", 1)


def render_env_int(block, key):
    match = re.search(
        rf"- key: {re.escape(key)}\s*\n\s*value: [\"']?(\d+)[\"']?",
        block,
    )
    return int(match.group(1)) if match else None


render_graph_wall = render_env_int(
    worker_block, "VERIPSA_GRAPH_EXTRACT_TIMEOUT_SECONDS")
render_liveness_hard = render_env_int(
    worker_block, "VERIPSA_CONVERGENCE_LIVENESS_HARD_SECONDS")
render_turn_hard = render_env_int(
    worker_block, "VERIPSA_CONVERGENCE_TURN_HARD_SECONDS")
render_poll_failure_hard = render_env_int(
    worker_block, "VERIPSA_CONVERGENCE_POLL_FAILURE_HARD_SECONDS")
render_stale = render_env_int(
    worker_block, "VERIPSA_POLICY_REFRESH_STALE_SECONDS")
check(
    "name: veripsa-app" in web_block
    and re.search(
        r"key: VERIPSA_RUNTIME_ROLE\s*\n\s*value: web",
        web_block,
    ) is not None
    and render_env_int(web_block, "VERIPSA_POLICY_REFRESH") == 1
    and render_env_int(
        web_block, "VERIPSA_POLICY_REFRESH_STALE_SECONDS") == 300
    and "VERIPSA_GRAPH_EXTRACT_TIMEOUT_SECONDS" not in web_block,
    "Render web delegates convergence by role while bounding old-image rollback claims at 300s",
)
check(
    all(token in worker_block for token in (
        "name: veripsa-convergence",
        "plan: starter",
        "region: oregon",
        "autoDeploy: false",
        "dockerCommand: env -u OWNER_DSN python3 github-app/convergence_worker.py",
        "maxShutdownDelaySeconds: 300",
        "key: VERIPSA_GRAPH_EXTRACT_TIMEOUT_SECONDS",
        'value: "180"',
        "key: VERIPSA_CONVERGENCE_LIVENESS_HARD_SECONDS",
        "key: VERIPSA_CONVERGENCE_TURN_HARD_SECONDS",
        "key: VERIPSA_CONVERGENCE_LIVENESS_INTERVAL_SECONDS",
        "key: VERIPSA_CONVERGENCE_HEARTBEAT_SECONDS",
        "key: VERIPSA_CONVERGENCE_POLL_FAILURE_HARD_SECONDS",
        "key: VERIPSA_POLICY_REFRESH_STALE_SECONDS",
        "key: VERIPSA_DSN",
        "key: GH_APP_ID",
        "key: GH_PRIVATE_KEY",
    ))
    and re.search(r"(?m)^\s*numInstances:\s*2\s*$", worker_block) is not None,
    "Render declares two manually-promoted starter workers with bounded shutdown, graph wall, and required secrets",
)
check(
    render_graph_wall == 180
    and render_liveness_hard == 240
    and render_turn_hard == 270
    and render_poll_failure_hard == 120
    and render_stale == 300
    and render_poll_failure_hard < render_turn_hard
    and max(render_graph_wall, render_liveness_hard, render_turn_hard) + 30 <= render_stale < 787,
    "abandoned turns become reclaimable at 300s—after the complete 270s wall/margin and before 787s",
)
check(
    "HEALTHCHECK" in (ROOT / "github-app" / "Dockerfile").read_text(encoding="utf-8")
    and "container_healthcheck.py" in (ROOT / "github-app" / "Dockerfile").read_text(encoding="utf-8"),
    "the image delegates role-aware liveness to the tested healthcheck helper",
)
dockerfile = (ROOT / "github-app" / "Dockerfile").read_text(encoding="utf-8")
server_source = (ROOT / "github-app" / "server.py").read_text(encoding="utf-8")
check(
    'CMD ["env", "-u", "OWNER_DSN", "python3", "github-app/server.py"]'
    in dockerfile
    and "CMD env -u OWNER_DSN python3 github-app/container_healthcheck.py"
    in dockerfile
    and 'if os.environ.get("OWNER_DSN")' in server_source
    and "OWNER_DSN is preDeploy-only" in server_source
    and "env -u OWNER_DSN python3 github-app/convergence_worker.py"
    in worker_block,
    "preDeploy owner credentials are absent from both runtime process environments and bypasses fail closed",
)
actual_deps = CW._load_runtime_dependencies()
import ingest  # noqa: E402
check(
    actual_deps.graph_extraction_liveness is ingest.graph_extraction_liveness
    and actual_deps.graph_refresh_strict is ingest.converge_main_graph_strict,
    "the production entrypoint wires ingest's real strict convergence and extractor-liveness authorities",
)

if fail:
    raise SystemExit(1)
print("CONVERGENCE WORKER ISOLATION: PASS")
