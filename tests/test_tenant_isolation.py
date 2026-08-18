#!/usr/bin/env python3
"""MULTI-TENANT ISOLATION gate (security-critical). Two GitHub installations must land in SEPARATE accounts
and must NOT be able to see each other's data — enforced by account-RLS, routed by enter_installation.

If this ever fails, do NOT ship: one customer would see another customer's code structure / collisions.

Run:  python3 tests/test_tenant_isolation.py   (needs local Postgres with the veripsa roles)
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
DB = "veripsa_tenanttest_" + str(os.getpid())


def db_for_installation(installation_id):
    """A db runner over ONE connection that has ENTERED `installation_id` (so the whole 'event' runs in that
    installation's tenant account, exactly like the live per-event processor)."""
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", (installation_id,))
        account = cur.fetchone()[0]

    def run(sql, args=()):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    run.account = account
    return run


def surf(db, repo):
    s = db("SELECT core.main_impact_surface(%s,%s)", (repo, "main"))
    return s if isinstance(s, dict) else json.loads(s)


def j(db, sql, args=()):
    """run a surface that RETURNS jsonb and parse it to a dict/list."""
    s = db(sql, args)
    return s if isinstance(s, (dict, list)) else (json.loads(s) if s is not None else None)


def blob(x):
    """flatten any jsonb result to one searchable string (every id / path / account / login it carries)."""
    return json.dumps(x, default=str)


def probe_overlapping(checks, A, B):
    """ADVERSARIAL per-surface isolation: A and B share the SAME repo full_name, SAME file paths, SAME author
    login, SAME PR change ids — the exact case where a surface that filters by repo/path but FORGOT the account
    pin would surface B's rows to A. We tag each account with a UNIQUE marker token (only in that account's own
    rows) and, after calling each surface AS A, assert (a) A sees its OWN marker and (b) A's result contains
    NEITHER B's marker token NOR B's private author NOR B's unique-only path/connection/utterance. A leak shows
    up as B's marker bleeding into A's surface output.

    Shared (overlapping) coordinate: REPO + BR + the two shared paths + the shared author 'carol' + PR-9.
    Per-account unique markers: a path / connection / policy / statement that exists ONLY in that account, so a
    missing account filter (repo-only) would pull the OTHER account's unique row into THIS account's surface."""
    REPO, BR = "acme/shared-repo", "main"
    P1, P2 = "core/pay.py", "core/charge.py"          # identical paths in BOTH accounts
    # ---- seed account A (marker 'MARK-A') --------------------------------------------------------------------
    # a graph at the SHARED coordinate: pay.py defines charge(); charge.py calls it (a real A→B coupling),
    # plus an account-unique file 'aonly.py' that ONLY A has.
    gA = {"nodes": [
        {"id": P1, "kind": "file", "path": P1, "language": "python"},
        {"id": P1 + "::charge", "kind": "def", "path": P1, "name": "charge", "start_line": 1, "end_line": 9},
        {"id": P2, "kind": "file", "path": P2, "language": "python"},
        {"id": "aonly.py", "kind": "file", "path": "aonly.py", "language": "python"},
    ], "edges": [{"src": P2, "dst": "charge", "kind": "calls"}]}
    A("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(gA), REPO, BR, "a" * 40))
    # two in-flight changes that COLLIDE (same path) + are semantically adjacent → exercises main_impact_surface
    A("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-9:" + P1, P1, REPO, BR, "carol"))
    A("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-9:" + P2, P2, REPO, BR, "carol"))
    A("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-9b:" + P1, P1, REPO, BR, "dave"))  # waiter→serialize
    A("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-A1:aonly", "aonly.py", REPO, BR, "carol"))
    A("SELECT core.record_collision_with_authority(%s,%s,%s,%s)", (P1, REPO, BR, "dave"))           # collision_held (A)
    A("SELECT core.record_warn_with_authority(%s,%s,%s,%s)", (P2, REPO, BR, "MARK-A-warn"))         # warn_issued (A)
    A("SELECT core.record_landing_with_authority(%s,%s,%s,%s,%s)", (REPO, BR, "a" * 40, [P1], "carol"))
    A("SELECT core.record_landing_with_authority(%s,%s,%s,%s,%s)", (REPO, BR, "a" * 39 + "b", [P1], "erin"))  # same path, diff author → collisions_on_main (A)
    A("SELECT core.record_pr_failing_with_authority(%s,%s,%s,%s,%s)", ("PR-9", REPO, BR, "a" * 40, "ci_failed"))
    A("SELECT core.record_statement_with_authority(%s,%s,%s,%s)", ("MARK-A-stmt", P1, REPO, BR))
    A("SELECT core.connect_store_with_authority(%s,%s,%s,%s)", ("CONN-MARK-A", "s3", "s3://a-only", "a/"))
    A("SELECT core.set_policy_with_authority(%s,%s)", ("stalled_waiting_minutes", "5"))
    A("SELECT core.follow_account_with_authority(%s)", (B.account,))  # A follows B (a real cross-acct edge it owns)

    # ---- seed account B (marker 'MARK-B') — the SAME repo/paths/author, plus B-unique rows -------------------
    gB = {"nodes": [
        {"id": P1, "kind": "file", "path": P1, "language": "python"},
        {"id": P1 + "::charge", "kind": "def", "path": P1, "name": "charge", "start_line": 1, "end_line": 9},
        {"id": P2, "kind": "file", "path": P2, "language": "python"},
        {"id": "bonly.py", "kind": "file", "path": "bonly.py", "language": "python"},
    ], "edges": [{"src": P2, "dst": "charge", "kind": "calls"}]}
    B("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(gB), REPO, BR, "b" * 40))
    B("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-9:" + P1, P1, REPO, BR, "carol"))
    B("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-9:" + P2, P2, REPO, BR, "carol"))
    B("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-9b:" + P1, P1, REPO, BR, "dave"))
    B("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-B1:bonly", "bonly.py", REPO, BR, "carol"))
    B("SELECT core.record_collision_with_authority(%s,%s,%s,%s)", (P1, REPO, BR, "dave"))
    B("SELECT core.record_warn_with_authority(%s,%s,%s,%s)", (P2, REPO, BR, "MARK-B-warn"))
    B("SELECT core.record_landing_with_authority(%s,%s,%s,%s,%s)", (REPO, BR, "b" * 40, [P1], "carol"))
    B("SELECT core.record_landing_with_authority(%s,%s,%s,%s,%s)", (REPO, BR, "b" * 39 + "a", [P1], "frank"))
    B("SELECT core.record_pr_failing_with_authority(%s,%s,%s,%s,%s)", ("PR-9", REPO, BR, "b" * 40, "conflict"))
    B("SELECT core.record_statement_with_authority(%s,%s,%s,%s)", ("MARK-B-stmt", P1, REPO, BR))
    B("SELECT core.connect_store_with_authority(%s,%s,%s,%s)", ("CONN-MARK-B", "s3", "s3://b-only", "b/"))
    B("SELECT core.set_policy_with_authority(%s,%s)", ("stalled_waiting_minutes", "9"))

    # B's private tokens that MUST NEVER appear in any of A's surfaces (the leak signature):
    B_TOKENS = ["MARK-B", "bonly.py", "CONN-MARK-B", "s3://b-only", B.account]

    def assert_a_only(label, surface_dict, must_have=(), surface_for_count=None):
        """A's surface must (a) contain A's own markers `must_have` and (b) contain NONE of B's private tokens."""
        s = blob(surface_dict)
        a_ok = all(m in s for m in must_have)
        b_leak = [t for t in B_TOKENS if t in s]
        checks.append((f"ISOLATION[{label}]: A sees its own rows, NONE of B (overlapping repo/path/author)"
                       + (f" — A-markers ok={a_ok}" if must_have else "")
                       + (f" — B-LEAK={b_leak}" if b_leak else ""),
                       a_ok and not b_leak))

    # ---- call EVERY surface AS A, assert A-only --------------------------------------------------------------
    # main_impact_surface (the brain): the shared repo, A-pinned. Must show A's PR-9 / dave waiter, never B.
    mi = j(A, "SELECT core.main_impact_surface(%s,%s)", (REPO, BR))
    assert_a_only("main_impact_surface", mi)
    # contention_surface (working-branch view over _claim_adjacency)
    assert_a_only("contention_surface", j(A, "SELECT core.contention_surface(%s,%s)", (REPO, BR)))
    # split_candidates (collisions + fan-in/churn over the shared repo)
    assert_a_only("split_candidates", j(A, "SELECT core.split_candidates(%s,%s)", (REPO, BR)))
    # collisions_on_main (same-path landings by different authors)
    com = j(A, "SELECT core.collisions_on_main(%s,%s,%s)", (REPO, BR, "14 days"))
    assert_a_only("collisions_on_main", com)
    # coordinate_file_paths (RETURNS text[]) — must list A's files incl aonly.py, never bonly.py
    cfp = A("SELECT to_jsonb(core.coordinate_file_paths(%s,%s))", (REPO, BR))
    assert_a_only("coordinate_file_paths", cfp if isinstance(cfp, (list, dict)) else json.loads(cfp),
                  must_have=["aonly.py"])
    # directory_surface (the home tree for the shared coordinate)
    assert_a_only("directory_surface", j(A, "SELECT core.directory_surface(%s,%s)", (REPO, BR)),
                  must_have=["aonly.py"])
    # board_surface (live fleet + queues) — A's claims only
    assert_a_only("board_surface", j(A, "SELECT core.board_surface()"))
    # collision_surface (held / steered)
    assert_a_only("collision_surface", j(A, "SELECT core.collision_surface()"))
    # effect_surface (the effect ledger, incl. collisions_occurred via collisions_on_main)
    assert_a_only("effect_surface", j(A, "SELECT core.effect_surface()"))
    # stalled_work_surface / stuck_prs_surface (lifecycle-derived)
    assert_a_only("stalled_work_surface", j(A, "SELECT core.stalled_work_surface()"))
    assert_a_only("stuck_prs_surface", j(A, "SELECT core.stuck_prs_surface()"))
    # meaning_surface (statements) — A's MARK-A-stmt only
    assert_a_only("meaning_surface", j(A, "SELECT core.meaning_surface()"), must_have=["MARK-A-stmt"])
    # profile_surface / account_surface (standing + seats)
    assert_a_only("profile_surface", j(A, "SELECT core.profile_surface()"), must_have=[A.account])
    assert_a_only("account_surface", j(A, "SELECT core.account_surface()"), must_have=[A.account])
    # notifications_surface (recent facts) — it renders kind/path/repo/agent (NOT the warn's detail label), so
    # the A-only positive marker is A's own non-empty feed; the security assertion is zero B tokens. Both A and B
    # warn on the SHARED path, so a missing account filter would DOUBLE the feed with B's identical-path events —
    # caught by both the B-token check (B.account never appears) and the exact-count check below.
    notif = j(A, "SELECT core.notifications_surface()")
    assert_a_only("notifications_surface", notif)
    notif_b = j(B, "SELECT core.notifications_surface()")
    na, nb = len(notif.get("items") or []), len(notif_b.get("items") or [])
    # A and B were seeded SYMMETRICALLY on the shared coordinate, so each must see the SAME count of its OWN
    # events. A LEAK (missing account filter) would make A see A's + B's events (≈ 2× nb) — this count guard
    # catches a leak that carries no literal B token (e.g. an identical-path collision_held event).
    checks.append((f"ISOLATION[notifications_surface]: A sees its OWN events only — count A={na} == B={nb} (a leak would ≈ double it)",
                   na >= 1 and na == nb))
    # branch_surface (coordinates + pushes/landings)
    assert_a_only("branch_surface", j(A, "SELECT core.branch_surface()"))
    # list_store_connections — A's CONN-MARK-A only, never CONN-MARK-B
    assert_a_only("list_store_connections", j(A, "SELECT core.list_store_connections()"), must_have=["CONN-MARK-A"])
    # get_policies — A's value (5), never B's (9). (B's policy value is content-free; the leak token is the account.)
    pol = j(A, "SELECT core.get_policies()")
    assert_a_only("get_policies", pol)
    checks.append((f"ISOLATION[get_policies]: A reads its OWN policy value (got {pol.get('stalled_waiting_minutes')}, A set 5)",
                   pol.get("stalled_waiting_minutes") == "5"))
    # export_my_account (composite of account/profile/meaning/notifications/connections) — must be all-A
    assert_a_only("export_my_account", j(A, "SELECT core.export_my_account()"),
                  must_have=["MARK-A-stmt", "CONN-MARK-A"])
    # change_concluded (boolean over claims): PR-9 is still in-flight in A → not concluded. A repo-only filter
    # could be confused by B's identical PR-9, but the account pin keeps it A-scoped.
    cc = A("SELECT core.change_concluded(%s,%s)", (REPO, "PR-9"))
    checks.append(("ISOLATION[change_concluded]: A's PR-9 (in-flight) is not falsely 'concluded' by B's identical PR-9",
                   cc is False))

    # discover_surface: the DESIGNED cross-account feed shows ONLY visibility='public' rows. Both A and B seeded
    # ONLY private rows (the gated path never sets public), so A's discover feed must contain NEITHER B's private
    # account NOR A's own private markers — it is honest-empty of private data. (This documents the public_readable
    # policy is SELECT-only over opt-in public rows, and private rows stay tenant-walled even on the social face.)
    disc = j(A, "SELECT core.discover_surface()")
    db_disc = blob(disc)
    priv_leak = [t for t in (["MARK-A", "MARK-B", A.account, B.account]) if t in db_disc]
    checks.append((f"ISOLATION[discover_surface]: the public feed leaks NO private rows (no account/marker present; leak={priv_leak})",
                   not priv_leak))


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    checks = []

    A = db_for_installation("111")     # tenant A = installation 111
    B = db_for_installation("222")     # tenant B = installation 222

    checks.append((f"two installations get two DISTINCT, isolated accounts (A={A.account}, B={B.account})",
                   A.account and B.account and A.account != B.account
                   and A.account == "ACCT-GH-111" and B.account == "ACCT-GH-222"))

    # each tenant ingests its OWN repo graph + opens a PR (different repo names, different authors)
    gA = {"nodes": [{"id": "a.py", "kind": "file", "path": "a.py", "language": "python"}], "edges": []}
    gB = {"nodes": [{"id": "b.py", "kind": "file", "path": "b.py", "language": "python"}], "edges": []}
    A("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(gA), "orgA/repo", "main", "a" * 40))
    B("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(gB), "orgB/repo", "main", "b" * 40))
    A("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-1:a.py", "a.py", "orgA/repo", "main", "alice"))
    B("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("PR-1:b.py", "b.py", "orgB/repo", "main", "bob"))

    # each tenant SEES its own in-flight work
    sa_own, sb_own = surf(A, "orgA/repo"), surf(B, "orgB/repo")
    checks.append((f"tenant A sees its OWN in-flight PR (orgA/repo inflight={sa_own.get('inflight_count')})",
                   sa_own.get("inflight_count", 0) >= 1))
    checks.append((f"tenant B sees its OWN in-flight PR (orgB/repo inflight={sb_own.get('inflight_count')})",
                   sb_own.get("inflight_count", 0) >= 1))

    # ISOLATION: neither tenant can see the OTHER's repo at all (account-RLS walls it off)
    sa_cross = surf(A, "orgB/repo")
    sb_cross = surf(B, "orgA/repo")
    checks.append((f"ISOLATION: tenant A canNOT see tenant B's repo (orgB/repo from A inflight={sa_cross.get('inflight_count')})",
                   sa_cross.get("inflight_count", 0) == 0 and not (sa_cross.get("changes") or [])))
    checks.append((f"ISOLATION: tenant B canNOT see tenant A's repo (orgA/repo from B inflight={sb_cross.get('inflight_count')})",
                   sb_cross.get("inflight_count", 0) == 0 and not (sb_cross.get("changes") or [])))

    # belt-and-braces: the App role cannot even read core.claim directly (no table grant) — data is reachable
    # ONLY through the account-scoped gated functions. So a cross-tenant raw read is impossible by construction.
    raw_denied = False
    try:
        A("SELECT count(*) FROM core.claim WHERE repo='orgB/repo'")
    except psycopg2.errors.InsufficientPrivilege:
        raw_denied = True
    checks.append(("ISOLATION: the App role cannot read core.claim directly — only via account-scoped gates",
                   raw_denied))

    # a NON-installation connection (no enter) falls back to the role's own account — local/dogfood unaffected
    plain = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    plain.autocommit = True
    with plain.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT account FROM core.resolve_session_identity() AS r(agent,account)")
        plain_account = cur.fetchone()[0]
    plain.close()
    checks.append((f"no-installation connection falls back to the role's own account (got {plain_account})",
                   plain_account == "ACCT-DEMO"))

    # ADVERSARIAL per-surface sweep: A and B share the SAME repo full_name / paths / author / PR ids, then call
    # EVERY read surface as A and prove A sees ONLY its own rows (a missing account filter would surface B's).
    probe_overlapping(checks, A, B)

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("TENANT ISOLATION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
