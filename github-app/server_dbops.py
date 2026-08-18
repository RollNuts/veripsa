#!/usr/bin/env python3
"""server_dbops — the OUT-OF-BAND DB operations the webhook event loop runs AROUND the body transaction:
the per-(account,repo) advisory lock (cross-process serialization) and the post-ingest planner-stats refresh.

Split out of server.py (the webhook god-file): this leaf cluster is pure DB plumbing — every function takes a
psycopg2 cursor/connection and a DSN, touches only core.* and Postgres' own catalogs, holds NO module state, and
is imported back by server.py UNCHANGED. It was a chronic edit/merge hotspot inside server.py (the #181 cold-stats
fix and the per-repo lock landed here), so it is exactly the kind of cohesive slice Veripsa's own god-file signal
flags for splitting — dogfooded. Behavior is identical; the full webhook gate (tests/test_server.py + multiproc +
external-resilience) exercises these through the real loop."""
from __future__ import annotations

import psycopg2

# env_int: the VALIDATED env-knob reader (fail-loud on a typo'd/out-of-range knob — same as make_db_processor).
try:
    from env_config import env_int
except ImportError:  # imported as a package
    from .env_config import env_int
try:
    import event_budget as _event_budget
except ImportError:  # imported as a package
    from . import event_budget as _event_budget
try:
    import db_connect as _db_connect
except ImportError:  # imported as a package
    from . import db_connect as _db_connect

# The per-lock-site session timeouts — the SAME knobs make_db_processor reads. Read once at import (fail-loud on a
# bad value). _take_repo_lock arms these on EVERY lock acquisition, so no lock site can drift back to an unguarded,
# unbounded pg_advisory_lock wait. Config constants, not mutable module state.
_LOCK_TIMEOUT_MS = env_int("VERIPSA_DB_LOCK_TIMEOUT_MS", 30_000, min_value=1)
_STMT_TIMEOUT_MS = env_int("VERIPSA_DB_STATEMENT_TIMEOUT_MS", 600_000, min_value=1)
_CONNECT_TIMEOUT_SECONDS = env_int(
    "VERIPSA_DB_CONNECT_TIMEOUT_SECONDS", 10, min_value=1, max_value=60,
)
_STATS_TIMEOUT_MS = env_int(
    "VERIPSA_DB_STATS_TIMEOUT_MS", 10_000, min_value=1, max_value=60_000,
)


def _remaining_timeout_ms(configured_ms: int) -> int:
    _event_budget.raise_if_expired()
    rem = _event_budget.remaining()
    if rem is None:
        return max(1, int(configured_ms))
    return max(1, min(int(configured_ms), int(rem * 1000)))


def _connect_timeout_seconds() -> int:
    seconds = _event_budget.timeout_for(_CONNECT_TIMEOUT_SECONDS)
    if seconds < 1.0:
        raise _event_budget.EventBudgetExceeded(
            "less than one second remains for the webhook DB connection")
    return max(1, int(seconds))


def _bounded_connect(dsn: str, **kwargs):
    """Resolve/connect within one local-or-event absolute deadline."""
    event_deadline = _event_budget.current_deadline()
    connect_deadline = _db_connect.deadline_after(
        _CONNECT_TIMEOUT_SECONDS, event_deadline)
    try:
        return _db_connect.connect(
            psycopg2.connect,
            dsn,
            deadline=connect_deadline,
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            **kwargs,
        )
    except _db_connect.DatabaseConnectDeadlineExceeded as error:
        remaining = _event_budget.remaining()
        if event_deadline is not None and remaining is not None and remaining <= 0:
            raise _event_budget.EventBudgetExceeded(
                "webhook DB connection exceeded its event deadline") from error
        raise


