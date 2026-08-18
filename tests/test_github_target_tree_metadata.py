#!/usr/bin/env python3
"""Offline unit gate for bounded target-ref Git-tree mode metadata.

No network and no DB. The fake client scripts Git Trees API responses and
asserts that the content-fetch surface returns only wanted path/mode/type
metadata while preserving enough completeness state for incremental ingest to
fail closed on symlinks, submodules, truncation, or malformed responses.

Run: ``python3 tests/test_github_target_tree_metadata.py``
"""
from __future__ import annotations

import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

from github_rest_contentfetch import (  # noqa: E402
    _GitHubContentFetchMixin,
    _TARGET_TREE_WANTED_PATH_CAP,
)


class _FakeContentClient(_GitHubContentFetchMixin):
    def __init__(self, response):
        self.response = response
        self.calls = []

    def _api(self, method, url, body=None, accept="application/vnd.github+json"):
        self.calls.append({
            "method": method,
            "url": url,
            "body": body,
            "accept": accept,
        })
        return self.response


def _entry(path, mode, entry_type, **extra):
    return {
        "path": path,
        "mode": mode,
        "type": entry_type,
        "sha": extra.pop("sha", "a" * 40),
        "size": extra.pop("size", 123),
        "url": extra.pop("url", "https://api.github.test/ignored"),
        **extra,
    }


def _check(label, condition, failures):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


def main() -> int:
    failures = []

    response = {
        "truncated": False,
        "tree": [
            _entry("src/plain.py", "100644", "blob"),
            _entry("bin/tool", "100755", "blob"),
            _entry("src/link.py", "120000", "blob"),
            _entry("vendor/dependency", "160000", "commit"),
            _entry("src", "040000", "tree"),
            _entry("unrelated.txt", "100644", "blob"),
        ],
    }
    client = _FakeContentClient(response)
    result = client.target_file_modes(
        "acme/widgets",
        "feature/mode check#1",
        [
            "src/plain.py",
            "bin/tool",
            "src/link.py",
            "vendor/dependency",
            "missing.py",
        ],
    )
    _check(
        "regular and executable modes remain distinguishable",
        result["entries"].get("src/plain.py")
        == {"mode": "100644", "type": "blob"}
        and result["entries"].get("bin/tool")
        == {"mode": "100755", "type": "blob"},
        failures,
    )
    _check(
        "symlink mode 120000 is returned without dereferencing content",
        result["entries"].get("src/link.py")
        == {"mode": "120000", "type": "blob"},
        failures,
    )
    _check(
        "submodule gitlink is returned as mode 160000/type commit",
        result["entries"].get("vendor/dependency")
        == {"mode": "160000", "type": "commit"},
        failures,
    )
    _check(
        "complete tree makes an absent wanted path authoritatively missing",
        result["complete"] is True
        and result["truncated"] is False
        and result["malformed"] is False
        and "missing.py" not in result["entries"],
        failures,
    )
    _check(
        "output contains only wanted path plus mode/type metadata",
        set(result["entries"])
        == {"src/plain.py", "bin/tool", "src/link.py", "vendor/dependency"}
        and all(
            set(value) == {"mode", "type"}
            for value in result["entries"].values()
        ),
        failures,
    )
    _check(
        "target ref is URL-quoted as one path component",
        len(client.calls) == 1
        and client.calls[0]["method"] == "GET"
        and client.calls[0]["url"]
        == (
            "/repos/acme/widgets/git/trees/"
            "feature%2Fmode%20check%231?recursive=1"
        ),
        failures,
    )

    truncated_client = _FakeContentClient({
        "truncated": True,
        "tree": [_entry("src/plain.py", "100644", "blob")],
    })
    truncated = truncated_client.target_file_modes(
        "acme/widgets", "deadbeef", ["src/plain.py", "missing.py"]
    )
    _check(
        "truncated response retains bounded findings but is never complete",
        truncated["complete"] is False
        and truncated["truncated"] is True
        and truncated["malformed"] is False
        and truncated["entries"]
        == {"src/plain.py": {"mode": "100644", "type": "blob"}},
        failures,
    )

    malformed_cases = (
        ("non-object response", []),
        ("missing truncated marker", {"tree": []}),
        ("non-list tree", {"truncated": False, "tree": {}}),
        (
            "unsupported mode/type pair",
            {
                "truncated": False,
                "tree": [_entry("src/plain.py", "120000", "commit")],
            },
        ),
        (
            "duplicate path entry",
            {
                "truncated": False,
                "tree": [
                    _entry("src/plain.py", "100644", "blob"),
                    _entry("src/plain.py", "100755", "blob"),
                ],
            },
        ),
    )
    for label, malformed_response in malformed_cases:
        malformed_client = _FakeContentClient(malformed_response)
        malformed = malformed_client.target_file_modes(
            "acme/widgets", "deadbeef", ["src/plain.py"]
        )
        _check(
            f"malformed response fails closed: {label}",
            malformed["complete"] is False
            and malformed["malformed"] is True,
            failures,
        )

    over_cap_client = _FakeContentClient({
        "truncated": False,
        "tree": [],
    })
    over_cap = over_cap_client.target_file_modes(
        "acme/widgets",
        "deadbeef",
        (
            f"src/{index}.py"
            for index in range(_TARGET_TREE_WANTED_PATH_CAP + 1)
        ),
    )
    _check(
        "wanted-path cap fails closed before any API call",
        over_cap["complete"] is False
        and over_cap["over_cap"] is True
        and over_cap_client.calls == [],
        failures,
    )

    empty_client = _FakeContentClient("must not be read")
    empty = empty_client.target_file_modes("acme/widgets", "deadbeef", [])
    _check(
        "empty wanted set is a complete no-op",
        empty["complete"] is True
        and empty["entries"] == {}
        and empty_client.calls == [],
        failures,
    )

    if failures:
        print("GITHUB TARGET TREE METADATA GATE: FAIL")
        for failure in failures:
            print(" -", failure)
        return 1
    print("GITHUB TARGET TREE METADATA GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
