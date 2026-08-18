"""G4 POLICY-CHANGE REFRESH — the durable-outbox DRAINER (a sibling of delivery_queue's recovery loop).

WHY THIS EXISTS. Policy values are read LIVE per evaluation (core._policy_int / _policy_text re-SELECT
core.policy every call), so a tenant's FUTURE evaluations already reflect a just-changed knob. But a Check
ALREADY POSTED on an OPEN PR reflects the policy at its LAST render — after the owner changes a tuning knob,
that open PR's posted Check stays STALE until the PR's next push/synchronize (hours / days / never). G4 closes
that gap: every canonical policy writer enqueues a content-free refresh row for its account IN THE SAME
TRANSACTION as the policy commit (core._enqueue_policy_refresh — see db/schema/97_policy_refresh.sql), and this
background drainer later re-derives that account's open PRs under the NEW policy.

THE DRAINER CONTRACT (mirrors delivery_queue.start_recovery_loop):
  * NO GitHub API call inside the enqueue/claim transaction. The store's claim/finish/fail each run on their
    OWN short-lived connection that commits and CLOSES immediately. Callback SQL also reconnects per statement.
    Clone/child work retains no backend; only the final bounded GitHub mutation section holds one session
    advisory-lock connection so a newer live event/worker cannot overtake it (never an outbox transaction).
  * TENANT-SCOPED. A claimed row names ONE account. Policy rows refresh that account's in-flight repos; graph
    rows durably name one stable repository id/default-branch/target-SHA even when that repo has no open PR.
  * GENERATION-FENCED. A claim snapshots the exact durable GitHub installation id + activation timestamp.
    The worker resolves that id directly (never by a fleet-wide partial installation list), checks the exact
    generation before work, and repeats that check under the shared account-lifecycle lock before every post.
  * GRAPH FIRST, THEN CHECKS. A graph row runs one strict, exact-target callback before recomputing and posting
    refreshes. Exact epoch+slot+lease checks fence every slow phase and terminal transition.
  * BOUNDED + FAIR. Per-tick drain limit; every policy account turn renders at most one repo, then attempts-neutral
    requeues its exact epoch at the scheduler tail. A failed turn consumes one bounded attempt.

KILL SWITCH: VERIPSA_POLICY_REFRESH=0 disables the loop (the enqueue rows simply accumulate, coalesced, until
re-enabled). GEN-AGNOSTIC: a tick against a DB that has not yet had the schema applied degrades to a logged
no-op (the store swallows undefined-table/function), so the loop never crashes a boot on the old schema.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
from contextlib import contextmanager

import psycopg2

try:
    from env_config import env_int
except ImportError:  # imported as a package
    from .env_config import env_int
try:
    import db_connect as _db_connect
except ImportError:  # imported as a package
    from . import db_connect as _db_connect


# ── knobs (validated ints; a typo'd value fails LOUD via env_int, naming the var) ─────────────────────────
_REFRESH_INTERVAL = env_int("VERIPSA_POLICY_REFRESH_INTERVAL", 15, min_value=1)
_MAX_ATTEMPTS     = env_int("VERIPSA_POLICY_REFRESH_MAX_ATTEMPTS", 5, min_value=1)
_STALE_SECONDS    = env_int(
    "VERIPSA_POLICY_REFRESH_STALE_SECONDS", 300, min_value=1, max_value=300)
_RETRY_SECONDS    = env_int("VERIPSA_POLICY_REFRESH_RETRY_SECONDS", 20, min_value=15, max_value=30)
_SCAN_CAP         = env_int("VERIPSA_POLICY_REFRESH_SCAN_CAP", 5000, min_value=1)
_DRAIN_LIMIT      = env_int("VERIPSA_POLICY_REFRESH_DRAIN_LIMIT", 50, min_value=1)
_COORD_CAP        = env_int("VERIPSA_POLICY_REFRESH_COORD_CAP", 200, min_value=1, max_value=100000)
# A policy turn deliberately renders ONE repository.  We fetch one look-ahead coordinate as a content-free
# `has_more` proof, then durably persist the processed coordinate before releasing the account lease to the
# scheduler tail.  This is intentionally not configurable: a large tenant must never monopolize one worker
# lane merely because an old/high environment value survived a deploy.
_POLICY_REPOS_PER_TURN = 1
_POLICY_COORD_LOOKAHEAD = _POLICY_REPOS_PER_TURN + 1
# Lease-protocol-v2 graph claims (independent of db/schema_generation) use exactly one per-account slot.
# Kept fixed with the SQL/table contract so two worker processes cannot both be occupied by one tenant;
# transitional five-argument workers are deliberately policy-only because they cannot terminalize a slot token.
_GRAPH_SLOTS_PER_ACCOUNT = 1
_QUOTA_DEFER_SECONDS = env_int(
    "VERIPSA_POLICY_REFRESH_QUOTA_DEFER_SECONDS", 900, min_value=60, max_value=86400)
# Bound the store's own DB session exactly like DeliveryStore (a wedged/contended op fails loud, never hangs
# the loop). ms; formatted as literals because Postgres SET takes no bind params.
_STORE_STMT_TIMEOUT_MS = env_int("VERIPSA_POLICY_REFRESH_STMT_TIMEOUT_MS", 30_000, min_value=1)
_STORE_LOCK_TIMEOUT_MS = env_int("VERIPSA_POLICY_REFRESH_LOCK_TIMEOUT_MS", 5_000, min_value=1)
_STORE_CONNECT_TIMEOUT_SECONDS = env_int(
    "VERIPSA_POLICY_REFRESH_CONNECT_TIMEOUT_SECONDS", 10, min_value=1, max_value=60)
# An upsert may need one bounded lookup plus one bounded PATCH/POST.  The
# complete worker turn is process-killed at 270s and the database lease at
# 300s; refusing below this fixed wall leaves margin for both logical calls.
_EXTERNAL_MUTATION_MIN_REMAINING_SECONDS = 70
# GitHub documents that REST processing is terminated at 10 seconds. Keep the
# same-repository session lock for a conservative extra window after an
# ambiguous client-side timeout/reset, so a request whose response was lost
# cannot remain remote-inflight when a newer writer enters.
_GITHUB_REMOTE_TERMINATION_HOLD_SECONDS = 12


def _bounded_connect(dsn: str):
    """Bound DNS + every libpq address attempt by one absolute deadline.

    Startup options arm statement/lock bounds before the first setup round-trip, so claim/depth/finish/fail and
    graph turns cannot wedge outside the worker monitor merely while opening a database connection."""
    return _db_connect.connect(
        psycopg2.connect,
        dsn,
        deadline=_db_connect.deadline_after(_STORE_CONNECT_TIMEOUT_SECONDS),
        connect_timeout=_STORE_CONNECT_TIMEOUT_SECONDS,
        options=(
            f"-c statement_timeout={int(_STORE_STMT_TIMEOUT_MS)} "
            f"-c lock_timeout={int(_STORE_LOCK_TIMEOUT_MS)} "
            "-c search_path=core"
        ),
    )


class AccountRouteUnavailable(RuntimeError):
    """The installation route disappeared between two independently bounded phases."""


class ExternalWriteAuthorityLost(RuntimeError):
    """The exact outbox lease cannot authorize another GitHub mutation."""


class _FreshAccountDB:
    """Tenant-pinned connect-per-statement runner for background convergence.

    Convergence callbacks deliberately mix cheap SQL with slow GitHub/clone/child work.  Holding even an
    autocommit session across those callbacks still consumes one scarce managed-Postgres backend for their whole
    wall time.  This runner reconnects, re-pins the live installation, executes one bounded statement, and closes
    before returning.  Therefore a callback may spend minutes outside PostgreSQL without leaving an idle backend
    behind.  Every later SQL statement also re-validates lifecycle authority after an offboard/reinstall race.
    """

    def __init__(self, dsn: str, account_key: str):
        self.dsn = dsn
        self.account_key = str(account_key or "")
        self.graph_bulk_loaded = False

    def __call__(self, sql: str, args=()):
        conn = _bounded_connect(self.dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SET statement_timeout = %s" % int(_STORE_STMT_TIMEOUT_MS))
                cur.execute("SET lock_timeout = %s" % int(_STORE_LOCK_TIMEOUT_MS))
                cur.execute(
                    "SELECT core.enter_existing_installation_with_authority(%s)",
                    (self.account_key,))
                route = cur.fetchone()
                if not route or not route[0]:
                    raise AccountRouteUnavailable("installation route is no longer live")
                cur.execute(sql, args)
                row = cur.fetchone() if cur.description else None
                value = row[0] if row else None
            normalized = str(sql or "").lower()
            if ("core.ingest_graph_with_authority" in normalized
                    or "core.patch_graph_with_authority" in normalized):
                self.graph_bulk_loaded = True
            return value
        finally:
            conn.close()


def _short_repo_barrier(dsn: str, account_key: str, repo: str, *,
                        take_repo_lock, release_repo_lock,
                        repository_id: str = "", take_repository_id_lock=None,
                        require_repository_live: bool = False) -> None:
    """Wait behind an already-running live repo event, then release every DB resource.

    This is a contention barrier, not a long-lived lock: it prevents convergence from starting in the middle of
    an event that already owns the coordinate, but the connection is closed before clone/GitHub work.  Exact
    request/lease CAS plus strict HEAD rechecks fence later races without pinning a Postgres backend.
    """
    conn = _bounded_connect(dsn)
    repo_lock_taken = False
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SET statement_timeout = %s" % int(_STORE_STMT_TIMEOUT_MS))
            cur.execute("SET lock_timeout = %s" % int(_STORE_LOCK_TIMEOUT_MS))
            cur.execute(
                "SELECT core.enter_existing_installation_with_authority(%s)",
                (str(account_key or ""),))
            route = cur.fetchone()
            if not route or not route[0]:
                raise AccountRouteUnavailable("installation route is no longer live")
            if repository_id and callable(take_repository_id_lock):
                take_repository_id_lock(cur, repository_id)
            take_repo_lock(cur, account_key, repo)
            repo_lock_taken = True
            if require_repository_live:
                cur.execute("SELECT core.assert_account_live_with_authority()")
                cur.fetchone()
                cur.execute(
                    "SELECT core.repository_event_allowed_with_authority(%s,%s)",
                    (repo, repository_id))
                allowed = cur.fetchone()
                if not allowed or not _db_bool(allowed[0]):
                    raise RepositoryLifecycleBlocked(
                        "repository lifecycle rejected graph refresh")
    finally:
        if repo_lock_taken:
            try:
                with conn.cursor() as cur:
                    release_repo_lock(cur, account_key, repo)
            except Exception:
                pass
        conn.close()


def _refresh_graph_stats_after_fresh_write(dsn: str, needed: bool) -> bool:
    """Refresh planner statistics on a fresh backend after a connect-per-statement graph write."""
    if not needed:
        return False
    conn = None
    try:
        conn = _bounded_connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core,pg_catalog")
            cur.execute("SET statement_timeout = %s" % int(_STORE_STMT_TIMEOUT_MS))
            cur.execute("CALL core.refresh_graph_stats()")
        return True
    except Exception as e:
        # The graph fact is already committed. Planner stats are a latency optimization, never authority to
        # replay or fail an otherwise correct convergence request.
        print(
            "post-ingest ANALYZE skipped "
            f"(graph already correct, plan stays cold): {str(e)[:160]}",
            flush=True)
        return False
    finally:
        if conn is not None:
            conn.close()


def _server():
    """The server module, resolved at CALL time (server.py imports the split modules, so a load-time import
    here would be circular). Used to reach the two EXISTING seams the refresh reuses — refresh_inflight (from
    webhook, re-exported on server) and _post_refreshes (from webhook_handlers, re-exported on server) — plus
    the single-connection DB runner _scoped_db. Mirrors event_processor._server."""
    try:
        import server as _s  # call-time import → no module-load circularity
    except ImportError:  # imported as a package
        from . import server as _s  # type: ignore
    return _s


def _as_jsonb(v):
    """A db() scalar that is a jsonb function result. psycopg2 adapts jsonb → dict/list; a str (no adapter) is
    also tolerated. Returns the parsed object, or None."""
    if v is None or isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


class PolicyRefreshStore:
    """The DB seam for the outbox — claim / finish / fail, each on its OWN short-lived autocommit connection
    (so no outbox transaction is ever held open across the drainer's GitHub work). Mirrors DeliveryStore's
    connect-per-op pattern. GEN-AGNOSTIC: an UndefinedTable/UndefinedFunction (a boot against a DB that has not
    yet had the G4 schema applied) is swallowed to a no-op — claim() returns None, finish()/fail() return
    falsey — so the loop degrades safely rather than crashing."""

    def __init__(self, dsn: str, *, max_attempts: int = _MAX_ATTEMPTS, stale_seconds: int = _STALE_SECONDS,
                 retry_seconds: int = _RETRY_SECONDS, scan_cap: int = _SCAN_CAP,
                 instance_id: str | None = None):
        self.dsn = dsn
        self.max_attempts = int(max_attempts)
        if self.max_attempts != 5:
            raise ValueError(
                "VERIPSA_POLICY_REFRESH_MAX_ATTEMPTS must be 5 "
                "(database convergence counters use the same fixed contract)")
        self.stale_seconds = int(stale_seconds)
        self.retry_seconds = int(retry_seconds)
        self.scan_cap = int(scan_cap)
        import uuid
        # Per-BOOT worker identity (fresh every process start — never hostname), stamped on each claimed row.
        self.instance_id = instance_id or ("pr-" + uuid.uuid4().hex)

    def _one(self, sql: str, args=()):
        conn = _bounded_connect(self.dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SET statement_timeout = %s" % int(_STORE_STMT_TIMEOUT_MS))
                cur.execute("SET lock_timeout = %s" % int(_STORE_LOCK_TIMEOUT_MS))
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()   # the claim/finish/fail transaction is CLOSED before any GitHub work begins

    def claim(self) -> dict | None:
        """Claim one pending outbox row for a live-installation account, or None when nothing is pending. The
        connection is closed on return, so the claim's transaction is not held during the refresh."""
        try:
            return _as_jsonb(self._one(
                "SELECT core.claim_policy_refresh_with_authority(%s,%s,%s,%s,%s,%s,%s)",
                (self.instance_id, self.max_attempts, self.stale_seconds, self.scan_cap,
                 True, _GRAPH_SLOTS_PER_ACCOUNT, True)))
        except psycopg2.errors.UndefinedTable:
            return None
        except psycopg2.errors.UndefinedFunction:
            return None

    @staticmethod
    def _identity(row: dict) -> tuple[str, str, str, str, int]:
        return (
            str(row.get("account_id") or ""),
            str(row.get("request_kind") or "policy"),
            str(row.get("repository_id") or ""),
            str(row.get("branch") or ""),
            int(row.get("request_epoch", row.get("policy_epoch"))),
        )

    @staticmethod
    def _graph_identity(row: dict) -> tuple[str, str, str, str, int, int, int]:
        account, kind, repository_id, branch, request_epoch = PolicyRefreshStore._identity(row)
        if kind != "graph":
            raise ValueError("graph lease identity requested for a policy turn")
        slot = int(row.get("graph_slot"))
        lease_epoch = int(row.get("lease_epoch"))
        if slot != 1 or lease_epoch < 1:
            raise ValueError("malformed graph lease identity")
        return account, kind, repository_id, branch, request_epoch, slot, lease_epoch

    def finish_turn(self, row: dict) -> dict:
        try:
            if str(row.get("request_kind") or "policy") == "graph":
                sql = (
                    "SELECT core.finish_policy_refresh_turn_with_authority("
                    "%s,%s,%s,%s,%s,%s::smallint,%s)")
                args = self._graph_identity(row)
            else:
                sql = "SELECT core.finish_policy_refresh_turn_with_authority(%s,%s,%s,%s,%s)"
                args = self._identity(row)
            return _as_jsonb(self._one(sql, args)) or {}
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedFunction):
            return {}

    def fail_turn(self, row: dict, error: str) -> int:
        try:
            if str(row.get("request_kind") or "policy") == "graph":
                sql = (
                    "SELECT core.fail_policy_refresh_turn_with_authority("
                    "%s,%s,%s,%s,%s,%s::smallint,%s,%s,%s)")
                args = (
                    *self._graph_identity(row),
                    str(error or "")[:200],
                    self.retry_seconds,
                )
            else:
                sql = (
                    "SELECT core.fail_policy_refresh_turn_with_authority("
                    "%s,%s,%s,%s,%s,%s,%s)")
                args = (
                    *self._identity(row),
                    str(error or "")[:200],
                    self.retry_seconds,
                )
            v = self._one(sql, args)
            return int(v) if v is not None else -1
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedFunction):
            return -1

    def requeue_policy_slice(self, row: dict, after_repo: str, after_branch: str) -> dict:
        """Attempts-neutral exact-epoch continuation for a policy sentinel.

        SQL persists the lexicographic coordinate cursor and moves this SAME epoch to the account scheduler
        tail.  A superseding policy write clears the cursor and bumps the epoch; this exact CAS therefore cannot
        overwrite or consume the newer request."""
        account, kind, repository_id, branch, epoch = self._identity(row)
        if kind != "policy" or repository_id or branch:
            return {}
        try:
            return _as_jsonb(self._one(
                "SELECT core.requeue_policy_refresh_slice_with_authority(%s,%s,%s,%s)",
                (account, epoch, str(after_repo or ""), str(after_branch or "")))) or {}
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedFunction):
            return {}

    def requeue_policy_page(self, row: dict, repo: str, branch: str,
                            change_cursor: str, *, repo_complete: bool) -> dict:
        """Persist either a completed PR page within one repo or the completed repo itself."""
        account, kind, repository_id, identity_branch, epoch = self._identity(row)
        if kind != "policy" or repository_id or identity_branch:
            return {}
        try:
            return _as_jsonb(self._one(
                "SELECT core.requeue_policy_refresh_page_with_authority("
                "%s,%s,%s,%s,%s,%s)",
                (account, epoch, str(repo or ""), str(branch or ""),
                 str(change_cursor or ""), bool(repo_complete)))) or {}
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedFunction):
            return {}

    def requeue_graph_page(self, row: dict, change_cursor: str) -> dict:
        account, _kind, repository_id, branch, request_epoch, slot, lease_epoch = (
            self._graph_identity(row))
        try:
            return _as_jsonb(self._one(
                "SELECT core.requeue_graph_refresh_page_with_authority("
                "%s,%s,%s,%s,%s::smallint,%s,%s)",
                (account, repository_id, branch, request_epoch, slot, lease_epoch,
                 str(change_cursor or "")))) or {}
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedFunction):
            return {}

    def requeue_graph_onboarding(
            self, row: dict, action: str, *, plan: list[int] | None = None,
            truncated: bool = False, expected_index: int = 0,
            pr_number: int = 0, watching_done: bool = False) -> dict:
        """Exact attempts-neutral onboarding phase transition on the graph row."""
        account, _kind, repository_id, branch, request_epoch, slot, lease_epoch = (
            self._graph_identity(row))
        generation = _claimed_installation_generation(row)
        if generation is None:
            return {}
        installation_id, installation_created_at = generation
        try:
            return _as_jsonb(self._one(
                "SELECT core.requeue_graph_onboarding_with_authority("
                "%s,%s,%s,%s,%s::smallint,%s,%s,%s::timestamptz,"
                "%s,%s::bigint[],%s,%s,%s,%s)",
                (
                    account, repository_id, branch, request_epoch, slot, lease_epoch,
                    installation_id, installation_created_at,
                    str(action or ""), plan, bool(truncated), int(expected_index),
                    int(pr_number), bool(watching_done),
                ))) or {}
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedFunction):
            return {}

    def resolve_graph_onboarding_head(
            self, row: dict, branch: str | None, target_sha: str | None) -> dict:
        account, _kind, repository_id, identity_branch, request_epoch, slot, lease_epoch = (
            self._graph_identity(row))
        generation = _claimed_installation_generation(row)
        if generation is None:
            return {}
        installation_id, installation_created_at = generation
        try:
            return _as_jsonb(self._one(
                "SELECT core.resolve_graph_onboarding_head_with_authority("
                "%s,%s,%s,%s,%s::smallint,%s,%s,%s::timestamptz,%s,%s)",
                (
                    account, repository_id, identity_branch, request_epoch, slot,
                    lease_epoch, installation_id, installation_created_at,
                    branch, target_sha,
                ))) or {}
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedFunction):
            return {}

    def defer_graph_turn(self, row: dict, reason: str = "quota_paused",
                         delay_seconds: int = _QUOTA_DEFER_SECONDS) -> dict:
        account, kind, repository_id, branch, request_epoch, slot, lease_epoch = (
            self._graph_identity(row))
        try:
            return _as_jsonb(self._one(
                "SELECT core.defer_graph_refresh_turn_with_authority("
                "%s,%s,%s,%s,%s::smallint,%s,%s,%s)",
                (account, repository_id, branch, request_epoch, slot, lease_epoch,
                 reason, int(delay_seconds)))) or {}
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedFunction):
            return {}

    def release_superseded(self, row: dict) -> bool:
        try:
            if str(row.get("request_kind") or "policy") == "graph":
                account, _kind, _repository_id, _branch, request_epoch, slot, lease_epoch = (
                    self._graph_identity(row))
                sql = (
                    "SELECT core.release_superseded_convergence_lease_with_authority("
                    "%s,%s,%s::smallint,%s)")
                args = (account, request_epoch, slot, lease_epoch)
            else:
                account, _kind, _repository_id, _branch, epoch = self._identity(row)
                sql = (
                    "SELECT core.release_superseded_convergence_lease_with_authority("
                    "%s,%s)")
                args = (account, epoch)
            return bool(self._one(sql, args))
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedFunction):
            return False

    def depth(self) -> dict:
        try:
            return _as_jsonb(self._one(
                "SELECT core.account_convergence_depth_with_authority()")) or {}
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedFunction):
            return {}

    def turn_is_current(self, row: dict) -> bool:
        try:
            if str(row.get("request_kind") or "policy") == "graph":
                sql = (
                    "SELECT core.policy_refresh_turn_is_current_with_authority("
                    "%s,%s,%s,%s,%s,%s::smallint,%s)")
                args = self._graph_identity(row)
            else:
                sql = (
                    "SELECT core.policy_refresh_turn_is_current_with_authority("
                    "%s,%s,%s,%s,%s)")
                args = self._identity(row)
            return bool(self._one(sql, args))
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedFunction):
            return False

    def installation_generation_is_current(
        self, row: dict
    ) -> bool | None:
        """Exact durable install generation, or None for rolling legacy rows."""

        generation = _claimed_installation_generation(row)
        if generation is None:
            return None
        installation_id, created_at = generation
        try:
            return bool(self._one(
                "SELECT "
                "core.policy_refresh_install_generation_current_with_authority("
                "%s,%s,%s::timestamptz)",
                (
                    str(row.get("account_id") or ""),
                    installation_id,
                    created_at,
                ),
            ))
        except (
            psycopg2.errors.UndefinedTable,
            psycopg2.errors.UndefinedFunction,
        ):
            return False

    # Compatibility helpers for callers/tests that still explicitly operate on the policy sentinel.
    def finish(self, account_id: str, policy_epoch: int) -> bool:
        row = {"account_id": account_id, "request_kind": "policy", "repository_id": "",
               "branch": "", "policy_epoch": policy_epoch}
        return self.finish_turn(row).get("finished") is True

    def fail(self, account_id: str, policy_epoch: int, error: str) -> int:
        row = {"account_id": account_id, "request_kind": "policy", "repository_id": "",
               "branch": "", "policy_epoch": policy_epoch}
        return self.fail_turn(row, error)


