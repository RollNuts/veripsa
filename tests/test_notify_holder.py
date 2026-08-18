#!/usr/bin/env python3
"""NOTIFY-THE-HOLDER gate — tell the lane HOLDER who is now queued behind it (PO 2026-06-18).

The pre-fix gap appears when PR-B directly collides with an earlier PR-A on the same lines: Veripsa tells
PR-B "⏸ Wait in line — queued behind PR-A". But PR-A (the HOLDER, first in line) is NEVER told anyone is now
blocked ON it — its notice only mentions its own coupling. Yet "others are waiting on me" is useful, actionable
context for the holder: it reinforces "keep this change focused and land it promptly so you free the lane".

This proves the fix end to end against the REAL gate + extractor + renderer, content-free:

  (1) the ENGINE surface gives the holder `queued_behind` (the waiter PR's label) + `queued_behind_paths`
      (the shared lane) — the exact INVERSE of the waiter's `serialize_behind`.
  (2) the HOLDER's RENDERED PR comment includes the "in-flight PR(s) are waiting behind this one" line, naming
      the right COUNT, the waiter's PR, and the shared lane path.
  (3) a PR with NOBODY queued behind it (an isolated change) does NOT show the line (no false noise).
  (4) the count is right with MULTIPLE waiters behind one holder (and the waiter still gets its own "wait in line").
  (5) CONTENT-FREE: no diff body / secret token ever reaches the engine surface or the rendered holder notice.

Run:  python3 tests/test_notify_holder.py
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

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a per-PID name (like db/smoke.sh,
# run_gates, test_finer_collision) lets concurrent runs / sibling agents not drop each other's DB mid-run.
DB = "veripsa_holdertest_" + str(os.getpid())
REPO = "acme/holder"

# A SECRET-LOOKING token that lives ONLY in a diff BODY (never a hunk header). If it ever reaches the engine
# surface or the rendered holder notice, the content-free contract is broken.
SECRET = "SUPER_SECRET_TOKEN_holder_999"


def build_graph():
    """Extract a REAL graph for a file shared.py with ONE symbol `core`, plus a standalone file solo.py — so the
    spans come from the actual extractor (extractor → schema → engine path proven end to end)."""
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "shared.py"), "w") as fh:
            fh.write(
                "def core(x):\n"           # lines 1-3 — the one symbol two PRs will both edit
                "    y = x + 1\n"
                "    return y\n"
            )
        with open(os.path.join(d, "solo.py"), "w") as fh:
            fh.write(
                "def only(z):\n"           # an unrelated, uncontested file
                "    return z\n"
            )
        g = X.build_graph(d)
    return g


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    import render as R

    file_hashes = {}

    def db_app(sql, args=()):
        conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    def ingest(graph):
        db_app("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", "a" * 40))
        file_hashes.clear()
        for n in graph.get("nodes", []):
            if n.get("kind") == "file" and n.get("content_hash"):
                file_hashes[n["path"]] = n["content_hash"]

    def claim(cid, path, author, ranges):
        # base_hash = the file's CURRENT graph content hash ("__FRESH__") so the spans are provably valid and the
        # finer engine keeps a same-symbol collision as a real serialize (precision); ranges are content-free line ranges.
        rj = json.dumps(ranges) if ranges else None
        bh = file_hashes.get(path)
        db_app("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s)",
               (cid, path, REPO, "main", author, rj, bh))

    def reset_claims():
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account','ACCT-DEMO',true)")
            cur.execute("SELECT set_config('core.governed_write_token','claim',true)")
            cur.execute("UPDATE core.claim SET claim_state='released', released_at=now() WHERE claim_state IN ('active','waiting')")
        conn.close()

    def impact():
        imp = db_app("SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
        if isinstance(imp, str):
            imp = json.loads(imp)
        return {c["change_id"]: c for c in imp.get("changes", [])}, imp

    checks = []
    graph = build_graph()
    ingest(graph)

    # The content-free boundary: the diff BODY (with the SECRET) is parsed to a line RANGE and discarded.
    core_patch = (f"@@ -1,3 +1,3 @@\n def core(x):\n-    y = x + 1\n+    y = x + 1  # {SECRET}\n     return y\n")
    from github_rest import changed_line_ranges_from_patch as parse_hunks
    core_ranges = parse_hunks(core_patch)   # → [[1,3]]
    checks.append((f"hunk-header parse is content-free (ranges {core_ranges} carry no body / no secret)",
                   core_ranges == [[1, 3]] and SECRET not in json.dumps(core_ranges)))

    # ── (1)+(2) ONE holder, ONE waiter on the SAME symbol → a SURVIVING serialize. The HOLDER must learn it. ──
    # PR-118 claims shared.py first → it is the ACTIVE holder. PR-121 claims the SAME symbol → it WAITS behind 118.
    claim("PR-118", "shared.py", "alice", [[1, 2]])    # holder (active) — edits `core`
    claim("PR-121", "shared.py", "bob", [[2, 3]])      # waiter (waiting) — same symbol `core`
    ch, imp = impact()
    holder = ch.get("PR-118", {})
    waiter = ch.get("PR-121", {})

    # sanity: the wait actually survived (same symbol = a real serialize, not a finer-disjoint drop).
    checks.append((f"(setup) the waiter PR-121 serializes behind the holder (verdict='{waiter.get('verdict')}', "
                   f"serialize_behind={waiter.get('serialize_behind')})",
                   waiter.get("verdict") == "serialize" and any("PR-118" in s for s in (waiter.get("serialize_behind") or []))))

    # (1) the ENGINE gives the HOLDER the inverse: who is queued behind it + on which lane.
    qb = holder.get("queued_behind") or []
    qbp = holder.get("queued_behind_paths") or []
    checks.append((f"(1) ENGINE: the holder PR-118 carries queued_behind naming the waiter (got {qb})",
                   any("PR-121" in s for s in qb)))
    checks.append((f"(1) ENGINE: the holder PR-118 carries the shared lane path in queued_behind_paths (got {qbp})",
                   qbp == ["shared.py"]))
    # the holder is NOT itself waiting (it has no serialize_behind) — it HOLDS the lane.
    checks.append(("(1) ENGINE: the holder has nothing in its own serialize_behind (it is first in line, not waiting)",
                   not (holder.get("serialize_behind") or [])))

    # (2) the HOLDER's RENDERED comment includes the "waiting behind this one" line, with COUNT + waiter PR + lane.
    rendered_holder = R.render_pr_check(imp, "PR-118")
    hbody = rendered_holder.get("comment") or ""
    checks.append(("(2) RENDER: the holder PR-118 gets a comment (not skipped) because work is queued behind it",
                   rendered_holder.get("comment") is not None))
    checks.append(("(2) RENDER: the holder notice states the COUNT '1 in-flight PR ... is waiting behind this one'",
                   "1 in-flight PR" in hbody and "waiting behind this one" in hbody))
    checks.append((f"(2) RENDER: the holder notice names the waiter PR-121 (got contains PR-121 = "
                   f"{'PR-121' in hbody})", "PR-121" in hbody))
    checks.append(("(2) RENDER: the holder notice names the shared lane `shared.py`",
                   "`shared.py`" in hbody))
    checks.append(("(2) RENDER: the holder notice is advisory (nothing is blocked) + keeps the never-block conclusion",
                   "Advisory" in hbody and rendered_holder.get("conclusion") in ("success", "neutral")))

    # ── (2b) CLEAR-COPY HONESTY (FINDING A regression guard) ──────────────────────────────────────────────────
    # The holder PR-118's verdict is 'clear' (it is not itself waiting), yet it carries a CO-SIGNAL (queued_behind:
    # a waiter is queued behind it). The clear copy must NOT be the ABSOLUTE "nothing else in flight touches this" /
    # "no other in-flight change touches your files" — that self-contradicts the very "1 in-flight PR is waiting
    # behind this one" line in the same comment. It must switch to a non-absolute, true phrasing ("nothing is
    # blocking you"). Assert across ALL THREE always-visible surfaces (the check title, the check summary, and the
    # comment header) — they were the contradiction, and they ship together.
    htitle = rendered_holder.get("title") or ""
    hsummary = rendered_holder.get("summary") or ""
    hhead = hbody.split("\n")[1] if "\n" in hbody else hbody     # the bold verdict header line (right under "### Veripsa")
    holder_blob_lower = (htitle + " " + hsummary + " " + hbody).lower()
    checks.append((f"(2b) FINDING A: the clear-holder verdict IS 'clear' (the contradiction precondition) — "
                   f"verdict='{holder.get('verdict')}'", holder.get("verdict") == "clear"))
    checks.append(("(2b) FINDING A: the clear-holder does NOT render the absolute 'nothing else ... touches this' "
                   "copy (it would contradict 'a PR is waiting behind this one') across title+summary+comment",
                   "nothing else" not in holder_blob_lower
                   and "no other in-flight change touches" not in holder_blob_lower))
    checks.append((f"(2b) FINDING A: the clear-holder check SUMMARY uses the non-absolute 'nothing is blocking you' "
                   f"phrasing (got: {hsummary[:70]!r})", "nothing is blocking you" in hsummary.lower()))
    checks.append((f"(2b) FINDING A: the clear-holder check TITLE is the base signal 'Veripsa — Clear' — NEVER the "
                   f"forbidden 'Clear to land' base-signal name (got: {htitle!r})",
                   htitle.strip() == "Veripsa — Clear" and "clear to land" not in htitle.lower()))
    checks.append((f"(2b) FINDING A: the clear-holder COMMENT header is the non-absolute lead (got: {hhead!r})",
                   "nothing is blocking you" in hhead.lower()))

    # ── (3) a PR with NOBODY behind it must NOT show the line (no false noise). ──
    # The WAITER PR-121 itself has nobody queued behind IT → its rendered comment (a "wait in line" comment) must
    # NOT carry the holder-notification line. (And a totally isolated clear PR shows neither comment nor line.)
    rendered_waiter = R.render_pr_check(imp, "PR-121")
    wbody = rendered_waiter.get("comment") or ""
    checks.append(("(3) RENDER: the WAITER (nobody behind it) does NOT show 'waiting behind this one'",
                   "waiting behind this one" not in wbody))
    checks.append((f"(3) ENGINE: the waiter PR-121 has an EMPTY queued_behind (got {waiter.get('queued_behind')})",
                   not (waiter.get("queued_behind") or [])))

    # an isolated clear PR: solo.py, uncontested → clear, no comment at all, certainly no holder line.
    reset_claims()
    claim("PR-200", "solo.py", "carol", [[1, 1]])
    ch2, imp2 = impact()
    solo = ch2.get("PR-200", {})
    rendered_solo = R.render_pr_check(imp2, "PR-200")
    checks.append((f"(3) an isolated clear PR has empty queued_behind (got {solo.get('queued_behind')}) and posts "
                   f"NO comment (verdict='{solo.get('verdict')}')",
                   not (solo.get("queued_behind") or []) and rendered_solo.get("comment") is None))
    # (3b) FINDING A counter-case: a clear PR with NO co-signal KEEPS the absolute copy (the fix must not soften
    # the genuine "all clear" message). Its check summary still names the absolute "no other in-flight change
    # touches your files" — true here (nobody waits behind it, no shared foundation, no cluster).
    checks.append(("(3b) FINDING A: a clear PR with NO co-signal keeps the absolute 'no other in-flight change "
                   "touches your files' copy (the fix only softens the co-signal case)",
                   "no other in-flight change touches" in (rendered_solo.get("summary") or "").lower()))

    # ── (4) MULTIPLE waiters behind one holder → the holder's count is N, and each waiter still waits in line. ──
    reset_claims()
    claim("PR-300", "shared.py", "dave", [[1, 2]])     # holder
    claim("PR-301", "shared.py", "erin", [[2, 3]])     # waiter 1 (same symbol)
    claim("PR-302", "shared.py", "fred", [[1, 3]])     # waiter 2 (same symbol)
    ch3, imp3 = impact()
    holder3 = ch3.get("PR-300", {})
    qb3 = holder3.get("queued_behind") or []
    checks.append((f"(4) ENGINE: the holder PR-300 has BOTH waiters in queued_behind (got {qb3})",
                   any("PR-301" in s for s in qb3) and any("PR-302" in s for s in qb3) and len(qb3) == 2))
    rendered_h3 = R.render_pr_check(imp3, "PR-300")
    h3body = rendered_h3.get("comment") or ""
    checks.append(("(4) RENDER: the holder notice pluralizes + counts '2 in-flight PRs are waiting behind this one'",
                   "2 in-flight PRs are waiting behind this one" in h3body))

    # ── (5) CONTENT-FREE: the diff body / secret never reaches the engine surface nor the holder notice. ──
    reset_claims()
    claim("PR-400", "shared.py", "gina", core_ranges)  # holder, ranges came from a patch carrying the SECRET in its body
    claim("PR-401", "shared.py", "hank", [[1, 2]])     # waiter (same symbol)
    _, imp4 = impact()
    checks.append(("(5) CONTENT-FREE: the secret token is NEVER in the engine surface output",
                   SECRET not in json.dumps(imp4)))
    rendered_h4 = R.render_pr_check(imp4, "PR-400")
    h4blob = (rendered_h4.get("summary") or "") + (rendered_h4.get("comment") or "")
    checks.append(("(5) CONTENT-FREE: the secret token is NEVER in the rendered holder notice",
                   SECRET not in h4blob))
    # the holder notice still fired here (regression guard the content-free path didn't accidentally suppress it).
    checks.append(("(5) the holder PR-400 still shows the 'waiting behind this one' line (content-free path intact)",
                   "waiting behind this one" in (rendered_h4.get("comment") or "")))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("NOTIFY-HOLDER GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
