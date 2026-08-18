#!/usr/bin/env python3
"""P3 ONBOARDING-UX gate — empty-repo install signal + over-cap watching copy.

Two audit findings:

P3-A — EMPTY-REPO INSTALL = TOTAL SILENCE (dead first impression)
  When a customer installs Veripsa on a repo with no commits, the default-branch
  branches API returns HTTP 404 (no branch exists yet).  The old code caught this
  as `ingest_error`, so the onboarding audit showed a false failure, and NO watching
  check was ever posted.  The customer saw total silence.

  THE FIX:
    1. backfill_repo: a branches-API HTTP 404 is marked `empty_repo: True`, not
       `ingest_error`.  It is NOT a product error — it is a legitimate state.
    2. the first protected-branch push durably queues exact graph convergence
       with the signed, rename-stable repository id.
    3. the live worker performs no clone/extract or stale watching post; the
       background convergence worker owns the final watching Check.

P3-B — OVER-CAP MONOREPO 'WATCHING' COPY IS MISLEADING
  A genuinely over-cap repo (> _MAX_INGEST_FILES real code files) gets an empty
  graph on every push.  The old `watching_check(indexing=True)` copy said 'indexing
  will complete on the next push' — but it NEVER completes.  The SAME copy was used
  for deferred repos (which DO cold-start on first push), making it honest only there.

  THE FIX: `watching_check(over_cap=True)` uses a distinct, honest copy that does
  NOT promise completion: 'may not index on the current tier', 'may surface unknown'.

WHAT THIS GATE PROVES (OFFLINE — no Postgres required):

  UX-1  empty-repo backfill: the result carries `empty_repo: True`, NOT `ingest_error`
  UX-2  non-404 errors still produce `ingest_error` (no regression)
  UX-3  watching_check(over_cap=True): copy does NOT promise completion
  UX-4  watching_check(over_cap=True): copy uses honest hedging ('may', not 'will')
  UX-5  watching_check(over_cap=False, indexing=False): normal indexed copy unchanged
  UX-6  watching_check(indexing=True): background progress needs no additional push
  UX-7  first push on an empty repo: exact durable graph work is queued, with no inline clone/post
  UX-8  first push on a deferred repo: exact durable graph work is queued, with no inline clone/post
  UX-9  a push whose graph is already current does not enqueue or post a duplicate watching Check
  UX-10 a potentially over-cap first push still performs no inline extraction; the background result owns copy

Run:  python3 tests/test_onboarding_ux_p3.py   (no DB required)
"""
from __future__ import annotations

import io
import os
import sys
import tarfile
from urllib.error import HTTPError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)

import ingest  # noqa: E402
import render  # noqa: E402
import server as S  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")

FAIL = 0


def check(cond: bool, label: str):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL += 1


# ---------------------------------------------------------------------------
# Offline doubles
# ---------------------------------------------------------------------------

class _RecordingDB:
    """Minimal DB double: record_push succeeds (no quota), coordinate_graph_sha
    returns the optional persisted SHA, and the durable graph outbox returns a
    positive epoch. Other calls are benign no-ops."""

    def __init__(self, stored_sha: str | None = None):
        self.ingested = []
        self.enqueued = []
        self._commit_sha = stored_sha
        self._graph_hash = "f" * 64

    def __call__(self, sql, params=None):
        if "record_push_with_authority" in sql:
            return None                         # no quota exceeded — proceed normally
        if "coordinate_graph_sha" in sql:
            # Before the writer this remains a cold/unknown coordinate. After a
            # successful fake write, mirror the production readback contract that
            # _validated_graph_write verifies in the same transaction.
            if self._commit_sha is None:
                return None
            return {
                "commit_sha": self._commit_sha,
                "graph_hash": self._graph_hash,
                "semantic_ref_version": ingest._SEMANTIC_REF_VERSION,
            }
        if "enqueue_graph_refresh_with_authority" in sql and params:
            self.enqueued.append(tuple(params))
            return len(self.enqueued)
        if "ingest_graph_with_authority" in sql and params:
            self.ingested.append(params[0])     # record what was stored
            self._commit_sha = params[3]
            return {
                "ok": True,
                "graph_hash": self._graph_hash,
                "semantic_ref_version": ingest._SEMANTIC_REF_VERSION,
            }
        if "record_landing_with_authority" in sql:
            return None
        return None


class _EmptyRepoGitHub:
    """GitHub client that raises HTTP 404 on repo_default_branch_head (empty repo,
    no default branch exists yet).  Used to drive the backfill_repo empty-repo path."""

    def repo_default_branch_head(self, repo):
        raise HTTPError(
            f"https://api.github.com/repos/{repo}/branches/main", 404,
            "Branch not found", {}, None)

    def list_open_pull_requests(self, repo, limit=None):
        return []


