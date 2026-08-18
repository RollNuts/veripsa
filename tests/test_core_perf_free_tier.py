#!/usr/bin/env python3
"""CORE-PERF FREE-TIER gate — pins the two performance fixes that target Render-free-tier latency without
changing semantics or moat invariants. OFFLINE; no Postgres, no GitHub account.

WHY THIS LANE: the PO observed (2026-06-25) that "core自体のレスポンスも遅い気がする / 無料の範囲でできる
ことをやってほしい" — Core's response feels slow within the free tier. Two surgical fixes were applied:

  (1) PR pre-brain parallel reads. _pr_pre_brain runs `gh.base_blob_shas` and `gh.compare_changed_paths`
      on every PR analyze. The two GitHub calls are independent (different endpoints, no shared state, both
      content-free path-only reads). Serial they cost ~400-1000ms; parallel they cost ~max(t1,t2). Bounded by
      VERIPSA_PR_PREBRAIN_PARALLEL (kill switch). Fail-open semantics + log strings unchanged.

  (2) /healthz MEMORY-ONLY advisory cache. Freshness and durable-depth DB reads populate shared last-good
      slots through fixed single-flight daemons. The liveness request returns immediately on cold/expired
      state, so a DB/kernel stall cannot hang Render's restart decision or spawn one thread per probe.

PINNED BEHAVIOR (the contract the fixes must preserve, on top of the perf claim):
  (A) parallel pre-brain returns the SAME base_hashes + branch_changed_paths the serial path returned.
  (B) parallel pre-brain DOES overlap the calls (we can prove it with two GH stubs that each sleep ~150ms;
      total wall-clock < ~250ms < serial would be ~300ms).
  (C) kill-switch VERIPSA_PR_PREBRAIN_PARALLEL=0 reverts to serial (and the same result still comes out).
  (D) parallel path fail-open: if one of the calls raises, the other's result is preserved + the failing
      one falls back to its default (the same default the serial path used).
  (E) /healthz cache reuses a value within the TTL (one DB call across N probes; subsequent probes don't
      touch the db lambda again).
  (F) /healthz cache TTL=0 disables the cache (every probe re-queries).
  (G) /healthz cache returns NONE on a DB error AND does NOT poison the cache (the next probe re-tries).
  (H2) the real health-only helpers return cold-cache None in <200ms while both samplers are blocked, start
       one daemon each, and publish the successful samples after the blockers clear.

Run:  python3 tests/test_core_perf_free_tier.py     (no DB needed)
"""
from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)   # for code_graph_extract (used by webhook_coercion._code_paths)

# Import the modules under test. Both load cleanly with NO Postgres (the lazy psycopg2 imports stay deferred).
import server_http as SH  # noqa: E402
import server_boot as SB  # noqa: E402
from github_rest_prread import (PRFilesMalformed, PRFilesPageBudgetExceeded, PRFilesShortfall,
                                _GitHubPRReadMixin)  # noqa: E402
import webhook_handlers as WH  # noqa: E402

FAIL = 0


def check(cond, label):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
# Fix (1) — PR pre-brain parallel reads. Drive _pr_pre_brain with a stub gh and a db lambda that records calls.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

class _StubGH:
    """A minimal Fake gh client for _pr_pre_brain. Supports base_blob_shas / compare_changed_paths with
    configurable latency + return values + error injection. Records call timestamps so the test can prove the
    two calls actually OVERLAP under the parallel path (and run SERIALLY under the kill switch)."""

    def __init__(self, *, base_hashes_result=None, branch_paths_result=None,
                 base_hashes_latency=0.0, branch_paths_latency=0.0,
                 base_hashes_raises=None, branch_paths_raises=None):
        self._bh = base_hashes_result if base_hashes_result is not None else {}
        self._bp = branch_paths_result if branch_paths_result is not None else []
        self._bh_lat = base_hashes_latency
        self._bp_lat = branch_paths_latency
        self._bh_raises = base_hashes_raises
        self._bp_raises = branch_paths_raises
        # Call instrumentation
        self.base_blob_shas_calls = []
        self.compare_changed_paths_calls = []

    def base_blob_shas(self, repo, base_sha, paths):
        self.base_blob_shas_calls.append((time.monotonic(), repo, base_sha, list(paths)))
        if self._bh_lat:
            time.sleep(self._bh_lat)
        if self._bh_raises is not None:
            raise self._bh_raises
        return dict(self._bh)

    def compare_changed_paths(self, repo, base_sha, head_ref):
        self.compare_changed_paths_calls.append((time.monotonic(), repo, base_sha, head_ref))
        if self._bp_lat:
            time.sleep(self._bp_lat)
        if self._bp_raises is not None:
            raise self._bp_raises
        return list(self._bp)


