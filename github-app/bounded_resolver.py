#!/usr/bin/env python3
"""Bounded, deadline-aware hostname resolution shared by outbound transports.

``socket.getaddrinfo`` is not bounded by a socket timeout: libc/NSS may remain
inside DNS resolution long after an HTTP or PostgreSQL connect timeout has
expired.  Starting one helper thread per lookup only moves the leak -- every
timed-out lookup can leave another permanently blocked thread behind.

This module instead owns one process-wide, fixed-size daemon pool and a bounded
pending queue.  Callers wait only until an absolute ``time.monotonic()``
deadline.  A wedged libc resolver can consume at most the fixed worker count;
repeated callers cannot create more threads or grow an unbounded queue.

The API deliberately accepts and returns the standard ``getaddrinfo`` shape so
HTTP and DB transports can share it while retaining their own Host/SNI/DSN
semantics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import queue
import socket
import threading
import time
from typing import Callable


DEFAULT_WORKERS = 4
DEFAULT_MAX_PENDING = 64
DEFAULT_MAX_CACHE_ENTRIES = 256
DEFAULT_TTL_SECONDS = 60.0
DEFAULT_NEGATIVE_TTL_SECONDS = 2.0


class ResolutionDeadlineExceeded(TimeoutError):
    """The caller's absolute deadline expired before resolution completed."""


class ResolutionCapacityExceeded(RuntimeError):
    """Every resolver worker/pending slot is already occupied."""


@dataclass
class _Lookup:
    key: tuple
    args: tuple
    done: threading.Event = field(default_factory=threading.Event)
    result: tuple | None = None
    error: BaseException | None = None


