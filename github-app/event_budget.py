"""One wall-clock budget shared by every stage of one webhook delivery.

The webhook queue uses a small keyed pool.  A timeout local to one network
*attempt* is still insufficient: GitHub retries, EventQueue retries, DB waits,
and graph extraction otherwise multiply into many minutes while holding one
tenant/repository lane and consuming scarce pool capacity.  EventQueue opens
one budget when it dequeues a delivery; all hot-path collaborators consume the
same monotonic deadline.
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
import socket
import threading
import time

try:
    from env_config import env_int
except ImportError:  # imported as a package
    from .env_config import env_int


_EVENT_WALL_TIMEOUT_SECONDS = env_int(
    "VERIPSA_EVENT_WALL_TIMEOUT_SECONDS", 90, min_value=1, max_value=900,
)
# Keep the final few seconds for durable release/defer/finish. A normal handler
# sees the earlier work deadline; only DeliveryStore's explicit terminal scope
# may consume this reserve. For tiny explicit test budgets the effective reserve
# is capped to 25%, so tests and emergency-low configurations retain useful work.
_EVENT_TERMINAL_RESERVE_SECONDS = env_int(
    "VERIPSA_EVENT_TERMINAL_RESERVE_SECONDS", 5, min_value=1, max_value=60,
)
_TERMINAL_RESERVE_MAX_FRACTION = 0.25
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "veripsa_event_work_deadline", default=None,
)
_TOTAL_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "veripsa_event_total_deadline", default=None,
)
_TERMINAL_SCOPE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "veripsa_event_terminal_scope", default=False,
)
# Account identity for process-wide scarce-resource arbitration. EventQueue
# binds this once per dequeued delivery; graph ingestion reads it without
# widening every handler/ingest signature. ``None`` means background work,
# while an empty string is a conservative unknown-event account bucket.
_ACCOUNT_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "veripsa_event_account_scope", default=None,
)


class EventBudgetExceeded(BaseException):
    """Cancellation: the current delivery exhausted its shared wall budget.

    This intentionally lives outside ``Exception``.  Webhook code has many
    narrow fail-open ``except Exception`` seams for optional enrichments; a
    delivery deadline must cross every one of them and reach the explicit
    transaction/lease/worker cancellation boundaries instead of being
    converted into partial success.
    """


def begin(seconds: float | None = None):
    budget = float(_EVENT_WALL_TIMEOUT_SECONDS if seconds is None else seconds)
    budget = max(0.001, budget)
    reserve = min(
        float(_EVENT_TERMINAL_RESERVE_SECONDS),
        budget * _TERMINAL_RESERVE_MAX_FRACTION,
    )
    total_deadline = time.monotonic() + budget
    work_deadline = total_deadline - reserve
    return (
        _DEADLINE.set(work_deadline),
        _TOTAL_DEADLINE.set(total_deadline),
        _TERMINAL_SCOPE.set(False),
    )


def end(token) -> None:
    work_token, total_token, scope_token = token
    # Reset in reverse set order, preserving nested budget contexts.
    _TERMINAL_SCOPE.reset(scope_token)
    _TOTAL_DEADLINE.reset(total_token)
    _DEADLINE.reset(work_token)


def bind_account(account: str | None):
    """Bind one content-free account key to the current delivery context."""
    return _ACCOUNT_SCOPE.set(str(account or "")[:120])


def reset_account(token) -> None:
    _ACCOUNT_SCOPE.reset(token)


def current_account() -> str | None:
    """Return the bound event account, or None outside EventQueue work."""
    return _ACCOUNT_SCOPE.get()


def begin_work(seconds: float):
    """Narrow only the current work deadline without touching terminal reserve.

    Fanout uses this for one repository. The child deadline is always capped by
    its parent event work deadline, while DeliveryStore's terminal scope keeps
    the original total deadline available for exact release/defer resolution.
    """
    deadline = time.monotonic() + max(0.001, float(seconds))
    parent = _DEADLINE.get()
    if parent is not None:
        deadline = min(deadline, parent)
    return _DEADLINE.set(deadline)


def end_work(token) -> None:
    _DEADLINE.reset(token)


def narrow_total_deadline(absolute_deadline: float) -> float | None:
    """Narrow work to a durable execution expiry, retaining a fixed terminal tail.

    Durable delivery retries share a database-clock window across process
    generations.  The claimant converts the database's remaining milliseconds
    to a conservative monotonic *execution* deadline and calls this immediately.
    The handler can never cross that deadline. Exact release/defer/finish may
    use up to the configured terminal reserve after it, still capped by the
    parent event's original total deadline. A later durable claim cannot extend
    either absolute boundary.

    No context is created when called outside an EventQueue budget.  Recovery
    admission runs on a scheduler thread and carries the absolute monotonic
    deadline to the eventual worker as a private, non-durable marker instead.
    """
    parent_total = _TOTAL_DEADLINE.get()
    if parent_total is None:
        return None
    execution_deadline = float(absolute_deadline)
    narrowed_total = min(
        parent_total,
        execution_deadline + float(_EVENT_TERMINAL_RESERVE_SECONDS),
    )
    parent_work = _DEADLINE.get()
    narrowed_work = min(
        narrowed_total,
        execution_deadline if parent_work is None
        else min(parent_work, execution_deadline),
    )
    _TOTAL_DEADLINE.set(narrowed_total)
    _DEADLINE.set(
        narrowed_work if parent_work is None
        else min(parent_work, narrowed_work)
    )
    return narrowed_total


def current_deadline() -> float | None:
    if _TERMINAL_SCOPE.get():
        return _TOTAL_DEADLINE.get()
    return _DEADLINE.get()


def total_deadline() -> float | None:
    return _TOTAL_DEADLINE.get()


def remaining() -> float | None:
    deadline = current_deadline()
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def work_remaining() -> float | None:
    deadline = _DEADLINE.get()
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def total_remaining() -> float | None:
    deadline = _TOTAL_DEADLINE.get()
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def in_terminal_scope() -> bool:
    return bool(_TERMINAL_SCOPE.get())


@contextmanager
def terminal_scope():
    """Temporarily spend the reserved tail on durable terminalization only."""
    token = _TERMINAL_SCOPE.set(True)
    try:
        yield
    finally:
        _TERMINAL_SCOPE.reset(token)


def deadline_for(local_seconds: float) -> float:
    """Deadline for a stage, capped by the current event deadline when present."""
    local = time.monotonic() + max(0.001, float(local_seconds))
    event = current_deadline()
    return local if event is None else min(local, event)


def timeout_for(local_seconds: float, *, floor: float = 0.001) -> float:
    """Timeout argument capped to the remaining event budget."""
    rem = remaining()
    if rem is None:
        return max(floor, float(local_seconds))
    if rem <= 0:
        raise EventBudgetExceeded("webhook event exceeded its total wall-clock budget")
    return max(floor, min(float(local_seconds), rem))


def raise_if_expired() -> None:
    rem = remaining()
    if rem is not None and rem <= 0:
        raise EventBudgetExceeded("webhook event exceeded its total wall-clock budget")


def can_retry(wait_seconds: float = 0.0) -> bool:
    """Whether a retry can start after its intended wait without crossing the budget."""
    rem = remaining()
    return rem is None or rem > max(0.0, float(wait_seconds))


class ConnectionDeadlineGuard:
    """Shutdown one live DB socket at the active absolute deadline.

    PostgreSQL's statement_timeout bounds server execution but not a black-holed
    TCP response. The daemon timer shuts down the already-connected socket so a
    blocked psycopg call returns. ``disarm`` and the callback serialize on one
    lock: callers disarm *before* close, so a later FD reuse can never be touched
    by a stale timer.
    """

    def __init__(self, conn):
        self._conn = conn
        self._lock = threading.Lock()
        self._armed = False
        self._fired = False
        self._timer = None
        deadline = current_deadline()
        if deadline is None:
            return
        self._armed = True
        delay = max(0.0, deadline - time.monotonic())
        timer = threading.Timer(delay, self._expire)
        timer.daemon = True
        self._timer = timer
        timer.start()

    @property
    def fired(self) -> bool:
        return self._fired

    def _expire(self) -> None:
        with self._lock:
            if not self._armed:
                return
            self._armed = False
            self._fired = True
            sock = None
            try:
                fd = int(self._conn.fileno())
                if fd < 0:
                    return
                # Wrap without ownership: detach in finally so this temporary
                # socket object never closes psycopg's descriptor.
                sock = socket.socket(fileno=fd)
                sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError, TypeError, ValueError):
                pass
            finally:
                if sock is not None:
                    try:
                        sock.detach()
                    except OSError:
                        pass

    def disarm(self) -> None:
        timer = None
        with self._lock:
            self._armed = False
            timer = self._timer
        if timer is not None:
            timer.cancel()

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        self.disarm()
        return False


def arm_connection_deadline(conn) -> ConnectionDeadlineGuard:
    """Arm a race-safe socket shutdown at the current work/terminal deadline."""
    return ConnectionDeadlineGuard(conn)
