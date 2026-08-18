#!/usr/bin/env python3
"""Offline contract gate for the durable graph-refresh latency boundary.

This gate deliberately uses no database or network.  It locks four seams that
must remain true when graph extraction is moved out of webhook workers:

* a current exact target is not enqueued, while a stale target is;
* deferred ``ingest_push`` never reaches the extractor and enqueue failure is
  retryable (not acknowledged as success);
* a signed PR base wakes the queue and reads its closed durable state through
  one atomic SQL wrapper call, never a scalar-wake/companion-read pair;
* the live protected-branch push handler explicitly selects deferred execution
  and performs no inline PR refresh against the old graph;
* strict background convergence supersedes an old queued SHA with the
  authoritative current HEAD without ever ingesting the old target.

Run: ``python3 tests/test_graph_refresh_hotpath.py``
"""
from __future__ import annotations

import ast
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import ingest as I  # noqa: E402
import webhook_handlers as H  # noqa: E402


REPO = "acme/widget"
BRANCH = "main"
OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
REPOSITORY_ID = "77"
OWNER_ID = "4242"

checks: list[bool] = []


def chk(condition, label: str) -> None:
    passed = bool(condition)
    checks.append(passed)
    print(("  [PASS] " if passed else "  [FAIL] ") + label)


def _request_helper_contract() -> None:
    calls: list[tuple[str, tuple]] = []

    def db(sql, args=()):
        calls.append((sql, args))
        return 9

    with patch.object(
        I,
        "graph_freshness_at_target",
        return_value={
            "behind": False,
            "stored_sha": NEW_SHA,
            "head_sha": NEW_SHA,
        },
    ):
        current = I.request_main_graph_refresh(
            db, REPO, BRANCH, NEW_SHA, REPOSITORY_ID
        )
    chk(
        current.get("queued") is False
        and current.get("reason") == "already current"
        and not calls,
        "request helper: an exact current graph performs no enqueue write",
    )

    with patch.object(
        I,
        "graph_freshness_at_target",
        return_value={
            "behind": False,
            "stored_sha": NEW_SHA,
            "head_sha": NEW_SHA,
        },
    ):
        forced = I.request_main_graph_refresh(
            db, REPO, BRANCH, NEW_SHA, REPOSITORY_ID, force=True
        )
    chk(
        forced.get("queued") is True
        and len(calls) == 1
        and "enqueue_graph_refresh_with_authority" in calls[0][0],
        "request helper: an untrusted PR base can force an authoritative background turn",
    )

    calls.clear()
    with patch.object(
        I,
        "graph_freshness_at_target",
        return_value={
            "behind": True,
            "stored_sha": OLD_SHA,
            "head_sha": NEW_SHA,
        },
    ):
        stale = I.request_main_graph_refresh(
            db, REPO, BRANCH, NEW_SHA, REPOSITORY_ID
        )
    chk(
        stale.get("queued") is True
        and stale.get("queue_epoch") == 9
        and len(calls) == 1
        and "enqueue_graph_refresh_with_authority" in calls[0][0]
        and calls[0][1] == (REPO, BRANCH, NEW_SHA, REPOSITORY_ID),
        "request helper: a stale graph durably enqueues the exact coordinate",
    )


def _wake_only_atomic_receipt_contract() -> None:
    calls: list[tuple[str, tuple]] = []

    def db(sql, args=()):
        calls.append((sql, args))
        return {
            "request_epoch": 17,
            "unfinished": True,
            "state": "quota_paused",
        }

    with patch.object(
        I,
        "graph_freshness_at_target",
        return_value={
            "behind": True,
            "stored_sha": OLD_SHA,
            "head_sha": NEW_SHA,
        },
    ):
        receipt = I.request_main_graph_refresh_wake_only(
            db, REPO, BRANCH, NEW_SHA, REPOSITORY_ID
        )

    sql = calls[0][0] if len(calls) == 1 else ""
    chk(
        len(calls) == 1
        and "wake_graph_refresh_candidate_state_with_authority" in sql
        and "wake_graph_refresh_candidate_with_authority(" not in sql
        and "graph_refresh_wake_state_with_authority" not in sql
        and calls[0][1] == (REPO, BRANCH, NEW_SHA, REPOSITORY_ID),
        "PR wake helper uses exactly one atomic state-returning DB call, never scalar wake + companion read",
    )
    chk(
        receipt.get("queued") is True
        and receipt.get("queue_epoch") == 17
        and receipt.get("quota_paused") is True
        and receipt.get("reason") == "free-tier limit",
        "atomic wake receipt preserves the durable quota-paused state for the PR surface",
    )