def _bounded_error_code(exc: Exception) -> str:
    """A bounded content-free class code. Exception text can contain a repo/path/remote response and is never
    persisted in the outbox."""
    return ("exception_" + type(exc).__name__.lower())[:80]


class GraphRefreshHookUnavailable(RuntimeError):
    pass


class GraphRefreshContractError(RuntimeError):
    pass


class RepositoryLifecycleBlocked(RuntimeError):
    pass


def _db_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in ("t", "true", "1")


def _claimed_installation_generation(
    row: dict,
) -> tuple[str, str] | None:
    installation_id = row.get("github_installation_id")
    created_at = row.get("github_installation_created_at")
    if (
        isinstance(installation_id, str)
        and installation_id.isascii()
        and installation_id.isdigit()
        and not installation_id.startswith("0")
        and len(installation_id) <= 64
        and created_at is not None
    ):
        created = str(created_at).strip()
        if (
            created
            and len(created) <= 64
            and "\n" not in created
            and "\r" not in created
        ):
            return installation_id, created
    return None


def _normalize_post_progress(value, entries: list, after_change: str = "") -> dict:
    """Normalize the opt-in background poster contract while preserving injectable legacy int test seams."""
    if isinstance(value, dict):
        return {
            "posted": int(value.get("posted") or 0),
            "processed": int(value.get("processed") or 0),
            "cursor": str(value.get("cursor") or after_change or ""),
            "has_more": value.get("has_more") is True,
            "errors": int(value.get("errors") or 0),
        }
    changes = sorted(
        entry["change"]
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict)
        and isinstance(entry.get("change"), str)
        and entry["change"] > str(after_change or ""))
    return {
        "posted": int(value or 0),
        "processed": len(changes),
        "cursor": changes[-1] if changes else str(after_change or ""),
        "has_more": False,
        "errors": 0,
    }


