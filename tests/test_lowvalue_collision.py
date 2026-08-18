#!/usr/bin/env python3
"""LOW-VALUE COLLISION SOFTENING gate — PRECISION / anti-wallpaper (品質=正確な沈黙, not 鳴りっぱなし).

A synthetic fixture gets a HARD "⏸ Wait in line" for adjacent edits in `run_gates.sh`. But
run_gates.sh is a build/test-RUNNER registration list: every audit PR APPENDS its own gate-invocation block,
so concurrent PRs textually "collide" on adjacent lines there. That is a 5-second git-conflict anyone resolves
— NOT meaningful logic coupling. Treating it with the SAME hard-serialize severity as a real collision in
github-app/server.py is OVER-SERIALIZATION = the wallpaper Veripsa explicitly avoids.

The fix SOFTENS a surviving direct collision to 'serialize_soft' (a low-stakes "heads up", STILL surfaced,
never silent) WHEN — and only when — EVERY surviving collision for the change is on a low-value append-mostly
build/test-RUNNER / registration file. A low-value file must NEVER MASK a real collision: the moment the SAME
PR pair ALSO collides on a real source file, the verdict is the hard 'serialize' again.

Proven against the REAL gate + the REAL extractor (run_gates.sh → a bare file node with NO symbol spans;
github-app/server.py → real `def` spans), using ONLY line metadata (content-free):

  (1) two PRs colliding ONLY on run_gates.sh        → 'serialize_soft'  (softened, NOT hard) — and STILL surfaced
  (2) two PRs colliding ONLY on github-app/server.py (same symbol) → hard 'serialize' (a real logic collision)
  (3) a pair colliding on BOTH run_gates.sh AND server.py         → hard 'serialize' (low-value never masks real)
  (4) the predicate itself: runner/registration scripts are low-value; real source / random .sh / deploy.sh are NOT
  (5) the rendered surface: a soft collision reads "Heads up" (NOT "Wait in line") AND still NAMES the file

Run:  python3 tests/test_lowvalue_collision.py
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
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run. Per-PID, exactly
# like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_finer_collision.py.
DB = "veripsa_lvcolltest_" + str(os.getpid())
REPO = "acme/lvcoll"


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
    """REAL extraction: run_gates.sh (a shell RUNNER → a BARE file node, NO symbol spans, like every .sh) +
    github-app/server.py (two real `def` spans). Proves the extractor → schema → engine path end to end."""
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "github-app"))
        with open(os.path.join(d, "run_gates.sh"), "w") as fh:
            fh.write("#!/usr/bin/env bash\n" + "".join(f"echo gate{i}\n" for i in range(1, 20)))   # an append-only list
        with open(os.path.join(d, "github-app", "server.py"), "w") as fh:
            fh.write("def handle_a(x):\n    return x\n\ndef handle_b(y):\n    return y\n")
        return X.build_graph(d)


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    import render as R

    checks = []
    file_hashes = {}

    def ingest(graph):
        db("veripsa_app", "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
           (json.dumps(graph), REPO, "main", "a" * 40))
        file_hashes.clear()
        for n in graph.get("nodes", []):
            if n.get("kind") == "file" and n.get("content_hash"):
                file_hashes[n["path"]] = n["content_hash"]

    def claim(cid, path, author, ranges):
        # base_hash = the same hash main's graph stored for this file (FRESH) so a server.py same-symbol overlap
        # is provably valid and the finer engine keeps it (real collision). run_gates.sh has no spans either way.
        rj = json.dumps(ranges) if ranges else None
        bh = file_hashes.get(path)
        db("veripsa_app", "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s)",
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
        imp = db("veripsa_app", "SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
        if isinstance(imp, str):
            imp = json.loads(imp)
        return {c["change_id"]: c for c in imp.get("changes", [])}, imp

    ingest(build_graph())

    # ── (4) the predicate (run the REAL function) ──────────────────────────────────────────────────────────
    for p, want in [
        ("run_gates.sh", True), ("db/smoke.sh", True), ("dogfood.sh", True), ("run_tests.sh", True),
        ("ci.sh", True), ("tests.sh", True),
        # word-boundary-anchored runner names that MUST still match (the legitimate class):
        ("ci_gates.sh", True), ("release-gates.sh", True),   # *_gates.sh / *-gates.sh
        ("smoke.sh", True), ("smoke_db.sh", True), ("e2e_smoke.sh", True),  # smoke.sh / smoke_*.sh / *_smoke.sh
        ("dogfood_seed.sh", True),
        ("github-app/server.py", False), ("db/schema/80_contention.sql", False),
        ("deploy.sh", False),          # a deploy script is NOT a test/registration runner → hard (conservative)
        ("src/widget.sh", False),      # a random .sh is NOT a runner → hard (conservative)
        ("watch.py", False),
        # ── PRECISION (the FIX): real SOURCE filenames that merely CONTAIN 'gates'/'smoke' must NOT match. The
        #    old bare `%gates%.sh` / `%smoke%.sh` over-matched these, silently SOFTENING a real same-function
        #    collision on a genuine script = an under-warn. Word-boundary anchoring excludes them.
        ("aggregates.sh", False), ("propagates.sh", False), ("delegates.sh", False),  # end in 'gates.sh' but NOT runners
        ("src/aggregates.sh", False),                                                 # with a dir prefix too
        ("smokestack.sh", False), ("smokescreen.sh", False),                          # contain 'smoke' but NOT runners
    ]:
        got = db("veripsa_migrator", "SELECT set_config('core.current_account','ACCT-DEMO',true); "
                                     "SELECT core._is_low_value_collision_path(%s)", (p,))
        checks.append((f"_is_low_value_collision_path({p!r}) == {want} (got {got})", got == want))

    # ── (1) collide ONLY on run_gates.sh → 'serialize_soft' (softened, NOT hard) ───────────────────────────
    reset_claims()
    claim("PR-1:run_gates.sh", "run_gates.sh", "alice", [[10, 12]])   # appends a gate block
    claim("PR-2:run_gates.sh", "run_gates.sh", "bob", [[11, 13]])     # overlaps on append order — low-value
    ch, imp = impact()
    pr2 = ch.get("PR-2", {})
    checks.append((f"(1) collide ONLY on run_gates.sh → 'serialize_soft' (softened) (verdict='{pr2.get('verdict')}')",
                   pr2.get("verdict") == "serialize_soft"))
    checks.append((f"(1) NOT hard 'serialize' (serialize_count={imp.get('serialize_count')}, "
                   f"serialize_soft_count={imp.get('serialize_soft_count')})",
                   imp.get("serialize_count") == 0 and imp.get("serialize_soft_count") >= 1))
    # STILL SURFACED (never silent): the soft change keeps WHO it's behind + a rendered comment.
    checks.append(("(1) STILL surfaced: a soft collision keeps serialize_behind (named holder)",
                   bool(pr2.get("serialize_behind"))))

    # ── (2) collide ONLY on github-app/server.py (SAME symbol) → hard 'serialize' ──────────────────────────
    reset_claims()
    claim("PR-3:server.py", "github-app/server.py", "carol", [[1, 2]])   # handle_a (lines 1-2)
    claim("PR-4:server.py", "github-app/server.py", "dave", [[1, 2]])    # handle_a — SAME symbol
    ch, imp = impact()
    pr4 = ch.get("PR-4", {})
    checks.append((f"(2) collide on real source (same symbol) → hard 'serialize' (verdict='{pr4.get('verdict')}')",
                   pr4.get("verdict") == "serialize"))
    checks.append((f"(2) hard serialize counted, NOT soft (serialize_count={imp.get('serialize_count')}, "
                   f"serialize_soft_count={imp.get('serialize_soft_count')})",
                   imp.get("serialize_count") >= 1 and imp.get("serialize_soft_count") == 0))

    # ── (3) collide on BOTH run_gates.sh AND server.py → hard 'serialize' (low-value must NOT mask real) ────
    reset_claims()
    # holder reserves BOTH files; waiter overlaps the holder on BOTH (server.py same symbol + run_gates.sh).
    claim("PR-5:run_gates.sh", "run_gates.sh", "erin", [[10, 12]])
    claim("PR-5:server.py", "github-app/server.py", "erin", [[1, 2]])      # handle_a
    claim("PR-6:run_gates.sh", "run_gates.sh", "frank", [[11, 13]])        # low-value overlap
    claim("PR-6:server.py", "github-app/server.py", "frank", [[1, 2]])     # handle_a — REAL same-symbol overlap
    ch, imp = impact()
    pr6 = ch.get("PR-6", {})
    behind_paths = {cp.get("path") for cp in (pr6.get("collision_points") or [])}
    checks.append((f"(3) collide on BOTH → hard 'serialize' (the real collision wins; verdict='{pr6.get('verdict')}')",
                   pr6.get("verdict") == "serialize"))
    checks.append((f"(3) the low-value file did NOT mask the real one (collision_points span both files: {sorted(behind_paths)})",
                   "github-app/server.py" in behind_paths))

    # ── (5) the rendered surface: soft = "Heads up" (NOT "Wait in line") and STILL names the file ──────────
    reset_claims()
    claim("PR-7:run_gates.sh", "run_gates.sh", "gina", [[10, 12]])
    claim("PR-8:run_gates.sh", "run_gates.sh", "hank", [[11, 13]])
    ch, imp = impact()
    rendered = R.render_pr_check(imp, "PR-8")
    blob = (rendered.get("summary") or "") + "\n" + (rendered.get("comment") or "")
    title = rendered.get("title") or ""
    checks.append((f"(5) soft collision: the check TITLE leads 'Heads up', NOT 'Wait in line' (got '{title}')",
                   "Heads up" in title and "Wait in line" not in title))
    checks.append(("(5) the check conclusion is non-blocking (advisory; never blocks a merge)",
                   rendered.get("conclusion") in ("neutral", "success")))
    checks.append(("(5) the rendered comment STILL names the low-value file (surfaced, not silent)",
                   rendered.get("comment") is not None and "run_gates.sh" in blob))
    checks.append(("(5) the soft comment does NOT say the hard 'Wait in line.'",
                   "Wait in line." not in (rendered.get("comment") or "")))

    # ── (6) CONTRAST: the REAL server.py collision renders the HARD 'Wait in line' (the hard path is intact) ─
    reset_claims()
    claim("PR-9:server.py", "github-app/server.py", "ivy", [[1, 2]])
    claim("PR-10:server.py", "github-app/server.py", "jay", [[1, 2]])
    ch, imp = impact()
    r2 = R.render_pr_check(imp, "PR-10")
    # WORDING AUDIT 2026-06-20: the hard-collision TITLE still leads "Wait in line" (the verdict headline, the
    # contrast vs the soft "Heads up"), but the comment BODY now leads with the ACTION "**Land in order.**"
    # instead of a duplicate "**Wait in line.**" (the headline was the same phrase twice — see the pause-comment
    # wording fix). So the hard path is still distinctly a HARD wait: title = "Wait in line", body = "Land in order."
    checks.append((f"(6) a REAL source collision still renders the hard wait (title='{r2.get('title')}': leads "
                   f"'Wait in line'; body leads the action 'Land in order.')",
                   "Wait in line" in (r2.get("title") or "") and "Land in order." in (r2.get("comment") or "")))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("LOW-VALUE COLLISION GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
