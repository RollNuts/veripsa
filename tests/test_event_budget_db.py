#!/usr/bin/env python3
"""The webhook event deadline must reach every live Postgres blocking seam."""
from __future__ import annotations

import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
for path in (ROOT, APP):
    if path not in sys.path:
        sys.path.insert(0, path)

import event_budget as budget
import delivery_queue as deliveries
import event_processor as processor
from event_queue import EventQueue
import server_dbops as dbops


class Cursor:
    def __init__(self, result=None):
        self.sql = []
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, args=()):
        self.sql.append((str(sql), args))

    def fetchone(self):
        return (self.result,) if self.result is not None else None


class Connection:
    def __init__(self, result=None):
        self.cursors = []
        self.result = result
        self.autocommit = False
        self.closed = False

    def cursor(self):
        cur = Cursor(self.result)
        self.cursors.append(cur)
        return cur

    def close(self):
        self.closed = True


class ProcessorConnection(Connection):
    def __init__(self):
        super().__init__()
        self.autocommit = None
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self):
        cur = super().cursor()
        cur.fetchone = lambda: ("ACCT-GH-42",)
        return cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok:", message)


def main():
    token = budget.begin(0.20)
    try:
        work_remaining = budget.remaining()
        total_remaining = budget.total_remaining()
        with budget.terminal_scope():
            terminal_remaining = budget.remaining()
        check(
            work_remaining is not None and total_remaining is not None
            and terminal_remaining is not None
            and 0 < work_remaining < terminal_remaining <= total_remaining,
            f"normal work stops before the reserved terminal tail "
            f"(work={work_remaining:.3f}s, terminal={terminal_remaining:.3f}s)",
        )
        check(not budget.in_terminal_scope() and budget.remaining() <= work_remaining,
              "terminal scope restores the earlier work deadline on exit")
    finally:
        budget.end(token)

    # Server statement_timeout cannot interrupt a black-holed TCP response.
    # The absolute guard must shutdown the live socket at the work deadline.
    guarded, peer = socket.socketpair()
    peer.settimeout(0.5)
    token = budget.begin(0.08)
    try:
        guard = budget.arm_connection_deadline(guarded)
        started = time.monotonic()
        eof = peer.recv(1)
        elapsed = time.monotonic() - started
        check(guard.fired and eof == b"" and elapsed <= 0.30,
              f"deadline guard shuts down a blocked DB socket ({elapsed:.3f}s)")
        guard.disarm()
    finally:
        budget.end(token)
        guarded.close()
        peer.close()

    # Disarm is serialized with the callback. Even a connection object that
    # keeps returning its old numeric FD cannot let a cancelled timer shutdown
    # a different socket that later reuses that number.
    old_socket, old_peer = socket.socketpair()
    old_fd = old_socket.fileno()

    class FixedFDConnection:
        def fileno(self):
            return old_fd

    token = budget.begin(0.08)
    replacement = replacement_peer = reused = None
    try:
        guard = budget.arm_connection_deadline(FixedFDConnection())
        guard.disarm()
        replacement, replacement_peer = socket.socketpair()
        old_socket.close()
        os.dup2(replacement.fileno(), old_fd)
        reused = socket.socket(fileno=old_fd)
        time.sleep(0.08)
        replacement_peer.sendall(b"x")
        reused.settimeout(0.2)
        check(not guard.fired and reused.recv(1) == b"x",
              "disarmed guard cannot shutdown a subsequently reused file descriptor")
    finally:
        budget.end(token)
        try:
            old_socket.close()
        except OSError:
            pass
        old_peer.close()
        if reused is not None:
            reused.close()
        if replacement is not None:
            replacement.close()
        if replacement_peer is not None:
            replacement_peer.close()

    token = budget.begin(0.20)
    try:
        live_ms = processor._event_timeout_ms(600_000)
        check(1 <= live_ms <= 200,
              f"processor statement timeout is capped by remaining delivery budget ({live_ms}ms)")

        cur = Cursor()
        dbops._arm_lock_session(cur, lock_timeout_ms=30_000)
        values = [int(sql.rsplit("=", 1)[1]) for sql, _ in cur.sql if "timeout" in sql]
        check(len(values) == 2 and all(1 <= value <= 200 for value in values),
              f"advisory-lock and statement waits share the same delivery budget ({values})")

        conn = Connection()
        calls = []

        def base(sql, args=()):
            calls.append((sql, args))
            return "done"

        run = processor._budgeted_scoped_db(conn, base, 600_000)
        check(run("SELECT 1") == "done" and calls == [("SELECT 1", ())],
              "budget wrapper preserves the injected DB runner result")
        set_sql = conn.cursors[0].sql[0][0]
        set_ms = int(set_sql.rsplit("=", 1)[1])
        check(set_sql.startswith("SET LOCAL statement_timeout") and 1 <= set_ms <= 200,
              f"every body query re-tightens statement_timeout ({set_ms}ms)")

        captured = {}
        original_connect = processor.psycopg2.connect

        def fake_connect(dsn, **kwargs):
            captured["calls"] = int(captured.get("calls") or 0) + 1
            captured.update(kwargs)
            return object()

        processor.psycopg2.connect = fake_connect
        connect_refused = False
        try:
            processor._connect_event_db("postgresql://127.0.0.1/unused", 600_000)
        except budget.EventBudgetExceeded:
            connect_refused = True
        finally:
            processor.psycopg2.connect = original_connect
        check(connect_refused and captured.get("calls", 0) == 0,
              "a sub-second remainder refuses DB connect instead of rounding past the deadline")
        dbops_connect_refused = False
        try:
            dbops._connect_timeout_seconds()
        except budget.EventBudgetExceeded:
            dbops_connect_refused = True
        check(dbops_connect_refused,
              "out-of-band DB operations also refuse a sub-second connect")
    finally:
        budget.end(token)

    token = budget.begin(1.90)
    captured = {}
    original_connect = processor.psycopg2.connect

    def fake_connect(dsn, **kwargs):
        captured.update(kwargs)
        return object()

    processor.psycopg2.connect = fake_connect
    try:
        try:
            marker = processor._connect_event_db("postgresql://127.0.0.1/unused", 600_000)
            background_timeout = dbops._connect_timeout_seconds()
        finally:
            processor.psycopg2.connect = original_connect
        startup_values = {
            name: int(value)
            for name, value in re.findall(
                r"(statement_timeout|lock_timeout)=(\d+)",
                captured.get("options", ""),
            )
        }
        check(marker is not None and captured.get("connect_timeout") == 1
              and background_timeout == 1,
              f"whole-second DB connect bounds floor the remaining budget "
              f"(live={captured}, out_of_band={background_timeout})")
        check(
            captured.get("options", "").endswith("-c search_path=core")
            and 1 <= startup_values.get("statement_timeout", 0) <= 1900
            and 1 <= startup_values.get("lock_timeout", 0) <= 1900,
            f"processor startup options protect its first SQL round-trip ({captured.get('options')})",
        )

        store_calls = []
        store_connections = []
        original_store_connect = deliveries.psycopg2.connect

        class StoreCursor(Cursor):
            def execute(self, sql, args=()):
                super().execute(sql, args)
                if "resolve_webhook_delivery_defer" in str(sql):
                    self.result = "deferred"
                elif "resolve_webhook_delivery_release" in str(sql):
                    self.result = "queued"

        class StoreConnection(Connection):
            def cursor(self):
                cur = StoreCursor(self.result)
                self.cursors.append(cur)
                return cur

        def fake_store_connect(_dsn, **kwargs):
            conn = StoreConnection(True)
            store_calls.append(dict(kwargs))
            store_connections.append(conn)
            return conn

        deliveries.psycopg2.connect = fake_store_connect
        try:
            store = deliveries.DeliveryStore("postgresql://127.0.0.1/unused")
            store.claim("delivery-budget")
            store.stamp_owner("delivery-budget", 1)
            store.defer(
                "delivery-budget", datetime.now(timezone.utc) + timedelta(seconds=1),
                "budgeted deferral", 1,
            )
            store.release("delivery-budget", "failed", 1)
            store.finish("delivery-budget", 1)
            store._set_inflight("delivery-budget", 1)
            store.expire_inflight_lease(grace_seconds=1)
        finally:
            deliveries.psycopg2.connect = original_store_connect

        operation_sql = [
            sql for conn in store_connections for cur in conn.cursors
            for sql, _args in cur.sql if sql.startswith("SELECT core.")
        ]
        expected_ops = (
            "claim_webhook", "stamp_webhook",
            "resolve_webhook_delivery_defer",
            "resolve_webhook_delivery_release",
            "finish_webhook", "expire_webhook",
        )
        check(len(store_calls) == len(expected_ops)
              and all(any(name in sql for sql in operation_sql) for name in expected_ops),
              "claim/stamp/defer/release/finish/lease-cleanup all cross the budgeted store boundary")
        option_values = [
            int(value)
            for kwargs in store_calls
            for value in re.findall(
                r"(?:statement_timeout|lock_timeout)=(\d+)", kwargs.get("options", ""))
        ]
        set_values = [
            int(sql.rsplit("=", 1)[1])
            for conn in store_connections for cur in conn.cursors
            for sql, _args in cur.sql
            if sql.startswith(("SET statement_timeout", "SET lock_timeout"))
        ]
        check(all(call.get("connect_timeout") == 1 for call in store_calls),
              f"DeliveryStore connect waits floor the event remainder ({store_calls})")
        check(option_values and set_values
              and all(1 <= value <= 1900 for value in option_values + set_values),
              f"DeliveryStore startup/query timeouts share the event remainder "
              f"(startup={option_values}, query={set_values})")
    finally:
        budget.end(token)

    token = budget.begin(0.20)
    commit_conn = ProcessorConnection()
    try:
        processor._commit_with_event_budget(commit_conn, 600_000)
        commit_sql = [
            sql for cur in commit_conn.cursors for sql, _args in cur.sql
            if sql.startswith("SET LOCAL statement_timeout")
        ]
        commit_ms = int(commit_sql[-1].rsplit("=", 1)[1]) if commit_sql else 0
        check(commit_conn.commits == 1 and 1 <= commit_ms <= 200,
              f"COMMIT inherits a freshly tightened remaining-budget timeout ({commit_ms}ms)")
    finally:
        budget.end(token)

    token = budget.begin(0.01)
    try:
        time.sleep(0.02)
        raised = False
        try:
            processor._event_timeout_ms(600_000)
        except budget.EventBudgetExceeded:
            raised = True
        check(raised, "an expired delivery cannot start another DB statement")

        store_connects = {"count": 0}
        original_store_connect = deliveries.psycopg2.connect

        def forbidden_store_connect(*_args, **_kwargs):
            store_connects["count"] += 1
            raise AssertionError("expired cleanup must not enter libpq")

        deliveries.psycopg2.connect = forbidden_store_connect
        try:
            expired_store = deliveries.DeliveryStore("postgresql://127.0.0.1/unused")
            terminal_result = expired_store.release(
                "expired-delivery", "timeout", 1)
            terminal_snapshot = expired_store.liveness_snapshot()
        finally:
            deliveries.psycopg2.connect = original_store_connect
        check(
            terminal_result == "pending"
            and store_connects["count"] == 0
            and terminal_snapshot["pending_terminal_depth"] == 1
            and terminal_snapshot["pending_terminal_failures"] == 2,
            "expired durable cleanup starts no DB wait and hands one exact intent to daemon recovery",
        )
    finally:
        budget.end(token)

    # Drive the real make_db_processor transaction shell with an offline fake
    # connection. The handler consumes the budget and returns normally: the
    # final pre-commit check must turn that into rollback, never commit.
    server = processor._server()
    proc_conn = ProcessorConnection()
    expired_after_handler = False
    original_connect_event_db = processor._connect_event_db
    original_take_locks = processor._take_live_repository_locks
    original_handle_event = server.handle_event
    original_scoped_db = server._scoped_db
    original_raise_if_expired = processor._event_budget.raise_if_expired

    def deadline_check():
        if expired_after_handler:
            raise budget.EventBudgetExceeded("expired after handler")

    def late_handler(*_args, **_kwargs):
        nonlocal expired_after_handler
        expired_after_handler = True
        return {"handled": True}

    processor._connect_event_db = lambda _dsn, _statement_ms: proc_conn
    processor._take_live_repository_locks = lambda *_args, **_kwargs: None
    server.handle_event = late_handler
    server._scoped_db = lambda _conn: (lambda _sql, _args=(): None)
    processor._event_budget.raise_if_expired = deadline_check
    deadline_raised = False
    try:
        live_processor = processor.make_db_processor("postgresql://127.0.0.1/unused")
        try:
            live_processor(
                "push",
                {
                    "_veripsa_delivery_key": "delivery-deadline",
                    "repository": {
                        "id": 101,
                        "full_name": "acme/repo",
                        "owner": {"id": 42, "login": "acme"},
                    },
                    "installation": {"account": {"id": 42}},
                },
                None,
                object(),
            )
        except budget.EventBudgetExceeded:
            deadline_raised = True
    finally:
        processor._connect_event_db = original_connect_event_db
        processor._take_live_repository_locks = original_take_locks
        server.handle_event = original_handle_event
        server._scoped_db = original_scoped_db
        processor._event_budget.raise_if_expired = original_raise_if_expired

    check(deadline_raised, "handler-return deadline expiry escapes the live processor for durable recovery")
    check(proc_conn.commits == 0, "an expired handler result is never committed")
    check(proc_conn.rollbacks == 1 and proc_conn.closed == 1,
          "an expired handler result rolls back once and releases its connection")

    # The socket guard itself is what turns a black-holed libpq operation into a
    # psycopg OperationalError. The processor must translate only a guard-fired
    # exception into the BaseException cancellation boundary; an identical
    # external error before the deadline remains an ordinary database failure.
    original_connect_event_db = processor._connect_event_db
    original_take_locks = processor._take_live_repository_locks
    original_handle_event = server.handle_event
    original_scoped_db = server._scoped_db
    original_arm_guard = processor._event_budget.arm_connection_deadline

    class GuardProbe:
        def __init__(self, fired):
            self.fired = fired
            self.disarmed = 0

        def disarm(self):
            self.disarmed += 1

    def eof_handler(*_args, **_kwargs):
        raise processor.psycopg2.OperationalError("SSL transport closed")

    guard_results = []
    try:
        processor._take_live_repository_locks = lambda *_args, **_kwargs: None
        server.handle_event = eof_handler
        server._scoped_db = lambda _conn: (lambda _sql, _args=(): None)
        for fired in (True, False):
            eof_conn = ProcessorConnection()
            guard = GuardProbe(fired)
            processor._connect_event_db = lambda _dsn, _statement_ms, c=eof_conn: c
            processor._event_budget.arm_connection_deadline = lambda _conn, g=guard: g
            raised = None
            try:
                processor.make_db_processor("postgresql://127.0.0.1/unused")(
                    "push",
                    {
                        "_veripsa_delivery_key": "private-provider-guid",
                        "repository": {
                            "id": 101,
                            "full_name": "acme/repo",
                            "owner": {"id": 42, "login": "acme"},
                        },
                        "installation": {"account": {"id": 42}},
                    },
                    None,
                    object(),
                )
            except BaseException as exc:
                raised = exc
            guard_results.append((fired, raised, eof_conn, guard))
    finally:
        processor._connect_event_db = original_connect_event_db
        processor._take_live_repository_locks = original_take_locks
        server.handle_event = original_handle_event
        server._scoped_db = original_scoped_db
        processor._event_budget.arm_connection_deadline = original_arm_guard

    fired_case, external_case = guard_results
    check(
        fired_case[0] is True
        and isinstance(fired_case[1], budget.EventBudgetExceeded)
        and isinstance(fired_case[1].__cause__, processor.psycopg2.OperationalError)
        and fired_case[2].rollbacks == 1 and fired_case[2].closed == 1
        and fired_case[3].disarmed >= 1,
        "guard-fired psycopg EOF becomes one rollback-safe durable cancellation",
    )
    check(
        external_case[0] is False
        and isinstance(external_case[1], processor.psycopg2.OperationalError)
        and not isinstance(external_case[1], budget.EventBudgetExceeded)
        and external_case[2].rollbacks == 1 and external_case[2].closed == 1
        and external_case[3].disarmed >= 1,
        "non-guard database transport failure preserves its ordinary failure classification",
    )

    # The installation fan-out has its own per-repository transaction shell;
    # lock the same last-safe-boundary contract there as well.
    fanout_conn = ProcessorConnection()
    expired_after_handler = False
    original_connect_event_db = processor._connect_event_db
    original_take_locks = processor._take_live_repository_locks
    original_handle_event = server.handle_event
    original_scoped_db = server._scoped_db
    original_raise_if_expired = processor._event_budget.raise_if_expired
    processor._connect_event_db = lambda _dsn, _statement_ms: fanout_conn
    processor._take_live_repository_locks = lambda *_args, **_kwargs: None
    server.handle_event = late_handler
    server._scoped_db = lambda _conn: (lambda _sql, _args=(): None)
    processor._event_budget.raise_if_expired = deadline_check
    fanout_deadline_raised = False
    try:
        try:
            processor._process_install_event_per_repo_locked(
                "postgresql://127.0.0.1/unused",
                "installation",
                {
                    "action": "created",
                    "_veripsa_delivery_key": "delivery-fanout-deadline",
                    "installation": {
                        "id": 700,
                        "account": {"id": 42, "login": "acme"},
                    },
                    "repositories": [{"id": 101, "full_name": "acme/repo"}],
                },
                "42",
                object(),
                [{"id": 101, "full_name": "acme/repo"}],
            )
        except budget.EventBudgetExceeded:
            fanout_deadline_raised = True
    finally:
        processor._connect_event_db = original_connect_event_db
        processor._take_live_repository_locks = original_take_locks
        server.handle_event = original_handle_event
        server._scoped_db = original_scoped_db
        processor._event_budget.raise_if_expired = original_raise_if_expired

    check(fanout_deadline_raised,
          "per-repository install handler expiry escapes instead of becoming a soft repo failure")
    check(fanout_conn.commits == 0 and fanout_conn.rollbacks == 1 and fanout_conn.closed == 1,
          "an expired per-repository handler rolls back without committing and closes its connection")

    claim_budget = []
    owner = deliveries.DeliveryStore("postgresql://127.0.0.1/unused")
    owner.claim = lambda _key: (
        claim_budget.append(budget.remaining())
        or {
            "claimed": True,
            "lease_generation": 1,
            "event_type": "push",
            "payload": {"repository": {"full_name": "o/r", "owner": {"id": 1}}},
        }
    )
    owner.stamp_owner = lambda *_args: True
    owner.finish = lambda *_args: True
    queue = EventQueue(
        None, None,
        owner.wrap_processor(lambda *_args, **_kwargs: {"ok": True}),
        account_of=lambda _payload: "1",
        retry_attempts=1,
    ).start()
    queued_payload = deliveries.with_delivery_key(
        {"repository": {"full_name": "o/r", "owner": {"id": 1}}},
        "claim-budget",
    )
    check(queue.submit("push", queued_payload, "claim-budget") and queue.wait_idle(2.0),
          "budget-order probe drains")
    check(len(claim_budget) == 1 and claim_budget[0] is not None and claim_budget[0] > 0,
          f"EventQueue installs the delivery budget before durable claim ({claim_budget})")

    # A normal DeliveryStore wrapper return is a COMMIT capability, not merely a Python success. The handler's
    # transaction has landed before finish() runs on its separate durable-store connection. If finish exhausts
    # the final sliver of event budget, EventQueue must record the committed work as processed and leave the
    # still-processing inbox row to stale/dead-instance recovery — never publish "failed" and authorize a replay
    # of work that already committed. Bare processors retain their post-return deadline gate (worker timeout gate).
    committed_store = deliveries.DeliveryStore("postgresql://127.0.0.1/unused")
    committed_runs = []
    finish_runs = []
    committed_store.claim = lambda _key: {
        "claimed": True,
        "lease_generation": 7,
        "event_type": "push",
        "payload": {"repository": {"full_name": "o/committed", "owner": {"id": 7}}},
    }
    committed_store.stamp_owner = lambda *_args: True

    def expire_during_finish(*_args):
        finish_runs.append(True)
        time.sleep(0.06)
        budget.raise_if_expired()

    def committed_handler(*_args, **_kwargs):
        committed_runs.append(True)
        return {"committed": True}

    committed_store.finish = expire_during_finish
    committed_process = committed_store.wrap_processor(committed_handler)
    check(getattr(committed_process, "_veripsa_commits_before_return", False) is True,
          "durable wrapper exposes the private commit-before-return capability")
    original_begin = budget.begin
    try:
        budget.begin = lambda: original_begin(0.03)
        committed_queue = EventQueue(
            None, None, committed_process,
            account_of=lambda _payload: "7",
            retry_attempts=3,
            retry_base_seconds=0,
        ).start()
        committed_payload = deliveries.with_delivery_key(
            {"repository": {"full_name": "o/committed", "owner": {"id": 7}}},
            "committed-return",
        )
        committed_drained = (
            committed_queue.submit("push", committed_payload, "committed-return")
            and committed_queue.wait_idle(1.0)
        )
    finally:
        budget.begin = original_begin
    check(committed_drained and committed_runs == [True] and finish_runs == [True],
          "post-commit finish expiry drains once without re-running the handler")
    check(committed_queue.processed() == 1
          and committed_queue.failed() == 0
          and committed_queue.retried() == 0,
          "a durable committed return cannot be reclassified as failed after its deadline")

    ambiguous_store = deliveries.DeliveryStore("postgresql://127.0.0.1/unused")
    ambiguous_calls = []

    def blackholed_claim(sql, args=()):
        ambiguous_calls.append((sql, args, budget.in_terminal_scope()))
        if "claim_webhook_delivery_with_authority" in sql:
            raise budget.EventBudgetExceeded("claim commit response blackholed")
        if "recover_ambiguous_webhook_claim_with_authority" in sql:
            return "queued"
        raise AssertionError(sql)

    ambiguous_store._one = blackholed_claim
    ambiguous_raised = False
    try:
        ambiguous_store.claim("claim-blackhole")
    except budget.EventBudgetExceeded:
        ambiguous_raised = True
    claim_args = ambiguous_calls[0][1] if ambiguous_calls else ()
    recover_args = ambiguous_calls[1][1] if len(ambiguous_calls) > 1 else ()
    check(
        ambiguous_raised
        and len(claim_args) == 6 and len(recover_args) == 4
        and claim_args[4] == recover_args[1]
        and len(claim_args[4]) == 63
        and ambiguous_calls[0][2] is False
        and ambiguous_calls[1][2] is True,
        "claim expiry uses the same pre-generated unique owner for terminal ambiguity recovery",
    )

    # Durable no-handler outcomes must be classified before the post-return clock check. This artificial late
    # claim pins the ordering: blocked_by_earlier is still a deferred durable row, never a handler failure.
    deferred_store = deliveries.DeliveryStore("postgresql://127.0.0.1/unused")
    deferred_handler_runs = []

    def late_blocked_claim(_key):
        time.sleep(0.06)
        return {"claimed": False, "reason": "blocked_by_earlier", "status": "queued"}

    deferred_store.claim = late_blocked_claim
    deferred_process = deferred_store.wrap_processor(
        lambda *_args, **_kwargs: deferred_handler_runs.append(True)
    )
    original_begin = budget.begin
    try:
        budget.begin = lambda: original_begin(0.03)
        deferred_queue = EventQueue(
            None, None, deferred_process,
            account_of=lambda _payload: "9",
            retry_attempts=1,
        ).start()
        deferred_payload = deliveries.with_delivery_key(
            {"repository": {"full_name": "o/deferred", "owner": {"id": 9}}},
            "late-deferred-claim",
        )
        deferred_drained = (
            deferred_queue.submit("push", deferred_payload, "late-deferred-claim")
            and deferred_queue.wait_idle(1.0)
        )
    finally:
        budget.begin = original_begin
    check(deferred_drained
          and deferred_queue.claim_deferred() == 1
          and deferred_queue.processed() == 0
          and deferred_queue.failed() == 0
          and deferred_handler_runs == [],
          "durable claim outcome is classified before post-return expiry")

    print("event budget DB gate: PASS")


if __name__ == "__main__":
    main()
