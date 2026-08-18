#!/usr/bin/env python3
"""Gate: a warm self-heal patches only the changed paths instead of re-downloading the whole repo.

WHY. Outbound traffic is the service's dominant hosting cost — measured at **6.09 GB of 6.88 GB total**
egress, i.e. 88%, almost all of it repo tarball downloads for graph ingest. A payload-less heal has no push
payload, so `_push_changed_sets()` returns empty and `_reingest_graph`'s incremental branch could never be
taken: every heal pulled the ENTIRE repo (measured in production at 348–776 files) just to catch up a
single commit.

When both endpoints are known (a warm coordinate with a stored sha) the heal now asks GitHub which paths
actually changed and patches those only when the Compare response is provably complete. GitHub caps that
response at 300 files, and push webhooks cap their embedded commit list at 2048, so either ceiling must
fall back to the exact full build. This is purely a transfer-size optimisation — the resulting graph is
the same.

Pinned here, with a fake GitHub client so the assertions are about WHICH network call was made:

  A. WARM + small delta  -> compare() is consulted and NO tarball is downloaded.
  B. COLD (no stored sha) -> full tarball, because there is no base to compare against.
  C. compare() FAILS/empty -> full tarball. The optimisation must never turn a failure into a wrong graph.
  D. exactly 300 compare files -> full tarball because GitHub may have truncated the list.
  E. Push size mismatch / 2048-commit ceiling -> changed set is incomplete and forces full.
  F. Content-free: only path strings are taken from compare; no patch/diff body is ever requested or kept.

Run:  python3 tests/test_heal_incremental_bandwidth.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import ingest as I  # noqa: E402


class FakeGH:
    """Records which network calls happened. download_tarball is the expensive one."""

    def __init__(self, changed=None, compare_raises=False):
        self.changed = changed if changed is not None else []
        self.compare_raises = compare_raises
        self.calls = []

    def compare_changed_paths_proof(self, repo, base_sha, head_ref):
        self.calls.append("compare")
        if self.compare_raises:
            raise RuntimeError("compare failed")
        return {
            "paths": list(self.changed),
            "base_sha": base_sha,
            "merge_base_sha": base_sha,
            "head_sha": head_ref,
            "status": "ahead",
        }

    def download_tarball(self, repo, sha):
        self.calls.append("tarball")
        return b""

    def get_file_at(self, repo, sha, path):
        self.calls.append("file")
        return ""


def _run(monkey, stored_sha, changed, compare_raises=False, cap_over=False):
    """Drive self_heal_main_graph with the expensive paths stubbed, and report which calls happened."""
    gh = FakeGH(changed=changed, compare_raises=compare_raises)
    seen = {}

    def fake_ingest_push(db, gh_, repo, branch, sha, payload=None, coalesce=None,
                         captured_at=None, changed_paths=None,
                         changed_paths_base_sha=None,
                         changed_paths_failure_code=None):
        seen["changed_paths"] = changed_paths
        seen["changed_paths_base_sha"] = changed_paths_base_sha
        seen["changed_paths_failure_code"] = changed_paths_failure_code
        seen["captured_at"] = captured_at
        # Mirror the real decision: incremental only when a delta is supplied and within the cap.
        n = len(changed_paths or [])
        seen["mode"] = "patch" if (0 < n <= I._INCR_CAP) else "full"
        if seen["mode"] == "full":
            gh_.download_tarball(repo, sha)
        else:
            for _ in changed_paths:
                gh_.get_file_at(repo, sha, _)
        return {"mode": seen["mode"], "files": n, "edges": 0}

    def fake_freshness(db, gh_, repo, branch):
        return {"head_sha": "b" * 40, "stored_sha": stored_sha, "behind": True,
                "head_committed_at": "2026-07-26T00:00:00Z"}

    monkey["ingest_push"] = I.ingest_push
    monkey["graph_freshness"] = I.graph_freshness
    I.ingest_push = fake_ingest_push
    I.graph_freshness = fake_freshness
    try:
        res = I.self_heal_main_graph(lambda *a, **k: None, gh, "org/app", "main")
    finally:
        I.ingest_push = monkey["ingest_push"]
        I.graph_freshness = monkey["graph_freshness"]
    return gh, seen, res


def _run_ingest_push(payload, changed_paths=None):
    """Capture ingest_push's graph-build decision without doing DB/network work."""
    originals = {
        name: getattr(I, name)
        for name in (
            "_record_push_facts",
            "_dispatch_cochange",
            "_graph_would_regress",
            "_reingest_graph",
            "_reconcile_repo_identity",
        )
    }
    seen = {}

    def fake_reingest(
            db, gh, repo, branch, sha, push_payload, decision,
            changed, removed, head_time, *, changed_set_complete=True,
            changed_set_base_sha=None, changed_set_failure_code=None):
        seen["changed"] = list(changed)
        seen["removed"] = list(removed)
        seen["complete"] = changed_set_complete
        seen["base_sha"] = changed_set_base_sha
        seen["failure_code"] = changed_set_failure_code
        return {"mode": "full", "files": 0, "edges": 0}

    I._record_push_facts = lambda *a, **k: None
    I._dispatch_cochange = lambda *a, **k: None
    I._graph_would_regress = lambda *a, **k: False
    I._reingest_graph = fake_reingest
    I._reconcile_repo_identity = lambda *a, **k: None
    try:
        I.ingest_push(
            lambda *a, **k: None,
            object(),
            "org/app",
            "main",
            "b" * 40,
            payload=payload,
            changed_paths=changed_paths,
            changed_paths_base_sha=("a" * 40 if changed_paths else None),
        )
    finally:
        for name, value in originals.items():
            setattr(I, name, value)
    return seen


