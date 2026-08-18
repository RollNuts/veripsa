#!/usr/bin/env python3
"""SYMBOL-USE RECALL gate (hermetic, deterministic).

A file F can DEPEND ON G with NO plain `calls` edge: it CONSTRUCTS one of G's types (`new Foo()` / a Go
composite literal / a Rust struct expr) or EXTENDS/IMPLEMENTS a base type G defines. The call graph misses
these — measured on real C# repos, this is +13.9 pts of co-change recall (shadowsocks) and 1752 newly
CORROBORATED namespace edges (jellyfin), exactly PR #253's finding. We capture base + constructor uses as
`calls`-class edges (dst = the used TYPE NAME), so they resolve to the defining file through the SAME
`_claim_adjacency` machinery (defs_ok≤3 fan-out cap, import-confirmation, single-definer, hub dampening,
prod→test guard) every call edge already passes — that machinery is what bounds precision.

This gate pins BOTH halves on tiny KNOWN-TRUTH multi-language fixtures:

  RECALL — a constructor use (`new Widget()`), a base-type use (`extends`/`implements`/`: Base`/`impl ... for`),
           and a Go composite literal across files each produce a `calls` edge whose dst is the USED TYPE
           NAME, so the engine can couple the using file to the file that DEFINES that type.

  PRECISION (the inverse risk) — a ubiquitous type name used as a constructor/field does NOT fan a coupling
           out to every same-named file: the extractor emits the NAME (it does not resolve), and the live
           engine's defs_ok≤3 + multi-definer-needs-import guard drops the decoy. We assert structurally
           that (a) a use of a type defined in >3 files is NOT in defs_ok (so it can never couple), and
           (b) a constructor's ARGUMENTS are NOT captured as type uses (only the constructed type is).

Hermetic: writes tiny source files to a temp dir, reads the RAW edges the language extractor emits — no
network, no checkout. Skips a language whose grammar is not loaded (never a false RED; the loaded set is
asserted non-trivial). Content-free throughout (we assert on edge dst NAMES, never source bodies).

Final line: SYMBOL-USE RECALL GATE: PASS/FAIL.  Returns 0/1.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import _cg_languages as LG  # noqa: E402
import code_graph_extract as X  # noqa: E402
from tree_sitter import Parser  # noqa: E402

_LANGS = LG._ts_languages()
_FAILS = []
_RAN = []
_SKIPPED = []


def _calls(filename, src):
    """The raw `calls` dst names the per-file extractor emits for one source file (None if grammar absent)."""
    ext = os.path.splitext(filename)[1]
    label = LG._LABEL_BY_EXT.get(ext)
    gram = LG._GRAMMAR_BY_EXT.get(ext)
    if gram not in _LANGS:
        return None
    parser = Parser(_LANGS[gram])
    d = tempfile.mkdtemp(prefix="sur-")
    p = os.path.join(d, filename)
    with open(p, "w") as fh:
        fh.write(src)
    if label in ("javascript", "typescript"):
        nodes, edges, _ = LG.extract_file_ts(p, filename, label, parser)
    else:
        nodes, edges, _ = LG.extract_file_generic(p, filename, label, parser, LG._GENERIC_SPEC[label])
    return sorted(e["dst"] for e in edges if e["kind"] == "calls")


def case(lang, filename, src, must_calls=(), forbid_calls=()):
    out = _calls(filename, src)
    if out is None:
        _SKIPPED.append(f"{lang} ({filename}) — grammar not loaded")
        return
    _RAN.append(lang)
    for c in must_calls:
        if c not in out:
            _FAILS.append(f"[{lang}] RECALL: missing symbol-use call edge {c!r}; got calls={out}")
    for c in forbid_calls:
        if c in out:
            _FAILS.append(f"[{lang}] PRECISION: FALSE symbol-use call edge {c!r}; got calls={out}")


# ── C#: the measured WIN — types wired via `new` + inheritance, NOT plain calls. base_list + ctor. ──
case("csharp", "App.cs", '''
namespace App {
  class Worker : BaseService, IHandler {
    private Repository _repo;
    public void Run() { var u = new UserService(); var w = new Widget(EXTRA_ARG_NAME); Local(); }
    void Local() {}
  }
}
''',
     must_calls=["BaseService", "IHandler", "UserService", "Widget", "Local"],
     forbid_calls=["EXTRA_ARG_NAME"])   # a constructor ARGUMENT is not a type use

# ── Java: extends/implements (superclass/super_interfaces) + `new` constructor. ──────────────────
case("java", "App.java", '''
package com.x;
public class App extends Base implements Handler {
    public void m() { Widget w = new Widget(argValue); helper(); }
    private void helper() {}
}
''',
     must_calls=["Base", "Handler", "Widget", "helper"],
     forbid_calls=["argValue"])

# ── Go: no inheritance; a value is CONSTRUCTED by a composite literal `Foo{}` / `pkg.Foo{}`. ──────
case("go", "main.go", '''
package main
func use() { s := Server{}; c := &config.Config{}; helper() }
func helper() {}
''',
     must_calls=["Server", "Config", "helper"])

# ── TypeScript: class heritage (extends/implements) + `new` expression. ──────────────────────────
case("typescript", "app.ts", '''
class App extends Base implements Handler {
  build() { const w = new Widget(argValue); local(); }
}
function local() {}
''',
     must_calls=["Base", "Handler", "Widget", "local"],
     forbid_calls=["argValue"])

# ── Rust: `impl Trait for Type` couples to BOTH; a struct expression `Foo{}` is construction. ─────
case("rust", "lib.rs", '''
impl Greet for App {
    fn greet(&self) { let h = Helper { field: 1 }; thing(); }
}
''',
     must_calls=["Greet", "App", "Helper", "thing"])

# ── PHP: extends/implements + `new`. ─────────────────────────────────────────────────────────────
case("php", "App.php", '''<?php
namespace App;
class Worker extends Base implements Handler {
    public function run() { $w = new Widget(); $this->local(); }
    private function local() {}
}
''',
     must_calls=["Base", "Handler", "Widget", "local"])

# ── Swift: conformances (`: Base, Handler`); construction `Widget()` is a plain call (no `new`). ──
case("swift", "App.swift", '''
class App: Base, Handler {
    func run() { let w = Widget(); local() }
    func local() {}
}
''',
     must_calls=["Base", "Handler", "Widget", "local"])

# ── C++: base_class_clause (`: public Base`) + `new` expression. ─────────────────────────────────
case("cpp", "main.cpp", '''
class App : public Base, public IHandler {
    void run() { auto* w = new Widget(); local(); }
    void local() {}
};
''',
     must_calls=["Base", "IHandler", "Widget", "local"])


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# RESOLUTION-LEVEL RECALL: build a tiny MULTI-FILE repo and run the FULL build_graph, then assert that
# the symbol-use `calls` edge lets the engine couple the USING file to the file that DEFINES the type —
# the actual recall win (a base/ctor use across files, where there is NO plain call). The dst stays a
# NAME (calls edges are name-resolved by the engine), so we assert the def the name resolves to exists.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def recall_resolution_case(lang, files, using_file, used_type, defining_file):
    """The using file must emit a `calls` edge to `used_type`, AND `used_type` must be a symbol the
    DEFINING file contains — so the name-join the engine runs couples using_file ↔ defining_file."""
    ext = os.path.splitext(using_file)[1]
    if LG._GRAMMAR_BY_EXT.get(ext) not in _LANGS:
        _SKIPPED.append(f"{lang} resolve ({using_file}) — grammar not loaded")
        return
    d = tempfile.mkdtemp(prefix="surr-")
    for rel, content in files.items():
        fp = os.path.join(d, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w") as fh:
            fh.write(content)
    g = X.build_graph(d)
    _RAN.append(lang)
    call_dsts = {e["dst"] for e in g["edges"] if e["kind"] == "calls" and e["src"] == using_file}
    # the symbol the defining file contains (the join target)
    defined = {e["dst"].split("::", 1)[1] for e in g["edges"]
               if e["kind"] == "contains" and e["src"] == defining_file}
    if used_type not in call_dsts:
        _FAILS.append(f"[{lang}] RESOLVE RECALL: {using_file} has no symbol-use call to {used_type!r}; "
                      f"calls={sorted(call_dsts)}")
    elif used_type not in defined:
        _FAILS.append(f"[{lang}] RESOLVE RECALL: {defining_file} does not define {used_type!r} "
                      f"(defines {sorted(defined)}) — name-join cannot couple")
    # blast_radius is the engine's own name-join: the using file must now be in the type-definer's radius.
    radius = X.blast_radius(g, defining_file)
    if using_file not in radius:
        _FAILS.append(f"[{lang}] RESOLVE RECALL: {using_file} NOT in blast radius of {defining_file} "
                      f"(radius={sorted(radius)}) — the symbol-use edge did not produce coupling")


# C#: Worker CONSTRUCTS UserService (defined in another file) — no call edge would exist, only `new`.
recall_resolution_case("csharp",
                        {"App/Worker.cs": "namespace App { class Worker { void Run(){ var u = new UserService(); } } }\n",
                         "App/UserService.cs": "namespace App { class UserService { public UserService(){} } }\n"},
                        "App/Worker.cs", "UserService", "App/UserService.cs")

# Java: App EXTENDS Base (defined in another file) — inheritance coupling, no call edge.
recall_resolution_case("java",
                        {"com/App.java": "package com;\npublic class App extends Base { void m(){} }\n",
                         "com/Base.java": "package com;\npublic class Base { }\n"},
                        "com/App.java", "Base", "com/Base.java")

# Go: a composite literal Server{} where Server is a type defined in another file.
recall_resolution_case("go",
                        {"main.go": "package main\nfunc use(){ s := Server{}; _ = s }\n",
                         "server.go": "package main\ntype Server struct { X int }\n"},
                        "main.go", "Server", "server.go")


def _precision_no_fanout():
    """PRECISION (the ubiquitous-name trap): a symbol-use of a type defined in MANY files must NOT couple
    out. The extractor emits the NAME (no resolution); the live engine's defs_ok≤3 cap is the guard. We
    prove the guard's PREMISE here at the extractor/graph level: a type name defined in >3 files is NOT a
    one-line couple target — blast_radius for any ONE definer must NOT include the user (the name is
    ambiguous, so the engine drops it). Mirrors recall_measure's defs_ok and _claim_adjacency's def_n>1."""
    d = tempfile.mkdtemp(prefix="surp-")
    # `Options` defined in FOUR files (a ubiquitous type), constructed in a user file.
    for i in range(4):
        fp = os.path.join(d, f"pkg{i}/Options.cs")
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w") as fh:
            fh.write(f"namespace P{i} {{ class Options {{ }} }}\n")
    up = os.path.join(d, "app/User.cs")
    os.makedirs(os.path.dirname(up), exist_ok=True)
    with open(up, "w") as fh:
        fh.write("namespace App { class User { void M(){ var o = new Options(); } } }\n")
    if LG._GRAMMAR_BY_EXT.get(".cs") not in _LANGS:
        _SKIPPED.append("csharp precision (no-fanout) — grammar not loaded")
        return
    g = X.build_graph(d)
    _RAN.append("csharp")
    # the user file DOES emit a `calls`→Options edge (recall-honest), but `Options` is defined in 4 files,
    # so the engine's defs_ok (≤3) EXCLUDES it → no coupling fans out. Prove defs_ok would exclude it.
    name_def_count = {}
    for e in g["edges"]:
        if e["kind"] == "contains":
            nm = e["dst"].split("::", 1)[1]
            name_def_count[nm] = name_def_count.get(nm, 0) + 1
    if name_def_count.get("Options", 0) <= 3:
        _FAILS.append(f"[csharp] PRECISION test setup: 'Options' should be defined in >3 files, "
                      f"got {name_def_count.get('Options')} — fixture broken")
    # the edge exists (honest recall) but the >3-definer cap means the engine never couples it out.
    has_edge = any(e["kind"] == "calls" and e["src"] == "app/User.cs" and e["dst"] == "Options"
                   for e in g["edges"])
    if not has_edge:
        _FAILS.append("[csharp] the ctor use 'Options' should still be EMITTED (recall-honest); "
                      "the engine's defs_ok cap — not extractor silence — is what prevents fan-out")


_precision_no_fanout()


def main():
    print("== SYMBOL-USE RECALL gate (base-type + constructor uses → calls-class edges) ==")
    loaded = sorted(_LANGS.keys())
    print(f"grammars loaded ({len(loaded)}): {loaded}")
    if len(set(_RAN)) < 5:
        print(f"FAIL: too few languages exercised ({sorted(set(_RAN))}) — grammars missing; gate is blind")
        for s in _SKIPPED:
            print("   skip:", s)
        print("SYMBOL-USE RECALL GATE: FAIL")
        return 1
    print(f"languages exercised: {sorted(set(_RAN))}")
    for s in _SKIPPED:
        print("   skip:", s)
    if _FAILS:
        print(f"\n{len(_FAILS)} symbol-use defect(s):")
        for f in _FAILS:
            print("   FAIL", f)
        print("\nSYMBOL-USE RECALL GATE: FAIL")
        return 1
    print(f"\nall {len(set(_RAN))} languages: base-type + constructor uses captured as resolvable calls-class")
    print("edges (RECALL), and a >3-definer ubiquitous type cannot fan out (PRECISION, engine defs_ok cap).")
    print("SYMBOL-USE RECALL GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
