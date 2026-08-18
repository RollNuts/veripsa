"""The LIVE per-event PROCESSOR concern — extracted from the server god-file (follow-up to #207's HONEST
residual: "the HTTP/queue/worker plumbing" split). This is the layer that wraps every dequeued webhook event
with the DB SESSION it needs BEFORE the dispatcher (handle_event) runs: a stable-ID lock plus, for ordinary work,
a per-(account,repo) advisory LOCK (multi-coordinate lifecycle actions take their sorted set inside the DB txn),
the tenant ROUTING pin (RLS),
and the ATOMIC body transaction (commit-on-success / rollback-on-error → Core recovery retries cleanly). It is a
DISTINCT responsibility from the dispatcher (handle_event answers "what happens on event X"; THIS answers "how
an event gets a connection + lock + tenant + txn around that dispatch") — and it changes for DIFFERENT reasons
(locking / transaction / scaling / tenant-routing semantics, exactly where the #181 cold-stats fix and the
per-repo lock churned), so its conflict surface is separable from the event-type router's.

Symbols (all MOVED here UNCHANGED from server.py):
  _event_repo / _event_account_key                          event → (repo coordinate, stable tenant key) routing
  _INSTALL_FANOUT_FIELD / _install_fanout_repos             the install/uninstall events that fan out per-repo
  _process_install_event_per_repo_locked                    per-repo locked queue/purge fan-out (no top-level repo)
  make_db_processor                                         the EventQueue's `process`: lock + tenant + atomic txn
  _READYZ_INSTALLATION / app_identity_ok                    /readyz service-identity self-check (deploy-blocker class)

DESIGN (mirrors event_queue.py / health_watchdog.py / graph_freshness.py / ingest.py): this module imports
NOTHING from server.py at LOAD time (no circular import — server.py imports THIS and RE-EXPORTS these names for
backward compatibility, so server.make_db_processor / server._event_account_key / server._event_repo /
server.app_identity_ok keep working for serve(), the __main__ backfill, and the gate's S.<name> access). The
few server-side SEAMS these functions need are NOT part of the processor concern and stay in server.py — the
payload type-guard _as_obj, the single-connection DB runner _scoped_db, and the webhook ROUTER handle_event
(which make_db_processor + the per-repo fan-out dispatch to). They are resolved at CALL time via the same lazy
`import server` idiom ingest.py / health_watchdog.py use (server.py is fully loaded by the time any of these
runs) — so the back-edge to handle_event never makes a module-load cycle.

WHY _scoped_db is reached via the call-time seam (NOT moved here): a test injects a connection-drop mid-event by
monkeypatching server._scoped_db (tests/test_external_resilience.py::make_failing_processor sets S._scoped_db =
patched before calling the real processor, to prove the per-event txn rolls back the WHOLE event). make_db_processor
calling _server()._scoped_db(conn) at event time means that patch is STILL seen here — so the rollback injection
keeps working with NO change to the test. Moving _scoped_db into this module would have bound the processor to
this module's global and silently DISARMED that injection (the test would pass trivially with nothing injected).
_scoped_db also stays the connection-runner seam serve() + __main__ use, so its home is correctly server.py.

The advisory-lock + planner-stats DB plumbing (_take_repo_lock / _release_repo_lock /
_refresh_graph_stats_if_bulk_loaded) already lives in server_dbops.py (#207) and imports nothing from server, so
it is imported at LOAD time here (no cycle). env_int (the validated env-knob reader) likewise.

Behavior-preserving extraction: a pure move + re-import. No logic, signatures, return shapes, never-crash
fail-open semantics, advisory/content-free guarantees, or lock/txn ordering changed — only the home of the symbols.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

import psycopg2
from psycopg2.extras import Json

# env_int: the VALIDATED env-knob reader (fail SAFE on a typo'd cap — a non-int or out-of-range value raises a
# loud, actionable refusal that names the var + value + range). Same standalone/package dual-import idiom server.py
# uses. make_db_processor reads its timeout knobs ONCE at construction through this.
try:
    from env_config import env_int  # noqa: E402
except ImportError:  # imported as a package
    from .env_config import env_int  # noqa: E402
try:
    import event_budget as _event_budget  # noqa: E402
except ImportError:  # imported as a package
    from . import event_budget as _event_budget  # noqa: E402
try:
    import db_connect as _db_connect  # noqa: E402
except ImportError:  # imported as a package
    from . import db_connect as _db_connect  # noqa: E402
try:
    from delivery_queue import (  # noqa: E402
        IntentionalDeliveryDeferral,
        _DELIVERY_ATOMIC_FINALIZE_PROTOCOL,
        _DELIVERY_EXECUTION_AUTHORITY,
        _DeliveryAtomicDeferralResult,
        _DeliveryCommitAmbiguity,
        _DeliveryExecutionAuthority,
        _DeliveryFanoutDeferralCommitAmbiguity,
    )
except ImportError:  # imported as a package
    from .delivery_queue import (  # noqa: E402
        IntentionalDeliveryDeferral,
        _DELIVERY_ATOMIC_FINALIZE_PROTOCOL,
        _DELIVERY_EXECUTION_AUTHORITY,
        _DeliveryAtomicDeferralResult,
        _DeliveryCommitAmbiguity,
        _DeliveryExecutionAuthority,
        _DeliveryFanoutDeferralCommitAmbiguity,
    )
# The out-of-band DB-ops cluster (#207): the per-(account,repo) advisory lock + the post-ingest planner-stats
# refresh. It imports NOTHING from server.py, so it is safe to import at LOAD time here (no cycle — server_dbops.py
# ← event_processor.py, both ← server.py).
try:
    from server_dbops import (_take_repo_lock, _release_repo_lock, _take_repository_id_lock,  # noqa: E402
                              _refresh_graph_stats_if_bulk_loaded)
except ImportError:  # imported as a package
    from .server_dbops import (_take_repo_lock, _release_repo_lock, _take_repository_id_lock,  # noqa: E402
                               _refresh_graph_stats_if_bulk_loaded)


def _server():
    """The server module, resolved at CALL time (NOT at load — server.py imports THIS, so a load-time
    `import server` here would be a circular import). server.py is fully loaded by the time any processor
    function runs, so this just returns the already-imported module. Mirrors ingest._server. Used to reach the
    server-side SEAMS that are not part of the processor concern: the _as_obj/_as_list payload guards, the
    single-connection DB runner _scoped_db (kept in server so its monkeypatch in test_external_resilience still
    takes effect here — see the module docstring), and the webhook ROUTER handle_event (which this dispatches to)."""
    try:
        import server as _s  # call-time import → no module-load circularity
    except ImportError:  # imported as a package
        from . import server as _s  # type: ignore
    return _s


def _event_repo(payload: dict):
    """The single repo coordinate for an event naming ONE repo (push/PR/repository-*). Ordinary work locks it in
    the outer session; multi-coordinate lifecycle actions delegate their complete lock set to the DB. None → the live
    body takes no top-level lock — BUT a multi-repo install/uninstall event (installation /
    installation_repositories, no top-level `repository`) is NOT left unlocked: make_db_processor routes it to a
    per-repo fan-out (_process_install_event_per_repo_locked) that locks each affected repository's short durable
    enqueue/purge transaction individually. Installation ingress performs no remote graph or PR work here."""
    r = _server()._as_obj(payload.get("repository")).get("full_name")
    return r if isinstance(r, str) and r else None


def _lifecycle_owns_coordinate_locks(event_type: str, payload: dict) -> bool:
    """True when the durable DB boundary discovers and locks every mutable coordinate itself.

    These lifecycle actions can touch an old name, a current name, or both. Taking the signed/new coordinate in
    the outer session before the DB acquires its sorted candidate set permits A→B / B→A lock inversion. Keep the
    stable repository-id session lock, but delegate all coordinate locks to the one DB transaction.
    """
    def canonical_id(value) -> bool:
        if value in (None, "") or isinstance(value, bool):
            return False
        value = str(value).strip()
        return (value.isascii() and value.isdecimal() and not value.startswith("0")
                and len(value) <= 32)

    action = payload.get("action") if isinstance(payload, dict) else None
    if event_type == "repository" and action in ("renamed", "transferred"):
        return True
    if event_type == "repository" and action == "deleted":
        return canonical_id(_server()._as_obj(payload.get("repository")).get("id"))
    if event_type == "installation_repositories" and action == "removed":
        removed = _server()._as_list(payload.get("repositories_removed"))
        return len(removed) == 1 and canonical_id(_server()._as_obj(removed[0]).get("id"))
    return False


def _event_account_key(payload: dict):
    """The STABLE tenant key for the account routing = the GitHub ACCOUNT (org/user) id that owns the repo /
    installation. We key the tenant by this, NOT by installation.id, because installation.id is EPHEMERAL —
    it changes if the owner uninstalls + reinstalls, which would orphan a paying customer's whole history.
    The owning account id is stable (it even survives an org/user RENAME). It is right there in the webhook:
    repository.owner.id (push/PR), installation.account.id (installation events), organization.id, or the signed
    marketplace_purchase.account.id (billing events carry no installation/repository).

    ALL THREE SOURCES ARE OWNER/ACCOUNT-ID SPACE — and that is load-bearing: core.installation_account.account_id
    holds the OWNER (account) id as 'ACCT-GH-'||<owner_id> (enter_installation_with_authority prefixes whatever this
    returns), NOT the install id. The PRIOR 4th choice — installation.id — is a DIFFERENT id space (the ephemeral
    install id), so a payload that carried ONLY installation.id minted a PHANTOM 'ACCT-GH-<install_id>' tenant,
    distinct from the repo's real 'ACCT-GH-<owner_id>' (a split-brain: the same customer routed to two accounts —
    audit iter-4 P2). So the installation.id fallback is DROPPED: when NONE of the owner/account sources resolve we
    return None (honest-unknown). None means the live processor pins NO tenant and the handler no-ops — the correct
    behavior for a minimal/malformed payload (every REAL installed-repo event carries repository.owner.id or
    installation.account.id, so this never demotes a genuine event). We never provision a tenant from an install id."""
    _s = _server()
    for v in (_s._as_obj(_s._as_obj(payload.get("repository")).get("owner")).get("id"),
              _s._as_obj(_s._as_obj(payload.get("installation")).get("account")).get("id"),
              _s._as_obj(payload.get("organization")).get("id"),
              _s._as_obj(_s._as_obj(payload.get("marketplace_purchase")).get("account")).get("id")):
        if v not in (None, ""):
            return str(v)
    return None


def _event_owner_account_id(payload: dict):
    """The OWNING-ACCOUNT id derived from the payload's repository.owner.id — the tenant key the FIRST-choice
    _event_account_key branch uses. None when this event carries no top-level repository (install/marketplace/etc).
    Content-free: a numeric account id only. Mirrors _event_account_key's repository.owner.id read EXACTLY."""
    _s = _server()
    v = _s._as_obj(_s._as_obj(payload.get("repository")).get("owner")).get("id")
    return str(v) if v not in (None, "") else None


