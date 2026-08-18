"""Attempt-neutral durable-delivery scheduling signal shared by runtime stages."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import email.utils
import math


GITHUB_RATE_LIMIT_FALLBACK_SECONDS = 60.0
GITHUB_RATE_LIMIT_MAX_SECONDS = 3600.0
GITHUB_RATE_LIMIT_REASON = "github_api_rate_limit"


class IntentionalDeliveryDeferral(RuntimeError):
    """A verified transient consistency/resource wait, not a failed handler attempt."""

    def __init__(self, reason: str, not_before: datetime):
        if (not isinstance(not_before, datetime) or not_before.tzinfo is None
                or not_before.utcoffset() is None):
            raise ValueError("intentional delivery deferral needs a timezone-aware not_before")
        self.reason = str(reason or "intentional consistency deferral")[:300]
        self.not_before = not_before.astimezone(timezone.utc)
        super().__init__(self.reason)


def _header(headers, name: str):
    """Case-insensitive header lookup for HTTPMessage and minimal dict fakes."""
    try:
        value = headers.get(name)
    except Exception:
        value = None
    if value is not None:
        return value
    try:
        wanted = name.lower()
        for key, candidate in headers.items():
            if str(key).lower() == wanted:
                return candidate
    except Exception:
        pass
    return None


def _retry_after_seconds(value, now: datetime) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = email.utils.parsedate_to_datetime(text)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = (parsed.astimezone(timezone.utc) - now).total_seconds()
            # A valid HTTP-date already in the past means retry now.
            return max(0.0, seconds) if math.isfinite(seconds) else None
        except (TypeError, ValueError, OverflowError):
            return None
    # Negative delta-seconds are malformed. Zero is deliberately valid and
    # means the caller may perform one immediate bounded retry.
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _reset_seconds(value, now: datetime) -> float | None:
    if value is None:
        return None
    try:
        reset_epoch = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    # The header is an epoch timestamp. A finite positive timestamp in the
    # past is a valid "retry now"; a negative epoch is malformed and must use
    # the safe fallback instead of being mistaken for an explicit zero wait.
    if not math.isfinite(reset_epoch) or reset_epoch < 0:
        return None
    return max(0.0, reset_epoch - now.timestamp())


def github_rate_limit_deferral(
    error,
    *,
    now: datetime | None = None,
    fallback_seconds: float = GITHUB_RATE_LIMIT_FALLBACK_SECONDS,
    max_seconds: float = GITHUB_RATE_LIMIT_MAX_SECONDS,
) -> IntentionalDeliveryDeferral | None:
    """Convert one GitHub rate-limit response into a bounded durable schedule.

    This helper does not inspect response bodies and never includes header
    values, URLs, repository names, or credentials in its reason. It returns
    ``None`` for a non-rate-limit response and for a valid zero-second wait.
    The caller can therefore keep its existing immediate retry semantics for
    zero while moving positive waits out of the EventQueue worker.
    """
    try:
        code = int(getattr(error, "code", 0))
    except (TypeError, ValueError):
        return None
    if code not in (403, 429):
        return None

    headers = getattr(error, "headers", None) or {}
    retry_after = _header(headers, "Retry-After")
    remaining = _header(headers, "X-RateLimit-Remaining")
    reset = _header(headers, "X-RateLimit-Reset")
    primary_exhausted = str(remaining).strip() == "0"
    # A normal permission 403 commonly carries ordinary rate-limit metadata.
    # It is a rate limit only with Retry-After or explicit remaining=0. A 429
    # is intrinsically rate-limited even when its headers are malformed.
    if code == 403 and retry_after is None and not primary_exhausted:
        return None

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("rate-limit deferral clock must be timezone-aware")
    current = current.astimezone(timezone.utc)

    seconds = _retry_after_seconds(retry_after, current)
    if seconds is None and (primary_exhausted or code == 429):
        seconds = _reset_seconds(reset, current)
    if seconds is None:
        try:
            seconds = float(fallback_seconds)
        except (TypeError, ValueError, OverflowError):
            seconds = GITHUB_RATE_LIMIT_FALLBACK_SECONDS
    if not math.isfinite(seconds) or seconds < 0:
        seconds = GITHUB_RATE_LIMIT_FALLBACK_SECONDS
    try:
        ceiling = float(max_seconds)
    except (TypeError, ValueError, OverflowError):
        ceiling = GITHUB_RATE_LIMIT_MAX_SECONDS
    if not math.isfinite(ceiling) or ceiling < 0:
        ceiling = GITHUB_RATE_LIMIT_MAX_SECONDS
    seconds = min(max(0.0, seconds), ceiling)
    if seconds == 0:
        return None
    return IntentionalDeliveryDeferral(
        GITHUB_RATE_LIMIT_REASON,
        current + timedelta(seconds=seconds),
    )
