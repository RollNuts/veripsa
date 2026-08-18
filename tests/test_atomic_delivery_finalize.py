#!/usr/bin/env python3
"""Runtime gate for one-transaction webhook body + durable completion.

Offline: fake connections model server-side COMMIT/ACK loss while exercising
the real DeliveryStore wrapper and real make_db_processor transaction code.
"""
from __future__ import annotations

import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
for path in (ROOT, APP):
    if path not in sys.path:
        sys.path.insert(0, path)

import delivery_queue as DQ  # noqa: E402
import event_budget  # noqa: E402
import event_processor as EP  # noqa: E402
from event_queue import EventQueue  # noqa: E402


FAIL = 0


def check(condition, label: str) -> None:
    global FAIL
    print(("PASS: " if condition else "FAIL: ") + label)
    if not condition:
        FAIL += 1


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, args=()):
        self.conn.sql.append((sql, args))
        self.row = None
        if "note_installation_account_metadata_with_authority" in sql:
            self.conn.metadata_autocommit_states.append(self.conn.autocommit)
        if "enter_existing_installation_with_authority" in sql:
            self.row = ("ACCT-GH-7",)
        elif "finish_webhook_delivery_with_authority" in sql:
            self.conn.finish_stages.append(tuple(args))
            self.row = (self.conn.finish_result,)

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, *, commit_mode="ok", finish_result=True):
        self.autocommit = False
        self.commit_mode = commit_mode
        self.finish_result = finish_result
        self.sql = []
        self.finish_stages = []
        self.body_writes = []
        self.metadata_autocommit_states = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.closed = False
        self.server_committed = False
        self.commit_error = (
            event_budget.EventBudgetExceeded("commit deadline response lost")
            if commit_mode == "cancel_after_commit"
            else RuntimeError("commit response lost")
        )

    def cursor(self):
        return FakeCursor(self)

    def body(self, sql, args=()):
        self.body_writes.append((sql, args))
        return None

    def commit(self):
        self.commit_calls += 1
        if self.commit_mode in ("ack_lost", "cancel_after_commit"):
            self.server_committed = True
            raise self.commit_error
        if self.commit_mode == "precommit_error":
            raise self.commit_error
        self.server_committed = True

    def rollback(self):
        self.rollback_calls += 1

    def close(self):
        self.closed = True

    def cancel(self):
        return None

    def fileno(self):
        return -1


class FakeServer:
    def __init__(self):
        self.handler_calls = []
        self.handler_error = None

    @staticmethod
    def _as_obj(value):
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _as_list(value):
        return value if isinstance(value, list) else []

    @staticmethod
    def _scoped_db(conn):
        return conn.body

    def handle_event(self, event_type, payload, db, _gh, coalesce=None):
        assert DQ._DELIVERY_EXECUTION_AUTHORITY not in payload
        self.handler_calls.append((event_type, payload.get("_veripsa_delivery_key"), coalesce))
        if self.handler_error is not None:
            raise self.handler_error
        db("HANDLER WRITE", ())
        return {"handled": True}


class FakeStore(DQ.DeliveryStore):
    def __init__(self, *, resolution="committed", resolver_error=None):
        super().__init__("postgresql://unused")
        self.resolution = resolution
        self.resolver_error = resolver_error
        self.finish_calls = []
        self.release_calls = []
        self.resolve_calls = []

    def finish(self, key, lease_generation):
        self.finish_calls.append(
            (key, lease_generation, event_budget.in_terminal_scope()))
        return True

    def release(self, key, error, lease_generation):
        self.release_calls.append(
            (key, error, lease_generation, event_budget.in_terminal_scope()))
        return "queued"

    def resolve_commit(self, key, error, lease_generation):
        self.resolve_calls.append(
            (key, error, lease_generation, event_budget.in_terminal_scope()))
        if self.resolver_error is not None:
            raise self.resolver_error
        return self.resolution


def payload(key: str, generation: int = 7, *, installation=False):
    out = {
        "_veripsa_delivery_key": key,
        DQ._DELIVERY_PRECLAIMED: True,
        DQ._DELIVERY_LEASE_GENERATION: generation,
        "ref": "refs/heads/main",
        "after": "a" * 40,
        "repository": {
            "id": 77,
            "full_name": "acme/repo",
            "owner": {"id": 7, "login": "acme", "type": "Organization"},
            "default_branch": "main",
        },
        "commits": [],
    }
    if installation:
        out["installation"] = {
            "id": 700,
            "account": {"id": 7, "login": "acme", "type": "Organization"},
        }
    return out


