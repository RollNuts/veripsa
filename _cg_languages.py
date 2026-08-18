"""tree-sitter language machinery for the code-graph extractor.

Owns: grammar/label maps, the _ts_languages() loader, the generic and
TypeScript/HTML extractors, the small text helpers they share, and
the C/C++ declarator walker.
"""

# Non-Python source files are parsed with tree-sitter.
# Map file extension -> (grammar name, language label).
# .tsx uses the tsx grammar but is labelled 'typescript' so coverage reads
# cleanly (py/js/ts).
_GRAMMAR_BY_EXT = {".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
                   ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx",
                   ".go": "go", ".java": "java", ".rb": "ruby", ".php": "php", ".cs": "csharp",
                   ".html": "html", ".htm": "html", ".rs": "rust",
                   ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
                   ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
                   ".kt": "kotlin", ".kts": "kotlin", ".swift": "swift",
                   ".ex": "elixir", ".exs": "elixir",
                   ".dart": "dart"}
_LABEL_BY_EXT = {".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
                 ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
                 ".go": "go", ".java": "java", ".rb": "ruby", ".php": "php", ".cs": "csharp",
                 ".html": "html", ".htm": "html", ".rs": "rust",
                 ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
                 ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
                 ".kt": "kotlin", ".kts": "kotlin", ".swift": "swift",
                 ".ex": "elixir", ".exs": "elixir",
                 ".dart": "dart",
                 # Bespoke web extractors (no direct grammar dispatch).  Keeping these
                 # labels here also makes line-capped files degrade to an honest language
                 # instead of ``unknown``.
                 ".astro": "astro", ".svelte": "svelte", ".vue": "vue",
                 ".css": "css", ".scss": "css", ".sass": "css",
                 ".less": "css", ".styl": "css"}

# Node type sets used by the TypeScript bespoke extractor.
_FUNC_DEF_TYPES = {"function_declaration", "generator_function_declaration",
                   "function_signature", "abstract_method_signature"}
# A TYPE-LEVEL declaration is a first-class symbol too: an `interface`/`type`/`enum` is exactly
# the kind of unit a PR edits in isolation, and finer-collision needs its [start,end] span to
# refine a file-level overlap to the symbol. Omitting interface_declaration/type_alias_declaration/
# enum_declaration meant a PR touching ONLY a TS interface body produced no symbol node → the
# collision degraded to file-level (recall-safe but imprecise, and INCONSISTENT with Java/C#/Rust/
# Swift, all of which mint interface/enum nodes). Node-type names verified against the typescript
# grammar (an `interface_body`/`enum_body` is a DIFFERENT type and is intentionally NOT here, so an
# enum MEMBER never becomes a top-level symbol).
_CLASS_TYPES = {"class_declaration", "abstract_class_declaration",
                "interface_declaration", "type_alias_declaration", "enum_declaration"}
_FUNC_VALUE_TYPES = {"arrow_function", "function_expression", "function", "generator_function"}

# Spec-driven extraction for additional tree-sitter languages. Adding a language = ONE entry here
# (node-type sets + how to read a call/import) — NOT new code. Node types are VERIFIED by
# introspecting each grammar (not guessed). JS/TS keep their bespoke extractor above (richer
# arrow-fn/require handling).
# SYMBOL-USE RECALL (measure-first, PR-this-lane). A file F can DEPEND ON G with NO `calls` edge: it
# EXTENDS/IMPLEMENTS a base type G defines, or CONSTRUCTS one of G's types (`new Foo()` / a composite
# literal / a struct expression). The call graph misses these (measured on real C# repos: shadowsocks
# +13.9 pts recall, jellyfin +89 strong-co-change pairs the call graph could not see — exactly PR #253's
# finding that C#/Go wire via constructor/inheritance, not calls). We capture these uses as `calls`-class
# edges (dst = the USED TYPE NAME — content-free), so they flow through the SAME `_claim_adjacency`
# resolution every call edge already passes: defs_ok≤3 fan-out cap, import-confirmation, single-definer,
# hub dampening, prod→test guard. That existing machinery is what keeps a ubiquitous type name (`String`,
# `Context`, `Options`) from fanning out to a decoy — so we never resolve a use ourselves; we hand the
# engine a `calls` edge and let its proven precision guards decide.
#
# PRECISION-SCOPED to TWO kinds, deliberately NOT broad type-annotation capture:
#   • base — a class's superclass / interfaces / Swift conformances / Rust trait impl / C++ base list.
#            A base type is a SPECIFIC, rarely-ubiquitous name → high-precision (measured 87–94% of the
#            added C# pairs co-change).
#   • ctor — `new Foo()` / a Go composite literal `Foo{}` / a Rust struct expression `Foo{}`. The dominant
#            cross-file dependency in C# (the #253 signal).
# We EXCLUDE parameter / field / local TYPE ANNOTATIONS on purpose: measured catastrophically noisy
# (ubiquitous type names, e.g. core: 98 false param pairs / 0 recall) for ~zero recall recovery — a decoy
# factory, the inverse of the win. Each language's `sym_use` names the node types that carry a base list
# and a constructor; absent `sym_use` = no symbol-use edges for that language (no behavior change).
_GENERIC_SPEC = {
    "go": {"def": {"function_declaration", "method_declaration"}, "class": {"type_spec"},
           "call": {"call_expression"}, "call_field": "function",
           "import_node": {"import_spec"}, "import_field": "path",
           # Go has no inheritance; a value is constructed by a COMPOSITE LITERAL `Foo{}` whose `type`
           # field is a type_identifier (`Foo`) or qualified_type (`pkg.Foo`). No `base`.
           # EMBED (recall, measure-first, this lane): Go's reuse/"inheritance" mechanism is STRUCT
           # EMBEDDING — `type Derived struct { Base }` embeds Base, promoting its methods/fields. This
           # is a real cross-file dependency (edit Base, Derived changes) with NO `calls` edge (methods
           # are PROMOTED, never explicitly called) and, for a SAME-PACKAGE embed, NO import either — so
           # it was SILENTLY MISSED (measured on a crafted same-pkg repo: `struct { Base }` emitted zero
           # edges, while the sibling composite-literal `Base{}` correctly emitted `calls -> Base`). This
           # is the exact #253-class hole (Go wires via composition, not calls). An embedded field is a
           # `field_declaration` carrying ONLY a type (NO `field_identifier`) — distinct from a NAMED
           # field `x T` (which IS a noisy type ANNOTATION the spec deliberately excludes). The embed
           # handler (see extract_file_generic) emits the embedded type NAME as a content-free `calls`
           # edge, guarded by the no-field-identifier rule, so only the specific embedded base name flows
           # through the resolver's precision guards — never a field's annotation type.
           "embed": {"field_declaration"},
           "sym_use": {"ctor": {"composite_literal"}}},
    # Java: the import is a FULLY-QUALIFIED dotted name (`import a.b.C;`). Read the WHOLE dotted
    # path (import_dotted), NOT just the last segment — a bare class name (`Type`, `List`) basename-
    # fans-out to every same-named file in the repo (real-repo audit on retrofit: `import
    # java.lang.reflect.Type` falsely resolved to test `…/jaxb/Type.java`; 106 src→test false edges).
    # The full path `java.lang.reflect.Type` → java/lang/reflect/Type suffix-matches NO local file
    # (correct: it's the JDK), while a local `retrofit2.Call` → retrofit2/Call resolves to the file.
    "java": {"def": {"method_declaration", "constructor_declaration"},
             "class": {"class_declaration", "interface_declaration", "enum_declaration"},
             "call": {"method_invocation"}, "call_field": "name",
             "import_node": {"import_declaration"}, "import_field": None, "import_dotted": True,
             # superclass = `extends B`, super_interfaces = `implements C, D`; object_creation_expression
             # is `new E()`. Base/ctor type names live as type_identifier descendants.
             "sym_use": {"base": {"superclass", "super_interfaces"},
                         "ctor": {"object_creation_expression"}}},
    # MIXIN (recall, measure-first, this lane): Ruby's primary cross-file coupling is the MIXIN —
    # `include M` / `extend M` / `prepend M` pulls module M's methods into a class. This is the Rails
    # "concern" idiom and the Ruby analogue of `implements`/base. It parses as a `call`/`command` whose
    # `method` is `include`/`extend`/`prepend` and whose ARGUMENT is the module CONSTANT (`Walkable`,
    # `Foo::Bar`) — so the generic callee path emitted `calls -> include` (a UBIQUITOUS hub name, every
    # class includes something → hub-dampened to nothing) and SILENTLY DROPPED the module name, the real
    # coupling target. Measured on a crafted autoload-style repo (`class Dog; include Walkable` with NO
    # require, the dominant Rails/Zeitwerk shape): the only edges were `contains` + `calls -> include` —
    # the Dog↔Walkable dependency was completely invisible. FIX: for these methods, emit a `calls` edge
    # to the constant ARGUMENT(S) (`_trailing_ident` → bare `Walkable`/`Bar`) instead of the useless
    # `include` callee — the SAME special-casing `require`/`require_relative` already get (they emit an
    # import, not `calls -> require`). Content-free (a module name, never a body); the bare name flows
    # through the resolver's precision guards (defs<=3 cap, single-definer, hub-dampening), so a
    # multi-definer/external module is DROPPED, never fanned out.
    "ruby": {"def": {"method", "singleton_method"}, "class": {"class", "module"},
             "call": {"call", "command"}, "call_field": "method",
             "require_methods": {"require", "require_relative"},
             "mixin_methods": {"include", "extend", "prepend"}},
    # PHP `use App\Models\User;`, Rust `use crate::auth::login;`, C# `using MyApp.Services.AuthService;`
    # are all FULLY-QUALIFIED paths — read the WHOLE path (import_dotted), NOT just the last segment.
    # The bare last segment (`User`/`login`/`AuthService`) basename-fans-out to EVERY same-named file in
    # the repo (the exact bug the Java path was fixed for above): `use App\Models\User` falsely coupled to
    # `lib/User.php`, `use crate::auth::login` to `tests/login.rs`. _dotted_import reads the qualified-name
    # node's text (PHP `\`, Rust `::`, C#/Java `.`), and _cg_resolve normalizes `\`/`::`→`/` (+ strips a
    # Rust `crate`/`self`/`super` root) so the full path suffix-matches the ONE file it names. Recall-safe:
    # if a full path resolves to no file (external pkg / odd grouped-use), the edge stays as-is (inert),
    # never a silent miss.
    "php": {"def": {"function_definition", "method_declaration"},
            "class": {"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"},
            "call": {"function_call_expression", "member_call_expression", "scoped_call_expression"},
            "call_field": ("function", "name"),     # function_call uses 'function'; member/scoped use 'name'
            "import_node": {"namespace_use_declaration"}, "import_field": None, "import_dotted": True,
            # base_clause = `extends B`, class_interface_clause = `implements C`; the base name is a `name`
            # (or qualified_name) leaf. object_creation_expression is `new D()`.
            "sym_use": {"base": {"base_clause", "class_interface_clause"},
                        "ctor": {"object_creation_expression"}}},
    "csharp": {"def": {"method_declaration", "constructor_declaration", "local_function_statement"},
               "class": {"class_declaration", "interface_declaration", "struct_declaration",
                          "record_declaration", "enum_declaration"},
               "call": {"invocation_expression"}, "call_field": ("function",),
               "import_node": {"using_directive"}, "import_field": None, "import_dotted": True,
               # base_list = `: Base, IHandler` (identifier / qualified_name / generic_name children);
               # object_creation_expression is `new Foo()` (the `type` field). C# is the measured win:
               # types are wired via `new`/inheritance, NOT calls — this is what the call graph missed.
               "sym_use": {"base": {"base_list"}, "ctor": {"object_creation_expression"}}},
    "rust": {"def": {"function_item"}, "class": {"struct_item", "enum_item", "trait_item"},
             "call": {"call_expression"}, "call_field": ("function",),
             "import_node": {"use_declaration"}, "import_field": None, "import_dotted": True,
             # impl_item = `impl Trait for Type` / `impl Type` — both type names are type_identifier
             # descendants (couples the impl file to the trait AND the type it implements for).
             # struct_expression is `Foo { .. }` (the value-construction equivalent of `new`).
             "sym_use": {"base": {"impl_item"}, "ctor": {"struct_expression"}}},
    # C/C++: the function NAME nests inside the declarator (no 'name' field) → name_via_declarator.
    # #include is the import (preproc_include, field 'path'); a local "util.h" resolves like any
    # bare import (basename).
    "c": {"def": {"function_definition"}, "class": {"struct_specifier", "enum_specifier", "union_specifier"},
          "call": {"call_expression"}, "call_field": ("function",),
          "import_node": {"preproc_include"}, "import_field": "path", "name_via_declarator": True},
    "cpp": {"def": {"function_definition"},
            "class": {"class_specifier", "struct_specifier", "enum_specifier", "union_specifier"},
            "call": {"call_expression"}, "call_field": ("function",),
            "import_node": {"preproc_include"}, "import_field": "path", "name_via_declarator": True,
            # base_class_clause = `: public Base` (type_identifier descendants); new_expression is
            # `new Derived()` (the constructed type). (C has neither — no sym_use entry above.)
            "sym_use": {"base": {"base_class_clause"}, "ctor": {"new_expression"}}},
    # Kotlin: function_declaration/.../object_declaration all carry a `name` field (identifier).
    # call_expression has no named callee field — the identifier/navigation_expression is the first
    # child, handled by the fallback in extract_file_generic (_trailing_ident on the first
    # identifier-ish child).  imports: node type is "import" (the keyword node wraps the qualified
    # path). The import target is a FULLY-QUALIFIED dotted name (`import android.os.Bundle`) parsed as a
    # `qualified_identifier` — read the WHOLE path (import_dotted), NOT just the last segment, so a bare
    # `Bundle`/`Helper` does not basename-fan-out to every same-named file (the Java/C#/Rust/PHP fix).
    # VERIFIED on okhttp: emitting the full `okhttp3.mockwebserver.MockWebServer` collapsed a 2-file
    # fan-out to the ONE file. (The `child_count > 0` guard below still drops the bare `import` keyword
    # leaf — _dotted_imports returns [] for it, so no false edge.)
    "kotlin": {"def": {"function_declaration"},
               "class": {"class_declaration", "object_declaration", "interface_declaration"},
               "call": {"call_expression"}, "call_field": None,
               "import_node": {"import"}, "import_field": None, "import_dotted": True},
    # Swift: function_declaration for funcs; class_declaration covers class/struct/enum (one node
    # type; the `class`/`struct`/`enum` keyword distinguishes them). protocol_declaration is a
    # SEPARATE node (verified by grammar introspection) and carries the same `name` field — it's a
    # first-class type def (Swift's interface), so include it like Java/C# interfaces and Rust traits;
    # omitting it silently dropped every protocol from the graph. call_expression callee is the first
    # child (simple_identifier or navigation_expression — no named field), handled by the fallback.
    # imports: `import_declaration` wraps an `identifier` that may be a dotted SUBMODULE path
    # (`import UIKit.UIView`, `import struct Foundation.Date`) — read the WHOLE path (import_dotted), NOT
    # just `UIView`, so a submodule import does not basename-fan-out (consistent with the other dotted
    # languages). A plain `import Foundation` is a single segment (correctly external, resolves to no
    # local file). The kind keyword (`struct`/`class`/`func`) is excluded — _dotted_imports reads the
    # `identifier` node, not the clause text.
    "swift": {"def": {"function_declaration"},
              "class": {"class_declaration", "protocol_declaration"},
              "call": {"call_expression"}, "call_field": None,
              "import_node": {"import_declaration"}, "import_field": None, "import_dotted": True,
              # inheritance_specifier = a `: Base`/`: Protocol` conformance (a user_type → type_identifier).
              # Swift construction `Widget()` is already a call_expression (no `new` keyword) → captured by
              # the call path; so NO ctor entry here (it would double-count).
              "sym_use": {"base": {"inheritance_specifier"}}},
    # Dart (Flutter). Loaded via tree-sitter-language-pack (ABI v14, compatible with tree-sitter 0.23.x).
    # grammar is obtained via tree_sitter_language_pack.get_language('dart'); see _ts_languages().
    #
    # SYMBOL NODES:
    #   class  — class_definition (has `name` field; covers class/abstract class), enum_declaration
    #            (has `name` field), extension_declaration (has `name` field: `extension Foo on T { }`).
    #            mixin_declaration has NO `name` field in this grammar version → mixin nodes are
    #            silently skipped (recall gap, not a false edge; file-level fallback still protects).
    #   def    — function_signature (has `name` field). Matches BOTH top-level functions and class
    #            methods (method_signature wraps function_signature; _GENERIC_SPEC walks the whole tree
    #            so function_signature is matched wherever it appears).
    #
    # CALL EDGES: omitted (v1). Dart's call AST is `identifier + selector(argument_part)` chains —
    #   no standalone `call_expression` node with a callee `function` field. The _GENERIC_SPEC call
    #   model cannot capture this shape without a bespoke extractor (like Elixir's). Deferring to v2.
    #
    # IMPORT EDGES: omitted (v1). `import 'path'` parses as import_specification → configurable_uri →
    #   uri → string_literal, with NO named child fields at any level. The _GENERIC_SPEC import model
    #   (impfield / dotted path / _trailing_ident fallback) cannot strip the surrounding quotes and
    #   produce a clean path. Deferring to v2 alongside a bespoke extractor for call edges.
    #
    # SYM_USE (base): `superclass` = `extends BaseClass` clause. The type_identifier child is the base
    #   type name (in _TYPE_NAME_NODES). Emitted as a `calls`-class edge so the engine's existing
    #   precision guards (hub-dampening, import-confirmation, defs_ok cap) apply. A plain `extends` in
    #   Flutter (e.g. `extends StatelessWidget`) is the primary cross-file dependency not captured by
    #   call edges — this fills the most important gap.
    "dart": {"def": {"function_signature"},
             "class": {"class_definition", "enum_declaration", "extension_declaration"},
             "sym_use": {"base": {"superclass"}}},
}

