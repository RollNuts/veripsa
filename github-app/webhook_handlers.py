#!/usr/bin/env python3
"""Veripsa GitHub App — the WEBHOOK EVENT-ROUTING BRAIN (extracted from server.py).

This is the ORCHESTRATOR of the webhook brain (itself the single biggest cohesive cluster of the old server.py
god-file — the most-churned file in the repo, the repeated merge-conflict source, e.g. #181/#190): the
`handle_event` router that maps a verified webhook to the brain (webhook.handle_pull_request) + renderer and posts
the result to GitHub, its per-event handlers (_handle_installation_event / _handle_repository_event / _pr_guard /
_handle_pull_request_event), the check/comment-refresh poster that overlays the pause-ack tier on each neighbor
(_post_refreshes), the rerun/merge-queue replay loop (_rerun_prs), and the push-time lane reservation
(reserve_branch_lanes). handle_event is PURE over an injected `db(sql,args)` + a `gh` client (the whole loop is
testable offline — tests/test_server.py drives server.handle_event with a Fake client + a real DB).

The content-free coercion / guard / id-builder leaf (_as_obj / _as_list / _code_paths / _push_changed_sets / the
branch- and change-id builders / _quota_result / the merge_group ref parser + check renderer / the re-run replay
builder) lives in webhook_coercion.py, and the never-crash GitHub-post helpers (_safe_upsert_check / _head_and_fork
/ _prior_ack_snapshot / _post_watching_signal / _default_branch_for) live in webhook_posters.py — two cohesive
sibling leaves lifted OUT of this file (mirror of the github_rest / _cg_schema surface splits). BOTH are RE-EXPORTED
at the top of this module (so `webhook_handlers.X` keeps resolving). _post_refreshes + the per-event handlers stay
HERE because they read the monkeypatch-bound `_optional` / `apply_pause_ack` overlay seam (the pause-ack tests
rebind `webhook_handlers._optional` / `webhook_handlers.apply_pause_ack` and reach the bindings those overlays
call) and the live-`server` caps via _server(); the extracted leaves touch neither. The three event-handling caps
(_MAX_PR_FILES / _FAILING_CONCLUSIONS / _RERUN_PR_CAP) stay defined on server.py.

This file changes when a new event TYPE is handled or its routing logic changes — a DIFFERENT reason than the
HTTP/queue/worker plumbing that stays in server.py (signature verify, bounded body read, the daemon worker +
watchdog, serve()'s HTTP handler, the boot/backfill CLI). Splitting the brain out of the plumbing cuts the
conflict surface that made server.py the #1 hotspot.

It imports NOTHING from server.py at LOAD time (no circular import — server.py imports THIS and RE-EXPORTS every
name below for backward compatibility, so `server.handle_event` / `server._as_obj` / `server._code_paths` / …
keep resolving for serve(), the gates, and the tests that do `from server import X` / `server.X`). The few
server-side seams the lazy back-references in ingest.py / event_processor.py reach (`server._as_obj`,
`server._as_list`, `server._push_changed_sets`, `server._push_author_is_bot`, `server._quota_result`,
`server.handle_event`) all resolve through those re-exports. The brain's OWN dependencies (the proven
brain/renderer, the ingest cluster) are imported DIRECTLY here from their leaf modules (which import nothing
from server.py — no cycle), using the same standalone/package dual-import idiom server.py uses. The three
event-handling CAPS (_MAX_PR_FILES / _FAILING_CONCLUSIONS / _RERUN_PR_CAP) stay defined on server.py — the
codebase's tests rebind them on `server` at runtime — and are read here at CALL time via _server() (below).
"""
from __future__ import annotations
import contextvars
import inspect
import json  # noqa: F401  (used by handle_event's tolerant result parsing)
import os     # marketplace-billing kill switch (VERIPSA_MARKETPLACE_BILLING) — read at CALL time, see handle_event
from datetime import datetime

try:
    import event_budget as _event_budget
except ImportError:  # imported as a package
    from . import event_budget as _event_budget

try:
    from webhook import (handle_pull_request, refresh_inflight, _bounded_claim_id, _CHANGE_ID_CAP, _optional,  # noqa: F401
                         _ensure_trace_id, _trace_of, _trace_log_prefix, _TRACE_TAG_WIDTH)  # noqa: F401
except ImportError:  # imported as a package
    from .webhook import (handle_pull_request, refresh_inflight, _bounded_claim_id, _CHANGE_ID_CAP, _optional,  # noqa: F401
                          _ensure_trace_id, _trace_of, _trace_log_prefix, _TRACE_TAG_WIDTH)  # noqa: F401

# Keep in sync with webhook._PR_ANALYZE_ACTIONS. Some regression tests stub the
# webhook module with only the handler functions, so this orchestrator cannot
# import the constant from webhook at module import time.
_PR_ANALYZE_ACTIONS = ("opened", "synchronize", "reopened", "ready_for_review", "converted_to_draft")
try:
    from render import (cleared_comment_body, watching_check, quota_paused_check,
                        quota_paused_comment_body, unread_files_check, unread_files_comment_body,
                        stale_graph_unknown_check, stale_graph_unknown_comment_body,
                        stale_graph_head_unknown_check, stale_graph_head_unknown_comment_body,
                        apply_pause_ack, prior_snapshot_from_comment, ACK_LABEL)
except ImportError:  # imported as a package
    from .render import (cleared_comment_body, watching_check, quota_paused_check,
                         quota_paused_comment_body, unread_files_check, unread_files_comment_body,
                         stale_graph_unknown_check, stale_graph_unknown_comment_body,
                         stale_graph_head_unknown_check, stale_graph_head_unknown_comment_body,
                         apply_pause_ack, prior_snapshot_from_comment, ACK_LABEL)
# The graph-ingestion + onboarding cluster (ingest.py) imports NOTHING from server.py at LOAD time and reaches
# the few server-side seams it needs lazily — so importing the entry points handle_event calls DIRECTLY here is
# cycle-free (webhook_handlers.py ← ingest.py is one-directional; ingest only does a CALL-time `import server`).
try:
    from ingest import (_queue_onboard_repos, purge_repo, purge_account_working_set,
                        ingest_push_deferred, request_main_graph_refresh,
                        request_main_graph_refresh_wake_only,
                        _PLANNED_ONBOARDING_GRAPH_PROOF,
                        _PLANNED_ONBOARDING_BOUNDED_UNREAD_KEY)
except ImportError:  # imported as a package
    from .ingest import (_queue_onboard_repos, purge_repo, purge_account_working_set,
                         ingest_push_deferred, request_main_graph_refresh,
                         request_main_graph_refresh_wake_only,
                         _PLANNED_ONBOARDING_GRAPH_PROOF,
                         _PLANNED_ONBOARDING_BOUNDED_UNREAD_KEY)
# graph_freshness.py is a LEAF (imports only env_config; NOTHING from server.py), so a top-level import here is
# cycle-free. Used by the G5 push-refresh path to derive the degraded-graph signal (a push has no self-heal /
# graph_heal in scope, but a deferred/failed push-ingest can still leave main's stored graph BEHIND HEAD).
try:
    from graph_freshness import graph_freshness
except ImportError:  # imported as a package
    from .graph_freshness import graph_freshness
try:
    from github_rest_prread import (PRFilesMalformed, PRFilesPageBudgetExceeded,
                                    PRFilesShortfall)  # pagination sentinels (false-clear + fan-out budget)
except ImportError:  # imported as a package
    from .github_rest_prread import PRFilesMalformed, PRFilesPageBudgetExceeded, PRFilesShortfall
# The CONTENT-FREE COERCION / GUARD / ID-BUILDER leaf (webhook_coercion.py) + the GITHUB-POST helpers
# (webhook_posters.py) — the two cohesive clusters lifted OUT of this orchestrator (mirror of the github_rest /
# _cg_schema surface splits). Both import NOTHING from THIS module (one-directional: webhook_handlers ←
# webhook_posters ← webhook_coercion — no cycle), and NEITHER touches the monkeypatch-bound `_optional` /
# `apply_pause_ack` overlay seam or the live-`server` caps (those stay HERE with the per-event handlers +
# _post_refreshes, so the tests that rebind `webhook_handlers._optional` / `webhook_handlers.apply_pause_ack` keep
# reaching the bindings those overlays actually call). EVERY name is RE-EXPORTED below (`# noqa: F401`) so
# `webhook_handlers.X` — and, through server.py's own re-export, `server.X` — keeps resolving for serve(), the
# gates, and the tests that reach these by name (test_server / test_merge_group / test_rerun_fork_safety /
# test_hostile_path_poison / … do `import webhook_handlers as W/WH/H` and reach W._code_paths / W._rerun_replay /
# W._merge_queue_pr_numbers / …).
try:
    from webhook_coercion import (  # noqa: F401  (re-exported — part of webhook_handlers' public surface)
        _change_id, _as_obj, _as_list, _as_int, _branch_from_ref, _branch_change_id, _branch_claim_id,
        _quota_result, _comment_marker, _marked_comment, _ack_label_present, _event_installation_id,
        _install_is_suspended, _marketplace_plan_name, _marketplace_effective_date, _code, _code_paths,
        _push_changed_sets,
        _push_author_is_bot, _pr_number_from_change, _merge_queue_pr_numbers, _render_merge_group_check,
        _rerun_replay)
except ImportError:  # imported as a package
    from .webhook_coercion import (  # noqa: F401
        _change_id, _as_obj, _as_list, _as_int, _branch_from_ref, _branch_change_id, _branch_claim_id,
        _quota_result, _comment_marker, _marked_comment, _ack_label_present, _event_installation_id,
        _install_is_suspended, _marketplace_plan_name, _marketplace_effective_date, _code, _code_paths,
        _push_changed_sets,
        _push_author_is_bot, _pr_number_from_change, _merge_queue_pr_numbers, _render_merge_group_check,
        _rerun_replay)
try:
    from webhook_posters import (  # noqa: F401  (re-exported — part of webhook_handlers' public surface)
        _safe_upsert_check, _upsert_check_result, _head_and_fork, _prior_ack_snapshot, _post_watching_signal,
        _default_branch_for)
except ImportError:  # imported as a package
    from .webhook_posters import (  # noqa: F401
        _safe_upsert_check, _upsert_check_result, _head_and_fork, _prior_ack_snapshot, _post_watching_signal,
        _default_branch_for)


def _delivery_of(payload) -> str:
    """Read the durable delivery key threaded by delivery_queue.with_delivery_key.

    Content-free: GitHub's delivery id (or the local fallback hash), never a payload body."""
    if isinstance(payload, dict):
        v = payload.get("_veripsa_delivery_key")
        if isinstance(v, str) and v:
            return v[:200]
    return ""


_CHECK_OPERATION_KEY = "_veripsa_check_operation"
_PR_SURFACE_OPERATION_KEY = "_veripsa_pr_surface_operation"
_ANALYSIS_VERDICT_KEY = "_veripsa_analysis_verdict"  # kept in sync with webhook._ANALYSIS_VERDICT_KEY
_MERGE_QUEUE_PROVEN_VERDICTS = frozenset(("clear", "warn", "serialize_soft", "serialize"))
# A requested suite normally names one PR, but a shared commit can name several.  The normal replay cap bounds
# expensive full analyses; strict exact-target proof may inspect beyond it so a stale/fork entry at the front
# cannot hide the current PR.  Keep even that proof scan hard-bounded against a signed-but-pathological payload.
# Hitting this ceiling raises for durable retry rather than authorizing a Check-less delivery.
_CHECK_SUITE_REQUESTED_PROOF_CAP = 100


def _attach_check_operation(result: dict, repo: str, sha: str, check_meta) -> dict:
    """Attach the exact acting-Check outcome for an in-process replay caller.

    `_upsert_check_result` intentionally stays fail-soft for ordinary webhook paths.  The one caller that must
    distinguish "the replay returned" from "the required Check was actually posted" is Veripsa's own
    `check_suite.requested` backstop.  Keep that distinction private to the returned event rather than changing
    the public GitHub poster contract or making every event retry on a transient Check API failure.
    """
    if isinstance(result, dict) and isinstance(check_meta, dict):
        result[_CHECK_OPERATION_KEY] = {
            "repo": repo,
            "sha": sha,
            "posted": check_meta.get("posted") is True,
        }
    return result


