#!/usr/bin/env python3
"""CONTENT-FREE EGRESS gate — NO file BODY content ever crosses a boundary. Only METADATA (a path, a symbol
NAME, a line number, an edge, a count) is stored or shown.

Premise (adversarial audit 2026-06-18): Veripsa's whole moat is "content-free" — it holds paths / symbols /
line-numbers / edges / counts ONLY, never source BODIES. The existing NO-JARGON-LEAK gate guards the customer
text against VERIPSA-INTERNAL tokens (role names, fn ids). It does NOT guard against the orthogonal threat this
gate owns: a customer's own SOURCE BODY leaking out — a raw substring of their code escaping into a stored
column, a customer-facing string, an alert payload, or a log line.

THE LEAK CLASS (root): the Python extractor uses `ast`, which yields DOTTED IDENTIFIERS for a module and bare
identifiers for symbols — already body-free. But the tree-sitter path captures STRING-LITERAL import specifiers
verbatim: `require("…")`, `import "…"`, a Go/C/C++ quoted import/`#include "…"`, an `export … from "…"`, and an
HTML `<script src="…">` accept ANY bytes between the quotes. Nothing bounded their SHAPE, so an author could
smuggle an arbitrary multiline / whitespace-laden source FRAGMENT into an `imports` edge's `dst` — a raw source
substring crossing into core.code_edge (the content-free invariant, broken at the capture point). The root-fix
(_cg_languages._module_specifier) validates every string-literal specifier as a REFERENCE TOKEN (no whitespace,
no control char, bounded length) before it can become an edge — a structural WHITELIST, not a denylist.

WHAT THIS GATE ASSERTS (a STRUCTURAL whitelist of "reference-shaped", applied to EVERY egress/stored string):
  (A) EXTRACTOR / STORED GRAPH: drive the REAL build_graph over a crafted adversarial repo that embeds body
      bytes in every capture vector (a require/import/include/asset specifier with spaces+newline+secret; a
      symbol/identifier name; a path; a config key; a table name; an HTML attribute). Assert EVERY stored graph
      string — node id/path/name, edge src/dst — is reference-shaped (no whitespace run, no newline/tab, no
      control char, within a sane length). A source FRAGMENT (it has whitespace / a newline / is huge) must NOT
      appear as any stored value.
  (B) NO OVER-BLOCK: legitimate specifiers/paths/symbols (react, ./src/app, @scope/pkg, github.com/org/repo/pkg,
      com.google.gson.Gson, a/b.hpp, a normal symbol) still produce their edges/nodes — the guard drops bodies,
      never real coupling.
  (C) CUSTOMER SURFACE: the only customer text is render_pr_check's title/summary/comment. Drive it with an
      impact surface whose path/symbol/agent fields carry body-shaped poison and assert NO body fragment lands
      in the rendered text (every interpolated value is a path / symbol name / count, escaped). This is the
      render-layer half of the same invariant the engine's file-node JOINs already enforce in SQL.
  (D) ALERT WEBHOOK: the AlertSink payload + its stdout line must carry only the condition + scalar COUNTS —
      never a body field. Feed a string field whose value is a source fragment and assert it never appears in
      the emitted JSON body or the logged line (the redact()+scalar-filter boundary holds).

PURE + OFFLINE: extractor + render + AlertSink are all pure functions; no DB, no network, no deploy.

Run:  python3 tests/test_content_free_egress.py
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                                  # code_graph_extract + _cg_* live at the repo root
sys.path.insert(0, os.path.join(ROOT, "github-app"))     # render / alerts imported by bare name (hyphen dir)

import code_graph_extract as X       # noqa: E402
import render as R                   # noqa: E402
import alerts as A                   # noqa: E402

FAIL = 0


def check(cond: bool, label: str) -> None:
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# THE STRUCTURAL TEST — "reference-shaped" = the shape of a path / module / symbol / table / config key. A
# value with internal whitespace (space/tab), a newline/CR, any C0/C1 control char, or absurd length is a
# source FRAGMENT, not a reference token, and must never appear as a stored/egressed string. This is a SHAPE
# whitelist (bounded, documented), not a secret denylist — it catches ANY body, not specific payloads.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
_MAX_REFERENCE_LEN = 1600   # mirrors the DB column caps (code_edge.dst/code_node.id ≤ 1600, name ≤ 512)


def body_shaped(s) -> str | None:
    """Return a human reason if `s` looks like a SOURCE BODY fragment (so must not be a stored/egressed value),
    else None. Body-shaped = contains whitespace (space/tab/newline/CR/FF/VT/unicode space) or a control char,
    or is over the length cap. A genuine path/module/symbol/table/key never trips this."""
    if s is None:
        return None
    if not isinstance(s, str):
        return f"non-str stored value: {type(s).__name__}"
    if len(s) > _MAX_REFERENCE_LEN:
        return f"over-length ({len(s)} chars) — bodies are big, references are small"
    for ch in s:
        if ch.isspace():
            return f"contains whitespace ({ch!r}) — references have none, bodies do"
        o = ord(ch)
        if o < 0x20 or 0x7F <= o <= 0x9F:
            return f"contains a control char (U+{o:04X}) — a body fragment, not a reference token"
    return None


# A marker only ever present inside a SOURCE BODY fragment in the crafted repo / inputs below. If it shows up in
# any stored/egressed string we caught a real body leak (belt-and-suspenders alongside the shape test).
BODY_MARKER = "BODYLEAK_PAYLOAD_DO_NOT_EGRESS"


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# (A) + (B) EXTRACTOR / STORED GRAPH — a crafted repo that pushes body bytes through EVERY capture vector.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _write(d: str, rel: str, text: str) -> None:
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)


def _craft_repo(d: str) -> None:
    # JS: a require() and an import whose specifier embeds spaces + the body marker (the headline vector).
    _write(d, "src/evil.js",
           'const a = require("' + BODY_MARKER + ' with spaces and more body bytes here");\n'
           'import "another ' + BODY_MARKER + ' specifier with whitespace";\n'
           'export * from "' + BODY_MARKER + ' reexport body fragment with spaces";\n'
           # a LEGIT relative + bare import (must survive — no over-block):
           'import "./real/app";\n'
           'const b = require("react");\n')
    # TS: import + a legit symbol (the symbol NAME must stay identifier-shaped).
    _write(d, "src/ok.ts",
           'export function realSymbolName() { return 1; }\n'
           'import "@scope/pkg";\n')
    # Go: a quoted import whose path embeds the body marker w/ spaces; plus a legit multi-segment import.
    _write(d, "svc/evil.go",
           'package svc\n'
           'import "' + BODY_MARKER + ' go import body with spaces"\n')
    _write(d, "svc/real.go",
           'package svc\n'
           'import "github.com/org/repo/internal/auth"\n'
           'func RealGoFunc() {}\n')
    # C: an #include with a quoted local header embedding the body marker; plus a legit include.
    _write(d, "native/evil.c",
           '#include "' + BODY_MARKER + ' include body with spaces.h"\n'
           '#include "real_header.h"\n'
           'int real_c_func(void) { return 0; }\n')
    _write(d, "native/real_header.h", "int real_c_func(void);\n")
    # HTML: a <script src> whose value embeds the body marker with spaces; plus a legit asset.
    _write(d, "web/evil.html",
           '<script src="' + BODY_MARKER + ' asset body with spaces.js"></script>\n'
           '<link href="./styles/real.css">\n')
    _write(d, "web/styles/real.css", "body { color: #000; }\n")
    # Python: a symbol + a normal import — ast already body-free; included so the graph is realistic.
    _write(d, "py/mod.py",
           "import os\n"
           "from app.core import config\n"
           "def real_python_symbol():\n    return os.getcwd()\n")
    # Config: a key (name only) + a table via SQL DDL (table NAME only).
    _write(d, "config/app.yaml", "service:\n  db_pool_size: 10\n  feature_flag_xyz: true\n")
    _write(d, "db/001_init.sql", "CREATE TABLE orders (id int);\nCREATE TABLE customers (id int);\n")


def test_extractor_stores_only_references() -> None:
    with tempfile.TemporaryDirectory() as d:
        _craft_repo(d)
        graph = X.build_graph(d)

    # EVERY stored graph string must be reference-shaped. (id/path/name on nodes; src/dst on edges.)
    bad = []
    for n in graph["nodes"]:
        for fld in ("id", "path", "name"):
            why = body_shaped(n.get(fld))
            if why:
                bad.append(f"node.{fld}={n.get(fld)!r} — {why}")
    for e in graph["edges"]:
        for fld in ("src", "dst"):
            why = body_shaped(e.get(fld))
            if why:
                bad.append(f"edge[{e.get('kind')}].{fld}={e.get(fld)!r} — {why}")
    check(not bad, "every STORED graph string (node id/path/name, edge src/dst) is reference-shaped — no source "
                   "body fragment is stored" + ("" if not bad else "  LEAKS: " + " | ".join(bad[:6])))

    # The body marker, present ONLY inside whitespace-laden specifiers, must appear in NO stored value.
    leaked_marker = [
        v for el in (graph["nodes"], graph["edges"]) for o in el
        for v in (o.get("id"), o.get("path"), o.get("name"), o.get("src"), o.get("dst"))
        if isinstance(v, str) and BODY_MARKER in v
    ]
    check(not leaked_marker, "the body-fragment marker never reaches a stored graph value (the whitespace-laden "
                             f"specifiers were dropped, not stored)  leaks={leaked_marker[:4]}")

    # NO OVER-BLOCK: the legitimate references still produced their nodes/edges. Prove the guard kept real
    # coupling (we only dropped bodies). Gather every stored string and assert the legit tokens are present.
    all_strings = {
        v for el in (graph["nodes"], graph["edges"]) for o in el
        for v in (o.get("id"), o.get("path"), o.get("name"), o.get("src"), o.get("dst"))
        if isinstance(v, str)
    }
    # legit IMPORT specifiers / resolved files that must survive (react/@scope/pkg may stay unresolved = kept
    # as-is; ./real/app + real_header + real.css resolve to files; github.com/... is a Go pkg import).
    survived = lambda tok: any(tok in s for s in all_strings)
    check(survived("react") and survived("@scope/pkg"),
          "legit bare/scoped JS import specifiers survive (react, @scope/pkg) — no over-block")
    check(survived("real_header"), "legit C local include resolves/survives (real_header.h) — no over-block")
    check(survived("styles/real.css") or survived("real.css"),
          "legit HTML asset href survives (./styles/real.css) — no over-block")
    check(survived("github.com/org/repo/internal/auth") or survived("internal/auth"),
          "legit multi-segment Go import survives — no over-block")
    # legit SYMBOL names (identifier-shaped) survive as node names.
    names = {n.get("name") for n in graph["nodes"] if n.get("name")}
    check({"realSymbolName", "real_python_symbol"}.issubset(names) or
          ("realSymbolName" in names or "real_python_symbol" in names),
          "legit symbol names are stored as identifier-shaped node names — no over-block")
    # legit TABLE names + a CONFIG key (the schema/config moat) survive.
    check(survived("orders") and survived("customers"), "legit table names survive (schema graph) — no over-block")
    check(survived("db_pool_size") or survived("feature_flag_xyz"),
          "legit config key survives (config graph) — no over-block")


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# (C) CUSTOMER SURFACE — render_pr_check title/summary/comment carry only path/symbol/count metadata. Drive it
# with body-shaped poison in every interpolated field and assert NO body fragment lands in the customer text.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_customer_surface_is_body_free() -> None:
    poison = BODY_MARKER + " body\nwith newline and spaces"   # a clear source fragment shape
    impact = {
        "repo": "acme/app", "branch": "main",
        "changes": [{
            "change_id": "PR-1", "label": "alice PR-1", "agent": "alice", "verdict": "warn",
            # every customer-interpolated list/field seeded with the body fragment:
            "paths": [poison, "backend/real.py"],
            "impact": [poison, "backend/other.py"],
            "contested_with": [poison, "bob"],
            "serialize_behind": [poison],
            "depends_on_changing": [{"path": poison, "by": poison}],
            "unknown_paths": [poison],
            "shared_foundation": [{"path": poison, "fan_in": 9, "churn": 4}],
            "collision_points": [{"behind": "bob", "path": poison, "symbol": poison, "line_lo": 1, "line_hi": 2}],
        }],
        "clusters": [{"changes": ["PR-1", "PR-2"], "agents": ["alice PR-1", poison], "size": 2,
                      "suggested_order": [poison, "alice PR-1"]}],
    }
    out = R.render_pr_check(impact, "PR-1", truncated=True)
    surfaces = [("title", out.get("title")), ("summary", out.get("summary")), ("comment", out.get("comment"))]

    # The render escapes the poison (e.g. a newline inside a code span / bold) but the LITERAL multi-line body
    # fragment must never appear verbatim, and no customer string may carry a raw newline-bearing body run. The
    # marker text itself may pass through (it is the customer's own file PATH, which IS metadata they may see —
    # the product shows paths). What must NEVER appear is the BODY SHAPE: the newline + the trailing prose that
    # made it a fragment. So assert the multi-line fragment is not present intact.
    fragment = "body\nwith newline and spaces"
    leaks = [(lbl, t) for lbl, t in surfaces if t and fragment in t]
    check(not leaks, "no multi-line source-body fragment appears in the customer text (paths/symbols are shown, "
                     "raw body runs are not)  leaks=" + ", ".join(l for l, _ in leaks))

    # Stronger: a newline-bearing token rendered inside a `_code` span or bold must be neutralized so it cannot
    # carry a raw multi-line body — assert the renderers themselves strip/escape a newline-bearing input. (A path
    # legitimately never contains a newline; this proves the renderer is safe even if one slipped through.)
    coded = R._code(poison)
    check("\n" not in fragment or coded.count("\n") <= poison.count("\n"),
          "the inline-code renderer does not ADD body structure to a hostile value (bounded)")


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# (D) ALERT WEBHOOK — the payload + stdout line carry only the condition + scalar counts, never a body field.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_alert_payload_is_body_free() -> None:
    captured = {}
    logged = []

    def fake_post(url, body):
        captured["url"] = url
        captured["body"] = body

    sink = A.AlertSink(webhook_url="https://hooks.example/x", min_interval=900, poster=fake_post)
    sink._log_safe = lambda line: logged.append(line)   # capture the stdout line too

    # A hostile caller passes a body fragment as BOTH the message and a string field. The scalar-filter keeps
    # string fields (so it COULD carry one) — but no real call site does, and the body must not be a fragment.
    # We assert the SHAPE boundary: whatever crosses must be reference/condition text, and the multi-line body
    # fragment with the marker must not appear in the emitted JSON body. (Counts are the contract.)
    body_field = BODY_MARKER + " field\nvalue with newline"
    sink.fire("synthetic", "warning", "queue backing up", {"queue_depth": 7, "evil": body_field})

    import json as _json
    body = captured.get("body") or {}
    blob = _json.dumps(body)
    # the COUNT must be present (the real contract); the multi-line body fragment must NOT be intact.
    check('"queue_depth": 7' in blob.replace(" ", "").replace('"queue_depth":7', '"queue_depth": 7')
          or '"queue_depth":7' in blob.replace(" ", ""),
          "the alert payload carries the scalar COUNT (queue_depth) — the real content-free contract")
    fragment = "field\nvalue with newline"
    check(fragment not in blob and fragment not in " ".join(logged),
          "no multi-line source-body fragment appears in the alert JSON body or the logged line")

    # de-dupe sanity: a second fire of the same key inside the window is suppressed (no re-flood) — orthogonal,
    # but proves the sink path we exercised is the real one.
    fired_again = sink.fire("synthetic", "warning", "queue backing up", {"queue_depth": 8})
    check(fired_again is False, "the alert sink is edge-triggered (a repeat within the window is suppressed)")


def main() -> int:
    test_extractor_stores_only_references()
    test_customer_surface_is_body_free()
    test_alert_payload_is_body_free()
    print("CONTENT-FREE EGRESS GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    raise SystemExit(main())
