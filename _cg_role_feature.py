"""Bounded role-feature contract extraction.

This substrate links files that share a distinctive feature token across a
small allowlist of role directories. It is intentionally narrower than a
folder-neighborhood detector: a directory pair is never coupled as a whole.
Only files in a known role pair that share a stable path-derived token join the
same content-free contract key.
"""
from __future__ import annotations

import os
import re
from typing import Any

from _cg_io import _mark_incomplete

_MAX_GROUP_FILES = 8

_FAMILY_BY_EXT = {
    ".go": "go",
    ".ts": "web",
    ".tsx": "web",
    ".js": "web",
    ".jsx": "web",
    ".svelte": "web",
    ".vue": "web",
    ".rs": "rust",
}

_SKIP_DIR_PARTS = frozenset({
    "__generated__", "__mocks__", "__snapshots__", "__tests__", "coverage",
    "dist", "docs", "examples", "fixtures", "generated", "mocks",
    "node_modules", "snapshots", "spec", "specs", "test", "tests",
    "vendor",
})

_TOKEN_STOP = frozenset({
    "api", "app", "base", "common", "component", "components", "config",
    "const", "constants", "content", "controller", "core", "data",
    "default", "detail", "display", "handler", "helper", "helpers",
    "hooks", "huma", "index", "internal", "layout", "lib", "main",
    "mod", "modal", "model", "module", "page", "pages", "provider",
    "route", "routes", "schema", "service", "services", "shared",
    "store", "test", "tests", "type", "types", "unit", "units",
    "util", "utils", "view", "views", "widget",
})

