#!/usr/bin/env python3
"""Public /statusz boundary gate.

/healthz is an operator probe and can carry internal counters. /statusz is the public/support trust surface:
coarse health only, never repo coordinates, PR numbers, delivery ids, Check Run ids/URLs, source bodies, or diff
bodies. This gate locks that separation without starting the HTTP server.
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


def payload_text(snap):
    return json.dumps(SH._public_status_payload(snap), sort_keys=True)


def exercise_statusz_route(snap):
    fake_server = type(sys)("server")
    fake_server.health_snapshot = lambda worker: dict(snap)
    sys.modules["server"] = fake_server
    Handler = SH.make_handler(secret="x", store=None, worker=object(), db=lambda *a, **k: None, dsn="", gh=None)
    h = Handler.__new__(Handler)
    h.path = "/statusz?ignore=1"
    h.headers = {}
    captured = {"status": None, "headers": [], "body": b""}

    def send_response(code, message=None):
        captured["status"] = code

    def send_header(k, v):
        captured["headers"].append((k, v))

    class Wfile:
        def write(self, b):
            captured["body"] += b

    h.send_response = send_response
    h.send_header = send_header
    h.end_headers = lambda: None
    h.wfile = Wfile()
    h.do_GET()
    return captured["status"], json.loads(captured["body"].decode())


good = SH._public_status_payload({
    "healthy": True,
    "worker_alive": True,
    "version": "abc123",
    "queue_depth": 0,
    "queue_maxsize": 1000,
    "failure_ratio_5min": {"ratio": 0.0, "threshold": 0.05},
})
check(good["status"] == "operational", "healthy worker + low queue + clean failure window is operational")
check(good["signals"]["check_run_canary"] == "operator_only",
      "public status names the Check Run canary as operator-only, not a public current-health claim")
check(good["check_run_canary"]["public_last_pass"] == "not_exposed",
      "public status does not expose canary run/check identifiers")

queued = SH._public_status_payload({
    "healthy": True,
    "worker_alive": True,
    "version": "abc123",
    "queue_depth": 80,
    "failure_ratio_5min": {"ratio": 0.0, "threshold": 0.05},
})
check(queued["status"] == "degraded" and queued["signals"]["webhook_ingress"] == "degraded",
      "large queue degrades public webhook-ingress state")

failed = SH._public_status_payload({
    "healthy": True,
    "worker_alive": True,
    "version": "abc123",
    "queue_depth": 0,
    "failure_ratio_5min": {"ratio": 0.20, "threshold": 0.05},
})
check(failed["status"] == "degraded" and failed["signals"]["event_processing"] == "degraded",
      "elevated event failure ratio degrades public event-processing state")

unknown = SH._public_status_payload({
    "healthy": True,
    "worker_alive": True,
    "version": "abc123",
    "queue_depth": 0,
    "failure_ratio_5min": {"ratio": None, "threshold": 0.05},
})
check(unknown["signals"]["event_processing"] == "unknown",
      "empty failure window is honest unknown, not a fake pass/fail claim")

malformed = SH._public_status_payload({
    "healthy": True,
    "worker_alive": True,
    "version": "abc123",
    "queue_depth": "not-a-number",
    "failure_ratio_5min": {"ratio": 0.0, "threshold": "not-a-number"},
})
check(malformed["status"] == "operational",
      "malformed optional counters fail open instead of breaking the public status endpoint")

route_status, route_body = exercise_statusz_route({
    "healthy": True,
    "worker_alive": True,
    "version": "route-ok",
    "queue_depth": 0,
    "failure_ratio_5min": {"ratio": 0.0, "threshold": 0.05},
})
check(route_status == 200 and route_body["status"] == "operational" and route_body["version"] == "route-ok",
      "/statusz route returns HTTP 200 with the public-safe payload when operational")

route_degraded_status, route_degraded_body = exercise_statusz_route({
    "healthy": False,
    "worker_alive": False,
    "version": "route-bad",
    "queue_depth": 0,
    "failure_ratio_5min": {"ratio": 0.0, "threshold": 0.05},
})
check(route_degraded_status == 503 and route_degraded_body["status"] == "degraded",
      "/statusz route returns HTTP 503 when the public status is degraded")

dirty_text = payload_text({
    "healthy": True,
    "worker_alive": True,
    "version": "abc123",
    "queue_depth": 0,
    "failure_ratio_5min": {"ratio": 0.0, "threshold": 0.05},
    "repo": "RollNuts/private-repo",
    "pr": 709,
    "delivery": "github-delivery-id",
    "check_run_id": 123,
    "check_run_url": "https://github.com/RollNuts/private-repo/runs/123",
    "comment_url": "https://github.com/RollNuts/private-repo/pull/709#issuecomment-1",
    "source_body": "secret source",
    "diff_body": "@@ private diff",
})
for forbidden in (
    "RollNuts/private-repo",
    "github-delivery-id",
    "runs/123",
    "issuecomment",
    "secret source",
    "@@ private diff",
):
    check(forbidden not in dirty_text, f"public status drops forbidden value: {forbidden}")

check(set(good.keys()) == {"service", "status", "version", "signals", "check_run_canary", "privacy"},
      "public status shape is compact and explicit")
check(good["privacy"] == {
    "content_free": True,
    "repo_identifiers_exposed": False,
    "pr_identifiers_exposed": False,
    "source_or_diff_bodies_exposed": False,
}, "privacy flags state the public boundary")

if FAIL:
    raise SystemExit(1)
print("PUBLIC STATUSZ GATE: PASS")
