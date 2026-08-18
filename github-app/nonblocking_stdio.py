#!/usr/bin/env python3
"""Non-blocking production stdout/stderr for worker liveness.

Render captures process output through pipes. A synchronous ``print(...,
flush=True)`` can therefore block forever when the downstream log drain is
stalled or back-pressured. Every event worker shares Python's text-stream lock,
so one blocked log write can stop otherwise independent tenant lanes.

The production composition root installs this stream only for pipe/socket
descriptors. Each complete line is emitted with one non-blocking ``os.write``;
when the pipe cannot accept it immediately, the line is dropped and a
content-free counter is incremented. Regular files and terminals are untouched.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
import threading


_MAX_RECORD_BYTES = 3_072  # below the portable 4 KiB PIPE_BUF atomic-write floor
_STATE_LOCK = threading.Lock()
_ORIGINAL_STREAMS: list[object] = []
_INSTALLED = False
_DROPPED_WRITES = 0


def _note_drop() -> None:
    global _DROPPED_WRITES
    # Exact accounting is not a correctness boundary; this is an operational
    # signal. CPython serializes the update under the GIL.
    _DROPPED_WRITES += 1


def dropped_writes() -> int:
    """Number of log records dropped since this process started."""
    return int(_DROPPED_WRITES)


class NonBlockingPipeStream:
    """A minimal TextIO-compatible, per-thread line-buffered pipe writer."""

    def __init__(self, fd: int, *, encoding: str = "utf-8", errors: str = "replace"):
        self._fd = int(fd)
        self.encoding = encoding or "utf-8"
        self.errors = errors or "replace"
        self._local = threading.local()

    def fileno(self) -> int:
        return self._fd

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def _emit(self, text: str) -> None:
        data = text.encode(self.encoding, self.errors)
        if len(data) > _MAX_RECORD_BYTES:
            suffix = b"...[truncated]\n"
            data = data[:_MAX_RECORD_BYTES - len(suffix)] + suffix
        try:
            written = os.write(self._fd, data)
            if written != len(data):
                _note_drop()
        except BlockingIOError:
            _note_drop()
        except OSError as exc:
            # Broken/closed log drains are observability loss, never permission
            # to stop event processing.
            if exc.errno in (
                errno.EAGAIN, errno.EWOULDBLOCK, errno.EPIPE,
                errno.EBADF, errno.ENXIO,
            ):
                _note_drop()
                return
            _note_drop()

    def write(self, value) -> int:
        text = str(value)
        pending = getattr(self._local, "pending", "") + text
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            self._emit(line + "\n")
        if len(pending.encode(self.encoding, self.errors)) >= _MAX_RECORD_BYTES:
            self._emit(pending + "\n")
            pending = ""
        self._local.pending = pending
        return len(text)

    def flush(self) -> None:
        pending = getattr(self._local, "pending", "")
        if pending:
            self._local.pending = ""
            self._emit(pending)


def _pipe_or_socket(stream) -> tuple[bool, int | None]:
    try:
        fd = int(stream.fileno())
        mode = os.fstat(fd).st_mode
    except (AttributeError, OSError, TypeError, ValueError):
        return False, None
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode), fd


def install_nonblocking_stdio(*, force: bool = False) -> bool:
    """Install once for production pipe/socket streams.

    Without ``force``, explicit ``VERIPSA_NONBLOCKING_STDIO`` wins; otherwise
    installation is enabled only on Render. This keeps local test capture and
    interactive terminals unchanged while making the deployed worker safe by
    default. Returns whether at least one stream was replaced.
    """
    global _INSTALLED
    explicit = os.environ.get("VERIPSA_NONBLOCKING_STDIO")
    if explicit is not None:
        enabled = explicit == "1"
    else:
        enabled = bool(
            os.environ.get("RENDER")
            or os.environ.get("RENDER_SERVICE_ID")
            or os.environ.get("RENDER_GIT_COMMIT")
        )
    if not (force or enabled):
        return False

    with _STATE_LOCK:
        if _INSTALLED:
            return True
        replaced = False
        for name in ("stdout", "stderr"):
            stream = getattr(sys, name, None)
            eligible, fd = _pipe_or_socket(stream)
            if not eligible or fd is None:
                continue
            try:
                os.set_blocking(fd, False)
            except (AttributeError, OSError):
                continue
            # Retain the original TextIOWrapper so its finalizer cannot close
            # the descriptor now owned by the replacement stream.
            _ORIGINAL_STREAMS.append(stream)
            setattr(
                sys,
                name,
                NonBlockingPipeStream(
                    fd,
                    encoding=getattr(stream, "encoding", None) or "utf-8",
                    errors=getattr(stream, "errors", None) or "replace",
                ),
            )
            replaced = True
        _INSTALLED = replaced
        return replaced
