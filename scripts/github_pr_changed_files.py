#!/usr/bin/env python3
"""Collect PR changed-file paths without depending on the GitHub CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_PAGES = 30  # GitHub's PR-files endpoint exposes at most 3,000 entries.
# The endpoint includes bounded patch metadata as well as filenames. Keep a
# finite per-page wall without rejecting an ordinary 100-file code-heavy page.
MAX_PAGE_BYTES = 32 * 1024 * 1024
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("GitHub API redirect refused")


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects()).open


@contextmanager
def _absolute_timeout(seconds: float):
    if not 0 < seconds <= 60:
        raise ValueError("collector timeout must be between 0 and 60 seconds")
    if (
        threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "setitimer")
    ):
        raise RuntimeError("collector absolute timeout requires a POSIX main thread")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise RuntimeError("collector refuses to replace an existing process timer")

    previous_handler = signal.getsignal(signal.SIGALRM)

    def expire(_signum, _frame):
        raise TimeoutError("GitHub PR-files collection exceeded its absolute deadline")

    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _next_link(value: str) -> str | None:
    for part in value.split(","):
        match = re.fullmatch(
            r'\s*<([^>]+)>\s*;\s*rel="([^"]+)"(?:\s*;.*)?\s*', part
        )
        if match and match.group(2) == "next":
            return match.group(1)
    return None


def _same_origin(left: str, right: str) -> bool:
    lhs = urlsplit(left)
    rhs = urlsplit(right)
    return (
        lhs.scheme in {"http", "https"}
        and lhs.scheme == rhs.scheme
        and lhs.netloc == rhs.netloc
    )


def _request_json(
    *,
    url: str,
    token: str,
    deadline: float,
    opener: Callable[..., object],
) -> tuple[object, str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("GitHub PR-files collection exceeded its absolute deadline")
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "veripsa-pr-body-quality",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with opener(request, timeout=min(30, remaining)) as response:
        raw = response.read(MAX_PAGE_BYTES + 1)
        if len(raw) > MAX_PAGE_BYTES:
            raise ValueError("GitHub API response exceeded the response cap")
        return json.loads(raw), response.headers.get("Link", "")


def _pr_state(payload: object) -> tuple[str, int, str, str, str]:
    if not isinstance(payload, dict):
        raise ValueError("GitHub pull-request response was not an object")
    head = payload.get("head")
    base = payload.get("base")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    base_ref = base.get("ref") if isinstance(base, dict) else None
    base_sha = base.get("sha") if isinstance(base, dict) else None
    changed_files = payload.get("changed_files")
    if (
        not isinstance(head_sha, str)
        or not _SHA_RE.fullmatch(head_sha)
        or not isinstance(base_ref, str)
        or not base_ref
        or len(base_ref) > 1024
        or any(ord(char) < 32 for char in base_ref)
        or not isinstance(base_sha, str)
        or not _SHA_RE.fullmatch(base_sha)
        or isinstance(changed_files, bool)
        or not isinstance(changed_files, int)
        or changed_files < 0
        or payload.get("state") != "open"
    ):
        raise ValueError("GitHub pull-request snapshot was malformed")
    return head_sha, changed_files, "open", base_ref, base_sha


def fetch_changed_files(
    *,
    api_url: str,
    repository: str,
    pr_number: int,
    expected_head_sha: str,
    expected_base_ref: str,
    expected_base_sha: str,
    token: str,
    total_timeout_seconds: float = 45,
    opener: Callable[..., object] = _NO_REDIRECT_OPENER,
) -> list[str]:
    if not _REPO_RE.fullmatch(repository):
        raise ValueError("repository must be owner/name")
    if pr_number <= 0:
        raise ValueError("PR number must be positive")
    if not _SHA_RE.fullmatch(expected_head_sha):
        raise ValueError("expected head SHA must be 40 lowercase hex characters")
    if (
        not expected_base_ref
        or len(expected_base_ref) > 1024
        or any(ord(char) < 32 for char in expected_base_ref)
    ):
        raise ValueError("expected base ref was malformed")
    if not _SHA_RE.fullmatch(expected_base_sha):
        raise ValueError("expected base SHA must be 40 lowercase hex characters")
    if not token:
        raise ValueError("GitHub token is required")

    owner, repo = repository.split("/", 1)
    root = api_url.rstrip("/") + "/"
    pr_url = urljoin(
        root,
        f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{pr_number}",
    )
    current = urljoin(
        root,
        f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}/"
        f"pulls/{pr_number}/files?per_page=100",
    )
    if not _same_origin(root, pr_url) or not _same_origin(root, current):
        raise ValueError("GitHub API URL must use HTTP(S)")

    filenames: list[str] = []
    deadline = time.monotonic() + total_timeout_seconds
    with _absolute_timeout(total_timeout_seconds):
        before = _pr_state(
            _request_json(
                url=pr_url,
                token=token,
                deadline=deadline,
                opener=opener,
            )[0]
        )
        if (
            before[0] != expected_head_sha
            or before[3] != expected_base_ref
            or before[4] != expected_base_sha
        ):
            raise ValueError(
                "GitHub pull-request coordinate moved past the workflow event"
            )

        for _page in range(MAX_PAGES):
            page, link = _request_json(
                url=current,
                token=token,
                deadline=deadline,
                opener=opener,
            )
            if not isinstance(page, list):
                raise ValueError("GitHub PR-files response was not a list")
            for item in page:
                filename = item.get("filename") if isinstance(item, dict) else None
                if (
                    not isinstance(filename, str)
                    or not filename
                    or len(filename) > 4096
                    or any(char in filename for char in ("\0", "\r", "\n"))
                ):
                    raise ValueError("GitHub PR-files response had an invalid filename")
                filenames.append(filename)

            following = _next_link(link)
            if following is None:
                if len(filenames) != before[1]:
                    raise ValueError(
                        "GitHub PR-files count did not match PR metadata"
                    )
                after = _pr_state(
                    _request_json(
                        url=pr_url,
                        token=token,
                        deadline=deadline,
                        opener=opener,
                    )[0]
                )
                if (
                    after != before
                    or after[0] != expected_head_sha
                    or after[3] != expected_base_ref
                    or after[4] != expected_base_sha
                ):
                    raise ValueError(
                        "GitHub pull-request snapshot changed during Files pagination"
                    )
                return filenames
            following = urljoin(current, following)
            if not _same_origin(root, following):
                raise ValueError(
                    "GitHub pagination attempted to leave the API origin"
                )
            current = following

    raise ValueError("GitHub PR-files pagination exceeded the 3,000-file cap")


def write_changed_files(path: Path, filenames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as output:
            temporary = output.name
            for filename in filenames:
                output.write(filename + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", ""))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--expected-base-ref", required=True)
    parser.add_argument("--expected-base-sha", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=45)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        filenames = fetch_changed_files(
            api_url=args.api_url,
            repository=args.repository,
            pr_number=args.pr_number,
            expected_head_sha=args.expected_head_sha,
            expected_base_ref=args.expected_base_ref,
            expected_base_sha=args.expected_base_sha,
            token=os.environ.get("GITHUB_TOKEN", ""),
            total_timeout_seconds=args.timeout_seconds,
        )
        write_changed_files(args.output, filenames)
    except Exception as exc:
        print(
            f"changed-file collection failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    print(f"collected {len(filenames)} changed file path(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
