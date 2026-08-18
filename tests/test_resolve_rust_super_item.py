#!/usr/bin/env python3
"""RUST super::/self:: TRAILING ITEM-NAME resolution gate (no DB, no network).

WHY THIS GATE EXISTS (measured HIGH recall miss on tokio: 36 dropped intra-crate edges):

  A relative Rust import whose tail is a SINGLE identifier that names an ITEM (a type / fn /
  const re-exported or defined in the PARENT module) was NOT resolved:

    // tokio/src/io/async_read.rs
    use super::ReadBuf;          // ReadBuf is `pub use`-d in tokio/src/io/mod.rs

  The resolver correctly anchors `super` at the importer's module directory (`tokio/src/io`)
  and probes for a SUBMODULE file `…/io/ReadBuf.rs` and folder-module `…/io/ReadBuf/mod.rs`.
  Both miss (ReadBuf is not a submodule), so the import went UNRESOLVED — even though `super::X`
  in Rust falls through to "an item X in the parent module", whose file is the PARENT MODULE
  FILE: `…/io/mod.rs` (folder-module) or `…/io.rs` (a flat module whose submodules live in a dir).
  Measured on tokio: 36 such `super::ReadBuf`/`super::TcpListener`/`super::Inject` couplings,
  every one to a unique existing parent-module file, were silently dropped.

  FIX (in _cg_resolve, the self/super relative branch): after the submodule + folder-module
  probes miss AND the tail is a SINGLE identifier (no `/`), fall back to the PARENT MODULE FILE
  (`climb/mod.rs` OR `climb.rs`). Accept a UNIQUE match only; discard a self-reference (a
  `super::X` written from the parent file itself is not a cross-file edge). This MIRRORS the
  `crate::` trailing-type-name fallback for the relative root, and runs ONLY after submodule
  resolution missed — so a real `super::submod` still wins its sibling FILE first (Case 4).

CONTENT-FREE: only repo file paths and Rust module-path strings are used.
Hermetic: stands up tiny synthetic crates in a tempdir; no DB, no network; deterministic.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import code_graph_extract as X  # noqa: E402


def _build(files: dict):
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        g = X.build_graph(d)
    fset = {n["path"] for n in g["nodes"] if n.get("kind") == "file"}
    resolved = {(e["src"], e["dst"]) for e in g["edges"]
                if e["kind"] == "imports" and e["dst"] in fset}
    return resolved, fset


def main() -> int:
    checks = []

    # ── Case 1: super::Type → parent FOLDER-module file (…/io/mod.rs) ─────────────────────
    # Mirrors tokio/src/io/async_read.rs `use super::ReadBuf;` resolving to tokio/src/io/mod.rs.
    c1, _ = _build({
        "src/io/mod.rs": "pub use self::read_buf::ReadBuf;\npub mod async_read;\npub mod read_buf;\n",
        "src/io/async_read.rs": "use super::ReadBuf;\n",
        "src/io/read_buf.rs": "pub struct ReadBuf;\n",
    })
    edge1 = ("src/io/async_read.rs", "src/io/mod.rs")
    checks.append((
        "super::ReadBuf (item, not submodule) resolves async_read.rs -> io/mod.rs (folder-module parent)",
        edge1 in c1,
    ))

    # ── Case 2: super::Type → parent FLAT-module file (…/inject.rs) ───────────────────────
    # Mirrors tokio/src/runtime/scheduler/inject/metrics.rs `use super::Inject;` resolving to
    # tokio/src/runtime/scheduler/inject.rs (a flat module file whose submodules live in inject/).
    c2, _ = _build({
        "src/scheduler/inject.rs": "pub(crate) struct Inject;\npub mod metrics;\n",
        "src/scheduler/inject/metrics.rs": "use super::Inject;\n",
    })
    edge2 = ("src/scheduler/inject/metrics.rs", "src/scheduler/inject.rs")
    checks.append((
        "super::Inject (item) resolves inject/metrics.rs -> inject.rs (flat-module parent file)",
        edge2 in c2,
    ))

    # ── Case 3: PRECISION — super::Type with NO parent file stays UNRESOLVED ──────────────
    # No mod.rs and no sibling-named module file at the parent level → no unique target.
    c3, fset3 = _build({
        "src/widget.rs": "use super::Helper;\n",   # parent dir is src/, no src/mod.rs, no src.rs
    })
    widget_edges = [(s, d) for (s, d) in c3 if s == "src/widget.rs" and d.endswith(".rs")]
    checks.append((
        "super::Helper with no parent module file does NOT fan out (no unique target → unresolved)",
        len(widget_edges) == 0,
    ))

    # ── Case 4: SUBMODULE still wins over the parent-file fallback (precedence) ───────────
    # `use super::lines;` where a sibling submodule lines.rs EXISTS must resolve to lines.rs,
    # NOT to the parent mod.rs (the existing relative behaviour, not broken by the fallback).
    c4, _ = _build({
        "src/searcher/mod.rs": "use super::lines;\npub mod lines;\n",
        "src/lines.rs": "pub fn f() {}\n",
    })
    edge4_submod = ("src/searcher/mod.rs", "src/lines.rs")
    edge4_parent = ("src/searcher/mod.rs", "src/mod.rs")  # would-be wrong parent (also doesn't exist)
    checks.append((
        "super::lines resolves to the sibling submodule lines.rs, not a parent file (precedence kept)",
        edge4_submod in c4 and edge4_parent not in c4,
    ))

    # ── Case 5: PRECISION — super::Type written FROM the parent file is not a self-edge ───
    # `use super::Thing;` in foo/bar.rs where Thing lives in foo/mod.rs resolves to foo/mod.rs;
    # but the SAME written in foo/mod.rs (referring to its own grandparent) must not self-couple.
    c5, _ = _build({
        "src/a/mod.rs": "use super::Shared;\n",     # parent of a/ is src/ (no src/mod.rs) -> unresolved, not self
        "src/a/child.rs": "// nothing\n",
    })
    self_edges = [(s, d) for (s, d) in c5 if s == d]
    checks.append((
        "super::X never creates a self-edge",
        len(self_edges) == 0,
    ))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print(f"RUST-SUPER-ITEM GATE: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
