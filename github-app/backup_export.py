#!/usr/bin/env python3
"""Veripsa — versioned disaster-recovery export of the durable product.

WHAT IS CATASTROPHIC TO LOSE: the append-only hash-chain-style fact ledger `core.event` AND the curated
records `core.statement` ARE the durable product (records-not-correctness). Retained uninstall/repository
lifecycle fences, billing-order high-water rows, sanitized unfinished deliveries, recovery-batch completion proofs,
and scrubbed erased-delivery receipts are non-rebuildable safety state. Explicit hard erasure removes its
account-keyed lifecycle marker; only opaque scrubbed receipt keys survive that boundary. The live code graph /
claims / lanes are rebuildable from
GitHub (a re-ingest/backfill), but the ledger, statements, and retained fences are NOT. Render's managed Postgres has automated
daily backups + PITR, BUT (a) that is Render's copy, on Render, with Render's retention — if the account or
the database is lost, so is the only copy; and (b) a backup nobody has ever RESTORED is a hope, not a plan.

WHAT THIS IS: an owned, off-host, portable JSONL export. A fixed versioned manifest precedes the records and a
footer carries exact per-table counts plus a SHA-256 digest. Restore verifies the complete envelope, record shapes,
privacy invariants, and duplicate primary keys before it opens a database connection. The digest detects accidental
corruption/truncation; it is not a signature, so the offsite object must still be encrypted and access-controlled.

CONTENT-FREE (the moat holds in the backup too): core.event / core.statement are content-free BY SCHEMA —
paths, branch names, author logins, short labels; NEVER file contents or bodies. Erased receipts are exported only
after account/repo/payload/error/lock fields are scrubbed; unfinished work keeps only the ingress-minimized payload
and drops error text/worker progress. A non-erased done row is accepted only as the empty-payload completion proof
for an unfinished exact operator-recovery batch. It carries no DSN, credential, or API/authentication token, but paths,
display names, operator labels, logins, and opaque ids can still be personal/confidential operational data:
encrypt and access-control the
off-box copy; never publish it. The `count` command prints row counts only.

ROLE: export/count use the dedicated NOLOGIN-by-default `veripsa_backup` principal after the operator grants LOGIN
and supplies a rotated credential out of band. Restore still requires the owner/migrator principal. The live App
identity must not carry the cross-tenant export privilege.

USAGE:
  BACKUP_DSN=postgresql://veripsa_backup@host/db  python3 github-app/backup_export.py export > durable.jsonl
  python3 github-app/backup_export.py verify durable.jsonl
  OWNER_DSN=postgresql://veripsa_migrator@host/db python3 github-app/backup_export.py restore durable.jsonl
  OWNER_DSN=postgresql://veripsa_migrator@host/db python3 github-app/backup_export.py diagnose --delivery <id>

RESTORE (documented + tested in tests/test_backup_restore.py and github-app/RUNBOOK.md §Disaster recovery):
the JSONL re-imports into a freshly-bootstrapped schema via the gated recorders / a privileged owner load;
the test proves an export → fresh DB → import round-trips the durable rows and lifecycle fence byte-for-byte.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime

FORMAT_NAME = "veripsa-durable-jsonl"
FORMAT_VERSION = 2
HASH_ALGORITHM = "sha256"
FETCH_BATCH = 500
_OPERATOR_RECOVERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_OPERATOR_RECOVERY_BATCH_TOKEN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_OPERATOR_CONTINUATION_SHA = re.compile(r"^[0-9a-f]{40}$")
_RFC3339_AWARE_TIMESTAMP = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,6}))?"
    r"(?P<offset>Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
)
_OPERATOR_GITHUB_REDELIVERY_OUTCOMES = frozenset({
    "accepted", "transport_ambiguous", "redirect_rejected", "auth_rejected",
    "rate_limited", "request_rejected", "server_rejected", "unexpected_status",
})

# installation_account also carries the convergence scheduler's rebuildable
# routing cache.  The queue rows and graph leases that give these fields meaning
# are deliberately excluded from DR, so restoring their live values would
# manufacture due work, resurrect a dead lease, or publish counters with no
# backing rows.  Current exports still carry and validate the complete table
# shape; restore (and legacy format-v2 normalization) resets only this named
# operational subset to a fresh-schema state.
INSTALLATION_CONVERGENCE_FRESH_DEFAULTS = {
    "policy_refresh_due_at": None,
    "graph_refresh_due_at": None,
    "legacy_graph_refresh_due_at": None,
    "convergence_claimed_until": None,
    "convergence_claimed_by": None,
    "convergence_claim_epoch": None,
    "convergence_graph_claim_count": 0,
    "convergence_graph_reclaim_at": None,
    "convergence_pending_count": 0,
    "convergence_retry_exhausted_count": 0,
    "convergence_quota_deferred_count": 0,
    "convergence_stall_started_at": None,
    # The restored database already has the current schema.  Marking the route
    # current avoids treating an intentionally empty DR queue as a legacy
    # bridge candidate; the next real enqueue advances from epoch zero.
    "convergence_schema_version": 2,
    "convergence_next_epoch": 0,
}

# the DURABLE, catastrophic-to-lose tables and their columns (stable, content-free), PLUS the tenant-registry
# rows the durable rows depend on (so a restore is self-sufficient — not orphaned). Per-table restore metadata:
#   cols       — the columns to re-insert
#   pk_cols    — the conflict target and duplicate-record identity
#   forgery    — True if the table is forgery-blocked (arm mark_governed_write before the insert)
#   needs_acct — True if FORCE RLS requires core.current_account pinned to the row's account for the insert
# Restore the privacy/replay fence in the same transaction as the product ledger. It is listed first so the
# invariant remains obvious even though no intermediate state is externally visible before COMMIT.
# Deliberate exclusion: github_delivery_recovery_scan / github_delivery_recovery are an external-API recovery
# control plane, not product or ingress authority. Restoring an old opaque cursor/claim could replay a POST from
# backup time; a fresh deployment starts a new metadata scan. Long-lived compacted counts are non-paging telemetry,
# deliberately not promoted into the durable product backup merely to make an unsafe cursor row restorable.
RESTORE_ORDER = [
    "account_lifecycle_tombstone",
    "account", "agent", "credential", "installation_account",
    "repository_lifecycle_tombstone", "repository_lifecycle_activation", "account_plan_event",
    "webhook_delivery", "event", "statement",
]
TABLE_META = {
    "account_lifecycle_tombstone": {
        "cols": ["account_id", "reason", "tombstoned_at", "active", "last_event_received_at",
                 "last_delivery_key", "blocked_installation_id"],
        "pk_cols": ["account_id"], "forgery": False, "needs_acct": False,
    },
    # Erased receipts preserve only the opaque idempotency boundary. Sanitized queued/processing/failed rows also
    # appear because GitHub may never redeliver work already ACKed 202; processing is requeued on restore. A done
    # non-erased row can appear only as the immutable completed sibling needed to authenticate an unfinished exact
    # operator-recovery batch; its payload is already empty and restore never requeues it.
    "webhook_delivery": {
        "cols": ["delivery_key", "event_type", "account_key", "repo", "payload", "status", "attempts",
                 "received_at", "updated_at", "locked_at", "done_at", "last_error", "not_before",
                 "lease_generation", "causal_order_version", "owner_instance",
                 "retry_window_expires_at", "auto_rearm_count", "operator_recovery_id",
                 "operator_recovered_at", "operator_recovery_batch_size",
                 "operator_recovery_batch_token", "operator_recovery_count",
                 "operator_continuation_id", "operator_continued_at",
                 "operator_continuation_sha", "operator_continuation_count",
                 "operator_github_redelivery_delivery_id", "operator_github_redelivery_sha",
                 "operator_github_redelivery_spent_at", "operator_github_redelivery_outcome",
                 "operator_github_redelivery_count"],
        "pk_cols": ["delivery_key"], "forgery": False, "needs_acct": False,
    },
    "account": {
        "cols": ["account_id", "display_name", "plan", "account_state", "created_at"],
        "pk_cols": ["account_id"], "forgery": True, "needs_acct": True,
    },
    "agent": {
        "cols": ["agent_id", "account_id", "display_name", "agent_kind", "default_model", "operator",
                 "agent_state", "created_at"],
        "pk_cols": ["agent_id"], "forgery": True, "needs_acct": True,
    },
    "credential": {
        "cols": ["role_name", "agent_id", "account_id", "credential_state", "created_at"],
        "pk_cols": ["role_name"], "forgery": False, "needs_acct": False,
    },
    "installation_account": {
        "cols": ["installation_id", "account_id", "bound_at", "revoked_at", "account_login",
                 "account_type", "account_seen_at", "github_installation_id",
                 "github_installation_created_at",
                 *INSTALLATION_CONVERGENCE_FRESH_DEFAULTS],
        "pk_cols": ["installation_id"], "forgery": False, "needs_acct": False,
    },
    "repository_lifecycle_tombstone": {
        "cols": ["account_id", "repository_id", "repo", "reason", "tombstoned_at",
                 "lifecycle_received_at", "superseded_at", "generation_started_at"],
        "pk_cols": ["account_id", "repository_id", "repo"], "forgery": False, "needs_acct": False,
    },
    "repository_lifecycle_activation": {
        "cols": ["account_id", "repository_id", "repo", "activated_at", "lifecycle_authoritative",
                 "generation_started_at"],
        "pk_cols": ["account_id", "repository_id"], "forgery": False, "needs_acct": True,
    },
    "account_plan_event": {
        "cols": ["account_id", "last_effective_at", "last_applied_at"],
        "pk_cols": ["account_id"], "forgery": False, "needs_acct": False,
    },
    "event": {
        # counterparty_sha + fact_fingerprint arrived with the compat-finding lane (20_core.sql): a bare sha and a
        # content-free identity hash — both safe to export, and required, or every DR export/restore validates red.
        "cols": ["event_id", "account_id", "kind", "agent_id", "counterparty_agent", "repo", "branch",
                 "path", "commit_sha", "model", "detail", "occurred_at", "visibility",
                 "counterparty_sha", "fact_fingerprint", "fact_class", "detector"],
        "pk_cols": ["account_id", "event_id"], "forgery": True, "needs_acct": True,
    },
    "statement": {
        "cols": ["statement_id", "account_id", "agent_id", "utterance", "about_repo", "about_branch",
                 "about_path", "supersedes", "superseded", "stated_at", "visibility"],
        "pk_cols": ["account_id", "statement_id"], "forgery": True, "needs_acct": True,
    },
}
# The rows whose LOSS is catastrophic (what count() reconciles on). The registry rows are restore-support.
DURABLE_TABLES = (
    "event", "statement", "account_lifecycle_tombstone", "repository_lifecycle_tombstone",
    "repository_lifecycle_activation", "account_plan_event", "webhook_delivery",
)
COUNT_KEYS = {
    "event": "events",
    "statement": "statements",
    "account_lifecycle_tombstone": "lifecycle_tombstones",
    "repository_lifecycle_tombstone": "repository_lifecycle_tombstones",
    "repository_lifecycle_activation": "repository_lifecycle_activations",
    "account_plan_event": "account_plan_event_fences",
}
# Keep the shipped format-v2 footer shape stable. `unfinished_webhook_deliveries` is the historical key for every
# replay-critical non-erased inbox record, including a done recovery-proof sibling retained solely because another
# member of its exact operator batch is unfinished. Adding a third key would invalidate genuine v2 footers.
WEBHOOK_COUNT_KEYS = ("erased_delivery_receipts", "unfinished_webhook_deliveries")


def _connect(dsn: str):
    import psycopg2
    return psycopg2.connect(dsn)


def _empty_counts() -> dict:
    return {key: 0 for key in (*COUNT_KEYS.values(), *WEBHOOK_COUNT_KEYS)}


def _empty_table_counts() -> dict:
    return {table: 0 for table in RESTORE_ORDER}


def _count_key(rec: dict) -> str | None:
    table = rec.get("_table")
    if table == "webhook_delivery":
        return ("erased_delivery_receipts" if rec.get("event_type") == "erased"
                else "unfinished_webhook_deliveries")
    return COUNT_KEYS.get(table)


def _count_record(counts: dict, rec: dict) -> None:
    key = _count_key(rec)
    if key:
        counts[key] += 1


def _canonical_line(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _reject_duplicate_json_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"backup contains duplicate JSON key {key!r}")
        out[key] = value
    return out


def _reject_json_constant(value):
    raise ValueError(f"backup contains non-JSON numeric constant {value!r}")


def _load_json(line: str, line_no: int) -> dict:
    try:
        value = json.loads(
            line,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"backup line {line_no} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"backup line {line_no} is not a JSON object")
    return value


def _manifest() -> dict:
    return {
        "_backup": "manifest",
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "hash_algorithm": HASH_ALGORITHM,
        "tables": list(RESTORE_ORDER),
    }


def _footer(records: int, table_counts: dict, durable_counts: dict, digest: str) -> dict:
    return {
        "_backup": "footer",
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "records": records,
        "table_counts": dict(table_counts),
        "durable_counts": dict(durable_counts),
        "sha256": digest,
    }


class _SeenKeys:
    """Disk-backed exact PK registry; avoids retaining every event id in Python memory."""

    def __init__(self):
        # An empty SQLite filename creates a private temporary database which is deleted on close. It stores only
        # table names and primary-key JSON, never payloads. SQLite creates it with owner-only permissions.
        self._db = sqlite3.connect("")
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.execute(
            "CREATE TABLE seen (table_name TEXT NOT NULL, pk_json TEXT NOT NULL, "
            "PRIMARY KEY(table_name, pk_json)) WITHOUT ROWID"
        )

    def add(self, rec: dict) -> None:
        table = rec["_table"]
        pk = [rec.get(col) for col in TABLE_META[table]["pk_cols"]]
        if any(value is None for value in pk):
            raise ValueError(f"backup {table} record has a null primary key")
        try:
            self._db.execute(
                "INSERT INTO seen(table_name,pk_json) VALUES (?,?)",
                (table, _canonical_line({"pk": pk})),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"backup contains duplicate {table} primary key") from exc

    def close(self) -> None:
        self._db.close()


def _validate_erased_receipt(rec: dict) -> None:
    """Refuse to export/restore tenant-bearing residue masquerading as an erased receipt."""
    if not (
        rec.get("event_type") == "erased"
        and rec.get("status") == "done"
        and rec.get("account_key") is None
        and rec.get("repo") is None
        and rec.get("payload") == {}
        and rec.get("attempts") == 0
        and rec.get("locked_at") is None
        and rec.get("last_error") is None
        and rec.get("not_before") is None
        and rec.get("retry_window_expires_at") is None
        and rec.get("operator_recovery_id") is None
        and rec.get("operator_recovered_at") is None
        and rec.get("operator_recovery_batch_size") is None
        and rec.get("operator_recovery_batch_token") is None
        and rec.get("operator_recovery_count") == 0
        and rec.get("operator_continuation_id") is None
        and rec.get("operator_continued_at") is None
        and rec.get("operator_continuation_sha") is None
        and rec.get("operator_continuation_count") == 0
        and rec.get("operator_github_redelivery_delivery_id") is None
        and rec.get("operator_github_redelivery_sha") is None
        and rec.get("operator_github_redelivery_spent_at") is None
        and rec.get("operator_github_redelivery_outcome") is None
        and rec.get("operator_github_redelivery_count") == 0
    ):
        raise ValueError("backup contains a non-scrubbed erased webhook_delivery")


def _normalize_iso8601_for_fromisoformat(value: str) -> str:
    """Make RFC3339/Postgres subseconds portable to Python 3.9.

    Python 3.9's ``datetime.fromisoformat`` accepts only three or six
    fractional digits, while PostgreSQL can serialize any precision from one
    through six.  Padding with trailing zeroes preserves the instant.  Refuse
    greater precision instead of relying on newer Python versions silently
    truncating it.
    """
    match = _RFC3339_AWARE_TIMESTAMP.fullmatch(value)
    if match is None:
        raise ValueError("timestamp is not strict RFC3339")
    fraction = match.group("fraction")
    fraction_text = f".{fraction.ljust(6, '0')}" if fraction else ""
    offset = "+00:00" if match.group("offset") == "Z" else match.group("offset")
    return (
        f"{match.group('date')}T{match.group('time')}"
        f"{fraction_text}{offset}"
    )


def _parse_aware_timestamp(value, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"backup webhook row has invalid {field}")
    try:
        parsed = datetime.fromisoformat(
            _normalize_iso8601_for_fromisoformat(value))
    except ValueError as exc:
        raise ValueError(f"backup webhook row has invalid {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"backup webhook row has non-aware {field}")
    return parsed


def _validate_operator_recovery_audit(rec: dict) -> None:
    count = rec.get("operator_recovery_count")
    recovery_id = rec.get("operator_recovery_id")
    recovered_at = rec.get("operator_recovered_at")
    batch_size = rec.get("operator_recovery_batch_size")
    batch_token = rec.get("operator_recovery_batch_token")
    continuation_count = rec.get("operator_continuation_count")
    continuation_id = rec.get("operator_continuation_id")
    continued_at = rec.get("operator_continued_at")
    continuation_sha = rec.get("operator_continuation_sha")
    redelivery_count = rec.get("operator_github_redelivery_count")
    redelivery_delivery_id = rec.get("operator_github_redelivery_delivery_id")
    redelivery_sha = rec.get("operator_github_redelivery_sha")
    redelivery_spent_at = rec.get("operator_github_redelivery_spent_at")
    redelivery_outcome = rec.get("operator_github_redelivery_outcome")
    if type(continuation_count) is not int or continuation_count not in (0, 1):
        raise ValueError("backup webhook row has invalid operator continuation count")
    if type(count) is not int or count not in (0, 1):
        raise ValueError("backup webhook row has invalid operator recovery count")
    if type(redelivery_count) is not int or redelivery_count not in (0, 1):
        raise ValueError("backup webhook row has invalid operator GitHub redelivery count")
    if redelivery_count == 0:
        if (redelivery_delivery_id is not None or redelivery_sha is not None
                or redelivery_spent_at is not None or redelivery_outcome is not None):
            raise ValueError("backup webhook row has incoherent operator GitHub redelivery audit")
    elif (count != 1 or continuation_count != 1
          or type(redelivery_delivery_id) is not int or redelivery_delivery_id <= 0
          or not isinstance(redelivery_sha, str)
          or not _OPERATOR_CONTINUATION_SHA.fullmatch(redelivery_sha)
          or not isinstance(redelivery_spent_at, str)
          or (redelivery_outcome is not None
              and (not isinstance(redelivery_outcome, str)
                   or redelivery_outcome not in _OPERATOR_GITHUB_REDELIVERY_OUTCOMES))):
        raise ValueError("backup webhook row has invalid operator GitHub redelivery audit")
    else:
        _parse_aware_timestamp(
            redelivery_spent_at, "operator GitHub redelivery spend timestamp")
    if count == 0:
        if (recovery_id is not None or recovered_at is not None
                or batch_size is not None or batch_token is not None):
            raise ValueError("backup webhook row has incoherent operator recovery audit")
        if (continuation_count != 0 or continuation_id is not None
                or continued_at is not None or continuation_sha is not None):
            raise ValueError("backup webhook row has incoherent operator continuation audit")
        return
    if (not isinstance(recovery_id, str)
            or not _OPERATOR_RECOVERY_ID.fullmatch(recovery_id)
            or not isinstance(recovered_at, str)
            or type(batch_size) is not int or not 1 <= batch_size <= 10
            or not isinstance(batch_token, str)
            or not _OPERATOR_RECOVERY_BATCH_TOKEN.fullmatch(batch_token)):
        raise ValueError("backup webhook row has invalid operator recovery audit")
    _parse_aware_timestamp(recovered_at, "operator recovery timestamp")
    if continuation_count == 0:
        if (continuation_id is not None or continued_at is not None
                or continuation_sha is not None):
            raise ValueError("backup webhook row has incoherent operator continuation audit")
    elif (not isinstance(continuation_id, str)
          or not _OPERATOR_RECOVERY_ID.fullmatch(continuation_id)
          or not isinstance(continued_at, str)
          or not isinstance(continuation_sha, str)
          or not _OPERATOR_CONTINUATION_SHA.fullmatch(continuation_sha)):
        raise ValueError("backup webhook row has invalid operator continuation audit")
    else:
        _parse_aware_timestamp(continued_at, "operator continuation timestamp")


def _validate_delivery_coordinates(rec: dict, row_kind: str) -> None:
    account_key = rec.get("account_key")
    repo = rec.get("repo")
    if account_key is not None and (not isinstance(account_key, str) or len(account_key) > 120):
        raise ValueError(f"backup {row_kind} webhook row has invalid account coordinate")
    if repo is not None and (not isinstance(repo, str) or len(repo) > 512):
        raise ValueError(f"backup {row_kind} webhook row has invalid repository coordinate")


def _validate_recovery_done_delivery(rec: dict) -> None:
    """Admit only the completed sibling that preserves an unfinished exact recovery batch's proof."""
    event_type = rec.get("event_type")
    if (rec.get("status") != "done"
            or not isinstance(event_type, str)
            or not (1 <= len(event_type) <= 80)
            or event_type == "erased"):
        raise ValueError("backup contains an invalid recovery completion proof")
    if rec.get("payload") != {}:
        raise ValueError("backup recovery completion proof has a non-empty payload")
    if rec.get("operator_recovery_count") != 1:
        raise ValueError("backup recovery completion proof has no operator recovery audit")

    done_at = _parse_aware_timestamp(rec.get("done_at"), "completion timestamp")
    recovered_at = _parse_aware_timestamp(
        rec.get("operator_recovered_at"), "operator recovery timestamp")
    if done_at < recovered_at:
        raise ValueError("backup recovery completion proof predates its operator recovery")

    if any(rec.get(field) is not None for field in (
        "locked_at", "owner_instance", "last_error", "not_before", "retry_window_expires_at",
    )):
        raise ValueError("backup recovery completion proof carries worker or retry residue")
    if type(rec.get("attempts")) is not int or rec["attempts"] < 0:
        raise ValueError("backup recovery completion proof has invalid attempts")
    _validate_delivery_coordinates(rec, "recovery completion proof")


