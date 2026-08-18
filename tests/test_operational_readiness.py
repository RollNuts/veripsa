#!/usr/bin/env python3
"""Operational readiness must not repeat the durable-inbox false green.

/healthz is intentionally process liveness. /readyz additionally owns the
customer-visible durable-work and account-convergence SLOs. Missing evidence is
Unknown and must not be interpreted as Clear.
"""
from __future__ import annotations

import json
import os
import sys
import types


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import server_http as SH  # noqa: E402


CLEAR_INBOX = {
    "queued": 0,
    "queued_due": 0,
    "queued_due_oldest_age_seconds": 0,
    "processing": 0,
    "processing_oldest_age_seconds": 0,
    "failed": 0,
}
CLEAR_CONVERGENCE = {
    "pending": 0,
    "claimed": 0,
    "retry_exhausted": 0,
    "quota_deferred": 0,
    "due_accounts": 0,
    "stalled_accounts": 0,
    "oldest_age_seconds": 0,
    "sample_cap": 1000,
    "scheduled_truncated": False,
    "stalled_truncated": False,
    "claimed_truncated": False,
    "exceptions_truncated": False,
}


class Store:
    def __init__(self, depth=CLEAR_INBOX):
        self._depth = depth

    def liveness_snapshot(self):
        return {
            "healthy": True,
            "thread_started": True,
            "thread_alive": True,
            "last_successful_heartbeat_seconds": 1.0,
        }

    def depth(self):
        if isinstance(self._depth, BaseException):
            raise self._depth
        return self._depth


class Worker:
    def stuck_worker_count(self, _threshold):
        return 0


class DB:
    def __init__(self, convergence=CLEAR_CONVERGENCE):
        self.convergence = convergence

    def __call__(self, sql):
        if "account_convergence_depth_with_authority" in sql:
            if isinstance(self.convergence, BaseException):
                raise self.convergence
            return self.convergence
        if "owner_graph_freshness_surface" in sql:
            return {"coordinate_count": 0, "max_age_seconds": 0}
        return 1


def _install_server():
    fake = types.ModuleType("server")
    fake.health_snapshot = lambda worker: {
        "healthy": True,
        "worker_alive": True,
        "alive_workers": 4,
        "worker_count": 4,
        "queue_depth": 0,
        "inflight_age_seconds": None,
        "version": "candidate123",
    }
    fake.watchdog_last_tick_seconds = lambda: 0.0
    fake.app_identity_ok = lambda dsn: (True, None)
    fake._worker_stuck_seconds = lambda: 120.0
    fake._worker_restart_seconds = lambda: 180.0
    fake.app_jwt_reachable = lambda: True
    fake.graph_extraction_liveness = lambda hard: {"healthy": True}
    sys.modules["server"] = fake


def _request(path, *, depth=CLEAR_INBOX, convergence=CLEAR_CONVERGENCE):
    _install_server()
    Handler = SH.make_handler(
        secret="test",
        store=Store(depth),
        worker=Worker(),
        db=DB(convergence),
        dsn="test",
        gh=None,
    )
    handler = Handler.__new__(Handler)
    handler.path = path
    handler.headers = {}
    handler.request_version = "HTTP/1.1"
    handler._headers_buffer = []
    captured = {"status": None, "body": b""}
    handler.send_response = (
        lambda code, message=None: captured.__setitem__("status", code))
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None

    class WFile:
        def write(self, body):
            captured["body"] += body

    handler.wfile = WFile()
    handler.do_GET()
    return captured["status"], json.loads(captured["body"])


def test_reported_durable_condition_is_non_ready_but_live():
    incident = {
        "queued": 24,
        "queued_due": 24,
        "queued_due_oldest_age_seconds": 19185,
        "processing": 1,
        "processing_oldest_age_seconds": 1159,
        "failed": 0,
    }
    health_status, health = _request("/healthz", depth=incident)
    assert health_status == 200 and health["healthy"] is True

    ready_status, ready = _request("/readyz", depth=incident)
    assert ready_status == 503 and ready["ready"] is False
    assert ready["inbox_depth"] == incident
    assert ready["readiness_failures"] == [
        "durable_inbox_latency",
        "durable_inbox_processing_stalled",
    ]


def test_clear_samples_are_ready_and_quota_only_is_not_a_failure():
    quota_only = dict(
        CLEAR_CONVERGENCE,
        quota_deferred=7,
    )
    status, body = _request(
        "/readyz", depth=CLEAR_INBOX, convergence=quota_only)
    assert status == 200 and body["ready"] is True
    assert body["readiness_failures"] == []
    assert body["account_convergence"]["quota_deferred"] == 7


def test_missing_or_malformed_evidence_is_unknown_never_clear():
    samples = (
        {},
        dict(CLEAR_INBOX, queued_due="0"),
        dict(CLEAR_INBOX, processing=False),
        dict(CLEAR_INBOX, queued=0, queued_due=1),
        dict(
            CLEAR_INBOX,
            queued_due=0,
            queued_due_oldest_age_seconds=1,
        ),
        RuntimeError("db unavailable"),
    )
    for sample in samples:
        status, body = _request("/readyz", depth=sample)
        assert status == 503 and body["ready"] is False
        assert body["inbox_depth"] is None
        assert "durable_inbox_unknown" in body["readiness_failures"]

    for convergence in (
        {},
        dict(CLEAR_CONVERGENCE, stalled_accounts="0"),
        dict(CLEAR_CONVERGENCE, stalled_accounts=0, oldest_age_seconds=1),
        dict(
            CLEAR_CONVERGENCE,
            quota_deferred=1000,
            exceptions_truncated=True,
        ),
        dict(CLEAR_CONVERGENCE, sample_cap=999),
        RuntimeError("db unavailable"),
    ):
        status, body = _request(
            "/readyz", convergence=convergence)
        assert status == 503 and body["ready"] is False
        assert body["account_convergence"] is None
        assert "account_convergence_unknown" in body["readiness_failures"]


def test_dead_letters_and_critical_convergence_are_non_ready():
    status, body = _request(
        "/readyz", depth=dict(CLEAR_INBOX, failed=1))
    assert status == 503
    assert body["readiness_failures"] == ["durable_inbox_dead_letter"]

    critical = dict(
        CLEAR_CONVERGENCE,
        pending=1,
        due_accounts=1,
        stalled_accounts=1,
        oldest_age_seconds=300,
    )
    status, body = _request("/readyz", convergence=critical)
    assert status == 503
    assert body["readiness_failures"] == ["account_convergence_stalled"]

    exhausted = dict(
        CLEAR_CONVERGENCE,
        retry_exhausted=1,
        stalled_accounts=1,
        oldest_age_seconds=1,
    )
    status, body = _request("/readyz", convergence=exhausted)
    assert status == 503
    assert body["readiness_failures"] == [
        "account_convergence_retry_exhausted"]


if __name__ == "__main__":
    tests = (
        test_reported_durable_condition_is_non_ready_but_live,
        test_clear_samples_are_ready_and_quota_only_is_not_a_failure,
        test_missing_or_malformed_evidence_is_unknown_never_clear,
        test_dead_letters_and_critical_convergence_are_non_ready,
    )
    for test in tests:
        test()
    print(
        f"OPERATIONAL READINESS: PASS ({len(tests)} contracts)",
        flush=True,
    )
