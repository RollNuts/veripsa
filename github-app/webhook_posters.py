#!/usr/bin/env python3
"""Veripsa GitHub App — the webhook brain's GITHUB-POST helpers (never-crash check/comment posters).

The cohesive cluster of webhook_handlers.py that POSTS to GitHub but is PURE of the patchable pause-ack / live-
`server`-module seam: the never-abort check upsert (_safe_upsert_check), the one-fetch head+fork resolver
(_head_and_fork), the best-effort default-branch reader (_default_branch_for), the prior-ack-snapshot readback
(_prior_ack_snapshot), and the silent-install 'now watching' signal (_post_watching_signal). Each takes the `gh`
client + content-free arguments and degrades gracefully (a post failure for one repo/PR is logged + skipped, never
raised) — but NONE of them reads the live `server` module's caps or the monkeypatch-bound `_optional` /
`apply_pause_ack` overlay seam (those stay with the per-event handlers + _post_refreshes in webhook_handlers.py, so
the tests that rebind `webhook_handlers._optional` / `webhook_handlers.apply_pause_ack` keep reaching the bindings
those overlays actually call).

WHY ITS OWN MODULE: these are the byte-for-byte GitHub-post primitives several event paths share. Splitting them out
shrinks webhook_handlers.py to the dispatch + per-event orchestration it owns, mirroring the github_rest.py surface
split. Import direction is one-directional (webhook_handlers ← webhook_posters ← webhook_coercion): this module
imports the comment-marker builder from webhook_coercion + the render leaves it needs, and NOTHING from
webhook_handlers.py — no cycle. webhook_handlers.py RE-EXPORTS every name below so `webhook_handlers.X` (and, via
server.py's own re-export, `server.X`) keeps resolving for the gates + the tests that reach these by name.
"""
from __future__ import annotations

try:
    from webhook_coercion import _comment_marker
except ImportError:  # imported as a package
    from .webhook_coercion import _comment_marker
try:
    from render import watching_check, prior_snapshot_from_comment
except ImportError:  # imported as a package
    from .render import watching_check, prior_snapshot_from_comment


def _check_result_from_response(resp, *, posted: bool, error: str = "") -> dict:
    """Normalize a GitHub check-run API response into content-free operator metadata.

    Some older test doubles return None on success, while production GitHubREST returns the check-run object from
    GitHub. Keep both valid: `posted` is the authoritative boolean, ids/URLs are opportunistic observability."""
    run = resp if isinstance(resp, dict) else {}
    return {
        "posted": bool(posted),
        "check_run_id": run.get("id"),
        "check_run_url": run.get("html_url") or run.get("url") or "",
        "details_url": run.get("details_url") or "",
        "error": error,
    }


def _upsert_check_result(gh, repo: str, sha, conclusion: str, title: str, summary: str, is_fork: bool = False,
                         *, pr_number: int | None = None, comment_id: int | None = None) -> dict:
    """Post/update the Veripsa check and return metadata for operator correlation.

    This is the structured sibling of _safe_upsert_check. It preserves the same never-crash / old-mock fallback
    behavior, but keeps Check Run id/URL/details_url when GitHub returns them."""
    try:
        if pr_number is not None or comment_id is not None:
            try:
                resp = gh.upsert_check(repo, sha, conclusion, title, summary,
                                       pr_number=pr_number, comment_id=comment_id)
            except TypeError as te:                         # mock without the new kwargs → fall back (back-compat)
                if "unexpected keyword argument" in str(te):
                    resp = gh.upsert_check(repo, sha, conclusion, title, summary)
                else:
                    raise
        else:
            resp = gh.upsert_check(repo, sha, conclusion, title, summary)
        return _check_result_from_response(resp, posted=True)
    except Exception as e:                                  # fork-head sha not in base repo / transient API error
        err = str(e)[:160]
        print(f"check post skipped repo={repo} sha={str(sha)[:7]} fork={is_fork}: {err}", flush=True)
        return _check_result_from_response(None, posted=False, error=err)


def _safe_upsert_check(gh, repo: str, sha, conclusion: str, title: str, summary: str, is_fork: bool = False,
                       *, pr_number: int | None = None, comment_id: int | None = None) -> bool:
    """Post (or update) the Veripsa check, but NEVER let a check-post failure abort the whole event. A check
    run can legitimately fail for a FORK PR (GitHub won't create a check on the base repo for a commit that
    lives only in the contributor's fork) or a transient GitHub error. In every such case the PR COMMENT
    (posted separately, always on the base repo's conversation) still carries the full serialize/warn signal —
    so we degrade to comment-only instead of dropping the event. Returns True if the check posted.

    pr_number / comment_id (PO #3 details_url, 2026-06-25): when set, the check's "Details" link points at the
    PR conversation (or the Veripsa verdict comment anchor, when comment_id is known) instead of the bare
    commit — a much stronger path to decision material. Back-compat: a gh client (test mock) that doesn't yet
    accept these kwargs is detected via TypeError on "unexpected keyword argument" and called the old way."""
    return bool(_upsert_check_result(gh, repo, sha, conclusion, title, summary, is_fork,
                                     pr_number=pr_number, comment_id=comment_id).get("posted"))


