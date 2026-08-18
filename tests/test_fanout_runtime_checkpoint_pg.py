#!/usr/bin/env python3
"""Real-Postgres runtime proof for fanout checkpoint/defer/finalize wiring."""
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
import event_budget  # noqa: E402
import event_processor as EP  # noqa: E402


DB = "veripsa_fanoutruntime_" + str(os.getpid())
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


def delivery_state(key: str):
    return execute(
        MIGRATOR_DSN,
        "SELECT jsonb_build_object("
        "'status',status,'attempts',attempts,'generation',lease_generation,"
        "'payload',payload,'not_before',not_before) "
        "FROM core.webhook_delivery WHERE delivery_key=%s",
        (key,),
        fetch=True,
    )


def probe(key: str):
    rows = execute(
        MIGRATOR_DSN,
        "SELECT COALESCE(jsonb_object_agg(repo_key,runs),'{}'::jsonb) "
        "FROM core.fanout_runtime_probe WHERE delivery_key=%s",
        (key,),
        fetch=True,
    )
    return rows or {}


class FakeServer:
    _ALLREPOS_DISCOVERY_MARKER = "_veripsa_allrepos_discovery_state"
    _ACTIVATION_PROOF_MARKER = "_veripsa_activation_installation_proof"
    _STALE_UNINSTALL_MARKER = "_veripsa_stale_uninstall"
    _SUSPEND_PROOF_MARKER = "_veripsa_suspend_proof"

    def __init__(self):
        self.calls = []
        self.cancel_repo = None
        self.cancelled = False

    @staticmethod
    def _as_obj(value):
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _as_list(value):
        return value if isinstance(value, list) else []

    @staticmethod
    def _activation_installation_proof(_gh, _payload):
        return {
            "installation_id": "900",
            "account_id": "acct",
            "created_at": "2026-07-28T00:00:00+00:00",
            "suspended": False,
        }

    @staticmethod
    def _scoped_db(conn):
        def run(sql, args=()):
            with conn.cursor() as cur:
                cur.execute(sql, args)
                if cur.description:
                    rows = cur.fetchall()
                    return [
                        dict(zip([col.name for col in cur.description], row))
                        for row in rows
                    ]
            return None
        return run

    def handle_event(self, event_type, body, db, _gh, coalesce=None):
        field = EP._INSTALL_FANOUT_FIELD[(event_type, body["action"])]
        repo = body[field][0]["full_name"]
        self.calls.append(repo)
        if repo == self.cancel_repo and not self.cancelled:
            self.cancelled = True
            raise event_budget.EventBudgetExceeded(
                "synthetic typed sibling cancellation")
        db(
            "INSERT INTO core.fanout_runtime_probe("
            "delivery_key,repo_key,runs) VALUES(%s,%s,1) "
            "ON CONFLICT(delivery_key,repo_key) DO UPDATE SET "
            "runs=core.fanout_runtime_probe.runs+1",
            (body["_veripsa_delivery_key"], repo),
        )
        return {"repo": repo}


class Inventory:
    def __init__(self, entries):
        self.entries = entries
        self.calls = 0

    def installation_repo_entries(self, cap):
        self.calls += 1
        return list(self.entries[:cap])


class CommitProxy:
    """Commit server-side once, then lose only the client acknowledgement."""

    def __init__(self, inner):
        self.inner = inner
        self.error = RuntimeError("simulated fanout COMMIT ACK loss")
        self.commit_calls = 0

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


def install_payload(entries=None, *, all_repositories=False):
    body = {
        "action": "created",
        "installation": {
            "id": 900,
            "account": {
                "id": "acct",
                "login": "acme",
                "type": "Organization",
            },
        },
        "sender": {"id": 1, "login": "octo", "type": "User"},
    }
    if entries is not None:
        body["repositories"] = [
            {"id": entry["id"], "full_name": entry["full_name"]}
            for entry in entries
        ]
    if all_repositories:
        body["repository_selection"] = "all"
    return body


def repo_entries(count, prefix):
    return [
        {"id": index, "full_name": f"acme/{prefix}-{index}"}
        for index in range(1, count + 1)
    ]


def run_job(wrapped, submitted, gh):
    return wrapped(
        submitted["event_type"], submitted["payload"], None, gh)


