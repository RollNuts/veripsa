"""Durable lifecycle authority fixtures shared by focused database gates."""
from __future__ import annotations

import json

import psycopg2


def absent_installation_proof(account_id: str | int, installation_id: str | int) -> dict:
    """App-JWT result for an installation generation confirmed absent."""
    return {
        "state": "absent",
        "deleted_installation_id": str(installation_id),
        "account_id": str(account_id),
    }


def current_suspend_proof(
    account_id: str | int,
    installation_id: str | int,
    *,
    created_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    """App-JWT current-account result bound to an exact installation.suspend target."""
    account = str(account_id)
    installation = str(installation_id)
    return {
        "state": "current",
        "suspended_installation_id": installation,
        "account_id": account,
        "current": {
            "installation_id": installation,
            "account_id": account,
            "created_at": created_at,
            "suspended": True,
        },
    }


def seed_processing_uninstall(
    owner_dsn: str,
    delivery_key: str,
    account_id: str | int,
    installation_id: str | int,
    *,
    received_at: str = "2026-01-02T00:00:00Z",
) -> None:
    """Insert the exact processing installation.deleted row consumed by the purge gate."""
    seed_processing_installation_event(
        owner_dsn,
        delivery_key,
        account_id,
        installation_id,
        "deleted",
        received_at=received_at,
    )


def seed_processing_installation_event(
    owner_dsn: str,
    delivery_key: str,
    account_id: str | int,
    installation_id: str | int,
    action: str,
    *,
    received_at: str = "2026-01-02T00:00:00Z",
) -> None:
    """Insert one exact processing installation lifecycle delivery."""
    account = str(account_id)
    payload = json.dumps(
        {
            "action": action,
            "installation": {
                "id": str(installation_id),
                "account": {"id": account},
            },
        },
        separators=(",", ":"),
    )
    conn = psycopg2.connect(owner_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO core.webhook_delivery("
                "delivery_key,event_type,account_key,payload,status,received_at) "
                "VALUES (%s,'installation',%s,%s::jsonb,'processing',%s::timestamptz)",
                (delivery_key, account, payload, received_at),
            )
    finally:
        conn.close()
