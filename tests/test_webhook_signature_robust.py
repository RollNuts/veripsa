#!/usr/bin/env python3
"""WEBHOOK SIGNATURE-VERIFY ROBUSTNESS gate — a forged/malformed X-Hub-Signature-256 header must be REJECTED
deterministically (return False), in constant time, NEVER raise.

The defect this locks (audit:security-auth 2026-06-20): verify_signature ended in
    hmac.compare_digest(expected, sig_header)
with BOTH operands as str. CPython's compare_digest, given two str, requires them to be ASCII-only and RAISES
`TypeError: comparing strings with non-ASCII characters is not supported` the moment the second operand carries
ANY non-ASCII character. The signature header is FULLY attacker-controlled on the PUBLIC webhook endpoint. So a
forged request with `X-Hub-Signature-256: sha256=<64 non-ASCII chars>` (right prefix, right length, but non-ASCII
bytes) makes verify_signature THROW instead of returning False — the request never reaches the clean 401
default-deny path; the TypeError unwinds out of do_POST, the handler logs a traceback and abruptly drops the
connection (a 500-class outcome an attacker can spray at will to flood logs / probe the verify path). A
security-critical compare that an UNAUTHENTICATED caller can crash is a verify-path defect: the control MUST
fail CLOSED (reject) on every malformed header, never raise.

The fix: compare on BYTES (encode both sides), so a non-ASCII / odd header is a clean digest mismatch → False,
in constant time, never a TypeError. Valid-signature behaviour is unchanged.

Proves on the REAL verify_signature (re-exported as server.verify_signature):
  (1) a VALID signature still verifies True (behaviour on valid auth unchanged);
  (2) a wrong-bytes / wrong-prefix / missing / empty header still returns False (unchanged);
  (3) a header with NON-ASCII bytes (right prefix+length, or a single trailing non-ASCII char, or emoji/unicode
      digits) returns False — NEVER raises (the defect);
  (4) the empty-secret dev mode still accepts (unchanged);
  (5) no secret material appears in any value this path could surface.

Run:  python3 tests/test_webhook_signature_robust.py
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import server as S  # noqa: E402  (re-exports verify_signature from webhook_ingress)

FAIL = 0


def chk(cond, label):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main() -> int:
    secret = "whsec_THE_REAL_WEBHOOK_SECRET"   # if this ever surfaced in an error, that is a leak (asserted below)
    body = b'{"action":"opened","number":7}'
    good = _sig(secret, body)

    # (1) a VALID signature still verifies — behaviour on valid auth UNCHANGED.
    chk(S.verify_signature(secret, body, good) is True,
        "a valid X-Hub-Signature-256 still verifies True (valid-auth behaviour unchanged)")

    # (2) the existing default-deny cases are UNCHANGED (no header / empty / wrong prefix / wrong bytes).
    chk(S.verify_signature(secret, body, None) is False, "missing header → False (unchanged)")
    chk(S.verify_signature(secret, body, "") is False, "empty header → False (unchanged)")
    chk(S.verify_signature(secret, body, "sha1=" + "0" * 40) is False, "wrong algo prefix → False (unchanged)")
    chk(S.verify_signature(secret, body, "sha256=" + "f" * 64) is False,
        "right length, wrong bytes → False (unchanged)")
    chk(S.verify_signature(secret, body, good.upper()) is False,
        "uppercase hex digest → False (GitHub sends lowercase; unchanged)")

    # (3) THE DEFECT: non-ASCII header bytes must REJECT cleanly (return False), NEVER raise TypeError.
    #     These are all attacker-controllable: the signature header is read straight off the public request.
    non_ascii_cases = [
        ("sha256= + 64 non-ASCII (é) — right prefix+length", "sha256=" + "é" * 64),
        ("a single trailing non-ASCII byte on an otherwise-valid sig", good[:-1] + "é"),
        ("emoji-filled header", "sha256=" + "\U0001f600" * 16),
        ("full-width unicode digits", "sha256=" + "１" * 64),
        ("non-ASCII INSIDE the prefix region", "shá256=" + "0" * 64),
    ]
    for label, header in non_ascii_cases:
        raised = None
        result = None
        try:
            result = S.verify_signature(secret, body, header)
        except BaseException as e:   # any raise here is the defect — a forged request crashing the verify path
            raised = e
        chk(raised is None and result is False,
            f"non-ASCII header rejects cleanly (False, no raise): {label} "
            f"[raised={type(raised).__name__ if raised else None}, result={result}]")

    # (4) empty-secret dev mode still accepts everything (unchanged — guarded behind VERIPSA_ALLOW_UNSIGNED at boot).
    chk(S.verify_signature("", body, None) is True, "empty secret = unsigned dev mode accepts (unchanged)")

    # (5) the rejection path must not surface the secret. Drive the non-ASCII reject and assert the secret value
    #     is not in the boolean/exception (there is no exception now, but lock it: never echo the secret).
    leaked = False
    try:
        r = S.verify_signature(secret, body, "sha256=" + "é" * 64)
        leaked = (secret in str(r))
    except BaseException as e:
        leaked = (secret in str(e) or "WEBHOOK_SECRET" in str(e))
    chk(not leaked, "the webhook secret never appears in the reject path's value/exception")

    print("WEBHOOK SIGNATURE ROBUSTNESS GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
