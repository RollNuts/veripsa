#!/usr/bin/env python3
"""Offboarding / data-rights gate — the FORGET / KEEP contract, end to end, with the NEW answer-check ledger.

WHY THIS GATE EXISTS (the coverage gap it closes): the offboarding mechanism is sound and already partly
tested — test_server.py proves a plain uninstall purges the working set + retains the ledger, and
test_account_erasure.py proves the full GDPR Art.17 hard-delete. But BOTH of those tests pre-date the
answer-check ledger (the 'prediction' + 'advice_outcome' event KINDS, 40_surfaces.sql) and NEITHER seeds those
kinds. So the privacy-critical question — "when a tenant offboards, does Veripsa FORGET the answer-check rows it
must and KEEP only what is defensible?" — was asserted only IMPLICITLY (erase_account's DELETE has no kind
filter, so it *happens* to take them; purge_repo *happens* to retain them). A future change that special-cased
event deletion by kind could silently leave prediction/advice_outcome ORPHANS under a purged/erased tenant's
coordinate, and no gate would catch it. This gate makes the contract EXPLICIT and regression-proof by injecting
the FULL lifecycle with the answer-check kinds front and centre and asserting, per data class:

  ── REPOSITORY REMOVAL (durable core.offboard_repository_with_authority boundary) ──
  FORGET the content-free WORKING SET of the removed repo, across all its coordinates:
    • code_node / code_edge / graph_version  → 0
    • claim (live lane state)                → 0
  KEEP (retained by design = the content-free, public-git-equivalent operational/answer-check audit ledger):
    • core.event for that repo (landed/push + prediction + advice_outcome) is UNTOUCHED, and is content-free
      (path = a change ref / file path, detail = bounded verdict tokens — never code/bodies).
  DO NOT OVER-DELETE: a DIFFERENT repo in the SAME tenant keeps its whole working set + ledger.
  PRIVACY INVARIANT (per-repo): the purged repo has claims = 0 AND no stray graph rows under ANY branch.

  ── RE-INSTALL (re-onboard the same repo after a purge) ──
  CLEAN: re-ingesting the same repo's graph resurrects NO stale claim and produces exactly the fresh graph —
  the purge left nothing behind for a reinstall to inherit.

  ── DATA-EXPORT (content-free portability / DSAR — Art.20) ──
  DERIVABLE: core.export_durable_rows_with_authority(<account>) returns THIS tenant's content-free durable rows
  — incl. its prediction + advice_outcome answer-check rows — and NOTHING from another tenant.

  ── FULL ERASURE (core.erase_account_with_authority, GDPR Art.17 / "delete ALL my data") ──
  FORGET EVERYTHING for the tenant, EXPLICITLY incl. the answer-check ledger:
    • core.event rows of kind 'prediction' AND 'advice_outcome' for the account → 0 (named, not just summed)
    • the whole per-account footprint → 0 (working set + immutable streams + identity + the account row)
  PRIVACY INVARIANT (account-wide): NO orphan tenant data survives under ANY coordinate — claims = 0, graph = 0,
  prediction = 0, advice_outcome = 0, and a fresh export carries ZERO rows for the erased account.
  NEIGHBOUR UNTOUCHED: a second tenant's answer-check ledger + working set are entirely unaffected and its
  ledger is STILL append-only (the erase never weakened the moat).

Run:  python3 tests/test_offboarding.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe), exactly like db/smoke.sh / run_gates / test_account_erasure: the gate
# bootstraps + drops this DB, so a FIXED name would let concurrent runs drop each other's DB mid-run.
DB = "veripsa_offboardtest_" + str(os.getpid())
REPO = "acme/offboard"          # the repo we will REMOVE (purge) and later RE-INSTALL
KEEP_REPO = "acme/stays"        # a SECOND repo in the SAME tenant — must NOT be over-deleted by a repo purge


def conn_for(role):
    return psycopg2.connect(f"postgresql://{role}@localhost/{DB}")


def run_as(role, sql, args=(), fetch=True):
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
    """Read as the migrator (table owner) with RLS pinned to `account` (FORCE RLS still walls the owner to
    exactly that account — which is what we assert)."""
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


def export_rows(role, account):
    """The content-free durable export for ONE account (DSAR / portability). Returns the list of row-objects."""
    conn = conn_for(role)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.export_durable_rows_with_authority(%s)", (account,))
            out = []
            for (row,) in cur.fetchall():
                out.append(row if isinstance(row, dict) else json.loads(row))
            return out
    finally:
        conn.close()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    checks = []

    # ── provision TWO tenants. ACCT-DEMO is the offboarding tenant (bootstrap already gave veripsa_app a
    # credential → ACCT-DEMO, so the App service identity resolves there for purge/erase/record). ACCT-NEIGH is
    # the RETAINED neighbour whose answer-check ledger + working set must stay entirely untouched.
    run_as("veripsa_migrator",
           "SELECT core.provision_seat('ACCT-NEIGH','Neighbour Inc','AG-N','neigh-writer','veripsa_neigh_agent')")
    # register BOTH tenants in the installation→account routing map (what a real install does on first event).
    # The data-export + retention sweep enumerate tenants via this map, so without it ACCT-DEMO is undiscoverable.
    run_as("veripsa_migrator",
           "INSERT INTO core.installation_account(installation_id, account_id) VALUES "
           "('inst-demo','ACCT-DEMO'),('inst-neigh','ACCT-NEIGH') ON CONFLICT (installation_id) DO NOTHING",
           fetch=False)

    # ── SEED ACCT-DEMO. Two repos: REPO (to be purged) and KEEP_REPO (must survive a per-repo purge). For EACH,
    # the FULL footprint: a code graph, a live claim, a landing event (operational telemetry), AND the NEW
    # answer-check ledger — a 'prediction' and an 'advice_outcome' (recorded as the App, content-free).
    # ingest_graph parses nodes by {id,kind,path} and edges by {src,dst,kind} (30_gate.sql) — content-free.
    graph = {"nodes": [{"id": "auth.py", "kind": "file", "path": "auth.py"}],
             "edges": [{"src": "auth.py", "dst": "auth.py", "kind": "contains"}]}
    app = lambda sql, args=(): run_as("veripsa_app", sql, args)  # noqa: E731  (the App service identity → ACCT-DEMO)
    for rp in (REPO, KEEP_REPO):
        app("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), rp, "main", "a" * 40))
        app("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
            (f"PR-1:{rp}", "auth.py", rp, "main", "dev"))
        app("SELECT core.record_landing_with_authority(%s,%s,%s,%s,%s)",
            (rp, "main", "b" * 40, ["auth.py"], "dev"))
        # THE NEW ANSWER-CHECK LEDGER (the thing the pre-existing offboarding tests never seed):
        app("SELECT core.record_prediction_with_authority(%s,%s,%s,%s,%s)",
            (f"PR-1:{rp}", rp, "main", "serialize", ["PR-0"]))
        app("SELECT core.record_advice_outcome_with_authority(%s,%s,%s,%s,%s,%s)",
            (f"PR-1:{rp}", rp, "main", True, False, "observed"))

    # Generic graph writes deliberately root repository identity at NULL. The live signed-App path re-stamps the
    # authenticated stable id in the same transaction; mirror that proof here so this gate exercises exact-id
    # removal instead of asking a delayed name-only event to delete an ambiguous/same-name replacement graph.
    app("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (REPO, "29001"))

    # ── neighbour footprint: its own answer-check ledger + a live claim, on its own repo.
    neigh = lambda sql, args=(): run_as("veripsa_neigh_agent", sql, args)  # noqa: E731
    # the buyer seat cannot call the App-only record fns; seed the neighbour's ledger as the App but pinned to
    # ACCT-NEIGH via its own installation. Simpler + identity-correct: seed neighbour rows as the owner directly.
    run_as("veripsa_migrator", """
        SET search_path=core;
        SELECT set_config('core.current_account','ACCT-NEIGH', true);
        SELECT core.mark_governed_write('event');
        INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,detail) VALUES
          ('EV-N-PRED','ACCT-NEIGH','prediction','AG-N','neigh/x','main','PR-7','verdict=warn;behind='),
          ('EV-N-OUT','ACCT-NEIGH','advice_outcome','AG-N','neigh/x','main','PR-7','pred=warn;adv=followed;land=clean;conf=observed');
        SELECT core.mark_governed_write('claim');
        INSERT INTO core.claim(claim_id,account_id,agent_id,repo,branch,target_path)
          VALUES ('CLM-N','ACCT-NEIGH','AG-N','neigh/x','main','x.py');
        """, fetch=False)

    def demo_evt(kind, repo=None):
        sql = "SELECT count(*)::int FROM core.event WHERE account_id='ACCT-DEMO' AND kind=%s"
        args = [kind]
        if repo is not None:
            sql += " AND repo=%s"; args.append(repo)
        return owner_count("ACCT-DEMO", sql, tuple(args))

    def demo_working(table, repo):
        return owner_count("ACCT-DEMO",
                           f"SELECT count(*)::int FROM core.{table} WHERE repo=%s", (repo,))

    # ── seed assertions: the answer-check ledger IS present for BOTH demo repos before any offboarding.
    checks.append((f"seed: REPO has a prediction + an advice_outcome "
                   f"(pred={demo_evt('prediction', REPO)}, out={demo_evt('advice_outcome', REPO)})",
                   demo_evt('prediction', REPO) == 1 and demo_evt('advice_outcome', REPO) == 1))
    checks.append((f"seed: KEEP_REPO has its own answer-check ledger too "
                   f"(pred={demo_evt('prediction', KEEP_REPO)}, out={demo_evt('advice_outcome', KEEP_REPO)})",
                   demo_evt('prediction', KEEP_REPO) == 1 and demo_evt('advice_outcome', KEEP_REPO) == 1))
    checks.append((f"seed: REPO has a live working set (nodes={demo_working('code_node', REPO)}, "
                   f"claims={demo_working('claim', REPO)})",
                   demo_working('code_node', REPO) > 0 and demo_working('claim', REPO) > 0))

    # ════════════════════════════════════════════════════════════════════════════════════════════
    # (1) REPOSITORY REMOVAL → durable core.offboard_repository_with_authority(REPO,...).
    # FORGET the working set; KEEP the content-free ledger; never over-delete the OTHER repo.
    # ════════════════════════════════════════════════════════════════════════════════════════════
    pred_before = demo_evt('prediction', REPO); out_before = demo_evt('advice_outcome', REPO)
    app_purge_denied = False
    try:
        app("SELECT core.purge_repo_with_authority(%s)", (REPO,))
    except psycopg2.Error as exc:
        app_purge_denied = exc.pgcode == "42501"
    checks.append(("legacy App cannot bypass stable-id/durable offboarding with raw repo purge",
                   app_purge_denied))
    run_as("veripsa_migrator", """
        INSERT INTO core.webhook_delivery(
          delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at)
        VALUES ('D-DATA-RIGHTS-REMOVE','installation_repositories','ACCT-DEMO',NULL,
          jsonb_build_object('action','removed','repositories_removed',
            jsonb_build_array(jsonb_build_object('id','29001','full_name',%s))),
          'processing',1,clock_timestamp(),clock_timestamp())
        """, (REPO,), fetch=False)
    purge = app("SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
                (REPO, "29001", "installation_removed", "D-DATA-RIGHTS-REMOVE"))
    purge = purge if isinstance(purge, dict) else json.loads(purge)
    checks.append((f"purge ran for REPO (ok={purge.get('ok')}, repo={purge.get('repo')})",
                   bool(purge.get('ok')) and purge.get('repo') == REPO))

    # FORGET: the working set for REPO is gone across every coordinate.
    checks.append((f"purge FORGETS the working set: REPO graph + claims gone "
                   f"(nodes={demo_working('code_node', REPO)}, edges={demo_working('code_edge', REPO)}, "
                   f"versions={demo_working('graph_version', REPO)}, claims={demo_working('claim', REPO)})",
                   demo_working('code_node', REPO) == 0 and demo_working('code_edge', REPO) == 0
                   and demo_working('graph_version', REPO) == 0 and demo_working('claim', REPO) == 0))

    # PRIVACY INVARIANT (per-repo): the purged repo has NO claim and NO graph row under ANY branch.
    any_branch_claims = owner_count("ACCT-DEMO",
        "SELECT count(*)::int FROM core.claim WHERE repo=%s", (REPO,))
    any_branch_graph = owner_count("ACCT-DEMO",
        "SELECT (SELECT count(*) FROM core.code_node WHERE repo=%s)"
        "      +(SELECT count(*) FROM core.code_edge WHERE repo=%s)"
        "      +(SELECT count(*) FROM core.graph_version WHERE repo=%s)", (REPO, REPO, REPO))
    checks.append((f"privacy invariant after purge: REPO has 0 claims + 0 graph rows under ANY coordinate "
                   f"(claims={any_branch_claims}, graph={any_branch_graph})",
                   any_branch_claims == 0 and any_branch_graph == 0))

    # KEEP: the content-free ledger for REPO is RETAINED by design (operational + answer-check). This is the
    # defensible KEEP — public-git-equivalent metadata, no code/bodies.
    checks.append((f"purge KEEPS the content-free ledger for REPO: prediction + advice_outcome RETAINED "
                   f"(pred {pred_before}->{demo_evt('prediction', REPO)}, "
                   f"out {out_before}->{demo_evt('advice_outcome', REPO)})",
                   demo_evt('prediction', REPO) == pred_before == 1
                   and demo_evt('advice_outcome', REPO) == out_before == 1))
    # ASSERT the retained answer-check rows are CONTENT-FREE: the only payload is a bounded verdict-token detail
    # and a change-ref path — never code. (Detail matches the closed 'verdict=<token>;behind=<refs>' grammar.)
    bad_detail = owner_count("ACCT-DEMO",
        "SELECT count(*)::int FROM core.event WHERE account_id='ACCT-DEMO' AND repo=%s AND kind='prediction' "
        "AND detail !~ '^verdict=(clear|warn|serialize|serialize_soft|unknown);behind='", (REPO,))
    checks.append((f"the RETAINED prediction rows are content-free (bounded verdict grammar; non-conforming={bad_detail})",
                   bad_detail == 0))

    # DO NOT OVER-DELETE: the OTHER repo in the SAME tenant keeps its whole working set + answer-check ledger.
    checks.append((f"purge does NOT over-delete: KEEP_REPO working set + ledger intact "
                   f"(nodes={demo_working('code_node', KEEP_REPO)}, claims={demo_working('claim', KEEP_REPO)}, "
                   f"pred={demo_evt('prediction', KEEP_REPO)}, out={demo_evt('advice_outcome', KEEP_REPO)})",
                   demo_working('code_node', KEEP_REPO) > 0 and demo_working('claim', KEEP_REPO) > 0
                   and demo_evt('prediction', KEEP_REPO) == 1 and demo_evt('advice_outcome', KEEP_REPO) == 1))

    # ════════════════════════════════════════════════════════════════════════════════════════════
    # (2) RE-INSTALL (re-onboard the same repo after the purge). CLEAN: nothing stale resurrects.
    # ════════════════════════════════════════════════════════════════════════════════════════════
    app("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", "c" * 40))
    reinstall_claims = demo_working('claim', REPO)
    reinstall_nodes = demo_working('code_node', REPO)
    checks.append((f"re-install is CLEAN: re-ingest resurrects NO stale claim (claims={reinstall_claims}) and "
                   f"rebuilds exactly the fresh graph (nodes={reinstall_nodes})",
                   reinstall_claims == 0 and reinstall_nodes == len(graph["nodes"])))

    # ════════════════════════════════════════════════════════════════════════════════════════════
    # (3) DATA-EXPORT (content-free portability / DSAR). Derivable per-account, isolated from other tenants.
    # ════════════════════════════════════════════════════════════════════════════════════════════
    # Cross-tenant durable export is deliberately separated from the live App role. The migrator owns the gate in
    # this local fixture; production invokes the same function through the dedicated veripsa_backup principal.
    demo_export = export_rows("veripsa_migrator", "ACCT-DEMO")
    exp_kinds = {r.get("kind") for r in demo_export if r.get("_table") == "event"}
    exp_other_tenant = [r for r in demo_export if r.get("account_id") not in (None, "ACCT-DEMO")]
    checks.append((f"data-export is derivable + per-account: ACCT-DEMO export carries its answer-check rows "
                   f"(kinds in export={sorted(k for k in exp_kinds if k)}), and NOTHING from another tenant "
                   f"(stray rows={len(exp_other_tenant)})",
                   "prediction" in exp_kinds and "advice_outcome" in exp_kinds and len(exp_other_tenant) == 0))

    # ════════════════════════════════════════════════════════════════════════════════════════════
    # (4) FULL ERASURE → core.erase_account_with_authority() (GDPR Art.17). The answer-check ledger MUST go too.
    # ════════════════════════════════════════════════════════════════════════════════════════════
    erase = app("SELECT core.erase_account_with_authority()")
    erase = erase if isinstance(erase, dict) else json.loads(erase)
    checks.append((f"erase ran for ACCT-DEMO (ok={erase.get('ok')}, account={erase.get('account')})",
                   bool(erase.get('ok')) and erase.get('account') == 'ACCT-DEMO'))

    # EXPLICIT: the answer-check ledger is GONE (named by kind — the assertion the pre-existing erasure gate
    # never makes). This is the regression guard: even if a future change special-cased event deletion by kind,
    # this fails the moment prediction/advice_outcome are left behind.
    checks.append((f"erase FORGETS the answer-check ledger by KIND: prediction=0, advice_outcome=0 "
                   f"(pred={demo_evt('prediction')}, out={demo_evt('advice_outcome')})",
                   demo_evt('prediction') == 0 and demo_evt('advice_outcome') == 0))

    # PRIVACY INVARIANT (account-wide): NO orphan tenant data under ANY coordinate.
    orphans = owner_count("ACCT-DEMO",
        "SELECT (SELECT count(*) FROM core.claim WHERE account_id='ACCT-DEMO')"
        "      +(SELECT count(*) FROM core.code_node WHERE account_id='ACCT-DEMO')"
        "      +(SELECT count(*) FROM core.code_edge WHERE account_id='ACCT-DEMO')"
        "      +(SELECT count(*) FROM core.graph_version WHERE account_id='ACCT-DEMO')"
        "      +(SELECT count(*) FROM core.event WHERE account_id='ACCT-DEMO')"
        "      +(SELECT count(*) FROM core.statement WHERE account_id='ACCT-DEMO')"
        "      +(SELECT count(*) FROM core.account WHERE account_id='ACCT-DEMO')")
    checks.append((f"privacy invariant after erasure: NO orphan tenant data under ANY coordinate "
                   f"(total residual rows for ACCT-DEMO across every table={orphans})",
                   orphans == 0))

    # the export carries ZERO rows for the erased account (the registry map is cleared, so it's undiscoverable).
    after_export = export_rows("veripsa_migrator", "ACCT-DEMO")
    checks.append((f"after erasure, a fresh data-export carries ZERO rows for ACCT-DEMO (rows={len(after_export)})",
                   len(after_export) == 0))

    # ════════════════════════════════════════════════════════════════════════════════════════════
    # (5) NEIGHBOUR UNTOUCHED + STILL IMMUTABLE. The erase of one tenant never reaches another's answer-check
    # ledger, and never weakened the append-only moat.
    # ════════════════════════════════════════════════════════════════════════════════════════════
    neigh_pred = owner_count("ACCT-NEIGH",
        "SELECT count(*)::int FROM core.event WHERE account_id='ACCT-NEIGH' AND kind='prediction'")
    neigh_out = owner_count("ACCT-NEIGH",
        "SELECT count(*)::int FROM core.event WHERE account_id='ACCT-NEIGH' AND kind='advice_outcome'")
    neigh_claim = owner_count("ACCT-NEIGH",
        "SELECT count(*)::int FROM core.claim WHERE account_id='ACCT-NEIGH'")
    checks.append((f"the neighbour tenant is UNTOUCHED by the erase "
                   f"(pred={neigh_pred}, out={neigh_out}, claims={neigh_claim})",
                   neigh_pred == 1 and neigh_out == 1 and neigh_claim == 1))

    # its ledger is STILL append-only — a plain DELETE on it is still refused (the erase never dropped the trigger).
    err = None
    conn = conn_for("veripsa_migrator")
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account','ACCT-NEIGH', true)")
            cur.execute("DELETE FROM core.event WHERE account_id='ACCT-NEIGH'")
        conn.commit()
    except psycopg2.Error as e:
        conn.rollback(); err = str(e)
    finally:
        conn.close()
    checks.append((f"the neighbour's ledger is STILL append-only after the erase (plain DELETE refused): "
                   f"{(err or 'NOT REFUSED')[:60]}",
                   err is not None and "append-only" in err))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("OFFBOARDING GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
