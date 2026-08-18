#!/usr/bin/env python3
"""Offline runtime proof for durable fanout checkpoints and bounded slices."""
from __future__ import annotations

import os
import sys
import threading
import time


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


class State:
    def __init__(self, plan, *, generation=1, attempts=1):
        self.plan = [dict(entry) for entry in plan]
        self.completed = set()
        self.business = []
        self.status = "processing"
        self.generation = generation
        self.attempts = attempts
        self.max_attempts_seen = attempts
        self.complete_calls = []
        self.defer_calls = 0
        self.finish_calls = 0
        self.connections = []
        self.force_complete_lost = False
        self.ack_loss_on_done = False
        self.ack_loss_on_queue = False
        self.ack_error = RuntimeError("simulated final COMMIT ACK loss")
        self.metadata_autocommit_states = []
        self.installation_admission_calls = []

    def connect(self):
        conn = FakeConnection(self)
        self.connections.append(conn)
        return conn

    def claim_next(self):
        assert self.status == "queued"
        self.status = "processing"
        self.generation += 1
        self.attempts += 1
        self.max_attempts_seen = max(self.max_attempts_seen, self.attempts)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, args=()):
        state = self.conn.state
        self.conn.sql.append((sql, args))
        self.row = None
        if "prepare_webhook_delivery_fanout_with_authority" in sql:
            key, generation, _proposal = args
            if (key == "delivery" and generation == state.generation
                    and state.status == "processing"):
                self.row = ({
                    "plan": [dict(entry) for entry in state.plan],
                    "completed": {
                        repo_key: True for repo_key in sorted(state.completed)
                    },
                },)
            else:
                self.row = (None,)
        elif "complete_webhook_delivery_fanout_repository_with_authority" in sql:
            key, generation, repo_key = args
            state.complete_calls.append((key, generation, repo_key))
            if (state.force_complete_lost or key != "delivery"
                    or generation != state.generation
                    or state.status != "processing"):
                self.row = ({"updated": False, "all_done": False},)
            else:
                self.conn.pending_completed.add(repo_key)
                all_completed = {
                    entry["key"] for entry in state.plan
                } == state.completed | self.conn.pending_completed
                self.row = ({"updated": True, "all_done": all_completed},)
        elif "finish_webhook_delivery_with_authority" in sql:
            key, generation = args
            state.finish_calls += 1
            all_completed = {
                entry["key"] for entry in state.plan
            } == state.completed | self.conn.pending_completed
            allowed = (
                key == "delivery"
                and generation == state.generation
                and state.status == "processing"
                and all_completed
            )
            if allowed:
                self.conn.pending_status = "done"
            self.row = (allowed,)
        elif "resolve_webhook_delivery_fanout_defer_with_authority" in sql:
            key, generation, _not_before, _reason = args
            allowed = (
                key == "delivery"
                and generation == state.generation
                and state.status == "processing"
            )
            if allowed:
                state.defer_calls += 1
                self.conn.pending_status = "queued"
                self.conn.pending_attempt_delta = -1
            self.row = ("deferred" if allowed else "ownership_lost",)
        elif "enter_installation_with_authority" in sql:
            self.row = ("acct",)
        elif "enter_existing_installation_with_authority" in sql:
            self.row = ("acct",)
        elif "admit_event_installation_generation_with_authority" in sql:
            installation_id, created_at = args
            state.installation_admission_calls.append(
                (installation_id, created_at, self.conn.autocommit))
            self.row = ({
                "ok": True,
                "admitted": True,
                "proof_required": False,
                "advanced": False,
            },)
        elif "note_installation_account_metadata_with_authority" in sql:
            state.metadata_autocommit_states.append(self.conn.autocommit)

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, state: State):
        self.state = state
        self.autocommit = False
        self.pending_completed = set()
        self.pending_business = []
        self.pending_status = None
        self.pending_attempt_delta = 0
        self.sql = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def body(self, sql, args=()):
        self.sql.append((sql, args))
        if sql == "BUSINESS WRITE":
            self.pending_business.append(args[0])
        return None

    def commit(self):
        self.commit_calls += 1
        self.state.completed.update(self.pending_completed)
        self.state.business.extend(self.pending_business)
        if self.pending_status is not None:
            self.state.status = self.pending_status
        self.state.attempts = max(
            0, self.state.attempts + self.pending_attempt_delta)
        ack_lost = (
            self.state.ack_loss_on_done
            and self.pending_status == "done"
        ) or (
            self.state.ack_loss_on_queue
            and self.pending_status == "queued"
        )
        self._clear()
        if ack_lost:
            raise self.state.ack_error

    def rollback(self):
        self.rollback_calls += 1
        self._clear()

    def _clear(self):
        self.pending_completed.clear()
        self.pending_business.clear()
        self.pending_status = None
        self.pending_attempt_delta = 0

    def close(self):
        self.closed = True

    def cancel(self):
        return None

    def fileno(self):
        return -1