class _WatchingRecordingGitHub:
    """GitHub client for push-handler tests. Any tarball request is recorded as a hot-path
    regression; Check writes are recorded so a stale synchronous watching post cannot hide."""

    def __init__(self, has_commits: bool = True):
        self.has_commits = has_commits
        self.checks = []                        # list of {repo, sha, conclusion, title, summary}
        self.tarball_downloads = []

    def repo_default_branch_head(self, repo):
        if self.has_commits:
            return "main", "a" * 40
        raise HTTPError(
            f"https://api.github.com/repos/{repo}/branches/main", 404,
            "Branch not found", {}, None)

    def download_tarball(self, repo, sha):
        self.tarball_downloads.append((repo, sha))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(FIXTURE, arcname="repo-" + sha[:7])
        return buf.getvalue()

    def get_file_at(self, repo, path, ref):
        full = os.path.join(FIXTURE, path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha]

    def upsert_check(self, repo, sha, conclusion, title, summary):
        self.checks.append({"repo": repo, "sha": sha,
                            "conclusion": conclusion, "title": title, "summary": summary})
        return self.checks[-1]

    def for_installation(self, installation_id):
        return self

    def list_open_pull_requests(self, repo, limit=None):
        return []


def _push_payload(repo: str, sha: str, branch: str = "main", repository_id: int = 7001):
    return {
        "ref": f"refs/heads/{branch}",
        "after": sha,
        "repository": {
            "full_name": repo, "default_branch": "main", "id": repository_id,
        },
        "commits": [],
    }


# ---------------------------------------------------------------------------
# P3-A: empty-repo classification in backfill_repo
# ---------------------------------------------------------------------------
print("-- P3-A: empty-repo backfill classification --")

# Stub the parts of backfill_repo we do not need to exercise
ingest.populate_cochange_async = lambda gh, repo, branch, window=800, repository_id=None: True

# UX-1: empty-repo install → empty_repo: True, NOT ingest_error
def _noop_db(sql, params=None):
    if "record_push_with_authority" in sql:
        return None
    return None

result_empty = ingest.backfill_repo(_noop_db, _EmptyRepoGitHub(), "acme/empty-repo")
check("empty_repo" in result_empty.get("graph", {}),
      "UX-1 empty-repo backfill: graph carries empty_repo:True (not ingest_error)")
check("ingest_error" not in result_empty.get("graph", {}),
      "UX-1b empty-repo backfill: graph does NOT carry ingest_error (a 404 from branches API is not an error)")


# UX-2: non-404 errors still produce ingest_error (no regression)
class _InternalErrorGitHub:
    def repo_default_branch_head(self, repo):
        raise HTTPError("url", 500, "Internal Server Error", {}, None)
    def list_open_pull_requests(self, repo, limit=None):
        return []

result_500 = ingest.backfill_repo(_noop_db, _InternalErrorGitHub(), "acme/broken-repo")
check("ingest_error" in result_500.get("graph", {}),
      "UX-2 non-404 errors still produce ingest_error (regression guard)")
check("empty_repo" not in result_500.get("graph", {}),
      "UX-2b non-404 errors do NOT produce empty_repo (regression guard)")


# ---------------------------------------------------------------------------
# P3-B: watching_check copy — over-cap vs deferred vs normal
# ---------------------------------------------------------------------------
print("-- P3-B: watching_check copy honesty --")

over_cap_chk = render.watching_check(files=0, edges=0, branch="main", over_cap=True)
normal_chk = render.watching_check(files=10, edges=5, branch="main", over_cap=False)
deferred_chk = render.watching_check(files=0, edges=0, branch="main", indexing=True, over_cap=False)

# UX-3: over-cap copy does NOT promise completion (no 'will complete', 'next push', or 'complete on')
completion_phrases = ["will complete", "complete on the next push", "it will complete"]
over_cap_body = over_cap_chk["summary"].lower()
check(not any(p in over_cap_body for p in completion_phrases),
      "UX-3 over-cap watching copy does NOT promise completion (no 'will complete' / 'complete on the next push')")

# UX-4: over-cap copy uses honest hedging ('may', never a flat 'will' assertion about indexing)
check("may" in over_cap_body,
      "UX-4 over-cap watching copy uses 'may' (honest hedging, not a flat promise)")

