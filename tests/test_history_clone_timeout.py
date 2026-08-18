#!/usr/bin/env python3
"""Offline history-clone process-boundary gate.

The co-change history clone is advisory, but a hung git process must not pin a
worker indefinitely.  This proves the clone has its own process group, shares
the GitHub/event deadline, kills the whole group on timeout, bounds cleanup,
and never exposes host secrets or the installation token through argv.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import event_budget  # noqa: E402
import github_rest as gr  # noqa: E402
import github_rest_contentfetch as contentfetch  # noqa: E402


FAIL = 0


def check(condition: bool, label: str) -> None:
    global FAIL
    print(("PASS: " if condition else "FAIL: ") + label)
    if not condition:
        FAIL += 1


class _Client(contentfetch._GitHubContentFetchMixin):
    API = "https://api.github.com"

    def __init__(self, token="installation-token-secret"):
        self.token = token

    def _itoken(self):
        return self.token


def test_spawn_contract_and_budget_cap() -> None:
    captured = []
    real_popen = contentfetch.subprocess.Popen
    secret_keys = ("VERIPSA_DSN", "GH_PRIVATE_KEY", "GH_WEBHOOK_SECRET", "GITHUB_TOKEN")
    originals = {key: os.environ.get(key) for key in secret_keys}

    class _SuccessProc:
        pid = 123456789
        returncode = 0

        def __init__(self, cmd, **kwargs):
            captured.append({"cmd": list(cmd), "kwargs": kwargs, "timeouts": []})

        def communicate(self, timeout=None):
            captured[-1]["timeouts"].append(timeout)
            return "", ""

    for key in secret_keys:
        os.environ[key] = f"host-{key}-secret"
    contentfetch.subprocess.Popen = _SuccessProc
    budget_token = event_budget.begin(0.25)
    try:
        result = _Client().history_clone(
            "acme/repo", "main", "/tmp/veripsa-history-contract", timeout=120)
    finally:
        event_budget.end(budget_token)
    try:
        outside_result = _Client().history_clone(
            "acme/repo", "main", "/tmp/veripsa-history-outside", timeout=120)
    finally:
        contentfetch.subprocess.Popen = real_popen
        for key, value in originals.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    call = captured[0] if captured else {}
    cmd = call.get("cmd") or []
    kwargs = call.get("kwargs") or {}
    child_env = kwargs.get("env") or {}
    timeout_seen = (call.get("timeouts") or [None])[0]
    outside_timeout = (
        (captured[1].get("timeouts") or [None])[0]
        if len(captured) > 1 else None
    )
    token = "installation-token-secret"
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    check(result == "/tmp/veripsa-history-contract", "successful clone preserves the destination contract")
    check(kwargs.get("start_new_session") is True, "git clone starts in a fresh process group")
    check(kwargs.get("close_fds") is True, "git clone closes inherited file descriptors")
    check(token not in " ".join(cmd) and basic not in " ".join(cmd), "token/basic credential never enters argv")
    check(
        all(key not in child_env for key in secret_keys),
        "git receives no host DB/App/webhook/token secrets",
    )
    check(
        child_env.get("GIT_CONFIG_VALUE_0") == f"AUTHORIZATION: basic {basic}",
        "installation credential remains only in git's extraHeader environment",
    )
    check(
        isinstance(timeout_seen, (int, float)) and 0 < timeout_seen <= 0.25,
        f"communicate timeout is capped by the shared event budget ({timeout_seen!r})",
    )
    check(
        outside_result == "/tmp/veripsa-history-outside"
        and isinstance(outside_timeout, (int, float))
        and 119.0 < outside_timeout <= 120.0,
        f"background clone outside event context retains caller's 120s allowance ({outside_timeout!r})",
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_real_hanging_process_group_is_killed() -> None:
    work = tempfile.mkdtemp(prefix="veripsa_history_hang_")
    real_popen = contentfetch.subprocess.Popen
    fixture = (
        "import os,subprocess,sys\n"
        "child=subprocess.Popen(['/bin/sleep','30'])\n"
        "with open(sys.argv[1]+'.pids','w',encoding='ascii') as fh:\n"
        "    fh.write(f'{os.getpid()} {child.pid}')\n"
        "child.wait()\n"
    )

    def hanging_popen(cmd, **kwargs):
        # Preserve every production Popen option, especially
        # start_new_session=True, while replacing network git with a fully
        # offline leader+descendant fixture.
        return real_popen([sys.executable, "-c", fixture, cmd[-1]], **kwargs)

    contentfetch.subprocess.Popen = hanging_popen
    try:
        dest = os.path.join(work, "clone")
        started = time.monotonic()
        try:
            # Leave enough setup time for a cold macOS/Xcode Python launcher;
            # the subprocess then remains blocked until the real timeout.
            _Client().history_clone("acme/repo", "main", dest, timeout=1.0)
        except subprocess.TimeoutExpired:
            timed_out = True
        else:
            timed_out = False
        elapsed = time.monotonic() - started
        with open(dest + ".pids", "r", encoding="ascii") as fh:
            pids = [int(value) for value in fh.read().split()]
        deadline = time.monotonic() + 1.0
        while any(_pid_exists(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.01)
        survivors = [pid for pid in pids if _pid_exists(pid)]
        check(timed_out, "real hanging git reaches the local communicate timeout")
        check(elapsed < 2.5, f"real timeout and cleanup remain bounded (elapsed={elapsed:.2f}s)")
        check(len(pids) == 2, "fixture recorded git leader and descendant")
        check(not survivors, f"SIGKILL removes the whole clone process group (survivors={survivors})")
    finally:
        contentfetch.subprocess.Popen = real_popen
        shutil.rmtree(work, ignore_errors=True)


def test_unreapable_cleanup_moves_to_daemon() -> None:
    real_popen = contentfetch.subprocess.Popen
    real_killpg = contentfetch.os.killpg
    old_cleanup = contentfetch._HISTORY_CLONE_KILL_WAIT_SECONDS
    daemon_waiting = threading.Event()
    allow_reap = threading.Event()
    calls = []
    spawn_kwargs = {}

    class _SlowProc:
        pid = 987654321
        returncode = None

        def __init__(self, cmd, **kwargs):
            spawn_kwargs.update(kwargs)

        def communicate(self, timeout=None):
            calls.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired(["fake-git"], timeout)
            daemon_waiting.set()
            allow_reap.wait(2.0)
            self.returncode = -9
            return "", ""

        def kill(self):
            return None

        def wait(self):
            allow_reap.wait(2.0)
            self.returncode = -9
            return -9

    contentfetch.subprocess.Popen = _SlowProc
    contentfetch.os.killpg = lambda *args, **kwargs: None
    contentfetch._HISTORY_CLONE_KILL_WAIT_SECONDS = 0.05
    started = time.monotonic()
    slot_held_while_reaping = False
    second_clone_could_enter = False
    slot_released_after_reap = False
    try:
        try:
            _Client().history_clone(
                "acme/repo", "main", "/tmp/veripsa-history-daemon", timeout=0.05)
        except subprocess.TimeoutExpired:
            timed_out = True
        else:
            timed_out = False
        elapsed = time.monotonic() - started
        daemon_started = daemon_waiting.wait(0.5)
        slot_held_while_reaping = contentfetch._HISTORY_CLONE_CHILD_LOCK.locked()
        second_clone_could_enter = contentfetch._HISTORY_CLONE_CHILD_LOCK.acquire(blocking=False)
        if second_clone_could_enter:
            contentfetch._HISTORY_CLONE_CHILD_LOCK.release()
        allow_reap.set()
        release_deadline = time.monotonic() + 1.0
        while (
            contentfetch._HISTORY_CLONE_CHILD_LOCK.locked()
            and time.monotonic() < release_deadline
        ):
            time.sleep(0.01)
        slot_released_after_reap = not contentfetch._HISTORY_CLONE_CHILD_LOCK.locked()
    finally:
        allow_reap.set()
        contentfetch.subprocess.Popen = real_popen
        contentfetch.os.killpg = real_killpg
        contentfetch._HISTORY_CLONE_KILL_WAIT_SECONDS = old_cleanup

    check(timed_out, "slow cleanup preserves the clone timeout outcome")
    check(elapsed < 0.5, f"worker returns after bounded cleanup handoff (elapsed={elapsed:.2f}s)")
    check(spawn_kwargs.get("start_new_session") is True, "daemon fallback still uses an isolated process group")
    check(
        len(calls) >= 3 and calls[0] <= 0.051 and calls[1] <= 0.051 and calls[2] is None,
        f"main wait and cleanup are bounded before daemon communicate (calls={calls!r})",
    )
    check(daemon_started, "unreaped clone is handed to a daemon reaper")
    check(
        slot_held_while_reaping and not second_clone_could_enter,
        "daemon reaper retains the one-child slot so a second clone cannot start",
    )
    check(
        slot_released_after_reap,
        "history-clone slot is released only after daemon wait completes",
    )


def main() -> int:
    print("=== HISTORY CLONE TIMEOUT GATE ===")
    test_spawn_contract_and_budget_cap()
    test_real_hanging_process_group_is_killed()
    test_unreapable_cleanup_moves_to_daemon()
    if FAIL:
        print(f"HISTORY CLONE TIMEOUT GATE: FAIL ({FAIL} check(s))")
        return 1
    print("HISTORY CLONE TIMEOUT GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
