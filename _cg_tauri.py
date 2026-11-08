"""Tauri command-contract coupling extraction.

Tauri apps split one contract across two tiers:

  - Rust backend: `#[tauri::command] fn greet(...)`
  - JS/TS frontend: `invoke("greet", ...)`

There is no import edge between `src-tauri/...rs` and `src/...ts`, but changing one
side can break the other. This substrate emits a shared contract key so Veripsa can
see that cross-directory, cross-language coupling without reading or storing bodies.

Precision discipline:
  - command definitions require `#[tauri::command]`, or a same-module explicit
    import of that attribute when the Rust grammar is available;
  - frontend references require `invoke` imported from the official Tauri API module;
  - references are literal command names only, and only to locally-defined commands;
  - duplicate command definitions/references remain as explicitly ambiguous,
    evidence-only edges.

Content-free output: command names, file paths, and edge kinds only.
"""
from __future__ import annotations

import os
import re
from typing import Any

from _cg_io import _mark_incomplete, _read_capped

_RUST_EXT = ".rs"
_FRONTEND_EXTS = frozenset({".ts", ".tsx", ".js", ".jsx", ".svelte", ".vue"})
_TAURI_MODULE_RE = r"@tauri-apps/api/(?:core|tauri)"

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")

_COMMAND_DEF_RE = re.compile(
    r"#\s*\[\s*tauri\s*::\s*command(?:\s*\([^]]*\))?\s*\]\s*"
    r"(?:pub(?:\s*\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.MULTILINE,
)

_NAMED_IMPORT_RE = re.compile(
    r"\bimport\s*\{([^}]+)\}\s*from\s*[\"']" + _TAURI_MODULE_RE + r"[\"']",
    re.MULTILINE,
)
_NAMESPACE_IMPORT_RE = re.compile(
    r"\bimport\s+\*\s+as\s+([A-Za-z_$][A-Za-z0-9_$]*)\s+from\s*[\"']"
    + _TAURI_MODULE_RE + r"[\"']",
    re.MULTILINE,
)
_REQUIRE_DESTRUCTURE_RE = re.compile(
    r"\b(?:const|let|var)\s*\{([^}]+)\}\s*=\s*require\s*\(\s*[\"']"
    + _TAURI_MODULE_RE + r"[\"']\s*\)",
    re.MULTILINE,
)


def _strip_c_like_comments(text: str) -> str:
    """Blank C/JS/Rust comments before scanning for attributes/imports/calls."""
    def blank(m: re.Match) -> str:
        return "".join("\n" if c == "\n" else " " for c in m.group(0))

    text = _BLOCK_COMMENT_RE.sub(blank, text)
    return _LINE_COMMENT_RE.sub(blank, text)


def _contract_id(name: str) -> str:
    return f"tauri_command::{name}"


def _rust_path(node, source: bytes) -> tuple[str, ...] | None:
    """Read a use/attribute path, without resolving reexports or crate aliases."""
    if node is None:
        return ()
    if node.type in {"identifier", "crate", "self", "super"}:
        name = source[node.start_byte:node.end_byte].decode("utf-8")
        return (name,) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else None
    if node.type == "scoped_identifier":
        path = _rust_path(node.child_by_field_name("path"), source)
        name = _rust_path(node.child_by_field_name("name"), source)
        if path is not None and name is not None:
            return path + name
    return None


def _rust_use_bindings(node, source: bytes, prefix=()) -> list[tuple[str, tuple]] | None:
    """Flatten explicit use trees. Unknown/glob imports cannot justify bare attributes."""
    if node is None:
        return None
    if node.type in {"use_list", "scoped_use_list"}:
        if node.type == "scoped_use_list":
            path = _rust_path(node.child_by_field_name("path"), source)
            if path is None:
                return None
            prefix += path
            node = node.child_by_field_name("list")
        bindings = []
        for child in node.named_children:
            if child.type in {"line_comment", "block_comment"}:
                continue
            nested = _rust_use_bindings(child, source, prefix)
            if nested is None:
                return None
            bindings.extend(nested)
        return bindings
    alias = None
    if node.type == "use_as_clause":
        alias_path = _rust_path(node.child_by_field_name("alias"), source)
        if alias_path is None or len(alias_path) != 1:
            return None
        alias = alias_path[0]
        node = node.child_by_field_name("path")
    path = _rust_path(node, source)
    if not path:
        return None
    path = prefix + path
    if path[-1] == "self":
        path = path[:-1]
    if not path or alias == "_":
        return []
    return [(alias or path[-1], path)]


