#!/usr/bin/env python3
"""GOD-FILE SPLIT gate — Veripsa flags a big, churny file (server.py-class) as a split candidate even when its
fan-in is LOW (PO 2026-06-18 "やって": add the size×churn arm so the product catches god-files, not only fan-in
foundations). End-to-end over the REAL extractor + engine + renderer:

  (1) GOD-FILE: a file that DEFINES many symbols (≥ split_min_symbols) AND churns, but is imported by ~nobody,
      appears in that PR's shared_foundation with basis='god_file' and its symbol count (an ENGINE-side fact) —
      and the rendered PR comment recommends splitting it, conveying "one file doing many things" QUALITATIVELY
      (NOT the raw symbol count: that count is the graph-size moat — PO 2026-06-21 「file count はダメ」).
  (2) FOUNDATION: a widely-imported, churny file still fires with basis='foundation' (fan-in arm intact).
  (3) NEGATIVE: a small, low-fan-in file (even if it churns) is NOT flagged — no wallpaper.
  (4) ADVISORY + CONTENT-FREE: the god-file PR's check conclusion is unchanged (never blocks); only the path +
      counts cross, never a body.

Run after bootstrap:  python3 tests/test_godfile_split.py
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
import render    # noqa: E402

DB = "veripsa_godfile_" + str(os.getpid())
REPO = "acme/godfile"


def build_graph():
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        # GOD-FILE: 30 top-level functions, imported by NOBODY (low fan-in, huge body).
        with open(os.path.join(d, "godfile.py"), "w") as fh:
            fh.write("\n".join(f"def f{i}():\n    return {i}\n" for i in range(30)))
        # FOUNDATION: a small module imported by 6 leaves (fan-in 6).
        with open(os.path.join(d, "foundation.py"), "w") as fh:
            fh.write("def base():\n    return 0\n")
        for i in range(6):
            with open(os.path.join(d, f"leaf_{i}.py"), "w") as fh:
                fh.write(f"from foundation import base\n\ndef use_{i}():\n    return base()\n")
        # NORMAL: tiny file, one importer — the negative control.
        with open(os.path.join(d, "normal.py"), "w") as fh:
            fh.write("def a():\n    return 1\n\ndef b():\n    return 2\n")
        with open(os.path.join(d, "uses_normal.py"), "w") as fh:
            fh.write("from normal import a\n\ndef g():\n    return a()\n")
        # VENDORED + GENERATED: a 30-symbol file in vendor/ with a _pb2 suffix — a poor split target (you regenerate
        # it, you don't hand-restructure it). Must be EXCLUDED from split advice despite clearing the size×churn bar.
        os.makedirs(os.path.join(d, "vendor"), exist_ok=True)
        with open(os.path.join(d, "vendor", "lib_pb2.py"), "w") as fh:
            fh.write("\n".join(f"def v{i}():\n    return {i}\n" for i in range(30)))
        # TEST god-file: a 30-symbol file under tests/ — a LEGITIMATE split target (we split test_server.py itself).
        # Must STAY flagged (tests are intentionally NOT excluded).
        os.makedirs(os.path.join(d, "tests"), exist_ok=True)
        with open(os.path.join(d, "tests", "test_big.py"), "w") as fh:
            fh.write("\n".join(f"def t{i}():\n    return {i}\n" for i in range(30)))
        return X.build_graph(d)


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

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

    graph = build_graph()
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", "a" * 40))

    # CHURN: each hotspot file lands 3 times on main (≥ split_min_churn default 3). normal.py lands too (3) so the
    # control proves it is the fan-in/symbols arms — NOT churn — that gates it OUT.
    def land(path, n):
        # the sha must be HEX (record_landing skips non-hex as a delete push); a distinct sha per landing → churn=n
        for k in range(n):
            db("SELECT core.record_landing_with_authority(%s,%s,%s,%s,%s)", (REPO, "main", "%040x" % (k + 1), [path], "lander"))
    land("godfile.py", 3)
    land("foundation.py", 3)
    land("normal.py", 3)
    land("vendor/lib_pb2.py", 3)
    land("tests/test_big.py", 3)

    # IN-FLIGHT PRs (different authors) each touching one of the files.
    def claim(cid, path, author):
        db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, REPO, "main", author))
    claim("PR-GOD:godfile.py", "godfile.py", "ada")
    claim("PR-FOUND:foundation.py", "foundation.py", "ben")
    claim("PR-NORM:normal.py", "normal.py", "cleo")
    claim("PR-VEND:vendor/lib_pb2.py", "vendor/lib_pb2.py", "dan")
    claim("PR-TEST:tests/test_big.py", "tests/test_big.py", "eve")

    imp = db("SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
    if isinstance(imp, str):
        imp = json.loads(imp)
    by = {c["change_id"]: c for c in imp.get("changes", [])}

    def sf_for(cid):
        return {e["path"]: e for e in (by.get(cid, {}).get("shared_foundation", []) or [])}

    checks = []

    # (1) GOD-FILE: flagged with basis='god_file' + its symbol count, despite ~zero fan-in.
    # NB: the engine's change_id is the PR prefix ("PR-GOD") — the ":<path>" suffix groups a PR's per-path claims.
    g = sf_for("PR-GOD").get("godfile.py")
    checks.append((f"(1) ENGINE: a 30-symbol low-fan-in file is flagged basis='god_file' "
                   f"(got {None if not g else (g.get('basis'), 'sym='+str(g.get('symbols')), 'fan='+str(g.get('fan_in')))})",
                   bool(g) and g.get("basis") == "god_file" and g.get("symbols", 0) >= 25 and g.get("fan_in", 0) < 5))

    # (2) FOUNDATION: the widely-imported file still fires on the fan-in arm.
    f = sf_for("PR-FOUND").get("foundation.py")
    checks.append((f"(2) ENGINE: a 6-importer file still fires basis in (foundation,both) "
                   f"(got {None if not f else (f.get('basis'), 'fan='+str(f.get('fan_in')))})",
                   bool(f) and f.get("basis") in ("foundation", "both") and f.get("fan_in", 0) >= 5))

    # (3) NEGATIVE: the small file is NOT flagged even though it churns (no wallpaper).
    nrm = sf_for("PR-NORM")
    checks.append((f"(3) ENGINE: a tiny low-fan-in file is NOT flagged despite churn (got keys={list(nrm)})",
                   "normal.py" not in nrm))

    # (3b) EXCLUSION: a vendored/generated god-file (vendor/lib_pb2.py) is NOT advised for splitting — a poor split
    #      target (you regenerate it). The PREDICTIVE arms must suppress it despite clearing size×churn.
    vend = sf_for("PR-VEND")
    checks.append((f"(3b) ENGINE: a vendored/generated god-file (vendor/lib_pb2.py) is EXCLUDED from split advice "
                   f"(got keys={list(vend)})", "vendor/lib_pb2.py" not in vend))

    # (3c) TEST files stay ELIGIBLE: a god TEST file (tests/test_big.py) IS still flagged — we split test_server.py
    #      ourselves, so tests are intentionally NOT excluded.
    tst = sf_for("PR-TEST").get("tests/test_big.py")
    checks.append((f"(3c) ENGINE: a god TEST file (tests/test_big.py) STAYS flagged basis='god_file' (not excluded) "
                   f"(got {None if not tst else (tst.get('basis'), 'sym='+str(tst.get('symbols')))})",
                   bool(tst) and tst.get("basis") == "god_file" and tst.get("symbols", 0) >= 25))

    # (4) RENDER: the god-file PR comment recommends splitting and states its real reason QUALITATIVELY — "defines
    #     many distinct pieces / one file doing many things" — NEVER the raw symbol COUNT (that count is the graph-
    #     size moat; PO 2026-06-21 「file count はダメ」). Advisory conclusion, content-free (path only, no body).
    out = render.render_pr_check(imp, "PR-GOD")
    comment = (out.get("comment") or "")
    lc = comment.lower()
    # the engine computed ~30 symbols (asserted at the ENGINE level above); the customer-facing render must convey
    # "this file does too many things" WITHOUT the number — so we assert the qualitative phrase AND that the raw
    # symbol count (30) and a "N symbol(s)" shape never reach the comment.
    checks.append(("(4) RENDER: the god-file PR comment recommends splitting and states its reason qualitatively "
                   "('defines many distinct pieces' / 'one file doing many things'), with NO raw symbol count "
                   "(30 / 'N symbols') leaked (MOAT — PO 「file count はダメ」)",
                   "consider splitting" in lc and "godfile.py" in comment
                   and ("defines many distinct pieces" in lc or "one file doing many things" in lc)
                   and "30 symbol" not in lc and "**30" not in comment and "symbols**" not in lc))
    checks.append((f"(4) ADVISORY: the god-file advice does not fail the check (conclusion={out.get('conclusion')})",
                   out.get("conclusion") in ("success", "neutral")))

    ok = all(p for _, p in checks)
    for desc, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {desc}")
    print("GODFILE-SPLIT GATE: PASS" if ok else "GODFILE-SPLIT GATE: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
    sys.exit(rc)