class FakeServer:
    _ALLREPOS_DISCOVERY_MARKER = "_veripsa_allrepos_discovery_state"
    _ACTIVATION_PROOF_MARKER = "_veripsa_activation_installation_proof"
    _STALE_UNINSTALL_MARKER = "_veripsa_stale_uninstall"
    _SUSPEND_PROOF_MARKER = "_veripsa_suspend_proof"

    def __init__(self):
        self.calls = []
        self.cancel_repo = None
        self.cancel_once = False

    @staticmethod
    def _as_obj(value):
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _as_list(value):
        return value if isinstance(value, list) else []

    @staticmethod
    def _scoped_db(conn):
        return conn.body

    @staticmethod
    def _activation_installation_proof(_gh, _payload):
        return {
            "installation_id": "900",
            "account_id": "acct",
            "created_at": "2026-07-28T00:00:00+00:00",
            "suspended": False,
        }

    def handle_event(self, event_type, payload, db, _gh, coalesce=None):
        field = EP._INSTALL_FANOUT_FIELD[(event_type, payload["action"])]
        repo = payload[field][0]["full_name"]
        self.calls.append(repo)
        if repo == self.cancel_repo and not self.cancel_once:
            self.cancel_once = True
            raise event_budget.EventBudgetExceeded(
                "typed cancellation in sibling repository")
        db("BUSINESS WRITE", (repo,))
        return {"repo": repo}


class Harness:
    def __init__(self, state: State, server: FakeServer):
        self.state = state
        self.server = server
        self.originals = {}

    def __enter__(self):
        for name in (
            "_server",
            "_connect_event_db",
            "_take_live_repository_locks",
            "_release_repo_lock",
            "_refresh_graph_stats_if_bulk_loaded",
        ):
            self.originals[name] = getattr(EP, name)
        EP._server = lambda: self.server
        EP._connect_event_db = (
            lambda _dsn, _timeout: self.state.connect())
        EP._take_live_repository_locks = (
            lambda *_args, **_kwargs: True)
        EP._release_repo_lock = lambda *_args, **_kwargs: True
        EP._refresh_graph_stats_if_bulk_loaded = (
            lambda *_args, **_kwargs: False)
        return self

    def __exit__(self, *_exc):
        for name, value in self.originals.items():
            setattr(EP, name, value)


def plan(count: int):
    return EP._canonical_fanout_proposal([
        {
            "key": f"id:{index}",
            "full_name": f"acme/repo-{index:03d}",
            "id": str(index),
        }
        for index in range(1, count + 1)
    ])


def payload(entries):
    return {
        "action": "created",
        "_veripsa_delivery_key": "delivery",
        "installation": {
            "id": 900,
            "account": {
                "id": "acct",
                "login": "acme",
                "type": "Organization",
            },
        },
        "repositories": [
            {"id": entry["id"], "full_name": entry["full_name"]}
            for entry in entries
        ],
    }


def removal_payload(entries):
    return {
        "action": "removed",
        "_veripsa_delivery_key": "delivery",
        "installation": {
            "id": 900,
            "account": {
                "id": "acct",
                "login": "acme",
                "type": "Organization",
            },
        },
        "repositories_removed": [
            {"id": entry["id"], "full_name": entry["full_name"]}
            for entry in entries
        ],
    }


def run_fanout(state, server, authority, *, completed=None):
    return EP._process_install_event_per_repo_locked(
        "postgresql://unused",
        "installation",
        payload(state.plan),
        "acct",
        object(),
        state.plan,
        execution_authority=authority,
        completed_keys=set(state.completed if completed is None else completed),
    )