_INTERNAL_ROLE_PAIRS = frozenset({
    ("db", "duckdb"),
    ("db", "parser"),
    ("db", "postgres"),
    ("db", "server"),
    ("db", "sync"),
    ("duckdb", "server"),
    ("duckdb", "sync"),
    ("parser", "sync"),
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_DYNAMIC_ROUTE_RE = re.compile(r"^\[.*\]$")


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _parts(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def _is_skipped_path(path: str) -> bool:
    parts = {p.lower() for p in _parts(path)}
    if parts & _SKIP_DIR_PARTS:
        return True
    base = os.path.basename(path).lower()
    return (
        base.endswith("_test.go")
        or ".test." in base
        or ".spec." in base
        or base.endswith(".test")
        or base.endswith(".spec")
    )


def _singular(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("ses") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 4:
        return token[:-1]
    return token


def _tokens_from_fragment(fragment: str) -> set[str]:
    fragment = fragment.strip()
    if not fragment:
        return set()
    if fragment.startswith(("[", "$")) or _DYNAMIC_ROUTE_RE.fullmatch(fragment):
        return set()
    if fragment.startswith("(") and fragment.endswith(")"):
        return set()
    spaced = _CAMEL_BOUNDARY_RE.sub("-", fragment)
    raw = _TOKEN_RE.findall(spaced.replace("_", "-").replace(".", "-").lower())
    out: set[str] = set()
    for token in raw:
        token = _singular(token)
        if len(token) < 4 or token in _TOKEN_STOP:
            continue
        if token.isdigit():
            continue
        out.add(token)
    return out


def _nearest_web_page_route_tokens(path: str) -> set[str]:
    parts = _parts(path)
    if len(parts) < 5 or parts[:3] != ["frontend", "src", "pages"]:
        return set()
    for segment in reversed(parts[3:-1]):
        tokens = _tokens_from_fragment(segment)
        if tokens:
            return tokens
    return set()


def _feature_tokens(path: str, role_name: str | None = None, role_family: str | None = None) -> set[str]:
    stem = os.path.splitext(os.path.basename(path))[0].strip()
    tokens = _tokens_from_fragment(stem)
    if role_family == "web_frontend" and role_name == "pages" and not tokens:
        tokens.update(_nearest_web_page_route_tokens(path))
    return tokens


def _role_for_path(path: str, family: str) -> tuple[str, str, str] | None:
    """Return (scope, role, role_family) for a path in a known role directory."""
    parts = _parts(path)
    if family == "go":
        try:
            i = parts.index("internal")
        except ValueError:
            i = -1
        if i >= 0 and len(parts) >= i + 3:
            role = parts[i + 1]
            if role in {"db", "duckdb", "parser", "postgres", "server", "sync"}:
                prefix = "/".join(parts[:i + 1])
                return prefix, role, "go_internal"

    if family == "web" and len(parts) >= 4 and parts[0] == "frontend" and parts[1] == "src":
        role = parts[2]
        if role in {"components", "pages"}:
            return "frontend/src", role, "web_frontend"

    if family == "rust" and len(parts) >= 4 and parts[:3] == ["src-tauri", "src", "commands"]:
        return "tauri", "commands", "tauri_command_page"

    if family == "web" and len(parts) >= 3 and parts[0] == "src" and parts[1] == "pages":
        return "tauri", "pages", "tauri_command_page"

    return None


def _allowed_pair(role_family: str, a: str, b: str) -> bool:
    pair = tuple(sorted((a, b)))
    if role_family == "go_internal":
        return pair in _INTERNAL_ROLE_PAIRS
    if role_family == "web_frontend":
        return pair == ("components", "pages")
    if role_family == "tauri_command_page":
        return pair == ("commands", "pages")
    return False


def _contract_id(role_family: str, scope: str, role_a: str, role_b: str, token: str) -> str:
    pair = "__".join(sorted((role_a, role_b)))
    clean_scope = scope.replace("/", "::")
    return f"role_feature::{role_family}::{clean_scope}::{pair}::{token}"


def _role_feature_graph(
    root: str,
    source_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> tuple[list, list]:
    groups: dict[tuple[str, str, str, str, str], set[str]] = {}
    roles: dict[tuple[str, str, str, str, str], dict[str, set[str]]] = {}

    records: list[tuple[str, str, str, str, set[str]]] = []
    for abs_path, ext in source_files:
        family = _FAMILY_BY_EXT.get(ext)
        if not family:
            continue
        rel = _rel(root, abs_path)
        if _is_skipped_path(rel):
            continue
        role = _role_for_path(rel, family)
        if not role:
            continue
        scope, role_name, role_family = role
        tokens = _feature_tokens(rel, role_name, role_family)
        if not tokens:
            continue
        records.append((rel, scope, role_name, role_family, tokens))

    for i, (path_a, scope_a, role_a, fam_a, tokens_a) in enumerate(records):
        for path_b, scope_b, role_b, fam_b, tokens_b in records[i + 1:]:
            if fam_a != fam_b or scope_a != scope_b or role_a == role_b:
                continue
            if not _allowed_pair(fam_a, role_a, role_b):
                continue
            shared = tokens_a & tokens_b
            if not shared:
                continue
            left, right = sorted((role_a, role_b))
            for token in shared:
                key = (fam_a, scope_a, left, right, token)
                groups.setdefault(key, set()).update((path_a, path_b))
                roles.setdefault(key, {}).setdefault(role_a, set()).add(path_a)
                roles.setdefault(key, {}).setdefault(role_b, set()).add(path_b)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for (role_family, scope, role_a, role_b, token), paths in sorted(groups.items()):
        role_paths = roles.get((role_family, scope, role_a, role_b, token), {})
        if len(paths) > _MAX_GROUP_FILES:
            for rel in paths:
                _mark_incomplete(incomplete_paths_out, rel)
            continue
        if len(paths) < 2:
            continue
        if not role_paths.get(role_a) or not role_paths.get(role_b):
            continue
        cid = _contract_id(role_family, scope, role_a, role_b, token)
        nodes.append({
            "id": cid,
            "kind": "role_feature",
            "name": token,
            "path": sorted(paths)[0],
            "language": role_family,
        })
        for rel in sorted(paths):
            edges.append({"src": rel, "dst": cid, "kind": "queries"})

    return nodes, edges
