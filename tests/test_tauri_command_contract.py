#!/usr/bin/env python3
"""TAURI COMMAND CONTRACT GATE.

Tauri command contracts are an AI-era cross-dir blind spot: Rust backend commands live
under `src-tauri`, while frontend code invokes them from JS/TS. There is no import/call
edge between those files, but the command name is a real contract.

This gate pins the precision-first extractor:
  1. Crown jewel: `#[tauri::command] fn greet` couples to official Tauri `invoke("greet")`.
  2. Alias/namespace imports work, but arbitrary local `invoke("greet")` calls do not.
  3. Dynamic command names, comments, and plain Rust functions stay silent; duplicate
     definitions/references remain as explicitly ambiguous, non-adjacent evidence.
  4. Content-free: secret body/comment text never appears in the graph.
  5. Additive: a non-Tauri repo produces no tauri_command nodes/edges.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import code_graph_extract as X  # noqa: E402
import _cg_tauri as T  # noqa: E402


def _w(d: str, rel: str, body: str) -> None:
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)


def _build(files: dict[str, str]) -> dict:
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            _w(d, rel, body)
        return X.build_graph(d)


def _nodes_of_kind(g: dict, kind: str) -> dict[str, dict]:
    return {n["id"]: n for n in g["nodes"] if n.get("kind") == kind}


def _edges_to(g: dict, dst: str, kind: str) -> list[str]:
    return [e["src"] for e in g["edges"] if e.get("dst") == dst and e.get("kind") == kind]


def _edge_records_to(g: dict, dst: str, kind: str) -> list[dict]:
    return [
        e for e in g["edges"]
        if e.get("dst") == dst and e.get("kind") == kind
    ]


def _direct_code_edges_between(g: dict, a: str, b: str) -> list[dict]:
    out = []
    for e in g["edges"]:
        if e.get("kind") not in ("calls", "imports"):
            continue
        s = str(e.get("src", ""))
        d = str(e.get("dst", ""))
        if (a in s and b in d) or (b in s and a in d):
            out.append(e)
    return out


def _has_tauri_edge(g: dict, name: str, kind: str, src_part: str) -> bool:
    dst = f"tauri_command::{name}"
    return any(src_part in src for src in _edges_to(g, dst, kind))


def _check_rust_imports(check) -> None:
    # The legacy path must not start accepting bare attributes without a grammar.
    fallback = (
        "use tauri::command;\n#[command] fn bare() {}\n"
        "#[tauri::command] fn qualified() {}\n"
    )
    check(T._command_defs(fallback) == {"qualified"},
          "(6) grammar-free fallback must remain qualified-attribute-only")
    check(T._command_defs(fallback, object()) == {"qualified"},
          "(6) unusable Rust grammar must preserve only qualified attributes")

    rust_language = X._ts_languages().get("rust")
    if rust_language is None:
        print("TAURI COMMAND IMPORT CASES: SKIPPED (Rust grammar unavailable)")
        return
    check(T._command_defs(fallback + "fn broken(", rust_language) == {"qualified"},
          "(6) parse errors must not enable imported bare attributes")
    check(T._command_defs("fn outer() { #[tauri::command] fn nested() {} }", rust_language)
          == {"nested"}, "(6) existing qualified nested-function extraction must remain unchanged")

    positive = {
        "named": "use tauri::command; #[command] fn save_file() {}",
        "alias": "use tauri::command as tc; #[tc] fn save_file() {}",
        "group": "use tauri::{command, State}; #[command] fn save_file() {}",
        "nested-first": (
            "use tauri::{command, ipc::{Channel, CommandScope}};\n"
            "#[command] fn save_file() {}"
        ),
        "nested-last": (
            "use tauri::{ipc::{Channel, CommandScope}, command};\n"
            "#[command] fn save_file() {}"
        ),
        "nested-alias": (
            "use tauri::{ipc::{Channel, CommandScope}, command as tc};\n"
            "#[tc(rename_all = \"snake_case\")] pub async fn save_file() {}"
        ),
        "comments-and-lines": (
            "use tauri::{\n /* import comment */ command as tc,\n State,\n};\n"
            "#[tc]\n// attribute comment\npub fn save_file() {}"
        ),
    }
    for label, source in positive.items():
        check(T._command_defs(source, rust_language) == {"save_file"},
              f"(6) explicit Rust import case failed: {label}")

    negative = {
        "no-import": "#[command] fn fake() {}",
        "unrelated": "use other::command; #[command] fn fake() {}",
        "local-path": "use crate::tauri::command; #[command] fn fake() {}",
        "other-tauri-item": "use tauri::plugin::command; #[command] fn fake() {}",
        "glob": "use tauri::*; #[command] fn fake() {}",
        "discarded": "use tauri::command as _; #[command] fn fake() {}",
        "reexport": "pub use tauri::command; #[command] fn fake() {}",
        "conditional-import": "#[cfg(any())] use tauri::command; #[command] fn fake() {}",
        "conditional-attribute": "use tauri::command; #[cfg_attr(any(), command)] fn fake() {}",
        "conflicting-import": (
            "use tauri::command; use other::command; #[command] fn fake() {}"
        ),
        "conflicting-alias": (
            "use tauri::command as tc; use other::command as tc; #[tc] fn fake() {}"
        ),
        "local-macro": (
            "use tauri::command; macro_rules! command { () => {} }\n"
            "#[command] fn fake() {}"
        ),
        "local-tauri": (
            "mod tauri {} use tauri::command; #[command] fn fake() {}"
        ),
        "tauri-alias-shadow": (
            "use other as tauri; use tauri::command; #[command] fn fake() {}"
        ),
        "comment": "/* use tauri::command; */ #[command] fn fake() {}",
        "string": (
            'const DOC: &str = "use tauri::command;"; #[command] fn fake() {}'
        ),
        "raw-string": (
            'const DOC: &str = r#"use tauri::command; #[command] fn ghost() {}"#;\n'
            "#[command] fn fake() {}"
        ),
        "block-import": "fn setup() { use tauri::command; } #[command] fn fake() {}",
        "macro-import": (
            "macro_rules! setup { () => { use tauri::command; } }\n"
            "#[command] fn fake() {}"
        ),
        "parse-error": "use tauri::command; #[command] fn fake( {",
    }
    for label, source in negative.items():
        check(not T._command_defs(source, rust_language),
              f"(7) unrelated/unsupported Rust import must remain silent: {label}")

    scoped = {
        "parent-does-not-leak": (
            "use tauri::command; #[command] fn real() {}\n"
            "mod child { #[command] fn fake() {} }"
        ),
        "sibling-does-not-leak": (
            "mod a { use tauri::command; #[command] fn real() {} }\n"
            "mod b { #[command] fn fake() {} }"
        ),
        "child-shadow": (
            "use tauri::command; #[command] fn real() {}\n"
            "mod child { use other::command; #[command] fn fake() {} }"
        ),
        "child-alias-shadow": (
            "use tauri::command as tc; #[tc] fn real() {}\n"
            "mod child { use other::command as tc; #[tc] fn fake() {} }"
        ),
        "child-import-does-not-leak-out": (
            "mod child { use tauri::command as tc; #[tc] fn real() {} }\n"
            "#[tc] fn fake() {}"
        ),
    }
    for label, source in scoped.items():
        check(T._command_defs(source, rust_language) == {"real"},
              f"(8) Rust attribute import escaped its module: {label}")

    # Exercise the public build_graph caller, not only the private AST helper.
    imported = _build({
        "src-tauri/src/commands.rs": (
            "use tauri::{ipc::{Channel, CommandScope}, command as tc};\n"
            "#[tc] pub fn imported() -> &'static str {\n"
            '  "IMPORTED_COMMAND_BODY_SHOULD_NOT_LEAK"\n}\n'
            'const DOC: &str = r#"use tauri::command; #[command] fn ghost() {}"#;\n'
        ),
        "src-tauri/src/other.rs": "#[tc] fn unrelated() {}\n",
        "src/main.ts": (
            "import { invoke } from '@tauri-apps/api/core';\n"
            "invoke('imported'); invoke('unrelated'); invoke('ghost');\n"
        ),
    })
    check(set(_nodes_of_kind(imported, "app_command")) == {"tauri_command::imported"},
          "(9) build_graph must forward its Rust language without leaking file imports")
    check(_has_tauri_edge(imported, "imported", "alters", "commands.rs")
          and _has_tauri_edge(imported, "imported", "queries", "main.ts"),
          "(9) imported Rust command must retain both contract edges")
    check("IMPORTED_COMMAND_BODY_SHOULD_NOT_LEAK" not in json.dumps(imported),
          "(9) imported command bodies must stay out of graph output")

    duplicate = _build({
        "src-tauri/src/a.rs": "use tauri::command; #[command] fn duplicated() {}\n",
        "src-tauri/src/b.rs": "#[tauri::command] fn duplicated() {}\n",
        "src/main.ts": "import { invoke } from '@tauri-apps/api/core'; invoke('duplicated');\n",
    })
    duplicate_node = _nodes_of_kind(duplicate, "app_command").get("tauri_command::duplicated")
    duplicate_edges = (
        _edge_records_to(duplicate, "tauri_command::duplicated", "alters")
        + _edge_records_to(duplicate, "tauri_command::duplicated", "queries")
    )
    check(duplicate_node is not None
          and (duplicate_node.get("provenance") or {}).get("ambiguous") is True,
          "(10) imported and qualified duplicate definitions must remain ambiguous")
    check(len(duplicate_edges) == 3
          and all(e.get("reference_status") == "ambiguous" for e in duplicate_edges),
          "(10) imported duplicate must retain every anchored ambiguous edge")
    print(f"TAURI COMMAND IMPORT CASES: {len(positive) + len(negative) + len(scoped)} AST cases + 2 graph cases")


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # (1) Crown jewel: backend command definition plus official frontend invoke.
    g1 = _build({
        "src-tauri/src/commands.rs": (
            "const SECRET_BODY_SHOULD_NOT_LEAK: &str = \"sk_live_hidden\";\n"
            "#[tauri::command]\n"
            "pub async fn greet(name: String) -> String {\n"
            "    format!(\"hello {name}\")\n"
            "}\n"
        ),
        "src/App.tsx": (
            "import { invoke } from '@tauri-apps/api/core';\n"
            "export async function run() {\n"
            "  return invoke('greet', { name: 'Ada' });\n"
            "}\n"
        ),
    })
    nodes1 = _nodes_of_kind(g1, "app_command")
    check("tauri_command::greet" in nodes1, f"(1) tauri_command::greet node missing; nodes={list(nodes1)}")
    check(_has_tauri_edge(g1, "greet", "alters", "commands.rs"),
          f"(1) alters edge from Rust command missing; alters={_edges_to(g1, 'tauri_command::greet', 'alters')}")
    check(_has_tauri_edge(g1, "greet", "queries", "App.tsx"),
          f"(1) queries edge from official invoke missing; queries={_edges_to(g1, 'tauri_command::greet', 'queries')}")
    check(
        all(
            "reference_status" not in e
            for kind in ("alters", "queries")
            for e in _edge_records_to(g1, "tauri_command::greet", kind)
        ),
        "(1) unambiguous Tauri edges must remain unchanged (no status marker)",
    )
    check(not _direct_code_edges_between(g1, "commands.rs", "App.tsx"),
          f"(1) coupling should be via command key, not a direct code edge; got={_direct_code_edges_between(g1, 'commands.rs', 'App.tsx')}")

    # (2) Alias and namespace imports are official Tauri invokes.
    g2 = _build({
        "src-tauri/src/lib.rs": (
            "#[tauri::command]\n"
            "fn save_file() {}\n"
        ),
        "src/save.ts": (
            "import { invoke as callTauri } from '@tauri-apps/api/tauri';\n"
            "import * as core from '@tauri-apps/api/core';\n"
            "callTauri(\"save_file\");\n"
            "core.invoke(`save_file`);\n"
        ),
    })
    check(_has_tauri_edge(g2, "save_file", "queries", "save.ts"),
          f"(2) alias/namespace invoke should query save_file; queries={_edges_to(g2, 'tauri_command::save_file', 'queries')}")

    # (3a) A local function named invoke is not enough: the official Tauri import is required.
    g3 = _build({
        "src-tauri/src/lib.rs": (
            "#[tauri::command]\n"
            "fn greet() {}\n"
        ),
        "src/local.ts": (
            "function invoke(name: string) { return name; }\n"
            "invoke('greet');\n"
        ),
    })
    check(_has_tauri_edge(g3, "greet", "alters", "lib.rs"),
          "(3a) setup sanity: Rust command def should still emit alters")
    check(not _edges_to(g3, "tauri_command::greet", "queries"),
          f"(3a) local invoke must not query tauri command; queries={_edges_to(g3, 'tauri_command::greet', 'queries')}")

    # (3b) Dynamic names, commented-out imports/calls, and plain Rust functions stay silent.
    g4 = _build({
        "src-tauri/src/lib.rs": (
            "fn plain() {}\n"
            "/* #[tauri::command]\n"
            "fn ghost() {}\n"
            "*/\n"
            "#[tauri::command]\n"
            "fn real_cmd() {}\n"
        ),
        "src/main.ts": (
            "/* import { invoke } from '@tauri-apps/api/core'; invoke('real_cmd'); */\n"
            "import { invoke } from '@tauri-apps/api/core';\n"
            "const cmd = 'real_cmd';\n"
            "invoke(cmd);\n"
        ),
    })
    check("tauri_command::plain" not in _nodes_of_kind(g4, "app_command"),
          "(3b) plain Rust fn must not mint a tauri command")
    check("tauri_command::ghost" not in _nodes_of_kind(g4, "app_command"),
          "(3b) commented command attribute must not mint a tauri command")
    check(_has_tauri_edge(g4, "real_cmd", "alters", "lib.rs"),
          "(3b) setup sanity: real_cmd definition should emit alters")
    check(not _edges_to(g4, "tauri_command::real_cmd", "queries"),
          f"(3b) dynamic/comment-only invoke must not emit queries; queries={_edges_to(g4, 'tauri_command::real_cmd', 'queries')}")

    # (3c) Duplicate command definers/references are retained as inert evidence.
    g5 = _build({
        "src-tauri/src/a.rs": "#[tauri::command]\nfn duplicated() {}\n",
        "src-tauri/src/b.rs": "#[tauri::command]\nfn duplicated() {}\n",
        "src/App.ts": "import { invoke } from '@tauri-apps/api/core';\ninvoke('duplicated');\n",
    })
    duplicate_node = _nodes_of_kind(g5, "app_command").get(
        "tauri_command::duplicated"
    )
    check(
        duplicate_node is not None
        and (duplicate_node.get("provenance") or {}).get("ambiguous") is True,
        "(3c) duplicate command must remain as explicit ambiguous evidence",
    )
    check(
        (g5.get("metrics") or {}).get("ambiguous_reference_count", 0) >= 1,
        "(3c) duplicate command must increment ambiguity observability",
    )
    duplicate_edges = (
        _edge_records_to(g5, "tauri_command::duplicated", "alters")
        + _edge_records_to(g5, "tauri_command::duplicated", "queries")
    )
    check(
        {e["src"] for e in duplicate_edges}
        == {"src-tauri/src/a.rs", "src-tauri/src/b.rs", "src/App.ts"},
        f"(3c) duplicate command must preserve both definitions and the literal invoke; "
        f"edges={duplicate_edges}",
    )
    check(
        duplicate_edges
        and all(e.get("reference_status") == "ambiguous" for e in duplicate_edges),
        f"(3c) every duplicate command edge must be explicitly ambiguous; edges={duplicate_edges}",
    )
    duplicate_file_statuses = {
        n.get("path"): n.get("analysis_status")
        for n in g5["nodes"]
        if n.get("kind") == "file"
        and n.get("path") in {
            "src-tauri/src/a.rs", "src-tauri/src/b.rs", "src/App.ts"
        }
    }
    check(
        set(duplicate_file_statuses)
        == {"src-tauri/src/a.rs", "src-tauri/src/b.rs", "src/App.ts"}
        and set(duplicate_file_statuses.values()) == {"ambiguous"},
        f"(3c) all files touching the ambiguous command must be locally Unknown; "
        f"statuses={duplicate_file_statuses}",
    )

    # (4) Content-free: body/comment secret strings never appear in graph JSON.
    raw1 = json.dumps(g1, sort_keys=True)
    check("SECRET_BODY_SHOULD_NOT_LEAK" not in raw1 and "sk_live_hidden" not in raw1,
          "(4) content-free: Rust body/comment strings must not appear in graph output")

    # (5) Additive: non-Tauri source should not produce tauri command contracts.
    g6 = _build({
        "backend/api.py": "def greet():\n    return 'ok'\n",
        "web/app.ts": "export function invoke(x: string) { return x; }\n",
    })
    check(not _nodes_of_kind(g6, "app_command"),
          f"(5) pure non-Tauri repo should not mint app_command nodes; nodes={list(_nodes_of_kind(g6, 'app_command'))}")
    check(not [e for e in g6["edges"] if str(e.get("dst", "")).startswith("tauri_command::")],
          "(5) pure non-Tauri repo should not emit tauri_command edges")

    _check_rust_imports(check)

    if failures:
        print("TAURI COMMAND CONTRACT GATE: FAIL")
        for f in failures:
            print("  -", f)
        return 1

    print("TAURI COMMAND CONTRACT GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
