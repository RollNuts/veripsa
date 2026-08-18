#!/usr/bin/env python3
"""WEBHOOK NO-HANG gate — the webhook receiver must NEVER hang reading a request body off a LIVE socket.

This is the regression guard for the production launch-blocker where EVERY webhook POST timed out: the body
reader did `rfile.read(cap + 1)` (cap = 2 MiB), which on a real keep-alive socket BLOCKS waiting for 2 MiB of
bytes that an HTTP client never sends (the client holds the connection open for the response — no EOF). Result:
every GitHub delivery failed AND the single-threaded server's /healthz was starved → restart loop. The unit
tests missed it because they fed io.BytesIO, which signals EOF immediately, so read(cap+1) returned at once — a
real socket does not. So this gate uses a REAL socketpair (no EOF until close), the only thing that reproduces it.

Proves on the REAL S.read_bounded_body:
  (1) NO HANG: body sent, the write end left OPEN (keep-alive, no EOF) → read_bounded_body RETURNS PROMPTLY with
      exactly the declared body (the old read(cap+1) would block here);
  (2) truncation is still caught: fewer bytes than declared, then EOF → 400;
  (3) an over-cap declared Content-Length is still 413'd header-only, WITHOUT touching the socket (returns even
      though zero bytes were sent);
  (4) STRUCTURAL: serve() uses a THREADING server (one slow request can't starve /healthz) and sets a per-request
      Handler.timeout (a slowloris read is bounded) — the two resilience guards that keep one bad client from
      taking the receiver down.

Run:  python3 tests/test_webhook_no_hang.py
"""
from __future__ import annotations
import os
import socket
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import server as S  # noqa: E402

FAIL = 0


def chk(c, label):
    global FAIL
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    if not c:
        FAIL = 1


def _read_in_thread(header, rfile, cap, timeout=3.0):
    """Run read_bounded_body on a background thread; return (returned?, result). returned=False means it HUNG."""
    out = {}
    t = threading.Thread(target=lambda: out.update(r=S.read_bounded_body(header, rfile, cap)), daemon=True)
    t.start()
    t.join(timeout)
    return (not t.is_alive()), out.get("r")


def main() -> int:
    cap = S._MAX_BODY_BYTES  # the real 2 MiB cap — the value the old read(cap+1) waited for

    # (1) NO HANG on a live keep-alive socket: send a small body, keep the write end OPEN (no EOF).
    a, b = socket.socketpair()
    body = b'{"action":"opened","number":7}'
    a.sendall(body)                                   # write end stays open — exactly like an HTTP client awaiting a response
    returned, res = _read_in_thread(str(len(body)), b.makefile("rb"), cap)
    chk(returned, "read_bounded_body RETURNS on a live keep-alive socket (does NOT hang waiting for cap+1 bytes)")
    chk(returned and res == (body, None), f"...and returns exactly the declared body, status ok (got {res if returned else 'HANG'})")
    a.close(); b.close()

    # (2) truncation still caught: declare more than is sent, then CLOSE (EOF) → short body → 400.
    a2, b2 = socket.socketpair()
    a2.sendall(b"only-12-byte"); a2.close()           # 12 bytes then EOF, but we declare 100
    returned2, res2 = _read_in_thread("100", b2.makefile("rb"), cap)
    chk(returned2 and res2 == (None, 400), f"a truncated body (fewer bytes than declared, then EOF) → 400 (got {res2 if returned2 else 'HANG'})")
    b2.close()

    # (3) over-cap declared length: 413 header-only, WITHOUT reading the socket (returns despite zero bytes sent).
    a3, b3 = socket.socketpair()                       # nothing sent on a3 at all
    returned3, res3 = _read_in_thread(str(cap + 1), b3.makefile("rb"), cap)
    chk(returned3 and res3 == (None, 413),
        f"an over-cap declared Content-Length is 413'd WITHOUT touching the socket (got {res3 if returned3 else 'HANG'})")
    a3.close(); b3.close()

    # (4) STRUCTURAL resilience guards in serve(): threading server + a per-request timeout.
    src = open(os.path.join(ROOT, "github-app", "server.py")).read()
    chk('ThreadingHTTPServer((' in src,
        "serve() binds a ThreadingHTTPServer (one slow request can't starve /healthz → no restart loop)")
    chk("timeout = _req_timeout" in src,
        "the request Handler sets a per-request socket timeout (a slowloris read is bounded)")

    print("WEBHOOK NO-HANG GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
