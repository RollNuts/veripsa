#!/usr/bin/env python3
"""LANDING-RECORD-ACCURACY gate (NO DB, fully offline): the PR-merge path must record only
code-coupling paths as 'landed' and must NOT roll back the merge handling when the landing
record itself fails.

TWO DEFECTS it locks out (round-1 audit A4-F3 + A4-F6):

  A4-F3 (transient failure -> full rollback): the old code called gh.list_pr_files INSIDE
  the event's body after the lane release + push record had already run. If that live GitHub
  call threw (rate limit / transient / perm revoked post-merge), the exception would propagate
  up and roll back the entire event transaction -- undoing the lane release and forcing a
  redeliver cycle for what is telemetry-only. The fix wraps record_landing_with_authority in
  try/except so a transient failure is logged and swallowed: the merge is already done, the
  ledger catches up on the next event.

  A4-F6 (ledger pollution): the old code used a fresh gh.list_pr_files call and filtered only
  isinstance(p, str) -- so docs, images, lock files (README.md, package-lock.json, yarn.lock)
  were passed to record_landing_with_authority, polluting the collisions_on_main ledger with
  non-code paths that can never carry coupling-level collisions. The fix reuses the already-
  computed, _code_paths-filtered `changed` list (computed earlier in the same event before the
  mega-PR cap) -- docs and lock files never reach the landing record.

Run:  python3 tests/test_landing_record_accuracy.py     (no DB needed)
"""
from __future__ import annotations

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

# ---------------------------------------------------------------------------
# Minimal stubs for the modules webhook_handlers.py imports at load time.
# We only need enough to satisfy the import chain without touching a DB or
# network -- exactly as test_rerun_fork_safety.py does for _rerun_replay.
# ---------------------------------------------------------------------------