def _validate_unfinished_delivery(rec: dict) -> None:
    """Require a minimized retryable row; error text and worker-only progress never cross the backup boundary."""
    status = rec.get("status")
    event_type = rec.get("event_type")
    payload = rec.get("payload")
    if status not in ("queued", "processing", "failed"):
        raise ValueError("backup contains a terminal non-erased webhook_delivery")
    if not isinstance(event_type, str) or not (1 <= len(event_type) <= 80) or event_type == "erased":
        raise ValueError("backup contains an invalid unfinished webhook event type")
    if not isinstance(payload, dict):
        raise ValueError("backup contains a non-object unfinished webhook payload")
    if rec.get("done_at") is not None or rec.get("last_error") is not None:
        raise ValueError("backup unfinished webhook row contains terminal/error residue")
    if status != "processing" and rec.get("locked_at") is not None:
        raise ValueError("backup queued/failed webhook row carries a worker lock")
    if type(rec.get("attempts")) is not int or rec["attempts"] < 0:
        raise ValueError("backup unfinished webhook row has invalid attempts")
    if type(rec.get("lease_generation")) is not int or rec["lease_generation"] < 0:
        raise ValueError("backup unfinished webhook row has invalid lease generation")
    retry_expires = rec.get("retry_window_expires_at")
    if retry_expires is not None and not isinstance(retry_expires, str):
        raise ValueError("backup unfinished webhook row has invalid retry-window timestamp")
    if type(rec.get("auto_rearm_count")) is not int or rec["auto_rearm_count"] < 0:
        raise ValueError("backup unfinished webhook row has invalid automatic rearm count")
    _validate_delivery_coordinates(rec, "unfinished")

    # The durable queue stores sanitize_payload() output. Re-sanitizing is a compact, shared allow-list check that
    # rejects bodies/messages and worker-added `_veripsa_*` progress markers. `Revert` is the one intentionally
    # retained title sentinel; make it re-sanitizable without admitting an arbitrary title.
    candidate = copy.deepcopy(payload)
    pr = candidate.get("pull_request") if isinstance(candidate, dict) else None
    if isinstance(pr, dict) and pr.get("title") == "Revert":
        pr["title"] = "Revert placeholder"
    try:
        from delivery_queue import sanitize_payload
    except ImportError:  # package import
        from .delivery_queue import sanitize_payload
    minimized = sanitize_payload(event_type, candidate)
    if _canonical_line(minimized) != _canonical_line(payload):
        raise ValueError("backup unfinished webhook payload is not the minimized durable form")


