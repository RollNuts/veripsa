#!/usr/bin/env python3
"""Automatic stale ``BR-*`` lane self-heal regression gate.

The delete webhook is the primary release path.  This gate locks the two
automatic backstops that repair a legacy/missed delete without asking a user
to acknowledge a branch that no longer exists:

* a destructive reconcile is allowed only after one complete (<100) branch
  inventory page;
* boot reconciliation cleans stale branch lanes before replaying open PRs;
* a hot PR that encounters a stale ``BR-*`` member cleans and recomputes its
  cluster before its check is rendered.

Run: python3 tests/test_branch_lane_selfheal.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import psycopg2  # noqa: E402
import ingest as I  # noqa: E402
import render as R  # noqa: E402
import server as S  # noqa: E402
import webhook as W  # noqa: E402
import webhook_handlers as WH  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402
from cg_schema_contract import (  # noqa: E402
    EXTRACTOR_VERSION,
    SCHEMA_CONTRACT_VERSION,
)
from github_rest_prread import _GitHubPRReadMixin  # noqa: E402
from test_draft_branch_lane_leak import FakeGitHub as BaseFakeGitHub, _pr  # noqa: E402


DB = "veripsa_branchselfheal_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
checks: list[bool] = []


def chk(condition: Any, label: str) -> None:
    passed = bool(condition)
    print(("  [PASS] " if passed else "  [FAIL] ") + label)
    checks.append(passed)


def raises(callable_obj, text: str = "") -> bool:
    try:
        callable_obj()
    except Exception as exc:  # the caller's no-write path owns the concrete exception type
        return text in str(exc)
    return False


class BranchPages(_GitHubPRReadMixin):
    """Tiny Link-aware REST fake for the exact branch inventory method."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    def _api_with_link(self, method: str, url: str):
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("unexpected extra branch page")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class NamesOnlyGitHub:
    def __init__(self, names):
        self.names = names

    def list_repo_branch_names(self, repo: str, limit: int | None = None):
        return list(self.names[:limit] if limit is not None else self.names)


class AppSession:
    """One App connection pinned exactly like one installation event."""

    def __init__(self, owner_id: int):
        seed_live_installation(
            DSN_APP,
            f"postgresql://veripsa_migrator@localhost/{DB}",
            owner_id,
            4242,
        )
        self.conn = psycopg2.connect(DSN_APP)
        self.conn.autocommit = True
        with self.conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(owner_id),))
            self.account = cur.fetchone()[0]

    def __call__(self, sql: str, args=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            try:
                row = cur.fetchone()
            except psycopg2.ProgrammingError:
                return None
            return row[0] if row else None

    def close(self):
        self.conn.close()


def seed_graph(db: AppSession, repo: str, path: str, sha: str, branch: str = "main") -> None:
    graph = {
        "extractor_version": EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": SCHEMA_CONTRACT_VERSION},
        "nodes": [{"id": path, "kind": "file", "path": path, "language": "typescript"}],
        "edges": [],
    }
    db("SELECT core.ingest_graph_with_authority(%s::jsonb,%s,%s,%s)",
       (json.dumps(graph), repo, branch, sha))


def seed_graph_paths(db: AppSession, repo: str, paths: list[str], sha: str) -> None:
    graph = {
        "extractor_version": EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": SCHEMA_CONTRACT_VERSION},
        "nodes": [{"id": path, "kind": "file", "path": path, "language": "typescript"}
                  for path in paths],
        "edges": [],
    }
    db("SELECT core.ingest_graph_with_authority(%s::jsonb,%s,%s,%s)",
       (json.dumps(graph), repo, "main", sha))


def claim_in(db: AppSession, repo: str, change_id: str, path: str, author: str,
             branch: str = "main") -> None:
    db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
       (f"{change_id}:{path}", path, repo, branch, author))


