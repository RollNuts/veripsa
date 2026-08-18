#!/usr/bin/env python3
"""Retention gate — the ledger is append-only EXCEPT a gated, account-scoped prune of OPERATIONAL telemetry.

core.event is an APPEND-ONLY ledger (trg_append_only_event → assert_append_only blocks DELETE/UPDATE), but it
grows UNBOUNDED (every push/landing forever × all tenants). core.prune_events_with_authority is the ONE
controlled erase: a SECURITY DEFINER, account-scoped DELETE of operational telemetry KINDS ('landed','push' by
default) older than a window edge. It is DISTINCT FROM TAMPERING:

  • old 'landed' telemetry past the window is pruned; recent telemetry is KEPT.
  • curated EFFECT records ('warn_issued'/'collision_held') are NOT pruned by the default (they feed
    effect_surface); they go only if explicitly named.
  • the 'statement' records are a SEPARATE table and are NEVER touched (and 'statement' is stripped from the
    kind list defensively, so it can't be named into the prune of core.event).
  • a PLAIN (non-gated) DELETE on core.event is STILL refused — append-only / the append-only guarantee intact.
  • cross-account: the prune only affects the CALLER'S account; another tenant's telemetry is untouched.

Run:  python3 tests/test_retention.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
# Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_server.py.
DB = "veripsa_retentiontest_" + str(os.getpid())
REPO = "acme/retention"


def conn_for(role):
    return psycopg2.connect(f"postgresql://{role}@localhost/{DB}")


def run_as(role, sql, args=(), fetch=True):
    """Run one statement in its own txn; return the first column of the first row (or None)."""
    conn = conn_for(role)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            if fetch:
                row = cur.fetchone()
                return row[0] if row else None
            return None
    finally:
        conn.close()


def owner_count(account, sql_tail, args=()):
    """Read raw rows as the migrator (table owner) with RLS pinned to `account`. Buyer roles have NO direct
    table SELECT (reads go through surfaces), so verification of the ledger uses the owner with the account
    pinned — and FORCE RLS still walls the owner to exactly that account, which is what we want to assert."""
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cur.execute(sql_tail, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def owner_rows(account, sql_tail, args=()):
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (account,))
            cur.execute(sql_tail, args)
            return cur.fetchall()
    finally:
        conn.close()


def expect_refused(role, sql, args=(), pin_account=None):
    """A statement that MUST raise (the gate refusing it). Returns the error text, or None if it wrongly passed.
    pin_account (only meaningful for the owner role) pins core.current_account so RLS admits the statement and
    the refusal we observe is the TRIGGER, not a missing-grant / RLS denial."""
    conn = conn_for(role)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            if pin_account is not None:
                cur.execute("SELECT set_config('core.current_account', %s, true)", (pin_account,))
            cur.execute(sql, args)
        conn.commit()
        return None  # it did NOT raise → the refusal failed
    except psycopg2.Error as e:
        conn.rollback()
        return str(e)
    finally:
        conn.close()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    checks = []

    # ── seed the DEMO account's ledger as the migrator (owner-bypass), so we control occurred_at precisely.
    # 'old' telemetry is 60 days back; 'recent' telemetry is 1 day back; one curated effect record; one
    # statement (separate table). The migrator arms the gate tokens exactly like the gate fns do.
    seed = """
    SET search_path=core;
    SELECT set_config('core.current_account','ACCT-DEMO', true);
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,occurred_at) VALUES
      ('EV-OLD-LAND-1','ACCT-DEMO','landed','AG-A',%(repo)s,'main','a.py', now()-interval '60 days'),
      ('EV-OLD-LAND-2','ACCT-DEMO','landed','AG-A',%(repo)s,'main','b.py', now()-interval '45 days'),
      ('EV-OLD-PUSH-1','ACCT-DEMO','push','AG-A',%(repo)s,'main','',      now()-interval '60 days');
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,occurred_at) VALUES
      ('EV-NEW-LAND-1','ACCT-DEMO','landed','AG-A',%(repo)s,'main','c.py', now()-interval '1 day'),
      ('EV-NEW-PUSH-1','ACCT-DEMO','push','AG-A',%(repo)s,'main','',      now()-interval '1 day');
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,counterparty_agent,repo,branch,path,occurred_at) VALUES
      ('EV-OLD-WARN-1','ACCT-DEMO','warn_issued','AG-A','AG-B',%(repo)s,'main','a.py', now()-interval '60 days'),
      ('EV-OLD-HELD-1','ACCT-DEMO','collision_held','AG-A','AG-B',%(repo)s,'main','b.py', now()-interval '60 days');
    SELECT core.mark_governed_write('statement');
    INSERT INTO core.statement(statement_id,account_id,agent_id,utterance,about_repo,about_branch,about_path,stated_at)
      VALUES ('ST-OLD-1','ACCT-DEMO','AG-A','this module owns auth',%(repo)s,'main','a.py', now()-interval '60 days');
    """
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(seed, {"repo": REPO})
    finally:
        conn.close()

    def demo_events_by_kind():
        rows = owner_rows("ACCT-DEMO",
                          "SELECT kind, count(*)::int FROM core.event GROUP BY kind ORDER BY kind")
        return dict(rows)

    # sanity: the seeded ACCT-DEMO ledger (owner-read, RLS-pinned to ACCT-DEMO) holds the full set.
    before = demo_events_by_kind()
    statements_before = owner_count("ACCT-DEMO", "SELECT count(*)::int FROM core.statement")
    checks.append((f"seed: ledger has old+new telemetry + curated effect records (kinds={before})",
                   before.get("landed") == 3 and before.get("push") == 2
                   and before.get("warn_issued") == 1 and before.get("collision_held") == 1))
    checks.append((f"seed: one statement in the separate stream (n={statements_before})", statements_before == 1))

    # ── (1) a PLAIN, non-gated DELETE on core.event is STILL refused by the APPEND-ONLY trigger (tamper-
    # evidence intact). Run it as the table OWNER (migrator) with the account pinned, so RLS admits the
    # statement and the ONLY thing that can refuse it is the assert_append_only trigger (no retention token
    # armed) — this proves the trigger, not just a missing grant, blocks the erase.
    err = expect_refused("veripsa_migrator", "DELETE FROM core.event WHERE event_id='EV-NEW-LAND-1'",
                         pin_account="ACCT-DEMO")
    checks.append((f"plain (non-gated) DELETE on core.event is refused by the append-only trigger: "
                   f"{(err or 'NOT REFUSED')[:70]}",
                   err is not None and "append-only" in err))
    # AND a buyer seat has no direct table access either (defense in depth: no DELETE grant at all).
    err_seat = expect_refused("veripsa_demo_agent", "DELETE FROM core.event WHERE event_id='EV-NEW-LAND-1'")
    checks.append((f"a buyer seat cannot touch core.event directly at all: {(err_seat or 'NOT REFUSED')[:60]}",
                   err_seat is not None))
    # the row must survive both attempts
    survived = owner_count("ACCT-DEMO",
                           "SELECT count(*)::int FROM core.event WHERE event_id='EV-NEW-LAND-1'")
    checks.append(("the row the plain DELETEs tried to remove still exists", survived == 1))

    # ── (2) the gated prune is App/cron-DELEGATED, not a buyer-seat operation. retention is run by the host's
    # scheduled job (render.yaml veripsa-retention cron → github-app/retention_prune.py), which connects as the
    # least-privilege service identity veripsa_app — the SAME role the server uses. A plain buyer WRITE seat
    # (veripsa_demo_agent) is NOT a retention caller: #108/#193 stripped the CREATE-FUNCTION PUBLIC-EXECUTE
    # default and grant prune_events_with_authority ONLY to veripsa_app + the owner seat veripsa_demo_steward,
    # so a buyer agent is refused at the GRANT layer (asserted just below — the security intent of the revoke).
    err_agent = expect_refused("veripsa_demo_agent",
                               "SELECT core.prune_events_with_authority(now()-interval '30 days')")
    checks.append((f"a buyer WRITE seat cannot run the prune (retention is App/cron-delegated; PUBLIC stripped): "
                   f"{(err_agent or 'NOT REFUSED')[:60]}",
                   err_agent is not None and "permission denied" in err_agent))
    # the gated prune, run as the delegated retention identity (veripsa_app): default kinds, window edge at 30
    # days ago. Old 'landed'/'push' go; recent stay. veripsa_app resolves to its OWN account (ACCT-DEMO here),
    # so the prune is account-scoped exactly as in prod.
    res = run_as("veripsa_app",
                 "SELECT core.prune_events_with_authority(now()-interval '30 days')")
    res = res if isinstance(res, dict) else json.loads(res)
    checks.append((f"gated prune ran (pruned={res.get('pruned')}, kinds={res.get('kinds')})",
                   bool(res.get("ok")) and res.get("pruned") == 3))  # 2 old landed + 1 old push

    after = demo_events_by_kind()
    checks.append((f"old telemetry pruned, RECENT telemetry KEPT (after={after})",
                   after.get("landed") == 1 and after.get("push") == 1))
    checks.append(("recent 'landed' (1d old) survived the 30d-window prune",
                   owner_count("ACCT-DEMO",
                               "SELECT count(*)::int FROM core.event WHERE event_id='EV-NEW-LAND-1'") == 1))
    checks.append(("the old 'landed' past the window is gone",
                   owner_count("ACCT-DEMO",
                               "SELECT count(*)::int FROM core.event WHERE event_id='EV-OLD-LAND-1'") == 0))

    # ── (3) curated EFFECT records are NOT pruned by the default (they feed effect_surface).
    checks.append((f"curated effect records untouched by the default prune "
                   f"(warn_issued={after.get('warn_issued')}, collision_held={after.get('collision_held')})",
                   after.get("warn_issued") == 1 and after.get("collision_held") == 1))

    # ── (4) statements are NEVER pruned — separate table, and 'statement' is stripped even if named.
    statements_after = owner_count("ACCT-DEMO", "SELECT count(*)::int FROM core.statement")
    checks.append(("the statement stream is untouched after the prune", statements_after == 1))

    # run as the delegated retention identity (veripsa_app) so we reach the IN-FUNCTION guard — the refusal we
    # assert is the "statement is never prunable" logic, not a missing grant (a buyer seat would 42501 first).
    err_stmt = expect_refused("veripsa_app",
                              "SELECT core.prune_events_with_authority(now()-interval '30 days', ARRAY['statement'])")
    checks.append((f"naming only 'statement' is refused (statement is never prunable): {(err_stmt or 'NOT REFUSED')[:70]}",
                   err_stmt is not None and "never prunable" in err_stmt))

    # ── (5) CROSS-ACCOUNT: the prune only affects the CALLER's account. Provision a 2nd tenant + seat,
    # seed it with old telemetry, prune as the FIRST tenant, and confirm the 2nd tenant is untouched.
    prov = """
    SET search_path=core;
    SELECT core.provision_seat('ACCT-ACME','Acme Inc','AG-ACME','acme-writer','veripsa_acme_agent');
    SELECT set_config('core.current_account','ACCT-ACME', true);
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,occurred_at) VALUES
      ('EV-ACME-OLD-1','ACCT-ACME','landed','AG-ACME','acme/x','main','z.py', now()-interval '60 days');
    """
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(prov)
    finally:
        conn.close()

    acme_before = owner_count("ACCT-ACME", "SELECT count(*)::int FROM core.event")
    # the App (resolving to ACCT-DEMO) prunes again with a wide window — the per-account prune must NOT reach
    # into ACCT-ACME (it is scoped to the CALLER's resolved account, not a caller-supplied arg).
    run_as("veripsa_app", "SELECT core.prune_events_with_authority(now())")
    acme_after = owner_count("ACCT-ACME", "SELECT count(*)::int FROM core.event")
    checks.append((f"cross-account: ACCT-ACME's old telemetry survives a per-account prune scoped to ACCT-DEMO "
                   f"(before={acme_before}, after={acme_after})",
                   acme_before == 1 and acme_after == 1))

    # ── (6) the STEWARD seat (granted) can prune its own account too.
    res3 = run_as("veripsa_demo_steward", "SELECT core.prune_events_with_authority(now()-interval '30 days')")
    res3 = res3 if isinstance(res3, dict) else json.loads(res3)
    checks.append((f"steward seat can run the gated prune (ok={res3.get('ok')})", bool(res3.get("ok"))))

    # ── (7) THE OPERATOR'S CROSS-TENANT SWEEP (prune_all_accounts_with_authority) — the call the scheduled
    # retention job actually makes. The per-account prune only touches the CALLER's account, so the HOST needs a
    # single call that sweeps EVERY tenant while keeping each DELETE token-pinned to its own rows. Re-seed BOTH
    # tenants (ACCT-DEMO + ACCT-ACME) with fresh OLD + RECENT telemetry, then run the all-accounts sweep ONCE as
    # veripsa_app (the host service identity) and assert: every account's OLD telemetry pruned, RECENT kept,
    # curated records untouched, statements untouched — across BOTH tenants from one call.
    reseed = """
    SET search_path=core;
    -- register both tenants in the installation→account map (no RLS — the cross-account routing table), exactly
    -- as enter_installation_with_authority does on a real tenant's first event. The operator sweep enumerates
    -- tenants from here (the one RLS-free place it can see the full list).
    INSERT INTO core.installation_account(installation_id, account_id) VALUES
      ('inst-demo','ACCT-DEMO'),('inst-acme','ACCT-ACME') ON CONFLICT (installation_id) DO NOTHING;
    SELECT set_config('core.current_account','ACCT-DEMO', true);
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,occurred_at) VALUES
      ('EV-SWEEP-DEMO-OLD','ACCT-DEMO','landed','AG-A',%(repo)s,'main','old.py', now()-interval '60 days'),
      ('EV-SWEEP-DEMO-NEW','ACCT-DEMO','landed','AG-A',%(repo)s,'main','new.py', now()-interval '1 day');
    SELECT set_config('core.current_account','ACCT-ACME', true);
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,occurred_at) VALUES
      ('EV-SWEEP-ACME-OLD','ACCT-ACME','push','AG-ACME','acme/x','main','', now()-interval '60 days'),
      ('EV-SWEEP-ACME-NEW','ACCT-ACME','push','AG-ACME','acme/x','main','', now()-interval '1 day');
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,counterparty_agent,repo,branch,path,occurred_at) VALUES
      ('EV-SWEEP-ACME-WARN','ACCT-ACME','warn_issued','AG-ACME','AG-B','acme/x','main','y.py', now()-interval '60 days');
    """
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(reseed, {"repo": REPO})
    finally:
        conn.close()

    # the operator sweep is granted to veripsa_app (the host service role), NOT a buyer seat. Run it as the App.
    sweep = run_as("veripsa_app", "SELECT core.prune_all_accounts_with_authority(now()-interval '30 days')")
    sweep = sweep if isinstance(sweep, dict) else json.loads(sweep)
    # pruned == 3: the 2 freshly-seeded old rows (1 demo + 1 acme) PLUS the check-(5) leftover EV-ACME-OLD-1
    # (it survived ACCT-DEMO's earlier per-account prune; the cross-tenant sweep correctly reaches it now). The
    # per-account safety is unchanged — the count just confirms the sweep reaches EVERY tenant's old telemetry.
    checks.append((f"operator sweep ran across all tenants (ok={sweep.get('ok')}, accounts={sweep.get('accounts')}, "
                   f"pruned={sweep.get('pruned')})",
                   bool(sweep.get("ok")) and sweep.get("accounts") >= 2 and sweep.get("pruned") == 3))
    # BOTH tenants: old gone, recent kept — from the ONE sweep call.
    checks.append(("operator sweep: ACCT-DEMO old telemetry gone, recent kept",
                   owner_count("ACCT-DEMO", "SELECT count(*)::int FROM core.event WHERE event_id='EV-SWEEP-DEMO-OLD'") == 0
                   and owner_count("ACCT-DEMO", "SELECT count(*)::int FROM core.event WHERE event_id='EV-SWEEP-DEMO-NEW'") == 1))
    checks.append(("operator sweep: ACCT-ACME old telemetry gone, recent kept (a SECOND tenant, one call)",
                   owner_count("ACCT-ACME", "SELECT count(*)::int FROM core.event WHERE event_id='EV-SWEEP-ACME-OLD'") == 0
                   and owner_count("ACCT-ACME", "SELECT count(*)::int FROM core.event WHERE event_id='EV-SWEEP-ACME-NEW'") == 1))
    # curated effect record + statements are NOT touched by the default-kind sweep.
    checks.append(("operator sweep: a curated effect record (warn_issued) is left immutable",
                   owner_count("ACCT-ACME", "SELECT count(*)::int FROM core.event WHERE event_id='EV-SWEEP-ACME-WARN'") == 1))
    checks.append(("operator sweep: the statement stream is untouched",
                   owner_count("ACCT-DEMO", "SELECT count(*)::int FROM core.statement") == 1))
    # a buyer seat CANNOT run the cross-tenant sweep (it is granted to veripsa_app only).
    err_sweep = expect_refused("veripsa_demo_agent", "SELECT core.prune_all_accounts_with_authority(now())")
    checks.append((f"a buyer seat cannot run the cross-tenant operator sweep (granted to veripsa_app only): "
                   f"{(err_sweep or 'NOT REFUSED')[:60]}", err_sweep is not None))

    # ── (8) DEAD-CLAIM SWEEP (audit2/scale): the operator sweep ALSO prunes terminal (released/expired) claim
    # rows past the window — the second unbounded grower. A landed/withdrawn/crashed PR's per-file claims are
    # only FLIPPED to released/expired, never deleted (measured ~450 B/row → MB/day of permanent bloat on the
    # 256 MiB tier). The sweep drops ONLY old terminal claims; it must NEVER touch an active/waiting lane or a
    # recently-concluded one. Seed under ACCT-DEMO (already a registered tenant) directly as the owner.
    claim_seed = """
    SET search_path=core;
    SELECT set_config('core.current_account','ACCT-DEMO', true);
    SELECT core.mark_governed_write('claim');
    INSERT INTO core.claim(claim_id,account_id,agent_id,change_id,repo,branch,target_path,claim_state,claimed_at,released_at,lease_expires_at) VALUES
      ('CLM-OLD-REL','ACCT-DEMO','AG-A','PR-91',%(repo)s,'main','old_rel.py','released', now()-interval '60 days', now()-interval '58 days', now()),
      ('CLM-OLD-EXP','ACCT-DEMO','AG-A','PR-92',%(repo)s,'main','old_exp.py','expired',  now()-interval '60 days', now()-interval '58 days', now()),
      ('CLM-NEW-REL','ACCT-DEMO','AG-A','PR-93',%(repo)s,'main','new_rel.py','released', now()-interval '2 days',  now()-interval '1 day',   now()),
      ('CLM-ACTIVE', 'ACCT-DEMO','AG-A','PR-94',%(repo)s,'main','live_a.py', 'active',   now()-interval '60 days', NULL, now()+interval '30 min'),
      ('CLM-WAITING','ACCT-DEMO','AG-B','PR-95',%(repo)s,'main','live_a.py', 'waiting',  now()-interval '60 days', NULL, now()+interval '30 min');
    """
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(claim_seed, {"repo": REPO})
    finally:
        conn.close()
    claim_sweep = run_as("veripsa_app", "SELECT core.prune_all_accounts_with_authority(now()-interval '30 days')")
    claim_sweep = claim_sweep if isinstance(claim_sweep, dict) else json.loads(claim_sweep)

    def demo_claim(claim_id):
        return owner_count("ACCT-DEMO", "SELECT count(*)::int FROM core.claim WHERE claim_id=%s", (claim_id,))

    checks.append((f"dead-claim sweep: 2 old terminal claims pruned (dead_claims_pruned={claim_sweep.get('dead_claims_pruned')})",
                   claim_sweep.get("dead_claims_pruned") == 2))
    checks.append(("dead-claim sweep: an OLD released claim past the window is gone", demo_claim("CLM-OLD-REL") == 0))
    checks.append(("dead-claim sweep: an OLD expired claim past the window is gone", demo_claim("CLM-OLD-EXP") == 0))
    checks.append(("dead-claim sweep: a RECENT released claim (inside the window) is KEPT", demo_claim("CLM-NEW-REL") == 1))
    checks.append(("dead-claim sweep: an ACTIVE lane is NEVER pruned (even if old)", demo_claim("CLM-ACTIVE") == 1))
    checks.append(("dead-claim sweep: a WAITING lane is NEVER pruned (even if old)", demo_claim("CLM-WAITING") == 1))

    # ── (9) SPENT-PREDICTION SWEEP (audit/backup-restore-retention): the THIRD unbounded grower. 'prediction'
    # is an append-only event KIND (one-per-change, record_prediction_with_authority) NOT in the default
    # telemetry kinds, so the event prune never touches it; its ONLY reader is the close-time answer-check
    # (record_advice_outcome_with_authority). Once a matching 'advice_outcome' is on record for the SAME
    # coordinate, the prediction is SPENT (never read again) and must be pruned past the window to bound growth;
    # but a prediction whose PR is still OPEN (no outcome yet) MUST survive regardless of age so the eventual
    # close-time join never misses. Seed under ACCT-DEMO (a registered tenant) directly as the owner:
    #   • SPENT-OLD  : old prediction + a matching advice_outcome → must be PRUNED.
    #   • OPEN-OLD   : old prediction, NO outcome (PR still open) → must SURVIVE (the long-lived-PR / skew edge).
    #   • SPENT-NEW  : recent prediction + outcome, inside the window → must SURVIVE (not past the window yet).
    pred_seed = """
    SET search_path=core;
    SELECT set_config('core.current_account','ACCT-DEMO', true);
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,detail,occurred_at) VALUES
      ('EV-PRED-SPENT-OLD','ACCT-DEMO','prediction','AG-A',%(repo)s,'main','PR-201','verdict=warn;behind=', now()-interval '60 days'),
      ('EV-OUT-FOR-201',   'ACCT-DEMO','advice_outcome','AG-A',%(repo)s,'main','PR-201','pred=warn;adv=followed;land=clean;conf=observed', now()-interval '58 days'),
      ('EV-PRED-OPEN-OLD', 'ACCT-DEMO','prediction','AG-A',%(repo)s,'main','PR-202','verdict=serialize;behind=', now()-interval '60 days'),
      ('EV-PRED-SPENT-NEW','ACCT-DEMO','prediction','AG-A',%(repo)s,'main','PR-203','verdict=clear;behind=', now()-interval '2 days'),
      ('EV-OUT-FOR-203',   'ACCT-DEMO','advice_outcome','AG-A',%(repo)s,'main','PR-203','pred=clear;adv=followed;land=clean;conf=inferred', now()-interval '1 day');
    """
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(pred_seed, {"repo": REPO})
    finally:
        conn.close()
    pred_sweep = run_as("veripsa_app", "SELECT core.prune_all_accounts_with_authority(now()-interval '30 days')")
    pred_sweep = pred_sweep if isinstance(pred_sweep, dict) else json.loads(pred_sweep)

    def demo_evt(event_id):
        return owner_count("ACCT-DEMO", "SELECT count(*)::int FROM core.event WHERE event_id=%s", (event_id,))

    checks.append((f"spent-prediction sweep: exactly the 1 SPENT-OLD prediction pruned "
                   f"(spent_predictions_pruned={pred_sweep.get('spent_predictions_pruned')})",
                   pred_sweep.get("spent_predictions_pruned") == 1))
    checks.append(("spent-prediction sweep: a SPENT prediction past the window (its PR closed = has an "
                   "advice_outcome) is PRUNED", demo_evt("EV-PRED-SPENT-OLD") == 0))
    checks.append(("spent-prediction sweep: an OPEN prediction (NO outcome yet) SURVIVES even when older than "
                   "the window — the close-time join must never miss", demo_evt("EV-PRED-OPEN-OLD") == 1))
    checks.append(("spent-prediction sweep: a RECENT prediction inside the window is KEPT (not past the window)",
                   demo_evt("EV-PRED-SPENT-NEW") == 1))
    checks.append(("spent-prediction sweep: the matching advice_outcome rows are NEVER pruned (the lifetime "
                   "answer-check surface reads ALL of them)",
                   demo_evt("EV-OUT-FOR-201") == 1 and demo_evt("EV-OUT-FOR-203") == 1))

    # ── (10) COLD-REPO WORKING-SET SWEEP (PO 2026-07-07): inactive repos do not need their rebuildable graph
    # kept warm forever. The operator sweep may delete only derived structural state for a repo that has no
    # activity past the retention edge and no active/waiting claim. A later push/PR cold-starts/self-heals the
    # graph again, so durable records stay while DB/storage cost is bounded.
    cold_repo = "acme/cold-retention"
    orphan_repo = "acme/orphan-cache-retention"
    hot_repo = "acme/hot-retention"
    live_repo = "acme/live-claim-retention"
    cold_seed = """
    SET search_path=core;
    -- Regression: ACCT-DEMO is a dogfood/credential account here, not a routed GitHub installation tenant.
    -- The all-account retention sweep must still reach its inactive working sets through core.credential.
    DELETE FROM core.installation_account WHERE account_id='ACCT-DEMO';
    SELECT set_config('core.current_account','ACCT-DEMO', true);
    SELECT core.mark_governed_write('code_node');
    INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path,name,language) VALUES
      ('ACCT-DEMO',%(cold)s,'main','cold.py','file','cold.py',NULL,'python'),
      ('ACCT-DEMO',%(hot)s,'main','hot.py','file','hot.py',NULL,'python'),
      ('ACCT-DEMO',%(live)s,'main','live.py','file','live.py',NULL,'python');
    SELECT core.mark_governed_write('code_edge');
    INSERT INTO core.code_edge(account_id,repo,branch,src,dst,edge_kind) VALUES
      ('ACCT-DEMO',%(cold)s,'main','cold.py','cold.py#fn','contains'),
      ('ACCT-DEMO',%(hot)s,'main','hot.py','hot.py#fn','contains'),
      ('ACCT-DEMO',%(live)s,'main','live.py','live.py#fn','contains');
    SELECT core.mark_governed_write('graph_version');
    INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,captured_at,ingested_at,node_count,edge_count) VALUES
      ('ACCT-DEMO',%(cold)s,'main','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',now()-interval '60 days',now()-interval '60 days',1,1),
      ('ACCT-DEMO',%(hot)s,'main','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',now()-interval '60 days',now()-interval '60 days',1,1),
      ('ACCT-DEMO',%(live)s,'main','cccccccccccccccccccccccccccccccccccccccc',now()-interval '60 days',now()-interval '60 days',1,1);
    SELECT core.mark_governed_write('co_change');
    INSERT INTO core.co_change(account_id,repo,path_a,path_b,co,n_a,n_b,strength,lift,n_total) VALUES
      ('ACCT-DEMO',%(cold)s,'a.py','b.py',5,7,8,0.7,2.5,40),
      ('ACCT-DEMO',%(orphan)s,'a.py','b.py',5,7,8,0.7,2.5,40),
      ('ACCT-DEMO',%(hot)s,'a.py','b.py',5,7,8,0.7,2.5,40),
      ('ACCT-DEMO',%(live)s,'a.py','b.py',5,7,8,0.7,2.5,40);
    SELECT core.mark_governed_write('co_change_seen_commit');
    INSERT INTO core.co_change_seen_commit(account_id,repo,commit_sha) VALUES
      ('ACCT-DEMO',%(cold)s,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'),
      ('ACCT-DEMO',%(orphan)s,'dddddddddddddddddddddddddddddddddddddddd'),
      ('ACCT-DEMO',%(hot)s,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'),
      ('ACCT-DEMO',%(live)s,'cccccccccccccccccccccccccccccccccccccccc');
    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,occurred_at) VALUES
      ('EV-HOT-RECENT','ACCT-DEMO','push','AG-A',%(hot)s,'main','',now()-interval '1 day');
    SELECT core.mark_governed_write('claim');
    INSERT INTO core.claim(claim_id,account_id,agent_id,change_id,repo,branch,target_path,claim_state,claimed_at,lease_expires_at) VALUES
      ('CLM-LIVE-REPO','ACCT-DEMO','AG-A','PR-301',%(live)s,'main','live.py','active',now()-interval '60 days',now()+interval '30 min');
    """
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(cold_seed, {"cold": cold_repo, "orphan": orphan_repo, "hot": hot_repo, "live": live_repo})
    finally:
        conn.close()
    demo_routes = owner_count("ACCT-DEMO", "SELECT count(*)::int FROM core.installation_account WHERE account_id='ACCT-DEMO'")
    cold_sweep = run_as("veripsa_app", "SELECT core.prune_all_accounts_with_authority(now()-interval '30 days')")
    cold_sweep = cold_sweep if isinstance(cold_sweep, dict) else json.loads(cold_sweep)

    def graph_versions(repo):
        return owner_count("ACCT-DEMO", "SELECT count(*)::int FROM core.graph_version WHERE repo=%s", (repo,))

    def graph_nodes(repo):
        return owner_count("ACCT-DEMO", "SELECT count(*)::int FROM core.code_node WHERE repo=%s", (repo,))

    def cochange_rows(repo):
        return owner_count("ACCT-DEMO", "SELECT count(*)::int FROM core.co_change WHERE repo=%s", (repo,))

    checks.append((f"cold-repo sweep: exactly 2 inactive repo working sets pruned "
                   f"(cold_repos_pruned={cold_sweep.get('cold_repos_pruned')})",
                   cold_sweep.get("cold_repos_pruned") == 2))
    checks.append(("cold-repo sweep: credential-only dogfood account has no installation route",
                   demo_routes == 0))
    checks.append(("cold-repo sweep: inactive repo graph is gone (rebuildable on next push/PR)",
                   graph_versions(cold_repo) == 0 and graph_nodes(cold_repo) == 0 and cochange_rows(cold_repo) == 0))
    checks.append(("cold-repo sweep: orphan cache without graph_version is also gone",
                   cochange_rows(orphan_repo) == 0))
    checks.append(("cold-repo sweep: repo with recent activity stays warm",
                   graph_versions(hot_repo) == 1 and graph_nodes(hot_repo) == 1 and cochange_rows(hot_repo) == 1))
    checks.append(("cold-repo sweep: repo with an active lane is NEVER pruned even if old",
                   graph_versions(live_repo) == 1 and graph_nodes(live_repo) == 1 and cochange_rows(live_repo) == 1))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("RETENTION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
