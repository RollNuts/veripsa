#!/usr/bin/env python3
"""RUST CRATE:: IMPORT RESOLUTION gate (no DB, no network) — trailing type-name fallback.

WHY THIS GATE EXISTS (measured HIGH recall miss on ripgrep, 98% miss rate):

  Rust intra-crate imports like `use crate::searcher::Searcher;` were NOT resolved.
  The resolver correctly strips `crate::` and builds mod = `searcher/Searcher`, then
  probes the suffix index for that path. No file is named `Searcher.rs`, so the probe
  misses. The REAL module file is one of:
    - `…/searcher/mod.rs`  (Rust folder-module convention, suffix key `searcher/mod`)
    - `…/searcher.rs`      (flat module file, suffix key `searcher`)
  Neither was tried as a fallback, so 85/87 crate:: imports in ripgrep went unresolved.

  FIX (in _cg_resolve._resolve_imports): after the full-path suffix probe misses for a
  .rs source file, and mod contains at least one `/`, also try:
    1. module_prefix + `/mod`  (Rust folder-module: `searcher/mod` hits `searcher/mod.rs`)
    2. module_prefix            (flat module: `searcher` hits `searcher.rs`)
  where module_prefix = `/`.join(segs[:-1]) (all segments except the trailing type name).
  PRECISION guard: only accept a UNIQUE match (len==1). Ambiguous module names (two files
  share the same suffix, e.g. `walk.rs` in both src/ and examples/) are left unresolved
  rather than creating a false edge.

  OUT OF SCOPE: the cross-workspace-crate case (`grep_matcher::` from a sibling Cargo
  workspace member) — that requires locating the sibling crate root from Cargo.toml and
  is not addressed in this fix.

CONTENT-FREE: only repo file paths and Rust module-path strings are used. No file bodies
are read by the resolver; build_graph reads source bodies only for AST extraction.

This gate stands up a tiny SYNTHETIC Rust crate in a tempdir and asserts the real
extractor resolves the import edge. No DB, no network; deterministic.
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

    # ── Case 1: use crate::searcher::Searcher from lib.rs ────────────────────────────────
    # Layout mirrors ripgrep's crates/searcher/src/:
    #   src/lib.rs         (crate root, imports searcher::Searcher)
    #   src/searcher/mod.rs (defines Searcher — this is what the import should resolve to)
    #   src/searcher/glue.rs (sibling file inside the searcher module)
    #
    # The import path after stripping crate:: is `searcher/Searcher`. No file is named
    # Searcher.rs; the resolver must fall back to `searcher/mod` (the mod.rs file).
    crate1, _ = _build({
        "src/lib.rs": "use crate::searcher::Searcher;\n",
        "src/searcher/mod.rs": "pub struct Searcher;\n",
        "src/searcher/glue.rs": "// glue\n",
    })
    # lib.rs -> searcher/mod.rs must be a resolved edge
    lib_to_mod = ("src/lib.rs", "src/searcher/mod.rs")
    checks.append((
        "crate::searcher::Searcher resolves lib.rs -> searcher/mod.rs (folder-module)",
        lib_to_mod in crate1,
    ))

    # ── Case 2: use crate::flags::lowargs::FieldMatchSeparator (two-level module) ────────
    # flags/lowargs.rs is a flat sub-module of flags/mod.rs.
    # After stripping crate:: : mod = `flags/lowargs/FieldMatchSeparator`
    # segs = [flags, lowargs, FieldMatchSeparator]
    # module_prefix = `flags/lowargs`
    # Probe 1: `flags/lowargs/mod` -> miss (no flags/lowargs/mod.rs)
    # Probe 2: `flags/lowargs`     -> hits `flags/lowargs.rs` (unique)
    crate2, _ = _build({
        "src/main.rs": "use crate::flags::lowargs::FieldMatchSeparator;\n",
        "src/flags/mod.rs": "pub mod lowargs;\n",
        "src/flags/lowargs.rs": "pub struct FieldMatchSeparator;\n",
    })
    main_to_lowargs = ("src/main.rs", "src/flags/lowargs.rs")
    checks.append((
        "crate::flags::lowargs::FieldMatchSeparator resolves main.rs -> flags/lowargs.rs (flat sub-module)",
        main_to_lowargs in crate2,
    ))

    # ── Case 3: precision — single-segment crate:: import does NOT fan out ───────────────
    # `use crate::SearcherBuilder;` after strip -> mod = `SearcherBuilder`, no `/`.
    # The trailing-type fallback requires `/ in mod`, so it must NOT fire. The import
    # stays unresolved (it names a type in lib.rs itself, not a separate file).
    crate3, fset3 = _build({
        "src/lib.rs": "use crate::SearcherBuilder;\npub struct SearcherBuilder;\n",
    })
    # No resolved edge from lib.rs to another .rs file for this single-segment import
    lib_imports_rs = [(s, d) for (s, d) in crate3 if s == "src/lib.rs" and d.endswith(".rs")]
    checks.append((
        "single-segment crate::SearcherBuilder does NOT fan out to any file (no slash = no fallback)",
        len(lib_imports_rs) == 0,
    ))

    # ── Case 4: precision — ambiguous module name stays unresolved ────────────────────────
    # A real in-crate module src/walk.rs and a same-basename examples/walk.rs DECOY.
    # examples/ is a SEPARATE Cargo compile target (never an in-crate module), so the resolver now
    # excludes it from the candidate set (see _cg_resolve._rs_inert_module_target / the external-crate
    # precision fix + tests/test_resolve_rust_external_crate.py). With the decoy gone the genuine module
    # resolves UNIQUELY: a recall GAIN, and STILL no false edge to the example. (Pre-fix this pair was
    # ambiguous → left unresolved; the new behaviour is strictly better.) A precision floor for TWO real
    # NON-target modules sharing a basename is covered by tests/test_resolve_rust_external_crate.py Case 5.
    crate4, fset4 = _build({
        "src/lib.rs": "use crate::walk::DirEntry;\n",
        "src/walk.rs": "pub struct DirEntry;\n",
        "examples/walk.rs": "fn main() {}\n",  # separate compile target -> inert as a module
    })
    lib_to_walk_src = ("src/lib.rs", "src/walk.rs")
    lib_to_walk_ex = ("src/lib.rs", "examples/walk.rs")
    checks.append((
        "crate::walk::DirEntry resolves to the real src/walk.rs and NOT the examples/walk.rs decoy",
        lib_to_walk_src in crate4 and lib_to_walk_ex not in crate4,
    ))

    # ── Case 5: self/super relative imports still work (recall-safe, not broken by fix) ──
    # The fix only fires when `crate::` was stripped. `self::` / `super::` take the
    # relative-resolution path and must still work correctly.
    crate5, _ = _build({
        "src/searcher/mod.rs": "use super::lines;\n",
        "src/lines.rs": "pub fn f() {}\n",
    })
    searcher_to_lines = ("src/searcher/mod.rs", "src/lines.rs")
    checks.append((
        "super::lines still resolves searcher/mod.rs -> lines.rs (existing relative path, not broken)",
        searcher_to_lines in crate5,
    ))

    ok = True
    for name, cond in checks:
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {name}")
        ok = ok and bool(cond)

    marker = "PASS" if ok else "FAIL"
    print(f"RUST-CRATE GATE: {marker}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
