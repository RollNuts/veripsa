#!/usr/bin/env python3
"""Real-Postgres proof for runtime one-transaction delivery finalization."""
from __future__ import annotations

import os
import subprocess
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
for path in (ROOT, APP):
    if path not in sys.path:
        sys.path.insert(0, path)

import psycopg2  # noqa: E402

import delivery_queue as DQ  # noqa: E402
import event_processor as EP  # noqa: E402
import server as S  # noqa: E402
from event_queue import EventQueue  # noqa: E402


DB = "veripsa_atomicfinalize_" + str(os.getpid())
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"
MIGRATOR_DSN = f"postgresql://veripsa_migrator@localhost/{DB}"
FAIL = 0


def check(condition, label: str) -> None:
    global FAIL
    print(("PASS: " if condition else "FAIL: ") + label)
    if not condition:
        FAIL += 1


def execute(dsn: str, sql: str, args=(), *, fetch=False):
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            if fetch:
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()
    return None


class CommitProxy:
    """Delegate a real connection while controlling only its explicit COMMIT ACK."""

    def __init__(self, inner, *, server_commits: bool):
        self.inner = inner
        self.server_commits = server_commits
        self.commit_calls = 0
        self.error = RuntimeError(
            "simulated lost COMMIT ACK"
            if server_commits
            else "simulated connection loss before COMMIT"
        )

    @property
    def autocommit(self):
        return self.inner.autocommit

    @autocommit.setter
    def autocommit(self, value):
        self.inner.autocommit = value

    def cursor(self, *args, **kwargs):
        return self.inner.cursor(*args, **kwargs)

    def commit(self):
        self.commit_calls += 1
        if self.server_commits:
            self.inner.commit()
        raise self.error

    def rollback(self):
        return self.inner.rollback()

    def close(self):
        return self.inner.close()

    def cancel(self):
        return self.inner.cancel()

    def fileno(self):
        return self.inner.fileno()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def payload(key: str) -> dict:
    return {
        "ref": "refs/heads/main",
        "after": ("a" if key.endswith("ack") else "b") * 40,
        "repository": {
            "id": 77007,
            "full_name": "acme/atomic-finalize",
            "owner": {"id": 7, "login": "acme", "type": "Organization"},
            "default_branch": "main",
        },
        "sender": {"id": 8, "login": "octo", "type": "User"},
        "commits": [],
    }


def state(key: str):
    conn = psycopg2.connect(MIGRATOR_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status,attempts,lease_generation,locked_at "
                "FROM core.webhook_delivery WHERE delivery_key=%s",
                (key,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def probe_runs(key: str) -> int:
    return int(execute(
        MIGRATOR_DSN,
        "SELECT COALESCE((SELECT runs FROM core.atomic_finalize_probe WHERE delivery_key=%s),0)",
        (key,),
        fetch=True,
    ))


def main() -> int:
    print("=== ATOMIC DELIVERY FINALIZE POSTGRES GATE ===")
    bootstrap = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if bootstrap.returncode != 0:
        print((bootstrap.stderr or bootstrap.stdout)[-1600:])
        return 1

    original_connect = EP._connect_event_db
    original_handle = S.handle_event
    proxies = []
    finish_calls = []
    try:
        execute(
            MIGRATOR_DSN,
            "CREATE TABLE core.atomic_finalize_probe("
            "delivery_key text PRIMARY KEY, runs int NOT NULL DEFAULT 0);"
            "GRANT SELECT,INSERT,UPDATE ON core.atomic_finalize_probe TO veripsa_app",
        )
        execute(
            APP_DSN,
            "SELECT core.enter_installation_with_authority(%s)",
            ("7",),
            fetch=True,
        )

        def probe_handler(_event_type, body, db, _gh, coalesce=None):
            assert DQ._DELIVERY_EXECUTION_AUTHORITY not in body
            db(
                "INSERT INTO core.atomic_finalize_probe(delivery_key,runs) VALUES(%s,1) "
                "ON CONFLICT(delivery_key) DO UPDATE SET runs="
                "core.atomic_finalize_probe.runs+1 RETURNING runs",
                (body["_veripsa_delivery_key"],),
            )
            return {"probe": True}

        S.handle_event = probe_handler
        commit_modes = [True, False]

        def controlled_connect(dsn, timeout):
            proxy = CommitProxy(
                original_connect(dsn, timeout),
                server_commits=commit_modes.pop(0),
            )
            proxies.append(proxy)
            return proxy

        EP._connect_event_db = controlled_connect

        store = DQ.DeliveryStore(APP_DSN)
        real_finish = store.finish

        def observed_separate_finish(key, generation):
            finish_calls.append((key, generation))
            return real_finish(key, generation)

        store.finish = observed_separate_finish
        wrapped = store.wrap_processor(EP.make_db_processor(APP_DSN))

        ack_submit = store.submit(
            "push", payload("runtime-ack"), "runtime-ack",
            account_key="7", repo="acme/atomic-finalize",
        )
        queue = EventQueue(
            None,
            object(),
            wrapped,
            account_of=lambda body: body["repository"]["owner"]["id"],
            repo_of=lambda body: body["repository"]["full_name"],
            retry_attempts=1,
            retry_base_seconds=0,
        ).start()
        accepted = queue.submit(
            ack_submit["event_type"], ack_submit["payload"], ack_submit["delivery"])
        drained = queue.wait_idle(5.0)
        ack_state = state("runtime-ack")
        check(
            accepted and drained
            and queue.processed() == 1
            and queue.failed() == 0
            and probe_runs("runtime-ack") == 1
            and ack_state[0] == "done"
            and ack_state[3] is None
            and proxies[0].commit_calls == 1
            and finish_calls == [],
            "real server-side body+finish commit with lost ACK resolves processed exactly once",
        )

        duplicate = wrapped(
            ack_submit["event_type"], ack_submit["payload"], None, object())
        check(
            duplicate.get(DQ._WORKER_CLAIM_OUTCOME) == "duplicate"
            and probe_runs("runtime-ack") == 1,
            "re-observing the same durable delivery does not execute the committed handler twice",
        )

        pre_submit = store.submit(
            "push", payload("runtime-pre"), "runtime-pre",
            account_key="7", repo="acme/atomic-finalize",
        )
        caught = None
        try:
            wrapped(
                pre_submit["event_type"], pre_submit["payload"], None, object())
        except RuntimeError as error:
            caught = error
        pre_state = state("runtime-pre")
        check(
            caught is proxies[1].error
            and probe_runs("runtime-pre") == 0
            and pre_state[0] == "queued"
            and pre_state[1] == 1
            and pre_state[3] is None
            and finish_calls == [],
            "real pre-COMMIT loss rolls body back and resolver returns the exact lease to queued",
        )
    finally:
        EP._connect_event_db = original_connect
        S.handle_event = original_handle
        subprocess.run(["dropdb", DB], capture_output=True, text=True)

    print(
        "ATOMIC DELIVERY FINALIZE POSTGRES GATE:",
        "PASS" if FAIL == 0 else "FAIL",
    )
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
