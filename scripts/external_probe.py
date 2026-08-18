#!/usr/bin/env python3
"""Read-only external probe using a neutral example repository fixture.

The release lane's forward smoke runs on the same self-hosted runner that
mutates Render, so it must not be the only observation point. This probe is a
deliberately independent one: it holds NO Render API key and NO platform read
token, speaks only public HTTPS, and is intended to run on a different runner
class (e.g. a GitHub-hosted runner) than the deploy job.

It re-proves the public source==artifact==live contract from the outside:

  * /api/version returns exactly {"commit": "<40-hex>"} == the reviewed SHA on
    both the canonical and cache-busted path, with no-store + noindex; and
  * every public launch/legal surface answers HTTP 200.

The reviewed SHA is a public git commit id, not a secret, so passing it in as
`EXPECTED_PLATFORM_SHA` keeps this probe free of any production credential.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APP_URL = os.environ.get("APP_URL", "https://veripsa.com").rstrip("/")
EXPECTED_PLATFORM_SHA = os.environ.get("EXPECTED_PLATFORM_SHA", "").strip().lower()
REQUEST_TIMEOUT_SECONDS = 20
VERSION_PATH = "/api/version"
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")

# Public surfaces that must answer 200 from an independent network path. This is
# exactly the list the release owner asked to confirm externally.
PUBLIC_PATHS: tuple[str, ...] = (
    "/",
    "/trust",
    "/ja/trust",
    "/security/disclosure",
    "/docs/onboarding",
    "/pricing",
    "/status",
    "/robots.txt",
    "/sitemap.xml",
    "/updates/sitemap.xml",
)

# This probe must stay credential-free. Guard against a misconfigured runner
# leaking a production secret into what is meant to be a public-only observer.
_FORBIDDEN_SECRET_ENV = ("RENDER_API_KEY", "PLATFORM_READ_TOKEN")


class ProbeError(RuntimeError):
    """A public-contract violation observed from the outside."""


def _bust(path: str) -> str:
    separator = "&" if "?" in path else "?"
    key = urllib.parse.quote(f"external-probe-{time.time_ns()}", safe="")
    return f"{path}{separator}external_probe={key}"


def fetch(path: str) -> tuple[int, dict[str, list[str]], bytes]:
    request = urllib.request.Request(
        f"{APP_URL}{path}",
        method="GET",
        headers={
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
            "User-Agent": "veripsa-external-probe",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            headers = _collect(response.headers)
            return int(response.status), headers, response.read()
    except urllib.error.HTTPError as error:
        return int(error.code), _collect(error.headers), error.read()


def _collect(message) -> dict[str, list[str]]:
    collected: dict[str, list[str]] = {}
    if message is None:
        return collected
    for name, value in message.items():
        collected.setdefault(str(name).lower(), []).append(str(value))
    return collected


def read_public_commit(*, cache_bust: bool) -> str:
    variant = "cache-busted" if cache_bust else "canonical"
    path = _bust(VERSION_PATH) if cache_bust else VERSION_PATH
    status, headers, raw = fetch(path)
    if status != 200:
        raise ProbeError(f"{VERSION_PATH} {variant} returned http={status}")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProbeError(f"{VERSION_PATH} {variant} returned invalid JSON") from error
    if not isinstance(body, dict) or set(body.keys()) != {"commit"}:
        shape = sorted(body.keys()) if isinstance(body, dict) else type(body).__name__
        raise ProbeError(
            f"{VERSION_PATH} {variant} JSON shape is not exactly "
            f'{{"commit": ...}}: {shape}'
        )
    commit = str(body.get("commit") or "").strip().lower()
    if not commit or commit == "unknown":
        raise ProbeError(f"{VERSION_PATH} {variant} reported commit={commit!r}")
    if not FULL_SHA_RE.fullmatch(commit):
        raise ProbeError(
            f"{VERSION_PATH} {variant} did not return a full 40-hex SHA: {commit!r}"
        )
    cache_control = " ".join(headers.get("cache-control", [])).lower()
    if "no-store" not in cache_control:
        raise ProbeError(
            f"{VERSION_PATH} {variant} missing Cache-Control: no-store "
            f"(got {cache_control!r})"
        )
    robots = " ".join(headers.get("x-robots-tag", [])).lower()
    if "noindex" not in robots:
        raise ProbeError(
            f"{VERSION_PATH} {variant} X-Robots-Tag missing noindex (got {robots!r})"
        )
    return commit


def verify_public_commit() -> str:
    if not FULL_SHA_RE.fullmatch(EXPECTED_PLATFORM_SHA):
        raise ProbeError(
            "EXPECTED_PLATFORM_SHA must be a 40-hex reviewed SHA, got "
            f"{EXPECTED_PLATFORM_SHA!r}"
        )
    canonical = read_public_commit(cache_bust=False)
    busted = read_public_commit(cache_bust=True)
    if canonical != EXPECTED_PLATFORM_SHA:
        raise ProbeError(
            f"{VERSION_PATH} canonical commit {canonical} != reviewed "
            f"{EXPECTED_PLATFORM_SHA}"
        )
    if busted != EXPECTED_PLATFORM_SHA:
        raise ProbeError(
            f"{VERSION_PATH} cache-busted commit {busted} != reviewed "
            f"{EXPECTED_PLATFORM_SHA}"
        )
    if canonical != busted:
        raise ProbeError(
            f"{VERSION_PATH} canonical {canonical} != cache-busted {busted}"
        )
    return canonical


def verify_public_surfaces() -> None:
    failures: list[str] = []
    for path in PUBLIC_PATHS:
        try:
            status, _headers, raw = fetch(path)
            if status != 200:
                failures.append(f"{path} -> http={status}")
                continue
            if not raw.strip():
                failures.append(f"{path} -> empty body")
                continue
            print(f"  ok  {path} (http=200, {len(raw)} bytes)")
        except Exception as error:  # noqa: BLE001 - report, don't crash the sweep
            failures.append(f"{path} -> {error}")
    if failures:
        raise ProbeError("public surface probe failed: " + "; ".join(failures))


def main() -> int:
    leaked = [name for name in _FORBIDDEN_SECRET_ENV if os.environ.get(name)]
    if leaked:
        # This probe is meant to be credential-free; a present prod secret means
        # it is no longer an independent, secret-free observer.
        print(
            "::error::external probe must run without production secrets; "
            f"found {', '.join(leaked)} in the environment"
        )
        return 2
    try:
        commit = verify_public_commit()
        print(
            f"external /api/version proof: {APP_URL} reports reviewed "
            f"{commit} (canonical == cache-busted, no-store + noindex)"
        )
        verify_public_surfaces()
    except Exception as error:  # noqa: BLE001 - single fail-closed exit point
        print(f"::error::external probe failed: {error}")
        return 1
    print(f"EXTERNAL PROBE VERIFIED: {APP_URL} @ {commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