def _head_and_fork(gh, repo: str, number: int):
    """(head_sha, confirmed_fork, redact_external) for a PR, in one fetch where supported."""
    pf = getattr(gh, "pull_request_head_and_fork_privacy", None)
    if callable(pf):
        return pf(repo, number)
    f = getattr(gh, "pull_request_head_and_fork", None)
    if callable(f):
        head_sha, is_fork = f(repo, number)
        return head_sha, is_fork, is_fork
    return gh.pull_request_head(repo, number), False, False


def _prior_ack_snapshot(gh, repo: str, pr_number: int) -> tuple[str | None, bool]:
    """PAUSE-ACK: (prior_hash, read_confirmed) for the coupling-snapshot hash embedded in Veripsa's PREVIOUS
    comment on this PR. Stateless ack readback — an ack is valid only if it was given FOR the coupling now in
    flight, so apply_pause_ack compares this prior hash to the CURRENT snapshot. Reuses the same issue-comment
    list the upsert path uses (the App's own marker comment), reads back the invisible `<!-- veripsa-ack-snap:… -->`
    marker.

    The SECOND value is whether the READ SUCCEEDED — load-bearing for ack-stickiness. A None prior hash is
    AMBIGUOUS: it can mean "there is genuinely no prior Veripsa comment yet" (read OK, marker absent → confirmed
    None → the ack has nothing to bind to → pause) OR "the comment read FAILED this event" (a transient list error
    / GitHub eventual-consistency → UNconfirmed None → must NOT strip a valid ack). We return confirmed=True only
    when the list read actually completed; on a read error we return (None, False) so the caller passes
    prior_confirmed=False and apply_pause_ack KEEPS the ack instead of stripping it on a non-proof."""
    try:
        marker = _comment_marker(pr_number)
        for comment in gh.list_issue_comments(repo, pr_number):
            body = comment.get("body") or ""
            if marker in body:
                return prior_snapshot_from_comment(body), True
        return None, True                                   # read OK, no marker → genuinely no prior ack to bind to
    except Exception as e:
        print(f"prior ack-snapshot read skipped repo={repo} pr={pr_number}: {str(e)[:120]}", flush=True)
        return None, False                                  # read FAILED → UNconfirmed → fail-safe: do not strip a valid ack


def _post_watching_signal(gh, onboarded: list) -> int:
    """THE SILENT-INSTALL FIX. On a fresh install the graph backfills, but a repo with NO open PRs posts NOTHING
    GitHub-visible — the user grants code-read and sees total silence (looks broken). So after onboarding, emit
    ONE content-free 'Veripsa is now watching' check-run on each onboarded repo's DEFAULT-BRANCH HEAD: "I indexed
    N files / M links and I'll flag pre-merge overlap on your next PR." Now 'I installed it' becomes 'I can see
    it's alive.'

    CHANNEL: a Checks-API check-run (conclusion `neutral`) on the default-branch HEAD sha. It needs only the
    EXISTING scopes (Checks:Write) — no new permission. It is ADVISORY: the default branch is not a merge target,
    so this check can never gate a merge. Counts only (files/edges) — never a path/symbol/body (content-free).

    IDEMPOTENT + BOUNDED: upsert_check PATCHES the existing Veripsa check on the same HEAD sha when re-delivered /
    re-installed (no second post — the no-double-post invariant), and we post exactly ONCE per onboarded repo
    (never per event). A repo with NO head sha (a brand-new empty repo) has nothing to anchor a check on → skip it;
    the check posts on the first real push (ingest_push detects cold_start=True and the push handler signals then).
    A deferred (over-onboard-cap) repo is also not signalled here; same cold-start path. An over-FILE-cap repo
    (over_cap=True) IS signalled with the honest over-cap copy (no false 'will complete' promise).
    NEVER-CRASH: a post failure for one repo is logged and skipped (it must not abort the org install)."""
    posted = 0
    for r in onboarded or []:
        if not isinstance(r, dict):
            continue
        repo = r.get("backfilled")
        head_sha = r.get("head_sha")
        if not (repo and head_sha):       # no repo name / no HEAD (empty repo) → nothing to anchor a check on
            continue
        graph = r.get("graph") if isinstance(r.get("graph"), dict) else {}
        # over-cap → the repo genuinely exceeds the file-count threshold; the over-cap-honest copy is used (no
        # false 'will complete' promise).  Non-over-cap install-time signal uses real file/edge counts.
        _over_cap = bool(graph.get("over_cap"))
        chk = watching_check(files=int(graph.get("files") or 0), edges=int(graph.get("edges") or 0),
                             branch=r.get("default_branch") or "main",
                             indexing=bool(graph.get("indexing")) and not _over_cap,
                             over_cap=_over_cap)
        if _safe_upsert_check(gh, repo, head_sha, chk["conclusion"], chk["title"], chk["summary"]):
            posted += 1
    return posted


def _default_branch_for(gh, repo: str) -> str:
    """Best-effort default branch for the cleared-comment header; falls back to 'main' if unresolvable."""
    try:
        return gh.repo_default_branch_head(repo)[0] or "main"
    except Exception:
        return "main"
