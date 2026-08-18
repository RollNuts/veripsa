#!/usr/bin/env python3
"""TS/JS NAMED-IMPORT → BARREL RE-EXPORT RECALL gate (no DB) — recall up, precision preserved, content-free.

WHY THIS GATE EXISTS (a measured recall defect in the TS/JS extractor, pure path/name resolution):

A barrel (`src/middleware.ts`) `export { devtools } from './middleware/devtools.ts'` and a consumer
`import { devtools } from 'pkg/middleware'`. The consumer's REAL dependency is the IMPLEMENTATION file
`src/middleware/devtools.ts` — editing `devtools` ripples to it — but the extractor emitted ONLY the
bare-module edge to the BARREL (`src/middleware.ts`), so the consumer→impl coupling was SILENTLY MISSED.
The adjacency couples DIRECT import pairs and does not follow the barrel hop, so the impl file stayed a
graph-blind 'clear'. MEASURED on pmndrs/zustand: live (dampened) co-change recall 57.1% → 71.4% (4/7 →
5/7 GT pairs), raw import pairs 79 → 91; every new live pair a REAL test→impl link (e.g.
`tests/devtools.test.tsx` ↔ `src/middleware/devtools.ts`) — zero fabricated couplings.

FIX (this lane, `_cg_languages._walk_ts_tree`): for each NAMED import specifier `nm` in `import { nm }
from 'mod'`, ALSO emit a name-qualified `imports` edge `mod/nm` — the SAME shape Python's bespoke path
already emits for `from pkg import name` (→ `pkg.name`). The resolver's EXISTING suffix / own-package
probe then reaches a submodule FILE named `nm` (`pkg/middleware/devtools` → `src/middleware/devtools.ts`)
when the barrel re-exports it under that name. CONTENT-FREE: a module path + an imported identifier,
never a body.

PRECISION-SAFE (the discriminator): `mod/nm` resolves ONLY when a file actually carries that suffix.
A named import from an EXTERNAL package (`import { useState } from 'react'` → `react/useState`) names no
local file → the edge stays inert (no resolved file→file edge). The IMPORTED name is read from the
specifier's `name` field, never the local `alias` (`{ x as y }` re-exports `x`, not `y`). A `* as ns`
namespace / default import has no `import_specifier` → only the bare-module edge, unchanged.

This gate stands up tiny SYNTHETIC repos and asserts, through the REAL extractor (build_graph →
_resolve_imports), the resolved file→file import edge set:
  (a) RECALL    — `import { x } from 'pkg/barrel'` where the barrel re-exports `x` from `./barrel/x.ts`
                  resolves the consumer to `src/barrel/x.ts` (the impl), not only the barrel.
  (b) RECALL    — works for a deep relative barrel too (`import { x } from './barrel'`).
  (c) PRECISION — `import { useState } from 'react'` (external) fabricates NO `react/useState` file edge.
  (d) PRECISION — the IMPORTED name is used, not the alias: `{ devtools as d }` couples to `devtools.ts`,
                  and an alias name with no matching file fabricates nothing.
  (e) CONTROL   — the bare-module edge is STILL emitted (the fix is additive, never a regression): the
                  consumer is also coupled to the barrel itself.
  (f) INERT     — a default / namespace import emits NO name-qualified edge (no `import_specifier`).
No DB, no network; deterministic.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import code_graph_extract as X  # noqa: E402


def _edges(files):
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
    imp = {(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == "imports" and e["dst"] in fset}
    return imp, fset


def main() -> int:
    checks = []

    # ── (a) own-package barrel re-export: consumer `pkg/barrel` -> impl file via the re-exported name ──────
    # package "acme": src/index.ts (entry), barrel src/feat.ts re-exports `widget` from ./feat/widget.ts.
    e1, _ = _edges({
        "package.json": '{"name": "acme", "main": "./src/index.ts"}',
        "src/index.ts": "export const x = 1\n",
        "src/feat.ts": "export { widget } from './feat/widget.ts'\n",
        "src/feat/widget.ts": "export const widget = () => 1\n",
        "src/feat/other.ts": "export const other = () => 2\n",   # a sibling that must NOT be coupled
        "tests/widget.test.ts": "import { widget } from 'acme/feat'\n",
    })
    checks.append(("(a) named import `{ widget } from 'acme/feat'` couples to the IMPL src/feat/widget.ts",
                   ("tests/widget.test.ts", "src/feat/widget.ts") in e1))
    checks.append(("(a-neg) it does NOT couple to the unrelated sibling src/feat/other.ts",
                   ("tests/widget.test.ts", "src/feat/other.ts") not in e1))
    # (e) CONTROL: the bare-module edge to the BARREL is STILL present (additive, no regression).
    checks.append(("(e) additive: the bare-module edge to the barrel src/feat.ts is STILL emitted",
                   ("tests/widget.test.ts", "src/feat.ts") in e1))

    # ── (b) RELATIVE barrel re-export (no manifest needed): `import { x } from './feat'` where the impl ────
    # lives in the matching `feat/` subdir (the dominant barrel layout: a `feat.ts` barrel beside a `feat/`
    # dir, exactly zustand's `middleware.ts` + `middleware/`). The name-qualified `./feat/thing` then suffix-
    # matches the impl. (HONEST BOUNDARY: a barrel that re-exports from a DIFFERENTLY-named dir — `feat.ts`
    # re-exporting from `./impl/thing.ts` — is NOT recovered by this name heuristic; that stays a recall gap,
    # never a false edge. We recover the common matching-name case, the one that dominates real npm packages.)
    e2, _ = _edges({
        "feat.ts": "export { thing } from './feat/thing.ts'\n",
        "feat/thing.ts": "export const thing = 1\n",
        "consumer.ts": "import { thing } from './feat'\n",
    })
    checks.append(("(b) relative `{ thing } from './feat'` couples to ./feat/thing.ts (the impl)",
                   ("consumer.ts", "feat/thing.ts") in e2))
    checks.append(("(b-ctrl) and STILL couples to the barrel itself ./feat.ts",
                   ("consumer.ts", "feat.ts") in e2))

    # ── (c) PRECISION: an EXTERNAL named import fabricates NO name-qualified file edge ─────────────────────
    # There is a LOCAL file named useState.ts, but it lives OUTSIDE any react package — `react/useState`
    # must resolve to NO repo file (the strong precision assertion: external named imports stay inert).
    e3, fset3 = _edges({
        "app.ts": "import { useState } from 'react'\n",
        "node_decoy/useState.ts": "export const useState = 1\n",   # a same-name decoy that must NOT be coupled
    })
    fabricated = [(s, d) for (s, d) in e3 if s == "app.ts"]
    checks.append(("(c) external `{ useState } from 'react'` fabricates NO file edge (stays inert)",
                   not fabricated))

    # ── (d) the IMPORTED name is used, not the LOCAL alias ────────────────────────────────────────────────
    # `{ widget as w }` re-exported under `widget` must couple to widget.ts; an alias whose name matches a
    # file must NOT (the alias is local-only and is never the re-exported symbol).
    e4, _ = _edges({
        "package.json": '{"name": "acme", "main": "./src/index.ts"}',
        "src/index.ts": "export const x = 1\n",
        "src/feat.ts": "export { widget } from './feat/widget.ts'\n",
        "src/feat/widget.ts": "export const widget = 1\n",
        "src/feat/w.ts": "export const w = 1\n",                   # matches the ALIAS, must NOT couple
        "tests/alias.test.ts": "import { widget as w } from 'acme/feat'\n",
    })
    checks.append(("(d) `{ widget as w }` uses the IMPORTED name → couples to src/feat/widget.ts",
                   ("tests/alias.test.ts", "src/feat/widget.ts") in e4))
    checks.append(("(d-neg) the LOCAL alias `w` does NOT couple to the same-name decoy src/feat/w.ts",
                   ("tests/alias.test.ts", "src/feat/w.ts") not in e4))

    # ── (f) INERT for default / namespace imports (no import_specifier) ───────────────────────────────────
    # A default import `import Foo from './mod'` and a namespace `import * as ns from './mod'` carry NO
    # named specifier → only the bare-module edge, never a `mod/Foo` / `mod/ns` decoy.
    e5, _ = _edges({
        "mod.ts": "const x = 1\nexport default x\n",
        "Foo.ts": "export const Foo = 1\n",                        # a decoy matching the default-local name
        "user.ts": "import Foo from './mod'\nimport * as ns from './mod'\n",
    })
    checks.append(("(f) default/namespace import couples ONLY to the bare module ./mod.ts",
                   ("user.ts", "mod.ts") in e5
                   and ("user.ts", "Foo.ts") not in e5))

    # ── (g) CONTROL that recall is LOAD-BEARING: with NO re-export, the impl is NOT reachable (so (a)'s ────
    # pass came from the new edge, not from some unrelated path). Same files as (a) minus the re-export.
    e6, _ = _edges({
        "package.json": '{"name": "acme", "main": "./src/index.ts"}',
        "src/index.ts": "export const x = 1\n",
        "src/feat.ts": "export const feat = 1\n",                  # NOT a barrel: defines its own symbol
        "src/feat/widget.ts": "export const widget = () => 1\n",
        "tests/widget.test.ts": "import { feat } from 'acme/feat'\n",  # `feat` IS the barrel's own symbol
    })
    # `acme/feat` resolves to src/feat.ts (the file), and `acme/feat/feat` names no file → impl stays uncoupled.
    checks.append(("(g) control: `{ feat }` from a NON-barrel couples to src/feat.ts, NOT src/feat/widget.ts",
                   ("tests/widget.test.ts", "src/feat.ts") in e6
                   and ("tests/widget.test.ts", "src/feat/widget.ts") not in e6))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("TS NAMED-IMPORT BARREL RECALL GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
