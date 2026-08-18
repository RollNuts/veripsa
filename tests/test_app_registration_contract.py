#!/usr/bin/env python3
"""Offline tests for the live GitHub App registration checker."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "github-app" / "scripts" / "verify_app_registration.py"

spec = importlib.util.spec_from_file_location("verify_app_registration", SCRIPT)
assert spec and spec.loader
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


MANIFEST = {
    "default_events": [
        "pull_request",
        "push",
        "repository",
        "check_suite",
        "check_run",
        "merge_group",
    ],
    "default_permissions": {
        "contents": "read",
        "pull_requests": "write",
        "checks": "write",
        "merge_queues": "read",
        "metadata": "read",
    },
}


def _live(**overrides):
    live = {
        "events": list(MANIFEST["default_events"]),
        "permissions": dict(MANIFEST["default_permissions"]),
    }
    live.update(overrides)
    return live


def _ok(results):
    return all(passed for _, passed, _ in results)


def test_matching_live_registration_passes():
    assert _ok(M.compare_registration(MANIFEST, _live()))


def test_missing_check_suite_event_fails():
    live = _live(events=["pull_request", "push", "repository", "check_run", "merge_group"])
    results = M.compare_registration(MANIFEST, live)
    assert not _ok(results)
    assert any("check_suite" in detail for _, passed, detail in results if not passed)


def test_missing_checks_write_permission_fails():
    permissions = dict(MANIFEST["default_permissions"])
    permissions["checks"] = "read"
    results = M.compare_registration(MANIFEST, _live(permissions=permissions))
    assert not _ok(results)
    assert any("checks" in detail for _, passed, detail in results if not passed)


def test_extra_issues_surface_fails():
    events = list(MANIFEST["default_events"]) + ["issues"]
    permissions = dict(MANIFEST["default_permissions"], issues="write")
    results = M.compare_registration(MANIFEST, _live(events=events, permissions=permissions))
    assert not _ok(results)
    failed = [name for name, passed, _ in results if not passed]
    assert "live registration does not subscribe to Issues webhooks" in failed
    assert "live registration does not request Issues permission" in failed


def main() -> int:
    tests = [
        test_matching_live_registration_passes,
        test_missing_check_suite_event_fails,
        test_missing_checks_write_permission_fails,
        test_extra_issues_surface_fails,
    ]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print("APP REGISTRATION CONTRACT TESTS: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
