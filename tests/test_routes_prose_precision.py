#!/usr/bin/env python3
"""Gate: Python route extraction ignores prose examples but keeps real decorators.

The cross-repo shadow runner exposed a dogfood false signal: _cg_routes.py's own docstring/comment
examples minted clean route:: keys such as `route::api/items`. Whitespace filtering caught wrapped prose,
but not one-line examples. This gate pins the stronger rule: Python comments and triple-quoted prose are
not route definitions; real decorators and APIRouter prefixes still are. Content-free: graph output must
not leak comment/docstring bodies.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import _cg_routes as R


SECRET = "VERIPSA_PROSE_SECRET_SHOULD_NOT_LEAK"


PY_API = f'''"""
Docs-only examples. These must not become route definitions:
@app.get("/api/docstring")
router = APIRouter(prefix="/api/docstring-prefix")
{SECRET}
"""
from fastapi import APIRouter, FastAPI

app = FastAPI()
router = APIRouter(prefix="/api/items")

# @app.get("/api/comment")
# router = APIRouter(prefix="/api/comment-prefix")
@app.get("/api/real")
def real():
    return {{}}

@router.get("/{{id}}")
def item(id):
    return {{"id": id}}
'''


def _write(root: str, rel: str, body: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def main() -> int:
    failures: list[str] = []

    defs = R.extract_route_defs("server/api.py", PY_API)
    if "/api/real" not in defs:
        failures.append("real FastAPI decorator was dropped")
    if "/api/items/{}" not in defs:
        failures.append("real APIRouter(prefix=...) + decorator join was dropped")
    for noisy in ("/api/docstring", "/api/docstring-prefix", "/api/comment", "/api/comment-prefix"):
        if noisy in defs:
            failures.append(f"prose/comment route definition was emitted: {noisy}")

    root = tempfile.mkdtemp(prefix="routes_prose_precision_")
    old_flag = os.environ.get("VERIPSA_CROSS_REPO_KEYS")
    try:
        os.environ["VERIPSA_CROSS_REPO_KEYS"] = "1"
        _write(root, "server/api.py", PY_API)
        _write(root, "web/client.ts",
               "export const load = () => fetch('/api/real').then(r => r.json())\n")

        scan = R.scan_repo(root, specificity_floor=True)
        edges = R._routes_xrepo_edges(scan)
        payload = json.dumps(_jsonable({"scan": scan, "edges": edges}), sort_keys=True)

        if SECRET in payload:
            failures.append("comment/docstring body leaked into route graph output")
        emitted_keys = {e["dst"] for e in edges if e.get("dst", "").startswith("route::")}
        if "route::api/real" not in emitted_keys:
            failures.append(f"real route:: key missing from xrepo edges; got {sorted(emitted_keys)!r}")
        bad_keys = [k for k in emitted_keys if "docstring" in k or "comment" in k]
        if bad_keys:
            failures.append(f"prose/comment route:: keys emitted: {bad_keys!r}")
    finally:
        if old_flag is None:
            os.environ.pop("VERIPSA_CROSS_REPO_KEYS", None)
        else:
            os.environ["VERIPSA_CROSS_REPO_KEYS"] = old_flag
        shutil.rmtree(root, ignore_errors=True)

    if failures:
        for failure in failures:
            print("FAIL:", failure)
        print("ROUTES PROSE PRECISION GATE: FAIL")
        return 1
    print("ROUTES PROSE PRECISION GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
