#!/usr/bin/env python3
"""Authoritative, content-free schema contract for Veripsa code graphs.

The extractor, persistence wall, and effective-adjacency query must agree on
kind names.  This module intentionally has no dependency on the extractor or
database packages so it can be imported by extraction, ingest, migrations,
tests, and offline audit tooling without creating an import cycle.

The contract distinguishes three questions:

* what the extractor may emit;
* what persistence is required to retain; and
* what effective adjacency consumes.

Persistence is intentionally lossless: every extractor kind is a persisted
kind.  Effective adjacency intentionally remains narrower: ``contains`` is
structural ownership evidence, while ``alters_col``/``queries_col`` are
persisted column evidence that existing review behavior does not yet traverse.
Any implementation that accepts a smaller persistence set is schema drift, not
a silent compatibility mode.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Mapping, Sequence
import unicodedata


SCHEMA_CONTRACT_VERSION = 2
EXTRACTOR_VERSION = "cg4"
SEMANTIC_REF_VERSION = 1

# Tuple order is presentation order only.  Membership is always checked through
# the corresponding frozenset.
EXTRACTOR_NODE_KIND_ORDER = (
    "file",
    "def",
    "class",
    "table",
    "column",
    "config_file",
    "config_key",
    "iac_resource",
    "k8s_resource",
    "api_type",
    "api_message",
    "api_service",
    "api_operation",
    "api_schema",
    "ci_script",
    "app_command",
    "job_task",
    "job_queue",
    "sibling_stem",
    "role_feature",
)
EXTRACTOR_EDGE_KIND_ORDER = (
    "contains",
    "calls",
    "imports",
    "alters",
    "queries",
    "reads_config",
    "alters_col",
    "queries_col",
)

EXTRACTOR_NODE_KINDS = frozenset(EXTRACTOR_NODE_KIND_ORDER)
EXTRACTOR_EDGE_KINDS = frozenset(EXTRACTOR_EDGE_KIND_ORDER)

# There is no intentional persistence exclusion.  Keeping separate names makes
# policy explicit and lets a future contract change show up as a reviewable
# diff rather than an implicit set subtraction in an ingest function.
PERSISTED_NODE_KINDS = EXTRACTOR_NODE_KINDS
PERSISTED_EDGE_KINDS = EXTRACTOR_EDGE_KINDS

DOCUMENT_NODE_KINDS = frozenset({"file", "config_file"})
NODE_ANALYSIS_STATUSES = frozenset({"failed", "ambiguous", "incomplete"})
EDGE_REFERENCE_STATUSES = frozenset({"ambiguous", "unresolved"})
CODE_SYMBOL_NODE_KINDS = frozenset({"def", "class"})
RESOURCE_NODE_KINDS = frozenset(
    {
        "table",
        "column",
        "config_key",
        "iac_resource",
        "k8s_resource",
        "api_type",
        "api_message",
        "api_service",
        "api_operation",
        "api_schema",
        "ci_script",
        "app_command",
        "job_task",
        "job_queue",
        "sibling_stem",
        "role_feature",
    }
)
# These kinds normally carry at least one alters/alters_col edge. A retained
# node without one is an explicit uncertainty signal (multi-definer
# suppression or an unresolved reference-conditioned contract).
RESOURCE_DEFINITION_EVIDENCE_KINDS = frozenset(
    RESOURCE_NODE_KINDS - {"config_key", "sibling_stem", "role_feature"}
)

# This is the actual core._claim_adjacency surface.  File nodes anchor direct
# import/resource traversal, while def/class nodes resolve call targets.
# Resource nodes themselves are persisted catalog evidence; adjacency currently
# joins their canonical keys through edges rather than traversing those rows.
EFFECTIVE_ADJACENCY_NODE_KINDS = frozenset({"file", "def", "class"})
EFFECTIVE_ADJACENCY_EDGE_KINDS = frozenset(
    {
        "calls",
        "imports",
        "alters",
        "queries",
        "reads_config",
    }
)
STRUCTURAL_ONLY_EDGE_KINDS = frozenset({"contains"})
EVIDENCE_ONLY_EDGE_KINDS = frozenset({"alters_col", "queries_col"})
NON_ADJACENCY_EDGE_KINDS = STRUCTURAL_ONLY_EDGE_KINDS | EVIDENCE_ONLY_EDGE_KINDS
NON_ADJACENCY_REASONS = {
    "contains": (
        "Symbol ownership is represented by def/class nodes and is used to "
        "resolve calls; contains is not itself a file-to-file traversal."
    ),
    "alters_col": (
        "Persisted column-change evidence; excluded from effective adjacency "
        "until a separately reviewed behavior change consumes column evidence."
    ),
    "queries_col": (
        "Persisted column-query evidence; excluded from effective adjacency "
        "until a separately reviewed behavior change consumes column evidence."
    ),
}

RESOURCE_METADATA_FIELDS = frozenset(
    {"kind", "canonical_key", "repo", "extractor", "confidence", "provenance"}
)
# Location is an explicit one-of requirement, not two independently required
# fields: a resource must carry at least one of path or scope.
RESOURCE_METADATA_PATH_OR_SCOPE_FIELDS = frozenset({"path", "scope"})
NODE_ID_MAX_LENGTH = 1600
NODE_PATH_MAX_LENGTH = 1024
NODE_NAME_MAX_LENGTH = 512
EDGE_SRC_MAX_LENGTH = 1024
EDGE_DST_MAX_LENGTH = 1600
REPO_MAX_LENGTH = 512
RESOURCE_CANONICAL_KEY_MAX_LENGTH = 1600
RESOURCE_SCOPE_MAX_LENGTH = 1024
RESOURCE_EXTRACTOR_MAX_LENGTH = 128
RESOURCE_PROVENANCE_MAX_BYTES = 8192
VOLATILE_GRAPH_FIELDS = frozenset(
    {
        "db_id",
        "row_id",
        "created_at",
        "updated_at",
        "ingested_at",
        "captured_at",
        "timestamp",
    }
)


@dataclass(frozen=True)
class SubstrateContract:
    """Kinds and provenance owned by one extraction substrate."""

    node_kinds: frozenset[str]
    edge_kinds: frozenset[str]
    extractor: str
    description: str


SUBSTRATE_CONTRACTS: dict[str, SubstrateContract] = {
    "code_structure": SubstrateContract(
        frozenset({"file", "def", "class"}),
        frozenset({"contains"}),
        "code_graph_extract",
        "Files, definitions, classes, and symbol ownership.",
    ),
    "imports": SubstrateContract(
        frozenset({"file"}),
        frozenset({"imports"}),
        "_cg_resolve",
        "Language imports resolved to repository paths when possible.",
    ),
    "calls": SubstrateContract(
        frozenset({"file", "def", "class"}),
        frozenset({"calls"}),
        "language extractors",
        "Bare call references resolved against persisted definitions.",
    ),
    "database": SubstrateContract(
        frozenset({"table", "column"}),
        frozenset({"alters", "queries", "alters_col", "queries_col"}),
        "_cg_schema",
        "Database table and column definitions and references.",
    ),
    "config": SubstrateContract(
        frozenset({"config_file", "config_key"}),
        frozenset({"reads_config"}),
        "_cg_config",
        "Configuration declarations and key reads.",
    ),
    "terraform": SubstrateContract(
        frozenset({"iac_resource"}),
        frozenset({"alters", "queries"}),
        "_cg_iac:terraform",
        "Terraform resources, modules, and output references.",
    ),
    "kubernetes": SubstrateContract(
        frozenset({"k8s_resource"}),
        frozenset({"alters", "queries"}),
        "_cg_iac:kubernetes",
        "Kubernetes resources, selectors, labels, and named references.",
    ),
    "graphql": SubstrateContract(
        frozenset({"api_type"}),
        frozenset({"alters", "queries"}),
        "_cg_api_contract:graphql",
        "GraphQL type definitions, extensions, and anchored references.",
    ),
    "protobuf": SubstrateContract(
        frozenset({"api_message", "api_service"}),
        frozenset({"alters", "queries"}),
        "_cg_api_contract:protobuf",
        "Protocol Buffer messages/services and anchored references.",
    ),
    "openapi": SubstrateContract(
        frozenset({"api_operation", "api_schema"}),
        frozenset({"alters", "queries"}),
        "_cg_openapi",
        "OpenAPI operations/schemas and code references.",
    ),
    "routes": SubstrateContract(
        frozenset(),
        frozenset({"imports"}),
        "_cg_routes",
        "Cross-tier route coupling encoded as resolved imports.",
    ),
    "ci_script": SubstrateContract(
        frozenset({"ci_script"}),
        frozenset({"alters", "queries"}),
        "_cg_ci",
        "Package scripts referenced by GitHub Actions.",
    ),
    "tauri": SubstrateContract(
        frozenset({"app_command"}),
        frozenset({"alters", "queries"}),
        "_cg_tauri",
        "Tauri command definitions and invoke references.",
    ),
    "celery": SubstrateContract(
        frozenset({"job_task"}),
        frozenset({"alters", "queries"}),
        "_cg_jobs:celery",
        "Celery task definitions and send_task references.",
    ),
    "bullmq": SubstrateContract(
        frozenset({"job_queue"}),
        frozenset({"alters", "queries"}),
        "_cg_jobs:bullmq",
        "BullMQ workers and queues.",
    ),
    "sibling_stem": SubstrateContract(
        frozenset({"sibling_stem"}),
        frozenset({"queries"}),
        "_cg_sibling",
        "Cross-directory files sharing a specific sibling stem.",
    ),
    "role_feature": SubstrateContract(
        frozenset({"role_feature"}),
        frozenset({"queries"}),
        "_cg_role_feature",
        "Complementary role files sharing a feature token.",
    ),
}

RESOURCE_KIND_TO_SUBSTRATE = {
    "table": "database",
    "column": "database",
    "config_key": "config",
    "iac_resource": "terraform",
    "k8s_resource": "kubernetes",
    "api_type": "graphql",
    "api_message": "protobuf",
    "api_service": "protobuf",
    "api_operation": "openapi",
    "api_schema": "openapi",
    "ci_script": "ci_script",
    "app_command": "tauri",
    "job_task": "celery",
    "job_queue": "bullmq",
    "sibling_stem": "sibling_stem",
    "role_feature": "role_feature",
}
DEFAULT_EXTRACTOR_BY_RESOURCE_KIND = {
    kind: SUBSTRATE_CONTRACTS[substrate].extractor
    for kind, substrate in RESOURCE_KIND_TO_SUBSTRATE.items()
}


class GraphContractError(ValueError):
    """Raised when a graph or metadata record violates this contract."""


class SchemaInventoryParseError(GraphContractError):
    """Raised when SQL acceptance kinds cannot be determined exactly."""


class SchemaDriftError(GraphContractError):
    """Raised when SQL persistence differs from the declared contract."""


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    location: str
    message: str

    def __str__(self) -> str:
        return f"{self.location}: {self.code}: {self.message}"


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def canonical_key_for_resource(node: Mapping[str, Any]) -> str:
    """Return the key used by coupling edges for a resource node.

    Existing extractors use three historical ID shapes.  This helper presents
    one explicit key to persistence/linking without changing the extractor's
    logical node ID.
    """

    kind = node.get("kind")
    if kind not in RESOURCE_NODE_KINDS:
        raise GraphContractError(f"not a resource node kind: {kind!r}")
    explicit = node.get("canonical_key")
    if _nonempty_string(explicit):
        return unicodedata.normalize("NFC", explicit.strip())

    node_id = node.get("id")
    name = node.get("name")
    if kind == "table" and _nonempty_string(name):
        key = name
    elif kind == "column":
        table = node.get("table")
        if _nonempty_string(table) and _nonempty_string(name):
            key = f"{table}.{name}"
        elif _nonempty_string(node_id) and node_id.startswith("column::"):
            key = node_id[len("column::") :]
        else:
            key = ""
    elif kind == "config_key" and _nonempty_string(name):
        key = name
    elif kind in {"iac_resource", "k8s_resource"} and _nonempty_string(node_id):
        prefix = f"{kind}::"
        key = node_id[len(prefix) :] if node_id.startswith(prefix) else node_id
    elif _nonempty_string(node_id):
        # API/CI/Tauri/job/sibling/role edges use the complete contract ID.
        key = node_id
    elif _nonempty_string(name):
        key = name
    else:
        key = ""

    if not _nonempty_string(key):
        raise GraphContractError(
            f"cannot derive canonical key for {kind!r}; provide canonical_key"
        )
    return unicodedata.normalize("NFC", str(key).strip())


def enrich_resource_node(
    node: Mapping[str, Any],
    *,
    repo: str,
    extractor: str | None = None,
    confidence: float = 1.0,
    provenance: Mapping[str, Any] | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """Return a copy with the required first-class resource metadata.

    ``provenance`` must remain content-free.  The default records only the
    extractor, logical node ID, and path.
    """

    kind = node.get("kind")
    if kind not in RESOURCE_NODE_KINDS:
        raise GraphContractError(f"cannot enrich non-resource kind {kind!r}")
    if not _nonempty_string(repo):
        raise GraphContractError("resource repo must be a non-empty string")
    if len(repo.strip()) > REPO_MAX_LENGTH:
        raise GraphContractError(f"resource repo exceeds {REPO_MAX_LENGTH} characters")
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError) as exc:
        raise GraphContractError("confidence must be numeric") from exc
    if not math.isfinite(confidence_value) or not 0.0 <= confidence_value <= 1.0:
        raise GraphContractError("confidence must be finite and between 0 and 1")

    path = node.get("path")
    resolved_scope = scope if scope is not None else node.get("scope")
    if not _nonempty_string(resolved_scope) and _nonempty_string(path):
        parent = str(PurePosixPath(str(path).replace("\\", "/")).parent)
        resolved_scope = parent if parent not in {"", "."} else "."
    if not _nonempty_string(path) and not _nonempty_string(resolved_scope):
        raise GraphContractError("resource must have path or scope")
    if _nonempty_string(resolved_scope) and len(str(resolved_scope).strip()) > RESOURCE_SCOPE_MAX_LENGTH:
        raise GraphContractError(
            f"resource scope exceeds {RESOURCE_SCOPE_MAX_LENGTH} characters"
        )

    resolved_extractor = (
        extractor
        or node.get("extractor")
        or DEFAULT_EXTRACTOR_BY_RESOURCE_KIND[kind]
    )
    if not _nonempty_string(resolved_extractor):
        raise GraphContractError("resource extractor must be a non-empty string")
    if len(str(resolved_extractor).strip()) > RESOURCE_EXTRACTOR_MAX_LENGTH:
        raise GraphContractError(
            f"resource extractor exceeds {RESOURCE_EXTRACTOR_MAX_LENGTH} characters"
        )

    if provenance is None:
        resolved_provenance: Mapping[str, Any] = {
            "extractor": resolved_extractor,
            "node_id": node.get("id"),
            "path": path,
        }
        if node.get("ambiguous") is True:
            resolved_provenance = {
                **resolved_provenance,
                "ambiguous": True,
            }
    elif not isinstance(provenance, Mapping):
        raise GraphContractError("resource provenance must be an object")
    else:
        resolved_provenance = provenance

    canonical_key = canonical_key_for_resource(node)
    if len(canonical_key) > RESOURCE_CANONICAL_KEY_MAX_LENGTH:
        raise GraphContractError(
            "resource canonical_key exceeds "
            f"{RESOURCE_CANONICAL_KEY_MAX_LENGTH} characters"
        )
    try:
        provenance_bytes = len(
            _stable_json(_normalize_json_value(dict(resolved_provenance))).encode("utf-8")
        )
    except (TypeError, GraphContractError) as exc:
        raise GraphContractError("resource provenance must be JSON-compatible") from exc
    if provenance_bytes > RESOURCE_PROVENANCE_MAX_BYTES:
        raise GraphContractError(
            f"resource provenance exceeds {RESOURCE_PROVENANCE_MAX_BYTES} bytes"
        )

    enriched = dict(node)
    enriched.update(
        {
            "canonical_key": canonical_key,
            "repo": unicodedata.normalize("NFC", repo.strip()),
            "scope": unicodedata.normalize("NFC", str(resolved_scope).strip()),
            "extractor": str(resolved_extractor).strip(),
            "confidence": confidence_value,
            "provenance": dict(resolved_provenance),
        }
    )
    return enriched


def enrich_resource_nodes(
    graph: Mapping[str, Any],
    *,
    repo: str,
    confidence: float = 1.0,
) -> dict[str, Any]:
    """Return a graph copy with every resource node metadata-enriched."""

    result = dict(graph)
    result["nodes"] = [
        enrich_resource_node(node, repo=repo, confidence=confidence)
        if isinstance(node, Mapping) and node.get("kind") in RESOURCE_NODE_KINDS
        else dict(node)
        for node in graph.get("nodes", ())
    ]
    result["edges"] = [dict(edge) for edge in graph.get("edges", ())]
    return result


def validate_graph(
    graph: Mapping[str, Any],
    *,
    persisted: bool = False,
    require_resource_metadata: bool = False,
) -> tuple[ValidationIssue, ...]:
    """Validate graph shape, kinds, identities, and optional resource metadata."""

    issues: list[ValidationIssue] = []
    if not isinstance(graph, Mapping):
        return (
            ValidationIssue("graph_type", "$", "graph must be an object"),
        )

    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        issues.append(ValidationIssue("nodes_type", "$.nodes", "nodes must be an array"))
        nodes = ()
    if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes)):
        issues.append(ValidationIssue("edges_type", "$.edges", "edges must be an array"))
        edges = ()

    allowed_nodes = PERSISTED_NODE_KINDS if persisted else EXTRACTOR_NODE_KINDS
    allowed_edges = PERSISTED_EDGE_KINDS if persisted else EXTRACTOR_EDGE_KINDS
    # A generated Resource id is not a globally reserved namespace: any such
    # string can also be a legal repository path.  Persisted Node identity is
    # therefore the complete (id, kind, path) tuple.  Requiring id alone to be
    # unique silently drops/rejects one of two distinct facts such as a file
    # named ``cfgkey::settings.json::veripsa.webhook.py`` and the config_key
    # whose generated id has that same spelling.
    seen_node_identities: set[tuple[str, str, str]] = set()
    for index, node in enumerate(nodes):
        location = f"$.nodes[{index}]"
        if not isinstance(node, Mapping):
            issues.append(ValidationIssue("node_type", location, "node must be an object"))
            continue
        kind = node.get("kind")
        if kind not in allowed_nodes:
            issues.append(
                ValidationIssue("node_kind", f"{location}.kind", f"undeclared kind {kind!r}")
            )
        node_id = node.get("id")
        if not _nonempty_string(node_id):
            issues.append(ValidationIssue("node_id", f"{location}.id", "id is required"))
        elif len(node_id) > NODE_ID_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    "node_id_length",
                    f"{location}.id",
                    f"id exceeds {NODE_ID_MAX_LENGTH} characters",
                )
            )
        node_path = node.get("path")
        if not _nonempty_string(node_path):
            issues.append(ValidationIssue("node_path", f"{location}.path", "path is required"))
        elif len(node_path) > NODE_PATH_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    "node_path_length",
                    f"{location}.path",
                    f"path exceeds {NODE_PATH_MAX_LENGTH} characters",
                )
            )
        if (
            _nonempty_string(node_id)
            and isinstance(kind, str)
            and _nonempty_string(node_path)
        ):
            identity = (node_id, kind, node_path)
            if identity in seen_node_identities:
                issues.append(
                    ValidationIssue(
                        "duplicate_node",
                        location,
                        f"duplicate identity {identity!r}",
                    )
                )
            else:
                seen_node_identities.add(identity)
        node_name = node.get("name")
        if node_name is not None and (
            not isinstance(node_name, str) or len(node_name) > NODE_NAME_MAX_LENGTH
        ):
            issues.append(
                ValidationIssue(
                    "node_name_length",
                    f"{location}.name",
                    f"name must be a string of at most {NODE_NAME_MAX_LENGTH} characters",
                )
            )

        analysis_status = node.get("analysis_status")
        if analysis_status is not None:
            if analysis_status not in NODE_ANALYSIS_STATUSES:
                issues.append(
                    ValidationIssue(
                        "node_analysis_status",
                        f"{location}.analysis_status",
                        "analysis_status must be null, 'failed', 'ambiguous', "
                        "or 'incomplete'",
                    )
                )
            elif kind not in DOCUMENT_NODE_KINDS:
                issues.append(
                    ValidationIssue(
                        "node_analysis_status_kind",
                        f"{location}.analysis_status",
                        "analysis_status is valid only on file/config_file nodes",
                    )
                )

        if require_resource_metadata and kind in RESOURCE_NODE_KINDS:
            for field_name in RESOURCE_METADATA_FIELDS:
                if field_name not in node:
                    issues.append(
                        ValidationIssue(
                            "resource_metadata",
                            f"{location}.{field_name}",
                            "required resource metadata is missing",
                        )
                    )
            if not (_nonempty_string(node.get("path")) or _nonempty_string(node.get("scope"))):
                issues.append(
                    ValidationIssue(
                        "resource_scope",
                        location,
                        "resource requires path or scope",
                    )
                )
            if not _nonempty_string(node.get("canonical_key")):
                issues.append(
                    ValidationIssue(
                        "resource_canonical_key",
                        f"{location}.canonical_key",
                        "canonical_key must be non-empty",
                    )
                )
            elif len(node["canonical_key"]) > RESOURCE_CANONICAL_KEY_MAX_LENGTH:
                issues.append(
                    ValidationIssue(
                        "resource_canonical_key_length",
                        f"{location}.canonical_key",
                        "canonical_key exceeds "
                        f"{RESOURCE_CANONICAL_KEY_MAX_LENGTH} characters",
                    )
                )
            if not _nonempty_string(node.get("repo")):
                issues.append(
                    ValidationIssue(
                        "resource_repo", f"{location}.repo", "repo must be non-empty"
                    )
                )
            elif len(node["repo"]) > REPO_MAX_LENGTH:
                issues.append(
                    ValidationIssue(
                        "resource_repo_length",
                        f"{location}.repo",
                        f"repo exceeds {REPO_MAX_LENGTH} characters",
                    )
                )
            if not _nonempty_string(node.get("extractor")):
                issues.append(
                    ValidationIssue(
                        "resource_extractor",
                        f"{location}.extractor",
                        "extractor must be non-empty",
                    )
                )
            elif len(node["extractor"]) > RESOURCE_EXTRACTOR_MAX_LENGTH:
                issues.append(
                    ValidationIssue(
                        "resource_extractor_length",
                        f"{location}.extractor",
                        f"extractor exceeds {RESOURCE_EXTRACTOR_MAX_LENGTH} characters",
                    )
                )
            resource_scope = node.get("scope")
            if _nonempty_string(resource_scope) and len(resource_scope) > RESOURCE_SCOPE_MAX_LENGTH:
                issues.append(
                    ValidationIssue(
                        "resource_scope_length",
                        f"{location}.scope",
                        f"scope exceeds {RESOURCE_SCOPE_MAX_LENGTH} characters",
                    )
                )
            confidence = node.get("confidence")
            if (
                not isinstance(confidence, (int, float))
                or isinstance(confidence, bool)
                or not math.isfinite(float(confidence))
                or not 0.0 <= float(confidence) <= 1.0
            ):
                issues.append(
                    ValidationIssue(
                        "resource_confidence",
                        f"{location}.confidence",
                        "confidence must be finite and between 0 and 1",
                    )
                )
            if not isinstance(node.get("provenance"), Mapping):
                issues.append(
                    ValidationIssue(
                        "resource_provenance",
                        f"{location}.provenance",
                        "provenance must be an object",
                    )
                )
            else:
                try:
                    provenance_size = len(
                        _stable_json(
                            _normalize_json_value(dict(node["provenance"]))
                        ).encode("utf-8")
                    )
                except (TypeError, GraphContractError):
                    provenance_size = RESOURCE_PROVENANCE_MAX_BYTES + 1
                if provenance_size > RESOURCE_PROVENANCE_MAX_BYTES:
                    issues.append(
                        ValidationIssue(
                            "resource_provenance_size",
                            f"{location}.provenance",
                            "provenance must be JSON-compatible and at most "
                            f"{RESOURCE_PROVENANCE_MAX_BYTES} bytes",
                        )
                    )

    seen_edges: set[tuple[str, str, str]] = set()
    for index, edge in enumerate(edges):
        location = f"$.edges[{index}]"
        if not isinstance(edge, Mapping):
            issues.append(ValidationIssue("edge_type", location, "edge must be an object"))
            continue
        kind = edge.get("kind")
        if kind not in allowed_edges:
            issues.append(
                ValidationIssue("edge_kind", f"{location}.kind", f"undeclared kind {kind!r}")
            )
        src, dst = edge.get("src"), edge.get("dst")
        if not _nonempty_string(src):
            issues.append(ValidationIssue("edge_src", f"{location}.src", "src is required"))
        elif len(src) > EDGE_SRC_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    "edge_src_length",
                    f"{location}.src",
                    f"src exceeds {EDGE_SRC_MAX_LENGTH} characters",
                )
            )
        if not _nonempty_string(dst):
            issues.append(ValidationIssue("edge_dst", f"{location}.dst", "dst is required"))
        elif len(dst) > EDGE_DST_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    "edge_dst_length",
                    f"{location}.dst",
                    f"dst exceeds {EDGE_DST_MAX_LENGTH} characters",
                )
            )
        if _nonempty_string(src) and _nonempty_string(dst) and isinstance(kind, str):
            identity = (src, dst, kind)
            if identity in seen_edges:
                issues.append(
                    ValidationIssue(
                        "duplicate_edge", location, f"duplicate edge {identity!r}"
                    )
                )
            else:
                seen_edges.add(identity)
        reference_status = edge.get("reference_status")
        if (
            reference_status is not None
            and reference_status not in EDGE_REFERENCE_STATUSES
        ):
            issues.append(
                ValidationIssue(
                    "edge_reference_status",
                    f"{location}.reference_status",
                    "reference_status must be null, 'ambiguous', or 'unresolved'",
                )
            )
    return tuple(issues)


def assert_valid_graph(
    graph: Mapping[str, Any],
    *,
    persisted: bool = False,
    require_resource_metadata: bool = False,
) -> None:
    issues = validate_graph(
        graph,
        persisted=persisted,
        require_resource_metadata=require_resource_metadata,
    )
    if issues:
        detail = "\n".join(f"- {issue}" for issue in issues)
        raise GraphContractError(f"graph schema validation failed:\n{detail}")


def _normalize_json_value(value: Any, *, field_name: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_json_value(item, field_name=str(key))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in VOLATILE_GRAPH_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize_json_value(item) for item in value]
        return sorted(normalized, key=lambda item: _stable_json(item))
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        if field_name in {"path", "src"}:
            normalized = normalized.replace("\\", "/")
        return normalized
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GraphContractError("non-finite numbers cannot be canonicalized")
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    raise GraphContractError(f"non-JSON graph value cannot be canonicalized: {type(value).__name__}")


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class CanonicalGraphSets:
    """Order-independent serialized node and edge sets."""

    nodes: frozenset[str]
    edges: frozenset[str]

    def as_dict(self) -> dict[str, list[str]]:
        return {"nodes": sorted(self.nodes), "edges": sorted(self.edges)}


@dataclass(frozen=True)
class CanonicalGraphDiff:
    nodes_only_left: frozenset[str]
    nodes_only_right: frozenset[str]
    edges_only_left: frozenset[str]
    edges_only_right: frozenset[str]

    @property
    def equivalent(self) -> bool:
        return not (
            self.nodes_only_left
            or self.nodes_only_right
            or self.edges_only_left
            or self.edges_only_right
        )


def canonical_normalized_sets(graph: Mapping[str, Any]) -> CanonicalGraphSets:
    """Normalize semantic records while ignoring order and volatile DB fields."""

    nodes = graph.get("nodes", ())
    edges = graph.get("edges", ())
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        raise GraphContractError("nodes must be an array")
    if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes)):
        raise GraphContractError("edges must be an array")
    return CanonicalGraphSets(
        nodes=frozenset(_stable_json(_normalize_json_value(node)) for node in nodes),
        edges=frozenset(_stable_json(_normalize_json_value(edge)) for edge in edges),
    )


def canonical_graph_diff(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> CanonicalGraphDiff:
    left_sets = canonical_normalized_sets(left)
    right_sets = canonical_normalized_sets(right)
    return CanonicalGraphDiff(
        nodes_only_left=left_sets.nodes - right_sets.nodes,
        nodes_only_right=right_sets.nodes - left_sets.nodes,
        edges_only_left=left_sets.edges - right_sets.edges,
        edges_only_right=right_sets.edges - left_sets.edges,
    )


def canonical_graph_hash(graph: Mapping[str, Any]) -> str:
    """Return a SHA-256 over normalized node/edge sets and contract version."""

    sets = canonical_normalized_sets(graph)
    payload = {
        "contract_version": SCHEMA_CONTRACT_VERSION,
        "nodes": sorted(sets.nodes),
        "edges": sorted(sets.edges),
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _edge_source_path(src: str) -> str:
    """Return the exact document path carried by an Edge source.

    Every current extractor emits ``edge.src`` as a file/config-file path.
    Git permits ``::`` inside that path, so it is kept verbatim and never
    delimiter-guessed against generated Node ids.
    """

    return src


def slice_graph_for_touched_paths(
    graph: Mapping[str, Any],
    touched_paths: Iterable[str],
    *,
    removed_paths: Iterable[str] = (),
    include_incoming_to_touched: bool = False,
) -> dict[str, Any]:
    """Return the path-owned slice used by an incremental persistence patch.

    This is a mechanical slice, not an equivalence proof.  Repo-level linkers
    must still rebuild/relink known-resource facts before the slice is safe to
    persist.
    """

    touched = {
        unicodedata.normalize("NFC", str(path).replace("\\", "/"))
        for path in touched_paths
        if _nonempty_string(path)
    }
    removed = {
        unicodedata.normalize("NFC", str(path).replace("\\", "/"))
        for path in removed_paths
        if _nonempty_string(path)
    }
    nodes = [
        dict(node)
        for node in graph.get("nodes", ())
        if isinstance(node, Mapping)
        and unicodedata.normalize(
            "NFC", str(node.get("path", "")).replace("\\", "/")
        )
        in touched
    ]
    edges: list[dict[str, Any]] = []
    for edge in graph.get("edges", ()):
        if not isinstance(edge, Mapping):
            continue
        src = unicodedata.normalize("NFC", str(edge.get("src", "")).replace("\\", "/"))
        dst = unicodedata.normalize("NFC", str(edge.get("dst", "")).replace("\\", "/"))
        src_touched = _edge_source_path(src) in touched
        removed_import = edge.get("kind") == "imports" and dst in removed
        incoming_touched = include_incoming_to_touched and dst in touched
        if src_touched or removed_import or incoming_touched:
            edges.append(dict(edge))
    result = {
        key: value
        for key, value in graph.items()
        if key not in {"nodes", "edges", "root"}
    }
    result.update({"nodes": nodes, "edges": edges})
    return result


def _resource_key_catalog(
    nodes: Iterable[Mapping[str, Any]],
) -> dict[str, set[str]]:
    catalog: dict[str, set[str]] = defaultdict(set)
    for node in nodes:
        kind = node.get("kind")
        if kind not in RESOURCE_NODE_KINDS:
            continue
        try:
            key = canonical_key_for_resource(node)
        except GraphContractError:
            continue
        catalog[key].add(RESOURCE_KIND_TO_SUBSTRATE[kind])
    return catalog


def classify_node_substrate(node: Mapping[str, Any]) -> str:
    explicit = node.get("substrate")
    if explicit in SUBSTRATE_CONTRACTS:
        return str(explicit)
    kind = node.get("kind")
    if kind in RESOURCE_KIND_TO_SUBSTRATE:
        return RESOURCE_KIND_TO_SUBSTRATE[kind]
    if kind == "config_file":
        return "config"
    return "code_structure"


def classify_edge_substrate(
    edge: Mapping[str, Any], resource_catalog: Mapping[str, set[str]]
) -> str:
    explicit = edge.get("substrate")
    if explicit in SUBSTRATE_CONTRACTS:
        return str(explicit)
    kind = edge.get("kind")
    if kind == "contains":
        return "code_structure"
    if kind == "calls":
        return "calls"
    if kind == "imports":
        # Route edges are intentionally encoded as imports.  Exact route metrics
        # require the producer to stamp substrate="routes".
        return "imports"
    if kind == "reads_config":
        return "config"
    if kind in {"alters_col", "queries_col"}:
        return "database"
    candidates = resource_catalog.get(str(edge.get("dst")), set())
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        return "ambiguous"
    return "unresolved"


def _observation_count(value: int | Iterable[Any], name: str) -> int:
    if isinstance(value, bool):
        raise GraphContractError(f"{name} count must be a non-negative integer")
    if isinstance(value, int):
        if value < 0:
            raise GraphContractError(f"{name} count must be non-negative")
        return value
    return sum(1 for _item in value)


@dataclass(frozen=True)
class GraphMetrics:
    input_file_count: int
    node_kind_counts: Mapping[str, int]
    edge_kind_counts: Mapping[str, int]
    nodes_by_substrate: Mapping[str, int]
    edges_by_substrate: Mapping[str, int]
    persistence_excluded_nodes: int
    persistence_excluded_edges: int
    persistence_exclusion_reasons: Mapping[str, int]
    unresolved_reference_count: int
    ambiguous_reference_count: int
    fallback_full_rebuild_reasons: tuple[str, ...]
    extraction_graph_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_file_count": self.input_file_count,
            "node_kind_counts": dict(self.node_kind_counts),
            "edge_kind_counts": dict(self.edge_kind_counts),
            "nodes_by_substrate": dict(self.nodes_by_substrate),
            "edges_by_substrate": dict(self.edges_by_substrate),
            "persistence_excluded_nodes": self.persistence_excluded_nodes,
            "persistence_excluded_edges": self.persistence_excluded_edges,
            "persistence_exclusion_reasons": dict(self.persistence_exclusion_reasons),
            "unresolved_reference_count": self.unresolved_reference_count,
            "ambiguous_reference_count": self.ambiguous_reference_count,
            "fallback_full_rebuild_reasons": list(self.fallback_full_rebuild_reasons),
            "extraction_graph_hash": self.extraction_graph_hash,
        }


def collect_graph_metrics(
    graph: Mapping[str, Any],
    *,
    input_paths: Iterable[str],
    unresolved_references: int | Iterable[Any],
    ambiguous_references: int | Iterable[Any],
    fallback_full_rebuild_reasons: Iterable[str] = (),
    accepted_node_kinds: Iterable[str] = PERSISTED_NODE_KINDS,
    accepted_edge_kinds: Iterable[str] = PERSISTED_EDGE_KINDS,
) -> GraphMetrics:
    """Collect honest graph observability without inferring Unknown as zero.

    The caller must provide unresolved/ambiguous observations explicitly.
    Extractor-native facts should be supplied once available; this function
    deliberately does not guess from an absent destination node because calls
    and external imports may legitimately have edge-only destinations.
    """

    nodes = tuple(node for node in graph.get("nodes", ()) if isinstance(node, Mapping))
    edges = tuple(edge for edge in graph.get("edges", ()) if isinstance(edge, Mapping))
    node_kind_counts = Counter(str(node.get("kind")) for node in nodes)
    edge_kind_counts = Counter(str(edge.get("kind")) for edge in edges)
    for kind in EXTRACTOR_NODE_KIND_ORDER:
        node_kind_counts.setdefault(kind, 0)
    for kind in EXTRACTOR_EDGE_KIND_ORDER:
        edge_kind_counts.setdefault(kind, 0)

    substrate_names = tuple(SUBSTRATE_CONTRACTS)
    nodes_by_substrate = Counter({name: 0 for name in substrate_names})
    edges_by_substrate = Counter({name: 0 for name in substrate_names})
    nodes_by_substrate.update(classify_node_substrate(node) for node in nodes)
    catalog = _resource_key_catalog(nodes)
    edges_by_substrate.update(classify_edge_substrate(edge, catalog) for edge in edges)

    accepted_nodes = frozenset(accepted_node_kinds)
    accepted_edges = frozenset(accepted_edge_kinds)
    excluded_node_counts = Counter(
        str(node.get("kind")) for node in nodes if node.get("kind") not in accepted_nodes
    )
    excluded_edge_counts = Counter(
        str(edge.get("kind")) for edge in edges if edge.get("kind") not in accepted_edges
    )
    exclusion_reasons = {
        f"node_kind_not_accepted:{kind}": count
        for kind, count in sorted(excluded_node_counts.items())
    }
    exclusion_reasons.update(
        {
            f"edge_kind_not_accepted:{kind}": count
            for kind, count in sorted(excluded_edge_counts.items())
        }
    )
    reasons = tuple(
        sorted(
            {
                str(reason).strip()
                for reason in fallback_full_rebuild_reasons
                if _nonempty_string(reason)
            }
        )
    )
    return GraphMetrics(
        input_file_count=len(
            {
                unicodedata.normalize("NFC", str(path).replace("\\", "/"))
                for path in input_paths
                if _nonempty_string(path)
            }
        ),
        node_kind_counts=dict(sorted(node_kind_counts.items())),
        edge_kind_counts=dict(sorted(edge_kind_counts.items())),
        nodes_by_substrate=dict(sorted(nodes_by_substrate.items())),
        edges_by_substrate=dict(sorted(edges_by_substrate.items())),
        persistence_excluded_nodes=sum(excluded_node_counts.values()),
        persistence_excluded_edges=sum(excluded_edge_counts.values()),
        persistence_exclusion_reasons=dict(sorted(exclusion_reasons.items())),
        unresolved_reference_count=_observation_count(
            unresolved_references, "unresolved_reference"
        ),
        ambiguous_reference_count=_observation_count(
            ambiguous_references, "ambiguous_reference"
        ),
        fallback_full_rebuild_reasons=reasons,
        extraction_graph_hash=canonical_graph_hash(graph),
    )


_SQL_LITERAL_RE = re.compile(r"'((?:''|[^'])*)'")


def _sql_literals(fragment: str) -> frozenset[str]:
    values = [match.replace("''", "'") for match in _SQL_LITERAL_RE.findall(fragment)]
    if not values:
        raise SchemaInventoryParseError("kind list contains no SQL string literals")
    return frozenset(values)


def _constraint_kinds(sql: str, constraint_name: str) -> frozenset[str]:
    pattern = re.compile(
        rf"\bCONSTRAINT\s+{re.escape(constraint_name)}\s+CHECK\s*"
        rf"\(.*?\bARRAY\s*\[(?P<values>.*?)\]\s*\)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    matches = list(pattern.finditer(sql))
    if not matches:
        raise SchemaInventoryParseError(
            f"expected at least one {constraint_name} ARRAY check, found 0"
        )
    definitions = {
        _sql_literals(match.group("values"))
        for match in matches
    }
    if len(definitions) != 1:
        rendered = "; ".join(
            ",".join(sorted(definition)) for definition in definitions
        )
        raise SchemaInventoryParseError(
            f"conflicting {constraint_name} ARRAY checks: {rendered}"
        )
    # CREATE TABLE plus an idempotent live-migration replacement commonly
    # repeats the same named constraint.  Identical definitions are one proven
    # acceptance set, not ambiguity.
    return next(iter(definitions))


def _function_block(sql: str, function_name: str) -> str:
    start_pattern = re.compile(
        rf"\bCREATE\s+OR\s+REPLACE\s+FUNCTION\s+core\.{re.escape(function_name)}\b",
        flags=re.IGNORECASE,
    )
    end_pattern = re.compile(
        rf"\bALTER\s+FUNCTION\s+core\.{re.escape(function_name)}\b",
        flags=re.IGNORECASE,
    )
    starts = list(start_pattern.finditer(sql))
    if len(starts) != 1:
        raise SchemaInventoryParseError(
            f"expected exactly one core.{function_name} definition, found {len(starts)}"
        )
    end = end_pattern.search(sql, starts[0].end())
    if end is None:
        raise SchemaInventoryParseError(
            f"could not find ALTER FUNCTION terminator for core.{function_name}"
        )
    return sql[starts[0].start() : end.start()]


def _persistence_insert_statement(function_sql: str, alias: str) -> str:
    """Return the one statement that persists the alias into its core table.

    Ingest functions also inspect ``n/e->>'kind'`` while validating metrics
    and counting document nodes.  Those predicates do not constrain what the
    persistence INSERT accepts, so treating the whole function as one filter
    surface can report false schema drift.
    """

    table_name = "code_node" if alias == "n" else "code_edge"
    start_pattern = re.compile(
        rf"\bINSERT\s+INTO\s+core\.{re.escape(table_name)}\b",
        flags=re.IGNORECASE,
    )
    starts = list(start_pattern.finditer(function_sql))
    if len(starts) != 1:
        raise SchemaInventoryParseError(
            f"expected exactly one INSERT INTO core.{table_name}, "
            f"found {len(starts)}"
        )

    # Find the SQL statement terminator, not a semicolon in an explanatory
    # comment or string literal.  Production migration comments intentionally
    # contain punctuation such as ``... bounded length; anything else ...``.
    index = starts[0].end()
    in_single_quote = False
    in_double_quote = False
    in_line_comment = False
    in_block_comment = False
    while index < len(function_sql):
        char = function_sql[index]
        following = function_sql[index + 1] if index + 1 < len(function_sql) else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
        elif in_block_comment:
            if char == "*" and following == "/":
                in_block_comment = False
                index += 1
        elif in_single_quote:
            if char == "'" and following == "'":
                index += 1
            elif char == "'":
                in_single_quote = False
        elif in_double_quote:
            if char == '"' and following == '"':
                index += 1
            elif char == '"':
                in_double_quote = False
        elif char == "-" and following == "-":
            in_line_comment = True
            index += 1
        elif char == "/" and following == "*":
            in_block_comment = True
            index += 1
        elif char == "'":
            in_single_quote = True
        elif char == '"':
            in_double_quote = True
        elif char == ";":
            return function_sql[starts[0].start() : index + 1]
        index += 1

    raise SchemaInventoryParseError(
        f"unterminated INSERT INTO core.{table_name}"
    )


def _ingest_filter_kinds(function_sql: str, alias: str) -> frozenset[str]:
    insert_sql = _persistence_insert_statement(function_sql, alias)
    where_matches = list(
        re.finditer(r"\bWHERE\b", insert_sql, flags=re.IGNORECASE)
    )
    if len(where_matches) != 1:
        raise SchemaInventoryParseError(
            "expected exactly one persistence WHERE clause for "
            f"{alias}->>'kind', found {len(where_matches)}"
        )
    where_sql = insert_sql[where_matches[0].start() :]
    literal_pattern = re.compile(
        rf"\b{re.escape(alias)}\s*->>\s*'kind'\s+IN\s*"
        rf"\((?P<values>.*?)\)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    literal_matches = list(literal_pattern.finditer(where_sql))
    if len(literal_matches) > 1:
        raise SchemaInventoryParseError(
            f"expected at most one {alias}->>'kind' IN filter in the "
            f"persistence INSERT, "
            f"found {len(literal_matches)}"
        )
    candidates = {
        _sql_literals(match.group("values")) for match in literal_matches
    }

    # Newer persistence functions declare one named allowlist and use
    # ``kind = ANY(v_allowed_*_kinds)`` both for fail-loud validation and the
    # INSERT filter.  Parse the declaration rather than mistaking a narrower
    # resource-kind ANY expression for the acceptance wall.
    variable = "v_allowed_node_kinds" if alias == "n" else "v_allowed_edge_kinds"
    declaration_pattern = re.compile(
        rf"\b{re.escape(variable)}\s+(?:CONSTANT\s+)?text\s*\[\s*\]\s*"
        rf":=\s*ARRAY\s*\[(?P<values>.*?)\]\s*;",
        flags=re.IGNORECASE | re.DOTALL,
    )
    declarations = list(declaration_pattern.finditer(function_sql))
    if len(declarations) > 1:
        raise SchemaInventoryParseError(
            f"expected at most one {variable} declaration, found {len(declarations)}"
        )
    if declarations:
        use_pattern = re.compile(
            rf"\b{re.escape(alias)}\s*->>\s*'kind'\s*=\s*ANY\s*"
            rf"\(\s*{re.escape(variable)}\s*\)",
            flags=re.IGNORECASE,
        )
        use_matches = list(use_pattern.finditer(where_sql))
        used_by_insert = bool(use_matches)
        if not used_by_insert and not literal_matches:
            raise SchemaInventoryParseError(
                f"{variable} is declared but not used by the "
                f"core.{'code_node' if alias == 'n' else 'code_edge'} INSERT"
            )
        # A literal insertion filter must agree with the named allowlist even
        # if it does not reference the variable directly.  This makes a
        # narrowing INSERT fail closed instead of silently accepting the
        # declaration as the effective persistence wall.
        candidates.add(_sql_literals(declarations[0].group("values")))
    else:
        use_matches = []

    if not candidates:
        raise SchemaInventoryParseError(
            f"no provable acceptance filter for {alias}->>'kind'"
        )
    if len(candidates) != 1:
        rendered = "; ".join(
            ",".join(sorted(definition)) for definition in candidates
        )
        raise SchemaInventoryParseError(
            f"conflicting acceptance filters for {alias}->>'kind': {rendered}"
        )

    # The allowlist must be a standalone top-level AND term, and it must be the
    # only access to this input alias's kind in the persistence WHERE clause.
    # Merely finding ``kind=ANY(allowlist)`` is insufficient: appending
    # ``AND kind<>'column'`` (or wrapping the allowlist in NOT/OR) silently
    # narrows the INSERT while a declaration-only inventory still reports every
    # kind as accepted.  This intentionally fails closed on a more elaborate
    # predicate until the inventory parser is taught its exact semantics.
    accepted_matches = [*literal_matches, *use_matches]
    kind_access_pattern = re.compile(
        rf"\b{re.escape(alias)}\s*->>?\s*'kind'",
        flags=re.IGNORECASE,
    )
    kind_accesses = list(kind_access_pattern.finditer(where_sql))
    for match in accepted_matches:
        prefix = where_sql[: match.start()]
        suffix = where_sql[match.end() :]
        if not re.search(
            r"(?:\bWHERE\b|\bAND\b)\s*$",
            prefix,
            flags=re.IGNORECASE | re.DOTALL,
        ) or not re.match(
            r"\s*(?:\bAND\b|;)",
            suffix,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            raise SchemaInventoryParseError(
                f"{alias}->>'kind' acceptance filter is not a standalone "
                "persistence AND term"
            )
    if any(
        not any(
            accepted.start() <= access.start() < accepted.end()
            for accepted in accepted_matches
        )
        for access in kind_accesses
    ):
        raise SchemaInventoryParseError(
            f"unrecognized narrowing access to {alias}->'kind' in the "
            "persistence WHERE clause"
        )
    return next(iter(candidates))


@dataclass(frozen=True)
class SqlSchemaAcceptance:
    constraint_node_kinds: frozenset[str]
    constraint_edge_kinds: frozenset[str]
    full_ingest_node_kinds: frozenset[str]
    full_ingest_edge_kinds: frozenset[str]
    incremental_patch_node_kinds: frozenset[str]
    incremental_patch_edge_kinds: frozenset[str]

    @property
    def node_stages(self) -> Mapping[str, frozenset[str]]:
        return {
            "db_constraint": self.constraint_node_kinds,
            "full_ingest": self.full_ingest_node_kinds,
            "incremental_patch": self.incremental_patch_node_kinds,
        }

    @property
    def edge_stages(self) -> Mapping[str, frozenset[str]]:
        return {
            "db_constraint": self.constraint_edge_kinds,
            "full_ingest": self.full_ingest_edge_kinds,
            "incremental_patch": self.incremental_patch_edge_kinds,
        }


def parse_sql_schema_acceptance(
    core_sql: str, gate_sql: str
) -> SqlSchemaAcceptance:
    """Parse DB CHECKs and both full/incremental ingest allowlists."""

    full = _function_block(gate_sql, "ingest_graph_with_authority")
    patch = _function_block(gate_sql, "patch_graph_with_authority")
    return SqlSchemaAcceptance(
        constraint_node_kinds=_constraint_kinds(core_sql, "code_node_kind_check"),
        constraint_edge_kinds=_constraint_kinds(core_sql, "code_edge_kind_check"),
        full_ingest_node_kinds=_ingest_filter_kinds(full, "n"),
        full_ingest_edge_kinds=_ingest_filter_kinds(full, "e"),
        incremental_patch_node_kinds=_ingest_filter_kinds(patch, "n"),
        incremental_patch_edge_kinds=_ingest_filter_kinds(patch, "e"),
    )


def _parse_effective_adjacency_kinds(
    adjacency_sql: str, column_name: str
) -> frozenset[str]:
    """Parse every literal kind predicate for one ``_claim_adjacency`` column.

    This deliberately accepts only directly auditable literal equality, ``IN``,
    and ``= ANY(ARRAY[...])`` predicates.  If the function mentions the kind
    column in any other expression, the inventory fails closed instead of
    reporting an incomplete acceptance set as authoritative.
    """
    function_sql = _function_block(adjacency_sql, "_claim_adjacency")
    # Predicates inside comments are explanation, not executable adjacency.
    function_sql = re.sub(r"/\*.*?\*/", "", function_sql, flags=re.DOTALL)
    function_sql = re.sub(r"--[^\n]*", "", function_sql)

    kinds: set[str] = set()
    recognized_spans: list[tuple[int, int]] = []
    # Resolver identity is not an adjacency-kind filter. cg3+ passes node_kind
    # into this fixed helper so file/symbol/resource Nodes select the correct
    # exact key. Treat only that named, fully qualified call as auditable
    # non-predicate use; arbitrary expressions still fail closed below.
    if column_name == "node_kind":
        semantic_key_call = re.compile(
            r"\bcore\._node_semantic_key\s*\([^)]*\)",
            flags=re.IGNORECASE | re.DOTALL,
        )
        recognized_spans.extend(
            match.span() for match in semantic_key_call.finditer(function_sql)
        )
    equality_pattern = re.compile(
        rf"\b{re.escape(column_name)}\s*=\s*"
        r"(?P<value>'(?:''|[^'])*')",
        flags=re.IGNORECASE,
    )
    for match in equality_pattern.finditer(function_sql):
        kinds.update(_sql_literals(match.group("value")))
        recognized_spans.append(match.span())
    in_pattern = re.compile(
        rf"\b{re.escape(column_name)}\s+IN\s*"
        r"\((?P<values>.*?)\)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in in_pattern.finditer(function_sql):
        kinds.update(_sql_literals(match.group("values")))
        recognized_spans.append(match.span())
    any_array_pattern = re.compile(
        rf"\b{re.escape(column_name)}\s*=\s*ANY\s*\(\s*ARRAY\s*"
        r"\[(?P<values>.*?)\](?:\s*::\s*text\s*\[\s*\])?\s*\)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in any_array_pattern.finditer(function_sql):
        kinds.update(_sql_literals(match.group("values")))
        recognized_spans.append(match.span())

    column_pattern = re.compile(
        rf"\b{re.escape(column_name)}\b", flags=re.IGNORECASE
    )
    unsupported_offsets = [
        match.start()
        for match in column_pattern.finditer(function_sql)
        if not any(start <= match.start() < end for start, end in recognized_spans)
    ]
    if unsupported_offsets:
        raise SchemaInventoryParseError(
            f"unsupported {column_name} expression(s) in core._claim_adjacency "
            f"at offsets {unsupported_offsets}"
        )
    if not kinds:
        raise SchemaInventoryParseError(
            f"no {column_name} predicates found in core._claim_adjacency"
        )
    return frozenset(kinds)


def parse_effective_adjacency_node_kinds(adjacency_sql: str) -> frozenset[str]:
    """Parse node-kind predicates actually used by ``core._claim_adjacency``."""

    return _parse_effective_adjacency_kinds(adjacency_sql, "node_kind")


def parse_effective_adjacency_edge_kinds(adjacency_sql: str) -> frozenset[str]:
    """Parse edge-kind predicates actually used by ``core._claim_adjacency``."""

    return _parse_effective_adjacency_kinds(adjacency_sql, "edge_kind")


@dataclass(frozen=True)
class SchemaInventoryReport:
    acceptance: SqlSchemaAcceptance
    actual_adjacency_node_kinds: frozenset[str]
    actual_adjacency_edge_kinds: frozenset[str]
    node_losses_by_stage: Mapping[str, frozenset[str]]
    edge_losses_by_stage: Mapping[str, frozenset[str]]
    unexpected_nodes_by_stage: Mapping[str, frozenset[str]]
    unexpected_edges_by_stage: Mapping[str, frozenset[str]]
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def effective_persisted_node_kinds(self) -> frozenset[str]:
        return frozenset.intersection(*self.acceptance.node_stages.values())

    @property
    def effective_persisted_edge_kinds(self) -> frozenset[str]:
        return frozenset.intersection(*self.acceptance.edge_stages.values())

    @property
    def persistence_node_losses(self) -> frozenset[str]:
        return EXTRACTOR_NODE_KINDS - self.effective_persisted_node_kinds

    @property
    def persistence_edge_losses(self) -> frozenset[str]:
        return EXTRACTOR_EDGE_KINDS - self.effective_persisted_edge_kinds

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_contract_version": SCHEMA_CONTRACT_VERSION,
            "semantic_ref_version": SEMANTIC_REF_VERSION,
            "semantic_identity_columns": {
                "node": "semantic_key",
                "edge": "semantic_dst_key",
                "algorithm": "sha256-utf8",
            },
            "node_analysis_statuses": sorted(NODE_ANALYSIS_STATUSES),
            "edge_reference_statuses": sorted(EDGE_REFERENCE_STATUSES),
            "effective_adjacency_reference_status": "resolved_only",
            "ok": self.ok,
            "extractor_node_kinds": list(EXTRACTOR_NODE_KIND_ORDER),
            "extractor_edge_kinds": list(EXTRACTOR_EDGE_KIND_ORDER),
            "required_persisted_node_kinds": sorted(PERSISTED_NODE_KINDS),
            "required_persisted_edge_kinds": sorted(PERSISTED_EDGE_KINDS),
            "db_node_kinds_by_stage": {
                stage: sorted(kinds)
                for stage, kinds in self.acceptance.node_stages.items()
            },
            "db_edge_kinds_by_stage": {
                stage: sorted(kinds)
                for stage, kinds in self.acceptance.edge_stages.items()
            },
            "effective_persisted_node_kinds": sorted(
                self.effective_persisted_node_kinds
            ),
            "effective_persisted_edge_kinds": sorted(
                self.effective_persisted_edge_kinds
            ),
            "persistence_node_losses": sorted(self.persistence_node_losses),
            "persistence_edge_losses": sorted(self.persistence_edge_losses),
            "node_losses_by_stage": {
                stage: sorted(kinds)
                for stage, kinds in self.node_losses_by_stage.items()
            },
            "edge_losses_by_stage": {
                stage: sorted(kinds)
                for stage, kinds in self.edge_losses_by_stage.items()
            },
            "unexpected_nodes_by_stage": {
                stage: sorted(kinds)
                for stage, kinds in self.unexpected_nodes_by_stage.items()
            },
            "unexpected_edges_by_stage": {
                stage: sorted(kinds)
                for stage, kinds in self.unexpected_edges_by_stage.items()
            },
            "resource_node_kinds": sorted(RESOURCE_NODE_KINDS),
            "effective_adjacency_node_kinds": sorted(
                EFFECTIVE_ADJACENCY_NODE_KINDS
            ),
            "actual_adjacency_node_kinds": sorted(
                self.actual_adjacency_node_kinds
            ),
            "effective_adjacency_edge_kinds": sorted(
                EFFECTIVE_ADJACENCY_EDGE_KINDS
            ),
            "actual_adjacency_edge_kinds": sorted(
                self.actual_adjacency_edge_kinds
            ),
            "structural_only_edge_kinds": sorted(STRUCTURAL_ONLY_EDGE_KINDS),
            "evidence_only_edge_kinds": sorted(EVIDENCE_ONLY_EDGE_KINDS),
            "non_adjacency_edge_kinds": sorted(NON_ADJACENCY_EDGE_KINDS),
            "non_adjacency_reasons": dict(sorted(NON_ADJACENCY_REASONS.items())),
            "resource_metadata": {
                "required_fields": sorted(RESOURCE_METADATA_FIELDS),
                "path_or_scope": sorted(RESOURCE_METADATA_PATH_OR_SCOPE_FIELDS),
            },
            "substrates": {
                name: {
                    "node_kinds": sorted(contract.node_kinds),
                    "edge_kinds": sorted(contract.edge_kinds),
                    "extractor": contract.extractor,
                    "description": contract.description,
                }
                for name, contract in SUBSTRATE_CONTRACTS.items()
            },
            "errors": list(self.errors),
        }


def build_schema_inventory(
    acceptance: SqlSchemaAcceptance,
    *,
    actual_adjacency_node_kinds: Iterable[str],
    actual_adjacency_edge_kinds: Iterable[str],
) -> SchemaInventoryReport:
    actual_adjacency_nodes = frozenset(actual_adjacency_node_kinds)
    actual_adjacency_edges = frozenset(actual_adjacency_edge_kinds)
    node_losses = {
        stage: PERSISTED_NODE_KINDS - kinds
        for stage, kinds in acceptance.node_stages.items()
    }
    edge_losses = {
        stage: PERSISTED_EDGE_KINDS - kinds
        for stage, kinds in acceptance.edge_stages.items()
    }
    unexpected_nodes = {
        stage: kinds - PERSISTED_NODE_KINDS
        for stage, kinds in acceptance.node_stages.items()
    }
    unexpected_edges = {
        stage: kinds - PERSISTED_EDGE_KINDS
        for stage, kinds in acceptance.edge_stages.items()
    }
    errors: list[str] = []
    for stage, losses in node_losses.items():
        if losses:
            errors.append(f"{stage} drops node kinds: {', '.join(sorted(losses))}")
    for stage, losses in edge_losses.items():
        if losses:
            errors.append(f"{stage} drops edge kinds: {', '.join(sorted(losses))}")
    for stage, kinds in unexpected_nodes.items():
        if kinds:
            errors.append(
                f"{stage} accepts undeclared node kinds: {', '.join(sorted(kinds))}"
            )
    for stage, kinds in unexpected_edges.items():
        if kinds:
            errors.append(
                f"{stage} accepts undeclared edge kinds: {', '.join(sorted(kinds))}"
            )

    node_stage_sets = set(acceptance.node_stages.values())
    edge_stage_sets = set(acceptance.edge_stages.values())
    if len(node_stage_sets) != 1:
        errors.append("node-kind allowlists drift between DB constraint/full/patch")
    if len(edge_stage_sets) != 1:
        errors.append("edge-kind allowlists drift between DB constraint/full/patch")
    missing_adjacency_nodes = (
        EFFECTIVE_ADJACENCY_NODE_KINDS - actual_adjacency_nodes
    )
    unexpected_adjacency_nodes = (
        actual_adjacency_nodes - EFFECTIVE_ADJACENCY_NODE_KINDS
    )
    if missing_adjacency_nodes:
        errors.append(
            "effective adjacency SQL does not consume declared node kinds: "
            + ", ".join(sorted(missing_adjacency_nodes))
        )
    if unexpected_adjacency_nodes:
        errors.append(
            "effective adjacency SQL consumes undeclared node kinds: "
            + ", ".join(sorted(unexpected_adjacency_nodes))
        )
    missing_adjacency_edges = (
        EFFECTIVE_ADJACENCY_EDGE_KINDS - actual_adjacency_edges
    )
    unexpected_adjacency_edges = (
        actual_adjacency_edges - EFFECTIVE_ADJACENCY_EDGE_KINDS
    )
    if missing_adjacency_edges:
        errors.append(
            "effective adjacency SQL does not consume declared edge kinds: "
            + ", ".join(sorted(missing_adjacency_edges))
        )
    if unexpected_adjacency_edges:
        errors.append(
            "effective adjacency SQL consumes undeclared edge kinds: "
            + ", ".join(sorted(unexpected_adjacency_edges))
        )

    return SchemaInventoryReport(
        acceptance=acceptance,
        actual_adjacency_node_kinds=actual_adjacency_nodes,
        actual_adjacency_edge_kinds=actual_adjacency_edges,
        node_losses_by_stage=node_losses,
        edge_losses_by_stage=edge_losses,
        unexpected_nodes_by_stage=unexpected_nodes,
        unexpected_edges_by_stage=unexpected_edges,
        errors=tuple(errors),
    )


def schema_inventory_from_paths(
    core_sql_path: str | Path,
    gate_sql_path: str | Path,
    adjacency_sql_path: str | Path,
) -> SchemaInventoryReport:
    core_sql = Path(core_sql_path).read_text(encoding="utf-8")
    gate_sql = Path(gate_sql_path).read_text(encoding="utf-8")
    adjacency_sql = Path(adjacency_sql_path).read_text(encoding="utf-8")
    return build_schema_inventory(
        parse_sql_schema_acceptance(core_sql, gate_sql),
        actual_adjacency_node_kinds=parse_effective_adjacency_node_kinds(
            adjacency_sql
        ),
        actual_adjacency_edge_kinds=parse_effective_adjacency_edge_kinds(
            adjacency_sql
        ),
    )


def assert_schema_inventory_aligned(report: SchemaInventoryReport) -> None:
    if not report.ok:
        detail = "\n".join(f"- {error}" for error in report.errors)
        raise SchemaDriftError(f"graph schema inventory failed:\n{detail}")


def contract_self_check() -> tuple[str, ...]:
    """Return structural errors in the Python contract itself."""

    errors: list[str] = []
    if len(EXTRACTOR_NODE_KINDS) != 20:
        errors.append(f"expected 20 node kinds, found {len(EXTRACTOR_NODE_KINDS)}")
    if len(EXTRACTOR_EDGE_KINDS) != 8:
        errors.append(f"expected 8 edge kinds, found {len(EXTRACTOR_EDGE_KINDS)}")
    if PERSISTED_NODE_KINDS != EXTRACTOR_NODE_KINDS:
        errors.append("persistence node contract is lossy")
    if PERSISTED_EDGE_KINDS != EXTRACTOR_EDGE_KINDS:
        errors.append("persistence edge contract is lossy")
    if not EFFECTIVE_ADJACENCY_NODE_KINDS <= PERSISTED_NODE_KINDS:
        errors.append(
            "effective adjacency consumes node kinds outside persistence"
        )
    if not EFFECTIVE_ADJACENCY_EDGE_KINDS <= PERSISTED_EDGE_KINDS:
        errors.append(
            "effective adjacency consumes edge kinds outside persistence"
        )
    accounted_nodes = frozenset().union(
        *(contract.node_kinds for contract in SUBSTRATE_CONTRACTS.values())
    )
    accounted_edges = frozenset().union(
        *(contract.edge_kinds for contract in SUBSTRATE_CONTRACTS.values())
    )
    if accounted_nodes != EXTRACTOR_NODE_KINDS:
        errors.append(
            "substrate node accounting mismatch: "
            f"missing={sorted(EXTRACTOR_NODE_KINDS - accounted_nodes)}, "
            f"extra={sorted(accounted_nodes - EXTRACTOR_NODE_KINDS)}"
        )
    if accounted_edges != EXTRACTOR_EDGE_KINDS:
        errors.append(
            "substrate edge accounting mismatch: "
            f"missing={sorted(EXTRACTOR_EDGE_KINDS - accounted_edges)}, "
            f"extra={sorted(accounted_edges - EXTRACTOR_EDGE_KINDS)}"
        )
    if set(RESOURCE_KIND_TO_SUBSTRATE) != RESOURCE_NODE_KINDS:
        errors.append("resource kinds are not completely mapped to substrates")
    if (
        EFFECTIVE_ADJACENCY_EDGE_KINDS | NON_ADJACENCY_EDGE_KINDS
        != PERSISTED_EDGE_KINDS
    ):
        errors.append("every persisted edge must be adjacency-bearing or non-adjacency evidence")
    if EFFECTIVE_ADJACENCY_EDGE_KINDS & NON_ADJACENCY_EDGE_KINDS:
        errors.append("an edge cannot be both adjacency-bearing and non-adjacency evidence")
    if STRUCTURAL_ONLY_EDGE_KINDS & EVIDENCE_ONLY_EDGE_KINDS:
        errors.append("structural-only and evidence-only edge sets must be disjoint")
    if set(NON_ADJACENCY_REASONS) != NON_ADJACENCY_EDGE_KINDS:
        errors.append("every non-adjacency edge requires a documented reason")
    return tuple(errors)
