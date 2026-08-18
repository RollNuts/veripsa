#!/usr/bin/env python3
"""Offline gate for generation-21 account-convergence observability.

No Postgres and no network are required. The gate proves the pure alert
semantics plus the live watchdog wiring:

* retry exhaustion is independently critical;
* 120s unfinished age is warning, 300s is critical, with only one latency key active;
* quota deferral alone is normal;
* malformed/query-failed samples are Unknown and never falsely resolve;
* every DB-healthy tick performs one global aggregate query, without a
  PolicyRefreshStore or tenant/repository scan.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import alerts  # noqa: E402
import health_watchdog as watchdog  # noqa: E402


CONVERGENCE_KEYS = {
    "account_convergence_retry_exhausted",
    "account_convergence_latency_warning",
    "account_convergence_latency_critical",
}


class RecordingSink:
    def __init__(self):
        self.events: list[tuple[str, str, str | None, dict | None]] = []

    def fire(self, key, level, message, fields=None, **_kwargs):
        self.events.append(("fire", key, level, fields))
        return True

    def resolve(self, key):
        self.events.append(("resolve", key, None, None))

    def convergence_events(self):
        return [event for event in self.events if event[1] in CONVERGENCE_KEYS]


class FakeWorker:
    def is_alive(self):
        return True

    def inflight_age(self):
        return None

    def retried(self):
        return 0

    def qsize(self):
        return 0

    def maxsize(self):
        return 1000

    def processed(self):
        return 1

    def failed(self):
        return 0

    def uptime(self):
        return 10.0


def surface(*, pending=0, claimed=0, retry_exhausted=0,
            quota_deferred=0, due_accounts=0, stalled_accounts=0, age=0):
    return {
        "pending": pending,
        "claimed": claimed,
        "retry_exhausted": retry_exhausted,
        "quota_deferred": quota_deferred,
        "due_accounts": due_accounts,
        "stalled_accounts": stalled_accounts,
        "oldest_age_seconds": age,
    }


def main() -> int:
    checks: list[tuple[str, bool]] = []

    def check(name: str, condition) -> None:
        ok = bool(condition)
        checks.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    quota_sink = RecordingSink()
    alerts.evaluate_account_convergence_depth(
        quota_sink,
        surface(quota_deferred=11),
    )
    check(
        "quota-deferred work alone is explicitly non-error",
        not any(event[0] == "fire" for event in quota_sink.convergence_events()),
    )

    warning_sink = RecordingSink()
    alerts.evaluate_account_convergence_depth(
        warning_sink,
        surface(pending=11, due_accounts=3, stalled_accounts=3, age=120),
    )
    warning_fires = [
        event for event in warning_sink.convergence_events()
        if event[0] == "fire"
    ]
    check(
        "oldest unfinished age 120s fires the stable warning key only",
        [(event[1], event[2]) for event in warning_fires]
        == [("account_convergence_latency_warning", "warning")],
    )

    warning_sink.events.clear()
    alerts.evaluate_account_convergence_depth(
        warning_sink,
        surface(pending=11, due_accounts=3, stalled_accounts=3, age=300),
    )
    critical_events = warning_sink.convergence_events()
    check(
        "oldest unfinished age 300s resolves warning and fires the distinct critical key",
        ("resolve", "account_convergence_latency_warning", None, None)
        in critical_events
        and any(
            event[0:3]
            == ("fire", "account_convergence_latency_critical", "critical")
            for event in critical_events
        )
        and not any(
            event[0] == "fire"
            and event[1] == "account_convergence_latency_warning"
            for event in critical_events
        ),
    )

    exhausted_sink = RecordingSink()
    alerts.evaluate_account_convergence_depth(
        exhausted_sink,
        surface(retry_exhausted=1),
    )
    check(
        "any retry-exhausted convergence row is critical",
        any(
            event[0:3]
            == ("fire", "account_convergence_retry_exhausted", "critical")
            for event in exhausted_sink.convergence_events()
        ),
    )

    # A bad observation must not clear either standing condition. Partial,
    # wrong-typed, impossible, and missing samples all remain Unknown.
    active_sink = RecordingSink()
    alerts.evaluate_account_convergence_depth(
        active_sink,
        surface(pending=3, retry_exhausted=1, due_accounts=1, stalled_accounts=1, age=301),
    )
    malformed_samples = (
        None,
        {},
        surface(pending=3, retry_exhausted=1, due_accounts=1, stalled_accounts=1, age=-1),
        surface(pending=3, retry_exhausted=1, due_accounts=1, stalled_accounts=1, age=301)
        | {"due_accounts": "1"},
        surface(pending=-1, retry_exhausted=1),
        surface(pending=3, due_accounts=0, age=301),
    )
    active_sink.events.clear()
    for malformed in malformed_samples:
        alerts.evaluate_account_convergence_depth(active_sink, malformed)
    check(
        "malformed/partial/impossible samples are Unknown with no false resolve",
        active_sink.convergence_events() == [],
    )

    # Prove live wiring uses the existing query callable once per healthy tick.
    # The function result is returned as JSON text to exercise that adapter
    # shape as well as the evaluator.
    queries: list[str] = []

    def healthy_db(sql, args=()):
        queries.append(sql)
        if sql == "SELECT 1":
            return 1
        if "account_convergence_depth_with_authority" in sql:
            return json.dumps(
                surface(pending=11, due_accounts=2, stalled_accounts=2, age=121)
            )
        if "db_usage_surface" in sql:
            return None
        raise AssertionError(f"unexpected query: {sql}")

    tick_sink = RecordingSink()
    watchdog.watchdog_tick(
        tick_sink,
        FakeWorker(),
        healthy_db,
        prev_failed=0,
    )
    watchdog.watchdog_tick(
        tick_sink,
        FakeWorker(),
        healthy_db,
        prev_failed=0,
    )
    convergence_queries = [
        query for query in queries
        if "account_convergence_depth_with_authority" in query
    ]
    check(
        "every DB-healthy tick samples one global convergence aggregate",
        len(convergence_queries) == 2
        and all("installation_account" not in query for query in convergence_queries),
    )
    check(
        "watchdog wiring fires the 120s warning without PolicyRefreshStore",
        any(
            event[0:3]
            == ("fire", "account_convergence_latency_warning", "warning")
            for event in tick_sink.convergence_events()
        ),
    )

    # Seed active alerts, then make only the convergence aggregate fail. The
    # tick remains fail-open and must emit no convergence resolve.
    failed_sink = RecordingSink()
    alerts.evaluate_account_convergence_depth(
        failed_sink,
        surface(pending=2, retry_exhausted=1, due_accounts=1, stalled_accounts=1, age=301),
    )
    failed_sink.events.clear()
    failed_queries: list[str] = []

    def failed_sample_db(sql, args=()):
        failed_queries.append(sql)
        if sql == "SELECT 1":
            return 1
        if "account_convergence_depth_with_authority" in sql:
            raise RuntimeError("sample unavailable")
        if "db_usage_surface" in sql:
            return None
        raise AssertionError(f"unexpected query: {sql}")

    watchdog.watchdog_tick(
        failed_sink,
        FakeWorker(),
        failed_sample_db,
        prev_failed=0,
    )
    check(
        "failed aggregate sample remains Unknown and never falsely resolves",
        failed_sink.convergence_events() == []
        and sum(
            "account_convergence_depth_with_authority" in query
            for query in failed_queries
        ) == 1,
    )

    db_down_queries: list[str] = []

    def db_down(sql, args=()):
        db_down_queries.append(sql)
        raise RuntimeError("database unavailable")

    watchdog.watchdog_tick(
        RecordingSink(),
        FakeWorker(),
        db_down,
        prev_failed=0,
    )
    check(
        "DB-unreachable tick does not attempt a second convergence query",
        db_down_queries == ["SELECT 1"],
    )

    ok = all(condition for _, condition in checks)
    print(
        "ACCOUNT CONVERGENCE OBSERVABILITY GATE:",
        "PASS" if ok else "FAIL",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
