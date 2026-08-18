#!/usr/bin/env python3
"""Offline contract test for the Phase 2 Rails/auth/Sidekiq audit indexes.

The fixture is synthetic and intentionally small. It proves one statically
resolved route -> controller -> authorization -> service -> enqueue -> worker
path while pinning conservative unresolved behavior for dynamic and ambiguous
Ruby constructs. No GitLab checkout, network service, or database is used.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "hunt_phase2"
sys.path.insert(0, str(ROOT))


def _load_builder():
    try:
        from tools.hunt_index import build_audit_indexes
    except (ImportError, ModuleNotFoundError) as exc:
        print(
            "  [BLOCKED] tools.hunt_index.build_audit_indexes is not available yet; "
            "the Phase 2 index implementation must provide that public API."
        )
        print(f"  [BLOCKED] import error: {type(exc).__name__}: {exc}")
        return None
    return build_audit_indexes


def _line_of(relative_path: str, needle: str) -> int:
    lines = (FIXTURE / relative_path).read_text(encoding="utf-8").splitlines()
    matches = [number for number, line in enumerate(lines, 1) if needle in line]
    if len(matches) != 1:
        raise AssertionError(
            f"{relative_path}: expected one line containing {needle!r}, got {matches}"
        )
    return matches[0]


def _line_occurrence(relative_path: str, needle: str, occurrence: int) -> int:
    """Return one explicitly selected duplicate occurrence (1-based)."""
    lines = (FIXTURE / relative_path).read_text(encoding="utf-8").splitlines()
    matches = [number for number, line in enumerate(lines, 1) if needle in line]
    if occurrence < 1 or occurrence > len(matches):
        raise AssertionError(
            f"{relative_path}: expected occurrence {occurrence} of {needle!r}, "
            f"got matches {matches}"
        )
    return matches[occurrence - 1]


def _first(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in record:
            return record[name]
    return None


def _file_of(record: dict[str, Any]) -> str:
    value = _first(
        record,
        "source_file",
        "enqueue_source_file",
        "worker_file",
        "file",
        "path",
    )
    return str(value or "").replace("\\", "/")


def _line_of_record(record: dict[str, Any]) -> Any:
    return _first(
        record,
        "source_line",
        "enqueue_line",
        "perform_method_line",
        "line",
    )


def _blob(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _find(
    records: Iterable[dict[str, Any]],
    description: str,
    predicate,
) -> dict[str, Any]:
    matches = [record for record in records if predicate(record)]
    if len(matches) != 1:
        raise AssertionError(
            f"{description}: expected exactly one record, got {len(matches)}; "
            f"records={_blob(matches or list(records))[:3000]}"
        )
    return matches[0]


def _route(
    routes: list[dict[str, Any]],
    verb: str,
    path: str,
    action: str,
) -> dict[str, Any]:
    return _find(
        routes,
        f"route {verb} {path} -> {action}",
        lambda record: str(_first(record, "http_verb", "verb") or "").upper() == verb
        and _first(record, "path", "route_path") == path
        and _first(record, "action") == action,
    )


def _class_name(record: dict[str, Any]) -> str:
    return str(
        _first(
            record,
            "worker_class",
            "class_name",
            "resolved_worker_class",
            "receiver_class_name",
            "name",
        )
        or ""
    )


def _check_schema(indexes: dict[str, Any]) -> None:
    expected = {
        "extractor_version",
        "rails_routes",
        "authorization_calls",
        "sidekiq_jobs",
        "execution_paths",
        "warnings",
    }
    missing = expected - set(indexes)
    assert not missing, f"index result missing keys: {sorted(missing)}"
    assert indexes["extractor_version"] == "hunt-ruby-rails-v1"
    assert isinstance(indexes["rails_routes"], list)
    assert isinstance(indexes["authorization_calls"], list)
    assert isinstance(indexes["execution_paths"], list)
    assert isinstance(indexes["warnings"], list)
    jobs = indexes["sidekiq_jobs"]
    assert isinstance(jobs, dict)
    assert isinstance(jobs.get("workers"), list)
    assert isinstance(jobs.get("enqueue_sites"), list)


def _check_routes(indexes: dict[str, Any]) -> None:
    routes = indexes["rails_routes"]

    create = _route(routes, "POST", "/api/v1/project_exports", "create")
    assert _file_of(create).endswith("config/routes.rb")
    assert _line_of_record(create) == _line_of(
        "config/routes.rb", "resources :project_exports"
    )
    assert _first(create, "controller") in {
        "api/v1/project_exports",
        "Api::V1::ProjectExportsController",
    }
    assert _first(create, "namespace_stack", "namespaces") == ["api", "v1"]
    assert not _first(create, "unresolved_reason")

    show = _route(routes, "GET", "/api/v1/project_exports/:id", "show")
    assert not _first(show, "unresolved_reason")

    member_cases = [
        ("GET", "download", "download"),
        ("POST", "schedule", "schedule"),
        ("POST", "unknown", "unknown"),
        ("PUT", "restart", "restart"),
        ("PATCH", "rename", "rename"),
        ("DELETE", "cancel", "cancel"),
    ]
    for verb, suffix, action in member_cases:
        route = _route(
            routes,
            verb,
            f"/api/v1/project_exports/:id/{suffix}",
            action,
        )
        assert _line_of_record(route) == _line_of(
            "config/routes.rb", f"{verb.lower()} :{suffix}"
        )
        assert not _first(route, "unresolved_reason")

    for action in (
        "status",
        "ambiguous",
        "namespace_shadow",
        "absolute_namespace",
        "filtered_shadow",
        "double_authorization",
        "dynamic_authorization",
    ):
        verb = "GET" if action == "status" else "POST"
        route = _route(
            routes,
            verb,
            f"/api/v1/project_exports/{action}",
            action,
        )
        assert not _first(route, "unresolved_reason")

    singleton = _route(routes, "POST", "/api/v1/project_import", "create")
    assert _first(singleton, "controller") in {
        "api/v1/project_imports",
        "Api::V1::ProjectImportsController",
    }

    scoped = _route(
        routes,
        "POST",
        "/legacy/project_exports/:id/retry",
        "retry",
    )
    assert _first(scoped, "controller") in {
        "legacy/project_exports",
        "Legacy::ProjectExportsController",
    }
    assert _line_of_record(scoped) == _line_of(
        "config/routes.rb", 'post "project_exports/:id/retry"'
    )

    detached = _find(
        routes,
        "detached route file has unknown inclusion context",
        lambda record: _file_of(record).endswith(
            "config/routes/admin_routes.rb"
        ),
    )
    assert _first(detached, "unresolved_reason"), detached
    assert "inclusion context is unknown" in _blob(detached)
    assert not _first(detached, "controller_file"), detached
    assert not _first(detached, "action_line"), detached

    dynamic = _find(
        routes,
        "dynamic route",
        lambda record: _line_of_record(record)
        == _line_of("config/routes.rb", "get dynamic_export_path"),
    )
    assert _first(dynamic, "unresolved_reason"), dynamic
    assert not _first(dynamic, "controller")
    assert not _first(dynamic, "action")

    for needle in (
        "resources :dynamic_path_exports",
        "resources :dynamic_controller_exports",
        "resources :module_exports",
        "resources :custom_param_exports",
        "namespace :dynamic_namespace",
        'scope "/dynamic_module"',
        'scope "/nested_scope"',
        "scope dynamic_scope_path",
        "namespace dynamic_namespace_name",
        "project_export_routes",
        'client.get "/not_a_rails_route"',
        "  draw do",
    ):
        source_line = _line_of("config/routes.rb", needle)
        unresolved = _find(
            routes,
            f"dynamic route option {needle}",
            lambda record, source_line=source_line: _line_of_record(record)
            == source_line,
        )
        assert _first(unresolved, "unresolved_reason"), unresolved
        assert _first(unresolved, "confidence") == "low", unresolved
        assert not _first(unresolved, "action"), unresolved
        assert not any(
            _line_of_record(record) == source_line
            and not _first(record, "unresolved_reason")
            for record in routes
        ), f"dynamic option was incorrectly emitted as resolved: {unresolved}"


def _check_authorization(indexes: dict[str, Any]) -> None:
    calls = indexes["authorization_calls"]
    controller = "app/controllers/api/v1/project_exports_controller.rb"

    static_call = _find(
        calls,
        "create authorize! call",
        lambda record: _file_of(record).endswith(controller)
        and _first(record, "enclosing_method", "method") == "create"
        and _first(record, "call_name", "name") == "authorize!",
    )
    assert _line_of_record(static_call) == _line_occurrence(
        controller, "authorize!(:export_project, current_project)", 1
    )
    assert "ProjectExportsController" in str(
        _first(
            static_call,
            "enclosing_class_or_module",
            "enclosing_class",
            "class_name",
        )
    )
    assert "export_project" in str(_first(static_call, "inferred_action", "action"))
    assert "current_project" in str(
        _first(static_call, "inferred_resource", "resource")
    )
    assert "export_project" in _blob(
        _first(static_call, "raw_arguments", "arguments")
    )
    assert not _first(static_call, "unresolved_reason")

    dynamic = _find(
        calls,
        "dynamic authorize! call",
        lambda record: _file_of(record).endswith(controller)
        and _first(record, "enclosing_method", "method")
        == "dynamic_authorization"
        and _first(record, "call_name", "name") == "authorize!",
    )
    assert _line_of_record(dynamic) == _line_of(
        controller,
        "authorize!(params[:permission], resource_for(params[:resource_type]))",
    )
    assert _first(dynamic, "unresolved_reason"), dynamic
    assert not _first(dynamic, "inferred_action", "action")
    assert "params[:permission]" in _blob(
        _first(dynamic, "raw_arguments", "arguments")
    )

    indexed_blob = _blob(calls)
    assert "AUTH_BODY_MARKER_MUST_NOT_APPEAR_OUTSIDE_THE_CALL_INDEX" not in indexed_blob

    expected_calls = {
        "authorize",
        "authorize!",
        "can?",
        "cannot?",
        "allowed?",
        "policy",
        "pundit_authorize",
    }
    actual_calls = {
        str(_first(record, "call_name", "name"))
        for record in calls
        if _file_of(record).endswith(controller)
    }
    assert expected_calls <= actual_calls, (
        f"missing authorization call names: {sorted(expected_calls - actual_calls)}"
    )
    assert any(
        _first(record, "receiver") == "Ability"
        and _first(record, "call_name", "name") == "allowed?"
        for record in calls
    )
    assert any(
        _first(record, "receiver") == "Ability"
        and _first(record, "call_name", "name") == "denied?"
        for record in calls
    )
    assert any(
        _first(record, "receiver") == "current_user"
        and _first(record, "call_name", "name") == "can?"
        for record in calls
    )

    oversized = _find(
        calls,
        "bounded oversized authorization receiver",
        lambda record: _first(record, "enclosing_method") == "oversized_receiver"
        and _first(record, "call_name", "name") == "can?",
    )
    assert _first(oversized, "receiver") is None, oversized
    assert "receiver exceeds" in str(
        _first(oversized, "unresolved_reason")
    ), oversized
    assert len(_blob(oversized)) < 2_000, oversized


def _check_sidekiq(indexes: dict[str, Any]) -> None:
    jobs = indexes["sidekiq_jobs"]
    workers = jobs["workers"]
    enqueue_sites = jobs["enqueue_sites"]

    export_worker = _find(
        workers,
        "ExportWorker definition",
        lambda record: _class_name(record) == "ExportWorker",
    )
    assert _file_of(export_worker).endswith("app/workers/export_worker.rb")
    assert _first(
        export_worker,
        "perform_method_line",
        "perform_line",
        "source_line",
        "line",
    ) == _line_of(
        "app/workers/export_worker.rb", "def perform(user_id, project_id)"
    )
    assert ["user_id", "project_id"] == _first(
        export_worker, "perform_parameters", "parameters", "params"
    )
    metadata = _blob(_first(export_worker, "queue_metadata", "metadata"))
    for token in ("project_export", "import_export", "low", "idempotent"):
        assert token in metadata, f"ExportWorker metadata missing {token}: {metadata}"
    worker_blob = _blob(export_worker)
    for token in ("ApplicationWorker", "Gitlab::SidekiqMiddleware"):
        assert token in worker_blob, f"ExportWorker marker missing {token}: {worker_blob}"

    scheduled_worker = _find(
        workers,
        "ScheduledExportWorker definition",
        lambda record: _class_name(record) == "ScheduledExportWorker",
    )
    assert "Sidekiq::Worker" in _blob(scheduled_worker)

    decoy_worker = _find(
        workers,
        "explicit receiver must not mark a Sidekiq worker",
        lambda record: _class_name(record) == "DecoyWorker",
    )
    assert _first(decoy_worker, "unresolved_reason"), decoy_worker
    assert _first(decoy_worker, "confidence") == "low", decoy_worker
    assert "must_not_be_indexed" not in _blob(decoy_worker)
    assert "ApplicationWorker" not in _blob(
        _first(decoy_worker, "worker_markers")
    )

    receiver_decoy = _find(
        workers,
        "short explicit class receiver must not mark a worker",
        lambda record: _class_name(record) == "ReceiverDecoyWorker",
    )
    assert _first(receiver_decoy, "unresolved_reason"), receiver_decoy
    assert _first(receiver_decoy, "confidence") == "low", receiver_decoy
    assert "must_also_not_be_indexed" not in _blob(receiver_decoy)

    async_site = _find(
        enqueue_sites,
        "ExportWorker.perform_async",
        lambda record: _first(record, "enqueue_method", "method") == "perform_async"
        and _first(record, "enclosing_class_or_module")
        == "ProjectExportService"
        and _first(record, "enclosing_method") == "execute"
        and _first(
            record,
            "resolved_worker_class",
            "receiver_class_name",
            "worker_class",
        )
        == "ExportWorker",
    )
    assert _file_of(async_site).endswith("app/services/project_export_service.rb")
    assert _line_of_record(async_site) == _line_occurrence(
        "app/services/project_export_service.rb",
        "ExportWorker.perform_async",
        1,
    )
    assert _first(async_site, "resolved_worker_class", "worker_class") == "ExportWorker"
    assert not _first(async_site, "unresolved_reason")
    assert "@user.id" in _blob(
        _first(async_site, "raw_argument_expressions", "raw_arguments", "arguments")
    )

    delayed_site = _find(
        enqueue_sites,
        "ScheduledExportWorker.perform_in",
        lambda record: _first(record, "enqueue_method", "method") == "perform_in"
        and _first(record, "enclosing_class_or_module")
        == "ProjectExportService"
        and _first(record, "enclosing_method") == "schedule"
        and _first(
            record,
            "resolved_worker_class",
            "receiver_class_name",
            "worker_class",
        )
        == "ScheduledExportWorker",
    )
    assert _first(
        delayed_site, "resolved_worker_class", "worker_class"
    ) == "ScheduledExportWorker"
    assert "5.minutes" in _blob(
        _first(
            delayed_site,
            "raw_argument_expressions",
            "raw_arguments",
            "arguments",
        )
    )

    unresolved = _find(
        enqueue_sites,
        "UnknownExportWorker.perform_async",
        lambda record: "UnknownExportWorker" in _blob(record)
        and _first(record, "enqueue_method", "method") == "perform_async",
    )
    assert _first(unresolved, "unresolved_reason"), unresolved
    assert not _first(unresolved, "resolved_worker_class", "worker_class")
    assert not _first(unresolved, "worker_file")

    for method in ("perform_at", "push", "push_bulk"):
        resolved = _find(
            enqueue_sites,
            f"resolved {method} enqueue",
            lambda record, method=method: _first(
                record, "enqueue_method", "method"
            )
            == method
            and _first(record, "resolved_worker_class", "worker_class")
            == "ExportWorker",
        )
        assert not _first(resolved, "unresolved_reason"), resolved

    delay = _find(
        enqueue_sites,
        "conservative delay enqueue",
        lambda record: _first(record, "enqueue_method", "method") == "delay",
    )
    assert _first(delay, "unresolved_reason"), delay
    assert not _first(delay, "resolved_worker_class", "worker_class")
    assert not _first(delay, "worker_file")

    relative_shadow = _find(
        enqueue_sites,
        "relative worker constant with lexical shadow",
        lambda record: _first(record, "enclosing_class_or_module")
        == "QueueScope::RelativeEnqueueService"
        and _first(record, "enclosing_method") == "execute",
    )
    assert _first(relative_shadow, "unresolved_reason"), relative_shadow
    assert "lexical scopes" in _blob(relative_shadow)
    assert not _first(relative_shadow, "resolved_worker_class", "worker_class")

    absolute_shadow = _find(
        enqueue_sites,
        "absolute worker constant bypasses lexical shadow",
        lambda record: _first(record, "enclosing_class_or_module")
        == "QueueScope::RelativeEnqueueService"
        and _first(record, "enclosing_method") == "absolute_execute",
    )
    assert not _first(absolute_shadow, "unresolved_reason"), absolute_shadow
    assert (
        _first(absolute_shadow, "resolved_worker_class", "worker_class")
        == "ExportWorker"
    )

    shadowed_push = _find(
        enqueue_sites,
        "lexically shadowed Sidekiq::Client push",
        lambda record: _first(record, "enclosing_class_or_module")
        == "QueueScope::RelativeEnqueueService"
        and _first(record, "enclosing_method") == "shadowed_push",
    )
    assert "Sidekiq::Client receiver is shadowed" in _blob(shadowed_push)
    assert not _first(shadowed_push, "resolved_worker_class", "worker_class")

    colon_style = _find(
        enqueue_sites,
        "colon-style class has its actual Ruby lexical nesting",
        lambda record: _first(record, "enclosing_class_or_module")
        == "QueueScope::ColonStyleEnqueueService"
        and _first(record, "enclosing_method") == "execute",
    )
    assert not _first(colon_style, "unresolved_reason"), colon_style
    assert (
        _first(colon_style, "resolved_worker_class", "worker_class")
        == "ExportWorker"
    )


def _check_execution_paths(indexes: dict[str, Any]) -> None:
    paths = indexes["execution_paths"]

    resolved = _find(
        paths,
        "resolved create export path",
        lambda record: record.get("status") == "resolved"
        and "ProjectExportsController" in _blob(record)
        and '"create"' in _blob(record)
        and "ProjectExportService" in _blob(record)
        and "ExportWorker" in _blob(record)
        and "perform_async" in _blob(record),
    )
    assert len(resolved.get("hops") or []) >= 4, resolved
    assert not (resolved.get("unresolved_gaps") or []), resolved
    assert "authorize!" in _blob(resolved)
    assert "config/routes.rb" in _blob(resolved)
    assert "app/workers/export_worker.rb" in _blob(resolved)
    for hop in resolved["hops"]:
        assert hop.get("relation"), hop
        assert hop.get("confidence"), hop
        assert hop.get("evidence"), hop

    scheduled = _find(
        paths,
        "resolved scheduled export path",
        lambda record: record.get("status") == "resolved"
        and '"schedule"' in _blob(record)
        and "ScheduledExportWorker" in _blob(record)
        and "perform_in" in _blob(record),
    )
    assert not (scheduled.get("unresolved_gaps") or []), scheduled

    unknown = _find(
        paths,
        "partial unknown-worker path",
        lambda record: record.get("status") == "partial"
        and '"unknown"' in _blob(record)
        and "UnknownExportWorker" in _blob(record),
    )
    assert unknown.get("unresolved_gaps"), unknown
    assert "app/workers/unknown" not in _blob(unknown)

    ambiguous = _find(
        paths,
        "partial ambiguous-service path",
        lambda record: record.get("status") == "partial"
        and '"ambiguous"' in _blob(record)
        and "ExportService" in _blob(record),
    )
    assert ambiguous.get("unresolved_gaps"), ambiguous
    assert "AlphaExportWorker" not in _blob(ambiguous)
    assert "BetaExportWorker" not in _blob(ambiguous)

    namespace_shadow = _find(
        paths,
        "partial relative service with lexical shadow",
        lambda record: record.get("status") == "partial"
        and '"namespace_shadow"' in _blob(record),
    )
    assert "ambiguous across lexical scopes" in _blob(namespace_shadow)
    assert "enqueue_to_worker_perform" not in _blob(namespace_shadow)

    absolute_namespace = _find(
        paths,
        "resolved absolute service despite lexical shadow",
        lambda record: record.get("status") == "resolved"
        and '"absolute_namespace"' in _blob(record),
    )
    assert "::NamespaceProbeService" in _blob(absolute_namespace)
    assert "ExportWorker" in _blob(absolute_namespace)
    assert not (absolute_namespace.get("unresolved_gaps") or [])

    filtered_shadow = _find(
        paths,
        "partial service shadow retained in the full universe",
        lambda record: record.get("status") == "partial"
        and '"filtered_shadow"' in _blob(record),
    )
    assert "ambiguous across lexical scopes" in _blob(filtered_shadow)

    double_authorization = [
        record
        for record in paths
        if record.get("status") == "resolved"
        and '"double_authorization"' in _blob(record)
    ]
    assert len(double_authorization) == 2, _blob(double_authorization)
    assert len(
        {record["path_id"] for record in double_authorization}
    ) == 2, double_authorization
    assert {
        "export_project",
        "download_project",
    } <= {
        token
        for record in double_authorization
        for token in ("export_project", "download_project")
        if token in _blob(record)
    }

    resolved_blob = _blob(
        [record for record in paths if record.get("status") == "resolved"]
    )
    assert "UnknownExportWorker" not in resolved_blob
    assert "dynamic_controller_action" not in resolved_blob


def _check_warnings(indexes: dict[str, Any]) -> None:
    warnings = indexes["warnings"]
    required = {"file", "line", "category", "reason", "severity"}
    for warning in warnings:
        assert required <= set(warning), f"warning schema incomplete: {warning}"

    dynamic_route_line = _line_of("config/routes.rb", "get dynamic_export_path")
    assert any(
        str(warning.get("file", "")).replace("\\", "/").endswith(
            "config/routes.rb"
        )
        and warning.get("line") == dynamic_route_line
        and "route" in str(warning.get("category", "")).lower()
        for warning in warnings
    ), f"dynamic route warning missing: {_blob(warnings)}"

    assert any(
        "UnknownExportWorker" in str(warning.get("reason", ""))
        for warning in warnings
    ), f"unknown worker warning missing: {_blob(warnings)}"
    assert any(
        "receiver exceeds the audit expression limit"
        in str(warning.get("reason", ""))
        for warning in warnings
    ), f"bounded receiver warning missing: {_blob(warnings)}"
    assert any(
        "ambiguous" in (
            str(warning.get("category", ""))
            + " "
            + str(warning.get("reason", ""))
        ).lower()
        for warning in warnings
    ), f"ambiguous service warning missing: {_blob(warnings)}"
    audit_label_line = _line_of(
        "app/controllers/api/v1/project_exports_controller.rb",
        "audit_label()",
    )
    assert any(
        str(warning.get("file", "")).replace("\\", "/").endswith(
            "app/controllers/api/v1/project_exports_controller.rb"
        )
        and warning.get("line") == audit_label_line
        and warning.get("category") == "service_call_unresolved"
        and "audit_label" in str(warning.get("reason", ""))
        for warning in warnings
    ), f"unresolved sibling call warning missing: {_blob(warnings)}"


def _check_filtered_definition_universe(_indexes: dict[str, Any]) -> None:
    builder = _load_builder()
    assert builder is not None
    filtered = builder(
        FIXTURE,
        include_patterns=(
            "config/routes.rb",
            "app/controllers/api/v1/project_exports_controller.rb",
            "app/services/project_export_service.rb",
            "app/workers/*.rb",
        ),
        exclude_patterns=("app/services/hidden/*.rb",),
    )
    filtered_path = _find(
        filtered["execution_paths"],
        "excluded shadow definition still prevents false uniqueness",
        lambda record: '"filtered_shadow"' in _blob(record),
    )
    assert filtered_path.get("status") == "partial", filtered_path
    assert "ambiguous across lexical scopes" in _blob(filtered_path)
    assert "HiddenExportWorker" not in _blob(filtered["sidekiq_jobs"])


def _check_parse_failure_definition_barrier(
    _indexes: dict[str, Any],
) -> None:
    builder = _load_builder()
    assert builder is not None
    with tempfile.TemporaryDirectory(prefix="hunt-parse-barrier-") as temp:
        target = Path(temp) / "fixture"
        shutil.copytree(FIXTURE, target)
        broken = target / "app" / "controllers" / "broken_shadow.rb"
        broken.write_text(
            "class Api::V1::ProjectExportsController\n"
            "  def create(project\n"
            "  end\n"
            "end\n",
            encoding="utf-8",
        )
        guarded = builder(target, include_patterns=(), exclude_patterns=())

    warnings = guarded["warnings"]
    assert any(
        warning.get("category") == "ruby_parse_error"
        and warning.get("file") == "app/controllers/broken_shadow.rb"
        for warning in warnings
    ), _blob(warnings)
    assert any(
        warning.get("category") == "ruby_definition_universe_incomplete"
        for warning in warnings
    ), _blob(warnings)

    create_path = _find(
        guarded["execution_paths"],
        "parse failure prevents a falsely unique controller link",
        lambda record: record.get("route", {}).get("path")
        == "/api/v1/project_exports"
        and record.get("route", {}).get("verb") == "POST",
    )
    assert create_path.get("status") == "partial", create_path
    assert "definition universe is incomplete" in _blob(create_path)
    assert not any(
        hop.get("relation") == "route_to_controller"
        for hop in create_path.get("hops", [])
    ), create_path

    export_enqueue = _find(
        guarded["sidekiq_jobs"]["enqueue_sites"],
        "parse failure prevents a falsely unique worker link",
        lambda record: record.get("receiver_class_name") == "ExportWorker"
        and record.get("enqueue_method") == "perform_async"
        and record.get("enclosing_class_or_module") == "ProjectExportService",
    )
    assert export_enqueue.get("resolved_worker_class") is None, export_enqueue
    assert "definition universe is incomplete" in _blob(export_enqueue)


def _check_final_route_cap_warning(_indexes: dict[str, Any]) -> None:
    import tools.hunt_index as hunt_index

    key = hunt_index.MethodKey("ExportsController", "create", False)
    method = hunt_index.MethodDefinition(
        key=key,
        file="app/controllers/exports_controller.rb",
        line=2,
        parameters=[],
        source_file=None,
        body_node=None,
    )
    route = {
        "source_file": "config/routes.rb",
        "source_line": 1,
        "source_column": 1,
        "http_verb": "POST",
        "path": "/exports",
        "controller": "ExportsController",
        "action": "create",
        "unresolved_reason": None,
    }
    for authorization_count, expected_paths, expected_warnings in (
        (4_999, 4_999, 0),
        (5_000, 5_000, 0),
        (5_001, 5_000, 1),
    ):
        authorizations = [
            {
                "source_file": method.file,
                "source_line": line,
                "source_column": 1,
                "call_name": "authorize!",
                "receiver": None,
                "raw_arguments": [":create", f"project_{line}"],
                "inferred_action": "create",
                "inferred_resource": f"project_{line}",
                "unresolved_reason": None,
            }
            for line in range(3, 3 + authorization_count)
        ]
        paths, warnings = hunt_index._execution_paths(
            [route],
            {0: method},
            {key: authorizations},
            {},
            {},
            {key: [method]},
        )

        assert len(paths) == expected_paths, (
            authorization_count,
            len(paths),
        )
        cap_warnings = [
            warning
            for warning in warnings
            if warning.category == "execution_path_cap_reached"
        ]
        assert len(cap_warnings) == expected_warnings, (
            authorization_count,
            [warning.as_dict() for warning in warnings],
        )


def main() -> int:
    builder = _load_builder()
    if builder is None:
        print("HUNT INDEX GATE: FAIL")
        return 1

    checks = [
        ("result schema", _check_schema),
        ("Rails route index", _check_routes),
        ("authorization call index", _check_authorization),
        ("Sidekiq worker/enqueue index", _check_sidekiq),
        ("execution path integration", _check_execution_paths),
        ("conservative unresolved warnings", _check_warnings),
        ("filtered definition universe", _check_filtered_definition_universe),
        ("parse failure definition barrier", _check_parse_failure_definition_barrier),
        ("final route execution cap warning", _check_final_route_cap_warning),
    ]
    failures: list[str] = []

    old_dsn = os.environ.get("VERIPSA_DSN")
    os.environ["VERIPSA_DSN"] = "postgresql://127.0.0.1:1/must-not-connect"
    try:
        indexes = builder(
            FIXTURE,
            include_patterns=(),
            exclude_patterns=(),
        )
    except Exception as exc:  # noqa: BLE001 - gate must report an actionable API/parse error
        print(
            "  [ERROR] build_audit_indexes failed before checks: "
            f"{type(exc).__name__}: {exc}"
        )
        print("HUNT INDEX GATE: FAIL")
        return 1
    finally:
        if old_dsn is None:
            os.environ.pop("VERIPSA_DSN", None)
        else:
            os.environ["VERIPSA_DSN"] = old_dsn

    for label, check in checks:
        try:
            check(indexes)
        except AssertionError as exc:
            failures.append(f"{label}: {exc}")
            print(f"  [FAIL] {label}: {exc}")
        except Exception as exc:  # noqa: BLE001 - preserve all gate failures
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            print(f"  [ERROR] {label}: {type(exc).__name__}: {exc}")
        else:
            print(f"  [PASS] {label}")

    print("HUNT INDEX GATE:", "PASS" if not failures else "FAIL")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
