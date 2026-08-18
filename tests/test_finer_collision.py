#!/usr/bin/env python3
"""FINER (SYMBOL-LEVEL) COLLISION gate — "中身を持たずに、衝突点を細かくする": without holding file CONTENTS,
make the collision unit FILE → SYMBOL, content-free.

Today a DIRECT collision fires when two in-flight changes touch the SAME FILE (the claim lock on target_path).
That is too blunt: two PRs editing DIFFERENT functions of one file are forced to "wait in line" even though git
auto-merges them. This proves the refinement, against the REAL gate + the REAL extractor, using ONLY line
metadata (symbol [start_line,end_line] spans + per-claim changed line RANGES parsed from diff HUNK HEADERS):

  (a) two changes on DIFFERENT symbols of one file  → NO serialize  (the finer win: no false wait in line)
  (b) two changes on the SAME symbol                → serialize, AND the surface NAMES the symbol
  (c) FALLBACK (recall safety net): a change touching TOP-LEVEL / un-mapped lines on a file another change also
      touches → STILL collides at FILE level (never a silent miss)
  (d) BACK-COMPAT: a graph with NO spans (an old ingest) → behaves EXACTLY as today (file-level serialize)

Plus the HARD CONSTRAINT: CONTENT-FREE — no file BODY text is ever stored on a claim or rendered on the surface
(we parse HUNK HEADERS only and discard the +/- body). This gate asserts that the diff body never reaches the DB.

Run:  python3 tests/test_finer_collision.py
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

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
# Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_server.py.
DB = "veripsa_finertest_" + str(os.getpid())
REPO = "acme/finer"

# A SECRET-LOOKING token that lives ONLY in the diff BODY (never a hunk header). If it ever reaches the DB
# (a claim's stored ranges) or the rendered surface, the content-free contract is broken.
SECRET = "SUPER_SECRET_TOKEN_zzz999"


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


def build_two_symbol_graph():
    """Extract a REAL graph for a file render.py with TWO symbols (alpha, beta), so the spans come from the
    actual extractor (not hand-fed line numbers) — proves the extractor → schema → engine path end to end."""
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "render.py"), "w") as fh:
            fh.write(
                "def alpha(x):\n"          # lines 1-3
                "    y = x + 1\n"
                "    return y\n"
                "\n"                        # line 4 (top-level, outside any symbol)
                "TOP_LEVEL = 99\n"          # line 5 (top-level/module code)
                "\n"                        # line 6
                "def beta(z):\n"            # lines 7-9
                "    w = z * 2\n"
                "    return w\n"
            )
        g = X.build_graph(d)
    return g


def strip_spans(graph):
    """A copy of the graph with every symbol span removed — simulates an OLD ingest (back-compat case d)."""
    out = {"nodes": [], "edges": list(graph["edges"])}
    for n in graph["nodes"]:
        m = {k: v for k, v in n.items() if k not in ("start_line", "end_line")}
        out["nodes"].append(m)
    return out


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    import render as R
    from github_rest import changed_line_ranges_from_patch as parse_hunks

    checks = []

    # FRESHNESS KEY: the content hash main's graph carries for each FILE node of the CURRENTLY-ingested graph.
    # `claim(..., base_hash="__FRESH__")` reads this so a same-version PR's base hash matches the graph hash and
    # the finer demotion still fires (precision). Re-populated on every ingest() so a re-ingest updates the truth.
    file_hashes = {}

    def ingest(graph):
        db("veripsa_app", "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
           (json.dumps(graph), REPO, "main", "a" * 40))
        file_hashes.clear()
        for n in graph.get("nodes", []):
            if n.get("kind") == "file" and n.get("content_hash"):
                file_hashes[n["path"]] = n["content_hash"]

    def claim(cid, path, author, ranges, base_hash="__FRESH__"):
        # base_hash defaults to "__FRESH__" = "the same hash main's graph stored for this file" (resolved at call
        # time from the ingested graph), so a same-version PR's spans are PROVABLY valid and the finer demotion
        # still fires (precision). Pass an explicit hash (or None) to model a STALE/unknown base (recall safety).
        rj = json.dumps(ranges) if ranges else None
        bh = file_hashes.get(path) if base_hash == "__FRESH__" else base_hash
        db("veripsa_app", "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s)",
           (cid, path, REPO, "main", author, rj, bh))

    def reset_claims():
        # release every in-flight claim for a clean slate between scenarios (migrator-owner direct, account-pinned).
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account','ACCT-DEMO',true)")
            cur.execute("SELECT set_config('core.governed_write_token','claim',true)")
            cur.execute("UPDATE core.claim SET claim_state='released', released_at=now() WHERE claim_state IN ('active','waiting')")
        conn.close()

    def impact():
        imp = db("veripsa_app", "SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
        if isinstance(imp, str):
            imp = json.loads(imp)
        return {c["change_id"]: c for c in imp.get("changes", [])}, imp

    graph = build_two_symbol_graph()
    # sanity: the extractor really gave alpha + beta a span (else the whole feature is a no-op here).
    spans = {n["name"]: (n.get("start_line"), n.get("end_line"))
             for n in graph["nodes"] if n.get("kind") == "def"}
    checks.append((f"extractor emits content-free symbol spans (alpha={spans.get('alpha')}, beta={spans.get('beta')})",
                   spans.get("alpha", (None,))[0] is not None and spans.get("beta", (None,))[0] is not None))

    # ── The diff hunk-header parser is the content-free boundary: BODY (with the SECRET) must be discarded. ──
    alpha_patch = (f"@@ -1,3 +1,3 @@\n def alpha(x):\n-    y = x + 1\n+    y = x + 1  # {SECRET}\n     return y\n")
    beta_patch = (f"@@ -7,3 +7,3 @@\n def beta(z):\n-    w = z * 2\n+    w = z * 2  # {SECRET}\n     return w\n")
    alpha_ranges = parse_hunks(alpha_patch)   # → [[1,3]]
    beta_ranges = parse_hunks(beta_patch)     # → [[7,9]]
    checks.append((f"hunk-header parse is content-free (ranges {alpha_ranges} carry NO body text / no secret)",
                   alpha_ranges == [[1, 3]] and SECRET not in json.dumps(alpha_ranges)))

    # ── (a) DIFFERENT symbols → NO serialize (the finer win) ──────────────────────────────────────────────
    ingest(graph)
    claim("PR-1:render.py", "render.py", "alice", alpha_ranges)   # edits alpha (lines 1-3)
    claim("PR-2:render.py", "render.py", "bob", beta_ranges)      # edits beta (lines 7-9) — DISJOINT
    ch, imp = impact()
    pr2 = ch.get("PR-2", {})
    serialize_count = imp.get("serialize_count")
    checks.append(("(a) DIFFERENT symbols: the second PR is NOT serialized (false wait dropped) "
                   f"(verdict='{pr2.get('verdict')}', serialize_count={serialize_count})",
                   pr2.get("verdict") != "serialize" and serialize_count == 0))
    checks.append(("(a) the dropped pair leaves NO serialize_behind on PR-2 (no false wait-in-line)",
                   not (pr2.get("serialize_behind") or [])))

    # ── (b) SAME symbol → serialize, surface NAMES the symbol ─────────────────────────────────────────────
    reset_claims()
    claim("PR-3:render.py", "render.py", "carol", [[1, 2]])       # alpha (lines 1-2)
    claim("PR-4:render.py", "render.py", "dave", [[2, 3]])        # alpha (lines 2-3) — SAME symbol
    ch, imp = impact()
    pr4 = ch.get("PR-4", {})
    cps = pr4.get("collision_points", []) or []
    named_symbol = next((cp.get("symbol") for cp in cps if cp.get("symbol")), None)
    checks.append((f"(b) SAME symbol: the waiter serializes (verdict='{pr4.get('verdict')}')",
                   pr4.get("verdict") == "serialize"))
    checks.append((f"(b) the surface NAMES the colliding symbol (got symbol='{named_symbol}')",
                   named_symbol == "alpha"))
    rendered = R.render_pr_check(imp, "PR-4")
    body = (rendered.get("summary") or "") + "\n" + (rendered.get("comment") or "")
    checks.append(("(b) the rendered PR check names the symbol `alpha` (the finer point)",
                   "alpha" in body and "`alpha`" in body))

    # ── (c) FALLBACK: top-level / un-mapped lines → STILL collides at FILE level (recall preserved) ────────
    reset_claims()
    claim("PR-5:render.py", "render.py", "erin", [[1, 2]])        # alpha
    claim("PR-6:render.py", "render.py", "frank", [[4, 5]])       # TOP-LEVEL lines (outside any symbol)
    ch, imp = impact()
    pr6 = ch.get("PR-6", {})
    checks.append((f"(c) FALLBACK: an un-mapped (top-level) change STILL serializes at file level "
                   f"(verdict='{pr6.get('verdict')}') — recall preserved, never a silent miss",
                   pr6.get("verdict") == "serialize"))

    # (c2) a change with NO ranges at all → file-level fallback (over-flag, never miss)
    reset_claims()
    claim("PR-7:render.py", "render.py", "gina", [[1, 2]])        # alpha
    claim("PR-8:render.py", "render.py", "hank", None)            # NO ranges
    ch, _ = impact()
    checks.append((f"(c2) FALLBACK: a change with NO line ranges STILL serializes at file level "
                   f"(verdict='{ch.get('PR-8', {}).get('verdict')}')",
                   ch.get("PR-8", {}).get("verdict") == "serialize"))

    # ── (d) BACK-COMPAT: NO spans in the graph (old ingest) → behaves EXACTLY as today (file-level) ────────
    reset_claims()
    ingest(strip_spans(graph))                                   # re-ingest WITHOUT spans
    claim("PR-9:render.py", "render.py", "ivy", alpha_ranges)    # alpha range
    claim("PR-10:render.py", "render.py", "jay", beta_ranges)    # beta range — DISJOINT, but no spans to prove it
    ch, _ = impact()
    checks.append((f"(d) BACK-COMPAT: with NO symbol spans, a disjoint-looking pair STILL serializes "
                   f"(file-level, exactly as today; verdict='{ch.get('PR-10', {}).get('verdict')}')",
                   ch.get("PR-10", {}).get("verdict") == "serialize"))

    # ── (e) EXACT BOUNDARIES + NESTING (locks the QA-finer correctness fixes; these are the off-by-one / nested
    #    cases the file-level→symbol refinement can get wrong — a wrong drop here is a SILENT MISS). We ingest a
    #    PRECISE hand-built graph so the spans are exact: a class C[10,40] with two methods m[20,30] and n[32,38],
    #    plus two non-adjacent top-level funcs alpha[50,60] and beta[70,80]. (Spans are content-free line numbers.)
    bgraph = {"nodes": [
        {"id": "b.py", "kind": "file", "path": "b.py", "language": "python", "content_hash": "b" * 40},
        {"id": "b.py::C", "kind": "class", "name": "C", "path": "b.py", "language": "python", "start_line": 10, "end_line": 40},
        {"id": "b.py::m", "kind": "def", "name": "m", "path": "b.py", "language": "python", "start_line": 20, "end_line": 30},
        {"id": "b.py::n", "kind": "def", "name": "n", "path": "b.py", "language": "python", "start_line": 32, "end_line": 38},
        {"id": "b.py::alpha", "kind": "def", "name": "alpha", "path": "b.py", "language": "python", "start_line": 50, "end_line": 60},
        {"id": "b.py::beta", "kind": "def", "name": "beta", "path": "b.py", "language": "python", "start_line": 70, "end_line": 80},
        # same NAME, different span — a decorator's inner closure (extremely common in real Python). MUST NOT
        # collide just because the name matches: identity is the SPAN, not the name.
        {"id": "b.py::inner#1", "kind": "def", "name": "inner", "path": "b.py", "language": "python", "start_line": 90, "end_line": 95},
        {"id": "b.py::inner#2", "kind": "def", "name": "inner", "path": "b.py", "language": "python", "start_line": 96, "end_line": 99},
    ], "edges": [{"src": "b.py", "dst": "b.py::C", "kind": "contains"}]}

    def bverdict(cid):
        ch2, _ = impact()
        return ch2.get(cid, {}).get("verdict")

    reset_claims(); ingest(bgraph)

    # (e1) LAST-LINE boundary: a change on a symbol's LAST line (m ends at 30) MAPS to it → SAME → serialize.
    reset_claims()
    claim("E1H", "b.py", "h", [[20, 22]]); claim("E1W", "b.py", "w", [[30, 30]])   # exactly m's last line
    checks.append(("(e1) BOUNDARY: a change on a symbol's LAST line maps to it → serialize (recall)",
                   bverdict("E1W") == "serialize"))

    # (e2) JUST-AFTER + STRADDLE (the Issue-A recall miss): a range [60,61] covers TOP-LEVEL func alpha's last
    # line (60) AND the truly top-level line 61 just after it (outside EVERY symbol — alpha[50,60] is top-level,
    # not inside any class). It is NOT fully inside alpha → un-mappable → the file-level collision is KEPT.
    # (Overlap-based mapping wrongly dropped this as 'disjoint' against beta = a SILENT MISS. This is the bug.)
    reset_claims()
    claim("E2H", "b.py", "h", [[70, 72]])                          # beta (a different symbol)
    claim("E2W", "b.py", "w", [[60, 61]])                          # straddles alpha-end (60) + top-level (61)
    checks.append(("(e2) BOUNDARY/RECALL: a range straddling a symbol-end and top-level is NOT dropped → "
                   "serialize (never a silent miss)", bverdict("E2W") == "serialize"))

    # (e3) NESTED DISJOINT WIN (the Issue-B fix): two DIFFERENT methods m and n of one class C → DROP. With
    # overlap-based mapping both also touch C's span and falsely serialize; innermost-containment maps each to
    # its own method, so they are siblings → no false wait.
    reset_claims()
    claim("E3H", "b.py", "h", [[20, 25]]); claim("E3W", "b.py", "w", [[33, 37]])   # method m vs method n
    checks.append(("(e3) NESTED WIN: two DIFFERENT methods of one class do NOT serialize (the finer win)",
                   bverdict("E3W") != "serialize"))

    # (e4) NESTED RECALL GUARD: a class-BODY edit (line 11, inside C but outside any method) vs a method edit
    # (m) → the class span CONTAINS the method → keep the collision → serialize. (Nesting must not be dropped.)
    reset_claims()
    claim("E4H", "b.py", "h", [[11, 11]]); claim("E4W", "b.py", "w", [[22, 22]])   # class body vs method m
    checks.append(("(e4) NESTED RECALL: a class-body edit vs a method of that class → serialize (recall guard)",
                   bverdict("E4W") == "serialize"))

    # (e5) SAME-NAME different-span: two functions both named 'inner' (spans 90-95 vs 96-99) → DROP. Identity is
    # the SPAN, not the name (else every decorator's inner closure would falsely serialize unrelated PRs).
    reset_claims()
    claim("E5H", "b.py", "h", [[91, 93]]); claim("E5W", "b.py", "w", [[97, 98]])   # inner#1 vs inner#2
    checks.append(("(e5) SAME-NAME: two DIFFERENT functions sharing a name do NOT serialize (span identity)",
                   bverdict("E5W") != "serialize"))

    # (e6) DISJOINT TOP-LEVEL FUNCS: alpha[50,60] vs beta[70,80], each fully inside its own symbol → DROP.
    reset_claims()
    claim("E6H", "b.py", "h", [[52, 58]]); claim("E6W", "b.py", "w", [[72, 78]])
    checks.append(("(e6) DISJOINT: two non-adjacent top-level funcs do NOT serialize (the base win)",
                   bverdict("E6W") != "serialize"))

    # ── (f) THE REAL FRESHNESS PATH (end-to-end, NO __FRESH__ shortcut) — proves the two PRODUCERS the live App
    #    actually feeds the gate are wired and content-free:
    #      GRAPH side  : code_graph_extract stamps each FILE node's content_hash = the GIT BLOB SHA of its bytes.
    #      EVENT side  : the App carries each claim's base_hash = the GIT BLOB SHA of the file AT THE PR'S BASE
    #                    (server.base_blob_shas reads the base TREE → blob shas, NO bodies → content-free).
    #    Because BOTH sides compute the IDENTICAL git-blob-sha over the same file version, a PR whose base matches
    #    the analyzed graph yields base_hash == content_hash → FRESH → symbol-level demotion; a PR branched off a
    #    newer main where the file changed yields a DIFFERENT sha → STALE → file-level fallback (recall-safe).
    #    Below we drive that with REAL git blob shas (the producer fn / git hash-object), never the test sentinel.
    import code_graph_extract as X

    # (f0) GIT-BLOB-SHA EXACTNESS — the crux of correctness. The graph hash and the App's base hash are only
    # comparable because the extractor's _git_blob_sha is BYTE-IDENTICAL to git's own blob sha (the App gets that
    # exact sha from GitHub's tree API for free). Prove it: (i) an EMPTY file's git blob sha is the well-known
    # e69de29b… , and (ii) for arbitrary content our producer == `git hash-object` (the canonical git blob sha).
    with tempfile.TemporaryDirectory() as d:
        empty_p = os.path.join(d, "empty.txt")
        open(empty_p, "wb").close()
        empty_sha = X._git_blob_sha(empty_p)
        checks.append((f"(f0) git-blob-sha EXACTNESS: empty file → e69de29b… (got {empty_sha})",
                       empty_sha == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"))
        known_p = os.path.join(d, "known.py")
        with open(known_p, "wb") as fh:
            fh.write(b"def known():\n    return 42\n")
        mine = X._git_blob_sha(known_p)
        git_sha = subprocess.run(["git", "hash-object", known_p], cwd=ROOT,
                                 capture_output=True, text=True).stdout.strip()
        checks.append((f"(f0) git-blob-sha EXACTNESS: producer == `git hash-object` (mine={mine}, git={git_sha})",
                       bool(mine) and mine == git_sha))

    # Build a REAL graph for f.py with two disjoint symbols (foo, bar) so the file node carries the producer's
    # REAL content_hash (git blob sha of the file's bytes), and capture that SAME sha as the App would at the
    # base. Also capture a DIFFERENT file's real blob sha to model a STALE base (the file changed under the PR).
    with tempfile.TemporaryDirectory() as d:
        fp = os.path.join(d, "f.py")
        with open(fp, "w") as fh:
            fh.write("def foo(a):\n    return a + 1\n\n\ndef bar(b):\n    return b * 2\n")   # foo[1,2], bar[5,6]
        fgraph = X.build_graph(d)
        real_blob = X._git_blob_sha(fp)            # the App's base_hash for a FRESH PR == the graph's content_hash
        other_p = os.path.join(d, "other.py")
        with open(other_p, "w") as fh:
            fh.write("def foo(a):\n    return a + 999\n\n\ndef bar(b):\n    return b * 7\n")  # different bytes
        stale_blob = X._git_blob_sha(other_p)      # a genuinely DIFFERENT real git blob sha (a newer file version)
    # the graph really stamped f.py's file node with the real git blob sha (the GRAPH producer, end-to-end).
    fnode_hash = next((n.get("content_hash") for n in fgraph["nodes"]
                       if n.get("kind") == "file" and n.get("path") == "f.py"), None)
    checks.append((f"(f) GRAPH producer: the extractor stamped f.py's file node with its REAL git blob sha "
                   f"(node hash={fnode_hash}, blob={real_blob})",
                   fnode_hash is not None and fnode_hash == real_blob and real_blob != stale_blob))
    foo_span = next(((n.get("start_line"), n.get("end_line")) for n in fgraph["nodes"]
                     if n.get("kind") == "def" and n.get("name") == "foo"), (None, None))
    checks.append((f"(f) the extractor gave foo a content-free span (foo={foo_span})", foo_span[0] is not None))

    # (f1) FRESH (base_hash == the graph's content_hash, the REAL git blob sha) + DIFFERENT symbols → demote to
    # SYMBOL level (disjoint) → NOT serialized. This is the marquee win running for real, no __FRESH__ sentinel.
    reset_claims(); ingest(fgraph)
    claim("PR-F1:f.py", "f.py", "amy", [[1, 2]], base_hash=real_blob)    # foo, FRESH (real blob sha)
    claim("PR-F2:f.py", "f.py", "ben", [[5, 6]], base_hash=real_blob)    # bar, FRESH — DISJOINT
    ch, imp = impact()
    f2 = ch.get("PR-F2", {})
    checks.append(("(f1) REAL FRESH path: base_hash == the graph's git-blob-sha + DIFFERENT symbols → NOT "
                   f"serialized (the symbol-level demotion fires for real; verdict='{f2.get('verdict')}', "
                   f"serialize_count={imp.get('serialize_count')})",
                   f2.get("verdict") != "serialize" and imp.get("serialize_count") == 0))

    # (f2) STALE (base_hash == a DIFFERENT real git blob sha — the file changed between the graph's commit and
    # the PR's base) + the SAME disjoint symbols → freshness_ok is FALSE → the file-level collision is KEPT →
    # STILL serializes (recall-safe: a stale graph could have mapped the diff lines onto the wrong symbols).
    reset_claims()
    claim("PR-F3:f.py", "f.py", "cyd", [[1, 2]], base_hash=stale_blob)   # foo, but base hash ≠ graph hash (stale)
    claim("PR-F4:f.py", "f.py", "dot", [[5, 6]], base_hash=stale_blob)   # bar, stale — disjoint-LOOKING
    ch, _ = impact()
    checks.append(("(f2) REAL STALE path: a base_hash that differs from the graph's git-blob-sha keeps the "
                   "file-level collision → STILL serializes (no false symbol-demotion on a stale graph; "
                   f"verdict='{ch.get('PR-F4', {}).get('verdict')}')",
                   ch.get("PR-F4", {}).get("verdict") == "serialize"))

    # (f3) MIXED freshness (one side FRESH, the other STALE) → freshness_ok requires BOTH → file-level KEPT.
    reset_claims()
    claim("PR-F5:f.py", "f.py", "eve", [[1, 2]], base_hash=real_blob)    # foo, FRESH
    claim("PR-F6:f.py", "f.py", "fox", [[5, 6]], base_hash=stale_blob)   # bar, STALE
    ch, _ = impact()
    checks.append(("(f3) MIXED freshness (one fresh, one stale) → BOTH must be fresh to demote → STILL "
                   f"serializes (verdict='{ch.get('PR-F6', {}).get('verdict')}')",
                   ch.get("PR-F6", {}).get("verdict") == "serialize"))

    # restore the render.py graph for the content-free assertions below.
    reset_claims(); ingest(graph)

    # ── CONTENT-FREE assertion: the diff BODY (the SECRET) never reached the DB (claims) nor the surface. ──
    reset_claims()
    ingest(graph)
    claim("PR-11:render.py", "render.py", "kate", alpha_ranges)
    claim("PR-12:render.py", "render.py", "leo", [[1, 2]])       # same symbol → a rendered comment
    _, imp = impact()
    surface_blob = json.dumps(imp)
    checks.append(("CONTENT-FREE: the diff body / secret token is NEVER in the engine surface output",
                   SECRET not in surface_blob))
    # the stored claim ranges must be integer ranges only — no text body anywhere on the claim row.
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    with conn, conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT set_config('core.current_account','ACCT-DEMO',true)")
        cur.execute("SELECT coalesce(string_agg(touched_ranges::text,' '),'') FROM core.claim WHERE touched_ranges IS NOT NULL")
        ranges_text = cur.fetchone()[0]
    conn.close()
    checks.append((f"CONTENT-FREE: stored claim ranges are line numbers only — no body text (got {ranges_text!r})",
                   SECRET not in ranges_text and "secret" not in ranges_text.lower()))
    r2 = R.render_pr_check(imp, "PR-12")
    rblob = (r2.get("summary") or "") + (r2.get("comment") or "")
    checks.append(("CONTENT-FREE: the rendered PR surface carries no diff body / secret",
                   SECRET not in rblob))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("FINER COLLISION GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
