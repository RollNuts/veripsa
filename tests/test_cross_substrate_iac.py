#!/usr/bin/env python3
"""IAC-SUBSTRATE GATE — Terraform + Kubernetes cross-substrate coupling extraction.

WHAT THIS GATE PINS (hermetic synthetic fixtures; content-free: resource NAMES + file
paths + edge kinds only — no resource values, no Secret data, no DB, no network):

Terraform (primary):
  (1) TF file A defines `aws_s3_bucket.logs`, file B references it → they couple via the
      resource node with NO code edge between the files.
  (2) Precision: a `type.name` token that appears ONLY in a comment, and is NOT backed by
      a `resource "type" "name" {}` block anywhere in the repo, does NOT create a coupling.
  (3) data sources: `data "aws_route53_zone" "this"` in file A is referenced as
      `data.aws_route53_zone.this` in file B → they couple.
  (4) Self-coupling: the defining file does NOT get a `queries` edge to its own resource
      (only an `alters` edge).
  (5) Content-free: resource values and variable values never appear in the graph.

Kubernetes (secondary):
  (6) A Service's selector couples to a Deployment in the same namespace whose pod-template
      labels are a superset of the selector.
  (7) A Deployment that references a ConfigMap via envFrom.configMapRef couples to the
      ConfigMap in the same namespace.
  (8) No cross-namespace coupling: a Service in namespace A does NOT couple to a Deployment
      in namespace B even if labels match.
  (9) No coupling to an undefined resource (precision): a Deployment references a ConfigMap
      that is not defined in the repo → no coupling.

Orchestrator regression:
  (10) The build_graph result is a valid graph (has nodes + edges keys; does not raise).
  (11) build_graph on a pure-.py repo produces NO iac_resource or k8s_resource nodes (the
       IaC pass is additive, never regresses code-only repos).

Print `IAC-SUBSTRATE GATE: PASS` or `IAC-SUBSTRATE GATE: FAIL`.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import code_graph_extract as X  # noqa: E402
import _cg_iac as IAC           # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _w(d: str, rel: str, body: str) -> None:
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)


def _build(files: dict[str, str]):
    """Write files to a temp dir and call build_graph."""
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            _w(d, rel, body)
        return X.build_graph(d)


def _nodes_of_kind(g: dict, kind: str) -> dict[str, dict]:
    return {n["id"]: n for n in g["nodes"] if n.get("kind") == kind}


def _edges_of_kind(g: dict, kind: str) -> list[tuple[str, str]]:
    return [(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == kind]


def _scoped_dst_for(g: dict, name: str) -> str | None:
    """The edge `dst` for the resource whose human `name` == name. The node id / edge dst is
    NAMESPACED by module dir (Terraform) / app dir (k8s) to suppress multi-definer false
    coupling, while the node `name` keeps the bare canonical id. Returns the scoped dst (the
    node id minus the `iac_resource::`/`k8s_resource::` prefix), or None."""
    for n in g["nodes"]:
        if n.get("kind") in ("iac_resource", "k8s_resource") and n.get("name") == name:
            nid = n["id"]
            for pre in ("iac_resource::", "k8s_resource::"):
                if nid.startswith(pre):
                    return nid[len(pre):]
            return nid
    return None


def _alters_to(g: dict, scoped_dst: str) -> list[str]:
    return [src for src, dst in _edges_of_kind(g, "alters") if dst == scoped_dst]


def _queries_to(g: dict, scoped_dst: str) -> list[str]:
    return [src for src, dst in _edges_of_kind(g, "queries") if dst == scoped_dst]


def _edge_records_to(g: dict, scoped_dst: str) -> list[dict]:
    return [
        e for e in g["edges"]
        if e.get("kind") in ("alters", "queries") and e.get("dst") == scoped_dst
    ]


def _coupling_pairs(g: dict, scoped_dst: str) -> list[tuple[str, str]]:
    """Files coupled through a specific resource (by its scoped edge dst): the engine couples
    two files iff they both emit a q/a/rc edge to the SAME dst — here alters x queries."""
    alters = [
        e["src"] for e in _edge_records_to(g, scoped_dst)
        if e.get("kind") == "alters"
        and e.get("reference_status") != "ambiguous"
    ]
    queries = [
        e["src"] for e in _edge_records_to(g, scoped_dst)
        if e.get("kind") == "queries"
        and e.get("reference_status") != "ambiguous"
    ]
    return [(a, q) for a in alters for q in queries]


def _any_coupling_via_name(g: dict, name: str) -> list[tuple[str, str]]:
    """All coupling pairs through the resource whose human name == name (scoped dst resolved)."""
    sd = _scoped_dst_for(g, name)
    return _coupling_pairs(g, sd) if sd else []


def _shared_dsts(g: dict) -> set[str]:
    """Every edge dst (q/a/rc) touched by >=2 distinct files — the set of nodes through which
    the engine would couple file pairs. Used to assert NO shared node for multi-definer dups."""
    from collections import defaultdict
    touch = defaultdict(set)
    for e in g["edges"]:
        if e.get("kind") in ("queries", "alters", "reads_config"):
            if e.get("reference_status") == "ambiguous":
                continue
            touch[e["dst"]].add(e["src"])
    return {d for d, srcs in touch.items() if len(srcs) >= 2}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # -------------------------------------------------------------------------
    # (1) Terraform: file A defines a resource, file B references it → coupled
    # -------------------------------------------------------------------------
    g1 = _build({
        "infra/storage.tf": (
            'resource "aws_s3_bucket" "logs" {\n'
            '  bucket = "my-logs-bucket"\n'
            '}\n'
        ),
        "infra/app.tf": (
            'resource "aws_lambda_function" "api" {\n'
            '  s3_bucket = aws_s3_bucket.logs.bucket\n'
            '}\n'
        ),
    })
    iac_nodes1 = _nodes_of_kind(g1, "iac_resource")
    alters1 = _edges_of_kind(g1, "alters")
    queries1 = _edges_of_kind(g1, "queries")

    # The resource node must exist (node `name` keeps the bare canonical id).
    resource_id1 = "aws_s3_bucket.logs"
    check(
        any(n.get("name") == resource_id1 for n in iac_nodes1.values()),
        f"(1) TF: iac_resource node for '{resource_id1}' missing; nodes: {list(iac_nodes1)}"
    )
    # The edge dst is module-dir-namespaced (e.g. infra::aws_s3_bucket.logs).
    sd1 = _scoped_dst_for(g1, resource_id1)
    # infra/storage.tf must have an alters edge to the resource
    check(
        any("storage.tf" in src for src in _alters_to(g1, sd1)),
        f"(1) TF: alters edge from storage.tf to '{sd1}' missing; alters: {alters1}"
    )
    # infra/app.tf must have a queries edge to the resource
    check(
        any("app.tf" in src for src in _queries_to(g1, sd1)),
        f"(1) TF: queries edge from app.tf to '{sd1}' missing; queries: {queries1}"
    )
    # The two files must be coupled (one alters, one queries — crown jewel)
    pairs1 = _coupling_pairs(g1, sd1)
    check(
        len(pairs1) >= 1,
        f"(1) TF crown-jewel: expected at least 1 coupling pair for '{sd1}', got {pairs1}"
    )
    # The coupling is through the RESOURCE node, not a direct code edge (crown-jewel).
    direct_edges = [
        e for e in g1["edges"]
        if e.get("kind") in ("calls", "imports")
        and (("storage" in str(e.get("src", "")) and "app" in str(e.get("dst", "")))
             or ("app" in str(e.get("src", "")) and "storage" in str(e.get("dst", ""))))
    ]
    check(
        not direct_edges,
        f"(1) crown-jewel: unexpected direct code edge between TF files: {direct_edges}"
    )

    # -------------------------------------------------------------------------
    # (2) Precision: a token that is NOT backed by a resource block does NOT couple
    # -------------------------------------------------------------------------
    g2 = _build({
        "infra/a.tf": (
            '# aws_s3_bucket.phantom is mentioned in a comment but never defined\n'
            'resource "aws_lambda_function" "handler" {\n'
            '  description = "no bucket here"\n'
            '}\n'
        ),
        "infra/b.tf": (
            '# aws_s3_bucket.phantom mentioned in b.tf too, still not defined\n'
            'resource "aws_iam_role" "exec_role" {}\n'
        ),
    })
    iac_nodes2 = _nodes_of_kind(g2, "iac_resource")
    # phantom must NOT be in the resource nodes (never defined → no mint)
    phantom_node = any(
        "phantom" in n.get("name", "") for n in iac_nodes2.values()
    )
    check(
        not phantom_node,
        f"(2) TF precision: 'aws_s3_bucket.phantom' should NOT be minted (comment-only mention)"
        f"; nodes: {list(iac_nodes2)}"
    )

    # -------------------------------------------------------------------------
    # (3) data sources: `data "type" "name" {}` definition + reference couples files
    # -------------------------------------------------------------------------
    g3 = _build({
        "infra/dns.tf": (
            'data "aws_route53_zone" "this" {\n'
            '  name = "example.com"\n'
            '}\n'
        ),
        "infra/certs.tf": (
            'resource "aws_acm_certificate" "cert" {\n'
            '  domain_name = data.aws_route53_zone.this.name\n'
            '}\n'
        ),
    })
    iac_nodes3 = _nodes_of_kind(g3, "iac_resource")
    alters3 = _edges_of_kind(g3, "alters")
    queries3 = _edges_of_kind(g3, "queries")

    data_rid = "data.aws_route53_zone.this"
    check(
        any(n.get("name") == data_rid for n in iac_nodes3.values()),
        f"(3) TF data source: iac_resource node for '{data_rid}' missing; nodes: {list(iac_nodes3)}"
    )
    sd3 = _scoped_dst_for(g3, data_rid)
    check(
        any("dns.tf" in src for src in _alters_to(g3, sd3)),
        f"(3) TF data source: alters edge from dns.tf to '{sd3}' missing; alters: {alters3}"
    )
    check(
        any("certs.tf" in src for src in _queries_to(g3, sd3)),
        f"(3) TF data source: queries edge from certs.tf to '{sd3}' missing; queries: {queries3}"
    )
    # And they must actually COUPLE (same-module def -> reference, recall preserved).
    check(
        len(_coupling_pairs(g3, sd3)) >= 1,
        f"(3) TF data source: dns.tf and certs.tf must couple via '{sd3}'; pairs: {_coupling_pairs(g3, sd3)}"
    )

    # -------------------------------------------------------------------------
    # (4) Self-coupling: the defining file gets only `alters`, not `queries`
    # -------------------------------------------------------------------------
    g4 = _build({
        "infra/main.tf": (
            'resource "aws_vpc" "main" {}\n'
            'resource "aws_subnet" "public" {\n'
            '  vpc_id = aws_vpc.main.id\n'   # references own file's resource
            '}\n'
        ),
    })
    alters4 = _edges_of_kind(g4, "alters")
    queries4 = _edges_of_kind(g4, "queries")
    sd4 = _scoped_dst_for(g4, "aws_vpc.main")

    # alters edge for aws_vpc.main must exist from main.tf
    check(
        sd4 is not None and any("main.tf" in src for src in _alters_to(g4, sd4)),
        f"(4) self-ref: alters edge for aws_vpc.main from main.tf missing; alters: {alters4}"
    )
    # queries edge for aws_vpc.main FROM main.tf must NOT exist (it's the definer)
    check(
        sd4 is not None and not any("main.tf" in src for src in _queries_to(g4, sd4)),
        f"(4) self-ref: main.tf should NOT have queries edge to its own resource aws_vpc.main"
        f"; queries: {queries4}"
    )

    # -------------------------------------------------------------------------
    # (5) Content-free: resource VALUES and variable values are never in the graph
    # -------------------------------------------------------------------------
    g5 = _build({
        "infra/secrets.tf": (
            'resource "aws_secretsmanager_secret" "db_pass" {}\n'
            'variable "db_password" {\n'
            '  default = "super-secret-value-should-not-appear"\n'
            '}\n'
        ),
    })
    for n in g5["nodes"]:
        if n.get("kind") == "iac_resource":
            node_str = str(n)
            check(
                "super-secret-value-should-not-appear" not in node_str,
                f"(5) content-free: resource node contains a variable value: {n}"
            )
    for e in g5["edges"]:
        if e.get("src", "").endswith(".tf") or e.get("dst", "").startswith("aws_"):
            edge_str = str(e)
            check(
                "super-secret-value-should-not-appear" not in edge_str,
                f"(5) content-free: edge contains a variable value: {e}"
            )

    # -------------------------------------------------------------------------
    # (5a) MULTI-DEFINER PRECISION (audit 2026-06-20): two Terraform files in DIFFERENT
    #      module dirs each defining `resource "aws_iam_role" "this"` are duplicate
    #      definitions (the ubiquitous `aws_iam_role.this` idiom), NOT a shared resource —
    #      they MUST NOT couple (no shared node / no cross-file pair). This is the false-
    #      couple that drove 77% of terraform-aws-eks's pairs before the fix.
    # -------------------------------------------------------------------------
    g5a = _build({
        "modules/cluster/main.tf": (
            'resource "aws_iam_role" "this" {\n'
            '  name = "cluster-role"\n'
            '}\n'
        ),
        "modules/nodegroup/main.tf": (
            'resource "aws_iam_role" "this" {\n'
            '  name = "nodegroup-role"\n'
            '}\n'
        ),
    })
    # Each module defines its own `aws_iam_role.this` -> two DISTINCT dir-scoped nodes.
    iac5a = _nodes_of_kind(g5a, "iac_resource")
    role_nodes = [nid for nid in iac5a if nid.endswith("aws_iam_role.this")]
    check(
        len(role_nodes) == 2,
        f"(5a) TF multi-definer: each module dir must mint its OWN aws_iam_role.this node "
        f"(expected 2 distinct nodes); got: {role_nodes}"
    )
    # No edge dst is shared by both files -> no cross-file coupling through the duplicate.
    cluster_alters = [dst for src, dst in _edges_of_kind(g5a, "alters") if "cluster" in src]
    ng_alters = [dst for src, dst in _edges_of_kind(g5a, "alters") if "nodegroup" in src]
    check(
        not (set(cluster_alters) & set(ng_alters)),
        f"(5a) TF multi-definer: the two modules' aws_iam_role.this must NOT share an edge "
        f"dst (would falsely couple); cluster={cluster_alters} nodegroup={ng_alters}"
    )
    # Belt-and-suspenders: the engine couples via shared dst — assert NO shared dst exists
    # between the two definer files for the duplicate name.
    pairs_via_role = _any_coupling_via_name(g5a, "aws_iam_role.this")
    cross_module_pairs = [
        (a, q) for (a, q) in pairs_via_role
        if ("cluster" in a and "nodegroup" in q) or ("nodegroup" in a and "cluster" in q)
    ]
    check(
        not cross_module_pairs,
        f"(5a) TF multi-definer: cross-module duplicate definers must NOT couple; got: "
        f"{cross_module_pairs}"
    )

    # -------------------------------------------------------------------------
    # (5b) RECALL PRESERVED: two Terraform files in the SAME module dir (main.tf defines,
    #      outputs.tf references) MUST still couple — the genuine 1-definer -> referencer
    #      shared-resource pattern the crown jewel exists for.
    # -------------------------------------------------------------------------
    g5b = _build({
        "modules/vpc/main.tf": (
            'resource "aws_vpc" "this" {\n'
            '  cidr_block = "10.0.0.0/16"\n'
            '}\n'
        ),
        "modules/vpc/outputs.tf": (
            'output "vpc_id" {\n'
            '  value = aws_vpc.this.id\n'   # references the same-module resource
            '}\n'
        ),
    })
    sd5b = _scoped_dst_for(g5b, "aws_vpc.this")
    check(
        sd5b is not None and any("main.tf" in s for s in _alters_to(g5b, sd5b))
        and any("outputs.tf" in s for s in _queries_to(g5b, sd5b)),
        f"(5b) TF recall: main.tf (def) and outputs.tf (ref) in the SAME module must emit "
        f"alters+queries to the shared node '{sd5b}'; alters={_alters_to(g5b, sd5b) if sd5b else None} "
        f"queries={_queries_to(g5b, sd5b) if sd5b else None}"
    )
    check(
        len(_coupling_pairs(g5b, sd5b) if sd5b else []) >= 1,
        f"(5b) TF recall: same-module main.tf <-> outputs.tf MUST still couple; pairs: "
        f"{_coupling_pairs(g5b, sd5b) if sd5b else []}"
    )
    check(
        sd5b is not None
        and all(
            "reference_status" not in e
            for e in _edge_records_to(g5b, sd5b)
        ),
        "(5b) unambiguous Terraform edges must remain unchanged (no status marker)",
    )

    # -------------------------------------------------------------------------
    # (5c) SAME-MODULE duplicate Terraform definitions are invalid/ambiguous,
    #      but both definitions and an anchored reference remain durable evidence.
    # -------------------------------------------------------------------------
    g5c = _build({
        "modules/logs/a.tf": 'resource "aws_s3_bucket" "logs" {}\n',
        "modules/logs/b.tf": 'resource "aws_s3_bucket" "logs" {}\n',
        "modules/logs/output.tf": (
            'output "bucket_id" {\n'
            '  value = aws_s3_bucket.logs.id\n'
            '}\n'
        ),
    })
    sd5c = _scoped_dst_for(g5c, "aws_s3_bucket.logs")
    tf_node = next(
        (
            n for n in _nodes_of_kind(g5c, "iac_resource").values()
            if n.get("name") == "aws_s3_bucket.logs"
        ),
        None,
    )
    tf_evidence = _edge_records_to(g5c, sd5c) if sd5c else []
    check(
        tf_node is not None
        and (tf_node.get("provenance") or {}).get("ambiguous") is True,
        "(5c) duplicate same-module Terraform resource must remain a provenance-bearing node",
    )
    check(
        {e.get("src") for e in tf_evidence}
        == {"modules/logs/a.tf", "modules/logs/b.tf", "modules/logs/output.tf"}
        and all(e.get("reference_status") == "ambiguous" for e in tf_evidence),
        f"(5c) duplicate Terraform definitions/reference must survive inert; edges={tf_evidence}",
    )
    tf_file_statuses = {
        n.get("path"): n.get("analysis_status")
        for n in g5c["nodes"]
        if n.get("kind") == "file"
        and n.get("path") in {e.get("src") for e in tf_evidence}
    }
    check(
        set(tf_file_statuses) == {e.get("src") for e in tf_evidence}
        and set(tf_file_statuses.values()) == {"ambiguous"},
        f"(5c) ambiguous Terraform sources must be locally Unknown; statuses={tf_file_statuses}",
    )
    check(
        not (_coupling_pairs(g5c, sd5c) if sd5c else []),
        "(5c) ambiguous Terraform evidence must not enter effective adjacency",
    )

    # -------------------------------------------------------------------------
    # Kubernetes tests — only run when PyYAML is importable
    # -------------------------------------------------------------------------
    try:
        import yaml as _yaml_check  # noqa: F401
        _yaml_available = True
    except ImportError:
        _yaml_available = False

    if _yaml_available:
        # -------------------------------------------------------------------------
        # (6) K8s: Service selector couples to matching Deployment in same namespace
        # -------------------------------------------------------------------------
        g6 = _build({
            "k8s/service.yaml": (
                "apiVersion: v1\n"
                "kind: Service\n"
                "metadata:\n"
                "  name: api-svc\n"
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
                "  name: api-deploy\n"
                "  namespace: prod\n"
                "spec:\n"
                "  template:\n"
                "    metadata:\n"
                "      labels:\n"
                "        app: api\n"
                "        tier: backend\n"
                "        version: v2\n"  # superset — allowed
                "    spec:\n"
                "      containers: []\n"
            ),
        })
        k8s_nodes6 = _nodes_of_kind(g6, "k8s_resource")
        queries6 = _edges_of_kind(g6, "queries")

        check(
            any("service" in nid and "api-svc" in nid for nid in k8s_nodes6),
            f"(6) K8s: k8s_resource node for Service/api-svc missing; nodes: {list(k8s_nodes6)}"
        )
        check(
            any("deployment" in nid and "api-deploy" in nid for nid in k8s_nodes6),
            f"(6) K8s: k8s_resource node for Deployment/api-deploy missing; nodes: {list(k8s_nodes6)}"
        )
        # Service must have a queries edge to the Deployment resource
        deploy_rid = next(
            (nid.replace("k8s_resource::", "") for nid in k8s_nodes6
             if "deployment" in nid and "api-deploy" in nid), None
        )
        check(
            deploy_rid is not None,
            "(6) K8s: deployment resource id not found in nodes"
        )
        if deploy_rid:
            check(
                any(dst == deploy_rid for src, dst in queries6 if "service" in src),
                f"(6) K8s: Service→Deployment queries edge missing; queries: {queries6}"
            )

        # -------------------------------------------------------------------------
        # (7) K8s: Deployment envFrom configMapRef couples to ConfigMap in same namespace
        # -------------------------------------------------------------------------
        g7 = _build({
            "k8s/configmap.yaml": (
                "apiVersion: v1\n"
                "kind: ConfigMap\n"
                "metadata:\n"
                "  name: app-config\n"
                "  namespace: staging\n"
                "data:\n"
                "  LOG_LEVEL: info\n"
            ),
            "k8s/deploy2.yaml": (
                "apiVersion: apps/v1\n"
                "kind: Deployment\n"
                "metadata:\n"
                "  name: worker\n"
                "  namespace: staging\n"
                "spec:\n"
                "  template:\n"
                "    metadata:\n"
                "      labels:\n"
                "        app: worker\n"
                "    spec:\n"
                "      containers:\n"
                "      - name: worker\n"
                "        image: worker:latest\n"
                "        envFrom:\n"
                "        - configMapRef:\n"
                "            name: app-config\n"
            ),
        })
        k8s_nodes7 = _nodes_of_kind(g7, "k8s_resource")
        queries7 = _edges_of_kind(g7, "queries")

        cm_rid = next(
            (nid.replace("k8s_resource::", "") for nid in k8s_nodes7
             if "configmap" in nid and "app-config" in nid), None
        )
        check(
            cm_rid is not None,
            f"(7) K8s: ConfigMap node for app-config missing; nodes: {list(k8s_nodes7)}"
        )
        if cm_rid:
            check(
                any(dst == cm_rid for src, dst in queries7 if "deploy2" in src),
                f"(7) K8s: Deployment→ConfigMap queries edge missing; queries: {queries7}"
            )

        # -------------------------------------------------------------------------
        # (8) K8s: no cross-namespace coupling (namespace guard)
        # -------------------------------------------------------------------------
        g8 = _build({
            "k8s/svc-ns1.yaml": (
                "apiVersion: v1\n"
                "kind: Service\n"
                "metadata:\n"
                "  name: svc\n"
                "  namespace: ns-alpha\n"
                "spec:\n"
                "  selector:\n"
                "    app: myapp\n"
            ),
            "k8s/deploy-ns2.yaml": (
                "apiVersion: apps/v1\n"
                "kind: Deployment\n"
                "metadata:\n"
                "  name: deploy\n"
                "  namespace: ns-beta\n"   # different namespace
                "spec:\n"
                "  template:\n"
                "    metadata:\n"
                "      labels:\n"
                "        app: myapp\n"     # same label but different namespace
                "    spec:\n"
                "      containers: []\n"
            ),
        })
        k8s_nodes8 = _nodes_of_kind(g8, "k8s_resource")
        queries8 = _edges_of_kind(g8, "queries")

        # Service and Deployment exist but must NOT be coupled (different namespaces)
        deploy_rid8 = next(
            (nid.replace("k8s_resource::", "") for nid in k8s_nodes8
             if "deployment" in nid and "deploy" in nid), None
        )
        if deploy_rid8:
            check(
                not any(dst == deploy_rid8 for src, dst in queries8 if "svc-ns1" in src),
                f"(8) K8s namespace guard: cross-namespace coupling must not exist; queries: {queries8}"
            )

        # -------------------------------------------------------------------------
        # (9) K8s precision: undefined configmap not coupled
        # -------------------------------------------------------------------------
        g9 = _build({
            "k8s/deploy-ghost.yaml": (
                "apiVersion: apps/v1\n"
                "kind: Deployment\n"
                "metadata:\n"
                "  name: ghost-deploy\n"
                "  namespace: default\n"
                "spec:\n"
                "  template:\n"
                "    metadata:\n"
                "      labels:\n"
                "        app: ghost\n"
                "    spec:\n"
                "      containers:\n"
                "      - name: ghost\n"
                "        image: ghost:latest\n"
                "        envFrom:\n"
                "        - configMapRef:\n"
                "            name: ghost-config\n"   # not defined anywhere
            ),
        })
        k8s_nodes9 = _nodes_of_kind(g9, "k8s_resource")
        queries9 = _edges_of_kind(g9, "queries")

        ghost_cm = any("ghost-config" in nid for nid in k8s_nodes9)
        check(
            not ghost_cm,
            f"(9) K8s precision: ghost-config should NOT be minted (not defined in repo); nodes: {list(k8s_nodes9)}"
        )
        ghost_queries = [dst for src, dst in queries9 if "ghost-config" in dst]
        check(
            not ghost_queries,
            f"(9) K8s precision: should be no queries edge to undefined ghost-config; queries: {queries9}"
        )

        # -------------------------------------------------------------------------
        # (9a) MULTI-DEFINER PRECISION (audit 2026-06-20): two UNRELATED manifests in
        #      DIFFERENT app dirs each defining `(Service, default, redis)` are duplicate
        #      example manifests, NOT one shared resource — they MUST NOT couple. (Drove
        #      79% of kubernetes/examples's pairs before the fix — e.g. `pod default/nginx`
        #      in four unrelated example dirs all coupling.)
        # -------------------------------------------------------------------------
        _redis_svc = (
            "apiVersion: v1\n"
            "kind: Service\n"
            "metadata:\n"
            "  name: redis\n"
            "  namespace: default\n"
            "spec:\n"
            "  selector:\n"
            "    app: redis\n"
        )
        g9a = _build({
            "examples/app-a/redis-service.yaml": _redis_svc,
            "examples/app-b/redis-service.yaml": _redis_svc,
        })
        # Each app dir mints its OWN dir-scoped Service node -> two distinct nodes.
        svc_nodes9a = [nid for nid in _nodes_of_kind(g9a, "k8s_resource")
                       if nid.endswith("service::default::redis")]
        check(
            len(svc_nodes9a) == 2,
            f"(9a) K8s multi-definer: each app dir must mint its OWN Service/default/redis "
            f"node (expected 2 distinct); got: {svc_nodes9a}"
        )
        # No edge dst is shared by the two definer files -> no coupling through the duplicate.
        a_alters = [dst for src, dst in _edges_of_kind(g9a, "alters") if "app-a" in src]
        b_alters = [dst for src, dst in _edges_of_kind(g9a, "alters") if "app-b" in src]
        check(
            not (set(a_alters) & set(b_alters)),
            f"(9a) K8s multi-definer: the two app dirs' redis Service must NOT share an edge "
            f"dst (would falsely couple); app-a={a_alters} app-b={b_alters}"
        )
        check(
            not (_shared_dsts(g9a)),
            f"(9a) K8s multi-definer: no edge dst may be shared across the two unrelated "
            f"manifests; shared: {_shared_dsts(g9a)}"
        )

        # -------------------------------------------------------------------------
        # (9b) SAME-DIR DUPLICATE-DEFINER GUARD: even within ONE dir, the same
        #      (kind, ns, name) defined by >1 file is duplicate ALTERNATIVES (e.g.
        #      `aws-ebs.yaml` + `gce-pd.yaml` both defining StorageClass `slow`) — they
        #      MUST NOT couple to each other.
        # -------------------------------------------------------------------------
        _sc_slow = (
            "apiVersion: storage.k8s.io/v1\n"
            "kind: StorageClass\n"
            "metadata:\n"
            "  name: slow\n"
            "  namespace: default\n"
        )
        g9b = _build({
            "storage/aws-ebs.yaml": _sc_slow,
            "storage/gce-pd.yaml": _sc_slow,
        })
        # Same dir + same name + 2 definers: retain both facts, explicitly inert.
        sc_node = next(
            (
                n for n in _nodes_of_kind(g9b, "k8s_resource").values()
                if n.get("name") == "storageclass/default/slow"
            ),
            None,
        )
        sc_dst = (
            sc_node["id"].replace("k8s_resource::", "", 1)
            if sc_node else ""
        )
        sc_edges = _edge_records_to(g9b, sc_dst) if sc_dst else []
        check(
            sc_node is not None
            and (sc_node.get("provenance") or {}).get("ambiguous") is True,
            "(9b) duplicate StorageClass must remain a provenance-bearing node",
        )
        check(
            {e.get("src") for e in sc_edges}
            == {"storage/aws-ebs.yaml", "storage/gce-pd.yaml"}
            and all(e.get("reference_status") == "ambiguous" for e in sc_edges),
            f"(9b) K8s same-dir duplicate-definer: StorageClass slow defined by 2 files in one "
            f"dir must preserve inert alters evidence; got: {sc_edges}"
        )

        # -------------------------------------------------------------------------
        # (9c) RECALL PRESERVED: a Service selector + a Deployment with matching labels in
        #      the SAME app dir (the genuine def=1 reference pattern) MUST still couple.
        # -------------------------------------------------------------------------
        g9c = _build({
            "examples/guestbook/redis-service.yaml": (
                "apiVersion: v1\n"
                "kind: Service\n"
                "metadata:\n"
                "  name: redis-replica\n"
                "  namespace: default\n"
                "spec:\n"
                "  selector:\n"
                "    app: redis\n"
                "    role: replica\n"
            ),
            "examples/guestbook/redis-deployment.yaml": (
                "apiVersion: apps/v1\n"
                "kind: Deployment\n"
                "metadata:\n"
                "  name: redis-replica\n"
                "  namespace: default\n"
                "spec:\n"
                "  template:\n"
                "    metadata:\n"
                "      labels:\n"
                "        app: redis\n"
                "        role: replica\n"
                "        tier: backend\n"   # superset — allowed
                "    spec:\n"
                "      containers: []\n"
            ),
        })
        deploy_rid9c = next(
            (nid.replace("k8s_resource::", "") for nid in _nodes_of_kind(g9c, "k8s_resource")
             if nid.endswith("deployment::default::redis-replica")), None
        )
        check(
            deploy_rid9c is not None,
            f"(9c) K8s recall: Deployment node missing; nodes: {list(_nodes_of_kind(g9c, 'k8s_resource'))}"
        )
        if deploy_rid9c:
            svc_queries9c = [src for src, dst in _edges_of_kind(g9c, "queries")
                             if dst == deploy_rid9c and "service" in src]
            check(
                bool(svc_queries9c),
                f"(9c) K8s recall: same-app Service selector MUST still couple to the matching "
                f"Deployment; queries to {deploy_rid9c}: "
                f"{[(s, d) for s, d in _edges_of_kind(g9c, 'queries') if d == deploy_rid9c]}"
            )

        # ---------------------------------------------------------------------
        # (9d) A literal ConfigMap reference to duplicate same-scope definitions
        #      remains persisted as ambiguous evidence, not silent absence.
        # ---------------------------------------------------------------------
        _dup_config = (
            "apiVersion: v1\n"
            "kind: ConfigMap\n"
            "metadata:\n"
            "  name: runtime-config\n"
            "  namespace: prod\n"
        )
        g9d = _build({
            "apps/api/config-a.yaml": _dup_config,
            "apps/api/config-b.yaml": _dup_config,
            "apps/api/deployment.yaml": (
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
                "    spec:\n"
                "      containers:\n"
                "        - name: api\n"
                "          image: example/api\n"
                "          envFrom:\n"
                "            - configMapRef:\n"
                "                name: runtime-config\n"
            ),
        })
        config_node = next(
            (
                n for n in _nodes_of_kind(g9d, "k8s_resource").values()
                if n.get("name") == "configmap/prod/runtime-config"
            ),
            None,
        )
        config_dst = (
            config_node["id"].replace("k8s_resource::", "", 1)
            if config_node else ""
        )
        config_evidence = _edge_records_to(g9d, config_dst) if config_dst else []
        check(
            config_node is not None
            and (config_node.get("provenance") or {}).get("ambiguous") is True,
            "(9d) duplicate ConfigMap must remain a provenance-bearing node",
        )
        check(
            {e.get("src") for e in config_evidence}
            == {
                "apps/api/config-a.yaml",
                "apps/api/config-b.yaml",
                "apps/api/deployment.yaml",
            }
            and all(e.get("reference_status") == "ambiguous" for e in config_evidence),
            f"(9d) duplicate ConfigMap definitions/reference must survive inert; "
            f"edges={config_evidence}",
        )
        config_file_statuses = {
            n.get("path"): n.get("analysis_status")
            for n in g9d["nodes"]
            if n.get("kind") in {"file", "config_file"}
            and n.get("path") in {e.get("src") for e in config_evidence}
        }
        check(
            set(config_file_statuses) == {e.get("src") for e in config_evidence}
            and set(config_file_statuses.values()) == {"ambiguous"},
            f"(9d) ambiguous ConfigMap sources must be locally Unknown; "
            f"statuses={config_file_statuses}",
        )

        # (9e) Duplicate workload candidates must not be last-write-wins: a
        # selector matching either candidate retains one ambiguous query edge.
        def _deployment(label: str) -> str:
            return (
                "apiVersion: apps/v1\n"
                "kind: Deployment\n"
                "metadata:\n"
                "  name: api\n"
                "  namespace: prod\n"
                "spec:\n"
                "  template:\n"
                "    metadata:\n"
                "      labels:\n"
                f"        app: {label}\n"
                "    spec:\n"
                "      containers: []\n"
            )

        g9e = _build({
            "apps/selector/a-match.yaml": _deployment("api"),
            "apps/selector/z-no-match.yaml": _deployment("other"),
            "apps/selector/service.yaml": (
                "apiVersion: v1\n"
                "kind: Service\n"
                "metadata:\n"
                "  name: api\n"
                "  namespace: prod\n"
                "spec:\n"
                "  selector:\n"
                "    app: api\n"
            ),
        })
        workload_node = next(
            (
                n for n in _nodes_of_kind(g9e, "k8s_resource").values()
                if n.get("name") == "deployment/prod/api"
            ),
            None,
        )
        workload_dst = (
            workload_node["id"].replace("k8s_resource::", "", 1)
            if workload_node else ""
        )
        workload_evidence = _edge_records_to(g9e, workload_dst) if workload_dst else []
        check(
            {e.get("src") for e in workload_evidence}
            == {
                "apps/selector/a-match.yaml",
                "apps/selector/z-no-match.yaml",
                "apps/selector/service.yaml",
            }
            and all(e.get("reference_status") == "ambiguous" for e in workload_evidence),
            f"(9e) selector must preserve any-match duplicate workload evidence; "
            f"edges={workload_evidence}",
        )

    else:
        print("  NOTE: PyYAML not available — skipping Kubernetes tests (6)-(9)")

    # -------------------------------------------------------------------------
    # (10) Orchestrator: build_graph returns a valid graph dict (never-crash)
    # -------------------------------------------------------------------------
    g10 = _build({
        "infra/net.tf": (
            'resource "aws_vpc" "main" {\n'
            '  cidr_block = "10.0.0.0/16"\n'
            '}\n'
            'resource "aws_subnet" "private" {\n'
            '  vpc_id = aws_vpc.main.id\n'
            '  cidr_block = "10.0.1.0/24"\n'
            '}\n'
        ),
        "app/main.py": "def hello(): return 'hi'\n",
    })
    check("nodes" in g10 and "edges" in g10, "(10) build_graph: result must have 'nodes' and 'edges' keys")
    check(isinstance(g10["nodes"], list), "(10) build_graph: nodes must be a list")
    check(isinstance(g10["edges"], list), "(10) build_graph: edges must be a list")
    iac10 = _nodes_of_kind(g10, "iac_resource")
    check(
        any("aws_vpc.main" in n.get("name", "") for n in iac10.values()),
        f"(10) build_graph integration: iac_resource for aws_vpc.main missing; iac nodes: {list(iac10)}"
    )

    # -------------------------------------------------------------------------
    # (11) Additive regression: pure Python repo produces NO iac_resource / k8s_resource nodes
    # -------------------------------------------------------------------------
    g11 = _build({
        "app/views.py": "def index(): return 'ok'\n",
        "app/models.py": "class User: pass\n",
    })
    iac11 = _nodes_of_kind(g11, "iac_resource")
    k8s11 = _nodes_of_kind(g11, "k8s_resource")
    check(
        not iac11,
        f"(11) additive regression: pure-Python repo must produce NO iac_resource nodes; got: {list(iac11)}"
    )
    check(
        not k8s11,
        f"(11) additive regression: pure-Python repo must produce NO k8s_resource nodes; got: {list(k8s11)}"
    )

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        print("IAC-SUBSTRATE GATE: FAIL")
        return 1

    print("iac-substrate: TF resource definition couples to cross-file reference (crown-jewel, no code edge);")
    print("  precision: comment-only type.name tokens do NOT couple (no definition = no mint);")
    print("  data sources: data.type.name cross-file coupling works;")
    print("  self-coupling: defining file gets alters only, not queries;")
    print("  content-free: resource values never appear in the graph;")
    if _yaml_available:
        print("  K8s label-selector coupling: Service→Deployment cross-file (same namespace only);")
        print("  K8s configMapRef coupling: Deployment→ConfigMap (locally-defined only);")
        print("  K8s namespace guard: no cross-namespace coupling;")
        print("  K8s precision: undefined resources not coupled;")
    print("  orchestrator integration: build_graph produces valid graph with iac_resource nodes;")
    print("  additive: pure-Python repo gets zero iac/k8s nodes (no regression).")
    print("IAC-SUBSTRATE GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
