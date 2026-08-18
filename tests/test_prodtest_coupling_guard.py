#!/usr/bin/env python3
"""PROD→TEST call-coupling guard gate (false-coupling audit r3 2026-06-19).

THE BUG (RED on origin/main): _claim_adjacency keeps an import-UNCONFIRMED call coupling when the called name
has a SINGLE definer (the unambiguous-recall rule). But a ubiquitous receiver/method name (.pop()/.end()/
.is_a?/.Encode()/verify…) that a TEST file happens to define ONCE, called from a PRODUCTION file, is a
coincidental name match — production code does NOT depend on a test file. Measured 8–40 such impossible
prod→test couplings per repo on axios/gin/sinatra (a lib file "coupled" to a *_test file it cannot import).

THE FIX: a PROD→TEST asymmetry guard on out_adj/in_adj — drop a single-definer import-unconfirmed coupling
whose DEFINER is a test file and whose CALLER is NOT a test file. Recall-safe: import-CONFIRMED links survive,
prod→prod single-definer survives (the existing recall rule), and a TEST→test coupling survives (two tests on
one fixture still coordinate). core._is_test_path detects test paths (segments + basename patterns).

Three constructed cases on the live core.main_impact_surface:
  (1) FIX — prod `app/worker.py` calls `verify_token` defined ONLY in `tests/mock_test.py` (no import) →
      NOT contested (the impossible prod→test coupling is dropped).
  (2) RECALL — prod `app/service.py` calls `charge` defined ONLY in prod `app/billing.py` (no import) →
      STILL contested (prod→prod single-definer kept).
  (3) RECALL — test `tests/api_test.py` calls `seed_fixture` defined ONLY in `tests/fixtures_test.py` →
      STILL contested (a test→test coupling is legitimate — two tests on one fixture coordinate).

PROCESS-UNIQUE scratch DB (parallel-safe). Run:  python3 tests/test_prodtest_coupling_guard.py
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
import code_graph_extract as X  # noqa: E402

DB = "veripsa_ptguard_" + str(os.getpid())


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


def _ingest(repo, files):
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        g = X.build_graph(d)
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(g), repo, "main", "a" * 40))


def _claim(cid, path, author, repo):
    db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, repo, "main", author))


def _surface(repo):
    imp = db("SELECT core.main_impact_surface(%s,%s)", (repo, "main"))
    if isinstance(imp, str):
        imp = json.loads(imp)
    return {c["change_id"]: c for c in imp.get("changes", [])}


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1
    checks = []

    # (1) FIX — prod caller, TEST definer, no import → NOT contested.
    repo1 = "pt/prodtest"
    _ingest(repo1, {
        "app/worker.py":     "def run():\n    return verify_token()\n",          # prod caller, no import
        "tests/mock_test.py": "def verify_token():\n    return 'mock'\n",          # the ONLY definer — a TEST file
    })
    _claim("PR-W:app/worker.py", "app/worker.py", "wdev", repo1)
    _claim("PR-T:tests/mock_test.py", "tests/mock_test.py", "tdev", repo1)
    s1 = _surface(repo1)
    w1 = s1.get("PR-W", {})
    cw1 = w1.get("contested_with", []) or []
    checks.append(("(1) prod worker calling a name defined ONLY in a TEST file is NOT contested with it "
                   f"(verdict='{w1.get('verdict')}', contested_with={cw1})",
                   w1.get("verdict") == "clear" and not any("tdev" in str(x) for x in cw1)))

    # (2) RECALL — prod caller, PROD definer, no import → STILL contested (single-definer rule preserved).
    repo2 = "pt/prodprod"
    _ingest(repo2, {
        "app/service.py": "def run():\n    return charge()\n",                    # prod caller, no import
        "app/billing.py": "def charge():\n    return 1\n",                        # the ONLY definer — a PROD file
    })
    _claim("PR-S:app/service.py", "app/service.py", "sdev", repo2)
    _claim("PR-B:app/billing.py", "app/billing.py", "bdev", repo2)
    s2 = _surface(repo2)
    sv2 = s2.get("PR-S", {})
    cw2 = sv2.get("contested_with", []) or []
    checks.append(("(2) prod→prod single-definer coupling is PRESERVED (recall kept) "
                   f"(verdict='{sv2.get('verdict')}', contested_with={cw2})",
                   sv2.get("verdict") in ("warn", "serialize") and any("bdev" in str(x) for x in cw2)))

    # (3) RECALL — TEST caller, TEST definer → STILL contested (two tests on one fixture must coordinate).
    repo3 = "pt/testtest"
    _ingest(repo3, {
        "tests/api_test.py":      "def test_api():\n    return seed_fixture()\n",  # test caller
        "tests/fixtures_test.py": "def seed_fixture():\n    return {}\n",          # the ONLY definer — also a test
    })
    _claim("PR-A:tests/api_test.py", "tests/api_test.py", "adev", repo3)
    _claim("PR-F:tests/fixtures_test.py", "tests/fixtures_test.py", "fdev", repo3)
    s3 = _surface(repo3)
    a3 = s3.get("PR-A", {})
    cw3 = a3.get("contested_with", []) or []
    checks.append(("(3) test→test single-definer coupling is PRESERVED (the guard only drops prod→test) "
                   f"(verdict='{a3.get('verdict')}', contested_with={cw3})",
                   a3.get("verdict") in ("warn", "serialize") and any("fdev" in str(x) for x in cw3)))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("PROD-TEST COUPLING GUARD GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
