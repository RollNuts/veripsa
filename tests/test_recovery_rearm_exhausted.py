#!/usr/bin/env python3
"""RECOVERY-QUEUE RE-ARM gate (stabilization 2026-07-19).

An EXHAUSTED (attempt_count>=3) but still-REDELIVERABLE, still-in-window failed GitHub delivery has no comeback
path: claim_github_delivery_redelivery() offers only attempt_count<3 rows, prune keeps the unresolved row ~33 days,
and the CRITICAL `github_delivery_redelivery_exhausted` depth alert re-fires every watchdog tick for that whole
retention. When the exhaustion cause was a TRANSIENT outbound outage that has since cleared, those rows page forever
with no operator comeback short of a raw table write.

`core.rearm_exhausted_github_delivery_recovery_with_authority` closes that, mirroring the DLQ re-arm: an exhausted,
still-redeliverable, still-in-window row whose LAST attempt is older than the re-arm age gets ONE fresh attempt
budget so the recovery loop redelivers it once more (only a GitHub OK / local receipt actually resolves it — it never
fakes delivery). This gate proves, against a REAL local Postgres:

  (1) an eligible exhausted redeliverable/timeout row IS re-armed (attempt_count->0, generation bumped, claim
      cleared, next_attempt_at due) and becomes claim-eligible again;
  (2) the AGE gate holds — a freshly-attempted exhausted row is NOT re-armed (no redelivery hot-loop);
  (3) the CLASS gate holds — a 'terminal' row (GitHub cannot redeliver) is NOT re-armed;
  (4) the WINDOW gate holds — an out-of-three-day-window row (a redeliver would 404) is NOT re-armed;
  (5) a resolved row and a not-yet-exhausted (attempt<3) row are untouched;
  (6) it is IDEMPOTENT — a second immediate sweep re-arms nothing;
  (7) the depth alert breakdown moves correctly (exhausted down, eligible up);
  (8) veripsa_app (the role the recovery loop runs as) may EXECUTE it; PUBLIC may not.

PROCESS-UNIQUE scratch DB (parallel-safe). Run:  python3 tests/test_recovery_rearm_exhausted.py
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

DB = "veripsa_recoveryrearm_" + str(os.getpid())
checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def conn_for(role):
    return psycopg2.connect(f"postgresql://{role}@localhost/{DB}")


def admin(sql, args=()):
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            try:
                row = cur.fetchone()
            except psycopg2.ProgrammingError:
                return None
            return row[0] if row else None
    finally:
        conn.close()


def seed(guid, klass, attempt_count, *, resolved=False, window_hours=24,
         last_attempt_ago_secs=86400, delivery_id=1):
    """Seed one github_delivery_recovery row (migrator = owner; the table has no per-account RLS)."""
    admin(
        "INSERT INTO core.github_delivery_recovery("
        "  delivery_guid, latest_delivery_id, latest_delivered_at, latest_status_code, latest_status,"
        "  latest_recovery_class, resolved, attempt_count, attempt_generation, window_expires_at,"
        "  last_attempt_at, next_attempt_at) "
        "VALUES (%s,%s, now()-interval '1 hour', %s, %s, %s, %s, %s, 7,"
        "  now()+make_interval(hours=>%s), now()-make_interval(secs=>%s), now())",
        (guid, delivery_id, 500 if klass != 'timeout' else -1,
         'Other' if klass != 'timeout' else 'Timed Out',
         klass, resolved, attempt_count, window_hours, last_attempt_ago_secs))


def row(guid, col):
    return admin(f"SELECT {col} FROM core.github_delivery_recovery WHERE delivery_guid=%s", (guid,))


def depth_key(key):
    return int(admin("SELECT (core.github_delivery_recovery_depth_with_authority()->>%s)::int", (key,)) or 0)


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:]); print(r.stderr[-2000:])
        print("RECOVERY REARM EXHAUSTED GATE: FAIL (bootstrap)")
        return 2
    try:
        # A: eligible exhausted redeliverable (old attempt) → re-arm.  B: exhausted timeout but FRESH attempt (age
        # gate).  C: exhausted TERMINAL (class gate).  D: exhausted redeliverable but OUT OF WINDOW (window gate).
        # E: resolved.  F: not yet exhausted (attempt<3).
        seed("A", "redeliverable", 3, last_attempt_ago_secs=86400, delivery_id=101)
        seed("B", "timeout", 3, last_attempt_ago_secs=60, delivery_id=102)
        seed("C", "terminal", 3, last_attempt_ago_secs=86400, delivery_id=103)
        seed("D", "redeliverable", 3, window_hours=-1, last_attempt_ago_secs=86400, delivery_id=104)
        seed("E", "redeliverable", 3, resolved=True, last_attempt_ago_secs=86400, delivery_id=105)
        seed("F", "redeliverable", 2, last_attempt_ago_secs=86400, delivery_id=106)

        gen_a_before = row("A", "attempt_generation")
        exhausted_before = depth_key("exhausted")   # A,B,D (redeliverable/timeout, attempt>=3, not resolved)
        eligible_before = depth_key("eligible")      # F only (attempt<3, in window)

        res = admin("SELECT core.rearm_exhausted_github_delivery_recovery_with_authority(21600, 100)")
        rearmed = int(res.get("rearmed")) if isinstance(res, dict) else -1
        remaining = int(res.get("exhausted_remaining")) if isinstance(res, dict) else -1

        chk(rearmed == 1, f"exactly ONE eligible row re-armed (got rearmed={rearmed})")
        chk(remaining == 2, f"exhausted_remaining counts B+D after A cleared (got {remaining})")

        # (1) A re-armed
        chk(row("A", "attempt_count") == 0, "A: attempt_count reset to 0")
        chk(row("A", "attempt_generation") == gen_a_before + 1, "A: attempt_generation bumped (stale claim = no-op)")
        chk(row("A", "claimed_at") is None, "A: claim cleared")
        chk(bool(admin("SELECT next_attempt_at<=now() FROM core.github_delivery_recovery WHERE delivery_guid='A'")),
            "A: next_attempt_at is due (immediately claimable)")
        # (2)-(5) others untouched
        chk(row("B", "attempt_count") == 3, "B: fresh-attempt exhausted NOT re-armed (age gate)")
        chk(row("C", "attempt_count") == 3, "C: terminal-class NOT re-armed (class gate)")
        chk(row("D", "attempt_count") == 3, "D: out-of-window NOT re-armed (window gate)")
        chk(row("E", "attempt_count") == 3 and row("E", "resolved") is True, "E: resolved untouched")
        chk(row("F", "attempt_count") == 2, "F: not-yet-exhausted untouched")

        # (6) idempotent — nothing left eligible for re-arm this window
        res2 = admin("SELECT core.rearm_exhausted_github_delivery_recovery_with_authority(21600, 100)")
        chk(isinstance(res2, dict) and int(res2.get("rearmed")) == 0, "second immediate sweep re-arms nothing (idempotent)")

        # (7) depth alert breakdown moved correctly
        chk(exhausted_before == 3, f"depth.exhausted was 3 before (A,B,D) (got {exhausted_before})")
        chk(depth_key("exhausted") == 2, "depth.exhausted dropped to 2 (A left the exhausted set)")
        chk(eligible_before == 1 and depth_key("eligible") == 2, "depth.eligible rose (A now claim-eligible)")

        # (8) grant: the recovery loop runs as veripsa_app
        app = conn_for("veripsa_app"); app.autocommit = True
        try:
            with app.cursor() as cur:
                cur.execute("SELECT core.rearm_exhausted_github_delivery_recovery_with_authority(21600, 100)")
                chk(cur.fetchone() is not None, "veripsa_app may EXECUTE the re-arm (the loop's role)")
        finally:
            app.close()
        denied = False
        try:
            g = conn_for("veripsa_reader"); g.autocommit = True
            with g.cursor() as cur:
                cur.execute("SELECT core.rearm_exhausted_github_delivery_recovery_with_authority(21600, 100)")
            g.close()
        except psycopg2.Error:
            denied = True
        chk(denied, "the read-only role is DENIED EXECUTE (REVOKE FROM PUBLIC holds)")
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)

    ok = all(checks) and len(checks) >= 16
    print(("RECOVERY REARM EXHAUSTED GATE: PASS" if ok else "RECOVERY REARM EXHAUSTED GATE: FAIL")
          + f" ({sum(checks)}/{len(checks)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
