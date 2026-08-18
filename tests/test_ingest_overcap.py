#!/usr/bin/env python3
"""INGEST OVER-CAP gate — the cost-guard probe must count what build_graph ACTUALLY ingests, not every file.

THE DEFECT (verified, MED — correctness + billing). _full_ingest (github-app/ingest.py) has a COST/SCALE
guard: before the expensive build_graph it probes the file count with _count_files and, if it is over
_MAX_INGEST_FILES (=12000), it stores an EMPTY graph (honest 'unknown', never an OOM-risking partial). The
guard is right; the COUNT was wrong. _count_files counted ALL regular files on disk — including
node_modules/vendor/dist/docs/images — BUT build_graph then SKIPS exactly those trees: it prunes _SKIP_DIRS,
drops files in non-source extensions (docs/images/data — not in _SOURCE_EXT, the is_noncode_path family), and
excludes generated/vendored files (_is_generated). So a repo with ~100 real code files + ~12,900 vendored/doc
files (bloat build_graph would NEVER touch) TRIPPED the cap → the coordinate was stored as an EMPTY graph →
the public meter (core.account_coverage_surface) then read 0 files / 0 coverage. Effect: a legitimately-
analyzable repo silently got NO analysis (and, reading as 0 files, was never even nudged to upgrade → stayed
free) purely because of bloat it would never analyze. Not customer-gaming (they LOSE the product), but a real
correctness + billing defect, and nothing covered it.

THE FIX (zero-drift). _count_files now counts only what build_graph will actually ingest, by reusing the
extractor's OWN iterators — _iter_source_files + _iter_config_files, the EXACT functions build_graph calls,
sharing the SAME _SKIP_DIRS prune, the SAME _passes_file_guards generated/symlink/size/binary filter
(_is_generated), and the SAME _SOURCE_EXT/_CONFIG_EXTS allowlists (which exclude the is_noncode_path
docs/images/lock-file set). The count therefore CANNOT drift from what is actually ingested — a node
build_graph emits == a file one of those iterators yields. The DoS guard is unchanged: a repo with >12000
REAL analyzable code files still trips the cap (we count the right thing, we did NOT remove the cap).

This gate proves, all OFFLINE (a temp dir + an in-memory tarball + a recording fake db/gh — no Postgres):
  (a) BUG FIXED: a ~100-real-code + ~12,900-vendored/doc repo PASSES the cap (count ~100, not ~13,000) and
      _full_ingest INGESTS the real files (a NON-EMPTY graph is written), where the old count would have
      stored an empty graph.
  (b) COUNT == BUILD: _count_files == the number of file nodes build_graph actually emits (the alignment that
      makes drift impossible — they walk the identical file set).
  (c) DoS GUARD PRESERVED: a repo with >_MAX_INGEST_FILES REAL code files still trips the cap → _full_ingest
      stores an EMPTY graph (over_cap=True), exactly as the guard is meant to.
  (d) NON-DRIFT spot-check: a single vendored tree alone (under a _SKIP_DIRS dir) counts 0 — none of it is
      ever ingested, so none of it may count against the cap.

Run:  python3 tests/test_ingest_overcap.py    (OFFLINE — no database required)
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))   # exercise the REAL guard, not a re-implementation

import ingest  # noqa: E402  (the module under test — _count_files + _full_ingest)
import code_graph_extract as X  # noqa: E402  (the extractor whose skip predicates we must NOT drift from)

FAIL = 0


def _check(name, ok):
    global FAIL
    print(("PASS" if ok else "FAIL") + ": " + name)
    if not ok:
        FAIL += 1


# ---------------------------------------------------------------------------------------------------------
# Offline doubles: a fake gh whose download_tarball serves a tar built from a temp dir on disk, and a fake db
# that RECORDS the graph JSON _full_ingest writes (so we can assert empty vs non-empty without Postgres).
# ---------------------------------------------------------------------------------------------------------
class _RecordingDB:
    """Minimal db() callable: records the FIRST positional arg of every ingest_graph_with_authority call (the
    graph JSON), so the test can read back whether _full_ingest stored an empty or a real graph. Any other SQL
    (none on this path beyond the ingest write) just returns None. No Postgres — we test the GUARD DECISION."""
    def __init__(self):
        self.ingested_json = None
        self.commit_sha = None
        self.graph_hash = "d" * 64

    def __call__(self, sql, params=None):
        if "ingest_graph_with_authority" in sql and params:
            self.ingested_json = params[0]      # the json.dumps(graph) string the guard chose to store
            self.commit_sha = params[3]
            return {
                "ok": True,
                "graph_hash": self.graph_hash,
                "semantic_ref_version": ingest._SEMANTIC_REF_VERSION,
            }
        if "coordinate_graph_sha" in sql:
            return {
                "commit_sha": self.commit_sha,
                "graph_hash": self.graph_hash,
                "semantic_ref_version": ingest._SEMANTIC_REF_VERSION,
            }
        return None


class _TarballGH:
    """A GitHub client double whose download_tarball returns a gzip tar of `srcdir` nested under one top dir
    (exactly how GitHub tarballs nest the repo), so _full_ingest's _safe_extractall + _count_files + build_graph
    run the SAME way they do in production. Content comes straight off disk — no monkeypatching of build_graph."""
    def __init__(self, srcdir):
        self.srcdir = srcdir

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(self.srcdir, arcname="repo-" + sha[:7])
        return buf.getvalue()


def _write(path, data="x = 1\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(data)


def _make_repo_with_vendored_bloat(n_real, n_vendored, n_docs):
    """A temp repo: `n_real` real python modules under src/, `n_vendored` vendored JS files spread across
    several _SKIP_DIRS trees (node_modules / vendor / dist), and `n_docs` markdown docs + a like number of png
    images at the top level (non-source extensions). The vendored/doc/image files are EXACTLY what build_graph
    skips — so the cap probe must not count them."""
    d = tempfile.mkdtemp(prefix="veripsa_overcap_")
    for i in range(n_real):
        _write(os.path.join(d, "src", f"mod{i}.py"), f"def f{i}():\n    return {i}\n")
    # vendored under THREE distinct _SKIP_DIRS so we prove the prune, not one lucky dir name.
    per = max(1, n_vendored // 3)
    for skipdir in ("node_modules", "vendor", "dist"):
        for i in range(per):
            _write(os.path.join(d, skipdir, "pkg", f"v{i}.js"), "module.exports = {}\n")
    for i in range(n_docs):
        _write(os.path.join(d, "docs", f"doc{i}.md"), "# doc\n")          # non-source ext (is_noncode)
    for i in range(n_docs):
        with open(os.path.join(d, f"img{i}.png"), "wb") as fh:            # binary asset, non-source ext
            fh.write(b"\x89PNG\r\n")
    return d


def _file_node_count(root):
    """How many `file`-kind nodes build_graph ACTUALLY emits for `root` — the ground truth the cap probe must
    match (a build_graph run over the real tree, no fakes)."""
    g = X.build_graph(root)
    return sum(1 for n in g["nodes"] if n.get("kind") == "file")


def main():
    cap = ingest._MAX_INGEST_FILES
    print(f"_MAX_INGEST_FILES = {cap}")

    # -----------------------------------------------------------------------------------------------------
    # (a) BUG FIXED + (b) COUNT == BUILD: a ~100-real + ~12,900-vendored/doc repo passes the cap and ingests.
    # We size the bloat ABOVE the cap on disk (so the OLD all-files count would have tripped it) but the real
    # code far UNDER the cap. Keep n_real small so build_graph is cheap; the bloat dominates the disk file
    # count exactly as in the reported repro.
    # -----------------------------------------------------------------------------------------------------
    n_real = 100
    d = _make_repo_with_vendored_bloat(n_real=n_real, n_vendored=12900, n_docs=300)
    try:
        on_disk = sum(len(fs) for _dp, _dn, fs in os.walk(d))
        count = ingest._count_files(d)
        nodes = _file_node_count(d)
        # the precondition that makes this test meaningful: the OLD probe (every regular file) WOULD have
        # tripped the cap — i.e. there really is enough bloat to demonstrate the bug.
        _check("(precondition) on-disk regular files exceed the cap (the OLD all-files probe WOULD trip it)",
               on_disk > cap)
        _check("(a) vendored-bloat repo: _count_files is UNDER the cap (was over → empty graph before the fix)",
               count <= cap)
        _check("(b) COUNT == BUILD: _count_files equals the file-node count build_graph actually emits "
               f"(count={count}, build_nodes={nodes}) — cannot drift from what is ingested",
               count == nodes)
        _check("(b') the real code IS what is counted (~the real files, NOT the ~13k vendored/doc/image bloat)",
               count == n_real)

        # END-TO-END through the REAL _full_ingest guard: it must now BUILD + store a NON-EMPTY graph, where
        # before the fix the over-count would have made it store {"nodes":[],"edges":[]} (the silent miss).
        db = _RecordingDB()
        gh = _TarballGH(d)
        stats = ingest._full_ingest(db, gh, "acme/bloated", "main", "a" * 40, captured_at=None)
        stored = json.loads(db.ingested_json) if db.ingested_json else None
        _check("(a) _full_ingest does NOT flag over_cap on the vendored-bloat repo (it ingests, no empty graph)",
               stats.get("over_cap") is not True)
        _check("(a) _full_ingest STORED a NON-EMPTY graph (the real code is now analyzed, not silently dropped)",
               isinstance(stored, dict) and len(stored.get("nodes", [])) > 0)
        _check("(a) stored graph file-node count matches build_graph (the real files landed in the coordinate)",
               isinstance(stored, dict)
               and sum(1 for n in stored.get("nodes", []) if n.get("kind") == "file") == nodes)
        _check("(a) _full_ingest stats report the real file count (the public meter reads real coverage, not 0)",
               stats.get("files") == nodes and nodes == n_real)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # -----------------------------------------------------------------------------------------------------
    # (c) DoS GUARD PRESERVED: a repo with > cap REAL code files still trips the cap → empty graph (over_cap).
    # We do NOT remove the guard; we count the RIGHT thing, so a genuine giant monorepo of real code is still
    # refused (an arbitrary partial graph would mislead / an OOM would crash a small host).
    # -----------------------------------------------------------------------------------------------------
    d2 = tempfile.mkdtemp(prefix="veripsa_overcap_dos_")
    try:
        n_over = cap + 25
        for i in range(n_over):
            _write(os.path.join(d2, "src", f"mod{i}.py"), f"def f{i}():\n    return {i}\n")
        count2 = ingest._count_files(d2)
        _check("(c) DoS guard: > cap REAL code files → _count_files trips the cap (short-circuits past it)",
               count2 > cap)
        db2 = _RecordingDB()
        gh2 = _TarballGH(d2)
        stats2 = ingest._full_ingest(db2, gh2, "acme/giant", "main", "b" * 40, captured_at=None)
        stored2 = json.loads(db2.ingested_json) if db2.ingested_json else None
        _check("(c) DoS guard: _full_ingest flags over_cap on a > cap REAL-code repo (the guard is preserved)",
               stats2.get("over_cap") is True and stats2.get("files") == 0 and stats2.get("edges") == 0)
        _check("(c) DoS guard: an over-cap REAL-code repo stores an EMPTY graph (honest 'unknown', never OOM)",
               isinstance(stored2, dict) and stored2.get("nodes") == [] and stored2.get("edges") == [])
    finally:
        shutil.rmtree(d2, ignore_errors=True)

    # -----------------------------------------------------------------------------------------------------
    # (d) NON-DRIFT spot-check: a tree that is ONLY vendored/skip-dir/doc/image content counts ZERO — none of
    # it is ever ingested, so none of it may count against the cap (the precise thing the bug got wrong).
    # -----------------------------------------------------------------------------------------------------
    d3 = tempfile.mkdtemp(prefix="veripsa_overcap_vendoronly_")
    try:
        for skipdir in ("node_modules", "vendor", ".git", "dist", "build"):
            for i in range(50):
                _write(os.path.join(d3, skipdir, f"v{i}.js"), "module.exports = {}\n")
        for i in range(50):
            _write(os.path.join(d3, "docs", f"d{i}.md"), "# d\n")
        count3 = ingest._count_files(d3)
        nodes3 = _file_node_count(d3)
        _check("(d) a vendored/skip-dir/doc-ONLY tree counts ZERO files (build_graph ingests none of it)",
               count3 == 0 and nodes3 == 0)
    finally:
        shutil.rmtree(d3, ignore_errors=True)

    if FAIL == 0:
        print("INGEST OVERCAP GATE: PASS")
        return 0
    print(f"INGEST OVERCAP GATE: FAIL ({FAIL} check(s) failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
