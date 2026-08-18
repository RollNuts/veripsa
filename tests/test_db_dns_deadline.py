#!/usr/bin/env python3
"""Database DNS/NSS and all address attempts share one hard absolute deadline."""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
for path in (ROOT, APP):
    if path not in sys.path:
        sys.path.insert(0, path)

import psycopg2  # noqa: E402
import bounded_resolver  # noqa: E402
import db_connect  # noqa: E402
import delivery_queue  # noqa: E402
import event_processor  # noqa: E402
import server_dbops  # noqa: E402


checks: list[tuple[str, bool]] = []


def check(label: str, condition) -> None:
    checks.append((label, bool(condition)))
    print(("  [PASS] " if condition else "  [FAIL] ") + label)


def addr(ip: str, port=5432):
    if ":" in ip:
        return (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


class FakeCursor:
    def __init__(self, result="ok"):
        self.result = result
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, args=()):
        self.sql.append((str(sql), args))

    def fetchone(self):
        return (self.result,)


class FakeConnection:
    def __init__(self, result="ok"):
        self.result = result
        self.autocommit = False
        self.closed = False
        self.cursors = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def cursor(self):
        cursor = FakeCursor(self.result)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


def main() -> int:
    # libc getaddrinfo ignores socket timeouts. A wedged resolver must consume
    # one fixed daemon worker, while this caller returns at its own deadline.
    blocker = threading.Event()
    entered = threading.Event()

    def stuck_getaddrinfo(*_args):
        entered.set()
        blocker.wait()
        return (addr("192.0.2.10"),)

    resolver = bounded_resolver.BoundedResolver(
        workers=1,
        max_pending=1,
        ttl_seconds=0,
        negative_ttl_seconds=0,
        resolver_fn=stuck_getaddrinfo,
        thread_name_prefix="veripsa-test-db-dns",
    )
    original_resolve = db_connect.resolve
    db_connect.resolve = resolver.resolve
    secret = "dns-secret-must-not-escape"
    started = time.monotonic()
    timeout_error = None
    connector_called = False
    try:
        def forbidden_connector(*_args, **_kwargs):
            nonlocal connector_called
            connector_called = True
            return FakeConnection()

        try:
            db_connect.connect(
                forbidden_connector,
                f"postgresql://app:{secret}@wedged.invalid/veripsa",
                deadline=time.monotonic() + 1.05,
                connect_timeout=10,
            )
        except Exception as error:
            timeout_error = error
        elapsed = time.monotonic() - started
        stats_at_timeout = resolver.stats()

        # With the only resolver worker stuck, one queued call consumes the
        # sole pending slot and a third distinct hostname is rejected fast.
        queued_result = {}

        def queue_second():
            try:
                resolver.resolve("second.invalid", 5432, deadline=time.monotonic() + 1.0)
                queued_result["done"] = True
            except Exception as error:
                queued_result["error"] = type(error).__name__

        queued = threading.Thread(target=queue_second, daemon=True)
        queued.start()
        time.sleep(0.03)
        capacity_started = time.monotonic()
        capacity_error = None
        try:
            resolver.resolve("third.invalid", 5432, deadline=time.monotonic() + 1.0)
        except Exception as error:
            capacity_error = error
        capacity_elapsed = time.monotonic() - capacity_started
    finally:
        blocker.set()
        db_connect.resolve = original_resolve
    queued.join(0.5)
    check(
        "fake stuck DNS returns by the absolute deadline without entering libpq or leaking a secret",
        isinstance(timeout_error, db_connect.DatabaseConnectDeadlineExceeded)
        and 0.95 <= elapsed <= 1.40
        and not connector_called
        and secret not in str(timeout_error)
        and stats_at_timeout["threads_started"] == 1
        and stats_at_timeout["threads_alive"] == 1,
    )
    check(
        "resolver saturation is bounded and rejects excess DNS work immediately",
        isinstance(capacity_error, bounded_resolver.ResolutionCapacityExceeded)
        and capacity_elapsed < 0.20
        and resolver.stats()["threads_started"] == 1,
    )

    # URL DSN: resolve to one hostaddr per attempt while retaining the original
    # hostname. The DSN still carries SSL/auth configuration unchanged.
    calls = []
    original_resolve = db_connect.resolve
    db_connect.resolve = lambda host, port, **_kwargs: (
        addr("2001:db8::10", int(port)),
        addr("192.0.2.20", int(port)),
    )
    url_dsn = (
        "postgresql://app:url-secret@db.example.test:5433/veripsa"
        "?sslmode=verify-full&sslrootcert=%2Ftmp%2Fca.pem"
    )

    def second_address_succeeds(dsn, **kwargs):
        calls.append((dsn, dict(kwargs)))
        if len(calls) == 1:
            raise psycopg2.OperationalError(f"driver echoed {dsn}")
        return FakeConnection()

    try:
        connected = db_connect.connect(
            second_address_succeeds,
            url_dsn,
            deadline=time.monotonic() + 5,
            connect_timeout=5,
        )
    finally:
        db_connect.resolve = original_resolve
    check(
        "URL DSN keeps TLS/auth hostname while trying resolved hostaddr values one-by-one",
        isinstance(connected, FakeConnection)
        and len(calls) == 2
        and all(call[0] == url_dsn for call in calls)
        and [call[1].get("host") for call in calls] == ["db.example.test", "db.example.test"]
        and [call[1].get("hostaddr") for call in calls] == ["2001:db8::10", "192.0.2.20"]
        and [call[1].get("port") for call in calls] == [5433, 5433]
        and all(1 <= call[1].get("connect_timeout", 0) <= 5 for call in calls),
    )

    # An omitted DSN port is not synonymous with 5432: libpq first honors
    # PGPORT.  The release gates use this exact shape for their private,
    # randomly-ported PostgreSQL cluster.
    env_port_calls = []
    env_port_resolves = []
    original_resolve = db_connect.resolve
    original_pgport = os.environ.get("PGPORT")
    os.environ["PGPORT"] = "6543"

    def env_port_resolve(host, port, **_kwargs):
        env_port_resolves.append((host, port))
        return (addr("192.0.2.21", int(port)),)

    def env_port_connector(dsn, **kwargs):
        env_port_calls.append((dsn, dict(kwargs)))
        return FakeConnection()

    db_connect.resolve = env_port_resolve
    try:
        env_port_connection = db_connect.connect(
            env_port_connector,
            "postgresql://app@db.example.test/veripsa",
            deadline=time.monotonic() + 3,
            connect_timeout=3,
        )
    finally:
        db_connect.resolve = original_resolve
        if original_pgport is None:
            os.environ.pop("PGPORT", None)
        else:
            os.environ["PGPORT"] = original_pgport
    check(
        "an omitted DSN port preserves libpq PGPORT instead of forcing 5432",
        isinstance(env_port_connection, FakeConnection)
        and env_port_resolves == [("db.example.test", 6543)]
        and len(env_port_calls) == 1
        and env_port_calls[0][1].get("port") == 6543,
    )

    # A driver may include its input DSN in a native exception. Exhaustion must
    # replace that with a content-free error rather than logging credentials.
    original_resolve = db_connect.resolve
    db_connect.resolve = lambda _host, port, **_kwargs: (addr("192.0.2.30", int(port)),)
    leaked_error = None
    try:
        try:
            db_connect.connect(
                lambda dsn, **_kwargs: (_ for _ in ()).throw(
                    psycopg2.OperationalError(f"failed DSN={dsn}")),
                "host=db.example.test dbname=x user=app password='keyword secret'",
                deadline=time.monotonic() + 3,
                connect_timeout=3,
            )
        except Exception as error:
            leaked_error = error
    finally:
        db_connect.resolve = original_resolve
    check(
        "keyword DSN failures are sanitized and never expose password text",
        isinstance(leaked_error, db_connect.DatabaseConnectError)
        and "keyword secret" not in str(leaked_error)
        and "db.example.test" not in str(leaked_error),
    )

    # Deterministic virtual time proves per-address connect_timeout values share
    # one budget instead of multiplying by the address count.
    virtual_now = [100.0]
    attempt_timeouts = []
    original_monotonic = time.monotonic
    original_resolve = db_connect.resolve
    db_connect.resolve = lambda _host, port, **_kwargs: (
        addr("192.0.2.41", int(port)),
        addr("192.0.2.42", int(port)),
        addr("192.0.2.43", int(port)),
    )

    def virtual_monotonic():
        return virtual_now[0]

    def consumes_its_timeout(_dsn, **kwargs):
        timeout = int(kwargs["connect_timeout"])
        attempt_timeouts.append(timeout)
        virtual_now[0] += timeout
        raise psycopg2.OperationalError("address refused")

    multi_error = None
    try:
        db_connect.time.monotonic = virtual_monotonic
        try:
            db_connect.connect(
                consumes_its_timeout,
                "host=multi.example.test dbname=x",
                deadline=103.4,
                connect_timeout=10,
            )
        except Exception as error:
            multi_error = error
    finally:
        db_connect.time.monotonic = original_monotonic
        db_connect.resolve = original_resolve
    check(
        "multiple resolved address attempts consume one total deadline",
        isinstance(multi_error, db_connect.DatabaseConnectError)
        and attempt_timeouts == [1, 1, 1]
        and sum(attempt_timeouts) <= 3.4,
    )

    # Numeric IP, Unix socket, hostless DSN, and an explicit hostaddr are
    # already routing authorities and must never invoke hostname resolution.
    original_resolve = db_connect.resolve
    direct_calls = []

    def resolution_forbidden(*_args, **_kwargs):
        raise AssertionError("resolver must not be called")

    def direct_connector(dsn, **kwargs):
        direct_calls.append((dsn, dict(kwargs)))
        return FakeConnection()

    db_connect.resolve = resolution_forbidden
    try:
        for dsn in (
            "postgresql://app@127.0.0.1/x",
            "host=/var/run/postgresql dbname=x",
            "postgresql:///x",
            "host=db.example.test hostaddr=192.0.2.99 dbname=x sslmode=verify-full",
        ):
            db_connect.connect(
                direct_connector,
                dsn,
                deadline=time.monotonic() + 3,
                connect_timeout=3,
            )
    finally:
        db_connect.resolve = original_resolve
    check(
        "IP, Unix socket, hostless, and explicit-hostaddr DSNs bypass DNS correctly",
        len(direct_calls) == 4
        and direct_calls[0][1].get("host") == "127.0.0.1"
        and direct_calls[1][1].get("host") == "/var/run/postgresql"
        and "host" not in direct_calls[2][1]
        and direct_calls[3][1].get("host") == "db.example.test"
        and direct_calls[3][1].get("hostaddr") == "192.0.2.99",
    )

    # Runtime integration: each named DB hot path must route through this leaf.
    integration_calls = []
    resolve_calls = []
    original_resolve = db_connect.resolve
    original_connect = psycopg2.connect

    def integration_resolve(host, port, **_kwargs):
        resolve_calls.append((host, port))
        return (addr("192.0.2.123", int(port)),)

    def integration_connector(dsn, **kwargs):
        integration_calls.append((dsn, dict(kwargs)))
        return FakeConnection("ok")

    db_connect.resolve = integration_resolve
    psycopg2.connect = integration_connector
    try:
        event_conn = event_processor._connect_event_db(
            "postgresql://app:integration-secret@event-db.example.test/event", 60_000)
        store = delivery_queue.DeliveryStore(
            "host=store-db.example.test dbname=store user=app password='integration secret'")
        store_value = store._one("SELECT 1")
        out_of_band_value = server_dbops._make_db(
            "postgresql://app@ops-db.example.test/ops")("SELECT 1")
    finally:
        psycopg2.connect = original_connect
        db_connect.resolve = original_resolve
    integrated_hosts = [kwargs.get("host") for _dsn, kwargs in integration_calls]
    integrated_addrs = [kwargs.get("hostaddr") for _dsn, kwargs in integration_calls]
    check(
        "event processor, DeliveryStore, and server_dbops all use bounded host+hostaddr connection setup",
        isinstance(event_conn, FakeConnection)
        and store_value == "ok" and out_of_band_value == "ok"
        and integrated_hosts == [
            "event-db.example.test", "store-db.example.test", "ops-db.example.test"]
        and integrated_addrs == ["192.0.2.123"] * 3
        and [host for host, _port in resolve_calls] == integrated_hosts,
    )

    ok = all(condition for _, condition in checks)
    print("\nDB DNS DEADLINE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
