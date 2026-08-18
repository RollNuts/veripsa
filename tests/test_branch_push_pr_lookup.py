#!/usr/bin/env python3
"""BRANCH-PUSH PR LOOKUP gate.

The feature-branch push handler uses GitHub's exact PR filter
`head=<owner>:<branch>&base=<default>` before replaying a matching open PR through the normal synchronize path.
This pure unit gate verifies the REST read helper builds the exact, URL-encoded query and honors the replay cap.
"""
from __future__ import annotations

import os
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

from github_rest_prread import _GitHubPRReadMixin  # noqa: E402


class _Client(_GitHubPRReadMixin):
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _api(self, method, url, body=None, accept="application/vnd.github+json"):
        self.calls.append((method, url))
        return self.pages.pop(0)


def _query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def main() -> int:
    checks = []

    c = _Client([[{"number": 10}, {"number": 11}]])
    out = c.list_open_pull_requests_for_head("example-org/veripsa-core-old", "fix/no-code push", "main", limit=1)
    method, url = c.calls[0]
    q = _query(url)
    checks.append(("lookup uses GET /pulls with an exact same-repo head filter",
                   method == "GET" and url.startswith("/repos/example-org/veripsa-core-old/pulls?")
                   and q.get("state") == "open"
                   and q.get("head") == "example-org:fix/no-code push"
                   and q.get("base") == "main"))
    checks.append(("lookup honors the replay cap after the exact query returns",
                   [p.get("number") for p in out] == [10]))

    c2 = _Client([{"message": "bad shape"}])
    out2 = c2.list_open_pull_requests_for_head("example-org/veripsa-core-old", "feature/x", "main")
    checks.append(("lookup fail-opens on a non-list API shape", out2 == []))

    c3 = _Client([])
    out3 = c3.list_open_pull_requests_for_head("bad-repo", "feature/x", "main")
    checks.append(("lookup rejects malformed repo coordinates without calling GitHub", out3 == [] and c3.calls == []))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("BRANCH PUSH PR LOOKUP GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