def _make_pr_frame(*, repo="acme/app", pr=42, base="main", base_sha="b" * 40,
                   default_branch="main", head_ref="feat-x", is_fork=False, action="opened",
                   should_analyze=True, prj=None):
    """The dict shape _pr_pre_brain reads — keep it minimal but complete."""
    return {
        "should_analyze": should_analyze,
        "repo": repo, "pr": pr, "base": base, "base_sha": base_sha, "is_fork": is_fork,
        "default_branch": default_branch, "head_ref_name": head_ref,
        "prj": dict(prj or {"draft": False}),
        "action": action,
    }


def _db_noop(_sql, _args=()):
    """The push↔PR reconcile is a side-effecting DB write (best-effort + the test stub is a no-op runner).
    Returns None — _pr_pre_brain does not consume the return for these writes."""
    return None


# (A) parallel pre-brain returns the SAME values the serial path returned.
print("=== Fix (1) PR pre-brain parallel reads ===")
os.environ["VERIPSA_PR_PREBRAIN_PARALLEL"] = "1"  # explicit ON
_changed_in = ["src/a.py", "src/b.py"]
_ranges_in = {"src/a.py": [[10, 12]], "src/b.py": [[1, 1]]}
gh1 = _StubGH(base_hashes_result={"src/a.py": "h_a", "src/b.py": "h_b"},
              branch_paths_result=["src/c.py", "src/a.py"])
changed_out, ranges_out, added_paths_out, truncated, base_hashes, branch_paths = WH._pr_pre_brain(
    _db_noop, gh1, _make_pr_frame(), _changed_in, _ranges_in)
check(base_hashes == {"src/a.py": "h_a", "src/b.py": "h_b"},
      "(A) parallel pre-brain returns the SAME base_hashes the gh client gave back")
check(branch_paths == ["src/c.py", "src/a.py"],
      "(A) parallel pre-brain returns the SAME branch_changed_paths the gh client gave back")
check(changed_out == _changed_in and not truncated,
      "(A) parallel pre-brain does not perturb the changed-files list / truncated flag")
check(ranges_out == _ranges_in,
      "(A) parallel pre-brain does not perturb the changed_ranges map")
check(added_paths_out == [],
      "(A) parallel pre-brain: added_paths defaults to [] when caller passes nothing (honest-verdict default)")


# (B) parallel pre-brain DOES overlap the calls. Drive each stub to sleep ~150ms; total wall-clock should be
# closer to max(150, 150) = ~150ms than serial sum = ~300ms. We pick a slack ceiling well below the serial sum
# but well above max+overhead so the assertion never flakes on a slow runner.
gh2 = _StubGH(base_hashes_result={"src/a.py": "h_a"},
              branch_paths_result=["src/c.py"],
              base_hashes_latency=0.15, branch_paths_latency=0.15)
_t0 = time.monotonic()
WH._pr_pre_brain(_db_noop, gh2, _make_pr_frame(), ["src/a.py"], {"src/a.py": []})
_t1 = time.monotonic()
_wall = _t1 - _t0
check(len(gh2.base_blob_shas_calls) == 1 and len(gh2.compare_changed_paths_calls) == 1,
      "(B) each GH call was made exactly once (no duplication / no extra retries)")
_start_delta = abs(gh2.base_blob_shas_calls[0][0] - gh2.compare_changed_paths_calls[0][0])
# Serial would start the second call after the first 150ms sleep completes. In
# the parallel path both call timestamps are recorded before their sleeps, so
# the start delta stays far below one call's latency even on a loaded runner.
check(_start_delta < 0.10,
      f"(B) parallel pre-brain overlaps the two GH reads (start delta={_start_delta:.3f}s < 0.10s; serial would be ≥ 0.15s, wall={_wall:.3f}s)")


# (C) kill switch VERIPSA_PR_PREBRAIN_PARALLEL=0 reverts to serial.
os.environ["VERIPSA_PR_PREBRAIN_PARALLEL"] = "0"
gh3 = _StubGH(base_hashes_result={"src/a.py": "h_a"},
              branch_paths_result=["src/c.py"],
              base_hashes_latency=0.10, branch_paths_latency=0.10)
_t0 = time.monotonic()
_, _, _, _, bh3, bp3 = WH._pr_pre_brain(_db_noop, gh3, _make_pr_frame(), ["src/a.py"], {"src/a.py": []})
_t1 = time.monotonic()
_wall_serial = _t1 - _t0
check(bh3 == {"src/a.py": "h_a"} and bp3 == ["src/c.py"],
      "(C) serial fallback returns the same values the parallel path returned")
# Serial wall time must be roughly >= sum of both sleeps (proof we did NOT take the parallel path).
check(_wall_serial >= 0.18,
      f"(C) kill-switch=0 actually runs the reads SERIALLY (wall={_wall_serial:.3f}s ≥ ~0.20s — sum of sleeps)")