def _resolve_repo_lock_fns(take_repo_lock, release_repo_lock):
    """Default the per-(account,repo) advisory-lock fns to the SAME ones the live webhook worker uses
    (server_dbops), so the drainer and a concurrent live event contend on the IDENTICAL lock key. Lazily
    imported (server_dbops is a leaf — no cycle); injectable so a test can spy/observe the lock calls."""
    if take_repo_lock is not None and release_repo_lock is not None:
        return take_repo_lock, release_repo_lock
    try:
        from server_dbops import _take_repo_lock as _trl, _release_repo_lock as _rrl
    except ImportError:  # imported as a package
        from .server_dbops import _take_repo_lock as _trl, _release_repo_lock as _rrl
    return (take_repo_lock or _trl), (release_repo_lock or _rrl)


def _resolve_repository_id_lock_fn(take_repository_id_lock):
    if take_repository_id_lock is not None:
        return take_repository_id_lock
    try:
        from server_dbops import _take_repository_id_lock as _tril
    except ImportError:  # imported as a package
        from .server_dbops import _take_repository_id_lock as _tril
    return _tril


class _LeaseFencedGitHub:
    """Delegate reads, but exact-fence every potentially mutating high-level call."""

    _MUTATIONS = frozenset({
        "upsert_check",
        "upsert_comment",
        "patch_comment_if_exists",
        "remove_label",
        "post_check",
        "patch_check",
        "post_comment",
        "patch_comment",
    })

    def __init__(self, gh, assert_current):
        self._gh = gh
        self._assert_current = assert_current

    def __getattr__(self, name):
        value = getattr(self._gh, name)
        if name not in self._MUTATIONS or not callable(value):
            return value

        def _fenced(*args, **kwargs):
            self._assert_current()
            try:
                # GitHubREST normally budgets each nested _api call. Opening
                # one outer logical scope makes an upsert (GET/list + one
                # PATCH/POST) share a single 30s wall, which is required by
                # the 70s exact-lease admission bound.
                try:
                    from github_rest import _logical_call_scope
                except ImportError:  # imported as a package
                    from .github_rest import _logical_call_scope
                with _logical_call_scope():
                    return value(*args, **kwargs)
            except Exception as exc:
                # An explicit HTTP response is definitive. A timeout/reset is
                # ambiguous: GitHub may have received the body even though we
                # lost the response. GitHub's documented 10s server processing
                # ceiling makes a 12s lock hold a bounded remote completion
                # fence before another worker/live event can post.
                explicit_http = isinstance(exc, urllib.error.HTTPError)
                ambiguous = (
                    isinstance(exc, (TimeoutError, ConnectionError, OSError))
                    and not explicit_http
                )
                if ambiguous:
                    time.sleep(_GITHUB_REMOTE_TERMINATION_HOLD_SECONDS)
                raise

        return _fenced


