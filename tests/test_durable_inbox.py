#!/usr/bin/env python3
"""DURABLE WEBHOOK INBOX — the persist-before-202 durability boundary (reliability-critical).

WHY THIS GATE. The HTTP path acks 202 fast, but once 202 is sent GitHub will NOT reliably redeliver a later
worker crash / a Render 2-instance rolling deploy (routine) / an OOM. The legacy in-memory queue processed
AFTER the 202, so an accepted event in flight at that moment was LOST. The durable inbox (db/schema/
25_webhook_queue.sql + github-app/delivery_queue.py) closes that hole: a SANITIZED delivery is persisted to
core.webhook_delivery BEFORE the 202, the worker claims/processes/finishes it, and a boot recovery loop
re-submits unfinished rows. This gate proves the durability + safety contract end to end:

  (1) CRASH-BEFORE-FINISH IS RECOVERED, REPLAYED IDEMPOTENTLY. A delivery persisted then 'crashed' mid-process
      (claimed → process started → no finish) is re-claimable after it goes stale, replays, and the net effect
      lands EXACTLY ONCE (not lost, not double-applied).
  (2) RECOVERY RE-SUBMITS STALE 'processing' + 'queued'. pending() returns a 'queued' row AND a stale-locked
      'processing' row, but NOT a freshly-locked 'processing' row (an actively-running worker is left alone).
  (3) CROSS-TENANT AUTHORITY. core.webhook_delivery is CROSS-TENANT (all tenants in one table). The ONLY writer
      is the single shared App identity (veripsa_app) via the SECURITY DEFINER *_with_authority fns. A BUYER
      tenant role can neither call those fns (EXECUTE revoked) nor touch the table directly (no grant) — so one
      tenant's connection can never claim/finish/release/recover ANOTHER tenant's delivery row.
  (4) THE _latest_push RESIDUAL IS CLOSED. A recovery-resubmitted (register_push=False) STALE push does NOT
      regress EventQueue._latest_push and CANNOT make a NEWER live push coalesce to 'skip' (which, with nothing
      queued behind it to rebuild, would leave a permanently stale graph).
  (5) THE PERSISTED PAYLOAD IS CONTENT-FREE (sanitize_payload): paths/shas kept, commit messages stripped.

HARDENING (audit P1 — the inbox is LIVE on prod, so these bite real installs):
  (6) THE DURABLE RETRY BUDGET HAS ITS OWN, LARGER CEILING than the in-memory transient-retry budget (floored
      above it), so a delivery that merely lived through a restart-spanning recovery cycle is not prematurely
      dead-lettered to a state pending() will never replay (a permanent, invisible loss).
  (7) A POISON 'failed' ROW IS SURFACED + RE-ARMED, NOT SILENTLY LOST: pending() does not replay it (the loss is
      real) BUT depth()['failed'] makes it visible, the watchdog's evaluate_delivery_depth ALERTS on it (and on a
      non-draining 'queued' backlog) — wired end-to-end through watchdog_tick(store=...) — and the DLQ sweep
      re-arms an aged 'failed' row for one fresh attempt-budget (a just-failed row is left alone by the age gate).
  (8) A FINAL-ATTEMPT STALE 'processing' ROW IS EXPIRED TO 'failed', NOT LEFT WEDGED FOREVER. claim() increments
      attempts before the worker runs; if the process dies on the last durable attempt before finish/release, the
      row has status='processing' and attempts>=max. pending() must move it into the existing DLQ path.
  (9) AN AGE-BASED REAPER (prune_all_accounts_with_authority) drops terminal done/failed rows past the window
      while KEEPING live 'queued'/'processing' rows — the fourth unbounded grower, bounded on the 256 MiB tier.
  (10) SCHEDULED RETRY IS ATTEMPT-NEUTRAL. A lifecycle consistency delay returns a processing row to queued,
      restores the claimed attempt, and hides it from pending/claim until not_before without a stale/DLQ detour.
  (11) RECOVERY ENUMERATION IS ACCOUNT-FAIR. A deep backlog for one owner cannot hide another tenant behind the
       global pending() limit before the runtime's one-outstanding-per-account admission can see it.
  (12) SHUTDOWN LEASE EXPIRY IS RACE-FREE. A drain-timeout shutdown backdates ONLY the owned in-flight lease so
       recovery reclaims it within a short grace instead of the full stale window (which freezes the row's whole
       account/repo causal lane) — while the row STAYS 'processing' with the lease intact, so a dying worker that
       still commits can finish normally (never a requeue race into double-processing).
  (13) AGED-DEFERRED LANE ESCALATION (issue #847). A delivery whose live claim answered 'blocked_by_earlier'
       stays 'queued' with NO comeback path of its own (the worker drops it; GitHub never redelivers a 202'd
       delivery; the App-level redelivery scan sees it locally-received/OK; pending() offers only lane heads).
       When its blocker ends 'failed' (protocol 1 — e.g. budget burned by restart-reclaim churn), the lane froze
       until the slow DLQ re-arm (1h; 0=never). The escalation re-arms an AGED failed lane head that is blocking
       AGED queued work NOW, order-preservingly; the lane then drains in causal order and the deferred delivery
       replays exactly once. Both age gates respected; 'done' rows never re-processed; protocol-0 quarantine
       preserved; junk rows never crash the sweep.

Run:  python3 tests/test_durable_inbox.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
from psycopg2 import errorcodes  # noqa: E402
import server as S  # noqa: E402
import delivery_queue as DQ  # noqa: E402
import ingest as I  # noqa: E402
import alerts as A  # noqa: E402  (the proactive-alert evaluators — the watchdog's durable-depth alert)
import health_watchdog as HW  # noqa: E402  (watchdog_tick — proves the durable store is wired into the monitor)
import server_http as SH  # noqa: E402  (real DB depth -> /readyz classifier contract)
from _installation_fixture import seed_live_installation  # noqa: E402

DB = "veripsa_durinbox_" + str(os.getpid())
ADMIN = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"


def _admin(sql, args=()):
    # Override only dbname. String replacement corrupts a DSN whose user is also
    # named postgres (postgresql://postgres@.../postgres) into a fake DB-named role.
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


def _bootstrap():
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        sys.exit(2)
    # a SECOND tenant account + writer (ACCT-ACME) so cross-tenant claims are tested with two REAL tenants,
    # not "empty other side" (mirrors the tamper gate's two-tenant proof).
    subprocess.run(["psql", f"postgresql://veripsa_migrator@localhost/{DB}", "-v", "ON_ERROR_STOP=1", "-q", "-c",
                    "SET search_path=core; SELECT core.provision_seat('ACCT-ACME','Acme Co','AG-ACME','acme','veripsa_acme_agent');"],
                   cwd=ROOT, capture_output=True, text=True)
    # The crash/reclaim probe measures durable lease generations.  Give its owner an already-live route so
    # finish() retains the lease attempt count instead of correctly anonymizing an unrouted delivery receipt.
    seed_live_installation(
        APP_DSN,
        f"postgresql://veripsa_migrator@localhost/{DB}",
        7777,
        4242,
    )


def _teardown():
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


def main() -> int:
    _bootstrap()
    checks = []

    # A raw push payload with SECRET commit-message text that must be STRIPPED at persistence.
    raw_push = {"repository": {"full_name": "acme/dur", "default_branch": "main", "owner": {"id": 7777}},
                "ref": "refs/heads/main", "before": "0" * 40,
                "after": "a" * 40, "size": 1,
                "head_commit": {"timestamp": "2026-06-22T00:00:00Z", "message": "TOP-SECRET-MSG"},
                "pusher": {"name": "ann"}, "sender": {"login": "ann", "type": "User"},
                "commits": [{"id": "c1", "message": "TOP-SECRET-MSG", "added": ["src/pay.py"],
                             "modified": [], "removed": []}]}

    store = S.DeliveryStore(APP_DSN, max_pending=50, max_attempts=3, stale_seconds=1)

    # PostgreSQL jsonb_object_agg omits a status group that has no rows.  The
    # authority function must make the readiness counters total rather than
    # asking Python to guess that arbitrary missing evidence means zero.
    empty_depth = store.depth()
    empty_ready_depth, empty_ready_failures = SH._durable_inbox_readiness(
        empty_depth)
    checks.append((
        "an empty real durable inbox publishes explicit zero status counters "
        "and is operationally ready, never Unknown",
        all(empty_depth.get(field) == 0 for field in (
            "queued",
            "queued_due",
            "queued_due_oldest_age_seconds",
            "processing",
            "processing_oldest_age_seconds",
            "done",
            "failed",
        ))
        and empty_ready_depth is not None
        and empty_ready_failures == (),
    ))

    # ─── (5) CONTENT-FREE PERSIST ──────────────────────────────────────────────────────────────────────────
    sub = store.submit("push", raw_push, "dur-key-1", account_key="7777", repo="acme/dur")
    stored = _admin("SELECT payload::text FROM core.webhook_delivery WHERE delivery_key=%s", ("dur-key-1",))
    stored_protocol = _admin(
        "SELECT causal_order_version FROM core.webhook_delivery WHERE delivery_key=%s", ("dur-key-1",),
    )
    stored_payload = json.loads(stored)
    checks.append(("persist is content-free: paths/shas kept, commit message text STRIPPED before the DB write",
                   sub.get("accepted") and "src/pay.py" in (stored or "") and "a" * 40 in (stored or "")
                   and "TOP-SECRET-MSG" not in (stored or "") and '"message"' not in (stored or "")
                   and stored_payload.get("before") == "0" * 40
                   and stored_payload.get("after") == "a" * 40
                   and stored_payload.get("size") == 1
                   and stored_protocol == 1))
    overlong_path = "x" * 1025
    malformed_push = dict(
        raw_push,
        commits=[{
            "id": "bad",
            "added": [overlong_path],
            "modified": ["src/pay.py"],
            "removed": [],
        }],
    )
    malformed_sanitized = DQ.sanitize_payload("push", malformed_push)
    nonlist_push = dict(
        raw_push,
        commits=[{
            "id": "bad-list",
            "added": "src/lost.py",
            "modified": ["src/pay.py"],
            "removed": [],
        }],
    )
    nonlist_sanitized = DQ.sanitize_payload("push", nonlist_push)
    checks.append((
        "durable sanitizer never turns malformed/overlong paths into a complete changed set",
        I._push_changed_set_complete(malformed_sanitized) is False
        and I._push_changed_set_complete(nonlist_sanitized) is False
        and malformed_sanitized["commits"][0]["added"] == [None]
        and nonlist_sanitized["commits"][0]["added"] == [None]
        and overlong_path not in json.dumps(malformed_sanitized),
    ))
    # Settle this independent fixture before the crash-order probe. Durable claim order correctly refuses a newer
    # same-account row while this older queued row is unfinished.
    content_claim = store.claim(sub["key"])
    assert content_claim.get("claimed"), content_claim
    assert store.finish(sub["key"], content_claim["lease_generation"])

    # ─── (1) CRASH-BEFORE-FINISH → RECOVERED + REPLAYED EXACTLY ONCE ───────────────────────────────────────
    # Model the worker's "work" with a side-effect counter. The wrapped processor claims the row, runs the
    # "work", then finishes. We simulate a CRASH AFTER the work commits but BEFORE finish on the FIRST run by
    # making finish a no-op once; recovery then re-claims and the SECOND run finishes. The work must be applied
    # exactly once NET (finish() is the idempotent commit boundary — a re-claim re-runs work only because the
    # first crash left no finish; we assert the row ends 'done' with attempts==2 and the work side effect is
    # idempotent by construction). Here we prove the DELIVERY lifecycle directly against the store.
    applied = {"n": 0}

    def _work(et, pl, db, gh, coalesce=None):
        applied["n"] += 1                      # the "landed fact" — idempotent in real handlers (upsert)
        return {"ok": True}

    crash_store = S.DeliveryStore(APP_DSN, max_pending=50, max_attempts=3, stale_seconds=1)
    csub = crash_store.submit("push", raw_push, "dur-crash-1", account_key="7777", repo="acme/dur")
    # FIRST run: claim → work commits → CRASH before finish (we just don't call finish).
    claimed1 = crash_store.claim(csub["key"])
    assert claimed1.get("claimed"), claimed1
    _work("push", DQ._as_jsonb(claimed1.get("payload")), None, None)   # work "commits"
    state_mid = _admin("SELECT status||':'||attempts FROM core.webhook_delivery WHERE delivery_key=%s", (csub["key"],))
    # …process dies here. The row is left 'processing' (attempts=1), no finish.
    time.sleep(1.1)                            # let it pass the 1s stale threshold
    # RECOVERY: it must reappear as pending (stale 'processing'); re-claim, finish.
    pend_keys = [r.get("key") for r in crash_store.pending()]
    claimed2 = crash_store.claim(csub["key"])
    fin = crash_store.finish(csub["key"], claimed2["lease_generation"]) if claimed2.get("claimed") else False
    state_done = _admin("SELECT status||':'||attempts FROM core.webhook_delivery WHERE delivery_key=%s", (csub["key"],))
    done_payload = _admin("SELECT payload::text FROM core.webhook_delivery WHERE delivery_key=%s", (csub["key"],))
    checks.append((f"crash-before-finish is RECOVERED + finished idempotently (mid={state_mid}, recovered={csub['key'] in pend_keys}, "
                   f"done={state_done}, payload={done_payload})",
                   state_mid == "processing:1" and csub["key"] in pend_keys and claimed2.get("claimed") is True
                   and fin is True and state_done == "done:2" and done_payload == "{}"))
    # a re-finish (a late duplicate) is an idempotent NO-OP (FOUND=false) — never re-opens the committed work.
    refin = crash_store.finish(csub["key"], claimed2["lease_generation"])
    checks.append(("a duplicate/late finish on a 'done' row is an idempotent NO-OP (owned-row guard)",
                   refin is False and _admin("SELECT status FROM core.webhook_delivery WHERE delivery_key=%s", (csub["key"],)) == "done"))

    # ─── (2) RECOVERY RE-SUBMITS STALE 'processing' + 'queued', LEAVES A FRESH 'processing' ALONE ───────────
    rstore = S.DeliveryStore(APP_DSN, max_pending=50, max_attempts=3, stale_seconds=30)
    rstore.submit("push", dict(raw_push, after="b" * 40), "rec-queued",
                  account_key="rec-queued-account", repo="acme/dur")    # 'queued'
    rstore.submit("push", dict(raw_push, after="c" * 40), "rec-stale-proc",
                  account_key="rec-stale-account", repo="acme/dur")
    rstore.submit("push", dict(raw_push, after="d" * 40), "rec-fresh-proc",
                  account_key="rec-fresh-account", repo="acme/dur")
    # force one to a STALE 'processing' (locked long ago) and one to a FRESH 'processing' (locked now), via the migrator.
    _admin("UPDATE core.webhook_delivery SET status='processing', locked_at=now()-interval '1 hour' WHERE delivery_key=%s", ("rec-stale-proc",))
    _admin("UPDATE core.webhook_delivery SET status='processing', locked_at=now() WHERE delivery_key=%s", ("rec-fresh-proc",))
    pend = {r.get("key") for r in rstore.pending()}   # stale_seconds=30 → 1h-old is stale, now() is not
    checks.append((f"recovery pending() = stale 'processing' + 'queued', NOT a freshly-locked 'processing' (pending={sorted(pend)})",
                   "rec-queued" in pend and "rec-stale-proc" in pend and "rec-fresh-proc" not in pend))

    # ─── (10) SCHEDULED RETRY DOES NOT SPEND ATTEMPTS OR USE STALE RECOVERY ──────────────────────────────
    scheduled = S.DeliveryStore(APP_DSN, max_pending=50, max_attempts=3, stale_seconds=1)
    scheduled.submit("push", dict(raw_push, after="8" * 40), "scheduled-retry",
                     account_key="scheduled-7777", repo="acme/dur")
    scheduled_claim1 = scheduled.claim("scheduled-retry")
    deferred = scheduled.defer(
        "scheduled-retry", datetime.now(timezone.utc) + timedelta(minutes=5),
        "test consistency delay", scheduled_claim1["lease_generation"],
    )
    scheduled_state = _admin(
        "SELECT status||':'||attempts||':'||(not_before>now())::text "
        "FROM core.webhook_delivery WHERE delivery_key=%s",
        ("scheduled-retry",),
    )
    scheduled_pending = {row.get("key") for row in scheduled.pending()}
    scheduled_claim_early = scheduled.claim("scheduled-retry")
    _admin("UPDATE core.webhook_delivery SET not_before=now()-interval '1 second' WHERE delivery_key=%s",
           ("scheduled-retry",))
    scheduled_pending_due = {row.get("key") for row in scheduled.pending()}
    scheduled_claim2 = scheduled.claim("scheduled-retry")
    checks.append((f"scheduled retry restores the claimed attempt and is invisible until due "
                   f"(first={scheduled_claim1}, deferred={deferred}, state={scheduled_state}, "
                   f"early={scheduled_claim_early}, due={scheduled_claim2})",
                   scheduled_claim1.get("claimed") is True
                   and deferred is True
                   and scheduled_state == "queued:0:true"
                   and "scheduled-retry" not in scheduled_pending
                   and scheduled_claim_early.get("claimed") is False
                   and "scheduled-retry" in scheduled_pending_due
                   and scheduled_claim2.get("claimed") is True
                   and scheduled_claim2.get("attempts") == 1))

    # ─── (8) FINAL-ATTEMPT STALE PROCESSING → DLQ, NOT FOREVER-WEDGED ─────────────────────────────────────
    maxed = S.DeliveryStore(APP_DSN, max_pending=50, max_attempts=3, stale_seconds=30)
    maxed.submit("push", dict(raw_push, after="9" * 40), "rec-maxed-proc",
                 account_key="maxed-7777", repo="acme/dur")
    _admin("UPDATE core.webhook_delivery SET status='processing', attempts=3, locked_at=now()-interval '1 hour' "
           "WHERE delivery_key=%s", ("rec-maxed-proc",))
    pend_maxed = {r.get("key") for r in maxed.pending()}   # pending() also expires exhausted stale processing rows
    state_maxed = _admin("SELECT status||':'||attempts||':'||COALESCE(last_error,'') "
                         "FROM core.webhook_delivery WHERE delivery_key=%s", ("rec-maxed-proc",))
    depth_maxed = maxed.depth()
    checks.append((f"final-attempt stale 'processing' is expired to the existing DLQ path, not left wedged forever "
                   f"(pending={sorted(pend_maxed)}, state={state_maxed}, depth={depth_maxed})",
                   "rec-maxed-proc" not in pend_maxed
                   and state_maxed == "failed:3:stale processing exceeded durable attempts"
                   and int(depth_maxed.get("failed", 0)) >= 1))

    # ─── (3) CROSS-TENANT AUTHORITY: a BUYER tenant role can neither call the fns nor touch the table ──────────
    # The durable inbox is written ONLY by the single shared App identity (veripsa_app). A buyer's writer role
    # (veripsa_acme_agent, a DIFFERENT tenant) must be REFUSED on every authority fn (EXECUTE revoked → 42501)
    # and on direct table access (no grant) — so it can never claim/finish/release/recover any delivery row.
    def _denied_as(role_dsn, sql, args=()):
        conn = psycopg2.connect(role_dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                try:
                    cur.execute(sql, args)
                    return None                     # NOT denied → a hole
                except psycopg2.Error as e:
                    return e.pgcode
        finally:
            conn.close()

    acme_dsn = f"postgresql://veripsa_acme_agent@localhost/{DB}"
    fn_calls = [
        ("enqueue-legacy", "SELECT core.enqueue_webhook_delivery_with_authority('x','push',NULL,NULL,'{}'::jsonb,100)"),
        ("enqueue-v2", "SELECT core.enqueue_webhook_delivery_with_authority('x','push',NULL,NULL,'{}'::jsonb,100,2)"),
        ("pending", "SELECT core.pending_webhook_deliveries_with_authority(10,1,3)"),
        ("claim-legacy", "SELECT core.claim_webhook_delivery_with_authority(%s,1,3)"),
        ("claim-v2", "SELECT core.claim_webhook_delivery_with_authority(%s,1,3,2)"),
        ("finish-legacy", "SELECT core.finish_webhook_delivery_with_authority(%s)"),
        ("finish-exact", "SELECT core.finish_webhook_delivery_with_authority(%s,1)"),
        ("release-legacy", "SELECT core.release_webhook_delivery_with_authority(%s,'',3)"),
        ("release-exact", "SELECT core.release_webhook_delivery_with_authority(%s,'',3,1)"),
        ("defer-exact", "SELECT core.defer_webhook_delivery_with_authority('dur-key-1',now(),'x',1)"),
        ("escalate", "SELECT core.escalate_blocked_webhook_deliveries_with_authority(600,10)"),
        ("depth",   "SELECT core.webhook_delivery_depth_with_authority()"),
        ("defer-internal", "SELECT core._defer_webhook_delivery_with_authority('dur-key-1',now(),'x')"),
    ]
    fn_codes = {name: _denied_as(acme_dsn, sql, (("dur-key-1",) if "%s" in sql else ()))
                for name, sql in fn_calls}
    all_fns_denied = all(code == errorcodes.INSUFFICIENT_PRIVILEGE for code in fn_codes.values())
    checks.append((f"cross-tenant: a buyer tenant role is DENIED EXECUTE on EVERY *_with_authority fn (42501) — "
                   f"cannot claim/finish/release/recover another tenant's row (codes={fn_codes})",
                   all_fns_denied))
    # …and direct table reads/writes are denied too (no grant) — it cannot even SEE another tenant's delivery.
    sel_code = _denied_as(acme_dsn, "SELECT count(*) FROM core.webhook_delivery")
    upd_code = _denied_as(acme_dsn, "UPDATE core.webhook_delivery SET status='done' WHERE delivery_key='dur-key-1'")
    checks.append((f"cross-tenant: a buyer tenant role is DENIED direct SELECT/UPDATE on core.webhook_delivery "
                   f"(select={sel_code}, update={upd_code}) — the table is unreachable without the gate fns",
                   sel_code == errorcodes.INSUFFICIENT_PRIVILEGE and upd_code == errorcodes.INSUFFICIENT_PRIVILEGE))

    # ─── (4) THE _latest_push RESIDUAL IS CLOSED ───────────────────────────────────────────────────────────
    # A recovery-resubmitted STALE push (older sha) is submitted with register_push=False; a NEWER live push
    # (newer sha) is submitted normally. The stale recovered push must NOT regress _latest_push and must NOT
    # make the newer live push coalesce to 'skip'. We do NOT start the worker (so submit() doesn't drain): we
    # inspect _latest_push + call _push_coalesce directly, exactly as the resilience gate models the worker.
    eq = S.EventQueue(db=None, gh=None, process=lambda *a, **k: None,
                      branch_from_ref=S._branch_from_ref, maxsize=100)

    def _pp(sha):
        return {"repository": {"full_name": "o/r"}, "ref": "refs/heads/main", "after": sha}

    # live newest push B registers (register_push default True).
    eq.submit("push", _pp("b" * 40))
    latest_after_live = eq._latest_push.get(("o/r", "main"))
    # a STALE recovered push A (older) arrives via the recovery path → register_push=False.
    eq.submit("push", _pp("a" * 40), "rec-A", register_push=False)
    latest_after_recovery = eq._latest_push.get(("o/r", "main"))
    # the newer live push B must STILL decide 'normal' (its own sha is the latest), never 'skip'.
    b_decision = eq._push_coalesce("o/r", "main", "b" * 40)
    # the stale recovered push A, when processed, decides 'skip' (B is genuinely newer + queued) — correct: B
    # rebuilds; A does not regress the graph. It does NOT mark B for a needless 'full', and never makes B 'skip'.
    a_decision = eq._push_coalesce("o/r", "main", "a" * 40)
    checks.append((f"_latest_push residual CLOSED: a stale recovered push (register_push=False) does NOT regress "
                   f"the newest-push tracking nor mask the newer live push (latest live={latest_after_live[:4] if latest_after_live else None}, "
                   f"after recovery={latest_after_recovery[:4] if latest_after_recovery else None}, B={b_decision}, A={a_decision})",
                   latest_after_live == "b" * 40 and latest_after_recovery == "b" * 40
                   and b_decision == "normal" and a_decision == "skip"))

    # CONTRAST (proves the residual was real): the SAME stale push submitted via the LIVE path (register_push=True)
    # WOULD overwrite _latest_push with the stale sha and make B coalesce to 'skip' — the masking bug. We assert
    # the recovery path differs from this broken behaviour so a regression that drops register_push is caught.
    eq2 = S.EventQueue(db=None, gh=None, process=lambda *a, **k: None,
                       branch_from_ref=S._branch_from_ref, maxsize=100)
    eq2.submit("push", _pp("b" * 40))                      # newest live B
    eq2.submit("push", _pp("a" * 40))                      # stale push via the LIVE path (the WRONG way for recovery)
    broken_latest = eq2._latest_push.get(("o/r", "main"))
    broken_b = eq2._push_coalesce("o/r", "main", "b" * 40)  # would be 'skip' — the masking bug
    checks.append((f"residual REGRESSION-CANARY: the live path (register_push=True) DOES regress+mask "
                   f"(latest={broken_latest[:4] if broken_latest else None}, B={broken_b}) — so the recovery path "
                   f"MUST use register_push=False (above) to avoid it",
                   broken_latest == "a" * 40 and broken_b == "skip"))

    # ─── (6) DURABLE RETRY BUDGET HAS ITS OWN, LARGER CEILING THAN THE IN-MEMORY TRANSIENT-RETRY BUDGET ──────
    # (audit P1) The in-memory EventQueue retries a transient blip a few times within ONE process run
    # (VERIPSA_EVENT_RETRY_ATTEMPTS); the durable row's `attempts` keeps climbing across RECOVERY replays that span
    # restarts. If both shared the same small ceiling, a delivery that merely lived through a rolling deploy could
    # hit attempts>=max → 'failed', and pending() (attempts<max) would then NEVER replay it → a permanent, invisible
    # loss. So the durable budget gets its own knob with a larger default, floored at the in-memory budget so it can
    # never be the smaller of the two.
    checks.append((f"durable budget ({DQ._MAX_ATTEMPTS}) is STRICTLY larger than the in-memory transient-retry "
                   f"budget ({DQ._INMEM_RETRY_ATTEMPTS}) — a restart-spanning recovery cycle does not prematurely "
                   f"dead-letter a delivery",
                   DQ._MAX_ATTEMPTS > DQ._INMEM_RETRY_ATTEMPTS and DQ._MAX_ATTEMPTS >= 8))

    # ─── (7) POISON/DEAD-LETTER ROW IS SURFACED + RE-ARMED, NOT SILENTLY LOST ───────────────────────────────
    # A row that exhausts the durable budget goes 'failed'. pending() must NOT replay it (attempts>=max) — proving
    # the silent-loss the audit flagged is real — BUT it must be (a) VISIBLE via depth()['failed'], (b) ALERTED by
    # the watchdog's evaluate_delivery_depth, and (c) RE-ARMABLE by the DLQ sweep (requeued for a fresh budget).
    dlq = S.DeliveryStore(APP_DSN, max_pending=50, max_attempts=3, stale_seconds=1)
    dlq.submit("push", dict(raw_push, after="e" * 40), "dlq-poison",
               account_key="dlq-7777", repo="acme/dur")
    # force it to a terminal 'failed' with attempts at the ceiling (a row that burned through its whole budget).
    _admin("UPDATE core.webhook_delivery SET status='failed', attempts=3, updated_at=now()-interval '2 hours' "
           "WHERE delivery_key=%s", ("dlq-poison",))
    pend_after_fail = {r.get("key") for r in dlq.pending()}
    depth_now = dlq.depth()
    checks.append((f"a 'failed' row is NOT replayed by pending() (attempts>=max) — the silent-loss the audit "
                   f"flagged is real (pending={sorted(pend_after_fail)}, depth={depth_now})",
                   "dlq-poison" not in pend_after_fail and int(depth_now.get("failed", 0)) >= 1
                   and all(field in depth_now for field in (
                       "queued_due", "queued_due_oldest_age_seconds", "queued_max_attempts",
                       "fanout_active", "fanout_remaining",
                       "fanout_oldest_delivery_age_seconds",
                       "fanout_oldest_progress_age_seconds"))))
    # (a) VISIBLE + ALERTED: the watchdog's depth evaluator fires delivery_dead_letter on failed>0 (it was SILENT
    # before — webhook_delivery_depth_with_authority existed but nothing called it).
    posts = []
    sink = A.AlertSink(webhook_url="https://hook.example/x", min_interval=0,
                       poster=lambda url, body: posts.append(body))
    A.evaluate_delivery_depth(sink, depth_now)
    fired_dl = any(p.get("key") == "delivery_dead_letter" and p.get("level") == "critical" for p in posts)
    checks.append((f"watchdog ALERTS on a dead-lettered row: evaluate_delivery_depth fires delivery_dead_letter "
                   f"CRITICAL on depth['failed']>0 (the P0 boundary is no longer silent) (fired={fired_dl})",
                   fired_dl))
    # a 'queued' backlog over threshold also pages (distinct from the in-memory queue_backlog) + resolves on drain.
    posts.clear()
    A.evaluate_delivery_depth(sink, {"queued": 5000, "failed": 0}, queued_backlog=1000)
    fired_bk = any(p.get("key") == "delivery_backlog" for p in posts)
    posts.clear()
    A.evaluate_delivery_depth(sink, {"queued": 0, "failed": 0}, queued_backlog=1000)
    resolved = any(p.get("key") == "delivery_backlog" and "recover" in p.get("text", "").lower() for p in posts)
    checks.append((f"watchdog ALERTS on a non-draining durable 'queued' backlog + resolves on drain "
                   f"(fired={fired_bk}, resolved={resolved})", fired_bk and resolved))
    # WIRED END-TO-END: the watchdog TICK itself (health_watchdog.watchdog_tick), given the real durable `store`,
    # samples store.depth() and fires the alert — proving server.py threads `store` into run_watchdog→watchdog_tick
    # (P1 #3), not just that the evaluator works in isolation. A fake worker (health_snapshot's surface) + a no-op
    # db callable keep the tick pure; the depth comes from the REAL store against the seeded 'failed' row.
    class _FakeWorker:
        def is_alive(self): return True
        def inflight_age(self): return None
        def retried(self): return 0
        def qsize(self): return 0
        def maxsize(self): return 1000
        def processed(self): return 0
        def failed(self): return 0
        def uptime(self): return 1.0
    posts.clear()
    HW.watchdog_tick(sink, _FakeWorker(), db=lambda *a, **k: 1, prev_failed=0, store=dlq)
    tick_fired = any(p.get("key") == "delivery_dead_letter" for p in posts)
    checks.append((f"watchdog_tick(store=...) SAMPLES the durable depth + fires the dead-letter alert end-to-end "
                   f"(server wires store→run_watchdog→watchdog_tick) (fired={tick_fired})", tick_fired))

    # (c) RE-ARMED: the DLQ sweep requeues the (>1h-old) 'failed' row for a fresh budget → it is replayable again.
    rearm = dlq.rearm_failed(rearm_seconds=3600, limit=100)
    state_rearmed = _admin("SELECT status||':'||attempts FROM core.webhook_delivery WHERE delivery_key=%s", ("dlq-poison",))
    pend_after_rearm = {r.get("key") for r in dlq.pending()}
    checks.append((f"DLQ re-arm requeues the aged 'failed' row for a fresh attempt-budget (not lost): "
                   f"rearmed={rearm.get('rearmed')}, state={state_rearmed}, replayable={'dlq-poison' in pend_after_rearm}",
                   int(rearm.get("rearmed", 0)) == 1 and state_rearmed == "queued:0" and "dlq-poison" in pend_after_rearm))
    # a FRESH 'failed' row (updated_at = now) is NOT yet re-armed (the age gate stops a poison row hot-looping).
    dlq.submit("push", dict(raw_push, after="f" * 40), "dlq-fresh-fail",
               account_key="dlq-7777", repo="acme/dur")
    _admin("UPDATE core.webhook_delivery SET status='failed', attempts=3, updated_at=now() WHERE delivery_key=%s", ("dlq-fresh-fail",))
    rearm_fresh = dlq.rearm_failed(rearm_seconds=3600, limit=100)
    fresh_state = _admin("SELECT status FROM core.webhook_delivery WHERE delivery_key=%s", ("dlq-fresh-fail",))
    checks.append((f"DLQ re-arm respects the age gate: a JUST-failed row is left 'failed' (no hot-loop) "
                   f"(rearmed={rearm_fresh.get('rearmed')}, state={fresh_state})",
                   int(rearm_fresh.get("rearmed", 0)) == 0 and fresh_state == "failed"))

    # ─── (9) AGE-BASED REAPER: terminal (done/failed) rows past the window are reaped; live rows are kept ────
    # The durable inbox keeps 'done' (payload {}, account_key/repo retained) + 'failed' rows FOREVER otherwise —
    # the fourth unbounded grower on the 256 MiB tier. prune_all_accounts_with_authority reaps TERMINAL rows past
    # the window in ONE cross-tenant DELETE, and must NEVER drop a live 'queued'/'processing' row.
    _admin("DELETE FROM core.webhook_delivery")   # clean slate for a precise reaper count
    _admin("INSERT INTO core.webhook_delivery(delivery_key,event_type,account_key,repo,payload,status,done_at,updated_at) VALUES "
           "('reap-done-old','push','7777','acme/dur','{}'::jsonb,'done', now()-interval '40 days', now()-interval '40 days'),"
           "('reap-failed-old','push','7777','acme/dur','{}'::jsonb,'failed', NULL, now()-interval '40 days'),"
           "('reap-erased-old','erased',NULL,NULL,'{}'::jsonb,'done', now()-interval '40 days', now()-interval '40 days'),"
           "('reap-done-new','push','7777','acme/dur','{}'::jsonb,'done', now(), now()),"
           "('reap-queued-old','push','7777','acme/dur','{}'::jsonb,'queued', NULL, now()-interval '40 days'),"
           "('reap-proc-old','push','7777','acme/dur','{}'::jsonb,'processing', NULL, now()-interval '40 days')")
    _admin(
        "INSERT INTO core.webhook_delivery("
        "delivery_key,event_type,account_key,repo,payload,status,done_at,updated_at,"
        "operator_recovery_id,operator_recovered_at,operator_recovery_batch_size,"
        "operator_recovery_batch_token,operator_recovery_count) VALUES "
        "('reap-proof-done','ping','7777','acme/dur','{}'::jsonb,'done',"
        " now()-interval '35 days',now()-interval '35 days','retention-proof',"
        " now()-interval '36 days',2,'00000000-0000-4000-8000-000000000013',1),"
        "('reap-proof-queued','ping','7777','acme/dur','{}'::jsonb,'queued',NULL,"
        " now()-interval '35 days','retention-proof',now()-interval '36 days',2,"
        " '00000000-0000-4000-8000-000000000013',1)"
    )
    reap = _admin("SELECT core.prune_all_accounts_with_authority(now() - interval '30 days')")
    reap = reap if isinstance(reap, dict) else json.loads(reap)
    survivors = set(_admin("SELECT COALESCE(array_agg(delivery_key ORDER BY delivery_key), ARRAY[]::text[]) "
                           "FROM core.webhook_delivery") or [])
    checks.append((f"reaper drops ordinary OLD terminal rows, keeps live work, and preserves the done proof "
                   f"for an unfinished exact operator batch (reaped={reap.get('webhook_deliveries_reaped')}, "
                   f"survivors={sorted(survivors)})",
                   int(reap.get("webhook_deliveries_reaped", -1)) == 3
                   and survivors == {
                       "reap-done-new", "reap-queued-old", "reap-proc-old",
                       "reap-proof-done", "reap-proof-queued",
                   }))

    # ─── (11) RECOVERY ENUMERATION IS ACCOUNT-FAIR BEFORE THE GLOBAL LIMIT ───────────────────────────────
    # One owner's deep old backlog must not consume every row returned by pending(limit), otherwise the runtime's
    # per-account outstanding cap would reject that whole page and a later tenant could remain hidden forever.
    _admin("DELETE FROM core.webhook_delivery")
    _admin(
        "INSERT INTO core.webhook_delivery"
        "(delivery_key,event_type,account_key,repo,payload,status,received_at,updated_at) VALUES "
        "('fair-a1','push','acct-a','a/r','{}'::jsonb,'queued',now()-interval '4 minutes',now()),"
        "('fair-a2','push','acct-a','a/r','{}'::jsonb,'queued',now()-interval '3 minutes',now()),"
        "('fair-b1','push','acct-b','b/r','{}'::jsonb,'queued',now()-interval '2 minutes',now())"
    )
    fair_pending = [row.get("key") for row in store.pending(limit=2)]
    checks.append((f"pending(limit=2) interleaves each account's oldest row before one account's second row "
                   f"(pending={fair_pending})",
                   fair_pending == ["fair-a1", "fair-b1"]))

    # ─── (12) SHUTDOWN LEASE EXPIRY: fast reclaim WITHOUT the requeue race ───────────────────────────────────
    # A rolling deploy's SIGTERM that lands mid-event leaves the row 'processing' until the FULL stale window —
    # blocking its whole account/repo causal lane (the queued=41 lane-freeze incident). The shutdown path expires
    # the owned lease (backdates locked_at) instead of requeueing, so the dying worker's own commit+finish still
    # wins if it lands, while a dead process's row is reclaimed within the grace.
    _admin("DELETE FROM core.webhook_delivery")
    estore = S.DeliveryStore(APP_DSN, max_pending=50, max_attempts=3, stale_seconds=600)
    exp_state = {}

    def _mid_shutdown(et, pl, db, gh, coalesce=None):
        # Model the SIGTERM racing the live run: expire fires WHILE the processor is mid-event.
        with estore._inflight_lock:
            exp_state["registry"] = estore._inflight
        exp_state["expired"] = estore.expire_inflight_lease(grace_seconds=1)
        exp_state["backdated"] = _admin(
            "SELECT locked_at < now() - interval '500 seconds' FROM core.webhook_delivery WHERE delivery_key=%s",
            (pl.get("_veripsa_delivery_key"),))
        return {"ok": True}

    esub = estore.submit("push", dict(raw_push, after="1" * 40), "exp-live",
                         account_key="exp-live-acct", repo="acme/dur")
    estore.wrap_processor(_mid_shutdown)("push", esub["payload"], None, None)
    live_state = _admin("SELECT status FROM core.webhook_delivery WHERE delivery_key=%s", ("exp-live",))
    checks.append((f"expire during a LIVE run backdates the owned lease (registry={exp_state.get('registry')}, "
                   f"expired={exp_state.get('expired')}, backdated={exp_state.get('backdated')}) and the worker's "
                   f"own finish still lands 'done' (state={live_state}) — no requeue race",
                   exp_state.get("registry") == ("exp-live", 1) and exp_state.get("expired") is True
                   and exp_state.get("backdated") is True and live_state == "done"
                   and estore._inflight is None))

    # Crash flow: claim → expire → process dies (no finish). The row must become reclaimable within the grace
    # despite the 600s stale window, and the reclaim takes a FRESH lease generation.
    esub2 = estore.submit("push", dict(raw_push, after="2" * 40), "exp-crash",
                          account_key="exp-crash-acct", repo="acme/dur")
    eclaim = estore.claim(esub2["key"])
    assert eclaim.get("claimed"), eclaim
    estore._set_inflight(esub2["key"], eclaim["lease_generation"])
    exp_ok = estore.expire_inflight_lease(grace_seconds=1)
    estore._clear_inflight(esub2["key"])          # the process is gone; only the durable row remains
    time.sleep(1.1)
    exp_pend = {r.get("key") for r in estore.pending()}
    ereclaim = estore.claim(esub2["key"])
    checks.append((f"crash-after-expire is reclaimed within the grace, NOT the 600s stale window "
                   f"(expired={exp_ok}, pending={'exp-crash' in exp_pend}, reclaimed={ereclaim.get('claimed')}, "
                   f"generation={ereclaim.get('lease_generation')})",
                   exp_ok is True and "exp-crash" in exp_pend and ereclaim.get("claimed") is True
                   and int(ereclaim.get("lease_generation", 0)) == 2))
    assert estore.finish(esub2["key"], ereclaim["lease_generation"])

    # A lost/foreign lease generation must be a NO-OP: the row keeps its fresh lock and stays invisible to recovery.
    esub3 = estore.submit("push", dict(raw_push, after="3" * 40), "exp-foreign",
                          account_key="exp-foreign-acct", repo="acme/dur")
    eclaim3 = estore.claim(esub3["key"])
    assert eclaim3.get("claimed"), eclaim3
    estore._set_inflight(esub3["key"], int(eclaim3["lease_generation"]) + 7)
    foreign_ok = estore.expire_inflight_lease(grace_seconds=1)
    estore._clear_inflight(esub3["key"])
    foreign_pend = {r.get("key") for r in estore.pending()}
    idle_ok = estore.expire_inflight_lease(grace_seconds=1)   # nothing registered → no SQL, False
    checks.append((f"a stale lease generation cannot expire the row (expired={foreign_ok}, "
                   f"still hidden={'exp-foreign' not in foreign_pend}), and expire with no in-flight is a no-op "
                   f"(idle={idle_ok})",
                   foreign_ok is False and "exp-foreign" not in foreign_pend and idle_ok is False))
    assert estore.finish(esub3["key"], eclaim3["lease_generation"])

    # ─── (13) AGED-DEFERRED LANE ESCALATION (issue #847) ───────────────────────────────────────────────────
    # THE INCIDENT: live delivery D claims → 'blocked_by_earlier' (an earlier same-lane row B exists) → the
    # worker drops D's in-memory generation; D stays 'queued'. Restart churn burns B's durable budget → B ends
    # 'failed' (protocol 1) — which STILL blocks the lane — and the only unfreeze was the 1h DLQ re-arm (or
    # never, when disabled). D has no comeback path of its own: GitHub never redelivers a 202'd delivery and the
    # App-level redelivery scan classifies it locally-received/OK. First PROVE the trap, then prove the fix.
    lane = S.DeliveryStore(APP_DSN, max_pending=50, max_attempts=3, stale_seconds=600)
    esc_push = dict(raw_push, repository=dict(raw_push["repository"], id=424242))
    lane.submit("push", dict(esc_push, after="e1" * 20), "esc-blocker",
                account_key="esc-acct", repo="acme/dur")
    lane.submit("push", dict(esc_push, after="e2" * 20), "esc-deferred",
                account_key="esc-acct", repo="acme/dur")
    live_claim = lane.claim("esc-deferred")            # the live in-memory claim at 15:15:08Z of the incident
    # restart-reclaim churn: B exhausted its durable budget and landed on the DLQ; both rows are now AGED.
    _admin("UPDATE core.webhook_delivery SET received_at=now()-interval '30 minutes' WHERE delivery_key=%s",
           ("esc-blocker",))
    _admin("UPDATE core.webhook_delivery SET received_at=now()-interval '20 minutes' WHERE delivery_key=%s",
           ("esc-deferred",))
    _admin("UPDATE core.webhook_delivery SET status='failed', attempts=3, locked_at=NULL, "
           "updated_at=now()-interval '20 minutes', last_error='stale processing exceeded durable attempts' "
           "WHERE delivery_key=%s", ("esc-blocker",))
    trap_pending = {r.get("key") for r in lane.pending()}
    trap_claim = lane.claim("esc-deferred")
    trap_rearm = lane.rearm_failed(3600, 100)          # the 1h DLQ cadence does NOT wake it yet (B too young)
    checks.append((f"TRAP (issue #847): a 'blocked_by_earlier' deferral has NO comeback — pending() offers "
                   f"neither the failed head nor the deferred row, claim still defers, and the 1h DLQ re-arm is "
                   f"not due (live={live_claim.get('reason')}, pending={sorted(trap_pending & {'esc-blocker', 'esc-deferred'})}, "
                   f"claim={trap_claim.get('reason')}, rearmed={trap_rearm.get('rearmed')})",
                   live_claim.get("claimed") is False and live_claim.get("reason") == "blocked_by_earlier"
                   and "esc-blocker" not in trap_pending and "esc-deferred" not in trap_pending
                   and trap_claim.get("claimed") is False and trap_claim.get("reason") == "blocked_by_earlier"
                   and int(trap_rearm.get("rearmed", -1)) == 0))

    # THE FIX: the escalation re-arms the AGED failed lane head blocking the AGED queued row — order-preserving.
    esc_res = lane.escalate_blocked(escalate_seconds=600, limit=100)
    blocker_state = _admin("SELECT status||':'||attempts FROM core.webhook_delivery WHERE delivery_key=%s",
                           ("esc-blocker",))
    checks.append((f"escalation re-arms the aged failed lane head for causal replay (res={esc_res}, "
                   f"blocker={blocker_state})",
                   int(esc_res.get("escalated", 0)) >= 1 and int(esc_res.get("aged_queued", 0)) >= 1
                   and blocker_state == "queued:0"))

    # …and the ordinary recovery loop now drains the lane IN ORDER, each delivery exactly once.
    esc_applied = []

    def _esc_work(et, pl, db, gh, coalesce=None):
        esc_applied.append(DQ._obj(pl).get("_veripsa_delivery_key"))
        return {"ok": True}

    _esc_proc = lane.wrap_processor(_esc_work)

    class _EscWorker:                      # inline worker: processes synchronously, only this scenario's keys
        def can_accept(self, event_type, payload, delivery=None):
            return delivery in ("esc-blocker", "esc-deferred")

        def submit(self, event_type, payload, delivery, register_push=True, recovered=False):
            _esc_proc(event_type, payload, None, None)
            return True

    for _ in range(4):
        DQ._recover_pending_deliveries(lane, _EscWorker())
    lane_states = _admin("SELECT string_agg(delivery_key||'='||status,',' ORDER BY delivery_key) "
                         "FROM core.webhook_delivery WHERE delivery_key IN ('esc-blocker','esc-deferred')")
    checks.append((f"after escalation, recovery replays the lane IN causal order exactly once "
                   f"(applied={esc_applied}, states={lane_states})",
                   esc_applied == ["esc-blocker", "esc-deferred"]
                   and lane_states == "esc-blocker=done,esc-deferred=done"))

    # IDEMPOTENT: a late replay of the already-completed deferred delivery must be a harmless duplicate — the
    # claim answers already_finished, the handler does NOT run, and another escalate+recovery pass is a no-op.
    dup_res = _esc_proc("push", DQ.with_delivery_key({}, "esc-deferred"), None, None)
    esc_res2 = lane.escalate_blocked(escalate_seconds=600, limit=100)
    DQ._recover_pending_deliveries(lane, _EscWorker())
    checks.append((f"an already-completed delivery is NOT double-processed (dup={dup_res.get(DQ._WORKER_CLAIM_OUTCOME)}"
                   f"/{dup_res.get('claim_reason')}, re-escalate={esc_res2.get('escalated')}, applied={esc_applied})",
                   dup_res.get(DQ._WORKER_CLAIM_OUTCOME) == "duplicate"
                   and dup_res.get("claim_reason") == "already_finished"
                   and int(esc_res2.get("escalated", -1)) == 0
                   and esc_applied == ["esc-blocker", "esc-deferred"]))

    # AGE GATE + QUARANTINE: an aged follower immediately re-arms a freshly-terminalized protocol-1 head (esc2);
    # waiting another full age from failed.updated_at recreated the reported long silence. A FRESH queued follower
    # (esc3) is left alone, and a protocol-0 failed head (esc4) is NEVER re-armed.
    for acct, head_age, tail_age, head_v0 in (
            ("esc2-acct", "0 seconds", "20 minutes", False),     # aged follower spends the finite epoch
            ("esc3-acct", "20 minutes", "0 seconds", False),     # deferred row too fresh
            ("esc4-acct", "20 minutes", "20 minutes", True)):    # protocol-0 quarantine
        lane.submit("push", dict(raw_push, after=(acct[3] + "a") * 20), f"{acct}-head", account_key=acct, repo="acme/dur")
        lane.submit("push", dict(raw_push, after=(acct[3] + "b") * 20), f"{acct}-tail", account_key=acct, repo="acme/dur")
        _admin("UPDATE core.webhook_delivery SET status='failed', attempts=3, "
               "received_at=now()-interval '30 minutes', updated_at=now()-interval %s, "
               "causal_order_version=%s WHERE delivery_key=%s",
               (head_age, 0 if head_v0 else 1, f"{acct}-head"))
        _admin("UPDATE core.webhook_delivery SET received_at=now()-interval %s WHERE delivery_key=%s",
               (tail_age, f"{acct}-tail"))
    gate_res = lane.escalate_blocked(escalate_seconds=600, limit=100)
    gate_states = _admin("SELECT string_agg(delivery_key||'='||status,',' ORDER BY delivery_key) "
                         "FROM core.webhook_delivery WHERE delivery_key IN "
                         "('esc2-acct-head','esc3-acct-head','esc4-acct-head')")
    checks.append((f"aged follower re-arms a fresh protocol-1 head while fresh followers and protocol-0 stay fenced (res={gate_res}, "
                   f"heads={gate_states})",
                   int(gate_res.get("escalated", -1)) == 1
                   and gate_states == "esc2-acct-head=queued,esc3-acct-head=failed,esc4-acct-head=failed"))

    # NEVER-CRASH on junk rows: an aged NULL-account row with no repository id has no lane (untouched), and a
    # malformed non-numeric repository id is also deliberately NOT promoted into a narrow cross-account lane.
    # Honest-unknown accounts remain independent, so the failed junk row stays quarantined. The sweep must return
    # counts, not throw.
    _admin("INSERT INTO core.webhook_delivery(delivery_key,event_type,payload,status,received_at,updated_at,causal_order_version) "
           "VALUES ('esc-junk-null','pull_request','{}'::jsonb,'queued',now()-interval '45 minutes',now(),1),"
           "('esc-junk-f','pull_request','{\"repository\":{\"id\":\"not-a-number\"}}'::jsonb,'failed',"
           "now()-interval '50 minutes',now()-interval '40 minutes',1),"
           "('esc-junk-q','pull_request','{\"repository\":{\"id\":\"not-a-number\"}}'::jsonb,'queued',"
           "now()-interval '45 minutes',now(),1)")
    try:
        junk_res = lane.escalate_blocked(escalate_seconds=600, limit=100)
        junk_crashed = False
    except Exception as e:  # noqa: BLE001 — the assertion IS "it must not raise"
        junk_res, junk_crashed = {"error": str(e)[:120]}, True
    junk_states = _admin("SELECT string_agg(delivery_key||'='||status,',' ORDER BY delivery_key) "
                         "FROM core.webhook_delivery WHERE delivery_key IN "
                         "('esc-junk-null','esc-junk-f','esc-junk-q')")
    checks.append((f"junk rows never crash the sweep; malformed repository ids remain conservative, not lanes "
                   f"(res={junk_res}, states={junk_states})",
                   junk_crashed is False and isinstance(junk_res, dict)
                   and junk_states == "esc-junk-f=failed,esc-junk-null=queued,esc-junk-q=queued"))

    _teardown()

    ok = sum(1 for _, p in checks if p)
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    print(f"\n-- {ok}/{len(checks)} durable-inbox durability + authority checks passed --")
    if ok != len(checks):
        print("DURABLE INBOX GATE: FAIL")
        return 1
    print("DURABLE INBOX GATE: PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:
        _teardown()
        print(f"DURABLE INBOX GATE: FAIL (unexpected error: {type(e).__name__}: {str(e)[:200]})")
        sys.exit(1)
