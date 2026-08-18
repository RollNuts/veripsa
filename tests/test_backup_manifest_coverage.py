#!/usr/bin/env python3
"""BACKUP MANIFEST COVERAGE — the schema↔backup drift backstop (DR completeness).

`test_backup_restore.py::manifest_drift` already proves every COLUMN of a table that is ALREADY in the
backup manifest is accounted for (issue #844, the S3a fact_class/detector regression). It CANNOT catch a
WHOLE new durable table added to `db/schema/*.sql` that never gets added to `backup_export` — such a table
is simply never iterated, so the column check stays green while the DR export silently omits it. That is
the exact "a schema table silently missing from backup with no failing test" class.

THIS gate is the STRUCTURAL backstop, modelled on `test_force_rls_enumeration.py` (which enumerates the
catalog and asserts every multi-tenant table is FORCE-RLS or on an explicit allowlist). It enumerates EVERY
`core.*` table DECLARED in `db/schema/*.sql` and asserts each is either:
  (a) in the durable backup set (`backup_export.TABLE_META`), or
  (b) on an EXPLICIT, reasoned exclusion allowlist below.
A new `core.*` table that is neither fails LOUD here, naming the table — forcing a conscious "back it up, or
record WHY it is safe to lose in DR" decision at the moment the table is added, not after a disaster reveals
the hole.

It also RECONCILES the four hand-maintained backup lists so they cannot silently diverge:
  - `RESTORE_ORDER` vs `TABLE_META`   (a table in RESTORE_ORDER but not TABLE_META has no manifest → export
                                       validation cannot describe it; a TABLE_META table missing from
                                       RESTORE_ORDER is never imported → restored as zero rows).
  - `DURABLE_TABLES` ⊆ `TABLE_META`   (a durable table with no manifest cannot be exported at all).
  - `COUNT_KEYS`     ⊆ `TABLE_META`   (a reconciliation count key with no backing table).

STATIC (parses the schema SQL; no database) so it runs in the FAST PR gate — a DR-breaking table addition
is caught ON THE PR, not only in the post-merge full suite where `test_backup_restore.py` runs.

Run:  python3 tests/test_backup_manifest_coverage.py
"""
from __future__ import annotations

import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import backup_export  # noqa: E402


# ── EXCLUSION ALLOWLIST ─────────────────────────────────────────────────────────────────────────────────
# Each `core.*` table NOT in the durable backup set must appear in exactly one bucket below, with a reason.
# The buckets are documentation, not behaviour — the test only cares that the UNION covers every non-backup
# table. Splitting them keeps the "why" auditable and separates a SETTLED exclusion (derived / ephemeral /
# operational — safe to lose because it is rebuilt, transient, or host control-plane) from an OPEN one
# (pre-GA durable state that MUST be promoted into the backup before its capability serves tenants).

# Recomputable: rebuilt from source / git history by re-ingest or backfill. Losing them costs a re-ingest,
# not data. (The DR backup deliberately restores authored FACTS, not derived analysis.)
EXCLUDE_DERIVED = {
    "code_node":            "code graph nodes — rebuilt from source by ingest",
    "code_edge":            "code graph edges — rebuilt from source by ingest",
    "graph_version":        "code graph generation marker — re-derived on re-ingest",
    "co_change":            "co-change signal — rebuilt from git history / periodic backfill",
    "co_change_seen_commit": "per-commit co-change dedup cursor — re-derived on re-ingest",
}

# Live, transient, self-expiring state with its own lease/heartbeat — explicitly NOT a ledger. Re-created by
# agents in the normal flow; a stale restored lock would be worse than an absent one.
EXCLUDE_EPHEMERAL = {
    "claim": "THE LOCK — live mutable claim state (30-min lease, heartbeat); explicitly 'NOT a ledger'",
}

# Host control-plane / telemetry, not tenant product data. Restoring an old row could REPLAY or mislead
# (the two github_delivery_recovery* tables carry the documented deliberate exclusion in backup_export.py:
# restoring an old opaque cursor/claim could replay a POST from backup time).
EXCLUDE_OPERATIONAL = {
    "active_alert":                 "host cross-tenant alert board — re-fired by the watchdog",
    "boot_reconcile_state":         "boot reconcile cursor — a fresh deploy re-derives it",
    "webhook_worker_instance":      "worker liveness heartbeat board — a fresh boot re-registers its instance; a restored stale heartbeat only self-expires (1-day housekeeping) and at worst delays one reap, never loses tenant data (host control-plane, safe to lose in DR)",
    "github_delivery_recovery":      "external-API redelivery control plane — restoring an old claim could replay a POST (documented exclusion)",
    "github_delivery_recovery_scan": "external-API redelivery scan cursor — a fresh deploy starts a new scan (documented exclusion)",
    "policy_refresh_outbox":         "G4 policy-change refresh outbox — re-enqueued on the next policy write; restoring a stale row could re-post a superseded refresh (safe to lose in DR, same class as github_delivery_recovery)",
    "graph_convergence_lease":        "short-lived graph worker slot snapshots — restored leases would falsely block fresh convergence and expire naturally",
}

