#!/usr/bin/env python3
"""GO SAME-PACKAGE const/var symbol-use recall — MEASURED BOUNDARY + precision LOCK (this lane).

DOGFOOD AUDIT (audit/extractor-recall-3, measured with the product's own tools). The dominant REAL
coupling the live dampened adjacency MISSES on Go repos is SAME-PACKAGE symbol use through package-level
const/var. In Go, every `.go` file in a directory shares ONE package and sees the others' top-level
symbols WITHOUT any import — so a file that reads a sibling's exported `var DefaultWriter` / `const
DebugMode` has NO import edge AND no `calls` edge (a bare value reference is not a `call_expression`).
The static graph is structurally blind to it.

MEASURED (recall_measure.py co-change ground truth, lift>=2 & support>=3, cutoff 8 — the SHIPPING
dampened adjacency):
    repo      files  commits   co-change GT  LIVE recall  dominant live-missed class
    logrus     48     496          56          44.6%       20 no-edge SAME-DIR (= same Go package)
    cobra      36     189          37          45.9%       16 no-edge SAME-DIR (= same Go package)
    gin        98     111          10          90.0%        (near-ceiling; small GT)
dampening cost on these = 0.0 pts: this is PURE extractor blindness, not a dampening tradeoff.

WHY IT IS NOT SAFELY CLOSABLE (the honest NO — a MEASURED upstream-blocked boundary, not a guess).
The only way to recover these pairs is to emit a coupling edge for a bare IDENTIFIER reference to a
package-level const/var. Measured on gin+logrus+cobra, the naive "single-definer + stoplist guard"
identifier-reference approach (exactly the guard every `calls` edge already passes) recovers at most
+3 GT pairs on ONE repo (logrus) and +0 on the other two — while injecting net-new pairs of which a
MEASURED 28% are linked ONLY by a lowercase short name (`param`, `root`, `engine`, `reset`) that is a
same-named function-LOCAL variable in an unrelated file, NOT a real package-var reference. A static
identifier walk cannot tell a package-var read from a same-named local without SCOPE RESOLUTION
(tree-sitter alone does not give reliable binding) — so the recoverable signal cannot be separated
from a ~28% false-coupling rate. For a precision-first product whose quality IS precise silence, a
28% false-edge rate on the new edges is the wallpaper that gets it muted: NET NEGATIVE. So we do NOT
ship the naive edge. (Contrast: `sym_use:ctor`/`base` are safe because a base/ctor is a structurally
DISTINCT position naming a TYPE, not an arbitrary value identifier — see PR #253 / py_base_class gate.)

WHAT THIS GATE LOCKS (so the boundary cannot silently move):
  GAP)  the gap is real and reproducible — a deterministic Go fixture where two sibling same-package
        files are coupled ONLY through a package-level const + var and the shipping extractor produces
        ZERO edge between them. (If a future change starts covering it, this assertion flips and the
        author must re-justify the precision.)
  HAZARD) the precise reason it is unsafe — an UNRELATED file with a same-named LOCAL variable must NOT
        become coupled to the const/var's home file. We assert the extractor does NOT naively fan a
        bare const/var identifier into a coupling edge (a regression that adds it is caught here).
  CONTROL) the gap is SAME-PACKAGE-specific: when the SAME coupling is expressed the normal way (one
        file IMPORTS another), the extractor DOES couple them — proving the miss is the const/var
        symbol-use blind spot, not a broken extractor.

Content-free throughout (only paths / symbol NAMES / counts). Pure extractor — no DB, parallel-safe.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402


def _graph(files):
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        return X.build_graph(d)


def _edge(g, a, b):
    """Any directed edge a->b or b->a, returned as (src, kind, dst) tuples (content-free)."""
    return [(e["src"], e["kind"], e["dst"]) for e in g["edges"]
            if (e["src"] == a and e["dst"] == b) or (e["src"] == b and e["dst"] == a)]


def main() -> int:
    checks = []

    # =================================================================================================
    # GAP) The real miss: two sibling same-package files coupled ONLY through a package-level const+var.
    #   mode.go defines `var DefaultWriter` + `const DebugMode`; user.go reads BOTH (no import needed in
    #   Go — same package). This is genuine cross-file coupling: edit DefaultWriter's meaning and user.go
    #   must coordinate. The SHIPPING extractor produces ZERO edge between them = a SILENT MISS.
    # =================================================================================================
    g = _graph({
        "pkg/mode.go":
            "package pkg\n\nvar DefaultWriter = openSink()\n\nconst DebugMode = \"debug\"\n\n"
            "func openSink() int { return 1 }\n",
        "pkg/user.go":
            "package pkg\n\nfunc Use() string {\n\tif DefaultWriter == 1 {\n\t\treturn DebugMode\n\t}\n"
            "\treturn \"\"\n}\n",
    })
    gap_edges = _edge(g, "pkg/mode.go", "pkg/user.go")
    checks.append((
        "GAP: a sibling same-package file reading a package-level const+var has NO edge to its home file "
        f"— the DOMINANT measured Go co-change miss (logrus 20 / cobra 16 same-pkg pairs). edges={gap_edges}",
        gap_edges == []))
    # and the const/var themselves mint no def/class node today (so even an existing call edge to the NAME
    # would have nothing to resolve to — this documents WHERE the chain breaks).
    cv_def_nodes = [n.get("name") for n in g["nodes"]
                    if n.get("kind") in ("def", "class") and n.get("name") in ("DefaultWriter", "DebugMode")]
    checks.append((
        "GAP: package-level const/var mint NO def/class node in the current extractor "
        f"(found={cv_def_nodes}) — the symbol the sibling references has no resolvable home",
        cv_def_nodes == []))

    # =================================================================================================
    # HAZARD) Why a naive identifier-reference fix is unsafe: an UNRELATED file in a DIFFERENT package
    #   declares a function-LOCAL variable with the SAME name `DefaultWriter`. It has nothing to do with
    #   pkg/mode.go. A naive "emit an edge for any identifier matching a known const/var name" would
    #   FALSELY couple it to pkg/mode.go (a same-name local shadow — MEASURED 28% of net-new pairs across
    #   gin+logrus+cobra). We LOCK that the extractor does NOT do this: no edge from the unrelated file.
    # =================================================================================================
    g_haz = _graph({
        "pkg/mode.go": "package pkg\n\nvar DefaultWriter = 1\n",
        "other/unrelated.go":
            "package other\n\nfunc F() int {\n\tDefaultWriter := 99\n\treturn DefaultWriter\n}\n",
    })
    haz_edges = _edge(g_haz, "pkg/mode.go", "other/unrelated.go")
    checks.append((
        "HAZARD-LOCK: an UNRELATED file with a same-named LOCAL variable is NOT coupled to the const/var's "
        f"home file — the extractor does not naively fan a bare const/var identifier into an edge. edges={haz_edges}",
        haz_edges == []))

    # =================================================================================================
    # CONTROL) The miss is SAME-PACKAGE-SPECIFIC, not a broken extractor. Express the SAME dependency the
    #   normal cross-package way — user2 in package `app` IMPORTS pkg — and the extractor DOES couple them.
    #   This proves the blind spot is precisely Go's import-free same-package symbol sharing.
    # =================================================================================================
    g_ctrl = _graph({
        "pkg/mode.go":
            "package pkg\n\nvar DefaultWriter = 1\n",
        "app/user2.go":
            "package app\n\nimport \"example.com/m/pkg\"\n\nfunc Use() int {\n\treturn pkg.DefaultWriter\n}\n",
    })
    ctrl_imports = [(e["src"], e["dst"]) for e in g_ctrl["edges"]
                    if e["kind"] == "imports" and e["src"] == "app/user2.go"]
    coupled = any(d == "pkg/mode.go" or d.endswith("/pkg") or d == "pkg"
                  or "pkg" in d.split("/") for _s, d in ctrl_imports)
    checks.append((
        "CONTROL: the SAME coupling expressed via a normal cross-package IMPORT IS captured — the miss is "
        f"Go same-package symbol sharing specifically, not a dead extractor. import edges={ctrl_imports}",
        coupled))

    # =================================================================================================
    # CONTENT-FREE) every edge dst the extractor emits for these fixtures is a bare path or symbol NAME,
    # never a file body — the standard egress invariant, asserted here too.
    # =================================================================================================
    all_dsts = {e["dst"] for e in g["edges"]} | {e["dst"] for e in g_haz["edges"]} | {e["dst"] for e in g_ctrl["edges"]}
    checks.append((
        "CONTENT-FREE: every emitted edge dst is a path or bare symbol name, never a file body (no newline)",
        all("\n" not in str(d) for d in all_dsts)))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("GO SAME-PACKAGE CONSTVAR RECALL GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
