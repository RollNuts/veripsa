#!/usr/bin/env python3
"""One EventQueue dequeue must equal one durable execution generation."""
from __future__ import annotations

import os
import sys
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
for path in (ROOT, APP):
    if path not in sys.path:
        sys.path.insert(0, path)

from event_queue import EventQueue  # noqa: E402


def main() -> int:
    checks: list[tuple[str, bool]] = []

    bare_runs = []

    def bare(_event_type, payload, _db, _gh):
        bare_runs.append(payload["id"])
        if len(bare_runs) == 1:
            raise RuntimeError("transient in-memory failure")

    bare_queue = EventQueue(
        None,
        None,
        bare,
        retry_attempts=3,
        retry_base_seconds=0,
    ).start()
    bare_queue.submit("push", {"id": 1})
    bare_idle = bare_queue.wait_idle(2.0)
    checks.append((
        "bare processors retain one short in-memory retry",
        bare_idle
        and bare_runs == [1, 1]
        and bare_queue.retried() == 1
        and bare_queue.failed() == 0
        and bare_queue.processed() == 1,
    ))

    durable_runs = []

    def durable(_event_type, payload, _db, _gh):
        durable_runs.append(payload["id"])
        if len(durable_runs) == 1:
            raise RuntimeError("durable generation failed")

    durable._veripsa_one_durable_attempt_per_dequeue = True
    durable_queue = EventQueue(
        None,
        None,
        durable,
        retry_attempts=3,
        # A durable failure must not sleep this backoff or run again inline.
        retry_base_seconds=5,
    ).start()
    started = time.monotonic()
    durable_queue.submit(
        "push",
        {"id": 7, "_veripsa_delivery_key": "durable-generation"},
    )
    first_idle = durable_queue.wait_idle(2.0)
    first_elapsed = time.monotonic() - started
    checks.append((
        "one durable dequeue runs exactly one generation without inline backoff/reclaim",
        first_idle
        and durable_runs == [7]
        and durable_queue.retried() == 0
        and durable_queue.failed() == 1
        and durable_queue.processed() == 0
        and first_elapsed < 1.0,
    ))

    # This second submit models the durable recovery scheduler publishing a
    # fresh DB generation. It may run; the first dequeue itself may not mint it.
    durable_queue.submit(
        "push",
        {"id": 7, "_veripsa_delivery_key": "durable-generation"},
        register_push=False,
    )
    second_idle = durable_queue.wait_idle(2.0)
    checks.append((
        "a later scheduler generation can retry and succeed exactly once",
        second_idle
        and durable_runs == [7, 7]
        and durable_queue.retried() == 0
        and durable_queue.failed() == 1
        and durable_queue.processed() == 1,
    ))

    disabled_runs = []

    def durable_wrapper_but_raw(_event_type, payload, _db, _gh):
        disabled_runs.append(payload["id"])
        if len(disabled_runs) == 1:
            raise RuntimeError("durable inbox disabled: raw memory transient")

    durable_wrapper_but_raw._veripsa_one_durable_attempt_per_dequeue = True
    disabled_queue = EventQueue(
        None, None, durable_wrapper_but_raw,
        retry_attempts=3, retry_base_seconds=0,
    ).start()
    # A GitHub delivery id is only an HTTP receipt. Without the private durable
    # payload key there is no DB generation and the short memory retry remains
    # authoritative (for example the durable-inbox kill switch).
    disabled_queue.submit("push", {"id": 9}, "raw-github-delivery")
    disabled_idle = disabled_queue.wait_idle(2.0)
    checks.append((
        "a raw delivery id without the durable payload key retains in-memory retry",
        disabled_idle
        and disabled_runs == [9, 9]
        and disabled_queue.retried() == 1
        and disabled_queue.failed() == 0
        and disabled_queue.processed() == 1,
    ))

    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print(
        "DURABLE RETRY GENERATION BOUNDARY:",
        "PASS" if ok else "FAIL",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