def _deferred_ingest_contract() -> None:
    extractor_calls: list[str] = []
    request_calls: list[tuple] = []
    fake_server = SimpleNamespace(
        _as_obj=lambda value: value if isinstance(value, dict) else {},
        _push_changed_sets=lambda _payload: ([], [], None),
        _push_author_is_bot=lambda _payload: False,
    )
    payload = {
        "after": NEW_SHA,
        "before": OLD_SHA,
        "commits": [],
        "size": 0,
        "repository": {"id": int(REPOSITORY_ID), "full_name": REPO},
        "_veripsa_trace_id": "offline-hotpath",
    }

    def forbidden_reingest(*_args, **_kwargs):
        extractor_calls.append("called")
        raise AssertionError("_reingest_graph reached from deferred execution")

    def requested(*args, **kwargs):
        request_calls.append((*args[1:5], kwargs.get("trace_id")))
        return {
            "queued": True,
            "reason": "refresh queued",
            "head_sha": NEW_SHA,
            "queue_epoch": 3,
        }

    common = (
        patch.object(I, "_server", return_value=fake_server),
        patch.object(I, "_record_push_facts", return_value=None),
        patch.object(I, "_dispatch_cochange", return_value=None),
        patch.object(I, "_reconcile_repo_identity", return_value=None),
        patch.object(I, "_reingest_graph", side_effect=forbidden_reingest),
    )
    with common[0], common[1], common[2], common[3], common[4], patch.object(
        I, "request_main_graph_refresh", side_effect=requested
    ):
        result = I.ingest_push_deferred(
            lambda *_args: None,
            object(),
            REPO,
            BRANCH,
            NEW_SHA,
            payload,
        )
    chk(
        result.get("mode") == "queued"
        and result.get("deferred") == "graph_refresh_queued"
        and request_calls
        and not extractor_calls,
        "deferred ingest schedules durable work and never reaches _reingest_graph",
    )

    with patch.object(I, "_server", return_value=fake_server), patch.object(
        I, "_record_push_facts", return_value=None
    ), patch.object(I, "_dispatch_cochange", return_value=None), patch.object(
        I, "_reconcile_repo_identity", return_value=None
    ), patch.object(
        I, "_reingest_graph", side_effect=forbidden_reingest
    ), patch.object(
        I,
        "request_main_graph_refresh",
        side_effect=RuntimeError("simulated durable enqueue failure"),
    ):
        raised = False
        try:
            I.ingest_push_deferred(
                lambda *_args: None,
                object(),
                REPO,
                BRANCH,
                NEW_SHA,
                payload,
            )
        except RuntimeError as exc:
            raised = "durable enqueue failure" in str(exc)
    chk(
        raised and not extractor_calls,
        "deferred ingest propagates enqueue failure for durable delivery retry",
    )

    quota_request_calls: list[str] = []
    with patch.object(I, "_server", return_value=fake_server), patch.object(
        I,
        "_record_push_facts",
        return_value={"quota_exceeded": True, "reason": "free-tier limit"},
    ), patch.object(I, "_dispatch_cochange", return_value=None), patch.object(
        I, "_reconcile_repo_identity", return_value=None
    ), patch.object(
        I, "_reingest_graph", side_effect=forbidden_reingest
    ), patch.object(
        I,
        "request_main_graph_refresh",
        side_effect=lambda *_a, **_k: (
            quota_request_calls.append("queued")
            or {"queued": True, "queue_epoch": 4, "head_sha": NEW_SHA}
        ),
    ):
        quota_result = I.ingest_push_deferred(
            lambda *_args: None,
            object(),
            REPO,
            BRANCH,
            NEW_SHA,
            payload,
        )
    chk(
        quota_result.get("quota_exceeded") is True
        and quota_result.get("mode") == "queued"
        and quota_result.get("graph_refresh", {}).get("queued") is True
        and quota_request_calls == ["queued"]
        and not extractor_calls,
        "quota-paused live push still persists exact convergence work for automatic resume",
    )


def _live_onboarding_resource_contract() -> None:
    populate_calls: list[str] = []
    with patch.object(
        I,
        "request_repository_onboarding",
        return_value={"queued": True, "queue_epoch": 5},
    ), patch.object(
        I,
        "populate_cochange_async",
        side_effect=lambda *_a, **_k: populate_calls.append("clone"),
    ), patch.object(
        I,
        "backfill_open_prs",
        return_value={"count": 0},
    ):
        result = I._queue_backfill_repo(
            lambda *_a, **_k: None,
            SimpleNamespace(
                repo_default_branch_head=lambda _repo: (BRANCH, NEW_SHA)
            ),
            REPO,
            repository_id=REPOSITORY_ID,
        )
    chk(
        result.get("graph", {}).get("queued") is True
        and result.get("cochange", {}).get("dispatched") is False
        and result.get("cochange", {}).get("deferred")
        == "durable_low_priority_lane_required"
        and not populate_calls,
        "live onboarding starts no history clone; advisory history waits for a durable low-priority lane",
    )


def _live_push_contract() -> None:
    ingest_kwargs: list[dict] = []
    inline_refresh_calls: list[str] = []

    def fake_ingest(*_args, **kwargs):
        ingest_kwargs.append(dict(kwargs))
        return {
            "ingested": REPO,
            "branch": BRANCH,
            "sha": NEW_SHA,
            "reingested": False,
            "mode": "queued",
            "deferred": "graph_refresh_queued",
            "graph_refresh": {"queued": True, "head_sha": NEW_SHA},
        }

    payload = {
        "ref": "refs/heads/main",
        "after": NEW_SHA,
        "repository": {
            "id": int(REPOSITORY_ID),
            "full_name": REPO,
            "default_branch": BRANCH,
        },
    }
    with patch.object(H, "ingest_push_deferred", side_effect=fake_ingest), patch.object(
        H,
        "refresh_inflight",
        side_effect=lambda *_a, **_k: inline_refresh_calls.append("refresh"),
    ), patch.object(
        H,
        "_post_refreshes",
        side_effect=lambda *_a, **_k: inline_refresh_calls.append("post"),
    ), patch.object(
        H,
        "_safe_upsert_check",
        side_effect=lambda *_a, **_k: inline_refresh_calls.append("watching"),
    ):
        result = H._handle_push_event(payload, object(), object())
    chk(
        len(ingest_kwargs) == 1
        and "graph_execution" not in ingest_kwargs[0],
        "live protected-branch push calls the structurally queue-only ingest entry point",
    )
    chk(
        result.get("mode") == "queued" and not inline_refresh_calls,
        "live queued push returns before inline neighbor/watching refresh",
    )


