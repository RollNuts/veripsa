#!/usr/bin/env python3
"""Fixed-small keyed worker pool: cross-account latency without same-account races."""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import delivery_queue  # noqa: E402
import event_budget  # noqa: E402
import health_watchdog  # noqa: E402
import ingest  # noqa: E402
import server_boot  # noqa: E402
from event_queue import EventQueue, _FairQueue, _repository_lane  # noqa: E402


FAIL = 0


def check(condition, message):
    global FAIL
    print(("  [PASS] " if condition else "  [FAIL] ") + message)
    if not condition:
        FAIL = 1


def account_of(payload):
    return str(payload.get("account") or "")


def test_cross_account_progress_and_account_fifo() -> None:
    a_started = threading.Event()
    release_a = threading.Event()
    b_started = threading.Event()
    lock = threading.Lock()
    active = {}
    overlap = []
    order = []
    starts = {}

    def process(_event_type, payload, _db, _gh):
        account = payload["account"]
        item = payload["item"]
        with lock:
            active[account] = active.get(account, 0) + 1
            if active[account] > 1:
                overlap.append((account, item))
            order.append(item)
            starts[item] = time.monotonic()
        try:
            if item == "A1":
                a_started.set()
                release_a.wait(2.0)
            elif item == "B1":
                b_started.set()
        finally:
            with lock:
                active[account] -= 1

    worker = EventQueue(
        None, None, process,
        account_of=account_of,
        worker_count=2,
        retry_attempts=1,
    ).start()
    check(worker.submit("push", {"account": "A", "item": "A1"}, "pool-a1"), "A1 accepted")
    check(a_started.wait(0.5), "A1 entered the graph-like slow stage")
    submitted_at = time.monotonic()
    check(worker.submit("push", {"account": "A", "item": "A2"}, "pool-a2"), "A2 accepted behind its lane")
    check(worker.submit("push", {"account": "B", "item": "B1"}, "pool-b1"), "B1 accepted on another lane")
    b_fast = b_started.wait(0.8)
    check(
        b_fast and starts.get("B1", submitted_at + 99) - submitted_at < 0.8,
        "tenant B starts in <1s while tenant A is blocked",
    )
    time.sleep(0.05)
    check("A2" not in order, "same-account A2 cannot overtake or overlap blocked A1")
    release_a.set()
    check(worker.wait_idle(2.0), "pool drains after A1 is released")
    check(not overlap and order.index("A1") < order.index("A2"), "same account is strict FIFO and non-overlapping")
    check(worker.processed() == 3 and worker.inflight_count() == 0, "pool counters and inflight registry are exact")


def test_pool_health_detects_partial_worker_death() -> None:
    died = threading.Event()

    def fatal(_event_type, _payload, _db, _gh):
        died.set()
        raise SystemExit("synthetic worker death")

    worker = EventQueue(
        None, None, fatal,
        account_of=account_of,
        worker_count=2,
        retry_attempts=1,
    ).start()
    check(worker.is_alive() and worker.alive_workers() == 2, "both configured workers start alive")
    worker.submit("push", {"account": "fatal", "item": "fatal"}, "pool-fatal")
    check(died.wait(0.5), "fatal test event reached one worker")
    deadline = time.monotonic() + 1.0
    while worker.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    snapshot = health_watchdog.health_snapshot(worker)
    check(
        not worker.is_alive() and worker.alive_workers() == 1,
        "loss of any configured pool member makes aggregate liveness false",
    )
    check(
        snapshot["healthy"] is False
        and snapshot["worker_count"] == 2
        and snapshot["alive_workers"] == 1,
        "health exposes configured/alive pool cardinality and fails closed",
    )


