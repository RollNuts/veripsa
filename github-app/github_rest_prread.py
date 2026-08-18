#!/usr/bin/env python3
"""Veripsa GitHub App — the PR-READ surface of the GitHub REST client.

Split OUT of github_rest.py's ~30-method GitHubREST god-class (Veripsa's own split_candidates flags it a
structural hotspot): the methods that READ pull-request and diff data — list_open_pull_requests,
get_pull_request, pull_request_head/head_and_fork, list_pr_files / list_pr_files_with_ranges,
base_blob_shas, compare_changed_paths — are a cohesive group that depends ONLY on the base client's
`self._api`. They live here as a mixin; GitHubREST inherits them. Same finer-files=finer-collision-point
leaf discipline as github_rest_prsurface / github_rest_contentfetch. Behaviour-preserving: callers use
GitHubREST.<method> unchanged (inherited); a test patching GitHubREST._urlopen/_sleep still works — those
stay on the base and resolve via MRO. Content-free (paths/ids/line-numbers only, never code bodies).

Shared module-level helpers note:
  • `changed_line_ranges_from_patch`, `_HUNK_RE`, `_MAX_HUNKS` — imported by 4 tests directly from github_rest
    (e.g. `from github_rest import changed_line_ranges_from_patch`), so they STAY in github_rest.py. The one
    method here that needs it (list_pr_files_with_ranges) resolves it via the same lazy-import seam that
    github_rest_contentfetch uses for _read_capped / _verify_complete_gzip, avoiding the circular import.
  • `_rename_source` — a @staticmethod used ONLY by list_pr_files / list_pr_files_with_ranges; no test imports
    it directly (the test_rename_lane.py reference is in a module docstring comment, not an import). Moved here."""
from __future__ import annotations
import re as _re
import urllib.parse as _urlparse


class PRFilesShortfall(Exception):
    """Raised by list_pr_files / list_pr_files_with_ranges when the total number of file entries returned by the
    Files API is less than the PR's own `changed_files` declaration (a provable partial read — the pagination loop
    terminated before all pages were delivered, most likely because a proxy/CDN edge served a SHORT but HTTP-200
    intermediate page that looked like the final page under the old `len(page) < 100` heuristic). This exception
    signals an HONEST-UNKNOWN state: the caller (webhook_handlers) MUST withhold the verdict rather than proceeding
    to a reconcile that would release lanes — exactly mirroring the `suspect_empty_files` path for a fully-empty
    read. Content-free (counts only, no paths in the message)."""
    def __init__(self, returned: int, declared: int):
        super().__init__(f"PR files partial read: returned {returned} file entries but PR declares {declared} changed_files")
        self.returned = returned
        self.declared = declared


class PRFilesPageBudgetExceeded(Exception):
    """Raised before Files pagination would request a page beyond the caller's explicit HTTP-page budget.

    Unlike PRFilesShortfall this is a cost-bound signal, not evidence about completeness. Callers must preserve
    the last authoritative verdict and retry on a later PR event/backfill. Content-free (page counts only).
    """
    def __init__(self, used: int, budget: int):
        super().__init__(f"PR files page budget exhausted after {used} of {budget} allowed pages")
        self.used = used
        self.budget = budget


class PRFilesMalformed(Exception):
    """Raised when a Files API entry cannot provide the path identity required for complete lane evidence."""


def _gh_prread_helpers():
    """Lazy import of module-level helpers that live in github_rest.py. Called at use-time rather than
    module-load-time to avoid the mutual import cycle: github_rest imports this module, so a top-level
    `from github_rest import …` here would cause a circular import error on Python's first pass."""
    try:
        import github_rest as _gr
    except ImportError:
        from . import github_rest as _gr
    return _gr.changed_line_ranges_from_patch


def _gh_conflict_helpers():
    """Lazy import of the conflict-marker scanner helper (same circular-import dance as _gh_prread_helpers)."""
    try:
        import github_rest as _gr
    except ImportError:
        from . import github_rest as _gr
    return _gr.conflict_markers_from_patch