@contextmanager
def _external_mutation_authority(
        dsn: str, gh_for, account_key: str, repo: str, row: dict, *,
        take_repo_lock, release_repo_lock, take_repository_id_lock=None,
        yield_scoped_db: bool = False):
    """Hold the live-event repository lock across the bounded mutation phase.

    The lock is acquired before the exact remaining-lease fence.  A new live
    event or reclaimed convergence worker therefore cannot overtake a delayed
    old Check/comment POST.  The one retained backend is bounded and exists
    only for this external mutation section, never for clone/extraction.
    """
    conn = _bounded_connect(dsn)
    repo_lock_taken = False
    lifecycle_account = None
    lifecycle_lock_taken = False
    scoped_transaction = False
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SET statement_timeout = %s" % int(_STORE_STMT_TIMEOUT_MS))
            cur.execute("SET lock_timeout = %s" % int(_STORE_LOCK_TIMEOUT_MS))
            cur.execute(
                "SELECT core.enter_existing_installation_with_authority(%s)",
                (str(account_key or ""),))
            route = cur.fetchone()
            if not route or not route[0]:
                raise AccountRouteUnavailable("installation route is no longer live")
            lifecycle_account = str(route[0])
            kind = str(row.get("request_kind") or "policy")
            if kind == "graph":
                repository_id = str(row.get("repository_id") or "")
                if callable(take_repository_id_lock):
                    take_repository_id_lock(cur, repository_id)
            take_repo_lock(cur, account_key, repo)
            repo_lock_taken = True
            # The session-level shared fence spans the bounded external
            # mutation, unlike the transaction helper used by one-statement
            # DB writers.  Destructive/replacement lifecycle work takes this
            # key exclusively.  Lock order remains stable repository id →
            # mutable repository coordinate → account lifecycle.
            cur.execute(
                "SELECT pg_advisory_lock_shared("
                "hashtext('core.account_lifecycle'),hashtext(%s))",
                (lifecycle_account,),
            )
            lifecycle_lock_taken = True

        if yield_scoped_db:
            # A planned PR replay routes through the ordinary live PR handler. That handler deliberately makes
            # several coupled writes (BR release, PR claims, reconciliation, exact-head stamp) and uses real
            # SAVEPOINTs for its optional reads. Reusing this locked backend in autocommit mode would make every
            # statement permanent independently: a later DB/GitHub failure could leave half a PR replay committed
            # while its durable plan cursor correctly stayed put. Open one explicit transaction only for this
            # bounded replay/final-authority turn. The context commits after the caller has validated its exact
            # receipt, and rolls every DB mutation back on any exception. Session advisory locks remain held across
            # the transaction and the idempotent GitHub writes, matching the live-event atomicity contract.
            conn.autocommit = False
            scoped_transaction = True

        def _assert_current() -> None:
            with conn.cursor() as cur:
                generation = _claimed_installation_generation(row)
                if generation is not None:
                    installation_id, created_at = generation
                    cur.execute(
                        "SELECT "
                        "core.policy_refresh_install_generation_current_with_authority("
                        "%s,%s,%s::timestamptz)",
                        (
                            str(row.get("account_id") or ""),
                            installation_id,
                            created_at,
                        ),
                    )
                    generation_current = cur.fetchone()
                    if (
                        not generation_current
                        or not _db_bool(generation_current[0])
                    ):
                        raise ExternalWriteAuthorityLost(
                            "installation generation cannot authorize "
                            "another external mutation")
                if str(row.get("request_kind") or "policy") == "graph":
                    sql = (
                        "SELECT core.policy_refresh_external_write_fence_with_authority("
                        "%s,%s,%s,%s,%s,%s::smallint,%s,%s)")
                    args = (
                        *PolicyRefreshStore._graph_identity(row),
                        _EXTERNAL_MUTATION_MIN_REMAINING_SECONDS,
                    )
                else:
                    sql = (
                        "SELECT core.policy_refresh_external_write_fence_with_authority("
                        "%s,%s,%s,%s,%s,%s)")
                    args = (
                        *PolicyRefreshStore._identity(row),
                        _EXTERNAL_MUTATION_MIN_REMAINING_SECONDS,
                    )
                cur.execute(sql, args)
                current = cur.fetchone()
            if not current or not _db_bool(current[0]):
                raise ExternalWriteAuthorityLost(
                    "convergence lease cannot authorize another external mutation")

        def _scoped_db(sql: str, args=()):
            # Synthetic onboarding replay must reuse this exact backend. Opening `_FreshAccountDB` while this
            # session holds repository/account advisory locks can wait on itself and recreate the observed convoy.
            # The exact generation+lease fence is refreshed before every statement, just as it is before every
            # externally visible GitHub mutation. Transaction-control statements are the sole exception: after a
            # best-effort query aborts PostgreSQL's transaction, ROLLBACK TO SAVEPOINT must execute before any
            # fence query can run again. Accept only the fixed, identifier-only command shapes used by the live
            # handler; arbitrary/multi-statement SQL never bypasses the fence.
            tokens = str(sql or "").strip().split()
            upper = [token.upper() for token in tokens]
            savepoint_name = tokens[-1] if tokens else ""
            valid_savepoint_name = (
                bool(savepoint_name)
                and (savepoint_name[0].isalpha() or savepoint_name[0] == "_")
                and all(char.isalnum() or char == "_" for char in savepoint_name)
            )
            transaction_control = valid_savepoint_name and (
                (len(upper) == 2 and upper[0] == "SAVEPOINT")
                or (len(upper) == 3 and upper[:2] == ["RELEASE", "SAVEPOINT"])
                or (len(upper) == 4 and upper[:3] == ["ROLLBACK", "TO", "SAVEPOINT"])
            )
            if not transaction_control:
                _assert_current()
            with conn.cursor() as cur:
                cur.execute(sql, args)
                one = cur.fetchone()
                return one[0] if one else None

        try:
            # Zero external calls when lock wait consumed the remaining lease.
            _assert_current()
            fenced = _LeaseFencedGitHub(gh_for, _assert_current)
            yield (fenced, _scoped_db) if yield_scoped_db else fenced
        except BaseException:
            if scoped_transaction:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        else:
            if scoped_transaction:
                try:
                    conn.commit()
                except Exception:
                    # A failed commit is ambiguous for the remote idempotent surface but must never leave a local
                    # partial transaction behind. The durable cursor is advanced only after this context returns,
                    # so the whole exact PR turn will retry safely.
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    raise
    finally:
        if scoped_transaction:
            try:
                # Unlock statements should not start a throwaway implicit transaction. If the connection broke,
                # close() below still releases every session advisory lock.
                conn.autocommit = True
            except Exception:
                pass
        if lifecycle_lock_taken:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock_shared("
                        "hashtext('core.account_lifecycle'),hashtext(%s))",
                        (lifecycle_account,),
                    )
            except Exception:
                pass
        if repo_lock_taken:
            try:
                with conn.cursor() as cur:
                    release_repo_lock(cur, account_key, repo)
            except Exception:
                pass
        conn.close()


def _post_quota_paused_refreshes(gh_for, repo: str, branch: str, refreshes: list,
                                 *, trace: str = "", after_change: str = "") -> dict:
    """Idempotently replace every bounded in-flight PR surface with the existing fair-use advisory.

    This is deliberately independent of graph impact: quota prevented the graph fact from converging, so posting
    an ordinary verdict (especially Clear) would be dishonest. A failed per-PR API call is counted but does not
    convert quota into the ordinary finite retry budget; the durable quota defer will offer the row again."""
    try:
        from render import quota_paused_check, quota_paused_comment_body
        from webhook_coercion import _marked_comment, _comment_marker, _pr_number_from_change
        from webhook_posters import _head_and_fork, _upsert_check_result
    except ImportError:  # imported as a package
        from .render import quota_paused_check, quota_paused_comment_body
        from .webhook_coercion import _marked_comment, _comment_marker, _pr_number_from_change
        from .webhook_posters import _head_and_fork, _upsert_check_result

    cap = max(1, int(getattr(_server(), "_NEIGHBOR_REFRESH_CAP", 30)))
    check = quota_paused_check()
    cursor = str(after_change or "")
    candidates = sorted(
        (
            entry for entry in (refreshes if isinstance(refreshes, list) else [])
            if isinstance(entry, dict)
            and isinstance(entry.get("change"), str)
            and _pr_number_from_change(entry.get("change"))
            and entry["change"] > cursor
        ),
        key=lambda entry: entry["change"],
    )
    processed = posted = errors = 0
    for entry in candidates:
        if processed >= cap:
            break
        pr = _pr_number_from_change(entry.get("change") if isinstance(entry, dict) else None)
        if not pr:
            continue
        processed += 1
        try:
            head_sha, is_fork, _redact_external = _head_and_fork(gh_for, repo, pr)
            comment_id = None
            resp = gh_for.upsert_comment(
                repo, pr, _comment_marker(pr),
                _marked_comment(pr, quota_paused_comment_body(branch)))
            if isinstance(resp, dict) and isinstance(resp.get("id"), int):
                comment_id = resp["id"]
            check_meta = _upsert_check_result(
                gh_for, repo, head_sha, check["conclusion"], check["title"], check["summary"],
                is_fork, pr_number=pr, comment_id=comment_id)
            # A fork head can legitimately be unavailable to the base-repo Check API; its idempotent PR comment is
            # still the authoritative visible fair-use surface. A same-repo Check failure is retried on the defer.
            if not is_fork and check_meta.get("posted") is not True:
                errors += 1
                break
            posted += 1
            cursor = entry["change"]
        except Exception as e:
            errors += 1
            print(
                f"trace_id={trace[:16]} quota-deferred surface skipped "
                f"repo={repo} pr={pr} error_code={_bounded_error_code(e)}",
                flush=True)
            break
    return {
        "processed": processed,
        "posted": posted,
        "errors": errors,
        "cursor": cursor,
        "has_more": bool(errors or processed < len(candidates)),
    }


