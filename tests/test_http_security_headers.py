#!/usr/bin/env python3
"""Core HTTP security-header baseline gate.

The Core webhook service is JSON/API-only: public status, operator probes, and
GitHub webhook ingress. Every response, including direct POST rejects that do
not use the JSON helper, should carry a conservative header baseline.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import server_http as SH  # noqa: E402


FAIL = 0


def check(cond, msg):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAIL = 1


def install_fake_server(*, signature_ok: bool = False, body: bytes = b"{}"):
    fake_server = type(sys)("server")
    fake_server.health_snapshot = lambda worker: {
        "healthy": True,
        "worker_alive": True,
        "version": "headers-test",
        "queue_depth": 0,
        "failure_ratio_5min": {"ratio": 0.0, "threshold": 0.05},
    }
    fake_server.watchdog_last_tick_seconds = lambda: 0.0
    fake_server.app_identity_ok = lambda dsn: (True, None)
    fake_server._worker_stuck_seconds = lambda: 600
    fake_server.app_jwt_reachable = lambda: True
    fake_server.read_bounded_body = lambda content_length, rfile: (body, None)
    fake_server.verify_signature = lambda secret, body, sig: signature_ok
    fake_server._as_obj = lambda value: value if isinstance(value, dict) else {}
    fake_server._event_account_key = lambda payload: None
    fake_server._event_repo = lambda payload: None
    sys.modules["server"] = fake_server


def make_handler_instance(path="/statusz", headers=None, worker=None, store=None, persist_all=None, trace=None):
    trace = trace if trace is not None else []
    worker = worker or type("Worker", (), {"submit": lambda self, event, payload, delivery: True})()
    Handler = SH.make_handler(
        secret="webhook-secret",
        store=store,
        worker=worker,
        db=lambda *a, **k: None,
        dsn="",
        gh=None,
        persist_all=persist_all,
    )
    h = Handler.__new__(Handler)
    h.path = path
    h.headers = headers or {}
    h.rfile = object()
    h._headers_buffer = []
    h.request_version = "HTTP/1.1"
    captured = {"status": None, "headers": [], "body": b"", "trace": trace}

    def send_response(code, message=None):
        captured["status"] = code
        trace.append(f"send_response:{code}")

    def send_header(k, v):
        captured["headers"].append((k, v))

    class Wfile:
        def write(self, b):
            captured["body"] += b

    h.send_response = send_response
    h.send_header = send_header
    h.flush_headers = lambda: trace.append(f"end_headers:{captured['status']}")
    h.wfile = Wfile()
    return h, captured


def header_map(captured):
    return {k.lower(): v for k, v in captured["headers"]}


def assert_security_headers(captured, label):
    headers = header_map(captured)
    check(headers.get("content-security-policy") == "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
          f"{label} sets the Core API content security policy")
    check(headers.get("strict-transport-security") == "max-age=63072000; includeSubDomains; preload",
          f"{label} sets long-lived HSTS")
    check(headers.get("x-content-type-options") == "nosniff",
          f"{label} sets nosniff")
    check(headers.get("x-frame-options") == "DENY",
          f"{label} denies framing")
    check(headers.get("referrer-policy") == "no-referrer",
          f"{label} suppresses referrer leakage")
    check(headers.get("permissions-policy") == "camera=(), microphone=(), geolocation=(), browsing-topics=()",
          f"{label} disables unused browser permissions")
    check(headers.get("cache-control") == "no-store",
          f"{label} disables shared caching")


install_fake_server(signature_ok=False)
status_h, status_captured = make_handler_instance("/statusz?ignore=1")
status_h.do_GET()
status_body = json.loads(status_captured["body"].decode())
check(status_captured["status"] == 200 and status_body["status"] == "operational",
      "control: /statusz still returns the public-safe payload")
assert_security_headers(status_captured, "/statusz")

health_h, health_captured = make_handler_instance("/healthz")
health_h.do_GET()
health_body = json.loads(health_captured["body"].decode())
check(health_captured["status"] == 200 and health_body["healthy"] is True,
      "control: /healthz still returns the operator liveness payload")
assert_security_headers(health_captured, "/healthz")


class OwnerLivenessStore:
    def __init__(self, healthy, *, thread_alive=None):
        self.healthy = bool(healthy)
        self.thread_alive = (
            self.healthy if thread_alive is None else bool(thread_alive))

    def liveness_snapshot(self):
        return {
            "healthy": self.healthy,
            "thread_started": True,
            "thread_alive": self.thread_alive,
            "last_successful_heartbeat_seconds": 1.0 if self.healthy else 101.0,
            "heartbeat_dead_seconds": 15,
            "reclaim_safe_seconds": 100,
        }

    def depth(self):
        return {}


# Worker threads alone are not healthy when the daemon maintaining their
# cross-instance lease authority is dead/stale. The same composed snapshot
# drives all three HTTP status decisions.
install_fake_server(signature_ok=False)
dead_liveness_store = OwnerLivenessStore(False)
dead_health_h, dead_health = make_handler_instance(
    "/healthz", store=dead_liveness_store)
dead_health_h.do_GET()
dead_health_body = json.loads(dead_health["body"].decode())
check(dead_health["status"] == 503
      and dead_health_body["worker_alive"] is True
      and dead_health_body["healthy"] is False
      and dead_health_body["delivery_liveness"]["thread_alive"] is False,
      "/healthz fails closed when owner-liveness dies although event workers remain alive")

dead_ready_h, dead_ready = make_handler_instance(
    "/readyz", store=OwnerLivenessStore(False, thread_alive=True))
dead_ready_h.do_GET()
dead_ready_body = json.loads(dead_ready["body"].decode())
check(dead_ready["status"] == 503
      and dead_ready_body["ready"] is False
      and dead_ready_body["delivery_liveness"]["healthy"] is False
      and dead_ready_body["delivery_liveness"]["thread_alive"] is True,
      "/readyz fails after a live liveness thread misses the reclaim-safe heartbeat ceiling")

dead_status_h, dead_status = make_handler_instance(
    "/statusz", store=dead_liveness_store)
dead_status_h.do_GET()
dead_status_body = json.loads(dead_status["body"].decode())
check(dead_status["status"] == 503
      and dead_status_body["status"] == "degraded"
      and dead_status_body["signals"]["worker"] == "degraded",
      "public status degrades without exposing owner identity when owner-liveness is unhealthy")

reject_h, reject_captured = make_handler_instance(
    "/webhook",
    headers={
        "Content-Length": "2",
        "X-Hub-Signature-256": "sha256=bad",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-id",
    },
)
reject_h.do_POST()
check(reject_captured["status"] == 401 and b"bad signature" in reject_captured["body"],
      "control: forged webhook request still fails closed")
assert_security_headers(reject_captured, "webhook 401")

install_fake_server(signature_ok=True)
accept_h, accept_captured = make_handler_instance(
    "/webhook",
    headers={
        "Content-Length": "2",
        "X-Hub-Signature-256": "sha256=ok",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-id",
    },
)
accept_h.do_POST()
check(accept_captured["status"] == 202 and b"accepted" in accept_captured["body"],
      "control: verified webhook request is still accepted")
assert_security_headers(accept_captured, "webhook 202")


class RecordingWorker:
    def __init__(self, trace=None):
        self.calls = []
        self.trace = trace

    def submit(self, event, payload, delivery):
        if self.trace is not None:
            self.trace.append("worker.submit")
        self.calls.append((event, payload, delivery))
        return True


class RecordingStore:
    def __init__(self, trace=None, *, queued=True):
        self.calls = []
        self.trace = trace
        self.queued = queued

    def submit(self, event, payload, delivery, *, account_key=None, repo=None):
        if self.trace is not None:
            self.trace.append("store.submit")
        self.calls.append((event, payload, delivery, account_key, repo))
        key = delivery or "local-test"
        stored = dict(payload)
        stored["_veripsa_delivery_key"] = key
        return {
            "accepted": True,
            "queued": self.queued,
            "event_type": event,
            "payload": stored,
            "delivery": key,
        }

    def depth(self):
        return {}


for event_type, payload in (
    ("repository", {"action": "deleted", "repository": {"id": 42, "full_name": "acme/removed"}}),
    ("installation_repositories", {
        "action": "removed",
        "repositories_removed": [{"id": 42, "full_name": "acme/removed"}],
    }),
):
    body = json.dumps(payload).encode()
    install_fake_server(signature_ok=True, body=body)
    for persist_all in (True, False):
        trace = []
        offboard_worker = RecordingWorker(trace)
        offboard_store = RecordingStore(trace)
        offboard_h, offboard_captured = make_handler_instance(
            "/webhook",
            headers={
                "Content-Length": str(len(body)),
                "X-Hub-Signature-256": "sha256=ok",
                "X-GitHub-Event": event_type,
                "X-GitHub-Delivery": f"delivery-{event_type}-{persist_all}",
            },
            worker=offboard_worker,
            store=offboard_store,
            persist_all=persist_all,
            trace=trace,
        )
        offboard_h.do_POST()
        mode = "normal durable path" if persist_all else "ordinary-event kill switch"
        check(offboard_captured["status"] == 202 and b"accepted" in offboard_captured["body"],
              f"{mode} accepts {event_type} removal only after durable persistence")
        check(len(offboard_store.calls) == 1,
              f"{mode} persists {event_type} removal before 202")
        check(len(offboard_worker.calls) == 1
              and offboard_worker.calls[0][1].get("_veripsa_delivery_key"),
              f"{mode} enqueues delivery-keyed {event_type} removal")
        check(trace == ["store.submit", "worker.submit", "send_response:202", "end_headers:202"],
              f"{mode} orders {event_type} persistence and enqueue before the 202 is flushed")
        assert_security_headers(offboard_captured, f"{event_type} offboard 202 ({mode})")


# A processing/done durable duplicate is already owned by its original generation. Ack it idempotently without
# invalidating that generation's receipt or appending a claim-noop duplicate to the worker queue.
duplicate_body = json.dumps({"action": "opened", "pull_request": {"number": 8}}).encode()
install_fake_server(signature_ok=True, body=duplicate_body)
duplicate_trace = []
duplicate_worker = RecordingWorker(duplicate_trace)
duplicate_store = RecordingStore(duplicate_trace, queued=False)
duplicate_h, duplicate_captured = make_handler_instance(
    "/webhook",
    headers={
        "Content-Length": str(len(duplicate_body)),
        "X-Hub-Signature-256": "sha256=ok",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-processing-duplicate",
    },
    worker=duplicate_worker,
    store=duplicate_store,
    persist_all=True,
    trace=duplicate_trace,
)
duplicate_h.do_POST()
check(duplicate_captured["status"] == 202 and b"accepted" in duplicate_captured["body"],
      "processing/done durable duplicate is acknowledged idempotently")
check(duplicate_worker.calls == []
      and duplicate_trace == ["store.submit", "send_response:202", "end_headers:202"],
      "processing/done durable duplicate does not create a second worker generation")
assert_security_headers(duplicate_captured, "durable duplicate 202")


# Production always supplies the authority store. A miswired/local receiver must fail before enqueue/202, because
# a deletion without that store cannot authenticate the stable repository id or durable receive order.
missing_store_payload = {
    "action": "deleted",
    "repository": {"id": 42, "full_name": "acme/removed"},
}
missing_store_body = json.dumps(missing_store_payload).encode()
install_fake_server(signature_ok=True, body=missing_store_body)
missing_store_worker = RecordingWorker()
missing_store_h, missing_store_captured = make_handler_instance(
    "/webhook",
    headers={
        "Content-Length": str(len(missing_store_body)),
        "X-Hub-Signature-256": "sha256=ok",
        "X-GitHub-Event": "repository",
        "X-GitHub-Delivery": "delivery-missing-offboard-store",
    },
    worker=missing_store_worker,
    store=None,
    persist_all=False,
)
missing_store_h.do_POST()
check(missing_store_captured["status"] == 503 and missing_store_worker.calls == [],
      "miswired no-store repository deletion fails before enqueue or 202")
assert_security_headers(missing_store_captured, "repository offboard no-store 503")


# The kill switch remains real for ordinary events: keeping the store available for deletion authority must not
# silently turn normal PR traffic back into persist-all mode.
ordinary_body = json.dumps({"action": "opened", "pull_request": {"number": 7}}).encode()
install_fake_server(signature_ok=True, body=ordinary_body)
ordinary_worker = RecordingWorker()
ordinary_store = RecordingStore()
ordinary_h, ordinary_captured = make_handler_instance(
    "/webhook",
    headers={
        "Content-Length": str(len(ordinary_body)),
        "X-Hub-Signature-256": "sha256=ok",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-ordinary-kill-switch",
    },
    worker=ordinary_worker,
    store=ordinary_store,
    persist_all=False,
)
ordinary_h.do_POST()
check(ordinary_captured["status"] == 202 and ordinary_store.calls == [],
      "ordinary-event kill switch still bypasses durable persistence for pull_request")
check(len(ordinary_worker.calls) == 1
      and "_veripsa_delivery_key" not in ordinary_worker.calls[0][1],
      "ordinary-event kill switch keeps pull_request on the memory queue")


if FAIL:
    raise SystemExit(1)
print("CORE HTTP SECURITY HEADERS: PASS")