class Harness:
    def __init__(self):
        self.server = FakeServer()
        self.connections = []
        self.originals = {}

    def install(self):
        for name in (
            "_server",
            "_connect_event_db",
            "_take_live_repository_locks",
            "_refresh_graph_stats_if_bulk_loaded",
            "_install_fanout_repos",
        ):
            self.originals[name] = getattr(EP, name)
        EP._server = lambda: self.server
        EP._connect_event_db = lambda _dsn, _timeout: self.connections.pop(0)
        EP._take_live_repository_locks = lambda *_args, **_kwargs: True
        EP._refresh_graph_stats_if_bulk_loaded = lambda *_args, **_kwargs: False

    def restore(self):
        for name, value in self.originals.items():
            setattr(EP, name, value)


def test_atomic_success_and_direct_compatibility(harness: Harness) -> None:
    store = FakeStore()
    atomic_conn = FakeConnection()
    harness.connections.append(atomic_conn)
    wrapped = store.wrap_processor(EP.make_db_processor("postgresql://unused"))
    result = wrapped("push", payload("atomic-ok"), None, object())

    finish_index = next(
        i for i, (sql, _args) in enumerate(atomic_conn.sql)
        if "finish_webhook_delivery_with_authority" in sql
    )
    handler_index = atomic_conn.body_writes.index(("HANDLER WRITE", ()))
    check(
        result is None
        and atomic_conn.finish_stages == [("atomic-ok", 7)]
        and atomic_conn.commit_calls == 1
        and store.finish_calls == []
        and store.release_calls == []
        and store.resolve_calls == [],
        "normal body and exact durable finish commit once; wrapper skips separate finish",
    )
    check(
        handler_index == 0 and finish_index == len(atomic_conn.sql) - 1,
        "handler cannot see the capability and exact finish is the transaction's final SQL",
    )
    check(
        atomic_conn.metadata_autocommit_states == [True],
        "shared installation metadata commits before the long repository body transaction",
    )

    direct_conn = FakeConnection()
    harness.connections.append(direct_conn)
    direct = EP.make_db_processor("postgresql://unused")
    direct_payload = payload("direct")
    direct_payload.pop(DQ._DELIVERY_PRECLAIMED)
    direct_payload.pop(DQ._DELIVERY_LEASE_GENERATION)
    direct_result = direct(
        "push",
        direct_payload,
        None,
        object(),
    )
    check(
        direct_result is None
        and direct_conn.finish_stages == []
        and direct_conn.commit_calls == 1
        and direct_conn.metadata_autocommit_states == [True],
        "direct/offline caller without execution capability keeps legacy separate-finish compatibility",
    )


def test_metadata_scope_guard() -> None:
    conn = FakeConnection()
    conn.autocommit = False
    caught = None
    try:
        EP._note_installation_account_metadata_outside_body(
            conn, "7", "acme", "Organization")
    except RuntimeError as error:
        caught = error
    check(
        caught is not None
        and "outside the event body transaction" in str(caught)
        and conn.sql == [],
        "metadata scope guard fails closed before any SQL inside a body transaction",
    )


def test_ack_loss_is_processed(harness: Harness) -> None:
    store = FakeStore(resolution="committed")
    conn = FakeConnection(commit_mode="ack_lost")
    harness.connections.append(conn)
    wrapped = store.wrap_processor(EP.make_db_processor("postgresql://unused"))
    queue = EventQueue(
        None,
        object(),
        wrapped,
        account_of=lambda body: body["repository"]["owner"]["id"],
        repo_of=lambda body: body["repository"]["full_name"],
        retry_attempts=2,
        retry_base_seconds=0,
    ).start()
    accepted = queue.submit("push", payload("ack-lost"), "ack-lost")
    drained = queue.wait_idle(2.0)
    check(
        accepted and drained
        and queue.processed() == 1
        and queue.failed() == 0
        and queue.retried() == 0
        and len(harness.server.handler_calls) >= 1
        and conn.finish_stages == [("ack-lost", 7)]
        and len(store.resolve_calls) == 1
        and store.resolve_calls[0][0] == "ack-lost"
        and store.resolve_calls[0][1] is conn.commit_error
        and store.resolve_calls[0][2:] == (7, True)
        and store.finish_calls == []
        and store.release_calls == [],
        "server-side commit with lost ACK resolves committed and EventQueue records one success",
    )