# Restore default
os.environ["VERIPSA_PR_PREBRAIN_PARALLEL"] = "1"


# (D) parallel path fail-open: if base_blob_shas raises, branch_changed_paths still comes through (and vice
# versa). Same fall-back defaults the serial path returned ({} / []).
gh4 = _StubGH(branch_paths_result=["src/c.py"], base_hashes_raises=RuntimeError("simulated GH 503"))
_, _, _, _, bh4, bp4 = WH._pr_pre_brain(_db_noop, gh4, _make_pr_frame(), ["src/a.py"], {"src/a.py": []})
check(bh4 == {} and bp4 == ["src/c.py"],
      "(D) parallel: base_blob_shas FAILS → bh={} fall-back, branch_paths still SUCCEEDS (no thread-leak across)")

gh5 = _StubGH(base_hashes_result={"src/a.py": "h_a"},
              branch_paths_raises=RuntimeError("simulated GH 503"))
_, _, _, _, bh5, bp5 = WH._pr_pre_brain(_db_noop, gh5, _make_pr_frame(), ["src/a.py"], {"src/a.py": []})
check(bh5 == {"src/a.py": "h_a"} and bp5 == [],
      "(D) parallel: compare_changed_paths FAILS → bp=[] fall-back, base_hashes still SUCCEEDS")


# (D2) PR Files metadata one-pass read: ranges + added status + conflict markers share ONE pagination pass.
class _FilesClient(_GitHubPRReadMixin):
    def __init__(self, pages):
        self.pages = pages
        self.page_calls = 0

    def _page_pr_files_raw(self, repo, number):
        self.page_calls += 1
        for page in self.pages:
            yield page, False


_patch_plain = "@@ -10,1 +10,1 @@\n-old\n+new\n"
_patch_conflict = "@@ -1,1 +1,4 @@\n-old\n+<<<<<<< ours\n+=======\n+>>>>>>> theirs\n"
files_client = _FilesClient([[
    {"filename": "src/a.py", "status": "modified", "patch": _patch_plain},
    {"filename": "src/new.py", "status": "added", "patch": _patch_conflict},
    {"filename": "src/renamed.py", "status": "renamed", "previous_filename": "src/old.py", "patch": _patch_plain},
]])
meta = files_client.list_pr_file_metadata("acme/app", 42, pr_changed_files=3)
check(files_client.page_calls == 1,
      f"(D2) one-pass PR metadata does exactly one Files API pagination pass (got {files_client.page_calls})")
check(meta["changed"] == ["src/a.py", "src/new.py", "src/renamed.py", "src/old.py"],
      "(D2) one-pass metadata returns changed filenames plus rename source in the same order")
check(meta["raw_entry_count"] == 3,
      "(D2) raw Files entry count stays 3 even though rename coordination expands changed paths to 4")
check(meta["changed_ranges"].get("src/old.py") == [] and "src/a.py" in meta["changed_ranges"],
      "(D2) one-pass metadata preserves changed ranges and maps rename source to file-level []")
check(meta["added_paths"] == ["src/new.py"],
      "(D2) one-pass metadata returns added paths without a second Files API read")
check(any(f["path"] == "src/new.py" and f["kind"] == "ours" for f in meta["conflict_markers"]),
      "(D2) one-pass metadata returns conflict-marker findings without a third Files API read")

try:
    _FilesClient([[{"status": "modified", "patch": _patch_plain}]]).list_pr_file_metadata(
        "acme/app", 42, pr_changed_files=1)
    _malformed_raised = False
except PRFilesMalformed:
    _malformed_raised = True
check(_malformed_raised, "(D2) a Files entry without filename is rejected, never counted as complete evidence")

class _CompareClient(_GitHubPRReadMixin):
    def __init__(self, response): self.response = response
    def _api(self, method, path): return self.response

_strict_bad = []
for _response in ({}, {"files": [{}]}):
    try:
        _CompareClient(_response).compare_changed_paths_strict("acme/app", "a" * 40, "main")
        _strict_bad.append(False)
    except ValueError:
        _strict_bad.append(True)
check(all(_strict_bad), "(D2) strict compare rejects missing files/filename instead of erasing a prior nudge")

_proof = _CompareClient({
    "files": [{"filename": "src/a.py"}],
    "base_commit": {"sha": "a" * 40},
    "merge_base_commit": {"sha": "a" * 40},
    "status": "ahead",
    "total_commits": 1,
    "commits": [{"sha": "b" * 40}],
}).compare_changed_paths_proof("acme/app", "a" * 40, "b" * 40)
check(
    _proof == {
        "paths": ["src/a.py"],
        "base_sha": "a" * 40,
        "merge_base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "status": "ahead",
    },
    "(D2) compare proof preserves paths plus exact base/merge-base/head ancestry",
)
try:
    _CompareClient({
        "files": [{"filename": "src/a.py"}],
        "base_commit": {"sha": "a" * 40},
        "status": "ahead",
        "total_commits": 1,
        "commits": [{"sha": "b" * 40}],
    }).compare_changed_paths_proof("acme/app", "a" * 40, "b" * 40)
    _proof_missing_rejected = False
