#!/usr/bin/env python3
"""G4 POLICY-CHANGE REFRESH gate.

Proves the durable-outbox refresh that turns "an owner tuned a policy knob" into "that tenant's OPEN in-flight
PRs re-derive their posted Check under the new policy" — WITHOUT a GitHub call inside the policy-write txn.

Two layers:
  * SQL layer (the outbox contract) — enqueue coalescing, ROLLBACK-safety (negative control), FORCE-RLS
    tenant isolation (negative control), claim/finish CAS, fast retry→poison-isolated slow retry, the
    generation/uninstall fence,
    and the owner-account no-op.
  * DRAINER layer (the worker orchestration) — refreshes ONLY the writing tenant's repos (negative control:
    a second tenant with open PRs but no policy change is untouched), does NO GitHub work while an outbox
    transaction is open (seam), skips a dead install (uninstalled between enqueue and drain), no-ops when the
    tenant has no open PRs, and moves persistent failures onto the bounded low-frequency retry lane.

Run: python3 tests/test_policy_change_refresh.py
"""
from __future__ import annotations

import os
import json
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import psycopg2  # noqa: E402

import policy_refresh_queue as PR  # noqa: E402
import code_graph_extract as X  # noqa: E402
import ingest  # noqa: E402
import webhook as W  # noqa: E402

DB = "veripsa_polrefresh_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
DSN_MIG = f"postgresql://veripsa_migrator@localhost/{DB}"

checks: list[bool] = []


def chk(condition, label: str) -> None:
    passed = bool(condition)
    print(("  [PASS] " if passed else "  [FAIL] ") + label)
    checks.append(passed)


# ── DB helpers ────────────────────────────────────────────────────────────────────────────────────────────
def _run(dsn: str, sql: str, args=(), *, fetch=False):
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            if fetch:
                return cur.fetchall()
            if cur.description:
                row = cur.fetchone()
                return row[0] if row else None
            return None
    finally:
        conn.close()


def backend_transaction_state(pid: int) -> dict:
    """Observe the refresh connection from a second backend at an injected slow-call seam."""
    raw = _run(
        DSN_APP,
        "SELECT jsonb_build_object("
        "'state',state,'xact_start_is_null',xact_start IS NULL,"
        "'backend_xid_is_null',backend_xid IS NULL"
        ") FROM pg_stat_activity WHERE pid=%s",
        (int(pid),))
    return raw if isinstance(raw, dict) else {}


def backend_absent_after_statement(db) -> dict:
    """Return pg_stat_activity proof after a connect-per-statement DB phase has closed."""
    pid = int(db("SELECT pg_backend_pid()"))
    return backend_transaction_state(pid)


def provision_install(owner_id: str) -> str:
    """Provision a live installation route for a GitHub owner id → returns the internal ACCT-GH-<owner> id."""
    acct = _run(DSN_APP, "SELECT core.enter_installation_with_authority(%s)", (owner_id,))
    return acct


def set_installation_generation(
        account_id: str, installation_id: str, created_at: str) -> None:
    _run(
        DSN_MIG,
        "UPDATE core.installation_account "
        "SET github_installation_id=%s,"
        "github_installation_created_at=%s::timestamptz "
        "WHERE account_id=%s",
        (installation_id, created_at, account_id),
    )


def revoke_install(owner_id: str) -> None:
    _run(DSN_MIG, "UPDATE core.installation_account SET revoked_at=now() WHERE installation_id=%s", (owner_id,))


def set_policy_as(owner_id: str, key: str, value: str, *, rollback: bool = False) -> None:
    """Write a policy AS the tenant (App with the install pinned). rollback=True aborts the txn to prove the
    same-txn enqueue rolls back with it."""
    conn = psycopg2.connect(DSN_APP)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_existing_installation_with_authority(%s)", (owner_id,))
            cur.execute("SELECT core.set_policy_with_authority(%s,%s)", (key, value))
        if rollback:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()


def _read_pinned(pin: str, sql: str, args=(), *, fetch=False):
    """Read as the owner with core.current_account pinned to `pin` (FORCE-RLS admits only that account's rows).
    The pin and the query are SEPARATE execute() calls on one connection (no multi-statement fragility)."""
    conn = psycopg2.connect(DSN_MIG)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account',%s,false)", (pin,))
            cur.execute(sql, args)
            if fetch:
                return cur.fetchall()
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def outbox(account_id: str) -> dict | None:
    """Read the outbox row for account_id as the owner WITH the account pinned (FORCE-RLS admits it)."""
    rows = _read_pinned(account_id,
                        "SELECT account_id, policy_epoch, claimed_at IS NOT NULL, done_at IS NOT NULL, "
                        "attempts, last_error, policy_cursor_repo, policy_cursor_branch, change_cursor "
                        "FROM core.policy_refresh_outbox WHERE account_id=%s AND request_kind='policy'",
                        (account_id,), fetch=True)
    if not rows:
        return None
    r = rows[0]
    return {
        "account_id": r[0], "epoch": r[1], "claimed": r[2], "done": r[3],
        "attempts": r[4], "last_error": r[5],
        "cursor_repo": r[6], "cursor_branch": r[7], "change_cursor": r[8],
    }


def graph_outbox(account_id: str, repository_id: str) -> dict | None:
    rows = _read_pinned(
        account_id,
        "SELECT account_id,policy_epoch,claimed_at IS NOT NULL,done_at IS NOT NULL,"
        "attempts,last_error,repo,branch,target_sha,repository_id,"
        "terminal_reason,not_before,change_cursor,onboarding_pending,"
        "onboarding_head_pending,onboarding_plan,onboarding_index,"
        "onboarding_truncated,onboarding_watching_done "
        "FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id=%s",
        (account_id, repository_id), fetch=True)
    if not rows:
        return None
    r = rows[0]
    return {
        "account_id": r[0], "epoch": r[1], "claimed": r[2], "done": r[3],
        "attempts": r[4], "last_error": r[5], "repo": r[6], "branch": r[7],
        "target_sha": r[8], "repository_id": r[9],
        "terminal_reason": r[10], "not_before": r[11],
        "change_cursor": r[12], "onboarding_pending": r[13],
        "onboarding_head_pending": r[14], "onboarding_plan": r[15],
        "onboarding_index": r[16], "onboarding_truncated": r[17],
        "onboarding_watching_done": r[18],
    }


def enqueue_graph(owner_id: str, repo: str, branch: str, sha: str, repository_id: str) -> int:
    conn = psycopg2.connect(DSN_APP)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_existing_installation_with_authority(%s)", (owner_id,))
            cur.execute(
                "SELECT core.enqueue_graph_refresh_with_authority(%s,%s,%s,%s)",
                (repo, branch, sha, repository_id))
            epoch = cur.fetchone()[0]
        conn.commit()
        return int(epoch)
    finally:
        conn.close()


def enqueue_onboarding(owner_id: str, repo: str, repository_id: str) -> int:
    conn = psycopg2.connect(DSN_APP)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_existing_installation_with_authority(%s)", (owner_id,))
            cur.execute(
                "SELECT core.enqueue_repository_onboarding_with_authority(%s,%s)",
                (repo, repository_id))
            epoch = cur.fetchone()[0]
        conn.commit()
        return int(epoch)
    finally:
        conn.close()


def activate_repository(account_id: str, repository_id: str, repo: str) -> None:
    _run(
        DSN_MIG,
        "SELECT set_config('core.current_account',%s,true); "
        "INSERT INTO core.repository_lifecycle_activation("
        "account_id,repository_id,repo,lifecycle_authoritative,generation_started_at) "
        "VALUES (%s,%s,%s,true,clock_timestamp())",
        (account_id, account_id, repository_id, repo),
    )


def persist_graph(owner_id: str, repo: str, branch: str, sha: str, repository_id: str) -> None:
    """Persist one exact current-generation graph + stable repository identity through the real App gates."""
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
    conn = psycopg2.connect(DSN_APP)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_existing_installation_with_authority(%s)", (owner_id,))
            cur.execute(
                "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                (json.dumps(graph), repo, branch, sha))
            cur.execute(
                "SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
                (repo, repository_id))
        conn.commit()
    finally:
        conn.close()


def claim_graph(worker: str = "graph-wk", max_attempts: int = 5, stale: int = 900) -> dict | None:
    return PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.claim_policy_refresh_with_authority(%s,%s,%s,8,true,1,true)",
        (worker, max_attempts, stale)))


def request_epoch(row: dict) -> int:
    return int(row.get("request_epoch", row["policy_epoch"]))


def graph_exact_args(row: dict) -> tuple:
    return (
        row["account_id"], row["request_kind"], row["repository_id"], row["branch"],
        request_epoch(row), int(row["graph_slot"]), int(row["lease_epoch"]),
    )


def finish_exact(row: dict) -> dict:
    if row.get("request_kind") == "graph" and row.get("graph_slot") is not None:
        sql = (
            "SELECT core.finish_policy_refresh_turn_with_authority("
            "%s,%s,%s,%s,%s,%s::smallint,%s)")
        args = graph_exact_args(row)
    else:
        sql = "SELECT core.finish_policy_refresh_turn_with_authority(%s,%s,%s,%s,%s)"
        args = (
            row["account_id"], row["request_kind"], row["repository_id"], row["branch"],
            request_epoch(row),
        )
    return PR._as_jsonb(_run(DSN_APP, sql, args)) or {}


def fail_exact(row: dict, error: str = "test_failure", retry_seconds: int = 1) -> int:
    if row.get("request_kind") == "graph" and row.get("graph_slot") is not None:
        sql = (
            "SELECT core.fail_policy_refresh_turn_with_authority("
            "%s,%s,%s,%s,%s,%s::smallint,%s,%s,%s)")
        args = (*graph_exact_args(row), error, retry_seconds)
    else:
        sql = (
            "SELECT core.fail_policy_refresh_turn_with_authority("
            "%s,%s,%s,%s,%s,%s,%s)")
        args = (
            row["account_id"], row["request_kind"], row["repository_id"], row["branch"],
            request_epoch(row), error, retry_seconds,
        )
    return int(_run(DSN_APP, sql, args))


def set_enqueued_at(account_id: str, kind: str, repository_id: str, branch: str, age_seconds: int) -> None:
    conn = psycopg2.connect(DSN_MIG)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account',%s,true)", (account_id,))
            cur.execute("SELECT core.mark_governed_write('policy_refresh_outbox')")
            cur.execute(
                "UPDATE core.policy_refresh_outbox "
                "SET enqueued_at=now()-make_interval(secs=>%s),claimed_at=NULL,claimed_by=NULL "
                "WHERE account_id=%s AND request_kind=%s AND repository_id=%s AND branch=%s",
                (age_seconds, account_id, kind, repository_id, branch))
            cur.execute(
                "SELECT core._sync_account_convergence_due(%s,2147483647,1,false)",
                (account_id,))
        conn.commit()
    finally:
        conn.close()


def age_claim(account_id: str, seconds: int = 100000) -> None:
    """Simulate either stale-lease or explicit retry-delay expiry.

    Current fail() releases the account route immediately and stores retry time in ``not_before``; rolling rows
    may still carry the historical claimed-at backoff. Age whichever representation is present without turning
    an unclaimed retry row into a synthetic stale lease."""
    conn = psycopg2.connect(DSN_MIG)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account',%s,true)", (account_id,))
            cur.execute("SELECT core.mark_governed_write('policy_refresh_outbox')")
            cur.execute(
                "UPDATE core.policy_refresh_outbox SET "
                "claimed_at=CASE WHEN claimed_at IS NULL THEN NULL "
                "  ELSE now()-make_interval(secs=>%s) END,"
                "not_before=CASE WHEN not_before IS NULL THEN NULL "
                "  ELSE now()-interval '1 second' END "
                "WHERE account_id=%s",
                (seconds, account_id))
            cur.execute(
                "UPDATE core.installation_account "
                "SET convergence_claimed_until=now()-interval '1 second' "
                "WHERE account_id=%s", (account_id,))
            cur.execute(
                "SELECT core._sync_account_convergence_due(%s,2147483647,1,false)",
                (account_id,))
        conn.commit()
    finally:
        conn.close()


def clear_outbox() -> None:
    """Delete mutable queue fixtures tenant-by-tenant. The graph-capable queue deliberately refuses to finish an
    unfulfilled graph row, so cleanup must not pretend fulfillment merely to get a clean test field."""
    accounts = _run(
        DSN_MIG,
        "SELECT account_id FROM core.installation_account ORDER BY account_id",
        fetch=True) or []
    for (account_id,) in accounts:
        conn = psycopg2.connect(DSN_MIG)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.current_account',%s,true)", (account_id,))
                cur.execute(
                    "DELETE FROM core.graph_convergence_lease WHERE account_id=%s",
                    (account_id,))
                cur.execute("SELECT core.mark_governed_write('policy_refresh_outbox')")
                cur.execute("DELETE FROM core.policy_refresh_outbox WHERE account_id=%s", (account_id,))
                cur.execute(
                    "UPDATE core.installation_account SET "
                    "policy_refresh_due_at=NULL,graph_refresh_due_at=NULL,"
                    "convergence_claimed_until=NULL,convergence_claimed_by=NULL,"
                    "convergence_claim_epoch=NULL,convergence_pending_count=0,"
                    "convergence_retry_exhausted_count=0,convergence_quota_deferred_count=0,"
                    "convergence_graph_claim_count=0,convergence_graph_reclaim_at=NULL "
                    "WHERE account_id=%s", (account_id,))
            conn.commit()
        finally:
            conn.close()


def outbox_count_for(account_id: str) -> int:
    """Rows for a SPECIFIC account (pinned, RLS-clean). Used for the no-op assertions (an account that should
    have NO outbox row)."""
    return int(_read_pinned(account_id,
                            "SELECT count(*) FROM core.policy_refresh_outbox WHERE account_id=%s",
                            (account_id,)) or 0)


def seed_claim(account_id: str, repo: str, branch: str, path: str) -> None:
    """Seed one ACTIVE in-flight claim for (account, repo) so the drainer's coordinate enumerator finds it.
    Written as the owner with the account pinned + the governed-write token armed (the same forgery-gated shape
    the real gate uses)."""
    conn = psycopg2.connect(DSN_MIG)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account',%s,true)", (account_id,))
            cur.execute("SELECT core.mark_governed_write('claim')")
            cur.execute(
                "INSERT INTO core.claim(claim_id, account_id, agent_id, change_id, repo, branch, target_path, claim_state) "
                "VALUES (%s,%s,'GH-tester',%s,%s,%s,%s,'active') "
                "ON CONFLICT DO NOTHING",
                (f"PR-1:{path}", account_id, "PR-1", repo, branch, path))
        conn.commit()
    finally:
        conn.close()


