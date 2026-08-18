#!/usr/bin/env python3
"""CALL-EDGE PRECISION — investigation result + recall-safety GUARD (quality/call-edge-precision lane).

THE FINDING (fleet, 2026-06-19): the extractor emits `calls` edges (file -> callee NAME). A TEST file
that calls a short/common name (`route`, `open`, `process`, `pop`, `parse`, …) with NO corroborating
import is low-confidence — yet the contention engine's `out_adj`/`in_adj` (db/schema/70_social.sql) KEEP
an import-UNconfirmed call edge whenever the name has a SINGLE definer (the "unambiguous" branch). So a
test calling a common method defined once in production becomes a CONFIDENT `calls` coupling between that
test's PR and the prod file's PR — even with no import. The existing PROD->TEST guard does NOT catch it
(it only drops the case where the DEFINER is a test). This is a cry-wolf source.

WHAT THIS GATE PROVES (deterministic, hermetic, no DB — the synthetic half), and what the INVESTIGATION
measured on real repos (the numbers below, reproducible with the probes referenced in the PR):

  1. The false-coupling source EXISTS and is the dominant CALL-graph cry-wolf class. Measured (build_graph
     + the engine's exact stoplist / defs_ok(<=3) / hub / import-confirmation rules) on real repos:
       flask   151 test-origin import-unconfirmed single-definer call edges (80 distinct file pairs)
       axios   170     "                                                      "
       sinatra 165     "                                                      "
       zustand  24 (29 file pairs)
     (gin: 0 — it has no test-as-caller pattern; the class is language/style specific, not universal.)

  2. DROPPING them is NOT recall-safe — they carry REAL coupling. Co-change ground truth (>=3 co-commits,
     lift>=2) over real history (flask 5539 commits, zustand 1370): dropping ALL test-origin unconfirmed
     single-definer call couplings loses co-change recall:
       flask    pairs 212 -> 125, but co-change recall 67/348 -> 60/348  (LOST 7 real couplings)
       zustand  pairs  53 ->  47, but co-change recall 15/116 -> 11/116  (LOST 4 real couplings)
     A test legitimately co-changes with the production file it exercises; the call edge is the SOLE
     structural carrier of that coupling (measured: 0 of those 7+4 recall-bearing pairs are ALSO covered
     by an import edge). So a DROP = the exact silent-miss failure Veripsa sells against.

  3. There is NO recall-safe NAME discriminator in the extractor. The names driving RECALL-BEARING
     test-origin pairs (`pop`,`push`,`response`,`parse`,`combine`,`shallow`,…) are exactly as generic as
     the names driving NOISE pairs (`route`,`open`,`command`,`extend`,…) — they overlap in genericness, so
     no length/stoplist threshold separates real from false without losing the measured recall.

THE RECALL-SAFE DEMOTION ("keep the edge, render 'unknown' not a confident 'calls'") therefore CANNOT be
done in the extractor: the engine stores only (src, dst, edge_kind); edge_kind is pinned by a CHECK
constraint to {contains,calls,imports,queries,alters,reads_config} (db/schema/20_core.sql) AND the gate
ingest WHERE-clause (db/schema/30_gate.sql) DROPS any edge whose kind is outside that set. So emitting a
`calls` edge with kind='unknown' (or any new lower-confidence kind) would be SILENTLY DROPPED at ingest =
recall loss, the worse outcome. The engine's real recall-safe demotion already exists — the `dampened`
mechanism renders a suppressed-but-real coupling as verdict 'unknown' WITH `dampened_with`, never a
confident 'clear', never dropped (db/schema/80_contention.sql) — but it lives in the SQL engine's
`out_adj`/`in_adj`/`dampened`, which this lane MUST NOT edit. The correct, recall-safe fix is a SYMMETRIC
test-as-caller guard in those CTEs that routes a test-origin import-unconfirmed single-definer call into
the `dampened` set (so it renders 'unknown'+dampened_with), mirroring the existing prod->test guard —
a SQL-engine change, handed off here, NOT forced into the extractor where it could only DROP.

This gate is the RECALL-SAFETY GUARD for that hand-off: it pins that the thin test-origin call edge is
emitted as a NORMAL `calls` edge (never dropped, never minted into a non-ingestible kind), so a future
agent cannot "fix precision" by dropping it (which the numbers above prove regresses recall). It also
pins that the EXISTING precision (import-confirmed kept, prod-origin single-definer kept) is intact.

Hermetic: synthetic source files in a temp dir; reads the RAW edges build_graph emits. No DB, no network.
Content-free: asserts on edge KINDS + dst NAMES only.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402

# The engine's permitted edge kinds (db/schema/20_core.sql code_edge_kind_check + db/schema/30_gate.sql
# ingest WHERE). An edge whose kind is NOT in this set is SILENTLY DROPPED at ingest — so the extractor
# can never "demote" a call to a lower-confidence kind without losing the edge. Pinned here so a future
# attempt to mint a 'calls_weak'/'unknown' edge kind fails THIS gate (it would be dropped in production).
_INGESTIBLE_EDGE_KINDS = frozenset({"contains", "calls", "imports", "queries", "alters", "reads_config"})

_TEST_SEG = ("test", "tests", "spec", "specs", "__tests__", "examples", "example")


def _is_test(p):
    return any(seg.lower() in _TEST_SEG for seg in p.split("/"))


def _build(files):
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        return X.build_graph(d)


def main():
    checks = []

    # ── Synthetic repo exercising the three call shapes, mirroring the real-repo finding ──────────────
    #   * tests/test_service.py calls process()  with NO import of service  → THIN TEST-ORIGIN UNCONFIRMED
    #     (single definer in prod) — the cry-wolf class the fleet flagged.
    #   * app/caller.py imports app.service + calls validate()              → IMPORT-CONFIRMED (high conf).
    #   * app/other.py calls validate()         with NO import              → PROD-ORIGIN single-definer
    #     unconfirmed (the engine KEEPS this — and SHOULD: prod single-definer is real coupling).
    g = _build({
        "app/service.py": "def process(x):\n    return x\n\ndef validate(x):\n    return True\n",
        "tests/test_service.py": "def test_it():\n    return process(1)\n",
        "app/caller.py": "from app.service import validate\n\ndef run():\n    return validate(2)\n",
        "app/other.py": "def go():\n    return validate(3)\n",
    })
    edges = g["edges"]
    calls = {(e["src"], e["dst"]) for e in edges if e["kind"] == "calls"}
    kinds = {e["kind"] for e in edges}

    # (1) RECALL-SAFETY: every edge the extractor emits is an INGESTIBLE kind — no edge is minted into a
    #     non-ingestible (would-be-dropped) kind. This is what makes "demote to a new kind" impossible
    #     recall-safely in the extractor (it pins WHY the fix must live in the SQL engine, not here).
    bad_kinds = sorted(kinds - _INGESTIBLE_EDGE_KINDS)
    checks.append((f"all emitted edge kinds are ingestible (no silent-drop kind); kinds={sorted(kinds)}",
                   not bad_kinds))

    # (2) RECALL GUARD: the THIN TEST-ORIGIN call edge is PRESENT as a normal `calls` edge — NEVER dropped
    #     and NEVER demoted into a non-`calls` kind. The real-repo numbers prove dropping it costs co-change
    #     recall (flask 7, zustand 4 real couplings), so a future "precision fix" that removes it must FAIL.
    test_origin = ("tests/test_service.py", "process")
    checks.append((f"thin test-origin unconfirmed call edge is KEPT as a `calls` edge (recall-safe) {test_origin}",
                   test_origin in calls))
    checks.append(("the kept test-origin call uses the `calls` kind, not a fabricated lower-confidence kind",
                   any(e["kind"] == "calls" and (e["src"], e["dst"]) == test_origin for e in edges)))

    # (3) EXISTING PRECISION INTACT: the import-confirmed call and the prod-origin single-definer call are
    #     both still emitted (the engine keeps these as confident — correctly). The extractor must not have
    #     started dropping them.
    checks.append(("import-confirmed call (caller imports service, calls validate) is present",
                   ("app/caller.py", "validate") in calls))
    checks.append(("prod-origin single-definer unconfirmed call (other.py -> validate) is present",
                   ("app/other.py", "validate") in calls))

    # (4) DETERMINISTIC CLASSIFICATION: the synthetic graph contains exactly ONE test-origin call edge and
    #     it targets a name defined exactly once in a NON-test (prod) file — i.e. the precise cry-wolf class
    #     (test-as-caller, import-unconfirmed, single prod definer). This pins the class definition so the
    #     SQL-side guard (the hand-off) has a deterministic fixture to assert against.
    def_paths = {}
    for n in g["nodes"]:
        if n.get("kind") in ("def", "class") and n.get("name"):
            def_paths.setdefault(n["name"], set()).add(n["path"])
    imp_by_src = {}
    for e in edges:
        if e["kind"] == "imports":
            imp_by_src.setdefault(e["src"], set()).add(e["dst"])
    cry_wolf = []
    for (src, name) in calls:
        definers = def_paths.get(name, set())
        if len(definers) != 1:
            continue
        d = next(iter(definers))
        if d == src:
            continue
        confirmed = bool(imp_by_src.get(src, set()) & definers)  # resolved import to the definer file
        if confirmed:
            continue
        if _is_test(src) and not _is_test(d):
            cry_wolf.append((src, name, d))
    checks.append((f"exactly ONE thin test-origin single-definer cry-wolf call in the fixture: {cry_wolf}",
                   cry_wolf == [("tests/test_service.py", "process", "app/service.py")]))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("CALL EDGE PRECISION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