# LEGACY-BACKUP COMPATIBILITY (Option A, FORMAT_VERSION kept at 2): every column below is a NULLABLE
# ADDITIVE extension appended to core.event AFTER a shipped backup already existed — counterparty_sha /
# fact_fingerprint arrived with the compat-finding lane, fact_class / detector with the S3a taxonomy. A
# backup written before a given column existed simply has no such key; `to_jsonb(row)` on a current DB always
# emits the key (null when unset), so the export side always carries all of them. On the READ side we fill a
# missing additive-nullable column with NULL before exact validation, so the immediately-prior valid backup
# (and every older one) still restores instead of failing `missing=[...]`. This is NOT a blanket relaxation:
# only these named nullable columns are backfilled; any OTHER missing column (a real required field) or any
# genuinely unknown column still fails validation. Adding a new additive-nullable event column in future =
# add it to TABLE_META cols AND here (the schema-manifest drift gate enforces the TABLE_META half).
_LEGACY_OPTIONAL_COLS = {
    "event": ("counterparty_sha", "fact_fingerprint", "fact_class", "detector"),
    # Format-v2 predates the account scheduler cache.  Its absent fields have
    # exactly the same meaning as a current restore: no queue/lease survives,
    # no claim or due pointer is live, and counters start from zero.
    "installation_account": tuple(INSTALLATION_CONVERGENCE_FRESH_DEFAULTS),
    # Shipped format-v2 inbox rows predate the finite retry epoch. Preserve
    # their restoreability without granting poison work a fresh automatic
    # retry: missing count normalizes to the exhausted value (1), never 0.
    "webhook_delivery": (
        "retry_window_expires_at", "auto_rearm_count",
        "operator_recovery_id", "operator_recovered_at",
        "operator_recovery_batch_size", "operator_recovery_batch_token",
        "operator_recovery_count", "operator_continuation_id",
        "operator_continued_at", "operator_continuation_sha",
        "operator_continuation_count", "operator_github_redelivery_delivery_id",
        "operator_github_redelivery_sha", "operator_github_redelivery_spent_at",
        "operator_github_redelivery_outcome", "operator_github_redelivery_count",
    ),
}
_LEGACY_OPTIONAL_DEFAULTS = {
    **{
        ("installation_account", column): value
        for column, value in INSTALLATION_CONVERGENCE_FRESH_DEFAULTS.items()
    },
    ("webhook_delivery", "retry_window_expires_at"): None,
    ("webhook_delivery", "auto_rearm_count"): 1,
    ("webhook_delivery", "operator_recovery_id"): None,
    ("webhook_delivery", "operator_recovered_at"): None,
    ("webhook_delivery", "operator_recovery_batch_size"): None,
    ("webhook_delivery", "operator_recovery_batch_token"): None,
    ("webhook_delivery", "operator_recovery_count"): 0,
    ("webhook_delivery", "operator_continuation_id"): None,
    ("webhook_delivery", "operator_continued_at"): None,
    ("webhook_delivery", "operator_continuation_sha"): None,
    ("webhook_delivery", "operator_continuation_count"): 0,
    ("webhook_delivery", "operator_github_redelivery_delivery_id"): None,
    ("webhook_delivery", "operator_github_redelivery_sha"): None,
    ("webhook_delivery", "operator_github_redelivery_spent_at"): None,
    ("webhook_delivery", "operator_github_redelivery_outcome"): None,
    ("webhook_delivery", "operator_github_redelivery_count"): 0,
}