def refresh_account(dsn: str, gh_for, account_key: str, *, coord_cap: int = _COORD_CAP,
                    after_repo: str = "", after_branch: str = "", after_change: str = "",
                    refresh_inflight=None, post_refreshes=None, graph_freshness=None, trace: str = "",
                    take_repo_lock=None, release_repo_lock=None,
                    turn_row: dict | None = None) -> dict:
    """Refresh ONE account's open in-flight PRs, OUTSIDE any outbox transaction. `gh_for` is the account-scoped
    GitHub client (gh.for_account(account_id)); `account_key` is the bare owner id used to pin the tenant route.

    A connect-per-statement runner re-pins lifecycle authority for every SQL call and closes before returning.
    The production durable turn then holds one bounded session advisory-lock connection across the final evidence
    snapshot and GitHub mutations, without retaining a DB transaction. Exact lease fences before each mutation
    prevent stale writes after reclaim. Returns a content-free summary with
    `has_more` and the processed cursor; the caller durably requeues that exact epoch at the scheduler tail."""
    _s = _server()
    refresh_inflight = refresh_inflight or _s.refresh_inflight
    post_refreshes = post_refreshes or _s._post_refreshes
    graph_freshness = graph_freshness or _s.graph_freshness
    take_repo_lock, release_repo_lock = _resolve_repo_lock_fns(take_repo_lock, release_repo_lock)
    db = _FreshAccountDB(dsn, account_key)
    try:
        # `coord_cap` remains in the public Python signature for rolling callers, but policy monopolization is
        # prevented by construction: SQL returns at most one work coordinate plus one look-ahead proof.
        coords = _as_jsonb(db(
            "SELECT core.account_inflight_refresh_coordinates_after_with_authority(%s,%s,%s)",
            (str(after_repo or ""), str(after_branch or ""), _POLICY_COORD_LOOKAHEAD)))
    except AccountRouteUnavailable:
        return {"account_key": account_key, "skipped": "no_live_route", "repos": 0, "posted": 0}
    coords = coords if isinstance(coords, list) else []

    repos = 0
    posted = 0
    errors = 0
    change_cursor = str(after_change or "")
    change_has_more = False
    processed_repo = ""
    processed_branch = ""
    for c in coords[:_POLICY_REPOS_PER_TURN]:
        if not isinstance(c, dict):
            continue
        repo = c.get("repo")
        branch = c.get("branch") or "main"
        if not isinstance(repo, str) or not repo:
            continue
        repos += 1
        processed_repo = repo
        processed_branch = branch
        try:
            # Wait for any already-running live event, then close the barrier connection before all callbacks.
            _short_repo_barrier(
                dsn, account_key, repo,
                take_repo_lock=take_repo_lock, release_repo_lock=release_repo_lock)
            # Production durable turns retain the identical live-event repo
            # lock across the evidence snapshot AND external mutation. A live
            # event can therefore occur wholly before or wholly after this
            # refresh; stale evidence can never overtake its newer surface.
            # Unit/rolling callers without a token retain the historical path.
            if isinstance(turn_row, dict):
                authority = _external_mutation_authority(
                    dsn, gh_for, account_key, repo, turn_row,
                    take_repo_lock=take_repo_lock,
                    release_repo_lock=release_repo_lock)
            else:
                @contextmanager
                def authority():
                    db("SELECT core.assert_account_live_with_authority()")
                    yield gh_for
                authority = authority()
            with authority as fenced_gh:
                refreshed = refresh_inflight(db, repo, branch)
                entries = refreshed.get("refreshed", []) if isinstance(refreshed, dict) else []
                # FALSE-CLEAR PARITY: a policy refresh that would reset a PR
                # to green must be withheld under a degraded graph.
                policy_graph_degraded = True
                try:
                    freshness = graph_freshness(db, fenced_gh, repo, branch)
                    policy_graph_degraded = (
                        not isinstance(freshness, dict)
                        or freshness.get("behind") is not False)
                except Exception as e:
                    print(
                        f"policy-refresh freshness read skipped repo={repo}: "
                        f"{str(e)[:120]}",
                        flush=True,
                    )
                    policy_graph_degraded = True
                progress = _normalize_post_progress(
                    post_refreshes(
                        fenced_gh, repo, entries, db=db, branch=branch,
                        trace_id=trace, delivery="",
                        graph_degraded=policy_graph_degraded,
                        return_progress=True, after_change=change_cursor),
                    entries, change_cursor)
            posted += int(progress["posted"])
            errors += int(progress["errors"])
            change_cursor = str(progress["cursor"] or change_cursor)
            change_has_more = progress["has_more"] is True
        except Exception as e:  # per-repo isolation — a lock/reconnect/GitHub error retains the exact coordinate
            errors += 1
            print(f"policy-refresh repo skipped account={account_key} repo={repo}: "
                  f"{str(e)[:140]}", flush=True)
    return {
        "account_key": account_key,
        "repos": repos,
        "posted": posted,
        "errors": errors,
        "has_more": bool(
            not errors and not change_has_more and processed_repo
            and len(coords) > _POLICY_REPOS_PER_TURN),
        "change_has_more": bool(not errors and change_has_more),
        "change_cursor": change_cursor,
        "cursor_repo": processed_repo,
        "cursor_branch": processed_branch,
    }


