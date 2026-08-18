#!/usr/bin/env python3
"""VERDICT-SPECTRUM PRECEDENCE GATE — locks the FULL classification ladder of core.main_impact_surface
on CONSTRUCTED REAL graphs (the extractor → schema → engine path, content-free), and the critical PRECEDENCE
the renderer's honesty depends on:

    serialize  >  serialize_soft  >  warn  >  unknown(ungraphed)  >  unknown(dampened)  >  clear

This is the VERDICT layer — DISTINCT from extractor precision/recall (whether the edge exists at all). Here the
edges are real (built by the extractor or hand-minted content-free schema rows); the question is whether the
engine assigns the RIGHT severity. Two failures it guards against:
  • a FALSE SERIALIZE (over-pause = wallpaper, the 品質=正確な沈黙 violation), and
  • a FALSE CLEAR  (a silent miss — the worst: "missed collision rendered safe", the thing Veripsa sells against).

THE BOUNDARIES (each MEASURED against the live db functions on a scratch DB):
  S1  DIRECT same-symbol same-file collision         → serialize        (a real wait, not warn/clear)
  S2  DIFFERENT-symbol same-file, disjoint lines      → NOT serialize    (the finer-collision recall-safe win;
                                                                          a same-FILE edit is NOT a false serialize)
  S3  low-value runner-file collision (append-order)  → serialize_soft   (demoted from hard, never silent)
  S4  semantic A→B with a DIFFERENT-author partner    → warn             (no silent miss, no over-pause)
  S5  ungraphed / new path (alone)                    → unknown          (honest "not analyzed", NOT a false clear)
  S6  genuinely isolated file (no coupling)           → clear            (clear is EARNED, only when truly uncoupled)

THE PRECEDENCE (a real collision is NEVER hidden behind an unknown path — the 80_contention CASE order):
  P1  same change has BOTH a real collision AND an ungraphed path → serialize  (COLLISION-BEATS-UNGRAPHED)
  P2  same change has BOTH a warn coupling   AND an ungraphed path → warn        (WARN-BEATS-UNGRAPHED)
  P3  a collision waiter that is ALSO dampened-coupled            → serialize  (COLLISION-BEATS-DAMPENED)
  P4  a change BOTH ungraphed AND dampened-coupled → unknown, and the dampened coupling is STILL SURFACED
      in dampened_with (the ungraphed 'unknown' does NOT MASK / silently swallow the dampened signal)

THE INTERACTION GUARDS (documented silent-miss / over-pause corners that the schema explicitly closes):
  G1  an owner-globbed .py (low_value_collision_globs='*.py') with a PROVEN SAME-SYMBOL collision → stays HARD
      'serialize', NOT softened (the #127 soften × #122 freshness silent-under-warn the guard prevents) — while a
      control proves the glob IS active (an un-mappable .py collision on the SAME glob softens to serialize_soft).
  G2  a change waiting on BOTH a low-value runner AND a real-source file → HARD 'serialize' (a low-value wait must
      never MASK a real collision on the same change).

HONEST-NO: a full audit of these boundaries found NO misclassification (no false-serialize, no false-clear) at the
verdict layer on these constructed graphs. This gate LOCKS that — a measured "every boundary classifies correctly
+ collision-beats-ungraphed holds" — so any future regression that introduces an over-pause or a silent miss fails.

Content-free throughout (paths / symbol NAMES / line numbers / counts only — no file bodies). Process-unique DB
(parallel-safe), bootstrap+drop like every sibling gate.

Run:  python3 tests/test_verdict_spectrum.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
DB = "veripsa_vspec_" + str(os.getpid())
REPO = "acme/vspec"
N = 12   # importers of the hub (> default hub cutoff 8 → hub.py is a dampened hub)


def db(role, sql, args=(), hub_degree=None):
    conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            if hub_degree is not None:
                cur.execute("SET veripsa.hub_degree = %s", (str(hub_degree),))
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def build_graph():
    """A REAL extracted Python graph carrying every shape the spectrum needs (spans + import edges from the
    actual extractor), PLUS a hand-minted (content-free) HUB so we can exercise the dampened-coupling axis."""
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        # SAME-FILE collision target: a module with two DISJOINT symbols (alpha, beta) + a top-level line.
        with open(os.path.join(d, "mod.py"), "w") as fh:
            fh.write("def alpha(x):\n    y = x + 1\n    return y\n\nTOP = 9\n\ndef beta(z):\n    w = z * 2\n    return w\n")
        # a second REAL-source module for the mixed low/real wait guard.
        with open(os.path.join(d, "core_mod.py"), "w") as fh:
            fh.write("def core_a(x):\n    return x\n\ndef core_b(y):\n    return y\n")
        # SEMANTIC A→B coupling: down.py imports up.py (down DEPENDS ON up).
        with open(os.path.join(d, "up.py"), "w") as fh:
            fh.write("def base():\n    return 1\n")
        with open(os.path.join(d, "down.py"), "w") as fh:
            fh.write("from up import base\n\ndef on_top():\n    return base()\n")
        # a genuinely ISOLATED file (nothing imports it, it imports nothing local) — must be 'clear'.
        with open(os.path.join(d, "island.py"), "w") as fh:
            fh.write("def alone():\n    return 7\n")
        # a low-value RUNNER file (built-in allowlist) for serialize_soft.
        with open(os.path.join(d, "run_gates.sh"), "w") as fh:
            fh.write("#!/usr/bin/env bash\necho a\necho b\necho c\n")
        # a .py the owner can glob as low-value but which HAS real symbols (the G1 misuse surface).
        with open(os.path.join(d, "list_reg.py"), "w") as fh:
            fh.write("def lr_alpha(x):\n    return x + 1\n\ndef lr_beta(z):\n    return z * 2\n")
        # a .py with NO span-mapped symbols (top-level list) — the G1 CONTROL (un-mappable → soften IS active).
        with open(os.path.join(d, "reglist.py"), "w") as fh:
            fh.write("ITEMS = [\n  1,\n  2,\n  3,\n]\n")
        # the HUB: imported by N leaves (in-degree N > 8 → dampened). Hand-minted is unnecessary — the extractor
        # builds the import edges for real; N real leaves make hub.py a true dampened hub.
        with open(os.path.join(d, "hub.py"), "w") as fh:
            fh.write("def helper():\n    return 1\n")
        for i in range(N):
            with open(os.path.join(d, f"leaf_{i}.py"), "w") as fh:
                fh.write(f"from hub import helper\n\ndef use_{i}():\n    return helper()\n")
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
        # base_hash="__FRESH__" → the SAME git-blob-sha main's graph stored for this file (so the finer engine's
        # spans are PROVABLY aligned to the diff lines → the symbol-level demotion fires). content-free.
        rj = json.dumps(ranges) if ranges else None
        bh = file_hashes.get(path) if base_hash == "__FRESH__" else base_hash
        db("veripsa_app", "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s)",
           (cid, path, REPO, "main", author, rj, bh))

    def set_policy(key, val):
        db("veripsa_app", "SELECT core.set_policy_with_authority(%s,%s)", (key, val))

    def reset():
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account','ACCT-DEMO',true)")
            cur.execute("SELECT set_config('core.governed_write_token','claim',true)")
            cur.execute("UPDATE core.claim SET claim_state='released', released_at=now() WHERE claim_state IN ('active','waiting')")
        conn.close()

    def impact(hub_degree=8):
        imp = db("veripsa_app", "SELECT core.main_impact_surface(%s,%s)", (REPO, "main"), hub_degree=hub_degree)
        if isinstance(imp, str):
            imp = json.loads(imp)
        return {c["change_id"]: c for c in imp.get("changes", [])}, imp

    checks = []

    # ── S1  DIRECT same-symbol same-file collision → serialize ────────────────────────────────────────────
    reset()
    claim("PR-A:mod.py", "mod.py", "alice", [[1, 2]])   # alpha
    claim("PR-B:mod.py", "mod.py", "bob", [[2, 3]])     # alpha (SAME symbol)
    ch, imp = impact()
    checks.append((f"S1 same-symbol collision → the waiter SERIALIZES (got '{ch.get('PR-B', {}).get('verdict')}', serialize_count={imp.get('serialize_count')})",
                   ch.get("PR-B", {}).get("verdict") == "serialize" and imp.get("serialize_count") == 1))

    # ── S2  DIFFERENT-symbol same-file, disjoint lines → NOT serialize (recall-safe finer win) ─────────────
    reset()
    claim("PR-C:mod.py", "mod.py", "carol", [[1, 3]])   # alpha
    claim("PR-D:mod.py", "mod.py", "dave", [[7, 9]])    # beta (DISJOINT)
    ch, imp = impact()
    checks.append((f"S2 disjoint-symbol same-FILE edit is NOT a false serialize (got '{ch.get('PR-D', {}).get('verdict')}', serialize_count={imp.get('serialize_count')})",
                   ch.get("PR-D", {}).get("verdict") != "serialize" and imp.get("serialize_count") == 0))

    # ── S3  low-value runner-file collision → serialize_soft (demoted, never silent) ──────────────────────
    reset()
    claim("PR-E:run_gates.sh", "run_gates.sh", "erin", [[2, 2]])
    claim("PR-F:run_gates.sh", "run_gates.sh", "fred", [[3, 3]])
    ch, imp = impact()
    checks.append((f"S3 low-value runner collision → serialize_SOFT, not hard (got '{ch.get('PR-F', {}).get('verdict')}', soft={imp.get('serialize_soft_count')}, hard={imp.get('serialize_count')})",
                   ch.get("PR-F", {}).get("verdict") == "serialize_soft" and imp.get("serialize_soft_count") == 1 and imp.get("serialize_count") == 0))

    # ── S4  semantic A→B with a DIFFERENT-author partner → warn (both sides) ──────────────────────────────
    reset()
    claim("PR-G:up.py", "up.py", "gail")        # editing the dependency
    claim("PR-H:down.py", "down.py", "hank")    # depends on up.py
    ch, imp = impact()
    checks.append((f"S4 semantic A→B both WARN — no silent miss, no over-pause (up='{ch.get('PR-G', {}).get('verdict')}', down='{ch.get('PR-H', {}).get('verdict')}', warn={imp.get('warn_count')})",
                   ch.get("PR-G", {}).get("verdict") == "warn" and ch.get("PR-H", {}).get("verdict") == "warn" and imp.get("warn_count") == 2))
    checks.append(("S4 the dependent's contested_with names the partner (the steer, content-free)",
                   any("gail" in str(x) for x in (ch.get("PR-H", {}).get("contested_with") or []))))

    # ── S5  ungraphed / new path (alone) → unknown (honest, NOT a false clear) ────────────────────────────
    reset()
    claim("PR-I:newfile.py", "newfile.py", "ivy")   # not a file node in main's graph
    ch, imp = impact()
    checks.append((f"S5 ungraphed new path → 'unknown' (honest not-analyzed), NEVER a false 'clear' (got '{ch.get('PR-I', {}).get('verdict')}')",
                   ch.get("PR-I", {}).get("verdict") == "unknown" and imp.get("clear_count") == 0))
    checks.append(("S5 the ungraphed path is surfaced in unknown_paths (the honesty is visible)",
                   "newfile.py" in (ch.get("PR-I", {}).get("unknown_paths") or [])))

    # ── S6  genuinely isolated file → clear (clear is EARNED) ─────────────────────────────────────────────
    reset()
    claim("PR-J:island.py", "island.py", "jade")
    ch, imp = impact()
    checks.append((f"S6 a genuinely isolated file → 'clear' (no coupling = clear is earned) (got '{ch.get('PR-J', {}).get('verdict')}')",
                   ch.get("PR-J", {}).get("verdict") == "clear" and imp.get("clear_count") == 1))

    # ══ PRECEDENCE — a real collision/warn is NEVER hidden behind an unknown path (80_contention CASE order) ══

    # ── P1  COLLISION-BEATS-UNGRAPHED: a change with BOTH a real same-symbol collision AND an ungraphed path. ─
    reset()
    claim("PR-K:mod.py", "mod.py", "kyle", [[1, 2]])                  # alpha holder
    claim("PR-L:mod.py", "mod.py", "liam", [[2, 3]])                  # alpha (collision with PR-K)
    claim("PR-L:newdir/brand_new.py", "newdir/brand_new.py", "liam")  # ungraphed, SAME change PR-L
    ch, imp = impact()
    prl = ch.get("PR-L", {})
    checks.append((f"P1 COLLISION-BEATS-UNGRAPHED: a change with a real collision + an ungraphed path → 'serialize', NOT 'unknown' (got '{prl.get('verdict')}')",
                   prl.get("verdict") == "serialize"))
    checks.append((f"P1 the collision is not hidden — the ungraphed path is STILL surfaced (unknown_paths={prl.get('unknown_paths')})",
                   "newdir/brand_new.py" in (prl.get("unknown_paths") or [])))

    # ── P2  WARN-BEATS-UNGRAPHED: a change with BOTH a semantic A→B coupling AND an ungraphed path. ─────────
    reset()
    claim("PR-M:up.py", "up.py", "mara")                              # editing dependency
    claim("PR-N:down.py", "down.py", "nora")                          # depends on up.py (warn)
    claim("PR-N:newdir/extra_new.py", "newdir/extra_new.py", "nora")  # ungraphed, SAME change PR-N
    ch, imp = impact()
    prn = ch.get("PR-N", {})
    checks.append((f"P2 WARN-BEATS-UNGRAPHED: a warn-coupled change + an ungraphed path → 'warn', NOT 'unknown' (got '{prn.get('verdict')}')",
                   prn.get("verdict") == "warn"))
    checks.append(("P2 the warn is not hidden — the ungraphed path is STILL surfaced",
                   "newdir/extra_new.py" in (prn.get("unknown_paths") or [])))

    # ── P3  COLLISION-BEATS-DAMPENED: a collision waiter that is ALSO dampened-coupled to an in-flight hub. ──
    reset()
    claim("PR-HOLD:leaf_0.py", "leaf_0.py", "holder", [[1, 1]])   # holder on leaf_0
    claim("PR-WAIT:leaf_0.py", "leaf_0.py", "waiter", [[1, 1]])   # waiter on leaf_0 (collision)
    claim("PR-HUB3:hub.py", "hub.py", "hubdev")                   # hub in-flight → dampened coupling to the leaf PRs
    ch, imp = impact()
    checks.append((f"P3 COLLISION-BEATS-DAMPENED: the collision waiter SERIALIZES even though it is also dampened-coupled (got '{ch.get('PR-WAIT', {}).get('verdict')}')",
                   ch.get("PR-WAIT", {}).get("verdict") == "serialize"))
    # the hub side (no direct collision) correctly stays 'unknown' (dampened), proving the two coexist.
    checks.append((f"P3 the hub side (dampened, no collision) stays honest 'unknown' (got '{ch.get('PR-HUB3', {}).get('verdict')}')",
                   ch.get("PR-HUB3", {}).get("verdict") == "unknown"))

    # ── P4  DAMPENED is NOT MASKED by an UNGRAPHED 'unknown': a change BOTH ungraphed AND dampened-coupled is
    #        'unknown' (both routes agree), and the dampened coupling is STILL SURFACED (not silently swallowed). ─
    reset()
    claim("PR-HUB2:hub.py", "hub.py", "hubdev")
    claim("PR-LEAFX:leaf_0.py", "leaf_0.py", "leafdev")                  # dampened importer of hub
    claim("PR-LEAFX:newdir/extra2.py", "newdir/extra2.py", "leafdev")    # ungraphed, SAME change PR-LEAFX
    ch, imp = impact()
    leafx = ch.get("PR-LEAFX", {})
    dw = leafx.get("dampened_with", []) or []
    checks.append((f"P4 ungraphed+dampened change → 'unknown' (got '{leafx.get('verdict')}')",
                   leafx.get("verdict") == "unknown"))
    checks.append((f"P4 the dampened coupling is STILL SURFACED — the ungraphed 'unknown' does NOT mask it (dampened_with={[ (e.get('by'), e.get('via_hub')) for e in dw ]})",
                   any(("hubdev" in str(e.get("by", "")) and e.get("via_hub") == "hub.py") for e in dw)))
    checks.append(("P4 the ungraphed path is ALSO surfaced (both honesty signals coexist)",
                   "newdir/extra2.py" in (leafx.get("unknown_paths") or [])))

    # ══ INTERACTION GUARDS — documented silent-miss / over-pause corners the schema explicitly closes ══

    # ── G1  owner-globbed .py with a PROVEN SAME-SYMBOL collision → stays HARD serialize (NOT softened). ────
    set_policy("low_value_collision_globs", "*.py")
    reset()
    claim("PR-LR1:list_reg.py", "list_reg.py", "lr1", [[1, 2]])   # lr_alpha
    claim("PR-LR2:list_reg.py", "list_reg.py", "lr2", [[2, 2]])   # lr_alpha (SAME symbol — proven 'same')
    ch, imp = impact()
    checks.append((f"G1 owner-globbed .py + PROVEN same-symbol collision STAYS HARD 'serialize' (not softened) — the #127×#122 silent-under-warn is prevented (got '{ch.get('PR-LR2', {}).get('verdict')}')",
                   ch.get("PR-LR2", {}).get("verdict") == "serialize"))
    # CONTROL: the SAME glob softens an UN-MAPPABLE (no-span, top-level) .py collision → proves the glob IS active.
    reset()
    claim("PR-RG1:reglist.py", "reglist.py", "rg1", [[2, 2]])   # top-level list line (no symbol span)
    claim("PR-RG2:reglist.py", "reglist.py", "rg2", [[3, 3]])   # top-level list line (un-mappable)
    ch, imp = impact()
    checks.append((f"G1 CONTROL: the SAME glob DOES soften an un-mappable .py collision → 'serialize_soft' (so G1's HARD is selective, not a dead glob) (got '{ch.get('PR-RG2', {}).get('verdict')}')",
                   ch.get("PR-RG2", {}).get("verdict") == "serialize_soft"))
    set_policy("low_value_collision_globs", "")   # restore (built-ins only) for G2

    # ── G2  a change waiting on BOTH a low-value runner AND a real-source file → HARD serialize (no masking). ─
    reset()
    claim("PR-H1:run_gates.sh", "run_gates.sh", "h1", [[1, 1]])    # holder on the runner (low-value)
    claim("PR-H2:core_mod.py", "core_mod.py", "h2", [[1, 2]])      # holder on real source (core_a)
    claim("PR-W:run_gates.sh", "run_gates.sh", "w", [[2, 2]])      # wait behind the runner (low-value)
    claim("PR-W:core_mod.py", "core_mod.py", "w", [[1, 2]])        # wait behind core_a (real, same symbol)
    ch, imp = impact()
    checks.append((f"G2 a low-value runner wait must NOT mask a real-source collision on the same change → HARD 'serialize' (got '{ch.get('PR-W', {}).get('verdict')}')",
                   ch.get("PR-W", {}).get("verdict") == "serialize"))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("VERDICT-SPECTRUM GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