def test_uncommitted_and_aba_preserve_original(harness: Harness) -> None:
    queued_store = FakeStore(resolution="queued")
    queued_conn = FakeConnection(commit_mode="precommit_error")
    harness.connections.append(queued_conn)
    queued_wrapped = queued_store.wrap_processor(
        EP.make_db_processor("postgresql://unused"))
    caught = None
    try:
        queued_wrapped("push", payload("precommit"), None, object())
    except RuntimeError as error:
        caught = error
    check(
        caught is queued_conn.commit_error
        and queued_conn.finish_stages == [("precommit", 7)]
        and queued_store.resolve_calls[0][2:] == (7, True)
        and queued_store.release_calls == []
        and queued_store.finish_calls == [],
        "staged-but-uncommitted body resolves queued and rethrows the original commit error",
    )

    aba_store = FakeStore(resolution="ownership_lost")
    aba_conn = FakeConnection(commit_mode="precommit_error")
    harness.connections.append(aba_conn)
    aba_wrapped = aba_store.wrap_processor(EP.make_db_processor("postgresql://unused"))
    aba_caught = None
    try:
        aba_wrapped("push", payload("aba", generation=19), None, object())
    except RuntimeError as error:
        aba_caught = error
    check(
        aba_caught is aba_conn.commit_error
        and aba_store.resolve_calls[0][2] == 19
        and aba_store.release_calls == []
        and aba_store.finish_calls == [],
        "generation ABA/ownership loss cannot authorize success or mutate through a second release",
    )

    resolver_failure = RuntimeError("resolver unavailable")
    failed_store = FakeStore(resolver_error=resolver_failure)
    failed_conn = FakeConnection(commit_mode="precommit_error")
    harness.connections.append(failed_conn)
    failed_wrapped = failed_store.wrap_processor(
        EP.make_db_processor("postgresql://unused"))
    failed_caught = None
    try:
        failed_wrapped("push", payload("resolver-fails"), None, object())
    except RuntimeError as error:
        failed_caught = error
    check(
        failed_caught is failed_conn.commit_error,
        "resolver failure never replaces the original commit exception",
    )


def test_stage_before_failure_and_compat_paths(harness: Harness) -> None:
    original = RuntimeError("handler failed before exact finish")
    harness.server.handler_error = original
    store = FakeStore()
    conn = FakeConnection()
    harness.connections.append(conn)
    wrapped = store.wrap_processor(EP.make_db_processor("postgresql://unused"))
    caught = None
    try:
        wrapped("push", payload("body-fails"), None, object())
    except RuntimeError as error:
        caught = error
    harness.server.handler_error = None
    check(
        caught is original
        and conn.finish_stages == []
        and conn.rollback_calls == 1
        and len(store.release_calls) == 1
        and store.release_calls[0][0] == "body-fails"
        and store.release_calls[0][1] is original
        and store.release_calls[0][2:] == (7, True),
        "failure before finish staging rolls back and uses the legacy exact release",
    )

    false_store = FakeStore()
    false_conn = FakeConnection(finish_result=False)
    harness.connections.append(false_conn)
    false_wrapped = false_store.wrap_processor(
        EP.make_db_processor("postgresql://unused"))
    false_caught = None
    try:
        false_wrapped("push", payload("finish-false"), None, object())
    except RuntimeError as error:
        false_caught = error
    check(
        str(false_caught) == "durable delivery exact finish lost lease authority"
        and false_conn.commit_calls == 0
        and false_conn.rollback_calls == 1
        and false_store.resolve_calls == []
        and len(false_store.release_calls) == 1
        and false_store.release_calls[0][0] == "finish-false"
        and false_store.release_calls[0][2:] == (7, True),
        "false exact-finish authority rolls the body back before COMMIT and uses exact release",
    )

    early_store = FakeStore()
    early_wrapped = early_store.wrap_processor(
        EP.make_db_processor("postgresql://unused"))
    early_wrapped(
        "push",
        {
            "_veripsa_delivery_key": "early-noop",
            DQ._DELIVERY_PRECLAIMED: True,
            DQ._DELIVERY_LEASE_GENERATION: 7,
        },
        None,
        object(),
    )
    check(
        early_store.finish_calls == [("early-noop", 7, True)],
        "early pre-body no-op retains the legacy separate exact finish",
    )

    fanout_store = FakeStore()
    fanout_processor = EP.make_db_processor("postgresql://unused")
    original_entries = EP._install_fanout_entries
    original_prepare = EP._prepare_durable_fanout
    original_fanout = EP._process_install_event_per_repo_locked
    observed_authorities = []
    EP._install_fanout_entries = lambda *_args, **_kwargs: [
        {"key": "id:77", "full_name": "acme/repo", "id": "77"},
    ]
    EP._prepare_durable_fanout = (
        lambda _dsn, _timeout, _authority, proposal: (proposal, set()))

    def atomic_fanout(*_args, execution_authority=None, **_kwargs):
        observed_authorities.append(execution_authority)
        return execution_authority

    EP._process_install_event_per_repo_locked = atomic_fanout
    try:
        fanout_store.wrap_processor(fanout_processor)(
            "push", payload("fanout"), None, object())
    finally:
        EP._process_install_event_per_repo_locked = original_fanout
        EP._prepare_durable_fanout = original_prepare
        EP._install_fanout_entries = original_entries
    check(
        len(observed_authorities) == 1
        and type(observed_authorities[0]) is DQ._DeliveryExecutionAuthority
        and fanout_store.finish_calls == []
        and fanout_store.release_calls == [],
        "durable fanout carries exact execution authority and skips separate finish",
    )


