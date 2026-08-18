#!/usr/bin/env python3
"""Veripsa GitHub App — the GitHub REST client (App-JWT auth → installation token → REST calls).
Split out of server.py so the thin webhook shell and the I/O client stay separate concerns.
(one file = one holder was the contention bottleneck)."""
from __future__ import annotations

import json
import os
import time   # module-level: _read_through_deadline + _req/_req_with_link use time.monotonic() for the total-call deadline
import contextvars as _contextvars
from contextlib import contextmanager as _contextmanager

# env_int: the VALIDATED env-knob reader (a non-int or <1 VERIPSA_MAX_TARBALL_BYTES fails LOUDLY at start,
# naming the var, instead of crashing bare or silently disabling all ingest with a 0/negative cap).
try:
    from env_config import env_int
except ImportError:  # imported as a package
    from .env_config import env_int
try:
    import event_budget as _event_budget
except ImportError:  # imported as a package
    from . import event_budget as _event_budget
try:
    import bounded_resolver as _bounded_resolver
except ImportError:  # imported as a package
    from . import bounded_resolver as _bounded_resolver
try:
    from delivery_deferral import github_rate_limit_deferral as _github_rate_limit_deferral
except ImportError:  # imported as a package
    from .delivery_deferral import github_rate_limit_deferral as _github_rate_limit_deferral
# alerts: the fail-open, edge-triggered alert sink — used by list_app_installations (now in
# github_rest_installs._GitHubInstallationsMixin, which reads this module-level singleton back via a lazy import)
# to page the operator when the App has reached the installations cap (a silent scale ceiling that degrades
# background reconciliation for new tenants). Same dual standalone/package import idiom as the rest of the App.
try:
    from alerts import AlertSink as _AlertSink  # noqa: E402
except ImportError:  # imported as a package
    from .alerts import AlertSink as _AlertSink  # noqa: E402
# Module-level singleton AlertSink used by list_app_installations (the installations mixin resolves it from here
# lazily — see _gh_installs_helpers). Reads VERIPSA_ALERT_WEBHOOK_URL + VERIPSA_ALERT_MIN_INTERVAL from the env
# at first import, exactly as alerts.py documents.
_alert_sink: _AlertSink = _AlertSink()
try:
    from github_rest_prsurface import _GitHubPRSurfaceMixin  # PR-write-surface (checks/comments/labels)
except ImportError:  # imported as a package
    from .github_rest_prsurface import _GitHubPRSurfaceMixin
try:
    from github_rest_contentfetch import _GitHubContentFetchMixin  # content-fetch surface (tarball/file/clone)
except ImportError:  # imported as a package
    from .github_rest_contentfetch import _GitHubContentFetchMixin
try:
    from github_rest_prread import _GitHubPRReadMixin  # PR-read surface (list/get PRs, files, diff ranges)
except ImportError:  # imported as a package
    from .github_rest_prread import _GitHubPRReadMixin
try:
    from github_rest_installs import _GitHubInstallationsMixin  # installations + account-resolution + repo metadata
except ImportError:  # imported as a package
    from .github_rest_installs import _GitHubInstallationsMixin


# COST/SCALE GUARD: a repo tarball is loaded into memory before extraction, so a giant repo (a big monorepo)
# could OOM a small/cheap host. Bound the in-memory tarball; over the cap we raise a clear error and the caller
# records the coordinate as 'too large to index' (honest 'unknown') rather than crashing the box. Configurable
# upward for a bigger instance / paid tier.
#
# DEFAULT TUNED FOR THE STARTER TIER (512 MiB box). A full ingest holds, AT ONCE: the compressed tarball bytes
# (≤ this cap), build_graph's whole result, AND json.dumps(graph) (a second full copy as a string) — so the cap
# must leave headroom for the interpreter + that graph/JSON on top. 300 MiB (the old default) ALONE was > half
# of a 512 MiB box → a mid-size real repo could OOM the starter instance, and the CUSTOMER doesn't know to set
# an env var. So the safe default is 64 MiB: a 64 MiB COMPRESSED tarball is already a large source repo (source
# compresses ~5–10×), comfortably indexable in 512 MiB, and a repo over it gets an honest 'unknown' (never a
# crash). Raise VERIPSA_MAX_TARBALL_BYTES for a bigger paid instance (e.g. 300 MiB on a ≥2 GiB box).
_MAX_TARBALL_BYTES = env_int("VERIPSA_MAX_TARBALL_BYTES", 64 * 1024 * 1024, min_value=1)


# RESILIENCE GUARD: every outbound GitHub call MUST have a socket timeout. The App drains its webhook queue on a
# SINGLE worker thread, so one urlopen on a hung/half-open socket (a stalled CDN, a black-holed connection — no
# RST, no FIN, just silence) would block that worker FOREVER, freezing ingest for EVERY tenant. And the hang is
# invisible to recovery: worker_alive stays true (the thread isn't dead, just parked in a syscall), so the
# watchdog never auto-restarts it. A bare urlopen() defaults to NO timeout = exactly this failure. With a timeout
# the stalled read raises urllib.error.URLError, which _req/_retry_wait already treat as retryable → the worker
# counts the event failed, Core's durable inbox retries it, and the queue keeps draining. Env-tunable for a slow link / big
# tarball; min 1s (a 0/negative timeout is not a meaningful bound — it would mean 'never' or fail instantly).
_HTTP_TIMEOUT = env_int("GITHUB_HTTP_TIMEOUT", 25, min_value=1)


# TOTAL-CALL DEADLINE (the actual prod-hang fix). _HTTP_TIMEOUT above is a PER-SOCKET-OPERATION timeout: urllib
# applies it to the connect AND to each individual recv() — but NOT to the WHOLE call. So a socket that is not
# dead but TRICKLING (a stalled/half-open codeload CDN that dribbles a byte every <_HTTP_TIMEOUT seconds and never
# sends FIN/RST — the exact "non-DB hang at the GitHub/Python level" we saw in prod: inflight_age climbed past
# 400s while pg_stat_activity was EMPTY) keeps EVERY individual read() under the per-op timeout, so the chunked
# read loop in _read_capped / _req's r.read() NEVER times out and the single drain worker is wedged on ONE event
# UNBOUNDED. (Proven: a 1s per-op timeout does not bound a loop reading a 0.4s-drip body — it runs forever.) The
# per-op timeout is necessary but NOT sufficient; the only bound that actually caps a trickle is a TOTAL wall-clock
# DEADLINE on the whole request (connect + all reads). _read_through_deadline enforces it on every body read.
# One absolute deadline is opened for the complete LOGICAL call — token mint, network attempts, retry sleeps,
# redirects, and the one reactive auth-remint retry all consume the SAME budget. The current webhook event budget
# caps it. This prevents the old 30s-per-attempt × retries × auth-remint multiplication.
#
# DEFAULT 30s: comfortably ABOVE normal GitHub latency (healthy REST calls finish in low single-digit seconds;
# even a cold full-repo tarball download completes well inside this on a live link), so a healthy event NEVER
# false-trips — only a genuinely stalled/black-holed socket hits the deadline. Tunable upward for a very large
# tarball on a slow link via VERIPSA_GITHUB_HTTP_TIMEOUT; min 1s (0/negative = "never", the unbounded hole this
# closes — env_int refuses it LOUD). Kept >= the per-op timeout in spirit (a total deadline below the per-op one
# would be self-defeating); the operator tunes both together for a slow link.
_HTTP_TOTAL_TIMEOUT = env_int("VERIPSA_GITHUB_HTTP_TIMEOUT", 30, min_value=1)
# A same-installation token mint intentionally holds one RLock across its
# network call so concurrent workers publish exactly one token. Waiting for
# that lock must still be bounded. Background callers get a small grace over
# the mint's own logical HTTP ceiling; event callers are capped by their
# event/logical remaining time.
_TOKEN_LOCK_WAIT_SECONDS = float(_HTTP_TOTAL_TIMEOUT + 5)
_UNCONDITIONAL_TOKEN_INVALIDATION = object()

# Error/redirect bodies are diagnostic metadata, never application payload. Keeping at most 64 KiB is enough for
# GitHub's JSON error envelope and prevents a hostile or broken peer from forcing an unbounded in-memory drain.
_MAX_HTTP_ERROR_BODY_BYTES = 64 * 1024

# Keep the public/test transport seam as `_urlopen(req)` with one argument. The deadline rides on the Request as
# private metadata so fakes remain source-compatible while the real urllib/keepalive transports cap connect and
# getresponse to the same remaining budget.
_REQUEST_DEADLINE_ATTR = "_veripsa_logical_deadline"
_REQUEST_NO_REDIRECT_ATTR = "_veripsa_no_redirect"
_LOGICAL_CALL_DEADLINE: _contextvars.ContextVar[float | None] = _contextvars.ContextVar(
    "veripsa_github_logical_deadline", default=None,
)


class _GitHubCallDeadlineExceeded(TimeoutError):
    """One logical GitHub operation exhausted its bounded wall-clock budget."""


def _logical_deadline_error(message: str):
    """Build the terminal error for a spent GitHub logical-call deadline.

    Outside webhook processing this remains an ordinary TimeoutError so
    background/operator callers keep their local timeout contract. Inside an
    event, a stage-local timeout is terminal for that delivery even when the
    event's larger wall budget has time left. Surface EventBudgetExceeded so
    fail-open ``except Exception`` boundaries cannot swallow the cancellation
    and commit work performed after GitHub's liveness boundary.
    """
    if _event_budget.remaining() is not None:
        return _event_budget.EventBudgetExceeded(message)
    return _GitHubCallDeadlineExceeded(message)


def _raise_if_logical_deadline_expired(deadline: float) -> None:
    """Raise the event cancellation in-event; retain TimeoutError in background."""
    _event_budget.raise_if_expired()
    if deadline - time.monotonic() <= 0:
        raise _logical_deadline_error(
            f"github logical call exceeded its {_HTTP_TOTAL_TIMEOUT}s wall-clock budget")


def _timeout_through_deadline(deadline: float, local_seconds: float) -> float:
    """A socket/subprocess timeout capped by both the logical-call and current event deadlines."""
    _raise_if_logical_deadline_expired(deadline)
    remaining = deadline - time.monotonic()
    # timeout_for also observes a dynamically nested/shortened event budget. Never pass zero to a socket API:
    # zero means non-blocking, not "bounded by an already-expired budget".
    return _event_budget.timeout_for(min(float(local_seconds), remaining), floor=0.001)