except ValueError:
    _proof_missing_rejected = True
check(
    _proof_missing_rejected,
    "(D2) compare proof rejects missing ancestry fields",
)

try:
    _FilesClient([[{"filename": "only.py", "status": "modified", "patch": _patch_plain}]]).list_pr_file_metadata(
        "acme/app", 42, pr_changed_files=2)
    _shortfall_raised = False
except PRFilesShortfall as e:
    _shortfall_raised = e.returned == 1 and e.declared == 2
check(_shortfall_raised,
      "(D2) one-pass metadata preserves PRFilesShortfall on a provable partial Files API read")


class _BudgetFilesClient(_GitHubPRReadMixin):
    """Infinite-looking Link chain; the metadata method must stop before requesting page max_pages+1."""
    def __init__(self):
        self.pages_requested = 0

    def _page_pr_files_raw(self, repo, number):
        for i in range(10):
            self.pages_requested += 1
            yield ([{"filename": f"src/p{i}.py", "status": "modified", "patch": _patch_plain}], True)


budget_client = _BudgetFilesClient()
try:
    budget_client.list_pr_file_metadata("acme/app", 42, pr_changed_files=1, max_pages=2)
    _page_budget_raised = False
except PRFilesPageBudgetExceeded as e:
    _page_budget_raised = e.used == 2 and e.budget == 2
check(_page_budget_raised and budget_client.pages_requested == 2,
      "(D2) Files max_pages raises after page 2 and BEFORE the generator can request page 3")


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
# Fix (2) — shared advisory CACHE + memory-only /healthz refresh. First pin the synchronous ready/diagnostic
# cache primitive, then prove the liveness-only helpers never wait for its DB/store samplers.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
print("=== Fix (2) /healthz freshness CACHE ===")


class _DBStub:
    """A db runner that counts how often it is asked. Returns a canned freshness surface dict."""
    def __init__(self, payload=None, raises_first=False):
        self.calls = 0
        self.payload = payload if payload is not None else {"coordinate_count": 3, "max_age_seconds": 42.5}
        self._raise_first = raises_first

    def __call__(self, _sql, _args=()):
        self.calls += 1
        if self._raise_first and self.calls == 1:
            raise RuntimeError("simulated DB blip — first call only")
        return self.payload


def _make_handler_instance(db):
    """Build a Handler class via make_handler with stubs for the other deps and instantiate it WITHOUT calling
    its HTTP machinery (we only call _freshness_summary_db_only directly)."""
    Handler = SH.make_handler(secret="s", store=None, worker=object(), db=db, dsn="dummy", gh=object())
    inst = Handler.__new__(Handler)  # bypass __init__ — BaseHTTPRequestHandler.__init__ needs a real socket
    return inst


class _StoreStub:
    def __init__(self, payload=None, raises_first=False):
        self.calls = 0
        self.payload = payload if payload is not None else {"queued": 2, "processing": 1, "failed": 0}
        self._raise_first = raises_first

    def depth(self):
        self.calls += 1
        if self._raise_first and self.calls == 1:
            raise RuntimeError("simulated store blip — first call only")
        return self.payload


def _make_handler_instance_with_store(store):
    Handler = SH.make_handler(secret="s", store=store, worker=object(), db=_DBStub(), dsn="dummy", gh=object())
    return Handler.__new__(Handler)


# Reset cache state from any prior interference (this module is shared).
SH._HEALTHZ_FRESHNESS_CACHE["value"] = None
SH._HEALTHZ_FRESHNESS_CACHE["at"] = 0.0
# Confirm default TTL is the documented 15s.
os.environ.pop("VERIPSA_HEALTHZ_FRESHNESS_CACHE_SECONDS", None)
check(abs(SH._healthz_freshness_cache_ttl() - 15.0) < 1e-9,
      "(E0) default cache TTL is 15s when the env knob is unset")
for bad_ttl in ("inf", "nan"):
    os.environ["VERIPSA_HEALTHZ_FRESHNESS_CACHE_SECONDS"] = bad_ttl
    check(abs(SH._healthz_freshness_cache_ttl() - 15.0) < 1e-9,
          f"(E0) non-finite cache TTL {bad_ttl!r} falls back to the safe 15s default")