def refresh_graph_turn(dsn: str, gh_for, account_key: str, row: dict, *,
                       graph_refresh_strict, refresh_inflight=None, post_refreshes=None,
                       resolve_onboarding_head=None, build_onboarding_plan=None,
                       replay_onboarding_pr=None,
                       surface_onboarding_quota_paused_pr=None,
                       trace: str = "", take_repository_id_lock=None,
                       take_repo_lock=None, release_repo_lock=None) -> dict:
    """Run one durable graph coordinate.

    The strict callback contract is:
      graph_refresh_strict(db, gh_for, repo, branch, target_sha, repository_id,
                           expected_owner_id=<installation owner>, trace_id=<content-free trace>)
        -> {"converged": True, "ingest": {...}} after persisting that exact target/current extractor, OR
           {"superseded": True} after atomically re-enqueuing the authoritative newer HEAD.

    Any other return is a retryable contract failure. Every DB statement uses a fresh, tenant-pinned, bounded
    connection which is closed before returning to the callback. Clone/child extraction retains no PostgreSQL
    backend. The bounded final evidence/Watching/post section holds one session advisory-lock connection and
    rechecks exact request+slot+lease authority before every mutation, so a reclaimed worker cannot be overtaken."""
    repo = row.get("repo")
    branch = row.get("branch")
    target_sha = row.get("target_sha")
    repository_id = row.get("repository_id")
    if not all(isinstance(v, str) and v for v in (repo, branch, target_sha, repository_id)):
        raise GraphRefreshContractError("malformed graph refresh row")
    after_change = str(row.get("change_cursor") or "")
    onboarding_pending = row.get("onboarding_pending") is True
    onboarding_head_pending = row.get("onboarding_head_pending") is True
    onboarding_plan = row.get("onboarding_plan")
    onboarding_index = int(row.get("onboarding_index") or 0)
    onboarding_watching_done = row.get("onboarding_watching_done") is True
    terminal_reason = row.get("terminal_reason")
    if terminal_reason not in (None, "quota_paused"):
        raise GraphRefreshContractError("malformed graph terminal reason")
    sentinel = branch == "__veripsa_onboarding_head__" and target_sha == "0000000"
    if onboarding_head_pending:
        if (not onboarding_pending or onboarding_plan is not None or onboarding_index != 0
                or onboarding_watching_done or row.get("onboarding_truncated") is True):
            raise GraphRefreshContractError("malformed onboarding HEAD phase")
    elif sentinel:
        raise GraphRefreshContractError("reserved onboarding coordinate escaped its HEAD phase")
    if onboarding_pending and not onboarding_head_pending:
        if onboarding_plan is not None:
            if (not isinstance(onboarding_plan, list) or len(onboarding_plan) > 300
                    or onboarding_index < 0 or onboarding_index > len(onboarding_plan)
                    or any(
                        not isinstance(number, int) or isinstance(number, bool) or number < 1
                        for number in onboarding_plan)
                    or onboarding_plan != sorted(set(onboarding_plan))):
                raise GraphRefreshContractError("malformed onboarding plan")
        elif (onboarding_index != 0 or onboarding_watching_done
                or row.get("onboarding_truncated") is True):
            raise GraphRefreshContractError("unfrozen onboarding plan carried progress")
    elif not onboarding_pending and (
            onboarding_plan is not None or onboarding_index != 0
            or onboarding_watching_done or row.get("onboarding_truncated") is True):
        raise GraphRefreshContractError("onboarding state leaked onto an ordinary graph row")
    quota_surface_phase = (
        onboarding_pending
        and onboarding_plan is not None
        and terminal_reason == "quota_paused"
        and row.get("not_before") is None
    )

    _s = _server()
    refresh_inflight = refresh_inflight or _s.refresh_inflight
    post_refreshes = post_refreshes or _s._post_refreshes
    resolve_onboarding_head = resolve_onboarding_head or _s.resolve_repository_onboarding_head
    build_onboarding_plan = build_onboarding_plan or _s.build_open_pr_backfill_plan
    replay_onboarding_pr = replay_onboarding_pr or _s.replay_onboarding_pull_request
    surface_onboarding_quota_paused_pr = (
        surface_onboarding_quota_paused_pr
        or _s.surface_onboarding_quota_paused_pull_request
    )
    take_repo_lock, release_repo_lock = _resolve_repo_lock_fns(take_repo_lock, release_repo_lock)
    take_repository_id_lock = _resolve_repository_id_lock_fn(take_repository_id_lock)
    db = _FreshAccountDB(dsn, account_key)
    try:
        _short_repo_barrier(
            dsn, account_key, repo,
            take_repo_lock=take_repo_lock, release_repo_lock=release_repo_lock,
            repository_id=repository_id,
            take_repository_id_lock=take_repository_id_lock,
            require_repository_live=True)
    except AccountRouteUnavailable:
        return {"skipped": "no_live_route", "repos": 0, "posted": 0}

    # Avoid clone/extraction for a request whose exact slot lease already lost a reclaim/supersede race.
    current = db(
        "SELECT core.policy_refresh_turn_is_current_with_authority("
        "%s,%s,%s,%s,%s,%s::smallint,%s)",
        PolicyRefreshStore._graph_identity(row))
    if not _db_bool(current):
        return {"superseded": True, "repos": 1, "posted": 0}
    if quota_surface_phase and onboarding_index == len(onboarding_plan):
        # Every frozen PR already has an exact surface receipt.  Close the
        # immediate surface phase without another clone/extract; the durable
        # defer stamps not_before, and only that later due turn probes whether
        # quota lifted.
        return {
            "quota_deferred": True,
            "terminal_degraded": "quota_paused",
            "repos": 1, "posted": 0,
            "surface_errors": 0, "errors": 0,
            "change_has_more": False,
            "change_cursor": after_change,
        }

    # Freeze the bounded number-only inventory in its own read-only fair turn,
    # before any clone/extraction.  A slow graph attempt can therefore never
    # be repeated merely because the later CAP+1 GitHub inventory read failed,
    # and quota discovery already has a durable plan to surface.
    if (onboarding_pending and not onboarding_head_pending
            and onboarding_plan is None):
        plan = build_onboarding_plan(gh_for, repo)
        numbers = plan.get("pr_numbers") if isinstance(plan, dict) else None
        if (not isinstance(numbers, list) or len(numbers) > 300
                or any(
                    not isinstance(number, int) or isinstance(number, bool)
                    or number < 1
                    for number in numbers)
                or numbers != sorted(set(numbers))
                or not isinstance(plan.get("truncated"), bool)):
            raise GraphRefreshContractError(
                "onboarding planner returned an invalid plan")
        return {
            "repos": 1, "posted": 0, "watching_posted": 0,
            "errors": 0, "onboarding_action": "freeze",
            "onboarding_plan": numbers,
            "onboarding_truncated": plan["truncated"],
            "onboarding_watching_done": False,
        }

    # The reserved phase token is never a graph coordinate. Resolve current canonical metadata in its own fair
    # turn; the exact DB CAS below owns lifecycle/lease invalidation. A 404/410 propagates and retries this cursor.
    if onboarding_head_pending:
        resolved = resolve_onboarding_head(
            db, gh_for, repo, repository_id, account_key)
        if isinstance(resolved, dict) and resolved.get("superseded") is True:
            return {"superseded": True, "repos": 1, "posted": 0}
        if not isinstance(resolved, dict) or resolved.get("empty") not in (True, False):
            raise GraphRefreshContractError("onboarding HEAD resolver returned no proof")
        resolved_branch = resolved.get("default_branch")
        resolved_head = resolved.get("head_sha")
        if resolved.get("empty") is True:
            if resolved_head is not None:
                raise GraphRefreshContractError("empty onboarding proof carried a HEAD")
        elif (not isinstance(resolved_head, str) or len(resolved_head) < 7):
            raise GraphRefreshContractError("onboarding HEAD proof was malformed")
        return {
            "repos": 1, "posted": 0, "errors": 0,
            "onboarding_action": "head",
            "onboarding_head_branch": resolved_branch,
            "onboarding_head_sha": resolved_head,
            "onboarding_empty": resolved.get("empty") is True,
        }

    frozen_onboarding = onboarding_pending and onboarding_plan is not None
    quota_surface_pending = (
        quota_surface_phase
        and onboarding_index < len(onboarding_plan)
    )
    reused_onboarding_snapshot = False
    if quota_surface_pending:
        # A prior strict graph attempt already proved the quota wall and the
        # exact immutable PR plan is durable.  Re-running clone/extraction for
        # every PR would recreate the long worker stall this phased worker is
        # designed to remove.  Surface one planned PR from durable state; once
        # the cursor reaches the end, the next turn enters the bounded defer;
        # only its later due turn performs one fresh graph probe and can resume
        # convergence after the quota wall lifts.
        outcome = {
            "terminal_degraded": "quota_paused",
            "reason": "free-tier limit",
        }
        quota_paused = True
    elif frozen_onboarding:
        snapshot = _as_jsonb(db(
            "SELECT core.graph_onboarding_snapshot_with_authority(%s,%s,%s,%s)",
            (repo, branch, target_sha, repository_id),
        )) or {}
        if snapshot.get("fulfilled") is True:
            if (not isinstance(snapshot.get("files"), int)
                    or isinstance(snapshot.get("files"), bool) or snapshot["files"] < 0
                    or not isinstance(snapshot.get("edges"), int)
                    or isinstance(snapshot.get("edges"), bool) or snapshot["edges"] < 0
                    or not isinstance(snapshot.get("over_cap"), bool)):
                raise GraphRefreshContractError("onboarding graph snapshot was malformed")
            outcome = {"converged": True, "ingest": snapshot}
            quota_paused = False
            reused_onboarding_snapshot = True

        # Frozen PR turns use the DB-local exact graph proof before their one planned replay.  A schema/extractor
        # upgrade can invalidate that proof; rebuild the same target in THIS turn while preserving the immutable
        # plan/index, then advance at most one PR.  Clearing the plan here would let a busy repo restart at PR 1.
    if not quota_surface_pending and not reused_onboarding_snapshot:
        if not callable(graph_refresh_strict):
            raise GraphRefreshHookUnavailable("strict graph refresh hook is required")

        outcome = graph_refresh_strict(
            db, gh_for, repo, branch, target_sha, repository_id,
            expected_owner_id=account_key, trace_id=trace,
            convergence_request_epoch=int(row.get("request_epoch", row.get("policy_epoch"))),
            convergence_slot=int(row.get("graph_slot")),
            convergence_lease_epoch=int(row.get("lease_epoch")))
        if not isinstance(outcome, dict):
            raise GraphRefreshContractError("strict graph refresh returned no proof")
        if outcome.get("superseded") is True:
            # The callback's required newer-target enqueue was one independently committed SQL-function call.
            return {"superseded": True, "repos": 1, "posted": 0}
        quota_paused = (
            outcome.get("terminal_degraded") == "quota_paused"
            or outcome.get("quota_paused") is True
            or outcome.get("reason") == "free-tier limit"
        )
        if outcome.get("converged") is not True and not quota_paused:
            raise GraphRefreshContractError("strict graph refresh did not complete")
        if not quota_paused:
            _refresh_graph_stats_after_fresh_write(dsn, db.graph_bulk_loaded)

    # Recheck the exact epoch+slot+lease after slow strict convergence and immediately before external writes.
    current = db(
        "SELECT core.policy_refresh_turn_is_current_with_authority("
        "%s,%s,%s,%s,%s,%s::smallint,%s)",
        PolicyRefreshStore._graph_identity(row))
    if not _db_bool(current):
        return {"superseded": True, "repos": 1, "posted": 0}
    # Watching already has a durable exact receipt. The mandated next fair turn only clears the onboarding latch;
    # even an extractor-generation repair above does not replay the immutable plan or issue another GitHub write.
    if onboarding_pending and onboarding_watching_done and not quota_paused:
        return {
            "repos": 1, "posted": 0, "watching_posted": 0,
            "errors": 0, "onboarding_action": "complete",
            "onboarding_watching_done": True,
        }
    ingest = outcome.get("ingest") if isinstance(outcome.get("ingest"), dict) else outcome
    if (quota_paused and onboarding_pending
            and onboarding_index < len(onboarding_plan)):
        pr_number = onboarding_plan[onboarding_index]
        try:
            # Keep the quota-only GitHub read/surface inside its own exact
            # authority scope.  Any read timeout, lifecycle ambiguity,
            # mutation-response ambiguity, lease fence loss, or commit failure
            # retains this immutable cursor and becomes the same attempts-
            # neutral quota defer.  It must never fall through fail_turn(),
            # which clears quota state and would repeat clone/extraction.
            with _external_mutation_authority(
                    dsn, gh_for, account_key, repo, row,
                    take_repo_lock=take_repo_lock,
                    release_repo_lock=release_repo_lock,
                    take_repository_id_lock=take_repository_id_lock,
                    yield_scoped_db=True) as authority:
                fenced_gh, held_db = authority
                if not callable(held_db):
                    raise GraphRefreshContractError(
                        "planned quota surface lacks held DB authority")
                surface = surface_onboarding_quota_paused_pr(
                    held_db, fenced_gh, repo, branch, target_sha,
                    repository_id, account_key, pr_number)
                if isinstance(surface, dict) and surface.get("superseded") is True:
                    return {"superseded": True, "repos": 1, "posted": 0}
                if (isinstance(surface, dict)
                        and surface.get("planned_pr") == pr_number
                        and surface.get("receipt") is False
                        and surface.get("retryable_surface") is True):
                    return {
                        "quota_deferred": True,
                        "terminal_degraded": "quota_paused",
                        "repos": 1, "posted": 0,
                        "surface_errors": 1, "errors": 0,
                        "change_has_more": False,
                        "change_cursor": after_change,
                    }
                if (not isinstance(surface, dict)
                        or surface.get("planned_pr") != pr_number
                        or surface.get("receipt") is not True):
                    raise GraphRefreshContractError(
                        "planned quota surface returned no exact receipt")
        except GraphRefreshContractError:
            # A malformed internal callback is a deploy/runtime contract bug,
            # not an expected GitHub ambiguity.  Keep it on the ordinary
            # visible failure budget instead of disguising it as a healthy,
            # indefinitely deferred quota wall.
            raise
        except Exception as exc:
            print(
                "onboarding quota surface retained "
                f"error_code={_bounded_error_code(exc)}",
                flush=True,
            )
            return {
                "quota_deferred": True,
                "terminal_degraded": "quota_paused",
                "repos": 1, "posted": 0,
                "surface_errors": 1, "errors": 0,
                "change_has_more": False,
                "change_cursor": after_change,
            }
        return {
            "terminal_degraded": "quota_paused",
            "repos": 1,
            "posted": int(surface.get("processed") is True),
            "surface_errors": 0, "errors": 0,
            "watching_posted": 0,
            "onboarding_action": "quota_advance",
            "onboarding_index": onboarding_index,
            "onboarding_pr_number": pr_number,
        }

    with _external_mutation_authority(
            dsn, gh_for, account_key, repo, row,
            take_repo_lock=take_repo_lock,
            release_repo_lock=release_repo_lock,
            take_repository_id_lock=take_repository_id_lock,
            yield_scoped_db=(
                onboarding_pending and onboarding_plan is not None
            )) as authority:
      if isinstance(authority, tuple):
        fenced_gh, held_db = authority
      else:
        fenced_gh, held_db = authority, None
      if quota_paused:
        refreshed = refresh_inflight(db, repo, branch)
        entries = refreshed.get("refreshed", []) if isinstance(refreshed, dict) else []
        surface = _post_quota_paused_refreshes(
            fenced_gh, repo, branch, entries, trace=trace,
            after_change=after_change)
        return {
            "quota_deferred": True, "terminal_degraded": "quota_paused",
            "repos": 1, "posted": int(surface.get("posted") or 0),
            "surface_errors": int(surface.get("errors") or 0),
            "errors": int(surface.get("errors") or 0),
            "change_has_more": surface.get("has_more") is True,
            "change_cursor": str(surface.get("cursor") or after_change),
        }

      watching_posted = 0

      def post_watching() -> int:
        try:
            from render import watching_check
        except ImportError:  # imported as a package
            from .render import watching_check
        watching = watching_check(
            files=int(ingest.get("files") or 0),
            edges=int(ingest.get("edges") or 0),
            branch=branch,
            indexing=False,
            over_cap=bool(ingest.get("over_cap")))
        if not fenced_gh.upsert_check(
                repo, target_sha, watching["conclusion"],
                watching["title"], watching["summary"]):
            raise RuntimeError("watching check upsert returned no receipt")
        return 1

      if onboarding_pending:
        if onboarding_index < len(onboarding_plan):
          pr_number = onboarding_plan[onboarding_index]
          if not callable(held_db):
            raise GraphRefreshContractError("planned PR replay lacks held DB authority")
          replay = replay_onboarding_pr(
              held_db, fenced_gh, repo, branch, target_sha, repository_id,
              account_key, pr_number)
          if isinstance(replay, dict) and replay.get("superseded") is True:
            return {"superseded": True, "repos": 1, "posted": 0}
          if (not isinstance(replay, dict) or replay.get("planned_pr") != pr_number
                  or replay.get("receipt") is not True):
            raise GraphRefreshContractError("planned PR replay returned no exact receipt")
          return {
              "repos": 1, "posted": int(replay.get("processed") is True),
              "watching_posted": 0, "errors": 0,
              "onboarding_action": "advance",
              "onboarding_index": onboarding_index,
              "onboarding_pr_number": pr_number,
          }

        # Exactly one authoritative repository/HEAD proof at the final plan boundary closes the
        # last-PR→Watching race without adding up to 300 redundant reads. It binds canonical name, stable id,
        # owner, current default branch, explicit non-empty state, and HEAD. Rename reconciles atomically through
        # the same held DB; branch/SHA drift supersedes through ordinary latest-wins before any old-target Check.
        if not callable(held_db):
            raise GraphRefreshContractError("onboarding Watching boundary lacks authoritative HEAD authority")
        latest = resolve_onboarding_head(
            held_db, fenced_gh, repo, repository_id, account_key)
        if isinstance(latest, dict) and latest.get("superseded") is True:
            return {"superseded": True, "repos": 1, "posted": 0}
        if (not isinstance(latest, dict) or latest.get("empty") is not False
                or latest.get("repo") != repo
                or str(latest.get("repository_id") or "") != repository_id):
            raise GraphRefreshContractError("onboarding Watching boundary returned malformed authority")
        latest_branch = latest.get("default_branch")
        latest_head = latest.get("head_sha")
        if (not isinstance(latest_branch, str) or not latest_branch
                or not isinstance(latest_head, str) or not (7 <= len(latest_head) <= 64)
                or any(char not in "0123456789abcdefABCDEF" for char in latest_head)):
            raise GraphRefreshContractError("onboarding Watching boundary returned a malformed HEAD")
        if latest_branch != branch or latest_head.lower() != target_sha.lower():
            superseding = _s.request_main_graph_refresh(
                held_db, repo, latest_branch, latest_head, repository_id,
                trace_id=trace, force=True)
            if not isinstance(superseding, dict) or superseding.get("queued") is not True:
                raise GraphRefreshContractError("new onboarding HEAD was not durably superseded")
            return {"superseded": True, "repos": 1, "posted": 0}

        watching_posted = post_watching()
        return {
            "repos": 1, "posted": 0, "watching_posted": watching_posted,
            "errors": 0, "onboarding_action": "watch",
            "onboarding_watching_done": True,
        }

      if ingest.get("cold_start") is True:
        watching_posted = post_watching()
      refreshed = refresh_inflight(db, repo, branch)
      entries = refreshed.get("refreshed", []) if isinstance(refreshed, dict) else []
      progress = _normalize_post_progress(
        post_refreshes(
            fenced_gh, repo, entries, db=db, branch=branch, trace_id=trace, delivery="",
            graph_degraded=False, return_progress=True, after_change=after_change),
        entries, after_change)
      return {
        "repos": 1,
        "posted": int(progress["posted"]),
        "watching_posted": watching_posted,
        "errors": int(progress["errors"]),
        "change_has_more": progress["has_more"] is True,
        "change_cursor": str(progress["cursor"] or after_change),
      }


