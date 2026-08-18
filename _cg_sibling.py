"""Sibling file-stem contract extraction.

This module emits a conservative content-free graph for files that share a
non-generic basename across sibling directories in the same app/package scope.
"""
from __future__ import annotations

import os
from typing import Any

from _cg_io import _mark_incomplete

_MAX_GROUP_FILES = 8

_FAMILY_BY_EXT = {
    ".go": "go",
    ".py": "python",
    ".pyi": "python",
    ".ts": "web",
    ".tsx": "web",
    ".js": "web",
    ".jsx": "web",
    ".svelte": "web",
    ".vue": "web",
    ".swift": "swift",
    ".rs": "rust",
}

_GENERIC_STEMS = frozenset({
    "app", "apps", "base", "common", "component", "components", "config",
    "constants", "default", "helper", "helpers", "index", "lib", "main",
    "mod", "page", "pages", "route", "routes", "schema", "service",
    "services", "store", "types", "util", "utils", "view", "views",
})

_SKIP_DIR_PARTS = frozenset({
    "__generated__", "__mocks__", "__snapshots__", "__tests__", "docs",
    "examples", "fixtures", "generated", "mocks", "snapshots", "spec",
    "specs", "test", "tests", "vendor",
})

_STEM_SUFFIXES = (
    ".component", ".components", ".container", ".controller", ".dao",
    ".handler", ".model", ".page", ".repo", ".repository", ".route",
    ".schema", ".service", ".store", ".view",
)


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _parts(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def _is_test_or_fixture_path(path: str) -> bool:
    parts = {p.lower() for p in _parts(path)}
    if parts & _SKIP_DIR_PARTS:
        return True
    base = os.path.basename(path).lower()
    return base.endswith(".test") or base.endswith(".spec") or ".test." in base or ".spec." in base


def _scope_for_path(path: str) -> str | None:
    parts = _parts(path)
    if len(parts) < 3:
        return None
    if parts[0] in {"frontend", "backend", "server", "client", "web", "ui"}:
        if len(parts) >= 4 and parts[1] == "src":
            return "/".join(parts[:2])
        return parts[0]
    if parts[0] in {"src", "app", "lib", "internal", "pkg"}:
        return parts[0]
    if parts[0] == "src-tauri":
        return "src-tauri"
    return parts[0]


def _normalized_stem(path: str) -> str | None:
    stem = os.path.splitext(os.path.basename(path))[0].strip().lower()
    if not stem:
        return None
    for suffix in _STEM_SUFFIXES:
        if stem.endswith(suffix) and len(stem) > len(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = stem.replace("_", "-")
    if stem in _GENERIC_STEMS or len(stem) < 3:
        return None
    if stem.startswith("[") or stem.startswith("_") or stem.startswith("$"):
        return None
    return stem


def _contract_id(scope: str, family: str, stem: str) -> str:
    return f"sibling_stem::{family}::{scope.replace('/', '::')}::{stem}"


def _sibling_stem_graph(
    root: str,
    source_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> tuple[list, list]:
    groups: dict[tuple[str, str, str], set[str]] = {}
    dirs: dict[tuple[str, str, str], set[str]] = {}

    for abs_path, ext in source_files:
        family = _FAMILY_BY_EXT.get(ext)
        if not family:
            continue
        rel = _rel(root, abs_path)
        if _is_test_or_fixture_path(rel):
            continue
        scope = _scope_for_path(rel)
        stem = _normalized_stem(rel)
        if not scope or not stem:
            continue
        key = (scope, family, stem)
        groups.setdefault(key, set()).add(rel)
        dirs.setdefault(key, set()).add(os.path.dirname(rel))

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for (scope, family, stem), paths in sorted(groups.items()):
        if len(paths) > _MAX_GROUP_FILES:
            for rel in paths:
                _mark_incomplete(incomplete_paths_out, rel)
            continue
        if len(paths) < 2:
            continue
        if len(dirs.get((scope, family, stem), set())) < 2:
            continue
        cid = _contract_id(scope, family, stem)
        first_path = sorted(paths)[0]
        nodes.append({
            "id": cid,
            "kind": "sibling_stem",
            "name": stem,
            "path": first_path,
            "language": family,
        })
        for rel in sorted(paths):
            edges.append({"src": rel, "dst": cid, "kind": "queries"})

    return nodes, edges
