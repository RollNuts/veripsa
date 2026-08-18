#!/usr/bin/env python3
"""CI SCRIPT CONTRACT GATE.

GitHub Actions workflows depend on package-manager scripts declared in package.json.
Those files have no import/call edge, but deleting or renaming the script breaks the
merge gate. This gate pins a precision-first, content-free contract extractor:

  1. Crown jewel: workflow `run: npm run typecheck` couples to package.json `scripts.typecheck`.
  2. Monorepo same-name scripts resolve only through `working-directory` or `cd`.
  3. Ambiguous same-name scripts without package context remain as inert evidence.
  4. Comments, non-workflow yaml, and non-`run:` prose do not couple.
  5. Content-free: script bodies and workflow comments never appear in graph output.
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


def _has_ci_edge(g: dict, pkg_dir: str, name: str, kind: str, src_part: str) -> bool:
    dst = f"ci_script::{pkg_dir}::{name}"
    return any(src_part in src for src in _edges_to(g, dst, kind))


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # (1) Crown jewel: a root package script used by GitHub Actions.
    g1 = _build({
        "package.json": json.dumps({
            "scripts": {
                "typecheck": "echo SECRET_SCRIPT_BODY_SHOULD_NOT_LEAK && tsc --noEmit",
                "test": "vitest",
            }
        }),
        ".github/workflows/ci.yml": (
            "name: ci\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: npm run typecheck # SECRET_WORKFLOW_COMMENT_SHOULD_NOT_LEAK\n"
        ),
        "src/app.ts": "export const ok = true;\n",
    })
    nodes1 = _nodes_of_kind(g1, "ci_script")
    check("ci_script::.::typecheck" in nodes1,
          f"(1) ci_script node missing; nodes={list(nodes1)}")
    check(_has_ci_edge(g1, ".", "typecheck", "alters", "package.json"),
          f"(1) package.json must alter typecheck; alters={_edges_to(g1, 'ci_script::.::typecheck', 'alters')}")
    check(_has_ci_edge(g1, ".", "typecheck", "queries", "ci.yml"),
          f"(1) workflow must query typecheck; queries={_edges_to(g1, 'ci_script::.::typecheck', 'queries')}")
    check(not _direct_code_edges_between(g1, "package.json", "ci.yml"),
          f"(1) coupling must be through ci_script key, not a direct code edge; got={_direct_code_edges_between(g1, 'package.json', 'ci.yml')}")

    # (2) Monorepo same-name scripts resolve through working-directory and through `cd`.
    g2 = _build({
        "packages/web/package.json": json.dumps({"scripts": {"test": "vitest", "build": "vite build"}}),
        "packages/api/package.json": json.dumps({"scripts": {"test": "pytest", "build": "python -m build"}}),
        ".github/workflows/ci.yml": (
            "jobs:\n"
            "  test:\n"
            "    steps:\n"
            "      - name: web\n"
            "        working-directory: packages/web\n"
            "        run: yarn test\n"
            "      - name: api\n"
            "        run: |\n"
            "          cd packages/api && pnpm build\n"
        ),
    })
    check(_has_ci_edge(g2, "packages/web", "test", "queries", "ci.yml"),
          "(2) working-directory should resolve packages/web test")
    check(_has_ci_edge(g2, "packages/api", "build", "queries", "ci.yml"),
          "(2) cd should resolve packages/api build")
    check(not _has_ci_edge(g2, "packages/api", "test", "queries", "ci.yml"),
          "(2) web test should not query api test")

    # (2b) Package-manager builtins are not script references, even if a script with that name exists.
    g2b = _build({
        "package.json": json.dumps({"scripts": {"install": "node scripts/install.js"}}),
        ".github/workflows/ci.yml": "jobs:\n  test:\n    steps:\n      - run: yarn install\n",
    })
    check(not _has_ci_edge(g2b, ".", "install", "queries", "ci.yml"),
          "(2b) package-manager builtins must not be treated as script calls")

    # (3) Ambiguous same-name scripts without working-directory/cd retain every
    # candidate edge, explicitly inert so effective adjacency cannot couple it.
    g3 = _build({
        "packages/web/package.json": json.dumps({"scripts": {"test": "vitest"}}),
        "packages/api/package.json": json.dumps({"scripts": {"test": "pytest"}}),
        ".github/workflows/ci.yml": "jobs:\n  test:\n    steps:\n      - run: npm run test\n",
    })
    ambiguous_ids = {
        "ci_script::packages/web::test",
        "ci_script::packages/api::test",
    }
    nodes3 = _nodes_of_kind(g3, "ci_script")
    check(set(nodes3) == ambiguous_ids and all(nodes3[n].get("ambiguous") for n in ambiguous_ids),
          f"(3) ambiguous monorepo candidates must mint inert nodes; nodes={nodes3!r}")
    ambiguous_edges = [
        edge
        for dst in ambiguous_ids
        for kind in ("alters", "queries")
        for edge in _edge_records_to(g3, dst, kind)
    ]
    check(
        len(ambiguous_edges) == 4
        and all(edge.get("reference_status") == "ambiguous" for edge in ambiguous_edges),
        f"(3) both candidate alters/queries edges must be ambiguous; edges={ambiguous_edges!r}",
    )
    check(
        not any(
            edge.get("reference_status") is None
            for dst in ambiguous_ids
            for kind in ("alters", "queries")
            for edge in _edge_records_to(g3, dst, kind)
        ),
        "(3) ambiguous monorepo evidence must not enter effective adjacency",
    )
    statuses3 = {
        n.get("path"): n.get("analysis_status")
        for n in g3["nodes"]
        if n.get("kind") in {"file", "config_file"}
        and n.get("path") in {
            "packages/web/package.json",
            "packages/api/package.json",
            ".github/workflows/ci.yml",
        }
    }
    check(
        set(statuses3) == {
            "packages/web/package.json",
            "packages/api/package.json",
            ".github/workflows/ci.yml",
        }
        and set(statuses3.values()) == {"ambiguous"},
        f"(3) all ambiguous CI documents must degrade to Unknown; got {statuses3!r}",
    )
    check(
        (g3.get("metrics") or {}).get("ambiguous_reference_count", 0) >= 1,
        f"(3) ambiguous CI reference must be observable; metrics={g3.get('metrics')!r}",
    )

    # (4) Comments, prose, and non-workflow yaml do not create script references.
    g4 = _build({
        "package.json": json.dumps({"scripts": {"deploy": "node deploy.js"}}),
        ".github/workflows/ci.yml": (
            "jobs:\n"
            "  test:\n"
            "    steps:\n"
            "      - name: text says npm run deploy but does not run it\n"
            "      # run: npm run deploy\n"
            "      - run: echo npm run deploy\n"
        ),
        "docs/example.yml": "run: npm run deploy\n",
    })
    check(not _has_ci_edge(g4, ".", "deploy", "queries", "ci.yml"),
          f"(4) comments/prose/echo must not query deploy; queries={_edges_to(g4, 'ci_script::.::deploy', 'queries')}")

    # (5) Content-free and never-crash on malformed package json.
    g5 = _build({
        "bad/package.json": "{not-json",
        "package.json": json.dumps({"scripts": {"lint": "echo SECRET_LINT_BODY_SHOULD_NOT_LEAK"}}),
        ".github/workflows/ci.yml": (
            "jobs:\n"
            "  lint:\n"
            "    steps:\n"
            "      - run: npm run lint # SECRET_COMMENT_SHOULD_NOT_LEAK\n"
        ),
    })
    blob = json.dumps(g5, sort_keys=True)
    for secret in ("SECRET_LINT_BODY_SHOULD_NOT_LEAK", "SECRET_COMMENT_SHOULD_NOT_LEAK"):
        check(secret not in blob, f"(5) content-free violation: {secret} leaked into graph")
    check(_has_ci_edge(g5, ".", "lint", "queries", "ci.yml"),
          "(5) malformed unrelated package.json must not abort valid script extraction")

    if failures:
        print("CI SCRIPT CONTRACT GATE: FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("CI SCRIPT CONTRACT GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
