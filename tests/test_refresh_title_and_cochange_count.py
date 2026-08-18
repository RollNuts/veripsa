#!/usr/bin/env python3
"""GATE 184 — refresh-path TITLE determinism + the co-change RAW-COUNT moat lock.

Two synthetic output/UX regressions are locked here.

(A) CHECK-TITLE NON-DETERMINISM. The ACTING path (server.py → render_pr_check → _safe_upsert_check) posts the rich
    per-verdict title ("Veripsa — Unknown" / "… — Wait in line" / …). The NEIGHBOR-REFRESH path (a SIBLING PR's
    open/sync/push re-renders an in-flight neighbor via webhook._refresh_changes → _post_refreshes) used to
    HARD-CODE the title to a bare "Veripsa" and discard render_pr_check's title — so the SAME PR's title silently
    DEGRADED whenever another PR last rendered it. ROOT FIX: thread the rendered title through the refresh payload
    (the summary already round-trips; the title now does too), and the ack-overlay path takes overlay["title"]
    exactly like the acting path. This gate proves the refresh path posts the SAME title the acting path renders
    for the same verdict (and never a bare "Veripsa") — for clear / warn / serialize / unknown, the non-overlay
    AND the pause-ack overlay (paused) branches.

(B) RAW CO-CHANGE COUNT LEAK + LITERAL "N%" HONEST-COPY (PO moat rule: "raw counts はダメ" + the honest-copy
    rule "no literal X% / no-guarantee"). The co-change advisory
    line used to render "… **{pct}** of the time ({liftx}; {co} of {n} changes)" — TWO leaks:
      • the "({co} of {n} changes)" parenthetical is a RAW graph co-occurrence count (the moat class — it
        exposes the graph's history depth/size). Dropped 2026-06-23.
      • the "**{pct}** of the time" segment SATURATED at strong couplings as "**100%** of the time" — a literal
        customer-facing guarantee. The lift "× more than
        chance" is the interpretable, uncapped signal that carries the whole meaning. ROOT FIX: DROP the "N%"
        segment too. This gate proves NO customer-facing co-change string contains a raw "(N of M)" count NOR a
        literal "N%" / "N% of the time", across verdicts + a fork PR + the degenerate-row mix — while the lift
        survives (a positive presence check, so the ban is precise).

PURE + OFFLINE (no DB, no network — render_pr_check / _refresh_changes / _post_refreshes / apply_pause_ack are
stateless over a main_impact_surface-shaped dict; the gh + db here are tiny in-memory fakes).
Run:  python3 tests/test_refresh_title_and_cochange_count.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import render as R                          # noqa: E402
import webhook as W                         # noqa: E402
from webhook_handlers import _post_refreshes  # noqa: E402
from github_rest_prread import PRFilesShortfall  # noqa: E402

FAIL = 0


def check(cond: bool, label: str) -> None:
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


REPO = "acme/app"

# A representative co-change payload (strongest-first, as core emits it): partner path + directional confidence +
# base-rate-corrected lift + the RAW support fields (co/n). The RAW support is an ENGINE INPUT here; the contract
# under test is that it is NOT echoed into any customer-facing string.
CC = [{"edited": "backend/auth.py", "partner": "backend/api.py", "prob": 0.73, "lift": 4.5, "co": 11, "n": 15}]

# the banned raw-count shape — "(N of M changes)" / "N of M changes" with any small integers. Word-boundaried so
# it can never match an unrelated "1 of 3 PRs"-style operational/queue count (those use "PR(s)" / "change(s)", and
# the co-change line is the only surface that ever said "N of M changes").
_RAW_COUNT_RE = re.compile(r"\b\d+\s+of\s+\d+\s+changes\b", re.IGNORECASE)


class _CaptureGH:
    """Just enough GitHub client for _post_refreshes. Records the (conclusion, title, summary) of every check
    upsert AND every comment body, keyed by PR number, so the test can assert what reached each PR. `fork_prs`
    is the set of PR numbers reported as forks (head.repo != base.repo). `labels` maps PR -> its label set (for
    the pause-ack readback). No prior comment (no ack snapshot) → a material neighbor with no ack label = PAUSED."""

    def __init__(self, fork_prs=(), labels=None, has_prior_comment=False, metadata=None, metadata_error=None,
                 compare_paths=None, compare_error=None):
        self.checks: dict[int, dict] = {}
        self.comments: dict[int, str] = {}
        self.check_writes = 0
        self.metadata_calls = 0
        self.pr_reads = 0
        self.compare_calls = 0
        self._forks = set(fork_prs)
        self._labels = labels or {}
        # clear_reset only posts a check when a PRIOR Veripsa marker comment exists (proof the PR was non-clear).
        # has_prior_comment=True models that "previously-warned, now clear" PR so the clear_reset check actually posts.
        self._has_prior_comment = has_prior_comment
        self._metadata = metadata if metadata is not None else {
            "changed": ["backend/auth.py"], "changed_ranges": {"backend/auth.py": []},
            "added_paths": [], "conflict_markers": [], "raw_entry_count": 1,
        }
        if isinstance(self._metadata, dict):
            self._metadata = dict(self._metadata)
            raw_changed = self._metadata.get("changed")
            self._metadata.setdefault("raw_entry_count", len(raw_changed) if isinstance(raw_changed, list) else 0)
        self._metadata_error = metadata_error
        self._compare_paths = list(compare_paths or [])
        self._compare_error = compare_error

    def get_pull_request(self, repo, number):
        self.pr_reads += 1
        changed = self._metadata.get("changed") if isinstance(self._metadata, dict) else None
        changed_files = len(changed) if isinstance(changed, list) else 1
        return {"changed_files": max(1, changed_files),
                "head": {"sha": f"sha-{number}", "repo": {"id": 2 if number in self._forks else 1}},
                "base": {"ref": "main", "sha": "base-sha", "repo": {"id": 1}}, "state": "open"}

    def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
        self.metadata_calls += 1
        if self._metadata_error is not None:
            raise self._metadata_error
        return self._metadata

    def pull_request_head_and_fork(self, repo, number):
        return (f"sha-{number}", number in self._forks)

    def pull_request_head(self, repo, number):          # used by the clear_reset path
        return f"sha-{number}"

    def upsert_check(self, repo, sha, conclusion, title, summary):
        # key by the PR number embedded in the fake sha (sha-<number>)
        num = int(str(sha).split("-")[-1])
        desired = {"conclusion": conclusion, "title": title, "summary": summary}
        if self.checks.get(num) == desired:
            return self.checks[num]                    # production's byte-identical check-run no-op
        self.check_writes += 1
        self.checks[num] = desired
        return desired

    def upsert_comment(self, repo, number, marker, body):
        self.comments[number] = body

    def patch_comment_if_exists(self, repo, number, marker, body_fn):
        if self._has_prior_comment:
            body_fn()                                   # exercise the deferred body builder (production-realistic)
            return True
        return False

    def repo_default_branch_head(self, repo):
        return ("main", "sha")

    def compare_changed_paths(self, repo, base_sha, branch):
        self.compare_calls += 1
        if self._compare_error is not None:
            raise self._compare_error
        return list(self._compare_paths)

    # Production neighbor refresh requires a strict compare that distinguishes a real empty diff from transport
    # failure. Keep the soft alias above for legacy callers, but drive the strict seam in every db-backed test.
    def compare_changed_paths_strict(self, repo, base_sha, branch):
        return self.compare_changed_paths(repo, base_sha, branch)

    def pr_labels(self, repo, number, strict=False):
        return list(self._labels.get(number, []))

    def list_issue_comments(self, repo, number):
        return []                                       # no prior Veripsa comment → no prior ack snapshot


def _impact_for(change_id, verdict, *, paths, **extra):
    """One in-flight change, plus a fixed SIBLING that does the refreshing (so `change_id` is always a NEIGHBOR
    refreshed because the sibling moved — the exact production path that used to degrade the title)."""
    me = {"change_id": change_id, "label": f"dev {change_id}", "agent": "dev", "verdict": verdict, "paths": paths}
    # The live DB-backed refresh path now proves that the claim surface and GitHub Files evidence belong to the
    # same PR head. Keep the common fixture production-shaped by default; individual regressions can explicitly
    # override it with an old head or None to exercise the fail-closed boundary.
    match = re.fullmatch(r"PR-(\d+)", change_id)
    if match and "head_sha" not in extra:
        me["head_sha"] = f"sha-{int(match.group(1))}"
    me.update(extra)
    sibling = {"change_id": "PR-SIB", "label": "sib PR-SIB", "agent": "sib", "verdict": "clear",
               "paths": ["unrelated/elsewhere.py"]}
    return {"repo": REPO, "branch": "main", "changes": [sibling, me]}


def _title_scenarios():
    """One scenario per verdict the customer ever sees, each as an in-flight NEIGHBOR refreshed by the sibling.
    Returns (desc, impact, change_id)."""
    return [
        ("unknown (the 实测 #435 regression: a new path → 'Veripsa — Unknown')",
         _impact_for("PR-1", "unknown", paths=["new/x.rs"], unknown_paths=["new/x.rs"]), "PR-1"),
        ("clear",
         _impact_for("PR-2", "clear", paths=["backend/auth.py"]), "PR-2"),
        ("warn",
         _impact_for("PR-3", "warn", paths=["backend/auth.py"], impact=["backend/handlers.py"],
                     contested_with=["alice PR-9"]), "PR-3"),
        ("serialize (a direct collision → 'Veripsa — Wait in line')",
         _impact_for("PR-4", "serialize", paths=["backend/auth.py"], serialize_behind=["bob PR-9"],
                     collision_points=[{"behind": "bob PR-9", "path": "backend/auth.py", "symbol": "login",
                                        "line_lo": 5, "line_hi": 20}]), "PR-4"),
    ]


def main() -> int:
    # ── (A) TITLE DETERMINISM: the refresh path posts the SAME title the acting path renders ───────────────────
    saw_unknown_title = False
    for desc, impact, cid in _title_scenarios():
        # the ACTING path's title for this exact change (what render_pr_check → _safe_upsert_check posts directly)
        acting_title = R.render_pr_check(impact, cid)["title"]
        check(acting_title and acting_title != "Veripsa" and acting_title.startswith("Veripsa — "),
              f"[{desc}] the acting-path title is the rich per-verdict title, never bare 'Veripsa'  (got {acting_title!r})")
        if "Unknown" in acting_title:
            saw_unknown_title = True

        # the REFRESH path: the sibling PR-SIB moved, so it refreshes every OTHER in-flight change (= cid). Build
        # the refresh payload (now carrying the title) and POST it through the real _post_refreshes (db=None → the
        # straight non-overlay path), then read back the title the check upsert actually received. The PAYLOAD must
        # ALWAYS carry the rendered title — whether the entry is a plain refresh (warn/serialize/unknown) or a
        # `clear_reset` (a clear PR: render_pr_check returns comment=None → the downgrade-to-clear entry).
        refreshed = W._refresh_changes(impact, exclude_change="PR-SIB")
        entry = next((e for e in refreshed if e.get("change") == cid), None)
        check(entry is not None, f"[{desc}] the neighbor ({cid}) IS in the refresh set")
        check((entry or {}).get("title") == acting_title,
              f"[{desc}] the refresh PAYLOAD carries the rendered title (round-trips, like summary)  "
              f"(payload={ (entry or {}).get('title')!r} acting={acting_title!r})")

        # A `clear_reset` entry (clear verdict) posts a check ONLY when a PRIOR Veripsa comment exists (proof the PR
        # was previously non-clear — the no-spam rule). Model that case so the clear_reset check actually fires and
        # we can assert IT, too, uses the threaded title (not a bare 'Veripsa'). A plain entry posts unconditionally.
        is_clear_reset = bool((entry or {}).get("clear_reset"))
        gh = _CaptureGH(has_prior_comment=is_clear_reset)
        _post_refreshes(gh, REPO, refreshed)            # db=None → no overlay; the title flows straight through
        posted_title = gh.checks.get(int(cid.split("-")[-1]), {}).get("title")
        check(posted_title == acting_title,
              f"[{desc}] the REFRESH path posts the SAME title as the ACTING path (no bare-'Veripsa' degradation)  "
              f"(refresh={posted_title!r} acting={acting_title!r})")
        check(posted_title and posted_title != "Veripsa",
              f"[{desc}] the refresh path never posts a bare 'Veripsa' (the 实测 #435 regression)  (got {posted_title!r})")

    check(saw_unknown_title,
          "coverage: the 'Veripsa — Unknown' verdict (the exact 实测 #435 title) was exercised on both paths")

    # ── (A2) THE PAUSE-ACK OVERLAY BRANCH of the refresh path: a MATERIAL neighbor with NO ack label is PAUSED.
    # The overlay can SWAP the title; the refresh path must take overlay["title"] (exactly like the acting path) —
    # never fall back to a bare 'Veripsa'. Drive _post_refreshes WITH a db (the overlay only runs when db is set).
    ser_impact = _impact_for("PR-7", "serialize", paths=["backend/auth.py"], serialize_behind=["bob PR-9"],
                             collision_points=[{"behind": "bob PR-9", "path": "backend/auth.py", "symbol": "login",
                                                "line_lo": 5, "line_hi": 20}])

    def _fake_db(sql, args=()):
        # the overlay's ONLY real read is core.main_impact_surface; everything else (SAVEPOINT/RELEASE) is a no-op
        # whose return value is ignored. Return the SAME surface the refresh was built from (production-realistic).
        if "main_impact_surface" in str(sql):
            return ser_impact
        if "co_change_partners_with_authority" in str(sql):
            return []
        if "account_coverage_surface" in str(sql):
            return {}
        return None

    refreshed = W._refresh_changes(ser_impact, exclude_change="PR-SIB")
    # the acting path's overlaid title for the SAME paused coupling (the reference the refresh path must match)
    acting_render = R.render_pr_check(ser_impact, "PR-7")
    acting_overlay = R.apply_pause_ack(
        {"conclusion": acting_render["conclusion"], "title": acting_render["title"],
         "summary": acting_render["summary"], "comment": acting_render.get("comment")},
        ser_impact, "PR-7", label_present=False, prior_hash=None, branch="main")
    expected_overlay_title = acting_overlay["title"]

    gh = _CaptureGH(labels={})                          # no ack label → PAUSED
    _post_refreshes(gh, REPO, refreshed, db=_fake_db, branch="main")
    overlay_posted = gh.checks.get(7, {})
    check(overlay_posted.get("conclusion") == "action_required",
          "pause-ack overlay: a material un-acked neighbor is PAUSED (action_required) on the refresh path")
    check(overlay_posted.get("title") == expected_overlay_title and overlay_posted.get("title") != "Veripsa",
          "pause-ack overlay: the refresh path takes overlay['title'] (same as the acting path), never bare 'Veripsa'  "
          f"(refresh={overlay_posted.get('title')!r} acting={expected_overlay_title!r})")

    # ── (A3) EVIDENCE-COMPLETE REFRESH: a sibling must never overwrite the acting PR's richer verdict ─────────
    # `_refresh_changes` has only main_impact_surface, not Files-API status/cap/patch metadata. The live post seam
    # must make exactly one authoritative metadata read and re-render before ANY clear-reset/comment/check write.
    def _db_for(surface, cochange=None, coverage=None, calls=None,
                cochange_error=None, coverage_error=None):
        def _db(sql, args=()):
            if "main_impact_surface" in str(sql):
                return surface
            if "co_change_partners_with_authority" in str(sql):
                if calls is not None:
                    calls["cochange"] = calls.get("cochange", 0) + 1
                if cochange_error is not None:
                    raise cochange_error
                return [] if cochange is None else cochange
            if "account_coverage_surface" in str(sql):
                if calls is not None:
                    calls["coverage"] = calls.get("coverage", 0) + 1
                if coverage_error is not None:
                    raise coverage_error
                return {} if coverage is None else coverage
            return None
        return _db

    # ADDED PATH: the engine says blanket Unknown, but Files API proves the only unknown path is newly added and
    # an in-graph path was verified. Acting path renders Clear. A no-history PR must remain a true no-op (no spam),
    # not have the evidence-free refresh overwrite it back to Unknown.
    added_impact = _impact_for("PR-21", "unknown", paths=["backend/auth.py", "new/worker.py"],
                               unknown_paths=["new/worker.py"])
    added_refresh = W._refresh_changes(added_impact, exclude_change="PR-SIB")
    check(added_refresh[0]["conclusion"] == "neutral",
          "evidence fixture: the pure refresh payload is Unknown before Files-API added-path evidence")
    added_meta = {"changed": ["backend/auth.py", "new/worker.py"], "changed_ranges": {},
                  "added_paths": ["new/worker.py"], "conflict_markers": []}
    gh = _CaptureGH(metadata=added_meta)
    touched = _post_refreshes(gh, REPO, added_refresh, db=_db_for(added_impact), branch="main")
    check(gh.pr_reads == 2 and gh.metadata_calls == 1,
          "added-path refresh brackets one-pass metadata with exactly two coherent PR reads")
    check(touched == 1 and gh.checks.get(21, {}).get("conclusion") == "success"
          and "New paths in this PR" in gh.comments.get(21, ""),
          "added-path evidence re-renders Unknown→Clear while retaining its meaningful new-path note")
    gh = _CaptureGH(metadata=added_meta, has_prior_comment=True)
    touched = _post_refreshes(gh, REPO, added_refresh, db=_db_for(added_impact), branch="main")
    check(touched == 1 and gh.checks.get(21, {}).get("conclusion") == "success",
          "added-path evidence re-renders Unknown→Clear and clears a genuinely stale prior verdict")

    # A plain evidence-complete Clear has no useful comment. It must still use the existing patch-if-exists seam:
    # without a prior Veripsa marker, no check/comment write is made (the PR + metadata GETs are reads, not spam).
    plain_clear_impact = _impact_for("PR-25", "clear", paths=["backend/auth.py"])
    plain_clear_refresh = W._refresh_changes(plain_clear_impact, exclude_change="PR-SIB")
    gh = _CaptureGH(metadata={"changed": ["backend/auth.py"], "changed_ranges": {},
                              "added_paths": [], "conflict_markers": []})
    touched = _post_refreshes(gh, REPO, plain_clear_refresh, db=_db_for(plain_clear_impact), branch="main")
    check(touched == 0 and not gh.checks and not gh.comments,
          "evidence-complete plain Clear preserves clear-reset no-spam when no prior marker exists")

    # FALSE-CLEAR PARITY on the DOWNGRADE-TO-CLEAR repost: a clear_reset that WOULD reset a previously-warned PR to
    # green must be WITHHELD under a degraded graph (stale-behind-HEAD / HEAD-unresolvable / version-mismatch) —
    # resetting a check to success off a graph we cannot confirm is current is a false clear, the SAME reason the
    # acting-PR clear is withheld. The next fresh event re-confirms and resets then.
    cr_impact = _impact_for("PR-27", "clear", paths=["backend/auth.py"])
    cr_refresh = W._refresh_changes(cr_impact, exclude_change="PR-SIB")
    check(bool((next((e for e in cr_refresh if e.get("change") == "PR-27"), {}) or {}).get("clear_reset")),
          "fixture: PR-27 refresh payload is a clear_reset (downgrade-to-clear)")
    cr_meta = {"changed": ["backend/auth.py"], "changed_ranges": {}, "added_paths": [], "conflict_markers": []}
    gh_ok = _CaptureGH(metadata=cr_meta, has_prior_comment=True)           # prior marker → previously non-clear
    touched_ok = _post_refreshes(gh_ok, REPO, cr_refresh, db=_db_for(cr_impact), branch="main")
    check(touched_ok == 1 and gh_ok.checks.get(27, {}).get("conclusion") == "success",
          "control: on a healthy graph the clear_reset resets the previously-warned PR to green")
    gh_deg = _CaptureGH(metadata=cr_meta, has_prior_comment=True)
    touched_deg = _post_refreshes(gh_deg, REPO, cr_refresh, db=_db_for(cr_impact), branch="main",
                                  graph_degraded=True)
    check(touched_deg == 0 and 27 not in gh_deg.checks and 27 not in gh_deg.comments,
          "FIX: under a degraded graph the clear_reset is withheld — no false-Clear reset, nothing touched")

    # TRUNCATION: the surface itself is Clear, but current metadata exceeds the analysis cap. The refresh must
    # become honest Unknown instead of using its pre-rendered clear_reset and overwriting a partial analysis green.
    import server as S
    old_cap = S._MAX_PR_FILES
    try:
        S._MAX_PR_FILES = 1
        truncated_impact = _impact_for("PR-22", "clear", paths=["backend/auth.py"])
        truncated_refresh = W._refresh_changes(truncated_impact, exclude_change="PR-SIB")
        check(bool(truncated_refresh[0].get("clear_reset")),
              "evidence fixture: the pure refresh payload is Clear before current file-cap evidence")
        gh = _CaptureGH(metadata={"changed": ["backend/auth.py", "backend/api.py"], "changed_ranges": {},
                                  "added_paths": [], "conflict_markers": []})
        touched = _post_refreshes(gh, REPO, truncated_refresh, db=_db_for(truncated_impact), branch="main")
        check(gh.metadata_calls == 1 and touched == 1 and gh.checks.get(22, {}).get("conclusion") == "neutral",
              "truncated-file evidence re-renders Clear→Unknown before posting")
        check("not fully analyzed" in gh.checks.get(22, {}).get("summary", "").lower(),
              "truncated refresh discloses the partial analysis instead of claiming Clear")
    finally:
        S._MAX_PR_FILES = old_cap

    # CONFLICT MARKERS: a clean engine surface cannot erase an acting-path hard failure. Current patch metadata
    # must restore action_required and its path/line detail before the old clear_reset branch can run.
    conflict_impact = _impact_for("PR-23", "clear", paths=["backend/auth.py"])
    conflict_refresh = W._refresh_changes(conflict_impact, exclude_change="PR-SIB")
    gh = _CaptureGH(metadata={"changed": ["backend/auth.py"], "changed_ranges": {}, "added_paths": [],
                              "conflict_markers": [
                                  {"path": "backend/auth.py", "line": 7, "kind": "start"},
                                  {"path": "backend/auth.py", "line": 11, "kind": "end"},
                              ]})
    touched = _post_refreshes(gh, REPO, conflict_refresh, db=_db_for(conflict_impact), branch="main")
    check(gh.metadata_calls == 1 and touched == 1 and gh.checks.get(23, {}).get("conclusion") == "action_required",
          "conflict-marker evidence re-renders Clear→action_required before posting")
    check("backend/auth.py" in gh.comments.get(23, "") and "line 7" in gh.comments.get(23, ""),
          "conflict refresh preserves content-free path/line evidence in the verdict comment")

    # The cached authoritative PR object must preserve the existing fork privacy split while avoiding a second
    # head/fork GET. The external conversation may name this PR's own path, never the maintainer-side partner.
    fork_impact = _impact_for(
        "PR-26", "serialize", paths=["backend/auth.py"], serialize_behind=["maint PR-99"],
        collision_points=[{"behind": "maint PR-99", "path": "private/internal.py", "symbol": "secret_fn"}])
    fork_refresh = W._refresh_changes(fork_impact, exclude_change="PR-SIB")
    gh = _CaptureGH(fork_prs={26}, metadata={"changed": ["backend/auth.py"], "changed_ranges": {},
                                                    "added_paths": [], "conflict_markers": []})
    touched = _post_refreshes(gh, REPO, fork_refresh, db=_db_for(fork_impact), branch="main")
    fork_body = gh.comments.get(26, "")
    check(touched == 1 and gh.pr_reads == 2 and "maint PR-99" not in fork_body
          and "private/internal.py" not in fork_body and "secret_fn" not in fork_body,
          "evidence-complete refresh reuses PR identity and preserves fork redaction")

    # ACTOR/NEIGHBOR INPUT PARITY: the evidence refresh must carry the same non-surface inputs as the PR's own
    # event. A co-change-only Clear has a meaningful comment + a non-absolute title; treating the pre-rendered
    # surface-only Clear as clear_reset would erase both on every sibling event.
    parity_cc = [{"edited": "backend/auth.py", "partner": "backend/policy.py",
                  "prob": 0.81, "lift": 3.4, "co": 8, "n": 11}]
    cochange_impact = _impact_for("PR-27", "clear", paths=["backend/auth.py"])
    cochange_refresh = W._refresh_changes(cochange_impact, exclude_change="PR-SIB")
    check(bool(cochange_refresh[0].get("clear_reset")),
          "co-change parity fixture starts as a surface-only Clear before current empirical evidence")
    cochange_actor = R.render_pr_check(cochange_impact, "PR-27", cochange=parity_cc)
    cochange_calls = {}
    gh = _CaptureGH(metadata={"changed": ["backend/auth.py"], "changed_ranges": {},
                              "added_paths": [], "conflict_markers": []})
    touched = _post_refreshes(
        gh, REPO, cochange_refresh,
        db=_db_for(cochange_impact, cochange=parity_cc, calls=cochange_calls), branch="main")
    check(touched == 1 and cochange_calls.get("cochange") == 1
          and gh.checks.get(27, {}).get("title") == cochange_actor["title"]
          and cochange_actor["comment"] in gh.comments.get(27, "")
          and "Historically changes together" in gh.comments.get(27, ""),
          "co-change-only Clear refresh preserves the actor's comment/title and does not take clear-reset")

    # The actor appends the stale-base line to the CHECK SUMMARY after comparing the PR base to current main.
    # Neighbor refresh must perform the same bounded compare and keep the suffix byte-identical.
    stale_parity_impact = _impact_for("PR-28", "clear", paths=["backend/auth.py"])
    stale_parity_refresh = W._refresh_changes(stale_parity_impact, exclude_change="PR-SIB")
    stale_line = R.stale_base_nudge_line(["backend/auth.py"], ["backend/auth.py"])
    stale_actor = R.render_pr_check(stale_parity_impact, "PR-28")
    expected_stale_summary = stale_actor["summary"] + "\n\n" + stale_line
    gh = _CaptureGH(has_prior_comment=True,
                    metadata={"changed": ["backend/auth.py"], "changed_ranges": {},
                              "added_paths": [], "conflict_markers": []},
                    compare_paths=["backend/auth.py"])
    touched = _post_refreshes(
        gh, REPO, stale_parity_refresh, db=_db_for(stale_parity_impact), branch="main")
    check(touched == 1 and gh.compare_calls == 1
          and gh.checks.get(28, {}).get("summary") == expected_stale_summary
          and "main` moved under you" in gh.checks.get(28, {}).get("summary", ""),
          "stale-base neighbor refresh preserves the actor's compare-derived summary suffix")

    # Both actor suffixes can coexist. Their order is part of the check-run byte contract: base render, then
    # coverage, then stale-base. Reversing two otherwise-correct lines causes permanent actor/neighbor PATCH
    # churn. Run the exact same refresh twice and prove the second check upsert is a byte-identical no-op.
    dual_impact = _impact_for("PR-46", "warn", paths=["backend/auth.py"], impact=["backend/api.py"],
                              contested_with=["alice PR-9"])
    dual_refresh = W._refresh_changes(dual_impact, exclude_change="PR-SIB")
    coverage_surface = {"plan": "early-access", "file_limit": 100, "file_count": 120,
                        "over_by": 20, "near": False}
    coverage_line = R.coverage_nudge_line(coverage_surface)
    dual_stale_line = R.stale_base_nudge_line(["backend/auth.py"], ["backend/auth.py"])
    # PR-46 is a MATERIAL warn+contested coupling, so BOTH the acting and the neighbor paths pause it via
    # apply_pause_ack — and the pause overlay is applied AFTER the coverage/stale suffixes (the exact webhook.py /
    # _post_refreshes order). Mirror that flow to build the reference: base render → coverage → stale-base →
    # pause overlay (which prefixes the paused summary with a "Paused — …" lead). The suffix ORDER (coverage
    # before stale-base) is still the byte contract under test; the pause prefix is applied identically on both
    # paths, so actor and neighbor stay byte-identical (no PATCH churn).
    dual_actor = R.render_pr_check(dual_impact, "PR-46", cochange=[])
    dual_actor["summary"] += "\n\n" + coverage_line + "\n\n" + dual_stale_line
    dual_actor = R.apply_pause_ack(dual_actor, dual_impact, "PR-46",
                                   label_present=False, prior_hash=None, branch="main")
    dual_actor_summary = dual_actor["summary"]
    gh = _CaptureGH(has_prior_comment=True, compare_paths=["backend/auth.py"])
    dual_db = _db_for(dual_impact, coverage=coverage_surface)
    touched = _post_refreshes(gh, REPO, dual_refresh, db=dual_db, branch="main")
    first_check_writes = gh.check_writes
    posted_dual_summary = gh.checks.get(46, {}).get("summary", "")
    check(touched == 1 and first_check_writes == 1 and posted_dual_summary == dual_actor_summary
          and posted_dual_summary.index(coverage_line) < posted_dual_summary.index(dual_stale_line),
          "coverage+stale neighbor summary is byte-identical to actor order (coverage before stale-base)")
    touched_again = _post_refreshes(gh, REPO, dual_refresh, db=dual_db, branch="main")
    check(touched_again == 1 and gh.check_writes == first_check_writes
          and gh.checks.get(46, {}).get("summary") == dual_actor_summary,
          "re-running the byte-identical coverage+stale refresh performs zero additional check PATCH writes")

    # Fork/uncertain-repo identity uses the actor's broad redaction decision: neither base-repo co-change paths
    # nor main-since-base paths may be queried or disclosed on the external contributor's conversation.
    fork_parity_impact = _impact_for(
        "PR-29", "serialize", paths=["backend/auth.py"], serialize_behind=["maint PR-99"],
        collision_points=[{"behind": "maint PR-99", "path": "private/internal.py", "symbol": "secret_fn"}])
    fork_parity_refresh = W._refresh_changes(fork_parity_impact, exclude_change="PR-SIB")
    fork_actor = R.render_pr_check(fork_parity_impact, "PR-29", is_fork=True, cochange=parity_cc)
    fork_calls = {}
    gh = _CaptureGH(fork_prs={29}, compare_paths=["backend/auth.py"],
                    metadata={"changed": ["backend/auth.py"], "changed_ranges": {},
                              "added_paths": [], "conflict_markers": []})
    touched = _post_refreshes(
        gh, REPO, fork_parity_refresh,
        db=_db_for(fork_parity_impact, cochange=parity_cc, calls=fork_calls), branch="main")
    fork_parity_body = gh.comments.get(29, "")
    fork_parity_summary = gh.checks.get(29, {}).get("summary", "")
    check(touched == 1 and fork_calls.get("cochange", 0) == 0 and gh.compare_calls == 0
          and gh.checks.get(29, {}).get("title") == fork_actor["title"]
          and "backend/policy.py" not in fork_parity_body
          and "Historically changes together" not in fork_parity_body
          and "main` moved under you" not in fork_parity_summary,
          "fork evidence refresh queries/discloses neither co-change nor stale-base actor inputs")

    # AUXILIARY-EVIDENCE FAIL-CLOSED: preserving actor/neighbor parity requires all three non-surface reads. A
    # transient co-change, strict-compare, or coverage failure must preserve the last authoritative GitHub state
    # rather than silently stripping an existing advisory/suffix with a partial refresh.
    aux_impact = _impact_for("PR-30", "warn", paths=["backend/auth.py"], impact=["backend/api.py"],
                             contested_with=["alice PR-9"])
    aux_refresh = W._refresh_changes(aux_impact, exclude_change="PR-SIB")

    aux_calls = {}
    gh = _CaptureGH(has_prior_comment=True)
    touched = _post_refreshes(
        gh, REPO, aux_refresh,
        db=_db_for(aux_impact, calls=aux_calls, cochange_error=RuntimeError("cochange unavailable")),
        branch="main")
    check(touched == 0 and aux_calls.get("cochange") == 1 and not gh.checks and not gh.comments,
          "co-change evidence failure preserves the prior surface with zero writes")

    aux_calls = {}
    gh = _CaptureGH(has_prior_comment=True, compare_error=RuntimeError("compare unavailable"))
    touched = _post_refreshes(
        gh, REPO, aux_refresh, db=_db_for(aux_impact, calls=aux_calls), branch="main")
    check(touched == 0 and gh.compare_calls == 1 and not gh.checks and not gh.comments,
          "strict stale-base compare failure preserves the prior surface with zero writes")

    aux_calls = {}
    gh = _CaptureGH(has_prior_comment=True)
    touched = _post_refreshes(
        gh, REPO, aux_refresh,
        db=_db_for(aux_impact, calls=aux_calls, coverage_error=RuntimeError("coverage unavailable")),
        branch="main")
    check(touched == 0 and aux_calls.get("coverage") == 1 and not gh.checks and not gh.comments,
          "coverage evidence failure preserves the prior surface with zero writes")

    # The new PR+Files reads live INSIDE the existing refresh-entry cap. Even entries that no-op at clear-reset
    # cannot trigger evidence reads beyond the configured fan-out bound.
    old_refresh_cap = S._NEIGHBOR_REFRESH_CAP
    try:
        S._NEIGHBOR_REFRESH_CAP = 2
        cap_impact = {"repo": REPO, "branch": "main", "changes": [
            {"change_id": f"PR-{n}", "label": f"dev PR-{n}", "agent": "dev",
             "verdict": "clear", "paths": ["backend/auth.py"], "head_sha": f"sha-{n}"}
            for n in (31, 32, 33)
        ]}
        cap_refresh = W._refresh_changes(cap_impact)
        gh = _CaptureGH(metadata={"changed": ["backend/auth.py"], "changed_ranges": {},
                                  "added_paths": [], "conflict_markers": []})
        _post_refreshes(gh, REPO, cap_refresh, db=_db_for(cap_impact), branch="main")
        check(gh.pr_reads == 4 and gh.metadata_calls == 2,
              "live evidence PR+Files reads remain bounded by the existing neighbor-refresh cap")
    finally:
        S._NEIGHBOR_REFRESH_CAP = old_refresh_cap

    # MULTI-PAGE SHARED BUDGET: first PR reserves two Files pages, the next two-page PR cannot fit in the one-page
    # remainder and is skipped before Files, while a later one-page PR can use that remainder. This is the live
    # db-backed branch (not the legacy db=None seam), proving the event cannot become cap×30 pages.
    class _BudgetGH(_CaptureGH):
        def __init__(self, specs):
            super().__init__()
            self.specs = specs
            self.metadata_numbers = []
            self.max_pages_seen = []

        def get_pull_request(self, repo, number):
            self.pr_reads += 1
            return {"changed_files": len(self.specs[number]), "state": "open",
                    "head": {"sha": f"sha-{number}", "repo": {"id": 1}},
                    "base": {"ref": "main", "sha": "base-sha", "repo": {"id": 1}}}

        def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
            self.metadata_calls += 1
            self.metadata_numbers.append(number)
            self.max_pages_seen.append(max_pages)
            paths = self.specs[number]
            return {"changed": paths, "changed_ranges": {}, "added_paths": [], "conflict_markers": [],
                    "raw_entry_count": len(paths)}

    budget_specs = {
        41: [f"src/a{i}.py" for i in range(200)],
        42: [f"src/b{i}.py" for i in range(200)],
        43: [f"src/c{i}.py" for i in range(100)],
    }
    budget_impact = {"repo": REPO, "branch": "main", "changes": [
        {"change_id": f"PR-{n}", "label": f"dev PR-{n}", "agent": "dev",
         "verdict": "clear", "paths": paths, "head_sha": f"sha-{n}"}
        for n, paths in budget_specs.items()
    ]}
    old_refresh_cap = S._NEIGHBOR_REFRESH_CAP
    try:
        S._NEIGHBOR_REFRESH_CAP = 3
        gh = _BudgetGH(budget_specs)
        _post_refreshes(gh, REPO, W._refresh_changes(budget_impact),
                        db=_db_for(budget_impact), branch="main")
        check(gh.metadata_numbers == [41, 43] and gh.max_pages_seen == [2, 1]
              and gh.pr_reads == 5,
              "shared Files-page budget reserves 2+1 pages, skips the over-budget middle PR before traversal")
    finally:
        S._NEIGHBOR_REFRESH_CAP = old_refresh_cap

    # GET(A) -> Files -> GET(B): never post evidence read from B onto A's check. A deterministic head flip between
    # the two authoritative PR reads must stop before comment/check/clear-reset writes.
    class _HeadRaceGH(_CaptureGH):
        def get_pull_request(self, repo, number):
            result = super().get_pull_request(repo, number)
            result["head"]["sha"] = "head-A" if self.pr_reads == 1 else "head-B"
            return result

    race_impact = _impact_for("PR-44", "warn", paths=["backend/auth.py"], impact=["backend/api.py"],
                              contested_with=["alice PR-9"])
    race_refresh = W._refresh_changes(race_impact, exclude_change="PR-SIB")
    gh = _HeadRaceGH(metadata={"changed": ["backend/auth.py"], "changed_ranges": {},
                               "added_paths": [], "conflict_markers": []}, has_prior_comment=True)
    touched = _post_refreshes(gh, REPO, race_refresh, db=_db_for(race_impact), branch="main")
    check(gh.pr_reads == 2 and gh.metadata_calls == 1 and touched == 0 and not gh.checks and not gh.comments,
          "A→B head race: second PR read detects the change and suppresses every stale write")

    class _BaseRaceGH(_CaptureGH):
        def get_pull_request(self, repo, number):
            result = super().get_pull_request(repo, number)
            result["base"]["sha"] = "base-A" if self.pr_reads == 1 else "base-B"
            return result

    gh = _BaseRaceGH(metadata={"changed": ["backend/auth.py"], "changed_ranges": {},
                               "added_paths": [], "conflict_markers": []}, has_prior_comment=True)
    touched = _post_refreshes(gh, REPO, race_refresh, db=_db_for(race_impact), branch="main")
    check(gh.pr_reads == 2 and gh.metadata_calls == 1 and touched == 0 and not gh.checks and not gh.comments,
          "base A→B race: second PR read detects the base commit change and suppresses every stale write")

    # CLAIM-HEAD COHERENCE: unlike the path-set guard, the analyzed head distinguishes two commits that touch the
    # SAME path with different line ranges. Model stable/current GitHub B while main_impact_surface still carries
    # the prior A claim. Even though paths match exactly and the B metadata traversal is complete, no stale-A
    # comment/check/reset may be written. Once the DB surface is restamped to B, the same evidence can post. A
    # legacy/unproven NULL stamp is also fail-closed until that PR's own webhook/backfill reconciles it.
    head_bound_meta = {
        "changed": ["backend/auth.py"],
        "changed_ranges": {"backend/auth.py": [[80, 96]]},  # B moved the edit within the same file
        "added_paths": [],
        "conflict_markers": [{"path": "backend/auth.py", "line": 88, "kind": "start"}],
    }
    stale_head_impact = _impact_for("PR-45", "clear", paths=["backend/auth.py"], head_sha="sha-old-A")
    stale_head_refresh = W._refresh_changes(stale_head_impact, exclude_change="PR-SIB")
    gh = _CaptureGH(metadata=head_bound_meta, has_prior_comment=True)  # stable GitHub B = sha-45 on both PR reads
    touched = _post_refreshes(
        gh, REPO, stale_head_refresh, db=_db_for(stale_head_impact), branch="main")
    check(gh.pr_reads == 2 and gh.metadata_calls == 1 and touched == 0 and not gh.checks and not gh.comments,
          "stable GitHub B + stale DB A on the SAME path/range-changing PR suppresses every refresh write")

    current_head_impact = _impact_for("PR-45", "clear", paths=["backend/auth.py"], head_sha="sha-45")
    current_head_refresh = W._refresh_changes(current_head_impact, exclude_change="PR-SIB")
    gh = _CaptureGH(metadata=head_bound_meta, has_prior_comment=True)
    touched = _post_refreshes(
        gh, REPO, current_head_refresh, db=_db_for(current_head_impact), branch="main")
    check(gh.pr_reads == 2 and gh.metadata_calls == 1 and touched == 1
          and gh.checks.get(45, {}).get("conclusion") == "action_required"
          and "backend/auth.py" in gh.comments.get(45, ""),
          "stable GitHub B + DB B resumes the evidence-complete refresh on that same path")

    null_head_impact = _impact_for("PR-45", "clear", paths=["backend/auth.py"], head_sha=None)
    null_head_refresh = W._refresh_changes(null_head_impact, exclude_change="PR-SIB")
    gh = _CaptureGH(metadata=head_bound_meta, has_prior_comment=True)
    touched = _post_refreshes(
        gh, REPO, null_head_refresh, db=_db_for(null_head_impact), branch="main")
    check(gh.pr_reads == 2 and gh.metadata_calls == 1 and touched == 0 and not gh.checks and not gh.comments,
          "NULL/unproven DB claim head suppresses every refresh write until the PR is restamped")

    # Malformed DB JSON and renderer failures are per-neighbor fail-closed conditions, never event-level crashes.
    def _bad_json_db(sql, args=()):
        return "{not-json" if "main_impact_surface" in str(sql) else None

    gh = _CaptureGH(metadata={"changed": ["backend/auth.py"], "changed_ranges": {},
                              "added_paths": [], "conflict_markers": []})
    touched = _post_refreshes(gh, REPO, race_refresh, db=_bad_json_db, branch="main")
    check(touched == 0 and not gh.checks and not gh.comments,
          "malformed impact JSON is isolated and skips writes without escaping")

    real_render = R.render_pr_check
    try:
        def _render_boom(*args, **kwargs):
            raise RuntimeError("deterministic render failure")
        R.render_pr_check = _render_boom
        gh = _CaptureGH(metadata={"changed": ["backend/auth.py"], "changed_ranges": {},
                                  "added_paths": [], "conflict_markers": []})
        touched = _post_refreshes(gh, REPO, race_refresh, db=_db_for(race_impact), branch="main")
        render_isolated = touched == 0 and not gh.checks and not gh.comments
    finally:
        R.render_pr_check = real_render
    check(render_isolated, "renderer exception is isolated and skips writes without escaping")

    malformed_isolated = True
    for malformed in (None, {"conclusion": "neutral"}):
        try:
            R.render_pr_check = lambda *args, _v=malformed, **kwargs: _v
            gh = _CaptureGH(metadata={"changed": ["backend/auth.py"], "changed_ranges": {},
                                      "added_paths": [], "conflict_markers": []})
            touched = _post_refreshes(gh, REPO, race_refresh, db=_db_for(race_impact), branch="main")
            malformed_isolated = malformed_isolated and touched == 0 and not gh.checks and not gh.comments
        finally:
            R.render_pr_check = real_render
    check(malformed_isolated, "None/missing-key renderer returns are rejected and never escape or write")

    fork_calls = 0
    def _malformed_fork_render(*args, **kwargs):
        nonlocal fork_calls
        fork_calls += 1
        return real_render(*args, **kwargs) if fork_calls == 1 else None
    try:
        R.render_pr_check = _malformed_fork_render
        gh = _CaptureGH(metadata={"changed": ["backend/auth.py"], "changed_ranges": {},
                                  "added_paths": [], "conflict_markers": []})
        touched = _post_refreshes(gh, REPO, race_refresh, db=_db_for(race_impact), branch="main")
        malformed_fork_isolated = touched == 0 and not gh.checks and not gh.comments
    finally:
        R.render_pr_check = real_render
    check(malformed_fork_isolated, "malformed fork renderer return is rejected before any write")

    # READ FAILURE / PROVABLE SHORTFALL: empty evidence must never be substituted. Preserve the last authoritative
    # GitHub surface by doing zero comment/check/reset writes for just this PR.
    failed_impact = _impact_for("PR-24", "warn", paths=["backend/auth.py"], impact=["backend/api.py"],
                                contested_with=["alice PR-9"])
    failed_refresh = W._refresh_changes(failed_impact, exclude_change="PR-SIB")
    for desc, error in (("metadata exception", RuntimeError("files unavailable")),
                        ("metadata shortfall", PRFilesShortfall(1, 2))):
        gh = _CaptureGH(metadata_error=error, has_prior_comment=True)
        touched = _post_refreshes(gh, REPO, failed_refresh, db=_db_for(failed_impact), branch="main")
        check(gh.pr_reads == 1 and gh.metadata_calls == 1 and touched == 0 and not gh.checks and not gh.comments,
              f"{desc}: refresh skips every write rather than overwriting from empty/partial evidence")
    gh = _CaptureGH(metadata={"changed": ["backend/different.py"], "changed_ranges": {},
                              "added_paths": [], "conflict_markers": []}, has_prior_comment=True)
    touched = _post_refreshes(gh, REPO, failed_refresh, db=_db_for(failed_impact), branch="main")
    check(touched == 0 and not gh.checks and not gh.comments,
          "claim/files mismatch: refresh skips writes instead of mixing current metadata with stale impact claims")

    # ── (B) RAW CO-CHANGE COUNT LEAK: no customer-facing co-change string carries "(N of M changes)" ────────────
    # cover clear / warn / serialize that ALSO carry a co-change signal, the degenerate-row mix, AND a fork PR.
    base = {"repo": REPO, "branch": "main"}
    cc_scenarios = [
        ("co-change clear", {**base, "changes": [{"change_id": "PR-1", "label": "dev PR-1", "agent": "dev",
            "verdict": "clear", "paths": ["backend/auth.py"]}]}, "PR-1", CC, False),
        ("co-change warn", {**base, "changes": [{"change_id": "PR-2", "label": "bob PR-2", "agent": "bob",
            "verdict": "warn", "paths": ["backend/auth.py"], "impact": ["backend/handlers.py"],
            "contested_with": ["alice PR-1"]}]}, "PR-2", CC, False),
        ("co-change serialize", {**base, "changes": [{"change_id": "PR-3", "label": "carol PR-3", "agent": "carol",
            "verdict": "serialize", "paths": ["backend/auth.py"], "serialize_behind": ["bob PR-2"],
            "collision_points": [{"behind": "bob PR-2", "path": "backend/auth.py", "symbol": "login",
                                  "line_lo": 5, "line_hi": 20}]}]}, "PR-3", CC, False),
        # degenerate rows mixed in (prob 0 / null path) — the renderer drops them; the kept line must still be clean
        ("co-change clear (degenerate rows mixed in)", {**base, "changes": [{"change_id": "PR-1",
            "label": "dev PR-1", "agent": "dev", "verdict": "clear", "paths": ["backend/auth.py"]}]}, "PR-1",
            CC + [{"edited": "x.py", "partner": "y.py", "prob": 0, "lift": 1.0, "co": 0, "n": 5},
                  {"edited": None, "partner": None, "prob": 0.9, "lift": 5.0, "co": 9, "n": 10}], False),
        # a FORK serialize PR (the redacted path) — it must not show co-change at all, and certainly no raw count
        ("co-change serialize (FORK, redacted)", {**base, "changes": [{"change_id": "PR-9", "label": "ext PR-9",
            "agent": "ext", "verdict": "serialize", "paths": ["backend/auth.py"],
            "serialize_behind": [{"change_id": "PR-1", "agent": "maint"}]}]}, "PR-9", CC, True),
    ]
    rendered_a_cochange_line = False
    for desc, impact, ref, cc, is_fork in cc_scenarios:
        out = R.render_pr_check(impact, ref, is_fork=is_fork, cochange=cc)
        for field in ("title", "summary", "comment"):
            text = out.get(field) or ""
            m = _RAW_COUNT_RE.search(text)
            check(m is None,
                  f"[{desc}] no raw co-change count '(N of M changes)' in {field}"
                  + (f"  (LEAKED {m.group(0)!r})" if m else ""))
            # the literal "of 15 changes" from the CC fixture must be gone specifically (belt-and-suspenders)
            check("11 of 15" not in text and "of 15 changes" not in text,
                  f"[{desc}] the specific raw support '11 of 15' / 'of 15 changes' is absent from {field}")
        body = out.get("comment") or ""
        if not is_fork and "Historically changes together" in body:
            rendered_a_cochange_line = True
            # PRECISION: the ban is on the RAW COUNT AND the literal "N%" (honest-copy, 2026-06-25) — the LIFT
            # MUST survive (a positive check so we know we didn't gut the whole line). The "% of the time"
            # segment was dropped because at strong couplings it saturated as "**100%** of the time" — a literal
            # customer-facing guarantee.
            check("4.5×" in body,
                  f"[{desc}] the co-change line KEEPS the lift (4.5×) — the interpretable, uncapped signal")
            check("more than chance" in body,
                  f"[{desc}] the co-change line keeps the lift phrasing ('× more than chance')")
            import re as _re
            check(_re.search(r"\b\d+%", body) is None and "of the time" not in body,
                  f"[{desc}] the co-change line does NOT print a literal 'N%' / 'N% of the time' (honest-copy)")
    # the fork scenario: co-change is NOT rendered at all (base-repo partner paths redacted)
    fork_out = R.render_pr_check(cc_scenarios[-1][1], "PR-9", is_fork=True, cochange=CC)
    check("Historically changes together" not in (fork_out.get("comment") or "")
          and "backend/api.py" not in (fork_out.get("comment") or ""),
          "a FORK PR shows NO co-change block (base-repo partner paths redacted) — so no raw count can leak there")

    check(rendered_a_cochange_line,
          "coverage: at least one real co-change advisory line was rendered + scanned (the surface this gate locks)")

    print("REFRESH-TITLE + CO-CHANGE-COUNT GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    raise SystemExit(main())
