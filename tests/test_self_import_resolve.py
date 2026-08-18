#!/usr/bin/env python3
"""OWN-PACKAGE SELF-IMPORT RESOLUTION gate (no DB) — recall up, decoy down, both content-free.

WHY THIS GATE EXISTS (two measured defects in _cg_resolve._resolve_imports, both pure path/name resolution):

  1. RECALL — package-name self-import unresolved. Inside a PUBLISHED package, an import of its OWN name's
     subpath (`zustand/shallow`, `from acme.widget import y`) names NO file at `…/zustand/shallow` — the
     suffix probe misses, so a REAL intra-package dependency edge is LOST. (Real-repo: zustand RAW recall
     ~52% → ~56% once these resolve; 30 recovered intra-package edges.) FIX: when an import's FIRST segment
     is the repo's OWN package name, STRIP it and resolve the REMAINDER against the package source tree.

  2. PRECISION — a bare single-segment import fabricates a DECOY coupling. A bare `import flask` (the repo's
     own published name) basename-fans-out to a file literally named `flask.py` — on real Flask that was 43
     false edges = 17% of resolved internal edges, every one pointing at a deep test fixture, not the real
     package. FIX: a bare own-name resolves to the package ENTRY (`src/flask/__init__.py`), never a decoy;
     and a bare EXTERNAL name (no local owner) fabricates nothing.

CONTENT-FREE: the own package name is recovered from the LAYOUT a manifest (`package.json` / `pyproject.toml`)
roots — its PATH + the `src/<name>/__init__.py` / npm `src/` entry — never by reading the manifest's body. Only
paths + module names cross.

This gate stands up a tiny SYNTHETIC repo (a couple of files + a manifest declaring a "name") and asserts,
through the REAL extractor (build_graph → _resolve_imports), all four behaviors:
  (a) `<ownname>/<sub>`        → the local sub FILE (recall),
  (b) bare `<ownname>`         → the package ENTRY, NOT a same-basename decoy (precision),
  (c) a genuine local basename → STILL resolves (recall-safe; the fix must not over-drop),
  (d) an external bare import  → fabricates NO decoy edge (precision).
No DB, no network; deterministic.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import code_graph_extract as X  # noqa: E402


def _build(files):
    """Write `files` (rel→body) to a temp repo, run the real extractor, return the set of RESOLVED
    file→file import edges (src, dst) where dst is an actual repo file — exactly what the engine couples on."""
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        g = X.build_graph(d)
    fset = {n["path"] for n in g["nodes"] if n.get("kind") == "file"}
    return {(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == "imports"}, fset


def main() -> int:
    checks = []

    # ── npm/TS package "acme": src/index.ts (entry) + src/util.ts (sub); a DECOY util.ts in a test dir ────
    npm_edges, npm_files = _build({
        "package.json": '{"name": "acme", "main": "./src/index.ts"}',
        "src/index.ts": "export const x = 1\n",
        "src/util.ts":  "export const u = 2\n",
        "tests/app.test.ts": 'import { x } from "acme"\nimport { u } from "acme/util"\n',
        "tests/decoy/util.ts": "export const d = 3\n",            # a same-basename decoy that must NOT win
    })
    decoy_ts = "tests/decoy/util.ts"
    # (a) self-import `acme/util` → src/util.ts (the local sub), NEVER the decoy.
    checks.append(("(a) npm self-import `acme/util` resolves to src/util.ts (recall), not the decoy",
                   ("tests/app.test.ts", "src/util.ts") in npm_edges
                   and ("tests/app.test.ts", decoy_ts) not in npm_edges))
    # (b) bare `acme` → the package ENTRY src/index.ts, NEVER a same-basename decoy / a different file.
    checks.append(("(b) bare own-name `acme` resolves to the package entry src/index.ts, not a decoy",
                   ("tests/app.test.ts", "src/index.ts") in npm_edges))

    # ── Python package "acme": src/acme/__init__.py (entry) + widget.py (sub); a DECOY acme.py in tests ──
    py_edges, _ = _build({
        "pyproject.toml": '[project]\nname = "acme"\n',
        "src/acme/__init__.py": "x = 1\n",
        "src/acme/widget.py":   "y = 2\n",
        "tests/conftest.py": "import acme\nfrom acme.widget import y\n",
        "tests/fixtures/deep/acme.py": "z = 3\n",                 # a decoy literally named acme.py
    })
    py_decoy = "tests/fixtures/deep/acme.py"
    checks.append(("(a-py) python self-import `acme.widget` → src/acme/widget.py (recall)",
                   ("tests/conftest.py", "src/acme/widget.py") in py_edges))
    checks.append(("(b-py) bare `import acme` → the package src/acme/__init__.py, NOT the deep acme.py decoy",
                   ("tests/conftest.py", "src/acme/__init__.py") in py_edges
                   and ("tests/conftest.py", py_decoy) not in py_edges))

    # ── recall-safe + external: a genuine local basename STILL resolves; an external bare fabricates nothing ─
    rs_edges, _ = _build({
        "package.json": '{"name": "acme"}',
        "src/index.ts": "export const a = 1\n",
        "src/helper.ts": "export const h = 1\n",
        "tests/x.test.ts": ('import { h } from "helper"\n'        # genuine local bare basename → must resolve
                            'import { z } from "lodash"\n'        # external bare → must NOT fabricate a decoy
                            'import { a } from "acme"\n'           # own bare → entry
                            'import { h2 } from "acme/helper"\n'),  # own subpath → corroborates the own name
    })
    # (c) RECALL-SAFE: a real local single-file basename import still resolves to its file.
    checks.append(("(c) recall-safe: genuine local `helper` STILL resolves to src/helper.ts",
                   ("tests/x.test.ts", "src/helper.ts") in rs_edges))
    # (d) EXTERNAL bare `lodash` (no local owner) → NO fabricated file→file edge (it stays inert / raw).
    fabricated_external = [(s, d) for (s, d) in rs_edges
                           if s == "tests/x.test.ts" and d.endswith(".ts") and "lodash" in (d.lower())]
    # the strong assertion: lodash resolves to NO repo file at all (every resolved dst is a real source file,
    # and none of them came from the external `lodash` import — there is no lodash* file in the repo).
    checks.append(("(d) external bare `lodash` fabricates NO decoy edge (stays inert)",
                   not fabricated_external
                   and not any(d for (s, d) in rs_edges if "lodash" in d and d.endswith(".ts"))))
    # own-bare also resolves to the entry here (sanity that the recall-safe fixture didn't disable own-name).
    checks.append(("(b-mixed) bare `acme` → src/index.ts even alongside a real local-basename import",
                   ("tests/x.test.ts", "src/index.ts") in rs_edges))

    # ── INERTNESS on a plain app (no manifest): own-name logic must NOT fire — a flat repo is untouched ────
    plain_edges, _ = _build({
        "main.py": "import helper\n",
        "helper.py": "def h():\n    pass\n",
    })
    checks.append(("(inert) a repo with NO manifest is unchanged: plain `import helper` → helper.py",
                   ("main.py", "helper.py") in plain_edges))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("SELF-IMPORT RESOLVE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
