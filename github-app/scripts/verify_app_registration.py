#!/usr/bin/env python3
"""Verify the live GitHub App registration against Veripsa's manifest.

This is an operator check, not a product webhook path. It reads GitHub's public
App registration endpoint and compares only the public contract Veripsa depends
on: subscribed events and repository permissions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "github-app"
MANIFEST = APP_DIR / "app-manifest.json"
DEFAULT_APP_SLUG = "veripsa-core"


EXPECTED_NO_ISSUE_EVENTS = {"issues", "issue_comment", "sub_issues"}
EXPECTED_NO_ISSUE_PERMISSIONS = {"issues"}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return data


def _manifest_contract(manifest: dict[str, Any]) -> tuple[set[str], dict[str, str]]:
    events = manifest.get("default_events") or []
    permissions = manifest.get("default_permissions") or {}
    if not isinstance(events, list) or not all(isinstance(v, str) for v in events):
        raise ValueError("manifest default_events must be a list of strings")
    if not isinstance(permissions, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                   for k, v in permissions.items()):
        raise ValueError("manifest default_permissions must be a string:string object")
    return set(events), dict(permissions)


def _live_contract(live_app: dict[str, Any]) -> tuple[set[str], dict[str, str]]:
    events = live_app.get("events") or []
    permissions = live_app.get("permissions") or {}
    if not isinstance(events, list) or not all(isinstance(v, str) for v in events):
        return set(), {}
    if not isinstance(permissions, dict):
        return set(events), {}
    return set(events), {str(k): str(v) for k, v in permissions.items()}


def compare_registration(manifest: dict[str, Any], live_app: dict[str, Any]) -> list[tuple[str, bool, str]]:
    expected_events, expected_permissions = _manifest_contract(manifest)
    live_events, live_permissions = _live_contract(live_app)

    missing_events = sorted(expected_events - live_events)
    extra_events = sorted(live_events - expected_events)
    missing_permissions = sorted(k for k in expected_permissions if k not in live_permissions)
    wrong_permissions = sorted(
        f"{k}: live={live_permissions.get(k)!r} expected={expected_permissions[k]!r}"
        for k in expected_permissions
        if k in live_permissions and live_permissions.get(k) != expected_permissions[k]
    )
    extra_permissions = sorted(k for k in live_permissions if k not in expected_permissions)
    issue_events = sorted(EXPECTED_NO_ISSUE_EVENTS & live_events)
    issue_permissions = sorted(EXPECTED_NO_ISSUE_PERMISSIONS & set(live_permissions))

    def detail(items: list[str], ok_text: str) -> str:
        return ok_text if not items else ", ".join(items)

    return [
        (
            "live events exactly match app-manifest.json",
            not missing_events and not extra_events,
            f"missing=[{detail(missing_events, 'none')}] extra=[{detail(extra_events, 'none')}]",
        ),
        (
            "live permissions exactly match app-manifest.json",
            not missing_permissions and not wrong_permissions and not extra_permissions,
            "missing=[{}] wrong=[{}] extra=[{}]".format(
                detail(missing_permissions, "none"),
                detail(wrong_permissions, "none"),
                detail(extra_permissions, "none"),
            ),
        ),
        (
            "live registration does not subscribe to Issues webhooks",
            not issue_events,
            detail(issue_events, "none"),
        ),
        (
            "live registration does not request Issues permission",
            not issue_permissions,
            detail(issue_permissions, "none"),
        ),
        (
            "live registration can receive Veripsa check recovery events",
            {"check_suite", "check_run"} <= live_events and live_permissions.get("checks") == "write",
            "requires events check_suite/check_run and checks:write",
        ),
    ]


def _load_private_key() -> str:
    value = os.environ.get("GH_PRIVATE_KEY", "")
    if value and os.path.exists(value):
        return Path(value).read_text(encoding="utf-8")
    return value


def _github_api_base() -> str:
    return os.environ.get("VERIPSA_GITHUB_API_BASE", "https://api.github.com").rstrip("/")


def _default_app_slug() -> str:
    return (
        os.environ.get("GH_APP_SLUG")
        or os.environ.get("VERIPSA_GITHUB_APP_SLUG")
        or DEFAULT_APP_SLUG
    )


def fetch_public_registration(app_slug: str) -> dict[str, Any]:
    if not app_slug:
        raise RuntimeError("missing GitHub App slug")

    quoted_slug = urllib.parse.quote(app_slug, safe="")
    url = f"{_github_api_base()}/apps/{quoted_slug}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "veripsa-app-registration-contract",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed GitHub API/operator URL
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub public App endpoint returned http={exc.code} for slug={app_slug}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub public App endpoint unreadable for slug={app_slug}: {exc.reason}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("GitHub public App response was not a JSON object")
    return data


def fetch_app_jwt_registration() -> dict[str, Any]:
    missing = []
    if not os.environ.get("GH_APP_ID"):
        missing.append("GH_APP_ID")
    if not os.environ.get("GH_PRIVATE_KEY"):
        missing.append("GH_PRIVATE_KEY")
    if missing:
        raise RuntimeError("missing required env: " + ", ".join(missing))

    sys.path.insert(0, str(APP_DIR))
    from github_rest import GitHubREST  # noqa: WPS433 - script-local live import

    client = GitHubREST(os.environ["GH_APP_ID"], _load_private_key(), os.environ.get("GH_INSTALLATION_ID", ""))
    live_app = client.get_app_registration()
    if not isinstance(live_app, dict):
        raise RuntimeError("GitHub /app response was not a JSON object")
    return live_app


def fetch_live_registration(app_slug: str) -> dict[str, Any]:
    try:
        return fetch_public_registration(app_slug)
    except RuntimeError as public_error:
        if os.environ.get("GH_APP_ID") and os.environ.get("GH_PRIVATE_KEY"):
            try:
                return fetch_app_jwt_registration()
            except RuntimeError as jwt_error:
                raise RuntimeError(
                    f"public App endpoint failed ({public_error}); App JWT fallback failed ({jwt_error})"
                ) from jwt_error
        raise


def _print_results(results: list[tuple[str, bool, str]]) -> None:
    print("=== LIVE GITHUB APP REGISTRATION CONTRACT ===")
    for name, passed, info in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: {info}")
    print("LIVE GITHUB APP REGISTRATION CONTRACT:", "PASS" if all(r[1] for r in results) else "FAIL")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(MANIFEST), help="Path to app-manifest.json")
    parser.add_argument(
        "--app-slug",
        default=_default_app_slug(),
        help="Public GitHub App slug to read from GET /apps/{slug}",
    )
    parser.add_argument("--live-json", help="Read a saved GitHub App JSON response instead of calling GitHub")
    args = parser.parse_args(argv)

    try:
        manifest = _load_json(Path(args.manifest))
        live_app = _load_json(Path(args.live_json)) if args.live_json else fetch_live_registration(args.app_slug)
        results = compare_registration(manifest, live_app)
    except RuntimeError as exc:
        print(f"LIVE GITHUB APP REGISTRATION CONTRACT: ERROR: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:
        print(f"LIVE GITHUB APP REGISTRATION CONTRACT: ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    _print_results(results)
    return 0 if all(passed for _, passed, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
