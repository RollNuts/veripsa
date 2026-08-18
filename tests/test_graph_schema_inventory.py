#!/usr/bin/env python3
"""Hermetic contract tests for graph extraction, persistence, and hashing."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cg_schema_contract as C  # noqa: E402
import code_graph_extract as X  # noqa: E402


def _quoted(kinds) -> str:
    return ",".join(f"'{kind}'" for kind in kinds)


def _core_sql(node_kinds, edge_kinds) -> str:
    return f"""
    CREATE TABLE core.code_node (
      node_kind text,
      CONSTRAINT code_node_kind_check
        CHECK (node_kind = ANY (ARRAY[{_quoted(node_kinds)}]))
    );
    CREATE TABLE core.code_edge (
      edge_kind text,
      CONSTRAINT code_edge_kind_check
        CHECK (edge_kind = ANY (ARRAY[{_quoted(edge_kinds)}]))
    );
    """


def _function_sql(name: str, node_kinds, edge_kinds) -> str:
    return f"""
    CREATE OR REPLACE FUNCTION core.{name}(p_graph jsonb)
    RETURNS void LANGUAGE plpgsql AS $$
    BEGIN
      INSERT INTO core.code_node
      SELECT n->>'kind' FROM jsonb_array_elements(p_graph->'nodes') n
       WHERE n->>'kind' IN ({_quoted(node_kinds)});
      INSERT INTO core.code_edge
      SELECT e->>'kind' FROM jsonb_array_elements(p_graph->'edges') e
       WHERE e->>'kind' IN ({_quoted(edge_kinds)});
    END $$;
    ALTER FUNCTION core.{name}(jsonb) OWNER TO veripsa_migrator;
    """


def _function_sql_any(name: str, node_kinds, edge_kinds) -> str:
    return f"""
    CREATE OR REPLACE FUNCTION core.{name}(p_graph jsonb)
    RETURNS void LANGUAGE plpgsql AS $$
    DECLARE
      v_allowed_node_kinds CONSTANT text[] := ARRAY[{_quoted(node_kinds)}];
      v_allowed_edge_kinds CONSTANT text[] := ARRAY[{_quoted(edge_kinds)}];
    BEGIN
      INSERT INTO core.code_node
      SELECT n->>'kind' FROM jsonb_array_elements(p_graph->'nodes') n
       WHERE n->>'kind'=ANY(v_allowed_node_kinds);
      INSERT INTO core.code_edge
      SELECT e->>'kind' FROM jsonb_array_elements(p_graph->'edges') e
       WHERE e->>'kind' = ANY (v_allowed_edge_kinds);
    END $$;
    ALTER FUNCTION core.{name}(jsonb) OWNER TO veripsa_migrator;
    """


def _gate_sql(
    full_nodes=C.EXTRACTOR_NODE_KIND_ORDER,
    full_edges=C.EXTRACTOR_EDGE_KIND_ORDER,
    patch_nodes=C.EXTRACTOR_NODE_KIND_ORDER,
    patch_edges=C.EXTRACTOR_EDGE_KIND_ORDER,
) -> str:
    return _function_sql(
        "ingest_graph_with_authority", full_nodes, full_edges
    ) + _function_sql("patch_graph_with_authority", patch_nodes, patch_edges)


def _kind_predicates(column: str, kinds) -> str:
    ordered = sorted(kinds)
    if not ordered:
        return "FALSE"
    predicates = [f"{column}='{ordered[0]}'"]
    if len(ordered) > 1:
        predicates.append(f"{column} IN ({_quoted(ordered[1:])})")
    return " OR ".join(predicates)


def _adjacency_sql(
    edge_kinds=C.EFFECTIVE_ADJACENCY_EDGE_KINDS,
    node_kinds=C.EFFECTIVE_ADJACENCY_NODE_KINDS,
) -> str:
    return f"""
    CREATE OR REPLACE FUNCTION core._claim_adjacency()
    RETURNS void LANGUAGE sql AS $$
      SELECT 1
        FROM core.code_edge ce
        JOIN core.code_node cn ON true
       WHERE ({_kind_predicates("ce.edge_kind", edge_kinds)})
         AND ({_kind_predicates("cn.node_kind", node_kinds)});
    $$;
    ALTER FUNCTION core._claim_adjacency() OWNER TO veripsa_migrator;
    """


def _inventory(
    acceptance,
    adjacency_edge_kinds=C.EFFECTIVE_ADJACENCY_EDGE_KINDS,
    adjacency_node_kinds=C.EFFECTIVE_ADJACENCY_NODE_KINDS,
):
    return C.build_schema_inventory(
        acceptance,
        actual_adjacency_node_kinds=adjacency_node_kinds,
        actual_adjacency_edge_kinds=adjacency_edge_kinds,
    )


def _all_kind_graph() -> dict:
    nodes = []
    for kind in C.EXTRACTOR_NODE_KIND_ORDER:
        node = {
            "id": f"{kind}::fixture",
            "kind": kind,
            "name": "fixture",
            "path": f"fixtures/{kind}.txt",
        }
        if kind == "column":
            node["table"] = "fixture_table"
        if kind in C.RESOURCE_NODE_KINDS:
            node = C.enrich_resource_node(node, repo="RollNuts/veripsa")
        nodes.append(node)
    edges = [
        {
            "src": f"fixtures/source_{index}.py",
            "dst": f"target::{kind}",
            "kind": kind,
        }
        for index, kind in enumerate(C.EXTRACTOR_EDGE_KIND_ORDER)
    ]
    return {"root": "/ignored/root", "nodes": nodes, "edges": edges}


def test_contract_accounts_for_every_declared_kind() -> None:
    assert C.EXTRACTOR_VERSION == "cg4"
    assert len(C.EXTRACTOR_NODE_KINDS) == 20
    assert len(C.EXTRACTOR_EDGE_KINDS) == 8
    assert C.PERSISTED_NODE_KINDS == C.EXTRACTOR_NODE_KINDS
    assert C.PERSISTED_EDGE_KINDS == C.EXTRACTOR_EDGE_KINDS
    assert not C.contract_self_check()

    substrate_nodes = frozenset().union(
        *(contract.node_kinds for contract in C.SUBSTRATE_CONTRACTS.values())
    )
    substrate_edges = frozenset().union(
        *(contract.edge_kinds for contract in C.SUBSTRATE_CONTRACTS.values())
    )
    assert substrate_nodes == C.EXTRACTOR_NODE_KINDS
    assert substrate_edges == C.EXTRACTOR_EDGE_KINDS
    assert set(C.RESOURCE_KIND_TO_SUBSTRATE) == C.RESOURCE_NODE_KINDS
    assert (
        C.EFFECTIVE_ADJACENCY_EDGE_KINDS | C.NON_ADJACENCY_EDGE_KINDS
        == C.EXTRACTOR_EDGE_KINDS
    )
    assert C.EVIDENCE_ONLY_EDGE_KINDS == {"alters_col", "queries_col"}
    assert set(C.NON_ADJACENCY_REASONS) == C.NON_ADJACENCY_EDGE_KINDS
    assert C.RESOURCE_METADATA_PATH_OR_SCOPE_FIELDS == {"path", "scope"}
    assert C.RESOURCE_DEFINITION_EVIDENCE_KINDS == (
        C.RESOURCE_NODE_KINDS - {"config_key", "sibling_stem", "role_feature"}
    )
    assert C.EFFECTIVE_ADJACENCY_NODE_KINDS == {"file", "def", "class"}


def test_every_kind_is_exercised_by_validation_hashing_and_metrics() -> None:
    graph = _all_kind_graph()
    C.assert_valid_graph(
        graph, persisted=True, require_resource_metadata=True
    )

    metrics = C.collect_graph_metrics(
        graph,
        input_paths=(node["path"] for node in graph["nodes"]),
        unresolved_references=3,
        ambiguous_references=("dup-a", "dup-b"),
        fallback_full_rebuild_reasons=("contract-bearing file changed",),
    )
    assert all(metrics.node_kind_counts[kind] == 1 for kind in C.EXTRACTOR_NODE_KINDS)
    assert all(metrics.edge_kind_counts[kind] == 1 for kind in C.EXTRACTOR_EDGE_KINDS)
    assert metrics.persistence_excluded_nodes == 0
    assert metrics.persistence_excluded_edges == 0
    assert metrics.unresolved_reference_count == 3
    assert metrics.ambiguous_reference_count == 2
    assert len(metrics.extraction_graph_hash) == 64
    assert "graph_hash" not in metrics.as_dict()
    assert metrics.as_dict()["extraction_graph_hash"] == metrics.extraction_graph_hash

    reordered = {
        "root": "/different/root",
        "nodes": [
            dict(node, created_at="ignored") for node in reversed(graph["nodes"])
        ],
        "edges": [
            dict(edge, db_id=index) for index, edge in enumerate(reversed(graph["edges"]))
        ],
    }
    assert C.canonical_graph_hash(reordered) == C.canonical_graph_hash(graph)
    assert C.canonical_graph_diff(graph, reordered).equivalent


@pytest.mark.parametrize(
    ("node", "expected_key"),
    [
        (
            {
                "id": "table::orders",
                "kind": "table",
                "name": "orders",
                "path": "db/schema.sql",
            },
            "orders",
        ),
        (
            {
                "id": "column::orders.id",
                "kind": "column",
                "name": "id",
                "table": "orders",
                "path": "db/schema.sql",
            },
            "orders.id",
        ),
        (
            {
                "id": "cfgkey::app.yml::DATABASE_URL",
                "kind": "config_key",
                "name": "DATABASE_URL",
                "path": "app.yml",
            },
            "DATABASE_URL",
        ),
        (
            {
                "id": "iac_resource::infra::aws_s3_bucket.logs",
                "kind": "iac_resource",
                "name": "aws_s3_bucket.logs",
                "path": "infra/main.tf",
            },
            "infra::aws_s3_bucket.logs",
        ),
        (
            {
                "id": "api_operation::getUser",
                "kind": "api_operation",
                "name": "getUser",
                "path": "openapi.yml",
            },
            "api_operation::getUser",
        ),
    ],
)
def test_resource_enrichment_preserves_edge_canonical_keys(node, expected_key) -> None:
    enriched = C.enrich_resource_node(node, repo="RollNuts/veripsa")
    assert enriched["canonical_key"] == expected_key
    assert enriched["repo"] == "RollNuts/veripsa"
    assert enriched["scope"]
    assert enriched["extractor"]
    assert enriched["confidence"] == 1.0
    assert enriched["provenance"]["node_id"] == node["id"]
    assert "canonical_key" not in node


def test_resource_enrichment_enforces_persistence_bounds() -> None:
    node = {
        "id": "table::orders",
        "kind": "table",
        "name": "orders",
        "path": "db/schema.sql",
    }
    with pytest.raises(C.GraphContractError, match="scope exceeds"):
        C.enrich_resource_node(
            node,
            repo="RollNuts/veripsa",
            scope="x" * (C.RESOURCE_SCOPE_MAX_LENGTH + 1),
        )
    with pytest.raises(C.GraphContractError, match="JSON-compatible"):
        C.enrich_resource_node(
            node,
            repo="RollNuts/veripsa",
            provenance={"not_json": object()},
        )


def test_resource_enrichment_retains_ambiguity_as_content_free_provenance() -> None:
    enriched = C.enrich_resource_node(
        {
            "id": "table::orders",
            "kind": "table",
            "name": "orders",
            "path": "db/a.sql",
            "ambiguous": True,
        },
        repo="RollNuts/veripsa",
    )
    assert enriched["provenance"]["ambiguous"] is True


def test_graph_validation_fails_loudly_for_unknown_and_duplicate_kinds() -> None:
    graph = {
        "nodes": [
            {"id": "x", "kind": "mystery", "path": "x.py"},
            {"id": "x", "kind": "file", "path": "x.py"},
            {"id": "x", "kind": "file", "path": "x.py"},
        ],
        "edges": [
            {"src": "x.py", "dst": "y.py", "kind": "teleports"},
            {"src": "x.py", "dst": "y.py", "kind": "teleports"},
        ],
    }
    issues = C.validate_graph(graph)
    assert {issue.code for issue in issues} >= {
        "node_kind",
        "duplicate_node",
        "edge_kind",
        "duplicate_edge",
    }
    with pytest.raises(C.GraphContractError, match="undeclared kind"):
        C.assert_valid_graph(graph)


def test_touched_path_slice_is_mechanical_and_keeps_removed_incoming_imports() -> None:
    graph = {
        "nodes": [
            {"id": "a.py", "kind": "file", "path": "a.py"},
            {"id": "b.py", "kind": "file", "path": "b.py"},
        ],
        "edges": [
            {"src": "a.py", "dst": "b.py", "kind": "imports"},
            {"src": "b.py", "dst": "a.py", "kind": "imports"},
            {"src": "b.py", "dst": "work", "kind": "calls"},
        ],
    }
    sliced = C.slice_graph_for_touched_paths(
        graph, {"a.py"}, removed_paths={"b.py"}
    )
    assert [node["path"] for node in sliced["nodes"]] == ["a.py"]
    assert {
        (edge["src"], edge["dst"], edge["kind"]) for edge in sliced["edges"]
    } == {
        ("a.py", "b.py", "imports"),
    }


def test_touched_path_slice_keeps_double_colon_document_path_exact() -> None:
    graph = {
        "nodes": [
            {
                "id": "weird::name.py",
                "kind": "file",
                "path": "weird::name.py",
            },
            {"id": "target.py", "kind": "file", "path": "target.py"},
        ],
        "edges": [
            {
                "src": "weird::name.py",
                "dst": "target.py",
                "kind": "imports",
            },
        ],
    }
    sliced = C.slice_graph_for_touched_paths(graph, {"weird::name.py"})
    assert {
        (edge["src"], edge["dst"], edge["kind"])
        for edge in sliced["edges"]
    } == {
        ("weird::name.py", "target.py", "imports"),
    }
    assert C.slice_graph_for_touched_paths(graph, {"weird"})["edges"] == []


def test_extractor_preserves_file_and_resource_with_same_node_id(
    tmp_path: Path,
) -> None:
    key = "veripsa.webhook.py"
    collision_path = f"cfgkey::settings.json::{key}"
    (tmp_path / "settings.json").write_text(
        json.dumps({key: "configured"}), encoding="utf-8"
    )
    (tmp_path / collision_path).write_text(
        f"value = cfg[{key!r}]\n", encoding="utf-8"
    )

    graph = X.build_graph(str(tmp_path))
    colliding_nodes = {
        (node.get("kind"), node.get("path"))
        for node in graph["nodes"]
        if node.get("id") == collision_path
    }
    assert colliding_nodes == {
        ("file", collision_path),
        ("config_key", "settings.json"),
    }
    assert {
        (edge.get("src"), edge.get("dst"), edge.get("kind"))
        for edge in graph["edges"]
    } >= {
        (collision_path, key, "reads_config"),
    }


def test_cross_kind_resource_key_collision_is_inert_ambiguity(
    tmp_path: Path,
) -> None:
    """An untyped canonical-key collision must never become resource adjacency."""
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE database_url (id integer);\n"
        "CREATE TABLE stable_table (id integer);\n",
        encoding="utf-8",
    )
    (tmp_path / "settings.json").write_text(
        json.dumps({"database_url": "configured"}),
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        "import os\n"
        "database_url = os.getenv('database_url')\n"
        "rows = db.execute('SELECT * FROM stable_table')\n",
        encoding="utf-8",
    )

    graph = X.build_graph(str(tmp_path), repo="graph/resource-key-collision")
    colliding_resources = {
        node.get("kind")
        for node in graph["nodes"]
        if (
            node.get("kind") in C.RESOURCE_NODE_KINDS
            and node.get("canonical_key") == "database_url"
        )
    }
    collision_edges = [
        edge for edge in graph["edges"]
        if (
            edge.get("kind")
            in {"alters", "queries", "reads_config", "alters_col", "queries_col"}
            and edge.get("dst") == "database_url"
        )
    ]
    assert colliding_resources == {"table", "config_key"}
    assert {
        (edge.get("src"), edge.get("kind")) for edge in collision_edges
    } == {
        ("schema.sql", "alters"),
        ("app.py", "reads_config"),
    }
    assert all(
        edge.get("reference_status") == "ambiguous"
        for edge in collision_edges
    )
    assert graph["metrics"]["ambiguous_reference_count"] == 1
    resolved_edge_projection = dict(graph)
    resolved_edge_projection["nodes"] = [
        dict(node) for node in graph["nodes"]
    ]
    resolved_edge_projection["edges"] = [
        {
            key: value
            for key, value in edge.items()
            if not (
                edge.get("dst") == "database_url"
                and key == "reference_status"
            )
        }
        for edge in graph["edges"]
    ]
    assert (
        C.canonical_graph_hash(resolved_edge_projection)
        != C.canonical_graph_hash(graph)
    )
    assert {
        node.get("path")
        for node in graph["nodes"]
        if node.get("analysis_status") == "ambiguous"
    } >= {"schema.sql", "app.py"}

    # The new wall is cross-KIND only. Exact table-only definition/query
    # behavior on another key remains resolved and classification-stable.
    stable_edges = [
        edge for edge in graph["edges"]
        if edge.get("dst") == "stable_table"
        and edge.get("kind") in {"alters", "queries"}
    ]
    assert {
        (edge.get("src"), edge.get("kind")) for edge in stable_edges
    } == {
        ("schema.sql", "alters"),
        ("app.py", "queries"),
    }
    assert all(edge.get("reference_status") is None for edge in stable_edges)
    assert all(edge.get("substrate") == "database" for edge in stable_edges)


def test_same_substrate_table_column_key_collision_is_still_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct kinds collide even when both map to the database substrate."""
    (tmp_path / "table_owner.py").write_text("table_owner = True\n", encoding="utf-8")
    (tmp_path / "column_reader.py").write_text("column_reader = True\n", encoding="utf-8")

    def schema_collision(_root, _source_files, incomplete_paths_out=None):
        del incomplete_paths_out
        return (
            [
                {
                    "id": "table::orders_dot_id",
                    "kind": "table",
                    "name": "orders_dot_id",
                    "canonical_key": "orders.id",
                    "path": "table_owner.py",
                    "language": "python",
                },
                {
                    "id": "column::orders.id",
                    "kind": "column",
                    "name": "id",
                    "table": "orders",
                    "canonical_key": "orders.id",
                    "path": "column_reader.py",
                    "language": "python",
                },
            ],
            [
                {
                    "src": "table_owner.py",
                    "dst": "orders.id",
                    "kind": "alters",
                },
                {
                    "src": "column_reader.py",
                    "dst": "orders.id",
                    "kind": "queries",
                },
            ],
        )

    monkeypatch.setattr(X, "_schema_graph", schema_collision)
    graph = X.build_graph(str(tmp_path), repo="graph/table-column-collision")
    collision_edges = [
        edge for edge in graph["edges"] if edge.get("dst") == "orders.id"
    ]

    assert {
        node.get("kind")
        for node in graph["nodes"]
        if node.get("canonical_key") == "orders.id"
    } == {"table", "column"}
    assert all(edge.get("substrate") == "database" for edge in collision_edges)
    assert all(
        edge.get("reference_status") == "ambiguous"
        for edge in collision_edges
    )
    assert {
        node.get("path")
        for node in graph["nodes"]
        if node.get("analysis_status") == "ambiguous"
    } >= {"table_owner.py", "column_reader.py"}