def _event_installation_account_id_from_payload(payload: dict):
    """The OWNING-ACCOUNT id GitHub stamps INSIDE the delivering installation object (installation.account.id) —
    the account that owns the installation, in the SAME id space as repository.owner.id (NOT installation.id, which
    is the ephemeral install id in a DIFFERENT space). None when absent. Content-free: a numeric account id only.
    This is _event_account_key's 2nd-choice source, read here on its own so the two can be CROSS-CHECKED."""
    _s = _server()
    v = _s._as_obj(_s._as_obj(payload.get("installation")).get("account")).get("id")
    return str(v) if v not in (None, "") else None


def _event_account_metadata(payload: dict) -> tuple[str | None, str | None]:
    """Public GitHub account metadata for owner/admin display.

    The tenant key stays the immutable numeric account id. Login/type are mutable
    public labels only, read from the webhook payload and stored so admin screens
    can show the current GitHub owner without hard-coded stale usernames.
    """
    _s = _server()
    repo_owner = _s._as_obj(_s._as_obj(payload.get("repository")).get("owner"))
    inst_owner = _s._as_obj(_s._as_obj(payload.get("installation")).get("account"))
    org = _s._as_obj(payload.get("organization"))

    login = (
        repo_owner.get("login")
        or inst_owner.get("login")
        or org.get("login")
    )
    account_type = (
        repo_owner.get("type")
        or inst_owner.get("type")
        or ("Organization" if org.get("id") not in (None, "") else None)
    )

    login_s = str(login).strip() if login not in (None, "") else ""
    type_s = str(account_type).strip() if account_type not in (None, "") else ""
    return (login_s or None, type_s or None)


def _event_installation_id(payload: dict) -> str | None:
    """Bounded ephemeral GitHub App installation generation carried by a signed delivery.

    The owner/account id routes the tenant; this different id proves which install generation emitted ordinary
    work. Events that genuinely carry no installation object return ``None`` and remain on the existing-route
    fence. A present malformed value is poison authority and must retry rather than silently bypass the fence.
    """
    value = _server()._as_obj(payload.get("installation")).get("id")
    if value in (None, ""):
        return None
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        raise RuntimeError("webhook installation.id is malformed")
    installation_id = str(value).strip()
    if not installation_id or len(installation_id) > 64:
        raise RuntimeError("webhook installation.id is malformed")
    return installation_id


_ACCOUNT_LIFECYCLE_LOCK_CONSTRAINT = "veripsa_account_lifecycle_advisory_timeout"
_LIVE_LOCK_RETRY_SECONDS = 30

# The LIVE per-event convergence-lock wait. One fixed pool slot and its keyed lane block this long inside a
# contended per-repo / account-lifecycle advisory lock. A long wait lets one hot repo monopolize that slot and
# delay its lane (the observed processed=0 wedge before the keyed pool); this is head-of-line time, not lock
# aliasing. A short wait defers in ~1s, preserving same-repo order without spending a durable attempt, while
# unrelated accounts continue on the pool's other slots.
# Set == VERIPSA_DB_LOCK_TIMEOUT_MS to restore the old blocking behavior (its own kill switch). Applies ONLY to the
# two live event flows; background sites (boot reconcile, co-change pool, install fan-out) keep the long wait.
_CONVERGENCE_LOCK_WAIT_MS = env_int("VERIPSA_CONVERGENCE_LOCK_WAIT_MS", 1_000, min_value=1)
_DB_CONNECT_TIMEOUT_SECONDS = env_int(
    "VERIPSA_DB_CONNECT_TIMEOUT_SECONDS", 10, min_value=1, max_value=60,
)


def _event_timeout_ms(configured_ms: int) -> int:
    """A Postgres timeout capped by the one delivery's remaining wall budget."""
    _event_budget.raise_if_expired()
    rem = _event_budget.remaining()
    if rem is None:
        return max(1, int(configured_ms))
    return max(1, min(int(configured_ms), int(rem * 1000)))


def _connect_event_db(dsn: str, statement_timeout_ms: int):
    """Open a live-event connection with its first SQL already budget-bounded."""
    # Startup options take effect before libpq exposes the connection. Without
    # them, the first ``SET search_path`` round-trip is itself an unbounded seam:
    # the later session SETs cannot protect the statement that establishes them.
    statement_ms = _event_timeout_ms(statement_timeout_ms)
    lock_ms = _event_timeout_ms(_CONVERGENCE_LOCK_WAIT_MS)
    # Check the whole-second libpq allowance last, immediately before entering
    # connect. Rounding a sub-second remainder up to one would outlive the event;
    # for >=1s, floor so the integer timeout never exceeds the observed budget.
    seconds = _event_budget.timeout_for(_DB_CONNECT_TIMEOUT_SECONDS)
    if seconds < 1.0:
        raise _event_budget.EventBudgetExceeded(
            "less than one second remains for the webhook DB connection")
    event_deadline = _event_budget.current_deadline()
    connect_deadline = _db_connect.deadline_after(
        _DB_CONNECT_TIMEOUT_SECONDS, event_deadline)
    try:
        return _db_connect.connect(
            psycopg2.connect,
            dsn,
            deadline=connect_deadline,
            connect_timeout=_DB_CONNECT_TIMEOUT_SECONDS,
            options=(
                f"-c statement_timeout={statement_ms} "
                f"-c lock_timeout={lock_ms} "
                "-c search_path=core"
            ),
        )
    except _db_connect.DatabaseConnectDeadlineExceeded as error:
        remaining = _event_budget.remaining()
        if event_deadline is not None and remaining is not None and remaining <= 0:
            raise _event_budget.EventBudgetExceeded(
                "webhook DB connection exceeded its event deadline") from error
        raise


def _budgeted_scoped_db(conn, base_runner, statement_timeout_ms: int):
    """Tighten statement_timeout before every body query as the event budget shrinks.

    Setting it only once at connection setup is insufficient: GitHub calls between
    SQL statements may consume most of the budget, leaving a late query with the
    original many-second allowance.  The SET LOCAL and query share the same body
    transaction; rollback still removes every partial event write.
    """
    def run(sql, args=()):
        timeout_ms = _event_timeout_ms(statement_timeout_ms)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = %s" % timeout_ms)
        return base_runner(sql, args)
    return run


def _commit_with_event_budget(conn, statement_timeout_ms: int) -> None:
    """Tighten the transaction timeout at the last rollback-safe boundary.

    A handler can spend most of the delivery allowance in GitHub calls after
    its last DB query. The earlier SET LOCAL would then be stale. Re-arm it from
    the current remainder and check once more after that SQL round-trip so the
    COMMIT command itself inherits the final bound.
    """
    timeout_ms = _event_timeout_ms(statement_timeout_ms)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = %s" % timeout_ms)
    _event_budget.raise_if_expired()
    conn.commit()


def _pop_delivery_execution_authority(payload: dict) -> _DeliveryExecutionAuthority | None:
    """Remove and validate DeliveryStore's one-call exact-lease capability.

    This is deliberately the first operation after top-level payload coercion:
    router/handler code never sees execution authority, and a persisted or
    caller-forged scalar marker cannot authorize durable finalization.
    """
    authority = payload.pop(_DELIVERY_EXECUTION_AUTHORITY, None)
    if authority is None:
        return None
    if type(authority) is not _DeliveryExecutionAuthority:
        raise RuntimeError("webhook delivery execution authority is malformed")
    key = payload.get("_veripsa_delivery_key")
    if (not isinstance(key, str) or not key or len(key) > 200
            or authority.key != key
            or isinstance(authority.lease_generation, bool)
            or not isinstance(authority.lease_generation, int)
            or authority.lease_generation < 1):
        raise RuntimeError("webhook delivery execution authority is malformed")
    return authority


def _finalize_body_transaction(
        conn, statement_timeout_ms: int,
        authority: _DeliveryExecutionAuthority | None,
) -> _DeliveryExecutionAuthority | None:
    """Stage exact durable finish and commit the body exactly once.

    Before finish is staged, every failure is ordinary rollback-safe processor
    failure. After a true finish result, a COMMIT exception is unknowable to
    the client: preserve it inside a BaseException-only private unwind so
    DeliveryStore can resolve the exact generation on a fresh connection.
    """
    finish_staged = False
    try:
        if authority is not None:
            timeout_ms = _event_timeout_ms(statement_timeout_ms)
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = %s" % timeout_ms)
                _event_budget.raise_if_expired()
                cur.execute(
                    "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
                    (authority.key, authority.lease_generation),
                )
                row = cur.fetchone()
                if not row or row[0] is not True:
                    raise RuntimeError(
                        "durable delivery exact finish lost lease authority")
                finish_staged = True
            # The timeout was tightened immediately before exact finish, so no
            # further SQL belongs after it: check locally, then send one COMMIT.
            _event_budget.raise_if_expired()
            conn.commit()
        else:
            _commit_with_event_budget(conn, statement_timeout_ms)
    except _event_budget.EventBudgetExceeded as error:
        if finish_staged:
            raise _DeliveryCommitAmbiguity(authority, error) from error
        raise
    except Exception as error:
        if finish_staged:
            raise _DeliveryCommitAmbiguity(authority, error) from error
        raise
    return authority


def _intentional_lock_deferral(reason: str) -> IntentionalDeliveryDeferral:
    """A content-free, attempt-neutral retry for a positively identified live-vs-background lock wait."""
    return IntentionalDeliveryDeferral(
        reason,
        datetime.now(timezone.utc) + timedelta(seconds=_LIVE_LOCK_RETRY_SECONDS),
    )


def _is_account_lifecycle_lock_timeout(exc: BaseException) -> bool:
    """True only for the SQL generation-admission boundary's fixed diagnostic marker.

    SQLSTATE 55P03 is also used by ordinary row/advisory lock failures.  Those remain real failures; accepting only
    this marker prevents a body bug or unrelated lock from being hidden as healthy background contention.
    """
    return (
        isinstance(exc, psycopg2.errors.LockNotAvailable)
        and getattr(exc, "pgcode", None) == "55P03"
        and getattr(getattr(exc, "diag", None), "constraint_name", None)
        == _ACCOUNT_LIFECYCLE_LOCK_CONSTRAINT
    )


def _installation_admission(cur, installation_id: str, created_at: str | None,
                            *, delivery_key: str | None = None) -> dict:
    """Run the DB generation fence and require its typed response shape.

    Only the marker emitted by that function's account advisory-lock boundary is an expected background wait.
    A durable delivery is scheduled without spending an attempt; non-durable work and every unmarked 55P03 retain
    their fail-loud behavior.
    """
    try:
        cur.execute(
            "SELECT core.admit_event_installation_generation_with_authority(%s,%s::timestamptz)",
            (installation_id, created_at),
        )
    except psycopg2.errors.LockNotAvailable as exc:
        if delivery_key and _is_account_lifecycle_lock_timeout(exc):
            raise _intentional_lock_deferral("account lifecycle convergence is busy") from exc
        raise
    row = cur.fetchone()
    result = row[0] if row else None
    if (not isinstance(result, dict) or result.get("ok") is not True
            or not isinstance(result.get("admitted"), bool)
            or not isinstance(result.get("proof_required"), bool)):
        raise RuntimeError("installation generation admission returned malformed authority")
    return result