def _rust_scope_commands(scope, source: bytes) -> set[str]:
    """Resolve direct imports separately for each source_file/declaration_list scope."""
    bindings: dict[str, list[tuple]] = {}
    trusted = set()
    blocked = set()
    unknown_import = False
    tauri_shadowed = False
    attributes = []
    for child in scope.named_children:
        if child.type == "attribute_item":
            attributes.append(child)
            continue
        if child.type in {"line_comment", "block_comment"}:
            continue
        if child.type == "use_declaration":
            uses = _rust_use_bindings(child.child_by_field_name("argument"), source)
            if uses is None:
                unknown_import = True
            else:
                # Attributed/visible imports can be conditional or reexports. They
                # still participate in conflicts, but do not grant a bare attribute.
                direct = not attributes and not any(
                    n.type == "visibility_modifier" for n in child.named_children
                )
                for alias, path in uses:
                    bindings.setdefault(alias, []).append(path)
                    if direct and path == ("tauri", "command"):
                        trusted.add(alias)
        name = child.child_by_field_name("name")
        if name is not None:
            name_text = source[name.start_byte:name.end_byte].decode("utf-8")
            if child.type == "macro_definition":
                blocked.add(name_text)
            if name_text == "tauri" and child.type in {
                "mod_item", "struct_item", "enum_item", "union_item", "type_item",
            }:
                tauri_shadowed = True
        if child.type == "extern_crate_declaration":
            if any(source[n.start_byte:n.end_byte] == b"tauri"
                   for n in child.named_children):
                tauri_shadowed = True
        attributes = []
    if any(path != ("tauri",) for path in bindings.get("tauri", [])):
        tauri_shadowed = True
    aliases = {
        alias for alias in trusted - blocked
        if bindings[alias] == [("tauri", "command")]
    } if not (unknown_import or tauri_shadowed) else set()

    commands = set()
    attributes = []
    for child in scope.named_children:
        if child.type == "attribute_item":
            attributes.append(child)
            continue
        if child.type in {"line_comment", "block_comment"}:
            continue
        if child.type == "function_item":
            for item in attributes:
                attribute = next((n for n in item.named_children if n.type == "attribute"), None)
                if attribute is None or not attribute.named_children:
                    continue
                path = _rust_path(attribute.named_children[0], source)
                if path and len(path) == 1 and path[0] in aliases:
                    name = _rust_path(child.child_by_field_name("name"), source)
                    if name:
                        commands.add(name[0])
                    break
        elif child.type == "mod_item":
            body = child.child_by_field_name("body")
            if body is not None and body.type == "declaration_list":
                # Rust module imports are not inherited by child/sibling modules.
                commands.update(_rust_scope_commands(body, source))
        attributes = []
    return commands


def _command_defs(text: str, rust_language=None) -> set[str]:
    # Keep qualified-attribute extraction unchanged, including nested functions.
    # Only the new imported spellings depend on the scoped Rust AST.
    qualified = {m.group(1) for m in _COMMAND_DEF_RE.finditer(_strip_c_like_comments(text))}
    if rust_language is not None:
        try:
            from tree_sitter import Parser
            source = text.encode("utf-8")
            tree = Parser(rust_language).parse(source)
            if not tree.root_node.has_error:
                return qualified | _rust_scope_commands(tree.root_node, source)
        except Exception:
            pass  # Preserve the original qualified-attribute-only fallback.
    return qualified


def _invoke_aliases(text: str) -> tuple[set[str], set[str]]:
    """Return named aliases and namespace aliases for official Tauri invoke imports."""
    text = _strip_c_like_comments(text)
    named: set[str] = set()
    namespaces: set[str] = set()

    for m in list(_NAMED_IMPORT_RE.finditer(text)) + list(_REQUIRE_DESTRUCTURE_RE.finditer(text)):
        for part in m.group(1).split(","):
            p = part.strip()
            if not p:
                continue
            mm = re.match(r"^invoke(?:\s+as\s+([A-Za-z_$][A-Za-z0-9_$]*))?$", p)
            if mm:
                named.add(mm.group(1) or "invoke")

    for m in _NAMESPACE_IMPORT_RE.finditer(text):
        namespaces.add(m.group(1))

    return named, namespaces