def _live_pr_neighbor_offload_contract() -> None:
    """The acting surface is immediate; every sibling surface is durable background work."""
    wake_calls: list[tuple] = []
    acting_posts: list[str] = []
    forbidden_neighbor_posts: list[str] = []
    neighbor_entries = [
        {"change": "PR-11", "conclusion": "neutral", "summary": "bounded"},
        {"change": "PR-12", "conclusion": "success", "summary": "bounded"},
    ]
    fields = {
        "should_analyze": False,
        "repo": REPO,
        "default_branch": BRANCH,
        "author": "octo",
        "pr": 10,
        "action": "closed",
        "base": BRANCH,
        "base_sha": OLD_SHA,
        "repository_id": REPOSITORY_ID,
        "is_ack_label_event": False,
        "head_sha": NEW_SHA,
        "trace_id": "",
    }

    def brain(*_args, **_kwargs):
        return {
            "check": {"conclusion": "success", "title": "Veripsa", "summary": "ok"},
            "comment": None,
            "refreshed": list(neighbor_entries),
        }

    with patch.object(H, "_pr_eligibility", return_value=(None, fields)), patch.object(
        H, "_pr_fetch_changed", return_value=([], {}, [], [], None, 0, 0)
    ), patch.object(
        H, "_pr_pre_brain", return_value=([], {}, [], False, {}, [])
    ), patch.object(
        H, "_pr_outcome_signals", return_value=(False, "unknown", False)
    ), patch.object(
        H, "_pr_build_event", return_value={"action": "closed"}
    ), patch.object(
        H, "handle_pull_request", side_effect=brain
    ), patch.object(
        H, "_pr_quota_paused_result", return_value=None
    ), patch.object(
        H, "_pr_stale_graph_unknown_result", return_value=None
    ), patch.object(
        H, "_pr_apply_pause_ack_overlay", return_value=None
    ), patch.object(
        H, "_pr_post_check_and_comment",
        side_effect=lambda *_a, **_k: acting_posts.append("acting") or {"posted": False},
    ), patch.object(
        H, "request_main_graph_refresh_wake_only",
        side_effect=lambda *args, **kwargs: wake_calls.append((args, kwargs)) or {
            "healed": False, "queued": True, "reason": "refresh wake recorded",
            "head_sha": OLD_SHA, "queue_epoch": 4,
        },
    ), patch.object(
        H, "_post_refreshes",
        side_effect=lambda *_a, **_k: forbidden_neighbor_posts.append("post") or 2,
    ), patch.object(H, "_pr_record_landing", return_value=None):
        result = H._handle_pull_request_event(
            "pull_request",
            {
                "action": "closed",
                "repository": {
                    "id": int(REPOSITORY_ID),
                    "full_name": REPO,
                    "default_branch": BRANCH,
                },
            },
            lambda *_args, **_kwargs: None,
            object(),
        )

    chk(
        acting_posts == ["acting"] and not forbidden_neighbor_posts,
        "live PR publishes only its acting Check and performs zero synchronous neighbor mutations",
    )
    chk(
        len(wake_calls) == 1
        and result.get("refreshed_inflight") == 0
        and result.get("refresh_deferred") == len(neighbor_entries)
        and result.get("graph_heal", {}).get("queued") is True,
        "neighbor changes durably wake one repo convergence turn and are reported as deferred",
    )


