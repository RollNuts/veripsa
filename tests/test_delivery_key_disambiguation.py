#!/usr/bin/env python3
"""DELIVERY-KEY DISAMBIGUATION gate (small-findings root-fix sweep, finding 4).

THE FOOTGUN (root-fixed here): on the NO-X-GitHub-Delivery path (local/smee/dev, or a header-stripped request) the
durable-inbox key is a content hash. It used to hash the SANITIZED payload — but sanitize_payload DELIBERATELY DROPS
the distinguishing fields (commit messages, PR bodies, titles, the delivery id itself). So two SEMANTICALLY-DIFFERENT
deliveries could sanitize to byte-identical content → hash to the SAME 'local-…' key → the durable-inbox
enqueue's ON CONFLICT (delivery_key) then MERGED two distinct events onto ONE row = a silently-LOST delivery.

THE ROOT FIX: delivery_key now hashes the RAW (pre-sanitization) payload on the headerless fallback (it retains the
fields sanitization strips). Two raw-distinct deliveries get DISTINCT keys (→ two rows, never merged); a TRUE
redelivery (byte-identical raw payload) still hashes to the SAME key (→ one row, idempotent). A real GitHub delivery
always carries X-GitHub-Delivery and keys by that; the recovery loop re-submits with the assigned key — both stay on
the delivery-id branch and remain idempotent regardless.

PROVES (pure-logic + a REAL DB round-trip through DeliveryStore.submit + the enqueue ON CONFLICT):
  (1) two distinct PR events differing ONLY in title/body (dropped by sanitize → identical sanitized content) hashed
      to the SAME key under the OLD (sanitized) scheme — the collision is real.
  (2) under the FIX (raw hash) they get DISTINCT keys → DB enqueue creates TWO rows (no merge / no lost delivery).
  (3) a TRUE redelivery (identical raw payload, no header) → the SAME key → ONE row (still idempotent).
  (4) a header-bearing delivery keys by the X-GitHub-Delivery id (unchanged), and a 503-redeliver is one row.

PROCESS-UNIQUE scratch DB (parallel-safe). Run:  python3 tests/test_delivery_key_disambiguation.py
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import delivery_queue as dq  # noqa: E402

DB = "veripsa_delivkey_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def row_count(key):
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT count(*) FROM core.webhook_delivery WHERE delivery_key=%s", (key,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def total_rows():
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT count(*) FROM core.webhook_delivery")
            return cur.fetchone()[0]
    finally:
        conn.close()


# Two DISTINCT pull_request events: SAME number/base/head/user/merged/draft, DIFFERENT title + body (both dropped by
# sanitize unless the title starts with 'Revert '). The sanitizer collapses them to identical content.
def _pr(title, body):
    base = {"ref": "main", "sha": "c" * 40, "repo": {"id": 1}}
    head = {"ref": "feat", "sha": "d" * 40, "repo": {"id": 1}}
    return {"action": "opened", "number": 7, "installation": {"id": 99},
            "repository": {"full_name": "acme/web", "owner": {"id": 4242}},
            "pull_request": {"number": 7, "base": base, "head": head, "user": {"id": 9, "login": "u"},
                             "merged": False, "draft": False, "title": title, "body": body}}


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    print("DELIVERY-KEY DISAMBIGUATION (small-findings root fix, finding 4)")

    raw_a = _pr("Add login", "this implements login")
    raw_b = _pr("Refactor parser", "a completely different change")
    san_a = dq.sanitize_payload("pull_request", raw_a)
    san_b = dq.sanitize_payload("pull_request", raw_b)

    # ── (1) the COLLISION is real: sanitize collapses the two distinct events to identical content → the OLD
    #        (sanitized-hash) key would be identical for both = the merge bug.
    chk(san_a == san_b, "(1) sanitize_payload COLLAPSES the two distinct PR events to identical content (the distinguishing fields are dropped)")
    old_a = dq.delivery_key("pull_request", "", san_a)                       # OLD scheme: hash the sanitized payload
    old_b = dq.delivery_key("pull_request", "", san_b)
    chk(old_a == old_b, "(1) the OLD sanitized-hash key is IDENTICAL for both distinct events (would MERGE them onto one row)")

    # ── (2) the FIX: raw-hash gives DISTINCT keys → two DB rows (no merge / no lost delivery).
    new_a = dq.delivery_key("pull_request", "", san_a, raw_payload=raw_a)
    new_b = dq.delivery_key("pull_request", "", san_b, raw_payload=raw_b)
    chk(new_a != new_b, "(2) the FIX (raw-hash) gives DISTINCT keys for the two distinct events")

    store = dq.DeliveryStore(DSN_APP)
    res_a = store.submit("pull_request", raw_a, None, account_key="4242", repo="acme/web")   # headerless (delivery=None)
    res_b = store.submit("pull_request", raw_b, None, account_key="4242", repo="acme/web")
    chk(res_a.get("accepted") and res_b.get("accepted"), "(2) both headerless submits were accepted by the durable store")
    chk(res_a["key"] != res_b["key"], "(2) the store assigned DISTINCT keys to the two distinct headerless deliveries")
    chk(row_count(res_a["key"]) == 1 and row_count(res_b["key"]) == 1 and total_rows() == 2,
        "(2) TWO distinct webhook_delivery rows exist (the ON CONFLICT no longer merges two semantically-different deliveries)")

    # ── (3) a TRUE redelivery (identical RAW payload, still headerless) → the SAME key → ONE row (idempotent).
    res_a2 = store.submit("pull_request", raw_a, None, account_key="4242", repo="acme/web")
    chk(res_a2["key"] == res_a["key"], "(3) a true redelivery (identical raw payload) maps to the SAME key (idempotent)")
    chk(row_count(res_a["key"]) == 1 and total_rows() == 2,
        "(3) the true redelivery did NOT create a second row (still exactly one row for it)")

    # ── (4) a HEADER-bearing delivery keys by the X-GitHub-Delivery id (unchanged) and a 503-redeliver is one row.
    res_h = store.submit("push", {"ref": "refs/heads/main", "after": "e" * 40,
                                  "repository": {"full_name": "acme/web", "owner": {"id": 4242}},
                                  "installation": {"id": 99}}, "GH-DELIV-XYZ", account_key="4242", repo="acme/web")
    chk(res_h["key"] == "GH-DELIV-XYZ", "(4) a header-bearing delivery keys by the X-GitHub-Delivery id")
    res_h2 = store.submit("push", {"ref": "refs/heads/main", "after": "e" * 40,
                                   "repository": {"full_name": "acme/web", "owner": {"id": 4242}},
                                   "installation": {"id": 99}}, "GH-DELIV-XYZ", account_key="4242", repo="acme/web")
    chk(res_h2["key"] == "GH-DELIV-XYZ" and row_count("GH-DELIV-XYZ") == 1,
        "(4) a 503-and-redeliver of the same X-GitHub-Delivery id is idempotent (one row)")

    print()
    if all(checks):
        print("DELIVERY-KEY DISAMBIGUATION GATE: PASS")
        return 0
    print(f"DELIVERY-KEY DISAMBIGUATION GATE: FAIL ({sum(1 for c in checks if not c)} of {len(checks)} failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
