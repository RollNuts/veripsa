#!/usr/bin/env python3
"""A stuck alert webhook must never block or multiply event-worker threads."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
import sys
import threading
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import alerts  # noqa: E402


started = threading.Event()
release = threading.Event()


def stuck_poster(_url, _body):
    started.set()
    release.wait(2.0)


dispatcher = alerts._BoundedWebhookDispatcher(stuck_poster, max_pending=1)
assert dispatcher.submit("https://alert.invalid/one", {"key": "one"})
assert started.wait(0.5)
assert dispatcher.submit("https://alert.invalid/two", {"key": "two"})

before = time.monotonic()
accepted = dispatcher.submit("https://alert.invalid/three", {"key": "three"})
elapsed = time.monotonic() - before

assert not accepted, "a saturated alert sink must drop instead of growing"
assert elapsed < 0.05, f"alert enqueue blocked for {elapsed:.3f}s"
assert dispatcher.dropped() == 1
assert dispatcher._thread is not None and dispatcher._thread.is_alive()
assert len([t for t in threading.enumerate() if t.name == "veripsa-alert-webhook"]) == 1

release.set()
dispatcher._queue.join()

# An incoming-webhook URL is the credential. urllib's malformed-URL error and
# an injected poster may both echo their complete URL; neither may reach logs.
secret_url = "hooks.slack.invalid/services/T000/B000/SECRET-MUST-NOT-LOG"
captured = io.StringIO()
with redirect_stdout(captured):
    alerts._http_post_once(secret_url, {"key": "malformed"})
malformed_log = captured.getvalue()
assert "SECRET-MUST-NOT-LOG" not in malformed_log
assert "T000" not in malformed_log and "B000" not in malformed_log
assert "ValueError" in malformed_log


def echoing_failure(url, _body):
    raise ValueError(f"poster rejected credential {url}")


safe_dispatcher = alerts._BoundedWebhookDispatcher(echoing_failure, max_pending=2)
captured = io.StringIO()
with redirect_stdout(captured):
    assert safe_dispatcher.submit(secret_url, {"key": "one"})
    assert safe_dispatcher.submit(secret_url, {"key": "two"})
    safe_dispatcher._queue.join()
safe_log = captured.getvalue()
assert "SECRET-MUST-NOT-LOG" not in safe_log
assert "T000" not in safe_log and "B000" not in safe_log
assert safe_log.count("ValueError") == 2
assert safe_dispatcher._thread is not None and safe_dispatcher._thread.is_alive()

# The synchronous injected-poster and durable-board seams have the same
# fail-open contract and must not stringify a credential-bearing exception.
captured = io.StringIO()


def echoing_persist(_action, _key, _level, _message, _fields):
    raise RuntimeError(
        "board failed at postgresql://user:PASSWORD-MUST-NOT-LOG@db/private"
    )


with redirect_stdout(captured):
    poster_sink = alerts.AlertSink(
        webhook_url=secret_url,
        min_interval=0,
        poster=echoing_failure,
    )
    assert not poster_sink.fire("sink_secret_test", "warning", "safe")
    board_sink = alerts.AlertSink(
        webhook_url=secret_url,
        min_interval=0,
        poster=lambda _url, _body: None,
        persist=echoing_persist,
    )
    assert board_sink.fire("board_secret_test", "warning", "safe")
    board_sink.resolve("board_secret_test")
seam_log = captured.getvalue()
for secret_part in (
    "SECRET-MUST-NOT-LOG",
    "T000",
    "B000",
    "PASSWORD-MUST-NOT-LOG",
    "user:",
):
    assert secret_part not in seam_log
assert "ValueError" in seam_log and "RuntimeError" in seam_log

print("alert dispatch liveness gate: PASS")