def _planned_onboarding_replay_contract() -> None:
    """Exact planned replay uses an unforgeable graph proof and never treats PR base.sha as current HEAD."""
    head_sha = "c" * 40

    def pr_object(*, base_ref=BRANCH, base_sha=OLD_SHA, full_name=REPO,
                  default_branch=BRANCH, head_repo_id=REPOSITORY_ID,
                  changed_files=1):
        return {
            "number": 7, "state": "open", "merged": False,
            "base": {
                "ref": base_ref, "sha": base_sha,
                "repo": {
                    "id": int(REPOSITORY_ID), "full_name": full_name,
                    "owner": {"id": int(OWNER_ID)},
                    "default_branch": default_branch,
                },
            },
            "head": {
                "sha": head_sha, "ref": "feature/x",
                "repo": {
                    "id": int(head_repo_id), "full_name": REPO,
                    "owner": {"id": int(OWNER_ID)},
                    "default_branch": BRANCH,
                },
            },
            "user": {"login": "octo"}, "labels": [], "changed_files": changed_files,
        }

    dispatch_proofs: list[object] = []
    enqueue_calls: list[tuple] = []

    class ExactGH:
        def __init__(self, current, branch_head=NEW_SHA):
            self.current = current
            self.branch_head = branch_head
            self.pr_reads = 0
            self.head_reads = 0

        def get_pull_request(self, _repo, _number):
            self.pr_reads += 1
            return self.current

        def repo_branch_head(self, _repo, _branch):
            self.head_reads += 1
            return self.branch_head

    def dispatched(*_args, **kwargs):
        dispatch_proofs.append(kwargs.get("planned_onboarding_graph_proof"))
        return {
            "_veripsa_check_operation": {
                "repo": REPO, "sha": head_sha, "posted": True,
            }
        }

    def enqueue(*args, **kwargs):
        enqueue_calls.append((*args[1:5], kwargs.get("force")))
        return {"queued": True, "queue_epoch": 41, "head_sha": args[3]}

    exact_gh = ExactGH(pr_object(base_sha=OLD_SHA))
    drift_gh = ExactGH(pr_object(base_sha=OLD_SHA), branch_head="d" * 40)
    retarget_gh = ExactGH(pr_object(base_ref="release", base_sha=OLD_SHA))
    with patch.object(I, "_dispatch_synthetic_pr_replay", side_effect=dispatched), patch.object(
        I, "request_main_graph_refresh", side_effect=enqueue
    ):
        exact = I.replay_onboarding_pull_request(
            lambda *_a, **_k: None, exact_gh,
            REPO, BRANCH, NEW_SHA, REPOSITORY_ID, OWNER_ID, 7)
        drift = I.replay_onboarding_pull_request(
            lambda *_a, **_k: None, drift_gh,
            REPO, BRANCH, NEW_SHA, REPOSITORY_ID, OWNER_ID, 7)
        retarget = I.replay_onboarding_pull_request(
            lambda *_a, **_k: None, retarget_gh,
            REPO, BRANCH, NEW_SHA, REPOSITORY_ID, OWNER_ID, 7)

    chk(
        exact.get("receipt_kind") == "exact_check"
        and dispatch_proofs == [I._PLANNED_ONBOARDING_GRAPH_PROOF]
        and drift.get("superseded") is True
        and enqueue_calls == [(REPO, BRANCH, "d" * 40, REPOSITORY_ID, True)]
        and all(client.pr_reads == 1 and client.head_reads == 1
                for client in (exact_gh, drift_gh, retarget_gh)),
        "old PR base.sha may differ from current target without graph rollback; a real branch-HEAD drift "
        "preserving-enqueues current HEAD and consumes no cursor; every planned PR pays exactly one current "
        "PR read and one current default-HEAD read before replay",
    )
    chk(
        retarget.get("receipt") is True
        and retarget.get("receipt_kind") == "authoritative_unsupported_base",
        "a current stable-id PR retargeted off the protected branch is an exact skip receipt, not cursor poison",
    )

    class QuotaGH(ExactGH):
        def __init__(self, current, *, fail_comment=False, fail_check=False):
            super().__init__(current)
            self.fail_comment = fail_comment
            self.fail_check = fail_check
            self.comments = []
            self.checks = []

        def upsert_comment(self, repo, number, marker, body):
            if self.fail_comment:
                raise TimeoutError("injected comment failure")
            self.comments.append((repo, number, marker, body))
            return {"id": 71}

        def upsert_check(self, repo, sha, conclusion, title, summary, **kwargs):
            if self.fail_check:
                raise TimeoutError("injected check failure")
            self.checks.append((repo, sha, conclusion, title, summary, kwargs))
            return {"id": 72}

    quota_same = QuotaGH(pr_object())
    quota_fork = QuotaGH(
        pr_object(head_repo_id="88"), fail_check=True)
    quota_unconfirmed = QuotaGH(
        pr_object(), fail_comment=True, fail_check=True)
    forbidden_db = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("quota surface must not enter the PR brain or DB analysis"))
    with patch.object(
        I, "_dispatch_synthetic_pr_replay",
        side_effect=AssertionError("quota surface replayed the PR brain"),
    ):
        same_surface = I.surface_onboarding_quota_paused_pull_request(
            forbidden_db, quota_same, REPO, BRANCH, NEW_SHA,
            REPOSITORY_ID, OWNER_ID, 7)
        fork_surface = I.surface_onboarding_quota_paused_pull_request(
            forbidden_db, quota_fork, REPO, BRANCH, NEW_SHA,
            REPOSITORY_ID, OWNER_ID, 7)
        missing_surface = I.surface_onboarding_quota_paused_pull_request(
            forbidden_db, quota_unconfirmed, REPO, BRANCH, NEW_SHA,
            REPOSITORY_ID, OWNER_ID, 7)
    chk(
        same_surface.get("receipt_kind") == "exact_quota_check"
        and len(quota_same.comments) == len(quota_same.checks) == 1
        and fork_surface.get("receipt_kind") == "exact_quota_fork_comment"
        and len(quota_fork.comments) == 1 and quota_fork.checks == []
        and missing_surface.get("receipt") is False
        and missing_surface.get("retryable_surface") is True
        and quota_unconfirmed.comments == quota_unconfirmed.checks == []
        and all(client.pr_reads == 1 and client.head_reads == 1
                for client in (quota_same, quota_fork, quota_unconfirmed)),
        "quota onboarding reuses exact current-PR/default-HEAD authority but bypasses files, brain, and claims; "
        "same-repo requires its exact Check, a fork requires its exact comment, and an unconfirmed surface "
        "cannot advance the durable cursor",
    )

    # The private planned path has a structural one-page Files ceiling. A declared mega PR makes zero Files
    # calls; <=100 may make one call with max_pages=1, and a contradictory next-page link becomes honest Unknown
    # without reaching the legacy unbounded fallback. The ordinary live path retains max_pages=None.
    class FilesBudgetGH:
        def __init__(self, *, page_overflow=False):
            self.page_overflow = page_overflow
            self.calls: list[int | None] = []

        def list_pr_file_metadata(
                self, _repo, _number, declared, max_pages=None):
            self.calls.append(max_pages)
            if self.page_overflow:
                raise H.PRFilesPageBudgetExceeded(1, 1)
            return {
                "changed": ["svc/a.py"], "changed_ranges": {"svc/a.py": []},
                "added_paths": [], "conflict_markers": [],
                "raw_entry_count": declared,
            }

    def fetch_fields(changed_files):
        return {
            "should_analyze": True, "repo": REPO, "pr": 7,
            "prj": {"changed_files": changed_files},
            "trace_id": "", "delivery": "",
        }

    mega_files = FilesBudgetGH()
    mega_read = H._pr_fetch_changed(
        mega_files, fetch_fields(101), planned_onboarding=True)
    linked_files = FilesBudgetGH(page_overflow=True)
    linked_read = H._pr_fetch_changed(
        linked_files, fetch_fields(100), planned_onboarding=True)
    live_files = FilesBudgetGH()
    H._pr_fetch_changed(live_files, fetch_fields(101))
    chk(
        mega_read[4] == "planned_page_budget" and mega_files.calls == []
        and linked_read[4] == "planned_page_budget" and linked_files.calls == [1]
        and live_files.calls == [None],
        "planned replay performs zero Files calls for declared >100, otherwise at most one page; ordinary live "
        "webhooks retain their existing uncapped acting-path call",
    )

    # Prove the handler wires the capability to both the Files read and unread receipt. A boolean impostor takes
    # the ordinary path and cannot mint the private result identity even when the public payload keys match.
    unread_fields = {
        "should_analyze": True, "repo": REPO, "default_branch": BRANCH,
        "author": "octo", "pr": 7, "action": "synchronize", "base": BRANCH,
        "base_sha": OLD_SHA, "repository_id": REPOSITORY_ID,
        "is_ack_label_event": False, "head_sha": head_sha, "trace_id": "",
        "delivery": "", "is_fork": False, "prj": {"changed_files": 101},
    }

    class UnreadGH(FilesBudgetGH):
        def list_pr_file_metadata(
                self, _repo, _number, _declared, max_pages=None):
            self.calls.append(max_pages)
            return {
                "changed": [], "changed_ranges": {}, "added_paths": [],
                "conflict_markers": [], "raw_entry_count": 0,
            }

        def upsert_comment(self, *_args, **_kwargs):
            return {"id": 55}

        def upsert_check(self, *_args, **_kwargs):
            return {"id": 66}

    private_gh, forged_gh = UnreadGH(), UnreadGH()
    with patch.object(H, "_pr_eligibility", return_value=(None, unread_fields)):
        private_unread = H._handle_pull_request_event(
            "pull_request", {}, lambda *_a, **_k: None, private_gh,
            planned_onboarding_graph_proof=I._PLANNED_ONBOARDING_GRAPH_PROOF)
        forged_unread = H._handle_pull_request_event(
            "pull_request", {}, lambda *_a, **_k: None, forged_gh,
            planned_onboarding_graph_proof=True)
    chk(
        private_gh.calls == [] and forged_gh.calls == [None]
        and private_unread.get(I._PLANNED_ONBOARDING_BOUNDED_UNREAD_KEY)
        is I._PLANNED_ONBOARDING_GRAPH_PROOF
        and isinstance(private_unread.get("_veripsa_pr_surface_operation"), dict)
        and I._PLANNED_ONBOARDING_BOUNDED_UNREAD_KEY not in forged_unread
        and "_veripsa_pr_surface_operation" not in forged_unread,
        "only the module-private planned capability can mint a bounded-Unknown cursor receipt; a boolean/payload "
        "forgery follows the ordinary Files path and gains no private surface proof",
    )

    def bounded_result(*, marker=I._PLANNED_ONBOARDING_GRAPH_PROOF,
                       check_posted=True, comment_posted=False):
        return {
            "skipped": "planned pr-files one-page ceiling",
            I._PLANNED_ONBOARDING_BOUNDED_UNREAD_KEY: marker,
            "_veripsa_check_operation": {
                "repo": REPO, "sha": head_sha, "posted": check_posted,
            },
            "_veripsa_pr_surface_operation": {
                "repo": REPO, "pr_number": 7, "sha": head_sha,
                "check_posted": check_posted,
                "comment_posted": comment_posted,
            },
        }

    with patch.object(I, "_dispatch_synthetic_pr_replay", return_value=bounded_result()):
        bounded_check = I.replay_onboarding_pull_request(
            lambda *_a, **_k: None, ExactGH(pr_object(changed_files=101)),
            REPO, BRANCH, NEW_SHA, REPOSITORY_ID, OWNER_ID, 7)
    fork_pr = pr_object(head_repo_id="88", changed_files=101)
    with patch.object(
        I, "_dispatch_synthetic_pr_replay",
        return_value=bounded_result(check_posted=False, comment_posted=True),
    ):
        bounded_fork = I.replay_onboarding_pull_request(
            lambda *_a, **_k: None, ExactGH(fork_pr),
            REPO, BRANCH, NEW_SHA, REPOSITORY_ID, OWNER_ID, 7)
    forged_accepted = False
    missing_fork_comment_accepted = False
    fork_check_only_accepted = False
    try:
        with patch.object(
            I, "_dispatch_synthetic_pr_replay",
            return_value=bounded_result(marker=True),
        ):
            I.replay_onboarding_pull_request(
                lambda *_a, **_k: None, ExactGH(pr_object(changed_files=101)),
                REPO, BRANCH, NEW_SHA, REPOSITORY_ID, OWNER_ID, 7)
        forged_accepted = True
    except RuntimeError:
        pass
    try:
        with patch.object(
            I, "_dispatch_synthetic_pr_replay",
            return_value=bounded_result(check_posted=False, comment_posted=False),
        ):
            I.replay_onboarding_pull_request(
                lambda *_a, **_k: None, ExactGH(fork_pr),
                REPO, BRANCH, NEW_SHA, REPOSITORY_ID, OWNER_ID, 7)
        missing_fork_comment_accepted = True
    except RuntimeError:
        pass
    try:
        with patch.object(
            I, "_dispatch_synthetic_pr_replay",
            return_value=bounded_result(check_posted=True, comment_posted=False),
        ):
            I.replay_onboarding_pull_request(
                lambda *_a, **_k: None, ExactGH(fork_pr),
                REPO, BRANCH, NEW_SHA, REPOSITORY_ID, OWNER_ID, 7)
        fork_check_only_accepted = True
    except RuntimeError:
        pass
    chk(
        bounded_check.get("receipt_kind") == "exact_bounded_unknown_check"
        and bounded_fork.get("receipt_kind") == "exact_bounded_unknown_fork_comment"
        and not forged_accepted and not missing_fork_comment_accepted
        and not fork_check_only_accepted,
        "bounded mega-PR Unknown advances only after an exact Check or confirmed-fork exact comment; forged "
        "capability, missing fork surface, and base-repo Check-only fork receipts all retry the immutable cursor",
    )

    renamed_repo = "acme/widget-renamed"
    with patch.object(
        I, "resolve_repository_onboarding_head",
        return_value={"superseded": True, "renamed": True, "repo": renamed_repo},
    ), patch.object(I, "_dispatch_synthetic_pr_replay", side_effect=AssertionError("old name replayed")):
        renamed = I.replay_onboarding_pull_request(
            lambda *_a, **_k: None,
            ExactGH(pr_object(full_name=renamed_repo)),
            REPO, BRANCH, NEW_SHA, REPOSITORY_ID, OWNER_ID, 7)
    chk(
        renamed.get("superseded") is True
        and renamed.get("receipt_kind") == "canonical_repo_changed",
        "a same-id/owner canonical rename is reconciled and superseded before any old-coordinate surface",
    )

    fields = {
        "should_analyze": True, "repo": REPO, "default_branch": BRANCH,
        "author": "octo", "pr": 7, "action": "synchronize", "base": BRANCH,
        "base_sha": OLD_SHA, "repository_id": REPOSITORY_ID,
        "is_ack_label_event": False, "head_sha": head_sha, "trace_id": "",
    }
    wake_calls: list[str] = []
    degraded: list[bool] = []

    def stale_probe(_gh, _f, _result, graph_heal):
        degraded.append(H._graph_degraded(graph_heal))
        return None

    with patch.object(H, "_pr_eligibility", return_value=(None, fields)), patch.object(
        H, "_pr_fetch_changed", return_value=(["svc/a.py"], {}, [], [], None, 1, 1)
    ), patch.object(H, "_pr_files_snapshot_is_current", return_value=True), patch.object(
        H, "_pr_pre_brain", return_value=(["svc/a.py"], {}, [], False, {}, [])
    ), patch.object(H, "_pr_outcome_signals", return_value=(False, "unknown", False)), patch.object(
        H, "_pr_build_event", return_value={"action": "synchronize"}
    ), patch.object(H, "handle_pull_request", side_effect=lambda *_a, **_k: {
        "check": {"conclusion": "success", "title": "Veripsa", "summary": "Clear"},
        "comment": None, "refreshed": [],
    }), patch.object(H, "_pr_quota_paused_result", return_value=None), patch.object(
        H, "_pr_stale_graph_unknown_result", side_effect=stale_probe
    ), patch.object(H, "_pr_apply_pause_ack_overlay", return_value=None), patch.object(
        H, "_pr_post_check_and_comment", return_value={"posted": False}
    ), patch.object(H, "_pr_record_landing", return_value=None), patch.object(
        H, "request_main_graph_refresh_wake_only",
        side_effect=lambda *_a, **_k: (
            wake_calls.append("wake")
            or {"queued": True, "reason": "refresh wake recorded", "head_sha": NEW_SHA}
        ),
    ):
        proven = H._handle_pull_request_event(
            "pull_request", {"_veripsa_no_neighbor_refresh": True},
            lambda *_a, **_k: None, object(),
            planned_onboarding_graph_proof=I._PLANNED_ONBOARDING_GRAPH_PROOF)
        forged = H._handle_pull_request_event(
            "pull_request", {"_veripsa_no_neighbor_refresh": True},
            lambda *_a, **_k: None, object(),
            planned_onboarding_graph_proof=True)
    chk(
        proven.get("graph_heal", {}).get("reason") == "already current"
        and forged.get("graph_heal", {}).get("reason") == "refresh wake recorded"
        and wake_calls == ["wake"] and degraded == [False, True],
        "only the module-private object identity suppresses planned structural wake/Unknown; boolean payload "
        "forgery remains degraded and follows the ordinary wake path",
    )