def test_sql_inventory_parses_all_three_acceptance_stages() -> None:
    acceptance = C.parse_sql_schema_acceptance(
        _core_sql(C.EXTRACTOR_NODE_KIND_ORDER, C.EXTRACTOR_EDGE_KIND_ORDER),
        _gate_sql(),
    )
    report = _inventory(acceptance)
    assert report.ok
    assert report.effective_persisted_node_kinds == C.EXTRACTOR_NODE_KINDS
    assert report.effective_persisted_edge_kinds == C.EXTRACTOR_EDGE_KINDS
    C.assert_schema_inventory_aligned(report)


def test_sql_inventory_parses_named_any_allowlists() -> None:
    gate = _function_sql_any(
        "ingest_graph_with_authority",
        C.EXTRACTOR_NODE_KIND_ORDER,
        C.EXTRACTOR_EDGE_KIND_ORDER,
    ) + _function_sql_any(
        "patch_graph_with_authority",
        C.EXTRACTOR_NODE_KIND_ORDER,
        C.EXTRACTOR_EDGE_KIND_ORDER,
    )
    report = _inventory(
        C.parse_sql_schema_acceptance(
            _core_sql(C.EXTRACTOR_NODE_KIND_ORDER, C.EXTRACTOR_EDGE_KIND_ORDER),
            gate,
        )
    )
    assert report.ok


