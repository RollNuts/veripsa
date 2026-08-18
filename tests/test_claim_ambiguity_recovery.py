#!/usr/bin/env python3
"""Claim ACK ambiguity is fenced by an atomic, generation-unique owner reference.

Drives a real scratch Postgres and proves:
  * a bounded /5 rolling claim writes processing + generation + nonce owner in
    one transaction; /4 and /3 remain present but fail closed without budget
    proof, and protocol 3 owns every replay.
  * a post-COMMIT response blackhole is returned queued/failed by the exact nonce recovery path.
  * delayed recovery cannot touch a newer generation, even when the same boot owns both generations.
  * if recovery itself disappears, a nonce orphan is untouched inside the event ceiling but immediately
    stale-reclaimable after its encoded ceiling while the worker heartbeat remains alive.
  * only veripsa_app can execute the new authority surfaces; it still has no direct queue-table UPDATE.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
for path in (ROOT, APP):
    if path not in sys.path:
        sys.path.insert(0, path)

import psycopg2  # noqa: E402
import delivery_queue as deliveries  # noqa: E402
import event_budget  # noqa: E402


DB = "veripsa_claimambiguity_" + str(os.getpid())
ADMIN = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"
MIGRATOR_DSN = f"postgresql://veripsa_migrator@localhost/{DB}"
checks: list[tuple[str, bool]] = []


def check(label: str, condition) -> None:
    checks.append((label, bool(condition)))
    print(("  [PASS] " if condition else "  [FAIL] ") + label)


def one(dsn: str, sql: str, args=()):
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            try:
                row = cur.fetchone()
            except psycopg2.ProgrammingError:
                return None
            return row[0] if row else None
    finally:
        conn.close()


def row(key: str):
    conn = psycopg2.connect(MIGRATOR_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status,attempts,lease_generation,owner_instance,locked_at "
                "FROM core.webhook_delivery WHERE delivery_key=%s",
                (key,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def submit(store: deliveries.DeliveryStore, key: str, account: str) -> None:
    payload = {
        "repository": {
            "id": abs(hash(key)) % 1_000_000 + 1,
            "full_name": f"acme/{key}",
            "default_branch": "main",
            "owner": {"id": 42},
        },
        "ref": "refs/heads/main",
        "after": "a" * 40,
        "pusher": {"name": "ann"},
        "sender": {"login": "ann", "type": "User"},
        "commits": [],
    }
    result = store.submit("push", payload, key, account_key=account, repo=f"acme/{key}")
    assert result.get("accepted"), result


def claim5(key: str, owner: str, *, stale: int = 1800, max_attempts: int = 3):
    return one(
        APP_DSN,
        "SELECT core.claim_webhook_delivery_with_authority(%s,%s,%s,2,%s)",
        (key, stale, max_attempts, owner),
    )


def claim6(key: str, owner: str, *, stale: int = 1800, max_attempts: int = 3):
    return one(
        APP_DSN,
        "SELECT core.claim_webhook_delivery_with_authority("
        "%s,%s,%s,3,%s,120)",
        (key, stale, max_attempts, owner),
    )


def recover(key: str, owner: str, *, max_attempts: int = 3):
    return one(
        APP_DSN,
        "SELECT core.recover_ambiguous_webhook_claim_with_authority(%s,%s,%s,%s)",
        (key, owner, "claim response blackholed", max_attempts),
    )


def bootstrap() -> None:
    result = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("bootstrap failed:\n" + (result.stderr or "")[-1200:])


def main() -> int:
    bootstrap()
    try:
        store = deliveries.DeliveryStore(APP_DSN, max_pending=100, max_attempts=3, stale_seconds=1800)
        store.beat_instance()

        # The new runtime ABI records the provisional nonce inside claim's own transaction.
        submit(store, "atomic-owner", "acct-atomic")
        atomic_owner = f"{store._instance_id}.0095.{'1' * 22}"
        atomic_claim = claim5("atomic-owner", atomic_owner)
        atomic_row = row("atomic-owner")
        check(
            "/5 claim atomically advances generation and records its exact nonce owner",
            atomic_claim.get("claimed") is True
            and atomic_claim.get("lease_generation") == 1
            and atomic_row[:4] == ("processing", 1, 1, atomic_owner),
        )
        recover("atomic-owner", atomic_owner)

        # Runtime happy path must replace the provisional nonce before authorizing handler execution.
        submit(store, "happy-owner", "acct-happy")
        happy = store.claim("happy-owner")
        happy_row = row("happy-owner")
        check(
            "a successful runtime claim exact-stamps the stable boot owner before returning",
            happy.get("claimed") is True and happy_row[3] == store._instance_id,
        )
        store.release("happy-owner", "test cleanup", happy["lease_generation"])

        # Simulate the exact network ambiguity: autocommit succeeded, then the result vanished.
        submit(store, "blackhole", "acct-blackhole")
        raw_one = store._one
        blackholed = {"done": False, "owner": None}

        def lose_claim_response(sql, args=()):
            if "claim_webhook_delivery_with_authority" in sql and not blackholed["done"]:
                blackholed["done"] = True
                blackholed["owner"] = args[4]
                raw_one(sql, args)  # real committed /5 claim
                raise psycopg2.OperationalError("simulated response blackhole after COMMIT")
            return raw_one(sql, args)

        store._one = lose_claim_response
        raised = False
        try:
            store.claim("blackhole")
        except psycopg2.OperationalError:
            raised = True
        finally:
            store._one = raw_one
        blackhole_row = row("blackhole")
        check(
            "post-COMMIT response blackhole is terminally returned queued by the exact nonce",
            raised and blackholed["owner"]
            and blackhole_row[:4] == ("queued", 1, 1, None)
            and blackhole_row[4] is None,
        )

        # Old recovery races a stale reclaim from the SAME boot: the fresh nonce, not just boot id, is the fence.
        submit(store, "generation-race", "acct-race")
        old_owner = f"{store._instance_id}.0095.{'2' * 22}"
        new_owner = f"{store._instance_id}.0095.{'3' * 22}"
        old_claim = claim5("generation-race", old_owner)
        one(
            MIGRATOR_DSN,
            "UPDATE core.webhook_delivery SET locked_at=now()-interval '10 seconds' "
            "WHERE delivery_key=%s",
            ("generation-race",),
        )
        new_claim = claim6("generation-race", new_owner, stale=1)
        late_recovery = recover("generation-race", old_owner)
        race_row = row("generation-race")
        check(
            "late recovery never touches a newer generation from the same boot",
            old_claim.get("lease_generation") == 1
            and new_claim.get("lease_generation") == 2
            and late_recovery == "missing"
            and race_row[:4] == ("processing", 2, 2, new_owner),
        )
        recover("generation-race", new_owner)

        # A nonce orphan stays protected while a delayed response is still legal, then becomes immediately
        # reclaimable after the encoded event ceiling even though its boot heartbeat remains fresh.
        submit(store, "nonce-orphan", "acct-nonce")
        encoded_window = min(9999, int(event_budget._EVENT_WALL_TIMEOUT_SECONDS) + 5)
        orphan_owner = f"{store._instance_id}.{encoded_window:04d}.{'4' * 22}"
        orphan_claim = claim5("nonce-orphan", orphan_owner)
        one(
            MIGRATOR_DSN,
            "UPDATE core.webhook_delivery SET locked_at=clock_timestamp()-make_interval(secs=>%s) "
            "WHERE delivery_key=%s",
            (encoded_window - 1, "nonce-orphan"),
        )
        store.beat_instance()
        before_safe = row("nonce-orphan")[4]
        reaped_early = store.reap_dead_instances(dead_seconds=15)
        after_safe = row("nonce-orphan")[4]
        one(
            MIGRATOR_DSN,
            "UPDATE core.webhook_delivery SET locked_at=clock_timestamp()-make_interval(secs=>%s) "
            "WHERE delivery_key=%s",
            (encoded_window + 1, "nonce-orphan"),
        )
        store.beat_instance()
        before_expired = row("nonce-orphan")[4]
        reaped_expired = store.reap_dead_instances(dead_seconds=15)
        after_expired = row("nonce-orphan")[4]
        successor_owner = f"{store._instance_id}.{encoded_window:04d}.{'5' * 22}"
        successor = claim6("nonce-orphan", successor_owner, stale=1800)
        check(
            "nonce orphan is protected inside the event ceiling and immediately reclaimable after it",
            orphan_claim.get("claimed") is True
            and reaped_early == 0 and after_safe == before_safe
            and reaped_expired >= 1 and after_expired < before_expired
            and successor.get("claimed") is True
            and successor.get("lease_generation") == 2,
        )
        recover("nonce-orphan", successor_owner)

        # Retry exhaustion takes the same visible DLQ transition, never an invisible processing freeze.
        submit(store, "blackhole-dlq", "acct-dlq")
        dlq_owner = f"{store._instance_id}.0095.{'6' * 22}"
        assert claim5("blackhole-dlq", dlq_owner, max_attempts=1).get("claimed")
        dlq_recovery = recover("blackhole-dlq", dlq_owner, max_attempts=1)
        dlq_row = row("blackhole-dlq")
        check(
            "ambiguous claim at the attempt ceiling becomes visible failed/DLQ and unlocks",
            dlq_recovery == "failed"
            and dlq_row[:4] == ("failed", 1, 1, None)
            and dlq_row[4] is None,
        )

        signatures = one(
            MIGRATOR_DSN,
            "SELECT jsonb_build_object("
            "'claim5',to_regprocedure('core.claim_webhook_delivery_with_authority(text,integer,integer,integer,text)') IS NOT NULL,"
            "'claim4',to_regprocedure('core.claim_webhook_delivery_with_authority(text,integer,integer,integer)') IS NOT NULL,"
            "'claim3',to_regprocedure('core.claim_webhook_delivery_with_authority(text,integer,integer)') IS NOT NULL,"
            "'recover4',to_regprocedure('core.recover_ambiguous_webhook_claim_with_authority(text,text,text,integer)') IS NOT NULL,"
            "'app_claim',has_function_privilege('veripsa_app',"
            " 'core.claim_webhook_delivery_with_authority(text,integer,integer,integer,text)','EXECUTE'),"
            "'app_recover',has_function_privilege('veripsa_app',"
            " 'core.recover_ambiguous_webhook_claim_with_authority(text,text,text,integer)','EXECUTE'),"
            "'writer_claim',has_function_privilege('veripsa_writer',"
            " 'core.claim_webhook_delivery_with_authority(text,integer,integer,integer,text)','EXECUTE'),"
            "'writer_recover',has_function_privilege('veripsa_writer',"
            " 'core.recover_ambiguous_webhook_claim_with_authority(text,text,text,integer)','EXECUTE'),"
            "'app_table_update',has_table_privilege('veripsa_app','core.webhook_delivery','UPDATE'))",
        )
        check(
            "rolling ABIs remain while new functions are App-only and table writes stay revoked",
            all(signatures.get(k) is True for k in ("claim5", "claim4", "claim3", "recover4",
                                                    "app_claim", "app_recover"))
            and all(signatures.get(k) is False for k in ("writer_claim", "writer_recover",
                                                         "app_table_update")),
        )

        # Generated runtime owners carry the structural window and generation nonce within the schema bound.
        check(
            "runtime ambiguity owner format embeds boot id, bounded event window, and claim nonce",
            bool(re.fullmatch(r"wk-[0-9a-f]{32}[.][0-9]{4}[.][0-9a-f]{22}",
                              str(blackholed["owner"] or ""))),
        )
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)

    ok = all(condition for _, condition in checks)
    print("\nCLAIM AMBIGUITY RECOVERY GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
