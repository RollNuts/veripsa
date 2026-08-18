#!/usr/bin/env python3
"""Real-Postgres proof that terminal ACK loss cannot freeze a causal lane.

Release and intentional defer use exact-generation idempotent resolvers.  Two
lost synchronous probes transfer one immutable intent to the fixed-small
process registry; the liveness daemon later drains it outside the event budget.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

import delivery_queue as DQ  # noqa: E402
import event_budget  # noqa: E402
import schema_contract  # noqa: E402


DB = f"veripsa_terminal_exact_{os.getpid()}"
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"
OWNER_DSN = f"postgresql://veripsa_migrator@localhost/{DB}"
FAILURES = 0


def check(condition, label: str) -> None:
    global FAILURES
    print(("PASS: " if condition else "FAIL: ") + label)
    if not condition:
        FAILURES += 1


class FaultStore(DQ.DeliveryStore):
    """Inject faults immediately before or after one real resolver statement."""

    _FRAGMENTS = {
        "release": "resolve_webhook_delivery_release_with_authority",
        "defer": "resolve_webhook_delivery_defer_with_authority",
        "commit": "resolve_webhook_delivery_commit_with_authority",
        "fanout_defer": "resolve_webhook_delivery_fanout_defer_with_authority",
    }

    def __init__(self, dsn: str):
        super().__init__(dsn, max_pending=100, max_attempts=3)
        self.fault_kind = None
        self.fault_modes: list[str] = []
        self.resolver_calls = 0

    def arm(self, kind: str, *modes: str) -> None:
        self.fault_kind = kind
        self.fault_modes = list(modes)
        self.resolver_calls = 0

    def clear_fault(self) -> None:
        self.fault_kind = None
        self.fault_modes = []

    def _one(self, sql: str, args=()):
        kind = self.fault_kind
        if kind and self._FRAGMENTS[kind] in sql:
            self.resolver_calls += 1
            mode = self.fault_modes.pop(0) if self.fault_modes else "none"
            if mode == "pre":
                raise psycopg2.OperationalError(
                    "injected resolver precommit transport loss")
            if mode == "cancel":
                raise event_budget.EventBudgetExceeded(
                    "injected terminal cancellation")
            result = super()._one(sql, args)
            if mode == "post":
                raise psycopg2.OperationalError(
                    "injected resolver postcommit ACK loss")
            return result
        return super()._one(sql, args)


def owner_one(sql: str, args=()):
    conn = psycopg2.connect(OWNER_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def state(key: str):
    conn = psycopg2.connect(OWNER_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status,attempts,lease_generation,locked_at,"
                "owner_instance,not_before,last_error "
                "FROM core.webhook_delivery WHERE delivery_key=%s",
                (key,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def webhook_payload(account: int, repo: str) -> dict:
    return {
        "repository": {
            "id": str(account * 100 + 7),
            "full_name": repo,
            "owner": {"id": account, "login": f"acct-{account}"},
            "default_branch": "main",
        },
        "after": "a" * 40,
        "ref": "refs/heads/main",
        "commits": [],
    }


def enqueue(store: FaultStore, key: str, account: int, repo: str):
    result = store.submit(
        "push", webhook_payload(account, repo), key,
        account_key=str(account), repo=repo,
    )
    assert result.get("accepted"), result
    return result


def claim(store: FaultStore, key: str) -> int:
    result = store.claim(key)
    assert result.get("claimed"), result
    return int(result["lease_generation"])


def bootstrap() -> None:
    result = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError("bootstrap failed:\n" + (result.stderr or "")[-1200:])


def resolver_contract(name: str, signature: str) -> dict:
    return owner_one(
        "SELECT jsonb_build_object("
        "'security_definer',p.prosecdef,"
        "'owner',r.rolname,"
        "'fixed_search_path',COALESCE(p.proconfig,'{}'::text[]) "
        "  @> ARRAY['search_path=core, pg_catalog'],"
        "'app_execute',has_function_privilege('veripsa_app',p.oid,'EXECUTE'),"
        "'writer_execute',has_function_privilege('veripsa_writer',p.oid,'EXECUTE'),"
        "'app_update',has_table_privilege('veripsa_app','core.webhook_delivery','UPDATE')) "
        "FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner "
        f"WHERE p.oid='core.{name}{signature}'::regprocedure"
    )


def main() -> int:
    bootstrap()
    try:
        store = FaultStore(APP_DSN)

        # A pre-COMMIT transport loss and the BaseException cancellation path
        # each receive exactly one immediate no-sleep re-probe.
        enqueue(store, "release-pre", 101, "acme/release-pre")
        release_pre_gen = claim(store, "release-pre")
        store.arm("release", "pre", "none")
        started = time.monotonic()
        release_pre = store.release(
            "release-pre", "processor rolled back", release_pre_gen)
        release_pre_elapsed = time.monotonic() - started
        release_pre_state = state("release-pre")
        check(
            release_pre == "queued"
            and store.resolver_calls == 2
            and release_pre_elapsed < 1.0
            and release_pre_state[0:2] == ("queued", 1)
            and release_pre_state[3] is None
            and release_pre_state[4] is None,
            "release precommit transport loss gets one no-sleep exact probe and clears the stable owner",
        )

        enqueue(store, "release-cancel", 102, "acme/release-cancel")
        release_cancel_gen = claim(store, "release-cancel")
        store.arm("release", "cancel", "none")
        release_cancel = store.release(
            "release-cancel", "event budget exceeded", release_cancel_gen)
        check(
            release_cancel == "queued"
            and store.resolver_calls == 2
            and state("release-cancel")[0] == "queued",
            "BaseException cancellation uses the same single exact re-probe",
        )

        # First statement commits and only its ACK disappears. The second
        # resolver observes exact queued state rather than returning a false
        # ownership_lost or minting another handler attempt.
        enqueue(store, "release-post", 103, "acme/release-post")
        release_post_gen = claim(store, "release-post")
        store.arm("release", "post", "none")
        release_post = store.release(
            "release-post", "ordinary processor error", release_post_gen)
        release_post_state = state("release-post")
        release_post_calls = store.resolver_calls
        store.clear_fault()
        mismatch = store._resolve_terminal_once(
            "release", "release-post", release_post_gen,
            ("different error", store.max_attempts),
        )
        check(
            release_post == "queued"
            and release_post_calls == 2
            and release_post_state[0] == "queued"
            and release_post_state[6] == "ordinary processor error"
            and mismatch == "ownership_lost",
            "release postcommit ACK loss is proven only by exact generation and exact arguments",
        )

        # Intentional defer has the same pre/post-COMMIT contract and restores
        # the claimed attempt without replacing the requested schedule.
        defer_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        enqueue(store, "defer-pre", 104, "acme/defer-pre")
        defer_pre_gen = claim(store, "defer-pre")
        store.arm("defer", "pre", "none")
        defer_pre = store.defer(
            "defer-pre", defer_at, "await consistency", defer_pre_gen)
        defer_pre_state = state("defer-pre")
        check(
            defer_pre
            and store.resolver_calls == 2
            and defer_pre_state[0:2] == ("queued", 0)
            and defer_pre_state[3] is None
            and defer_pre_state[4] is None
            and defer_pre_state[5] == defer_at
            and defer_pre_state[6] == "await consistency",
            "defer precommit transport loss re-probes once and preserves the exact schedule attempt-neutrally",
        )

        defer_post_at = defer_at + timedelta(seconds=30)
        enqueue(store, "defer-post", 105, "acme/defer-post")
        defer_post_gen = claim(store, "defer-post")
        store.arm("defer", "post", "none")
        defer_post = store.defer(
            "defer-post", defer_post_at, "rate window", defer_post_gen)
        defer_post_state = state("defer-post")
        defer_post_calls = store.resolver_calls
        store.clear_fault()
        defer_mismatch = store._resolve_terminal_once(
            "defer", "defer-post", defer_post_gen,
            (defer_post_at + timedelta(seconds=1), "rate window"),
        )
        check(
            defer_post
            and defer_post_calls == 2
            and defer_post_state[0:2] == ("queued", 0)
            and defer_post_state[5] == defer_post_at
            and defer_mismatch == "ownership_lost",
            "defer postcommit ACK loss is idempotent and a different schedule cannot forge prior commit",
        )

        # Both inline ACKs disappear after the release has committed. The
        # method returns pending after exactly two calls, and the background
        # resolver later proves that same terminal state without rerunning a
        # handler.
        enqueue(store, "registry-ack", 106, "acme/registry-ack")
        registry_ack_gen = claim(store, "registry-ack")
        store.arm("release", "post", "post")
        registry_result = store.release(
            "registry-ack", "lost twice", registry_ack_gen)
        pending_before = store.liveness_snapshot()
        store.clear_fault()
        drain_ack = store.drain_pending_terminals()
        pending_after = store.liveness_snapshot()
        check(
            registry_result == "pending"
            and store.resolver_calls == 2
            and state("registry-ack")[0] == "queued"
            and pending_before["pending_terminal_depth"] == 1
            and pending_before["pending_terminal_failures"] >= 2
            and drain_ack == {"attempted": 1, "resolved": 1, "pending": 0}
            and pending_after["pending_terminal_depth"] == 0,
            "two resolver ACK losses transfer one exact intent to the registry and later drain it",
        )
        check(
            "registry-ack" not in repr(pending_before)
            and "lost twice" not in repr(pending_before),
            "liveness exposes only content-free terminal depth, age, failures, overflow, and cap",
        )

        # A successful DB round trip is not itself terminal proof.  A missing
        # or unknown resolver outcome receives the same bounded second probe
        # and registry handoff as transport/ACK loss; otherwise the caller
        # could return while its exact row remains processing forever.
        enqueue(store, "registry-missing", 112, "acme/registry-missing")
        registry_missing_gen = claim(store, "registry-missing")
        original_resolve_once = store._resolve_terminal_once
        missing_calls = [0]

        def return_missing(kind, key, generation, args):
            missing_calls[0] += 1
            return "missing"

        store._resolve_terminal_once = return_missing
        registry_missing_result = store.release(
            "registry-missing", "inconclusive outcome",
            registry_missing_gen,
        )
        registry_missing_pending = store.liveness_snapshot()
        registry_missing_processing = state("registry-missing")
        store._resolve_terminal_once = original_resolve_once
        registry_missing_drain = store.drain_pending_terminals()
        check(
            registry_missing_result == "pending"
            and missing_calls[0] == 2
            and registry_missing_processing[0] == "processing"
            and registry_missing_pending["pending_terminal_depth"] == 1
            and registry_missing_drain
            == {"attempted": 1, "resolved": 1, "pending": 0}
            and state("registry-missing")[0] == "queued",
            "two successful-but-missing probes retain and later converge the exact terminal intent",
        )

        enqueue(store, "registry-defer-ack", 110, "acme/registry-defer-ack")
        registry_defer_gen = claim(store, "registry-defer-ack")
        registry_defer_at = defer_post_at + timedelta(seconds=30)
        store.arm("defer", "post", "post")
        registry_defer_result = store.defer(
            "registry-defer-ack",
            registry_defer_at,
            "defer ACK lost twice",
            registry_defer_gen,
        )
        registry_defer_state = state("registry-defer-ack")
        registry_defer_pending = store.liveness_snapshot()
        store.clear_fault()
        registry_defer_drain = store.drain_pending_terminals()
        check(
            registry_defer_result is True
            and store.resolver_calls == 2
            and registry_defer_state[0:2] == ("queued", 0)
            and registry_defer_state[5] == registry_defer_at
            and registry_defer_pending["pending_terminal_depth"] == 1
            and registry_defer_drain["resolved"] == 1,
            "two defer resolver ACK losses also hand off and drain without consuming an attempt",
        )

        # ABA: the exact old intent is allowed to outlive a successor claim,
        # but its eventual drain can only observe ownership_lost.
        enqueue(store, "registry-aba", 107, "acme/registry-aba")
        aba_gen1 = claim(store, "registry-aba")
        store.arm("release", "post", "post")
        assert store.release(
            "registry-aba", "old generation", aba_gen1) == "pending"
        store.clear_fault()
        aba_gen2 = claim(store, "registry-aba")
        aba_before = state("registry-aba")
        aba_drain = store.drain_pending_terminals()
        aba_after = state("registry-aba")
        check(
            aba_gen2 == aba_gen1 + 1
            and aba_drain["resolved"] == 1
            and aba_before == aba_after
            and aba_after[0] == "processing",
            "registry drain treats generation plus one as ownership_lost and leaves the successor byte-identical",
        )
        assert store.release(
            "registry-aba", "successor cleanup", aba_gen2) == "queued"

        # One failed daemon call is enough for this tick. The immediate second
        # call is suppressed by the one-second backoff, keeping heartbeat work
        # bounded even if every registered resolver sees an outage.
        enqueue(store, "registry-backoff", 108, "acme/registry-backoff")
        backoff_gen = claim(store, "registry-backoff")
        store.arm("release", "pre", "pre")
        assert store.release(
            "registry-backoff", "background outage", backoff_gen) == "pending"
        store.arm("release", "pre")
        first_drain = store.drain_pending_terminals()
        immediate_drain = store.drain_pending_terminals()
        check(
            first_drain["attempted"] == 1
            and first_drain["resolved"] == 0
            and immediate_drain["attempted"] == 0
            and store.resolver_calls == 1,
            "daemon resolver uses one call per tick and exponential backoff, with no retry burst",
        )
        with store._pending_terminal_lock:
            for entry in store._pending_terminals.values():
                entry["next_attempt_at"] = 0.0
        store.clear_fault()
        assert store.drain_pending_terminals()["resolved"] == 1

        # A same-lane follower is blocked while the exact head is processing.
        # Once the pending release converges, recovery can finish that head and
        # the follower is synchronously claimable without a stale-window wait.
        enqueue(store, "lane-head", 109, "acme/lane")
        enqueue(store, "lane-follower", 109, "acme/lane")
        lane_gen = claim(store, "lane-head")
        store.arm("release", "pre", "pre")
        assert store.release(
            "lane-head", "temporary DB outage", lane_gen) == "pending"
        blocked = store.claim("lane-follower")
        store.clear_fault()
        terminal_drain = store.drain_pending_terminals()
        replayed = store.claim("lane-head")
        assert replayed.get("claimed"), replayed
        assert store.finish("lane-head", replayed["lease_generation"])
        follower = store.claim("lane-follower")
        check(
            not blocked.get("claimed")
            and terminal_drain["resolved"] == 1
            and follower.get("claimed") is True,
            "same-lane follower is synchronously claimable after exact terminal convergence",
        )
        assert store.finish("lane-follower", follower["lease_generation"])

        # The production owner-liveness daemon, not a handler retry, owns the
        # eventual drain. Use a short test cadence and observe the real thread
        # heartbeat/reaper/one-intent sequence. Keep this after synchronous
        # drain assertions: the daemon deliberately has no shutdown hook and
        # must not race a later test that is proving which caller drained an
        # exact intent.
        enqueue(store, "registry-daemon", 111, "acme/registry-daemon")
        daemon_gen = claim(store, "registry-daemon")
        store.arm("release", "pre", "pre")
        assert store.release(
            "registry-daemon", "daemon recovery", daemon_gen) == "pending"
        store.clear_fault()
        daemon_thread = DQ.start_instance_liveness_loop(
            store, interval=0.05)
        daemon_deadline = time.monotonic() + 2.0
        while (
            store.liveness_snapshot()["pending_terminal_depth"] > 0
            and time.monotonic() < daemon_deadline
        ):
            time.sleep(0.01)
        daemon_snapshot = store.liveness_snapshot()
        check(
            daemon_thread.is_alive()
            and daemon_snapshot["pending_terminal_depth"] == 0
            and state("registry-daemon")[0] == "queued",
            "dedicated owner-liveness daemon drains the exact intent outside handler retry",
        )

        # A permanent resolver error can coexist with successful heartbeats.
        # Once its exact intent is as old as the dead-owner safe age, health
        # must force process replacement so the dead-owner reaper converges it
        # instead of preserving a processing lease for the 1800-second stale
        # window.
        aged_store = FaultStore(APP_DSN)
        aged_store._liveness_thread = type(
            "AliveThread", (), {"is_alive": lambda self: True})()
        aged_store._last_successful_heartbeat_at = time.monotonic()
        aged_identity = (
            "release", "aged-health", 1,
            ("permanent resolver error", aged_store.max_attempts),
        )
        with aged_store._pending_terminal_lock:
            aged_store._pending_terminals[aged_identity] = {
                "kind": "release",
                "key": "aged-health",
                "generation": 1,
                "args": ("permanent resolver error", aged_store.max_attempts),
                "created_at": (
                    time.monotonic()
                    - DQ._DEAD_INSTANCE_RECLAIM_SAFE_SECONDS + 0.2
                ),
                "next_attempt_at": time.monotonic(),
                "attempts": 1,
            }
        before_safe_age = aged_store.liveness_snapshot()
        with aged_store._pending_terminal_lock:
            aged_store._pending_terminals[aged_identity]["created_at"] = (
                time.monotonic() - DQ._DEAD_INSTANCE_RECLAIM_SAFE_SECONDS
            )
        at_safe_age = aged_store.liveness_snapshot()
        check(
            before_safe_age["healthy"] is True
            and at_safe_age["healthy"] is False
            and at_safe_age["pending_terminal_depth"] == 1
            and at_safe_age["pending_terminal_overflow"] is False,
            "aged pending terminal fails health at the dead-owner safe boundary even without overflow",
        )

        # Fixed cap overflow is latched and makes health fail closed. Populate
        # directly with content-free synthetic exact intents; this does not
        # create DB work and proves the bound itself.
        overflow_store = FaultStore(APP_DSN)
        for index in range(DQ._PENDING_TERMINAL_CAP):
            assert overflow_store._register_pending_terminal(
                "release", f"bounded-{index}", 1,
                ("bounded", overflow_store.max_attempts))
        overflow_store._register_pending_terminal(
            "release", "bounded-overflow", 1,
            ("bounded", overflow_store.max_attempts))
        overflow = overflow_store.liveness_snapshot()
        check(
            overflow["pending_terminal_depth"] == DQ._PENDING_TERMINAL_CAP
            and overflow["pending_terminal_overflow"] is True
            and overflow["healthy"] is False,
            "pending-terminal registry is fixed-small and overflow fails health closed",
        )

        release_acl = resolver_contract(
            "resolve_webhook_delivery_release_with_authority",
            "(text,text,integer,bigint)",
        )
        defer_acl = resolver_contract(
            "resolve_webhook_delivery_defer_with_authority",
            "(text,bigint,timestamp with time zone,text)",
        )
        check(
            all(
                contract.get("security_definer") is True
                and contract.get("owner") == "veripsa_migrator"
                and contract.get("fixed_search_path") is True
                and contract.get("app_execute") is True
                and contract.get("writer_execute") is False
                and contract.get("app_update") is False
                for contract in (release_acl, defer_acl)
            ),
            "both resolvers are migrator-owned fixed-search_path SECURITY DEFINER and App-only",
        )
        expected = set(schema_contract._EXPECTED_FUNCTIONS)
        check(
            {
                ("resolve_webhook_delivery_release_with_authority", 4),
                ("resolve_webhook_delivery_defer_with_authority", 4),
                ("beat_webhook_worker_instance_with_authority", 1),
                ("stamp_webhook_owner_instance_with_authority", 3),
                ("reap_dead_instance_leases_with_authority", 3),
            }.issubset(expected),
            "boot schema contract includes terminal resolvers and all three heartbeat/reaper functions",
        )
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)

    print(
        "TERMINAL EXACT RECOVERY GATE:",
        "PASS" if FAILURES == 0 else f"FAIL ({FAILURES})",
    )
    return 0 if FAILURES == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
