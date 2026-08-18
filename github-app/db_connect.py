#!/usr/bin/env python3
"""Deadline-bounded PostgreSQL connection setup with bounded DNS resolution.

libpq's ``connect_timeout`` starts too late to bound a wedged libc/NSS hostname
lookup. Resolve hostnames through the shared fixed-size resolver first, then
give libpq one numeric ``hostaddr`` at a time while retaining the original
``host`` for TLS SNI/certificate verification and pgpass authentication.

The DSN remains the source of credentials and SSL settings. This module never
formats, logs, or exposes it in an exception message.
"""
from __future__ import annotations

import ipaddress
import math
import os
import socket
import time
from typing import Callable

import psycopg2
from psycopg2.extensions import parse_dsn

try:
    from bounded_resolver import resolve
except ImportError:  # imported as a package
    from .bounded_resolver import resolve


class DatabaseConnectError(psycopg2.OperationalError):
    """A sanitized PostgreSQL resolution/connection failure."""


class DatabaseConnectDeadlineExceeded(DatabaseConnectError):
    """The shared absolute resolution/connection deadline expired."""


def deadline_after(timeout_seconds: float, absolute_deadline: float | None = None) -> float:
    """Return one monotonic deadline capped by an optional event deadline."""
    local = time.monotonic() + max(0.001, float(timeout_seconds))
    return local if absolute_deadline is None else min(local, float(absolute_deadline))


def _deadline_error() -> DatabaseConnectDeadlineExceeded:
    return DatabaseConnectDeadlineExceeded(
        "database hostname resolution/connection deadline exceeded")


def _parse(dsn: str, kwargs: dict) -> tuple[str | None, str | None, str | None]:
    try:
        parsed = parse_dsn(dsn)
    except Exception:
        # parse_dsn's native text may echo a malformed DSN containing a password.
        raise DatabaseConnectError("invalid database connection configuration") from None
    host = kwargs.get("host", parsed.get("host"))
    hostaddr = kwargs.get("hostaddr", parsed.get("hostaddr"))
    port = kwargs.get("port", parsed.get("port"))
    return (
        None if host is None else str(host)
    ), (
        None if hostaddr is None else str(hostaddr)
    ), (
        None if port is None else str(port)
    )


def _split_ports(port_spec: str | None, count: int) -> list[str]:
    # libpq treats PGPORT as the effective port when neither the DSN nor
    # explicit kwargs name one.  Preserve that contract: run_gates and managed
    # environments intentionally publish a non-default port through PGPORT.
    # Replacing an omitted port with a literal 5432 here would resolve the
    # correct host and then silently connect to the wrong postmaster.
    effective = port_spec
    if effective is None:
        effective = os.environ.get("PGPORT") or "5432"
    ports = effective.split(",")
    if len(ports) == 1:
        return [ports[0] or "5432"] * count
    if len(ports) != count:
        raise DatabaseConnectError("invalid database host/port configuration")
    return [port or "5432" for port in ports]


def _is_numeric_host(host: str) -> bool:
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    # An IPv6 scope identifier is meaningful to getaddrinfo/libpq but is not
    # accepted by ipaddress.ip_address; the address portion is still numeric.
    candidate = candidate.split("%", 1)[0]
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return False


def _connect_attempt(
    connector: Callable,
    dsn: str,
    base_kwargs: dict,
    *,
    deadline: float,
    timeout_cap: int,
    attempts_left: int,
    host: str | None = None,
    hostaddr: str | None = None,
    port: str | int | None = None,
    override_host: bool = False,
):
    remaining = float(deadline) - time.monotonic()
    if remaining < 1.0:
        raise _deadline_error()
    # libpq accepts only whole seconds. Divide the remaining allowance across
    # unresolved candidates so one dead address cannot multiply the total wait.
    fair_share = max(1, int(remaining / max(1, int(attempts_left))))
    connect_timeout = max(1, min(int(timeout_cap), fair_share, int(remaining)))
    call_kwargs = dict(base_kwargs)
    call_kwargs["connect_timeout"] = connect_timeout
    if override_host:
        call_kwargs["host"] = host if host is not None else ""
    if hostaddr is not None:
        call_kwargs["hostaddr"] = hostaddr
    if port is not None:
        call_kwargs["port"] = port
    conn = connector(dsn, **call_kwargs)
    if time.monotonic() >= deadline:
        try:
            conn.close()
        except Exception:
            pass
        raise _deadline_error()
    return conn