class _GitHubPRReadMixin:
    """PR and diff READ surface: list/get pull requests · list files with ranges · base blob shas ·
    compare changed paths. The host class MUST provide `self._api` (the authenticated REST call) —
    GitHubREST does. This class is never instantiated on its own."""

    @staticmethod
    def _has_link_next(link_header: str) -> bool:
        """True when the `Link` response header contains a `rel="next"` relation — the authoritative GitHub signal
        that another page exists. Parses the RFC 5988 comma-separated list: each entry is `<url>; rel="type"` or
        `<url>; rel="type1 type2"`. We accept both `rel="next"` (quoted) and `rel=next` (bare). A missing, empty,
        or malformed header is treated as 'no next page' (safe — it can only shorten pagination, never skip pages).
        Content-free: only the relation-type keyword is inspected, never the URL body."""
        if not link_header:
            return False
        # Match `; rel="next"` or `; rel=next` (case-insensitive, with optional whitespace).
        return bool(_re.search(r';\s*rel=["\']?next["\']?', link_header, _re.IGNORECASE))

    def _page_pr_files_raw(self, repo: str, number: int):
        """Paginate GET /repos/{repo}/pulls/{number}/files with Link-header termination (primary) and len<100
        heuristic as a fallback for clients that don't expose _api_with_link (e.g. FakeGitHub in tests).
        Yields each page as (raw_page_list, has_next_bool). Raises TypeError on a non-list response body
        (null / error envelope / malformed JSON) — preserving the fail-clean contract the external-resilience gate
        verifies: a bad response must RAISE so the worker counts the event failed and Core recovery retries."""
        page = 1
        use_link = hasattr(self, "_api_with_link")
        while True:
            url = f"/repos/{repo}/pulls/{number}/files?per_page=100&page={page}"
            if use_link:
                r, link = self._api_with_link("GET", url)
                has_next = self._has_link_next(link)
            else:
                r = self._api("GET", url)
                has_next = len(r) >= 100   # heuristic fallback for non-live clients
            if not isinstance(r, list):
                # Preserve the fail-clean contract: a null/object/malformed response must RAISE (never silently
                # return an empty list, which would be indistinguishable from a genuine 0-file PR and risk a
                # wrong-clear on a non-raising 200 — caught separately by the suspect_empty_files guard).
                raise TypeError(f"GitHub Files API returned a non-list response for {repo} PR#{number}: {type(r).__name__}")
            yield r, has_next
            if not has_next:
                break
            page += 1

    @staticmethod
    def _rename_source(f: dict) -> str | None:
        """The OLD path a RENAMED Files-API entry came FROM (`previous_filename`), or None. A rename surfaces in
        the Files API ONLY as the NEW `filename` — the OLD path is GONE from the changed-file list, so a
        concurrent PR still editing that OLD path would NOT collide with this rename (a false CLEAR: git WILL
        conflict — one side renames/deletes the file, the other modifies it). Surfacing the source path so this
        PR ALSO reserves a lane on it closes that gap. Restricted to `renamed` (the source is genuinely removed
        = a guaranteed conflict with a concurrent edit); a `copied` entry LEAVES its source in place (editing it
        concurrently does not conflict) so its source is NOT added — over-flagging there would be cry-wolf.
        Content-free: only the old path STRING crosses (the same content-free coordinate as every other path),
        never any code. Guarded against a junk entry."""
        if f.get("status") == "renamed" and isinstance(f.get("previous_filename"), str):
            prev = f["previous_filename"]
            return prev if prev else None
        return None

    def list_pr_files(self, repo: str, number: int, pr_changed_files: int = 0) -> list:
        """The list of filenames (+ rename sources) changed by PR `number`. Pagination is terminated on the
        `Link: rel="next"` header (primary — the authoritative GitHub paging signal) so a short-but-200
        intermediate page no longer ends the loop early and silently drops files (the P1 false-clear fix).

        `pr_changed_files` is the PR's own declared count (pull_request.changed_files from the webhook payload
        or get_pull_request). When provided and > 0, the total raw file entries returned are cross-checked against
        it: if returned < declared, we raise PRFilesShortfall rather than returning a partial list — the caller
        MUST withhold the verdict as honest-unknown (do NOT reconcile/release lanes on an incomplete file set).
        A perfectly small PR (returned == declared) still returns normally; a full multi-page read where the count
        happens to equal declared also returns normally. Content-free (filenames only, never code bodies)."""
        out = []
        raw_entry_count = 0            # count of raw Files-API entry objects (pre-rename-expansion)
        for r, _has_next in self._page_pr_files_raw(repo, number):
            raw_entry_count += len(r)
            for f in r:
                fn = f.get("filename")
                if isinstance(fn, str) and fn:
                    out.append(fn)
                src = self._rename_source(f)          # a RENAME's old path also coordinates (false-clear fix)
                if src:
                    out.append(src)
        # SHORTFALL BACKSTOP: if the declared count is available and the API returned fewer entries, a proxy/CDN
        # must have served a short intermediate page and terminated pagination early — a partial read. Raising
        # PRFilesShortfall lets the caller withhold the verdict rather than analyzing an incomplete file set.
        # Guard: skip the check when pr_changed_files is 0 (not provided / unavailable) — degrade safely.
        if pr_changed_files > 0 and raw_entry_count < pr_changed_files:
            raise PRFilesShortfall(raw_entry_count, pr_changed_files)
        return out

    def list_pr_files_with_ranges(self, repo: str, number: int, pr_changed_files: int = 0) -> dict:
        """{filename: [[start,end], …]} — the PR's changed files mapped to their BASE-side changed line ranges
        (the `-a,b` OLD/base-file fields of each diff HUNK HEADER — the +/- body is DISCARDED; see
        changed_line_ranges_from_patch, whose own docstring explains WHY base-side: the ranges must align with
        main's base-version symbol spans and be comparable across PRs — the audit:silent-miss launch-blocker fix.
        NOT new-side, despite an earlier version of this line saying so). CONTENT-FREE: only line numbers cross, never code. A file whose patch
        is absent (binary, too large, rename-only) maps to [] → the engine falls back to FILE-level collision
        for it (the recall safety net). A RENAMED entry ALSO contributes its OLD path (previous_filename) mapped
        to [] — the rename SOURCE has no new-side lines, so it collides at FILE level with any concurrent edit to
        that old path (closes the rename false-clear; a COPIED source is left in place by git and is NOT added —
        see _rename_source). Pagination is Link-header-terminated (same fix as list_pr_files — see module note).

        `pr_changed_files`: same shortfall backstop as list_pr_files — raises PRFilesShortfall on a provable
        partial read (returned raw entry count < declared changed_files). See list_pr_files for the full rationale."""
        changed_line_ranges_from_patch = _gh_prread_helpers()
        out = {}
        raw_entry_count = 0
        for r, _has_next in self._page_pr_files_raw(repo, number):
            raw_entry_count += len(r)
            for f in r:
                fn = f.get("filename")
                if isinstance(fn, str) and fn:
                    out[fn] = changed_line_ranges_from_patch(f.get("patch"))   # HEADERS only; body discarded here
                src = self._rename_source(f)          # rename SOURCE → [] (file-level), unless it is itself a touched path
                if src and src not in out:
                    out[src] = []
        if pr_changed_files > 0 and raw_entry_count < pr_changed_files:
            raise PRFilesShortfall(raw_entry_count, pr_changed_files)
        return out

    def list_pr_conflict_markers(self, repo: str, number: int) -> list:
        """The PR's UNRESOLVED git-merge-conflict markers.

        For each changed file, scan the diff's NEW-side ADDED lines for the unique `<<<<<<<` (ours) /
        `=======` (separator) / `>>>>>>>` (theirs) shape at line-START. Return a list of FINDINGS:
            [{"path": <str>, "line": <int new-side line_no>, "kind": "ours"|"separator"|"theirs"}, …]

        High precision: a file is reported ONLY when both the `<<<<<<<` AND `>>>>>>>` markers are present in
        the lines THIS PR ADDED (the file-level pair gate — `=======` alone is too common in markdown / rst /
        comment dividers and never fires on its own). At line-START, in ADDED lines only — so a marker inside
        an unchanged context line, in a removed line, or mid-line (a docstring discussing markers) does NOT
        fire. See conflict_markers_from_patch in github_rest.py for the full content-free contract.

        CONTENT-FREE BY CONSTRUCTION: the only thing that ever crosses is the FILE PATH (the same coordinate
        every other path-only method on this mixin returns) + a LINE NUMBER (the same metadata as the
        line-range geometry) + a kind label drawn from a fixed 3-value enum. The actual line bodies are
        read from the patch transiently and DISCARDED — never returned, stored, or logged. Same discipline as
        every existing patch-scanning helper.

        FAIL-OPEN: a transient read error or a per-file scan error returns whatever was successfully gathered
        before the error (or [] if the error hit on the first page) — this is an ADVISORY enrichment, and a
        crash here must NEVER take down the customer-facing verdict. Pagination is Link-header-terminated.
        Bounded: per-file findings are capped (see _MAX_CONFLICT_FINDINGS_PER_FILE in github_rest.py)."""
        conflict_markers_from_patch = _gh_conflict_helpers()
        out: list = []
        try:
            for r, _has_next in self._page_pr_files_raw(repo, number):
                for f in r:
                    fn = f.get("filename")
                    if not isinstance(fn, str) or not fn:
                        continue
                    patch = f.get("patch")
                    if not isinstance(patch, str) or not patch:
                        # binary file / too-large diff / no patch → cannot scan; skip (recall-safe: a marker
                        # in a binary-rendered file is essentially impossible — markers are textual by def).
                        continue
                    for finding in conflict_markers_from_patch(patch):
                        out.append({"path": fn, "line": finding["line"], "kind": finding["kind"]})
        except Exception:
            # FAIL-OPEN: advisory enrichment — a paginator error must never crash the verdict path.
            # Return whatever we got so far; a missing finding is over-clear (file-level cy-wolf safety net
            # still catches genuine collisions on the SAME path through the structural-graph engine).
            return out
        return out

    def list_pr_added_paths(self, repo: str, number: int) -> list:
        """The PR's files whose Files-API `status` is `added` — i.e. paths NEW to this PR (not present at base).
        Used by the renderer to DISTINGUISH the two semantically-different `unknown_paths` cases that the engine
        otherwise lumps together (the PO 2026-06-25 honest-verdict refinement):
          (a) NEW path in THIS PR — of course it's absent from main's graph (main does not have it). EXPECTED,
              framed as "coupling computable after merge", not a warning.
          (b) EXISTING path (modified/renamed) but NOT in main's graph — a real extractor gap (unsupported
              language, an un-indexed area, .meta/asset/etc.). Frame more cautiously.
        Lumping both as "Unknown" trains customers to ignore the verdict — case (a) is by design, case (b) is the
        real signal. Content-free: only the FILENAME crosses (the same content-free coordinate as every other
        path-only method on this mixin); the `patch` body / line numbers / status keyword are NOT returned.
        Pagination is Link-header-terminated (same as list_pr_files). FAIL-OPEN: a transient read error returns []
        so the renderer simply degrades to the legacy lumped "Unknown" copy — never crashes a verdict. Returns a
        list (not a set) for JSON-roundtrippability through the event dict."""
        out: list[str] = []
        try:
            for r, _has_next in self._page_pr_files_raw(repo, number):
                for f in r:
                    if f.get("status") != "added":
                        continue
                    fn = f.get("filename")
                    if isinstance(fn, str) and fn:
                        out.append(fn)
        except Exception:
            # FAIL-OPEN: an added-paths read is ADVISORY (it only refines the Unknown copy). A transient error
            # must NEVER crash the verdict — drop back to [] so the renderer keeps today's lumped wording.
            return []
        return out

    def list_pr_file_metadata(self, repo: str, number: int, pr_changed_files: int = 0,
                              max_pages: int | None = None) -> dict:
        """One-pass PR Files API read for the webhook hot path.

        Returns `{changed, changed_ranges, added_paths, conflict_markers, raw_entry_count}`. This is behavior-equivalent to
        calling list_pr_files_with_ranges + list_pr_added_paths + list_pr_conflict_markers, but it paginates
        GitHub's Files API ONCE instead of up to three times. The same shortfall guard applies: a provable
        partial read raises PRFilesShortfall so the caller can withhold the verdict as honest-unknown.

        `max_pages` is an optional hard HTTP ceiling for neighbor fan-out. The current page is processed, but if
        its Link header says another page exists and the ceiling has been reached, raise
        PRFilesPageBudgetExceeded BEFORE the generator can request that next page. None keeps acting-path behavior.
        """
        changed_line_ranges_from_patch = _gh_prread_helpers()
        conflict_markers_from_patch = _gh_conflict_helpers()
        changed: list[str] = []
        changed_ranges: dict[str, list] = {}
        added_paths: list[str] = []
        conflict_markers: list[dict] = []
        raw_entry_count = 0
        pages_read = 0

        if max_pages is not None and (isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1):
            raise PRFilesPageBudgetExceeded(0, 0)

        for r, _has_next in self._page_pr_files_raw(repo, number):
            pages_read += 1
            raw_entry_count += len(r)
            for f in r:
                if not isinstance(f, dict):
                    raise PRFilesMalformed("PR Files entry is not an object")
                fn = f.get("filename")
                if not isinstance(fn, str) or not fn:
                    raise PRFilesMalformed("PR Files entry has no filename")
                if f.get("status") == "renamed" and not (
                        isinstance(f.get("previous_filename"), str) and f.get("previous_filename")):
                    raise PRFilesMalformed("renamed PR Files entry has no previous_filename")

                changed.append(fn)
                patch = f.get("patch")
                changed_ranges[fn] = changed_line_ranges_from_patch(patch)
                if f.get("status") == "added":
                    added_paths.append(fn)

                if isinstance(patch, str) and patch:
                    try:
                        for finding in conflict_markers_from_patch(patch):
                            conflict_markers.append({"path": fn, "line": finding["line"], "kind": finding["kind"]})
                    except Exception:
                        # Advisory enrichment only; keep changed files/ranges intact.
                        pass

                src = self._rename_source(f)
                if src:
                    changed.append(src)
                    if src not in changed_ranges:
                        changed_ranges[src] = []

            if max_pages is not None and pages_read >= max_pages and _has_next:
                raise PRFilesPageBudgetExceeded(pages_read, max_pages)

        if pr_changed_files > 0 and raw_entry_count < pr_changed_files:
            raise PRFilesShortfall(raw_entry_count, pr_changed_files)
        return {
            "changed": changed,
            "changed_ranges": changed_ranges,
            "added_paths": added_paths,
            "conflict_markers": conflict_markers,
            # GitHub's authoritative changed_files counts API entries. `changed` can be larger because a rename
            # intentionally adds previous_filename as a second lane, so callers must compare this raw count.
            "raw_entry_count": raw_entry_count,
        }

    def compare_changed_paths_proof(self, repo: str, base_sha: str,
                                    head_sha: str) -> dict:
        """Content-free ancestry proof plus a complete-candidate path list.

        Incremental self-heal needs more authority than the advisory
        path-only compare surface: the stored commit must be the merge base and
        the response must actually terminate at the requested HEAD.  Keep the
        proof fields from GitHub's unpaginated Compare response, whose bounded
        commit list includes the comparison's most recent commit. Callers still
        reject responses beyond that proof bound and the ambiguous 300-file
        ceiling.
        """
        if not (
            isinstance(repo, str) and repo
            and isinstance(base_sha, str) and base_sha
            and isinstance(head_sha, str) and head_sha
        ):
            raise ValueError("compare proof needs repo, base sha, and head sha")
        b = _urlparse.quote(base_sha, safe="")
        h = _urlparse.quote(head_sha, safe="")
        r = self._api("GET", f"/repos/{repo}/compare/{b}...{h}")
        if not isinstance(r, dict):
            raise ValueError("malformed compare proof response")
        files = r.get("files")
        commits = r.get("commits")
        base_commit = r.get("base_commit")
        merge_base = r.get("merge_base_commit")
        status = r.get("status")
        total_commits = r.get("total_commits")
        if (
            not isinstance(files, list)
            or not isinstance(commits, list)
            or not isinstance(base_commit, dict)
            or not isinstance(merge_base, dict)
            or not isinstance(status, str)
            or isinstance(total_commits, bool)
            or not isinstance(total_commits, int)
            or total_commits < 0
        ):
            raise ValueError("malformed compare proof response")
        paths = []
        for entry in files:
            if not isinstance(entry, dict):
                raise ValueError("malformed compare proof file entry")
            filename = entry.get("filename")
            if not isinstance(filename, str) or not filename:
                raise ValueError("malformed compare proof filename")
            paths.append(filename)
        response_head_sha = None
        if status == "identical" and total_commits == 0 and not commits:
            response_head_sha = base_commit.get("sha")
        elif (
            0 < total_commits <= 250
            and len(commits) == total_commits
            and all(isinstance(commit, dict) for commit in commits)
        ):
            response_head_sha = commits[-1].get("sha")
        return {
            "paths": paths,
            "base_sha": base_commit.get("sha"),
            "merge_base_sha": merge_base.get("sha"),
            "head_sha": response_head_sha,
            "status": status,
        }

    def compare_changed_paths_strict(self, repo: str, base_sha: str, head_ref: str) -> list[str]:
        """Strict neighbor-refresh variant of compare_changed_paths.

        The acting path may fail-soft an advisory read. A neighbor overwrite cannot: treating an API failure as
        a genuine empty comparison would strip an existing stale-base nudge. This variant raises on transport or
        malformed response while preserving the same content-free path-only result.
        """
        if not (isinstance(repo, str) and repo and isinstance(base_sha, str) and base_sha
                and isinstance(head_ref, str) and head_ref):
            raise ValueError("compare needs repo, base sha, and head ref")
        import urllib.parse
        b = urllib.parse.quote(base_sha, safe="")
        h = urllib.parse.quote(head_ref, safe="")
        r = self._api("GET", f"/repos/{repo}/compare/{b}...{h}")
        if not isinstance(r, dict) or "files" not in r or not isinstance(r.get("files"), list):
            raise ValueError("malformed compare response")
        out = []
        for f in r.get("files", []):
            if not isinstance(f, dict):
                raise ValueError("malformed compare file entry")
            fn = f.get("filename")
            if not isinstance(fn, str) or not fn:
                raise ValueError("malformed compare filename")
            out.append(fn)
        return out

    def base_blob_shas(self, repo: str, base_sha: str, paths) -> dict:
        """{path: git-blob-sha} for the given CHANGED `paths` AT THE PR'S BASE commit — the FRESHNESS KEY that
        lets the gate PROVE a claim's diff line numbers were mapped against the SAME file version the graph's
        symbol spans came from (claim base hash == the graph's file content_hash → demote to symbol-level; else
        file-level fallback, recall-safe). See db/schema/80_contention.sql (freshness_ok) and code_graph_extract
        `_git_blob_sha` (the graph side computes the IDENTICAL git-blob-sha, so an equality test is meaningful).

        CONTENT-FREE BY CONSTRUCTION — the crux of the PO's condition. We read the base commit's GIT TREE
        (`GET /git/trees/{base_sha}?recursive=1`), which returns `{path, sha('blob'), type}` entries: a list of
        path→blob-sha FINGERPRINTS with **NO file bodies**. We NEVER touch the contents API / the blob-content
        API / the tarball — nothing that returns file content ever crosses for this. A blob sha is git's own
        `sha1(b"blob <len>\\0" + bytes)` over the file, byte-identical to what the extractor computes, but it is a
        fixed-width digest that cannot be inverted to the bytes. So the only thing that leaves GitHub here is a
        per-path content hash — same content-free coordinate class as a path string or a line number.

        Bounded + recall-safe degradation:
          • A CHANGED path that is ABSENT from the base tree is a NEW file (it did not exist at the base) → it
            gets NO entry → the claim carries no base hash for it → file-level fallback (correct: a brand-new
            file has no prior version to be 'fresh' against). Same for a path the tree simply doesn't list.
          • If the tree is `truncated` (GitHub caps a recursive tree at ~100k entries / 7 MB — only a giant
            monorepo hits this), we return whatever entries DID come back and leave the rest unmapped → those
            paths get no base hash → file-level fallback. We do NOT page-fetch bodies to fill the gap (that would
            break content-free AND cost N blob fetches); a missing freshness proof simply keeps the safe
            file-level collision. The caller logs the truncation honestly.
        Restricting the returned map to the small CHANGED `paths` set keeps the payload bounded even on a big
        repo whose tree has thousands of files. Returns {} when paths is empty / base_sha is absent (no work)."""
        wanted = {p for p in (paths or []) if isinstance(p, str) and p}
        if not wanted or not isinstance(base_sha, str) or not base_sha:
            return {}
        import urllib.parse
        r = self._api("GET", f"/repos/{repo}/git/trees/{urllib.parse.quote(base_sha)}?recursive=1")
        out = {}
        if not isinstance(r, dict):
            return out
        for ent in (r.get("tree") or []):
            if not isinstance(ent, dict) or ent.get("type") != "blob":   # only blobs (files); skip 'tree' (dirs)
                continue
            p, sha = ent.get("path"), ent.get("sha")
            if isinstance(p, str) and p in wanted and isinstance(sha, str) and sha:
                out[p] = sha                                   # path → its base blob sha (a fingerprint, no body)
        # r.get("truncated") True ⇒ some `wanted` paths may be unmapped above → they fall back to file-level
        # (recall-safe). We deliberately do NOT fetch bodies to resolve them (content-free + cost). The caller
        # surfaces the truncation; the unmapped paths simply keep the safe file-level collision.
        return out

    def compare_changed_paths(self, repo: str, base_sha: str, head_ref: str) -> list[str]:
        """The file PATHS that changed on `head_ref` (the protected branch's current tip) SINCE `base_sha` (the
        commit this PR branched from) — `GET /repos/{repo}/compare/{base_sha}...{head_ref}` → [f["filename"], …].
        This is the POST-MERGE STALENESS signal: while a PR sits open, OTHER PRs keep landing on the protected
        branch; compare(base...head_ref) lists exactly the files those landings touched. The stale-base nudge
        intersects this with the PR's OWN changed paths → an overlap means main moved under the PR on files it is
        editing (a rebase/merge conflict the in-flight collision detector — which only sees CONCURRENTLY-open PRs —
        cannot see, because the colliding change has ALREADY MERGED).

        CONTENT-FREE BY CONSTRUCTION: the compare API returns per-file `{filename, status, …, patch}`; we take ONLY
        the `filename` STRING from each `files[]` entry and DISCARD everything else — never the `patch` body, never
        a diff line. The only thing that leaves GitHub here is a list of path strings (the same content-free
        coordinate class as list_pr_files). Bounded + FAIL-SOFT — this is an ADVISORY add-on, so it must never be a
        hard dependency: returns [] on absent inputs, a non-dict / odd-shaped response, OR ANY exception (the
        caller is already fail-open, but we swallow here too so a bad ref can never escalate). The two refs are
        URL-quoted (a branch name can carry `/` or other URL-significant characters)."""
        if not (isinstance(repo, str) and repo and isinstance(base_sha, str) and base_sha
                and isinstance(head_ref, str) and head_ref):
            return []
        import urllib.parse
        # safe="" so a branch ref's '/' (release/1.0, feature/x) is percent-encoded — an unencoded slash would
        # be read as another path segment and break the compare URL.
        b = urllib.parse.quote(base_sha, safe="")
        h = urllib.parse.quote(head_ref, safe="")
        try:
            r = self._api("GET", f"/repos/{repo}/compare/{b}...{h}")
        except Exception:                       # advisory add-on: a compare error must never crash the event
            return []
        if not isinstance(r, dict):
            return []
        out = []
        for f in (r.get("files") or []):
            fn = f.get("filename") if isinstance(f, dict) else None
            if isinstance(fn, str) and fn:
                out.append(fn)                  # the path STRING only — the patch body is never read
        return out

    def merge_base_sha(self, repo: str, base_ref: str, head_sha: str) -> "str | None":
        """The MERGE-BASE commit SHA of `base_ref` (a PR's target branch) and `head_sha` (its verified head) —
        `GET /repos/{repo}/compare/{base}...{head}` → `merge_base_commit.sha`. This is the BASELINE SHAPE POINT
        of the shadow compatibility lane (docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md §2.1): the authoritative
        "before" that a PR's head and its target branch AGREE on — never the sibling PR's head.

        CONTENT-FREE BY CONSTRUCTION: the compare response's per-file entries (which can carry `patch` bodies)
        are never touched — ONLY the fixed-width hex SHA under `merge_base_commit` is read, validated, and
        returned. The only thing that leaves GitHub here is one commit id (the same content-free coordinate
        class as a head sha). Bounded + FAIL-SOFT (the compare_changed_paths discipline — this feeds an
        ADVISORY shadow surface, never a hard dependency): absent/malformed inputs, a transport error, an
        odd-shaped response, or a non-hex sha all → None, and the shadow caller degrades that pair to UNKNOWN
        (never a guess, never a crash). Both refs are URL-quoted with safe='' (a branch name can carry '/')."""
        if not (isinstance(repo, str) and repo and isinstance(base_ref, str) and base_ref
                and isinstance(head_sha, str) and head_sha):
            return None
        b = _urlparse.quote(base_ref, safe="")
        h = _urlparse.quote(head_sha, safe="")
        try:
            r = self._api("GET", f"/repos/{repo}/compare/{b}...{h}")
        except Exception:                       # advisory: a compare failure must never crash the event
            return None
        mbc = r.get("merge_base_commit") if isinstance(r, dict) else None
        sha = mbc.get("sha") if isinstance(mbc, dict) else None
        if isinstance(sha, str) and _re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
            return sha
        return None

    def list_open_pull_requests(self, repo: str, limit: int | None = None) -> list[dict]:
        # `limit` bounds the FETCH (stop paginating once we have enough) — the install-time backfill caps how
        # many open PRs it processes, so a repo with thousands of open PRs can't trigger an API storm on install.
        out, page = [], 1
        while True:
            r = self._api("GET", f"/repos/{repo}/pulls?state=open&per_page=100&page={page}")
            out += r
            if len(r) < 100 or (limit is not None and len(out) >= limit):
                break
            page += 1
        return out[:limit] if limit is not None else out

    def list_repo_branch_names(self, repo: str, limit: int | None = None) -> list[str]:
        """Return the repo's live branch names, with an optional hard fetch bound.

        Boot/PR reconciliation uses a COMPLETE live inventory to reclaim legacy ``BR-*`` lanes whose delete
        webhook predates the automatic branch-delete handler. Completeness is therefore a correctness boundary:
        a short intermediate page must never be mistaken for "there are no more live branches" and cause a live
        lane to be released. Production drives pagination from GitHub's authoritative ``Link: rel=next`` header;
        the length fallback exists only for older test clients that do not expose ``_api_with_link``.

        Malformed pages raise instead of degrading to ``[]``. The caller treats every read error as "do not
        reconcile", so uncertainty preserves live work. Branch names are content-free Git refs; no code or diff
        body is read.
        """
        if not isinstance(repo, str) or "/" not in repo:
            raise ValueError("repo must be owner/name")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
            raise ValueError("limit must be a positive integer")

        out: list[str] = []
        page = 1
        use_link = hasattr(self, "_api_with_link")
        while True:
            url = f"/repos/{repo}/branches?per_page=100&page={page}"
            if use_link:
                raw, link = self._api_with_link("GET", url)
            else:  # compatibility for narrow fakes; the production client always exposes _api_with_link
                raw, link = self._api("GET", url), ""
            if not isinstance(raw, list):
                raise RuntimeError("branch inventory returned a non-list page")
            for item in raw:
                name = item.get("name") if isinstance(item, dict) else None
                if not isinstance(name, str) or not name:
                    raise RuntimeError("branch inventory contained a malformed entry")
                out.append(name)
                if limit is not None and len(out) >= limit:
                    return out[:limit]
            has_next = self._has_link_next(link) if use_link else len(raw) == 100
            if not has_next:
                break
            # The destructive reconcile caller uses limit<=100 as a single-page safety wall. Never follow a
            # second page there: GitHub refs can mutate between pages and shift a still-live branch out of the
            # aggregate. A full first page returned at `limit` above becomes the caller's truncation sentinel; a
            # short page that nevertheless advertises `next` is incomplete and therefore an error/no-release.
            if limit is not None and limit <= 100:
                raise RuntimeError("branch inventory exceeds the single-page safety bound")
            page += 1
        return out

    def list_open_pull_requests_for_head(self, repo: str, head_branch: str, base: str | None = None,
                                         limit: int | None = None) -> list[dict]:
        """Open PRs whose head is THIS repo's branch, optionally targeting `base`.

        Used by the branch-push path to turn a same-repo push into the matching open PR's live `synchronize`
        evaluation even if GitHub's pull_request webhook is delayed or missing. The query is exact
        (`head=<owner>:<branch>`), so it does not scan every open PR and it intentionally excludes fork PRs: a
        push webhook for a fork branch is not delivered to the base repo installation. Content-free (repo,
        branch, PR metadata only; no file bodies)."""
        if not (isinstance(repo, str) and "/" in repo and isinstance(head_branch, str) and head_branch):
            return []
        owner = repo.split("/", 1)[0]
        out, page = [], 1
        while True:
            params = {
                "state": "open",
                "head": f"{owner}:{head_branch}",
                "per_page": "100",
                "page": str(page),
            }
            if isinstance(base, str) and base:
                params["base"] = base
            qs = _urlparse.urlencode(params)
            r = self._api("GET", f"/repos/{repo}/pulls?{qs}")
            if not isinstance(r, list):
                return out[:limit] if limit is not None else out
            out += r
            if len(r) < 100 or (limit is not None and len(out) >= limit):
                break
            page += 1
        return out[:limit] if limit is not None else out

    def list_pull_requests_for_commit(self, repo: str, head_sha: str,
                                      limit: int | None = None) -> list[dict]:
        """Open/closed PRs associated with an exact commit (GitHub 'List pull requests associated with a
        commit', GET /repos/{repo}/commits/{sha}/pulls).

        Used by the check_suite.requested backstop to resolve the acting PR from the suite's OWN head_sha
        when GitHub delivers an empty pull_requests[] array (seen under webhook-processing lag). The caller
        filters to open, same-repo, default-branch PRs at this exact head, so a fork PR sharing the sha is
        never given a base-repo Check. Content-free (PR number/state/refs/head metadata only; no file
        bodies) and bounded by `limit`."""
        if not (isinstance(repo, str) and "/" in repo and isinstance(head_sha, str) and head_sha):
            return []
        out, page = [], 1
        while True:
            r = self._api("GET", f"/repos/{repo}/commits/{head_sha}/pulls?per_page=100&page={page}")
            if not isinstance(r, list):
                return out[:limit] if limit is not None else out
            out += r
            if len(r) < 100 or (limit is not None and len(out) >= limit):
                break
            page += 1
        return out[:limit] if limit is not None else out

    def pull_request_head(self, repo: str, number: int) -> str:
        pr = self._api("GET", f"/repos/{repo}/pulls/{number}")
        return pr["head"]["sha"]

    def pull_request_head_and_fork(self, repo: str, number: int):
        """(head_sha, is_fork) in ONE fetch. is_fork = head.repo.id != base.repo.id — a FORK PR's comment posts on
        the BASE-repo conversation the EXTERNAL contributor can read, so its neighbor-refresh comment must be
        REDACTED of the base repo's other in-flight PR identities / paths (the INFO-LEAK GUARD the acting-PR path
        already applies; the engine has no fork concept, so fork status comes from here). Reuses the SAME GET
        pull_request_head does → no extra API call vs the head fetch _post_refreshes already makes."""
        head_sha, is_fork, _redact_external = self.pull_request_head_and_fork_privacy(repo, number)
        return head_sha, is_fork

    def pull_request_head_and_fork_privacy(self, repo: str, number: int):
        """(head_sha, confirmed_fork, redact_external) from one PR read.

        Missing nested repository identity is not proof of a fork and must not disable merge gating, but a deleted
        fork can produce exactly that shape. Keep the states separate: redact the externally readable comment while
        preserving required-check / pause behavior until GitHub positively confirms a fork.
        """
        pr = self._api("GET", f"/repos/{repo}/pulls/{number}")
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
        head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
        base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
        head_id, base_id = head_repo.get("id"), base_repo.get("id")
        is_fork = head_id is not None and base_id is not None and head_id != base_id
        identity_unknown = head_id is None or base_id is None
        return pr["head"]["sha"], is_fork, is_fork or identity_unknown

    def get_pull_request(self, repo: str, number: int) -> dict:
        """The PR's AUTHORITATIVE object (author / head sha / base ref / draft / merged) — the same fields a live
        pull_request webhook carries. Used by the check-rerun path: a check_suite/check_run `rerequested` event's
        pull_requests[] entry is a sparse REF (number + base), NOT a trustworthy source of authorship or head —
        so we re-fetch the real PR and replay THAT through the live router, never the check payload's fields."""
        return self._api("GET", f"/repos/{repo}/pulls/{number}")
