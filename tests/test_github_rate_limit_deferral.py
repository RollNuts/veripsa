#!/usr/bin/env python3
"""GitHub rate-limit waits become attempt-neutral durable scheduling in events."""
from __future__ import annotations

import io
import os
import sys
import time
import urllib.error
from datetime import datetime, timezone


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import delivery_deferral as DD  # noqa: E402
import event_budget  # noqa: E402
import github_rest as GR  # noqa: E402
from delivery_queue import DeliveryStore, with_delivery_key  # noqa: E402
from event_queue import EventQueue  # noqa: E402


class _Resp:
    def __init__(self, body=b'{"ok":1}', headers=None):
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self._body


def _http(code, headers=None):
    return urllib.error.HTTPError(
        "https://api.github.com/x",
        code,
        "limited",
        headers or {},
        io.BytesIO(b""),
    )


def _client(script):
    client = GR.GitHubREST("app", "key", "installation")
    steps = list(script)
    calls = []
    waits = []

    def _urlopen(request):
        calls.append(request.full_url)
        step = steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step() if callable(step) else step

    client._urlopen = _urlopen
    client._sleep = lambda seconds: waits.append(float(seconds))
    return client, calls, waits


def _within(value, low, high):
    return low <= value <= high


def main() -> int:
    checks = []

    # Retry-After:60 used to park the worker, then multiply across GitHub and
    # EventQueue retries. It now returns a typed durable schedule immediately.
    client, calls, waits = _client([_http(429, {"Retry-After": "60"})])
    token = event_budget.begin(2.0)
    started = time.monotonic()
    wall_started = datetime.now(timezone.utc)
    deferred = None
    try:
        client._req("GET", "/limited", "token")
    except DD.IntentionalDeliveryDeferral as exc:
        deferred = exc
    finally:
        elapsed = time.monotonic() - started
        event_budget.end(token)
    delay = (
        (deferred.not_before - wall_started).total_seconds()
        if deferred is not None else -1
    )
    checks.append((
        "event Retry-After:60 raises typed deferral in <200ms without sleep "
        f"(elapsed={elapsed:.4f}s, delay={delay:.2f}s, calls={len(calls)}, waits={waits})",
        isinstance(deferred, DD.IntentionalDeliveryDeferral)
        and elapsed < 0.2
        and _within(delay, 59.0, 61.0)
        and deferred.not_before.tzinfo is not None
        and deferred.not_before.utcoffset() is not None
        and deferred.reason == DD.GITHUB_RATE_LIMIT_REASON
        and "60" not in deferred.reason
        and len(calls) == 1
        and waits == [],
    ))

    # The Link-header request path must use the same primary-limit schedule.
    reset_epoch = time.time() + 120.0
    link, link_calls, link_waits = _client([
        _http(403, {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(reset_epoch),
        }),
    ])
    token = event_budget.begin(2.0)
    link_wall = datetime.now(timezone.utc)
    link_deferred = None
    try:
        link._req_with_link("GET", "/limited-link", "token")
    except DD.IntentionalDeliveryDeferral as exc:
        link_deferred = exc
    finally:
        event_budget.end(token)
    link_delay = (
        (link_deferred.not_before - link_wall).total_seconds()
        if link_deferred is not None else -1
    )
    checks.append((
        "_req_with_link primary reset becomes a timezone-aware durable schedule "
        f"(delay={link_delay:.2f}s, calls={len(link_calls)}, waits={link_waits})",
        isinstance(link_deferred, DD.IntentionalDeliveryDeferral)
        and _within(link_delay, 118.0, 121.0)
        and len(link_calls) == 1
        and link_waits == [],
    ))

    # Malformed/non-finite/negative headers never become NaN datetimes or
    # unbounded delays. Malformed values use a safe one-minute fallback; huge
    # finite values are capped at one hour.
    fixed_now = datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc)
    malformed = [
        DD.github_rate_limit_deferral(
            _http(429, {"Retry-After": value}),
            now=fixed_now,
        )
        for value in ("nan", "not-a-delay", "-4")
    ]
    malformed.append(DD.github_rate_limit_deferral(
        _http(403, {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "-4",
        }),
        now=fixed_now,
    ))
    capped = DD.github_rate_limit_deferral(
        _http(429, {"Retry-After": "999999999"}),
        now=fixed_now,
    )
    http_date = DD.github_rate_limit_deferral(
        _http(429, {
            "Retry-After": "Tue, 28 Jul 2026 00:03:00 GMT",
        }),
        now=fixed_now,
    )
    malformed_delays = [
        (item.not_before - fixed_now).total_seconds() if item is not None else -1
        for item in malformed
    ]
    capped_delay = (capped.not_before - fixed_now).total_seconds() if capped else -1
    date_delay = (http_date.not_before - fixed_now).total_seconds() if http_date else -1
    checks.append((
        "rate-limit parser safely falls back, caps at one hour, and supports HTTP-date "
        f"(malformed={malformed_delays}, capped={capped_delay}, date={date_delay})",
        malformed_delays == [60.0, 60.0, 60.0, 60.0]
        and capped_delay == DD.GITHUB_RATE_LIMIT_MAX_SECONDS
        and date_delay == 180.0,
    ))

    # A zero wait is genuinely retryable now; do not churn a durable row for it.
    zero, zero_calls, zero_waits = _client([
        _http(429, {"Retry-After": "0"}),
        _Resp(),
    ])
    token = event_budget.begin(2.0)
    try:
        zero_result = zero._req("GET", "/zero", "token")
    finally:
        event_budget.end(token)
    checks.append((
        "Retry-After:0 keeps the immediate bounded retry path "
        f"(calls={len(zero_calls)}, waits={zero_waits}, result={zero_result})",
        zero_result == {"ok": 1}
        and len(zero_calls) == 2
        and zero_waits == [0.0],
    ))

    # Outside EventQueue context, boot/watchdog/CLI callers retain the exact
    # historical bounded retry/sleep behavior.
    background, background_calls, background_waits = _client([
        _http(429, {"Retry-After": "2"}),
        _Resp(),
    ])
    background_result = background._req("GET", "/background", "token")
    checks.append((
        "background GitHub calls retain existing bounded wait+retry semantics "
        f"(calls={len(background_calls)}, waits={background_waits})",
        background_result == {"ok": 1}
        and len(background_calls) == 2
        and background_waits == [2.0],
    ))

    # A permission 403 often carries ordinary quota metadata. Without an
    # explicit Retry-After or remaining=0 it must remain the original error,
    # not become a delayed durable row.
    permission, permission_calls, permission_waits = _client([
        _http(403, {
            "X-RateLimit-Remaining": "4999",
            "X-RateLimit-Reset": str(time.time() + 300),
        }),
    ])
    token = event_budget.begin(2.0)
    permission_error = None
    try:
        permission._req("GET", "/forbidden", "token")
    except urllib.error.HTTPError as exc:
        permission_error = exc
    finally:
        event_budget.end(token)
    checks.append((
        "ordinary event permission 403 remains immediate and is not misclassified as rate-limit deferral "
        f"(calls={len(permission_calls)}, waits={permission_waits})",
        isinstance(permission_error, urllib.error.HTTPError)
        and permission_error.code == 403
        and len(permission_calls) == 1
        and permission_waits == [],
    ))

    # A response arriving after the event deadline is cancellation, not a
    # lower-priority deferral.
    late, _, _ = _client([])

    def _late_limit(_request):
        time.sleep(0.08)
        raise _http(429, {"Retry-After": "60"})

    late._urlopen = _late_limit
    token = event_budget.begin(0.08)
    late_error = None
    try:
        late._req("GET", "/late", "token")
    except BaseException as exc:
        late_error = exc
    finally:
        event_budget.end(token)
    checks.append((
        "expired event cancellation outranks rate-limit scheduling "
        f"(exc={type(late_error).__name__ if late_error else None})",
        isinstance(late_error, event_budget.EventBudgetExceeded)
        and not isinstance(late_error, DD.IntentionalDeliveryDeferral),
    ))

    # Reactive 401 still follows expected-token invalidation/remint; only
    # genuine rate-limit responses are scheduled.
    auth, auth_calls, auth_waits = _client([_http(401), _Resp()])
    auth._token = "T0"
    auth._token_exp = time.time() + 3600
    auth_mints = {"n": 0}

    def _mint():
        auth_mints["n"] += 1
        auth._token = "T1"
        auth._token_exp = time.time() + 3600
        return auth._token

    auth._mint_installation_token = _mint
    token = event_budget.begin(2.0)
    try:
        auth_result = auth._api("GET", "/auth")
    finally:
        event_budget.end(token)
    checks.append((
        "event 401 retains one conditional invalidate/remint retry "
        f"(calls={len(auth_calls)}, mints={auth_mints['n']}, waits={auth_waits})",
        auth_result == {"ok": 1}
        and len(auth_calls) == 2
        and auth_mints["n"] == 1
        and auth._token == "T1"
        and auth_waits == [],
    ))

    # End-to-end through the existing DeliveryStore wrapper and EventQueue:
    # first tenant is scheduled durably without spending an attempt; the
    # second tenant runs immediately on the same single worker.
    integration_gh = GR.GitHubREST("app", "key", "integration")
    integration_calls = []

    def _integration_urlopen(request):
        integration_calls.append(request.full_url)
        if request.full_url.endswith("/limited"):
            raise _http(429, {"Retry-After": "60"})
        return _Resp()

    integration_gh._urlopen = _integration_urlopen
    integration_gh._sleep = lambda seconds: (_ for _ in ()).throw(
        AssertionError(f"event worker must not sleep for rate limit: {seconds}"))

    payloads = {
        "rate-limited-delivery": {
            "installation": {"account": {"id": "tenant-a"}},
            "repository": {"id": 101, "full_name": "a/limited"},
        },
        "healthy-delivery": {
            "installation": {"account": {"id": "tenant-b"}},
            "repository": {"id": 202, "full_name": "b/healthy"},
        },
    }
    durable = DeliveryStore("postgresql://unused")
    durable_state = {
        key: {"status": "queued", "attempts": 0, "not_before": None}
        for key in payloads
    }
    deferred_calls = []
    finished = []
    released = []
    lease_counter = {"n": 0}

    def _claim(key):
        lease_counter["n"] += 1
        durable_state[key]["status"] = "processing"
        durable_state[key]["attempts"] += 1
        return {
            "claimed": True,
            "lease_generation": lease_counter["n"],
            "event_type": "pull_request",
            "payload": payloads[key],
        }

    def _defer(key, not_before, reason, lease_generation):
        deferred_calls.append((key, not_before, reason, lease_generation))
        durable_state[key]["status"] = "queued"
        durable_state[key]["attempts"] -= 1
        durable_state[key]["not_before"] = not_before
        return True

    def _finish(key, lease_generation):
        finished.append((key, lease_generation))
        durable_state[key]["status"] = "done"
        return True

    durable.claim = _claim
    durable.defer = _defer
    durable.finish = _finish
    durable.release = lambda key, error, lease: released.append((key, error, lease))
    healthy_started = []

    def _processor(_event_type, payload, _db, gh, coalesce=None):
        key = payload["_veripsa_delivery_key"]
        if key == "rate-limited-delivery":
            return gh._req("GET", "/limited", "token")
        healthy_started.append(time.monotonic())
        return gh._req("GET", "/healthy", "token")

    wrapped = durable.wrap_processor(_processor)
    worker = EventQueue(
        None,
        integration_gh,
        wrapped,
        account_of=lambda payload: str(
            payload.get("installation", {}).get("account", {}).get("id", "")),
        repo_of=lambda payload: payload.get("repository", {}).get("full_name"),
        worker_count=1,
        retry_attempts=3,
        retry_base_seconds=1,
    )
    worker.submit(
        "pull_request",
        with_delivery_key(payloads["rate-limited-delivery"], "rate-limited-delivery"),
        "rate-limited-delivery",
    )
    worker.submit(
        "pull_request",
        with_delivery_key(payloads["healthy-delivery"], "healthy-delivery"),
        "healthy-delivery",
    )
    integration_started = time.monotonic()
    worker.start()
    drained = worker.wait_idle(1.0)
    healthy_latency = (
        healthy_started[0] - integration_started
        if healthy_started else 999.0
    )
    scheduled = durable_state["rate-limited-delivery"]
    scheduled_delay = (
        (scheduled["not_before"] - datetime.now(timezone.utc)).total_seconds()
        if scheduled["not_before"] is not None else -1
    )
    checks.append((
        "DeliveryStore+EventQueue: rate-limited row is queued attempt-neutral and another tenant runs immediately "
        f"(drained={drained}, latency={healthy_latency:.4f}s, state={scheduled}, "
        f"failed={worker.failed()}, retried={worker.retried()}, processed={worker.processed()}, "
        f"claim_deferred={worker.claim_deferred()})",
        drained
        and healthy_latency < 0.2
        and scheduled["status"] == "queued"
        and scheduled["attempts"] == 0
        and _within(scheduled_delay, 58.0, 61.0)
        and len(deferred_calls) == 1
        and deferred_calls[0][0] == "rate-limited-delivery"
        and deferred_calls[0][2] == DD.GITHUB_RATE_LIMIT_REASON
        and released == []
        and [key for key, _lease in finished] == ["healthy-delivery"]
        and worker.failed() == 0
        and worker.retried() == 0
        and worker.processed() == 1
        and worker.claim_deferred() == 1
        and worker.delivery_outcome("rate-limited-delivery") is None
        and integration_calls.count("https://api.github.com/limited") == 1
        and integration_calls.count("https://api.github.com/healthy") == 1,
    ))

    ok = True
    for name, condition in checks:
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
        ok = ok and bool(condition)
    print("GITHUB RATE LIMIT DEFERRAL GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