def test_repository_lanes_and_account_wide_barrier() -> None:
    repo1_started = threading.Event()
    repo2_started = threading.Event()
    release_repo1 = threading.Event()
    wide_started = threading.Event()
    release_wide = threading.Event()
    after_wide_started = threading.Event()

    def process(event_type, payload, _db, _gh):
        item = payload["item"]
        if item == "R1":
            repo1_started.set()
            release_repo1.wait(2.0)
        elif item == "R2":
            repo2_started.set()
        elif item == "W":
            wide_started.set()
            release_wide.wait(2.0)
        elif item == "AFTER":
            after_wide_started.set()

    worker = EventQueue(
        None, None, process,
        account_of=account_of,
        worker_count=3,
        per_account_workers=2,
        retry_attempts=1,
    ).start()
    repo1 = {"account": "same", "item": "R1", "repository": {"id": 101}}
    repo2 = {"account": "same", "item": "R2", "repository": {"id": 202}}
    worker.submit("push", repo1, "repo-r1")
    check(repo1_started.wait(0.5), "same-account repo 1 starts")
    started_at = time.monotonic()
    worker.submit("pull_request", repo2, "repo-r2")
    check(
        repo2_started.wait(0.8) and time.monotonic() - started_at < 1.0,
        "same-account different repository starts concurrently in <1s",
    )
    release_repo1.set()
    check(worker.wait_idle(2.0), "different-repository phase drains")

    # Account-wide lifecycle work is a writer barrier: it waits for prior repo work and blocks every later repo.
    repo1_started.clear()
    release_repo1.clear()
    worker.submit("push", repo1, "barrier-r1")
    check(repo1_started.wait(0.5), "pre-barrier repository work starts")
    worker.submit("installation", {"account": "same", "item": "W"}, "barrier-wide")
    worker.submit(
        "push",
        {"account": "same", "item": "AFTER", "repository": {"id": 202}},
        "barrier-after",
    )
    time.sleep(0.08)
    check(not wide_started.is_set() and not after_wide_started.is_set(), "wide waits for earlier repo; later repo waits")
    release_repo1.set()
    check(wide_started.wait(0.5), "account-wide event starts after prior repository completes")
    time.sleep(0.05)
    check(not after_wide_started.is_set(), "account-wide event exclusively blocks later repositories")
    release_wide.set()
    check(after_wide_started.wait(0.5) and worker.wait_idle(2.0), "later repository resumes after wide barrier")

    # GitHub's stable repository id survives rename and account transfer. A changed full_name/account bucket must
    # not create a second live lane for the same underlying repository.
    transfer_old_started = threading.Event()
    transfer_new_started = threading.Event()
    release_transfer = threading.Event()

    def transfer_process(_event_type, payload, _db, _gh):
        if payload["item"] == "old":
            transfer_old_started.set()
            release_transfer.wait(2.0)
        else:
            transfer_new_started.set()

    transfer = EventQueue(
        None, None, transfer_process,
        account_of=account_of,
        worker_count=2,
        per_account_workers=1,
        retry_attempts=1,
    ).start()
    transfer.submit(
        "repository",
        {"account": "old-owner", "item": "old", "repository": {"id": 909, "full_name": "old/name"}},
        "transfer-old",
    )
    check(transfer_old_started.wait(0.5), "pre-transfer stable repository lane starts")
    transfer.submit(
        "repository",
        {"account": "new-owner", "item": "new", "repository": {"id": 909, "full_name": "new/name"}},
        "transfer-new",
    )
    check(not transfer_new_started.wait(0.2), "rename/account transfer cannot overlap the same stable repository id")
    release_transfer.set()
    check(
        transfer_new_started.wait(0.5) and transfer.wait_idle(2.0),
        "transferred repository resumes FIFO under its unchanged stable id",
    )


