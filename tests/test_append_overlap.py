#!/usr/bin/env python3
"""APPEND OVERLAP GATE — locks the OVERLAPPING-APPEND recall guard on the append→serialize_soft demotion. #356
added relation 'append' to core._file_pair_symbol_overlap: when one side appends a brand-new function STRICTLY PAST
the file's last known symbol end-line (a provable past-EOF additive tail, no graph span) and the other side is ALSO
a past-max append OR a fully-mapped DISJOINT interior edit (sharing NO symbol), the file-level collision is KEPT but
SOFTENED to a non-blocking heads-up (serialize_soft / neutral, never action_required). That is correct for two
appends to DISJOINT tails — they cannot textually collide.

THE BUG THIS LOCKS (measured MED over-reach, round-4 new-code audit of #356): past_max is computed PER SIDE — each
side's lowest changed line is past the file's last symbol. It proves each side is a tail append INDEPENDENTLY, but
it does NOT prove the two appends are disjoint FROM EACH OTHER. Two PRs that BOTH insert at the SAME tail line range
(e.g. A on [11,14), B on [12,15) — both past max, but OVERLAPPING each other) both satisfy past_max, neither lands
in any existing symbol (so shares_symbol is false), and the pair was graded relation 'append' → softened to
serialize_soft. But two appends at the SAME insertion point ARE a real same-insertion-point textual conflict, NOT
the disjoint-tails multi-agent pattern — softening it silently DOWNGRADES a real collision. (The verdict ladder
collision/serialize > warn > clear > unknown; serialize_soft is a softened serialize, so this is a real-collision
under-warn — exactly what the demotion must not do.)

THE FIX (recall-safe, content-free): the 'append' arm now ALSO requires NOT ranges_overlap — the two sides' raw
changed line ranges (claim.touched_ranges, the SAME int4range data the finer mapping already uses; line numbers,
never code) must NOT overlap each other (`&&`). When they overlap, the pair is NOT 'append' → it falls back to
'unknown' = HARD (the file-level collision, recall preserved). Non-overlapping tails are UNCHANGED — still
serialize_soft, so #356's intent is preserved exactly.

THE BOUNDARIES (each MEASURED against the live db functions on a per-PID scratch DB):
  S1  two NON-overlapping appends, disjoint tails past EOF   → serialize_SOFT  (preserves #356 — still demoted, visible)
  S2  two NON-overlapping appends with a wide gap            → serialize_SOFT  (gap size irrelevant — #356 preserved)
  O1  two OVERLAPPING appends past EOF ([11,14) vs [12,15))  → serialize       (HARD — a real same-insertion conflict)
  O2  two appends at the SAME insertion point ([11,12) x2)   → serialize       (HARD — identical tail ranges collide)
  O3  the OVERLAPPING-append change is MATERIAL              → is_material_coupling(O1 change) is True (the pause-tier
                                                               link: hard serialize CAN be action_required — not neutral)

This gate FAILS on origin/main (where O1/O2 are wrongly serialize_soft) and PASSES on the fix. Process-unique DB
(parallel-safe); bootstrap+drop like every sibling gate. Run:  python3 tests/test_append_overlap.py
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
# each other's DB mid-run. Same convention as test_append_falsepause.py / test_verdict_spectrum.py.
DB = "veripsa_appovl_" + str(os.getpid())
REPO = "acme/appovl"


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
    PROVABLE append past EOF-at-base (no graph span) — the same fixture shape as the append false-pause gate."""
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "mod.py"), "w") as fh:
            fh.write("def alpha(x):\n    y = x + 1\n    return y\n\nTOP = 9\n\ndef beta(z):\n    w = z * 2\n    return w\n")
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

    # ══ S — NON-OVERLAPPING appends STAY serialize_soft (the fix must NOT regress #356's intent) ══

    # ── S1  two NON-overlapping appends, disjoint tails past EOF → serialize_SOFT (still demoted, still visible) ──
    reset()
    claim("PR-A:mod.py", "mod.py", "alice", [[11, 12]])   # append past max (end-line 9)
    claim("PR-B:mod.py", "mod.py", "bob",   [[14, 15]])   # ANOTHER append, DISJOINT lines
    ch, imp = impact()
    prb = ch.get("PR-B", {})
    checks.append((f"S1 NON-overlapping appends → serialize_SOFT (preserve #356) (got '{prb.get('verdict')}', "
                   f"soft={imp.get('serialize_soft_count')}, hard={imp.get('serialize_count')})",
                   prb.get("verdict") == "serialize_soft" and imp.get("serialize_soft_count") == 1 and imp.get("serialize_count") == 0))
    checks.append(("S1 the demoted collision is STILL VISIBLE (collision_points + serialize_behind present)",
                   bool(prb.get("collision_points")) and bool(prb.get("serialize_behind"))))

    # ── S2  two NON-overlapping appends with a wide gap (82 vs 200) → serialize_SOFT (gap size irrelevant) ───────
    reset()
    claim("PR-C:mod.py", "mod.py", "carol", [[82, 83]])
    claim("PR-D:mod.py", "mod.py", "dave",  [[200, 201]])
    ch, imp = impact()
    checks.append((f"S2 wide-gap non-overlap appends → serialize_SOFT (got '{ch.get('PR-D', {}).get('verdict')}', "
                   f"soft={imp.get('serialize_soft_count')}, hard={imp.get('serialize_count')})",
                   ch.get("PR-D", {}).get("verdict") == "serialize_soft" and imp.get("serialize_count") == 0))

    # ══ O — OVERLAPPING appends STAY HARD (the over-reach this gate locks; FAILS on origin/main) ══

    # ── O1  two OVERLAPPING appends past EOF ([11,14) vs [12,15)) → serialize (HARD, not softened) ───────────────
    # Both lowest lines (11, 12) are past the file max symbol end-line (9) → both past_max. But their ranges OVERLAP
    # each other → a real same-insertion-point textual conflict, NOT the disjoint-tails pattern. The demotion's
    # premise ("two appends to disjoint tails cannot collide") does NOT hold → must stay HARD serialize.
    reset()
    claim("PR-E:mod.py", "mod.py", "erin", [[11, 14]])    # append past max
    claim("PR-F:mod.py", "mod.py", "fred", [[12, 15]])    # append past max, OVERLAPS PR-E
    ch, imp = impact()
    prf = ch.get("PR-F", {})
    checks.append((f"O1 OVERLAPPING appends → HARD 'serialize' (NOT serialize_soft) (got '{prf.get('verdict')}', "
                   f"hard={imp.get('serialize_count')}, soft={imp.get('serialize_soft_count')})",
                   prf.get("verdict") == "serialize" and imp.get("serialize_count") == 1 and imp.get("serialize_soft_count") == 0))

    # ── O2  two appends at the SAME insertion point ([11,12) x2) → serialize (identical tail ranges collide) ─────
    reset()
    claim("PR-G:mod.py", "mod.py", "gail", [[11, 12]])
    claim("PR-H:mod.py", "mod.py", "hank", [[11, 12]])    # IDENTICAL append range
    ch, imp = impact()
    prh = ch.get("PR-H", {})
    checks.append((f"O2 SAME-insertion-point appends → HARD 'serialize' (got '{prh.get('verdict')}', "
                   f"hard={imp.get('serialize_count')}, soft={imp.get('serialize_soft_count')})",
                   prh.get("verdict") == "serialize" and imp.get("serialize_count") == 1 and imp.get("serialize_soft_count") == 0))

    # ── O3  the OVERLAPPING-append change is MATERIAL → the pause tier CAN pause it (the load-bearing link) ──────
    # is_material_coupling drives apply_pause_ack: a hard 'serialize' is material (True) so it CAN be action_required;
    # a softened append would be False (neutral). Proving the overlapping-append change is material confirms the
    # under-warn is closed at the surface the pause tier actually reads, not only in the verdict string.
    try:
        import render  # github-app/render.py (on sys.path above)
        reset()
        claim("PR-I:mod.py", "mod.py", "ivy",  [[11, 14]])
        claim("PR-J:mod.py", "mod.py", "jade", [[12, 15]])   # overlapping append
        ch, imp = impact()
        hard_me = ch.get("PR-J", {})
        material = render.is_material_coupling(hard_me)
        checks.append((f"O3 an overlapping-append (hard serialize) change is MATERIAL (is_material_coupling=True) → "
                       f"the pause tier may surface it as action_required (verdict='{hard_me.get('verdict')}', material={material})",
                       hard_me.get("verdict") == "serialize" and material is True))
    except Exception as e:  # render import should succeed; if not, FAIL loudly (don't silently skip the link)
        checks.append((f"O3 could not exercise render.is_material_coupling (import/eval error: {e!r})", False))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("APPEND OVERLAP GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