def test_sql_inventory_ignores_auxiliary_literal_kind_predicates() -> None:
    auxiliary_check = """
      PERFORM 1
        FROM jsonb_array_elements(p_graph->'nodes') n
       WHERE n->>'kind' IN ('file','config_file');
    """
    full = _function_sql_any(
        "ingest_graph_with_authority",
        C.EXTRACTOR_NODE_KIND_ORDER,
        C.EXTRACTOR_EDGE_KIND_ORDER,
    ).replace("BEGIN", f"BEGIN\n{auxiliary_check}", 1)
    patch = _function_sql_any(
        "patch_graph_with_authority",
        C.EXTRACTOR_NODE_KIND_ORDER,
        C.EXTRACTOR_EDGE_KIND_ORDER,
    ).replace("BEGIN", f"BEGIN\n{auxiliary_check}", 1)

    report = _inventory(
        C.parse_sql_schema_acceptance(
            _core_sql(C.EXTRACTOR_NODE_KIND_ORDER, C.EXTRACTOR_EDGE_KIND_ORDER),
            full + patch,
        )
    )
    assert report.ok


def test_sql_inventory_rejects_insert_filter_conflicting_with_named_allowlist() -> None:
    full = _function_sql_any(
        "ingest_graph_with_authority",
        C.EXTRACTOR_NODE_KIND_ORDER,
        C.EXTRACTOR_EDGE_KIND_ORDER,
    ).replace(
        "WHERE n->>'kind'=ANY(v_allowed_node_kinds);",
        (
            "WHERE n->>'kind'=ANY(v_allowed_node_kinds) "
            "AND n->>'kind' IN ('file','config_file');"
        ),
        1,
    )
    gate = full + _function_sql_any(
        "patch_graph_with_authority",
        C.EXTRACTOR_NODE_KIND_ORDER,
        C.EXTRACTOR_EDGE_KIND_ORDER,
    )

    with pytest.raises(C.SchemaInventoryParseError, match="conflicting acceptance"):
        C.parse_sql_schema_acceptance(
            _core_sql(C.EXTRACTOR_NODE_KIND_ORDER, C.EXTRACTOR_EDGE_KIND_ORDER),
            gate,
        )


