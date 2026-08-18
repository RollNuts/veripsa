#!/usr/bin/env python3
"""Run one command with a wall-clock deadline and terminate its process group."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


TERM_GRACE_S = 5
KILL_GRACE_S = 1


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # POSIX defines EPERM as "the group exists, but the caller may not
        # signal every member".  It is existence evidence, not an exception
        # the timeout wrapper should let escape.
        return True
    return True


def _signal_process_group(process_group: int, signal_number: int) -> bool:
    try:
        os.killpg(process_group, signal_number)
    except ProcessLookupError:
        return False
    except PermissionError:
        print(
            f"[timeout] process-group cleanup unverified: signal {signal_number} denied",
            file=sys.stderr,
            flush=True,
        )
        return False
    return True


def _terminate_process_group(process: subprocess.Popen[bytes], initial_signal: int) -> None:
    _signal_process_group(process.pid, initial_signal)
    try:
        process.wait(timeout=TERM_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    # The group leader can exit while a descendant ignores the forwarded signal.
    if _process_group_exists(process.pid):
        _signal_process_group(process.pid, signal.SIGKILL)
    # Signal permissions and kernel teardown are outside our control.  The
    # deadline wrapper itself must remain bounded even when the final reap
    # cannot complete.
    try:
        process.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        print(
            "[timeout] process-group cleanup unverified: leader survived SIGKILL grace",
            file=sys.stderr,
            flush=True,
        )


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        print("usage: run_with_timeout.py SECONDS -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    try:
        timeout_s = int(sys.argv[1])
    except ValueError:
        print("timeout must be an integer number of seconds", file=sys.stderr)
        return 2
    if timeout_s <= 0:
        print("timeout must be positive", file=sys.stderr)
        return 2

    command = sys.argv[3:]
    process = subprocess.Popen(command, start_new_session=True)

    def forward_outer_signal(signal_number: int, _frame: object) -> None:
        _terminate_process_group(process, signal_number)
        raise SystemExit(128 + signal_number)

    for signal_number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signal_number, forward_outer_signal)
    try:
        return process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        print(
            f"[timeout] command exceeded {timeout_s}s: {' '.join(command)}",
            file=sys.stderr,
            flush=True,
        )
        _terminate_process_group(process, signal.SIGTERM)
        return 124


if __name__ == "__main__":
    raise SystemExit(main())
