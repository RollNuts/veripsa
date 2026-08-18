"""Durable webhook delivery inbox for the hosted GitHub App.

The HTTP handler must ack fast, and GitHub does not automatically redeliver a later worker failure. This
module persists a minimized delivery before the 202, then the worker claims/processes/finishes it. It deliberately
does NOT store raw GitHub JSON: commit messages, PR bodies, and arbitrary text are stripped before DB persistence.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import Json

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
try:
    from delivery_deferral import IntentionalDeliveryDeferral
except ImportError:  # imported as a package
    from .delivery_deferral import IntentionalDeliveryDeferral
try:
    from runtime_protocols import DURABLE_RETRY_PROTOCOL
except ImportError:  # imported as a package
    from .runtime_protocols import DURABLE_RETRY_PROTOCOL


_MAX_PENDING = env_int("VERIPSA_DELIVERY_QUEUE_MAX_PENDING", 5000, min_value=1)
# DURABLE retry budget — its OWN, LARGER ceiling, distinct from the in-memory transient-retry budget (audit P1).
# The in-memory EventQueue retries a transient blip a few times WITHIN one process run (VERIPSA_EVENT_RETRY_ATTEMPTS,
# default 3); once those run out the durable row is released and its `attempts` keeps climbing across RECOVERY
# replays (a crash/deploy/transient-DB-outage that spans restarts). If the durable ceiling were the SAME small 3,
# a row that simply lived through one rolling deploy + two in-memory retries would hit `attempts>=max` and be
# dead-lettered to 'failed' — and because pending() filters attempts<max, boot-recovery would then NEVER replay it
# and GitHub will not redeliver a 202'd event → a PERMANENTLY LOST, invisible delivery. So the durable budget gets
# its own knob with a larger default (8): it tolerates several restart-spanning recovery cycles before a row is
# truly considered poison. Floored at the in-memory budget so the durable ceiling can never be the SMALLER of the
# two (a misconfig that would re-create the premature dead-letter); env-overridable upward for a noisier fleet.
_INMEM_RETRY_ATTEMPTS = env_int("VERIPSA_EVENT_RETRY_ATTEMPTS", 3, min_value=1)
_MAX_ATTEMPTS = max(_INMEM_RETRY_ATTEMPTS, env_int("VERIPSA_DELIVERY_MAX_ATTEMPTS", 8, min_value=1))
# One absolute database timestamp spans all ordinary durable generations. The
# EventQueue's per-dequeue budget is an inner cap, never a way to mint another
# full wall allowance after a release/reclaim cycle.
_DELIVERY_RETRY_WINDOW_SECONDS = env_int(
    "VERIPSA_DELIVERY_RETRY_WINDOW_SECONDS", 120, min_value=1, max_value=3600,
)
# Both the slow DLQ sweep and aged-blocker escalation share this single fixed
# automatic epoch. This is deliberately not an environment knob: operators use
# a genuine GitHub redelivery to authorize another epoch.
_MAX_AUTO_REARMS = 1


def _bounded_error_code(exc: BaseException) -> str:
    """Diagnosable exception metadata that cannot echo a delivery key or payload field."""
    raw_name = type(exc).__name__
    name = "".join(
        char if char.isascii() and (char.isalnum() or char == "_") else "_"
        for char in raw_name
    )[:64].lower() or "unknown"
    pgcode = getattr(exc, "pgcode", None)
    if (isinstance(pgcode, str) and len(pgcode) == 5 and pgcode.isascii()
            and pgcode.isalnum() and pgcode.upper() == pgcode):
        return f"exception_{name}_sqlstate_{pgcode}"
    return f"exception_{name}"
# A peer may only reclaim a stable owner after the longest valid delivery has
# had time to consume its *whole* wall budget, including terminalization, plus
# a small wall/transport skew margin.  The terminal reserve is already inside
# the wall budget; adding it once more is intentional headroom for a commit
# that reaches Postgres at the edge of that budget while the reaper observes a
# slightly newer clock.  Keep this value derived from the same validated event
# knobs instead of duplicating a hard-coded "90 seconds" in the lease protocol.
_OWNER_RECLAIM_CLOCK_MARGIN_SECONDS = 5
_DEAD_INSTANCE_RECLAIM_SAFE_SECONDS = int(math.ceil(
    float(_event_budget._EVENT_WALL_TIMEOUT_SECONDS)
    + float(_event_budget._EVENT_TERMINAL_RESERVE_SECONDS)
    + _OWNER_RECLAIM_CLOCK_MARGIN_SECONDS
))
# Ordinary stale reclaim is an independent path, so a configured stale window
# below the owner-safe ceiling would bypass every heartbeat safeguard.  Refuse
# that deployment at import rather than silently making the reaper safe while
# leaving ordinary claim recovery unsafe.
_STALE_SECONDS = env_int(
    "VERIPSA_DELIVERY_STALE_SECONDS",
    1800,
    min_value=_DEAD_INSTANCE_RECLAIM_SAFE_SECONDS,
)
_RECOVER_LIMIT = env_int("VERIPSA_DELIVERY_RECOVER_LIMIT", 100, min_value=1, max_value=1000)
_RECOVER_INTERVAL = env_int(
    "VERIPSA_DELIVERY_RECOVER_INTERVAL", 5, min_value=1, max_value=60,
)
_RECOVERY_MAX_QUEUE_DEPTH = env_int("VERIPSA_DELIVERY_RECOVERY_MAX_QUEUE_DEPTH", 20, min_value=1)
# DEAD-LETTER RE-ARM cadence (audit P1 — the poison/transient-outage row that exhausted even the durable budget).
# A row that hit 'failed' is the DLQ: pending() will NOT replay it (attempts>=max), so without this it is lost +
# invisible. The DLQ sweep re-arms 'failed' rows back to 'queued' (attempts reset) on a SLOW cadence — long enough
# that a genuinely poison row is not hot-looped (the watchdog alert is the human signal meanwhile), short enough
# that a transient multi-hour outage's casualties eventually get one more honest try. Default 1h; 0 disables the
# re-arm (alert-only DLQ). A 'failed' row older than this re-arm age is requeued for exactly one fresh attempt-budget.
_DLQ_REARM_SECONDS = env_int("VERIPSA_DELIVERY_DLQ_REARM_SECONDS", 3600, min_value=0)
# AGED-DEFERRED LANE ESCALATION age (issue #847 — the deferred delivery that was never replayed). A live claim
# answering 'blocked_by_earlier' leaves the row 'queued' with NO comeback path of its own: the worker drops its
# in-memory generation, GitHub never redelivers a 202'd delivery, the App-level redelivery scan (#826) classifies
# it locally-received/OK (never a candidate), and pending() offers only causal lane HEADS — so its replay is
# entirely hostage to the lane head resolving. A blocker reclaimed by restart churn burns its durable budget and
# lands 'failed' (protocol 1), which STILL blocks the lane; the only unfreeze was the DLQ re-arm (default 1h; 0 =
# never) → the deferred delivery could reproduce the reported ~787s silence even after health replaced the stuck
# process. Size the default to the same hard delivery envelope used by worker-stuck detection: the tighter of the
# event wall and durable window, terminal reserve, plus 25s native/Render margin. The single fixed auto-rearm epoch
# below already prevents a poison hot-loop, so a stale high override is clamped to this bound. An explicit zero
# remains the documented fail-closed kill switch. Once a due queued row crosses this age, the recovery loop re-arms
# its failed head order-preservingly on the next normal recovery tick.
_DEFAULT_DEFERRED_ESCALATE_SECONDS = max(
    60,
    min(
        int(_event_budget._EVENT_WALL_TIMEOUT_SECONDS),
        int(_DELIVERY_RETRY_WINDOW_SECONDS),
    )
    + int(_event_budget._EVENT_TERMINAL_RESERVE_SECONDS)
    + 25,
)
_CONFIGURED_DEFERRED_ESCALATE_SECONDS = env_int(
    "VERIPSA_DELIVERY_DEFERRED_ESCALATE_SECONDS",
    _DEFAULT_DEFERRED_ESCALATE_SECONDS,
    min_value=0,
)
_DEFERRED_ESCALATE_SECONDS = (
    0
    if _CONFIGURED_DEFERRED_ESCALATE_SECONDS == 0
    else min(
        _CONFIGURED_DEFERRED_ESCALATE_SECONDS,
        _DEFAULT_DEFERRED_ESCALATE_SECONDS,
    )
)
# SHUTDOWN LEASE-EXPIRY grace — how soon after a drain-timeout shutdown the abandoned in-flight row becomes
# stale-reclaimable. A rolling deploy's SIGTERM that lands mid-event otherwise freezes that row's whole
# account/repository causal lane for the FULL _STALE_SECONDS window (30 min default): the row stays 'processing',
# every later same-lane claim defers behind it, and only the stale reclaim unblocks it. The dying process cannot
# safely requeue the row (its processor may still commit — that race is the audited double-processing defect), so
# on drain timeout it instead BACKDATES the lease (expire_inflight_lease) to become stale after this short grace.
# Long enough that the final in-flight event's own commit/finish comfortably wins; short enough that a deploy costs
# the lane ~a minute, not half an hour.
_SHUTDOWN_GRACE_SECONDS = env_int("VERIPSA_DELIVERY_SHUTDOWN_GRACE_SECONDS", 60, min_value=1)
# A worker instance is judged DEAD after this many seconds without a heartbeat. Must be >> the liveness-loop
# interval (5s) so a single DB blip can't false-kill a live instance: 15s = 3 missed beats.  "Dead" is only a
# suspicion; SQL additionally requires the exact delivery lease to be at least
# _DEAD_INSTANCE_RECLAIM_SAFE_SECONDS old before it backdates anything.
_DEAD_INSTANCE_SECONDS = env_int("VERIPSA_DELIVERY_DEAD_INSTANCE_SECONDS", 15, min_value=1)
# A nonce owner is present only between the atomic claim commit and the exact-lease boot-owner stamp. If both the
# claim response and its terminal recovery disappear, the DB may reclaim it after the event ceiling plus this
# clock/transport margin. No handler starts until the claim response has returned and the boot-owner stamp succeeds.
_CLAIM_AMBIGUITY_GRACE_SECONDS = 5

# SESSION TIMEOUTS for the DeliveryStore's OWN connections (iteration-5 audit — the one unbounded DB path).
# DeliveryStore opens a SEPARATE psycopg2 connection per call (submit/pending/claim/finish/release/depth/rearm),
# NOT the per-event processor's connection, so make_db_processor's session SETs (lock_timeout/statement_timeout)
# and server_dbops._arm_lock_session do NOT cover it — and there is no role-level default for veripsa_app. Every
# other DB session in the app bounds its statements; this was the lone exception. Harmless in steady state (each
# call is a single-row, indexed op on one small table) but this whole session's incident was UNBOUNDED waits, so
# we close the last gap by arming the SAME two knobs on EVERY DeliveryStore session (see _one): a wedged/contended
# store query now dies LOUD (QueryCanceled → logged → next recovery tick retries) instead of pinning the recovery
# loop or a 202 handler forever. Read ONCE at import via the validated env_int reader (a typo'd/out-of-range knob
# refuses to start, naming itself — same fail-loud contract as make_db_processor); min_value=1 disallows 0 (which
# Postgres reads as "unlimited" = exactly the unbounded hang this guards against). statement_timeout default 30s
# (single-row store ops are sub-ms; 30s is pure headroom — a runaway means something is badly wrong); lock_timeout
# default 5s (the store takes NO advisory lock, but a row-level lock contended by a concurrent claim/finish on the
# same delivery should fail fast, not stack up). Both env-tunable upward for a noisier fleet.
_STORE_STMT_TIMEOUT_MS = env_int("VERIPSA_DELIVERY_DB_STATEMENT_TIMEOUT_MS", 30_000, min_value=1)
_STORE_LOCK_TIMEOUT_MS = env_int("VERIPSA_DELIVERY_DB_LOCK_TIMEOUT_MS", 5_000, min_value=1)
_STORE_CONNECT_TIMEOUT_SECONDS = env_int(
    "VERIPSA_DELIVERY_DB_CONNECT_TIMEOUT_SECONDS", 10, min_value=1, max_value=60,
)
# A store operation can commit and then lose its response.  The synchronous
# terminal boundary probes the same exact-generation resolver at most twice;
# if both probes lose transport, this fixed-small registry transfers the
# terminal intent to the dedicated liveness daemon.  The worker pool is hard
# capped at four, so 32 entries leave ample room for every simultaneously
# owned lease without turning a DB outage into an unbounded memory queue.
_PENDING_TERMINAL_CAP = env_int(
    "VERIPSA_PENDING_TERMINAL_CAP", 32, min_value=4, max_value=128,
)
_PENDING_TERMINAL_BACKOFF_MAX_SECONDS = 60.0


def _store_timeout_ms(configured_ms: int) -> int:
    """Postgres timeout capped by the current delivery, or the configured default off-worker."""
    _event_budget.raise_if_expired()
    remaining = _event_budget.remaining()
    if remaining is None:
        return max(1, int(configured_ms))
    return max(1, min(int(configured_ms), int(remaining * 1000)))


def _store_connect_timeout_seconds() -> int:
    """Whole-second libpq timeout that never rounds beyond the delivery deadline."""
    seconds = _event_budget.timeout_for(_STORE_CONNECT_TIMEOUT_SECONDS)
    if seconds < 1.0:
        raise _event_budget.EventBudgetExceeded(
            "less than one second remains for the durable-store DB connection")
    return max(1, int(seconds))


def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def _lst(v) -> list:
    return v if isinstance(v, list) else []


def _s(v, n: int = 512):
    return v[:n] if isinstance(v, str) else v


def _path_list(v) -> list:
    """Preserve valid paths exactly and retain a fixed invalidity sentinel.

    Dropping malformed members or truncating overlong paths would let durable
    replay reinterpret a partial/aliased push as a complete changed set.
    ``None`` carries no attacker text but makes ingest's completeness validator
    force a full rebuild.
    """
    if not isinstance(v, list):
        return [None]
    out = []
    for path in v:
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 1024
            or "\x00" in path
        ):
            out.append(None)
        else:
            out.append(path)
    return out


def _user(u) -> dict:
    u = _obj(u)
    out = {}
    for k in ("id", "login", "type"):
        if u.get(k) not in (None, ""):
            out[k] = _s(u.get(k), 200) if isinstance(u.get(k), str) else u.get(k)
    return out


def _app(a) -> dict:
    a = _obj(a)
    out = {}
    for k in ("slug", "name"):
        if a.get(k) not in (None, ""):
            out[k] = _s(a.get(k), 120)
    return out


def _repo(r) -> dict:
    r = _obj(r)
    out = {}
    if r.get("id") not in (None, ""):
        out["id"] = r.get("id")
    for k in ("full_name", "name", "default_branch"):
        if r.get(k):
            out[k] = _s(r.get(k), 512)
    owner = _user(r.get("owner"))
    if owner:
        out["owner"] = owner
    return out


def _installation(i) -> dict:
    i = _obj(i)
    out = {}
    if i.get("id") not in (None, ""):
        out["id"] = i.get("id")
    acct = _user(i.get("account"))
    if acct:
        out["account"] = acct
    if i.get("suspended_at") not in (None, ""):
        out["suspended_at"] = _s(i.get("suspended_at"), 80)
    return out


def _pr_ref(ref) -> dict:
    ref = _obj(ref)
    out = {}
    for k in ("ref", "sha"):
        if ref.get(k):
            out[k] = _s(ref.get(k), 200)
    repo = _obj(ref.get("repo"))
    if repo.get("id") not in (None, ""):
        out["repo"] = {"id": repo.get("id")}
    return out


def _pr_entry(pr) -> dict:
    pr = _obj(pr)
    out = {}
    if pr.get("number") is not None:
        out["number"] = pr.get("number")
    base = _pr_ref(pr.get("base"))
    head = _pr_ref(pr.get("head"))
    if base:
        out["base"] = base
    if head:
        out["head"] = head
    return out


def _common(event_type: str, payload) -> dict:
    p = _obj(payload)
    out = {}
    if p.get("action") not in (None, ""):
        out["action"] = _s(p.get("action"), 80)
    if p.get("installation_id") not in (None, ""):
        out["installation_id"] = p.get("installation_id")
    inst = _installation(p.get("installation"))
    if inst:
        out["installation"] = inst
    org = _user(p.get("organization"))
    if org:
        out["organization"] = org
    repo = _repo(p.get("repository"))
    if repo:
        out["repository"] = repo
    sender = _user(p.get("sender"))
    if sender:
        out["sender"] = sender
    return out


def sanitize_payload(event_type: str, payload) -> dict:
    """Minimize a GitHub webhook payload to the fields the current handlers read.

    This keeps paths, ids, shas, branch names, short action labels, and booleans. It intentionally drops arbitrary
    text fields such as commit messages and PR bodies. For revert detection it stores only whether the title starts
    with "Revert ", not the title itself.
    """
    p = _obj(payload)
    out = _common(event_type, p)

    if event_type == "pull_request":
        pr = _obj(p.get("pull_request"))
        out["number"] = p.get("number")
        pr_out = {
            "base": _pr_ref(pr.get("base")),
            "head": _pr_ref(pr.get("head")),
            "user": _user(pr.get("user")),
            "merged": bool(pr.get("merged")),
            "draft": bool(pr.get("draft")),
        }
        if pr.get("merge_commit_sha"):
            pr_out["merge_commit_sha"] = _s(pr.get("merge_commit_sha"), 80)
        if pr.get("changed_files") is not None:
            pr_out["changed_files"] = pr.get("changed_files")
        title = pr.get("title")
        if isinstance(title, str) and title.strip().lower().startswith("revert "):
            pr_out["title"] = "Revert"
        labels = []
        for label in _lst(pr.get("labels")):
            name = _obj(label).get("name")
            if isinstance(name, str):
                labels.append({"name": name[:120]})
        if labels:
            pr_out["labels"] = labels
        out["pull_request"] = pr_out
        label_name = _obj(p.get("label")).get("name")
        if isinstance(label_name, str):
            out["label"] = {"name": label_name[:120]}
        changes = _obj(p.get("changes"))
        if "base" in changes:
            out["changes"] = {"base": {}}
        return out

    if event_type == "push":
        # ``before`` is the authority for commits[]' changed-path base and
        # ``size`` proves GitHub did not truncate the delivered commit list.
        # Both are content-free and must survive durable replay; omitting either
        # turns every recovered push into an unprovable incremental patch.
        for k in ("ref", "before", "after"):
            if k in p:
                out[k] = _s(p.get(k), 200)
        if "size" in p:
            raw_size = p.get("size")
            out["size"] = (
                raw_size
                if (
                    isinstance(raw_size, int)
                    and not isinstance(raw_size, bool)
                    and 0 <= raw_size <= 10_000_000
                )
                else -1
            )
        for k in ("deleted", "forced"):
            if k in p:
                out[k] = bool(p.get(k))
        head_commit = _obj(p.get("head_commit"))
        if head_commit.get("timestamp"):
            out["head_commit"] = {"timestamp": _s(head_commit.get("timestamp"), 80)}
        pusher = _obj(p.get("pusher"))
        if pusher.get("name"):
            out["pusher"] = {"name": _s(pusher.get("name"), 200)}
        commits = []
        for commit in _lst(p.get("commits")):
            c = _obj(commit)
            commits.append({
                "id": _s(c.get("id"), 80) if c.get("id") else None,
                "added": _path_list(c.get("added")),
                "modified": _path_list(c.get("modified")),
                "removed": _path_list(c.get("removed")),
            })
        if commits:
            out["commits"] = commits
        return out

    if event_type in ("installation", "installation_repositories"):
        for src, dst in (("repositories", "repositories"),
                         ("repositories_added", "repositories_added"),
                         ("repositories_removed", "repositories_removed")):
            repos = []
            for r in _lst(p.get(src)):
                rr = _repo(r)
                if rr.get("full_name"):
                    repo_out = {"full_name": rr["full_name"]}
                    if rr.get("id") not in (None, ""):
                        repo_out["id"] = rr["id"]
                    repos.append(repo_out)
            if repos:
                out[dst] = repos
        if p.get("repository_selection"):
            out["repository_selection"] = _s(p.get("repository_selection"), 40)
        return out

    if event_type == "repository":
        changes = _obj(p.get("changes"))
        ch = {}
        old_name = _obj(_obj(changes.get("repository")).get("name")).get("from")
        if old_name:
            ch.setdefault("repository", {})["name"] = {"from": _s(old_name, 512)}
        owner_from = _obj(_obj(changes.get("owner")).get("from"))
        old_user = _user(owner_from.get("user"))
        old_org = _user(owner_from.get("organization"))
        if old_user or old_org:
            ch.setdefault("owner", {})["from"] = {}
            if old_user:
                ch["owner"]["from"]["user"] = old_user
            if old_org:
                ch["owner"]["from"]["organization"] = old_org
        if ch:
            out["changes"] = ch
        return out

    if event_type in ("check_suite", "check_run"):
        node = _obj(p.get(event_type))
        node_out = {}
        for k in ("conclusion", "head_sha"):
            if node.get(k):
                node_out[k] = _s(node.get(k), 120)
        app = _app(node.get("app"))
        if app:
            node_out["app"] = app
        prs = [_pr_entry(pr) for pr in _lst(node.get("pull_requests"))]
        prs = [pr for pr in prs if pr]
        if prs:
            node_out["pull_requests"] = prs
        suite = _obj(node.get("check_suite"))
        suite_out = {}
        suite_app = _app(suite.get("app"))
        if suite_app:
            suite_out["app"] = suite_app
        suite_prs = [_pr_entry(pr) for pr in _lst(suite.get("pull_requests"))]
        suite_prs = [pr for pr in suite_prs if pr]
        if suite_prs:
            suite_out["pull_requests"] = suite_prs
        if suite_out:
            node_out["check_suite"] = suite_out
        out[event_type] = node_out
        return out

    if event_type == "merge_group":
        mg = _obj(p.get("merge_group"))
        out["merge_group"] = {k: _s(mg.get(k), 300) for k in ("head_sha", "head_ref", "base_ref") if mg.get(k)}
        return out

    if event_type == "marketplace_purchase":
        purchase = _obj(p.get("marketplace_purchase"))
        plan = _obj(purchase.get("plan"))
        out["marketplace_purchase"] = {"account": _user(purchase.get("account")), "plan": {}}
        for k in ("id", "name"):
            if plan.get(k) not in (None, ""):
                out["marketplace_purchase"]["plan"][k] = _s(plan.get(k), 80) if isinstance(plan.get(k), str) else plan.get(k)
        effective_date = p.get("effective_date")
        if (isinstance(effective_date, str) and len(effective_date.strip()) <= 80
                and re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
                    effective_date.strip(),
                )):
            out["effective_date"] = effective_date.strip()
        return out

    return out


def delivery_key(event_type: str, delivery: str | None, payload: dict, raw_payload: dict | None = None) -> str:
    """The idempotency key for a durable webhook row.

    PRIMARY: GitHub's X-GitHub-Delivery id — globally unique per delivery, so a 503-and-redeliver (or the recovery
    loop re-submitting with the already-assigned key) collapses onto ONE row via ON CONFLICT. Real GitHub ALWAYS
    sends this header, so the prod path is keyed by it.

    HEADERLESS FALLBACK (the local/smee/dev path, or a header-stripped request): there is no delivery id, so the key
    is a content hash. THE FOOTGUN this closes (small-findings sweep): the hash used to be over the SANITIZED payload
    — but sanitize_payload DELIBERATELY DROPS the distinguishing fields (commit messages, PR bodies, titles, the
    delivery id itself), so two SEMANTICALLY-DIFFERENT deliveries could sanitize to byte-identical content and hash to
    the SAME 'local-…' key → ON CONFLICT then MERGED two distinct events onto one row (a lost delivery). The ROOT fix
    is to hash the RAW (pre-sanitization) payload, which still carries those distinguishing fields: two raw-distinct
    deliveries get DISTINCT keys (→ two rows, never merged), while a TRUE redelivery (byte-identical raw payload) hashes
    to the SAME key (→ one row, still idempotent). We fall back to the sanitized payload only when no raw is supplied
    (so an older 3-arg caller keeps working); the key stays content-derived + deterministic (NOT a random/seq value, so
    idempotency holds across a retry). Content-free OUTPUT: only a hash crosses to the DB, never the raw text itself."""
    if delivery:
        return str(delivery)[:200]
    # hash the RAW payload when available (it retains the fields sanitization strips); else the sanitized one.
    body = json.dumps(raw_payload if raw_payload is not None else payload,
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return ("local-" + hashlib.sha256((event_type + "\0" + body).encode()).hexdigest())[:200]


_DELIVERY_PRECLAIMED = "_veripsa_delivery_preclaimed"
_DELIVERY_LEASE_GENERATION = "_veripsa_delivery_lease_generation"
_DELIVERY_RETRY_DEADLINE_MONOTONIC = "_veripsa_delivery_retry_deadline_monotonic"
_DELIVERY_EXECUTION_AUTHORITY = "_veripsa_delivery_execution_authority"
_WORKER_CLAIM_OUTCOME = "_veripsa_worker_claim_outcome"
_DELIVERY_ATOMIC_FINALIZE_PROTOCOL = object()


class _DeliveryExecutionAuthority:
    """One-call capability authorizing the live body to finalize one exact lease.

    It is created only after DeliveryStore owns a claim, travels only in the
    in-memory processing copy, and is popped before routing or handler code.
    Returning this exact object proves that body writes and durable completion
    committed together; a boolean/string marker could be forged by a handler.
    """

    __slots__ = ("key", "lease_generation")

    def __init__(self, key: str, lease_generation: int):
        self.key = str(key)
        self.lease_generation = int(lease_generation)


class _DeliveryCommitAmbiguity(BaseException):
    """Private unwind for an error after exact finish was staged.

    BaseException keeps the signal out of ordinary processor fail/release
    handlers. DeliveryStore owns the only recovery decision and always
    re-raises ``original`` unless the durable resolver proves ``committed``.
    """

    __slots__ = ("authority", "original")

    def __init__(self, authority: _DeliveryExecutionAuthority, original: BaseException):
        super().__init__("durable delivery commit outcome is ambiguous")
        self.authority = authority
        self.original = original


class _DeliveryFanoutDeferralCommitAmbiguity(BaseException):
    """Private unwind for a partial fanout defer whose COMMIT ACK vanished."""

    __slots__ = ("authority", "original", "not_before", "reason")

    def __init__(
            self, authority: _DeliveryExecutionAuthority,
            original: BaseException, not_before: datetime, reason: str,
    ):
        super().__init__("durable fanout deferral commit outcome is ambiguous")
        self.authority = authority
        self.original = original
        self.not_before = not_before
        self.reason = str(reason)[:300]


class _DeliveryAtomicDeferralResult:
    """Proof that the processor atomically checkpointed work and requeued itself.

    Only the live processor receives the exact execution-authority object, so
    the wrapper accepts this result solely when it carries that same identity.
    It then reports a durable deferral to EventQueue without issuing another
    finish/defer mutation on a separate connection.
    """

    __slots__ = ("authority", "reason")

    def __init__(self, authority: _DeliveryExecutionAuthority, reason: str):
        self.authority = authority
        self.reason = str(reason)[:160]


def _raise_original_commit_error(signal: _DeliveryCommitAmbiguity) -> None:
    """Re-raise the exact commit error object carried by a private unwind."""
    original = signal.original
    raise original.with_traceback(original.__traceback__) from None


def _lease_generation(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        generation = int(value)
    except (TypeError, ValueError):
        return None
    return generation if generation > 0 else None


def with_delivery_key(payload: dict, key: str, *, preclaimed: bool = False,
                      lease_generation: int | None = None,
                      retry_deadline_monotonic: float | None = None) -> dict:
    out = dict(payload or {})
    out["_veripsa_delivery_key"] = key
    if preclaimed:
        out[_DELIVERY_PRECLAIMED] = True
    lease = _lease_generation(lease_generation)
    if lease is not None:
        out[_DELIVERY_LEASE_GENERATION] = lease
    if retry_deadline_monotonic is not None:
        deadline = float(retry_deadline_monotonic)
        if not math.isfinite(deadline):
            raise ValueError("durable retry deadline must be finite")
        out[_DELIVERY_RETRY_DEADLINE_MONOTONIC] = deadline
    return out


def _as_jsonb(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, (dict, list)) else {}


def _drop_internal_markers(payload) -> dict:
    out = dict(_as_jsonb(payload) or {})
    out.pop("_veripsa_delivery_key", None)
    out.pop(_DELIVERY_PRECLAIMED, None)
    out.pop(_DELIVERY_LEASE_GENERATION, None)
    out.pop(_DELIVERY_RETRY_DEADLINE_MONOTONIC, None)
    out.pop(_DELIVERY_EXECUTION_AUTHORITY, None)
    return out


class DeliveryStore:
    def __init__(self, dsn: str, *, max_pending: int = _MAX_PENDING, max_attempts: int = _MAX_ATTEMPTS,
                 stale_seconds: int = _STALE_SECONDS,
                 retry_window_seconds: int = _DELIVERY_RETRY_WINDOW_SECONDS):
        self.dsn = dsn
        self.max_pending = int(max_pending)
        self.max_attempts = int(max_attempts)
        self.stale_seconds = int(stale_seconds)
        self.retry_window_seconds = int(retry_window_seconds)
        if not 1 <= self.retry_window_seconds <= 3600:
            raise ValueError("retry_window_seconds must be between 1 and 3600")
        # Every event currently owned by the fixed-small keyed worker pool, indexed by worker thread id.
        # Written by wrap_processor around each live processor run; read by the shutdown path so a drain
        # timeout can expire exactly those leases and nothing else. The registry is bounded by worker_count
        # (production default 3, hard max 4), not by queue depth.
        # RLock preserves the legacy test/diagnostic pattern that reads ``_inflight`` while already holding the
        # registry lock; production paths use it as an ordinary mutex.
        self._inflight_lock = threading.RLock()
        self._inflight_registry: dict[int, tuple[str, int]] = {}
        # Per-BOOT worker identity (a fresh uuid every process start — NEVER hostname, so a restarted box does
        # not inherit its own dead predecessor's rows as "live"). Each /5 claim appends a claim-unique nonce to
        # this heartbeat identity. That complete owner reference is atomically stored with the lease generation,
        # so an ACK-blackholed claim can be recovered without ever touching a later generation from this same boot.
        # "wk-" + 32 hex = 35 chars; claim refs add ".dddd." + 22 hex = 63 (within the schema bound).
        self._instance_id = "wk-" + uuid.uuid4().hex
        # Dedicated owner-liveness diagnostics.  These are intentionally
        # content-free and process-local: /healthz needs to distinguish "the
        # event workers are alive" from "their lease heartbeat daemon died".
        # The synchronous boot beat populates the timestamp before workers
        # start, and every successful later beat refreshes it.
        self._liveness_state_lock = threading.Lock()
        self._liveness_thread: threading.Thread | None = None
        self._last_successful_heartbeat_at: float | None = None
        # Exact terminal intents whose two synchronous resolver probes both
        # lost transport. Keys include the delivery generation, resolver kind,
        # and every normalized argument, so retries are idempotent and an ABA
        # successor can only return ownership_lost. Values contain only local
        # scheduling metadata; health output exposes counts/age, never keys,
        # errors, repository coordinates, or timestamps supplied by tenants.
        self._pending_terminal_lock = threading.Lock()
        self._pending_terminals: dict[tuple, dict] = {}
        self._pending_terminal_failures = 0
        self._pending_terminal_overflow = False

    def _one(self, sql: str, args=()):
        # Startup `options` arm the GUCs before the first SQL round-trip. Without this, the SET statements that
        # were supposed to establish the bound were themselves an unbounded blocking seam.
        statement_ms = _store_timeout_ms(_STORE_STMT_TIMEOUT_MS)
        lock_ms = _store_timeout_ms(_STORE_LOCK_TIMEOUT_MS)
        event_deadline = _event_budget.current_deadline()
        connect_deadline = _db_connect.deadline_after(
            _STORE_CONNECT_TIMEOUT_SECONDS, event_deadline)
        try:
            conn = _db_connect.connect(
                psycopg2.connect,
                self.dsn,
                deadline=connect_deadline,
                connect_timeout=_STORE_CONNECT_TIMEOUT_SECONDS,
                options=(
                    f"-c statement_timeout={statement_ms} "
                    f"-c lock_timeout={lock_ms} "
                    "-c search_path=core"
                ),
            )
        except _db_connect.DatabaseConnectDeadlineExceeded as error:
            if event_deadline is not None and time.monotonic() >= event_deadline:
                raise _event_budget.EventBudgetExceeded(
                    "webhook durable-store DB connection exceeded its event deadline") from error
            raise
        deadline_guard = _event_budget.arm_connection_deadline(conn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                _event_budget.raise_if_expired()
                cur.execute("SET search_path=core")
                # Re-tighten after connect/startup consumed time. Every claim/stamp/defer/release/finish/cleanup
                # method routes through this boundary, while recovery/HTTP submit calls with no event context keep
                # the configured defaults.
                cur.execute("SET statement_timeout = %s" % _store_timeout_ms(_STORE_STMT_TIMEOUT_MS))
                cur.execute("SET lock_timeout = %s" % _store_timeout_ms(_STORE_LOCK_TIMEOUT_MS))
                _event_budget.raise_if_expired()
                # The two session-setup round trips above consumed part of the same delivery allowance. Tighten
                # once more immediately before the business statement so it cannot inherit the larger
                # post-connect value and run beyond the event deadline.
                cur.execute("SET statement_timeout = %s" % _store_timeout_ms(_STORE_STMT_TIMEOUT_MS))
                _event_budget.raise_if_expired()
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            # Disarm before close so a delayed timer can never hit an FD that
            # the OS has already reassigned to an unrelated connection.
            deadline_guard.disarm()
            conn.close()

    def submit(self, event_type: str, payload, delivery: str | None, *, account_key=None, repo=None) -> dict:
        sanitized = sanitize_payload(event_type, payload)
        # Derive the key from the RAW payload on the headerless fallback path (sanitization drops the distinguishing
        # fields, so hashing the sanitized payload could merge two distinct deliveries — see delivery_key). A true
        # GitHub redelivery carries X-GitHub-Delivery and keys by that; the recovery loop re-submits with the assigned
        # key as `delivery`, so both stay on the delivery-id branch and remain idempotent regardless of this.
        key = delivery_key(event_type, delivery, sanitized, raw_payload=_obj(payload))
        res = _as_jsonb(self._one(
            "SELECT core.enqueue_webhook_delivery_with_authority(%s,%s,%s,%s,%s,%s,%s)",
            (key, event_type, account_key, repo, Json(sanitized), self.max_pending, 2)))
        if not res.get("accepted"):
            return {"accepted": False, "queued": False, "reason": res.get("reason", "busy")}
        return {"accepted": True, "queued": bool(res.get("queued")), "key": key,
                "event_type": event_type, "payload": with_delivery_key(sanitized, key), "delivery": key}

    def pending(self, limit: int = _RECOVER_LIMIT) -> list[dict]:
        rows = _as_jsonb(self._one(
            "SELECT core.pending_webhook_deliveries_with_authority(%s,%s,%s)",
            (limit, self.stale_seconds, self.max_attempts)))
        return rows if isinstance(rows, list) else []

    def claim(self, key: str) -> dict:
        # The nonce is generated BEFORE the SQL call. The DB writes this complete reference in the same UPDATE
        # that advances lease_generation, making a lost post-COMMIT response recoverable without knowing the
        # returned generation. A fresh nonce on every call also fences a later generation owned by this same boot.
        remaining_total = _event_budget.total_remaining()
        ambiguity_window = math.ceil(
            float(_event_budget._EVENT_WALL_TIMEOUT_SECONDS)
            if remaining_total is None else max(0.0, remaining_total)
        ) + _CLAIM_AMBIGUITY_GRACE_SECONDS
        ambiguity_window = min(9999, max(1, ambiguity_window))
        owner_instance = (
            f"{self._instance_id}.{ambiguity_window:04d}.{uuid.uuid4().hex[:22]}"
        )
        # Anchor before the DB round-trip. The server reports remaining time at
        # claim execution; adding it to this earlier monotonic point
        # conservatively subtracts the whole transport duration.
        claim_started = time.monotonic()
        try:
            claimed = _as_jsonb(self._one(
                "SELECT core.claim_webhook_delivery_with_authority(%s,%s,%s,%s,%s,%s)",
                (key, self.stale_seconds, self.max_attempts,
                 DURABLE_RETRY_PROTOCOL, owner_instance,
                 self.retry_window_seconds)))
        except _event_budget.EventBudgetExceeded as claim_error:
            self._recover_ambiguous_claim(key, owner_instance, claim_error)
            raise
        except Exception as claim_error:
            self._recover_ambiguous_claim(key, owner_instance, claim_error)
            raise
        if not claimed.get("claimed"):
            return claimed
        lease_generation = _lease_generation(claimed.get("lease_generation"))
        if lease_generation is None:
            malformed = RuntimeError("durable claim missing lease_generation")
            self._recover_ambiguous_claim(key, owner_instance, malformed)
            raise malformed
        remaining_ms = claimed.get("retry_window_remaining_ms")
        if isinstance(remaining_ms, bool):
            remaining_ms = None
        try:
            remaining_ms = int(remaining_ms)
        except (TypeError, ValueError):
            remaining_ms = -1
        if remaining_ms < 0:
            malformed = RuntimeError("durable claim missing retry window")
            self._release_known_claim(key, lease_generation, malformed)
            raise malformed
        retry_deadline = claim_started + (remaining_ms / 1000.0)
        claimed[_DELIVERY_RETRY_DEADLINE_MONOTONIC] = retry_deadline
        _event_budget.narrow_total_deadline(retry_deadline)
        # Once the response is known, replace the temporary nonce with the stable boot heartbeat id. A handler is
        # authorized to start only after this exact-lease stamp succeeds; therefore every remaining nonce row is
        # provably pre-handler and can be reclaimed at its encoded ambiguity deadline.
        try:
            stamped = self.stamp_owner(key, lease_generation)
        except _event_budget.EventBudgetExceeded as stamp_error:
            self._release_known_claim(key, lease_generation, stamp_error)
            raise
        if not stamped:
            stamp_error = RuntimeError("durable delivery owner stamp failed")
            self._release_known_claim(key, lease_generation, stamp_error)
            raise stamp_error
        return claimed

    def _recover_ambiguous_claim(self, key: str, owner_instance: str, error: BaseException) -> None:
        """Best-effort terminal recovery for a claim whose commit result is unknowable.

        The SQL predicate uses the complete claim-unique owner reference, not merely this process's boot id.
        Therefore a rollback/no-commit is a harmless miss, a committed claim is returned queued/failed, and a
        later lease generation (including one from this same process) is never modified.
        """
        cleanup_error = None
        for _probe in range(2):
            try:
                with _event_budget.terminal_scope():
                    outcome = str(self._one(
                        "SELECT core.recover_ambiguous_webhook_claim_with_authority(%s,%s,%s,%s)",
                        (key, owner_instance, str(error)[:300], self.max_attempts),
                    ) or "missing")
                if outcome != "missing":
                    print(f"durable delivery ambiguous claim recovered: delivery=present status={outcome}",
                          flush=True)
                return
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except BaseException as probe_error:
                cleanup_error = probe_error
        if isinstance(cleanup_error, _event_budget.EventBudgetExceeded):
            print("durable delivery ambiguous-claim recovery exceeded terminal budget: "
                  f"delivery=present error_code={_bounded_error_code(cleanup_error)} "
                  "— stale/dead-instance recovery remains authoritative", flush=True)
        else:
            print("durable delivery ambiguous-claim recovery FAILED: "
                  f"delivery=present error_code={_bounded_error_code(cleanup_error)} "
                  "— stale/dead-instance recovery remains authoritative", flush=True)

    def _release_known_claim(self, key: str, lease_generation: int, error: BaseException) -> None:
        """Return a known exact lease when the post-response boot-owner stamp cannot complete."""
        try:
            with _event_budget.terminal_scope():
                self.release(key, str(error), lease_generation)
        except _event_budget.EventBudgetExceeded as cleanup_error:
            print("durable delivery release exceeded terminal budget after owner-stamp failure: "
                  f"delivery=present error_code={_bounded_error_code(cleanup_error)} "
                  "— nonce expiry remains authoritative", flush=True)
        except Exception as cleanup_error:
            print("durable delivery release FAILED after owner-stamp failure: "
                  f"delivery=present error_code={_bounded_error_code(cleanup_error)} "
                  "— nonce expiry remains authoritative", flush=True)

    @staticmethod
    def _terminal_identity(
            kind: str, key: str, lease_generation: int, args: tuple) -> tuple:
        return (
            str(kind),
            str(key)[:200],
            int(lease_generation),
            tuple(args),
        )

    def _record_terminal_probe_failure(self) -> None:
        with self._pending_terminal_lock:
            self._pending_terminal_failures += 1

    def _register_pending_terminal(
            self, kind: str, key: str, lease_generation: int, args: tuple,
    ) -> bool:
        """Transfer one exact terminal intent to the liveness daemon.

        No processor or EventQueue retry is created here. The registry is
        intentionally fixed-small: a cap violation means an owned terminal
        intent could not be retained, so health remains fail-closed until this
        process is replaced and dead-instance recovery fences its leases.
        """
        identity = self._terminal_identity(kind, key, lease_generation, args)
        now = time.monotonic()
        with self._pending_terminal_lock:
            if identity in self._pending_terminals:
                return True
            if len(self._pending_terminals) >= _PENDING_TERMINAL_CAP:
                self._pending_terminal_overflow = True
                self._pending_terminal_failures += 1
                return False
            self._pending_terminals[identity] = {
                "kind": str(kind),
                "key": str(key)[:200],
                "generation": int(lease_generation),
                "args": tuple(args),
                "created_at": now,
                "next_attempt_at": now,
                "attempts": 0,
            }
        return True

    def _resolve_terminal_once(
            self, kind: str, key: str, lease_generation: int, args: tuple,
    ) -> str:
        generation = int(lease_generation)
        if kind == "release":
            error, max_attempts = args
            sql = (
                "SELECT core.resolve_webhook_delivery_release_with_authority("
                "%s,%s,%s,%s)"
            )
            values = (key, error, int(max_attempts), generation)
        elif kind == "defer":
            not_before, reason = args
            sql = (
                "SELECT core.resolve_webhook_delivery_defer_with_authority("
                "%s,%s,%s,%s)"
            )
            values = (key, generation, not_before, reason)
        elif kind == "commit":
            error, max_attempts = args
            sql = (
                "SELECT core.resolve_webhook_delivery_commit_with_authority("
                "%s,%s,%s,%s)"
            )
            values = (key, error, int(max_attempts), generation)
        elif kind == "fanout_defer":
            not_before, reason = args
            sql = (
                "SELECT core.resolve_webhook_delivery_fanout_defer_with_authority("
                "%s,%s,%s,%s)"
            )
            values = (key, generation, not_before, reason)
        else:
            raise ValueError("unknown durable terminal resolver kind")
        return str(self._one(sql, values) or "missing")

    @staticmethod
    def _terminal_resolution_complete(kind: str, outcome: str) -> bool:
        if outcome == "ownership_lost":
            return True
        return outcome in {
            "release": {"queued", "failed"},
            "defer": {"deferred"},
            "commit": {"committed", "queued", "failed"},
            "fanout_defer": {"deferred"},
        }.get(kind, set())

    def _resolve_terminal_with_probe(
            self, kind: str, key: str, lease_generation: int, args: tuple,
    ) -> str:
        """Resolve inline at most twice, with no sleep, then hand off.

        A cancellation is a BaseException by design. It and ordinary transport
        failures take the same exact second probe, so a post-COMMIT ACK loss is
        normally proven before returning while a real outage never multiplies
        inline sleeps or handler executions.
        """
        for _probe in range(2):
            try:
                outcome = self._resolve_terminal_once(
                    kind, key, lease_generation, args)
                if self._terminal_resolution_complete(kind, outcome):
                    return outcome
                # A successful transport carrying ``missing`` (or any
                # unrecognised value) is still an inconclusive terminal
                # result.  Treat it exactly like an ACK/probe failure: retry
                # once inline, then retain the immutable intent for the
                # liveness daemon.  Returning here would leave the row
                # processing while falsely telling the caller cleanup had
                # completed.
                self._record_terminal_probe_failure()
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except BaseException:
                self._record_terminal_probe_failure()
        self._register_pending_terminal(
            kind, key, lease_generation, args)
        return "pending"

    def drain_pending_terminals(self, *, limit: int = 1) -> dict:
        """Retry due exact intents from the event-budget-free liveness daemon.

        Each intent receives one DB call per daemon tick. Failures use
        exponential 1..60 second backoff; success or ownership_lost removes the
        entry. Missing rows remain visible and retry because disappearance is a
        durability invariant violation, not proof that a terminal commit won.
        """
        now = time.monotonic()
        with self._pending_terminal_lock:
            due = [
                (identity, dict(entry))
                for identity, entry in self._pending_terminals.items()
                if float(entry["next_attempt_at"]) <= now
            ]
        due.sort(key=lambda item: float(item[1]["created_at"]))
        attempted = 0
        resolved = 0
        for identity, entry in due[:max(0, int(limit))]:
            attempted += 1
            try:
                outcome = self._resolve_terminal_once(
                    entry["kind"], entry["key"], entry["generation"],
                    tuple(entry["args"]))
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except BaseException:
                outcome = "probe_failed"
            if self._terminal_resolution_complete(entry["kind"], outcome):
                with self._pending_terminal_lock:
                    if self._pending_terminals.get(identity) is not None:
                        self._pending_terminals.pop(identity, None)
                        resolved += 1
                continue
            with self._pending_terminal_lock:
                current = self._pending_terminals.get(identity)
                if current is None:
                    continue
                attempts = int(current["attempts"]) + 1
                current["attempts"] = attempts
                current["next_attempt_at"] = time.monotonic() + min(
                    _PENDING_TERMINAL_BACKOFF_MAX_SECONDS,
                    float(2 ** min(attempts - 1, 6)),
                )
                self._pending_terminal_failures += 1
        with self._pending_terminal_lock:
            depth = len(self._pending_terminals)
        return {"attempted": attempted, "resolved": resolved, "pending": depth}

    def finish(self, key: str, lease_generation: int) -> bool:
        return bool(self._one(
            "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
            (key, int(lease_generation)),
        ))

    def resolve_commit(self, key: str, error: BaseException | str, lease_generation: int) -> str:
        """Resolve only a body transaction that staged exact durable finish.

        ``committed`` is the sole success result. ``queued``/``failed`` mean
        the transaction did not land and the SQL boundary already performed
        the ordinary exact-generation release. Every other result is
        deliberately inconclusive and therefore preserves the original error.
        """
        msg = str(error)[:300]
        return self._resolve_terminal_with_probe(
            "commit", key, lease_generation, (msg, self.max_attempts))

    def resolve_fanout_defer(
            self, key: str, lease_generation: int,
            not_before: datetime, reason: str,
    ) -> str:
        """Resolve or perform one exact attempt-neutral partial fanout yield."""
        if (not isinstance(not_before, datetime) or not_before.tzinfo is None
                or not_before.utcoffset() is None):
            raise ValueError("durable fanout defer needs a timezone-aware not_before")
        schedule = not_before.astimezone(timezone.utc)
        return self._resolve_terminal_with_probe(
            "fanout_defer", key, lease_generation,
            (schedule, str(reason)[:300]))

    def release(self, key: str, error: BaseException | str, lease_generation: int) -> str:
        msg = str(error)[:300]
        return self._resolve_terminal_with_probe(
            "release", key, lease_generation, (msg, self.max_attempts))

    def defer(self, key: str, not_before: datetime, reason: str, lease_generation: int) -> bool:
        """Return an owned delivery to the scheduled queue without consuming its claimed attempt."""
        if (not isinstance(not_before, datetime) or not_before.tzinfo is None
                or not_before.utcoffset() is None):
            raise ValueError("durable delivery defer needs a timezone-aware not_before")
        outcome = self._resolve_terminal_with_probe(
            "defer", key, lease_generation,
            (
                not_before.astimezone(timezone.utc),
                str(reason or "consistency deferral")[:300],
            ),
        )
        return outcome in ("deferred", "pending")

    def _set_inflight(self, key: str, lease_generation: int) -> None:
        with self._inflight_lock:
            self._inflight_registry[threading.get_ident()] = (key, int(lease_generation))

    def _clear_inflight(self, key: str) -> None:
        with self._inflight_lock:
            worker_id = threading.get_ident()
            current = self._inflight_registry.get(worker_id)
            if current is not None and current[0] == key:
                self._inflight_registry.pop(worker_id, None)

    @property
    def _inflight(self):
        """Legacy diagnostic view: the sole lease as a tuple, all leases as a tuple, or None.

        Older gates inspect this private attribute in the one-worker case. Keep that shape while the real
        shutdown path uses the complete registry.
        """
        with self._inflight_lock:
            values = tuple(self._inflight_registry.values())
        if not values:
            return None
        return values[0] if len(values) == 1 else values

    def expire_inflight_lease(self, grace_seconds: int = _SHUTDOWN_GRACE_SECONDS) -> bool:
        """SHUTDOWN-ONLY: make every current in-flight durable row stale-reclaimable after grace_seconds.

        Called when the graceful drain times out with the worker still mid-event. The row deliberately STAYS
        'processing' with its lease intact — the dying processor may still commit and finish, and requeueing here
        would race that commit into a double-processed delivery. Backdating locked_at instead means: worker wins →
        normal finish; process dies → the next recovery tick reclaims within the grace instead of the full stale
        window (which otherwise freezes the row's whole account/repo causal lane for that long)."""
        with self._inflight_lock:
            inflight = tuple(self._inflight_registry.values())
        if not inflight:
            return False
        expired = False
        for key, lease_generation in inflight:
            expired = bool(self._one(
                "SELECT core.expire_webhook_delivery_lease_with_authority(%s,%s,%s,%s)",
                (key, lease_generation, self.stale_seconds, int(grace_seconds)))) or expired
        return expired

    def stamp_owner(self, key: str, lease_generation: int) -> bool:
        """Rolling-worker compatibility for old /4 claims which could not stamp atomically.

        New /5 callers use this exact-lease transaction immediately after receiving the claim response to replace
        its temporary nonce with the stable boot heartbeat id. Older /4 in-memory workers use the same API.
        """
        try:
            return bool(self._one(
                "SELECT core.stamp_webhook_owner_instance_with_authority(%s,%s,%s)",
                (key, int(lease_generation), self._instance_id)))
        except _event_budget.EventBudgetExceeded:
            raise
        except Exception as e:
            print(f"delivery owner-stamp skipped: error_code={_bounded_error_code(e)}", flush=True)
            return False

    def beat_instance(self) -> None:
        """Heartbeat this worker instance so the reaper can tell a slow-but-alive worker from a dead one."""
        self._one("SELECT core.beat_webhook_worker_instance_with_authority(%s)", (self._instance_id,))
        with self._liveness_state_lock:
            self._last_successful_heartbeat_at = time.monotonic()

    def _bind_liveness_thread(self, thread: threading.Thread) -> None:
        """Record the one dedicated heartbeat/reaper daemon for health output."""
        with self._liveness_state_lock:
            self._liveness_thread = thread

    def liveness_snapshot(self) -> dict:
        """Content-free owner-heartbeat diagnostics for /healthz and tests."""
        now = time.monotonic()
        with self._liveness_state_lock:
            thread = self._liveness_thread
            last_beat = self._last_successful_heartbeat_at
        with self._pending_terminal_lock:
            pending_depth = len(self._pending_terminals)
            pending_oldest = (
                min(
                    float(entry["created_at"])
                    for entry in self._pending_terminals.values()
                )
                if self._pending_terminals else None
            )
            pending_failures = int(self._pending_terminal_failures)
            pending_overflow = bool(self._pending_terminal_overflow)
        age = None if last_beat is None else max(0.0, now - last_beat)
        pending_age = (
            None if pending_oldest is None
            else max(0.0, now - pending_oldest)
        )
        thread_alive = thread.is_alive() if thread is not None else False
        heartbeat_current = (
            isinstance(age, (int, float))
            and age <= float(_DEAD_INSTANCE_RECLAIM_SAFE_SECONDS)
        )
        pending_terminal_current = (
            pending_age is None
            or pending_age < float(_DEAD_INSTANCE_RECLAIM_SAFE_SECONDS)
        )
        return {
            "healthy": bool(
                thread_alive and heartbeat_current and not pending_overflow
                and pending_terminal_current),
            "thread_started": thread is not None,
            "thread_alive": thread_alive,
            "last_successful_heartbeat_seconds": (
                round(age, 1) if isinstance(age, (int, float)) else None
            ),
            "heartbeat_dead_seconds": int(_DEAD_INSTANCE_SECONDS),
            "reclaim_safe_seconds": int(_DEAD_INSTANCE_RECLAIM_SAFE_SECONDS),
            "event_total_seconds": int(_event_budget._EVENT_WALL_TIMEOUT_SECONDS),
            "terminal_reserve_seconds": int(
                _event_budget._EVENT_TERMINAL_RESERVE_SECONDS),
            "clock_margin_seconds": int(
                _OWNER_RECLAIM_CLOCK_MARGIN_SECONDS),
            "pending_terminal_depth": pending_depth,
            "pending_terminal_oldest_seconds": (
                round(pending_age, 1)
                if isinstance(pending_age, (int, float)) else None
            ),
            "pending_terminal_failures": pending_failures,
            "pending_terminal_overflow": pending_overflow,
            "pending_terminal_cap": int(_PENDING_TERMINAL_CAP),
        }

    def reap_dead_instances(self, dead_seconds: int = _DEAD_INSTANCE_SECONDS) -> int:
        """Reclaim only dead-owner leases older than the valid-event ceiling.

        A missed heartbeat is evidence that the process *may* be dead, not
        authority to overlap a still-valid handler.  Postgres therefore also
        requires the delivery lease itself to be at least the derived safe
        ceiling old before it backdates the lock for ordinary stale reclaim.
        """
        return int(self._one(
            "SELECT core.reap_dead_instance_leases_with_authority(%s,%s,%s)",
            (int(dead_seconds), self.stale_seconds,
             _DEAD_INSTANCE_RECLAIM_SAFE_SECONDS)) or 0)

    def depth(self) -> dict:
        out = _as_jsonb(self._one("SELECT core.webhook_delivery_depth_with_authority()"))
        return out if isinstance(out, dict) else {}

    def rearm_failed(self, rearm_seconds: int = _DLQ_REARM_SECONDS, limit: int = _RECOVER_LIMIT) -> dict:
        """Give an eligible failed row its sole automatic fresh epoch."""
        out = _as_jsonb(self._one(
            "SELECT core.rearm_failed_webhook_deliveries_with_authority(%s,%s,%s)",
            (int(rearm_seconds), int(limit), _MAX_AUTO_REARMS)))
        return out if isinstance(out, dict) else {}

    def recover_terminal(self, delivery_guids: list[str], recovery_id: str) -> dict:
        """Requeue exact exhausted failed rows using only their stored ingress payload.

        Shape/authentication validation lives at the HTTP boundary and is
        repeated by PostgreSQL as the final authority. The database owns the
        one-shot fence, row locking, audit stamp, and content-free result.
        """
        out = _as_jsonb(self._one(
            "SELECT core.recover_terminal_webhook_deliveries_with_authority(%s,%s)",
            (list(delivery_guids), recovery_id)))
        return out if isinstance(out, dict) else {}

    def continue_terminal(
            self, delivery_guids: list[str], continuation_id: str,
            exact_sha: str) -> dict:
        """Continue failed members of one exact recovered batch once.

        PostgreSQL preserves the original batch audit, skips already-complete
        members, stamps the exact reviewed runtime SHA on every member, and
        owns the all-or-zero one-shot fence.
        """
        out = _as_jsonb(self._one(
            "SELECT core.continue_terminal_webhook_deliveries_with_authority(%s,%s,%s)",
            (list(delivery_guids), continuation_id, exact_sha)))
        return out if isinstance(out, dict) else {}

    def terminal_recovery_status(self, delivery_guids: list[str]) -> dict:
        """Read durable, content-free status for one exact operator recovery.

        Unlike the bounded in-process delivery receipts, this proof survives a
        web-process restart. PostgreSQL validates the exact GUID set and proves
        one internally stored recovery epoch without trusting a caller run id.
        """
        out = _as_jsonb(self._one(
            "SELECT core.terminal_webhook_delivery_recovery_status_with_authority(%s)",
            (list(delivery_guids),)))
        return out if isinstance(out, dict) else {}

    def operator_github_redelivery_audit(
            self, recovery_id: str, redelivery_sha: str,
            expected_continuation_id: str | None = None,
            expected_continuation_sha: str | None = None) -> dict:
        """Read one exact external-redelivery aggregate without row identity."""
        out = _as_jsonb(self._one(
            "SELECT core.operator_github_redelivery_audit_with_authority(%s,%s,%s,%s)",
            (
                recovery_id, redelivery_sha,
                expected_continuation_id, expected_continuation_sha,
            )))
        return out if isinstance(out, dict) else {}

    def escalate_blocked(self, escalate_seconds: int = _DEFERRED_ESCALATE_SECONDS,
                         limit: int = _RECOVER_LIMIT) -> dict:
        """AGED-DEFERRED LANE ESCALATION (issue #847): when a due 'queued' row has waited longer than
        escalate_seconds, re-arm the aged protocol-1 'failed' rows blocking its account/repository lane NOW
        instead of waiting for the slow DLQ re-arm cadence, so the lane drains in causal order and the deferred
        delivery is replayed. Never touches 'done'/'processing' rows and never re-arms quarantined protocol-0
        failures. Returns {aged_queued, escalated}. Best-effort: a DB blip returns {} and the next tick retries."""
        out = _as_jsonb(self._one(
            "SELECT core.escalate_blocked_webhook_deliveries_with_authority(%s,%s,%s)",
            (int(escalate_seconds), int(limit), _MAX_AUTO_REARMS)))
        return out if isinstance(out, dict) else {}

    def wrap_processor(self, processor):
        def process(event_type, payload, db, gh, coalesce=None):
            key = _obj(payload).get("_veripsa_delivery_key")
            if not key:
                if coalesce is not None:
                    return processor(event_type, payload, db, gh, coalesce=coalesce)
                return processor(event_type, payload, db, gh)
            preclaimed = bool(_obj(payload).get(_DELIVERY_PRECLAIMED))
            retry_deadline = _obj(payload).get(
                _DELIVERY_RETRY_DEADLINE_MONOTONIC)
            if preclaimed:
                lease_generation = _lease_generation(_obj(payload).get(_DELIVERY_LEASE_GENERATION))
                if lease_generation is None:
                    return {"delivery": key, _WORKER_CLAIM_OUTCOME: "unclaimable",
                            "claim_reason": "missing_lease_generation", "claim_status": "processing"}
                claimed_event_type = event_type
                stored_payload = _drop_internal_markers(payload)
            else:
                claimed = self.claim(key)
                if not claimed.get("claimed"):
                    # A false claim has materially different meanings. An older same-account durable row means this
                    # memory generation must yield to recovery; processing/done means another generation already owns
                    # the immutable delivery id; a missing row is durability loss and must be loud. Thread a private
                    # marker to EventQueue instead of returning an ordinary success that increments `processed`.
                    reason = str(claimed.get("reason") or "unclassified")[:80]
                    if reason in ("blocked_by_earlier", "not_due"):
                        outcome = "deferred"
                    elif reason in ("already_owned", "already_finished"):
                        outcome = "duplicate"
                    elif reason == "missing":
                        outcome = "missing"
                    else:
                        outcome = "unclaimable"
                    return {"delivery": key, _WORKER_CLAIM_OUTCOME: outcome,
                            "claim_reason": reason, "claim_status": claimed.get("status")}
                lease_generation = _lease_generation(claimed.get("lease_generation"))
                if lease_generation is None:
                    return {"delivery": key, _WORKER_CLAIM_OUTCOME: "unclaimable",
                            "claim_reason": "missing_lease_generation", "claim_status": "processing"}
                claimed_event_type = claimed.get("event_type") or event_type
                stored_payload = _as_jsonb(claimed.get("payload"))
                retry_deadline = claimed.get(
                    _DELIVERY_RETRY_DEADLINE_MONOTONIC)
            try:
                retry_deadline = float(retry_deadline)
                if not math.isfinite(retry_deadline):
                    retry_deadline = None
            except (TypeError, ValueError):
                # Rolling/test preclaims created before the /6 contract retain
                # the enclosing EventQueue budget. Every new claim supplies the
                # marker and therefore receives the cross-generation cap.
                retry_deadline = None
            # Thread the content-free delivery id into every processor. Only the real DB processor advertises the
            # private atomic-finalize protocol; for it, add a one-call object capability carrying this exact lease.
            # make_db_processor pops and validates the capability before any router/handler can observe the payload.
            # It is neither JSON-serializable nor ever passed to submit(), and _drop_internal_markers removes it as
            # a final defense, so execution authority can never become durable webhook data.
            processing_payload = with_delivery_key(stored_payload, key)
            execution_authority = None
            if (getattr(processor, "_veripsa_atomic_delivery_finalize_protocol", None)
                    is _DELIVERY_ATOMIC_FINALIZE_PROTOCOL):
                execution_authority = _DeliveryExecutionAuthority(key, lease_generation)
                processing_payload[_DELIVERY_EXECUTION_AUTHORITY] = execution_authority
            # Register the owned lease for the shutdown path (expire_inflight_lease) for exactly the span the
            # processor runs + finalises; the finally below unconditionally deregisters on every exit path.
            self._set_inflight(key, lease_generation)
            try:
                try:
                    if retry_deadline is not None:
                        _event_budget.narrow_total_deadline(retry_deadline)
                        _event_budget.raise_if_expired()
                    if coalesce is not None:
                        result = processor(claimed_event_type, processing_payload, db, gh, coalesce=coalesce)
                    else:
                        result = processor(claimed_event_type, processing_payload, db, gh)
                except _DeliveryFanoutDeferralCommitAmbiguity as ambiguous:
                    # The partial repository business/checkpoint/defer
                    # transaction either committed and lost its ACK, or did
                    # not commit. The exact resolver covers both outcomes:
                    # matching queued proves commit; matching processing
                    # performs the same attempt-neutral defer now.
                    if ambiguous.authority is not execution_authority:
                        original = ambiguous.original
                        raise original.with_traceback(
                            original.__traceback__) from None
                    if isinstance(payload, dict):
                        payload.pop(_DELIVERY_PRECLAIMED, None)
                        payload.pop(_DELIVERY_LEASE_GENERATION, None)
                        payload.pop(_DELIVERY_RETRY_DEADLINE_MONOTONIC, None)
                    try:
                        with _event_budget.terminal_scope():
                            resolution = self.resolve_fanout_defer(
                                key,
                                lease_generation,
                                ambiguous.not_before,
                                ambiguous.reason,
                            )
                    except _event_budget.EventBudgetExceeded:
                        original = ambiguous.original
                        raise original.with_traceback(
                            original.__traceback__) from None
                    except Exception:
                        original = ambiguous.original
                        raise original.with_traceback(
                            original.__traceback__) from None
                    if resolution == "deferred":
                        return {
                            "delivery": key,
                            _WORKER_CLAIM_OUTCOME: "deferred",
                            "claim_reason": ambiguous.reason,
                            "claim_status": "queued",
                        }
                    original = ambiguous.original
                    raise original.with_traceback(
                        original.__traceback__) from None
                except _DeliveryCommitAmbiguity as ambiguous:
                    # Exact finish was staged in the same body transaction, but the COMMIT response disappeared
                    # (or cancellation fired at that boundary). Only the per-call capability created above may ask
                    # this wrapper to resolve it. The resolver locks the exact durable generation: done proves the
                    # body+finish landed and is a normal success; processing is atomically released queued/failed.
                    # Any resolver error/inconclusive state preserves the ORIGINAL exception object.
                    if ambiguous.authority is not execution_authority:
                        _raise_original_commit_error(ambiguous)
                    if isinstance(payload, dict):
                        payload.pop(_DELIVERY_PRECLAIMED, None)
                        payload.pop(_DELIVERY_LEASE_GENERATION, None)
                        payload.pop(_DELIVERY_RETRY_DEADLINE_MONOTONIC, None)
                    try:
                        with _event_budget.terminal_scope():
                            resolution = self.resolve_commit(
                                key, ambiguous.original, lease_generation)
                    except _event_budget.EventBudgetExceeded:
                        _raise_original_commit_error(ambiguous)
                    except Exception:
                        _raise_original_commit_error(ambiguous)
                    if resolution == "committed":
                        return None
                    # queued/failed already performed the exact ordinary release. ownership_lost/missing are
                    # deliberately fail-closed. In every non-committed case EventQueue sees the original failure.
                    _raise_original_commit_error(ambiguous)
                except IntentionalDeliveryDeferral as defer:
                    # This is an expected external-consistency wait, not failed work.  Clear a recovery lease marker
                    # before the separate defer call for the same commit/ACK ambiguity handled by release below: if the
                    # DB commits and the response is lost, an in-process retry must perform a fresh durable claim and see
                    # ``not_due`` rather than reusing the old lease.  The SQL boundary restores the claimed attempt.
                    if isinstance(payload, dict):
                        payload.pop(_DELIVERY_PRECLAIMED, None)
                        payload.pop(_DELIVERY_LEASE_GENERATION, None)
                        payload.pop(_DELIVERY_RETRY_DEADLINE_MONOTONIC, None)
                    with _event_budget.terminal_scope():
                        if not self.defer(key, defer.not_before, defer.reason, lease_generation):
                            raise RuntimeError("durable delivery defer lost lease authority")
                    return {
                        "delivery": key,
                        _WORKER_CLAIM_OUTCOME: "deferred",
                        "claim_reason": "intentional_consistency_deferral",
                        "claim_status": "queued",
                    }
                except _event_budget.EventBudgetExceeded as event_cancel:
                    # Cancellation intentionally lives outside Exception so no
                    # fail-open collaborator can turn an expired transaction
                    # into success.  This is the explicit durable boundary:
                    # clear any reusable recovery authority, return this exact
                    # lease inside the reserved terminal tail, then preserve
                    # the ORIGINAL cancellation even when cleanup itself fails.
                    if isinstance(payload, dict):
                        payload.pop(_DELIVERY_PRECLAIMED, None)
                        payload.pop(_DELIVERY_LEASE_GENERATION, None)
                        payload.pop(_DELIVERY_RETRY_DEADLINE_MONOTONIC, None)
                    try:
                        with _event_budget.terminal_scope():
                            self.release(key, event_cancel, lease_generation)
                    except _event_budget.EventBudgetExceeded as cleanup_error:
                        try:
                            print("durable delivery cancellation release exceeded terminal budget: "
                                  f"delivery=present error_code={_bounded_error_code(cleanup_error)} "
                                  "— stale/dead-instance recovery remains authoritative",
                                  flush=True)
                        except Exception:
                            pass
                    except Exception as cleanup_error:
                        try:
                            print("durable delivery cancellation release FAILED: "
                                  f"delivery=present error_code={_bounded_error_code(cleanup_error)} "
                                  "— stale/dead-instance recovery remains authoritative",
                                  flush=True)
                        except Exception:
                            pass
                    raise
                except Exception as e:
                    # The PROCESSOR failed → its transaction rolled back, the work did NOT land. This is the ONLY path
                    # that may re-queue: release for a bounded retry.
                    # A recovery-submitted row is already claimed on its first in-memory run. Once a processor failure
                    # starts release, its lease is no longer safe to trust: the separate release connection may commit
                    # and lose its ACK, or raise before commit. Clear both markers BEFORE that call so either outcome
                    # forces every in-process retry through a fresh durable claim.
                    if isinstance(payload, dict):
                        payload.pop(_DELIVERY_PRECLAIMED, None)
                        payload.pop(_DELIVERY_LEASE_GENERATION, None)
                        payload.pop(_DELIVERY_RETRY_DEADLINE_MONOTONIC, None)
                    with _event_budget.terminal_scope():
                        self.release(key, e, lease_generation)
                    raise
                if type(result) is _DeliveryAtomicDeferralResult:
                    if result.authority is not execution_authority:
                        raise RuntimeError(
                            "durable delivery atomic deferral authority is malformed")
                    return {
                        "delivery": key,
                        _WORKER_CLAIM_OUTCOME: "deferred",
                        "claim_reason": result.reason or "fanout_slice",
                        "claim_status": "queued",
                    }
                # Returning the exact one-call capability is proof that handler writes and durable completion
                # committed in the SAME transaction. Do not issue the legacy separate-connection finish: that
                # would recreate the very post-commit ACK ambiguity this protocol removes.
                if execution_authority is not None and result is execution_authority:
                    return None
                # The processor RETURNED — its content-free writes are COMMITTED on the worker's connection. Finalising
                # the delivery is now BEST-EFFORT and must NEVER re-queue (audit P0): the old code put finish() inside
                # the SAME try, so a finish() blip on the store's SEPARATE connection (a transient DB error after the
                # work already committed) fell into `except → release → recovery re-submits → the SAME delivery is
                # processed TWICE` (metric inflation on any non-idempotent write, e.g. co-change counters). Finish on
                # its own connection; a NO-OP (FOUND=false: the row was concurrently reclaimed/finalised — see the
                # owned-row guard in finish_webhook_delivery_with_authority) or an error is LOGGED, not re-queued — the
                # recovery loop's stale-'processing' reclaim is the durability backstop, not an immediate re-run here.
                try:
                    with _event_budget.terminal_scope():
                        if not self.finish(key, lease_generation):
                            print("durable delivery finish: delivery=present not finalizable "
                                  f"(concurrently reclaimed/finalised or lifecycle work incomplete) "
                                  f"— durable recovery remains authoritative", flush=True)
                except _event_budget.EventBudgetExceeded as e:
                    # The handler transaction is already committed. Expiring while finalising must never turn
                    # that committed result into an EventQueue failure/retry; leave the row processing and let
                    # stale/dead-instance recovery perform the idempotent finalisation later.
                    print("durable delivery finish SKIPPED post-commit: delivery=present "
                          f"error_code={_bounded_error_code(e)} "
                          f"— work is committed; recovery will finalise (no immediate re-queue)", flush=True)
                except Exception as e:
                    print("durable delivery finish FAILED post-commit: delivery=present "
                          f"error_code={_bounded_error_code(e)} "
                          f"— work is committed; recovery will finalise (no immediate re-queue)", flush=True)
                return result
            finally:
                self._clear_inflight(key)
        # Private capability contract consumed by EventQueue: a normal return from this wrapper means the live
        # processor's DB transaction has already committed. EventQueue must still classify durable claim outcomes,
        # but it must not reinterpret a slightly-late committed return as failure and mint a contradictory retry.
        process._veripsa_commits_before_return = True
        process._veripsa_one_durable_attempt_per_dequeue = True
        return process


def _worker_can_accept(worker, event_type: str, payload: dict, key: str, *, recovered: bool = False) -> bool:
    # The real EventQueue exposes a stricter recovery admission seam: at most one queued/in-flight recovered
    # delivery per account.  That keeps crash recovery moving across tenants without preloading a long same-owner
    # FIFO ahead of fresh webhooks.  Legacy/test workers fall back to their ordinary capacity contract.
    can_accept = getattr(worker, "can_accept_recovery", None) if recovered else None
    if not callable(can_accept):
        can_accept = getattr(worker, "can_accept", None)
    if not callable(can_accept):
        return True
    try:
        return bool(can_accept(event_type, payload, key))
    except TypeError:
        return bool(can_accept(event_type, payload))


def _worker_reserve_recovery(worker, event_type: str, payload: dict, key: str) -> bool:
    reserve = getattr(worker, "reserve_recovery", None)
    if callable(reserve):
        return bool(reserve(event_type, payload, key))
    return _worker_can_accept(worker, event_type, payload, key, recovered=True)


def _worker_claims_recovery_on_dequeue(worker) -> bool:
    """Whether local delivery reservations make an unclaimed queue item unique.

    EventQueue reserves a delivery/lane until that exact in-memory item reaches
    terminal cleanup, and a DeliveryStore-wrapped processor advertises that it
    will perform the exact claim at dequeue. Both capabilities are required: a
    bare EventQueue has reservations too, but must never run an unclaimed
    durable body. Legacy/test adapters keep claim-before-submit so a queued DB
    row cannot be submitted again on every recovery tick.
    """
    return (
        callable(getattr(worker, "reserve_recovery", None))
        and bool(getattr(worker, "_one_durable_attempt_per_dequeue", False))
    )


def _worker_cancel_recovery(worker, payload: dict, key: str) -> None:
    cancel = getattr(worker, "cancel_recovery_reservation", None)
    if callable(cancel):
        cancel(payload, key)


def _worker_queue_depth(worker) -> int | None:
    qsize = getattr(worker, "qsize", None)
    if not callable(qsize):
        return None
    try:
        n = qsize()
    except Exception:
        return None
    return int(n) if isinstance(n, (int, float)) and n >= 0 else None


def _recover_pending_deliveries(store: DeliveryStore, worker, *, limit: int = _RECOVER_LIMIT,
                                max_queue_depth: int = _RECOVERY_MAX_QUEUE_DEPTH) -> dict:
    """Reserve-before-submit recovery; production claims only at worker dequeue.

    Starting a durable retry window while an item is merely waiting in the
    memory queue lets slow heads consume the whole window before its handler
    runs. With the configured depth 20 and three workers that can recreate a
    hundreds-second stable-owner lane block. Production EventQueue therefore
    reserves the delivery/lane locally, submits an UNCLAIMED recovered item,
    and performs the exact DB claim in ``wrap_processor`` after dequeue. The DB
    row stays queued (attempts/window/owner unchanged) through all queue wait.

    The reservation prevents the next local recovery tick from submitting the
    same key again. Another instance may hold its own preview, but the exact DB
    claim still grants one owner and every loser exits as a duplicate. Legacy
    workers without atomic reservation retain claim-before-submit solely for
    backward compatibility.

    Recovery is deliberately paced behind live traffic. Without a queue-depth cap, a deploy that makes dozens of
    stale durable rows reclaimable can immediately refill the in-memory queue with old work, delaying fresh PR
    webhooks even though the service is healthy. Limit each sweep to the worker headroom below
    max_queue_depth; when the live queue is already busy, leave durable rows in the DB for the next tick.
    """
    submitted = 0
    skipped_full = 0
    skipped_claim = 0
    skipped_backlog = 0
    max_queue_depth = max(1, int(max_queue_depth))
    start_depth = _worker_queue_depth(worker)
    claim_on_dequeue = _worker_claims_recovery_on_dequeue(worker)
    recover_limit = max(1, int(limit))
    submission_limit = recover_limit
    if start_depth is not None:
        headroom = max_queue_depth - start_depth
        if headroom <= 0:
            return {"submitted": 0, "skipped_full": 0, "skipped_claim": 0, "skipped_backlog": 1}
        submission_limit = min(submission_limit, headroom)
    # SQL interleaves rows by per-account rank. Scan at least one headroom-sized window even when an operator sets
    # recover_limit=1: the first row may belong to an account that already has its one recovery slot, while a later
    # tenant is free. Only `submission_limit` rows can be claimed; the bounded scan never exceeds the SQL cap.
    scan_limit = min(1000, max(recover_limit, max_queue_depth + 1))
    for row in store.pending(scan_limit):
        cur_depth = _worker_queue_depth(worker)
        if submitted >= submission_limit or (cur_depth is not None and cur_depth >= max_queue_depth):
            skipped_backlog += 1
            break
        key = row.get("key")
        event_type = row.get("event_type")
        row_payload = _as_jsonb(row.get("payload"))
        if not key or not event_type:
            skipped_claim += 1
            continue
        # This is only a routing/capacity preview. In the production path it
        # deliberately carries no preclaim, lease, or retry-deadline marker.
        capacity_payload = with_delivery_key(row_payload, key)
        if not _worker_reserve_recovery(worker, event_type, capacity_payload, key):
            skipped_full += 1
            continue
        lease_generation = None
        if claim_on_dequeue:
            claimed_event_type = event_type
            submit_payload = capacity_payload
        else:
            try:
                claimed = store.claim(key)
            except Exception:
                # The DB statement failed/rolled back, so no in-memory job owns
                # this durable row. Never leak the local reservation.
                _worker_cancel_recovery(worker, capacity_payload, key)
                raise
            if not isinstance(claimed, dict) or not claimed.get("claimed"):
                _worker_cancel_recovery(worker, capacity_payload, key)
                skipped_claim += 1
                continue
            lease_generation = _lease_generation(claimed.get("lease_generation"))
            if lease_generation is None:
                _worker_cancel_recovery(worker, capacity_payload, key)
                raise RuntimeError("durable claim missing lease_generation")
            claimed_event_type = claimed.get("event_type") or event_type
            claimed_payload = _as_jsonb(claimed.get("payload")) or row_payload
            submit_payload = with_delivery_key(
                claimed_payload, key, preclaimed=True, lease_generation=lease_generation,
                retry_deadline_monotonic=claimed.get(
                    _DELIVERY_RETRY_DEADLINE_MONOTONIC),
            )
        # Recovered pushes may be stale relative to a newer live push already queued in this process. Keep
        # register_push=False so recovery cannot overwrite the live latest-push marker and make the newer push skip.
        try:
            accepted = worker.submit(
                claimed_event_type, submit_payload, key, register_push=False, recovered=True,
            )
        except Exception:
            _worker_cancel_recovery(worker, capacity_payload, key)
            if lease_generation is not None:
                try:
                    store.release(key, "recovery submit raised before memory admission", lease_generation)
                except Exception as release_error:
                    print("delivery recovery release FAILED after submit exception: delivery=present "
                          f"error_code={_bounded_error_code(release_error)}", flush=True)
            raise
        if not accepted:
            _worker_cancel_recovery(worker, capacity_payload, key)
            skipped_full += 1
            if lease_generation is not None:
                try:
                    store.release(key, "memory queue full before recovery submit", lease_generation)
                except Exception as e:
                    print("delivery recovery release skipped after submit-full: delivery=present "
                          f"error_code={_bounded_error_code(e)}", flush=True)
            print("delivery recovery: memory queue full, will retry next tick: "
                  f"delivery=present event={claimed_event_type}", flush=True)
            continue
        submitted += 1
    return {"submitted": submitted, "skipped_full": skipped_full, "skipped_claim": skipped_claim,
            "skipped_backlog": skipped_backlog}


def start_instance_liveness_loop(
        store: DeliveryStore, *, interval: int = _RECOVER_INTERVAL):
    """Register this boot before it can own work, then keep its lease authority live.

    This loop is deliberately independent from delivery recovery. Recovery is
    an operator kill-switch; owner liveness is a correctness precondition for
    every claimed delivery and therefore may never be disabled while workers
    still accept events.

    The first heartbeat is synchronous and fail-closed. Consequently a worker
    cannot stamp its stable boot id while a rolling peer's reaper still sees
    that id as absent. Subsequent heartbeat/reap calls stay on this dedicated
    daemon so a slow event cannot starve its own liveness signal.
    """
    store.beat_instance()

    def _loop():
        while True:
            time.sleep(interval)
            try:
                # Beat first. If this boot cannot renew authority, do not let
                # it run the fleet reaper on a potentially misleading view.
                store.beat_instance()
                reaped = store.reap_dead_instances()
                if reaped:
                    print(
                        "delivery lease reaper: reclaimed "
                        f"{reaped} lease(s) from a dead worker instance",
                        flush=True,
                    )
                # At most one off-event exact terminal resolver call per tick.
                # A broken DB may hold that call until its own bounded store
                # timeout, so draining the whole fixed registry here would
                # starve later heartbeats. Beat/reap always run first.
                terminal = store.drain_pending_terminals(limit=1)
                if terminal.get("resolved"):
                    print(
                        "delivery terminal recovery: resolved "
                        f"{terminal['resolved']} intent(s), "
                        f"{terminal['pending']} pending",
                        flush=True,
                    )
            except Exception as error:
                print(
                    f"delivery heartbeat/reap skipped: error_code={_bounded_error_code(error)}",
                    flush=True,
                )

    thread = threading.Thread(
        target=_loop, name="veripsa-delivery-liveness", daemon=True)
    thread.start()
    store._bind_liveness_thread(thread)
    return thread


def start_recovery_loop(store: DeliveryStore, worker, *, interval: int = _RECOVER_INTERVAL, limit: int = _RECOVER_LIMIT,
                        dlq_rearm_seconds: int = _DLQ_REARM_SECONDS,
                        deferred_escalate_seconds: int = _DEFERRED_ESCALATE_SECONDS):
    # DLQ RE-ARM cadence layered on the fast recovery tick (audit P1): re-arm 'failed' rows on a SLOW schedule
    # (every dlq_rearm_seconds worth of ticks), not every tick — a poison row gets at most one fresh try per
    # re-arm interval, with the watchdog 'failed>0' alert as the human signal in between. 0 disables the re-arm
    # (alert-only DLQ): the rows stay 'failed' + visible (depth/alert) but are never auto-requeued. The re-arm's
    # OWN age gate (in the SQL) is the real guard; this just bounds how often we issue the cheap sweep call.
    _rearm_every = max(1, round(dlq_rearm_seconds / interval)) if (dlq_rearm_seconds and interval > 0) else 0
    # AGED-DEFERRED LANE ESCALATION cadence (issue #847): check on each already-bounded recovery tick. The queued
    # age gate plus the one-ever automatic epoch are the hot-loop guards; adding a second minute-scale polling
    # delay after process replacement only extends the same-repository outage. The partial failed-row index keeps
    # the no-op path cheap as the retained inbox grows.
    _escalate_every = 1 if (deferred_escalate_seconds and interval > 0) else 0

    def _loop():
        _tick = 0
        while True:
            try:
                # DEAD-LETTER RE-ARM (slow cadence): requeue 'failed' poison/outage rows past the re-arm age so they
                # are not permanently lost; pending() below then replays the freshly-'queued' rows. Best-effort +
                # bounded (its own age gate + LIMIT); a re-arm error is logged, never stalls live recovery.
                if _rearm_every and (_tick % _rearm_every == 0):
                    try:
                        res = store.rearm_failed(dlq_rearm_seconds, limit)
                        if res.get("rearmed"):
                            print(f"delivery DLQ: re-armed {res.get('rearmed')} 'failed' row(s) for one fresh "
                                  f"attempt-budget ({res.get('failed_remaining')} still failed)", flush=True)
                    except Exception as e:
                        print(f"delivery DLQ re-arm skipped: error_code={_bounded_error_code(e)}", flush=True)
                # AGED-DEFERRED LANE ESCALATION (issue #847): a 'blocked_by_earlier' deferral has no comeback of
                # its own — when its lane head ended 'failed', only the slow DLQ re-arm above would ever unfreeze
                # the lane. Re-arm the aged failed head(s) of lanes with aged queued work NOW; pending() below then
                # drains the lane in causal order. Best-effort + bounded; an error is logged, never stalls recovery.
                if _escalate_every and (_tick % _escalate_every == 0):
                    try:
                        res = store.escalate_blocked(deferred_escalate_seconds, limit)
                        if res.get("escalated"):
                            print(f"delivery lane escalation: re-armed {res.get('escalated')} failed lane-head "
                                  f"row(s) blocking {res.get('aged_queued')} aged queued deliver(ies)", flush=True)
                    except Exception as e:
                        print("delivery lane escalation skipped: "
                              f"error_code={_bounded_error_code(e)}", flush=True)
                _tick += 1
                _recover_pending_deliveries(store, worker, limit=limit)
            except Exception as e:
                print(f"delivery recovery skipped: error_code={_bounded_error_code(e)}", flush=True)
            time.sleep(interval)

    thread = threading.Thread(target=_loop, name="veripsa-delivery-recovery", daemon=True)
    thread.start()
    return thread