def test_sql_inventory_rejects_additional_negative_kind_predicate() -> None:
    full = _function_sql_any(
        "ingest_graph_with_authority",
        C.EXTRACTOR_NODE_KIND_ORDER,
        C.EXTRACTOR_EDGE_KIND_ORDER,
    ).replace(
        "WHERE n->>'kind'=ANY(v_allowed_node_kinds);",
        (
            "WHERE n->>'kind'=ANY(v_allowed_node_kinds) "
            "AND n->>'kind'<>'column';"
        ),
        1,
    )
    gate = full + _function_sql_any(
        "patch_graph_with_authority",
        C.EXTRACTOR_NODE_KIND_ORDER,
        C.EXTRACTOR_EDGE_KIND_ORDER,
    )

    with pytest.raises(
        C.SchemaInventoryParseError, match="unrecognized narrowing"
    ):
        C.parse_sql_schema_acceptance(
            _core_sql(C.EXTRACTOR_NODE_KIND_ORDER, C.EXTRACTOR_EDGE_KIND_ORDER),
            gate,
        )


def test_sql_inventory_rejects_negated_allowlist_predicate() -> None:
    full = _function_sql_any(
        "ingest_graph_with_authority",
        C.EXTRACTOR_NODE_KIND_ORDER,
        C.EXTRACTOR_EDGE_KIND_ORDER,
    ).replace(
        "WHERE n->>'kind'=ANY(v_allowed_node_kinds);",
        "WHERE NOT n->>'kind'=ANY(v_allowed_node_kinds);",
        1,
    )
    gate = full + _function_sql_any(
        "patch_graph_with_authority",
        C.EXTRACTOR_NODE_KIND_ORDER,
        C.EXTRACTOR_EDGE_KIND_ORDER,
    )

    with pytest.raises(
        C.SchemaInventoryParseError, match="not a standalone"
    ):
        C.parse_sql_schema_acceptance(
            _core_sql(C.EXTRACTOR_NODE_KIND_ORDER, C.EXTRACTOR_EDGE_KIND_ORDER),
            gate,
        )


