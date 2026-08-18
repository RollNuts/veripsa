#!/usr/bin/env python3
"""Docker healthcheck shared by the HTTP service and non-HTTP container roles."""
from __future__ import annotations

import os
import urllib.request


def check(*, role: str | None = None, opener=urllib.request.urlopen) -> int:
    # Render health probes apply only to traffic-serving services. Docker engines may still honor the image's
    # HEALTHCHECK for a worker/cron override; those roles have no HTTP port, and PID 1 exiting already stops the
    # container, so an HTTP probe would only create a false restart loop.
    runtime_role = role or os.environ.get("VERIPSA_RUNTIME_ROLE", "web")
    if runtime_role in {"convergence-worker", "cron"}:
        return 0
    if runtime_role != "web":
        return 1
    try:
        response = opener("http://127.0.0.1:8000/healthz", timeout=3)
        status = int(getattr(response, "status", 0))
        close = getattr(response, "close", None)
        if callable(close):
            close()
        return 0 if 200 <= status < 400 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(check())