def test_partial_cancellation_and_retry_skip() -> None:
    entries = plan(2)
    state = State(entries)
    server = FakeServer()
    server.cancel_repo = entries[1]["full_name"]
    with Harness(state, server):
        first_authority = DQ._DeliveryExecutionAuthority("delivery", 1)
        first = run_fanout(state, server, first_authority, completed=set())
        check(
            type(first) is DQ._DeliveryAtomicDeferralResult
            and state.completed == {entries[0]["key"]}
            and state.business == [entries[0]["full_name"]]
            and state.status == "queued"
            and state.attempts == 0,
            "A commits, B typed-cancels, and exact partial progress defers attempt-neutrally",
        )

        state.claim_next()
        second_authority = DQ._DeliveryExecutionAuthority(
            "delivery", state.generation)
        second = run_fanout(state, server, second_authority)
        check(
            second is second_authority
            and state.status == "done"
            and state.business == [
                entries[0]["full_name"], entries[1]["full_name"],
            ]
            and server.calls.count(entries[0]["full_name"]) == 1,
            "retry restores the immutable checkpoint, skips A, and atomically finishes B",
        )


def test_fifty_repositories_slice_without_attempt_growth() -> None:
    entries = plan(50)
    state = State(entries)
    server = FakeServer()
    slices = 0
    with Harness(state, server):
        while state.status != "done":
            authority = DQ._DeliveryExecutionAuthority(
                "delivery", state.generation)
            result = run_fanout(state, server, authority)
            if type(result) is DQ._DeliveryAtomicDeferralResult:
                slices += 1
                check(
                    state.status == "queued" and state.attempts == 0,
                    f"slice {slices} restores the claimed attempt before recovery",
                )
                state.claim_next()
            else:
                check(
                    result is authority and state.status == "done",
                    "last bounded slice finishes exactly once",
                )
        check(
            slices >= 2
            and len(state.business) == 50
            and len(set(state.business)) == 50
            and state.max_attempts_seen == 1
            and state.attempts == 1,
            "50 repositories drain over bounded slices without replay, DLQ, or cumulative attempts",
        )


def test_authority_loss_rolls_business_back_and_direct_compatibility() -> None:
    entries = plan(1)
    lost = State(entries)
    lost.force_complete_lost = True
    lost_server = FakeServer()
    with Harness(lost, lost_server):
        caught = None
        try:
            run_fanout(
                lost, lost_server,
                DQ._DeliveryExecutionAuthority("delivery", 1),
                completed=set(),
            )
        except RuntimeError as error:
            caught = error
        check(
            caught is not None
            and lost.business == []
            and lost.completed == set()
            and lost.status == "processing",
            "updated=false is fail-closed and rolls repository business writes back",
        )

    stale = State(entries, generation=2)
    stale_server = FakeServer()
    with Harness(stale, stale_server):
        caught = None
        try:
            run_fanout(
                stale, stale_server,
                DQ._DeliveryExecutionAuthority("delivery", 1),
                completed=set(),
            )
        except RuntimeError as error:
            caught = error
        check(
            caught is not None
            and stale.business == []
            and stale_server.calls == [],
            "prepare NULL/stale generation cannot reach a handler or checkpoint",
        )

    direct = State(entries)
    direct_server = FakeServer()
    with Harness(direct, direct_server):
        result = EP._process_install_event_per_repo_locked(
            "postgresql://unused",
            "installation",
            payload(entries),
            "acct",
            object(),
            entries,
        )
        check(
            isinstance(result, dict)
            and direct.business == [entries[0]["full_name"]]
            and direct.complete_calls == []
            and direct.finish_calls == 0
            and direct.defer_calls == 0,
            "authorityless/direct fanout keeps its legacy commit path without durable checkpointing",
        )


