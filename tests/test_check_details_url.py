#!/usr/bin/env python3
"""DETAILS-URL gate — the check's "Details" link points at decision material, not a bare commit.

A synthetic navigation review shows that details_url pointing only to the commit page is a weak link to decision
material. The original target — the customer's own commit page (https://github.com/{repo}/commit/{sha})
— lands the reader on raw diff with NO Veripsa context. The replacement is a FALLBACK CHAIN to the
strongest available decision-material URL:

  1. comment anchor — https://github.com/{repo}/issues/{pr_number}#issuecomment-{comment_id}
     (lands directly on the Veripsa verdict prose with the reasoning visible)
  2. PR conversation — https://github.com/{repo}/pull/{pr_number}
     (PR thread; the Veripsa comment is in view but the reader scrolls to find it)
  3. commit page — https://github.com/{repo}/commit/{sha}
     (the back-compat default; never lost — only ever a strict improvement)

This gate proves:
  (a) the URL builder picks the most specific URL available given the inputs;
  (b) the builder is content-free (only repo, sha, pr number, comment id — never a path/body);
  (c) post_check / upsert_check carry the new kwargs through to the API body's details_url field;
  (d) upsert_check's existing-check PATCH path also uses the same chain (no stale commit URL on a re-post);
  (e) the _safe_upsert_check fallback in webhook_posters.py gracefully degrades for a gh client (test
      mock) that doesn't yet accept the new kwargs — back-compat for the 60+ existing mocks across the
      test suite.

Self-contained: no DB, no live network, no harness. Runs in < 1s offline.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

from github_rest_prsurface import _GitHubPRSurfaceMixin  # noqa: E402
import webhook_posters  # noqa: E402


# ---------------------------------------------------------------------------
# (a) URL BUILDER — fallback chain
# ---------------------------------------------------------------------------
def test_url_builder_comment_anchor():
    """When BOTH pr_number AND comment_id are present, the comment-anchor URL is used (the strongest)."""
    u = _GitHubPRSurfaceMixin._check_details_url("acme/app", "a" * 40, pr_number=42, comment_id=12345)
    assert u == "https://github.com/acme/app/issues/42#issuecomment-12345", u


def test_url_builder_pr_conversation():
    """When ONLY pr_number is present (no comment id yet — first post failed / pre-comment), use the PR URL."""
    u = _GitHubPRSurfaceMixin._check_details_url("acme/app", "a" * 40, pr_number=42)
    assert u == "https://github.com/acme/app/pull/42", u


def test_url_builder_commit_fallback():
    """When NEITHER is present (push-time watching check / merge_group with multiple PRs), use the commit URL."""
    u = _GitHubPRSurfaceMixin._check_details_url("acme/app", "a" * 40)
    assert u == "https://github.com/acme/app/commit/" + ("a" * 40), u


def test_url_builder_comment_id_without_pr_falls_back():
    """A comment id alone (no pr_number) cannot anchor — the comment-URL needs the PR/issue number too. Fall
    through to the commit URL (do NOT emit a broken URL like /issues/None#issuecomment-X)."""
    u = _GitHubPRSurfaceMixin._check_details_url("acme/app", "a" * 40, comment_id=12345)
    assert u == "https://github.com/acme/app/commit/" + ("a" * 40), u


def test_url_builder_content_free():
    """The URL only carries a repo coordinate + numeric ids — never a path, symbol, body, or file content.
    A content-leak would be a regression. Inputs simulate the WORST-case names the caller could pass."""
    u = _GitHubPRSurfaceMixin._check_details_url("acme/app", "deadbeef", pr_number=42, comment_id=999)
    # Acceptable parts: the repo coordinate, the issue/PR number, the comment id, the host.
    forbidden = ["src/", ".py", "secret", "TODO", "<", ">", " "]
    for f in forbidden:
        assert f not in u, f"URL leaked content-shaped substring {f!r}: {u}"


def test_details_url_pr_number_parser():
    """The stale-link guard recognizes only PR-specific URLs on the same repo."""
    assert _GitHubPRSurfaceMixin._details_url_pr_number(
        "acme/app", "https://github.com/acme/app/issues/42#issuecomment-12345") == 42
    assert _GitHubPRSurfaceMixin._details_url_pr_number(
        "acme/app", "https://github.com/acme/app/pull/42") == 42
    assert _GitHubPRSurfaceMixin._details_url_pr_number(
        "acme/app", "https://github.com/acme/app/commit/deadbeef") is None
    assert _GitHubPRSurfaceMixin._details_url_pr_number(
        "acme/app", "https://github.com/other/app/pull/42") is None


# ---------------------------------------------------------------------------
# (c+d) post_check / upsert_check WIRE the new fields into the API body
# ---------------------------------------------------------------------------
class _FakeAPI:
    """Mimics _api(method, path, body=None): records the calls, returns a stub response when needed."""

    def __init__(self, list_check_runs_resp=None):
        self.calls = []
        self._list_check_runs_resp = list_check_runs_resp or []

    def __call__(self, method, path, body=None):
        self.calls.append({"method": method, "path": path, "body": body})
        if method == "GET" and "check-runs" in path:
            return {"check_runs": self._list_check_runs_resp}
        if method == "POST" and "/check-runs" in path:
            return {"id": 7777}
        if method == "PATCH" and "/check-runs/" in path:
            return {"id": 7777}
        return {}


class _Client(_GitHubPRSurfaceMixin):
    def __init__(self, list_check_runs_resp=None):
        self._api = _FakeAPI(list_check_runs_resp)


def test_post_check_wires_comment_anchor():
    """post_check with pr_number AND comment_id puts the comment-anchor URL into the API body."""
    c = _Client()
    c.post_check("acme/app", "deadbeef", "neutral", "T", "S", pr_number=42, comment_id=12345)
    body = c._api.calls[0]["body"]
    assert body["details_url"] == "https://github.com/acme/app/issues/42#issuecomment-12345", body


def test_post_check_wires_pr_url():
    """post_check with ONLY pr_number puts the PR URL into the API body (comment-anchor not yet known)."""
    c = _Client()
    c.post_check("acme/app", "deadbeef", "neutral", "T", "S", pr_number=42)
    body = c._api.calls[0]["body"]
    assert body["details_url"] == "https://github.com/acme/app/pull/42", body


def test_post_check_back_compat_commit_url():
    """post_check WITHOUT the new kwargs (back-compat caller) keeps the commit URL."""
    c = _Client()
    c.post_check("acme/app", "deadbeef", "neutral", "T", "S")
    body = c._api.calls[0]["body"]
    assert body["details_url"] == "https://github.com/acme/app/commit/deadbeef", body


def test_upsert_check_patch_path_uses_comment_anchor():
    """The PATCH path (an existing Veripsa check for this sha) also uses the comment-anchor URL — proving
    a re-deliver / refresh doesn't lose the better URL once the comment exists."""
    c = _Client(list_check_runs_resp=[{
        "id": 555, "name": "Veripsa", "conclusion": "neutral",
        "output": {"title": "old", "summary": "old"},
    }])
    c.upsert_check("acme/app", "deadbeef", "neutral", "newT", "newS", pr_number=42, comment_id=12345)
    # 2 calls: list_check_runs (GET) + patch (PATCH)
    patch_call = next(k for k in c._api.calls if k["method"] == "PATCH")
    assert patch_call["body"]["details_url"] == "https://github.com/acme/app/issues/42#issuecomment-12345", \
        patch_call["body"]


def test_upsert_check_no_churn_skip_does_not_re_post():
    """The NO-CHURN invariant holds only when the rendered check AND details_url are identical."""
    c = _Client(list_check_runs_resp=[{
        "id": 555, "name": "Veripsa", "conclusion": "neutral",
        "output": {"title": "T", "summary": "S"},
        "details_url": "https://github.com/acme/app/issues/42#issuecomment-12345",
    }])
    c.upsert_check("acme/app", "deadbeef", "neutral", "T", "S", pr_number=42, comment_id=12345)
    # NO PATCH should fire — the only API call is the existing list_check_runs GET.
    patches = [k for k in c._api.calls if k["method"] == "PATCH"]
    assert patches == [], f"expected no PATCH (no-churn), got: {patches}"


def test_upsert_check_patches_stale_details_url_even_when_verdict_is_unchanged():
    """A repeated event can have identical verdict text but a newly known comment id. Patch the details_url."""
    c = _Client(list_check_runs_resp=[{
        "id": 555, "name": "Veripsa", "conclusion": "neutral",
        "output": {"title": "T", "summary": "S"},
        "details_url": "https://github.com/acme/app/commit/deadbeef",
    }])
    c.upsert_check("acme/app", "deadbeef", "neutral", "T", "S", pr_number=42, comment_id=12345)

    patch_call = next(k for k in c._api.calls if k["method"] == "PATCH")
    assert patch_call["body"]["details_url"] == "https://github.com/acme/app/issues/42#issuecomment-12345", \
        patch_call["body"]


def test_upsert_check_does_not_point_shared_sha_at_another_pr():
    """If a SHA already has a Veripsa check anchored to a different PR, avoid a wrong-PR Details link.

    GitHub check runs are commit-scoped. Duplicate PRs can share a SHA; a single check run can then appear on
    both PRs. The safe universal target is the commit URL, not PR #41 or PR #42's conversation.
    """
    c = _Client(list_check_runs_resp=[{
        "id": 555, "name": "Veripsa", "conclusion": "neutral",
        "output": {"title": "T", "summary": "S"},
        "details_url": "https://github.com/acme/app/issues/41#issuecomment-999",
    }])
    c.upsert_check("acme/app", "deadbeef", "neutral", "T", "S", pr_number=42, comment_id=12345)

    patch_call = next(k for k in c._api.calls if k["method"] == "PATCH")
    assert patch_call["body"]["details_url"] == "https://github.com/acme/app/commit/deadbeef", \
        patch_call["body"]


# ---------------------------------------------------------------------------
# (e) _safe_upsert_check back-compat for mocks that don't accept the new kwargs
# ---------------------------------------------------------------------------
class _OldMockGH:
    """Existing test-mock shape: upsert_check takes the 5 fixed positional args, no extras."""

    def __init__(self):
        self.calls = []

    def upsert_check(self, repo, sha, conclusion, title, summary):
        self.calls.append((repo, sha, conclusion, title, summary))


class _NewMockGH:
    """New shape: upsert_check accepts the optional pr_number / comment_id kwargs."""

    def __init__(self):
        self.calls = []

    def upsert_check(self, repo, sha, conclusion, title, summary, pr_number=None, comment_id=None):
        self.calls.append((repo, sha, conclusion, title, summary, pr_number, comment_id))


def test_safe_upsert_check_old_mock_falls_back_gracefully():
    """When _safe_upsert_check is given the new kwargs but the gh client is an OLD mock that doesn't accept
    them, the TypeError("unexpected keyword argument") path falls back to the legacy call. No exception
    propagates — the 60+ existing test mocks keep working with no changes to their signatures."""
    gh = _OldMockGH()
    ok = webhook_posters._safe_upsert_check(gh, "acme/app", "deadbeef", "neutral", "T", "S",
                                            pr_number=42, comment_id=12345)
    assert ok, "_safe_upsert_check should report success"
    assert gh.calls == [("acme/app", "deadbeef", "neutral", "T", "S")], gh.calls


def test_safe_upsert_check_new_mock_receives_kwargs():
    """When the gh client accepts the new kwargs, they reach it — the production GitHubREST.upsert_check is
    this shape, so the better URL flows end-to-end."""
    gh = _NewMockGH()
    ok = webhook_posters._safe_upsert_check(gh, "acme/app", "deadbeef", "neutral", "T", "S",
                                            pr_number=42, comment_id=12345)
    assert ok
    assert gh.calls == [("acme/app", "deadbeef", "neutral", "T", "S", 42, 12345)], gh.calls


def test_safe_upsert_check_back_compat_no_kwargs_path():
    """When called WITHOUT the new kwargs (the merge_group / cold-start / watching-signal paths that have no
    PR), the original signature is used — no kwargs passed, no fallback path triggered. Confirms the new
    code does not regress the non-PR call sites."""
    gh = _OldMockGH()
    ok = webhook_posters._safe_upsert_check(gh, "acme/app", "deadbeef", "neutral", "T", "S")
    assert ok
    assert gh.calls == [("acme/app", "deadbeef", "neutral", "T", "S")], gh.calls


def test_safe_upsert_check_non_kwarg_typeerror_propagates():
    """A TypeError that is NOT 'unexpected keyword argument' must still surface — we only swallow the narrow
    back-compat case. A genuine code bug (wrong arg count, wrong type) should not be hidden."""

    class _BuggyGH:
        def upsert_check(self, repo, sha, conclusion, title, summary, pr_number=None, comment_id=None):
            raise TypeError("integer is required")  # NOT the back-compat message

    gh = _BuggyGH()
    # _safe_upsert_check's outer try/except swallows ANY Exception and returns False (the "never-crash" guard).
    # But the inner TypeError check must not LOOP / fall through to the old signature on a non-back-compat TE.
    # It returns False (the post failed) — the contract is unchanged.
    ok = webhook_posters._safe_upsert_check(gh, "acme/app", "deadbeef", "neutral", "T", "S",
                                            pr_number=42, comment_id=12345)
    assert not ok, "a genuine TypeError must report failure, not silently call the legacy path"


class _ResultMockGH:
    def upsert_check(self, repo, sha, conclusion, title, summary, pr_number=None, comment_id=None):
        return {
            "id": 9090,
            "html_url": "https://github.example/checks/9090",
            "details_url": f"https://github.com/{repo}/issues/{pr_number}#issuecomment-{comment_id}",
        }


def test_upsert_check_result_preserves_observer_metadata():
    """The structured helper keeps the GitHub Check Run id/URL so runtime logs can correlate the PR surface."""
    meta = webhook_posters._upsert_check_result(
        _ResultMockGH(), "acme/app", "deadbeef", "neutral", "T", "S", pr_number=42, comment_id=12345)
    assert meta["posted"] is True, meta
    assert meta["check_run_id"] == 9090, meta
    assert meta["check_run_url"] == "https://github.example/checks/9090", meta
    assert meta["details_url"] == "https://github.com/acme/app/issues/42#issuecomment-12345", meta


# ---------------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------------
TESTS = [
    test_url_builder_comment_anchor,
    test_url_builder_pr_conversation,
    test_url_builder_commit_fallback,
    test_url_builder_comment_id_without_pr_falls_back,
    test_url_builder_content_free,
    test_details_url_pr_number_parser,
    test_post_check_wires_comment_anchor,
    test_post_check_wires_pr_url,
    test_post_check_back_compat_commit_url,
    test_upsert_check_patch_path_uses_comment_anchor,
    test_upsert_check_no_churn_skip_does_not_re_post,
    test_upsert_check_patches_stale_details_url_even_when_verdict_is_unchanged,
    test_upsert_check_does_not_point_shared_sha_at_another_pr,
    test_safe_upsert_check_old_mock_falls_back_gracefully,
    test_safe_upsert_check_new_mock_receives_kwargs,
    test_safe_upsert_check_back_compat_no_kwargs_path,
    test_safe_upsert_check_non_kwarg_typeerror_propagates,
    test_upsert_check_result_preserves_observer_metadata,
]


def main() -> int:
    failed = []
    for t in TESTS:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            failed.append((t.__name__, str(e)))
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:
            failed.append((t.__name__, f"{type(e).__name__}: {e}"))
            print(f"  [FAIL] {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"DETAILS-URL GATE: FAIL ({len(failed)}/{len(TESTS)})")
        return 1
    print("DETAILS-URL GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