def _strict_supersession_contract() -> None:
    requested_targets: list[tuple[str, str, bool]] = []
    old_target_writes: list[str] = []

    def reenqueue(_db, _repo, target_branch, target, _repo_id, **kwargs):
        requested_targets.append((target_branch, target, kwargs.get("force") is True))
        return {
            "queued": True,
            "head_sha": target,
            "queue_epoch": 12,
        }

    def forbidden_heal(*_args, **_kwargs):
        old_target_writes.append(OLD_SHA)
        raise AssertionError("old queued target was sent to graph ingest")

    identity = {
        "repository": {
            "id": REPOSITORY_ID,
            "full_name": REPO,
            "owner": {"id": OWNER_ID},
        }
    }
    with patch.object(
        I, "_current_repo_identity_for_self_heal", return_value=identity
    ), patch.object(
        I, "_onboard_repo_identity_allowed", return_value=True
    ), patch.object(
        I, "request_main_graph_refresh", side_effect=reenqueue
    ), patch.object(
        I, "self_heal_main_graph", side_effect=forbidden_heal
    ):
        result = I.converge_main_graph_strict(
            lambda *_args: None,
            SimpleNamespace(
                repo_default_branch_head=lambda _repo: (BRANCH, NEW_SHA)
            ),
            REPO,
            BRANCH,
            OLD_SHA,
            REPOSITORY_ID,
            expected_owner_id=OWNER_ID,
        )
    chk(
        result.get("superseded") is True
        and result.get("target_sha") == OLD_SHA
        and result.get("head_sha") == NEW_SHA
        and requested_targets == [(BRANCH, NEW_SHA, True)],
        "strict convergence supersedes a stale queued SHA with current HEAD",
    )
    chk(
        not old_target_writes,
        "strict convergence never writes the old target after HEAD has advanced",
    )

    renamed_repo = "acme/widget-renamed"
    renamed_identity = {
        "repository": {
            "id": REPOSITORY_ID,
            "full_name": renamed_repo,
            "owner": {"id": OWNER_ID},
        }
    }
    rename_requests: list[tuple[str, str, str, bool]] = []
    with patch.object(
        I, "_current_repo_identity_for_self_heal", return_value=renamed_identity
    ), patch.object(
        I, "_onboard_repo_identity_allowed", return_value=True
    ), patch.object(
        I, "_reconcile_repo_identity", return_value={"ok": True}
    ), patch.object(
        I,
        "request_main_graph_refresh",
        side_effect=lambda _db, requested_repo, requested_branch, requested_sha, _rid, **kw: (
            rename_requests.append(
                (requested_repo, requested_branch, requested_sha, kw.get("force") is True)
            )
            or {"queued": True, "queue_epoch": 13}
        ),
    ), patch.object(
        I, "self_heal_main_graph", side_effect=forbidden_heal
    ):
        renamed = I.converge_main_graph_strict(
            lambda *_args: None,
            SimpleNamespace(
                repo_default_branch_head=lambda requested_repo: (
                    BRANCH,
                    NEW_SHA,
                )
            ),
            REPO,
            BRANCH,
            OLD_SHA,
            REPOSITORY_ID,
            expected_owner_id=OWNER_ID,
        )
    chk(
        renamed.get("superseded") is True
        and renamed.get("renamed") is True
        and renamed.get("head_repo") == renamed_repo
        and rename_requests == [(renamed_repo, BRANCH, NEW_SHA, True)],
        "same-owner repository rename migrates and supersedes the stable-id row instead of exhausting retries",
    )

    strict_cold_populates: list[str] = []
    with patch.object(
        I,
        "graph_freshness",
        return_value={
            "head_sha": NEW_SHA,
            "stored_sha": None,
            "behind": True,
            "head_committed_at": None,
        },
    ), patch.object(
        I,
        "ingest_push",
        return_value={"mode": "full", "files": 4, "edges": 3},
    ), patch.object(
        I,
        "_capture_repository_graph_generation",
        return_value={
            "version": "repository-graph-generation-v1",
            "repo": REPO,
            "repository_id": REPOSITORY_ID,
            "activated_at": "2026-07-29T00:00:00+00:00",
            "generation_started_at": None,
            "lifecycle_authoritative": False,
        },
    ), patch.object(
        I, "_restamp_current_repo_identity", return_value={"ok": True}
    ), patch.object(
        I,
        "_server",
        return_value=SimpleNamespace(_quota_result=lambda _stats: None),
    ), patch.object(
        I,
        "_dispatch_cochange_populate",
        side_effect=lambda *_a, **_k: strict_cold_populates.append("clone"),
    ):
        strict_heal = I.self_heal_main_graph(
            lambda *_args: None,
            object(),
            REPO,
            BRANCH,
            expected_repository_id=REPOSITORY_ID,
            expected_owner_id=OWNER_ID,
            strict=True,
        )
    chk(
        strict_heal.get("healed") is True
        and strict_heal.get("cochange_populate", {}).get("deferred")
        == "durable_low_priority_lane_required"
        and not strict_cold_populates,
        "strict cold self-heal persists the structural graph without launching a history clone",
    )

    cold_populates: list[str] = []
    with patch.object(
        I, "_current_repo_identity_for_self_heal", return_value=identity
    ), patch.object(
        I, "_onboard_repo_identity_allowed", return_value=True
    ), patch.object(
        I,
        "self_heal_main_graph",
        return_value={
            "healed": True,
            "stored_sha": None,
            "head_sha": NEW_SHA,
            "cochange_populate": {
                "dispatched": False,
                "deferred": "durable_low_priority_lane_required",
            },
        },
    ), patch.object(
        I, "graph_freshness_at_target",
        return_value={"behind": False, "stored_sha": NEW_SHA},
    ), patch.object(
        I, "_reconcile_repo_identity", return_value={"ok": True}
    ), patch.object(
        I,
        "populate_cochange_async",
        side_effect=lambda *_a, **_k: cold_populates.append("clone"),
    ):
        cold = I.converge_main_graph_strict(
            lambda *_args: None,
            SimpleNamespace(
                repo_default_branch_head=lambda _repo: (BRANCH, NEW_SHA)
            ),
            REPO,
            BRANCH,
            NEW_SHA,
            REPOSITORY_ID,
            expected_owner_id=OWNER_ID,
        )
    chk(
        cold.get("converged") is True
        and cold.get("cochange_populate", {}).get("deferred")
        == "durable_low_priority_lane_required"
        and not cold_populates,
        "strict cold convergence cannot launch an overlapping history clone",
    )