def test_fanout_metadata_stays_outside_repository_bodies() -> None:
    entries = plan(1)

    activation = State(entries)
    with Harness(activation, FakeServer()):
        run_fanout(
            activation,
            FakeServer(),
            DQ._DeliveryExecutionAuthority("delivery", 1),
            completed=set(),
        )
    check(
        activation.metadata_autocommit_states == [True],
        "activation fanout records shared account metadata in autocommit scope",
    )

    removal = State(entries)
    removal_server = FakeServer()
    with Harness(removal, removal_server):
        result = EP._process_install_event_per_repo_locked(
            "postgresql://unused",
            "installation_repositories",
            removal_payload(entries),
            "acct",
            object(),
            entries,
            generation_proof=removal_server._activation_installation_proof(
                None, None),
        )
    check(
        isinstance(result, dict)
        and removal.metadata_autocommit_states == [True]
        and removal.installation_admission_calls == [
            ("900", "2026-07-28T00:00:00+00:00", True),
            ("900", None, False),
        ]
        and removal.business == [entries[0]["full_name"]],
        "removal fanout advances generation in autocommit, rechecks it in-body, and keeps metadata outside",
    )


def test_single_slow_repository_has_its_own_deadline() -> None:
    entries = plan(1)
    state = State(entries)

    class SlowServer(FakeServer):
        def __init__(self):
            super().__init__()
            self.observed_budget = None

        def handle_event(self, event_type, body, db, _gh, coalesce=None):
            field = EP._INSTALL_FANOUT_FIELD[(event_type, body["action"])]
            repo = body[field][0]["full_name"]
            self.calls.append(repo)
            db("BUSINESS WRITE", (repo,))
            self.observed_budget = event_budget.remaining()
            wait = event_budget.timeout_for(5.0)
            time.sleep(wait + 0.015)
            event_budget.raise_if_expired()

    server = SlowServer()
    original_repo_seconds = EP._FANOUT_REPO_WORK_SECONDS
    EP._FANOUT_REPO_WORK_SECONDS = 0.05
    caught = None
    started = time.monotonic()
    try:
        with Harness(state, server):
            try:
                run_fanout(
                    state,
                    server,
                    DQ._DeliveryExecutionAuthority("delivery", 1),
                    completed=set(),
                )
            except event_budget.EventBudgetExceeded as error:
                caught = error
    finally:
        EP._FANOUT_REPO_WORK_SECONDS = original_repo_seconds
    elapsed = time.monotonic() - started
    check(
        caught is not None
        and server.observed_budget is not None
        and server.observed_budget <= 0.055
        and 0.045 <= elapsed < 0.3
        and state.business == []
        and state.completed == set()
        and state.status == "processing",
        "one slow repository cancels near its own bound and rolls every staged write back",
    )


def test_failed_repositories_share_one_slice_wall() -> None:
    entries = plan(4)
    state = State(entries)

    class SlowFailureServer(FakeServer):
        def handle_event(self, event_type, body, _db, _gh, coalesce=None):
            field = EP._INSTALL_FANOUT_FIELD[(event_type, body["action"])]
            self.calls.append(body[field][0]["full_name"])
            time.sleep(0.07)
            raise RuntimeError("ordinary slow repository failure")

    server = SlowFailureServer()
    original_repo_seconds = EP._FANOUT_REPO_WORK_SECONDS
    original_slice_seconds = EP._FANOUT_SLICE_SECONDS
    original_min_next = EP._FANOUT_MIN_NEXT_REPO_SECONDS
    EP._FANOUT_REPO_WORK_SECONDS = 0.08
    EP._FANOUT_SLICE_SECONDS = 0.08
    EP._FANOUT_MIN_NEXT_REPO_SECONDS = 0.02
    caught = None
    started = time.monotonic()
    try:
        with Harness(state, server):
            try:
                run_fanout(
                    state,
                    server,
                    DQ._DeliveryExecutionAuthority("delivery", 1),
                    completed=set(),
                )
            except RuntimeError as error:
                caught = error
    finally:
        EP._FANOUT_REPO_WORK_SECONDS = original_repo_seconds
        EP._FANOUT_SLICE_SECONDS = original_slice_seconds
        EP._FANOUT_MIN_NEXT_REPO_SECONDS = original_min_next
    elapsed = time.monotonic() - started
    check(
        caught is not None
        and len(server.calls) == 1
        and elapsed < 0.16
        and state.business == []
        and state.completed == set()
        and state.status == "processing",
        "ordinary failures consume one shared slice wall instead of four repo deadlines",
    )


