#!/usr/bin/env python3
"""LINE-SHIFT SYMBOL-MAPPING gate (silent-miss launch-blocker fix, audit r3 2026-06-19).

THE BUG THIS LOCKS (RED on origin/main): the finer-collision symbol mapping joined a PR's changed-line ranges
to main's BASE-version symbol spans — but `github_rest.changed_line_ranges_from_patch` parsed the NEW-side
(`+c,d`) of each hunk header. When a PR adds/removes net lines ABOVE an edit (a contained hunk), the later
edit's NEW-side line numbers are SHIFTED off its base symbol → the edit maps onto the WRONG base symbol → a
REAL same-symbol collision silently drops to 'clear' (and two PRs' new-side numbers live in different
coordinate frames, so the conflict-overlap test was meaningless too). The freshness gate can't catch it: the
PR's BASE genuinely equals the analyzed graph (freshness_ok=true); the shift is WITHIN the PR.

THE FIX: parse the BASE-side (`-a,b`) range — it aligns with the base-version symbol spans the freshness key
already proves valid, AND is comparable across PRs (all relative to the same `main`). Pure-insertion hunks
(`-a,0`) touch no base lines → no range → file-level fallback (recall-safe).

This gate drives the REAL parser → the REAL gate path (act_for_claim → main_impact_surface) and asserts:
  • parser returns BASE-side ranges (a contained line-adding hunk does NOT shift the later edit's numbers);
  • two PRs editing the SAME base symbol (foo) are caught as a same-symbol collision (serialize), NOT 'clear'.
RED on origin/main (new-side parse → maps foo's edit onto bar → 'disjoint' → both 'clear'), GREEN after.

Run:  python3 tests/test_lineshift_symbol_mapping.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402

DB = "veripsa_lineshift_" + str(os.getpid())
REPO = "acme/lineshift"


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
    checks = []
    try:
        import code_graph_extract as X
        from github_rest import changed_line_ranges_from_patch as parse_hunks

        # BASE file: a HUGE `top` (1-200), then foo (201-230), then bar (231-260). Symbol spans are base-relative.
        base = ["def top(a):\n"]
        for i in range(2, 200): base.append(f"    a=a+{i}\n")
        base.append("    return a\n")
        base.append("def foo(a):\n")
        for i in range(202, 230): base.append(f"    a=a+{i}\n")
        base.append("    return a\n")
        base.append("def bar(a):\n")
        for i in range(232, 260): base.append(f"    a=a+{i}\n")
        base.append("    return a\n")
        with tempfile.TemporaryDirectory() as d:
            with open(d + "/f.py", "w") as fh:
                fh.writelines(base)
            g = X.build_graph(d)
            real_blob = X._git_blob_sha(d + "/f.py")   # the App's base_hash for a FRESH PR == the graph content_hash

        db("veripsa_app", "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(g), REPO, "main", "a" * 40))

        def claim(cid, ranges, bh):
            db("veripsa_app", "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s)",
               (f"{cid}:f.py", "f.py", REPO, "main", cid.split("-")[0].lower(), json.dumps(ranges), bh))

        # PR-A (real `git diff` hunk headers): a CONTAINED `top` edit that nets +25 lines, then the SAME `foo`
        # edit at base line ~225 — whose hunk header NEW-side shifts to +247 because of the lines added above,
        # while its BASE-side stays -222. PR-B edits foo's base line directly. BOTH edit foo = a real collision.
        prA = parse_hunks("@@ -18,6 +18,31 @@ def top(a):\n ctx\n-    a=a+20\n+    a=a+20\n+more\n"
                          "@@ -222,7 +247,7 @@ def foo(a):\n ctx\n-    a=a+225\n+    a=a+999999\n ctx\n")
        prB = parse_hunks("@@ -222,7 +222,7 @@ def foo(a):\n ctx\n-    a=a+225\n+    a=a+111\n ctx\n")

        # (1) the parser returns BASE-side ranges — the contained line-adding hunk does NOT shift the later
        #     edit's numbers off its base symbol (foo's base span). On origin/main this was [[18,48],[247,253]].
        checks.append((f"parser returns BASE-side ranges for the shifted PR (got {prA})", prA == [[18, 23], [222, 228]]))
        checks.append((f"parser returns the unshifted PR's base range (got {prB})", prB == [[222, 228]]))

        claim("PR-A", prA, real_blob)   # FRESH base hash (PR base == analyzed graph)
        claim("PR-B", prB, real_blob)   # FRESH

        imp = db("veripsa_app", "SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
        if isinstance(imp, str):
            imp = json.loads(imp)
        verdicts = {c["change_id"]: c["verdict"] for c in imp.get("changes", [])}
        serialize_count = imp.get("serialize_count")

        # (2) the real same-symbol collision (both edit foo) is CAUGHT — not silently dropped to 'clear'.
        both_clear = verdicts.get("PR-A") == "clear" and verdicts.get("PR-B") == "clear"
        checks.append((f"the same-symbol collision is NOT a silent 'clear' (verdicts={verdicts})", not both_clear))
        checks.append((f"the colliding pair serializes (serialize_count={serialize_count})",
                       isinstance(serialize_count, int) and serialize_count >= 1))

        ok = True
        for name, cond in checks:
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
            ok = ok and bool(cond)
        print("LINE-SHIFT SYMBOL-MAPPING GATE:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)


if __name__ == "__main__":
    sys.exit(main())