def _normalize_legacy_record(rec: dict) -> dict:
    """Fill additive-nullable columns absent from a legacy backup record with None (never mutates the input)."""
    table = rec.get("_table")
    optional = _LEGACY_OPTIONAL_COLS.get(table)
    if not optional:
        return rec
    missing = [c for c in optional if c not in rec]
    if not missing:
        return rec
    out = dict(rec)
    for c in missing:
        out[c] = _LEGACY_OPTIONAL_DEFAULTS.get((table, c))
    return out


def _validate_record(rec: dict) -> None:
    table = rec.get("_table")
    if table not in TABLE_META:
        raise ValueError(f"backup contains unknown or missing table {table!r}")
    expected = {"_table", *TABLE_META[table]["cols"]}
    actual = set(rec)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(
            f"backup {table} record shape mismatch (missing={missing}, unknown={unknown})"
        )
    if table == "account_lifecycle_tombstone":
        # Explicit hard erasure removes this account-keyed row. Exporting a legacy/malformed gdpr_erase marker
        # would reintroduce the exact raw account/installation identity the erase promised to remove.
        if rec.get("reason") != "uninstall_purge":
            raise ValueError("backup contains a forbidden hard-erasure account lifecycle marker")
    if table == "webhook_delivery":
        if (not isinstance(rec.get("delivery_key"), str)
                or not (1 <= len(rec["delivery_key"]) <= 200)):
            raise ValueError("backup webhook row has invalid delivery key")
        if not isinstance(rec.get("payload"), dict):
            raise ValueError("backup webhook row has a non-object payload")
        if not isinstance(rec.get("received_at"), str) or not isinstance(rec.get("updated_at"), str):
            raise ValueError("backup webhook row has invalid durable timestamps")
        if type(rec.get("lease_generation")) is not int or rec["lease_generation"] < 0:
            raise ValueError("backup webhook row has invalid lease generation")
        if type(rec.get("causal_order_version")) is not int or rec["causal_order_version"] < 0:
            raise ValueError("backup webhook row has invalid causal-order version")
        if type(rec.get("auto_rearm_count")) is not int or rec["auto_rearm_count"] < 0:
            raise ValueError("backup webhook row has invalid automatic rearm count")
        _validate_operator_recovery_audit(rec)
        if (rec.get("retry_window_expires_at") is not None
                and not isinstance(rec.get("retry_window_expires_at"), str)):
            raise ValueError("backup webhook row has invalid retry-window timestamp")
        if rec.get("event_type") == "erased":
            _validate_erased_receipt(rec)
        elif rec.get("status") == "done":
            _validate_recovery_done_delivery(rec)
        else:
            _validate_unfinished_delivery(rec)