def _literal_invoke_commands(text: str, known: frozenset[str]) -> set[str]:
    """Literal command names invoked through official Tauri invoke imports."""
    named, namespaces = _invoke_aliases(text)
    if not named and not namespaces:
        return set()

    text = _strip_c_like_comments(text)
    found: set[str] = set()
    cmd = r"([A-Za-z_][A-Za-z0-9_]*)"

    for alias in named:
        pat = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(alias)}\s*\(\s*([\"'`]){cmd}\1")
        for m in pat.finditer(text):
            name = m.group(2)
            if name in known:
                found.add(name)

    for ns in namespaces:
        pat = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(ns)}\s*\.\s*invoke\s*\(\s*([\"'`]){cmd}\1")
        for m in pat.finditer(text):
            name = m.group(2)
            if name in known:
                found.add(name)

    return found


def _tauri_command_graph(
    root: str,
    source_files: list[tuple[str, str]],
    incomplete_paths_out=None,
    rust_language=None,
) -> tuple[list, list]:
    """Backward-compatible diagnostics wrapper for Tauri contracts."""
    try:
        return _tauri_command_graph_impl(
            root,
            source_files,
            incomplete_paths_out=incomplete_paths_out,
            rust_language=rust_language,
        )
    except Exception:
        has_rust = any(ext == _RUST_EXT for _path, ext in source_files)
        has_frontend = any(ext in _FRONTEND_EXTS for _path, ext in source_files)
        if has_rust and has_frontend:
            for abs_path, ext in source_files:
                if ext == _RUST_EXT or ext in _FRONTEND_EXTS:
                    _mark_incomplete(
                        incomplete_paths_out,
                        os.path.relpath(abs_path, root).replace(os.sep, "/"),
                    )
        return [], []


def _tauri_command_graph_impl(
    root: str,
    source_files: list[tuple[str, str]],
    incomplete_paths_out=None,
    rust_language=None,
) -> tuple[list, list]:
    """Return (nodes, edges) for Tauri command contracts.

    Nodes are useful in the extractor/test graph. The live DB may store only the edges
    because contract node kinds are extensible at the graph layer but filtered at ingest;
    the shared `alters`/`queries` edge dst is the coupling key the engine uses.
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    rust_files: list[tuple[str, str]] = []
    frontend_files: list[tuple[str, str, str]] = []
    for abs_path, ext in source_files:
        rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
        if ext == _RUST_EXT:
            rust_files.append((abs_path, rel))
        elif ext in _FRONTEND_EXTS:
            frontend_files.append((abs_path, rel, ext))

    if not rust_files or not frontend_files:
        return nodes, edges

    definers: dict[str, set[str]] = {}
    definition_loss = set()
    for abs_path, rel in rust_files:
        text = _read_capped(abs_path, definition_loss, rel)
        if text is None:
            continue
        for name in _command_defs(text, rust_language=rust_language):
            definers.setdefault(name, set()).add(rel)
    if definition_loss:
        for _abs_path, rel in rust_files:
            _mark_incomplete(incomplete_paths_out, rel)
        for _abs_path, rel, _ext in frontend_files:
            _mark_incomplete(incomplete_paths_out, rel)

    # One command name with several Rust definers is ambiguous. Keep the
    # first-class node and every anchored edge as explicitly inert evidence.
    single_definers = {name: next(iter(paths)) for name, paths in definers.items() if len(paths) == 1}
    ambiguous_names = {name for name, paths in definers.items() if len(paths) > 1}
    for name, paths in sorted(definers.items()):
        if len(paths) > 1:
            nodes.append({
                "id": _contract_id(name),
                "kind": "app_command",
                "name": name,
                "path": sorted(paths)[0],
                "language": "tauri",
                "ambiguous": True,
            })
            for rel in sorted(paths):
                edges.append({
                    "src": rel,
                    "dst": _contract_id(name),
                    "kind": "alters",
                    "reference_status": "ambiguous",
                })

    known = frozenset(definers)
    for name, rel in sorted(single_definers.items()):
        nodes.append({
            "id": _contract_id(name),
            "kind": "app_command",
            "name": name,
            "path": rel,
            "language": "tauri",
        })
        edges.append({"src": rel, "dst": _contract_id(name), "kind": "alters"})

    for abs_path, rel, _ext in frontend_files:
        text = _read_capped(abs_path, incomplete_paths_out, rel)
        if text is None:
            continue
        for name in sorted(_literal_invoke_commands(text, known)):
            edge = {"src": rel, "dst": _contract_id(name), "kind": "queries"}
            if name in ambiguous_names:
                edge["reference_status"] = "ambiguous"
            edges.append(edge)

    return nodes, edges