def test_sql_inventory_accepts_identical_live_migration_constraint_copy() -> None:
    core = _core_sql(C.EXTRACTOR_NODE_KIND_ORDER, C.EXTRACTOR_EDGE_KIND_ORDER)
    core += f"""
    ALTER TABLE core.code_node ADD CONSTRAINT code_node_kind_check
      CHECK (node_kind = ANY (ARRAY[{_quoted(C.EXTRACTOR_NODE_KIND_ORDER)}]));
    ALTER TABLE core.code_edge ADD CONSTRAINT code_edge_kind_check
      CHECK (edge_kind = ANY (ARRAY[{_quoted(C.EXTRACTOR_EDGE_KIND_ORDER)}]));
    """
    report = _inventory(
        C.parse_sql_schema_acceptance(core, _gate_sql())
    )
    assert report.ok


def test_sql_inventory_rejects_conflicting_constraint_copies() -> None:
    core = _core_sql(C.EXTRACTOR_NODE_KIND_ORDER, C.EXTRACTOR_EDGE_KIND_ORDER)
    core += f"""
    ALTER TABLE core.code_node ADD CONSTRAINT code_node_kind_check
      CHECK (node_kind = ANY (ARRAY[{_quoted(C.EXTRACTOR_NODE_KIND_ORDER[:-1])}]));
    """
    with pytest.raises(C.SchemaInventoryParseError, match="conflicting"):
        C.parse_sql_schema_acceptance(core, _gate_sql())