class BoundedResolver:
    """A fixed-size resolver pool with in-flight de-duplication and TTL cache."""

    def __init__(
        self,
        *,
        workers: int = DEFAULT_WORKERS,
        max_pending: int = DEFAULT_MAX_PENDING,
        max_cache_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        negative_ttl_seconds: float = DEFAULT_NEGATIVE_TTL_SECONDS,
        resolver_fn: Callable | None = None,
        thread_name_prefix: str = "veripsa-dns-resolver",
    ):
        if int(workers) < 1:
            raise ValueError("workers must be >= 1")
        if int(max_pending) < 1:
            raise ValueError("max_pending must be >= 1")
        if int(max_cache_entries) < 1:
            raise ValueError("max_cache_entries must be >= 1")
        self._worker_count = int(workers)
        self._queue: queue.Queue[_Lookup] = queue.Queue(maxsize=int(max_pending))
        self._max_cache_entries = int(max_cache_entries)
        self._ttl = max(0.0, float(ttl_seconds))
        self._negative_ttl = max(0.0, float(negative_ttl_seconds))
        # None intentionally means resolve ``socket.getaddrinfo`` dynamically.
        # Tests and embedders can monkey-patch it before a queued lookup starts.
        self._resolver_fn = resolver_fn
        self._thread_name_prefix = str(thread_name_prefix)
        self._lock = threading.Lock()
        self._started = False
        self._threads: list[threading.Thread] = []
        self._inflight: dict[tuple, _Lookup] = {}
        # key -> (expires_monotonic, result, error)
        self._cache: dict[tuple, tuple[float, tuple | None, BaseException | None]] = {}

    def _ensure_started_locked(self) -> None:
        if self._started:
            return
        self._started = True
        for index in range(self._worker_count):
            thread = threading.Thread(
                target=self._worker,
                name=f"{self._thread_name_prefix}-{index + 1}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def _worker(self) -> None:
        while True:
            lookup = self._queue.get()
            try:
                resolver = self._resolver_fn or socket.getaddrinfo
                try:
                    result = tuple(resolver(*lookup.args))
                    if not result:
                        raise socket.gaierror(socket.EAI_NONAME, "hostname resolved to no addresses")
                    error = None
                # A resolver implementation is an injected/OS boundary. One
                # unexpected BaseException must complete this lookup and leave
                # the fixed worker available; otherwise `_started` stays true
                # while the pool silently loses a thread and its `_inflight`
                # entry remains pinned forever.
                except BaseException as exc:
                    result = None
                    error = exc

                now = time.monotonic()
                ttl = self._ttl if error is None else self._negative_ttl
                with self._lock:
                    # Only this exact lookup may publish/remove its in-flight
                    # entry; the guard makes future cancellation extensions safe.
                    if self._inflight.get(lookup.key) is lookup:
                        self._inflight.pop(lookup.key, None)
                    if ttl > 0:
                        # Dicts preserve insertion order. Purge expired entries
                        # first, then evict oldest entries so arbitrary hostnames
                        # cannot turn the TTL cache into an unbounded map.
                        for key, cached in list(self._cache.items()):
                            if cached[0] <= now:
                                self._cache.pop(key, None)
                        while len(self._cache) >= self._max_cache_entries:
                            self._cache.pop(next(iter(self._cache)))
                        self._cache[lookup.key] = (now + ttl, result, error)
                    lookup.result = result
                    lookup.error = error
                    lookup.done.set()
            finally:
                self._queue.task_done()

    def resolve(
        self,
        host: str,
        port,
        *,
        deadline: float,
        family: int = socket.AF_UNSPEC,
        socktype: int = socket.SOCK_STREAM,
        proto: int = 0,
        flags: int = 0,
    ) -> tuple:
        """Resolve ``host`` before the absolute monotonic ``deadline``.

        The deadline bounds queueing and waiting, not libc itself.  libc runs
        only on the fixed worker pool, so an uninterruptible resolver cannot
        multiply leaked threads.  Concurrent identical lookups share one task.
        Native resolver errors (notably ``socket.gaierror``) are re-raised.
        """
        absolute = float(deadline)
        if absolute - time.monotonic() <= 0:
            raise ResolutionDeadlineExceeded(
                f"hostname resolution deadline expired for {host!r}")

        key = (str(host), port, int(family), int(socktype), int(proto), int(flags))
        args = key
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                expires, result, error = cached
                if expires > time.monotonic():
                    if absolute - time.monotonic() <= 0:
                        raise ResolutionDeadlineExceeded(
                            f"hostname resolution deadline expired for {host!r}")
                    if error is not None:
                        raise error
                    return result or ()
                self._cache.pop(key, None)

            lookup = self._inflight.get(key)
            if lookup is None:
                lookup = _Lookup(key=key, args=args)
                self._inflight[key] = lookup
                self._ensure_started_locked()
                try:
                    self._queue.put_nowait(lookup)
                except queue.Full:
                    self._inflight.pop(key, None)
                    raise ResolutionCapacityExceeded(
                        "bounded hostname resolver is at capacity")

        remaining = absolute - time.monotonic()
        if remaining <= 0 or not lookup.done.wait(remaining):
            raise ResolutionDeadlineExceeded(
                f"hostname resolution deadline expired for {host!r}")
        if absolute - time.monotonic() <= 0:
            raise ResolutionDeadlineExceeded(
                f"hostname resolution deadline expired for {host!r}")
        if lookup.error is not None:
            raise lookup.error
        return lookup.result or ()

    def clear_cache(self) -> None:
        """Clear completed entries; in-flight calls remain safely de-duplicated."""
        with self._lock:
            self._cache.clear()

    def stats(self) -> dict:
        """Content-free diagnostics/tests; never includes hostnames or addresses."""
        with self._lock:
            return {
                "workers_configured": self._worker_count,
                "threads_started": len(self._threads),
                "threads_alive": sum(thread.is_alive() for thread in self._threads),
                "pending": self._queue.qsize(),
                "inflight": len(self._inflight),
                "cached": len(self._cache),
            }


_SHARED_RESOLVER = BoundedResolver()


def resolve(
    host: str,
    port,
    *,
    deadline: float,
    family: int = socket.AF_UNSPEC,
    socktype: int = socket.SOCK_STREAM,
    proto: int = 0,
    flags: int = 0,
) -> tuple:
    """Resolve through the process-wide bounded pool."""
    return _SHARED_RESOLVER.resolve(
        host,
        port,
        deadline=deadline,
        family=family,
        socktype=socktype,
        proto=proto,
        flags=flags,
    )


def clear_cache() -> None:
    _SHARED_RESOLVER.clear_cache()


def stats() -> dict:
    return _SHARED_RESOLVER.stats()
