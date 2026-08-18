#!/usr/bin/env python3
"""UNINSTALL ACCOUNT-WIDE PURGE gate (audit r4 — privacy launch-blocker fix).

THE BUG (RED on origin/main): the `installation.deleted` uninstall purge was 100% payload-driven — it forgot
only the repos NAMED in the webhook's `repositories` array. GitHub OMITS that array for an "All repositories"
install (the common case), so an uninstall could leave a whole tenant's code graph + live claims sitting in
our DB = a broken "we immediately purge on uninstall" privacy claim.

THE FIX (two parts, both asserted here):
  (A) core.purge_account_working_set_with_authority() — forgets the pinned tenant's ENTIRE content-free working
      set (code_node/code_edge/graph_version/claim) across EVERY repo, no repo filter; the append-only event
      ledger is RETAINED by design.
  (B) event_processor no longer fans `installation.deleted` out per-repo, so the uninstall falls through to the
      handler account-pinned and calls (A) — independent of whatever the payload did/didn't name.

PROCESS-UNIQUE scratch DB (parallel-safe). Run:  python3 tests/test_uninstall_account_purge.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
from _lifecycle_fixture import absent_installation_proof, seed_processing_uninstall  # noqa: E402

DB = "veripsa_uninstpurge_" + str(os.getpid())
checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def app(sql, args=()):
    """One connection that has ENTERED installation 555 → pinned to its tenant account for the whole event."""
    if not hasattr(app, "conn"):
        app.conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
        app.conn.autocommit = True
        with app.conn.cursor() as c:
            c.execute("SET search_path=core")
            c.execute("SELECT core.enter_installation_with_authority(%s)", ("555",))
    with app.conn.cursor() as c:
        c.execute(sql, args)
        row = c.fetchone()
        return row[0] if row else None


def count(table):
    """Owner read of a per-account table, pinned to the tenant (FORCE RLS hides rows otherwise)."""
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as c:
            c.execute("SET search_path=core")
            c.execute("SELECT set_config('core.current_account', 'ACCT-GH-555', false)")
            c.execute(f"SELECT count(*) FROM core.{table}")
            return c.fetchone()[0]
    finally:
        conn.close()


def main():
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    import code_graph_extract as X  # noqa
    import tempfile

    # (0) ROUTING (part B): the uninstall is NOT fanned out per-repo anymore → it reaches the account-pinned
    # handler regardless of the payload's `repositories`.
    import event_processor as EP
    fan = EP._install_fanout_repos("installation", {"action": "deleted", "repositories": [{"full_name": "a/b"}]})
    chk(fan == [], f"event_processor no longer fans installation.deleted out per-repo (got {fan})")
    # control: a partial repo-removal STILL fans out per-repo (account survives, one repo named authoritatively)
    fan2 = EP._install_fanout_repos("installation_repositories", {"action": "removed", "repositories_removed": [{"full_name": "a/b"}]})
    chk(fan2 == ["a/b"], f"installation_repositories.removed still fans out per-repo (got {fan2})")
    duplicate_same = EP._install_fanout_repos(
        "installation_repositories",
        {"action": "removed", "repositories_removed": [
            {"id": 101, "full_name": "a/b"},
            {"id": "101", "full_name": "a/b"},
        ]},
    )
    chk(duplicate_same == ["a/b"],
        f"an exact duplicate repository object is processed once (got {duplicate_same})")
    conflicting_duplicate_rejected = False
    try:
        EP._install_fanout_repos(
            "installation_repositories",
            {"action": "removed", "repositories_removed": [
                {"id": 101, "full_name": "a/b"},
                {"id": 202, "full_name": "a/b"},
            ]},
        )
    except ValueError as exc:
        conflicting_duplicate_rejected = "conflicting duplicate repository identity" in str(exc)
    chk(conflicting_duplicate_rejected,
        "one full_name carrying conflicting stable ids is rejected before fan-out mutation")

    # seed a tenant with a non-empty working set across TWO repos + live claims + an event-ledger row.
    def ingest(repo, files):
        with tempfile.TemporaryDirectory() as d:
            for rel, body in files.items():
                p = os.path.join(d, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w").write(body)
            g = X.build_graph(d)
        app("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(g), repo, "main", "a" * 40))

    ingest("acme/web", {"app/a.py": "def fa():\n    return 1\n"})
    ingest("acme/api", {"svc/b.py": "def fb():\n    return 2\n"})
    app("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-1:app/a.py", "app/a.py", "acme/web", "main", "alice"))
    app("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-2:svc/b.py", "svc/b.py", "acme/api", "main", "bob"))
    app("SELECT core.record_landing_with_authority(%s,%s,%s,%s,%s)", ("acme/web", "main", "b" * 40, ["app/a.py"], "alice"))

    before = {t: count(t) for t in ("code_node", "code_edge", "graph_version", "claim", "event")}
    chk(before["code_node"] > 0 and before["claim"] >= 2,
        f"seeded a non-empty working set across 2 repos (nodes={before['code_node']}, claims={before['claim']})")
    events_before = before["event"]

    # (A) ACCOUNT-WIDE purge — what the uninstall handler now calls (NO repo argument, no payload dependency).
    from ingest import purge_account_working_set
    uninstall_key = "account-purge-555"
    deleted_installation_id = "A-555"
    seed_processing_uninstall(
        f"postgresql://veripsa_migrator@localhost/{DB}",
        uninstall_key,
        "555",
        deleted_installation_id,
    )
    res = purge_account_working_set(
        app,
        absent_installation_proof("555", deleted_installation_id),
        uninstall_key,
    )
    chk(isinstance(res, dict) and res.get("ok") and res.get("account_wide"),
        f"purge_account_working_set returns ok+account_wide (got {res})")

    after = {t: count(t) for t in ("code_node", "code_edge", "graph_version", "claim", "event")}
    chk(after["code_node"] == 0 and after["code_edge"] == 0 and after["graph_version"] == 0 and after["claim"] == 0,
        f"the ENTIRE tenant working set is purged account-wide — across BOTH repos (after={ {k: after[k] for k in ('code_node','code_edge','graph_version','claim')} })")
    chk(after["event"] == events_before and events_before > 0,
        f"the append-only event ledger is RETAINED (content-free audit trail kept: {after['event']} == {events_before})")

    ok = all(checks)
    print("UNINSTALL ACCOUNT-WIDE PURGE GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