def _deadline_for_request(req) -> float:
    deadline = getattr(req, _REQUEST_DEADLINE_ATTR, None)
    if deadline is None:
        current = _LOGICAL_CALL_DEADLINE.get()
        deadline = current if current is not None else _event_budget.deadline_for(_HTTP_TOTAL_TIMEOUT)
        try:
            setattr(req, _REQUEST_DEADLINE_ATTR, deadline)
        except Exception:
            pass
    return float(deadline)


@_contextmanager
def _logical_call_scope():
    """Open one deadline, or inherit the caller's, across nested token/auth/tarball operations."""
    existing = _LOGICAL_CALL_DEADLINE.get()
    if existing is not None:
        _raise_if_logical_deadline_expired(existing)
        yield existing
        return
    deadline = _event_budget.deadline_for(_HTTP_TOTAL_TIMEOUT)
    token = _LOGICAL_CALL_DEADLINE.set(deadline)
    try:
        _raise_if_logical_deadline_expired(deadline)
        yield deadline
    finally:
        _LOGICAL_CALL_DEADLINE.reset(token)


def _retry_sleep(client, wait_seconds: float, deadline: float) -> None:
    """Sleep only when the intended retry can start inside both wall-clock budgets."""
    wait = max(0.0, float(wait_seconds))
    _raise_if_logical_deadline_expired(deadline)
    if not _event_budget.can_retry(wait):
        raise _event_budget.EventBudgetExceeded(
            "webhook event budget cannot accommodate the GitHub retry wait")
    if wait >= deadline - time.monotonic():
        raise _logical_deadline_error(
            "github logical call budget cannot accommodate the retry wait")
    client._sleep(wait)
    _raise_if_logical_deadline_expired(deadline)


def _raise_event_rate_limit_deferral(error, deadline: float) -> None:
    """Move a positive GitHub rate-limit wait out of an active event worker."""
    if _event_budget.remaining() is None:
        return
    # Cancellation has stronger semantics than scheduling. Check both before
    # and after header parsing so a deadline crossing is never overwritten by
    # an ordinary IntentionalDeliveryDeferral.
    _raise_if_logical_deadline_expired(deadline)
    defer = _github_rate_limit_deferral(error)
    if defer is None:  # non-rate-limit, or a valid zero-second immediate retry
        return
    _raise_if_logical_deadline_expired(deadline)
    raise defer


@_contextmanager
def _token_lock_scope(client):
    """Acquire one installation's token lock without crossing active budgets."""
    now = time.monotonic()
    acquire_deadline = now + _TOKEN_LOCK_WAIT_SECONDS
    logical_deadline = _LOGICAL_CALL_DEADLINE.get()
    if logical_deadline is not None:
        acquire_deadline = min(acquire_deadline, float(logical_deadline))
    event_remaining = _event_budget.remaining()
    if event_remaining is not None:
        acquire_deadline = min(acquire_deadline, now + event_remaining)

    remaining = acquire_deadline - time.monotonic()
    if remaining <= 0:
        if logical_deadline is not None:
            _raise_if_logical_deadline_expired(logical_deadline)
        _event_budget.raise_if_expired()
        raise _logical_deadline_error(
            "github installation-token lock wait exceeded its wall-clock budget")

    acquired = client._token_lock.acquire(timeout=remaining)
    if not acquired:
        if logical_deadline is not None:
            _raise_if_logical_deadline_expired(logical_deadline)
        _event_budget.raise_if_expired()
        raise _logical_deadline_error(
            "github installation-token lock wait exceeded its wall-clock budget")
    try:
        # A lock can become available just after the deadline. Never accept
        # that late acquisition as permission to mint or return success.
        if logical_deadline is not None:
            _raise_if_logical_deadline_expired(logical_deadline)
        _event_budget.raise_if_expired()
        if time.monotonic() >= acquire_deadline:
            raise _logical_deadline_error(
                "github installation-token lock was acquired after its wall-clock budget")
        yield
    finally:
        client._token_lock.release()


def _read_through_deadline(resp, deadline, chunk=None):
    """Read a response body to EOF (or up to one `chunk` bytes) while enforcing a TOTAL wall-clock `deadline`
    (a time.monotonic() epoch seconds) across the WHOLE read — the bound a per-recv socket timeout does NOT give.

    WHY A PER-RECV READ (the crux): a plain resp.read(n) BLOCKS until it has n bytes (or EOF). Against a steady
    TRICKLE (a stalled CDN dribbling a byte every < the per-op timeout, never closing) NO single recv ever gaps
    long enough to trip the socket timeout, so that one read(n) accumulates bytes FOREVER — the production hang.
    The only thing that bounds a steady trickle is to (a) read ONE recv at a time (resp.read1 returns after a
    SINGLE underlying recv — verified: it returns the 1 trickled byte immediately, it does NOT wait for n), and
    (b) re-check the monotonic deadline AFTER EVERY recv, raising once the budget is spent. We also shrink the
    socket's per-op timeout to the remaining budget so a recv that genuinely stalls (a true gap) can't itself
    overshoot the total. Together this turns the UNBOUNDED slow-drip read into a BOUNDED failure (socket.timeout,
    caught as a retryable transient by _req/_req_with_link/download_tarball's callers → the event fails cleanly →
    the durable inbox bounds it → the worker proceeds to the next event).

    `chunk` None → read to EOF, returning the whole body (used by _req for small JSON bodies). `chunk` an int →
    return AT MOST that many bytes (the streamed cap loop in _read_capped calls this per 1 MiB so its own size
    guard still applies per chunk). DEGRADES GRACEFULLY for a test double whose response is not a real socket
    (no read1 / a no-arg read()): it falls back to a single bounded read() — a mock has no socket to trickle and
    returns its whole body at once, so behaviour for tests is unchanged. NEVER masks an empty deadline: with
    `deadline=None` it is a plain read (back-compat for any caller that doesn't opt in)."""
    import socket as _socket
    if deadline is None:                                  # no deadline requested → plain read (back-compat)
        return resp.read() if chunk is None else resp.read(chunk)

    def _set_sock_timeout(t):
        # Best-effort: tighten the underlying socket's per-op timeout to the remaining budget so even a single
        # genuinely-stalled recv cannot exceed the TOTAL deadline. urllib's response wraps an http.client response
        # whose .fp is a socket file object; .fp.raw._sock (Py3) is the socket. Any miss is non-fatal — the
        # per-recv deadline check below still bounds the loop (a mock with no real socket simply skips this).
        try:
            sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(max(t, 0.001))           # never <=0 (which would mean non-blocking / instant fail)
        except Exception:
            pass

    def _expired_timeout():
        return _logical_deadline_error(
            f"github call exceeded the {_HTTP_TOTAL_TIMEOUT}s total deadline (stalled/trickling socket) — "
            f"raise VERIPSA_GITHUB_HTTP_TIMEOUT if a slow link legitimately needs longer")

    # The per-recv reader: resp.read1(n) returns after ONE recv (so a trickle yields control immediately and we
    # re-check the deadline), which is exactly what bounds a steady drip. A test double without read1 (or whose
    # read() takes no size) has no socket to trickle → fall back to one whole-body read() under the deadline.
    _read1 = getattr(resp, "read1", None)
    if not callable(_read1):
        _event_budget.raise_if_expired()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _expired_timeout()
        event_remaining = _event_budget.remaining()
        if event_remaining is not None:
            remaining = min(remaining, event_remaining)
        _set_sock_timeout(remaining)
        try:
            data = resp.read() if chunk is None else resp.read(chunk)
        except TypeError:                                 # a no-arg-read() mock ignores the size — whole body at once
            data = resp.read()
        except _socket.timeout:
            # The socket's inactivity timer and our absolute deadline commonly
            # fire together. Classify the latter as event cancellation in-event.
            _raise_if_logical_deadline_expired(deadline)
            raise
        # A blocking read may finish just after the stage-local deadline. Re-check
        # after it returns so a late EOF cannot be accepted as successful work.
        _raise_if_logical_deadline_expired(deadline)
        return data

    want = chunk if chunk is not None else -1             # -1 = read to EOF; else stop at `want` bytes
    out = []
    got = 0
    while want < 0 or got < want:
        _event_budget.raise_if_expired()
        remaining = deadline - time.monotonic()
        if remaining <= 0:                                # TOTAL budget spent → bounded failure (retryable upstream)
            raise _expired_timeout()
        event_remaining = _event_budget.remaining()
        if event_remaining is not None:
            remaining = min(remaining, event_remaining)
        _set_sock_timeout(remaining)                      # a true recv-gap can't outlast the remaining budget
        ask = (1 << 16) if want < 0 else min(1 << 16, want - got)   # 64 KiB per recv slice (re-check often)
        try:
            b = _read1(ask)
        except _socket.timeout:
            _raise_if_logical_deadline_expired(deadline)
            raise
        # Check AFTER every blocking recv too. This is load-bearing when the
        # logical GitHub limit is shorter than the containing event budget.
        _raise_if_logical_deadline_expired(deadline)
        if not b:                                         # EOF
            break
        out.append(b)
        got += len(b)
    return b"".join(out)


# SCALE GUARD (multi-tenant public Marketplace App): GET /app/installations is paginated at 100/page. Without a
# hard ceiling a huge App (thousands of tenants) makes legacy/account resolvers list EVERY page before their
# background loop can start. Production boot_reconcile uses the durable DB route cursor instead. The cap bounds
# this resolver budget. DEFAULT 500 (enough for a comfortable early-Marketplace footprint). RAISE for a bigger fleet:
# install count. A 0 or negative value is nonsensical (a cap of 0 would silently drop ALL installations from
# the background path — exactly the DoS-by-misconfiguration env_int was designed to catch). Fail LOUD on bad
# values: the operator needs to know to fix the config, not silently run on a broken cap.
#
# ALERT: when the list saturates (returned count ≥ cap) the App is hitting the ceiling — an account-level miss
# cannot prove absence. A content-free WARNING is emitted so the operator can raise the cap or use exact
# installation routing. Boot reconciliation and the LIVE WEBHOOK path are unaffected (DB cursor / payload id).
_APP_INSTALLATIONS_CAP = env_int("VERIPSA_APP_INSTALLATIONS_CAP", 500, min_value=1)


