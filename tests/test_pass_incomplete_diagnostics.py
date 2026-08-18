#!/usr/bin/env python3
"""Dedicated extraction-pass completeness diagnostics gate.

Pins the contract that bounded/never-crash producers keep their historical
``(nodes, edges)`` return shape while reporting every path whose substrate
evidence may be incomplete. No DB or network is required.
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import _cg_api_contract as API  # noqa: E402
import _cg_config as CONFIG  # noqa: E402
import _cg_iac as IAC  # noqa: E402
import _cg_io as IO  # noqa: E402
import _cg_openapi as OPENAPI  # noqa: E402
import _cg_routes as ROUTES  # noqa: E402
import _cg_tauri as TAURI  # noqa: E402


def _write(root: str, rel: str, text: str) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _flask(route: str) -> str:
    return (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        f"@app.get('{route}')\n"
        "def handler():\n"
        "    return 'ok'\n"
    )


def main() -> int:
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    # Shared reader: distinguish exactly-capped input from an actually truncated
    # file and preserve the historical string return value.
    with tempfile.TemporaryDirectory(prefix="pass_diag_read_") as root:
        path = _write(root, "large.py", "x" * (IO._MAX_SCAN_BYTES + 17))
        incomplete: set[str] = set()
        text = IO._read_capped(path, incomplete, "large.py")
        check(text is not None and len(text) == IO._MAX_SCAN_BYTES,
              "capped reader must return exactly its bounded prefix")
        check(incomplete == {"large.py"},
              f"capped reader must report truncation; got {incomplete!r}")

    # Wrapper exception: keep the 2-tuple API and mark only the Tauri candidate
    # surface, not an unrelated Python input.
    with tempfile.TemporaryDirectory(prefix="pass_diag_wrapper_") as root:
        rust = _write(root, "src-tauri/src/lib.rs", "#[tauri::command]\nfn greet() {}\n")
        front = _write(root, "src/app.ts", "invoke('greet')\n")
        unrelated = _write(root, "tools/helper.py", "def helper(): pass\n")
        incomplete = set()
        with mock.patch.object(
            TAURI,
            "_tauri_command_graph_impl",
            side_effect=RuntimeError("forced fixture failure"),
        ):
            result = TAURI._tauri_command_graph(
                root,
                [(rust, ".rs"), (front, ".ts"), (unrelated, ".py")],
                incomplete_paths_out=incomplete,
            )
        check(result == ([], []), f"wrapper must preserve empty 2-tuple fallback; got {result!r}")
        check(incomplete == {"src-tauri/src/lib.rs", "src/app.ts"},
              f"wrapper must report only relevant candidates; got {incomplete!r}")

    # Config definition catalog cap: one dropped definition can be consumed by
    # any code candidate, so both the definer and all candidate consumers become
    # incomplete. A normal fixture must remain diagnostic-free.
    with tempfile.TemporaryDirectory(prefix="pass_diag_config_") as root:
        env = _write(
            root,
            ".env",
            "FEATURE_ALPHA_FLAG=true\nFEATURE_BETA_FLAG=true\n",
        )
        consumer = _write(
            root,
            "app.py",
            "import os\nprint(os.environ['FEATURE_ALPHA_FLAG'])\n",
        )
        incomplete = set()
        with mock.patch.object(CONFIG, "_MAX_CONFIG_KEYS", 1):
            CONFIG._config_graph(
                root,
                [(env, ".env")],
                [(consumer, ".py")],
                incomplete_paths_out=incomplete,
            )
        check(incomplete == {".env", "app.py"},
              f"config global cap must propagate to consumers; got {incomplete!r}")

        normal_incomplete: set[str] = set()
        _nodes, edges = CONFIG._config_graph(
            root,
            [(env, ".env")],
            [(consumer, ".py")],
            incomplete_paths_out=normal_incomplete,
        )
        check(not normal_incomplete,
              f"normal config fixture must have no incomplete paths; got {normal_incomplete!r}")
        check(any(e.get("kind") == "reads_config" for e in edges),
              "normal config fixture must still emit its reads_config edge")

    # Routes must use the caller's guard-filtered universe. A >400 KiB file is
    # valid under the shared 1 MiB pass budget, while a generated duplicate that
    # exists on disk but was excluded by .gitattributes must never be re-walked.
    with tempfile.TemporaryDirectory(prefix="pass_diag_routes_") as root:
        backend = _write(
            root,
            "server/api.py",
            _flask("/api/items/{id}") + ("# padding\n" * 52_000),
        )
        generated = _write(
            root,
            "generated/api.py",
            _flask("/api/items/{id}"),
        )
        frontend = _write(root, "web/items.ts", "fetch('/api/items/42')\n")
        _write(root, ".gitattributes", "generated/** linguist-generated=true\n")
        check(os.path.getsize(backend) > ROUTES.MAX_FILE_BYTES,
              "route fixture must exceed the removed private 400 KiB cap")

        incomplete = set()
        _nodes, edges = ROUTES._routes_graph(
            root,
            [(backend, ".py"), (frontend, ".ts")],
            incomplete_paths_out=incomplete,
        )
        endpoints = {(e.get("src"), e.get("dst")) for e in edges}
        check(
            {
                ("server/api.py", "web/items.ts"),
                ("web/items.ts", "server/api.py"),
            } <= endpoints,
            f">400 KiB guarded route file must still couple; got {endpoints!r}",
        )
        check(not any("generated/api.py" in pair for pair in endpoints),
              f"guard-excluded generated route must not be re-walked; got {endpoints!r}")
        check(not incomplete,
              f"complete <1 MiB route fixture must have empty diagnostics; got {incomplete!r}")
        check(os.path.exists(generated), "generated exclusion fixture must exist on disk")

        oversized = _write(
            root,
            "server/oversized.py",
            _flask("/api/oversized/{id}") + ("# oversize\n" * 110_000),
        )
        oversized_front = _write(
            root,
            "web/oversized.ts",
            "fetch('/api/oversized/7')\n",
        )
        check(os.path.getsize(oversized) > IO._MAX_SCAN_BYTES,
              "oversized route fixture must exceed the shared scan cap")
        incomplete = set()
        ROUTES._routes_graph(
            root,
            [(oversized, ".py"), (oversized_front, ".ts")],
            incomplete_paths_out=incomplete,
        )
        check(incomplete == {"server/oversized.py", "web/oversized.ts"},
              f"route truncation must taint the whole route catalog; got {incomplete!r}")

    # Every route work/output ceiling must be externally visible.
    defs = {"server/api.py": {"/api/orders/{}"}}
    reqs = {"web/orders.ts": {"/api/orders/42"}}
    for cap_name in (
        "_MAX_ROUTE_PAIR_PROBES",
        "_MAX_ROUTE_CANDIDATES_PER_ROUTE",
        "_MAX_ROUTE_PAIRS",
    ):
        incomplete = set()
        with mock.patch.object(ROUTES, cap_name, 0):
            ROUTES.match_pairs(
                defs,
                reqs,
                True,
                ambiguous_out={},
                incomplete_paths_out=incomplete,
            )
        check(incomplete == {"server/api.py", "web/orders.ts"},
              f"{cap_name} must report every affected route candidate; got {incomplete!r}")

    # An OpenAPI marker can appear after the 1 MiB prefix. Truncation is a
    # definition-catalog loss even when the visible prefix looks like generic
    # YAML, and therefore propagates to every spec/code candidate.
    with tempfile.TemporaryDirectory(prefix="pass_diag_openapi_") as root:
        late_spec = _write(
            root,
            "contracts/late.yaml",
            ("# padding\n" * 112_000)
            + "openapi: 3.0.0\npaths:\n  /api/late:\n    get:\n      operationId: getLate\n",
        )
        consumer = _write(root, "server/late.py", "def getLate(): pass\n")
        incomplete = set()
        OPENAPI._openapi_graph(
            root,
            [(late_spec, ".yaml"), (consumer, ".py")],
            incomplete_paths_out=incomplete,
        )
        check(incomplete == {"contracts/late.yaml", "server/late.py"},
              f"late OpenAPI marker loss must propagate to code; got {incomplete!r}")

        json_spec = _write(
            root,
            "contracts/openapi.json",
            '{"openapi":"3.0.0","paths":{"/api/ok":{"get":{"operationId":"getOk"}}}}',
        )
        normal_consumer = _write(root, "server/ok.py", "def getOk(): pass\n")
        normal_incomplete: set[str] = set()
        nodes, edges = OPENAPI._openapi_graph(
            root,
            [(json_spec, ".json"), (normal_consumer, ".py")],
            incomplete_paths_out=normal_incomplete,
        )
        check(not normal_incomplete,
              f"normal OpenAPI fixture must have empty diagnostics; got {normal_incomplete!r}")
        check(any(n.get("kind") == "api_operation" for n in nodes)
              and any(e.get("kind") == "queries" for e in edges),
              "normal OpenAPI fixture must still emit operation/reference evidence")

    # Same routing uncertainty for Kubernetes: apiVersion/kind can occur after
    # the capped prefix. The late candidate and every possible manifest consumer
    # become incomplete; a normal parsed manifest stays clean.
    with tempfile.TemporaryDirectory(prefix="pass_diag_k8s_") as root:
        late = _write(
            root,
            "k8s/late.yaml",
            ("# padding\n" * 112_000)
            + "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: late-config\n",
        )
        deployment = _write(
            root,
            "k8s/deployment.yaml",
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n  name: app\n"
            "spec:\n"
            "  template:\n"
            "    metadata:\n      labels:\n        app: demo\n"
            "    spec:\n      containers:\n      - name: app\n        image: demo\n",
        )
        incomplete = set()
        IAC._iac_graph_k8s(
            root,
            [(late, "k8s/late.yaml"), (deployment, "k8s/deployment.yaml")],
            incomplete_paths_out=incomplete,
        )
        check(incomplete == {"k8s/late.yaml", "k8s/deployment.yaml"},
              f"late K8s marker loss must propagate to candidates; got {incomplete!r}")

        normal_incomplete: set[str] = set()
        nodes, edges = IAC._iac_graph_k8s(
            root,
            [(deployment, "k8s/deployment.yaml")],
            incomplete_paths_out=normal_incomplete,
        )
        check(not normal_incomplete,
              f"normal K8s fixture must have empty diagnostics; got {normal_incomplete!r}")
        check(any(n.get("kind") == "k8s_resource" for n in nodes)
              and any(e.get("kind") == "alters" for e in edges),
              "normal K8s fixture must still emit resource definition evidence")

    # API wrapper control: no GraphQL files means a forced GraphQL child failure
    # must not mark unrelated inputs.
    with tempfile.TemporaryDirectory(prefix="pass_diag_api_") as root:
        only_py = _write(root, "only.py", "def only(): pass\n")
        incomplete = set()
        with mock.patch.object(
            API,
            "_graphql_graph",
            side_effect=RuntimeError("forced no-schema failure"),
        ):
            API._api_contract_graph(
                root,
                [(only_py, ".py")],
                incomplete_paths_out=incomplete,
            )
        check(not incomplete,
              f"absent GraphQL substrate must not taint unrelated input; got {incomplete!r}")

    if failures:
        for failure in failures:
            print("FAIL:", failure)
        print(f"PASS DIAGNOSTICS GATE: FAIL ({len(failures)})")
        return 1
    print("PASS DIAGNOSTICS GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
