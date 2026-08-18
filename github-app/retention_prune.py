#!/usr/bin/env python3
"""Veripsa retention sweep — the scheduled job that keeps the append-only ledger bounded.

Why this exists
    core.event is an append-only ledger: every push and every landing is one row, kept forever,
    across all tenants (tens of GB/year at scale). core.prune_all_accounts_with_authority is the
    one controlled erase, but a gated function does nothing until something calls it on a schedule.
    This module is that caller — a tiny, dependency-light entry point a cron job invokes (the Render
    cron service; see render.yaml). Without it the ledger grows forever and eventually fills the
    small, cheap Postgres instance, a real production-ops gap on the 256 MiB tier.

What it does
    Connects as veripsa_app (the VERIPSA_DSN secret — the same least-privilege role the server uses,
    so the security boundary is unchanged) and calls
    core.prune_all_accounts_with_authority(now() - RETENTION_DAYS). That prunes, for every account
    (each pinned to its own rows by its token), the four unbounded growers past the window:
      • the operational telemetry kinds (PRUNE_KINDS: 'landed', 'push', 'compat_finding') older than the window;
      • terminal (released/expired) claim rows past the window — a landed/withdrawn PR's dead lanes;
      • SPENT 'prediction' events past the window whose answer-check is DONE (a matching 'advice_outcome'
        already on record for the same coordinate) — a prediction is read only once, at PR-close, then
        grows one-per-closed-PR forever; an OPEN PR's prediction (no outcome yet) is KEPT regardless of
        age so the close-time join never misses.
      • TERMINAL (done/failed) durable webhook-inbox rows (core.webhook_delivery) past the window — the
        persist-before-202 inbox keeps a 'done' row (payload cleared to {}, account_key + repo retained) and a
        'failed' poison row forever, and nothing else prunes them; reaped fleet-wide (this table has no
        account_id) in ONE cross-tenant DELETE per sweep, leaving live 'queued'/'processing' rows untouched.
      • cold repo structural working sets whose repo has no activity past the window and no active/waiting claim:
        code graph rows and co-change caches are rebuildable. The next default-branch push/PR cold-starts the
        code graph and queues co-change history populate instead of keeping an inactive repo warm forever.
    The curated effect records (warn_issued / collision_held), the 'advice_outcome' answer-check rows
    (the lifetime accuracy surface reads ALL of them), and the statement stream are left immutable
    (records, not correctness).

Why 30 days (and a hard floor of 14)
    The only consumer of the pruned telemetry is collisions_on_main, which reads a 14-day window —
    so 14 days is the hard floor below which pruning would blind the collision engine. The default
    is 30 days as a data-minimisation choice: roughly 2x the analytical need rather than 90. 30 days
    covers the 14-day collision window plus every surface clamped to <=30 days (e.g. stuck_pr_window)
    with margin, while retaining two-thirds less operational telemetry (a private repo's paths and
    author logins) than the old 90-day default — a better privacy posture and a smaller DB. Pruning
    never weakens predictions.

Environment
    VERIPSA_DSN              the veripsa_app DSN (same secret the server uses).
    VERIPSA_RETENTION_DAYS   how many days of operational telemetry to keep (default 30; floor 14 =
                             the collisions_on_main window). Older 'landed'/'push' rows are pruned;
                             curated records are kept forever.
    VERIPSA_RETENTION_BATCH_ACCOUNTS
                             BOUNDED-BATCH chunk size (audit P2-2): tenants processed per transaction before
                             committing + paging on. UNSET / <=0 = one unbounded sweep (the prior behaviour).
                             Set it (e.g. 200) at large install counts so the nightly sweep is not one giant
                             all-tenant transaction. Full coverage is preserved — it pages to completion.

Run
    python3 github-app/retention_prune.py        # one sweep, then exit (a cron-friendly one-shot).
"""
from __future__ import annotations

import json
import os
import sys

