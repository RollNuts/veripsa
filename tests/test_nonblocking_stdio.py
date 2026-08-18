#!/usr/bin/env python3
"""A full log pipe must drop records promptly, never stop event workers."""
from __future__ import annotations

import os
import pathlib
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "github-app"))

import nonblocking_stdio as N  # noqa: E402


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("PASS:", label)


read_fd, write_fd = os.pipe()
try:
    os.set_blocking(write_fd, False)
    filler = b"x" * 4096
    while True:
        try:
            os.write(write_fd, filler)
        except BlockingIOError:
            break

    stream = N.NonBlockingPipeStream(write_fd)
    before = N.dropped_writes()
    started = time.monotonic()
    stream.write("worker must not wait for the log drain\n")
    elapsed = time.monotonic() - started
    check("a full log pipe returns in under 50ms", elapsed < 0.05)
    check("the dropped-record counter increments", N.dropped_writes() == before + 1)
finally:
    os.close(read_fd)
    os.close(write_fd)


regular = open(__file__, "r", encoding="utf-8")
try:
    eligible, _fd = N._pipe_or_socket(regular)
    check("regular files are never replaced", not eligible)
finally:
    regular.close()

print("nonblocking stdio gate: PASS")
