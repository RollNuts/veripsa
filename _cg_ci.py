"""CI package-script contract extraction.

GitHub Actions often depends on package manager scripts without any source import:

  - `package.json`: `"scripts": { "typecheck": "tsc --noEmit" }`
  - workflow: `run: npm run typecheck`

Changing either side can break the merge gate, but the normal code graph sees no
edge between `.github/workflows/ci.yml` and `package.json`. This substrate emits a
shared contract key for scripts that are explicitly invoked from GitHub Actions.

Precision discipline:
  - definitions come only from `package.json` `scripts` keys;
  - references come only from workflow `run:` commands under `.github/workflows`;
  - ambiguous monorepo script names retain explicitly ambiguous candidate evidence
    unless a working directory resolves the package uniquely;
  - comments and non-`run:` workflow prose are ignored.

Content-free output: script names, package directories, file paths, and edge kinds only.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from _cg_io import _mark_incomplete, _read_capped

_WORKFLOW_EXTS = frozenset({".yml", ".yaml"})
_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,80}$")
_RUN_LINE_RE = re.compile(r"^(\s*)(?:-\s*)?run\s*:\s*(.*)$")
_WORKDIR_LINE_RE = re.compile(r"^\s*working-directory\s*:\s*(.+?)\s*$")
_CD_RE = re.compile(r"^\s*cd\s+([A-Za-z0-9_./-]+)\s*(?:&&|;)?\s*(.*)$")

_EXPLICIT_RUN_CMD_RE = re.compile(
    r"^(?:npm|pnpm|yarn|bun)\s+"
    r"(?:run|run-script)\s+"
    r"(?:(?:--[A-Za-z0-9_-]+(?:[=\s][^\s;&|]+)?|-s)\s+)*"
    r"([A-Za-z0-9][A-Za-z0-9_.:/-]{0,80})"
)
_NPM_SHORT_CMD_RE = re.compile(r"^npm\s+(test|start|stop|restart)\b")
_DIRECT_SCRIPT_CMD_RE = re.compile(r"^(?:pnpm|yarn|bun)\s+([A-Za-z0-9][A-Za-z0-9_.:/-]{0,80})\b")
_PKG_MANAGER_BUILTINS = frozenset({
    "add", "audit", "cache", "ci", "config", "create", "dlx", "exec", "explain",
    "help", "init", "install", "link", "list", "outdated", "pack", "publish",
    "remove", "run", "run-script", "set", "unlink", "up", "upgrade", "version",
    "why", "workspace", "workspaces",
})


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _norm_dir(path: str | None) -> str | None:
    if not path:
        return "."
    p = path.strip().strip("\"'")
    if not p:
        return "."
    p = os.path.normpath(p).replace(os.sep, "/")
    if p in ("", "."):
        return "."
    if p == ".." or p.startswith("../") or p.startswith("/"):
        return None
    return p.rstrip("/")


def _script_id(pkg_dir: str, name: str) -> str:
    return f"ci_script::{pkg_dir}::{name}"


def _is_workflow(rel: str, ext: str) -> bool:
    return ext in _WORKFLOW_EXTS and rel.startswith(".github/workflows/")


def _package_scripts(
    root: str,
    config_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> dict[str, set[str]]:
    """Return package_dir -> script names declared in package.json files."""
    out: dict[str, set[str]] = {}
    for abs_path, _ext in config_files:
        if os.path.basename(abs_path) != "package.json":
            continue
        rel = _rel(root, abs_path)
        text = _read_capped(abs_path, incomplete_paths_out, rel)
        if text is None:
            continue
        try:
            obj = json.loads(text)
        except Exception:
            _mark_incomplete(incomplete_paths_out, rel)
            continue
        scripts = obj.get("scripts") if isinstance(obj, dict) else None
        if not isinstance(scripts, dict):
            continue
        pkg_dir = os.path.dirname(rel).replace(os.sep, "/") or "."
        names = {
            str(k)
            for k in scripts.keys()
            if isinstance(k, str) and _SCRIPT_NAME_RE.fullmatch(k)
        }
        if names:
            out[pkg_dir] = names
    return out


def _strip_yaml_inline_comment(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value[0] in ("'", '"') and value.endswith(value[0]):
        return value[1:-1]
    return re.sub(r"\s+#.*$", "", value).strip()


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _step_bounds(lines: list[str], idx: int, prop_indent: int) -> tuple[int, int]:
    start = idx
    for j in range(idx, -1, -1):
        stripped = lines[j].lstrip()
        if stripped.startswith("- ") and _line_indent(lines[j]) <= prop_indent:
            start = j
            break
    end = len(lines)
    base_indent = _line_indent(lines[start])
    for j in range(idx + 1, len(lines)):
        stripped = lines[j].lstrip()
        if stripped.startswith("- ") and _line_indent(lines[j]) <= base_indent:
            end = j
            break
    return start, end


def _workdir_for_run(lines: list[str], idx: int, prop_indent: int) -> str:
    start, end = _step_bounds(lines, idx, prop_indent)
    for line in lines[start:end]:
        m = _WORKDIR_LINE_RE.match(line)
        if not m:
            continue
        cwd = _norm_dir(_strip_yaml_inline_comment(m.group(1)))
        if cwd is not None:
            return cwd
    return "."


def _workflow_runs(text: str) -> list[tuple[str, str]]:
    """Return `(command_text, cwd)` pairs from workflow `run:` entries only."""
    lines = text.splitlines()
    runs: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _RUN_LINE_RE.match(line)
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        raw = m.group(2).strip()
        cwd = _workdir_for_run(lines, i, indent)
        if raw in ("|", ">", "|-", ">-", "|+", ">+"):
            block: list[str] = []
            i += 1
            while i < len(lines):
                ln = lines[i]
                if ln.strip() and _line_indent(ln) <= indent:
                    break
                block.append(ln)
                i += 1
            runs.append(("\n".join(block), cwd))
            continue
        runs.append((_strip_yaml_inline_comment(raw), cwd))
        i += 1
    return runs


def _join_dir(base: str, child: str) -> str | None:
    child = child.strip().strip("\"'")
    if not child:
        return base
    if child.startswith("$"):
        return None
    if child.startswith("/"):
        return None
    p = child if base == "." else f"{base}/{child}"
    return _norm_dir(p)


def _script_calls(command_text: str, cwd: str) -> list[tuple[str, str]]:
    """Return `(script_name, cwd)` calls from a shell command block."""
    calls: list[tuple[str, str]] = []
    current = cwd
    for line in command_text.splitlines() or [command_text]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        cm = _CD_RE.match(stripped)
        scan = stripped
        if cm:
            next_dir = _join_dir(current, cm.group(1))
            if next_dir is None:
                continue
            current = next_dir
            scan = cm.group(2) or ""
        for segment in re.split(r"&&|\|\||;|\|", scan):
            segment = segment.strip()
            if not segment:
                continue
            m = _EXPLICIT_RUN_CMD_RE.match(segment)
            if m:
                name = m.group(1)
                if _SCRIPT_NAME_RE.fullmatch(name):
                    calls.append((name, current))
                continue
            m = _NPM_SHORT_CMD_RE.match(segment)
            if m:
                calls.append((m.group(1), current))
                continue
            m = _DIRECT_SCRIPT_CMD_RE.match(segment)
            if m:
                name = m.group(1)
                if name not in _PKG_MANAGER_BUILTINS and _SCRIPT_NAME_RE.fullmatch(name):
                    calls.append((name, current))
    return calls


def _script_candidates(pkg_scripts: dict[str, set[str]], name: str) -> list[str]:
    """Every package dir defining ``name``, in stable order."""
    return sorted(pkg_dir for pkg_dir, names in pkg_scripts.items() if name in names)


def _resolve_script(pkg_scripts: dict[str, set[str]], name: str, cwd: str) -> str | None:
    """Resolve a script call to a package dir, returning None when ambiguous/unknown."""
    candidates = _script_candidates(pkg_scripts, name)
    if not candidates:
        return None

    # Prefer the nearest package.json at or above the workflow working directory.
    matches = [
        pkg_dir for pkg_dir in candidates
        if cwd == pkg_dir or (pkg_dir != "." and cwd.startswith(pkg_dir + "/"))
    ]
    if cwd == "." and "." in candidates:
        return "."
    if matches:
        return max(matches, key=len)

    # In a single-package repo, a workflow-level cwd is unnecessary and the script name is unique.
    if len(candidates) == 1:
        return candidates[0]
    return None


def _ci_script_graph(
    root: str,
    config_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> tuple[list, list]:
    """Backward-compatible diagnostics wrapper for CI script extraction."""
    try:
        return _ci_script_graph_impl(
            root,
            config_files,
            incomplete_paths_out=incomplete_paths_out,
        )
    except Exception:
        for abs_path, ext in config_files:
            rel = _rel(root, abs_path)
            if os.path.basename(abs_path) == "package.json" or _is_workflow(rel, ext):
                _mark_incomplete(incomplete_paths_out, rel)
        return [], []


def _ci_script_graph_impl(
    root: str,
    config_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> tuple[list, list]:
    """Return (nodes, edges) for package script contracts used by GitHub Actions."""
    definition_loss = set()
    pkg_scripts = _package_scripts(
        root, config_files, incomplete_paths_out=definition_loss
    )
    if definition_loss:
        for lost_path in definition_loss:
            _mark_incomplete(incomplete_paths_out, lost_path)
        for abs_path, ext in config_files:
            rel = _rel(root, abs_path)
            if _is_workflow(rel, ext):
                _mark_incomplete(incomplete_paths_out, rel)
    if not pkg_scripts:
        return [], []

    workflow_files = [
        (abs_path, _rel(root, abs_path))
        for abs_path, ext in config_files
        if _is_workflow(_rel(root, abs_path), ext)
    ]
    if not workflow_files:
        return [], []

    queries: set[tuple[str, str, str]] = set()  # resolved: workflow rel, package dir, script name
    ambiguous_queries: set[tuple[str, str, str]] = set()
    for abs_path, rel in workflow_files:
        text = _read_capped(abs_path, incomplete_paths_out, rel)
        if text is None:
            continue
        for command, cwd in _workflow_runs(text):
            for name, call_cwd in _script_calls(command, cwd):
                pkg_dir = _resolve_script(pkg_scripts, name, call_cwd)
                if pkg_dir is not None:
                    queries.add((rel, pkg_dir, name))
                    continue
                candidates = _script_candidates(pkg_scripts, name)
                if len(candidates) > 1:
                    # The invocation is real and anchored, but package context
                    # is missing/insufficient. Preserve every candidate as inert
                    # evidence instead of making the reference disappear.
                    for candidate in candidates:
                        ambiguous_queries.add((rel, candidate, name))

    if not queries and not ambiguous_queries:
        return [], []

    used = {(pkg_dir, name) for _relpath, pkg_dir, name in queries}
    ambiguous_used = {
        (pkg_dir, name)
        for _relpath, pkg_dir, name in ambiguous_queries
    }
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for pkg_dir, name in sorted(used | ambiguous_used):
        pkg_rel = "package.json" if pkg_dir == "." else f"{pkg_dir}/package.json"
        node = {
            "id": _script_id(pkg_dir, name),
            "kind": "ci_script",
            "name": name,
            "path": pkg_rel,
            "language": "ci",
        }
        ambiguous_only = (pkg_dir, name) in ambiguous_used and (pkg_dir, name) not in used
        if ambiguous_only:
            node["ambiguous"] = True
        nodes.append(node)
        edge = {"src": pkg_rel, "dst": _script_id(pkg_dir, name), "kind": "alters"}
        if ambiguous_only:
            edge["reference_status"] = "ambiguous"
        edges.append(edge)

    for rel, pkg_dir, name in sorted(queries):
        edges.append({"src": rel, "dst": _script_id(pkg_dir, name), "kind": "queries"})
    for rel, pkg_dir, name in sorted(ambiguous_queries):
        edge = {
            "src": rel,
            "dst": _script_id(pkg_dir, name),
            "kind": "queries",
            "reference_status": "ambiguous",
        }
        edges.append(edge)

    return nodes, edges