# The prunable event KINDS this sweep erases past the window (the operational-telemetry list). ONE list for
# both the unbounded and the bounded-batch call so they can never drift apart:
#   • 'landed' / 'push'   — the original operational telemetry (per-landing / per-push rows).
#   • 'compat_finding'    — the compatibility shadow findings (compat lane PR-2): one content-free row per
#     (head pair, finding). Wired into retention from DAY ONE — the writer exists before any production
#     caller, so the kind is bounded by this sweep from the first row ever written, never a fifth unbounded
#     grower. A finding past the window is stale by definition (the analysis reruns per webhook on the
#     CURRENT heads; old head pairs are never re-read).
# The curated effect records (warn_issued / collision_held), 'advice_outcome', and the statement stream are
# NEVER in this list (records, not correctness); 'prediction' has its own answer-check-aware sweep server-side.
PRUNE_KINDS = ["landed", "push", "compat_finding"]


def prune_once(dsn: str, retention_days: int, batch_accounts: int | None = None) -> dict:
    """Run one cross-tenant retention sweep over the given DSN.

    Returns the gate function's result dict, e.g. {ok, accounts, pruned, dead_claims_pruned, kinds}.

    BOUNDED-BATCH (audit P2-2 — the cross-tenant sweep was O(N accounts) in ONE transaction, degrading as the
    Marketplace install count grows: a single statement scanned thousands of tenants and held its locks for the
    whole run). When `batch_accounts` is set (env VERIPSA_RETENTION_BATCH_ACCOUNTS), this PAGES the gate function
    account_id-ascending in chunks of that many tenants — each chunk its OWN transaction (committed before the
    next), so locks release between batches and no single transaction sweeps the whole fleet. FULL COVERAGE is
    preserved: it loops, passing the returned next_after cursor, until the gate reports done=true — the union of
    the bounded chunks equals the one-shot sweep (retention MUST stay complete, so it is paginated, never capped).
    UNSET (batch_accounts None) → ONE unbounded call = the EXACT prior behaviour (byte-for-byte), so the change is
    opt-in and behaviour-preserving by default. The per-chunk result dicts are summed into one manifest the caller
    logs (accounts/pruned/dead_claims_pruned/spent_predictions_pruned across all batches)."""
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        # UNBOUNDED (default): ONE call, in ONE transaction — identical to the original sweep.
        if batch_accounts is None:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                # The gate computes the window edge server-side from now() minus the interval, so the cutoff uses
                # the DATABASE clock (correct even if the cron host's clock is skewed). Scoped per account row.
                # KINDS are passed EXPLICITLY (PRUNE_KINDS) rather than relying on the SQL default, so adding a
                # prunable kind (e.g. 'compat_finding') is one Python-side list edit covering BOTH call shapes.
                cur.execute(
                    "SELECT core.prune_all_accounts_with_authority(now() - make_interval(days => %s), %s)",
                    (retention_days, PRUNE_KINDS),
                )
                row = cur.fetchone()
                res = row[0] if row else None
                return res if isinstance(res, dict) else (json.loads(res) if res else {"ok": False})

        # BOUNDED-BATCH: page the gate in chunks of `batch_accounts` tenants, each its OWN committed transaction
        # (the `with conn` per iteration commits the chunk before the next opens), until done=true. Summed manifest.
        after = None
        totals = {"ok": True, "accounts": 0, "pruned": 0, "dead_claims_pruned": 0,
                  "spent_predictions_pruned": 0, "webhook_deliveries_reaped": 0,
                  "cold_repos_pruned": 0,
                  "cold_graph_rows_pruned": {"nodes": 0, "edges": 0, "versions": 0, "cochange": 0,
                                             "cochange_seen": 0},
                  "batches": 0}
        # a hard ceiling on iterations = a defensive guard so a never-advancing cursor (should be impossible —
        # next_after strictly increases) can never spin forever; far above any realistic tenant count.
        for _ in range(1_000_000):
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(
                    "SELECT core.prune_all_accounts_with_authority("
                    "now() - make_interval(days => %s), %s, %s, %s)",
                    (retention_days, PRUNE_KINDS, batch_accounts, after),
                )
                row = cur.fetchone()
                res = row[0] if row else None
                res = res if isinstance(res, dict) else (json.loads(res) if res else {})
            totals["batches"] += 1
            totals["ok"] = totals["ok"] and bool(res.get("ok"))
            # webhook_deliveries_reaped is the cross-tenant durable-inbox reaper count (audit P1); the reaper runs
            # only on the FIRST page (p_after IS NULL), so summing across pages still yields the one true total.
            for k in ("accounts", "pruned", "dead_claims_pruned", "spent_predictions_pruned",
                      "webhook_deliveries_reaped", "cold_repos_pruned"):
                totals[k] += int(res.get(k) or 0)
            cold_rows = res.get("cold_graph_rows_pruned") if isinstance(res, dict) else None
            if isinstance(cold_rows, dict):
                for k in totals["cold_graph_rows_pruned"]:
                    totals["cold_graph_rows_pruned"][k] += int(cold_rows.get(k) or 0)
            if res.get("done") or not res.get("ok"):
                break
            nxt = res.get("next_after")
            if nxt is None or nxt == after:   # no forward progress → stop (defensive; cursor should advance)
                break
            after = nxt
        return totals
    finally:
        conn.close()