def test_noisy_account_cannot_occupy_the_whole_pool() -> None:
    a1_started = threading.Event()
    a2_started = threading.Event()
    c_started = threading.Event()
    release = threading.Event()

    def process(_event_type, payload, _db, _gh):
        item = payload["item"]
        if item == "A1":
            a1_started.set()
            release.wait(2.0)
        elif item == "A2":
            a2_started.set()
            release.wait(2.0)
        elif item == "C":
            c_started.set()

    worker = EventQueue(
        None, None, process,
        account_of=account_of,
        worker_count=3,
        per_account_workers=2,
        retry_attempts=1,
    ).start()
    worker.submit("push", {"account": "A", "item": "A1", "repository": {"id": 1}}, "cap-a1")
    worker.submit("push", {"account": "A", "item": "A2", "repository": {"id": 2}}, "cap-a2")
    check(a1_started.wait(0.5) and a2_started.wait(0.5), "noisy account uses at most its two allowed lanes")
    started_at = time.monotonic()
    worker.submit("push", {"account": "C", "item": "C", "repository": {"id": 3}}, "cap-c")
    check(
        c_started.wait(0.8) and time.monotonic() - started_at < 1.0,
        "another account starts in <1s while two noisy-account repositories are slow",
    )
    release.set()
    check(worker.wait_idle(2.0), "per-account active-cap scenario drains")


def test_recovery_reservations_follow_repository_barriers() -> None:
    worker = EventQueue(None, None, lambda *_args: None, account_of=account_of, worker_count=2)
    repo1 = {"account": "same", "repository": {"id": 101}}
    repo2 = {"account": "same", "repository": {"id": 202}}
    transferred_repo1 = {"account": "new-owner", "repository": {"id": 101, "full_name": "new/name"}}
    wide = {"account": "same"}
    check(worker.reserve_recovery("push", repo1, "recover-r1"), "recovery reserves repo 1")
    check(worker.reserve_recovery("push", repo2, "recover-r2"), "recovery independently reserves repo 2")
    check(not worker.reserve_recovery("push", repo1, "recover-r1b"), "recovery serializes the same repo")
    check(
        not worker.reserve_recovery("repository", transferred_repo1, "recover-r1-transfer"),
        "recovery serializes a stable repository id across account transfer",
    )
    check(not worker.reserve_recovery("installation", wide, "recover-wide"), "wide recovery waits for repo lanes")
    worker.cancel_recovery_reservation(repo1, "recover-r1")
    worker.cancel_recovery_reservation(repo2, "recover-r2")
    check(worker.reserve_recovery("installation", wide, "recover-wide"), "wide recovery reserves exclusively")
    check(not worker.reserve_recovery("push", repo1, "recover-r1c"), "wide recovery blocks repository recovery")
    worker.cancel_recovery_reservation(wide, "recover-wide")


def test_default_one_and_backpressure_compatibility() -> None:
    worker = EventQueue(
        None, None, lambda *_args, **_kwargs: None,
        account_of=account_of,
        maxsize=4,
        per_account_cap=1,
    )
    check(worker.worker_count() == 1, "direct EventQueue construction remains one-worker compatible")
    check(worker.submit("push", {"account": "A", "item": "A1"}, "cap-a1"), "first A item fits")
    check(not worker.submit("push", {"account": "A", "item": "A2"}, "cap-a2"), "per-account backpressure remains")
    check(worker.submit("push", {"account": "B", "item": "B1"}, "cap-b1"), "another account keeps reserved room")
    check(worker.qsize() == 2, "global queue accounting remains exact")
    check(
        _repository_lane(("push", {"repository": {"id": 42}}, None)) == "42"
        and _repository_lane(("push", {"repository": {}}, None)) is None
        and _repository_lane(("push", {"repository": {"id": "not-a-number"}}, None)) is None
        and _repository_lane(("push", {"repository": {"id": "042"}}, None)) is None
        and _repository_lane(("push", {"repository": {"id": " 42 "}}, None)) is None
        and _repository_lane(("installation", {"repository": {"id": 42}}, None)) is None
        and _repository_lane(("unknown_event", {}, None)) is None,
        "positive numeric repository.id is the only repo lane; invalid/missing/unknown/lifecycle shapes are wide",
    )
    source = inspect.getsource(server_boot.wire_runtime)
    check(
        'env_int("VERIPSA_EVENT_WORKER_COUNT", 3, min_value=1, max_value=4)' in source
        and '"VERIPSA_EVENT_PER_ACCOUNT_WORKERS", 2, min_value=1, max_value=3' in source
        and '"worker_count"' in source and '"per_account_workers"' in source,
        "production validates pool default=3 and per-account active cap default=2",
    )