# HTTP KEEP-ALIVE (round-2 perf follow-up): every GitHub REST call from _urlopen used to open a FRESH TCP+TLS
# handshake (~80–150 ms per call to api.github.com — measurable on a free/starter box). The App makes many calls
# per event (list PRs · per-PR files+diff · check upserts · comments · labels · access-token mints), so the
# handshake overhead dominates a small webhook's latency on the free tier. Reusing ONE persistent connection per
# (host, port) cuts the handshake cost to one per worker-thread-lifetime. THREAD-LOCAL by design: server.py runs
# the webhook handlers on a ThreadingHTTPServer (multiple concurrent threads), and http.client.HTTPSConnection is
# NOT thread-safe (a single connection serialises request→response with an internal _HTTPConnection.__state lock,
# so two threads sharing one connection would interleave wire bytes). A threading.local() pool gives each thread
# its OWN cached connection — no cross-thread contention, no lock, no risk of interleaved frames. KILL SWITCH:
# VERIPSA_HTTP_KEEPALIVE=0 uses the SAME deadline-safe http.client transport but closes each connection after one
# response. This preserves the operational escape hatch (no pooled/stale reuse) without falling back to urllib's
# header parser, whose per-recv timeout can be defeated by a peer trickling one header byte at a time. MAX AGE:
# a cached connection past this age is recycled before reuse — bounds the window in
# which a silently-half-closed socket (a stale NAT/LB entry GitHub closed without telling us) can wedge the worker
# (the per-op timeout + total deadline still bound a wedge, but recycling proactively avoids the retry pain).
# Default 60s: comfortably below typical keep-alive timeouts (api.github.com keeps connections ~90 s) so we
# refresh BEFORE the remote side does, avoiding the "connection reset on first reuse" trip.
_HTTP_KEEPALIVE = env_int("VERIPSA_HTTP_KEEPALIVE", 1, min_value=0)            # 1=on (default); 0=off (kill switch)
_HTTP_KEEPALIVE_MAX_AGE = env_int("VERIPSA_HTTP_KEEPALIVE_MAX_AGE_SECONDS", 60, min_value=1)

# USER-AGENT (GitHub REQUIRES one — a request without it gets 403 "Request forbidden by administrative rules.
# Please make sure your request has a User-Agent header"). urllib.request.urlopen auto-injects a default UA at
# open time; http.client (the keep-alive path _urlopen_keepalive below) does NOT — so EVERY call on the keep-alive
# path must carry an explicit UA or GitHub 403s it. We set it on the Request at construction (so BOTH the urllib
# and keep-alive paths emit it) AND default it in _urlopen_keepalive (so a future Request site that forgets can
# never regress). GitHub asks the UA be the app/username; the App slug is content-free. (2026-06-27 incident fix.)
_USER_AGENT = os.environ.get("VERIPSA_USER_AGENT", "veripsa-core")
_KEEPALIVE_MAX_REDIRECTS = 10

# Per-thread cache: each thread keeps its OWN dict {(host, port, scheme): (HTTP[S]Connection, created_monotonic)}.
# threading.local guarantees thread-local storage; no inter-thread sharing (so no locking on the cache itself).
import threading as _threading
_http_pool = _threading.local()


def _shutdown_deadline_connection(conn) -> None:
    """Interrupt a blocking request/status/header read from another thread.

    ``HTTPConnection.close()`` alone is not a reliable cross-thread interrupt on every platform. Shutdown first
    so a thread parked in ``recv()`` wakes immediately, then close. The request thread removes the poisoned
    connection from its own thread-local pool after the blocked operation returns.
    """
    import socket as _socket
    try:
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(_socket.SHUT_RDWR)
            except (OSError, ValueError):
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


class _HeaderDeadlineLease:
    """One request/status/header phase registered with the shared watchdog."""

    def __init__(self, owner, conn):
        self._owner = owner
        self._conn = conn
        self._active = True
        self._expired = False

    def cancel(self) -> None:
        with self._owner._cv:
            self._active = False
            self._owner._cv.notify()

    def expired(self) -> bool:
        with self._owner._cv:
            return self._expired


class _HTTPHeaderDeadlineWatchdog:
    """One daemon scheduler for all absolute request/header deadlines.

    Socket timeouts are inactivity timeouts. ``http.client.getresponse()`` reads status/header lines with
    ``readline()``, so a peer can send one byte before every socket timeout and keep that call alive forever.
    This separate scheduler interrupts the blocked recv at an ABSOLUTE deadline. A shared thread avoids creating
    one Timer thread per GitHub request.
    """

    def __init__(self):
        self._cv = _threading.Condition()
        self._heap = []
        self._sequence = 0
        self._thread = None

    def arm(self, conn, deadline: float) -> _HeaderDeadlineLease:
        import heapq
        lease = _HeaderDeadlineLease(self, conn)
        with self._cv:
            self._sequence += 1
            heapq.heappush(self._heap, (float(deadline), self._sequence, lease))
            if self._thread is None or not self._thread.is_alive():
                self._thread = _threading.Thread(
                    target=self._run,
                    name="veripsa-http-header-deadline",
                    daemon=True,
                )
                self._thread.start()
            self._cv.notify()
        return lease

    def _run(self) -> None:
        import heapq
        while True:
            expired_lease = None
            with self._cv:
                while expired_lease is None:
                    while self._heap and not self._heap[0][2]._active:
                        heapq.heappop(self._heap)
                    if not self._heap:
                        self._cv.wait()
                        continue
                    deadline, _sequence, lease = self._heap[0]
                    delay = deadline - time.monotonic()
                    if delay > 0:
                        self._cv.wait(delay)
                        continue
                    heapq.heappop(self._heap)
                    if lease._active:
                        lease._active = False
                        lease._expired = True
                        expired_lease = lease
            # Never hold the scheduler lock across socket operations. An odd close cannot delay unrelated
            # tenants from arming or canceling their deadlines.
            _shutdown_deadline_connection(expired_lease._conn)


_HTTP_HEADER_DEADLINE_WATCHDOG = _HTTPHeaderDeadlineWatchdog()


def _close_keepalive_connection(conn) -> None:
    """Best-effort close a cached HTTP[S]Connection — used on age-recycle, on a transient error, and on test
    teardown. Never raises (close() on an already-broken socket can raise OSError; we don't want a teardown to
    mask the original error)."""
    try:
        conn.close()
    except Exception:
        pass


def _reset_keepalive_pool() -> None:
    """Drop EVERY cached connection on the current thread + close it. Called on a transient network error so the
    next call mints a fresh connection (a half-closed socket survived: e.g. the LB closed it without FIN, or a
    KeepAlive timeout fired on the remote side; the per-call timeout caught the wedge but the cached connection
    is now poisoned). Also used by tests to start each case from a clean cache."""
    cache = getattr(_http_pool, "cache", None)
    if not cache:
        return
    for conn, _at in list(cache.values()):
        _close_keepalive_connection(conn)
    cache.clear()


def _set_keepalive_timeout(conn, timeout: float, deadline: float | None = None) -> None:
    """Apply the current timeout/deadline to a future connect or an already-live pooled socket."""
    try:
        conn.timeout = timeout
    except Exception:
        pass
    if deadline is not None:
        try:
            conn._veripsa_connect_deadline = float(deadline)
        except Exception:
            pass
    try:
        sock = getattr(conn, "sock", None)
        if sock is not None:
            sock.settimeout(timeout)
    except Exception:
        pass


import http.client as _http_client
_ORIGINAL_HTTP_CONNECTION = _http_client.HTTPConnection
_ORIGINAL_HTTPS_CONNECTION = _http_client.HTTPSConnection


def _remaining_connection_timeout(conn) -> float:
    """Remaining absolute connect/TLS budget, capped by the configured socket timeout."""
    import socket as _socket
    deadline = getattr(conn, "_veripsa_connect_deadline", None)
    configured = getattr(conn, "timeout", _HTTP_TIMEOUT)
    if deadline is None:
        deadline = time.monotonic() + (
            float(configured)
            if isinstance(configured, (int, float))
            else float(_HTTP_TIMEOUT)
        )
        conn._veripsa_connect_deadline = deadline
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise _socket.timeout("github connect exceeded its absolute deadline")
    if isinstance(configured, (int, float)):
        remaining = min(remaining, float(configured))
    return max(0.001, remaining)


def _connect_resolved_socket(conn) -> None:
    """Resolve and connect while one absolute deadline covers every address.

    ``HTTPConnection.connect`` delegates to ``socket.create_connection``. That
    performs unbounded DNS first and only assigns ``conn.sock`` *after* connect,
    so the header watchdog cannot interrupt either phase. Here DNS waits on the
    fixed resolver pool, and each candidate socket is assigned to ``conn.sock``
    before ``connect()``. The existing watchdog can therefore shut down the
    exact live socket at any point in TCP connect.
    """
    import socket as _socket
    deadline = getattr(conn, "_veripsa_connect_deadline", None)
    if deadline is None:
        deadline = time.monotonic() + _remaining_connection_timeout(conn)
        conn._veripsa_connect_deadline = deadline
    try:
        addresses = _bounded_resolver.resolve(
            conn.host,
            conn.port,
            deadline=float(deadline),
            family=_socket.AF_UNSPEC,
            socktype=_socket.SOCK_STREAM,
        )
    except _bounded_resolver.ResolutionDeadlineExceeded as exc:
        raise _socket.timeout("github DNS resolution exceeded its absolute deadline") from exc
    except _bounded_resolver.ResolutionCapacityExceeded as exc:
        raise _socket.gaierror(
            _socket.EAI_AGAIN, "bounded DNS resolver is at capacity") from exc

    last_error = None
    address_count = len(addresses)
    for index, (family, socktype, proto, _canonname, sockaddr) in enumerate(addresses):
        # Fair-share the one remaining allowance across candidates. Giving the
        # first address all 30s makes an IPv6 blackhole prevent a healthy IPv4
        # fallback even though DNS returned both. Equal remaining shares keep
        # total time bounded while guaranteeing every candidate a turn.
        remaining = _remaining_connection_timeout(conn)
        candidates_left = max(1, address_count - index)
        timeout = max(0.001, remaining / candidates_left)
        candidate = _socket.socket(family, socktype, proto)
        conn.sock = candidate
        try:
            candidate.settimeout(timeout)
            source_address = getattr(conn, "source_address", None)
            if source_address:
                candidate.bind(source_address)
            candidate.connect(sockaddr)
            # Match socket.create_connection/http.client's latency setting.
            try:
                candidate.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
            except OSError:
                pass
            # Reject a connect that returned after the shared wall deadline.
            candidate.settimeout(_remaining_connection_timeout(conn))
            return
        except BaseException as exc:
            last_error = exc
            try:
                candidate.close()
            except Exception:
                pass
            if getattr(conn, "sock", None) is candidate:
                conn.sock = None
            # EventBudgetExceeded is intentionally migrating outside Exception.
            # Cleanup above is universal; cancellation/system exceptions must
            # never be treated as "try the next address".
            if not isinstance(exc, Exception):
                raise
            if float(deadline) - time.monotonic() <= 0:
                raise _socket.timeout(
                    "github TCP connect exceeded its absolute deadline") from exc
    if last_error is not None:
        raise last_error
    raise _socket.gaierror(_socket.EAI_NONAME, "hostname resolved to no addresses")


