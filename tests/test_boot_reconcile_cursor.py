#!/usr/bin/env python3
"""Durable boot-reconcile keyset cursor and production-routing gate.

Proves the restart self-heal no longer treats bounded GitHub App installation
or per-installation repository inventories as complete:

* PostgreSQL pages every live lifecycle route with a durable composite keyset;
* multiple repositories in one installation, a route after the old 500-item
  boundary, and wrap-around are all reachable;
* cursor state is content-free and advances by exact compare-and-swap;
* a failed attempted route advances (no poison-tenant starvation), while a
  deadline never advances an unattempted route;
* the production DB path mints exact installation clients and never calls the
  App-installation or installation-repository list methods;
* page(A) cannot authorize writes after installation generation B replaces it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import psycopg2


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = "veripsa_boot_cursor_" + str(os.getpid())
MIG_DSN = f"postgresql://veripsa_migrator@localhost/{DB}"
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"
checks: list[bool] = []


def check(ok: bool, label: str) -> None:
    passed = bool(ok)
    checks.append(passed)
    print(("  [PASS] " if passed else "  [FAIL] ") + label)


def scalar(dsn: str, sql: str, args=()):
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone() if cur.description else None
            return row[0] if row else None
    finally:
        conn.close()


def as_json(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def seed_routes() -> list[tuple[str, str, str, str]]:
    """Create 503 live routes; account 1 owns two repositories."""
    rows = []
    conn = psycopg2.connect(MIG_DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            for index in range(1, 504):
                owner = f"{index:04d}"
                account = f"ACCT-GH-{owner}"
                installation = str(900000 + index)
                repository = str(700000 + index)
                repo = f"tenant-{index:04d}/repo"
                cur.execute(
                    "SELECT core.provision_seat(%s,%s,%s,%s,%s)",
                    (
                        account,
                        account,
                        f"AG-BOOT-{owner}",
                        "Veripsa App",
                        "veripsa_app",
                    ),
                )
                cur.execute(
                    "INSERT INTO core.installation_account("
                    "installation_id,account_id,github_installation_id,"
                    "github_installation_created_at) "
                    "VALUES (%s,%s,%s,%s::timestamptz)",
                    (
                        owner,
                        account,
                        installation,
                        f"2026-01-{(index % 27) + 1:02d}T00:00:00Z",
                    ),
                )
                cur.execute(
                    "SELECT set_config('core.current_account',%s,true)",
                    (account,),
                )
                cur.execute(
                    "INSERT INTO core.repository_lifecycle_activation("
                    "account_id,repository_id,repo,lifecycle_authoritative,"
                    "generation_started_at) VALUES (%s,%s,%s,true,clock_timestamp())",
                    (account, repository, repo),
                )
                rows.append((account, installation, repository, repo))
                if index == 1:
                    second_repository = "799999"
                    second_repo = "tenant-0001/repo-two"
                    cur.execute(
                        "INSERT INTO core.repository_lifecycle_activation("
                        "account_id,repository_id,repo,lifecycle_authoritative,"
                        "generation_started_at) "
                        "VALUES (%s,%s,%s,true,clock_timestamp())",
                        (account, second_repository, second_repo),
                    )
                    rows.append(
                        (account, installation, second_repository, second_repo))
    finally:
        conn.close()
    return rows


def advance(expected: dict, route: dict) -> dict:
    return as_json(scalar(
        APP_DSN,
        "SELECT core.advance_boot_reconcile_route_cursor_with_authority("
        "%s,%s,%s,%s)",
        (
            expected.get("account_id"),
            expected.get("repository_id"),
            route["account_id"],
            route["repository_id"],
        ),
    ))


class FakeDB:
    def __init__(self, routes, *, fail_advance_at=None):
        self.routes = list(routes)
        self.cursor = {"account_id": None, "repository_id": None}
        self.advances = []
        self.fail_advance_at = fail_advance_at

    def __call__(self, sql, args=()):
        if "read_boot_reconcile_route_page_with_authority" in sql:
            return {
                "routes": list(self.routes),
                "cursor": dict(self.cursor),
                "wrapped": False,
            }
        if "advance_boot_reconcile_route_cursor_with_authority" in sql:
            expected = {
                "account_id": args[0],
                "repository_id": args[1],
            }
            next_cursor = {
                "account_id": args[2],
                "repository_id": args[3],
            }
            if expected != self.cursor:
                return {"advanced": False, "reason": "cursor changed"}
            if (
                    self.fail_advance_at is not None
                    and len(self.advances) == self.fail_advance_at):
                return {"advanced": False, "reason": "injected CAS loss"}
            self.cursor = next_cursor
            self.advances.append(dict(next_cursor))
            return {"advanced": True}
        raise AssertionError(f"unexpected DB call: {sql}")


class ScopedGH:
    def __init__(self, installation_id):
        self.installation_id = installation_id


class RootGH:
    def __init__(self):
        self.minted = []
        self.list_calls = 0
        self.repo_list_calls = 0

    def list_app_installations(self):
        self.list_calls += 1
        raise AssertionError("production boot must not enumerate App installs")

    def installation_repo_entries(self, cap=200):
        self.repo_list_calls += 1
        raise AssertionError("production boot must not enumerate repo page one")

    def for_installation(self, installation_id):
        self.minted.append(str(installation_id))
        return ScopedGH(str(installation_id))


def runtime_routes():
    return [
        {
            "account_id": f"ACCT-GH-{index}",
            "github_installation_id": str(990000 + index),
            "github_installation_created_at": "2026-01-01T00:00:00+00:00",
            "repository_id": str(880000 + index),
            "repo": f"runtime/repo-{index}",
        }
        for index in range(1, 5)
    ]


def main() -> int:
    subprocess.run(["dropdb", "--if-exists", DB], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["createdb", "-O", "veripsa_migrator", DB], check=True)
    try:
        subprocess.run(
            ["psql", MIG_DSN, "-v", "ON_ERROR_STOP=1", "-f", "db/schema.sql"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        seeded = seed_routes()

        first = as_json(scalar(
            APP_DSN,
            "SELECT core.read_boot_reconcile_route_page_with_authority(200)",
        ))
        first_routes = first["routes"]
        check(
            len(first_routes) == 200
            and len({
                route["repo"]
                for route in first_routes
                if route["github_installation_id"] == seeded[0][1]
            }) == 2,
            "one bounded DB page includes multiple repositories from the same installation",
        )

        expected = dict(first["cursor"])
        for route in first_routes:
            moved = advance(expected, route)
            if not moved.get("advanced"):
                raise AssertionError(f"cursor did not advance: {moved}")
            expected = {
                "account_id": route["account_id"],
                "repository_id": route["repository_id"],
            }
        second = as_json(scalar(
            APP_DSN,
            "SELECT core.read_boot_reconcile_route_page_with_authority(200)",
        ))
        for route in second["routes"]:
            moved = advance(expected, route)
            if not moved.get("advanced"):
                raise AssertionError(f"cursor did not advance: {moved}")
            expected = {
                "account_id": route["account_id"],
                "repository_id": route["repository_id"],
            }
        third = as_json(scalar(
            APP_DSN,
            "SELECT core.read_boot_reconcile_route_page_with_authority(200)",
        ))
        tail_accounts = {route["account_id"] for route in third["routes"]}
        check(
            "ACCT-GH-0503" in tail_accounts,
            "durable keyset reaches the route after the old 500-installation boundary",
        )
        for route in third["routes"]:
            moved = advance(expected, route)
            if not moved.get("advanced"):
                raise AssertionError(f"cursor did not advance: {moved}")
            expected = {
                "account_id": route["account_id"],
                "repository_id": route["repository_id"],
            }
        wrapped = as_json(scalar(
            APP_DSN,
            "SELECT core.read_boot_reconcile_route_page_with_authority(2)",
        ))
        check(
            wrapped.get("wrapped") is True
            and wrapped["routes"] == first_routes[:2],
            "after the final key the durable page wraps to the beginning",
        )

        stale = advance({"account_id": None, "repository_id": None},
                        wrapped["routes"][0])
        check(
            stale.get("advanced") is False,
            "cursor advance is exact CAS and rejects a stale expected key",
        )
        cursor_fields = as_json(scalar(
            MIG_DSN,
            "SELECT fields FROM core.boot_reconcile_state "
            "WHERE kind='boot_reconcile_cursor'",
        ))
        check(
            set(cursor_fields) == {"account_id", "repository_id"}
            and all(
                "tenant-" not in str(value)
                for value in cursor_fields.values()
            ),
            "persisted cursor is content-free (bounded account/repository ids only)",
        )

        # Exact generation check is consumed under the Python-held shared
        # lifecycle lock.  The SQL predicate itself must reject stale A after B.
        generation_route = first_routes[0]
        generation_account = generation_route["account_id"]
        generation_owner = generation_account.removeprefix("ACCT-GH-")
        conn = psycopg2.connect(APP_DSN)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(
                    "SELECT core.enter_existing_installation_with_authority(%s)",
                    (generation_owner,),
                )
                cur.execute(
                    "SELECT core.boot_reconcile_route_is_current_with_authority("
                    "%s,%s::timestamptz,%s,%s)",
                    (
                        generation_route["github_installation_id"],
                        generation_route["github_installation_created_at"],
                        generation_route["repo"],
                        generation_route["repository_id"],
                    ),
                )
                before_generation_change = cur.fetchone()[0]
        finally:
            conn.close()
        scalar(
            MIG_DSN,
            "UPDATE core.installation_account "
            "SET github_installation_id='123456789',"
            "github_installation_created_at='2027-01-01T00:00:00Z' "
            "WHERE account_id=%s",
            (generation_account,),
        )
        conn = psycopg2.connect(APP_DSN)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(
                    "SELECT core.enter_existing_installation_with_authority(%s)",
                    (generation_owner,),
                )
                cur.execute(
                    "SELECT core.boot_reconcile_route_is_current_with_authority("
                    "%s,%s::timestamptz,%s,%s)",
                    (
                        generation_route["github_installation_id"],
                        generation_route["github_installation_created_at"],
                        generation_route["repo"],
                        generation_route["repository_id"],
                    ),
                )
                after_generation_change = cur.fetchone()[0]
        finally:
            conn.close()
        check(
            before_generation_change is True
            and after_generation_change is False,
            "exact route generation fence rejects page(A) after installation B replaces it",
        )

        sys.path.insert(0, os.path.join(ROOT, "github-app"))
        import ingest  # noqa: E402

        original_reconcile = ingest._reconcile_one_repo
        original_monotonic = ingest.time.monotonic
        seen = []

        def fake_reconcile(_db, scoped, repo, _dsn, repository_id=None, **kwargs):
            seen.append((
                repo,
                scoped.installation_id,
                str(repository_id),
                kwargs.get("expected_account_id"),
            ))
            if repo.endswith("-2"):
                raise RuntimeError("poison route")
            return {"backfilled": repo}

        ingest._reconcile_one_repo = fake_reconcile
        try:
            routes = runtime_routes()
            fake_db = FakeDB(routes)
            root_gh = RootGH()
            result = ingest.boot_reconcile(
                fake_db, root_gh, cap=200, dsn="postgresql://unused")
            check(
                result["reconciled"] == 3
                and result["failed"] == 1
                and result["cursor_healthy"] is True
                and len(fake_db.advances) == 4
                and [row[0] for row in seen] == [
                    route["repo"] for route in routes
                ],
                "a failed attempted route advances and does not starve later DB routes",
            )
            check(
                root_gh.list_calls == 0
                and root_gh.repo_list_calls == 0
                and root_gh.minted == [
                    route["github_installation_id"] for route in routes
                ],
                "production DB path never calls installation/repository inventories and mints exact clients",
            )

            deadline_db = FakeDB(routes)
            deadline_gh = RootGH()
            seen.clear()
            ticks = [0.0, 0.0, 9999.0]
            ingest.time.monotonic = (
                lambda: ticks.pop(0) if ticks else 9999.0)
            deadline_result = ingest.boot_reconcile(
                deadline_db,
                deadline_gh,
                cap=200,
                dsn="postgresql://unused",
                deadline_seconds=10,
            )
            check(
                deadline_result["reconciled"] == 1
                and deadline_result["deferred"] == 3
                and len(deadline_db.advances) == 1
                and deadline_db.cursor["repository_id"]
                == routes[0]["repository_id"],
                "deadline leaves every unattempted route behind the durable cursor",
            )

            cas_db = FakeDB(routes, fail_advance_at=1)
            cas_gh = RootGH()
            seen.clear()
            ingest.time.monotonic = original_monotonic
            cas_result = ingest.boot_reconcile(
                cas_db, cas_gh, cap=200, dsn="postgresql://unused")
            check(
                cas_result["cursor_healthy"] is False
                and cas_result["deferred"] == 2
                and len(seen) == 2
                and len(cas_db.advances) == 1,
                "lost cursor CAS stops before processing any later route",
            )
        finally:
            ingest._reconcile_one_repo = original_reconcile
            ingest.time.monotonic = original_monotonic

        import server_boot  # noqa: E402

        original_server = server_boot._server
        stamped = []
        throttle_reads = []

        def throttle_db(sql, args=()):
            if "read_boot_reconcile_last_run_with_authority" in sql:
                throttle_reads.append(sql)
                return {"found": False}
            if "mark_boot_reconcile_run_with_authority" in sql:
                stamped.append(args)
                return {"ok": True}
            raise AssertionError(f"unexpected throttle DB call: {sql}")

        class BootResult:
            def __init__(self, result):
                self.result = result

            def boot_reconcile(self, *_args, **_kwargs):
                return dict(self.result)

        try:
            server_boot._server = lambda: BootResult({"error": "route page"})
            server_boot._boot_reconcile_throttled_once(
                throttle_db, object(), 200, None, 60, False)
            server_boot._server = lambda: BootResult(
                {"cursor_healthy": False, "repos": 2})
            server_boot._boot_reconcile_throttled_once(
                throttle_db, object(), 200, None, 60, False)
            failed_stamps = len(stamped)
            server_boot._server = lambda: BootResult(
                {"cursor_healthy": True, "repos": 2, "reconciled": 2})
            server_boot._boot_reconcile_throttled_once(
                throttle_db, object(), 200, None, 60, False)
        finally:
            server_boot._server = original_server
        check(
            len(throttle_reads) == 3
            and failed_stamps == 0
            and len(stamped) == 1,
            "route-page/cursor failures are not stamped as a successful 60-minute throttle run",
        )

        # Direct App execution is required, while a buyer writer remains denied.
        app_exec = scalar(
            MIG_DSN,
            "SELECT has_function_privilege("
            "'veripsa_app',"
            "'core.read_boot_reconcile_route_page_with_authority(integer)',"
            "'EXECUTE')",
        )
        writer_exec = scalar(
            MIG_DSN,
            "SELECT has_function_privilege("
            "'veripsa_writer',"
            "'core.advance_boot_reconcile_route_cursor_with_authority("
            "text,text,text,text)',"
            "'EXECUTE')",
        )
        check(
            app_exec is True and writer_exec is False,
            "boot route/cursor capabilities are App-only",
        )
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ok = all(checks)
    print("BOOT RECONCILE CURSOR GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
