#!/usr/bin/env python3
"""A work-budget expiry must release its durable lease inside the reserved tail.

Real Postgres gate: the first delivery consumes its normal work deadline after
claiming. DeliveryStore must use the terminalization reserve to move that exact
lease back to ``queued``; it must not remain ``processing`` for the 1800-second
stale window. An unrelated tenant starts within the same total wall ceiling, and
the released lane head plus its follower are immediately claimable in order.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import psycopg2


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
for path in (ROOT, APP):
    if path not in sys.path:
        sys.path.insert(0, path)

import delivery_queue as deliveries  # noqa: E402
import event_budget as budget  # noqa: E402
from event_queue import EventQueue  # noqa: E402


DB = f"veripsa_event_terminal_{os.getpid()}"
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"
OWNER_DSN = f"postgresql://veripsa_migrator@localhost/{DB}"
TOTAL_SECONDS = 5.2


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok:", message)


def payload(account: int, repo: str) -> dict:
    return {
        "repository": {
            "id": str(account * 100),
            "full_name": repo,
            "owner": {"id": account, "login": f"owner-{account}"},
        },
        "after": "a" * 40,
        "ref": "refs/heads/main",
    }


def durable_row(key: str):
    conn = psycopg2.connect(OWNER_DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status,locked_at,last_error FROM core.webhook_delivery "
                "WHERE delivery_key=%s",
                (key,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def main() -> None:
    boot = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if boot.returncode != 0:
        raise RuntimeError("bootstrap failed: " + boot.stderr[-800:])

    store = deliveries.DeliveryStore(APP_DSN)
    poison = store.submit(
        "push", payload(101, "slow/repo"), "terminal-poison",
        account_key="101", repo="slow/repo",
    )
    follower = store.submit(
        "push", payload(101, "slow/repo"), "terminal-follower",
        account_key="101", repo="slow/repo",
    )
    healthy = store.submit(
        "push", payload(202, "fast/repo"), "terminal-healthy",
        account_key="202", repo="fast/repo",
    )
    check(poison.get("accepted") and follower.get("accepted") and healthy.get("accepted"),
          "three durable deliveries are admitted")

    poison_started = threading.Event()
    healthy_started = []
    origin = time.monotonic()

    def processor(_event_type, event_payload, _db, _gh, **_kwargs):
        key = event_payload.get("_veripsa_delivery_key")
        if key == poison["delivery"]:
            poison_started.set()
            while True:
                remaining = budget.remaining()
                if remaining is None:
                    raise AssertionError("worker did not install an event budget")
                if remaining <= 0:
                    budget.raise_if_expired()
                time.sleep(min(0.02, remaining + 0.002))
        healthy_started.append(time.monotonic() - origin)
        return {"ok": True}

    original_begin = budget.begin
    try:
        budget.begin = lambda: original_begin(TOTAL_SECONDS)
        queue = EventQueue(
            None,
            None,
            store.wrap_processor(processor),
            account_of=lambda item: str(
                (item.get("repository") or {}).get("owner", {}).get("id", "")
            ),
            retry_attempts=1,
        ).start()
        check(queue.submit("push", poison["payload"], poison["delivery"]),
              "slow delivery enters the worker")
        check(poison_started.wait(1.0), "slow handler owns its durable lease")
        check(queue.submit("push", healthy["payload"], healthy["delivery"]),
              "unrelated tenant queues behind the slow handler")
        drained = queue.wait_idle(TOTAL_SECONDS + 2.0)
    finally:
        budget.begin = original_begin

    check(drained and queue.failed() == 1 and queue.processed() == 1,
          "work expiry fails once while the unrelated tenant still completes")
    check(healthy_started and healthy_started[0] <= TOTAL_SECONDS + 0.30,
          f"next tenant starts within the total event ceiling ({healthy_started})")

    poison_row = durable_row(poison["delivery"])
    healthy_row = durable_row(healthy["delivery"])
    check(poison_row and poison_row[0] == "queued" and poison_row[1] is None,
          f"expired work is terminalized to queued, not left processing ({poison_row})")
    check(healthy_row and healthy_row[0] == "done",
          f"unrelated successful delivery is finalized ({healthy_row})")

    # No 1800-second stale wait: the released head is reclaimable immediately.
    reclaimed = store.claim(poison["delivery"])
    check(reclaimed.get("claimed") is True,
          f"released lane head is immediately reclaimable ({reclaimed})")
    check(store.finish(poison["delivery"], reclaimed["lease_generation"]),
          "reclaimed lane head can finalize")
    follower_claim = store.claim(follower["delivery"])
    check(follower_claim.get("claimed") is True,
          f"same-lane follower advances immediately after its head ({follower_claim})")
    check(store.finish(follower["delivery"], follower_claim["lease_generation"]),
          "same-lane follower can finalize")

    print("EVENT TERMINALIZATION RESERVE GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