# SYMBOL-USE: descendant node types whose trailing identifier is a TYPE NAME inside a base-list / ctor
# node. A base list wraps the name in language-specific carriers (Swift `user_type`, generic wrappers,
# qualified names) — we collect the trailing identifier of each, so `pkg.Base`/`Base<T>` → `Base`.
_TYPE_NAME_NODES = ("type_identifier", "identifier", "name", "qualified_name", "qualified_type",
                    "scoped_type_identifier", "generic_name", "generic_type", "user_type",
                    "constrained_type", "simple_identifier")


def _sym_use_names(node, kind):
    """Type NAME(s) used by a base-list or constructor `node`. `kind` is 'base' or 'ctor'.

    For a CTOR node the constructed type is the node's `type`/`constructor`/`name` field (or, lacking a
    field, the first type-ish child) — the ONE type being built. For a BASE node every type-name
    descendant is a base/interface/conformance/impl target (a class can extend one + implement several;
    a Rust `impl Trait for Type` couples to BOTH). We read the TRAILING identifier of each carrier
    (`_trailing_ident`), so a qualified/generic base (`a.b.Base`, `Base<T>`) yields the bare `Base` — the
    same content-free symbol-name shape a `calls` dst already carries. Returns a de-duplicated list of
    names (may be empty: an anonymous/array/tuple construction names no single defined type → no edge)."""
    if node is None:
        return []
    out, seen = [], set()

    def take(n):
        nm = _trailing_ident(n)
        if nm and nm not in seen and nm.isidentifier():   # a real identifier, never an operator/array shape
            seen.add(nm)
            out.append(nm)

    if kind == "ctor":
        # the ONE constructed type: a named field if the grammar exposes it, else the first type-ish child
        # (NOT a descendant walk — that would pull constructor ARGUMENTS in as false type uses).
        tgt = (node.child_by_field_name("type") or node.child_by_field_name("constructor")
               or node.child_by_field_name("name"))
        if tgt is None:
            tgt = next((c for c in node.children if c.type in _TYPE_NAME_NODES), None)
        if tgt is not None:
            take(tgt)
        return out
    # BASE: every type-name carrier under the base/impl/inheritance node (a class implements several).
    # Do NOT descend into a BODY (`declaration_list`/`field_declaration_list`/block) — a Rust `impl ... {
    # ... }` carries its method bodies inline, and walking them would pull every type used INSIDE the impl
    # in as a false base. The base/interface names sit BEFORE the body, so stopping at the body node keeps
    # only the genuine base targets (the trait + the type it is implemented for).
    _BODY = ("declaration_list", "field_declaration_list", "class_body", "block",
             "enum_class_body", "interface_body", "compound_statement", "field_initializer_list")
    stack = [node]
    while stack:
        n = stack.pop()
        if n is not node and n.type in _TYPE_NAME_NODES:
            take(n)
            continue                  # don't descend a matched carrier (a generic `Base<T>` → `Base`, not `T`)
        if n is not node and n.type in _BODY:
            continue                  # an impl/class BODY is not where base names live → skip it
        stack.extend(n.children)
    return out


# ── GRAMMAR HEALTH PROBE (recall-safe gate against a grammar that LOOKS supported but silently
#    mis-parses) ────────────────────────────────────────────────────────────────────────────────
# Some pinned grammars parse a trivially-valid file with root_node.has_error == True and then salvage
# symbols INCONSISTENTLY via error recovery — an AUTHORITATIVE-looking graph that silently drops
# symbols (the worst outcome: a missed collision read as "clear"). Concretely, tree-sitter-kotlin
# 1.0.0 returns has_error == True on EVERY real .kt file, even `class C { val x = 1 }`. So before we
# trust a grammar for structural extraction, we PROBE it on a minimal class-with-member sample and
# require (a) has_error == False AND (b) it actually yields the class + method nodes the spec names.
# A grammar that fails the probe is DROPPED from the loaded set → build_graph routes its files to the
# node-only path (bare file node, no structural edges = recall-safe, honest), exactly like an
# unsupported language. If a WORKING grammar is ever pinned, it passes the probe and full extraction
# auto-enables — no other code change. Only languages with a known mis-parse risk are probed (keyed by
# grammar name); every other grammar loads as before (no probe overhead, no behavior change).
_PROBE = {
    # grammar_name: (sample_source, expected_class_name, expected_def_name)
    # NOTE: tree-sitter-kotlin 1.0.x returns has_error==True for the inline single-line form
    # `class C { fun m() {} }` — the grammar parses a class body with a member function correctly
    # ONLY when the body spans multiple lines OR the function carries an explicit return type
    # annotation. The multiline form `class C {\n    fun m(): Unit {}\n}` is structurally identical
    # (same node types, same name fields) and parses with has_error==False — verified by grammar
    # introspection. The probe exercises the exact node types the spec names (class_declaration with
    # name field + function_declaration with name field), so the health check is preserved.
    "kotlin": ("class C {\n    fun m(): Unit {}\n}", "C", "m"),
}


def _grammar_passes_probe(name, lang, spec):
    """True if grammar `name` parses its probe sample CLEANLY (no error) AND yields the expected
    class + def symbols. Languages with no probe entry always pass (no behavior change). Any failure
    to parse/import is treated as NOT passing (recall-safe: fall back to node-only)."""
    sample = _PROBE.get(name)
    if sample is None:
        return True
    src, want_class, want_def = sample
    if not spec:
        return False
    try:
        from tree_sitter import Parser
        tree = Parser(lang).parse(src.encode("utf-8"))
    except Exception:
        return False
    if tree.root_node.has_error:                 # the kotlin-1.0.0 failure mode: valid file, errored tree
        return False
    defset, classset = spec.get("def", set()), spec.get("class", set())
    got_class = got_def = False
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type in classset and _text(n.child_by_field_name("name")) == want_class:
            got_class = True
        elif n.type in defset and _text(n.child_by_field_name("name")) == want_def:
            got_def = True
        stack.extend(n.children)
    return got_class and got_def


def _ts_languages():
    """Lazily build {grammar_name: Language} for every grammar that is BOTH installed AND
    version-compatible with the tree-sitter core (each loaded independently, so one
    missing/incompatible grammar never blocks the others — the extractor simply covers fewer
    languages) AND passes the structural-health PROBE (see _grammar_passes_probe: a grammar that
    mis-parses valid files is dropped → its files degrade to the node-only path, never a silent
    symbol miss). A new language = one row here + one entry in _GENERIC_SPEC; unsupported (or
    probe-failing) source still gets storey-1 (direct collision).

    Svelte is included here so build_graph can obtain its Parser via langs["svelte"]. It has
    no _GENERIC_SPEC entry (it is not dispatched through extract_file_generic); instead
    build_graph routes .svelte files to extract_file_svelte which uses the svelte parser to
    locate the <script> block and then delegates to _walk_ts_tree with the TS/JS parser."""
    try:
        from tree_sitter import Language, Parser
    except Exception:
        return {}
    from importlib import import_module
    specs = [("javascript", "tree_sitter_javascript", "language"),
             ("typescript", "tree_sitter_typescript", "language_typescript"),
             ("tsx", "tree_sitter_typescript", "language_tsx"),
             ("go", "tree_sitter_go", "language"),
             ("java", "tree_sitter_java", "language"),
             ("ruby", "tree_sitter_ruby", "language"),
             ("php", "tree_sitter_php", "language_php"),
             ("csharp", "tree_sitter_c_sharp", "language"),
             ("html", "tree_sitter_html", "language"),
             ("rust", "tree_sitter_rust", "language"),
             ("c", "tree_sitter_c", "language"),
             ("cpp", "tree_sitter_cpp", "language"),
             ("kotlin", "tree_sitter_kotlin", "language"),
             ("swift", "tree_sitter_swift", "language"),
             # Svelte: grammar used only to locate the <script> block; JS/TS parsing is done
             # by the typescript parser (no _GENERIC_SPEC entry for "svelte").
             ("svelte", "tree_sitter_svelte", "language"),
             # Elixir: defmodule/def/defp/defmacro/defmacrop are all `call` nodes distinguished
             # by the first-child identifier text — _GENERIC_SPEC cannot dispatch on node TYPE,
             # so Elixir uses a bespoke extractor (extract_file_elixir). Grammar is still loaded
             # here so the router can obtain a Parser.
             ("elixir", "tree_sitter_elixir", "language")]
    out = {}
    for name, pkg, attr in specs:
        try:
            lang = Language(getattr(import_module(pkg), attr)())
            Parser(lang)        # verify ABI compatibility HERE (the version check fires at Parser
                                # build, not at Language()), so an ABI-too-new grammar is dropped —
                                # never crashes build_graph.
            # STRUCTURAL-HEALTH PROBE: a grammar that parses a trivially-valid file with errors (and
            # then salvages symbols inconsistently) is DROPPED, so its files take the node-only path
            # (recall-safe) instead of producing an authoritative-looking partial graph. Only probed
            # languages (see _PROBE, e.g. kotlin-1.0.0) can fail; all others pass unconditionally.
            if not _grammar_passes_probe(name, lang, _GENERIC_SPEC.get(name)):
                continue
            out[name] = lang
        except Exception:
            pass  # not installed, or grammar/core ABI mismatch — skip, keep the rest
    # Dart: no standalone tree-sitter-dart wheel on PyPI; loaded via tree-sitter-language-pack
    # (the 0.9.x series is ABI v14, compatible with tree-sitter 0.23.x). Absent or incompatible
    # pack → .dart files keep the file-node / direct-collision path (recall-safe, never fatal).
    try:
        from tree_sitter_language_pack import get_language as _lp_get
        _dart_lang = _lp_get("dart")
        Parser(_dart_lang)                    # ABI check: raises on mismatch
        out["dart"] = _dart_lang
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Small text helpers (shared by ts/generic/html extractors)
# ---------------------------------------------------------------------------

def _text(node):
    return node.text.decode("utf-8", "replace") if node is not None else None


def _strip_quotes(s):
    if s and len(s) >= 2 and s[0] in "\"'`" and s[-1] in "\"'`":
        return s[1:-1]
    return s


# ── CONTENT-FREE GUARD: a string-literal IMPORT specifier is a REFERENCE TOKEN, never a source BODY ─────────
# A tree-sitter string-literal import/require/include/`<script src>` carries the RAW bytes the author wrote
# between the quotes — and `require("...")` / `import "..."` / `#include "..."` / `<script src="...">` accept
# ANY string. Unlike Python's `ast` (which yields dotted IDENTIFIERS for a module), nothing here bounds the
# shape, so an author could smuggle an arbitrary multiline / whitespace-laden / huge source fragment into the
# `imports` edge's `dst` — a stored content-free VIOLATION (the graph must hold paths/names, never bodies).
# A genuine specifier (`react`, `./src/app`, `@scope/pkg`, `github.com/org/repo/pkg`, `com.google.gson.Gson`,
# `a/b.hpp`, `<vector>`) has NO whitespace, NO newline, NO control char, and a sane length. We REJECT anything
# else (return None → the caller emits NO edge): dropping a non-reference specifier loses no real coupling (a
# real module path never looks like that) and a malformed/hostile one can never become a stored body. The cap
# is generous (real scoped/monorepo specifiers can be long) but far below a body.
_SPEC_MAX_LEN = 512


def _module_specifier(s):
    """A string-literal import/require/include/asset specifier, validated as a content-free REFERENCE TOKEN —
    or None when it does not look like one (so the caller emits no edge). Rejects whitespace (incl. newline/tab),
    any C0/C1 control char, and over-length — i.e. anything that is a source FRAGMENT rather than a module path /
    name. Keeps the moat content-free at the capture point: only reference-shaped tokens ever become an edge dst."""
    if not s or not isinstance(s, str):
        return None
    if len(s) > _SPEC_MAX_LEN:
        return None
    for ch in s:
        # any whitespace (space/tab/newline/CR/FF/VT + unicode spaces) or a control char (C0/C1) ⇒ not a
        # reference token — a real specifier never contains these. `ord(ch) < 0x20` covers tab/newline/etc.
        if ch.isspace() or ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F:
            return None
    return s


