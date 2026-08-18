#!/usr/bin/env python3
"""PLAN-EVENT ORDERING gate (audit iter-5 P2) — the marketplace/billing plan setter must be MONOTONIC against
UNORDERED, RE-DELIVERABLE webhooks, so a stale event cannot wrongly throttle a paying customer.

THE DEFECT. core.set_account_plan_with_authority is VALUE-idempotent (re-applying the same plan is harmless),
but GitHub Marketplace webhook delivery is UNORDERED and RE-DELIVERABLE with no timestamp monotonicity. So a
re-delivered `cancelled`(→free) event can arrive AFTER a later `purchased`/`changed`(→pro) event and overwrite
the STALE plan — the paying customer is silently downgraded to 'free' and the per-plan graph_units wall
re-arms = a billing DoS (a paying customer wrongly throttled). The graph-ingest path already guards this exact
shape with captured_at MONOTONICITY (db/schema/30_gate.sql: it refuses to overwrite a graph captured at a
strictly-newer commit time than a reordered push). This is the COMMERCIAL-path mirror.

THE FIX (db/schema/30_gate.sql). The setter takes a trailing p_effective_at timestamptz (the marketplace event's
effective_date, threaded from the webhook handler) and persists the LAST-APPLIED effective time per account in a
NEW FK-free table core.account_plan_event (NOT an ALTER-TABLE column on the busy core.account — so the delta
applies contention-free on a live prod). It REFUSES a plan write whose p_effective_at is STRICTLY OLDER than the
stored high-water mark (keeping the newer plan), and ADVANCES the mark (GREATEST) on every applied write. The
guard is GUARDED: inert when p_effective_at is NULL (value-idempotency still holds), and an equal/newer event
applies. The installation-keyed sibling threads the same arg. Both lifecycle paths (uninstall purge + GDPR
erase) forget the per-account mark so a reinstall + fresh purchase is not blocked by a stale high-water mark.

WHAT THIS GATE PROVES (live, on the ephemeral test Postgres — never grep; the only truth is the running DB):
  1. ORDERED APPLY: purchased(t1,'pro') then cancelled(t2>t1,'free') → plan ends 'free' (the LATER event wins).
  2. THE DEFECT CLOSED: purchased(t2,'pro') then a RE-DELIVERED cancelled(t1<t2,'free') → plan STAYS 'pro' (the
     stale older event is REFUSED, not applied — a paying customer is NOT wrongly throttled). And the high-water
     mark is unchanged (still t2), so a second re-delivery of the old event is refused again.
  3. EQUAL/NEWER applies: an event AT the mark (t2) and an event NEWER than it (t3) both apply (boundary + happy).
  4. INERT WITHOUT A TIME: a NULL p_effective_at always applies (value-idempotency unchanged) and does NOT plant
     or move a high-water mark (so it never blocks a later real-timestamped event).
  5. TENANT-SCOPE: account B's mark + plan are entirely independent of account A's reordered stream.
  6. LIFECYCLE RESET: after a GDPR erase, the per-account high-water mark is gone (the erase receipt counts it),
     so a fresh post-reinstall purchase with an OLDER-than-the-erased-mark time still applies (not stale-refused).
  7. STRUCTURE: core.account_plan_event is FK-free (contention-free apply) + REVOKEd from PUBLIC (no buyer reach).

Run:  python3 tests/test_plan_event_ordering.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe), like the sibling lifecycle/billing gates.
DB = "veripsa_planorder_" + str(os.getpid())

# two GH account ids → two tenants. A is the reordered stream under test; B proves tenant-scope independence.
A_ID, B_ID = "70001", "70002"
A_ACCT, B_ACCT = "ACCT-GH-" + A_ID, "ACCT-GH-" + B_ID

# three strictly-increasing effective times (ISO-8601, the marketplace effective_date shape).
T1 = "2026-01-01T00:00:00+00:00"
T2 = "2026-02-01T00:00:00+00:00"
T3 = "2026-03-01T00:00:00+00:00"

checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def conn_for(role):
    return psycopg2.connect(f"postgresql://{role}@localhost/{DB}")


def set_plan(gh_id, plan, effective_at):
    """Call the gated setter AS the App (its delegated path), exactly as the live handler does (3-arg form)."""
    conn = conn_for("veripsa_app")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.set_account_plan_with_authority(%s,%s,%s)", (gh_id, plan, effective_at))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def plan_of(account):
    """Ground-truth plan column (migrator, account pinned — core.account is FORCE-RLS)."""
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cur.execute("SELECT plan FROM core.account WHERE account_id=%s", (account,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def hwm(account):
    """The stored high-water effective time for account (migrator; account_plan_event has no per-account RLS).
    Returns the timestamptz (or None if no row)."""
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT last_effective_at FROM core.account_plan_event WHERE account_id=%s", (account,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def erase_as_app():
    """Run the GDPR erase AS the App in the connection-resolved tenant (returns the receipt jsonb)."""
    conn = conn_for("veripsa_app")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.erase_account_with_authority()")
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    # ── 1. ORDERED APPLY (the baseline causal case): the LATER event wins. ─────────────────────────────────────
    set_plan(A_ID, "pro", T1)
    set_plan(A_ID, "free", T2)        # cancelled at a LATER time → genuinely downgrades
    chk(plan_of(A_ACCT) == "free",
        f"1 ORDERED: purchased(t1,'pro') then cancelled(t2>t1,'free') → plan is 'free' (the later event wins) "
        f"(plan={plan_of(A_ACCT)})")
    chk(hwm(A_ACCT) is not None,
        "1 ORDERED: a high-water mark is recorded after the applied events")

    # reset A for the defect scenario: a fresh tenant-equivalent state. Re-seed 'pro' at t2 as the CURRENT truth.
    set_plan(A_ID, "pro", T2)
    chk(plan_of(A_ACCT) == "pro",
        f"2 SETUP: the current applied truth is purchased(t2,'pro') (plan={plan_of(A_ACCT)})")
    mark_before = hwm(A_ACCT)

    # ── 2. THE DEFECT CLOSED: a RE-DELIVERED OLDER cancelled(t1<t2) must NOT regress the paying plan. ──────────
    set_plan(A_ID, "free", T1)        # the stale, reordered re-delivery
    chk(plan_of(A_ACCT) == "pro",
        f"2 DEFECT CLOSED: a re-delivered cancelled(t1<t2,'free') does NOT overwrite the later 'pro' — the paying "
        f"customer is NOT wrongly throttled (plan stays {plan_of(A_ACCT)})")
    chk(hwm(A_ACCT) == mark_before,
        "2 DEFECT CLOSED: the high-water mark is UNCHANGED by the refused stale event (a second re-delivery is "
        "refused again, not slowly advanced backward)")
    # a SECOND re-delivery of the same old event is still refused (idempotent refusal).
    set_plan(A_ID, "free", T1)
    chk(plan_of(A_ACCT) == "pro",
        "2 DEFECT CLOSED: a SECOND re-delivery of the stale cancelled is refused again (plan still 'pro')")

    # ── 3. EQUAL applies (boundary: an event AT the mark is not 'strictly older'); NEWER applies (happy path). ──
    set_plan(A_ID, "free", T2)        # EQUAL to the mark → NOT strictly older → applies (this is the real t2 cancel)
    chk(plan_of(A_ACCT) == "free",
        f"3 EQUAL: an event AT the high-water time (t2) applies (only STRICTLY-older is refused) "
        f"(plan={plan_of(A_ACCT)})")
    set_plan(A_ID, "pro", T3)         # strictly newer → applies
    chk(plan_of(A_ACCT) == "pro" and hwm(A_ACCT) is not None,
        f"3 NEWER: a strictly-newer event (t3) applies and advances the mark (plan={plan_of(A_ACCT)})")

    # ── 4. INERT WITHOUT A TIME: a NULL effective_at always applies + plants/moves NO mark (B is pristine). ────
    chk(hwm(B_ACCT) is None, "4 INERT SETUP: account B has no high-water mark yet")
    set_plan(B_ID, "pro", None)       # no effective_date in the payload
    chk(plan_of(B_ACCT) == "pro",
        f"4 INERT: a NULL effective_at applies (value-idempotency unchanged) (plan={plan_of(B_ACCT)})")
    chk(hwm(B_ACCT) is None,
        "4 INERT: a NULL effective_at does NOT plant a high-water mark (so it never blocks a later real-timed event)")
    # a NULL write does not regress a later real-timestamped event either:
    set_plan(B_ID, "free", T1)        # now a real-timed event lands cleanly (no mark was in the way)
    chk(plan_of(B_ACCT) == "free" and hwm(B_ACCT) is not None,
        "4 INERT: a subsequent real-timed event applies + plants the mark (the NULL write left no stale barrier)")

    # ── 5. TENANT-SCOPE: A's reordered stream never touched B, and vice-versa. ────────────────────────────────
    chk(plan_of(A_ACCT) == "pro",
        f"5 TENANT-SCOPE: account A is unaffected by B's writes (plan={plan_of(A_ACCT)})")
    chk(plan_of(B_ACCT) == "free",
        f"5 TENANT-SCOPE: account B is unaffected by A's reordered stream (plan={plan_of(B_ACCT)})")

    # ── 6. LIFECYCLE RESET: a GDPR erase forgets A's mark (receipt counts it), so a fresh post-reinstall purchase
    #    with an OLDER-than-the-erased-mark time still APPLIES (the stale barrier is gone). ──────────────────────
    # point the App identity at A so the erase runs in A's tenant (bootstrap pins veripsa_app → ACCT-DEMO; re-bind).
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            # ensure A has an agent + an App credential bound to it (provision_seat is idempotent).
            cur.execute("SELECT core.provision_seat(%s,'A Co','AG-A70001','a-writer','veripsa_a70001_agent')", (A_ACCT,))
            cur.execute("INSERT INTO core.installation_account(installation_id, account_id) VALUES (%s,%s) "
                        "ON CONFLICT (installation_id) DO NOTHING", (A_ID, A_ACCT))
            cur.execute("INSERT INTO core.credential(role_name, agent_id, account_id) VALUES ('veripsa_app','AG-A70001',%s) "
                        "ON CONFLICT (role_name) DO UPDATE SET account_id=EXCLUDED.account_id, agent_id=EXCLUDED.agent_id",
                        (A_ACCT,))
    finally:
        conn.close()
    chk(hwm(A_ACCT) is not None, "6 RESET SETUP: account A has a high-water mark before the erase")
    receipt = erase_as_app()
    receipt = receipt if isinstance(receipt, dict) else __import__("json").loads(receipt)
    chk(receipt.get("account") == A_ACCT and "plan_events" in receipt.get("erased", {}),
        f"6 RESET: the GDPR erase receipt counts plan_events in its deletion manifest "
        f"(reported={receipt.get('erased', {}).get('plan_events')})")
    chk(hwm(A_ACCT) is None,
        "6 RESET: the per-account high-water mark is GONE after the erase (the stale barrier cannot outlive the tenant)")
    # a fresh post-reinstall purchase, even with a time OLDER than the erased mark (t1), applies cleanly now.
    set_plan(A_ID, "pro", T1)
    chk(plan_of(A_ACCT) == "pro",
        "6 RESET: a fresh purchase after the erase (even at an OLDER time t1) APPLIES — not refused by a ghost mark "
        f"(plan={plan_of(A_ACCT)})")

    # ── 7. STRUCTURE — account_plan_event is FK-free (contention-free prod apply) + REVOKEd from PUBLIC. ────────
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM pg_constraint con
                           JOIN pg_class c ON c.oid = con.conrelid
                           JOIN pg_namespace n ON n.oid = c.relnamespace
                           WHERE n.nspname='core' AND c.relname='account_plan_event' AND con.contype='f'""")
            n_fks = cur.fetchone()[0]
            cur.execute("SELECT has_table_privilege('veripsa_writer','core.account_plan_event','SELECT')")
            writer_can_read = cur.fetchone()[0]
            cur.execute("SELECT has_table_privilege('veripsa_reader','core.account_plan_event','SELECT')")
            reader_can_read = cur.fetchone()[0]
    finally:
        conn.close()
    chk(n_fks == 0,
        f"7 STRUCTURE: core.account_plan_event has ZERO foreign keys (new FK-free table → contention-free prod "
        f"apply, no lock on the busy core.account) (fks={n_fks})")
    chk(not writer_can_read and not reader_can_read,
        "7 STRUCTURE: core.account_plan_event is REVOKEd from PUBLIC — a buyer writer/reader cannot read it "
        f"(writer={writer_can_read}, reader={reader_can_read})")

    ok = all(checks)
    print("PLAN-EVENT ORDERING GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
