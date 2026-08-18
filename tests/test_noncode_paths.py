#!/usr/bin/env python3
"""NON-CODE PATH gate — a doc/asset/lock touched alongside code must NOT drag the whole PR to 'unknown'.

The pre-fix regression appears when a change touches a documentation file plus an analyzed source file and the
App returns "❓ Not analyzed". core.main_impact_surface flags a change 'unknown' when ANY claimed path is absent
from main's graph — and a doc (.md / LICENSE / image / lock file) is never in the graph. So a single README
dragged an otherwise-clear PR to the scary 'Not analyzed' verdict. Since nearly every real PR touches a
non-code file, this fired on most normal PRs — pure reviewer-burden noise that erodes trust in the signal.

The fix filters non-code paths out of the claim set BEFORE they become claims
(server._code_paths → code_graph_extract.is_noncode_path). This proves, against the REAL gate + a real
ingested graph:
  1. the predicate is correct (docs/images/locks → non-code; .py/config/unsupported-source/.gate → code/kept);
  2. the server wiring (server._code_paths) drops docs/assets and keeps code (incl. unsupported-language src/gates);
  3. WITHOUT the filter, claiming {code, README.md} → verdict 'unknown' (reproduces the regression);
  4. WITH the filter, claiming the survivors → verdict is NOT 'unknown' (the fix);
  5. honest recall preserved: a NEW un-indexed .py is KEPT by the filter and still verdicts 'unknown'.
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
import server as S  # noqa: E402  — exercise the REAL server filter wiring, not a re-implementation

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
# Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_server.py.
DB = "veripsa_noncodetest_" + str(os.getpid())
REPO = "acme/noncode"


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


def build_graph(d):
    # two independent, in-graph solo files (no coupling between them → a 'clear' baseline, so any 'unknown'
    # we see is caused ONLY by a non-code path, never by adjacency noise).
    with open(os.path.join(d, "code_a.py"), "w") as fh:
        fh.write("def solo_a():\n    return 1\n")
    with open(os.path.join(d, "code_b.py"), "w") as fh:
        fh.write("def solo_b():\n    return 2\n")
    os.makedirs(os.path.join(d, "gates.d"), exist_ok=True)
    with open(os.path.join(d, "gates.d", "159-billing_role.gate"), "w") as fh:
        fh.write('register_gate "tests/test_billing_role.py" "BILLING ROLE GATE: PASS" "billing role" "ok" "desc" 16\n')
    return X.build_graph(d)


def main() -> int:
    checks = []

    # ---- 1) the predicate ----
    for p, want in [
        ("README.md", True), ("docs/guide.mdx", True), ("LICENSE", True), ("assets/logo.png", True),
        ("package-lock.json", True), ("go.sum", True), (".gitignore", True),
        ("code_a.py", False), ("github-app/server.py", False), ("config/app.yaml", False),
        ("gates.d/159-billing_role.gate", False),
        ("Makefile", False), ("src/lib.rs", False),  # .rs = source in a possibly-unsupported lang → KEPT
        ("go.mod", False),                            # module deps ARE coupling-relevant → KEPT
    ]:
        got = X.is_noncode_path(p)
        checks.append((f"is_noncode_path({p!r}) == {want} (got {got})", got == want))

    # ---- 2) the real server filter wiring ----
    filtered = S._code_paths([
        "code_a.py", "README.md", "LICENSE", "src/lib.rs", "gates.d/159-billing_role.gate", "docs/x.png"
    ])
    checks.append((f"server._code_paths drops docs/assets, keeps code + unsupported-lang src/gates (got {filtered})",
                   filtered == ["code_a.py", "src/lib.rs", "gates.d/159-billing_role.gate"]))

    # ---- 3) end-to-end against the real surface ----
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    with tempfile.TemporaryDirectory() as d:
        graph = build_graph(d)
    paths = {n.get("path") for n in graph.get("nodes", []) if n.get("kind") == "file"}
    checks.append(("build_graph emits a bare file node for hand-authored .gate release gates",
                   "gates.d/159-billing_role.gate" in paths))
    gate_node = next(
        (
            n for n in graph.get("nodes", [])
            if n.get("kind") == "file"
            and n.get("path") == "gates.d/159-billing_role.gate"
        ),
        {},
    )
    checks.append((
        "the declared file-only .gate contract is complete (not a parser-loss Unknown)",
        gate_node.get("analysis_status") is None,
    ))
    db("veripsa_app", "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
       (json.dumps(graph), REPO, "main", "a" * 40))

    def claim(cid_path, author="dev"):
        # cid_path = '<change_id>:<path>' (the gate derives change_id by splitting on the first ':')
        path = cid_path.split(":", 1)[1]
        db("veripsa_app", "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
           (cid_path, path, REPO, "main", author))

    # BUG repro: claim the doc directly alongside code (the pre-fix server behavior) → 'unknown'.
    claim("PR-BUG:code_a.py", "buggy")
    claim("PR-BUG:README.md", "buggy")
    # FIX: claim only the filtered survivors of {code_b.py, LICENSE} → 'clear' (code_b is a clean in-graph solo).
    for p in S._code_paths(["code_b.py", "LICENSE"]):
        claim("PR-FIX:" + p, "fixed")
    # Gate files are hand-authored release logic. They must be coordinated as bare file nodes, not treated like
    # prose and not left absent from the graph.
    for p in S._code_paths(["gates.d/159-billing_role.gate"]):
        claim("PR-GATE:" + p, "gatedev")
    # honest recall: a NEW un-indexed .py survives the filter and is still 'unknown'.
    for p in S._code_paths(["brand_new.py"]):
        claim("PR-NEW:" + p, "newdev")

    imp = db("veripsa_app", "SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
    if isinstance(imp, str):
        imp = json.loads(imp)
    v = {c["change_id"]: c.get("verdict") for c in imp.get("changes", [])}

    checks.append((f"BUG repro: doc claimed directly → 'unknown' (got '{v.get('PR-BUG')}')",
                   v.get("PR-BUG") == "unknown"))
    checks.append((f"FIX: doc filtered out → NOT 'unknown' (got '{v.get('PR-FIX')}')",
                   v.get("PR-FIX") not in (None, "unknown")))
    checks.append((f"GATE: .gate path is in graph → NOT 'unknown' (got '{v.get('PR-GATE')}')",
                   v.get("PR-GATE") not in (None, "unknown")))
    checks.append((f"honest recall preserved: new un-indexed .py still 'unknown' (got '{v.get('PR-NEW')}')",
                   v.get("PR-NEW") == "unknown"))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("NONCODE PATHS GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
