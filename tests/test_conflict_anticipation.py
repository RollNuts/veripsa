#!/usr/bin/env python3
"""MECHANICAL merge-conflict anticipation gate — a DIFFERENT AXIS from semantic-coupling severity.

Veripsa's existing engine answers "how IMPORTANT is this coupling (logic risk)?" — it hard-serializes a real
source collision and SOFTENS a low-value append-mostly-file collision (run_gates.sh and kin) to a "heads up".
This gate proves a SEPARATE signal: "will these two in-flight PRs produce a GIT MERGE CONFLICT on rebase
(mechanical certainty)?", derived ONLY from the per-PR changed-line ranges Veripsa ALREADY stores (the same
diff HUNK-HEADER ranges the finer/symbol collision uses). Content-free: line numbers + paths only, never code.

The case that bit us (the regression this gate locks): three PRs each APPENDED a gate-registration block near
the SAME lines of run_gates.sh → a guaranteed git conflict on rebase. But run_gates.sh is "low value", so the
severity got SOFTENED — and softening silently dropped the conflict forewarning, so the conflict was a SURPRISE.
The lesson: severity and conflict-certainty are INDEPENDENT axes. A low-value file can be low-severity (don't
serialize) yet high-conflict-certainty (you WILL hit a trivial merge conflict). So this signal NEVER changes a
verdict; it rides ALONGSIDE.

Proven here against the REAL gate + REAL extractor/hunk-parser + REAL renderer:
  (a) two PRs with OVERLAPPING ranges in a file → merge_conflict_likely flagged + surfaced.
  (b) the APPEND-AT-SAME-LINE case (both insert near the same line of run_gates.sh) → flagged (the regression),
      AND the verdict stays SOFT (the conflict heads-up does NOT re-serialize a low-value collision).
  (c) two PRs touching the SAME file at DISJOINT ranges with a real gap → NOT flagged (no false conflict).
  (d) the rendered text is content-free + HONESTLY framed ("likely"/"expect", never "will definitely").
  (e) a SOFTENED low-value collision now ALSO carries the conflict heads-up (the regression fix, end to end).

Run:  python3 tests/test_conflict_anticipation.py
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
# drop each other's DB mid-run. Per-PID, exactly like db/smoke.sh, run_gates, test_finer_collision.py.
DB = "veripsa_conflicttest_" + str(os.getpid())
REPO = "acme/conflict"

# A SECRET-LOOKING token that lives ONLY in a diff BODY (never a hunk header). If it ever reaches the DB or the
# rendered surface, the content-free contract is broken. (The conflict signal must derive from line ranges only.)
SECRET = "SUPER_SECRET_CONFLICT_zzz777"


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


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    import render as R
    from github_rest import changed_line_ranges_from_patch as parse_hunks

    checks = []

    # the file-node content hashes of the currently-ingested graph (the freshness key claims read so a same-version
    # PR's ranges are provably valid — mirrors test_finer_collision.py).
    file_hashes = {}

    def ingest(graph):
        db("veripsa_app", "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
           (json.dumps(graph), REPO, "main", "a" * 40))
        file_hashes.clear()
        for n in graph.get("nodes", []):
            if n.get("kind") == "file" and n.get("content_hash"):
                file_hashes[n["path"]] = n["content_hash"]

    def claim(cid, path, author, ranges):
        rj = json.dumps(ranges) if ranges else None
        db("veripsa_app", "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s)",
           (cid, path, REPO, "main", author, rj, file_hashes.get(path)))

    def reset_claims():
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

    # The graph: run_gates.sh (a low-value append-mostly RUNNER, no symbols) + server.py (REAL source w/ a symbol).
    graph = {"nodes": [
        {"id": "run_gates.sh", "kind": "file", "path": "run_gates.sh", "language": "shell", "content_hash": "a" * 40},
        {"id": "server.py", "kind": "file", "path": "server.py", "language": "python", "content_hash": "b" * 40},
        {"id": "server.py::handle", "kind": "def", "name": "handle", "path": "server.py", "language": "python",
         "start_line": 10, "end_line": 60, "content_hash": "b" * 40},
        {"id": "server.py::other", "kind": "def", "name": "other", "path": "server.py", "language": "python",
         "start_line": 100, "end_line": 150, "content_hash": "b" * 40},
    ]}
    ingest(graph)

    # ── The hunk-header parser stays content-free; the APPEND geometry that bit us produces OVERLAPPING ranges. ──
    # An append near line 207 with 3 context lines: @@ -205,3 +205,6 @@ → BASE-side [[205, 207]] (the 3 base
    # context lines the hunk anchors on; the 3 added lines have no base existence). Two appends at the SAME base
    # anchor → identical base ranges → they OVERLAP → a merge conflict is still anticipated (base-side fix).
    append_patch_a = (f"@@ -205,3 +205,6 @@\n ctx1\n ctx2\n ctx3\n+# gate block A {SECRET}\n+run A\n+done A\n")
    append_patch_b = (f"@@ -205,3 +205,6 @@\n ctx1\n ctx2\n ctx3\n+# gate block B {SECRET}\n+run B\n+done B\n")
    append_a = parse_hunks(append_patch_a)   # → [[205, 207]] (base-side)
    append_b = parse_hunks(append_patch_b)   # → [[205, 207]] (base-side)
    checks.append((f"hunk-header parse is content-free (append range {append_a} carries NO body / no secret)",
                   append_a == [[205, 207]] and SECRET not in json.dumps(append_a)))

    # ── (a) OVERLAPPING ranges in a file → merge_conflict_likely flagged + surfaced ───────────────────────────
    # Two PRs on server.py both editing OVERLAPPING lines of the SAME function → a real source collision (hard
    # serialize) AND a likely git conflict (overlapping lines). Proves the signal fires on a true overlap.
    reset_claims()
    claim("PR-1:server.py", "server.py", "alice", [[20, 30]])    # handle, lines 20-30
    claim("PR-2:server.py", "server.py", "bob", [[25, 35]])      # handle, lines 25-35 — OVERLAP at 25-30
    ch, imp = impact()
    pr2 = ch.get("PR-2", {})
    cps = pr2.get("conflict_points", []) or []
    checks.append((f"(a) OVERLAP: merge_conflict_likely is flagged (got {pr2.get('merge_conflict_likely')})",
                   pr2.get("merge_conflict_likely") is True))
    checks.append((f"(a) OVERLAP: a conflict point names the file + a content-free line (got {json.dumps(cps)})",
                   bool(cps) and cps[0].get("path") == "server.py" and isinstance(cps[0].get("line"), int)))
    # this real-source overlap is ALSO a hard serialize (severity axis) — the conflict signal rides alongside, not instead.
    checks.append((f"(a) OVERLAP on real source is still a HARD serialize (verdict='{pr2.get('verdict')}') — "
                   "the conflict signal is ADDITIVE, not a verdict change", pr2.get("verdict") == "serialize"))

    # ── (b) APPEND-AT-SAME-LINE on run_gates.sh → flagged (THE REGRESSION) + verdict stays SOFT ───────────────
    reset_claims()
    claim("PR-3:run_gates.sh", "run_gates.sh", "carol", append_a)   # holder, append near 207
    claim("PR-4:run_gates.sh", "run_gates.sh", "dave", append_b)    # waiter, append the SAME spot
    ch, imp = impact()
    pr4 = ch.get("PR-4", {})
    cps4 = pr4.get("conflict_points", []) or []
    near_line = cps4[0].get("line") if cps4 else None
    checks.append((f"(b) APPEND-AT-SAME-LINE (the regression): merge_conflict_likely flagged even though no "
                   f"existing line is shared (got {pr4.get('merge_conflict_likely')}, near line {near_line})",
                   pr4.get("merge_conflict_likely") is True and isinstance(near_line, int)))
    checks.append((f"(b) the SEVERITY stays SOFT — the conflict signal does NOT re-serialize a low-value "
                   f"collision (verdict='{pr4.get('verdict')}')", pr4.get("verdict") == "serialize_soft"))
    checks.append((f"(b) the conflict_line points at the shared insertion region (near line {near_line}, "
                   f"the append point ~205)", near_line == 205))

    # ── (c) SAME file, DISJOINT ranges with a real gap → NOT flagged (no false conflict) ──────────────────────
    # Two PRs on server.py editing FAR-APART functions (handle 20-30 vs other 105-115). Disjoint with a big gap →
    # git auto-merges → must NOT raise a false conflict. (This pair is also symbol-disjoint, so it does not even
    # serialize — but the point under test is the CONFLICT axis: no false conflict on a real gap.)
    reset_claims()
    claim("PR-5:server.py", "server.py", "erin", [[20, 30]])     # handle
    claim("PR-6:server.py", "server.py", "frank", [[105, 115]])  # other — DISJOINT, gap ~75 lines
    ch, imp = impact()
    pr5, pr6 = ch.get("PR-5", {}), ch.get("PR-6", {})
    no_conflict = not pr5.get("merge_conflict_likely") and not pr6.get("merge_conflict_likely")
    no_points = not (pr5.get("conflict_points") or []) and not (pr6.get("conflict_points") or [])
    checks.append((f"(c) DISJOINT-with-gap: NO false conflict flagged on either side "
                   f"(PR-5={pr5.get('merge_conflict_likely')}, PR-6={pr6.get('merge_conflict_likely')})",
                   no_conflict and no_points))

    # (c2) DISJOINT on a LOW-VALUE file too: two PRs on run_gates.sh at far-apart line ranges (a real gap) →
    # still NOT a conflict (the signal is about geometry, not the file's value).
    reset_claims()
    claim("PR-7:run_gates.sh", "run_gates.sh", "gina", [[10, 15]])    # top of the file
    claim("PR-8:run_gates.sh", "run_gates.sh", "hank", [[205, 210]])  # the append region — DISJOINT, gap ~190
    ch, _ = impact()
    checks.append((f"(c2) DISJOINT on a low-value file: NO false conflict "
                   f"(PR-8 conflict_likely={ch.get('PR-8', {}).get('merge_conflict_likely')})",
                   not ch.get("PR-8", {}).get("merge_conflict_likely")))

    # (c3) JUST OUTSIDE the gap tolerance (default 2): ranges separated by 3 blank lines must NOT flag. Proves the
    # tolerance is bounded — "near" is genuinely near, not a back-door that flags any same-file pair.
    reset_claims()
    claim("PR-9:run_gates.sh", "run_gates.sh", "ivy", [[10, 12]])    # ends at 12
    claim("PR-10:run_gates.sh", "run_gates.sh", "jay", [[16, 18]])   # starts at 16 → gap of 3 (>tol=2)
    ch, _ = impact()
    checks.append((f"(c3) BOUNDED tolerance: a 3-line gap is NOT flagged (conflict_likely="
                   f"{ch.get('PR-10', {}).get('merge_conflict_likely')}) — 'near' stays near, no false conflict",
                   not ch.get("PR-10", {}).get("merge_conflict_likely")))

    # (c4) WITHIN the gap tolerance: ranges separated by exactly 1 line (differing context between two appends) →
    # flagged. This is the realistic append case where context lines leave a tiny gap; git still conflicts.
    reset_claims()
    claim("PR-11:run_gates.sh", "run_gates.sh", "kate", [[205, 208]])   # ends at 208
    claim("PR-12:run_gates.sh", "run_gates.sh", "leo", [[210, 213]])    # starts at 210 → gap of 1 (<=tol=2)
    ch, _ = impact()
    checks.append((f"(c4) WITHIN tolerance: a 1-line gap (two appends, slightly different context) IS flagged "
                   f"(conflict_likely={ch.get('PR-12', {}).get('merge_conflict_likely')}) — the same-insertion case",
                   ch.get("PR-12", {}).get("merge_conflict_likely") is True))

    # ── (d) + (e) RENDERED text: content-free, HONESTLY framed, and the SOFTENED collision carries the heads-up ─
    reset_claims()
    claim("PR-13:run_gates.sh", "run_gates.sh", "mia", append_a)
    claim("PR-14:run_gates.sh", "run_gates.sh", "ned", append_b)   # append same spot → soft + conflict-likely
    ch, imp = impact()
    pr14 = ch.get("PR-14", {})
    rendered = R.render_pr_check(imp, "PR-14")
    body = (rendered.get("summary") or "") + "\n" + (rendered.get("comment") or "")

    # (e) the softened low-value collision now ALSO carries the conflict heads-up (the regression fix end to end).
    checks.append((f"(e) the SOFTENED low-value collision (verdict='{pr14.get('verdict')}') ALSO carries the "
                   "merge-conflict heads-up in the rendered comment",
                   pr14.get("verdict") == "serialize_soft"
                   and "merge conflict" in body.lower()
                   and "run_gates.sh" in body))
    # (d) HONEST framing: "likely"/"expect", never "will definitely" / "guaranteed".
    low = body.lower()
    honest = ("likely" in low or "expect" in low) and ("will definitely" not in low and "guaranteed" not in low)
    checks.append(("(d) HONEST framing: the conflict text says 'likely'/'expect', never 'will definitely' / "
                   "'guaranteed' (it is a content-free heuristic, not a 3-way merge)", honest))
    # (d) the heads-up names the approximate LINE (content-free locus), not a vague "somewhere".
    checks.append(("(d) the conflict heads-up names an approximate line (content-free 'near line N')",
                   "near line" in low))
    # (d) CONTENT-FREE: the diff body / secret token never reached the engine surface nor the rendered text.
    surface_blob = json.dumps(imp)
    checks.append(("(d) CONTENT-FREE: the diff body / secret token is NEVER in the engine surface output",
                   SECRET not in surface_blob))
    checks.append(("(d) CONTENT-FREE: the diff body / secret token is NEVER in the rendered PR surface",
                   SECRET not in body))
    # (d) and not in the stored claim ranges (line numbers only on the claim row).
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    with conn, conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT set_config('core.current_account','ACCT-DEMO',true)")
        cur.execute("SELECT coalesce(string_agg(touched_ranges::text,' '),'') FROM core.claim WHERE touched_ranges IS NOT NULL")
        ranges_text = cur.fetchone()[0]
    conn.close()
    checks.append((f"(d) CONTENT-FREE: stored claim ranges are line numbers only — no body (got {ranges_text!r})",
                   SECRET not in ranges_text and "secret" not in ranges_text.lower()))

    # ── (f) NO-DATA HONESTY: a change with NO line ranges cannot be anticipated → no conflict claim (but the
    #    file-level COLLISION still stands). Under-anticipating is acceptable; a false "conflict" is not. ────────
    reset_claims()
    claim("PR-15:run_gates.sh", "run_gates.sh", "omar", append_a)
    claim("PR-16:run_gates.sh", "run_gates.sh", "pat", None)       # NO ranges → cannot anticipate a conflict
    ch, _ = impact()
    pr16 = ch.get("PR-16", {})
    checks.append((f"(f) NO-DATA: a change with NO line ranges makes NO conflict claim "
                   f"(conflict_likely={pr16.get('merge_conflict_likely')}) — honest under-anticipation, not a false flag",
                   not pr16.get("merge_conflict_likely")))
    # ...but the collision itself is NOT lost — the file-level wait still stands (recall preserved).
    checks.append((f"(f) NO-DATA: the file-level collision still stands (verdict='{pr16.get('verdict')}') — the "
                   "conflict signal is additive and never suppresses a real collision",
                   pr16.get("verdict") in ("serialize", "serialize_soft")))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("CONFLICT ANTICIPATION GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
