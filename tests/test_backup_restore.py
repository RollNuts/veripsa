#!/usr/bin/env python3
"""Backup-restore round-trip gate — the DR claim, PROVEN (not just documented).

github-app/backup_export.py's module docstring states the restore is "documented + tested in
tests/test_backup_restore.py … the test proves an export → fresh DB → import round-trips the durable rows
byte-for-byte." That file did not exist — the catastrophic-to-lose half of disaster recovery (the EVENT
ledger + the STATEMENT stream, incl. the NEW 'prediction'/'advice_outcome' answer-check kinds) was asserted
only in prose. A backup nobody has ever RESTORED is a hope, not a plan. This gate makes it a plan.

WHAT IT PROVES (export from a populated SRC DB → import_jsonl into a freshly-bootstrapped EMPTY DST DB):
  • LOSSLESS per KIND: every durable event kind (landed/push + warn_issued/collision_held + the answer-check
    'prediction'/'advice_outcome') and every statement re-appears in DST with identical counts and identical
    per-row column values — a true round-trip, not just a count match.
  • CONTENT-FREE: the exported JSONL carries only the schema's content-free columns (no bodies); we assert the
    exported event objects expose exactly the documented column set and nothing more.
  • RLS-PINNED / NO CROSS-TENANT BLEED: three tenants are seeded; the restore re-pins RLS per row's account, and
    a per-tenant read of DST returns exactly that tenant's rows (the other tenant's rows never leak in).
  • NO ORPHAN: account + agent + credential + installation registries are restored before child ledger rows.
    Both GitHub-routed tenants and a credential-only local/API tenant remain discoverable on a SECOND export.
    The current GitHub installation generation also survives, so a delayed delete cannot be mistaken for the
    restored replacement generation.
  • INTEGRITY: a versioned manifest + footer counts + SHA-256 reject truncation, unknown/missing/duplicate rows,
    duplicate JSON keys, and unsupported versions before any database connection is opened.
  • IDEMPOTENT WITHOUT CONCEALMENT: a second identical import is safe; a same-PK/different-value row aborts the
    transaction instead of being silently swallowed by ON CONFLICT DO NOTHING.
  • EMPTY-DB edge: a genuinely empty freshly migrated DB exports a valid zero-record envelope.
  • LIFECYCLE/BILLING/INBOX FENCES: retained uninstall + repository tombstones/activations, plan-event high-water
    marks, scrubbed erased receipts, sanitized unfinished deliveries, and the completed sibling of an unfinished
    operator-recovery batch round-trip. A processing row restores queued; the restored exact set can perform its
    one forward continuation; an explicit hard erase leaves no raw account/install id in its backup boundary.
  • ERASURE SCOPE: lifecycle fence rows are emitted only by the host-wide `p_account=''` DR export. A scoped
    export for a hard-erased account remains exactly empty and cannot expose the cross-tenant fence registry.

Run:  python3 tests/test_backup_restore.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import backup_export  # noqa: E402

# PROCESS-UNIQUE pair (parallel-safe; bootstrap drops+recreates these): a SRC we populate + a DST we restore
# into. Same per-PID discipline as db/smoke.sh / test_retention.py so concurrent gate runs never collide.
SRC = "veripsa_backupsrc_" + str(os.getpid())
DST = "veripsa_backuprestore_" + str(os.getpid())

# the documented content-free column set of an exported event object (TABLE_META['event']['cols'] + _table).
EVENT_COLS = set(backup_export.TABLE_META["event"]["cols"]) | {"_table"}
TOMBSTONE_COLS = set(backup_export.TABLE_META["account_lifecycle_tombstone"]["cols"]) | {"_table"}
ERASED_RECEIPT_COLS = set(backup_export.TABLE_META["webhook_delivery"]["cols"]) | {"_table"}
RECOVERY_DONE_GUID = "00000000-0000-4000-8000-000000000007"
RECOVERY_FAILED_GUID = "00000000-0000-4000-8000-000000000008"
RECOVERY_BATCH_TOKEN = "00000000-0000-4000-8000-000000000011"
RECOVERY_ID = "dr-operator-recovery-partial"
CONTINUATION_ID = "dr-operator-continuation-after-restore"
CONTINUATION_SHA = "a" * 40


def mig(db):
    return psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{db}")


def dsn(db):
    return f"postgresql://veripsa_migrator@localhost/{db}"


def boot(db):
    r = subprocess.run(["bash", "db/bootstrap_local.sh", db], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"bootstrap {db} failed:\n", r.stderr[-800:])
        return False
    return True


def clear_bootstrap_registry(db):
    """Make DST genuinely empty; strict restore correctly rejects non-identical bootstrap fixture collisions."""
    conn = mig(db)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account','ACCT-DEMO',true)")
            cur.execute("DELETE FROM core.credential WHERE account_id='ACCT-DEMO'")
            cur.execute("DELETE FROM core.installation_account WHERE account_id='ACCT-DEMO'")
            cur.execute("DELETE FROM core.agent WHERE account_id='ACCT-DEMO'")
            cur.execute("DELETE FROM core.account WHERE account_id='ACCT-DEMO'")
    finally:
        conn.close()


def manifest_drift(db):
    """Every column of every to_jsonb-exported durable table MUST be in its TABLE_META manifest, and vice
    versa. The SQL export dumps to_jsonb(row) (ALL columns) and _validate_record requires an EXACT set match,
    so a column added to a durable table without a matching manifest entry silently breaks DR export/restore
    (issue #844 first, the S3a fact_class/detector regression second). Returns {table: (db_only, manifest_only)}
    for any table that drifts; empty dict = clean."""
    conn = mig(db)
    drift = {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            for table, meta in backup_export.TABLE_META.items():
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='core' AND table_name=%s", (table,))
                db_cols = {r[0] for r in cur.fetchall()}
                manifest_cols = set(meta["cols"])
                db_only = db_cols - manifest_cols
                manifest_only = manifest_cols - db_cols
                if db_only or manifest_only:
                    drift[table] = (sorted(db_only), sorted(manifest_only))
    finally:
        conn.close()
    return drift


def events_by_kind(db, account):
    """Owner-read of DST, RLS-pinned to `account`: kind -> count (FORCE RLS walls the owner to this account)."""
    conn = mig(db)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cur.execute("SELECT kind, count(*)::int FROM core.event GROUP BY kind ORDER BY kind")
            return dict(cur.fetchall())
    finally:
        conn.close()


def event_rows(db, account):
    """Owner-read full event rows for `account` as {event_id: row-dict} for a byte-for-byte column compare."""
    conn = mig(db)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cols = backup_export.TABLE_META["event"]["cols"]
            cur.execute(f"SELECT {', '.join(cols)} FROM core.event")
            out = {}
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                d = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()}
                out[d["event_id"]] = d
            return out
    finally:
        conn.close()


def table_rows(db, account, table):
    """Exact selected-column rows, normalized through PostgreSQL JSON encoding."""
    conn = mig(db)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cols = backup_export.TABLE_META[table]["cols"]
            cur.execute(
                f"SELECT jsonb_build_object({', '.join(repr(c) + ', ' + c for c in cols)}) "
                f"FROM core.{table} ORDER BY {', '.join(backup_export.TABLE_META[table]['pk_cols'])}"
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def installation_revoked(db, installation_id):
    conn = mig(db)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT revoked_at IS NOT NULL FROM core.installation_account WHERE installation_id=%s",
                        (installation_id,))
            row = cur.fetchone()
            return bool(row and row[0])
    finally:
        conn.close()


def installation_generation(db, installation_id):
    conn = mig(db)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT github_installation_id FROM core.installation_account WHERE installation_id=%s",
                        (installation_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def lifecycle_fence_rows(db):
    """Full content-free fence rows for an exact SRC↔DST comparison."""
    conn = mig(db)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT to_jsonb(t) FROM core.account_lifecycle_tombstone t ORDER BY account_id")
            tombstones = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT to_jsonb(d) FROM core.webhook_delivery d "
                        "WHERE event_type='erased' ORDER BY delivery_key")
            receipts = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT to_jsonb(t) FROM core.repository_lifecycle_tombstone t "
                        "ORDER BY account_id,repository_id,repo")
            repo_tombstones = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT account_id FROM core.installation_account UNION SELECT account_id FROM core.credential")
            activation_accounts = [row[0] for row in cur.fetchall()]
            repo_activations = []
            for account in activation_accounts:
                cur.execute("SELECT set_config('core.current_account',%s,true)", (account,))
                cur.execute("SELECT to_jsonb(a) FROM core.repository_lifecycle_activation a "
                            "ORDER BY account_id,repository_id")
                repo_activations.extend(row[0] for row in cur.fetchall())
            repo_activations.sort(key=lambda row: (row["account_id"], row["repository_id"]))
            cur.execute("SELECT to_jsonb(p) FROM core.account_plan_event p ORDER BY account_id")
            plan_events = [row[0] for row in cur.fetchall()]
            return tombstones, receipts, repo_tombstones, repo_activations, plan_events
    finally:
        conn.close()


def scoped_export_rows(db, account):
    conn = mig(db)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.export_durable_rows_with_authority(%s)", (account,))
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def identity_registry_rows(db, account):
    """Exact account/agent/credential/installation registry for a route-discovery round-trip."""
    conn = mig(db)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cur.execute("SELECT to_jsonb(a) FROM core.account a WHERE account_id=%s", (account,))
            accounts = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT to_jsonb(a) FROM core.agent a WHERE account_id=%s ORDER BY agent_id", (account,))
            agents = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT to_jsonb(c) FROM core.credential c WHERE account_id=%s ORDER BY role_name", (account,))
            credentials = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT to_jsonb(i) FROM core.installation_account i WHERE account_id=%s "
                        "ORDER BY installation_id", (account,))
            return {
                "accounts": accounts,
                "agents": agents,
                "credentials": credentials,
                "installations": [row[0] for row in cur.fetchall()],
            }
    finally:
        conn.close()


def canonical_rows(rows):
    return sorted(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows)


def main() -> int:
    if not boot(SRC) or not boot(DST):
        return 1
    clear_bootstrap_registry(DST)
    checks = []

    # PostgreSQL trims trailing fractional zeroes, so valid audited timestamps
    # can have any precision from one through six digits.  Python 3.9's
    # fromisoformat accepts only three or six; the backup validator must remain
    # portable because release/DR gates run on the macOS system interpreter.
    observed_five_digit_timestamp = "2026-08-04T03:17:04.30789+09:00"
    valid_timestamp_matrix = (
        ("2026-08-04T03:17:04+09:00", 0),
        ("2026-08-04T03:17:04.3+09:00", 300000),
        ("2026-08-04T03:17:04.30+09:00", 300000),
        ("2026-08-04T03:17:04.307+09:00", 307000),
        ("2026-08-04T03:17:04.3078+09:00", 307800),
        (observed_five_digit_timestamp, 307890),
        ("2026-08-04T03:17:04.307891Z", 307891),
        ("2026-08-04T03:17:04.1-23:59", 100000),
    )
    parsed_timestamp_matrix = [
        backup_export._parse_aware_timestamp(
            timestamp, "operator recovery timestamp")
        for timestamp, _microsecond in valid_timestamp_matrix
    ]
    parsed_five_digit_timestamp = parsed_timestamp_matrix[5]
    checks.append((
        "valid 0-6 digit RFC3339/PostgreSQL audit timestamps parse portably",
        backup_export._normalize_iso8601_for_fromisoformat(
            observed_five_digit_timestamp
        ) == "2026-08-04T03:17:04.307890+09:00"
        and all(
            parsed.microsecond == expected_microsecond
            for parsed, (_timestamp, expected_microsecond) in zip(
                parsed_timestamp_matrix, valid_timestamp_matrix)
        )
        and parsed_five_digit_timestamp.microsecond == 307890
        and parsed_five_digit_timestamp.utcoffset().total_seconds() == 9 * 3600,
    ))
    invalid_timestamp_matrix = (
        None,
        123,
        "",
        "2026-08-04T03:17:04",
        "2026-08-04T03:17:04.+09:00",
        "2026-08-04T03:17:04.3078901+09:00",
        "2026-02-30T03:17:04+09:00",
        "2026-08-04T03:17:04+24:00",
        "2026-08-04T03:17:04.30789+00:60",
        "2026-08-04T03:17:04.30789-23:60",
        "2026-08-04 03:17:04+09:00",
        "2026-08-04T03:17:04z",
        "2026-08-04T03:17:04+09:00junk",
    )
    invalid_timestamp_refusals = []
    for timestamp in invalid_timestamp_matrix:
        try:
            backup_export._parse_aware_timestamp(
                timestamp, "operator recovery timestamp")
        except ValueError:
            invalid_timestamp_refusals.append(True)
        else:
            invalid_timestamp_refusals.append(False)
    checks.append((
        "backup timestamp parser rejects malformed, naive, and unpreservable values",
        invalid_timestamp_refusals == [True] * len(invalid_timestamp_matrix),
    ))

    # ── EMPTY-DB edge: a genuinely empty freshly migrated schema emits only a valid empty envelope.
    empty_buf = io.StringIO()
    empty_res = backup_export.export(dsn(DST), out=empty_buf)
    empty_lines = empty_buf.getvalue().splitlines()
    empty_meta = backup_export.verify(empty_lines)
    empty_recs = backup_export.load_verified_records(empty_lines)
    empty_registry_tables = {rec.get("_table") for rec in empty_recs}
    checks.append((f"fresh empty schema exports a valid manifest/footer with ZERO records "
                   f"(counts={empty_res}, envelope_records={empty_meta['records']})",
                   all(n == 0 for n in empty_res.values())
                   and empty_meta["records"] == 0 and empty_registry_tables == set()))

    # ── SCHEMA-MANIFEST DRIFT: every durable table's live columns must exactly match its TABLE_META manifest.
    # A column added without updating the manifest fails DR export/restore (this regression class has now bitten
    # twice: #844, then S3a's fact_class/detector). This assertion turns that from a silent prod-DR break into a
    # red release gate BEFORE deploy.
    drift = manifest_drift(DST)
    checks.append((f"backup manifest matches live durable columns for every exported table (drift={drift})",
                   drift == {}))

    # ── LEGACY-BACKUP COMPATIBILITY (Option A): a pre-S3a event record (no fact_class/detector — and, older
    # still, no counterparty_sha/fact_fingerprint) must normalize + validate, so the immediately-prior valid
    # backup restores. And this must NOT be a blanket relaxation: a missing REQUIRED column or an unknown column
    # still fails.
    legacy_event = {"_table": "event", "event_id": "EV-LEGACY", "account_id": "ACCT-ACME", "kind": "landed",
                    "agent_id": None, "counterparty_agent": None, "repo": "acme/app", "branch": "main",
                    "path": None, "commit_sha": None, "model": None, "detail": None,
                    "occurred_at": "2026-07-01T00:00:00+00:00", "visibility": "private"}
    legacy_ok = False
    try:
        backup_export._validate_record(backup_export._normalize_legacy_record(legacy_event))
        legacy_ok = True
    except ValueError:
        legacy_ok = False
    checks.append(("pre-S3a legacy event record (missing 4 additive-nullable cols) normalizes and validates",
                   legacy_ok))

    # Exercise the COMPLETE legacy envelope, not only record-shape validation. A real pre-S3a footer hashes
    # exactly the old event object without the four later nullable keys. Normalizing before digesting rewrites
    # that byte stream and rejects every genuine legacy backup with a footer SHA mismatch.
    legacy_table_counts = backup_export._empty_table_counts()
    legacy_table_counts["event"] = 1
    legacy_durable_counts = backup_export._empty_counts()
    legacy_durable_counts["events"] = 1
    legacy_line = backup_export._canonical_line(legacy_event) + "\n"
    legacy_digest = hashlib.sha256(legacy_line.encode("utf-8")).hexdigest()
    legacy_envelope = [
        backup_export._canonical_line(backup_export._manifest()),
        backup_export._canonical_line(legacy_event),
        backup_export._canonical_line(backup_export._footer(
            1, legacy_table_counts, legacy_durable_counts, legacy_digest)),
    ]
    legacy_envelope_ok = False
    legacy_staged = []
    try:
        legacy_meta = backup_export.verify(legacy_envelope)
        legacy_staged = backup_export.load_verified_records(legacy_envelope)
        legacy_envelope_ok = (
            legacy_meta["records"] == 1
            and legacy_meta["table_counts"]["event"] == 1
            and legacy_meta["durable_counts"]["events"] == 1
            and len(legacy_staged) == 1
            and all(legacy_staged[0].get(c) is None
                    for c in backup_export._LEGACY_OPTIONAL_COLS["event"])
        )
    except ValueError:
        legacy_envelope_ok = False
    checks.append(("real pre-S3a envelope preserves its original footer digest and stages normalized event keys",
                   legacy_envelope_ok))

    missing_required = {k: v for k, v in legacy_event.items() if k != "event_id"}
    req_refused = False
    try:
        backup_export._validate_record(backup_export._normalize_legacy_record(missing_required))
    except ValueError:
        req_refused = True
    checks.append(("a missing REQUIRED column (event_id) is still refused — normalization is not a blanket pass",
                   req_refused))

    unknown_col = {**legacy_event, "surprise_col": "x"}
    unknown_refused = False
    try:
        backup_export._validate_record(backup_export._normalize_legacy_record(unknown_col))
    except ValueError:
        unknown_refused = True
    checks.append(("an unknown column is still refused after normalization",
                   unknown_refused))

    # Shipped format-v2 installation rows predate the convergence scheduler
    # cache.  The cache's queue and lease tables are intentionally outside DR,
    # so both a legacy omission and a current live snapshot must restore to the
    # same fresh operational state.
    legacy_installation = {
        "_table": "installation_account",
        **{column: None for column in backup_export.TABLE_META["installation_account"]["cols"]},
        "installation_id": "legacy-v2-install",
        "account_id": "ACCT-LEGACY",
    }
    for column in backup_export.INSTALLATION_CONVERGENCE_FRESH_DEFAULTS:
        legacy_installation.pop(column)
    legacy_installation_ok = False
    try:
        normalized_installation = backup_export._normalize_legacy_record(legacy_installation)
        backup_export._validate_record(normalized_installation)
        legacy_installation_ok = all(
            normalized_installation[column] == value
            for column, value
            in backup_export.INSTALLATION_CONVERGENCE_FRESH_DEFAULTS.items()
        )
    except ValueError:
        pass
    checks.append((
        "legacy format-v2 installation rows gain only fresh convergence operational defaults",
        legacy_installation_ok,
    ))

    live_installation = {
        "_table": "installation_account",
        **{column: None for column in backup_export.TABLE_META["installation_account"]["cols"]},
        "installation_id": "legacy-v2-install",
        "account_id": "ACCT-LEGACY",
    }
    live_installation.update(
        policy_refresh_due_at="2026-01-01T00:00:00+00:00",
        graph_refresh_due_at="2026-01-01T00:00:00+00:00",
        legacy_graph_refresh_due_at="2026-01-01T00:00:00+00:00",
        convergence_claimed_until="2026-01-01T00:05:00+00:00",
        convergence_claimed_by="lost-worker",
        convergence_claim_epoch=91,
        convergence_graph_claim_count=1,
        convergence_graph_reclaim_at="2026-01-01T00:05:00+00:00",
        convergence_pending_count=8,
        convergence_retry_exhausted_count=2,
        convergence_quota_deferred_count=3,
        convergence_stall_started_at="2026-01-01T00:00:00+00:00",
        convergence_schema_version=1,
        convergence_next_epoch=91,
    )
    restored_installation = backup_export._restore_record(live_installation)
    checks.append((
        "restore discards source due/claim/counters whose outbox and leases are excluded from DR",
        restored_installation["installation_id"] == "legacy-v2-install"
        and restored_installation["account_id"] == "ACCT-LEGACY"
        and all(
            restored_installation[column] == value
            for column, value
            in backup_export.INSTALLATION_CONVERGENCE_FRESH_DEFAULTS.items()
        ),
    ))

    # ── seed SRC: two GitHub-routed tenants plus one credential-only local/API tenant. Every identity source
    # must be enumerated or the third tenant's irreplaceable ledger silently disappears from the host-wide backup.
    seed = """
    SET search_path=core;
    -- ACCT-DEMO is provisioned by bootstrap. ACCT-ACME is GitHub-routed; ACCT-LOCAL intentionally has a
    -- credential but NO installation row, matching local/API seats that the old installation-only export lost.
    SELECT core.provision_seat('ACCT-ACME','Acme Inc','AG-ACME','acme-writer','veripsa_acme_agent');
    SELECT core.provision_seat('ACCT-LOCAL','Local API','AG-LOCAL','local-writer','veripsa_dr_local_agent');
    INSERT INTO core.installation_account(installation_id, account_id) VALUES
      ('inst-demo','ACCT-DEMO'),('inst-acme','ACCT-ACME') ON CONFLICT (installation_id) DO NOTHING;
    UPDATE core.installation_account
       SET github_installation_id = CASE installation_id
             WHEN 'inst-demo' THEN 'GH-INSTALL-GEN-DEMO-B'
             WHEN 'inst-acme' THEN 'GH-INSTALL-GEN-ACME-A' END,
           github_installation_created_at = CASE installation_id
             WHEN 'inst-demo' THEN now() - interval '30 days'
             WHEN 'inst-acme' THEN now() - interval '60 days' END,
           account_login = CASE installation_id WHEN 'inst-demo' THEN 'demo-owner' ELSE 'acme-owner' END,
           account_type = 'Organization', account_seen_at = now() - interval '1 day',
           revoked_at = CASE WHEN installation_id='inst-acme' THEN now() - interval '1 hour' ELSE NULL END
     WHERE installation_id IN ('inst-demo','inst-acme');
    -- tenant 1
    SELECT set_config('core.current_account','ACCT-DEMO', true);
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,counterparty_agent,repo,branch,path,commit_sha,model,detail,occurred_at,visibility) VALUES
      ('EV-D-LAND','ACCT-DEMO','landed','AG-A',NULL,'demo/r','main','a.py','abc123','claude-opus-4-8','behind=',now()-interval '3 days','private'),
      ('EV-D-PUSH','ACCT-DEMO','push','AG-A',NULL,'demo/r','main','','deadbeef','claude-sonnet-4-6',NULL,now()-interval '3 days','private'),
      ('EV-D-WARN','ACCT-DEMO','warn_issued','AG-A','AG-B','demo/r','main','a.py',NULL,NULL,'exposed_by=PR-2',now()-interval '2 days','team'),
      ('EV-D-HELD','ACCT-DEMO','collision_held','AG-A','AG-B','demo/r','main','b.py',NULL,NULL,NULL,now()-interval '2 days','private'),
      ('EV-D-PRED','ACCT-DEMO','prediction','AG-A',NULL,'demo/r','main','PR-1',NULL,NULL,'verdict=warn;behind=PR-2',now()-interval '2 days','private'),
      ('EV-D-OUT','ACCT-DEMO','advice_outcome','AG-A',NULL,'demo/r','main','PR-1',NULL,NULL,'pred=warn;adv=followed;land=clean;conf=observed',now()-interval '1 day','private');
    SELECT core.mark_governed_write('statement');
    INSERT INTO core.statement(statement_id,account_id,agent_id,utterance,about_repo,about_branch,about_path,stated_at,visibility)
      VALUES ('ST-D-1','ACCT-DEMO','AG-A','this module owns auth','demo/r','main','a.py',now()-interval '2 days','private');
    -- tenant 2 (a DIFFERENT account — the cross-tenant bleed guard)
    SELECT set_config('core.current_account','ACCT-ACME', true);
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,detail,occurred_at) VALUES
      ('EV-A-PRED','ACCT-ACME','prediction','AG-ACME','acme/x','main','PR-9','verdict=serialize;behind=',now()-interval '2 days'),
      ('EV-A-OUT','ACCT-ACME','advice_outcome','AG-ACME','acme/x','main','PR-9','pred=serialize;adv=ignored;land=conflicted;conf=inferred',now()-interval '1 day');
    SELECT core.mark_governed_write('statement');
    INSERT INTO core.statement(statement_id,account_id,agent_id,utterance,about_repo,about_branch,about_path,stated_at)
      VALUES ('ST-A-1','ACCT-ACME','AG-ACME','acme api surface','acme/x','main','y.py',now()-interval '2 days');
    -- tenant 3: credential-discovered ONLY (no installation_account row by construction).
    SELECT set_config('core.current_account','ACCT-LOCAL', true);
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,commit_sha,detail,occurred_at)
      VALUES ('EV-L-PUSH','ACCT-LOCAL','push','AG-LOCAL','local/api','main','worker.py','c0ffee',
              'credential-only fixture',now()-interval '1 day');
    SELECT core.mark_governed_write('statement');
    INSERT INTO core.statement(statement_id,account_id,agent_id,utterance,about_repo,about_branch,about_path,stated_at)
      VALUES ('ST-L-1','ACCT-LOCAL','AG-LOCAL','local api ownership','local/api','main','worker.py',
              now()-interval '1 day');
    -- The DR privacy/replay fence retains an uninstall high-water mark while that account remains retained.
    -- A hard erase must delete its account-keyed tombstone too; only opaque scrubbed delivery keys survive below.
    INSERT INTO core.account_lifecycle_tombstone(
      account_id,reason,tombstoned_at,active,last_event_received_at,last_delivery_key,blocked_installation_id)
    VALUES
      ('ACCT-DEMO','uninstall_purge',now()-interval '4 hours',false,
       now()-interval '3 hours','DR-ACTIVATED-NEW','inst-demo-old');
    -- A hard erase retains only opaque delivery keys. Every tenant-bearing and executable field is scrubbed.
    INSERT INTO core.webhook_delivery(
      delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,updated_at,
      locked_at,done_at,last_error,not_before,lease_generation,causal_order_version,
      retry_window_expires_at,auto_rearm_count,operator_recovery_id,operator_recovered_at,
      operator_recovery_batch_size,operator_recovery_batch_token,
      operator_recovery_count)
    VALUES
      ('DR-ERASED-OLD-A','erased',NULL,NULL,'{}'::jsonb,'done',0,
       now()-interval '2 hours',now()-interval '2 hours',NULL,now()-interval '2 hours',NULL,NULL,2,1,NULL,0,
       NULL,NULL,NULL,NULL,0),
      ('DR-ERASED-OLD-B','erased',NULL,NULL,'{}'::jsonb,'done',0,
       now()-interval '1 hour',now()-interval '1 hour',NULL,now()-interval '1 hour',NULL,NULL,1,1,NULL,0,
       NULL,NULL,NULL,NULL,0);
    -- Repo lifecycle and billing high-water rows are non-rebuildable safety state too.
    SELECT set_config('core.current_account','ACCT-DEMO', true);
    INSERT INTO core.repository_lifecycle_tombstone(
      account_id,repository_id,repo,reason,tombstoned_at,lifecycle_received_at,superseded_at,generation_started_at)
    VALUES ('ACCT-DEMO','101','demo/retired','repository_deleted',now()-interval '5 hours',
            now()-interval '5 hours',NULL,now()-interval '20 days');
    INSERT INTO core.repository_lifecycle_activation(
      account_id,repository_id,repo,activated_at,lifecycle_authoritative,generation_started_at)
    VALUES ('ACCT-DEMO','202','demo/r',now()-interval '4 hours',true,now()-interval '10 days');
    INSERT INTO core.account_plan_event(account_id,last_effective_at,last_applied_at)
    VALUES ('ACCT-DEMO',now()-interval '7 days',now()-interval '6 days');
    -- Unfinished work must survive host loss. The export projects last_error=NULL and only minimized payloads.
    INSERT INTO core.webhook_delivery(
      delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,updated_at,
      locked_at,done_at,last_error,not_before,lease_generation,causal_order_version,
      retry_window_expires_at,auto_rearm_count,operator_recovery_id,operator_recovered_at,
      operator_recovery_batch_size,operator_recovery_batch_token,
      operator_recovery_count)
    VALUES
      ('DR-QUEUED-PUSH','push','demo-owner-id','demo/r',
       '{"ref":"refs/heads/main","after":"abc123","repository":{"id":202,"full_name":"demo/r","name":"r","default_branch":"main","owner":{"id":77,"login":"demo-owner","type":"Organization"}}}'::jsonb,
       'queued',0,now()-interval '20 minutes',now()-interval '20 minutes',NULL,NULL,NULL,NULL,0,1,
       now()+interval '2 minutes',0,NULL,NULL,NULL,NULL,0),
      ('DR-PROCESSING-PING','ping','demo-owner-id',NULL,'{}'::jsonb,
       'processing',1,now()-interval '10 minutes',now()-interval '9 minutes',now()-interval '9 minutes',
       NULL,NULL,NULL,4,1,now()+interval '1 minute',0,NULL,NULL,NULL,NULL,0),
      ('DR-FAILED-PING','ping','demo-owner-id',NULL,'{}'::jsonb,
       'failed',8,now()-interval '2 hours',now()-interval '1 hour',NULL,NULL,
       'operator-only diagnostic must not leave export',NULL,9,1,
       now()-interval '1 hour',1,'dr-operator-recovery-1',
       date_trunc('milliseconds',now()-interval '90 minutes')+interval '890 microseconds',1,
       '00000000-0000-4000-8000-000000000003',1),
      ('00000000-0000-4000-8000-000000000007','ping','demo-owner-id',NULL,'{}'::jsonb,
       'done',2,now()-interval '2 hours',now()-interval '30 minutes',NULL,
       now()-interval '30 minutes',NULL,NULL,10,1,NULL,2,
       'dr-operator-recovery-partial',now()-interval '90 minutes',2,
       '00000000-0000-4000-8000-000000000011',1),
      ('00000000-0000-4000-8000-000000000008','ping','demo-owner-id',NULL,'{}'::jsonb,
       'failed',8,now()-interval '2 hours',now()-interval '20 minutes',NULL,NULL,
       'second epoch exhausted before host loss',NULL,11,1,
       now()-interval '20 minutes',2,'dr-operator-recovery-partial',now()-interval '90 minutes',2,
       '00000000-0000-4000-8000-000000000011',1);
    """
    conn = mig(SRC)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(seed)
    finally:
        conn.close()

    diagnose_env = os.environ.copy()
    diagnose_env.pop("BACKUP_DSN", None)
    diagnose_env.pop("VERIPSA_DSN", None)
    diagnose_env["OWNER_DSN"] = dsn(SRC)
    diagnosed = subprocess.run(
        [sys.executable, "github-app/backup_export.py", "diagnose",
         "--delivery", RECOVERY_FAILED_GUID],
        cwd=ROOT, env=diagnose_env, capture_output=True, text=True,
    )
    backup_only_env = dict(diagnose_env)
    backup_only_env.pop("OWNER_DSN", None)
    backup_only_env["BACKUP_DSN"] = dsn(SRC)
    backup_only_denied = subprocess.run(
        [sys.executable, "github-app/backup_export.py", "diagnose",
         "--delivery", RECOVERY_FAILED_GUID],
        cwd=ROOT, env=backup_only_env, capture_output=True, text=True,
    )
    wrong_owner_env = dict(diagnose_env)
    wrong_owner_env["OWNER_DSN"] = (
        f"postgresql://veripsa_demo_agent@localhost/{SRC}")
    wrong_owner_denied = subprocess.run(
        [sys.executable, "github-app/backup_export.py", "diagnose",
         "--delivery", RECOVERY_FAILED_GUID],
        cwd=ROOT, env=wrong_owner_env, capture_output=True, text=True,
    )
    diagnose_streams = "".join((
        diagnosed.stdout, diagnosed.stderr,
        backup_only_denied.stdout, backup_only_denied.stderr,
        wrong_owner_denied.stdout, wrong_owner_denied.stderr,
    ))
    checks.append((
        "owner-only exact-delivery diagnosis is useful but never emits the GUID, payload/error, or log-grep advice",
        diagnosed.returncode == 0
        and diagnosed.stdout.strip() == (
            "# diagnose delivery=present status=failed attempts=8 locked=false due=true "
            "auto_rearm_count=2 operator_recovery_count=1 operator_continuation_count=0"
        )
        and diagnosed.stderr == ""
        and backup_only_denied.returncode == 2
        and wrong_owner_denied.returncode == 2
        and "requires OWNER_DSN" in backup_only_denied.stderr
        and "requires veripsa_migrator OWNER_DSN" in wrong_owner_denied.stderr
        and RECOVERY_FAILED_GUID not in diagnose_streams
        and "second epoch exhausted before host loss" not in diagnose_streams
        and "render" not in diagnose_streams.lower()
        and "grep" not in diagnose_streams.lower(),
    ))

    src_demo_kinds = events_by_kind(SRC, "ACCT-DEMO")
    checks.append((f"seed: SRC ACCT-DEMO holds the full durable spread incl. answer-check kinds (kinds={src_demo_kinds})",
                   src_demo_kinds.get("landed") == 1 and src_demo_kinds.get("push") == 1
                   and src_demo_kinds.get("warn_issued") == 1 and src_demo_kinds.get("collision_held") == 1
                   and src_demo_kinds.get("prediction") == 1 and src_demo_kinds.get("advice_outcome") == 1))

    src_local_registry = identity_registry_rows(SRC, "ACCT-LOCAL")
    src_local_export = scoped_export_rows(SRC, "ACCT-LOCAL")
    src_local_tables = {row.get("_table") for row in src_local_export}
    checks.append((f"seed: credential-only tenant is discoverable without an installation "
                   f"(tables={sorted(src_local_tables)}, registry={src_local_registry})",
                   len(src_local_registry["installations"]) == 0
                   and len(src_local_registry["accounts"]) == 1
                   and len(src_local_registry["agents"]) == 1
                   and len(src_local_registry["credentials"]) == 1
                   and src_local_tables == {"account", "agent", "credential", "event", "statement"}))

    (src_tombstones, src_receipts, src_repo_tombstones,
     src_repo_activations, src_plan_events) = lifecycle_fence_rows(SRC)
    checks.append((f"seed: retained uninstall/repo/billing fences plus scrubbed hard-erase receipts "
                   f"(account={len(src_tombstones)} repo_t={len(src_repo_tombstones)} "
                   f"repo_a={len(src_repo_activations)} plan={len(src_plan_events)} receipts={len(src_receipts)})",
                   len(src_tombstones) == 1 and len(src_receipts) == 2
                   and len(src_repo_tombstones) == len(src_repo_activations) == len(src_plan_events) == 1
                   and src_tombstones[0]["reason"] == "uninstall_purge"
                   and src_tombstones[0]["account_id"] == "ACCT-DEMO"
                   and all(r["event_type"] == "erased" and r["status"] == "done"
                           and r["account_key"] is None and r["repo"] is None and r["payload"] == {}
                           for r in src_receipts)))

    erased_scoped = scoped_export_rows(SRC, "ACCT-GH-DR-ERASED")
    demo_scoped = scoped_export_rows(SRC, "ACCT-DEMO")
    checks.append(("hard-erased account's scoped DR export remains exactly ZERO rows", erased_scoped == []))
    checks.append(("per-account DR export never exposes the cross-tenant lifecycle fence",
                   not any(r.get("_table") in ("account_lifecycle_tombstone", "webhook_delivery")
                           for r in demo_scoped)))

    # ── EXPORT the full cross-tenant durable set, then assert CONTENT-FREE shape on the wire.
    buf = io.StringIO()
    res = backup_export.export(dsn(SRC), out=buf)
    src_counts = backup_export.count(dsn(SRC))
    lines = buf.getvalue().splitlines()
    envelope = backup_export.verify(lines)
    recs = backup_export.load_verified_records(lines)
    ev_recs = [r for r in recs if r.get("_table") == "event"]
    tombstone_recs = [r for r in recs if r.get("_table") == "account_lifecycle_tombstone"]
    repo_tombstone_recs = [r for r in recs if r.get("_table") == "repository_lifecycle_tombstone"]
    repo_activation_recs = [r for r in recs if r.get("_table") == "repository_lifecycle_activation"]
    plan_recs = [r for r in recs if r.get("_table") == "account_plan_event"]
    webhook_recs = [r for r in recs if r.get("_table") == "webhook_delivery"]
    receipt_recs = [r for r in webhook_recs if r.get("event_type") == "erased"]
    replay_recs = [r for r in webhook_recs if r.get("event_type") != "erased"]
    unfinished_recs = [r for r in replay_recs if r.get("status") != "done"]
    recovery_done_recs = [r for r in replay_recs if r.get("status") == "done"]
    failed_recovery_wire_timestamp = next(
        r for r in unfinished_recs
        if r["delivery_key"] == "DR-FAILED-PING"
    )["operator_recovered_at"]
    failed_recovery_wire_match = (
        backup_export._RFC3339_AWARE_TIMESTAMP.fullmatch(
            failed_recovery_wire_timestamp)
    )
    extra_cols = set()
    for r in ev_recs:
        extra_cols |= (set(r.keys()) - EVENT_COLS)
    checks.append((f"export is CONTENT-FREE: every event object exposes only the documented columns "
                   f"(stray columns seen={sorted(extra_cols) or 'none'})", not extra_cols))
    fence_shapes_ok = (
        all(set(r) == TOMBSTONE_COLS for r in tombstone_recs)
        and all(set(r) == ERASED_RECEIPT_COLS for r in receipt_recs)
        and all(r["event_type"] == "erased" and r["status"] == "done"
                and r["account_key"] is None and r["repo"] is None and r["payload"] == {}
                and r["attempts"] == 0 and r["last_error"] is None
                and r["locked_at"] is None and r["not_before"] is None
                for r in receipt_recs)
    )
    checks.append((f"global export carries only the content-free lifecycle fence "
                   f"(tombstones={len(tombstone_recs)} erased_receipts={len(receipt_recs)})",
                   len(tombstone_recs) == 1 and len(receipt_recs) == 2 and fence_shapes_ok
                   and "ACCT-GH-DR-ERASED" not in buf.getvalue()
                   and "GH-INSTALL-GEN-ERASED-A" not in buf.getvalue()))
    checks.append(("export carries repo/billing fences, sanitized unfinished rows, and exact-batch done proof",
                   len(repo_tombstone_recs) == len(repo_activation_recs) == len(plan_recs) == 1
                   and {r["delivery_key"] for r in unfinished_recs}
                   == {"DR-QUEUED-PUSH", "DR-PROCESSING-PING", "DR-FAILED-PING",
                       RECOVERY_FAILED_GUID}
                   and {r["delivery_key"] for r in recovery_done_recs} == {RECOVERY_DONE_GUID}
                   and all(r["last_error"] is None and r["done_at"] is None for r in unfinished_recs)
                   and recovery_done_recs[0]["payload"] == {}
                   and recovery_done_recs[0]["locked_at"] is None
                   and recovery_done_recs[0]["owner_instance"] is None
                   and recovery_done_recs[0]["last_error"] is None
                   and recovery_done_recs[0]["not_before"] is None
                   and recovery_done_recs[0]["retry_window_expires_at"] is None
                   and recovery_done_recs[0]["operator_recovery_id"] == RECOVERY_ID
                   and recovery_done_recs[0]["operator_recovery_batch_size"] == 2
                   and recovery_done_recs[0]["operator_recovery_batch_token"] == RECOVERY_BATCH_TOKEN
                   and next(r for r in unfinished_recs
                            if r["delivery_key"] == "DR-FAILED-PING")["auto_rearm_count"] == 1
                   and next(r for r in unfinished_recs
                            if r["delivery_key"] == "DR-FAILED-PING")["operator_recovery_id"]
                   == "dr-operator-recovery-1"
                   and next(r for r in unfinished_recs
                            if r["delivery_key"] == "DR-FAILED-PING")["operator_recovery_count"] == 1
                   and next(r for r in unfinished_recs
                            if r["delivery_key"] == "DR-FAILED-PING")["operator_recovery_batch_size"] == 1
                   and next(r for r in unfinished_recs
                            if r["delivery_key"] == "DR-FAILED-PING")["operator_recovery_batch_token"]
                   == "00000000-0000-4000-8000-000000000003"
                   and all(r["retry_window_expires_at"] is not None for r in unfinished_recs)
                   and "operator-only diagnostic" not in buf.getvalue()))
    checks.append((
        "round-trip fixture carries PostgreSQL's trailing-zero-trimmed five-digit timestamp",
        failed_recovery_wire_match is not None
        and len(failed_recovery_wire_match.group("fraction") or "") == 5,
    ))
    checks.append(("export count reconciles every non-rebuildable class",
                   src_counts == res
                   and res["lifecycle_tombstones"] == len(tombstone_recs) == 1
                   and res["repository_lifecycle_tombstones"] == len(repo_tombstone_recs) == 1
                   and res["repository_lifecycle_activations"] == len(repo_activation_recs) == 1
                   and res["account_plan_event_fences"] == len(plan_recs) == 1
                   and res["erased_delivery_receipts"] == len(receipt_recs) == 2
                   # Stable format-v2 semantics: this historical key counts every replay-critical non-erased row,
                   # including the done sibling that proves an unfinished exact operator batch.
                   and res["unfinished_webhook_deliveries"] == len(replay_recs) == 5
                   and envelope["durable_counts"] == res
                   and envelope["records"] == len(recs)))
    global_local_rows = [row for row in recs if row.get("account_id") == "ACCT-LOCAL"]
    checks.append(("host-wide export includes the credential-only tenant's complete registry and durable rows",
                   canonical_rows(global_local_rows) == canonical_rows(src_local_export)
                   and {row.get("_table") for row in global_local_rows}
                   == {"account", "agent", "credential", "event", "statement"}))
    exp_kinds = {}
    for r in ev_recs:
        exp_kinds[r["kind"]] = exp_kinds.get(r["kind"], 0) + 1
    checks.append((f"export carries the answer-check kinds across BOTH tenants "
                   f"(prediction={exp_kinds.get('prediction')}, advice_outcome={exp_kinds.get('advice_outcome')})",
                   exp_kinds.get("prediction") == 2 and exp_kinds.get("advice_outcome") == 2))

    # The envelope/parser must fail closed before `_connect` for every ambiguity or incomplete file.
    data_indexes = [i for i, line in enumerate(lines) if '"_table"' in line]

    def refused_before_db(candidate):
        try:
            backup_export.import_jsonl("not-opened", candidate)
        except ValueError:
            return True
        except Exception:
            return False
        return False

    unknown = list(lines)
    unknown.insert(-1, json.dumps({"_table": "future_unknown", "id": "x"}))
    duplicate = list(lines)
    duplicate.insert(-1, lines[data_indexes[0]])
    missing = [line for i, line in enumerate(lines) if i != data_indexes[0]]
    unsupported = list(lines)
    manifest = json.loads(unsupported[0]); manifest["version"] = 999
    unsupported[0] = json.dumps(manifest)
    duplicate_json_key = list(lines)
    duplicate_json_key[0] = '{"_backup":"manifest","_backup":"manifest"}'
    checks.append(("restore rejects truncation/unknown/missing/duplicate/unsupported/duplicate-key input before DB",
                   all(refused_before_db(candidate) for candidate in (
                       lines[:-1], unknown, duplicate, missing, unsupported, duplicate_json_key))))

    # ── RESTORE into the fresh EMPTY DST via the documented import_jsonl path.
    imp = backup_export.import_jsonl(dsn(DST), lines)
    checks.append((f"restore re-inserted every catastrophic-to-lose class (counts={imp})", imp == res))

    # ── LOSSLESS per KIND + byte-for-byte per ROW, per tenant.
    for acct in ("ACCT-DEMO", "ACCT-ACME", "ACCT-LOCAL"):
        src_k = events_by_kind(SRC, acct)
        dst_k = events_by_kind(DST, acct)
        checks.append((f"round-trip {acct}: per-kind counts identical SRC↔DST (src={src_k} dst={dst_k})",
                       src_k == dst_k and len(src_k) > 0))
        src_rows = event_rows(SRC, acct)
        dst_rows = event_rows(DST, acct)
        same = src_rows == dst_rows
        checks.append((f"round-trip {acct}: every event row is byte-for-byte identical "
                       f"({len(src_rows)} rows; identical={same})", same))
        src_statements = table_rows(SRC, acct, "statement")
        dst_statements = table_rows(DST, acct, "statement")
        checks.append((f"round-trip {acct}: every statement row is byte-for-byte identical "
                       f"(src={len(src_statements)} dst={len(dst_statements)})",
                       src_statements == dst_statements and len(src_statements) >= 1))
        checks.append((f"round-trip {acct}: identity/route registry columns are exactly identical",
                       identity_registry_rows(SRC, acct) == identity_registry_rows(DST, acct)))

    (dst_tombstones, dst_receipts, dst_repo_tombstones,
     dst_repo_activations, dst_plan_events) = lifecycle_fence_rows(DST)
    checks.append(("round-trip: lifecycle tombstones preserve exact state, ordering boundary, and generation id",
                   dst_tombstones == src_tombstones))
    checks.append(("round-trip: erased delivery receipts are byte-for-byte identical before redelivery",
                   dst_receipts == src_receipts))
    checks.append(("round-trip: repository lifecycle and billing high-water rows are byte-for-byte identical",
                   dst_repo_tombstones == src_repo_tombstones
                   and dst_repo_activations == src_repo_activations
                   and dst_plan_events == src_plan_events))
    conn = mig(DST)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT delivery_key,status,locked_at,last_error,payload,"
                        "retry_window_expires_at,auto_rearm_count,operator_recovery_id,"
                        "operator_recovered_at,operator_recovery_batch_size,"
                        "operator_recovery_batch_token,operator_recovery_count "
                        "FROM core.webhook_delivery "
                        "WHERE event_type<>'erased' ORDER BY delivery_key")
            restored_replay_rows = cur.fetchall()
    finally:
        conn.close()
    restored_by_key = {row[0]: row for row in restored_replay_rows}
    restored_unfinished = [row for row in restored_replay_rows if row[1] != "done"]
    restored_done_proof = restored_by_key[RECOVERY_DONE_GUID]
    exported_failed_recovered_at = backup_export._parse_aware_timestamp(
        failed_recovery_wire_timestamp,
        "operator recovery timestamp",
    )
    checks.append(("round-trip: replay inbox and done proof survive; processing restores queued/unlocked",
                   set(restored_by_key)
                   == {"DR-FAILED-PING", "DR-PROCESSING-PING", "DR-QUEUED-PUSH",
                       RECOVERY_FAILED_GUID, RECOVERY_DONE_GUID}
                   and restored_by_key["DR-PROCESSING-PING"][1:4]
                   == ("queued", None, None)
                   and all(row[3] is None and isinstance(row[4], dict)
                           and row[5] is not None for row in restored_unfinished)
                   and restored_by_key["DR-FAILED-PING"][6] == 1
                   and restored_by_key["DR-FAILED-PING"][7] == "dr-operator-recovery-1"
                   and restored_by_key["DR-FAILED-PING"][8]
                   == exported_failed_recovered_at
                   and restored_by_key["DR-FAILED-PING"][8].isoformat()
                   != failed_recovery_wire_timestamp
                   and restored_by_key["DR-FAILED-PING"][9] == 1
                   and restored_by_key["DR-FAILED-PING"][10]
                   == "00000000-0000-4000-8000-000000000003"
                   and restored_by_key["DR-FAILED-PING"][11] == 1
                   and restored_done_proof[1] == "done"
                   and restored_done_proof[2:4] == (None, None)
                   and restored_done_proof[4] == {}
                   and restored_done_proof[5] is None
                   and restored_done_proof[7] == RECOVERY_ID
                   and restored_done_proof[9] == 2
                   and restored_done_proof[10] == RECOVERY_BATCH_TOKEN
                   and restored_done_proof[11] == 1))

    # ── NO CROSS-TENANT BLEED: DST ACCT-DEMO must NOT contain ACCT-ACME's PR-9 prediction, and vice-versa.
    demo_ids = set(event_rows(DST, "ACCT-DEMO").keys())
    acme_ids = set(event_rows(DST, "ACCT-ACME").keys())
    local_ids = set(event_rows(DST, "ACCT-LOCAL").keys())
    overlap = (demo_ids & acme_ids) | (demo_ids & local_ids) | (acme_ids & local_ids)
    checks.append((f"no cross-tenant bleed in DST across GitHub and credential-only routes "
                   f"(overlap={sorted(overlap) or 'none'})",
                   not overlap and "EV-A-PRED" not in demo_ids and "EV-D-PRED" not in acme_ids
                   and local_ids == {"EV-L-PUSH"}))

    # ── NO ORPHAN: the registry was restored, so a fresh DR export OF DST is non-empty AND per-account
    # (the restored event/statement rows are re-discoverable, not orphaned under a missing tenant).
    dst_buf = io.StringIO()
    dst_res = backup_export.export(dsn(DST), out=dst_buf)
    dst_lines = dst_buf.getvalue().splitlines()
    dst_recs = backup_export.load_verified_records(dst_lines)
    has_acct_rows = any(r.get("_table") == "account" for r in dst_recs)
    has_agent_rows = any(r.get("_table") == "agent" for r in dst_recs)
    has_credential_rows = any(r.get("_table") == "credential" for r in dst_recs)
    has_install_rows = any(r.get("_table") == "installation_account" for r in dst_recs)
    checks.append((f"no orphan: DST re-export is non-empty + restored the registry "
                   f"(events={dst_res['events']}, account={has_acct_rows}, agent={has_agent_rows}, "
                   f"credential={has_credential_rows}, install={has_install_rows})",
                   dst_res == res and has_acct_rows and has_agent_rows
                   and has_credential_rows and has_install_rows))
    dst_local_registry = identity_registry_rows(DST, "ACCT-LOCAL")
    dst_local_export = scoped_export_rows(DST, "ACCT-LOCAL")
    checks.append(("second export after fresh restore preserves the route-less tenant and its discovery registry",
                   dst_local_registry == src_local_registry
                   and canonical_rows(dst_local_export) == canonical_rows(src_local_export)
                   and len(dst_local_registry["installations"]) == 0))
    checks.append(("restore preserves installation liveness state (revoked_at survives DR round-trip)",
                   installation_revoked(SRC, "inst-acme") and installation_revoked(DST, "inst-acme")))
    checks.append(("restore preserves the current GitHub installation generation used by delayed-delete fencing",
                   installation_generation(SRC, "inst-demo")
                   == installation_generation(DST, "inst-demo") == "GH-INSTALL-GEN-DEMO-B"
                   and installation_generation(SRC, "inst-acme")
                   == installation_generation(DST, "inst-acme") == "GH-INSTALL-GEN-ACME-A"))

    # ── IDEMPOTENT: a SECOND import inserts nothing because every existing value is identical.
    backup_export.import_jsonl(dsn(DST), lines)
    re_k = events_by_kind(DST, "ACCT-DEMO")
    (re_tombstones, re_receipts, re_repo_tombstones,
     re_repo_activations, re_plan_events) = lifecycle_fence_rows(DST)
    checks.append((f"restore is IDEMPOTENT: a second import adds no rows (ACCT-DEMO kinds still {re_k})",
                   re_k == events_by_kind(SRC, "ACCT-DEMO")
                   and re_tombstones == src_tombstones and re_receipts == src_receipts
                   and re_repo_tombstones == src_repo_tombstones
                   and re_repo_activations == src_repo_activations and re_plan_events == src_plan_events
                   and canonical_rows(scoped_export_rows(DST, "ACCT-LOCAL"))
                   == canonical_rows(src_local_export)))

    # The done sibling is not historical decoration: together with the failed row it reconstructs the exact
    # all-or-zero operator batch after a host loss. A subset must remain ineligible; the full restored set then
    # requeues only the failed member and stamps one continuation time/SHA while preserving the completed member.
    conn = mig(DST)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            exact_set = [RECOVERY_FAILED_GUID, RECOVERY_DONE_GUID]
            cur.execute(
                "SELECT core.terminal_webhook_delivery_recovery_status_with_authority(%s)",
                (exact_set,),
            )
            restored_status = cur.fetchone()[0]
            cur.execute(
                "SELECT core.continue_terminal_webhook_deliveries_with_authority(%s,%s,%s)",
                ([RECOVERY_FAILED_GUID], CONTINUATION_ID, CONTINUATION_SHA),
            )
            subset_continuation = cur.fetchone()[0]
            cur.execute(
                "SELECT to_jsonb(d) FROM core.webhook_delivery d WHERE delivery_key=%s",
                (RECOVERY_DONE_GUID,),
            )
            done_before_continuation = cur.fetchone()[0]
            cur.execute(
                "SELECT core.continue_terminal_webhook_deliveries_with_authority(%s,%s,%s)",
                (exact_set, CONTINUATION_ID, CONTINUATION_SHA),
            )
            exact_continuation = cur.fetchone()[0]
            cur.execute(
                "SELECT delivery_key,to_jsonb(d) FROM core.webhook_delivery d "
                "WHERE delivery_key=ANY(%s) ORDER BY delivery_key",
                (exact_set,),
            )
            continued_rows = dict(cur.fetchall())
    finally:
        conn.close()
    continued_failed = continued_rows[RECOVERY_FAILED_GUID]
    continued_done = continued_rows[RECOVERY_DONE_GUID]
    common_continuation = all(
        row["operator_continuation_count"] == 1
        and row["operator_continuation_id"] == CONTINUATION_ID
        and row["operator_continuation_sha"] == CONTINUATION_SHA
        and row["operator_continued_at"] is not None
        and row["operator_recovery_id"] == RECOVERY_ID
        and row["operator_recovery_batch_token"] == RECOVERY_BATCH_TOKEN
        for row in continued_rows.values()
    )
    checks.append((
        "restored exact recovery batch performs one forward continuation without replaying its done sibling",
        restored_status == {"status": "continuable", "requested": 2}
        and subset_continuation == {
            "status": "ok", "requested": 1, "continued": 0,
            "already_completed": 0, "already_continued": 0,
            "ineligible": 1, "missing": 0, "results": ["ineligible"],
        }
        and exact_continuation == {
            "status": "ok", "requested": 2, "continued": 1,
            "already_completed": 1, "already_continued": 0,
            "ineligible": 0, "missing": 0,
            "results": ["continued", "already_completed"],
        }
        and continued_failed["status"] == "queued"
        and continued_failed["attempts"] == 0
        and continued_failed["auto_rearm_count"] == 3
        and continued_failed["done_at"] is None
        and continued_failed["last_error"] is None
        and continued_failed["not_before"] is None
        and continued_failed["retry_window_expires_at"] is None
        and continued_done["status"] == "done"
        and continued_done["done_at"] == done_before_continuation["done_at"]
        and continued_done["payload"] == {}
        and common_continuation
        and continued_failed["operator_continued_at"]
        == continued_done["operator_continued_at"],
    ))

    # A restored erased receipt is an executable safety boundary, not just an audit count: GitHub redelivering
    # the old key must hit ON CONFLICT(status=done), retain the scrubbed row, and never enter the worker queue.
    conn = mig(DST)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            old_payload = json.dumps({
                "action": "created",
                "installation": {"id": "inst-old", "account": {"id": "DR-ERASED"}},
            })
            cur.execute(
                "SELECT core.enqueue_webhook_delivery_with_authority(%s,%s,%s,%s,%s::jsonb,%s,%s)",
                ("DR-ERASED-OLD-A", "installation", "DR-ERASED", "private/should-not-return",
                 old_payload, 5000, 2),
            )
            replay = cur.fetchone()[0]
            cur.execute(
                "SELECT event_type,status,account_key,repo,payload,attempts,locked_at,last_error,not_before "
                "FROM core.webhook_delivery WHERE delivery_key='DR-ERASED-OLD-A'"
            )
            replay_row = cur.fetchone()
    finally:
        conn.close()
    checks.append((f"old delivery replay stays non-executable after DR restore (enqueue={replay})",
                   replay.get("accepted") is True and replay.get("queued") is False
                   and replay.get("status") == "done"
                   and replay_row == ("erased", "done", None, None, {}, 0, None, None, None)))

    # The restore path itself fails closed if a hand-edited/tampered JSONL tries to turn the receipt section into
    # an executable webhook or reintroduce a private repo coordinate. Validation runs before any DB connection.
    malformed_lines = list(lines)
    malformed = dict(receipt_recs[0])
    malformed["repo"] = "private/must-not-restore"
    for i, line in enumerate(malformed_lines):
        decoded = json.loads(line)
        if decoded.get("_table") == "webhook_delivery" and decoded.get("delivery_key") == malformed["delivery_key"]:
            malformed_lines[i] = json.dumps(malformed)
            break
    malformed_refused = False
    try:
        backup_export.import_jsonl("not-opened", malformed_lines)
    except ValueError:
        malformed_refused = True
    checks.append(("restore refuses a non-scrubbed webhook_delivery before touching the DB", malformed_refused))

    # A shipped format-v2 row omitted the new epoch fields. Compatibility must
    # never interpret that omission as a fresh automatic retry.
    missing_rearm = dict(unfinished_recs[0])
    missing_rearm.pop("auto_rearm_count")
    missing_rearm.pop("retry_window_expires_at")
    missing_rearm.pop("operator_recovery_id")
    missing_rearm.pop("operator_recovered_at")
    missing_rearm.pop("operator_recovery_batch_size")
    missing_rearm.pop("operator_recovery_batch_token")
    missing_rearm.pop("operator_recovery_count")
    missing_rearm.pop("operator_continuation_id")
    missing_rearm.pop("operator_continued_at")
    missing_rearm.pop("operator_continuation_sha")
    missing_rearm.pop("operator_continuation_count")
    safe_legacy_rearm = False
    try:
        normalized_rearm = backup_export._normalize_legacy_record(missing_rearm)
        backup_export._validate_record(normalized_rearm)
        safe_legacy_rearm = (
            normalized_rearm["auto_rearm_count"] == 1
            and normalized_rearm["retry_window_expires_at"] is None
            and normalized_rearm["operator_recovery_id"] is None
            and normalized_rearm["operator_recovered_at"] is None
            and normalized_rearm["operator_recovery_batch_size"] is None
            and normalized_rearm["operator_recovery_batch_token"] is None
            and normalized_rearm["operator_recovery_count"] == 0
            and normalized_rearm["operator_continuation_id"] is None
            and normalized_rearm["operator_continued_at"] is None
            and normalized_rearm["operator_continuation_sha"] is None
            and normalized_rearm["operator_continuation_count"] == 0
        )
    except ValueError:
        pass
    checks.append(("legacy v2 webhook omission restores conservatively with automatic rearm exhausted",
                   safe_legacy_rearm))

    # The new exact-batch proof is part of the one-shot audit, not optional
    # telemetry. Reject malformed or half-cleared metadata before opening the
    # restore database so a subset can never become a reconstructed epoch.
    recovered_record = next(
        r for r in unfinished_recs if r["delivery_key"] == "DR-FAILED-PING")
    malformed_audits = []
    for field, value in (
        ("operator_recovery_batch_size", 0),
        ("operator_recovery_batch_token", "not-a-uuid"),
        ("operator_recovery_batch_token", "11111111-1111-4111-8111-00000000000a".upper()),
        ("operator_recovery_batch_token", "99999999-8888-4777-7666-555555555555"),
    ):
        candidate = dict(recovered_record)
        candidate[field] = value
        try:
            backup_export._validate_record(candidate)
        except ValueError:
            malformed_audits.append(True)
        else:
            malformed_audits.append(False)
    half_cleared = dict(next(
        r for r in unfinished_recs
        if r["operator_recovery_count"] == 0
    ))
    half_cleared["operator_recovery_batch_token"] = (
        "00000000-0000-4000-8000-000000000029")
    try:
        backup_export._validate_record(half_cleared)
    except ValueError:
        malformed_audits.append(True)
    else:
        malformed_audits.append(False)

    valid_continuation = dict(recovered_record)
    valid_continuation.update(
        operator_continuation_id="dr-backup-continuation",
        operator_continued_at=recovered_record["operator_recovered_at"],
        operator_continuation_sha="b" * 40,
        operator_continuation_count=1,
    )
    for candidate in (
        {**valid_continuation, "operator_continuation_sha": None},
        {**valid_continuation, "operator_continuation_sha": "B" * 40},
        {**valid_continuation, "operator_continued_at": "2026-07-01T00:00:00"},
        {**recovered_record, "operator_continuation_id": "half-cleared-continuation"},
    ):
        try:
            backup_export._validate_record(candidate)
        except ValueError:
            malformed_audits.append(True)
        else:
            malformed_audits.append(False)
    checks.append((
        "backup validator rejects malformed recovery and continuation batch proof",
        malformed_audits == [True] * 9,
    ))

    # A non-erased done row is exceptional: it is retained only as the completed half of an unfinished exact
    # recovery batch. Hand-edited terminal history must not use that narrow opening to enter a restore.
    recovery_done_record = recovery_done_recs[0]
    proof_mutations = [
        {"payload": {"action": "must-not-restore"}},
        {"done_at": "2026-07-01T00:00:00"},
        {"done_at": "2000-01-01T00:00:00+00:00"},
        {"attempts": True},
        {"account_key": "x" * 121},
        {"repo": "x" * 513},
        {"locked_at": recovery_done_record["done_at"]},
        {"owner_instance": "lost-worker"},
        {"last_error": "must-not-cross-DR"},
        {"not_before": recovery_done_record["done_at"]},
        {"retry_window_expires_at": recovery_done_record["done_at"]},
    ]
    proof_refusals = []
    for mutation in proof_mutations:
        try:
            backup_export._validate_record({**recovery_done_record, **mutation})
        except ValueError:
            proof_refusals.append(True)
        else:
            proof_refusals.append(False)
    checks.append((
        "done recovery proof validator rejects payload/time/coordinate/worker/retry residue",
        proof_refusals == [True] * len(proof_mutations),
    ))

    # A valid envelope is idempotent only while every collision is identical. Alter one registry value and prove
    # that the next restore aborts instead of silently accepting a split-brain registry.
    conn = mig(DST)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE core.installation_account SET account_login='different-owner' "
                        "WHERE installation_id='inst-demo'")
    finally:
        conn.close()
    collision_refused = False
    try:
        backup_export.import_jsonl(dsn(DST), lines)
    except ValueError as exc:
        collision_refused = "not byte-identical" in str(exc)
    checks.append(("restore refuses a same-primary-key/different-value collision", collision_refused))

    # The rolling-upgrade schema temporarily retains legacy hard-erasure fences for old workers, but they must not
    # enter a new backup. The shared export/import validator independently refuses that raw-identity shape.
    forbidden_marker = dict(tombstone_recs[0])
    forbidden_marker.update(
        account_id="ACCT-GH-DR-ERASED",
        reason="gdpr_erase",
        active=True,
        last_delivery_key="DR-ERASED-OLD-A",
        blocked_installation_id="GH-INSTALL-GEN-ERASED-A",
    )
    forbidden_marker_refused = False
    try:
        backup_export._validate_record(forbidden_marker)
    except ValueError:
        forbidden_marker_refused = True
    checks.append(("backup validator refuses a legacy hard-erasure tombstone with raw identities",
                   forbidden_marker_refused
                   and "ACCT-GH-DR-ERASED" not in buf.getvalue()
                   and "GH-INSTALL-GEN-ERASED-A" not in buf.getvalue()))

    # Export validates erased rows before writing them. A malformed DB receipt can leave a partial envelope, but
    # never the unsafe record or a success footer; such a file is independently unrestorable as truncated.
    conn = mig(SRC)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO core.webhook_delivery(delivery_key,event_type,repo,payload,status,attempts) "
                "VALUES ('DR-BAD-ERASED','erased','private/must-not-export','{}'::jsonb,'done',0)"
            )
    finally:
        conn.close()
    unsafe_out = io.StringIO()
    unsafe_refused = False
    try:
        backup_export.export(dsn(SRC), out=unsafe_out)
    except ValueError:
        unsafe_refused = True
    checks.append(("export refuses malformed erased receipt before writing that record/footer",
                   unsafe_refused and "DR-BAD-ERASED" not in unsafe_out.getvalue()
                   and '"_backup":"footer"' not in unsafe_out.getvalue()))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("BACKUP RESTORE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", SRC], capture_output=True)
        subprocess.run(["dropdb", DST], capture_output=True)