def test_delivery_shutdown_registry_is_pool_safe() -> None:
    store = delivery_queue.DeliveryStore("postgresql://unused")
    calls = []
    store._one = lambda sql, args=(): calls.append((sql, args)) or True
    registered = threading.Barrier(3)
    clear = threading.Event()

    def own(key, generation):
        store._set_inflight(key, generation)
        registered.wait()
        clear.wait(1.0)
        store._clear_inflight(key)

    threads = [
        threading.Thread(target=own, args=("lease-a", 1)),
        threading.Thread(target=own, args=("lease-b", 2)),
    ]
    for thread in threads:
        thread.start()
    registered.wait()
    view = store._inflight
    expired = store.expire_inflight_lease(grace_seconds=1)
    clear.set()
    for thread in threads:
        thread.join(1.0)
    expired_keys = {args[0] for sql, args in calls if "expire_webhook_delivery_lease" in sql}
    check(isinstance(view, tuple) and len(view) == 2, "shutdown registry retains both concurrent durable owners")
    check(expired and expired_keys == {"lease-a", "lease-b"}, "shutdown expires every exact in-flight lease")
    check(store._inflight is None, "each worker clears only its own shutdown registry entry")


def test_repository_fifo_index_is_bounded_and_not_rescanned() -> None:
    classifications = 0

    def lane_of(item):
        nonlocal classifications
        classifications += 1
        return item[1]["repository"]["id"]

    backlog = _FairQueue(
        maxsize=400,
        per_account_cap=2,
        account_of=lambda payload: payload["account"],
        lane_of=lane_of,
        max_active_per_account=2,
    )
    for index in range(300):
        backlog.put_nowait((
            "push",
            {"account": f"acct-{index}", "repository": {"id": f"{index + 1}"}},
            f"delivery-{index}",
        ))
    check(classifications == 300, "repository lanes are classified exactly once at admission")
    for _index in range(300):
        backlog.get(activate=True)
        backlog.task_done()
    check(
        classifications == 300 and not backlog._queued_repository_sequences,
        "global repository FIFO uses a bounded sequence index and releases every entry",
    )


def test_graph_slot_contention_defers_quickly() -> None:
    acquired = ingest._GRAPH_EXTRACT_CHILD_LOCK.acquire(timeout=1.0)
    token = event_budget.begin(10.0)
    started = time.monotonic()
    deferred = None
    try:
        with tempfile.TemporaryDirectory(prefix="veripsa_pool_graph_") as workspace:
            try:
                ingest._run_isolated_extractor(workspace, mode="incremental", source_root=workspace)
            except delivery_queue.IntentionalDeliveryDeferral as exc:
                deferred = exc
    finally:
        event_budget.end(token)
        if acquired:
            ingest._GRAPH_EXTRACT_CHILD_LOCK.release()
    elapsed = time.monotonic() - started
    check(acquired, "contention test owns the singleton graph child slot")
    check(deferred is not None and elapsed < 0.5, f"event graph contention yields with typed defer in {elapsed:.3f}s")


