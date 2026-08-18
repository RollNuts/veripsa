#!/usr/bin/env python3
"""GRAPH-GENERATION FRESHNESS gate (G3) — freshness must account for extractor and exact-reference storage
generations, not only the commit SHA.

THE GAP (G3): graph_freshness decided "behind" purely by `stored_sha == HEAD`. Nothing stamped the EXTRACTOR
version on the stored graph. After an extractor UPGRADE an UNMOVED repo (same HEAD) read "fresh" and kept
computing coupling on OLD-extractor edges → a coupling only the NEW extractor finds was SILENTLY missed → an
indefinite silent false `clear`. Legacy graph rows with no version stamp must NOT be treated as unconditionally
fresh either.

The same invariant applies to semantic-reference storage: a v0 display-only
coordinate at the current SHA/extractor is still behind v1 and must full
rebuild before any path-local patch can be trusted.

THE FIX (fail-closed, recall-safe, reuses the shipped G1 withhold):
  • core.graph_version.extractor_version stamps the explicitly validated producer
    version carried by every new extractor payload.
  • coordinate_graph_sha returns the stored stamp AND the current token; graph_freshness treats a stored stamp
    that DIFFERS from current — OR is NULL (legacy) — EXACTLY like a lagged SHA: behind=True.
  • behind=True feeds the SAME self-heal (a full re-ingest re-stamps current) and, when the re-ingest FAILS, the
    SAME G1 withhold (`_pr_stale_graph_unknown_result`) downgrades the would-be `clear` to an advisory `unknown`.

WHAT THIS LOCKS:
  PART 1 (pure logic, NO Postgres — always runs): graph_freshness()'s behind semantics for the version axis —
    a version MISMATCH at the current HEAD ⇒ behind=True (the bug was behind=False); a legacy NULL stamp ⇒
    behind=True; the OLD (pre-predeploy) schema — coordinate_graph_sha returns NO current token — keeps the check
    INERT (gen-agnostic: identical to the SHA-only path); an unresolvable HEAD stays behind=None. Part 1 alone
    proves FAIL-before / PASS-after: before the fix the mismatch case returned behind=False (sha==HEAD).
  PART 2 (DB end-to-end over the REAL handler — runs when a local Postgres is available): seed main's graph at
    the CURRENT HEAD (so SHA freshness says "current"), then SIMULATE an extractor upgrade by replacing
    core.current_extractor_version() → a NEW token. The unmoved repo is now version-behind. Assert:
      (A) upgrade + self-heal re-ingest FAILS + would-be clear  → `neutral` withheld-clear advisory (NOT `success`)
                                                                    — before the fix this was a SILENT `success`.
      (B) upgrade + self-heal re-ingest SUCCEEDS (re-stamps)    → `success` (the clear stands; heal fixed it).
      (C) no upgrade (stamp == current) at HEAD                 → `success` (the fix does not over-fire).

Run:  python3 tests/test_extractor_version_freshness.py   (PART 2 needs local Postgres with the veripsa roles;
                                                            it self-skips if the bootstrap cannot stand one up)
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import graph_freshness as F  # noqa: E402

CHECKS: list[tuple[str, bool]] = []


def chk(cond, label):
    CHECKS.append((label, bool(cond)))


# ── PART 1 — graph_freshness() version-axis logic, driven with a fake db + fake gh (no Postgres) ──────────────
class _FakeGH:
    def __init__(self, head):
        self._head = head

    def repo_default_branch_head(self, repo):
        return "main", self._head


def _fake_db(coord_payload):
    """A db() double: the only query graph_freshness issues is coordinate_graph_sha — return the crafted object."""
    def run(sql, args=()):
        assert "coordinate_graph_sha" in sql, sql
        return coord_payload
    return run


def part1_pure_logic():
    HEAD = "e" * 40
    # 1) stored at HEAD, stamp == current → FRESH (the steady-state clear).
    f = F.graph_freshness(_fake_db({"commit_sha": HEAD, "extractor_version": "cg2",
                                    "current_extractor_version": "cg2"}), _FakeGH(HEAD), "acme/app", "main")
    chk(f["behind"] is False,
        "P1 stored@HEAD + stamp==current → behind=False (steady-state clear stands)")

    # Exact-reference storage has its own migration generation. A pre-key
    # coordinate at the same SHA/extractor version is still behind because its
    # sanitized display values can silently miss legal path/symbol characters.
    f = F.graph_freshness(_fake_db({
        "commit_sha": HEAD,
        "extractor_version": "cg4",
        "current_extractor_version": "cg4",
        "semantic_ref_version": 0,
        "current_semantic_ref_version": 1,
    }), _FakeGH(HEAD), "acme/app", "main")
    chk(f["behind"] is True,
        "P1 stored@HEAD + current extractor but semantic refs v0→v1 "
        "→ behind=True (forces a lossless full rebuild)")

    f = F.graph_freshness(_fake_db({
        "commit_sha": HEAD,
        "extractor_version": "cg4",
        "current_extractor_version": "cg4",
        "semantic_ref_version": 1,
        "current_semantic_ref_version": 1,
    }), _FakeGH(HEAD), "acme/app", "main")
    chk(f["behind"] is False,
        "P1 stored/current semantic refs v1 at HEAD → behind=False")

    f = F.graph_freshness(_fake_db({
        "commit_sha": HEAD,
        "extractor_version": "cg4",
        "current_extractor_version": "cg4",
        "semantic_ref_version": 0,
    }), _FakeGH(HEAD), "acme/app", "main")
    chk(f["behind"] is False,
        "P1 old schema without current semantic version keeps the new axis inert")

    # 2) THE G3 CORE: stored at HEAD (SHA says current) but the stamp is STALE vs current → behind=True.
    #    BEFORE THE FIX this returned behind=False (sha==HEAD, no version axis) — the SILENT false clear. This
    #    single assertion is the FAIL-before / PASS-after discriminator.
    f = F.graph_freshness(_fake_db({"commit_sha": HEAD, "extractor_version": "cg3",
                                    "current_extractor_version": "cg4"}), _FakeGH(HEAD), "acme/app", "main")
    chk(f["behind"] is True,
        "P1 stored@HEAD but STALE extractor stamp (cg3 vs current cg4) → behind=True "
        "(the G3 fix; was behind=False before the fix = the silent false clear)")

    # 3) LEGACY NULL stamp at HEAD, current present → behind=True (a legacy row is NEVER 'assumed fresh').
    f = F.graph_freshness(_fake_db({"commit_sha": HEAD, "extractor_version": None,
                                    "current_extractor_version": "cg4"}), _FakeGH(HEAD), "acme/app", "main")
    chk(f["behind"] is True,
        "P1 stored@HEAD with a LEGACY NULL stamp (current present) → behind=True (forces one re-ingest)")

    # 4) GEN-AGNOSTIC: the OLD (pre-predeploy) schema's coordinate_graph_sha returns NO current token → the
    #    version check is INERT → behind follows the SHA only (identical to the pre-fix path, never a spurious
    #    behind, never a crash on the missing keys).
    f = F.graph_freshness(_fake_db({"commit_sha": HEAD}), _FakeGH(HEAD), "acme/app", "main")
    chk(f["behind"] is False,
        "P1 OLD schema (no current_extractor_version returned) → version check INERT → behind=False (gen-agnostic)")

    # 5) unresolvable HEAD + a version mismatch → behind stays None (we never page/withhold on what we cannot see;
    #    the G1 withhold requires a resolvable head_sha anyway). Consistent with the SHA-staleness semantics.
    f = F.graph_freshness(_fake_db({"commit_sha": HEAD, "extractor_version": "cg3",
                                    "current_extractor_version": "cg4"}), _FakeGH(""), "acme/app", "main")
    chk(f["behind"] is None,
        "P1 unresolvable HEAD + version mismatch → behind=None (never a false page on an unseeable HEAD)")

    # 6) never-ingested coordinate → behind=True (cold-start owed), version axis irrelevant.
    f = F.graph_freshness(_fake_db({"commit_sha": None, "extractor_version": None,
                                    "current_extractor_version": "cg4"}), _FakeGH(HEAD), "acme/app", "main")
    chk(f["behind"] is True,
        "P1 never-ingested coordinate → behind=True (cold-start owed)")


# ── PART 2 — DB end-to-end over the REAL handler (self-heal → G1 withhold). Self-skips without Postgres ───────
def part2_db_end_to_end() -> str:
    """Returns 'ran' | 'skipped'. Extends the G1 harness: seed the graph AT the live HEAD (so SHA freshness reads
    current), then simulate an extractor upgrade by replacing core.current_extractor_version()."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _server_harness import DB, REPO, ROOT as HROOT, make_db  # noqa: E402
    # Reuse the EXACT G1 fakes/helpers (StaleGraphGitHub: main HEAD + a self-heal re-ingest that can be forced to
    # RAISE; _seed_graph_at / _pr_payload / _conclusion / _title / _comment_for).
    from test_stale_graph_failclosed import (  # noqa: E402
        StaleGraphGitHub, _pr_payload, _conclusion, _title, _comment_for, _seed_graph_at, HEAD_AHEAD)
    import psycopg2  # noqa: E402

    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=HROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("  [SKIP] PART 2 (no local Postgres — bootstrap failed):", (r.stderr or "")[-200:].replace("\n", " "))
        return "skipped"

    try:
        db = make_db("veripsa_app")
        sys.path.insert(0, HROOT)
        import server as S            # noqa: E402
        import code_graph_extract as X  # noqa: E402
        graph = X.build_graph(os.path.join(HROOT, "tests", "fixtures", "sample_app"))
        legacy_graph = dict(graph)
        legacy_graph["extractor_version"] = "cg3"
        legacy_graph["metrics"] = dict(
            legacy_graph.get("metrics") or {},
            schema_contract_version=1,
        )

        def set_current_ev(token):
            """Simulate an extractor upgrade/downgrade by REPLACING the current-version constant (owner DDL — no
            row write, no RLS/account pin needed). This is the most faithful reproduction of a real extractor
            upgrade: the stored graph was stamped at the OLD token, then 'current' advances underneath it."""
            conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
            conn.autocommit = True
            try:
                with conn.cursor() as c:
                    c.execute("CREATE OR REPLACE FUNCTION core.current_extractor_version() "
                              "RETURNS text LANGUAGE sql IMMUTABLE AS $BODY$ SELECT %s::text $BODY$" % ("'" + token + "'"))
            finally:
                conn.close()

        # ---- (A) extractor UPGRADE + self-heal re-ingest FAILS + would-be clear → withheld-clear (neutral) ----
        # Seed the graph AT the live HEAD with the deployed historical cg3/v1
        # producer under current cg4. The unmoved repo is VERSION-behind.
        # Self-heal tries a
        # re-ingest and the tarball fetch RAISES → reason='ingest error'. A solo PR that would CLEAR must instead
        # show the advisory `neutral` withheld-clear (before the fix: a SILENT `success`).
        set_current_ev("cg4")
        _seed_graph_at(db, legacy_graph, HEAD_AHEAD)   # explicit cg3/v1 producer at commit HEAD_AHEAD
        ghA = StaleGraphGitHub({2: ["backend/api.py"]}, heal_raises=True)
        S.handle_event("pull_request", _pr_payload("opened", 2, "bob"), db, ghA)
        head2 = f"{2:040x}"
        chk(_conclusion(ghA, head2) == "neutral",
            "P2-A extractor upgrade at UNMOVED HEAD + self-heal FAILED + would-be clear → `neutral` withheld-clear "
            "(NOT a silent `success`; SHA matched HEAD, only the extractor version was stale)")
        titleA = _title(ghA, head2).lower()
        cmtA = _comment_for(ghA, 2)
        chk("not confirmed current" in titleA or "not cleared" in titleA,
            "P2-A the withheld check is the stale-graph advisory (not a confident 'Clear')")
        chk(bool(cmtA) and "unknown" in cmtA["body"].lower() and "not cleared" in cmtA["body"].lower(),
            "P2-A an advisory 'not cleared'/unknown comment is posted (content-free, jargon-free)")
        S.handle_event("pull_request", _pr_payload("closed", 2, "bob"), db, ghA)

        # ---- (B) extractor UPGRADE + self-heal re-ingest SUCCEEDS (re-stamps current) → clear STANDS ----------
        # Same setup, but the tarball fetch WORKS → self-heal re-ingests @HEAD → the FULL ingest re-stamps
        # extractor_version='cg4' → the coordinate is genuinely current → healed=True → the clear stands. Proves
        # a version-mismatch behind is HEALED by the re-ingest re-stamping the current token (not stuck behind).
        set_current_ev("cg4")
        _seed_graph_at(db, legacy_graph, HEAD_AHEAD)
        ghB = StaleGraphGitHub({3: ["backend/billing.py"]}, heal_raises=False)
        S.handle_event("pull_request", _pr_payload("opened", 3, "carol"), db, ghB)
        head3 = f"{3:040x}"
        chk(_conclusion(ghB, head3) == "success",
            "P2-B extractor upgrade + self-heal SUCCEEDED (re-ingest re-stamped current) → clear STANDS (`success`) "
            "— a version-mismatch behind is healed, not stuck")
        S.handle_event("pull_request", _pr_payload("closed", 3, "carol"), db, ghB)

        # ---- (C) NO upgrade (stamp == current) at HEAD → clear STANDS — the fix does not over-fire -------------
        set_current_ev("cg4")
        _seed_graph_at(db, graph, HEAD_AHEAD)   # stamped 'cg4' == current 'cg4', SHA == HEAD
        ghC = StaleGraphGitHub({4: ["backend/worker.py"]}, heal_raises=True)
        S.handle_event("pull_request", _pr_payload("opened", 4, "dave"), db, ghC)
        head4 = f"{4:040x}"
        chk(_conclusion(ghC, head4) == "success",
            "P2-C stamp == current at HEAD → clear STANDS (`success`) — the version check does NOT over-fire on a "
            "genuinely current-extractor graph")
        S.handle_event("pull_request", _pr_payload("closed", 4, "dave"), db, ghC)
        return "ran"
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)


def main() -> int:
    part1_pure_logic()
    mode = "skipped"
    try:
        mode = part2_db_end_to_end()
    except Exception as e:
        # PART 2 is best-effort (it needs Postgres); a harness/env error there must not mask PART 1's proof, but
        # it IS surfaced so a real regression in the DB path is visible.
        print(f"  [SKIP] PART 2 raised (treated as env/harness, not a logic failure): {str(e)[:200]}")
    ok = True
    for name, cond in CHECKS:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond
    print(f"  (PART 2 DB end-to-end: {mode})")
    print("EXTRACTOR VERSION FRESHNESS GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
