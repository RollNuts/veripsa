"""Gate: cross-file COUPLING RECALL for supported-language constructs the call graph cannot see.

A SUPPORTED language can carry a real cross-file dependency through a construct that emits NO `calls`
edge — so the coupling is SILENTLY MISSED (recall hole = the worst failure: a missed collision reads as
"clear"). This gate MEASURES two such holes found in INSTALLED grammars and locks the fix, mirroring the
high-precision sym_use:base the spec already gives Java/C#/Rust/PHP/C++/Swift:

  (1) GO STRUCT EMBEDDING — `type Derived struct { Base }` embeds Base (Go's reuse/"inheritance"
      mechanism; methods are PROMOTED, never called). A SAME-PACKAGE embed has NO import AND NO call
      edge → invisible. FIX emits the embedded type NAME as a content-free `calls` edge. Measured
      baseline: zero edges; fixed: `calls -> Base`.
  (2) RUBY MIXIN — `include M` / `extend M` / `prepend M` (the Rails "concern" idiom, the Ruby
      analogue of `implements`). It parses as a call whose callee is the UBIQUITOUS hub `include` and
      whose ARGUMENT is the module constant — so the generic path emitted `calls -> include` and
      DROPPED the module name. FIX emits `calls -> M` (the module) instead of the hub callee.

RECALL-SAFE / PRECISION (proven by the controls below, NOT just the recall direction):
  • GO precision  — a NAMED field `x T` (a type ANNOTATION, the measured-noisy case the spec excludes)
                    must NOT emit a `calls` edge to `T`. Only the embedded (no-field-name) type does.
  • RUBY precision — the bare `include`/`extend` hub name must NOT appear as a `calls` dst; a plain
                    receiver method call (`obj.process()`) must STILL emit `calls -> process`
                    (no regression to the ordinary call path).

The emitted dst is a BARE NAME (content-free) that flows through the engine's EXISTING `_claim_adjacency`
resolution (defs<=3 fan-out cap, import-confirmation, single-definer, hub-dampening) — this gate never
resolves a name itself; it asserts only that build_graph HANDS the resolver the bare-name edge.

CI-DETERMINISM: each language is checked ONLY if its tree-sitter grammar is INSTALLED (probed via
_ts_languages()). A language whose grammar is absent is SKIPPED (skip-pass), so the gate is green in any
environment — an absent grammar is environment, never a bug.

Prints LANG RECALL GATE: PASS on success, ... FAIL on any failure.
"""
import sys
import os
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import code_graph_extract as X
import _cg_languages as L


