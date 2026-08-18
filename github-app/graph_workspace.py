"""Bounded asynchronous cleanup for transient graph-extraction workspaces.

Graph workspaces may contain a large extracted tree.  ``TemporaryDirectory``
deletes that tree synchronously from ``__exit__``; a filesystem stall there
can therefore hold both the webhook worker lane and ingest's global graph
memory slot indefinitely, after the killable parser child has already exited.

This leaf owns exactly one daemon janitor.  A reservation covers the complete
workspace lifetime (active work plus queued/active/retrying cleanup), so the
queue is logically bounded even though ``SimpleQueue.put`` itself is
deliberately non-blocking.  A deletion error quarantines the path under that
same reservation and retries it with bounded exponential backoff.  Capacity is
released only after the path is actually absent.  Therefore repeated errors or
one cleanup that never returns can consume at most the configured cap; callers
then fail before creating or downloading into another workspace.
"""
from __future__ import annotations

import heapq
import math
import os
import queue
import shutil
import tempfile
import threading
import time

try:
    from env_config import env_int
except ImportError:  # imported as a package
    from .env_config import env_int


GRAPH_WORKSPACE_CLEANUP_CAP = env_int(
    "VERIPSA_GRAPH_WORKSPACE_CLEANUP_CAP",
    4,
    min_value=1,
    max_value=32,
)
_JANITOR_THREAD_NAME = "veripsa-graph-workspace-janitor"
_CLEANUP_RETRY_BASE_SECONDS = 1.0
_CLEANUP_RETRY_MAX_SECONDS = 60.0


class GraphWorkspaceCapacityError(RuntimeError):
    """All bounded active/pending workspace reservations are occupied."""


class _CleanupTicket:
    """Internal path identity and retry count; never exposed by diagnostics."""

    def __init__(self, path: str):
        self.path = path
        self.failures = 0


class _WorkspaceLease:
    """One reserved path whose exit only publishes an asynchronous cleanup."""

    def __init__(self, owner: "GraphWorkspaceJanitor", path: str):
        self._owner = owner
        self._path = path
        self._open = True

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        # SimpleQueue.put() never blocks.  Reservation accounting bounds the
        # number of live tickets, so this does not create an unbounded queue.
        self._owner._enqueue_cleanup(self._path)

    def __enter__(self) -> str:
        if not self._open:
            raise RuntimeError("graph workspace lease is already closed")
        return self._path

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()
        return False


