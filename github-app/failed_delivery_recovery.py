#!/usr/bin/env python3
"""Bounded, content-free recovery for failed GitHub App webhook deliveries.

GitHub does not automatically retry failed webhook deliveries.  The App-level delivery API retains metadata for
three days, so a low-priority loop scans that metadata and asks GitHub to redeliver every explicit bounded non-OK
delivery; malformed/unknown outcomes remain durable evidence without a guessed POST. Webhook bodies/details are
never fetched. Marketplace and Sponsors webhooks are not
available through this API and are therefore explicitly outside this mechanism.

The database owns every decision that must survive a crash: scan epoch/high-water/cursor, GUID newest-state
grouping, local durable-receipt resolution, and the max-three retry budget.  A candidate claim commits before the
network POST.  A 202 or transport error is deliberately only an ambiguous attempt followed by cooldown; neither
is treated as successful delivery.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import threading
from typing import Any

try:
    from github_rest_installs import InvalidDeliveryCursor
except ImportError:  # imported as a package
    from .github_rest_installs import InvalidDeliveryCursor


_SCAN_PAGES_PER_TICK = 3
_CANDIDATES_PER_TICK = 5
_SCAN_PAGE_SIZE = 100
_INTERVAL_SECONDS = 60
_START_DELAY_SECONDS = 120
# RECOVERY-QUEUE RE-ARM cadence (stabilization 2026-07-19): give an EXHAUSTED but still-redeliverable, still-in-window
# delivery one fresh attempt-budget once its last attempt is older than this, so a transient-outage casualty is not
# stuck exhausted (+ CRITICAL github_delivery_redelivery_exhausted alert) for the whole ~33-day retention. The SQL's
# own age/window/class gate is the real guard; the loop just issues the cheap sweep about once per this window. 6h =
# long enough for a fleet-wide 403/429/5xx cause to clear and the alert to have paged a human first.
_REARM_SECONDS = 21600
_DB_STATEMENT_TIMEOUT_MS = 10_000
_DB_LOCK_TIMEOUT_MS = 2_000


def _as_json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _aware_time(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and len(value) <= 80:
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        except ValueError as exc:
            raise RuntimeError(f"{label} is malformed") from exc
    else:
        raise RuntimeError(f"{label} is malformed")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{label} has no timezone")
    return parsed.astimezone(timezone.utc)


def _delivery_metadata(value: Any) -> dict:
    """Reduce one list item to the five operational fields Core accepts; reject a partial proof."""
    if not isinstance(value, dict):
        raise RuntimeError("GitHub delivery metadata item is malformed")
    delivery_id = value.get("id")
    status_code = value.get("status_code")
    guid = value.get("guid")
    status = value.get("status")
    if (isinstance(delivery_id, bool) or not isinstance(delivery_id, int) or delivery_id <= 0
            or not isinstance(guid, str) or not 1 <= len(guid) <= 200
            or not all(("A" <= c <= "Z") or ("a" <= c <= "z") or ("0" <= c <= "9")
                       or c in "_-" for c in guid)
            ):
        raise RuntimeError("GitHub delivery metadata item is malformed")
    # Outcome metadata is not identity authority.  GitHub may add a status enum or temporarily omit a code; one
    # unknown item must be persisted as terminal and must not prevent a later explicit failure being recovered.
    if isinstance(status_code, bool) or not isinstance(status_code, int) or not 0 <= status_code <= 599:
        status_code = -1
    if (not isinstance(status, str) or not 1 <= len(status) <= 80
            or status.strip() != status
            or not status.isascii()
            or any(ord(char) < 0x20 or ord(char) > 0x7e for char in status)):
        status = "Unknown"
    elif status in ("OK", "Timed Out"):
        status = status        # retain the exact official success and timeout enums
    else:
        status = "Other"      # discard every other raw label; status_code carries the decision
    return {
        "id": delivery_id,
        "guid": guid,
        "delivered_at": _aware_time(value.get("delivered_at"), "GitHub delivery timestamp"),
        "status_code": status_code,
        "status": status,
    }


class FailedDeliveryRecoveryStore:
    """Short autocommit DB operations plus one session advisory lock held across a scan/retry tick."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._singleflight_conn = None

    def _one(self, sql: str, args=()):
        import psycopg2
        conn = self._singleflight_conn
        owned = conn is None
        if owned:
            conn = psycopg2.connect(self.dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                if owned:
                    cur.execute("SET search_path=core")
                    cur.execute("SET statement_timeout = %s" % _DB_STATEMENT_TIMEOUT_MS)
                    cur.execute("SET lock_timeout = %s" % _DB_LOCK_TIMEOUT_MS)
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            if owned:
                conn.close()

    @contextmanager
    def singleflight(self):
        """Fleet-wide session lock.  A crash closes the connection and releases authority immediately."""
        import psycopg2
        conn = psycopg2.connect(self.dsn)
        acquired = False
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = %s" % _DB_STATEMENT_TIMEOUT_MS)
                cur.execute("SET lock_timeout = %s" % _DB_LOCK_TIMEOUT_MS)
                cur.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s),hashtext(%s))",
                    ("core.github_delivery_recovery", "scan-and-redeliver"),
                )
                row = cur.fetchone()
                acquired = bool(row and row[0])
            if acquired:
                self._singleflight_conn = conn
            yield acquired
        finally:
            # Session-close is the authoritative unlock, including exception/crash paths.
            self._singleflight_conn = None
            conn.close()

    def begin_scan(self) -> dict:
        return _as_json(self._one("SELECT core.begin_github_delivery_recovery_scan_with_authority()"))

    def observe(self, epoch: int, item: dict) -> bool:
        return bool(self._one(
            "SELECT core.observe_github_delivery_with_authority(%s,%s,%s,%s,%s,%s)",
            (epoch, item["id"], item["guid"], item["delivered_at"],
             item["status_code"], item["status"]),
        ))

    def advance_scan(self, epoch: int, expected_cursor: str | None, cursor: str | None, complete: bool,
                     page_head: dict | None, page_tail: dict | None) -> bool:
        return bool(self._one(
            "SELECT core.advance_github_delivery_recovery_scan_with_authority(%s,%s,%s,%s,%s,%s,%s,%s)",
            (epoch, expected_cursor, cursor, bool(complete),
             page_head["delivered_at"] if page_head else None,
             page_head["id"] if page_head else None,
             page_tail["delivered_at"] if page_tail else None,
             page_tail["id"] if page_tail else None),
        ))

    def reset_scan(self, epoch: int, expected_cursor: str | None) -> bool:
        return bool(self._one(
            "SELECT core.reset_github_delivery_recovery_scan_with_authority(%s,%s)",
            (epoch, expected_cursor)))

    def claim_candidate(self) -> dict:
        return _as_json(self._one("SELECT core.claim_github_delivery_redelivery_with_authority()"))

    def record_attempt(self, guid: str, generation: int, outcome: str,
                       not_before: datetime | None = None) -> bool:
        return bool(self._one(
            "SELECT core.record_github_delivery_redelivery_attempt_with_authority(%s,%s,%s,%s)",
            (guid, generation, outcome, not_before),
        ))

    def prune(self) -> int:
        return int(self._one("SELECT core.prune_github_delivery_recovery_with_authority()") or 0)

    def rearm_exhausted(self, rearm_seconds: int = _REARM_SECONDS, limit: int = _CANDIDATES_PER_TICK) -> dict:
        """RECOVERY-QUEUE RE-ARM: give an EXHAUSTED (attempt_count>=3) but still-redeliverable, still-in-window
        delivery ONE fresh attempt-budget once its last attempt is older than rearm_seconds — the same controlled
        sweep as the DLQ re-arm, so a transient-outage casualty is not stuck exhausted (+ CRITICAL-alerting) for
        the whole ~33-day retention. The SQL's own age/window/class gate is the real guard. Returns
        {rearmed, exhausted_remaining}."""
        return _as_json(self._one(
            "SELECT core.rearm_exhausted_github_delivery_recovery_with_authority(%s,%s)",
            (int(rearm_seconds), int(limit))))

    def depth(self) -> dict:
        return _as_json(self._one("SELECT core.github_delivery_recovery_depth_with_authority()"))