def _http_status(exc: Exception) -> int | None:
    """Best-effort HTTP status extraction without coupling the router to one REST exception type."""
    for owner in (exc, getattr(exc, "response", None)):
        if owner is None:
            continue
        for name in ("code", "status", "status_code"):
            value = getattr(owner, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def _comment_url(repo: str, pr_number: int | None, comment_id) -> str:
    if pr_number is None or comment_id in (None, ""):
        return ""
    return f"https://github.com/{repo}/issues/{pr_number}#issuecomment-{comment_id}"


def _cleared_comment_preserving_ack(existing_body: str, branch: str) -> str:
    """Clear visible coupling prose without erasing the last ACK identity marker.

    A retained ``veripsa-ack`` label must stay bound to the snapshot it actually acknowledged. If Clear removed
    the marker, a later different coupling could bind that old label to a new snapshot after one automatic event.
    Keeping the invisible prior marker makes the next different coupling positively stale and forces a real re-ACK.
    """
    body = cleared_comment_body(branch)
    prior = prior_snapshot_from_comment(existing_body)
    return (f"<!-- veripsa-ack-snap:{prior} -->\n" + body) if prior else body


def _surface_result(check_meta: dict, *, comment_needed: bool, comment_ok: bool) -> str:
    check_ok = bool((check_meta or {}).get("posted"))
    if check_ok and comment_needed and comment_ok:
        return "posted"
    if check_ok:
        return "check-only"
    if comment_needed and comment_ok:
        return "comment-only"
    return "failed"


def _signal_token(conclusion, verdict=None, paused: bool = False) -> str:
    """Map an already-computed Veripsa check outcome to the BOUNDED A4 activation signal enum recorded with the
    'check_published' event (Issue #648): 'clear' | 'heads_up' | 'wait_in_line' | 'unknown' | 'paused'.

    Content-free by construction — it reads ONLY the GitHub check conclusion, the raw engine verdict token, and
    whether a pause-ack overlay paused this PR; NEVER a title/body/summary. Any unrecognized shape collapses to
    'unknown' (the DB recorder re-clamps to the same allow-list, so an unexpected value can never leak)."""
    if conclusion == "success":
        return "clear"
    if conclusion == "action_required":
        return "paused" if paused else "wait_in_line"
    # neutral (advisory) — distinguish a warn (heads up) from a serialize / honest-unknown when the verdict is known.
    if verdict == "warn":
        return "heads_up"
    if verdict in ("serialize", "serialize_soft"):
        return "wait_in_line"
    return "unknown"


def _log_pr_surface(trace_prefix: str, *, surface: str, delivery: str = "", event: str = "pull_request",
                    action: str = "", repo: str, pr_number: int | None, head_sha: str = "",
                    comment_needed: bool = False, comment_ok: bool = False, comment_id=None,
                    check_meta: dict | None = None) -> None:
    """One normalized, content-free operator log for the PR-visible surface.

    The values are stable handles only: a random trace id, delivery presence, repo coordinate, PR number, sha
    prefix, GitHub object ids/URLs, and a coarse result. The provider delivery GUID is deliberately never logged:
    it is an operator capability, not an observability token. No paths, source bodies, diff contents, or secrets."""
    check_meta = check_meta or {}
    result = _surface_result(check_meta, comment_needed=comment_needed, comment_ok=comment_ok)
    check_run_id = check_meta.get("check_run_id")
    check_run_url = check_meta.get("check_run_url") or ""
    details_url = check_meta.get("details_url") or ""
    print(
        f"{trace_prefix}github surface={surface} event={event} action={action or 'unknown'} "
        f"delivery={'present' if delivery else 'missing'} repo={repo} "
        f"pr={pr_number if pr_number is not None else 'missing'} "
        f"head={str(head_sha)[:12] or 'missing'} comment_id={comment_id if comment_id not in (None, '') else 'missing'} "
        f"comment_url={_comment_url(repo, pr_number, comment_id) or 'missing'} "
        f"check_run_id={check_run_id if check_run_id not in (None, '') else 'missing'} "
        f"check_run_url={check_run_url or 'missing'} details_url={details_url or 'missing'} result={result}",
        flush=True,
    )


def _post_refreshes(gh, repo: str, refreshes: list, db=None, branch: str = "main", trace_id: str = "",
                    delivery: str = "", impact_override=None,
                    branch_inventory_unknown_changes=None, graph_degraded: bool = False,
                    return_progress: bool = False, after_change: str = ""):
    """Post the neighborhood-refresh payload [{change,conclusion,summary,comment,clear_reset?}] to each PR — used
    by the merge/withdraw refresh, the push-to-main stale-verdict refresh, AND the open/sync neighbor refresh.
    upsert_check/upsert_comment are idempotent (they PATCH the PR's existing check + marker comment), so
    re-rendering an UNCHANGED verdict rewrites the same body — never a new comment (no spam under churn).

    PAUSE-ACK (一時停止): a refreshed NEIGHBOR that is itself a MATERIAL coupling must ALSO be paused
    (action_required) until acknowledged — otherwise opening PR-B would CLOBBER PR-A's paused check back to a
    plain advisory `neutral`. So when `db` is supplied, each material neighbor's pre-rendered check+comment is run
    through apply_pause_ack here too (same content-free snapshot + label readback as the acting-PR path). FORK
    neighbors are NEVER overlaid (their redacted body must not gain partner specifics, and the pause copy names
    no partner — but we skip them to keep the fork redaction strictly as rendered). main_impact_surface is read
    ONCE, lazily, only if a material neighbor actually appears (a refresh of all-clear neighbors pays nothing).

    Two kinds of entry:
      - a NON-clear entry (warn/serialize/unknown, comment set): upsert the check AND the verdict comment.
      - a `clear_reset` entry (a PR that DROPPED back to clear: its blocker withdrew/landed, the coupling
        cleared): its stale neutral check + verdict comment must be corrected — BUT only if it had one. We PATCH
        the comment ONLY IF a marker comment already exists (proof it was previously non-clear); a PR that was
        ALWAYS clear has no marker comment, so we touch NOTHING (no check post, no comment) and the less-noise
        rule holds. The check is reset to green only when we found+patched that comment, so an always-clear PR
        never gets a redundant check write either.

    Resolving a PR's head sha or a single post failing must not abort the rest — each is best-effort. Returns the
    count of PRs actually touched (a no-op clear_reset on an always-clear PR is NOT counted).

    Background convergence opts into ``return_progress``. Only in that mode, entries are ordered by the stable
    ``change`` label, entries at/before ``after_change`` are skipped, and the first incomplete surface stops the
    page. The result is ``{posted,processed,cursor,has_more,errors}``; cursor advances only across a contiguous
    successful prefix, so a transient GitHub failure can never be skipped by a durable outbox cursor. Live webhook
    callers retain the historical integer return and best-effort continue behavior.

    BOUNDED by _NEIGHBOR_REFRESH_CAP (read off `server` at call time so a test patch is seen, exactly like
    _RERUN_PR_CAP). EVERY push-to-main and every PR open/sync funnels its WHOLE in-flight neighbor set through here,
    and each non-clear neighbor costs a baseline of up to 5 GitHub API calls (two coherent PR/head/identity
    fetches around one file-metadata traversal + check upsert + comment upsert; pause-ACK readback can add calls) — ALL
    while the per-(account,repo) advisory lock is HELD — so on a busy monorepo one push could fan out to HUNDREDS of
    posts under one held lock. We process AT MOST the cap many
    refresh ENTRIES per event (count every entry the loop starts API work on — incl. a clear_reset, which still
    issues a comment-list probe — so the bound covers the real API storm, not just the 'touched' count). The rest
    are NOT lost: each in-flight PR re-renders on its OWN next webhook (push/sync) or a `backfill`. main_impact_surface
    returns `changes` in label order (NOT materiality), so this is a DETERMINISTIC bound + a content-free truncation
    log when it trips; the common small-N case (N <= cap) never trips it and is byte-for-byte unchanged."""
    n = 0
    processed = 0                                    # refresh ENTRIES we started API work on (the storm-cost bound)
    _refresh_cap = _server()._NEIGHBOR_REFRESH_CAP   # read off server at call time (honors the test patch)
    # FILES-API PAGE BUDGET (shared by this WHOLE fan-out): the Files endpoint is paginated at 100 entries/page.
    # An entry cap alone is insufficient: 30 neighbors × GitHub's 3,000-file PR ceiling = 900 HTTP requests while
    # the per-repo lock is held. Give the event at most one Files page per configured refresh slot (default 30).
    # Before any traversal we reserve ceil(changed_files/100) from this shared pool; a PR that cannot fit is safely
    # skipped (last authoritative surface preserved), while a later small PR may still use the remainder.
    _files_page_budget = _refresh_cap
    _files_pages_reserved = 0
    _compare_budget = _refresh_cap                    # at most one strict compare read per bounded neighbor
    _AUX_MISS = object()                              # distinguish read failure from a legitimate empty advisory
    _impact_cache = ({"impact": impact_override} if isinstance(impact_override, dict) else {})
    # Only members of the acting PR's original BR-containing cluster need the pending-branch note. Other clusters
    # keep their ordinary refresh, while every pause decision below uses the exact PR-only speculative impact.
    _raw_branch_unknown = (branch_inventory_unknown_changes
                           if isinstance(branch_inventory_unknown_changes, (list, tuple, set)) else [])
    _branch_unknown_changes = {change for change in _raw_branch_unknown if isinstance(change, str)}
    _coverage_cache = {} # lazy, fetched once: keep acting/neighbor check summaries byte-identical without N reads
    # Trace-id prefix for every log line below — the dispatcher minted one and the caller forwarded it.
    _tp = f"trace_id={trace_id[:_TRACE_TAG_WIDTH]} " if trace_id else ""
    _progress_mode = bool(return_progress)
    _progress_cursor = str(after_change or "")
    _progress_errors = 0
    _progress_has_more = False
    if _progress_mode:
        refreshes = sorted(
            (
                refresh for refresh in (refreshes if isinstance(refreshes, list) else [])
                if isinstance(refresh, dict)
                and isinstance(refresh.get("change"), str)
                and _pr_number_from_change(refresh.get("change"))
                and refresh["change"] > _progress_cursor
            ),
            key=lambda refresh: refresh["change"],
        )

    def _progress_complete(refresh, ok: bool) -> bool:
        """Record one contiguous page result. Return True when background processing must stop."""
        nonlocal _progress_cursor, _progress_errors, _progress_has_more
        if not _progress_mode:
            return False
        if ok:
            _progress_cursor = str(refresh.get("change") or _progress_cursor)
            return False
        _progress_errors += 1
        _progress_has_more = True
        return True

    def _impact():
        if "impact" not in _impact_cache:
            # SAVEPOINT-ISOLATE the neighbor-overlay brain read so it can never leave the shared per-event txn
            # aborted for the next neighbor's check/comment post (the same isolation the acting-PR overlay uses).
            imp = _optional(
                db, "neighbor pause-ack impact read",
                lambda: db("SELECT core.main_impact_surface(%s,%s)", (repo, branch)),
                default={}, repo=repo, trace_id=trace_id)
            if isinstance(imp, str):
                try:
                    imp = json.loads(imp)
                except Exception as e:
                    print(f"{_tp}refresh impact decode skipped repo={repo}: {str(e)[:120]}", flush=True)
                    imp = {}
            _impact_cache["impact"] = imp if isinstance(imp, dict) else {}
        return _impact_cache["impact"]

    def _coverage_nudge():
        """Return the same account-level summary suffix the acting path uses, once per refresh event."""
        if db is None:
            return None
        if "line" not in _coverage_cache:
            def _read_line():
                try:
                    from render import coverage_nudge_line as _coverage_nudge_line
                except ImportError:
                    from .render import coverage_nudge_line as _coverage_nudge_line
                surface = db("SELECT core.account_coverage_surface()")
                if isinstance(surface, str):
                    surface = json.loads(surface)
                return _coverage_nudge_line(surface if isinstance(surface, dict) else {})
            _coverage_cache["line"] = _optional(
                db, "neighbor coverage nudge", _read_line, default=_AUX_MISS,
                repo=repo, trace_id=trace_id)
        return _coverage_cache["line"]

    def _mark_branch_inventory_unknown(refresh: dict) -> dict:
        """Keep independent neighbor evidence, but never green/ACK-pause it because of an unverified BR row."""
        if not isinstance(refresh, dict) or refresh.get("change") not in _branch_unknown_changes:
            return refresh
        try:
            from render import branch_inventory_unknown_check as _pending_check
            from render import branch_inventory_unknown_note as _pending_note
        except ImportError:
            from .render import branch_inventory_unknown_check as _pending_check
            from .render import branch_inventory_unknown_note as _pending_note
        row = dict(refresh)
        if row.get("clear_reset") or row.get("conclusion") == "success":
            prior_comment = row.get("comment")
            prior_fork_comment = row.get("fork_comment")
            pending = _pending_check(branch)
            row.update({"conclusion": pending["conclusion"], "title": pending["title"],
                        "summary": pending["summary"], "comment": pending["summary"],
                        "fork_title": pending["title"], "fork_summary": pending["summary"],
                        "fork_comment": pending["summary"], "branch_inventory_unknown": True})
            if prior_comment:
                row["comment"] += "\n\n---\n\n" + prior_comment
            if prior_fork_comment:
                row["fork_comment"] += "\n\n---\n\n" + prior_fork_comment
            row.pop("clear_reset", None)
            return row
        note = _pending_note()
        for key in ("summary", "comment", "fork_summary", "fork_comment"):
            if row.get(key):
                row[key] += "\n\n---\n\n" + note if "comment" in key else "\n\n" + note
        row["branch_inventory_unknown"] = True
        return row

    def _evidence_complete_refresh(refresh: dict, refresh_pr: int) -> tuple[dict | None, tuple | None]:
        """Re-render one neighbor from its CURRENT, complete PR-file evidence before posting.

        `_refresh_changes` is intentionally pure over `main_impact_surface`, which does not carry Files-API
        evidence.  Rendering its payload directly therefore loses the three inputs that can change the visible
        result: `added_paths` (Unknown -> Clear), the code-file cap (`truncated`, Clear -> Unknown), and unresolved
        conflict markers (anything -> action_required).  A sibling event could consequently overwrite the acting
        PR's evidence-complete check with a contradictory result.

        Production callers always supply `db`; keep the db-less pure/offline seam backward-compatible.  On the
        live path, read the existing ONE-PASS metadata endpoint exactly once, derive the same post-code-filter cap
        inputs as `_pr_pre_brain`, then render full + fork-redacted variants against one current impact snapshot.
        The PR object is re-read after Files and must still be the same open head/count/base: this closes the
        GET(A) -> Files(B) race before any write, while retaining the acting path's fail-safe "next webhook wins".
        Missing/malformed/partial metadata or a missing current change is NOT evidence for empty lists: return
        None so the caller skips every write for this PR.  That preserves the last authoritative check instead of
        replacing it with an evidence-free Clear/Unknown.  The outer refresh-entry cap still bounds this read.
        """
        if db is None:
            return refresh, None
        nonlocal _files_pages_reserved, _compare_budget
        pr_read = getattr(gh, "get_pull_request", None)
        metadata_read = getattr(gh, "list_pr_file_metadata", None)
        if not callable(pr_read) or not callable(metadata_read):
            print(f"{_tp}refresh evidence unavailable repo={repo} pr={refresh_pr}: authoritative PR/metadata read unsupported — skipped", flush=True)
            return None, None
        try:
            # The PR object's declared `changed_files` count is the authoritative short-page backstop. Passing 0
            # would silently DISABLE PRFilesShortfall in list_pr_file_metadata, so a missing/malformed/non-positive
            # declaration is a hard evidence gap: preserve the existing GitHub surface and skip every write.
            prj = pr_read(repo, refresh_pr)
            declared_files = prj.get("changed_files") if isinstance(prj, dict) else None
            if isinstance(declared_files, bool) or not isinstance(declared_files, int) or declared_files <= 0:
                print(f"{_tp}refresh evidence malformed repo={repo} pr={refresh_pr}: changed_files unavailable — skipped", flush=True)
                return None, None
            head = prj.get("head") if isinstance(prj.get("head"), dict) else {}
            base = prj.get("base") if isinstance(prj.get("base"), dict) else {}
            head_sha = head.get("sha")
            base_ref = base.get("ref")
            base_sha = base.get("sha")
            if (not isinstance(head_sha, str) or not head_sha or prj.get("state") != "open"
                    or not isinstance(base_ref, str) or base_ref != branch
                    or not isinstance(base_sha, str) or not base_sha):
                print(f"{_tp}refresh evidence malformed repo={repo} pr={refresh_pr}: head/state/base unavailable — skipped", flush=True)
                return None, None

            declared_pages = (declared_files + 99) // 100  # Files API contract: per_page=100
            if _files_pages_reserved + declared_pages > _files_page_budget:
                print(f"{_tp}refresh Files-page budget skipped repo={repo} pr={refresh_pr}: "
                      f"need={declared_pages} remaining={_files_page_budget - _files_pages_reserved}", flush=True)
                return None, None
            # Reserve BEFORE I/O and never refund on failure/shortfall: the real number of requests may already
            # have been spent, so refunding would let later neighbors exceed the hard per-event HTTP ceiling.
            _files_pages_reserved += declared_pages

            metadata = metadata_read(repo, refresh_pr, declared_files, declared_pages)

            # COHERENT PR SNAPSHOT: Files has no head parameter and always reads GitHub's current PR. Re-read the
            # PR after it and require the exact same head/count/open/base facts as the first read. If A changed to
            # B between calls, skip all writes; B's synchronize webhook/backfill will reconcile claims and render.
            current_prj = pr_read(repo, refresh_pr)
            current_head = current_prj.get("head") if isinstance(current_prj, dict) and isinstance(current_prj.get("head"), dict) else {}
            current_base = current_prj.get("base") if isinstance(current_prj, dict) and isinstance(current_prj.get("base"), dict) else {}
            current_declared = current_prj.get("changed_files") if isinstance(current_prj, dict) else None
            if (isinstance(current_declared, bool) or not isinstance(current_declared, int)
                    or current_head.get("sha") != head_sha or current_declared != declared_files
                    or current_prj.get("state") != "open" or current_base.get("ref") != base_ref
                    or current_base.get("ref") != branch or current_base.get("sha") != base_sha):
                print(f"{_tp}refresh evidence raced repo={repo} pr={refresh_pr}: PR changed during Files read — skipped", flush=True)
                return None, None
            head_repo = current_head.get("repo") if isinstance(current_head.get("repo"), dict) else {}
            base_repo = current_base.get("repo") if isinstance(current_base.get("repo"), dict) else {}
            head_id, base_id = head_repo.get("id"), base_repo.get("id")
            is_fork = head_id is not None and base_id is not None and head_id != base_id
            redact_external = is_fork or head_id is None or base_id is None
            pr_identity = (head_sha, is_fork, redact_external)
        except PRFilesShortfall as e:
            print(f"{_tp}refresh evidence shortfall repo={repo} pr={refresh_pr}: "
                  f"{e.returned} returned vs {e.declared} declared — skipped", flush=True)
            return None, None
        except PRFilesPageBudgetExceeded as e:
            print(f"{_tp}refresh Files-page traversal capped repo={repo} pr={refresh_pr}: "
                  f"used={e.used} budget={e.budget} — skipped", flush=True)
            return None, None
        except Exception as e:
            print(f"{_tp}refresh evidence read skipped repo={repo} pr={refresh_pr}: {str(e)[:120]}", flush=True)
            return None, None
        if not isinstance(metadata, dict) or any(k not in metadata for k in
                                                ("changed", "added_paths", "conflict_markers", "raw_entry_count")):
            print(f"{_tp}refresh evidence malformed repo={repo} pr={refresh_pr} — skipped", flush=True)
            return None, None
        raw_changed = metadata.get("changed")
        raw_added = metadata.get("added_paths")
        conflict_markers = metadata.get("conflict_markers")
        raw_entry_count = metadata.get("raw_entry_count")
        if (not isinstance(raw_changed, list) or not isinstance(raw_added, list)
                or not isinstance(conflict_markers, list) or isinstance(raw_entry_count, bool)
                or not isinstance(raw_entry_count, int) or raw_entry_count != declared_files):
            print(f"{_tp}refresh evidence malformed repo={repo} pr={refresh_pr} — skipped", flush=True)
            return None, None

        code_paths = _code_paths(raw_changed)
        max_pr_files = _server()._MAX_PR_FILES
        truncated_files = len(code_paths) > max_pr_files
        analyzed_paths = code_paths[:max_pr_files]
        analyzed_set = set(analyzed_paths)
        added_paths = [p for p in _code_paths(raw_added) if p in analyzed_set]

        impact = _impact()
        change_id = _change_id(refresh_pr)
        changes = impact.get("changes") if isinstance(impact, dict) else None
        current_change = next((c for c in changes
                               if isinstance(c, dict) and c.get("change_id") == change_id), None) \
            if isinstance(changes, list) else None
        if current_change is None:
            print(f"{_tp}refresh evidence re-render skipped repo={repo} pr={refresh_pr}: current impact unavailable", flush=True)
            return None, None
        if current_change.get("head_sha") != head_sha:
            print(f"{_tp}refresh evidence re-render skipped repo={repo} pr={refresh_pr}: claim/head mismatch", flush=True)
            return None, None
        # COHERENCE BOUNDARY: GET(PR) -> Files is the same unavoidable GitHub race window as the acting path, but
        # a neighbor additionally has an existing DB claim surface. Never combine CURRENT added/conflict/cap
        # evidence with a STALE claim set: the acting path reconciles claims to exactly its capped `_code_paths`
        # list before rendering, so require the current impact row to contain the same normalized paths (order is
        # not semantically meaningful in main_impact_surface). A synchronize that lands between reads can only
        # make us SKIP this refresh; that PR's own webhook/backfill will reconcile and render authoritatively.
        impact_paths = _code_paths(current_change.get("paths"))
        if len(impact_paths) != len(analyzed_paths) or set(impact_paths) != analyzed_set:
            print(f"{_tp}refresh evidence re-render skipped repo={repo} pr={refresh_pr}: claim/files mismatch", flush=True)
            return None, None

        # Match every non-surface input the acting path gives the renderer. Otherwise a sibling event strips the
        # co-change advisory / stale-base nudge, and the PR's own next event adds it back (permanent comment/check
        # churn and, for a cochange-only Clear, a false downgrade-to-Cleared). Both reads are content-free,
        # fail-open like the actor, and bounded by the outer neighbor cap.
        cochange = None
        branch_changed_paths = []
        if not redact_external:
            def _read_cochange():
                value = db("SELECT core.co_change_partners_with_authority(%s,%s,%s,%s)",
                           (repo, analyzed_paths, 3, 0.4))
                if isinstance(value, str):
                    value = json.loads(value)
                return value if isinstance(value, (dict, list)) else None
            cochange = _optional(db, "neighbor co-change read", _read_cochange,
                                 default=_AUX_MISS, repo=repo, pr=refresh_pr, trace_id=trace_id)
            if cochange is _AUX_MISS or not isinstance(cochange, list):
                print(f"{_tp}refresh evidence co-change unavailable repo={repo} pr={refresh_pr} — skipped", flush=True)
                return None, None
            strict_compare = getattr(gh, "compare_changed_paths_strict", None)
            soft_compare = getattr(gh, "compare_changed_paths", None)
            if callable(strict_compare):
                if _compare_budget <= 0:
                    print(f"{_tp}refresh compare budget exhausted repo={repo} pr={refresh_pr} — skipped", flush=True)
                    return None, None
                _compare_budget -= 1
                try:
                    value = strict_compare(repo, base_sha, branch)
                    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
                        raise TypeError("malformed strict compare result")
                    branch_changed_paths = value
                except Exception as e:
                    print(f"{_tp}refresh evidence stale-base unavailable repo={repo} pr={refresh_pr}: {str(e)[:120]} — skipped",
                          flush=True)
                    return None, None
            elif callable(soft_compare):
                # A fail-soft compare cannot distinguish a real empty diff from a swallowed transport failure;
                # never let that ambiguity erase an existing nudge.
                print(f"{_tp}refresh evidence strict stale-base read unsupported repo={repo} pr={refresh_pr} — skipped",
                      flush=True)
                return None, None
            else:
                print(f"{_tp}refresh evidence stale-base read unsupported repo={repo} pr={refresh_pr} — skipped",
                      flush=True)
                return None, None

        try:
            # LAZY import: release/landing gates intentionally stub a partial `render` module. The evidence helper
            # runs only on a live db-backed refresh; resolving the full renderer here preserves those module-load
            # seams while production reaches the real renderer.
            try:
                from render import render_pr_check as _render_pr_check
            except ImportError:
                from .render import render_pr_check as _render_pr_check
            rendered = _render_pr_check(
                impact, change_id, truncated=truncated_files, added_paths=added_paths,
                conflict_markers=conflict_markers, cochange=cochange)
            if (not isinstance(rendered, dict)
                    or not all(isinstance(rendered.get(k), str) and rendered.get(k)
                               for k in ("conclusion", "title", "summary"))
                    or not (rendered.get("comment") is None
                            or isinstance(rendered.get("comment"), str) and rendered.get("comment"))):
                raise TypeError("renderer returned an incomplete refresh surface")
            # The acting path appends this non-fork check-summary suffix.  Omitting it here makes every sibling
            # refresh strip the suffix and the PR's next own delivery restore it, causing endless no-op PATCH
            # churn.  Reuse one content-free coverage read across the fan-out; this changes no comment surface.
            if not redact_external:
                nudge = _coverage_nudge()
                if nudge is _AUX_MISS:
                    raise RuntimeError("coverage evidence unavailable")
                if nudge:
                    rendered["summary"] += "\n\n" + nudge
                # Match acting order exactly: base render → coverage → stale-base. Reversing these stable suffixes
                # produces byte-different summaries and permanent actor/neighbor PATCH churn.
                if branch_changed_paths:
                    try:
                        from render import stale_base_nudge_line as _stale_base_nudge_line
                    except ImportError:
                        from .render import stale_base_nudge_line as _stale_base_nudge_line
                    stale_line = _stale_base_nudge_line(branch_changed_paths, analyzed_paths)
                    if stale_line and stale_line not in (rendered.get("summary") or ""):
                        rendered["summary"] += "\n\n" + stale_line
        except Exception as e:
            print(f"{_tp}refresh evidence render skipped repo={repo} pr={refresh_pr}: {str(e)[:120]}", flush=True)
            return None, None
        if rendered.get("comment") is None:
            return ({"agent": refresh.get("agent"), "change": change_id,
                     "conclusion": rendered["conclusion"], "title": rendered["title"],
                     "summary": rendered["summary"], "comment": None, "clear_reset": True}, pr_identity)
        try:
            fork_rendered = _render_pr_check(
                impact, change_id, truncated=truncated_files, is_fork=True, added_paths=added_paths,
                conflict_markers=conflict_markers)
            if (not isinstance(fork_rendered, dict)
                    or not all(isinstance(fork_rendered.get(k), str) and fork_rendered.get(k)
                               for k in ("conclusion", "title", "summary"))
                    or not (fork_rendered.get("comment") is None
                            or isinstance(fork_rendered.get("comment"), str) and fork_rendered.get("comment"))):
                raise TypeError("fork renderer returned an incomplete refresh surface")
        except Exception as e:
            print(f"{_tp}refresh evidence fork-render skipped repo={repo} pr={refresh_pr}: {str(e)[:120]}", flush=True)
            return None, None
        return ({"agent": refresh.get("agent"), "change": change_id,
                 "conclusion": rendered["conclusion"], "title": rendered["title"],
                 "summary": rendered["summary"], "comment": rendered["comment"],
                 "fork_title": fork_rendered["title"], "fork_summary": fork_rendered["summary"],
                 "fork_comment": fork_rendered["comment"]}, pr_identity)

    for refresh in refreshes or []:
        refresh_pr = _pr_number_from_change(refresh.get("change"))
        if not refresh_pr:
            _progress_complete(refresh, True)  # malformed/non-PR labels have no GitHub surface to converge
            continue
        if processed >= _refresh_cap:                # BOUNDED: stop the fan-out; the rest refresh on their own next
            # webhook (push/sync) or a backfill. Content-free (counts only — no path/identity), never crashes. The
            # post storm under the held per-repo lock is now capped at _refresh_cap GitHub round-trips, not O(N).
            print(f"{_tp}neighbor refresh capped repo={repo}: posted up to {_refresh_cap} of {len(refreshes)} in-flight "
                  f"(VERIPSA_NEIGHBOR_REFRESH_CAP); the rest refresh on their own next event", flush=True)
            _progress_has_more = True
            break
        processed += 1                               # this entry will start API work below → it counts against the cap
        refresh, refresh_identity = _evidence_complete_refresh(refresh, refresh_pr)
        if refresh is None:
            if _progress_complete(
                    {"change": _change_id(refresh_pr)}, False):
                break
            continue
        refresh = _mark_branch_inventory_unknown(refresh)
        if refresh.get("clear_reset"):
            if graph_degraded:
                # G1 PARITY FOR THE DOWNGRADE-TO-CLEAR REPOST: the acting PR's own would-be clear is already
                # withheld under a degraded graph (stale-behind-HEAD / HEAD-unresolvable / version-mismatch); the
                # SAME must hold for a neighbor/push/policy refresh that would reset THIS PR to green. Resetting a
                # check to `success` off a graph we cannot confirm is current is a false clear — so withhold it and
                # touch nothing (no cleared-comment patch, no check reset). The next fresh event re-confirms and,
                # when the overlap has genuinely resolved on a current graph, resets it then. Fail-closed + advisory.
                print(f"{_tp}clear-reset withheld (graph degraded) repo={repo} pr={refresh_pr}", flush=True)
                _progress_complete(refresh, True)
                continue
            # DOWNGRADE-TO-CLEAR: correct a stale warn/serialize ONLY if this PR actually has a Veripsa comment
            # (proof it was previously non-clear). Do the CHEAP comment-list+patch-if-exists FIRST; an always-clear
            # PR has no marker comment → no patch → we touch NOTHING (no head fetch, no check write, no comment) so
            # the no-spam contract AND the API-cost budget both hold (busy repos can have many always-clear PRs).
            # Only when we actually patched the comment do we spend the head fetch + reset the check to green.
            try:
                patched = gh.patch_comment_if_exists(
                    repo, refresh_pr, _comment_marker(refresh_pr),
                    # DEFERRED body: the default-branch fetch + render only run if a marker comment actually exists
                    lambda existing="": _marked_comment(
                        refresh_pr,
                        _cleared_comment_preserving_ack(existing, _default_branch_for(gh, repo))))
            except Exception as e:
                print(f"{_tp}clear-reset comment skipped repo={repo} pr={refresh_pr}: {str(e)[:120]}", flush=True)
                if _progress_complete(refresh, False):
                    break
                continue
            if patched:
                if refresh_identity is not None:
                    head_ref = refresh_identity[0]          # reuse the authoritative PR read; never duplicate GET
                else:
                    try:
                        head_ref = gh.pull_request_head(repo, refresh_pr)
                    except Exception as e:                  # comment fixed; check reset is best-effort
                        print(f"{_tp}clear-reset check skipped repo={repo} pr={refresh_pr}: {str(e)[:120]}", flush=True)
                        n += 1
                        if _progress_complete(refresh, False):
                            break
                        continue
                check_meta = _upsert_check_result(
                    gh, repo, head_ref, "success", refresh.get("title") or "Veripsa",
                    refresh.get("summary") or "Clear — the earlier overlap has resolved.",
                    pr_number=refresh_pr)
                _log_pr_surface(
                    _tp, surface="neighbor-clear-reset", delivery=delivery, action="neighbor-refresh",
                    repo=repo, pr_number=refresh_pr, head_sha=head_ref, comment_needed=True, comment_ok=True,
                    check_meta=check_meta)
                n += 1
                if _progress_complete(
                        refresh,
                        isinstance(check_meta, dict) and check_meta.get("posted") is True):
                    break
            else:
                _progress_complete(refresh, True)  # always-clear PR: deliberate no-op, fully converged
            continue
        if refresh_identity is not None:
            head_ref, is_fork, redact_external = refresh_identity  # reuse PR GET made for declared file count
        else:
            try:
                head_ref, is_fork, redact_external = _head_and_fork(gh, repo, refresh_pr)
            except Exception as e:                          # PR head unresolvable (closed mid-flight / API error) → skip it
                print(f"{_tp}refresh skipped repo={repo} pr={refresh_pr}: {str(e)[:120]}", flush=True)
                # A positively-gone PR has no remaining surface. Other failures retain the cursor for retry.
                gone = getattr(e, "code", None) in (404, 410)
                if _progress_complete(refresh, gone):
                    break
                continue
        # FORK INFO-LEAK GUARD (the NEIGHBOR path — audit r3): a fork neighbor's refresh comment posts on the
        # EXTERNAL contributor's conversation, so post the REDACTED variant (_refresh_changes pre-rendered it) that
        # drops the base repo's other in-flight PR identities / paths. (is_fork also degrades the head-sha check to
        # comment-only — a fork head sha isn't in the base repo.) The engine has no fork concept; fork status is
        # resolved from GitHub here, in the ONE head fetch _post_refreshes already makes.
        summary = (refresh.get("fork_summary") if redact_external else refresh.get("summary")) or refresh["summary"]
        conclusion = refresh["conclusion"]
        # CARRY THE RENDERED TITLE (title non-determinism fix, 2026-06-23): use render_pr_check's rich per-verdict
        # title threaded through the refresh payload (fork-aware, exactly like summary/body), NOT a hard-coded bare
        # "Veripsa". Falling back to "Veripsa" only for an old payload that predates the title key. This is what
        # stops a neighbor's title silently degrading from e.g. "Veripsa — Unknown" to bare "Veripsa" when a
        # sibling PR's event re-renders it.
        title = ((refresh.get("fork_title") if redact_external else refresh.get("title"))
                 or refresh.get("title") or "Veripsa")
        body = (refresh.get("fork_comment") if redact_external else
                (refresh.get("comment") or f"### Veripsa\n\n{summary}"))
        # PAUSE-ACK overlay for a MATERIAL neighbor (NON-fork only): keep its paused (action_required) check +
        # ack-bound comment so opening/syncing another PR does not clobber its pause back to a plain neutral. Read
        # its CURRENT label set + the snapshot embedded in its prior comment, then overlay the rendered check+body.
        if db is not None and not is_fork and conclusion == "neutral":
            try:
                # UNIFIED, FAIL-SAFE label read (the SAME ack-stickiness contract as the acting path, which reads
                # the payload directly): read strictly so an UNREADABLE label set RAISES → the except below FAIL-
                # OPENs (the neighbor keeps its plain advisory neutral) rather than masking the error as "no label"
                # and re-raising a paused check on an already-acked neighbor. A possibly-stale API read must never
                # strip or over-pause a valid ack — only a positively-proven coupling change (in apply_pause_ack) does.
                label_present = ACK_LABEL in (gh.pr_labels(repo, refresh_pr, strict=True) if hasattr(gh, "pr_labels") else [])
                prior_hash, prior_confirmed = _prior_ack_snapshot(gh, repo, refresh_pr) if label_present else (None, True)
                uncertainty_kwargs = ({"preserve_ack_on_uncertainty": True}
                                      if refresh.get("branch_inventory_unknown")
                                      and _accepts_keyword(apply_pause_ack, "preserve_ack_on_uncertainty") else {})
                # G5: thread the SAME degraded-graph signal the acting overlay uses, so a sibling PR's event that
                # re-renders THIS acked neighbor while main's graph is still degraded does NOT re-clear its honest
                # `ack_unconfirmable_degraded` back to "Acknowledged" (the false-clear flap G5 removes on the
                # acting path). Neighbor-safe: the degraded state stays neutral AND keeps the
                # label (label_action None), so this never re-pauses and never strips. Signature-stable seam (like
                # the uncertainty kwarg): passed only when degraded AND the bound apply_pause_ack accepts it, so a
                # monkeypatched/legacy fake without the keyword is never handed it. Default False = today's exact
                # behavior (the caller supplies the shared _graph_degraded(graph_heal) / freshness signal).
                degraded_kwargs = ({"graph_degraded": True}
                                   if graph_degraded and _accepts_keyword(apply_pause_ack, "graph_degraded") else {})
                overlay = apply_pause_ack(
                    {"conclusion": conclusion, "title": title, "summary": summary, "comment": body},
                    _impact(), _change_id(refresh_pr), label_present=label_present, prior_hash=prior_hash,
                    branch=branch, is_fork=is_fork, prior_confirmed=prior_confirmed,
                    **uncertainty_kwargs, **degraded_kwargs)
                # PAUSE-ACK DECISION LOG — content-free, always-on (same as the acting path). The NEIGHBOR path
                # re-renders an in-flight PR because ANOTHER PR moved; this line is how we SEE whether a neighbor
                # refresh wrongly re-paused/stripped an already-acked PR (the cross-path ack-stickiness gap). The
                # 'action' here is the neighbor refresh itself (no per-PR webhook action), hence 'neighbor-refresh'.
                print(f"{_tp}pause-ack decide repo={repo} pr={refresh_pr} path=neighbor action=neighbor-refresh "
                      f"label_present={label_present} prior_hash={prior_hash} prior_confirmed={prior_confirmed} "
                      f"current_snap={overlay.get('snapshot')} ack_state={overlay.get('ack_state')} "
                      f"label_action={overlay.get('label_action')}", flush=True)
                if overlay.get("ack_state") != "not_material":
                    conclusion = overlay["conclusion"]
                    title = overlay["title"]                 # ack overlay can swap the title (e.g. "Acknowledged — proceeding…"); take it, exactly like the acting path
                    summary = overlay["summary"]             # keep acting/neighbor paused checks byte-identical; otherwise each path churn-patches the other's summary
                    body = overlay["comment"]
                    if overlay.get("label_action") == "remove" and hasattr(gh, "remove_label"):
                        try:
                            gh.remove_label(repo, refresh_pr, ACK_LABEL)
                        except Exception as e:
                            print(f"{_tp}neighbor stale-ack label removal skipped repo={repo} pr={refresh_pr}: {str(e)[:120]}", flush=True)
            except Exception as e:                          # FAIL-OPEN: the neighbor's advisory verdict stands
                print(f"{_tp}neighbor pause-ack overlay skipped repo={repo} pr={refresh_pr}: {str(e)[:120]}", flush=True)
        # PRE-POST THE COMMENT (PO #3 details_url) so the check's "Details" link anchors at the verdict
        # comment. A comment failure must not drop the check; degrades to PR-URL / commit fallback.
        comment_id = None
        comment_ok = False
        if body:
            try:
                resp = gh.upsert_comment(repo, refresh_pr, _comment_marker(refresh_pr),
                                         _marked_comment(refresh_pr, body))
                comment_ok = True
                if isinstance(resp, dict):
                    cid = resp.get("id")
                    if isinstance(cid, int):
                        comment_id = cid
            except Exception as e:                          # one comment post failing must not drop the others
                print(f"{_tp}refresh comment skipped repo={repo} pr={refresh_pr}: {str(e)[:120]}", flush=True)
                _log_pr_surface(
                    _tp, surface="neighbor", delivery=delivery, action="neighbor-refresh",
                    repo=repo, pr_number=refresh_pr, head_sha=head_ref, comment_needed=True, comment_ok=False,
                    check_meta={"posted": False})
                if _progress_complete(refresh, False):
                    break
                continue
        check_meta = _upsert_check_result(gh, repo, head_ref, conclusion, title, summary, is_fork,
                                          pr_number=refresh_pr, comment_id=comment_id)
        _log_pr_surface(
            _tp, surface="neighbor", delivery=delivery, action="neighbor-refresh", repo=repo,
            pr_number=refresh_pr, head_sha=head_ref, comment_needed=bool(body), comment_ok=comment_ok,
            comment_id=comment_id, check_meta=check_meta)
        # A4 ACTIVATION (Issue #648): a Check was PUBLISHED on this neighbor's PR head — record the first-check
        # fact (idempotent on head; the surface takes the earliest). ONLY after a confirmed post (posted=True),
        # append-only + ledger-wall guarded + ON CONFLICT DO NOTHING, wrapped fail-soft so a DB error can NEVER
        # affect the refresh, the check, or webhook routing. In the neighbor path action_required arises via the
        # pause-ack overlay, so it maps to 'paused'.
        if db is not None and isinstance(check_meta, dict) and check_meta.get("posted"):
            try:
                db("SELECT core.record_check_published_with_authority(%s,%s,%s,%s,%s)",
                   (_change_id(refresh_pr), repo, branch, head_ref,
                    _signal_token(conclusion, paused=(conclusion == "action_required"))))
            except Exception as e:
                print(f"{_tp}check_published record skipped repo={repo} pr={refresh_pr}: {str(e)[:120]}", flush=True)
        n += 1
        surface_ok = (
            isinstance(check_meta, dict) and check_meta.get("posted") is True
        ) or (bool(is_fork) and comment_ok)
        if _progress_complete(refresh, surface_ok):
            break
    if _progress_mode:
        # An error, a cap break, or any unvisited candidate means the exact cursor must be requeued.
        _progress_has_more = bool(
            _progress_has_more or processed < len(refreshes or []))
        return {
            "posted": n,
            "processed": processed,
            "cursor": _progress_cursor,
            "has_more": _progress_has_more,
            "errors": _progress_errors,
        }
    return n


# THE EVENT-HANDLING CAPS (_MAX_PR_FILES / _FAILING_CONCLUSIONS / _RERUN_PR_CAP) stay DEFINED ON server.py and
# are read here at CALL time via _server(). They are NOT plain module constants on THIS file because the
# codebase's monkeypatch CONTRACT rebinds them on `server` at runtime: the per-PR file-cap + rerun-bound gates
# in tests/test_server.py do `S._MAX_PR_FILES = 3` / `S._RERUN_PR_CAP = 1` and expect handle_event /
# reserve_branch_lanes to read the NEW value. Reading off the live `server` module (server._MAX_PR_FILES, …) at
# call time honors that patch, exactly as before the split. server.py is fully loaded by the time any handler
# runs, so the call-time `import server` is cycle-free (server.py imports THIS at load and re-exports the names).
def _f_trace_prefix(f) -> str:
    """Build the trace-id log prefix off the per-PR context dict `f` (set by _pr_eligibility from the dispatcher-
    stashed payload key). Mirrors webhook._trace_log_prefix's prefix shape so an on-call's grep is uniform across
    the webhook → ingest → render trail. Returns '' when absent (a degraded-but-valid log line — better a missing
    prefix than a crash on a code path that bypassed the dispatcher, e.g. a unit test that builds `f` by hand)."""
    if isinstance(f, dict):
        tid = f.get("trace_id")
        if isinstance(tid, str) and tid:
            return f"trace_id={tid[:_TRACE_TAG_WIDTH]} "
    return ""


def _server():
    """The server module, resolved at CALL time (NOT at load — server.py imports THIS, so a load-time
    `import server` here would be a circular import). server.py is fully loaded by the time any handler runs."""
    try:
        import server as _s  # call-time import → no module-load circularity
    except ImportError:
        from . import server as _s  # type: ignore
    return _s


def _reconcile_live_branch_claims(db, gh, repo: str, default_branch: str) -> dict:
    """Resolve the optional branch-lane self-heal at call time.

    ``webhook_handlers`` is imported by several narrow/offline harnesses that intentionally provide only the
    historical public ``ingest`` seams. Keeping this new helper lazy preserves those harnesses and avoids making
    an optional convergence path a module-load dependency; the live process always has the real module.
    """
    try:
        import ingest as _ingest
    except ImportError:
        from . import ingest as _ingest  # type: ignore
    work = getattr(_ingest, "_reconcile_live_branch_claims", None)
    if not callable(work):
        raise RuntimeError("branch-claim reconciler unavailable")
    return work(db, gh, repo, default_branch)


def _accepts_keyword(work, name: str) -> bool:
    """Whether an injected callable accepts ``name`` (keeps narrow legacy/offline seams compatible)."""
    try:
        params = inspect.signature(work).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def reserve_branch_lanes(db, repo: str, head_branch: str, protected_branch: str, payload: dict | None) -> dict:
    """A push to a NON-main feature branch reserves lanes the moment it is pushed — so collision/serialize can
    fire BEFORE a PR exists (the pre-merge window opens at PUSH time, not only at PR time). NOTIFY-ONLY: this
    only RECORDS reservations (a held collision when the lane is already taken) — it never blocks the push.

    The lane namespace is the PROTECTED branch (main), the SAME coordinate the later PR's claims reserve — so a
    feature branch and an open PR both heading to main contend on a shared path (the contention key is
    (account, repo, main, path), author-agnostic). The work-unit identity is the BRANCH (change_id 'BR-<branch>'),
    never the author: two works on the same path collide regardless of who pushed (confirmed by the gate's
    author-agnostic lane lock). Reservation is attributed to the pusher (display only), exactly as the PR path
    attributes to the PR author — attribution ≠ work-unit. We act_for each changed path (added ∪ modified ∪
    removed: a deletion still touches the lane). Bounded by _MAX_PR_FILES (a mega-branch-push can't open
    thousands of lanes); skipped entirely when nothing changed (per-branch, not per-commit noise)."""
    changed, removed, pusher = _push_changed_sets(payload)
    # Reserve lanes only for paths that can carry code coupling — a docs/asset-only push has nothing to
    # coordinate (and must not reserve a lane that would later force a PR to 'unknown'); see _code_paths.
    paths = _code_paths(sorted(set(changed) | set(removed)))
    if not paths:
        return {"event": "push", "branch": head_branch, "reserved": 0, "skipped": "no code paths changed"}
    truncated = False
    _max_pr_files = _server()._MAX_PR_FILES                      # read off server at call time (honors the test patch)
    if len(paths) > _max_pr_files:                              # COST GUARD: bound lanes for a mega-branch-push
        paths = paths[:_max_pr_files]
        truncated = True
    cid = _branch_change_id(head_branch)
    author = pusher or "unknown"                                # attribution only; the work-unit is the branch
    author_is_bot = _push_author_is_bot(payload)                # SEAT METERING: a bot pusher stays free (sender.type)
    reserved = 0
    for path in paths:
        # A branch push reservation is NEVER a draft (a draft is a PR-state; the branch precedes the PR). Pass
        # NULL for p_is_draft so the gate leaves the column at its default false — the engine's draft-softening
        # never fires on a BR claim, exactly as today.
        db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
           (_branch_claim_id(head_branch, path), path, repo, protected_branch, author, None, None, author_is_bot, None))
        reserved += 1
    # REAL-PUSH SIGNAL (#851): record THIS branch's new head so the stale-reservation decay in main_impact_surface
    # keys off a genuine head change, NOT heartbeat_at. The act_for calls above already re-stamped heartbeat_at via
    # the gate's self-heartbeat even for an idempotent same-head re-push — so heartbeat_at can never prove a branch
    # is idle. note_branch_push_head advances last_real_push_at ONLY when the head actually moved; an identical-head
    # re-push (or a reconcile that never calls this) leaves it untouched, so an idle no-PR reservation still decays.
    after = _as_obj(payload).get("after")
    head_sha = after if (isinstance(after, str) and after and after.strip("0")) else ""
    if head_sha:
        db("SELECT core.note_branch_push_head_with_authority(%s,%s,%s,%s)",
           (cid, repo, protected_branch, head_sha))
    return {"event": "push", "repo": repo, "branch": head_branch, "change_id": cid,
            "reserved_lanes_on": protected_branch, "reserved": reserved, "truncated_files": truncated}


_ACTIVATION_PROOF_MARKER = "_veripsa_activation_installation_proof"
_SUSPEND_PROOF_MARKER = "_veripsa_installation_suspend_proof"
_DELETE_PROOF_MARKER = "_veripsa_installation_delete_proof"
_ALLREPOS_DISCOVERY_MARKER = "_veripsa_allrepos_discovery_state"
_ALLREPOS_DISCOVERY_STATES = frozenset(("found", "empty", "failed"))
# Kept under the old exported symbol because server.py re-exports it for package/backward compatibility.  The value
# is now a structured proof, never the former boolean skip marker.
_STALE_UNINSTALL_MARKER = _DELETE_PROOF_MARKER


