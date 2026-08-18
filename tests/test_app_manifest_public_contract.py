#!/usr/bin/env python3
"""Offline gate for the public GitHub App registration contract.

The app manifest is copied into Marketplace/App settings by operators, so stale
claims here become public product claims. Keep this focused on public surface:
events requested by new installs and the content-free wording in the listing
description. It intentionally does not inspect private engine behavior.

Run:
  python3 tests/test_app_manifest_public_contract.py
"""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "github-app" / "app-manifest.json"
EVENT_DOCS = [ROOT / "docs" / "webhook-spec" / "events-subscribed.md"]


def main() -> int:
    manifest = json.loads(MANIFEST.read_text())
    events = set(manifest.get("default_events", []))
    permissions = manifest.get("default_permissions", {})
    description = manifest.get("description", "")

    expected_events = {
        "pull_request",
        "push",
        "repository",
        "check_suite",
        "check_run",
        "merge_group",
    }

    checks = []
    checks.append((
        "app-manifest.json default_permissions matches the least-privilege public contract",
        permissions == {
            "contents": "read",
            "pull_requests": "write",
            "checks": "write",
            "merge_queues": "read",
            "metadata": "read",
        },
    ))
    checks.append((
        "app-manifest.json default_events matches the handled public event contract",
        events == expected_events,
    ))
    checks.append((
        "app-manifest.json does not request GitHub Issues or commit-status surfaces",
        "issues" not in permissions and "statuses" not in permissions,
    ))
    checks.append((
        "app-manifest.json does not subscribe to Issues webhook surfaces",
        not ({"issues", "issue_comment", "sub_issues"} & events),
    ))
    checks.append((
        "app-manifest.json does not request deployment webhooks before a handler exists",
        "deployment" not in events and "deployment_status" not in events,
    ))
    checks.append((
        "description avoids the inaccurate 'never reads file bodies' claim",
        "never reads file bodies" not in description.lower(),
    ))
    checks.append((
        "description states the safe public content-free claim",
        "no source bodies stored or displayed" in description.lower(),
    ))

    for doc in EVENT_DOCS:
        text = doc.read_text()
        for permission, level in permissions.items():
            checks.append((
                f"{doc.relative_to(ROOT)} documents manifest permission {permission}:{level}",
                f"`{permission}`" in text and level in text,
            ))
        checks.append((
            f"{doc.relative_to(ROOT)} documents deployment_status as out of scope",
            "deployment_status" in text and "out of scope" in text,
        ))
        checks.append((
            f"{doc.relative_to(ROOT)} documents issue-backed PR endpoints without adding Issues scope",
            "issue-backed REST" in text and "does not request the `issues` permission" in text,
        ))
        checks.append((
            f"{doc.relative_to(ROOT)} documents issue/comment/sub-issue webhooks as out of scope",
            "`issues`" in text and "`issue_comment`" in text and "`sub_issues`" in text,
        ))
        checks.append((
            f"{doc.relative_to(ROOT)} distinguishes App lifecycle deliveries from default_events",
            "Automatic App lifecycle deliveries" in text
            and "not listed\nin `default_events`" in text,
        ))

    ok = all(passed for _, passed in checks)
    print("\n=== APP MANIFEST PUBLIC CONTRACT (offline) ===")
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    print("\nAPP MANIFEST PUBLIC CONTRACT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
