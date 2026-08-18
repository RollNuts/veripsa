#!/usr/bin/env python3
"""Durable fleet graph-freshness cursor gate.

The owner freshness lens is called from latency-sensitive health/watchdog
paths.  A bounded first-page scan is fast but not complete: account 101 never
appears at cap=100, account 501 never appears once the fleet exceeds 500, and a
graph-heavy early account can consume the whole coordinate budget forever.

This real-PostgreSQL gate proves the bounded replacement:

* 520 live routes are traversed in stable account-key pages, including 100
  graph-empty accounts before the first graph-bearing account;
* account 101 and account 501 are eventually emitted, with the exact durable
  GitHub installation id + creation time and at most one coordinate/account;
* end-of-fleet wraps {after_account:null} and increments cycle;
* cycle % graph_count rotates a three-coordinate account through all three
  deterministic (repo,branch) coordinates;
* cursor advancement is an exact CAS, stale readers cannot skip a page, and
  its durable state contains only after_account + cycle;
* both SQL functions are executable by veripsa_app and denied to buyer roles.

Run: python3 tests/test_graph_freshness_cursor.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import psycopg2


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = "veripsa_fresh_cursor_" + str(os.getpid())
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"
MIG_DSN = f"postgresql://veripsa_migrator@localhost/{DB}"
CAP = 100
ACCOUNT_COUNT = 520
checks: list[bool] = []


def check(ok: bool, label: str) -> None:
    passed = bool(ok)
    checks.append(passed)
    print(("  [PASS] " if passed else "  [FAIL] ") + label)


def one(dsn: str, sql: str, args=()):
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
    if isinstance(value, dict):
        return value
    return json.loads(value) if value else {}


def account_key(number: int) -> str:
    return f"ACCT-GH-{number:06d}"


def exact_installation(number: int) -> str:
    return str(700000 + number)


def exact_created_at(value, number: int) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        expected = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
            seconds=number
        )
        return parsed.astimezone(timezone.utc) == expected
    except (TypeError, ValueError):
        return False


def surface() -> dict:
    return as_json(one(
        APP_DSN,
        "SELECT core.owner_graph_freshness_surface(%s)",
        (CAP,),
    ))


def advance(page: dict) -> bool:
    expected = page["expected_cursor"]
    nxt = page["next_cursor"]
    return bool(one(
        APP_DSN,
        "SELECT core.advance_graph_freshness_cursor_with_authority"
        "(%s,%s,%s,%s)",
        (
            expected.get("after_account"),
            expected.get("cycle"),
            nxt.get("after_account"),
            nxt.get("cycle"),
        ),
    ))


def ingest(owner_id: int, repo: str, branch: str, sha_char: str) -> None:
    conn = psycopg2.connect(APP_DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SELECT core.enter_existing_installation_with_authority(%s)",
                (f"{owner_id:06d}",),
            )
            graph = json.dumps({
                "nodes": [{
                    "id": f"{repo}:{branch}",
                    "kind": "file",
                    "path": "fixture.py",
                    "name": "fixture.py",
                }],
                "edges": [],
            })
            cur.execute(
                "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                (graph, repo, branch, sha_char * 40),
            )
    finally:
        conn.close()


def selected_repo(page: dict, account: str) -> str | None:
    for entry in page.get("entries") or []:
        if entry.get("account_id") != account:
            continue
        coordinate = entry.get("coordinate")
        if isinstance(coordinate, dict):
            return coordinate.get("repo")
    return None


def complete_cycle_from(page: dict) -> dict:
    """Advance the current page through its tail and return next cycle page 1."""
    current = page
    for _ in range(20):
        wrapped = current.get("coverage_complete") is True
        if not advance(current):
            raise AssertionError("freshness cursor CAS unexpectedly lost")
        current = surface()
        if wrapped:
            return current
    raise AssertionError("freshness cycle did not terminate within bounded pages")


def denied(sql: str, args=()) -> bool:
    conn = psycopg2.connect(f"postgresql://veripsa_demo_agent@localhost/{DB}")
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
    except psycopg2.errors.InsufficientPrivilege:
        return True
    except Exception as exc:
        return "permission denied" in str(exc).lower()
    finally:
        conn.close()
    return False


def main() -> int:
    boot = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if boot.returncode != 0:
        print("bootstrap failed:\n", boot.stderr[-1200:])
        return 1

    created = int(one(
        APP_DSN,
        "SELECT count(core.enter_installation_with_authority("
        "lpad(g::text,6,'0'))) FROM generate_series(1,%s) g",
        (ACCOUNT_COUNT,),
    ))
    stamped = int(one(
        MIG_DSN,
        "WITH updated AS ("
        " UPDATE core.installation_account"
        " SET github_installation_id=(700000+installation_id::int)::text,"
        "     github_installation_created_at="
        "       '2026-01-01T00:00:00Z'::timestamptz"
        "       +(installation_id::int * interval '1 second'),"
        "     revoked_at=NULL"
        " RETURNING 1"
        ") SELECT count(*) FROM updated",
    ))
    check(
        created == ACCOUNT_COUNT and stamped == ACCOUNT_COUNT,
        f"seeded {ACCOUNT_COUNT} exact live installation generations",
    )

    # Accounts 1..100 deliberately have no graph. Account 101 has three
    # coordinates and account 501 proves traversal past the historic 500 edge.
    ingest(101, "rotate/alpha", "main", "a")
    ingest(101, "rotate/beta", "dev", "b")
    ingest(101, "rotate/gamma", "main", "c")
    ingest(501, "after-five-hundred/repo", "main", "d")

    first = surface()
    first_entries = first.get("entries") or []
    check(
        first.get("expected_cursor") == {
            "after_account": None,
            "cycle": 0,
        }
        and first.get("next_cursor") == {
            "after_account": account_key(100),
            "cycle": 0,
        }
        and len(first_entries) == CAP
        and first.get("accounts_scanned") == CAP
        and first.get("coordinate_count") == 0
        and all(entry.get("coordinate") is None for entry in first_entries)
        and first.get("coverage_complete") is False
        and first.get("capped") is True,
        "page 1 advances across 100 graph-empty live accounts without "
        "inventing coordinates or claiming fleet coverage",
    )
    first_entry = first_entries[0]
    check(
        first_entry.get("account_id") == account_key(1)
        and first_entry.get("github_installation_id") == exact_installation(1)
        and exact_created_at(
            first_entry.get("github_installation_created_at"),
            1,
        ),
        "graph-empty scan entries retain the exact durable installation tuple",
    )

    check(
        advance(first) is True and advance(first) is False,
        "exact cursor CAS advances one consumed page and rejects the stale reader",
    )
    durable = as_json(one(
        MIG_DSN,
        "SELECT fields FROM core.boot_reconcile_state "
        "WHERE kind='graph_freshness_cursor'",
    ))
    check(
        durable == {"after_account": account_key(100), "cycle": 0},
        "durable cursor is content-free and stores only after_account + cycle",
    )

    second = surface()
    second_entries = second.get("entries") or []
    second_coordinate = next(
        (
            entry.get("coordinate")
            for entry in second_entries
            if entry.get("account_id") == account_key(101)
        ),
        None,
    )
    check(
        len(second_entries) == CAP
        and second_entries[0].get("account_id") == account_key(101)
        and isinstance(second_coordinate, dict)
        and second_coordinate.get("repo") == "rotate/alpha"
        and second_coordinate.get("account_id") == account_key(101)
        and second_coordinate.get("github_installation_id")
        == exact_installation(101)
        and exact_created_at(
            second_coordinate.get("github_installation_created_at"),
            101,
        )
        and second.get("coordinate_count") == 1,
        "the 101st account is reached and a graph-heavy account contributes "
        "exactly one coordinate with its exact installation generation",
    )

    # Finish cycle 0 while collecting the account keys. This reaches page 6,
    # which contains account 501, and then wraps to {NULL,cycle=1}.
    seen = {
        entry.get("account_id")
        for entry in first_entries + second_entries
        if entry.get("account_id")
    }
    current = second
    final_page = None
    for _ in range(10):
        if current.get("coverage_complete") is True:
            final_page = current
            break
        check(advance(current), "intermediate freshness page CAS advances")
        current = surface()
        seen.update(
            entry.get("account_id")
            for entry in (current.get("entries") or [])
            if entry.get("account_id")
        )
    if final_page is None and current.get("coverage_complete") is True:
        final_page = current

    final_page = final_page or {}
    after_500_coordinate = next(
        (
            entry.get("coordinate")
            for entry in (final_page.get("entries") or [])
            if entry.get("account_id") == account_key(501)
        ),
        None,
    )
    check(
        len(seen) == ACCOUNT_COUNT
        and account_key(501) in seen
        and isinstance(after_500_coordinate, dict)
        and after_500_coordinate.get("repo") == "after-five-hundred/repo"
        and after_500_coordinate.get("github_installation_id")
        == exact_installation(501),
        "bounded pages cover all 520 accounts, including account 501 beyond "
        "the former 500-row edge",
    )
    check(
        final_page.get("coverage_complete") is True
        and final_page.get("capped") is False
        and final_page.get("expected_cursor") == {
            "after_account": account_key(500),
            "cycle": 0,
        }
        and final_page.get("next_cursor") == {
            "after_account": None,
            "cycle": 1,
        }
        and len(final_page.get("entries") or []) == 20,
        "the tail page is honest and wraps to a null high-water at cycle 1",
    )

    cycle_one_first = complete_cycle_from(final_page)
    check(
        cycle_one_first.get("expected_cursor") == {
            "after_account": None,
            "cycle": 1,
        },
        "the durable wrap starts the next full-fleet cycle at account 1",
    )
    check(advance(cycle_one_first), "cycle 1 empty-prefix page advances")
    cycle_one_second = surface()
    cycle_one_repo = selected_repo(cycle_one_second, account_key(101))

    cycle_two_first = complete_cycle_from(cycle_one_second)
    check(advance(cycle_two_first), "cycle 2 empty-prefix page advances")
    cycle_two_second = surface()
    cycle_two_repo = selected_repo(cycle_two_second, account_key(101))
    check(
        [
            second_coordinate.get("repo"),
            cycle_one_repo,
            cycle_two_repo,
        ] == ["rotate/alpha", "rotate/beta", "rotate/gamma"],
        "cycle % graph_count rotates the three-repository account through "
        "all coordinates without monopolizing a page",
    )

    app_privileges = one(
        MIG_DSN,
        "SELECT "
        "has_function_privilege("
        "'veripsa_app','core.owner_graph_freshness_surface(integer)',"
        "'EXECUTE')"
        " AND has_function_privilege("
        "'veripsa_app',"
        "'core.advance_graph_freshness_cursor_with_authority"
        "(text,bigint,text,bigint)','EXECUTE')",
    )
    buyer_denied = denied(
        "SELECT core.owner_graph_freshness_surface(1)"
    ) and denied(
        "SELECT core.advance_graph_freshness_cursor_with_authority"
        "(NULL,0,'ACCT-GH-000001',0)"
    )
    check(
        app_privileges is True and buyer_denied,
        "freshness producer/CAS are App-executable and buyer roles are denied",
    )

    print(
        "GRAPH FRESHNESS CURSOR GATE:",
        "PASS" if all(checks) else "FAIL",
    )
    return 0 if all(checks) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(
            ["dropdb", "--if-exists", DB],
            capture_output=True,
            text=True,
        )
