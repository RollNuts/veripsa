#!/usr/bin/env python3
"""Dead-instance bounded lease reclaim: an ungracefully-orphaned 'processing' row is reclaimed after the valid
event ceiling instead of the full 1800s stale window, while a slow handler survives missed heartbeats.

Root cause fixed: a 'processing' lease was judged stale ONLY by wall-clock age, with no owner-liveness signal;
#840's early expiry runs only from the graceful SIGTERM path, so a SIGKILL/OOM/host-loss left the row frozen —
and its whole account/repo causal lane frozen behind it — for the full window. This adds a per-boot worker
heartbeat + a reaper that backdates locked_at (the proven #840 LEAST() mechanism) ONLY when the owner stopped
beating AND the exact lease is older than event-total + terminal/clock margin. Strictly additive:
pending()/claim() predicates are unchanged; a NULL owner_instance is never fast-reaped.

Time-independent assertions: we compare locked_at BEFORE vs AFTER a reap (backdated => decreased) rather than
sleeping out a real window. Drives a REAL scratch Postgres.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
from psycopg2 import errorcodes  # noqa: E402
import server as S  # noqa: E402
import server_http as SH  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402

DB = "veripsa_deadreclaim_" + str(os.getpid())
ADMIN = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"

checks: list[tuple[str, bool]] = []


def chk(label: str, cond) -> None:
    checks.append((label, bool(cond)))
    print(("  [PASS] " if cond else "  [FAIL] ") + label)


def _admin(sql, args=()):
    conn = psycopg2.connect(ADMIN, dbname=DB)
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


def _locked_at(key):
    return _admin("SELECT locked_at FROM core.webhook_delivery WHERE delivery_key=%s", (key,))


def _bootstrap():
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        sys.exit(2)
    # A second real tenant (+ its login agent role) so the cross-tenant authority refusal is tested against a
    # genuine buyer role, not an empty other side (mirrors the durable-inbox gate's two-tenant proof).
    subprocess.run(["psql", f"postgresql://veripsa_migrator@localhost/{DB}", "-v", "ON_ERROR_STOP=1", "-q", "-c",
                    "SET search_path=core; SELECT core.provision_seat('ACCT-ACME','Acme Co','AG-ACME','acme',"
                    "'veripsa_acme_agent');"], cwd=ROOT, capture_output=True, text=True)
    seed_live_installation(APP_DSN, f"postgresql://veripsa_migrator@localhost/{DB}", 7777, 4242)


def _teardown():
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


def _claim_row(store, key, account):
    push = {"repository": {"full_name": f"acme/{account}", "default_branch": "main", "owner": {"id": 7777}},
            "ref": "refs/heads/main", "after": "a" * 40, "pusher": {"name": "ann"},
            "sender": {"login": "ann", "type": "User"}, "commits": []}
    sub = store.submit("push", push, key, account_key=account, repo=f"acme/{account}")
    assert sub.get("accepted"), sub
    claimed = store.claim(key)
    assert claimed.get("claimed"), claimed
    return claimed["lease_generation"]


def main() -> int:
    _bootstrap()
    try:
        # stale window 1800s (default) so a freshly-claimed row is well inside it. The reaper may backdate only
        # after the valid-event ceiling; ordinary stale reclaim remains the 1800s fallback.
        store = S.DeliveryStore(APP_DSN, max_pending=50, max_attempts=3, stale_seconds=1800)
        liveness_config = store.liveness_snapshot()
        safe_seconds = int(liveness_config["reclaim_safe_seconds"])
        configured_floor = (
            int(liveness_config["event_total_seconds"])
            + int(liveness_config["terminal_reserve_seconds"])
            + int(liveness_config["clock_margin_seconds"])
        )
        chk("dead-owner reclaim ceiling derives from event total + terminal reserve + clock margin",
            safe_seconds >= configured_floor and safe_seconds > 90)

        # ── OWN + HEARTBEAT ────────────────────────────────────────────────────────────────────────────────
        gen = _claim_row(store, "dead-1", "acct-dead")
        owner = _admin("SELECT owner_instance FROM core.webhook_delivery WHERE delivery_key=%s", ("dead-1",))
        beat = _admin("SELECT count(*) FROM core.webhook_worker_instance WHERE instance_id=%s",
                      (store._instance_id,))
        chk("exact owner stamp atomically creates THIS worker's heartbeat before a handler can start",
            owner == store._instance_id and beat == 1)

        # ── (A) ALIVE worker is NEVER reaped ──────────────────────────────────────────────────────────────
        # Make the exact lease older than the safe ceiling: heartbeat freshness, not lease age alone, keeps it live.
        _admin(
            "UPDATE core.webhook_delivery SET locked_at=now()-make_interval(secs=>%s) "
            "WHERE delivery_key=%s",
            (safe_seconds + 1, "dead-1"),
        )
        store.beat_instance()  # fresh heartbeat = provably alive
        before_alive = _locked_at("dead-1")
        reaped_alive = store.reap_dead_instances(dead_seconds=15)
        after_alive = _locked_at("dead-1")
        chk("a slow-but-ALIVE worker's lease is left untouched (reaped=0, locked_at unchanged)",
            reaped_alive == 0 and after_alive == before_alive)

        # ── (B) MISSED HEARTBEATS CANNOT OVERLAP A VALID 90s HANDLER ──────────────────────────────────────
        _admin("UPDATE core.webhook_worker_instance SET last_heartbeat = now() - interval '120 seconds' "
               "WHERE instance_id=%s", (store._instance_id,))
        _admin(
            "UPDATE core.webhook_delivery SET locked_at=now()-make_interval(secs=>%s) "
            "WHERE delivery_key=%s",
            (int(liveness_config["event_total_seconds"]), "dead-1"),
        )
        before_safe = _locked_at("dead-1")
        reaped_before_safe = store.reap_dead_instances(dead_seconds=15)
        after_before_safe = _locked_at("dead-1")
        peer_owner = "wk-" + ("f" * 32)
        peer_before_safe = _admin(
            "SELECT core.claim_webhook_delivery_with_authority(%s,1800,3,3,%s,120)",
            ("dead-1", peer_owner),
        )
        chk("3+ missed beats cannot reap or peer-claim a valid handler before the safe ceiling",
            reaped_before_safe == 0
            and after_before_safe == before_safe
            and not peer_before_safe.get("claimed"))

        # Once the exact lease crosses that absolute ceiling, the same dead owner becomes immediately
        # stale-claimable instead of waiting the full 1800-second ordinary window.
        _admin(
            "UPDATE core.webhook_delivery SET locked_at=now()-make_interval(secs=>%s) "
            "WHERE delivery_key=%s",
            (safe_seconds + 1, "dead-1"),
        )
        before_dead = _locked_at("dead-1")
        reaped_dead = store.reap_dead_instances(dead_seconds=15)
        after_dead = _locked_at("dead-1")
        still_processing = _admin("SELECT status FROM core.webhook_delivery WHERE delivery_key=%s", ("dead-1",))
        peer_after_safe = _admin(
            "SELECT core.claim_webhook_delivery_with_authority(%s,1800,3,3,%s,120)",
            ("dead-1", peer_owner),
        )
        chk("a dead owner becomes reclaimable immediately after the safe ceiling, not after 1800s",
            reaped_dead >= 1 and after_dead < before_dead and still_processing == "processing")
        chk("a peer can claim only after the dead-owner safe ceiling",
            peer_after_safe.get("claimed") is True)

        # ── (C) ABSENT instance row (heartbeat gone) is treated as dead ───────────────────────────────────
        gen2 = _claim_row(store, "gone-1", "acct-gone")
        _ = gen2
        _admin("DELETE FROM core.webhook_worker_instance WHERE instance_id=%s", (store._instance_id,))
        _admin(
            "UPDATE core.webhook_delivery SET locked_at=now()-make_interval(secs=>%s) "
            "WHERE delivery_key=%s",
            (safe_seconds + 1, "gone-1"),
        )
        before_gone = _locked_at("gone-1")
        store.reap_dead_instances(dead_seconds=15)
        after_gone = _locked_at("gone-1")
        chk("a row whose owner instance row is GONE is reclaimed (locked_at backdated)",
            after_gone < before_gone)

        # ── (D) PRE-STAMP NONCE + ABSENT FIRST HEARTBEAT IS NOT FALSE-REAPED ──────────────────────────────
        # This is the rolling-start race: /5 claim commits its nonce before the caller receives the response and
        # stamps the stable owner. A peer reaper may run in that gap. Missing heartbeat alone is not proof of death
        # until the nonce's encoded ambiguity allowance expires.
        nonce_key = "nonce-first-beat"
        nonce_push = {
            "repository": {
                "full_name": "acme/acct-nonce",
                "default_branch": "main",
                "owner": {"id": 7777},
            },
            "ref": "refs/heads/main",
            "after": "c" * 40,
            "pusher": {"name": "ann"},
            "sender": {"login": "ann", "type": "User"},
            "commits": [],
        }
        assert store.submit(
            "push", nonce_push, nonce_key, account_key="acct-nonce",
            repo="acme/acct-nonce").get("accepted")
        store.beat_instance()
        _admin("UPDATE core.webhook_worker_instance SET last_heartbeat=now()-interval '120 seconds' "
               "WHERE instance_id=%s", (store._instance_id,))
        nonce_owner = f"{store._instance_id}.0095.{'a' * 22}"
        nonce_claim = _admin(
            "SELECT core.claim_webhook_delivery_with_authority(%s,1800,3,2,%s)",
            (nonce_key, nonce_owner),
        )
        assert nonce_claim.get("claimed"), nonce_claim
        before_nonce = _locked_at(nonce_key)
        store.reap_dead_instances(dead_seconds=15)
        after_fresh_nonce = _locked_at(nonce_key)
        chk("a stale boot heartbeat cannot accelerate a fresh pre-handler claim nonce",
            after_fresh_nonce == before_nonce)
        _admin(
            "UPDATE core.webhook_delivery SET locked_at=now()-interval '90 seconds' "
            "WHERE delivery_key=%s",
            (nonce_key,),
        )
        before_nonce_deadline = _locked_at(nonce_key)
        store.reap_dead_instances(dead_seconds=15)
        after_nonce_deadline = _locked_at(nonce_key)
        chk("a stale heartbeat still cannot reap a nonce before its encoded allowance",
            after_nonce_deadline == before_nonce_deadline)
        _admin(
            "UPDATE core.webhook_delivery SET locked_at=now()-interval '96 seconds' "
            "WHERE delivery_key=%s",
            (nonce_key,),
        )
        before_expired_nonce = _locked_at(nonce_key)
        store.reap_dead_instances(dead_seconds=15)
        after_expired_nonce = _locked_at(nonce_key)
        chk("the same absent nonce becomes reclaimable only after its encoded ambiguity window",
            after_expired_nonce < before_expired_nonce)

        # ── (E) NULL owner_instance is NEVER fast-reaped (ownerless compatibility row) ─────────────────────
        push = {"repository": {"full_name": "acme/acct-null", "default_branch": "main",
                               "owner": {"id": 7777}},
                "ref": "refs/heads/main", "after": "b" * 40, "pusher": {"name": "ann"},
                "sender": {"login": "ann", "type": "User"}, "commits": []}
        assert store.submit("push", push, "null-1", account_key="acct-null",
                            repo="acme/acct-null").get("accepted")
        ownerless_claim = _admin(
            "SELECT core.claim_webhook_delivery_with_authority("
            "%s,1800,3,3,NULL,120)",
            ("null-1",),
        )
        assert ownerless_claim.get("claimed"), ownerless_claim
        null_owner = _admin("SELECT owner_instance FROM core.webhook_delivery WHERE delivery_key=%s", ("null-1",))
        _admin(
            "UPDATE core.webhook_delivery SET locked_at=now()-make_interval(secs=>%s) "
            "WHERE delivery_key=%s",
            (safe_seconds + 1, "null-1"),
        )
        before_null = _locked_at("null-1")
        store.reap_dead_instances(dead_seconds=15)  # no live heartbeat exists at this point either
        after_null = _locked_at("null-1")
        chk("a NULL-owner row keeps today's full-window behavior (never fast-reaped)",
            null_owner is None and after_null == before_null)

        # ── (F) cross-tenant authority: only veripsa_app may run the reaper/heartbeat/stamp ───────────────
        # A buyer/second-tenant role must be REFUSED EXECUTE (42501), like every other *_with_authority fn.
        refused = False
        try:
            conn = psycopg2.connect(f"postgresql://veripsa_acme_agent@localhost/{DB}")
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT core.reap_dead_instance_leases_with_authority(15,1800,%s)",
                        (safe_seconds,),
                    )
            finally:
                conn.close()
        except psycopg2.Error as e:
            refused = (e.pgcode == errorcodes.INSUFFICIENT_PRIVILEGE)
        chk("a non-operator tenant role is REFUSED EXECUTE on the reaper (42501)", refused)

        # ── (G) OWNER-LIVENESS DIAGNOSTICS ARE PRESENT ON THE OPERATOR HEALTH SURFACE ─────────────────────
        thread = S.start_instance_liveness_loop(store, interval=60)
        live = store.liveness_snapshot()
        with store._liveness_state_lock:
            store._last_successful_heartbeat_at = (
                time.monotonic() - live["heartbeat_dead_seconds"] - 1)
        missed_beats = store.liveness_snapshot()
        with store._liveness_state_lock:
            store._last_successful_heartbeat_at = (
                time.monotonic() - safe_seconds - 1)
        heartbeat_unsafe = store.liveness_snapshot()
        store.beat_instance()
        health = SH._health_snapshot_with_liveness(
            lambda _worker: {"healthy": True}, object(), store)
        chk("dedicated liveness thread + last successful heartbeat are observable",
            thread.is_alive()
            and live.get("thread_alive") is True
            and isinstance(live.get("last_successful_heartbeat_seconds"), (int, float)))
        chk("a live liveness thread does not flap health after only the 15s suspicion threshold",
            missed_beats.get("healthy") is True)
        chk("a live thread with no successful beat by the reclaim-safe ceiling fails health",
            heartbeat_unsafe.get("healthy") is False)
        chk("/healthz composition exposes content-free delivery owner liveness",
            health.get("delivery_liveness", {}).get("thread_alive") is True
            and health["delivery_liveness"].get("reclaim_safe_seconds") == safe_seconds)

    finally:
        _teardown()

    ok = all(c for _, c in checks)
    print("\nDEAD INSTANCE RECLAIM GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