class _DeadlineHTTPConnection(_ORIGINAL_HTTP_CONNECTION):
    """HTTPConnection with bounded DNS and watchdog-visible TCP connect."""

    def connect(self):
        _connect_resolved_socket(self)
        if self._tunnel_host:
            self._tunnel()


class _DeadlineHTTPSConnection(_ORIGINAL_HTTPS_CONNECTION):
    """HTTPSConnection retaining the original hostname for Host/SNI/verify."""

    def connect(self):
        _connect_resolved_socket(self)
        server_hostname = self._tunnel_host or self.host
        if self._tunnel_host:
            self._tunnel()
        try:
            # Delay the handshake until the SSLSocket is published on the
            # connection. The watchdog then always sees the live TLS socket,
            # while server_hostname remains the original DNS name (never IP).
            wrapped = self._context.wrap_socket(
                self.sock,
                server_hostname=server_hostname,
                do_handshake_on_connect=False,
            )
            self.sock = wrapped
            wrapped.settimeout(_remaining_connection_timeout(self))
            wrapped.do_handshake()
            wrapped.settimeout(_remaining_connection_timeout(self))
        except BaseException:
            try:
                if self.sock is not None:
                    self.sock.close()
            except Exception:
                pass
            self.sock = None
            raise


def _new_http_connection(
    host: str,
    port: int,
    scheme: str,
    timeout: float,
    deadline: float | None = None,
):
    """Construct one deadline-aware connection; preserve the fake-constructor test seam."""
    effective_deadline = (
        time.monotonic() + float(timeout)
        if deadline is None
        else float(deadline)
    )
    if scheme == "https":
        constructor = _http_client.HTTPSConnection
        if constructor is _ORIGINAL_HTTPS_CONNECTION:
            conn = _DeadlineHTTPSConnection(host, port, timeout=timeout)
        else:
            # Tests/embedders historically monkey-patch this constructor.
            conn = constructor(host, port, timeout=timeout)
    else:
        constructor = _http_client.HTTPConnection
        if constructor is _ORIGINAL_HTTP_CONNECTION:
            conn = _DeadlineHTTPConnection(host, port, timeout=timeout)
        else:
            conn = constructor(host, port, timeout=timeout)
    _set_keepalive_timeout(conn, timeout, effective_deadline)
    return conn


def _get_keepalive_connection(
    host: str,
    port: int,
    scheme: str,
    timeout: float | None = None,
    deadline: float | None = None,
):
    """Return a pooled, per-thread HTTPConnection / HTTPSConnection for (host, port, scheme), recycling one past
    _HTTP_KEEPALIVE_MAX_AGE. Each call applies the configured _HTTP_TIMEOUT as the per-op socket timeout (so even
    a connection minted on a cold path inherits the same bound as the legacy per-call urllib.urlopen path)."""
    cache = getattr(_http_pool, "cache", None)
    if cache is None:
        cache = {}
        _http_pool.cache = cache
    key = (host, port, scheme)
    now = time.monotonic()
    entry = cache.get(key)
    if entry is not None:
        conn, created_at = entry
        if now - created_at > _HTTP_KEEPALIVE_MAX_AGE:
            # Past the max-age window — proactively recycle BEFORE a stale-connection reset surfaces as a URLError.
            _close_keepalive_connection(conn)
            cache.pop(key, None)
            entry = None
        else:
            _set_keepalive_timeout(
                conn,
                _HTTP_TIMEOUT if timeout is None else timeout,
                deadline,
            )
            return conn
    # Mint a fresh connection. The per-op timeout bounds connect() AND each recv (mirrors urllib's behaviour) so
    # a hung peer can never wedge the worker beyond _HTTP_TIMEOUT (the total-call deadline in _req still bounds the
    # whole call across reads). The connection is registered ONLY after a successful construction; a constructor
    # raise would otherwise leave a None entry.
    effective_timeout = _HTTP_TIMEOUT if timeout is None else timeout
    conn = _new_http_connection(
        host,
        port,
        scheme,
        effective_timeout,
        deadline=deadline,
    )
    cache[key] = (conn, now)
    return conn


def _drop_keepalive_connection(conn, pool_key) -> None:
    _close_keepalive_connection(conn)
    cache = getattr(_http_pool, "cache", None)
    if cache is not None and pool_key is not None:
        cache.pop(pool_key, None)


def _consume_keepalive_body(resp, conn, pool_key, deadline: float,
                            cap: int = _MAX_HTTP_ERROR_BODY_BYTES, *, keepalive: bool = True) -> bytes:
    """Deadline-read at most `cap` bytes; drop the connection when the body is larger or the read fails."""
    try:
        body = _read_through_deadline(resp, deadline, chunk=cap + 1)
    except BaseException:
        _drop_keepalive_connection(conn, pool_key)
        try:
            resp.close()
        except Exception:
            pass
        raise
    if len(body) > cap:
        # The response was not fully drained. It cannot safely remain in the keepalive pool.
        _drop_keepalive_connection(conn, pool_key)
        body = body[:cap]
    try:
        resp.close()
    except Exception:
        pass
    if not keepalive:
        _close_keepalive_connection(conn)
    return body


class _KeepAliveResponse:
    """A thin context-manager wrapper around an http.client.HTTPResponse that mirrors the urllib.urlopen response
    shape `_req`/`_req_with_link` already consume: it exposes `.read()` / `.read1(n)` / `.headers` / `.fp` (for
    _read_through_deadline's socket-timeout tightening) and supports `with self._urlopen(req) as r:`.

    THE WHOLE POINT: on `__exit__` we DRAIN any remaining body bytes (so the underlying connection is in a clean
    state to accept the next request) and CLOSE the HTTPResponse — but we DO NOT close the connection. The next
    call on the same thread re-uses it via _get_keepalive_connection. If `__exit__` is called with an exception
    (the read raised — a per-op timeout, a total-deadline raise, a torn body), we ALSO close the underlying
    connection: a connection mid-response is poisoned (the next getresponse() would read leftover frames and
    misalign), so the safe path is to drop it and let the next call mint a fresh one (a clean-slate fallback)."""

    def __init__(self, response, conn, pool_key, deadline, *, keepalive: bool = True):
        self._resp = response
        self._conn = conn
        self._pool_key = pool_key
        self._deadline = deadline
        self._keepalive = bool(keepalive)
        # Mirror the attributes urllib's response exposes, so the existing callers don't branch on the wrapper:
        # .headers (used by _req_with_link to read Link); .status (parity); .fp (used by _read_through_deadline's
        # socket-timeout tightener); .url (some loggers).
        self.headers = response.msg if response.msg is not None else getattr(response, "headers", None)
        self.status = response.status
        self.fp = getattr(response, "fp", None)

    def read(self, *a):
        return self._resp.read(*a)

    def read1(self, *a):
        # Some Python builds expose read1 on HTTPResponse only via the underlying fp; degrade gracefully — the
        # _read_through_deadline path already falls back to a single read() when read1 is missing.
        r1 = getattr(self._resp, "read1", None)
        if callable(r1):
            return r1(*a)
        raise AttributeError("read1 not supported on this response")

    def getheader(self, name, default=None):
        return self._resp.getheader(name, default)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            # The body read raised — the connection is in an indeterminate state (a partial body still on the
            # wire, or a half-closed socket). Drop it so a stale connection cannot be silently reused by the next
            # call on this thread; the next call mints a fresh one. Never mask the original exception.
            _drop_keepalive_connection(self._conn, self._pool_key)
            return False
        # Clean exit: ensure the body is fully consumed so the connection is ready for the next request. CPython's
        # http.client requires the previous response's body to be fully read before getresponse() can be called
        # for the next request on the same connection. _req already calls _read_through_deadline → reads the WHOLE
        # body; the close() below is a defensive drain in case a caller short-read.
        _consume_keepalive_body(
            self._resp,
            self._conn,
            self._pool_key,
            self._deadline,
            keepalive=self._keepalive,
        )
        return False