class FakeStore(DQ.DeliveryStore):
    def __init__(self):
        super().__init__("postgresql://unused")
        self.resolve_calls = []
        self.defer_resolve_calls = []
        self.finish_calls = []
        self.release_calls = []

    def resolve_commit(self, key, error, lease_generation):
        self.resolve_calls.append((key, error, lease_generation))
        return "committed"

    def finish(self, key, lease_generation):
        self.finish_calls.append((key, lease_generation))
        return True

    def resolve_fanout_defer(
            self, key, lease_generation, not_before, reason):
        self.defer_resolve_calls.append(
            (key, lease_generation, not_before, reason))
        return "deferred"

    def release(self, key, error, lease_generation):
        self.release_calls.append((key, error, lease_generation))
        return "queued"


def test_last_repository_ack_loss_is_one_logical_run() -> None:
    entries = plan(1)
    state = State(entries)
    state.ack_loss_on_done = True
    server = FakeServer()
    store = FakeStore()

    def processor(event_type, body, _db, gh, coalesce=None):
        authority = body.pop(DQ._DELIVERY_EXECUTION_AUTHORITY)
        return EP._process_install_event_per_repo_locked(
            "postgresql://unused",
            event_type,
            body,
            "acct",
            gh,
            entries,
            execution_authority=authority,
            completed_keys=set(),
        )

    processor._veripsa_atomic_delivery_finalize_protocol = (
        DQ._DELIVERY_ATOMIC_FINALIZE_PROTOCOL)
    wrapped = store.wrap_processor(processor)
    body = payload(entries)
    body[DQ._DELIVERY_PRECLAIMED] = True
    body[DQ._DELIVERY_LEASE_GENERATION] = 1
    with Harness(state, server):
        result = wrapped("installation", body, None, object())
    check(
        result is None
        and state.status == "done"
        and state.business == [entries[0]["full_name"]]
        and len(store.resolve_calls) == 1
        and store.resolve_calls[0][1] is state.ack_error
        and store.finish_calls == []
        and store.release_calls == [],
        "last repo business+checkpoint+done ACK loss resolves committed and handler runs once",
    )


def test_slice_deferral_ack_loss_resolves_attempt_neutrally() -> None:
    entries = plan(5)
    state = State(entries)
    state.ack_loss_on_queue = True
    state.ack_error = RuntimeError("simulated slice COMMIT ACK loss")
    server = FakeServer()
    store = FakeStore()

    def processor(event_type, body, _db, gh, coalesce=None):
        authority = body.pop(DQ._DELIVERY_EXECUTION_AUTHORITY)
        return EP._process_install_event_per_repo_locked(
            "postgresql://unused",
            event_type,
            body,
            "acct",
            gh,
            entries,
            execution_authority=authority,
            completed_keys=set(),
        )

    processor._veripsa_atomic_delivery_finalize_protocol = (
        DQ._DELIVERY_ATOMIC_FINALIZE_PROTOCOL)
    wrapped = store.wrap_processor(processor)
    body = payload(entries)
    body[DQ._DELIVERY_PRECLAIMED] = True
    body[DQ._DELIVERY_LEASE_GENERATION] = 1
    with Harness(state, server):
        result = wrapped("installation", body, None, object())
    check(
        isinstance(result, dict)
        and result.get(DQ._WORKER_CLAIM_OUTCOME) == "deferred"
        and state.status == "queued"
        and state.attempts == 0
        and len(state.completed) == EP._FANOUT_REPOS_PER_SLICE
        and len(state.business) == EP._FANOUT_REPOS_PER_SLICE
        and len(store.defer_resolve_calls) == 1
        and store.defer_resolve_calls[0][0:2] == ("delivery", 1)
        and store.finish_calls == []
        and store.release_calls == [],
        "slice COMMIT ACK loss reuses exact timestamp/reason and remains attempt-neutral",
    )


class Inventory:
    def __init__(self):
        self.calls = 0

    def installation_repo_entries(self, cap):
        self.calls += 1
        return [
            {"id": 22, "full_name": "acme/zeta"},
            {"id": 11, "full_name": "acme/alpha"},
        ]


