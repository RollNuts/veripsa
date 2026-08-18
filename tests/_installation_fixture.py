"""Small helpers for tests that start from an already-live GitHub App install.

Production may create or reactivate a tenant only from a durable lifecycle
delivery plus an App-JWT installation proof.  Tests whose subject is unrelated
to installation lifecycle can instead seed that post-activation state directly:
the stable owner-id route exists and its current GitHub installation generation
is already recorded.  This keeps those tests focused without weakening the
runtime generation fence.
"""
from __future__ import annotations

import psycopg2


def seed_live_installation(
    app_dsn: str,
    owner_dsn: str,
    account_id: str | int,
    github_installation_id: str | int,
    *,
    created_at: str = "2026-01-01T00:00:00Z",
) -> str:
    """Provision a test route, then stamp its already-verified live generation."""
    account_key = str(account_id)
    installation_id = str(github_installation_id)

    app = psycopg2.connect(app_dsn)
    app.autocommit = True
    try:
        with app.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SELECT core.enter_installation_with_authority(%s)",
                (account_key,),
            )
            routed = cur.fetchone()
            if not routed or not routed[0]:
                raise RuntimeError("test installation route was not provisioned")
    finally:
        app.close()

    owner = psycopg2.connect(owner_dsn)
    owner.autocommit = True
    try:
        with owner.cursor() as cur:
            cur.execute(
                "UPDATE core.installation_account "
                "SET github_installation_id=%s, "
                "github_installation_created_at=%s::timestamptz, revoked_at=NULL "
                "WHERE installation_id=%s",
                (installation_id, created_at, account_key),
            )
            if cur.rowcount != 1:
                raise RuntimeError("test installation generation was not recorded")
    finally:
        owner.close()

    return f"ACCT-GH-{account_key}"
