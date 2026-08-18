#!/usr/bin/env python3
"""Repository offboarding stays durable when ordinary webhook persistence is disabled."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import server_boot as SB  # noqa: E402


FAIL = 0


def check(condition, message):
    global FAIL
    print(("  [PASS] " if condition else "  [FAIL] ") + message)
    if not condition:
        FAIL = 1


stores = []
workers = []
recovery_calls = []
liveness_calls = []
policy_refresh_calls = []
threads = []
startup_order = []


class FakeStore:
    def __init__(self, dsn):
        self.dsn = dsn
        self.wrapped_base = None
        self.wrapped_process = None
        stores.append(self)

    def wrap_processor(self, processor):
        self.wrapped_base = processor

        def wrapped(*args, **kwargs):
            return processor(*args, **kwargs)

        self.wrapped_process = wrapped
        return wrapped

    def beat_instance(self):
        startup_order.append("heartbeat")


class FakeWorker:
    def __init__(self, db, gh, *, process, account_of, repo_of, branch_from_ref):
        self.process = process
        self.gh = gh
        workers.append(self)

    def start(self):
        startup_order.append("worker")
        return self


class FakeThread:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        threads.append(self)

    def start(self):
        startup_order.append(self.kwargs.get("name"))
        return None


def base_processor(*args, **kwargs):
    return {"ok": True}


fake_server = SimpleNamespace(
    DeliveryStore=FakeStore,
    EventQueue=FakeWorker,
    make_db_processor=lambda dsn: base_processor,
    _event_account_key=lambda payload: None,
    _event_repo=lambda payload: None,
    _branch_from_ref=lambda ref: None,
    start_recovery_loop=lambda store, worker: (
        recovery_calls.append((store, worker)), startup_order.append("recovery")),
    start_instance_liveness_loop=lambda store: (
        store.beat_instance(), liveness_calls.append(store)),
    # G4 policy-change refresh: wire_runtime constructs PolicyRefreshStore(dsn) and starts the drainer loop
    # right after delivery recovery (mirrors start_recovery_loop). Stubbed so the composition root runs offline.
    PolicyRefreshStore=lambda dsn: SimpleNamespace(dsn=dsn),
    start_policy_refresh_loop=lambda store, gh, dsn, **kwargs: (
        policy_refresh_calls.append((store, gh, dsn)), startup_order.append("policy-refresh")),
    converge_main_graph_strict=lambda *args, **kwargs: {"converged": True},
    run_watchdog=lambda **kwargs: None,
    graph_freshness_all=lambda *args, **kwargs: [],
)

saved_server = SB._server
saved_contract = SB.check_schema_contract
saved_set_result = SB.set_boot_result
saved_thread = SB.threading.Thread
saved_event = SB.threading.Event
saved_reconcile = SB._boot_reconcile_throttled
env_names = (
    "VERIPSA_DURABLE_INBOX",
    "VERIPSA_BOOT_RECONCILE",
    "VERIPSA_BOOT_RECONCILE_START_DELAY_SEC",
    "VERIPSA_DELIVERY_RECOVERY",
    "VERIPSA_RUNTIME_ROLE",
    "VERIPSA_POLICY_REFRESH",
)
saved_env = {name: os.environ.get(name) for name in env_names}

try:
    SB._server = lambda: fake_server
    SB.check_schema_contract = lambda dsn: SimpleNamespace(
        skipped=True,
        healthy=True,
        checked=0,
        violations=[],
    )
    SB.set_boot_result = lambda result: None
    SB.threading.Thread = FakeThread
    os.environ["VERIPSA_DURABLE_INBOX"] = "0"
    os.environ["VERIPSA_BOOT_RECONCILE"] = "1"
    os.environ.pop("VERIPSA_BOOT_RECONCILE_START_DELAY_SEC", None)
    os.environ["VERIPSA_DELIVERY_RECOVERY"] = "1"
    # An old image/local embed has no role. Keeping refresh=1 must preserve its
    # in-process drainer so a web-image rollback immediately restores convergence.
    os.environ.pop("VERIPSA_RUNTIME_ROLE", None)
    os.environ["VERIPSA_POLICY_REFRESH"] = "1"

    runtime = SB.wire_runtime(SB.BootConfig(
        secret="secret",
        dsn="postgresql://example/veripsa",
        gh=object(),
        db=lambda *args, **kwargs: None,
    ))

    grace_order = []

    class FakeGraceEvent:
        def wait(self, seconds):
            grace_order.append(("wait", seconds))

    SB.threading.Event = FakeGraceEvent
    SB._boot_reconcile_throttled = (
        lambda *_args, **_kwargs: grace_order.append(("reconcile", None)))
    SB._boot_reconcile_after_live_grace(
        120, lambda *args, **kwargs: None, object(), 1,
        "postgresql://example/veripsa", 60, False, 0)
finally:
    SB._server = saved_server
    SB.check_schema_contract = saved_contract
    SB.set_boot_result = saved_set_result
    SB.threading.Thread = saved_thread
    SB.threading.Event = saved_event
    SB._boot_reconcile_throttled = saved_reconcile
    for name, value in saved_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


check(len(stores) == 1 and runtime.store is stores[0],
      "composition root always constructs the durable authority store")
check(runtime.persist_all is False,
      "VERIPSA_DURABLE_INBOX=0 disables ordinary-event persistence")
check(stores[0].wrapped_base is base_processor
      and workers[0].process is stores[0].wrapped_process,
      "worker always understands delivery-keyed repository offboarding payloads")
check(recovery_calls == [(stores[0], workers[0])],
      "delivery recovery remains wired for persisted offboarding and existing backlog")
check(liveness_calls == [stores[0]],
      "worker instance authority is synchronously registered before work starts")
check(len(policy_refresh_calls) == 1 and policy_refresh_calls[0][2] == "postgresql://example/veripsa",
      "unset-role old-image compatibility still wires the refresh drainer on the same dsn")
check([thread.kwargs.get("name") for thread in threads]
      == ["veripsa-boot-reconcile", "veripsa-failed-delivery-recovery", "veripsa-watchdog"],
      "boot reconcile, failed-delivery recovery, and watchdog remain separately named background threads")
check(startup_order == ["heartbeat", "worker", "recovery", "policy-refresh", "veripsa-boot-reconcile",
                        "veripsa-failed-delivery-recovery", "veripsa-watchdog"],
      "composition root registers lease authority before worker/local recovery and lower-priority work")
boot_thread = threads[0]
check(boot_thread.kwargs.get("target") is SB._boot_reconcile_after_live_grace
      and boot_thread.kwargs.get("args", (None,))[0] == 120,
      "boot convergence is routed through the default 120-second live-start grace")
check(grace_order == [("wait", 120), ("reconcile", None)],
      "the default grace wait happens before boot reconciliation")

if FAIL:
    raise SystemExit(1)
print("DURABLE OFFBOARDING KILL SWITCH: PASS")
