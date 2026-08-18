#!/usr/bin/env python3
"""MARKETPLACE BILLING — the readied commercial seam (SECURITY-sensitive: it governs the free-tier wall).

This gate proves the TWO wired pieces, end-to-end against a REAL local Postgres (not a mock of the gate):

  1. THE PAID SHORT-CIRCUIT (core._account_over_quota): a NON-'free' plan ⇒ NULL = never over quota (writes
     succeed PAST the free cap). A 'free' account is STILL walled, EXACTLY as before (the wall is not weakened).
     FAIL-SAFE DIRECTION (the security-critical invariant): a bug must NOT let a FREE account bypass the wall —
     only ever leave a paid account walled. The override fires ONLY on a plan we can READ and that is != 'free';
     any plan-read error falls through to 'free' = the wall STAYS ON.

  2. THE marketplace_purchase HANDLER (server.handle_event): action 'purchased'/'changed' maps the GitHub
     account's plan onto core.account.plan via the gated, tenant-pinned SECURITY DEFINER setter (never a raw
     Python write); 'cancelled' resets to 'free'; 'pending_change' is a grace-period no-op (no early downgrade).

  3. CROSS-TENANT ISOLATION: one account's plan change never touches another's (RLS-walled; the setter resolves +
     writes EXACTLY the one 'ACCT-GH-'||<gh-account-id> account).

  4. PERIMETER: the setter is REVOKEd from PUBLIC and granted to veripsa_app ONLY (never a buyer seat).

INERT until the PO lists the App on Marketplace + creates plans (PO-gated commercial acts) — this readies the
seam, it does not sell. GitHub bills the customer; we only map plan→account.

Run:  python3 tests/test_marketplace_billing.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import psycopg2  # noqa: E402
from _lifecycle_fixture import (  # noqa: E402
    absent_installation_proof,
    seed_processing_installation_event,
    seed_processing_uninstall,
)

# This gate exercises the marketplace_purchase handler's ENABLED (write) path — the seam readied for a possible
# future Marketplace SKU. That path is OFF by default (handle_event gates it behind VERIPSA_MARKETPLACE_BILLING so
# Marketplace billing stays explicitly gated — see github-app/webhook_handlers.py._marketplace_billing_enabled and the
# dedicated gates.d/NN-marketplace_billing_off gate that proves the DEFAULT-OFF noop). Opt this gate IN so the
# handler actually maps plan→account here (read at call time inside handle_event, so setting it before importing
# server suffices).
os.environ["VERIPSA_MARKETPLACE_BILLING"] = "1"

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name would let concurrent
# runs (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run. Per-PID,
# exactly like db/smoke.sh, run_gates, and the other gates.
DB = "veripsa_marketplace_" + str(os.getpid())
DSN_MIG = f"postgresql://veripsa_migrator@localhost/{DB}"
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"

# valid 40-char hex commit shas (ingest_graph_with_authority rejects non-hex / >64 chars). One per write so a
# free account's SECOND write is a genuinely new coordinate-commit (and so re-ingest monotonicity never interferes).
SHA1 = "1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa"
SHA2 = "2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb"

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, passed))


def psql_mig(sql):
    r = subprocess.run(["psql", DSN_MIG, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr)


def psql_app(sql):
    r = subprocess.run(["psql", DSN_APP, "-v", "ON_ERROR_STOP=0", "-tAc", sql], capture_output=True, text=True)
    return (r.stdout + r.stderr)


def last_value(out):
    """The LAST non-empty line — a multi-statement `SET ...; SELECT ...` prints a 'SET' ack before the result."""
    lines = [ln for ln in out.splitlines() if ln.strip() != ""]
    return lines[-1].strip() if lines else ""


def uninstall_account(account_id: str) -> dict:
    key = f"marketplace-uninstall-{account_id}"
    deleted_installation_id = f"A-{account_id}"
    seed_processing_uninstall(DSN_MIG, key, account_id, deleted_installation_id)
    conn = psycopg2.connect(DSN_APP)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (account_id,))
            cur.execute("SELECT set_config('core.current_delivery_key',%s,false)", (key,))
            cur.execute(
                "SELECT core.purge_account_working_set_with_authority(%s::jsonb)",
                (json.dumps(absent_installation_proof(account_id, deleted_installation_id)),),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def reactivate_account(account_id: str) -> dict:
    key = f"marketplace-reinstall-{account_id}"
    installation_id = f"B-{account_id}"
    seed_processing_installation_event(
        DSN_MIG,
        key,
        account_id,
        installation_id,
        "created",
        received_at="2099-01-01T00:00:00Z",
    )
    proof = {
        "installation_id": installation_id,
        "account_id": account_id,
        "created_at": "2099-01-01T00:00:00Z",
        "suspended": False,
    }
    conn = psycopg2.connect(DSN_APP)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (account_id,))
            cur.execute(
                "SELECT core.reactivate_account_with_authority(%s,%s::jsonb)",
                (key, json.dumps(proof)),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def bootstrap():
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("[FAIL] bootstrap (roles + schema.sql + seats)")
        print((r.stdout + r.stderr)[-2000:])
        sys.exit(2)
    # TIGHTEN the line so the wall BITES on the SECOND graph write (the over-quota check is a PRE-WRITE
    # "already over?" test, so the first write that crosses still lands; the next is refused — see 30_gate.sql).
    # graph_units line = 0 → after ONE node is stored, the account is over → its NEXT ingest is walled. The
    # cleanest, fastest way to exercise the real wall without storing thousands of rows.
    # graph_units is now a PER-PLAN HARD line (core._plan_graph_units_limit, NOT _free_line) — so the graph_units
    # axis is tightened via set_plan_graph_units_limit_with_authority PER PLAN. We zero EVERY plan's graph_units
    # line so the wall bites on whatever plan an account is on in this gate (free baseline AND, post-change, the
    # paid tiers — though the paid accounts here ingest only ~2 nodes, so the paid-tier checks below are written to
    # match the real per-plan caps). repos/events stay on _free_line (free-tier only) and are set wide-open so
    # graph_units is the dimension under test.
    psql_mig("SET search_path=core; "
             "SELECT core.set_free_line_with_authority('free_max_repos',100000); "
             "SELECT core.set_free_line_with_authority('free_max_events',1000000); "
             "SELECT core.set_plan_graph_units_limit_with_authority('free',0);")


def drop():
    subprocess.run(["dropdb", DB], capture_output=True, text=True)


# A one-node graph for an ingest write (content-free; path/name are coordinates, not bodies).
def _graph(name):
    return '{"nodes":[{"id":"n-%s","kind":"file","path":"%s.py","name":"%s"}],"edges":[]}' % (name, name, name)


def _ingest(inst, repo, sha, name):
    """As veripsa_app: enter a real installation (pins the tenant) then ingest one node — the legit live path.
    Returns the LAST output line (the ingest result JSON, or a quota_exceeded sentinel when walled)."""
    g = _graph(name)
    return last_value(psql_app("SET search_path=core; "
                               f"SELECT core.enter_installation_with_authority('{inst}'); "
                               f"SELECT core.ingest_graph_with_authority('{g}'::jsonb,'{repo}','main','{sha}');"))


def _plan_of(account):
    """Ground-truth plan column for `account` (migrator, pinned — account is FORCE-RLS)."""
    return last_value(psql_mig(f"SET search_path=core; SET core.current_account='{account}'; "
                               f"SELECT plan FROM core.account WHERE account_id='{account}';"))


def _over_quota(account):
    """core._account_over_quota('{account}') with the account pinned (its callers always pin it first).
    Returns the over-dimension string, or 'NULL' when under the line (= allow)."""
    return last_value(psql_mig(f"SET search_path=core; SET core.current_account='{account}'; "
                               f"SELECT COALESCE(core._account_over_quota('{account}'),'NULL');"))


def _pgu_limit(plan):
    """core._plan_graph_units_limit('{plan}') — the per-plan HARD graph_units ceiling (the commercial quota line)."""
    return last_value(psql_mig(f"SET search_path=core; SELECT core._plan_graph_units_limit('{plan}');"))


def _set_pgu(plan, value):
    """Owner-tune ONE plan's HARD graph_units line via the App-delegation setter (no redeploy). Returns effective."""
    return last_value(psql_app(f"SET search_path=core; SELECT core.set_plan_graph_units_limit_with_authority('{plan}',{value});"))