def main() -> int:
    print("=== FANOUT RUNTIME CHECKPOINT POSTGRES GATE ===")
    bootstrap = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if bootstrap.returncode != 0:
        print((bootstrap.stderr or bootstrap.stdout)[-1800:])
        return 1

    fake = FakeServer()
    original_server = EP._server
    original_take = EP._take_live_repository_locks
    original_release = EP._release_repo_lock
    original_connect = EP._connect_event_db
    original_retry = EP._FANOUT_SLICE_RETRY_SECONDS
    original_slice = EP._FANOUT_REPOS_PER_SLICE
    try:
        execute(
            MIGRATOR_DSN,
            "CREATE TABLE core.fanout_runtime_probe("
            "delivery_key text NOT NULL,repo_key text NOT NULL,runs int NOT NULL,"
            "PRIMARY KEY(delivery_key,repo_key));"
            "GRANT SELECT,INSERT,UPDATE ON core.fanout_runtime_probe "
            "TO veripsa_app",
        )
        EP._server = lambda: fake
        EP._take_live_repository_locks = (
            lambda *_args, **_kwargs: True)
        EP._release_repo_lock = lambda *_args, **_kwargs: True
        EP._FANOUT_SLICE_RETRY_SECONDS = 0
        EP._FANOUT_REPOS_PER_SLICE = 4

        store = DQ.DeliveryStore(APP_DSN)
        separate_finishes = []
        real_finish = store.finish

        def observed_finish(key, generation):
            separate_finishes.append((key, generation))
            return real_finish(key, generation)

        store.finish = observed_finish
        wrapped = store.wrap_processor(EP.make_db_processor(APP_DSN))

        # All-repositories discovery is frozen once. A commits, B is typed-
        # cancelled, and the exact partial state is queued with attempts
        # restored. Retry skips A and never asks GitHub inventory again.
        all_entries = repo_entries(2, "inventory")
        inventory = Inventory(all_entries)
        fake.cancel_repo = all_entries[1]["full_name"]
        partial_submit = store.submit(
            "installation",
            install_payload(all_repositories=True),
            "pg-fanout-partial",
            account_key="acct",
        )
        first = run_job(wrapped, partial_submit, inventory)
        first_state = delivery_state("pg-fanout-partial")
        check(
            first.get(DQ._WORKER_CLAIM_OUTCOME) == "deferred"
            and first_state["status"] == "queued"
            and first_state["attempts"] == 0
            and first_state["payload"]["_veripsa_fanout_completed"]
            == {"id:1": True}
            and probe("pg-fanout-partial")
            == {all_entries[0]["full_name"]: 1},
            "real A commit + B typed cancellation atomically checkpoints and defers without an attempt",
        )
        second = run_job(wrapped, partial_submit, inventory)
        second_state = delivery_state("pg-fanout-partial")
        check(
            second is None
            and second_state["status"] == "done"
            and second_state["attempts"] == 1
            and inventory.calls == 1
            and probe("pg-fanout-partial") == {
                all_entries[0]["full_name"]: 1,
                all_entries[1]["full_name"]: 1,
            }
            and fake.calls.count(all_entries[0]["full_name"]) == 1,
            "real retry restores immutable plan, skips A, avoids inventory, and finishes B once",
        )

        # Lost ACK on the slice commit: the DB is already queued with four
        # checkpoints. DeliveryStore reuses the exact timestamp/reason and
        # classifies one durable deferral, never release/failure.
        fake.cancel_repo = None
        slice_entries = repo_entries(5, "slice")
        slice_submit = store.submit(
            "installation",
            install_payload(slice_entries),
            "pg-fanout-slice-ack",
            account_key="acct",
        )
        connection_count = [0]
        slice_proxy = [None]

        def slice_connect(dsn, timeout):
            connection_count[0] += 1
            inner = original_connect(dsn, timeout)
            if connection_count[0] == 5:
                slice_proxy[0] = CommitProxy(inner)
                return slice_proxy[0]
            return inner

        EP._connect_event_db = slice_connect
        slice_first = run_job(wrapped, slice_submit, object())
        EP._connect_event_db = original_connect
        slice_state = delivery_state("pg-fanout-slice-ack")
        check(
            slice_first.get(DQ._WORKER_CLAIM_OUTCOME) == "deferred"
            and slice_proxy[0] is not None
            and slice_proxy[0].commit_calls == 1
            and slice_state["status"] == "queued"
            and slice_state["attempts"] == 0
            and len(slice_state["payload"]["_veripsa_fanout_completed"]) == 4
            and len(probe("pg-fanout-slice-ack")) == 4,
            "real slice COMMIT ACK loss resolves the exact queued checkpoint set attempt-neutrally",
        )
        slice_second = run_job(wrapped, slice_submit, object())
        check(
            slice_second is None
            and delivery_state("pg-fanout-slice-ack")["status"] == "done"
            and probe("pg-fanout-slice-ack")
            == {entry["full_name"]: 1 for entry in slice_entries},
            "real slice retry skips four successes and executes the last repository once",
        )

        # Last repository body+checkpoint+done commit lands, then its ACK
        # disappears. The normal exact commit resolver proves done.
        last_entries = repo_entries(1, "last")
        last_submit = store.submit(
            "installation",
            install_payload(last_entries),
            "pg-fanout-last-ack",
            account_key="acct",
        )
        connection_count = [0]
        last_proxy = [None]

        def last_connect(dsn, timeout):
            connection_count[0] += 1
            inner = original_connect(dsn, timeout)
            if connection_count[0] == 2:
                last_proxy[0] = CommitProxy(inner)
                return last_proxy[0]
            return inner

        EP._connect_event_db = last_connect
        last_result = run_job(wrapped, last_submit, object())
        EP._connect_event_db = original_connect
        check(
            last_result is None
            and last_proxy[0] is not None
            and last_proxy[0].commit_calls == 1
            and delivery_state("pg-fanout-last-ack")["status"] == "done"
            and probe("pg-fanout-last-ack")
            == {last_entries[0]["full_name"]: 1}
            and separate_finishes == [],
            "real last-repo ACK loss resolves done; handler and durable finish each occur once",
        )
    finally:
        EP._FANOUT_REPOS_PER_SLICE = original_slice
        EP._FANOUT_SLICE_RETRY_SECONDS = original_retry
        EP._connect_event_db = original_connect
        EP._release_repo_lock = original_release
        EP._take_live_repository_locks = original_take
        EP._server = original_server
        subprocess.run(["dropdb", DB], capture_output=True, text=True)

    print(
        "FANOUT RUNTIME CHECKPOINT POSTGRES GATE:",
        "PASS" if FAIL == 0 else "FAIL",
    )
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
