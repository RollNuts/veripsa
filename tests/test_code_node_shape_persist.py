#!/usr/bin/env python3
"""CODE-NODE SHAPE PERSIST gate (compatibility lane PR-1 — the dropped-fields fix).

#831 taught the Python extractor to attach a content-free signature shape to every `def` node
(param NAMES, required/optional arity COUNTS, varargs/kwargs FLAGS, kwonly NAMES, a hash
FINGERPRINT). But the governed ingest INSERT into core.code_node enumerated only
id/kind/path/name/language/span/hash — so the shape fields were silently DROPPED at the store and
the compatibility rules would have had no main-side input. This gate proves the fix, end to end on
a real database through the REAL SECURITY DEFINER write path, for BOTH write paths:

  FULL)  core.ingest_graph_with_authority persists the shape on the def row, byte-equal to what the
         real extractor emitted (arity counts, flags, ordered name arrays, fingerprint).
  PATCH) core.patch_graph_with_authority (the STEADY-STATE incremental path a normal push takes)
         persists it too — the audit-r2 lesson: a shape persisted only by the full re-ingest would
         be silently WIPED for every file touched by a normal push.
  NULLS) a file node and a class node store all seven shape columns NULL (non-def rows unchanged).
  FREE)  content-free AT THE STORE: no default-value token, no annotation token, no body token, no
         default literal appears ANYWHERE in ANY stored code_node row (whole-row text, superuser
         read so RLS cannot hide a row).
  MAL)   a hand-crafted graph with MALFORMED shape fields (wrong types, out-of-bounds counts, an
         oversized array, a junk fingerprint) ingests WITHOUT ERROR and stores NULL for every bad
         field — degrade-to-NULL, never an aborted shared transaction, and the sanitizer bound
         provably matches the schema CHECK bound (512 stored, 513 → NULL, not a violation).

PROCESS-UNIQUE scratch DB (parallel-safe), exactly like the other gates.

Run:  python3 tests/test_code_node_shape_persist.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
import code_graph_extract as X  # noqa: E402

DB = "veripsa_shapepersist_" + str(os.getpid())
REPO = "sp/shape"

# Sentinels with the SHAPE of the three things that must NEVER reach storage: a default-value
# expression, an annotation source token, a body token. All deliberately UNDEFINED names (the file
# still parses; it is never executed) so no legitimate class/def node carries these strings.
DFLT_TOKEN = "DFLT_SENTINEL_9Q7"
ANNO_TOKEN = "AnnoSentinel4K2"
BODY_TOKEN = "BODY_SENTINEL_8Z1"  # gitleaks:allow -- deliberate invalid content-free sentinel
DFLT_LITERAL = "73737"   # a distinctive numeric default — must not land in any row either

SRC = (
    "class Widget:\n"
    "    def refresh(self, force=False):\n"
    "        return None\n"
    "\n"
    f"def handler(order_id, mode={DFLT_TOKEN}, *extras, timeout: {ANNO_TOKEN} = {DFLT_LITERAL}, tag, **opts):\n"
    f"    return {BODY_TOKEN}\n"
    "\n"
    "def ping():\n"
    "    return None\n"
)

SHAPE_COLS = ("required_arity", "optional_arity", "has_varargs", "has_kwargs",
              "param_names", "kwonly_names", "shape_fingerprint")


def db_app(sql, args=()):
    """One statement as the REAL least-privilege veripsa_app role (the write path prod uses)."""
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def stored_rows(repo):
    """EVERY code_node row for a repo — shape columns + node identity + the WHOLE ROW as text (the
    content-free audit surface). Superuser read so RLS cannot hide what physically landed."""
    conn = psycopg2.connect(f"postgresql:///{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT node_kind, name, required_arity, optional_arity, has_varargs, has_kwargs, "
                "       param_names, kwonly_names, shape_fingerprint, t::text "
                "FROM core.code_node t WHERE repo=%s AND branch='main'", (repo,))
            return cur.fetchall()
    finally:
        conn.close()


def _graph(files):
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        return X.build_graph(d)


def _row_by_name(rows, kind, name):
    for r in rows:
        if r[0] == kind and r[1] == name:
            return r
    return None


def main() -> int:
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    # ── FULL) the real extractor → real ingest → shape persists byte-equal on the def row ─────────
    g = _graph({"app.py": SRC})
    handler_node = next(n for n in g["nodes"] if n.get("kind") == "def" and n.get("name") == "handler")
    res = json.loads(db_app("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                            (json.dumps(g), REPO, "main", "a" * 40)))
    ck("FULL: ingest succeeded", res.get("ok") is True and res.get("nodes", 0) >= 4)

    rows = stored_rows(REPO)
    h = _row_by_name(rows, "def", "handler")
    ck("FULL: def row present", h is not None)
    if h is not None:
        ck("FULL: required/optional arity persisted (2 required, 2 optional)",
           h[2] == handler_node["required_arity"] == 2 and h[3] == handler_node["optional_arity"] == 2)
        ck("FULL: varargs/kwargs flags persisted (both True)",
           h[4] is True and h[5] is True and handler_node["has_varargs"] and handler_node["has_kwargs"])
        ck("FULL: ordered param_names persisted exactly as extracted",
           h[6] == handler_node["param_names"] == ["order_id", "mode", "timeout", "tag"])
        ck("FULL: ordered kwonly_names persisted exactly as extracted",
           h[7] == handler_node["kwonly_names"] == ["timeout", "tag"])
        ck("FULL: fingerprint persisted verbatim, bounded lower-hex",
           h[8] == handler_node["shape_fingerprint"] and re.fullmatch(r"[0-9a-f]{12}", h[8] or "") is not None)
    p = _row_by_name(rows, "def", "ping")
    ck("FULL: a ZERO-parameter def stores KNOWN-EMPTY arrays ('{}'), not NULL (= unknown)",
       p is not None and p[6] == [] and p[7] == [] and p[2] == 0 and p[3] == 0)

    # ── NULLS) non-def rows carry NO shape (file + class unchanged by this lane) ──────────────────
    f = _row_by_name(rows, "file", None) or next((r for r in rows if r[0] == "file"), None)
    c = _row_by_name(rows, "class", "Widget")
    ck("NULLS: the FILE node stores all seven shape columns NULL",
       f is not None and all(v is None for v in f[2:9]))
    ck("NULLS: the CLASS node stores all seven shape columns NULL",
       c is not None and all(v is None for v in c[2:9]))

    # ── FREE) content-free at the STORE: no default/annotation/body token in ANY row's text ───────
    leaks = []
    for row in rows:
        whole = row[9] or ""
        for token in (DFLT_TOKEN, ANNO_TOKEN, BODY_TOKEN, DFLT_LITERAL):
            if token in whole:
                leaks.append((row[0], row[1], token))
    ck("FREE: no default-value token, no annotation token, no body token, no default literal "
       "appears ANYWHERE in ANY stored code_node row (whole-row text, RLS-bypassing read)",
       not leaks and len(rows) >= 5)
    if leaks:
        print("  leaked:", leaks[:5])

    # ── PATCH) the STEADY-STATE incremental path persists the shape too ───────────────────────────
    src2 = SRC.replace("def handler(order_id, mode=", "def handler(order_id, priority, mode=")
    g2 = _graph({"app.py": src2})
    handler2 = next(n for n in g2["nodes"] if n.get("kind") == "def" and n.get("name") == "handler")
    db_app("SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
           (json.dumps({
                "extractor_version": g2["extractor_version"],
                "metrics": g2["metrics"],
                "expected_base_sha": "a" * 40,
                "expected_base_revision": res["graph_revision"],
                "nodes": g2["nodes"],
                "edges": g2["edges"],
            }), REPO, "main",
            ["app.py"], [], "b" * 40))
    h2 = _row_by_name(stored_rows(REPO), "def", "handler")
    ck("PATCH: a normal (incremental) push persists the NEW shape — required_arity 2→3, "
       "fingerprint CHANGED and matches the re-extraction (the audit-r2 wipe-on-push class is closed)",
       h2 is not None and h2[2] == handler2["required_arity"] == 3
       and h2[8] == handler2["shape_fingerprint"] and h2[8] != (h[8] if h else None)
       and h2[6] == handler2["param_names"])

    # ── MAL) malformed shape fields degrade to NULL — never an error, never a constraint abort ────
    repo_mal = "sp/malformed"
    poison = {"nodes": [
        {"id": "m.py", "kind": "file", "path": "m.py"},
        {"id": "m.py::bad", "kind": "def", "path": "m.py", "name": "bad",
         "required_arity": "nope", "optional_arity": 99999, "has_varargs": "yes",
         "has_kwargs": 1, "param_names": "not-an-array", "kwonly_names": ["x"] * 200,
         "shape_fingerprint": "ZZ-not-hex\nline2"},
        {"id": "m.py::edge512", "kind": "def", "path": "m.py", "name": "edge512",
         "required_arity": 512, "optional_arity": 513},
    ], "edges": []}
    res_mal = json.loads(db_app("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                                (json.dumps(poison), repo_mal, "main", "c" * 40)))
    mal_rows = stored_rows(repo_mal)
    bad = _row_by_name(mal_rows, "def", "bad")
    edge = _row_by_name(mal_rows, "def", "edge512")
    ck("MAL: a graph carrying junk-typed / out-of-bounds / oversized shape fields ingests "
       "WITHOUT ERROR and every bad field stores NULL (degrade, never abort)",
       res_mal.get("ok") is True and bad is not None and all(v is None for v in bad[2:9]))
    ck("MAL: sanitizer bound == schema CHECK bound (512 stored; 513 degrades to NULL, no violation)",
       edge is not None and edge[2] == 512 and edge[3] is None)

    # ── report ────────────────────────────────────────────────────────────────────────────────────
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("CODE-NODE SHAPE PERSIST GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
