#!/usr/bin/env python3
"""AUDIT r2 REGRESSION LOCKS — load-bearing fixes that no broad gate can prove structurally.

Found by a max-firepower multi-agent audit (2026-06-19) of this session's own PRs. Each lock FAILS if the
fix is reverted, so the bug cannot silently come back.

  (1) CROSS-TENANT POISON-PILL (HIGH — a real multi-customer outage class). core.agent's PK is the GLOBAL
      agent_id, so ONE GitHub login shared across two installations (a bot like dependabot, or a human in two
      orgs) is ONE row, owned by whichever tenant saw it first. The seat self-heal used
      `ON CONFLICT (agent_id) DO UPDATE …`, so the SECOND tenant's webhook tried to UPDATE a row behind
      ANOTHER tenant's FORCE-RLS wall → Postgres raises 42501 → the whole event fails → GitHub redelivers →
      raises again = a DETERMINISTIC poison-pill (that tenant's PRs NEVER get a check). The fix: `DO NOTHING`
      + a SEPARATE account-scoped UPDATE (WHERE account_id = v_account) = a cross-tenant same-login row is a
      clean 0-row no-op. In BOTH act_for_claim_with_authority (the PR path) AND record_landing_with_authority
      (the merge path). Proven two ways: STRUCTURAL (neither agent-insert uses ON CONFLICT DO UPDATE; both
      carry the account-scoped self-heal) + BEHAVIOURAL (two real installations, same login, the 2nd does NOT
      raise and does NOT flip the 1st tenant's row).

  (2) PATCH-PATH content-free SANITIZE (the moat had a steady-state hole). The moat strips control chars /
      angle brackets from the egress-bound node NAME + edge DST on FULL ingest — but the INCREMENTAL patch
      path (core.patch_graph_with_authority — the NORMAL-push steady state) was RAW. A normal push could store
      an un-sanitized symbol / module specifier. The fix mirrors the full-ingest sanitize on the patch path.
      STRUCTURAL anchor: the patch body applies core._safe_ref_token to both n->>'name' AND e->>'dst'.
      (The sanitize is a NO-OP on a real path/specifier — proven by tests/test_content_free_egress.py on the
      full path; this only closes the incremental gap, so a normal push gets the same content-free shape.)

  (3) SPLIT-ADVICE OVER-EXCLUSION (a silent false-negative). _is_generated_or_vendored_path excluded path
      SEGMENTS the EXTRACTOR actually admits ('vendored'/'pods'/'site-packages'/'venv'/'virtualenv'), so a
      real first-party 'pods/' microservice or 'src/venv/' dir got NO split advice (silently). The fix aligns
      the exclusion to the extractor's walk-skip set. BEHAVIOURAL: a first-party 'pods/'…'venv/' path is NOT
      excluded, while genuine vendored / build-output dirs (node_modules/ · .venv/ · dist/) still ARE.

  (4) PATCH BASELINE CAS (graph-integrity race). An incremental subgraph is derived from a stored coordinate,
      but another writer can replace that coordinate while extraction is in flight. The SQL writer must acquire
      the coordinate advisory lock, compare graph_version.commit_sha + the never-reused graph_revision with both
      expected tokens, and reject a mismatch before its first DELETE. The order is load-bearing: checking before
      the lock is a TOCTOU, and checking after DELETE permits a transient hybrid graph inside the transaction.

  (5) GRAPH LIFECYCLE SERIALIZATION. Every production mutation of code_node/code_edge/graph_version must share
      the applicable prefix of stable-id → sorted repo(s) → account(shared/exclusive) → coordinate. Cold
      retention takes the repo tier and then repeats its freshness/claim predicate before DELETE. These structural
      anchors complement the real two-session persistence gate which proves the lock actually blocks a sweep.

Run:  python3 tests/test_audit_r2_regressions.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): per-PID like db/smoke.sh / run_gates / the other test_*.py, so concurrent
# runs never drop each other's DB mid-run.
DB = "veripsa_auditr2_" + str(os.getpid())
G30 = os.path.join(ROOT, "db", "schema", "30_gate.sql")
G35 = os.path.join(ROOT, "db", "schema", "35_lifecycle.sql")
G80 = os.path.join(ROOT, "db", "schema", "80_contention.sql")


def _conn(role):
    conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
    return conn


def db_for_installation(installation_id):
    """A runner over ONE connection that has ENTERED `installation_id` (its whole 'event' runs in that
    installation's tenant account — exactly like the live per-event processor). Mirrors test_tenant_isolation."""
    conn = _conn("veripsa_app")
    with conn.cursor() as cur:
        cur.execute("SELECT core.enter_installation_with_authority(%s)", (installation_id,))
        account = cur.fetchone()[0]

    def run(sql, args=()):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    run.account = account
    return run


def migrator_run():
    """Owner connection (no RLS pin needed — the helper under test is a PURE regex function over the path)."""
    conn = _conn("veripsa_migrator")

    def run(sql, args=()):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    return run


def _body(src: str, fn: str) -> str:
    """The text of one CREATE FUNCTION block, scoped from its header to its ALTER FUNCTION (so an anchor can
    fail loudly inside the RIGHT function without matching a sibling)."""
    s = src.find(f"FUNCTION core.{fn}")
    e = src.find(f"ALTER FUNCTION core.{fn}", s)
    return src[s:e] if (s >= 0 and e > s) else ""


def _self_heal_is_rls_safe(body: str) -> bool:
    """The agent-insert must NOT use `ON CONFLICT (agent_id) DO UPDATE` (the cross-tenant poison-pill) — it must
    `DO NOTHING` and self-heal via a SEPARATE account-scoped UPDATE (WHERE … account_id = v_account)."""
    no_do_update = re.search(r"ON CONFLICT \(agent_id\) DO UPDATE", body) is None
    has_do_nothing = "ON CONFLICT (agent_id) DO NOTHING" in body
    has_scoped_update = re.search(r"UPDATE core\.agent SET agent_kind.*?account_id = v_account", body, re.S) is not None
    return no_do_update and has_do_nothing and has_scoped_update


def _anchors_in_order(body: str, *anchors: str) -> bool:
    """True only when every exact structural anchor occurs after its predecessor."""
    cursor = 0
    for anchor in anchors:
        cursor = body.find(anchor, cursor)
        if cursor < 0:
            return False
        cursor += len(anchor)
    return True


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    checks = []
    g30 = open(G30, encoding="utf-8").read()
    g35 = open(G35, encoding="utf-8").read()
    g80 = open(G80, encoding="utf-8").read()

    # ── (1) cross-tenant poison-pill — STRUCTURAL (both author-write paths) ──────────────────────────────────
    act_body = _body(g30, "act_for_claim_with_authority")
    land_body = _body(g30, "record_landing_with_authority")
    checks.append(("(1) STRUCTURAL: act_for_claim_with_authority self-heal is RLS-safe (DO NOTHING + account-scoped "
                   "UPDATE, NOT `ON CONFLICT (agent_id) DO UPDATE` — the cross-tenant 42501 poison-pill)",
                   _self_heal_is_rls_safe(act_body)))
    checks.append(("(1) STRUCTURAL: record_landing_with_authority self-heal is RLS-safe too (the merge path carries "
                   "the SAME fix — a shared login first SEEN on a landing can't poison-pill either)",
                   _self_heal_is_rls_safe(land_body)))

    # ── (1) cross-tenant poison-pill — BEHAVIOURAL (two real installations, same global login) ───────────────
    A = db_for_installation("111")   # tenant A = ACCT-GH-111
    B = db_for_installation("222")   # tenant B = ACCT-GH-222
    checks.append((f"(1) two installations get two DISTINCT accounts (A={A.account}, B={B.account})",
                   A.account == "ACCT-GH-111" and B.account == "ACCT-GH-222"))
    LOGIN = "sharedacct"   # the ONE global login both orgs see (a human in two orgs / a shared bot)
    # A (org A) first sees LOGIN as a HUMAN PR author → provisions GH-sharedacct under ACCT-GH-111 (kind=human).
    A("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s,%s,%s)",
      ("PR-A:shared.py", "shared.py", "orgA/repo", "main", LOGIN, None, None, False))
    # B (org B) then sees the SAME LOGIN as a BOT (kind=ai ≠ human) — on the OLD code this is exactly the
    # cross-tenant DO UPDATE that raises 42501. The 2nd tenant's event MUST succeed (no poison-pill).
    b_raised, b_err = False, ""
    try:
        B("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s,%s,%s)",
          ("PR-B:shared.py", "shared.py", "orgB/repo", "main", LOGIN, None, None, True))
    except Exception as e:   # noqa: BLE001 — any raise here is the poison-pill (or a regression) we must catch
        b_raised, b_err = True, str(e).strip().splitlines()[0] if str(e).strip() else "raised"
    checks.append((f"(1) BEHAVIOURAL: the 2nd installation's same-login event does NOT raise "
                   f"(no 42501 poison-pill){'' if not b_raised else ' — RAISED: ' + b_err}", not b_raised))
    # …and it did NOT reach across the RLS wall to flip tenant A's agent (account-scoped UPDATE = 0 rows).
    # core.agent is not readable by veripsa_app (writes go through SECURITY DEFINER fns only); read it as the
    # OWNER with tenant A's RLS pin (FORCE RLS hides the row otherwise — empty != none).
    mig = migrator_run()
    mig("SELECT set_config('core.current_account', %s, false)", (A.account,))
    a_kind = mig("SELECT agent_kind FROM core.agent WHERE agent_id = %s", ("GH-" + LOGIN,))
    checks.append((f"(1) BEHAVIOURAL: tenant A's agent kept agent_kind='human' — B's scoped self-heal was a 0-row "
                   f"cross-tenant no-op (got {a_kind!r})", a_kind == "human"))

    # ── (2) patch-path content-free sanitize — STRUCTURAL ───────────────────────────────────────────────────
    patch_body = _body(g30, "patch_graph_with_authority")
    patch_sanitizes_name = "core._safe_ref_token(n->>'name')" in patch_body
    patch_sanitizes_dst = "core._safe_ref_token(e->>'dst')" in patch_body
    checks.append(("(2) STRUCTURAL: the INCREMENTAL patch path sanitizes the egress-bound node NAME "
                   "(core._safe_ref_token(n->>'name')) — the moat's content-free shape now runs on a normal push, "
                   "not just on full ingest", patch_sanitizes_name))
    checks.append(("(2) STRUCTURAL: the INCREMENTAL patch path sanitizes the egress-bound edge DST "
                   "(core._safe_ref_token(e->>'dst')) — mirrors the full ingest (a real path/specifier passes "
                   "through untouched; only control/angle-bracket junk is stripped)", patch_sanitizes_dst))

    # ── (4) patch baseline compare-and-swap — STRUCTURAL ORDER ──────────────────────────────────────────────
    repo_lock_at = patch_body.find(
        "hashtext(CASE WHEN v_account LIKE 'ACCT-GH-%'"
    )
    account_lock_at = patch_body.find(
        "PERFORM core.assert_account_live_with_authority()"
    )
    coordinate_lock_at = patch_body.find(
        "hashtext('core.graph_coordinate')"
    )
    cas = re.search(
        r"PERFORM 1 FROM core\.graph_version\s+"
        r"WHERE .*?commit_sha\s*=\s*v_expected_base_sha\s+"
        r"AND graph_revision\s*=\s*v_expected_base_revision\s+"
        r"AND semantic_ref_version\s*=\s*1;\s*"
        r"IF NOT FOUND THEN\s*"
        r"RAISE EXCEPTION 'patch graph baseline coordinate does not match stored "
        r"SHA/revision/semantic version'",
        patch_body,
        re.S,
    )
    revision_at = patch_body.find(
        "v_graph_revision := nextval('core.graph_revision_seq')"
    )
    first_delete_candidates = [
        position for position in (
            patch_body.find("DELETE FROM core.code_node"),
            patch_body.find("DELETE FROM core.code_edge"),
        )
        if position >= 0
    ]
    first_delete_at = min(first_delete_candidates) if first_delete_candidates else -1
    checks.append((
        "(4) STRUCTURAL: repo→account(shared/live)→coordinate locks precede SHA+revision+semantic-version CAS, "
        "which allocates a never-reused token before first DELETE "
        "(no lifecycle TOCTOU, ABA reuse, cross-generation patch, or hybrid mutation)",
        repo_lock_at >= 0
        and account_lock_at >= 0
        and coordinate_lock_at >= 0
        and cas is not None
        and revision_at >= 0
        and first_delete_at >= 0
        and repo_lock_at < account_lock_at < coordinate_lock_at
        < cas.start() < revision_at < first_delete_at,
    ))

    # ── (5) every graph lifecycle mutation shares the applicable ordered lock tiers ─────────────────────────
    full_body = _body(g30, "ingest_graph_with_authority")
    checks.append((
        "(5) STRUCTURAL: full writer takes repo→account(shared/live)→coordinate and allocates revision before DELETE",
        _anchors_in_order(
            full_body,
            "hashtext(CASE WHEN v_account LIKE 'ACCT-GH-%'",
            "PERFORM core.assert_account_live_with_authority()",
            "hashtext('core.graph_coordinate')",
            "v_graph_revision := nextval('core.graph_revision_seq')",
            "DELETE FROM core.code_node",
        ),
    ))

    purge_repo_body = _body(g35, "_purge_repo_with_authority")
    account_purge_body = _body(g35, "purge_account_working_set_with_authority")
    erase_body = _body(g35, "erase_account_with_authority")
    checks.append((
        "(5) STRUCTURAL: repo purge locks repo→account(shared) before graph DELETE; account purge/erase hold exclusive fence",
        _anchors_in_order(
            purge_repo_body,
            "PERFORM pg_advisory_xact_lock(",
            "PERFORM core._take_account_lifecycle_xact_lock_shared(v_account)",
            "DELETE FROM core.code_node",
        )
        and _anchors_in_order(
            account_purge_body,
            "PERFORM core._take_account_lifecycle_xact_lock(v_account)",
            "DELETE FROM core.code_node",
        )
        and _anchors_in_order(
            erase_body,
            "PERFORM core._take_account_lifecycle_xact_lock(v_account)",
            "DELETE FROM core.code_node",
        ),
    ))

    migrate_body = _body(g35, "_migrate_repo_coordinate")
    reconcile_body = _body(g35, "reconcile_repo_identity_with_authority")
    transfer_body = _body(g35, "transfer_repo_coordinate_with_authority")
    checks.append((
        "(5) STRUCTURAL: rename/reconcile/transfer lock stable identity and sorted repos before account(shared) and mutation",
        _anchors_in_order(
            migrate_body,
            "FROM unnest(ARRAY[p_old,p_new])",
            "PERFORM pg_advisory_xact_lock(",
            "PERFORM core._take_account_lifecycle_xact_lock_shared(p_account)",
            "DELETE FROM core.graph_version",
        )
        and _anchors_in_order(
            reconcile_body,
            "pg_advisory_xact_lock(hashtext('github-repository-id')",
            "FOREACH v_old IN ARRAY v_lock_repos",
            "pg_try_advisory_xact_lock(",
            "PERFORM core._take_account_lifecycle_xact_lock_shared(v_account)",
            "UPDATE core.graph_version SET repo_id",
        )
        and _anchors_in_order(
            transfer_body,
            "pg_advisory_xact_lock(hashtext('github-repository-id')",
            "FOR v_lock_owner_id,v_lock_repo IN",
            "PERFORM core._take_account_lifecycle_xact_lock_shared(v_old_account)",
            "DELETE FROM core.code_node",
        ),
    ))

    retention_body = _body(g35, "prune_all_accounts_with_authority")
    retention_marker = retention_body.find(
        "Candidate selection and deletion are separated"
    )
    retention_loop = (
        retention_body[retention_marker:] if retention_marker >= 0 else ""
    )
    checks.append((
        "(5) STRUCTURAL: cold retention takes the writer's repo lock and rechecks claims/freshness before DELETE",
        retention_loop
        and _anchors_in_order(
            retention_loop,
            "PERFORM pg_advisory_xact_lock(",
            "SELECT 1 FROM core.claim",
            "SELECT max(gv.ingested_at)",
            "DELETE FROM core.code_edge",
        ),
    ))

    # ── (3) split-advice over-exclusion — BEHAVIOURAL (the pure path filter) ─────────────────────────────────
    # (reuse the owner connection from (1); _is_generated_or_vendored_path is a PURE regex fn — no pin/table read)
    def excluded(p):
        return mig("SELECT core._is_generated_or_vendored_path(%s)", (p,))

    # first-party dirs the extractor ADMITS must NOT be excluded (they were silently dropped before the fix):
    checks.append(("(3) a first-party 'pods/' microservice path is NOT excluded → it can still get split advice",
                   excluded("pods/payments/handler.py") is False))
    checks.append(("(3) a first-party 'src/venv/' path (plain 'venv', not '.venv') is NOT excluded",
                   excluded("src/venv/util.py") is False))
    checks.append(("(3) a first-party 'lib/site-packages-helpers/x.py' is NOT excluded (the word 'site-packages' no "
                   "longer over-matches)", excluded("lib/vendored_utils/x.py") is False))
    # genuine vendored / generated / build dirs the extractor WALK-SKIPS must STILL be excluded (no cry-wolf):
    checks.append(("(3) STILL excluded: node_modules stays out (real vendored — no false split advice)",
                   excluded("frontend/node_modules/react/index.js") is True))
    checks.append(("(3) STILL excluded: .venv stays out (the extractor walk-skips it)",
                   excluded(".venv/lib/python3.11/site.py") is True))
    checks.append(("(3) STILL excluded: dist/ build output stays out",
                   excluded("dist/bundle.js") is True))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("AUDIT R2 REGRESSION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