def test_graph_slot_preserves_fifo_account_turns() -> None:
    current = {"account": "A"}
    now = {"value": 100.0}
    slot = ingest._TenantFairGraphSlot(
        clock=lambda: now["value"],
        turn_seconds=30.0,
        account_provider=lambda: ("event", current["account"]),
    )

    def acquire(account: str) -> bool:
        current["account"] = account
        return slot.acquire(timeout=0)

    check(acquire("A"), "account A acquires an idle graph slot")
    check(
        not acquire("A"),
        "a same-account burst does not mint a second FIFO ticket while A is active",
    )
    check(
        not acquire("B") and not acquire("C"),
        "distinct contending accounts retain one quick-defer ticket each",
    )
    slot.release()
    check(
        not acquire("A"),
        "the releasing account cannot jump ahead of the reserved B turn",
    )
    order = []
    if acquire("B"):
        order.append("B")
        slot.release()
    if acquire("C"):
        order.append("C")
        slot.release()
    if acquire("A"):
        order.append("A")
        slot.release()
    check(
        order == ["B", "C", "A"],
        f"unique-account graph turns stay FIFO under an A burst (order={order})",
    )

    # A vanished/deleted deferred delivery must not reserve the singleton
    # forever. TTL starts only once the active owner releases.
    current["account"] = "A"
    check(slot.acquire(timeout=0), "TTL fixture acquires A")
    check(not acquire("B"), "TTL fixture records B's deferred turn")
    now["value"] += 300.0
    check(
        not acquire("A"),
        "an active extraction does not age out B's future turn",
    )
    slot.release()
    now["value"] += 31.0
    check(
        acquire("A"),
        "a vanished B turn expires 30s after release and cannot strand graph ingestion",
    )
    slot.release()

    # Many accounts can quick-defer while one 85s extraction is active. If
    # none comes back, they share one post-release expiry; charging 30s each
    # would let 27 phantoms recreate a >787s idle stall.
    current["account"] = "A"
    check(slot.acquire(timeout=0), "multi-phantom fixture acquires A")
    for phantom in ("B", "C", "D", "E"):
        check(
            not acquire(phantom),
            f"multi-phantom fixture records {phantom}",
        )
    slot.release()
    now["value"] += 31.0
    check(
        acquire("A"),
        "one expired head purges all equally-vanished turns without N×30s idle time",
    )
    slot.release()

    # Child cleanup can transfer lease ownership to a daemon. Cross-thread
    # release must still advance the same account turn.
    check(acquire("A"), "reaper fixture acquires A")
    check(not acquire("B"), "reaper fixture records B")
    reaper = threading.Thread(target=slot.release)
    reaper.start()
    reaper.join(0.5)
    check(
        not reaper.is_alive() and acquire("B"),
        "cross-thread child reaper release preserves B's reserved turn",
    )
    slot.release()

    # Background reconcile has no durable 5-second comeback path. A timed-out
    # background probe must remove its own waiter instead of creating a
    # phantom turn that delays live accounts.
    actor = {"value": ("event", "A")}
    mixed_slot = ingest._TenantFairGraphSlot(
        account_provider=lambda: actor["value"],
    )
    check(mixed_slot.acquire(timeout=0), "mixed fixture acquires event A")
    actor["value"] = ("background", "")
    check(
        not mixed_slot.acquire(timeout=0),
        "timed-out background contention yields without retaining a phantom turn",
    )
    actor["value"] = ("event", "B")
    check(not mixed_slot.acquire(timeout=0), "mixed fixture records live B")
    mixed_slot.release()
    check(
        mixed_slot.acquire(timeout=0),
        "live B is next because the abandoned background waiter was removed",
    )
    mixed_slot.release()