# (E) /healthz cache reuses a value within the TTL — N probes → 1 DB call.
os.environ["VERIPSA_HEALTHZ_FRESHNESS_CACHE_SECONDS"] = "30"   # generous TTL so the test never expires mid-loop
SH._HEALTHZ_FRESHNESS_CACHE["value"] = None  # warm-from-empty
SH._HEALTHZ_FRESHNESS_CACHE["at"] = 0.0
db1 = _DBStub()
h1 = _make_handler_instance(db1)
results = [h1._freshness_summary_db_only() for _ in range(5)]
check(db1.calls == 1, f"(E) within TTL: 5 probes ⇒ 1 DB call (got {db1.calls})")
check(all(r == {"coordinate_count": 3, "max_age_seconds": 42.5} for r in results),
      "(E) cached value is returned byte-for-byte across probes (no torn read)")
db1_other = _DBStub(payload={"coordinate_count": 9, "max_age_seconds": 1.0})
h1_other = _make_handler_instance(db1_other)
check(h1_other._freshness_summary_db_only() == {"coordinate_count": 9, "max_age_seconds": 1.0}
      and db1_other.calls == 1,
      "(E) cache is scoped to the db runner (a second handler cannot reuse the first handler's freshness)")


# (F) /healthz cache TTL=0 disables the cache — every probe re-queries.
os.environ["VERIPSA_HEALTHZ_FRESHNESS_CACHE_SECONDS"] = "0"
SH._HEALTHZ_FRESHNESS_CACHE["value"] = None
SH._HEALTHZ_FRESHNESS_CACHE["at"] = 0.0
db2 = _DBStub()
h2 = _make_handler_instance(db2)
for _ in range(4):
    h2._freshness_summary_db_only()
check(db2.calls == 4, f"(F) TTL=0: 4 probes ⇒ 4 DB calls (got {db2.calls})")


# (G) /healthz cache returns None on a DB error AND does NOT poison the cache — the next probe re-tries.
os.environ["VERIPSA_HEALTHZ_FRESHNESS_CACHE_SECONDS"] = "30"
SH._HEALTHZ_FRESHNESS_CACHE["value"] = None
SH._HEALTHZ_FRESHNESS_CACHE["at"] = 0.0
db3 = _DBStub(raises_first=True)
h3 = _make_handler_instance(db3)
r_err = h3._freshness_summary_db_only()                          # first call raises → returns None
check(r_err is None, "(G) DB error returns None (fail-open)")
check(SH._HEALTHZ_FRESHNESS_CACHE.get("value") is None,
      "(G) DB error does NOT poison the cache slot (it stays None — never a False/{} half-state)")
r_ok = h3._freshness_summary_db_only()                           # second call succeeds → real surface, cached
check(r_ok == {"coordinate_count": 3, "max_age_seconds": 42.5},
      "(G) NEXT probe re-tries the DB after the failure (cache was not poisoned)")
check(db3.calls == 2,
      "(G) total DB calls = 2 (one failure + one success); the cache was not skipped after the failure either")


# (H) /healthz inbox_depth cache bounds durable-store reads; TTL=0 disables it; failures do not poison.
os.environ["VERIPSA_HEALTHZ_INBOX_DEPTH_CACHE_SECONDS"] = "30"
SH._HEALTHZ_INBOX_DEPTH_CACHE["value"] = None
SH._HEALTHZ_INBOX_DEPTH_CACHE["at"] = 0.0
store1 = _StoreStub()
h_store1 = _make_handler_instance_with_store(store1)
depths = [h_store1._inbox_depth_cached() for _ in range(5)]
check(store1.calls == 1, f"(H) inbox_depth within TTL: 5 probes ⇒ 1 store.depth() call (got {store1.calls})")
check(all(d == {"queued": 2, "processing": 1, "failed": 0} for d in depths),
      "(H) cached inbox_depth returns the same advisory counts across probes")
store1_other = _StoreStub(payload={"queued": 8, "processing": 0, "failed": 1})
h_store1_other = _make_handler_instance_with_store(store1_other)
check(h_store1_other._inbox_depth_cached() == {"queued": 8, "processing": 0, "failed": 1}
      and store1_other.calls == 1,
      "(H) inbox_depth cache is scoped to the durable store instance")

os.environ["VERIPSA_HEALTHZ_INBOX_DEPTH_CACHE_SECONDS"] = "0"
SH._HEALTHZ_INBOX_DEPTH_CACHE["value"] = None
SH._HEALTHZ_INBOX_DEPTH_CACHE["at"] = 0.0
store2 = _StoreStub()
h_store2 = _make_handler_instance_with_store(store2)
for _ in range(4):
    h_store2._inbox_depth_cached()
check(store2.calls == 4, f"(H) inbox_depth TTL=0: 4 probes ⇒ 4 store.depth() calls (got {store2.calls})")

