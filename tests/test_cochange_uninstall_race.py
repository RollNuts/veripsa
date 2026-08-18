#!/usr/bin/env python3
"""CO-CHANGE / UNINSTALL LIVE-RACE gate (small-findings root-fix sweep, finding 3).

THE GAP (root-fixed here): the background co-change populate/increment runs on an AUTOCOMMIT connection that calls
core.assert_account_live_with_authority() as ONE statement and THEN the co-change WRITE
(ingest_cochange_with_authority / co_change_filter_unseen_commits_with_authority) as ANOTHER. Because the two are
SEPARATE autocommit transactions, there is a real TOCTOU WINDOW between them: the account-wide uninstall purge
(which holds NO advisory lock — the lock-key asymmetry the iter-4 audit calls out) can commit its TOMBSTONE in that
gap, and the writer would then RESURRECT co_change rows (private file PATHS) into a tenant we just purged. The
pre-flight assert cannot close it because it commits BEFORE the write begins. The earlier audit verified the
tombstone is SET on uninstall but did NOT race the writer LIVE.

THE ROOT FIX: the liveness check is now ATOMIC WITH THE WRITE, inside the writer's own statement-transaction. The
writer row-locks the account FOR SHARE (which conflicts with the purge's plain UPDATE plan='free' [FOR NO KEY
UPDATE] and the erase's DELETE [FOR UPDATE], so the two can no longer interleave) and then refuses (42501) if a
tombstone exists. Whoever takes the account lock first wins: if the purge committed first we SEE the tombstone and
refuse; if we wrote first the purge's account-wide DELETE that follows reaps our rows. No post-tombstone residue.

PROVES, against a REAL local Postgres (two live connections):
  (A) AUTOCOMMIT-GAP REPRO: pre-flight assert passes, THEN the purge commits the tombstone, THEN the co-change write
      is attempted — the write is REFUSED (42501) and ZERO co_change rows land (the resurrection is blocked).
  (B) the SEEN-COMMIT ledger writer is guarded the same way (the per-push increment's first write).
  (C) CONCURRENT serialization: a writer that holds the account row (in an open txn) BLOCKS the purge until it
      commits; once the writer's legitimately-live write commits, the purge's account-wide DELETE reaps it → no
      residue either way (the two can no longer both "win").
  (D) STRUCTURAL: both co-change writers reference the tombstone table + the FOR SHARE account lock (a refactor that
      drops the in-txn guard FAILS the gate).
  (E) the legitimate live path still works: a write on a NON-tombstoned (reactivated) account LANDS.

PROCESS-UNIQUE scratch DB (parallel-safe). Run:  python3 tests/test_cochange_uninstall_race.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
from _lifecycle_fixture import absent_installation_proof, seed_processing_uninstall  # noqa: E402

DB = "veripsa_ccrace_" + str(os.getpid())
checks = []

CC_PAIR = json.dumps([{"a": "a.py", "b": "b.py", "co": 7, "n_a": 7, "n_b": 7, "n_total": 7, "strength": 1.0, "lift": 2.0}])


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def conn_for(role, autocommit=True):
    c = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    c.autocommit = autocommit
    return c


def app_pinned(install_id, autocommit=True):
    """A held App connection that ENTERED `install_id` → pinned to its tenant for the whole 'event'."""
    c = conn_for("veripsa_app", autocommit=autocommit)
    with c.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", (install_id,))
    if not autocommit:
        c.commit()
    return c


def owner_count(account, sql_tail, args=()):
    # NON-autocommit on purpose: set_config(...,true) is TXN-LOCAL, so the pin + the read must be in ONE transaction
    # (an autocommit connection would run them as two txns → the pin is lost → FORCE-RLS hides every row → a read of
    # 0 that is meaningless). The implicit txn holds the pin across both statements; we commit the read at the end.
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cur.execute(sql_tail, args)
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
    finally:
        conn.close()


def tomb(account):
    conn = conn_for("veripsa_migrator")
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT count(*) FROM core.account_lifecycle_tombstone WHERE account_id=%s", (account,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def purge_absent(conn, install_id: str, delivery_key: str):
    deleted_installation_id = f"A-{install_id}"
    seed_processing_uninstall(
        f"postgresql://veripsa_migrator@localhost/{DB}",
        delivery_key,
        install_id,
        deleted_installation_id,
    )
    proof = json.dumps(absent_installation_proof(install_id, deleted_installation_id))
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('core.current_delivery_key',%s,false)", (delivery_key,))
        cur.execute(
            "SELECT core.purge_account_working_set_with_authority(%s::jsonb)",
            (proof,),
        )
        return cur.fetchone()[0]


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    print("CO-CHANGE / UNINSTALL LIVE RACE (small-findings root fix, finding 3)")
    REPO = "acme/web"

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # (A) AUTOCOMMIT-GAP REPRO — the writer's pre-flight assert passes, the purge commits the tombstone in the gap,
    #     then the write is attempted. The in-writer guard must REFUSE it → no resurrection.
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    INSTALL, ACCT = "701", "ACCT-GH-701"
    writer = app_pinned(INSTALL)                      # the background co-change writer's connection (autocommit, like prod)

    def w(sql, args=()):
        with writer.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None

    # the writer does its PRE-FLIGHT liveness assert FIRST — and on a still-live account it passes (this is the
    # exact moment of the gap: in prod this commits, then the clone/fold happens, then the write).
    pre = w("SELECT core.assert_account_live_with_authority()")
    chk(pre == ACCT, "(A) the writer's pre-flight assert PASSES on the still-live account (the gap is now open)")

    # NOW the uninstall purge runs to completion on a SEPARATE connection (sets the tombstone, commits).
    purge_conn = app_pinned(INSTALL)
    purge_absent(purge_conn, INSTALL, "ccrace-delete-701")
    purge_conn.close()
    chk(tomb(ACCT) == 1, "(A) the account-wide purge committed the tombstone in the gap")

    # the writer, unaware, now attempts its co_change WRITE. WITHOUT the fix this lands (resurrection). WITH the fix
    # the writer's in-txn guard (FOR SHARE account lock + tombstone check) REFUSES it.
    refused = None
    try:
        w("SELECT core.ingest_cochange_with_authority(%s,%s)", (CC_PAIR, REPO))
    except psycopg2.Error as e:
        refused = e.pgcode
        writer.rollback()
    chk(refused == "42501",
        f"(A) ROOT FIX: the post-tombstone co_change write is REFUSED in-txn [42501] (got {refused}) — no autocommit-gap resurrection")
    chk(owner_count(ACCT, "SELECT count(*) FROM core.co_change WHERE account_id=%s", (ACCT,)) == 0,
        "(A) ZERO co_change rows landed for the purged tenant (the resurrection is blocked)")
    writer.close()

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # (B) the SEEN-COMMIT ledger writer (the per-push increment's FIRST write) is guarded identically.
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    INSTALL2, ACCT2 = "702", "ACCT-GH-702"
    w2c = app_pinned(INSTALL2)

    def w2(sql, args=()):
        with w2c.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None

    w2("SELECT core.assert_account_live_with_authority()")
    p2 = app_pinned(INSTALL2)
    purge_absent(p2, INSTALL2, "ccrace-delete-702")
    p2.close()
    refused2 = None
    try:
        w2("SELECT core.co_change_filter_unseen_commits_with_authority(%s,%s)", (REPO, ["a" * 40, "b" * 40]))
    except psycopg2.Error as e:
        refused2 = e.pgcode
        w2c.rollback()
    chk(refused2 == "42501",
        f"(B) the seen-commit ledger write is also REFUSED post-tombstone in-txn [42501] (got {refused2})")
    chk(owner_count(ACCT2, "SELECT count(*) FROM core.co_change_seen_commit WHERE account_id=%s", (ACCT2,)) == 0,
        "(B) ZERO seen-commit rows landed for the purged tenant")
    w2c.close()

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # (C) CONCURRENT serialization (two connections, real threads): a writer holding the account row in an OPEN txn
    #     BLOCKS the purge until it commits; the writer's legitimately-LIVE write commits, then the purge's
    #     account-wide DELETE reaps it → no residue. Proves the FOR SHARE lock actually serializes the two.
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    INSTALL3, ACCT3 = "703", "ACCT-GH-703"
    # writer: a NON-autocommit connection so we can HOLD the txn open across the purge attempt.
    wc = app_pinned(INSTALL3, autocommit=False)
    purge_started = threading.Event()
    purge_done = threading.Event()
    purge_err = {}

    def purge_thread():
        pc = app_pinned(INSTALL3)
        purge_started.set()
        try:
            purge_absent(pc, INSTALL3, "ccrace-delete-703")  # BLOCKS on the writer's FOR SHARE
        except Exception as e:
            purge_err["e"] = str(e)
        finally:
            pc.close()
            purge_done.set()

    # the writer opens its txn and performs the gated write (which takes FOR SHARE on the account row and, account
    # still live, INSERTs the pair) but does NOT yet commit — holding the account-row lock.
    with wc.cursor() as cur:
        cur.execute("SELECT core.ingest_cochange_with_authority(%s,%s)", (CC_PAIR, REPO))
    # launch the purge; it must BLOCK on the held account-row lock (FOR SHARE vs the purge's UPDATE).
    t = threading.Thread(target=purge_thread, daemon=True)
    t.start()
    purge_started.wait(5)
    time.sleep(0.5)
    blocked_while_held = not purge_done.is_set()
    chk(blocked_while_held, "(C) the purge BLOCKS while the writer holds the account row (FOR SHARE serializes the two)")
    # the writer commits its legitimately-live write; the purge then unblocks and runs account-wide.
    wc.commit()
    wc.close()
    t.join(10)
    chk(purge_done.is_set() and not purge_err, f"(C) the purge proceeded once the writer committed (err={purge_err.get('e')})")
    chk(tomb(ACCT3) == 1, "(C) the purge set the tombstone after winning the lock")
    chk(owner_count(ACCT3, "SELECT count(*) FROM core.co_change WHERE account_id=%s", (ACCT3,)) == 0,
        "(C) the writer's committed pair was REAPED by the purge's account-wide DELETE → no residue (the other ordering is safe too)")

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # (D) STRUCTURAL: both co-change writers carry the in-txn guard (tombstone reference + the FOR SHARE account lock).
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    def body(sig):
        conn = conn_for("veripsa_migrator")
        try:
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT pg_get_functiondef(%s::regprocedure)", (sig,))
                return cur.fetchone()[0]
        finally:
            conn.close()

    for sig, name in (("core.ingest_cochange_with_authority(jsonb,text)", "ingest_cochange_with_authority"),
                      ("core.co_change_filter_unseen_commits_with_authority(text,text[])", "co_change_filter_unseen_commits_with_authority")):
        b = body(sig)
        has = "account_lifecycle_tombstone" in b and "FOR SHARE" in b
        chk(has, f"(D) STRUCTURAL: {name} carries the in-txn resurrection guard (tombstone check + FOR SHARE account lock)")

    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    # (E) the legitimate LIVE path still works: a write on a NON-tombstoned account LANDS (no over-refusal).
    # ════════════════════════════════════════════════════════════════════════════════════════════════════════
    INSTALL4, ACCT4 = "704", "ACCT-GH-704"
    lc = app_pinned(INSTALL4)
    with lc.cursor() as cur:
        cur.execute("SELECT core.ingest_cochange_with_authority(%s,%s)", (CC_PAIR, REPO))
        e_cc = cur.fetchone()[0]
        cur.execute("SELECT core.co_change_filter_unseen_commits_with_authority(%s,%s)", (REPO, ["c" * 40]))
        e_seen = cur.fetchone()[0]
    lc.close()
    e_cc_rows = owner_count(ACCT4, "SELECT count(*) FROM core.co_change WHERE account_id=%s", (ACCT4,))
    e_seen_rows = owner_count(ACCT4, "SELECT count(*) FROM core.co_change_seen_commit WHERE account_id=%s", (ACCT4,))
    chk(e_cc_rows == 1 and e_seen_rows == 1,
        f"(E) the legitimate live-path co-change + seen-commit writes still LAND on a non-tombstoned account "
        f"(cc_write={e_cc} seen_write={e_seen} cc_rows={e_cc_rows} seen_rows={e_seen_rows})")

    print()
    if all(checks):
        print("CO-CHANGE/UNINSTALL RACE GATE: PASS")
        return 0
    print(f"CO-CHANGE/UNINSTALL RACE GATE: FAIL ({sum(1 for c in checks if not c)} of {len(checks)} failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
