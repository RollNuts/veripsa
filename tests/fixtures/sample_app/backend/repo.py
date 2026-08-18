"""Data access — QUERIES the `accounts` table (schema coupling to the migration that ALTERs it)."""


def fetch_accounts(db):
    return db.execute("SELECT id, name, active FROM accounts WHERE active = true")


def deactivate(db, account_id: int):
    return db.execute("UPDATE accounts SET active = false WHERE id = %s", (account_id,))