def test_retry_restores_plan_without_inventory() -> None:
    server = FakeServer()
    inventory = Inventory()
    state = State(plan(1))
    prepared = []
    original_prepare = EP._prepare_durable_fanout
    original_process = EP._process_install_event_per_repo_locked
    try:
        def prepare(_dsn, _timeout, _authority, proposal):
            canonical = EP._canonical_fanout_proposal(proposal)
            prepared.append(canonical)
            completed = set()
            if len(prepared) == 2:
                completed.add("id:11")
            return canonical, completed

        EP._prepare_durable_fanout = prepare
        EP._process_install_event_per_repo_locked = (
            lambda *_args, execution_authority=None, **_kwargs:
            execution_authority)
        with Harness(state, server):
            processor = EP.make_db_processor("postgresql://unused")
            first = payload([])
            first["repository_selection"] = "all"
            first[DQ._DELIVERY_EXECUTION_AUTHORITY] = (
                DQ._DeliveryExecutionAuthority("delivery", 1))
            first_result = processor(
                "installation", first, None, inventory)

            frozen = [
                {
                    "key": "id:11",
                    "full_name": "acme/alpha",
                    "id": "11",
                },
                {
                    "key": "id:22",
                    "full_name": "acme/zeta",
                    "id": "22",
                },
            ]
            retry = payload([])
            retry["repository_selection"] = "all"
            retry[EP._FANOUT_PLAN_FIELD] = frozen
            retry[EP._FANOUT_COMPLETED_FIELD] = {"id:11": True}
            retry[DQ._DELIVERY_EXECUTION_AUTHORITY] = (
                DQ._DeliveryExecutionAuthority("delivery", 2))
            retry_result = processor(
                "installation", retry, None, inventory)
        check(
            type(first_result) is DQ._DeliveryExecutionAuthority
            and type(retry_result) is DQ._DeliveryExecutionAuthority
            and inventory.calls == 1
            and prepared == [frozen, frozen],
            "all-repositories retry uses the frozen id-first plan and never inventories GitHub twice",
        )
    finally:
        EP._process_install_event_per_repo_locked = original_process
        EP._prepare_durable_fanout = original_prepare


def test_other_account_starts_while_fanout_lane_is_slow() -> None:
    fanout_started = threading.Event()
    release_fanout = threading.Event()
    other_started = threading.Event()
    sibling_started = threading.Event()

    def process(_event_type, body, _db, _gh):
        if body["account"] == "large":
            if body["item"] == "fanout-slice":
                fanout_started.set()
                release_fanout.wait(2.0)
            else:
                sibling_started.set()
        else:
            other_started.set()

    worker = EventQueue(
        None,
        None,
        process,
        account_of=lambda body: body["account"],
        worker_count=2,
        retry_attempts=1,
    ).start()
    worker.submit(
        "installation",
        {"account": "large", "item": "fanout-slice"},
        "large-slice",
    )
    check(fanout_started.wait(0.5), "large fanout slice occupies one keyed worker")
    worker.submit(
        "push",
        {
            "account": "large",
            "item": "same-account-sibling",
            "repository": {"id": 91},
        },
        "large-sibling",
    )
    started = time.monotonic()
    worker.submit(
        "push",
        {"account": "other", "item": "small"},
        "other-account",
    )
    check(
        other_started.wait(0.8) and time.monotonic() - started < 1.0,
        "another account starts immediately while the fanout lane is slow",
    )
    check(
        not sibling_started.wait(0.08),
        "same-account sibling cannot overtake the lifecycle fanout barrier",
    )
    release_fanout.set()
    check(
        sibling_started.wait(0.5) and worker.wait_idle(2.0),
        "same-account sibling resumes only after the fanout barrier yields",
    )


def main() -> int:
    print("=== FANOUT RUNTIME CHECKPOINT GATE ===")
    test_partial_cancellation_and_retry_skip()
    test_fifty_repositories_slice_without_attempt_growth()
    test_authority_loss_rolls_business_back_and_direct_compatibility()
    test_fanout_metadata_stays_outside_repository_bodies()
    test_single_slow_repository_has_its_own_deadline()
    test_failed_repositories_share_one_slice_wall()
    test_last_repository_ack_loss_is_one_logical_run()
    test_slice_deferral_ack_loss_resolves_attempt_neutrally()
    test_retry_restores_plan_without_inventory()
    test_other_account_starts_while_fanout_lane_is_slow()
    print(
        "FANOUT RUNTIME CHECKPOINT GATE:",
        "PASS" if FAIL == 0 else "FAIL",
    )
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
