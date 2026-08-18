#!/usr/bin/env python3
"""PHP use-function/use-const precision gate (no DB, no network).

WHY: PHP `use function App\Helpers\format;` and `use const App\X\MAX;` name a
FUNCTION or CONST SYMBOL — not a file path. The PHP tree-sitter grammar places a
`function`/`const` keyword node inside namespace_use_clause (single import) or as a
direct child of namespace_use_declaration (grouped import). Before this fix,
`_dotted_imports` treated them identically to plain class imports (`use App\B\Widget;`),
emitting an `imports` edge whose dst suffix-matched a same-basename .php file
(e.g. `use function Laravel\Prompts\confirm` hit `Confirm.php`). A false edge is a
false pause — precision regression in production.

WHAT THIS GATE PROVES (synthetic repo, offline):
  A) `use function A\B\format;`       → NO import edge (format.php exists as a decoy)
  B) `use const A\B\MAX;`             → NO import edge (MAX.php exists as a decoy)
  C) `use function A\B\{f1, f2};`     → NO import edge (f1.php, f2.php exist as decoys)
  D) `use const A\B\{C1, C2};`        → NO import edge (C1.php, C2.php exist as decoys)
  E) `use A\B\Widget;`                → DOES produce an import edge to Widget.php (recall preserved)
  F) `use A\B\{Left, Right};`         → DOES produce import edges to Left.php, Right.php (recall preserved)

Prints `PHP-USE-FUNCTION GATE: PASS` on success, `PHP-USE-FUNCTION GATE: FAIL` on any failure.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402


def _build(files: dict[str, str]):
    """Write files to a temp dir, run the extractor, return resolved file-to-file import edges."""
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        g = X.build_graph(d)
    fset = {n["path"] for n in g["nodes"] if n.get("kind") == "file"}
    edges = {(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == "imports"}
    return edges, fset


def main() -> int:
    # Grammar availability check: PHP grammar must be installed for this gate to run.
    from _cg_languages import _ts_languages
    langs = _ts_languages()
    if "php" not in langs:
        print("PHP-USE-FUNCTION GATE: SKIP (tree-sitter-php not installed)")
        return 0

    # Synthetic repo layout:
    #   src/caller.php     — the file with all the use declarations under test
    #   src/A/B/format.php — DECOY: a real file whose name matches `use function A\B\format`
    #   src/A/B/MAX.php    — DECOY: a real file whose name matches `use const A\B\MAX`
    #   src/A/B/f1.php     — DECOY: a real file whose name matches grouped `use function A\B\{f1, ...}`
    #   src/A/B/f2.php     — DECOY: a real file for grouped use function
    #   src/A/B/C1.php     — DECOY: a real file whose name matches grouped `use const A\B\{C1, ...}`
    #   src/A/B/C2.php     — DECOY: a real file for grouped use const
    #   src/A/B/Widget.php — REAL class target: `use A\B\Widget` should resolve here
    #   src/A/B/Left.php   — REAL class target: `use A\B\{Left, Right}` should resolve here
    #   src/A/B/Right.php  — REAL class target: grouped class import
    CALLER = "src/caller.php"

    files = {
        CALLER: (
            "<?php\n"
            # (A) function import — format.php is a decoy but this must NOT produce an edge
            "use function A\\B\\format;\n"
            # (B) const import — MAX.php is a decoy but this must NOT produce an edge
            "use const A\\B\\MAX;\n"
            # (C) grouped function import — f1.php/f2.php are decoys
            "use function A\\B\\{f1, f2};\n"
            # (D) grouped const import — C1.php/C2.php are decoys
            "use const A\\B\\{C1, C2};\n"
            # (E) plain class import — Widget.php MUST produce an edge
            "use A\\B\\Widget;\n"
            # (F) grouped class import — Left.php/Right.php MUST produce edges
            "use A\\B\\{Left, Right};\n"
        ),
        # Decoy files (named after function/const symbols)
        "src/A/B/format.php": "<?php // decoy\n",
        "src/A/B/MAX.php":    "<?php // decoy\n",
        "src/A/B/f1.php":     "<?php // decoy\n",
        "src/A/B/f2.php":     "<?php // decoy\n",
        "src/A/B/C1.php":     "<?php // decoy\n",
        "src/A/B/C2.php":     "<?php // decoy\n",
        # Real class targets
        "src/A/B/Widget.php": "<?php class Widget {}\n",
        "src/A/B/Left.php":   "<?php class Left {}\n",
        "src/A/B/Right.php":  "<?php class Right {}\n",
    }

    edges, fset = _build(files)
    # Only consider edges FROM the caller file to .php files
    caller_edges = {dst for (src, dst) in edges if src == CALLER and dst.endswith(".php")}

    checks: list[tuple[str, bool]] = []

    # (A) use function A\B\format — must NOT couple to format.php
    checks.append((
        "(A) use function A\\B\\format: no edge to format.php (function symbol, not a file)",
        "src/A/B/format.php" not in caller_edges,
    ))

    # (B) use const A\B\MAX — must NOT couple to MAX.php
    checks.append((
        "(B) use const A\\B\\MAX: no edge to MAX.php (const symbol, not a file)",
        "src/A/B/MAX.php" not in caller_edges,
    ))

    # (C) grouped use function A\B\{f1, f2} — must NOT couple to f1.php or f2.php
    checks.append((
        "(C) use function A\\B\\{f1, f2}: no edge to f1.php (grouped function symbol)",
        "src/A/B/f1.php" not in caller_edges,
    ))
    checks.append((
        "(C) use function A\\B\\{f1, f2}: no edge to f2.php (grouped function symbol)",
        "src/A/B/f2.php" not in caller_edges,
    ))

    # (D) grouped use const A\B\{C1, C2} — must NOT couple to C1.php or C2.php
    checks.append((
        "(D) use const A\\B\\{C1, C2}: no edge to C1.php (grouped const symbol)",
        "src/A/B/C1.php" not in caller_edges,
    ))
    checks.append((
        "(D) use const A\\B\\{C1, C2}: no edge to C2.php (grouped const symbol)",
        "src/A/B/C2.php" not in caller_edges,
    ))

    # (E) use A\B\Widget — MUST couple to Widget.php (recall preserved)
    checks.append((
        "(E) use A\\B\\Widget: edge to Widget.php exists (class import recall preserved)",
        "src/A/B/Widget.php" in caller_edges,
    ))

    # (F) grouped use A\B\{Left, Right} — MUST couple to Left.php AND Right.php
    checks.append((
        "(F) use A\\B\\{Left, Right}: edge to Left.php exists (grouped class import recall preserved)",
        "src/A/B/Left.php" in caller_edges,
    ))
    checks.append((
        "(F) use A\\B\\{Left, Right}: edge to Right.php exists (grouped class import recall preserved)",
        "src/A/B/Right.php" in caller_edges,
    ))

    failures = []
    for label, ok in checks:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}  {label}")
        if not ok:
            failures.append(label)

    print()
    if failures:
        print(f"PHP-USE-FUNCTION GATE: FAIL  ({len(failures)} check(s) failed)")
        return 1
    print("PHP-USE-FUNCTION GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