# ── SPY doubles for the drainer layer ────────────────────────────────────────────────────────────────────
class CountingStore(PR.PolicyRefreshStore):
    """A store that counts how many outbox connections are OPEN at any moment (each _one connects+closes in its
    own body, so `active` is >0 ONLY during a claim/finish/fail call). The seam assertion: during a GitHub post
    `active` must be 0 — no outbox transaction is held across the refresh."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.active = 0
        self.max_active = 0

    def _one(self, sql, args=()):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            return super()._one(sql, args)
        finally:
            self.active -= 1


class SpyGH:
    def __init__(self, dead_accounts=(), reachable=True, complete=True):
        self.dead = set(dead_accounts)
        self._reachable = reachable        # what app_installations_reachability reports (True=reachable map)
        self._complete = complete
        self.for_account_calls = []
        self.for_installation_calls = []
        self.checks = []
        self.comments = []

    def for_account(self, account_id):
        self.for_account_calls.append(account_id)
        if account_id in self.dead:
            return None                    # uninstalled/transferred/regenerated → the generation fence trips
        return self

    def for_installation(self, installation_id):
        self.for_installation_calls.append(str(installation_id))
        return self

    def upsert_check(self, repo, sha, conclusion, title, summary, **kwargs):
        self.checks.append({
            "repo": repo, "sha": sha, "conclusion": conclusion,
            "title": title, "summary": summary})
        return {"id": len(self.checks)}

    def pull_request_head(self, repo, pr):
        return ("%040x" % int(pr))[-40:]

    def upsert_comment(self, repo, pr, marker, body):
        self.comments.append({"repo": repo, "pr": pr, "marker": marker, "body": body})
        return {"id": len(self.comments)}

    def app_installations_reachability(self):
        # reachable=True → a genuinely-reachable map (a for_account None means a REAL dead install → skip);
        # reachable=False → the GET /app/installations map is UNREACHABLE (a transient 403 storm → retry).
        # complete=False means a cap-saturated partial map: its misses are Unknown, never absence authority.
        return {"reachable": self._reachable, "complete": self._complete}


def make_spies(store, *, post_raises=False):
    refreshed_repos: list[str] = []
    posted_repos: list[str] = []
    seam_ok = {"value": True}

    def spy_refresh_inflight(db, repo, branch="main", *a, **k):
        refreshed_repos.append(repo)
        # one entry so post_refreshes is exercised
        return {"repo": repo, "branch": branch, "refreshed": [{"change": "PR-1", "conclusion": "neutral"}]}

    def spy_post_refreshes(gh, repo, entries, db=None, branch="main", trace_id="", delivery="", **k):
        # SEAM: no outbox transaction may be open while we (would) call GitHub.
        if store.active != 0:
            seam_ok["value"] = False
        if post_raises:
            raise RuntimeError("simulated GitHub outage")
        posted_repos.append(repo)
        return len(entries)

    return spy_refresh_inflight, spy_post_refreshes, refreshed_repos, posted_repos, seam_ok


# ── SQL-LAYER TESTS ──────────────────────────────────────────────────────────────────────────────────────
def sql_enqueue_coalesce_and_rollback():
    acct = provision_install("100")
    set_policy_as("100", "split_min_fanin", "7")
    row = outbox(acct)
    chk(row is not None and row["epoch"] == 1 and not row["claimed"] and not row["done"],
        "a policy write enqueues exactly one pending outbox row (epoch=1)")

    set_policy_as("100", "split_min_churn", "4")
    set_policy_as("100", "split_window_days", "14")
    row = outbox(acct)
    n = int(_run(DSN_MIG,
                 "SELECT set_config('core.current_account',%s,true); "
                 "SELECT count(*) FROM core.policy_refresh_outbox WHERE account_id=%s",
                 (acct, acct)) or 0)
    chk(row is not None and row["epoch"] == 3 and n == 1,
        "rapid repeated writes COALESCE to ONE row; epoch climbs (3) — no storm")

    # NEGATIVE CONTROL: a rolled-back policy write leaves NO trace (same-txn enqueue rolls back too).
    before = outbox(acct)["epoch"]
    set_policy_as("100", "rollback_probe", "999", rollback=True)
    after = outbox(acct)["epoch"]
    chk(before == after == 3,
        "ROLLBACK-safety (negative control): a rolled-back policy write does NOT enqueue / bump the outbox")


def sql_tenant_isolation_rls():
    acct = provision_install("101")
    set_policy_as("101", "k", "v")
    # pinned to a DIFFERENT account, the FORCE-RLS wall hides account 101's row.
    visible_other = int(_run(DSN_MIG,
                             "SELECT set_config('core.current_account','ACCT-GH-999999',false); "
                             "SELECT count(*) FROM core.policy_refresh_outbox WHERE account_id=%s",
                             (acct,)) or 0)
    visible_self = int(_run(DSN_MIG,
                            "SELECT set_config('core.current_account',%s,false); "
                            "SELECT count(*) FROM core.policy_refresh_outbox WHERE account_id=%s",
                            (acct, acct)) or 0)
    chk(visible_other == 0 and visible_self == 1,
        "FORCE-RLS tenant isolation (negative control): another tenant's pin sees 0 of this account's rows")


def sql_claim_finish_and_cas():
    clear_outbox()
    acct = provision_install("102")
    set_policy_as("102", "k", "v")             # epoch 1
    claimed = PR._as_jsonb(_run(DSN_APP,
                                "SELECT core.claim_policy_refresh_with_authority('wk',8,1800,10000)"))
    chk(isinstance(claimed, dict) and claimed.get("account_id") == acct
        and claimed.get("account_key") == "102" and claimed.get("policy_epoch") == 1,
        "claim returns the account id + routing key + epoch")

    # finish with a WRONG (superseded) epoch is a no-op (the CAS guard).
    bad = _run(DSN_APP, "SELECT core.finish_policy_refresh_with_authority(%s,%s)", (acct, 999))
    chk(bad is False and outbox(acct)["done"] is False,
        "finish with a stale/superseded epoch is a NO-OP (CAS) — never marks the newer request done")

    good = _run(DSN_APP, "SELECT core.finish_policy_refresh_with_authority(%s,%s)", (acct, 1))
    chk(good is True and outbox(acct)["done"] is True,
        "finish with the claimed epoch marks the row drained")

    # a fresh policy write RE-OPENS the coalesced row (done→pending, epoch bumps).
    set_policy_as("102", "k2", "v2")
    row = outbox(acct)
    chk(row["done"] is False and row["epoch"] == 2 and not row["claimed"],
        "a new policy write after a completed refresh RE-ARMS the same row (epoch bumps, pending again)")


def sql_fail_retry_giveup():
    clear_outbox()
    acct = provision_install("103")
    set_policy_as("103", "k", "v")             # epoch 1
    _run(DSN_APP, "SELECT core.claim_policy_refresh_with_authority('wk',3,1800,10000)")
    n1 = _run(DSN_APP, "SELECT core.fail_policy_refresh_with_authority(%s,%s,%s)", (acct, 1, "err_a"))
    row = outbox(acct)
    retry_delay = float(_read_pinned(
        acct,
        "SELECT extract(epoch FROM not_before-clock_timestamp()) "
        "FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='policy'",
        (acct,),
    ))
    # RETRY-DELAY: fail releases the scarce account route/row claim immediately, but not_before keeps the row
    # out of the scheduler until its bounded backoff expires.
    chk(n1 == 1 and not row["claimed"] and not row["done"] and row["last_error"] == "err_a"
        and 0 < retry_delay <= 30.5,
        "legacy fail ABI increments once and clamps its retry delay to <=30s before releasing the lease")
    not_yet = _run(DSN_APP, "SELECT core.claim_policy_refresh_with_authority('wk',3,1800,10000)")
    chk(not_yet is None,
        "a just-failed row is NOT re-claimable within the stale window (no same-tick budget burn)")

    # Caller max=3 is a retained-ABI input, not a tuning knob. Burn the schema-fixed remaining four attempts;
    # every counter, due probe, alert, and worker agrees on exactly five.
    for error in ("err_b", "err_c", "err_d", "err_e"):
        age_claim(acct)
        retry = PR._as_jsonb(_run(
            DSN_APP, "SELECT core.claim_policy_refresh_with_authority('wk',3,1800,10000)"))
        _run(
            DSN_APP,
            "SELECT core.fail_policy_refresh_with_authority(%s,%s,%s)",
            (acct, retry["policy_epoch"], error),
        )
    row = outbox(acct)
    slow_delay = float(_read_pinned(
        acct,
        "SELECT extract(epoch FROM not_before-clock_timestamp()) "
        "FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='policy'",
        (acct,),
    ))
    not_yet_slow = _run(
        DSN_APP,
        "SELECT core.claim_policy_refresh_with_authority('wk',3,1800,10000)")
    age_claim(acct)
    claimed_after = PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.claim_policy_refresh_with_authority('wk',3,1800,10000)"))
    sixth = _run(
        DSN_APP,
        "SELECT core.fail_policy_refresh_with_authority(%s,%s,%s)",
        (acct, claimed_after["policy_epoch"], "err_f"),
    )
    sixth_delay = float(_read_pinned(
        acct,
        "SELECT extract(epoch FROM not_before-clock_timestamp()) "
        "FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='policy'",
        (acct,),
    ))
    chk(row["attempts"] == 5 and 290 <= slow_delay <= 305
        and not_yet_slow is None
        and claimed_after.get("account_id") == acct
        and int(sixth) == 6 and 590 <= sixth_delay <= 605,
        "caller max=3 cannot drift fixed-five isolation; slow retry auto-rearms at 5m then backs off to 10m")

    # A new policy write still gives operator/user intent immediate priority (attempts reset, cooldown cleared).
    set_policy_as("103", "k2", "v2")
    reclaim = PR._as_jsonb(_run(DSN_APP, "SELECT core.claim_policy_refresh_with_authority('wk',3,1800,10000)"))
    chk(outbox(acct)["attempts"] == 0 and isinstance(reclaim, dict) and reclaim.get("account_id") == acct,
        "a new policy write RE-ARMS a given-up row (attempts reset, claimable again)")


def sql_generation_fence_and_owner_noop():
    # (a) enqueue then REVOKE the install (uninstall) → claim no longer enumerates it (fence).
    acct = provision_install("104")
    set_policy_as("104", "k", "v")
    revoke_install("104")
    claimed = _run(DSN_APP, "SELECT core.claim_policy_refresh_with_authority('wk',8,1800,10000)")
    # the ONLY live-install account with a pending row is none now (104 revoked); other test accounts are done.
    chk(claimed is None or PR._as_jsonb(claimed).get("account_id") != acct,
        "generation fence: an install revoked between enqueue and claim is NOT enumerated (never drained)")

    # (b) a write for an account with NO live installation does not enqueue (leak-free). Exercise the enqueue
    # helper directly for a non-live account (as owner) → returns NULL, writes nothing.
    r = _run(DSN_MIG, "SELECT core._enqueue_policy_refresh('ACCT-GH-NOLIVE')")
    chk(r is None and outbox("ACCT-GH-NOLIVE") is None,
        "enqueue for a non-live-installation account is a NO-OP (no un-drainable row is left behind)")

    # (c) the OWNER tuning setters write under the reserved owner account (no install) → enqueue no-ops.
    owner_acct = _run(DSN_MIG, "SELECT core._owner_account()")
    _run(DSN_APP, "SELECT core.set_plan_limit_with_authority('pro', 300)")
    _run(DSN_APP, "SELECT core.set_free_line_with_authority('free_max_seats', 5)")
    _run(DSN_APP, "SELECT core.set_plan_graph_units_limit_with_authority('pro', 90000)")
    _run(DSN_APP, "SELECT core.set_dev_exempt_accounts_with_authority('ACCT-DEMO')")
    chk(outbox_count_for(owner_acct) == 0,
        "all four owner tuning setters (under the owner account) enqueue NOTHING (deliberate no-op)")


def sql_graph_identity_legacy_abi_and_supersede():
    clear_outbox()
    acct = provision_install("300")
    sha1, sha2, sha3 = "a" * 40, "b" * 40, "c" * 40
    e1 = enqueue_graph("300", "old/name", "main", sha1, "9001")
    e2 = enqueue_graph("300", "new/name", "trunk", sha2, "9001")
    row = graph_outbox(acct, "9001")
    count = int(_read_pinned(
        acct,
        "SELECT count(*) FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id='9001'",
        (acct,)) or 0)
    chk(count == 1 and e2 > e1 and row["repo"] == "new/name"
        and row["branch"] == "trunk" and row["target_sha"] == sha2,
        "stable repository_id is the sole graph identity: rename/default-branch change updates ONE latest-wins row")

    app_only = bool(_run(
        DSN_MIG,
        "SELECT has_function_privilege('veripsa_app',"
        "'core.enqueue_graph_refresh_with_authority(text,text,text,text)','EXECUTE') "
        "AND NOT has_function_privilege('veripsa_writer',"
        "'core.enqueue_graph_refresh_with_authority(text,text,text,text)','EXECUTE')"))
    chk(app_only, "graph enqueue is App-only (writer tenants cannot forge scheduler facts)")

    old_claim = _run(
        DSN_APP, "SELECT core.claim_policy_refresh_with_authority('old-wk',5,900,5000)")
    transitional_claim = _run(
        DSN_APP,
        "SELECT core.claim_policy_refresh_with_authority('five-arg-wk',5,900,5000,true)")
    old_finish = _run(
        DSN_APP, "SELECT core.finish_policy_refresh_with_authority(%s,%s)", (acct, e2))
    old_fail = _run(
        DSN_APP, "SELECT core.fail_policy_refresh_with_authority(%s,%s,'legacy')", (acct, e2))
    chk(old_claim is None and transitional_claim is None
        and old_finish is False and old_fail == -1
        and graph_outbox(acct, "9001")["attempts"] == 0,
        "rolling 4arg and transitional 5arg workers cannot see or terminalize graph slot work")

    first = claim_graph("new-wk")
    chk(first and first["request_kind"] == "graph" and first["repository_id"] == "9001"
        and first["branch"] == "trunk" and first["target_sha"] == sha2,
        "graph-capable v2 claim returns the exact stable-id/branch/SHA/epoch fact")
    slot_two_rejected = False
    try:
        malformed = dict(first)
        malformed["graph_slot"] = 2
        PR.PolicyRefreshStore._graph_identity(malformed)
    except ValueError:
        slot_two_rejected = True
    chk(slot_two_rejected,
        "Python terminal dispatch rejects a slot-2 token instead of reviving predecessor capacity")
    e3 = enqueue_graph("300", "newest/name", "default", sha3, "9001")
    miss = finish_exact(first)
    immediate = claim_graph("new-wk-2")
    chk(e3 > first["policy_epoch"] and miss.get("superseded") is True
        and immediate and immediate["target_sha"] == sha3 and immediate["branch"] == "default",
        "superseded exact CAS preserves the newer HEAD, releases only its old route lease, and is immediately reclaimable")
    fail_exact(immediate)


def sql_same_owner_rename_supersedes_graph_queue_coordinate():
    clear_outbox()
    acct = provision_install("304")
    _run(
        DSN_MIG,
        "SELECT set_config('core.current_account',%s,true); "
        "INSERT INTO core.repository_lifecycle_activation("
        "account_id,repository_id,repo,lifecycle_authoritative,generation_started_at) "
        "VALUES (%s,'9401','rename/old',true,clock_timestamp())",
        (acct, acct),
    )
    enqueue_graph("304", "rename/old", "main", "9" * 40, "9401")
    old = claim_graph("rename-old")
    before = PR._as_jsonb(_read_pinned(
        acct,
        "SELECT jsonb_build_object("
        "'repository_id',a.repository_id,"
        "'generation_started_at',a.generation_started_at,"
        "'request_epoch',q.policy_epoch,"
        "'lease_epoch',l.lease_epoch,"
        "'lease_request_epoch',l.request_epoch,"
        "'lease_repo',l.repo) "
        "FROM core.repository_lifecycle_activation a "
        "JOIN core.policy_refresh_outbox q "
        " ON q.account_id=a.account_id AND q.repository_id=a.repository_id "
        " AND q.request_kind='graph' "
        "JOIN core.graph_convergence_lease l "
        " ON l.account_id=q.account_id AND l.repository_id=q.repository_id "
        "WHERE a.account_id=%s AND a.repository_id='9401'",
        (acct,),
    ))
    conn = psycopg2.connect(DSN_APP)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_existing_installation_with_authority('304')")
            cur.execute(
                "SELECT core.rename_repo_coordinate_with_authority("
                "'rename/old','rename/new')")
            renamed = PR._as_jsonb(cur.fetchone()[0])
    finally:
        conn.close()
    old_lease_remaining = int(_read_pinned(
        acct,
        "SELECT count(*) FROM core.graph_convergence_lease "
        "WHERE account_id=%s AND repository_id='9401' AND lease_epoch=%s",
        (acct, old["lease_epoch"]),
    ) or 0)
    late = finish_exact(old)
    row = graph_outbox(acct, "9401")
    replacement = claim_graph("rename-new")
    activation = PR._as_jsonb(_read_pinned(
        acct,
        "SELECT jsonb_build_object("
        "'repo',repo,'repository_id',repository_id,"
        "'generation_started_at',generation_started_at) "
        "FROM core.repository_lifecycle_activation "
        "WHERE account_id=%s AND repository_id='9401'",
        (acct,),
    ))
    chk(
        renamed.get("ok") is True
        and renamed.get("repointed", {}).get("policy_refresh_outbox") == 1
        and before.get("repository_id") == "9401"
        and before.get("lease_repo") == "rename/old"
        and before.get("lease_request_epoch") == old["request_epoch"]
        and old_lease_remaining == 0
        and late.get("lease_lost") is True
        and row["repo"] == "rename/new" and row["repository_id"] == "9401"
        and row["epoch"] > before.get("request_epoch", 0)
        and row["attempts"] == 0 and not row["done"]
        and activation.get("repo") == "rename/new"
        and activation.get("repository_id") == before.get("repository_id")
        and activation.get("generation_started_at") == before.get("generation_started_at")
        and replacement and replacement["repository_id"] == "9401"
        and replacement["repo"] == "rename/new"
        and replacement["request_epoch"] > old["request_epoch"]
        and replacement["lease_epoch"] > old["lease_epoch"],
        "same-owner rename preserves lifecycle generation + stable repository id, exact-fences the old lease, "
        "and requeues only a fresh request/lease generation under the new mutable coordinate",
    )
    if replacement:
        fail_exact(replacement, "rename_fixture_release")


def sql_live_legacy_graph_lease_is_immediately_superseded_by_onboarding():
    """An install signal racing an old /6 graph turn must stop before clone, not merely at its final write."""
    clear_outbox()
    acct = provision_install("305")
    set_installation_generation(acct, "1305", "2026-08-03T00:00:00Z")
    activate_repository(acct, "9402", "latch/old")
    ordinary_epoch = enqueue_graph("305", "latch/old", "main", "a" * 40, "9402")
    old = PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.claim_policy_refresh_with_authority("
        "'old-six',5,300,8,true,1)",
    ))
    new_epoch = enqueue_onboarding("305", "latch/old", "9402")
    latched = graph_outbox(acct, "9402")
    old_current = _run(
        DSN_APP,
        "SELECT core.policy_refresh_turn_is_current_with_authority("
        "%s,%s,%s,%s,%s,%s::smallint,%s)",
        graph_exact_args(old),
    )
    stale_finish = finish_exact(old)
    after_stale = graph_outbox(acct, "9402")
    old_six_cannot_claim = _run(
        DSN_APP,
        "SELECT core.claim_policy_refresh_with_authority("
        "'old-six-next',5,300,8,true,1)",
    )
    capable = claim_graph("onboarding-seven")
    chk(
        old and old["policy_epoch"] == ordinary_epoch
        and new_epoch > ordinary_epoch
        and latched["epoch"] == new_epoch
        and latched["branch"] == "__veripsa_onboarding_head__"
        and latched["target_sha"] == "0000000"
        and latched["onboarding_pending"] is True
        and latched["onboarding_head_pending"] is True
        and old_current is False,
        "live install latch replaces the outbox with a fresh HEAD epoch so an old /6 preflight stops before clone",
    )
    chk(
        stale_finish.get("superseded") is True
        and stale_finish.get("finished") is False
        and after_stale["epoch"] == new_epoch
        and after_stale["branch"] == "__veripsa_onboarding_head__"
        and after_stale["onboarding_head_pending"] is True
        and old_six_cannot_claim is None
        and capable and capable["request_epoch"] == new_epoch
        and capable["onboarding_head_pending"] is True,
        "stale /6 finish cannot consume the latch; only onboarding-capable /7 can claim the fresh HEAD phase",
    )
    if capable:
        fail_exact(capable, "fixture_release")


def sql_fresh_onboarding_rename_uses_lifecycle_identity_without_graph():
    """Canonical HEAD rename converges from the activation stable id before any graph_version exists."""
    clear_outbox()
    acct = provision_install("306")
    set_installation_generation(acct, "1306", "2026-08-03T00:00:00Z")
    old_epoch = enqueue_onboarding("306", "fresh/old", "9403")
    graph_before = int(_read_pinned(
        acct,
        "SELECT count(*) FROM core.graph_version WHERE account_id=%s AND repo_id='9403'",
        (acct,),
    ) or 0)

    def pinned_db(sql, params=None):
        conn = psycopg2.connect(DSN_APP)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.enter_existing_installation_with_authority('306')")
                cur.execute(sql, params or ())
                row = cur.fetchone() if cur.description else None
                return row[0] if row else None
        finally:
            conn.close()

    class CanonicalHead:
        @staticmethod
        def repo_onboarding_head_info(_repo):
            return {
                "repository_id": "9403", "owner_id": "306",
                "full_name": "fresh/new", "default_branch": "main",
                "head_sha": "b" * 40, "empty": False,
            }

    resolved = ingest.resolve_repository_onboarding_head(
        pinned_db, CanonicalHead(), "fresh/old", "9403", "306")
    activation_count = int(_read_pinned(
        acct,
        "SELECT count(*) FROM core.repository_lifecycle_activation "
        "WHERE account_id=%s AND repository_id='9403'",
        (acct,),
    ) or 0)
    queue = graph_outbox(acct, "9403")
    graph_after = int(_read_pinned(
        acct,
        "SELECT count(*) FROM core.graph_version WHERE account_id=%s AND repo_id='9403'",
        (acct,),
    ) or 0)
    claimed = claim_graph("fresh-rename-head")
    chk(
        graph_before == 0 and graph_after == 0
        and resolved.get("superseded") is True and resolved.get("renamed") is True
        and activation_count == 0
        and queue["repo"] == "fresh/new"
        and queue["epoch"] > old_epoch
        and queue["branch"] == "__veripsa_onboarding_head__"
        and queue["onboarding_head_pending"] is True
        and claimed and claimed["repo"] == "fresh/new"
        and claimed["request_epoch"] == queue["epoch"],
        "installation.created onboarding rename migrates the exact stable-id queue and supersedes HEAD with zero activation/graph rows",
    )
    if claimed:
        fail_exact(claimed, "fixture_release")


def _onboarding_phase_args(row: dict) -> tuple:
    return (
        row["account_id"], row["repository_id"], row["branch"], request_epoch(row),
        int(row["graph_slot"]), int(row["lease_epoch"]),
    )


def sql_onboarding_phase_cas_fences_installation_generation():
    """Remote G1 phase results cannot consume a duplicate G2 onboarding row with the same request epoch."""
    clear_outbox()
    acct = provision_install("307")
    g1_id, g1_at = "1307", "2026-08-03T00:00:01Z"
    g2_id, g2_at = "2307", "2026-08-03T00:00:02Z"
    set_installation_generation(acct, g1_id, g1_at)
    epoch = enqueue_onboarding("307", "generation/empty", "9404")
    g1 = claim_graph("generation-g1-empty")
    before = graph_outbox(acct, "9404")
    set_installation_generation(acct, g2_id, g2_at)
    duplicate_epoch = enqueue_onboarding("307", "generation/empty", "9404")
    refused = PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.resolve_graph_onboarding_head_with_authority("
        "%s,%s,%s,%s,%s::smallint,%s,%s,%s::timestamptz,%s,%s)",
        (*_onboarding_phase_args(g1), g1_id, g1_at, "main", None),
    )) or {}
    after = graph_outbox(acct, "9404")
    old_lease_count = int(_read_pinned(
        acct,
        "SELECT count(*) FROM core.graph_convergence_lease "
        "WHERE account_id=%s AND request_epoch=%s AND lease_epoch=%s",
        (acct, request_epoch(g1), int(g1["lease_epoch"])),
    ) or 0)
    g2 = claim_graph("generation-g2-empty")
    chk(
        epoch == duplicate_epoch == before["epoch"] == after["epoch"]
        and refused.get("superseded") is True
        and refused.get("installation_generation_changed") is True
        and after["onboarding_head_pending"] is True
        and after["branch"] == "__veripsa_onboarding_head__"
        and old_lease_count == 0
        and g2 and g2.get("github_installation_id") == g2_id,
        "G1 empty HEAD proof after reinstall preserves the duplicate G2 onboarding row, releases only G1's "
        "exact lease, and leaves it claimable through G2's direct generation",
    )
    if g2:
        # The generation-less predecessor ABI is intentionally no-progress during schema-first overlap.
        legacy = PR._as_jsonb(_run(
            DSN_APP,
            "SELECT core.resolve_graph_onboarding_head_with_authority("
            "%s,%s,%s,%s,%s::smallint,%s,%s,%s)",
            (*_onboarding_phase_args(g2), "main", None),
        )) or {}
        legacy_after = graph_outbox(acct, "9404")
        chk(
            legacy.get("superseded") is True
            and legacy.get("legacy_installation_generation_missing") is True
            and legacy_after["epoch"] == epoch
            and legacy_after["onboarding_head_pending"] is True,
            "legacy /8 HEAD ABI cannot infer G2 authority; it retains the outbox and releases only its lease",
        )

    clear_outbox()
    acct2 = provision_install("308")
    h1_id, h1_at = "1308", "2026-08-03T00:00:03Z"
    h2_id, h2_at = "2308", "2026-08-03T00:00:04Z"
    set_installation_generation(acct2, h1_id, h1_at)
    phase_epoch = enqueue_onboarding("308", "generation/phase", "9405")
    head_claim = claim_graph("generation-g1-head")
    head_done = PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.resolve_graph_onboarding_head_with_authority("
        "%s,%s,%s,%s,%s::smallint,%s,%s,%s::timestamptz,%s,%s)",
        (*_onboarding_phase_args(head_claim), h1_id, h1_at, "main", "a" * 40),
    )) or {}
    phase_claim = claim_graph("generation-g1-freeze")
    set_installation_generation(acct2, h2_id, h2_at)
    duplicate_phase_epoch = enqueue_onboarding("308", "generation/phase", "9405")
    phase_refused = PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.requeue_graph_onboarding_with_authority("
        "%s,%s,%s,%s,%s::smallint,%s,%s,%s::timestamptz,"
        "%s,%s::bigint[],%s,%s,%s,%s)",
        (*_onboarding_phase_args(phase_claim), h1_id, h1_at,
         "freeze", [11, 12], False, 0, 0, False),
    )) or {}
    phase_after = graph_outbox(acct2, "9405")
    h2_claim = claim_graph("generation-g2-freeze")
    freeze_ok = PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.requeue_graph_onboarding_with_authority("
        "%s,%s,%s,%s,%s::smallint,%s,%s,%s::timestamptz,"
        "%s,%s::bigint[],%s,%s,%s,%s)",
        (*_onboarding_phase_args(h2_claim), h2_id, h2_at,
         "freeze", [11, 12], False, 0, 0, False),
    )) or {}
    frozen = graph_outbox(acct2, "9405")
    chk(
        head_done.get("requeued") is True
        and duplicate_phase_epoch == phase_epoch
        and phase_refused.get("superseded") is True
        and phase_refused.get("installation_generation_changed") is True
        and phase_after["onboarding_plan"] is None and phase_after["onboarding_index"] == 0
        and h2_claim and h2_claim.get("github_installation_id") == h2_id
        and freeze_ok.get("requeued") is True
        and frozen["onboarding_plan"] == [11, 12] and frozen["onboarding_index"] == 0,
        "G1 phase transition after reinstall cannot freeze/advance/watch/complete G2 state; exact G2 /14 "
        "authority succeeds on the unchanged epoch",
    )
    frozen_claim = claim_graph("generation-frozen-release")
    if frozen_claim:
        fail_exact(frozen_claim, "fixture_release")


def _requeue_onboarding_sql(
        row: dict, installation_id: str, created_at: str, action: str, *,
        plan=None, truncated: bool = False, expected_index: int = 0,
        pr_number: int = 0, watching_done: bool = False) -> dict:
    return PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.requeue_graph_onboarding_with_authority("
        "%s,%s,%s,%s,%s::smallint,%s,%s,%s::timestamptz,"
        "%s,%s::bigint[],%s,%s,%s,%s)",
        (*_onboarding_phase_args(row), installation_id, created_at,
         action, plan, truncated, expected_index, pr_number, watching_done),
    )) or {}


def _resolve_onboarding_sql(
        row: dict, installation_id: str, created_at: str,
        branch: str, target_sha: str | None) -> dict:
    return PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.resolve_graph_onboarding_head_with_authority("
        "%s,%s,%s,%s,%s::smallint,%s,%s,%s::timestamptz,%s,%s)",
        (*_onboarding_phase_args(row), installation_id, created_at, branch, target_sha),
    )) or {}


def sql_onboarding_plan_preservation_refreeze_and_fairness():
    """Same-branch churn resumes one immutable cursor; scope change refreezes; phase turns stay fair."""
    clear_outbox()
    acct = provision_install("309")
    installation_id, created_at = "1309", "2026-08-03T00:00:05Z"
    set_installation_generation(acct, installation_id, created_at)
    enqueue_onboarding("309", "plan/repo", "9406")
    head = claim_graph("plan-head")
    _resolve_onboarding_sql(head, installation_id, created_at, "main", "a" * 40)
    freeze = claim_graph("plan-freeze")
    _requeue_onboarding_sql(
        freeze, installation_id, created_at, "freeze", plan=[1, 2])
    old_target_claim = claim_graph("plan-old-target")
    b_epoch = enqueue_graph("309", "plan/repo", "main", "b" * 40, "9406")
    same_branch = graph_outbox(acct, "9406")
    old_finish = finish_exact(old_target_claim)
    b_claim = claim_graph("plan-b-advance")
    _requeue_onboarding_sql(
        b_claim, installation_id, created_at, "advance",
        expected_index=0, pr_number=1)
    progressed = graph_outbox(acct, "9406")
    b_progress_claim = claim_graph("plan-b-progress")
    c_epoch = enqueue_graph("309", "plan/repo", "main", "c" * 40, "9406")
    same_branch_progress = graph_outbox(acct, "9406")
    finish_exact(b_progress_claim)
    trunk_epoch = enqueue_graph("309", "plan/repo", "trunk", "d" * 40, "9406")
    changed_scope = graph_outbox(acct, "9406")
    chk(
        b_epoch > request_epoch(old_target_claim)
        and same_branch["onboarding_plan"] == [1, 2]
        and same_branch["onboarding_index"] == 0
        and old_finish.get("superseded") is True
        and progressed["onboarding_index"] == 1
        and c_epoch > b_epoch
        and same_branch_progress["onboarding_plan"] == [1, 2]
        and same_branch_progress["onboarding_index"] == 1
        and trunk_epoch > c_epoch
        and changed_scope["branch"] == "trunk"
        and changed_scope["onboarding_plan"] is None
        and changed_scope["onboarding_index"] == 0,
        "same-branch HEAD churn preserves immutable plan/index across epochs, while a real default-branch "
        "scope change clears the old inventory for one new CAP+1 freeze",
    )

    clear_outbox()
    fair_acct_a = provision_install("310")
    fair_acct_b = provision_install("311")
    fair_id_a, fair_at_a = "1310", "2026-08-03T00:00:06Z"
    fair_id_b, fair_at_b = "1311", "2026-08-03T00:00:07Z"
    set_installation_generation(fair_acct_a, fair_id_a, fair_at_a)
    set_installation_generation(fair_acct_b, fair_id_b, fair_at_b)
    enqueue_onboarding("310", "fair/a", "9407")
    enqueue_onboarding("311", "fair/b", "9408")
    # Build two CROSS-ACCOUNT phase-ready fixtures through the exact public phase ABIs while allowing the global
    # fair scheduler itself to interleave A-head, B-head, A-freeze, B-freeze.  Keeping the accounts distinct is
    # Load-bearing: this models an account-wide convoy, not merely two repositories in one tenant.
    fair_specs = {
        "fair/a": (fair_acct_a, fair_id_a, fair_at_a, "e" * 40, [1, 2], "9407"),
        "fair/b": (fair_acct_b, fair_id_b, fair_at_b, "f" * 40, [9], "9408"),
    }
    frozen_repos = set()
    for setup_turn in range(8):
        if len(frozen_repos) == 2:
            break
        setup_claim = claim_graph(f"fair-setup-{setup_turn}")
        spec = fair_specs.get(setup_claim.get("repo")) if setup_claim else None
        if spec is None:
            raise AssertionError("unexpected onboarding setup coordinate")
        if setup_claim.get("onboarding_head_pending") is True:
            _resolve_onboarding_sql(
                setup_claim, spec[1], spec[2], "main", spec[3])
        elif setup_claim.get("onboarding_plan") is None:
            _requeue_onboarding_sql(
                setup_claim, spec[1], spec[2], "freeze", plan=spec[4])
            frozen_repos.add(setup_claim["repo"])
        else:
            raise AssertionError("frozen onboarding fixture was claimed before setup completed")
    if len(frozen_repos) != 2:
        raise AssertionError("onboarding fairness fixtures did not freeze")

    # Put A just ahead of B. Each successful exact advance releases its account slot and moves that account to
    # the scheduler tail.  Execute all three transitions (not merely claims) so the permanent test proves the
    # phase CAS and the cross-account ordering together: A[0]/PR#1 → B[0]/PR#9 → A[1]/PR#2.
    set_enqueued_at(fair_acct_a, "graph", "9407", "main", 20)
    set_enqueued_at(fair_acct_b, "graph", "9408", "main", 10)
    advances = []
    for label in ("fair-a1", "fair-b1", "fair-a2"):
        turn = claim_graph(label)
        spec = fair_specs.get(turn.get("repo")) if turn else None
        if spec is None:
            raise AssertionError("unexpected cross-account onboarding advance coordinate")
        index = int(turn.get("onboarding_index") or 0)
        plan = spec[4]
        if index >= len(plan):
            raise AssertionError("onboarding fairness cursor advanced past its frozen plan")
        transition = _requeue_onboarding_sql(
            turn, spec[1], spec[2], "advance",
            expected_index=index, pr_number=plan[index])
        advances.append((turn.get("account_id"), turn.get("repo"), plan[index], transition))
    chk(
        [(account, repo, number) for account, repo, number, _transition in advances]
        == [
            (fair_acct_a, "fair/a", 1),
            (fair_acct_b, "fair/b", 9),
            (fair_acct_a, "fair/a", 2),
        ]
        and all(transition.get("requeued") is True for *_prefix, transition in advances)
        and graph_outbox(fair_acct_a, "9407")["onboarding_index"] == 2
        and graph_outbox(fair_acct_b, "9408")["onboarding_index"] == 1,
        "phase-ready cross-account one-PR turns rotate fairly A1 → B1 → A2 (PR #1 → #9 → #2) "
        "instead of letting one tenant convoy the worker",
    )


def sql_rolling_pre_schema_policy_terminal_bridge():
    clear_outbox()
    acct = provision_install("301")
    set_policy_as("301", "rolling", "finish")
    old = PR._as_jsonb(_run(
        DSN_APP, "SELECT core.claim_policy_refresh_with_authority('old-image',5,900,5000)"))
    graph_epoch = enqueue_graph("301", "roll/repo", "main", "d" * 40, "9101")
    # Simulate a claim committed immediately BEFORE the scheduler lease columns were published.
    _run(
        DSN_MIG,
        "UPDATE core.installation_account SET convergence_claimed_until=NULL,"
        "convergence_claimed_by=NULL,convergence_claim_epoch=NULL WHERE account_id=%s",
        (acct,))
    finished = _run(
        DSN_APP, "SELECT core.finish_policy_refresh_with_authority(%s,%s)",
        (acct, old["policy_epoch"]))
    graph = graph_outbox(acct, "9101")
    claimed_graph = claim_graph("new-image")
    legacy_graph_finish = _run(
        DSN_APP, "SELECT core.finish_policy_refresh_with_authority(%s,%s)",
        (acct, graph_epoch))
    chk(finished is True and graph and not graph["done"] and graph["attempts"] == 0
        and claimed_graph and claimed_graph["repository_id"] == "9101"
        and legacy_graph_finish is False and not graph_outbox(acct, "9101")["done"],
        "pre-schema old policy claim materializes only its exact sentinel lease; graph work remains unreachable")
    fail_exact(claimed_graph)

    acct2 = provision_install("302")
    set_policy_as("302", "rolling", "fail")
    old2 = PR._as_jsonb(_run(
        DSN_APP, "SELECT core.claim_policy_refresh_with_authority('old-image',5,900,5000)"))
    _run(
        DSN_MIG,
        "UPDATE core.installation_account SET convergence_claimed_until=NULL,"
        "convergence_claimed_by=NULL,convergence_claim_epoch=NULL WHERE account_id=%s",
        (acct2,))
    failed = _run(
        DSN_APP, "SELECT core.fail_policy_refresh_with_authority(%s,%s,'old_failure')",
        (acct2, old2["policy_epoch"]))
    chk(failed == 1 and outbox(acct2)["attempts"] == 1 and outbox(acct2)["last_error"] == "old_failure",
        "pre-schema old 3arg fail materializes and fails only its exact policy sentinel epoch")


def sql_expired_policy_worker_is_fenced_and_reclaimed():
    clear_outbox()
    acct = provision_install("303")
    set_policy_as("303", "stale", "fence")
    old = PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.claim_policy_refresh_with_authority('rollback-old',1,1800,5000)",
    ))
    lease_seconds = float(_run(
        DSN_MIG,
        "SELECT extract(epoch FROM convergence_claimed_until-clock_timestamp()) "
        "FROM core.installation_account WHERE account_id=%s",
        (acct,),
    ))
    chk(0 < lease_seconds <= 300.5,
        "retained 4arg caller max=1/stale=1800 is DB-clamped to the fixed-five, 300s lease contract")
    _run(
        DSN_MIG,
        "SELECT set_config('core.current_account',%s,true); "
        "INSERT INTO core.repository_lifecycle_tombstone("
        "account_id,repository_id,repo,reason) "
        "VALUES (%s,'999304','unrelated/tombstone','repository_deleted')",
        (acct, acct),
    )
    tombstone_due_horizon = float(_read_pinned(
        acct,
        "SELECT extract(epoch FROM r.policy_refresh_due_at-q.claimed_at) "
        "FROM core.installation_account r "
        "JOIN core.policy_refresh_outbox q ON q.account_id=r.account_id "
        "WHERE r.account_id=%s AND q.request_kind='policy'",
        (acct,),
    ))
    chk(0 < tombstone_due_horizon <= 300.5,
        "unrelated repository tombstone cannot republish an active policy due horizon beyond 300s")

    conn = psycopg2.connect(DSN_MIG)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account',%s,true)", (acct,))
            cur.execute("SELECT core.mark_governed_write('policy_refresh_outbox')")
            cur.execute(
                "UPDATE core.policy_refresh_outbox SET attempts=2,"
                "policy_cursor_repo='cursor/repo',policy_cursor_branch='main',"
                "change_cursor='PR-30',claimed_at=clock_timestamp()-interval '301 seconds' "
                "WHERE account_id=%s AND request_kind='policy' AND repository_id=''",
                (acct,),
            )
            cur.execute(
                "UPDATE core.installation_account "
                "SET convergence_claimed_until=clock_timestamp()-interval '1 second' "
                "WHERE account_id=%s",
                (acct,),
            )
            cur.execute(
                "SELECT core._sync_account_convergence_due(%s,1,300,false)",
                (acct,),
            )
    finally:
        conn.close()

    current = _run(
        DSN_APP,
        "SELECT core.policy_refresh_turn_is_current_with_authority(%s,'policy','','',%s)",
        (acct, old["policy_epoch"]),
    )
    finished = PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.finish_policy_refresh_turn_with_authority(%s,'policy','','',%s)",
        (acct, old["policy_epoch"]),
    ))
    failed = _run(
        DSN_APP,
        "SELECT core.fail_policy_refresh_turn_with_authority("
        "%s,'policy','','',%s,'expired',20)",
        (acct, old["policy_epoch"]),
    )
    paged = PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.requeue_policy_refresh_page_with_authority("
        "%s,%s,'cursor/repo','main','PR-60',false)",
        (acct, old["policy_epoch"]),
    ))
    # Simulate the pre-router compatibility shape after expiry. The legacy helper must not mint now()+1s.
    _run(
        DSN_MIG,
        "UPDATE core.installation_account SET convergence_claimed_until=NULL,"
        "convergence_claimed_by=NULL,convergence_claim_epoch=NULL WHERE account_id=%s",
        (acct,),
    )
    legacy_finish = _run(
        DSN_APP,
        "SELECT core.finish_policy_refresh_with_authority(%s,%s)",
        (acct, old["policy_epoch"]),
    )
    legacy_fail = _run(
        DSN_APP,
        "SELECT core.fail_policy_refresh_with_authority(%s,%s,'legacy_expired')",
        (acct, old["policy_epoch"]),
    )
    untouched = outbox(acct)
    replacement = PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.claim_policy_refresh_with_authority('replacement',1,1800,5000)",
    ))
    replacement_lease = float(_run(
        DSN_MIG,
        "SELECT extract(epoch FROM convergence_claimed_until-clock_timestamp()) "
        "FROM core.installation_account WHERE account_id=%s",
        (acct,),
    ))
    chk(
        current is False
        and finished.get("lease_lost") is True
        and failed == -1
        and paged.get("lease_lost") is True
        and legacy_finish is False and legacy_fail == -1
        and untouched["attempts"] == 2
        and untouched["cursor_repo"] == "cursor/repo"
        and untouched["cursor_branch"] == "main"
        and untouched["change_cursor"] == "PR-30",
        "expired policy authority cannot current/finish/fail/page or resurrect through the legacy materializer",
    )
    chk(
        replacement
        and replacement["request_kind"] == "policy"
        and replacement["policy_epoch"] > old["policy_epoch"]
        and replacement["attempts"] == 2
        and replacement["policy_cursor_repo"] == "cursor/repo"
        and replacement["policy_cursor_branch"] == "main"
        and replacement["change_cursor"] == "PR-30"
        and 0 < replacement_lease <= 300.5,
        "stale policy is reclaimed under a fresh epoch while attempts/cursors survive and lease stays <=300s",
    )
    fail_exact(replacement, "stale_fixture_release")


def sql_late_account_capacity_fence_and_tail():
    clear_outbox()
    a = provision_install("399")
    enqueue_graph("399", "fair/a1", "main", "1" * 40, "9201")
    enqueue_graph("399", "fair/a2", "main", "2" * 40, "9202")
    set_enqueued_at(a, "graph", "9201", "main", 400)
    set_enqueued_at(a, "graph", "9202", "main", 350)

    first = claim_graph("fair-1")
    same_account_second = claim_graph("fair-same-account")
    chk(first and first["account_id"] == a and first["repository_id"] == "9201",
        "global scheduler selects the account's true oldest due repository")
    chk(same_account_second is None,
        "A2 cannot claim while A1 owns the account's exact-one graph slot")

    # B is deliberately provisioned only after A's second claim was refused. Worker two must still be available.
    b = provision_install("310")
    enqueue_graph("310", "fair/b", "main", "3" * 40, "9210")
    late_b = claim_graph("fair-late-b")
    chk(late_b and late_b["account_id"] == b and late_b["repository_id"] == "9210",
        "a later-arriving account claims immediately while A1 remains active")

    persist_graph("399", "fair/a1", "main", "1" * 40, "9201")
    first_done = finish_exact(first)
    a_tail = claim_graph("fair-a2-after-finish")
    persist_graph("399", "fair/a2", "main", "2" * 40, "9202")
    second_done = finish_exact(a_tail) if a_tail else {}
    chk(first_done.get("finished") is True
        and a_tail and a_tail["account_id"] == a and a_tail["repository_id"] == "9202"
        and second_done.get("finished") is True,
        "finishing A1 releases the sole slot and only then makes A2 claimable")
    if late_b:
        fail_exact(late_b)


# ── DRAINER-LAYER TESTS ──────────────────────────────────────────────────────────────────────────────────
def drain_tenant_scoped_and_seam():
    clear_outbox()
    a = provision_install("200")               # writing tenant
    b = provision_install("201")               # bystander tenant (open PR, but NO policy change)
    set_policy_as("200", "k", "v")             # enqueue for A ONLY
    seed_claim(a, "acme/a", "main", "svc/a.py")
    seed_claim(b, "acme/b", "main", "svc/b.py")

    store = CountingStore(DSN_APP, max_attempts=5, stale_seconds=900, scan_cap=10000)
    gh = SpyGH()
    ri, pr_post, refreshed, posted, seam = make_spies(store)
    res = PR._drain_policy_refreshes(store, gh, DSN_APP, limit=50,
                                     refresh_inflight=ri, post_refreshes=pr_post)

    chk("acme/a" in refreshed and "acme/b" not in refreshed,
        "the drainer refreshes ONLY the writing tenant's repo (negative control: the bystander tenant is untouched)")
    chk("acme/a" in posted and "acme/b" not in posted and res.get("drained") == 1,
        "only the writing tenant's open PR is re-posted; the bystander's is not")
    chk(seam["value"] and store.max_active >= 1,
        "no GitHub call happens while an outbox transaction is open (seam: store had 0 open conns at post time)")
    chk(outbox(a)["done"] is True,
        "the drained tenant's outbox row is marked done")


def drain_remote_absence_waits_for_authoritative_offboard():
    clear_outbox()
    a = provision_install("202")
    set_policy_as("202", "k", "v")
    seed_claim(a, "acme/c", "main", "svc/c.py")

    store = CountingStore(DSN_APP)
    gh = SpyGH(dead_accounts={a})              # gh.for_account(a) → None (uninstalled between enqueue and drain)
    ri, pr_post, refreshed, posted, seam = make_spies(store)
    PR._drain_policy_refreshes(store, gh, DSN_APP, limit=50, refresh_inflight=ri, post_refreshes=pr_post)

    chk("acme/c" not in posted and "acme/c" not in refreshed,
        "generation fence (drainer): gh.for_account None → NO post to a dead install")
    row = outbox(a)
    chk(
        row["done"] is False
        and row["attempts"] == 1
        and row["last_error"] == "installation_absence_unconfirmed",
        "remote list absence cannot consume a still-live DB route; "
        "the row retries until authoritative offboarding removes it",
    )


def drain_exact_installation_generation_routes_without_fleet_scan():
    clear_outbox()
    account = provision_install("208")
    installation_id = "9000208"
    created_at = "2026-07-29T00:00:08+00:00"
    set_installation_generation(account, installation_id, created_at)
    set_policy_as("208", "direct", "yes")
    seed_claim(account, "acme/direct", "main", "svc/direct.py")

    store = CountingStore(DSN_APP)
    gh = SpyGH()
    ri, pr_post, refreshed, posted, _seam = make_spies(store)
    result = PR._drain_policy_refreshes(
        store,
        gh,
        DSN_APP,
        limit=50,
        refresh_inflight=ri,
        post_refreshes=pr_post,
    )
    chk(
        gh.for_installation_calls == [installation_id]
        and gh.for_account_calls == []
        and "acme/direct" in posted
        and outbox(account)["done"] is True
        and result.get("drained") == 1,
        "an exact claim resolves its durable installation id directly; "
        "the capped fleet installation map is never scanned",
    )


def drain_changed_installation_generation_has_zero_external_calls():
    clear_outbox()
    account = provision_install("209")
    set_installation_generation(
        account, "9000209", "2026-07-29T00:00:09+00:00")
    set_policy_as("209", "generation", "a")
    seed_claim(account, "acme/generation", "main", "svc/generation.py")

    class ChangeAfterClaimStore(CountingStore):
        def claim(self):
            claimed = super().claim()
            if isinstance(claimed, dict) and claimed.get("account_id"):
                set_installation_generation(
                    account,
                    "9001209",
                    "2026-07-29T00:01:09+00:00",
                )
            return claimed

    store = ChangeAfterClaimStore(DSN_APP)
    gh = SpyGH()
    ri, pr_post, refreshed, posted, _seam = make_spies(store)
    result = PR._drain_policy_refreshes(
        store,
        gh,
        DSN_APP,
        limit=1,
        refresh_inflight=ri,
        post_refreshes=pr_post,
    )
    row = outbox(account)
    chk(
        gh.for_installation_calls == []
        and gh.for_account_calls == []
        and gh.checks == []
        and gh.comments == []
        and refreshed == []
        and posted == []
        and result.get("failed") == 1
        and row["done"] is False
        and row["attempts"] == 1
        and row["last_error"] == "installation_generation_changed",
        "claim generation A loses authority after route generation B; "
        "zero GitHub reads or mutations occur and the row retries",
    )


def drain_threads_graph_degraded_into_post():
    # FALSE-CLEAR PARITY (policy path): the drainer must thread a graph-degraded signal into _post_refreshes so a
    # policy-triggered clear_reset is WITHHELD under a stale-behind-HEAD graph — the acting-PR and push paths
    # already do this. Only explicit current proof may clear; Unknown/malformed/read failure all fail closed.
    cases = [
        ("230", {"behind": True},  True,  "behind HEAD → graph_degraded=True"),
        ("231", {"behind": False}, False, "current → graph_degraded=False"),
        ("232", "RAISE",           True,  "freshness read error → graph_degraded=True (fail-closed)"),
        ("233", {"behind": None},  True,  "unknown currency → graph_degraded=True (fail-closed)"),
    ]
    for num, fresh_ret, expect, label in cases:
        clear_outbox()
        acct = provision_install(num)
        set_policy_as(num, "k", "v")
        seed_claim(acct, "acme/%s" % num, "main", "svc/x.py")
        store = CountingStore(DSN_APP, max_attempts=5, stale_seconds=900, scan_cap=10000)
        gh = SpyGH()
        seen = {"degraded": "UNSET"}

        def ri(db, repo, branch="main", *a, **k):
            return {"repo": repo, "branch": branch, "refreshed": [{"change": "PR-1", "conclusion": "neutral"}]}

        def post(gh, repo, entries, db=None, branch="main", trace_id="", delivery="", **k):
            seen["degraded"] = k.get("graph_degraded")
            return len(entries)

        def fresh(db, gh_for, repo, branch, _ret=fresh_ret):
            if _ret == "RAISE":
                raise RuntimeError("freshness outage")
            return dict(_ret)

        PR._drain_policy_refreshes(store, gh, DSN_APP, limit=50,
                                   refresh_inflight=ri, post_refreshes=post, graph_freshness=fresh)
        chk(seen["degraded"] is expect,
            "policy drainer threads graph_degraded=%s into _post_refreshes (%s)" % (expect, label))


def drain_transient_unreachable_retries():
    clear_outbox()
    a = provision_install("207")
    set_policy_as("207", "k", "v")
    seed_claim(a, "acme/g", "main", "svc/g.py")
    store = CountingStore(DSN_APP)
    # for_account None BUT the install map is UNREACHABLE (a transient GET /app/installations 403 storm) — this
    # must NOT be terminally consumed as a dead install; it must RETRY.
    gh = SpyGH(dead_accounts={a}, reachable=False)
    ri, pr_post, refreshed, posted, seam = make_spies(store)
    PR._drain_policy_refreshes(store, gh, DSN_APP, limit=50, refresh_inflight=ri, post_refreshes=pr_post)
    row = outbox(a)
    chk("acme/g" not in posted and row["done"] is False and row["attempts"] == 1
        and row["last_error"] == "install_map_unreachable",
        "transient unreachable (for_account None + map UNREACHABLE) → NO post, NOT done, ONE attempt (no same-tick burn)")
    # the row is NOT terminally dropped — after the backoff window it is claimable again (bounded retry).
    age_claim(a)
    reclaim = PR._as_jsonb(_run(DSN_APP, "SELECT core.claim_policy_refresh_with_authority('wk',5,0,1000000)"))
    chk(isinstance(reclaim, dict) and reclaim.get("account_id") == a,
        "a transient-unreachable row stays claimable after the backoff (bounded retry, never a silent drop)")


def drain_partial_install_map_miss_retries():
    clear_outbox()
    account = provision_install("210")
    set_policy_as("210", "partial", "yes")
    seed_claim(account, "acme/partial", "main", "svc/partial.py")
    store = CountingStore(DSN_APP)
    gh = SpyGH(
        dead_accounts={account},
        reachable=True,
        complete=False,
    )
    ri, pr_post, _refreshed, posted, _seam = make_spies(store)
    PR._drain_policy_refreshes(
        store,
        gh,
        DSN_APP,
        limit=1,
        refresh_inflight=ri,
        post_refreshes=pr_post,
    )
    row = outbox(account)
    chk(
        posted == []
        and row["done"] is False
        and row["attempts"] == 1
        and row["last_error"] == "install_map_incomplete",
        "a cap-saturated reachable-but-partial map cannot prove absence; "
        "its miss retries instead of consuming the policy row",
    )


def drain_takes_repo_lock_seam():
    clear_outbox()
    a = provision_install("205")
    set_policy_as("205", "k", "v")
    seed_claim(a, "acme/e", "main", "svc/e.py")
    store = CountingStore(DSN_APP)
    gh = SpyGH()
    ri, pr_post, refreshed, posted, seam = make_spies(store)
    events: list = []

    def spy_take(cur, account_key, repo):
        events.append(("lock", account_key, repo))

    def spy_release(cur, account_key, repo):
        events.append(("unlock", account_key, repo))

    def ri_ordered(db, repo, branch="main", *aa, **kk):
        events.append(("refresh", repo))
        return ri(db, repo, branch)

    def post_ordered(gh_, repo, entries, **kk):
        events.append(("post", repo))
        return pr_post(gh_, repo, entries, **kk)

    PR._drain_policy_refreshes(store, gh, DSN_APP, limit=50, refresh_inflight=ri_ordered,
                               post_refreshes=post_ordered, take_repo_lock=spy_take, release_repo_lock=spy_release)
    chk(("lock", "205", "acme/e") in events and ("unlock", "205", "acme/e") in events,
        "the drainer uses the live-worker (account,repo) advisory key as a bounded contention barrier")
    if (("lock", "205", "acme/e") in events and ("unlock", "205", "acme/e") in events
            and ("refresh", "acme/e") in events and ("post", "acme/e") in events):
        li = events.index(("lock", "205", "acme/e"))
        ui = events.index(("unlock", "205", "acme/e"))
        ri = events.index(("refresh", "acme/e"))
        pi = events.index(("post", "acme/e"))
        chk(li < ui < ri < pi,
            "the contention barrier is released before refresh and GitHub post (lock < unlock < refresh < post)")
    else:
        chk(False, "the contention barrier is released before refresh and GitHub post")


def drain_repo_lock_serializes_with_live():
    clear_outbox()
    import server_dbops as SD
    a = provision_install("206")
    set_policy_as("206", "k", "v")
    seed_claim(a, "acme/f", "main", "svc/f.py")
    # Hold the EXACT per-(account,repo) advisory lock the LIVE webhook worker uses (event_processor via
    # server_dbops), on a separate connection — simulating a concurrent live event on this repo.
    hold = psycopg2.connect(DSN_APP)
    hold.autocommit = True
    with hold.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))", ("206", "acme/f"))
    saved = SD._LOCK_TIMEOUT_MS
    SD._LOCK_TIMEOUT_MS = 800   # so the drainer's CONTENDED acquire fails fast (proves it waits on the same key)
    try:
        store = CountingStore(DSN_APP)
        ri, pr_post, refreshed, posted, seam = make_spies(store)
        PR._drain_policy_refreshes(store, SpyGH(), DSN_APP, limit=50, refresh_inflight=ri, post_refreshes=pr_post)
        row = outbox(a)
        chk("acme/f" not in posted and row["done"] is False and row["attempts"] >= 1,
            "a HELD per-(account,repo) lock BLOCKS the drainer's post (serialized with the live worker) → repo error → account retried")
    finally:
        with hold.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))", ("206", "acme/f"))
        hold.close()
        SD._LOCK_TIMEOUT_MS = saved
    # the blocked tick left the row failed + leased (backoff) — age past it so the next drain can re-claim.
    age_claim(a)
    # lock free now → the next drain acquires it, refreshes, posts, marks done (serialization is not a permanent block).
    store2 = CountingStore(DSN_APP)
    ri2, post2, refreshed2, posted2, seam2 = make_spies(store2)
    PR._drain_policy_refreshes(store2, SpyGH(), DSN_APP, limit=50, refresh_inflight=ri2, post_refreshes=post2)
    chk("acme/f" in posted2 and outbox(a)["done"] is True,
        "once the lock is released the next drain acquires it, posts, and marks done")


def external_mutations_are_exact_fenced_and_not_overtaken():
    take_lock, release_lock = PR._resolve_repo_lock_fns(None, None)

    # The drainer's pre-work generation check is not sufficient by itself:
    # replacement can win after that check.  The mutation wrapper repeats the
    # exact id+activation timestamp predicate under its account-lifecycle
    # session fence before each GitHub write.
    clear_outbox()
    generation_account = provision_install("362")
    set_installation_generation(
        generation_account,
        "9000362",
        "2026-07-29T00:00:32+00:00",
    )
    set_policy_as("362", "fence", "generation-a")
    seed_claim(
        generation_account,
        "fence/generation",
        "main",
        "svc/a.py",
    )
    generation_row = PR.PolicyRefreshStore(DSN_APP).claim()
    generation_calls: list[str] = []

    class GenerationGH:
        def upsert_check(self, *_args, **_kwargs):
            generation_calls.append("external")
            return {"id": 1}

    generation_refused = False
    try:
        with PR._external_mutation_authority(
                DSN_APP, GenerationGH(), "362", "fence/generation",
                generation_row, take_repo_lock=take_lock,
                release_repo_lock=release_lock) as fenced:
            set_installation_generation(
                generation_account,
                "9001362",
                "2026-07-29T00:01:32+00:00",
            )
            fenced.upsert_check(
                "fence/generation", "a" * 40,
                "success", "t", "s")
    except PR.ExternalWriteAuthorityLost:
        generation_refused = True
    chk(
        generation_refused and generation_calls == [],
        "a replacement after preflight is re-fenced under the "
        "account-lifecycle lock before every GitHub mutation",
    )

    # Waiting for the repo lock may consume the lease. The post guard checks
    # remaining wall after acquisition and must make zero external calls.
    clear_outbox()
    account = provision_install("360")
    set_policy_as("360", "fence", "v1")
    seed_claim(account, "fence/repo", "main", "svc/a.py")
    row = PR.PolicyRefreshStore(DSN_APP).claim()
    _run(
        DSN_MIG,
        "UPDATE core.installation_account "
        "SET convergence_claimed_until=clock_timestamp()+interval '60 seconds' "
        "WHERE account_id=%s",
        (account,),
    )
    calls: list[str] = []

    class GH:
        def upsert_check(self, *_args, **_kwargs):
            calls.append("external")
            return {"id": 1}

    refused = False
    try:
        with PR._external_mutation_authority(
                DSN_APP, GH(), "360", "fence/repo", row,
                take_repo_lock=take_lock,
                release_repo_lock=release_lock) as fenced:
            fenced.upsert_check("fence/repo", "a" * 40, "success", "t", "s")
    except PR.ExternalWriteAuthorityLost:
        refused = True
    chk(
        refused and calls == [],
        "insufficient exact lease wall refuses the writer before any GitHub mutation",
    )
    held: list[float] = []
    original_sleep = PR.time.sleep

    class LostResponseGH:
        def upsert_check(self, *_args, **_kwargs):
            raise TimeoutError("response lost after request send")

    PR.time.sleep = lambda seconds: held.append(float(seconds))
    try:
        try:
            PR._LeaseFencedGitHub(
                LostResponseGH(), lambda: None
            ).upsert_check("fence/repo", "a" * 40, "success", "t", "s")
        except TimeoutError:
            pass
    finally:
        PR.time.sleep = original_sleep
    chk(
        held == [float(PR._GITHUB_REMOTE_TERMINATION_HOLD_SECONDS)],
        "ambiguous GitHub response loss retains the repo lock beyond the documented remote termination wall",
    )

    # A new exact owner can be claimed while an old already-started POST is
    # delayed, but cannot enter the same repo's mutation section until the old
    # call returns and releases the session advisory lock.
    clear_outbox()
    account = provision_install("361")
    set_policy_as("361", "fence", "v1")
    seed_claim(account, "fence/order", "main", "svc/a.py")
    store = PR.PolicyRefreshStore(DSN_APP)
    old_row = store.claim()
    old_entered = threading.Event()
    release_old = threading.Event()
    new_entered = threading.Event()
    order: list[str] = []
    errors: list[Exception] = []

    class OldGH:
        def upsert_check(self, *_args, **_kwargs):
            order.append("old-enter")
            old_entered.set()
            if not release_old.wait(5):
                raise RuntimeError("old post release timed out")
            order.append("old-exit")
            return {"id": 1}

    class NewGH:
        def upsert_check(self, *_args, **_kwargs):
            order.append("new-enter")
            new_entered.set()
            return {"id": 2}

    def run_post(gh, claimed):
        try:
            with PR._external_mutation_authority(
                    DSN_APP, gh, "361", "fence/order", claimed,
                    take_repo_lock=take_lock,
                    release_repo_lock=release_lock) as fenced:
                fenced.upsert_check(
                    "fence/order", "a" * 40, "success", "t", "s")
        except Exception as exc:
            errors.append(exc)

    old_thread = threading.Thread(target=run_post, args=(OldGH(), old_row))
    old_thread.start()
    entered = old_entered.wait(5)
    set_policy_as("361", "fence", "v2")
    _run(
        DSN_MIG,
        "UPDATE core.installation_account "
        "SET convergence_claimed_until=clock_timestamp()-interval '1 second',"
        "policy_refresh_due_at=clock_timestamp()-interval '1 second' "
        "WHERE account_id=%s",
        (account,),
    )
    new_row = store.claim()
    new_thread = threading.Thread(target=run_post, args=(NewGH(), new_row))
    new_thread.start()
    time.sleep(0.2)
    overtook = new_entered.is_set()
    release_old.set()
    old_thread.join(5)
    new_thread.join(5)
    if not (
            entered and new_row and not overtook and not errors
            and order == ["old-enter", "old-exit", "new-enter"]):
        print(
            "    external ordering diagnostic:",
            {
                "entered": entered,
                "new_claim": bool(new_row),
                "overtook": overtook,
                "errors": [type(exc).__name__ for exc in errors],
                "order": order,
                "old_alive": old_thread.is_alive(),
                "new_alive": new_thread.is_alive(),
            },
        )
    chk(
        entered and new_row and not overtook and not errors
        and order == ["old-enter", "old-exit", "new-enter"],
        "new worker cannot overtake an old delayed GitHub POST on the same repository",
    )


def external_mutation_scoped_replay_is_atomic():
    """A planned replay shares one real transaction; failure cannot commit its early handler writes."""
    clear_outbox()
    account = provision_install("363")
    repo, branch, path = "fence/atomic-replay", "main", "svc/atomic.py"
    seed_claim(account, repo, branch, path)
    set_policy_as("363", "fence", "atomic-replay")
    row = PR.PolicyRefreshStore(DSN_APP).claim()
    take_lock, release_lock = PR._resolve_repo_lock_fns(None, None)

    def active_claims() -> int:
        return int(_read_pinned(
            account,
            "SELECT count(*) FROM core.claim "
            "WHERE account_id=%s AND repo=%s AND branch=%s "
            "AND change_id='PR-1' AND claim_state='active'",
            (account, repo, branch),
        ) or 0)

    savepoints = []
    injected = False
    try:
        with PR._external_mutation_authority(
                DSN_APP, SpyGH(), "363", repo, row,
                take_repo_lock=take_lock, release_repo_lock=release_lock,
                yield_scoped_db=True) as authority:
            _fenced, held_db = authority
            # The ordinary PR handler depends on these controls for optional reads. They were structurally broken
            # while the held backend stayed in autocommit mode.
            savepoints.append(W._txn_cmd(held_db, "SAVEPOINT vp_atomic_probe"))
            savepoints.append(W._txn_cmd(held_db, "RELEASE SAVEPOINT vp_atomic_probe"))
            held_db(
                "SELECT core.release_change_on_main_with_authority(%s,%s,%s)",
                ("PR-1", repo, branch),
            )
            raise RuntimeError("injected after early planned-replay write")
    except RuntimeError as exc:
        injected = str(exc) == "injected after early planned-replay write"

    rolled_back = active_claims() == 1
    optional_recovered = False
    with PR._external_mutation_authority(
            DSN_APP, SpyGH(), "363", repo, row,
            take_repo_lock=take_lock, release_repo_lock=release_lock,
            yield_scoped_db=True) as authority:
        _fenced, held_db = authority
        # Exercise the critical aborted-transaction path, not only a successful SAVEPOINT/RELEASE pair. The
        # undefined function aborts the savepoint's subtransaction; _optional must issue ROLLBACK TO without a
        # pre-fence SELECT, after which this turn's next fenced business write must still succeed and commit.
        optional_recovered = W._optional(
            held_db, "atomic replay optional probe",
            lambda: held_db("SELECT core.__veripsa_missing_optional_probe__()"),
            default="isolated", repo=repo, pr=1,
        ) == "isolated"
        held_db(
            "SELECT core.release_change_on_main_with_authority(%s,%s,%s)",
            ("PR-1", repo, branch),
        )
    committed = active_claims() == 0
    chk(
        injected and savepoints == [True, True] and rolled_back
        and optional_recovered and committed,
        "the held planned-replay backend supports real SAVEPOINTs, rolls every early DB mutation back on failure, "
        "recovers an aborted optional subtransaction, and commits only after a successful context receipt",
    )


def drain_onboarding_final_authority_refuses_stale_watching():
    """The final Watching turn repeats full repo/default/HEAD authority and honors the 70s lease admission."""
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))

    def run_drift(owner: str, repository_id: str, *, drift_branch: str, drift_head: str):
        clear_outbox()
        account = provision_install(owner)
        installation_id = str(800000 + int(owner))
        created_at = f"2026-08-03T00:10:{int(owner) % 60:02d}Z"
        set_installation_generation(account, installation_id, created_at)
        repo = f"watch/{owner}"
        target = "a" * 40
        enqueue_onboarding(owner, repo, repository_id)
        resolver_calls = []
        planner_calls = []
        replay_calls = []
        strict_calls = []
        gh = SpyGH()

        def resolve(_db, _gh, requested_repo, requested_id, expected_owner):
            resolver_calls.append((requested_repo, str(requested_id), str(expected_owner)))
            final = len(resolver_calls) > 1
            return {
                "repo": repo, "repository_id": repository_id,
                "default_branch": drift_branch if final else "main",
                "head_sha": drift_head if final else target,
                "empty": False,
            }

        def strict(db, _gh, requested_repo, branch, target_sha, requested_id, **_kwargs):
            strict_calls.append((requested_repo, branch, target_sha, str(requested_id)))
            stats = db(
                "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                (json.dumps(graph), requested_repo, branch, target_sha))
            db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
               (requested_repo, requested_id))
            return {
                "converged": True,
                "ingest": {
                    **(stats if isinstance(stats, dict) else {}),
                    "files": 2, "edges": 1, "over_cap": False,
                },
            }

        def plan(_gh, requested_repo):
            planner_calls.append(requested_repo)
            return {"pr_numbers": [7], "truncated": False}

        def replay(_db, _gh, requested_repo, branch, target_sha, requested_id,
                   expected_owner, number):
            replay_calls.append((requested_repo, branch, target_sha, str(requested_id),
                                 str(expected_owner), number))
            return {"planned_pr": number, "processed": True, "receipt": True}

        summaries = []
        store = PR.PolicyRefreshStore(DSN_APP)
        for _turn in range(4):
            summaries.append(PR._drain_policy_refreshes(
                store, gh, DSN_APP, limit=1,
                graph_refresh_strict=strict,
                resolve_onboarding_head=resolve,
                build_onboarding_plan=plan,
                replay_onboarding_pr=replay,
                refresh_inflight=lambda *_a, **_k: {"refreshed": []},
                post_refreshes=lambda *_a, **_k: 0))
        return {
            "account": account, "row": graph_outbox(account, repository_id),
            "gh": gh, "resolver_calls": resolver_calls,
            "planner_calls": planner_calls, "replay_calls": replay_calls,
            "strict_calls": strict_calls, "summaries": summaries,
        }

    branch_drift = run_drift(
        "371", "9371", drift_branch="trunk", drift_head="b" * 40)
    head_drift = run_drift(
        "372", "9372", drift_branch="main", drift_head="c" * 40)
    chk(
        len(branch_drift["resolver_calls"]) == 2
        and len(head_drift["resolver_calls"]) == 2
        and len(branch_drift["strict_calls"]) == len(head_drift["strict_calls"]) == 1
        and len(branch_drift["planner_calls"]) == len(head_drift["planner_calls"]) == 1
        and len(branch_drift["replay_calls"]) == len(head_drift["replay_calls"]) == 1
        and branch_drift["gh"].checks == [] and head_drift["gh"].checks == []
        and branch_drift["row"]["branch"] == "trunk"
        and branch_drift["row"]["target_sha"] == "b" * 40
        and branch_drift["row"]["onboarding_plan"] is None
        and branch_drift["row"]["onboarding_index"] == 0
        and head_drift["row"]["branch"] == "main"
        and head_drift["row"]["target_sha"] == "c" * 40
        and head_drift["row"]["onboarding_plan"] == [7]
        and head_drift["row"]["onboarding_index"] == 1,
        "the final Watching boundary performs a second full authority read: default-branch drift refreezes, "
        "same-branch HEAD drift preserves the immutable plan/index, and neither posts a stale Watching Check",
    )

    # Prepare an empty-plan final turn, then shorten only its exact graph lease below the 70s admission wall.
    # The mutation context must reject before the final resolver (a GitHub metadata/HEAD read) or any Check call.
    clear_outbox()
    fence_account = provision_install("373")
    fence_installation_id = "800373"
    fence_created_at = "2026-08-03T00:11:13Z"
    fence_repo, fence_repository_id, fence_target = "watch/fence", "9373", "d" * 40
    set_installation_generation(fence_account, fence_installation_id, fence_created_at)
    enqueue_onboarding("373", fence_repo, fence_repository_id)
    fence_resolver_calls = []
    fence_gh = SpyGH()

    def fence_resolve(_db, _gh, requested_repo, requested_id, expected_owner):
        fence_resolver_calls.append((requested_repo, str(requested_id), str(expected_owner)))
        return {
            "repo": fence_repo, "repository_id": fence_repository_id,
            "default_branch": "main", "head_sha": fence_target, "empty": False,
        }

    def fence_strict(db, _gh, requested_repo, branch, target_sha, requested_id, **_kwargs):
        stats = db(
            "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
            (json.dumps(graph), requested_repo, branch, target_sha))
        db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
           (requested_repo, requested_id))
        return {
            "converged": True,
            "ingest": {
                **(stats if isinstance(stats, dict) else {}),
                "files": 2, "edges": 1, "over_cap": False,
            },
        }

    setup_store = PR.PolicyRefreshStore(DSN_APP)
    for _turn in range(2):
        PR._drain_policy_refreshes(
            setup_store, fence_gh, DSN_APP, limit=1,
            graph_refresh_strict=fence_strict,
            resolve_onboarding_head=fence_resolve,
            build_onboarding_plan=lambda *_a, **_k: {
                "pr_numbers": [], "truncated": False},
            refresh_inflight=lambda *_a, **_k: {"refreshed": []},
            post_refreshes=lambda *_a, **_k: 0)

    class ShortOnboardingLeaseStore(PR.PolicyRefreshStore):
        def claim(self):
            row = super().claim()
            if isinstance(row, dict) and row.get("request_kind") == "graph":
                _run(
                    DSN_MIG,
                    "SELECT set_config('core.current_account',%s,true); "
                    "SELECT core.mark_governed_write('graph_convergence_lease'); "
                    "UPDATE core.graph_convergence_lease "
                    "SET claimed_until=clock_timestamp()+interval '60 seconds' "
                    "WHERE account_id=%s AND slot=%s::smallint AND lease_epoch=%s",
                    (row["account_id"], row["account_id"],
                     int(row["graph_slot"]), int(row["lease_epoch"])),
                )
            return row

    fenced_result = PR._drain_policy_refreshes(
        ShortOnboardingLeaseStore(DSN_APP), fence_gh, DSN_APP, limit=1,
        graph_refresh_strict=fence_strict,
        resolve_onboarding_head=fence_resolve,
        build_onboarding_plan=lambda *_a, **_k: {
            "pr_numbers": [], "truncated": False},
        refresh_inflight=lambda *_a, **_k: {"refreshed": []},
        post_refreshes=lambda *_a, **_k: 0)
    fenced_row = graph_outbox(fence_account, fence_repository_id)
    chk(
        fenced_result.get("failed") == 1
        and len(fence_resolver_calls) == 1
        and fence_gh.checks == []
        and fenced_row["onboarding_watching_done"] is False
        and fenced_row["attempts"] == 1,
        "a planned final turn with <70s exact lease wall is refused before any final GitHub metadata/HEAD read "
        "or Watching mutation and retains the cursor for bounded retry",
    )


def drain_onboarding_quota_surfaces_claimless_plan_one_pr_per_turn():
    """Quota onboarding surfaces its frozen existing PRs without claims or repeated graph work."""
    clear_outbox()
    account = provision_install("374")
    installation_id = "800374"
    created_at = "2026-08-03T00:11:14Z"
    repo, repository_id, target = "quota/onboarding", "9374", "e" * 40
    set_installation_generation(account, installation_id, created_at)
    enqueue_onboarding("374", repo, repository_id)

    resolver_calls = []
    planner_calls = []
    strict_calls = []
    surface_calls = []
    refresh_calls = []

    def resolve(_db, _gh, requested_repo, requested_id, expected_owner):
        resolver_calls.append((requested_repo, str(requested_id), str(expected_owner)))
        return {
            "repo": repo, "repository_id": repository_id,
            "default_branch": "main", "head_sha": target, "empty": False,
        }

    def plan(_gh, requested_repo):
        planner_calls.append(requested_repo)
        return {"pr_numbers": [7, 8], "truncated": False}

    def strict(*_args, **_kwargs):
        strict_calls.append("quota_probe")
        return {"terminal_degraded": "quota_paused", "reason": "free-tier limit"}

    def surface(_db, _gh, requested_repo, branch, target_sha, requested_id,
                expected_owner, number):
        surface_calls.append((
            requested_repo, branch, target_sha, str(requested_id),
            str(expected_owner), number))
        return {
            "planned_pr": number, "processed": True,
            "receipt": True, "receipt_kind": "exact_quota_check",
        }

    def forbidden_replay(*_args, **_kwargs):
        raise AssertionError("quota onboarding must not enter ordinary PR replay")

    def forbidden_refresh(*_args, **_kwargs):
        raise AssertionError("claimless onboarding quota surface must not scan inflight claims")

    store = PR.PolicyRefreshStore(DSN_APP)
    gh = SpyGH()
    rows = []
    summaries = []
    for _turn in range(5):
        summaries.append(PR._drain_policy_refreshes(
            store, gh, DSN_APP, limit=1,
            graph_refresh_strict=strict,
            resolve_onboarding_head=resolve,
            build_onboarding_plan=plan,
            replay_onboarding_pr=forbidden_replay,
            surface_onboarding_quota_paused_pr=surface,
            refresh_inflight=forbidden_refresh,
            post_refreshes=lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("quota onboarding must not post ordinary verdicts"))))
        rows.append(graph_outbox(account, repository_id))

    claim_count = int(_read_pinned(
        account,
        "SELECT count(*) FROM core.claim WHERE account_id=%s AND repo=%s",
        (account, repo),
    ) or 0)
    router = _read_pinned(
        account,
        "SELECT jsonb_build_object("
        "'pending',convergence_pending_count,"
        "'quota',convergence_quota_deferred_count,"
        "'retry',convergence_retry_exhausted_count) "
        "FROM core.installation_account WHERE account_id=%s",
        (account,),
    ) or {}
    chk(
        len(resolver_calls) == 1 and len(planner_calls) == 1
        and len(strict_calls) == 1
        and [call[-1] for call in surface_calls] == [7, 8]
        and rows[0]["onboarding_head_pending"] is False
        and rows[0]["onboarding_plan"] is None
        and rows[1]["onboarding_plan"] == [7, 8]
        and rows[1]["onboarding_index"] == 0
        and rows[1]["terminal_reason"] is None
        and rows[2]["onboarding_index"] == 1
        and rows[2]["terminal_reason"] == "quota_paused"
        and rows[2]["not_before"] is None
        and rows[3]["onboarding_index"] == 2
        and rows[3]["terminal_reason"] == "quota_paused"
        and rows[3]["not_before"] is None
        and rows[4]["onboarding_index"] == 2
        and rows[4]["terminal_reason"] == "quota_paused"
        and rows[4]["not_before"] is not None
        and all(row["attempts"] == 0 and not row["claimed"] for row in rows)
        and [summary.get("tail_rearmed") for summary in summaries[:4]] == [1, 1, 1, 1]
        and summaries[4].get("quota_deferred") == 1
        and claim_count == 0
        and router == {"pending": 0, "quota": 1, "retry": 0},
        "claimless onboarding resolves HEAD, freezes CAP+1 before its sole strict quota probe, then publishes "
        "exactly one planned PR per fair turn and defers only after every receipt",
    )

    # The non-NULL deferred deadline suppresses the immediate surface phase. Once due, one strict probe is
    # mandatory so a lifted quota can resume normal convergence; a still-paused result must not replay PRs.
    age_claim(account)

    def empty_refresh(*_args, **_kwargs):
        refresh_calls.append("due_quota_probe")
        return {"refreshed": []}

    due = PR._drain_policy_refreshes(
        store, gh, DSN_APP, limit=1,
        graph_refresh_strict=strict,
        resolve_onboarding_head=resolve,
        build_onboarding_plan=plan,
        replay_onboarding_pr=forbidden_replay,
        surface_onboarding_quota_paused_pr=surface,
        refresh_inflight=empty_refresh,
        post_refreshes=lambda *_a, **_k: 0)
    due_row = graph_outbox(account, repository_id)
    chk(
        due.get("quota_deferred") == 1 and len(strict_calls) == 2
        and refresh_calls == ["due_quota_probe"]
        and [call[-1] for call in surface_calls] == [7, 8]
        and due_row["onboarding_index"] == 2
        and due_row["terminal_reason"] == "quota_paused"
        and due_row["not_before"] is not None and due_row["attempts"] == 0,
        "a completed quota plan stays cold until due, then performs one fresh strict probe without duplicating "
        "an already-receipted PR surface",
    )

    clear_outbox()
    failed_account = provision_install("375")
    failed_installation_id = "800375"
    failed_created_at = "2026-08-03T00:11:15Z"
    failed_repo, failed_repository_id = "quota/unconfirmed", "9375"
    set_installation_generation(
        failed_account, failed_installation_id, failed_created_at)
    enqueue_onboarding("375", failed_repo, failed_repository_id)
    failed_strict_calls = []
    failed_surface_calls = []

    def failed_resolve(_db, _gh, _repo, _repository_id, _expected_owner):
        return {
            "repo": failed_repo, "repository_id": failed_repository_id,
            "default_branch": "main", "head_sha": "f" * 40, "empty": False,
        }

    def failed_strict(*_args, **_kwargs):
        failed_strict_calls.append("quota_probe")
        return {"terminal_degraded": "quota_paused", "reason": "free-tier limit"}

    def unconfirmed_surface(_db, _gh, _repo, _branch, _target, _repository_id,
                            _expected_owner, number):
        failed_surface_calls.append(number)
        raise TimeoutError("injected authoritative PR read timeout")

    failed_summaries = []
    failed_store = PR.PolicyRefreshStore(DSN_APP)
    for _turn in range(3):
        failed_summaries.append(PR._drain_policy_refreshes(
            failed_store, SpyGH(), DSN_APP, limit=1,
            graph_refresh_strict=failed_strict,
            resolve_onboarding_head=failed_resolve,
            build_onboarding_plan=lambda *_a, **_k: {
                "pr_numbers": [9], "truncated": False},
            replay_onboarding_pr=forbidden_replay,
            surface_onboarding_quota_paused_pr=unconfirmed_surface,
            refresh_inflight=forbidden_refresh,
            post_refreshes=lambda *_a, **_k: 0))
    failed_row = graph_outbox(failed_account, failed_repository_id)
    failed_claim_count = int(_read_pinned(
        failed_account,
        "SELECT count(*) FROM core.claim WHERE account_id=%s AND repo=%s",
        (failed_account, failed_repo),
    ) or 0)
    chk(
        failed_summaries[2].get("quota_deferred") == 1
        and failed_strict_calls == ["quota_probe"]
        and failed_surface_calls == [9]
        and failed_row["onboarding_plan"] == [9]
        and failed_row["onboarding_index"] == 0
        and failed_row["terminal_reason"] == "quota_paused"
        and failed_row["not_before"] is not None
        and failed_row["attempts"] == 0 and not failed_row["claimed"]
        and failed_claim_count == 0,
        "an exception/ambiguous quota read never advances the immutable PR cursor or falls into finite retry; "
        "it keeps quota state attempts-neutral without creating a claim",
    )

    clear_outbox()
    malformed_account = provision_install("376")
    malformed_installation_id = "800376"
    malformed_created_at = "2026-08-03T00:11:16Z"
    malformed_repo, malformed_repository_id = "quota/malformed", "9376"
    set_installation_generation(
        malformed_account, malformed_installation_id, malformed_created_at)
    enqueue_onboarding("376", malformed_repo, malformed_repository_id)
    malformed_strict_calls = []

    def malformed_resolve(_db, _gh, _repo, _repository_id, _expected_owner):
        return {
            "repo": malformed_repo, "repository_id": malformed_repository_id,
            "default_branch": "main", "head_sha": "1" * 40, "empty": False,
        }

    def malformed_strict(*_args, **_kwargs):
        malformed_strict_calls.append("quota_probe")
        return {"terminal_degraded": "quota_paused", "reason": "free-tier limit"}

    malformed_summaries = []
    for _turn in range(3):
        malformed_summaries.append(PR._drain_policy_refreshes(
            PR.PolicyRefreshStore(DSN_APP), SpyGH(), DSN_APP, limit=1,
            graph_refresh_strict=malformed_strict,
            resolve_onboarding_head=malformed_resolve,
            build_onboarding_plan=lambda *_a, **_k: {
                "pr_numbers": [10], "truncated": False},
            replay_onboarding_pr=forbidden_replay,
            surface_onboarding_quota_paused_pr=lambda *_a, **_k: {
                "planned_pr": 10, "receipt": False},
            refresh_inflight=forbidden_refresh,
            post_refreshes=lambda *_a, **_k: 0))
    malformed_row = graph_outbox(malformed_account, malformed_repository_id)
    chk(
        malformed_summaries[2].get("failed") == 1
        and malformed_summaries[2].get("quota_deferred") == 0
        and malformed_strict_calls == ["quota_probe"]
        and malformed_row["onboarding_plan"] == [10]
        and malformed_row["onboarding_index"] == 0
        and malformed_row["terminal_reason"] is None
        and malformed_row["not_before"] is not None
        and malformed_row["attempts"] == 1 and not malformed_row["claimed"],
        "a malformed internal quota-surface receipt stays a visible bounded runtime failure instead of being "
        "misreported as a healthy indefinite quota defer",
    )


def drain_no_open_prs_noop():
    clear_outbox()
    a = provision_install("203")
    set_policy_as("203", "k", "v")             # policy change, but NO in-flight claims seeded
    store = CountingStore(DSN_APP)
    gh = SpyGH()
    ri, pr_post, refreshed, posted, seam = make_spies(store)
    res = PR._drain_policy_refreshes(store, gh, DSN_APP, limit=50, refresh_inflight=ri, post_refreshes=pr_post)
    chk(posted == [] and res.get("drained") == 1 and outbox(a)["done"] is True,
        "a tenant with no open PRs drains to a clean no-op (no refresh_inflight per repo, no post)")


def drain_policy_one_repo_slices_and_supersede():
    clear_outbox()
    acct = provision_install("340")
    set_policy_as("340", "slice", "v1")
    for suffix in ("a", "b", "c"):
        seed_claim(acct, "slice/%s" % suffix, "main", "svc/%s.py" % suffix)

    posted: list[str] = []

    def refresh(db, repo, branch="main", **kwargs):
        return {"refreshed": [{"change": "PR-1", "conclusion": "neutral"}]}

    def post(gh, repo, entries, **kwargs):
        posted.append(repo)
        return len(entries)

    store = CountingStore(DSN_APP)
    first = PR._drain_policy_refreshes(
        store, SpyGH(), DSN_APP, limit=1, coord_cap=100000,
        refresh_inflight=refresh, post_refreshes=post,
        graph_freshness=lambda *a, **k: {"behind": False})
    first_row = outbox(acct)
    chk(posted == ["slice/a"] and first.get("policy_sliced") == 1
        and first.get("policy_drained") == 0 and first_row["done"] is False
        and first_row["attempts"] == 0 and first_row["claimed"] is False
        and (first_row["cursor_repo"], first_row["cursor_branch"]) == ("slice/a", "main"),
        "one policy turn renders exactly ONE repo regardless of coord_cap, then attempts-neutral requeues its exact epoch")

    second = PR._drain_policy_refreshes(
        store, SpyGH(), DSN_APP, limit=1,
        refresh_inflight=refresh, post_refreshes=post,
        graph_freshness=lambda *a, **k: {"behind": False})
    second_row = outbox(acct)
    third = PR._drain_policy_refreshes(
        store, SpyGH(), DSN_APP, limit=1,
        refresh_inflight=refresh, post_refreshes=post,
        graph_freshness=lambda *a, **k: {"behind": False})
    chk(posted == ["slice/a", "slice/b", "slice/c"]
        and second.get("policy_sliced") == 1
        and second_row["cursor_repo"] == "slice/b"
        and third.get("policy_drained") == 1 and outbox(acct)["done"] is True,
        "durable lexicographic cursor resumes at the next repo and terminally finishes only after the last slice")

    # Recreate and supersede DURING the first slow post. The stale exact-epoch requeue must miss without
    # overwriting the newer epoch/cursor; the new request restarts from the first coordinate under current policy.
    clear_outbox()
    set_policy_as("340", "slice", "v2")
    old_epoch = outbox(acct)["epoch"]
    superseded_once = {"value": False}
    replayed: list[str] = []

    def superseding_post(gh, repo, entries, **kwargs):
        replayed.append(repo)
        if not superseded_once["value"]:
            superseded_once["value"] = True
            set_policy_as("340", "slice", "v3")
        return len(entries)

    stale = PR._drain_policy_refreshes(
        store, SpyGH(), DSN_APP, limit=1,
        refresh_inflight=refresh, post_refreshes=superseding_post,
        graph_freshness=lambda *a, **k: {"behind": False})
    newer = outbox(acct)
    chk(stale.get("superseded") == 1 and newer["epoch"] > old_epoch
        and newer["cursor_repo"] == "" and newer["cursor_branch"] == ""
        and newer["done"] is False and newer["claimed"] is False and newer["attempts"] == 0,
        "slice requeue CAS cannot consume a policy write that supersedes it; newer epoch resets and remains immediately pending")

    PR._drain_policy_refreshes(
        store, SpyGH(), DSN_APP, limit=1,
        refresh_inflight=refresh, post_refreshes=superseding_post,
        graph_freshness=lambda *a, **k: {"behind": False})
    chk(replayed == ["slice/a", "slice/a"],
        "after a supersede CAS miss the newer epoch restarts from the first repo (no coordinate is silently skipped)")

    # Cross-account fairness: A has another repo, but yielding moves A behind B's already-due sentinel.
    clear_outbox()
    account_a = provision_install("344")
    set_policy_as("344", "slice", "a")
    seed_claim(account_a, "fair-policy/a", "main", "svc/a.py")
    seed_claim(account_a, "fair-policy/b", "main", "svc/b.py")
    account_b = provision_install("345")
    set_policy_as("345", "slice", "b")
    seed_claim(account_b, "fair-policy/z", "main", "svc/z.py")
    fair_order: list[str] = []

    def fair_post(gh, repo, entries, **kwargs):
        fair_order.append(repo)
        return len(entries)

    fair_store = CountingStore(DSN_APP)
    PR._drain_policy_refreshes(
        fair_store, SpyGH(), DSN_APP, limit=1,
        refresh_inflight=refresh, post_refreshes=fair_post,
        graph_freshness=lambda *a, **k: {"behind": False})
    PR._drain_policy_refreshes(
        fair_store, SpyGH(), DSN_APP, limit=1,
        refresh_inflight=refresh, post_refreshes=fair_post,
        graph_freshness=lambda *a, **k: {"behind": False})
    chk(fair_order == ["fair-policy/a", "fair-policy/z"],
        "policy slice yield moves a large account to the scheduler tail so an already-due second account runs next")


def refresh_connections_close_before_external_calls():
    clear_outbox()
    policy_acct = provision_install("341")
    set_policy_as("341", "idle", "policy")
    seed_claim(policy_acct, "idle/policy", "main", "svc/policy.py")
    policy_states: list[tuple[str, dict]] = []

    def policy_refresh(db, repo, branch="main", **kwargs):
        policy_states.append(("refresh", backend_absent_after_statement(db)))
        return {"refreshed": [{"change": "PR-1", "conclusion": "neutral"}]}

    def policy_freshness(db, gh, repo, branch):
        policy_states.append(("freshness", backend_absent_after_statement(db)))
        return {"behind": False}

    def policy_post(gh, repo, entries, db=None, **kwargs):
        policy_states.append(("post", backend_absent_after_statement(db)))
        return len(entries)

    PR._drain_policy_refreshes(
        CountingStore(DSN_APP), SpyGH(), DSN_APP, limit=1,
        refresh_inflight=policy_refresh, post_refreshes=policy_post,
        graph_freshness=policy_freshness)
    chk(len(policy_states) == 3 and all(not state for _label, state in policy_states),
        "policy refresh/freshness/GitHub-post seams retain no DB backend after each bounded statement")

    clear_outbox()
    graph_acct = provision_install("342")
    target = "9" * 40
    enqueue_graph("342", "idle/graph", "main", target, "9342")
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
    graph_states: list[tuple[str, dict]] = []
    graph_pid = {"value": None}

    class IdleGraphGH(SpyGH):
        def upsert_check(self, repo, sha, conclusion, title, summary, **kwargs):
            graph_states.append(("watching", backend_transaction_state(graph_pid["value"])))
            return super().upsert_check(repo, sha, conclusion, title, summary, **kwargs)

    def strict(db, gh, repo, branch, target_sha, repository_id, **kwargs):
        graph_pid["value"] = int(db("SELECT pg_backend_pid()"))
        graph_states.append(("strict_external_before", backend_transaction_state(graph_pid["value"])))
        stats = db(
            "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
            (json.dumps(graph), repo, branch, target_sha))
        db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (repo, repository_id))
        graph_states.append(("strict_external_after", backend_transaction_state(graph_pid["value"])))
        return {
            "converged": True,
            "ingest": {
                **(stats if isinstance(stats, dict) else {}),
                "cold_start": True, "files": 2, "edges": 1, "over_cap": False,
            },
        }

    def graph_refresh(db, repo, branch="main", **kwargs):
        graph_states.append(("refresh", backend_transaction_state(graph_pid["value"])))
        return {"refreshed": []}

    def graph_post(gh, repo, entries, db=None, **kwargs):
        graph_states.append(("post", backend_transaction_state(graph_pid["value"])))
        return 0

    result = PR._drain_policy_refreshes(
        PR.PolicyRefreshStore(DSN_APP), IdleGraphGH(), DSN_APP, limit=1,
        graph_refresh_strict=strict, refresh_inflight=graph_refresh,
        post_refreshes=graph_post)
    chk(result.get("graph_drained") == 1 and graph_outbox(graph_acct, "9342")["done"]
        and len(graph_states) >= 5 and all(not state for _label, state in graph_states),
        "strict graph callback, Watching, impact refresh, and GitHub-post seams retain no DB backend")


def refresh_phase_reconnect_failure_is_retryable():
    """Losing one fresh phase connection must retain the exact row, never falsely complete it."""
    clear_outbox()
    acct = provision_install("349")
    set_policy_as("349", "reconnect", "retry")
    seed_claim(acct, "reconnect/policy", "main", "svc/reconnect.py")
    original = PR._bounded_connect
    injection = {"armed": False, "fired": False}

    def flaky_connect(dsn):
        if injection["armed"] and not injection["fired"]:
            injection["fired"] = True
            raise psycopg2.OperationalError("injected phase reconnect failure")
        return original(dsn)

    def post(_gh, _repo, entries, *, db=None, **_kwargs):
        injection["armed"] = True
        db("SELECT 1")  # production poster DB overlay reconnect: fail exactly once here
        raise AssertionError("the injected reconnect should have raised")

    PR._bounded_connect = flaky_connect
    try:
        result = PR._drain_policy_refreshes(
            PR.PolicyRefreshStore(DSN_APP), SpyGH(), DSN_APP, limit=1,
            refresh_inflight=lambda *a, **k: {
                "refreshed": [{"change": "PR-1", "conclusion": "neutral"}]},
            graph_freshness=lambda *a, **k: {"behind": False},
            post_refreshes=post)
    finally:
        PR._bounded_connect = original
    row = outbox(acct)
    chk(injection["fired"] and result.get("failed") == 1
        and row["done"] is False and row["claimed"] is False
        and row["attempts"] == 1 and row["last_error"] == "repo_refresh_failed",
        "a fresh-phase reconnect failure retains the exact row for retry and never marks it done")


def drain_fail_then_slow_retry():
    clear_outbox()
    a = provision_install("204")
    set_policy_as("204", "k", "v")
    seed_claim(a, "acme/d", "main", "svc/d.py")

    store = CountingStore(DSN_APP, max_attempts=5)
    gh = SpyGH()
    ri, pr_post, refreshed, posted, seam = make_spies(store, post_raises=True)   # every post raises
    attempts = []
    for turn in range(5):
        PR._drain_policy_refreshes(
            store, gh, DSN_APP, limit=50,
            refresh_inflight=ri, post_refreshes=pr_post)
        attempts.append(outbox(a)["attempts"])
        if turn < 4:
            age_claim(a)
    not_yet = _run(
        DSN_APP,
        "SELECT core.claim_policy_refresh_with_authority('wk',5,1800,10000)")
    age_claim(a)
    claim_after = PR._as_jsonb(_run(
        DSN_APP,
        "SELECT core.claim_policy_refresh_with_authority('wk',5,1800,10000)"))
    chk(attempts == [1, 2, 3, 4, 5] and not outbox(a)["done"]
        and not_yet is None
        and claim_after.get("account_id") == a
        and claim_after.get("request_kind") == "policy",
        "a persistently failing refresh enters the bounded slow lane then auto-rearms without a new event")


def drain_graph_success_failure_and_quota_defer():
    clear_outbox()
    acct = provision_install("330")
    target = "5" * 40
    enqueue_graph("330", "graph/success", "main", target, "9301")
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
    events = []

    def strict(db, gh, repo, branch, target_sha, repository_id, **kwargs):
        graph_tx = int(db("SELECT txid_current()"))
        events.append(("graph", graph_tx))
        stats = db(
            "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
            (json.dumps(graph), repo, branch, target_sha))
        db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (repo, repository_id))
        return {
            "converged": True,
            "ingest": {
                **(stats if isinstance(stats, dict) else {}),
                "cold_start": True, "files": 2, "edges": 1, "over_cap": False,
            },
        }

    def refresh(db, repo, branch="main", **kwargs):
        events.append(("refresh", int(db("SELECT txid_current()"))))
        return {"refreshed": []}  # graph convergence is required even with no open PR/inflight claim

    def post(gh, repo, entries, **kwargs):
        events.append(("post", repo))
        return 0

    gh = SpyGH()
    result = PR._drain_policy_refreshes(
        PR.PolicyRefreshStore(DSN_APP), gh, DSN_APP, limit=1,
        graph_refresh_strict=strict, refresh_inflight=refresh, post_refreshes=post)
    row = graph_outbox(acct, "9301")
    graph_tx = next((e[1] for e in events if e[0] == "graph"), None)
    refresh_tx = next((e[1] for e in events if e[0] == "refresh"), None)
    chk(result.get("graph_drained") == 1 and row["done"] and row["attempts"] == 0,
        "graph turn converges and drains even when the repo has no open PR/inflight claim")
    chk(graph_tx and refresh_tx and graph_tx != refresh_tx
        and events.index(("graph", graph_tx)) < events.index(("refresh", refresh_tx))
        < events.index(("post", "graph/success")),
        "autocommit graph write finishes before the later short refresh query and GitHub post")
    chk(any(c["repo"] == "graph/success" and c["sha"] == target for c in gh.checks),
        "cold graph convergence posts the Watching check before impact refresh")

    clear_outbox()
    fail_acct = provision_install("331")
    enqueue_graph("331", "graph/fail", "main", "6" * 40, "9302")

    def strict_fail(*args, **kwargs):
        raise RuntimeError("customer path must not persist")

    failed = PR._drain_policy_refreshes(
        PR.PolicyRefreshStore(DSN_APP, max_attempts=5), SpyGH(), DSN_APP, limit=1,
        graph_refresh_strict=strict_fail,
        refresh_inflight=lambda *a, **k: {"refreshed": []},
        post_refreshes=lambda *a, **k: 0)
    fail_row = graph_outbox(fail_acct, "9302")
    chk(failed.get("failed") == 1 and not fail_row["done"] and fail_row["attempts"] == 1
        and fail_row["last_error"] == "exception_runtimeerror",
        "strict graph failure is retained for bounded retry with a content-free error code")

    clear_outbox()
    quota_acct = provision_install("332")
    quota_target = "7" * 40
    enqueue_graph("332", "graph/quota", "main", quota_target, "9303")
    seed_claim(quota_acct, "graph/quota", "main", "svc/quota.py")
    quota_gh = SpyGH()

    def strict_quota(*args, **kwargs):
        return {"terminal_degraded": "quota_paused", "reason": "free-tier limit"}

    quota_store = PR.PolicyRefreshStore(DSN_APP)
    quota = PR._drain_policy_refreshes(
        quota_store, quota_gh, DSN_APP, limit=1,
        graph_refresh_strict=strict_quota,
        refresh_inflight=lambda *a, **k: {
            "refreshed": [{"change": "PR-1", "conclusion": "unknown"}]},
        post_refreshes=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("ordinary verdict poster must not run under quota")))
    quota_row = graph_outbox(quota_acct, "9303")
    depth = quota_store.depth()
    chk(quota.get("quota_deferred") == 1 and quota_row["terminal_reason"] == "quota_paused"
        and quota_row["not_before"] is not None and quota_row["attempts"] == 0
        and not quota_row["done"] and not quota_row["claimed"],
        "free-tier wall becomes an attempts-neutral durable quota_paused defer, never finite-retry give-up")
    chk(len(quota_gh.comments) == 1 and len(quota_gh.checks) == 1
        and quota_gh.checks[0]["conclusion"] == "neutral",
        "quota defer idempotently replaces the open PR's stale verdict with existing fair-use check/comment surfaces")
    chk(depth.get("quota_deferred", 0) >= 1 and depth.get("claimed") == 0
        and quota_store.claim() is None,
        "aggregate queue health reports quota_deferred separately and the deferred row is not hot-reclaimed")

    rearmed_epoch = enqueue_graph("332", "graph/quota", "main", "8" * 40, "9303")
    rearmed = claim_graph("quota-lifted")
    chk(rearmed and rearmed["policy_epoch"] == rearmed_epoch
        and graph_outbox(quota_acct, "9303")["terminal_reason"] is None,
        "a new authoritative event immediately clears quota defer and re-arms the latest HEAD")
    fail_exact(rearmed)


def drain_change_fanout_is_durable_across_policy_graph_and_quota_pages():
    """A repo with more open PR surfaces than one API page must resume from durable state.

    The exact same lexicographic contract is exercised for the policy sentinel, a graph lease, and the
    quota-paused graph surface.  Sixty-five PRs at the production cap of thirty require exactly three turns;
    every intermediate turn is attempts-neutral and no successful PR is duplicated or skipped.
    """
    changes = sorted("PR-%d" % n for n in range(1, 66))
    entries = [{"change": change, "conclusion": "neutral"} for change in changes]

    def pager(seen: list[str], cursors: list[str]):
        def post(_gh, _repo, refreshes, *, return_progress=False, after_change="", **_kwargs):
            cursors.append(str(after_change or ""))
            remaining = sorted(
                entry["change"]
                for entry in refreshes
                if isinstance(entry, dict)
                and isinstance(entry.get("change"), str)
                and entry["change"] > str(after_change or ""))
            page = remaining[:30]
            seen.extend(page)
            result = {
                "posted": len(page),
                "processed": len(page),
                "cursor": page[-1] if page else str(after_change or ""),
                "has_more": len(remaining) > len(page),
                "errors": 0,
            }
            return result if return_progress else len(page)
        return post

    # Policy: the repo coordinate cannot advance while its PR fan-out still has a durable tail.
    clear_outbox()
    policy_acct = provision_install("346")
    set_policy_as("346", "page", "policy")
    seed_claim(policy_acct, "page/policy", "main", "svc/policy.py")
    policy_seen: list[str] = []
    policy_cursors: list[str] = []
    policy_results = []
    policy_rows = []
    policy_store = PR.PolicyRefreshStore(DSN_APP)
    for _ in range(3):
        policy_results.append(PR._drain_policy_refreshes(
            policy_store, SpyGH(), DSN_APP, limit=1,
            refresh_inflight=lambda *a, **k: {"refreshed": list(entries)},
            post_refreshes=pager(policy_seen, policy_cursors),
            graph_freshness=lambda *a, **k: {"behind": False}))
        policy_rows.append(outbox(policy_acct))
    chk(
        [r.get("change_sliced") for r in policy_results] == [1, 1, 0]
        and policy_results[2].get("policy_drained") == 1
        and all(
            not row["done"] and not row["claimed"] and row["attempts"] == 0
            and row["cursor_repo"] == "" and row["cursor_branch"] == ""
            for row in policy_rows[:2])
        and policy_rows[0]["change_cursor"] == changes[29]
        and policy_rows[1]["change_cursor"] == changes[59]
        and policy_rows[2]["done"] is True
        and policy_cursors == ["", changes[29], changes[59]]
        and policy_seen == changes,
        "65-PR policy fan-out resumes over three durable attempts-neutral pages without advancing the repo or skipping a PR")

    # Graph: each page releases only its exact slot lease; the same request epoch resumes with a fresh lease.
    clear_outbox()
    graph_acct = provision_install("347")
    graph_sha = "e" * 40
    enqueue_graph("347", "page/graph", "main", graph_sha, "9347")
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
    graph_seen: list[str] = []
    graph_cursors: list[str] = []
    persisted = {"value": False}

    def graph_strict(db, _gh, repo, branch, target_sha, repository_id, **_kwargs):
        if not persisted["value"]:
            db(
                "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                (json.dumps(graph), repo, branch, target_sha))
            db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (repo, repository_id))
            persisted["value"] = True
        return {"converged": True, "ingest": {"cold_start": False}}

    graph_results = []
    graph_rows = []
    graph_store = PR.PolicyRefreshStore(DSN_APP)
    graph_post = pager(graph_seen, graph_cursors)
    for _ in range(3):
        graph_results.append(PR._drain_policy_refreshes(
            graph_store, SpyGH(), DSN_APP, limit=1,
            graph_refresh_strict=graph_strict,
            refresh_inflight=lambda *a, **k: {"refreshed": list(entries)},
            post_refreshes=graph_post))
        graph_rows.append(graph_outbox(graph_acct, "9347"))
    chk(
        [r.get("change_sliced") for r in graph_results] == [1, 1, 0]
        and graph_results[2].get("graph_drained") == 1
        and all(
            not row["done"] and not row["claimed"] and row["attempts"] == 0
            for row in graph_rows[:2])
        and graph_rows[0]["change_cursor"] == changes[29]
        and graph_rows[1]["change_cursor"] == changes[59]
        and graph_rows[2]["done"] is True
        and graph_cursors == ["", changes[29], changes[59]]
        and graph_seen == changes,
        "65-PR graph fan-out resumes over three exact slot-lease pages without duplicates or omissions")

    # Quota: advisory surfaces use the same durable cursor, but only the terminal page arms quota defer.
    clear_outbox()
    quota_acct = provision_install("348")
    enqueue_graph("348", "page/quota", "main", "f" * 40, "9348")
    quota_gh = SpyGH()
    quota_results = []
    quota_rows = []
    server_module = PR._server()
    saved_cap = server_module._NEIGHBOR_REFRESH_CAP
    server_module._NEIGHBOR_REFRESH_CAP = 30
    try:
        for _ in range(3):
            quota_results.append(PR._drain_policy_refreshes(
                PR.PolicyRefreshStore(DSN_APP), quota_gh, DSN_APP, limit=1,
                graph_refresh_strict=lambda *a, **k: {
                    "terminal_degraded": "quota_paused", "reason": "free-tier limit"},
                refresh_inflight=lambda *a, **k: {"refreshed": list(entries)},
                post_refreshes=lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("ordinary poster must not run during quota pagination"))))
            quota_rows.append(graph_outbox(quota_acct, "9348"))
    finally:
        server_module._NEIGHBOR_REFRESH_CAP = saved_cap
    chk(
        [r.get("change_sliced") for r in quota_results] == [1, 1, 0]
        and quota_results[2].get("quota_deferred") == 1
        and all(
            not row["done"] and not row["claimed"] and row["attempts"] == 0
            and row["terminal_reason"] is None
            for row in quota_rows[:2])
        and quota_rows[0]["change_cursor"] == changes[29]
        and quota_rows[1]["change_cursor"] == changes[59]
        and quota_rows[2]["terminal_reason"] == "quota_paused"
        and quota_rows[2]["change_cursor"] == ""
        and len(quota_gh.comments) == 65 and len(quota_gh.checks) == 65,
        "65-PR quota advisory fan-out drains three pages before attempts-neutral defer and resets its cursor")


def store_db_connect_is_absolutely_bounded():
    clear_outbox()
    acct = provision_install("333")
    set_policy_as("333", "bounded", "yes")
    calls = []
    original = PR._db_connect.connect

    def spy(connector, dsn, **kwargs):
        calls.append(dict(kwargs))
        return original(connector, dsn, **kwargs)

    PR._db_connect.connect = spy
    try:
        store = PR.PolicyRefreshStore(DSN_APP, stale_seconds=1)
        store.depth()
        claimed = store.claim()
        store.fail_turn(claimed, "bounded_probe")
        set_policy_as("333", "bounded", "again")
        claimed2 = store.claim()
        store.finish_turn(claimed2)
    finally:
        PR._db_connect.connect = original
    chk(len(calls) >= 5 and all(
        isinstance(c.get("deadline"), float)
        and c.get("connect_timeout") == PR._STORE_CONNECT_TIMEOUT_SECONDS
        and "statement_timeout=" in c.get("options", "")
        and "lock_timeout=" in c.get("options", "")
        for c in calls),
        "depth/claim/fail/finish all share bounded DNS+libpq absolute connect deadlines and startup SQL timeouts")


def store_retry_delay_is_independent_of_stale_reclaim():
    calls = []

    class CaptureStore(PR.PolicyRefreshStore):
        def _one(self, sql, args=()):
            calls.append((sql, tuple(args)))
            return 1

    store = CaptureStore(
        "postgresql://unused", stale_seconds=300, retry_seconds=20,
        instance_id="retry-contract")
    row = {
        "account_id": "ACCT-GH-343", "request_kind": "policy",
        "repository_id": "", "branch": "", "policy_epoch": 9,
    }
    result = store.fail_turn(row, "retry_probe")
    chk(result == 1 and len(calls) == 1 and calls[0][1][-1] == 20
        and calls[0][1][-1] != store.stale_seconds,
        "fail_turn passes the dedicated 20s retry delay; 300s stale reclaim remains lease-only")
    rejected = False
    try:
        CaptureStore("postgresql://unused", max_attempts=4)
    except ValueError:
        rejected = True
    chk(rejected,
        "runtime refuses a max-attempt value other than five instead of drifting from SQL convergence counters")


def main() -> int:
    print("POLICY-CHANGE REFRESH GATE")
    boot = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if boot.returncode != 0:
        print("bootstrap failed:\n", boot.stderr[-1500:])
        return 1

    sql_enqueue_coalesce_and_rollback()
    sql_tenant_isolation_rls()
    sql_claim_finish_and_cas()
    sql_fail_retry_giveup()
    sql_generation_fence_and_owner_noop()
    sql_graph_identity_legacy_abi_and_supersede()
    sql_same_owner_rename_supersedes_graph_queue_coordinate()
    sql_live_legacy_graph_lease_is_immediately_superseded_by_onboarding()
    sql_fresh_onboarding_rename_uses_lifecycle_identity_without_graph()
    sql_onboarding_phase_cas_fences_installation_generation()
    sql_onboarding_plan_preservation_refreeze_and_fairness()
    sql_rolling_pre_schema_policy_terminal_bridge()
    sql_expired_policy_worker_is_fenced_and_reclaimed()
    sql_late_account_capacity_fence_and_tail()
    drain_tenant_scoped_and_seam()
    drain_remote_absence_waits_for_authoritative_offboard()
    drain_exact_installation_generation_routes_without_fleet_scan()
    drain_changed_installation_generation_has_zero_external_calls()
    drain_transient_unreachable_retries()
    drain_partial_install_map_miss_retries()
    drain_takes_repo_lock_seam()
    drain_repo_lock_serializes_with_live()
    external_mutations_are_exact_fenced_and_not_overtaken()
    external_mutation_scoped_replay_is_atomic()
    drain_onboarding_final_authority_refuses_stale_watching()
    drain_onboarding_quota_surfaces_claimless_plan_one_pr_per_turn()
    drain_no_open_prs_noop()
    drain_policy_one_repo_slices_and_supersede()
    drain_fail_then_slow_retry()
    drain_threads_graph_degraded_into_post()
    refresh_connections_close_before_external_calls()
    refresh_phase_reconnect_failure_is_retryable()
    drain_graph_success_failure_and_quota_defer()
    drain_change_fanout_is_durable_across_policy_graph_and_quota_pages()
    store_db_connect_is_absolutely_bounded()
    store_retry_delay_is_independent_of_stale_reclaim()

    print()
    if all(checks):
        print("POLICY-CHANGE REFRESH GATE: PASS")
        return 0
    print(f"POLICY-CHANGE REFRESH GATE: FAIL ({sum(not c for c in checks)} of {len(checks)} failed)")
    return 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    raise SystemExit(rc)
