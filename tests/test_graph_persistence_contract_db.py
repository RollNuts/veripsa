#!/usr/bin/env python3
"""Real-PostgreSQL gate for the cg4 graph persistence contract.

Proves through the governed App write path that:
  * all 20 node kinds and all 8 edge kinds persist;
  * an unexpected per-file extractor failure persists one canonical bare file;
  * resource metadata and column evidence survive full + patch writes;
  * persisted SHA-256 hashes are coordinate-independent and full/patch equivalent;
  * schema-first cg3 FULL writers remain behind cg4 while every PATCH is current-only;
  * unknown kinds fail before replacing a valid coordinate;
  * resource catalog path classes and config-only baseline paths are exposed.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import cg_schema_contract as C  # noqa: E402
import code_graph_extract as X  # noqa: E402
import ingest as I  # noqa: E402
import schema_contract as SC  # noqa: E402


DB = "veripsa_graphpersist_" + str(os.getpid())
REPO = "graph/persisted"
TRUTH_REPO = "graph/persisted-truth"
BRANCH = "main"


def app(sql, args=()):
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def admin_rows(sql, args=()):
    conn = psycopg2.connect(f"postgresql://localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def admin_exec(sql, args=()):
    conn = psycopg2.connect(f"postgresql://localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, args)
    finally:
        conn.close()


def admin_explain(sql, args=()):
    conn = psycopg2.connect(f"postgresql://localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET LOCAL enable_seqscan=off")
            cur.execute("EXPLAIN (FORMAT JSON, COSTS OFF) " + sql, args)
            return cur.fetchone()[0]
    finally:
        conn.close()


def plan_index_names(value):
    names = set()
    if isinstance(value, dict):
        if value.get("Index Name"):
            names.add(value["Index Name"])
        for child in value.values():
            names.update(plan_index_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(plan_index_names(child))
    return names


def obj(value):
    return value if isinstance(value, (dict, list)) else json.loads(value)


def coordinate_snapshot(repo=REPO):
    """Exact persisted state used to prove rejected writes are atomic."""
    return {
        "version": obj(app("SELECT core.coordinate_graph_sha(%s,%s)", (repo, BRANCH))),
        "nodes": admin_rows(
            "SELECT node_id,node_kind,path,content_hash FROM core.code_node "
            "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s "
            "ORDER BY node_id,node_kind,path",
            (repo, BRANCH),
        ),
        "edges": admin_rows(
            "SELECT src,dst,edge_kind FROM core.code_edge "
            "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s "
            "ORDER BY src,dst,edge_kind",
            (repo, BRANCH),
        ),
    }


def account_graph_version(account, repo):
    """Read one FORCE-RLS graph_version row under its exact tenant pin."""
    rows = admin_rows(
        "SELECT set_config('core.current_account',%s,true); "
        "SELECT jsonb_build_object("
        "'graph_hash',graph_hash,'observability',observability,"
        "'extractor_version',extractor_version"
        ") FROM core.graph_version "
        "WHERE account_id=%s AND repo=%s AND branch=%s",
        (account, account, repo, BRANCH),
    )
    return obj(rows[0][0]) if rows else {}


def resource(node: dict) -> dict:
    return C.enrich_resource_node(node, repo=REPO)


def repo_tarball(files: dict[str, str]) -> bytes:
    """Minimal immutable GitHub-style tarball for real `_full_ingest` tests."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        root = tarfile.TarInfo("repo")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        tf.addfile(root)
        for path, text in sorted(files.items()):
            body = text.encode("utf-8")
            info = tarfile.TarInfo("repo/" + path)
            info.mode = 0o644
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def graph() -> dict:
    nodes = [
        {"id": "app.py", "kind": "file", "path": "app.py", "language": "python",
         "content_hash": "a" * 40},
        {"id": "peer.py", "kind": "file", "path": "peer.py", "language": "python"},
        {"id": "schema.sql", "kind": "file", "path": "schema.sql", "language": "sql"},
        {"id": "column-only-def.sql", "kind": "file", "path": "column-only-def.sql",
         "language": "sql"},
        {"id": "column-only-query.py", "kind": "file", "path": "column-only-query.py",
         "language": "python"},
        {"id": "app.py::run", "kind": "def", "path": "app.py", "name": "run", "language": "python"},
        {"id": "app.py::App", "kind": "class", "path": "app.py", "name": "App", "language": "python"},
        resource({"id": "table::orders", "kind": "table", "path": "schema.sql",
                  "name": "orders", "language": "sql"}),
        resource({"id": "column::orders.id", "kind": "column", "path": "schema.sql",
                  "name": "id", "table": "orders", "language": "sql"}),
        {"id": "package.json", "kind": "config_file", "path": "package.json", "language": "config"},
        resource({"id": "cfgkey::package.json::DATABASE_URL", "kind": "config_key",
                  "path": "package.json", "name": "DATABASE_URL", "language": "config"}),
        resource({"id": "iac_resource::infra::aws_s3_bucket.logs", "kind": "iac_resource",
                  "path": "infra/main.tf", "name": "aws_s3_bucket.logs", "language": "terraform"}),
        resource({"id": "k8s_resource::k8s::service::default::api", "kind": "k8s_resource",
                  "path": "k8s/service.yaml", "name": "Service/default/api", "language": "kubernetes"}),
        resource({"id": "api_type::Order", "kind": "api_type", "path": "schema.graphql",
                  "name": "Order", "language": "graphql"}),
        resource({"id": "api_message::OrderRequest", "kind": "api_message", "path": "api.proto",
                  "name": "OrderRequest", "language": "protobuf"}),
        resource({"id": "api_service::OrderService", "kind": "api_service", "path": "api.proto",
                  "name": "OrderService", "language": "protobuf"}),
        resource({"id": "api_operation::getOrder", "kind": "api_operation", "path": "openapi.yaml",
                  "name": "getOrder", "language": "openapi"}),
        resource({"id": "api_schema::Order", "kind": "api_schema", "path": "openapi.yaml",
                  "name": "Order", "language": "openapi"}),
        resource({"id": "ci_script::.::build", "kind": "ci_script", "path": "package.json",
                  "name": "build", "language": "ci"}),
        resource({"id": "app_command::refresh", "kind": "app_command", "path": "src-tauri/lib.rs",
                  "name": "refresh", "language": "tauri"}),
        resource({"id": "job_task::celery::billing.charge", "kind": "job_task", "path": "tasks.py",
                  "name": "billing.charge", "language": "celery"}),
        resource({"id": "job_queue::bullmq::emails", "kind": "job_queue", "path": "worker.ts",
                  "name": "emails", "language": "bullmq"}),
        resource({"id": "sibling_stem::tsx::ui::card", "kind": "sibling_stem",
                  "path": "ui/card.tsx", "name": "card", "language": "tsx"}),
        resource({"id": "role_feature::api::auth::admin::user::reset", "kind": "role_feature",
                  "path": "auth/admin/reset.py", "name": "reset", "language": "api"}),
    ]
    edges = [
        {"src": "app.py", "dst": "app.py::run", "kind": "contains"},
        {"src": "app.py", "dst": "run", "kind": "calls"},
        {"src": "app.py", "dst": "peer.py", "kind": "imports"},
        {"src": "peer.py", "dst": "app.py", "kind": "imports"},
        {"src": "schema.sql", "dst": "orders", "kind": "alters"},
        {"src": "app.py", "dst": "orders", "kind": "queries"},
        {"src": "app.py", "dst": "DATABASE_URL", "kind": "reads_config"},
        {"src": "schema.sql", "dst": "orders.id", "kind": "alters_col"},
        {"src": "app.py", "dst": "orders.id", "kind": "queries_col"},
        {"src": "tasks.py", "dst": "job_task::celery::billing.charge", "kind": "alters"},
        {"src": "app.py", "dst": "job_task::celery::billing.charge", "kind": "queries"},
        {"src": "worker.ts", "dst": "job_queue::bullmq::emails", "kind": "alters"},
        {"src": "app.py", "dst": "job_queue::bullmq::emails", "kind": "queries"},
        {"src": "package.json", "dst": "ci_script::.::build", "kind": "alters"},
        {
            "src": ".github/workflows/ci.yml",
            "dst": "ci_script::.::build",
            "kind": "queries",
        },
        # Deliberately no table-level counterpart: these prove the actual
        # adjacency query does not consume column evidence yet.
        {"src": "column-only-def.sql", "dst": "audit.marker", "kind": "alters_col"},
        {"src": "column-only-query.py", "dst": "audit.marker", "kind": "queries_col"},
        {"src": "ui/card.tsx", "dst": "sibling_stem::tsx::ui::card", "kind": "queries"},
        {"src": "ui/card.test.tsx", "dst": "sibling_stem::tsx::ui::card", "kind": "queries"},
    ]
    metrics = C.collect_graph_metrics(
        {"nodes": nodes, "edges": edges},
        input_paths=(
            node["path"]
            for node in nodes
            if node["kind"] in {"file", "config_file"}
        ),
        unresolved_references=2,
        ambiguous_references=1,
        fallback_full_rebuild_reasons=("no_stored_graph_baseline",),
    ).as_dict()
    metrics.update({
        "mode": "full",
        "schema_contract_version": C.SCHEMA_CONTRACT_VERSION,
        "ambiguity_detection_scope": (
            "retained multi-definer resources, emitted canonical-key "
            "collisions, and local import candidate ambiguity"
        ),
    })
    return {
        "extractor_version": C.EXTRACTOR_VERSION,
        "nodes": nodes,
        "edges": edges,
        "metrics": metrics,
    }


