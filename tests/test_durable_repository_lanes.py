#!/usr/bin/env python3
"""Real-Postgres proof for durable repository lanes and account-wide barriers.

The in-memory keyed worker pool is only safe if recovery makes the same causal
decision.  This gate exercises pending(), claim(), failed/DLQ blockers, and the
private lane classifier through the App role against a freshly bootstrapped DB.
"""
from __future__ import annotations

import os
import subprocess
import sys

import psycopg2
from psycopg2.extras import Json


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
from event_queue import _repository_lane  # noqa: E402

DB = "veripsa_durable_lanes_" + str(os.getpid())
MIGRATOR_DSN = f"postgresql://veripsa_migrator@localhost/{DB}"
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"
FAIL = 0
OWNER_SEQ = 0


def check(ok: bool, message: str) -> None:
    global FAIL
    print(("PASS" if ok else "FAIL") + ": " + message)
    if not ok:
        FAIL += 1


def one(dsn: str, sql: str, args=()):
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, args)
            try:
                row = cur.fetchone()
            except psycopg2.ProgrammingError:
                return None
            return row[0] if row else None
    finally:
        conn.close()


def reset() -> None:
    one(MIGRATOR_DSN, "TRUNCATE core.webhook_delivery")


def payload(repository_id=...):
    if repository_id is ...:
        return {"repository": {"id": 101}, "ref": "refs/heads/main", "after": "a" * 40}
    if repository_id is None:
        return {"ref": "refs/heads/main", "after": "b" * 40}
    return {"repository": {"id": repository_id}, "ref": "refs/heads/main", "after": "c" * 40}


def insert(
    key: str,
    event_type: str,
    repository_id,
    *,
    account: str = "tenant-A",
    seconds_ago: int,
    status: str = "queued",
    updated_seconds_ago: int | None = None,
    repo_name: str | None = None,
) -> None:
    body = payload(repository_id)
    if repo_name and isinstance(body.get("repository"), dict):
        body["repository"]["full_name"] = repo_name
    one(
        MIGRATOR_DSN,
        """
        INSERT INTO core.webhook_delivery(
          delivery_key,event_type,account_key,repo,payload,status,attempts,
          received_at,updated_at,locked_at,causal_order_version
        ) VALUES (
          %s,%s,%s,%s,%s,%s,0,
          now()-make_interval(secs=>%s),
          now()-make_interval(secs=>%s),
          CASE WHEN %s='processing' THEN now() ELSE NULL END,
          1
        )
        """,
        (
            key,
            event_type,
            account,
            repo_name or f"owner/repo-{repository_id}",
            Json(body),
            status,
            seconds_ago,
            seconds_ago if updated_seconds_ago is None else updated_seconds_ago,
            status,
        ),
    )


def claim(key: str) -> dict:
    global OWNER_SEQ
    OWNER_SEQ += 1
    result = one(
        APP_DSN,
        "SELECT core.claim_webhook_delivery_with_authority(%s,600,8,3,%s,120)",
        (key, f"durable-lane-test-{OWNER_SEQ}"),
    )
    return result if isinstance(result, dict) else {}


def finish(key: str, result: dict) -> bool:
    return bool(
        one(
            APP_DSN,
            "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
            (key, result["lease_generation"]),
        )
    )


def pending() -> set[str]:
    rows = one(APP_DSN, "SELECT core.pending_webhook_deliveries_with_authority(100,600,8)")
    return {row["key"] for row in (rows or [])}


def bootstrap() -> None:
    result = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        print(result.stdout[-1500:])
        print(result.stderr[-3000:], file=sys.stderr)
        raise SystemExit(2)


def teardown() -> None:
    subprocess.run(["dropdb", DB], text=True, capture_output=True)