@contextmanager
def _gate_cursor(dsn: str):
    """Open a bounded client-side named cursor over the cross-tenant SECURITY DEFINER gate."""
    conn = _connect(dsn)
    try:
        conn.set_session(readonly=True)
        with conn:
            with conn.cursor() as setup:
                setup.execute("SET search_path=core")
            with conn.cursor(name="veripsa_durable_export") as cur:
                cur.itersize = FETCH_BATCH
                cur.execute("SELECT core.export_durable_rows_with_authority('')")
                yield cur
    finally:
        conn.close()


def _iter_cursor(cur):
    while True:
        batch = cur.fetchmany(FETCH_BATCH)
        if not batch:
            return
        for (rec,) in batch:
            yield rec


def export(dsn: str, out=sys.stdout) -> dict:
    """Stream a complete manifest + records + verified footer. Invalid DB rows are never written as records."""
    counts = _empty_counts()
    table_counts = _empty_table_counts()
    digest = hashlib.sha256()
    total = 0
    seen = _SeenKeys()
    out.write(_canonical_line(_manifest()) + "\n")
    try:
        with _gate_cursor(dsn) as cur:
            for rec in _iter_cursor(cur):
                _validate_record(rec)  # validate privacy + exact schema before this record reaches `out`
                seen.add(rec)
                _count_record(counts, rec)
                table_counts[rec["_table"]] += 1
                total += 1
                line = _canonical_line(rec) + "\n"
                digest.update(line.encode("utf-8"))
                out.write(line)
    finally:
        seen.close()
    out.write(_canonical_line(_footer(total, table_counts, counts, digest.hexdigest())) + "\n")
    return counts


