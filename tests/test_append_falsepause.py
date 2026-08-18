#!/usr/bin/env python3
"""APPEND FALSE-PAUSE GATE — locks the recall-safe DEMOTION of a PROVABLE pure-append same-file overlap from a
HARD pause (serialize / action_required) to a non-blocking heads-up (serialize_soft / neutral), WITHOUT opening a
silent miss. Measured against the LIVE engine (core.main_impact_surface + core._file_pair_symbol_overlap) on a
constructed REAL extractor graph (content-free: paths / symbol NAMES / line numbers / counts only — no file bodies).

THE BUG THIS LOCKS (verified HIGH false-PAUSE — the canonical multi-agent pattern, so a false pause here is exactly
the wallpaper the pause tier must avoid): two PRs that EACH APPEND a brand-new, independent function to DISJOINT new
lines (past EOF-at-base) of the SAME real source file were graded `serialize` → hard PAUSE (action_required). An
appended new function has no graph span at base, so its changed range mapped to no symbol → side 'un-mapped' →
relation 'unknown' → the file-level collision was KEPT and escalated to a hard wait, even though the two appends
cannot textually collide and the engine's own merge_conflict_likely was False.

THE FIX (recall-safe): a changed range lying ENTIRELY BEYOND the file's highest known symbol end-line (purely
additive, past EOF-at-base) is PROVABLY distinguishable from an interior top-level edit. _file_pair_symbol_overlap
now returns relation 'append' when one side is such a past-max append AND the other is ALSO a past-max append OR a
fully-mapped DISJOINT interior edit (and they share NO symbol). The verdict ladder treats 'append' the SAME way it
treats a low-value runner-list overlap: KEEP the collision VISIBLE (serialize_soft) but DEMOTE it to NEUTRAL (never
action_required). is_material_coupling() returns False for serialize_soft, so the pause tier never pauses it.

THE BOUNDARIES (each MEASURED against the live db functions on a scratch DB):
  A1  append-vs-append, disjoint tails past EOF       → serialize_SOFT (demoted from hard) AND still VISIBLE
  A2  append-vs-append, 118-LINE gap (82 vs 200)       → serialize_SOFT  (the wide-gap repro — gap size irrelevant)
  A3  append-vs-DISJOINT-interior (tail vs an existing → serialize_SOFT  (one side appends, other edits a disjoint
      symbol, no shared symbol)                                          existing symbol → still pure-additive)
  A4  the demoted verdict is NON-MATERIAL              → is_material_coupling(serialize_soft change) is False
                                                         (the pause-tier link: serialize_soft never action_required)

THE RECALL CONTROLS (the fix must NOT introduce a silent miss — these STAY HARD / unchanged):
  R1  two edits to the SAME existing symbol            → serialize     (a real logic collision — unchanged)
  R2  two OVERLAPPING edits inside ONE symbol          → serialize     (still the same symbol — unchanged)
  R3  an append whose range SPILLS INTO an interior    → serialize     (NOT past-max on that side → 'unknown' → HARD;
      edit (overlaps a symbol body)                                     an append that touches interior code is never demoted)
  R4  two edits to existing DISJOINT symbols           → clear         (the pre-existing finer-collision win — DROP,
                                                                         not merely soften — must be preserved)
  R5  a change waiting on BOTH an append-soft AND a     → serialize     (the masking guard: a soft wait must NEVER
      real same-symbol collision (same change)                          hide a real collision on the same change)

Process-unique DB (parallel-safe); bootstrap+drop like every sibling gate. Run:  python3 tests/test_append_falsepause.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs drop
# each other's DB mid-run → "does not exist". Same convention as test_verdict_spectrum.py.
DB = "veripsa_append_" + str(os.getpid())
REPO = "acme/append"


def db(role, sql, args=()):
    conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def build_graph():
    """A REAL extracted Python graph. mod.py has alpha (lines 1-3), a TOP line (5), beta (lines 7-9): the file's
    MAX known symbol end-line is 9, and the file ends at line 9. A range whose lowest line is > 9 is therefore a
    PROVABLE append past EOF-at-base (no graph span). core_mod.py is a 2nd real-source file for the masking guard."""
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "mod.py"), "w") as fh:
            fh.write("def alpha(x):\n    y = x + 1\n    return y\n\nTOP = 9\n\ndef beta(z):\n    w = z * 2\n    return w\n")
        with open(os.path.join(d, "core_mod.py"), "w") as fh:
            fh.write("def core_a(x):\n    return x\n\ndef core_b(y):\n    return y\n")
        g = X.build_graph(d)
    return g


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-1200:]); return 1

    graph = build_graph()
    file_hashes = {n["path"]: n["content_hash"] for n in graph["nodes"]
                   if n.get("kind") == "file" and n.get("content_hash")}
    db("veripsa_app", "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", "a" * 40))

    def claim(cid, path, author, ranges=None, base_hash="__FRESH__"):
        # base_hash="__FRESH__" → the SAME git-blob-sha main's graph stored for this file, so the finer engine's
        # spans are PROVABLY aligned to the diff lines (freshness_ok) → the symbol-level append demotion can fire.
        # claim_id convention is "<change_id>:<path>" — split_part(claim_id,':',1) is the change_id, so a single
        # change can carry several paths (the masking-guard case). Content-free throughout.
        rj = json.dumps(ranges) if ranges else None
        bh = file_hashes.get(path) if base_hash == "__FRESH__" else base_hash
        db("veripsa_app", "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s)",
           (cid, path, REPO, "main", author, rj, bh))

    def reset():
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account','ACCT-DEMO',true)")
            cur.execute("SELECT set_config('core.governed_write_token','claim',true)")
            cur.execute("DELETE FROM core.claim")
        conn.close()

    def impact():
        imp = db("veripsa_app", "SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
        if isinstance(imp, str):
            imp = json.loads(imp)
        return {c["change_id"]: c for c in imp.get("changes", [])}, imp

    checks = []

    # ── A1  append-vs-append, disjoint tails past EOF → serialize_SOFT (demoted) AND still VISIBLE ──────────────
    reset()
    claim("PR-A:mod.py", "mod.py", "alice", [[11, 12]])   # a brand-new fn appended past the last symbol (end-line 9)
    claim("PR-B:mod.py", "mod.py", "bob",   [[14, 15]])   # ANOTHER brand-new fn appended, disjoint lines
    ch, imp = impact()
    prb = ch.get("PR-B", {})
    checks.append((f"A1 append-vs-append → serialize_SOFT, NOT a hard serialize/pause (got '{prb.get('verdict')}', "
                   f"soft={imp.get('serialize_soft_count')}, hard={imp.get('serialize_count')})",
                   prb.get("verdict") == "serialize_soft" and imp.get("serialize_soft_count") == 1 and imp.get("serialize_count") == 0))
    checks.append(("A1 the collision is STILL VISIBLE — demote does not silence it (collision_points non-empty, "
                   f"serialize_behind names the holder) (cp={prb.get('collision_points')})",
                   bool(prb.get("collision_points")) and bool(prb.get("serialize_behind"))))

    # ── A2  append-vs-append, 118-LINE gap (82 vs 200) → serialize_SOFT (gap size is irrelevant) ────────────────
    reset()
    claim("PR-C:mod.py", "mod.py", "carol", [[82, 83]])
    claim("PR-D:mod.py", "mod.py", "dave",  [[200, 201]])
    ch, imp = impact()
    checks.append((f"A2 append-vs-append with a 118-line gap → serialize_SOFT (got '{ch.get('PR-D', {}).get('verdict')}', "
                   f"soft={imp.get('serialize_soft_count')}, hard={imp.get('serialize_count')})",
                   ch.get("PR-D", {}).get("verdict") == "serialize_soft" and imp.get("serialize_count") == 0))

    # ── A3  append-vs-DISJOINT-interior (tail vs an existing disjoint symbol) → serialize_SOFT ──────────────────
    reset()
    claim("PR-E:mod.py", "mod.py", "erin", [[11, 12]])   # append past max
    claim("PR-F:mod.py", "mod.py", "fred", [[1, 2]])     # edits EXISTING alpha (disjoint from the append, no shared symbol)
    ch, imp = impact()
    checks.append((f"A3 append vs a DISJOINT existing symbol → serialize_SOFT (still pure-additive) "
                   f"(got '{ch.get('PR-F', {}).get('verdict')}', hard={imp.get('serialize_count')})",
                   ch.get("PR-F", {}).get("verdict") == "serialize_soft" and imp.get("serialize_count") == 0))

    # ── A4  the demoted verdict is NON-MATERIAL → the pause tier never pauses it (the load-bearing link) ────────
    # is_material_coupling drives apply_pause_ack: True → action_required (pause); False → never paused. It MUST be
    # False for a serialize_soft change, else the demotion would still pause. Proven on the REAL render predicate.
    try:
        import render  # github-app/render.py (on sys.path above)
        reset()
        claim("PR-G:mod.py", "mod.py", "gail", [[11, 12]])
        claim("PR-H:mod.py", "mod.py", "hank", [[14, 15]])
        ch, imp = impact()
        soft_me = ch.get("PR-H", {})
        material = render.is_material_coupling(soft_me)
        checks.append((f"A4 a serialize_soft append change is NON-MATERIAL (is_material_coupling=False) → the pause "
                       f"tier renders it neutral, never action_required (verdict='{soft_me.get('verdict')}', material={material})",
                       soft_me.get("verdict") == "serialize_soft" and material is False))
    except Exception as e:  # render import should succeed; if not, FAIL loudly (don't silently skip the link)
        checks.append((f"A4 could not exercise render.is_material_coupling (import/eval error: {e!r})", False))

    # ══ RECALL CONTROLS — the fix must NOT introduce a silent miss (these stay HARD / unchanged) ══

    # ── R1  two edits to the SAME existing symbol → serialize (a real collision — unchanged) ────────────────────
    reset()
    claim("PR-I:mod.py", "mod.py", "ivy",  [[1, 2]])   # alpha
    claim("PR-J:mod.py", "mod.py", "jade", [[2, 3]])   # alpha (SAME symbol)
    ch, imp = impact()
    checks.append((f"R1 same-symbol collision STAYS hard 'serialize' (got '{ch.get('PR-J', {}).get('verdict')}', "
                   f"hard={imp.get('serialize_count')})",
                   ch.get("PR-J", {}).get("verdict") == "serialize" and imp.get("serialize_count") == 1))

    # ── R2  two OVERLAPPING edits inside ONE symbol → serialize (still the same symbol) ─────────────────────────
    reset()
    claim("PR-K:mod.py", "mod.py", "kyle", [[7, 8]])   # beta (7-9)
    claim("PR-L:mod.py", "mod.py", "liam", [[8, 9]])   # beta (overlapping, same symbol)
    ch, imp = impact()
    checks.append((f"R2 overlapping edits inside one symbol STAY hard 'serialize' (got '{ch.get('PR-L', {}).get('verdict')}')",
                   ch.get("PR-L", {}).get("verdict") == "serialize" and imp.get("serialize_count") == 1))

    # ── R3  an append whose range SPILLS INTO an interior edit → serialize (NOT past-max → 'unknown' → HARD) ────
    # [8,12] starts at 8 (≤ the max symbol end-line 9) AND spills past 9 → it is NEITHER fully-mapped (it spills past
    # beta's body) NOR a pure past-max append (its lowest line 8 is not past 9) → relation 'unknown' → stays HARD.
    reset()
    claim("PR-M:mod.py", "mod.py", "mara", [[11, 12]])   # a pure append past max
    claim("PR-N:mod.py", "mod.py", "nora", [[8, 12]])    # overlaps beta's interior AND spills past EOF (NOT past-max)
    ch, imp = impact()
    checks.append((f"R3 an append that SPILLS INTO an interior symbol STAYS hard 'serialize' (not demoted) "
                   f"(got '{ch.get('PR-N', {}).get('verdict')}', hard={imp.get('serialize_count')})",
                   ch.get("PR-N", {}).get("verdict") == "serialize" and imp.get("serialize_count") == 1))

    # ── R4  two edits to existing DISJOINT symbols → clear (the pre-existing finer win — DROP, not soften) ──────
    reset()
    claim("PR-O:mod.py", "mod.py", "olga", [[1, 3]])   # alpha
    claim("PR-P:mod.py", "mod.py", "pete", [[7, 9]])   # beta (DISJOINT existing)
    ch, imp = impact()
    checks.append((f"R4 existing disjoint symbols STAY 'clear' (dropped, not merely softened) "
                   f"(got '{ch.get('PR-P', {}).get('verdict')}', clear={imp.get('clear_count')}, soft={imp.get('serialize_soft_count')})",
                   ch.get("PR-P", {}).get("verdict") == "clear" and imp.get("clear_count") == 2 and imp.get("serialize_soft_count") == 0))

    # ── R5  MASKING GUARD: a change waiting on BOTH an append-soft AND a real same-symbol collision → serialize ──
    reset()
    claim("PR-Q:mod.py",      "mod.py",      "quinn", [[11, 12]])   # holder: append tail on mod.py (would be soft)
    claim("PR-Q:core_mod.py", "core_mod.py", "quinn", [[1, 2]])     # holder: core_a on core_mod.py
    claim("PR-R:mod.py",      "mod.py",      "rosa",  [[14, 15]])   # waiter: append tail on mod.py (soft, vs PR-Q)
    claim("PR-R:core_mod.py", "core_mod.py", "rosa",  [[1, 2]])     # waiter: core_a — SAME symbol (real collision vs PR-Q)
    ch, imp = impact()
    prr = ch.get("PR-R", {})
    checks.append((f"R5 an append-soft wait must NOT mask a real same-symbol collision on the same change → HARD "
                   f"'serialize' (got '{prr.get('verdict')}', hard={imp.get('serialize_count')})",
                   prr.get("verdict") == "serialize" and imp.get("serialize_count") == 1))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("APPEND FALSEPAUSE GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
