#!/usr/bin/env python3
"""Offline exact-target contract for Veripsa-owned ``check_suite.requested``.

The requested suite is GitHub asking this App to create a Check on one exact
head.  A strict current same-repo PR must therefore either report an exact
successful Check post or raise so the durable queue retries.  Historical
rerequest/merge/push replay remains fail-soft.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import webhook_handlers as H  # noqa: E402


REPO = "acme/widget"
SHA = "a" * 40
checks: list[bool] = []


def chk(condition, label: str) -> None:
    passed = bool(condition)
    checks.append(passed)
    print(("  [PASS] " if passed else "  [FAIL] ") + label)


def raises_retry(work) -> bool:
    try:
        work()
    except RuntimeError as exc:
        return "exact-target replay retry required" in str(exc)
    return False


def pr(*, state="open", merged=False, sha=SHA, base="main", head_repo=77, base_repo=77) -> dict:
    return {
        "number": 7,
        "state": state,
        "merged": merged,
        "draft": False,
        "changed_files": 1,
        "user": {"login": "octo"},
        "head": {"sha": sha, "ref": "topic", "repo": {"id": head_repo, "full_name": REPO}},
        "base": {"ref": base, "sha": "b" * 40,
                 "repo": {"id": base_repo, "full_name": REPO}},
    }


class GH:
    def __init__(self, answer):
        self.answer = answer

    def get_pull_request(self, _repo, _number):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class GHByNumber:
    def __init__(self, answers):
        self.answers = answers

    def get_pull_request(self, _repo, number):
        return self.answers[number]


class StatusError(RuntimeError):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


def replay(answer, *, handler, required=SHA, sparse_base="main", default_branch_authoritative=True):
    H.handle_event = handler
    return H._rerun_prs(
        [(7, sparse_base)], REPO, "main", None, GH(answer), "check_suite requested",
        default_branch_authoritative=default_branch_authoritative, required_check_sha=required,
    )


def exact_result(*, posted=True, repo=REPO, sha=SHA) -> dict:
    return {H._CHECK_OPERATION_KEY: {"posted": posted, "repo": repo, "sha": sha}}


def main() -> int:
    original_server = H._server
    original_handle_event = H.handle_event
    original_rerun = H._rerun_prs
    original_upsert = H._upsert_check_result
    original_log = H._log_pr_surface
    original_proof_cap = H._CHECK_SUITE_REQUESTED_PROOF_CAP
    try:
        H._server = lambda: SimpleNamespace(_RERUN_PR_CAP=8)

        reran, capped = replay(pr(), handler=lambda *_: exact_result(), sparse_base="stale-sparse-base")
        chk(reran == ["PR-7"] and capped is False,
            "authoritative main overrides a stale sparse base and exact posted=true succeeds")
        fallback_reran, _ = replay(pr(), handler=lambda *_: exact_result(),
                                   default_branch_authoritative=False)
        chk(fallback_reran == ["PR-7"],
            "strict candidacy follows the exact full-PR conditions and needs no extra authority flag")

        H._server = lambda: SimpleNamespace(_RERUN_PR_CAP=1)
        multi_reran, multi_capped = H._rerun_prs(
            [(6, "main"), (7, "main")], REPO, "main", None,
            GHByNumber({6: pr(sha="d" * 40), 7: pr()}), "check_suite requested",
            default_branch_authoritative=True, required_check_sha=SHA,
        )
        chk(multi_reran == ["PR-7"] and multi_capped is False,
            "strict proof scans past the ordinary replay cap to find the exact current PR")

        H._CHECK_SUITE_REQUESTED_PROOF_CAP = 1
        chk(raises_retry(lambda: H._rerun_prs(
                [(6, "main"), (7, "main")], REPO, "main", None,
                GHByNumber({6: pr(sha="d" * 40), 7: pr()}), "check_suite requested",
                default_branch_authoritative=True, required_check_sha=SHA)),
            "strict proof ceiling fails closed instead of authorizing a Check-less delivery")
        H._CHECK_SUITE_REQUESTED_PROOF_CAP = original_proof_cap
        H._server = lambda: SimpleNamespace(_RERUN_PR_CAP=8)

        chk(raises_retry(lambda: replay(pr(), handler=lambda *_: {})),
            "strict candidate early/no-op return raises for durable retry")
        chk(raises_retry(lambda: replay(pr(), handler=lambda *_: exact_result(posted=False))),
            "strict candidate posted=false raises for durable retry")
        chk(raises_retry(lambda: replay(pr(), handler=lambda *_: exact_result(sha="c" * 40))),
            "a Check operation for the wrong SHA does not satisfy the exact-target contract")

        def inner_failure(*_args):
            raise RuntimeError("transient inner failure")

        chk(raises_retry(lambda: replay(pr(), handler=inner_failure)),
            "strict candidate replay exception propagates as retryable outer failure")
        chk(raises_retry(lambda: replay(StatusError(503), handler=lambda *_: {})),
            "non-terminal authoritative fetch failure raises for retry")
        chk(all(replay(StatusError(code), handler=lambda *_: {}) == ([], False) for code in (404, 410)),
            "authoritative 404/410 are clean terminal non-candidates")

        malformed = pr()
        malformed.pop("state")
        chk(raises_retry(lambda: replay(malformed, handler=lambda *_: exact_result())),
            "missing mandatory authoritative PR proof is malformed and retryable")
        missing_identity = pr()
        missing_identity["head"]["repo"] = {}
        chk(raises_retry(lambda: replay(missing_identity, handler=lambda *_: exact_result())),
            "missing authoritative repository identity is malformed and retryable")
        chk(raises_retry(lambda: replay(["not", "a", "PR"], handler=lambda *_: exact_result())),
            "non-object authoritative PR response is malformed and retryable")

        non_candidates = [
            pr(state="closed"),
            pr(merged=True),
            pr(sha="d" * 40),
            pr(head_repo=88),
            pr(base="release"),
        ]
        noncandidate_calls = []

        def should_not_replay(*_args):
            noncandidate_calls.append(True)
            return exact_result()

        clean = [replay(item, handler=should_not_replay) for item in non_candidates]
        chk(clean == [([], False)] * len(non_candidates) and not noncandidate_calls,
            "closed/merged/stale-head/fork/off-default PRs are clean non-required skips")
        H.handle_event = should_not_replay
        chk(H._rerun_prs([], REPO, "main", None, GH(pr()), "check_suite requested",
                        default_branch_authoritative=True, required_check_sha=SHA) == ([], False),
            "zero associated PRs is a clean non-required result")

        # Omit required_check_sha: this is the historical rerequest/merge/push contract.
        H.handle_event = inner_failure
        chk(H._rerun_prs([(7, "main")], REPO, "main", None, GH(pr()), "rerequest") == ([], False),
            "ordinary replay keeps inner failures fail-soft")
        chk(H._rerun_prs([(7, "main")], REPO, "main", None, GH(StatusError(503)), "rerequest") == ([], False),
            "ordinary replay keeps authoritative fetch failures fail-soft")

        # Lock the real acting-poster metadata that the strict outer replay consumes.
        H._upsert_check_result = lambda *_args, **_kwargs: {"posted": True}
        H._log_pr_surface = lambda *_args, **_kwargs: None
        f = {"repo": REPO, "pr": 7, "head_sha": SHA, "is_fork": False,
             "action": "synchronize", "default_branch": "main", "is_ack_label_event": False}
        result = {"check": {"conclusion": "success", "title": "clear", "summary": "clear"}}
        poster = SimpleNamespace(patch_comment_if_exists=lambda *_args, **_kwargs: False)
        check_meta = H._pr_post_check_and_comment(poster, f, result)
        H._attach_check_operation(result, REPO, SHA, check_meta)
        chk(result.get(H._CHECK_OPERATION_KEY) == {"repo": REPO, "sha": SHA, "posted": True},
            "the real acting Check poster exposes exact repo/SHA/posted metadata to its replay caller")

        captured = {}

        def capture_rerun(_entries, _repo, _branch, _db, _gh, _label, **kwargs):
            captured.update(kwargs)
            captured["entries"] = list(_entries)
            return ["PR-7"], False

        H._rerun_prs = capture_rerun
        payload = {
            "action": "requested",
            "repository": {"full_name": REPO, "default_branch": "main"},
            "check_suite": {
                "head_sha": SHA,
                "app": {"slug": "veripsa-core"},
                "pull_requests": [{"number": 7, "base": {"ref": "main"}}],
            },
        }
        wired = H._handle_check_event("check_suite", payload, None, SimpleNamespace())
        chk(captured.get("required_check_sha") == SHA
            and captured.get("suppress_neighbor_refresh") is False
            and wired.get("reran") == ["PR-7"],
            "Veripsa-owned check_suite.requested wires the suite head into strict exact-target replay")

        captured.clear()
        payload["check_suite"]["app"] = {"slug": "other-ci"}
        ignored = H._handle_check_event("check_suite", payload, None, SimpleNamespace())
        chk(ignored.get("noop") is True and not captured,
            "another app's requested suite never enters Veripsa's strict replay contract")

        payload["action"] = "rerequested"
        H._handle_check_event("check_suite", payload, None, SimpleNamespace())
        chk("required_check_sha" not in captured,
            "customer rerequest replay remains outside the strict requested-suite contract")

        # HEAD_SHA FALLBACK: GitHub delivers check_suite.requested with an EMPTY pull_requests[] under
        # processing lag. The backstop must resolve the acting PR from the suite's own head_sha (else the
        # missed pull_request delivery is never re-checked — the #13/#14 failure).
        class HeadResolverGH:
            def __init__(self, prs):
                self._prs = prs
                self.calls = 0

            def list_pull_requests_for_commit(self, _repo, _head_sha, limit=None):
                self.calls += 1
                return self._prs

        empty_payload = {
            "action": "requested",
            "repository": {"full_name": REPO, "default_branch": "main"},
            "check_suite": {"head_sha": SHA, "app": {"slug": "veripsa-core"}, "pull_requests": []},
        }

        captured.clear()
        gh_open = HeadResolverGH([pr()])  # open, same-repo head at SHA, base=main
        wired_hs = H._handle_check_event("check_suite", empty_payload, None, gh_open)
        chk(gh_open.calls == 1 and captured.get("entries") == [(7, "main")]
            and captured.get("required_check_sha") == SHA
            and wired_hs.get("resolved_by_head_sha") is True and wired_hs.get("associated_prs") == 1,
            "empty pull_requests[] + head_sha resolves the open same-repo PR by commit and replays it")

        captured.clear()
        gh_closed = HeadResolverGH([pr(state="closed")])
        wired_closed = H._handle_check_event("check_suite", empty_payload, None, gh_closed)
        chk(captured.get("entries") == [] and wired_closed.get("resolved_by_head_sha") is False
            and wired_closed.get("associated_prs") == 0,
            "empty pull_requests[] + head_sha mapping only to a CLOSED PR resolves nothing (clean no-op)")

        captured.clear()
        fork_pr = pr()
        fork_pr["head"]["repo"] = {"id": 999, "full_name": "attacker/widget"}
        gh_fork = HeadResolverGH([fork_pr])
        wired_fork = H._handle_check_event("check_suite", empty_payload, None, gh_fork)
        chk(captured.get("entries") == [] and wired_fork.get("resolved_by_head_sha") is False,
            "empty pull_requests[] + a FORK PR sharing the sha is excluded (no base-repo Check)")

        captured.clear()
        gh_offbase = HeadResolverGH([pr(base="release")])
        wired_off = H._handle_check_event("check_suite", empty_payload, None, gh_offbase)
        chk(captured.get("entries") == [] and wired_off.get("resolved_by_head_sha") is False,
            "empty pull_requests[] + an off-default-base PR is excluded")

        captured.clear()
        gh_wrong_sha = HeadResolverGH([pr(sha="d" * 40)])
        H._handle_check_event("check_suite", empty_payload, None, gh_wrong_sha)
        chk(captured.get("entries") == [],
            "empty pull_requests[] + a PR whose head moved off the suite sha is excluded")

        captured.clear()
        gh_probe = HeadResolverGH([pr()])
        nonempty_payload = {
            "action": "requested",
            "repository": {"full_name": REPO, "default_branch": "main"},
            "check_suite": {"head_sha": SHA, "app": {"slug": "veripsa-core"},
                            "pull_requests": [{"number": 7, "base": {"ref": "main"}}]},
        }
        H._handle_check_event("check_suite", nonempty_payload, None, gh_probe)
        chk(gh_probe.calls == 0 and captured.get("entries") == [(7, "main")],
            "non-empty pull_requests[] path is byte-identical — no commit->PR resolver call")
    finally:
        H._server = original_server
        H.handle_event = original_handle_event
        H._rerun_prs = original_rerun
        H._upsert_check_result = original_upsert
        H._log_pr_surface = original_log
        H._CHECK_SUITE_REQUESTED_PROOF_CAP = original_proof_cap

    ok = all(checks)
    print("\nCHECK_SUITE REQUESTED EXACT CHECK GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
