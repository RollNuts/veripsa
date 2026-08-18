"""Reporting — also QUERIES `accounts` (so it co-depends on the same table as repo.py + the migration)."""


def account_summary(db):
    return db.execute("SELECT count(*) FROM accounts")