os.environ["VERIPSA_HEALTHZ_INBOX_DEPTH_CACHE_SECONDS"] = "30"
SH._HEALTHZ_INBOX_DEPTH_CACHE["value"] = None
SH._HEALTHZ_INBOX_DEPTH_CACHE["at"] = 0.0
store3 = _StoreStub(raises_first=True)
h_store3 = _make_handler_instance_with_store(store3)
check(h_store3._inbox_depth_cached() is None,
      "(H) inbox_depth store error omits the block (fail-open)")
check(SH._HEALTHZ_INBOX_DEPTH_CACHE.get("value") is None,
      "(H) inbox_depth store error does NOT poison the cache")
check(h_store3._inbox_depth_cached() == {"queued": 2, "processing": 1, "failed": 0} and store3.calls == 2,
      "(H) inbox_depth retries after a store error and then caches the success")

# (H2) The Render liveness helpers never perform those DB reads synchronously.
os.environ["VERIPSA_HEALTHZ_FRESHNESS_CACHE_SECONDS"] = "30"
os.environ["VERIPSA_HEALTHZ_INBOX_DEPTH_CACHE_SECONDS"] = "30"
SH._cache_clear(SH._HEALTHZ_FRESHNESS_CACHE)
SH._cache_clear(SH._HEALTHZ_INBOX_DEPTH_CACHE)
release_advisories = threading.Event()
freshness_started = threading.Event()
depth_started = threading.Event()


class _BlockingDB:
    def __init__(self):
        self.calls = 0

    def __call__(self, _sql, _args=()):
        self.calls += 1
        freshness_started.set()
        release_advisories.wait(2.0)
        return {"coordinate_count": 4, "max_age_seconds": 2.0}


class _BlockingStore:
    def __init__(self):
        self.calls = 0

    def depth(self):
        self.calls += 1
        depth_started.set()
        release_advisories.wait(2.0)
        return {"queued": 7, "processing": 1, "failed": 0}


blocking_db = _BlockingDB()
blocking_store = _BlockingStore()
BlockingHandler = SH.make_handler(
    secret="s",
    store=blocking_store,
    worker=object(),
    db=blocking_db,
    dsn="dummy",
    gh=object(),
)
blocking_handler = BlockingHandler.__new__(BlockingHandler)
nonblocking_started = time.monotonic()
cold_freshness = blocking_handler._healthz_freshness_nonblocking()
cold_depth = blocking_handler._healthz_inbox_depth_nonblocking()
nonblocking_elapsed = time.monotonic() - nonblocking_started
samplers_started = (
    freshness_started.wait(0.5) and depth_started.wait(0.5)
)
second_started = time.monotonic()
blocking_handler._healthz_freshness_nonblocking()
blocking_handler._healthz_inbox_depth_nonblocking()
second_elapsed = time.monotonic() - second_started
check(
    cold_freshness is None
    and cold_depth is None
    and nonblocking_elapsed < 0.2
    and second_elapsed < 0.2
    and samplers_started,
    f"(H2) cold /healthz advisory reads return without waiting for DB/store "
    f"(first={nonblocking_elapsed:.4f}s, second={second_elapsed:.4f}s)",
)
check(
    blocking_db.calls == 1 and blocking_store.calls == 1,
    "(H2) blocked advisory refreshes are single-flight, never one daemon per probe",
)
release_advisories.set()
refresh_deadline = time.monotonic() + 1.0
while (
    (SH._HEALTHZ_FRESHNESS_CACHE.get("value") is None
     or SH._HEALTHZ_INBOX_DEPTH_CACHE.get("value") is None)
    and time.monotonic() < refresh_deadline
):
    time.sleep(0.005)
check(
    SH._HEALTHZ_FRESHNESS_CACHE.get("value")
    == {"coordinate_count": 4, "max_age_seconds": 2.0}
    and SH._HEALTHZ_INBOX_DEPTH_CACHE.get("value")
    == {"queued": 7, "processing": 1, "failed": 0},
    "(H2) successful background samples become the next probe's last-good memory values",
)


# (I) /freshz cache bounds the DB+GitHub freshness sample; TTL=0 disables it; failures do not poison.
import server as S  # noqa: E402