class RecoveryScanInvariant(RuntimeError):
    """A scan-phase invariant violation in recover_failed_deliveries_tick. Carries a content-free `reason` slug
    (a fixed compile-time identifier — NEVER a delivery body/GUID/token/cursor) so the alert can name which
    invariant tripped without leaking. IS-A RuntimeError, so every existing `except RuntimeError`/`except
    Exception` still catches it unchanged."""
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _scan_invariant(store, epoch, cursor, reason: str, message: str) -> RecoveryScanInvariant:
    """Re-baseline the scan (CAS-guarded reset_scan: a genuine no-op if the observed epoch/cursor already moved
    on) BEFORE raising, so a wedged in-progress scan self-heals on the next tick regardless of WHICH invariant
    fired. The scan restarts from newest (page_tail cleared), so no newer failed delivery is skipped, and a
    single persistent invariant can no longer raise on every tick forever (the alert stops flapping)."""
    try:
        store.reset_scan(epoch, cursor)
    except Exception as e:  # best-effort self-heal; never mask the underlying invariant behind a reset error
        print(f"delivery recovery scan re-baseline skipped (reason={reason}): {str(e)[:120]}", flush=True)
    return RecoveryScanInvariant(reason, message)


def recover_failed_deliveries_tick(
    store: Any,
    gh: Any,
    *,
    max_pages: int = _SCAN_PAGES_PER_TICK,
    max_candidates: int = _CANDIDATES_PER_TICK,
) -> dict:
    """Run one bounded scan then a bounded retry batch.  Safe to call concurrently across instances."""
    def bounded(value: Any, cap: int, label: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{label} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{label} must be an integer") from exc
        return min(max(parsed, 0), cap)

    page_budget = bounded(max_pages, _SCAN_PAGES_PER_TICK, "scan page budget")
    candidate_budget = bounded(max_candidates, _CANDIDATES_PER_TICK, "redelivery candidate budget")
    out = {"singleflight": False, "pages": 0, "observed": 0, "scan_complete": False,
           "attempted": 0, "transport_ambiguous": 0, "rate_limited": 0,
           "auth_deferred": 0, "terminal_rejected": 0, "pruned": 0, "depth": {}}
    with store.singleflight() as acquired:
        if not acquired:
            return out
        out["singleflight"] = True
        scan = store.begin_scan()
        epoch = scan.get("epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise RecoveryScanInvariant("no-epoch", "GitHub delivery scan did not return an epoch")
        cursor = scan.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048):
            store.reset_scan(epoch, cursor)
            raise InvalidDeliveryCursor("stored GitHub delivery cursor is malformed")
        high_water = scan.get("high_water_delivery_id")
        if high_water is not None and (isinstance(high_water, bool) or not isinstance(high_water, int)
                                       or high_water <= 0):
            raise _scan_invariant(store, epoch, cursor, "high-water-malformed",
                                  "GitHub delivery scan high-water is malformed")
        previous_tail_id = scan.get("page_tail_delivery_id")
        previous_tail_raw = scan.get("page_tail_delivered_at")
        if (previous_tail_id is None) != (previous_tail_raw is None):
            raise _scan_invariant(store, epoch, cursor, "page-tail-malformed",
                                  "GitHub delivery scan page-tail is malformed")
        previous_tail = None
        if previous_tail_id is not None:
            if (isinstance(previous_tail_id, bool) or not isinstance(previous_tail_id, int)
                    or previous_tail_id <= 0):
                raise _scan_invariant(store, epoch, cursor, "page-tail-malformed",
                                  "GitHub delivery scan page-tail is malformed")
            previous_tail = (_aware_time(previous_tail_raw, "GitHub delivery scan page-tail"), previous_tail_id)
        cutoff = _aware_time(scan.get("cutoff_at"), "GitHub delivery scan cutoff")

        for _ in range(page_budget):
            try:
                page = gh.list_app_hook_deliveries(cursor=cursor, per_page=_SCAN_PAGE_SIZE)
            except InvalidDeliveryCursor:
                store.reset_scan(epoch, cursor)
                raise
            if not isinstance(page, dict) or not isinstance(page.get("deliveries"), list):
                raise _scan_invariant(store, epoch, cursor, "list-page-malformed",
                                      "GitHub delivery list page is malformed")
            raw_items = page["deliveries"]
            if len(raw_items) > _SCAN_PAGE_SIZE:
                raise _scan_invariant(store, epoch, cursor, "list-page-oversized",
                                      "GitHub delivery list page exceeds its bound")
            next_cursor = page.get("next_cursor")
            if next_cursor is not None and (not isinstance(next_cursor, str)
                                            or not 1 <= len(next_cursor) <= 2048):
                store.reset_scan(epoch, cursor)
                raise InvalidDeliveryCursor("GitHub delivery next cursor is malformed")
            if next_cursor is not None and next_cursor == cursor:
                store.reset_scan(epoch, cursor)
                raise InvalidDeliveryCursor("GitHub delivery cursor did not advance")

            items = [_delivery_metadata(raw) for raw in raw_items]
            if not items and next_cursor is not None:
                store.reset_scan(epoch, cursor)
                raise InvalidDeliveryCursor("GitHub delivery pagination returned an empty non-terminal page")
            for newer, older in zip(items, items[1:]):
                if (older["delivered_at"], older["id"]) > (newer["delivered_at"], newer["id"]):
                    # HWM/cutoff early-stop is sound only under GitHub's documented newest→oldest order.  Re-baseline
                    # (clears cursor+tail) rather than resuming a wedged cursor: begin_scan then restarts from newest
                    # so no newer work is skipped, and a persistently violated page cannot raise on every tick forever.
                    raise _scan_invariant(store, epoch, cursor, "page-order",
                                          "GitHub delivery page order is malformed")
            if items and previous_tail is not None:
                current_head = (items[0]["delivered_at"], items[0]["id"])
                if current_head > previous_tail:
                    # A resumed cursor whose page head is newer than the committed tail can never satisfy the SQL
                    # order proof, so an interrupted in-progress scan would raise here on every tick forever.  Re-
                    # baseline (cursor+tail cleared); the next scan restarts from newest and re-covers everything.
                    raise _scan_invariant(store, epoch, cursor, "cross-page-order",
                                          "GitHub delivery cross-page order is malformed")

            reached_boundary = False
            for item in items:
                # The three-day cutoff is a hard boundary. The HWM item itself is different: GitHub may update
                # the outcome of the SAME delivery id in place, so observe that item once before stopping. If we
                # broke before observe, a failure that later became OK at the HWM would remain failed forever.
                if item["delivered_at"] < cutoff:
                    reached_boundary = True
                    break
                if not store.observe(epoch, item):
                    raise _scan_invariant(store, epoch, cursor, "observe-lost-authority",
                                          "GitHub delivery observation lost scan authority")
                out["observed"] += 1
                if high_water is not None and item["id"] == high_water:
                    reached_boundary = True
                    break

            out["pages"] += 1
            complete = reached_boundary or next_cursor is None
            page_head = items[0] if items else None
            page_tail = items[-1] if items else None
            if not store.advance_scan(
                    epoch, cursor, None if complete else next_cursor, complete, page_head, page_tail):
                raise _scan_invariant(store, epoch, cursor, "advance-lost-authority",
                                      "GitHub delivery scan lost cursor/epoch/order authority")
            if complete:
                out["scan_complete"] = True
                break
            previous_tail = (page_tail["delivered_at"], page_tail["id"])
            cursor = next_cursor

        # Never redeliver from a partial scan: a later page may contain a newer success for a repeated GUID.
        if out["scan_complete"]:
            for _ in range(candidate_budget):
                candidate = store.claim_candidate()  # committed authority and attempt budget BEFORE the POST
                if not candidate:
                    break
                delivery_id = candidate.get("delivery_id")
                guid = candidate.get("delivery_guid")
                generation = candidate.get("generation")
                if (isinstance(delivery_id, bool) or not isinstance(delivery_id, int) or delivery_id <= 0
                        or not isinstance(guid, str) or not guid
                        or isinstance(generation, bool) or not isinstance(generation, int)
                        or generation < 1):
                    raise RuntimeError("GitHub delivery candidate is malformed")
                out["attempted"] += 1
                not_before = None
                try:
                    # Exactly one POST.  The client method has no internal HTTP retry because an I/O failure is
                    # ACK-ambiguous and another POST here could create duplicate deliveries.
                    response_status = gh.redeliver_app_hook_delivery_once(delivery_id)
                    if response_status != 202:
                        raise RuntimeError("GitHub redelivery returned an unexpected success status")
                    outcome = "accepted_ambiguous"
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    if code in (403, 429):
                        outcome = "rate_limited"
                        not_before = _http_cooldown(exc, default_seconds=900)
                        out["rate_limited"] += 1
                    elif code == 401:
                        outcome = "auth_deferred"
                        not_before = _http_cooldown(exc, default_seconds=1800)
                        out["auth_deferred"] += 1
                    elif isinstance(code, int) and 400 <= code <= 499:
                        # A definite client rejection (including 400/422) cannot improve by replaying the same id.
                        outcome = "terminal_rejected"
                        out["terminal_rejected"] += 1
                    else:
                        outcome = "transport_ambiguous"
                        out["transport_ambiguous"] += 1
                if not store.record_attempt(guid, generation, outcome, not_before):
                    raise RuntimeError("GitHub delivery attempt lost generation authority")
                if outcome in ("rate_limited", "auth_deferred"):
                    break  # global App cooldown is now durable; do not spend another request in this tick

        out["pruned"] = store.prune()
        out["depth"] = store.depth()
        return out


def _http_cooldown(exc: Exception, default_seconds: int) -> datetime:
    """Bound Retry-After/rate-reset to a durable 1-minute..6-hour global pause without reading an error body."""
    import email.utils
    import math
    import time
    now = datetime.now(timezone.utc)
    seconds = float(default_seconds)
    headers = getattr(exc, "headers", None) or {}
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            seconds = float(retry_after)
        except (TypeError, ValueError):
            try:
                parsed = email.utils.parsedate_to_datetime(str(retry_after))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                seconds = (parsed.astimezone(timezone.utc) - now).total_seconds()
            except (TypeError, ValueError, OverflowError):
                pass
    elif headers.get("X-RateLimit-Reset") is not None:
        try:
            seconds = float(headers["X-RateLimit-Reset"]) - time.time()
        except (TypeError, ValueError, OverflowError):
            pass
    if not math.isfinite(seconds):
        seconds = float(default_seconds)
    # SQL independently rejects an unbounded cooldown.  Leave a small clock-skew margin below its six-hour cap.
    return now + timedelta(seconds=min(max(seconds, 60.0), 5.5 * 3600.0))


def _evaluate_alerts(sink: Any, result: dict) -> None:
    transport = int(result.get("transport_ambiguous") or 0)
    if transport:
        sink.fire(
            "github_delivery_redelivery_transport", "warning",
            "GitHub webhook redelivery POST was ACK-ambiguous; Core entered cooldown",
            {"ambiguous_attempts": transport},
        )
    else:
        sink.resolve("github_delivery_redelivery_transport")

    limited = int(result.get("rate_limited") or 0)
    auth = int(result.get("auth_deferred") or 0)
    depth = result.get("depth") if isinstance(result.get("depth"), dict) else {}
    if bool(depth.get("cooldown_active")):
        sink.fire(
            "github_delivery_redelivery_global_cooldown", "warning",
            "GitHub rejected webhook recovery requests; Core entered a durable global cooldown",
            {"rate_limited": limited, "auth_deferred": auth},
        )
    else:
        sink.resolve("github_delivery_redelivery_global_cooldown")

    terminal = int(depth.get("terminal_unrecovered") or 0)
    if terminal:
        sink.fire(
            "github_delivery_redelivery_terminal", "warning",
            "One or more GitHub webhook failures cannot be automatically redelivered from bounded metadata",
            {"terminal_unrecovered": terminal},
            escalate_value=terminal,
        )
    else:
        sink.resolve("github_delivery_redelivery_terminal")

    exhausted = int(depth.get("exhausted") or 0)
    expiring = int(depth.get("expiring") or 0)
    expired = int(depth.get("expired_unrecovered") or 0)
    if exhausted:
        sink.fire(
            "github_delivery_redelivery_exhausted", "critical",
            "GitHub webhook failures exhausted Core's bounded retry budget",
            {"exhausted": exhausted},
            escalate_value=exhausted,
        )
    else:
        sink.resolve("github_delivery_redelivery_exhausted")
    if expiring:
        sink.fire(
            "github_delivery_redelivery_expiring", "warning",
            "GitHub webhook failures are nearing the three-day recovery limit",
            {"expiring": expiring},
            escalate_value=expiring,
        )
    else:
        sink.resolve("github_delivery_redelivery_expiring")
    if expired:
        sink.fire(
            "github_delivery_redelivery_expired", "critical",
            "GitHub webhook failures passed the three-day recovery limit without proof of delivery",
            {"expired_unrecovered": expired},
            escalate_value=expired,
        )
    else:
        sink.resolve("github_delivery_redelivery_expired")

    lag = int(depth.get("scan_lag_seconds") or 0)
    if lag >= 48 * 3600:
        sink.fire(
            "github_delivery_recovery_scan_lag", "critical",
            "GitHub failed-delivery metadata scan is critically stale",
            {"scan_lag_seconds": lag},
        )
    elif lag >= 6 * 3600:
        sink.fire(
            "github_delivery_recovery_scan_lag", "warning",
            "GitHub failed-delivery metadata scan is stale",
            {"scan_lag_seconds": lag},
        )
    else:
        sink.resolve("github_delivery_recovery_scan_lag")


def start_failed_delivery_recovery(
    store: Any,
    gh: Any,
    sink: Any,
    *,
    interval_seconds: float = _INTERVAL_SECONDS,
    start_delay_seconds: float = _START_DELAY_SECONDS,
) -> threading.Thread:
    """Start the fixed-default, low-priority recovery loop.  Production exposes no env kill switch/knob."""
    interval = max(1.0, float(interval_seconds))
    delay = max(0.0, float(start_delay_seconds))
    # RECOVERY-QUEUE RE-ARM cadence (mirrors the DLQ re-arm in delivery_queue.start_recovery_loop): issue the cheap
    # re-arm sweep about once per _REARM_SECONDS worth of ticks, not every tick — an exhausted delivery gets at most
    # one fresh attempt-budget per window, with the CRITICAL exhausted alert as the human signal in between. `% == 0`
    # is true on the FIRST tick, so a boot right after the transient cause clears re-arms the stranded rows promptly.
    _rearm_every = max(1, round(_REARM_SECONDS / interval)) if interval > 0 else 0

    def _loop():
        if delay:
            threading.Event().wait(delay)
        _tick = 0
        while True:
            try:
                # RECOVERY-QUEUE RE-ARM (slow cadence, before the tick so the freshly re-armed rows are offered by
                # this same tick's claim): an EXHAUSTED still-redeliverable in-window delivery past the re-arm age is
                # given one fresh attempt-budget. Best-effort + bounded (its own age/window/class gate + LIMIT); a
                # re-arm error is logged, never stalls live recovery.
                if _rearm_every and (_tick % _rearm_every == 0):
                    try:
                        res = store.rearm_exhausted()
                        if res.get("rearmed"):
                            print(f"delivery recovery re-arm: {res.get('rearmed')} exhausted redeliverable row(s) "
                                  f"given a fresh attempt-budget ({res.get('exhausted_remaining')} still exhausted)",
                                  flush=True)
                    except Exception as e:
                        print(f"delivery recovery re-arm skipped: {str(e)[:160]}", flush=True)
                result = recover_failed_deliveries_tick(store, gh)
                if result.get("singleflight"):
                    _evaluate_alerts(sink, result)
                    sink.resolve("github_delivery_recovery_unavailable")
            except Exception as exc:
                # Content-free by construction: class name + (for our own scan invariants) a fixed reason SLUG.
                # The slug is a compile-time identifier (e.g. "advance-lost-authority"), never exception text,
                # cursor, GUID, token, or body — so an operator can see WHICH invariant wedges the scan without
                # ever leaking delivery content. Arbitrary (non-RecoveryScanInvariant) exceptions stay class-only.
                fields = {"error_type": type(exc).__name__}
                # Gate on OUR type, not duck-typed getattr(exc,"reason"): stdlib errors such as
                # urllib.error.URLError also carry a `.reason` (host/socket text), which is NOT a
                # fixed slug. Only RecoveryScanInvariant guarantees a compile-time identifier.
                if isinstance(exc, RecoveryScanInvariant) and isinstance(exc.reason, str) and exc.reason:
                    fields["reason"] = exc.reason
                sink.fire(
                    "github_delivery_recovery_unavailable", "warning",
                    "Core could not complete the GitHub failed-delivery recovery tick",
                    fields,
                )
            _tick += 1
            threading.Event().wait(interval)

    thread = threading.Thread(target=_loop, name="veripsa-failed-delivery-recovery", daemon=True)
    thread.start()
    return thread
