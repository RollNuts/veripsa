"""Offline, local-only audit wrapper around Veripsa's rich graph.

Run:

    python -m tools.hunt --target /absolute/repo --output /absolute/output

No GitHub App, database, SaaS, network client, or product ingest module is
imported by this wrapper.
"""

from __future__ import annotations

import sys

# The target and Veripsa worktrees are read-only inputs.  Set this before
# importing extractor modules so their imports do not create __pycache__.
sys.dont_write_bytecode = True

import argparse
from dataclasses import dataclass
import datetime as dt
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import tempfile
import time
import unicodedata
from typing import Any, Callable, Iterable

from tools.hunt_index import build_audit_indexes


_ARTIFACTS = (
    "run_manifest.json",
    "nodes.json",
    "edges.json",
    "rails_routes.json",
    "authorization_calls.json",
    "sidekiq_jobs.json",
    "execution_paths.json",
    "warnings.json",
    "summary.md",
)


class HuntError(RuntimeError):
    """Expected fail-closed CLI error."""


@dataclass(frozen=True)
class RepoState:
    requested_path: Path
    root: Path
    name: str
    commit_sha: str
    dirty: bool


@dataclass(frozen=True)
class HuntConfig:
    target: Path
    output: Path
    allow_dirty_target: bool = False
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()