def _urlopen_httpclient(req, _redirects: int = 0, deadline: float | None = None, *,
                        keepalive: bool = True):
    """Deadline-safe http.client transport shared by pooled and one-shot modes.

    The watchdog covers ``request()`` plus ``getresponse()`` — the status/header phase that body-read checks
    cannot reach. Body reads remain bounded by ``_read_through_deadline``. With ``keepalive=False`` the exact same
    parser/watchdog is used but the connection is closed after this response, preserving the kill switch without
    reopening urllib's header-trickle hole.
    """
    import urllib.error
    import urllib.parse
    import urllib.request
    import http.client
    import socket as _socket
    deadline = _deadline_for_request(req) if deadline is None else deadline
    socket_timeout = _timeout_through_deadline(deadline, _HTTP_TIMEOUT)
    header_deadline = min(float(deadline), time.monotonic() + socket_timeout)
    parsed = urllib.parse.urlsplit(req.full_url)
    scheme = parsed.scheme or "https"
    host = parsed.hostname
    port = parsed.port or (443 if scheme == "https" else 80)
    # urllib normalises the path+query into the request line; preserve query string verbatim.
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    method = req.get_method()
    body = req.data
    # Header set: urllib applies the lowercased dict on .headers and any unredirected headers; we replay both,
    # matching urllib's emitted set. http.client adds Host on its own.
    headers = {}
    for k, v in req.header_items():
        headers[k] = v
    # GitHub REQUIRES a User-Agent (a UA-less request → 403 "Request forbidden by administrative rules"). urllib's
    # opener auto-injects one but http.client does NOT, so guarantee it here as a defense-in-depth backstop for any
    # Request site that forgot to set it (the callers above DO set it; this stops a future regression). Case-
    # insensitive check so we never emit a duplicate UA header (urllib stores the name as "User-agent").
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = _USER_AGENT
    pool_key = (host, port, scheme) if keepalive else None
    conn = (
        _get_keepalive_connection(
            host,
            port,
            scheme,
            timeout=socket_timeout,
            deadline=header_deadline,
        )
        if keepalive
        else _new_http_connection(
            host,
            port,
            scheme,
            socket_timeout,
            deadline=header_deadline,
        )
    )
    header_watchdog = _HTTP_HEADER_DEADLINE_WATCHDOG.arm(conn, header_deadline)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
    except _event_budget.EventBudgetExceeded:
        # EventBudgetExceeded is cancellation (BaseException in production).
        # A request interrupted mid-write/header-read is never reusable.
        _drop_keepalive_connection(conn, pool_key)
        raise
    except (http.client.HTTPException, _socket.error, OSError) as e:
        watchdog_expired = header_watchdog.expired()
        # A broken connection (a stale-keepalive RST, a TLS reset, a server close mid-write) — close + drop so the
        # next call gets a fresh one, then surface as a URLError so _req's retry path treats it as a transient.
        _drop_keepalive_connection(conn, pool_key)
        if watchdog_expired or time.monotonic() >= header_deadline:
            # Prefer the enclosing event/logical typed deadline. A shorter per-operation deadline remains an
            # ordinary socket.timeout and may retry if the logical budget still has room.
            _raise_if_logical_deadline_expired(deadline)
            raise _socket.timeout(
                "github response status/headers exceeded their absolute deadline") from e
        if isinstance(e, _socket.timeout):
            # A per-op timeout is retryable only when it fired before the
            # absolute logical/event deadline. At the boundary, promote it to
            # the terminal typed cancellation.
            _raise_if_logical_deadline_expired(deadline)
            raise
        raise urllib.error.URLError(str(e)) from e
    except BaseException:
        # Preserve cancellation/system exceptions, but never leave a connection
        # with indeterminate request/response framing in the pool.
        _drop_keepalive_connection(conn, pool_key)
        raise
    finally:
        header_watchdog.cancel()
    if header_watchdog.expired() or time.monotonic() >= header_deadline:
        _drop_keepalive_connection(conn, pool_key)
        _raise_if_logical_deadline_expired(deadline)
        raise _socket.timeout("github response status/headers exceeded their absolute deadline")
    # 3xx — urllib.request.urlopen follows redirects; http.client does not. Mirror the relevant safe subset so
    # API reads through renamed repos keep working on the keep-alive path (GET /repos/old returns the canonical
    # repo body after GitHub redirects). We only auto-follow same-origin redirects: installation-token headers
    # must never be replayed to an arbitrary host. Tarball/codeload has its own hand-rolled redirect flow.
    if resp.status in (301, 302, 303, 307, 308):
        status, reason, response_headers = resp.status, resp.reason or "", resp.msg or {}
        loc = resp.getheader("Location")
        _consume_keepalive_body(resp, conn, pool_key, deadline, keepalive=keepalive)
        if (
            getattr(req, _REQUEST_NO_REDIRECT_ATTR, False) is True
            or not loc
            or _redirects >= _KEEPALIVE_MAX_REDIRECTS
        ):
            import io
            raise urllib.error.HTTPError(req.full_url, status, reason, response_headers,
                                         io.BytesIO(b""))
        new_url = urllib.parse.urljoin(req.full_url, loc)
        new_parsed = urllib.parse.urlsplit(new_url)
        new_scheme = new_parsed.scheme or scheme
        new_host = new_parsed.hostname
        new_port = new_parsed.port or (443 if new_scheme == "https" else 80)
        if (new_scheme, new_host, new_port) != (scheme, host, port):
            import io
            raise urllib.error.HTTPError(req.full_url, status, reason, response_headers,
                                         io.BytesIO(b""))
        redirect_method = method
        redirect_body = body
        if status == 303 or (status in (301, 302) and method not in ("GET", "HEAD")):
            redirect_method = "GET"
            redirect_body = None
        redirected = urllib.request.Request(new_url, data=redirect_body, method=redirect_method)
        for k, v in headers.items():
            redirected.add_header(k, v)
        setattr(redirected, _REQUEST_DEADLINE_ATTR, deadline)
        return _urlopen_httpclient(
            redirected,
            _redirects + 1,
            deadline,
            keepalive=keepalive,
        )
    # 4xx / 5xx — http.client returns the response object; urllib raises HTTPError. Mirror that contract so the
    # existing retry/auth-failure classification (_retry_wait / _is_auth_failure_403) is unchanged. We DRAIN+CLOSE
    # the response body BEFORE raising (so the connection is reusable for the next call) and pass the body to the
    # HTTPError (its callers may read it via err.read() — see _is_auth_failure_403). A fully consumed small error
    # keeps the connection cached; an over-cap or timed-out error body drops it because unread wire bytes remain.
    if resp.status >= 400:
        status, reason, response_headers = resp.status, resp.reason or "", resp.msg or {}
        body_bytes = _consume_keepalive_body(
            resp,
            conn,
            pool_key,
            deadline,
            keepalive=keepalive,
        )
        import io
        raise urllib.error.HTTPError(req.full_url, status, reason, response_headers,
                                     io.BytesIO(body_bytes))
    return _KeepAliveResponse(
        resp,
        conn,
        pool_key,
        deadline,
        keepalive=keepalive,
    )


def _urlopen_keepalive(req, _redirects: int = 0, deadline: float | None = None):
    """Compatibility/test seam for the pooled variant of the common transport."""
    return _urlopen_httpclient(req, _redirects, deadline, keepalive=True)


def _urlopen_oneshot(req, _redirects: int = 0, deadline: float | None = None):
    """Kill-switch variant: common deadline-safe transport, with no connection pooling."""
    return _urlopen_httpclient(req, _redirects, deadline, keepalive=False)


def _read_capped(resp, cap: int = _MAX_TARBALL_BYTES, deadline=None) -> bytes:
    """Read a response body but never buffer more than `cap` bytes — a streamed size guard so an enormous repo
    can't exhaust memory. Raises RuntimeError once the cap is exceeded (the caller turns that into an honest
    'over-cap, not indexed' coordinate).

    TOTAL-CALL DEADLINE (the prod-hang fix): the SIZE cap above bounds bytes, but NOT TIME — a stalled/trickling
    codeload socket that dribbles a byte every <_HTTP_TIMEOUT seconds keeps each `resp.read(1<<20)` under the
    per-op socket timeout, so this loop never times out and one scarce worker/repository lane is wedged on ONE tarball
    fetch UNBOUNDED (the exact non-DB hang we saw: inflight_age climbing, pg_stat_activity empty). When `deadline`
    (a time.monotonic() epoch) is supplied, each 1 MiB read goes through _read_through_deadline, which checks the
    monotonic deadline + shrinks the per-op timeout to the remaining budget BEFORE each read → on expiry it raises
    socket.timeout (caught as a retryable transient by download_tarball's callers) → the event fails cleanly and
    the durable inbox bounds it. `deadline=None` preserves the old unbounded-time behaviour for any caller that
    doesn't pass one (kept for back-compat)."""
    chunks, total = [], 0
    while True:
        b = (_read_through_deadline(resp, deadline, chunk=1 << 20)   # bounded by the total deadline (per chunk)
             if deadline is not None else resp.read(1 << 20))        # 1 MiB at a time
        if not b:
            break
        total += len(b)
        if total > cap:
            raise RuntimeError(f"repo tarball exceeds the {cap}-byte indexing cap (repo too large for this tier)")
        chunks.append(b)
    return b"".join(chunks)


def _verify_complete_gzip(data: bytes) -> None:
    """INTEGRITY GUARD against a SILENT PARTIAL DOWNLOAD = a silent graph corruption. A repo tarball is a gzip
    stream. A connection that drops NEAR the end (a CDN closing the socket on the last few KiB — common) yields a
    body that is missing its gzip TRAILER (the final CRC32 + length). The danger: `tarfile.open` reads members
    LAZILY and STOPS as soon as it has enough — it never consumes/validates that trailer — so a body truncated at
    ~90–99% is silently accepted as a SHORT tar with only the LEADING files. _full_ingest would then REPLACE
    main's authoritative graph with a partial one (some files just vanish), and EVERY prediction afterwards runs
    against corrupt 'truth' with NOTHING signalling it (not a crash, not a failed event — wrong data recorded as
    fact). The only honest defence is to verify the gzip stream is COMPLETE before we treat the bytes as the repo:
    a full decompress consumes the trailer and RAISES (EOFError / BadGzipFile / zlib.error) on a truncated stream.
    We stream-decompress (bounded memory, output discarded) so the check costs no extra full in-memory copy. On a
    truncated/corrupt stream we raise RuntimeError → the event fails cleanly → Core's durable recovery retries → no partial graph
    is ever ingested. (build_graph reads the decompressed tar anyway; this just makes the integrity failure LOUD
    instead of a silent short read.)"""
    import zlib
    # wbits=47 = gzip auto-detect (16) + max window (31): decode the gzip container, validating its trailer.
    dec = zlib.decompressobj(47)
    try:
        for i in range(0, len(data), 1 << 20):           # 1 MiB at a time — bounded memory, output thrown away
            _event_budget.raise_if_expired()
            if dec.eof:                                  # the gzip member already completed → stop feeding (any
                break                                    # trailing bytes are not part of this stream)
            dec.decompress(data[i:i + (1 << 20)], 1 << 20)
            while dec.unconsumed_tail and not dec.eof:    # drain the held-back input (max_length back-pressure)
                _event_budget.raise_if_expired()
                dec.decompress(dec.unconsumed_tail, 1 << 20)
        _event_budget.raise_if_expired()
        dec.flush()
    except _event_budget.EventBudgetExceeded:
        raise
    except (EOFError, OSError, zlib.error) as e:
        # zlib.error / OSError(BadGzipFile)-class on a truncated/corrupt stream → a partial/corrupt download.
        raise RuntimeError(f"repo tarball download is truncated/corrupt ({type(e).__name__}: {str(e)[:80]}) — "
                           f"refusing to ingest a partial graph; the event will fail and Core recovery retries") from e
    if not dec.eof:
        # the stream ended without a complete gzip member (no valid trailer) = a truncated download. Refuse it.
        raise RuntimeError("repo tarball download ended before the gzip stream completed (truncated body) — "
                           "refusing to ingest a partial graph; the event will fail and Core recovery retries")