def _first_string_in(node):
    """The first string literal anywhere under `node` (used to read require('x') / import args)."""
    if node is None:
        return None
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("string", "template_string"):
            return _strip_quotes(_text(n))
        stack.extend(n.children)
    return None


# ---------------------------------------------------------------------------
# C/C++ declarator walker
# ---------------------------------------------------------------------------

def _trailing_ident(node):
    """The last identifier-ish leaf under `node` (so `pkg.Bar`/`o.g`/`a::b` → Bar/g/b). Falls
    back to the node's own text. Language-agnostic way to get a callee/name without per-language
    member-access shapes."""
    if node is None:
        return None
    out = []

    def rec(n):
        if n.child_count == 0 and ("identifier" in n.type or n.type in ("constant", "name")):
            out.append(_text(n))
        for c in n.children:
            rec(c)
    rec(node)
    return out[-1] if out else _text(node)


# Qualified-name node types across the dotted-import languages. `qualified_identifier` is KOTLIN's
# (`import android.os.Bundle` → qualified_identifier with `.`-joined identifier children, verified by
# grammar introspection); the rest are Java/C#(.) Rust(::) PHP(\) Swift(.).
_QUALIFIED_NAME_TYPES = ("scoped_identifier", "qualified_name", "qualified_identifier",
                         "namespace_name", "identifier")


def _dotted_import(node):
    """The FIRST full dotted import path of an import node (back-compat single-value wrapper around
    _dotted_imports). Returns None when the node names nothing local-resolvable."""
    paths = _dotted_imports(node)
    return paths[0] if paths else None


def _dotted_imports(node):
    """The FULL qualified import path(s) of an import node, as the author wrote them — ONE per imported
    target. A single import (`import a.b.C;`, `using a.b.C;`, `use a::b::c;`, `use A\\B\\C;`,
    `import a.b.*;`, `import UIKit.UIView`) yields a 1-element list with the OUTERMOST qualified-name
    node's text, so the resolver suffix-matches it to the ONE file it names instead of fanning a bare
    last segment out to every same-basename file. A GROUPED import yields one path per item, each
    prefixed by the group's shared path:
      • Rust `use a::b::{X, Y, c::D}`  → ['a::b::X', 'a::b::Y', 'a::b::c::D']   (scoped_use_list/use_list)
      • PHP  `use A\\B\\{X, Y}`         → ['A\\B\\X', 'A\\B\\Y']                  (namespace_use_group)
    The separator (`.`/`::`/`\\`) is kept verbatim — _cg_resolve normalizes it to '/'. The alias of
    `use A\\B\\C as X` / `using X = A.B.C` is EXCLUDED (we read the qualified-name node, not the clause
    text). A path that suffix-matches no local file (a JDK/stdlib/external import) resolves to nothing —
    correctly external, no false coupling. Returns [] when nothing import-shaped is found."""
    if node is None:
        return []

    # RUST grouped import: `use a::b::{X, Y, c::D}` parses as scoped_use_list = prefix scoped_identifier
    # + use_list({...}). Emit prefix + "::" + each item (items are identifier or nested scoped_identifier).
    grp = _find_first(node, ("scoped_use_list",))
    if grp is not None:
        prefix = _find_first(grp, ("scoped_identifier", "identifier"))
        ulist = _find_first(grp, ("use_list",))
        pfx = _text(prefix)
        if pfx and ulist is not None:
            items = [_text(c) for c in ulist.children
                     if c.type in ("identifier", "scoped_identifier")]
            paths = [pfx + "::" + it for it in items if it]
            if paths:
                return paths

    # PHP grouped import: `use A\B\{X, Y}` parses as namespace_use_declaration with a namespace_name
    # prefix + namespace_use_group({...}); each namespace_use_clause holds the leaf symbol `name`.
    # PRECISION: `use function A\B\{f1, f2}` and `use const A\B\{C1, C2}` place a `function`/`const`
    # keyword node as a DIRECT child of namespace_use_declaration (BEFORE namespace_name). These name
    # FUNCTION/CONST SYMBOLS — not files — so suffix-matching them to .php paths produces false edges.
    # Detected by a direct-child type scan; return [] immediately (no edge for this import).
    _PHP_SYMKIND = frozenset({"function", "const"})
    if any(c.type in _PHP_SYMKIND for c in node.children):
        return []
    grp = _find_first(node, ("namespace_use_group",))
    if grp is not None:
        prefix = _text(_find_first(node, ("namespace_name",)))
        if prefix:
            items = [_text(_find_first(cl, ("qualified_name", "namespace_name", "name")))
                     for cl in grp.children if cl.type == "namespace_use_clause"]
            paths = [prefix + "\\" + it for it in items if it]
            if paths:
                return paths

    # PHP single-symbol import: `use function App\Helpers\format;` / `use const App\X\MAX;` places a
    # `function`/`const` keyword as the FIRST child of namespace_use_clause (before the qualified_name).
    # These name a FUNCTION or CONST SYMBOL, not a file — skip them to prevent false file edges.
    # A plain class `use App\Models\User;` has no such keyword in its clause → unaffected.
    for c in node.children:
        if c.type == "namespace_use_clause":
            if any(k.type in _PHP_SYMKIND for k in c.children):
                return []

    # ALIAS exclusion: `using Series = A.B.C;` (C#) / `import C = a.b.C` puts a BARE alias `identifier`
    # BEFORE the `=` and the real target `qualified_name` AFTER it. A naive pre-order search returns the
    # alias `identifier` first (it IS a qualified type) → a single-segment dst that basename-fans-out to
    # every same-named file (real-repo audit on jellyfin: `using Series = …TV.Series` falsely coupled to
    # BOTH `…/TV/Series.cs` AND `…/Libraries/Series.cs`). When the import carries an `=`, search ONLY the
    # children AFTER it (the target), so the alias never becomes the import path.
    search_root = node
    eq = next((i for i, c in enumerate(node.children) if c.type == "="), None)
    if eq is not None:
        for c in node.children[eq + 1:]:
            q = _find_first(c, _QUALIFIED_NAME_TYPES)
            if q is not None:
                return [_text(q)] if _text(q) else []
        return []
    q = _find_first(search_root, _QUALIFIED_NAME_TYPES)
    t = _text(q) if q is not None else None
    return [t] if t else []


def _find_first(node, types):
    """Pre-order search for the OUTERMOST descendant whose type is in `types` (don't descend once
    matched, so a nested qualified-name yields the whole path, not an inner prefix). None if absent."""
    if node is None:
        return None
    if node.type in types:
        return node
    for c in node.children:
        r = _find_first(c, types)
        if r is not None:
            return r
    return None


def _c_decl_name(n):
    """The function NAME of a C/C++ function_definition: descend the `declarator` chain
    (function/pointer/reference declarators) to the innermost identifier — NOT a parameter or a
    body identifier. `Cls::m` → last segment via _trailing_ident."""
    d = n.child_by_field_name("declarator")
    seen = 0
    while d is not None and d.type not in ("identifier", "field_identifier", "qualified_identifier",
                                           "destructor_name", "operator_name") and seen < 8:
        seen += 1
        nxt = d.child_by_field_name("declarator")
        if nxt is None:
            nxt = next((c for c in d.children if "identifier" in c.type
                        or c.type in ("qualified_identifier",)), None)
        d = nxt
    return _trailing_ident(d) if d is not None else None


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------

def _span(node):
    """A tree-sitter node's CONTENT-FREE line span: (start_line, end_line), 1-based and inclusive, from
    start_point.row / end_point.row (which are 0-based). Line numbers only — never the code. None on a
    missing node so the caller omits the span (a symbol without a span resolves at the file level — the
    safety net, never a missed collision)."""
    if node is None:
        return None, None
    try:
        sl = node.start_point.row + 1
        el = node.end_point.row + 1
    except Exception:
        return None, None
    if isinstance(sl, int) and isinstance(el, int) and el >= sl:
        return sl, el
    return None, None


def _is_exported_decl(declarator):
    """True when a `variable_declarator` is part of a DIRECTLY-EXPORTED top-level binding —
    i.e. its `lexical_declaration`/`variable_declaration` parent is an immediate child of an
    `export_statement` (`export const X = …`, `export let Y = …`, `export const A=1, B=2`).
    Content-free (node types only). This is the PRECISION FLOOR for minting a symbol from a
    NON-function value: a bare local `const x = 1` (parent = a plain lexical_declaration, NOT under
    export) returns False, so a function-local temporary never becomes a top-level symbol; a
    `export { x }` re-export of a separately-declared const carries an export_clause (not a
    declaration), so that const's declarator parent is also a plain lexical_declaration → False."""
    decl = declarator.parent
    if decl is None or decl.type not in ("lexical_declaration", "variable_declaration"):
        return False
    return decl.parent is not None and decl.parent.type == "export_statement"


