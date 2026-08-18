#!/usr/bin/env python3
"""Print and enforce the extractor/persistence graph schema inventory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cg_schema_contract import (  # noqa: E402
    SchemaInventoryParseError,
    contract_self_check,
    schema_inventory_from_paths,
)


def _csv(values: list[str]) -> str:
    return ", ".join(values) if values else "(none)"


def _print_human(report: dict) -> None:
    print(f"Graph Schema Inventory (contract v{report['schema_contract_version']})")
    print(f"Status: {'PASS' if report['ok'] else 'FAIL'}")
    print()
    print(f"Extractor node kinds ({len(report['extractor_node_kinds'])}):")
    print(f"  {_csv(report['extractor_node_kinds'])}")
    print(f"Extractor edge kinds ({len(report['extractor_edge_kinds'])}):")
    print(f"  {_csv(report['extractor_edge_kinds'])}")
    print()
    print("DB node kinds by acceptance stage:")
    for stage, kinds in report["db_node_kinds_by_stage"].items():
        print(f"  {stage}: {_csv(kinds)}")
    print("DB edge kinds by acceptance stage:")
    for stage, kinds in report["db_edge_kinds_by_stage"].items():
        print(f"  {stage}: {_csv(kinds)}")
    print()
    print("Persistence losses:")
    print(f"  nodes: {_csv(report['persistence_node_losses'])}")
    print(f"  edges: {_csv(report['persistence_edge_losses'])}")
    print("Declared effective-adjacency Node kinds:")
    print(f"  {_csv(report['effective_adjacency_node_kinds'])}")
    print("Actual _claim_adjacency Node-kind predicates:")
    print(f"  {_csv(report['actual_adjacency_node_kinds'])}")
    print("Declared effective-adjacency Edge kinds:")
    print(f"  {_csv(report['effective_adjacency_edge_kinds'])}")
    print("Actual _claim_adjacency Edge-kind predicates:")
    print(f"  {_csv(report['actual_adjacency_edge_kinds'])}")
    print("Structural-only edges:")
    print(f"  {_csv(report['structural_only_edge_kinds'])}")
    print("Evidence-only edges:")
    print(f"  {_csv(report['evidence_only_edge_kinds'])}")
    print()
    print("Substrate mapping:")
    for name, contract in report["substrates"].items():
        print(
            f"  {name}: nodes=[{_csv(contract['node_kinds'])}] "
            f"edges=[{_csv(contract['edge_kinds'])}] "
            f"extractor={contract['extractor']}"
        )
    if report["errors"]:
        print()
        print("Errors:")
        for error in report["errors"]:
            print(f"  - {error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the authoritative graph schema contract with PostgreSQL "
            "CHECK constraints and full/incremental ingest filters."
        )
    )
    parser.add_argument(
        "--core-sql",
        type=Path,
        default=ROOT / "db/schema/20_core.sql",
        help="path containing code_node/code_edge CHECK constraints",
    )
    parser.add_argument(
        "--gate-sql",
        type=Path,
        default=ROOT / "db/schema/30_gate.sql",
        help="path containing full and incremental ingest functions",
    )
    parser.add_argument(
        "--adjacency-sql",
        type=Path,
        default=ROOT / "db/schema/70_social.sql",
        help="path containing core._claim_adjacency",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one machine-readable JSON object",
    )
    args = parser.parse_args(argv)

    self_errors = contract_self_check()
    if self_errors:
        payload = {
            "ok": False,
            "parse_error": None,
            "contract_errors": list(self_errors),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print("Graph Schema Inventory")
            print("Status: FAIL")
            for error in self_errors:
                print(f"- contract: {error}")
        return 1

    try:
        report = schema_inventory_from_paths(
            args.core_sql, args.gate_sql, args.adjacency_sql
        )
    except (OSError, SchemaInventoryParseError) as exc:
        payload = {
            "ok": False,
            "parse_error": str(exc),
            "contract_errors": [],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print("Graph Schema Inventory")
            print("Status: FAIL")
            print(f"- unable to determine DB acceptance exactly: {exc}")
        return 2

    payload = report.as_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        _print_human(payload)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