def main() -> int:
    checks: list[tuple[str, bool]] = []

    def ck(label, condition):
        checks.append((label, bool(condition)))

    boot = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if boot.returncode:
        print("bootstrap failed:\n", boot.stderr[-1200:])
        return 1

    helper_privileges = admin_rows(
        "SELECT "
        "has_function_privilege("
        "'veripsa_writer',"
        "'core._assert_unique_graph_identities(jsonb,jsonb)',"
        "'EXECUTE'),"
        "has_function_privilege("
        "'veripsa_reader',"
        "'core._assert_unique_graph_identities(jsonb,jsonb)',"
        "'EXECUTE'),"
        "has_function_privilege("
        "'veripsa_app',"
        "'core._assert_unique_graph_identities(jsonb,jsonb)',"
        "'EXECUTE')"
    )[0]
    ck(
        "private duplicate-identity helper is not directly executable by "
        "writer, reader, or App roles",
        helper_privileges == (False, False, False),
    )

    base = graph()
    full = obj(app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(base), REPO, BRANCH, "a" * 40),
    ))
    ck("full ingest succeeds and returns a 64-hex persisted hash",
       full.get("ok") is True
       and len(full.get("graph_hash") or "") == 64
       and set(full.get("graph_hash") or "") <= set("0123456789abcdef"))

    node_uncertainty_plan = admin_explain(
        "SELECT 1 FROM core.code_node "
        "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s "
        "AND analysis_status IS NOT NULL LIMIT 1",
        (REPO, BRANCH),
    )
    edge_uncertainty_plan = admin_explain(
        "SELECT 1 FROM core.code_edge "
        "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s "
        "AND reference_status IS NOT NULL LIMIT 1",
        (REPO, BRANCH),
    )
    ck(
        "normal coordinate uncertainty probes use the sparse partial indexes",
        "code_node_coord_uncertain"
        in plan_index_names(node_uncertainty_plan)
        and "code_edge_coord_uncertain"
        in plan_index_names(edge_uncertainty_plan),
    )

    # The over-cap branch builds its empty payload by hand instead of calling
    # build_graph. Drive that exact App path through the real governed DB
    # writer: a current cg4 empty graph must still declare schema contract v2,
    # persist an honest zero-row coordinate, and retain its measured input
    # count. This catches a producer/DB rollout mismatch that a fake writer
    # cannot observe.
    overcap_repo = "graph/current-over-cap"
    overcap_data = repo_tarball({
        "one.py": "VALUE = 1\n",
        "two.py": "VALUE = 2\n",
    })

    class OverCapGH:
        def download_tarball(self, repo, sha):
            del repo, sha
            return overcap_data

    old_ingest_cap = I._MAX_INGEST_FILES
    overcap_result = {}
    overcap_error = ""
    try:
        I._MAX_INGEST_FILES = 1
        overcap_result = I._full_ingest(
            app,
            OverCapGH(),
            overcap_repo,
            BRANCH,
            "9" * 40,
        )
    except Exception as exc:
        overcap_error = f"{type(exc).__name__}: {exc}"
    finally:
        I._MAX_INGEST_FILES = old_ingest_cap
    overcap_version = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)",
            (overcap_repo, BRANCH))
    )
    overcap_obs = overcap_version.get("observability") or {}
    overcap_persistence = overcap_obs.get("persistence") or {}
    ck(
        "real over-cap full path persists a cg4/v2 honest empty coordinate "
        f"(error={overcap_error!r})",
        not overcap_error
        and overcap_result.get("mode") == "full"
        and overcap_result.get("over_cap") is True
        and overcap_result.get("source_files") == 2
        and overcap_version.get("extractor_version") == C.EXTRACTOR_VERSION
        and overcap_version.get("commit_sha") == "9" * 40
        and overcap_version.get("node_count") == 0
        and overcap_version.get("edge_count") == 0
        and overcap_obs.get("schema_contract_version")
        == C.SCHEMA_CONTRACT_VERSION
        and overcap_obs.get("mode") == "full"
        and overcap_obs.get("over_cap") is True
        and overcap_obs.get("input_file_count") == 2
        and overcap_persistence.get("nodes_input") == 0
        and overcap_persistence.get("edges_input") == 0
        and admin_rows(
            "SELECT count(*) FROM core.code_node "
            "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s",
            (overcap_repo, BRANCH),
        )[0][0] == 0
        and admin_rows(
            "SELECT count(*) FROM core.code_edge "
            "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s",
            (overcap_repo, BRANCH),
        )[0][0] == 0,
    )

    # The extractor's outer per-file exception guard must remain compatible
    # with the governed full-writer observability wall.  A parser defect is
    # recorded as files_failed=1, but the analyzed document still persists as
    # a canonical, hashed bare file node so input_file_count remains truthful.
    failed_file_repo = "graph/persisted-failed-file"
    with tempfile.TemporaryDirectory(prefix="persist_failed_file_") as fixture:
        failed_path = os.path.join(fixture, "boom.py")
        with open(failed_path, "w", encoding="utf-8") as fh:
            fh.write("def retained_at_file_level():\n    return True\n")

        original_extract_file_py = X.extract_file_py

        def raise_unexpected_extractor_error(*_args, **_kwargs):
            raise RuntimeError("synthetic per-file extractor failure")

        X.extract_file_py = raise_unexpected_extractor_error
        try:
            failed_file_graph = X.build_graph(
                fixture,
                repo=failed_file_repo,
            )
        finally:
            X.extract_file_py = original_extract_file_py

    failed_file_graph["metrics"] = dict(
        failed_file_graph.get("metrics") or {},
        mode="full",
    )
    failed_file_write = obj(app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (
            json.dumps(failed_file_graph),
            failed_file_repo,
            BRANCH,
            "e" * 40,
        ),
    ))
    failed_file_rows = admin_rows(
        "SELECT node_id,node_kind,path,language,content_hash,analysis_status "
        "FROM core.code_node "
        "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s "
        "ORDER BY node_id,node_kind,path",
        (failed_file_repo, BRANCH),
    )
    failed_file_version = obj(app(
        "SELECT core.coordinate_graph_sha(%s,%s)",
        (failed_file_repo, BRANCH),
    ))
    failed_file_obs = failed_file_version.get("observability") or {}
    ck("unexpected extractor exception survives governed full ingest as one bare file row",
       failed_file_write.get("ok") is True
       and failed_file_graph.get("files_failed") == 1
       and failed_file_graph.get("files_parsed") == 0
       and len(failed_file_rows) == 1
       and failed_file_rows[0][:4]
       == ("boom.py", "file", "boom.py", "python")
       and isinstance(failed_file_rows[0][4], str)
       and len(failed_file_rows[0][4]) == 40
       and failed_file_rows[0][5] == "failed")
    ck("failed-file DB observability retains the exact one-document contract",
       failed_file_obs.get("input_file_count") == 1
       and failed_file_obs.get("node_kind_counts", {}).get("file") == 1
       and (failed_file_obs.get("persistence") or {}).get("input_files") == 1
       and (failed_file_obs.get("persistence") or {}).get(
           "exclusions", {}
       ).get("nodes", {}).get("count") == 0)

    node_kinds = {
        row[0] for row in admin_rows(
            "SELECT DISTINCT node_kind FROM core.code_node WHERE repo=%s", (REPO,))
    }
    edge_kinds = {
        row[0] for row in admin_rows(
            "SELECT DISTINCT edge_kind FROM core.code_edge WHERE repo=%s", (REPO,))
    }
    ck("all 20 extractor node kinds persist", node_kinds == C.EXTRACTOR_NODE_KINDS)
    ck("all 8 extractor edge kinds persist", edge_kinds == C.EXTRACTOR_EDGE_KINDS)
    missing_semantic_nodes = admin_rows(
        "SELECT count(*) FROM core.code_node "
        "WHERE repo=%s AND semantic_key IS NULL",
        (REPO,),
    )[0][0]
    missing_semantic_edges = admin_rows(
        "SELECT count(*) FROM core.code_edge "
        "WHERE repo=%s AND semantic_dst_key IS NULL",
        (REPO,),
    )[0][0]
    ck("every resolvable persisted Node/Edge carries semantic identity",
       missing_semantic_nodes == 0 and missing_semantic_edges == 0)

    metadata = admin_rows(
        "SELECT canonical_key,resource_scope,extractor,confidence,provenance "
        "FROM core.code_node WHERE repo=%s AND node_kind='table'", (REPO,)
    )[0]
    ck("resource metadata survives persistence",
       metadata[0] == "orders" and metadata[1] and metadata[2]
       and metadata[3] == 1.0 and isinstance(metadata[4], dict))

    version = obj(app("SELECT core.coordinate_graph_sha(%s,%s)", (REPO, BRANCH)))
    obs = version.get("observability") or {}
    ck("graph_version stores returned hash and merged extractor/persistence observability",
       version.get("graph_hash") == full.get("graph_hash")
       and isinstance(version.get("graph_revision"), int)
       and version.get("graph_revision") > 0
       and full.get("graph_revision") == version.get("graph_revision")
       and version.get("extractor_version") == C.EXTRACTOR_VERSION
       and version.get("semantic_ref_version") == C.SEMANTIC_REF_VERSION
       and version.get("current_semantic_ref_version")
       == C.SEMANTIC_REF_VERSION
       and obs.get("extraction_graph_hash") == base["metrics"]["extraction_graph_hash"]
       and obs.get("node_kind_counts") == base["metrics"]["node_kind_counts"]
       and obs.get("edge_kind_counts") == base["metrics"]["edge_kind_counts"]
       and obs.get("nodes_by_substrate") == base["metrics"]["nodes_by_substrate"]
       and obs.get("edges_by_substrate") == base["metrics"]["edges_by_substrate"]
       and obs.get("fallback_full_rebuild_reasons") == ["no_stored_graph_baseline"]
       and obs.get("persisted_graph_hash") == full.get("graph_hash")
       and (obs.get("persistence") or {}).get("producer_extractor_version")
       == C.EXTRACTOR_VERSION
       and (obs.get("persistence") or {}).get("input_files")
       == base["metrics"]["input_file_count"]
       and (obs.get("persistence") or {}).get("exclusions", {}).get("nodes", {}).get("count") == 0
       and (obs.get("persistence") or {}).get("exclusions", {}).get("edges", {}).get("count") == 0)

    catalog = obj(app("SELECT core.coordinate_resource_catalog(%s,%s)", (REPO, BRANCH)))
    ck("resource catalog has the fixed six-field outer shape",
       set(catalog) == {
           "resources", "context_paths", "definition_paths",
           "reference_conditioned_paths", "pairing_paths",
           "bidirectional_import_paths",
       })
    ck("catalog context includes resource definitions, config-only files and manifest paths",
       {"schema.sql", "package.json", "app.py"} <= set(catalog["context_paths"]))
    ck("catalog resources expose exact-reference semantic keys",
       all(
           len(str(resource.get("semantic_key") or "")) == 64
           for resource in catalog["resources"]
       ))
    ck("catalog definition paths include every resource node path plus explicit definers",
       {
           "schema.sql", "package.json", "infra/main.tf", "k8s/service.yaml",
           "schema.graphql", "api.proto", "openapi.yaml", "src-tauri/lib.rs",
           "tasks.py", "worker.ts",
       } <= set(catalog["definition_paths"]))
    ck("catalog identifies reference-conditioned CI/Celery/BullMQ consumers",
       {"app.py", ".github/workflows/ci.yml"}
       <= set(catalog["reference_conditioned_paths"]))
    ck("catalog pairing paths are limited to query-only sibling/role members",
       set(catalog["pairing_paths"]) == {"ui/card.tsx", "ui/card.test.tsx"})
    ck("catalog detects both sides of bidirectional imports",
       {"app.py", "peer.py"} <= set(catalog["bidirectional_import_paths"]))
    ck("coordinate_file_paths treats config_file as a real baseline path",
       "package.json" in set(app("SELECT core.coordinate_file_paths(%s,%s)", (REPO, BRANCH)) or []))

    # Patch the same semantic end state that a full build writes to a second coordinate.
    end = json.loads(json.dumps(base))
    for node in end["nodes"]:
        if node["id"] == "app.py":
            node["content_hash"] = "b" * 40
    end["metrics"]["extraction_graph_hash"] = "f" * 64
    patch_nodes = [n for n in end["nodes"] if n["path"] == "app.py"]
    patch_edges = [
        e for e in end["edges"] if e["src"].split("::", 1)[0] == "app.py"
    ]
    patch_metrics = C.collect_graph_metrics(
        {"nodes": patch_nodes, "edges": patch_edges},
        input_paths=("app.py",),
        unresolved_references=2,
        ambiguous_references=0,
    ).as_dict()
    patch_metrics.update({
        "mode": "patch",
        "resolution_context_file_count": 17,
        "schema_contract_version": C.SCHEMA_CONTRACT_VERSION,
    })
    patch_payload = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "expected_base_sha": "a" * 40,
        "expected_base_revision": version["graph_revision"],
        "nodes": patch_nodes,
        "edges": patch_edges,
        "metrics": patch_metrics,
    }
    patch = obj(app(
        "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
        (json.dumps(patch_payload), REPO, BRANCH, ["app.py"], [], "b" * 40),
    ))
    truth = obj(app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(end), TRUTH_REPO, BRANCH, "b" * 40),
    ))
    ck("full and patch hashes are coordinate-independent and equal for the same persisted graph",
       patch.get("graph_hash") == truth.get("graph_hash") and patch.get("graph_hash") != full.get("graph_hash"))
    patched_version = obj(app("SELECT core.coordinate_graph_sha(%s,%s)", (REPO, BRANCH)))
    ck("an explicit current-version patch preserves the whole-coordinate stamp",
       patched_version.get("extractor_version") == C.EXTRACTOR_VERSION
       and patched_version.get("graph_revision") > version.get("graph_revision")
       and patch.get("graph_revision") == patched_version.get("graph_revision")
       and (patched_version.get("observability") or {}).get("mode") == "patch"
       and (patched_version.get("observability") or {}).get("node_kind_counts")
       == patch_metrics["node_kind_counts"]
       and (patched_version.get("observability") or {}).get(
           "resolution_context_file_count") == 17
       and ((patched_version.get("observability") or {}).get("persistence") or {}).get(
           "producer_extractor_version") == C.EXTRACTOR_VERSION
       and ((patched_version.get("observability") or {}).get("persistence") or {}).get(
           "input_files") == patch_metrics["input_file_count"]
       and ((patched_version.get("observability") or {}).get("persistence") or {}).get(
           "base_graph_revision") == version.get("graph_revision")
       and ((patched_version.get("observability") or {}).get("persistence") or {}).get(
           "graph_revision") == patched_version.get("graph_revision"))
    ck("column edges remain present after patch",
       {row[0] for row in admin_rows(
           "SELECT edge_kind FROM core.code_edge WHERE repo=%s AND edge_kind IN ('alters_col','queries_col')",
           (REPO,),
       )} == {"alters_col", "queries_col"})
    # Every later rejection probe is derived from the now-patched coordinate.
    patch_payload["expected_base_sha"] = "b" * 40
    patch_payload["expected_base_revision"] = patched_version["graph_revision"]

    # Python's graph contract is a semantic set: duplicate Node
    # (id,kind,path) or Edge (src,dst,kind) identities are invalid. Exercise
    # the same wall through both governed SQL writers and prove each 22023
    # rejection occurs before a full-coordinate or touched-path mutation.
    for writer_mode in ("full", "patch"):
        for identity_kind, array_key in (
            ("Node", "nodes"),
            ("Edge", "edges"),
        ):
            before_duplicate = coordinate_snapshot()
            duplicate_payload = json.loads(json.dumps(
                end if writer_mode == "full" else patch_payload
            ))
            duplicate_payload[array_key].append(
                json.loads(json.dumps(duplicate_payload[array_key][0]))
            )
            duplicate_rejected = False
            try:
                if writer_mode == "full":
                    app(
                        "SELECT core.ingest_graph_with_authority"
                        "(%s,%s,%s,%s)",
                        (
                            json.dumps(duplicate_payload),
                            REPO,
                            BRANCH,
                            "d" * 40,
                        ),
                    )
                else:
                    app(
                        "SELECT core.patch_graph_with_authority"
                        "(%s,%s,%s,%s,%s,%s)",
                        (
                            json.dumps(duplicate_payload),
                            REPO,
                            BRANCH,
                            ["app.py"],
                            [],
                            "e" * 40,
                        ),
                    )
            except psycopg2.Error as exc:
                duplicate_rejected = (
                    exc.pgcode == "22023"
                    and f"duplicate graph {identity_kind} identity"
                    in str(exc)
                )
            ck(
                f"{writer_mode} writer rejects duplicate {identity_kind} "
                "identity with 22023 before mutation",
                duplicate_rejected
                and coordinate_snapshot() == before_duplicate,
            )

    # Compare-and-swap the baseline under the coordinate lock.  The stale patch
    # was prepared from A; a concurrent/same-clock full write installs B before
    # it executes.  captured_at is NULL throughout, so the historical timestamp
    # guard cannot mask this race.  Rejection must preserve B byte-for-byte.
    cas_repo = "graph/patch-baseline-cas"
    cas_a = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": C.SCHEMA_CONTRACT_VERSION},
        "nodes": [{
            "id": "race.py", "kind": "file", "path": "race.py",
            "language": "python", "content_hash": "5" * 40,
        }],
        "edges": [],
    }
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(cas_a), cas_repo, BRANCH, "5" * 40),
    )
    cas_a_version = coordinate_snapshot(cas_repo)["version"]
    stale_cas_patch = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": C.SCHEMA_CONTRACT_VERSION},
        "expected_base_sha": "5" * 40,
        "expected_base_revision": cas_a_version["graph_revision"],
        "nodes": [{
            "id": "race.py", "kind": "file", "path": "race.py",
            "language": "python", "content_hash": "7" * 40,
        }],
        "edges": [],
    }
    cas_b = json.loads(json.dumps(cas_a))
    cas_b["nodes"][0]["content_hash"] = "6" * 40
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(cas_b), cas_repo, BRANCH, "6" * 40),
    )
    cas_b_snapshot = coordinate_snapshot(cas_repo)
    stale_cas_rejected = False
    try:
        app(
            "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
            (json.dumps(stale_cas_patch), cas_repo, BRANCH,
             ["race.py"], [], "7" * 40),
        )
    except psycopg2.Error as exc:
        stale_cas_rejected = (
            exc.pgcode == "40001"
            and "baseline coordinate does not match" in str(exc)
        )
    ck("stale baseline patch is rejected before DELETE with exact B snapshot preserved",
       stale_cas_rejected
       and coordinate_snapshot(cas_repo) == cas_b_snapshot)

    fresh_cas_patch = json.loads(json.dumps(stale_cas_patch))
    fresh_cas_patch["expected_base_sha"] = "6" * 40
    fresh_cas_patch["expected_base_revision"] = cas_b_snapshot[
        "version"]["graph_revision"]
    fresh_cas = obj(app(
        "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
        (json.dumps(fresh_cas_patch), cas_repo, BRANCH,
         ["race.py"], [], "7" * 40),
    ))
    fresh_cas_snapshot = coordinate_snapshot(cas_repo)
    ck("matching baseline patch advances atomically to the requested SHA",
       fresh_cas.get("ok") is True
       and fresh_cas.get("commit_sha") == "7" * 40
       and fresh_cas.get("graph_revision")
       > cas_b_snapshot["version"].get("graph_revision")
       and fresh_cas_snapshot["version"].get("commit_sha") == "7" * 40
       and fresh_cas_snapshot["version"].get("graph_revision")
       == fresh_cas.get("graph_revision")
       and fresh_cas_snapshot["nodes"][0][3] == "7" * 40)

    # SHA-only CAS has an ABA hole: a stale patch captured at P can otherwise
    # pass after two full writers move P→B→P.  The monotonic revision must
    # reject that exact sequence even though the stored SHA equals the stale
    # patch's expected SHA again.
    aba_repo = "graph/patch-baseline-aba"
    aba_p = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": C.SCHEMA_CONTRACT_VERSION},
        "nodes": [{
            "id": "aba.py", "kind": "file", "path": "aba.py",
            "language": "python", "content_hash": "a" * 40,
        }],
        "edges": [],
    }
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(aba_p), aba_repo, BRANCH, "a" * 40),
    )
    aba_initial_revision = coordinate_snapshot(aba_repo)[
        "version"]["graph_revision"]
    stale_aba_patch = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": C.SCHEMA_CONTRACT_VERSION},
        "expected_base_sha": "a" * 40,
        "expected_base_revision": aba_initial_revision,
        "nodes": [{
            "id": "aba.py", "kind": "file", "path": "aba.py",
            "language": "python", "content_hash": "c" * 40,
        }],
        "edges": [],
    }
    aba_b = json.loads(json.dumps(aba_p))
    aba_b["nodes"][0]["content_hash"] = "b" * 40
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(aba_b), aba_repo, BRANCH, "b" * 40),
    )
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(aba_p), aba_repo, BRANCH, "a" * 40),
    )
    aba_p3_snapshot = coordinate_snapshot(aba_repo)
    stale_aba_rejected = False
    try:
        app(
            "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
            (json.dumps(stale_aba_patch), aba_repo, BRANCH,
             ["aba.py"], [], "c" * 40),
        )
    except psycopg2.Error as exc:
        stale_aba_rejected = (
            exc.pgcode == "40001"
            and "baseline coordinate does not match" in str(exc)
        )
    ck("stale P patch is rejected after full P→B→P ABA with newer snapshot preserved",
       stale_aba_rejected
       and aba_p3_snapshot["version"].get("commit_sha") == "a" * 40
       and aba_p3_snapshot["version"].get("graph_revision")
       > aba_initial_revision
       and coordinate_snapshot(aba_repo) == aba_p3_snapshot)

    # Retention/offboarding can delete graph_version entirely. A coordinate-
    # local counter would restart and reuse the stale token on recreation.
    # The global NO-CYCLE sequence must keep the new token distinct.
    recreate_repo = "graph/patch-baseline-recreate"
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(aba_p), recreate_repo, BRANCH, "a" * 40),
    )
    recreate_initial = coordinate_snapshot(recreate_repo)
    stale_recreate_patch = json.loads(json.dumps(stale_aba_patch))
    stale_recreate_patch["expected_base_revision"] = recreate_initial[
        "version"]["graph_revision"]
    admin_exec(
        "DELETE FROM core.code_edge WHERE repo=%s; "
        "DELETE FROM core.code_node WHERE repo=%s; "
        "DELETE FROM core.graph_version WHERE repo=%s",
        (recreate_repo, recreate_repo, recreate_repo),
    )
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(aba_p), recreate_repo, BRANCH, "a" * 40),
    )
    recreate_new = coordinate_snapshot(recreate_repo)
    stale_recreate_rejected = False
    try:
        app(
            "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
            (json.dumps(stale_recreate_patch), recreate_repo, BRANCH,
             ["aba.py"], [], "c" * 40),
        )
    except psycopg2.Error as exc:
        stale_recreate_rejected = (
            exc.pgcode == "40001"
            and "baseline coordinate does not match" in str(exc)
        )
    ck("delete→same-SHA recreate uses a never-reused token and rejects the pre-delete patch",
       stale_recreate_rejected
       and recreate_new["version"].get("commit_sha") == "a" * 40
       and recreate_new["version"].get("graph_revision")
       != recreate_initial["version"].get("graph_revision")
       and coordinate_snapshot(recreate_repo) == recreate_new)

    # The cold-retention candidate scan and its DELETE statements are separate
    # snapshots.  Reproduce the production race with two real sessions: A
    # applies a path-local patch and deliberately keeps the transaction (and
    # repo lock) open; B selected the old graph as cold and must block on that
    # exact repo lock before deleting anything.  After A commits, the retry
    # sees the new ingested_at and skips the now-warm graph.  Without the
    # shared repo serialization, retention can delete the retained cold.py
    # row between patch CAS and the touched-path INSERT, leaving a falsely
    # stamped partial graph.
    retention_race_repo = "graph/lifecycle-retention-race"
    retention_base = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": C.SCHEMA_CONTRACT_VERSION},
        "nodes": [
            {
                "id": "hot.py", "kind": "file", "path": "hot.py",
                "language": "python", "content_hash": "d" * 40,
            },
            {
                "id": "cold.py", "kind": "file", "path": "cold.py",
                "language": "python", "content_hash": "1" * 40,
            },
        ],
        "edges": [
            {"src": "hot.py", "dst": "cold.py", "kind": "imports"},
        ],
    }
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (
            json.dumps(retention_base),
            retention_race_repo,
            BRANCH,
            "d" * 40,
        ),
    )
    retention_base_version = coordinate_snapshot(retention_race_repo)["version"]
    admin_exec(
        "SELECT core.mark_governed_write('graph_version'); "
        "UPDATE core.graph_version "
        "SET ingested_at=clock_timestamp()-interval '2 hours' "
        "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s",
        (retention_race_repo, BRANCH),
    )
    retention_old_snapshot = coordinate_snapshot(retention_race_repo)
    retention_patch = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": C.SCHEMA_CONTRACT_VERSION},
        "expected_base_sha": "d" * 40,
        "expected_base_revision": retention_base_version["graph_revision"],
        "nodes": [
            {
                "id": "hot.py", "kind": "file", "path": "hot.py",
                "language": "python", "content_hash": "e" * 40,
            },
        ],
        "edges": [
            {"src": "hot.py", "dst": "cold.py", "kind": "imports"},
        ],
    }
    patch_conn = psycopg2.connect(
        f"postgresql://veripsa_app@localhost/{DB}"
    )
    retention_blocked_before_mutation = False
    retention_visible_during_patch = None
    retention_patch_result = {}
    try:
        with patch_conn.cursor() as patch_cur:
            patch_cur.execute("SET search_path=core")
            patch_cur.execute(
                "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
                (
                    json.dumps(retention_patch),
                    retention_race_repo,
                    BRANCH,
                    ["hot.py"],
                    [],
                    "e" * 40,
                ),
            )
            retention_patch_result = obj(patch_cur.fetchone()[0])

        sweep_conn = psycopg2.connect(
            f"postgresql://veripsa_app@localhost/{DB}"
        )
        try:
            with sweep_conn.cursor() as sweep_cur:
                sweep_cur.execute("SET lock_timeout='300ms'")
                sweep_cur.execute(
                    "SELECT core.prune_all_accounts_with_authority("
                    "now()-interval '1 hour',ARRAY['landed','push'],1000,NULL)"
                )
            sweep_conn.commit()
        except psycopg2.Error as exc:
            retention_blocked_before_mutation = exc.pgcode == "55P03"
            sweep_conn.rollback()
        finally:
            sweep_conn.close()

        # A separate connection sees the pre-patch committed snapshot, proving
        # B's timed-out transaction did not leak a partial DELETE.
        retention_visible_during_patch = coordinate_snapshot(
            retention_race_repo
        )
        patch_conn.commit()
    finally:
        if not patch_conn.closed:
            patch_conn.rollback()
            patch_conn.close()

    retention_committed_snapshot = coordinate_snapshot(retention_race_repo)
    retention_retry = obj(app(
        "SELECT core.prune_all_accounts_with_authority("
        "now()-interval '1 hour',ARRAY['landed','push'],1000,NULL)"
    ))
    retention_after_retry = coordinate_snapshot(retention_race_repo)
    ck("cold retention blocks before mutation while a patch holds the repo lock",
       retention_blocked_before_mutation
       and retention_visible_during_patch == retention_old_snapshot)
    ck("retention retry rechecks freshness and preserves the complete committed patch",
       retention_patch_result.get("ok") is True
       and retention_patch_result.get("mode") == "patch"
       and retention_committed_snapshot["version"].get("commit_sha")
       == "e" * 40
       and retention_committed_snapshot["version"].get("graph_hash")
       == retention_patch_result.get("graph_hash")
       and {
           row[0] for row in retention_committed_snapshot["nodes"]
       } == {"hot.py", "cold.py"}
       and retention_committed_snapshot["edges"]
       == [("hot.py", "cold.py", "imports")]
       and retention_retry.get("ok") is True
       and retention_after_retry == retention_committed_snapshot)

    # An overlong changed path used to be left-truncated to 1024 characters,
    # aliasing and deleting a distinct valid prefix path.  Keep that exact
    # prefix in the real DB and prove the 1025-character request is atomic.
    prefix_repo = "graph/patch-path-boundary"
    prefix_path = "p" * 1024
    prefix_graph = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": C.SCHEMA_CONTRACT_VERSION},
        "nodes": [{
            "id": prefix_path, "kind": "file", "path": prefix_path,
            "content_hash": "8" * 40,
        }],
        "edges": [],
    }
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(prefix_graph), prefix_repo, BRANCH, "8" * 40),
    )
    prefix_snapshot = coordinate_snapshot(prefix_repo)
    overlong_patch = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": C.SCHEMA_CONTRACT_VERSION},
        "expected_base_sha": "8" * 40,
        "expected_base_revision": prefix_snapshot["version"]["graph_revision"],
        "nodes": [],
        "edges": [],
    }
    overlong_rejected = False
    try:
        app(
            "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
            (json.dumps(overlong_patch), prefix_repo, BRANCH,
             [prefix_path + "x"], [], "9" * 40),
        )
    except psycopg2.Error as exc:
        overlong_rejected = (
            exc.pgcode == "22023"
            and "touched path exceeds" in str(exc)
        )
    ck("1025-character patch path is rejected without deleting its 1024-character prefix",
       overlong_rejected
       and coordinate_snapshot(prefix_repo) == prefix_snapshot)

    # The two governed writers share a closed, content-free observability
    # contract.  Exercise both input aliases and all three smuggling classes:
    # an unknown key, arbitrary nested data, and a free-form string in a known
    # scalar slot.  Every rejection must happen before the full-coordinate or
    # touched-path DELETE, proven by exact real-DB readback.
    sentinel = "SOURCE_BODY_SENTINEL_def_secret_value"
    attacks = (
        ("metrics unknown key", "metrics", {"source_body": sentinel}),
        ("observability unknown key", "observability", {"source_body": sentinel}),
        (
            "metrics nested payload",
            "metrics",
            {"node_kind_counts": {"file": {"source_body": sentinel}}},
        ),
        (
            "observability nested payload",
            "observability",
            {"node_kind_counts": {"file": {"source_body": sentinel}}},
        ),
        ("metrics free-form string", "metrics", {"mode": sentinel}),
        (
            "observability free-form string",
            "observability",
            {"ambiguity_detection_scope": sentinel},
        ),
        (
            "metrics free-form fallback reason",
            "metrics",
            {"fallback_full_rebuild_reasons": [sentinel]},
        ),
        (
            "metrics nonzero producer exclusion",
            "metrics",
            {"persistence_excluded_nodes": 1},
        ),
        (
            "metrics producer exclusion reason",
            "metrics",
            {"persistence_exclusion_reasons": {"node_kind_not_accepted:file": 1}},
        ),
        (
            "metrics forged node-kind counts",
            "metrics",
            {
                "schema_contract_version": C.SCHEMA_CONTRACT_VERSION,
                "node_kind_counts": {
                    kind: 0 for kind in C.EXTRACTOR_NODE_KIND_ORDER
                }
            },
        ),
    )
    for index, (label, field, malicious_value) in enumerate(attacks):
        full_hex = "123456789abcdef0"[index]
        patch_hex = "89abcdef01234567"[index]
        before_full_rejection = coordinate_snapshot()
        bad_full = json.loads(json.dumps(end))
        for node in bad_full["nodes"]:
            if node["id"] == "app.py":
                node["content_hash"] = full_hex * 40
        bad_full[field] = malicious_value
        full_rejected = False
        try:
            app(
                "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                (json.dumps(bad_full), REPO, BRANCH, full_hex * 40),
            )
        except psycopg2.Error as exc:
            full_rejected = (
                exc.pgcode == "22023"
                and "graph observability" in str(exc)
            )
        ck(
            f"full writer rejects {label} before replacing the coordinate",
            full_rejected and coordinate_snapshot() == before_full_rejection,
        )

        before_patch_rejection = coordinate_snapshot()
        bad_patch = json.loads(json.dumps(patch_payload))
        bad_patch["nodes"][0]["content_hash"] = patch_hex * 40
        bad_patch[field] = malicious_value
        patch_rejected = False
        try:
            app(
                "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
                (
                    json.dumps(bad_patch),
                    REPO,
                    BRANCH,
                    ["app.py"],
                    [],
                    patch_hex * 40,
                ),
            )
        except psycopg2.Error as exc:
            patch_rejected = (
                exc.pgcode == "22023"
                and "graph observability" in str(exc)
            )
        ck(
            f"patch writer rejects {label} before deleting the touched path",
            patch_rejected and coordinate_snapshot() == before_patch_rejection,
        )

    ck(
        "rejected source-body sentinels never reach graph_version.observability",
        admin_rows(
            "SELECT count(*) FROM core.graph_version "
            "WHERE observability::text LIKE %s",
            (f"%{sentinel}%",),
        )[0][0]
        == 0,
    )

    # Writer-context fields are also fail-closed: a value which is legitimate
    # for the other writer must not be accepted under the wrong semantics.
    full_policy_attacks = (
        ("patch mode on full writer", {"mode": "patch"}),
        (
            "patch-only resolution context on full writer",
            {"resolution_context_file_count": 1},
        ),
        ("false over-cap claim on full writer", {"over_cap": False}),
    )
    for index, (label, malicious_metrics) in enumerate(full_policy_attacks):
        before = coordinate_snapshot()
        bad_full = json.loads(json.dumps(end))
        bad_full["metrics"] = malicious_metrics
        rejected = False
        try:
            app(
                "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                (
                    json.dumps(bad_full),
                    REPO,
                    BRANCH,
                    format(index + 10, "x") * 40,
                ),
            )
        except psycopg2.Error as exc:
            rejected = (
                exc.pgcode == "22023"
                and "graph observability" in str(exc)
            )
        ck(
            f"full writer rejects {label} before replacing the coordinate",
            rejected and coordinate_snapshot() == before,
        )

    patch_policy_attacks = (
        ("full mode on patch writer", {"mode": "full"}),
        ("full-only over-cap on patch writer", {"over_cap": True}),
        (
            "nonempty fallback codes on patch writer",
            {"fallback_full_rebuild_reasons": ["incremental_internal_error"]},
        ),
    )
    for index, (label, malicious_metrics) in enumerate(patch_policy_attacks):
        before = coordinate_snapshot()
        bad_patch = json.loads(json.dumps(patch_payload))
        bad_patch["metrics"] = malicious_metrics
        rejected = False
        try:
            app(
                "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
                (
                    json.dumps(bad_patch),
                    REPO,
                    BRANCH,
                    ["app.py"],
                    [],
                    format(index + 13, "x") * 40,
                ),
            )
        except psycopg2.Error as exc:
            rejected = (
                exc.pgcode == "22023"
                and "graph observability" in str(exc)
            )
        ck(
            f"patch writer rejects {label} before deleting the touched path",
            rejected and coordinate_snapshot() == before,
        )

    # Execute the real review adjacency query against persisted rows. Table
    # edges must couple schema.sql↔app.py, while the column-only evidence pair
    # must remain absent (behavior preservation, not merely source inspection).
    admin_exec(
        "SELECT core.mark_governed_write('claim'); "
        "INSERT INTO core.claim(claim_id,account_id,agent_id,change_id,repo,branch,target_path) VALUES "
        "('graph-table','ACCT-DEMO','agent-a','PR-1',%s,%s,'schema.sql'),"
        "('graph-column','ACCT-DEMO','agent-b','PR-2',%s,%s,'column-only-def.sql')",
        (REPO, BRANCH, REPO, BRANCH),
    )
    adjacency = {
        (row[0], row[1], row[2])
        for row in admin_rows(
            "SELECT set_config('core.current_account','ACCT-DEMO',true); "
            "SELECT f,nbr,dir FROM core._claim_adjacency(%s,%s,%s)",
            ("ACCT-DEMO", REPO, BRANCH),
        )
    }
    ck(f"actual adjacency query uses table edges but keeps column-only evidence out of review behavior "
       f"(rows={sorted(adjacency)!r})",
       ("schema.sql", "app.py", "shared") in adjacency
       and not any(
           {src, dst} == {"column-only-def.sql", "column-only-query.py"}
           for src, dst, _direction in adjacency
       ))

    # A legal Git path can equal a generated Resource node id. Both facts must
    # persist, but edge-source resolution must use only document Nodes; joining
    # every node_kind on node_id would falsely attribute this file's call edge
    # to settings.json through the colliding config_key row.
    collision_repo = "graph/node-id-collision"
    collision_key = "veripsa.webhook.py"
    collision_path = f"cfgkey::settings.json::{collision_key}"
    collision_graph = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": C.SCHEMA_CONTRACT_VERSION},
        "nodes": [
            {
                "id": collision_path,
                "kind": "file",
                "path": collision_path,
                "language": "python",
                "content_hash": "4" * 40,
            },
            {
                "id": "settings.json",
                "kind": "config_file",
                "path": "settings.json",
                "language": "config",
            },
            C.enrich_resource_node(
                {
                    "id": collision_path,
                    "kind": "config_key",
                    "path": "settings.json",
                    "name": collision_key,
                    "language": "config",
                },
                repo=collision_repo,
            ),
            {
                "id": "target.py",
                "kind": "file",
                "path": "target.py",
                "language": "python",
                "content_hash": "5" * 40,
            },
            {
                "id": "target.py::calculate_collision_target",
                "kind": "def",
                "path": "target.py",
                "name": "calculate_collision_target",
                "language": "python",
            },
        ],
        "edges": [
            {
                "src": "target.py",
                "dst": "target.py::calculate_collision_target",
                "kind": "contains",
            },
            {
                "src": collision_path,
                "dst": "calculate_collision_target",
                "kind": "calls",
            },
            {
                "src": collision_path,
                "dst": collision_key,
                "kind": "reads_config",
            },
        ],
    }
    collision_write = obj(app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (
            json.dumps(collision_graph),
            collision_repo,
            BRANCH,
            "4" * 40,
        ),
    ))
    collision_nodes = set(admin_rows(
        "SELECT node_kind,path FROM core.code_node "
        "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s "
        "AND node_id=%s",
        (collision_repo, BRANCH, collision_path),
    ))
    collision_edges = set(admin_rows(
        "SELECT src,dst,edge_kind FROM core.code_edge "
        "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s",
        (collision_repo, BRANCH),
    ))
    admin_exec(
        "SELECT core.mark_governed_write('claim'); "
        "INSERT INTO core.claim("
        "claim_id,account_id,agent_id,change_id,repo,branch,target_path"
        ") VALUES "
        "('graph-id-collision-file','ACCT-DEMO','agent-c','PR-3',%s,%s,%s),"
        "('graph-id-collision-resource','ACCT-DEMO','agent-d','PR-4',%s,%s,'settings.json')",
        (
            collision_repo,
            BRANCH,
            collision_path,
            collision_repo,
            BRANCH,
        ),
    )
    collision_adjacency = {
        (row[0], row[1], row[2])
        for row in admin_rows(
            "SELECT set_config('core.current_account','ACCT-DEMO',true); "
            "SELECT f,nbr,dir FROM core._claim_adjacency(%s,%s,%s)",
            ("ACCT-DEMO", collision_repo, BRANCH),
        )
    }
    ck("same-id file and config Resource both persist with their Resource edge",
       collision_write.get("ok") is True
       and collision_nodes
       == {
           ("file", collision_path),
           ("config_key", "settings.json"),
       }
       and (
           collision_path,
           collision_key,
           "reads_config",
       ) in collision_edges)
    ck(f"effective adjacency resolves edge sources through document kinds only "
       f"(rows={sorted(collision_adjacency)!r})",
       any(
           src == collision_path and dst == "target.py"
           for src, dst, _direction in collision_adjacency
       )
       and not any(
           src == "settings.json" and dst == "target.py"
           for src, dst, _direction in collision_adjacency
       ))

    # Presentation filtering must never rewrite semantic identity. `<>` is a
    # legal Git path but sanitizes to no display token at all. The writer must
    # retain a safe surrogate, full exact-reference digests on both endpoints,
    # zero unexplained loss, and production adjacency to the raw Node path.
    angle_repo = "graph/lossless-reference"
    angle_path = "<>"
    angle_digest = hashlib.sha256(angle_path.encode("utf-8")).hexdigest()
    angle_graph = {
        "extractor_version": C.EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": C.SCHEMA_CONTRACT_VERSION},
        "nodes": [
            {
                "id": "consumer.js",
                "kind": "file",
                "path": "consumer.js",
                "language": "javascript",
                "content_hash": "6" * 40,
            },
            {
                "id": angle_path,
                "kind": "file",
                "path": angle_path,
                "language": "javascript",
                "content_hash": "7" * 40,
            },
        ],
        "edges": [
            {
                "src": "consumer.js",
                "dst": angle_path,
                "kind": "imports",
            },
        ],
    }
    angle_write = obj(app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(angle_graph), angle_repo, BRANCH, "6" * 40),
    ))
    angle_exclusions = (
        ((angle_write.get("observability") or {}).get("persistence") or {})
        .get("exclusions")
        or {}
    )
    stored_angle_edges = set(admin_rows(
        "SELECT src,dst,edge_kind,semantic_dst_key FROM core.code_edge "
        "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s",
        (angle_repo, BRANCH),
    ))
    stored_angle_node_key = admin_rows(
        "SELECT semantic_key FROM core.code_node "
        "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s "
        "AND node_kind='file' AND path=%s",
        (angle_repo, BRANCH, angle_path),
    )
    admin_exec(
        "SELECT core.mark_governed_write('claim'); "
        "INSERT INTO core.claim("
        "claim_id,account_id,agent_id,change_id,repo,branch,target_path"
        ") VALUES "
        "('graph-angle-consumer','ACCT-DEMO','agent-e','PR-5',%s,%s,'consumer.js'),"
        "('graph-angle-target','ACCT-DEMO','agent-f','PR-6',%s,%s,%s)",
        (angle_repo, BRANCH, angle_repo, BRANCH, angle_path),
    )
    angle_adjacency = {
        (row[0], row[1], row[2])
        for row in admin_rows(
            "SELECT set_config('core.current_account','ACCT-DEMO',true); "
            "SELECT f,nbr,dir FROM core._claim_adjacency(%s,%s,%s)",
            ("ACCT-DEMO", angle_repo, BRANCH),
        )
    }
    ck("all-sanitized legal path keeps a safe display surrogate and exact semantic digests",
       angle_write.get("ok") is True
       and stored_angle_node_key == [(angle_digest,)]
       and any(
           src == "consumer.js"
           and dst == f"ref#{angle_digest[:12]}"
           and kind == "imports"
           and semantic_key == angle_digest
           for src, dst, kind, semantic_key in stored_angle_edges
       )
       and (angle_exclusions.get("nodes") or {}).get("count") == 0
       and (angle_exclusions.get("edges") or {}).get("count") == 0)
    ck(f"effective adjacency resolves the exact angle-bracket path "
       f"(rows={sorted(angle_adjacency)!r})",
       any(
           {src, dst} == {"consumer.js", angle_path}
           for src, dst, _direction in angle_adjacency
       ))

    # An unknown kind must fail before the coordinate replacement DELETE.
    before = obj(app("SELECT core.coordinate_graph_sha(%s,%s)", (REPO, BRANCH)))
    unknown_failed = False
    try:
        app(
            "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
            (json.dumps({"nodes": [{"id": "x", "kind": "mystery", "path": "x"}], "edges": []}),
             REPO, BRANCH, "c" * 40),
        )
    except psycopg2.Error as exc:
        unknown_failed = exc.pgcode == "22023" and "unknown graph node kind" in str(exc)
    after = obj(app("SELECT core.coordinate_graph_sha(%s,%s)", (REPO, BRANCH)))
    ck("unknown node kind raises 22023 and leaves the valid coordinate unchanged",
       unknown_failed and before.get("graph_hash") == after.get("graph_hash"))

    # SCHEMA-FIRST / ROLLBACK WINDOW. Historical full writers remain accepted
    # and honestly stamped behind cg4. A path-local patch, however, may run only
    # with the current producer on a same-version baseline: retained files cannot
    # be upgraded from cg1/cg2/cg3 semantics by a partial write.
    legacy_repo = "graph/legacy-producer"
    legacy_graph = {
        "nodes": [
            {"id": "legacy.py", "kind": "file", "path": "legacy.py",
             "content_hash": "1" * 40},
        ],
        "edges": [],
    }
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(legacy_graph), legacy_repo, BRANCH, "1" * 40),
    )
    legacy_version = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)", (legacy_repo, BRANCH)))
    ck("version-absent legacy full ingest stamps unknown, not cg4",
       legacy_version.get("extractor_version") is None
       and legacy_version.get("current_extractor_version") == "cg4")

    current_graph = json.loads(json.dumps(legacy_graph))
    current_graph["extractor_version"] = C.EXTRACTOR_VERSION
    current_graph["metrics"] = {
        "schema_contract_version": C.SCHEMA_CONTRACT_VERSION,
    }
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(current_graph), legacy_repo, BRANCH, "2" * 40),
    )
    current_version = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)", (legacy_repo, BRANCH)))
    ck("explicit cg4/v2 full ingest stamps cg4",
       current_version.get("extractor_version") == "cg4"
       and (current_version.get("observability") or {}).get(
           "schema_contract_version") == 2)

    explicit_cg3 = json.loads(json.dumps(legacy_graph))
    explicit_cg3["extractor_version"] = "cg3"
    explicit_cg3["metrics"] = {"schema_contract_version": 1}
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(explicit_cg3), legacy_repo, BRANCH, "8" * 40),
    )
    explicit_cg3_version = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)", (legacy_repo, BRANCH)))
    ck("schema-first cg3/v1 FULL ingest is accepted and stamped behind cg4",
       explicit_cg3_version.get("extractor_version") == "cg3"
       and explicit_cg3_version.get("current_extractor_version") == "cg4"
       and (explicit_cg3_version.get("observability") or {}).get(
           "schema_contract_version") == 1)

    # Exact immediately-historical under-cap wire: cg3/v1 build_graph emitted
    # the old closed ambiguity token.  A schema-first generation-15 rollout
    # must accept that in-flight FULL payload, stamp it honestly behind cg4,
    # and must not reinterpret the token as cg4's uncertainty contract.
    cg3_wire_repo = "graph/cg3-observability-wire"
    cg3_wire = json.loads(json.dumps(base))
    cg3_wire["extractor_version"] = "cg3"
    cg3_wire_metrics = dict(cg3_wire["metrics"])
    cg3_wire_metrics.update({
        "mode": "full",
        "schema_contract_version": 1,
        "ambiguity_detection_scope": (
            "retained multi-definer resources, emitted canonical-key "
            "collisions, and resolved import fan-out"
        ),
    })
    cg3_wire["metrics"] = cg3_wire_metrics
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(cg3_wire), cg3_wire_repo, BRANCH, "7" * 40),
    )
    cg3_wire_version = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)",
            (cg3_wire_repo, BRANCH)))
    cg3_wire_obs = cg3_wire_version.get("observability") or {}
    ck("exact cg3/v1 under-cap ambiguity token survives schema-first FULL ingest",
       cg3_wire_version.get("extractor_version") == "cg3"
       and cg3_wire_version.get("current_extractor_version") == "cg4"
       and cg3_wire_obs.get("schema_contract_version") == 1
       and cg3_wire_obs.get("ambiguity_detection_scope")
       == (
           "retained multi-definer resources, emitted canonical-key "
           "collisions, and resolved import fan-out"
       ))

    # Exact immediately-historical over-cap wire from cg3 _full_ingest.  That
    # branch constructed an honest empty graph by hand and therefore carried
    # neither schema_contract_version nor ambiguity_detection_scope.  Keep
    # this one bounded exception exact: cg3 + over_cap=true + empty arrays.
    cg3_overcap_repo = "graph/cg3-over-cap-wire"
    cg3_overcap = {
        "extractor_version": "cg3",
        "nodes": [],
        "edges": [],
        "metrics": {
            "mode": "full",
            "input_file_count": 401,
            "fallback_full_rebuild_reasons": [],
            "over_cap": True,
        },
    }
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(cg3_overcap), cg3_overcap_repo, BRANCH, "0" * 40),
    )
    cg3_overcap_version = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)",
            (cg3_overcap_repo, BRANCH)))
    cg3_overcap_obs = cg3_overcap_version.get("observability") or {}
    ck("exact markerless cg3 over-cap FULL wire remains rollout-compatible",
       cg3_overcap_version.get("extractor_version") == "cg3"
       and cg3_overcap_version.get("current_extractor_version") == "cg4"
       and cg3_overcap_version.get("node_count") == 0
       and cg3_overcap_version.get("edge_count") == 0
       and cg3_overcap_obs.get("over_cap") is True
       and "schema_contract_version" not in cg3_overcap_obs
       and "ambiguity_detection_scope" not in cg3_overcap_obs)

    explicit_cg2 = json.loads(json.dumps(legacy_graph))
    explicit_cg2["extractor_version"] = "cg2"
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(explicit_cg2), legacy_repo, BRANCH, "9" * 40),
    )
    explicit_cg2_version = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)", (legacy_repo, BRANCH)))
    ck("explicit historical cg2 full ingest records cg2 behind cg4",
       explicit_cg2_version.get("extractor_version") == "cg2"
       and explicit_cg2_version.get("current_extractor_version") == "cg4")

    # The immediately-prior cg2 app emitted human-readable fallback details in
    # this otherwise-current metrics shape.  Schema-first rollout must accept
    # that bounded historical wire without persisting its arbitrary strings.
    # Every non-empty legacy array is collapsed to one fixed code; cg3+ remains
    # strict and malformed historical arrays remain atomic hard failures.
    legacy_sentinel = "LEGACY_FALLBACK_DETAIL_MUST_NOT_PERSIST"
    legacy_metrics = C.collect_graph_metrics(
        legacy_graph,
        input_paths=("legacy.py",),
        unresolved_references=0,
        ambiguous_references=0,
    ).as_dict()
    legacy_metrics.update({
        "mode": "full",
        "schema_contract_version": 1,
        "ambiguity_detection_scope": (
            "retained multi-definer resources, emitted canonical-key "
            "collisions, and resolved import fan-out"
        ),
        "fallback_full_rebuild_reasons": [
            "no stored graph baseline",
            f"incremental unsafe: {legacy_sentinel}",
        ],
    })
    cg2_wire_repo = "graph/cg2-observability-wire"
    cg2_wire = {
        **json.loads(json.dumps(legacy_graph)),
        "extractor_version": "cg2",
        "metrics": legacy_metrics,
    }
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(cg2_wire), cg2_wire_repo, BRANCH, "d" * 40),
    )
    cg2_wire_version = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)",
            (cg2_wire_repo, BRANCH)))
    cg2_wire_obs = cg2_wire_version.get("observability") or {}
    ck("real cg2 prose fallback wire is accepted but normalized to one fixed code",
       cg2_wire_version.get("extractor_version") == "cg2"
       and cg2_wire_obs.get("fallback_full_rebuild_reasons")
       == ["incremental_internal_error"]
       and legacy_sentinel not in json.dumps(cg2_wire_obs))

    versionless_wire_repo = "graph/versionless-observability-wire"
    versionless_wire = json.loads(json.dumps(cg2_wire))
    versionless_wire.pop("extractor_version")
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(versionless_wire), versionless_wire_repo, BRANCH,
         "e" * 40),
    )
    versionless_wire_version = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)",
            (versionless_wire_repo, BRANCH)))
    ck("version-absent prose fallback wire is normalized and stamped unknown",
       versionless_wire_version.get("extractor_version") is None
       and (versionless_wire_version.get("observability") or {}).get(
           "fallback_full_rebuild_reasons")
       == ["incremental_internal_error"]
       and legacy_sentinel not in json.dumps(
           versionless_wire_version.get("observability") or {}))

    before_current_prose = coordinate_snapshot(cg2_wire_repo)
    current_prose = json.loads(json.dumps(cg2_wire))
    current_prose["extractor_version"] = C.EXTRACTOR_VERSION
    current_prose_rejected = False
    try:
        app(
            "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
            (json.dumps(current_prose), cg2_wire_repo, BRANCH, "f" * 40),
        )
    except psycopg2.Error as exc:
        current_prose_rejected = (
            exc.pgcode == "22023"
            and "graph observability" in str(exc)
        )
    ck("the identical cg4 prose wire is rejected before coordinate mutation",
       current_prose_rejected
       and coordinate_snapshot(cg2_wire_repo) == before_current_prose)

    malformed_legacy_fallbacks = (
        ("non-array", "no stored graph baseline"),
        ("more than sixteen entries", ["legacy"] * 17),
        ("non-string entry", ["legacy", {"detail": legacy_sentinel}]),
    )
    for label, malformed_value in malformed_legacy_fallbacks:
        before_malformed = coordinate_snapshot(cg2_wire_repo)
        malformed_wire = json.loads(json.dumps(cg2_wire))
        malformed_wire["metrics"]["fallback_full_rebuild_reasons"] = (
            malformed_value
        )
        malformed_rejected = False
        try:
            app(
                "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                (json.dumps(malformed_wire), cg2_wire_repo, BRANCH,
                 "7" * 40),
            )
        except psycopg2.Error as exc:
            malformed_rejected = (
                exc.pgcode == "22023"
                and "graph observability" in str(exc)
            )
        ck(f"legacy fallback {label} is rejected before coordinate mutation",
           malformed_rejected
           and coordinate_snapshot(cg2_wire_repo) == before_malformed)

    ck("legacy fallback prose never appears in persisted observability",
       admin_rows(
           "SELECT count(*) FROM core.graph_version "
           "WHERE observability::text LIKE %s",
           (f"%{legacy_sentinel}%",),
       )[0][0] == 0)

    explicit_cg1 = json.loads(json.dumps(legacy_graph))
    explicit_cg1["extractor_version"] = "cg1"
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(explicit_cg1), legacy_repo, BRANCH, "a" * 40),
    )
    explicit_cg1_version = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)", (legacy_repo, BRANCH)))
    ck("explicit historical cg1 full ingest records cg1 behind cg4",
       explicit_cg1_version.get("extractor_version") == "cg1"
       and explicit_cg1_version.get("current_extractor_version") == "cg4")

    # Cross-generation and version-absent patches fail before touching rows.
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(current_graph), legacy_repo, BRANCH, "b" * 40),
    )
    before_cross_generation_patch = coordinate_snapshot(legacy_repo)
    explicit_cg3_patch = json.loads(json.dumps(explicit_cg3))
    explicit_cg3_patch["expected_base_sha"] = "b" * 40
    explicit_cg3_patch["expected_base_revision"] = before_cross_generation_patch[
        "version"]["graph_revision"]
    explicit_cg3_patch["nodes"][0]["content_hash"] = "c" * 40
    cg3_patch_rejected = False
    try:
        app(
            "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
            (json.dumps(explicit_cg3_patch), legacy_repo, BRANCH,
             ["legacy.py"], [], "c" * 40),
        )
    except psycopg2.Error as exc:
        cg3_patch_rejected = (
            exc.pgcode == "22023"
            and "extractor_version does not match stored coordinate" in str(exc)
        )
    ck("cg3 patch cannot partially overwrite a cg4 coordinate",
       cg3_patch_rejected
       and coordinate_snapshot(legacy_repo) == before_cross_generation_patch)

    before_versionless_patch = coordinate_snapshot(legacy_repo)
    legacy_patch = json.loads(json.dumps(legacy_graph))
    legacy_patch["expected_base_sha"] = "b" * 40
    legacy_patch["expected_base_revision"] = before_versionless_patch[
        "version"]["graph_revision"]
    legacy_patch["nodes"][0]["content_hash"] = "3" * 40
    versionless_patch_rejected = False
    try:
        app(
            "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
            (json.dumps(legacy_patch), legacy_repo, BRANCH,
             ["legacy.py"], [], "3" * 40),
        )
    except psycopg2.Error as exc:
        versionless_patch_rejected = (
            exc.pgcode == "22023"
            and "extractor_version does not match stored coordinate" in str(exc)
        )
    ck("version-absent patch cannot downgrade a cg4 coordinate",
       versionless_patch_rejected
       and coordinate_snapshot(legacy_repo) == before_versionless_patch)

    # Producer and schema-contract generations are a strict pair. Accepting a
    # cg3 payload which claims v2 would falsely advertise first-class
    # uncertainty; accepting cg4 with v1 would silently discard it. Both
    # mismatches must fail before replacing the valid current coordinate.
    for label, producer, contract_version in (
        ("cg3 cannot claim the cg4/v2 schema contract", "cg3", 2),
        ("cg4 cannot downgrade to the historical v1 schema contract", "cg4", 1),
    ):
        before_contract_mismatch = coordinate_snapshot(legacy_repo)
        mismatched = json.loads(json.dumps(legacy_graph))
        mismatched["extractor_version"] = producer
        mismatched["metrics"] = {
            "schema_contract_version": contract_version,
        }
        mismatch_rejected = False
        try:
            app(
                "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                (json.dumps(mismatched), legacy_repo, BRANCH, "6" * 40),
            )
        except psycopg2.Error as exc:
            mismatch_rejected = (
                exc.pgcode == "22023"
                and "schema_contract_version does not match" in str(exc)
            )
        ck(
            f"{label} and rejection is atomic",
            mismatch_rejected
            and coordinate_snapshot(legacy_repo)
            == before_contract_mismatch,
        )

    # The two fixed ambiguity-scope tokens are also producer generations, not
    # interchangeable prose.  Reject cross-generation claims atomically even
    # though the closed validator recognizes both during schema-first rollout.
    for label, producer, contract_version, scope in (
        (
            "cg3 cannot claim cg4 local-reference ambiguity coverage",
            "cg3",
            1,
            (
                "retained multi-definer resources, emitted canonical-key "
                "collisions, and local import candidate ambiguity"
            ),
        ),
        (
            "cg4 cannot claim the historical resolved-fan-out scope",
            "cg4",
            2,
            (
                "retained multi-definer resources, emitted canonical-key "
                "collisions, and resolved import fan-out"
            ),
        ),
    ):
        before_scope_mismatch = coordinate_snapshot(legacy_repo)
        mismatched_scope = json.loads(json.dumps(legacy_graph))
        mismatched_scope["extractor_version"] = producer
        mismatched_scope["metrics"] = {
            "schema_contract_version": contract_version,
            "ambiguity_detection_scope": scope,
        }
        scope_rejected = False
        try:
            app(
                "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                (json.dumps(mismatched_scope), legacy_repo, BRANCH, "6" * 40),
            )
        except psycopg2.Error as exc:
            scope_rejected = (
                exc.pgcode == "22023"
                and "ambiguity_detection_scope does not match" in str(exc)
            )
        ck(
            f"{label} and rejection is atomic",
            scope_rejected
            and coordinate_snapshot(legacy_repo)
            == before_scope_mismatch,
        )

    future_failed = False
    future = json.loads(json.dumps(current_graph))
    future["extractor_version"] = "cg999"
    legacy_before_future = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)", (legacy_repo, BRANCH)))
    try:
        app(
            "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
            (json.dumps(future), legacy_repo, BRANCH, "4" * 40),
        )
    except psycopg2.Error as exc:
        future_failed = (
            exc.pgcode == "22023"
            and "unsupported graph extractor_version" in str(exc)
        )
    legacy_after_future = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)", (legacy_repo, BRANCH)))
    ck("unknown/future producer version fails before replacing the coordinate",
       future_failed
       and legacy_after_future.get("graph_hash")
       == legacy_before_future.get("graph_hash")
       and legacy_after_future.get("commit_sha")
       == legacy_before_future.get("commit_sha"))

    incomplete_failed = False
    incomplete_cg3 = {
        "extractor_version": "cg3",
        "metrics": {"schema_contract_version": 1},
        "nodes": [
            {"id": "api_type::Legacy", "kind": "api_type",
             "path": "schema.graphql"},
        ],
        "edges": [],
    }
    try:
        app(
            "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
            (json.dumps(incomplete_cg3), legacy_repo, BRANCH, "5" * 40),
        )
    except psycopg2.Error as exc:
        incomplete_failed = (
            exc.pgcode == "22023"
            and "cg3+ resource nodes require" in str(exc)
        )
    ck("cg3 producer cannot omit first-class resource metadata",
       incomplete_failed)

    # Generation 15 changes the DB hash projection by adding node/edge
    # uncertainty fields. Simulate a pre-upgrade current-cg3 coordinate whose
    # two persisted hash copies use the old algorithm, apply the real gate
    # module twice, and prove it becomes honestly unknown while an already-cg4
    # coordinate keeps its new hash. This is the production schema-first
    # migration shape, not a Python-only assertion.
    hash_rollout_repo = "graph/cg3-hash-rollout"
    app(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(explicit_cg3), hash_rollout_repo, BRANCH, "7" * 40),
    )
    old_hash_sentinel = "0" * 64
    admin_exec(
        "SELECT core.mark_governed_write('graph_version'); "
        "UPDATE core.graph_version "
        "SET graph_hash=%s, "
        "observability=jsonb_set("
        "COALESCE(observability,'{}'::jsonb) "
        "#- '{persistence,graph_hash_contract}',"
        "'{persisted_graph_hash}',to_jsonb(%s::text),true) "
        "WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s",
        (
            old_hash_sentinel,
            old_hash_sentinel,
            hash_rollout_repo,
            BRANCH,
        ),
    )
    # Hosted App tenants can be discoverable only through installation_account
    # (production veripsa_app has no per-tenant credential). Seed that second
    # real tenancy shape directly, with its own historical hash claim.
    app_only_account = "ACCT-GH-CG4-HASH-ROLLOUT"
    app_only_repo = "graph/cg3-hash-rollout-app-only"
    admin_exec(
        "SELECT set_config('core.current_account',%s,true); "
        "SELECT core.mark_governed_write('account'); "
        "INSERT INTO core.account(account_id,display_name) VALUES (%s,%s); "
        "INSERT INTO core.installation_account(installation_id,account_id) "
        "VALUES (%s,%s); "
        "SELECT core.mark_governed_write('graph_version'); "
        "INSERT INTO core.graph_version("
        "account_id,repo,branch,commit_sha,node_count,edge_count,"
        "extractor_version,graph_hash,observability,graph_revision,"
        "semantic_ref_version"
        ") VALUES (%s,%s,%s,%s,0,0,'cg3',%s,"
        "jsonb_build_object('persisted_graph_hash',%s::text),"
        "nextval('core.graph_revision_seq'),1)",
        (
            app_only_account,
            app_only_account,
            "App-only cg4 hash rollout fixture",
            "cg4-hash-rollout-installation",
            app_only_account,
            app_only_account,
            app_only_repo,
            BRANCH,
            "6" * 40,
            old_hash_sentinel,
            old_hash_sentinel,
        ),
    )
    app_only_has_no_credential = admin_rows(
        "SELECT count(*) FROM core.credential WHERE account_id=%s",
        (app_only_account,),
    )[0][0] == 0
    current_hash_before_reapply = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)", (REPO, BRANCH))
    ).get("graph_hash")
    hash_rollout_reapply_ok = True
    hash_rollout_error = ""
    hash_rollout_versions = []
    hash_rollout_session_pins = []
    schema_session_sentinel = "schema-apply-pin-restored"
    for _attempt in range(2):
        run = subprocess.run(
            [
                "psql",
                f"postgresql://veripsa_migrator@localhost/{DB}",
                "-v",
                "ON_ERROR_STOP=1",
                "-q",
                "-At",
                "-c",
                (
                    "SELECT set_config('core.current_account',"
                    f"'{schema_session_sentinel}',false)"
                ),
                "-f",
                "db/schema/30_gate.sql",
                "-c",
                "SELECT current_setting('core.current_account',true)",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if run.returncode:
            hash_rollout_reapply_ok = False
            hash_rollout_error = run.stderr[-800:]
            break
        hash_rollout_session_pins.append(
            (run.stdout.strip().splitlines() or [""])[-1]
        )
        hash_rollout_versions.append({
            "credential": obj(
                app(
                    "SELECT core.coordinate_graph_sha(%s,%s)",
                    (hash_rollout_repo, BRANCH),
                )
            ),
            "installation": account_graph_version(
                app_only_account, app_only_repo
            ),
        })
    current_hash_after_reapply = obj(
        app("SELECT core.coordinate_graph_sha(%s,%s)", (REPO, BRANCH))
    ).get("graph_hash")
    ck(
        "cg4 schema apply clears both stale cg3 hash claims and is idempotent "
        f"(versions={hash_rollout_versions!r}, error={hash_rollout_error!r})",
        hash_rollout_reapply_ok
        and len(hash_rollout_versions) == 2
        and app_only_has_no_credential
        and all(
            version.get("graph_hash") is None
            for snapshot in hash_rollout_versions
            for version in snapshot.values()
        )
        and all(
            "persisted_graph_hash"
            not in (version.get("observability") or {})
            for snapshot in hash_rollout_versions
            for version in snapshot.values()
        ),
    )
    ck(
        "cg4 hash migration restores the schema session tenant pin",
        hash_rollout_session_pins
        == [schema_session_sentinel, schema_session_sentinel],
    )
    ck(
        f"cg4 schema reapply preserves current hashes "
        f"(error={hash_rollout_error!r})",
        isinstance(current_hash_before_reapply, str)
        and current_hash_after_reapply == current_hash_before_reapply,
    )

    inventory = obj(app("SELECT core.graph_schema_inventory()"))
    ck("SQL inventory formally marks column edges evidence-only with no persistence loss",
       set(inventory["evidence_only_edge_kinds"]) == {"alters_col", "queries_col"}
       and inventory["persistence_losses"] == [])
    expected_observability_substrates = (
        set(C.SUBSTRATE_CONTRACTS) | {"ambiguous", "unresolved"}
    )
    ck("observability kind/substrate allowlists use the extractor inventory exactly",
       inventory["extractor_version"] == C.EXTRACTOR_VERSION == "cg4"
       and set(inventory["extractor_node_kinds"]) == C.EXTRACTOR_NODE_KINDS
       and len(inventory["extractor_node_kinds"]) == len(C.EXTRACTOR_NODE_KINDS)
       and set(inventory["extractor_edge_kinds"]) == C.EXTRACTOR_EDGE_KINDS
       and len(inventory["extractor_edge_kinds"]) == len(C.EXTRACTOR_EDGE_KINDS)
       and set(inventory["observability_substrates"])
       == expected_observability_substrates
       and len(inventory["observability_substrates"])
       == len(expected_observability_substrates))
    ck("SQL and App fallback reason-code allowlists are exact and duplicate-free",
       set(inventory["observability_fallback_reason_codes"])
       == I._FALLBACK_FULL_REBUILD_REASON_CODES
       and len(inventory["observability_fallback_reason_codes"])
       == len(I._FALLBACK_FULL_REBUILD_REASON_CODES))

    contract = SC.check_schema_contract(f"postgresql://veripsa_app@localhost/{DB}")
    ck(f"boot schema contract accepts the cg4 functions, columns and exact kind constraints "
       f"(violations={contract.violations!r})",
       contract.healthy and not contract.violations)

    # Expand-first migration proof: emulate a populated, partially-applied DB.
    # The malformed kind walls deliberately contain the old sentinel probes
    # (iac_resource/role_feature and both column edges) while omitting another
    # required kind. An exact-set guard must repair them; the old sentinel-only
    # guard would incorrectly skip them.
    admin_exec(
        "DELETE FROM core.code_node WHERE node_kind NOT IN "
        "('file','def','class','table','column','config_file','config_key')"
    )
    admin_exec(
        "DELETE FROM core.code_edge WHERE edge_kind NOT IN "
        "('contains','imports','queries','alters','reads_config')"
    )
    admin_exec("DELETE FROM core.graph_version")
    legacy_rows = admin_rows("SELECT count(*) FROM core.code_node")[0][0]
    admin_exec(
        "ALTER TABLE core.code_node DROP CONSTRAINT code_node_kind_check; "
        "ALTER TABLE core.code_node ADD CONSTRAINT code_node_kind_check CHECK "
        "(node_kind=ANY(ARRAY['file','def','class','table','column','config_file','config_key',"
        "'iac_resource','role_feature']))"
    )
    admin_exec(
        "ALTER TABLE core.code_edge DROP CONSTRAINT code_edge_kind_check; "
        "ALTER TABLE core.code_edge ADD CONSTRAINT code_edge_kind_check CHECK "
        "(edge_kind=ANY(ARRAY['contains','imports','queries','alters','reads_config',"
        "'alters_col','queries_col']))"
    )
    admin_exec(
        "ALTER TABLE core.graph_version DROP CONSTRAINT graph_version_graph_hash_shape; "
        "ALTER TABLE core.graph_version ADD CONSTRAINT graph_version_graph_hash_shape CHECK "
        "(graph_hash IS NULL OR (length(graph_hash)=32 AND graph_hash ~ '^[0-9a-f]{32}$'))"
    )
    mig_dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    reapplied = True
    reapply_error = ""
    for module in ("db/schema/20_core.sql", "db/schema/30_gate.sql"):
        run = subprocess.run(
            ["psql", mig_dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", module],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if run.returncode:
            reapplied = False
            reapply_error = run.stderr[-800:]
            break
    defs = {
        row[0]: (row[1], row[2]) for row in admin_rows(
            "SELECT conname,pg_get_constraintdef(oid),convalidated FROM pg_constraint "
            "WHERE conrelid IN ('core.code_node'::regclass,'core.code_edge'::regclass,"
            "'core.graph_version'::regclass) "
            "AND conname IN ('code_node_kind_check','code_edge_kind_check',"
            "'graph_version_graph_hash_shape')"
        )
    }
    ck(f"expand-first reapply exactly repairs partial constraints without losing legacy rows ({reapply_error})",
       reapplied
       and admin_rows("SELECT count(*) FROM core.code_node")[0][0] == legacy_rows
       and "api_operation" in defs.get("code_node_kind_check", ("", False))[0]
       and "calls" in defs.get("code_edge_kind_check", ("", False))[0]
       and "64" in defs.get("graph_version_graph_hash_shape", ("", False))[0]
       and all(validated for _definition, validated in defs.values()))

    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("GRAPH PERSISTENCE CONTRACT DB GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