def test_sql_inventory_parses_actual_adjacency_and_rejects_drift() -> None:
    assert (
        C.parse_effective_adjacency_node_kinds(_adjacency_sql())
        == C.EFFECTIVE_ADJACENCY_NODE_KINDS
    )
    assert (
        C.parse_effective_adjacency_edge_kinds(_adjacency_sql())
        == C.EFFECTIVE_ADJACENCY_EDGE_KINDS
    )
    acceptance = C.parse_sql_schema_acceptance(
        _core_sql(C.EXTRACTOR_NODE_KIND_ORDER, C.EXTRACTOR_EDGE_KIND_ORDER),
        _gate_sql(),
    )
    report = _inventory(
        acceptance,
        adjacency_edge_kinds=C.parse_effective_adjacency_edge_kinds(
            _adjacency_sql({"calls", "imports"})
        ),
    )
    assert not report.ok
    assert any("does not consume" in error for error in report.errors)


def test_sql_inventory_rejects_actual_adjacency_node_drift() -> None:
    acceptance = C.parse_sql_schema_acceptance(
        _core_sql(C.EXTRACTOR_NODE_KIND_ORDER, C.EXTRACTOR_EDGE_KIND_ORDER),
        _gate_sql(),
    )
    drift_sql = _adjacency_sql(
        node_kinds={"file", "def", "config_key"}
    )
    report = _inventory(
        acceptance,
        adjacency_node_kinds=C.parse_effective_adjacency_node_kinds(
            drift_sql
        ),
    )
    assert not report.ok
    assert any(
        "does not consume declared node kinds: class" in error
        for error in report.errors
    )
    assert any(
        "consumes undeclared node kinds: config_key" in error
        for error in report.errors
    )