def _run_incomplete_reingest():
    """Exercise the real path selector: incomplete metadata must bypass patch."""
    originals = {
        name: getattr(I, name)
        for name in ("_coordinate_paths", "_incremental_ingest", "_full_ingest")
    }
    seen = {"incremental": 0, "full": 0}

    def fake_incremental(*args, **kwargs):
        seen["incremental"] += 1
        return {"mode": "patch", "files": 1, "edges": 0}

    def fake_full(
            *args, fallback_reasons=None, fallback_reason_codes=None, **kwargs):
        seen["full"] += 1
        seen["reason_codes"] = list(fallback_reason_codes or [])
        return {"mode": "full", "files": 1, "edges": 0}

    I._coordinate_paths = lambda *a, **k: ["src/a.py"]
    I._incremental_ingest = fake_incremental
    I._full_ingest = fake_full
    try:
        result = I._reingest_graph(
            lambda *a, **k: None,
            FakeGH(changed=["src/a.py"]),
            "org/app",
            "main",
            "b" * 40,
            {},
            "normal",
            ["src/a.py"],
            [],
            None,
            changed_set_complete=False,
        )
    finally:
        for name, value in originals.items():
            setattr(I, name, value)
    return seen, result


def main() -> int:
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    m = {}

    # A. warm + small delta -> compare consulted, NO tarball
    gh, seen, _ = _run(m, stored_sha="a" * 40, changed=["src/a.py", "src/b.py"])
    check("warm heal consults compare() for the changed paths", "compare" in gh.calls)
    check("warm heal downloads NO repo tarball (the bandwidth win)", "tarball" not in gh.calls)
    check("warm heal takes the incremental path", seen.get("mode") == "patch")
    check("only path strings are passed on (content-free)",
          seen.get("changed_paths") == ["src/a.py", "src/b.py"])
    check("heal threads the exact compare base SHA with its changed paths",
          seen.get("changed_paths_base_sha") == "a" * 40)
    check("the delivery-order clock is still supplied", bool(seen.get("captured_at")))

    # B. cold coordinate -> no base to compare against -> full
    gh, seen, _ = _run(m, stored_sha=None, changed=["src/a.py"])
    check("cold coordinate does NOT call compare (no base sha exists)", "compare" not in gh.calls)
    check("cold coordinate still does a full ingest", seen.get("mode") == "full" and "tarball" in gh.calls)

    # C. compare fails / returns empty -> must fall back to full, never a partial graph
    gh, seen, _ = _run(m, stored_sha="a" * 40, changed=[], compare_raises=True)
    check("a failed compare falls back to the FULL ingest (never a partial graph)",
          seen.get("mode") == "full" and "tarball" in gh.calls
          and seen.get("changed_paths_failure_code")
          == "compare_history_unproven")
    gh, seen, _ = _run(m, stored_sha="a" * 40, changed=[])
    check("a proven-empty compare is not patchable and takes the FULL ingest",
          seen.get("mode") == "full" and "tarball" in gh.calls)

    # D. The heal has a request/byte budget far below the generic work cap.
    over_heal_budget = [
        f"src/h{i}.py" for i in range(I._HEAL_PATCH_MAX_PATHS + 1)
    ]
    gh, seen, _ = _run(
        m, stored_sha="a" * 40, changed=over_heal_budget)
    check("a heal delta above its patch budget takes one tarball and zero file fetches",
          seen.get("changed_paths") is None
          and seen.get("mode") == "full"
          and gh.calls.count("tarball") == 1
          and gh.calls.count("file") == 0)
    at_heal_budget = [
        f"src/e{i}.py" for i in range(I._HEAL_PATCH_MAX_PATHS)
    ]
    gh, seen, _ = _run(m, stored_sha="a" * 40, changed=at_heal_budget)
    check("the heal patch budget boundary is inclusive",
          seen.get("mode") == "patch"
          and "tarball" not in gh.calls
          and gh.calls.count("file") == I._HEAL_PATCH_MAX_PATHS)
    check("the heal byte budget is tighter than the generic incremental work cap",
          I._HEAL_PATCH_MAX_PATHS < I._INCR_CAP)

    # GitHub Compare returns at most 300 files and provides no truncation bit.
    at_compare_cap = [
        f"src/f{i}.py" for i in range(I._GITHUB_COMPARE_FILE_CAP)
    ]
    gh, seen, _ = _run(m, stored_sha="a" * 40, changed=at_compare_cap)
    check("a compare response at GitHub's 300-file ceiling takes the FULL path",
          seen.get("changed_paths") is None
          and seen.get("mode") == "full"
          and "tarball" in gh.calls)
    valid_proof = {
        "paths": ["src/a.py"],
        "base_sha": "a" * 40,
        "merge_base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "status": "ahead",
    }

    class _Proof:
        def __init__(self, proof):
            self.proof = proof

        def compare_changed_paths_proof(self, *args):
            return self.proof

    check("an ancestry-bound compare proof is patchable",
          I._complete_compare_changed_paths(
              _Proof(valid_proof), "org/app", "a" * 40, "b" * 40
          ) == ["src/a.py"])
    for label, mutation in (
        ("merge base differs", {"merge_base_sha": "c" * 40}),
        ("history is diverged", {"status": "diverged"}),
        ("response head differs", {"head_sha": "c" * 40}),
        ("proof omits its base", {"base_sha": None}),
    ):
        invalid = {**valid_proof, **mutation}
        check(f"compare proof with {label} takes the FULL path",
              I._complete_compare_changed_paths(
                  _Proof(invalid), "org/app", "a" * 40, "b" * 40
              ) is None)

    # E. Push webhook completeness: never stamp a partial 2048-commit payload as current.
    good_commit = {"added": [], "modified": ["src/a.py"], "removed": []}
    small_push = _run_ingest_push(
        {"before": "a" * 40, "after": "b" * 40,
         "size": 1, "commits": [good_commit]}
    )
    check("a small, well-formed push proves a complete changed set",
          small_push.get("complete") is True
          and small_push.get("base_sha") == "a" * 40)
    missing_after = _run_ingest_push(
        {"before": "a" * 40, "size": 1, "commits": [good_commit]}
    )
    check("a push without its target SHA cannot authorize a patch",
          missing_after.get("complete") is False)
    mismatched_after = _run_ingest_push(
        {"before": "a" * 40, "after": "c" * 40,
         "size": 1, "commits": [good_commit]}
    )
    check("payload.after must equal the graph target SHA",
          mismatched_after.get("complete") is False)
    size_mismatch = _run_ingest_push(
        {"before": "a" * 40, "after": "b" * 40,
         "size": 2, "commits": [good_commit]}
    )
    check("push size != delivered commits marks the changed set incomplete",
          size_mismatch.get("complete") is False)
    capped_push = _run_ingest_push(
        {
            "before": "a" * 40,
            "after": "b" * 40,
            "size": I._GITHUB_PUSH_COMMIT_CAP,
            "commits": [good_commit] * I._GITHUB_PUSH_COMMIT_CAP,
        }
    )
    check("GitHub's 2048-commit webhook ceiling marks the changed set incomplete",
          capped_push.get("complete") is False)
    malformed_push = _run_ingest_push(
        {"before": "a" * 40, "after": "b" * 40,
         "commits": [{"added": "src/a.py", "modified": [], "removed": []}]}
    )
    check("malformed push commit/path arrays fail closed to a full build",
          malformed_push.get("complete") is False)
    selector, selection = _run_incomplete_reingest()
    check("an incomplete changed set bypasses incremental and records a full fallback",
          selector.get("incremental") == 0
          and selector.get("full") == 1
          and selection.get("mode") == "full"
          and "unpatchable_changed_set" in selector.get("reason_codes", []))

    explicit = [f"src/e{i}.py" for i in range(I._INCR_CAP + 1)]
    payloadless = _run_ingest_push(None, changed_paths=explicit)
    check("payload-less changed paths are never silently sliced at the incremental cap",
          payloadless.get("complete") is True
          and payloadless.get("changed") == explicit
          and payloadless.get("base_sha") == "a" * 40)

    # F. the fake client offers no patch/diff accessor at all, and nothing tried to reach for one
    check("no diff/patch body is ever requested (only compare + per-path file reads)",
          set(gh.calls) <= {"compare", "tarball", "file"})

    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"HEAL INCREMENTAL BANDWIDTH GATE: FAIL ({len(failed)} of {len(results)})")
        for n in failed:
            print("  -", n)
        return 1
    print(f"HEAL INCREMENTAL BANDWIDTH GATE: PASS ({len(results)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