def _stub_module(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m

# --- webhook stub ---
wh = _stub_module("webhook")
wh.handle_pull_request = lambda db, event, author, act_for=False: {"check": {"conclusion": "success", "title": "Veripsa", "summary": "ok"}, "comment": None, "refreshed": []}
wh.refresh_inflight = lambda db, repo, branch: {"refreshed": []}
wh._bounded_claim_id = lambda cid, path: f"{cid}:{path}"
wh._CHANGE_ID_CAP = 200
# _optional: the savepoint-isolation helper webhook_handlers imports at load time (the merge path uses it to run
# the change_failing signal read fail-open). A pass-through stub that runs work() and fails open is sufficient
# here (no shared txn in this stubbed test — the real savepoint behavior is proven by test_pauseack_txn_isolation).
def _stub_optional(db, label, work, *, default=None, repo="", pr="", trace_id=""):
    # trace_id accepted as a tolerated kwarg to stay signature-compatible with the real _optional (Round-2
    # observability follow-up: a content-free per-event trace_id threaded into the skipped-surface log line).
    try:
        return work()
    except Exception:
        return default
wh._optional = _stub_optional
# PER-EVENT TRACE-ID helpers webhook_handlers.py imports from `webhook` at load time (Round-2 observability
# follow-up). Pass-through stubs are sufficient here — the merge path's prints are content-free and the trace
# helpers are pure formatting (the real behavior is exercised by tests that drive the real webhook module).
wh._ensure_trace_id = lambda payload: ""
wh._trace_of = lambda payload: ""
wh._trace_log_prefix = lambda payload: ""
wh._TRACE_TAG_WIDTH = 12

# --- render stub ---
rd = _stub_module("render")
rd.cleared_comment_body = lambda branch="main": "cleared"
rd.watching_check = lambda **kw: {"conclusion": "neutral", "title": "Veripsa", "summary": "watching"}
rd.quota_paused_check = lambda: {"conclusion": "neutral", "title": "Veripsa", "summary": "paused"}
rd.quota_paused_comment_body = lambda branch="main": "paused"
# EMPTY-FILES WRONG-CLEAR guard: webhook_handlers imports these at load time. The merge path under test
# (action='closed', merged=True) never enters the analyze fetch where the guard fires (should_analyze is False
# on close), so a pass-through stub only needs to satisfy the import chain.
rd.unread_files_check = lambda: {"conclusion": "neutral", "title": "Veripsa", "summary": "not analyzed"}
rd.unread_files_comment_body = lambda branch="main": "not analyzed"
# G1 STALE-GRAPH WITHHELD-CLEAR guard: webhook_handlers imports these at load time. The merge path under test
# (action='closed', merged=True) has should_analyze False, so the withhold never fires; a pass-through stub only
# needs to satisfy the import chain.
rd.stale_graph_unknown_check = lambda: {"conclusion": "neutral", "title": "Veripsa", "summary": "not confirmed current"}
rd.stale_graph_unknown_comment_body = lambda branch="main": "not confirmed current"
# G1 TAIL — HEAD-UNRESOLVABLE WITHHELD-CLEAR guard: webhook_handlers imports these at load time too. Same as the
# stale-graph pair above, the merge path under test (should_analyze False) never fires the withhold; a pass-through
# stub only needs to satisfy the import chain.
rd.stale_graph_head_unknown_check = lambda: {"conclusion": "neutral", "title": "Veripsa", "summary": "currency not confirmed"}
rd.stale_graph_head_unknown_comment_body = lambda branch="main": "currency not confirmed"
# PAUSE-ACK: webhook_handlers imports these at load time. The merge path under test never triggers the overlay
# (it runs only on a 'neutral' acting-PR check; this test's handle_pull_request stub returns 'success'), so a
# pass-through stub that does nothing is sufficient — it must only satisfy the import chain.
rd.ACK_LABEL = "veripsa-ack"
rd.prior_snapshot_from_comment = lambda body: None
rd.apply_pause_ack = lambda rendered, impact, change_ref, **kw: {**rendered, "ack_state": "not_material",
                                                                 "snapshot": "", "label_action": None}

# --- ingest stub ---
ig = _stub_module("ingest")
ig._onboard_repos = lambda db, gh, repos: ([], [])
ig._queue_onboard_repos = lambda db, gh, repos, **kwargs: ([], [])
ig.purge_repo = lambda db, repo: None
ig.purge_account_working_set = lambda db: None
ig.ingest_push = lambda *a, **kw: {}
ig.ingest_push_deferred = lambda *a, **kw: {}
ig.self_heal_main_graph = lambda *a, **kw: None
ig.request_main_graph_refresh = lambda *a, **kw: {
    "healed": False, "queued": True, "reason": "refresh queued",
}
ig.request_main_graph_refresh_wake_only = lambda *a, **kw: {
    "healed": False, "queued": True, "reason": "refresh wake recorded",
}
# Private planned-onboarding capability bindings imported by webhook_handlers.
# This merge-only fixture never presents either value to handle_event; they
# merely keep the fake ingest module load-compatible with the queue-only path.
ig._PLANNED_ONBOARDING_GRAPH_PROOF = object()
ig._PLANNED_ONBOARDING_BOUNDED_UNREAD_KEY = "_veripsa_planned_onboarding_bounded_unread"

# --- server stub (caps read at call time via _server()) ---
sv = _stub_module("server")
sv._MAX_PR_FILES = 50
sv._FAILING_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required"}
sv._RERUN_PR_CAP = 5
sv._NEIGHBOR_REFRESH_CAP = 30   # _post_refreshes reads this at call time (caps the neighbor-refresh fan-out)

# --- code_graph_extract stub ---
cge = _stub_module("code_graph_extract")
_NONCODE_EXTS = {
    ".md", ".txt", ".rst", ".json", ".lock", ".yaml", ".yml",
    ".png", ".jpg", ".gif", ".svg", ".ico", ".pdf",
    ".toml", ".cfg", ".ini", ".env",
}
def _is_noncode(p: str) -> bool:
    _, ext = os.path.splitext(p)
    return ext.lower() in _NONCODE_EXTS
cge.is_noncode_path = _is_noncode
cge._nfc = lambda p: p  # no-op NFC in tests (all ASCII paths)

# ---------------------------------------------------------------------------
# Now import the module under test
# ---------------------------------------------------------------------------
import webhook_handlers as W  # noqa: E402


# ---------------------------------------------------------------------------
# Fake GitHub client
# ---------------------------------------------------------------------------

class FakeGH:
    """Records calls; list_pr_files optionally raises to test fail-open."""
    def __init__(self, files=None, raise_on_list=False):
        self._files = files or []
        self._raise_on_list = raise_on_list
        self.list_pr_files_calls = 0
        self.checks = []
        self.comments = []

    def for_installation(self, iid):
        return self

    def installation_account_id(self):
        return "42"

    def list_pr_files(self, repo, number, pr_changed_files=0):
        self.list_pr_files_calls += 1
        if self._raise_on_list:
            raise RuntimeError("simulated GitHub transient error (rate limit / timeout)")
        return self._files

    def list_pr_files_with_ranges(self, repo, number, pr_changed_files=0):
        self.list_pr_files_calls += 1
        if self._raise_on_list:
            raise RuntimeError("simulated GitHub transient error (rate limit / timeout)")
        # return dict path -> ranges for code files only
        return {p: [] for p in self._files if not _is_noncode(p)}

    def upsert_check(self, repo, sha, conclusion, title, summary):
        self.checks.append({"sha": sha, "conclusion": conclusion})

    def upsert_comment(self, repo, number, marker, body):
        self.comments.append({"number": number, "body": body})

    def list_issue_comments(self, repo, number):
        return []

    def patch_comment_if_exists(self, repo, number, marker, body):
        return False

    def list_check_runs(self, repo, sha):
        return []

    def pull_request_head(self, repo, number):
        return f"head-{number}"

    def pull_request_head_and_fork(self, repo, number):
        return f"head-{number}", False

    def repo_default_branch_head(self, repo):
        return "main", "c" * 40

    def list_open_pull_requests(self, repo, limit=None):
        return []


# ---------------------------------------------------------------------------
# Fake DB that records landing-record calls and can optionally raise
# ---------------------------------------------------------------------------

class FakeDB:
    def __init__(self, raise_on_landing=False):
        self._raise_on_landing = raise_on_landing
        self.landing_calls = []   # each entry: (repo, branch, sha, paths, author, author_is_bot)
        self.other_calls = []

    def __call__(self, sql: str, args=()):
        if "record_landing_with_authority" in sql:
            if self._raise_on_landing:
                raise RuntimeError("simulated DB transient error on record_landing")
            self.landing_calls.append(args)
            return None
        if "land_change" in sql or "record_push" in sql:
            return None
        if "release_change_on_main" in sql:
            return None
        if "act_for_claim" in sql:
            return None
        if "change_failing" in sql:
            return None
        if "enter_installation" in sql:
            return None
        # any other call: no-op
        self.other_calls.append(sql[:60])
        return None


# ---------------------------------------------------------------------------
# Helper: build a minimal closed+merged pull_request payload
# ---------------------------------------------------------------------------

def _merge_payload(pr_number: int = 42, merged: bool = True):
    return {
        "action": "closed",
        "number": pr_number,
        "installation": {"id": 9999},
        "repository": {"full_name": "acme/app", "default_branch": "main"},
        "pull_request": {
            "base": {"ref": "main", "sha": "b" * 40},
            "head": {"sha": "h" * 40, "ref": "feat/thing", "repo": {"id": 1}},
            "user": {"login": "alice", "type": "User"},
            "merged": merged,
            "draft": False,
            "merge_commit_sha": "m" * 40,
        },
    }


# ---------------------------------------------------------------------------
# Scenario A: list_pr_files raises -> event must NOT propagate the exception
#             (fail-open: landing telemetry must not roll back the merge handling)
# ---------------------------------------------------------------------------

def scenario_a_fail_open() -> list[tuple[str, bool]]:
    checks: list[tuple[str, bool]] = []

    # Part 1: DB raises on record_landing (the old rollback scenario — a DB transient after the merge)
    gh1 = FakeGH(files=["src/engine.py", "README.md"], raise_on_list=False)
    db1 = FakeDB(raise_on_landing=True)

    payload = _merge_payload(pr_number=42, merged=True)
    try:
        result1 = W.handle_event("pull_request", payload, db1, gh1)
        raised1 = False
    except Exception:
        result1 = {}
        raised1 = True

    checks.append((
        "A4-F3 fail-open (DB error): a transient record_landing DB failure must NOT propagate",
        not raised1,
    ))
    checks.append((
        "A4-F3 fail-open (DB error): result dict is returned even when record_landing raised",
        isinstance(result1, dict) and len(result1) > 0,
    ))

    # Part 2: gh.list_pr_files itself raises (rate limit / timeout / perm revoked post-merge)
    # This is the ORIGINAL A4-F3 defect: the fresh gh.list_pr_files call was outside try/except,
    # so a transient GitHub error would propagate and roll back the lane release.
    gh2 = FakeGH(files=[], raise_on_list=True)
    db2 = FakeDB(raise_on_landing=False)

    payload2 = _merge_payload(pr_number=43, merged=True)
    try:
        result2 = W.handle_event("pull_request", payload2, db2, gh2)
        raised2 = False
    except Exception:
        result2 = {}
        raised2 = True

    checks.append((
        "A4-F3 fail-open (GH error): a transient gh.list_pr_files error must NOT propagate (was the original rollback vector)",
        not raised2,
    ))
    checks.append((
        "A4-F3 fail-open (GH error): result dict is returned even when list_pr_files raised",
        isinstance(result2, dict) and len(result2) > 0,
    ))
    checks.append((
        "A4-F3 fail-open (GH error): no landing record is written when the files fetch fails (correct — no paths to record)",
        len(db2.landing_calls) == 0,
    ))

    return checks


# ---------------------------------------------------------------------------
# Scenario B: mixed code + doc paths -> only code paths reach the landing record
# ---------------------------------------------------------------------------

def scenario_b_no_doc_pollution() -> list[tuple[str, bool]]:
    checks: list[tuple[str, bool]] = []

    # The PR touches both code and non-code paths
    mixed_files = [
        "src/engine.py",       # code -- should land
        "src/core.py",         # code -- should land
        "README.md",           # doc  -- must NOT land
        "package-lock.json",   # lock -- must NOT land
        "docs/guide.rst",      # doc  -- must NOT land
        "assets/logo.png",     # image -- must NOT land
    ]
    gh = FakeGH(files=mixed_files, raise_on_list=False)
    db = FakeDB(raise_on_landing=False)

    payload = _merge_payload(pr_number=7, merged=True)
    result = W.handle_event("pull_request", payload, db, gh)

    checks.append((
        "A4-F6 no-pollution: handle_event returns a result dict for a merged PR",
        isinstance(result, dict),
    ))

    if db.landing_calls:
        recorded_paths = list(db.landing_calls[0][3])  # p_paths arg is index 3 in (repo,branch,sha,paths,author,bot)
        non_code_landed = [p for p in recorded_paths if _is_noncode(p)]
        checks.append((
            f"A4-F6 no-pollution: README.md / lock / docs / images must NOT be in the landing record (found: {non_code_landed})",
            len(non_code_landed) == 0,
        ))
        code_landed = [p for p in recorded_paths if not _is_noncode(p)]
        checks.append((
            f"A4-F6 no-pollution: code files ARE in the landing record (found: {code_landed})",
            len(code_landed) > 0,
        ))
        checks.append((
            "A4-F6 no-pollution: landing record paths are a subset of the input code files",
            all(p in mixed_files for p in code_landed),
        ))
    else:
        # No landing call at all -- still fine if changed ended up empty (e.g. all non-code),
        # but for this fixture we expect at least one code path so mark it as a failure.
        checks.append(("A4-F6 no-pollution: expected a landing record call for code paths", False))
        checks.append(("A4-F6 no-pollution: landing paths are code-only (no call recorded)", False))
        checks.append(("A4-F6 no-pollution: code paths present in landing record", False))

    return checks


# ---------------------------------------------------------------------------
# Scenario C: code-only PR -> landing record receives all code paths
# ---------------------------------------------------------------------------

def scenario_c_code_only() -> list[tuple[str, bool]]:
    checks: list[tuple[str, bool]] = []

    code_files = ["lib/parser.py", "lib/router.py", "tests/test_parser.py"]
    gh = FakeGH(files=code_files, raise_on_list=False)
    db = FakeDB(raise_on_landing=False)

    payload = _merge_payload(pr_number=99, merged=True)
    W.handle_event("pull_request", payload, db, gh)

    if db.landing_calls:
        recorded = list(db.landing_calls[0][3])
        checks.append((
            f"code-only PR: all code paths land (expected {sorted(code_files)}, got {sorted(recorded)})",
            set(recorded) == set(code_files),
        ))
    else:
        checks.append(("code-only PR: landing record call must be made", False))

    return checks


# ---------------------------------------------------------------------------
# Scenario D: a NON-merged closed PR must NOT produce a landing record
# ---------------------------------------------------------------------------

def scenario_d_no_landing_on_close_without_merge() -> list[tuple[str, bool]]:
    checks: list[tuple[str, bool]] = []

    gh = FakeGH(files=["src/engine.py"], raise_on_list=False)
    db = FakeDB(raise_on_landing=False)

    payload = _merge_payload(pr_number=5, merged=False)
    W.handle_event("pull_request", payload, db, gh)

    checks.append((
        "closed-but-not-merged PR must produce zero landing records",
        len(db.landing_calls) == 0,
    ))
    return checks


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    all_checks: list[tuple[str, bool]] = []

    print("\n--- Scenario A: A4-F3 fail-open (transient DB error on record_landing) ---")
    a = scenario_a_fail_open()
    for name, ok in a:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    all_checks.extend(a)

    print("\n--- Scenario B: A4-F6 no ledger pollution (mixed code+doc paths) ---")
    b = scenario_b_no_doc_pollution()
    for name, ok in b:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    all_checks.extend(b)

    print("\n--- Scenario C: code-only PR lands all paths ---")
    c = scenario_c_code_only()
    for name, ok in c:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    all_checks.extend(c)

    print("\n--- Scenario D: closed-but-not-merged PR produces no landing record ---")
    d = scenario_d_no_landing_on_close_without_merge()
    for name, ok in d:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    all_checks.extend(d)

    ok = all(c[1] for c in all_checks)
    print(f"\nLANDING-RECORD GATE: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
