#!/usr/bin/env python3
"""SCOPED MONOREPO IMPORT RESOLUTION gate (no DB, no network).

WHY THIS GATE EXISTS (measured MED recall miss, huge JS/TS ecosystem):

  NestJS / Angular nx / Turborepo / Lerna monorepos host many workspace packages
  under packages/*, libs/*, apps/*, etc. Each member has its own package.json
  declaring a SCOPED NAME like "name": "@org/core". Files within the same repo
  import these packages by scoped name:

      import { Thing } from '@org/core';
      import { helper } from '@org/utils/helpers';

  Before this fix, the resolver's _own_packages function explicitly skipped ALL
  imports whose first character is '@' (the guard "if not raw or raw[0] in './<@':
  continue"). On a real NestJS monorepo this left 1,763 @nestjs/* internal imports
  from 1,030 files at 0% resolution -- all cross-package coupling was invisible.

  FIX (in _cg_resolve._scoped_workspace_pkg_map + _resolve_imports):
    1. Scan local package.json files under packages/*, libs/*, apps/*, modules/*,
       projects/* for "name" fields that start with "@".
    2. Build @scope/pkg -> {roots, entry} map (reads package.json body for the name
       -- unavoidable, same precedent as Cargo.toml reading in _rust_workspace_crate_map).
    3. When resolving a @scope/pkg[/subpath] import, check this map FIRST. Only treat
       it as local when the scoped name is in the map AND the subpath resolves to a real
       local file -- external deps not hosted locally stay unresolved (precision guard).

  PRECISION: only local package.json-declared scoped packages are probed. An external
  @nestjs/common (when not in packages/*), @babel/core, @types/node -- any scoped name
  NOT declared in a local package.json -- stays unresolved (correct non-resolve).
  NEVER-CRASH: missing/malformed package.json -> that entry is skipped.
  CONTENT-FREE: only package names (manifest metadata) and file paths are used.

This gate builds a synthetic monorepo in a tempdir and asserts the real extractor
resolves cross-package import edges correctly. No DB, no network; deterministic.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import code_graph_extract as X  # noqa: E402


def _build(files: dict) -> tuple[set, set]:
    """Write files (rel_path -> body) to a temp repo, run the real extractor, return
    (resolved_file_file_import_edges, all_file_paths). The edge set contains only edges
    where dst is an actual repo file (i.e. resolved, not a bare module name)."""
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        g = X.build_graph(d)
    fset = {n["path"] for n in g["nodes"] if n.get("kind") == "file"}
    resolved = {
        (e["src"], e["dst"])
        for e in g["edges"]
        if e["kind"] == "imports" and e["dst"] in fset
    }
    return resolved, fset


def main() -> int:
    checks = []

    # ── Case 1: basic scoped import with subpath ─────────────────────────────────────
    # packages/core/package.json: { "name": "@org/core" }
    # packages/core/src/index.ts: exports Thing
    # packages/app/src/main.ts: imports from '@org/core' and '@org/core/utils'
    # packages/core/src/utils.ts: sibling module
    # Expected: app/main.ts -> core/src/index.ts  (bare @org/core)
    #           app/main.ts -> core/src/utils.ts   (subpath @org/core/utils)
    case1, fset1 = _build({
        "packages/core/package.json": '{"name": "@org/core"}',
        "packages/core/src/index.ts": "export class Thing {}\n",
        "packages/core/src/utils.ts": "export function helper() {}\n",
        "packages/app/package.json": '{"name": "@org/app"}',
        "packages/app/src/main.ts": (
            "import { Thing } from '@org/core';\n"
            "import { helper } from '@org/core/utils';\n"
        ),
    })
    app_main = "packages/app/src/main.ts"
    core_index = "packages/core/src/index.ts"
    core_utils = "packages/core/src/utils.ts"

    hit_bare = (app_main, core_index) in case1
    checks.append(("bare @org/core resolves to packages/core/src/index.ts", hit_bare))

    hit_sub = (app_main, core_utils) in case1
    checks.append(("@org/core/utils resolves to packages/core/src/utils.ts", hit_sub))

    # ── Case 2: precision -- external scoped dep does NOT resolve ─────────────────────
    # Only @org/core is locally declared. @other/lib is NOT in any local package.json.
    # Import of @other/lib must NOT produce a resolved edge (no false coupling).
    case2, fset2 = _build({
        "packages/core/package.json": '{"name": "@org/core"}',
        "packages/core/src/index.ts": "export class Thing {}\n",
        "packages/app/package.json": '{"name": "@org/app"}',
        "packages/app/src/main.ts": (
            "import { X } from '@other/lib';\n"       # external -- no local package.json
            "import { Y } from '@other/lib/deep';\n"  # same
        ),
    })
    app_main2 = "packages/app/src/main.ts"
    # There must be NO resolved edge from app/main.ts to any file for @other/lib.
    external_false_edges = {(s, d) for (s, d) in case2 if s == app_main2}
    checks.append(("external @other/lib does NOT produce resolved edges (precision)", not external_false_edges))

    # ── Case 3: libs/* layout (not just packages/*) ──────────────────────────────────
    case3, fset3 = _build({
        "libs/shared/package.json": '{"name": "@myorg/shared"}',
        "libs/shared/src/index.ts": "export const VERSION = '1';\n",
        "apps/web/package.json": '{"name": "@myorg/web"}',
        "apps/web/src/app.ts": "import { VERSION } from '@myorg/shared';\n",
    })
    web_app = "apps/web/src/app.ts"
    shared_idx = "libs/shared/src/index.ts"
    hit_libs = (web_app, shared_idx) in case3
    checks.append(("@myorg/shared in libs/* resolves from apps/web (libs layout)", hit_libs))

    # ── Case 4: malformed package.json is skipped (never-crash) ─────────────────────
    # The malformed package.json must not crash the extractor.
    try:
        case4, _ = _build({
            "packages/bad/package.json": "NOT VALID JSON {{{",
            "packages/good/package.json": '{"name": "@org/good"}',
            "packages/good/src/index.ts": "export const X = 1;\n",
            "packages/app/src/main.ts": "import { X } from '@org/good';\n",
        })
        app_m4 = "packages/app/src/main.ts"
        good_idx = "packages/good/src/index.ts"
        hit_good = (app_m4, good_idx) in case4
        checks.append(("malformed package.json skipped, good one still resolves (never-crash)", hit_good))
    except Exception as exc:
        checks.append(("malformed package.json skipped, good one still resolves (never-crash)",
                        False))
        print(f"  CRASH on malformed package.json: {exc}")

    # ── Case 5: TOP-LEVEL workspace members (Directus-style) ─────────────────────────
    # Some monorepos (Directus: sdk/, api/, app/) declare named packages at the repo ROOT itself,
    # NOT under packages/*/libs/*. These are pnpm-workspace.yaml / package.json "workspaces" members
    # at depth 1. The _MONO_ROOTS scan never lists abs_root's OWN children, so these resolved 0% before
    # the top-level scan. Expected: app/ imports @acme/sdk[/realtime] -> sdk's files (recall).
    case5, fset5 = _build({
        "sdk/package.json": '{"name": "@acme/sdk"}',
        "sdk/src/index.ts": "export const sdk = 1;\n",
        "sdk/src/realtime.ts": "export const realtime = 1;\n",
        "app/package.json": '{"name": "@acme/app"}',
        "app/src/use-collab.ts": (
            "import { sdk } from '@acme/sdk';\n"
            "import { realtime } from '@acme/sdk/realtime';\n"
        ),
        "tools/helper.ts": "export const h = 1;\n",   # a top-level dir with NO scoped package.json
    })
    use_collab = "app/src/use-collab.ts"
    sdk_index = "sdk/src/index.ts"
    sdk_realtime = "sdk/src/realtime.ts"
    checks.append(("top-level @acme/sdk resolves from app/ (ROOT-level workspace member, recall)",
                   (use_collab, sdk_index) in case5))
    checks.append(("top-level @acme/sdk/realtime subpath resolves (ROOT-level member, recall)",
                   (use_collab, sdk_realtime) in case5))

    # ── Case 6: precision -- a top-level EXTERNAL scoped import (declared nowhere local) does NOT resolve
    case6, _ = _build({
        "sdk/package.json": '{"name": "@acme/sdk"}',
        "sdk/src/index.ts": "export const sdk = 1;\n",
        "app/package.json": '{"name": "@acme/app"}',
        "app/src/main.ts": "import { z } from '@vendor/zzz';\n",   # external -- no local package.json
    })
    appmain6 = "app/src/main.ts"
    checks.append(("external @vendor/zzz (top-level scan path) does NOT resolve (precision)",
                   not {(s, d) for (s, d) in case6 if s == appmain6}))

    # ── Report ────────────────────────────────────────────────────────────────────────
    passed = 0
    failed = 0
    for label, ok in checks:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}")
        if ok:
            passed += 1
        else:
            failed += 1

    print()
    if failed == 0:
        print("SCOPED-MONOREPO GATE: PASS")
        return 0
    else:
        print(f"SCOPED-MONOREPO GATE: FAIL  ({failed}/{len(checks)} checks failed)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