def _walk_ts_tree(tree, rel, label, nodes, edges, line_offset=0):
    """Walk a tree-sitter tree (TypeScript/JavaScript grammar) and append symbol nodes and
    structural edges to `nodes` / `edges`. `line_offset` is the 0-based row in the OUTER
    file where the parsed text begins (0 for a plain .ts/.js file; `raw_text.start_point.row`
    for a Svelte `<script>` block; the `<script>` tag's row for a Vue SFC). All emitted
    start_line/end_line values are 1-based line numbers relative to the OUTER file."""

    def _def(name, kind, span_node=None):
        sym = f"{rel}::{name}"
        n = {"id": sym, "kind": kind, "name": name, "path": rel, "language": label}
        if span_node is not None:
            try:
                # start_point.row is 0-based relative to the parsed bytes.
                # file line (1-based) = line_offset + span_node.start_point.row + 1
                sl = line_offset + span_node.start_point.row + 1
                el = line_offset + span_node.end_point.row + 1
                if isinstance(sl, int) and isinstance(el, int) and el >= sl:
                    n["start_line"], n["end_line"] = sl, el
            except Exception:
                pass
        nodes.append(n)
        edges.append({"src": rel, "dst": sym, "kind": "contains"})

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        t = node.type
        if t in _FUNC_DEF_TYPES:
            nm = _text(node.child_by_field_name("name"))
            if nm:
                _def(nm, "def", node)
        elif t == "method_definition":
            nm = _text(node.child_by_field_name("name"))
            if nm:
                _def(nm, "def", node)
        elif t in _CLASS_TYPES:
            nm = _text(node.child_by_field_name("name"))
            if nm:
                _def(nm, "class", node)
        elif t == "variable_declarator":
            # `const foo = () => {}` / `const foo = function(){}` — a named function value.
            val = node.child_by_field_name("value")
            nm = _text(node.child_by_field_name("name"))
            if nm and val is not None and val.type in _FUNC_VALUE_TYPES:
                # span the WHOLE declarator (const foo = …) so the symbol covers its body lines.
                _def(nm, "def", node)
            elif nm and _is_exported_decl(node):
                # NON-FUNCTION EXPORTED VALUE (recall, measure-first, this lane). An
                # `export const NAME = <value>` that BINDS a name is a first-class symbol a PR edits in
                # isolation — a store factory (`defineStore(...)`), a Zod/Yup validator (`z.object(...)`),
                # a config/lookup map, a singleton — exactly like an exported `interface`/`type`/`enum`
                # already mints a node. Omitting it meant a PR touching ONLY such a binding produced no
                # symbol → finer-collision degraded to FILE level (recall-safe but imprecise, and
                # INCONSISTENT with the type-level kinds we already mint). MEASURED miss rate on the #1
                # customer market (TypeScript): directus 57%, NestJS 86%, tRPC 90%, plane 94% of these
                # bindings had no symbol. We mint a `def` (NOT a new kind — the schema node_kind CHECK is
                # {file,def,class,table,column,config_file,config_key}; a `const`/`binding` kind would be
                # SILENTLY DROPPED at ingest and need a DDL migration). The `def` flows through the SAME
                # finer-collision span logic (node_kind IN def/class/table) every function def already does.
                # PRECISION-SCOPED to DIRECTLY-EXPORTED top-level bindings (the declarator's
                # lexical_declaration/variable_declaration parent is the child of an `export_statement`):
                #   • a bare local `const x = 1` (NOT exported) → NO symbol (the precision floor — a
                #     function-local temp must never become a top-level symbol);
                #   • `export { x }` re-exporting a separately-declared `const x = 1` carries an
                #     export_clause, NOT a declaration, so `x`'s declarator parent is a plain
                #     lexical_declaration (NOT under export) → still NO symbol (consistent: only the
                #     declaration-site export mints).
                # The function-value branch above ALREADY mints non-exported `const fn = () => {}` (a
                # local function IS a unit), so this branch deliberately handles ONLY the non-function case.
                _def(nm, "def", node)
        elif t in ("public_field_definition", "field_definition"):
            # CLASS-FIELD ARROW METHOD (recall, this lane). `fetch = async () => {}` /
            # `handler = (x) => x` as a CLASS FIELD (the MobX/Angular/idiomatic-TS pattern: an arrow
            # bound as a field so `this` is captured) is a METHOD in all but grammar — but it is a
            # `public_field_definition`/`field_definition`, NOT a `method_definition`, so the method
            # branch above missed it and the whole method produced no symbol. MEASURED on plane_web:
            # 490 such fields, 0 captured; cycle.store.ts alone had 9/26 methods invisible. We mirror the
            # variable_declarator function-value check: when the field's VALUE is a function (arrow /
            # function expression), mint a `def` named after the field. The field NAME is a
            # `property_identifier` (read via the `name` field; falls back to the first
            # property_identifier child for grammars that don't expose `name`). PRECISION: a plain data
            # field (`count = 5`, `name: string`) has a NON-function value (or none) → NO symbol, so
            # only the genuinely-method-shaped fields mint (no junk from every class property).
            val = node.child_by_field_name("value")
            if val is not None and val.type in _FUNC_VALUE_TYPES:
                nm = _text(node.child_by_field_name("name")) or _text(
                    next((c for c in node.children if c.type == "property_identifier"), None))
                if nm and nm.isidentifier():
                    # span the WHOLE field definition so the symbol covers the arrow body lines.
                    _def(nm, "def", node)
        elif t == "assignment_expression":
            # `app.set = function set(s,v){}` / `proto.route = function route(p){}` — prototype-style
            # method definitions, idiomatic in Express, Connect, and older Node.js libraries. These
            # produce NO def node without this branch: the LHS is a `member_expression` (not a plain
            # `identifier`), so `variable_declarator` never matches and the method is invisible at
            # symbol level — finer-collision degrades to file-level for these files (recall-safe but
            # imprecise). Pattern: assignment_expression whose `left` field is a `member_expression`
            # and `right` field is a function (function_expression or arrow_function). The def name
            # is the RIGHTMOST property of the LHS (e.g. `app.set` → `set`, `proto.route` → `route`).
            # PRECISION GUARDS (in priority order):
            #   1. RHS must be directly in _FUNC_VALUE_TYPES — NOT a call_expression wrapping a function
            #      (e.g. `arr.forEach(x => x)` is a `call_expression`, never `assignment_expression`).
            #   2. LHS property must be a plain `property_identifier` leaf — exactly one segment (e.g.
            #      `app.set` or `proto.route`), so multi-level chains like `a.b.c = fn` only emit `c`
            #      via `_trailing_ident`, which is correct and consistent with how callee names are read.
            #   3. No special handling for chained alias assignments (`req.get = req.header = fn`): the
            #      outer `assignment_expression` has RHS = `assignment_expression` (NOT a function) so it
            #      does not match; the inner one (RHS = function_expression) DOES match and emits the
            #      inner property name. Acceptable: captures the canonical name; aliases are a recall gap,
            #      never a false def (no junk minted).
            #   4. Inline callbacks (`[].forEach(x => x)`, `arr.map(function(i){})`) live INSIDE a
            #      `call_expression`'s `arguments` node, NOT as the RHS of an `assignment_expression` —
            #      they CANNOT trigger this branch regardless of tree depth.
            lhs = node.child_by_field_name("left")
            rhs = node.child_by_field_name("right")
            if (lhs is not None and lhs.type == "member_expression"
                    and rhs is not None and rhs.type in _FUNC_VALUE_TYPES):
                # Use `_trailing_ident` on the LHS so `proto.ns.method` → `method` (consistent with
                # callee resolution throughout the extractor). A bare `property_identifier` returns
                # its own text; a deeper chain returns the rightmost segment.
                nm = _trailing_ident(lhs)
                if nm and nm.isidentifier():
                    # Span the WHOLE assignment_expression so the symbol covers its function body lines.
                    _def(nm, "def", node)
        elif t == "class_heritage":
            # SYMBOL-USE (recall): `class App extends Base implements IHandler` — the extends/implements
            # clauses name base types App DEPENDS ON with no `calls` edge. Capture each as a `calls`-class
            # edge (dst = base type name) so the engine's call-resolution precision guards apply. An
            # `extends`/`implements` clause wraps the name as a type_identifier/identifier (or a generic /
            # member-expression for a qualified base) → trailing identifier, content-free.
            cstack = [node]
            while cstack:
                cn = cstack.pop()
                if cn is not node and cn.type in _TYPE_NAME_NODES + ("member_expression",):
                    nm = _trailing_ident(cn)
                    if nm and nm.isidentifier():
                        edges.append({"src": rel, "dst": nm, "kind": "calls"})
                    continue
                if cn.type in ("type_arguments", "statement_block"):   # not a base name (generic arg / body)
                    continue
                cstack.extend(cn.children)
        elif t == "new_expression":
            # SYMBOL-USE (recall): `new Widget()` constructs a type with no `calls` edge to its definer.
            # The constructed type is the `constructor` field (identifier / member_expression). Trailing
            # identifier → content-free type name, emitted as a `calls`-class edge.
            ctor = node.child_by_field_name("constructor")
            nm = _trailing_ident(ctor) if ctor is not None else None
            if nm and nm.isidentifier():
                edges.append({"src": rel, "dst": nm, "kind": "calls"})
        elif t == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None:
                if fn.type == "import":
                    # DYNAMIC import: `import("./mod")` / `await import("./mod")` is a call_expression
                    # whose callee node TYPE is `import` (the keyword), NOT an identifier — so the
                    # `require`/member branches miss it and the module was SILENTLY DROPPED (a real
                    # coupling lost: code-split lazy chunks, conditional loads). Read the string arg and
                    # emit the same `imports` edge shape as require()/static import; the resolver then
                    # links `./mod` like any relative import. (A bare `import x from …` is a STATEMENT,
                    # handled separately — this is only the call form.)
                    mod = _module_specifier(_first_string_in(node.child_by_field_name("arguments")))
                    if mod:
                        edges.append({"src": rel, "dst": mod, "kind": "imports"})
                elif fn.type == "identifier":
                    nm = _text(fn)
                    if nm == "require":
                        mod = _module_specifier(_first_string_in(node.child_by_field_name("arguments")))
                        if mod:
                            edges.append({"src": rel, "dst": mod, "kind": "imports"})
                    elif nm:
                        edges.append({"src": rel, "dst": nm, "kind": "calls"})
                elif fn.type == "member_expression":
                    nm = _text(fn.child_by_field_name("property"))
                    if nm:
                        edges.append({"src": rel, "dst": nm, "kind": "calls"})
        elif t in ("import_statement", "import_require_clause"):
            mod = _module_specifier(_strip_quotes(_text(node.child_by_field_name("source"))) or _first_string_in(node))
            if mod:
                edges.append({"src": rel, "dst": mod, "kind": "imports"})
                # NAMED-IMPORT → BARREL RE-EXPORT (recall, measure-first, this lane). `import { devtools }
                # from 'zustand/middleware'` couples the importer to `…/middleware.ts`, but the SYMBOL
                # `devtools` is RE-EXPORTED by that barrel from `…/middleware/devtools.ts` — and the
                # importer's REAL dependency is that implementation file, NOT the barrel. The bare-module
                # edge above reaches only the barrel, so the importer→impl coupling was SILENTLY MISSED
                # (measured on zustand: every `tests/devtools.test.tsx` → `src/middleware/devtools.ts`
                # co-change pair was a graph-blind miss; live recall 57→71%). FIX: for each NAMED specifier
                # `nm`, also emit `mod/nm` — the SAME shape Python's bespoke path already emits for
                # `from pkg import name` (→ `pkg.name`), so the resolver's existing suffix/own-package probe
                # can reach a submodule FILE named `nm` (`middleware/devtools`→`src/middleware/devtools.ts`)
                # when the barrel re-exports it under that name. CONTENT-FREE (a module path + an imported
                # identifier, never a body). PRECISION-SAFE: `mod/nm` resolves ONLY when a file actually
                # carries that suffix — a named import from an EXTERNAL pkg (`{ useState } from 'react'`
                # → `react/useState`) names no local file → inert (no edge survives resolution); a re-export
                # whose impl basename differs from the imported name simply does not resolve (recall gap,
                # never a false edge). We read the SPECIFIER's `name` field (the re-exported symbol), NOT the
                # local `alias` (`{ x as y }` re-exports `x`). A `* as ns` namespace / default import has no
                # `import_specifier` → no extra edge (only the bare-module edge, unchanged).
                clause = node.child_by_field_name("name") or node.child_by_field_name("import")
                _scan = node.children if clause is None else [clause]
                _stk = list(_scan)
                while _stk:
                    _n = _stk.pop()
                    if _n.type == "import_specifier":
                        _nm = _text(_n.child_by_field_name("name") or (_n.children[0] if _n.children else None))
                        if _nm and _nm.isidentifier():
                            edges.append({
                                "src": rel,
                                "dst": mod.rstrip("/") + "/" + _nm,
                                "kind": "imports",
                                # Resolver-only: the synthesized coordinate
                                # may name a real re-export module or merely a
                                # member of the resolved base module. The
                                # resolver strips this marker before graph
                                # assembly/persistence/hash.
                                "resolution_probe": "member",
                            })
                    elif _n.type not in ("string", "string_fragment"):
                        _stk.extend(_n.children)
        elif t == "export_statement":
            # A RE-EXPORT (`export * from './foo'`, `export { x } from './bar'`,
            # `export { x as y } from './bar'`, `export * as ns from './ns'`) is an
            # export_statement that CARRIES a `source` field (the `from '...'` clause) — verified
            # by grammar introspection across javascript/typescript/tsx. It couples this file to
            # the re-exported module exactly like an import (a barrel `index.ts` is precisely where
            # coupling concentrates), so emit the same `imports` edge to that specifier; the
            # resolver already resolves `./foo` correctly once the edge exists.
            # PRECISION: a LOCAL export (`export const LOCAL = 1` carries `declaration`; `export
            # { localOnly }` carries only an `export_clause`) has NO `source` field → no edge.
            src_node = node.child_by_field_name("source")
            if src_node is not None:
                mod = _module_specifier(_strip_quotes(_text(src_node)))
                if mod:
                    edges.append({"src": rel, "dst": mod, "kind": "imports"})
        stack.extend(node.children)


def _tree_parse_complete(tree, nodes):
    """Return whether a tree-sitter parse proved a complete syntax tree.

    tree-sitter deliberately returns useful partial trees for malformed input.
    Those nodes/edges remain valuable evidence, but they cannot prove that
    definitions or references absent from the partial tree do not exist. Stamp
    the canonical document node so graph assembly persists that distinction.
    """
    try:
        complete = not bool(tree.root_node.has_error)
    except Exception:
        complete = False
    if not complete:
        for node in nodes:
            if node.get("kind") == "file":
                node["analysis_status"] = "incomplete"
    return complete


def extract_file_ts(path, rel, label, parser):
    """Return (nodes, edges, ok) for one tree-sitter-parsed file, in the uniform model."""
    nodes = [{"id": rel, "kind": "file", "path": rel, "language": label}]
    edges = []
    try:
        with open(path, "rb") as fh:
            src = fh.read()
        tree = parser.parse(src)
    except (OSError, ValueError):
        return nodes, edges, False
    complete = _tree_parse_complete(tree, nodes)
    _walk_ts_tree(tree, rel, label, nodes, edges, line_offset=0)
    return nodes, edges, complete


_HTML_RAW_TEXT_TAGS = frozenset({
    "style", "textarea", "title", "xmp", "iframe", "noembed", "noframes", "plaintext",
})


def _html_tag_end(source, start):
    """Index of the next static, unquoted ``>`` in one HTML-ish start tag.

    Framework attributes may contain JavaScript expressions.  Their comparison
    operators and string/regex/comment contents cannot terminate the surrounding
    tag, so balanced ``{...}`` regions are consumed as one lexical unit.
    """
    quote = None
    escaped = False
    expression_depth = 0
    pos = start

    def regex_may_start(at):
        previous = at - 1
        while previous >= start and source[previous].isspace():
            previous -= 1
        if previous < start or source[previous] in "{([=,:;!?&|+-*%^~>":
            return True
        word_end = previous + 1
        while previous >= start and (
            source[previous].isalnum() or source[previous] in "_$"
        ):
            previous -= 1
        return source[previous + 1:word_end] in {
            "return", "throw", "case", "delete", "void", "typeof",
            "instanceof", "in", "of", "yield", "await",
        }

    def regex_end(at):
        cursor = at + 1
        regex_escaped = False
        in_class = False
        while cursor < len(source):
            current = source[cursor]
            if regex_escaped:
                regex_escaped = False
            elif current == "\\":
                regex_escaped = True
            elif current == "[":
                in_class = True
            elif current == "]" and in_class:
                in_class = False
            elif current == "/" and not in_class:
                cursor += 1
                while cursor < len(source) and source[cursor].isalpha():
                    cursor += 1
                return cursor
            elif current in "\r\n":
                return cursor
            cursor += 1
        return len(source)

    while pos < len(source):
        ch = source[pos]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif expression_depth > 0 and source.startswith("//", pos):
            newline = source.find("\n", pos + 2)
            pos = len(source) if newline < 0 else newline
            continue
        elif expression_depth > 0 and source.startswith("/*", pos):
            comment_end = source.find("*/", pos + 2)
            pos = len(source) if comment_end < 0 else comment_end + 2
            continue
        elif expression_depth > 0 and ch == "/" and regex_may_start(pos):
            pos = regex_end(pos)
            continue
        elif ch in ("\"", "'") or (expression_depth > 0 and ch == "`"):
            quote = ch
        elif ch == "{":
            expression_depth += 1
        elif ch == "}" and expression_depth > 0:
            expression_depth -= 1
        elif ch == ">" and expression_depth == 0:
            return pos
        pos += 1
    return None


def _html_open_tag_name_exact(source, start, end):
    """Case-preserved opening tag name at ``start``; None for closing/declarations."""
    pos = start + 1
    if pos >= end or source[pos] in "/!?":
        return None
    name_start = pos
    while pos < end and (source[pos].isalnum() or source[pos] in "_:-"):
        pos += 1
    if pos == name_start:
        return None
    if pos < end and not (source[pos].isspace() or source[pos] == "/"):
        return None
    return source[name_start:pos]


def _html_open_tag_name(source, start, end):
    """Lower-cased opening tag name at ``start``; None for closing/declaration tags."""
    name = _html_open_tag_name_exact(source, start, end)
    return name.lower() if name is not None else None


def _html_closing_tag_name_exact(source, start, end):
    """Case-preserved closing tag name at ``start``; None for malformed/non-closing tags."""
    if not source.startswith("</", start):
        return None
    pos = start + 2
    name_start = pos
    while pos < end and (source[pos].isalnum() or source[pos] in "_:-"):
        pos += 1
    if pos == name_start:
        return None
    name_end = pos
    while pos < end and source[pos].isspace():
        pos += 1
    if pos != end:
        return None
    return source[name_start:name_end]


def _html_closing_tag(source, body_start, name):
    """Return ``(start, end)`` for the next matching HTML closing tag."""
    pos = source.find("<", body_start)
    prefix_len = len(name) + 2
    n = len(source)
    while pos >= 0:
        after_name = pos + prefix_len
        if (
            source[pos:after_name].lower() == "</" + name
            and (after_name >= n or source[after_name].isspace() or source[after_name] == ">")
        ):
            end = after_name
            while end < n and source[end].isspace():
                end += 1
            if end < n and source[end] == ">":
                return pos, end
        pos = source.find("<", pos + 1)
    return None