def count(dsn: str) -> dict:
    """Cross-tenant row counts of the durable tables (no data leaves) — a cheap pre/post-restore reconciliation
    check. Counts via the same DR gate so it sees exactly what the export would carry."""
    counts = _empty_counts()
    seen = _SeenKeys()
    try:
        with _gate_cursor(dsn) as cur:
            for rec in _iter_cursor(cur):
                _validate_record(rec)
                seen.add(rec)
                _count_record(counts, rec)
    finally:
        seen.close()
    return counts


class _StagedBackup:
    def __init__(self, files: dict, records: int, table_counts: dict, durable_counts: dict, digest: str):
        self.files = files
        self.records_total = records
        self.table_counts = table_counts
        self.durable_counts = durable_counts
        self.digest = digest

    def records(self, table: str):
        stream = self.files[table]
        stream.seek(0)
        for line_no, line in enumerate(stream, 1):
            if line.strip():
                yield _load_json(line, line_no)


def _validate_footer(rec: dict, records: int, table_counts: dict, durable_counts: dict, digest: str) -> None:
    expected_keys = {
        "_backup", "format", "version", "records", "table_counts", "durable_counts", "sha256"
    }
    if set(rec) != expected_keys or rec.get("_backup") != "footer":
        raise ValueError("backup footer shape is invalid")
    if rec.get("format") != FORMAT_NAME or rec.get("version") != FORMAT_VERSION:
        raise ValueError("backup footer format/version is unsupported")
    if type(rec.get("records")) is not int or rec["records"] < 0:
        raise ValueError("backup footer record count is invalid")
    if rec["records"] != records:
        raise ValueError("backup footer record count mismatch (truncated or edited backup)")
    if rec.get("table_counts") != table_counts:
        raise ValueError("backup footer per-table counts mismatch (truncated or edited backup)")
    if rec.get("durable_counts") != durable_counts:
        raise ValueError("backup footer durable counts mismatch (truncated or edited backup)")
    if rec.get("sha256") != digest:
        raise ValueError("backup footer SHA-256 mismatch (corrupt or edited backup)")


