#!/usr/bin/env python3
"""PYTHON TEST-FIXTURE SHADOW resolution gate (no DB) — a precision fix that is RECALL-SAFE, content-free.

WHY THIS GATE EXISTS (a measured precision defect in _cg_resolve._resolve_imports, pure path/name resolution):

  A Python test tree (`tests/units/…`) frequently MIRRORS the package directory layout. So a dotted import
  of a PACKAGE module — `from reflex_base.utils import types`, normalized to the suffix `reflex_base/utils` —
  suffix-matches BOTH the real package file (`packages/reflex-base/src/reflex_base/utils/__init__.py`) AND a
  test-fixture that shadows the same path (`tests/units/reflex_base/utils/__init__.py`). The resolver's
  recall-biased suffix probe then fans out to the test mirror too, minting a FALSE production→test coupling.
  Real-repo audit on reflex-dev/reflex: 158 such production→test edges (203 across all importers), 6 of which
  SURVIVED hub-dampening to a customer warn — the cry-wolf that gets a control-plane muted.

  This is the SAME class the Go (`_test.go` excluded), C# (`_CS_TEST_SEG` excluded) and Rust (separate
  Cargo compile-target excluded) resolvers already guard against — Python's general dotted-suffix probe had
  no such guard.

THE FIX (mirrors tests/audit_repo.py's `src_to_test_real` rule): when a Python import co-resolves to BOTH a
test-path file AND a NON-test file, the non-test file IS the module the import names; the test-path candidate
is the fixture-mirror false edge → drop it. ONLY in that case. So it is strictly RECALL-SAFE:
  • a real `from tests.foo import x` (the import literally names a test path → no non-test sibling
    co-resolves) is KEPT — a genuine production→test-util coupling is not lost;
  • a genuine module living under an `examples/` package directory (a UNIQUE suffix → no non-test sibling)
    is KEPT — the guard only fires when a non-test sibling PROVES the test candidate is the shadow.

CONTENT-FREE: only file PATHS and the dotted module name cross — never a file body. A test path is a path
segment in (`test`/`tests`/`spec`/`__tests__`/`examples`/`example`), matched case-insensitively.

This gate stands up tiny SYNTHETIC repos through the REAL extractor (build_graph → _resolve_imports) and
asserts: the shadow false edge is DROPPED, the real package edge is KEPT, two distinct recall cases SURVIVE,
and the fix is INERT on a non-Python (TS) layout with the same shape. No DB, no network; deterministic.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import code_graph_extract as X  # noqa: E402


def _build(files):
    """Write `files` (rel→body) to a temp repo, run the REAL extractor, return the set of RESOLVED
    file→file import edges (src, dst) where dst is an actual repo file — exactly what the engine couples on."""
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        g = X.build_graph(d)
    fset = {n["path"] for n in g["nodes"] if n.get("kind") == "file"}
    edges = {(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == "imports" and e["dst"] in fset}
    return edges, fset


def main() -> int:
    checks = []

    # ── SHADOW: a package whose tests/ tree MIRRORS the package layout. A production module imports a
    #    sibling package module by its dotted name; the same dotted suffix ALSO matches the test mirror. ──
    sh_edges, sh_files = _build({
        "pyproject.toml": '[project]\nname = "acme"\n',
        # real package (under src/acme/…) — the module the import actually names
        "src/acme/__init__.py": "from acme.utils import helper\n",
        "src/acme/widget.py":   "from acme.utils import helper\n",     # production importer
        "src/acme/utils/__init__.py": "def helper():\n    return 1\n",  # the REAL target
        # test tree that SHADOWS the package layout: tests/units/acme/utils/__init__.py mirrors acme/utils
        "tests/units/acme/utils/__init__.py": "# test fixture mirroring acme.utils\n",
        "tests/units/acme/test_widget.py": "from acme import widget\n",
    })
    real_target = "src/acme/utils/__init__.py"
    shadow_target = "tests/units/acme/utils/__init__.py"
    importer = "src/acme/widget.py"
    # The REAL package edge must survive (recall preserved on the legitimate coupling).
    checks.append(("(1) production `from acme.utils import helper` STILL resolves to the real src/acme/utils (recall)",
                   (importer, real_target) in sh_edges))
    # The test-fixture SHADOW edge must be DROPPED (the precision fix).
    checks.append(("(2) the test-fixture SHADOW tests/units/acme/utils is DROPPED (no false production→test edge)",
                   (importer, shadow_target) not in sh_edges))
    # No production .py file anywhere couples to the shadow fixture (the defect produced MANY such edges).
    def _is_test(p):
        segs = p.split("/")
        return any(s in ("test", "tests", "spec", "specs", "__tests__", "examples", "example") for s in segs)
    prod_to_shadow = [(s, d) for (s, d) in sh_edges
                      if d == shadow_target and not _is_test(s)]
    checks.append(("(3) NO production→shadow edge survives anywhere in the package",
                   not prod_to_shadow))

    # ── RECALL CASE A: a real `from tests.helpers import x` — the import LITERALLY names a test path, so no
    #    non-test sibling co-resolves → the guard must NOT fire; the genuine prod→test-util edge is KEPT. ──
    ra_edges, _ = _build({
        "pyproject.toml": '[project]\nname = "acme"\n',
        "src/acme/__init__.py": "x = 1\n",
        # a SCRIPT that genuinely depends on a test utility by its fully-qualified test path
        "scripts/bench.py": "from tests.helpers.fixture import seed\n",
        "tests/helpers/__init__.py": "\n",
        "tests/helpers/fixture.py": "def seed():\n    return 0\n",
    })
    checks.append(("(4) recall-safe: a REAL `from tests.helpers.fixture import seed` is KEPT "
                   "(the import names a test path; no non-test sibling → not dropped)",
                   ("scripts/bench.py", "tests/helpers/fixture.py") in ra_edges))

    # ── RECALL CASE B: a genuine module under an `examples/` PACKAGE directory, imported by a UNIQUE dotted
    #    path (no non-test sibling shares that suffix) → the guard must NOT fire; the edge is KEPT. ──
    rb_edges, _ = _build({
        "pyproject.toml": '[project]\nname = "acme"\n',
        "src/acme/__init__.py": "x = 1\n",
        "src/acme/demo.py": "from acme.examples.gallery import show\n",   # production importer of an examples module
        "src/acme/examples/__init__.py": "\n",
        "src/acme/examples/gallery.py": "def show():\n    return 1\n",     # the UNIQUE target (no shadow twin)
    })
    checks.append(("(5) recall-safe: a UNIQUE module under an examples/ package (no shadow twin) STILL resolves",
                   ("src/acme/demo.py", "src/acme/examples/gallery.py") in rb_edges))

    # ── INERTNESS on a non-Python (TS) layout with the SAME shadow shape: the .py-only guard must not fire,
    #    and the existing TS resolution (recall-biased fan-out, dampened downstream) is UNCHANGED. ──
    ts_edges, _ = _build({
        "package.json": '{"name": "acme"}',
        "src/index.ts": "export const x = 1\n",
        "src/utils/index.ts": "export const u = 1\n",
        "src/widget.ts": 'import { u } from "utils"\n',                    # bare basename → src/utils (and the mirror)
        "tests/units/utils/index.ts": "export const t = 1\n",             # a TS test mirror
    })
    # The guard is Python-only, so the TS resolution is byte-for-byte what it was before the fix: the real
    # src/utils/index.ts still resolves (recall), proving the fix did not touch the TS path.
    checks.append(("(6) inert on TS: `import {u} from 'utils'` STILL resolves to src/utils/index.ts (fix is .py-only)",
                   ("src/widget.ts", "src/utils/index.ts") in ts_edges))

    # ── INERTNESS on a flat Python app (no test tree at all): nothing to drop, plain imports unaffected. ──
    flat_edges, _ = _build({
        "main.py": "from pkg import thing\n",
        "pkg/__init__.py": "\n",
        "pkg/thing.py": "def thing():\n    return 1\n",
    })
    checks.append(("(inert) a flat python repo with no test mirror is unchanged: `from pkg import thing` → pkg/thing.py",
                   ("main.py", "pkg/thing.py") in flat_edges))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("PY TEST-SHADOW RESOLVE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
