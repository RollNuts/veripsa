#!/usr/bin/env python3
"""Veripsa GitHub App — the PR-WRITE-SURFACE of the GitHub REST client (checks · comments · labels).

Split OUT of github_rest.py's ~40-method GitHubREST god-class (Veripsa's own split_candidates flags it a
structural hotspot): the methods that WRITE Veripsa's output onto a PR — the check run, the verdict comment,
the veripsa-ack label — are a cohesive group that depends ONLY on the base client's `self._api`. They live
here as a mixin; GitHubREST inherits them. Same finer-files=finer-collision-point leaf discipline as
render_safe / render_bound / render_pauseack. Behaviour-preserving: callers use GitHubREST.<method> unchanged
(inherited); a test patching GitHubREST._urlopen/_sleep still works — those stay on the base and resolve via
MRO no matter which class defines the calling method. Content-free (paths/ids/labels only, never a body)."""
from __future__ import annotations

import inspect
import re

try:
    from check_delivery_observer import note_check_delivery_outcome
except ImportError:  # imported as a package
    from .check_delivery_observer import note_check_delivery_outcome


class _GitHubPRSurfaceMixin:
    """Checks · comments · labels writes. The host class MUST provide `self._api` (the authenticated REST
    call) — GitHubREST does. This class is never instantiated on its own."""

    @staticmethod
    def _check_details_url(repo: str, sha: str, pr_number: int | None = None,
                           comment_id: int | None = None) -> str:
        """The check's "Details" link, set EXPLICITLY so GitHub does NOT fall back to the App's REGISTERED
        default URL (a single URL baked into the App registration — which would send EVERY customer's check
        to that one repo, not theirs). PO #3 (2026-06-25): the original target — the customer's own commit
        page (https://github.com/{repo}/commit/{sha}) — is a WEAK link to decision material; the reader wants
        the Veripsa VERDICT itself. Fallback chain (most specific → least): the comment anchor on the PR
        conversation → the PR conversation → the commit (back-compat). Content-free (an issue/PR number +
        a comment id, never a body). No specific repo is hardcoded; everything is derived from the event."""
        if pr_number is not None and comment_id is not None:
            # ISSUE-COMMENT anchor: lands the reader on the Veripsa verdict prose with the reasoning visible.
            return f"https://github.com/{repo}/issues/{pr_number}#issuecomment-{comment_id}"
        if pr_number is not None:
            # PR CONVERSATION: still much better than the bare commit — the Veripsa comment is in view.
            return f"https://github.com/{repo}/pull/{pr_number}"
        return f"https://github.com/{repo}/commit/{sha}"

    @staticmethod
    def _details_url_pr_number(repo: str, details_url: str | None) -> int | None:
        """Extract the PR/issue number from a Veripsa-owned details_url, if it is PR-specific.

        GitHub check runs attach to a commit SHA, not to a PR. Two PRs can point at the same SHA
        (duplicate branch, no-op retry PR, or bot-created duplicate). In that case one check run can appear on
        both PRs. A PR-specific details_url for PR #A is actively misleading when the same check appears on
        PR #B, so upsert_check uses this parser to detect cross-PR reuse and fall back to the commit URL.
        """
        if not isinstance(details_url, str) or not details_url:
            return None
        escaped_repo = re.escape(repo)
        m = re.match(rf"^https://github\.com/{escaped_repo}/(?:pull|issues)/(\d+)(?:[#/?].*)?$", details_url)
        if not m:
            return None
        try:
            return int(m.group(1))
        except ValueError:
            return None

    def _check_details_url_for_upsert(self, repo: str, sha: str, pr_number: int | None = None,
                                      comment_id: int | None = None,
                                      existing_details_url: str | None = None) -> str:
        """Best details_url for this upsert, with a no-wrong-PR guard for shared commit checks."""
        desired = self._check_details_url(repo, sha, pr_number, comment_id)
        existing_pr = self._details_url_pr_number(repo, existing_details_url)
        if pr_number is not None and existing_pr is not None and existing_pr != pr_number:
            return self._check_details_url(repo, sha)
        return desired

    def post_check(self, repo: str, sha: str, conclusion: str, title: str, summary: str,
                   pr_number: int | None = None, comment_id: int | None = None):
        return self._api("POST", f"/repos/{repo}/check-runs",
                         {"name": "Veripsa", "head_sha": sha, "status": "completed", "conclusion": conclusion,
                          "details_url": self._check_details_url(repo, sha, pr_number, comment_id),
                          "output": {"title": title, "summary": summary}})

    def list_check_runs(self, repo: str, sha: str) -> list[dict]:
        r = self._api("GET", f"/repos/{repo}/commits/{sha}/check-runs?check_name=Veripsa")
        return r.get("check_runs", [])

    def patch_check(self, repo: str, check_run_id: int, conclusion: str, title: str, summary: str,
                    details_url: str | None = None):
        body = {"name": "Veripsa", "status": "completed", "conclusion": conclusion,
                "output": {"title": title, "summary": summary}}
        if details_url:                          # keep the Details link on the customer's own repo (see post_check)
            body["details_url"] = details_url
        return self._api("PATCH", f"/repos/{repo}/check-runs/{check_run_id}", body)

    def upsert_check(self, repo: str, sha: str, conclusion: str, title: str, summary: str,
                     pr_number: int | None = None, comment_id: int | None = None):
        expected_app_id = str(getattr(self, "app_id", "") or "")
        existing = []
        for run in self.list_check_runs(repo, sha):
            if run.get("name") != "Veripsa":
                continue
            # Production clients always know their numeric App id. Require the existing Check Run to be owned by
            # that App before it can authorize a no-op or be patched. This prevents another App using the same
            # display name from producing a false `check_noop` receipt. Older test doubles without app_id keep the
            # historical exact-name fallback; the live GitHubREST path never takes it.
            if expected_app_id:
                app = run.get("app") if isinstance(run.get("app"), dict) else {}
                if str(app.get("id") or "") != expected_app_id:
                    continue
            existing.append(run)
        if existing:
            # Check runs are commit-scoped. If this SHA already has a Veripsa check whose Details link points at
            # a DIFFERENT PR, a PR-specific link would be wrong for at least one PR sharing that SHA. Prefer the
            # neutral commit URL over sending a reader to another PR's conversation.
            # NO-CHURN: a neighbor refresh re-renders EVERY in-flight PR's check on every cluster event, even
            # when this PR's verdict did NOT move. A PATCH with the identical conclusion/title/summary is a
            # no-op for the reader but still spends an API call (and a PATCH that the renderer happens to
            # change re-stamps the check). So SKIP the PATCH when the rendered check AND details_url are
            # byte-identical to what is already posted — re-post a neighbor ONLY when its content or link target
            # actually changed. Idempotent + bounded.
            cur = existing[0]
            out = cur.get("output") or {}
            details_url = self._check_details_url_for_upsert(
                repo, sha, pr_number, comment_id, existing_details_url=cur.get("details_url"))
            if (cur.get("conclusion") == conclusion and (out.get("title") or "") == (title or "")
                    and (out.get("summary") or "") == (summary or "")
                    and (cur.get("details_url") or "") == details_url):
                note_check_delivery_outcome(repo, sha, "noop")
                return cur                       # unchanged → touch nothing (no API write, no churn)
            result = self.patch_check(repo, cur["id"], conclusion, title, summary, details_url=details_url)
            note_check_delivery_outcome(repo, sha, "updated")
            return result
        result = self.post_check(repo, sha, conclusion, title, summary, pr_number, comment_id)
        note_check_delivery_outcome(repo, sha, "updated")
        return result

    def post_comment(self, repo: str, number: int, body: str):
        return self._api("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def list_issue_comments(self, repo: str, number: int) -> list[dict]:
        out, page = [], 1
        while True:
            r = self._api("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100&page={page}")
            out += r
            if len(r) < 100:
                break
            page += 1
        return out

    def patch_comment(self, repo: str, comment_id: int, body: str):
        return self._api("PATCH", f"/repos/{repo}/issues/comments/{comment_id}", {"body": body})

    def pr_labels(self, repo: str, number: int, strict: bool = False) -> list[str]:
        """PAUSE-ACK: the CURRENT label NAMES on a PR (used by the NEIGHBOR refresh to tell whether a material
        neighbor's coupling has been acknowledged — the live pull_request event reads them off its own payload,
        but a refresh has no payload). Content-free (label names we chose / the customer set, never code).

        PULLS SURFACE ONLY (live outage #848, 2026-07-17): this read MUST go through GET /repos/{repo}/pulls/{n}
        (the PR object carries labels[]), NEVER GET /repos/{repo}/issues/{n}. The App's registration has NO
        `issues` permission (pull_requests/checks/contents/metadata only), and "Get an issue" is issues-scoped —
        the old issue-GET here 403'd on EVERY repo, so the ACK overlay fail-opened everywhere and the whole
        pause→acknowledge tier was silently dead ("pause-ack overlay skipped …: HTTP Error 403: Forbidden").
        The comment endpoints (list/post/patch under /issues/{n}/comments) are dual-scoped and keep working under
        pull_requests — the issue GET is the one issues-only read, so it is the one that moves. Do NOT "fix" a
        recurrence by adding an issues permission (Marketplace re-review); keep reads on the pulls surface.

        strict=False (default): returns [] on any error (best-effort, never crashes).
        strict=True (the NEIGHBOR pause-ack overlay): RAISE on a read error instead of masking it as []. A masked-[]
        was the wrong fail direction for ACK STICKINESS — an unreadable label set is "I don't know", NOT "the ack
        label is absent", and treating it as absent would re-raise a paused (action_required) check on an ALREADY-
        ACKNOWLEDGED neighbor on every refresh (the ack would not stick). With strict=True the caller's try/except
        FAIL-OPENs instead, leaving the neighbor's plain advisory `neutral` verdict — it never strips a label and
        never re-raises a pause it cannot justify; the next real event re-derives the correct state."""
        try:
            pr = self._api("GET", f"/repos/{repo}/pulls/{number}")
            return [lab.get("name") for lab in (pr.get("labels") or []) if isinstance(lab, dict) and lab.get("name")]
        except Exception:
            if strict:
                raise
            return []

    def remove_label(self, repo: str, number: int, label: str) -> bool:
        """PAUSE-ACK: take ONE label off a PR (the `veripsa-ack` label, when a STALE ack must read un-acked
        because the coupling changed under it). Idempotent + best-effort: GitHub 404s when the label is already
        absent — that is success for our purpose (the desired end-state holds), so swallow it and return False
        (nothing removed) rather than raise. Any other error propagates to the caller's never-crash guard.
        Content-free (a label name we chose, never customer data). GitHub exposes PR comments and labels through
        issue-backed REST endpoints; this uses the App's pull_requests surface and does not imply an `issues` or
        `issue_comment` webhook subscription."""
        import urllib.parse
        import urllib.error
        try:
            self._api("DELETE", f"/repos/{repo}/issues/{number}/labels/{urllib.parse.quote(label)}")
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:                              # already absent → desired state already holds (no loop)
                return False
            raise

    def upsert_comment(self, repo: str, number: int, marker: str, body: str):
        legacy = []
        for comment in self.list_issue_comments(repo, number):
            existing = comment.get("body") or ""
            user = comment.get("user") or {}
            if marker in existing:
                # NO-CHURN: the neighbor refresh re-renders every in-flight PR's verdict comment on each cluster
                # event. A PATCH that rewrites the SAME body is invisible to a reader yet still bumps the
                # comment's updated_at (re-surfacing it / re-notifying subscribers) and spends an API call. So
                # re-post ONLY when the rendered body actually changed; an unchanged verdict touches nothing.
                if existing == body:
                    return comment                # identical body → no PATCH (no spam, no API write)
                return self.patch_comment(repo, comment["id"], body)
            if existing.startswith("### Veripsa") and user.get("type") == "Bot":
                legacy.append(comment)
        if len(legacy) == 1:
            if (legacy[0].get("body") or "") == body:
                return legacy[0]                  # unchanged legacy comment → no PATCH
            return self.patch_comment(repo, legacy[0]["id"], body)
        return self.post_comment(repo, number, body)

    def patch_comment_if_exists(self, repo: str, number: int, marker: str, body) -> bool:
        """PATCH the Veripsa marker comment IF it already exists — but NEVER create one. Used by the clear-reset
        refresh: when a neighbor PR that PREVIOUSLY had a warn/serialize verdict drops back to 'clear' (its
        blocker withdrew/landed, the coupling cleared), its stale verdict comment must be corrected — but a PR
        that was ALWAYS clear has no marker comment and must stay comment-free (the less-noise rule). `body` may
        be a string OR a zero-arg callable (so the caller can DEFER building the body — e.g. a default-branch
        fetch — until we KNOW a comment exists, keeping an always-clear PR free of any extra API cost). A callable
        may accept zero arguments (historical seam) or one ``existing_body`` argument, which lets a clear rewrite
        retain invisible state such as the ACK snapshot without a second comment-list read. Returns True iff an
        existing marker comment was patched."""
        for comment in self.list_issue_comments(repo, number):
            existing = comment.get("body") or ""
            if marker in existing:
                if callable(body):
                    try:
                        params = inspect.signature(body).parameters.values()
                        accepts_existing = any(
                            p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                       inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                       inspect.Parameter.VAR_POSITIONAL)
                            for p in params)
                    except (TypeError, ValueError):
                        accepts_existing = False
                    new_body = body(existing) if accepts_existing else body()
                else:
                    new_body = body
                # NO-CHURN: if the clear-reset body equals what is already there, skip the PATCH (no updated_at
                # bump, no re-notify). Still returns True — a marker comment exists, so the caller may reset the
                # check; upsert_check there is itself a no-op when the check is already green.
                if existing != new_body:
                    self.patch_comment(repo, comment["id"], new_body)
                return True
        return False