def test_atomic_generation_fence(harness: Harness) -> None:
    store = FakeStore()
    conn = FakeConnection()
    harness.connections.append(conn)
    admissions = iter((
        {"ok": True, "admitted": True, "proof_required": False, "reason": "setup"},
        {"ok": True, "admitted": False, "proof_required": False, "reason": "stale"},
    ))
    original_admission = EP._installation_admission
    EP._installation_admission = lambda *_args, **_kwargs: next(admissions)
    before_handlers = len(harness.server.handler_calls)
    try:
        store.wrap_processor(EP.make_db_processor("postgresql://unused"))(
            "push", payload("atomic-fence", installation=True), None, object())
    finally:
        EP._installation_admission = original_admission
    check(
        len(harness.server.handler_calls) == before_handlers
        and conn.finish_stages == [("atomic-fence", 7)]
        and conn.commit_calls == 1
        and store.finish_calls == [],
        "atomic generation-fence no-op converges on the same body+finish commit",
    )


def test_typed_commit_cancellation(harness: Harness) -> None:
    store = FakeStore(resolution="committed")
    conn = FakeConnection(commit_mode="cancel_after_commit")
    harness.connections.append(conn)
    result = store.wrap_processor(EP.make_db_processor("postgresql://unused"))(
        "push", payload("typed-commit"), None, object())
    check(
        result is None
        and len(store.resolve_calls) == 1
        and store.resolve_calls[0][1] is conn.commit_error
        and store.release_calls == [],
        "typed cancellation after finish staging resolves committed instead of false failure/replay",
    )

    forged = {
        "_veripsa_delivery_key": "forged",
        DQ._DELIVERY_EXECUTION_AUTHORITY: 7,
    }
    forged_caught = None
    before_handlers = len(harness.server.handler_calls)
    try:
        EP.make_db_processor("postgresql://unused")(
            "push", forged, None, object())
    except RuntimeError as error:
        forged_caught = error
    check(
        str(forged_caught) == "webhook delivery execution authority is malformed"
        and DQ._DELIVERY_EXECUTION_AUTHORITY not in forged
        and len(harness.server.handler_calls) == before_handlers
        and DQ._DELIVERY_EXECUTION_AUTHORITY not in DQ._drop_internal_markers({
            DQ._DELIVERY_EXECUTION_AUTHORITY: object(),
        }),
        "execution authority is popped/validated before handlers and stripped from durable payload copies",
    )


def main() -> int:
    print("=== ATOMIC DELIVERY FINALIZE GATE ===")
    harness = Harness()
    harness.install()
    try:
        test_atomic_success_and_direct_compatibility(harness)
        test_metadata_scope_guard()
        test_ack_loss_is_processed(harness)
        test_uncommitted_and_aba_preserve_original(harness)
        test_stage_before_failure_and_compat_paths(harness)
        test_atomic_generation_fence(harness)
        test_typed_commit_cancellation(harness)
    finally:
        harness.restore()
    print("ATOMIC DELIVERY FINALIZE GATE:", "PASS" if FAIL == 0 else "FAIL")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
