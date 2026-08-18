#!/usr/bin/env python3
"""RUST WORKSPACE-CRATE IMPORT RESOLUTION gate (no DB, no network).

WHY THIS GATE EXISTS (measured MED recall miss, ~93 unresolved cross-crate imports on ripgrep):

  A Cargo workspace has member crates (e.g. crates/matcher = package grep-matcher,
  crates/searcher = package grep-searcher). A file in crates/searcher doing:

      use grep_matcher::Matcher;

  is importing a LOCAL workspace sibling, but the resolver had no knowledge of this.
  It saw grep_matcher as an external crate (like serde, std, etc.) and left the import
  unresolved (inert — no file->file dependency edge). Measured on ripgrep: 93 workspace-
  sibling imports were unresolved before this fix.

  FIX (in _cg_resolve._rust_workspace_crate_map + _resolve_imports):
    1. Parse root Cargo.toml [workspace].members (with glob expansion: crates/*).
    2. For each member, parse its Cargo.toml [package].name (+ optional [lib].name).
    3. Build crate_name -> member_src_dir map (hyphens <-> underscores aliases).
    4. When resolving a .rs import, check if the first import segment is a workspace
       crate name. If so, route the remainder into that member's src/ directory using
       the existing suffix-index + mod.rs/lib fallback.

  PRECISION: only workspace members are probed; a crate name not in the map stays
  unresolved (it is a real external dep such as std/serde — correct non-resolve).
  NEVER-CRASH: missing/malformed Cargo.toml -> empty map -> behavior unchanged.
  CARGO NAME vs RUST NAME: grep-matcher (hyphen) maps to grep_matcher (underscore).

CONTENT-FREE: only repo file paths and crate names (manifest metadata) are used.
No user code bodies are read by the resolver.

This gate builds a synthetic Cargo workspace in a tempdir and asserts the real
extractor resolves cross-crate import edges correctly. No DB, no network; deterministic.
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

    # ── Case 1: basic cross-crate import ────────────────────────────────────────────────
    # Workspace: root Cargo.toml members = [crates/a, crates/b]
    # crates/a  package name: crate_a   src: crates/a/src/lib.rs
    # crates/b  package name: crate_b   src: crates/b/src/lib.rs
    # crates/b/src/lib.rs: `use crate_a::Thing;`
    # Expected: b -> a resolved (imports edge b/src/lib.rs -> a/src/lib.rs)
    case1, _ = _build({
        "Cargo.toml": '[workspace]\nmembers = ["crates/a", "crates/b"]\n',
        "crates/a/Cargo.toml": '[package]\nname = "crate_a"\nedition = "2021"\n',
        "crates/a/src/lib.rs": "pub struct Thing;\n",
        "crates/b/Cargo.toml": '[package]\nname = "crate_b"\nedition = "2021"\n',
        "crates/b/src/lib.rs": "use crate_a::Thing;\n",
    })
    b_to_a = ("crates/b/src/lib.rs", "crates/a/src/lib.rs")
    checks.append((
        "basic cross-crate: use crate_a::Thing in crate_b resolves to crates/a/src/lib.rs",
        b_to_a in case1,
    ))

    # ── Case 2: hyphen-to-underscore name normalization ──────────────────────────────────
    # Cargo package name uses hyphen (grep-matcher), but Rust imports use underscore (grep_matcher).
    # The resolver must alias both forms.
    case2, _ = _build({
        "Cargo.toml": '[workspace]\nmembers = ["crates/matcher", "crates/searcher"]\n',
        "crates/matcher/Cargo.toml": '[package]\nname = "grep-matcher"\nedition = "2021"\n',
        "crates/matcher/src/lib.rs": "pub trait Matcher {}\n",
        "crates/searcher/Cargo.toml": '[package]\nname = "grep-searcher"\nedition = "2021"\n',
        "crates/searcher/src/lib.rs": "use grep_matcher::Matcher;\n",
    })
    searcher_to_matcher = ("crates/searcher/src/lib.rs", "crates/matcher/src/lib.rs")
    checks.append((
        "hyphen package name: grep-matcher (Cargo) resolves from grep_matcher:: (Rust import)",
        searcher_to_matcher in case2,
    ))

    # ── Case 3: glob expansion in workspace members ──────────────────────────────────────
    # `members = ["crates/*"]` must expand to include both crates/a and crates/b.
    case3, _ = _build({
        "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
        "crates/alpha/Cargo.toml": '[package]\nname = "alpha_crate"\nedition = "2021"\n',
        "crates/alpha/src/lib.rs": "pub fn alpha_fn() {}\n",
        "crates/beta/Cargo.toml": '[package]\nname = "beta_crate"\nedition = "2021"\n',
        "crates/beta/src/lib.rs": "use alpha_crate::alpha_fn;\n",
    })
    beta_to_alpha = ("crates/beta/src/lib.rs", "crates/alpha/src/lib.rs")
    checks.append((
        "glob expansion crates/*: beta imports alpha_crate from workspace sibling",
        beta_to_alpha in case3,
    ))

    # ── Case 4: precision — external crate stays unresolved ──────────────────────────────
    # `use serde::Serialize;` is an EXTERNAL dep (serde is not in the workspace).
    # It must NOT resolve to any local file (no false edge).
    case4, fset4 = _build({
        "Cargo.toml": '[workspace]\nmembers = ["crates/my_crate"]\n',
        "crates/my_crate/Cargo.toml": '[package]\nname = "my_crate"\nedition = "2021"\n',
        "crates/my_crate/src/lib.rs": "use serde::Serialize;\n",
    })
    lib_rs_imports = [(s, d) for (s, d) in case4
                      if s == "crates/my_crate/src/lib.rs" and d.endswith(".rs")]
    checks.append((
        "precision: external crate serde::Serialize does NOT resolve to any local file",
        len(lib_rs_imports) == 0,
    ))

    # ── Case 5: precision — no workspace (no root Cargo.toml) is inert ──────────────────
    # A single-crate repo with no root [workspace] Cargo.toml: workspace resolution must
    # not fire (inert), and existing intra-crate resolution still works.
    case5, _ = _build({
        "Cargo.toml": '[package]\nname = "single_crate"\nedition = "2021"\n',
        "src/lib.rs": "use crate::submod::Foo;\n",
        "src/submod.rs": "pub struct Foo;\n",
    })
    lib_to_submod = ("src/lib.rs", "src/submod.rs")
    checks.append((
        "no workspace: single-crate repo still resolves intra-crate imports via existing logic",
        lib_to_submod in case5,
    ))

    # ── Case 6: recall-safe — intra-crate imports still work alongside workspace ─────────
    # A workspace member's own intra-crate `use crate::` imports must still resolve
    # correctly when workspace resolution is also active.
    case6, _ = _build({
        "Cargo.toml": '[workspace]\nmembers = ["crates/a", "crates/b"]\n',
        "crates/a/Cargo.toml": '[package]\nname = "crate_a"\nedition = "2021"\n',
        "crates/a/src/lib.rs": "use crate::inner::Inner;\n",
        "crates/a/src/inner.rs": "pub struct Inner;\n",
        "crates/b/Cargo.toml": '[package]\nname = "crate_b"\nedition = "2021"\n',
        "crates/b/src/lib.rs": "use crate_a::inner::Inner;\n",
    })
    a_intra = ("crates/a/src/lib.rs", "crates/a/src/inner.rs")
    b_cross = ("crates/b/src/lib.rs", "crates/a/src/inner.rs")
    checks.append((
        "recall-safe: intra-crate crate::inner::Inner still resolves (not broken by workspace logic)",
        a_intra in case6,
    ))
    checks.append((
        "cross-crate with sub-module: crate_a::inner::Inner resolves to crates/a/src/inner.rs",
        b_cross in case6,
    ))

    ok = True
    for name, cond in checks:
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {name}")
        ok = ok and bool(cond)

    marker = "PASS" if ok else "FAIL"
    print(f"RUST-WORKSPACE GATE: {marker}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
