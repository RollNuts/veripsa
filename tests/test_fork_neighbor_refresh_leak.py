#!/usr/bin/env python3
"""FORK-NEIGHBOR-REFRESH info-leak gate (privacy launch-blocker fix, audit r3 2026-06-19).

THE BUG (RED on origin/main): render_pr_check(is_fork=True) correctly REDACTS a fork PR's OWN comment (drops
other base-repo PR refs / author logins / base-repo paths+symbols) because that comment posts on the base-repo
conversation the EXTERNAL contributor can read. server.py threads is_fork ONLY for the ACTING pull_request
event. But on open/sync/merge/withdraw/push the server ALSO re-renders every OTHER in-flight change via
webhook._refresh_changes(...) and posts each via _post_refreshes() to THAT PR's conversation — and a neighbor
that is ITSELF a fork received the FULL, non-redacted comment = the base repo's private in-flight structure
leaked to an outside contributor.

THE FIX (Design B — the engine/DB has NO fork concept, so fork status comes from GitHub at POST time):
  • _refresh_changes carries BOTH the full comment AND a redacted `fork_*` variant per in-flight change.
  • _post_refreshes resolves each neighbor's fork status (GitHubREST.pull_request_head_and_fork →
    head.repo.id != base.repo.id) in the ONE head fetch it already makes, and posts the REDACTED variant to a
    fork neighbor (full detail still goes to non-fork maintainer PRs — the redaction is SELECTIVE).

This gate proves the production path end-to-end with a PRODUCTION-realistic impact (NO injected fork flag):
  (1) the refresh entry carries a redacted `fork_comment` with NO base-repo-private token;
  (2) the full `comment` still carries the detail (the redaction is selective, not a blanket blank);
  (3) _post_refreshes, given a gh that reports the neighbor as a fork, posts the REDACTED comment to it.
RED on origin/main (the fork neighbor's posted comment leaks), GREEN after.

PURE + OFFLINE (no DB, no network).  Run:  python3 tests/test_fork_neighbor_refresh_leak.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import webhook as W                       # noqa: E402
from github_rest_prread import _GitHubPRReadMixin  # noqa: E402
from webhook_handlers import _post_refreshes   # noqa: E402

FAIL = 0


def check(cond: bool, label: str) -> None:
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


# Base-repo-private tokens an EXTERNAL fork contributor must never be shown.
PRIVATE_TOKENS = ["PR-50", "alicemaint", "verify_token", "secrets_loader", "backend/auth.py"]
REPO = "acme/private-app"


class _FakeGH:
    """Just enough of the GitHub client for _post_refreshes. PR-100 is a FORK (head.repo != base.repo); records
    every comment body it is asked to post so the test can assert what reached each PR's conversation."""

    def __init__(self):
        self.posted: dict[int, str] = {}

    def pull_request_head_and_fork(self, repo, number):
        return (f"sha-{number}", number == 100)        # PR-100 = fork; everything else = base-repo PR

    def upsert_check(self, repo, sha, conclusion, title, summary):
        pass                                            # a fork head sha can't take a check — _safe_upsert_check swallows; here a no-op

    def upsert_comment(self, repo, number, marker, body):
        self.posted[number] = body

    def patch_comment_if_exists(self, repo, number, marker, body_fn):
        return False

    def repo_default_branch_head(self, repo):
        return ("main", "sha")


class _ReadGH(_GitHubPRReadMixin):
    def __init__(self, pr):
        self.pr = pr

    def _api(self, method, path):
        return self.pr


def main() -> int:
    # PRODUCTION-REALISTIC impact: two overlapping in-flight PRs and NO `is_fork` field anywhere (the engine /
    # main_impact_surface carries no fork concept). PR-50 = a base-repo MAINTAINER PR (the acting PR). PR-100 = an
    # external FORK PR that serializes behind PR-50 on a base-repo file.
    impact = {
        "repo": REPO, "branch": "main",
        "changes": [
            {"change_id": "PR-50", "label": "alicemaint PR-50", "agent": "alicemaint", "verdict": "clear",
             "paths": ["backend/auth.py"],
             "queued_behind": [{"change_id": "PR-100", "agent": "externaluser"}],
             "queued_behind_paths": ["backend/auth.py"]},
            {"change_id": "PR-100", "label": "externaluser PR-100", "agent": "externaluser", "verdict": "serialize",
             "paths": ["backend/auth.py"],
             "serialize_behind": [{"change_id": "PR-50", "agent": "alicemaint"}],
             "collision_points": [{"behind": "alicemaint", "path": "backend/auth.py",
                                   "symbol": "verify_token", "line_lo": 10, "line_hi": 40}],
             "shared_foundation": [{"path": "backend/secrets_loader.py", "fan_in": 12, "churn": 5}]},
        ],
    }

    # The acting PR is PR-50; it refreshes every OTHER in-flight change (= the fork PR-100).
    refreshed = W._refresh_changes(impact, exclude_change="PR-50")
    fork_entry = next((e for e in refreshed if e.get("change") == "PR-100"), None)
    check(fork_entry is not None, "the fork-PR neighbor (PR-100) IS in the refresh set")

    full = (fork_entry or {}).get("comment") or ""
    fork_variant = (fork_entry or {}).get("fork_comment") or ""
    # (1) the redacted variant exists and carries NONE of the base-repo-private tokens.
    fork_leak = [t for t in PRIVATE_TOKENS if t in fork_variant]
    check(bool(fork_variant) and not fork_leak,
          "the refresh entry carries a REDACTED fork_comment variant (no base-repo-private token)  LEAK=" + repr(fork_leak))
    # (2) the FULL comment still carries the detail — the redaction is SELECTIVE (maintainer PRs keep specifics).
    check(any(t in full for t in PRIVATE_TOKENS),
          "the full comment still carries the maintainer detail (redaction is selective, not a blanket blank)")

    # (3) PRODUCTION PATH: _post_refreshes resolves PR-100 as a fork (from the gh client, not the impact) and
    # posts the REDACTED comment to its conversation.
    gh = _FakeGH()
    _post_refreshes(gh, REPO, refreshed)
    posted = gh.posted.get(100, "")
    posted_leak = [t for t in PRIVATE_TOKENS if t in posted]
    check(bool(posted) and not posted_leak,
          "_post_refreshes posts the REDACTED comment to the fork neighbor's conversation (content-free)  LEAK=" + repr(posted_leak))

    # Deleted forks can make GitHub return head.repo=null. The production REST helper must treat that unknown as
    # externally readable and select the redacted variant, while complete equal ids remain a normal same-repo PR.
    sparse_reader = _ReadGH({"head": {"sha": "sparse", "repo": None}, "base": {"repo": {"id": 1}}})
    sparse_sha, sparse_is_fork, sparse_redact = sparse_reader.pull_request_head_and_fork_privacy(REPO, 100)
    _, legacy_sparse_is_fork = sparse_reader.pull_request_head_and_fork(REPO, 100)
    _, same_repo_is_fork, same_repo_redact = _ReadGH(
        {"head": {"sha": "same", "repo": {"id": 1}}, "base": {"repo": {"id": 1}}}
    ).pull_request_head_and_fork_privacy(REPO, 101)
    check(sparse_sha == "sparse" and sparse_is_fork is False and sparse_redact is True
          and legacy_sparse_is_fork is False and same_repo_is_fork is False and same_repo_redact is False,
          "neighbor unknown identity redacts without disabling confirmed-fork merge behavior")

    print("FORK-NEIGHBOR-REFRESH GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    raise SystemExit(main())
