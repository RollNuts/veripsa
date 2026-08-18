#!/usr/bin/env python3
"""Regression checks for the standard-gate process deadline wrapper."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import signal
import subprocess
import sys
import tempfile
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "scripts", "run_with_timeout.py")


def _load_runner_module():
    spec = importlib.util.spec_from_file_location("veripsa_run_with_timeout", RUNNER)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load timeout runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Linux keeps a killed orphan briefly as a zombie until init reaps it. kill(0)
    # still succeeds for that state, but it cannot execute and is not a survivor.
    proc_stat = f"/proc/{pid}/stat"
    try:
        with open(proc_stat, encoding="utf-8") as proc_file:
            stat_fields = proc_file.read().rsplit(")", 1)[1].split()
    except (FileNotFoundError, IndexError, OSError):
        return True
    if stat_fields and stat_fields[0] == "Z":
        return False
    return True


def _stubborn_child_program(pid_file: str) -> str:
    return (
        "import os,signal,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
        f"open({pid_file!r},'w').write(str(os.getpid())+','+str(child.pid)); time.sleep(60)"
    )


def _wait_for_pid_file(pid_file: str) -> tuple[int, int]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if os.path.exists(pid_file):
            try:
                fields = open(pid_file, encoding="utf-8").read().split(",")
                if len(fields) == 2 and all(field.isdigit() for field in fields):
                    return int(fields[0]), int(fields[1])
            except OSError:
                pass
        time.sleep(0.05)
    raise AssertionError("timed child did not publish its process ids")


def main() -> int:
    failures: list[str] = []

    runner_module = _load_runner_module()
    original_killpg = runner_module.os.killpg

    class _ScriptedProcess:
        pid = 987_654_321

        def __init__(self, *, time_out: bool) -> None:
            self.time_out = time_out
            self.wait_timeouts: list[int] = []

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            if self.time_out:
                raise subprocess.TimeoutExpired("scripted", timeout)
            return -signal.SIGTERM

    attempted_signals: list[int] = []
    injected_diagnostics = io.StringIO()

    try:
        def observed_ci_sequence(_process_group: int, signal_number: int) -> None:
            attempted_signals.append(signal_number)
            if signal_number in (0, signal.SIGKILL):
                raise PermissionError(1, "Operation not permitted")

        runner_module.os.killpg = observed_ci_sequence
        with contextlib.redirect_stderr(injected_diagnostics):
            if not runner_module._process_group_exists(_ScriptedProcess.pid):
                failures.append("EPERM existence probe was mistaken for a missing process group")
            observed_process = _ScriptedProcess(time_out=False)
            runner_module._terminate_process_group(observed_process, signal.SIGTERM)
            if attempted_signals != [0, signal.SIGTERM, 0, signal.SIGKILL]:
                failures.append(
                    f"EPERM termination path skipped a signal attempt: {attempted_signals}"
                )
            if observed_process.wait_timeouts != [
                runner_module.TERM_GRACE_S,
                runner_module.KILL_GRACE_S,
            ]:
                failures.append(
                    f"EPERM termination used unexpected waits: {observed_process.wait_timeouts}"
                )

            attempted_signals.clear()
            bounded_process = _ScriptedProcess(time_out=True)
            runner_module._terminate_process_group(bounded_process, signal.SIGTERM)
            if bounded_process.wait_timeouts != [
                runner_module.TERM_GRACE_S,
                runner_module.KILL_GRACE_S,
            ]:
                failures.append(
                    f"unreaped termination was not bounded twice: {bounded_process.wait_timeouts}"
                )
    except PermissionError:
        failures.append("process-group EPERM escaped the bounded termination path")
    finally:
        runner_module.os.killpg = original_killpg

    if injected_diagnostics.getvalue().count("process-group cleanup unverified") != 3:
        failures.append("process-group cleanup uncertainty was not reported fail-loud")

    success = subprocess.run(
        [sys.executable, RUNNER, "5", "--", sys.executable, "-c", "print('ok')"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if success.returncode != 0 or success.stdout.strip() != "ok":
        failures.append("successful child exit/output was not preserved")

    nonzero = subprocess.run(
        [sys.executable, RUNNER, "5", "--", sys.executable, "-c", "raise SystemExit(7)"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if nonzero.returncode != 7:
        failures.append(f"non-zero child exit was not preserved: {nonzero.returncode}")

    with tempfile.TemporaryDirectory() as temp_dir:
        pid_file = os.path.join(temp_dir, "child.pid")
        child_program = _stubborn_child_program(pid_file)
        started = time.monotonic()
        timed = subprocess.run(
            [sys.executable, RUNNER, "1", "--", sys.executable, "-c", child_program],
            capture_output=True,
            text=True,
            timeout=10,
        )
        elapsed = time.monotonic() - started
        if timed.returncode != 124:
            failures.append(f"timed-out child returned {timed.returncode}, expected 124")
        if "[timeout] command exceeded 1s" not in timed.stderr:
            failures.append("timeout diagnostic was missing")
        if elapsed >= 8:
            failures.append(f"timed-out process group took {elapsed:.1f}s to terminate")
        _leader_pid, descendant_pid = _wait_for_pid_file(pid_file)
        if _pid_is_alive(descendant_pid):
            failures.append("timed-out descendant survived process-group termination")
            os.kill(descendant_pid, signal.SIGKILL)

    with tempfile.TemporaryDirectory() as temp_dir:
        pid_file = os.path.join(temp_dir, "outer-signal.pid")
        wrapper = subprocess.Popen(
            [sys.executable, RUNNER, "30", "--", sys.executable, "-c", _stubborn_child_program(pid_file)],
        )
        leader_pid, descendant_pid = _wait_for_pid_file(pid_file)
        wrapper.terminate()
        wrapper_rc = wrapper.wait(timeout=10)
        if wrapper_rc != 128 + signal.SIGTERM:
            failures.append(f"outer SIGTERM returned {wrapper_rc}, expected {128 + signal.SIGTERM}")
        for label, pid in (("leader", leader_pid), ("descendant", descendant_pid)):
            if _pid_is_alive(pid):
                failures.append(f"outer SIGTERM left {label} process {pid} alive")
                os.kill(pid, signal.SIGKILL)

    if failures:
        print("GATE PROCESS TIMEOUT: FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("GATE PROCESS TIMEOUT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