# UX-5: normal indexed copy is unchanged
check("is indexed" in normal_chk["summary"] or "branch is indexed" in normal_chk["summary"],
      "UX-5 normal indexed watching copy unchanged ('branch is indexed')")

# UX-6: queued background work is explicit and does not ask the customer for another push.
deferred_body = deferred_chk["summary"].lower()
check("background" in deferred_body
      and "no additional push is required" in deferred_body
      and "complete on the next push" not in deferred_body,
      "UX-6 indexing=True reports background progress and requires no additional push")

# conclusion is neutral in all cases (advisory — never blocks anything)
check(over_cap_chk["conclusion"] == "neutral",
      "UX-3b over-cap watching check conclusion is neutral (advisory)")
check(normal_chk["conclusion"] == "neutral",
      "UX-5b normal watching check conclusion is neutral (advisory)")


# ---------------------------------------------------------------------------
# P3-A: cold-start durable enqueue via push handler
# ---------------------------------------------------------------------------
print("-- P3-A: cold-start graph work leaves the live worker --")

# UX-7: first push to an empty repo → exact durable work, no inline clone/post.
gh7 = _WatchingRecordingGitHub(has_commits=True)
db7 = _RecordingDB()                     # no prior graph → convergence required
sha7 = "b" * 40
res7 = S.handle_event("push", _push_payload("acme/was-empty", sha7), db7, gh7)
check(res7.get("mode") == "queued"
      and res7.get("graph_refresh", {}).get("queued") is True
      and db7.enqueued == [("acme/was-empty", "main", sha7, "7001")],
      "UX-7 empty-repo first push durably queues the exact stable-id graph coordinate")
check(gh7.tarball_downloads == [] and gh7.checks == [] and res7.get("cold_start") is not True,
      "UX-7b empty-repo first push performs no inline clone or stale watching post")

# UX-8: first push to a deferred repo follows the same async boundary.
gh8 = _WatchingRecordingGitHub(has_commits=True)
db8 = _RecordingDB()
sha8 = "c" * 40
res8 = S.handle_event(
    "push", _push_payload("acme/deferred-repo", sha8, repository_id=7002), db8, gh8)
check(res8.get("mode") == "queued"
      and db8.enqueued == [("acme/deferred-repo", "main", sha8, "7002")]
      and gh8.tarball_downloads == [] and gh8.checks == [],
      "UX-8 deferred-repo first push queues exact work and leaves clone/post to background convergence")

# Missing stable identity is never acknowledged as durable success.
missing_id_payload = _push_payload("acme/missing-id", "f" * 40)
missing_id_payload["repository"].pop("id")
missing_id_failed = False
try:
    S.handle_event("push", missing_id_payload, _RecordingDB(), _WatchingRecordingGitHub())
except RuntimeError as exc:
    missing_id_failed = "stable repository identity" in str(exc)
check(missing_id_failed,
      "UX-8b a structural push without authenticated stable repository identity fails for durable retry")

# UX-9: a graph already current at this exact push needs neither a queue write nor a watching post.
gh9 = _WatchingRecordingGitHub(has_commits=True)
sha9 = "d" * 40
db9 = _RecordingDB(stored_sha=sha9)
res9 = S.handle_event(
    "push", _push_payload("acme/existing-repo", sha9, repository_id=7003), db9, gh9)
check(res9.get("mode") == "skip"
      and res9.get("graph_refresh", {}).get("reason") == "already current"
      and db9.enqueued == [] and gh9.checks == [] and gh9.tarball_downloads == [],
      "UX-9 an exact-current graph skips enqueue, clone, and duplicate watching output")

# UX-10: even a potentially over-cap repo is never scanned on the live push worker. The direct render assertions
# above lock the honest over-cap copy used after the background extractor discovers that outcome.
old_cap = ingest._MAX_INGEST_FILES
ingest._MAX_INGEST_FILES = 1
gh10 = _WatchingRecordingGitHub(has_commits=True)
db10 = _RecordingDB()
sha10 = "e" * 40
try:
    res10 = S.handle_event(
        "push", _push_payload("acme/giant-repo", sha10, repository_id=7004), db10, gh10)
finally:
    ingest._MAX_INGEST_FILES = old_cap
check(res10.get("mode") == "queued"
      and db10.enqueued == [("acme/giant-repo", "main", sha10, "7004")]
      and gh10.tarball_downloads == [] and gh10.checks == [],
      "UX-10 a potentially over-cap first push queues work without inline extraction or premature copy")

print()
print("ONBOARDING UX P3 GATE:", "PASS" if FAIL == 0 else f"FAIL ({FAIL} check(s) failed)")
sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    pass  # sys.exit above handles it