def _take_live_repository_locks(cur, repository_id, account_key, repo, *,
                                take_coordinate: bool, delivery_key: str | None,
                                lock_wait_ms: int | None = None) -> bool:
    """Take the two explicit live serialization locks, deferring only their own durable lock timeout.

    Keep the catch immediately around these calls.  A 55P03 raised later by a handler/body is not equivalent and
    must continue through the ordinary failure/retry/DLQ path. lock_wait_ms (default None → global) passes the
    SHORT live convergence wait so a contended lock defers fast instead of pinning one pool slot and keyed lane.
    """
    try:
        _take_repository_id_lock(cur, repository_id, lock_timeout_ms=lock_wait_ms)
        if take_coordinate:
            _take_repo_lock(cur, account_key, repo, lock_timeout_ms=lock_wait_ms)
    except psycopg2.errors.LockNotAvailable as exc:
        if delivery_key:
            raise _intentional_lock_deferral("repository convergence is busy") from exc
        raise
    return bool(take_coordinate)


def _note_installation_account_metadata_outside_body(
        conn, account_key: str | None, account_login: str | None,
        account_type: str | None) -> None:
    """Record public account metadata without widening the event transaction.

    ``installation_account`` is one row per installation/account, while event
    bodies are serialized per repository.  Updating that shared row inside a
    repository body transaction holds its row lock across GitHub and graph
    work, accidentally serializing otherwise-independent repositories.  Keep
    this advisory metadata write in a short autocommit statement instead.  The
    SQL gate still binds the write to the already-routed account, and a later
    atomic generation recheck remains authoritative for business mutations.
    """
    if getattr(conn, "autocommit", None) is not True:
        raise RuntimeError(
            "installation account metadata must be recorded outside the event body transaction")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT core.note_installation_account_metadata_with_authority(%s,%s,%s)",
            (account_key, account_login, account_type),
        )


def _ordinary_installation_proof(gh, payload: dict, expected_account_id: str) -> dict | None:
    """Exact App-JWT generation proof for an ordinary webhook.

    Unlike installation lifecycle payloads, ordinary GitHub webhooks commonly carry only ``installation.id``;
    their owning account comes from ``repository.owner.id``. Bind the exact App endpoint response to both signed
    coordinates without inventing a requirement for ``installation.account`` in those payloads.
    """
    _s = _server()
    installation_id = _event_installation_id(payload)
    if installation_id is None:
        raise RuntimeError("ordinary installation proof needs installation.id")
    point_read = getattr(gh, "app_installation_identity", None)
    if not callable(point_read):
        raise RuntimeError("GitHub client lacks installation generation point-read authority")
    current = _s._validated_installation_proof(
        point_read(installation_id), "installation generation point read")
    if current is None:
        return None
    expected = str(expected_account_id)
    if expected.startswith("ACCT-GH-"):
        expected = expected[8:]
    if current["installation_id"] != installation_id or current["account_id"] != expected:
        raise RuntimeError("installation generation point read mismatched the webhook identity")
    if current["suspended"]:
        return None
    return current


def _account_consistency_violation(payload: dict) -> str | None:
    """STRUCTURAL owner↔installation authority check (moat red-team F2 — the founder's exact worry).

    _event_account_key picks the tenant from repository.owner.id (1st choice) … installation.id (4th) with NO
    assertion the sources AGREE. HMAC only proves GitHub DELIVERED the event — NOT that the repository in the body
    is owned by the account that owns the DELIVERING installation. So a validly-signed event could pair one
    installation's authenticated delivery with a repository.owner.id naming ANOTHER account, and the whole identity
    moat would silently route the event into a tenant the delivering installation does not own.

    Make it STRUCTURAL on the PAYLOAD: GitHub stamps the full `installation` object — INCLUDING its `account` (the
    account that owns the delivering installation, in the SAME id space as repository.owner.id) — onto every
    installation-bearing webhook. When BOTH a repository.owner.id-derived account AND that installation.account.id
    are present, ASSERT they are equal; a mismatch is a provable forgery/bug → return a content-free reason (the
    caller NO-OPs/drops the event). This is the structural reconciliation the founder asked for: the repo this
    event routes by must belong to the account that owns the installation GitHub delivered it for.

    BEHAVIOR-PRESERVING + cheap by construction:
      • ZERO network, ZERO new failure surface on the hot path — both ids are already in the body (the value
        _event_account_key itself reads). The overwhelmingly common live case (a push/PR on an installed repo:
        repository.owner.id == installation.account.id) returns None = proceeds, unchanged.
      • When only ONE (or neither) source resolves there is NOTHING to cross-check → return None (proceed): an
        install/marketplace event with no top-level repository, or a (malformed) repository event carrying no
        installation.account, keeps its existing routing — _event_account_key already keys such an event by
        repository.owner.id with no second source to contradict, so there is no consistency claim to violate.
    The STRONGER authenticated reference (gh.installation_account_id() — the owner id of the installation GitHub
    authenticated the delivery for) is deliberately NOT consulted here: it is a per-event network round-trip on the
    live path and a new failure mode, and the boot/co-change paths that have no payload ALREADY derive the tenant
    from exactly that authenticated id (server.boot_reconcile / cochange), so the authenticated owner is the moat
    on those paths. On the live path the payload's own installation.account is the authoritative, free reference.
    Never raises (pure payload reads through _as_obj)."""
    owner_acct = _event_owner_account_id(payload)
    if owner_acct is None:
        return None                                   # no repository.owner.id to cross-check → nothing to assert
    inst_acct = _event_installation_account_id_from_payload(payload)
    if inst_acct is None:
        return None                                   # no installation.account.id in the body → nothing to compare
    if inst_acct != owner_acct:
        return "repository.owner.id disagrees with installation.account.id"
    return None                                       # consistent — proceed (the overwhelmingly common case)


# The (event_type, action) → payload list-field that names the repos an install/uninstall event fans out over.
# These are the ONLY events whose queue-only activation or purge touches per-repo lifecycle/graph-request rows but
# carries NO top-level `repository` (so _event_repo is None and the live single-connection body would run them
# unlocked). The bounded fan-out gives each repository its own short DB transaction and durable checkpoint; it
# never performs HEAD/PR reads, clone, extraction, or customer-surface posting. Top-level repository lifecycle
# events are intentionally NOT here: they already route through the single-event body, with multi-coordinate
# actions taking their sorted lock set inside the DB.
_INSTALL_FANOUT_FIELD = {
    ("installation", "created"): "repositories",
    ("installation", "unsuspend"): "repositories",
    ("installation", "new_permissions_accepted"): "repositories",
    # NOTE: ("installation","deleted") is DELIBERATELY NOT fanned out per-repo (audit r4 privacy fix). The
    # uninstall purge is ACCOUNT-WIDE — it must forget EVERY repo's working set, including repos GitHub did not
    # name in the payload's `repositories` array (omitted for an "All repositories" install — the common case).
    # So an uninstall falls through to handle_event, which calls purge_account_working_set (no repo filter). The
    # per-repo advisory lock the fan-out provided is not needed for a full uninstall: the token is being revoked
    # and the account-wide DELETE is one tenant-pinned txn. (installation_repositories:removed stays per-repo —
    # it names ONE repo authoritatively and the account survives.)
    #
    # NOTE: ("installation","suspend") is LIKEWISE NOT fanned out per-repo (same shape as "deleted"). A suspend
    # releases the account's in-flight lanes ACCOUNT-WIDE (graph kept — suspend is reversible, unlike uninstall),
    # so it must reach every repo's lanes including ones GitHub did not name (the `repositories` array is omitted
    # for an "All repositories" install — the common case). So a suspend falls through to handle_event, which calls
    # the account-wide release (no repo filter) in one tenant-pinned txn. Unsuspend stays fanned out so each
    # repository activation and durable onboarding enqueue is independently checkpointed.
    ("installation_repositories", "added"): "repositories_added",
    ("installation_repositories", "removed"): "repositories_removed",
}

_FANOUT_PLAN_FIELD = "_veripsa_fanout_plan"
_FANOUT_COMPLETED_FIELD = "_veripsa_fanout_completed"
_FANOUT_PLAN_CAP = 500
_FANOUT_REPOS_PER_SLICE = env_int(
    "VERIPSA_FANOUT_REPOS_PER_SLICE", 4, min_value=1, max_value=50)
_FANOUT_SLICE_SECONDS = env_int(
    "VERIPSA_FANOUT_SLICE_SECONDS", 20, min_value=1, max_value=80)
_FANOUT_REPO_WORK_SECONDS = env_int(
    "VERIPSA_FANOUT_REPO_WORK_SECONDS", 20, min_value=1, max_value=80)
_FANOUT_MIN_NEXT_REPO_SECONDS = env_int(
    "VERIPSA_FANOUT_MIN_NEXT_REPO_SECONDS", 10, min_value=1, max_value=60)
_FANOUT_SLICE_RETRY_SECONDS = env_int(
    "VERIPSA_FANOUT_SLICE_RETRY_SECONDS", 1, min_value=0, max_value=60)


def _canonical_fanout_proposal(repositories) -> list[dict]:
    """Build the exact id-first plan shape independently checked by Postgres.

    A stable id is authoritative when present; only genuinely id-less entries
    use the bounded coordinate fallback. Nothing malformed is skipped because
    silently shrinking a frozen plan would turn omitted work into false done.
    """
    if not isinstance(repositories, list):
        raise RuntimeError("durable fanout repositories must be a list")
    entries = []
    keys = {}
    names = {}
    for raw in repositories:
        if isinstance(raw, str):
            repo = {"full_name": raw}
        elif isinstance(raw, dict):
            repo = raw
        else:
            raise RuntimeError("durable fanout repository identity is malformed")
        full = repo.get("full_name")
        if (not isinstance(full, str) or not full or len(full) > 512
                or full != full.strip() or full.count("/") != 1
                or any(ord(char) < 32 or ord(char) == 127 for char in full)):
            raise RuntimeError("durable fanout repository coordinate is malformed")
        owner, name = full.split("/", 1)
        if not owner or not name:
            raise RuntimeError("durable fanout repository coordinate is malformed")

        raw_id = repo.get("id")
        if raw_id in (None, ""):
            repository_id = None
            key = "name:" + full
            entry = {"key": key, "full_name": full}
        else:
            if isinstance(raw_id, (bool, dict, list, tuple, set)):
                raise RuntimeError("durable fanout repository id is malformed")
            repository_id = str(raw_id).strip()
            if (not repository_id.isascii() or not repository_id.isdecimal()
                    or repository_id.startswith("0") or len(repository_id) > 32):
                raise RuntimeError("durable fanout repository id is malformed")
            key = "id:" + repository_id
            entry = {"key": key, "full_name": full, "id": repository_id}

        previous_key = names.get(full)
        previous_name = keys.get(key)
        if previous_key is not None:
            if previous_key != key:
                raise RuntimeError(
                    "durable fanout carries conflicting duplicate repository identity")
            continue
        if previous_name is not None and previous_name != full:
            raise RuntimeError(
                "durable fanout repository id names conflicting coordinates")
        names[full] = key
        keys[key] = full
        entries.append(entry)
    if len(entries) > _FANOUT_PLAN_CAP:
        raise RuntimeError("durable fanout repository plan exceeds its bounded cap")
    return sorted(entries, key=lambda entry: (entry["key"], entry["full_name"]))


def _install_fanout_entries(event_type, payload) -> list[dict]:
    field = _INSTALL_FANOUT_FIELD.get((event_type, payload.get("action")))
    if field is None:
        return []
    raw = _server()._as_list(payload.get(field))
    if not raw:
        return []
    return _canonical_fanout_proposal(raw)