@contextmanager
def _stage_backup(lines):
    """Validate the whole envelope into owner-only temporary streams before any database is opened."""
    files = {
        table: tempfile.TemporaryFile(mode="w+t", encoding="utf-8", newline="\n")
        for table in RESTORE_ORDER
    }
    seen = _SeenKeys()
    table_counts = _empty_table_counts()
    durable_counts = _empty_counts()
    digest = hashlib.sha256()
    records = 0
    got_manifest = False
    got_footer = False
    footer = None
    try:
        for line_no, raw_line in enumerate(lines, 1):
            line = raw_line.strip()
            if not line:
                continue
            rec = _load_json(line, line_no)
            if not got_manifest:
                if rec != _manifest():
                    raise ValueError("backup must begin with the exact supported versioned manifest")
                got_manifest = True
                continue
            if got_footer:
                raise ValueError("backup contains data after its footer")
            if rec.get("_backup") == "footer":
                footer = rec
                got_footer = True
                continue
            if "_backup" in rec:
                raise ValueError(f"backup contains unknown envelope record {rec.get('_backup')!r}")
            # The footer authenticates the record shape that the EXPORTER actually wrote.  A legacy
            # export therefore hashed the pre-extension event object (without later additive-nullable
            # keys).  Normalize only for current-schema validation/staging; hashing the normalized row
            # would rewrite history and make every genuine legacy footer fail verification.
            digest_canonical = _canonical_line(rec) + "\n"
            rec = _normalize_legacy_record(rec)
            _validate_record(rec)
            seen.add(rec)
            canonical = _canonical_line(rec) + "\n"
            digest.update(digest_canonical.encode("utf-8"))
            files[rec["_table"]].write(canonical)
            table_counts[rec["_table"]] += 1
            _count_record(durable_counts, rec)
            records += 1
        if not got_manifest:
            raise ValueError("backup manifest is missing")
        if not got_footer or footer is None:
            raise ValueError("backup footer is missing (truncated backup)")
        _validate_footer(footer, records, table_counts, durable_counts, digest.hexdigest())
        for stream in files.values():
            stream.flush()
        yield _StagedBackup(files, records, table_counts, durable_counts, digest.hexdigest())
    finally:
        seen.close()
        for stream in files.values():
            stream.close()


def verify(lines) -> dict:
    """Verify an export without connecting to Postgres. Returns only envelope metadata/counts."""
    with _stage_backup(lines) as staged:
        return {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "records": staged.records_total,
            "table_counts": dict(staged.table_counts),
            "durable_counts": dict(staged.durable_counts),
            "sha256": staged.digest,
        }


def load_verified_records(lines) -> list[dict]:
    """Small-backup/test helper. Production restore streams from the staged per-table files."""
    with _stage_backup(lines) as staged:
        return [rec for table in RESTORE_ORDER for rec in staged.records(table)]


def _restore_record(rec: dict) -> dict:
    if rec["_table"] == "installation_account":
        # policy_refresh_outbox and graph_convergence_lease are rebuildable
        # operational state and intentionally absent from RESTORE_ORDER.
        # Therefore every derived router pointer/claim/counter must be fresh,
        # even when the source export captured an active production turn.
        rec = dict(rec)
        rec.update(INSTALLATION_CONVERGENCE_FRESH_DEFAULTS)
    if rec["_table"] == "webhook_delivery" and rec.get("status") == "processing":
        # No worker/lease survives a database restore. Requeue the work immediately under its preserved monotonic
        # generation; a new worker must claim a new lease before it can finish/release it.
        rec = dict(rec)
        rec.update(status="queued", locked_at=None, done_at=None, last_error=None, not_before=None,
                   owner_instance=None)
    return rec


def _db_value(table: str, column: str, value):
    return _canonical_line(value) if table == "webhook_delivery" and column == "payload" else value


def _insert_or_assert_identical(cur, table: str, rec: dict) -> None:
    meta = TABLE_META[table]
    cols = meta["cols"]
    pk_cols = meta["pk_cols"]
    placeholders = ["%s::jsonb" if table == "webhook_delivery" and col == "payload" else "%s" for col in cols]
    values = [_db_value(table, col, rec.get(col)) for col in cols]
    cur.execute(
        f"INSERT INTO core.{table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT ({', '.join(pk_cols)}) DO NOTHING RETURNING 1",
        values,
    )
    if cur.fetchone() is not None:
        return

    where_pk = " AND ".join(f"{col}=%s" for col in pk_cols)
    comparisons = []
    compare_values = []
    for col in cols:
        placeholder = "%s::jsonb" if table == "webhook_delivery" and col == "payload" else "%s"
        comparisons.append(f"{col} IS NOT DISTINCT FROM {placeholder}")
        compare_values.append(_db_value(table, col, rec.get(col)))
    cur.execute(
        f"SELECT 1 FROM core.{table} WHERE {where_pk} AND {' AND '.join(comparisons)}",
        [rec.get(col) for col in pk_cols] + compare_values,
    )
    if cur.fetchone() is None:
        raise ValueError(f"restore conflict: existing core.{table} primary key is not byte-identical")


def import_jsonl(dsn: str, lines) -> dict:
    """Validate completely, then atomically restore as owner; conflicting existing rows abort the transaction."""
    with _stage_backup(lines) as staged:  # validation completes before `_connect` is reachable
        conn = _connect(dsn)
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SET search_path=core")
                    for table in RESTORE_ORDER:
                        meta = TABLE_META[table]
                        for source_rec in staged.records(table):
                            rec = _restore_record(source_rec)
                            if meta["needs_acct"]:
                                cur.execute(
                                    "SELECT set_config('core.current_account', %s, true)",
                                    (rec.get("account_id"),),
                                )
                            if meta["forgery"]:
                                cur.execute("SELECT core.mark_governed_write(%s)", (table,))
                            _insert_or_assert_identical(cur, table, rec)
        finally:
            conn.close()
        return dict(staged.durable_counts)