def test_adjacency_kind_parser_ignores_comments_and_fails_closed() -> None:
    commented = _adjacency_sql() + """
    -- node_kind='table' AND edge_kind='queries_col'
    /* node_kind IN ('column') AND edge_kind IN ('alters_col') */
    """
    assert (
        C.parse_effective_adjacency_node_kinds(commented)
        == C.EFFECTIVE_ADJACENCY_NODE_KINDS
    )
    assert (
        C.parse_effective_adjacency_edge_kinds(commented)
        == C.EFFECTIVE_ADJACENCY_EDGE_KINDS
    )

    dynamic = _adjacency_sql().replace(
        "cn.node_kind='class'",
        "cn.node_kind = ANY(v_runtime_node_kinds)",
    )
    with pytest.raises(
        C.SchemaInventoryParseError, match="unsupported node_kind"
    ):
        C.parse_effective_adjacency_node_kinds(dynamic)

    semantic_identity = _adjacency_sql().replace(
        "cn.node_kind='class'",
        "cn.node_kind='class' AND "
        "core._node_semantic_key("
        "cn.node_kind,cn.node_id,cn.path,cn.name,cn.canonical_key"
        ") IS NOT NULL",
    )
    assert (
        C.parse_effective_adjacency_node_kinds(semantic_identity)
        == C.EFFECTIVE_ADJACENCY_NODE_KINDS
    )


