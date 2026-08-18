#!/usr/bin/env python3
"""MULTI-LANGUAGE call/import EDGE PRECISION + RECALL gate (hermetic).

The code graph drives every collision verdict: a FALSE call/import edge = a false warn,
a MISSING one = a silent miss. This gate pins the per-language extraction behaviour the
extractor PROMISES on the known-hard cases — re-exports, aliases, member/navigation calls,
generics, interfaces, package-vs-file imports, keyword-vs-statement node-type collisions —
on small KNOWN-TRUTH fixtures, so a language-layer regression that flips an edge fails CI.

Hermetic: writes tiny source files to a temp dir and reads the RAW edges the language
extractor emits (defs/calls/imports) — no network, no checkout. Skips a language whose
grammar is not installed (never a false RED — the loaded set is asserted to be non-trivial).

Content-free throughout: we assert on edge dst NAMES/PATHS (identifiers, module paths),
never on source bodies.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import _cg_languages as LG  # noqa: E402
from tree_sitter import Parser  # noqa: E402

_LANGS = LG._ts_languages()


def _extract(filename, src):
    """Build the raw (pre-resolution) edge set for ONE source file via the SAME per-file
    extractor build_graph uses. Returns (defs, calls, imports) as sorted name lists, or
    None if the grammar for this extension is not loaded."""
    ext = os.path.splitext(filename)[1]
    label = LG._LABEL_BY_EXT.get(ext)
    gram = LG._GRAMMAR_BY_EXT.get(ext)
    if gram not in _LANGS:
        return None
    parser = Parser(_LANGS[gram])
    d = tempfile.mkdtemp(prefix="mlp-")
    p = os.path.join(d, filename)
    with open(p, "w") as fh:
        fh.write(src)
    if label in ("javascript", "typescript"):
        nodes, edges, _ = LG.extract_file_ts(p, filename, label, parser)
    elif gram == "html":
        nodes, edges, _ = LG.extract_file_html(p, filename, parser)
    else:
        nodes, edges, _ = LG.extract_file_generic(p, filename, label, parser, LG._GENERIC_SPEC[label])
    defs = sorted(n["name"] for n in nodes if n.get("kind") in ("def", "class"))
    calls = sorted(e["dst"] for e in edges if e["kind"] == "calls")
    imports = sorted(e["dst"] for e in edges if e["kind"] == "imports")
    return defs, calls, imports


_FAILS = []
_RAN = []
_SKIPPED = []


def case(lang, filename, src, must_calls=(), must_imports=(), must_defs=(),
         forbid_calls=(), forbid_imports=()):
    """Assert presence (recall) and absence (precision) of specific edges for one fixture."""
    out = _extract(filename, src)
    if out is None:
        _SKIPPED.append(f"{lang} ({filename}) — grammar not loaded")
        return
    defs, calls, imports = out
    _RAN.append(lang)
    for c in must_calls:
        if c not in calls:
            _FAILS.append(f"[{lang}] RECALL: missing call edge {c!r}; got calls={calls}")
    for c in must_imports:
        if c not in imports:
            _FAILS.append(f"[{lang}] RECALL: missing import edge {c!r}; got imports={imports}")
    for c in must_defs:
        if c not in defs:
            _FAILS.append(f"[{lang}] RECALL: missing def {c!r}; got defs={defs}")
    for c in forbid_calls:
        if c in calls:
            _FAILS.append(f"[{lang}] PRECISION: FALSE call edge {c!r}; got calls={calls}")
    for c in forbid_imports:
        if c in imports:
            _FAILS.append(f"[{lang}] PRECISION: FALSE import edge {c!r}; got imports={imports}")


# ── Go: full-path package imports (precision: a local import is the full module path; the
#    resolver matches the dir suffix), method + bare calls. ───────────────────────────────
case("go", "main.go", '''
package main
import (
    "fmt"
    "github.com/org/repo/internal/auth"
)
func Run() { fmt.Println("x"); auth.Login(); helper() }
func helper() {}
''',
     must_calls=["Println", "Login", "helper"],
     must_imports=["fmt", "github.com/org/repo/internal/auth"])

# ── Java: FQN imports (NOT a bare class name — the prior real-repo fix), member + static calls. ─
case("java", "App.java", '''
package com.x;
import java.util.List;
import com.google.gson.Gson;
public interface Handler { void handle(); }
public class App<T extends Comparable<T>> implements Handler {
    public void handle() { Gson g = new Gson(); g.toJson(this); helper(); }
    private void helper() {}
}
''',
     must_calls=["toJson", "helper"],
     must_imports=["java.util.List", "com.google.gson.Gson"],
     must_defs=["App", "Handler"],
     forbid_imports=["List", "Gson"])   # bare class names would fan out to every same-named file

# ── Kotlin: this case asserts the symbol/edge behaviour a HEALTHY kotlin grammar must produce
#    (imports yield the symbol, never the literal `import` keyword leaf; member calls h.process()
#    are captured). NOTE: with the currently-pinned tree-sitter-kotlin 1.0.0 the structural-health
#    probe in _ts_languages REJECTS the grammar (it returns has_error on every valid .kt), so the
#    grammar is NOT loaded and this case is SKIPPED here — kotlin then degrades to NODE-ONLY at the
#    build_graph level (the recall-safe FINDING-1 fix, asserted in tests/test_extractor.py). If a
#    working grammar is ever pinned it passes the probe, loads, and this case runs again. ─────────
case("kotlin", "App.kt", '''
package com.x
import android.os.Bundle
import com.example.util.Helper
interface Handler { fun handle() }
class App<T : Comparable<T>> : Handler {
    override fun handle() { val h = Helper(); h.process(); g.a.b.deep(); println("x"); local() }
    private fun local() {}
}
''',
     must_calls=["process", "deep", "println", "local"],
     must_imports=["android.os.Bundle", "com.example.util.Helper"],   # FULL dotted path (import_dotted), not bare last segment
     must_defs=["App", "Handler"],
     forbid_imports=["import", "Bundle", "Helper"])   # keyword leaf never an edge; bare last segment would fan out

# ── Swift: protocols are first-class type defs; member calls (h.process()) MUST be captured; a
#    SUBMODULE import (import UIKit.UIView) keeps the FULL dotted path, not the bare submodule. ─
case("swift", "App.swift", '''
import Foundation
import UIKit.UIView
protocol Handler { func handle() }
class App<T: Comparable>: Handler {
    func handle() { let h = Helper(); h.process(); print("x"); local() }
    private func local() {}
}
''',
     must_calls=["process", "print", "local"],
     must_imports=["Foundation", "UIKit.UIView"],   # plain module + FULL dotted submodule (import_dotted)
     must_defs=["Handler", "App"],      # protocol_declaration must produce a def
     forbid_imports=["UIView"])         # bare submodule would fan out to every same-named file

# ── C#: `using` imports capture the FULL dotted path, NOT just the last segment (FINDING 2): a
#    bare `AuthService` basename-fans-out to every same-named file; the full `MyApp.Services.
#    AuthService` resolves to the one file it names. Generic class, member + local calls. ───────
case("csharp", "App.cs", '''
using System;
using MyApp.Services.AuthService;
namespace MyApp {
  interface IHandler { void Handle(); }
  class App<T> : IHandler where T : class {
    public void Handle() { var l = new System.Collections.Generic.List<int>(); l.Add(1); Local(); }
    void Local() {}
  }
}
''',
     must_calls=["Add", "Local"],
     must_defs=["App", "IHandler"],
     must_imports=["MyApp.Services.AuthService"],
     forbid_imports=["AuthService"])     # bare last segment would fan out to every same-named file

# ── Rust: `use` imports capture the FULL `::` path, NOT just the last segment (FINDING 2): bare
#    `HashMap` fans out; `std::collections::HashMap` names the one module. Trait, impl, calls. ──
case("rust", "lib.rs", '''
use std::collections::HashMap;
trait Greet { fn greet(&self) -> String; }
struct App;
impl Greet for App {
    fn greet(&self) -> String { let m: HashMap<String,i32> = HashMap::new(); m.len(); helper(); String::from("x") }
}
fn helper() {}
''',
     must_calls=["new", "len", "helper", "from"],
     must_defs=["App", "Greet"],
     must_imports=["std::collections::HashMap"],
     forbid_imports=["HashMap"])         # bare last segment would fan out to every same-named file

# ── PHP: `use` imports capture the FULL `\` path, NOT just the last segment (FINDING 2): bare
#    `Mailer` fans out; `App\Service\Mailer` names the one file. The `as M` alias is EXCLUDED.
#    Static + member + local calls. ───────────────────────────────────────────────────────────
case("php", "App.php", '''<?php
namespace App;
use App\\Service\\Mailer;
interface Sender { public function send(); }
class App implements Sender {
    public function send() { $m = new Mailer(); $m->deliver(); Helper::format(); $this->local(); }
    private function local() {}
}
''',
     must_calls=["deliver", "format", "local"],
     must_defs=["App", "Sender"],
     must_imports=["App\\Service\\Mailer"],
     forbid_imports=["Mailer"])          # bare last segment would fan out to every same-named file

# ── TypeScript: RE-EXPORTS (export * / export {x as y} from) couple like imports; a LOCAL
#    export (no `from`) must NOT; aliased import + member + arrow callees. ────────────────
case("typescript", "index.ts", '''
export * from "./foo";
export { bar as renamedBar } from "./bar";
export { localOnly };
import { thing as t } from "./thing";
import defaultExp from "./def";
const localOnly = 1;
const handler = () => { t(); defaultExp(); obj.method(); };
''',
     must_imports=["./foo", "./bar", "./thing", "./def"],
     must_calls=["t", "defaultExp", "method"])

# ── C++: <system> include vs "local" include; namespace + member + local calls. ─────────
case("cpp", "main.cpp", '''
#include <vector>
#include "auth.h"
namespace app {
class Service {
public:
    void run() { std::vector<int> v; v.push_back(1); login(); local(); }
    void local() {}
};
}
''',
     must_calls=["push_back", "login", "local"],
     must_imports=["<vector>", "auth.h"])

# ── Rust GROUP import `use a::b::{X, Y, c::D}` → ONE full-path edge per item (prefix-joined),
#    not a single edge collapsed to the prefix (which dropped the items entirely). ─────────────
case("rust", "group.rs", '''
use std::collections::{HashMap, BTreeMap};
use crate::value::{Map, ser::Serializer};
fn f() { let _: HashMap<String, i32> = HashMap::new(); }
''',
     must_imports=["std::collections::HashMap", "std::collections::BTreeMap",
                   "crate::value::Map", "crate::value::ser::Serializer"],
     forbid_imports=["std::collections", "HashMap", "BTreeMap", "Map", "Serializer"])  # neither prefix-only nor bare item

# ── Kotlin DUPLICATE-BASENAME import: two imports whose LAST segment is identical (Response) must
#    keep their FULL distinct paths, so they resolve to two different files (no basename collapse). ─
case("kotlin", "Dup.kt", '''
package com.x
import okhttp3.Response
import retrofit2.Response
fun f() {}
''',
     must_imports=["okhttp3.Response", "retrofit2.Response"],
     forbid_imports=["Response"])        # bare last segment would collapse both to ONE basename fan-out

# ── PHP GROUPED `use A\\B\\{X, Y, Z}` → ONE full-path edge per clause (prefix-joined), not just the
#    last symbol (the prior trailing-ident kept only `Z`). ─────────────────────────────────────────
case("php", "Group.php", '''<?php
namespace App;
use App\\Service\\{Mailer, Logger, Cache};
use App\\Models\\User;
class C { function f() { new Mailer(); } }
''',
     must_imports=["App\\Service\\Mailer", "App\\Service\\Logger", "App\\Service\\Cache",
                   "App\\Models\\User"],
     forbid_imports=["Mailer", "Logger", "Cache", "User", "App\\Service"])  # no bare item, no prefix-only

# ── TypeScript DYNAMIC import(): `import("./mod")` is a call_expression whose callee node TYPE is
#    `import` (not an identifier) — it MUST emit an imports edge like a static import / require. ────
case("typescript", "dyn.ts", '''
async function load() {
  const m = await import("./lazy");
  const n = await import("./other");
  return m.default;
}
''',
     must_imports=["./lazy", "./other"])


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# RESOLUTION-LEVEL cases: build a tiny MULTI-FILE repo and run the FULL build_graph (extract +
# _resolve_imports), then assert the resolved file→file `imports` edges. These prove the resolver
# (C# namespace→directory, Rust group/relative, PHP grouped, TS dynamic) links the ONE correct file —
# no fan-out, no src→test — which the per-file `case()` above cannot exercise.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
import code_graph_extract as X  # noqa: E402


def resolve_case(lang, files, src, want_edges=(), forbid_edges=()):
    """Write `files` (rel-path -> content) under a temp repo, run build_graph, and assert the resolved
    file→file imports of `src`. want/forbid are (src, dst) FILE-PATH pairs. Skips if grammar absent."""
    ext = os.path.splitext(src)[1]
    if LG._GRAMMAR_BY_EXT.get(ext) not in _LANGS:
        _SKIPPED.append(f"{lang} resolve ({src}) — grammar not loaded")
        return
    d = tempfile.mkdtemp(prefix="mlr-")
    for rel, content in files.items():
        fp = os.path.join(d, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w") as fh:
            fh.write(content)
    g = X.build_graph(d)
    _RAN.append(lang)
    resolved = {(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == "imports"}
    for pair in want_edges:
        if pair not in resolved:
            got = sorted(dst for s, dst in resolved if s == pair[0])
            _FAILS.append(f"[{lang}] RESOLVE RECALL: missing {pair}; {pair[0]} resolved to {got}")
    for pair in forbid_edges:
        if pair in resolved:
            _FAILS.append(f"[{lang}] RESOLVE PRECISION: FALSE edge {pair} (fan-out / src→test)")


# C# namespace→DIRECTORY: `using App.Services` names a namespace = the directory of .cs files. The
# bare last segment must NOT basename-fan-out to an unrelated same-named file (here a TEST copy). ──
resolve_case("csharp",
             {"App/Services/AuthService.cs": "namespace App.Services { class AuthService {} }\n",
              "App/Services/UserService.cs": "namespace App.Services { class UserService {} }\n",
              "tests/AuthService.cs": "namespace App.Tests { class AuthService {} }\n",
              "App/Program.cs": "using App.Services;\nnamespace App { class Program {} }\n"},
             "App/Program.cs",
             want_edges=[("App/Program.cs", "App/Services/AuthService.cs"),
                         ("App/Program.cs", "App/Services/UserService.cs")],
             forbid_edges=[("App/Program.cs", "tests/AuthService.cs")])   # never the test copy (src→test)

# Rust GROUP `use crate::a::{X, Y}` resolves EACH item; a relative `use super::sib` resolves to the
# SIBLING module file (not a same-named file under tests/). ───────────────────────────────────────
resolve_case("rust",
             {"src/lib.rs": "pub mod a;\npub mod consumer;\n",
              "src/a.rs": "pub mod x;\npub mod y;\n",
              "src/a/x.rs": "pub struct X;\n",
              "src/a/y.rs": "pub struct Y;\n",
              "src/sib.rs": "pub struct Sib;\n",
              "tests/sib.rs": "pub struct Sib;\n",
              "src/consumer.rs": "use crate::a::{x, y};\nuse super::sib;\nfn f() {}\n"},
             "src/consumer.rs",
             want_edges=[("src/consumer.rs", "src/a/x.rs"),
                         ("src/consumer.rs", "src/a/y.rs"),
                         ("src/consumer.rs", "src/sib.rs")],
             forbid_edges=[("src/consumer.rs", "tests/sib.rs")])   # super:: must be dir-relative, not basename fan-out

# PHP grouped `use A\\B\\{X, Y}` resolves EACH clause to its own file (PSR-4 dir). ─────────────────
resolve_case("php",
             {"src/Service/Mailer.php": "<?php\nnamespace App\\Service;\nclass Mailer {}\n",
              "src/Service/Logger.php": "<?php\nnamespace App\\Service;\nclass Logger {}\n",
              "src/App.php": "<?php\nnamespace App;\nuse App\\Service\\{Mailer, Logger};\nclass App {}\n"},
             "src/App.php",
             want_edges=[("src/App.php", "src/Service/Mailer.php"),
                         ("src/App.php", "src/Service/Logger.php")])

# TS DYNAMIC import() resolves to the lazy-loaded module FILE (couples like a static import). ──────
resolve_case("typescript",
             {"src/lazy.ts": "export default 1;\n",
              "src/main.ts": "async function f() { return import('./lazy'); }\n"},
             "src/main.ts",
             want_edges=[("src/main.ts", "src/lazy.ts")])


def main():
    print("== MULTI-LANGUAGE EDGE PRECISION/RECALL gate ==")
    loaded = sorted(_LANGS.keys())
    print(f"grammars loaded ({len(loaded)}): {loaded}")
    # Guard against a silently-empty run: the gate is meaningless if no grammar is present.
    if len(set(_RAN)) < 5:
        print(f"FAIL: too few languages exercised ({sorted(set(_RAN))}) — grammars missing; gate is blind")
        for s in _SKIPPED:
            print("   skip:", s)
        print("MULTI-LANGUAGE EDGE PRECISION GATE: FAIL")
        return 1
    print(f"languages exercised: {sorted(set(_RAN))}")
    for s in _SKIPPED:
        print("   skip:", s)
    if _FAILS:
        print(f"\n{len(_FAILS)} edge defect(s):")
        for f in _FAILS:
            print("   FAIL", f)
        print("\nMULTI-LANGUAGE EDGE PRECISION GATE: FAIL")
        return 1
    print(f"\nall {len(set(_RAN))} languages: call/import edges correct on the known-hard cases")
    print("MULTI-LANGUAGE EDGE PRECISION GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