def connect(
    connector: Callable,
    dsn: str,
    *,
    deadline: float,
    connect_timeout: int,
    **kwargs,
):
    """Connect within one absolute deadline, resolving each hostname once.

    ``connector`` is supplied by the caller (normally its local
    ``psycopg2.connect`` seam), preserving existing fake-connection tests.
    ``host`` remains the original DNS name on every libpq attempt; only
    ``hostaddr`` changes to a resolved numeric address.
    """
    absolute = float(deadline)
    if not math.isfinite(absolute) or absolute - time.monotonic() < 1.0:
        raise _deadline_error()
    timeout_cap = max(1, int(connect_timeout))
    base_kwargs = dict(kwargs)
    # These are reconstructed per attempt so a URL DSN, keyword DSN, and
    # explicit kwargs all follow the same one-address contract.
    base_kwargs.pop("connect_timeout", None)
    base_kwargs.pop("host", None)
    base_kwargs.pop("hostaddr", None)
    base_kwargs.pop("port", None)
    host_spec, hostaddr_spec, port_spec = _parse(dsn, kwargs)

    # Hostless DSNs use libpq's local Unix-socket default. There is no hostname
    # to resolve and no TLS hostname to override.
    if host_spec is None and hostaddr_spec is None:
        try:
            return _connect_attempt(
                connector, dsn, base_kwargs,
                deadline=absolute, timeout_cap=timeout_cap, attempts_left=1,
            )
        except DatabaseConnectDeadlineExceeded:
            raise
        except Exception as error:
            # Keep injected test/control-flow seams observable. Real libpq
            # failures are psycopg2.Error (or an OS transport error) and remain
            # sanitized below.
            if not isinstance(error, (psycopg2.Error, OSError)):
                raise
            raise DatabaseConnectError("database connection failed") from None

    hosts = host_spec.split(",") if host_spec is not None else []

    # An explicit hostaddr is already numeric authority. Split it so libpq
    # cannot apply connect_timeout once per address behind our total deadline.
    if hostaddr_spec is not None:
        addresses = hostaddr_spec.split(",")
        if hosts and len(hosts) not in (1, len(addresses)):
            raise DatabaseConnectError("invalid database host/address configuration")
        ports = _split_ports(port_spec, len(addresses))
        candidates = []
        for index, address in enumerate(addresses):
            original_host = hosts[index] if len(hosts) == len(addresses) else (hosts[0] if hosts else None)
            candidates.append((original_host, address, ports[index], bool(hosts)))
    else:
        ports = _split_ports(port_spec, len(hosts))
        candidates = []
        for original_host, port in zip(hosts, ports):
            # Empty/multi-host Unix socket and numeric hosts need no DNS.
            if not original_host or original_host.startswith("/") or _is_numeric_host(original_host):
                candidates.append((original_host, None, port, True))
                continue
            try:
                resolved = resolve(
                    original_host,
                    int(port) if port.isdecimal() else port,
                    deadline=absolute,
                    family=socket.AF_UNSPEC,
                    socktype=socket.SOCK_STREAM,
                )
            except Exception:
                if time.monotonic() >= absolute:
                    raise _deadline_error() from None
                raise DatabaseConnectError("database hostname resolution failed") from None
            seen: set[str] = set()
            for _family, _socktype, _proto, _canonname, sockaddr in resolved:
                address = str(sockaddr[0])
                if address in seen:
                    continue
                seen.add(address)
                # Use getaddrinfo's numeric service result so libpq cannot
                # repeat an NSS service lookup after hostname resolution.
                resolved_port = sockaddr[1] if len(sockaddr) > 1 else port
                candidates.append((original_host, address, resolved_port, True))

    if not candidates:
        raise DatabaseConnectError("database hostname resolved to no addresses")

    last_error = None
    total = len(candidates)
    for index, (original_host, address, port, override_host) in enumerate(candidates):
        try:
            return _connect_attempt(
                connector, dsn, base_kwargs,
                deadline=absolute,
                timeout_cap=timeout_cap,
                attempts_left=total - index,
                host=original_host,
                hostaddr=address,
                port=port,
                override_host=override_host,
            )
        except DatabaseConnectDeadlineExceeded:
            raise
        except Exception as exc:
            if not isinstance(exc, (psycopg2.Error, OSError)):
                raise
            last_error = exc
            if time.monotonic() >= absolute:
                raise _deadline_error() from None
    _ = last_error  # Deliberately never stringify: a driver error may include DSN fragments.
    raise DatabaseConnectError(
        f"database connection failed after {total} address attempt(s)") from None