class GraphWorkspaceJanitor:
    """One fixed daemon plus bounded active/pending/retrying reservations."""

    def __init__(
        self,
        capacity: int,
        *,
        rmtree=None,
        thread_name: str = _JANITOR_THREAD_NAME,
        retry_base_seconds: float = _CLEANUP_RETRY_BASE_SECONDS,
        retry_max_seconds: float = _CLEANUP_RETRY_MAX_SECONDS,
        clock=time.monotonic,
    ):
        value = int(capacity)
        if value < 1:
            raise ValueError("graph workspace janitor capacity must be positive")
        retry_base = float(retry_base_seconds)
        retry_max = float(retry_max_seconds)
        if (
            not math.isfinite(retry_base)
            or not math.isfinite(retry_max)
            or retry_base <= 0
            or retry_max <= 0
            or retry_base > retry_max
        ):
            raise ValueError(
                "graph workspace cleanup retry bounds must be positive and ordered")
        self.capacity = value
        self._retry_base_seconds = retry_base
        self._retry_max_seconds = retry_max
        if not callable(clock):
            raise TypeError("graph workspace janitor clock must be callable")
        self._clock = clock
        self._reservations = threading.BoundedSemaphore(value)
        self._cleanup_queue = queue.SimpleQueue()
        self._rmtree = rmtree or shutil.rmtree
        self._state_lock = threading.Lock()
        self._reserved = 0
        self._pending = 0
        self._active = 0
        self._quarantined = 0
        self._cleaned = 0
        self._failed = 0
        self._active_started_at = None
        self._saturated_since = None
        self._last_progress_at = self._clock()
        self._thread = threading.Thread(
            target=self._run,
            name=str(thread_name),
            daemon=True,
        )
        self._thread.start()

    def _update_saturation_locked(self) -> None:
        cleanup_backlog = (
            self._pending > 0
            or self._active > 0
            or self._quarantined > 0
        )
        if self._reserved >= self.capacity and cleanup_backlog:
            if self._saturated_since is None:
                self._saturated_since = self._clock()
        else:
            self._saturated_since = None

    def reserve(self, *, prefix: str = "veripsa_graph_") -> _WorkspaceLease:
        """Reserve before mkdtemp; never wait for a stuck cleanup."""
        if not self._reservations.acquire(blocking=False):
            raise GraphWorkspaceCapacityError(
                "graph workspace cleanup capacity is exhausted")
        with self._state_lock:
            self._reserved += 1
            self._update_saturation_locked()
        try:
            path = tempfile.mkdtemp(prefix=prefix)
        except BaseException:
            self._release_uncreated_reservation()
            raise
        return _WorkspaceLease(self, path)

    def _enqueue_cleanup(self, path: str) -> None:
        ticket = _CleanupTicket(path)
        with self._state_lock:
            self._pending += 1
            self._update_saturation_locked()
        self._cleanup_queue.put(ticket)

    def _release_uncreated_reservation(self) -> None:
        with self._state_lock:
            self._reserved -= 1
            self._update_saturation_locked()
        self._reservations.release()

    def _start_attempt(self) -> None:
        with self._state_lock:
            self._pending -= 1
            self._active += 1
            self._active_started_at = self._clock()
            self._update_saturation_locked()

    def _retry_attempt(self, ticket: _CleanupTicket) -> float:
        ticket.failures += 1
        # Clamp the exponent as well as the resulting delay so an indefinitely
        # quarantined path cannot overflow while computing its cadence.
        exponent = min(ticket.failures - 1, 30)
        delay = min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2 ** exponent),
        )
        with self._state_lock:
            self._active -= 1
            self._active_started_at = None
            self._pending += 1
            self._failed += 1
            if ticket.failures == 1:
                self._quarantined += 1
            self._update_saturation_locked()
        return delay

    def _finish_attempt(self, ticket: _CleanupTicket) -> None:
        with self._state_lock:
            self._active -= 1
            self._active_started_at = None
            self._reserved -= 1
            self._cleaned += 1
            self._last_progress_at = self._clock()
            if ticket.failures:
                self._quarantined -= 1
            self._update_saturation_locked()
        self._reservations.release()

    def _run(self) -> None:
        retries = []
        sequence = 0
        while True:
            now = time.monotonic()
            if retries and retries[0][0] <= now:
                _due, _sequence, ticket = heapq.heappop(retries)
            else:
                timeout = (
                    max(0.0, retries[0][0] - now)
                    if retries else None
                )
                try:
                    if timeout is None:
                        ticket = self._cleanup_queue.get()
                    else:
                        ticket = self._cleanup_queue.get(timeout=timeout)
                except queue.Empty:
                    _due, _sequence, ticket = heapq.heappop(retries)
            self._start_attempt()
            cleanup_failed = False
            try:
                self._rmtree(ticket.path)
            except FileNotFoundError:
                # This may be the root already being absent, or a child racing
                # with recursive deletion. Verify the root below.
                pass
            except BaseException:
                # Keep the reservation until deletion is proven.  This makes a
                # permanently failing filesystem fail closed at `capacity`
                # instead of admitting an unbounded number of leftover trees.
                # Do not log the path: it is transient host metadata.
                cleanup_failed = True
            if not cleanup_failed:
                try:
                    os.lstat(ticket.path)
                except FileNotFoundError:
                    pass
                except BaseException:
                    # Permission/I/O errors are not proof of absence.
                    cleanup_failed = True
                else:
                    # A buggy/partial filesystem operation that returns
                    # normally is not proof of deletion.
                    cleanup_failed = True
            if cleanup_failed:
                delay = self._retry_attempt(ticket)
                sequence += 1
                heapq.heappush(
                    retries,
                    (time.monotonic() + delay, sequence, ticket),
                )
            else:
                self._finish_attempt(ticket)

    def stats(self) -> dict:
        """Content-free diagnostics and a deterministic test seam."""
        with self._state_lock:
            now = self._clock()
            reserved = self._reserved
            pending = self._pending
            active = self._active
            quarantined = self._quarantined
            cleaned = self._cleaned
            failed = self._failed
            cleanup_saturated = bool(
                reserved >= self.capacity
                and (pending > 0 or active > 0 or quarantined > 0)
            )
            active_seconds = (
                max(0.0, now - float(self._active_started_at))
                if active and self._active_started_at is not None else None
            )
            saturated_seconds = (
                max(0.0, now - float(self._saturated_since))
                if cleanup_saturated
                and self._saturated_since is not None else None
            )
            no_progress_seconds = max(
                0.0, now - float(self._last_progress_at))
        return {
            "capacity": self.capacity,
            "reserved": reserved,
            "leased": max(0, reserved - pending - active),
            "pending": pending,
            "active": active,
            "quarantined": quarantined,
            "cleaned": cleaned,
            "failed": failed,
            "saturated": reserved >= self.capacity,
            "cleanup_saturated": cleanup_saturated,
            "active_seconds": active_seconds,
            "saturated_seconds": saturated_seconds,
            "no_progress_seconds": no_progress_seconds,
            "thread_alive": self._thread.is_alive(),
            "thread_ident": self._thread.ident,
            "thread_name": self._thread.name,
        }

    def liveness_snapshot(self, hard_seconds: float) -> dict:
        """Return the content-free global-admission liveness state.

        A full reservation set is ordinary short-lived backpressure. It becomes
        a process failure only when the cap has remained full with no
        successful cleanup for the complete hard grace. A dead sole janitor is
        immediately unrecoverable even while idle: the next workspace could
        never release its reservation.
        """
        try:
            hard = float(hard_seconds)
        except (TypeError, ValueError):
            hard = 180.0
        if not math.isfinite(hard) or hard <= 0.0:
            hard = 180.0
        stats = self.stats()
        thread_dead = not bool(stats.get("thread_alive"))
        saturated_stuck = bool(
            stats.get("cleanup_saturated")
            and isinstance(stats.get("saturated_seconds"), (int, float))
            and stats["saturated_seconds"] >= hard
            and isinstance(stats.get("no_progress_seconds"), (int, float))
            and stats["no_progress_seconds"] >= hard
        )
        return {
            "healthy": not (thread_dead or saturated_stuck),
            "stuck": thread_dead or saturated_stuck,
            "thread_alive": not thread_dead,
            "capacity": stats["capacity"],
            "reserved": stats["reserved"],
            "leased": stats["leased"],
            "pending": stats["pending"],
            "active": stats["active"],
            "quarantined": stats["quarantined"],
            "saturated": stats["saturated"],
            "cleanup_saturated": stats["cleanup_saturated"],
            "active_seconds": stats["active_seconds"],
            "saturated_seconds": stats["saturated_seconds"],
            "no_progress_seconds": stats["no_progress_seconds"],
            "hard_seconds": hard,
        }


_JANITOR = GraphWorkspaceJanitor(GRAPH_WORKSPACE_CLEANUP_CAP)


def reserve_graph_workspace() -> _WorkspaceLease:
    """Reserve and create one production graph workspace."""
    return _JANITOR.reserve()


def janitor_stats() -> dict:
    return _JANITOR.stats()


def janitor_liveness(hard_seconds: float) -> dict:
    return _JANITOR.liveness_snapshot(hard_seconds)