def _static_call_graph_boundary_contract() -> None:
    """Prove transitive separation, not merely the handlers' direct calls."""
    handler_tree = ast.parse(
        open(H.__file__, encoding="utf-8").read(), filename=H.__file__)
    ingest_tree = ast.parse(
        open(I.__file__, encoding="utf-8").read(), filename=I.__file__)
    live_handlers = {
        "_handle_pull_request_event",
        "_handle_push_event",
        "_handle_installation_event",
    }
    heavy = {
        "converge_main_graph_strict",
        "self_heal_main_graph",
        "_execute_push_graph_inline",
        "_reingest_graph",
        "_incremental_ingest",
        "_full_ingest",
        "_full_ingest_with_slot",
        "backfill_repo",
        "_dispatch_cochange_populate",
        "populate_cochange_async",
    }

    functions: dict[str, ast.AST] = {}
    by_name: dict[str, set[str]] = {}
    for module, tree in (
        ("webhook_handlers", handler_tree),
        ("ingest", ingest_tree),
    ):
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            key = f"{module}.{node.name}"
            functions[key] = node
            by_name.setdefault(node.name, set()).add(key)

    graph: dict[str, set[str]] = {key: set() for key in functions}
    for caller, node in functions.items():
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                name = (
                    child.func.id if isinstance(child.func, ast.Name)
                    else child.func.attr
                    if isinstance(child.func, ast.Attribute)
                    else ""
                )
                graph[caller].update(by_name.get(name, ()))
                # Strategy functions are passed explicitly rather than called
                # at this site. Add those reference edges so the proof does
                # not hide behind callback indirection.
                for value in (
                    list(child.args)
                    + [kw.value for kw in child.keywords]
                ):
                    if isinstance(value, ast.Name):
                        graph[caller].update(by_name.get(value.id, ()))

    roots = {
        f"webhook_handlers.{name}" for name in live_handlers
    } | {
        # This root also runs inside the web container after each deploy.  It
        # may inventory/replay bounded PR metadata, but graph convergence must
        # stay a durable request; otherwise adding accounts/repos would move
        # clone/extract CPU straight back onto live ingress at every rollout.
        "ingest.boot_reconcile",
    }
    reachable = set(roots)
    frontier = list(roots)
    while frontier:
        caller = frontier.pop()
        for callee in graph.get(caller, ()):
            if callee not in reachable:
                reachable.add(callee)
                frontier.append(callee)
    violations = sorted(
        key for key in reachable if key.rsplit(".", 1)[-1] in heavy)
    chk(
        not violations,
        "transitive code graph: live webhook and web-boot roots cannot reach clone/extract/history nodes",
    )

    quota_root = "ingest.surface_onboarding_quota_paused_pull_request"
    quota_forbidden = {
        "ingest.replay_onboarding_pull_request",
        "ingest._dispatch_synthetic_pr_replay",
        "webhook_handlers.handle_event",
        "webhook_handlers._handle_pull_request_event",
        "webhook_handlers._pr_fetch_changed",
        "webhook_handlers._pr_pre_brain",
    }
    quota_reachable = {quota_root}
    frontier = [quota_root]
    while frontier:
        caller = frontier.pop()
        for callee in graph.get(caller, ()):
            if callee not in quota_reachable:
                quota_reachable.add(callee)
                frontier.append(callee)
    chk(
        quota_root in functions
        and quota_forbidden <= set(functions)
        and quota_reachable.isdisjoint(quota_forbidden),
        "transitive code graph: quota onboarding surface cannot reach normal replay, Files, or the PR brain",
    )

    allowed_callers = {
        "_full_ingest": {"_reingest_graph"},
        "_full_ingest_with_slot": {"_full_ingest"},
        "_reingest_graph": {"_execute_push_graph_inline"},
        "self_heal_main_graph": {"converge_main_graph_strict"},
        "backfill_repo": {"_onboard_repos"},
    }
    seen: dict[str, set[str]] = {name: set() for name in allowed_callers}
    for node in ast.walk(ingest_tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            name = (
                child.func.id if isinstance(child.func, ast.Name)
                else child.func.attr if isinstance(child.func, ast.Attribute)
                else ""
            )
            if name in seen:
                seen[name].add(node.name)
    chk(
        all(seen[name] <= callers and seen[name]
            for name, callers in allowed_callers.items()),
        "function-level code graph: every heavy edge stays in the isolated convergence chain",
    )
    strict_node = next(
        node for node in ast.walk(ingest_tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "converge_main_graph_strict"
    )
    strict_calls = {
        (
            child.func.id if isinstance(child.func, ast.Name)
            else child.func.attr if isinstance(child.func, ast.Attribute)
            else ""
        )
        for child in ast.walk(strict_node)
        if isinstance(child, ast.Call)
    }
    chk(
        "populate_cochange_async" not in strict_calls
        and "_dispatch_cochange_populate" not in strict_calls,
        "function-level code graph: strict graph root has no history-clone dispatch edge",
    )


def main() -> int:
    _request_helper_contract()
    _wake_only_atomic_receipt_contract()
    _deferred_ingest_contract()
    _live_onboarding_resource_contract()
    _live_push_contract()
    _live_pr_neighbor_offload_contract()
    _planned_onboarding_replay_contract()
    _strict_supersession_contract()
    _static_call_graph_boundary_contract()
    ok = all(checks)
    print("GRAPH REFRESH HOTPATH GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
