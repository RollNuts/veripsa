#!/usr/bin/env python3
"""DEPLOY-BLOCKER gate — a by-the-book PROD deploy must actually FUNCTION (security/availability-critical).

The hosted App connects as the Postgres role `veripsa_app` with NO per-account `core.credential` row — a clean
production deploy runs `db/roles.sql` + `db/schema.sql` ONLY; it does NOT run provision_seat(...,'veripsa_app')
(that is local/dogfood-only — the RUNBOOK + render.yaml forbid it in prod, since it would mint a wildcard
identity). Per webhook event the App instead pins the installation's account (enter_installation) and the gate
resolves its identity from THAT. If resolve_session_identity demands a credential BEFORE honoring the pinned
installation, every event 42501s ('no active credential') and time-to-first-value is infinite. This gate stands
up exactly that clean-prod shape and proves it works — the thing that silently failed on every webhook before.

It also proves the moat is unchanged (App with no installation pinned, and a genuine seat with no credential,
both still RAISE) and the server's empty-secret startup guard refuses a forgery-accepting public deploy.

Run:  python3 tests/test_app_deploy_resolution.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import server as S  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
# Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_server.py.
DB = "veripsa_deploytest_" + str(os.getpid())


def _psql_file(dsn, path):
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", path],
                       cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or "")[-800:]


def app_conn(installation=None):
    """A connection AS veripsa_app, optionally having ENTERED an installation (the live per-event shape)."""
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
        if installation is not None:
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (installation,))
    return conn


def resolve(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT agent, account FROM core.resolve_session_identity() AS r(agent, account)")
        return cur.fetchone()


def main() -> int:
    checks = []

    # CLEAN PROD DEPLOY: roles + schema ONLY. Deliberately NO bootstrap_local / provision_seat — this is exactly
    # what render.yaml's release step does (the RUNBOOK forbids provisioning veripsa_app in prod).
    ok, err = _psql_file("postgresql://localhost/postgres", "db/roles.sql")
    if not ok:
        print("roles.sql failed:\n", err); return 1
    subprocess.run(["dropdb", DB], capture_output=True)
    cr = subprocess.run(["createdb", DB, "-O", "veripsa_migrator"], capture_output=True, text=True)
    if cr.returncode != 0:
        subprocess.run(["createdb", DB], capture_output=True)
    ok, err = _psql_file(f"postgresql://veripsa_migrator@localhost/{DB}", "db/schema.sql")
    if not ok:
        print("schema.sql failed:\n", err); return 1

    # sanity: veripsa_app genuinely has NO credential row (this is the prod precondition the bug depended on).
    mig = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}"); mig.autocommit = True
    with mig.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.credential WHERE role_name='veripsa_app'")
        n_app_cred = cur.fetchone()[0]
    mig.close()
    checks.append(("clean prod precondition: veripsa_app has NO core.credential row (provision_seat NOT run)",
                   n_app_cred == 0))

    # (1) THE DEPLOY-BLOCKER: App resolves with an installation pinned but NO credential row — must SUCCEED.
    a = app_conn("777")
    agent, account = resolve(a)
    checks.append((f"App RESOLVES with installation pinned, no credential (agent={agent}, account={account})",
                   bool(account) and account == "ACCT-GH-777" and bool(agent)))

    # (2) and act_for_claim_with_authority — the exact gate call every PR event makes — must GRANT (it 42501'd today).
    with a.cursor() as cur:
        cur.execute("SELECT (core.act_for_claim_with_authority(%s,%s,%s,%s,%s)->>'granted')",
                    ("PR-1:a.py", "a.py", "org/repo", "main", "alice"))
        granted = cur.fetchone()[0]
    checks.append(("act_for_claim_with_authority GRANTS under the App service identity (the call that failed today)",
                   granted == "true"))

    # the claim is attributed to the real author (GH-alice), NOT a wildcard App agent — tenant/attribution intact.
    with a.cursor() as cur:
        cur.execute("SELECT account FROM core.resolve_session_identity() AS r(agent, account)")
        ev_account = cur.fetchone()[0]
    checks.append((f"the App's event account is the pinned installation's own account ({ev_account})",
                   ev_account == "ACCT-GH-777"))
    a.close()

    # (3) MOAT UNCHANGED — App with NO installation pinned + NO credential must STILL RAISE (no silent wildcard).
    raised_app = False
    b = app_conn(None)
    try:
        resolve(b)
    except psycopg2.errors.InsufficientPrivilege:
        raised_app = True
    except psycopg2.Error:
        raised_app = True
    b.close()
    checks.append(("MOAT: App with NO installation pinned + no credential STILL raises (no wildcard fallback)",
                   raised_app))

    # (4) MOAT UNCHANGED — a genuine unprovisioned SEAT role still raises 42501 (only veripsa_app is the service id).
    raised_seat = False
    seat = psycopg2.connect(f"postgresql://veripsa_demo_agent@localhost/{DB}"); seat.autocommit = True
    try:
        with seat.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT account FROM core.resolve_session_identity() AS r(agent, account)")
    except psycopg2.Error:
        raised_seat = True
    seat.close()
    checks.append(("MOAT: a genuine unprovisioned seat role STILL raises 42501 (service id is veripsa_app ONLY)",
                   raised_seat))

    # (5) READINESS self-check helper: app_identity_ok() must return True against this clean-prod DB (the /readyz
    #     probe exercises the same service-identity resolve path a live event uses — would catch a 42501 misconfig).
    ok_ident, err_ident = S.app_identity_ok(f"postgresql://veripsa_app@localhost/{DB}")
    checks.append((f"/readyz self-check: app_identity_ok() is True on a clean-prod deploy (err={err_ident})",
                   ok_ident is True and err_ident is None))

    # (6) SECURITY STARTUP GUARD: serve() with an empty GH_WEBHOOK_SECRET must REFUSE to start (else the public
    #     webhook accepts ALL forgeries — verify_signature returns True for an empty secret). Assert it exits.
    refused = False
    saved = {k: os.environ.get(k) for k in ("GH_WEBHOOK_SECRET", "VERIPSA_DSN", "VERIPSA_ALLOW_UNSIGNED")}
    try:
        os.environ["GH_WEBHOOK_SECRET"] = ""
        os.environ["VERIPSA_DSN"] = f"postgresql://veripsa_app@localhost/{DB}"
        os.environ.pop("VERIPSA_ALLOW_UNSIGNED", None)
        try:
            S.serve(0)
        except SystemExit:
            refused = True
        except Exception:
            refused = False     # any OTHER exception means it got PAST the guard → guard failed
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    checks.append(("SECURITY GUARD: serve() with an empty GH_WEBHOOK_SECRET REFUSES to start (no forgery-accepting deploy)",
                   refused))

    # and that empty secret indeed accepts forgeries (the property the guard protects against) — proves the guard
    # is load-bearing, not cosmetic.
    checks.append(("verify_signature returns True for an empty secret (the exact hole the startup guard closes)",
                   S.verify_signature("", b"forged", None) is True))

    ok_all = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok_all = ok_all and bool(cond)
    print("DEPLOY RESOLUTION GATE:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
