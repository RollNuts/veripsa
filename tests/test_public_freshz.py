#!/usr/bin/env python3
"""Public /freshz privacy boundary gate.

The internal sampler needs cross-tenant coordinates to detect graph drift. The unauthenticated HTTP surface must
expose only aggregate state, including on aliases, cache hits, stale samples, slow/failure fallbacks, and malformed
future cache values.
"""
from __future__ import annotations

import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import server_http as SH  # noqa: E402


FAIL = 0
PRIVATE_REPO = "customer/private-control-plane"
PRIVATE_BRANCH = "agent/private-rollout"
PRIVATE_SHA = "a" * 40
PRIVATE_ACCOUNT = "account-4242"
ALLOWED_KEYS = {
    "service",
    "coordinate_count",
    "behind_count",
    "unknown_count",
    "any_behind",
    "sampled",
    "coverage_complete",
    "status",
    "timed_out",
    "cursor_healthy",
    "sample_error",
    "stale_sample",
    "sample_age_seconds",
}


def check(cond, msg):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAIL = 1


def assert_public(label: str, payload: dict) -> None:
    rendered = json.dumps(payload, sort_keys=True)
    check(set(payload) <= ALLOWED_KEYS, f"{label}: response uses the aggregate allowlist")
    check("coordinates" not in payload, f"{label}: coordinates array is absent")
    for forbidden in (PRIVATE_REPO, PRIVATE_BRANCH, PRIVATE_SHA, PRIVATE_ACCOUNT, "secret source", "@@ private"):
        check(forbidden not in rendered, f"{label}: drops private value {forbidden}")


def exercise_route(path: str, records: list[dict]) -> tuple[int, dict]:
    fake_server = type(sys)("server")
    fake_server.graph_freshness_all = lambda db, gh: list(records)
    sys.modules["server"] = fake_server
    Handler = SH.make_handler(secret="x", store=None, worker=object(), db=lambda sql: {}, dsn="", gh=object())
    handler = Handler.__new__(Handler)
    handler.path = path
    handler.headers = {}
    captured = {"status": None, "headers": [], "body": b""}

    handler.send_response = lambda code, message=None: captured.update(status=code)
    handler.send_header = lambda key, value: captured["headers"].append((key, value))
    handler.end_headers = lambda: None

    class Wfile:
        def write(self, body):
            captured["body"] += body

    handler.wfile = Wfile()
    handler.do_GET()
    return captured["status"], json.loads(captured["body"].decode())


private_records = [
    {
        "account_id": PRIVATE_ACCOUNT,
        "repo": PRIVATE_REPO,
        "branch": PRIVATE_BRANCH,
        "stored_sha": PRIVATE_SHA,
        "head_sha": "b" * 40,
        "behind": True,
        "node_count": 123,
        "edge_count": 456,
        "source_body": "secret source",
        "diff_body": "@@ private",
    },
    {"repo": "another/private-repo", "branch": "main", "behind": False},
]