def _mask_astro_non_executable(text):
    """Blank Astro non-code literals/comments/raw text without changing length or rows.

    Real ``<script>`` elements are skipped wholesale and therefore retain their exact
    bodies for the TypeScript pass. Astro template comments, strings/comments inside
    ``{...}`` expressions, and HTML raw/RCDATA element bodies are replaced with spaces
    while CR/LF bytes stay in place. The same masked markup feeds both the script and
    asset passes, so neither can persist a reference-shaped token from presentation text.
    """
    source = text or ""
    n = len(source)
    masked = None

    def blank(start, end):
        nonlocal masked
        if end <= start:
            return
        if masked is None:
            masked = list(source)
        for idx in range(start, min(end, n)):
            if source[idx] not in "\r\n":
                masked[idx] = " "

    def quoted_end(start):
        quote = source[start]
        pos = start + 1
        escaped = False
        while pos < n:
            ch = source[pos]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                return pos + 1
            pos += 1
        return n

    def regex_may_start(start):
        pos = start - 1
        while pos >= 0 and source[pos].isspace():
            pos -= 1
        if pos < 0 or source[pos] in "{([=,:;!?&|+-*%^~>":
            return True
        end = pos + 1
        while pos >= 0 and (source[pos].isalnum() or source[pos] in "_$"):
            pos -= 1
        return source[pos + 1:end] in {
            "return", "throw", "case", "delete", "void", "typeof",
            "instanceof", "in", "of", "yield", "await",
        }

    def regex_end(start):
        pos = start + 1
        escaped = False
        in_class = False
        while pos < n:
            ch = source[pos]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "[":
                in_class = True
            elif ch == "]" and in_class:
                in_class = False
            elif ch == "/" and not in_class:
                pos += 1
                while pos < n and source[pos].isalpha():
                    pos += 1
                return pos
            elif ch in "\r\n":
                return pos
            pos += 1
        return n

    cursor = 0
    expression_depth = 0
    while cursor < n:
        if source.startswith("<!--", cursor):
            end = source.find("-->", cursor + 4)
            cursor = n if end < 0 else end + 3
            continue

        if source.startswith("{/*", cursor):
            end = source.find("*/}", cursor + 3)
            next_cursor = n if end < 0 else end + 3
            blank(cursor, next_cursor)
            cursor = next_cursor
            continue

        if expression_depth > 0:
            if source[cursor] in ("\"", "'", "`"):
                end = quoted_end(cursor)
                blank(cursor, end)
                cursor = end
                continue
            if source.startswith("//", cursor):
                end = source.find("\n", cursor + 2)
                end = n if end < 0 else end
                blank(cursor, end)
                cursor = end
                continue
            if source.startswith("/*", cursor):
                end = source.find("*/", cursor + 2)
                end = n if end < 0 else end + 2
                blank(cursor, end)
                cursor = end
                continue
            if source[cursor] == "/" and regex_may_start(cursor):
                end = regex_end(cursor)
                blank(cursor, end)
                cursor = end
                continue
            if source[cursor] == "<":
                # Markup nested in an Astro expression is dynamic coupling.  It
                # is deliberately outside the persisted graph, and consuming
                # only this byte also keeps comparison-heavy expressions linear:
                # no candidate can rescan the remaining input looking for `>`.
                blank(cursor, cursor + 1)
                cursor += 1
                continue

        if source[cursor] == "<":
            open_end = _html_tag_end(source, cursor + 1)
            if open_end is None:
                break
            tag_name = _html_open_tag_name(source, cursor, open_end)
            if tag_name == "script":
                closing = _html_closing_tag(source, open_end + 1, tag_name)
                if closing is None:
                    break
                cursor = closing[1] + 1
                continue
            if tag_name in _HTML_RAW_TEXT_TAGS:
                closing = _html_closing_tag(source, open_end + 1, tag_name)
                body_end = n if closing is None else closing[0]
                blank(open_end + 1, body_end)
                cursor = n if closing is None else closing[1] + 1
                continue
            # Attribute quotes are already bounded by the tag; dynamic src/href
            # expressions are rejected later at the attribute syntax boundary.
            cursor = open_end + 1
            continue

        if source[cursor] == "{":
            expression_depth += 1
        elif source[cursor] == "}" and expression_depth > 0:
            expression_depth -= 1
        cursor += 1

    return source if masked is None else "".join(masked)


def _script_blocks(text, astro_template_comments=False):
    """Return executable JS/TS SFC script blocks in one bounded, linear scan.

    The former dot-all ``<script ...>...</script>`` regex retried the remainder of
    the file at every unmatched opening tag, making a sub-size-cap SFC quadratic.
    This scanner advances monotonically, skips HTML comments and quoted ``<``
    bytes inside other tags, and retains the existing executable ``type`` allowlist.
    Astro callers also skip ``{/* ... */}`` template comments. Each tuple is
    ``(body, zero_based_outer_line)``.
    """
    source = text or ""
    blocks = []
    n = len(source)
    cursor = 0
    lines_before_cursor = 0
    # Cache the next Astro template-comment opener. Searching the whole remaining
    # suffix once per HTML tag would turn an otherwise monotonic scan quadratic on
    # comment-free markup with many tags.
    next_tag_start = source.find("<")
    template_comment_start = source.find("{/*") if astro_template_comments else -1
    allowed_types = {
        "module",
        "text/javascript",
        "application/javascript",
        "text/typescript",
        "application/typescript",
    }

    def declared_script_type(attrs):
        """Actual ``type`` attribute value, not type-like bytes inside another value."""
        pos = 0
        attrs_len = len(attrs)
        while pos < attrs_len:
            while pos < attrs_len and attrs[pos].isspace():
                pos += 1
            if pos >= attrs_len:
                break
            if attrs[pos] == "/":
                pos += 1
                continue

            name_start = pos
            while (
                pos < attrs_len
                and not attrs[pos].isspace()
                and attrs[pos] not in "=/>"
            ):
                pos += 1
            if pos == name_start:
                pos += 1
                continue
            name = attrs[name_start:pos].lower()
            while pos < attrs_len and attrs[pos].isspace():
                pos += 1

            value = None
            if pos < attrs_len and attrs[pos] == "=":
                pos += 1
                while pos < attrs_len and attrs[pos].isspace():
                    pos += 1
                if pos < attrs_len and attrs[pos] in ("\"", "'"):
                    quote = attrs[pos]
                    pos += 1
                    value_start = pos
                    while pos < attrs_len and attrs[pos] != quote:
                        pos += 1
                    value = attrs[value_start:pos]
                    if pos < attrs_len:
                        pos += 1
                else:
                    value_start = pos
                    while pos < attrs_len and not attrs[pos].isspace():
                        pos += 1
                    value = attrs[value_start:pos]

            if name == "type":
                return (value or "").strip().lower()
        return None

    try:
        while cursor < n:
            while 0 <= next_tag_start < cursor:
                next_tag_start = source.find("<", cursor)
            start = next_tag_start
            if astro_template_comments:
                while 0 <= template_comment_start < cursor:
                    template_comment_start = source.find("{/*", cursor)
                if template_comment_start >= 0 and (
                    start < 0 or template_comment_start < start
                ):
                    lines_before_start = lines_before_cursor + source.count(
                        "\n", cursor, template_comment_start
                    )
                    template_comment_end = source.find(
                        "*/}", template_comment_start + 3
                    )
                    if template_comment_end < 0:
                        break
                    next_cursor = template_comment_end + 3
                    lines_before_cursor = lines_before_start + source.count(
                        "\n", template_comment_start, next_cursor
                    )
                    cursor = next_cursor
                    template_comment_start = source.find("{/*", cursor)
                    continue
            if start < 0:
                break
            lines_before_start = lines_before_cursor + source.count("\n", cursor, start)

            if source.startswith("<!--", start):
                comment_end = source.find("-->", start + 4)
                if comment_end < 0:
                    break
                next_cursor = comment_end + 3
                lines_before_cursor = lines_before_start + source.count(
                    "\n", start, next_cursor
                )
                cursor = next_cursor
                continue

            open_end = _html_tag_end(source, start + 1)
            if open_end is None:
                break
            tag_name = _html_open_tag_name(source, start, open_end)
            is_script = tag_name == "script"
            if tag_name in _HTML_RAW_TEXT_TAGS:
                closing = _html_closing_tag(source, open_end + 1, tag_name)
                if closing is None:
                    break
                next_cursor = closing[1] + 1
                lines_before_cursor = lines_before_start + source.count(
                    "\n", start, next_cursor
                )
                cursor = next_cursor
                continue
            if not is_script:
                next_cursor = open_end + 1
                lines_before_cursor = lines_before_start + source.count(
                    "\n", start, next_cursor
                )
                cursor = next_cursor
                continue

            name_end = start + len("<script")
            attrs = source[name_end:open_end]
            if attrs.rstrip().endswith("/"):
                next_cursor = open_end + 1
                lines_before_cursor = lines_before_start + source.count(
                    "\n", start, next_cursor
                )
                cursor = next_cursor
                continue

            body_start = open_end + 1
            closing = _html_closing_tag(source, body_start, "script")
            if closing is None:
                break
            close_start, close_end = closing

            declared_type = declared_script_type(attrs)
            # Per HTML's classic-script default, an omitted, empty, or valueless
            # ``type`` remains executable. Only an actual non-empty unsupported
            # MIME/data type suppresses source parsing.
            if declared_type in (None, "") or declared_type in allowed_types:
                body_line = lines_before_start + source.count("\n", start, body_start)
                blocks.append((source[body_start:close_start], body_line))

            next_cursor = close_end + 1
            lines_before_cursor = lines_before_start + source.count(
                "\n", start, next_cursor
            )
            cursor = next_cursor
    except Exception:
        return blocks
    return blocks


def _first_script_block(text, astro_template_comments=False):
    """Return the first executable SFC script block, using the linear scanner."""
    blocks = _script_blocks(text, astro_template_comments=astro_template_comments)
    return blocks[0] if blocks else None


def _vue_expression_end(source, start):
    """Byte after one Vue ``{{ ... }}`` expression, or None when it is unclosed.

    This is a bounded lexical skip, not a JavaScript parser. Its only job is to keep
    tag-shaped strings/comments/regexes inside rendered template expressions from
    being mistaken for SFC structure while locating the outer ``</template>``.
    """
    n = len(source)
    pos = start + 2
    stack = []

    def quoted_end(at):
        quote = source[at]
        cursor = at + 1
        escaped = False
        while cursor < n:
            ch = source[cursor]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                return cursor + 1
            cursor += 1
        return None

    def regex_may_start(at):
        previous = at - 1
        while previous >= start + 2 and source[previous].isspace():
            previous -= 1
        if previous < start + 2 or source[previous] in "{([=,:;!?&|+-*%^~>":
            return True
        word_end = previous + 1
        while previous >= start + 2 and (
            source[previous].isalnum() or source[previous] in "_$"
        ):
            previous -= 1
        return source[previous + 1:word_end] in {
            "return", "throw", "case", "delete", "void", "typeof",
            "instanceof", "in", "of", "yield", "await",
        }

    def regex_end(at):
        cursor = at + 1
        escaped = False
        in_class = False
        while cursor < n:
            ch = source[cursor]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "[":
                in_class = True
            elif ch == "]" and in_class:
                in_class = False
            elif ch == "/" and not in_class:
                cursor += 1
                while cursor < n and source[cursor].isalpha():
                    cursor += 1
                return cursor
            elif ch in "\r\n":
                return cursor
            cursor += 1
        return None

    pairs = {"(": ")", "[": "]", "{": "}"}
    while pos < n:
        if not stack and source.startswith("}}", pos):
            return pos + 2
        ch = source[pos]
        if ch in ("\"", "'", "`"):
            pos = quoted_end(pos)
            if pos is None:
                return None
            continue
        if source.startswith("//", pos):
            newline = source.find("\n", pos + 2)
            if newline < 0:
                return None
            pos = newline + 1
            continue
        if source.startswith("/*", pos):
            comment_end = source.find("*/", pos + 2)
            if comment_end < 0:
                return None
            pos = comment_end + 2
            continue
        if ch == "/" and regex_may_start(pos):
            pos = regex_end(pos)
            if pos is None:
                return None
            continue
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        elif ch in ")]}":
            # A mismatched closer makes every later template boundary uncertain.
            return None
        pos += 1
    return None


def _vue_top_level_element_end(source, body_start, tag_name):
    """Closing-tag end for one Vue top-level non-script block, or None.

    Only same-named nested elements affect depth. Other child markup is irrelevant,
    while comments, Vue interpolations and HTML raw-text bodies are skipped so their
    tag-shaped presentation bytes cannot end the outer block early. The cursor only
    advances, keeping the scan linear.
    """
    n = len(source)
    cursor = body_start
    depth = 1
    next_tag = source.find("<", cursor)
    next_expression = source.find("{{", cursor)
    while cursor < n:
        while 0 <= next_tag < cursor:
            next_tag = source.find("<", cursor)
        while 0 <= next_expression < cursor:
            next_expression = source.find("{{", cursor)
        if next_expression >= 0 and (next_tag < 0 or next_expression < next_tag):
            expression_end = _vue_expression_end(source, next_expression)
            if expression_end is None:
                return None
            cursor = expression_end
            continue
        if next_tag < 0:
            return None
        if source.startswith("<!--", next_tag):
            comment_end = source.find("-->", next_tag + 4)
            if comment_end < 0:
                return None
            cursor = comment_end + 3
            continue
        tag_end = _html_tag_end(source, next_tag + 1)
        if tag_end is None:
            return None
        if source.startswith("</", next_tag):
            closing_name = _html_closing_tag_name_exact(source, next_tag, tag_end)
            if closing_name == tag_name:
                depth -= 1
                if depth == 0:
                    return tag_end
            cursor = tag_end + 1
            continue

        opening_name = _html_open_tag_name_exact(source, next_tag, tag_end)
        self_closing = source[next_tag:tag_end].rstrip().endswith("/")
        if opening_name == tag_name and not self_closing:
            depth += 1
        elif opening_name and opening_name.lower() in (_HTML_RAW_TEXT_TAGS | {"script"}):
            raw_closing = _html_closing_tag(
                source, tag_end + 1, opening_name.lower()
            )
            if raw_closing is None:
                return None
            cursor = raw_closing[1] + 1
            continue
        cursor = tag_end + 1
    return None


def _vue_native_html_template(source, start, end):
    """Whether one lowercase ``template`` block contains native HTML markup.

    Vue preprocessors put their language in the top-level block's static ``lang``
    attribute. Their bodies are not HTML: a perfectly valid Pug comparison such as
    ``count < limit`` must therefore never enter the HTML nesting scanner. Dynamic
    language bindings are not valid SFC compiler metadata and are treated as unknown
    (raw) rather than guessed to be HTML. The attribute cursor is bounded by the
    already-located opening-tag end and advances monotonically.
    """
    pos = start + len("<template")
    while pos < end:
        while pos < end and source[pos].isspace():
            pos += 1
        if pos >= end:
            break
        if source[pos] == "/":
            pos += 1
            continue

        name_start = pos
        while (
            pos < end
            and not source[pos].isspace()
            and source[pos] not in "=/>"
        ):
            pos += 1
        if pos == name_start:
            pos += 1
            continue
        name = source[name_start:pos].lower()
        while pos < end and source[pos].isspace():
            pos += 1

        value = None
        if pos < end and source[pos] == "=":
            pos += 1
            while pos < end and source[pos].isspace():
                pos += 1
            if pos < end and source[pos] in ("\"", "'"):
                quote = source[pos]
                pos += 1
                value_start = pos
                while pos < end and source[pos] != quote:
                    pos += 1
                value = source[value_start:pos]
                if pos < end:
                    pos += 1
            else:
                value_start = pos
                while pos < end and not source[pos].isspace():
                    pos += 1
                value = source[value_start:pos]

        if name == "lang":
            return value is not None and value.strip().lower() == "html"
        if name in {":lang", "v-bind:lang"}:
            return False
    return True


