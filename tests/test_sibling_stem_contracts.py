#!/usr/bin/env python3
"""Sibling-stem contract graph gate."""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from _cg_sibling import _sibling_stem_graph  # noqa: E402
from _cg_jobs import _job_queue_graph  # noqa: E402


def _touch(root: str, rel: str) -> tuple[str, str]:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# empty\n")
    return path, os.path.splitext(rel)[1]


def _edge_pairs(edges):
    return {(e["src"], e["dst"], e["kind"]) for e in edges}


def run() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as root:
        source_files = [
            _touch(root, "internal/db/user.go"),
            _touch(root, "internal/server/user.go"),
            _touch(root, "internal/server/order.go"),
        ]
        nodes, edges = _sibling_stem_graph(root, source_files)
        if len(nodes) != 1:
            failures.append(f"expected one sibling_stem node, got {nodes!r}")
        dsts = {e["dst"] for e in edges}
        if dsts != {"sibling_stem::go::internal::user"}:
            failures.append(f"unexpected sibling dsts: {dsts!r}")
        srcs = {e["src"] for e in edges}
        if srcs != {"internal/db/user.go", "internal/server/user.go"}:
            failures.append(f"unexpected sibling srcs: {srcs!r}")

    with tempfile.TemporaryDirectory() as root:
        source_files = [
            _touch(root, "frontend/src/components/Dashboard.tsx"),
            _touch(root, "frontend/src/pages/Dashboard.tsx"),
            _touch(root, "frontend/src/pages/index.tsx"),
            _touch(root, "frontend/src/components/index.tsx"),
        ]
        nodes, edges = _sibling_stem_graph(root, source_files)
        dsts = {e["dst"] for e in edges}
        if dsts != {"sibling_stem::web::frontend::src::dashboard"}:
            failures.append(f"generic index/page stems should stay silent, got {dsts!r}")

    with tempfile.TemporaryDirectory() as root:
        source_files = [_touch(root, f"internal/pkg{i}/user.go") for i in range(9)]
        nodes, edges = _sibling_stem_graph(root, source_files)
        if nodes or edges:
            failures.append("groups larger than the dampening threshold must stay silent")

    with tempfile.TemporaryDirectory() as root:
        source_files = [
            _touch(root, "internal/db/user.go"),
            _touch(root, "cmd/server/user.go"),
        ]
        nodes, edges = _sibling_stem_graph(root, source_files)
        if nodes or edges:
            failures.append("different coarse scopes should stay silent")

    with tempfile.TemporaryDirectory() as root:
        source_files = [
            _touch(root, "internal/db/user.go"),
            _touch(root, "internal/server/user.go"),
        ]
        _nodes, edges = _job_queue_graph(root, source_files)
        if (
            "internal/db/user.go",
            "sibling_stem::go::internal::user",
            "queries",
        ) not in _edge_pairs(edges):
            failures.append("_job_queue_graph should include sibling-stem edges through the existing integration point")

    ok = not failures
    print("SIBLING STEM CONTRACT GATE:", "PASS" if ok else "FAIL")
    for failure in failures:
        print("  -", failure)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
