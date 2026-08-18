#!/usr/bin/env python3
"""RUST trailing-type-name fallback PRECISION gate (no DB, no network).

WHY THIS GATE EXISTS (measured FALSE src->test coupling on a real repo, clap-rs/clap):

  The Rust crate:: trailing-type-name fallback (in _cg_resolve._resolve_imports) resolves
  `use crate::searcher::Searcher;` by dropping the trailing type name `Searcher` and probing
  the suffix index for the CONTAINING MODULE (`searcher/mod` then `searcher`). That is correct
  recall for a real in-crate module.

  BUT the SAME 2-segment shape arises for an EXTERNAL-crate import (`use roff::{Roff, roman}`
  → mod `roff/Roff`) and an INLINE-module import (`use markdown::parse_markdown;` where
  `markdown` is a `mod markdown { .. }` defined in the SAME file → mod `markdown/parse_markdown`).
  Here the leading segment names an external crate / an inline module, NOT a local directory —
  so dropping the trailing segment leaves a BARE name (`roff`, `markdown`) that basename-matches
  an unrelated same-named file. When the ONLY same-basename file is a TEST/EXAMPLE/BENCH file
  (a SEPARATE Cargo compile target, NEVER referenceable as an in-crate module path), the
  uniqueness guard is satisfied by that single decoy → a FALSE production->test edge that
  survives dampening (sole, non-hub).

  MEASURED (clap-rs/clap @ HEAD, tests/audit_repo.py): 3 surviving src->test false edges, all
  from this fallback:
    clap_mangen/src/lib.rs       -> clap_mangen/tests/testsuite/roff.rs   (external crate `roff`)
    clap_mangen/src/render.rs    -> clap_mangen/tests/testsuite/roff.rs   (external crate `roff`)
    clap_derive/src/utils/doc_comments.rs -> tests/derive/markdown.rs     (inline mod `markdown`)

  FIX: filter the candidate set to NON-test/example/bench targets BEFORE the uniqueness check —
  the SAME precedent the C# namespace fallback (`_CS_TEST_SEG`) and the Go `_test.go` rule already
  apply. A real in-crate Rust module is NEVER in tests/examples/benches (those are separate compile
  targets), so this is RECALL-SAFE; and when a test/example decoy is removed, a genuine in-crate
  module that shares the basename now resolves UNIQUELY (a recall GAIN, not loss).

CONTENT-FREE: only repo file paths and Rust module-path strings are used.

Stands up tiny SYNTHETIC Rust crates in tempdirs and asserts the real extractor. Deterministic.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import code_graph_extract as X  # noqa: E402


def _build(files: dict) -> set:
    """Write files (rel_path -> body) to a temp repo, run the real extractor, return the set of
    RESOLVED file->file import edges (dst is an actual repo file, not a bare module name)."""
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        g = X.build_graph(d)
    fset = {n["path"] for n in g["nodes"] if n.get("kind") == "file"}
    return {(e["src"], e["dst"]) for e in g["edges"]
            if e["kind"] == "imports" and e["dst"] in fset}


def main() -> int:
    checks = []

    # ── Case 1: EXTERNAL crate `use roff::{Roff, roman}` must NOT couple to a test decoy ──────
    # Mirrors clap_mangen: src files use the external `roff` crate; the ONLY `roff.rs` in the repo
    # is a TEST file. The fallback drops the trailing type and probes `roff`, which uniquely matches
    # the test file. After the fix, a test target is inert → no false src->test edge.
    e1 = _build({
        "src/lib.rs": "use roff::{Roff, roman};\npub fn f() {}\n",
        "src/render.rs": "use roff::{Roff, bold};\npub fn g() {}\n",
        "tests/testsuite/roff.rs": "// integration test for roff output\n",
    })
    bad = [(s, dst) for (s, dst) in e1 if dst == "tests/testsuite/roff.rs"]
    checks.append((
        "external-crate `use roff::X` does NOT resolve any src file -> tests/testsuite/roff.rs",
        len(bad) == 0,
    ))

    # ── Case 2: INLINE-module `use markdown::parse_markdown;` must NOT couple to a test decoy ──
    # Mirrors clap_derive/doc_comments.rs: `markdown` is an inline `mod markdown { .. }` in the same
    # file; the only `markdown.rs` in the repo is a test file in a DIFFERENT crate.
    e2 = _build({
        "clap_derive/src/utils/doc_comments.rs":
            "use markdown::parse_markdown;\n"
            "fn use_it() { let _ = parse_markdown; }\n"
            "mod markdown { pub fn parse_markdown() {} }\n",
        "tests/derive/markdown.rs": "// integration test\n",
    })
    bad2 = [(s, dst) for (s, dst) in e2 if dst == "tests/derive/markdown.rs"]
    checks.append((
        "inline-module `use markdown::fn` does NOT resolve src -> tests/derive/markdown.rs",
        len(bad2) == 0,
    ))

    # ── Case 3: RECALL preserved — a real in-crate `crate::searcher::Searcher` still resolves ──
    # The legitimate case the fallback exists for must be UNCHANGED: searcher/mod.rs is a real
    # in-crate module (NOT a test target), so it still resolves.
    e3 = _build({
        "src/lib.rs": "use crate::searcher::Searcher;\n",
        "src/searcher/mod.rs": "pub struct Searcher;\n",
        "src/searcher/glue.rs": "// glue\n",
    })
    checks.append((
        "RECALL: crate::searcher::Searcher still resolves src/lib.rs -> src/searcher/mod.rs",
        ("src/lib.rs", "src/searcher/mod.rs") in e3,
    ))

    # ── Case 4: RECALL GAIN — a real in-crate module WINS over a same-named test/example decoy ──
    # `use crate::walk::DirEntry` has a genuine in-crate module src/walk.rs AND a same-basename
    # examples/walk.rs decoy. Before the fix the pair was AMBIGUOUS (2 matches) → left unresolved
    # (a recall MISS). After the fix the example target is inert → the real src/walk.rs resolves
    # UNIQUELY (recall gain), and the example decoy is never coupled.
    e4 = _build({
        "src/lib.rs": "use crate::walk::DirEntry;\n",
        "src/walk.rs": "pub struct DirEntry;\n",
        "examples/walk.rs": "fn main() {}\n",       # separate compile target — inert as a module
    })
    checks.append((
        "RECALL GAIN: crate::walk::DirEntry resolves src/lib.rs -> src/walk.rs (real in-crate module)",
        ("src/lib.rs", "src/walk.rs") in e4,
    ))
    checks.append((
        "PRECISION: crate::walk::DirEntry does NOT couple src/lib.rs -> examples/walk.rs (decoy)",
        ("src/lib.rs", "examples/walk.rs") not in e4,
    ))

    # ── Case 4b: BARE re-export `pub use roff;` must NOT couple to a test decoy ───────────────
    # The SECOND false-coupling path on clap_mangen/src/lib.rs: a BARE single-segment `pub use roff;`
    # (an external-crate re-export) hits the bare-basename fallback, whose only same-basename match is
    # the TEST file. A bare Rust `use barename;` is always an external crate, so a separate compile
    # target must be inert here too.
    e4b = _build({
        "src/lib.rs": "pub use roff;\nuse roff::Roff;\n",
        "tests/testsuite/roff.rs": "// integration test\n",
    })
    bad4b = [(s, dst) for (s, dst) in e4b if dst == "tests/testsuite/roff.rs"]
    checks.append((
        "bare `pub use roff;` does NOT resolve src/lib.rs -> tests/testsuite/roff.rs",
        len(bad4b) == 0,
    ))
    # RECALL preserved: a bare `use foo;` still resolves to a genuine NON-test local module file.
    e4c = _build({
        "src/lib.rs": "use foo;\n",
        "src/foo.rs": "pub fn f() {}\n",
    })
    checks.append((
        "RECALL: bare `use foo;` still resolves src/lib.rs -> src/foo.rs (non-test module untouched)",
        ("src/lib.rs", "src/foo.rs") in e4c,
    ))

    # ── Case 5: PRECISION floor — a genuinely AMBIGUOUS non-test module stays unresolved ──────
    # Two real in-crate modules share the basename `walk` (src/a/walk.rs, src/b/walk.rs). Neither is
    # a test target, so the fix does not disambiguate them — the uniqueness guard still rejects the
    # fan-out, leaving the import unresolved (the conservative pre-fix behaviour for true ambiguity).
    e5 = _build({
        "src/lib.rs": "use crate::walk::DirEntry;\n",
        "src/a/walk.rs": "pub struct DirEntry;\n",
        "src/b/walk.rs": "pub struct DirEntry;\n",
    })
    walk_edges = [(s, dst) for (s, dst) in e5 if s == "src/lib.rs" and dst.endswith("walk.rs")]
    checks.append((
        "PRECISION: two non-test walk.rs stay AMBIGUOUS -> no fan-out (uniqueness guard holds)",
        len(walk_edges) == 0,
    ))

    ok = True
    for name, cond in checks:
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {name}")
        ok = ok and bool(cond)

    marker = "PASS" if ok else "FAIL"
    print(f"RUST-EXTERNAL-CRATE GATE: {marker}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
