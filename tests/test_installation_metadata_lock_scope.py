#!/usr/bin/env python3
"""Regression: account display metadata must not convoy repository event bodies.

Production evidence showed otherwise-independent repository deliveries failing
``lock_timeout`` in ``note_installation_account_metadata_with_authority``.  The
cause was one shared ``installation_account`` row being updated inside each
long repository body transaction.  This gate proves both halves of the fix on
real Postgres:

* the old in-transaction shape really does block another repository's metadata
  statement, so the fixture detects the incident rather than merely grepping;
* the production helper commits metadata before the body transaction, and an
  unchanged observation performs no physical hot-row update for five minutes.

Run: python3 tests/test_installation_metadata_lock_scope.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import psycopg2


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
for path in (ROOT, APP):
    if path not in sys.path:
        sys.path.insert(0, path)

import event_processor as EP  # noqa: E402


DB = "veripsa_metadata_lock_" + str(os.getpid())
ADMIN_DSN = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
MIGRATOR_DSN = f"postgresql://veripsa_migrator@localhost/{DB}"
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"
INSTALLATION = "metadata-lock-installation"
ACCOUNT = "ACCT-DEMO"


def check(results: list[tuple[str, bool]], label: str, condition: bool) -> None:
    passed = bool(condition)
    results.append((label, passed))
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}")


def bootstrap() -> None:
    result = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr)[-2000:])
    with psycopg2.connect(MIGRATOR_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO core.installation_account(
                    installation_id,account_id,account_login,account_type,account_seen_at
                ) VALUES (%s,%s,'metadata-old','Organization',now()-interval '1 hour')
                ON CONFLICT (installation_id) DO UPDATE
                    SET account_id=EXCLUDED.account_id,
                        account_login=EXCLUDED.account_login,
                        account_type=EXCLUDED.account_type,
                        account_seen_at=EXCLUDED.account_seen_at,
                        revoked_at=NULL
                """,
                (INSTALLATION, ACCOUNT),
            )


def route(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute(
            "SELECT core.enter_existing_installation_with_authority(%s)",
            (INSTALLATION,),
        )
        row = cur.fetchone()
        if not row or row[0] != ACCOUNT:
            raise RuntimeError("metadata fixture did not resolve its installation route")


def state() -> tuple[str | None, str | None, str]:
    with psycopg2.connect(MIGRATOR_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_login,account_seen_at::text,xmin::text
                  FROM core.installation_account
                 WHERE installation_id=%s
                """,
                (INSTALLATION,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("metadata fixture row disappeared")
            return row


def mark_stale() -> None:
    with psycopg2.connect(MIGRATOR_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE core.installation_account
                   SET account_seen_at=now()-interval '1 hour'
                 WHERE installation_id=%s
                """,
                (INSTALLATION,),
            )


def main() -> int:
    print("=== INSTALLATION METADATA LOCK SCOPE GATE ===")
    results: list[tuple[str, bool]] = []
    holder = probe = None
    try:
        bootstrap()
        holder = psycopg2.connect(APP_DSN)
        probe = psycopg2.connect(APP_DSN)
        holder.autocommit = True
        probe.autocommit = True
        route(holder)
        route(probe)

        EP._note_installation_account_metadata_outside_body(
            holder, INSTALLATION, "metadata-current", "Organization")
        first = state()
        EP._note_installation_account_metadata_outside_body(
            probe, INSTALLATION, "metadata-current", "Organization")
        second = state()
        check(
            results,
            "unchanged metadata is throttled without a physical hot-row update",
            first == second and first[0] == "metadata-current" and first[1] is not None,
        )

        EP._note_installation_account_metadata_outside_body(
            probe, INSTALLATION, "metadata-renamed", "Organization")
        renamed = state()
        check(
            results,
            "a real public-account rename remains immediate despite throttling",
            renamed[0] == "metadata-renamed" and renamed[2] != second[2],
        )

        # Sensitivity control: reproduce the pre-fix shape.  The holder updates
        # metadata inside a transaction and keeps that transaction open; the
        # independent probe must hit the same 55P03 observed in production.
        mark_stale()
        holder.autocommit = False
        with holder.cursor() as cur:
            cur.execute(
                "SELECT core.note_installation_account_metadata_with_authority(%s,%s,%s)",
                (INSTALLATION, "metadata-held", "Organization"),
            )
        with probe.cursor() as cur:
            cur.execute("SET lock_timeout = 250")
        blocked = None
        started = time.monotonic()
        try:
            with probe.cursor() as cur:
                cur.execute(
                    "SELECT core.note_installation_account_metadata_with_authority(%s,%s,%s)",
                    (INSTALLATION, "metadata-probe", "Organization"),
                )
        except psycopg2.errors.LockNotAvailable as error:
            blocked = error
        blocked_ms = (time.monotonic() - started) * 1000.0
        probe.rollback()
        holder.rollback()
        check(
            results,
            "sensitivity control reproduces the old in-body metadata lock timeout",
            blocked is not None and blocked_ms < 5_000,
        )

        # Fixed shape: the production helper commits the shared metadata row,
        # then an unrelated long body transaction can stay open without owning
        # that row.  A peer repository can force a real metadata write
        # immediately while the unrelated body remains open.
        holder.autocommit = True
        EP._note_installation_account_metadata_outside_body(
            holder, INSTALLATION, "metadata-safe", "Organization")
        holder.autocommit = False
        with holder.cursor() as cur:
            cur.execute("SELECT 1")
        started = time.monotonic()
        EP._note_installation_account_metadata_outside_body(
            probe, INSTALLATION, "metadata-peer", "Organization")
        safe_ms = (time.monotonic() - started) * 1000.0
        check(
            results,
            "autocommit metadata lets a peer write during an unrelated repository body",
            safe_ms < 1_000 and state()[0] == "metadata-peer",
        )
        holder.rollback()
    except Exception as error:
        check(results, f"gate setup/runtime completed ({type(error).__name__}: {error})", False)
    finally:
        if holder is not None:
            holder.close()
        if probe is not None:
            probe.close()
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)

    failed = [label for label, passed in results if not passed]
    if failed:
        print(f"INSTALLATION METADATA LOCK SCOPE GATE: FAIL ({len(failed)} failure(s))")
        for label in failed:
            print("  -", label)
        return 1
    print(f"INSTALLATION METADATA LOCK SCOPE GATE: PASS ({len(results)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