def _install_fanout_repos(event_type, payload):
    """The repo full_names an install/uninstall event fans out over (onboard or purge), or [] if this is not a
    fanned-out install event. Content-free: only the repo coordinates the handler would already touch."""
    try:
        return [entry["full_name"] for entry in _install_fanout_entries(event_type, payload)]
    except RuntimeError as error:
        # Preserve the long-standing public helper's ValueError contract used
        # by direct/offline callers while the durable runtime fails closed.
        raise ValueError(str(error).replace("durable fanout", "install fan-out")) from error


# The install-level ONBOARDING actions (a fresh install / a resumed-from-suspended install / accepted new
# permissions) — the events whose handler CLONES + ingests each repo. GitHub OMITS the `repositories` array
# from these payloads when the install scope is "All repositories" (repository_selection == 'all' — the
# DEFAULT, most-common org install), so for those _install_fanout_repos returns [] even though there IS heavy
# per-repo onboarding work to do (the handler enumerates the install's repos itself). This is the set for which
# an EMPTY named list means "enumerate the installation's repos" (the all-repos fallback), NOT "no work":
#   * installation_repositories:added names its repos AUTHORITATIVELY (a delta) — an empty array there means no
#     repos were added, so it is NOT in this set (enumerating all repos on an empty delta would re-onboard the
#     whole org). The uninstall/remove (purge) actions are likewise excluded — a purge names its repos, and the
#     account-wide uninstall is deliberately NOT fanned out per-repo (it reaches handle_event account-pinned).
_INSTALL_ALLREPOS_ONBOARD_ACTIONS = {
    ("installation", "created"),
    ("installation", "unsuspend"),
    ("installation", "new_permissions_accepted"),
}


def _onboard_eager_cap() -> int:
    """The ingress onboarding cap (how many repo identities one installation delivery durably queues; the rest
    defer to a later authoritative repository event/push). This is ingest._ONBOARD_REPO_CAP — the SAME budget the
    named-list onboarding honors — resolved at CALL time (so a test or env retune of the cap on `ingest` is seen
    here too, and there is no load-time import of ingest). Fail safe to the documented default (50) if ingest is
    unavailable: a positive bound prevents an unbounded inventory/API or DB-enqueue burst."""
    try:
        try:
            import ingest as _ing  # call-time → no module-load coupling at import
        except ImportError:        # imported as a package
            from . import ingest as _ing  # type: ignore
        cap = int(getattr(_ing, "_ONBOARD_REPO_CAP", 50))
        return cap if cap >= 1 else 50
    except Exception:
        return 50


def _trace_prefix_for(payload):
    """Read the per-event trace_id prefix off the dispatcher-stashed payload key (`_veripsa_trace_id`), with a
    cycle-free lazy import (event_processor is imported by webhook only via server; webhook itself is a leaf, so
    this call-time import is safe from any load-order). Returns '' on a payload without one (a degraded but
    valid log line — better than a crash on an installation-ingress path that ran before _ensure_trace_id)."""
    try:
        from webhook import _trace_log_prefix
    except ImportError:
        from .webhook import _trace_log_prefix
    return _trace_log_prefix(payload)


def _install_allrepos_onboard_repos(event_type, payload, gh, *, strict=False):
    """The repos to onboard for an ALL-REPOSITORIES install whose payload named NO repos (repository_selection ==
    'all' → GitHub omits the `repositories` array). Mirrors ingest._onboard_entries's fallback exactly — ask the
    installation which repos it can see (prefer gh.installation_repo_entries so the stable repository id survives;
    fall back to the legacy name list) — but returns the bounded metadata list so make_db_processor can route it
    through the per-repo locked fan-out. Each repository's lifecycle activation plus durable onboarding enqueue
    commits in its own checkpointed transaction; no graph/PR work runs in this path, and one DB failure cannot
    roll back successful siblings. ONLY for install-level onboarding actions (a delta event names its repos
    authoritatively). Bounded by the ingress cap (a huge org cannot make one installation storm the API/DB; the
    rest defer to later authoritative traffic, exactly as the named path). Durable callers are strict: malformed
    or uncertain inventory raises so the inbox retries rather than freezing a false-empty plan. Legacy/direct
    callers retain the fail-open [] fallback. Content-free: repository full_name + stable numeric id only."""
    if (event_type, payload.get("action")) not in _INSTALL_ALLREPOS_ONBOARD_ACTIONS:
        return []
    # Carry the outer discovery attempt into the handler. Empty and transient-failure are materially different
    # from "not attempted": without this marker the shared fallback transaction would call the inventory API a
    # second time and could discover/enqueue repositories outside the frozen per-repo plan.
    marker = _server()._ALLREPOS_DISCOVERY_MARKER
    payload[marker] = "failed"
    entry_lister = getattr(gh, "installation_repo_entries", None)
    lister = entry_lister if callable(entry_lister) else getattr(gh, "installation_repos", None)
    if not callable(lister):
        return []
    _s = _server()
    cap = _onboard_eager_cap()
    try:
        entries, seen = [], set()
        raw_entries = _s._as_list(lister(cap=cap))
        for raw in raw_entries:
            obj = _s._as_obj(raw)
            full = obj.get("full_name") if obj else (raw if isinstance(raw, str) else None)
            if not isinstance(full, str) or not full:
                if strict:
                    raise RuntimeError(
                        "all-repositories inventory returned malformed repository identity")
                continue
            if full in seen and not strict:
                continue
            entry = {"full_name": full}
            if obj.get("id") not in (None, ""):
                entry["id"] = obj.get("id")
            seen.add(full)
            entries.append(entry)
        if strict and entries:
            # Validate every coordinate/id and reject identity conflicts before
            # any lifecycle/queue write. The DB independently canonicalizes again.
            entries = _canonical_fanout_proposal(entries)
        if entries:
            _tp = _trace_prefix_for(payload)
            print(f"{_tp}install all-repos onboard: payload named no repos → enumerated {len(entries)} via "
                  f"installation repository API (routed through the per-repo LOCKED fan-out)", flush=True)
        payload[marker] = "found" if entries else "empty"
        return entries
    except _event_budget.EventBudgetExceeded:
        # The inventory request consumed the delivery's one absolute budget.
        # This is terminal for the current in-memory generation and must remain
        # durable/retryable; converting it to [] would commit a false successful
        # "zero repositories onboarded" result and permanently finish the row.
        raise
    except Exception as e:   # transient list error → onboard nothing here (fail-open; live webhooks remain primary)
        if strict:
            # A durable delivery must not freeze a silently truncated plan.
            # External uncertainty and malformed inventory remain retryable.
            raise
        _tp = _trace_prefix_for(payload)
        print(f"{_tp}install all-repos onboard: installation_repos failed ({str(e)[:120]}) — deferring to live webhooks",
              flush=True)
        return []


def _validate_fanout_view(value) -> tuple[list[dict], set[str]]:
    if not isinstance(value, dict):
        raise RuntimeError("durable fanout prepare lost exact lease authority")
    raw_plan = value.get("plan")
    raw_completed = value.get("completed")
    plan = _canonical_fanout_proposal(raw_plan)
    if not plan or plan != raw_plan:
        raise RuntimeError("durable fanout prepare returned a noncanonical plan")
    if not isinstance(raw_completed, dict):
        raise RuntimeError("durable fanout completion checkpoint is malformed")
    plan_keys = {entry["key"] for entry in plan}
    completed = set()
    for key, done in raw_completed.items():
        if not isinstance(key, str) or done is not True or key not in plan_keys:
            raise RuntimeError("durable fanout completion checkpoint is malformed")
        completed.add(key)
    return plan, completed


def _prepare_fanout_on_cursor(
        cur, authority: _DeliveryExecutionAuthority, proposal: list[dict],
) -> tuple[list[dict], set[str]]:
    cur.execute(
        "SELECT core.prepare_webhook_delivery_fanout_with_authority(%s,%s,%s)",
        (authority.key, authority.lease_generation, Json(proposal)),
    )
    row = cur.fetchone()
    return _validate_fanout_view(row[0] if row else None)


def _prepare_durable_fanout(
        dsn: str, statement_timeout_ms: int,
        authority: _DeliveryExecutionAuthority, proposal: list[dict],
) -> tuple[list[dict], set[str]]:
    """Freeze/restore the plan before any repository does heavy work."""
    conn = _connect_event_db(dsn, statement_timeout_ms)
    deadline_guard = _event_budget.arm_connection_deadline(conn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SET statement_timeout = %s"
                % _event_timeout_ms(statement_timeout_ms))
            _event_budget.raise_if_expired()
            return _prepare_fanout_on_cursor(cur, authority, proposal)
    finally:
        deadline_guard.disarm()
        conn.close()


def _finish_already_completed_fanout(
        dsn: str, statement_timeout_ms: int,
        authority: _DeliveryExecutionAuthority, plan: list[dict],
) -> _DeliveryExecutionAuthority:
    """Finalize a retry whose immutable plan was wholly checkpointed already."""
    conn = _connect_event_db(dsn, statement_timeout_ms)
    deadline_guard = _event_budget.arm_connection_deadline(conn)
    try:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SET LOCAL statement_timeout = %s"
                    % _event_timeout_ms(statement_timeout_ms))
                current_plan, completed = _prepare_fanout_on_cursor(
                    cur, authority, plan)
            if current_plan != plan or completed != {
                    entry["key"] for entry in plan}:
                raise RuntimeError(
                    "durable fanout cannot finalize an incomplete plan")
            return _finalize_body_transaction(
                conn, statement_timeout_ms, authority)
        except _event_budget.EventBudgetExceeded:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
    finally:
        deadline_guard.disarm()
        conn.close()


def _stage_fanout_deferral(
        cur, authority: _DeliveryExecutionAuthority, reason: str,
        not_before: datetime | None = None,
) -> tuple[datetime, str]:
    """Atomically return a partially checkpointed delivery to the due queue."""
    retry_at = not_before or (
        datetime.now(timezone.utc)
        + timedelta(seconds=_FANOUT_SLICE_RETRY_SECONDS))
    bounded_reason = str(reason)[:300]
    cur.execute(
        "SELECT core.resolve_webhook_delivery_fanout_defer_with_authority("
        "%s,%s,%s,%s)",
        (authority.key, authority.lease_generation, retry_at,
         bounded_reason),
    )
    row = cur.fetchone()
    if not row or row[0] != "deferred":
        raise RuntimeError(
            "durable fanout deferral lost exact lease authority")
    return retry_at, bounded_reason


def _commit_fanout_deferral(
        conn, authority: _DeliveryExecutionAuthority,
        retry_at: datetime, reason: str,
) -> _DeliveryAtomicDeferralResult:
    """Commit a staged defer, preserving its exact ambiguity coordinates."""
    try:
        _event_budget.raise_if_expired()
        conn.commit()
    except _event_budget.EventBudgetExceeded as error:
        raise _DeliveryFanoutDeferralCommitAmbiguity(
            authority, error, retry_at, reason) from error
    except Exception as error:
        raise _DeliveryFanoutDeferralCommitAmbiguity(
            authority, error, retry_at, reason) from error
    return _DeliveryAtomicDeferralResult(authority, reason)