def _proof_id(value, label: str) -> str:
    if value in (None, "") or isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        raise RuntimeError(f"{label} omitted or malformed")
    text = str(value).strip()
    if not text or len(text) > 64:
        raise RuntimeError(f"{label} omitted or malformed")
    return text


def _proof_created_at(value, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{label} omitted or malformed")
    text = value.strip()
    if not text or len(text) > 80:
        raise RuntimeError(f"{label} omitted or malformed")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError as exc:
        raise RuntimeError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{label} must include a timezone")
    return text


def _validated_installation_proof(value, label: str) -> dict | None:
    """Validate an injected point-read at the consumer boundary; literal ``None`` alone means HTTP 404."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} returned malformed authority")
    installation_id = _proof_id(value.get("installation_id"), f"{label}.installation_id")
    account_id = _proof_id(value.get("account_id"), f"{label}.account_id")
    created_at = _proof_created_at(value.get("created_at"), f"{label}.created_at")
    suspended = value.get("suspended")
    if not isinstance(suspended, bool):
        raise RuntimeError(f"{label}.suspended omitted or malformed")
    return {
        "installation_id": installation_id,
        "account_id": account_id,
        "created_at": created_at,
        "suspended": suspended,
    }


def _activation_installation_proof(gh, payload: dict) -> dict | None:
    """Return a bounded App-JWT proof for this exact activation installation, or ``None`` if absent/stale.

    The exact installation is point-read with App-JWT.  Matching both installation and owning-account ids prevents
    a signed but cross-account payload from lending another tenant's live generation proof.  API/malformed failures
    raise so the durable delivery retries; an honest 404, suspension, or identity mismatch is a completed stale no-op.
    """
    installation = _as_obj(_as_obj(payload).get("installation"))
    installation_id = installation.get("id") or _as_obj(payload).get("installation_id")
    expected_account_id = _as_obj(installation.get("account")).get("id")
    if installation_id in (None, "") or expected_account_id in (None, ""):
        raise RuntimeError("activation generation proof needs installation.id and installation.account.id")
    point_read = getattr(gh, "app_installation_identity", None)
    if not callable(point_read):
        raise RuntimeError("GitHub client lacks installation generation point-read authority")
    current = _validated_installation_proof(
        point_read(str(installation_id)), "installation generation point read")
    if current is None:
        return None
    if (current["installation_id"] != _proof_id(installation_id, "webhook installation.id")
            or current["account_id"] != _proof_id(expected_account_id, "webhook installation.account.id")):
        # This is an exact-id App endpoint.  A different id/account is malformed authority, not a stale 404.
        raise RuntimeError("installation generation point read mismatched the webhook identity")
    if current["suspended"]:
        return None
    return current


def _current_account_installation_proof(gh, payload: dict) -> dict | None:
    """Return the App's current installation for a lifecycle event's stable account id.

    The login carried by a delayed webhook is mutable and may already have been renamed.  The client therefore
    resolves only by ``installation.account.id`` through a complete App-installations scan; uncertainty raises and
    retries rather than becoming destructive absence.
    """
    installation = _as_obj(_as_obj(payload).get("installation"))
    account = _as_obj(installation.get("account"))
    expected_account_id = _proof_id(account.get("id"), "webhook installation.account.id")
    point_read = getattr(gh, "app_account_installation_identity", None)
    if not callable(point_read):
        raise RuntimeError("GitHub client lacks current-account installation authority")
    current = _validated_installation_proof(
        point_read(expected_account_id), "current-account installation point read")
    if current is None:
        return None
    if current["account_id"] != expected_account_id:
        raise RuntimeError("current-account installation authority mismatched the webhook account")
    return current


def _validated_delete_proof(payload: dict) -> dict:
    """Bind the processor's App-JWT result to the exact durable uninstall payload before SQL consumes it."""
    raw = _as_obj(payload).get(_DELETE_PROOF_MARKER)
    if not isinstance(raw, dict) or raw.get("state") not in ("absent", "replacement"):
        raise RuntimeError("installation delete needs explicit current-generation authority")
    installation = _as_obj(_as_obj(payload).get("installation"))
    deleted_id = _proof_id(installation.get("id"), "installation.delete installation.id")
    account_id = _proof_id(_as_obj(installation.get("account")).get("id"),
                           "installation.delete installation.account.id")
    if (_proof_id(raw.get("deleted_installation_id"), "delete proof deleted_installation_id") != deleted_id
            or _proof_id(raw.get("account_id"), "delete proof account_id") != account_id):
        raise RuntimeError("installation delete proof mismatched the durable payload")
    proof = {
        "state": raw["state"],
        "deleted_installation_id": deleted_id,
        "account_id": account_id,
    }
    if raw["state"] == "replacement":
        current = _validated_installation_proof(raw.get("current"), "replacement installation proof")
        if current is None or current["account_id"] != account_id or current["installation_id"] == deleted_id:
            raise RuntimeError("replacement installation proof did not identify a different current generation")
        proof["current"] = current
    elif "current" in raw:
        raise RuntimeError("authoritative-absence proof cannot carry a current installation")
    return proof


def _validated_suspend_proof(payload: dict) -> dict:
    """Bind the processor's current-account App read to the exact durable suspend target."""
    raw = _as_obj(payload).get(_SUSPEND_PROOF_MARKER)
    if not isinstance(raw, dict) or raw.get("state") not in ("absent", "current"):
        raise RuntimeError("installation suspend needs explicit current-generation authority")
    installation = _as_obj(_as_obj(payload).get("installation"))
    suspended_id = _proof_id(installation.get("id"), "installation.suspend installation.id")
    account_id = _proof_id(_as_obj(installation.get("account")).get("id"),
                           "installation.suspend installation.account.id")
    if (_proof_id(raw.get("suspended_installation_id"),
                  "suspend proof suspended_installation_id") != suspended_id
            or _proof_id(raw.get("account_id"), "suspend proof account_id") != account_id):
        raise RuntimeError("installation suspend proof mismatched the durable payload")
    proof = {
        "state": raw["state"],
        "suspended_installation_id": suspended_id,
        "account_id": account_id,
    }
    if raw["state"] == "current":
        current = _validated_installation_proof(raw.get("current"), "current installation proof")
        if current is None or current["account_id"] != account_id:
            raise RuntimeError("current installation proof mismatched the suspend account")
        proof["current"] = current
    elif "current" in raw:
        raise RuntimeError("authoritative-absence suspend proof cannot carry a current installation")
    return proof


def _purge_account_with_delete_proof(db, payload: dict) -> dict:
    delivery_key = _delivery_of(payload) or None
    if not delivery_key:
        raise RuntimeError("installation delete needs durable delivery authority")
    proof = _validated_delete_proof(payload)
    db("SELECT set_config('core.current_delivery_key',%s,true)", (str(delivery_key)[:200],))
    res = db("SELECT core.purge_account_working_set_with_authority(%s::jsonb)",
             (json.dumps(proof, sort_keys=True, separators=(",", ":")),))
    if isinstance(res, str):
        res = json.loads(res)
    if not isinstance(res, dict) or not res.get("ok"):
        raise RuntimeError("account working-set purge failed")
    return res


def _reactivate_account(db, delivery_key: str | None = None, *, payload: dict | None = None) -> bool:
    """RESURRECTION-TOMBSTONE CLEAR (audit iter-4 P1): a GENUINE re-install/onboard event reactivates the tenant —
    clear any uninstall-purge/erase tombstone (and re-provision the identity rows an erase hard-deleted) so the
    account is live again and its background writers proceed. This is the EXPLICIT legitimate-reinstall edge that
    distinguishes a real reinstall (history preserved, must work) from a background writer racing the uninstall
    (blocked by the tombstone). Runs FIRST in each onboarding branch, on the event's tenant-pinned connection.  The
    durable delivery key is resolved by SQL to its immutable receive order.  False means the caller must skip every
    onboarding write; a stale activation is a completed no-op, while a real DB error leaves the transaction aborted
    so durable processing retries it."""
    try:
        key = str(delivery_key)[:200] if delivery_key else None
        raw_proof = _as_obj(payload).get(_ACTIVATION_PROOF_MARKER) if isinstance(payload, dict) else None
        proof = raw_proof if isinstance(raw_proof, dict) else None
        # The point-read happens at the top of event_processor, before any DB/advisory lock.  Pass its bounded JSON
        # once; SQL binds it to the processing delivery's installation/account fields.
        res = db("SELECT core.reactivate_account_with_authority(%s,%s::jsonb)",
                 (key, json.dumps(proof) if proof is not None else None))
        if isinstance(res, str):
            res = json.loads(res)
        # None is retained for narrow legacy/offline adapters that do not model the additive return shape.  The
        # production function always returns JSON and explicitly marks stale/missing authority as not reactivated.
        if isinstance(res, dict) and (not res.get("ok") or res.get("reactivated") is False):
            print(f"account reactivate refused: {str(res.get('reason') or 'lifecycle order')[:160]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"account reactivate failed (durable retry required): {str(e)[:160]}", flush=True)
        raise


def _repository_identity(repo_value) -> tuple[str | None, str | None, bool]:
    repo = _as_obj(repo_value)
    full = repo.get("full_name")
    repo_id = repo.get("id")
    full_s = str(full).strip()[:512] if full not in (None, "") else None
    id_present = repo_id not in (None, "")
    id_s = str(repo_id).strip() if id_present and not isinstance(repo_id, bool) else None
    # GitHub repository ids are positive JSON integers. Validate before bounding: truncating a long attacker-
    # controlled value could otherwise turn it into a different apparently valid id. `isascii` is required because
    # Python's digit predicates also accept Unicode numerals, while the DB authority boundary uses ASCII decimals.
    invalid_id = id_present and (id_s is None or len(id_s) > 32 or not id_s.isascii()
                                 or not id_s.isdigit() or id_s.startswith("0"))
    if invalid_id:
        id_s = None
    return full_s, id_s, invalid_id


def _reactivate_repository(db, repo_value, delivery_key: str | None = None) -> dict:
    """Clear a repo-scoped revocation only on an explicit GitHub lifecycle reactivation."""
    full, repo_id, invalid_id = _repository_identity(repo_value)
    if invalid_id:
        # Do not collapse a present-but-invalid id into the legacy name-only authority path. Raising keeps the
        # durable lifecycle delivery retryable and leaves every tombstone intact.
        raise RuntimeError("repository reactivation rejected an invalid repository id")
    if full is None and repo_id is None:
        return {"ok": True, "cleared": 0, "skipped": "missing repository identity"}
    if not delivery_key:
        # Live webhook processing always carries the durable delivery key. Never fall back to the unordered
        # compatibility overload: during a rolling deploy an old/replayed add could otherwise clear a newer marker.
        return {"ok": True, "cleared": 0, "not_observed": True,
                "skipped": "missing durable lifecycle authority"}
    res = db("SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
             (full, repo_id, delivery_key))
    # Older offline adapters and pre-lifecycle test doubles have no return shape for this additive call. A real
    # DB failure raises, while the real function always returns JSON. Treat only this unobserved None as a no-op;
    # an explicit non-ok result still fails loudly.
    if res is None:
        return {"ok": True, "cleared": 0, "not_observed": True}
    if isinstance(res, str):
        res = json.loads(res)
    if not isinstance(res, dict) or not res.get("ok"):
        raise RuntimeError("repository reactivation failed")
    return res


def _reactivate_repositories(db, repos: list, delivery_key: str | None = None) -> tuple[list, int]:
    """Return only repositories whose lifecycle activation was observed current and may be onboarded.

    Durable delivery order is authoritative. A stale add/create is a successful DB no-op, but it must also skip
    the durable onboarding enqueue; otherwise a later convergence turn could rebuild behind the tombstone
    it correctly preserved. Missing durable authority and legacy adapters that cannot observe the lifecycle result
    are equally non-current: fail closed before enqueue instead of scheduling a graph behind a live tombstone.
    """
    current = []
    skipped = 0
    for repo in repos:
        result = _reactivate_repository(db, repo, delivery_key)
        if result.get("stale_lifecycle_event") is False:
            current.append(repo)
        else:
            skipped += 1
    return current, skipped


def _account_onboarding_allowed(db, repo_value) -> bool:
    """Gate account-level onboarding enqueue without granting repository-selection authority.

    installation.created/unsuspend/new_permissions can reactivate the account, but an old delivery may still name
    a repository removed later. The DB checks the current repo tombstone/activation boundary; this helper never
    clears either one. A missing result is not permission to queue convergence, so fail loudly and let the durable
    inbox retry.
    """
    full, repo_id, invalid_id = _repository_identity(repo_value)
    if invalid_id:
        raise RuntimeError("repository account-onboarding rejected an invalid repository id")
    if full is None:
        return False
    allowed = db(
        "SELECT core.repository_account_onboarding_allowed_with_authority(%s,%s)",
        (full, repo_id),
    )
    if isinstance(allowed, str):
        return allowed.strip().lower() in ("t", "true", "1")
    if isinstance(allowed, bool):
        return allowed
    raise RuntimeError("repository account-onboarding authority was not observed")


def _repository_event_allowed(payload: dict, db) -> bool:
    """Fail closed for work delivered after a repository was removed or deleted."""
    full, repo_id, invalid_id = _repository_identity(_as_obj(payload).get("repository"))
    if invalid_id:
        return False
    if full is None:
        return True
    allowed = db("SELECT core.repository_event_allowed_with_authority(%s,%s)", (full, repo_id))
    if isinstance(allowed, str):
        return allowed.strip().lower() not in ("f", "false", "0")
    if isinstance(allowed, bool):
        return allowed
    # The production function is non-null boolean and a DB/schema failure raises. None/{} only come from legacy
    # offline adapters that do not model this additive read; absence is not an explicit revocation signal.
    return True


def _handle_installation_event(event_type: str, payload: dict, db, gh) -> dict:
    """INSTALLATION-LIFECYCLE tier: installation (create/suspend/unsuspend/delete) + installation_repositories
    (add/remove). Lifted verbatim from handle_event so the dispatch + return dicts are byte-identical; an
    UNHANDLED action of either event falls through to the same honest `noop` handle_event would have returned."""
    _tp = _trace_log_prefix(payload)
    # ONBOARDING INGRESS: validate lifecycle authority and durably queue each accepted repository. Default-HEAD
    # resolution, graph extraction, current-PR replay, and the final Watching check run only in account-fair
    # convergence turns after this transaction commits.
    if event_type == "installation" and payload.get("action") in ("created", "unsuspend", "new_permissions_accepted"):
        if _ACTIVATION_PROOF_MARKER in payload and not isinstance(payload.get(_ACTIVATION_PROOF_MARKER), dict):
            return {"event": "installation", "action": payload.get("action"), "onboarded": [],
                    "deferred_repos": 0, "watching_signalled": 0,
                    "stale_account_lifecycle_skipped": True}
        if not _reactivate_account(db, _delivery_of(payload) or None, payload=payload):
            return {"event": "installation", "action": payload.get("action"), "onboarded": [],
                    "deferred_repos": 0, "watching_signalled": 0,
                    "stale_account_lifecycle_skipped": True}
        repos = _as_list(payload.get("repositories"))
        # Account-level events may omit the repository list. They reactivate the account but never clear repo-level
        # tombstones; only repository.created / installation_repositories.added can durably bind a full name/id.
        # Apply the same read-only lifecycle gate to payload-named AND All-repositories-discovered entries before
        # any enqueue. A stale account event is account authority, not repo-selection authority.
        skipped = [0]

        def repo_gate(repo_value):
            allowed = _account_onboarding_allowed(db, repo_value)
            if not allowed:
                skipped[0] += 1
            return allowed

        # make_db_processor discovers All-repositories inventory before opening this shared transaction so any
        # repositories it finds can be routed through per-repo locks. If that first read was empty or failed, do
        # not independently repeat it here: a second read could suddenly return repositories outside the frozen
        # per-repo durable plan. Direct handle_event callers carry no marker and retain the normal fallback.
        discovery_attempted = payload.get(_ALLREPOS_DISCOVERY_MARKER) in _ALLREPOS_DISCOVERY_STATES
        onboard_kwargs = {"repo_gate": repo_gate}
        if _accepts_keyword(_queue_onboard_repos, "discover_when_empty"):
            onboard_kwargs["discover_when_empty"] = not discovery_attempted
        onboarded, deferred = _queue_onboard_repos(
            db, gh, repos, **onboard_kwargs)
        return {"event": "installation", "action": payload.get("action"),
                "onboarded": onboarded, "deferred_repos": deferred, "watching_signalled": 0,
                "watching_deferred": "durable_account_convergence",
                "stale_repositories_skipped": skipped[0]}

    # SUSPEND (the install was suspended on GitHub — a REVERSIBLE pause, not an uninstall): a suspended install
    # accepts NO further work (GitHub STOPS delivering its webhooks and revokes the token), yet every in-flight
    # lane the account holds across ALL its repos would otherwise stay HELD until each lease lapses (default 30 min,
    # up to 24 h, and the recall guard can re-queue a swept active claim as 'waiting' when a waiter exists — so the
    # board does NOT reliably go quiet by lease expiry alone). Correct behavior must NOT depend ENTIRELY on
    # GitHub's external masking: at-least-once redelivery and the rolling-deploy 2-instance overlap during the
    # suspend transition can still hand us a live event for a suspended account. So make suspend EXPLICIT (never a
    # silent noop): release the account's in-flight lanes ACCOUNT-WIDE so the board goes quiet by Veripsa's OWN
    # logic. This is the account-wide counterpart to the repo `archived` release (release lanes, KEEP the graph) —
    # NOT the uninstall purge (suspend is reversible; `unsuspend` re-onboards from the retained structure, so the
    # code graph + the append-only event ledger are KEPT). No waiter is promoted (the whole account's lane
    # namespace is emptied — a suspended install can land nothing). Never let a suspend event crash the worker.
    if event_type == "installation" and payload.get("action") == "suspend":
        delivery_key = _delivery_of(payload) or None
        if not delivery_key:
            raise RuntimeError("installation suspend needs durable delivery authority")
        proof = _validated_suspend_proof(payload)
        released = db("SELECT core.release_account_claims_with_authority(%s,%s::jsonb)",
                      (str(delivery_key)[:200],
                       json.dumps(proof, sort_keys=True, separators=(",", ":"))))
        if isinstance(released, str):
            released = json.loads(released)
        if not isinstance(released, dict) or not released.get("ok"):
            raise RuntimeError("installation suspend release did not confirm lifecycle authority")
        return {"event": "installation", "action": "suspended", "released": released}

    if event_type == "installation_repositories" and payload.get("action") == "added":
        if _ACTIVATION_PROOF_MARKER in payload and not isinstance(payload.get(_ACTIVATION_PROOF_MARKER), dict):
            return {"event": "installation_repositories", "onboarded": [], "deferred_repos": 0,
                    "watching_signalled": 0, "stale_account_lifecycle_skipped": True}
        if not _reactivate_account(db, _delivery_of(payload) or None, payload=payload):
            return {"event": "installation_repositories", "onboarded": [], "deferred_repos": 0,
                    "watching_signalled": 0, "stale_account_lifecycle_skipped": True}
        repos = _as_list(payload.get("repositories_added"))
        current_repos, skipped = _reactivate_repositories(db, repos, _delivery_of(payload) or None)
        # repositories_added is an authoritative delta. An empty delta never means "enumerate the whole install".
        onboard_kwargs = (
            {"discover_when_empty": False}
            if _accepts_keyword(
                _queue_onboard_repos, "discover_when_empty")
            else {}
        )
        onboarded, deferred = _queue_onboard_repos(
            db, gh, current_repos, **onboard_kwargs)
        return {"event": "installation_repositories", "onboarded": onboarded, "deferred_repos": deferred,
                "watching_signalled": 0,
                "watching_deferred": "durable_account_convergence",
                "stale_repositories_skipped": skipped}

    # OFFBOARDING (privacy table-stakes — the symmetric counterpart to onboarding): the App was uninstalled, or
    # repos were removed / the repo deleted → FORGET the content-free working set (code graph + live claims) for
    # each affected repo. Uninstall must not leave a customer's code structure sitting in our DB. The append-only
    # event ledger (push/landed audit, content-free) is retained by design (immutable moat); see RUNBOOK residual.
    if event_type == "installation" and payload.get("action") == "deleted":
        # ACCOUNT-WIDE purge (audit r4 privacy fix): forget the ENTIRE pinned tenant's content-free working set
        # (code graph + live claims) across EVERY repo — not just the ones GitHub named in `repositories` (that
        # array is omitted for an "All repositories" install — the common case — so a payload-driven purge could
        # leave a whole tenant's code structure in our DB after uninstall, breaking the "we purge on uninstall"
        # claim). The append-only event ledger is retained by design. (event_processor no longer fans this event
        # out per-repo, so it reaches here account-pinned.)
        return {"event": "installation", "action": "deleted",
                # SQL /1 consumes the explicit App-JWT result under the account lifecycle lock.  ``replacement``
                # records the current generation and returns a stale no-op; ``absent`` authorizes the purge.
                "purged": _purge_account_with_delete_proof(db, payload)}
    if event_type == "installation_repositories" and payload.get("action") == "removed":
        repos = [_as_obj(r) for r in _as_list(payload.get("repositories_removed"))]
        return {"event": "installation_repositories", "action": "removed",
                "purged": [purge_repo(db, r["full_name"], r.get("id"), "installation_removed",
                                      _delivery_of(payload) or None, gh=gh)
                           for r in repos if r.get("full_name")]}
    # UNHANDLED installation / installation_repositories action → the same honest no-op handle_event's tail returns.
    return {"event": event_type, "noop": True}


def _handle_repository_event(event_type: str, payload: dict, db, gh) -> dict:
    """REPOSITORY-LIFECYCLE tier: repository deleted/renamed/transferred/archived. Lifted verbatim from
    handle_event (dispatch + return dicts byte-identical); an UNHANDLED action falls through to the same
    honest `noop` the dispatcher's tail returns."""
    _tp = _trace_log_prefix(payload)
    if event_type == "repository" and payload.get("action") == "deleted":
        repository = _as_obj(payload.get("repository"))
        full = repository.get("full_name")
        return {"event": "repository", "action": "deleted",
                "purged": ([purge_repo(db, full, repository.get("id"), "repository_deleted",
                                       _delivery_of(payload) or None, gh=gh)] if full else [])}

    if event_type == "repository" and payload.get("action") == "created":
        return {"event": "repository", "action": "created",
                "reactivated": _reactivate_repository(
                    db, payload.get("repository"), _delivery_of(payload) or None)}

    # REPO RENAME (same owner, new full_name): the coordinate keys the whole content-free working set by
    # full_name, so a rename would orphan it (the renamed repo reads 'unknown' until its next push). Re-point
    # graph + claims old→new. GitHub's `repository` renamed payload gives the new full_name + the owner + the
    # old short name (changes.repository.name.from); the old full_name = "<owner>/<old short name>".
    if event_type == "repository" and payload.get("action") == "renamed":
        repository = _as_obj(payload.get("repository"))
        new_full = repository.get("full_name")
        owner = _as_obj(repository.get("owner")).get("login")
        old_name = _as_obj(_as_obj(_as_obj(payload.get("changes")).get("repository")).get("name")).get("from")
        if new_full and owner and old_name:
            old_full = f"{owner}/{old_name}"
            res = db("SELECT core.rename_repo_coordinate_with_authority(%s,%s)", (old_full, new_full))
            if isinstance(res, str):
                res = json.loads(res)
            # STAMP the rename-stable repository.id onto the now-migrated coordinate so a LATER rename we DON'T get a
            # webhook for (an owner-login rename) can still be detected by id on the next push (the orphan fix). The
            # explicit old→new rename above already did the move; this is the id-stamp + a belt-and-suspenders probe
            # (no-op if nothing else carries the id). Best-effort: a stamp failure must not fail the rename handler.
            _rid = str(repository.get("id")).strip() if repository.get("id") is not None else ""
            if _rid.isdigit():
                try:
                    db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (new_full, _rid))
                except Exception as _se:
                    print(f"{_tp}rename id-stamp skipped repo={new_full}: {str(_se)[:120]}", flush=True)
            return {"event": "repository", "action": "renamed", "old": old_full, "new": new_full, "repointed": res}
        return {"event": "repository", "action": "renamed", "skipped": "missing rename fields"}

    # REPO TRANSFER (new OWNER → new full_name): a transfer re-keys the repo's full_name (owner part changes,
    # and GitHub also permits changing the short name during the transfer). The coordinate keys the whole working
    # set by full_name. GitHub's
    # `repository` transferred payload gives the new full_name (repository.full_name = "<new owner>/<name>") +
    # the new short name (repository.name), optional old short name (changes.repository.name.from), and old owner
    # (changes.owner.from.{user|organization}.{login,id}); the old full_name uses that old short-name when present.
    # Two CASES, by whether the OWNING ACCOUNT changed:
    #
    #  (A) CROSS-ACCOUNT transfer (old owner id ≠ new owner id — the org/user MOVE case). The graph rows live under
    #      the OLD owner's tenant ('ACCT-GH-'||<old owner id>), but this webhook runs SESSION-PINNED to the NEW
    #      owner (enter_installation_with_authority keyed by repository.owner.id). The same-owner re-point below
    #      (rename_repo_coordinate, UPDATE … WHERE account_id=SESSION=new owner) would match ZERO old-tenant rows →
    #      a SILENT-FALSE ok:true while the old tenant's content-free graph STRANDS under the former owner after the
    #      repo left (privacy/data-residency P1). A clean cross-account ROW MOVE is not RLS-feasible (account_id is
    #      in the PK; tenant_isolation's USING needs current_account=OLD to see the rows while WITH CHECK needs =NEW
    #      to admit the re-key — one GUC can't be both; see 35_lifecycle.sql). So we PURGE the OLD account's
    #      coordinate (owner-context, keyed by the OLD owner id) so NOTHING strands, and signal HONESTLY that the
    #      new coordinate re-ingests on its next push (the working set is content-free + rebuildable from GitHub).
    #
    #  (B) SAME-OWNER transfer (no distinct old/new ids, or ids equal) → behaviour-preserving: the existing
    #      in-place re-point old→new via rename_repo_coordinate_with_authority (UNCHANGED).
    if event_type == "repository" and payload.get("action") == "transferred":
        repository = _as_obj(payload.get("repository"))
        new_full = repository.get("full_name")
        name = repository.get("name")
        old_name_from = _as_obj(
            _as_obj(_as_obj(payload.get("changes")).get("repository")).get("name")
        ).get("from")
        old_name = old_name_from if isinstance(old_name_from, str) and old_name_from.strip() else name
        owner_from = _as_obj(_as_obj(_as_obj(payload.get("changes")).get("owner")).get("from"))
        old_owner_obj = (_as_obj(owner_from.get("user")) if _as_obj(owner_from.get("user")).get("login")
                         else _as_obj(owner_from.get("organization")))
        old_owner = old_owner_obj.get("login")
        # owning-account ids: the NEW owner is repository.owner.id (the SAME id _event_account_key keys the tenant
        # by); the OLD owner is changes.owner.from.{user|organization}.id (same object the old login came from).
        new_owner_id = _as_obj(repository.get("owner")).get("id")
        old_owner_id = old_owner_obj.get("id")
        if new_full and name and old_name and old_owner:
            old_full = f"{old_owner}/{old_name.strip() if isinstance(old_name, str) else old_name}"
            # CROSS-ACCOUNT only when BOTH ids are present AND they differ (a genuine owning-account move). Absent
            # ids (or equal ids) → treat as the same-owner re-point (case B), exactly as before — never guess a
            # cross-account move from logins alone.
            cross_account = (old_owner_id not in (None, "") and new_owner_id not in (None, "")
                             and str(old_owner_id) != str(new_owner_id))
            if cross_account:
                # A cross-tenant purge cannot rely on payload-derived caller arguments alone.  Thread the exact
                # durable delivery id plus GitHub's rename-stable repository.id to the DB; the DB re-derives and
                # authenticates old account/full-name/id/action from that signed, PROCESSING inbox row and applies
                # its receive-time generation boundary before deleting anything.  Missing/malformed authority must
                # retry/fail closed, never fall back to the legacy name-only surface.
                raw_repository_id = repository.get("id")
                repository_id = (str(raw_repository_id).strip()
                                 if raw_repository_id not in (None, "") and not isinstance(raw_repository_id, bool)
                                 else "")
                delivery_key = _delivery_of(payload)
                old_owner_key = (str(old_owner_id).strip()
                                 if not isinstance(old_owner_id, bool) else "")
                new_owner_key = (str(new_owner_id).strip()
                                 if not isinstance(new_owner_id, bool) else "")
                if (not repository_id.isascii() or not repository_id.isdecimal()
                        or repository_id.startswith("0") or len(repository_id) > 32
                        or not old_owner_key.isascii() or not old_owner_key.isdecimal()
                        or old_owner_key.startswith("0") or len(old_owner_key) > 32
                        or not new_owner_key.isascii() or not new_owner_key.isdecimal()
                        or new_owner_key.startswith("0") or len(new_owner_key) > 32):
                    raise RuntimeError("cross-account transfer needs canonical repository and owner ids")
                if not delivery_key:
                    raise RuntimeError("cross-account transfer needs durable delivery authority")
                old_account = f"ACCT-GH-{old_owner_key}"   # SAME key as enter_installation_with_authority
                # Authenticate the exact durable arguments and acquire the former coordinate's transaction lock
                # before the network point read.  If a prior purge committed but finish() did not, its completion
                # marker returns here and recovery never depends on GitHub availability just to finalize that row.
                probe = db("SELECT core.transfer_repo_coordinate_with_authority(%s,%s,%s,%s,%s,%s,%s,%s)",
                           (old_account, old_full, repository_id, delivery_key,
                            "probe", None, None, None))
                if isinstance(probe, str):
                    probe = json.loads(probe)
                if not isinstance(probe, dict) or not probe.get("ok"):
                    raise RuntimeError("cross-account transfer completion probe was not authorized")
                if probe.get("idempotent") is True:
                    return {"event": "repository", "action": "transferred", "cross_account": True,
                            "old": old_full, "new": new_full, "old_account": old_account,
                            "transferred": probe}
                if probe.get("proof_required") is not True:
                    raise RuntimeError("cross-account transfer completion probe returned an invalid state")
                identity_reader = getattr(gh, "repo_current_identity", None)
                if not callable(identity_reader):
                    raise RuntimeError("cross-account transfer needs a scoped current-identity reader")

                def _bounded_current_identity(current):
                    if current is None:
                        return "absent", None, None, None
                    if not isinstance(current, dict):
                        raise RuntimeError("cross-account transfer current identity is malformed")
                    raw_current_repository_id = current.get("id")
                    raw_current_owner_id = current.get("owner_id")
                    current_id = (str(raw_current_repository_id).strip()
                                  if raw_current_repository_id not in (None, "")
                                  and not isinstance(raw_current_repository_id, bool) else "")
                    current_owner = (str(raw_current_owner_id).strip()
                                     if raw_current_owner_id not in (None, "")
                                     and not isinstance(raw_current_owner_id, bool) else "")
                    current_full = current.get("full_name")
                    if (not current_id.isascii() or not current_id.isdecimal()
                            or current_id.startswith("0") or len(current_id) > 32
                            or not current_owner.isascii() or not current_owner.isdecimal()
                            or current_owner.startswith("0") or len(current_owner) > 32
                            or not isinstance(current_full, str) or not current_full
                            or len(current_full) > 512):
                        raise RuntimeError("cross-account transfer current identity is malformed")
                    return "found", current_id, current_owner, current_full

                app_installation_reader = getattr(
                    gh, "repo_current_identity_via_app_installation", None)

                def _read_current_identity(repo):
                    raw_current_identity = identity_reader(repo)
                    if raw_current_identity is None and callable(app_installation_reader):
                        # B's installation token cannot see a private repository already moved onward to C.
                        # Resolve only this 404 through App-JWT installation authority, then re-read with C's
                        # installation token. No App installation means honest unknown/non-destructive.
                        raw_current_identity = app_installation_reader(repo)
                    return _bounded_current_identity(raw_current_identity)

                current_identity = _read_current_identity(new_full)
                current_state, current_repository_id, current_owner_id, current_full_name = current_identity
                if (current_state == "found" and current_repository_id == repository_id
                        and current_owner_id != old_owner_key
                        and (current_owner_id != new_owner_key or current_full_name != new_full)):
                    # GitHub followed a rename or a rapid onward transfer. The worker's outer session lock covers
                    # payload owner/name, not this canonical coordinate. Ask the DB to bind and xact-lock the exact
                    # first proof, then repeat the point read while that lock is held. Any move/rename/404 between
                    # reads retries the durable event; an old proof is never used to clear a newer transfer marker.
                    locked = db(
                        "SELECT core.transfer_repo_coordinate_with_authority(%s,%s,%s,%s,%s,%s,%s,%s)",
                        (old_account, old_full, repository_id, delivery_key, "lock_current",
                         current_repository_id, current_owner_id, current_full_name))
                    if isinstance(locked, str):
                        locked = json.loads(locked)
                    if not isinstance(locked, dict) or locked.get("current_reproof_required") is not True:
                        raise RuntimeError("cross-account transfer current-coordinate lock was not authorized")
                    reproof = _read_current_identity(current_full_name)
                    if reproof != current_identity:
                        raise RuntimeError("cross-account transfer current identity changed during locked proof")
                res = db("SELECT core.transfer_repo_coordinate_with_authority(%s,%s,%s,%s,%s,%s,%s,%s)",
                         (old_account, old_full, repository_id, delivery_key,
                          current_state, current_repository_id, current_owner_id, current_full_name))
                if isinstance(res, str):
                    res = json.loads(res)
                if not isinstance(res, dict) or not res.get("ok"):
                    raise RuntimeError("cross-account transfer purge was not durably authorized")
                reactivated = None
                if (res.get("ownership_isolated") is True
                        and current_owner_id == new_owner_key
                        and res.get("current_account_owns_repository") is True):
                    # The live point read proves where this stable object is NOW. Activate that current coordinate
                    # (which may be a same-owner redirect from the webhook's original full_name) only after the old
                    # account is purged/isolated and its completion marker is durable in this same DB transaction.
                    # A rapid A→B→C proof still isolates A, but must never activate C inside tenant B.
                    # Any failure rolls the whole event transaction back, including the old tombstone + marker.
                    reactivated = _reactivate_repository(
                        db, {"full_name": current_full_name, "id": repository_id}, delivery_key)
                    if (reactivated.get("activated") is not True
                            or reactivated.get("stale_lifecycle_event") is not False):
                        raise RuntimeError("cross-account transfer current repository was not activated")
                return {"event": "repository", "action": "transferred", "cross_account": True,
                        "old": old_full, "new": new_full, "old_account": old_account,
                        "transferred": res, "reactivated": reactivated}
            res = db("SELECT core.rename_repo_coordinate_with_authority(%s,%s)", (old_full, new_full))
            if isinstance(res, str):
                res = json.loads(res)
            return {"event": "repository", "action": "transferred", "old": old_full, "new": new_full, "repointed": res}
        return {"event": "repository", "action": "transferred", "skipped": "missing transfer fields"}

    # REPO ARCHIVE: an archived repo accepts NO further pushes and merges NOTHING — yet its in-flight lanes
    # (every active/waiting claim across all branches) would otherwise stay HELD until each lease expires,
    # reading as falsely in-flight. Release them all repo-wide (the withdraw half of the lifecycle applied to
    # the whole repo; no waiter is promoted — the repo's lane namespace is emptied since it can land nothing).
    # The code graph + the append-only event ledger are KEPT (archived structure is still real history;
    # deletion's purge is the only thing that forgets the graph). Mirrors the renamed/deleted handlers' style.
    if event_type == "repository" and payload.get("action") == "archived":
        full = _as_obj(payload.get("repository")).get("full_name")
        if not full:
            return {"event": "repository", "action": "archived", "skipped": "missing repository full_name"}
        res = None
        try:
            res = db("SELECT core.release_repo_claims_with_authority(%s)", (full,))
            if isinstance(res, str):
                res = json.loads(res)
        except Exception as e:                                  # never let an archive event crash the worker
            print(f"{_tp}archive release skipped repo={full}: {str(e)[:120]}", flush=True)
        return {"event": "repository", "action": "archived", "repo": full, "released": res}
    # UNHANDLED repository action → the same honest no-op handle_event's tail returns.
    return {"event": event_type, "noop": True}


def _pr_guard(payload: dict, db, gh) -> dict | None:
    """PULL_REQUEST early guard / early-exit block. Returns a SHORT-CIRCUIT result dict to return verbatim from
    handle_event, OR None to continue into the full analyze body (_handle_pull_request_event). Lifted verbatim:
    malformed-payload no-op and off-protected-branch retarget release — the early exits
    the pull_request path takes before any analysis. (The empty-files honest-unknown guard depends on values
    computed mid-body, so it stays in _handle_pull_request_event.)"""
    # POISON-EVENT TOLERANCE: a malformed / partial payload (missing the PR number, repo, base ref, or head
    # sha we rely on below) must be a clean NO-OP, never an exception the worker has to catch + count as a
    # 'failed' delivery. We .get() everything and bail early if the essentials are absent.
    action = payload.get("action")
    repository = _as_obj(payload.get("repository"))
    repo = repository.get("full_name")
    prj = _as_obj(payload.get("pull_request"))
    pr = payload.get("number")
    base = _as_obj(prj.get("base")).get("ref")
    head = _as_obj(prj.get("head"))
    head_sha = head.get("sha")
    if not (repo and pr is not None and base and head_sha):
        return {"event": "pull_request", "skipped": "malformed payload (missing repo/number/base/head)"}

    # MAIN-PROTECTION SCOPE: Veripsa governs what LANDS on the protected branch — the repo's DEFAULT branch
    # (the only branch whose graph we ingest, on push). A PR targeting any OTHER base (a feature branch, a
    # release branch, a stacked PR) is out of scope: we have no graph for it, so analyzing it would emit only
    # noisy 'unknown'. Skip it entirely — no check, no comment. (A team-configurable protected branch is a
    # later enhancement; default_branch is the right v1 signal and matches the push-side filter.)
    default_branch = repository.get("default_branch") or "main"
    if base != default_branch:
        # BASE-RETARGET LEAK (an `edited` whose changes.base moved the PR OFF the protected branch): the PR
        # used to head to main and reserved lanes there ('PR-<n>' on default_branch). Now it targets a
        # feature/release branch — out of scope, but its OLD protected-branch claims would otherwise stay
        # active FOREVER, blocking every future PR touching those files behind a ghost. So RELEASE this
        # change's protected-branch lanes before bailing (promotes any waiter — same as a withdraw). This is
        # the cancel half of the retarget; the new-coordinate path is closed separately by
        # reconcile_change_claims on a later synchronize. Idempotent + content-free: a PR genuinely opened
        # ON a non-default base never had protected-branch claims → release frees nothing (clean no-op). The
        # never-crash invariant holds: a release error is logged, never raised out of the handler.
        released = None
        try:
            released = db("SELECT core.release_change_on_main_with_authority(%s,%s,%s)",
                          (_change_id(pr), repo, default_branch))
        except Exception as e:
            _tp = _trace_log_prefix(payload)
            print(f"{_tp}retarget release skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
        return {"event": "pull_request", "action": action, "repo": repo, "pr_number": pr,
                "released_off_protected": released,
                "skipped": f"base '{base}' is not the protected branch '{default_branch}'"}

    # DRAFT PRs ARE analyzed — they are the SCOUT WINDOW for in-flight traffic control (PO 2026-06-25 round-2
    # framing): humans and AI agents (codex on game-app) often open as draft precisely to SEE Core's
    # verdict on the in-flight set BEFORE rebase. Skipping draft = silently denying the scout exactly when the
    # signal would help most. The earlier "draft = skip" guard returned EARLY here for every analyze action of
    # a draft PR — REMOVED. Drafts now flow through declare/check/comment like any other PR; the cross-state
    # softening (a draft↔non-draft same-file overlap demotes to 'warn', never the hard 'serialize' that pauses
    # a non-draft behind a still-iterating scout) lives in the verdict CTE (db/schema/80_contention.sql).
    analyze_actions = _PR_ANALYZE_ACTIONS
    # PAUSE-ACK (一時停止): the `veripsa-ack` LABEL is the ACK signal — an autonomous agent adds it
    # (`gh pr edit --add-label veripsa-ack`) or a human clicks it. The App is ALREADY subscribed to the
    # `pull_request` event, so its `labeled`/`unlabeled` actions arrive with NO new event subscription. We
    # re-analyze THIS PR on an ack-label change so its check flips between `action_required` (paused) and
    # `neutral` (acknowledged) — but ONLY when the label that changed is OUR ack label (any other label is
    # noise we must not re-render on). It is a cheap, THIS-PR-ONLY re-evaluation: it changes no files and no
    # other PR's verdict, so the neighbor refresh is suppressed (no fan-out storm on a label click).
    changed_label = _as_obj(payload.get("label")).get("name")
    is_ack_label_event = action in ("labeled", "unlabeled") and changed_label == ACK_LABEL
    # BASE-RETARGET ONTO the protected branch (a `pull_request.edited` whose changes.base moved the PR back
    # onto main): GitHub fires ONLY `edited` for a base change (no `synchronize` — no head commit moved), so
    # without treating it as an analyze action a PR (re)heading to main would NEVER declare its lanes or get a
    # check = a SILENT MISS of a real in-flight PR. So an `edited` carrying `changes.base` (base already known
    # == default_branch; the off-main case returned above) is analyzed exactly like an open. It is ALSO a
    # RE-ACTIVATION: if this PR was previously retargeted OFF main it was tombstoned
    # (release_change_on_main → change_concluded), so it must bypass the stale-event concluded guard — which
    # it does naturally in handle_pull_request (action=='edited' is not in the guarded 'opened'/'synchronize'
    # set), re-activating the lanes the guard would otherwise silence forever.
    is_base_retarget = action == "edited" and "base" in _as_obj(payload.get("changes"))
    should_analyze = action in analyze_actions or is_base_retarget or is_ack_label_event
    return None


def _pr_eligibility(payload: dict, db) -> tuple[dict | None, dict]:
    """GUARD + FIELD-DERIVATION phase. Derives the per-event scalar bundle `f` (the values every later sub-phase
    reads) ONCE, and re-runs the three early-exit guards (malformed payload / off-protected-branch retarget-
    release). Returns (short_circuit_result | None, f): a non-None first element is the dict to
    return verbatim; otherwise continue with `f`. The guards are byte-identical to _pr_guard (reached only after
    _pr_guard already returned None, so they are belt-and-suspenders no-ops on the happy path) — kept here so the
    analyze body still derives + protects its own inputs in ONE place. Pure over (payload, db): the only side
    effect is the off-protected-branch lane release (the never-crash release is logged, never raised)."""
    # POISON-EVENT TOLERANCE: a malformed / partial payload (missing the PR number, repo, base ref, or head
    # sha we rely on below) must be a clean NO-OP, never an exception the worker has to catch + count as a
    # 'failed' delivery. We .get() everything and bail early if the essentials are absent.
    action = payload.get("action")
    repository = _as_obj(payload.get("repository"))
    repo = repository.get("full_name")
    prj = _as_obj(payload.get("pull_request"))
    pr = payload.get("number")
    base = _as_obj(prj.get("base")).get("ref")
    base_sha = _as_obj(prj.get("base")).get("sha")          # the BASE COMMIT sha (for the freshness tree read)
    head = _as_obj(prj.get("head"))
    head_sha = head.get("sha")
    if not (repo and pr is not None and base and head_sha):
        return {"event": "pull_request", "skipped": "malformed payload (missing repo/number/base/head)"}, {}

    # MAIN-PROTECTION SCOPE: Veripsa governs what LANDS on the protected branch — the repo's DEFAULT branch
    # (the only branch whose graph we ingest, on push). A PR targeting any OTHER base (a feature branch, a
    # release branch, a stacked PR) is out of scope: we have no graph for it, so analyzing it would emit only
    # noisy 'unknown'. Skip it entirely — no check, no comment. (A team-configurable protected branch is a
    # later enhancement; default_branch is the right v1 signal and matches the push-side filter.)
    default_branch = repository.get("default_branch") or "main"
    if base != default_branch:
        # BASE-RETARGET LEAK (an `edited` whose changes.base moved the PR OFF the protected branch): the PR
        # used to head to main and reserved lanes there ('PR-<n>' on default_branch). Now it targets a
        # feature/release branch — out of scope, but its OLD protected-branch claims would otherwise stay
        # active FOREVER, blocking every future PR touching those files behind a ghost. So RELEASE this
        # change's protected-branch lanes before bailing (promotes any waiter — same as a withdraw). This is
        # the cancel half of the retarget; the new-coordinate path is closed separately by
        # reconcile_change_claims on a later synchronize. Idempotent + content-free: a PR genuinely opened
        # ON a non-default base never had protected-branch claims → release frees nothing (clean no-op). The
        # never-crash invariant holds: a release error is logged, never raised out of the handler.
        released = None
        try:
            released = db("SELECT core.release_change_on_main_with_authority(%s,%s,%s)",
                          (_change_id(pr), repo, default_branch))
        except Exception as e:
            _tp = _trace_log_prefix(payload)
            print(f"{_tp}retarget release skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
        return ({"event": "pull_request", "action": action, "repo": repo, "pr_number": pr,
                 "released_off_protected": released,
                 "skipped": f"base '{base}' is not the protected branch '{default_branch}'"}, {})

    # FORK PR: the head commit lives in the contributor's FORK, not the base repo. The base-repo Files API
    # still lists the PR's changed files (works), but a CHECK RUN created on the base repo at the fork's head
    # sha can be rejected (the sha isn't in the base repo) — so we detect it and let the check degrade to
    # comment-only (the PR comment always posts on the base repo). head.repo.id != base.repo.id ⇒ fork.
    head_repo_id = _as_obj(head.get("repo")).get("id")
    base_repo_id = _as_obj(_as_obj(prj.get("base")).get("repo")).get("id")
    is_fork = (head_repo_id is not None and base_repo_id is not None and head_repo_id != base_repo_id)
    # A deleted fork can have head.repo=null in both normal webhooks and synthetic current-PR replays. Treating that
    # unknown as same-repo would expose the base repo's in-flight paths/identities. Redact on missing identity while
    # keeping `is_fork` confirmed-only so required-check / pause behavior is never weakened by uncertainty.
    fork_identity_unknown = (head_repo_id is None or base_repo_id is None
                             or payload.get("_veripsa_fork_identity_unknown") is True)
    redact_external = is_fork or fork_identity_unknown

    # DRAFT PRs ARE analyzed — they are the SCOUT WINDOW for in-flight traffic control (PO 2026-06-25 round-2
    # framing). The earlier "draft = skip" guard returned EARLY here for every analyze action of a draft PR —
    # REMOVED. See _pr_guard above for the same rationale; the cross-state softening (a draft↔non-draft same-file
    # overlap demotes to 'warn', never the hard 'serialize' that would pause a non-draft behind a still-iterating
    # scout) lives in the verdict CTE (db/schema/80_contention.sql).
    analyze_actions = _PR_ANALYZE_ACTIONS
    # PAUSE-ACK (一時停止): the `veripsa-ack` LABEL is the ACK signal — an autonomous agent adds it
    # (`gh pr edit --add-label veripsa-ack`) or a human clicks it. The App is ALREADY subscribed to the
    # `pull_request` event, so its `labeled`/`unlabeled` actions arrive with NO new event subscription. We
    # re-analyze THIS PR on an ack-label change so its check flips between `action_required` (paused) and
    # `neutral` (acknowledged) — but ONLY when the label that changed is OUR ack label (any other label is
    # noise we must not re-render on). It is a cheap, THIS-PR-ONLY re-evaluation: it changes no files and no
    # other PR's verdict, so the neighbor refresh is suppressed (no fan-out storm on a label click).
    changed_label = _as_obj(payload.get("label")).get("name")
    is_ack_label_event = action in ("labeled", "unlabeled") and changed_label == ACK_LABEL
    # BASE-RETARGET ONTO the protected branch (a `pull_request.edited` whose changes.base moved the PR back
    # onto main): GitHub fires ONLY `edited` for a base change (no `synchronize` — no head commit moved), so
    # without treating it as an analyze action a PR (re)heading to main would NEVER declare its lanes or get a
    # check = a SILENT MISS of a real in-flight PR. So an `edited` carrying `changes.base` (base already known
    # == default_branch; the off-main case returned above) is analyzed exactly like an open. It is ALSO a
    # RE-ACTIVATION: if this PR was previously retargeted OFF main it was tombstoned
    # (release_change_on_main → change_concluded), so it must bypass the stale-event concluded guard — which
    # it does naturally in handle_pull_request (action=='edited' is not in the guarded 'opened'/'synchronize'
    # set), re-activating the lanes the guard would otherwise silence forever.
    is_base_retarget = action == "edited" and "base" in _as_obj(payload.get("changes"))
    should_analyze = action in analyze_actions or is_base_retarget or is_ack_label_event

    # FAITHFUL LANDING SHA: on a MERGE, the commit that actually reaches main is the MERGE commit
    # (merge_commit_sha), NOT the PR's feature-branch head (head_sha — which never lands on main). The
    # landing-record path (land_change → record_push) must key on the real on-main commit so it DEDUPES
    # against the separate `push`-to-main webhook GitHub also fires for the same merge (both key the push
    # event on the same sha → ONE push fact, not two). Without this, a merge recorded a FALSE second push
    # at head_sha — inflating landings/pushes counts for a commit that is not on main. Fall back to head_sha
    # only when merge_commit_sha is absent (a squash-in-progress / old payload).
    merged = bool(prj.get("merged"))
    head_ref_name = head.get("ref")
    merge_commit_sha = (prj.get("merge_commit_sha") or head_sha or "")[:64]
    land_sha = merge_commit_sha if (action == "closed" and merged) else head_sha
    author = _as_obj(prj.get("user")).get("login") or "unknown"
    # SEAT METERING (content-free): is the PR author a Bot? The stored login is sanitized (the '[bot]' marker
    # is stripped), so the bot signal canNOT be recovered downstream — it must be read HERE from the webhook's
    # pull_request.user.type ('Bot' vs 'User'/'Organization'). A human author = a SEAT (the PLG value line); a
    # bot = free (the AI-fleet wedge). Threaded into the gate so _seat_count meters humans, not bots.
    author_is_bot = (_as_obj(prj.get("user")).get("type") == "Bot")
    # SCOUT-WINDOW state (PO 2026-06-25 round-2): the PR's draft flag is threaded through to the gate so the
    # engine softens a draft↔non-draft same-file overlap to 'warn' (never the hard 'serialize' that would pause
    # a non-draft behind a still-iterating scout). Content-free (a boolean only).
    is_draft = bool(prj.get("draft"))
    # PAUSE-ACK: a `labeled`/`unlabeled` of our ack label is NOT one of the brain's analyze actions — but it
    # must produce a fresh check (the pause flips on the label). So the brain re-analyzes it exactly like a
    # `synchronize` (re-declare the SAME lanes idempotently, recompute the verdict): the files are unchanged,
    # so reconcile is a no-op and the verdict is identical to the last real event — only the pause-tier overlay
    # (below) changes. The handler's OWN `action` stays 'labeled'/'unlabeled' for the post-path decisions; only
    # the brain sees a 'synchronize'. (Keeps the analyze logic in ONE place; no second brain code path.)
    brain_action = "synchronize" if is_ack_label_event else action
    return None, {
        "action": action, "repo": repo, "prj": prj, "pr": pr,
        "base": base, "base_sha": base_sha, "head": head, "head_sha": head_sha,
        "repository_id": repository.get("id"),
        "default_branch": default_branch, "is_fork": is_fork, "redact_external": redact_external,
        "is_ack_label_event": is_ack_label_event,
        "is_base_retarget": is_base_retarget, "should_analyze": should_analyze, "merged": merged,
        "head_ref_name": head_ref_name, "merge_commit_sha": merge_commit_sha, "land_sha": land_sha,
        "author": author, "author_is_bot": author_is_bot, "is_draft": is_draft, "brain_action": brain_action,
        # AUTHORITATIVE REPLAY: branch-push/check-suite backstops synthesize a PR event only after re-fetching
        # the CURRENT open PR from GitHub. Carry that through the normalized brain event so a stale conclusion
        # tombstone cannot suppress a live PR forever. Real webhook redeliveries do not set this flag.
        "authoritative_pr_replay": bool(payload.get("_veripsa_authoritative_pr_replay")),
        # PER-EVENT TRACE-ID (Round-2 observability follow-up): the dispatcher minted a uuid4 trace_id and stashed
        # it on the payload; carry it on `f` so every per-PR sub-phase (_pr_fetch_changed / _pr_pre_brain /
        # _pr_apply_pause_ack_overlay / _pr_post_check_and_comment / _pr_record_landing / _pr_unread_files_result
        # / _pr_quota_paused_result) can tag its log lines with the SAME id without re-reading the payload.
        "trace_id": _trace_of(payload),
        # Durable delivery id/key is the bridge from ingress logs to this worker attempt's trace_id. Content-free:
        # GitHub delivery id or local fallback hash, never a body/diff.
        "delivery": _delivery_of(payload),
    }


def _pr_fetch_changed(gh, f: dict, *, planned_onboarding: bool = False) -> tuple:
    """CHANGED-FILES fetch (with NEW-side line ranges) + ADDED-PATH STATUS + CONFLICT-MARKER FINDINGS +
    shortfall detection. Returns (changed, changed_ranges, added_paths, conflict_markers, suspect_shortfall,
    pr_changed_count). Reads `gh` only; the empty/shortfall ADVISORY post stays in _pr_unread_files_result
    so this stays a pure read.

    `added_paths` (PO 2026-06-25 honest-verdict refinement) is the subset of changed files whose Files-API
    `status` is `added` — i.e. NEW IN THIS PR (not present at base). The renderer uses it to split the lumped
    `unknown_paths` arm into two semantically-distinct sentences: case (a) "new in this PR — expected, coupling
    becomes computable after merge", case (b) "modified path NOT in main's graph — possible extractor gap".
    Lumping both as "Unknown" trains customers to ignore the verdict. FAIL-OPEN: an older Fake client (no
    list_pr_added_paths) or a transient error returns [] → the renderer simply degrades to today's lumped wording.
    Bounded to the same code-path filter the cap applies to changed (in _pr_pre_brain) so a non-code added file
    (a new README) does not surface a false "new code" line.

    `conflict_markers` (PO 2026-06-25 dogfood hole-fix — PRs #111/#114): findings of UNRESOLVED git merge
    markers (`<<<<<<<` / `=======` / `>>>>>>>`) introduced in this PR's added lines. Each finding is
    {"path", "line", "kind"} — line numbers ONLY, never the line body (content-free, same discipline as the
    line-range geometry). The renderer escalates THIS PR to action_required (with a top-of-comment block
    naming the path + line) when any finding is present — these are 100%-certain build-breakers (npm/python
    parsers choke on the marker shape), so a hard-fail block is honest, not over-flagging. FAIL-OPEN: an
    older Fake client (no list_pr_conflict_markers) or a transient error returns [] → no escalation,
    behavior-preserving for clean PRs."""
    should_analyze, repo, pr, prj = f["should_analyze"], f["repo"], f["pr"], f["prj"]
    _tp = _f_trace_prefix(f)
    # FINER COLLISION: fetch the PR's changed files WITH their NEW-side changed line ranges (parsed from the
    # diff HUNK HEADERS only — the +/- body is discarded in github_rest, never stored/logged → content-free).
    # Fall back to filenames-only when the ranges call is unavailable (older Fake client / transient error):
    # no ranges → the engine uses FILE-level collision (the safety net), never a miss.
    changed_ranges = {}
    added_paths: list[str] = []
    conflict_markers: list = []         # unresolved git conflict markers introduced by this PR (dogfood hole-fix)
    raw_entry_count = 0                  # Files API entries; unlike changed paths, a rename counts once
    pr_changed_count = _as_int(prj.get("changed_files"))   # declared by the PR object; used for empty + shortfall guards
    _files_issue = None                 # None | shortfall | malformed — both error kinds withhold lane mutation
    _files_metadata_fetched = False
    if should_analyze:
        # Installation replay is a fair one-PR turn under a 300s exact lease. GitHub's Files endpoint can expose
        # up to 30 pages for a 3,000-file PR; letting the ordinary acting path traverse all of them can consume the
        # entire worker lease and retry the same immutable cursor forever. The authoritative PR object already
        # declares the entry count: >100 is an exact over-budget proof and therefore performs ZERO Files calls.
        # For <=100, require the one-pass metadata API with max_pages=1. A contradictory next-page link becomes
        # honest Unknown below; it never falls through to an unbounded filenames-only traversal. Ordinary live
        # webhooks keep their historical uncapped acting-path behavior.
        planned_files_over_budget = (
            planned_onboarding and pr_changed_count > 100
        )
        planned_reader_unavailable = (
            planned_onboarding and not hasattr(gh, "list_pr_file_metadata")
        )
        if planned_files_over_budget or planned_reader_unavailable:
            _files_issue = "planned_page_budget"
            changed_ranges, changed, raw_entry_count = {}, [], 0
        else:
            try:
                # Pass pr_changed_files so the pagination methods can detect a shortfall (returned < declared)
                # and raise PRFilesShortfall rather than silently returning a partial list. A partial list is
                # never analyzable and must not fall back to another traversal of the same endpoint.
                if hasattr(gh, "list_pr_file_metadata"):
                    _meta = (
                        gh.list_pr_file_metadata(
                            repo, pr, pr_changed_count, max_pages=1)
                        if planned_onboarding else
                        gh.list_pr_file_metadata(repo, pr, pr_changed_count)
                    )
                    changed_ranges = dict(_meta.get("changed_ranges") or {})
                    changed = _as_list(_meta.get("changed")) or list(changed_ranges.keys())
                    added_paths = _as_list(_meta.get("added_paths")) or []
                    _cm = _meta.get("conflict_markers")
                    conflict_markers = _cm if isinstance(_cm, list) else []
                    _raw_count = _meta.get("raw_entry_count")
                    raw_entry_count = (
                        _raw_count
                        if isinstance(_raw_count, int) and not isinstance(_raw_count, bool)
                        and _raw_count >= 0 else len(changed)
                    )
                    _files_metadata_fetched = True
                else:
                    changed_ranges = (
                        gh.list_pr_files_with_ranges(repo, pr, pr_changed_count)
                        if hasattr(gh, "list_pr_files_with_ranges") else {}
                    )
                    changed = (
                        list(changed_ranges.keys()) if changed_ranges else
                        _as_list(gh.list_pr_files(repo, pr, pr_changed_count))
                    )
                    raw_entry_count = len(changed)
            except PRFilesPageBudgetExceeded as e:
                if not planned_onboarding:
                    raise
                print(f"{_tp}pull_request {repo} pr={pr} → planned PR Files page ceiling "
                      f"({e.used}/{e.budget}) — will withhold verdict as honest-unknown "
                      "(lanes NOT released)", flush=True)
                _files_issue = "planned_page_budget"
                changed_ranges, changed, raw_entry_count = {}, [], 0
            except PRFilesShortfall as e:
                # A partial read is not analyzable. Do not retry the same endpoint through a fallback.
                print(f"{_tp}pull_request {repo} pr={pr} → PR files SHORTFALL "
                      f"({e.returned} returned vs {e.declared} declared) — will withhold verdict as "
                      "honest-unknown (lanes NOT released)", flush=True)
                _files_issue = "shortfall"
                changed_ranges, changed, raw_entry_count = {}, [], 0
            except PRFilesMalformed as e:
                print(f"{_tp}pull_request {repo} pr={pr} → PR files MALFORMED ({str(e)[:100]}) — "
                      "will withhold verdict as honest-unknown (lanes NOT released)", flush=True)
                _files_issue = "malformed"
                changed_ranges, changed, raw_entry_count = {}, [], 0
            except Exception as e:
                if planned_onboarding:
                    # A missing max_pages seam or other metadata failure is not permission to invoke the legacy
                    # unbounded fallback. Preserve lanes and publish the bounded honest-Unknown receipt.
                    print(f"{_tp}planned PR files read unavailable repo={repo} pr={pr}: {str(e)[:120]} — "
                          "will withhold verdict as honest-unknown (lanes NOT released)", flush=True)
                    _files_issue = "planned_page_budget"
                    changed_ranges, changed, raw_entry_count = {}, [], 0
                else:
                    print(f"{_tp}pr files-with-ranges fell back to names repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
                    changed_ranges, changed = {}, _as_list(gh.list_pr_files(repo, pr))
                    raw_entry_count = len(changed)
        # ADDED-PATH STATUS: only worth fetching when we actually got a path set to analyze (no shortfall, not
        # empty) — otherwise the renderer's `unknown_paths` arm is moot and the extra paginated call is wasted.
        # FAIL-OPEN: older Fake clients have no list_pr_added_paths → degrade to [] = today's lumped copy.
        if not _files_metadata_fetched and changed and hasattr(gh, "list_pr_added_paths"):
            try:
                added_paths = _as_list(gh.list_pr_added_paths(repo, pr)) or []
            except Exception as e:
                # ADVISORY enrichment only — a read error must never crash the verdict or the surrounding fetch.
                print(f"{_tp}pr added-paths read skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
                added_paths = []
        # CONFLICT-MARKER FINDINGS (dogfood hole-fix, PO 2026-06-25 PRs #111/#114): scan the PR's diff patches
        # for unresolved git merge markers. CONTENT-FREE: only path + new-side line + a 3-value kind label
        # cross — never the surrounding line body (see conflict_markers_from_patch). Fetched only when we have
        # files to analyze. FAIL-OPEN: older Fake clients (no list_pr_conflict_markers) or a transient error
        # returns [] → no escalation, behavior-preserving for existing call sites + clean PRs.
        if not _files_metadata_fetched and changed and hasattr(gh, "list_pr_conflict_markers"):
            try:
                _cm = gh.list_pr_conflict_markers(repo, pr)
                if isinstance(_cm, list):
                    conflict_markers = _cm
            except Exception as e:
                print(f"{_tp}pr conflict-markers read skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
                conflict_markers = []
    else:
        changed = []
    return (changed, changed_ranges, added_paths, conflict_markers, _files_issue,
            pr_changed_count, raw_entry_count)


def _pr_files_snapshot_is_current(gh, f: dict, pr_changed_count: int,
                                  raw_changed_count: int) -> bool | None:
    """Prove the unversioned Files read still belongs to the webhook's PR head before any lane mutation.

    A delayed event can say head A while GitHub's Files endpoint already serves B. Stamping those B paths/ranges
    as A would defeat the durable claim/head boundary and can later overwrite B with a stale neighbor refresh.
    Production clients support the authoritative PR read; missing/malformed/raced evidence is a conservative
    no-op and the current synchronize/backfill event will reconcile it.
    """
    read = getattr(gh, "get_pull_request", None)
    if not callable(read):
        # Legacy/offline fakes can still exercise the acting path, but the brain event is marked unverified and
        # the DB setter clears its head proof. Production clients implement this method.
        return None
    try:
        current = read(f["repo"], f["pr"])
    except Exception:
        return False
    if not isinstance(current, dict) or current.get("state") != "open" or current.get("merged") is True:
        return False
    head = current.get("head") if isinstance(current.get("head"), dict) else {}
    base = current.get("base") if isinstance(current.get("base"), dict) else {}
    if head.get("sha") != f.get("head_sha") or base.get("ref") != f.get("base"):
        return False
    payload_base_sha = f.get("base_sha")
    if payload_base_sha and base.get("sha") != payload_base_sha:
        return False
    current_declared = current.get("changed_files")
    if (not isinstance(current_declared, int) or isinstance(current_declared, bool)
            or current_declared < 0 or current_declared != raw_changed_count):
        # The webhook may omit changed_files; the authoritative post-read therefore owns the completeness
        # check. Without this unconditional count equality, a partial Files response fetched with count=0 could
        # be stamped as the current head and silently reconcile away unread lanes.
        return False
    payload_declared = f.get("prj", {}).get("changed_files") if isinstance(f.get("prj"), dict) else None
    if isinstance(payload_declared, int) and not isinstance(payload_declared, bool):
        if current_declared != payload_declared or pr_changed_count != payload_declared:
            return False
    return True


def _pr_unread_files_result(
        gh, f: dict, pr_changed_count: int, files_issue: str | None, graph_heal, *,
        planned_onboarding_graph_proof=None) -> dict:
    """EMPTY-FILES / SHORTFALL honest-unknown ADVISORY post + the short-circuit result dict. Called ONLY when
    suspect_empty_files is True (the caller already decided to withhold the verdict). Posts the advisory check +
    comment (best-effort) and returns the handler result with its private acting-Check outcome."""
    repo, pr, head_sha, is_fork, action, default_branch = (
        f["repo"], f["pr"], f["head_sha"], f["is_fork"], f["action"], f["default_branch"])
    _tp = _f_trace_prefix(f)
    uchk = unread_files_check()
    # PRE-POST THE ADVISORY COMMENT (PO #3 details_url) so the check anchors at the verdict comment.
    comment_id = None
    comment_ok = False
    try:
        resp = gh.upsert_comment(repo, pr, _comment_marker(pr),
                                 _marked_comment(pr, unread_files_comment_body(default_branch)))
        comment_ok = True
        if isinstance(resp, dict):
            cid = resp.get("id")
            if isinstance(cid, int):
                comment_id = cid
    except Exception as e:
        print(f"{_tp}unread-files note skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
    check_meta = _upsert_check_result(gh, repo, head_sha, uchk["conclusion"], uchk["title"], uchk["summary"],
                                      is_fork, pr_number=pr, comment_id=comment_id)
    _log_pr_surface(
        _tp, surface="unread-files", delivery=f.get("delivery", ""), action=action, repo=repo, pr_number=pr,
        head_sha=head_sha, comment_needed=True, comment_ok=comment_ok, comment_id=comment_id,
        check_meta=check_meta)
    if files_issue == "shortfall":
        print(f"{_tp}pull_request {repo} pr={pr} → PR-FILES PAGINATION SHORTFALL (partial read) — "
              f"posted advisory 'not analyzed' (lanes NOT released, verdict withheld; honest-unknown)", flush=True)
    elif files_issue == "malformed":
        print(f"{_tp}pull_request {repo} pr={pr} → PR-FILES MALFORMED — posted advisory 'not analyzed' "
              "(lanes NOT released, verdict withheld; honest-unknown)", flush=True)
    elif files_issue == "planned_page_budget":
        print(f"{_tp}pull_request {repo} pr={pr} → PLANNED PR-FILES ONE-PAGE CEILING — posted advisory "
              "'not analyzed' (lanes NOT released, verdict withheld; honest-unknown)", flush=True)
    else:
        print(f"{_tp}pull_request {repo} pr={pr} → CHANGED-FILES READ EMPTY but PR reports {pr_changed_count} changed "
              f"file(s) — posted advisory 'not analyzed' (lanes NOT released, verdict withheld; honest-unknown)", flush=True)
    skipped_reason = ("pr-files pagination shortfall — partial read, honest-unknown, not analyzed"
                      if files_issue == "shortfall" else
                      "pr-files response malformed — honest-unknown, not analyzed"
                      if files_issue == "malformed" else
                      "planned pr-files one-page ceiling — honest-unknown, not analyzed"
                      if files_issue == "planned_page_budget" else
                      "changed-files API returned empty while PR reports changed_files>0 — honest-unknown, not analyzed")
    result = {
        "event": "pull_request", "action": action, "repo": repo, "pr_number": pr,
        "skipped": skipped_reason,
        "pr_changed_files": pr_changed_count, "graph_heal": graph_heal,
    }
    bounded_capability = (
        files_issue == "planned_page_budget"
        and planned_onboarding_graph_proof is _PLANNED_ONBOARDING_GRAPH_PROOF
    )
    if bounded_capability:
        # A confirmed fork often cannot receive a Check on its head through the base repository. Preserve this
        # only for the private bounded onboarding path; ordinary webhook results never gain cursor authority.
        result[_PR_SURFACE_OPERATION_KEY] = {
            "repo": repo, "pr_number": pr, "sha": head_sha,
            "check_posted": isinstance(check_meta, dict) and check_meta.get("posted") is True,
            "comment_posted": comment_ok is True,
        }
        # The value is the in-process capability itself. A JSON boolean/string/key cannot forge permission to
        # consume a skipped cursor; replay_onboarding_pull_request checks object identity plus an exact surface.
        result[_PLANNED_ONBOARDING_BOUNDED_UNREAD_KEY] = _PLANNED_ONBOARDING_GRAPH_PROOF
    return _attach_check_operation(result, repo, head_sha, check_meta)


def _pr_pre_brain(db, gh, f: dict, changed: list, changed_ranges: dict, added_paths: list | None = None) -> tuple:
    """PRE-BRAIN compute + lane-reconcile phase. Code-path filter + mega-PR cap, the freshness/base-hash read, the
    post-merge-staleness compare, the push↔PR lane reconcile, and the close-time branch-lane release. Returns
    (changed, changed_ranges, added_paths, truncated_files, base_hashes, branch_changed_paths). Side effects (the
    two lane releases) are byte-identical to the inline body; reads fail open exactly as before. _MAX_PR_FILES is
    read off _server() at call time so a test patch on `server._MAX_PR_FILES` is honoured.

    `added_paths` (PO 2026-06-25 honest-verdict refinement): the subset of `changed` whose Files-API status was
    `added`. Filtered to the SURVIVING (post-code-filter, post-cap) `changed` set so a non-code added file (a new
    README) doesn't reach the renderer's "new code" sentence, and a truncated-off added file doesn't either.
    Defaults to None → []  → renderer simply falls back to today's lumped Unknown copy."""
    (should_analyze, repo, pr, base, base_sha, default_branch, head_ref_name, prj, action) = (
        f["should_analyze"], f["repo"], f["pr"], f["base"], f["base_sha"],
        f["default_branch"], f["head_ref_name"], f["prj"], f["action"])
    redact_external = f.get("redact_external", f["is_fork"])
    _tp = _f_trace_prefix(f)
    # Coordinate only code-coupling paths: a README/image/lock touched alongside code must not drag the
    # whole PR to '❓ Not analyzed'. Filter BEFORE the mega-PR cap so the cap counts real code files only.
    changed = _code_paths(changed)
    truncated_files = False
    _max_pr_files = _server()._MAX_PR_FILES             # read off server at call time (honors the test patch)
    if len(changed) > _max_pr_files:                   # COST GUARD: bound claims for a mega-PR (see _MAX_PR_FILES)
        changed = changed[:_max_pr_files]
        truncated_files = True
    # keep ranges only for the surviving (code, non-truncated) paths (content-free line numbers).
    changed_ranges = {p: changed_ranges.get(p, []) for p in changed} if changed_ranges else {}
    # Intersect added_paths with the surviving changed set so we never advertise an added path the engine never
    # saw (filtered out as non-code, or dropped by the mega-PR cap) — keeps the renderer's split honest.
    _changed_set = set(changed)
    added_paths = [p for p in (added_paths or []) if isinstance(p, str) and p in _changed_set]

    # FRESHNESS KEY + STALE-BASE COMPARE — TWO INDEPENDENT GITHUB READS, RUN IN PARALLEL.
    # These two calls (base_blob_shas + compare_changed_paths) are I/O-bound, hit DIFFERENT GitHub endpoints
    # (git/trees vs compare), share NO state, and BOTH need to complete before the brain runs. Running them
    # serially on every PR analyze cost two round-trip latencies back-to-back (~200-500ms each = ~400-1000ms
    # wall time on the hot path). Running them concurrently overlaps the network wait under the GIL (urlopen
    # releases the GIL on the socket recv) → ~max(t1,t2) ≈ half the wall time on the customer-latency path.
    # SAFE / BEHAVIOR-PRESERVING:
    #   * Each call has the SAME guards / fail-open semantics / log strings it had inline (only the home moved).
    #   * No shared mutable state — each lambda writes to its own slot in a dict.
    #   * Errors are caught INSIDE each worker (never propagate across threads); the parent sees the same
    #     fall-back defaults ({} / []) on any failure, identical to the prior serial flow.
    #   * Threads are joined before we return, so there is no leak / no race with the brain that reads
    #     base_hashes / branch_changed_paths next.
    #   * The gh client is read-only here (both calls are GET); GitHubREST's installation token + retry/backoff
    #     are thread-safe for concurrent GETs (the cached token dict is set during _itoken; CPython dict get is
    #     atomic, and a concurrent remint at most makes one extra mint — never wrong / corrupting). The single
    #     drain worker still owns one event end-to-end; the parallelism is just within ONE event's read phase.
    # FREE-TIER GUARD: the parallel path can be disabled via VERIPSA_PR_PREBRAIN_PARALLEL=0 to fall back to the
    # original serial sequence (a kill switch for any deploy that wants the old shape, e.g. an in-process gh
    # mock that is not thread-safe; the test paths use a Fake client whose calls touch only local state).
    # FRESHNESS KEY: per-path git-blob-sha of each CHANGED file AT THE PR'S BASE, read from the base commit's
    # GIT TREE (path→blob-sha, NO bodies → content-free; see github_rest.base_blob_shas). The gate demotes a
    # file-level collision to the finer symbol verdict ONLY when this hash == the hash main's graph stored for
    # the file (spans provably valid for the diff lines); otherwise it keeps the file-level serialize (recall-
    # safe). Scoped to the SURVIVING changed paths so the read is bounded. Fail-open + degrade gracefully: an
    # older Fake client (no base_blob_shas), a missing base sha, or a transient fetch error → {} → no base
    # hashes → file-level fallback (NEVER a missed collision, NEVER a crashed event). A NEW file absent from
    # the base tree gets no entry → None → file-level fallback too (correct: no prior version to be fresh of).
    base_hashes = {}
    # POST-MERGE STALENESS (a PRE-CONFLICT nudge, advisory): Veripsa's collision detector compares CONCURRENTLY-
    # open PRs — it is BLIND to a change that ALREADY MERGED into the protected branch. So a file a PR is editing
    # that was touched by a landing AFTER the PR branched only surfaces as a conflict at rebase/merge time. We
    # ask GitHub which files changed on the protected branch SINCE this PR's base (compare base_sha...base) — the
    # render layer intersects that with the PR's own changed files and, on an overlap, appends a content-free
    # "main moved under you — rebase before merge" line to THIS PR's check summary. CONTENT-FREE (only path
    # strings cross — see github_rest.compare_changed_paths) and FAIL-OPEN: an older Fake client (no
    # compare_changed_paths), a missing base sha, or any fetch error → [] → no nudge (the verdict is unaffected;
    # this is purely additive). NON-fork only (the protected branch's other-landing paths are base-repo paths an
    # external fork contributor must not see — same guard the co-change / coverage-nudge lines use). Only on the
    # analyze actions (a closed event does not re-render a check). compare_changed_paths swallows its own errors
    # too, but the try/except here keeps the never-crash invariant even on an unexpected client shape.
    branch_changed_paths = []
    _want_base_hashes = bool(should_analyze and changed and base_sha and hasattr(gh, "base_blob_shas"))
    _want_branch_paths = bool(should_analyze and not redact_external and changed and base_sha
                              and hasattr(gh, "compare_changed_paths"))
    _parallel = os.environ.get("VERIPSA_PR_PREBRAIN_PARALLEL", "1") != "0"

    def _do_base_hashes():
        nonlocal base_hashes
        try:
            base_hashes = gh.base_blob_shas(repo, base_sha, changed) or {}
        except _event_budget.EventBudgetExceeded:
            raise
        except Exception as e:
            print(f"{_tp}base blob-sha read fell back to file-level repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
            base_hashes = {}

    def _do_branch_paths():
        nonlocal branch_changed_paths
        try:
            branch_changed_paths = gh.compare_changed_paths(repo, base_sha, base) or []
        except _event_budget.EventBudgetExceeded:
            raise
        except Exception as e:
            print(f"{_tp}stale-base compare skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
            branch_changed_paths = []

    if _parallel and _want_base_hashes and _want_branch_paths:
        # both reads needed — overlap them. Threads are daemon so a hard-crash never leaves them parked beyond
        # the worker's lifetime; the join() before we return ensures both results are visible to the brain.
        import threading as _threading
        _budget_hit = _threading.Event()

        def _run_in_context(ctx, fn):
            try:
                ctx.run(fn)
            except _event_budget.EventBudgetExceeded:
                _budget_hit.set()

        # ContextVars do not propagate to raw threads automatically. Copy the
        # delivery deadline into each read thread so parallelism cannot escape
        # the same absolute event budget.
        t1 = _threading.Thread(
            target=_run_in_context,
            args=(contextvars.copy_context(), _do_base_hashes),
            name="veripsa-pr-base-blob-shas", daemon=True,
        )
        t2 = _threading.Thread(
            target=_run_in_context,
            args=(contextvars.copy_context(), _do_branch_paths),
            name="veripsa-pr-compare-paths", daemon=True,
        )
        t1.start(); t2.start()
        # Bounded join: each read is itself bounded by the GH client's per-call total deadline
        # (VERIPSA_GITHUB_HTTP_TIMEOUT, default 30s) + retry budget (MAX_RETRIES). Use a generous join timeout
        # well above that ceiling so a wedged thread is detected (it will not actually happen — the GH client
        # raises socket.timeout on the deadline — but a belt-and-braces bound on join is harmless).
        t1.join(timeout=_event_budget.timeout_for(120.0))
        t2.join(timeout=_event_budget.timeout_for(120.0))
        if t1.is_alive() or t2.is_alive() or _budget_hit.is_set():
            raise _event_budget.EventBudgetExceeded(
                "parallel GitHub reads exceeded the webhook event wall-clock budget"
            )
    else:
        # SERIAL FALLBACK: kill-switch off, or only one read is needed (older client, fork, no base_sha, etc.)
        # — keep the original serial sequence so the byte-for-byte behaviour is preserved.
        if _want_base_hashes:
            _do_base_hashes()
        if _want_branch_paths:
            _do_branch_paths()

    # PUSH↔PR RECONCILIATION (no SELF-collision): the head branch already reserved its lanes at PUSH time
    # under change_id 'BR-<head>' (the push handler, before any PR). The PR now re-claims the SAME paths under
    # change_id 'PR-<n>'. Two different change_ids on the same lane would otherwise SELF-COLLIDE (the gate's
    # lane lock is author-agnostic: it sends a lane held by ANY other change_id — even the same author's — to
    # 'waiting'). So when the PR opens, RELEASE the branch's claims FIRST; handle_pull_request then re-declares
    # the same lanes as 'PR-<n>' on a now-free lane → granted, never queued behind itself. The lane stays
    # continuous push→PR. (If a genuinely-DIFFERENT change was waiting in line, releasing promotes it and the
    # PR correctly takes its place in line — that is real FIFO fairness, not a self-collision.) Idempotent: on
    # a later 'synchronize' the BR-claims are already released, so this is a no-op. Keyed by the HEAD branch
    # ref (head.ref); skipped when absent (a malformed/old payload) — the PR path still works, just no reconcile.
    # DRAFT PRs NOW reconcile too (PO 2026-06-25): a draft IS a scout window with full analysis, so its 'PR-<n>'
    # supersedes the branch's 'BR-<head>' immediately — same self-collision avoidance, and the zombie 'BR-<head>'
    # path the old "draft = skip" used to strand is gone at its root (no PR enters analysis still holding a
    # parallel BR claim). The close-time BR release below stays as belt-and-suspenders idempotency.
    if should_analyze and isinstance(head_ref_name, str) and head_ref_name:
        db("SELECT core.release_change_on_main_with_authority(%s,%s,%s)",
           (_branch_change_id(head_ref_name), repo, default_branch))

    # CLOSE-TIME BRANCH-LANE RELEASE (stale-lane leak belt-and-suspenders): a feature-branch push reserved its
    # lanes at PUSH time under change_id 'BR-<head>' (reserve_branch_lanes, before any PR). The push↔PR reconcile
    # above converts 'BR-<head>' → 'PR-<n>' on an analyze action; with full draft analysis the draft and non-draft
    # paths BOTH reconcile at open, so this close-time release is idempotent on the happy path. Kept as a defence
    # against PRs that never reached an analyze action (a payload that arrived past the analyze guards) so an
    # abandoned 'BR-<head>' still gets freed on close. IDEMPOTENT: releasing an already-released change_id frees
    # nothing — a clean no-op. Keyed by the HEAD branch ref (head.ref); skipped when absent. NEVER-CRASH: a
    # release error is logged, not raised.
    if action == "closed" and isinstance(head_ref_name, str) and head_ref_name:
        try:
            db("SELECT core.release_change_on_main_with_authority(%s,%s,%s)",
               (_branch_change_id(head_ref_name), repo, default_branch))
        except Exception as e:
            print(f"{_tp}close branch-lane release skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
    return changed, changed_ranges, added_paths, truncated_files, base_hashes, branch_changed_paths


def _pr_outcome_signals(db, f: dict) -> tuple:
    """ANSWER-CHECK (答え合わせ) — the content-free OUTCOME signals for the merge path. Returns
    (conflicted, outcome_conf, reverted). Only a merged `closed` event reads anything (else the defaults stand);
    the savepoint-isolated reads go through `_optional` (read as a module global so the txn-isolation test's
    rebind of `webhook_handlers._optional` is honoured). Lifted byte-identical."""
    action, merged, repo, pr, base, head_sha, prj, head = (
        f["action"], f["merged"], f["repo"], f["pr"], f["base"], f["head_sha"], f["prj"], f["head"])
    # ANSWER-CHECK (答え合わせ) — content-free OUTCOME signals for the merge path, so handle_pull_request can
    # grade Veripsa's advice against what actually happened. Every signal here is content-free (a boolean /
    # a change ref / a count — never a file body) and records-not-correctness (a FACT label, never a claim
    # we were right). Two facets:
    #   conflicted : did the change land BADLY? The honest, already-in-system content-free signal is the
    #                pr_failing ledger — when this PR's required check went RED (the App recorded a
    #                'pr_failing' fact via the check_suite/check_run path) it did NOT pass cleanly. That is an
    #                OBSERVED signal (a real red check), so confidence='observed'. Absent → not conflicted
    #                (we do not invent a conflict we cannot see — silence ≠ a bad outcome).
    #   reverted   : THIS PR is itself a revert (GitHub names a revert PR "Revert \"…\"" + sets the head ref
    #                'revert-<n>-…'). That is a content-free boolean about THIS change (its OWN merge undoes
    #                work) — recorded so the matrix can separate reverts from plain conflicts. (Linking the
    #                revert back to the EARLIER merge it undoes — to flip THAT change's recorded outcome to
    #                'reverted' — is a cross-PR update that would violate the append-only first-wins ledger;
    #                see the TODO in webhook.py. v1 records the revert fact on the reverting change only.)
    conflicted = False
    outcome_conf = "inferred"
    reverted = False
    if action == "closed" and merged:
        # SAVEPOINT-ISOLATED best-effort signal read: a failure must roll back ONLY itself, never abort the
        # merge-handling txn the landing record + the neighbor refresh (_post_refreshes) still need (a bare
        # try/except would leave the txn aborted → every later statement on the merge path skipped).
        # SHA-AWARE conflict read (#334 fix): scope the red fact to the PR's FINAL head — the commit that
        # actually merged — so a transient earlier RED (a flaky test / a typo the author then FIXED, recorded
        # as a 'pr_failing' fact the append-only ledger never retracts) no longer mis-grades a CLEAN merge as
        # land=conflicted (a false silent-miss that inflates the effect numbers). A PR genuinely merged WHILE
        # red (a red fact AT head_sha) is still caught — recall preserved. DEPLOY-SAFETY: the prod DB may not
        # carry the 4-arg overload until the PO-gated schema reapply, so a 'function does not exist' error must
        # NOT abort grading. Each read runs inside its OWN _optional savepoint (never raises, rolls back only
        # itself), so on a not-yet-migrated DB the 4-arg read returns its `default` sentinel and we DEGRADE to
        # the 3-arg (sha-blind) call — TODAY'S behavior, unchanged. Once the overload lands, the sha-scoped
        # grade takes effect. (Two separate _optional calls so the 4-arg failure's savepoint is fully rolled
        # back before the 3-arg fallback runs — the shared LIVE txn stays clean for it; never raises either way.)
        _MISS = object()                                      # sentinel distinct from a real False/None result
        fail_fact = _optional(
            db, "outcome conflict-signal (sha-aware)",
            lambda: db("SELECT core.change_failing(%s,%s,%s,%s)", (_change_id(pr), repo, base, head_sha)),
            default=_MISS, repo=repo, pr=pr)
        if fail_fact is _MISS:                                # 4-arg overload absent (or read failed) → sha-blind fallback (today's grade)
            fail_fact = _optional(
                db, "outcome conflict-signal",
                lambda: db("SELECT core.change_failing(%s,%s,%s)", (_change_id(pr), repo, base)),
                repo=repo, pr=pr)
        if fail_fact:                                   # a recorded red required-check / conflict fact for this change
            conflicted, outcome_conf = True, "observed"
        title = prj.get("title")                        # content-free: we read ONLY the leading 'Revert ' keyword + the revert- head-ref shape, never store the title
        head_ref_for_revert = head.get("ref") or ""
        reverted = (isinstance(title, str) and title.strip().lower().startswith("revert "
                    )) or (isinstance(head_ref_for_revert, str) and head_ref_for_revert.startswith("revert-"))
    return conflicted, outcome_conf, reverted


def _pr_build_event(f: dict, changed: list, changed_ranges: dict, base_hashes: dict,
                    branch_changed_paths: list, truncated_files: bool,
                    conflicted: bool, reverted: bool, outcome_conf: str,
                    added_paths: list | None = None,
                    conflict_markers: list | None = None) -> dict:
    """Assemble the IMPACT/BRAIN input `event` from the derived fields + the pre-brain compute. Pure (no I/O).

    `added_paths` (PO 2026-06-25 honest-verdict refinement): the SURVIVING (post-filter, post-cap) subset of
    `changed` whose Files-API status was `added`. Threaded onto the event so the renderer can split the lumped
    `unknown_paths` arm into "(a) new in this PR — expected" vs "(b) modified path NOT in main's graph — possible
    extractor gap". Default None → [] keeps callers (older tests) byte-identical.

    `conflict_markers` (PO 2026-06-25 dogfood hole-fix, PRs #111/#114): findings list
    [{"path", "line", "kind"}, …] of UNRESOLVED git conflict markers introduced by this PR's added lines.
    Threaded onto the event so the renderer escalates THIS PR to action_required (build-breaker, hard fail)
    with a top-of-comment block naming the path + line. Content-free (no surrounding text, only metadata).
    Default None → [] is behavior-preserving for clean PRs and existing call sites."""
    return {"action": f["brain_action"], "repo": f["repo"], "base_branch": f["base"], "pr_number": f["pr"],
            "changed_files": changed, "changed_ranges": changed_ranges, "base_hashes": base_hashes,
            # files that landed on the protected branch SINCE this PR's base — handle_pull_request intersects
            # them with this PR's changed files for the post-merge staleness nudge (content-free; [] = no nudge).
            "branch_changed_paths": branch_changed_paths,
            # ADDED-PATH STATUS: the subset of `changed_files` that are NEW IN THIS PR (Files-API status='added').
            # The renderer reads this to split the lumped `unknown_paths` Unknown copy. Content-free (path strings
            # only, the same coordinate class as changed_files). Empty → render falls back to today's lumped copy.
            "added_paths": list(added_paths or []),
            # CONFLICT-MARKER FINDINGS: findings of unresolved git conflict markers introduced by this PR (the
            # dogfood hole-fix). Path + new-side line + 3-value kind label only — content-free. Empty → no
            # escalation (behavior-preserving for clean PRs). The renderer hard-fails THIS PR (action_required)
            # with a top-of-comment block when non-empty: these are 100%-certain build-breakers.
            "conflict_markers": list(conflict_markers or []),
            "author": f["author"], "author_is_bot": f["author_is_bot"], "head_sha": f["head_sha"],
            "head_snapshot_verified": bool(f.get("head_snapshot_verified")),
            "land_sha": f["land_sha"],
            # SCOUT-WINDOW state (PO 2026-06-25): the PR's draft flag, threaded through to the gate so a
            # draft↔non-draft same-file overlap softens to 'warn' (never the hard 'serialize' that pauses a
            # non-draft behind a still-iterating scout). Content-free (a boolean only).
            "draft": f["is_draft"],
            # a base-retarget back ONTO main is analyzed like an open (it carries no `synchronize`) and
            # re-activates lanes — handle_pull_request reads this to enter its analyze path on `edited`.
            "base_retarget_onto_main": f["is_base_retarget"],
            "_veripsa_authoritative_pr_replay": f.get("authoritative_pr_replay", False),
            # FORK INFO-LEAK GUARD: thread the already-computed is_fork to the renderer (via handle_pull_request)
            # so the ACTING fork PR's own comment — posted on the base-repo conversation the external contributor
            # can read — is REDACTED of the base repo's other in-flight PR identities / paths. (Until now is_fork
            # only reached the check-post log line + _safe_upsert_check; it never reached render_pr_check.)
            # Privacy redaction is broader than a CONFIRMED fork: a synthetic replay whose fork identity is
            # unavailable must redact too, but it must NOT disable required-check / pause-ACK behavior.
            "is_fork": f.get("redact_external", f["is_fork"]),
            "merged": f["merged"], "model": None, "truncated_files": truncated_files,
            "conflicted": conflicted, "reverted": reverted, "outcome_confidence": outcome_conf}


def _graph_degraded(graph_heal) -> bool:
    """G1/G5 shared predicate for an unconfirmed main-graph coordinate.

    The live handler supplies the DB-only result from
    ``request_main_graph_refresh``: ``refresh queued`` means the exact signed
    coordinate is durable but not current yet; ``already current`` means no
    withhold is needed. Legacy/offline callers may still supply the historical
    self-heal shape, where ``healed=True`` likewise proves current. No second
    HEAD read is performed here.

    PURE (no I/O, no mutation). The SINGLE source of truth for BOTH G1 (`_pr_stale_graph_unknown_result` withholds
    a would-be clear over a degraded graph) and G5 (`apply_pause_ack` must NOT honor ACK stickiness on an EMPTY
    recompute over a degraded graph), so the two can never diverge on what 'degraded' means."""
    if not isinstance(graph_heal, dict):
        return False
    if graph_heal.get("head_sha") is None:                 # behind is None (HEAD unresolvable) → not degraded
        return False
    if graph_heal.get("healed") is True or graph_heal.get("reason") == "already current":
        return False                                       # behind is False / graph brought current → not degraded
    return True


def _graph_head_unresolvable(graph_heal) -> bool:
    """Compatibility predicate for legacy/offline self-heal callers.

    Production live PR handling queues a signed base coordinate and never reads
    unversioned HEAD here. Older injected callers can still report the exact
    ``main HEAD unresolvable`` shape; retain its honest-Unknown behavior.
    """
    try:
        return (isinstance(graph_heal, dict)
                and graph_heal.get("head_sha") is None
                and graph_heal.get("reason") == "main HEAD unresolvable")
    except Exception:
        return False


def _pr_quota_paused_result(gh, f: dict, result: dict, graph_heal) -> dict | None:
    """THE SILENT-FAIR-USE-WALL FIX. When self_heal_main_graph hit the free-tier wall (graph_heal.reason ==
    'free-tier limit'), post the advisory early-access-limit check + comment IN PLACE of the
    now-stale verdict and return `result` to short-circuit; otherwise return None to continue. Lifted byte-
    equivalent for ordinary events (NEVER-CRASH there); its private Check outcome lets strict requested replay
    turn a failed exact-target post into an outer retry."""
    repo, pr, head_sha, is_fork, default_branch, should_analyze = (
        f["repo"], f["pr"], f["head_sha"], f["is_fork"], f["default_branch"], f["should_analyze"])
    _tp = _f_trace_prefix(f)
    # THE SILENT-PAYWALL FIX. When an account crosses the free line the DB gate refuses further writes and the
    # graph silently goes stale — the customer sees the bot quietly stop, which reads as 'broke'. self_heal_main_
    # graph already detected the wall (it tried to re-ingest main and got the quota_exceeded sentinel back →
    # reason='free-tier limit'). Turn that SILENT break into a VISIBLE fair-use event: post/patch an advisory,
    # content-free, jargon-free early-access-limit check + comment ON THIS PR, IN PLACE of the
    # now-stale verdict (the verdict can't be trusted — the graph couldn't update). ADVISORY (`neutral` — never a
    # blocking/failing conclusion). IDEMPOTENT: the same upsert the normal verdict uses PATCHES in place (no
    # flap on every synchronize). NEVER-CRASH: a post failure must not abort the event.
    quota_paused = isinstance(graph_heal, dict) and graph_heal.get("reason") == "free-tier limit"
    if quota_paused and should_analyze:
        qchk = quota_paused_check()
        # PRE-POST THE COMMENT (PO #3 details_url) so the check's "Details" link anchors at the verdict comment.
        comment_id = None
        comment_ok = False
        try:
            resp = gh.upsert_comment(repo, pr, _comment_marker(pr),
                                     _marked_comment(pr, quota_paused_comment_body(default_branch)))
            comment_ok = True
            if isinstance(resp, dict):
                cid = resp.get("id")
                if isinstance(cid, int):
                    comment_id = cid
        except Exception as e:
            print(f"{_tp}quota-paused note skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
        check_meta = _upsert_check_result(gh, repo, head_sha, qchk["conclusion"], qchk["title"], qchk["summary"],
                                          is_fork, pr_number=pr, comment_id=comment_id)
        _log_pr_surface(
            _tp, surface="quota-paused", delivery=f.get("delivery", ""), action="quota-paused", repo=repo,
            pr_number=pr, head_sha=head_sha, comment_needed=True, comment_ok=comment_ok, comment_id=comment_id,
            check_meta=check_meta)
        result["quota_paused"] = True
        _attach_check_operation(result, repo, head_sha, check_meta)
        print(f"{_tp}pull_request {repo} pr={pr} → EARLY-ACCESS LIMIT reached — posted advisory fair-use note (graph is stale, verdict withheld)", flush=True)
        return result
    return None


def _pr_stale_graph_unknown_result(gh, f: dict, result: dict, graph_heal) -> dict | None:
    """Withhold a would-be Clear until durable graph convergence is current.

    Live structural events return the durable enqueue shape; legacy callers may
    return a self-heal/head-unresolvable shape. Only ``success`` is downgraded:
    material findings remain visible. The background worker re-renders after
    exact-HEAD graph commit.
    """
    repo, pr, head_sha, is_fork, default_branch, should_analyze = (
        f["repo"], f["pr"], f["head_sha"], f["is_fork"], f["default_branch"], f["should_analyze"])
    _tp = _f_trace_prefix(f)
    if not should_analyze:
        return None
    # WOULD-BE-CLEAR ONLY: a `success` (clear) is the only verdict we replace. Any `neutral` (material heads-up /
    # wait-in-line / unknown / an already-withheld advisory) flows through untouched — never suppress a real signal.
    check = result.get("check") if isinstance(result, dict) else None
    if not isinstance(check, dict) or check.get("conclusion") != "success":
        return None
    # UNCONFIRMABLE-CURRENCY over a would-be clear, in TWO clearly-scoped, mutually-exclusive states:
    #   (1) BEHIND-AND-NOT-CURRENT (the shipped G1 path, UNCHANGED) — the stored graph is BEHIND a RESOLVABLE
    #       HEAD and self-heal did not close the gap. Derived from the self-heal outcome without a second HEAD
    #       read via the SHARED `_graph_degraded` predicate (the SAME one G5's pause-ack overlay uses, so the two
    #       never diverge). A non-dict / missing graph_heal, a healed-to-current graph, or an already-current
    #       graph (behind is False) are NOT degraded.
    #   (2) HEAD-UNRESOLVABLE (the G1 TAIL, this branch) — the state `_graph_degraded` DELIBERATELY excludes: main
    #       HEAD could not be READ at all (a repo_default_branch_head API failure → self_heal reason 'main HEAD
    #       unresolvable', head_sha None, behind None). We cannot compare the stored sha to an unknown HEAD, so
    #       currency is UNCONFIRMABLE — a would-be clear over it is just as untrustworthy as (1). Kept SEPARATE
    #       from `_graph_degraded` so the shipped behind-and-not-current meaning of 'degraded' (and G5's) is
    #       untouched; this only ADDS the head-unresolvable withhold. Honest, distinct wording ("currency not
    #       confirmed" — we could not read HEAD, we do NOT claim it is behind).
    # Both are would-be-CLEAR only (gated above) and both withhold to an advisory `neutral` — NEVER a collision,
    # an ACK strip, or a non-success verdict (recall-safe / monotonic).
    degraded = _graph_degraded(graph_heal)
    head_unresolvable = (not degraded) and _graph_head_unresolvable(graph_heal)
    if not (degraded or head_unresolvable):
        return None
    if degraded:
        schk = stale_graph_unknown_check()
        body = stale_graph_unknown_comment_body(default_branch)
        surface = "stale-graph-unknown"
        log_reason = "STALE GRAPH (background convergence pending)"
        result_flag = "stale_graph_unknown"
    else:
        schk = stale_graph_head_unknown_check()
        body = stale_graph_head_unknown_comment_body(default_branch)
        surface = "stale-graph-head-unknown"
        log_reason = "HEAD UNRESOLVABLE (main HEAD could not be read — currency unconfirmable)"
        result_flag = "stale_graph_head_unknown"
    # PRE-POST THE COMMENT (details_url) so the check's "Details" link anchors at the verdict comment.
    comment_id = None
    comment_ok = False
    try:
        resp = gh.upsert_comment(repo, pr, _comment_marker(pr), _marked_comment(pr, body))
        comment_ok = True
        if isinstance(resp, dict):
            cid = resp.get("id")
            if isinstance(cid, int):
                comment_id = cid
    except Exception as e:
        print(f"{_tp}{surface} note skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
    check_meta = _upsert_check_result(gh, repo, head_sha, schk["conclusion"], schk["title"], schk["summary"],
                                      is_fork, pr_number=pr, comment_id=comment_id)
    _log_pr_surface(
        _tp, surface=surface, delivery=f.get("delivery", ""), action=surface,
        repo=repo, pr_number=pr, head_sha=head_sha, comment_needed=True, comment_ok=comment_ok,
        comment_id=comment_id, check_meta=check_meta)
    result[result_flag] = True
    _attach_check_operation(result, repo, head_sha, check_meta)
    print(f"{_tp}pull_request {repo} pr={pr} → {log_reason} — "
          "withheld would-be clear, posted advisory 'graph not confirmed current' (verdict treated as unknown)",
          flush=True)
    return result


def _pr_apply_pause_ack_overlay(db, gh, f: dict, result: dict) -> None:
    """PAUSE-ACK (一時停止) OVERLAY — the tier that actually changes behavior. Mutates `result` in place when the
    brain's check is `neutral` (a material verdict) and apply_pause_ack flips it to action_required / acked /
    stale. `_optional` + `apply_pause_ack` are read as MODULE GLOBALS so the pause-ack tests' rebinds of
    `webhook_handlers._optional` / `webhook_handlers.apply_pause_ack` are honoured. Lifted byte-identical;
    FAIL-OPEN (any error leaves the brain's advisory verdict exactly as it was)."""
    repo, pr, base, is_fork, action, prj, is_ack_label_event = (
        f["repo"], f["pr"], f["base"], f["is_fork"], f["action"], f["prj"], f["is_ack_label_event"])
    _tp = _f_trace_prefix(f)
    _trace_id = f.get("trace_id", "")
    # PAUSE-ACK (一時停止) OVERLAY — the tier that actually changes behavior. For a MATERIAL coupling (a REAL
    # in-flight cross-PR collision) the check is NOT green until someone EXPLICITLY acknowledges THIS specific
    # coupling (the `veripsa-ack` label). It is NOT a blunt block: the author/agent proceeds by acknowledging
    # (a conscious, RECORDED stop-and-engage), then the check clears. STATELESS — the ack state is the GitHub
    # LABEL + the content-free coupling-snapshot HASH embedded in Veripsa's OWN prior comment (no DB, no DDL).
    #
    # We overlay ONLY when the brain produced a check whose conclusion is `neutral` (every material verdict —
    # serialize / serialize_soft / warn — renders `neutral`; clear=success and the paused/quota paths returned
    # already). So a clean/clear PR pays NO extra read. apply_pause_ack itself returns 'not_material' (a no-op
    # overlay) for a warn with no in-flight partner or a solo hotspot/split notice — precision: we never pause
    # someone who is not actually coupled. main_impact_surface is the SAME surface the brain just computed (the
    # snapshot needs the partner refs + colliding paths it carries); we re-read it here rather than thread it
    # out of the brain (webhook.handle_pull_request is out of this module's lane). FAIL-OPEN: any error leaves
    # the brain's advisory verdict exactly as it was (never worse than before the tier).
    if "check" in result and result["check"].get("conclusion") == "neutral":
        try:
            # SAVEPOINT-ISOLATE the overlay's OWN brain read: this is the PRODUCT OUTPUT (it decides whether
            # the check is action_required), and an earlier optional surface (co-change / coverage nudge /
            # prediction telemetry) is now savepoint-contained so the txn is CLEAN here — but isolate this
            # read too so the overlay can never be the surface that leaves the txn aborted for what follows.
            filtered_impact = result.get("_branch_filtered_impact")
            # Never fall back to the actual DB surface while BR authority is uncertain: it still contains the
            # unverified participant and would turn the neutral retry surface straight back into a ghost-derived
            # pause. A safe speculative view is used when available; otherwise {} lets the ACK-preservation path
            # retain an existing marker without treating the BR as material. Conflict-marker hard findings bypass
            # this neutral-only overlay and therefore remain action_required.
            if result.get("branch_inventory_unknown"):
                impact = filtered_impact if isinstance(filtered_impact, dict) else {}
            else:
                impact = _optional(
                    db, "pause-ack impact read",
                    lambda: db("SELECT core.main_impact_surface(%s,%s)", (repo, base)),
                    default={}, repo=repo, pr=pr, trace_id=_trace_id)
            if isinstance(impact, str):
                impact = json.loads(impact)
            impact = impact if isinstance(impact, dict) else {}
            # AUTHORITATIVE LABEL READ: determine `label_present`
            # from the CURRENT label set via a SINGLE gh.pr_labels API call — NOT from `pull_request.labels[]`.
            # The per-event payload labels VARY by event type and silently lose the ack: an OLD push's
            # synchronize (fired before the label was added) carries no ack label; a check_suite/check_run
            # rerequested REPLAY carries only whatever _rerun_replay threaded through; only the `labeled` event
            # is guaranteed to carry it. ANY of those reading label_present=False re-raised action_required on an
            # ALREADY-ACKED PR (the ack did not stick). gh.pr_labels(strict=True) = GET pulls/{n} → labels[] = the
            # PR's CURRENT labels (the PULLS surface — the App carries no `issues` permission, and the earlier
            # issues-scoped reads can be unavailable under least privilege), identical
            # regardless of which event fired, so a rerun/sync/labeled event ALL
            # see the same true state. We only reach here when a pause decision is actually being made (the
            # overlay fires only on a `neutral` material verdict), so a clear/non-material PR pays NO extra call.
            # FAIL-SAFE (keeps #303's principle): strict=True RAISES on an unreadable label set → the outer
            # except FAIL-OPENs (the brain's plain advisory `neutral` stands) rather than masking it as "no
            # label" and re-raising a pause on a valid ack — a transient API error must never undo an ack; the
            # next real event re-derives the state. (Older fakes/clients without pr_labels degrade to the payload
            # read — no worse than before.)
            if hasattr(gh, "pr_labels"):
                label_present = ACK_LABEL in gh.pr_labels(repo, pr, strict=True)
            else:
                label_present = _ack_label_present(prj)
            prior_hash, prior_confirmed = _prior_ack_snapshot(gh, repo, pr) if label_present else (None, True)
            # Keep the historical monkeypatch/call seam: existing injected implementations do not know the new
            # optional keyword. Pass it only on the new uncertainty path; ordinary calls remain signature-stable.
            uncertainty_kwargs = ({"preserve_ack_on_uncertainty": True}
                                  if result.get("branch_inventory_unknown")
                                  and _accepts_keyword(apply_pause_ack, "preserve_ack_on_uncertainty") else {})
            # G5: thread the SHARED degraded-graph signal (the SAME `_graph_degraded` predicate G1 uses) so an
            # EMPTY coupling recompute over a BEHIND-and-un-healed main graph is NOT cleared via ack stickiness —
            # it surfaces the honest `ack_unconfirmable_degraded` state instead (see apply_pause_ack). graph_heal
            # was surfaced onto `result` before this overlay ran (result["graph_heal"]); a healthy / absent heal
            # → not degraded → default False → the exact keep-ack behavior.
            # Signature-stable seam (like the uncertainty kwarg): pass it only when degraded AND the bound
            # apply_pause_ack accepts it, so a monkeypatched/legacy fake without the keyword is never handed it.
            degraded_kwargs = ({"graph_degraded": True}
                               if _graph_degraded(result.get("graph_heal"))
                               and _accepts_keyword(apply_pause_ack, "graph_degraded") else {})
            overlay = apply_pause_ack(
                {"conclusion": result["check"]["conclusion"], "title": result["check"]["title"],
                 "summary": result["check"]["summary"], "comment": result.get("comment")},
                impact, _change_id(pr), label_present=label_present, prior_hash=prior_hash,
                branch=base, is_fork=is_fork, prior_confirmed=prior_confirmed,
                **uncertainty_kwargs, **degraded_kwargs)
            # PAUSE-ACK DECISION LOG — content-free, always-on (hashes/booleans/enums/PR-number only, NEVER a
            # body). The SINGLE observable record of WHY a check paused/acked/stale-stripped on the ACTING path,
            # printing the EXACT inputs the recognition turns on (label_present / prior_hash / prior_confirmed /
            # current_snap) so a live mis-recognition is diagnosable from the logs (not re-guessed off-line).
            print(f"{_tp}pause-ack decide repo={repo} pr={pr} path=acting action={action} "
                  f"label_present={label_present} prior_hash={prior_hash} prior_confirmed={prior_confirmed} "
                  f"current_snap={overlay.get('snapshot')} ack_state={overlay.get('ack_state')} "
                  f"label_action={overlay.get('label_action')}", flush=True)
            if overlay.get("ack_state") != "not_material":
                result["check"]["conclusion"] = overlay["conclusion"]
                result["check"]["title"] = overlay["title"]
                result["comment"] = overlay["comment"]
                result["ack_state"] = overlay["ack_state"]
                result["coupling_snapshot"] = overlay["snapshot"]
                # STALE ACK: the label was on but acked for a DIFFERENT coupling → take it off so the PR reads
                # un-acked (the comment tells the author to re-add it). GUARDED so it can NEVER loop: we remove
                # ONLY when the label is present AND stale (the unlabeled event that GitHub fires back is a
                # clean no-op — not an ack label, or the recomputed state is no longer stale). remove_label is
                # idempotent (404-on-absent → no-op). Best-effort: a removal error must not abort the event.
                if overlay.get("label_action") == "remove" and hasattr(gh, "remove_label"):
                    try:
                        gh.remove_label(repo, pr, ACK_LABEL)
                    except Exception as e:
                        print(f"{_tp}stale-ack label removal skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
                print(f"{_tp}pull_request {repo} pr={pr} → pause-ack {overlay['ack_state']} "
                      f"(conclusion={overlay['conclusion']}, snap={overlay['snapshot']})", flush=True)
        except Exception as e:                          # FAIL-OPEN: the advisory verdict stands unchanged
            print(f"{_tp}pause-ack overlay skipped repo={repo} pr={pr}: {str(e)[:140]}", flush=True)


def _pr_post_check_and_comment(gh, f: dict, result: dict) -> dict | None:
    """POST the (possibly pause-ack-overlaid) check + comment to GitHub, and the self-downgrade-to-clear comment
    rewrite. The upserts are idempotent (PATCH in place → no comment spam).

    ORDERING (PO #3 details_url, 2026-06-25): post the COMMENT FIRST so the check's "Details" link can
    anchor at the comment id (`#issuecomment-<id>`) — landing the reader on the Veripsa verdict prose with
    the reasoning visible, not on the bare commit. A comment-post failure is non-fatal; the check still posts
    (with the PR-conversation fallback, then commit). Idempotent: a repeated event re-finds the same comment
    id (upsert_comment patches in place) so the check's details_url stays stable across re-deliveries. Returns
    the private Check-post outcome for an in-process exact-target replay caller, or None when no Check existed."""
    repo, pr, head_sha, is_fork, action, default_branch, is_ack_label_event = (
        f["repo"], f["pr"], f["head_sha"], f["is_fork"], f["action"], f["default_branch"], f["is_ack_label_event"])
    _tp = _f_trace_prefix(f)
    check_meta = None
    if "check" in result:                              # opened/synchronize → post check + comment
        chk = result["check"]
        # PRE-POST THE COMMENT (when there is one) so we can anchor the check's Details link at it. Best-effort:
        # a comment-post failure must never abort the check post — degrade to PR-URL / commit-URL fallback.
        comment_id = None
        comment_ok = False
        if result.get("comment"):
            try:
                resp = gh.upsert_comment(repo, pr, _comment_marker(pr),
                                         _marked_comment(pr, result["comment"]))
                comment_ok = True
                if isinstance(resp, dict):
                    cid = resp.get("id")
                    if isinstance(cid, int):
                        comment_id = cid
            except Exception as e:                     # comment failed → check still posts (PR fallback)
                print(f"{_tp}verdict comment skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
        check_meta = _upsert_check_result(gh, repo, head_sha, chk["conclusion"], chk["title"], chk["summary"],
                                          is_fork, pr_number=pr, comment_id=comment_id)
        # Private typed receipt for strict in-process replays.  A confirmed fork often cannot receive a Check
        # on its head SHA through the base repository, so cursor advancement requires this exact PR-comment
        # proof instead.  Ordinary webhook posting remains fail-soft; only the replay caller interprets it.
        result[_PR_SURFACE_OPERATION_KEY] = {
            "repo": repo,
            "pr_number": pr,
            "sha": head_sha,
            "check_posted": isinstance(check_meta, dict) and check_meta.get("posted") is True,
            "comment_posted": comment_ok is True,
        }
        _log_pr_surface(
            _tp, surface="acting", delivery=f.get("delivery", ""), action=action, repo=repo, pr_number=pr,
            head_sha=head_sha, comment_needed=bool(result.get("comment")), comment_ok=comment_ok,
            comment_id=comment_id, check_meta=check_meta)
        if not result.get("comment") and not result.get("branch_inventory_unknown") and (
                action in ("synchronize", "ready_for_review", "reopened", "converted_to_draft") or is_ack_label_event):
            # SELF-DOWNGRADE-TO-CLEAR: the ACTING PR re-scoped itself down to 'clear' (it dropped the file it
            # was waiting on, or narrowed out of a coupling). Its check is already reset to green above, but a
            # stale "Wait in line" / "Heads up" comment it left from a PREVIOUS verdict would otherwise linger
            # on its own conversation. Rewrite it to the cleared body — but only if it exists (an opened PR
            # that is clean from the start never had one → no new comment, no spam).
            try:
                gh.patch_comment_if_exists(
                    repo, pr, _comment_marker(pr),
                    lambda existing="": _marked_comment(
                        pr, _cleared_comment_preserving_ack(existing, default_branch)))
            except Exception as e:
                print(f"{_tp}self clear-reset skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
    return check_meta


def _pr_record_landing(db, gh, f: dict) -> None:
    """UNIFIED LANDING MODEL: on a merged `closed`, record the PR's changed (code-only) files as 'landed' events
    so collisions_on_main captures real same-path collisions from PR-merges. Lifted byte-identical; FAIL-OPEN
    (A4-F3: telemetry must never roll back the already-committed merge handling)."""
    action, merged, repo, pr, base, merge_commit_sha, author, author_is_bot = (
        f["action"], f["merged"], f["repo"], f["pr"], f["base"], f["merge_commit_sha"],
        f["author"], f["author_is_bot"])
    _tp = _f_trace_prefix(f)
    # UNIFIED LANDING MODEL: when a PR merges, record its changed files as 'landed' events so
    # collisions_on_main captures real same-path collisions from PR-merges (not only direct pushes).
    #
    # A4-F3 (fail-open): the landing record is TELEMETRY — a transient GitHub error (rate limit /
    # timeout / perm revoked post-merge) or a transient DB error must NEVER propagate up and roll back
    # the lane-release + push record already committed in this event. The WHOLE block is wrapped in
    # try/except so any failure — including the files fetch — is logged and swallowed: the merge is
    # already done, the ledger catches up on the next event.
    #
    # A4-F6 (no ledger pollution): the old code used a raw `isinstance(p, str)` filter on the
    # list_pr_files response, so docs / lock files (README.md, package-lock.json) entered the
    # collisions_on_main ledger as 'landed' paths — polluting it with non-code paths that can never
    # carry coupling-level collisions. The fix applies _code_paths (the SAME filter the rest of the
    # pipeline uses) so only code files reach the landing record. The `closed` action is NOT in
    # `analyze_actions` so `changed` is always [] at this point; a fresh files fetch is correct here
    # (the closed event does not re-predict, so the fetch was always deferred to this site) — the
    # fix keeps that fetch but moves it INSIDE the fail-open try/except and applies _code_paths.
    if action == "closed" and merged:
        merge_sha = merge_commit_sha                       # the SAME real on-main commit land_change recorded the push for
        try:
            merge_paths = _code_paths(gh.list_pr_files(repo, pr))  # A4-F6: _code_paths filter (not just isinstance str)
            if merge_paths and merge_sha:
                db("SELECT core.record_landing_with_authority(%s,%s,%s,%s,%s,%s)",
                   (repo, base, merge_sha, merge_paths, author, author_is_bot))
        except Exception as e:                             # A4-F3: fail-open — telemetry must not roll back the merge handling
            print(f"{_tp}landing record skipped repo={repo} pr={pr}: {str(e)[:160]}", flush=True)


def _handle_pull_request_event(
        event_type: str, payload: dict, db, gh, *,
        planned_onboarding_graph_proof=None) -> dict:
    """PULL_REQUEST tier (the largest) — now a THIN ORCHESTRATOR over cohesive PR sub-phase helpers, each lifted
    VERBATIM so behaviour is unchanged (the gates + the pause-ack tests prove it). Reached only after _pr_guard
    returned None; threads the same (event_type, payload, db, gh) the dispatcher had (coalesce is unused here).

    The phases, in order:
      1. _pr_eligibility        — derive the scalar field bundle `f` + the three early-exit guards
      2. _pr_fetch_changed      — fetch changed files (+ ranges) and detect a pagination shortfall
         _pr_unread_files_result— the empty/shortfall honest-unknown advisory (short-circuit)
      3. request_main_graph_refresh — DB-only durable enqueue for structural work
      4. _pr_pre_brain          — code-path filter + cap, base-hash/staleness reads, push↔PR + close lane releases
      5. _pr_outcome_signals    — the answer-check (conflicted/reverted) for the merge path
         _pr_build_event        — assemble the brain input → handle_pull_request (the IMPACT/BRAIN compute)
      6. _pr_quota_paused_result— the early-access fair-use advisory (short-circuit)
         _pr_apply_pause_ack_overlay — the pause-ack tier (mutates result in place)
         _pr_post_check_and_comment  — post the check + comment / self-clear-reset
      then the neighbor refresh + _pr_record_landing (the merge-time landing telemetry).

    The pause-ack / txn-isolation / landing tests rebind `webhook_handlers.apply_pause_ack` /
    `webhook_handlers._optional` and read `_server()._MAX_PR_FILES`; every helper above is in THIS module and calls
    those names as MODULE GLOBALS, so the rebinds keep taking effect (same binding contract as the #408 split)."""
    short_circuit, f = _pr_eligibility(payload, db)
    if short_circuit is not None:
        return short_circuit

    # Read and prove the PR's exact changed-file snapshot BEFORE deciding whether a main-graph refresh is needed.
    # A docs/assets-only PR has no structural input at all. Running a whole-repository self-heal first made a tiny
    # check_suite.requested canary inherit an unrelated extractor migration: on the starter CPU the child spent the
    # delivery's complete 85-second work allowance, durable recovery spent the remaining shared retry window, and
    # the exact Check was never posted. The changed-file read is already mandatory below, so moving it ahead costs
    # no extra GitHub call and lets the hot path prove that zero code paths means zero graph dependency.
    graph_heal = None
    planned_onboarding = (
        planned_onboarding_graph_proof is _PLANNED_ONBOARDING_GRAPH_PROOF
    )
    (changed, changed_ranges, added_paths, conflict_markers, files_issue,
     pr_changed_count, raw_entry_count) = _pr_fetch_changed(
         gh, f, planned_onboarding=planned_onboarding)
    # EMPTY-FILES-API WRONG-CLEAR GUARD + PR-FILES PAGINATION SHORTFALL (audited silent-miss, recall-safe): a
    # RAW (pre-_code_paths) empty list while the PR object reports changed_files > 0, OR a partial-page shortfall,
    # is a PROVABLE read failure → HONEST-UNKNOWN (do NOT analyze, post an advisory `neutral`, release no lanes).
    # A docs-only PR returns a NON-empty raw list (README.md etc.) → guard does NOT fire → stays honest `success`;
    # a real 0-file PR reports changed_files==0 → guard off too. Content-free (a count + a path-less advisory).
    raw_changed_empty = f["should_analyze"] and not changed
    suspect_empty_files = (raw_changed_empty and pr_changed_count > 0) or files_issue is not None
    if suspect_empty_files:
        return _pr_unread_files_result(
            gh, f, pr_changed_count, files_issue, graph_heal,
            planned_onboarding_graph_proof=planned_onboarding_graph_proof)

    if f["should_analyze"]:
        snapshot_current = _pr_files_snapshot_is_current(gh, f, pr_changed_count, raw_entry_count)
        if snapshot_current is False:
            _tp = _f_trace_prefix(f)
            print(f"{_tp}pull_request {f['repo']} pr={f['pr']} changed during Files read — skipped before lane mutation",
                  flush=True)
            return {"event": "pull_request", "action": f["action"], "repo": f["repo"],
                    "pr_number": f["pr"], "noop": True,
                    "skipped": "PR changed during Files read; current event will reconcile"}
        f["head_snapshot_verified"] = snapshot_current is True

    # REQUEST GRAPH CONVERGENCE only when this exact, authoritatively-proven PR snapshot contains structural input.
    # `_code_paths` is the same filter `_pr_pre_brain` applies below, so this preflight cannot skip a path the brain
    # would consume. Docs/assets-only work proceeds directly to the exact Check. Code/config/schema work performs
    # only a DB-local freshness comparison and durable enqueue here; clone/extract belongs exclusively to the
    # background convergence worker. A PR's base SHA is an analysis coordinate, not proof that the default ref has
    # not advanced since the event. Therefore structural events always force the latest-wins durable row even when
    # the DB graph happens to match that base SHA. The background worker resolves authoritative current HEAD and
    # either converges or supersedes this target; meanwhile the queued graph_heal shape withholds a would-be clear
    # as Unknown without holding this webhook worker for repository-sized work.
    if f["should_analyze"] and _code_paths(changed):
        # A structural PR may proceed only after the exact protected-branch coordinate is either proven current
        # or durably queued. Missing authority is therefore a delivery failure, never an implicit healthy graph.
        if planned_onboarding_graph_proof is _PLANNED_ONBOARDING_GRAPH_PROOF:
            # Only replay_onboarding_pull_request can supply this process-local object after it matched the current
            # PR's base ref/repository/SHA to the exact frozen graph request under the held lease.  Suppress the
            # generic wake: returning its "refresh wake recorded" shape would incorrectly downgrade a Clear result
            # to Unknown even though this replay already has stronger graph authority.  JSON/persisted payloads can
            # never manufacture object identity, so ordinary webhook delivery cannot enter this branch.
            graph_heal = {
                "healed": False, "queued": False, "reason": "already current",
                "stored_sha": f.get("base_sha"), "head_sha": f.get("base_sha"),
            }
        else:
            graph_heal = request_main_graph_refresh_wake_only(
                db, f["repo"], f["default_branch"], f.get("base_sha"), f.get("repository_id"),
                trace_id=f.get("trace_id", ""))

    changed, changed_ranges, added_paths, truncated_files, base_hashes, branch_changed_paths = _pr_pre_brain(
        db, gh, f, changed, changed_ranges, added_paths)
    conflicted, outcome_conf, reverted = _pr_outcome_signals(db, f)

    event = _pr_build_event(f, changed, changed_ranges, base_hashes, branch_changed_paths,
                            truncated_files, conflicted, reverted, outcome_conf,
                            added_paths=added_paths,
                            conflict_markers=conflict_markers)
    # Release-by-difference must never inherit the generic eligibility fallback that guesses ``main`` for a
    # malformed legacy payload. Only the signed repository object's concrete default branch authorizes this
    # optional reconcile; otherwise the callback raises into the brain's neutral/automatic-retry path and every
    # BR lane is preserved (never a manual ACK prompt on uncertain branch truth).
    payload_default_branch = _as_obj(payload.get("repository")).get("default_branch")
    synthetic_default_branch_authority = payload.get("_veripsa_default_branch_authoritative", None)

    def reconcile_branch_claims():
        if (("_veripsa_default_branch_authoritative" in payload
             and synthetic_default_branch_authority is not True)
                or not isinstance(payload_default_branch, str) or not payload_default_branch
                or payload_default_branch != f["default_branch"]):
            raise RuntimeError("authoritative default-branch metadata unavailable")
        return _reconcile_live_branch_claims(db, gh, f["repo"], payload_default_branch)
    if _accepts_keyword(handle_pull_request, "reconcile_branch_claims"):
        brain_kwargs = {"reconcile_branch_claims": reconcile_branch_claims}
        if _accepts_keyword(handle_pull_request, "gh"):
            # compat shadow analysis (compat lane PR-3): the brain reads shared files at verified PR heads
            # ONLY when VERIPSA_COMPAT_ANALYSIS + its repo allowlist say so — inert (never touched) otherwise.
            brain_kwargs["gh"] = gh
        result = handle_pull_request(db, event, f["author"], act_for=True, **brain_kwargs)
    else:  # compatibility for narrow injected/offline handlers that expose the historical signature
        result = handle_pull_request(db, event, f["author"], act_for=True)
    if graph_heal is not None:                     # surface convergence state in logs/result
        result["graph_heal"] = graph_heal

    quota_short_circuit = _pr_quota_paused_result(gh, f, result, graph_heal)
    if quota_short_circuit is not None:
        quota_short_circuit.pop("_branch_filtered_impact", None)
        quota_short_circuit.pop("_branch_unknown_changes", None)
        return quota_short_circuit

    # G1 STALE-GRAPH WITHHELD-CLEAR: structural work remains Unknown
    # while its exact durable graph request is pending. Ordered after the quota
    # path so the fair-use wall keeps its more specific note. Only a success is
    # downgraded; material findings remain visible.
    stale_short_circuit = _pr_stale_graph_unknown_result(gh, f, result, graph_heal)
    if stale_short_circuit is not None:
        stale_short_circuit.pop("_branch_filtered_impact", None)
        stale_short_circuit.pop("_branch_unknown_changes", None)
        return stale_short_circuit

    _pr_apply_pause_ack_overlay(db, gh, f, result)
    check_meta = _pr_post_check_and_comment(gh, f, result)
    _attach_check_operation(result, f["repo"], f["head_sha"], check_meta)

    # A4 ACTIVATION (Issue #648): the acting PR's Veripsa Check was PUBLISHED — record the content-free first-check
    # fact (idempotent on head sha; the surface takes the earliest per repo). ONLY after a confirmed post
    # (check_meta.posted is True), append-only + ledger-wall guarded + ON CONFLICT DO NOTHING, wrapped fail-soft so
    # a DB error can NEVER affect the verdict, the check, or webhook routing. The signal is derived from the FINAL
    # (post-overlay) conclusion + the raw analysis verdict token + whether a pause-ack overlay paused this PR.
    if db is not None and isinstance(check_meta, dict) and check_meta.get("posted"):
        try:
            _chk = result.get("check") if isinstance(result.get("check"), dict) else {}
            _proof = result.get(_ANALYSIS_VERDICT_KEY)
            _raw = _proof.get("verdict") if isinstance(_proof, dict) else None
            db("SELECT core.record_check_published_with_authority(%s,%s,%s,%s,%s)",
               (_change_id(f["pr"]), f["repo"], f["base"], f["head_sha"],
                _signal_token(_chk.get("conclusion"), _raw, bool(result.get("ack_state")))))
        except Exception as e:
            print(f"{_f_trace_prefix(f)}check_published record skipped repo={f['repo']} pr={f['pr']}: {str(e)[:120]}",
                  flush=True)

    # NEIGHBOR REFRESH: on open/sync this carries the OTHER in-flight changes whose verdict shifted because
    # THIS PR entered/re-scoped the neighborhood (the stale-verdict-on-open fix); on merge/withdraw it carries
    # the promoted/freed neighbors. Never POST those neighbors from this webhook transaction. A single event can
    # produce the bounded 30-entry surface and each entry may require several GitHub calls; doing that here held
    # the repository lock + one DB transaction until the 85-second event guard killed its own socket. The graph
    # outbox already owns the exact durable convergence path: it resolves authoritative HEAD, recomputes the
    # current in-flight set, posts one cursor-bounded slice with fresh DB connections, and requeues fairly. Wake
    # that existing turn below and report the deferred count honestly. The acting PR's own Check above remains
    # synchronous. BACKFILL suppresses the wake (it visits every PR directly).
    # PAUSE-ACK: an ack-label change re-evaluates THIS PR only — it touches no files and shifts no neighbor's
    # verdict, so it must NOT fan a neighbor-refresh storm across the whole in-flight set on a single label
    # click (the SAME suppressor the re-run replay + backfill use — _veripsa_no_neighbor_refresh).
    if payload.get("_veripsa_no_neighbor_refresh") or f["is_ack_label_event"]:
        result["refreshed_inflight"] = 0
        result["refresh_deferred"] = 0
    else:
        # A docs-only acting PR is correctly independent of the code graph, but its release/re-scope can still
        # produce graph-dependent NEIGHBOR refresh entries. Do one DB-local target comparison only when such
        # entries exist. If stale, enqueue background convergence and withhold neighbor clear-resets while the
        # acting docs Check remains the success already posted above. This closes the code→README rescope hole:
        # docs-only never pays clone latency, and a sibling can never be falsely cleared over an old graph.
        neighbor_entries = result.get("refreshed", []) or []
        if graph_heal is None and neighbor_entries:
            # Docs-only can alter a code sibling's neighborhood, but base_sha still is not current-HEAD proof.
            # Force one coalesced background turn and withhold sibling clears until that worker verifies the ref.
            graph_heal = request_main_graph_refresh_wake_only(
                db, f["repo"], f["default_branch"], f.get("base_sha"), f.get("repository_id"),
                trace_id=f.get("trace_id", ""))
            result["graph_heal"] = graph_heal
        # The durable graph turn recomputes instead of trusting this transient payload, then applies the same
        # pause/degraded-graph overlays through `_post_refreshes(..., return_progress=True)` outside this event.
        # `refreshed_inflight` remains the number posted by this event (zero); `refresh_deferred` is only a bounded
        # content-free observation, not a promise that stale entries themselves will be posted unchanged.
        result["refreshed_inflight"] = 0
        result["refresh_deferred"] = len(neighbor_entries)
    # Internal PR-only surface used to keep pause/neighbor decisions honest under branch-list uncertainty. The
    # returned event remains compact/content-free; the public flag + reconcile summary below are sufficient.
    result.pop("_branch_filtered_impact", None)
    result.pop("_branch_unknown_changes", None)

    _pr_record_landing(db, gh, f)
    return result


def _validated_merge_group_pr_head(raw_full, repo: str, num, default_branch: str) -> str | None:
    """Return the exact head SHA only for an authoritative, analyzable merge-queue PR.

    A successful API call is not analysis authority by itself.  Merge-group clear requires an open PR with an
    exact number, default-branch base, and positively known same-repository head/base identity.  Anything sparse,
    malformed, closed/merged, forked, or out of window is an honest unknown.
    """
    if not isinstance(raw_full, dict) or isinstance(num, bool) or not isinstance(num, int):
        return None
    if raw_full.get("number") != num or raw_full.get("state") != "open" or raw_full.get("merged") is not False:
        return None
    head = raw_full.get("head")
    base = raw_full.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        return None
    head_sha = head.get("sha")
    if (not isinstance(head_sha, str) or not head_sha.strip() or len(head_sha.strip()) > 64
            or base.get("ref") != default_branch):
        return None
    head_repo = head.get("repo")
    base_repo = base.get("repo")
    if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
        return None
    head_repo_id, base_repo_id = head_repo.get("id"), base_repo.get("id")
    head_repo_name, base_repo_name = head_repo.get("full_name"), base_repo.get("full_name")
    if head_repo_id not in (None, "") and base_repo_id not in (None, ""):
        same_repo = str(head_repo_id) == str(base_repo_id)
    elif (isinstance(head_repo_name, str) and head_repo_name.strip()
          and isinstance(base_repo_name, str) and base_repo_name.strip()):
        same_repo = head_repo_name.strip().lower() == base_repo_name.strip().lower()
    else:
        same_repo = False
    if not same_repo:
        return None
    if (isinstance(base_repo_name, str) and base_repo_name.strip()
            and base_repo_name.strip().lower() != repo.lower()):
        return None
    return head_sha.strip()


def _rerun_prs(entries, repo: str, default_branch: str, db, gh, log_label: str, trace_id: str = "",
               suppress_neighbor_refresh: bool = True,
               default_branch_authoritative: bool = False,
               required_check_sha: str = "",
               require_validated_analysis: bool = False) -> tuple[list, bool]:
    """SHARED replay loop for check_suite/check_run (rerequest) AND merge_group (checks_requested): fetch the
    AUTHORITATIVE PR → _rerun_replay into a synthesized 'synchronize' → re-run through handle_event → cap.
    `entries` is an iterable of (num, sparse_base_ref): sparse_base_ref is the PR's base ref from the sparse
    check payload (used for the out-of-window pre-skip) or None when the caller already resolved in-window PR
    NUMBERS (merge_group — its head_ref only encodes in-window numbers, so it passes None and the pre-skip is a
    no-op). BOUNDED by _RERUN_PR_CAP and per-PR fail-soft for historical callers. Returns (reran, capped_out) —
    the change refs re-run + whether the cap truncated the batch. This is
    lifted verbatim from the two identical loops so behaviour is byte-for-byte unchanged; `log_label` reproduces
    each site's exact log prefix ('<event_type> rerequest' or 'merge_group'). `suppress_neighbor_refresh` stays
    True for customer-initiated re-run/backfill-style replays; branch-push replay passes False because a pushed
    head update must refresh the same neighborhood as a live pull_request synchronize.
    `default_branch_authoritative` is threaded into every synthetic payload so a legacy `main` guess may analyze
    conservatively but can never authorize BR release-by-difference.

    `required_check_sha` is used only by Veripsa-owned `check_suite.requested`.  When the authoritative PR read
    proves an open, same-repo, default-branch PR at that exact head, a normal return is not enough: the replay
    must report an exact acting Check with `posted=true`, otherwise this outer delivery raises for queue retry.
    All historical callers omit it and retain the original per-PR fail-soft behaviour.

    `require_validated_analysis` is the merge-queue authority boundary.  It counts a PR only after an authoritative
    open/same-repo/default-branch read and an explicit exact-head Check operation with `posted=true`; a normal
    no-op/malformed return is not evidence that the batch was analyzed."""
    reran, capped_out = [], False
    required_failures = []
    strict_requested = isinstance(required_check_sha, str) and bool(required_check_sha)
    strict_proofs = 0
    analysis_proofs = 0
    strict_satisfied = False
    _rerun_cap = _server()._RERUN_PR_CAP                # read off server at call time (honors the test patch)
    _tp = f"trace_id={trace_id[:_TRACE_TAG_WIDTH]} " if trace_id else ""
    for num, sparse_base_ref in entries:
        if num is None:
            continue
        if (not strict_requested and sparse_base_ref and sparse_base_ref != default_branch):
            # Historical callers may pre-skip from a sparse check ref.  The exact-target contract must instead
            # fetch the authoritative PR: a stale sparse base must not suppress a required current Check.
            continue
        if strict_requested:
            # The ordinary replay cap cannot bound proof discovery: a stale/closed/fork entry before the exact
            # current PR is not a replay and must not let this delivery finish without its Check. Scan farther,
            # but retain a separate hard ceiling; an over-ceiling payload fails closed instead of silently
            # completing, while normal one/few-PR suites still need only one or two authoritative reads.
            if strict_proofs >= _CHECK_SUITE_REQUESTED_PROOF_CAP:
                capped_out = True
                required_failures.append("authoritative PR proof scan capped")
                break
            strict_proofs += 1
        elif require_validated_analysis:
            if analysis_proofs >= _rerun_cap:
                capped_out = True
                break
            analysis_proofs += 1
        elif len(reran) >= _rerun_cap:
            # Historical replay callers retain the existing bounded fan-out contract.
            capped_out = True
            break
        # FETCH THE AUTHORITATIVE PR (never trust the sparse check payload entry for authorship or head): a
        # rerequest's PR ref is a SPARSE pointer (number + base) — its `user`/`head` are NOT a reliable source.
        # Reusing them mis-attributed the re-declared claim to the wrong author and collided on the existing
        # (correctly-authored) active claim → a crash. Re-fetch the real PR object (author / head sha / base ref /
        # draft / merged) — the SAME fields a live pull_request webhook carries — and replay THAT through the live
        # 'synchronize' path (idempotent on an already-declared claim). Historical callers log and skip one PR's
        # fetch/replay failure; strict requested replay records a retry requirement instead.
        try:
            raw_full = gh.get_pull_request(repo, num)
        except Exception as e:                          # PR unresolvable (closed/dequeued mid-flight / API error) → skip it
            print(f"{_tp}{log_label} fetch skipped repo={repo} pr={num}: {str(e)[:140]}", flush=True)
            if strict_requested and _http_status(e) not in (404, 410):
                required_failures.append(f"PR-{num}: authoritative fetch failed")
            continue
        validated_analysis_sha = ""
        if require_validated_analysis:
            validated_analysis_sha = _validated_merge_group_pr_head(raw_full, repo, num, default_branch) or ""
            if not validated_analysis_sha:
                print(f"{_tp}{log_label} validation skipped repo={repo} pr={num}: "
                      "not an open same-repo default-branch PR", flush=True)
                continue
        if strict_requested and not isinstance(raw_full, dict):
            required_failures.append(f"PR-{num}: malformed authoritative PR")
            continue
        full = _as_obj(raw_full)
        full_head_raw = full.get("head")
        full_base_raw = full.get("base")
        if strict_requested:
            head_repo_raw = full_head_raw.get("repo") if isinstance(full_head_raw, dict) else None
            base_repo_raw = full_base_raw.get("repo") if isinstance(full_base_raw, dict) else None
            head_repo_explicitly_gone = isinstance(full_head_raw, dict) and "repo" in full_head_raw and head_repo_raw is None
            base_repo_identity = _as_obj(base_repo_raw)
            head_repo_identity = _as_obj(head_repo_raw)
            malformed_full = (
                not isinstance(full_head_raw, dict) or not isinstance(full_base_raw, dict)
                or not isinstance(full_head_raw.get("sha"), str) or not full_head_raw.get("sha")
                or not isinstance(full_base_raw.get("ref"), str) or not full_base_raw.get("ref")
                or full.get("state") not in ("open", "closed")
                or not isinstance(full.get("merged"), bool)
                or "repo" not in full_head_raw or "repo" not in full_base_raw
                or (head_repo_raw is not None and not isinstance(head_repo_raw, dict))
                or not isinstance(base_repo_raw, dict)
                or (base_repo_identity.get("id") in (None, "")
                    and not (isinstance(base_repo_identity.get("full_name"), str)
                             and base_repo_identity.get("full_name")))
                or (not head_repo_explicitly_gone
                    and head_repo_identity.get("id") in (None, "")
                    and not (isinstance(head_repo_identity.get("full_name"), str)
                             and head_repo_identity.get("full_name")))
            )
            if malformed_full:
                required_failures.append(f"PR-{num}: malformed authoritative PR")
                continue
            if head_repo_explicitly_gone:
                # A deleted head repository cannot own the requested exact Check; this is a clean non-candidate.
                continue
        full_base = _as_obj(full.get("base")).get("ref") or sparse_base_ref or default_branch
        if full_base != default_branch:                 # re-check on the AUTHORITATIVE base (it may differ from the sparse ref)
            continue

        required_candidate = False
        if strict_requested:
            full_head = _as_obj(full_head_raw)
            full_base_obj = _as_obj(full_base_raw)
            head_repo = _as_obj(full_head.get("repo"))
            base_repo = _as_obj(full_base_obj.get("repo"))
            head_repo_id = head_repo.get("id")
            base_repo_id = base_repo.get("id")
            head_repo_name = head_repo.get("full_name")
            base_repo_name = base_repo.get("full_name")
            if head_repo_id is not None and base_repo_id is not None:
                same_repo = str(head_repo_id) == str(base_repo_id)
                repo_identity_known = True
            elif isinstance(head_repo_name, str) and head_repo_name and isinstance(base_repo_name, str) and base_repo_name:
                same_repo = head_repo_name.lower() == base_repo_name.lower()
                repo_identity_known = True
            else:
                same_repo = False
                repo_identity_known = False

            # Positively non-required cases are clean skips: this requested suite cannot own a current Check for
            # a closed/merged, stale-head, fork, or out-of-window PR. Missing mandatory proof was rejected above;
            # ordinary non-strict callers still accept their historical sparse fakes unchanged.
            if full.get("state") != "open" and isinstance(full.get("state"), str):
                continue
            if full.get("merged") is True:
                continue
            if full_head.get("sha") != required_check_sha:
                continue
            if repo_identity_known and not same_repo:
                continue
            required_candidate = (
                full.get("state") == "open"
                and full.get("merged") is not True
                and full_head.get("sha") == required_check_sha
                and full_base == default_branch
                and repo_identity_known
                and same_repo
            )
        if _accepts_keyword(_rerun_replay, "default_branch_authoritative"):
            replay = _rerun_replay(
                full, repo, default_branch, full_base, num,
                default_branch_authoritative=default_branch_authoritative)
        else:
            replay = _rerun_replay(full, repo, default_branch, full_base, num)
            # Historical injected replay builders remain callable, but their synthetic work still carries the
            # authority boundary before it re-enters the live handler.
            replay["_veripsa_default_branch_authoritative"] = bool(default_branch_authoritative)
        if not suppress_neighbor_refresh:
            replay.pop("_veripsa_no_neighbor_refresh", None)
        try:
            replay_result = handle_event("pull_request", replay, db, gh)
            if require_validated_analysis:
                operation = replay_result.get(_CHECK_OPERATION_KEY) if isinstance(replay_result, dict) else None
                verdict_proof = replay_result.get(_ANALYSIS_VERDICT_KEY) if isinstance(replay_result, dict) else None
                expected_change = _change_id(num)
                if (not isinstance(replay_result, dict)
                        or replay_result.get("noop") is True or bool(replay_result.get("skipped"))
                        or replay_result.get("quota_paused") is True
                        or replay_result.get("branch_inventory_unknown") is True
                        or not isinstance(operation, dict)
                        or operation.get("repo") != repo
                        or operation.get("sha") != validated_analysis_sha
                        or operation.get("posted") is not True
                        or not isinstance(verdict_proof, dict)
                        or verdict_proof.get("repo") != repo
                        or verdict_proof.get("branch") != default_branch
                        or verdict_proof.get("change_id") != expected_change
                        or verdict_proof.get("head_sha") != validated_analysis_sha
                        or verdict_proof.get("verdict") not in _MERGE_QUEUE_PROVEN_VERDICTS):
                    print(f"{_tp}{log_label} analysis skipped repo={repo} pr={num}: "
                          "no explicit authoritative structural verdict on the posted exact-head Check", flush=True)
                    continue
            if required_candidate:
                operation = replay_result.get(_CHECK_OPERATION_KEY) if isinstance(replay_result, dict) else None
                if not (isinstance(operation, dict)
                        and operation.get("repo") == repo
                        and operation.get("sha") == required_check_sha
                        and operation.get("posted") is True):
                    required_failures.append(f"PR-{num}: exact Check was not posted")
                    continue
            reran.append(_change_id(num))
            if required_candidate:
                # A Check Run is keyed by repo + SHA, not by PR number. One exact successful operation satisfies
                # this requested suite even if GitHub associates the shared commit with additional PRs.
                strict_satisfied = True
                break
        except Exception as e:                          # normal callers stay fail-soft; strict candidates retry outside
            print(f"{_tp}{log_label} re-run skipped repo={repo} pr={num}: {str(e)[:140]}", flush=True)
            if required_candidate:
                required_failures.append(f"PR-{num}: replay failed")
    if required_failures and not strict_satisfied:
        raise RuntimeError("check_suite.requested exact-target replay retry required: "
                           + "; ".join(required_failures[:8]))
    return reran, capped_out


def _branch_push_pr_entries(gh, repo: str, branch: str, default_branch: str, trace_id: str = "") -> list[tuple]:
    """Resolve same-repo open PRs whose head branch just received a push.

    A feature-branch push is the earliest signal Veripsa receives, but once a PR already exists that same push is
    also the PR's new head. Replaying the matching PR through the normal synchronize path keeps check runs,
    comments, and lane reconciliation current even when the pull_request webhook is delayed or dropped. Exact
    live path: use GitHub's `head=<owner>:<branch>&base=<default>` filter when the client has it. Fallback path:
    tolerate older/fake clients by filtering their open-PR list locally. Fork PRs are skipped when identifiable;
    a base-repo push cannot update a fork head. Content-free: only PR numbers and refs are read."""
    _tp = f"trace_id={trace_id[:_TRACE_TAG_WIDTH]} " if trace_id else ""
    try:
        if hasattr(gh, "list_open_pull_requests_for_head"):
            prs = gh.list_open_pull_requests_for_head(repo, branch, default_branch,
                                                      limit=_server()._RERUN_PR_CAP)
        elif hasattr(gh, "list_open_pull_requests"):
            prs = gh.list_open_pull_requests(repo)
        else:
            return []
    except Exception as e:
        print(f"{_tp}push {repo}@{branch} open-PR lookup skipped: {str(e)[:140]}", flush=True)
        return []
    out = []
    seen = set()
    for pr in _as_list(prs):
        pr = _as_obj(pr)
        num = pr.get("number")
        if num is None or num in seen:
            continue
        base_obj = _as_obj(pr.get("base"))
        base_ref = base_obj.get("ref")
        if base_ref and base_ref != default_branch:
            continue
        head_obj = _as_obj(pr.get("head"))
        if head_obj.get("ref") != branch:
            continue
        head_repo = _as_obj(head_obj.get("repo"))
        head_repo_name = head_repo.get("full_name")
        if head_repo_name and head_repo_name != repo:
            continue
        out.append((num, base_ref))
        seen.add(num)
    return out


def _handle_push_event(payload: dict, db, gh, coalesce=None) -> dict:
    """PUSH tier: a push to the protected (default) branch ingests main's graph + refreshes the in-flight
    neighborhood; a push to a NON-main feature branch reserves lanes; a non-main branch-delete releases its
    push-time lanes; a no-head push is a clean no-op. Lifted VERBATIM from handle_event's inline `push` branch
    (the dispatch + every return dict are byte-identical) — coalesce is threaded through to ingest_push exactly
    as the router passed it."""
    repository = _as_obj(payload.get("repository"))       # POISON-EVENT TOLERANCE: no/wrong-typed repo → clean no-op
    repo = repository.get("full_name")
    branch = _branch_from_ref(payload.get("ref"))         # strip refs/heads/ but KEEP slashes (feature/x)
    payload_default_branch = repository.get("default_branch")
    default_branch_authoritative = isinstance(payload_default_branch, str) and bool(payload_default_branch)
    default_branch = payload_default_branch if default_branch_authoritative else "main"
    after = payload.get("after")
    sha = after if isinstance(after, str) else ""         # a non-string 'after' (malformed) → treated as no head
    if not repo:
        return {"event": "push", "skipped": "malformed payload (no repository)"}
    deleted = bool(payload.get("deleted")) or (sha.strip("0") == "" if sha else True)  # branch-delete push (after=000…)
    _tp = _trace_log_prefix(payload)
    if branch and branch == default_branch and sha and not sha.strip("0") == "":
        res = ingest_push_deferred(
            db, gh, repo, branch, sha, payload, coalesce=coalesce)
        # FREE-TIER WALL: the account is over the free line → ingest_push recorded/ingested NOTHING and returned
        # the quota_exceeded signal. The graph did NOT change, so the neighborhood refresh below is pointless
        # (skip it — no work, no stale-verdict risk). Log honestly (content-free) + return the signal. A bare
        # push has no PR conversation to comment on; the user-facing note is posted on the PR path (below).
        if _quota_result(res) is not None:
            print(f"{_tp}push {repo}@{branch} {sha[:7]} → EARLY-ACCESS LIMIT reached (dimension={res.get('dimension')}) — graph NOT updated", flush=True)
            return res
        if res.get("coalesced") or res.get("stale"):
            # No graph changed. A newer queued push owns convergence, or this delivery is older than the stored
            # graph; rendering neighbors now would only repeat a stale/no-op surface.
            return res
        if res.get("mode") in ("queued", "skip") and isinstance(res.get("graph_refresh"), dict):
            # The graph and every graph-dependent customer surface now have one owner: the durable background
            # convergence turn. Refreshing neighbors or posting the cold-start watching Check here would read the
            # *previous* graph and create a stale false-clear/duplicate-post flap. The background worker performs
            # both only after the exact graph write has committed.
            print(
                f"{_tp}push {repo}@{branch} {sha[:7]} → graph "
                f"{'queued' if res['graph_refresh'].get('queued') else 'already current'}; "
                "webhook hot path complete",
                flush=True,
            )
            return res
        # COLD-START WATCHING SIGNAL (explicit/rolling fallback): ingest_push can return cold_start=True for the
        # first graph-bearing push to a deferred or previously empty repository. Current queue-only ingress gives
        # this surface to account-fair convergence after exact HEAD proof; this retained inline path covers callers
        # that have no durable onboarding turn. NEVER-CRASH: a failed post must not abort the ingest result. Idempotent:
        # _safe_upsert_check patches if a check for this sha already exists. Content-free: file/edge counts
        # from the ingest stats, never a path/symbol/body. over_cap → post the over-cap-honest watching check.
        if res.get("cold_start"):
            try:
                _cw = watching_check(
                    files=int(res.get("files") or 0), edges=int(res.get("edges") or 0),
                    branch=branch, indexing=False,
                    over_cap=bool(res.get("over_cap")))
                if _safe_upsert_check(gh, repo, sha, _cw["conclusion"], _cw["title"], _cw["summary"]):
                    res["watching_signalled"] = True
                    print(f"{_tp}push {repo}@{branch} {sha[:7]} → cold-start watching check posted (files={res.get('files')} over_cap={res.get('over_cap')})", flush=True)
            except Exception as _we:
                print(f"{_tp}push {repo}@{branch} {sha[:7]} → cold-start watching check FAILED (non-fatal): {str(_we)[:120]}", flush=True)
        # STALE-VERDICT FIX: main's graph just changed → every in-flight PR's blast radius/coupling may have
        # shifted. The merge/withdraw paths already refresh the neighborhood, but a DIRECT push to main (a
        # hotfix, an admin commit, a squash that arrives only as a push) would otherwise leave open PRs
        # showing a STALE verdict until their author next pushes. Re-render + re-post (idempotent upsert →
        # no comment spam; 'clear' PRs are skipped) so a wrong verdict can't sit on a customer's PR. Never
        # let a refresh-post error abort the push ingest (the graph + landing are already recorded).
        try:
            payload_default_branch = repository.get("default_branch")

            def reconcile_push_branch_claims():
                if (not isinstance(payload_default_branch, str) or not payload_default_branch
                        or payload_default_branch != branch):
                    raise RuntimeError("authoritative default-branch metadata unavailable")
                return _reconcile_live_branch_claims(db, gh, repo, payload_default_branch)

            if _accepts_keyword(refresh_inflight, "reconcile_branch_claims"):
                refresh_kwargs = {"reconcile_branch_claims": reconcile_push_branch_claims}
                if _accepts_keyword(refresh_inflight, "trace_id"):
                    refresh_kwargs["trace_id"] = _trace_of(payload)
                refreshed = refresh_inflight(db, repo, branch, **refresh_kwargs)
            else:
                refreshed = refresh_inflight(db, repo, branch)
            # db+branch threaded so a material neighbor keeps its paused (action_required) check on a push refresh.
            # G5: a push has no self-heal / graph_heal in scope, but a DEFERRED or FAILED push-ingest can still
            # leave main's stored graph BEHIND HEAD — reproducing the same false-clear flap on an acked neighbor.
            # Derive the degraded signal cheaply from one freshness read (HEAD is already resolved for this event,
            # so this is ~free). OWN try/except → False so a freshness-read error degrades to today's behavior
            # (never skips the whole refresh, never crashes); only when db+gh are available.
            push_graph_degraded = False
            if db is not None and gh is not None:
                try:
                    push_graph_degraded = graph_freshness(db, gh, repo, branch).get("behind") is True
                except Exception as e:
                    print(f"{_tp}push refresh freshness read skipped repo={repo}: {str(e)[:120]}", flush=True)
                    push_graph_degraded = False
            posted = _post_refreshes(gh, repo, refreshed.get("refreshed", []), db=db, branch=branch,
                                     trace_id=_trace_of(payload), delivery=_delivery_of(payload),
                                     graph_degraded=push_graph_degraded)
            res["refreshed_inflight"] = posted
        except Exception as e:
            print(f"{_tp}push refresh skipped repo={repo}: {str(e)[:160]}", flush=True)
        print(f"{_tp}push {repo}@{branch} {sha[:7]} → ingest mode={res.get('mode')} files={res.get('files')} edges={res.get('edges')} refreshed={res.get('refreshed_inflight',0)}", flush=True)
        return res
    # NON-MAIN BRANCH DELETE → release lanes NOW. The corresponding non-main push reserved work under
    # change_id 'BR-<branch>' before any PR existed; if the branch is deleted without ever opening a PR, those
    # lanes otherwise remain live until lease expiry and can keep future work waiting behind a ghost branch.
    # Use the same withdraw primitive as PR-close/base-retarget so waiter promotion stays centralized in the
    # gate. NEVER-CRASH: a DB failure is logged and returned as a skipped release, not raised out of the
    # webhook worker. Main-branch deletes (rare/malformed) do not ingest or release BR lanes.
    if branch and deleted and branch != default_branch:
        change_id = _branch_change_id(branch)
        released = None
        try:
            released = db("SELECT core.release_change_on_main_with_authority(%s,%s,%s)",
                          (change_id, repo, default_branch))
        except Exception as e:
            print(f"{_tp}push {repo}@{branch} delete → branch-lane release skipped: {str(e)[:120]}", flush=True)
            return {"event": "push", "repo": repo, "branch": branch, "deleted": True,
                    "change_id": change_id, "released": None, "skipped": "branch delete release failed"}
        print(f"{_tp}push {repo}@{branch} delete → released branch lanes as {change_id}", flush=True)
        return {"event": "push", "repo": repo, "branch": branch, "deleted": True,
                "change_id": change_id, "released": released}

    # NON-MAIN BRANCH push → reserve lanes NOW (the pre-merge window opens at PUSH time, before any PR). We
    # do NOT ingest a feature branch's graph (only main's is the prediction baseline), but we DO reserve its
    # changed paths against main's lane namespace so collision/serialize can fire before a PR is opened.
    # NOTIFY-ONLY: this only records reservations.
    if branch and not deleted:
        res = reserve_branch_lanes(db, repo, branch, default_branch, payload)
        entries = _branch_push_pr_entries(gh, repo, branch, default_branch, trace_id=_trace_of(payload))
        if entries:
            reran, capped_out = _rerun_prs(entries, repo, default_branch, db, gh, "branch push",
                                           trace_id=_trace_of(payload), suppress_neighbor_refresh=False,
                                           default_branch_authoritative=default_branch_authoritative)
            res["replayed_prs"] = reran
            res["replay_capped"] = capped_out
        else:
            res["replayed_prs"] = []
            res["replay_capped"] = False
        print(f"{_tp}push {repo}@{branch} (feature) → reserved {res.get('reserved')} lanes on {default_branch} "
              f"as {res.get('change_id')}; replayed {res.get('replayed_prs')}", flush=True)
        return res
    return {"push": repo, "branch": branch, "skipped": "branch delete / no head"}


def _check_suite_entries_from_head(repo: str, head_sha: str, default_branch: str, gh) -> list:
    """Resolve (PR number, base ref) entries for the OPEN, same-repo, default-branch PR(s) whose head is
    EXACTLY head_sha — the check_suite.requested backstop's fallback when GitHub delivers an empty
    pull_requests[] array (observed under webhook-processing lag). Content-free (PR number/refs/head
    metadata only), fork-excluding (same-repo head only, so a shared sha on a fork never earns a base-repo
    Check), and bounded by _RERUN_PR_CAP. Best-effort: any read error or a client without the head resolver
    → [] so the backstop stays a clean no-op rather than crashing."""
    if not (isinstance(head_sha, str) and head_sha):
        return []
    cap = _server()._RERUN_PR_CAP
    resolver = getattr(gh, "list_pull_requests_for_commit", None)
    if resolver is None:
        return []
    try:
        prs = resolver(repo, head_sha, limit=cap) or []
    except Exception:
        return []
    entries, seen = [], set()
    for prj in prs:
        prj = _as_obj(prj)
        num = prj.get("number")
        if num is None or num in seen:
            continue
        if str(prj.get("state") or "").lower() != "open":
            continue
        head = _as_obj(prj.get("head"))
        # Exact head match AND same-repo head: only the PR currently AT this sha in THIS repo. A fork PR that
        # merely shares the commit must never get an exact Check posted on the base repo (fork-safety, gate 104).
        if str(head.get("sha") or "") != head_sha:
            continue
        if str(_as_obj(head.get("repo")).get("full_name") or "") != repo:
            continue
        base_ref = _as_obj(prj.get("base")).get("ref")
        if base_ref and base_ref != default_branch:
            continue
        seen.add(num)
        entries.append((num, base_ref))
        if len(entries) >= cap:
            break
    return entries


def _handle_check_event(event_type: str, payload: dict, db, gh) -> dict:
    """CHECK_SUITE / CHECK_RUN tier: a re-run request (rerequested/requested_action) replays each associated
    in-window PR through the live router; a Veripsa-owned `check_suite.requested` is a backstop for a missed
    pull_request/push delivery on a fresh head; a `completed` event with a failing conclusion records the
    stuck-PR fact; everything else is a clean no-op."""
    node = _as_obj(payload.get(event_type))            # check_suite{} | check_run{}
    action = payload.get("action")
    repository = _as_obj(payload.get("repository"))     # POISON-EVENT TOLERANCE: no/wrong-typed repo → clean no-op
    repo = repository.get("full_name")
    if not repo:
        return {"event": event_type, "skipped": "malformed payload (no repository)"}
    payload_default_branch = repository.get("default_branch")
    default_branch_authoritative = isinstance(payload_default_branch, str) and bool(payload_default_branch)
    default_branch = payload_default_branch if default_branch_authoritative else "main"
    _hs = node.get("head_sha")
    head_sha = (_hs if isinstance(_hs, str) else "")[:64]
    # a check_run nests the suite's PRs under check_suite; a check_suite has them at top level.
    prs = _as_list(node.get("pull_requests")) or _as_list(_as_obj(node.get("check_suite")).get("pull_requests"))

    # RE-RUN REQUEST → re-analyze + re-post the Veripsa check. The customer pressed "Re-run all checks" in the
    # merge box (check_suite action=rerequested) or "Re-run" on the Veripsa check row (check_run action=
    # rerequested / requested_action). The check run IS the merge-box surface, so a re-run is the customer's
    # EXPLICIT ask to refresh Veripsa's verdict — and GitHub does NOT re-send a pull_request event for it, so
    # THIS event is the ONLY signal. Ignoring it left the merge box showing a stale Veripsa verdict and made
    # the button do nothing (a broken-feeling surface). We replay each associated, in-window PR through the
    # SAME live router as a synthesized 'synchronize' — the identical analyze+post path a real head-update
    # takes (never a second code path that could drift). Content-free (we read only the PR NUMBER + base ref
    # from pull_requests[]; the PR's author/head come from a re-fetch — never a file body), BOUNDED
    # (_RERUN_PR_CAP — one press can't fan out into an API storm), and never-crash (each PR's fetch+replay is
    # best-effort; one failure cannot abort the rest or raise out of the handler). check_run `requested_action` is
    # folded in because Veripsa posts no check `actions`, so the only honest meaning of a button on its row is
    # "refresh me".
    is_rerun_request = action == "rerequested" or (event_type == "check_run" and action == "requested_action")
    if is_rerun_request:
        # Each entry is (PR number, sparse base ref) parsed from the check payload's pull_requests[]; a
        # non-object entry → no number → skipped inside _rerun_prs (same as the old inline `num is None` skip).
        entries = [(_as_obj(prj).get("number"), _as_obj(_as_obj(prj).get("base")).get("ref")) for prj in prs]
        _tp_check = _trace_log_prefix(payload)
        rerun, capped_out = _rerun_prs(entries, repo, default_branch, db, gh, f"{event_type} rerequest",
                                       trace_id=_trace_of(payload),
                                       default_branch_authoritative=default_branch_authoritative)
        if rerun:
            print(f"{_tp_check}{event_type} {repo} rerequested → re-ran {rerun}"
                  + (f" (+more capped at {_server()._RERUN_PR_CAP})" if capped_out else ""), flush=True)
        return {"event": event_type, "action": action, "repo": repo, "reran": rerun, "rerun_capped": capped_out}

    # CHECK-SUITE REQUESTED BACKSTOP: GitHub creates the App's check suite for a fresh PR head even when the
    # normal pull_request.synchronize or base-repo push delivery is delayed/dropped. If we ignore that requested
    # suite, GitHub leaves "Veripsa expected/queued" with no check-run. For Veripsa's OWN suite, replay the
    # associated PRs through the same live synchronize path as branch-push replay. This is a resilience backstop,
    # not a second verdict path. Scope it to the Veripsa app so other CI providers' suite creation cannot fan out
    # redundant refreshes.
    if event_type == "check_suite" and action == "requested":
        app = _as_obj(node.get("app"))
        app_slug = str(app.get("slug") or "").lower()
        app_name = str(app.get("name") or "").lower()
        is_veripsa_suite = app_slug == "veripsa-core" or app_name in {"veripsa", "veripsa core", "veripsa-core"}
        if not is_veripsa_suite:
            return {"event": event_type, "action": action, "repo": repo, "noop": True,
                    "skipped": f"non-Veripsa check suite requested ({app_slug or app_name or 'unknown app'})"}
        entries = [(_as_obj(prj).get("number"), _as_obj(_as_obj(prj).get("base")).get("ref")) for prj in prs]
        # BACKSTOP-OF-THE-BACKSTOP: GitHub sometimes delivers check_suite.requested with an EMPTY
        # pull_requests[] (observed under webhook-processing lag) even though the suite was created for a real
        # PR head. With no numbers the replay found nothing and the missed pull_request delivery stayed
        # un-checked ("Veripsa expected" forever). Resolve the acting PR(s) from the suite's own head_sha so
        # the backstop can still fire. _rerun_prs(required_check_sha=head_sha) re-validates the live head, so a
        # PR whose head has since moved simply does not post — never a stale-head Check.
        resolved_by_head = False
        if not any(num is not None for num, _ in entries) and head_sha:
            head_entries = _check_suite_entries_from_head(repo, head_sha, default_branch, gh)
            if head_entries:
                entries = head_entries
                resolved_by_head = True
        _tp_check = _trace_log_prefix(payload)
        rerun, capped_out = _rerun_prs(entries, repo, default_branch, db, gh, "check_suite requested",
                                       trace_id=_trace_of(payload), suppress_neighbor_refresh=False,
                                       default_branch_authoritative=default_branch_authoritative,
                                       required_check_sha=head_sha)
        print(f"{_tp_check}check_suite {repo} requested {head_sha[:7]} → backstop "
              f"associated_prs={len(entries)}{' (by head_sha)' if resolved_by_head else ''} "
              f"reran={len(rerun)} {rerun}"
              + (f" (+more capped at {_server()._RERUN_PR_CAP})" if capped_out else ""), flush=True)
        return {"event": event_type, "action": action, "repo": repo, "head_sha": head_sha,
                "associated_prs": len(entries), "resolved_by_head_sha": resolved_by_head,
                "reran": rerun, "rerun_capped": capped_out,
                "backstop": "check_suite.requested"}

    if action != "completed":
        return {"event": event_type, "noop": True, "skipped": "not a completed check"}
    if node.get("conclusion") not in _server()._FAILING_CONCLUSIONS:
        return {"event": event_type, "noop": True, "skipped": f"conclusion {node.get('conclusion')} is not a failure"}
    recorded = []
    for prj in prs:
        prj = _as_obj(prj)                             # a non-object PR entry → no number → skipped, no crash
        num = prj.get("number")
        if num is None:
            continue
        # SCOPE to the protected branch (mirror the pull_request path): only PRs heading to the default
        # branch are in Veripsa's window. A check on a PR targeting some other base is out of scope.
        base_ref = _as_obj(prj.get("base")).get("ref")
        if base_ref and base_ref != default_branch:
            continue
        # SAVEPOINT-ISOLATE this advisory, append-only write (audit P1): recording a 'failing' fact is
        # best-effort notify data, but a bare call shares the body txn, so one failure (e.g. the session write
        # context not yet pinned on this connection for a just-provisioned tenant, or a CHECK on a bad sha)
        # ABORTS the whole check_suite delivery → it re-queues and fails the same way = the signal is dropped.
        # Every other advisory write here uses _optional to roll back ONLY itself; this one was the exception.
        # record_pr_failing_with_authority is idempotent (ON CONFLICT DO NOTHING), so isolating it is safe.
        rec = _optional(
            db, "stuck-PR failing fact",
            lambda: db("SELECT core.record_pr_failing_with_authority(%s,%s,%s,%s,%s)",
                       (_change_id(num), repo, default_branch, head_sha or None, "ci_failed")),
            default=None, repo=repo, pr=num, trace_id=_trace_of(payload))
        if rec is not None:                            # only count a PR that was actually recorded (accurate log/return)
            recorded.append(_change_id(num))
    if recorded:
        _tp_chk = _trace_log_prefix(payload)
        print(f"{_tp_chk}{event_type} {repo} {head_sha[:7]} conclusion={node.get('conclusion')} → stuck {recorded}", flush=True)
    return {"event": event_type, "repo": repo, "conclusion": node.get("conclusion"), "stuck_prs": recorded}


def _handle_merge_group_event(payload: dict, db, gh) -> dict:
    """MERGE_GROUP tier: on `checks_requested` re-run each batched in-window PR through the live router, then
    ALWAYS post an advisory (success/neutral only — never blocking) check on the batch head so a required
    Veripsa check can never deadlock the queue; every other action is a clean no-op. Lifted VERBATIM from
    handle_event's inline merge_group branch (dispatch, _rerun_prs replay, surface read, and every return dict
    byte-identical)."""
    mg = _as_obj(payload.get("merge_group"))
    action = payload.get("action")
    repository = _as_obj(payload.get("repository"))     # POISON-EVENT TOLERANCE: no/wrong-typed repo → clean no-op
    repo = repository.get("full_name")
    if not isinstance(repo, str) or not repo:
        return {"event": "merge_group", "skipped": "malformed payload (no repository)"}
    # Only `checks_requested` is actionable (it is the queue ASKING for a status on the batch). `destroyed`
    # (the batch landed or dissolved) and any other action have nothing to post — a clean no-op.
    if action != "checks_requested":
        return {"event": "merge_group", "action": action, "noop": True,
                "skipped": "not a checks_requested merge_group"}
    _hs = mg.get("head_sha")
    head_sha = (_hs if isinstance(_hs, str) else "")[:64]
    if not head_sha:                                    # no batch commit to anchor a check on → clean no-op (never crash)
        return {"event": "merge_group", "action": action, "repo": repo,
                "skipped": "malformed payload (no merge_group head_sha)"}
    # SCOPE to the protected branch (mirror the PR/check paths). Missing default/base authority must not fall
    # through to a guessed-main "clear"; it gets the same neutral Unknown check as a graph-read failure.
    payload_default_branch = repository.get("default_branch")
    default_branch_authoritative = isinstance(payload_default_branch, str) and bool(payload_default_branch)
    default_branch = payload_default_branch if default_branch_authoritative else "main"
    base_from_ref = _branch_from_ref(mg.get("base_ref"))
    base = base_from_ref or mg.get("base_ref") or default_branch
    base_authoritative = bool(base_from_ref)
    in_window = (base == default_branch)
    # RESOLVE THE BATCHED PR(s): the head_ref encodes them as `gh-readonly-queue/<base>/pr-<n>-<sha>` segments
    # (content-free — only the integer PR numbers). We re-run each through the SAME live 'synchronize' router a
    # real head-update / a check rerun takes (so the batched changes' OWN PR checks + lanes are refreshed before
    # the batch lands — never a second analyze code path), BOUNDED by _RERUN_PR_CAP (a huge batch can't fan out
    # an API storm) + per-PR fail-soft (one PR's fetch/replay failing cannot abort the rest or crash the event).
    # This is the same fetch-authoritative-PR-then-replay shape as the rerun path. Then we post the BATCH check.
    batched_nums = _merge_queue_pr_numbers(mg.get("head_ref")) if in_window else []
    reran, capped_out = [], False
    validated_analysis_supported = True
    if in_window and batched_nums:
        # in_window ⟹ base == default_branch, and head_ref only encodes in-window numbers, so each entry's
        # sparse base ref is None (the out-of-window pre-skip is a no-op here — same as the old inline loop,
        # which had no per-number base pre-check); _rerun_prs still re-checks the AUTHORITATIVE base per PR.
        replay_entries = [(num, None) for num in batched_nums]
        if _accepts_keyword(_rerun_prs, "require_validated_analysis"):
            reran, capped_out = _rerun_prs(
                replay_entries, repo, default_branch, db, gh, "merge_group",
                trace_id=_trace_of(payload),
                default_branch_authoritative=default_branch_authoritative,
                require_validated_analysis=True)
        else:
            # Historical offline seams remain callable, but they cannot prove the new exact-head Check
            # operation. Their refresh side effects are harmless; their result must never authorize Clear.
            reran, capped_out = _rerun_prs(
                replay_entries, repo, default_branch, db, gh, "merge_group",
                trace_id=_trace_of(payload),
                default_branch_authoritative=default_branch_authoritative)
            validated_analysis_supported = False
    # RECOMPUTE only when the target branch and every queued PR were authoritatively resolved. Any missing
    # proof, replay cap/failure, malformed surface, or read error becomes an explicit neutral Unknown — never a
    # false clear, and never a queue-stalling conclusion.
    batch_refs = [_change_id(num) for num in batched_nums]
    can_analyze = (
        in_window and default_branch_authoritative and base_authoritative and bool(batched_nums)
        and validated_analysis_supported and not capped_out and len(reran) == len(batched_nums)
    )
    impact = None
    if can_analyze:
        impact = _optional(
            db, "merge_group impact read",
            lambda: db("SELECT core.main_impact_surface(%s,%s)", (repo, base)),
            default=None, repo=repo, pr="(merge_group)", trace_id=_trace_of(payload))
        if isinstance(impact, str):
            try:
                impact = json.loads(impact)
            except (ValueError, TypeError):
                impact = None
        if (not isinstance(impact, dict)
                or impact.get("repo") != repo or impact.get("branch") != base):
            impact = None
    chk = _render_merge_group_check(impact, batch_refs)
    check_meta = _upsert_check_result(gh, repo, head_sha, chk["conclusion"], chk["title"], chk["summary"])
    posted = bool(check_meta.get("posted"))
    _tp_mg = _trace_log_prefix(payload)
    _log_pr_surface(
        _tp_mg, surface="merge_group", delivery=_delivery_of(payload), event="merge_group", action=action,
        repo=repo, pr_number=None, head_sha=head_sha, comment_needed=False, comment_ok=False,
        check_meta=check_meta)
    if not posted:
        # Unlike an ordinary PR (where the comment is a second customer-visible surface), a merge-group head has
        # no fallback conversation. A required Veripsa Check that was not written would leave the queue waiting
        # forever if the durable delivery were marked done. Raise after the content-free operator log so the
        # durable inbox retries this exact delivery instead.
        raise RuntimeError("merge_group required Check was not posted")
    print(f"{_tp_mg}merge_group {repo} {head_sha[:7]} → check {chk['conclusion']} (verdict={chk['verdict']}, "
          f"batch={reran}" + (f" +more capped at {_server()._RERUN_PR_CAP}" if capped_out else "") + ")", flush=True)
    return {"event": "merge_group", "action": action, "repo": repo, "head_sha": head_sha,
            "conclusion": chk["conclusion"], "verdict": chk["verdict"], "check_posted": posted,
            "reran": reran, "rerun_capped": capped_out, "in_window": in_window}


def _marketplace_billing_enabled() -> bool:
    """KILL SWITCH for the GitHub-Marketplace billing path — DEFAULTS OFF (opt-in).

    Free-first launch policy: Marketplace purchase events must not write Core plans unless an operator explicitly
    sets VERIPSA_MARKETPLACE_BILLING=1 after implementing Marketplace entitlement sync. Turning this flag on for
    a future paid Marketplace SKU first requires reconciling the platform entitlement tables and payer↔org binding,
    so a Marketplace webhook cannot mint paid Core access for the wrong organization. Anything else (unset, '',
    '0', 'true') is OFF. Read at CALL time so a deploy/test can flip it without relying on
    import-time state."""
    return os.environ.get("VERIPSA_MARKETPLACE_BILLING", "") == "1"


def _handle_marketplace_event(payload: dict, db, gh) -> dict:
    """MARKETPLACE_PURCHASE tier: map the purchased plan onto core.account.plan when the explicit Marketplace
    billing switch is on. With the switch off, handle_event never reaches this body."""
    _tp = _trace_log_prefix(payload)
    action = payload.get("action")             # purchased | changed | cancelled | pending_change
    purchase = _as_obj(payload.get("marketplace_purchase"))
    gh_account_id = _as_obj(purchase.get("account")).get("id")
    if action not in ("purchased", "changed", "cancelled", "pending_change") or gh_account_id in (None, ""):
        return {"event": "marketplace_purchase", "action": action,
                "skipped": "unhandled action or missing account id"}
    # pending_change is a grace-period notice, not an effective entitlement change. The later changed/cancelled
    # delivery carries the actual plan.
    if action == "pending_change":
        return {"event": "marketplace_purchase", "action": action, "skipped": "grace-period notice — plan unchanged"}
    plan = "free" if action == "cancelled" else _marketplace_plan_name(purchase)
    # EVENT-ORDERING (audit iter-5 P2): thread the delivery's effective_date (content-free billing timestamp) into
    # the gated setter as its monotonicity high-water mark — GitHub re-delivers + does NOT order webhooks, so a
    # stale cancelled→free arriving AFTER a later purchased→pro must NOT regress a paying customer's plan. The
    # setter REFUSES a write whose effective date is strictly older than the last applied. A DURABLE delivery must
    # carry the validated timestamp retained by the v2 sanitizer; otherwise retry/fail closed instead of making the
    # high-water guard inert. Unkeyed direct/legacy seams retain their historical compatibility behavior.
    effective_at = _marketplace_effective_date(payload)
    if _delivery_of(payload) and effective_at is None:
        raise RuntimeError("durable marketplace delivery missing valid effective_date")
    account = db("SELECT core.set_account_plan_with_authority(%s,%s,%s)", (str(gh_account_id), plan, effective_at))
    print(f"{_tp}marketplace_purchase {action} gh_account={gh_account_id} → plan={plan} "
          f"effective_at={effective_at} account={account}", flush=True)
    return {"event": "marketplace_purchase", "action": action, "account": account, "plan": plan}


def handle_event(
        event_type: str, payload: dict, db, gh, coalesce=None, *,
        planned_onboarding_graph_proof=None) -> dict:
    """Route one verified webhook to the brain + post the result. PURE over (db, gh).

    THIN DISPATCH TABLE: the top-level poison-event coercion + per-installation `gh` rebind + the suspend-window
    quiesce gate are the shared PREAMBLE every event passes through; below it, each event TYPE routes to its
    cohesive `_handle_<event>` helper (each lifted VERBATIM, so the dispatch + every return dict are byte-
    identical to the old inline router). An UNHANDLED event type — and any handler's unhandled action — returns
    the same honest `noop` tail. The monkeypatch seams the tests rebind (`apply_pause_ack` / `_optional` / the
    per-event handlers) and the live-`server` caps (read via _server()) are MODULE GLOBALS the helpers reach by
    name, so every existing rebind keeps taking effect."""
    # POISON-EVENT TOLERANCE (top-level container): every NESTED field below is already coerced with
    # _as_obj/_as_list, but the payload ITSELF is attacker-controlled and arrives straight from
    # json.loads(body) — and valid JSON `null`/`[...]`/`"x"`/`42` parses to None/list/str/int, NOT a dict.
    # Those pass do_POST's `except ValueError` guard untouched, get enqueued, and the very first
    # payload.get(...) here would raise AttributeError that ESCAPES the handler — churning the worker's
    # 'failed' counter and (on the live processor) holding a per-repo advisory lock until connection-close,
    # for what must be a clean no-op. Coerce once at the door: a real dict passes through unchanged
    # (idempotent), any other shape becomes {} → falls through to the honest `noop` return at the bottom.
    payload = _as_obj(payload)
    # PER-EVENT TRACE-ID (Round-2 observability follow-up): mint an opaque uuid4 trace_id and stash it on the
    # payload at handle_event entry so every downstream log line in the webhook → ingest → render pipeline can
    # tag itself with the SAME id. An on-call then greps one event's full trail end-to-end. Content-free (random
    # bytes, never derived from any payload field / PII / sha). Idempotent: re-entry on a payload that ALREADY
    # carries a trace_id (e.g. an inline re-dispatch) is a no-op. The X-GitHub-Delivery header correlates ACROSS
    # delivery attempts; the trace_id correlates a SINGLE in-process attempt's logs (a fresh attempt — a redeliv,
    # a recovery replay, a re-run replay — mints its own, by design: each attempt's logs grep cleanly on their own).
    _trace_id = _ensure_trace_id(payload)
    _tp = _trace_log_prefix(payload)  # parseable 'trace_id=<12hex> ' prefix for downstream prints; '' when absent
    installation_id = _event_installation_id(payload)
    if installation_id is not None and hasattr(gh, "for_installation"):
        gh = gh.for_installation(str(installation_id))

    # SUSPEND WINDOW QUIESCE (the leak the suspend handler below can't close on its own): GitHub normally STOPS
    # delivering webhooks to a suspended install, but that masking does NOT cover at-least-once REDELIVERY of a
    # queued event nor the rolling-deploy 2-instance overlap during the suspend transition — either can still hand
    # this worker a WORK-bearing event (a pull_request / push / check_suite) for an account that is currently
    # suspended. Such an event must NOT mutate as if live (acquire a lane, post a check) — Veripsa records nothing
    # for a suspended install. GitHub stamps every event delivered while suspended with installation.suspended_at,
    # so we read that content-free flag and SKIP work-bearing events. The lifecycle events themselves are NEVER
    # skipped here: installation / installation_repositories carry their own correct handling (suspend RELEASES
    # lanes — and its payload also carries suspended_at, so a top-level skip would short-circuit the very release;
    # unsuspend RE-ONBOARDS; deleted PURGES), so they fall through to their explicit branches below. Advisory +
    # fail-safe: a malformed payload has no suspended_at → not skipped (the normal path's own guards still apply).
    if (_install_is_suspended(payload)
            and event_type not in ("installation", "installation_repositories")):
        return {"event": event_type, "action": payload.get("action"),
                "skipped": "installation suspended", "suspended": True}

    # INSTALLATION-LIFECYCLE tier (installation create/suspend/unsuspend/delete + installation_repositories
    # add/remove). Routed to a cohesive helper; the helper's UNHANDLED-action fall-through returns the same
    # honest `noop` this dispatcher's tail would (so any non-handled action is byte-identical to before).
    if event_type in ("installation", "installation_repositories"):
        return _handle_installation_event(event_type, payload, db, gh)

    # Deletion creates the revocation marker; creation explicitly clears it. Other repository actions and every
    # work-bearing event first pass the same repo-liveness guard, so an unordered old delivery cannot resurrect
    # working state after access was removed.
    if event_type == "repository" and payload.get("action") in ("deleted", "created"):
        return _handle_repository_event(event_type, payload, db, gh)

    if not _repository_event_allowed(payload, db):
        return {"event": event_type, "action": payload.get("action"),
                "skipped": "repository removed or deleted", "repository_revoked": True}

    # REPOSITORY-LIFECYCLE tier (renamed/transferred/archived after the liveness guard).
    if event_type == "repository":
        return _handle_repository_event(event_type, payload, db, gh)

    if event_type == "pull_request":
        # PULL_REQUEST tier. The early guard / early-exit block (malformed payload, off-protected-branch
        # retarget-release) is _pr_guard: it returns a short-circuit result dict to return as-is,
        # or None to continue into the full analyze+post body (_handle_pull_request_event). Same dispatch +
        # same return dicts as the old inline body — just split for cohesion.
        guard = _pr_guard(payload, db, gh)
        if guard is not None:
            return guard
        return _handle_pull_request_event(
            event_type, payload, db, gh,
            planned_onboarding_graph_proof=planned_onboarding_graph_proof)

    # PUSH tier (ingest main / reserve feature-branch lanes / branch-delete no-op). coalesce threads through to
    # ingest_push exactly as before; same dispatch + same return dicts.
    if event_type == "push":
        return _handle_push_event(payload, db, gh, coalesce)

    # CHECK FAILED / RE-RUN tier (check_suite / check_run): a re-run replays each in-window PR; a `completed`
    # failure records the stuck-PR fact. REQUIRES the App registration to subscribe to the check_suite +
    # check_run events (a registration setting, not code — see the module header + RUNBOOK); without that
    # subscription GitHub never delivers these and this is inert.
    if event_type in ("check_suite", "check_run"):
        return _handle_check_event(event_type, payload, db, gh)

    # MERGE QUEUE tier (merge_group): ALWAYS report an advisory status on the batch commit so a REQUIRED Veripsa
    # check can never deadlock the queue, AND re-analyze the batched changes so the batch never lands
    # UN-analyzed. REQUIRES the App registration to subscribe to `merge_group` (a registration setting — see
    # app-manifest.json default_events + the RUNBOOK); without it GitHub never delivers these and this is inert.
    if event_type == "merge_group":
        return _handle_merge_group_event(payload, db, gh)

    # MARKETPLACE BILLING tier (marketplace_purchase): default-off unless an operator explicitly opts in with
    # VERIPSA_MARKETPLACE_BILLING=1.
    if event_type == "marketplace_purchase":
        if not _marketplace_billing_enabled():
            print(f"{_tp}marketplace_purchase IGNORED action="
                  f"{_as_obj(payload).get('action')!r}: VERIPSA_MARKETPLACE_BILLING is off; no plan written",
                  flush=True)
            return {"event": "marketplace_purchase", "noop": True,
                    "skipped": "marketplace billing disabled (VERIPSA_MARKETPLACE_BILLING off)"}
        return _handle_marketplace_event(payload, db, gh)

    return {"event": event_type, "noop": True}
