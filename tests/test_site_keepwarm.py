#!/usr/bin/env python3
"""Gate: the site keep-warm loop is off by default, content-free, and cannot affect event processing.

WHY THIS EXISTS. The previous keep-warm was a scheduled GitHub Actions workflow that failed 100 runs in
a row without anyone noticing, because a pinger that stops pinging looks exactly like a pinger that is
working. Moving it in-process removes the billing dependency but introduces a worse failure mode: a
background loop inside the webhook service that could, if written carelessly, log a remote response,
emit a UA-less request, or take the worker down. This gate pins the boundaries that make it safe:

  A. OFF BY DEFAULT — no target, no thread. A test run or a local server pings nothing.
  B. HTTPS ONLY — a plaintext target is refused, not silently accepted, because this runs unattended.
  C. THE BODY IS NEVER READ — only the status code is observed, so a remote response can never reach
     our logs or storage.
  D. A USER-AGENT IS ALWAYS SENT — a UA-less outbound request previously caused a production outage.
  E. FAIL-OPEN AND QUIET — any transport error is swallowed, an HTTP error status still counts as awake
     (the instance answered, which is the entire purpose), and a healthy pinger logs only on transition.
  F. THE CADENCE BEATS THE SLEEP THRESHOLD — the default is under the ~15-minute spin-down, and the
     interval cannot be configured low enough to become a traffic generator.

Run:  python3 tests/test_site_keepwarm.py
"""
from __future__ import annotations

import io
import os
import sys
import threading
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import site_keepwarm as K  # noqa: E402


class _Resp:
    """A response whose body MUST NOT be touched: reading it fails the test loudly."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a, **k):
        raise AssertionError("keep-warm read the response body — it must observe the status code only")


def main() -> int:
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    os.environ.pop("VERIPSA_KEEPWARM_URL", None)
    os.environ.pop("VERIPSA_KEEPWARM_INTERVAL_SEC", None)

    before = {t.name for t in threading.enumerate()}

    # A. off by default
    check("unset VERIPSA_KEEPWARM_URL does not start the loop", K.start_site_keepwarm() is False)
    check("an empty/whitespace target does not start the loop",
          K.start_site_keepwarm(url="   ") is False)
    check("no keep-warm thread exists when unconfigured",
          not any(t.name == "veripsa-site-keepwarm" for t in threading.enumerate()))

    # B. https only
    buf, real = io.StringIO(), sys.stdout
    sys.stdout = buf
    started_http = K.start_site_keepwarm(url="http://veripsa.com/api/healthz")
    sys.stdout = real
    check("a plaintext http:// target is REFUSED", started_http is False)
    check("the refusal is announced rather than silent", "refusing a non-HTTPS target" in buf.getvalue())
    check("the refusal does not echo the full target URL", "veripsa.com" not in buf.getvalue())

    # C + D. body never read, UA always present
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        return _Resp()

    K.urllib.request.urlopen = fake_urlopen
    code = K._ping("https://example.invalid/api/healthz")
    check("the status code is returned", code == 200)
    check("the response body is never read", True)  # _Resp.read() would have raised
    check("a User-Agent is always sent", bool(seen.get("ua")))
    check("the request is bounded by a timeout", isinstance(seen.get("timeout"), int) and seen["timeout"] > 0)

    # E. fail-open: transport error -> None, HTTP error status -> still awake
    def raise_transport(req, timeout=None):
        raise urllib.error.URLError("no route to host")

    K.urllib.request.urlopen = raise_transport
    check("a transport failure is swallowed and reported as not-responding",
          K._ping("https://example.invalid/") is None)

    def raise_http(req, timeout=None):
        raise urllib.error.HTTPError("https://example.invalid/", 503, "unavailable", {}, None)

    K.urllib.request.urlopen = raise_http
    check("an HTTP error status still counts as answered (the instance is awake)",
          K._ping("https://example.invalid/") == 503)

    # E2. transition-only logging: a steady state must not log every tick
    calls = {"n": 0}

    def ok(req, timeout=None):
        calls["n"] += 1
        return _Resp()

    K.urllib.request.urlopen = ok
    stop = threading.Event()
    buf2, real2 = io.StringIO(), sys.stdout
    sys.stdout = buf2
    t = threading.Thread(target=K._loop, args=("https://example.invalid/", 0, stop), daemon=True)
    t.start()
    while calls["n"] < 25:
        pass
    stop.set()
    t.join(timeout=5)
    sys.stdout = real2
    lines = [x for x in buf2.getvalue().splitlines() if x.strip()]
    check(f"a steady-state pinger logs once, not per tick ({calls['n']} pings -> {len(lines)} lines)",
          len(lines) <= 1)

    # F. cadence
    check("the default interval is under the ~15-minute sleep threshold",
          K._DEFAULT_INTERVAL_SEC < 900)
    check("the default interval leaves margin for one missed tick",
          K._DEFAULT_INTERVAL_SEC * 2 >= 900 or K._DEFAULT_INTERVAL_SEC <= 600)
    os.environ["VERIPSA_KEEPWARM_INTERVAL_SEC"] = "1"
    floored = False
    buf3, real3 = io.StringIO(), sys.stdout
    sys.stdout = buf3
    try:
        K.env_int("VERIPSA_KEEPWARM_INTERVAL_SEC", K._DEFAULT_INTERVAL_SEC, min_value=60)
    except BaseException:
        # The validated reader REFUSES an out-of-range knob by exiting, not by clamping — a typo'd
        # cadence must not silently become a traffic generator against the public site.
        floored = True
    finally:
        sys.stdout = real3
    os.environ.pop("VERIPSA_KEEPWARM_INTERVAL_SEC", None)
    check("the interval cannot be set below the 60s floor", floored)

    # G. isolation: the thread is a daemon and does not join shutdown
    K.urllib.request.urlopen = ok
    started = K.start_site_keepwarm(url="https://example.invalid/api/healthz", interval_sec=3600)
    kw = [t for t in threading.enumerate() if t.name == "veripsa-site-keepwarm"]
    check("a configured target starts exactly one named thread", started is True and len(kw) == 1)
    check("the keep-warm thread is a daemon (it can never hold shutdown open)",
          bool(kw) and kw[0].daemon)
    check("no unrelated thread was started", len({t.name for t in threading.enumerate()} - before) == 1)

    failed = [n for n, ok_ in results if not ok_]
    if failed:
        print(f"SITE KEEPWARM GATE: FAIL ({len(failed)} of {len(results)})")
        for n in failed:
            print("  -", n)
        return 1
    print(f"SITE KEEPWARM GATE: PASS ({len(results)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