def main() -> int:
    bootstrap()
    try:
        # The classifier is private and its SQL definition matches the live scheduler's positive-id rule.
        lane_corpus = [
            ("push", {"repository": {"id": 42}}, "42"),
            ("push", {"repository": {"id": "42"}}, "42"),
            ("push", {"repository": {"id": 0}}, None),
            ("push", {"repository": {"id": "0"}}, None),
            ("push", {"repository": {"id": -1}}, None),
            ("push", {"repository": {"id": True}}, None),
            ("push", {"repository": {"id": "not-a-number"}}, None),
            ("push", {"repository": {"id": "042"}}, None),
            ("push", {"repository": {"id": " 42 "}}, None),
            ("push", {"repository": {"id": "١٢"}}, None),
            ("push", {"repository": {"id": "1" * 32}}, "1" * 32),
            ("push", {"repository": {"id": "1" * 33}}, None),
            ("push", {"repository": {}}, None),
            ("installation", {"repository": {"id": 42}}, None),
            ("unknown_event", {"repository": {"id": 42}}, None),
        ]
        lane_results = []
        for event_type, body, expected in lane_corpus:
            memory_lane = _repository_lane((event_type, body, None))
            sql_lane = one(
                MIGRATOR_DSN,
                "SELECT core._webhook_delivery_repository_lane(%s,%s)",
                (event_type, Json(body)),
            )
            lane_results.append((memory_lane, sql_lane, expected))
        acl_ok = one(
            MIGRATOR_DSN,
            """
            SELECT NOT has_function_privilege(
              'veripsa_app','core._webhook_delivery_repository_lane(text,jsonb)','EXECUTE')
              AND NOT has_function_privilege(
              'public','core._webhook_delivery_repository_lane(text,jsonb)','EXECUTE')
            """,
        )
        check(
            all(memory == sql == expected for memory, sql, expected in lane_results) and acl_ok,
            "memory and private SQL classifier agree on one positive-ASCII-decimal payload corpus",
        )

        # A processing repository is deliberately "slow"; a second repository in the account remains claimable.
        reset()
        insert("parallel-a", "push", 101, seconds_ago=30)
        insert("parallel-b", "push", 202, seconds_ago=20)
        a = claim("parallel-a")
        recovery_keys = pending()
        b = claim("parallel-b")
        check(
            a.get("claimed") is True
            and "parallel-b" in recovery_keys
            and b.get("claimed") is True,
            "repo B is pending and claimable while repo A is still processing (live/recovery mix)",
        )
        finish("parallel-a", a)
        finish("parallel-b", b)

        # Rename/account transfer changes the coordinate and account key, but not GitHub's stable repository id.
        # The global repository head therefore stays FIFO across both account buckets.
        reset()
        insert(
            "transfer-old",
            "repository",
            250,
            account="old-owner",
            seconds_ago=30,
            repo_name="old/name",
        )
        insert(
            "transfer-new",
            "repository",
            250,
            account="new-owner",
            seconds_ago=20,
            repo_name="new/name",
        )
        transfer_old = claim("transfer-old")
        transfer_early = claim("transfer-new")
        finish("transfer-old", transfer_old)
        transfer_new = claim("transfer-new")
        check(
            transfer_old.get("claimed") is True
            and transfer_early.get("reason") == "blocked_by_earlier"
            and transfer_new.get("claimed") is True,
            "rename/account transfer stays FIFO by stable repository id, not full_name",
        )
        finish("transfer-new", transfer_new)

        # Same repository stays FIFO even if callers attempt to claim the tail first.
        reset()
        insert("fifo-a", "push", 303, seconds_ago=30)
        insert("fifo-b", "check_run", 303, seconds_ago=20)
        tail_first = claim("fifo-b")
        head = claim("fifo-a")
        tail_during = claim("fifo-b")
        finish("fifo-a", head)
        tail_after = claim("fifo-b")
        check(
            tail_first.get("reason") == "blocked_by_earlier"
            and head.get("claimed") is True
            and tail_during.get("reason") == "blocked_by_earlier"
            and tail_after.get("claimed") is True,
            "same-repository durable work is strict FIFO with no overlap",
        )
        finish("fifo-b", tail_after)

        # repo A -> wide -> repo B: the wide row waits for A and B cannot cross it.
        reset()
        insert("sandwich-a", "push", 401, seconds_ago=40)
        insert("sandwich-wide", "installation", 401, seconds_ago=30)
        insert("sandwich-b", "push", 402, seconds_ago=20)
        sa = claim("sandwich-a")
        wide_early = claim("sandwich-wide")
        sb_early = claim("sandwich-b")
        finish("sandwich-a", sa)
        sw = claim("sandwich-wide")
        sb_during = claim("sandwich-b")
        finish("sandwich-wide", sw)
        sb = claim("sandwich-b")
        check(
            sa.get("claimed") is True
            and wide_early.get("reason") == "blocked_by_earlier"
            and sb_early.get("reason") == "blocked_by_earlier"
            and sw.get("claimed") is True
            and sb_during.get("reason") == "blocked_by_earlier"
            and sb.get("claimed") is True,
            "repo A -> account-wide -> repo B is a two-sided causal barrier",
        )
        finish("sandwich-b", sb)

        # A wide head blocks every later repository until it finishes.
        reset()
        insert("wide-head", "marketplace_purchase", 501, seconds_ago=30)
        insert("wide-tail", "push", 502, seconds_ago=20)
        tail_early = claim("wide-tail")
        wh = claim("wide-head")
        tail_during = claim("wide-tail")
        finish("wide-head", wh)
        wt = claim("wide-tail")
        check(
            tail_early.get("reason") == "blocked_by_earlier"
            and wh.get("claimed") is True
            and tail_during.get("reason") == "blocked_by_earlier"
            and wt.get("claimed") is True,
            "account-wide head blocks every later repository in claim order",
        )
        finish("wide-tail", wt)

        # Failed protocol-1 rows retain their causal role, but only in the matching lane.
        reset()
        insert("failed-other", "push", 601, seconds_ago=30, status="failed")
        insert("after-other", "push", 602, seconds_ago=20)
        other_pending = pending()
        other = claim("after-other")
        check(
            "after-other" in other_pending and other.get("claimed") is True,
            "failed row in another valid repository does not block pending or claim",
        )
        finish("after-other", other)

        # Failed barriers are directional: billing failure affects later repository behavior, but a completed
        # repository failure is not an entitlement predecessor of a later Marketplace event in the same account.
        reset()
        insert("failed-repo-before-marketplace", "push", 611, seconds_ago=30, status="failed")
        insert("marketplace-after-failed-repo", "marketplace_purchase", None, seconds_ago=20)
        marketplace_pending = pending()
        marketplace_claim = claim("marketplace-after-failed-repo")
        check(
            "marketplace-after-failed-repo" in marketplace_pending
            and marketplace_claim.get("claimed") is True,
            "Marketplace target does not inherit an unrelated valid-repository DLQ",
        )
        finish("marketplace-after-failed-repo", marketplace_claim)

        reset()
        insert("failed-marketplace-before-repo", "marketplace_purchase", None,
               seconds_ago=30, status="failed")
        insert("repo-after-failed-marketplace", "push", 612, seconds_ago=20)
        repo_after_billing_pending = pending()
        repo_after_billing_claim = claim("repo-after-failed-marketplace")
        check(
            "repo-after-failed-marketplace" not in repo_after_billing_pending
            and repo_after_billing_claim.get("reason") == "blocked_by_earlier",
            "failed Marketplace predecessor remains an account-wide blocker for later repository work",
        )

        reset()
        insert("failed-wide", "installation_repositories", 701, seconds_ago=30, status="failed")
        insert("after-wide-failed", "push", 702, seconds_ago=20)
        wide_failed_pending = pending()
        wide_failed_claim = claim("after-wide-failed")
        check(
            "after-wide-failed" not in wide_failed_pending
            and wide_failed_claim.get("reason") == "blocked_by_earlier",
            "failed account-wide row remains a pending/claim blocker",
        )

        # Missing and malformed IDs are conservative wide rows, never narrow accidental lanes.
        reset()
        insert("missing-id", "push", None, seconds_ago=30)
        insert("after-missing", "push", 801, seconds_ago=20)
        missing_claim = claim("after-missing")
        reset()
        insert("bad-id", "push", "not-a-number", seconds_ago=30)
        insert("after-bad", "push", 802, seconds_ago=20)
        bad_claim = claim("after-bad")
        check(
            missing_claim.get("reason") == "blocked_by_earlier"
            and bad_claim.get("reason") == "blocked_by_earlier",
            "missing or malformed repository.id becomes an account-wide barrier",
        )

        # The aged-DLQ escalation uses exactly the same relationship: another repo's
        # failed row is ignored, while a failed wide row is rearmed to unblock its tail.
        reset()
        insert(
            "aged-other-failed",
            "push",
            901,
            seconds_ago=300,
            updated_seconds_ago=300,
            status="failed",
        )
        insert("aged-other-tail", "push", 902, seconds_ago=200)
        other_escalation = one(
            APP_DSN,
            "SELECT core.escalate_blocked_webhook_deliveries_with_authority(60,100)",
        )
        other_status = one(
            MIGRATOR_DSN,
            "SELECT status FROM core.webhook_delivery WHERE delivery_key='aged-other-failed'",
        )
        reset()
        insert(
            "aged-wide-failed",
            "installation_repositories",
            903,
            seconds_ago=300,
            updated_seconds_ago=300,
            status="failed",
        )
        insert("aged-wide-tail", "push", 904, seconds_ago=200)
        wide_escalation = one(
            APP_DSN,
            "SELECT core.escalate_blocked_webhook_deliveries_with_authority(60,100)",
        )
        wide_status = one(
            MIGRATOR_DSN,
            "SELECT status FROM core.webhook_delivery WHERE delivery_key='aged-wide-failed'",
        )
        check(
            other_escalation.get("escalated") == 0
            and other_status == "failed"
            and wide_escalation.get("escalated") == 1
            and wide_status == "queued",
            "DLQ escalation ignores other repositories and rearms a true wide blocker",
        )
    finally:
        teardown()

    if FAIL:
        return 1
    print("DURABLE REPOSITORY LANES GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