def _arm_lock_session(cur, lock_timeout_ms=None):
    """Bound the BLOCKING advisory-lock wait + every statement on this connection BEFORE the lock — so NO lock
    site can wedge forever on a contended/severed lock holder (the unbounded pg_advisory_lock hang). Called by
    _take_repo_lock, so EVERY lock site — the live per-event processor, the co-change populate pool, the boot
    self-heal reconcile, the install/uninstall fan-out — is guarded BY CONSTRUCTION and can never again open an
    unguarded lock. SESSION SETs (cover the lock wait AND the body on the same connection); idempotent (the live
    processor also sets the same env values before calling here — harmless). Postgres SET takes no bind params,
    so the validated-int ms values are formatted as literals.

    lock_timeout_ms overrides the wait ONLY for this call (default None → the global _LOCK_TIMEOUT_MS): the LIVE
    per-event path passes a SHORT convergence wait so a contended per-repo/lifecycle lock DEFERS in ~1s instead of
    burning one worker lane's wall-clock for the full 30s (needlessly consuming keyed-pool capacity).
    Background sites (boot reconcile, co-change pool, install fan-out) pass nothing and keep the long wait."""
    _lt = _remaining_timeout_ms(
        int(lock_timeout_ms) if lock_timeout_ms else int(_LOCK_TIMEOUT_MS)
    )
    cur.execute("SET lock_timeout = %s" % _lt)
    cur.execute("SET statement_timeout = %s" % _remaining_timeout_ms(_STMT_TIMEOUT_MS))


def _canonical_repository_id(value):
    """Canonical positive ASCII GitHub repository id, or None."""
    if value in (None, "") or isinstance(value, bool):
        return None
    value = str(value).strip()
    if (not value.isascii() or not value.isdigit() or value.startswith("0")
            or len(value) > 32):
        return None
    return value


def _take_repository_id_lock(cur, repository_id, *, lock_timeout_ms=None):
    """Acquire the bounded SESSION lock for one globally-stable GitHub repository object.

    This lock must precede mutable coordinate locks and survive autocommit background workflows. Keeping timeout
    arming here makes every live/fanout/boot/co-change call site bounded by construction. lock_timeout_ms (default
    None → global) lets the LIVE path pass a short convergence wait; see _arm_lock_session.
    """
    repository_id = _canonical_repository_id(repository_id)
    if repository_id is not None:
        _arm_lock_session(cur, lock_timeout_ms)
        cur.execute(
            "SELECT pg_advisory_lock(hashtext('github-repository-id'),hashtext(%s))",
            (repository_id,),
        )
    return repository_id


# ── PER-(ACCOUNT,REPO) ADVISORY LOCK — the SINGLE source of truth for the per-repo serialization key. ───────
# The lock serializes same-coordinate events across OS processes (Render's 2-instance rolling-deploy overlap,
# and any scale-out). It MUST be keyed on (account_id, repo), NOT the bare repo full_name: two DIFFERENT tenants
# can install Veripsa on a repo with the SAME full_name (e.g. two forks both named "org/app", or a public repo
# both org-A and org-B install) — keying on the bare name would make tenant A's event for "org/app" block (and
# race the lock-held writes against) tenant B's UNRELATED "org/app", a cross-tenant collision. It is also a 32-
# bit single hashtext, so even WITHIN one tenant two different repo names birthday-collide at ~77k repos. Keying
# on BOTH coordinates with the TWO-ARG pg_advisory_lock(int4,int4) form gives an independent 32-bit dimension
# per coordinate (64 bits total): a collision now needs BOTH the account AND the repo to hash-collide. Every
# lock site (the live per-event processor here AND the boot/backfill _reconcile_one_repo in ingest.py) routes
# through these two helpers so the two sites can NEVER drift to disagreeing key schemes (a silent split that
# would let a live event and a boot reconcile for the same coordinate run concurrently). account_key None (a
# minimal/malformed payload with no owner id, or the local/dogfood path) → a STABLE empty-string sentinel so the
# null-account space still serializes by repo and never aliases a real numeric account id.
def _repo_lock_args(account_key, repo):
    """The two text args fed to hashtext() for the per-(account,repo) advisory lock. account_key None → '' (the
    stable null-account sentinel: hashtext('') is a fixed key, distinct from any real numeric account id)."""
    return (str(account_key) if account_key is not None else "", repo)