def test_graph_slot_fairness_integrates_with_keyed_pool() -> None:
    """Sustained A graph work cannot starve B; non-graph C stays live."""
    slot = ingest._TenantFairGraphSlot(turn_seconds=30.0)
    release_a1 = threading.Event()
    a1_started = threading.Event()
    c_done = threading.Event()
    attempts = {
        name: threading.Event()
        for name in ("A2", "B1", "A3")
    }
    completions = {
        name: threading.Event()
        for name in ("B2", "A4")
    }
    acquired_order = []
    observed_accounts = {}
    state_lock = threading.Lock()

    def process(_event_type, payload, _db, _gh):
        item = payload["item"]
        account = payload["account"]
        with state_lock:
            observed_accounts[item] = event_budget.current_account()
        if not payload.get("graph"):
            c_done.set()
            return {"ok": True}
        acquired = slot.acquire(timeout=0.02)
        if not acquired:
            if item in attempts:
                attempts[item].set()
            return {
                "_veripsa_worker_claim_outcome": "deferred",
                "claim_reason": "graph extractor busy",
            }
        try:
            with state_lock:
                acquired_order.append(account)
            if item == "A1":
                a1_started.set()
                release_a1.wait(1.5)
            if item in completions:
                completions[item].set()
            return {"ok": True}
        finally:
            slot.release()

    worker = EventQueue(
        None,
        None,
        process,
        account_of=account_of,
        worker_count=3,
        per_account_workers=2,
        retry_attempts=1,
    ).start()
    worker.submit(
        "push",
        {"account": "A", "item": "A1", "graph": True,
         "repository": {"id": 1001}},
        "graph-fair-a1",
    )
    check(a1_started.wait(0.5), "A1 owns the singleton graph slot")
    worker.submit(
        "push",
        {"account": "A", "item": "A2", "graph": True,
         "repository": {"id": 1002}},
        "graph-fair-a2",
    )
    worker.submit(
        "push",
        {"account": "B", "item": "B1", "graph": True,
         "repository": {"id": 2001}},
        "graph-fair-b1",
    )
    check(
        attempts["A2"].wait(0.5) and attempts["B1"].wait(0.5),
        "A burst and B both yield quickly while A1 holds the memory slot",
    )
    worker.submit(
        "check_run",
        {"account": "C", "item": "C1", "graph": False,
         "repository": {"id": 3001}},
        "graph-fair-c1",
    )
    check(
        c_done.wait(0.5) and not release_a1.is_set(),
        "non-graph account C completes while A1 still owns graph memory",
    )
    release_a1.set()
    check(worker.wait_idle(1.0), "first graph contention generation drains")

    # Even though A recovery is submitted first, B owns the durable next turn.
    worker.submit(
        "push",
        {"account": "A", "item": "A3", "graph": True,
         "repository": {"id": 1003}},
        "graph-fair-a3",
    )
    check(
        attempts["A3"].wait(0.5),
        "a fresh A graph job cannot steal B's reserved recovery turn",
    )
    worker.submit(
        "push",
        {"account": "B", "item": "B2", "graph": True,
         "repository": {"id": 2001}},
        "graph-fair-b2",
    )
    check(
        completions["B2"].wait(0.5),
        "B acquires by the second graph-account turn despite sustained A work",
    )
    worker.submit(
        "push",
        {"account": "A", "item": "A4", "graph": True,
         "repository": {"id": 1003}},
        "graph-fair-a4",
    )
    check(
        completions["A4"].wait(0.5) and worker.wait_idle(1.0),
        "A resumes after B without leaking the singleton slot",
    )
    check(
        acquired_order[:3] == ["A", "B", "A"],
        f"keyed-pool graph turns are A,B,A rather than A,A,... (order={acquired_order})",
    )
    check(
        all(
            observed_accounts.get(item) == account
            for item, account in {
                "A1": "A", "A2": "A", "B1": "B", "C1": "C",
                "A3": "A", "B2": "B", "A4": "A",
            }.items()
        ),
        "EventQueue binds the account ContextVar for every graph and non-graph delivery",
    )


if __name__ == "__main__":
    test_cross_account_progress_and_account_fifo()
    test_pool_health_detects_partial_worker_death()
    test_repository_lanes_and_account_wide_barrier()
    test_noisy_account_cannot_occupy_the_whole_pool()
    test_recovery_reservations_follow_repository_barriers()
    test_default_one_and_backpressure_compatibility()
    test_delivery_shutdown_registry_is_pool_safe()
    test_repository_fifo_index_is_bounded_and_not_rescanned()
    test_graph_slot_contention_defers_quickly()
    test_graph_slot_preserves_fifo_account_turns()
    test_graph_slot_fairness_integrates_with_keyed_pool()
    if FAIL:
        raise SystemExit(1)
    print("KEYED WORKER POOL GATE: PASS")