def main() -> int:
    dsn = os.environ.get("VERIPSA_DSN")
    if not dsn:
        print("retention sweep: VERIPSA_DSN is not set (the veripsa_app DSN) — nothing to do", flush=True)
        return 2
    try:
        days = int(os.environ.get("VERIPSA_RETENTION_DAYS", "30"))
    except ValueError:
        days = 30
    # Floor of 14 (not 1): collisions_on_main reads a hard 14-day window of 'landed'/'push' telemetry
    # (see db/schema/80_contention.sql + 40_surfaces.sql — `'14 days'::interval`). Pruning younger than
    # that would silently blind the collision engine: the App would keep answering, but quietly grow
    # less correct. The RUNBOOK and render.yaml both state "floor 14 = collisions_on_main window"; we
    # enforce it here so a typo'd VERIPSA_RETENTION_DAYS=5 is refused loudly rather than degrading
    # predictions in silence.
    if days < 14:
        print(f"retention sweep: VERIPSA_RETENTION_DAYS={days} is below the 14-day floor "
              f"(collisions_on_main reads a hard 14-day window — pruning younger blinds the engine) — refusing",
              flush=True)
        return 2
    # BOUNDED-BATCH chunk size (audit P2-2): how many tenants the cross-tenant sweep processes PER transaction
    # before committing + paging to the next chunk. UNSET / <=0 → one unbounded sweep (the prior behaviour, exact).
    # Set it (e.g. 200) once the Marketplace install count is large so the nightly sweep never runs as one giant
    # all-tenant transaction holding locks for the whole run. Full coverage is preserved either way (it pages to
    # done). A typo'd non-int is treated as unset (fail-safe — retention must still run; never crash the cron).
    batch_accounts: int | None = None
    raw_batch = os.environ.get("VERIPSA_RETENTION_BATCH_ACCOUNTS")
    if raw_batch is not None and raw_batch.strip():
        try:
            b = int(raw_batch)
            batch_accounts = b if b > 0 else None
        except ValueError:
            print(f"retention sweep: VERIPSA_RETENTION_BATCH_ACCOUNTS={raw_batch!r} is not an integer — "
                  f"ignoring (running one unbounded sweep)", flush=True)
    try:
        res = prune_once(dsn, days, batch_accounts=batch_accounts)
    except Exception as e:  # a DB hiccup must fail the JOB cleanly (cron logs + retries next run), never silently
        print(f"retention sweep FAILED: {str(e)[:200]}", flush=True)
        return 1
    print(f"retention sweep: kept {days}d of operational telemetry, "
          f"pruned {res.get('pruned')} event rows "
          f"(incl. {res.get('spent_predictions_pruned', 0)} spent predictions whose answer-check is done) "
          f"+ {res.get('dead_claims_pruned', 0)} dead claim rows "
          f"+ {res.get('webhook_deliveries_reaped', 0)} terminal durable-inbox rows "
          f"+ {res.get('cold_repos_pruned', 0)} cold repo working sets "
          f"across {res.get('accounts')} accounts (kinds={res.get('kinds')})", flush=True)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
