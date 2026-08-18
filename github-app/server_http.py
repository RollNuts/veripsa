"""The HTTP REQUEST-HANDLING layer — extracted from the server god-file (the #2 coupling hotspot; serve() was a
245-line function inlining this whole BaseHTTPRequestHandler subclass + 4 nested defs alongside the config read
and the runtime wiring). This is the request-routing surface of the live webhook receiver:

  do_POST  the ACK-FAST webhook door: bounded-body read → HMAC verify → persist-before-202 (durable inbox) →
           enqueue → 202/503/401/400/413 (it NEVER does the work — that runs on the background worker). The
           durable-inbox kill switch makes ordinary events memory-only, but repository removals still persist
           before 202 because their stable-ID lifecycle boundary requires durable delivery authority.
  do_GET   the operational probes: /healthz (worker-driven liveness), /freshz (coarse stored-graph freshness), and
           /readyz (DB-reachable + the App service-identity self-check + the stuck-worker readiness signal);
           plus /statusz, a public-safe coarse status surface that deliberately does NOT expose repo/PR/canary
           identifiers.
  + _freshness_summary_db_only / _json / log_message (the per-handler helpers).

It changes for a DIFFERENT reason than the env/config + runtime wiring (server_boot.py) or the event-type router
(webhook_handlers.py / handle_event): it changes when the request-routing / probe / ack-fast contract changes.
make_handler(...) is a FACTORY — it takes the per-process dependencies serve() wired (the secret, the durable
store, the in-process worker, the db/dsn/gh) and returns the Handler CLASS — so the routing body no longer has to
live as a closure inside serve(). serve() subclasses it ONLY to stamp the per-request socket timeout
(`timeout = _req_timeout`) and then binds the ThreadingHTTPServer — both of which stay textually in serve() (the
no-hang gate pins those two substrings to server.py; the structural resilience guard).

DESIGN (mirrors event_processor.py / ingest.py / server_boot.py): this module imports NOTHING from server.py at
LOAD time (no circular import — server.py imports THIS). Every server-resident function the handler calls — the
request-ingress guards read_bounded_body / verify_signature, the payload guards _as_obj, the event→(repo,account)
routing _event_account_key / _event_repo, the health snapshot health_snapshot / watchdog_last_tick_seconds, the
freshness surface graph_freshness_all, and the readiness self-check app_identity_ok / _worker_stuck_seconds — is
resolved at CALL time off the `server` module via the lazy `_server()` idiom event_processor.py uses, NOT captured
at factory time. So a test (or an operator hot-patch) that does setattr(server, "verify_signature", …) /
"read_bounded_body" / "health_snapshot" / "graph_freshness_all" is STILL seen by the live handler — the same
monkeypatch contract those names had when they were module globals serve() closed over. (json is the stdlib;
imported directly.)

Behavior-preserving extraction: a pure move of the Handler body. No status code, header, route, ack-fast
ordering (verify → persist → enqueue → respond), fail-open semantics, or content-free guarantee changed — only
the home of the class. The captured deps (secret/store/worker/db/dsn/gh) are the SAME values serve()'s closure
saw; the server-resident FUNCTIONS are reached through `server` so their seams stay live.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler


def _schema_contract_result():
    """Read the boot-time schema↔runtime contract result at CALL time so /healthz reflects the current state
    (None when the boot path has not run the check — e.g. a unit test importing this module directly without
    going through server_boot.wire_runtime). schema_contract is imported LAZILY here (not at module load) for
    two reasons: (1) it lives next to server_boot in the import graph and we want server_http to stay free of any
    server-resident name; (2) so a test that monkeypatches schema_contract.get_boot_result is honored exactly as
    every other lazy-`_server()` seam in this file. Fail-open: any import error returns None (the probe behaves
    as it did before the contract existed — never break liveness on a missing module)."""
    try:
        try:
            from schema_contract import get_boot_result  # type: ignore
        except ImportError:
            from .schema_contract import get_boot_result  # type: ignore
        return get_boot_result()
    except Exception:
        return None


# /healthz ADVISORY CACHE — keep the platform LIVENESS PROBE independent of DB latency.
#
# A socket/statement timeout cannot bound every DNS, kernel, or pool boundary. If Render's /healthz request
# synchronously samples Postgres, the probe can hang precisely when it must decide whether this process is alive.
# Health therefore reads only the last successful advisory values from memory. On a miss/expiry it starts one
# fixed single-flight daemon per advisory source and returns immediately; a wedged refresh cannot occupy request
# threads or manufacture an unbounded sampler fleet. /readyz remains explicitly DB-touching.
#
# SAFETY:
#   * Read-only / advisory: the cached value is metadata on the liveness body, never gates a 200/503. Caching it a
#     few seconds can only DELAY surfacing a freshness change, never fabricate one (the watchdog already PUSH-alerts
#     on a behind coordinate at its own cadence).
#   * Per-process: a process restart re-warms from scratch (no inter-process stale state).
#   * TTL is conservative + tunable (default 15s). Zero requests an async refresh on every probe; it never restores
#     synchronous DB I/O.
#   * One lock protects sampler creation; readers take a snapshot of the module-level last-good slot.
_HEALTHZ_FRESHNESS_CACHE: dict = {"value": None, "at": 0.0, "key": None}
_HEALTHZ_INBOX_DEPTH_CACHE: dict = {"value": None, "at": 0.0, "key": None}
_HEALTHZ_FRESHNESS_REFRESH: dict = {"key": None, "thread": None}
_HEALTHZ_INBOX_DEPTH_REFRESH: dict = {"key": None, "thread": None}
_HEALTHZ_REFRESH_LOCK = threading.Lock()
_FRESHZ_CACHE: dict = {"value": None, "at": 0.0, "key": None}
_FRESHZ_REFRESH: dict = {"key": None, "thread": None, "started": 0.0}
_FRESHZ_REFRESH_LOCK = threading.Lock()
_READYZ_IDENTITY_CACHE: dict = {"value": None, "at": 0.0, "key": None}

_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), browsing-topics=()"),
    ("Cache-Control", "no-store"),
)

_DELIVERY_PROBE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_RECOVERY_PATH = "/recoveryz/durable-deliveries"
_RECOVERY_STATUS_PATH = "/recoveryz/durable-deliveries/status"
_RECOVERY_CONTINUATION_PATH = "/recoveryz/durable-deliveries/continue"
_OPERATOR_GITHUB_REDELIVERY_AUDIT_PATH = (
    "/recoveryz/operator-github-redelivery/audit"
)
_RECOVERY_SIGNATURE_HEADER = "X-Veripsa-Recovery-Signature"
_RECOVERY_MAX_BODY_BYTES = 4096
_RECOVERY_SHA = re.compile(r"^[0-9a-f]{40}$")
_RECOVERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_RECOVERY_GUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_OPERATOR_RECOVERY_ID = re.compile(r"^deploy-core:[1-9][0-9]{0,19}$")
_RECOVERY_RESULT_KEYS = frozenset((
    "status", "requested", "rearmed", "already_recovered", "ineligible", "missing", "results",
))
_RECOVERY_RESULT_ENUMS = frozenset(("rearmed", "already_recovered", "ineligible", "missing"))
_RECOVERY_STATUS_RESULT_KEYS = frozenset(("status", "requested"))
_RECOVERY_STATUS_RESULT_ENUMS = frozenset((
    "unspent", "never_recovered", "recovering", "verified", "failed",
    "continuable", "continuation_failed", "unverified",
))
_RECOVERY_CONTINUATION_RESULT_KEYS = frozenset((
    "status", "requested", "continued", "already_completed",
    "already_continued", "ineligible", "missing", "results",
))
_RECOVERY_CONTINUATION_RESULT_ENUMS = frozenset((
    "continued", "already_completed", "already_continued", "ineligible", "missing",
))
_OPERATOR_GITHUB_REDELIVERY_AUDIT_KEYS = frozenset((
    "status", "state", "total", "unspent", "spent", "accepted",
    "non_202", "ambiguous", "continuation_id", "continuation_sha",
))
_OPERATOR_GITHUB_REDELIVERY_AUDIT_STATES = frozenset((
    "ambiguous", "rejected", "pre-spend-unclassified", "accepted",
    "partial-accepted",
))


def _bounded_error_code(exc: BaseException) -> str:
    """Diagnosable ingress error metadata that cannot echo a row, payload, or capability."""
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

# INGRESS NO-OP FAST-ACK. Because the App holds checks:write, GitHub delivers a `check_run` for EVERY check by
# EVERY CI provider on EVERY install — the single largest webhook volume source. `_handle_check_event`
# UNCONDITIONALLY no-ops two of its actions: `created` (never a rerun, never check_suite.requested, and
# action != 'completed' → webhook_handlers.py:3354) and `completed` with a NON-failing conclusion
# (webhook_handlers.py:3356). Un-filtered, each such no-op still pays the full persist→enqueue→claim→
# tenant-admit→lock pipeline AND consumes the per-account fairness cap — so a CI check_run storm from one busy
# org 503s that org's OWN pull_request/push work (the noisy-neighbor starvation behind the deploy-canary wedge).
# Fast-acking them 2xx WITHOUT enqueue yields the IDENTICAL outcome (skipped) at none of that cost. NEVER dropped:
# the load-bearing check_run actions (`rerequested` / `requested_action` = the Re-run button, the ONLY signal
# GitHub sends for it; `completed` with a FAILING conclusion = the stuck-PR fact), and ALL of `check_suite`
# (its `requested` is the deploy Tier-4 canary AND the synchronize/base-push backstop). Kill switch:
# VERIPSA_INGRESS_NOOP_FASTACK=0 restores full processing. Content-free (event_type + action + conclusion only).
_INGRESS_NOOP_FASTACK = os.environ.get("VERIPSA_INGRESS_NOOP_FASTACK", "1") != "0"
_INGRESS_NOOP_LOCK = threading.Lock()
_INGRESS_NOOP_COUNT = 0


def _ingress_noop_fastack(event_type, action, check_run_conclusion, failing_conclusions, *, enabled: bool) -> bool:
    """True iff this event is one `_handle_check_event` UNCONDITIONALLY no-ops, so it is safe to 2xx-fast-ack at
    ingress WITHOUT enqueue. ONLY `check_run`: `created` (always skipped) and `completed` with a conclusion NOT in
    `failing_conclusions` (skipped). `rerequested` / `requested_action` and `completed`-with-failing are
    load-bearing; ALL `check_suite` (incl. the canary's `requested`) and every other event are preserved. Pure +
    total (a mistyped/absent action or conclusion falls through to False = process, fail-safe)."""
    if not enabled or event_type != "check_run":
        return False
    if action == "created":
        return True
    if action == "completed":
        return check_run_conclusion not in failing_conclusions
    return False


def _requires_durable_repository_offboard(event_type: str, payload) -> bool:
    """Return whether this lifecycle webhook cannot be acknowledged without durable delivery authority.

    Repository deletion/deselection/ownership transfer mutates lifecycle tombstones and purges or isolates working
    state using the durable delivery's stable ID and receive order. Installation activation, suspension, and deletion
    additionally bind the App-observed installation generation to that immutable inbox row.  The ordinary in-memory
    kill-switch path has neither, so these actions keep the narrow durable store path. This classifier reads only
    event/action metadata.
    """
    if not isinstance(payload, dict):
        return False
    action = payload.get("action")
    return ((event_type == "repository" and action in ("deleted", "transferred"))
            or (event_type == "installation"
                and action in ("created", "unsuspend", "new_permissions_accepted", "suspend", "deleted"))
            or (event_type == "installation_repositories" and action in ("added", "removed")))


def _delivery_probe_signature(secret: str, delivery: str) -> str:
    """Domain-separated HMAC for the private delivery-receipt probe."""
    body = ("deliveryz\0" + delivery).encode("ascii")
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _recovery_signature(secret: str, body: bytes) -> str:
    """Domain-separated operator signature; never interchangeable with a GitHub webhook HMAC."""
    return "sha256=" + hmac.new(
        secret.encode(), b"recoveryz\0" + body, hashlib.sha256,
    ).hexdigest()


def _recovery_status_signature(secret: str, body: bytes) -> str:
    """Separate read-only status authority from the recovery mutation capability."""
    return "sha256=" + hmac.new(
        secret.encode(), b"recovery-statusz\0" + body, hashlib.sha256,
    ).hexdigest()


def _recovery_continuation_signature(secret: str, body: bytes) -> str:
    """A continuation capability is not interchangeable with first recovery or status."""
    return "sha256=" + hmac.new(
        secret.encode(), b"recovery-continuationz\0" + body, hashlib.sha256,
    ).hexdigest()


def _operator_github_redelivery_audit_signature(
        secret: str, body: bytes) -> str:
    """Separate read-only ledger audit from every recovery capability."""
    return "sha256=" + hmac.new(
        secret.encode(),
        b"operator-github-redelivery-auditz\0" + body,
        hashlib.sha256,
    ).hexdigest()


def _recovery_authorized(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    try:
        return hmac.compare_digest(
            _recovery_signature(secret, body).encode("ascii"), signature.encode("ascii"),
        )
    except (AttributeError, TypeError, UnicodeEncodeError, ValueError):
        return False


def _recovery_status_authorized(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    try:
        return hmac.compare_digest(
            _recovery_status_signature(secret, body).encode("ascii"), signature.encode("ascii"),
        )
    except (AttributeError, TypeError, UnicodeEncodeError, ValueError):
        return False


def _recovery_continuation_authorized(
        secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    try:
        return hmac.compare_digest(
            _recovery_continuation_signature(secret, body).encode("ascii"),
            signature.encode("ascii"),
        )
    except (AttributeError, TypeError, UnicodeEncodeError, ValueError):
        return False


def _operator_github_redelivery_audit_authorized(
        secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    try:
        return hmac.compare_digest(
            _operator_github_redelivery_audit_signature(
                secret, body
            ).encode("ascii"),
            signature.encode("ascii"),
        )
    except (AttributeError, TypeError, UnicodeEncodeError, ValueError):
        return False


def _read_recovery_body(content_length, stream) -> tuple[bytes, int | None]:
    """Read one small exact body without invoking the much larger webhook-body allowance."""
    if not isinstance(content_length, str) or not content_length.isascii() or not content_length.isdigit():
        return b"", 400
    try:
        length = int(content_length, 10)
    except (TypeError, ValueError, OverflowError):
        return b"", 400
    if length < 1 or length > _RECOVERY_MAX_BODY_BYTES:
        return b"", 400
    body = stream.read(length)
    if not isinstance(body, bytes) or len(body) != length:
        return b"", 400
    return body, None


def _reject_recovery_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate recovery JSON key")
        out[key] = value
    return out


def _reject_recovery_json_constant(_value):
    raise ValueError("invalid recovery JSON constant")


def _parse_recovery_request(body: bytes) -> dict | None:
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_recovery_duplicate_keys,
            parse_constant=_reject_recovery_json_constant,
        )
    except (TypeError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {"expected_sha", "recovery_id", "delivery_guids"}:
        return None
    expected_sha = value.get("expected_sha")
    recovery_id = value.get("recovery_id")
    delivery_guids = value.get("delivery_guids")
    if not isinstance(expected_sha, str) or not _RECOVERY_SHA.fullmatch(expected_sha):
        return None
    if not isinstance(recovery_id, str) or not _RECOVERY_ID.fullmatch(recovery_id):
        return None
    if not isinstance(delivery_guids, list) or not 1 <= len(delivery_guids) <= 10:
        return None
    if any(not isinstance(guid, str) or not _RECOVERY_GUID.fullmatch(guid) for guid in delivery_guids):
        return None
    if len(set(delivery_guids)) != len(delivery_guids):
        return None
    return value


def _parse_operator_github_redelivery_audit_request(
        body: bytes) -> dict | None:
    """Parse one exact read-only operator request without echoing its values."""
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_recovery_duplicate_keys,
            parse_constant=_reject_recovery_json_constant,
        )
    except (TypeError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {
        "expected_runtime_sha",
        "redelivery_sha",
        "recovery_id",
        "expected_continuation_id",
        "expected_continuation_sha",
    }:
        return None
    expected_runtime_sha = value.get("expected_runtime_sha")
    redelivery_sha = value.get("redelivery_sha")
    recovery_id = value.get("recovery_id")
    continuation_id = value.get("expected_continuation_id")
    continuation_sha = value.get("expected_continuation_sha")
    if (
        not isinstance(expected_runtime_sha, str)
        or _RECOVERY_SHA.fullmatch(expected_runtime_sha) is None
        or not isinstance(redelivery_sha, str)
        or _RECOVERY_SHA.fullmatch(redelivery_sha) is None
        or not isinstance(recovery_id, str)
        or _OPERATOR_RECOVERY_ID.fullmatch(recovery_id) is None
    ):
        return None
    if continuation_id is None and continuation_sha is None:
        return value
    if (
        not isinstance(continuation_id, str)
        or _OPERATOR_RECOVERY_ID.fullmatch(continuation_id) is None
        or not isinstance(continuation_sha, str)
        or _RECOVERY_SHA.fullmatch(continuation_sha) is None
    ):
        return None
    return value


def _exact_runtime_sha() -> str | None:
    """Return the one exact deployed commit, failing closed on absence or disagreement."""
    candidates = []
    for name in ("RENDER_GIT_COMMIT", "VERIPSA_BUILD_SHA"):
        value = (os.environ.get(name) or "").strip()
        if _RECOVERY_SHA.fullmatch(value):
            candidates.append(value)
    build_sha_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "BUILD_SHA"))
    try:
        with open(build_sha_file, "r", encoding="utf-8") as handle:
            value = handle.read(128).strip()
        if _RECOVERY_SHA.fullmatch(value):
            candidates.append(value)
    except (OSError, UnicodeError):
        pass
    exact = set(candidates)
    return next(iter(exact)) if len(exact) == 1 else None


def _validated_recovery_result(value, requested: int) -> dict | None:
    """Fail closed if the DB boundary ever returns a wider or internally inconsistent shape."""
    if not isinstance(value, dict) or set(value) != _RECOVERY_RESULT_KEYS or value.get("status") != "ok":
        return None
    count_keys = ("requested", "rearmed", "already_recovered", "ineligible", "missing")
    if any(type(value.get(key)) is not int or value[key] < 0 for key in count_keys):
        return None
    results = value.get("results")
    if (value["requested"] != requested or not isinstance(results, list)
            or len(results) != requested
            or any(type(item) is not str or item not in _RECOVERY_RESULT_ENUMS for item in results)):
        return None
    if value["rearmed"] + value["already_recovered"] + value["ineligible"] + value["missing"] != requested:
        return None
    # The DB batch is atomic: a successful mutation rearms every requested
    # row, while every mixed/missing/ineligible response mutates none.
    if value["rearmed"] not in (0, requested):
        return None
    for result in _RECOVERY_RESULT_ENUMS:
        if value[result] != results.count(result):
            return None
    return {key: value[key] for key in (
        "status", "requested", "rearmed", "already_recovered", "ineligible", "missing", "results",
    )}


def _validated_recovery_status_result(value, requested: int) -> dict | None:
    """Validate the content-free durable completion proof returned by PostgreSQL."""
    if not isinstance(value, dict) or set(value) != _RECOVERY_STATUS_RESULT_KEYS:
        return None
    if value.get("status") not in _RECOVERY_STATUS_RESULT_ENUMS:
        return None
    if type(value.get("requested")) is not int or value["requested"] != requested:
        return None
    return {"status": value["status"], "requested": value["requested"]}


def _validated_recovery_continuation_result(value, requested: int) -> dict | None:
    """Validate the exact-batch continuation's closed content-free result."""
    if (not isinstance(value, dict)
            or set(value) != _RECOVERY_CONTINUATION_RESULT_KEYS
            or value.get("status") != "ok"):
        return None
    count_keys = (
        "requested", "continued", "already_completed", "already_continued",
        "ineligible", "missing",
    )
    if any(type(value.get(key)) is not int or value[key] < 0 for key in count_keys):
        return None
    results = value.get("results")
    if (value["requested"] != requested or not isinstance(results, list)
            or len(results) != requested
            or any(type(item) is not str
                   or item not in _RECOVERY_CONTINUATION_RESULT_ENUMS
                   for item in results)):
        return None
    if sum(value[key] for key in count_keys[1:]) != requested:
        return None
    for result in _RECOVERY_CONTINUATION_RESULT_ENUMS:
        if value[result] != results.count(result):
            return None
    # A first continuation either stamps the whole original batch (one or
    # more failed rows plus any completed siblings), adopts one already-spent
    # whole batch, or changes nothing.
    first = value["continued"] + value["already_completed"]
    if first not in (0, requested):
        return None
    if value["already_continued"] not in (0, requested):
        return None
    if first and value["continued"] < 1:
        return None
    return {key: value[key] for key in (
        "status", "requested", "continued", "already_completed",
        "already_continued", "ineligible", "missing", "results",
    )}


def _validated_operator_github_redelivery_audit_result(
        value, *, expected_continuation_id, expected_continuation_sha
) -> dict | None:
    """Accept only one exact-three aggregate or the exact unverified sentinel."""
    if (
        isinstance(value, dict)
        and set(value) == {"status"}
        and value.get("status") == "unverified"
    ):
        return {"status": "unverified"}
    state = value.get("state") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != _OPERATOR_GITHUB_REDELIVERY_AUDIT_KEYS
        or value.get("status") != "ok"
        or not isinstance(state, str)
        or state not in _OPERATOR_GITHUB_REDELIVERY_AUDIT_STATES
        or value.get("total") != 3
    ):
        return None
    count_keys = (
        "total", "unspent", "spent", "accepted", "non_202", "ambiguous",
    )
    if any(type(value.get(key)) is not int or value[key] < 0 for key in count_keys):
        return None
    if (
        value["unspent"] + value["spent"] != value["total"]
        or value["accepted"] + value["non_202"] + value["ambiguous"]
        != value["spent"]
        or value["non_202"] + value["ambiguous"] > 1
    ):
        return None
    if state == "pre-spend-unclassified" and not (
        value["unspent"] == 3
        and value["spent"] == value["accepted"]
        == value["non_202"] == value["ambiguous"] == 0
    ):
        return None
    if state == "accepted" and not (
        value["spent"] == value["accepted"] == 3
        and value["unspent"] == value["non_202"] == value["ambiguous"] == 0
    ):
        return None
    if state == "partial-accepted" and not (
        1 <= value["accepted"] <= 2
        and value["spent"] == value["accepted"]
        and value["unspent"] == 3 - value["spent"]
        and value["non_202"] == value["ambiguous"] == 0
    ):
        return None
    if state == "rejected" and not (
        value["non_202"] == 1 and value["ambiguous"] == 0
        and value["spent"] == value["accepted"] + 1
    ):
        return None
    if state == "ambiguous" and not (
        value["ambiguous"] == 1 and value["non_202"] == 0
        and value["spent"] == value["accepted"] + 1
    ):
        return None
    continuation_id = value.get("continuation_id")
    continuation_sha = value.get("continuation_sha")
    if (
        not isinstance(continuation_id, str)
        or _OPERATOR_RECOVERY_ID.fullmatch(continuation_id) is None
        or not isinstance(continuation_sha, str)
        or _RECOVERY_SHA.fullmatch(continuation_sha) is None
        or (
            expected_continuation_id is not None
            and continuation_id != expected_continuation_id
        )
        or (
            expected_continuation_sha is not None
            and continuation_sha != expected_continuation_sha
        )
    ):
        return None
    return {key: value[key] for key in (
        "status", "state", "total", "unspent", "spent", "accepted",
        "non_202", "ambiguous", "continuation_id", "continuation_sha",
    )}