def _vue_script_blocks(text):
    """Executable native Vue SFC scripts, restricted to lowercase top-level blocks.

    Vue presentation markup can legally contain tag-shaped strings, custom ``<docs>``
    blocks and PascalCase ``<Script>`` components. The generic HTML scanner cannot
    distinguish those from an SFC block, so using all of its matches stores rendered
    examples as definitions/imports/calls. This wrapper walks only top-level blocks,
    skips every non-script block wholesale, and delegates MIME/type filtering and body
    extraction to :func:`_script_blocks` without changing Astro/HTML semantics.
    """
    source = text or ""
    blocks = []
    n = len(source)
    cursor = 0
    lines_before_cursor = 0
    try:
        while cursor < n:
            start = source.find("<", cursor)
            if start < 0:
                break
            lines_before_start = lines_before_cursor + source.count("\n", cursor, start)
            if source.startswith("<!--", start):
                comment_end = source.find("-->", start + 4)
                if comment_end < 0:
                    break
                next_cursor = comment_end + 3
                lines_before_cursor = lines_before_start + source.count(
                    "\n", start, next_cursor
                )
                cursor = next_cursor
                continue

            open_end = _html_tag_end(source, start + 1)
            if open_end is None:
                break
            tag_name = _html_open_tag_name_exact(source, start, open_end)
            if tag_name is None:
                next_cursor = open_end + 1
            elif source[start:open_end].rstrip().endswith("/"):
                next_cursor = open_end + 1
            elif tag_name == "script":
                closing = _html_closing_tag(source, open_end + 1, "script")
                if closing is None:
                    break
                next_cursor = closing[1] + 1
                element = source[start:next_cursor]
                for body, line_offset in _script_blocks(element):
                    blocks.append((body, lines_before_start + line_offset))
            elif tag_name == "template" and _vue_native_html_template(
                source, start, open_end
            ):
                close_end = _vue_top_level_element_end(
                    source, open_end + 1, tag_name
                )
                if close_end is None:
                    break
                next_cursor = close_end + 1
            else:
                # Preprocessed templates and custom SFC blocks contain arbitrary
                # non-HTML languages. Skip them as raw text to their own delimiter;
                # interpreting each ``<`` as markup can consume a following real
                # top-level script (and repeatedly reparsing suffixes risks quadratic
                # behavior). ``_html_closing_tag`` is a monotonic linear scan.
                closing = _html_closing_tag(
                    source, open_end + 1, tag_name.lower()
                )
                if closing is None:
                    break
                next_cursor = closing[1] + 1

            lines_before_cursor = lines_before_start + source.count(
                "\n", start, next_cursor
            )
            cursor = next_cursor
    except Exception:
        return blocks
    return blocks


def _astro_frontmatter_block(text):
    """Return ``(frontmatter, line_offset, markup_start)`` for a real Astro fence.

    Astro frontmatter is valid only at the beginning of the file.  Anchoring the
    opening fence there prevents a later Markdown/thematic ``---`` or code example
    from being treated as executable TypeScript.  The closing fence must begin its
    own line.  Pure and never-raising.
    """
    import re as _re

    try:
        pattern = _re.compile(
            r"\A(?:\ufeff)?[ \t]*---[ \t]*\r?\n"
            r"(?P<body>[\s\S]*?)"
            r"(?m:^[ \t]*---[ \t]*)(?:\r?\n|\Z)",
        )
        match = pattern.match(text or "")
        if match is None:
            return None
        return (
            match.group("body") or "",
            (text or "")[: match.start("body")].count("\n"),
            match.end(),
        )
    except Exception:
        return None


def _sfc_executable_text(text, ext):
    """Executable regions of an Astro/Vue/Svelte file, never its presentation markup.

    Contract scanners use this shared precision boundary before matching route or
    schema literals.  Regular code extensions pass through unchanged.  Pure and
    fail-soft so an unusual/malformed SFC becomes silent instead of fabricating a
    coupling from rendered examples.
    """
    try:
        if ext == ".astro":
            frontmatter = _astro_frontmatter_block(text)
            markup_start = frontmatter[2] if frontmatter is not None else 0
            safe_markup = _mask_astro_non_executable(text[markup_start:])
            parts = []
            if frontmatter is not None and frontmatter[0].strip():
                parts.append(frontmatter[0])
            parts.extend(
                body for body, _line in _script_blocks(
                    safe_markup, astro_template_comments=True
                )
                if body.strip()
            )
            return "\n".join(parts)
        if ext == ".vue":
            return "\n".join(
                body for body, _line in _vue_script_blocks(text) if body.strip()
            )
        if ext == ".svelte":
            return "\n".join(
                body for body, _line in _script_blocks(text) if body.strip()
            )
    except Exception:
        return ""
    return text


def extract_file_astro(path, rel, ts_parser, html_parser=None):
    """Return structural graph rows for one Astro component/page.

    Astro's executable regions are its leading frontmatter and an optional client
    ``script`` block.  Each is parsed through the proven TypeScript walker with
    outer-file line offsets.  The remaining markup is passed through the same
    local ``src``/``href`` extractor as HTML, so stylesheet/script assets remain
    connected without interpreting DOM text or Astro expressions.

    Missing parsers, template-only components, malformed fences and malformed
    script blocks degrade to a correctly-labelled bare file node; no whole-markup
    JS scan and no invented symbol edge.
    """
    nodes = [{"id": rel, "kind": "file", "path": rel, "language": "astro"}]
    edges = []
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8", "replace")
    except (OSError, ValueError):
        return nodes, edges, False

    frontmatter = _astro_frontmatter_block(text)
    markup_start = frontmatter[2] if frontmatter is not None else 0
    code_blocks = []
    if frontmatter is not None and frontmatter[0].strip():
        code_blocks.append((frontmatter[0], frontmatter[1]))
    markup_line_offset = text[:markup_start].count("\n")
    safe_markup = _mask_astro_non_executable(text[markup_start:])
    for script_body, script_line in _script_blocks(
        safe_markup, astro_template_comments=True
    ):
        if script_body.strip():
            code_blocks.append((script_body, script_line + markup_line_offset))

    parsed_code = False
    complete = True
    if ts_parser is not None:
        for code, line_offset in code_blocks:
            try:
                tree = ts_parser.parse(code.encode("utf-8", "replace"))
            except Exception:
                complete = False
                continue
            complete = _tree_parse_complete(tree, nodes) and complete
            _walk_ts_tree(tree, rel, "astro", nodes, edges, line_offset=line_offset)
            parsed_code = True
    elif code_blocks:
        complete = False

    if html_parser is not None:
        try:
            markup_tree = html_parser.parse(safe_markup.encode("utf-8", "replace"))
            complete = _tree_parse_complete(markup_tree, nodes) and complete
            edges.extend(_html_import_edges(markup_tree, rel))
        except Exception:
            complete = False
    elif safe_markup.strip():
        complete = False

    if not complete:
        for node in nodes:
            if node.get("kind") == "file":
                node["analysis_status"] = "incomplete"

    # A template-only Astro file is a valid parsed surface even when it contributes
    # no executable symbols.  False means only that executable code existed but no
    # TS parser could process it, matching the Svelte/Vue degradation contract.
    return nodes, edges, complete and (parsed_code or not code_blocks)


