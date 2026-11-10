"""Read-only local review candidates, reusing Core's graph and history producers."""
from __future__ import annotations

import sys

_BYTECODE_DISABLED_AT_STARTUP = bool(sys.flags.dont_write_bytecode)
_BYTECODE_DISABLED_ON_ENTRY = bool(sys.dont_write_bytecode)
sys.dont_write_bytecode = True
if __name__ == "__main__" and not (
    _BYTECODE_DISABLED_AT_STARTUP and _BYTECODE_DISABLED_ON_ENTRY
):
    print('{"status":"error","error":"bytecode_startup_required"}')
    raise SystemExit(2)

import argparse
import collections
import contextlib
import hashlib
import inspect
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess

MAX_TARGET_BYTES = 1_500_000


class ReviewError(ValueError):
    """A controlled code, never subprocess output or a local absolute path."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ReviewError("invalid_arguments")


@contextlib.contextmanager
def _git_environment():
    original = dict(os.environ)
    try:
        for key in list(os.environ):
            if key.startswith("GIT_"):
                del os.environ[key]
        os.environ.update({
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1",
            "GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "core.hooksPath",
            "GIT_CONFIG_VALUE_1": os.devnull, "GIT_CONFIG_KEY_2": "core.quotePath",
            "GIT_CONFIG_VALUE_2": "false",
        })
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _git(root, *arguments, allowed=(0,)):
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments], capture_output=True,
            text=True, timeout=15, check=False, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ReviewError("git_unavailable") from None
    if result.returncode not in allowed:
        raise ReviewError("git_read_failed")
    return result.stdout, result.returncode


def _relative(value):
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or "\\" in value
            or any(part in {"..", ".git"} for part in path.parts)
            or any(ord(char) < 32 for char in value)):
        raise ReviewError("invalid_relative_file")
    return path.as_posix()


def _regular(root, relative):
    path = root
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ReviewError("symlink_input")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ReviewError("file_not_regular_within_checkout")
    return path


def _state(root, target):
    top, _ = _git(root, "rev-parse", "--show-toplevel")
    if Path(top.strip()).resolve() != root:
        raise ReviewError("checkout_root_required")
    head, code = _git(root, "rev-parse", "--verify", "--quiet", "HEAD", allowed=(0, 1))
    if code:
        ref, _ = _git(root, "symbolic-ref", "--quiet", "HEAD")
        _, exists = _git(root, "show-ref", "--verify", "--quiet", ref.strip(), allowed=(0, 1))
        if exists != 1:
            raise ReviewError("git_head_unavailable")
    shallow, _ = _git(root, "rev-parse", "--is-shallow-repository")
    if shallow.strip() not in {"true", "false"}:
        raise ReviewError("git_state_invalid")
    flags, _ = _git(root, "ls-files", "-v", "-z")
    if any(row and (row[0].islower() or row[0] == "S") for row in flags.split("\0")):
        raise ReviewError("hidden_index_flags")
    stages, _ = _git(root, "ls-files", "--stage", "-z")
    if any(row.startswith("160000 ") for row in stages.split("\0")):
        raise ReviewError("submodule_input")
    # Status can run clean/process filters while comparing worktree content.
    # Query names only, including local include files, before any status read.
    _, filter_status = _git(root, "config", "--includes", "--name-only", "--get-regexp",
                            r"^filter\.", allowed=(0, 1))
    if filter_status == 0:
        raise ReviewError("unsupported_git_filters")
    status, _ = _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    with _regular(root, target).open("rb") as source:
        target_bytes = source.read(MAX_TARGET_BYTES + 1)
    if len(target_bytes) > MAX_TARGET_BYTES:
        raise ReviewError("target_exceeds_size_limit")
    return {
        "head": head.strip() if not code else None, "dirty": bool(status),
        "shallow": shallow.strip() == "true",
        "status_sha256": hashlib.sha256(status.encode()).hexdigest(),
        "target_sha256": hashlib.sha256(target_bytes).hexdigest(),
    }


def review(checkout, relative_file):
    root = Path(checkout).resolve(strict=True)
    target = _relative(relative_file)
    _regular(root, target)
    with _git_environment():
        before = _state(root, target)
        # Trusted Core modules only. Target source is parsed, never imported.
        import code_graph_extract as X
        import cochange_backtest as BT
        import _cg_cochange as CC

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            graph = X.build_graph(str(root))
        files = {node["path"] for node in graph["nodes"] if node.get("kind") == "file"}
        documents = [node for node in graph["nodes"]
                     if node.get("kind") in {"file", "config_file"}]
        target_nodes = [node for node in documents
                        if node.get("kind") == "file" and node.get("path") == target]
        target_documents = [node for node in documents if node.get("path") == target]
        coverage = {key: graph.get(key, 0)
                    for key in ("files_parsed", "files_failed", "files_skipped")}
        # Substrate/import resolution can mark successfully parsed documents
        # incomplete or ambiguous without increasing failed/skipped counters.
        # Count unique paths per status, including configuration documents.
        document_statuses = {
            status: len({node["path"] for node in documents
                         if node.get("analysis_status") == status and node.get("path")})
            for status in ("failed", "incomplete", "ambiguous")
        }
        coverage["document_analysis_status_counts"] = document_statuses
        static_warnings = []
        if not target_nodes:
            static_warnings.append("target_not_parsed_as_source")
        if any(node.get("analysis_status") in {"failed", "incomplete", "ambiguous"}
               for node in target_documents):
            static_warnings.append("target_analysis_incomplete")
        if (coverage["files_failed"] or coverage["files_skipped"]
                or any(document_statuses.values())):
            static_warnings.append("checkout_analysis_incomplete")
        static = []
        for pair, kinds in BT.coupled_pairs(graph, files).items():
            if target in pair:
                other = next(path for path in pair if path != target)
                _regular(root, _relative(other))
                static.append({"file": other, "evidence_kinds": sorted(kinds)})
        static.sort(key=lambda row: row["file"])

        parameters = {key: value.default for key, value in
                      inspect.signature(CC.cochange_pairs).parameters.items()
                      if key not in {"repo_dir"}}
        commits = []
        if before["head"] is not None:
            try:
                commits = CC._git_log_commits(
                    str(root), parameters["window"], parameters["timeout"], strict=True,
                )
            except (ValueError, OSError, subprocess.SubprocessError):
                raise ReviewError("history_read_failed") from None
        change, co = collections.Counter(), collections.Counter()
        n_total = CC.fold_commits(commits, change, co, parameters["max_commit_files"])
        pairs = CC.emit_pairs(change, co, n_total, **{
            key: parameters[key] for key in ("min_support", "min_prob", "min_lift", "max_pairs")
        })
        history, omitted = [], 0
        for pair in pairs:
            if target not in (pair["a"], pair["b"]):
                continue
            other = pair["b"] if pair["a"] == target else pair["a"]
            other = _relative(other)
            if not (root / other).exists():
                omitted += 1
                continue
            _regular(root, other)
            history.append({"file": other, **{key: value for key, value in pair.items()
                                             if key not in {"a", "b"}}})
        history_warnings = []
        if before["head"] is None:
            history_warnings.append("unborn_head")
        if before["shallow"]:
            history_warnings.append("shallow_history")
        if change[target] < parameters["min_support"]:
            history_warnings.append("target_support_below_floor")
        if before != _state(root, target):
            raise ReviewError("checkout_changed_during_analysis")
    return {
        "schema_version": "local-file-review-v1", "target": target,
        "scope": "Heuristic review candidates; not dependencies proven, bug prediction or merge approval.",
        "provenance": {**before, "extractor_version": graph.get("extractor_version"),
                       "static_input": "current_worktree", "history_input": "committed_HEAD"},
        "static": {
            "status": "uncertain" if static_warnings else "observed" if static else "not_observed",
            "method": "cochange_backtest.coupled_pairs coarse offline file projection; not App policy",
            "coverage": coverage, "warnings": static_warnings, "candidates": static,
        },
        "history": {
            "status": "uncertain" if history_warnings else "observed" if history else "not_observed",
            "parameters": parameters, "commits_read": len(commits), "commits_retained": n_total,
            "target_change_count": change[target], "missing_current_partners_omitted": omitted,
            "warnings": history_warnings, "candidates": history,
        },
        "limitations": [
            "No candidates under these filters is not proof of no relationship or safety.",
            "Static name matching and shared resources are coarse; co-change is historical correlation.",
            "Optional parsers, producer guards, hub filters, history window and global pair cap limit recall.",
            "HEAD, status and target bytes were checked before/after; this is not an atomic snapshot. "
            "Other edits which leave status unchanged, or transient reverted edits, can escape detection. "
            "Git status also excludes ignored files. Run in an isolated checkout without concurrent writers.",
        ],
    }


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("checkout")
        parser.add_argument("relative_file")
        args = parser.parse_args(argv)
        report = review(args.checkout, args.relative_file)
    except ReviewError as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        return 2
    except Exception:
        print('{"status":"error","error":"analysis_failed"}')
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