_orig_graph_freshness_all = S.graph_freshness_all
try:
    class _FreshnessStub:
        def __init__(self, raises_first=False, payload=None, delay=0.0):
            self.calls = 0
            self._raise_first = raises_first
            self._payload = payload if payload is not None else [
                {"repo": "acme/app", "branch": "main", "behind": True}
            ]
            self._delay = delay

        def __call__(self, _db, _gh):
            self.calls += 1
            if self._delay:
                time.sleep(self._delay)
            if self._raise_first and self.calls == 1:
                raise RuntimeError("simulated GitHub freshness blip")
            return self._payload

    os.environ["VERIPSA_FRESHZ_CACHE_SECONDS"] = "30"
    SH._FRESHZ_CACHE["value"] = None
    SH._FRESHZ_CACHE["at"] = 0.0
    fresh1 = _FreshnessStub()
    S.graph_freshness_all = fresh1
    h_fresh1 = _make_handler_instance(_DBStub())
    fresh_payloads = [h_fresh1._freshz_payload_cached() for _ in range(5)]
    check(fresh1.calls == 1, f"(I) /freshz within TTL: 5 probes ⇒ 1 freshness sample (got {fresh1.calls})")
    check(all(p["behind_count"] == 1 and p["any_behind"] is True for p in fresh_payloads),
          "(I) /freshz cached payload preserves behind_count / any_behind")
    fresh1_other = _FreshnessStub(payload=[{"repo": "acme/app", "branch": "main", "behind": False}])
    S.graph_freshness_all = fresh1_other
    h_fresh1_other = _make_handler_instance(_DBStub())
    other_payload = h_fresh1_other._freshz_payload_cached()
    check(fresh1_other.calls == 1 and other_payload["behind_count"] == 0 and other_payload["sampled"] is True,
          "(I) /freshz cache is scoped to the freshness function/db/gh tuple")

    os.environ["VERIPSA_FRESHZ_CACHE_SECONDS"] = "0"
    SH._FRESHZ_CACHE["value"] = None
    SH._FRESHZ_CACHE["at"] = 0.0
    fresh2 = _FreshnessStub()
    S.graph_freshness_all = fresh2
    h_fresh2 = _make_handler_instance(_DBStub())
    for _ in range(4):
        h_fresh2._freshz_payload_cached()
    check(fresh2.calls == 4, f"(I) /freshz TTL=0: 4 probes ⇒ 4 freshness samples (got {fresh2.calls})")

    os.environ["VERIPSA_FRESHZ_CACHE_SECONDS"] = "30"
    SH._FRESHZ_CACHE["value"] = None
    SH._FRESHZ_CACHE["at"] = 0.0
    SH._FRESHZ_REFRESH["thread"] = None
    SH._FRESHZ_REFRESH["key"] = None
    fresh3 = _FreshnessStub(raises_first=True)
    S.graph_freshness_all = fresh3
    h_fresh3 = _make_handler_instance(_DBStub())
    failed_payload = h_fresh3._freshz_payload_cached()
    check(failed_payload["sampled"] is False
          and failed_payload.get("sample_error") == "freshness_sample_failed"
          and SH._FRESHZ_CACHE.get("value") is None,
          "(I) /freshz sample failure/in-progress path is visible in the JSON and does NOT poison cache")
    ok_payload = h_fresh3._freshz_payload_cached()
    check(ok_payload["behind_count"] == 1 and fresh3.calls == 2,
          "(I) /freshz retries after a sample failure and then caches the success")

    os.environ["VERIPSA_FRESHZ_CACHE_SECONDS"] = "30"
    os.environ["VERIPSA_FRESHZ_SYNC_WAIT_SECONDS"] = "0.02"
    SH._FRESHZ_CACHE["value"] = None
    SH._FRESHZ_CACHE["at"] = 0.0
    SH._FRESHZ_REFRESH["thread"] = None
    SH._FRESHZ_REFRESH["key"] = None
    slow_db = _DBStub(payload={"coordinate_count": 7, "coordinates": [
        {"repo": "acme/app", "branch": "main", "age_seconds": 12}
    ]})
    slow_fresh = _FreshnessStub(delay=0.15, payload=[{"repo": "acme/app", "branch": "main", "behind": False}])
    S.graph_freshness_all = slow_fresh
    h_slow = _make_handler_instance(slow_db)
    _slow_t0 = time.monotonic()
    slow_first = h_slow._freshz_payload_cached()
    _slow_wall = time.monotonic() - _slow_t0
    check(_slow_wall < 0.10 and slow_first["sampled"] is False
          and slow_first["sample_error"] == "freshness_sample_in_progress"
          and slow_first["coordinate_count"] == 7,
          f"(I2) /freshz cache miss is bounded by sync-wait and returns explicit sampled:false fallback (wall={_slow_wall:.3f}s)")
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and SH._FRESHZ_CACHE.get("value") is None:
        time.sleep(0.02)
    slow_second = h_slow._freshz_payload_cached()
    check(slow_second["sampled"] is True and slow_second["behind_count"] == 0 and slow_fresh.calls == 1,
          "(I2) slow /freshz background sample populates cache for the next probe")

    os.environ["VERIPSA_FRESHZ_SYNC_WAIT_SECONDS"] = "0.02"
    os.environ["VERIPSA_FRESHZ_STALE_MAX_SECONDS"] = "300"
    SH._FRESHZ_CACHE["at"] = time.monotonic() - 60.0
    slow_fresh._delay = 0.15
    slow_fresh._payload = [{"repo": "acme/app", "branch": "main", "behind": True}]
    stale_payload = h_slow._freshz_payload_cached()
    check(stale_payload.get("stale_sample") is True and stale_payload["sampled"] is True
          and stale_payload["behind_count"] == 0,
          "(I3) expired /freshz cache returns the last successful sample marked stale while refresh runs")
