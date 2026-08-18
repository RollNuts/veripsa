#!/usr/bin/env python3
"""Cancellation-boundary gate for one bounded webhook delivery.

EventBudgetExceeded is deliberately a BaseException cancellation so optional
``except Exception`` fallbacks cannot convert an expired delivery into partial
success.  The few owning boundaries must still catch it explicitly: the durable
wrapper releases its exact lease, EventQueue fails one generation without
dying/retrying, and post-commit maintenance swallows it because the body is
already committed.

Offline: no GitHub or Postgres required.
"""
from __future__ import annotations

import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
for path in (ROOT, APP):
    if path not in sys.path:
        sys.path.insert(0, path)

import _compat_analysis as compat  # noqa: E402
import delivery_queue as deliveries  # noqa: E402
import event_budget  # noqa: E402
from event_queue import EventQueue  # noqa: E402
from env_config import ConfigError  # noqa: E402
import ingest  # noqa: E402
import server_dbops  # noqa: E402
import webhook_handlers  # noqa: E402


FAIL = 0


def check(condition: bool, label: str) -> None:
    global FAIL
    print(("PASS: " if condition else "FAIL: ") + label)
    if not condition:
        FAIL += 1


def test_type_boundary() -> None:
    cancellation = event_budget.EventBudgetExceeded("boundary probe")
    exception_handler_ran = False
    propagated = None
    try:
        try:
            raise cancellation
        except Exception:
            exception_handler_ran = True
    except event_budget.EventBudgetExceeded as exc:
        propagated = exc

    check(
        issubclass(event_budget.EventBudgetExceeded, BaseException)
        and not issubclass(event_budget.EventBudgetExceeded, Exception),
        "EventBudgetExceeded is BaseException cancellation, not a catchable Exception timeout",
    )
    check(
        not exception_handler_ran and propagated is cancellation,
        "a generic fail-open Exception handler cannot swallow delivery cancellation",
    )


def test_event_queue_contains_cancellation() -> None:
    runs = []

    def processor(_event_type, payload, _db, _gh):
        key = payload["key"]
        runs.append(key)
        if key == "poison":
            raise event_budget.EventBudgetExceeded("poison exhausted its event budget")
        return {"ok": True}

    queue = EventQueue(
        None,
        None,
        processor,
        account_of=lambda payload: payload["account"],
        repo_of=lambda payload: payload["repo"],
        retry_attempts=3,
        retry_base_seconds=0,
    ).start()
    accepted = (
        queue.submit(
            "push",
            {"key": "poison", "account": "tenant-a", "repo": "a/poison"},
            "cancel-poison",
        )
        and queue.submit(
            "push",
            {"key": "healthy", "account": "tenant-b", "repo": "b/healthy"},
            "cancel-healthy",
        )
    )
    drained = queue.wait_idle(2.0)

    check(accepted and drained, "cancellation generation and following tenant both drain")
    check(
        runs.count("poison") == 1
        and runs.count("healthy") == 1
        and queue.retried() == 0,
        f"cancellation is terminal for one in-memory generation without retry ({runs})",
    )
    check(
        queue.failed() == 1 and queue.processed() == 1,
        "cancellation is failed once and the following healthy event is processed",
    )
    check(queue.is_alive(), "explicit cancellation catch keeps the daemon worker alive")


def test_durable_wrapper_releases_and_preserves_original() -> None:
    store = deliveries.DeliveryStore("postgresql://unused")
    release_calls = []
    finish_calls = []
    cancellation = event_budget.EventBudgetExceeded("original processor cancellation")

    store.claim = lambda key: {
        "claimed": True,
        "lease_generation": 17,
        "event_type": "push",
        "payload": {"repository": {"full_name": f"acme/{key}"}},
    }

    def release(key, error, lease_generation):
        release_calls.append(
            (key, error, lease_generation, event_budget.in_terminal_scope())
        )
        # Cleanup failure must never replace the cancellation that owns this
        # unwind. Stale/dead-instance recovery remains the final backstop.
        raise RuntimeError("simulated release ACK loss")

    def finish(key, lease_generation):
        finish_calls.append((key, lease_generation))
        return True

    store.release = release
    store.finish = finish

    def processor(_event_type, payload, _db, _gh):
        if payload["_veripsa_delivery_key"] == "poison":
            raise cancellation
        return {"ok": True}

    wrapped = store.wrap_processor(processor)
    caught = None
    try:
        wrapped(
            "push",
            deliveries.with_delivery_key({}, "poison"),
            None,
            None,
        )
    except event_budget.EventBudgetExceeded as exc:
        caught = exc

    healthy = wrapped(
        "push",
        deliveries.with_delivery_key({}, "healthy"),
        None,
        None,
    )

    check(
        caught is cancellation,
        "durable cleanup failure preserves the original cancellation object",
    )
    check(
        len(release_calls) == 1
        and release_calls[0][0] == "poison"
        and release_calls[0][1] is cancellation
        and release_calls[0][2] == 17
        and release_calls[0][3] is True,
        f"cancellation releases the exact lease inside terminal scope ({release_calls})",
    )
    check(
        store._inflight is None,
        "durable inflight registry is cleared even when cancellation cleanup fails",
    )
    check(
        healthy == {"ok": True} and finish_calls == [("healthy", 17)],
        "the same durable wrapper remains usable for the following healthy delivery",
    )


