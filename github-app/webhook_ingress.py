#!/usr/bin/env python3
"""Veripsa GitHub App — the WEBHOOK REQUEST-INGRESS layer (extracted from server.py).

The first, security-critical line of the live HTTP path: turn an UNAUTHENTICATED, attacker-controllable inbound
webhook request into a SAFE, verified body the rest of server.py can trust — before any parse, enqueue, or work.
Two concerns, one cohesive cluster:

  • HMAC SIGNATURE VERIFY (verify_signature): GitHub signs every delivery (X-Hub-Signature-256); reject forgeries.
  • BOUNDED BODY READ (_MAX_BODY_BYTES / _checked_content_length / read_bounded_body): the endpoint is PUBLIC once
    deployed and we must buffer the FULL body before we can verify its HMAC — so cap the bytes a caller can make us
    read, header-first (413 a lying-large Content-Length WITHOUT touching the socket), then read EXACTLY the
    declared length (never `read(cap+1)` on a live socket — that hangs every POST). Content-free: never look at or
    log the body's contents.

This file changes for a DIFFERENT reason than the rest of server.py: it changes when the request-ingress / DoS /
signature-verification rules change — not when the daemon worker, watchdog, serve() wiring, or boot/backfill CLI
change. Splitting it out cuts server.py's out-degree (the #1 Veripsa-flagged hotspot).

It imports NOTHING from server.py at LOAD time (no circular import — server.py imports THIS and RE-EXPORTS every
name below for backward compatibility, so `server.verify_signature` / `server.read_bounded_body` /
`server._checked_content_length` / `server._MAX_BODY_BYTES` keep resolving for serve()'s handler, the gates, and
the tests that do `S.verify_signature` / `S.read_bounded_body` / `S._checked_content_length`). Its only dependency
is env_int (the validated env-knob reader), imported DIRECTLY from its leaf module env_config.py — which imports
nothing from server.py, so there is no cycle — using the same standalone/package dual-import idiom server.py uses.
"""
from __future__ import annotations

import hashlib
import hmac

# env_int: the VALIDATED env-knob reader (fail SAFE on a typo'd cap — a non-int or out-of-range value raises a
# loud, actionable refusal that names the var + value + range, instead of crashing with a bare ValueError or
# silently misbehaving on a 0/negative cap). Imported directly from its leaf module (no cycle).
try:
    from env_config import env_int
except ImportError:  # imported as a package
    from .env_config import env_int


def verify_signature(secret: str, body: bytes, sig_header: str | None) -> bool:
    """GitHub signs each delivery (X-Hub-Signature-256). Reject forgeries. Empty secret = unsigned dev mode.

    COMPARE ON BYTES, NOT str (audit:security-auth 2026-06-20 — a fail-CLOSED hardening). The sig_header is
    FULLY attacker-controlled on the PUBLIC endpoint. `hmac.compare_digest(str, str)` RAISES
    `TypeError: comparing strings with non-ASCII characters is not supported` the instant the header carries any
    non-ASCII char (e.g. a forged `sha256=<64 non-ASCII chars>` with the right prefix+length). A security-critical
    compare an UNAUTHENTICATED caller can crash is a defect: the TypeError unwinds out of verify_signature (and
    do_POST), so the request never reaches the clean 401 default-deny — it dumps a traceback and drops the
    connection, a 500-class outcome an attacker can spray. Encoding both sides to bytes makes a non-ASCII / odd
    header a plain digest MISMATCH → False, in constant time, with no raise. (We pre-screen the `sha256=` prefix on
    the str — a cheap structural check whose timing carries no secret — then compare the hex digests as bytes.)
    """
    if not secret:
        return True
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = b"sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest().encode("ascii")
    # encode the attacker header to bytes; a non-ASCII / non-UTF-8-safe header → a length/byte mismatch (False),
    # never a TypeError. errors="ignore" can only DROP bytes (shortening it) → still a guaranteed mismatch.
    return hmac.compare_digest(expected, sig_header.encode("utf-8", "ignore"))


# DoS GUARD: the webhook endpoint is PUBLIC once deployed, and we must read the FULL body before we can verify
# its HMAC — so an unauthenticated caller could make us buffer arbitrary bytes BEFORE the signature check can
# reject the forgery. A webhook payload is SMALL (GitHub's pull_request / push deliveries are well under a few
# MiB), so we cap the body at a sane size. Anything bigger is rejected (413) and is NEVER fully buffered or
# parsed. Configurable, with a hard upper sanity bound so the knob can't be widened into a DoS vector itself.
_MAX_BODY_BYTES = env_int("VERIPSA_MAX_BODY_BYTES", 2 * 1024 * 1024, min_value=1, max_value=32 * 1024 * 1024)


def _checked_content_length(header_value, cap: int = _MAX_BODY_BYTES):
    """Parse + bound a Content-Length header. Returns the int length if 0 <= len <= cap, else None (the caller
    then 413s WITHOUT reading the body). First, cheap, header-only line of defense: reject an obviously huge or
    forged declared length BEFORE touching the socket."""
    try:
        n = int(header_value if header_value is not None else 0)
    except (TypeError, ValueError):
        return None
    if n < 0 or n > cap:
        return None
    return n


def read_bounded_body(header_value, rfile, cap: int = _MAX_BODY_BYTES):
    """Read the webhook request body with a HARD bound on the ACTUAL bytes consumed. Returns (body, status):
      - (body_bytes, None)  → within cap, hand it to the HMAC check
      - (None, 413)         → declared length over cap — rejected WITHOUT touching the socket
      - (None, 400)         → the stream delivered fewer bytes than declared (truncated/short body)

    READ EXACTLY THE DECLARED LENGTH — never more. The declared length is already bounded to [0, cap] by
    _checked_content_length (a lying-LARGE Content-Length is rejected here, header-only, before any read), so
    reading exactly `length` bytes can never buffer more than `cap` — the memory-DoS bound holds. A lying-SMALL
    length is harmless: we read only those `length` bytes (the attacker's extra megabytes are left UNREAD in the
    socket and discarded when the connection closes — never buffered or parsed), and the HMAC check then rejects
    the short body. Content-free: we never look at or log the body's contents.

    WHY NOT `rfile.read(cap + 1)` (the previous probe): `rfile` is a LIVE socket. An HTTP client (GitHub, curl,
    every real client) sends the body and then HOLDS the connection open waiting for the response — it does NOT
    send EOF. `read(cap+1)` therefore BLOCKS waiting for cap+1 (= 2 MiB) bytes that never arrive, hanging EVERY
    webhook POST until the socket times out → the delivery fails AND (single-threaded server) /healthz is starved
    → restart. `read(length)` returns the instant the declared bytes are in. (The old probe only ever 'worked'
    against an io.BytesIO in tests, which signals EOF immediately; a real socket does not.) A slow/incomplete
    body can still stall this read — bounded by the per-request socket timeout on the handler (Handler.timeout)."""
    length = _checked_content_length(header_value, cap)
    if length is None:
        return None, 413
    body = rfile.read(length)                 # exactly `length` bytes (already ≤ cap) — returns as soon as the body is in
    if len(body) < length:                    # socket delivered fewer bytes than declared → truncated/short body
        return None, 400
    return body, None
