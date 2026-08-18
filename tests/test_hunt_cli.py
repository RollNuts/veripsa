#!/usr/bin/env python3
"""Offline contract gate for ``python -m tools.hunt``.

The test creates only throwaway local git repositories copied from the
synthetic Phase 2 fixture. It never clones, opens a socket, or connects to a
database.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import urllib.request
from typing import Any, Callable, Iterator
import unicodedata

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "hunt_phase2"
sys.path.insert(0, str(ROOT))

import tools.hunt as H  # noqa: E402


EXPECTED_ARTIFACTS = {
    "run_manifest.json",
    "nodes.json",
    "edges.json",
    "rails_routes.json",
    "authorization_calls.json",
    "sidekiq_jobs.json",
    "execution_paths.json",
    "warnings.json",
    "summary.md",
}
STABLE_ARTIFACTS = EXPECTED_ARTIFACTS - {"run_manifest.json"}
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "hunt-test",
    "GIT_AUTHOR_EMAIL": "hunt-test@example.invalid",
    "GIT_COMMITTER_NAME": "hunt-test",
    "GIT_COMMITTER_EMAIL": "hunt-test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=GIT_ENV,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed in {repository}: {result.stderr}"
        )
    return result.stdout.rstrip("\n")


def _init_repository(
    parent: Path,
    name: str,
    *,
    fixture: bool,
) -> Path:
    repository = parent / name
    if fixture:
        shutil.copytree(FIXTURE, repository)
    else:
        repository.mkdir(parents=True)
        (repository / "README.md").write_text(
            f"# {name}\n", encoding="utf-8"
        )
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "hunt-test@example.invalid")
    _git(repository, "config", "user.name", "hunt-test")
    _git(repository, "config", "commit.gpgsign", "false")
    _git(repository, "config", "gc.auto", "0")
    _git(repository, "add", "--all")
    _git(repository, "commit", "--no-verify", "-q", "-m", "fixture")
    return repository.resolve()


def _init_runtime_repository(parent: Path) -> Path:
    repository = parent / "veripsa-runtime"
    shutil.copytree(
        ROOT,
        repository,
        ignore=shutil.ignore_patterns(
            ".git",
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
        ),
    )
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "hunt-test@example.invalid")
    _git(repository, "config", "user.name", "hunt-test")
    _git(repository, "config", "commit.gpgsign", "false")
    _git(repository, "config", "gc.auto", "0")
    _git(repository, "add", "--all")
    _git(repository, "commit", "--no-verify", "-q", "-m", "runtime")
    return repository.resolve()


def _tree_digest(repository: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(repository.rglob("*")):
        relative = path.relative_to(repository)
        if ".git" in relative.parts or not path.is_file():
            continue
        result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _fake_graph(target: Path) -> dict[str, Any]:
    return {
        "root": str(target),
        "extractor_version": "fixture-rich-graph-v7",
        "metrics": {"schema_contract_version": 42},
        "nodes": [
            {
                "id": "file::app/controllers/api/v1/project_exports_controller.rb",
                "kind": "file",
                "path": "app/controllers/api/v1/project_exports_controller.rb",
                "language": "ruby",
            },
            {
                "id": "def::Api::V1::ProjectExportsController#create",
                "kind": "def",
                "path": "app/controllers/api/v1/project_exports_controller.rb",
                "name": "create",
                "language": "ruby",
                "start_line": 4,
                "end_line": 7,
            },
        ],
        "edges": [
            {
                "src": "file::app/controllers/api/v1/project_exports_controller.rb",
                "dst": "def::Api::V1::ProjectExportsController#create",
                "kind": "contains",
            }
        ],
        "files_parsed": 9,
        # Deliberately omit files_failed/files_skipped: unavailable aggregate
        # information must remain JSON null, never be guessed as zero.
    }


class CountingBuilder:
    def __init__(self, target: Path):
        self.target = target
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, root: str, *, universe_paths: Any) -> dict[str, Any]:
        self.calls.append((root, universe_paths))
        return copy.deepcopy(_fake_graph(self.target))


def _expect_hunt_error(
    action: Callable[[], Any],
    text: str,
) -> None:
    try:
        action()
    except H.HuntError as exc:
        assert text.lower() in str(exc).lower(), (
            f"expected error containing {text!r}, got {exc!r}"
        )
        return
    raise AssertionError(f"expected HuntError containing {text!r}")


@contextlib.contextmanager
def _no_network_or_db() -> Iterator[dict[str, list[Any]]]:
    """Trap Python network/DB calls and restrict wrapper subprocesses to local git."""

    calls: dict[str, list[Any]] = {
        "network": [],
        "database": [],
        "subprocess": [],
    }
    original_socket = socket.socket
    original_create_connection = socket.create_connection
    original_urlopen = urllib.request.urlopen
    original_subprocess_run = H.subprocess.run
    original_dsn = os.environ.get("VERIPSA_DSN")
    existing_psycopg2 = sys.modules.get("psycopg2")

    def forbidden_network(*arguments: Any, **keywords: Any) -> Any:
        calls["network"].append((arguments, keywords))
        raise AssertionError("network access attempted by local hunt")

    def guarded_subprocess(
        command: Any, *arguments: Any, **keywords: Any
    ) -> Any:
        argv = [str(item) for item in command]
        calls["subprocess"].append(argv)
        assert argv and argv[0] == "git", f"non-git subprocess attempted: {argv}"
        forbidden = {"clone", "fetch", "pull", "push", "ls-remote"}
        assert not forbidden.intersection(argv), f"remote git attempted: {argv}"
        return original_subprocess_run(command, *arguments, **keywords)

    def forbidden_database(*arguments: Any, **keywords: Any) -> Any:
        calls["database"].append((arguments, keywords))
        raise AssertionError("database access attempted by local hunt")

    trap_psycopg2 = types.ModuleType("psycopg2")
    trap_psycopg2.connect = forbidden_database  # type: ignore[attr-defined]

    socket.socket = forbidden_network  # type: ignore[assignment]
    socket.create_connection = forbidden_network
    urllib.request.urlopen = forbidden_network
    H.subprocess.run = guarded_subprocess
    sys.modules["psycopg2"] = trap_psycopg2
    os.environ["VERIPSA_DSN"] = "postgresql://127.0.0.1:1/must-not-connect"
    try:
        yield calls
    finally:
        socket.socket = original_socket  # type: ignore[assignment]
        socket.create_connection = original_create_connection
        urllib.request.urlopen = original_urlopen
        H.subprocess.run = original_subprocess_run
        if existing_psycopg2 is None:
            sys.modules.pop("psycopg2", None)
        else:
            sys.modules["psycopg2"] = existing_psycopg2
        if original_dsn is None:
            os.environ.pop("VERIPSA_DSN", None)
        else:
            os.environ["VERIPSA_DSN"] = original_dsn


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalized_manifest(path: Path) -> dict[str, Any]:
    manifest = _read_json(path)
    for volatile in (
        "utc_timestamp",
        "exact_command",
        "elapsed_time_seconds",
        "maximum_rss_bytes",
    ):
        manifest.pop(volatile, None)
    return manifest


def test_validation(work: Path) -> None:
    target = _init_repository(work, "validation-target", fixture=True)
    veripsa = _init_repository(work, "validation-veripsa", fixture=False)
    output = (work / "validation-output").resolve()
    builder = CountingBuilder(target)

    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(Path("relative-target"), output),
            graph_builder=builder,
            veripsa_root=veripsa,
        ),
        "target must be an absolute path",
    )
    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig((work / "does-not-exist").resolve(), output),
            graph_builder=builder,
            veripsa_root=veripsa,
        ),
        "does not exist",
    )
    nongit = (work / "not-a-repository").resolve()
    nongit.mkdir()
    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(nongit, output),
            graph_builder=builder,
            veripsa_root=veripsa,
        ),
        "not a usable git repository",
    )
    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(target, Path("relative-output")),
            graph_builder=builder,
            veripsa_root=veripsa,
        ),
        "output must be an absolute path",
    )
    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(target, target / "hunt-output"),
            graph_builder=builder,
            veripsa_root=veripsa,
        ),
        "must not be inside the target",
    )
    assert not builder.calls, "validation failures must happen before graph build"
    assert not output.exists()


def test_extractor_version_metadata(_work: Path) -> None:
    versions, schema = H._extractor_metadata(
        {
            "extractor_version": "fixture-rich-graph-v7",
            "metrics": {"schema_contract_version": 42},
        },
        {"extractor_version": "fixture-audit-index-v3"},
    )
    assert versions == {
        "rich_graph": "fixture-rich-graph-v7",
        "ruby_rails_audit_index": "fixture-audit-index-v3",
    }
    assert schema == 42

    unavailable_versions, unavailable_schema = H._extractor_metadata(
        {
            "extractor_version": "",
            "metrics": {"schema_contract_version": True},
        },
        {},
    )
    assert unavailable_versions == {
        "rich_graph": None,
        "ruby_rails_audit_index": None,
    }
    assert unavailable_schema is None


def test_dirty_states(work: Path) -> None:
    target = _init_repository(work, "dirty-target", fixture=True)
    veripsa = _init_repository(work, "dirty-veripsa-clean", fixture=False)
    tracked = target / "config" / "routes.rb"
    tracked.write_text(
        tracked.read_text(encoding="utf-8") + "\n# local dirty target\n",
        encoding="utf-8",
    )
    dirty_before = _tree_digest(target)
    builder = CountingBuilder(target)
    rejected_output = (work / "dirty-rejected-output").resolve()
    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(target, rejected_output),
            graph_builder=builder,
            veripsa_root=veripsa,
        ),
        "target repository is dirty",
    )
    assert not builder.calls
    assert not rejected_output.exists()

    allowed_output = (work / "dirty-allowed-output").resolve()
    result = H.run_hunt(
        H.HuntConfig(
            target,
            allowed_output,
            allow_dirty_target=True,
        ),
        graph_builder=builder,
        veripsa_root=veripsa,
        exact_command=("python", "-m", "tools.hunt", "--allow-dirty-target"),
    )
    assert result["manifest"]["target_dirty_state"] is True
    assert builder.calls == [(str(target), None)]
    assert _tree_digest(target) == dirty_before

    clean_target = _init_repository(work, "clean-target-for-dirty-v", fixture=True)
    dirty_veripsa = _init_repository(work, "injected-dirty-veripsa", fixture=False)
    (dirty_veripsa / "README.md").write_text(
        "# dirty injected Veripsa\n", encoding="utf-8"
    )
    dirty_veripsa_builder = CountingBuilder(clean_target)
    dirty_veripsa_output = (work / "dirty-veripsa-output").resolve()
    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(clean_target, dirty_veripsa_output),
            graph_builder=dirty_veripsa_builder,
            veripsa_root=dirty_veripsa,
        ),
        "Veripsa worktree is dirty",
    )
    assert not dirty_veripsa_builder.calls
    assert not dirty_veripsa_output.exists()


def test_hidden_index_state_fails_closed(work: Path) -> None:
    veripsa = _init_repository(work, "hidden-index-veripsa", fixture=False)

    assume_target = _init_repository(
        work, "assume-unchanged-target", fixture=True
    )
    assume_path = "config/routes.rb"
    _git(assume_target, "update-index", "--assume-unchanged", assume_path)
    tracked = assume_target / assume_path
    tracked.write_text(
        tracked.read_text(encoding="utf-8")
        + "\n# hidden working-tree bytes\n",
        encoding="utf-8",
    )
    assert _git(assume_target, "status", "--porcelain=v1") == ""
    assume_builder = CountingBuilder(assume_target)
    assume_output = (work / "assume-unchanged-output").resolve()
    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(assume_target, assume_output),
            graph_builder=assume_builder,
            veripsa_root=veripsa,
        ),
        "assume-unchanged or skip-worktree",
    )
    assert not assume_builder.calls
    assert not assume_output.exists()

    sparse_target = _init_repository(work, "skip-worktree-target", fixture=True)
    _git(
        sparse_target,
        "update-index",
        "--skip-worktree",
        "config/routes.rb",
    )
    assert _git(sparse_target, "status", "--porcelain=v1") == ""
    sparse_builder = CountingBuilder(sparse_target)
    sparse_output = (work / "skip-worktree-output").resolve()
    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(sparse_target, sparse_output),
            graph_builder=sparse_builder,
            veripsa_root=veripsa,
        ),
        "assume-unchanged or skip-worktree",
    )
    assert not sparse_builder.calls
    assert not sparse_output.exists()

    clean_target = _init_repository(
        work, "hidden-index-clean-target", fixture=True
    )
    hidden_veripsa = _init_repository(
        work, "assume-unchanged-veripsa", fixture=False
    )
    _git(
        hidden_veripsa,
        "update-index",
        "--assume-unchanged",
        "README.md",
    )
    (hidden_veripsa / "README.md").write_text(
        "# hidden Veripsa implementation bytes\n",
        encoding="utf-8",
    )
    assert _git(hidden_veripsa, "status", "--porcelain=v1") == ""
    veripsa_builder = CountingBuilder(clean_target)
    veripsa_output = (work / "assume-unchanged-veripsa-output").resolve()
    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(clean_target, veripsa_output),
            graph_builder=veripsa_builder,
            veripsa_root=hidden_veripsa,
        ),
        "Veripsa uses assume-unchanged or skip-worktree",
    )
    assert not veripsa_builder.calls
    assert not veripsa_output.exists()


def test_submodule_checkout_fails_closed(work: Path) -> None:
    target = _init_repository(work, "gitlink-target", fixture=True)
    veripsa = _init_repository(work, "gitlink-veripsa", fixture=False)
    dependency = _init_repository(
        work, "gitlink-local-dependency", fixture=False
    )
    _git(
        target,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(dependency),
        "vendor/local-dependency",
    )
    _git(target, "add", ".gitmodules", "vendor/local-dependency")
    _git(
        target,
        "commit",
        "--no-verify",
        "-q",
        "-m",
        "add synthetic gitlink",
    )
    assert _git(target, "status", "--porcelain=v1") == ""
    builder = CountingBuilder(target)
    output = (work / "gitlink-output").resolve()
    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(target, output),
            graph_builder=builder,
            veripsa_root=veripsa,
        ),
        "gitlinks/submodules",
    )
    assert not builder.calls
    assert not output.exists()


def test_artifacts_manifest_and_single_build(work: Path) -> None:
    target = _init_repository(work, "artifact-target", fixture=True)
    veripsa = _init_repository(work, "artifact-veripsa", fixture=False)
    output = (work / "artifact-output").resolve()
    before = _tree_digest(target)
    before_status = _git(target, "status", "--porcelain=v1")
    builder = CountingBuilder(target)
    exact_command = (
        "python",
        "-m",
        "tools.hunt",
        "--target",
        str(target),
        "--output",
        str(output),
    )
    result = H.run_hunt(
        H.HuntConfig(target, output),
        graph_builder=builder,
        veripsa_root=veripsa,
        exact_command=exact_command,
    )

    assert builder.calls == [(str(target), None)], (
        "build_graph must be called exactly once with universe_paths=None"
    )
    assert {path.name for path in output.iterdir()} == EXPECTED_ARTIFACTS
    assert _read_json(output / "nodes.json") == _fake_graph(target)["nodes"]
    assert _read_json(output / "edges.json") == _fake_graph(target)["edges"]

    manifest = _read_json(output / "run_manifest.json")
    assert manifest == result["manifest"]
    assert manifest["target_absolute_path"] == str(target)
    assert manifest["target_repository_name"] == target.name
    assert manifest["target_commit_sha"] == _git(target, "rev-parse", "HEAD")
    assert manifest["target_dirty_state"] is False
    assert manifest["veripsa_commit_sha"] == _git(veripsa, "rev-parse", "HEAD")
    assert manifest["veripsa_dirty_state"] is False
    assert manifest["exact_command"] == list(exact_command)
    assert manifest["extractor_versions"] == {
        "rich_graph": "fixture-rich-graph-v7",
        "ruby_rails_audit_index": "hunt-ruby-rails-v1",
    }
    assert manifest["graph_schema_contract_version"] == 42
    assert manifest["node_count"] == 2
    assert manifest["edge_count"] == 1
    assert manifest["files_parsed"] == 9
    assert manifest["files_failed"] is None
    assert manifest["files_skipped"] is None
    assert manifest["warning_count"] == len(_read_json(output / "warnings.json"))
    assert set(manifest["artifacts"]) == EXPECTED_ARTIFACTS
    assert isinstance(manifest["python_version"], str)
    assert set(manifest["relevant_package_versions"]) == {
        "tree-sitter",
        "tree-sitter-ruby",
        "tree-sitter-language-pack",
    }
    assert all(
        value is None or isinstance(value, str)
        for value in manifest["relevant_package_versions"].values()
    )
    assert manifest["maximum_rss_bytes"] is None or isinstance(
        manifest["maximum_rss_bytes"], int
    )
    timestamp = dt.datetime.fromisoformat(manifest["utc_timestamp"])
    assert timestamp.tzinfo is not None

    assert _tree_digest(target) == before
    assert _git(target, "status", "--porcelain=v1") == before_status == ""


def test_no_network_or_database(work: Path) -> None:
    target = _init_repository(work, "offline-target", fixture=True)
    veripsa = _init_repository(work, "offline-veripsa", fixture=False)
    output = (work / "offline-output").resolve()
    before = _tree_digest(target)
    with _no_network_or_db() as calls:
        H.run_hunt(
            H.HuntConfig(target, output),
            veripsa_root=veripsa,
            exact_command=("python", "-m", "tools.hunt"),
        )
    assert not calls["network"], calls
    assert not calls["database"], calls
    assert calls["subprocess"], "local git validation should have been observed"
    assert all(command[0] == "git" for command in calls["subprocess"])
    assert _tree_digest(target) == before
    assert _git(target, "status", "--porcelain=v1") == ""


def test_git_fsmonitor_is_disabled(work: Path) -> None:
    target = _init_repository(work, "fsmonitor-target", fixture=True)
    veripsa = _init_repository(work, "fsmonitor-veripsa", fixture=False)
    output = (work / "fsmonitor-output").resolve()
    marker = (work / "fsmonitor-was-invoked").resolve()
    hook = (work / "hostile-fsmonitor").resolve()
    hook.write_text(
        "#!/bin/sh\n"
        f": > {shlex.quote(str(marker))}\n"
        "exit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    _git(target, "config", "core.fsmonitor", str(hook))

    H.run_hunt(
        H.HuntConfig(target, output),
        graph_builder=CountingBuilder(target),
        veripsa_root=veripsa,
        exact_command=("python", "-m", "tools.hunt"),
    )
    assert not marker.exists(), "repository-local core.fsmonitor hook was executed"
    assert {path.name for path in output.iterdir()} == EXPECTED_ARTIFACTS


def test_state_change_during_analysis_fails_closed(work: Path) -> None:
    target = _init_repository(work, "moving-target", fixture=True)
    veripsa = _init_repository(work, "moving-veripsa", fixture=False)
    output = (work / "moving-output").resolve()

    def mutating_builder(root: str, *, universe_paths: Any) -> dict[str, Any]:
        assert universe_paths is None
        (Path(root) / "changed_during_analysis.rb").write_text(
            "class ChangedDuringAnalysis; end\n",
            encoding="utf-8",
        )
        return _fake_graph(Path(root))

    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(target, output),
            graph_builder=mutating_builder,
            veripsa_root=veripsa,
            exact_command=("python", "-m", "tools.hunt"),
        ),
        "state changed during analysis",
    )
    assert not output.exists(), "mixed-snapshot failure must precede artifact writes"


def test_ignored_analyzable_input_fails_closed(work: Path) -> None:
    target = _init_repository(work, "ignored-source-target", fixture=True)
    veripsa = _init_repository(work, "ignored-source-veripsa", fixture=False)
    output = (work / "ignored-source-output").resolve()
    ignored_name = unicodedata.normalize("NFD", "café_override.rb")
    (target / ".gitignore").write_text(
        "*override.rb\n",
        encoding="utf-8",
    )
    _git(target, "add", ".gitignore")
    _git(target, "commit", "--no-verify", "-q", "-m", "ignore local override")
    (target / ignored_name).write_text(
        "class LocalOverride; end\n",
        encoding="utf-8",
    )
    assert _git(target, "status", "--porcelain=v1") == ""

    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(target, output),
            veripsa_root=veripsa,
            exact_command=("python", "-m", "tools.hunt"),
        ),
        "ignored untracked files entered the rich graph",
    )
    assert not output.exists()


def test_ignored_edge_and_attributes_fail_closed(work: Path) -> None:
    veripsa = _init_repository(
        work, "ignored-edge-attributes-veripsa", fixture=False
    )

    edge_target = _init_repository(work, "ignored-edge-target", fixture=True)
    (edge_target / ".gitignore").write_text(
        "ignored_route.rb\n",
        encoding="utf-8",
    )
    _git(edge_target, "add", ".gitignore")
    _git(
        edge_target,
        "commit",
        "--no-verify",
        "-q",
        "-m",
        "ignore route-only input",
    )
    (edge_target / "ignored_route.rb").write_text(
        "post '/edge-only', to: 'exports#create'\n",
        encoding="utf-8",
    )
    assert _git(edge_target, "status", "--porcelain=v1") == ""

    def edge_only_builder(root: str, *, universe_paths: Any) -> dict[str, Any]:
        assert Path(root) == edge_target
        assert universe_paths is None
        graph = _fake_graph(edge_target)
        graph["edges"].append(
            {
                "src": "ignored_route.rb",
                "dst": "config/routes.rb",
                "kind": "imports",
            }
        )
        return graph

    edge_output = (work / "ignored-edge-output").resolve()
    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(edge_target, edge_output),
            graph_builder=edge_only_builder,
            veripsa_root=veripsa,
        ),
        "ignored untracked files entered the rich graph",
    )
    assert not edge_output.exists()

    attributes_target = _init_repository(
        work, "ignored-attributes-target", fixture=True
    )
    (attributes_target / ".gitignore").write_text(
        ".gitattributes\n",
        encoding="utf-8",
    )
    _git(attributes_target, "add", ".gitignore")
    _git(
        attributes_target,
        "commit",
        "--no-verify",
        "-q",
        "-m",
        "ignore local attributes",
    )
    (attributes_target / ".gitattributes").write_text(
        "*.rb linguist-generated\n",
        encoding="utf-8",
    )
    assert _git(attributes_target, "status", "--porcelain=v1") == ""
    attributes_builder = CountingBuilder(attributes_target)
    attributes_output = (work / "ignored-attributes-output").resolve()
    _expect_hunt_error(
        lambda: H.run_hunt(
            H.HuntConfig(attributes_target, attributes_output),
            graph_builder=attributes_builder,
            veripsa_root=veripsa,
        ),
        "ignored untracked .gitattributes",
    )
    assert not attributes_builder.calls
    assert not attributes_output.exists()


def test_actual_module_cli_is_output_only(work: Path) -> None:
    runtime = _init_runtime_repository(work)
    target = _init_repository(work, "subprocess-target", fixture=True)
    (target / ".gitignore").write_text("ignored-local.txt\n", encoding="utf-8")
    _git(target, "add", ".gitignore")
    _git(target, "commit", "--no-verify", "-q", "-m", "ignore local evidence")
    ignored = target / "ignored-local.txt"
    ignored.write_text("must remain untouched\n", encoding="utf-8")
    assert _git(target, "status", "--porcelain=v1") == ""

    output = (work / "subprocess-output").resolve()
    target_before = _tree_digest(target)
    runtime_before = _tree_digest(runtime)
    environment = {
        **GIT_ENV,
        "PYTHONHASHSEED": "0",
    }
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment.pop("VERIPSA_DSN", None)
    command = [
        sys.executable,
        "-m",
        "tools.hunt",
        "--target",
        str(target),
        "--output",
        str(output),
    ]
    result = subprocess.run(
        command,
        cwd=runtime,
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert result.returncode == 0, (
        f"actual module CLI failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "hunt complete" in result.stdout
    assert not result.stderr
    assert {path.name for path in output.iterdir()} == EXPECTED_ARTIFACTS
    assert _tree_digest(target) == target_before
    assert ignored.read_text(encoding="utf-8") == "must remain untouched\n"
    assert _git(target, "status", "--porcelain=v1") == ""
    assert _tree_digest(runtime) == runtime_before, (
        "bare module CLI wrote outside its output directory"
    )
    assert _git(runtime, "status", "--porcelain=v1") == ""
    assert not list(runtime.rglob("__pycache__"))
    assert not list(runtime.rglob("*.pyc"))

    manifest = _read_json(output / "run_manifest.json")
    exact_command = manifest["exact_command"]
    assert isinstance(exact_command, list), manifest
    assert "-m" in exact_command and "tools.hunt" in exact_command, manifest
    assert manifest["exact_command_capture"] in {
        "sys.orig_argv",
        "reconstructed_module_argv",
    }


def test_include_exclude_semantics(work: Path) -> None:
    target = _init_repository(work, "pattern-target", fixture=True)
    veripsa = _init_repository(work, "pattern-veripsa", fixture=False)
    output = (work / "pattern-output").resolve()
    builder = CountingBuilder(target)
    config = H.HuntConfig(
        target,
        output,
        include_patterns=("app/workers/*.rb",),
        exclude_patterns=("app/workers/scheduled_export_worker.rb",),
    )
    H.run_hunt(
        config,
        graph_builder=builder,
        veripsa_root=veripsa,
        exact_command=("python", "-m", "tools.hunt", "--include-pattern"),
    )

    assert builder.calls == [(str(target), None)]
    assert len(_read_json(output / "nodes.json")) == 2, (
        "include/exclude filters audit indexes, not the full rich graph"
    )
    assert _read_json(output / "rails_routes.json") == []
    assert _read_json(output / "authorization_calls.json") == []
    jobs = _read_json(output / "sidekiq_jobs.json")
    assert [worker["class_name"] for worker in jobs["workers"]] == [
        "ExportWorker"
    ]
    assert jobs["enqueue_sites"] == []
    assert _read_json(output / "execution_paths.json") == []
    manifest = _read_json(output / "run_manifest.json")
    assert manifest["include_patterns"] == ["app/workers/*.rb"]
    assert manifest["exclude_patterns"] == [
        "app/workers/scheduled_export_worker.rb"
    ]


def test_deterministic_outputs(work: Path) -> None:
    target = _init_repository(work, "determinism-target", fixture=True)
    veripsa = _init_repository(work, "determinism-veripsa", fixture=False)
    first = (work / "determinism-output-a").resolve()
    second = (work / "determinism-output-b").resolve()
    builder_a = CountingBuilder(target)
    builder_b = CountingBuilder(target)

    H.run_hunt(
        H.HuntConfig(target, first),
        graph_builder=builder_a,
        veripsa_root=veripsa,
        exact_command=("python", "-m", "tools.hunt", "--output", str(first)),
    )
    H.run_hunt(
        H.HuntConfig(target, second),
        graph_builder=builder_b,
        veripsa_root=veripsa,
        exact_command=("python", "-m", "tools.hunt", "--output", str(second)),
    )
    assert builder_a.calls == builder_b.calls == [(str(target), None)]
    for artifact in sorted(STABLE_ARTIFACTS):
        assert (first / artifact).read_bytes() == (second / artifact).read_bytes(), (
            f"stable analysis artifact differs between runs: {artifact}"
        )
    assert _normalized_manifest(first / "run_manifest.json") == (
        _normalized_manifest(second / "run_manifest.json")
    )


def test_main_wiring(work: Path) -> None:
    target = _init_repository(work, "main-target", fixture=True)
    output = (work / "main-output").resolve()
    captured: list[H.HuntConfig] = []
    original_run_hunt = H.run_hunt

    def fake_run_hunt(config: H.HuntConfig, **_keywords: Any) -> dict[str, Any]:
        captured.append(config)
        return {
            "manifest": {"node_count": 2, "edge_count": 1, "warning_count": 0},
            "output": config.output,
        }

    H.run_hunt = fake_run_hunt
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = H.main(
                [
                    "--target",
                    str(target),
                    "--output",
                    str(output),
                    "--allow-dirty-target",
                    "--include-pattern",
                    "app/**/*.rb",
                    "--exclude-pattern",
                    "vendor/**",
                ]
            )
    finally:
        H.run_hunt = original_run_hunt
    assert result == 0
    assert not stderr.getvalue()
    assert "hunt complete" in stdout.getvalue()
    assert captured == [
        H.HuntConfig(
            target=target,
            output=output,
            allow_dirty_target=True,
            include_patterns=("app/**/*.rb",),
            exclude_patterns=("vendor/**",),
        )
    ]

    missing_output = (work / "main-missing-output").resolve()
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = H.main(
            [
                "--target",
                str((work / "main-missing-target").resolve()),
                "--output",
                str(missing_output),
            ]
        )
    assert result == 2
    assert not stdout.getvalue()
    assert "does not exist" in stderr.getvalue()
    assert not missing_output.exists()


def main() -> int:
    tests = [
        test_validation,
        test_extractor_version_metadata,
        test_dirty_states,
        test_hidden_index_state_fails_closed,
        test_submodule_checkout_fails_closed,
        test_artifacts_manifest_and_single_build,
        test_no_network_or_database,
        test_git_fsmonitor_is_disabled,
        test_state_change_during_analysis_fails_closed,
        test_ignored_analyzable_input_fails_closed,
        test_ignored_edge_and_attributes_fail_closed,
        test_actual_module_cli_is_output_only,
        test_include_exclude_semantics,
        test_deterministic_outputs,
        test_main_wiring,
    ]
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="hunt_cli_gate_") as temporary:
        root = Path(temporary).resolve()
        for index, test in enumerate(tests):
            work = root / f"{index:02d}-{test.__name__}"
            work.mkdir()
            try:
                test(work)
            except AssertionError as exc:
                failures.append(f"{test.__name__}: {exc}")
                print(f"  [FAIL] {test.__name__}: {exc}")
            except Exception as exc:  # noqa: BLE001 - preserve actionable gate diagnostics
                failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
                print(
                    f"  [ERROR] {test.__name__}: "
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                print(f"  [PASS] {test.__name__}")

    print("HUNT CLI GATE:", "PASS" if not failures else "FAIL")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