class _StatsCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _sql, _args=()):
        return None

    def fetchone(self):
        return ("1",)


class _CommittedEventConnection:
    def __init__(self):
        self.rollbacks = 0

    def cursor(self):
        return _StatsCursor()

    def rollback(self):
        self.rollbacks += 1


def test_postcommit_stats_swallow_cancellation() -> None:
    connection = _CommittedEventConnection()
    cancellation = event_budget.EventBudgetExceeded(
        "no event budget remains for post-commit ANALYZE"
    )
    original_connect = server_dbops._bounded_connect

    def cancelled_connect(_dsn):
        raise cancellation

    server_dbops._bounded_connect = cancelled_connect
    escaped = None
    try:
        try:
            refreshed = server_dbops._refresh_graph_stats_if_bulk_loaded(
                connection, "postgresql://unused"
            )
        except BaseException as exc:
            escaped = exc
            refreshed = None
    finally:
        server_dbops._bounded_connect = original_connect

    check(
        refreshed is False and escaped is None,
        "post-commit graph-stat cancellation is swallowed as best-effort maintenance",
    )
    check(
        connection.rollbacks == 1,
        "post-commit stats probe closes its implicit read transaction before cancellation",
    )


def test_fail_open_seams_propagate_cancellation() -> None:
    # _compat_analysis is the only production seam that intentionally catches a
    # SystemExit-derived ConfigError. It must narrow to that exact type.
    original_env_int = compat.env_int
    compat.env_int = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ConfigError("bad optional compat cap")
    )
    try:
        config_defaulted = compat._cap("TEST_CAP", 3, 9)
    finally:
        compat.env_int = original_env_int

    compat_cancel = event_budget.EventBudgetExceeded("compat cancellation")
    compat.env_int = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        compat_cancel
    )
    compat_propagated = None
    try:
        try:
            compat._cap("TEST_CAP", 3, 9)
        except event_budget.EventBudgetExceeded as exc:
            compat_propagated = exc
    finally:
        compat.env_int = original_env_int

    check(
        config_defaulted == 3 and compat_propagated is compat_cancel,
        "optional compat cap catches ConfigError only and propagates cancellation",
    )

    # Representative broad fail-open boundaries must now be transparent to
    # BaseException cancellation without each site learning the cancellation
    # type. These protect graph self-heal and PR file/head reads.
    heal_cancel = event_budget.EventBudgetExceeded("self-heal cancellation")
    original_freshness = ingest.graph_freshness
    original_ingest_push = ingest.ingest_push
    ingest.graph_freshness = lambda *_args, **_kwargs: {
        "stored_sha": "a" * 40,
        "head_sha": "b" * 40,
        "behind": True,
        "head_committed_at": None,
    }
    ingest.ingest_push = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        heal_cancel
    )
    heal_propagated = None
    try:
        try:
            ingest.self_heal_main_graph(
                lambda *_args, **_kwargs: None,
                object(),
                "acme/repo",
                "main",
            )
        except event_budget.EventBudgetExceeded as exc:
            heal_propagated = exc
    finally:
        ingest.graph_freshness = original_freshness
        ingest.ingest_push = original_ingest_push

    files_cancel = event_budget.EventBudgetExceeded("PR files cancellation")

    class _FilesGH:
        def list_pr_file_metadata(self, *_args, **_kwargs):
            raise files_cancel

    files_propagated = None
    try:
        webhook_handlers._pr_fetch_changed(
            _FilesGH(),
            {
                "should_analyze": True,
                "repo": "acme/repo",
                "pr": 7,
                "prj": {"changed_files": 1},
                "trace_id": "",
                "delivery": "",
            },
        )
    except event_budget.EventBudgetExceeded as exc:
        files_propagated = exc

    snapshot_cancel = event_budget.EventBudgetExceeded(
        "PR snapshot cancellation"
    )

    class _SnapshotGH:
        def get_pull_request(self, *_args, **_kwargs):
            raise snapshot_cancel

    snapshot_propagated = None
    try:
        webhook_handlers._pr_files_snapshot_is_current(
            _SnapshotGH(),
            {
                "repo": "acme/repo",
                "pr": 7,
                "head_sha": "h" * 40,
                "base": "main",
                "base_sha": "m" * 40,
                "prj": {"changed_files": 1},
            },
            1,
            1,
        )
    except event_budget.EventBudgetExceeded as exc:
        snapshot_propagated = exc

    check(
        heal_propagated is heal_cancel
        and files_propagated is files_cancel
        and snapshot_propagated is snapshot_cancel,
        "graph/PR fail-open Exception seams all propagate delivery cancellation",
    )


def main() -> int:
    print("=== EVENT CANCELLATION BOUNDARY GATE ===")
    test_type_boundary()
    test_event_queue_contains_cancellation()
    test_durable_wrapper_releases_and_preserves_original()
    test_postcommit_stats_swallow_cancellation()
    test_fail_open_seams_propagate_cancellation()
    if FAIL:
        print(f"EVENT CANCELLATION BOUNDARY GATE: FAIL ({FAIL} check(s))")
        return 1
    print("EVENT CANCELLATION BOUNDARY GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
