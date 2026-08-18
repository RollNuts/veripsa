#!/usr/bin/env python3
"""RENAME-COMPLETENESS ENUMERATION gate (integration-audit P1, root-of-root) — a STRUCTURAL invariant tying the
set of repo-COORDINATE-keyed tables to the coverage of the rename mover, so the next new repo-keyed table CANNOT
silently reopen the rename-ORPHAN gap. It already fired TWICE: first for core.co_change/co_change_seen_commit (the
graph #436 fix), then for core.workspace_member (the CONSENT layer — a rename orphaned the bilateral cross-tenant
link + stranded a stale 'accepted' row under the dead coordinate) and the repo-scoped core.grant. Modules created
later in bootstrap may participate through a verified late-bound delegate; the gate follows that executed call
edge and audits its body too.

THE DEFECT. core._migrate_repo_coordinate(account, old_full, new_full) is the FULL old→new coordinate move on a
repo/owner RENAME (re-points / dedups every repo-keyed table). It is a HAND-MAINTAINED list of UPDATE … SET repo.
Nothing tied the set of tables carrying a `repo` column to that coverage — so when a new repo-keyed table shipped
(workspace_member, the consent substrate), the mover silently skipped it and a rename ORPHANED the row: the
coordinate went dark under the dead name, never to self-heal (the same orphan CLASS the graph fix #436 closed,
now in the moat-critical consent layer). THIS gate GENERALIZES the fix: it introspects pg_attribute for EVERY
core.* TABLE carrying a `repo` column plus the explicit semantic repository fields below, and asserts the rename
mover or an actually invoked late-bound delegate references each (via pg_get_functiondef), modulo an EXPLICIT,
documented RETAIN allowlist — failing CI the instant a new repo-keyed table is added without
being wired into the rename move (or consciously retained with a reason).

THE CONTRACT (mirrors gate 173's erase/purge enumeration — a catalog-introspecting structural invariant):
  • The rename mover MUST re-point EVERY repo-coordinate-keyed table — a renamed repo's WHOLE working set + live
    state follows old→new — EXCEPT a documented RETAIN allowlist. A table the mover forgets ORPHANS on a rename.
  • RETAIN allowlist (the rows that deliberately DO NOT move on a rename, each a CONSCIOUS retain with a reason):
      - event            : the APPEND-ONLY push/landed AUDIT ledger. Those landings happened under the OLD name and
                           that audit fact is permanent + correct — history is honest about the name AT THE TIME.
                           Rewriting it would falsify the record. (Same rationale the rename handler documents.)
      - webhook_delivery : the durable webhook INBOX is a transient, AGE-PRUNED delivery buffer (reaped by the
                           watchdog); a coordinate rename does not need to rewrite in-flight/already-processed
                           delivery rows — they age out. Not part of the durable working set a rename re-keys.
      - repository_lifecycle_tombstone: a removed/deleted repository is blocked by its stable repository id,
                           independent of a mutable full_name. A live rename is never tombstoned; a stale rename
                           for a tombstoned id is rejected before the coordinate mover. Rewriting the marker is
                           unnecessary and would weaken the historical deletion coordinate.
    A NEW repo-keyed table is NOT on the allowlist by default → it must be wired into the mover OR consciously
    added here WITH a reason. Either way it cannot silently slip the rename and orphan on the next rename.

This is a pure STRUCTURAL gate (stands up the ephemeral schema, introspects the catalog + the mover's body — it
never greps source). Content-free.

Run:  python3 tests/test_rename_completeness_enumeration.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

DB = "veripsa_renameenum_" + str(os.getpid())

# the rename mover's exact signature (it is overloaded-safe to pin it — pg_get_functiondef on a bare name with
# multiple candidates would be ambiguous). This is the FULL old→new coordinate move.
MIGRATE_FN = "core._migrate_repo_coordinate(text,text,text)"

# Schema 35 owns the generic coordinate mover, while schema 97 introduces the graph convergence tables later in
# the bootstrap/rolling order. PostgreSQL therefore cannot bind those relations into the 35 function directly.
# The mover calls this internal function late-bound instead. The coverage gate follows that *executed* dependency
# (resolver guard + exact dynamic SELECT), rather than weakening the table inventory with an allowlist.
LATE_BOUND_MIGRATE_DELEGATES = {
    "core._rename_graph_refresh_coordinate(text,text,text)":
        "schema 97 graph scheduler rename delegate (lease fencing + stable-id desired-row migration)",
}

# ── RENAME RETAIN ALLOWLIST ─────────────────────────────────────────────────────────────────────────────────
# Tables carrying a `repo` column that the rename mover deliberately does NOT re-point (each a CONSCIOUS retain
# with a reason). A NEW repo-keyed table is NOT here by default — so it must be wired into the mover OR added here
# on purpose. (See the module docstring for the full rationale.)
RENAME_RETAINS = {
    "event":            "append-only AUDIT ledger — landings happened under the OLD name; history is honest about "
                        "the name at the time (rewriting it would falsify the permanent record)",
    "webhook_delivery": "transient AGE-PRUNED webhook inbox buffer (watchdog-reaped) — not part of the durable "
                        "working set a rename re-keys; in-flight/processed delivery rows age out",
    "repository_lifecycle_tombstone": "stable-id revocation marker — live repos have no marker, and a stale "
                                      "rename for a revoked id is blocked before coordinate migration",
}

# Repository coordinates do not always use a column literally named `repo`. Keep the small semantic registry
# explicit so those live-authority fields cannot evade catalog enumeration. `store_connection.target` is a GitHub
# full_name when provider='github'; non-GitHub targets are excluded by the mover itself.
SEMANTIC_REPO_FIELDS = {
    "store_connection": "target",
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
    rolling_guard_sqlstate = None
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            # EVERY core base TABLE carrying a `repo` column (introspect the live catalog, never source).
            cur.execute(
                """SELECT DISTINCT c.relname
                   FROM pg_attribute a
                   JOIN pg_class c ON c.oid = a.attrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname='core' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
                     AND a.attname = 'repo'
                   ORDER BY c.relname""")
            repo_keyed = [row[0] for row in cur.fetchall()]
            missing_semantic = []
            for table, column in SEMANTIC_REPO_FIELDS.items():
                cur.execute(
                    """SELECT EXISTS (
                         SELECT 1 FROM pg_attribute a
                         JOIN pg_class c ON c.oid=a.attrelid
                         JOIN pg_namespace n ON n.oid=c.relnamespace
                          WHERE n.nspname='core' AND c.relkind='r' AND c.relname=%s
                            AND a.attnum>0 AND NOT a.attisdropped AND a.attname=%s)""",
                    (table, column),
                )
                if cur.fetchone()[0]:
                    repo_keyed.append(table)
                else:
                    missing_semantic.append(f"{table}.{column}")
            repo_keyed = sorted(set(repo_keyed))
            cur.execute("SELECT pg_get_functiondef(%s::regprocedure)", (MIGRATE_FN,))
            migrate_body = cur.fetchone()[0]
            delegate_bodies = {}
            unresolved_delegates = []
            uninvoked_delegates = []
            for signature in LATE_BOUND_MIGRATE_DELEGATES:
                function_name = signature.split("(", 1)[0]
                argument_count = signature.count("text")
                placeholders = ",".join(f"${n}" for n in range(1, argument_count + 1))
                guarded = f"to_regprocedure('{signature}')" in migrate_body
                invoked = re.search(
                    rf"\bSELECT\s+{re.escape(function_name)}"
                    rf"\(\s*{re.escape(placeholders)}\s*\)",
                    migrate_body,
                    flags=re.IGNORECASE,
                ) is not None
                if not (guarded and invoked):
                    uninvoked_delegates.append(signature)
                    continue
                cur.execute("SELECT to_regprocedure(%s)", (signature,))
                if cur.fetchone()[0] is None:
                    unresolved_delegates.append(signature)
                    continue
                cur.execute("SELECT pg_get_functiondef(%s::regprocedure)", (signature,))
                delegate_bodies[signature] = cur.fetchone()[0]

        # Emulate the only hazardous rolling window: schema 97 has published its graph lease relation, but its
        # late-bound rename function is not yet callable. DROP and the deliberately failing rename share one
        # transaction, so the exception rolls the DROP back and leaves the ephemeral schema intact. A silent
        # success here would prove that a rename can commit split coordinates during deployment.
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DROP FUNCTION core._rename_graph_refresh_coordinate(text,text,text)")
                cur.execute(
                    "SELECT set_config('core.current_account',%s,true)",
                    ("ACCT-RENAME-ROLLING-GUARD",),
                )
                cur.execute(
                    "SELECT core._migrate_repo_coordinate(%s,%s,%s)",
                    ("ACCT-RENAME-ROLLING-GUARD", "rolling/old", "rolling/new"),
                )
        except psycopg2.Error as exc:
            rolling_guard_sqlstate = exc.pgcode
            conn.rollback()  # also restores the hook dropped above
        else:
            conn.rollback()
    finally:
        conn.close()

    # sanity: the introspection found a real repo-keyed table set (a parser/path regression would zero it).
    chk(len(repo_keyed) >= 6,
        f"introspection: found {len(repo_keyed)} core tables carrying a `repo` column — {repo_keyed}")
    chk(not missing_semantic,
        f"semantic repository-field registry resolves every documented field — missing={missing_semantic}")
    chk(not uninvoked_delegates and not unresolved_delegates,
        "late-bound rename delegates are both guarded and actually invoked by the schema-35 mover, and resolve "
        f"after the schema-97 cutover — uninvoked={uninvoked_delegates}, unresolved={unresolved_delegates}")
    chk(
        "to_regclass('core.graph_convergence_lease')" in migrate_body
        and "graph convergence rename hook unavailable; retry after schema cutover" in migrate_body
        and rolling_guard_sqlstate == "55000",
        "rolling apply is fail-atomic: pre-97 has no graph table and remains compatible, while a published graph "
        "table without its late-bound rename hook aborts instead of committing split coordinates "
        f"(sqlstate={rolling_guard_sqlstate})")

    coverage_bodies = {MIGRATE_FN: migrate_body, **delegate_bodies}

    def references(t):
        # The mover and its verified late-bound delegates always qualify a table as core.<name> (pinned
        # search_path); require a boundary after the name so core.co_change does NOT spuriously satisfy
        # core.co_change_seen_commit (substring).
        return [
            signature for signature, body in coverage_bodies.items()
            if any(f"core.{t}{boundary}" in body for boundary in (" ", ".", "\n", "\t"))
        ]

    # ── (1) RENAME COMPLETENESS: every repo-keyed table OUTSIDE the documented retain allowlist MUST be referenced
    #    by the mover. A new repo-keyed table not wired + not allowlisted is a FAIL (it would orphan on a rename). ─
    expected = [t for t in repo_keyed if t not in RENAME_RETAINS]
    missing = [t for t in expected if not references(t)]
    chk(not missing,
        f"RENAME completeness: EVERY repo-coordinate-keyed core table OUTSIDE the documented retain allowlist is "
        f"referenced by _migrate_repo_coordinate or one of its verified, executed late-bound delegates — a new "
        f"repo-keyed table cannot silently escape the old→new coordinate move and ORPHAN on a rename. "
        f"missing={missing}")

    # ── (2) RETAIN ALLOWLIST HYGIENE: every allowlisted table actually EXISTS + carries a `repo` column (so the
    #    allowlist can't drift into a typo that secretly excuses a real repo-keyed table), and it really is NOT
    #    referenced by the mover (a stale allowlist entry the mover DOES move is dead config — flag it to prune). ──
    stale_allow = [t for t in RENAME_RETAINS if t not in repo_keyed]
    chk(not stale_allow,
        f"RETAIN-ALLOWLIST hygiene: every retain-allowlisted table exists + carries a `repo` column (no typo'd "
        f"entry secretly excusing a real repo-keyed table). stale/unknown entries={stale_allow}")
    moved_but_allowlisted = [t for t in RENAME_RETAINS if t in repo_keyed and references(t)]
    chk(not moved_but_allowlisted,
        f"RETAIN-ALLOWLIST hygiene: no allowlisted table is ALSO moved by the rename mover (a dead allowlist "
        f"entry — the mover already re-points it, so it should leave the retain set). dead entries={moved_but_allowlisted}")

    # ── REPORT the full coverage matrix (so a reviewer sees exactly what the rename mover touches). ──────────────
    print("\n  ── rename-coverage matrix (table : moved on rename ; R=retained-by-design) ──")
    for t in repo_keyed:
        owners = references(t)
        moved = "R" if t in RENAME_RETAINS else ("Y" if owners else "—")
        via = "" if not owners else f" via {', '.join(owners)}"
        print(f"     {t:<28} rename={moved}{via}")

    ok = all(checks)
    print("\nRENAME-COMPLETENESS ENUMERATION GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