def _defer_durable_fanout(
        dsn: str, statement_timeout_ms: int,
        authority: _DeliveryExecutionAuthority, plan: list[dict],
        reason: str,
        not_before: datetime | None = None,
) -> _DeliveryAtomicDeferralResult:
    """Defer an already-partial plan in one exact, attempt-neutral commit."""
    conn = _connect_event_db(dsn, statement_timeout_ms)
    deadline_guard = _event_budget.arm_connection_deadline(conn)
    try:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SET LOCAL statement_timeout = %s"
                    % _event_timeout_ms(statement_timeout_ms))
                current_plan, _completed = _prepare_fanout_on_cursor(
                    cur, authority, plan)
                if current_plan != plan:
                    raise RuntimeError(
                        "durable fanout plan changed before deferral")
                retry_at, bounded_reason = _stage_fanout_deferral(
                    cur, authority, reason, not_before=not_before)
            return _commit_fanout_deferral(
                conn, authority, retry_at, bounded_reason)
        except _event_budget.EventBudgetExceeded:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
    finally:
        deadline_guard.disarm()
        conn.close()


def _process_install_event_per_repo_locked(
        dsn, event_type, payload, account_key, gh, repos, generation_proof=None,
        execution_authority: _DeliveryExecutionAuthority | None = None,
        completed_keys: set[str] | None = None):
    """Run a bounded installation lifecycle plan as short per-repository DB transactions.

    Installation events carry no top-level `repository`, while activation/enqueue and repo-scoped purge mutate
    the same lifecycle/request coordinates as ordinary events. Each frozen stable-id-first entry therefore gets
    the normal repository lock, tenant pin, handler transaction, and same-transaction durable checkpoint before
    the next entry. Activation handlers are queue-only: they bind lifecycle authority and enqueue account-fair
    convergence, with no default-HEAD/PR reads, graph extraction, or GitHub surface mutation while this lock is
    held. Removal handlers perform their bounded purge. A failure rolls back only that repository, so successful
    siblings remain checkpointed and redelivery skips them; one lock is held at a time, avoiding cross-repository
    deadlock. Authorityless/direct callers retain the legacy idempotent per-repository commit path."""
    _s = _server()
    field = _INSTALL_FANOUT_FIELD[(event_type, payload.get("action"))]
    results = []
    failures = 0
    _tp = _trace_prefix_for(payload)
    _delivery = payload.get("_veripsa_delivery_key") if isinstance(payload, dict) else None
    account_login, account_type = _event_account_metadata(payload)
    lifecycle_action = (event_type, payload.get("action"))
    activation_route = lifecycle_action in {
        ("installation", "created"),
        ("installation", "unsuspend"),
        ("installation", "new_permissions_accepted"),
        ("installation_repositories", "added"),
    }
    installation_id = _event_installation_id(payload)
    statement_timeout_ms = env_int(
        "VERIPSA_DB_STATEMENT_TIMEOUT_MS", 600_000, min_value=1)
    durable_plan = (
        _canonical_fanout_proposal(repos)
        if execution_authority is not None else None
    )
    completed = set(completed_keys or ())
    if durable_plan is not None:
        plan_keys = {entry["key"] for entry in durable_plan}
        if not durable_plan or not completed.issubset(plan_keys):
            raise RuntimeError("durable fanout execution view is malformed")
    else:
        plan_keys = set()
    completed_at_slice_start = set(completed)
    checkpointed_this_slice = 0
    slice_started = time.monotonic()

    for repo_value in repos:
        if (execution_authority is not None
                and time.monotonic() - slice_started >= _FANOUT_SLICE_SECONDS):
            # Check the slice wall before starting another repository, including after an ordinary failed repo.
            # The old code checked only after a successful checkpoint, so four ~20s failures marched through one
            # generation and recreated the observed 80s × eight durable generations multiplier.
            if completed != completed_at_slice_start:
                return _defer_durable_fanout(
                    dsn, statement_timeout_ms, execution_authority,
                    durable_plan, "durable_fanout_budget_slice")
            if failures:
                break
            raise RuntimeError("durable fanout slice exhausted without progress")
        repo_obj = _s._as_obj(repo_value)
        full = repo_obj.get("full_name") if repo_obj else (repo_value if isinstance(repo_value, str) else None)
        if not isinstance(full, str) or not full:
            if execution_authority is not None:
                raise RuntimeError("durable fanout repository identity is malformed")
            continue
        repo_key = repo_obj.get("key") if execution_authority is not None else None
        if execution_authority is not None:
            if not isinstance(repo_key, str) or repo_key not in plan_keys:
                raise RuntimeError("durable fanout repository key is malformed")
            if repo_key in completed:
                results.append({"repo": full, "fanout_checkpoint": "already_completed"})
                continue
        # NARROW the payload to THIS one repo so the unchanged router onboards/purges exactly it (every other field
        # — action, installation, sender — is preserved so handle_event's installation scoping/branching is identical).
        original = next((_s._as_obj(r) for r in _s._as_list(payload.get(field))
                         if _s._as_obj(r).get("full_name") == full), {})
        narrowed = {"full_name": full}
        repo_id = repo_obj.get("id") if repo_obj.get("id") not in (None, "") else original.get("id")
        if repo_id not in (None, ""):
            narrowed["id"] = repo_id
        one = dict(payload)
        one.pop(_FANOUT_PLAN_FIELD, None)
        one.pop(_FANOUT_COMPLETED_FIELD, None)
        one[field] = [narrowed]
        # One pathological repository must not consume the whole 90-second
        # delivery allowance on every one of eight durable attempts (the
        # observed ~787s failure mode). Narrow only the work deadline here;
        # the parent event's terminal reserve remains intact for exact
        # checkpoint defer/release after this child expires.
        slice_remaining = max(
            0.001,
            float(_FANOUT_SLICE_SECONDS)
            - (time.monotonic() - slice_started),
        )
        repo_work_token = (
            _event_budget.begin_work(min(
                float(_FANOUT_REPO_WORK_SECONDS),
                slice_remaining,
            ))
            if execution_authority is not None else None
        )
        try:
            conn = _connect_event_db(dsn, statement_timeout_ms)
            deadline_guard = _event_budget.arm_connection_deadline(conn)
        except BaseException:
            if repo_work_token is not None:
                _event_budget.end_work(repo_work_token)
            raise
        repo_lock_taken = False
        checkpoint_staged = False
        setup_generation_rejected = False
        try:
            conn.autocommit = True   # session-scoped lock + tenant GUC must outlive each statement on this connection
            unrouted = False
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                # Stable ID precedes the mutable coordinate lock. Keep it session-held through this repository's
                # lifecycle gate→durable enqueue/purge→checkpoint transaction so transfer/offboard cannot cross it.
                repo_lock_taken = _take_live_repository_locks(
                    cur, repo_id, account_key, full,
                    take_coordinate=not _lifecycle_owns_coordinate_locks(event_type, one),
                    delivery_key=_delivery,
                    lock_wait_ms=_CONVERGENCE_LOCK_WAIT_MS,
                )
                if account_key is not None:
                    route_fn = ("enter_installation_with_authority" if activation_route
                                else "enter_existing_installation_with_authority")
                    cur.execute(f"SELECT core.{route_fn}(%s)", (account_key,))
                    routed = cur.fetchone()
                    if not activation_route and (not routed or routed[0] in (None, "")):
                        if execution_authority is None:
                            print(f"{_tp}install-event repo={full} ignored: no live installation route", flush=True)
                            return {"event": event_type, "action": payload.get("action"), "per_repo": results,
                                    "unrouted": True}
                        unrouted = True
                    if not activation_route and installation_id is not None and not unrouted:
                        # A proof-bearing admission may advance the durable
                        # generation, so do that short exclusive step before
                        # the repository body.  The body rechecks read-only and
                        # holds the shared lifecycle fence through its writes.
                        created_at = (generation_proof or {}).get("created_at")
                        admission = _installation_admission(
                            cur, installation_id, created_at, delivery_key=_delivery)
                        if not admission["admitted"]:
                            if execution_authority is None:
                                print(f"{_tp}install-event repo={full} ignored by installation generation fence: "
                                      f"{admission.get('reason', 'not admitted')}", flush=True)
                                return {"event": event_type, "action": payload.get("action"), "per_repo": results,
                                        "generation_admitted": False}
                            setup_generation_rejected = True
            if not unrouted and not setup_generation_rejected:
                _note_installation_account_metadata_outside_body(
                    conn, account_key, account_login, account_type)
            # BODY: one repo is one transaction. The session-scoped tenant pin + advisory lock above survive it,
            # while any graph/claim/lifecycle writes made before a later failure roll back together. This is
            # especially important when graph ingest succeeds but stable repository identity binding fails: the
            # durable delivery retries from a clean per-repo baseline rather than inheriting a half-onboarded graph.
            conn.autocommit = False
            try:
                current_completed = set(completed)
                if execution_authority is not None:
                    # Re-confirm exact ownership inside the SAME transaction before handler work. prepare() takes
                    # the delivery row lock and holds it through complete/commit, so complete(false) can safely mean
                    # "partial" rather than an indistinguishable stale-generation no-op.
                    with conn.cursor() as cur:
                        cur.execute(
                            "SET LOCAL statement_timeout = %s"
                            % _event_timeout_ms(statement_timeout_ms))
                        current_plan, current_completed = _prepare_fanout_on_cursor(
                            cur, execution_authority, durable_plan)
                    if current_plan != durable_plan:
                        raise RuntimeError("durable fanout plan changed after preparation")
                    if repo_key in current_completed:
                        conn.rollback()
                        completed = current_completed
                        results.append({"repo": full, "fanout_checkpoint": "already_completed"})
                        continue

                generation_rejected = setup_generation_rejected
                if (not generation_rejected and not activation_route
                        and installation_id is not None):
                    with conn.cursor() as cur:
                        admission = _installation_admission(
                            cur, installation_id, None, delivery_key=_delivery)
                        if not admission["admitted"]:
                            if execution_authority is None:
                                _commit_with_event_budget(conn, statement_timeout_ms)
                                print(f"{_tp}install-event repo={full} ignored by installation generation fence: "
                                      f"{admission.get('reason', 'not admitted')}", flush=True)
                                return {"event": event_type, "action": payload.get("action"), "per_repo": results,
                                        "generation_admitted": False}
                            generation_rejected = True

                if unrouted:
                    result = {"repo": full, "unrouted": True}
                elif generation_rejected:
                    result = {"repo": full, "generation_admitted": False}
                else:
                    _body_db = _budgeted_scoped_db(
                        conn, _s._scoped_db(conn),
                        statement_timeout_ms,
                    )
                    result = _s.handle_event(event_type, one, _body_db, gh)

                if execution_authority is None:
                    # Direct/offline compatibility: no unforgeable exact lease
                    # means no durable checkpoint/finalize authority.
                    _commit_with_event_budget(conn, statement_timeout_ms)
                    results.append(result)
                else:
                    # Completion is the transaction's final checkpoint SQL. The
                    # prepare row lock above makes its boolean unambiguous.
                    with conn.cursor() as cur:
                        cur.execute(
                            "SET LOCAL statement_timeout = %s"
                            % _event_timeout_ms(statement_timeout_ms))
                        _event_budget.raise_if_expired()
                        cur.execute(
                            "SELECT core.complete_webhook_delivery_fanout_repository_with_authority(%s,%s,%s)",
                            (execution_authority.key,
                             execution_authority.lease_generation,
                             repo_key),
                        )
                        complete_row = cur.fetchone()
                    complete_state = complete_row[0] if complete_row else None
                    if (not isinstance(complete_state, dict)
                            or not isinstance(complete_state.get("updated"), bool)
                            or not isinstance(complete_state.get("all_done"), bool)):
                        raise RuntimeError(
                            "durable fanout completion returned malformed authority")
                    if complete_state["updated"] is not True:
                        raise RuntimeError(
                            "durable fanout completion lost exact lease authority")
                    all_done = complete_state["all_done"]
                    expected_all_done = (
                        plan_keys - current_completed == {repo_key})
                    if all_done is not expected_all_done:
                        raise RuntimeError(
                            "durable fanout completion lost exact lease authority")
                    checkpoint_staged = True
                    results.append(result)
                    if all_done:
                        # Last repository business writes + checkpoint + exact
                        # delivery finish share this one commit. Any lost ACK
                        # uses the normal private commit resolver.
                        return _finalize_body_transaction(
                            conn, statement_timeout_ms, execution_authority)
                    checkpointed_this_slice += 1
                    elapsed = time.monotonic() - slice_started
                    remaining = _event_budget.work_remaining()
                    slice_exhausted = (
                        checkpointed_this_slice >= _FANOUT_REPOS_PER_SLICE
                        or elapsed >= _FANOUT_SLICE_SECONDS
                        or (remaining is not None
                            and remaining <= _FANOUT_MIN_NEXT_REPO_SECONDS)
                    )
                    if slice_exhausted:
                        # This is the critical anti-787s boundary. The repo's
                        # business writes, its completion checkpoint, and the
                        # attempt-neutral queued transition commit together.
                        # Recovery resumes from the immutable remaining set;
                        # this claim never marches toward the DLQ merely
                        # because a large installation needs several slices.
                        reason = "durable_fanout_slice"
                        with conn.cursor() as cur:
                            cur.execute(
                                "SET LOCAL statement_timeout = %s"
                                % _event_timeout_ms(statement_timeout_ms))
                            retry_at, bounded_reason = _stage_fanout_deferral(
                                cur, execution_authority, reason)
                        deferred_result = _commit_fanout_deferral(
                            conn, execution_authority,
                            retry_at, bounded_reason)
                        completed = set(current_completed)
                        completed.add(repo_key)
                        return deferred_result
                    _event_budget.raise_if_expired()
                    conn.commit()
                    completed = set(current_completed)
                    completed.add(repo_key)
            except _event_budget.EventBudgetExceeded:
                # Keep this typed branch explicit even if budget cancellation
                # later moves outside Exception (like asyncio cancellation).
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                # The lock is session-scoped, not transaction-scoped. Return to autocommit before the explicit
                # unlock; if the connection is already broken, close() below is the authoritative lock backstop.
                try:
                    conn.autocommit = True
                    if repo_lock_taken:
                        with conn.cursor() as cur:
                            _release_repo_lock(cur, account_key, full)
                except Exception:
                    pass
        except IntentionalDeliveryDeferral as defer:
            # Expected live-vs-background contention belongs to the durable scheduler, not this per-repo
            # best-effort accumulator. Propagate the typed wait so no poison attempt is spent.
            if (execution_authority is not None
                    and completed != completed_at_slice_start):
                return _defer_durable_fanout(
                    dsn, statement_timeout_ms, execution_authority,
                    durable_plan, defer.reason, not_before=defer.not_before)
            raise
        except _event_budget.EventBudgetExceeded as cancellation:
            # One absolute budget covers the WHOLE fan-out. Do not turn expiry
            # into one synthetic failure per remaining repository.
            if (execution_authority is not None
                    and completed != completed_at_slice_start):
                # The work deadline is exhausted, but EventBudget reserves a
                # small terminal tail precisely for returning exact durable
                # authority. Preserve the original cancellation if even that
                # bounded attempt-neutral defer cannot complete.
                try:
                    with _event_budget.terminal_scope():
                        return _defer_durable_fanout(
                            dsn, statement_timeout_ms, execution_authority,
                            durable_plan, "durable_fanout_budget_slice")
                except _DeliveryFanoutDeferralCommitAmbiguity:
                    raise
                except BaseException:
                    raise cancellation
            raise
        except Exception as e:   # never let one repo abort the rest of the install/uninstall sweep
            if checkpoint_staged:
                # COMMIT may have landed the business write + checkpoint. Stop
                # this generation; after exact release the retry reads the
                # durable completion set and either skips or re-runs safely.
                raise
            failures += 1
            print(f"{_tp}install-event repo={full} ({event_type}/{payload.get('action')}) FAILED "
                  f"(skipped, sweep continues): {str(e)[:160]}", flush=True)
            results.append({"repo": full, "install_event_error": str(e)[:200]})
            if execution_authority is not None:
                elapsed = time.monotonic() - slice_started
                remaining = _event_budget.work_remaining()
                if (
                    elapsed >= _FANOUT_SLICE_SECONDS
                    or (remaining is not None
                        and remaining <= _FANOUT_MIN_NEXT_REPO_SECONDS)
                ):
                    # A failure is not a checkpoint, but it still consumes the same slice wall. Stop this
                    # generation and let the exact release/recovery boundary retry from the unchanged checkpoint
                    # set. This is the missing failure-side half of the anti-787s boundary.
                    break
        finally:
            deadline_guard.disarm()
            conn.close()   # backstop: also drops the session lock if the explicit release never ran (dead conn)
            if repo_work_token is not None:
                _event_budget.end_work(repo_work_token)
    if failures:
        # Durable successful siblings are already checkpointed. Yield
        # attempt-neutrally so retry visits only failures; the authorityless
        # compatibility path still raises for its legacy idempotent replay.
        if (execution_authority is not None
                and completed != completed_at_slice_start):
            return _defer_durable_fanout(
                dsn, statement_timeout_ms, execution_authority, durable_plan,
                "durable_fanout_partial_retry")
        raise RuntimeError(f"{event_type}/{payload.get('action')} failed for {failures} repository(s)")
    if execution_authority is not None:
        # Every entry was already complete in the prepared view (typical after
        # a lost last-checkpoint ACK before finish). Finalize without rerunning
        # a handler, under the same exact finish/commit ambiguity protocol.
        return _finish_already_completed_fanout(
            dsn, statement_timeout_ms, execution_authority, durable_plan)
    return {"event": event_type, "action": payload.get("action"), "per_repo": results}