def _delivery_probe_response(worker, secret: str, delivery: str, signature: str | None) -> tuple[int, dict]:
    """Authenticate and read one bounded in-memory worker receipt.

    Content-free: the response is only a terminal outcome tag; it never returns the delivery id, payload,
    repository, account, source body, or diff body.
    """
    if not _DELIVERY_PROBE_ID.fullmatch(delivery or ""):
        return 400, {"status": "invalid"}
    if not secret or not signature:
        return 401, {"status": "unauthorized"}
    try:
        expected = _delivery_probe_signature(secret, delivery)
        authorized = hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii"))
    except (UnicodeEncodeError, TypeError, ValueError):
        authorized = False
    if not authorized:
        return 401, {"status": "unauthorized"}
    status_lookup = getattr(worker, "delivery_status", None)
    lookup = status_lookup if callable(status_lookup) else getattr(worker, "delivery_outcome", None)
    if not callable(lookup):
        return 503, {"status": "unavailable"}
    try:
        outcome = lookup(delivery)
    except Exception:
        return 503, {"status": "unavailable"}
    allowed = ("queued", "processing", "processed", "check_noop", "check_updated", "failed")
    return 200, {"status": outcome if outcome in allowed else "pending"}


def _env_ttl(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        if not math.isfinite(value):
            return default
        return max(0.0, value)
    except (TypeError, ValueError):
        return default


def _healthz_freshness_cache_ttl() -> float:
    """Read the cache TTL at CALL time so an operator can rebind VERIPSA_HEALTHZ_FRESHNESS_CACHE_SECONDS on the
    fly (and so tests can patch it without restart). Default 15s; 0 asks every
    health probe to start/observe an asynchronous single-flight refresh."""
    return _env_ttl("VERIPSA_HEALTHZ_FRESHNESS_CACHE_SECONDS", 15.0)


def _healthz_inbox_depth_cache_ttl() -> float:
    return _env_ttl("VERIPSA_HEALTHZ_INBOX_DEPTH_CACHE_SECONDS", 15.0)


def _freshz_cache_ttl() -> float:
    return _env_ttl("VERIPSA_FRESHZ_CACHE_SECONDS", 15.0)


def _freshz_sync_wait_seconds() -> float:
    return _env_ttl("VERIPSA_FRESHZ_SYNC_WAIT_SECONDS", 1.5)


def _freshz_stale_max_seconds() -> float:
    return _env_ttl("VERIPSA_FRESHZ_STALE_MAX_SECONDS", 300.0)


def _readyz_identity_cache_ttl() -> float:
    return _env_ttl("VERIPSA_READYZ_IDENTITY_CACHE_SECONDS", 5.0)


def _env_positive_int(
        name: str, default: int, *, max_value: int | None = None) -> int:
    """Mirror the operational alert thresholds without making probe config fatal.

    Invalid values fall back to the shipped, fail-safe default.  Readiness
    samples themselves are stricter: missing or malformed evidence is Unknown
    and therefore cannot produce a green readiness result.
    """
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return int(default)
    if value < 1 or (max_value is not None and value > max_value):
        return int(default)
    return value


def _readyz_durable_queued_age_seconds() -> int:
    # Keep this exactly aligned with alerts._default_durable_queued_age_seconds.
    return _env_positive_int(
        "VERIPSA_DURABLE_QUEUED_AGE_SECONDS", 120, max_value=86400)


def _readyz_durable_processing_age_seconds() -> int:
    # Operational readiness uses the existing lane-freeze threshold, not the
    # longer stale-reclaim/dead-letter threshold.  A serialized customer lane
    # that has already crossed this boundary is live but not ready.
    return _env_positive_int(
        "VERIPSA_DURABLE_LANE_FROZEN_SECONDS", 300)


_READYZ_ACCOUNT_CONVERGENCE_CRITICAL_SECONDS = 300
_READYZ_INBOX_FIELDS = (
    "queued",
    "queued_due",
    "queued_due_oldest_age_seconds",
    "processing",
    "processing_oldest_age_seconds",
    "failed",
)
_READYZ_CONVERGENCE_COUNT_FIELDS = (
    "pending",
    "claimed",
    "retry_exhausted",
    "quota_deferred",
    "due_accounts",
    "stalled_accounts",
    "oldest_age_seconds",
    "sample_cap",
)
_READYZ_CONVERGENCE_FLAG_FIELDS = (
    "scheduled_truncated",
    "stalled_truncated",
    "claimed_truncated",
    "exceptions_truncated",
)


def _json_object(value) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _strict_nonnegative_ints(
        sample, fields: tuple[str, ...]) -> dict | None:
    """Return only the declared typed counters, else Unknown.

    ``bool`` is deliberately rejected even though it is an ``int`` subclass.
    Floats and numeric strings are also rejected: a probe response must reflect
    the exact JSONB integer contract, not a caller's coercion guess.
    """
    obj = _json_object(sample)
    if obj is None:
        return None
    out: dict[str, int] = {}
    for field in fields:
        value = obj.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        out[field] = value
    return out


def _durable_inbox_readiness(sample) -> tuple[dict | None, tuple[str, ...]]:
    """Classify the durable webhook inbox for operational readiness.

    This is intentionally stricter than /healthz.  Liveness remains green while
    durable work is safely retained; /readyz becomes non-ready once retained
    work has violated its response/serialization SLO.  Missing or inconsistent
    evidence is Unknown, never Clear.
    """
    depth = _strict_nonnegative_ints(sample, _READYZ_INBOX_FIELDS)
    if depth is None:
        return None, ("durable_inbox_unknown",)
    if (
            depth["queued_due"] > depth["queued"]
            or (depth["queued_due"] == 0
                and depth["queued_due_oldest_age_seconds"] != 0)
            or (depth["processing"] == 0
                and depth["processing_oldest_age_seconds"] != 0)):
        return None, ("durable_inbox_unknown",)

    failures: list[str] = []
    if depth["failed"] > 0:
        failures.append("durable_inbox_dead_letter")
    if (
            depth["queued_due"] > 0
            and depth["queued_due_oldest_age_seconds"]
            >= _readyz_durable_queued_age_seconds()):
        failures.append("durable_inbox_latency")
    if (
            depth["processing"] > 0
            and depth["processing_oldest_age_seconds"]
            >= _readyz_durable_processing_age_seconds()):
        failures.append("durable_inbox_processing_stalled")
    return depth, tuple(failures)


def _account_convergence_readiness(
        sample) -> tuple[dict | None, tuple[str, ...]]:
    """Classify the global, content-free convergence scheduler aggregate.

    Quota-only deferral is expected flow control.  A retry-exhausted row or an
    account whose unfinished work crossed the existing 300s critical boundary
    means the App is live but operationally not ready.  Truncated samples remain
    useful because positive counts are lower bounds and oldest stall age is
    selected from the globally oldest indexed row.
    """
    counts = _strict_nonnegative_ints(
        sample, _READYZ_CONVERGENCE_COUNT_FIELDS)
    obj = _json_object(sample)
    if counts is None or obj is None:
        return None, ("account_convergence_unknown",)
    flags: dict[str, bool] = {}
    for field in _READYZ_CONVERGENCE_FLAG_FIELDS:
        value = obj.get(field)
        if not isinstance(value, bool):
            return None, ("account_convergence_unknown",)
        flags[field] = value
    if (
            counts["sample_cap"] != 1000
            or (counts["stalled_accounts"] == 0
                and counts["oldest_age_seconds"] != 0)
            # The exception sample is account-id ordered. If its first page is
            # all quota deferrals, a retry-exhausted row may exist beyond the
            # cap. That lower bound is not evidence of zero exhaustion.
            or (flags["exceptions_truncated"]
                and counts["retry_exhausted"] == 0)):
        return None, ("account_convergence_unknown",)

    failures: list[str] = []
    if counts["retry_exhausted"] > 0:
        failures.append("account_convergence_retry_exhausted")
    if (
            counts["stalled_accounts"] > 0
            and counts["oldest_age_seconds"]
            >= _READYZ_ACCOUNT_CONVERGENCE_CRITICAL_SECONDS):
        failures.append("account_convergence_stalled")
    return dict(counts, **flags), tuple(failures)


def _public_status_queue_threshold() -> float:
    return _env_ttl("VERIPSA_PUBLIC_STATUS_QUEUE_THRESHOLD", 50.0)


def _health_snapshot_with_liveness(
        snapshot_fn, worker, store, graph_liveness_fn=None,
        graph_hard_seconds: float = 180.0) -> dict:
    """Attach content-free owner + graph-slot liveness.

    Several embedding/tests provide a one-argument ``health_snapshot`` stub.
    Calling that stable seam first and then sampling the optional store keeps
    rolling compatibility while exposing the dedicated heartbeat daemon on the
    real /healthz and /readyz paths. Graph-slot sampling is memory-only: a
    killed child whose daemon reaper never returns must remain visible after
    the event worker has cleared its own inflight record.
    """
    raw = snapshot_fn(worker)
    snap = dict(raw) if isinstance(raw, dict) else {}
    if store is not None:
        try:
            liveness = store.liveness_snapshot()
        except Exception:
            liveness = None
        if isinstance(liveness, dict):
            snap["delivery_liveness"] = liveness
            liveness_healthy = liveness.get("healthy")
            if isinstance(liveness_healthy, bool):
                snap["healthy"] = bool(
                    snap.get("healthy", False) and liveness_healthy)
    if callable(graph_liveness_fn):
        try:
            graph_liveness = graph_liveness_fn(graph_hard_seconds)
        except Exception:
            graph_liveness = None
        if isinstance(graph_liveness, dict):
            snap["graph_extraction_liveness"] = graph_liveness
            graph_healthy = graph_liveness.get("healthy")
            if isinstance(graph_healthy, bool):
                snap["healthy"] = bool(
                    snap.get("healthy", False) and graph_healthy)
    return snap


def _worker_stuck_capacity(worker, snap: dict, threshold: float) -> tuple[int, int]:
    """Return (stuck lanes, still-productive live lanes), content-free."""
    try:
        alive_workers = max(0, int(snap.get("alive_workers", 0)))
    except (TypeError, ValueError):
        alive_workers = 0
    if alive_workers == 0 and snap.get("worker_alive", snap.get("healthy")):
        try:
            alive_workers = max(1, int(snap.get("worker_count", 1)))
        except (TypeError, ValueError):
            alive_workers = 1
    try:
        stuck_workers = int(worker.stuck_worker_count(float(threshold)))
    except (AttributeError, TypeError, ValueError):
        inflight = snap.get("inflight_age_seconds")
        stuck_workers = int(
            isinstance(inflight, (int, float))
            and not isinstance(inflight, bool)
            and inflight >= threshold
        )
    stuck_workers = min(alive_workers, max(0, stuck_workers))
    return stuck_workers, max(0, alive_workers - stuck_workers)


def _state(ok: bool) -> str:
    return "operational" if ok else "degraded"


def _float_or_none(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    return None


def _public_status_payload(snap: dict) -> dict:
    """Coarse, public-safe status surface derived from the worker health snapshot.

    /healthz is operator-facing and may include internal counters. /statusz is for support/trust copy: no repo
    coordinates, PR numbers, delivery ids, Check Run ids/URLs, customer metadata, source bodies, diff bodies, or
    secrets. The Tier 4 Check Run canary remains an operator-controlled observer in GitHub Actions until there is
    a durable public-safe canary result store."""
    if not isinstance(snap, dict):
        snap = {}
    healthy = bool(snap.get("healthy"))
    worker_alive = bool(snap.get("worker_alive", healthy))
    try:
        queue_depth = float(snap.get("queue_depth", 0) or 0)
    except (TypeError, ValueError):
        queue_depth = 0.0
    queue_degraded = queue_depth >= _public_status_queue_threshold()

    fr = snap.get("failure_ratio_5min")
    ratio = _float_or_none(fr.get("ratio")) if isinstance(fr, dict) else None
    threshold = _float_or_none(fr.get("threshold")) if isinstance(fr, dict) else None
    if threshold is None:
        threshold = 0.05
    failure_degraded = ratio is not None and ratio > threshold

    operational = healthy and worker_alive and not queue_degraded and not failure_degraded
    event_state = "unknown" if ratio is None and healthy else _state(not failure_degraded and healthy)
    worker_state = worker_alive and healthy
    return {
        "service": "veripsa-core",
        "status": _state(operational),
        "version": str(snap.get("version") or "unknown"),
        "signals": {
            "worker": _state(worker_state),
            "webhook_ingress": _state(worker_state and not queue_degraded),
            "event_processing": event_state,
            "check_run_canary": "operator_only",
        },
        "check_run_canary": {
            "observer": "GitHub Checks API via post-deploy-smoke",
            "public_last_pass": "not_exposed",
            "note": "Canary PR/head/check-run identifiers stay in operator evidence, not public status JSON.",
        },
        "privacy": {
            "content_free": True,
            "repo_identifiers_exposed": False,
            "pr_identifiers_exposed": False,
            "source_or_diff_bodies_exposed": False,
        },
    }


def _public_freshness_payload(sample: dict) -> dict:
    """Allowlist the unauthenticated freshness response.

    The sampler may inspect cross-tenant coordinates to compute fleet health, but the HTTP surface exposes only
    aggregate counts and bounded sample state. Keep this final projection even though the current cache is already
    aggregate-only: a future sampler field or a malformed cache value must not widen the public contract.
    """
    if not isinstance(sample, dict):
        sample = {}

    def _count(name: str) -> int:
        value = sample.get(name)
        return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0

    coordinate_count = _count("coordinate_count")
    behind_count = min(coordinate_count, _count("behind_count"))
    unknown_count = min(coordinate_count, _count("unknown_count"))
    sampled = sample.get("sampled") is True
    timed_out = sample.get("timed_out") is True

    # Compatibility: pre-coverage/plain-list samplers had no cursor metadata.
    # A successful legacy sample is therefore treated as a complete page. New
    # samplers always carry explicit attributes, and any negative evidence
    # fails closed to Unknown.
    cursor_healthy = sample.get("cursor_healthy", sampled) is True
    coverage_complete = (
        sampled
        and sample.get("coverage_complete", sampled) is True
        and cursor_healthy
        and not timed_out
    )
    any_behind = sample.get("any_behind") is True or behind_count > 0
    status = (
        "behind"
        if any_behind
        else (
            "clear"
            if coverage_complete and unknown_count == 0
            else "unknown"
        )
    )

    out = {
        "service": "veripsa-webhook",
        "coordinate_count": coordinate_count,
        "behind_count": behind_count,
        "unknown_count": unknown_count,
        "any_behind": any_behind,
        "sampled": sampled,
        "coverage_complete": coverage_complete,
        "status": status,
        "timed_out": timed_out,
        "cursor_healthy": cursor_healthy,
    }
    sample_error = sample.get("sample_error")
    if sample_error in ("freshness_sample_failed", "freshness_sample_in_progress"):
        out["sample_error"] = sample_error
    if sample.get("stale_sample") is True:
        out["stale_sample"] = True
        age = _float_or_none(sample.get("sample_age_seconds"))
        if age is not None and age >= 0.0:
            out["sample_age_seconds"] = round(age, 3)
    return out


def _cache_get(slot: dict, key, ttl: float):
    if ttl <= 0.0 or slot.get("key") != key:
        return None
    cached_value = slot.get("value")
    cached_at = slot.get("at", 0.0)
    if cached_value is not None and (time.monotonic() - cached_at) < ttl:
        return cached_value
    return None


def _cache_peek(slot: dict, key):
    """Return the last successful value regardless of age."""
    if slot.get("key") != key:
        return None
    return slot.get("value")


def _cache_put(slot: dict, key, value) -> None:
    slot["key"] = key
    slot["value"] = value
    slot["at"] = time.monotonic()


def _cache_clear(slot: dict, key=None) -> None:
    if key is None or slot.get("key") == key:
        slot["key"] = None
        slot["value"] = None
        slot["at"] = 0.0


def _start_healthz_cache_refresh(
        *, state: dict, slot: dict, key, sampler, thread_name: str) -> None:
    """Start at most one non-blocking liveness-observation refresh.

    DB/NSS/socket timeouts are not trustworthy enough for a platform liveness
    request. A wedged sampler may consume one daemon forever, but it cannot
    consume request threads or spawn an unbounded replacement fleet. Health
    keeps returning the last successful sample (or None) from memory.
    """
    with _HEALTHZ_REFRESH_LOCK:
        current = state.get("thread")
        if current is not None and current.is_alive():
            return

        def _run():
            try:
                value = sampler()
                if value is not None:
                    _cache_put(slot, key, value)
            except Exception:
                pass
            finally:
                me = threading.current_thread()
                with _HEALTHZ_REFRESH_LOCK:
                    if state.get("thread") is me:
                        state["thread"] = None
                        state["key"] = None

        thread = threading.Thread(
            target=_run,
            name=thread_name,
            daemon=True,
        )
        state["key"] = key
        state["thread"] = thread
        try:
            thread.start()
        except Exception:
            state["key"] = None
            state["thread"] = None


def _server():
    """The server module, resolved at CALL time (NOT at load — server.py imports THIS, so a load-time
    `import server` here would be a circular import). server.py is fully loaded by the time any request is
    served (the handler is built + bound by serve()), so this just returns the already-imported module. Mirrors
    event_processor._server. Used to reach every server-resident SEAM the handler calls so its monkeypatch stays
    live: read_bounded_body / verify_signature, _as_obj, _event_account_key / _event_repo, health_snapshot /
    watchdog_last_tick_seconds / app_jwt_reachable, graph_freshness_all, app_identity_ok / _worker_stuck_seconds."""
    try:
        import server  # type: ignore
    except ImportError:  # imported as a package
        from . import server  # type: ignore
    return server


def make_handler(*, secret: str, store, worker, db, dsn: str, gh, persist_all: bool | None = None):
    """Build the webhook request Handler CLASS, closing over the per-process deps serve() wired (the SAME values
    the old inline closure saw): the webhook `secret`, the durable `store` (always wired in production for the
    offboarding boundary), the in-process `worker`, the connect-per-query `db`, the `dsn`, and the live GitHub
    client `gh`. The
    server-resident request functions are reached via _server() at call time (NOT captured here) so their
    monkeypatch seams stay live. `persist_all` defaults to whether a store was supplied for legacy/test callers;
    production passes it explicitly so repository offboarding can retain the store while ordinary events honor
    VERIPSA_DURABLE_INBOX=0. serve() subclasses the returned class to set `timeout = _req_timeout` and binds it
    under a ThreadingHTTPServer."""

    persist_every_event = store is not None if persist_all is None else bool(persist_all)

    class Handler(BaseHTTPRequestHandler):
        def end_headers(self):
            for key, value in _SECURITY_HEADERS:
                self.send_header(key, value)
            super().end_headers()

        def do_POST(self):
            recovery_path = getattr(self, "path", "")
            if recovery_path in (
                    _RECOVERY_PATH, _RECOVERY_STATUS_PATH,
                    _RECOVERY_CONTINUATION_PATH,
                    _OPERATOR_GITHUB_REDELIVERY_AUDIT_PATH):
                status_probe = recovery_path == _RECOVERY_STATUS_PATH
                continuation = recovery_path == _RECOVERY_CONTINUATION_PATH
                operator_audit = (
                    recovery_path == _OPERATOR_GITHUB_REDELIVERY_AUDIT_PATH
                )
                # Operator recovery is reachable only on the HTTP/web role.
                # Worker/convergence processes must never acquire an ambient
                # control-plane surface merely because they share this image.
                if os.environ.get("VERIPSA_RUNTIME_ROLE") != "web":
                    self._json(404, {"status": "unavailable"})
                    return
                body, body_error = _read_recovery_body(
                    self.headers.get("Content-Length"), self.rfile,
                )
                if body_error is not None:
                    self._json(400, {"status": "invalid"})
                    return
                authorized = (
                    _operator_github_redelivery_audit_authorized
                    if operator_audit
                    else _recovery_status_authorized if status_probe
                    else _recovery_continuation_authorized if continuation
                    else _recovery_authorized
                )
                if not authorized(secret, body, self.headers.get(_RECOVERY_SIGNATURE_HEADER)):
                    self._json(401, {"status": "unauthorized"})
                    return
                request = (
                    _parse_operator_github_redelivery_audit_request(body)
                    if operator_audit
                    else _parse_recovery_request(body)
                )
                if request is None:
                    self._json(400, {"status": "invalid"})
                    return
                runtime_sha = _exact_runtime_sha()
                if runtime_sha is None:
                    self._json(503, {"status": "unavailable"})
                    return
                expected_runtime_sha = (
                    request["expected_runtime_sha"]
                    if operator_audit
                    else request["expected_sha"]
                )
                if expected_runtime_sha != runtime_sha:
                    self._json(409, {"status": "sha_mismatch"})
                    return
                operation = getattr(
                    store,
                    "operator_github_redelivery_audit" if operator_audit
                    else "terminal_recovery_status" if status_probe
                    else "continue_terminal" if continuation
                    else "recover_terminal",
                    None,
                )
                if not callable(operation):
                    self._json(503, {"status": "unavailable"})
                    return
                try:
                    if operator_audit:
                        result = operation(
                            request["recovery_id"],
                            request["redelivery_sha"],
                            request["expected_continuation_id"],
                            request["expected_continuation_sha"],
                        )
                    elif status_probe:
                        result = operation(request["delivery_guids"])
                    elif continuation:
                        result = operation(
                            request["delivery_guids"], request["recovery_id"],
                            request["expected_sha"])
                    else:
                        result = operation(
                            request["delivery_guids"], request["recovery_id"])
                except Exception:
                    # No identifiers or exception text: both can contain the
                    # operator correlation token or a tenant delivery GUID.
                    if operator_audit:
                        print(
                            "operator GitHub redelivery audit operation failed",
                            flush=True,
                        )
                    else:
                        print(
                            "durable delivery operator recovery operation failed "
                            f"requested={len(request['delivery_guids'])}",
                            flush=True,
                        )
                    self._json(503, {"status": "unavailable"})
                    return
                if operator_audit:
                    response = _validated_operator_github_redelivery_audit_result(
                        result,
                        expected_continuation_id=request[
                            "expected_continuation_id"
                        ],
                        expected_continuation_sha=request[
                            "expected_continuation_sha"
                        ],
                    )
                    if response is None:
                        self._json(503, {"status": "unavailable"})
                        return
                    self._json(200, response)
                    return
                validator = (
                    _validated_recovery_status_result if status_probe
                    else _validated_recovery_continuation_result if continuation
                    else _validated_recovery_result
                )
                response = validator(result, len(request["delivery_guids"]))
                if response is None:
                    self._json(503, {"status": "unavailable"})
                    return
                self._json(200, response)
                return

            S = _server()
            # ACK-FAST: verify the signature, ENQUEUE, and return within GitHub's ~10s timeout. The actual
            # work (which may clone/extract or wait out a rate limit) happens on the background worker.
            # BOUNDED read: reject an oversize/forged/short body BEFORE buffering or parsing it (memory-DoS guard).
            body, err = S.read_bounded_body(self.headers.get("Content-Length"), self.rfile)
            if err is not None:                                # 413 (too large) / 400 (truncated) → never enqueue
                self.send_response(err); self.end_headers()
                self.wfile.write(b"payload too large" if err == 413 else b"bad length"); return
            if not S.verify_signature(secret, body, self.headers.get("X-Hub-Signature-256")):
                self.send_response(401); self.end_headers(); self.wfile.write(b"bad signature"); return  # forgeries: never enqueue
            event_type = self.headers.get("X-GitHub-Event", "")
            delivery = self.headers.get("X-GitHub-Delivery", "")
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                self.send_response(400); self.end_headers(); self.wfile.write(b"bad json"); return
            try:
                _payload_obj = S._as_obj(payload)
                _acct = S._event_account_key(_payload_obj)
                _repo = S._event_repo(_payload_obj)
                _action = _payload_obj.get("action") or ""
                _prj = S._as_obj(_payload_obj.get("pull_request"))
                _pr = _payload_obj.get("number") or _prj.get("number") or ""
                _head = S._as_obj(_prj.get("head")).get("sha") or _payload_obj.get("after") or ""
            except Exception:
                _acct, _repo, _action, _pr, _head = None, None, "", "", ""
            # INGRESS NO-OP FAST-ACK (see _ingress_noop_fastack): drop always-skipped check_run storm events
            # BEFORE persist/enqueue, 2xx so GitHub never retries. Identical outcome (skipped), none of the
            # pipeline/fairness-cap cost that starves real PR/push work. check_suite (incl. the canary) untouched.
            if event_type == "check_run" and _ingress_noop_fastack(
                    event_type, _action,
                    S._as_obj(payload.get("check_run")).get("conclusion"),
                    S._FAILING_CONCLUSIONS, enabled=_INGRESS_NOOP_FASTACK):
                global _INGRESS_NOOP_COUNT
                with _INGRESS_NOOP_LOCK:
                    _INGRESS_NOOP_COUNT += 1
                    _noop_n = _INGRESS_NOOP_COUNT
                if _noop_n == 1 or _noop_n % 500 == 0:
                    print(f"ingress no-op fast-ack: {_noop_n} check_run created/passing-completed events dropped "
                          f"before enqueue (VERIPSA_INGRESS_NOOP_FASTACK=0 to disable)", flush=True)
                self.send_response(202); self.end_headers(); self.wfile.write(b"accepted"); return
            _offboard = _requires_durable_repository_offboard(event_type, payload)
            _event_store = store if (persist_every_event or _offboard) else None
            if _offboard and _event_store is None:
                # Production always wires the authority store. A miswired/local receiver must not 202 work its
                # processor cannot authorize; GitHub requires operator/automation redelivery after a failed hook.
                print(f"lifecycle authority durable store unavailable — 503; Core recovery will redeliver "
                      f"event={event_type or 'missing'} action={_action or 'missing'}", flush=True)
                self.send_response(503); self.end_headers(); self.wfile.write(b"busy"); return
            # DURABILITY BOUNDARY: PERSIST a SANITIZED delivery to core.webhook_delivery BEFORE replying 202, so a
            # crash/deploy/OOM after the ack cannot lose it (GitHub does not redeliver a 202'd delivery; the boot
            # recovery loop replays any unfinished row). The in-memory _FairQueue is then fed the SANITIZED,
            # delivery-keyed payload so the worker claims/finishes the SAME row it processes. The store is keyed by
            # the X-GitHub-Delivery id, so operator/automated redelivery is idempotent (ON CONFLICT — never a
            # duplicate row). The ordinary-event kill switch still selects this path for repository offboarding.
            if _event_store is not None:
                try:
                    # best-effort operational columns (content-free: repo full_name + account id only); the durable
                    # row's AUTHORITY does not depend on them — they exist only for the per-repo recovery index.
                    res = _event_store.submit(event_type, payload, delivery, account_key=_acct, repo=_repo)
                except Exception as e:
                    # The persist FAILED (DB blip), so the event is not durably accepted. GitHub records the 503
                    # but does not automatically redeliver; operator/automation must redeliver the failed hook.
                    # PostgreSQL exception text can embed DETAIL with the whole failing row. Never log it here:
                    # the provider GUID and minimized payload are capabilities/data, not observability fields.
                    print("durable inbox persist FAILED — 503; Core recovery will redeliver: "
                          f"error_code={_bounded_error_code(e)}", flush=True)
                    self.send_response(503); self.end_headers(); self.wfile.write(b"busy"); return
                if not res.get("accepted"):                    # durable backlog full → failed delivery; no 202
                    self.send_response(503); self.end_headers(); self.wfile.write(b"busy"); return
                if res.get("queued") is False:
                    # Idempotent GitHub redelivery of an already-processing/done durable row. Its original worker
                    # generation remains authoritative; enqueueing another one would fail its claim and overwrite
                    # a valid terminal receipt. The durable row proves this delivery was already accepted.
                    print(f"webhook duplicate accepted delivery={'present' if delivery else 'missing'} "
                          f"event={event_type or 'missing'} state=already-owned", flush=True)
                    self.send_response(202); self.end_headers(); self.wfile.write(b"accepted"); return
                # persisted; hand the SANITIZED, delivery-keyed payload to the in-process scheduler.
                event_type, payload, delivery = res["event_type"], res["payload"], res["delivery"]
            if worker.submit(event_type, payload, delivery):
                print(f"webhook accepted delivery={'present' if delivery else 'missing'} "
                      f"event={event_type or 'missing'} "
                      f"action={_action or 'missing'} repo={_repo or 'missing'} pr={_pr or 'missing'} "
                      f"head={str(_head)[:12] or 'missing'} "
                      f"queue={'durable' if persist_every_event else ('durable-authority' if _event_store else 'memory')}",
                      flush=True)
                self.send_response(202); self.end_headers(); self.wfile.write(b"accepted")
            else:
                # In-memory scheduler full (global bound or per-account fairness cap). When this event used the
                # durable store its row is already persisted 'queued' for recovery, so nothing is lost. A memory-
                # only event remains a failed GitHub delivery and needs the documented redelivery automation.
                self.send_response(503); self.end_headers(); self.wfile.write(b"busy")

        def do_GET(self):
            S = _server()
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path.startswith("/deliveryz/"):
                delivery = path[len("/deliveryz/"):]
                code, payload = _delivery_probe_response(
                    worker,
                    secret,
                    delivery,
                    self.headers.get("X-Veripsa-Delivery-Probe-Signature"),
                )
                self._json(code, payload)
            elif path in ("/statusz", "/status"):              # PUBLIC-SAFE coarse status (no repo/PR/canary ids)
                payload = _public_status_payload(
                    _health_snapshot_with_liveness(
                        S.health_snapshot, worker, store,
                        getattr(S, "graph_extraction_liveness", None),
                        getattr(S, "_worker_restart_seconds",
                                lambda: 180.0)()))
                self._json(200 if payload.get("status") == "operational" else 503, payload)
            elif path in ("/healthz", "/health"):              # LIVENESS: worker-driven (dead → 503 → restart)
                stuck_seconds = getattr(
                    S, "_worker_stuck_seconds", lambda: 120.0)()
                restart_seconds = getattr(
                    S, "_worker_restart_seconds", lambda: 180.0)()
                snap = _health_snapshot_with_liveness(
                    S.health_snapshot, worker, store,
                    getattr(S, "graph_extraction_liveness", None),
                    restart_seconds)
                # HARD LIVENESS ENVELOPE. A worker thread can remain alive
                # inside a syscall/native boundary that failed to cooperate
                # with the 90s event cancellation budget. One stuck keyed lane
                # is isolated and must not make unrelated accounts lose the
                # sole Render instance; the watchdog pages it. It receives one
                # short continuity grace, then any lane still stuck crosses an
                # absolute replacement bound—otherwise its process-local
                # recovery reservation and worker slot would be stranded
                # forever. Exhausting every live lane skips that grace.
                # Exact durable leases, terminal intents, and dead-owner
                # fencing make replacement recoverable.
                stuck_workers, available_workers = _worker_stuck_capacity(
                    worker, snap, stuck_seconds)
                worker_stuck = stuck_workers > 0
                capacity_exhausted = worker_stuck and available_workers == 0
                restart_workers, _ = _worker_stuck_capacity(
                    worker, snap, restart_seconds)
                restart_required = restart_workers > 0
                snap["worker_stuck"] = worker_stuck
                snap["stuck_workers"] = stuck_workers
                snap["available_workers"] = available_workers
                snap["worker_capacity_exhausted"] = capacity_exhausted
                snap["restart_required_workers"] = restart_workers
                snap["worker_restart_required"] = restart_required
                if capacity_exhausted or restart_required:
                    snap["healthy"] = False
                # FRESHNESS SIGNAL (memory-only on this request): surface the last successful DB advisory sample.
                # A cold/expired cache starts one background single-flight and returns immediately, so DB/kernel
                # stalls can never hang Render's liveness decision. Behind-HEAD remains advisory on /freshz.
                snap["graph_freshness"] = self._healthz_freshness_nonblocking()
                # WATCHDOG LIVENESS: the age of the MONITOR's last completed tick (None until it first ticks). So a
                # watchdog that silently stopped — despite its loop now being crash-proof — is VISIBLE here instead
                # of leaving /healthz green while proactive alerting is dead (nothing else watches the monitor).
                # ADVISORY (a stale/None tick never 503s liveness: the worker can be perfectly alive while the
                # monitor thread is the problem). Distinct from inflight_age_seconds, which is the WORKER's wedge.
                snap["watchdog_last_tick_seconds"] = S.watchdog_last_tick_seconds()
                # DURABLE INBOX DEPTH (the known-but-unexposed observability gap): the watchdog already samples the
                # durable webhook inbox (queued / processing / dead-lettered 'failed' counts) for ALERTING, but the
                # operator-facing probe never surfaced it — so a growing 'queued' backlog or an accumulating 'failed'
                # (dead-letter) row was invisible to a human curling /healthz (the exact blind spot behind the
                # 2026-06-26 "why is failure_ratio up?" triage). Expose it. ADVISORY — never 503s liveness (a backlog
                # is the worker's pace, the durable rows are safe); content-free (counts only, no payloads/ids).
                # Fail-open: any read error omits the block. Only when the durable store is wired (live serve() path).
                if store is not None:
                    inbox_depth = self._healthz_inbox_depth_nonblocking()
                    if inbox_depth is not None:
                        snap["inbox_depth"] = inbox_depth
                # BOOT-TIME SCHEMA↔RUNTIME CONTRACT (the 2026-06-25 defense): if the boot path's schema-contract
                # check found a mismatch (live DB missing a function the runtime calls, or wrong arity, or missing
                # column), 503 forever until restarted into a fixed DB. This makes the "Python ahead of DB schema"
                # class of deploy mechanically impossible — Render's deploy gate fails when /healthz fails →
                # previous live commit stays serving. The block is reported in the body so an operator sees the
                # exact missing function/arity/column. Honored even when snap["healthy"] is True (the worker
                # would happily process every event into 42883 SQLSTATE failures otherwise — the 2026-06-25
                # incident shape). See github-app/schema_contract.py for the contract definition + kill switch.
                contract = _schema_contract_result()
                schema_ok = True
                if contract is not None:
                    snap["schema_contract"] = {
                        "checked": contract.checked,
                        "skipped": contract.skipped,
                        "healthy": contract.healthy,
                        "violations": [
                            {"kind": v.kind, "name": v.name,
                             "expected": v.expected, "actual": v.actual}
                            for v in contract.violations
                        ],
                    }
                    schema_ok = contract.healthy
                self._json(200 if (snap["healthy"] and schema_ok) else 503, snap)
            elif path in ("/alarmz", "/alarm"):                # WINDOWED FAILURE-RATIO ALARM (the 2026-06-25 incident gap)
                # WHY: the SQLSTATE-42883 incident failed EVERY PR event for an hour while /healthz stayed
                # 200 (queue=0 because events failed fast; worker_alive=True; cumulative processed/failed
                # both climbed together so the cumulative ratio was steady). D1 (schema-contract assertion)
                # now refuses to start when that exact deploy shape is detected — but other failure modes
                # (a transient GitHub auth fault, an upstream rate-limit, a per-tenant misconfiguration that
                # only shows up in production traffic) still need a windowed signal. /alarmz turns the
                # windowed failure ratio (failed / (processed + failed) over the last `window_seconds`)
                # into a 503 the moment it exceeds VERIPSA_FAILURE_RATIO_THRESHOLD — so an EXTERNAL alerter
                # (Render's own health check pointed here, or UptimeRobot) PAGES on an elevated ratio,
                # independently of the worker-liveness contract /healthz keeps. Empty window (cold start /
                # no events yet) = ratio None = 200: we cannot page on no data (honest unknown, not a false
                # alarm). The body mirrors what /healthz returns under `failure_ratio_5min`, so the same
                # JSON consumer works against either endpoint. Content-free (counts + a ratio; never event
                # bodies). See [[project_p0_incident_2026_06_25]] D4 of the 4-layer defense.
                try:
                    try:
                        from health_watchdog import failure_ratio_window as _frw  # call-time: no load-time cycle
                    except ImportError:  # imported as a package
                        from .health_watchdog import failure_ratio_window as _frw
                    fr = _frw()
                except Exception:
                    fr = None
                ratio = fr.get("ratio") if isinstance(fr, dict) else None
                threshold = fr.get("threshold", 0.05) if isinstance(fr, dict) else 0.05
                alarming = ratio is not None and ratio > threshold
                self._json(503 if alarming else 200, {
                    "service": "veripsa-webhook",
                    "alarming": alarming,
                    "failure_ratio": fr,
                })
            elif path in ("/freshz", "/fresh"):                # FRESHNESS: coarse stored main-graph vs HEAD state
                # Public-safe aggregate only. The sampler still compares each internal coordinate, but repository,
                # branch, account, commit, graph-count, and timestamp metadata never crosses the HTTP boundary.
                self._json(200, _public_freshness_payload(self._freshz_payload_cached()))
            elif path in ("/readyz", "/ready"):                # READINESS: DB reachable AND the App can resolve its identity
                stuck_seconds = getattr(
                    S, "_worker_stuck_seconds", lambda: 120.0)()
                restart_seconds = getattr(
                    S, "_worker_restart_seconds", lambda: 180.0)()
                snap = _health_snapshot_with_liveness(
                    S.health_snapshot, worker, store,
                    getattr(S, "graph_extraction_liveness", None),
                    restart_seconds)
                db_ok = True
                try:
                    db("SELECT 1")
                except Exception:
                    db_ok = False
                # DEPLOY-BLOCKER self-check: confirm the App role actually RESOLVES its service identity the way a
                # live event does (pinned installation, no per-account credential). If the schema's service path is
                # misconfigured this would 42501 on every webhook — so fail readiness LOUDLY instead of silently.
                identity_ok, identity_error = self._app_identity_cached() if db_ok else (False, "db unreachable")
                # A partially degraded keyed pool remains ready for unrelated
                # accounts during its short continuity grace. Exhaustion of
                # every live lane or one lane crossing the absolute replacement
                # bound makes the single-instance process unready.
                stuck_workers, available_workers = _worker_stuck_capacity(
                    worker, snap, stuck_seconds)
                worker_stuck = stuck_workers > 0
                capacity_exhausted = worker_stuck and available_workers == 0
                restart_workers, _ = _worker_stuck_capacity(
                    worker, snap, restart_seconds)
                restart_required = restart_workers > 0
                readiness_failures: list[str] = []
                inbox_depth = None
                if store is None:
                    inbox_failures = ("durable_inbox_unknown",)
                else:
                    try:
                        inbox_depth, inbox_failures = (
                            _durable_inbox_readiness(store.depth()))
                    except Exception:
                        # Connection/statement details may contain credentials.
                        # The stable reason code is sufficient and content-free.
                        inbox_failures = ("durable_inbox_unknown",)
                readiness_failures.extend(inbox_failures)

                account_convergence = None
                if db_ok:
                    try:
                        account_convergence, convergence_failures = (
                            _account_convergence_readiness(db(
                                "SELECT "
                                "core.account_convergence_depth_with_authority()"
                            )))
                    except Exception:
                        convergence_failures = (
                            "account_convergence_unknown",)
                else:
                    convergence_failures = (
                        "account_convergence_unknown",)
                readiness_failures.extend(convergence_failures)
                ready = (
                    snap["healthy"] and db_ok and identity_ok
                    and not capacity_exhausted and not restart_required
                    and not readiness_failures
                )
                # APP-JWT REACHABILITY (advisory — reported, NEVER blocks ready): the watchdog's last GET /app/
                # installations probe (the LIVE 2026-06-26 gap — a systemic 403 there blinds every background loop
                # while the DB-side app_identity_ok above stays True). Read from the watchdog's STAMPED slot (no
                # GitHub call per probe — readyz stays cheap); None until the watchdog first probes. A False here
                # does NOT 503 readiness: the worker + DB can be healthy while the App-JWT is the problem, and the
                # 403 root cause is an out-of-band operator fix — the watchdog already PAGES (app_jwt_unreachable).
                app_jwt_reachable = S.app_jwt_reachable()
                # FRESHNESS (advisory in readiness — reported, never blocks ready: a behind graph self-heals + the
                # watchdog already alerts). DB-only summary keeps /readyz from doing a GitHub call per probe.
                self._json(200 if ready else 503, dict(snap, db_reachable=db_ok,
                                                       app_identity_ok=identity_ok, app_identity_error=identity_error,
                                                       worker_stuck=worker_stuck,
                                                       stuck_workers=stuck_workers,
                                                       available_workers=available_workers,
                                                       worker_capacity_exhausted=capacity_exhausted,
                                                       restart_required_workers=restart_workers,
                                                       worker_restart_required=restart_required,
                                                       app_jwt_reachable=app_jwt_reachable,
                                                       inbox_depth=inbox_depth,
                                                       account_convergence=account_convergence,
                                                       readiness_failures=readiness_failures,
                                                       graph_freshness=(self._freshness_summary_db_only() if db_ok else None),
                                                       ready=ready))
            else:
                self._json(404, {"error": "not found"})

        def _freshness_summary_now(self):
            try:
                raw = db("SELECT core.owner_graph_freshness_surface()")
                surface = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
                if not isinstance(surface, dict):
                    return None
                return {
                    "coordinate_count": surface.get("coordinate_count"),
                    "max_age_seconds": surface.get("max_age_seconds"),
                }
            except Exception:
                return None

        def _freshness_summary_db_only(self):
            """Cheap, DB-only freshness summary (no GitHub call) for the liveness/readiness bodies: the count of
            recorded coordinates + the OLDEST coordinate's age (the worst staleness). The behind-HEAD boolean
            needs main HEAD → /freshz. Fail-open: any error returns None (the block is simply omitted).

            This synchronous form is used only by /readyz, which is explicitly
            DB-touching already. /healthz uses the non-blocking form below.
            The cache slot is module-level so all handler instances share it."""
            ttl = _healthz_freshness_cache_ttl()
            cache_key = id(db)
            # Snapshot (value, at) so a concurrent writer can never tear what we read.
            cached_value = _cache_get(_HEALTHZ_FRESHNESS_CACHE, cache_key, ttl)
            if cached_value is not None:
                return cached_value
            out = self._freshness_summary_now()
            if ttl > 0.0 and out is not None:
                _cache_put(_HEALTHZ_FRESHNESS_CACHE, cache_key, out)
            return out

        def _healthz_freshness_nonblocking(self):
            """Return freshness from memory and refresh it off-request."""
            ttl = _healthz_freshness_cache_ttl()
            cache_key = id(db)
            fresh = _cache_get(
                _HEALTHZ_FRESHNESS_CACHE, cache_key, ttl)
            if fresh is not None:
                return fresh
            stale = _cache_peek(_HEALTHZ_FRESHNESS_CACHE, cache_key)
            _start_healthz_cache_refresh(
                state=_HEALTHZ_FRESHNESS_REFRESH,
                slot=_HEALTHZ_FRESHNESS_CACHE,
                key=cache_key,
                sampler=self._freshness_summary_now,
                thread_name="veripsa-healthz-freshness",
            )
            return stale

        def _inbox_depth_cached(self):
            """Synchronous cached depth for DB-touching diagnostic callers."""
            ttl = _healthz_inbox_depth_cache_ttl()
            cache_key = id(store)
            cached_value = _cache_get(_HEALTHZ_INBOX_DEPTH_CACHE, cache_key, ttl)
            if cached_value is not None:
                return cached_value
            try:
                out = store.depth()
                if ttl > 0.0:
                    _cache_put(_HEALTHZ_INBOX_DEPTH_CACHE, cache_key, out)
                return out
            except Exception:
                return None

        def _healthz_inbox_depth_nonblocking(self):
            """Return durable depth from memory and refresh it off-request."""
            ttl = _healthz_inbox_depth_cache_ttl()
            cache_key = id(store)
            fresh = _cache_get(
                _HEALTHZ_INBOX_DEPTH_CACHE, cache_key, ttl)
            if fresh is not None:
                return fresh
            stale = _cache_peek(_HEALTHZ_INBOX_DEPTH_CACHE, cache_key)
            _start_healthz_cache_refresh(
                state=_HEALTHZ_INBOX_DEPTH_REFRESH,
                slot=_HEALTHZ_INBOX_DEPTH_CACHE,
                key=cache_key,
                sampler=store.depth,
                thread_name="veripsa-healthz-inbox-depth",
            )
            return stale

        def _freshz_payload_cached(self):
            """Coarse public-safe stored-graph-vs-main freshness payload.

            The live sample may call GitHub once per coordinate. Keep /freshz bounded: use a fresh cached sample
            immediately; on cache miss, start/observe one background refresh and wait only a short grace window.
            If it is still running, return the last successful sample marked stale, or an explicit sampled:false
            fallback. The per-coordinate records are used only to derive aggregate counts and are never cached or
            returned. TTL=0 restores the old synchronous every-request sample behavior for debugging."""
            ttl = _freshz_cache_ttl()
            S = _server()
            cache_key = (id(db), id(gh), id(S.graph_freshness_all))
            if ttl <= 0.0:
                return self._freshz_sample_now(S, cache_key, cache_success=False)
            cached_value = _cache_get(_FRESHZ_CACHE, cache_key, ttl)
            if cached_value is not None:
                return cached_value
            th = self._freshz_refresh_started(S, cache_key)
            wait = _freshz_sync_wait_seconds()
            refresh_finished = False
            if wait > 0.0:
                th.join(wait)
                refresh_finished = not th.is_alive()
                cached_value = _cache_get(_FRESHZ_CACHE, cache_key, ttl)
                if cached_value is not None:
                    return cached_value
            stale = self._freshz_stale_payload(cache_key)
            if stale is not None:
                return stale
            reason = "freshness_sample_failed" if refresh_finished else "freshness_sample_in_progress"
            return self._freshz_unsampled_payload(reason)

        def _freshz_sample_now(self, S, cache_key, *, cache_success: bool):
            fresh = []
            sampled = True
            try:
                fresh = S.graph_freshness_all(db, gh)
                if not isinstance(fresh, list):
                    raise TypeError("freshness sampler returned a non-list")
            except Exception as e:
                sampled = False
                fresh = []
                print(f"/freshz sample skipped: {str(e)[:120]}", flush=True)

            behind = [
                f for f in fresh
                if isinstance(f, dict) and f.get("behind") is True
            ]
            unknown_count = sum(
                1 for f in fresh
                if not isinstance(f, dict)
                or (
                    f.get("behind") is not True
                    and f.get("behind") is not False
                )
            )
            timed_out = sampled and getattr(fresh, "timed_out", False) is True
            cursor_healthy = (
                sampled and getattr(fresh, "cursor_healthy", True) is True
            )
            coverage_complete = (
                sampled
                and getattr(fresh, "coverage_complete", True) is True
                and cursor_healthy
                and not timed_out
            )
            status = (
                "behind"
                if behind
                else (
                    "clear"
                    if coverage_complete and unknown_count == 0
                    else "unknown"
                )
            )
            out = {"service": "veripsa-webhook",
                   "coordinate_count": len(fresh), "behind_count": len(behind),
                   "unknown_count": unknown_count,
                   "any_behind": bool(behind), "sampled": sampled,
                   "coverage_complete": coverage_complete,
                   "status": status, "timed_out": timed_out,
                   "cursor_healthy": cursor_healthy}
            if not sampled:
                out["sample_error"] = "freshness_sample_failed"
            if cache_success and sampled:
                _cache_put(_FRESHZ_CACHE, cache_key, out)
            return out

        def _freshz_refresh_started(self, S, cache_key):
            with _FRESHZ_REFRESH_LOCK:
                th = _FRESHZ_REFRESH.get("thread")
                if th is not None and th.is_alive() and _FRESHZ_REFRESH.get("key") == cache_key:
                    return th

                def _run():
                    try:
                        self._freshz_sample_now(S, cache_key, cache_success=True)
                    finally:
                        cur = threading.current_thread()
                        with _FRESHZ_REFRESH_LOCK:
                            if _FRESHZ_REFRESH.get("thread") is cur:
                                _FRESHZ_REFRESH["thread"] = None
                                _FRESHZ_REFRESH["key"] = None
                                _FRESHZ_REFRESH["started"] = 0.0

                th = threading.Thread(target=_run, name="veripsa-freshz-sample", daemon=True)
                _FRESHZ_REFRESH["key"] = cache_key
                _FRESHZ_REFRESH["thread"] = th
                _FRESHZ_REFRESH["started"] = time.monotonic()
                th.start()
                return th

        def _freshz_stale_payload(self, cache_key):
            if _FRESHZ_CACHE.get("key") != cache_key:
                return None
            value = _FRESHZ_CACHE.get("value")
            cached_at = _FRESHZ_CACHE.get("at", 0.0)
            if value is None:
                return None
            age = time.monotonic() - cached_at
            max_age = _freshz_stale_max_seconds()
            if max_age <= 0.0 or age > max_age:
                return None
            out = dict(value)
            out["stale_sample"] = True
            out["sample_age_seconds"] = round(age, 3)
            return out

        def _freshz_unsampled_payload(self, reason: str):
            # Fast DB-only count for shape/honesty; if even that blips, return an explicit sampled:false payload.
            count = 0
            try:
                raw = db("SELECT core.owner_graph_freshness_surface()")
                surface = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
                if isinstance(surface, dict):
                    raw_coords = surface.get("coordinates")
                    coordinates = raw_coords if isinstance(raw_coords, list) else []
                    count = (surface.get("coordinate_count")
                             if isinstance(surface.get("coordinate_count"), int)
                             else len(coordinates))
            except Exception:
                count = 0
            return {"service": "veripsa-webhook", "coordinate_count": count,
                    "behind_count": 0, "unknown_count": count,
                    "any_behind": False, "sampled": False,
                    "coverage_complete": False, "status": "unknown",
                    "timed_out": False, "cursor_healthy": False,
                    "sample_error": reason}

        def _app_identity_cached(self):
            """Readiness identity self-check. Cache only successful checks for a very short TTL; failures clear the
            cache and keep readiness fail-closed."""
            ttl = _readyz_identity_cache_ttl()
            S = _server()
            cache_key = (dsn, id(S.app_identity_ok))
            cached_value = _cache_get(_READYZ_IDENTITY_CACHE, cache_key, ttl)
            if cached_value is not None:
                return cached_value
            out = S.app_identity_ok(dsn)
            if ttl > 0.0 and out[0] is True:
                _cache_put(_READYZ_IDENTITY_CACHE, cache_key, out)
            else:
                _cache_clear(_READYZ_IDENTITY_CACHE, cache_key)
            return out

        def _json(self, code: int, obj: dict):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    return Handler
