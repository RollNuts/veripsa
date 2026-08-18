#!/usr/bin/env python3
"""Veripsa — VALIDATED env config readers (the fail-safe-on-misconfig primitive).

WHY THIS EXISTS: the cost/scale knobs (VERIPSA_MAX_INGEST_FILES, VERIPSA_MAX_PR_FILES, VERIPSA_ONBOARD_REPO_CAP,
VERIPSA_PER_ACCOUNT_QUEUE_CAP, VERIPSA_MAX_TARBALL_BYTES, …) used to each be a BARE `int(os.environ.get(KEY, D))`.
That has two silent-failure modes an operator can trip with a typo:

  1. A NON-INT value ("100files", "1e6", "") raises a raw ValueError deep in module import — the process dies
     with an unhelpful traceback that doesn't even name the offending env var (and kills the cron / CLI paths
     too, not just `serve`).
  2. A `0` or NEGATIVE value passes `int()` CLEANLY and then SILENTLY MISBEHAVES. The worst case is
     VERIPSA_PER_ACCOUNT_QUEUE_CAP=0 → the per-account bucket is "full" at length 0 → EVERY webhook 503s →
     the App processes NOTHING, while /healthz stays green. The box looks alive and is silently broken.

This module is the ONE validated reader so a bad knob fails SAFE: a non-int or out-of-range value raises a
LOUD, ACTIONABLE error that NAMES the variable, the bad value (config knobs are non-secret), and the allowed
range — at the first read (module import = process start), so a misconfigured deploy refuses to start rather
than running silently degraded. This mirrors the DB side, where core._policy_int already parses → CLAMPS →
falls back for every owner-tunable knob. Same discipline, now on the Python env knobs.

Kept dependency-light (stdlib only) so the cron + CLI entry points can import it as cheaply as the server.
"""
from __future__ import annotations

import os


class ConfigError(SystemExit):
    """A misconfigured env knob. A SystemExit subclass so an unguarded read at import aborts the process with a
    clean, non-zero exit + a one-line message (no scary traceback), exactly like the empty-secret startup guard.
    Naming the var + value + range is SAFE: these are operational scale/cost knobs, never secrets."""


def env_int(key: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    """Read an integer env knob, FAILING SAFE on misconfiguration.

    Unset → `default` (the default itself is trusted; it is the shipped, documented value).
    Set → parsed; a non-int, or a value outside [min_value, max_value], raises ConfigError (a loud, actionable
    refusal that names the var + value + allowed range). Never returns a silently-broken value.

    Most caps want `min_value=1` (a cap of 0 or negative is never a meaningful "limit" — it disables the path).
    Pass an explicit range so the refusal message can state it."""
    raw = os.environ.get(key)
    if raw is None:
        return default
    raw = raw.strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(
            f"FATAL: {key}={raw!r} is not an integer — refusing to start with a misconfigured knob. "
            f"Unset it to use the default ({default}), or set a whole number"
            + _range_hint(min_value, max_value) + "."
        )
    if (min_value is not None and value < min_value) or (max_value is not None and value > max_value):
        raise ConfigError(
            f"FATAL: {key}={value} is out of range — refusing to start with a misconfigured knob. "
            f"Set a value{_range_hint(min_value, max_value)} (default {default})."
        )
    return value


def _range_hint(min_value: int | None, max_value: int | None) -> str:
    if min_value is not None and max_value is not None:
        return f" in [{min_value}, {max_value}]"
    if min_value is not None:
        return f" >= {min_value}"
    if max_value is not None:
        return f" <= {max_value}"
    return ""