finally:
    S.graph_freshness_all = _orig_graph_freshness_all


# (J) /readyz app identity cache caches successes only; failures remain fail-closed and retry every probe.
_orig_app_identity_ok = S.app_identity_ok
try:
    class _IdentityStub:
        def __init__(self, ok=True):
            self.calls = 0
            self.ok = ok

        def __call__(self, _dsn):
            self.calls += 1
            return (True, None) if self.ok else (False, "simulated identity failure")

    os.environ["VERIPSA_READYZ_IDENTITY_CACHE_SECONDS"] = "30"
    SH._READYZ_IDENTITY_CACHE["value"] = None
    SH._READYZ_IDENTITY_CACHE["at"] = 0.0
    ident1 = _IdentityStub(ok=True)
    S.app_identity_ok = ident1
    h_ident1 = _make_handler_instance(_DBStub())
    identities = [h_ident1._app_identity_cached() for _ in range(5)]
    check(ident1.calls == 1, f"(J) /readyz identity success within TTL: 5 probes ⇒ 1 check (got {ident1.calls})")
    check(all(x == (True, None) for x in identities),
          "(J) /readyz identity cached success preserves the exact result tuple")
    ident1_other = _IdentityStub(ok=False)
    S.app_identity_ok = ident1_other
    h_ident1_other = _make_handler_instance(_DBStub())
    check(h_ident1_other._app_identity_cached() == (False, "simulated identity failure")
          and ident1_other.calls == 1,
          "(J) /readyz identity cache is scoped to the identity checker and cannot reuse a stale success")

    SH._READYZ_IDENTITY_CACHE["value"] = None
    SH._READYZ_IDENTITY_CACHE["at"] = 0.0
    ident2 = _IdentityStub(ok=False)
    S.app_identity_ok = ident2
    h_ident2 = _make_handler_instance(_DBStub())
    failures = [h_ident2._app_identity_cached() for _ in range(3)]
    check(ident2.calls == 3,
          f"(J) /readyz identity failures are NOT cached: 3 probes ⇒ 3 checks (got {ident2.calls})")
    check(all(x == (False, "simulated identity failure") for x in failures),
          "(J) /readyz identity failure remains fail-closed on every probe")
finally:
    S.app_identity_ok = _orig_app_identity_ok


# (K) watchdog interval env is validated so 0/negative cannot spin or crash the monitor loop.
_old_wd_interval = os.environ.get("VERIPSA_WATCHDOG_INTERVAL")
try:
    os.environ.pop("VERIPSA_WATCHDOG_INTERVAL", None)
    check(SB._watchdog_interval_seconds() == 30.0,
          "(K) watchdog interval default is 30s when env is unset")
    for bad in ("0", "-5", "not-a-number", "inf", "nan"):
        os.environ["VERIPSA_WATCHDOG_INTERVAL"] = bad
        check(SB._watchdog_interval_seconds() == 30.0,
              f"(K) watchdog interval {bad!r} falls back to safe 30s")
    os.environ["VERIPSA_WATCHDOG_INTERVAL"] = "0.25"
    check(SB._watchdog_interval_seconds() == 1.0,
          "(K) watchdog interval tiny positive values clamp to 1s, not a busy loop")
    os.environ["VERIPSA_WATCHDOG_INTERVAL"] = "2.5"
    check(SB._watchdog_interval_seconds() == 2.5,
          "(K) watchdog interval valid positive float is preserved")
finally:
    if _old_wd_interval is None:
        os.environ.pop("VERIPSA_WATCHDOG_INTERVAL", None)
    else:
        os.environ["VERIPSA_WATCHDOG_INTERVAL"] = _old_wd_interval


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
# Final
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
# Restore env so a follow-on test in the same Python doesn't inherit a non-default TTL.
os.environ.pop("VERIPSA_HEALTHZ_FRESHNESS_CACHE_SECONDS", None)
os.environ.pop("VERIPSA_HEALTHZ_INBOX_DEPTH_CACHE_SECONDS", None)
os.environ.pop("VERIPSA_FRESHZ_CACHE_SECONDS", None)
os.environ.pop("VERIPSA_FRESHZ_SYNC_WAIT_SECONDS", None)
os.environ.pop("VERIPSA_FRESHZ_STALE_MAX_SECONDS", None)
os.environ.pop("VERIPSA_READYZ_IDENTITY_CACHE_SECONDS", None)
os.environ.pop("VERIPSA_PR_PREBRAIN_PARALLEL", None)

if FAIL:
    print("CORE-PERF FREE-TIER GATE: FAIL")
    sys.exit(1)
print("CORE-PERF FREE-TIER GATE: PASS")