def test_sql_inventory_rejects_loss_and_stage_drift() -> None:
    missing_node = C.EXTRACTOR_NODE_KIND_ORDER[:-1]
    missing_edge = C.EXTRACTOR_EDGE_KIND_ORDER[:-1]
    acceptance = C.parse_sql_schema_acceptance(
        _core_sql(missing_node, missing_edge),
        _gate_sql(
            full_nodes=missing_node,
            full_edges=missing_edge,
            patch_nodes=C.EXTRACTOR_NODE_KIND_ORDER,
            patch_edges=C.EXTRACTOR_EDGE_KIND_ORDER,
        ),
    )
    report = _inventory(acceptance)
    assert not report.ok
    assert report.persistence_node_losses == {"role_feature"}
    assert report.persistence_edge_losses == {"queries_col"}
    assert any("drift" in error for error in report.errors)
    with pytest.raises(C.SchemaDriftError, match="role_feature"):
        C.assert_schema_inventory_aligned(report)


def test_inventory_cli_has_machine_output_and_nonzero_on_loss(tmp_path: Path) -> None:
    core = tmp_path / "core.sql"
    gate = tmp_path / "gate.sql"
    core.write_text(
        _core_sql(C.EXTRACTOR_NODE_KIND_ORDER[:-1], C.EXTRACTOR_EDGE_KIND_ORDER),
        encoding="utf-8",
    )
    gate.write_text(_gate_sql(), encoding="utf-8")
    adjacency = tmp_path / "adjacency.sql"
    adjacency.write_text(_adjacency_sql(), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/graph_schema_inventory.py"),
            "--core-sql",
            str(core),
            "--gate-sql",
            str(gate),
            "--adjacency-sql",
            str(adjacency),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["persistence_node_losses"] == ["role_feature"]
    assert "drops node kinds" in "\n".join(payload["errors"])
