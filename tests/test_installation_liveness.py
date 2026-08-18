#!/usr/bin/env python3
"""INSTALLATION LIVENESS gate (commercial-completeness — the no-billing-without-a-LIVE-link invariant).

THE GAP this closes: core.installation_account is the installation->account routing MAP, but it carried NO
liveness state. The uninstall purge (purge_account_working_set_with_authority) and the suspend release
(release_account_claims_with_authority) deliberately KEEP the routing row (the uninstall keeps it because the
append-only ledger is retained + a reinstall re-binds the SAME id; suspend is reversible). Only a GDPR erase
deletes the row. So the platform's liveness read seam -- list_installation_ids() (a bare SELECT) -- reported an
UNINSTALLED/SUSPENDED installation as if it were still LIVE. That is the exact read the no-billing-without-a-
live-link gates depend on, so a dead install could look billable, and graph freshness would keep it warm.

THE FIX (asserted here):
  (A) core.installation_account.revoked_at (nullable) — STAMPED by the uninstall + suspend handlers, CLEARED by
      reactivate. "revoked_at IS NULL" == live.
  (B) core.installation_is_live(installation_id) — the platform's per-installation liveness read (true iff
      mapped AND not revoked; an unknown id is NOT live).
  (C) core.list_installation_ids() — now enumerates ONLY live (revoked_at IS NULL) installs.

Proven end-to-end: a fresh install is live + enumerated -> uninstall makes it NOT live + drops it from the list
WHILE KEEPING the routing row (so a reinstall re-binds the same id + the audit ledger survives) -> reactivate
brings it back live. Suspend revokes liveness; unsuspend (reactivate) restores it. An unknown id is never live.

PROCESS-UNIQUE scratch DB (parallel-safe). Run:  python3 tests/test_installation_liveness.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
from _lifecycle_fixture import (  # noqa: E402
    absent_installation_proof,
    current_suspend_proof,
    seed_processing_installation_event,
    seed_processing_uninstall,
)

DB = "veripsa_instlive_" + str(os.getpid())
INST = "777"
ACCT = "ACCT-GH-" + INST
ADMIN = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def app(sql, args=()):
    """One App connection that has ENTERED installation 777 -> pinned to its tenant account for the event."""
    if not hasattr(app, "conn"):
        app.conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
        app.conn.autocommit = True
        with app.conn.cursor() as c:
            c.execute("SET search_path=core")
            c.execute("SELECT core.enter_installation_with_authority(%s)", (INST,))
    with app.conn.cursor() as c:
        c.execute(sql, args)
        row = c.fetchone()
        return row[0] if row else None


def reader(sql, args=()):
    """The SEPARATE platform reader role — the connection the liveness gates actually use."""
    conn = psycopg2.connect(f"postgresql://example_platform_reader@localhost/{DB}")
    try:
        conn.autocommit = True
        with conn.cursor() as c:
            c.execute("SET search_path=core")
            c.execute(sql, args)
            row = c.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def list_ids():
    """The live-only enumerator, read as the platform reader (returns a python list of ids)."""
    conn = psycopg2.connect(f"postgresql://example_platform_reader@localhost/{DB}")
    try:
        conn.autocommit = True
        with conn.cursor() as c:
            c.execute("SET search_path=core")
            c.execute("SELECT core.list_installation_ids()")
            return [r[0] for r in c.fetchall()]
    finally:
        conn.close()


def row_present():
    """Is the routing ROW still present at all (regardless of liveness)? Migrator read of the routing table."""
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as c:
            c.execute("SET search_path=core")
            c.execute("SELECT count(*) FROM core.installation_account WHERE installation_id=%s", (INST,))
            return c.fetchone()[0]
    finally:
        conn.close()


def reactivate_from_delivery(key, action):
    """Model the durable installation event that production resolves before changing lifecycle generation."""
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as c:
            c.execute("SET search_path=core")
            c.execute(
                "INSERT INTO core.webhook_delivery(delivery_key,event_type,account_key,payload,status,received_at) "
                "VALUES (%s,'installation',%s,%s::jsonb,'processing',clock_timestamp())",
                (key, INST, json.dumps({
                    "action": action,
                    "installation": {"id": "B-777", "account": {"id": INST}},
                })),
            )
    finally:
        conn.close()
    proof = json.dumps({"installation_id": "B-777", "account_id": INST,
                        "created_at": "2099-01-01T00:00:00Z", "suspended": False})
    return app("SELECT core.reactivate_account_with_authority(%s,%s::jsonb)", (key, proof))


def ingest(repo):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "app", "a.py")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("def fa():\n    return 1\n")
        import code_graph_extract as X  # noqa
        g = X.build_graph(d)
    app("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(g), repo, "main", "a" * 40))


def empty_graph_shape(value):
    return isinstance(value, dict) and value.get("repo") is None and value.get("coupled") == [] and value.get("central") == []


def main():
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    # LOGIN for the platform reader so the gate can connect AS it (prod sets LOGIN+password out-of-band; roles.sql
    # ships it NOLOGIN). ALTER ROLE needs the cluster admin (the migrator lacks CREATEROLE) — the same authority
    # db/roles.sql uses to LOGIN the local fixtures. Peer auth on localhost -> no password. Reset NOLOGIN at the end.
    login = subprocess.run(["psql", ADMIN, "-tAc", "ALTER ROLE example_platform_reader LOGIN;"],
                           capture_output=True, text=True)
    if login.returncode != 0 or "ERROR" in (login.stdout + login.stderr):
        print("could not grant LOGIN to example_platform_reader via ADMIN_DSN:\n", (login.stdout + login.stderr)[-800:])
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
        return 1

    # (0) FRESH INSTALL — entering the installation lazily provisions the mapping row, live by default.
    ingest("acme/web")   # forces a real working set so the uninstall purge has something to forget
    app("SELECT core.record_push_with_authority(%s,%s,%s)", ("acme/web", "main", "b" * 40))
    chk(row_present() == 1, "the installation routing row exists after a fresh install")
    chk(reader("SELECT core.installation_is_live(%s)", (INST,)) is True,
        "installation_is_live == true for a fresh, live install")
    chk(INST in list_ids(), "the live install is enumerated by list_installation_ids()")
    chk(reader("SELECT core.installation_is_live(%s)", ("does-not-exist",)) is False,
        "installation_is_live == false for an UNKNOWN installation id (fail-closed)")
    chk(reader("SELECT core.effect_for_installation(%s) IS NOT NULL", (INST,)) is True,
        "effect_for_installation returns a live install's content-free effect surface")
    chk(reader("SELECT core.account_coverage_for_installation(%s) IS NOT NULL", (INST,)) is True,
        "account_coverage_for_installation returns a live install's content-free usage surface")
    chk(reader("SELECT core.graph_insights_for_installation(%s)->>'repo'", (INST,)) == "acme/web",
        "graph_insights_for_installation can read a live install's ingested graph summary")

    # (1) UNINSTALL — the purge KEEPS the routing row but must mark it revoked (not live, not enumerated).
    from ingest import purge_account_working_set
    uninstall_key = "instlive-uninstall"
    deleted_installation_id = "A-777"
    seed_processing_uninstall(
        f"postgresql://veripsa_migrator@localhost/{DB}",
        uninstall_key,
        INST,
        deleted_installation_id,
    )
    res = purge_account_working_set(
        app,
        absent_installation_proof(INST, deleted_installation_id),
        uninstall_key,
    )
    purged = res.get("purged") if isinstance(res, dict) else None
    chk(isinstance(purged, dict) and res.get("ok") and purged.get("installations_revoked") == 1,
        f"uninstall purge reports installations_revoked=1 (got {purged.get('installations_revoked') if isinstance(purged, dict) else None})")
    chk(row_present() == 1, "uninstall KEEPS the routing row (reinstall re-binds the same id; ledger retained)")
    chk(reader("SELECT core.installation_is_live(%s)", (INST,)) is False,
        "installation_is_live == false after uninstall (revoked)")
    chk(INST not in list_ids(), "the uninstalled install is DROPPED from list_installation_ids()")
    chk(reader("SELECT core.effect_for_installation(%s)", (INST,)) is None,
        "effect_for_installation fail-closes for an uninstalled install")
    chk(reader("SELECT core.account_coverage_for_installation(%s)", (INST,)) is None,
        "account_coverage_for_installation fail-closes for an uninstalled install")

    # (2) REINSTALL — reactivate clears the revoke -> live again, enumerated again.
    relive = reactivate_from_delivery("instlive-reinstall", "created")
    if isinstance(relive, str):
        relive = json.loads(relive)
    chk(isinstance(relive, dict) and relive.get("installations_relived") == 1,
        f"reactivate reports installations_relived=1 (got {relive.get('installations_relived')})")
    chk(reader("SELECT core.installation_is_live(%s)", (INST,)) is True,
        "installation_is_live == true again after reinstall (reactivate cleared the revoke)")
    chk(INST in list_ids(), "the reinstalled install is enumerated again")
    ingest("acme/web")
    app("SELECT core.record_push_with_authority(%s,%s,%s)", ("acme/web", "main", "c" * 40))
    chk(reader("SELECT core.graph_insights_for_installation(%s)->>'repo'", (INST,)) == "acme/web",
        "a reinstalled live install can rebuild and read its graph summary")

    # (3) SUSPEND — release the lanes account-wide AND revoke liveness (a suspended install is not billable-live).
    suspend_key = "instlive-suspend"
    seed_processing_installation_event(
        f"postgresql://veripsa_migrator@localhost/{DB}",
        suspend_key,
        INST,
        "B-777",
        "suspend",
    )
    suspend_proof = json.dumps(current_suspend_proof(
        INST, "B-777", created_at="2099-01-01T00:00:00Z"))
    susp = app("SELECT core.release_account_claims_with_authority(%s,%s::jsonb)",
               (suspend_key, suspend_proof))
    if isinstance(susp, str):
        susp = json.loads(susp)
    chk(isinstance(susp, dict) and susp.get("installations_revoked") == 1,
        f"suspend reports installations_revoked=1 (got {susp.get('installations_revoked')})")
    chk(reader("SELECT core.installation_is_live(%s)", (INST,)) is False,
        "installation_is_live == false while SUSPENDED")
    chk(INST not in list_ids(), "a suspended install is dropped from list_installation_ids()")
    chk(reader("SELECT core.effect_for_installation(%s)", (INST,)) is None,
        "effect_for_installation fail-closes for a suspended install even though audit metadata remains")
    chk(reader("SELECT core.repos_for_installation(%s)", (INST,)) == [],
        "repos_for_installation returns no repos for a suspended install")
    chk(reader("SELECT core.repo_insights_for_installation(%s,%s)", (INST, "acme/web")) is None,
        "repo_insights_for_installation fail-closes for a suspended install")
    chk(reader("SELECT core.file_insights_for_installation(%s,%s,%s)", (INST, "acme/web", "app/a.py")) is None,
        "file_insights_for_installation fail-closes for a suspended install")
    chk(reader("SELECT core.now_for_installation(%s)", (INST,)) is None,
        "now_for_installation fail-closes for a suspended install")
    chk(reader("SELECT core.account_coverage_for_installation(%s)", (INST,)) is None,
        "account_coverage_for_installation fail-closes for a suspended install")
    chk(empty_graph_shape(reader("SELECT core.graph_insights_for_installation(%s)", (INST,))),
        "graph_insights_for_installation returns the benign empty shape for a suspended install")

    # (4) UNSUSPEND — the unsuspend onboarding branch calls reactivate -> live again.
    reactivate_from_delivery("instlive-unsuspend", "unsuspend")
    chk(reader("SELECT core.installation_is_live(%s)", (INST,)) is True,
        "installation_is_live == true after unsuspend (reactivate cleared the revoke)")

    # (5) IDEMPOTENCE — a re-delivered suspend revokes nothing the second time (does not churn the timestamp).
    suspend2_key = "instlive-suspend-again"
    seed_processing_installation_event(
        f"postgresql://veripsa_migrator@localhost/{DB}",
        suspend2_key,
        INST,
        "B-777",
        "suspend",
        received_at="2099-01-02T00:00:00Z",
    )
    app("SELECT core.release_account_claims_with_authority(%s,%s::jsonb)",
        (suspend2_key, suspend_proof))
    susp2 = app("SELECT core.release_account_claims_with_authority(%s,%s::jsonb)",
                (suspend2_key, suspend_proof))
    if isinstance(susp2, str):
        susp2 = json.loads(susp2)
    chk(isinstance(susp2, dict) and susp2.get("installations_revoked") == 0,
        f"a re-delivered suspend revokes 0 the second time (idempotent; got {susp2.get('installations_revoked')})")

    ok = all(checks)
    print("INSTALLATION LIVENESS GATE:", "PASS" if ok else "FAIL")
    # reset the cluster-global role back to NOLOGIN (it ships NOLOGIN; do not leave a stray LOGIN bit on a shared
    # cluster). Best-effort. Then drop the ephemeral DB.
    subprocess.run(["psql", ADMIN, "-tAc", "ALTER ROLE example_platform_reader NOLOGIN;"], capture_output=True, text=True)
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
