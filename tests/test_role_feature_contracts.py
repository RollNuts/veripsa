#!/usr/bin/env python3
"""Role-feature contract graph gate."""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _cg_jobs import _job_queue_graph  # noqa: E402
from _cg_role_feature import _role_feature_graph  # noqa: E402


def _touch(root: str, rel: str, body: str = "// empty\n") -> tuple[str, str]:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path, os.path.splitext(rel)[1]


def _edge_pairs(edges):
    return {(e["src"], e["dst"], e["kind"]) for e in edges}


def run() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as root:
        source_files = [
            _touch(root, "internal/db/usage.go", "// SUPER_SECRET_DB_TOKEN\n"),
            _touch(root, "internal/server/huma_routes_usage.go", "// SUPER_SECRET_ROUTE_TOKEN\n"),
            _touch(root, "internal/server/auth.go"),
        ]
        nodes, edges = _role_feature_graph(root, source_files)
        want = "role_feature::go_internal::internal::db__server::usage"
        if ("internal/db/usage.go", want, "queries") not in _edge_pairs(edges):
            failures.append("go internal db/server files sharing feature token should couple")
        if any(e["src"] == "internal/server/auth.go" for e in edges):
            failures.append("unshared role files must stay silent")
        if "SUPER_SECRET" in repr((nodes, edges)):
            failures.append("role-feature graph must not emit source file bodies")

    with tempfile.TemporaryDirectory() as root:
        source_files = [
            _touch(root, "frontend/src/components/SettingsLayout.jsx"),
            _touch(root, "frontend/src/pages/Settings.jsx"),
            _touch(root, "frontend/src/components/Layout.jsx"),
        ]
        _nodes, edges = _role_feature_graph(root, source_files)
        want = "role_feature::web_frontend::frontend::src::components__pages::setting"
        if ("frontend/src/components/SettingsLayout.jsx", want, "queries") not in _edge_pairs(edges):
            failures.append("frontend component/page files sharing feature token should couple")
        if any(e["src"] == "frontend/src/components/Layout.jsx" for e in edges):
            failures.append("generic UI tokens like layout must stay silent")

    with tempfile.TemporaryDirectory() as root:
        source_files = [
            _touch(root, "frontend/src/components/SettingsPanel.tsx"),
            _touch(root, "frontend/src/pages/settings/index.tsx"),
            _touch(root, "frontend/src/pages/index.tsx"),
            _touch(root, "frontend/src/pages/[slug].tsx"),
        ]
        _nodes, edges = _role_feature_graph(root, source_files)
        want = "role_feature::web_frontend::frontend::src::components__pages::setting"
        edge_pairs = _edge_pairs(edges)
        if ("frontend/src/pages/settings/index.tsx", want, "queries") not in edge_pairs:
            failures.append("frontend generic page basenames should use the nearest stable route segment")
        if any(e["src"] in {"frontend/src/pages/index.tsx", "frontend/src/pages/[slug].tsx"} for e in edges):
            failures.append("root index pages and dynamic-only pages must stay silent")

    with tempfile.TemporaryDirectory() as root:
        source_files = [
            _touch(root, "frontend/src/components/ProjectList.tsx"),
            _touch(root, "frontend/src/pages/projects/[id].tsx"),
        ]
        _nodes, edges = _role_feature_graph(root, source_files)
        want = "role_feature::web_frontend::frontend::src::components__pages::project"
        if ("frontend/src/pages/projects/[id].tsx", want, "queries") not in _edge_pairs(edges):
            failures.append("frontend dynamic pages should use a stable parent route segment")

    with tempfile.TemporaryDirectory() as root:
        source_files = [
            _touch(root, "src-tauri/src/commands/agent.rs"),
            _touch(root, "src/pages/agents.js"),
            _touch(root, "src/pages/settings.js"),
        ]
        _nodes, edges = _role_feature_graph(root, source_files)
        want = "role_feature::tauri_command_page::tauri::commands__pages::agent"
        if ("src-tauri/src/commands/agent.rs", want, "queries") not in _edge_pairs(edges):
            failures.append("tauri command/page singular-plural feature token should couple")
        if any(e["src"] == "src/pages/settings.js" for e in edges):
            failures.append("unmatched pages must stay silent")

    with tempfile.TemporaryDirectory() as root:
        source_files = [
            _touch(root, "internal/db/usage.go"),
            _touch(root, "internal/server/usage_test.go"),
            _touch(root, "internal/generated/usage.go"),
            _touch(root, "cmd/agent/usage.go"),
        ]
        _nodes, edges = _role_feature_graph(root, source_files)
        if edges:
            failures.append(f"tests/generated/unallowlisted roles should stay silent, got {edges!r}")

    with tempfile.TemporaryDirectory() as root:
        source_files = [_touch(root, "internal/db/usage.go")]
        source_files.extend(_touch(root, f"internal/server/huma_routes_usage_{i}.go") for i in range(8))
        _nodes, edges = _role_feature_graph(root, source_files)
        if edges:
            failures.append("role-feature groups larger than the dampening threshold must stay silent")

    with tempfile.TemporaryDirectory() as root:
        source_files = [
            _touch(root, "internal/db/usage.go"),
            _touch(root, "internal/server/huma_routes_usage.go"),
        ]
        _nodes, edges = _job_queue_graph(root, source_files)
        want = "role_feature::go_internal::internal::db__server::usage"
        if ("internal/server/huma_routes_usage.go", want, "queries") not in _edge_pairs(edges):
            failures.append("_job_queue_graph should include role-feature edges through the existing integration point")

    ok = not failures
    print("ROLE FEATURE CONTRACT GATE:", "PASS" if ok else "FAIL")
    for failure in failures:
        print("  -", failure)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