# OPEN ITEM — durable, tenant/agent-authored, content-free state for capabilities that are NOT yet generally
# available (the cross-repo / workspace / social layer ships behind gated kill-switches). They are absent from
# the DR backup today because they are not yet live product surfaces (expected near-empty in production). Each
# MUST be promoted into `backup_export.TABLE_META` BEFORE its owning capability serves production tenants —
# otherwise the first live rows are unprotected by DR. Tracked as a DR follow-up; this gate keeps the list
# from growing silently. (If a capability here goes GA, move its table up into the backup and delete it here —
# the reconciliation asserts below will then require the manifest/restore wiring too.)
EXCLUDE_PREGA_DURABLE_REVIEW = {
    "intent":           "agent declared-scope ledger (drift baseline) — promote to backup when scope/intent GA",
    "grant":            "direct delegations (authz) — promote to backup when delegation GA",
    "policy":           "owner setting keys (external_share, scope_checkpoint, …) — promote to backup when used in prod",
    "store_connection": "attached-store identities (content-free) — promote to backup when attach/relay GA",
    "workspace":        "cross-repo collaboration space — promote to backup when workspaces GA",
    "workspace_member": "workspace membership + consent — promote to backup when workspaces GA",
    "follow":           "social graph — promote to backup when the social layer GA",
}

# Every excluded table, flattened. A table may appear in exactly ONE bucket (asserted below).
_EXCLUDE_BUCKETS = {
    "EXCLUDE_DERIVED": EXCLUDE_DERIVED,
    "EXCLUDE_EPHEMERAL": EXCLUDE_EPHEMERAL,
    "EXCLUDE_OPERATIONAL": EXCLUDE_OPERATIONAL,
    "EXCLUDE_PREGA_DURABLE_REVIEW": EXCLUDE_PREGA_DURABLE_REVIEW,
}

# A broken/empty parse must fail LOUD, not pass on an empty enumeration (same guard as the RLS enumeration).
# 28 core base tables exist at authoring time (deploy_event at 20_core.sql:539 is commented out — a stub, not
# a table); a floor a few below that catches a parser regression without being brittle to the next legitimate
# table addition.
_MIN_TABLES = 24

_CREATE_RE = re.compile(r"create\s+table\s+(?:if\s+not\s+exists\s+)?core\.([a-z_][a-z0-9_]*)", re.IGNORECASE)


def schema_core_tables():
    """Every `core.*` base table declared in db/schema/*.sql. Line-comments stripped first so prose that
    merely MENTIONS a table name cannot register a phantom table."""
    tables = set()
    for path in sorted(glob.glob(os.path.join(ROOT, "db", "schema", "*.sql"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                code = line.split("--", 1)[0]  # drop trailing line comment
                for m in _CREATE_RE.finditer(code):
                    tables.add(m.group(1).lower())
    return tables


def _flat_exclusions():
    flat = {}
    collisions = []
    for bucket_name, bucket in _EXCLUDE_BUCKETS.items():
        for table in bucket:
            if table in flat:
                collisions.append((table, flat[table], bucket_name))
            flat[table] = bucket_name
    return flat, collisions


def main() -> None:
    schema = schema_core_tables()
    backup = set(backup_export.TABLE_META)
    excluded, collisions = _flat_exclusions()

    # ── Sanity floor: a broken parse must not pass silently. ──────────────────────────────────────────────
    assert len(schema) >= _MIN_TABLES, (
        f"parsed only {len(schema)} core.* tables from db/schema (floor {_MIN_TABLES}) — "
        f"the schema-table parser likely regressed; refusing to pass on a near-empty enumeration")

    # ── A table may be excluded in exactly one bucket. ───────────────────────────────────────────────────
    assert not collisions, f"table listed in multiple exclusion buckets: {collisions}"

    # ── A table cannot be BOTH backed up and excluded. ───────────────────────────────────────────────────
    both = sorted(backup & set(excluded))
    assert not both, f"table is BOTH in the backup set and on the exclusion allowlist: {both}"

    # ── THE STRUCTURAL ASSERTION: every schema table is classified (backed up OR explicitly excluded). ────
    #    A new durable core.* table added without a decision fails HERE, naming the table.
    classified = backup | set(excluded)
    unclassified = sorted(schema - classified)
    assert not unclassified, (
        "UNCLASSIFIED core.* table(s) — add to backup_export.TABLE_META (durable data) OR to an exclusion "
        f"bucket in this test with a reason (safe to lose in DR): {unclassified}")

    # ── Keep the lists honest: no backup/allowlist entry may name a table that no longer exists. ──────────
    stale_backup = sorted(backup - schema)
    assert not stale_backup, f"backup_export.TABLE_META names table(s) not in db/schema: {stale_backup}"
    stale_excl = sorted(set(excluded) - schema)
    assert not stale_excl, f"exclusion allowlist names table(s) not in db/schema: {stale_excl}"

    # ── Reconcile the four hand-maintained backup lists (backup audit D2). ────────────────────────────────
    restore_order = set(backup_export.RESTORE_ORDER)
    assert restore_order == backup, (
        "RESTORE_ORDER and TABLE_META disagree — "
        f"RESTORE_ORDER-only={sorted(restore_order - backup)} TABLE_META-only={sorted(backup - restore_order)}")
    assert len(backup_export.RESTORE_ORDER) == len(set(backup_export.RESTORE_ORDER)), \
        "RESTORE_ORDER contains a duplicate table"

    durable = set(backup_export.DURABLE_TABLES)
    assert durable <= backup, f"DURABLE_TABLES not in TABLE_META (cannot be exported): {sorted(durable - backup)}"

    count_keys = set(backup_export.COUNT_KEYS)
    assert count_keys <= backup, \
        f"COUNT_KEYS names table(s) not in TABLE_META: {sorted(count_keys - backup)}"

    print(f"BACKUP MANIFEST COVERAGE GATE: PASS "
          f"({len(schema)} core tables: {len(backup)} backed up, {len(excluded)} explicitly excluded, "
          f"{len(EXCLUDE_PREGA_DURABLE_REVIEW)} pre-GA durable pending promotion)")


if __name__ == "__main__":
    main()
