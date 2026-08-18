"""Keep the public front door warm — an OPS concern, deliberately isolated from product logic.

WHY THIS EXISTS. veripsa.com runs on a Render instance tier that sleeps after ~15 idle minutes and
cold-starts in ~10s+. A pinger existed for exactly this reason, as a scheduled GitHub Actions workflow
in the platform repository. It stopped working: every run since at least 2026-07-19 was rejected before
its first step with

    "The job was not started because recent account payments have failed or your spending limit
     needs to be increased."

100 consecutive runs, zero steps executed, so the failure was invisible in the product — the site simply
went back to sleeping. A crawler that arrives during a spin-down gets a server error rather than a page.

WHY IT LIVES HERE. Restoring the Actions pinger is not an option: paying for GitHub-hosted minutes is a
standing owner decision against, no self-hosted runner is registered for the platform repository, and
GitHub throttles scheduled workflows anyway (the previous `*/10` cron fired at a median ~76-minute gap,
well past the ~15-minute sleep threshold, so it was already a partial measure). This process is the only
thing we already run continuously on a non-sleeping paid tier, so it is the only place a RELIABLE
sub-15-minute cadence is available at no additional cost.

THIS IS A STOPGAP, NOT THE FIX. The fix is an instance tier for the site that does not sleep; then set
VERIPSA_KEEPWARM_URL empty and delete this. Keeping that explicit matters because a pinger that works
makes the underlying cost invisible.

BOUNDARIES this loop keeps:
  * OFF unless VERIPSA_KEEPWARM_URL is set — no default target, so no test or local run pings anything.
  * HTTPS only, and the response BODY is never read, logged, or stored. Only the status code is observed.
    A keep-warm request must not become a way for an external response to reach our logs.
  * A User-Agent is always sent. A UA-less outbound request caused a production outage before
    (every GitHub call 403'd); never emit one from this process again.
  * Fail-open and quiet: any error is swallowed and the loop continues. Keeping a marketing page awake
    must never be able to affect webhook processing or this service's health.
  * Logs only on TRANSITION (warm→failing, failing→warm), not every tick, so a working pinger costs
    ~2 log lines a week rather than 144 a day.
"""
from __future__ import annotations

import threading
import urllib.request

# Same standalone/package dual-import idiom the rest of the app uses; env_config imports nothing back.
try:
    from env_config import env_int
except ImportError:  # imported as a package
    from .env_config import env_int

_USER_AGENT = "Veripsa-KeepWarm"
# Under Render's ~15-minute sleep threshold with enough margin that one missed tick does not sleep the
# site. Not configurable below 60s: this exists to prevent sleeping, not to generate traffic.
_DEFAULT_INTERVAL_SEC = 600
_REQUEST_TIMEOUT_SEC = 30


def _ping(url: str) -> int | None:
    """GET `url` and return its status code, or None if it did not respond. The body is never read."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SEC) as resp:
            return int(getattr(resp, "status", 0) or 0)
    except Exception as exc:  # noqa: BLE001 - any failure is the same outcome: not confirmed awake
        # An HTTP error status still proves the instance answered, which is the whole point.
        code = getattr(exc, "code", None)
        return int(code) if isinstance(code, int) else None


def _loop(url: str, interval_sec: int, stop: threading.Event) -> None:
    awake: bool | None = None
    while not stop.wait(interval_sec):
        code = _ping(url)
        now_awake = code is not None
        if now_awake != awake:  # transition only — a healthy pinger stays silent
            if now_awake:
                print(f"keep-warm: {url} responding (HTTP {code})", flush=True)
            else:
                print(f"keep-warm: {url} did not respond at the transport level", flush=True)
            awake = now_awake


def start_site_keepwarm(url: str | None = None, interval_sec: int | None = None) -> bool:
    """Start the keep-warm loop when configured. Returns whether it started.

    Unconfigured is the DEFAULT and is not an error — this is an ops stopgap for one deployment, not a
    property of the application.
    """
    import os

    url = (url if url is not None else os.environ.get("VERIPSA_KEEPWARM_URL", "")).strip()
    if not url:
        return False
    if not url.startswith("https://"):
        # Refuse plaintext rather than silently downgrade: this runs unattended forever.
        print(f"keep-warm: refusing a non-HTTPS target ({url.split(':', 1)[0]}:) — not started", flush=True)
        return False

    interval = (interval_sec if interval_sec is not None
                else env_int("VERIPSA_KEEPWARM_INTERVAL_SEC", _DEFAULT_INTERVAL_SEC, min_value=60))
    stop = threading.Event()
    threading.Thread(target=_loop, args=(url, interval, stop),
                     name="veripsa-site-keepwarm", daemon=True).start()
    print(f"keep-warm: pinging {url} every {interval}s "
          f"(ops stopgap for a sleeping instance tier; body never read)", flush=True)
    return True
