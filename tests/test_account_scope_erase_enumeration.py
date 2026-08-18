#!/usr/bin/env python3
"""ACCOUNT-SCOPE ERASE/PURGE ENUMERATION gate (audit iter-5 P1) — a STRUCTURAL invariant tying the per-account
table set to the coverage of the two lifecycle-forget functions, so the next new table CANNOT silently reopen the
GDPR-erase / uninstall-purge gap (it already fired once, for core.webhook_delivery).

THE DEFECT. core.erase_account_with_authority() (GDPR Art.17 / CCPA right-to-deletion HARD DELETE) and
core.purge_account_working_set_with_authority() (uninstall working-set forget) are HAND-MAINTAINED DELETE lists.
They are COMPLETE today, but nothing tied the set of per-account tables to that coverage — so when a new
per-account table shipped (core.webhook_delivery), the erase silently forgot it and a tenant's repo full_names +
GitHub account id OUTLIVED the "delete ALL my data". The inbox-hardening added a narrow guard over `account_key`
columns only, against the erase body only. THIS gate GENERALIZES it: it introspects pg_attribute for EVERY
core.* TABLE carrying ANY account-scoping column (account_id / account_key / grantor_account / followed_account)
and asserts coverage of BOTH functions via pg_get_functiondef — failing CI the instant a new such table is added
without being wired into the lifecycle-forget path.

THE TWO CONTRACTS (they are deliberately DIFFERENT — this gate encodes the difference, not a false symmetry):
  • ERASE = GDPR COMPLETENESS. The hard delete MUST reference EVERY account-scoping table — no exceptions. A new
    per-account table the erase forgets is a privacy/compliance hole. Any unreferenced table is a hard FAIL.
  • PURGE = WORKING-SET forget. The uninstall purge deliberately forgets only the content-free WORKING SET and
    RETAINS the identity rows (account stays; agent/credential), the append-only audit ledger (event/statement),
    the cross-account ROUTING map (installation_account), and the delegation/social graphs (grant/follow) +
    policy/store_connection — an uninstall is NOT a hard delete (that is erase). So the purge must reference every
    account-scoping table EXCEPT an EXPLICIT, documented RETAIN allowlist below. A NEW table is NOT on the
    allowlist by default → it must be wired into the purge OR consciously added to the retain set WITH a reason.
    Either way it cannot silently slip the working-set forget.

So both functions are pinned: erase to the FULL set, purge to (full set − the documented retain allowlist). The
next new per-account table reopens NEITHER gap silently — it lands red here until it is consciously handled.

This is a pure STRUCTURAL gate (stands up the ephemeral schema, introspects the catalog + the two function
bodies — never greps source). Content-free.

Run:  python3 tests/test_account_scope_erase_enumeration.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

DB = "veripsa_scopeenum_" + str(os.getpid())

# the account-SCOPING columns: a row carrying any of these is owned by / points at a specific tenant account.
SCOPE_COLS = ("account_id", "account_key", "grantor_account", "followed_account")

# ── PURGE RETAIN ALLOWLIST ──────────────────────────────────────────────────────────────────────────────────
# Tables the UNINSTALL working-set purge deliberately does NOT delete (an uninstall is not a hard delete). Each
# entry is a CONSCIOUS retain with a reason; a NEW per-account table is NOT here by default, so it must be wired
# into the purge OR added here on purpose. (The ERASE — the GDPR hard delete — still takes ALL of these; this
# allowlist is ONLY about the lighter uninstall purge.)
PURGE_RETAINS = {
    # NOTE: core.account is NOT here — the purge DOES reference it (it resets plan->'free' + writes the
    # resurrection tombstone), so it is covered by the working-set check below. The allowlist holds ONLY tables
    # the purge does not touch at all (the dead-entry hygiene check keeps it that way).
    "agent":                 "tenant identity rows survive an uninstall (a reinstall re-binds the same account); erase deletes them",
    "credential":            "the connection-role→identity map is retained across an uninstall/reinstall; erase clears it",
    "installation_account":  "the cross-account ROUTING map is retained so a reinstall re-binds cleanly; erase clears it",
    "event":                 "the append-only push/landed AUDIT ledger is RETAINED by design on uninstall (records-not-correctness); erase token-deletes it",
    "statement":             "the append-only statement records ledger is RETAINED on uninstall (immutable audit); erase token-deletes it",
    "grant":                 "the delegation graph is retained on uninstall (a reinstall keeps prior grants); erase deletes both directions",
    "follow":                "the social follow graph is retained on uninstall; erase deletes both sides via the erasure-token policy",
    "policy":                "per-account policy rows are retained on uninstall (config survives a reinstall); erase deletes them",
    "store_connection":      "store-connection rows are retained on uninstall; erase deletes them",
}

checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            # EVERY core base TABLE carrying ANY account-scoping column (introspect the live catalog, not source).
            cur.execute(
                """SELECT DISTINCT c.relname
                   FROM pg_attribute a
                   JOIN pg_class c ON c.oid = a.attrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname='core' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
                     AND a.attname = ANY(%s)
                   ORDER BY c.relname""",
                (list(SCOPE_COLS),),
            )
            scoped = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT pg_get_functiondef('core.erase_account_with_authority()'::regprocedure)")
            erase_body = cur.fetchone()[0]
            cur.execute(
                "SELECT pg_get_functiondef("
                "'core.purge_account_working_set_with_authority(jsonb)'::regprocedure)")
            purge_body = cur.fetchone()[0]
    finally:
        conn.close()

    # sanity: the introspection found a real per-account table set (a parser/path regression would zero it).
    chk(len(scoped) >= 10,
        f"introspection: found {len(scoped)} core tables carrying an account-scoping column "
        f"({', '.join(SCOPE_COLS)}) — {scoped}")

    def referenced(body, t):
        # the functions always qualify a table as core.<name> (pinned search_path); match that exact token.
        return f"core.{t}" in body

    def deletes_rows(body, t):
        # the WORKING-SET FORGET is specifically a DELETE of the table's rows. A table the purge merely TOUCHES
        # without deleting (e.g. it stamps installation_account.revoked_at = the liveness mark, or resets
        # core.account.plan) is RETAINED, not forgotten — so the dead-allowlist-entry hygiene below must key on a
        # real row DELETE, not any reference, or a retained-but-touched table looks falsely "already forgotten".
        return f"DELETE FROM core.{t} " in body or f"DELETE FROM core.{t}\n" in body or f"DELETE FROM core.{t}(" in body

    # ── (1) ERASE = GDPR COMPLETENESS: every account-scoping table MUST be referenced by the hard delete. ──────
    erase_missing = [t for t in scoped if not referenced(erase_body, t)]
    chk(not erase_missing,
        f"ERASE completeness: EVERY account-scoping core table is referenced by erase_account_with_authority "
        f"(the GDPR/CCPA hard delete) — a new per-account table cannot silently escape the 'delete ALL my data' "
        f"receipt. missing={erase_missing}")

    # ── (2) PURGE = WORKING-SET forget: every account-scoping table MUST be referenced by the uninstall purge,
    #    EXCEPT the explicit documented RETAIN allowlist. A new table not wired + not allowlisted is a FAIL. ─────
    purge_expected = [t for t in scoped if t not in PURGE_RETAINS]
    purge_missing = [t for t in purge_expected if not referenced(purge_body, t)]
    chk(not purge_missing,
        f"PURGE working-set forget: every account-scoping core table OUTSIDE the documented retain allowlist is "
        f"referenced by purge_account_working_set_with_authority — a new per-account table cannot silently escape "
        f"the uninstall working-set forget. missing={purge_missing}")

    # ── (3) RETAIN ALLOWLIST HYGIENE: every allowlisted table actually EXISTS + carries a scope column (so the
    #    allowlist can't drift into a typo that secretly excuses a real working-set table), and it really is NOT
    #    referenced by the purge (a stale allowlist entry that IS purged is dead config — flag it so it's pruned).
    stale_allow = [t for t in PURGE_RETAINS if t not in scoped]
    chk(not stale_allow,
        f"RETAIN-ALLOWLIST hygiene: every retain-allowlisted table exists + carries a scope column (no typo'd "
        f"entry secretly excusing a real working-set table). stale/unknown entries={stale_allow}")
    # a "dead" allowlist entry = one the purge actually DELETES (forgets) anyway. A table the purge merely TOUCHES
    # without deleting (installation_account gets its liveness revoked_at stamped, but the routing ROW is RETAINED so
    # a reinstall re-binds the same id) is correctly retained + correctly allowlisted — it is NOT dead. So key this
    # on a real row DELETE, not any reference.
    purged_but_allowlisted = [t for t in PURGE_RETAINS if t in scoped and deletes_rows(purge_body, t)]
    chk(not purged_but_allowlisted,
        f"RETAIN-ALLOWLIST hygiene: no allowlisted table is ALSO row-DELETED by the purge (a dead allowlist entry — "
        f"the purge already forgets it, so it should be removed from the retain set). dead entries={purged_but_allowlisted}")

    # ── REPORT the full coverage matrix (so a reviewer sees exactly what each function touches). ───────────────
    print("\n  ── coverage matrix (table : erase / purge ; R=retained-by-design on uninstall) ──")
    for t in scoped:
        e = "Y" if referenced(erase_body, t) else "—"
        if t in PURGE_RETAINS:
            p = "R"
        else:
            p = "Y" if referenced(purge_body, t) else "—"
        print(f"     {t:<28} erase={e}  purge={p}")

    ok = all(checks)
    print("\nACCOUNT-SCOPE ERASE/PURGE ENUMERATION GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