old_ttl = os.environ.get("VERIPSA_FRESHZ_CACHE_SECONDS")
try:
    os.environ["VERIPSA_FRESHZ_CACHE_SECONDS"] = "0"
    for route in ("/freshz?probe=1", "/fresh"):
        status, body = exercise_route(route, private_records)
        check(status == 200, f"{route}: aggregate freshness remains available")
        check(body["coordinate_count"] == 2 and body["behind_count"] == 1 and body["any_behind"] is True,
              f"{route}: aggregate counts preserve drift signal")
        check(body["sampled"] is True, f"{route}: successful sample remains explicit")
        check(body["coverage_complete"] is True and body["unknown_count"] == 0
              and body["status"] == "behind",
              f"{route}: a legacy plain-list sample remains coverage-compatible and reports drift")
        assert_public(route, body)

    dirty_stale = {
        "service": "attacker-controlled",
        "coordinates": private_records,
        "coordinate_count": 2,
        "behind_count": 1,
        "any_behind": True,
        "sampled": True,
        "stale_sample": True,
        "sample_age_seconds": 12.34567,
        "repo": PRIVATE_REPO,
        "account_id": PRIVATE_ACCOUNT,
        "sample_error": PRIVATE_REPO,
    }
    projected = SH._public_freshness_payload(dirty_stale)
    check(projected["service"] == "veripsa-webhook", "final projection fixes the service identity")
    check(projected["sample_age_seconds"] == 12.346, "stale age remains bounded aggregate metadata")
    check(projected["status"] == "behind" and projected["coverage_complete"] is True,
          "final projection derives status from aggregate evidence instead of accepting a caller claim")
    check("sample_error" not in projected, "unknown sample errors cannot widen the public response")
    assert_public("dirty stale cache projection", projected)

    fallback_db = lambda sql: {
        "coordinate_count": 7,
        "coordinates": private_records,
        "account_id": PRIVATE_ACCOUNT,
    }
    Handler = SH.make_handler(secret="x", store=None, worker=object(), db=fallback_db, dsn="", gh=object())
    fallback = Handler.__new__(Handler)._freshz_unsampled_payload("freshness_sample_in_progress")
    check(fallback["coordinate_count"] == 7 and fallback["sampled"] is False,
          "slow-sample fallback keeps honest aggregate count and sampled:false")
    fallback_public = SH._public_freshness_payload(fallback)
    check(fallback_public["coverage_complete"] is False
          and fallback_public["unknown_count"] == 7
          and fallback_public["status"] == "unknown",
          "an unsampled fleet is explicitly Unknown, never false-Clear")
    assert_public("slow/failure fallback", fallback_public)

    class CoverageSample(list):
        def __init__(self, rows, *, complete, cursor=True, timed_out=False):
            super().__init__(rows)
            self.coverage_complete = complete
            self.cursor_healthy = cursor
            self.timed_out = timed_out

    handler = Handler.__new__(Handler)
    partial = CoverageSample(
        [{"behind": False}], complete=False, cursor=True)
    partial_payload = handler._freshz_sample_now(
        type("Sampler", (), {
            "graph_freshness_all": staticmethod(lambda db, gh: partial),
        }),
        ("partial",), cache_success=False)
    check(partial_payload["sampled"] is True
          and partial_payload["any_behind"] is False
          and partial_payload["coverage_complete"] is False
          and partial_payload["status"] == "unknown",
          "a successful but partial sample is Unknown, never false-Clear")

    unresolved = CoverageSample(
        [{"behind": None}], complete=True, cursor=True)
    unresolved_payload = handler._freshz_sample_now(
        type("Sampler", (), {
            "graph_freshness_all": staticmethod(lambda db, gh: unresolved),
        }),
        ("unresolved",), cache_success=False)
    check(unresolved_payload["coverage_complete"] is True
          and unresolved_payload["unknown_count"] == 1
          and unresolved_payload["status"] == "unknown",
          "complete coverage with an unresolved HEAD remains Unknown")

    timed_out = CoverageSample(
        [{"behind": False}], complete=True, cursor=True, timed_out=True)
    timed_out_payload = handler._freshz_sample_now(
        type("Sampler", (), {
            "graph_freshness_all": staticmethod(lambda db, gh: timed_out),
        }),
        ("timed-out",), cache_success=False)
    check(timed_out_payload["timed_out"] is True
          and timed_out_payload["coverage_complete"] is False
          and timed_out_payload["status"] == "unknown",
          "a timed-out sample cannot claim complete coverage or Clear")

    cursor_failed = CoverageSample(
        [{"behind": False}], complete=True, cursor=False)
    cursor_failed_payload = handler._freshz_sample_now(
        type("Sampler", (), {
            "graph_freshness_all": staticmethod(lambda db, gh: cursor_failed),
        }),
        ("cursor-failed",), cache_success=False)
    check(cursor_failed_payload["cursor_healthy"] is False
          and cursor_failed_payload["coverage_complete"] is False
          and cursor_failed_payload["status"] == "unknown",
          "a failed freshness cursor cannot claim complete coverage or Clear")

    plain_clear = handler._freshz_sample_now(
        type("Sampler", (), {
            "graph_freshness_all": staticmethod(
                lambda db, gh: [{"behind": False}]),
        }),
        ("plain-clear",), cache_success=False)
    check(plain_clear["coverage_complete"] is True
          and plain_clear["cursor_healthy"] is True
          and plain_clear["status"] == "clear",
          "legacy plain-list samplers retain complete/Clear compatibility")

    malformed = SH._public_freshness_payload({
        "coordinate_count": PRIVATE_REPO,
        "behind_count": True,
        "any_behind": "yes",
        "sampled": "yes",
        "stale_sample": "yes",
        "sample_age_seconds": float("inf"),
        "coordinates": private_records,
    })
    check(malformed == {
        "service": "veripsa-webhook",
        "coordinate_count": 0,
        "behind_count": 0,
        "unknown_count": 0,
        "any_behind": False,
        "sampled": False,
        "coverage_complete": False,
        "status": "unknown",
        "timed_out": False,
        "cursor_healthy": False,
    }, "malformed optional values fail closed to the minimal aggregate shape")
    assert_public("malformed projection", malformed)
finally:
    if old_ttl is None:
        os.environ.pop("VERIPSA_FRESHZ_CACHE_SECONDS", None)
    else:
        os.environ["VERIPSA_FRESHZ_CACHE_SECONDS"] = old_ttl


if FAIL:
    raise SystemExit(1)
print("PUBLIC FRESHZ GATE: PASS")