def make_db_processor(dsn: str):
    """The LIVE per-event processor (the EventQueue's `process`). Per event: ONE Postgres connection, held
    under a per-repo ADVISORY LOCK, so that even with MULTIPLE server instances (Render does a brief 2-instance
    overlap on every rolling deploy; scale-out would make it permanent) two events for the SAME repo are
    processed SERIALLY — the same-lane non-overlap guarantee, now safe across
    processes. DIFFERENT repos still run in parallel. (1) closes the connect-per-query problem, (2) makes the
    design horizontally scalable. The lock is held for the event incl. its GitHub API calls (acceptable: same
    repo events rarely overlap; closing the connection releases the lock even on error).

    ATOMIC PER EVENT (no partial-write on a DB blip): the event's MUTATING body runs in ONE transaction —
    commit on success, ROLLBACK on any error (a connection drop / statement timeout / query error MID-event).
    Without this, the body ran in autocommit, so a failure AFTER some of an event's writes committed but
    BEFORE the rest (e.g. a PR open RELEASES the head-branch's lanes, then dies before re-declaring the PR's
    claims) left a CORRUPT half-state committed: lanes freed but not re-held, some files claimed and not others,
    a graph patched but its landing not recorded. The all-or-nothing txn means a mid-event failure leaves the
    DB exactly as it was; the worker counts the event 'failed' and re-raises, Core recovery retries, and that retry
    re-runs the WHOLE event from a clean baseline (every gate write is idempotent — ON CONFLICT /
    converge-to-current — so a clean retry is a no-op-or-progress, never a double-apply). The advisory lock +
    tenant admission run in autocommit FIRST (session-scoped: the lock must outlive the body txn and the GUC route
    must cover the whole event). Only App-proven activation may provision; ordinary events resolve an existing
    live route and otherwise stop. Then the body txn opens. GitHub posts made mid-event are idempotent, so a
    rollback-then-retry re-posts the same content — the customer never sees a torn comment from a DB blip."""
    import psycopg2

    # FAIL-LOUD TIMEOUTS (read ONCE at construction so a typo'd knob refuses to start, not silently per-event):
    #   lock_timeout      — the cap on how long the BLOCKING pg_advisory_lock below may wait for a contended
    #                       per-repo lock. WITHOUT it that wait is UNBOUNDED: if some other session holds the lock
    #                       and never releases (a peer instance wedged mid-event, a stuck txn), THIS pool slot and
    #                       keyed lane block FOREVER inside pg_advisory_lock — the thread stays alive, so the lying-green
    #                       worker_stuck detector is the ONLY thing that even notices, and nothing self-corrects.
    #                       With it, the wait fails LOUD ("canceling statement due to lock timeout") → the event is
    #                       counted FAILED + logged with repo/account → Core recovery retries it later (idempotent).
    #                       The LIVE wait is bounded to the SHORT module-level _CONVERGENCE_LOCK_WAIT_MS (default 1s,
    #                       SET on the session below) so a contended convergence lock DEFERS fast instead of pinning
    #                       that slot/lane; background sites keep VERIPSA_DB_LOCK_TIMEOUT_MS via server_dbops.
    #   statement_timeout — the cap on any SINGLE query in the session (the lock wait + each body write). Bounds a
    #                       runaway/pathological query so it dies LOUD (→ failed → rollback → redeliver) instead of
    #                       pinning one scarce pool slot. GENEROUS for rolling compatibility and bounded lifecycle
    #                       batches (600s), while current installation ingress itself performs DB-local queue work.
    # Session-level SET (autocommit), so it covers the lock wait AND the subsequent body txn on the same connection.
    # Env-tunable; a non-int/out-of-range value fails SAFE via env_int (refuses to start, naming the knob). 0 is
    # DISALLOWED (min_value=1): 0 means "unlimited" in Postgres — the unbounded-hang this guards against.
    _stmt_timeout_ms = env_int("VERIPSA_DB_STATEMENT_TIMEOUT_MS", 600_000, min_value=1)

    def process(event_type, payload, _db_unused, gh, coalesce=None):
        # COERCE FIRST: a valid-but-non-dict top-level JSON body (null / [] / "x" / 42 — a deliberately
        # malformed or hostile webhook) must be a clean NO-OP, not an AttributeError. _event_repo /
        # _event_account_key (and the handlers below) all do payload.get(...), which raises on a non-dict.
        # Without this, such a body would be counted a worker 'failed' (→ pointless GitHub redelivery churn)
        # instead of being dropped as the empty event it is. Coerce once here so every downstream read is safe.
        _s = _server()
        payload = _s._as_obj(payload)
        execution_authority = _pop_delivery_execution_authority(payload)
        # PER-EVENT TRACE-ID (Round-2 observability follow-up): mint the trace_id at the TOP of the processor so
        # the install/uninstall FAN-OUT logs below + the dispatcher's per-repo handle_event calls all share the
        # SAME id (handle_event reads `_veripsa_trace_id` off the payload via _ensure_trace_id and reuses it
        # rather than minting a fresh one). An on-call greps one webhook delivery's whole trail across the
        # fan-out + the per-repo dispatches. Content-free (random uuid4 bytes only).
        try:
            from webhook import _ensure_trace_id, _trace_log_prefix  # cycle-free (webhook is leaf)
        except ImportError:
            from .webhook import _ensure_trace_id, _trace_log_prefix
        _ensure_trace_id(payload)
        _tp = _trace_log_prefix(payload)
        repo = _event_repo(payload)
        _delivery, _action, _pr, _head = "", "", "", ""
        try:
            _delivery = payload.get("_veripsa_delivery_key") if isinstance(payload, dict) else ""
            _action = payload.get("action") if isinstance(payload, dict) else ""
            _prj = _s._as_obj(payload.get("pull_request")) if isinstance(payload, dict) else {}
            _pr = (payload.get("number") if isinstance(payload, dict) else None) or _prj.get("number") or ""
            _head = _s._as_obj(_prj.get("head")).get("sha") or (payload.get("after") if isinstance(payload, dict) else "") or ""
            print(f"{_tp}webhook processing delivery={'present' if _delivery else 'missing'} "
                  f"event={event_type or 'missing'} "
                  f"action={_action or 'missing'} repo={repo or 'missing'} pr={_pr or 'missing'} "
                  f"head={str(_head)[:12] or 'missing'}", flush=True)
        except Exception:
            print(f"{_tp}webhook processing delivery=missing event={event_type or 'missing'} "
                  f"repo={repo or 'missing'}", flush=True)
        account_key = _event_account_key(payload)     # the STABLE GitHub account id (not the ephemeral install id)
        account_login, account_type = _event_account_metadata(payload)

        # OWNER↔INSTALLATION AUTHORITY (moat red-team F2): before pinning ANY tenant / taking ANY lock / dispatching,
        # assert the repository.owner.id this event routes by actually belongs to the installation GitHub delivered
        # it for (reconciled against the payload's own installation.account.id). HMAC proves the delivery, not
        # owner↔installation consistency, so a signed event could pair one installation's authenticated delivery
        # with another account's repo. On a PROVABLE mismatch this is a forgery/bug → DROP the event (clean no-op,
        # content-free log) rather than route it into a tenant the delivering installation does not own. FAIL-SOFT:
        # only a provable mismatch drops; when a source is absent there is nothing to assert so the event proceeds
        # (the consistent common case — owner.id == installation.account.id — is unchanged), and the check never raises.
        _acct_violation = _account_consistency_violation(payload)
        if _acct_violation:
            print(f"{_tp}event dropped (account-authority): {event_type}/{payload.get('action')} — {_acct_violation}",
                  flush=True)
            return                                    # no-op: no tenant pin, no lock, no handler dispatch

        # INSTALLATION GENERATION PROOF (before every DB/advisory lock). Webhook delivery is not globally ordered:
        # an old installation A delete can arrive after replacement B's create. Point-read activation generations
        # once and carry only bounded ids/created_at through fan-out; for delete, compare the account's CURRENT App
        # installation so stale A can never purge live B. Only a complete stable-account App-installations scan
        # proves absence; API uncertainty/malformed pagination/cap truncation raises and leaves the durable row
        # retryable.
        lifecycle_action = (event_type, payload.get("action"))
        activation_actions = {
            ("installation", "created"),
            ("installation", "unsuspend"),
            ("installation", "new_permissions_accepted"),
            ("installation_repositories", "added"),
        }
        generation_proof = None
        if lifecycle_action in activation_actions:
            activation_proof = _s._activation_installation_proof(gh, payload)
            if activation_proof is None:
                print(f"{_tp}lifecycle activation ignored before tenant admission: current installation absent, "
                      f"mismatched, or suspended", flush=True)
                return
            payload[_s._ACTIVATION_PROOF_MARKER] = activation_proof
        elif lifecycle_action == ("installation", "suspend"):
            installation = _s._as_obj(payload.get("installation"))
            suspended_id = installation.get("id")
            suspended_account = _s._as_obj(installation.get("account"))
            suspended_account_id = suspended_account.get("id")

            def bounded_suspend_id(value):
                if value in (None, "") or isinstance(value, (bool, dict, list, tuple, set)):
                    return None
                text = str(value).strip()
                return text if text and len(text) <= 64 else None

            suspended_id = bounded_suspend_id(suspended_id)
            suspended_account_id = bounded_suspend_id(suspended_account_id)
            if suspended_id is None or suspended_account_id is None:
                # This identity is immutable across redelivery.  A malformed target can never acquire generation
                # authority, so drop it before opening a tenant connection instead of churning the durable lane.
                print(f"{_tp}malformed installation suspend dropped before tenant admission", flush=True)
                return
            current_installation = _s._current_account_installation_proof(gh, payload)
            suspend_proof = {
                "state": "absent" if current_installation is None else "current",
                "suspended_installation_id": suspended_id,
                "account_id": suspended_account_id,
            }
            if current_installation is not None:
                suspend_proof["current"] = current_installation
            payload[_s._SUSPEND_PROOF_MARKER] = suspend_proof
        elif lifecycle_action == ("installation", "deleted"):
            installation = _s._as_obj(payload.get("installation"))
            deleted_id = installation.get("id")
            deleted_account = _s._as_obj(installation.get("account"))
            deleted_account_id = deleted_account.get("id")
            # Payload shape is immutable across redelivery. Missing/poison identity can never become deletion
            # authority by retrying, so drop it before tenant admission instead of permanently churning the durable
            # lane. External App point-read failures below still raise and retry, because those can recover.
            def bounded_id(value):
                if value in (None, "") or isinstance(value, (bool, dict, list, tuple, set)):
                    return None
                text = str(value).strip()
                return text if text and len(text) <= 64 else None

            deleted_id = bounded_id(deleted_id)
            deleted_account_id = bounded_id(deleted_account_id)
            if deleted_id is None or deleted_account_id is None:
                print(f"{_tp}malformed installation delete dropped before tenant admission", flush=True)
                return
            deleted_id = str(deleted_id).strip()
            deleted_account_id = str(deleted_account_id).strip()
            current_installation = _s._current_account_installation_proof(gh, payload)
            if current_installation is None:
                payload[_s._STALE_UNINSTALL_MARKER] = {
                    "state": "absent",
                    "deleted_installation_id": deleted_id,
                    "account_id": deleted_account_id,
                }
            elif current_installation["installation_id"] == deleted_id:
                # GitHub emitted the delete but its account-installation read model still exposes that exact
                # generation.  This is an intentional consistency wait, not poison work: DeliveryStore schedules
                # it without spending an attempt, and the durable account lane keeps later work ordered behind it.
                raise IntentionalDeliveryDeferral(
                    "deleted installation is still current; waiting for GitHub convergence",
                    datetime.now(timezone.utc) + timedelta(seconds=30),
                )
            else:
                payload[_s._STALE_UNINSTALL_MARKER] = {
                    "state": "replacement",
                    "deleted_installation_id": deleted_id,
                    "account_id": deleted_account_id,
                    "current": current_installation,
                }
                print(f"{_tp}stale installation delete ignored before tenant admission: a replacement generation "
                      f"is current; handing proof to the DB lifecycle fence", flush=True)
        elif lifecycle_action == ("installation_repositories", "removed"):
            # A removal fans out before the shared body path. Resolve its exact live installation once, before any
            # DB/repository lock, then carry only the bounded generation tuple into each atomic repo transaction.
            generation_proof = _s._activation_installation_proof(gh, payload)
            if generation_proof is None:
                print(f"{_tp}repository removal ignored before tenant admission: installation absent or suspended",
                      flush=True)
                return

        # INSTALL/UNINSTALL FAN-OUT (these carry no top-level `repository`, so route each repository through its
        # own advisory lock + transaction). A durable retry receives the immutable plan/checkpoint fields merged
        # into its claimed payload by Postgres. Restore that exact plan BEFORE consulting GitHub inventory: a
        # changed installation listing must never add/remove siblings halfway through one accepted delivery.
        stored_plan = payload.get(_FANOUT_PLAN_FIELD)
        stored_completed = payload.get(_FANOUT_COMPLETED_FIELD)
        if execution_authority is not None and (
                stored_plan is not None or stored_completed is not None):
            if lifecycle_action not in _INSTALL_FANOUT_FIELD:
                raise RuntimeError(
                    "durable fanout checkpoint is not allowed for this event/action")
            if stored_plan is None:
                raise RuntimeError(
                    "durable fanout completion exists without an immutable plan")
            proposal, claimed_completed = _validate_fanout_view({
                "plan": stored_plan,
                "completed": stored_completed,
            })
            if lifecycle_action in _INSTALL_ALLREPOS_ONBOARD_ACTIONS:
                # The handler's empty-list fallback must not enumerate again for
                # a retry restored from an all-repositories frozen plan.
                payload[_s._ALLREPOS_DISCOVERY_MARKER] = "found"
            fanout_plan, completed = _prepare_durable_fanout(
                dsn, _stmt_timeout_ms, execution_authority, proposal)
            if not claimed_completed.issubset(completed):
                raise RuntimeError(
                    "durable fanout checkpoint regressed after exact preparation")
            return _process_install_event_per_repo_locked(
                dsn, event_type, payload, account_key, gh, fanout_plan,
                generation_proof=generation_proof,
                execution_authority=execution_authority,
                completed_keys=completed,
            )

        fanout_entries = _install_fanout_entries(event_type, payload)
        if fanout_entries:
            if execution_authority is not None:
                fanout_entries, completed = _prepare_durable_fanout(
                    dsn, _stmt_timeout_ms, execution_authority, fanout_entries)
                return _process_install_event_per_repo_locked(
                    dsn, event_type, payload, account_key, gh, fanout_entries,
                    generation_proof=generation_proof,
                    execution_authority=execution_authority,
                    completed_keys=completed,
                )
            _process_install_event_per_repo_locked(
                dsn, event_type, payload, account_key, gh, fanout_entries,
                generation_proof=generation_proof,
            )
            return
        # GitHub omits `repositories` for an All-repositories install. Discover
        # its bounded ingress set once, then freeze and execute the same per-repo
        # locked plan used above. Durable discovery is strict/retryable so an
        # uncertain inventory cannot become a false empty completion; direct
        # compatibility callers retain the older fail-open empty fallback.
        allrepos = _install_allrepos_onboard_repos(
            event_type, payload, gh, strict=execution_authority is not None)
        if allrepos:
            if execution_authority is not None:
                allrepos, completed = _prepare_durable_fanout(
                    dsn, _stmt_timeout_ms, execution_authority, allrepos)
                return _process_install_event_per_repo_locked(
                    dsn, event_type, payload, account_key, gh, allrepos,
                    generation_proof=generation_proof,
                    execution_authority=execution_authority,
                    completed_keys=completed,
                )
            _process_install_event_per_repo_locked(
                dsn, event_type, payload, account_key, gh, allrepos,
                generation_proof=generation_proof,
            )
            return
        # An install/uninstall event with an EMPTY repo set (action matched but no repos in the payload, and no
        # all-repos enumeration above) is a clean no-op — fall through to the normal body, which (repo None) takes
        # no lock and the handler returns its empty 'purged: []' / 'onboarded: []'. No per-repo work to serialize.

        if account_key is None:
            print(f"{_tp}webhook ignored before tenant admission: no owning account authority", flush=True)
            return

        activation_route = lifecycle_action in activation_actions
        installation_id = _event_installation_id(payload)
        generation_fenced = installation_id is not None and event_type != "installation" and not activation_route

        conn = _connect_event_db(dsn, _stmt_timeout_ms)
        deadline_guard = _event_budget.arm_connection_deadline(conn)
        try:
            # SETUP (autocommit): only authenticated activation may provision. Ordinary/live/background work must
            # resolve an existing unrevoked route; otherwise a late event after erasure could recreate the tenant.
            # Generation admission runs before repository locks, with an App-JWT point read only on a DB cache miss.
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                # DEFENSE (fail-loud, not hang): bound the lock wait + any single query on this session BEFORE the
                # blocking advisory lock below. A contended lock / runaway query now RAISES (→ counted failed →
                # logged repo/account → Core recovery retries) instead of wedging one pool slot/lane indefinitely. These
                # session SETs also cover the body txn on the same connection. The values are validated ints
                # (env_int); the unit is ms; they are formatted as literals since Postgres SET takes no bind params.
                # lock_timeout uses the SHORT LIVE convergence wait (not the 30s VERIPSA_DB_LOCK_TIMEOUT_MS): covers the lifecycle
                # admission lock below AND the body's same-repo writes (already serialized by the repo lock, so
                # uncontended → the short wait never trips them), so a contended lifecycle/repo lock DEFERS in ~1s
                # rather than consuming this slot's wall-clock; unrelated accounts retain the other pool slots.
                cur.execute("SET lock_timeout = %s" % _event_timeout_ms(_CONVERGENCE_LOCK_WAIT_MS))
                cur.execute("SET statement_timeout = %s" % _event_timeout_ms(_stmt_timeout_ms))
                route_fn = ("enter_installation_with_authority" if activation_route
                            else "enter_existing_installation_with_authority")
                cur.execute(f"SELECT core.{route_fn}(%s)", (account_key,))
                routed = cur.fetchone()
                if not activation_route and (not routed or routed[0] in (None, "")):
                    print(f"{_tp}webhook ignored before tenant admission: no live installation route", flush=True)
                    return

                if generation_fenced:
                    created_at = (generation_proof or {}).get("created_at")
                    admission = _installation_admission(
                        cur, installation_id, created_at, delivery_key=_delivery)
                else:
                    admission = None

            if admission is not None and not admission["admitted"]:
                if admission["proof_required"] and generation_proof is None:
                    # Slow path only: verify the exact installation generation through the App endpoint. An honest
                    # 404/suspension is a completed stale no-op; malformed/transient authority raises for retry.
                    generation_proof = _ordinary_installation_proof(gh, payload, account_key)
                    if generation_proof is None:
                        print(f"{_tp}webhook ignored: installation generation absent or suspended", flush=True)
                        return
                    with conn.cursor() as cur:
                        admission = _installation_admission(
                            cur, installation_id, generation_proof["created_at"], delivery_key=_delivery)
                if not admission["admitted"]:
                    print(f"{_tp}webhook ignored by installation generation fence: "
                          f"{admission.get('reason', 'not admitted')}", flush=True)
                    return

            # Public login/type/last-seen metadata is advisory and already
            # scoped by the trusted route above.  Commit it separately before
            # the repository/body transaction: keeping this shared-account row
            # locked across slow handler work turns per-repository concurrency
            # into an account-wide lock-timeout convoy.
            _note_installation_account_metadata_outside_body(
                conn, account_key, account_login, account_type)

            with conn.cursor() as cur:
                _take_live_repository_locks(
                    cur, _s._as_obj(payload.get("repository")).get("id"), account_key, repo,
                    take_coordinate=bool(
                        repo and not _lifecycle_owns_coordinate_locks(event_type, payload)),
                    delivery_key=_delivery,
                    lock_wait_ms=_CONVERGENCE_LOCK_WAIT_MS,
                )
            # BODY (one transaction): every claim/graph/landing write of this event is now atomic. Commit on
            # success; on ANY error roll back the WHOLE event (no partial state) and re-raise so the worker
            # counts it 'failed' → Core recovery retries → the retry re-runs the event from a clean baseline.
            conn.autocommit = False
            try:
                atomic_fence_reason = None
                with conn.cursor() as cur:
                    if generation_fenced:
                        # Setup already proved/advanced the generation when needed. The atomic body recheck is
                        # deliberately read-only so it shares boot's lifecycle lock while still fencing a delete or
                        # replacement that wins between setup and this transaction.
                        admission = _installation_admission(
                            cur, installation_id, None, delivery_key=_delivery)
                        if not admission["admitted"]:
                            atomic_fence_reason = admission.get("reason", "not admitted")
                if atomic_fence_reason is None:
                    _body_db = _budgeted_scoped_db(conn, _s._scoped_db(conn), _stmt_timeout_ms)
                    _s.handle_event(event_type, payload, _body_db, gh, coalesce=coalesce)
                # The exact durable finish is the transaction's final business mutation. A false result raises
                # before commit and rolls the whole body back. Once true is staged, commit failure is ambiguous
                # and escapes through _DeliveryCommitAmbiguity for exact-generation resolution by DeliveryStore.
                committed_authority = _finalize_body_transaction(
                    conn, _stmt_timeout_ms, execution_authority)
            except _event_budget.EventBudgetExceeded:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    conn.rollback()                    # discard EVERY write this event made — no half-state survives
                except Exception:
                    pass                               # the conn is already broken (it dropped); close() cleans up
                raise                                  # re-raise → worker._failed += 1 → Core recovery retries cleanly
            # Everything below is post-COMMIT. No observer/log exception may turn the confirmed body+finish commit
            # into a processor failure which the wrapper could release/replay.
            if atomic_fence_reason is not None:
                try:
                    print(f"{_tp}webhook ignored by atomic installation generation fence: "
                          f"{atomic_fence_reason}", flush=True)
                except Exception:
                    pass
                return committed_authority
            try:
                print(f"{_tp}webhook processed delivery={'present' if _delivery else 'missing'} "
                      f"event={event_type or 'missing'} "
                      f"repo={repo or 'missing'} result=committed", flush=True)
            except Exception:
                pass
            # POST-COMMIT, OUTSIDE the body try (so a stats-refresh hiccup can NEVER roll back the committed event
            # nor mis-count it 'failed'/redeliver): if this event BULK-LOADED main's graph (a full re-ingest's
            # DELETE+INSERT of the whole coordinate), refresh the planner stats NOW so the FIRST main_impact_surface
            # plans the O(edges) adjacency correctly instead of hanging multi-minute on cold/empty stats (audit
            # #169). The graph is already correct on disk — only the PLAN is cold — so this is best-effort and
            # never-crash. ANALYZE can't run inside the body txn (it is a transaction block), AND a same-backend
            # ANALYZE right after the insert reads a stale 0-page relation size, so the helper runs it on a FRESH
            # short-lived connection that sees the true heap — see the helper's docstring for both constraints.
            _refresh_graph_stats_if_bulk_loaded(conn, dsn)
            return committed_authority
        except Exception as error:
            # ConnectionDeadlineGuard deliberately shuts down this exact session socket at the event work
            # deadline so a black-holed psycopg call cannot occupy a worker forever. psycopg surfaces that local
            # shutdown as an ordinary OperationalError/InterfaceError (commonly "SSL SYSCALL ... EOF"). Without
            # this translation DeliveryStore spends another durable failure generation instead of entering its
            # explicit cancellation boundary. `_DeliveryCommitAmbiguity` is a BaseException and therefore bypasses
            # this block: once exact finish is staged, the fresh-connection commit resolver remains authoritative.
            # Serialize with the timer callback before reading `fired`; otherwise an exception arriving at the
            # boundary could observe False and then have the timer win between this branch and `finally`.
            deadline_guard.disarm()
            if deadline_guard.fired:
                raise _event_budget.EventBudgetExceeded(
                    "webhook event database guard reached its work deadline"
                ) from error
            raise
        finally:
            deadline_guard.disarm()
            conn.close()   # releases the session advisory lock and closes the per-event connection
    process._veripsa_atomic_delivery_finalize_protocol = _DELIVERY_ATOMIC_FINALIZE_PROTOCOL
    return process