# ── CONTENT-FREE HUNK-HEADER PARSING (the finer-collision moat — line numbers ONLY, never code) ───────────
# The GitHub Files API returns a unified-diff `patch` per file. We extract the BASE-side changed line ranges
# from the HUNK HEADERS (`@@ -a,b +c,d @@` → the `-a,b` OLD/base-file range) and DISCARD the patch BODY
# entirely (every +/- /context content line). This keeps the symbol-collision feature strictly content-free:
# the only thing that ever leaves the diff is a list of [start,end] line numbers — no file content is stored,
# logged, or rendered.
#
# WHY BASE-SIDE, NOT NEW-SIDE (audit:silent-miss 2026-06-19 — a launch-blocker fix): the engine maps these
# ranges onto main's graph symbol spans, AND compares two PRs' ranges to each other — BOTH are BASE-relative
# coordinate frames (every in-flight PR branches from the same `main`). The NEW-side (`+c,d`) numbers are
# relative to EACH PR's own head, so a PR that adds/removes net lines ABOVE an edit shifts that edit's
# new-side number off its base symbol → the edit maps onto the WRONG symbol → a real same-symbol collision
# silently drops to 'clear' (and two PRs' new-side numbers live in different coordinate frames, so the
# conflict-overlap test is meaningless too). The `-a,b` base-side range aligns with the base-version spans the
# freshness key already proves valid. (A pure INSERTION hunk `-a,0` touches no base lines → contributes
# nothing → the file falls back to the FILE-level collision = recall-safe, never a missed collision.)
import re as _re

# Capture the BASE-side `-a,b` (group1 = a start, group2 = b count); the `+c,d` new-side is matched-but-discarded.
_HUNK_RE = _re.compile(r"^@@+\s*-(\d+)(?:,(\d+))?\s+\+\d+(?:,\d+)?\s")

# RANGES CAP (audit:dos): a crafted PR file with thousands of tiny disjoint hunks parses to a huge ranges list
# → a touched_ranges int4range[] the finer-collision engine joins against every file symbol (O(ranges×symbols)).
# A real PR has a handful of hunks; above this many we return [] (no ranges) so the change falls back to the
# FILE-level collision (the recall safety net) instead of seeding the blow-up. The DB (_ranges_from_jsonb) caps
# again as the authoritative boundary; this avoids holding/shipping the huge list in the first place.
_MAX_HUNKS = 512


def changed_line_ranges_from_patch(patch) -> list:
    """Parse a unified-diff `patch` string → a list of [start, end] BASE-side changed line ranges (1-based,
    inclusive), reading ONLY the `@@ -a,b +c,d @@` HUNK HEADER lines (the `-a,b` OLD/base-file range). The patch
    BODY (the +/- and context content lines) is NEVER read — we only look at lines beginning with `@@`.
    Content-free by construction: the return value carries line numbers, never any code. A header `-a` with no
    count means a single line (b defaults to 1); `-a,0` (a pure INSERTION hunk with no base-side lines)
    contributes nothing — the added lines don't exist in main's base graph, so the file falls back to the
    FILE-level collision. Returns [] for an empty / None / binary (no patch) file — the caller then has NO
    ranges for that file → the engine falls back to the FILE-level collision (the safety net, never a missed
    collision). BASE-side (NOT new-side) so the ranges align with main's base-version symbol spans AND are
    comparable across PRs — see the `_HUNK_RE` note above (audit:silent-miss launch-blocker fix)."""
    if not patch or not isinstance(patch, str):
        return []
    out = []
    for line in patch.split("\n"):
        if not line.startswith("@@"):     # ONLY hunk headers — the +/- body is discarded, never inspected
            continue
        m = _HUNK_RE.match(line)
        if not m:
            continue
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        if count <= 0:                    # -a,0 = an insertion-only hunk: no BASE-side lines touched
            continue
        out.append([start, start + count - 1])
        if len(out) > _MAX_HUNKS:         # DoS cap (audit:dos): pathological hunk count → drop ranges, fall
            return []                     # back to FILE-level collision (the safety net), never seed O(ranges×syms)
    return out


# ── CONFLICT-MARKER DETECTOR (content-free) ───────────────────────────────────────────────────────────────
# An unresolved git merge conflict leaves three SHAPED markers at the START of a line: a 7-char `<<<<<<<` (ours
# header), a 7-char `=======` (separator), and a 7-char `>>>>>>>` (theirs trailer). Such markers can survive an
# incomplete conflict resolution and deterministically break parsers or builds. This detector closes that hole
# CONTENT-FREE-LY:
# we scan ONLY the PR diff's NEW-side ADDED lines (the `+`-prefixed lines in the patch's hunks) for the
# 3-marker shape and RECORD ONLY (path, new-side line_no, marker_kind). The line CONTENT is NEVER stored,
# logged, or rendered — same discipline as line-range geometry (line numbers cross, code bodies never do).
#
# PRECISION: we require BOTH the `<<<<<<<` (ours) marker AND a matching `>>>>>>>` (theirs) marker in the same
# file (the file-level high-precision gate). `=======` alone is too common (markdown separators, reST rules,
# Python comment dividers) — firing on it alone would be cry-wolf. The 3-marker shape (or even just the
# <<<<<<< + >>>>>>> pair) at line-start is essentially impossible to construct accidentally in source code.
#
# NEW-SIDE LINE NUMBERS: we report the new-side line number (the `+c,d` field) because the marker is in the
# PR's added content, NOT in main. That's the line the developer sees in their checkout and the line GitHub
# anchors to in the PR view. Content-free as ever (a line number is metadata, not the marker text).
_MARKER_OURS = "<" * 7      # the `<<<<<<<` (ours) head of a git conflict block
_MARKER_SEP = "=" * 7       # the `=======` separator (mid-block)
_MARKER_THEIRS = ">" * 7    # the `>>>>>>>` (theirs) tail
# Per-file cap so a degenerate patch (an attacker-crafted diff with thousands of marker lines) cannot blow up
# the per-file finding list. Real cases have a handful per file. Above this we keep the first N and stop scanning.
_MAX_CONFLICT_FINDINGS_PER_FILE = 32


def _hunk_new_start(line):
    """Pull the `+c` NEW-side start line out of a `@@ -a,b +c,d @@` hunk header, or None if the header is junk.
    The companion to _HUNK_RE which captures the BASE-side `-a,b`; the conflict-marker detector needs the
    NEW-side number because the markers live in the PR's added content (not in main). Content-free (one int)."""
    m = _re.match(r"^@@+\s*-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s", line)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def conflict_markers_from_patch(patch) -> list:
    """Scan a unified-diff `patch` string for unresolved git merge-conflict markers introduced in this PR. Returns
    a list of {"line": <new-side line_no>, "kind": "ours"|"separator"|"theirs"} dicts — line numbers only,
    NEVER the surrounding text. A literal `<<<<<<< HEAD` /
    `=======` / `>>>>>>>` triplet committed without resolution will fail any build, and Core was structurally
    blind to it (content-free = no body inspection). We scan ONLY the PR diff's NEW-side ADDED lines (lines
    starting with `+` inside a hunk, EXCEPT the `+++ b/path` file header), at the line START — so a marker
    inside an unchanged line, in the OLD side, or mid-line (e.g. a docstring discussing markers) does NOT fire.
    Returns [] for: an empty/None/non-string patch, OR a file whose patch contains the ours marker but no
    matching theirs marker (precision: the `<<<<<<<` + `>>>>>>>` PAIR is the unique-to-merge shape; `=======`
    alone fires on markdown separators, so we never fire on a separator-only file). Content-free by
    construction; bounded by _MAX_CONFLICT_FINDINGS_PER_FILE so a degenerate patch can never explode the list."""
    if not patch or not isinstance(patch, str):
        return []
    findings = []
    new_line_no = 0           # current new-side line number as we walk hunks
    in_hunk = False
    have_ours = False         # this file has at least one start-of-line `<<<<<<<` marker
    have_theirs = False       # this file has at least one start-of-line `>>>>>>>` marker
    for raw in patch.split("\n"):
        # File-header lines (`+++ b/path`, `--- a/path`) are NOT hunk content — skip them BEFORE the +-check.
        if raw.startswith("+++") or raw.startswith("---"):
            in_hunk = False
            continue
        if raw.startswith("@@"):
            ns = _hunk_new_start(raw)
            if ns is None:
                in_hunk = False
                continue
            new_line_no = ns
            in_hunk = True
            continue
        if not in_hunk:
            continue
        # In a hunk: `+` = added (new-side), `-` = removed (old-side only, NOT in new), ` ` (space) = context
        # (present on both sides). We ONLY scan ADDED lines — a marker that's an unchanged context line was
        # there at base too (Core's content-free design accepts that as out-of-scope; it'd be old garbage,
        # not introduced by this PR), and a `-` removed line is not in the new file at all.
        if raw.startswith("+"):
            # Strip the leading '+' to inspect the actual content line (we ONLY look at its 7-char prefix
            # for marker shape — never the rest of the line, never logged/stored). _MARKER_OURS/SEP/THEIRS
            # is a 7-char run of <, =, > — the line-START test is the high-precision gate.
            body = raw[1:]
            kind = None
            if body.startswith(_MARKER_OURS):
                kind = "ours"
                have_ours = True
            elif body.startswith(_MARKER_THEIRS):
                kind = "theirs"
                have_theirs = True
            elif body.startswith(_MARKER_SEP):
                kind = "separator"
            if kind is not None:
                # record (line, kind) ONLY — the actual line body is discarded (content-free)
                findings.append({"line": new_line_no, "kind": kind})
                if len(findings) >= _MAX_CONFLICT_FINDINGS_PER_FILE:
                    # advance past the cap and stop scanning this file — the engine will surface the cap
                    # honestly (the FIRST N + "more not shown"), never crash on a degenerate diff.
                    break
            new_line_no += 1
        elif raw.startswith("-"):
            # removed line — does NOT advance new-side line number (it's gone from the new file)
            pass
        else:
            # context line ` ` (or blank-line continuation) — advances new-side
            new_line_no += 1
    # PRECISION GATE: a file must carry BOTH the ours AND theirs markers to be a true unresolved merge. A
    # `=======` separator on its own (markdown rule, RST underline, a Python comment divider) is too common
    # to fire on. The unique-to-git-merge shape is the `<<<<<<<` + `>>>>>>>` pair (both at line-start, in lines
    # this PR ADDED). Without that pair we drop the findings entirely — no false positive.
    if not (have_ours and have_theirs):
        return []
    return findings