def diagnose(dsn: str, repo: str | None, account: str | None, delivery: str | None, out=sys.stdout) -> dict:
    """INCIDENT DIAGNOSIS — "no Veripsa check appeared on my PR": trace what the ledger DID record for a repo.

    An exact delivery lookup is owner-only and returns a deliberately closed set of status/counter fields. It
    never prints the supplied delivery key, payload, error, tenant, repository, or timestamps. Without a delivery
    key, the existing cross-tenant DR gate provides recent content-free ledger coordinates for a repo. Read-only."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            if delivery:
                cur.execute("SELECT current_user")
                if cur.fetchone()[0] != "veripsa_migrator":
                    raise PermissionError(
                        "diagnose --delivery requires veripsa_migrator OWNER_DSN")
                cur.execute(
                    "SELECT status,attempts,"
                    " (locked_at IS NOT NULL OR owner_instance IS NOT NULL) AS locked,"
                    " (not_before IS NULL OR not_before<=clock_timestamp()) AS due,"
                    " auto_rearm_count,operator_recovery_count,operator_continuation_count"
                    " FROM core.webhook_delivery WHERE delivery_key=%s",
                    (delivery,),
                )
                row = cur.fetchone()
                if row is None:
                    result = {"present": False}
                    out.write("# diagnose delivery=absent\n")
                    return result
                stored_status = row[0] if row[0] in {
                    "queued", "processing", "done", "failed"
                } else "unknown"
                result = {
                    "present": True,
                    "status": stored_status,
                    "attempts": max(int(row[1] or 0), 0),
                    "locked": bool(row[2]),
                    "due": bool(row[3]),
                    "auto_rearm_count": max(int(row[4] or 0), 0),
                    "operator_recovery_count": max(int(row[5] or 0), 0),
                    "operator_continuation_count": max(int(row[6] or 0), 0),
                }
                out.write(
                    "# diagnose delivery=present"
                    f" status={result['status']} attempts={result['attempts']}"
                    f" locked={str(result['locked']).lower()}"
                    f" due={str(result['due']).lower()}"
                    f" auto_rearm_count={result['auto_rearm_count']}"
                    f" operator_recovery_count={result['operator_recovery_count']}"
                    f" operator_continuation_count={result['operator_continuation_count']}\n"
                )
                return result
            # Read the durable event rows cross-tenant via the SAME moat-correct DR gate, then filter in Python
            # (the gate enumerates tenants; a client can't read event cross-account directly under FORCE RLS).
            cur.execute("SELECT core.export_durable_rows_with_authority(%s)", (account or "",))
            events = []
            for (rec,) in cur.fetchall():
                if rec.get("_table") != "event":
                    continue
                if repo and rec.get("repo") != repo:
                    continue
                events.append(rec)
            events.sort(key=lambda e: e.get("occurred_at") or "", reverse=True)
            events = events[:50]
            out.write(f"# diagnose repo={repo or '*'} account={account or '*'}\n")
            if not events:
                out.write("# NO events recorded for this repo/account. Either the webhook never arrived "
                          "(check the App -> Advanced -> Deliveries), it was rejected (401 bad signature / "
                          "413 oversize), or the worker failed before recording an event.\n")
            else:
                out.write(f"# {len(events)} recent event(s) (newest first):\n")
                for e in events:
                    sha = (e.get("commit_sha") or "")[:7]
                    out.write(f"{e.get('occurred_at')}  {e.get('kind',''):16} "
                              f"{e.get('repo','')}@{e.get('branch','')} {sha} {e.get('detail') or ''}\n".rstrip() + "\n")
            return {"events": len(events)}
    finally:
        conn.close()


def main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else "count"
    if cmd == "verify":
        if len(argv) != 3:
            print("backup_export: verify requires one JSONL path (or - for stdin)", file=sys.stderr)
            return 2
        stream = sys.stdin if argv[2] == "-" else open(argv[2], encoding="utf-8")
        try:
            print(json.dumps(verify(stream), sort_keys=True))
        finally:
            if stream is not sys.stdin:
                stream.close()
        return 0
    if cmd == "restore":
        if len(argv) != 3:
            print("backup_export: restore requires one verified JSONL path", file=sys.stderr)
            return 2
        owner_dsn = os.environ.get("OWNER_DSN") or os.environ.get("VERIPSA_DSN")
        if not owner_dsn:
            print("backup_export: restore requires OWNER_DSN", file=sys.stderr)
            return 2
        with open(argv[2], encoding="utf-8") as stream:
            print(json.dumps(import_jsonl(owner_dsn, stream), sort_keys=True))
        return 0

    if cmd == "diagnose":
        repo = account = delivery = None
        for i, a in enumerate(argv):
            if a == "--repo" and i + 1 < len(argv):
                repo = argv[i + 1]
            elif a == "--account" and i + 1 < len(argv):
                account = argv[i + 1]
            elif a == "--delivery" and i + 1 < len(argv):
                delivery = argv[i + 1]
        if delivery:
            owner_dsn = os.environ.get("OWNER_DSN")
            if not owner_dsn:
                print("backup_export: diagnose --delivery requires OWNER_DSN", file=sys.stderr)
                return 2
            try:
                diagnose(owner_dsn, repo, account, delivery)
            except PermissionError:
                print(
                    "backup_export: diagnose --delivery requires veripsa_migrator OWNER_DSN",
                    file=sys.stderr,
                )
                return 2
            return 0
        dsn = (os.environ.get("BACKUP_DSN") or os.environ.get("OWNER_DSN")
               or os.environ.get("VERIPSA_DSN"))
        if not dsn:
            print("backup_export: diagnose requires BACKUP_DSN or OWNER_DSN", file=sys.stderr)
            return 2
        diagnose(dsn, repo, account, None)
        return 0

    dsn = (os.environ.get("BACKUP_DSN") or os.environ.get("OWNER_DSN")
           or os.environ.get("VERIPSA_DSN"))
    if not dsn:
        print("backup_export: set BACKUP_DSN (OWNER_DSN is an administrative fallback)", file=sys.stderr)
        return 2
    if cmd == "export":
        res = export(dsn)
        print("# exported " + " + ".join(f"{n} {key}" for key, n in res.items()), file=sys.stderr)
        return 0
    if cmd == "count":
        print(json.dumps(count(dsn)))
        return 0
    print(f"backup_export: unknown command {cmd!r} (export | count | verify | restore | diagnose)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