def _drain_policy_refreshes(store: PolicyRefreshStore, gh, dsn: str, *, limit: int = _DRAIN_LIMIT,
                            coord_cap: int = _COORD_CAP, refresh_inflight=None, post_refreshes=None,
                            graph_freshness=None, graph_refresh_strict=None,
                            resolve_onboarding_head=None, build_onboarding_plan=None,
                            replay_onboarding_pr=None,
                            surface_onboarding_quota_paused_pr=None,
                            take_repository_id_lock=None, take_repo_lock=None,
                            release_repo_lock=None) -> dict:
    """Claim + refresh up to `limit` pending accounts this tick.

    New claims carry an exact durable installation generation and route with
    ``for_installation`` in O(1), independent of fleet size.  Legacy claims
    alone fall back to ``for_account``. A fallback miss is never terminal:
    even a complete App list is only a remote observation, while the claimed
    DB lifecycle route is still live. It retries until authoritative
    offboarding removes the route/queue, so duplicate or malformed inventory
    can never consume convergence evidence.
    """
    counters = {
        "drained": 0, "graph_drained": 0, "policy_drained": 0,
        "policy_sliced": 0, "change_sliced": 0,
        "superseded": 0, "quota_deferred": 0, "tail_rearmed": 0,
        "surface_rearmed": 0,
        "skipped_dead": 0, "failed": 0,
    }
    for _ in range(max(1, int(limit))):
        row = store.claim()
        if not isinstance(row, dict) or not row.get("account_id"):
            break
        account_id = row["account_id"]
        account_key = row.get("account_key") or ""
        request_kind = row.get("request_kind") or "policy"
        try:
            generation = _claimed_installation_generation(row)
            if row.get("onboarding_pending") is True and generation is None:
                # Installation onboarding is never a rolling name-only route. Its enqueue requires the admitted
                # exact generation tuple; a malformed legacy row retries content-free instead of scanning every
                # App installation through for_account.
                store.fail_turn(row, "onboarding_generation_missing")
                counters["failed"] += 1
                continue
            generation_current = store.installation_generation_is_current(row)
            if generation_current is False:
                store.fail_turn(row, "installation_generation_changed")
                counters["failed"] += 1
                continue
            if generation is not None:
                if not callable(getattr(gh, "for_installation", None)):
                    store.fail_turn(
                        row, "installation_direct_resolver_unavailable")
                    counters["failed"] += 1
                    continue
                gh_for = gh.for_installation(generation[0])
                if gh_for is None:
                    store.fail_turn(
                        row, "installation_direct_resolver_unavailable")
                    counters["failed"] += 1
                    continue
            else:
                gh_for = (
                    gh.for_account(account_id)
                    if hasattr(gh, "for_account")
                    else gh
                )
            if gh_for is None:
                reachable = None
                complete = None
                if hasattr(gh, "app_installations_reachability"):
                    try:
                        install_map = gh.app_installations_reachability()
                        reachable = install_map.get("reachable")
                        complete = install_map.get("complete")
                    except Exception:
                        reachable = False
                        complete = False
                if reachable is not True:
                    reason = "install_map_unreachable"
                elif complete is not True:
                    reason = "install_map_incomplete"
                else:
                    reason = "installation_absence_unconfirmed"
                n = store.fail_turn(row, reason)
                counters["failed"] += 1
                print(
                    "account-convergence non-authoritative install-map "
                    f"failure attempts={n}",
                    flush=True,
                )
                continue
            if not account_key:
                store.fail_turn(row, "missing_account_key")
                counters["failed"] += 1
                continue
            # Claim and work are intentionally separated by external installation-map resolution. Recheck the
            # exact policy epoch or graph request+slot+lease after that gap so a reclaim/supersede never performs
            # expensive work. Terminal/page functions repeat the same CAS after the external phase.
            if not store.turn_is_current(row):
                completion = store.finish_turn(row)
                if completion.get("superseded") is True:
                    store.release_superseded(row)
                    counters["superseded"] += 1
                else:
                    store.fail_turn(row, "preflight_epoch_not_current")
                    counters["failed"] += 1
                continue

            if request_kind == "graph":
                summary = refresh_graph_turn(
                    dsn, gh_for, account_key, row,
                    graph_refresh_strict=graph_refresh_strict,
                    refresh_inflight=refresh_inflight, post_refreshes=post_refreshes,
                    resolve_onboarding_head=resolve_onboarding_head,
                    build_onboarding_plan=build_onboarding_plan,
                    replay_onboarding_pr=replay_onboarding_pr,
                    surface_onboarding_quota_paused_pr=(
                        surface_onboarding_quota_paused_pr),
                    trace=str(account_id)[:16],
                    take_repository_id_lock=take_repository_id_lock,
                    take_repo_lock=take_repo_lock, release_repo_lock=release_repo_lock)
            else:
                summary = refresh_account(
                    dsn, gh_for, account_key, coord_cap=coord_cap,
                    after_repo=str(row.get("policy_cursor_repo") or ""),
                    after_branch=str(row.get("policy_cursor_branch") or ""),
                    after_change=str(row.get("change_cursor") or ""),
                    refresh_inflight=refresh_inflight, post_refreshes=post_refreshes,
                    graph_freshness=graph_freshness, trace=str(account_id)[:16],
                    take_repo_lock=take_repo_lock, release_repo_lock=release_repo_lock,
                    turn_row=row)

            if summary.get("skipped") == "no_live_route":
                if request_kind == "policy":
                    completion = store.finish_turn(row)
                    counters["tail_rearmed"] += int(completion.get("tail_rearmed") or 0)
                    counters["skipped_dead"] += 1
                else:
                    store.fail_turn(row, "no_live_route")
                    counters["failed"] += 1
                continue
            if summary.get("superseded") is True:
                completion = store.finish_turn(row)
                if completion.get("superseded") is True:
                    # New SQL releases the still-owned old route lease inside the exact miss. This additive helper
                    # is the rolling bridge for a generation where finish did not yet perform that lease-only CAS.
                    store.release_superseded(row)
                    counters["superseded"] += 1
                else:
                    store.fail_turn(row, "supersede_not_committed")
                    counters["failed"] += 1
                continue
            if summary.get("errors"):
                n = store.fail_turn(row, "repo_refresh_failed")
                counters["failed"] += 1
                print(f"account-convergence repo refresh failed attempts={n}", flush=True)
                continue

            onboarding_action = summary.get("onboarding_action")
            if request_kind == "graph" and onboarding_action == "head":
                transition = store.resolve_graph_onboarding_head(
                    row,
                    summary.get("onboarding_head_branch"),
                    summary.get("onboarding_head_sha"),
                )
                if transition.get("requeued") is True or transition.get("finished_empty") is True:
                    counters["tail_rearmed"] += int(transition.get("tail_rearmed") or 0)
                elif transition.get("superseded") is True:
                    store.release_superseded(row)
                    counters["superseded"] += 1
                else:
                    store.fail_turn(row, "onboarding_head_cas_failed")
                    counters["failed"] += 1
                continue
            if request_kind == "graph" and onboarding_action in (
                    "freeze", "advance", "quota_advance",
                    "watch", "complete"):
                transition = store.requeue_graph_onboarding(
                    row,
                    onboarding_action,
                    plan=(
                        summary.get("onboarding_plan")
                        if onboarding_action == "freeze" else None
                    ),
                    truncated=summary.get("onboarding_truncated") is True,
                    expected_index=int(summary.get("onboarding_index") or 0),
                    pr_number=int(summary.get("onboarding_pr_number") or 0),
                    watching_done=summary.get("onboarding_watching_done") is True,
                )
                if transition.get("requeued") is True:
                    counters["tail_rearmed"] += int(
                        transition.get("tail_rearmed") or 0)
                    print(
                        "account-convergence onboarding "
                        f"action={onboarding_action} repos=1 "
                        f"watching={int(summary.get('watching_posted') or 0)} "
                        "tail_rearmed=1",
                        flush=True,
                    )
                elif transition.get("superseded") is True:
                    store.release_superseded(row)
                    counters["superseded"] += 1
                else:
                    store.fail_turn(row, "onboarding_requeue_cas_failed")
                    counters["failed"] += 1
                continue

            if summary.get("change_has_more") is True:
                change_cursor = str(summary.get("change_cursor") or "")
                if request_kind == "graph":
                    sliced = store.requeue_graph_page(row, change_cursor)
                else:
                    sliced = store.requeue_policy_page(
                        row,
                        str(summary.get("cursor_repo") or ""),
                        str(summary.get("cursor_branch") or ""),
                        change_cursor,
                        repo_complete=False)
                if sliced.get("requeued") is True:
                    counters["change_sliced"] += 1
                    counters["tail_rearmed"] += 1
                    print(
                        f"account-convergence sliced kind={request_kind} "
                        "page=change tail_rearmed=1",
                        flush=True)
                elif sliced.get("superseded") is True:
                    store.release_superseded(row)
                    counters["superseded"] += 1
                else:
                    store.fail_turn(row, "change_requeue_cas_failed")
                    counters["failed"] += 1
                continue

            if summary.get("quota_deferred") is True:
                deferred = store.defer_graph_turn(
                    row, "quota_paused", _QUOTA_DEFER_SECONDS)
                if deferred.get("deferred") is True:
                    counters["quota_deferred"] += 1
                    print(
                        f"account-convergence deferred kind=graph reason=quota_paused "
                        f"repos={int(summary.get('repos') or 0)} "
                        f"posted={int(summary.get('posted') or 0)} "
                        f"surface_errors={int(summary.get('surface_errors') or 0)}",
                        flush=True)
                elif deferred.get("superseded") is True:
                    store.release_superseded(row)
                    counters["superseded"] += 1
                else:
                    store.fail_turn(row, "quota_defer_cas_failed")
                    counters["failed"] += 1
                continue

            if request_kind == "policy" and summary.get("has_more") is True:
                sliced = store.requeue_policy_page(
                    row,
                    str(summary.get("cursor_repo") or ""),
                    str(summary.get("cursor_branch") or ""),
                    str(summary.get("change_cursor") or ""),
                    repo_complete=True)
                if sliced.get("requeued") is True:
                    counters["policy_sliced"] += 1
                    counters["tail_rearmed"] += 1
                    print(
                        "account-convergence sliced kind=policy repos=1 tail_rearmed=1",
                        flush=True)
                elif sliced.get("superseded") is True:
                    # The SQL exact miss releases only the still-owned old route lease. This helper remains a
                    # rolling bridge for an older SQL generation, matching finish_turn's supersede handling.
                    store.release_superseded(row)
                    counters["superseded"] += 1
                else:
                    store.fail_turn(row, "slice_requeue_cas_failed")
                    counters["failed"] += 1
                continue

            completion = store.finish_turn(row)
            if completion.get("finished") is True:
                counters["drained"] += 1
                counters["graph_drained"] += int(request_kind == "graph")
                counters["policy_drained"] += int(request_kind == "policy")
                counters["tail_rearmed"] += int(completion.get("tail_rearmed") or 0)
                counters["surface_rearmed"] += int(
                    completion.get("surface_rearmed") is True)
                print(
                    f"account-convergence drained kind={request_kind} "
                    f"repos={int(summary.get('repos') or 0)} posted={int(summary.get('posted') or 0)} "
                    f"watching={int(summary.get('watching_posted') or 0)} "
                    f"tail_rearmed={int(completion.get('tail_rearmed') or 0)} "
                    f"surface_rearmed={'true' if completion.get('surface_rearmed') is True else 'false'}",
                    flush=True)
            elif completion.get("superseded") is True:
                store.release_superseded(row)
                counters["superseded"] += 1
            else:
                store.fail_turn(row, "graph_not_fulfilled")
                counters["failed"] += 1
        except Exception as e:
            code = _bounded_error_code(e)
            n = store.fail_turn(row, code)
            counters["failed"] += 1
            print(
                f"account-convergence failed kind={request_kind} attempts={n} error_code={code}",
                flush=True)
    return counters


def start_policy_refresh_loop(store: PolicyRefreshStore, gh, dsn: str, *, interval: int = _REFRESH_INTERVAL,
                              limit: int = _DRAIN_LIMIT, coord_cap: int = _COORD_CAP,
                              graph_refresh_strict=None):
    """Start the background drainer daemon (mirrors delivery_queue.start_recovery_loop). Each tick drains up to
    `limit` pending accounts; a tick error is logged, never fatal (best-effort like the delivery recovery loop).
    Returns the thread."""
    def _loop():
        while True:
            try:
                _drain_policy_refreshes(
                    store, gh, dsn, limit=limit, coord_cap=coord_cap,
                    graph_refresh_strict=graph_refresh_strict)
            except Exception as e:
                print(f"policy-refresh drain skipped: {str(e)[:160]}", flush=True)
            time.sleep(interval)

    thread = threading.Thread(target=_loop, name="veripsa-policy-refresh", daemon=True)
    thread.start()
    return thread