# ── the real GitHub REST client (live mode only; never touched by the offline test) ──────────────────────
class GitHubREST(_GitHubPRSurfaceMixin, _GitHubContentFetchMixin, _GitHubPRReadMixin, _GitHubInstallationsMixin):
    API = "https://api.github.com"
    # RESILIENCE: GitHub rate-limits (5000 calls/hr/installation) and has transient 5xx. The App makes many
    # calls per event, so at scale these WILL happen. _req retries the retryable ones with backoff; a wait is
    # capped (don't block the single-threaded server forever — failing → Core recovery retries, and recording is
    # idempotent), and after MAX_RETRIES it re-raises so the webhook still fails cleanly.
    MAX_RETRIES = 4
    RETRY_CAP_SECONDS = 60
    TOKEN_SKEW_SECONDS = 300   # remint the 1h installation token when within 5 min of expiry (clock skew + safety)
    # for_account's account→installation map (GET /app/installations) is cached, but the App's installation SET
    # changes over time (a new customer installs). On a long-running loop (the watchdog freshness self-heal) a
    # forever-cached map would make a freshly-installed tenant invisible until restart. So a cache MISS rebuilds
    # the map — THROTTLED to at most once per this window, so a genuinely-orphaned coordinate (no installation,
    # always a miss) can't storm /app/installations every tick. 5 min: a new install is picked up by the next
    # freshness pass within the window, the API cost stays one list per window at most.
    ACCOUNT_MAP_TTL_SECONDS = 300

    # The Veripsa tenant-account prefix: the DB keys every GitHub tenant as 'ACCT-GH-'||<owning-account id>
    # (core.enter_installation_with_authority; _event_account_key feeds it repository.owner.id). A background
    # loop's coordinate carries that account_id, so for_account() strips this prefix to recover the bare GitHub
    # account (owner) id and maps it → the installation id via the App-installations list. Mirrors the SQL
    # convention exactly (same literal); kept here so the resolver needs no DB call to invert an account_id.
    ACCOUNT_PREFIX = "ACCT-GH-"

    def __init__(self, app_id: str, private_key: str, installation_id: str):
        self.app_id, self.private_key, self.installation_id = app_id, private_key, installation_id
        # Keyed workers may share one installation client. Serialize the
        # check→mint→publish sequence so a cold/expiring token produces one
        # network mint, not one mint per worker. RLock permits _itoken() to call
        # the independently-safe _mint_installation_token().
        self._token_lock = _threading.RLock()
        self._token = None
        self._token_exp = 0.0   # epoch seconds when the cached installation token expires (0.0 = none minted yet)
        self._installations: dict = {}   # {installation_id: GitHubREST} — shared sibling cache
        self._installations_lock = _threading.RLock()
        self._account_install_map = None  # {gh-account-id: installation-id}, lazily built from GET /app/installations (see for_account)
        self._account_install_map_at = 0.0  # monotonic time the map was last built (0.0 = never) — see ACCOUNT_MAP_TTL_SECONDS
        self._account_install_map_reachable = None  # bool|None: last App-JWT installations-list reachability observation
        self._account_install_map_complete = None  # bool|None: false when the bounded list saturated; misses are not absence proof
        self._account_install_map_last_ok_at = 0.0  # monotonic time of last successful App installations list
        self._account_install_map_last_error_at = 0.0  # monotonic time of last failed App installations list
        self._account_install_map_last_error = ""  # content-free, truncated class/message for the last failure
        self._account_install_map_count = None  # number of account→installation entries in the last successful map
        self._last_app_installations_complete = None  # bool|None: endpoint end was observed before the raw-row cap
        self._last_app_installations_raw_count = None  # raw rows examined, independent of malformed/duplicate entries
        self._app_registration_probe_lock = _threading.RLock()
        self._app_registration_probe_at = 0.0
        self._app_registration_probe_reachable = None

    @staticmethod
    def _urlopen(req):                      # the one network call — a seam the resilience test overrides
        # KILL SWITCH disables POOLING, not deadline safety. Both modes use the common http.client transport +
        # absolute request/header watchdog; one-shot mode simply closes after each response.
        deadline = _deadline_for_request(req)
        if not _HTTP_KEEPALIVE:
            return _urlopen_oneshot(req, deadline=deadline)
        return _urlopen_keepalive(req, deadline=deadline)

    @staticmethod
    def _sleep(seconds):                    # overridden in tests to a no-op (so backoff is instant + asserted)
        import time
        time.sleep(seconds)

    def get_app_registration(self) -> dict:
        """The live GitHub App registration (permissions/events) via App JWT.

        This is App-level, not installation-level. Deployment smoke uses it to
        catch registration drift such as a missing `check_suite` subscription
        or missing `checks: write` before operators have to inspect the GitHub
        App settings page by hand.
        """
        return self._req("GET", "/app", self._jwt())

    def app_registration_reachability(self) -> dict:
        """Constant-time cached App-JWT identity probe.

        Production background routing carries exact durable installation ids,
        so watchdog health must not paginate the fleet merely to prove the App
        JWT works. ``GET /app`` is one App-level point read and its returned id
        must match the configured App id. The shared TTL keeps overlapping
        watchdog ticks from adding request load.
        """
        now = time.monotonic()
        with self._app_registration_probe_lock:
            if (
                self._app_registration_probe_at
                and (
                    now - self._app_registration_probe_at
                    < self.ACCOUNT_MAP_TTL_SECONDS
                )
                and isinstance(
                    self._app_registration_probe_reachable, bool)
            ):
                return {
                    "reachable": self._app_registration_probe_reachable,
                    "attempted_at": self._app_registration_probe_at,
                }
            try:
                registration = self.get_app_registration()
                returned_id = (
                    registration.get("id")
                    if isinstance(registration, dict)
                    else None
                )
                reachable = (
                    returned_id is not None
                    and not isinstance(returned_id, bool)
                    and str(returned_id).strip() == str(self.app_id).strip()
                )
            except Exception:
                reachable = False
            self._app_registration_probe_at = time.monotonic()
            self._app_registration_probe_reachable = reachable
            return {
                "reachable": reachable,
                "attempted_at": self._app_registration_probe_at,
            }

    @classmethod
    def _backoff(cls, attempt: int) -> float:
        import random
        return min((2 ** attempt) + random.random(), cls.RETRY_CAP_SECONDS)   # exponential + jitter, capped

    @classmethod
    def _retry_wait(cls, err, attempt: int):
        """Seconds to wait before retrying this HTTPError, or None if it is NOT retryable (re-raise now).
        Retryable: transient 5xx; 429 / 403 secondary-limit (Retry-After); 403 primary limit (remaining 0 →
        wait to X-RateLimit-Reset). NOT retryable: 404 / 401 / 422 and a plain 403 (a permissions denial)."""
        import time as _t
        code = getattr(err, "code", None)
        headers = getattr(err, "headers", None) or {}
        if code in (500, 502, 503, 504):
            return cls._backoff(attempt)
        if code in (403, 429):
            ra = headers.get("Retry-After")                       # secondary / abuse rate limit
            if ra is not None:
                try:
                    return min(max(float(ra), 0.0), cls.RETRY_CAP_SECONDS)
                except (TypeError, ValueError):
                    pass
            if str(headers.get("X-RateLimit-Remaining")) == "0":  # primary rate limit → wait to reset
                reset = headers.get("X-RateLimit-Reset")
                try:
                    return min(max(float(reset) - _t.time(), 0.0), cls.RETRY_CAP_SECONDS)
                except (TypeError, ValueError):
                    return cls._backoff(attempt)
            return None                                           # a 403 that is not a rate limit → don't retry
        return None                                               # 404 / 401 / 422 / etc → not retryable

    @staticmethod
    def _is_auth_failure_403(err) -> bool:
        """True iff `err` is a 403 that signals a BAD/EXPIRED CREDENTIAL (a stale installation token) — the kind
        a single remint+retry can self-heal — as opposed to a RATE-LIMIT 403 or a GENUINE permission denial,
        which a remint can NEVER fix (and which must NOT trigger a remint loop). The reactive 401 path already
        covers the common early-revocation; this is the rarer 403 variant GitHub returns when a cached token was
        invalidated mid-life (a permission re-approval during a migration revokes the old token — the App
        symptom). DISTINGUISHED conservatively, defence-in-depth:
          • a 403 carrying a RATE-LIMIT signal (Retry-After, or X-RateLimit-Remaining: 0) is NOT an auth failure
            — _retry_wait already backs those off; reminting would be wrong (the token is fine) → return False.
          • a 403 whose body says 'Resource not accessible by integration' / 'forbidden' (a real PERMISSION
            denial — the App lacks a scope on this repo) is NOT remintable → return False (so we never loop on a
            genuine 403, per the constraint: at most one remint+retry, then propagate).
          • a 403 whose body mentions bad/expired/invalid CREDENTIALS / token is a stale-token auth failure →
            True (remint once + retry once). Body read is best-effort + bounded; an unreadable/odd body is treated
            as NOT an auth failure (fail closed → propagate, never a spurious remint).
        Never masks the original HTTP error, except that an enclosing event/logical deadline remains terminal."""
        try:
            if getattr(err, "code", None) != 403:
                return False
            headers = getattr(err, "headers", None) or {}
            # a rate-limit 403 is NOT an auth failure (the token is valid; _retry_wait handles the wait).
            if headers.get("Retry-After") is not None or str(headers.get("X-RateLimit-Remaining")) == "0":
                return False
            body = ""
            try:
                deadline = _LOGICAL_CALL_DEADLINE.get()
                if deadline is None:
                    deadline = _event_budget.deadline_for(_HTTP_TOTAL_TIMEOUT)
                # urllib's fallback HTTPError may still own a live/trickling response. Keep the classifier
                # bounded by the same logical call and retain only GitHub's small diagnostic envelope.
                raw = _read_through_deadline(
                    err, deadline, chunk=_MAX_HTTP_ERROR_BODY_BYTES + 1)[:_MAX_HTTP_ERROR_BODY_BYTES]
                if isinstance(raw, (bytes, bytearray)):
                    body = raw.decode("utf-8", "replace")
                elif isinstance(raw, str):
                    body = raw
            except _event_budget.EventBudgetExceeded:
                raise
            except TimeoutError:
                _raise_if_logical_deadline_expired(deadline)
                body = ""
            except Exception:
                body = ""
            low = body.lower()
            # a GENUINE permission denial is NOT remintable — propagate (never remint-loop on a real 403).
            if "not accessible by integration" in low or "must have admin" in low:
                return False
            # a stale/expired/invalid CREDENTIAL → remintable (one remint + one retry).
            return ("bad credential" in low or "credentials" in low
                    or "expired" in low or "invalid token" in low or "token expired" in low)
        except (_event_budget.EventBudgetExceeded, _GitHubCallDeadlineExceeded):
            raise
        except Exception:
            return False

    def for_installation(self, installation_id: str):
        """A client scoped to `installation_id`, CACHED so each tenant's client RETAINS its (auto-refreshing)
        installation token ACROSS events. The configured id returns self; any other id is created ONCE and
        reused — without this a multi-tenant App would mint a fresh token for every event of every non-primary
        installation (wasteful + hits GitHub's token-creation limits at scale). Keyed workers can call this
        concurrently, so siblings share both the cache and its creation lock. Each sibling retains its OWN token
        lock, allowing different installations to mint independently."""
        iid = str(installation_id)
        if iid == str(self.installation_id):
            return self
        with self._installations_lock:
            client = self._installations.get(iid)
            if client is None:
                client = GitHubREST(self.app_id, self.private_key, iid)
                client._installations = self._installations
                client._installations_lock = self._installations_lock
                self._installations[iid] = client
            return client

    def _jwt(self) -> str:
        import time
        try:
            import jwt  # PyJWT
        except ImportError as e:                           # live-only dep; the offline test never gets here
            raise RuntimeError("PyJWT required for live mode: pip install pyjwt cryptography") from e
        now = int(time.time())
        # iat backdated 60s (tolerate our clock running slightly ahead of GitHub's); exp 480s = 8 min out. The
        # full iat→exp span is then 540s — a deliberate 60s margin UNDER GitHub's hard 600s (10-min) ceiling for
        # an App JWT, so positive clock skew on our host can't push exp past the limit (which would 401 EVERY
        # installation-token mint, silently killing all checks/comments). Mirrors the TOKEN_SKEW_SECONDS=300
        # headroom on the installation token. The JWT is re-minted lazily each token window, so a shorter life
        # costs nothing. This JWT only mints the installation token, then is discarded.
        return jwt.encode({"iat": now - 60, "exp": now + 480, "iss": self.app_id}, self.private_key, algorithm="RS256")

    def _req(self, method: str, url: str, token: str, body: dict | None = None, accept="application/vnd.github+json"):
        """One GitHub REST call, with bounded retry+backoff over rate limits (403/429) and transient 5xx.
        A 404/401/422 (and a non-rate-limit 403) re-raises immediately; exhausting MAX_RETRIES re-raises too
        (the webhook then fails cleanly → Core's durable recovery retries, and our recording is idempotent)."""
        import socket
        import urllib.request
        import urllib.error
        data = json.dumps(body).encode() if body is not None else None
        full = url if url.startswith("http") else self.API + url
        with _logical_call_scope() as deadline:
            attempt = 0
            while True:
                _raise_if_logical_deadline_expired(deadline)
                req = urllib.request.Request(full, data=data, method=method)
                req.add_header("Authorization", f"Bearer {token}")
                req.add_header("Accept", accept)
                req.add_header("X-GitHub-Api-Version", "2022-11-28")
                req.add_header("User-Agent", _USER_AGENT)   # GitHub 403s any UA-less request
                setattr(req, _REQUEST_DEADLINE_ATTR, deadline)
                try:
                    with self._urlopen(req) as r:
                        raw = _read_through_deadline(r, deadline)
                        return json.loads(raw) if raw and accept.endswith("json") else raw
                except _event_budget.EventBudgetExceeded:
                    raise
                except urllib.error.HTTPError as e:
                    _raise_event_rate_limit_deferral(e, deadline)
                    wait = self._retry_wait(e, attempt)
                    if wait is None or attempt >= self.MAX_RETRIES:
                        raise
                    _retry_sleep(self, wait, deadline)
                    attempt += 1
                except (urllib.error.URLError, socket.timeout, TimeoutError):
                    # A per-op timeout may leave enough logical time for one retry. A logical/event deadline
                    # timeout does not: this check re-raises the corresponding terminal timeout immediately.
                    _raise_if_logical_deadline_expired(deadline)
                    if attempt >= self.MAX_RETRIES:
                        raise
                    _retry_sleep(self, self._backoff(attempt), deadline)
                    attempt += 1

    def _req_with_link(self, method: str, url: str, token: str, body: dict | None = None,
                       accept="application/vnd.github+json"):
        """Like _req but returns (body, link_header_str) so the caller can inspect `Link: rel="next"` to drive
        CORRECT pagination rather than using the len<per_page heuristic (the P1 false-clear gap). The `link_header_str`
        is the raw `Link` response header value (a comma-separated RFC 5988 string) or '' when absent. All retry
        and error-handling is identical to _req. Content-free: header values are URLs + relation types only."""
        import socket
        import urllib.request
        import urllib.error
        data = json.dumps(body).encode() if body is not None else None
        full = url if url.startswith("http") else self.API + url
        with _logical_call_scope() as deadline:
            attempt = 0
            while True:
                _raise_if_logical_deadline_expired(deadline)
                req = urllib.request.Request(full, data=data, method=method)
                req.add_header("Authorization", f"Bearer {token}")
                req.add_header("Accept", accept)
                req.add_header("X-GitHub-Api-Version", "2022-11-28")
                req.add_header("User-Agent", _USER_AGENT)   # GitHub 403s any UA-less request
                setattr(req, _REQUEST_DEADLINE_ATTR, deadline)
                try:
                    with self._urlopen(req) as r:
                        raw = _read_through_deadline(r, deadline)
                        # r.headers may be absent on a minimal test double; no header safely degrades to the
                        # pagination helper's fallback instead of crashing.
                        hdrs = getattr(r, "headers", None)
                        link = (hdrs.get("Link") if hdrs is not None else None) or ""
                        parsed = json.loads(raw) if raw and accept.endswith("json") else raw
                        return parsed, link
                except _event_budget.EventBudgetExceeded:
                    raise
                except urllib.error.HTTPError as e:
                    _raise_event_rate_limit_deferral(e, deadline)
                    wait = self._retry_wait(e, attempt)
                    if wait is None or attempt >= self.MAX_RETRIES:
                        raise
                    _retry_sleep(self, wait, deadline)
                    attempt += 1
                except (urllib.error.URLError, socket.timeout, TimeoutError):
                    _raise_if_logical_deadline_expired(deadline)
                    if attempt >= self.MAX_RETRIES:
                        raise
                    _retry_sleep(self, self._backoff(attempt), deadline)
                    attempt += 1

    def _mint_installation_token(self) -> str:
        """POST a fresh installation access token (authed with the short-lived App JWT) and remember its
        expiry. GitHub returns `expires_at` (~1h out); we fall back to ~55 min if it is missing/unparseable."""
        import calendar
        import time as _t
        with _token_lock_scope(self):
            r = self._req("POST", f"/app/installations/{self.installation_id}/access_tokens", self._jwt())
            self._token = r["token"]
            exp = r.get("expires_at")
            exp_epoch = 0.0
            if exp:
                try:
                    exp_epoch = float(calendar.timegm(_t.strptime(exp, "%Y-%m-%dT%H:%M:%SZ")))  # GitHub returns UTC 'Z'
                except (ValueError, TypeError):
                    exp_epoch = 0.0
            self._token_exp = exp_epoch or (_t.time() + 3300.0)
            return self._token

    def _itoken(self) -> str:
        """The current installation token, REMINTED proactively before it expires. Installation tokens last
        ~1h; a long-running server that cached one forever would 401 on every call after expiry. So remint when
        we have none OR are within TOKEN_SKEW_SECONDS of expiry."""
        import time as _t
        with _token_lock_scope(self):
            if not self._token or _t.time() >= self._token_exp - self.TOKEN_SKEW_SECONDS:
                self._mint_installation_token()
            return self._token

    def _invalidate_token_for_retry(
        self,
        expected_token=_UNCONDITIONAL_TOKEN_INVALIDATION,
    ) -> bool:
        """Drop the cached installation token so the NEXT _itoken() mints a fresh one. The reactive half of the
        token lifecycle: proactive remint (in _itoken) covers the predictable ~1h expiry, but a token can be
        revoked EARLY (an admin rotating the App key, a suspended-then-resumed install, a server clock that ran
        slow) — surfacing as a 401 on an otherwise-valid call. Every installation-token call recovers the same
        way: invalidate here, then retry ONCE on a freshly minted token (a single retry, never a loop — a second
        401 re-raises, so a genuinely broken key fails the delivery cleanly rather than spinning).

        Concurrent workers must pass the token used by their failed attempt.
        A delayed 401 for old T0 must not erase a newer T1 another worker has
        already minted. The no-argument form remains as a compatibility seam
        for explicit administrative callers, while all reactive paths use the
        conditional form. Returns whether this call actually invalidated."""
        with _token_lock_scope(self):
            if (
                expected_token is not _UNCONDITIONAL_TOKEN_INVALIDATION
                and self._token != expected_token
            ):
                return False
            self._token, self._token_exp = None, 0.0
            return True

    def _api(self, method: str, url: str, body: dict | None = None, accept="application/vnd.github+json"):
        """An installation-token-authenticated REST call. Proactive remint (via _itoken) keeps the token fresh;
        REACTIVELY, an AUTH FAILURE — a 401, OR a 403 whose body shows a bad/expired credential (NOT a rate-limit,
        NOT a genuine 'Resource not accessible by integration' permission denial) — invalidates the cache, remints
        ONCE, and retries the request ONCE. The 403-credential case covers a mid-life token invalidation (a
        permission re-approval during a migration revokes the cached token before its ~1h expiry — the App
        symptom): without the remint the App would reuse the dead token until expiry. STRICTLY one remint + one
        retry (a second auth failure re-raises) — a genuine permission 403 is NOT remintable (so it never loops:
        _is_auth_failure_403 returns False for it, the request re-raises straight away)."""
        import urllib.error
        with _logical_call_scope():
            attempt_token = self._itoken()
            try:
                return self._req(method, url, attempt_token, body, accept)
            except urllib.error.HTTPError as e:
                if e.code == 401 or self._is_auth_failure_403(e):
                    self._invalidate_token_for_retry(attempt_token)
                    retry_token = self._itoken()
                    return self._req(method, url, retry_token, body, accept)
                raise

    def _api_with_link(self, method: str, url: str, body: dict | None = None,
                       accept="application/vnd.github+json"):
        """Like _api but returns (body, link_header_str) — the Link header drives correct pagination for the PR
        Files API (primary fix for the P1 false-clear gap: short-but-200 intermediate page silently ends the loop).
        Same auth / remint / retry semantics as _api. Callers that want only the body use _api; those that NEED the
        Link header (currently: list_pr_files / list_pr_files_with_ranges in _GitHubPRReadMixin) use this."""
        import urllib.error
        with _logical_call_scope():
            attempt_token = self._itoken()
            try:
                return self._req_with_link(method, url, attempt_token, body, accept)
            except urllib.error.HTTPError as e:
                if e.code == 401 or self._is_auth_failure_403(e):
                    self._invalidate_token_for_retry(attempt_token)
                    retry_token = self._itoken()
                    return self._req_with_link(method, url, retry_token, body, accept)
                raise