def extract_file_svelte(path, rel, svelte_parser, ts_parser):
    """Return (nodes, edges, ok) for a .svelte Single-File Component.

    Extracts the `<script>` block text via the tree-sitter-svelte grammar's
    `script_element -> raw_text` node, then delegates JS/TS symbol extraction to
    `_walk_ts_tree` with the correct file-line offset so start_line/end_line on
    every def/class node are 1-based line numbers in the OUTER .svelte file.

    NEVER-CRASH: missing grammar, missing script block, malformed SFC, or parse
    error → bare file node with no structural edges (recall-safe)."""
    nodes = [{"id": rel, "kind": "file", "path": rel, "language": "svelte"}]
    edges = []
    if svelte_parser is None or ts_parser is None:
        return nodes, edges, False
    try:
        with open(path, "rb") as fh:
            src = fh.read()
        outer_tree = svelte_parser.parse(src)
    except (OSError, ValueError):
        return nodes, edges, False
    outer_complete = _tree_parse_complete(outer_tree, nodes)
    # Find the raw_text node inside the first script_element.  A .svelte file may have
    # no <script> block (template-only component) — that is fine, return bare file node.
    raw_text_node = None
    stack = [outer_tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "raw_text" and n.parent is not None and n.parent.type == "script_element":
            raw_text_node = n
            break
        stack.extend(n.children)
    if raw_text_node is None:
        return nodes, edges, outer_complete   # no script block — bare file node
    # line_offset: the 0-based row of raw_text in the outer file.  Adding it to
    # a JS/TS node's start_point.row + 1 gives the 1-based line in the .svelte file.
    line_offset = raw_text_node.start_point.row
    script_bytes = raw_text_node.text
    if not script_bytes or not script_bytes.strip():
        return nodes, edges, outer_complete   # empty script block — bare file node
    try:
        ts_tree = ts_parser.parse(script_bytes)
    except Exception:
        return nodes, edges, False
    ts_complete = _tree_parse_complete(ts_tree, nodes)
    _walk_ts_tree(ts_tree, rel, "svelte", nodes, edges, line_offset=line_offset)
    return nodes, edges, outer_complete and ts_complete


def extract_file_vue(path, rel, ts_parser):
    """Return (nodes, edges, ok) for a .vue Single-File Component.

    tree-sitter-vue has no pip wheel, so a bounded Vue SFC scanner extracts
    each top-level native lowercase `<script>` / `<script setup>` block before
    delegating to `_walk_ts_tree`. Template markup, custom blocks and PascalCase
    components are presentation surfaces and never enter the executable graph.
    Line offsets are computed from the raw file bytes so start_line/end_line
    on every emitted node are 1-based line numbers in the OUTER .vue file.

    NEVER-CRASH: missing parser, no script block, malformed SFC, or parse
    error → bare file node with no structural edges (recall-safe)."""
    nodes = [{"id": rel, "kind": "file", "path": rel, "language": "vue"}]
    edges = []
    if ts_parser is None:
        return nodes, edges, False
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        # Decode leniently — a real Vue SFC is UTF-8 but the odd one isn't.
        text = raw.decode("utf-8", "replace")
    except (OSError, ValueError):
        return nodes, edges, False
    blocks = _vue_script_blocks(text)
    if not blocks:
        return nodes, edges, True   # no script block — bare file node, no error
    parsed = False
    complete = True
    for script_text, line_offset in blocks:
        if not script_text.strip():
            continue
        try:
            ts_tree = ts_parser.parse(script_text.encode("utf-8", "replace"))
        except Exception:
            complete = False
            continue
        complete = _tree_parse_complete(ts_tree, nodes) and complete
        _walk_ts_tree(ts_tree, rel, "vue", nodes, edges, line_offset=line_offset)
        parsed = True
    if not complete:
        for node in nodes:
            if node.get("kind") == "file":
                node["analysis_status"] = "incomplete"
    return (
        nodes,
        edges,
        complete and (parsed or not any(body.strip() for body, _line in blocks)),
    )


def extract_file_generic(path, rel, label, parser, spec):
    """Return (nodes, edges, ok) for one tree-sitter file, driven by `spec` (the uniform model)."""
    nodes = [{"id": rel, "kind": "file", "path": rel, "language": label}]
    edges = []
    try:
        with open(path, "rb") as fh:
            src = fh.read()
        tree = parser.parse(src)
    except (OSError, ValueError):
        return nodes, edges, False
    complete = _tree_parse_complete(tree, nodes)

    def _def(name, kind, span_node=None):
        sym = f"{rel}::{name}"
        n = {"id": sym, "kind": kind, "name": name, "path": rel, "language": label}
        sl, el = _span(span_node)   # content-free [start_line, end_line]; omitted when unavailable
        if sl is not None:
            n["start_line"], n["end_line"] = sl, el
        nodes.append(n)
        edges.append({"src": rel, "dst": sym, "kind": "contains"})

    defset, classset = spec.get("def", set()), spec.get("class", set())
    callset, callfield = spec.get("call", set()), spec.get("call_field")
    impnodes, impfield = spec.get("import_node", set()), spec.get("import_field")
    reqm = spec.get("require_methods", set())
    # SYMBOL-USE (recall): node types that carry a base-type list / a constructor. Captured as `calls`
    # edges (dst = used type name) so the engine's existing call-resolution precision guards apply.
    sym_use = spec.get("sym_use") or {}
    su_base, su_ctor = sym_use.get("base", set()), sym_use.get("ctor", set())
    # EMBED (Go struct embedding): node types whose EMBEDDED type name is a base-class-class dependency.
    # An embedded field is a `field_declaration` with NO `field_identifier` (just the type) — distinct
    # from a named field `x T` (a noisy type ANNOTATION we exclude). Empty for every other language.
    embed_nodes = spec.get("embed") or set()
    # MIXIN (Ruby include/extend/prepend): callee names whose constant ARGUMENT (the module) is the
    # coupling target — captured instead of the ubiquitous-hub callee name. Empty elsewhere.
    mixin_methods = spec.get("mixin_methods") or set()
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        t = n.type
        # SYMBOL-USE first (a base/ctor node is structurally distinct from def/call/import nodes, so the
        # elif chain below never reaches it — emit its type-name `calls` edge, then fall through to descend).
        if t in su_base:
            for nm in _sym_use_names(n, "base"):
                edges.append({"src": rel, "dst": nm, "kind": "calls"})   # a `calls`-class type dependency
        elif t in su_ctor:
            for nm in _sym_use_names(n, "ctor"):
                edges.append({"src": rel, "dst": nm, "kind": "calls"})
        if t in embed_nodes:
            # Go STRUCT EMBEDDING: a `field_declaration` with NO `field_identifier` is an EMBEDDED type
            # (`struct { Base }` / `struct { *Base }` / `struct { pkg.Base }`), the Go reuse mechanism —
            # a real base-class-class dependency. A NAMED field (`x T`, which HAS a `field_identifier`)
            # is a type ANNOTATION → deliberately excluded (the measured-noisy case). Emit the embedded
            # type's trailing identifier (`pkg.Base` → `Base`) as a content-free `calls` edge so it flows
            # through the resolver's precision guards. (No `continue`: fall through to descend the node.)
            if not any(c.type == "field_identifier" for c in n.children):
                tgt = next((c for c in n.children
                            if c.type in ("type_identifier", "qualified_type", "generic_type",
                                          "pointer_type")), None)
                nm = _trailing_ident(tgt) if tgt is not None else None
                if nm and nm.isidentifier():
                    edges.append({"src": rel, "dst": nm, "kind": "calls"})
        if t in defset or t in classset:
            is_class = t in classset
            if not is_class and spec.get("name_via_declarator"):   # C/C++ function: name is in declarator
                nm = _c_decl_name(n)
            else:
                nm = _text(n.child_by_field_name("name"))
            if nm:
                _def(nm, "class" if is_class else "def", n)   # span the whole def/class node (content-free lines)
        elif t in callset:
            callee = None
            for fld in ((callfield,) if isinstance(callfield, str) else (callfield or ())):
                callee = n.child_by_field_name(fld)
                if callee is not None:
                    break
            if callee is None:                          # fallback: first callee-ish child
                # An identifier/name IS the callee (plain call `f()`); a MEMBER/NAVIGATION expression
                # (`h.process()`, `g.a.b.deep()` — Kotlin/Swift `call_field: None`) is ALSO the callee —
                # _trailing_ident reads its trailing method name (`process`/`deep`), so a member call is
                # recorded, not silently dropped (it was: navigation_expression matched neither test →
                # callee=None → no `calls` edge = a SILENT MISS of the dominant call form in OO code).
                # We do NOT walk into argument_list / value_arguments (those hold the args, never the
                # callee), so no false call edge from a nested argument.
                callee = next((c for c in n.children
                               if "identifier" in c.type or c.type in ("name", "navigation_expression",
                                                                        "member_access_expression", "field_access")),
                              None)
            nm = _trailing_ident(callee) if callee is not None else None
            if nm and nm in reqm:                      # ruby require/require_relative → an import
                mod = _module_specifier(_first_string_in(n.child_by_field_name("arguments") or n))
                if mod:
                    edges.append({"src": rel, "dst": mod, "kind": "imports"})
            elif nm and nm in mixin_methods:
                # ruby MIXIN: `include M` / `extend M` / `prepend M` — couple to the module CONSTANT
                # argument(s), NOT the ubiquitous `include`/`extend`/`prepend` callee. Each argument is a
                # `constant` (`Walkable`) or `scope_resolution` (`Foo::Bar`); `_trailing_ident` reads the
                # bare name (`Bar`). Only constant-shaped args become edges (a non-constant arg — e.g. a
                # dynamic `include some_module` expression — names no defined type → no edge). The bare
                # `include` callee edge is dropped (a hub name with zero coupling signal).
                args = n.child_by_field_name("arguments")
                if args is not None:
                    for a in args.children:
                        if a.type in ("constant", "scope_resolution", "identifier"):
                            mn = _trailing_ident(a)
                            if mn and mn.isidentifier() and mn[:1].isupper():  # a Ruby CONSTANT (module)
                                edges.append({"src": rel, "dst": mn, "kind": "calls"})
            elif nm:
                edges.append({"src": rel, "dst": nm, "kind": "calls"})
        elif t in impnodes and n.child_count > 0:
            # PRECISION: some grammars reuse the import node-TYPE name for the `import` KEYWORD LEAF
            # itself (Kotlin: the statement `import a.b.C` is type `import` AND contains a child of
            # type `import` whose text is the bare keyword `"import"`, child_count==0). Without the
            # `child_count > 0` guard the keyword leaf matched `impnodes`, fell to `_trailing_ident`'s
            # last resort (its own text), and emitted a FALSE `imports` edge to `"import"` — which the
            # resolver then couples to any file literally named `import.*` (a real false collision warn,
            # confirmed on a synthetic repo). A genuine import statement always carries path children
            # (child_count > 0); the bare keyword leaf carries none, so this drops ONLY the false edge
            # and never a real import (no recall loss).
            if impfield:
                # a STRING-LITERAL import path (go/c/cpp): validate it as a content-free reference token, so a
                # `#include "<arbitrary author bytes>"` / a quoted Go import can never store a source fragment.
                mod = _module_specifier(_strip_quotes(_text(n.child_by_field_name(impfield))))
                if mod:
                    edges.append({"src": rel, "dst": mod, "kind": "imports"})
            elif spec.get("import_dotted"):
                # full dotted FQN(s) — Java/C#/Rust/PHP/Kotlin/Swift. A GROUPED import
                # (`use a::b::{X,Y}`, `use A\B\{X,Y}`) emits ONE edge per item (prefix-joined), so the
                # group's members each resolve to the file they name instead of collapsing to the prefix.
                for mod in _dotted_imports(n):
                    edges.append({"src": rel, "dst": mod, "kind": "imports"})
            else:
                mod = _trailing_ident(n)                # identifier from named nodes (not a raw literal)
                if mod:
                    edges.append({"src": rel, "dst": mod, "kind": "imports"})
        stack.extend(n.children)
    return nodes, edges, complete


def _html_import_edges(tree, rel):
    """Local HTML asset edges from a parsed tree; shared by HTML and Astro markup."""
    edges = []
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "attribute":
            aname = aval = None
            quoted = False
            for c in n.children:
                if c.type == "attribute_name":
                    aname = _text(c)
                elif c.type == "quoted_attribute_value":
                    quoted = True
                    aval = _strip_quotes(_text(c))
                elif c.type == "attribute_value":
                    aval = _strip_quotes(_text(c))
            if aname in ("src", "href") and aval and not aval.startswith(
                    ("http://", "https://", "//", "#", "data:", "mailto:", "javascript:")):
                # tree-sitter-html represents an Astro expression such as
                # ``src={privateSourceExpression}`` as a direct, unquoted
                # attribute_value.  It does not expose an Astro expression node, so
                # reject expression delimiters at this syntax boundary.  Quoted
                # values remain static HTML strings (braces inside quotes are literal).
                if not quoted and ("{" in aval or "}" in aval):
                    stack.extend(n.children)
                    continue
                # an asset path is a reference token too: a `<script src="<arbitrary author bytes>">` must not
                # store a source fragment as the edge dst (content-free at the capture point).
                aval = _module_specifier(aval)
                if aval:
                    edges.append({"src": rel, "dst": aval, "kind": "imports"})
        stack.extend(n.children)
    return edges


def extract_file_html(path, rel, parser):
    """HTML/template → `imports` edges to the LOCAL assets it loads (`<script src>`, `<link
    href>`). A template and the JS/CSS it pulls in are coupled (edit either and the page changes).
    External URLs and anchors are skipped. (No defs/calls — HTML's coupling is its referenced
    files.)"""
    nodes = [{"id": rel, "kind": "file", "path": rel, "language": "html"}]
    try:
        with open(path, "rb") as fh:
            tree = parser.parse(fh.read())
    except (OSError, ValueError):
        return nodes, [], False
    complete = _tree_parse_complete(tree, nodes)
    edges = _html_import_edges(tree, rel)
    return nodes, edges, complete


def _strip_css_comments(text, line_comments=False):
    """Blank stylesheet comments in one linear pass, preserving newlines/strings.

    CSS itself has block comments; Sass/Less/Stylus additionally accept ``//`` line
    comments.  The caller enables that dialect rule from the file extension.
    """
    out = []
    i = 0
    quote = None
    escaped = False
    n = len(text or "")
    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("\"", "'", "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            out.extend(("\n" if c == "\n" else " ") for c in "/*")
            i += 2
            while i < n:
                if text[i] == "*" and i + 1 < n and text[i + 1] == "/":
                    out.extend((" ", " "))
                    i += 2
                    break
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            continue
        if line_comments and ch == "/" and i + 1 < n and text[i + 1] == "/":
            out.extend((" ", " "))
            i += 2
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _css_string_end(source, start):
    """Return the byte after one CSS-family quoted string, or EOF."""
    quote = source[start]
    pos = start + 1
    escaped = False
    while pos < len(source):
        ch = source[pos]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == quote:
            return pos + 1
        pos += 1
    return len(source)


def _css_balanced_end(source, open_pos):
    """Skip one nested (), [] or {} group without interpreting its contents."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    opener = source[open_pos] if open_pos < len(source) else ""
    if opener not in pairs:
        return open_pos + 1
    stack = [pairs[opener]]
    pos = open_pos + 1
    while pos < len(source):
        ch = source[pos]
        if ch in ("\"", "'", "`"):
            pos = _css_string_end(source, pos)
            continue
        if ch == "\\":
            pos += 2
            continue
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch == stack[-1]:
            stack.pop()
            if not stack:
                return pos + 1
        elif ch in ")]}":
            # A mismatched closer makes the group structurally uncertain. Consume to EOF so callers never resume
            # scanning attacker-controlled text as if it were a fresh top-level statement.
            return len(source)
        pos += 1
    return len(source)


def _css_statement_structure(source):
    """Return statement delimiters/newlines outside strings and value groups.

    Declaration-vs-selector lookahead needs the next top-level ``{``, ``;`` or
    ``}``. Indentation syntaxes additionally need newlines outside ``()``/``[]``.
    Tokenizing both once keeps the scanners linear and makes malformed, unclosed
    groups fail closed instead of manufacturing statement boundaries inside them.
    """
    delimiters = []
    newlines = []
    stack = []
    pos = 0
    n = len(source)
    while pos < n:
        ch = source[pos]
        if ch in ("\"", "'", "`"):
            pos = _css_string_end(source, pos)
            continue
        if ch == "\\":
            pos += 2
            continue
        if ch == "#" and pos + 1 < n and source[pos + 1] == "{":
            pos = _css_balanced_end(source, pos + 1)
            continue
        if ch in "([":
            stack.append(")" if ch == "(" else "]")
        elif stack and ch == stack[-1]:
            stack.pop()
        elif ch in ")]" and (not stack or ch != stack[-1]):
            # Once a group is malformed there is no trustworthy later statement boundary. Keep only the verified
            # prefix and fail closed for the remainder of this file.
            break
        elif not stack:
            if ch in "{;}":
                delimiters.append(pos)
            elif ch in "\r\n":
                newlines.append(pos)
        pos += 1
    return delimiters, newlines


_CSS_PSEUDO_SELECTOR_VALUES = frozenset({
    "active", "after", "any-link", "autofill", "before", "blank", "buffering",
    "checked", "current", "default", "defined", "dir", "disabled", "empty",
    "enabled", "first", "first-child", "first-letter", "first-line", "first-of-type",
    "focus", "focus-visible", "focus-within", "fullscreen", "future", "global", "has",
    "host", "host-context", "hover", "in-range", "indeterminate", "invalid", "is", "lang",
    "last-child", "last-of-type", "left", "link", "local-link", "modal", "muted",
    "not", "nth-child", "nth-last-child", "nth-last-of-type", "nth-of-type",
    "only-child", "only-of-type", "open", "optional", "out-of-range", "past",
    "paused", "picture-in-picture", "placeholder-shown", "playing", "popover-open",
    "read-only", "read-write", "required", "right", "root", "scope", "seeking", "slotted",
    "stalled", "state", "target", "target-within", "user-invalid", "user-valid", "local", "deep",
    "valid", "visited", "volume-locked", "where",
})


def _css_indented_pseudo_selector(source, value_start):
    """Whether a colon value is a complete pseudo selector on its physical line."""
    n = len(source)
    pos = value_start
    while pos < n and source[pos] in " \t\f":
        pos += 1
    if pos < n and source[pos] == ":":  # legacy pseudo-element spelling, e.g. a::before
        pos += 1
    name_start = pos
    while pos < n and (source[pos].isalnum() or source[pos] in "_-"):
        pos += 1
    pseudo_name = source[name_start:pos].lower()
    if pseudo_name not in _CSS_PSEUDO_SELECTOR_VALUES and not pseudo_name.startswith("-"):
        return False
    while pos < n and source[pos] in " \t\f":
        pos += 1
    if pos >= n or source[pos] in "\r\n":
        return True
    if source[pos] != "(":
        return False
    end = _css_balanced_end(source, pos)
    while end < n and source[end] in " \t\f":
        end += 1
    return end >= n or source[end] in "\r\n"


def _css_declaration_at(
    source, start, statement_delimiters=None, statement_delimiter_index=0,
    newline_terminated=False, statement_newlines=None, statement_newline_index=0,
):
    """Return ``(name, value_start)`` for a property at a statement boundary.

    Sass/Less variables are accepted as property names.  A later top-level ``{``
    distinguishes an identifier-led nested selector such as ``a:hover {`` from a
    declaration; an immediate ``:{`` remains a valid nested declaration value.
    ``statement_delimiters`` is the source-wide linear tokenization used to keep
    repeated lookahead bounded. Indentation syntaxes classify brace-less pseudo
    selectors from their current line and never use a brace on a later line.
    """
    n = len(source)
    pos = start
    if pos >= n:
        return None
    if source[pos] in "$@":
        pos += 1
        name_start = pos
    else:
        name_start = pos
        if not (source[pos].isalpha() or source[pos] in "_-"):
            return None
    while pos < n and (source[pos].isalnum() or source[pos] in "_-"):
        pos += 1
    if pos == name_start:
        return None
    name = source[start:pos].lower()
    while pos < n and source[pos].isspace():
        pos += 1
    if pos >= n or source[pos] != ":":
        return None
    value_start = pos + 1

    # Variables and custom properties are declarations even when their value contains a balanced block after a
    # leading token. Treating that block as a selector would expose directives nested inside an inert value.
    if name.startswith(("$", "@", "--")):
        return name, value_start
    if newline_terminated and _css_indented_pseudo_selector(source, value_start):
        return None

    look = value_start
    while look < n and source[look].isspace() and (
        not newline_terminated or source[look] not in "\r\n"
    ):
        look += 1
    if look < n and source[look] != "{":
        if statement_delimiters is None:
            statement_delimiters, _newlines = _css_statement_structure(source)
        while (
            statement_delimiter_index < len(statement_delimiters)
            and statement_delimiters[statement_delimiter_index] < look
        ):
            statement_delimiter_index += 1
        if statement_delimiter_index < len(statement_delimiters):
            delimiter_pos = statement_delimiters[statement_delimiter_index]
            if source[delimiter_pos] == "{":
                if not newline_terminated:
                    return None
                if statement_newlines is None:
                    _delimiters, statement_newlines = _css_statement_structure(source)
                while (statement_newline_index < len(statement_newlines)
                       and statement_newlines[statement_newline_index] < value_start):
                    statement_newline_index += 1
                line_end = (statement_newlines[statement_newline_index]
                            if statement_newline_index < len(statement_newlines) else n)
                if delimiter_pos < line_end:
                    return None
    return name, value_start


def _css_statement_indent(source, start):
    """Indent width before a statement without rescanning non-whitespace bytes."""
    pos = start - 1
    width = 0
    while pos >= 0 and source[pos] in " \t\f":
        width += 1
        pos -= 1
    return width if pos < 0 or source[pos] in "\r\n" else 0


def _css_next_content(source, newline_pos):
    """Return ``(indent, content_pos)`` for the next nonblank physical line."""
    n = len(source)
    pos = newline_pos + 1
    if source[newline_pos] == "\r" and pos < n and source[pos] == "\n":
        pos += 1
    while pos < n:
        width = 0
        while pos < n and source[pos] in " \t\f":
            width += 1
            pos += 1
        if pos >= n:
            return 0, n
        if source[pos] in "\r\n":
            if source[pos] == "\r" and pos + 1 < n and source[pos + 1] == "\n":
                pos += 2
            else:
                pos += 1
            continue
        return width, pos
    return 0, n


def _css_declaration_bounds(
    source, value_start, newline_terminated=False, declaration_indent=0
):
    """Return ``(value_end, resume_at)`` for one possibly nested declaration value."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    pos = value_start
    n = len(source)
    while pos < n:
        ch = source[pos]
        if ch in ("\"", "'", "`"):
            pos = _css_string_end(source, pos)
            continue
        if ch == "\\":
            pos += 2
            continue
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        elif ch == "}" and not stack:
            return pos, pos
        elif ch == ";" and not stack:
            return pos, pos + 1
        elif ch in "\r\n" and newline_terminated and not stack:
            next_indent, next_content = _css_next_content(source, pos)
            if next_indent <= declaration_indent:
                return pos, next_content
            # Jump over the whole blank-line run once. Rechecking every suffix here made N blank lines O(N²).
            pos = next_content
            continue
        pos += 1
    return n, n


def _css_directive_refs(text, ext):
    """Quoted CSS/Sass/Less directive targets outside declaration values."""
    source = text or ""
    n = len(source)
    refs = []
    allowed = {"import", "use", "forward"} if ext in {"scss", "sass"} else {"import"}
    newline_terminated = ext in {"sass", "styl"}
    statement_delimiters, statement_newlines = _css_statement_structure(source)
    statement_delimiter_index = 0
    statement_newline_index = 0
    i = 0
    statement_start = True

    while i < n:
        while (
            statement_delimiter_index < len(statement_delimiters)
            and statement_delimiters[statement_delimiter_index] < i
        ):
            statement_delimiter_index += 1
        while (
            statement_newline_index < len(statement_newlines)
            and statement_newlines[statement_newline_index] < i
        ):
            statement_newline_index += 1
        ch = source[i]
        if ch in "\r\n":
            if (newline_terminated
                    and statement_newline_index < len(statement_newlines)
                    and statement_newlines[statement_newline_index] == i):
                statement_start = True
            i += 1
            continue
        if ch in " \t\f":
            i += 1
            continue

        if statement_start:
            declaration = _css_declaration_at(
                source, i, statement_delimiters, statement_delimiter_index,
                newline_terminated=newline_terminated,
                statement_newlines=statement_newlines,
                statement_newline_index=statement_newline_index,
            )
            if declaration is not None:
                _name, value_start = declaration
                _value_end, i = _css_declaration_bounds(
                    source,
                    value_start,
                    newline_terminated=newline_terminated,
                    declaration_indent=_css_statement_indent(source, i),
                )
                statement_start = True
                continue

        if ch in ("\"", "'", "`"):
            i = _css_string_end(source, i)
            statement_start = False
            continue
        if (ch in "{;}"
                and statement_delimiter_index < len(statement_delimiters)
                and statement_delimiters[statement_delimiter_index] == i):
            statement_start = True
            i += 1
            continue
        if ch == "#" and i + 1 < n and source[i + 1] == "{":
            i = _css_balanced_end(source, i + 1)
            statement_start = False
            continue

        if not statement_start or ch != "@":
            statement_start = False
            i += 1
            continue

        name_start = i + 1
        pos = name_start
        while pos < n and (source[pos].isalnum() or source[pos] in "_-"):
            pos += 1
        name = source[name_start:pos].lower()
        # A quoted target may follow the at-keyword without whitespace after
        # minification (`@import"./x.css"`).  The quote is a CSS token
        # boundary, so accepting it cannot turn a longer at-keyword into an
        # import. Less also permits its option group directly after import.
        boundary = source[pos] if pos < n else ""
        if (
            name not in allowed
            or pos >= n
            or not (
                boundary.isspace()
                or boundary in ("\"", "'")
                or (ext == "less" and name == "import" and boundary == "(")
            )
        ):
            statement_start = False
            i = max(pos, i + 1)
            continue
        while pos < n and source[pos].isspace():
            pos += 1

        if ext == "less" and name == "import" and pos < n and source[pos] == "(":
            option_end = _css_balanced_end(source, pos)
            if option_end >= n and (n == 0 or source[n - 1] != ")"):
                break
            pos = option_end
            while pos < n and source[pos].isspace():
                pos += 1

        if source[pos:pos + 3].lower() == "url" and pos + 3 < n and source[pos + 3] == "(":
            pos += 4
            while pos < n and source[pos].isspace():
                pos += 1
        if pos >= n or source[pos] not in ("\"", "'"):
            statement_start = False
            i = max(pos, i + 1)
            continue

        next_pos = _css_string_end(source, pos)
        if next_pos <= n and next_pos > pos + 1 and source[next_pos - 1] == source[pos]:
            refs.append(source[pos + 1:next_pos - 1])
        i = next_pos
        statement_start = False
    return refs


def _css_composes_refs(text, ext=""):
    """CSS Modules ``composes`` refs from real declarations in one linear scan."""
    refs = []
    source = text or ""
    n = len(source)
    i = 0
    statement_start = True
    newline_terminated = ext in {"sass", "styl"}
    statement_delimiters, statement_newlines = _css_statement_structure(source)
    statement_delimiter_index = 0
    statement_newline_index = 0

    def ident_char(ch):
        return ch.isalnum() or ch in "_-"

    def composes_ref(value_start, value_end):
        pos = value_start
        stack = []
        pairs = {"(": ")", "[": "]", "{": "}"}
        while pos < value_end:
            ch = source[pos]
            if ch in ("\"", "'", "`"):
                pos = _css_string_end(source, pos)
                continue
            if ch == "#" and pos + 1 < value_end and source[pos + 1] == "{":
                pos = _css_balanced_end(source, pos + 1)
                continue
            if ch in pairs:
                stack.append(pairs[ch])
                pos += 1
                continue
            if stack and ch == stack[-1]:
                stack.pop()
                pos += 1
                continue
            from_end = pos + len("from")
            if (
                not stack
                and source[pos:from_end].lower() == "from"
                and (pos == value_start or not ident_char(source[pos - 1]))
                and (from_end >= value_end or not ident_char(source[from_end]))
            ):
                target = from_end
                while target < value_end and source[target].isspace():
                    target += 1
                if target < value_end and source[target] in ("\"", "'"):
                    target_end = _css_string_end(source, target)
                    if (
                        target_end <= value_end
                        and target_end > target + 1
                        and source[target_end - 1] == source[target]
                    ):
                        return source[target + 1:target_end - 1]
            pos += 1
        return None

    while i < n:
        while (
            statement_delimiter_index < len(statement_delimiters)
            and statement_delimiters[statement_delimiter_index] < i
        ):
            statement_delimiter_index += 1
        while (
            statement_newline_index < len(statement_newlines)
            and statement_newlines[statement_newline_index] < i
        ):
            statement_newline_index += 1
        ch = source[i]
        if ch in "\r\n":
            if (newline_terminated
                    and statement_newline_index < len(statement_newlines)
                    and statement_newlines[statement_newline_index] == i):
                statement_start = True
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue
        if ch in ("\"", "'", "`"):
            i = _css_string_end(source, i)
            statement_start = False
            continue
        if (ch in "{;}"
                and statement_delimiter_index < len(statement_delimiters)
                and statement_delimiters[statement_delimiter_index] == i):
            statement_start = True
            i += 1
            continue
        if ch == "#" and i + 1 < n and source[i + 1] == "{":
            i = _css_balanced_end(source, i + 1)
            statement_start = False
            continue

        if statement_start:
            declaration = _css_declaration_at(
                source, i, statement_delimiters, statement_delimiter_index,
                newline_terminated=newline_terminated,
                statement_newlines=statement_newlines,
                statement_newline_index=statement_newline_index,
            )
            if declaration is not None:
                name, value_start = declaration
                value_end, resume_at = _css_declaration_bounds(
                    source,
                    value_start,
                    newline_terminated=newline_terminated,
                    declaration_indent=_css_statement_indent(source, i),
                )
                if name == "composes":
                    ref = composes_ref(value_start, value_end)
                    if ref is not None:
                        refs.append(ref)
                i = resume_at
                statement_start = True
                continue

        statement_start = False
        i += 1
    return refs


def extract_file_css(path, rel):
    """Stylesheet file node plus high-confidence local dependency directives.

    Captures quoted CSS/Less imports, Sass use/forward directives, and CSS Modules
    composes-from references.  Selectors, declarations and arbitrary url() assets
    deliberately do not become symbols/couplings.  Bare paths are normalized to
    same-directory relative paths (the stylesheet resolution rule), avoiding a
    repo-wide basename fan-out.  Remote/data/builtin schemes remain inert.
    """
    nodes = [{"id": rel, "kind": "file", "path": rel, "language": "css"}]
    edges = []
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return nodes, edges, False

    stripped = _strip_css_comments(
        text,
        line_comments=rel.lower().endswith((".scss", ".sass", ".less", ".styl")),
    )
    ext = rel.lower().rsplit(".", 1)[-1] if "." in rel else ""
    refs = _css_directive_refs(stripped, ext)
    refs.extend(_css_composes_refs(stripped, ext))
    for raw in refs:
        ref = (raw or "").strip()
        low = ref.lower()
        if not ref or low.startswith(("http://", "https://", "//", "data:", "sass:", "#")):
            continue
        # Query/hash suffixes affect bundling, not the target file coordinate.
        ref = ref.split("?", 1)[0].split("#", 1)[0].strip()
        if not ref:
            continue
        if not ref.startswith(("./", "../", "/")):
            ref = "./" + ref
        ref = _module_specifier(ref)
        if ref:
            edges.append({"src": rel, "dst": ref, "kind": "imports"})
    return nodes, edges, True


# ---------------------------------------------------------------------------
# Elixir extractor (bespoke — defmodule/def/defp/defmacro/defmacrop are all
# `call` nodes distinguished by first-child identifier text, NOT by node type,
# so _GENERIC_SPEC's type-dispatch cannot handle them)
# ---------------------------------------------------------------------------

# Keywords that introduce a new named definition.  defmacrop is the private-macro
# variant; all five produce first-class symbols a PR can touch in isolation.
_ELIXIR_DEF_KEYWORDS = frozenset({"defmodule", "def", "defp", "defmacro", "defmacrop"})

# Keywords whose call nodes we SKIP for `calls` edges (they are declarations /
# directives, not cross-module calls the engine cares about).
_ELIXIR_SKIP_CALL_KEYWORDS = frozenset({
    "defmodule", "def", "defp", "defmacro", "defmacrop",
    "alias", "import", "use", "require",
    "do", "end",
})


def _elixir_def_name(call_node):
    """Return (name, kind) for an Elixir `call` node whose first child is a def-keyword,
    or (None, None) when the name cannot be cleanly read.

    defmodule MyApp.Router  ->  ('MyApp.Router', 'class')
    def get_user(id)        ->  ('get_user',      'def')
    def simple_fn, do: :ok  ->  ('simple_fn',     'def')

    Elixir module names are `alias` nodes (dot-joined capitalized segments);
    function names live as an inner `call` node (name + args) or a bare `identifier`.
    A name we cannot cleanly read returns (None, None) -- skip it, never mint junk.
    """
    children = call_node.children
    if not children or children[0].type != "identifier":
        return None, None
    keyword = _text(children[0])
    if keyword not in _ELIXIR_DEF_KEYWORDS:
        return None, None

    is_module = keyword == "defmodule"
    kind = "class" if is_module else "def"

    # The arguments node is child[1] when present
    args_node = children[1] if len(children) > 1 and children[1].type == "arguments" else None
    if args_node is None:
        return None, None

    args_children = args_node.children
    if not args_children:
        return None, None

    first = args_children[0]

    if is_module:
        # `defmodule MyApp.Accounts do` -- first arg is an `alias` node (fully-qualified module name)
        # or a bare `identifier` for a simple unqualified module name.
        if first.type == "alias":
            name = _text(first)   # 'MyApp.Accounts' -- the canonical module name, content-free
            return (name, kind) if name and (name.isidentifier() or "." in name) else (None, None)
        elif first.type == "identifier":
            name = _text(first)
            return (name, kind) if name and name.isidentifier() else (None, None)
        return None, None

    # def / defp / defmacro / defmacrop
    # Pattern A: `def get_user(id)` -- first arg is a nested `call` node: `get_user(id)`
    #   The call's first child is an `identifier` holding the function name.
    if first.type == "call":
        fn_children = first.children
        if fn_children and fn_children[0].type == "identifier":
            name = _text(fn_children[0])
            return (name, kind) if name and name.isidentifier() else (None, None)
        return None, None

    # Pattern B: `def simple_fn, do: :ok` (no-arg function with inline do) -- first arg is `identifier`
    if first.type == "identifier":
        name = _text(first)
        return (name, kind) if name and name.isidentifier() else (None, None)

    return None, None


def extract_file_elixir(path, rel, parser):
    """Return (nodes, edges, ok) for one .ex/.exs file.

    Walks the tree-sitter parse tree and for each `call` node whose first-child
    identifier text is in {defmodule, def, defp, defmacro, defmacrop} mints a
    `class`-kind node (for modules) or `def`-kind node (for functions/macros)
    with a content-free line span.

    Cross-module `calls` edges are emitted for `Module.function(...)` call-sites
    (dot-call nodes), where the callee module is an `alias` (a capitalized,
    dot-joined name).  Plain local calls are cheap to capture too and emitted as
    `calls` edges (dst = function name, consistent with other languages).
    Directives (alias/import/use/require/defmodule/def/defp/defmacro/defmacrop)
    are excluded from call-edge emission.

    NEVER-CRASH: any parse or I/O error returns the bare file node without edges.
    CONTENT-FREE: only names (identifiers / module aliases) and line numbers enter
    the graph -- never source text, comments, or literal values."""
    nodes = [{"id": rel, "kind": "file", "path": rel, "language": "elixir"}]
    edges = []
    if parser is None:
        return nodes, edges, False
    try:
        with open(path, "rb") as fh:
            src = fh.read()
        tree = parser.parse(src)
    except (OSError, ValueError):
        return nodes, edges, False
    complete = _tree_parse_complete(tree, nodes)

    def _def(name, kind, span_node=None):
        sym = f"{rel}::{name}"
        n = {"id": sym, "kind": kind, "name": name, "path": rel, "language": "elixir"}
        sl, el = _span(span_node)
        if sl is not None:
            n["start_line"], n["end_line"] = sl, el
        nodes.append(n)
        edges.append({"src": rel, "dst": sym, "kind": "contains"})

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type != "call":
            stack.extend(node.children)
            continue

        children = node.children
        if not children:
            stack.extend(children)
            continue

        first_child = children[0]

        # -- DEFINITION: first child is an identifier matching a def keyword --
        if first_child.type == "identifier":
            keyword = _text(first_child)
            if keyword in _ELIXIR_DEF_KEYWORDS:
                name, kind = _elixir_def_name(node)
                if name:
                    _def(name, kind, node)
                # Always descend into the do_block to find nested defs
                stack.extend(node.children)
                continue

            # -- PLAIN CALL: `local_fn(args)` -- keyword is the callee --
            # Excludes directives (alias/import/use/require) and def-keywords above.
            if keyword not in _ELIXIR_SKIP_CALL_KEYWORDS:
                edges.append({"src": rel, "dst": keyword, "kind": "calls"})
            stack.extend(node.children)
            continue

        # -- DOT-CALL: `Module.function(args)` -- first child is a `dot` node --
        # Structure: call -> [dot(alias . identifier), arguments]
        # We emit a `calls` edge to the trailing function name (content-free).
        # The module name (`alias`) itself is also a useful coupling signal:
        # emit a second edge to the module alias text so module-level coupling
        # is visible even if the function name fans out (hub-dampening applies).
        if first_child.type == "dot":
            dot_children = first_child.children
            # dot_children: [alias_or_identifier, '.', identifier]
            fn_name = None
            mod_name = None
            for dc in dot_children:
                if dc.type == "identifier":
                    fn_name = _text(dc)
                elif dc.type == "alias":
                    mod_name = _text(dc)
            if fn_name and fn_name.isidentifier():
                edges.append({"src": rel, "dst": fn_name, "kind": "calls"})
            if mod_name and (mod_name.isidentifier() or "." in mod_name):
                edges.append({"src": rel, "dst": mod_name, "kind": "calls"})
        stack.extend(node.children)

    return nodes, edges, complete
