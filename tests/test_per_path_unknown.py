#!/usr/bin/env python3
"""PER-PATH UNKNOWN gate — a PR that mixes IN-GRAPH files with NEW (not-in-graph) paths must get a PER-PATH
verdict, NEVER a blanket whole-PR "❓ Not analyzed" that suppresses the useful verdict on the in-graph part.

THE BUG: Veripsa flags a change 'unknown' the moment ANY reserved path is absent from main's
graph (a NEW file — a test, a new module). That was BLANKET: a synthetic PR adds new files while
new gates/tests) AND modified coupled IN-GRAPH files (30_gate / 35_lifecycle / webhook_handlers) → it got the
whole-PR "Unknown / ❓ Not analyzed: some of your changed paths aren't in main's graph", SUPPRESSING the real
verdict on the in-graph files. Since most real PRs add at least one new file, most PRs landed on 'unknown' →
the product rarely gave a useful verdict. This is a USEFULNESS bug, NOT a safety bug: honest-unknown is CORRECT;
the defect was that it was BLANKET when it should be PER-PATH.

THE FIX (github-app/render.py: _effective_verdict + _render_unverified_paths_note): the verdict is resolved for
the VERIFIABLE (in-graph) subset only when the renderer has positive Files-API evidence that EVERY unknown path
is NEW in this PR. The exact promotion gate is: all unknown paths are in `added_paths`, at least one known path
exists, no dampened coupling exists, and the analyzed path set is not truncated. Missing/failed added-path data,
one existing graph gap, an all-new PR, dampening, or an unparsed remainder all stay honest Unknown/neutral.
Unknown or missing evidence is never Clear.

This proves the fix END-TO-END against the REAL engine (core.main_impact_surface) + the REAL renderer
(render.render_pr_check) on a real ingested graph:

  A) THE #433 SHAPE — in-graph (clear) + a path positively identified as NEW: the engine verdict is 'unknown',
     but the renderer gives the VERIFIABLE part its real verdict — conclusion 'success', title "Clear" — and
     posts a separate note for the new path.

  B) CONSERVATIVE BOUNDARIES — added-path fetch failure, an existing graph gap, no known path, dampening,
     truncation, and a missing engine verdict all stay Unknown/neutral.

  C) A REAL COLLISION IS NEVER MASKED — a same-file collision on an in-graph file + a NEW path: the colliding
     PR still verdicts 'serialize' (conclusion 'neutral', "Wait in line") — the new path did NOT downgrade it to
     clear — AND the new path is STILL surfaced separately. The verifiable verdict and the honest new-paths note
     coexist.

Content-free throughout: only paths / counts / verdicts / conclusions are observed (no code bodies).

Run:  python3 tests/test_per_path_unknown.py   (needs local Postgres with the veripsa roles)
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
import code_graph_extract as X  # noqa: E402
import render as R  # noqa: E402  — exercise the REAL renderer, not a re-implementation

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
# Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_noncode_paths.py.
DB = "veripsa_perpath_" + str(os.getpid())
REPO = "acme/perpath"


def db(sql, args=()):
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _w(d, rel, body):
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(body)


def build_graph(d):
    # IN-GRAPH files: a coupled pair (uses.py imports base.py → an edge, for the collision/warn case) plus a
    # STANDALONE solo file (no coupling → a clean 'clear' baseline for the mixed-with-new-paths case).
    _w(d, "app/base.py", "def core_fn():\n    return 1\n")
    _w(d, "app/uses.py", "from app.base import core_fn\n\ndef caller():\n    return core_fn()\n")
    _w(d, "app/solo.py", "def solo_fn():\n    return 7\n")
    return X.build_graph(d)


def claim(cid, path, author):
    db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, REPO, "main", author))


def surface():
    imp = db("SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
    if isinstance(imp, str):
        imp = json.loads(imp)
    return imp


def main() -> int:
    checks = []

    # sanity: the new paths we'll claim ARE real code (kept by the server filter, not dropped as docs) — so the
    # ONLY reason they're un-analyzable is that they're not yet in main's graph, exactly the #433 condition.
    for p in ("brand_new_module.py", "tests/test_brand_new.py", "db/schema/99_least_privilege.sql"):
        checks.append((f"setup: {p!r} is CODE (not a doc/asset filtered out) — so its 'unknown' is the new-path "
                       f"condition, not a non-code drop", not X.is_noncode_path(p)))

    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    with tempfile.TemporaryDirectory() as d:
        graph = build_graph(d)
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", "a" * 40))

    # ── A) THE #433 SHAPE: an IN-GRAPH solo file (clear) + a NEW file. The engine verdict is 'unknown' (a new
    #       path is present), but the renderer must give the VERIFIABLE part its real verdict and note the new
    #       path separately — never blanket the whole PR to "Not analyzed".
    claim("PR-MIX:app/solo.py", "app/solo.py", "mixdev")
    claim("PR-MIX:brand_new_module.py", "brand_new_module.py", "mixdev")

    # ── B) HONEST RECALL: a PR where EVERY path is new (no verifiable subset) must STAY 'unknown'.
    claim("PR-ALLNEW:tests/test_brand_new.py", "tests/test_brand_new.py", "newdev")
    claim("PR-ALLNEW:db/schema/99_least_privilege.sql", "db/schema/99_least_privilege.sql", "newdev")

    # ── C) A REAL COLLISION + a new path: PR-COLL and PR-HOLD both claim the SAME in-graph file app/base.py (a
    #       direct same-file collision → serialize), and PR-COLL ALSO claims a new file. The collision must NOT
    #       be downgraded to clear by the new path, AND the new path must still surface.
    claim("PR-HOLD:app/base.py", "app/base.py", "holddev")
    claim("PR-COLL:app/base.py", "app/base.py", "colldev")
    claim("PR-COLL:another_new.py", "another_new.py", "colldev")

    imp = surface()
    by = {c["change_id"]: c for c in imp.get("changes", [])}

    # ---- A) assertions: the engine flags 'unknown' (new path present), the renderer splits to a real verdict ----
    mix = by.get("PR-MIX", {})
    checks.append((f"A0: the ENGINE verdict for the mixed PR is 'unknown' (a new path IS present) — got "
                   f"'{mix.get('verdict')}'; unknown_paths={mix.get('unknown_paths')}",
                   mix.get("verdict") == "unknown"
                   and "brand_new_module.py" in (mix.get("unknown_paths") or [])
                   and "app/solo.py" not in (mix.get("unknown_paths") or [])))
    out_mix = R.render_pr_check(imp, "PR-MIX", added_paths=["brand_new_module.py"])
    c_mix = out_mix.get("comment") or ""
    checks.append((f"A1 (THE FIX): with positive added-path evidence, the RENDERER gives the verifiable part "
                   f"its real verdict — conclusion "
                   f"'success' (NOT the blanket-unknown 'neutral') — got '{out_mix.get('conclusion')}'",
                   out_mix.get("conclusion") == "success"))
    checks.append((f"A2 (THE FIX): the check TITLE reads 'Clear', NOT the blanket 'Unknown'/'Not analyzed' — "
                   f"got {out_mix.get('title')!r}",
                   "Clear" in out_mix.get("title", "") and "Unknown" not in out_mix.get("title", "")))
    checks.append(("A3 (THE FIX): the blanket whole-PR 'Not analyzed: some of your changed paths' copy is GONE "
                   "(it no longer suppresses the verifiable verdict)",
                   "Not analyzed: some of your changed paths" not in (out_mix.get("summary") or "")
                   and "Not analyzed: some of your changed paths" not in c_mix))
    checks.append(("A4 (THE FIX): the proven NEW path is reported SEPARATELY and names the new file",
                   "New paths in this PR" in c_mix
                   and "coupling will be computable after merge" in c_mix
                   and "brand_new_module.py" in c_mix))
    checks.append(("A5 (HONEST, not over-claiming): the per-path note never calls the new path clear",
                   "Treat those as clear" not in c_mix and "are clear" not in c_mix))

    checks.append(("A6: proven additions use expected-new wording, never extractor-gap wording",
                   "extractor gap" not in c_mix
                   and "unsupported language" not in c_mix
                   and "un-indexed area" not in c_mix))

    # ---- B) conservative boundaries: every missing/uncertain input remains honest Unknown/neutral ----
    allnew = by.get("PR-ALLNEW", {})
    checks.append((f"B0: the all-new PR verdict is 'unknown' (got '{allnew.get('verdict')}')",
                   allnew.get("verdict") == "unknown"))
    out_an = R.render_pr_check(
        imp, "PR-ALLNEW",
        added_paths=["tests/test_brand_new.py", "db/schema/99_least_privilege.sql"],
    )
    c_an = out_an.get("comment") or ""
    checks.append((f"B1: even when every unknown path is proven added, an all-new PR has no known subset and "
                   f"STAYS Unknown/neutral (got conclusion '{out_an.get('conclusion')}')",
                   out_an.get("conclusion") == "neutral"
                   and "Unknown" in out_an.get("title", "")
                   and "Not analyzed: some of your changed paths" in (out_an.get("summary") or "")
                   and "New paths in this PR" in c_an))

    # A Files-API failure currently reaches the renderer as None or []; neither is proof that an unknown path is
    # added. Both forms must stay Unknown rather than silently reusing the old mixed-path Clear promotion.
    for label, unavailable_added_paths in (("None", None), ("empty", [])):
        out_missing_added = R.render_pr_check(
            imp, "PR-MIX", added_paths=unavailable_added_paths
        )
        checks.append((f"B2 ({label} added_paths): failed/missing added-path evidence stays Unknown/neutral",
                       out_missing_added.get("conclusion") == "neutral"
                       and "Unknown" in out_missing_added.get("title", "")
                       and "Clear" not in out_missing_added.get("title", "")))

    # One existing path absent from the graph poisons the promotion even when another unknown path is proven new
    # and a known path exists. This is the mixed new-file + extractor-gap shape that previously went green.
    gap_mix = {"repo": "acme/perpath", "branch": "main",
               "changes": [{"change_id": "PR-GAP", "agent": "sdev", "label": "sdev",
                            "verdict": "unknown", "paths": ["known.py", "existing.meta", "new.py"],
                            "unknown_paths": ["existing.meta", "new.py"]}],
               "clusters": []}
    out_gap = R.render_pr_check(gap_mix, "PR-GAP", added_paths=["new.py"])
    c_gap = out_gap.get("comment") or ""
    checks.append(("B3 (graph gap): one unknown path not proven added keeps the whole verdict Unknown/neutral",
                   out_gap.get("conclusion") == "neutral"
                   and "Unknown" in out_gap.get("title", "")
                   and "Clear" not in out_gap.get("title", "")))
    checks.append(("B3 copy: the proven new path and existing graph gap remain separately visible",
                   "New paths in this PR" in c_gap and "new.py" in c_gap
                   and "extractor gap" in c_gap and "existing.meta" in c_gap
                   and "Treat as unknown, not clear" in c_gap))

    # Dampening is a real suppressed coupling. Even with a known path and complete added-path evidence it cannot
    # be promoted to Clear.
    dampened = {"repo": "acme/perpath", "branch": "main",
                "changes": [{"change_id": "PR-DAMP", "agent": "ddev", "label": "ddev",
                             "verdict": "unknown", "paths": ["known.py", "new.py"],
                             "unknown_paths": ["new.py"],
                             "dampened_with": [{"by": "other PR-9", "via_hub": "hub.py"}]}],
                "clusters": []}
    out_dampened = R.render_pr_check(dampened, "PR-DAMP", added_paths=["new.py"])
    checks.append(("B4 (dampened): a suppressed coupling blocks unknown→clear promotion",
                   out_dampened.get("conclusion") == "neutral"
                   and "Unknown" in out_dampened.get("title", "")
                   and "suppressed to avoid noise" in (out_dampened.get("comment") or "")))

    # Truncation leaves files unparsed. It blocks both an otherwise-eligible unknown→clear promotion and a raw
    # engine Clear, keeping check title/conclusion/comment coherent.
    out_truncated_unknown = R.render_pr_check(
        imp, "PR-MIX", added_paths=["brand_new_module.py"], truncated=True
    )
    checks.append(("B5 (truncated unknown): proven additions cannot clear an unparsed remainder",
                   out_truncated_unknown.get("conclusion") == "neutral"
                   and "Unknown" in out_truncated_unknown.get("title", "")
                   and "unknown, not clear" in (out_truncated_unknown.get("comment") or "").lower()))
    truncated_clear = {"repo": "acme/perpath", "branch": "main",
                       "changes": [{"change_id": "PR-TRUNC", "agent": "tdev", "label": "tdev",
                                    "verdict": "clear", "paths": ["known.py"], "unknown_paths": []}],
                       "clusters": []}
    out_truncated_clear = R.render_pr_check(truncated_clear, "PR-TRUNC", truncated=True)
    checks.append(("B6 (truncated clear): a partial engine Clear renders Unknown/neutral, never green Clear",
                   out_truncated_clear.get("conclusion") == "neutral"
                   and "Unknown" in out_truncated_clear.get("title", "")
                   and "Clear" not in out_truncated_clear.get("title", "")
                   and "unparsed file remainder" in (out_truncated_clear.get("summary") or "")))

    # A missing verdict is missing evidence, not an implicit engine Clear.
    missing_verdict = {"repo": "acme/perpath", "branch": "main",
                       "changes": [{"change_id": "PR-MISSING", "agent": "mdev", "label": "mdev",
                                    "paths": ["known.py", "new.py"], "unknown_paths": ["new.py"]}],
                       "clusters": []}
    out_missing_verdict = R.render_pr_check(
        missing_verdict, "PR-MISSING", added_paths=["new.py"]
    )
    checks.append(("B7 (missing verdict): even otherwise-complete promotion evidence cannot turn missing engine "
                   "state into Clear",
                   out_missing_verdict.get("conclusion") == "neutral"
                   and "Unknown" in out_missing_verdict.get("title", "")
                   and "Clear" not in out_missing_verdict.get("title", "")))

    # ---- C) a real collision is NEVER masked by a new path; the new path is STILL surfaced ----
    coll = by.get("PR-COLL", {})
    checks.append((f"C0: the colliding PR verdict is 'serialize' — the engine ladder keeps a real same-file "
                   f"collision AHEAD of the unknown-path test (got '{coll.get('verdict')}')",
                   coll.get("verdict") == "serialize"
                   and "another_new.py" in (coll.get("unknown_paths") or [])))
    out_coll = R.render_pr_check(imp, "PR-COLL", added_paths=["another_new.py"])
    c_coll = out_coll.get("comment") or ""
    checks.append((f"C1: a real collision is NOT downgraded to clear by the new path — conclusion stays 'neutral' "
                   f"and the title is 'Wait in line' (got conclusion '{out_coll.get('conclusion')}', title "
                   f"{out_coll.get('title')!r})",
                   out_coll.get("conclusion") == "neutral" and "Wait in line" in out_coll.get("title", "")))
    checks.append(("C2: the new path is STILL surfaced separately on the colliding PR (the collision verdict and "
                   "the honest new-paths note coexist) — names the new file",
                   "New paths in this PR" in c_coll and "another_new.py" in c_coll))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("PER-PATH UNKNOWN GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