def states(db: AppSession, repo: str) -> dict[str, list[str]]:
    # The production App role intentionally cannot read tables directly.  Read back as the
    # migrator with the same account RLS pin the existing DB gates use.
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (db.account,))
            cur.execute(
                """SELECT change_id, claim_state FROM core.claim
                     WHERE repo=%s ORDER BY change_id, claim_state""",
                (repo,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    out: dict[str, list[str]] = {}
    for change_id, state in rows:
        out.setdefault(change_id, []).append(state)
    return out


class EventGitHub(BaseFakeGitHub):
    """PR/check fake plus a complete live branch inventory."""

    def __init__(self, owner_id: int, *, branches=None, inventory_error: Exception | None = None,
                 head_sha: str = "a" * 40, open_prs=None):
        super().__init__(head_sha=head_sha)
        self.owner_id = owner_id
        self.branches = list(branches or ["main"])
        self.inventory_error = inventory_error
        self.open_prs = list(open_prs or [])
        self.order: list[str] = []
        self.check_writes: list[dict] = []

    def post_check(self, repo, sha, conclusion, title, summary):
        self._check_id += 1
        record = {"id": self._check_id, "sha": sha, "conclusion": conclusion,
                  "title": title, "summary": summary, "name": "Veripsa"}
        self.checks.append(record)
        self.check_writes.append(dict(record))
        return record

    def patch_check(self, repo, cid, conclusion, title, summary):
        for record in self.checks:
            if record["id"] == cid:
                record.update({"conclusion": conclusion, "title": title, "summary": summary})
                self.check_writes.append(dict(record))
                return record
        raise AssertionError(f"check not found: {cid}")

    def list_repo_branch_names(self, repo: str, limit: int | None = None):
        self.order.append("branches")
        if self.inventory_error is not None:
            raise self.inventory_error
        return list(self.branches[:limit] if limit is not None else self.branches)

    def repo_default_branch_name(self, repo: str) -> str:
        self.order.append("default_branch")
        return "main"

    def installation_account_id(self):
        return str(self.owner_id)

    def list_open_pull_requests(self, repo: str, limit: int | None = None):
        self.order.append("open_prs")
        return list(self.open_prs[:limit] if limit is not None else self.open_prs)


class MixedEventGitHub(EventGitHub):
    """Production-shaped PR evidence for a real mixed PR/BR cluster refresh."""

    def __init__(self, owner_id: int, *, pr_objects: dict[int, dict], **kwargs):
        super().__init__(owner_id, **kwargs)
        self.pr_objects = {number: dict(pr) for number, pr in pr_objects.items()}

    def get_pull_request(self, repo: str, number: int) -> dict:
        raw = self.pr_objects[number]
        files = list(self.files_by_pr.get(number, []))
        repo_id = self.owner_id * 10
        base = dict(raw.get("base") or {})
        head = dict(raw.get("head") or {})
        base["repo"] = base.get("repo") or {"id": repo_id, "full_name": repo}
        head["repo"] = head.get("repo") or {"id": repo_id, "full_name": repo}
        return {
            **raw,
            "number": number,
            "state": "open",
            "merged": False,
            "draft": bool(raw.get("draft")),
            "changed_files": len(files),
            "base": base,
            "head": head,
        }

    def list_pr_file_metadata(self, repo: str, number: int, pr_changed_files: int = 0,
                              max_pages: int | None = None) -> dict:
        files = list(self.files_by_pr.get(number, []))
        return {"changed": files, "changed_ranges": {path: [] for path in files},
                "added_paths": [], "conflict_markers": [], "raw_entry_count": len(files)}

    def compare_changed_paths_strict(self, repo: str, base_sha: str, branch: str) -> list[str]:
        return []

    def pr_labels(self, repo: str, number: int, strict: bool = False) -> list[str]:
        return []


def pr_event(repo: str, number: int, owner_id: int, head_sha: str, head_ref: str,
             path_count: int = 1) -> dict:
    payload = _pr("opened", repo, number, f"author{number}", head_sha, head_ref=head_ref)
    payload["repository"]["id"] = owner_id * 10
    payload["repository"]["owner"]["id"] = owner_id
    pr = payload["pull_request"]
    pr["changed_files"] = path_count
    pr["base"]["sha"] = "a" * 40
    pr["base"]["repo"] = {"id": owner_id * 10, "full_name": repo}
    pr["head"]["repo"] = {"id": owner_id * 10, "full_name": repo}
    return payload


def inventory_contract() -> None:
    print("\n-- strict one-page GitHub branch inventory --")
    def import_with_cap(value: str):
        env = os.environ.copy()
        env["VERIPSA_BACKFILL_BRANCH_CAP"] = value
        return subprocess.run(
            [sys.executable, "-c", "import ingest; print(ingest._BACKFILL_BRANCH_CAP)"],
            cwd=os.path.join(ROOT, "github-app"), env=env, capture_output=True, text=True)

    cap_100 = import_with_cap("100")
    rejected = [import_with_cap(value) for value in ("101", "1", "not-an-int")]
    chk(
        cap_100.returncode == 0 and cap_100.stdout.strip().endswith("100"),
        "VERIPSA_BACKFILL_BRANCH_CAP=100 is accepted as the single-page safety wall",
    )
    chk(
        all(run.returncode != 0 and "VERIPSA_BACKFILL_BRANCH_CAP" in run.stderr for run in rejected),
        "branch caps above 100, below 2, or non-integer are rejected at import/startup",
    )

    complete = BranchPages([([{"name": "main"}, {"name": "feat/live"}], "")])
    chk(
        complete.list_repo_branch_names("acme/repo", 100) == ["main", "feat/live"]
        and len(complete.calls) == 1,
        "a complete short first page (<100, no rel=next) is accepted",
    )

    hundred = [{"name": f"branch-{n}"} for n in range(100)]
    sentinel = BranchPages([(hundred, '<https://api.github.test/p2>; rel="next"')])
    chk(
        len(sentinel.list_repo_branch_names("acme/repo", 100)) == 100
        and len(sentinel.calls) == 1,
        "exactly 100 names is returned only as the caller's truncation sentinel; page 2 is never read",
    )

    db_calls: list[tuple] = []
    skipped = I._reconcile_live_branch_claims(
        lambda sql, args=(): db_calls.append((sql, args)),
        NamesOnlyGitHub([f"branch-{n}" for n in range(I._BACKFILL_BRANCH_CAP)]),
        "acme/repo", "main",
    )
    chk(
        skipped.get("skipped") == "branch inventory truncated" and not db_calls,
        "the 100-name sentinel never reaches the destructive DB gate",
    )

    short_next = BranchPages([([{"name": "main"}], '<https://api.github.test/p2>; rel="next"')])
    chk(
        raises(lambda: short_next.list_repo_branch_names("acme/repo", 100), "single-page safety bound"),
        "a short page that advertises rel=next is incomplete and raises instead of releasing",
    )
    api_error = BranchPages([RuntimeError("GitHub unavailable")])
    chk(
        raises(lambda: api_error.list_repo_branch_names("acme/repo", 100), "GitHub unavailable"),
        "an inventory API error propagates to the caller's preserve-all path",
    )
    malformed_page = BranchPages([({"message": "not a list"}, "")])
    malformed_entry = BranchPages([([{"name": "main"}, {}], "")])
    chk(
        raises(lambda: malformed_page.list_repo_branch_names("acme/repo", 100), "non-list")
        and raises(lambda: malformed_entry.list_repo_branch_names("acme/repo", 100), "malformed entry"),
        "malformed pages and branch entries raise; neither can masquerade as an empty inventory",
    )


def ack_uncertainty_contract() -> None:
    print("\n-- branch-inventory uncertainty preserves ACK identity --")
    original = {"repo": "acme/ack-stability", "branch": "main", "changes": [
        {"change_id": "PR-71", "label": "PR-71", "agent": "actor", "verdict": "serialize",
         "paths": ["src/original.ts"], "serialize_behind": ["neighbor PR-70"],
         "collision_points": [{"symbol": "original", "path": "src/original.ts"}]},
    ]}
    filtered_clear = {"repo": "acme/ack-stability", "branch": "main", "changes": [
        {"change_id": "PR-71", "label": "PR-71", "agent": "actor", "verdict": "clear",
         "paths": ["src/original.ts"]},
    ]}
    filtered_changed = {"repo": "acme/ack-stability", "branch": "main", "changes": [
        {"change_id": "PR-71", "label": "PR-71", "agent": "actor", "verdict": "serialize",
         "paths": ["src/filtered.ts"], "serialize_behind": ["different PR-99"],
         "collision_points": [{"symbol": "filtered", "path": "src/filtered.ts"}]},
    ]}
    prior = R.coupling_snapshot(original["changes"][0])
    changed = R.coupling_snapshot(filtered_changed["changes"][0])
    prior_marker = f"<!-- veripsa-ack-snap:{prior} -->"
    changed_marker = f"<!-- veripsa-ack-snap:{changed} -->"

    pending_check = R.branch_inventory_unknown_check("main")
    pending_render = {**pending_check, "comment": R.branch_inventory_unknown_comment_body("main")}
    clear_out = R.apply_pause_ack(
        pending_render, filtered_clear, "PR-71", label_present=True, prior_hash=prior,
        branch="main", prior_confirmed=True, preserve_ack_on_uncertainty=True)
    clear_body = clear_out.get("comment") or ""
    chk(
        clear_out.get("conclusion") == "neutral"
        and "pending" in str(clear_out.get("title") or "").lower()
        and clear_out.get("label_action") is None,
        "a filtered-clear uncertainty stays neutral/pending and never removes the existing ACK label",
    )
    chk(
        clear_out.get("snapshot") == prior
        and R.prior_snapshot_from_comment(clear_body) == prior
        and clear_body.count(prior_marker) == 1,
        "a filtered-clear uncertainty keeps the exact prior ACK binding marker",
    )
    authoritative = R.apply_pause_ack(
        R.render_pr_check(original, "PR-71"), original, "PR-71",
        label_present=True, prior_hash=R.prior_snapshot_from_comment(clear_body),
        branch="main", prior_confirmed=True)
    chk(
        authoritative.get("ack_state") == "acknowledged"
        and authoritative.get("snapshot") == prior
        and authoritative.get("label_action") is None,
        "the next authoritative unchanged render recognizes the same ACK without remove/rebind churn",
    )

    changed_render = R.render_pr_check(filtered_changed, "PR-71")
    changed_render["title"] = pending_check["title"]
    changed_render["summary"] += "\n\n" + R.branch_inventory_unknown_note()
    changed_render["comment"] += "\n\n---\n\n" + R.branch_inventory_unknown_note()
    mismatch = R.apply_pause_ack(
        changed_render, filtered_changed, "PR-71", label_present=True, prior_hash=prior,
        branch="main", prior_confirmed=True, preserve_ack_on_uncertainty=True)
    mismatch_body = mismatch.get("comment") or ""
    chk(
        mismatch.get("conclusion") == "neutral"
        and "acknowledged" not in str(mismatch.get("title") or "").lower()
        and mismatch.get("ack_state") != "acknowledged"
        and mismatch.get("label_action") is None,
        "a filtered-material mismatch stays pending; it neither claims Acknowledged nor removes the label",
    )
    chk(
        prior != changed and mismatch.get("snapshot") == prior
        and R.prior_snapshot_from_comment(mismatch_body) == prior
        and prior_marker in mismatch_body and changed_marker not in mismatch_body,
        "a filtered-material mismatch preserves the prior marker and never binds the speculative snapshot",
    )

    unbound = R.apply_pause_ack(
        changed_render, filtered_changed, "PR-71", label_present=True, prior_hash=None,
        branch="main", prior_confirmed=True, preserve_ack_on_uncertainty=True)
    chk(
        unbound.get("conclusion") == "neutral"
        and unbound.get("ack_state") != "acknowledged"
        and unbound.get("snapshot") != changed
        and R.prior_snapshot_from_comment(unbound.get("comment")) != changed
        and unbound.get("label_action") is None,
        "a label with confirmed no prior binding cannot auto-bind to filtered material under uncertainty",
    )

    class LabelledGitHub:
        def pr_labels(self, repo: str, pr: int, strict: bool = False) -> list[str]:
            return [R.ACK_LABEL]

    legacy_calls: list[dict] = []
    original_apply = WH.apply_pause_ack
    original_prior = WH._prior_ack_snapshot

    def legacy_apply(rendered, impact, change_ref, *, label_present, prior_hash,
                     branch="main", is_fork=False, prior_confirmed=True):
        legacy_calls.append({"change_ref": change_ref, "prior_hash": prior_hash})
        return {**rendered, "ack_state": "legacy_called", "snapshot": prior_hash or "",
                "label_action": None}

    result = {
        "check": {"conclusion": "neutral", "title": pending_check["title"],
                  "summary": pending_check["summary"]},
        "comment": changed_render["comment"],
        "branch_inventory_unknown": True,
        "_branch_filtered_impact": filtered_changed,
    }
    f = {"repo": "acme/ack-stability", "pr": 71, "base": "main", "is_fork": False,
         "action": "synchronize", "prj": {"labels": [{"name": R.ACK_LABEL}]},
         "is_ack_label_event": False}
    WH.apply_pause_ack = legacy_apply
    WH._prior_ack_snapshot = lambda *_args, **_kwargs: (prior, True)
    try:
        WH._pr_apply_pause_ack_overlay(None, LabelledGitHub(), f, result)
    finally:
        WH.apply_pause_ack = original_apply
        WH._prior_ack_snapshot = original_prior
    chk(
        legacy_calls == [{"change_ref": "PR-71", "prior_hash": prior}]
        and result.get("ack_state") == "legacy_called",
        "an old monkeypatched apply_pause_ack signature is invoked without an unexpected-keyword TypeError",
    )


def ack_overlay_without_filtered_impact_contract() -> None:
    print("\n-- missing speculative impact never falls back to ghost DB state --")
    prior = "a1b2c3d4e5f6"
    marker = f"<!-- veripsa-ack-snap:{prior} -->"
    own_marker = "<!-- veripsa:PR-72 -->"

    class OverlayGitHub:
        def __init__(self, labelled: bool):
            self.labelled = labelled
            self.removed: list[str] = []

        def pr_labels(self, repo: str, pr: int, strict: bool = False) -> list[str]:
            return [R.ACK_LABEL] if self.labelled else []

        def list_issue_comments(self, repo: str, pr: int) -> list[dict]:
            return [{"body": own_marker + "\n" + marker + "\nprior verdict"}]

        def remove_label(self, repo: str, pr: int, label: str) -> None:
            self.removed.append(label)

    db_reads: list[str] = []

    def forbidden_db(sql: str, args=()):
        db_reads.append(sql)
        raise AssertionError("actual DB impact must not be read while branch authority is unknown")

    pending = R.branch_inventory_unknown_check("main")
    f = {"repo": "acme/no-filtered-impact", "pr": 72, "base": "main", "is_fork": False,
         "action": "synchronize", "prj": {"labels": []}, "is_ack_label_event": False}
    unlabeled = {"check": dict(pending), "comment": R.branch_inventory_unknown_comment_body("main"),
                 "branch_inventory_unknown": True}
    WH._pr_apply_pause_ack_overlay(forbidden_db, OverlayGitHub(False), f, unlabeled)
    chk(
        db_reads == [] and unlabeled["check"].get("conclusion") == "neutral"
        and unlabeled.get("ack_state") in (None, "not_material")
        and "paused" not in str(unlabeled["check"].get("title") or "").lower(),
        "unknown branch + no filtered view + no label stays neutral without rereading ghost-bearing DB impact",
    )

    labelled_gh = OverlayGitHub(True)
    labelled = {"check": dict(pending), "comment": R.branch_inventory_unknown_comment_body("main"),
                "branch_inventory_unknown": True}
    WH._pr_apply_pause_ack_overlay(forbidden_db, labelled_gh, f, labelled)
    body = labelled.get("comment") or ""
    chk(
        db_reads == [] and labelled["check"].get("conclusion") == "neutral"
        and labelled.get("ack_state") == "ack_verification_pending"
        and labelled.get("coupling_snapshot") == prior
        and R.prior_snapshot_from_comment(body) == prior
        and body.count(marker) == 1 and labelled_gh.removed == [],
        "the same no-view fallback preserves an existing label and its exact prior marker without DB reread",
    )


def ack_clear_rewrite_contract() -> None:
    print("\n-- authoritative Clear preserves the last ACK identity --")

    def material(pr: int, path: str, partner: int, head_sha: str = "") -> dict:
        change = {"change_id": f"PR-{pr}", "label": f"PR-{pr}", "agent": f"author{pr}",
                  "verdict": "serialize", "paths": [path],
                  "serialize_behind": [f"other PR-{partner}"],
                  "collision_points": [{"symbol": "touch", "path": path}]}
        if head_sha:
            change["head_sha"] = head_sha
        return {"repo": "acme/ack-clear", "branch": "main", "changes": [change]}

    class AckClearGitHub:
        def __init__(self, pr: int, path: str, head_sha: str, prior: str):
            self.pr, self.path, self.head_sha = pr, path, head_sha
            self.body = (f"<!-- veripsa:PR-{pr} -->\n"
                         f"<!-- veripsa-ack-snap:{prior} -->\nold coupled verdict")
            self.removed: list[str] = []
            self.checks: list[dict] = []

        def list_issue_comments(self, repo: str, pr: int) -> list[dict]:
            return [{"id": 1, "number": pr, "body": self.body, "user": {"type": "Bot"}}]

        def patch_comment_if_exists(self, repo: str, pr: int, marker: str, body) -> bool:
            if marker not in self.body:
                return False
            self.body = body(self.body) if callable(body) else body
            return True

        def upsert_comment(self, repo: str, pr: int, marker: str, body: str) -> dict:
            self.body = body
            return {"id": 1, "body": body}

        def upsert_check(self, repo, sha, conclusion, title, summary, **_kwargs):
            row = {"id": len(self.checks) + 1, "sha": sha, "conclusion": conclusion,
                   "title": title, "summary": summary}
            self.checks.append(row)
            return row

        def repo_default_branch_head(self, repo: str) -> tuple[str, str]:
            return "main", "a" * 40

        def pull_request_head(self, repo: str, pr: int) -> str:
            return self.head_sha

        def pr_labels(self, repo: str, pr: int, strict: bool = False) -> list[str]:
            return [R.ACK_LABEL]

        def remove_label(self, repo: str, pr: int, label: str) -> None:
            self.removed.append(label)

        def get_pull_request(self, repo: str, pr: int) -> dict:
            repo_meta = {"id": 77, "full_name": repo}
            return {"number": pr, "state": "open", "merged": False, "changed_files": 1,
                    "head": {"sha": self.head_sha, "repo": repo_meta},
                    "base": {"ref": "main", "sha": "a" * 40, "repo": repo_meta}}

        def list_pr_file_metadata(self, repo: str, pr: int, pr_changed_files: int = 0,
                                  max_pages: int | None = None) -> dict:
            return {"changed": [self.path], "changed_ranges": {self.path: []},
                    "added_paths": [], "conflict_markers": [], "raw_entry_count": 1}

        def compare_changed_paths_strict(self, repo: str, base_sha: str, branch: str) -> list[str]:
            return []

    # ACTING path: a synchronize becomes authoritative Clear. Its clear rewrite must retain the invisible marker;
    # the next materially different coupling then proves the old label stale and removes it instead of auto-binding.
    acting_old = material(75, "src/old.ts", 70)
    acting_prior = R.coupling_snapshot(acting_old["changes"][0])
    acting_gh = AckClearGitHub(75, "src/new.ts", "b" * 40, acting_prior)
    clear = {"repo": "acme/ack-clear", "branch": "main", "changes": [
        {"change_id": "PR-75", "label": "PR-75", "verdict": "clear", "paths": []},
    ]}
    clear_render = R.render_pr_check(clear, "PR-75")
    f = {"repo": "acme/ack-clear", "pr": 75, "head_sha": "b" * 40, "is_fork": False,
         "action": "synchronize", "default_branch": "main", "is_ack_label_event": False,
         "base": "main", "prj": {"labels": [{"name": R.ACK_LABEL}]}}
    WH._pr_post_check_and_comment(
        acting_gh, f,
        {"check": {k: clear_render[k] for k in ("conclusion", "title", "summary")}, "comment": None})
    acting_cleared = acting_gh.body
    acting_new = material(75, "src/new.ts", 99)
    acting_new_render = R.render_pr_check(acting_new, "PR-75")

    def acting_db(sql: str, args=()):
        return acting_new if "main_impact_surface" in sql else None

    acting_result = {
        "check": {k: acting_new_render[k] for k in ("conclusion", "title", "summary")},
        "comment": acting_new_render["comment"],
    }
    WH._pr_apply_pause_ack_overlay(acting_db, acting_gh, f, acting_result)
    chk(
        R.prior_snapshot_from_comment(acting_cleared) == acting_prior
        and "cleared" in acting_cleared.lower(),
        "acting authoritative-Clear rewrite keeps the exact prior ACK marker while replacing stale visible prose",
    )
    chk(
        acting_result.get("ack_state") == "stale_reack"
        and acting_result["check"].get("conclusion") == "action_required"
        and acting_gh.removed == [R.ACK_LABEL],
        "a later different acting coupling sees the retained marker, re-pauses, and removes the stale label",
    )

    # NEIGHBOR clear-reset uses the same marker-preserving rewrite. Drive its later material refresh through the
    # evidence-complete neighbor overlay too, proving the stale-label removal contract is symmetric.
    neighbor_old = material(76, "src/neighbor-old.ts", 70)
    neighbor_prior = R.coupling_snapshot(neighbor_old["changes"][0])
    neighbor_gh = AckClearGitHub(76, "src/neighbor-new.ts", "c" * 40, neighbor_prior)
    clear_row = {"agent": "PR-76", "change": "PR-76", "conclusion": "success",
                 "title": "Veripsa — Clear", "summary": "Clear", "comment": None, "clear_reset": True}
    cleared_count = WH._post_refreshes(neighbor_gh, "acme/ack-clear", [clear_row], db=None, branch="main")
    neighbor_cleared = neighbor_gh.body
    neighbor_new = material(76, "src/neighbor-new.ts", 98, head_sha="c" * 40)
    neighbor_refreshes = W._refresh_changes(neighbor_new)

    def neighbor_db(sql: str, args=()):
        if "main_impact_surface" in sql:
            return neighbor_new
        if "co_change_partners_with_authority" in sql:
            return []
        if "account_coverage_surface" in sql:
            return {}
        return None

    refreshed_count = WH._post_refreshes(
        neighbor_gh, "acme/ack-clear", neighbor_refreshes, db=neighbor_db, branch="main")
    chk(
        cleared_count == 1 and R.prior_snapshot_from_comment(neighbor_cleared) == neighbor_prior
        and "cleared" in neighbor_cleared.lower(),
        "neighbor authoritative-Clear reset retains the exact ACK marker through its existing-comment callback",
    )
    chk(
        refreshed_count == 1 and neighbor_gh.removed == [R.ACK_LABEL]
        and "re-paused" in neighbor_gh.body.lower(),
        "a different neighbor coupling consumes that marker as stale_reack and removes the old label",
    )


def synthetic_default_branch_authority_contract() -> None:
    print("\n-- synthetic backfill cannot guess release authority --")

    class BackfillGitHub:
        def __init__(self, strict_branch: str | None):
            self.strict_branch = strict_branch

        def repo_default_branch_name(self, repo: str) -> str:
            if self.strict_branch is None:
                raise RuntimeError("strict default branch unavailable")
            return self.strict_branch

        def repo_default_branch_head(self, repo: str) -> tuple[str, str]:
            return "main", "a" * 40

        def list_open_pull_requests(self, repo: str, limit: int | None = None) -> list[dict]:
            branch = self.strict_branch or "main"
            repo_meta = {"id": 88001, "full_name": repo}
            return [{"number": 73, "state": "open", "merged": False, "changed_files": 1,
                     "base": {"ref": branch, "sha": "a" * 40, "repo": repo_meta},
                     "head": {"ref": "feat/backfill", "sha": "b" * 40, "repo": repo_meta},
                     "user": {"login": "backfill-author", "type": "User"}}]

    captured: list[dict] = []
    original_handle_event = S.handle_event
    S.handle_event = lambda _event, payload, _db, _gh: (captured.append(payload) or {"captured": True})
    try:
        db = lambda _sql, _args=(): False
        I.backfill_open_prs(db, BackfillGitHub(None), "acme/legacy-guess")
        I.backfill_open_prs(db, BackfillGitHub("trunk"), "acme/strict-default")
    finally:
        S.handle_event = original_handle_event

    guessed = captured[0] if len(captured) > 0 else {}
    strict = captured[1] if len(captured) > 1 else {}
    chk(
        guessed.get("repository", {}).get("default_branch") == "main"
        and guessed.get("_veripsa_default_branch_authoritative") is False,
        "strict-read failure may conservatively replay on legacy main but marks that coordinate non-authoritative",
    )
    chk(
        strict.get("repository", {}).get("default_branch") == "trunk"
        and strict.get("_veripsa_default_branch_authoritative") is True,
        "a successful strict metadata read marks only its exact default branch authoritative",
    )

    release_calls: list[tuple[str, str]] = []
    originals = {
        "_pr_eligibility": WH._pr_eligibility,
        "request_main_graph_refresh_wake_only": WH.request_main_graph_refresh_wake_only,
        "_pr_fetch_changed": WH._pr_fetch_changed,
        "_pr_files_snapshot_is_current": WH._pr_files_snapshot_is_current,
        "_pr_pre_brain": WH._pr_pre_brain,
        "_pr_outcome_signals": WH._pr_outcome_signals,
        "_pr_build_event": WH._pr_build_event,
        "handle_pull_request": WH.handle_pull_request,
        "_reconcile_live_branch_claims": WH._reconcile_live_branch_claims,
        "_pr_quota_paused_result": WH._pr_quota_paused_result,
        "_pr_apply_pause_ack_overlay": WH._pr_apply_pause_ack_overlay,
        "_pr_post_check_and_comment": WH._pr_post_check_and_comment,
        "_pr_record_landing": WH._pr_record_landing,
    }

    def eligibility(payload: dict, _db):
        repo = payload["repository"]["full_name"]
        branch = payload["repository"]["default_branch"]
        prj = payload["pull_request"]
        return None, {"should_analyze": True, "repo": repo, "default_branch": branch,
                      "author": "backfill-author", "pr": payload["number"], "action": "synchronize",
                      "base": branch, "is_ack_label_event": False, "prj": prj,
                      "base_sha": prj["base"].get("sha"),
                      "repository_id": payload["repository"].get("id"),
                      "is_fork": False, "head_sha": prj["head"]["sha"], "trace_id": ""}

    def brain(_db, _event, _author, act_for=False, reconcile_branch_claims=None):
        try:
            reconciled = reconcile_branch_claims() if callable(reconcile_branch_claims) else None
            skipped = None
        except Exception as exc:
            reconciled, skipped = None, str(exc)
        return {"check": {"conclusion": "success", "title": "Veripsa", "summary": "ok"},
                "comment": None, "refreshed": [], "reconciled": reconciled, "skipped": skipped}

    WH._pr_eligibility = eligibility
    WH.request_main_graph_refresh_wake_only = lambda *_args, **_kwargs: {
        "healed": False, "queued": True, "reason": "refresh queued",
        "head_sha": "a" * 40,
    }
    WH._pr_fetch_changed = lambda *_args, **_kwargs: (["src/a.ts"], {}, [], [], None, 1, 1)
    WH._pr_files_snapshot_is_current = lambda *_args, **_kwargs: True
    WH._pr_pre_brain = lambda _db, _gh, _f, changed, ranges, added: (changed, ranges, added, False, {}, [])
    WH._pr_outcome_signals = lambda *_args, **_kwargs: (False, "unknown", False)
    WH._pr_build_event = lambda *_args, **_kwargs: {"action": "synchronize"}
    WH.handle_pull_request = brain
    WH._reconcile_live_branch_claims = lambda _db, _gh, repo, branch: (
        release_calls.append((repo, branch)) or {"reconciled": True})
    WH._pr_quota_paused_result = lambda *_args, **_kwargs: None
    WH._pr_apply_pause_ack_overlay = lambda *_args, **_kwargs: None
    WH._pr_post_check_and_comment = lambda *_args, **_kwargs: None
    WH._pr_record_landing = lambda *_args, **_kwargs: None
    try:
        guessed_result = WH._handle_pull_request_event("pull_request", guessed, lambda *_args: None, object())
        strict_result = WH._handle_pull_request_event("pull_request", strict, lambda *_args: None, object())
    finally:
        for name, work in originals.items():
            setattr(WH, name, work)
    chk(
        "authoritative default-branch metadata unavailable" in str(guessed_result.get("skipped") or "")
        and release_calls == [("acme/strict-default", "trunk")]
        and strict_result.get("reconciled", {}).get("reconciled") is True,
        "the hot reconcile callback performs no release for a legacy guess and runs only for strict authority",
    )


def synthetic_replay_authority_contract() -> None:
    print("\n-- every synthetic replay preserves default-branch authority --")
    captured: list[tuple[str, str, bool]] = []
    merge_group_results: list[dict] = []
    original_rerun_prs = WH._rerun_prs
    original_reserve = WH.reserve_branch_lanes
    original_branch_entries = WH._branch_push_pr_entries

    def record_rerun(entries, repo, default_branch, db, gh, log_label, trace_id="",
                     suppress_neighbor_refresh=True, default_branch_authoritative=False):
        captured.append((log_label, default_branch, bool(default_branch_authoritative)))
        return ["PR-1"], False

    class CheckGitHub:
        def upsert_check(self, repo, sha, conclusion, title, summary):
            return {"id": 1}

    def repository(branch: str | None) -> dict:
        out = {"full_name": "acme/synthetic-replays"}
        if branch is not None:
            out["default_branch"] = branch
        return out

    WH._rerun_prs = record_rerun
    WH.reserve_branch_lanes = lambda *_args, **_kwargs: {"reserved": 1, "change_id": "BR-feat/replay"}
    WH._branch_push_pr_entries = lambda _gh, _repo, _branch, default, **_kwargs: [(1, default)]
    try:
        for branch in (None, "trunk"):
            default = branch or "main"
            sparse_pr = {"number": 1, "base": {"ref": default}}
            WH._handle_check_event(
                "check_suite",
                {"action": "rerequested", "repository": repository(branch),
                 "check_suite": {"head_sha": "a" * 40, "pull_requests": [sparse_pr]}},
                lambda *_args: {}, CheckGitHub())
            WH._handle_check_event(
                "check_run",
                {"action": "rerequested", "repository": repository(branch),
                 "check_run": {"head_sha": "b" * 40,
                               "check_suite": {"pull_requests": [sparse_pr]}}},
                lambda *_args: {}, CheckGitHub())
            merge_group_results.append(WH._handle_merge_group_event(
                {"action": "checks_requested", "repository": repository(branch),
                 "merge_group": {"head_sha": "c" * 40, "base_ref": f"refs/heads/{default}",
                                 "head_ref": f"gh-readonly-queue/{default}/pr-1-deadbeef"}},
                lambda sql, _args=(): (
                    {"repo": "acme/synthetic-replays", "branch": default, "changes": []}
                    if "main_impact_surface" in sql else None),
                CheckGitHub()))
            WH._handle_push_event(
                {"ref": "refs/heads/feat/replay", "after": "d" * 40,
                 "repository": repository(branch)},
                lambda *_args: None, object())
    finally:
        WH._rerun_prs = original_rerun_prs
        WH.reserve_branch_lanes = original_reserve
        WH._branch_push_pr_entries = original_branch_entries

    by_source: dict[str, list[tuple[str, bool]]] = {}
    for label, branch, authoritative in captured:
        by_source.setdefault(label, []).append((branch, authoritative))
    expected = [("main", False), ("trunk", True)]
    chk(
        all(by_source.get(label) == expected for label in
            ("check_suite rerequest", "check_run rerequest", "merge_group", "branch push")),
        "check_suite/check_run/merge_group/branch-push mark guesses false and concrete signed branches true",
    )
    chk(
        all(result.get("verdict") == "unknown" and result.get("conclusion") == "neutral"
            and result.get("rerun_capped") is False for result in merge_group_results),
        "an old merge-group replay seam remains callable but cannot authorize Clear without exact-head proof",
    )

    # Historical injected replay builders accepted the original five positional parameters. _rerun_prs must keep
    # calling that seam while stamping the authority bit on its returned payload before live-handler re-entry.
    replay_payloads: list[dict] = []
    old_builder_calls: list[tuple[str, int]] = []
    original_builder = WH._rerun_replay
    original_handle_event = WH.handle_event

    def old_builder(full, repo, default_branch, full_base, number):
        old_builder_calls.append((default_branch, number))
        return {"action": "synchronize", "number": number,
                "repository": {"full_name": repo, "default_branch": default_branch},
                "pull_request": {"base": {"ref": full_base}, "head": {}, "user": {}}}

    class ReplayGitHub:
        def get_pull_request(self, repo: str, number: int) -> dict:
            return {"number": number, "base": {"ref": "trunk"}, "head": {}, "user": {}}

    WH._rerun_replay = old_builder
    WH.handle_event = lambda _event, payload, _db, _gh: (replay_payloads.append(payload) or {})
    try:
        WH._rerun_prs([(5, "trunk")], "acme/legacy-replay", "trunk", None, ReplayGitHub(),
                      "legacy replay", default_branch_authoritative=False)
        WH._rerun_prs([(6, "trunk")], "acme/legacy-replay", "trunk", None, ReplayGitHub(),
                      "legacy replay", default_branch_authoritative=True)
    finally:
        WH._rerun_replay = original_builder
        WH.handle_event = original_handle_event
    chk(
        old_builder_calls == [("trunk", 5), ("trunk", 6)]
        and [payload.get("_veripsa_default_branch_authoritative") for payload in replay_payloads]
        == [False, True],
        "the old five-argument replay-builder seam remains callable and receives an authority-stamped payload",
    )


def db_gate_contract() -> None:
    print("\n-- App-only DB branch reconcile --")
    repo = "acme/branch-db-selfheal"
    a = AppSession(93001)
    b = AppSession(93002)
    try:
        claim_in(a, repo, "BR-feat/live", "src/live.ts", "a_live")
        claim_in(a, repo, "BR-feat/stale", "src/shared.ts", "a_stale")
        claim_in(a, repo, "PR-wait", "src/shared.ts", "a_wait")
        claim_in(a, repo, "PR-keep", "src/keep.ts", "a_keep")
        # Same repo/change/path in another installation: the A reconcile must not see or release it.
        claim_in(b, repo, "BR-feat/stale", "src/shared.ts", "b_stale")

        before = states(a, repo)
        result = I._reconcile_live_branch_claims(
            a, NamesOnlyGitHub(["main", "feat/live"]), repo, "main")
        after = states(a, repo)
        other = states(b, repo)

        chk(
            before.get("BR-feat/live") == ["active"]
            and after.get("BR-feat/live") == ["active"],
            "a BR lane whose branch is still live is preserved",
        )
        chk(
            after.get("BR-feat/stale") == ["released"]
            and result.get("released_changes") == ["BR-feat/stale"],
            "only the absent BR lane is released",
        )
        chk(
            before.get("PR-wait") == ["waiting"] and after.get("PR-wait") == ["active"],
            "releasing the stale holder promotes the PR waiter immediately",
        )
        chk(
            after.get("PR-keep") == ["active"]
            and all(not cid.startswith("PR-") for cid in result.get("released_changes", [])),
            "the branch gate never releases PR-* changes",
        )
        chk(
            other.get("BR-feat/stale") == ["active"],
            "tenant pinning isolates the same repo/change/path in another installation",
        )
    finally:
        a.close()
        b.close()


def hot_pr_contract() -> None:
    print("\n-- hot PR cluster self-heal --")
    repo = "acme/hot-branch-selfheal"
    owner = 93101
    path = "src/hot.ts"
    main_sha = "a" * 40
    head_sha = "b" * 40
    seed = AppSession(owner)
    try:
        seed_graph(seed, repo, path, main_sha)
        claim_in(seed, repo, "BR-feat/deleted", path, "ghost_holder")
    finally:
        seed.close()

    gh = EventGitHub(owner, branches=["main", "feat/current"], head_sha=main_sha)
    gh.files_by_pr = {41: [path]}
    # The live processor intentionally returns None; customer-visible truth is the GitHub post it records.
    S.make_db_processor(DSN_APP)(
        "pull_request", pr_event(repo, 41, owner, head_sha, "feat/current"), None, gh)

    read = AppSession(owner)
    try:
        live = states(read, repo)
    finally:
        read.close()
    conclusion = gh.checks[-1].get("conclusion") if gh.checks else None
    comment = "\n".join(str(item.get("body") or "") for item in gh.comments).lower()
    chk(
        live.get("BR-feat/deleted") == ["released"]
        and live.get("PR-41") == ["active"],
        "a stale BR member in the acting PR's cluster is released and the PR is promoted in the same event",
    )
    chk(
        conclusion != "action_required"
        and "veripsa-ack" not in comment
        and "acknowledge" not in comment,
        "the cluster is recomputed before rendering: no ghost-driven action_required or ACK instruction is posted",
    )


def inventory_failure_contract() -> None:
    print("\n-- inventory uncertainty preserves coupling --")
    repo = "acme/hot-branch-inventory-error"
    owner = 93102
    path = "src/fail-closed.ts"
    main_sha = "a" * 40
    seed = AppSession(owner)
    try:
        seed_graph(seed, repo, path, main_sha)
        claim_in(seed, repo, "BR-feat/uncertain", path, "uncertain_holder")
    finally:
        seed.close()

    gh = EventGitHub(owner, inventory_error=RuntimeError("branch inventory timeout"), head_sha=main_sha)
    gh.files_by_pr = {42: [path]}
    refresh_inputs: list[list] = []
    deferred_refreshes: list[list] = []
    original_post_refreshes = WH._post_refreshes
    original_overlay = WH._pr_apply_pause_ack_overlay

    def record_post_refreshes(client, refresh_repo, refreshes, *args, **kwargs):
        refresh_inputs.append(list(refreshes or []))
        return original_post_refreshes(client, refresh_repo, refreshes, *args, **kwargs)

    def capture_overlay(db, client, fields, result):
        deferred_refreshes.append(list(result.get("refreshed") or []))
        return original_overlay(db, client, fields, result)

    WH._post_refreshes = record_post_refreshes
    WH._pr_apply_pause_ack_overlay = capture_overlay
    try:
        S.make_db_processor(DSN_APP)(
            "pull_request", pr_event(repo, 42, owner, "c" * 40, "feat/current"), None, gh)
    finally:
        WH._post_refreshes = original_post_refreshes
        WH._pr_apply_pause_ack_overlay = original_overlay

    isolated_progress = original_post_refreshes(
        gh, repo, deferred_refreshes[0] if deferred_refreshes else [],
        db=None, branch="main", return_progress=True)

    read = AppSession(owner)
    try:
        live = states(read, repo)
    finally:
        read.close()
    chk(
        live.get("BR-feat/uncertain") == ["active"]
        and live.get("PR-42") == ["waiting"],
        "an inventory failure is savepoint-isolated and preserves the existing BR/PR coupling",
    )
    posted = gh.checks[-1] if gh.checks else {}
    title = str(posted.get("title") or "")
    comment = "\n".join(str(item.get("body") or "") for item in gh.comments)
    visible = "\n".join((title, str(posted.get("summary") or ""), comment)).lower()
    chk(
        posted.get("conclusion") == "neutral" and "retry" in title.lower(),
        "unverified branch truth posts a neutral check whose title says Veripsa is retrying",
    )
    chk(
        "retry automatically" in visible and "no acknowledgement" in visible
        and "veripsa-ack" not in visible,
        "the visible note promises automatic retry, requires no acknowledgement, and never instructs veripsa-ack",
    )
    chk(
        len(gh.check_writes) == 1 and refresh_inputs == []
        and deferred_refreshes == [[]]
        and isolated_progress.get("processed") == 0
        and isolated_progress.get("posted") == 0,
        "inventory uncertainty posts only the acting check; the webhook invokes no neighbor seam and the "
        "isolated convergence slice has zero neighbor work",
    )


def mixed_inventory_failure_contract() -> None:
    print("\n-- mixed PR/PR/BR cluster keeps verified materiality --")
    repo = "acme/hot-branch-mixed-inventory-error"
    owner = 93103
    path = "src/mixed.ts"
    main_sha = "a" * 40
    neighbor_sha = "d" * 40
    acting_sha = "e" * 40
    neighbor_pr, acting_pr = 61, 62
    repo_id = owner * 10

    neighbor = {
        "number": neighbor_pr,
        "base": {"ref": "main", "sha": main_sha,
                 "repo": {"id": repo_id, "full_name": repo}},
        "head": {"ref": "feat/neighbor", "sha": neighbor_sha,
                 "repo": {"id": repo_id, "full_name": repo}},
        "user": {"login": "neighbor_author", "type": "User"},
        "changed_files": 1,
    }
    payload = pr_event(repo, acting_pr, owner, acting_sha, "feat/acting")
    acting = dict(payload["pull_request"])

    seed = AppSession(owner)
    try:
        seed_graph(seed, repo, path, main_sha)
        # Queue order deliberately puts the ghost between two real PRs on one lane. The speculative
        # PR-only read must remove only that middle BR row, not erase the real PR↔PR collision.
        claim_in(seed, repo, f"PR-{neighbor_pr}", path, "neighbor_author")
        seed("SELECT core.set_change_head_sha_with_authority(%s,%s,%s,%s)",
             (f"PR-{neighbor_pr}", repo, "main", neighbor_sha))
        claim_in(seed, repo, "BR-feat/uncertain-mixed", path, "ghost_actor")
    finally:
        seed.close()

    gh = MixedEventGitHub(
        owner,
        pr_objects={neighbor_pr: neighbor, acting_pr: acting},
        inventory_error=RuntimeError("branch inventory timeout"),
        head_sha=main_sha,
    )
    gh.files_by_pr = {neighbor_pr: [path], acting_pr: [path]}
    # Seed an existing neighbor surface so the webhook boundary proves both kinds of forbidden synchronous
    # mutation: it may neither create a second surface nor patch this stale one while holding the event lock.
    gh.upsert_check(repo, neighbor_sha, "success", "Veripsa — prior", "prior neighbor surface")
    gh.upsert_comment(
        repo, neighbor_pr, f"<!-- veripsa:PR-{neighbor_pr} -->",
        f"<!-- veripsa:PR-{neighbor_pr} -->\nprior neighbor surface")
    seeded_neighbor_check = next(check for check in gh.checks if check.get("sha") == neighbor_sha)
    seeded_neighbor_comment = next(item for item in gh.comments if item.get("number") == neighbor_pr)
    neighbor_check_before_event = dict(seeded_neighbor_check)
    neighbor_body_before_event = seeded_neighbor_comment["body"]
    neighbor_check_writes_before_event = sum(
        1 for write in gh.check_writes if write.get("id") == seeded_neighbor_check["id"])

    # Capture the handler's exact speculative PR-only evidence before its internal-only fields are removed from
    # the compact event result. The actual overlay remains production code and still renders the acting surface.
    captured_turns: list[dict] = []
    original_overlay = WH._pr_apply_pause_ack_overlay

    def capture_overlay(db, client, fields, result):
        captured_turns.append({
            "refreshes": json.loads(json.dumps(result.get("refreshed") or [])),
            "impact": json.loads(json.dumps(result.get("_branch_filtered_impact") or {})),
            "unknown_changes": list(result.get("_branch_unknown_changes") or []),
        })
        return original_overlay(db, client, fields, result)

    WH._pr_apply_pause_ack_overlay = capture_overlay
    try:
        S.make_db_processor(DSN_APP)("pull_request", payload, None, gh)
    finally:
        WH._pr_apply_pause_ack_overlay = original_overlay

    neighbor_after_event = next(check for check in gh.checks if check.get("sha") == neighbor_sha)
    neighbor_comment_after_event = next(item for item in gh.comments if item.get("number") == neighbor_pr)
    event_neighbor_unchanged = (
        neighbor_after_event == neighbor_check_before_event
        and neighbor_comment_after_event["body"] == neighbor_body_before_event
        and sum(1 for write in gh.check_writes if write.get("id") == seeded_neighbor_check["id"])
        == neighbor_check_writes_before_event
        and len([check for check in gh.checks if check.get("sha") == neighbor_sha]) == 1
        and len([item for item in gh.comments if item.get("number") == neighbor_pr]) == 1
    )

    read = AppSession(owner)
    try:
        live = states(read, repo)
        call = captured_turns[0] if captured_turns else {}
        worker_progress = WH._post_refreshes(
            gh, repo, call.get("refreshes") or [], db=read, branch="main",
            impact_override=call.get("impact"),
            branch_inventory_unknown_changes=call.get("unknown_changes") or [],
            return_progress=True,
        )
    finally:
        read.close()

    pr_only_impact = call.get("impact") if isinstance(call.get("impact"), dict) else {}
    pr_only_changes = [change for change in (pr_only_impact.get("changes") or [])
                       if isinstance(change, dict)]
    pr_only_ids = {change.get("change_id") for change in pr_only_changes}
    acting_me = next((change for change in pr_only_changes
                      if change.get("change_id") == f"PR-{acting_pr}"), {})
    actor_check = next((check for check in reversed(gh.checks)
                        if check.get("sha") == acting_sha), {})
    neighbor_check = next((check for check in reversed(gh.checks)
                           if check.get("sha") == neighbor_sha), {})
    actor_body = "\n".join(str(item.get("body") or "") for item in gh.comments
                           if item.get("number") == acting_pr)
    neighbor_body = "\n".join(str(item.get("body") or "") for item in gh.comments
                              if item.get("number") == neighbor_pr)
    expected_snapshot = R.coupling_snapshot(acting_me)
    observed_snapshot = R.prior_snapshot_from_comment(actor_body)
    partner_refs = R._ack_partner_refs(acting_me)

    chk(
        live.get("BR-feat/uncertain-mixed") == ["waiting"]
        and live.get(f"PR-{neighbor_pr}") == ["active"]
        and live.get(f"PR-{acting_pr}") == ["waiting"],
        "the failed inventory preserves the real DB BR row and both PR lane states",
    )
    chk(
        pr_only_ids == {f"PR-{neighbor_pr}", f"PR-{acting_pr}"}
        and all(not str(change_id).startswith("BR-") for change_id in pr_only_ids),
        "the speculative impact supplied to pause/refresh is PR-only while authoritative DB state stays intact",
    )
    chk(
        event_neighbor_unchanged,
        "the mixed-cluster webhook performs zero synchronous GitHub mutation on the existing neighbor surface",
    )
    chk(
        actor_check.get("conclusion") == "action_required"
        and neighbor_check.get("conclusion") == "neutral",
        "the acting verdict stays action_required and the isolated convergence slice posts the genuine neighbor warn",
    )
    chk(
        bool(expected_snapshot) and observed_snapshot == expected_snapshot
        and partner_refs == [f"PR-{neighbor_pr}"]
        and "BR-feat/uncertain-mixed" not in actor_body and "ghost_actor" not in actor_body,
        "the acting ACK snapshot/partner set is derived from the PR-only coupling and excludes the BR ghost",
    )
    refreshed_changes = [row.get("change") for row in (call.get("refreshes") or [])
                         if isinstance(row, dict)]
    chk(
        worker_progress.get("posted") == 1 and refreshed_changes == [f"PR-{neighbor_pr}"]
        and {f"PR-{neighbor_pr}", f"PR-{acting_pr}"}.issubset(
            set(call.get("unknown_changes") or [])),
        "the isolated durable-convergence slice posts the genuine neighbor once from the PR-only impact",
    )
    pending_phrase = "no acknowledgement or manual branch operation is needed for that unverified branch"
    chk(
        all("branch verification pending" in body.lower() and pending_phrase in body.lower()
            for body in (actor_body, neighbor_body))
        and "veripsa-ack" in actor_body.lower(),
        "both visible notes require no ACK for the unverified branch while the genuine PR↔PR pause keeps its ACK path",
    )


def fallback_honesty_contract() -> None:
    print("\n-- no-savepoint fallback keeps hard findings and partial-analysis honesty --")
    repo = "acme/branch-fallback-honesty"
    owner = 93104
    path = "src/conflicted.ts"
    branch = "trunk"
    main_sha = "a" * 40
    head_sha = "f" * 40
    db = AppSession(owner)
    try:
        seed_graph(db, repo, path, main_sha, branch=branch)
        claim_in(db, repo, "BR-feat/unverified", path, "ghost_actor", branch=branch)

        def unavailable_inventory():
            raise RuntimeError("branch inventory timeout")

        result = W.handle_pull_request(
            db,
            {"action": "opened", "repo": repo, "base_branch": branch, "pr_number": 74,
             "changed_files": [path], "changed_ranges": {}, "base_hashes": {},
             "author": "fallback_author", "author_is_bot": False, "head_sha": head_sha,
             "head_snapshot_verified": True, "draft": False, "truncated_files": True,
             "conflict_markers": [{"path": path, "line": 7, "kind": "start"}]},
            "fallback_author", act_for=True, reconcile_branch_claims=unavailable_inventory)
        live = states(db, repo)
    finally:
        db.close()

    visible = "\n".join((str((result.get("check") or {}).get("title") or ""),
                           str((result.get("check") or {}).get("summary") or ""),
                           str(result.get("comment") or ""))).lower()
    chk(
        result.get("branch_inventory_unknown") is True
        and (result.get("check") or {}).get("conclusion") == "action_required"
        and "unresolved merge conflict marker" in visible
        and "heading to `trunk`" in visible and "heading to `main`" not in visible,
        "the honest-unknown fallback keeps the conflict hard failure and renders the real trunk coordinate",
    )
    chk(
        "partial analysis" in visible and "unanalyzed remainder" in visible
        and "unknown, not clear" in visible,
        "the same fallback discloses truncation and never overclaims Clear for unread files",
    )
    chk(
        live.get("BR-feat/unverified") == ["active"] and live.get("PR-74") == ["waiting"],
        "fallback rendering leaves the unverified BR and real PR lane states untouched",
    )


def unrelated_branch_cluster_refresh_contract() -> None:
    print("\n-- one PR event cannot refresh an unrelated BR-containing cluster --")
    repo = "acme/unrelated-branch-cluster"
    owner = 93105
    path_acting = "src/acting.ts"
    path_unrelated = "src/unrelated.ts"
    main_sha = "a" * 40
    reconcile_calls: list[bool] = []
    db = AppSession(owner)
    try:
        seed_graph_paths(db, repo, [path_acting, path_unrelated], main_sha)
        claim_in(db, repo, "PR-80", path_acting, "acting_neighbor")
        claim_in(db, repo, "BR-feat/unrelated", path_unrelated, "unrelated_ghost")
        claim_in(db, repo, "PR-90", path_unrelated, "unrelated_waiter")

        def must_not_run():
            reconcile_calls.append(True)
            return {"reconciled": True, "released_changes": []}

        result = W.handle_pull_request(
            db,
            {"action": "opened", "repo": repo, "base_branch": "main", "pr_number": 81,
             "changed_files": [path_acting], "changed_ranges": {}, "base_hashes": {},
             "author": "acting_author", "author_is_bot": False, "head_sha": "b" * 40,
             "head_snapshot_verified": True, "draft": False},
            "acting_author", act_for=True, reconcile_branch_claims=must_not_run)
        impact = db("SELECT core.main_impact_surface(%s,%s)", (repo, "main")) or {}
        if isinstance(impact, str):
            impact = json.loads(impact)
        unscoped = W._refresh_changes(impact, exclude_change="PR-81")
    finally:
        db.close()

    raw_changes = {row.get("change") for row in unscoped if isinstance(row, dict)}
    returned_changes = {row.get("change") for row in (result.get("refreshed") or [])
                        if isinstance(row, dict)}
    chk(
        "PR-90" in raw_changes and "PR-80" in raw_changes,
        "setup proves the unfiltered surface contains refresh work in both independent clusters",
    )
    chk(
        reconcile_calls == [] and returned_changes == {"PR-80"}
        and "PR-90" not in returned_changes and "BR-feat/unrelated" not in returned_changes,
        "the acting event refreshes its PR neighbor only and excludes the unrelated BR-containing cluster",
    )


def global_refresh_branch_authority_contract() -> None:
    print("\n-- global merge/push refresh obtains repo-wide BR authority --")
    owner = 93106
    db = AppSession(owner)
    try:
        # Protected-branch push refresh: a complete inventory proves the missing branch stale. The gate releases
        # it, promotes its PR waiter, and the refresh MUST re-read that new surface (Clear), not render the old wait.
        push_repo = "acme/global-push-stale-branch"
        push_path = "src/push-stale.ts"
        seed_graph(db, push_repo, push_path, "a" * 40)
        claim_in(db, push_repo, "BR-feat/deleted-global", push_path, "ghost")
        claim_in(db, push_repo, "PR-100", push_path, "waiter")
        push_result = W.refresh_inflight(
            db, push_repo, "main",
            reconcile_branch_claims=lambda: I._reconcile_live_branch_claims(
                db, NamesOnlyGitHub(["main"]), push_repo, "main"))
        push_live = states(db, push_repo)
        push_pr = next((row for row in push_result.get("refreshed", [])
                        if row.get("change") == "PR-100"), {})

        # Withdraw refresh uses the SAME global authority gate. Closing one PR is unrelated to the stale BR
        # cluster, but the global refresh still reconciles it before rendering the promoted waiter.
        close_repo = "acme/global-withdraw-stale-branch"
        close_path = "src/closing.ts"
        close_branch_path = "src/close-stale.ts"
        seed_graph_paths(db, close_repo, [close_path, close_branch_path], "a" * 40)
        claim_in(db, close_repo, "PR-110", close_path, "closing")
        claim_in(db, close_repo, "BR-feat/deleted-on-close", close_branch_path, "ghost")
        claim_in(db, close_repo, "PR-111", close_branch_path, "waiter")
        close_result = W.handle_pull_request(
            db,
            {"action": "closed", "repo": close_repo, "base_branch": "main", "pr_number": 110,
             "merged": False, "author": "closing"},
            "closing", act_for=True,
            reconcile_branch_claims=lambda: I._reconcile_live_branch_claims(
                db, NamesOnlyGitHub(["main"]), close_repo, "main"))
        close_live = states(db, close_repo)
        close_pr = next((row for row in close_result.get("refreshed", [])
                         if row.get("change") == "PR-111"), {})

        # A complete inventory that still contains the branch is equally authoritative: release nothing and
        # render the ordinary live BR↔PR coordination instead of suppressing the cluster.
        live_repo = "acme/global-live-branch"
        live_path = "src/live-global.ts"
        seed_graph(db, live_repo, live_path, "a" * 40)
        claim_in(db, live_repo, "BR-feat/live-global", live_path, "live_branch")
        claim_in(db, live_repo, "PR-120", live_path, "live_waiter")
        live_result = W.refresh_inflight(
            db, live_repo, "main",
            reconcile_branch_claims=lambda: I._reconcile_live_branch_claims(
                db, NamesOnlyGitHub(["main", "feat/live-global"]), live_repo, "main"))
        live_state = states(db, live_repo)
        live_pr = next((row for row in live_result.get("refreshed", [])
                        if row.get("change") == "PR-120"), {})

        # Unknown/truncated inventory: preserve DB rows, suppress ONLY the PR whose cluster contains BR, and keep
        # an independent PR-only collision refresh. Exercise both a raised API error and the 100-name sentinel.
        unknown_repo = "acme/global-unknown-branch"
        unknown_branch_path = "src/unknown-branch.ts"
        pr_only_path = "src/pr-only.ts"
        seed_graph_paths(db, unknown_repo, [unknown_branch_path, pr_only_path], "a" * 40)
        claim_in(db, unknown_repo, "BR-feat/uncertain-global", unknown_branch_path, "ghost")
        claim_in(db, unknown_repo, "PR-130", unknown_branch_path, "branch_waiter")
        claim_in(db, unknown_repo, "PR-140", pr_only_path, "pr_holder")
        claim_in(db, unknown_repo, "PR-141", pr_only_path, "pr_waiter")

        def inventory_error():
            raise RuntimeError("branch inventory timeout")

        unknown_result = W.refresh_inflight(
            db, unknown_repo, "main", reconcile_branch_claims=inventory_error)
        truncated_result = W.refresh_inflight(
            db, unknown_repo, "main",
            reconcile_branch_claims=lambda: I._reconcile_live_branch_claims(
                db, NamesOnlyGitHub([f"branch-{n}" for n in range(I._BACKFILL_BRANCH_CAP)]),
                unknown_repo, "main"))
        unknown_live = states(db, unknown_repo)

        # A lone pre-PR BR has no GitHub PR surface. Global refresh must not spend a branch-list read for it.
        lone_repo = "acme/global-lone-branch"
        lone_path = "src/lone-branch.ts"
        seed_graph(db, lone_repo, lone_path, "a" * 40)
        claim_in(db, lone_repo, "BR-feat/lone", lone_path, "branch_only")
        lone_inventory_calls: list[bool] = []

        def unexpected_inventory():
            lone_inventory_calls.append(True)
            raise AssertionError("lone BR must not request global branch inventory")

        lone_result = W.refresh_inflight(
            db, lone_repo, "main", reconcile_branch_claims=unexpected_inventory)
    finally:
        db.close()

    # Main-push handler wiring: the signed repository default passes a usable reconcile closure into the shared
    # global refresh; a missing default may use main for conservative ingest but its closure refuses authority.
    push_wiring: list[tuple[str, str, object]] = []
    push_reconcile_calls: list[tuple[str, str]] = []
    original_ingest_push = WH.ingest_push_deferred
    original_refresh_inflight = WH.refresh_inflight
    original_post_refreshes = WH._post_refreshes
    original_reconcile = WH._reconcile_live_branch_claims

    def wiring_refresh(db, repo, branch, reconcile_branch_claims=None, trace_id=""):
        try:
            authority = reconcile_branch_claims() if callable(reconcile_branch_claims) else None
        except Exception as exc:
            authority = exc
        push_wiring.append((repo, branch, authority))
        return {"repo": repo, "branch": branch, "refreshed": []}

    WH.ingest_push_deferred = lambda *_args, **_kwargs: {"mode": "test", "files": 1, "edges": 0}
    WH.refresh_inflight = wiring_refresh
    WH._post_refreshes = lambda *_args, **_kwargs: 0
    WH._reconcile_live_branch_claims = lambda _db, _gh, repo, branch: (
        push_reconcile_calls.append((repo, branch)) or {"reconciled": True, "released_changes": []})
    try:
        WH._handle_push_event(
            {"ref": "refs/heads/main", "after": "e" * 40,
             "repository": {"full_name": "acme/push-guessed-main"}},
            lambda *_args: None, object())
        WH._handle_push_event(
            {"ref": "refs/heads/trunk", "after": "f" * 40,
             "repository": {"full_name": "acme/push-signed-trunk", "default_branch": "trunk"}},
            lambda *_args: None, object())
    finally:
        WH.ingest_push_deferred = original_ingest_push
        WH.refresh_inflight = original_refresh_inflight
        WH._post_refreshes = original_post_refreshes
        WH._reconcile_live_branch_claims = original_reconcile

    chk(
        (push_result.get("branch_claims_reconciled") or {}).get("released_changes")
        == ["BR-feat/deleted-global"]
        and push_live.get("BR-feat/deleted-global") == ["released"]
        and push_live.get("PR-100") == ["active"]
        and push_pr.get("clear_reset") is True and push_pr.get("conclusion") == "success",
        "main-push refresh releases a proven-stale BR, re-reads the surface, and resets its promoted PR to Clear",
    )
    chk(
        (close_result.get("branch_claims_reconciled") or {}).get("released_changes")
        == ["BR-feat/deleted-on-close"]
        and close_live.get("PR-110") == ["released"]
        and close_live.get("BR-feat/deleted-on-close") == ["released"]
        and close_live.get("PR-111") == ["active"]
        and close_pr.get("clear_reset") is True and close_pr.get("conclusion") == "success",
        "merge/withdraw global refresh applies the same reconcile-and-reread contract before rendering neighbors",
    )
    chk(
        (live_result.get("branch_claims_reconciled") or {}).get("released_changes") == []
        and live_state.get("BR-feat/live-global") == ["active"]
        and live_state.get("PR-120") == ["waiting"]
        and live_pr and live_pr.get("clear_reset") is not True,
        "a complete inventory preserves a live branch and emits its normal BR↔PR coordination refresh",
    )
    unknown_prs = {row.get("change") for row in unknown_result.get("refreshed", [])
                   if isinstance(row, dict) and str(row.get("change") or "").startswith("PR-")}
    truncated_prs = {row.get("change") for row in truncated_result.get("refreshed", [])
                     if isinstance(row, dict) and str(row.get("change") or "").startswith("PR-")}
    chk(
        unknown_prs == {"PR-140", "PR-141"} and truncated_prs == {"PR-140", "PR-141"}
        and unknown_live.get("BR-feat/uncertain-global") == ["active"]
        and unknown_live.get("PR-130") == ["waiting"]
        and (truncated_result.get("branch_claims_reconciled") or {}).get("skipped")
        == "branch inventory truncated",
        "unknown or truncated inventory suppresses only BR-cluster PRs while independent PR-only refreshes remain",
    )
    chk(
        lone_inventory_calls == [] and "branch_claims_reconciled" not in lone_result,
        "a lone BR without any PR surface performs no inventory API call during global refresh",
    )
    chk(
        len(push_wiring) == 2
        and isinstance(push_wiring[0][2], Exception)
        and "authoritative default-branch metadata unavailable" in str(push_wiring[0][2])
        and push_wiring[1][2] == {"reconciled": True, "released_changes": []}
        and push_reconcile_calls == [("acme/push-signed-trunk", "trunk")],
        "main-push wiring rejects guessed main authority and passes the signed trunk reconcile closure",
    )


def boot_order_contract() -> None:
    print("\n-- boot cleanup precedes open-PR replay --")
    repo = "acme/boot-branch-selfheal"
    owner = 93201
    repo_id = owner * 10
    path = "src/boot.ts"
    main_sha = "a" * 40
    pr_sha = "d" * 40
    seed = AppSession(owner)
    try:
        seed_graph(seed, repo, path, main_sha)
        claim_in(seed, repo, "BR-feat/boot-deleted", path, "boot_ghost")
    finally:
        seed.close()

    open_pr = {
        "number": 51,
        "base": {"ref": "main", "sha": main_sha,
                 "repo": {"id": repo_id, "full_name": repo}},
        "head": {"ref": "feat/replayed", "sha": pr_sha,
                 "repo": {"id": repo_id, "full_name": repo}},
        "user": {"login": "boot_author", "type": "User"},
        "changed_files": 1,
        "draft": False,
        "labels": [],
    }
    gh = EventGitHub(
        owner, branches=["main", "feat/replayed"], head_sha=main_sha, open_prs=[open_pr])
    gh.files_by_pr = {51: [path]}
    result = I._reconcile_one_repo(lambda *_args, **_kwargs: None, gh, repo, DSN_APP)

    read = AppSession(owner)
    try:
        live = states(read, repo)
    finally:
        read.close()
    first_branches = gh.order.index("branches") if "branches" in gh.order else 10**6
    first_prs = gh.order.index("open_prs") if "open_prs" in gh.order else -1
    replay = (result.get("results") or [{}])[0]
    chk(
        first_branches < first_prs
        and (result.get("branch_claims_reconciled") or {}).get("released_changes")
        == ["BR-feat/boot-deleted"],
        f"boot inventories/releases branches before listing or replaying PRs (order={gh.order})",
    )
    chk(
        live.get("BR-feat/boot-deleted") == ["released"]
        and live.get("PR-51") == ["active"]
        and ((replay.get("check") or {}).get("conclusion") != "action_required"),
        "the replayed PR sees the cleaned state and posts no ghost-driven action_required check",
    )


def main() -> int:
    print("BRANCH LANE SELF-HEAL GATE")
    inventory_contract()
    ack_uncertainty_contract()
    ack_overlay_without_filtered_impact_contract()
    ack_clear_rewrite_contract()
    synthetic_default_branch_authority_contract()
    synthetic_replay_authority_contract()

    boot = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if boot.returncode != 0:
        print("bootstrap failed:\n", boot.stderr[-1200:])
        return 1

    db_gate_contract()
    hot_pr_contract()
    inventory_failure_contract()
    mixed_inventory_failure_contract()
    fallback_honesty_contract()
    unrelated_branch_cluster_refresh_contract()
    global_refresh_branch_authority_contract()
    boot_order_contract()

    print()
    if all(checks):
        print("BRANCH LANE SELF-HEAL GATE: PASS")
        return 0
    print(f"BRANCH LANE SELF-HEAL GATE: FAIL ({sum(not c for c in checks)} of {len(checks)} failed)")
    return 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    raise SystemExit(rc)