def _mk(files):
    """Write {relpath: body} into a fresh temp dir; return its path."""
    root = tempfile.mkdtemp(prefix="lang_recall_")
    for rel, body in files.items():
        fp = os.path.join(root, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w") as fh:
            fh.write(body)
    return root


def _calls_from(root, srcfile):
    """The set of bare `calls`-edge dsts emitted FROM srcfile (repo-relative, forward-slash)."""
    g = X.build_graph(root)
    srcfile = srcfile.replace(os.sep, "/")
    return {e["dst"] for e in g["edges"]
            if e.get("kind") == "calls" and e.get("src", "").replace(os.sep, "/") == srcfile}


def main():
    failures = []
    installed = set(L._ts_languages().keys())   # grammars that are BOTH installed AND probe-passing

    # ── GO STRUCT EMBEDDING ──────────────────────────────────────────────────────────────────────
    if "go" in installed:
        # SAME-PACKAGE embed (no import, no call edge → only the embed handler can couple it). The
        # struct also has a NAMED field `named Other` and a primitive `X int` — neither must couple.
        root = _mk({
            "pkg/base.go": "package pkg\ntype Base struct { V int }\n",
            "pkg/other.go": "package pkg\ntype Other struct {}\n",
            "pkg/derived.go": ("package pkg\ntype Derived struct {\n  Base\n  named Other\n"
                               "  X int\n}\nfunc (d Derived) M() {}\n"),
        })
        try:
            calls = _calls_from(root, "pkg/derived.go")
        finally:
            shutil.rmtree(root, ignore_errors=True)
        # RECALL: the embedded type name is emitted as a bare `calls` edge.
        if "Base" not in calls:
            print(f"FAIL [go-embed recall]: same-package `struct {{ Base }}` must emit calls->Base; got {sorted(calls)!r}")
            failures.append("go-embed-recall")
        # PRECISION: a NAMED field's type ANNOTATION (`named Other`, `X int`) must NOT couple.
        if "Other" in calls or "int" in calls:
            print(f"FAIL [go-embed precision]: a named field's annotation type must NOT couple; got {sorted(calls)!r}")
            failures.append("go-embed-precision")

        # CONTROL: a struct with ONLY named fields emits NO embed edge (the recall check is load-bearing —
        # the win is the no-field-name shape, not all field_declarations).
        root = _mk({"pkg/m.go": "package pkg\ntype M struct {\n  a int\n  b Foo\n  c Bar\n}\n"})
        try:
            calls = _calls_from(root, "pkg/m.go")
        finally:
            shutil.rmtree(root, ignore_errors=True)
        if calls:
            print(f"FAIL [go-embed control]: a named-field-only struct must emit no embed edge; got {sorted(calls)!r}")
            failures.append("go-embed-control")
    else:
        print("SKIP [go]: tree-sitter-go grammar not installed (environment, not a bug)")

    # ── RUBY MIXIN (include / extend / prepend) ──────────────────────────────────────────────────
    if "ruby" in installed:
        # AUTOLOAD-style mixin (NO require — the dominant Rails/Zeitwerk shape): the only way to couple
        # Dog to Walkable/ClassMethods is the module-name edge.
        root = _mk({
            "concerns/walkable.rb": "module Walkable\n  def walk; end\nend\n",
            "concerns/class_methods.rb": "module ClassMethods\nend\n",
            "models/dog.rb": "class Dog\n  include Walkable\n  extend ClassMethods\nend\n",
        })
        try:
            calls = _calls_from(root, "models/dog.rb")
        finally:
            shutil.rmtree(root, ignore_errors=True)
        # RECALL: the mixed-in module CONSTANTS are emitted as bare `calls` edges.
        if "Walkable" not in calls:
            print(f"FAIL [ruby-mixin recall]: `include Walkable` must emit calls->Walkable; got {sorted(calls)!r}")
            failures.append("ruby-mixin-recall")
        if "ClassMethods" not in calls:
            print(f"FAIL [ruby-mixin recall]: `extend ClassMethods` must emit calls->ClassMethods; got {sorted(calls)!r}")
            failures.append("ruby-mixin-recall-extend")
        # PRECISION: the ubiquitous hub callee `include`/`extend` must NOT appear as a coupling dst.
        if "include" in calls or "extend" in calls or "prepend" in calls:
            print(f"FAIL [ruby-mixin precision]: the include/extend/prepend hub name must NOT couple; got {sorted(calls)!r}")
            failures.append("ruby-mixin-precision")

        # NAMESPACED mixin: `include Foo::Bar` → bare `Bar` (trailing identifier, content-free).
        root = _mk({"models/x.rb": "class X\n  include Foo::Bar\nend\n"})
        try:
            calls = _calls_from(root, "models/x.rb")
        finally:
            shutil.rmtree(root, ignore_errors=True)
        if "Bar" not in calls:
            print(f"FAIL [ruby-mixin namespaced]: `include Foo::Bar` must emit calls->Bar; got {sorted(calls)!r}")
            failures.append("ruby-mixin-namespaced")

        # CONTROL (no regression): an ordinary RECEIVER method call must STILL emit its `calls` edge —
        # the mixin special-case is scoped to include/extend/prepend only.
        root = _mk({"models/y.rb": "class Y\n  def run\n    helper.process\n  end\nend\n"})
        try:
            calls = _calls_from(root, "models/y.rb")
        finally:
            shutil.rmtree(root, ignore_errors=True)
        if "process" not in calls:
            print(f"FAIL [ruby-call control]: an ordinary receiver call must still emit calls->process; got {sorted(calls)!r}")
            failures.append("ruby-call-regression")
    else:
        print("SKIP [ruby]: tree-sitter-ruby grammar not installed (environment, not a bug)")

    if failures:
        print(f"LANG RECALL GATE: FAIL (failures: {failures})")
        sys.exit(1)
    print("LANG RECALL GATE: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
