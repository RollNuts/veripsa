#!/usr/bin/env python3
"""Real-PostgreSQL gate for first-class graph uncertainty.

Proves that parser/extractor failures and ambiguous local references survive
full and incremental persistence, affect the canonical graph hash, are
excluded from effective adjacency, and yield Unknown only when no stronger
serialize/warn evidence exists.
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import cg_schema_contract as C  # noqa: E402
import code_graph_extract as X  # noqa: E402
import schema_contract as SC  # noqa: E402


DB = "veripsa_graph_uncertainty_" + str(os.getpid())
REPO = "graph/uncertainty"
COLLISION_REPO = "graph/resource-key-collision"
BRANCH = "main"


def app(sql: str, args=()):
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def admin_rows(sql: str, args=()):
    conn = psycopg2.connect(f"postgresql://localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def admin_exec(sql: str, args=()) -> None:
    conn = psycopg2.connect(f"postgresql://localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, args)
    finally:
        conn.close()


def obj(value):
    return value if isinstance(value, (dict, list)) else json.loads(value)


def graph(*, uncertain: bool) -> dict:
    nodes = [
        {
            "id": "failed.py",
            "kind": "file",
            "path": "failed.py",
            "language": "python",
            **({"analysis_status": "failed"} if uncertain else {}),
        },
        {
            "id": "analysis_ambiguous.py",
            "kind": "file",
            "path": "analysis_ambiguous.py",
            "language": "python",
            **({"analysis_status": "ambiguous"} if uncertain else {}),
        },
        {
            "id": "incomplete.py",
            "kind": "file",
            "path": "incomplete.py",
            "language": "javascript",
            **({"analysis_status": "incomplete"} if uncertain else {}),
        },
        {
            "id": "ambiguous_ref.py",
            "kind": "file",
            "path": "ambiguous_ref.py",
            "language": "python",
        },
        {
            "id": "unresolved_ref.py",
            "kind": "file",
            "path": "unresolved_ref.py",
            "language": "python",
        },
        {"id": "target.py", "kind": "file", "path": "target.py", "language": "python"},
        {"id": "warn.py", "kind": "file", "path": "warn.py", "language": "python"},
        {"id": "peer.py", "kind": "file", "path": "peer.py", "language": "python"},
        {"id": "clear.py", "kind": "file", "path": "clear.py", "language": "python"},
    ]
    if not uncertain:
        nodes.append(
            {
                "id": "missing.py",
                "kind": "file",
                "path": "missing.py",
                "language": "python",
            }
        )
    edges = [
        {
            "src": "ambiguous_ref.py",
            "dst": "target.py",
            "kind": "imports",
            **({"reference_status": "ambiguous"} if uncertain else {}),
        },
        (
            {
                "src": "unresolved_ref.py",
                "dst": "./missing",
                "kind": "imports",
                "reference_status": "unresolved",
            }
            if uncertain
            else {
                "src": "unresolved_ref.py",
                "dst": "missing.py",
                "kind": "imports",
            }
        ),
        {"src": "warn.py", "dst": "peer.py", "kind": "imports"},
    ]
    return {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {
            "schema_contract_version": C.SCHEMA_CONTRACT_VERSION,
        },
        "nodes": nodes,
        "edges": edges,
    }


def resource_key_collision_graph() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        with open(
            os.path.join(directory, "schema.sql"), "w", encoding="utf-8"
        ) as handle:
            handle.write("CREATE TABLE database_url (id integer);\n")
        with open(
            os.path.join(directory, "settings.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump({"database_url": "configured"}, handle)
        with open(
            os.path.join(directory, "app.py"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                "import os\n"
                "database_url = os.getenv('database_url')\n"
            )
        return X.build_graph(directory, repo=COLLISION_REPO)


def version(repo: str = REPO) -> dict:
    return obj(app("SELECT core.coordinate_graph_sha(%s,%s)", (repo, BRANCH)))


def patch(payload: dict, changed_paths: list[str], commit_sha: str) -> dict:
    base = version()
    subgraph = copy.deepcopy(payload)
    subgraph["expected_base_sha"] = base["commit_sha"]
    subgraph["expected_base_revision"] = base["graph_revision"]
    return obj(
        app(
            "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
            (
                json.dumps(subgraph),
                REPO,
                BRANCH,
                changed_paths,
                [],
                commit_sha,
            ),
        )
    )


def claim(
    change_id: str,
    path: str,
    agent: str,
    *,
    repo: str = REPO,
) -> None:
    app(
        "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
        (f"{change_id}:{path}", path, repo, BRANCH, agent),
    )


def main() -> int:
    checks: list[tuple[str, bool]] = []

    boot = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if boot.returncode:
        print(boot.stderr[-1200:])
        return 1

    # Simulate a populated generation-15 database that received the original
    # ambiguous-only expand-first check. Reapplying the exact schema must widen
    # the existing named constraint; ADD-IF-MISSING would silently preserve the
    # stale wall and reject every new unresolved edge in production.
    admin_exec(
        "ALTER TABLE core.code_edge "
        "DROP CONSTRAINT code_edge_reference_status_check; "
        "ALTER TABLE core.code_edge "
        "ADD CONSTRAINT code_edge_reference_status_check "
        "CHECK (reference_status IS NULL OR reference_status='ambiguous')"
    )
    repair = subprocess.run(
        [
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-d",
            DB,
            "-f",
            "db/schema/20_core.sql",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    repaired_constraint = admin_rows(
        "SELECT pg_get_constraintdef(oid),convalidated "
        "FROM pg_constraint "
        "WHERE conrelid='core.code_edge'::regclass "
        "AND conname='code_edge_reference_status_check'"
    )
    checks.append(
        (
            "hot schema reapply widens the populated ambiguous-only edge check",
            repair.returncode == 0
            and len(repaired_constraint) == 1
            and repaired_constraint[0][1] is True
            and "'ambiguous'" in repaired_constraint[0][0]
            and "'unresolved'" in repaired_constraint[0][0],
        )
    )

    inventory = obj(app("SELECT core.graph_schema_inventory()"))
    checks.append(
        (
            "schema inventory publishes the closed uncertainty enums",
            inventory.get("schema_contract_version") == C.SCHEMA_CONTRACT_VERSION
            and set(inventory.get("node_analysis_statuses") or [])
            == C.NODE_ANALYSIS_STATUSES
            and set(inventory.get("edge_reference_statuses") or [])
            == C.EDGE_REFERENCE_STATUSES
            and inventory.get("effective_adjacency_reference_status")
            == "resolved_only",
        )
    )
    fallback_codes = set(inventory.get("observability_fallback_reason_codes") or [])
    checks.append(
        (
            "schema inventory accepts every fixed uncertainty rebuild reason",
            {
                "stored_graph_uncertainty",
                "ambiguous_reference_detected",
                "extractor_file_failed",
                "extractor_file_incomplete",
            }
            <= fallback_codes,
        )
    )

    contract = SC.check_schema_contract(
        f"postgresql://veripsa_app@localhost/{DB}"
    )
    checks.append(
        (
            "boot-time runtime contract sees both status columns and exact checks",
            contract.healthy and not contract.violations,
        )
    )
    # Count one resolved-only guard for every effective code_edge consumer.
    # #982 removed two complete (and truth-table-redundant) edge consumers
    # from _claim_adjacency and one from _dampened_adjacency; no surviving
    # consumer lost its reference_status guard.
    effective_filter_minimums = {
        "_claim_adjacency": 10,
        "_cross_repo_adjacency": 2,
        "_dampened_adjacency": 6,
        "cross_tenant_contract_surface": 2,
        "main_impact_surface": 1,
        "split_candidates": 1,
        "graph_insights_for_installation": 1,
    }
    effective_defs = dict(
        admin_rows(
            "SELECT p.proname,pg_get_functiondef(p.oid) "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' AND p.proname=ANY(%s)",
            (list(effective_filter_minimums),),
        )
    )
    checks.append(
        (
            "every effective SQL graph consumer admits resolved edges only",
            set(effective_defs) == set(effective_filter_minimums)
            and all(
                len(
                    re.findall(
                        r"\breference_status\s+IS\s+NULL\b",
                        effective_defs[name],
                        re.IGNORECASE,
                    )
                )
                >= minimum
                for name, minimum in effective_filter_minimums.items()
            ),
        )
    )

    py_valid = graph(uncertain=True)
    C.assert_valid_graph(py_valid, persisted=True)
    bad_node = graph(uncertain=True)
    bad_node["nodes"][0]["analysis_status"] = "partial"
    bad_edge = graph(uncertain=True)
    bad_edge["edges"][0]["reference_status"] = "guessed"
    checks.append(
        (
            "Python graph contract rejects status tokens outside the closed enums",
            any(i.code == "node_analysis_status" for i in C.validate_graph(bad_node))
            and any(i.code == "edge_reference_status" for i in C.validate_graph(bad_edge)),
        )
    )

    full = obj(
        app(
            "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
            (json.dumps(py_valid), REPO, BRANCH, "a" * 40),
        )
    )
    full_hash = version()["graph_hash"]
    rows = admin_rows(
        "SELECT path,analysis_status FROM core.code_node "
        "WHERE repo=%s AND analysis_status IS NOT NULL ORDER BY path",
        (REPO,),
    )
    edge_rows = admin_rows(
        "SELECT src,dst,reference_status FROM core.code_edge "
        "WHERE repo=%s AND reference_status IS NOT NULL ORDER BY src,dst",
        (REPO,),
    )
    checks.append(
        (
            "governed full ingest persists every closed node/edge uncertainty",
            full.get("ok") is True
            and rows
            == [
                ("analysis_ambiguous.py", "ambiguous"),
                ("failed.py", "failed"),
                ("incomplete.py", "incomplete"),
            ]
            and edge_rows
            == [
                ("ambiguous_ref.py", "target.py", "ambiguous"),
                ("unresolved_ref.py", "./missing", "unresolved"),
            ],
        )
    )

    checks.append(
        (
            "coordinate read exposes stored uncertainty without making it stale",
            version().get("has_graph_uncertainty") is True
            and version().get("extractor_version") == C.EXTRACTOR_VERSION,
        )
    )

    touched = [
        "failed.py",
        "analysis_ambiguous.py",
        "incomplete.py",
        "ambiguous_ref.py",
        "unresolved_ref.py",
        "missing.py",
    ]
    normal = graph(uncertain=False)
    normal_slice = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {
            "schema_contract_version": C.SCHEMA_CONTRACT_VERSION,
        },
        "nodes": [n for n in normal["nodes"] if n["path"] in touched],
        "edges": [e for e in normal["edges"] if e["src"] in touched],
    }
    normal_patch = patch(normal_slice, touched, "b" * 40)
    normal_hash = version()["graph_hash"]
    exact_resolution_rows = admin_rows(
        "SELECT src,dst,reference_status FROM core.code_edge "
        "WHERE repo=%s AND src='unresolved_ref.py'",
        (REPO,),
    )
    checks.append(
        (
            "exact later target resolution clears source uncertainty and changes hash",
            normal_patch.get("ok") is True
            and normal_hash != full_hash
            and version().get("has_graph_uncertainty") is False
            and exact_resolution_rows
            == [("unresolved_ref.py", "missing.py", None)]
            and not admin_rows(
                "SELECT 1 FROM core.code_node WHERE repo=%s AND analysis_status IS NOT NULL "
                "UNION ALL SELECT 1 FROM core.code_edge WHERE repo=%s "
                "AND reference_status IS NOT NULL",
                (REPO, REPO),
            ),
        )
    )

    uncertain_slice = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {
            "schema_contract_version": C.SCHEMA_CONTRACT_VERSION,
        },
        "nodes": [n for n in py_valid["nodes"] if n["path"] in touched],
        "edges": [e for e in py_valid["edges"] if e["src"] in touched],
    }
    uncertain_patch = patch(uncertain_slice, touched, "c" * 40)
    patch_hash = version()["graph_hash"]
    checks.append(
        (
            "full and incremental persistence produce the same uncertainty-aware hash",
            uncertain_patch.get("ok") is True
            and patch_hash == full_hash
            and version().get("has_graph_uncertainty") is True,
        )
    )

    before_bad = version()
    bad_full_rejected = False
    try:
        app(
            "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
            (json.dumps(bad_edge), REPO, BRANCH, "d" * 40),
        )
    except psycopg2.Error as exc:
        bad_full_rejected = exc.pgcode == "22023"

    bad_patch = copy.deepcopy(uncertain_slice)
    bad_patch["nodes"][0]["analysis_status"] = "partial"
    bad_patch_rejected = False
    try:
        patch(bad_patch, touched, "e" * 40)
    except psycopg2.Error as exc:
        bad_patch_rejected = exc.pgcode == "22023"
    checks.append(
        (
            "full and incremental writers atomically reject invalid uncertainty enums",
            bad_full_rejected
            and bad_patch_rejected
            and version() == before_bad,
        )
    )

    # A canonical resource key is not globally typed. A table and config_key
    # named database_url must persist as two first-class nodes, while every
    # resource edge to that untyped coordinate remains inert ambiguity.
    collision_graph = resource_key_collision_graph()
    C.assert_valid_graph(
        collision_graph,
        persisted=True,
        require_resource_metadata=True,
    )
    collision_ingest = obj(
        app(
            "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
            (
                json.dumps(collision_graph),
                COLLISION_REPO,
                BRANCH,
                "f" * 40,
            ),
        )
    )
    collision_resources = admin_rows(
        "SELECT node_kind,canonical_key FROM core.code_node "
        "WHERE repo=%s AND canonical_key='database_url' "
        "ORDER BY node_kind",
        (COLLISION_REPO,),
    )
    collision_edges = admin_rows(
        "SELECT src,dst,edge_kind,reference_status FROM core.code_edge "
        "WHERE repo=%s AND dst='database_url' "
        "ORDER BY src,edge_kind",
        (COLLISION_REPO,),
    )
    collision_documents = admin_rows(
        "SELECT path,analysis_status FROM core.code_node "
        "WHERE repo=%s AND node_kind='file' "
        "AND path=ANY(%s) ORDER BY path",
        (COLLISION_REPO, ["app.py", "schema.sql"]),
    )
    checks.append(
        (
            "cross-kind canonical collision persists both resources and only ambiguous edges",
            collision_ingest.get("ok") is True
            and collision_resources
            == [
                ("config_key", "database_url"),
                ("table", "database_url"),
            ]
            and collision_edges
            == [
                ("app.py", "database_url", "reads_config", "ambiguous"),
                ("schema.sql", "database_url", "alters", "ambiguous"),
            ]
            and collision_documents
            == [
                ("app.py", "ambiguous"),
                ("schema.sql", "ambiguous"),
            ]
            and version(COLLISION_REPO).get("has_graph_uncertainty") is True,
        )
    )

    claim(
        "PR-COLLISION-APP",
        "app.py",
        "collisionapp",
        repo=COLLISION_REPO,
    )
    claim(
        "PR-COLLISION-SCHEMA",
        "schema.sql",
        "collisionschema",
        repo=COLLISION_REPO,
    )
    collision_adjacency = admin_rows(
        "SELECT f,nbr,dir FROM core._claim_adjacency(%s,%s,%s) "
        "ORDER BY f,nbr,dir",
        ("ACCT-DEMO", COLLISION_REPO, BRANCH),
    )
    collision_surface = obj(
        app(
            "SELECT core.main_impact_surface(%s,%s)",
            (COLLISION_REPO, BRANCH),
        )
    )
    collision_by_change = {
        row["change_id"]: row
        for row in collision_surface.get("changes", [])
    }
    collision_app = collision_by_change.get("PR-COLLISION-APP", {})
    collision_schema = collision_by_change.get("PR-COLLISION-SCHEMA", {})
    checks.append(
        (
            "cross-kind collision is absent from effective adjacency and both sources are Unknown",
            collision_adjacency == []
            and collision_app.get("verdict") == "unknown"
            and collision_schema.get("verdict") == "unknown"
            and not collision_app.get("contested_with")
            and not collision_schema.get("contested_with")
            and "app.py" in (collision_app.get("unknown_paths") or [])
            and "schema.sql" in (collision_schema.get("unknown_paths") or [])
            and {
                "path": "app.py",
                "reason": "reference_ambiguous",
            }
            in (collision_app.get("graph_uncertainty") or [])
            and {
                "path": "schema.sql",
                "reason": "reference_ambiguous",
            }
            in (collision_schema.get("graph_uncertainty") or []),
        )
    )

    claim("PR-TARGET", "target.py", "targetdev")
    claim("PR-AMBIGREF", "ambiguous_ref.py", "ambigdev")
    claim("PR-UNRESOLVED", "unresolved_ref.py", "unresolveddev")
    claim("PR-PEER", "peer.py", "peerdev")
    claim("PR-WARN", "warn.py", "warndev")
    claim("PR-ANALYSIS", "analysis_ambiguous.py", "analysisdev")
    claim("PR-CLEAR", "clear.py", "cleardev")

    # Failed/incomplete rows already exist in the coordinate, but neither path
    # is in-flight yet. They must not permanently poison unrelated work.
    bounded_surface = obj(
        app("SELECT core.main_impact_surface(%s,%s)", (REPO, BRANCH))
    )
    by_change = {
        row["change_id"]: row for row in bounded_surface.get("changes", [])
    }
    ambig = by_change.get("PR-AMBIGREF", {})
    unresolved = by_change.get("PR-UNRESOLVED", {})
    analysis = by_change.get("PR-ANALYSIS", {})
    target = by_change.get("PR-TARGET", {})
    warn = by_change.get("PR-WARN", {})
    clear = by_change.get("PR-CLEAR", {})
    checks.append(
        (
            "ambiguous edge is excluded from adjacency and its source falls back to Unknown",
            ambig.get("verdict") == "unknown"
            and not ambig.get("contested_with")
            and "ambiguous_ref.py" in (ambig.get("unknown_paths") or [])
            and {
                "path": "ambiguous_ref.py",
                "reason": "reference_ambiguous",
            }
            in (ambig.get("graph_uncertainty") or []),
        )
    )
    checks.append(
        (
            "unresolved local edge is inert and its source falls back to Unknown",
            unresolved.get("verdict") == "unknown"
            and not unresolved.get("contested_with")
            and "unresolved_ref.py"
            in (unresolved.get("unknown_paths") or [])
            and {
                "path": "unresolved_ref.py",
                "reason": "reference_unresolved",
            }
            in (unresolved.get("graph_uncertainty") or []),
        )
    )
    checks.append(
        (
            "bounded analysis ambiguity remains endpoint-local",
            analysis.get("verdict") == "unknown"
            and {
                "path": "analysis_ambiguous.py",
                "reason": "analysis_ambiguous",
            }
            in (analysis.get("graph_uncertainty") or [])
            and {
                "path": "analysis_ambiguous.py",
                "reason": "inflight_peer_analysis_incomplete",
            }
            not in (analysis.get("graph_uncertainty") or []),
        )
    )
    checks.append(
        (
            "ambiguous reference destination also falls back to Unknown",
            target.get("verdict") == "unknown"
            and not target.get("contested_with")
            and "target.py" in (target.get("unknown_paths") or [])
            and {
                "path": "target.py",
                "reason": "reference_ambiguous",
            }
            in (target.get("graph_uncertainty") or []),
        )
    )
    checks.append(
        (
            "non-in-flight extraction loss does not poison the current set",
            warn.get("verdict") == "warn"
            and warn.get("contested_with")
            and clear.get("verdict") == "clear",
        )
    )

    # A failed/incomplete document now enters the live set. Extraction loss is
    # unbounded, so every otherwise-Clear in-flight change becomes Unknown.
    # Bounded ambiguity stays endpoint-local and stronger Warn still wins.
    claim("PR-FAILED", "failed.py", "faildev")
    wall_surface = obj(
        app("SELECT core.main_impact_surface(%s,%s)", (REPO, BRANCH))
    )
    wall_by_change = {
        row["change_id"]: row for row in wall_surface.get("changes", [])
    }
    failed = wall_by_change.get("PR-FAILED", {})
    target_after_wall = wall_by_change.get("PR-TARGET", {})
    warn_after_wall = wall_by_change.get("PR-WARN", {})
    clear_after_wall = wall_by_change.get("PR-CLEAR", {})
    checks.append(
        (
            "one actual in-flight failed path preserves its precise reason",
            failed.get("verdict") == "unknown"
            and failed.get("graph_uncertainty")
            == [{"path": "failed.py", "reason": "analysis_failed"}],
        )
    )
    checks.append(
        (
            "in-flight extraction loss promotes the opposite isolated path with a visible reason",
            clear_after_wall.get("verdict") == "unknown"
            and clear_after_wall.get("unknown_paths") == ["clear.py"]
            and clear_after_wall.get("graph_uncertainty")
            == [
                {
                    "path": "clear.py",
                    "reason": "inflight_peer_analysis_incomplete",
                }
            ],
        )
    )
    checks.append(
        (
            "the wall preserves bounded endpoint reasons and stronger Warn precedence",
            target_after_wall.get("verdict") == "unknown"
            and target_after_wall.get("graph_uncertainty")
            == [{"path": "target.py", "reason": "reference_ambiguous"}]
            and warn_after_wall.get("verdict") == "warn"
            and warn_after_wall.get("contested_with")
            and not warn_after_wall.get("unknown_paths")
            and not warn_after_wall.get("graph_uncertainty"),
        )
    )

    claim("PR-INCOMPLETE", "incomplete.py", "incompletedev")
    with_incomplete = obj(
        app("SELECT core.main_impact_surface(%s,%s)", (REPO, BRANCH))
    )
    incomplete = {
        row["change_id"]: row for row in with_incomplete.get("changes", [])
    }.get("PR-INCOMPLETE", {})
    checks.append(
        (
            "an actual incomplete path also preserves its precise reason",
            incomplete.get("verdict") == "unknown"
            and incomplete.get("graph_uncertainty")
            == [{"path": "incomplete.py", "reason": "analysis_incomplete"}],
        )
    )

    claim("PR-SERIAL", "failed.py", "serialdev")
    after_wait = obj(
        app("SELECT core.main_impact_surface(%s,%s)", (REPO, BRANCH))
    )
    serial = {
        row["change_id"]: row for row in after_wait.get("changes", [])
    }.get("PR-SERIAL", {})
    checks.append(
        (
            "strong direct collision remains Serialize ahead of uncertainty fallback",
            serial.get("verdict") == "serialize"
            and "failed.py" in (serial.get("unknown_paths") or []),
        )
    )

    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and bool(passed)
    print("GRAPH UNCERTAINTY CONTRACT DB GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