def _take_repo_lock(cur, account_key, repo, *, lock_timeout_ms=None):
    """Acquire the SESSION-scoped per-(account,repo) advisory lock on `cur`'s connection (blocks until free,
    BUT bounded — see below). Session-scoped (not _xact) so it outlives the event's body txn — released only on
    unlock/connection close. Arms lock_timeout + statement_timeout on the connection FIRST so the 'blocks until
    free' wait can never be unbounded on a contended/severed holder (every lock site is guarded here, not just
    the live processor). lock_timeout_ms (default None → global) lets the LIVE path pass a short convergence wait."""
    _arm_lock_session(cur, lock_timeout_ms)
    cur.execute("SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))", _repo_lock_args(account_key, repo))


def _release_repo_lock(cur, account_key, repo):
    """Release the per-(account,repo) advisory lock taken by _take_repo_lock (same key). Closing the connection
    also drops it — this explicit release lets one held connection reconcile many repos one-lock-at-a-time."""
    cur.execute("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))", _repo_lock_args(account_key, repo))


# ── CONNECT-PER-QUERY DB RUNNER — used for ONE-OFF startup work (boot self-heal, the __main__ backfill CLI). ──
# A NEW psycopg2 connection per query: simple + correct for a sequential, single-tenant batch (open → SET search_path
# → execute → close). DELIBERATELY NOT used on the hot webhook event path: a webhook event runs ~17 queries; this
# would open/close ~17 connections per event and exhaust the cheap managed Postgres connection limit in seconds. The
# live per-event processor (event_processor.make_db_processor) instead holds ONE pooled, lock-pinned connection per
# event and runs every query over it via _scoped_db (the single-connection sibling — kept on server.py because tests
# monkeypatch server._scoped_db to inject mid-event connection drops; see event_processor._server()._scoped_db).
def _make_db(dsn: str):
    """Connect-per-query runner — used for one-off startup work (backfill). The hot per-event path uses the
    pooled, lock-held processor below, NOT this (this would open ~17 connections per webhook event)."""
    def run(sql, args=()):
        conn = _bounded_connect(dsn)
        deadline_guard = _event_budget.arm_connection_deadline(conn)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SET statement_timeout = %s" % _remaining_timeout_ms(_STMT_TIMEOUT_MS))
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            deadline_guard.disarm()
            conn.close()
    return run


_GRAPH_BULK_LOADED_GUC = "core.graph_bulk_loaded"