# A sentinel installation key for the readiness probe ONLY — it is pinned into the session GUC (set_config, txn-
# local) to exercise the App's service-identity resolution; it is NEVER inserted (no enter_installation call), so
# the probe creates no account/tenant row. The leading/trailing underscores keep it out of any real GH-id space.
_READYZ_INSTALLATION = "__veripsa_readyz__"


def app_identity_ok(dsn: str) -> tuple[bool, str | None]:
    """READINESS self-check for the deploy-blocker class of misconfig: can the App role actually RESOLVE its own
    identity the way an authenticated activation does? In prod the App connects as veripsa_app with NO per-account
    credential row; activation provisions/pins core.installation_account, while ordinary work resolves that route,
    and both then call core.resolve_session_identity(). If the service-identity path is missing, that resolve raises
    42501 ('no active credential') and EVERY event silently fails. So here we pin a SENTINEL installation account
    GUC (txn-local; no row created) and call resolve_session_identity() — the exact path the live event uses. It
    must return a non-empty account. Returns (ok, error). Cheap: one short-lived connection, no writes.

    SECURITY NOTE: resolve_session_identity now honors the App's core.installation_account pin ONLY when it is a
    GENUINELY-ROUTED account (a row exists in core.installation_account, written exclusively by the trusted
    enter_installation_with_authority route — a stray/forged raw `SET` to an unrouted account is ignored). So this
    self-check must exercise the REAL route: call enter_installation_with_authority(sentinel) (which routes the
    sentinel + pins the GUC) inside the txn, then resolve, then ROLL BACK — the probe-only sentinel account
    + its routing row are discarded, so the probe still leaves NOTHING behind while testing the exact live path."""
    import psycopg2
    conn = None
    try:
        conn = psycopg2.connect(dsn, connect_timeout=int(_DB_CONNECT_TIMEOUT_SECONDS))
        conn.autocommit = False                     # one txn; the enter+resolve below is rolled back, leaving no data
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            # Route the SENTINEL through the TRUSTED activation gate: it provisions the probe-only account + route
            # and pins core.installation_account, covering the same identity primitive ordinary resolution uses. The
            # whole txn is rolled back below, so neither the account nor the routing row persists — no tenant data.
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (_READYZ_INSTALLATION,))
            cur.execute("SELECT account FROM core.resolve_session_identity() AS r(agent, account)")
            row = cur.fetchone()
        conn.rollback()                             # discard the sentinel account + routing row; leaves nothing behind
        account = row[0] if row else None
        if not account:
            return False, "resolve_session_identity returned no account"
        return True, None
    except Exception as e:                          # 42501 here = the deploy-blocker misconfig → fail readiness loudly
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, str(e)[:200]
    finally:
        if conn is not None:
            conn.close()