def _git(path: Path, *arguments: str) -> str:
    environment = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
    }
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "submodule.recurse=false",
                "-C",
                str(path),
                *arguments,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
    except OSError as exc:
        raise HuntError(f"unable to execute local git: {type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        reason = detail[-1] if detail else f"git exit {result.returncode}"
        raise HuntError(f"{path} is not a usable git repository: {reason}")
    return result.stdout.rstrip("\n")


def _repo_state(path: Path) -> RepoState:
    try:
        requested = path.resolve(strict=True)
    except OSError as exc:
        raise HuntError(f"repository path does not exist: {path}") from exc
    if not requested.is_dir():
        raise HuntError(f"repository path is not a directory: {requested}")
    root = Path(_git(requested, "rev-parse", "--show-toplevel")).resolve()
    if requested != root:
        raise HuntError(
            f"--target must be the git repository root: requested={requested} root={root}"
        )
    commit = _git(root, "rev-parse", "HEAD").strip()
    if not commit:
        raise HuntError(f"git repository has no readable HEAD commit: {root}")
    dirty = bool(_git(root, "status", "--porcelain=v1", "--untracked-files=normal"))
    return RepoState(requested, root, root.name, commit, dirty)


def _ignored_untracked_paths(repository: Path) -> set[str]:
    raw = _git(
        repository,
        "ls-files",
        "-z",
        "--others",
        "--ignored",
        "--exclude-standard",
    )
    return {
        unicodedata.normalize("NFC", value.replace(os.sep, "/"))
        for value in raw.split("\0")
        if value
    }


def _hidden_index_paths(repository: Path) -> list[str]:
    """Return paths hidden from normal status by index compatibility flags."""
    raw = _git(repository, "ls-files", "-v", "-z")
    flagged: list[str] = []
    for entry in raw.split("\0"):
        if len(entry) < 3 or entry[1] != " ":
            continue
        tag, value = entry[0], entry[2:]
        if tag == "S" or tag.islower():
            flagged.append(
                unicodedata.normalize("NFC", value.replace(os.sep, "/"))
            )
    return sorted(set(flagged))


def _submodule_paths(repository: Path) -> list[str]:
    raw = _git(repository, "ls-files", "--stage", "-z")
    paths: list[str] = []
    for entry in raw.split("\0"):
        if not entry:
            continue
        metadata, separator, value = entry.partition("\t")
        if separator and metadata.split(" ", 1)[0] == "160000":
            paths.append(
                unicodedata.normalize("NFC", value.replace(os.sep, "/"))
            )
    return sorted(set(paths))


def _preview_paths(paths: list[str]) -> str:
    preview = ", ".join(paths[:5])
    if len(paths) > 5:
        preview += f" (+{len(paths) - 5} more)"
    return preview


def _assert_reproducible_checkout(repository: Path, label: str) -> None:
    hidden = _hidden_index_paths(repository)
    if hidden:
        raise HuntError(
            f"{label} uses assume-unchanged or skip-worktree index flags; "
            "a full checkout is required: "
            + _preview_paths(hidden)
        )
    submodules = _submodule_paths(repository)
    if submodules:
        raise HuntError(
            f"{label} contains gitlinks/submodules whose working-tree bytes are "
            "not fixed by the recorded repository commit alone: "
            + _preview_paths(submodules)
        )


def _normalized_graph_path(value: str, target_root: Path) -> str | None:
    path = Path(value)
    if path.is_absolute():
        try:
            value = path.resolve(strict=False).relative_to(
                target_root
            ).as_posix()
        except ValueError:
            return None
    else:
        value = value.replace(os.sep, "/")
        if value.startswith("./"):
            value = value[2:]
    return unicodedata.normalize("NFC", value)


def _graph_paths(graph: dict[str, Any], target_root: Path) -> set[str]:
    paths: set[str] = set()
    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        value = node.get("path")
        if not isinstance(value, str) or not value:
            continue
        normalized = _normalized_graph_path(value, target_root)
        if normalized:
            paths.add(normalized)
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        for field in ("src", "dst"):
            value = edge.get(field)
            if not isinstance(value, str) or not value:
                continue
            normalized = _normalized_graph_path(value, target_root)
            if normalized:
                paths.add(normalized)
                if normalized.startswith("file::"):
                    paths.add(normalized.removeprefix("file::"))
                coordinate_path, separator, _symbol = normalized.partition(
                    "::"
                )
                if separator and (
                    "/" in coordinate_path or "." in Path(coordinate_path).name
                ):
                    paths.add(coordinate_path)
    return paths


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_output_path(output: Path, target_root: Path, veripsa_root: Path) -> Path:
    if not output.is_absolute():
        raise HuntError("--output must be an absolute path")
    resolved = output.resolve(strict=False)
    if _is_within(resolved, target_root):
        raise HuntError("output directory must not be inside the target repository")
    if _is_within(resolved, veripsa_root):
        raise HuntError("output directory must not be inside the Veripsa worktree")
    if resolved.exists() and not resolved.is_dir():
        raise HuntError(f"output path exists and is not a directory: {resolved}")
    return resolved


def _validate_states(
    target: RepoState,
    veripsa: RepoState,
    allow_dirty_target: bool,
) -> None:
    if veripsa.dirty:
        raise HuntError(
            f"Veripsa worktree is dirty; refusing audit execution: {veripsa.root}"
        )
    if target.dirty and not allow_dirty_target:
        raise HuntError(
            "target repository is dirty; pass --allow-dirty-target to record and analyze it explicitly"
        )


def _assert_repo_state_unchanged(
    label: str,
    before: RepoState,
    after: RepoState,
) -> None:
    if (
        before.root != after.root
        or before.commit_sha != after.commit_sha
        or before.dirty != after.dirty
    ):
        raise HuntError(
            f"{label} repository state changed during analysis; "
            "refusing to write a mixed-snapshot audit result"
        )


def _package_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for package in (
        "tree-sitter",
        "tree-sitter-ruby",
        "tree-sitter-language-pack",
    ):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = None
    return result


def _command_argv(
    explicit: Iterable[str] | None,
) -> tuple[list[str] | None, str]:
    if explicit is not None:
        return list(explicit), "caller_supplied"
    try:
        executing_this_module = (
            Path(sys.argv[0]).resolve() == Path(__file__).resolve()
        )
    except (IndexError, OSError):
        executing_this_module = False
    if not executing_this_module:
        return None, "unavailable_programmatic_call"
    original = getattr(sys, "orig_argv", None)
    if original:
        return list(original), "sys.orig_argv"
    # Python 3.9 does not expose sys.orig_argv.  When invoked through the
    # supported module entry point, __spec__.name and sys.argv let us derive
    # the effective interpreter argv without guessing a shell alias.
    module_name = getattr(globals().get("__spec__"), "name", None)
    if module_name:
        return [
            sys.executable,
            "-m",
            module_name,
            *sys.argv[1:],
        ], "reconstructed_module_argv"
    return [sys.executable, *sys.argv], "reconstructed_script_argv"


def _maximum_rss_bytes() -> int | None:
    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (AttributeError, OSError, ValueError):
        return None
    # macOS reports bytes; Linux and most BSD-derived CI images report KiB.
    if sys.platform == "darwin":
        return value
    return value * 1024


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _write_json(path: Path, value: Any) -> None:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    _write_text_atomic(path, f"{rendered}\n")


def _graph_warnings(graph: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    failed = graph.get("files_failed")
    skipped = graph.get("files_skipped")
    if isinstance(failed, int) and failed:
        warnings.append(
            {
                "file": None,
                "line": None,
                "category": "graph_parse_failures_aggregate",
                "reason": (
                    f"rich graph reports {failed} parse failures, but committed "
                    "extractor API does not expose individual files"
                ),
                "severity": "warning",
            }
        )
    if isinstance(skipped, int) and skipped:
        warnings.append(
            {
                "file": None,
                "line": None,
                "category": "graph_skipped_files_aggregate",
                "reason": (
                    f"rich graph reports {skipped} skipped files, but committed "
                    "extractor API does not expose individual files or reasons"
                ),
                "severity": "info",
            }
        )
    return warnings


def _warning_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("file") or "",
        item.get("line") or 0,
        item.get("category") or "",
        item.get("reason") or "",
        item.get("severity") or "",
    )


def _extractor_metadata(
    graph: dict[str, Any],
    indexes: dict[str, Any],
) -> tuple[dict[str, str | None], int | None]:
    """Return only producer-declared versions; unavailable values stay null."""

    rich_graph = graph.get("extractor_version")
    if not isinstance(rich_graph, str) or not rich_graph.strip():
        rich_graph = None
    audit_index = indexes.get("extractor_version")
    if not isinstance(audit_index, str) or not audit_index.strip():
        audit_index = None

    metrics = graph.get("metrics")
    schema_contract = (
        metrics.get("schema_contract_version")
        if isinstance(metrics, dict)
        else None
    )
    if (
        isinstance(schema_contract, bool)
        or not isinstance(schema_contract, int)
        or schema_contract < 1
    ):
        schema_contract = None

    return (
        {
            "rich_graph": rich_graph,
            "ruby_rails_audit_index": audit_index,
        },
        schema_contract,
    )


def _summary(
    target: RepoState,
    graph: dict[str, Any],
    indexes: dict[str, Any],
    warning_count: int,
) -> str:
    paths = indexes["execution_paths"]
    resolved = sum(1 for item in paths if item.get("status") == "resolved")
    partial = sum(1 for item in paths if item.get("status") == "partial")
    jobs = indexes["sidekiq_jobs"]
    return "\n".join(
        [
            "# Veripsa Hunt Phase 2 Summary",
            "",
            "This is a local audit index. It does not assert authorization gaps or vulnerabilities.",
            "",
            f"- Target: `{target.name}`",
            f"- Target commit: `{target.commit_sha}`",
            f"- Target dirty: `{str(target.dirty).lower()}`",
            f"- Graph nodes: {len(graph.get('nodes', []))}",
            f"- Graph edges: {len(graph.get('edges', []))}",
            f"- Rails routes: {len(indexes['rails_routes'])}",
            f"- Authorization calls: {len(indexes['authorization_calls'])}",
            f"- Sidekiq workers: {len(jobs['workers'])}",
            f"- Sidekiq enqueue sites: {len(jobs['enqueue_sites'])}",
            f"- Resolved execution paths: {resolved}",
            f"- Partial execution paths: {partial}",
            f"- Warnings: {warning_count}",
            "",
            "Resolved paths prove only static lexical linkage. They do not prove runtime control flow,",
            "authorization sufficiency, exploitability, or vulnerability severity.",
            "",
        ]
    )


def run_hunt(
    config: HuntConfig,
    *,
    graph_builder: Callable[..., dict[str, Any]] | None = None,
    veripsa_root: Path | None = None,
    exact_command: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate, run one full graph build, build indexes, and write artifacts.

    ``graph_builder`` and ``veripsa_root`` are explicit test seams.  The CLI
    uses the committed extractor and this worktree root.
    """

    started = time.perf_counter()
    if not config.target.is_absolute():
        raise HuntError("--target must be an absolute path")
    veripsa_path = (
        Path(veripsa_root).resolve()
        if veripsa_root is not None
        else Path(__file__).resolve().parents[1]
    )
    target_state = _repo_state(config.target)
    veripsa_state = _repo_state(veripsa_path)
    _validate_states(
        target_state, veripsa_state, config.allow_dirty_target
    )
    output = _validate_output_path(
        config.output, target_state.root, veripsa_state.root
    )
    _assert_reproducible_checkout(target_state.root, "target")
    _assert_reproducible_checkout(veripsa_state.root, "Veripsa")
    ignored_untracked = _ignored_untracked_paths(target_state.root)
    ignored_attributes = sorted(
        path
        for path in ignored_untracked
        if Path(path).name == ".gitattributes"
    )
    if ignored_attributes:
        raise HuntError(
            "ignored untracked .gitattributes can change extractor input "
            "selection outside the recorded commit: "
            + _preview_paths(ignored_attributes)
        )

    if graph_builder is None:
        import code_graph_extract

        graph_builder = code_graph_extract.build_graph

    # This is intentionally the only build_graph call in the wrapper.
    try:
        graph = graph_builder(str(target_state.root), universe_paths=None)
    except Exception as exc:
        raise HuntError(
            f"full rich graph build failed: {type(exc).__name__}"
        ) from exc
    if not isinstance(graph, dict):
        raise HuntError("build_graph returned a non-object result")
    if not isinstance(graph.get("nodes"), list) or not isinstance(
        graph.get("edges"), list
    ):
        raise HuntError("build_graph result does not contain nodes/edges lists")
    ignored_graph_inputs = sorted(
        ignored_untracked & _graph_paths(graph, target_state.root)
    )
    if ignored_graph_inputs:
        raise HuntError(
            "ignored untracked files entered the rich graph; "
            "a commit SHA cannot reproduce this input: "
            + _preview_paths(ignored_graph_inputs)
        )

    try:
        indexes = build_audit_indexes(
            target_state.root,
            include_patterns=config.include_patterns,
            exclude_patterns=config.exclude_patterns,
        )
    except Exception as exc:
        raise HuntError(
            f"Ruby/Rails audit index build failed: {type(exc).__name__}"
        ) from exc
    extractor_versions, graph_schema_contract_version = _extractor_metadata(
        graph,
        indexes,
    )
    warnings = list(indexes["warnings"]) + _graph_warnings(graph)
    warnings.sort(key=_warning_sort_key)

    # Catch normal concurrent commit/clean-to-dirty changes before emitting any
    # artifact. This is not an atomic filesystem snapshot; the limitation is
    # recorded explicitly in the Phase 2 documentation.
    target_after = _repo_state(target_state.root)
    veripsa_after = _repo_state(veripsa_state.root)
    _assert_repo_state_unchanged("target", target_state, target_after)
    _assert_repo_state_unchanged("Veripsa", veripsa_state, veripsa_after)

    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "nodes.json", graph["nodes"])
    _write_json(output / "edges.json", graph["edges"])
    _write_json(output / "rails_routes.json", indexes["rails_routes"])
    _write_json(
        output / "authorization_calls.json",
        indexes["authorization_calls"],
    )
    _write_json(output / "sidekiq_jobs.json", indexes["sidekiq_jobs"])
    _write_json(output / "execution_paths.json", indexes["execution_paths"])
    _write_json(output / "warnings.json", warnings)
    _write_text_atomic(
        output / "summary.md",
        _summary(target_state, graph, indexes, len(warnings)),
    )

    elapsed = time.perf_counter() - started
    command_argv, command_capture = _command_argv(exact_command)
    manifest = {
        "target_absolute_path": str(target_state.root),
        "target_repository_name": target_state.name,
        "target_commit_sha": target_state.commit_sha,
        "target_dirty_state": target_state.dirty,
        "veripsa_commit_sha": veripsa_state.commit_sha,
        "veripsa_dirty_state": veripsa_state.dirty,
        "utc_timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "exact_command": command_argv,
        "exact_command_capture": command_capture,
        "python_version": platform.python_version(),
        "extractor_versions": extractor_versions,
        "graph_schema_contract_version": graph_schema_contract_version,
        "relevant_package_versions": _package_versions(),
        "elapsed_time_seconds": round(elapsed, 6),
        "maximum_rss_bytes": _maximum_rss_bytes(),
        "files_parsed": graph.get("files_parsed"),
        "files_failed": graph.get("files_failed"),
        "files_skipped": graph.get("files_skipped"),
        "node_count": len(graph["nodes"]),
        "edge_count": len(graph["edges"]),
        "warning_count": len(warnings),
        "include_patterns": list(config.include_patterns),
        "exclude_patterns": list(config.exclude_patterns),
        "artifacts": list(_ARTIFACTS),
    }
    _write_json(output / "run_manifest.json", manifest)
    return {
        "manifest": manifest,
        "output": output,
        "indexes": indexes,
        "warnings": warnings,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.hunt",
        description=(
            "Build a full local rich graph and conservative Ruby/Rails audit indexes. "
            "No network, DB, GitHub App, or SaaS integration is used."
        ),
    )
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-dirty-target", action="store_true")
    parser.add_argument(
        "--include-pattern",
        action="append",
        default=[],
        help="repeatable glob applied to audit-index Ruby files only",
    )
    parser.add_argument(
        "--exclude-pattern",
        action="append",
        default=[],
        help="repeatable glob applied to audit-index Ruby files only",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = HuntConfig(
        target=args.target,
        output=args.output,
        allow_dirty_target=args.allow_dirty_target,
        include_patterns=tuple(args.include_pattern),
        exclude_patterns=tuple(args.exclude_pattern),
    )
    try:
        result = run_hunt(config)
    except HuntError as exc:
        print(f"hunt: {exc}", file=sys.stderr)
        return 2
    manifest = result["manifest"]
    print(
        "hunt complete: "
        f"nodes={manifest['node_count']} "
        f"edges={manifest['edge_count']} "
        f"warnings={manifest['warning_count']} "
        f"output={result['output']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
