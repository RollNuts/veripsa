#!/usr/bin/env python3
"""OWNER-SWEEP BOUNDED gate (audit P2-2) — the cross-tenant owner sweeps do BOUNDED work per call at large N.

THE GAP (P2-2): the owner-context cross-tenant sweeps in 95_owner.sql / 35_lifecycle.sql visited EVERY account
sequentially (O(N accounts)) — owner_cost_surface (~6 queries/account), owner_graph_freshness_surface (the
/freshz + /readyz + watchdog freshness input), and prune_all_accounts_with_authority (retention). The App is
going public on the GitHub Marketplace, so the account count will grow into the hundreds/thousands, and these
degrade (/freshz slow, watchdog tick lags, one giant retention transaction holds locks for the whole run).

THE FIX (proven here, real DB, N tenants >> cap):
  • READ surfaces CAP (advisory/visibility — capping is safe; the ENFORCED quota wall is _account_over_quota at
    the gate, NOT these lenses):
      - owner_graph_freshness_surface(p_cap): emits AT MOST p_cap coordinates AND visits AT MOST p_cap accounts,
        oldest-ingested first (the stalest = what evaluate_graph_freshness escalates on). Mirrors the app-layer
        VERIPSA_GRAPH_FRESHNESS_CAP=100 the only caller already slices to.
      - owner_cost_surface(p_cap): the per-account DETAIL scan is bounded to p_cap accounts, BUT account_count
        stays EXACT (a cheap count over the no-RLS routing map) and db_total_bytes stays EXACT (one size call) —
        so the disk-fill alert (db_size_high) and the true tenant total are never bounded away. capped/
        accounts_scanned flag the bound honestly.
  • RETENTION must stay COMPLETE → it is PAGINATED, never capped: prune_all_accounts_with_authority(...,
    p_max_accounts, p_after) does bounded work per call (≤ p_max_accounts tenants, each its own txn) and the
    caller pages account_id-ascending with the returned next_after cursor until done — the UNION of the bounded
    chunks equals the one-shot full sweep (proven identical here). A 1-arg/2-arg call is the EXACT prior sweep.

PROVES (real scratch DB, N=24 tenants, cap=5):
  (1) BOUNDED FRESHNESS: owner_graph_freshness_surface(5) returns <=5 coords + scanned<=5 (not all 24).
  (2) BOUNDED COST + EXACT TOTALS: owner_cost_surface(5) scans <=5 accounts (capped=true) yet account_count==24
      (exact) and db_total_bytes>0 (exact) — the cap never lies about the fleet total or the disk-fill signal.
  (3) COMPLETE-BUT-BATCHED RETENTION: a paginated sweep (chunk=5) visits EVERY one of the 24 tenants exactly
      once (gap-free union) and prunes the SAME rows as a single one-shot sweep — bounded per call, complete in total.
  (4) ISOLATION PRESERVED: a buyer/tenant role (veripsa_demo_agent) is DENIED all three (owner-only; the cap did
      not widen the cross-tenant boundary).
  (5) CONTENT-FREE: the freshness/cost surface output carries ids + counts/shas only — never a source path/body
      (a commit sha + a file PATH like 'x.py' as a coordinate is git metadata; we assert no NON-coordinate path leaks).

Run:  python3 tests/test_owner_sweep_bounded.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import psycopg2  # noqa: E402

DB = "veripsa_sweepbound_" + str(os.getpid())
N = 24          # tenants — comfortably > CAP so the bound is observable
CAP = 5         # the per-call bound under test
RETENTION_ACCOUNTS = N + 1  # N GitHub tenants + bootstrap's credential-only dogfood account.
checks = []


def chk(c, label):
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    checks.append(bool(c))


def app_conn():
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    return conn


def run_app(sql, args=()):
    conn = app_conn()
    try:
        with conn.cursor() as c:
            c.execute("SET search_path=core")
            c.execute(sql, args)
            row = c.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _j(v):
    return v if isinstance(v, dict) else (json.loads(v) if v else {})


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    # ── seed N tenants the REAL way (the gate lazy-provisions account + routing row on enter_installation), each
    # with a graph_version (a freshness coordinate) + OLD (60d) and NEW (1d) 'landed' telemetry. occurred_at is
    # backdated as the migrator (owner-bypass arms the gate token) so the retention window actually bites.
    conn = app_conn()
    try:
        with conn.cursor() as c:
            c.execute("SET search_path=core")
            for i in range(N):
                inst = str(1000 + i)
                c.execute("SELECT core.enter_installation_with_authority(%s)", (inst,))
                g = json.dumps({"nodes": [{"id": "f", "kind": "file", "path": "x.py", "name": "x.py"}], "edges": []})
                c.execute("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                          (g, f"acme/r{i:03d}", "main", f"{i:040x}"))
                c.execute("SELECT set_config('core.current_account','',true)")
    finally:
        conn.close()
    # backdate OLD telemetry per tenant as the migrator (owner-bypass + governed token, like the gate does).
    seed = ["SET search_path=core;"]
    for i in range(N):
        a = f"ACCT-GH-{1000 + i}"
        seed.append(f"SELECT set_config('core.current_account','{a}', true);")
        seed.append("SELECT core.mark_governed_write('event');")
        seed.append(
            "INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,occurred_at) VALUES "
            f"('SW-OLD-{i}','{a}','landed','AG','acme/r{i:03d}','main','old.py', now()-interval '60 days'),"
            f"('SW-NEW-{i}','{a}','landed','AG','acme/r{i:03d}','main','new.py', now()-interval '1 day');")
    mconn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    mconn.autocommit = True
    try:
        with mconn.cursor() as c:
            c.execute("\n".join(seed))
    finally:
        mconn.close()

    # ── (1) BOUNDED FRESHNESS — owner_graph_freshness_surface(CAP) does ≤CAP work at N≫CAP. ────────────────────
    fr = _j(run_app("SELECT core.owner_graph_freshness_surface(%s)", (CAP,)))
    fr_full = _j(run_app("SELECT core.owner_graph_freshness_surface()"))
    chk(fr.get("coordinate_count") <= CAP and fr.get("accounts_scanned") <= CAP and fr.get("capped") is True,
        f"BOUNDED freshness: owner_graph_freshness_surface({CAP}) emits <= {CAP} coords + scans <= {CAP} accounts "
        f"at N={N} (coords={fr.get('coordinate_count')}, scanned={fr.get('accounts_scanned')}, capped={fr.get('capped')})")
    chk(fr_full.get("coordinate_count") == N,
        f"UNBOUNDED freshness still sees every tenant (coords={fr_full.get('coordinate_count')} == N={N}) — the cap is "
        f"opt-in, the full lens is intact")

    # ── (2) BOUNDED COST + EXACT TOTALS — the scan caps but account_count + db_total_bytes stay EXACT. ─────────
    co = _j(run_app("SELECT core.owner_cost_surface(%s)", (CAP,)))
    chk(co.get("accounts_scanned") <= CAP and co.get("capped") is True and len(co.get("accounts") or []) <= CAP,
        f"BOUNDED cost: owner_cost_surface({CAP}) scans <= {CAP} accounts at N={N} "
        f"(scanned={co.get('accounts_scanned')}, rows={len(co.get('accounts') or [])}, capped={co.get('capped')})")
    chk(co.get("account_count") == N,
        f"EXACT TOTAL: account_count == N == {N} even though the per-account scan was capped at {CAP} "
        f"(got {co.get('account_count')}) — the cap never lies about the fleet size")
    chk(isinstance(co.get("db_total_bytes"), int) and co.get("db_total_bytes") > 0,
        f"EXACT DISK SIGNAL: db_total_bytes stays exact under the cap (got {co.get('db_total_bytes')}) — the "
        f"db_size_high disk-fill alert is unaffected by the scan bound")

    # ── (3) COMPLETE-BUT-BATCHED RETENTION — a paginated sweep covers EVERY tenant + prunes the SAME rows. ─────
    # one-shot first would prune everything; so test order: PAGINATE (chunk=CAP) and assert it (a) does bounded
    # work per call (each call visits <= CAP accounts) and (b) reaches done having visited all retention accounts
    # exactly once. Retention intentionally includes bootstrap's credential-only dogfood account in addition to
    # the N routed GitHub tenants; owner freshness/cost remain installation-fleet lenses above.
    after = None
    visited = 0
    batches = 0
    pruned_total = 0
    max_per_call = 0
    seen_done = False
    for _ in range(N + 10):   # generous loop bound (must finish in ceil(N/CAP)+1 calls)
        res = _j(run_app(
            "SELECT core.prune_all_accounts_with_authority(now()-interval '30 days', ARRAY['landed','push'], %s, %s)",
            (CAP, after)))
        batches += 1
        v = int(res.get("accounts") or 0)
        max_per_call = max(max_per_call, v)
        visited += v
        pruned_total += int(res.get("pruned") or 0)
        if res.get("done"):
            seen_done = True
            break
        after = res.get("next_after")
    chk(seen_done and max_per_call <= CAP,
        f"BOUNDED retention: each paginated call visits <= {CAP} tenants (max_per_call={max_per_call}) and the "
        f"sweep reaches done (batches={batches})")
    chk(visited == RETENTION_ACCOUNTS,
        f"COMPLETE retention: the paginated sweep visited every routed tenant plus credential-only dogfood account "
        f"exactly once (visited={visited} == {RETENTION_ACCOUNTS}) — full coverage via the cursor, gap-free")
    chk(pruned_total == N,
        f"COMPLETE prune: it pruned the OLD 'landed' row of all {N} tenants (pruned_total={pruned_total}) — the "
        f"union of the bounded chunks equals the one-shot sweep")
    # and a SUBSEQUENT one-shot finds nothing old left (the paginated run already pruned it all = identical effect).
    one = _j(run_app("SELECT core.prune_all_accounts_with_authority(now()-interval '30 days')"))
    chk(one.get("accounts") == RETENTION_ACCOUNTS and one.get("pruned") == 0,
        f"IDENTICAL EFFECT: a one-shot sweep AFTER the paginated run prunes 0 (the batched run already achieved "
        f"the full one-shot coverage) and still visits all {RETENTION_ACCOUNTS} retention accounts "
        f"(accounts={one.get('accounts')}, pruned={one.get('pruned')})")
    # the NEW (1d) telemetry survived the 30d window across all tenants (the cap/pagination didn't over-prune).
    # Read as the migrator with the account pinned — the pin is txn-local (set_config is_local=true), so pin +
    # count must share ONE transaction (autocommit OFF), else the pin is gone by the next autocommit statement.
    new_rows = []
    mconn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with mconn, mconn.cursor() as c:   # autocommit OFF → all statements share ONE txn, so the pin persists
            c.execute("SET search_path=core")
            for i in range(N):
                c.execute("SELECT set_config('core.current_account',%s,true)", (f"ACCT-GH-{1000+i}",))
                c.execute("SELECT count(*)::int FROM core.event WHERE event_id=%s", (f"SW-NEW-{i}",))
                new_rows.append(c.fetchone()[0])
    finally:
        mconn.close()
    chk(all(n == 1 for n in new_rows),
        f"RECENT telemetry KEPT across all {N} tenants (no over-prune from the cap/pagination) — sum(new)={sum(new_rows)}")

    # A suspended/uninstalled install keeps its routing row, but owner_cost_surface is an active-fleet lens. It
    # must match owner_graph_freshness_surface/list_installation_ids liveness and exclude revoked rows.
    conn = app_conn()
    try:
        with conn.cursor() as c:
            c.execute("SET search_path=core")
            c.execute("SELECT core.enter_installation_with_authority('9999')")
            c.execute("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                      (json.dumps({"nodes": [{"id": "f", "kind": "file", "path": "x.py", "name": "x.py"}],
                                   "edges": []}),
                       "acme/revoked", "main", "f" * 40))
            c.execute("SELECT set_config('core.current_account','',true)")
    finally:
        conn.close()
    mconn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    mconn.autocommit = True
    try:
        with mconn.cursor() as c:
            c.execute("SET search_path=core; UPDATE core.installation_account SET revoked_at=now() "
                      "WHERE installation_id='9999';")
    finally:
        mconn.close()
    co_live = _j(run_app("SELECT core.owner_cost_surface(%s)", (N + 5,)))
    co_live_accounts = [a.get("account_id") for a in (co_live.get("accounts") or []) if isinstance(a, dict)]
    chk(co_live.get("account_count") == N and "ACCT-GH-9999" not in co_live_accounts,
        f"LIVENESS: owner_cost_surface excludes revoked installs from totals/details "
        f"(account_count={co_live.get('account_count')}, revoked_in_rows={'ACCT-GH-9999' in co_live_accounts})")

    # ── (4) ISOLATION PRESERVED — a buyer/tenant role is DENIED all three (the cap did not widen the boundary). ─
    tconn = psycopg2.connect(f"postgresql://veripsa_demo_agent@localhost/{DB}")
    tconn.autocommit = True
    denials = {}
    for name, call in (("owner_graph_freshness_surface", "SELECT core.owner_graph_freshness_surface(5)"),
                       ("owner_cost_surface", "SELECT core.owner_cost_surface(5)"),
                       ("prune_all_accounts_with_authority",
                        "SELECT core.prune_all_accounts_with_authority(now(), ARRAY['landed'], 5, NULL)")):
        refused = False
        try:
            with tconn.cursor() as c:
                c.execute("SET search_path=core")
                c.execute(call)
        except psycopg2.errors.InsufficientPrivilege:
            refused = True
        except Exception as e:
            refused = "permission denied" in str(e).lower()
        denials[name] = refused
    tconn.close()
    chk(all(denials.values()),
        f"ISOLATION: a buyer/tenant role is DENIED all three bounded owner sweeps (owner-only preserved) — {denials}")

    # ── (5) CONTENT-FREE — the surfaces carry ids + counts/shas + coordinate (repo/branch/path) git-metadata
    # ONLY. We assert no NON-coordinate source path leaks: the only 'path'-ish strings are the per-coordinate
    # repo/branch (public git metadata) — never a code body or an internal file path beyond the coordinate.
    blob = json.dumps(fr_full) + json.dumps(_j(run_app("SELECT core.owner_cost_surface()")))
    chk("old.py" not in blob and "new.py" not in blob,
        "CONTENT-FREE: neither owner read surface leaks a telemetry file PATH (only ids/counts/shas + coordinate "
        "repo/branch cross the boundary)")

    print("OWNER-SWEEP-BOUNDED GATE:", "PASS" if all(checks) else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if all(checks) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