# ── A tiny Fake gh so handle_event's marketplace branch runs offline (the marketplace path makes NO gh calls,
#    but handle_event's signature requires a client; for_installation is consulted only when an installation id
#    is present, which a marketplace_purchase payload has none of). ─────────────────────────────────────────────
class FakeGH:
    def for_installation(self, _id):
        return self


def _scoped_runner(conn):
    """A db(sql,args) runner bound to one autocommit connection — the same shape server._scoped_db builds."""
    def run(sql, args=()):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    return run


def main():
    print("VERIPSA MARKETPLACE BILLING — paid-override + purchase handler + isolation (real gate)")
    print(f"(scratch DB: {DB})")
    bootstrap()

    import psycopg2
    import server  # the live handler module (github-app/server.py)

    gh = FakeGH()
    conn = psycopg2.connect(DSN_APP)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
    db = _scoped_runner(conn)

    # ── PIECE 1 — THE PAID SHORT-CIRCUIT (driven through the REAL enforcement point: ingest_graph). ────────────
    # FREE account: the FIRST ingest crosses the (zero graph_units) line and still lands; the SECOND is WALLED.
    r1 = _ingest("inst-free", "freeorg/repo", SHA1, "freeone")
    add("PROBE-VALIDITY: a free account's FIRST graph write lands (the pre-write wall lets the crossing write through)",
        '"ok" : true' in r1 or '"ok":true' in r1)
    r2 = _ingest("inst-free", "freeorg/repo2", SHA2, "freetwo")
    add("PAID-OVERRIDE off: a FREE account IS walled on its next write (free-tier wall UNCHANGED, still enforced)",
        "quota_exceeded" in r2)
    add("FREE account _account_over_quota reports the over dimension (the wall really fired)",
        _over_quota("ACCT-GH-inst-free") in ("graph_units", "repos", "events"))

    # PAID account: set plan=pro FIRST, then the same two-write sequence — NEITHER write is walled (override fires).
    psql_app("SET search_path=core; SELECT core.set_account_plan_with_authority('5001','pro');")
    add("PAID account plan column is 'pro' after set_account_plan_with_authority", _plan_of("ACCT-GH-5001") == "pro")
    add("PAID account _account_over_quota returns NULL = never over (the override short-circuits)",
        _over_quota("ACCT-GH-5001") == "NULL")
    p1 = _ingest("5001", "proorg/repo", SHA1, "proone")
    p2 = _ingest("5001", "proorg/repo2", SHA2, "protwo")
    add("PAID account write #1 succeeds PAST the free cap (not walled)", '"ok"' in p1 and "quota_exceeded" not in p1)
    add("PAID account write #2 succeeds PAST the free cap (not walled) — the wall does NOT apply to paid",
        '"ok"' in p2 and "quota_exceeded" not in p2)

    # ── PIECE 2 — THE marketplace_purchase HANDLER (server.handle_event over the real DB). ─────────────────────
    # 'purchased' maps the plan; the GitHub account id is at marketplace_purchase.account.id, the plan at .plan.
    res = server.handle_event("marketplace_purchase",
                              {"action": "purchased",
                               "marketplace_purchase": {"account": {"id": 7777}, "plan": {"id": 42, "name": "team"}}},
                              db, gh)
    add("HANDLER purchased: returns the mapped account + plan label",
        res.get("event") == "marketplace_purchase" and res.get("account") == "ACCT-GH-7777" and res.get("plan") == "team")
    add("HANDLER purchased: core.account.plan is now 'team' (written via the gated setter, not raw Python)",
        _plan_of("ACCT-GH-7777") == "team")
    add("HANDLER purchased: that paid account is now over-quota-EXEMPT (NULL)", _over_quota("ACCT-GH-7777") == "NULL")

    # 'changed' (an upgrade/downgrade to another paid plan) re-maps the plan.
    server.handle_event("marketplace_purchase",
                        {"action": "changed",
                         "marketplace_purchase": {"account": {"id": 7777}, "plan": {"id": 99, "name": "enterprise"}}},
                        db, gh)
    add("HANDLER changed: re-maps the plan to the new label ('enterprise')", _plan_of("ACCT-GH-7777") == "enterprise")

    # Give 7777 a footprint OVER the (zero graph_units) line WHILE it is still paid (the override lets these
    # writes land), so that after we cancel it, the wall has something to bite on — proving the override truly
    # toggled OFF (not merely that an empty account is trivially under the cap).
    _ingest("7777", "teamorg/repo", SHA1, "teamone")
    _ingest("7777", "teamorg/repo2", SHA2, "teamtwo")
    add("PAID 7777 with a footprint is STILL exempt (the override holds while paid)", _over_quota("ACCT-GH-7777") == "NULL")

    # 'cancelled' resets to free → walled again (it now has a footprint over the line).
    rc = server.handle_event("marketplace_purchase",
                             {"action": "cancelled",
                              "marketplace_purchase": {"account": {"id": 7777}, "plan": {"id": 99, "name": "enterprise"}}},
                             db, gh)
    add("HANDLER cancelled: returns plan='free'", rc.get("plan") == "free")
    add("HANDLER cancelled: core.account.plan is reset to 'free' (downgrade)", _plan_of("ACCT-GH-7777") == "free")
    add("HANDLER cancelled: that account (with a footprint) is over-quota-ENFORCED again (the wall is back on)",
        _over_quota("ACCT-GH-7777") != "NULL")

    # 'pending_change' is a grace-period notice — it must NOT change the plan (no early downgrade). Set paid first.
    psql_app("SET search_path=core; SELECT core.set_account_plan_with_authority('7777','pro');")
    rp = server.handle_event("marketplace_purchase",
                             {"action": "pending_change",
                              "marketplace_purchase": {"account": {"id": 7777}, "plan": {"id": 1, "name": "free"}}},
                             db, gh)
    add("HANDLER pending_change: is a grace-period NO-OP (skipped)", "skipped" in rp)
    add("HANDLER pending_change: the plan is UNCHANGED (still 'pro' — no EARLY downgrade)",
        _plan_of("ACCT-GH-7777") == "pro")

    # A malformed marketplace payload (no account id) is a clean no-op, never a crash.
    rm = server.handle_event("marketplace_purchase", {"action": "purchased", "marketplace_purchase": {}}, db, gh)
    add("HANDLER malformed (no account id): clean no-op, not a crash", "skipped" in rm)

    # ── PIECE 2b — EVENT-ORDERING through the HANDLER (audit iter-5 P2). GitHub re-delivers + does NOT order
    #    webhooks, so a stale cancelled→free arriving AFTER a later purchased→pro must NOT regress a paying customer.
    #    The handler threads the delivery's top-level `effective_date` into the setter's monotonicity guard. Drive it
    #    end-to-end via handle_event (NOT the raw SQL): apply purchased(T_LATER,'pro'), then re-deliver
    #    cancelled(T_EARLIER,'free') — the stale event is refused and the plan stays 'pro'. (A fresh account id so the
    #    high-water mark starts clean.) The detailed boundary/inert/tenant-scope matrix is in test_plan_event_ordering.
    T_LATER, T_EARLIER = "2026-05-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00"
    server.handle_event("marketplace_purchase",
                        {"action": "purchased", "effective_date": T_LATER,
                         "marketplace_purchase": {"account": {"id": 7790}, "plan": {"id": 42, "name": "pro"}}},
                        db, gh)
    add("HANDLER ordering: purchased(T_LATER,'pro') applied (plan='pro')", _plan_of("ACCT-GH-7790") == "pro")
    ro = server.handle_event("marketplace_purchase",
                             {"action": "cancelled", "effective_date": T_EARLIER,
                              "marketplace_purchase": {"account": {"id": 7790}, "plan": {"id": 99, "name": "pro"}}},
                             db, gh)
    # the handler still returns its mapped dict (the refusal is a content-free no-op inside the setter, not an error).
    add("HANDLER ordering: a RE-DELIVERED cancelled(T_EARLIER<T_LATER) does NOT regress the paying plan — it stays "
        f"'pro' (the stale reordered event is refused, no wrongful throttle) (plan={_plan_of('ACCT-GH-7790')})",
        _plan_of("ACCT-GH-7790") == "pro" and ro.get("event") == "marketplace_purchase")

    # ── PIECE 3 — CROSS-TENANT ISOLATION: changing one account's plan never touches another's. ────────────────
    # 5001 is 'pro' (above); 7777 is 'pro' (above). Set a THIRD account paid and confirm the others are untouched;
    # then cancel the third and confirm 5001 stays paid (one plan write is RLS-walled to its own account row).
    psql_app("SET search_path=core; SELECT core.set_account_plan_with_authority('8888','enterprise');")
    add("ISOLATION: setting account 8888 paid leaves account 5001's plan untouched ('pro')", _plan_of("ACCT-GH-5001") == "pro")
    add("ISOLATION: setting account 8888 paid leaves account 7777's plan untouched ('pro')", _plan_of("ACCT-GH-7777") == "pro")
    server.handle_event("marketplace_purchase",
                        {"action": "cancelled", "marketplace_purchase": {"account": {"id": 8888}, "plan": {"name": "enterprise"}}},
                        db, gh)
    add("ISOLATION: cancelling 8888 resets ONLY 8888 (→free), 5001 still 'pro'",
        _plan_of("ACCT-GH-8888") == "free" and _plan_of("ACCT-GH-5001") == "pro")

    # ── PIECE 4 — PERIMETER: the setter is App-delegation ONLY (revoked from PUBLIC; a buyer seat cannot call it).
    # SIGNATURE: the setter carries a trailing p_effective_at timestamptz (audit iter-5 P2 event-ordering); the
    # privilege signature is now (text,text,timestamptz). The 2-arg calls above still resolve (the arg defaults NULL).
    sig = "core.set_account_plan_with_authority(text,text,timestamptz)"
    add("PERIMETER: veripsa_app HAS EXECUTE on the plan setter (the App's delegated path is live)",
        last_value(psql_mig(f"SELECT has_function_privilege('veripsa_app','{sig}','EXECUTE');")) == "t")
    add("PERIMETER: a buyer writer (veripsa_writer) has NO EXECUTE on the plan setter (App-delegation only)",
        last_value(psql_mig(f"SELECT has_function_privilege('veripsa_writer','{sig}','EXECUTE');")) == "f")
    add("PERIMETER: a buyer reader (veripsa_reader) has NO EXECUTE on the plan setter",
        last_value(psql_mig(f"SELECT has_function_privilege('veripsa_reader','{sig}','EXECUTE');")) == "f")
    # A real buyer LOGIN seat (veripsa_demo_agent3 → role veripsa_demo_agent3, provisioned by bootstrap) CANNOT
    # escalate its OWN account by calling the setter directly: the GRANT layer denies it (the writer/reader
    # privilege roles are NOLOGIN, so the canonical buyer-reachability check is a real connecting agent role). We
    # accept ONLY a permission-denied (a generic error must not masquerade as a security denial), and confirm the
    # call wrote NOTHING.
    buyer_dsn = f"postgresql://veripsa_demo_agent3@localhost/{DB}"
    r_buyer = subprocess.run(["psql", buyer_dsn, "-v", "ON_ERROR_STOP=0", "-tAc",
                              "SET search_path=core; SELECT core.set_account_plan_with_authority('5001','enterprise');"],
                             capture_output=True, text=True)
    add("PERIMETER: a buyer SEAT calling the plan setter directly is DENIED at the GRANT layer (no self-upgrade)",
        "permission denied" in (r_buyer.stdout + r_buyer.stderr).lower())
    add("PERIMETER: the buyer's denied self-upgrade did NOT change account 5001's plan (still 'pro')",
        _plan_of("ACCT-GH-5001") == "pro")

    # ── PIECE 5 — FILE-COUNT COVERAGE METER + UPGRADE NUDGE (the PUBLIC billing line: analyzed-file count). ──────
    # The COMMERCIAL coverage line (how big a codebase a plan watches), DISTINCT from the abuse wall above. Tunable
    # owner policy; NULL = unlimited (enterprise / unmapped paid → never nudge a payer). account_coverage_surface()
    # is what the PR check reads to NUDGE early-access limits (advisory; never blocks); render.coverage_nudge_line turns it
    # into the gentle hint. Counts DISTINCT (repo,path) file nodes (branches never double-count; deletes auto-drop).
    import json as _j
    import render as _render
    add("plan_file_limit free=10 (a low line on purpose)",  last_value(psql_mig("SET search_path=core; SELECT core._plan_file_limit('free')")) == "10")
    add("plan_file_limit pro=250",                          last_value(psql_mig("SET search_path=core; SELECT core._plan_file_limit('pro')")) == "250")
    add("plan_file_limit scale=1500",                       last_value(psql_mig("SET search_path=core; SELECT core._plan_file_limit('scale')")) == "1500")
    add("plan_file_limit enterprise=unlimited (NULL)",      last_value(psql_mig("SET search_path=core; SELECT COALESCE(core._plan_file_limit('enterprise')::text,'NULL')")) == "NULL")
    add("plan_file_limit unknown-paid=unlimited (NULL, never nudge a payer)", last_value(psql_mig("SET search_path=core; SELECT COALESCE(core._plan_file_limit('galaxy')::text,'NULL')")) == "NULL")

    # FREE 'inst-free' has ONE stored file node (its 2nd ingest was walled) → UNDER the default line (10) → no nudge.
    cov_free = _j.loads(last_value(psql_app("SET search_path=core; SELECT core.enter_installation_with_authority('inst-free'); SELECT core.account_coverage_surface();")))
    add("coverage(free): plan=free, file_limit=10, file_count counts the analyzed file",
        cov_free.get("plan") == "free" and cov_free.get("file_limit") == 10 and cov_free.get("file_count") >= 1)
    add("coverage(free) UNDER the line → over_by 0 (not nudged)", cov_free.get("over_by") == 0)
    add("nudge(free, under) → None (no spam when within plan)", _render.coverage_nudge_line(cov_free) is None)

    # Tighten the free file line to 0 → the SAME account is now OVER by its file_count (the nudge driver fires).
    psql_app("SET search_path=core; SELECT core.set_plan_limit_with_authority('free',0);")
    cov_over = _j.loads(last_value(psql_app("SET search_path=core; SELECT core.enter_installation_with_authority('inst-free'); SELECT core.account_coverage_surface();")))
    add("coverage(free, line=0) → over_by = file_count (over the plan)",
        cov_over.get("over_by") == cov_over.get("file_count") and cov_over.get("over_by") >= 1)
    _nl = _render.coverage_nudge_line(cov_over)
    add("nudge(over) → an honest early-access limits line (content-free: percent + plan label, no raw count)",
        isinstance(_nl, str)
        and "Plan & limits" in _nl
        and "early-access" in _nl
        and "%" in _nl
        and "Upgrade" not in _nl
        and "analyzed files" not in _nl)
    psql_app("SET search_path=core; SELECT core.set_plan_limit_with_authority('free',10);")   # restore the default line

    # PRO 5001 has 2 stored file nodes (both ingests landed via the paid override) → WAY under 250 → no nudge.
    cov_pro = _j.loads(last_value(psql_app("SET search_path=core; SELECT core.enter_installation_with_authority('5001'); SELECT core.account_coverage_surface();")))
    add("coverage(pro): file_limit=250, UNDER → over_by 0", cov_pro.get("plan") == "pro" and cov_pro.get("file_limit") == 250 and cov_pro.get("over_by") == 0)
    add("nudge(pro, under) → None", _render.coverage_nudge_line(cov_pro) is None)

    # ENTERPRISE → unlimited: set 5001 enterprise (it has files) → file_limit null → never nudged (a payer).
    psql_app("SET search_path=core; SELECT core.set_account_plan_with_authority('5001','enterprise');")
    cov_ent = _j.loads(last_value(psql_app("SET search_path=core; SELECT core.enter_installation_with_authority('5001'); SELECT core.account_coverage_surface();")))
    add("coverage(enterprise): file_limit=null (unlimited), over_by 0 (never nudge a payer)",
        cov_ent.get("file_limit") is None and cov_ent.get("over_by") == 0)
    add("nudge(enterprise) → None (unlimited, never nudge a payer)", _render.coverage_nudge_line(cov_ent) is None)

    # ── PIECE 6 — PLAN-LABEL NORMALIZATION + UNINSTALL RESET (the launch-blocking billing holes this gate MISSED).
    # The earlier pieces only ever fed already-lowercase plan labels ('pro','team','enterprise'), so they never
    # exercised the CASE-sensitivity bug: GitHub's Marketplace FREE plan is listed as "Free" (capital F), and the
    # abuse-wall free-detection compared the stored label case-SENSITIVELY against the literal 'free' — so "Free"
    # read as PAID and EVERY free subscriber skipped the free-tier wall (unlimited free-subscriber writes against
    # the 256 MiB instance). These five cases pin the fix down to behaviour past the wall.

    # (a) THE P0 REPRO — a `purchased` event with plan.name='Free' (CAPITAL F, exactly the listing label). After
    #     the fix the label canonicalizes to lowercase 'free' at the boundary, so the account stays WALLED: it must
    #     be over-quota ENFORCED past the free line (NOT exempt). This is the exact case the gate previously missed.
    server.handle_event("marketplace_purchase",
                        {"action": "purchased",
                         "marketplace_purchase": {"account": {"id": 9100}, "plan": {"id": 1, "name": "Free"}}},
                        db, gh)
    add("P0 REPRO: plan.name='Free' (capital) is stored CANONICAL-lowercase 'free' (no capitalized label survives)",
        _plan_of("ACCT-GH-9100") == "free")
    # give it a footprint over the (zero graph_units) line, then prove the wall bites — a FREE subscriber is NOT
    # exempt. (The first ingest crosses + lands; the over-quota probe then reports the over-dimension = walled.)
    _ingest("9100", "freecap/repo", SHA1, "freecapone")
    add("P0 REPRO: a 'Free' subscriber is WALLED (over-quota ENFORCED past the free line — NOT the paid override)",
        _over_quota("ACCT-GH-9100") in ("graph_units", "repos", "events"))
    r_capwall = _ingest("9100", "freecap/repo2", SHA2, "freecaptwo")
    add("P0 REPRO: a 'Free' subscriber's next write is REFUSED (quota_exceeded — the free wall is genuinely on)",
        "quota_exceeded" in r_capwall)

    # (b) PAID STILL WORKS — both 'Pro' (capital) and 'pro' (lowercase) canonicalize to 'pro' = a real paid plan =
    #     unlimited override. (Proves the lowercasing did not break paid; case no longer matters for a paid label.)
    server.handle_event("marketplace_purchase",
                        {"action": "purchased",
                         "marketplace_purchase": {"account": {"id": 9200}, "plan": {"id": 7, "name": "Pro"}}},
                        db, gh)
    add("PAID(case): plan.name='Pro' (capital) stores as 'pro'", _plan_of("ACCT-GH-9200") == "pro")
    add("PAID(case): the 'Pro' account is over-quota-EXEMPT (the paid override fires)", _over_quota("ACCT-GH-9200") == "NULL")
    server.handle_event("marketplace_purchase",
                        {"action": "purchased",
                         "marketplace_purchase": {"account": {"id": 9201}, "plan": {"id": 7, "name": "pro"}}},
                        db, gh)
    add("PAID(case): plan.name='pro' (lowercase) also stores as 'pro' and is EXEMPT (case-insensitive paid)",
        _plan_of("ACCT-GH-9201") == "pro" and _over_quota("ACCT-GH-9201") == "NULL")

    # (c) A NAME-LESS plan (only an id, no `name`) must map to 'free' = WALLED — NEVER to the id string (an id like
    #     "42" is non-'free', so the old id-fallback silently granted the unlimited override to a name-less plan).
    server.handle_event("marketplace_purchase",
                        {"action": "purchased",
                         "marketplace_purchase": {"account": {"id": 9300}, "plan": {"id": 42}}},
                        db, gh)
    add("NAME-LESS plan → 'free' (NOT the id string '42' — a name-less plan must not become a false paid label)",
        _plan_of("ACCT-GH-9300") == "free")
    _ingest("9300", "nameless/repo", SHA1, "namelessone")
    add("NAME-LESS plan: the account is WALLED (over-quota enforced — no unlimited override from a bare id)",
        _over_quota("ACCT-GH-9300") in ("graph_units", "repos", "events"))

    # (d) UNINSTALL RESETS the plan (P2): a stale paid plan must NOT survive an uninstall and resurrect on reinstall.
    #     Set 9400 paid, give it a footprint, then run the uninstall purge (installation.deleted →
    #     purge_account_working_set_with_authority) IN THE SAME app session after entering the installation (so the
    #     purge resolves THIS tenant from the session pin, exactly like the live event path). After uninstall the
    #     plan must be 'free' (so a reinstall — which re-binds the same installation id to the existing account —
    #     cannot resurrect the paid override) AND the account must be WALLED again on its retained footprint.
    psql_app("SET search_path=core; SELECT core.set_account_plan_with_authority('9400','enterprise');")
    _ingest("9400", "uninst/repo", SHA1, "uninstone")
    _ingest("9400", "uninst/repo2", SHA2, "uninsttwo")
    add("UNINSTALL setup: 9400 is paid ('enterprise') with a footprint and currently EXEMPT",
        _plan_of("ACCT-GH-9400") == "enterprise" and _over_quota("ACCT-GH-9400") == "NULL")
    purge_out = uninstall_account("9400")
    add("UNINSTALL purge ran account-wide (ok:true)",
        isinstance(purge_out, dict) and purge_out.get("ok") and purge_out.get("account_wide"))
    add("UNINSTALL resets the plan to 'free' (a reinstall cannot resurrect the stale paid override)",
        _plan_of("ACCT-GH-9400") == "free")
    # REINSTALL + push: the uninstall purged the old footprint, so to prove the wall RE-ARMS we re-ingest a fresh
    # footprint (the reinstall's next push) and confirm the free wall now bites — the stale 'enterprise' override is
    # gone. (First fresh ingest crosses the zero line + lands; the next is REFUSED — the wall is genuinely back on.)
    reactivate_account("9400")
    _ingest("9400", "uninst/repo3", SHA1, "uninstthree")          # the reinstall's first push (re-ingests under the re-bound tenant)
    add("REINSTALL re-arms the free wall: with plan reset to 'free' the account is over-quota ENFORCED on its new footprint",
        _over_quota("ACCT-GH-9400") in ("graph_units", "repos", "events"))
    r_reinst = _ingest("9400", "uninst/repo4", SHA2, "uninstfour")
    add("REINSTALL: the next write is REFUSED (quota_exceeded) — no lingering paid bypass survived the uninstall",
        "quota_exceeded" in r_reinst)

    # (e) CANCELLED resets to 'free' EVEN when the cancel payload echoes a CAPITALIZED paid label — the cancel path
    #     forces 'free' regardless of the echoed plan name (defense alongside the boundary lowercasing).
    psql_app("SET search_path=core; SELECT core.set_account_plan_with_authority('9500','pro');")
    rc2 = server.handle_event("marketplace_purchase",
                              {"action": "cancelled",
                               "marketplace_purchase": {"account": {"id": 9500}, "plan": {"name": "Pro"}}},
                              db, gh)
    add("CANCELLED: returns plan='free' (cancel forces the free wall, ignores the echoed 'Pro' label)",
        rc2.get("plan") == "free")
    add("CANCELLED: core.account.plan is reset to 'free'", _plan_of("ACCT-GH-9500") == "free")

    # ── PIECE 7 — PER-PLAN graph_units HARD WALL (the commercial quota line: EVERY tier is capped at its line). ──
    # The PO set graph_units (= Σ(node_count+edge_count) over core.graph_version per account) as the commercial
    # line, HARD-enforced PER PLAN: free 6000 · starter 30000 · pro 80000 · scale 200000 · enterprise 500000
    # (enterprise = the for-now cost-safety ceiling, raisable later). BEFORE this change a NON-'free' plan was
    # UNLIMITED on graph_units (the paid bypass returned NULL unconditionally); NOW core._plan_graph_units_limit
    # is the single source of the graph_units line for ALL plans and core._account_over_quota enforces it on every
    # tier. We restore the free graph_units line first (bootstrap zeroed it) so this piece reads the real defaults.
    _set_pgu("free", 6000)
    add("DEFAULT lines: free=6000 · starter=30000 · pro=80000 · scale=200000 · enterprise=500000 (the PO's 实测-calibrated commercial caps)",
        _pgu_limit("free") == "6000" and _pgu_limit("starter") == "30000" and _pgu_limit("pro") == "80000"
        and _pgu_limit("scale") == "200000" and _pgu_limit("enterprise") == "500000")
    # FAIL-SAFE / HARD-WALL POSTURE: an UNMAPPED/unknown paid label is bounded by the for-now ceiling (500000),
    # NEVER unlimited (a HARD wall must never hand an unrecognised plan an unbounded graph — the inverse of the
    # file-meter's NULL=unlimited). This is the launch-safety invariant the file meter does NOT have.
    add("HARD-WALL: an unmapped/unknown plan → 500000 (the for-now ceiling), NOT unlimited",
        _pgu_limit("galaxy") == "500000" and _pgu_limit("") == "6000")

    # PAID TIER IS NOW CAPPED (before→after: previously-unlimited paid is now bounded at its line). Account 5001 is
    # 'enterprise' (Piece 5) with a small footprint. With enterprise's line at its real 500000 it is UNDER → NULL
    # (the previously-unlimited behaviour STILL holds while under the tier cap — paid is not gratuitously walled).
    add("BEFORE→AFTER (paid under its line): enterprise 5001 (small footprint, line=500000) is UNDER → NULL (allowed)",
        _plan_of("ACCT-GH-5001") == "enterprise" and _over_quota("ACCT-GH-5001") == "NULL")
    # Now LOWER the enterprise line BELOW 5001's footprint (the setter — the one-command tune the PO uses on the
    # ceiling). 5001's stored graph (>=1 unit) now EXCEEDS the line → the wall fires on graph_units. This is the
    # proof a previously-UNLIMITED paid account is genuinely CAPPED now (an enterprise tier hits 500000 the same way).
    add("set_plan_graph_units_limit_with_authority('enterprise',0) returns the effective line 0 (tunable, no redeploy)",
        _set_pgu("enterprise", 0) == "0")
    add("PAID NOW CAPPED: enterprise 5001 with a footprint over its (lowered) line is OVER on 'graph_units' — the "
        "paid bypass is GONE (every tier is walled at its line; enterprise at 500000 by default)",
        _over_quota("ACCT-GH-5001") == "graph_units")
    r_ent = _ingest("5001", "entwall/repo", SHA1, "entwallone")
    add("PAID NOW CAPPED: enterprise 5001's next ingest is REFUSED (quota_exceeded) — a HARD wall, not advisory",
        "quota_exceeded" in r_ent)
    # RAISING the line flips the over-account back UNDER (the tunable un-walls — exactly how the PO 解放s a tier).
    add("TUNABLE un-wall: raising enterprise back to 500000 flips 5001 back UNDER → NULL (one command, no redeploy)",
        _set_pgu("enterprise", 500000) == "500000" and _over_quota("ACCT-GH-5001") == "NULL")

    # PER-TIER independence: lowering ONE tier's line does not move another tier's. 9200 ('pro') has no footprint
    # yet (it was created via the purchase handler, never ingested) — give it one (lands: pro line is 80000 ≫ 1),
    # then drop the PRO line below it → 9200 walls on graph_units, while 5001 ('enterprise', restored) stays NULL.
    _ingest("9200", "prowall/repo", SHA1, "prowallone")
    add("set_plan_graph_units_limit_with_authority('pro',0) → effective 0", _set_pgu("pro", 0) == "0")
    add("PER-TIER: pro 9200 (footprint over the lowered pro line) is OVER on 'graph_units'", _over_quota("ACCT-GH-9200") == "graph_units")
    add("PER-TIER: lowering the PRO line did NOT wall the ENTERPRISE account 5001 (independent per-plan lines)",
        _over_quota("ACCT-GH-5001") == "NULL")
    add("PER-TIER restore: raising pro back to 80000 flips 9200 back UNDER → NULL", _set_pgu("pro", 80000) == "80000" and _over_quota("ACCT-GH-9200") == "NULL")

    # FAIL-SAFE DIRECTION (the security-critical invariant of a HARD wall): an above-frame stored line clamps to a
    # SANE value (never unlimited). 2_000_000_000 (within int4, ABOVE the 1e9 frame) clamps to 1000000000 at READ
    # via _policy_int — so a fat-fingered ceiling can never make the wall lie or vanish.
    add("CLAMP: a setter value above the frame clamps to 1000000000 at read (never an unbounded line)",
        _set_pgu("scale", 2000000000) == "1000000000")
    _set_pgu("scale", 200000)   # restore the real scale line

    # PERIMETER: the graph_units line setter is App-delegation ONLY (revoked from PUBLIC; a buyer seat cannot move
    # its own quota wall) — the SAME lock as set_plan_limit_with_authority.
    gsig = "core.set_plan_graph_units_limit_with_authority(text,int)"
    add("PERIMETER: veripsa_app HAS EXECUTE on the graph_units line setter",
        last_value(psql_mig(f"SELECT has_function_privilege('veripsa_app','{gsig}','EXECUTE');")) == "t")
    add("PERIMETER: a buyer writer (veripsa_writer) has NO EXECUTE on the graph_units line setter (no self-raise)",
        last_value(psql_mig(f"SELECT has_function_privilege('veripsa_writer','{gsig}','EXECUTE');")) == "f")
    r_buyer_g = subprocess.run(["psql", buyer_dsn, "-v", "ON_ERROR_STOP=0", "-tAc",
                                "SET search_path=core; SELECT core.set_plan_graph_units_limit_with_authority('enterprise',999999999);"],
                               capture_output=True, text=True)
    add("PERIMETER: a buyer SEAT calling the graph_units line setter directly is DENIED at the GRANT layer (no self-raise)",
        "permission denied" in (r_buyer_g.stdout + r_buyer_g.stderr).lower())

    conn.close()

    # ── verdict ──────────────────────────────────────────────────────────────────────────────────────────
    drop()
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    failed = [label for label, ok in checks if not ok]
    print(f"\n-- {passed}/{total} marketplace-billing assertions passed "
          "(paid short-circuit + free wall unchanged + purchase/changed/cancelled/pending_change handler + "
          "cross-tenant isolation + App-delegation-only perimeter + PER-PLAN graph_units HARD wall "
          "[every tier capped at its line: free 6k/starter 30k/pro 80k/scale 200k/enterprise 500k; paid was "
          "unlimited→now per-plan capped; tunable via set_plan_graph_units_limit_with_authority; clamps + fail-safe]) --")
    if failed:
        print(f"\n[FAIL] {len(failed)} assertion(s) FAILED — the billing seam is unsafe:")
        for f in failed[:40]:
            print(f"   - {f}")
        print("\nMARKETPLACE BILLING GATE: FAIL")
        sys.exit(1)
    print("\nHONEST: this readies the commercial seam (plan→account map + paid short-circuit). It is INERT until "
          "the PO lists the App on Marketplace + creates plans. GitHub bills the customer; Veripsa only maps the "
          "plan. The free-tier wall stays EXACTLY as enforced for 'free' accounts (the fail-safe direction holds).")
    print("MARKETPLACE BILLING GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never leak the scratch DB on an unexpected error
        drop()
        print(f"\n[FAIL] unexpected error: {e}")
        print("MARKETPLACE BILLING GATE: FAIL")
        sys.exit(1)
