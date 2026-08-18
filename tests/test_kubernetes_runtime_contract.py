#!/usr/bin/env python3
"""Kubernetes runtime dependency and extraction contract.

This gate is deliberately fail-closed: Kubernetes extraction uses
``yaml.safe_load_all``, so an environment without PyYAML must fail instead of
silently skipping the Kubernetes assertions.

The fixture exercises the production entry point, ``build_graph``, and proves:

* a Service selector connects to matching workload template labels;
* a Deployment connects to a locally-defined ConfigMap;
* the same Deployment connects to a locally-defined Secret.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import code_graph_extract as X  # noqa: E402


PASS_MARKER = "KUBERNETES RUNTIME CONTRACT GATE: PASS"
FAIL_MARKER = "KUBERNETES RUNTIME CONTRACT GATE: FAIL"


def _write(root: str, rel: str, body: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def _build_fixture() -> dict:
    files = {
        "k8s/service.yaml": (
            "apiVersion: v1\n"
            "kind: Service\n"
            "metadata:\n"
            "  name: api-service\n"
            "  namespace: prod\n"
            "spec:\n"
            "  selector:\n"
            "    app: api\n"
            "    tier: backend\n"
        ),
        "k8s/deployment.yaml": (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: api\n"
            "  namespace: prod\n"
            "spec:\n"
            "  template:\n"
            "    metadata:\n"
            "      labels:\n"
            "        app: api\n"
            "        tier: backend\n"
            "        version: v2\n"
            "    spec:\n"
            "      containers:\n"
            "      - name: api\n"
            "        image: example.invalid/api:runtime-contract\n"
            "        envFrom:\n"
            "        - configMapRef:\n"
            "            name: runtime-config\n"
            "        - secretRef:\n"
            "            name: runtime-secret\n"
        ),
        "k8s/configmap.yaml": (
            "apiVersion: v1\n"
            "kind: ConfigMap\n"
            "metadata:\n"
            "  name: runtime-config\n"
            "  namespace: prod\n"
        ),
        "k8s/secret.yaml": (
            "apiVersion: v1\n"
            "kind: Secret\n"
            "metadata:\n"
            "  name: runtime-secret\n"
            "  namespace: prod\n"
        ),
    }
    with tempfile.TemporaryDirectory() as root:
        for rel, body in files.items():
            _write(root, rel, body)
        return X.build_graph(root)


def main() -> int:
    failures: list[str] = []

    try:
        import yaml
    except ImportError as exc:
        print(f"{FAIL_MARKER}: PyYAML import failed: {exc}")
        return 1

    if not callable(getattr(yaml, "safe_load_all", None)):
        print(f"{FAIL_MARKER}: imported yaml module has no callable safe_load_all")
        return 1

    graph = _build_fixture()
    k8s_nodes = {
        n.get("id")
        for n in graph.get("nodes", [])
        if n.get("kind") == "k8s_resource"
    }
    expected_nodes = {
        "k8s_resource::k8s::service::prod::api-service",
        "k8s_resource::k8s::deployment::prod::api",
        "k8s_resource::k8s::configmap::prod::runtime-config",
        "k8s_resource::k8s::secret::prod::runtime-secret",
    }
    if not expected_nodes <= k8s_nodes:
        failures.append(
            f"missing Kubernetes resource nodes: {sorted(expected_nodes - k8s_nodes)}"
        )

    edge_set = {
        (e.get("src"), e.get("dst"), e.get("kind"))
        for e in graph.get("edges", [])
    }
    expected_edges = {
        (
            "k8s/service.yaml",
            "k8s::deployment::prod::api",
            "queries",
        ),
        (
            "k8s/deployment.yaml",
            "k8s::configmap::prod::runtime-config",
            "queries",
        ),
        (
            "k8s/deployment.yaml",
            "k8s::secret::prod::runtime-secret",
            "queries",
        ),
    }
    if not expected_edges <= edge_set:
        failures.append(
            f"missing Kubernetes coupling edges: {sorted(expected_edges - edge_set)}"
        )

    if failures:
        print(FAIL_MARKER)
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(
        "PyYAML "
        f"{getattr(yaml, '__version__', 'unknown')}: Service selector, ConfigMap, and Secret "
        "couplings verified through build_graph"
    )
    print(PASS_MARKER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
