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

    if failures:
        print("TAURI COMMAND CONTRACT GATE: FAIL")
        for f in failures:
            print("  -", f)
        return 1

    print("TAURI COMMAND CONTRACT GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