def _refresh_graph_stats_if_bulk_loaded(conn, dsn: str) -> bool:
    """ROOT FIX (the cold-stats hang, audit #169): after a bulk graph load the FIRST core.main_impact_surface
    plans the O(edges) recursive adjacency over core.code_node/code_edge BLIND — the ingest path bulk
    DELETE+INSERTs the whole coordinate but autovacuum's ANALYZE runs on a DELAY, so the first brain call after
    a fresh/large ingest hits EMPTY/STALE planner stats (reltuples=-1, never analyzed) and mis-plans into a
    multi-minute CPU-bound scan. Measured: ~121s cold vs ~0.5s after ANALYZE on a 13.6k-node / 18.4k-edge graph
    — byte-identical result. The fix: refresh the stats RIGHT AFTER the load so the first call plans correctly.

    WHY THIS MECHANISM (three real constraints, all load-bearing — see core.refresh_graph_stats):
      (a) TRANSACTION: ANALYZE cannot run inside a transaction block, and the event's mutating body runs in ONE
          txn (#106). It must run AFTER the commit. We invoke core.refresh_graph_stats() — a PROCEDURE whose
          `CALL` on an autocommit connection is NOT inside a transaction block (a plain function body would be).
      (b) PRIVILEGE (moat preserved): the App connects as least-privilege veripsa_app, and on PG16 ANALYZE needs
          the table OWNER (no MAINTAIN grant pre-PG17). core.refresh_graph_stats is SECURITY DEFINER owned by
          veripsa_migrator, so the ANALYZE runs with the owner's authority while the App stays least-privilege.
      (c) FRESH-BACKEND VISIBILITY: a same-backend ANALYZE immediately after that backend's own INSERT samples a
          STALE cached relation size (0 pages) and records the table EMPTY (reltuples stays -1, planner blind) —
          verified: pg_relation_size shows the data is there, yet same-backend ANALYZE reads relpages=0. So we
          run it on ONE fresh short-lived autocommit connection (a new backend sees the true heap). This mirrors
          how the advisory lock / tenant routing already run OUTSIDE the body txn — same class of out-of-band work.

    HOW WE KNOW A BULK LOAD HAPPENED (synchronous, not a stats read): core.ingest_graph_with_authority sets the
    session GUC core.graph_bulk_loaded='1' inside the body txn. We do NOT consult pg_stat n_mod_since_analyze:
    Postgres' cumulative stats are flushed ASYNCHRONOUSLY (~1s lag), so read immediately post-commit they still
    show 0 — the ANALYZE would be skipped exactly when it is needed. A session GUC is synchronous and precise:
    set-and-committed by the ingest → readable here on the SAME (event) connection; rolled back (no ingest) →
    it reverts → we do nothing. We read it on `conn` (which holds it) and CALL the refresh on the fresh connection.

    BOUNDED + IDEMPOTENT: at most ONE refresh (one ANALYZE of the two graph tables) per event, and only when the
    graph was actually re-ingested. CONTENT-FREE (touches only Postgres' own statistics, never row contents).
    NEVER-CRASH: the graph is ALREADY correct after commit — only the PLAN is cold — so a failed refresh must
    NEVER fail the delivery; any error is swallowed (worst case: the next event's load, or autovacuum, refreshes
    the stats; the result is identical, just the first call is slow). Returns True iff a refresh was issued."""
    fresh = None
    fresh_guard = None
    try:
        # Read the bulk-load flag on the EVENT connection (it set+committed the GUC there). After commit `conn`
        # may be in non-autocommit mode with no open txn; a bare SELECT is fine and we roll it back so we leave
        # the connection clean for close().
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting(%s, true)", (_GRAPH_BULK_LOADED_GUC,))
            row = cur.fetchone()
        try:
            conn.rollback()    # close out the implicit read txn (no-op in autocommit) — leave conn pristine
        except Exception:
            pass
        if not row or (row[0] or "") != "1":
            return False
        # FRESH autocommit connection: a new backend sees the true (just-grown) heap size, and autocommit lets
        # the procedure's ANALYZE run outside any transaction block. Short-lived: connect, CALL, close.
        fresh = _bounded_connect(dsn)
        fresh_guard = _event_budget.arm_connection_deadline(fresh)
        fresh.autocommit = True
        with fresh.cursor() as cur:
            cur.execute("SET search_path=core,pg_catalog")
            cur.execute("SET statement_timeout = %s" % _remaining_timeout_ms(_STATS_TIMEOUT_MS))
            cur.execute("CALL core.refresh_graph_stats()")   # SECURITY DEFINER → runs ANALYZE as the table owner
        return True
    except _event_budget.EventBudgetExceeded as e:
        # The event body is already committed.  Delivery cancellation here is
        # post-commit cleanup failure, never authority to release/replay the
        # durable row and duplicate the committed business writes.
        try:
            print(f"post-ingest ANALYZE skipped after event budget (graph already correct, plan stays cold): "
                  f"{str(e)[:160]}", flush=True)
        except Exception:
            pass
        return False
    except Exception as e:
        # The graph is already correct (committed); only the cold plan is unrefreshed. NEVER let a stats refresh
        # failure fail a delivery — log content-free and move on (autovacuum / the next load catches up).
        try:
            print(f"post-ingest ANALYZE skipped (graph already correct, plan stays cold): {str(e)[:160]}", flush=True)
        except Exception:
            pass
        return False
    finally:
        if fresh_guard is not None:
            fresh_guard.disarm()
        if fresh is not None:
            try:
                fresh.close()
            except Exception:
                pass
