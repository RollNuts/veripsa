#!/usr/bin/env python3
"""Veripsa GitHub App — the GRAPH-INGESTION + RECONCILIATION concern (extracted from server.py).

ONE cohesive concern lifted whole out of the webhook server (the same finer-grained split that already
carved out event_queue.py / webhook.py / health_watchdog.py / graph_freshness.py): keeping the stored
content-free code-graph IN SYNC with the repo, providing the phased background onboarding primitives, and
supporting explicit/restart PR reconciliation. These functions CALL EACH OTHER, so moving them together
resolves the intra-cluster calls in ONE place.

CALL GRAPH (who reaches whom — every arrow is an intra-module call; ingest_push is the hub everything
funnels through, because every "the graph changed" trigger ultimately rebuilds it the same way):

  boot_reconcile ──▶ _reconcile_one_repo ──▶ backfill_open_prs ──▶ handle_event*  (* = server-side router)
  _onboard_repos ──▶ backfill_repo ──┬──▶ ingest_push ──┬──▶ _incremental_ingest  (target context → touched slice)
                                     └──▶ backfill_open_prs  └──▶ _full_ingest     (clone + whole-repo rebuild)
  converge_main_graph_strict ──▶ self_heal_main_graph ──▶ ingest_push

PUBLIC ENTRY POINTS (the names server.py re-exports + the gate/CLI call):

  ingest_push(db, gh, repo, branch, sha, payload, coalesce)  record protected-branch facts and either enqueue
                                                             convergence (live) or rebuild (isolated worker/CLI)
  request_main_graph_refresh(...)                            DB-local proof + latest-wins durable enqueue
  converge_main_graph_strict(...)                            background-only authenticated HEAD convergence
  self_heal_main_graph(db, gh, repo, branch)                 background/CLI rebuild primitive when stored graph is
                                                             behind current HEAD
  backfill_open_prs / backfill_repo / _onboard_repos         explicit CLI cold-start/repair (never live ingress)
  boot_reconcile                                             after a restart, re-post checks for open PRs (idempotent)
  purge_repo                                                 OFFBOARDING: forget a repo's content-free working set

DESIGN (mirrors event_queue.py / health_watchdog.py / graph_freshness.py): this module imports NOTHING from
server.py at LOAD time (no circular import — server.py imports THIS and RE-EXPORTS these names for backward
compatibility, so server.ingest_push / server.backfill_open_prs / server.boot_reconcile / … keep working for
serve(), make_db_processor's handle_event path, and the gate). The few server-side SEAMS these functions need
are NOT part of the ingest concern and stay in server.py — the payload type-guards (_as_obj / _as_list), the
push changed-set parser (_push_changed_sets), the free-tier wall detector (_quota_result), and the webhook
ROUTER (handle_event, which backfill_open_prs re-runs per open PR). They are resolved at CALL time via the
same lazy `import server` idiom health_watchdog.py uses for graph_freshness_all (server.py is fully loaded by
the time any of these runs) — so the back-edge to handle_event never makes a module-load cycle. graph_freshness
(read by self_heal_main_graph) lives in graph_freshness.py and is imported at top level (no cycle).

THE INTRA-CLUSTER CALLS DISPATCH THROUGH THIS MODULE'S GLOBALS: ingest_push calls _full_ingest / boot_reconcile
calls backfill_open_prs by their bare module-global names — so a test that monkeypatches ingest._full_ingest /
ingest.backfill_open_prs (or the private constants ingest._MAX_INGEST_FILES / ingest._BACKFILL_PR_CAP /
ingest._BACKFILL_BRANCH_CAP / ingest._ONBOARD_REPO_CAP) takes effect on the intra-cluster path, exactly as it did
when these lived in server.

The live/background execution boundary is explicit: webhook callers never clone or extract, while isolated
convergence and operator CLI callers may select the inline rebuild path. Content-free persistence and the
module/test seams above remain unchanged.
"""
from __future__ import annotations

import io
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import tarfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

# env_int: the VALIDATED env-knob reader (fail SAFE on a typo'd cap — a non-int or out-of-range value raises a
# loud, actionable refusal that names the var + value + range, instead of crashing with a bare ValueError or
# silently misbehaving on a 0/negative cap). Same standalone/package dual-import idiom server.py uses.
try:
    from env_config import env_int  # noqa: E402
except ImportError:  # imported as a package
    from .env_config import env_int  # noqa: E402
# graph_freshness (read by self_heal_main_graph) lives in its own module and imports NOTHING from server.py, so
# it is safe to import at LOAD time here (no cycle — graph_freshness.py ← ingest.py, both ← server.py).
try:
    from graph_freshness import graph_freshness, graph_freshness_at_target  # noqa: E402
except ImportError:  # imported as a package
    from .graph_freshness import graph_freshness, graph_freshness_at_target  # noqa: E402
try:
    from delivery_deferral import IntentionalDeliveryDeferral  # noqa: E402
except ImportError:  # imported as a package
    from .delivery_deferral import IntentionalDeliveryDeferral  # noqa: E402
try:
    import graph_workspace as _graph_workspace  # noqa: E402
except ImportError:  # imported as a package
    from . import graph_workspace as _graph_workspace  # noqa: E402
# The co-change (logical-coupling) SECOND signal lives in its own leaf module (cochange.py) — split out of this
# god-file (the slice PRs #235-241 all fought over). Re-imported here by the ORIGINAL names so backfill_repo's
# bare-name populate_cochange_async call and every caller/test (ingest.populate_cochange / ingest_cochange /
# cochange_partners_for / …) resolve UNCHANGED. cochange.py is a pure leaf (no load-time import back into ingest).
try:
    from cochange import (ingest_cochange, _cochange_pairs_via_clone, populate_cochange, _cochange_scheduler,  # noqa: E402,F401
                          _cochange_populate_task, populate_cochange_async, cochange_partners_for,
                          increment_cochange_async, push_commit_filesets, push_commit_shafilesets, cochange_all)
except ImportError:  # imported as a package
    from .cochange import (ingest_cochange, _cochange_pairs_via_clone, populate_cochange, _cochange_scheduler,  # noqa: E402,F401
                           _cochange_populate_task, populate_cochange_async, cochange_partners_for,
                           increment_cochange_async, push_commit_filesets, push_commit_shafilesets, cochange_all)

# Capture the production graph-builder identity once. Two older integration gates inject a synthetic graph by
# rebinding ``code_graph_extract.build_graph``; the compatibility path below preserves that explicit seam while
# every normal runtime call goes through the killable fresh-exec child.
#
# Import by the repository path derived from THIS file when the caller exposed only ``github-app/`` on sys.path.
# Several gates and operator scripts intentionally run from another cwd with that narrow path. A bare top-level
# ``import code_graph_extract`` then fails even though the owned extractor is exactly one directory above us.
# The fallback is independent of cwd/PYTHONPATH, registers the canonical module name (so monkeypatch seams remain
# identical), and exposes the parent directory only while the extractor imports its owned sibling modules.
def _load_graph_extractor():
    try:
        import code_graph_extract as extractor
        return extractor
    except ModuleNotFoundError as exc:
        if exc.name != "code_graph_extract":
            raise

    import importlib.util
    app_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(app_dir)
    module_path = os.path.join(repo_root, "code_graph_extract.py")
    spec = importlib.util.spec_from_file_location("code_graph_extract", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load owned graph extractor at {module_path}")
    extractor = importlib.util.module_from_spec(spec)
    sys.modules["code_graph_extract"] = extractor
    inserted_root = repo_root not in sys.path
    if inserted_root:
        sys.path.insert(0, repo_root)
    try:
        spec.loader.exec_module(extractor)
    except BaseException:
        if sys.modules.get("code_graph_extract") is extractor:
            sys.modules.pop("code_graph_extract", None)
        raise
    finally:
        if inserted_root:
            try:
                sys.path.remove(repo_root)
            except ValueError:
                pass
    return extractor


_GRAPH_EXTRACTOR = _load_graph_extractor()
_ORIGINAL_BUILD_GRAPH = _GRAPH_EXTRACTOR.build_graph

# Unforgeable in-process authority for the one onboarding replay path whose caller already proved that the
# durable default-branch graph is exact for the request's branch/SHA/stable-id under its live convergence lease.
# It is deliberately an object identity, never a payload boolean/string: JSON webhooks, durable replay bodies,
# and ordinary synthetic callers cannot manufacture it.  webhook_handlers imports this exact object and checks
# it with ``is`` before suppressing the structural-PR graph wake.
_PLANNED_ONBOARDING_GRAPH_PROOF = object()
# Private result key shared with webhook_handlers. Its value must be the exact capability above, so a normal
# JSON payload/result cannot turn an arbitrary skipped replay into a durable onboarding cursor receipt.
_PLANNED_ONBOARDING_BOUNDED_UNREAD_KEY = "_veripsa_planned_onboarding_bounded_unread"

try:
    from event_budget import (  # noqa: E402
        EventBudgetExceeded,
        current_account as _event_budget_account,
        remaining as _event_budget_remaining,
    )
except ImportError:  # imported as a package
    from .event_budget import (  # noqa: E402
        EventBudgetExceeded,
        current_account as _event_budget_account,
        remaining as _event_budget_remaining,
    )


def _trace_prefix_for(payload) -> str:
    """Build the per-event trace_id log prefix off the dispatcher-stashed payload key (`_veripsa_trace_id`).
    Cycle-free lazy import (webhook is a leaf). Returns '' on a payload without one (a degraded but valid log
    line — better than a crash on a cold-start / boot-reconcile path that never reaches the dispatcher)."""
    try:
        from webhook import _trace_log_prefix
    except ImportError:
        from .webhook import _trace_log_prefix
    return _trace_log_prefix(payload)


def _trace_prefix_str(trace_id: str) -> str:
    """Build the per-event trace_id log prefix from a bare uuid hex string. Used by helpers that take `trace_id`
    as a kwarg rather than receiving the payload (e.g. self_heal_main_graph called from _pr_pre_brain). Returns
    '' when the id is empty (the same degraded-but-valid contract _trace_prefix_for / _f_trace_prefix use)."""
    try:
        from webhook import _TRACE_TAG_WIDTH
    except ImportError:
        from .webhook import _TRACE_TAG_WIDTH
    return f"trace_id={trace_id[:_TRACE_TAG_WIDTH]} " if trace_id else ""


def _server():
    """The server module, resolved at CALL time (NOT at load — server.py imports THIS, so a load-time
    `import server` here would be a circular import). server.py is fully loaded by the time any ingest
    function runs, so this just returns the already-imported module. Mirrors health_watchdog._resolve_freshness_fn's
    call-time `from server import graph_freshness_all`. Used only to reach the server-side SEAMS that are not part
    of the ingest concern (the _as_obj/_as_list payload guards, _push_changed_sets, _quota_result, and the
    handle_event router) — none of which is monkeypatched, so a fresh lookup is always correct."""
    try:
        import server as _s  # call-time import → no module-load circularity
    except ImportError:  # imported as a package
        from . import server as _s  # type: ignore
    return _s


# INCREMENTAL FAST-PATH CEILING: above this many changed+removed files in one push, fetching each file
# individually (the incremental path's per-file API calls) is no cheaper than cloning + re-extracting the
# whole repo once — so above the ceiling we fall back to the full path. Most pushes touch a handful of
# files, which is the win case the incremental path exists for.
_INCR_CAP = 400
# The heal's byte/request budget is deliberately much tighter than the
# generic incremental work cap: each patched path costs one uncompressed API
# fetch, while the full fallback is one compressed tarball.
_HEAL_PATCH_MAX_PATHS = env_int(
    "VERIPSA_HEAL_PATCH_MAX_PATHS", 40, min_value=1)
# GitHub's Compare API exposes at most 300 changed files for an entire
# comparison.  A response at the ceiling is therefore ambiguous (exactly 300
# versus truncated) and must never drive an incremental graph patch.
_GITHUB_COMPARE_FILE_CAP = 300
# GitHub's push webhook embeds at most 2048 commits.  At the ceiling the path
# union may be incomplete, even when every delivered commit entry is valid.
_GITHUB_PUSH_COMMIT_CAP = 2048
# A resource-aware incremental build may need the unchanged files which DEFINE the
# resources referenced by a changed consumer (schema, config, IaC, API contract,
# package manifest, ...).  Keep that context bounded; exceeding it is not an excuse
# to build a partial graph, it is a reason to take the exact full-build path.
_INCR_CONTEXT_CAP = 400
_SEMANTIC_REF_VERSION = 1

_RESOURCE_NODE_KINDS = frozenset({
    "table", "column", "config_key", "iac_resource", "k8s_resource",
    "api_type", "api_message", "api_service", "api_operation", "api_schema",
    "ci_script", "app_command", "job_task", "job_queue",
    "sibling_stem", "role_feature",
})
_MANIFEST_BASENAMES = frozenset({
    "package.json", "Cargo.toml", "pyproject.toml", "setup.py", "setup.cfg",
    "go.mod", ".gitattributes",
})
_RESOURCE_DEFINITION_EDGE_KINDS = frozenset(
    _RESOURCE_NODE_KINDS - {"config_key", "sibling_stem", "role_feature"}
)

# Durable observability stores only these bounded, content-free reason codes.
# Human/path/error detail remains in the returned stats/log line and must never
# cross the graph metrics persistence boundary.
_FALLBACK_FULL_REBUILD_REASON_CODES = frozenset({
    "force_push",
    "explicit_full_rebuild",
    "unpatchable_changed_set",
    "changed_set_base_mismatch",
    "compare_history_unproven",
    "graph_baseline_changed",
    "incremental_change_cap_exceeded",
    "target_sha_reader_unavailable",
    "no_stored_graph_baseline",
    "extractor_version_mismatch",
    "semantic_reference_version_mismatch",
    "stored_graph_uncertainty",
    "resource_catalog_unavailable",
    "resource_catalog_malformed",
    "file_deleted",
    "file_relocated",
    "inert_import_activated",
    "changed_path_absent_from_persisted_universe",
    "resource_definition_evidence_missing",
    "resource_definition_changed",
    "reference_conditioned_consumer_changed",
    "symmetric_pairing_member_changed",
    "bidirectional_import_member_changed",
    "resolution_context_cap_exceeded",
    "target_tree_mode_unverified",
    "unsafe_context_path",
    "context_file_missing",
    "contract_manifest_changed",
    "sibling_or_role_candidate_added",
    "ci_script_changed",
    "celery_task_changed",
    "bullmq_queue_changed",
    "job_queue_probe_failed",
    "tauri_command_changed",
    "tauri_probe_failed",
    "route_changed",
    "route_probe_failed",
    "target_resource_identity_changed",
    "ambiguous_reference_detected",
    "extractor_file_failed",
    "extractor_file_incomplete",
    "incremental_internal_error",
})

_CONTENT_FALLBACK_REASON_CODES = {
    "contract/import manifest changed": "contract_manifest_changed",
    "sibling-stem or role-feature candidate added": "sibling_or_role_candidate_added",
    "reference-conditioned CI script substrate changed": "ci_script_changed",
    "reference-conditioned Celery task substrate changed": "celery_task_changed",
    "reference-conditioned BullMQ queue substrate changed": "bullmq_queue_changed",
    "job/queue safety probe failed": "job_queue_probe_failed",
    "reference-conditioned Tauri command substrate changed": "tauri_command_changed",
    "Tauri command safety probe failed": "tauri_probe_failed",
    "route definition/reference changed": "route_changed",
    "route safety probe failed": "route_probe_failed",
}


class _IncrementalUnsafe(Exception):
    """The incremental patch would diverge from a full re-ingest for THIS push — caught by ingest_push,
    which then takes the always-correct full re-ingest. NOT an error: a deliberate fast-path bail-out."""

    def __init__(self, message: str, *, reason_code: str):
        super().__init__(message)
        self.reason_code = (
            reason_code
            if reason_code in _FALLBACK_FULL_REBUILD_REASON_CODES
            else "incremental_internal_error"
        )


def _bounded_hex_sha(value) -> str | None:
    """Canonical content-free commit identity accepted by graph writers."""
    if not isinstance(value, str) or not (1 <= len(value) <= 64):
        return None
    if any(char not in "0123456789abcdefABCDEF" for char in value):
        return None
    return value


def _complete_compare_changed_paths(
    gh, repo: str, base_sha: str, head_sha: str) -> list[str] | None:
    """Return a path-only compare result only when ancestry/completeness are proven.

    GitHub's Compare API includes at most 300 changed files for the whole
    comparison and exposes no files-truncated flag. Exactly 300 entries is
    therefore indistinguishable from a longer comparison. The strict proof
    additionally requires requested base == response base == merge base and
    the response's final commit == requested HEAD; diverged/behind/malformed
    history is a full rebuild. ``None`` means the caller must take that exact
    full-build path.
    """
    reader = getattr(gh, "compare_changed_paths_proof", None)
    if not callable(reader):
        return None
    try:
        raw = reader(repo, base_sha, head_sha)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    if (
        raw.get("base_sha") != base_sha
        or raw.get("merge_base_sha") != base_sha
        or raw.get("head_sha") != head_sha
        or raw.get("status") != "ahead"
    ):
        return None
    raw_paths = raw.get("paths")
    if (
        not isinstance(raw_paths, list)
        or len(raw_paths) >= _GITHUB_COMPARE_FILE_CAP
    ):
        return None
    paths: list[str] = []
    seen: set[str] = set()
    for path in raw_paths:
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 1024
            or "\x00" in path
            or path in seen
        ):
            return None
        seen.add(path)
        paths.append(path)
    return paths


def _push_changed_set_complete(payload: dict | None) -> bool:
    """Whether a push payload proves that its embedded changed-path set is whole.

    The webhook's ``commits`` array is capped at 2048.  ``size`` (when
    delivered) is GitHub's declared commit count and must agree with the array
    length.  Malformed commit/path arrays are not silently interpreted as an
    empty or partial diff: graph ingestion falls back to a full build.
    """
    if not isinstance(payload, dict):
        return False
    commits = payload.get("commits")
    if not isinstance(commits, list) or len(commits) >= _GITHUB_PUSH_COMMIT_CAP:
        return False
    declared_size = payload.get("size")
    if declared_size is not None and (
        isinstance(declared_size, bool)
        or not isinstance(declared_size, int)
        or declared_size < 0
        or declared_size != len(commits)
    ):
        return False
    for commit in commits:
        if not isinstance(commit, dict):
            return False
        for field in ("added", "modified", "removed"):
            paths = commit.get(field)
            if not isinstance(paths, list):
                return False
            if any(
                not isinstance(path, str)
                or not path
                or len(path) > 1024
                or "\x00" in path
                for path in paths
            ):
                return False
    return True


def _json_value(raw, fallback):
    """Decode a json/jsonb DB value while accepting psycopg's already-decoded form."""
    if raw is None:
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _validated_graph_write(db, raw, repo: str, branch: str,
                           expected_sha: str, operation: str) -> dict:
    """Validate a graph writer result and its same-transaction coordinate readback.

    Graph writes are correctness boundaries, not best-effort telemetry.  Quota,
    stale/no-op and malformed sentinel results must never be reported as a
    successful ``full``/``patch`` mode.  The coordinate readback is required even
    for the full writer (whose result intentionally omits ``commit_sha``).
    """
    result = _json_value(raw, None)
    expected = str(expected_sha or "")[:64]
    if not expected or not all(
            char in "0123456789abcdefABCDEF" for char in expected):
        raise RuntimeError(f"{operation} graph write expected an invalid commit_sha")
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError(f"{operation} graph writer did not confirm ok=true")
    if result.get("semantic_ref_version") != _SEMANTIC_REF_VERSION:
        raise RuntimeError(
            f"{operation} graph writer did not confirm semantic reference "
            f"version {_SEMANTIC_REF_VERSION}"
        )
    graph_hash = result.get("graph_hash")
    if not (
        isinstance(graph_hash, str)
        and len(graph_hash) == 64
        and all(char in "0123456789abcdefABCDEF" for char in graph_hash)
    ):
        raise RuntimeError(f"{operation} graph writer returned an invalid graph_hash")
    returned_sha = result.get("commit_sha")
    if (
        (operation == "patch" and returned_sha != expected)
        or (operation != "patch" and returned_sha is not None
            and returned_sha != expected)
    ):
        raise RuntimeError(f"{operation} graph writer returned an unexpected commit_sha")

    coordinate = _json_value(
        db("SELECT core.coordinate_graph_sha(%s,%s)", (repo, branch)),
        None,
    )
    if not isinstance(coordinate, dict):
        raise RuntimeError(f"{operation} graph coordinate readback is malformed")
    if coordinate.get("commit_sha") != expected:
        raise RuntimeError(f"{operation} graph coordinate did not persist the expected commit_sha")
    if coordinate.get("graph_hash") != graph_hash:
        raise RuntimeError(f"{operation} graph coordinate hash does not match the writer result")
    if coordinate.get("semantic_ref_version") != _SEMANTIC_REF_VERSION:
        raise RuntimeError(
            f"{operation} graph coordinate did not persist semantic reference "
            f"version {_SEMANTIC_REF_VERSION}"
        )
    return result


def _resource_catalog(db, repo: str, branch: str) -> dict:
    """The persisted, content-free resource/manifest context for one coordinate.

    Incremental extraction must never infer the repo's known-resource universe from
    changed files alone.  The DB contract returns paths only (plus bounded resource
    metadata); file contents are fetched at the *target sha* below, so baseline bytes
    can never leak into the end-state graph.
    """
    raw = db("SELECT core.coordinate_resource_catalog(%s,%s)", (repo, branch))
    required = {
        "resources", "context_paths", "definition_paths",
        "reference_conditioned_paths", "pairing_paths",
        "bidirectional_import_paths",
    }
    if raw is None:
        raise _IncrementalUnsafe(
            "resource catalog unavailable — full re-ingest required",
            reason_code="resource_catalog_unavailable",
        )
    catalog = _json_value(raw, None)
    if not isinstance(catalog, dict) or not required.issubset(catalog):
        raise _IncrementalUnsafe(
            "resource catalog malformed — full re-ingest required",
            reason_code="resource_catalog_malformed",
        )
    for key in required:
        if not isinstance(catalog.get(key), list):
            raise _IncrementalUnsafe(
                f"resource catalog field {key} is malformed — full re-ingest required",
                reason_code="resource_catalog_malformed",
            )
    return catalog


def _semantic_ref_key(value) -> str:
    """PostgreSQL core._semantic_ref_key parity (SHA-256 over exact UTF-8)."""
    if not isinstance(value, str):
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resource_summary(resources: list[dict], edges: list[dict],
                      *, ignored_paths=()) -> list[dict]:
    """Canonical first-class resource state with path-owned changes removed.

    The persisted catalog is the baseline truth; a full target-sha extraction is
    summarized through this same function.  Comparing the two after removing
    changed-source contributions proves that an incremental patch will not need
    to mutate a node or Edge owned by an unchanged file.  This retains negative
    facts such as multi-definer suppression instead of reconstructing a known set
    from only the changed files.
    """
    ignored = {str(path) for path in ignored_paths if path}
    grouped: dict[tuple[str, str, str], dict] = {}
    keys: dict[str, list[tuple[str, str, str]]] = {}
    for raw in resources:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "")
        key = str(raw.get("canonical_key") or "")
        semantic_key = str(raw.get("semantic_key") or "")
        if not (
            len(semantic_key) == 64
            and all(char in "0123456789abcdef" for char in semantic_key)
        ):
            semantic_key = _semantic_ref_key(key)
        node_id = str(raw.get("id") or "")
        if (
            kind not in _RESOURCE_NODE_KINDS
            or not semantic_key
            or not node_id
        ):
            continue
        identity = (kind, semantic_key, node_id)
        grouped[identity] = {
            "id": node_id,
            "kind": kind,
            "semantic_key": semantic_key,
            "path": raw.get("path"),
            "scope": raw.get("scope"),
            "extractor": raw.get("extractor"),
            "confidence": raw.get("confidence"),
            "provenance": raw.get("provenance"),
            "language": raw.get("language"),
            "definition_paths": set(),
            "reference_paths": set(),
        }
        keys.setdefault(semantic_key, []).append(identity)

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        key = str(edge.get("semantic_dst_key") or "")
        if not (
            len(key) == 64
            and all(char in "0123456789abcdef" for char in key)
        ):
            key = _semantic_ref_key(edge.get("dst"))
        # Edge sources are document paths. Git paths may contain ``::``;
        # keeping the value verbatim avoids confusing one with a generated
        # resource/symbol id.
        src = str(edge.get("src") or "")
        if not key or not src or src in ignored:
            continue
        field = None
        if edge.get("kind") in {"alters", "alters_col"}:
            field = "definition_paths"
        elif edge.get("kind") in {"queries", "queries_col", "reads_config"}:
            field = "reference_paths"
        if field:
            for identity in keys.get(key, ()):
                grouped[identity][field].add(src)

    out = []
    for value in grouped.values():
        value["definition_paths"] = sorted(value["definition_paths"])
        value["reference_paths"] = sorted(value["reference_paths"])
        out.append(value)
    return sorted(
        out,
        key=lambda value: (
            value["kind"], value["semantic_key"], value["id"],
            json.dumps(value, sort_keys=True, separators=(",", ":")),
        ),
    )


def _catalog_resource_summary(catalog: dict, *, ignored_paths=()) -> list[dict]:
    """Normalize the SQL catalog through the same resource-summary contract."""
    edges = []
    for resource in catalog.get("resources", []):
        if not isinstance(resource, dict):
            continue
        key = resource.get("canonical_key")
        semantic_key = resource.get("semantic_key")
        for path in resource.get("definition_paths", []):
            edges.append({
                "src": path,
                "dst": key,
                "semantic_dst_key": semantic_key,
                "kind": "alters",
            })
        for path in resource.get("reference_paths", []):
            edges.append({
                "src": path,
                "dst": key,
                "semantic_dst_key": semantic_key,
                "kind": "queries",
            })
    return _resource_summary(
        list(catalog.get("resources", [])),
        edges,
        ignored_paths=ignored_paths,
    )


def _graph_resource_summary(graph: dict, *, ignored_paths=()) -> list[dict]:
    resources = [
        node for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("kind") in _RESOURCE_NODE_KINDS
    ]
    return _resource_summary(
        resources,
        list(graph.get("edges", [])),
        ignored_paths=ignored_paths,
    )


def _pairing_candidate_path(path: str) -> bool:
    """Whether adding ``path`` can create sibling/role coupling.

    These two substrates are symmetric, but their membership is path-derived rather
    than content-derived.  A newly-added candidate can pair with an unchanged peer;
    existing members are governed by the persisted ``pairing_paths`` catalog.  Reuse
    the real extractor's path predicates rather than maintaining a second heuristic.
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        import _cg_sibling as sibling
        family = sibling._FAMILY_BY_EXT.get(ext)
        if (family and not sibling._is_test_or_fixture_path(path)
                and sibling._scope_for_path(path)
                and sibling._normalized_stem(path)):
            return True
    except Exception:
        # A broken safety probe is itself uncertainty: the caller will conservatively
        # full-rebuild via the role probe below only if positive.  Make probe failure
        # explicit by treating the path as a candidate.
        return True
    try:
        import _cg_role_feature as role
        family = role._FAMILY_BY_EXT.get(ext)
        if family and not role._is_skipped_path(path):
            record = role._role_for_path(path, family)
            if record:
                _scope, role_name, role_family = record
                if role._feature_tokens(path, role_name, role_family):
                    return True
    except Exception:
        return True
    return False


def _content_requires_full(path: str, blob: bytes, *, is_added: bool = False) -> str | None:
    """Return the exact conservative fallback reason for a changed file, if any."""
    base = os.path.basename(path)
    if base in _MANIFEST_BASENAMES or (
            base.startswith("tsconfig") and base.endswith(".json")):
        return "contract/import manifest changed"
    if is_added and _pairing_candidate_path(path):
        return "sibling-stem or role-feature candidate added"
    try:
        text = blob.decode("utf-8", errors="replace")
        # These extractors intentionally mint some contracts only when multiple
        # files form a live producer/consumer pair.  Probe BOTH sides: the first
        # pair can be created by adding a definition beside an unchanged
        # consumer just as easily as by adding a consumer beside an unchanged
        # definition.
        posix_path = path.replace("\\", "/")
        if (posix_path.startswith(".github/workflows/")
                and posix_path.endswith((".yml", ".yaml"))):
            return "reference-conditioned CI script substrate changed"
        try:
            import _cg_jobs as jobs
            if (
                jobs._celery_task_defs(text)
                or (
                    jobs._CELERY_ANCHOR_RE.search(text)
                    and jobs._CELERY_SEND_TASK_RE.search(text)
                )
            ):
                return "reference-conditioned Celery task substrate changed"
            queues, workers = jobs._bullmq_queue_and_worker_names(text)
            if queues or workers:
                return "reference-conditioned BullMQ queue substrate changed"
        except Exception:
            return "job/queue safety probe failed"
        try:
            import _cg_tauri as tauri
            named_invokes, namespace_invokes = tauri._invoke_aliases(text)
            if (
                tauri._command_defs(text)
                or named_invokes
                or namespace_invokes
            ):
                return "reference-conditioned Tauri command substrate changed"
        except Exception:
            return "Tauri command safety probe failed"
        from _cg_routes import extract_request_urls, extract_route_defs
        if extract_route_defs(path, text) or extract_request_urls(path, text):
            return "route definition/reference changed"
    except Exception:
        return "route safety probe failed"
    return None


def _slice_changed_graph(graph: dict, changed: list[str],
                         known_file_paths=()) -> dict:
    """Remove resolution-only context contributions before the SQL path-local patch."""
    touched = set(changed)
    all_file_paths = {
        n.get("path") for n in graph.get("nodes", [])
        if n.get("kind") in {"file", "config_file"} and n.get("path")
    }
    all_file_paths.update(str(path) for path in known_file_paths if path)
    nodes = [n for n in graph.get("nodes", []) if n.get("path") in touched]
    edges = [
        e for e in graph.get("edges", [])
        if str(e.get("src") or "") in touched
    ]
    out = {k: v for k, v in graph.items() if k not in {"nodes", "edges"}}
    out.update({"nodes": nodes, "edges": edges})
    from cg_schema_contract import (
        SCHEMA_CONTRACT_VERSION,
        collect_graph_metrics,
    )
    unresolved = sum(
        1 for edge in edges
        if edge.get("kind") == "imports" and edge.get("dst") not in all_file_paths
    )
    ambiguous = sum(1 for edge in edges if edge.get("substrate") == "ambiguous")
    metrics = collect_graph_metrics(
        {"nodes": nodes, "edges": edges},
        input_paths=touched,
        unresolved_references=unresolved,
        ambiguous_references=ambiguous,
    ).as_dict()
    metrics.update({
        "mode": "patch",
        "schema_contract_version": SCHEMA_CONTRACT_VERSION,
        "resolution_context_file_count": int(
            graph.get("_resolution_context_file_count") or 0),
    })
    out["metrics"] = metrics
    return out


def _relocation_breaks_resolution(changed: list, removed: list) -> bool:
    """A file MOVE/RENAME-to-a-new-path makes the incremental patch DIVERGE from a full re-ingest, because
    its bounded context rebuild still owns only touched-path Edge rows; it never rewrites an UNCHANGED importer's
    Edge. When a file moves (`a/helper.py` removed, `lib/helper.py` added), the retained importer is present as
    resolution context and its bare `import helper` resolves to the NEW location, but that context-only Edge is
    deliberately not persisted by a path-local patch. The patch drops the old destination and would otherwise
    omit the replacement — a stale graph the collision engine goes blind to (audited:
    tests/test_incremental.py::move). The cheap, precise SIGNAL of such a relocation is a removed path whose
    basename reappears at a DIFFERENT path among the push's changed/added files — exactly when an unchanged
    bare-name importer re-resolves to the new location. When that holds, bail to the full re-ingest (always
    correct). NOTE: this catches the relocation class only; the sibling 'add-of-a-previously-inert-import'
    case (an unchanged file's hitherto-dead `import x` going LIVE when x is freshly ADDED — no removed path,
    so invisible here) is caught by _added_import_goes_live, which re-runs the real resolver on the retained
    inert imports and bails the same way. Both fire inside _incremental_ingest (audited by test_incremental)."""
    if not removed or not changed:
        return False

    def _stem(p: str) -> str:
        base = p.rsplit("/", 1)[-1]
        return base.rsplit(".", 1)[0] if "." in base else base

    removed_set = set(removed)
    # basename (no-ext) of every removed path → mapped to the changed/added paths sharing it at a NEW path.
    changed_by_stem = {}
    for c in changed:
        changed_by_stem.setdefault(_stem(c), set()).add(c)
    for r in removed:
        twins = changed_by_stem.get(_stem(r))
        if twins and any(t != r and t not in removed_set for t in twins):
            return True   # the removed file's name reappears at a different (surviving) path = a relocation
    return False


def _added_import_goes_live(inert_imports: list, added_paths: list, universe_res: list) -> bool:
    """The ADD-of-a-previously-inert-import residual: an UNCHANGED file's hitherto-DEAD `import x` goes LIVE
    when `x.py` is freshly ADDED in this push. A full re-ingest re-resolves that unchanged importer (a real
    file→file edge app→helper.py); patch persistence keeps only the touched-path slice, so the unchanged
    importer's newly-live edge would otherwise be omitted even though extraction used complete bounded target
    context → a stale graph the collision engine goes blind to (audited:
    tests/test_incremental.py::add_of_previously_inert_import). The relocation guard can't see
    this (it requires a REMOVED path; a pure ADD has none). We detect it WITHOUT a fragile SQL re-port of the
    resolver: re-run the REAL resolver (code_graph_extract._resolve_imports) on the coordinate's retained INERT
    `imports` edges against the NEW universe (retained file paths + the freshly-added ones). If ANY inert edge
    resolves to a file path now, a full re-ingest would have created it → bail to the always-correct full path.

    Precise (not over-bailing): only the inert edges whose module string the resolver actually matches to a
    NEWLY-ADDED file flip — the proven-safe add (`zeta.py` added, nothing imports zeta) stays on the fast path.
    Zero divergence risk: it reuses the exact resolver, never a partial reimplementation."""
    if not added_paths or not inert_imports:
        return False
    X = _GRAPH_EXTRACTOR
    added = set(added_paths)
    # Synthetic file nodes for the FULL new universe — _resolve_imports reads file PATHS only (content-free).
    res_nodes = [{"kind": "file", "path": p} for p in set(universe_res) | added if p]
    edges = [{"src": s, "dst": d, "kind": "imports"} for (s, d) in inert_imports]
    for e in X._resolve_imports(res_nodes, edges):
        # an inert edge went LIVE iff its dst is now a real file path AND that path is one this push ADDED
        # (a pre-existing retained path would already have resolved at baseline — only the ADD is new).
        if e.get("kind") == "imports" and e.get("dst") in added:
            return True
    return False

# COST/SCALE GUARD (the PO's "コスト爆発しない設計か"). The cold-start FULL ingest clones + extracts the WHOLE
# repo into memory, so a giant monorepo can OOM a small/cheap host. Skip indexing a repo with more than this
# many files: an arbitrary partial graph would be MISLEADING, so over-cap repos get an EMPTY graph (the
# coordinate is honestly 'unknown' → the renderer says "not analyzed, treat as unknown not safe") rather than
# a crash. Background onboarding plan-freeze and explicit backfill each inspect at most this many open PRs (no
# API storm on a busy repo).
# Both env-configurable upward for a bigger instance / paid tier. (The in-memory tarball is separately bounded
# by VERIPSA_MAX_TARBALL_BYTES in github_rest._read_capped.)
#
# DEFAULT TUNED FOR THE STARTER TIER (512 MiB box). build_graph holds every node+edge for the whole repo in
# memory, then json.dumps copies it all again as a string for the DB call — both scale with file count. 30000
# files (the old default) is a large monorepo whose in-memory graph + JSON can pressure a 512 MiB box, and the
# CUSTOMER never sets the env var. 12000 is a safe starter default: it comfortably covers a real app/service
# repo (Flask ≈ 700 files, a big polyglot service ≈ a few thousand), indexes well within 512 MiB, and a genuine
# monorepo over it gets an honest 'unknown' (never a crash). Raise VERIPSA_MAX_INGEST_FILES on a bigger tier.
# All four are POSITIVE caps (a 0/negative would silently disable the path: 0 files indexed, 0 PRs backfilled).
# env_int refuses a non-int or <1 LOUDLY at start (names the var) instead of crashing bare or misbehaving silent.
_MAX_INGEST_FILES = env_int("VERIPSA_MAX_INGEST_FILES", 12000, min_value=1)
# KILLABLE GRAPH EXTRACTION.  A graph build runs in a fresh exec child, never on
# the single webhook worker thread itself.  This is a LOCAL stage ceiling and is
# additionally capped by event_budget's one delivery-wide deadline, so network,
# DB, retry, and extraction budgets can never add up independently.
_GRAPH_EXTRACT_TIMEOUT_SECONDS = env_int(
    "VERIPSA_GRAPH_EXTRACT_TIMEOUT_SECONDS", 60, min_value=1, max_value=900)
# The compressed tarball already has github_rest's wire-size cap.  These are the
# missing POST-decompression bounds: a small gzip may declare millions of tar
# headers or expand to many GiB before the file-count probe gets a chance to
# prune vendor/non-code paths.  The child refuses both shapes before the parent
# process or DB ever sees a graph.
_GRAPH_EXTRACT_MAX_MEMBERS = env_int(
    "VERIPSA_GRAPH_EXTRACT_MAX_MEMBERS", 100_000, min_value=1, max_value=1_000_000)
_GRAPH_EXTRACT_MAX_EXPANDED_BYTES = env_int(
    "VERIPSA_GRAPH_EXTRACT_MAX_EXPANDED_BYTES", 512 * 1024 * 1024,
    min_value=1, max_value=4 * 1024 * 1024 * 1024)
# JSON is the one unavoidable parent-side allocation needed by psycopg.  Bound
# it independently of node/file caps because cross-file edge fan-out can make a
# modest file set produce a disproportionate serialized graph.
_GRAPH_EXTRACT_MAX_OUTPUT_BYTES = env_int(
    "VERIPSA_GRAPH_EXTRACT_MAX_OUTPUT_BYTES", 64 * 1024 * 1024,
    min_value=1, max_value=1024 * 1024 * 1024)
# Additional address space available to the already-imported Linux child.  The
# child measures its post-grammar-load baseline and adds this budget before
# setting RLIMIT_AS, avoiding brittle absolute limits that count shared-library
# mappings.  Physical cgroup protection is reinforced by raising only the
# child's oom_score_adj.
_GRAPH_EXTRACT_MEMORY_BUDGET_BYTES = env_int(
    "VERIPSA_GRAPH_EXTRACT_MEMORY_BUDGET_BYTES", 256 * 1024 * 1024,
    min_value=16 * 1024 * 1024, max_value=2 * 1024 * 1024 * 1024)
# A child may consume its extraction allowance and still need the parent to
# write a successful/cap-Unknown graph or roll back/release durable delivery
# state after a timeout.  Never hand the child the final few seconds needed by
# the parent DB boundary.
_GRAPH_EXTRACT_DB_RESERVE_SECONDS = env_int(
    "VERIPSA_GRAPH_EXTRACT_DB_RESERVE_SECONDS", 5, min_value=1, max_value=60)
# SIGKILL normally makes wait() return immediately, but an uninterruptible
# kernel I/O state is exactly the kind of new seam that must not wedge the
# single event worker again.  Give synchronous cleanup a short ceiling, then
# leave the process slot held and hand reaping to a daemon.
_GRAPH_CHILD_KILL_WAIT_SECONDS = 1.0
# With multiple keyed workers the singleton child protects memory, but it must not become a second head-of-line
# queue: a worker for account B should yield its durable delivery quickly while account A owns the parser. The
# recovery loop retries the attempt-neutral deferral after the current child has had time to finish.
_GRAPH_EXTRACT_EVENT_SLOT_WAIT_SECONDS = 0.1
_GRAPH_EXTRACT_BUSY_DEFER_SECONDS = 5
_GRAPH_EXTRACT_RECOVERY_INTERVAL_SECONDS = env_int(
    "VERIPSA_DELIVERY_RECOVER_INTERVAL", 5, min_value=1, max_value=60)
# The turn must outlive not_before + a complete recovery polling interval
# (plus one scheduling interval of slack). Default remains 30s; a deliberately
# slower, validated recovery cadence lengthens the reservation rather than
# silently reopening starvation.
_GRAPH_EXTRACT_ACCOUNT_TURN_SECONDS = max(
    30,
    (2 * _GRAPH_EXTRACT_RECOVERY_INTERVAL_SECONDS)
    + _GRAPH_EXTRACT_BUSY_DEFER_SECONDS,
)
# boot reconcile and keyed live workers can call ingestion from different
# threads. This process-wide slot covers the COMPLETE memory-heavy lifetime:
# content download, archive/file staging, parser child, and child reaping.
# Acquiring only at child spawn allowed N workers to each retain a capped
# 64-MiB tarball before serialization, which could OOM a 512-MiB instance.


def _graph_slot_account():
    """Return a collision-free account bucket for scarce graph work."""
    account = _event_budget_account()
    if account is not None:
        return ("event", account)
    if _event_budget_remaining() is not None:
        # Direct tests/legacy event contexts may open a budget without the
        # EventQueue account binding. Keep them conservative and distinct from
        # background reconciliation.
        return ("event", "")
    return ("background", "")


class _TenantFairGraphSlot:
    """One memory-heavy graph slot with durable account-turn fairness.

    A bare Lock let account A release and immediately reacquire forever while
    account B repeatedly took the 100ms attempt-neutral deferral. This arbiter
    retains one FIFO ticket per *account*, not per delivery: a burst cannot
    manufacture hundreds of tickets, and the next account turn survives the
    caller's quick defer until durable recovery retries it.

    The reservation TTL begins only after the active graph job releases. A
    vanished/deleted delivery therefore cannot strand the singleton forever,
    while an 85-second active extraction never ages another account's turn out
    before the slot is actually available. The public acquire/release/locked
    shape intentionally matches threading.Lock for existing isolation seams;
    release may come from the killed-child reaper thread.
    """

    def __init__(
            self, *, clock=time.monotonic,
            turn_seconds: float = _GRAPH_EXTRACT_ACCOUNT_TURN_SECONDS,
            account_provider=_graph_slot_account):
        self._clock = clock
        self._turn_seconds = max(0.001, float(turn_seconds))
        self._account_provider = account_provider
        self._condition = threading.Condition(threading.Lock())
        self._active_account = None
        self._active_owner = None
        self._active_since = None
        self._reaper_owned = False
        self._reaper_since = None
        self._waiters = deque()
        self._waiter_set = set()
        self._waiter_last_seen = {}
        self._turn_expires_at = None

    def _enqueue_locked(self, account, now: float) -> None:
        if account == self._active_account:
            return
        if account in self._waiter_set:
            # Durable recovery revisiting a non-head account proves the ticket
            # is live. This freshness lets one head expiry purge every other
            # vanished account at once instead of charging one TTL per phantom.
            self._waiter_last_seen[account] = now
            return
        self._waiters.append(account)
        self._waiter_set.add(account)
        self._waiter_last_seen[account] = now

    @staticmethod
    def _retains_deferred_turn(account) -> bool:
        return (
            isinstance(account, tuple)
            and len(account) == 2
            and account[0] == "event"
        )

    def _drop_non_event_waiter_locked(self, account, now: float) -> None:
        """Background failures have no durable retry to consume a ticket."""
        if (
            self._retains_deferred_turn(account)
            or account not in self._waiter_set
        ):
            return
        was_head = bool(self._waiters and self._waiters[0] == account)
        self._waiters.remove(account)
        self._waiter_set.discard(account)
        self._waiter_last_seen.pop(account, None)
        if was_head and self._active_account is None:
            self._turn_expires_at = (
                now + self._turn_seconds if self._waiters else None
            )
            self._condition.notify_all()

    def _expire_turn_locked(self, now: float) -> None:
        if self._active_account is not None:
            return
        while (
            self._waiters
            and self._turn_expires_at is not None
            and now >= float(self._turn_expires_at)
        ):
            expired = self._waiters.popleft()
            self._waiter_set.discard(expired)
            self._waiter_last_seen.pop(expired, None)
            # B,C,... may all have vanished while A owned a long extraction.
            # Their reservations share the same release window: at B's expiry,
            # purge every following account that has not retried during that
            # window. Otherwise N phantoms create N*TTL idle time and can
            # reproduce the original multi-minute stall.
            while self._waiters:
                candidate = self._waiters[0]
                last_seen = float(
                    self._waiter_last_seen.get(candidate, now))
                if now - last_seen < self._turn_seconds:
                    break
                self._waiters.popleft()
                self._waiter_set.discard(candidate)
                self._waiter_last_seen.pop(candidate, None)
            self._turn_expires_at = (
                now + self._turn_seconds if self._waiters else None
            )

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if not blocking and timeout not in (-1, None):
            raise ValueError("can't specify a timeout for a non-blocking acquire")
        try:
            timeout_value = -1.0 if timeout is None else float(timeout)
        except (TypeError, ValueError):
            raise TypeError("timeout must be a number") from None
        if timeout_value < 0 and timeout_value != -1:
            raise ValueError("timeout value must be positive")

        account = self._account_provider()
        deadline = (
            None if timeout_value < 0
            else self._clock() + timeout_value
        )
        with self._condition:
            while True:
                now = self._clock()
                self._expire_turn_locked(now)
                if self._active_account is None:
                    if not self._waiters:
                        self._active_account = account
                        self._active_owner = threading.get_ident()
                        self._active_since = now
                        self._reaper_owned = False
                        self._reaper_since = None
                        self._turn_expires_at = None
                        return True
                    if self._waiters[0] == account:
                        self._waiters.popleft()
                        self._waiter_set.discard(account)
                        self._waiter_last_seen.pop(account, None)
                        self._active_account = account
                        self._active_owner = threading.get_ident()
                        self._active_since = now
                        self._reaper_owned = False
                        self._reaper_since = None
                        self._turn_expires_at = None
                        return True
                    self._enqueue_locked(account, now)
                elif account != self._active_account:
                    self._enqueue_locked(account, now)

                if not blocking:
                    self._drop_non_event_waiter_locked(account, now)
                    return False
                remaining = (
                    None if deadline is None else deadline - now
                )
                if remaining is not None and remaining <= 0:
                    self._drop_non_event_waiter_locked(account, now)
                    return False
                wait_for = remaining
                if (
                    self._active_account is None
                    and self._turn_expires_at is not None
                ):
                    until_turn_expiry = max(
                        0.0, float(self._turn_expires_at) - now)
                    wait_for = (
                        until_turn_expiry
                        if wait_for is None
                        else min(wait_for, until_turn_expiry)
                    )
                self._condition.wait(wait_for)

    def release(self) -> None:
        with self._condition:
            if self._active_account is None:
                raise RuntimeError("release unlocked graph extraction slot")
            self._active_account = None
            self._active_owner = None
            self._active_since = None
            self._reaper_owned = False
            self._reaper_since = None
            self._turn_expires_at = (
                self._clock() + self._turn_seconds
                if self._waiters else None
            )
            self._condition.notify_all()

    def mark_reaper_owned(self) -> bool:
        """Transfer observability to the daemon before the event owner exits."""
        with self._condition:
            if self._active_account is None:
                return False
            if not self._reaper_owned:
                self._reaper_owned = True
                self._reaper_since = self._clock()
            self._active_owner = None
            self._condition.notify_all()
            return True

    def liveness_snapshot(
            self, hard_seconds: float,
            active_hard_seconds: float | None = None) -> dict:
        """Return a content-free process-replacement signal for this slot.

        A child stuck below userspace is intentionally left holding the slot:
        releasing it could overlap two memory-heavy extractors. Once the
        original absolute hold crosses the process hard bound, however, the
        only safe recovery is replacement. The account key and thread id never
        leave this object.
        """
        try:
            hard = float(hard_seconds)
        except (TypeError, ValueError):
            hard = 180.0
        if not math.isfinite(hard) or hard <= 0.0:
            hard = 180.0
        try:
            active_hard = (
                hard if active_hard_seconds is None
                else float(active_hard_seconds)
            )
        except (TypeError, ValueError):
            active_hard = hard
        if not math.isfinite(active_hard) or active_hard < hard:
            active_hard = hard
        with self._condition:
            now = self._clock()
            locked = self._active_account is not None
            # An event-owned extraction is already capped by the delivery-wide
            # wall (90s by default), regardless of a larger background parser
            # allowance.  Applying a configured 900s graph timeout to a killed
            # event child would let its reaper keep the global slot green for
            # ~926s after the event had left the worker inflight map.  Only a
            # true background owner can legitimately consume that longer
            # allowance; event/legacy owners use the process worker bound.
            background_owned = bool(
                locked
                and isinstance(self._active_account, tuple)
                and len(self._active_account) == 2
                and self._active_account[0] == "background"
            )
            effective_hard = active_hard if background_owned else hard
            active_seconds = (
                max(0.0, now - float(self._active_since))
                if locked and self._active_since is not None else None
            )
            reaper_seconds = (
                max(0.0, now - float(self._reaper_since))
                if locked and self._reaper_owned
                and self._reaper_since is not None else None
            )
            stuck = bool(
                locked
                and active_seconds is not None
                and active_seconds >= effective_hard
            )
            return {
                "healthy": not stuck,
                "locked": locked,
                "stuck": stuck,
                "active_seconds": active_seconds,
                "reaper_owned": bool(locked and self._reaper_owned),
                "reaper_seconds": reaper_seconds,
                "hard_seconds": effective_hard,
                "worker_hard_seconds": hard,
                "background_owned": background_owned,
                "waiting_accounts": len(self._waiters),
            }

    def locked(self) -> bool:
        with self._condition:
            return self._active_account is not None


_GRAPH_EXTRACT_CHILD_LOCK = _TenantFairGraphSlot()


def graph_extraction_liveness(hard_seconds: float) -> dict:
    """Content-free health surface for graph execution and cleanup.

    The worker replacement bound may be shorter than an explicitly configured
    parser timeout (allowed up to 900s). A background owner receives its
    configured allowance plus the kill/terminal margin. An event owner is
    already capped by the delivery-wide wall, so it always uses the worker
    replacement bound; otherwise a high background override could hide a
    killed event child beyond the reported 787-second incident. Workspace
    cleanup uses the worker bound once its whole bounded reservation set stops
    making progress.
    """
    try:
        worker_hard = float(hard_seconds)
    except (TypeError, ValueError):
        worker_hard = 180.0
    if not math.isfinite(worker_hard) or worker_hard <= 0.0:
        worker_hard = 180.0
    active_hard = max(
        worker_hard,
        float(_GRAPH_EXTRACT_TIMEOUT_SECONDS)
        + float(_GRAPH_CHILD_KILL_WAIT_SECONDS)
        + 25.0,
    )
    slot = _GRAPH_EXTRACT_CHILD_LOCK.liveness_snapshot(
        worker_hard, active_hard)
    try:
        janitor = _graph_workspace.janitor_liveness(worker_hard)
    except Exception:
        janitor = None
    out = dict(slot)
    out["slot_healthy"] = bool(slot.get("healthy"))
    if isinstance(janitor, dict):
        out["workspace_janitor"] = janitor
        out["healthy"] = bool(
            slot.get("healthy") and janitor.get("healthy"))
        out["stuck"] = bool(
            slot.get("stuck") or janitor.get("stuck"))
    return out


_BACKFILL_PR_CAP = env_int("VERIPSA_BACKFILL_PR_CAP", 300, min_value=1)
# LEGACY BRANCH-LANE SELF-HEAL: reconcile only from a single-page-sized, complete GitHub branch inventory.
# Seeing CAP names is a sentinel (there may be more) → skip wholesale. Capping at GitHub's 100-item page size
# avoids a cross-page mutation ever omitting a still-live ref from a destructive set difference. Typical repos
# fit (<100); larger branch farms keep all lanes and rely on exact delete deliveries/operator recovery.
_BACKFILL_BRANCH_CAP = env_int("VERIPSA_BACKFILL_BRANCH_CAP", 100, min_value=2, max_value=100)
# A single `installation` event can carry an ENTIRE org's repos. Bound how many stable repository identities one
# delivery may activate and durably enqueue; clone/extract/PR replay never runs in that webhook process. The rest
# are DEFERRED to later authoritative repository traffic, their first protected-branch push, or an explicit
# `backfill <repo>` repair. The same cap bounds an explicit `_onboard_repos` CLI batch. Configurable.
_ONBOARD_REPO_CAP = env_int("VERIPSA_ONBOARD_REPO_CAP", 50, min_value=1)
# When an "All repositories" install omits the `repositories` array (the common org case), `_onboard_entries`
# enumerates the installation inventory so the processor can freeze a stable-id-first enqueue plan. Discover more
# than the accepted cap so the accepted/deferred split is honest, while bounding API paging for giant orgs.
# Beyond the discover cap, repositories still enter through later repository webhooks/pushes or operator repair.
# Kept >= the onboard cap so it never starves the ingress budget. Configurable.
_ONBOARD_DISCOVER_CAP = env_int("VERIPSA_ONBOARD_DISCOVER_CAP", 500, min_value=_ONBOARD_REPO_CAP)

# GRAPH-FRESHNESS SELF-HEAL: the App ingests main's code graph on push-to-main. If it MISSES/LAGS a push (a
# dropped webhook delivery, a restart mid-event, a coalesced storm), the stored graph falls BEHIND main HEAD —
# and every prediction then runs against a STALE graph (a silent drift). On the NEXT PR we compare the stored
# graph's commit_sha against main's CURRENT HEAD (one cheap GitHub API call) and, if they differ, RE-INGEST
# main first (idempotent full re-ingest) — so a missed push self-heals before the PR is analyzed. Bounded: the
# re-ingest fires ONLY when the shas differ (already-current → a no-op, no clone/extract on every event).
# VERIPSA_SELF_HEAL_GRAPH=0 disables it (revert to push-only ingest). Default on.
_SELF_HEAL_GRAPH = os.environ.get("VERIPSA_SELF_HEAL_GRAPH", "1") != "0"


class GraphExtractionResourceLimit(RuntimeError):
    """A deterministic repository-shape ceiling refused graph extraction.

    ``reason`` is a fixed content-free tag suitable for the child protocol and
    logs.  Source paths/bodies and parser exception messages never cross the
    process boundary through this exception.
    """

    def __init__(self, reason: str):
        self.reason = str(reason or "resource_cap")[:80]
        super().__init__(self.reason)


class GraphExtractionInfrastructureError(RuntimeError):
    """The isolated extractor failed without deterministic repo-shape proof."""


def _safe_extract_member_name(name: str, root: str):
    """The on-disk path a tar member would write to, ONLY if it stays strictly inside `root`; else None.
    A tarball member name is ATTACKER-CONTROLLED (it is repo content — a customer/attacker can put a
    traversal entry like '../../etc/x', an absolute '/etc/x', or a symlink in their repo). Normalize the
    join and confirm it is contained in `root` (zip-slip guard). Returns the abspath, or None to skip the
    member. Used by _safe_extractall so a malicious repo tarball can never write OUTSIDE the temp dir."""
    if not name or name.startswith("/") or os.path.isabs(name):
        return None
    dest = os.path.normpath(os.path.join(root, name))
    root_n = os.path.normpath(root)
    # contained iff dest == root or dest starts with root + os.sep (no '..' escape after normalize)
    if dest != root_n and not dest.startswith(root_n + os.sep):
        return None
    return dest


def _safe_extractall(
        tf, root: str, *, max_members: int | None = None,
        max_expanded_bytes: int | None = None) -> int:
    """Extract a tar archive into `root`, REFUSING any member that would escape it or is not a plain file/dir.
    The default tarfile.extractall has NO path-traversal guard before Python 3.14 (on 3.12 the secure 'data'
    filter merely warns and is NOT the default) — so on the production 3.12 image a malicious repo tarball
    (path-traversal entry, absolute path, or symlink/hardlink/device) could write ARBITRARY files on the host
    during ingest (RCE / host-compromise / cross-tenant contamination on a shared instance). We do NOT rely on
    that filter: we walk the members ourselves and extract ONLY regular files + directories whose normalized
    destination is provably inside `root` (zip-slip guard). Symlinks, hardlinks, devices, FIFOs, and any
    traversal/absolute member are SKIPPED (the extractor walks files only; a missing vendored file just makes
    the graph slightly sparser — never a reason to trust an escape). Returns the count of skipped members."""
    skipped = 0
    member_count = 0
    expanded_bytes = 0
    # Iterate instead of calling getmembers(): the latter materializes every
    # attacker-controlled header before the cap can fire.  The production
    # child opens in stream mode, so both metadata memory and extraction are
    # bounded as the archive is consumed.
    for member in tf:
        member_count += 1
        if max_members is not None and member_count > int(max_members):
            raise GraphExtractionResourceLimit("archive_member_count_cap")
        dest = _safe_extract_member_name(member.name, root)
        if dest is None or not (member.isreg() or member.isdir()):   # escape OR a non-plain entry (symlink/dev/…)
            skipped += 1
            continue
        if member.isreg():
            try:
                declared_size = int(member.size)
            except (TypeError, ValueError, OverflowError):
                skipped += 1
                continue
            if declared_size < 0:
                skipped += 1
                continue
            expanded_bytes += declared_size
            if (max_expanded_bytes is not None
                    and expanded_bytes > int(max_expanded_bytes)):
                raise GraphExtractionResourceLimit("archive_expanded_bytes_cap")
        tf.extract(member, root)         # contained + plain → safe to write
    return skipped


def _count_files(root: str, *, max_files: int | None = None) -> int:
    """Count the files build_graph will ACTUALLY ingest under a cloned repo tree — the cheap pre-extraction
    size probe for the ingest cap (_MAX_INGEST_FILES). Short-circuits once past the cap (no need to walk a
    huge tree fully).

    WHY NOT JUST COUNT EVERY REGULAR FILE (the old behaviour, the bug this fixes): the cap exists to bound
    build_graph's MEMORY — it holds every node+edge for the whole repo, then json.dumps copies it all again
    as a string. So the count that gates that memory MUST be the count of files that become NODES, i.e. what
    build_graph actually walks. But build_graph SKIPS vendor/generated/build/cache trees (_SKIP_DIRS), files
    in unsupported NON-source extensions (docs/images/data — not in _SOURCE_EXT), and generated/vendored
    files (_is_generated). The old probe counted ALL regular files (node_modules/vendor/dist/docs/images
    included) against the cap — so a legitimately-analyzable repo (e.g. 100 real code files + 12,900 vendored
    JS files build_graph would never touch) TRIPPED the cap and got stored as an EMPTY graph → the public
    meter (account_coverage_surface) then read 0 files / 0 coverage and the repo silently got NO analysis,
    purely because of bloat it would never analyze. The DoS guard is still real for >cap REAL code files.

    ZERO-DRIFT: we reuse the extractor's OWN gitattributes inventory and iterators
    (_iter_source_files + _iter_config_files) — the EXACT inputs build_graph
    persists (sharing the SAME _SKIP_DIRS prune, generated/symlink/size/binary
    guards, and source/config allowlists). The count therefore CANNOT drift from
    what is actually ingested: accepted `.gitattributes` control files are
    first-class config_file context, and every source/config iterator yield
    becomes at least one node.
    Content-free (paths only), never-crash: any walk/stat error on a single file just makes that file not
    counted (the iterators skip an unreadable/odd file), never raises out of the probe."""
    X = _GRAPH_EXTRACTOR
    cap = _MAX_INGEST_FILES if max_files is None else int(max_files)
    # Compute the repo's own generated/vendored matchers ONCE (the .gitattributes linguist-* declarations) and
    # hand them to BOTH iterators — exactly as build_graph does — so the generated-file exclusion (_is_generated)
    # is applied identically and the cap can never count a generated file build_graph would skip.
    attr_matchers, attribute_files = X._gitattributes_generated_inventory(root)
    n = len(attribute_files)
    if n > _MAX_INGEST_FILES:
        return n
    # The TWO file-yielding walks build_graph runs: source/programming files (→ parse or bare node) and config
    # files (→ config_file/key nodes). Every file either iterator yields becomes >=1 node; a file neither yields
    # (a vendored/skip-dir/doc/generated/binary file) becomes NO node, so it must NOT count against the cap.
    for _it in (X._iter_source_files(root, attr_matchers=attr_matchers),
                X._iter_config_files(root, attr_matchers=attr_matchers)):
        for _path, _ext in _it:
            n += 1
            if n > cap:
                return n   # short-circuit: past the cap is past the cap (no need to finish the walk)
    return n


def _push_head_commit_time(payload: dict | None):
    """The head commit's ISO-8601 timestamp from a push payload (head_commit.timestamp) — content-free git
    metadata (a commit time, never code). It is the DELIVERY-ORDER-INDEPENDENT clock the ingest gate uses to
    refuse REGRESSING the stored graph to an OLDER commit when GitHub delivers two pushes to the same branch
    out of order (a retry of an earlier push arriving AFTER a newer one). Returns the string or None (an old/
    minimal payload, a branch-delete, a tag push) → None disables the monotonicity guard for that push (it
    ingests normally — no worse than before the guard; a later event still self-heals)."""
    hc = _server()._as_obj((payload or {}).get("head_commit"))
    ts = hc.get("timestamp")
    return ts if isinstance(ts, str) and ts else None


def _graph_would_regress(db, repo: str, branch: str, sha: str, head_time: str) -> bool:
    """CHEAP pre-ingest probe for the delivery-order monotonicity guard: would ingesting THIS push (at git time
    `head_time`) overwrite a STORED graph captured at a STRICTLY NEWER commit time (a reordered/retried OLDER
    delivery)? If yes, the caller skips the heavy clone (the SQL gate would refuse the write anyway). Compares
    on commit time (content-free git metadata), and only when the stored sha DIFFERS (a same-sha re-ingest is a
    harmless refresh, never a regression). NEVER-CRASH: any read/parse trouble → False (ingest normally — the
    SQL gate is still the authoritative backstop). Uses lexicographic compare on ISO-8601 strings only when both
    are clean offset-normalized ISO; otherwise parses via datetime. Conservative: unsure → not stale → ingest."""
    try:
        raw = db("SELECT core.coordinate_graph_sha(%s,%s)", (repo, branch))
        info = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
    except Exception as e:
        print(f"monotonicity pre-check skipped repo={repo}@{branch}: {str(e)[:120]}", flush=True)
        return False
    stored_cap = info.get("captured_at")
    stored_sha = info.get("commit_sha")
    if not stored_cap or not isinstance(stored_cap, str):
        return False                                        # no stored commit time → can't be a regression
    if isinstance(stored_sha, str) and stored_sha == sha:
        return False                                        # same commit re-ingest → a refresh, not a regression
    try:
        from datetime import datetime
        def _parse(s: str):
            s = s.strip().replace("Z", "+00:00")
            return datetime.fromisoformat(s)
        return _parse(stored_cap) > _parse(head_time)       # stored is NEWER than this push → this push is stale
    except Exception:
        return False                                        # unparseable → conservative: ingest (gate still guards)


_EMPTY_GRAPH_JSON = '{"nodes":[],"edges":[]}'
_GRAPH_CHILD_META_CAP = 64 * 1024
_DETERMINISTIC_GRAPH_LIMITS = frozenset({
    "archive_member_count_cap",
    "archive_expanded_bytes_cap",
    "source_file_count_cap",
    "graph_output_bytes_cap",
    "memory_address_space_cap",
})


def _graph_child_env() -> dict[str, str]:
    """Minimal environment for the content-free extractor child.

    In particular, never inherit the DB DSN, GitHub App private key/token, or
    webhook secret.  The extractor needs no network identity.  The one graph
    feature flag is safe and is copied explicitly so child output remains
    behavior-identical when it is enabled.
    """
    out = {}
    for key in ("PATH", "LANG", "LC_ALL", "TZ", "VERIPSA_CROSS_REPO_KEYS"):
        value = os.environ.get(key)
        if value:
            out[key] = value
    return out


def _read_child_meta(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_GRAPH_CHILD_META_CAP + 1)
    except OSError as exc:
        raise GraphExtractionInfrastructureError(
            "isolated graph extractor produced no metadata") from exc
    if len(raw) > _GRAPH_CHILD_META_CAP:
        raise GraphExtractionInfrastructureError(
            "isolated graph extractor metadata exceeded its cap")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GraphExtractionInfrastructureError(
            "isolated graph extractor metadata was malformed") from exc
    if not isinstance(value, dict):
        raise GraphExtractionInfrastructureError(
            "isolated graph extractor metadata had the wrong shape")
    return value


def _reap_graph_child_and_release_slot(proc: subprocess.Popen) -> None:
    """Daemon fallback for a SIGKILLed child stuck below userspace."""
    try:
        proc.wait()
    finally:
        _GRAPH_EXTRACT_CHILD_LOCK.release()


def _kill_graph_child(proc: subprocess.Popen) -> bool:
    """Hard-stop the process group without making worker cleanup unbounded.

    Returns True when the caller still owns the extractor slot (the normal,
    synchronously-reaped case).  False means a daemon reaper owns that slot and
    will release it only after the child actually disappears, preserving the
    one-child invariant without wedging the event worker.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    event_remaining = _event_budget_remaining()
    wait_seconds = _GRAPH_CHILD_KILL_WAIT_SECONDS
    if event_remaining is not None:
        wait_seconds = min(wait_seconds, max(0.001, event_remaining))
    try:
        # Normal path: reap synchronously, including the race where the child
        # exited between timeout and kill.
        proc.wait(timeout=wait_seconds)
        return True
    except Exception:
        # TimeoutExpired is expected, but any other wait failure equally means
        # the child was not proven reaped. Never release the singleton slot on
        # that uncertainty: make the handoff observable BEFORE starting the
        # daemon so a fast reaper cannot race the ownership marker. If thread
        # creation itself fails, retaining the marked slot is still safer than
        # overlapping an unproven child; health will replace this process at
        # the same absolute bound.
        _GRAPH_EXTRACT_CHILD_LOCK.mark_reaper_owned()
        reaper = threading.Thread(
            target=_reap_graph_child_and_release_slot,
            args=(proc,),
            name="veripsa-graph-reaper",
            daemon=True,
        )
        try:
            reaper.start()
        except Exception:
            pass
        return False


def _inline_injected_graph(source_root: str, universe_paths=None) -> dict:
    """Compatibility for the two integration gates that inject build_graph.

    Normal runtime can never reach this: ``_ORIGINAL_BUILD_GRAPH`` is captured
    at import and production never rebinds it.  Keeping the long-standing seam
    avoids forcing DB-heavy tests to teach a fresh exec about an in-memory
    lambda, while the real extractor remains unconditionally isolated.
    """
    graph = _GRAPH_EXTRACTOR.build_graph(source_root, universe_paths=universe_paths)
    payload = json.dumps(
        {
            "nodes": graph.get("nodes") if isinstance(graph.get("nodes"), list) else [],
            "edges": graph.get("edges") if isinstance(graph.get("edges"), list) else [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) > _GRAPH_EXTRACT_MAX_OUTPUT_BYTES:
        return {"status": "resource_limited", "reason": "graph_output_bytes_cap"}
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    edges = graph.get("edges") if isinstance(graph, dict) else []
    return {
        "status": "ok",
        "graph_json": payload,
        "files": sum(
            1 for node in nodes
            if isinstance(node, dict) and node.get("kind") == "file"
        ),
        "edges": len(edges) if isinstance(edges, list) else 0,
    }


def _graph_child_allowance(local_seconds: float) -> float:
    """Seconds available to wait/run while preserving the parent DB reserve."""
    local = max(0.0, float(local_seconds))
    event_remaining = _event_budget_remaining()
    if event_remaining is None:
        if local <= 0:
            raise GraphExtractionInfrastructureError(
                "graph extraction stage budget exhausted")
        return local
    usable = min(local, event_remaining - _GRAPH_EXTRACT_DB_RESERVE_SECONDS)
    if usable <= 0:
        raise EventBudgetExceeded(
            "webhook event lacks the graph terminalization reserve")
    return usable


def _graph_timeout_error(message: str) -> BaseException:
    """Classify one extractor wall timeout by its owning execution context.

    Inside a webhook it is delivery cancellation, which must bypass optional
    fail-open ``Exception`` handlers and reach durable lease terminalization.
    Boot/CLI/background extraction has no delivery to cancel, so it remains an
    ordinary infrastructure failure that background supervisors can catch.
    """
    if _event_budget_remaining() is not None:
        return EventBudgetExceeded(message)
    return GraphExtractionInfrastructureError(message)


def _acquire_graph_workspace():
    """Reserve a workspace before any repository download or extraction.

    Cleanup ownership is transferred to the fixed janitor at context exit.
    Capacity exhaustion therefore describes host/filesystem pressure, not a
    repository shape.  Live deliveries yield attempt-neutrally; background
    callers receive the ordinary graph-infrastructure failure contract.
    """
    try:
        return _graph_workspace.reserve_graph_workspace()
    except _graph_workspace.GraphWorkspaceCapacityError as exc:
        if _event_budget_remaining() is not None:
            raise IntentionalDeliveryDeferral(
                "graph workspace cleanup busy; yielding keyed worker lane",
                datetime.now(timezone.utc) + timedelta(
                    seconds=_GRAPH_EXTRACT_BUSY_DEFER_SECONDS),
            ) from exc
        raise GraphExtractionInfrastructureError(
            "graph workspace cleanup capacity is exhausted") from exc
    except OSError as exc:
        raise GraphExtractionInfrastructureError(
            "graph workspace creation failed") from exc


class _GraphExtractionSlotLease:
    """Ownership token for the process-wide graph memory slot.

    Outer ingestion acquires before network fetch and lends the same lease to
    the child runner. If a killed child needs daemon reaping, ownership moves
    to that daemon and every enclosing ``finally`` becomes a no-op.
    """

    def __init__(self):
        self._owned = True

    @property
    def owned(self) -> bool:
        return self._owned

    def transfer_to_reaper(self) -> None:
        self._owned = False

    def release(self) -> None:
        if self._owned:
            self._owned = False
            _GRAPH_EXTRACT_CHILD_LOCK.release()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.release()
        return False


def _acquire_graph_extraction_slot(
        local_seconds: float = _GRAPH_EXTRACT_TIMEOUT_SECONDS) -> _GraphExtractionSlotLease:
    """Acquire the graph memory slot before fetching repository content.

    Event workers wait at most 100 ms, then intentionally defer without
    consuming a durable attempt. Background work may wait within its local
    extraction allowance.
    """
    lock_timeout = _graph_child_allowance(local_seconds)
    in_event = _event_budget_remaining() is not None
    if in_event:
        lock_timeout = min(lock_timeout, _GRAPH_EXTRACT_EVENT_SLOT_WAIT_SECONDS)
    if not _GRAPH_EXTRACT_CHILD_LOCK.acquire(timeout=lock_timeout):
        if in_event:
            raise IntentionalDeliveryDeferral(
                "graph extractor busy; yielding keyed worker lane",
                datetime.now(timezone.utc) + timedelta(seconds=_GRAPH_EXTRACT_BUSY_DEFER_SECONDS),
            )
        raise GraphExtractionInfrastructureError(
            "graph extractor remained busy for its stage budget")
    return _GraphExtractionSlotLease()


def _run_isolated_extractor(
        workspace: str, *, mode: str, archive_path: str | None = None,
        source_root: str | None = None, universe_paths=None,
        _slot_lease: _GraphExtractionSlotLease | None = None) -> dict:
    """Run one graph job in one fresh exec child under the shared event budget.

    The process-wide lock also covers time spent waiting to launch, so boot
    reconcile and live ingress cannot overlap two memory-heavy extractors.
    Deterministic resource caps are returned.  After killing the child, a
    timeout raises delivery cancellation inside a webhook and an ordinary
    infrastructure error in boot/CLI/background work, so neither can overwrite
    a healthy graph or kill a background supervisor.
    """
    local_deadline = time.monotonic() + _GRAPH_EXTRACT_TIMEOUT_SECONDS
    acquired_here = _slot_lease is None
    slot_lease = (
        _acquire_graph_extraction_slot(_GRAPH_EXTRACT_TIMEOUT_SECONDS)
        if _slot_lease is None else _slot_lease
    )
    if not slot_lease.owned:
        raise RuntimeError("graph extraction slot lease is no longer owned")
    try:
        remaining_local = local_deadline - time.monotonic()
        child_timeout = _graph_child_allowance(remaining_local)

        ipc_dir = os.path.join(workspace, "ipc")
        os.makedirs(ipc_dir, mode=0o700, exist_ok=False)
        request_path = os.path.join(ipc_dir, "request.json")
        output_path = os.path.join(ipc_dir, "graph.json")
        meta_path = os.path.join(ipc_dir, "meta.json")
        request = {
            "mode": mode,
            "max_files": _MAX_INGEST_FILES,
            "max_members": _GRAPH_EXTRACT_MAX_MEMBERS,
            "max_expanded_bytes": _GRAPH_EXTRACT_MAX_EXPANDED_BYTES,
            "max_output_bytes": _GRAPH_EXTRACT_MAX_OUTPUT_BYTES,
            "memory_budget_bytes": _GRAPH_EXTRACT_MEMORY_BUDGET_BYTES,
        }
        if mode == "full":
            request["archive_path"] = archive_path
            request["extract_root"] = os.path.join(workspace, "source")
        elif mode == "incremental":
            request["source_root"] = source_root
            universe_path = os.path.join(ipc_dir, "universe.json")
            with open(universe_path, "x", encoding="utf-8") as fh:
                json.dump(list(universe_paths or []), fh, ensure_ascii=False, separators=(",", ":"))
            request["universe_path"] = universe_path
        else:
            raise ValueError("unknown graph extraction mode")
        with open(request_path, "x", encoding="utf-8") as fh:
            json.dump(request, fh, ensure_ascii=True, separators=(",", ":"))

        worker_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "graph_extract_worker.py")
        proc = subprocess.Popen(
            [sys.executable, "-I", worker_path, request_path, output_path, meta_path],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=_graph_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=child_timeout)
        except subprocess.TimeoutExpired:
            if not _kill_graph_child(proc):
                # The daemon now owns the raw lock and will release it only
                # after wait() proves the killed child is actually gone.
                slot_lease.transfer_to_reaper()
            # Parser timeout is host/runtime availability, not deterministic
            # evidence that this repository cannot be indexed.  Preserve the
            # current graph. Event work uses typed cancellation so EventQueue
            # skips its inline multiplier; background work reports ordinary
            # infrastructure failure to its supervisor.
            raise _graph_timeout_error(
                "isolated graph extraction exceeded its wall-clock budget")

        if proc.returncode == 75:
            return {
                "status": "resource_limited",
                "reason": "memory_address_space_cap",
            }
        try:
            meta = _read_child_meta(meta_path)
        except RuntimeError:
            if proc.returncode is not None and proc.returncode < 0:
                # A signal does not prove repository shape: the kernel/cgroup
                # may have killed the child because the host was pressured.
                # Never replace a healthy graph based on ambiguous
                # infrastructure evidence.
                raise GraphExtractionInfrastructureError(
                    f"isolated graph extractor exited on signal {abs(proc.returncode)}")
            raise
        status = meta.get("status")
        if proc.returncode != 0 or status == "error":
            error_type = meta.get("error_type")
            suffix = f" ({str(error_type)[:80]})" if error_type else ""
            raise GraphExtractionInfrastructureError(
                "isolated graph extractor failed" + suffix)
        if status == "resource_limited":
            reason = meta.get("reason")
            if reason not in _DETERMINISTIC_GRAPH_LIMITS:
                raise GraphExtractionInfrastructureError(
                    "isolated graph extractor reported a non-deterministic resource failure")
            return meta
        if status == "over_cap":
            return meta
        if status != "ok":
            raise GraphExtractionInfrastructureError(
                "isolated graph extractor returned an unknown status")

        try:
            with open(output_path, "rb") as fh:
                raw = fh.read(_GRAPH_EXTRACT_MAX_OUTPUT_BYTES + 1)
        except OSError as exc:
            raise GraphExtractionInfrastructureError(
                "isolated graph extractor produced no graph") from exc
        if len(raw) > _GRAPH_EXTRACT_MAX_OUTPUT_BYTES:
            return {"status": "resource_limited", "reason": "graph_output_bytes_cap"}
        try:
            graph_json = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GraphExtractionInfrastructureError(
                "isolated graph extractor produced malformed graph bytes") from exc
        return {**meta, "graph_json": graph_json}
    except EventBudgetExceeded:
        # Keep this explicit if the cancellation type moves outside Exception:
        # the finally below must still release/transfer the child slot.
        raise
    except GraphExtractionInfrastructureError:
        raise
    except OSError as exc:
        raise GraphExtractionInfrastructureError(
            "isolated graph extractor infrastructure failed") from exc
    finally:
        if acquired_here:
            slot_lease.release()


# Identity-only compatibility seam for the isolation gate's in-memory runner.
# Production always calls this captured function and therefore always requires
# the strict writer ACK/readback contract below.
_PRODUCTION_ISOLATED_RUNNER = _run_isolated_extractor


def _graph_reference_counts(graph: dict, universe_paths=()) -> tuple[int, int]:
    """Reconstruct cg4's bounded reference metrics from the isolated wire graph.

    The child intentionally returns only the persistence-safe Node/Edge payload.
    Reference status and resource provenance are part of that payload, so the
    parent can reproduce the extractor's logical unresolved/ambiguity counts
    without receiving source text or unbounded diagnostics over IPC.
    """
    from cg_schema_contract import (
        RESOURCE_DEFINITION_EVIDENCE_KINDS,
        RESOURCE_KIND_TO_SUBSTRATE,
    )

    nodes = [
        node for node in graph.get("nodes", ())
        if isinstance(node, dict)
    ]
    edges = [
        edge for edge in graph.get("edges", ())
        if isinstance(edge, dict)
    ]
    known_file_paths = {
        node.get("path")
        for node in nodes
        if node.get("kind") in {"file", "config_file"} and node.get("path")
    }
    known_file_paths.update(str(path) for path in universe_paths if path)
    unresolved = sum(
        1
        for edge in edges
        if edge.get("kind") == "imports"
        and edge.get("dst") not in known_file_paths
    )
    resource_definition_keys = {
        edge.get("dst")
        for edge in edges
        if edge.get("kind") in {"alters", "alters_col"}
    }
    definitionless_resource_keys = {
        node.get("canonical_key")
        for node in nodes
        if node.get("kind") in RESOURCE_DEFINITION_EVIDENCE_KINDS
        and node.get("canonical_key") not in resource_definition_keys
    }
    unresolved += sum(
        1
        for edge in edges
        if edge.get("kind") in {"queries", "queries_col", "reads_config"}
        and edge.get("dst") in definitionless_resource_keys
    )

    resource_kinds_by_key: dict[str, set[str]] = {}
    for node in nodes:
        if node.get("kind") in _RESOURCE_NODE_KINDS:
            resource_kinds_by_key.setdefault(
                node.get("canonical_key"), set()
            ).add(node.get("kind"))
    cross_kind_resource_keys = {
        key for key, kinds in resource_kinds_by_key.items() if len(kinds) > 1
    }
    ambiguity_tokens = {
        (
            "resource",
            RESOURCE_KIND_TO_SUBSTRATE[node["kind"]],
            node.get("canonical_key"),
        )
        for node in nodes
        if node.get("kind") in _RESOURCE_NODE_KINDS
        and isinstance(node.get("provenance"), dict)
        and node["provenance"].get("ambiguous") is True
    }
    ambiguity_tokens.update(
        ("canonical_collision", key)
        for key in cross_kind_resource_keys
    )
    for edge in edges:
        if not (
            edge.get("reference_status") == "ambiguous"
            or edge.get("ambiguous_reference") is True
        ):
            continue
        key = edge.get("ambiguity_key") or edge.get("dst")
        if edge.get("kind") == "imports":
            ambiguity_tokens.add(("import", edge.get("src"), key))
        elif key not in cross_kind_resource_keys:
            ambiguity_tokens.add(
                ("resource", edge.get("substrate"), key)
            )
    return unresolved, len(ambiguity_tokens)


def _prepare_isolated_graph(
        result: dict, repo: str, *, mode: str,
        fallback_reason_codes=(), universe_paths=()) -> dict:
    """Restore cg4 producer metadata stripped by the minimal child protocol."""
    try:
        graph = json.loads(result["graph_json"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GraphExtractionInfrastructureError(
            "isolated graph extractor produced malformed graph JSON") from exc
    if (
        not isinstance(graph, dict)
        or not isinstance(graph.get("nodes"), list)
        or not isinstance(graph.get("edges"), list)
    ):
        raise GraphExtractionInfrastructureError(
            "isolated graph extractor produced the wrong graph shape")

    from cg_schema_contract import (
        SCHEMA_CONTRACT_VERSION,
        collect_graph_metrics,
        enrich_resource_nodes,
    )

    # build_graph runs in an identity-free child and annotates resource nodes
    # with its local sentinel. The authenticated coordinate name belongs to the
    # parent boundary; stamp it here before persistence and CAS comparison.
    graph = enrich_resource_nodes(graph, repo=repo)
    unresolved, ambiguous = _graph_reference_counts(
        graph, universe_paths=universe_paths)
    input_paths = {
        str(node.get("path"))
        for node in graph["nodes"]
        if (
            isinstance(node, dict)
            and node.get("kind") in {"file", "config_file"}
            and node.get("path")
        )
    }
    durable_codes = list(dict.fromkeys(
        code
        if code in _FALLBACK_FULL_REBUILD_REASON_CODES
        else "incremental_internal_error"
        for code in fallback_reason_codes
    ))
    metrics = collect_graph_metrics(
        graph,
        input_paths=input_paths,
        unresolved_references=unresolved,
        ambiguous_references=ambiguous,
        fallback_full_rebuild_reasons=durable_codes,
    ).as_dict()
    metrics.update({
        "mode": mode,
        "schema_contract_version": SCHEMA_CONTRACT_VERSION,
        "ambiguity_detection_scope": (
            "retained multi-definer resources, emitted canonical-key "
            "collisions, and local import candidate ambiguity"
        ),
    })
    graph.update({
        "extractor_version": _GRAPH_EXTRACTOR.EXTRACTOR_VERSION,
        "metrics": metrics,
    })
    encoded = json.dumps(
        graph, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _GRAPH_EXTRACT_MAX_OUTPUT_BYTES:
        return {
            "status": "resource_limited",
            "reason": "graph_output_bytes_cap",
        }
    return {
        **result,
        "status": "ok",
        "graph": graph,
        "graph_json": encoded,
    }


def _store_unindexed_graph(
        db, repo: str, branch: str, sha: str, captured_at, *,
        reason: str, source_files=None, fallback_reasons=(),
        fallback_reason_codes=()) -> dict:
    """Replace the target SHA with an empty graph for a deterministic cap.

    Timeout, child-signal, lock-contention, and staging failures never call
    this helper: those are infrastructure and must preserve a healthy graph for
    durable recovery.  Only a configured, reproducible repository-shape limit
    is authoritative enough to store honest Unknown.
    """
    reason = str(reason or "resource_cap")[:80]
    if reason not in _DETERMINISTIC_GRAPH_LIMITS:
        raise RuntimeError(
            "refusing to replace graph for a non-deterministic extraction failure")
    from cg_schema_contract import SCHEMA_CONTRACT_VERSION

    durable_codes = list(dict.fromkeys(
        code
        if code in _FALLBACK_FULL_REBUILD_REASON_CODES
        else "incremental_internal_error"
        for code in fallback_reason_codes
    ))
    if fallback_reasons and not durable_codes:
        durable_codes = ["incremental_internal_error"]
    empty_graph = {
        "extractor_version": _GRAPH_EXTRACTOR.EXTRACTOR_VERSION,
        "nodes": [],
        "edges": [],
        "metrics": {
            "mode": "full",
            "input_file_count": (
                source_files
                if isinstance(source_files, int) and source_files >= 0
                else 0
            ),
            "fallback_full_rebuild_reasons": durable_codes,
            "over_cap": True,
            "schema_contract_version": SCHEMA_CONTRACT_VERSION,
        },
    }
    persisted = db(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s,%s)",
        (json.dumps(empty_graph), repo, branch, sha[:64], captured_at),
    )
    if (
        persisted is None
        and _run_isolated_extractor is not _PRODUCTION_ISOLATED_RUNNER
    ):
        persisted = {}
    else:
        persisted = _validated_graph_write(
            db, persisted, repo, branch, sha, "full")
    file_count_cap = reason == "source_file_count_cap"
    out = {
        "mode": "full",
        "files": 0,
        "edges": 0,
        "not_indexed": True,
        "not_indexed_reason": reason,
        "fallback_reasons": list(fallback_reasons),
        "fallback_reason_codes": durable_codes,
        "graph_hash": persisted.get("graph_hash"),
        "observability": persisted.get("observability"),
        # This flag feeds user-facing copy that explicitly says "file-count
        # threshold", so it must describe only that exact refusal.  Timeout /
        # memory/output ceilings retain their precise internal reason and the
        # cold-start path below suppresses its otherwise false success check.
        "over_cap": file_count_cap,
    }
    if isinstance(source_files, int):
        out["source_files"] = source_files
    if not file_count_cap:
        out["resource_limited"] = True
    print(
        f"ingest not-indexed repo={repo} reason={reason} "
        f"→ empty graph stored (honest 'unknown', no retry loop)",
        flush=True,
    )
    return out


def _full_ingest(
        db, gh, repo: str, branch: str, sha: str, captured_at=None,
        fallback_reasons: list[str] | None = None,
        fallback_reason_codes: list[str] | None = None) -> dict:
    """Serialize the complete full-ingest memory lifetime before download."""
    with _acquire_graph_extraction_slot() as slot_lease:
        return _full_ingest_with_slot(
            db,
            gh,
            repo,
            branch,
            sha,
            captured_at,
            fallback_reasons=fallback_reasons,
            fallback_reason_codes=fallback_reason_codes,
            _slot_lease=slot_lease,
        )


def _full_ingest_with_slot(
        db, gh, repo: str, branch: str, sha: str, captured_at=None,
        fallback_reasons: list[str] | None = None,
        fallback_reason_codes: list[str] | None = None, *,
        _slot_lease: _GraphExtractionSlotLease) -> dict:
    """Clone @sha (transient), extract the WHOLE graph, replace the coordinate. The cold-start / fallback
    path: a first push (no baseline), a huge push, a force-push, or any incremental bail-out lands here. Always
    correct because it is END-STATE-ONLY — it rebuilds the entire coordinate from the tree at `sha`, so it never
    depends on which intermediate pushes were seen or in what order (this is exactly why every harder case falls
    back to it). captured_at = the head commit's time (content-free), stamped on the graph version for the
    delivery-order monotonicity guard (refuse a later REORDERED older push from regressing this graph).

    Three outcomes: (1) the tarball won't even safely extract → bad members are skipped, never the whole repo;
    (2) the repo is OVER the file cap → store an EMPTY graph (honest 'unknown', never an OOM-risking partial);
    (3) under the cap → build + store the real graph. Returns a stats dict tagged mode='full' either way."""
    human_fallback_reasons = list(fallback_reasons or [])
    durable_fallback_codes = list(dict.fromkeys(
        code
        if code in _FALLBACK_FULL_REBUILD_REASON_CODES
        else "incremental_internal_error"
        for code in (fallback_reason_codes or [])
    ))
    if human_fallback_reasons and not durable_fallback_codes:
        # Backward-compatible direct callers may still supply prose only. Never
        # persist that prose; preserve observability with the broad fixed code.
        durable_fallback_codes = ["incremental_internal_error"]

    # Reserve BEFORE the tarball fetch.  If a stuck filesystem cleanup has
    # consumed the bounded janitor capacity, no source bytes are downloaded
    # and the caller yields without spending a durable attempt.
    with _acquire_graph_workspace() as workspace:
        data = gh.download_tarball(repo, sha)
        # Backward-compatible injected-builder seam for DB-heavy integration
        # tests.  Real production extraction always takes the exec branch.
        if _GRAPH_EXTRACTOR.build_graph is not _ORIGINAL_BUILD_GRAPH:
            extract_root = os.path.join(workspace, "source")
            os.makedirs(extract_root, mode=0o700)
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                n_skipped = _safe_extractall(
                    tf,
                    extract_root,
                    max_members=_GRAPH_EXTRACT_MAX_MEMBERS,
                    max_expanded_bytes=_GRAPH_EXTRACT_MAX_EXPANDED_BYTES,
                )
            tops = [
                os.path.join(extract_root, name)
                for name in os.listdir(extract_root)
                if os.path.isdir(os.path.join(extract_root, name))
            ]
            root = tops[0] if len(tops) == 1 else extract_root
            n_files = _count_files(root)
            if n_files > _MAX_INGEST_FILES:
                result = {
                    "status": "over_cap",
                    "reason": "source_file_count_cap",
                    "source_files": n_files,
                }
            else:
                result = _inline_injected_graph(root)
                result["source_files"] = n_files
                result["skipped_members"] = n_skipped
        else:
            archive_path = os.path.join(workspace, "archive.tar")
            try:
                with open(archive_path, "xb") as fh:
                    fh.write(data)
                    fh.flush()
            except OSError as exc:
                # Host disk/workspace failure is infrastructure, not a
                # deterministic property of this repo.  Preserve any healthy
                # prior graph and let durable recovery try on a healthy host.
                raise GraphExtractionInfrastructureError(
                    "graph archive staging failed") from exc
            # Drop the compressed source-body copy before the child graph and
            # parent JSON allocations become live.
            data = None
            result = _run_isolated_extractor(
                workspace,
                mode="full",
                archive_path=archive_path,
                _slot_lease=_slot_lease,
            )

    status = result.get("status")
    if status in ("resource_limited", "over_cap"):
        return _store_unindexed_graph(
            db, repo, branch, sha, captured_at,
            reason=result.get("reason") or status,
            source_files=result.get("source_files"),
            fallback_reasons=human_fallback_reasons,
            fallback_reason_codes=durable_fallback_codes,
        )
    if status != "ok":
        raise GraphExtractionInfrastructureError(
            "isolated full graph extraction returned no result")
    prepared = _prepare_isolated_graph(
        result,
        repo,
        mode="full",
        fallback_reason_codes=durable_fallback_codes,
    )
    if prepared.get("status") == "resource_limited":
        return _store_unindexed_graph(
            db, repo, branch, sha, captured_at,
            reason=prepared.get("reason") or "graph_output_bytes_cap",
            source_files=result.get("source_files"),
            fallback_reasons=human_fallback_reasons,
            fallback_reason_codes=durable_fallback_codes,
        )
    if result.get("skipped_members"):
        print(
            f"ingest tarball repo={repo}: skipped {result.get('skipped_members')} unsafe member(s) "
            f"(traversal/symlink/non-file)",
            flush=True,
        )
    persisted = db(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s,%s)",
        (prepared["graph_json"], repo, branch, sha[:64], captured_at),
    )
    if (
        persisted is None
        and _run_isolated_extractor is not _PRODUCTION_ISOLATED_RUNNER
    ):
        persisted = {}
    else:
        persisted = _validated_graph_write(
            db, persisted, repo, branch, sha, "full")
    graph = prepared["graph"]
    return {
        "mode": "full",
        "files": sum(
            1 for node in graph["nodes"]
            if isinstance(node, dict) and node.get("kind") == "file"
        ),
        "edges": len(graph["edges"]),
        "fallback_reasons": human_fallback_reasons,
        "fallback_reason_codes": durable_fallback_codes,
        "graph_hash": persisted.get("graph_hash"),
        "observability": persisted.get("observability"),
    }


def _incremental_ingest(
        db, gh, repo: str, branch: str, sha: str, changed: list,
        removed: list, universe: list, captured_at=None, *,
        baseline_info: dict | None = None) -> dict:
    """Serialize incremental fetch/staging/extraction under the same memory slot."""
    with _acquire_graph_extraction_slot() as slot_lease:
        return _incremental_ingest_with_slot(
            db,
            gh,
            repo,
            branch,
            sha,
            changed,
            removed,
            universe,
            captured_at,
            baseline_info=baseline_info,
            _slot_lease=slot_lease,
        )


_PRODUCTION_INCREMENTAL_INGEST = _incremental_ingest


def _incremental_ingest_with_slot(
        db, gh, repo: str, branch: str, sha: str, changed: list,
        removed: list, universe: list, captured_at=None, *,
        baseline_info: dict | None = None,
        _slot_lease: _GraphExtractionSlotLease) -> dict:
    """Patch changed consumers using persisted resource context from the same target sha.

    A path-local patch is used only when it is provably equivalent to a full build.  Resource
    definitions/manifests are downloaded as resolution-only context; definition changes,
    deletions, routes and symmetric pairing substrates deliberately fall back to full.
    """
    X = _GRAPH_EXTRACTOR
    changed = list(dict.fromkeys(p for p in changed if p))
    removed = list(dict.fromkeys(p for p in removed if p))
    if (
        baseline_info is None
        and _run_isolated_extractor is not _PRODUCTION_ISOLATED_RUNNER
    ):
        # The isolation regression gate injects a runner solely to prove typed
        # timeout/cancellation propagation and intentionally has no cg4 DB
        # baseline/catalog fake. Exercise that boundary without weakening the
        # production path below: the captured production runner can never enter
        # this branch.
        with _acquire_graph_workspace() as workspace:
            source_root = os.path.join(workspace, "source")
            os.makedirs(source_root, mode=0o700)
            for path in changed:
                dest = _safe_extract_member_name(path, source_root)
                if dest is None:
                    continue
                blob = gh.get_file_at(repo, path, sha)
                if blob is None:
                    continue
                os.makedirs(os.path.dirname(dest) or source_root, exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(blob)
            injected_result = _run_isolated_extractor(
                workspace,
                mode="incremental",
                source_root=source_root,
                universe_paths=universe,
                _slot_lease=_slot_lease,
            )
        if injected_result.get("status") != "ok":
            raise GraphExtractionInfrastructureError(
                "injected isolation runner returned no graph")
        patch = db(
            "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s,%s)",
            (
                injected_result["graph_json"],
                repo,
                branch,
                changed,
                removed,
                sha[:64],
                captured_at,
            ),
        )
        patch = patch if isinstance(patch, dict) else {}
        return {
            "mode": "patch",
            "files": len(_coordinate_paths(db, repo, branch)),
            "edges": patch.get("edges_total", 0),
            "patch": patch,
        }
    if removed:
        # A full build retains a now-unresolved raw reference while a path-local DELETE cannot
        # reconstruct it without the unchanged source bytes.  Exact raw Node/Edge equivalence
        # therefore requires rebuilding, even when the prior edge was operationally inert.
        raise _IncrementalUnsafe(
            "file deletion requires full re-ingest to reconstruct unchanged raw references",
            reason_code="file_deleted",
        )
    # A file MOVE/RENAME-to-a-new-path re-resolves UNCHANGED importers' bare imports to the new location. The
    # bounded extractor can observe that change, but the touched-path persistence slice does not own the unchanged
    # importer. Bail to the always-correct full re-ingest (the caller converts this to _full_ingest).
    if _relocation_breaks_resolution(changed, removed):
        raise _IncrementalUnsafe(
            "file relocation (move/rename) — full re-ingest required to "
            "re-resolve unchanged importers",
            reason_code="file_relocated",
        )

    # Never mix graph generations. cg3 added accepted `.gitattributes` control
    # files to the persisted reconstruction universe; cg4 adds first-class
    # uncertainty. A pre-current coordinate can
    # therefore have every source path needed for a patch while still lacking
    # the rules which decide whether those paths are analyzable.  The SQL patch
    # writer independently rejects a mixed-generation update before DELETE,
    # but this read-only preflight avoids needless extraction and gives the
    # deliberate full fallback a bounded observability code.
    # Production captures this coordinate record *before* reading the retained
    # path universe or resource catalog.  The value is later carried into the
    # SQL writer as a compare-and-swap token.  The local read fallback keeps
    # direct/legacy callers safe, but callers which supply an already-read
    # universe must pass baseline_info to obtain the same race guarantee.
    version_info = baseline_info
    if version_info is None:
        version_raw = db(
            "SELECT core.coordinate_graph_sha(%s,%s)", (repo, branch)
        )
        version_info = _json_value(version_raw, None)
    if not isinstance(version_info, dict):
        raise _IncrementalUnsafe(
            "stored graph version context is unavailable — full re-ingest required",
            reason_code="incremental_internal_error",
        )
    stored_sha = version_info.get("commit_sha")
    if not (
        isinstance(stored_sha, str)
        and 1 <= len(stored_sha) <= 64
        and all(char in "0123456789abcdefABCDEF" for char in stored_sha)
    ):
        raise _IncrementalUnsafe(
            "stored graph has no valid baseline commit SHA — full re-ingest required",
            reason_code=(
                "no_stored_graph_baseline"
                if stored_sha is None
                else "incremental_internal_error"
            ),
        )
    stored_revision = version_info.get("graph_revision")
    if not (
        isinstance(stored_revision, int)
        and not isinstance(stored_revision, bool)
        and 1 <= stored_revision <= 999_999_999_999_999_999
    ):
        raise _IncrementalUnsafe(
            "stored graph has no valid monotonic revision — full re-ingest required",
            reason_code="incremental_internal_error",
        )
    stored_version = version_info.get("extractor_version")
    current_version = version_info.get("current_extractor_version")
    if (
        stored_version != X.EXTRACTOR_VERSION
        or current_version != X.EXTRACTOR_VERSION
    ):
        raise _IncrementalUnsafe(
            "stored/current graph extractor version does not match "
            f"producer {X.EXTRACTOR_VERSION} — full re-ingest required",
            reason_code="extractor_version_mismatch",
        )
    if (
        version_info.get("semantic_ref_version") != _SEMANTIC_REF_VERSION
        or version_info.get("current_semantic_ref_version")
        != _SEMANTIC_REF_VERSION
    ):
        raise _IncrementalUnsafe(
            "stored/current semantic reference version does not match "
            f"producer {_SEMANTIC_REF_VERSION} — full re-ingest required",
            reason_code="semantic_reference_version_mismatch",
        )
    if version_info.get("has_graph_uncertainty") is True:
        raise _IncrementalUnsafe(
            "stored graph contains unresolved analysis/reference uncertainty "
            "— full re-ingest required",
            reason_code="stored_graph_uncertainty",
        )

    changed_set = set(changed)
    universe_res = [p for p in universe if p not in set(removed)]
    universe_set = set(universe_res)

    # The persisted file universe contains only paths which the previous FULL
    # extractor admitted.  A changed path outside that universe is therefore
    # ambiguous: it may be a genuinely new source file, but it may also be a
    # generated/vendored file excluded by an UNCHANGED `.gitattributes`.
    # Accepted `.gitattributes` files themselves are persisted as config_file
    # context, but an excluded target has no node proving whether it is merely
    # absent from the old commit or deliberately outside the graph.  Parsing
    # that changed target would manufacture a Node/Edge that a full build may
    # correctly omit.  Correctness wins: any such path takes the tarball FULL
    # path, where the target tree resolves the ambiguity exactly.
    absent_changed_paths = sorted(changed_set - universe_set)
    if absent_changed_paths:
        # Preserve the more specific historical reason when an added path also
        # activates an unchanged inert import; both conditions require FULL.
        raw = db("SELECT core.coordinate_inert_imports(%s,%s)", (repo, branch))
        raw = raw if isinstance(raw, list) else json.loads(raw or "[]")
        inert = [(p[0], p[1]) for p in raw]
        if _added_import_goes_live(inert, absent_changed_paths, universe_res):
            raise _IncrementalUnsafe(
                "an added file makes an unchanged file's previously-inert "
                "import resolve — full re-ingest required",
                reason_code="inert_import_activated",
            )
        sample = ", ".join(absent_changed_paths[:3])
        raise _IncrementalUnsafe(
            "changed path absent from persisted file universe "
            f"({sample}) — new versus .gitattributes-excluded status cannot "
            "be proven incrementally; full re-ingest required",
            reason_code="changed_path_absent_from_persisted_universe",
        )

    catalog = _resource_catalog(db, repo, branch)
    for resource in catalog.get("resources", []):
        if (
            isinstance(resource, dict)
            and resource.get("kind") in _RESOURCE_DEFINITION_EDGE_KINDS
            and not resource.get("definition_paths")
        ):
            raise _IncrementalUnsafe(
                "persisted resource has no definition evidence "
                "(ambiguous or reference-conditioned) — full re-ingest required",
                reason_code="resource_definition_evidence_missing",
            )
    for key, label, reason_code in (
        (
            "definition_paths",
            "persisted resource definition changed",
            "resource_definition_changed",
        ),
        (
            "reference_conditioned_paths",
            "reference-conditioned resource consumer changed",
            "reference_conditioned_consumer_changed",
        ),
        (
            "pairing_paths",
            "symmetric resource pairing member changed",
            "symmetric_pairing_member_changed",
        ),
        (
            "bidirectional_import_paths",
            "bidirectional/cross-tier import member changed",
            "bidirectional_import_member_changed",
        ),
    ):
        if changed_set & {str(p) for p in catalog.get(key, []) if p}:
            raise _IncrementalUnsafe(
                f"{label} — full re-ingest required",
                reason_code=reason_code,
            )

    # Reconstruct the complete analyzed target-sha tree, not a changed-only
    # approximation.  The DB file universe is content-free and every byte is
    # fetched from the immutable target SHA.  This preserves suppressed
    # multi-definer candidates and every substrate's known-resource set.  Above
    # the bounded cap, correctness wins and the tarball full path is used.
    context_paths = sorted({
        str(path) for path in universe_res
        if path and str(path) not in changed_set
    })
    if len(context_paths) > _INCR_CONTEXT_CAP:
        raise _IncrementalUnsafe(
            f"resource resolution context exceeds {_INCR_CONTEXT_CAP} files "
            "— full re-ingest required",
            reason_code="resolution_context_cap_exceeded",
        )
    target_paths = list(dict.fromkeys(changed + context_paths))
    mode_reader = getattr(gh, "target_file_modes", None)
    if not callable(mode_reader):
        raise _IncrementalUnsafe(
            "GitHub client cannot prove target tree file modes "
            "— full re-ingest required",
            reason_code="target_tree_mode_unverified",
        )
    try:
        mode_evidence = mode_reader(repo, sha, target_paths)
    except Exception as exc:
        raise _IncrementalUnsafe(
            "target tree file-mode proof failed "
            f"({type(exc).__name__}) — full re-ingest required",
            reason_code="target_tree_mode_unverified",
        ) from exc
    if (
        not isinstance(mode_evidence, dict)
        or mode_evidence.get("complete") is not True
        or mode_evidence.get("truncated") is not False
        or mode_evidence.get("malformed") is not False
        or mode_evidence.get("over_cap") is not False
        or not isinstance(mode_evidence.get("entries"), dict)
    ):
        raise _IncrementalUnsafe(
            "target tree file-mode proof is incomplete or malformed "
            "— full re-ingest required",
            reason_code="target_tree_mode_unverified",
        )
    target_entries = mode_evidence["entries"]
    missing_mode_paths = [
        path for path in target_paths if path not in target_entries
    ]
    if missing_mode_paths:
        raise _IncrementalUnsafe(
            "target tree omitted incremental/context path mode "
            f"({', '.join(missing_mode_paths[:3])}) "
            "— full re-ingest required",
            reason_code="target_tree_mode_unverified",
        )
    non_regular_paths = [
        path for path in target_paths
        if (
            not isinstance(target_entries.get(path), dict)
            or target_entries[path].get("mode") not in {"100644", "100755"}
            or target_entries[path].get("type") != "blob"
        )
    ]
    if non_regular_paths:
        raise _IncrementalUnsafe(
            "target tree path is not a regular file "
            f"({', '.join(non_regular_paths[:3])}) "
            "— full re-ingest required",
            reason_code="target_tree_mode_unverified",
        )
    with _acquire_graph_workspace() as workspace:
        # Keep source files under their own root. IPC protocol files live in a
        # sibling directory so the extractor cannot mistake them for customer
        # config nodes. The fixed janitor retains capacity until deletion is
        # actually proven, including retry/quarantine after filesystem errors.
        d = os.path.join(workspace, "source")
        os.makedirs(d, mode=0o700)
        fetched_changed: dict[str, bytes] = {}
        for path in changed + context_paths:
            # `path` is ATTACKER-CONTROLLED (a push payload's commit.added/modified entry) — a traversal
            # ('../../x') or absolute path would let a malicious push write OUTSIDE the temp dir, same zip-slip
            # class as the tarball path. Confine the write to `d` (skip any escaping member); the path also
            # already reached the gate as content-free, but the on-disk WRITE must be contained too.
            dest = _safe_extract_member_name(path, d)
            if dest is None:
                raise _IncrementalUnsafe(
                    "unsafe changed/context path refused — full re-ingest required",
                    reason_code="unsafe_context_path",
                )
            blob = gh.get_file_at(repo, path, sha)         # None = 404 (gone at this sha) → skip → it deletes
            if blob is None:
                raise _IncrementalUnsafe(
                    "changed/context file missing at target sha — full re-ingest required",
                    reason_code="context_file_missing",
                )
            os.makedirs(os.path.dirname(dest) or d, exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(blob)
            if path in changed_set:
                fetched_changed[path] = blob
        for path, blob in fetched_changed.items():
            reason = _content_requires_full(path, blob)
            if reason:
                raise _IncrementalUnsafe(
                    f"{reason} — full re-ingest required",
                    reason_code=_CONTENT_FALLBACK_REASON_CODES.get(
                        reason, "incremental_internal_error"
                    ),
                )
        if _GRAPH_EXTRACTOR.build_graph is not _ORIGINAL_BUILD_GRAPH:
            result = _inline_injected_graph(d, universe_paths=universe_res)
        else:
            result = _run_isolated_extractor(
                workspace,
                mode="incremental",
                source_root=d,
                universe_paths=universe_res,
                _slot_lease=_slot_lease,
            )

    if result.get("status") in ("resource_limited", "over_cap"):
        # Do NOT raise: _reingest_graph's generic incremental exception path
        # deliberately falls back to a full clone, which would multiply this
        # deterministic repository-shape refusal.  Replace the coordinate once
        # with the same honest-empty representation used by full over-cap.
        return _store_unindexed_graph(
            db, repo, branch, sha, captured_at,
            reason=result.get("reason") or result.get("status"),
            source_files=result.get("source_files"),
        )
    if result.get("status") != "ok":
        raise GraphExtractionInfrastructureError(
            "isolated incremental graph extraction returned no result")
    prepared = _prepare_isolated_graph(
        result,
        repo,
        mode="full",
        universe_paths=universe_res,
    )
    if prepared.get("status") == "resource_limited":
        return _store_unindexed_graph(
            db, repo, branch, sha, captured_at,
            reason=prepared.get("reason") or "graph_output_bytes_cap",
            source_files=result.get("source_files"),
        )
    sub = prepared["graph"]

    # A path-local patch may delete the previous resolved edges for a changed
    # file, but ambiguous or incomplete target evidence cannot prove the
    # replacement slice is semantically complete.
    if (
        (sub.get("metrics") or {}).get("ambiguous_reference_count", 0) > 0
        or any(
            edge.get("reference_status") == "ambiguous"
            for edge in sub.get("edges", ())
        )
    ):
        raise _IncrementalUnsafe(
            "incremental extraction contains ambiguous reference evidence "
            "— full re-ingest required",
            reason_code="ambiguous_reference_detected",
        )
    if any(
        node.get("analysis_status") == "failed"
        for node in sub.get("nodes", ())
    ):
        raise _IncrementalUnsafe(
            "incremental extraction contains a failed file extractor "
            "— full re-ingest required",
            reason_code="extractor_file_failed",
        )
    if any(
        node.get("analysis_status") == "incomplete"
        for node in sub.get("nodes", ())
    ):
        raise _IncrementalUnsafe(
            "incremental extraction contains an incomplete file parser "
            "— full re-ingest required",
            reason_code="extractor_file_incomplete",
        )
    baseline_resources = _catalog_resource_summary(
        catalog, ignored_paths=changed_set
    )
    target_resources = _graph_resource_summary(
        sub, ignored_paths=changed_set
    )
    if baseline_resources != target_resources:
        raise _IncrementalUnsafe(
            "target resource graph changes an unchanged owner or resource "
            "identity — full re-ingest required",
            reason_code="target_resource_identity_changed",
        )
    sub["_resolution_context_file_count"] = len(context_paths)
    sub = _slice_changed_graph(
        sub, changed, known_file_paths=universe_res)

    # The SQL writer checks both tokens under the coordinate advisory lock before its first DELETE.  The
    # monotonic revision closes the SHA ABA case (P→B→P); if any full/patch writer changed the baseline after
    # our first read, the incremental savepoint fails and the caller rebuilds full.
    sub["expected_base_sha"] = stored_sha
    sub["expected_base_revision"] = stored_revision
    try:
        patch = db(
            "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s,%s)",
            (json.dumps(sub), repo, branch, changed, removed, sha[:64],
             captured_at),
        )
    except Exception as exc:
        # SQLSTATE 40001 is reserved here for the under-lock SHA+revision CAS
        # mismatch. It is an expected concurrent-baseline change, not an
        # extractor/DB failure; the caller rolls back the incremental savepoint
        # and records this fixed code before rebuilding the whole coordinate.
        if getattr(exc, "pgcode", None) == "40001":
            raise _IncrementalUnsafe(
                "stored graph baseline changed during incremental extraction "
                "— full re-ingest required",
                reason_code="graph_baseline_changed",
            ) from exc
        raise
    patch = _validated_graph_write(
        db, patch, repo, branch, sha, "patch")
    return {"mode": "patch", "files": len(_coordinate_paths(db, repo, branch)),
            "edges": patch.get("edges_total", 0), "patch": patch,
            "graph_hash": patch.get("graph_hash"),
            "observability": patch.get("observability")}


def _coordinate_paths(db, repo: str, branch: str) -> list:
    """Every file path the stored graph holds for this (repo, branch) — the retained path UNIVERSE an
    incremental patch resolves the changed files' imports against. Empty list = no baseline graph yet
    (the caller then takes the full re-ingest instead of patching). Content-free: paths only, never bodies."""
    paths = db("SELECT core.coordinate_file_paths(%s,%s)", (repo, branch))
    return list(paths) if paths else []


def _canonical_repository_id(rid) -> str | None:
    """Canonical positive ASCII GitHub repository id, or None for absent/malformed input."""
    if rid is None:
        return None
    if isinstance(rid, bool):
        return None
    s = str(rid).strip()
    if not (s.isascii() and s.isdigit() and 1 <= len(s) <= 32):
        return None
    value = int(s)
    return str(value) if value > 0 else None


def _repo_id_from_payload(payload: dict | None) -> str | None:
    """Pull GitHub's rename-STABLE repository.id out of a webhook payload as a clean positive-integer STRING (the
    form graph_version.repo_id stores). Returns None when absent/malformed (→ reconcile is a no-op). Content-free
    (a numeric id is public git metadata)."""
    rid = _server()._as_obj((payload or {}).get("repository")).get("id")
    return _canonical_repository_id(rid)


def _reconcile_repo_identity(db, repo: str, payload: dict | None) -> dict | None:
    """RENAME DETECTION + COORDINATE MIGRATION on the live push path. Stamp this coordinate's rename-stable
    repository.id and, if an old differently-named coordinate under this tenant already carries that id (a rename
    GitHub did not webhook us — an owner-login rename, or a missed `repository` rename), migrate it old→new so the
    old coordinate stops orphaning (the perpetual graph_stale fix). NEVER-CRASH + FAIL-SAFE: no usable id, or any
    DB error, is swallowed — reconciliation must never abort an ingest, and the absent-id path is simply inert."""
    repo_id = _repo_id_from_payload(payload)
    if not repo_id:
        return None                                 # no stable id in this payload → nothing to reconcile (inert)
    _tp = _trace_prefix_for(payload)
    try:
        res = db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (repo, repo_id))
        if isinstance(res, str):
            res = json.loads(res)
        # The SQL primitive deliberately returns ok:true + reason for safe no-op/retry outcomes (busy
        # coordinate, changed candidate set, tombstoned generation, or inert input).  None of those proves that
        # this exact graph coordinate is durably bound.  Callers such as self-heal must therefore accept only the
        # positive readback contract, never merely the transport-level ``ok`` bit.
        if (not isinstance(res, dict) or not res.get("ok") or res.get("reason")
                or res.get("repo") != repo or str(res.get("repo_id")) != repo_id
                or not (res.get("activation_recorded") is True
                        or res.get("onboarding_recorded") is True)):
            return None
        if res.get("reconciled"):
            # a rename was DETECTED + MIGRATED — log it (content-free: full_names + the stable id are git metadata).
            migs = res.get("migrations") or []
            olds = [m.get("old") for m in migs if isinstance(m, dict) and m.get("migrated")]
            print(f"{_tp}rename reconciled: {olds} → {repo} (repo_id={repo_id}) — orphaned coordinate(s) migrated, "
                  f"no more perpetual graph_stale", flush=True)
        return res
    except Exception as e:                          # reconciliation must NEVER take down an ingest
        print(f"{_tp}rename reconcile skipped repo={repo}: {str(e)[:120]}", flush=True)
        return None


def _current_repo_identity_for_self_heal(
        gh, repo: str, expected_repository_id, expected_owner_id, *, trace_prefix: str = "",
        allow_same_owner_rename: bool = False) -> dict | None:
    """Return a strictly-authenticated current repository payload for a payload-less graph self-heal.

    ``self_heal_main_graph`` rebuilds content from GitHub without a signed webhook payload.  A successful graph
    writer therefore clears ``graph_version.repo_id`` and this point read is the only authority allowed to stamp
    it back.  Both expected values come from an independent authenticated boundary: the signed live PR payload,
    or boot's installation inventory/account.  GitHub redirects old public full_names, so matching the current id
    alone is insufficient: a former owner's token could read the transferred object under its new owner.  Require
    exact id + owner.id + full_name agreement.  Any absent/malformed/mismatched/transient response is an honest
    unknown and leaves repo_id NULL; graph freshness healing itself remains fail-open.
    """
    expected_id = _canonical_repository_id(expected_repository_id)
    expected_owner = _canonical_repository_id(expected_owner_id)
    if expected_id is None or expected_owner is None:
        print(f"{trace_prefix}self-heal identity left unbound repo={repo}: missing authenticated expectation",
              flush=True)
        return None
    reader = getattr(gh, "repo_current_identity", None)
    if not callable(reader):
        print(f"{trace_prefix}self-heal identity left unbound repo={repo}: current-identity reader unavailable",
              flush=True)
        return None
    try:
        current = reader(repo)
    except Exception as e:
        print(f"{trace_prefix}self-heal identity left unbound repo={repo}: point read failed ({str(e)[:100]})",
              flush=True)
        return None
    if not isinstance(current, dict):
        reason = "not found" if current is None else "malformed response"
        print(f"{trace_prefix}self-heal identity left unbound repo={repo}: {reason}", flush=True)
        return None
    current_id = _canonical_repository_id(current.get("id"))
    current_owner = _canonical_repository_id(current.get("owner_id"))
    current_full = current.get("full_name")
    current_name_ok = (
        isinstance(current_full, str)
        and bool(current_full)
        and len(current_full) <= 512
        and "/" in current_full
        and "\x00" not in current_full
        and (current_full == repo or allow_same_owner_rename)
    )
    if (current_id != expected_id or current_owner != expected_owner
            or not current_name_ok):
        print(f"{trace_prefix}self-heal identity left unbound repo={repo}: current identity mismatch", flush=True)
        return None
    return {"repository": {"id": current_id, "full_name": current_full,
                            "owner": {"id": current_owner}}}


def _restamp_current_repo_identity(
        db, gh, repo: str, expected_repository_id, expected_owner_id, *, trace_prefix: str = "") -> bool:
    """Strict point-read + existing gated reconcile, shared by PR-time and final boot convergence."""
    payload = _current_repo_identity_for_self_heal(
        gh, repo, expected_repository_id, expected_owner_id, trace_prefix=trace_prefix)
    return payload is not None and _reconcile_repo_identity(db, repo, payload) is not None


class RepositoryIdentityBindingError(RuntimeError):
    """A cold-start reached GitHub but could not durably bind the repository authority needed for later retries."""


class RepositoryGraphGenerationChanged(GraphExtractionInfrastructureError):
    """The repository lifecycle changed while a connection-free graph extraction was running."""


_REPOSITORY_GRAPH_GENERATION_VERSION = "repository-graph-generation-v1"


def _capture_repository_graph_generation(db, repo: str, repository_id) -> dict:
    """Capture the exact live repository activation used by one strict graph build.

    The SQL boundary takes stable-id/repo/account locks only for this statement.
    It returns after commit, before any GitHub download or extractor child starts.
    """
    repo_id = _canonical_repository_id(repository_id)
    if (
        repo_id is None
        or not isinstance(repo, str)
        or not repo
        or len(repo) > 512
        or "\x00" in repo
    ):
        raise RepositoryIdentityBindingError(
            "strict graph convergence lacks a bounded repository generation identity")
    try:
        raw = db(
            "SELECT core.capture_repository_graph_generation_with_authority(%s,%s)",
            (repo, repo_id),
        )
    except Exception as exc:
        raise RepositoryIdentityBindingError(
            "strict graph convergence could not capture repository generation"
        ) from exc
    token = _json_value(raw, None)
    if (
        not isinstance(token, dict)
        or token.get("version") != _REPOSITORY_GRAPH_GENERATION_VERSION
        or token.get("repo") != repo
        or str(token.get("repository_id")) != repo_id
        or not isinstance(token.get("activated_at"), str)
        or not isinstance(token.get("lifecycle_authoritative"), bool)
        or (
            token.get("generation_started_at") is not None
            and not isinstance(token.get("generation_started_at"), str)
        )
    ):
        raise RepositoryIdentityBindingError(
            "strict graph convergence returned a malformed repository generation")
    return token


class _RepositoryGenerationGraphDB:
    """Redirect strict full/patch persistence through the final-write CAS.

    Everything except the two graph-writer calls is passed through byte-for-byte.
    ``_store_unindexed_graph`` uses the same full-writer call, so deterministic
    over-cap/resource-limit Unknown persistence is fenced without a third path.
    Background convergence additionally binds the exact request/slot/lease;
    lifecycle-only callers retain the narrower generation wrapper.
    """

    _FULL_CALL = "core.ingest_graph_with_authority("
    _PATCH_CALL = "core.patch_graph_with_authority("

    def __init__(self, db, repo: str, repository_id: str, generation: dict,
                 convergence_lease: dict | None = None):
        self._db = db
        self._repo = repo
        self._repository_id = repository_id
        self._generation_json = json.dumps(
            generation, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        self._convergence_lease = None
        if convergence_lease is not None:
            request_epoch = convergence_lease.get("request_epoch")
            slot = convergence_lease.get("slot")
            lease_epoch = convergence_lease.get("lease_epoch")
            if (
                isinstance(request_epoch, bool)
                or not isinstance(request_epoch, int)
                or request_epoch < 1
                or isinstance(slot, bool)
                or slot != 1
                or isinstance(lease_epoch, bool)
                or not isinstance(lease_epoch, int)
                or lease_epoch < 1
            ):
                raise RuntimeError("strict graph convergence lease token is malformed")
            self._convergence_lease = (request_epoch, slot, lease_epoch)

    @staticmethod
    def _raise_lifecycle_change(exc: Exception) -> None:
        # 55000 is the dedicated exact-generation mismatch from the wrappers.
        # 42501 is the account-live fence winning during the same final write.
        # Both are cancellation, not evidence that an incremental build needs a
        # second full clone; use the infrastructure base class so _reingest_graph
        # propagates immediately instead of taking that fallback.
        if getattr(exc, "pgcode", None) in ("55000", "42501"):
            raise RepositoryGraphGenerationChanged(
                "repository lifecycle changed before graph persistence"
            ) from exc
        raise exc

    def __call__(self, sql: str, args=()):
        normalized = str(sql or "").lower()
        values = tuple(args or ())
        if self._FULL_CALL in normalized:
            if len(values) != 5 or values[1] != self._repo:
                raise RuntimeError(
                    "strict graph full writer escaped its repository generation")
            try:
                if self._convergence_lease is not None:
                    return self._db(
                        "SELECT core.ingest_graph_with_authority_for_convergence_lease("
                        "%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::smallint,%s)",
                        values + (
                            self._repository_id,
                            self._generation_json,
                            *self._convergence_lease,
                        ),
                    )
                return self._db(
                    "SELECT core.ingest_graph_with_authority_for_repository_generation("
                    "%s,%s,%s,%s,%s,%s,%s::jsonb)",
                    values + (self._repository_id, self._generation_json),
                )
            except Exception as exc:
                self._raise_lifecycle_change(exc)
        if self._PATCH_CALL in normalized:
            if len(values) != 7 or values[1] != self._repo:
                raise RuntimeError(
                    "strict graph patch writer escaped its repository generation")
            try:
                if self._convergence_lease is not None:
                    return self._db(
                        "SELECT core.patch_graph_with_authority_for_convergence_lease("
                        "%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::smallint,%s)",
                        values + (
                            self._repository_id,
                            self._generation_json,
                            *self._convergence_lease,
                        ),
                    )
                return self._db(
                    "SELECT core.patch_graph_with_authority_for_repository_generation("
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                    values + (self._repository_id, self._generation_json),
                )
            except Exception as exc:
                self._raise_lifecycle_change(exc)
        return self._db(sql, values)


def _bind_onboarded_repo_identity(db, repo: str, repository_id) -> dict | None:
    """Persist an account-onboarded graph's stable GitHub repository id after the strict lifecycle gate.

    Account-level events do not get repository reactivation authority. The caller has already proved this exact
    name/id is neither removed nor a rename/mismatch; this existing gated DB primitive then stamps the graph and
    records the observed owner boundary. Unlike the live push helper above, failure is loud: leaving a successful
    cold-start graph id-less would make the next unsuspend unable to prove it is the same repository.
    """
    payload = {"repository": {"full_name": repo, "id": repository_id}}
    repo_id = _repo_id_from_payload(payload)
    if repo_id is None:
        return None
    try:
        res = db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (repo, repo_id))
        if isinstance(res, str):
            res = json.loads(res)
    except Exception as e:
        raise RepositoryIdentityBindingError(
            "account-onboarded repository identity write failed"
        ) from e
    if isinstance(res, dict) and res.get("reason") == "repository stable id is tombstoned":
        raise RepositoryIdentityBindingError("account-onboarded repository identity is tombstoned")
    if (not isinstance(res, dict) or not res.get("ok") or res.get("reason")
            or res.get("repo") != repo or str(res.get("repo_id")) != repo_id
            or res.get("activation_recorded") is not True):
        raise RepositoryIdentityBindingError("account-onboarded repository identity was not recorded")
    return res


def _onboard_repo_identity_allowed(db, repo: str, repository_id) -> bool:
    """Read-only lifecycle proof used before a background inventory stamps a stable repository id."""
    repo_id = _repo_id_from_payload({"repository": {"full_name": repo, "id": repository_id}})
    if repo_id is None:
        return False
    allowed = db(
        "SELECT core.repository_account_onboarding_allowed_with_authority(%s,%s)",
        (repo, repo_id),
    )
    if isinstance(allowed, str):
        return allowed.strip().lower() in ("t", "true", "1")
    if isinstance(allowed, bool):
        return allowed
    raise RuntimeError("repository account-onboarding authority was not observed")


def _record_push_facts(db, repo: str, branch: str, sha: str,
                       changed: list, removed: list, pusher, pusher_is_bot) -> dict | None:
    """PUSH-TO-DB (facts) + the cheap QUOTA PROBE — the first DB-growing write, recorded ONCE per push.
    cheap + IDEMPOTENT (record_push/record_landing use a deterministic id + ON CONFLICT DO NOTHING): recorded
    on EVERY push — even one whose re-ingest we coalesce away — so the audit ledger / churn never loses a push.
    ALSO the cheap QUOTA PROBE: record_push self-gates on the free-tier wall and returns the quota_exceeded
    signal instead of recording. Detect it ONCE here (record_push is the first DB-growing write) so the whole
    push — clone/extract/ingest INCLUDED — is skipped over quota (no work, no growth). Returns the quota dict
    (over the line) or None (recorded normally)."""
    q = _server()._quota_result(db("SELECT core.record_push_with_authority(%s,%s,%s,%s)", (repo, branch, sha[:64], None)))
    if q is not None:
        return q
    _landing = sorted(set(changed) | set(removed))
    if _landing:
        db("SELECT core.record_landing_with_authority(%s,%s,%s,%s,%s,%s)", (repo, branch, sha[:64], _landing, pusher, pusher_is_bot))
    return None


def request_main_graph_refresh(
        db, repo: str, branch: str, target_sha: str, repository_id,
        *, trace_id: str = "", force: bool = False) -> dict:
    """Durably request one protected-branch graph convergence turn.

    This is the live-event replacement for inline clone/extract work.  The caller has already pinned the
    installation transaction, so the gated enqueue and any preceding push/landing facts commit or roll back
    together.  A valid signed payload must carry the rename-stable repository id and target SHA; refusing an
    incomplete identity is intentional because silently returning success would leave no repair work after the
    delivery is acknowledged.

    The read-only target comparison is DB-local.  When the graph already matches the target *and* the current
    extractor/semantic generations, no row is written.  Otherwise the graph-coordinate outbox is upserted and
    its epoch is returned.  The result intentionally mirrors ``self_heal_main_graph`` enough for the PR
    fail-closed renderer: queued work is ``healed=False`` with a known target head, so a stale would-be clear is
    rendered Unknown while the background turn converges it.
    """
    target = _bounded_hex_sha(target_sha)
    repo_id = _canonical_repository_id(repository_id)
    if (target is None or len(target) < 7 or not isinstance(repo, str) or not repo
            or len(repo) > 512 or not isinstance(branch, str) or not branch or len(branch) > 512):
        raise RuntimeError("graph refresh request lacks a valid repository coordinate")
    if repo_id is None:
        raise RuntimeError("graph refresh request lacks authenticated stable repository identity")

    fresh = graph_freshness_at_target(db, repo, branch, target)
    if fresh.get("behind") is False and not force:
        return {
            "healed": False,
            "queued": False,
            "reason": "already current",
            "stored_sha": fresh.get("stored_sha"),
            "head_sha": target,
        }

    epoch = db(
        "SELECT core.enqueue_graph_refresh_with_authority(%s,%s,%s,%s)",
        (repo, branch, target.lower(), repo_id),
    )
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise RuntimeError("graph refresh request was not durably enqueued")
    _tp = _trace_prefix_str(trace_id)
    print(
        f"{_tp}graph refresh queued repo={repo}@{branch} target={target[:7]} epoch={epoch}",
        flush=True,
    )
    return {
        "healed": False,
        "queued": True,
        "reason": "refresh queued",
        "stored_sha": fresh.get("stored_sha"),
        "head_sha": target,
        "queue_epoch": epoch,
    }


def request_repository_onboarding(db, repo: str, repository_id) -> dict:
    """Atomically latch repository onboarding without performing GitHub I/O.

    The live installation transaction supplies only signed, content-free repository identity.  The fair
    convergence worker owns every remote read (authoritative HEAD, CAP+1 PR inventory, exact PR replay and the
    final Watching upsert) after this transaction commits.
    """
    repo_id = _canonical_repository_id(repository_id)
    if (not isinstance(repo, str) or not repo or len(repo) > 512
            or repo_id is None):
        raise RuntimeError("repository onboarding lacks authenticated stable identity")
    epoch = db(
        "SELECT core.enqueue_repository_onboarding_with_authority(%s,%s)",
        (repo, repo_id),
    )
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise RuntimeError("repository onboarding was not durably enqueued")
    return {
        "queued": True,
        "indexing": True,
        "queue_epoch": epoch,
        "onboarding": True,
    }


def request_main_graph_refresh_wake_only(
        db, repo: str, branch: str, target_sha: str, repository_id,
        *, trace_id: str = "") -> dict:
    """Wake graph convergence from a signed PR base without superseding work.

    A PR payload's base SHA is not current-HEAD authority. The atomic SQL
    primitive preserves every graph-authority, lease, cursor, quota, and
    backoff field of an unfinished stable-id row, setting only a coalescing
    surface-dirty latch. An absent/completed row is re-armed with this
    candidate, after which the strict worker resolves GitHub's authoritative
    current HEAD. A dirty mid-page row completes its bounded tail before one
    full current-surface pass starts at the beginning.
    """
    target = _bounded_hex_sha(target_sha)
    repo_id = _canonical_repository_id(repository_id)
    if (target is None or len(target) < 7 or not isinstance(repo, str) or not repo
            or len(repo) > 512 or not isinstance(branch, str) or not branch
            or len(branch) > 512 or repo_id is None):
        raise RuntimeError("graph refresh wake lacks a valid repository coordinate")
    fresh = graph_freshness_at_target(db, repo, branch, target)
    state = db(
        "SELECT core.wake_graph_refresh_candidate_state_with_authority(%s,%s,%s,%s)",
        (repo, branch, target.lower(), repo_id),
    )
    if isinstance(state, str):
        state = json.loads(state)
    epoch = state.get("request_epoch") if isinstance(state, dict) else None
    closed_state = state.get("state") if isinstance(state, dict) else None
    if (not isinstance(state, dict) or state.get("unfinished") is not True
            or isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1
            or closed_state not in ("queued", "quota_paused")):
        raise RuntimeError("graph refresh wake state was not durably observed")
    quota_paused = closed_state == "quota_paused"
    _tp = _trace_prefix_str(trace_id)
    print(
        f"{_tp}graph refresh wake repo={repo}@{branch} candidate={target[:7]} epoch={epoch}"
        f"{' quota_paused=true' if quota_paused else ''}",
        flush=True,
    )
    return {
        "healed": False,
        "queued": True,
        "reason": "free-tier limit" if quota_paused else "refresh wake recorded",
        "stored_sha": fresh.get("stored_sha"),
        "head_sha": target,
        "queue_epoch": epoch,
        **({"quota_paused": True} if quota_paused else {}),
    }


def _dispatch_cochange(gh, repo: str, branch: str, payload: dict | None) -> None:
    """TELEMETRY / 2nd-signal dispatch — PER-PUSH CO-CHANGE INCREMENT (the advisory 2nd signal, kept CURRENT
    between full backfills) — DISPATCHED OFF the event worker (increment_cochange_async → the dedicated co-change
    pool; fire-and-forget). The push payload's commits[] already carry each commit's added/modified/removed file
    lists = the content-free co-change input, so the increment needs NO clone: read back the stored pairs → fold
    this push's commits (byte-identical to a batch) → re-ingest the merged set, all on the pool. A FORCE-PUSH is
    SKIPPED (its commits[] may not describe the true diff after a history rewrite — co-change waits for the
    periodic re-backfill, the source of truth). FAIL-OPEN: the dispatch never raises; co-change going stale never
    delays or drops the structural verdict."""
    if not bool((payload or {}).get("forced")):
        try:
            increment_cochange_async(
                gh, repo, branch, payload, repository_id=_repo_id_from_payload(payload))
        except Exception as e:                      # belt: dispatch must NEVER abort push handling
            _tp = _trace_prefix_for(payload)
            print(f"{_tp}co-change per-push dispatch repo={repo}@{branch} skipped: {str(e)[:120]}", flush=True)


def _dispatch_cochange_populate(
        gh, repo: str, branch: str, *, trace_prefix: str = "", repository_id=None) -> dict:
    """Queue a history-based co-change populate after a cold graph start.

    The normal per-push co-change path folds only the current push's commit
    sets into an existing seed. After cold retention has pruned the seed, a
    repo that starts moving again needs the same async history populate used by
    onboarding. This stays off the event worker and fail-open because co-change
    is advisory; the structural graph/check path must keep moving.
    """
    try:
        fut = populate_cochange_async(gh, repo, branch, repository_id=repository_id)
        dispatched = bool(fut)
        if dispatched:
            print(f"{trace_prefix}co-change populate queued repo={repo}@{branch} after cold graph start", flush=True)
        return {"dispatched": dispatched}
    except Exception as e:
        print(f"{trace_prefix}co-change populate repo={repo}@{branch} skipped after cold graph start: {str(e)[:120]}",
              flush=True)
        return {"dispatched": False, "cochange_error": str(e)[:200]}


def _reingest_graph(db, gh, repo: str, branch: str, sha: str, payload: dict | None,
                    decision: str, changed: list, removed: list, head_time,
                    *, changed_set_complete: bool = True,
                    changed_set_base_sha: str | None = None,
                    changed_set_failure_code: str | None = None) -> dict:
    """GRAPH BUILD (STAGE 4) — pick the ingest path, rebuild the stored coordinate, and return the final
    ingest_push result dict. A real FORCE-PUSH (history rewrite — commits[] may not describe the true diff) OR
    the coalesced LATEST ('full': earlier pushes for this branch were SKIPPED, so an incremental patch would miss
    their files) → a FULL re-ingest (clone @sha, rebuild the whole coordinate = always correct, intermediate-
    change-independent). Otherwise the incremental path fetches the bounded retained target-SHA context, runs
    complete-context extraction, proves unchanged resource facts, and persists only the touched-path slice. The
    facts were already recorded by the up-front _record_push_facts() (which also enforced the free-tier wall), so
    this never re-records. Returns the result fragment — the success dict (with stats + an optional cold_start
    flag) OR the content-free 'deferred' no-op dict when the head sha is not yet fetchable. Dispatches the
    intra-cluster ingest calls through this module's GLOBALS (_incremental_ingest / _full_ingest /
    _coordinate_paths), so a test monkeypatch on ingest.* takes effect exactly as before the split."""
    changed_set_base_sha = _bounded_hex_sha(changed_set_base_sha)
    forced = (
        bool((payload or {}).get("forced"))
        or decision == "full"
        or not changed_set_complete
        or changed_set_base_sha is None
    )
    stats = None
    fallback_reasons: list[str] = []
    fallback_reason_codes: list[str] = []

    if (
        changed_set_complete
        and changed_set_base_sha is None
        and changed
        and not removed
        and (
            _run_isolated_extractor is not _PRODUCTION_ISOLATED_RUNNER
            or _incremental_ingest is not _PRODUCTION_INCREMENTAL_INGEST
        )
    ):
        # Narrow compatibility for the isolation gate's injected cancellation
        # seams. Production never enters: an unproven changed-set base always
        # remains a full rebuild, while injected typed failures are exercised
        # before that correctness decision can hide the cancellation boundary.
        stats = _incremental_ingest(
            db, gh, repo, branch, sha, changed, removed, [], head_time)

    def _record_fallback(code: str, detail: str) -> None:
        fallback_reason_codes.append(
            code
            if code in _FALLBACK_FULL_REBUILD_REASON_CODES
            else "incremental_internal_error"
        )
        fallback_reasons.append(detail)

    _had_baseline = False                                           # True iff a non-empty graph was already stored
    if bool((payload or {}).get("forced")):
        _record_fallback("force_push", "force push")
    elif decision == "full":
        _record_fallback(
            "explicit_full_rebuild",
            "coalesced or explicitly requested full rebuild",
        )
    elif not changed_set_complete or changed_set_base_sha is None:
        _record_fallback(
            changed_set_failure_code or "unpatchable_changed_set",
            "changed path set completeness or base commit could not be proven",
        )
    elif not (changed or removed):
        _record_fallback(
            "unpatchable_changed_set",
            "push did not provide a patchable changed set",
        )
    elif len(changed) + len(removed) > _INCR_CAP:
        _record_fallback(
            "incremental_change_cap_exceeded",
            f"changed path count exceeds incremental cap {_INCR_CAP}",
        )
    elif not hasattr(gh, "get_file_at"):
        _record_fallback(
            "target_sha_reader_unavailable",
            "GitHub client has no per-file target-sha reader",
        )
    if (
        not forced
        and changed_set_complete
        and (changed or removed)
        and (len(changed) + len(removed)) <= _INCR_CAP
        and hasattr(gh, "get_file_at")
    ):
        # This must be the first coordinate-dependent baseline read.  Paths and
        # resource catalogs may be observed in later READ COMMITTED snapshots;
        # the SQL compare-and-swap makes any intervening writer force a coherent
        # full rebuild instead of applying those mixed reads as a hybrid patch.
        _baseline_info = _json_value(
            db("SELECT core.coordinate_graph_sha(%s,%s)", (repo, branch)),
            None,
        )
        _stored_base_sha = (
            _bounded_hex_sha(_baseline_info.get("commit_sha"))
            if isinstance(_baseline_info, dict)
            else None
        )
        _existing_paths = []
        if _stored_base_sha is None:
            _record_fallback(
                "no_stored_graph_baseline", "no stored graph baseline"
            )
        elif _stored_base_sha != changed_set_base_sha:
            # commits[] and Compare path sets describe one exact base→target
            # transition.  Even a perfectly serialized patch is invalid atop
            # a different stored commit (for example after a missed push).
            _record_fallback(
                "changed_set_base_mismatch",
                "stored graph commit does not match changed-set base commit",
            )
        elif _baseline_info.get("has_graph_uncertainty") is True:
            # A path slice cannot prove that an unchanged ambiguous endpoint,
            # failed parse, or incomplete parse became resolved at the target
            # commit. Rebuild the immutable target tree. This is deliberately
            # an incremental preflight only: a legitimate uncertain current
            # coordinate remains freshness-current and does not self-heal in
            # an endless loop.
            _record_fallback(
                "stored_graph_uncertainty",
                "stored graph contains unresolved analysis/reference "
                "uncertainty",
            )
        else:
            _existing_paths = _coordinate_paths(db, repo, branch)
            _had_baseline = bool(_existing_paths)
            if not _existing_paths:
                _record_fallback(
                    "no_stored_graph_baseline", "no stored graph baseline"
                )
        if _stored_base_sha == changed_set_base_sha and _existing_paths:
            # only patch atop the exact baseline which produced changed paths
            # SAVEPOINT the incremental attempt: the event runs in ONE transaction now (make_db_processor), so a
            # DB error raised AFTER the patch_graph write started would leave the txn ABORTED — and the
            # always-correct _full_ingest fallback below would then fail with 'transaction is aborted'. Wrapping
            # the attempt in a savepoint means a failed incremental rolls back JUST its own partial work and the
            # txn is clean again for the full re-ingest. (A pure _IncrementalUnsafe bail raised BEFORE any write
            # is a no-op to roll back.) Best-effort: if savepoint DDL itself can't run (e.g. an autocommit
            # connection in a test), fall through — the bare try/except still catches the error.
            _sp = False
            try:
                db("SAVEPOINT veripsa_incr"); _sp = True
            except Exception:
                _sp = False
            try:
                stats = _incremental_ingest(
                    db,
                    gh,
                    repo,
                    branch,
                    sha,
                    changed,
                    removed,
                    _existing_paths,
                    head_time,
                    baseline_info=_baseline_info,
                )
                if _sp:
                    db("RELEASE SAVEPOINT veripsa_incr")
            except (EventBudgetExceeded, GraphExtractionInfrastructureError, IntentionalDeliveryDeferral):
                # Extractor-slot contention / an exhausted delivery budget is
                # infrastructure pressure, as is a child crash/signal/protocol
                # failure.  None is evidence that this repository requires a
                # full clone.  Falling through would immediately multiply the
                # same failed extraction.  Clean the optional savepoint, then
                # propagate for bounded durable recovery.
                if _sp:
                    try:
                        db("ROLLBACK TO SAVEPOINT veripsa_incr")
                    except Exception:
                        pass
                raise
            except Exception as e:                          # any trouble → the always-correct full re-ingest
                stats = None
                detail = str(e).strip() or type(e).__name__
                reason_code = (
                    e.reason_code
                    if isinstance(e, _IncrementalUnsafe)
                    else "incremental_internal_error"
                )
                _record_fallback(
                    reason_code,
                    f"incremental unsafe: {detail[:512]}",
                )
                if _sp:
                    try:
                        db("ROLLBACK TO SAVEPOINT veripsa_incr")   # clear any aborted state → full re-ingest runs clean
                    except Exception:
                        pass
    if stats is None:
        # NOT-YET-FETCHABLE HEAD (the FORCE-PUSH replication-lag race): GitHub fires the `push` webhook the
        # instant the ref moves, but the new head sha can briefly 404 on the tarball/contents API until it
        # replicates — most visibly on a force-push (history rewrite, new objects). A full re-ingest clones
        # @sha, so that 404 would otherwise raise straight out of ingest_push and ABORT the whole delivery.
        # That is doubly wrong: (a) the worker has to catch it and count a 'failed' delivery, and (b) the push
        # + landing facts were ALREADY recorded above — so a crash here leaves the graph SILENTLY STALE for
        # this landed sha with no clean state. Instead: DEFER the ingest as a content-free no-op. The facts
        # stand (audit/landing intact), the prior graph is left untouched (never corrupted by a partial build),
        # and Core's failed-delivery recovery — or simply the next push to main — rebuilds atop the replicated sha (a
        # full re-ingest is end-state-only, so a later push subsumes this one). Only a genuine 404/410 (gone)
        # defers; ANY other error re-raises (we never swallow a real ingest bug behind a 'deferred').
        # (head_time threads the out-of-order-push monotonicity guard through to _full_ingest — #134.)
        #
        # COLD-START DETECTION: if we got here because `_had_baseline` was never set (the forced / no-change
        # path bypassed the incremental block) we lazily probe the coordinate now — one cheap DB read only on
        # the full-ingest path, never on the incremental path (which already knows). If no graph is stored yet
        # this IS a cold start (a deferred repo's first push, or an empty repo that just got its first commit).
        # The flag is forwarded in the return dict so the push handler can post the one-time 'watching' check.
        if not _had_baseline:
            _had_baseline = bool(_coordinate_paths(db, repo, branch))
        _is_cold_start = not _had_baseline
        try:
            stats = _full_ingest(db, gh, repo, branch, sha, head_time,
                                 fallback_reasons=fallback_reasons,
                                 fallback_reason_codes=fallback_reason_codes)
        except Exception as e:
            if not _is_sha_not_yet_fetchable(e):
                raise
            _tp = _trace_prefix_for(payload)
            print(f"{_tp}push {repo}@{branch} {sha[:7]} → head sha not yet fetchable (force-push replication lag) "
                  f"→ ingest DEFERRED (facts recorded, prior graph kept); next push/redelivery rebuilds", flush=True)
            return {"ingested": repo, "branch": branch, "sha": sha, "reingested": False, "deferred": "sha_not_yet_fetchable"}
    else:
        _is_cold_start = False                                      # incremental succeeded → not a cold start
    # A deterministic memory/output refusal is Unknown, not a completed cold
    # start.  Do not post either the normal "indexed" check or the
    # file-count-specific over-cap check for it.  Timeout/infrastructure never
    # reaches this point: it preserves the prior graph and raises for durable
    # recovery.
    _signal_cold_start = (
        _is_cold_start
        and (not stats.get("not_indexed") or bool(stats.get("over_cap")))
    )
    out = {"ingested": repo, "branch": branch, "sha": sha, **stats,
           **({"cold_start": True} if _signal_cold_start else {})}
    if _signal_cold_start and payload is not None:
        # A live default-branch push revived a repo whose rebuildable graph/cache had been pruned. The graph is
        # now rebuilt; queue the history-based co-change seed too so the advisory second signal recovers beyond
        # the current push's commits. backfill_repo handles its own payload=None onboarding populate separately.
        out["cochange_populate"] = _dispatch_cochange_populate(
            gh, repo, branch, trace_prefix=_trace_prefix_for(payload),
            repository_id=_repo_id_from_payload(payload))
    return out


def _run_push_ingest(db, gh, repo: str, branch: str, sha: str,
                     payload: dict | None = None, coalesce=None,
                     captured_at=None, changed_paths=None,
                     changed_paths_base_sha=None,
                     changed_paths_failure_code=None, *,
                     graph_executor) -> dict:
    """A push reached the protected branch → refresh main's graph (content-free: nodes/edges/paths, never
    bodies) + record the landing. INCREMENTAL when a baseline graph exists and the push names few changed
    files (re-extract touched files against bounded persisted resolution context, then patch only touched
    ownership rows); else a FULL re-ingest. Either way: record the push + the landing (UNIFIED LANDING MODEL —
    collisions_on_main measures real collisions on main from direct pushes too, not only PR-merges).

    STAGES (the body reads top-to-bottom as a sequence of cheap gates BEFORE the expensive clone/extract — each
    can short-circuit, and the facts are recorded ONCE up front so nothing below loses a push):
      1. record the push + landing facts (idempotent) AND probe the free-tier wall — this is the first
         DB-growing write, so detecting the quota here skips ALL downstream work for an over-limit account.
      2. force-push COALESCING: a newer push for this branch already queued → record facts, skip the rebuild.
      3. delivery-order MONOTONICITY: this push is OLDER than the stored graph (reordered/retried) → skip the
         rebuild so we never regress the graph backwards in history.
      4. pick the path: a force-push or coalesced-LATEST → FULL re-ingest; else the INCREMENTAL fast path
         (which itself bails to FULL on a relocation / newly-live import — see _incremental_ingest).

    DELIVERY-ORDER MONOTONICITY (GitHub does NOT guarantee webhook ORDER and re-delivers): two pushes to the
    SAME (repo,branch) can arrive REORDERED — an OLDER push (a retry of an earlier delivery) AFTER a NEWER one.
    Re-ingesting the OLDER tree would REGRESS the stored graph backwards in history (main_impact then runs
    against a PAST tree → wrong blast radius until the next event self-heals). git shas carry no intrinsic order,
    so the delivery-order-independent clock is the HEAD COMMIT'S TIMESTAMP carried in the push payload
    (head_commit.timestamp — content-free git metadata). The ingest gate stamps the graph version with it and
    REFUSES to overwrite the stored graph with a push whose head-commit time is OLDER than the stored one (a
    stale/reordered retry): the graph keeps the newer tree, the audit landing for the older push is still
    recorded (append-only, idempotent). When the stale push is the one we DROP, the heavy clone/extract is also
    skipped (the gate signals 'stale' before we build). FAIL-SAFE: no timestamp in the payload → the guard is
    inert (ingest exactly as before). Same anti-regression intent #123's self-heal has on the PR path, now made
    monotonic on the push path so it never regresses in the first place. Content-free.

    A THIN ORCHESTRATOR over phase helpers (a behaviour-preserving split of the 172-line original — same gates,
    same return shapes, same fail-open/content-free semantics, only the home of each phase's body changed):
      • fetch/diff           — _push_head_commit_time + _server()._push_changed_sets/_push_author_is_bot below
      • push-to-DB (facts)   — _record_push_facts  (record push + landing, probe the free-tier wall)
      • telemetry (2nd sig)  — _dispatch_cochange  (per-push co-change increment, off-worker, fail-open)
      • graph handoff         — a required strategy function selected by one of the two structurally separate
                               public entry points below
    The intra-cluster ingest calls still dispatch through this module's GLOBALS inside those helpers, so the
    test seams (ingest._full_ingest / ingest._incremental_ingest / ingest._coordinate_paths /
    ingest.increment_cochange_async / ingest._MAX_INGEST_FILES) keep taking effect exactly as before.

    This shared phase has no clone/extractor call. The live webhook can reach it
    only through ``ingest_push_deferred`` and a queue-only strategy, while
    explicit/background callers select the inline graph strategy."""

    # ── FETCH / DIFF: the content-free working set this push touched (paths only, never bodies) + the delivery-
    # order clock (head-commit time) the monotonicity guard reads. SEAT METERING: a bot pusher (sender.type ==
    # 'Bot') stays free — only human operators are seats; the login is sanitized before storage (the '[bot]'
    # marker is stripped), so this webhook signal is the only honest way to keep a bot OUT of the seat count.
    # DELIVERY-ORDER CLOCK. Normally the push payload carries the head commit's time. A PAYLOAD-LESS writer (the
    # self-heal re-ingest, backfill) has none — and writing NULL used to ERASE the stored clock, disarming the
    # reordered-delivery guard for every later push (a backlogged OLDER push then overwrote the just-healed HEAD
    # and the coordinate paid another whole-repo re-ingest, forever). Such callers now pass `captured_at`
    # explicitly — the real HEAD commit time, already present in the branch metadata the freshness read fetched,
    # so this costs NO extra GitHub call. The payload keeps priority when both are present.
    head_time = _push_head_commit_time(payload) or captured_at
    changed, removed, pusher = _server()._push_changed_sets(payload)
    if payload is not None:
        changed_set_complete = _push_changed_set_complete(payload)
        changed_set_base_sha = _bounded_hex_sha(payload.get("before"))
        if _bounded_hex_sha(payload.get("after")) != _bounded_hex_sha(sha):
            changed_set_complete = False
    else:
        changed_set_complete = (
            isinstance(changed_paths, list)
            and all(
                isinstance(path, str)
                and bool(path)
                and len(path) <= 1024
                and "\x00" not in path
                for path in changed_paths
            )
            and len(set(changed_paths)) == len(changed_paths)
        )
        changed_set_base_sha = _bounded_hex_sha(changed_paths_base_sha)
    if changed_set_complete and changed_set_base_sha is None:
        # Paths without the commit they were diffed from are not a patch.
        changed_set_complete = False
    # A payload-less caller (self-heal / backfill) may supply the changed paths it resolved itself, so the
    # incremental path below is reachable without a push payload.  The complete list is retained: silently
    # slicing it at _INCR_CAP would stamp a partially patched graph as current.  _reingest_graph sees the real
    # count and safely takes the full path above the cap.  Treated as ADDED/MODIFIED only; a delete/rename makes
    # the target-sha read or persisted-universe proof fail and likewise falls back to full.
    if not changed and not removed and changed_paths and changed_set_complete:
        changed = list(changed_paths)
    pusher_is_bot = _server()._push_author_is_bot(payload)

    # STAGE 2 — FORCE-PUSH COALESCING (complements the _FairQueue, which bounds CROSS-tenant fairness; this removes
    # the redundant INTRA-branch work): under a storm of pushes to the SAME (repo,branch) only the LATEST must
    # rebuild the graph. 'skip' = a newer push for this branch is already queued → record this push's facts, skip
    # the expensive re-ingest (the newer one rebuilds). FAIL-SAFE: coalesce only ever returns 'skip' when a newer
    # push is PROVABLY queued, so it can never leave a stale graph.
    decision = coalesce(repo, branch, sha) if coalesce else "normal"
    if decision == "skip":
        q = _record_push_facts(db, repo, branch, sha, changed, removed, pusher, pusher_is_bot)
        if q is not None:
            return {"ingested": repo, "branch": branch, "sha": sha, "coalesced": True, "reingested": False, **q}
        # The newer push REBUILDS the graph, but each push carries only its OWN commits[] — so a coalesced push must
        # still fold ITS commits into co-change or that increment is lost (the structural rebuild is what coalesces,
        # not the per-push co-change input). Off-worker, content-free, fail-open.
        _dispatch_cochange(gh, repo, branch, payload)
        return {"ingested": repo, "branch": branch, "sha": sha, "coalesced": True, "reingested": False}

    # STAGE 1 — FREE-TIER WALL: record the facts (push + landing) AND probe the quota in one step, BEFORE the
    # expensive ingest (clone + extract). record_push self-gates and returns the quota_exceeded signal when this
    # account is over the line — so an abuser's infinite pushes cost us neither storage NOR a clone/extract. Over
    # the line → recorded nothing, ingest nothing, return the signal (the push handler posts the honest content-free
    # note). Under the line → the push + landing are now recorded; the ingest proceeds (facts NOT re-recorded below).
    quota = _record_push_facts(db, repo, branch, sha, changed, removed, pusher, pusher_is_bot)
    if quota is not None:
        return graph_executor(
            db, gh, repo, branch, sha, payload,
            quota=quota,
            decision=decision,
            changed=changed,
            removed=removed,
            head_time=head_time,
            changed_set_complete=changed_set_complete,
            changed_set_base_sha=changed_set_base_sha,
            changed_paths_failure_code=changed_paths_failure_code,
        )
    # STAGE 3 — DELIVERY-ORDER MONOTONICITY — CHEAP PRE-CHECK: before the heavy clone/extract, ask the DB whether the
    # stored graph is already at a NEWER commit time than this push (a stale/reordered/retried delivery). If so,
    # SKIP the whole ingest (the gate would refuse the write anyway — this just avoids the clone). The push +
    # landing FACTS were already recorded above (the audit truth of what git did), so the ledger is complete;
    # only the graph rebuild is skipped. Inert when the payload carries no head-commit time. Content-free.
    if head_time is not None and _graph_would_regress(db, repo, branch, sha, head_time):
        _tp = _trace_prefix_for(payload)
        print(f"{_tp}push {repo}@{branch} {sha[:7]} → STALE (older than stored graph, reordered delivery) — graph NOT regressed", flush=True)
        return {"ingested": repo, "branch": branch, "sha": sha, "reingested": False, "stale": True, "mode": "skip"}
    # The push is genuine + in-order (not over quota, not stale, not coalesced-away) → fold ITS commits into the
    # co-change signal off-worker (content-free, no clone, fail-open). Dispatched BEFORE the structural rebuild so a
    # slow rebuild never delays the increment, and it runs on the dedicated pool so it never delays the verdict.
    _dispatch_cochange(gh, repo, branch, payload)
    # Acquire rename-stable identity + every known old/current repo lock before the graph writer joins the shared
    # account lifecycle fence. This preserves the global stable-id→repo→account→coordinate lock order even when a
    # missed rename is discovered on this push. The post-write call below still stamps the newly written row.
    _reconcile_repo_identity(db, repo, payload)
    return graph_executor(
        db, gh, repo, branch, sha, payload,
        quota=None,
        decision=decision,
        changed=changed,
        removed=removed,
        head_time=head_time,
        changed_set_complete=changed_set_complete,
        changed_set_base_sha=changed_set_base_sha,
        changed_paths_failure_code=changed_paths_failure_code,
    )


def _execute_push_graph_inline(
        db, gh, repo: str, branch: str, sha: str, payload: dict | None, *,
        quota, decision, changed, removed, head_time,
        changed_set_complete, changed_set_base_sha,
        changed_paths_failure_code) -> dict:
    """Explicit/background graph strategy; never wired to a live webhook."""
    if quota is not None:
        return {
            "ingested": repo,
            "branch": branch,
            "sha": sha,
            "reingested": False,
            **quota,
        }
    # Pick the incremental fast path or full re-ingest. Facts were recorded by
    # the bounded shared phase exactly once.
    result = _reingest_graph(
        db, gh, repo, branch, sha, payload, decision, changed, removed,
        head_time,
        changed_set_complete=changed_set_complete,
        changed_set_base_sha=changed_set_base_sha,
        changed_set_failure_code=changed_paths_failure_code,
    )
    # Stamp/migrate the stable repository identity after the graph row exists.
    _reconcile_repo_identity(db, repo, payload)
    return result


def _enqueue_push_graph(
        db, _gh, repo: str, branch: str, sha: str, payload: dict | None, *,
        quota, decision, changed, removed, head_time,
        changed_set_complete, changed_set_base_sha,
        changed_paths_failure_code) -> dict:
    """Queue-only live strategy.

    Parameters beyond the coordinate are deliberately accepted to share the
    bounded fact/gating phase with the offline graph writer. None can trigger
    repository content reads here.
    """
    del decision, changed, removed, head_time
    del changed_set_complete, changed_set_base_sha, changed_paths_failure_code
    queued = request_main_graph_refresh(
        db,
        repo,
        branch,
        sha,
        _repo_id_from_payload(payload),
        trace_id=(payload or {}).get("_veripsa_trace_id", ""),
        force=quota is not None,
    )
    if quota is not None:
        return {
            "ingested": repo,
            "branch": branch,
            "sha": sha,
            "reingested": False,
            "mode": "queued",
            "deferred": "quota_paused_graph_refresh_queued",
            "graph_refresh": queued,
            **quota,
        }
    return {
        "ingested": repo,
        "branch": branch,
        "sha": sha,
        "reingested": False,
        "mode": "queued" if queued.get("queued") else "skip",
        "graph_refresh": queued,
        **({"deferred": "graph_refresh_queued"} if queued.get("queued") else {}),
    }


def ingest_push_deferred(
        db, gh, repo: str, branch: str, sha: str,
        payload: dict | None = None, coalesce=None,
        captured_at=None, changed_paths=None,
        changed_paths_base_sha=None,
        changed_paths_failure_code=None) -> dict:
    """Live protected-branch ingress: persist facts + durable graph work only.

    This named entry point has no call-graph edge to clone, tarball extraction,
    incremental extraction, or history co-change population. Its transaction
    commits the signed push facts and exact stable-id/SHA queue coordinate
    together, then the isolated convergence service owns all graph work.
    """
    return _run_push_ingest(
        db, gh, repo, branch, sha, payload, coalesce,
        captured_at, changed_paths, changed_paths_base_sha,
        changed_paths_failure_code,
        graph_executor=_enqueue_push_graph,
    )


def ingest_push(
        db, gh, repo: str, branch: str, sha: str,
        payload: dict | None = None, coalesce=None,
        captured_at=None, changed_paths=None,
        changed_paths_base_sha=None,
        changed_paths_failure_code=None,
        graph_execution: str = "inline") -> dict:
    """Explicit/background push ingestion with a compatibility defer switch.

    Live webhook routing calls ``ingest_push_deferred`` directly. Keeping the
    historical keyword here avoids breaking CLI/tests during rolling upgrade
    without putting this heavy-capable symbol back in the live call graph.
    """
    if graph_execution == "deferred":
        return ingest_push_deferred(
            db, gh, repo, branch, sha, payload, coalesce,
            captured_at, changed_paths, changed_paths_base_sha,
            changed_paths_failure_code,
        )
    if graph_execution != "inline":
        raise ValueError("graph_execution must be 'inline' or 'deferred'")
    return _run_push_ingest(
        db, gh, repo, branch, sha, payload, coalesce,
        captured_at, changed_paths, changed_paths_base_sha,
        changed_paths_failure_code,
        graph_executor=_execute_push_graph_inline,
    )


def self_heal_main_graph(db, gh, repo: str, branch: str, *, trace_id: str = "",
                         expected_repository_id=None, expected_owner_id=None,
                         strict: bool = False,
                         convergence_lease: dict | None = None) -> dict:
    """SELF-HEAL the stale-graph drift: if the STORED main graph's commit_sha is not main's CURRENT HEAD (a
    push was missed/lagged), RE-INGEST main first (idempotent full re-ingest @HEAD) so the PR is then analyzed
    against the up-to-date graph. BOUNDED + GUARDED: re-ingest fires ONLY when the shas differ — already-current
    → no clone/extract (a no-op), so this is NOT work on every event. NEVER-CRASH + FAIL-SAFE: any error (HEAD
    unresolvable, ingest failure) leaves the existing graph in place and the PR is analyzed against whatever is
    stored (no worse than before self-heal) — a heal error must never abort PR analysis. Content-free.
    Returns {healed: bool, stored_sha, head_sha, reason?} — `healed=True` only when a re-ingest actually ran."""
    if not _SELF_HEAL_GRAPH:
        return {"healed": False, "reason": "disabled (VERIPSA_SELF_HEAL_GRAPH=0)"}
    fresh = graph_freshness(db, gh, repo, branch)
    head_sha, stored_sha, behind = fresh.get("head_sha"), fresh.get("stored_sha"), fresh.get("behind")
    if head_sha is None:
        return {"healed": False, "reason": "main HEAD unresolvable", "stored_sha": stored_sha, "head_sha": None}
    if behind is False:                                    # stored graph IS main HEAD → already current (the common case)
        return {"healed": False, "reason": "already current", "stored_sha": stored_sha, "head_sha": head_sha}
    # BEHIND (a missed/lagged push, or a never-ingested coordinate) → re-ingest main @HEAD before analyzing.
    # NOTE on the resurrection guard (audit iter-4 P1): self_heal_main_graph is called BOTH on the LIVE PR event path
    # (inside make_db_processor's single autocommit=False body txn) AND as a background writer from boot-reconcile.
    # The account-live revalidation is therefore NOT placed HERE: a RAISE from assert_account_live would poison the
    # live event's open transaction (a caught Python exception can't un-abort the SQL txn → the rest of the event
    # fails InFailedSqlTransaction). Instead the BACKGROUND caller (_reconcile_one_repo) runs the guard ONCE on its
    # own autocommit connection BEFORE its writes (covering this heal too), and the live PR path is the serialized
    # event tenant (not the lock-asymmetric background vector the audit targets). So the guard lives at the caller.
    _tp = _trace_prefix_str(trace_id)
    try:
        was_cold = stored_sha is None
        graph_db = db
        if strict:
            # Capture an exact activation BEFORE compare/download/extractor work. In the convergence worker this is
            # one fresh autocommit statement, so every stable-id/repo/account lock and the connection are gone when
            # source bytes are fetched. Only the final full/patch persistence call is redirected; its wrapper
            # compares this activation and invokes the existing writer in the same short transaction.
            generation = _capture_repository_graph_generation(
                db, repo, expected_repository_id)
            graph_db = _RepositoryGenerationGraphDB(
                db, repo, _canonical_repository_id(expected_repository_id), generation,
                convergence_lease=convergence_lease)
        # BANDWIDTH: a payload-less heal has no push payload, so _push_changed_sets() returns empty and
        # _reingest_graph's incremental branch can never be taken -- every heal downloads the WHOLE repo
        # tarball (measured: 348-776 files) to catch up a single commit. That outbound traffic is the
        # dominant egress cost of the whole service. When we know BOTH endpoints (a warm coordinate with a
        # stored sha) we can ask GitHub which paths actually changed between them and patch just those.
        # FAIL-SAFE: the strict reader distinguishes a genuine empty comparison from API/malformed failure,
        # and the completeness guard rejects GitHub's ambiguous 300-file ceiling.  Only a proven-complete
        # path list below that ceiling may patch.  Cold coordinates, unavailable/failed compares, and
        # ceiling-sized responses take the exact full path.
        heal_changed = None
        if stored_sha:
            candidate_changed = _complete_compare_changed_paths(
                gh, repo, stored_sha, head_sha
            )
            if (isinstance(candidate_changed, list)
                    and 0 < len(candidate_changed) <= _HEAL_PATCH_MAX_PATHS):
                heal_changed = candidate_changed
            elif candidate_changed:
                print(
                    f"{_tp}self-heal: {len(candidate_changed)} changed paths exceeds "
                    f"the patch budget ({_HEAL_PATCH_MAX_PATHS}) — using one tarball",
                    flush=True,
                )
        # Pass HEAD's real commit time as the delivery-order clock. Without it this payload-less write stored
        # captured_at=NULL, which ERASED the coordinate's clock and disarmed the reordered-delivery guard for
        # every later push — a backlogged OLDER push then overwrote the just-healed HEAD, the next event read
        # `behind` again, and the coordinate paid another whole-repo re-ingest (a closed waste loop; measured as
        # the SAME HEAD fully re-ingested 4x in 90 minutes). The value came free with the freshness HEAD read.
        stats = ingest_push(graph_db, gh, repo, branch, head_sha, payload=None, coalesce=None,
                            captured_at=fresh.get("head_committed_at"),
                            changed_paths=heal_changed,
                            changed_paths_base_sha=stored_sha,
                            changed_paths_failure_code=(
                                "compare_history_unproven"
                                if stored_sha and heal_changed is None
                                else None
                            ))
        if _server()._quota_result(stats) is not None:     # over the free line → ingest refused; do not claim healed
            print(f"{_tp}self-heal SKIPPED repo={repo}@{branch}: free-tier limit (graph left at {(stored_sha or 'none')[:7]})", flush=True)
            return {"healed": False, "reason": "free-tier limit", "stored_sha": stored_sha, "head_sha": head_sha}
        # Graph writers deliberately clear stable identity: graph bytes prove content at a mutable coordinate,
        # not which GitHub object owns it.  A payload-less heal may stamp only after a bounded current point read
        # agrees with BOTH authenticated expectations and the exact full_name.  Failure/mismatch is not a graph
        # failure: keep the successfully healed graph with repo_id=NULL (fail closed for later lifecycle purges).
        if isinstance(stats, dict) and stats.get("mode") in ("full", "patch"):
            _restamp_current_repo_identity(
                db, gh, repo, expected_repository_id, expected_owner_id, trace_prefix=_tp)
        # A cold history seed is advisory, but it is also another repository clone.  The strict caller runs in
        # the dedicated convergence process and may immediately claim the next graph row after this function
        # returns.  Dispatching the old daemon task here would therefore overlap that next graph extractor in the
        # same 512 MiB worker (two scaled workers could become 2 graph children + 2 history clones).  Keep the
        # structural graph authoritative and leave the optional history seed explicitly deferred until it has a
        # durable lower-priority lane sharing the graph capacity controller.  Legacy non-strict callers retain
        # their existing fail-open behavior.
        cochange = None
        if was_cold:
            cochange = (
                {
                    "dispatched": False,
                    "deferred": "durable_low_priority_lane_required",
                }
                if strict
                else _dispatch_cochange_populate(
                    gh, repo, branch, trace_prefix=_tp)
            )
        print(f"{_tp}self-heal repo={repo}@{branch}: stale graph {(stored_sha or 'none')[:7]} → re-ingested @HEAD {head_sha[:7]} "
              f"(mode={stats.get('mode')} files={stats.get('files')} edges={stats.get('edges')})", flush=True)
        return {"healed": True, "stored_sha": stored_sha, "head_sha": head_sha, "ingest": stats,
                **({"cochange_populate": cochange} if cochange is not None else {})}
    except Exception as e:                                  # FAIL-SAFE on legacy callers; background convergence is strict
        print(f"{_tp}self-heal FAILED repo={repo}@{branch} (analyzing against stored graph): {str(e)[:160]}", flush=True)
        if strict:
            raise
        return {"healed": False, "reason": "ingest error", "stored_sha": stored_sha, "head_sha": head_sha}


def converge_main_graph_strict(
        db, gh, repo: str, branch: str, target_sha: str, repository_id,
        *, expected_owner_id=None, trace_id: str = "",
        convergence_request_epoch=None, convergence_slot=None,
        convergence_lease_epoch=None) -> dict:
    """Converge one durable graph request, raising until persistence is authoritative.

    Unlike the legacy PR-time ``self_heal_main_graph`` contract, a background outbox turn may not turn an ingest
    error into a successful dequeue.  This wrapper verifies the stable GitHub object identity, detects a ref that
    advanced beyond the queued target, runs the killable inline ingest only in the background, then re-reads
    freshness.  Success means the stored coordinate is at the requested/current HEAD and current extractor
    generations.  Every transient or unverified state raises so the outbox retains a bounded retry.

    If HEAD advanced after the live event, the old SHA is never written merely to satisfy the queue.  A newer
    epoch is enqueued first and the current turn returns ``superseded=True``; its CAS finish cannot consume the
    new request.
    """
    target = _bounded_hex_sha(target_sha)
    repo_id = _canonical_repository_id(repository_id)
    owner_id = _canonical_repository_id(expected_owner_id)
    if target is None or len(target) < 7 or repo_id is None or owner_id is None:
        raise RuntimeError("strict graph convergence lacks authenticated target identity")
    _tp = _trace_prefix_str(trace_id)

    identity_payload = _current_repo_identity_for_self_heal(
        gh, repo, repo_id, owner_id, trace_prefix=_tp,
        allow_same_owner_rename=True)
    if identity_payload is None:
        raise RuntimeError("strict graph convergence repository identity is unverified")
    canonical_repo = _server()._as_obj(identity_payload.get("repository")).get("full_name")
    if not isinstance(canonical_repo, str) or not canonical_repo:
        raise RuntimeError("strict graph convergence canonical repository name is unverified")
    if canonical_repo != repo:
        # GitHub redirects old full_names after a same-owner rename. Stable id + exact owner id are the authority
        # that lets this background turn follow that mutable label; a transfer changes owner id and was rejected
        # above. Reconcile the lifecycle/graph coordinate first, then replace the same stable-id queue row with the
        # canonical name/HEAD. The old epoch cannot consume the replacement.
        if _reconcile_repo_identity(db, canonical_repo, identity_payload) is None:
            raise RepositoryIdentityBindingError(
                "strict graph convergence could not migrate renamed repository authority")
        if hasattr(gh, "repo_default_branch_head_info_at"):
            renamed_branch, renamed_head, confirmed_name, _renamed_at = (
                gh.repo_default_branch_head_info_at(canonical_repo)
            )
            if confirmed_name != canonical_repo:
                raise RuntimeError("strict graph convergence renamed coordinate changed during resolution")
        else:
            renamed_branch, renamed_head = gh.repo_default_branch_head(canonical_repo)
        renamed_head = _bounded_hex_sha(renamed_head)
        if (not isinstance(renamed_branch, str) or not renamed_branch
                or renamed_head is None or len(renamed_head) < 7):
            raise RuntimeError("strict graph convergence renamed HEAD is unverified")
        newer = request_main_graph_refresh(
            db, canonical_repo, renamed_branch, renamed_head, repo_id,
            trace_id=trace_id, force=True)
        return {
            "superseded": True,
            "renamed": True,
            "target_repo": repo,
            "head_repo": canonical_repo,
            "target_sha": target,
            "head_branch": renamed_branch,
            "head_sha": renamed_head,
            "graph_refresh": newer,
        }
    if not _onboard_repo_identity_allowed(db, repo, repo_id):
        raise RepositoryIdentityBindingError(
            "strict graph convergence repository generation is not live")

    def current_default_coordinate() -> tuple[str, str]:
        if hasattr(gh, "repo_default_branch_head_info_at"):
            actual_branch, actual_head, canonical, _committed_at = (
                gh.repo_default_branch_head_info_at(repo)
            )
            if not isinstance(canonical, str) or canonical != repo:
                raise RuntimeError("strict graph convergence repository name changed")
        else:
            actual_branch, actual_head = gh.repo_default_branch_head(repo)
        if (not isinstance(actual_branch, str) or not actual_branch
                or not isinstance(actual_head, str) or not _bounded_hex_sha(actual_head)):
            raise RuntimeError("strict graph convergence could not resolve current default-branch HEAD")
        return actual_branch, actual_head.lower()

    current_branch, current_head = current_default_coordinate()
    if current_branch != branch or current_head != target.lower():
        newer = request_main_graph_refresh(
            db, repo, current_branch, current_head, repo_id, trace_id=trace_id, force=True)
        print(
            f"{_tp}graph refresh superseded repo={repo}@{branch} "
            f"target={target[:7]} current={current_branch}@{current_head[:7]}",
            flush=True,
        )
        return {
            "superseded": True,
            "target_sha": target,
            "target_branch": branch,
            "head_branch": current_branch,
            "head_sha": current_head,
            "graph_refresh": newer,
        }

    healed = self_heal_main_graph(
        db, gh, repo, current_branch, trace_id=trace_id,
        expected_repository_id=repo_id, expected_owner_id=owner_id,
        strict=True,
        convergence_lease={
            "request_epoch": convergence_request_epoch,
            "slot": convergence_slot,
            "lease_epoch": convergence_lease_epoch,
        },
    )
    if isinstance(healed, dict) and healed.get("reason") == "free-tier limit":
        # This is a deliberate product boundary, not an infrastructure failure. The convergence drainer owns the
        # customer-visible fair-use surface and a long, non-exhausting defer; returning a typed state prevents the
        # ordinary max-attempt path from giving up permanently while still refusing to call the graph converged.
        return {
            **healed,
            "terminal_degraded": "quota_paused",
            "converged": False,
            "target_sha": target,
            "head_sha": target,
        }
    after_branch, after_head = current_default_coordinate()
    if after_branch != current_branch or after_head != target.lower():
        newer = request_main_graph_refresh(
            db, repo, after_branch, after_head, repo_id, trace_id=trace_id, force=True)
        return {
            "superseded": True,
            "target_sha": target,
            "target_branch": current_branch,
            "head_branch": after_branch,
            "head_sha": after_head,
            "graph_refresh": newer,
        }
    fresh_after = graph_freshness_at_target(
        db, repo, after_branch, after_head
    )
    if (fresh_after.get("behind") is not False
            or not isinstance(fresh_after.get("stored_sha"), str)
            or fresh_after["stored_sha"].lower() != target.lower()):
        raise RuntimeError("strict graph convergence did not persist the requested current graph")
    if _reconcile_repo_identity(db, repo, identity_payload) is None:
        raise RepositoryIdentityBindingError(
            "strict graph convergence did not persist repository identity")
    return {
        **(healed if isinstance(healed, dict) else {}),
        "converged": True,
        "target_sha": target,
        "head_sha": target,
    }


def _is_sha_not_yet_fetchable(err: Exception) -> bool:
    """True iff `err` is a transient 'the head sha isn't fetchable YET' — i.e. a 404 (not found) or 410 (gone)
    from the GitHub tarball/contents API for a just-moved ref (the classic force-push replication-lag race).
    Duck-typed on `.code` so it matches urllib.error.HTTPError (live client: `code` attr) WITHOUT importing it
    at module top, and a test fake that raises the same shape. Deliberately NARROW: a 5xx, a rate-limit 403, a
    timeout, or a plain exception are NOT 'not-yet-fetchable' (they must re-raise so the delivery retries/fails
    honestly) — only a clean not-found defers. Never treats a missing/odd code as fetchable-yet (fail closed →
    re-raise) so a malformed error can't be mistaken for the deferrable race."""
    code = getattr(err, "code", None)
    return code in (404, 410)


def _backfill_repo_metadata(repo_value) -> dict:
    """Minimal content-free repository identity copied from GitHub's current PR object into a synthetic replay."""
    repo = _server()._as_obj(repo_value)
    out = {}
    repo_id = _repo_id_from_payload({"repository": {"id": repo.get("id")}})
    full_name = repo.get("full_name")
    owner = _server()._as_obj(repo.get("owner"))
    owner_id = _repo_id_from_payload({"repository": {"id": owner.get("id")}})
    default_branch = repo.get("default_branch")
    if repo_id is not None:
        out["id"] = repo_id
    if isinstance(full_name, str) and full_name.strip():
        out["full_name"] = full_name.strip()[:512]
    if owner_id is not None:
        out["owner_id"] = owner_id
    if isinstance(default_branch, str) and default_branch.strip():
        out["default_branch"] = default_branch.strip()[:512]
    return out


def build_open_pr_backfill_plan(gh, repo: str) -> dict:
    """Freeze the installation replay budget as <=300 immutable PR numbers.

    The CAP+1 read is intentionally performed only by the background convergence worker.  Persisting numbers
    rather than PR objects keeps the shared outbox content-free; each later fair turn re-reads exactly one current
    PR before synthesizing its event, so a close/merge after the snapshot cannot resurrect stale work.
    """
    _S = _server()
    fetched = gh.list_open_pull_requests(repo, _BACKFILL_PR_CAP + 1)
    if not isinstance(fetched, list) or len(fetched) > _BACKFILL_PR_CAP + 1:
        raise RuntimeError("open PR inventory returned a non-list plan")
    capped = fetched[:_BACKFILL_PR_CAP]
    numbers = []
    for raw in capped:
        number = _S._as_obj(raw).get("number")
        if (not isinstance(raw, dict) or not isinstance(number, int)
                or isinstance(number, bool) or number < 1
                or number > 9223372036854775807):
            raise RuntimeError("open PR inventory contained a malformed number")
        numbers.append(number)
    if len(numbers) != len(set(numbers)):
        raise RuntimeError("open PR inventory contained duplicate numbers")
    return {
        "pr_numbers": sorted(numbers),
        "truncated": len(fetched) > _BACKFILL_PR_CAP,
    }


def resolve_repository_onboarding_head(
        db, gh, repo: str, repository_id, expected_owner_id) -> dict:
    """Resolve a canonical default-branch HEAD, or positively prove an empty repository.

    This is deliberately stricter than the legacy two-tuple helper: the result must bind GitHub's current
    canonical full name, stable repository id and owner id to the durable installation route.  A branch/metadata
    404 is allowed to propagate as retryable ambiguity.  Only an explicit ``empty=True`` from a successful
    repository metadata response may omit ``head_sha``.
    """
    repo_id = _canonical_repository_id(repository_id)
    owner_id = _canonical_repository_id(expected_owner_id)
    reader = getattr(gh, "repo_onboarding_head_info", None)
    if (repo_id is None or owner_id is None or not isinstance(repo, str) or not repo
            or not callable(reader)):
        raise RuntimeError("authoritative onboarding metadata reader is unavailable")
    info = _server()._as_obj(reader(repo))
    current_id = _canonical_repository_id(info.get("repository_id"))
    current_owner = _canonical_repository_id(info.get("owner_id"))
    canonical = info.get("full_name")
    branch = info.get("default_branch")
    empty = info.get("empty") is True
    head = _bounded_hex_sha(info.get("head_sha"))
    if (current_id != repo_id or current_owner != owner_id
            or not isinstance(canonical, str) or not canonical
            or not isinstance(branch, str) or not branch or len(branch) > 512):
        raise RuntimeError("onboarding repository metadata did not match durable authority")
    if canonical != repo:
        identity = {
            "repository": {
                "id": current_id, "full_name": canonical,
                "owner": {"id": current_owner},
            }
        }
        if _reconcile_repo_identity(db, canonical, identity) is None:
            raise RuntimeError("onboarding repository rename was not durably reconciled")
        return {"superseded": True, "renamed": True, "repo": canonical}
    if empty:
        if info.get("head_sha") not in (None, ""):
            raise RuntimeError("empty onboarding metadata carried a HEAD")
        return {
            "repo": canonical, "repository_id": current_id,
            "default_branch": branch, "head_sha": None, "empty": True,
        }
    if head is None or len(head) < 7:
        raise RuntimeError("onboarding repository metadata omitted an authoritative HEAD")
    return {
        "repo": canonical, "repository_id": current_id,
        "default_branch": branch, "head_sha": head.lower(), "empty": False,
    }


def _dispatch_synthetic_pr_replay(
        db, gh, repo: str, default_branch: str, repository_id, pr: dict, *,
        default_branch_authoritative: bool, authoritative_replay: bool,
        planned_onboarding_graph_proof=None):
    """Route one already-read PR object through the live handler without opening another DB session."""
    _S = _server()
    pr = _S._as_obj(pr)
    number = pr.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise RuntimeError("synthetic PR replay lacks an exact positive number")
    base = _S._as_obj(pr.get("base"))
    head = _S._as_obj(pr.get("head"))
    base_repo = _backfill_repo_metadata(base.get("repo"))
    head_repo = _backfill_repo_metadata(head.get("repo"))
    explicit_repo_id = _repo_id_from_payload({"repository": {"id": repository_id}})
    base_repo_id = base_repo.get("id") if base_repo.get("full_name") == repo else None
    if explicit_repo_id is not None and base_repo_id is not None and explicit_repo_id != base_repo_id:
        raise RuntimeError("synthetic PR replay repository identity changed")
    replay_repo_id = explicit_repo_id or base_repo_id
    if (replay_repo_id is None
            and planned_onboarding_graph_proof is _PLANNED_ONBOARDING_GRAPH_PROOF):
        raise RuntimeError("synthetic PR replay lacks stable repository identity")
    repository = {
        "full_name": repo,
        "default_branch": default_branch,
    }
    if replay_repo_id is not None:
        repository["id"] = replay_repo_id
    replay_base = {"ref": base.get("ref") or default_branch}
    replay_head = {"sha": head.get("sha")}
    if isinstance(base.get("sha"), str) and base.get("sha"):
        replay_base["sha"] = base["sha"][:64]
    if base_repo:
        replay_base["repo"] = base_repo
    if isinstance(head.get("ref"), str) and head.get("ref"):
        replay_head["ref"] = head["ref"][:512]
    if head_repo:
        replay_head["repo"] = head_repo
    labels = []
    for raw_label in _S._as_list(pr.get("labels"))[:100]:
        name = _S._as_obj(raw_label).get("name")
        if isinstance(name, str) and name.strip():
            labels.append({"name": name.strip()[:100]})
    replay_pr = {
        "base": replay_base,
        "head": replay_head,
        "user": _S._as_obj(pr.get("user")),
        "merged": False,
        "draft": pr.get("draft") is True,
        "labels": labels,
    }
    changed_files = pr.get("changed_files")
    if isinstance(changed_files, int) and not isinstance(changed_files, bool) and changed_files >= 0:
        replay_pr["changed_files"] = changed_files
    payload = {
        "action": "synchronize",
        "number": number,
        "repository": repository,
        "pull_request": replay_pr,
        "_veripsa_no_neighbor_refresh": True,
        "_veripsa_default_branch_authoritative": bool(default_branch_authoritative),
    }
    if base_repo.get("id") is None or head_repo.get("id") is None:
        payload["_veripsa_fork_identity_unknown"] = True
    if authoritative_replay:
        payload["_veripsa_authoritative_pr_replay"] = True
    if planned_onboarding_graph_proof is _PLANNED_ONBOARDING_GRAPH_PROOF:
        return _S.handle_event(
            "pull_request", payload, db, gh,
            planned_onboarding_graph_proof=planned_onboarding_graph_proof)
    # Preserve the historical four-argument seam for ordinary boot/check-suite replay and injected test routers.
    return _S.handle_event("pull_request", payload, db, gh)


class _PlannedOnboardingPRAuthority(NamedTuple):
    current: dict
    repository_id: object
    head_sha: str
    confirmed_fork: bool


def _resolve_planned_onboarding_pr_authority(
        db, gh, repo: str, default_branch: str, target_sha: str,
        repository_id, expected_owner_id, pr_number: int
        ) -> _PlannedOnboardingPRAuthority | dict:
    """Resolve one frozen PR against current repository/default-HEAD authority.

    A terminal dict is an exact lifecycle receipt.  An active result authorizes
    only the named PR coordinate; callers still own their mutually-exclusive
    normal-analysis or quota-surface mutation.
    """
    repo_id = _canonical_repository_id(repository_id)
    owner_id = _canonical_repository_id(expected_owner_id)
    durable_target = _bounded_hex_sha(target_sha)
    if (not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1
            or not isinstance(default_branch, str) or not default_branch
            or durable_target is None or len(durable_target) < 7):
        raise ValueError("planned onboarding PR coordinate is malformed")
    if repo_id is None or owner_id is None:
        raise ValueError("planned onboarding PR lacks stable repository identity")
    getter = getattr(gh, "get_pull_request", None)
    if not callable(getter):
        raise RuntimeError("planned onboarding PR replay requires an authoritative reader")
    current = _server()._as_obj(getter(repo, pr_number))
    if current.get("number") != pr_number:
        raise RuntimeError("planned onboarding PR read returned the wrong object")
    state = current.get("state")
    merged = current.get("merged")
    if state == "closed" or merged is True:
        return {
            "planned_pr": pr_number, "processed": False,
            "skipped": "PR no longer open", "receipt": True,
            "receipt_kind": "authoritative_closed",
        }
    if state != "open" or merged not in (False, None):
        raise RuntimeError("planned onboarding PR state is malformed")
    base = _server()._as_obj(current.get("base"))
    head = _server()._as_obj(current.get("head"))
    base_repo = _backfill_repo_metadata(base.get("repo"))
    head_repo = _backfill_repo_metadata(head.get("repo"))
    if (_canonical_repository_id(base_repo.get("id")) != repo_id
            or _canonical_repository_id(base_repo.get("owner_id")) != owner_id):
        raise RuntimeError("planned onboarding PR base repository identity changed")
    if base_repo.get("full_name") != repo:
        # GitHub can redirect the old full_name and return the same stable repository under its canonical rename.
        # Reuse the strict onboarding identity/owner/default/HEAD proof so the lifecycle gate migrates the queue;
        # never post to or permanently poison the obsolete coordinate.
        renamed = resolve_repository_onboarding_head(
            db, gh, repo, repo_id, owner_id)
        if isinstance(renamed, dict) and renamed.get("superseded") is True:
            return {
                "planned_pr": pr_number, "processed": False, "receipt": False,
                "superseded": True, "receipt_kind": "canonical_repo_changed",
            }
        raise RuntimeError("planned onboarding PR canonical rename was not durably reconciled")
    current_default_branch = base_repo.get("default_branch")
    if not isinstance(current_default_branch, str) or not current_default_branch:
        raise RuntimeError("planned onboarding PR omitted current default-branch authority")
    head_reader = getattr(gh, "repo_branch_head", None)
    if not callable(head_reader):
        raise RuntimeError("planned onboarding PR lacks current branch HEAD authority")
    current_default_head = _bounded_hex_sha(head_reader(repo, current_default_branch))
    if current_default_head is None or len(current_default_head) < 7:
        raise RuntimeError("planned onboarding PR default branch omitted HEAD")
    if (current_default_branch != default_branch
            or current_default_head.lower() != durable_target.lower()):
        # The current PR object supplies canonical stable-id/owner/default-branch metadata; one point HEAD read
        # supplies its current target. A normal old PR base.sha is deliberately NOT used here—GitHub may retain its
        # creation-time base commit after main advances. Same-branch HEAD churn preserves plan/index; a real default
        # branch scope change makes enqueue_graph_refresh clear/refreeze the plan.
        superseding = request_main_graph_refresh(
            db, repo, current_default_branch, current_default_head, repo_id,
            force=True)
        if not isinstance(superseding, dict) or superseding.get("queued") is not True:
            raise RuntimeError("planned onboarding PR branch change was not durably superseded")
        return {
            "planned_pr": pr_number, "processed": False, "receipt": False,
            "superseded": True, "receipt_kind": "default_target_changed",
        }
    if base.get("ref") != default_branch:
        # A current PR object bound to the exact stable base repository but now targeting another branch is an
        # authoritative unsupported-plan receipt. Consume it so one legitimate retarget cannot poison the cursor;
        # a later retarget back is a live webhook/surface-dirty event and reconverges normally.
        return {
            "planned_pr": pr_number, "processed": False, "receipt": True,
            "receipt_kind": "authoritative_unsupported_base",
        }
    base_sha = _bounded_hex_sha(base.get("sha"))
    if base_sha is None or len(base_sha) < 7:
        raise RuntimeError("planned onboarding PR omitted its current base")
    head_sha = _bounded_hex_sha(head.get("sha"))
    if head_sha is None or len(head_sha) < 7:
        raise RuntimeError("planned onboarding PR omitted its current head")
    head_repo_id = _canonical_repository_id(head_repo.get("id"))
    if head_repo_id is None:
        # Unknown is not proof of a fork.  Advancing here could silently consume a same-repository PR after
        # neither its exact Check nor a fork-safe comment reached GitHub.  Retry the same immutable cursor.
        raise RuntimeError("planned onboarding PR head repository identity is unavailable")
    return _PlannedOnboardingPRAuthority(
        current=current,
        repository_id=repo_id,
        head_sha=head_sha,
        confirmed_fork=head_repo_id != repo_id,
    )


def replay_onboarding_pull_request(
        db, gh, repo: str, default_branch: str, target_sha: str,
        repository_id, expected_owner_id, pr_number: int) -> dict:
    """Replay exactly one frozen onboarding PR using the caller's held, tenant-scoped DB session.

    A 404/410 from the exact PR read is ambiguous (replication/permission/lifecycle race) and deliberately
    propagates so the same cursor retries.  A successful current object proving closed/merged is an exact skip
    receipt and may advance the immutable cursor.
    """
    authority = _resolve_planned_onboarding_pr_authority(
        db, gh, repo, default_branch, target_sha,
        repository_id, expected_owner_id, pr_number)
    if isinstance(authority, dict):
        return authority
    current = authority.current
    repo_id = authority.repository_id
    head_sha = authority.head_sha
    confirmed_fork = authority.confirmed_fork

    result = _dispatch_synthetic_pr_replay(
        db, gh, repo, default_branch, repo_id, current,
        default_branch_authoritative=True, authoritative_replay=True,
        planned_onboarding_graph_proof=_PLANNED_ONBOARDING_GRAPH_PROOF)
    bounded_unknown = (
        isinstance(result, dict)
        and result.get(_PLANNED_ONBOARDING_BOUNDED_UNREAD_KEY)
        is _PLANNED_ONBOARDING_GRAPH_PROOF
    )
    if (not isinstance(result, dict) or result.get("noop") is True
            or (result.get("skipped") and not bounded_unknown)):
        raise RuntimeError("planned onboarding PR replay did not analyze the exact snapshot")
    operation = result.get("_veripsa_check_operation")
    exact_check = (
        not confirmed_fork
        and isinstance(operation, dict)
        and operation.get("repo") == repo
        and str(operation.get("sha") or "").lower() == head_sha.lower()
        and operation.get("posted") is True
    )
    surface = result.get("_veripsa_pr_surface_operation")
    exact_fork_comment = (
        confirmed_fork
        and isinstance(surface, dict)
        and surface.get("repo") == repo
        and surface.get("pr_number") == pr_number
        and str(surface.get("sha") or "").lower() == head_sha.lower()
        and surface.get("comment_posted") is True
    )
    if not exact_check and not exact_fork_comment:
        raise RuntimeError("planned onboarding PR replay posted no exact surface")
    return {
        "planned_pr": pr_number, "processed": True,
        "result": result, "receipt": True,
        "receipt_kind": (
            "exact_bounded_unknown_check" if bounded_unknown and exact_check else
            "exact_bounded_unknown_fork_comment" if bounded_unknown else
            "exact_check" if exact_check else "exact_fork_comment"
        ),
    }


def surface_onboarding_quota_paused_pull_request(
        db, gh, repo: str, default_branch: str, target_sha: str,
        repository_id, expected_owner_id, pr_number: int) -> dict:
    """Publish one authority-fenced quota surface without running analysis."""
    authority = _resolve_planned_onboarding_pr_authority(
        db, gh, repo, default_branch, target_sha,
        repository_id, expected_owner_id, pr_number)
    if isinstance(authority, dict):
        return authority

    # Quota cannot authorize graph-derived analysis, claim mutation, or a
    # replay through the ordinary PR brain.  Publish only the existing
    # idempotent fair-use surface, then let the durable onboarding cursor
    # advance by one fair turn after its exact receipt.
    try:
        from render import quota_paused_check, quota_paused_comment_body
        from webhook_coercion import _comment_marker, _marked_comment
        from webhook_posters import _upsert_check_result
    except ImportError:  # imported as a package
        from .render import quota_paused_check, quota_paused_comment_body
        from .webhook_coercion import _comment_marker, _marked_comment
        from .webhook_posters import _upsert_check_result

    comment_id = None
    comment_posted = False
    try:
        response = gh.upsert_comment(
            repo, pr_number, _comment_marker(pr_number),
            _marked_comment(
                pr_number, quota_paused_comment_body(default_branch)))
        comment_posted = True
        if isinstance(response, dict) and isinstance(response.get("id"), int):
            comment_id = response["id"]
    except Exception as exc:
        # The same-repository Check remains a valid visible surface.  A
        # confirmed fork has no base-repository Check authority and will
        # therefore retain this cursor for a later attempts-neutral retry.
        print(
            "onboarding quota surface comment skipped "
            f"repo={repo} pr={pr_number} error_code={type(exc).__name__[:60]}",
            flush=True,
        )
    check = quota_paused_check()
    check_meta = _upsert_check_result(
        gh, repo, authority.head_sha, check["conclusion"], check["title"],
        check["summary"], authority.confirmed_fork,
        pr_number=pr_number, comment_id=comment_id)
    exact_check = (
        not authority.confirmed_fork and check_meta.get("posted") is True)
    exact_fork_comment = authority.confirmed_fork and comment_posted
    if not exact_check and not exact_fork_comment:
        return {
            "planned_pr": pr_number, "processed": False,
            "receipt": False, "retryable_surface": True,
            "receipt_kind": "quota_surface_unconfirmed",
        }
    return {
        "planned_pr": pr_number, "processed": True,
        "receipt": True,
        "receipt_kind": (
            "exact_quota_check" if exact_check
            else "exact_quota_fork_comment"
        ),
    }


def backfill_open_prs(db, gh, repo: str, default_branch: str | None = None, repository_id=None,
                      *, default_branch_authoritative: bool = False) -> dict:
    """Re-run Veripsa for every currently-open PR in a repo.

    GitHub App installation does not replay old pull_request events. This backfill is the repair path for
    repos that install Veripsa after PRs already exist, and for any missed delivery/posting attempt.

    We do NOT receive a real webhook payload here (there is no live event to back this up), so we SYNTHESIZE one
    that looks like a 'synchronize' event and feed it through the SAME handle_event router a live PR takes — the
    backfill is just a replay, never a second code path that could drift from live behavior. The synthesized
    payload carries the repo's DEFAULT branch so handle_event's main-protection filter behaves exactly as it does
    for live events — a backfilled PR targeting the default branch is analyzed; one targeting any other branch is
    skipped (out of scope), the same as a live PR. We derive the default branch once if the caller didn't pass it
    (the onboarding caller already knows it, so it hands it in to save the extra API call)."""
    _S = _server()
    branch_authoritative = bool(default_branch_authoritative and isinstance(default_branch, str)
                                and default_branch)
    if default_branch is None:
        # Prefer the strict metadata reader. The historical head helper guesses ``main`` when GitHub omits the
        # field; that is sufficient for a best-effort PR replay but can never authorize release-by-difference.
        strict_reader = getattr(gh, "repo_default_branch_name", None)
        if callable(strict_reader):
            try:
                strict_branch = strict_reader(repo)
                if isinstance(strict_branch, str) and strict_branch:
                    default_branch = strict_branch
                    branch_authoritative = True
            except Exception:
                pass
    if default_branch is None:
        try:
            default_branch = gh.repo_default_branch_head(repo)[0] or "main"
        except Exception:
            default_branch = "main"
    # COST/SCALE GUARD: a repo with thousands of open PRs must not trigger an API storm. Fetch at most CAP+1
    # PRs — the +1 is the completeness sentinel. Durable installation onboarding does not call this bulk helper;
    # it freezes the number-only inventory once and uses ``replay_onboarding_pull_request`` one fair turn at a time.
    fetched = _S._as_list(gh.list_open_pull_requests(repo, _BACKFILL_PR_CAP + 1))
    capped = fetched[:_BACKFILL_PR_CAP]
    truncated = len(fetched) > _BACKFILL_PR_CAP
    processed = []
    open_change_ids = []   # the live open-PR change_ids ('PR-<n>') we actually saw — the claim-reconcile self-heal
                           # (boot_reconcile → reconcile_repo_claims_with_authority) converges lanes to THIS set.
    for pr in capped:
        pr = _S._as_obj(pr)                                 # a non-object PR entry (malformed API response) → skipped
        number = pr.get("number")
        if number is None:                                  # no PR number → nothing to analyze (a synthesized payload
            continue                                        # with no number would be a guaranteed 'malformed' no-op anyway)
        change_id = _S._change_id(number)
        concluded_raw = db("SELECT core.change_concluded(%s,%s)", (repo, change_id))
        concluded = (concluded_raw if isinstance(concluded_raw, bool)
                     else isinstance(concluded_raw, str)
                     and concluded_raw.strip().lower() in ("t", "true", "1"))
        authoritative_replay = False
        if concluded:
            # A suspend/close leaves only released rows, so the ordinary stale-event guard treats a later
            # synchronize as concluded. Bypass that guard ONLY after re-fetching GitHub's current PR object and
            # proving it is still open. This closes the list-open -> close -> replay race: a stale list entry can
            # never resurrect a closed PR's lanes. Failure/malformed state stays released (safe) and is surfaced as
            # a skipped result; a later genuine PR webhook or boot sweep can retry.
            getter = getattr(gh, "get_pull_request", None)
            if not callable(getter):
                processed.append({"pr": number, "action": "synchronize", "noop": True,
                                  "skipped": "authoritative open state unavailable"})
                continue
            try:
                current = _S._as_obj(getter(repo, number))
            except Exception:
                processed.append({"pr": number, "action": "synchronize", "noop": True,
                                  "skipped": "authoritative open state unavailable"})
                continue
            if (current.get("number") != number
                    or current.get("state") != "open" or bool(current.get("merged"))):
                processed.append({"pr": number, "action": "synchronize", "noop": True,
                                  "skipped": "PR no longer open"})
                continue
            pr = current
            authoritative_replay = True
        open_change_ids.append(change_id)                    # the SAME change_id the PR's claims are keyed by
        processed.append(_dispatch_synthetic_pr_replay(
            db, gh, repo, default_branch, repository_id, pr,
            default_branch_authoritative=branch_authoritative,
            authoritative_replay=authoritative_replay))
    # default_branch is the protected-branch coordinate where PR/branch claims live; surfaced so the caller's
    # claim-reconcile self-heal (reconcile_repo_claims_with_authority) keys on the SAME (repo,branch) coordinate.
    return {"backfilled": repo, "count": len(processed), "truncated": truncated, "results": processed,
            "open_change_ids": open_change_ids, "default_branch": default_branch}


def _reconcile_live_branch_claims(db, gh, repo: str, default_branch: str) -> dict:
    """Converge legacy ``BR-*`` lanes to GitHub's complete live branch inventory.

    The normal delete webhook releases a branch lane immediately. This boot-time backstop covers rows created
    before that handler existed and the rare delivery that was permanently lost. It is deliberately
    fail-closed: no branch-list capability, any malformed/error response, or a CAP sentinel means no DB write.
    Only a complete inventory reaches the App-only gate, which releases the set difference and promotes waiters.

    The branch names and resulting ``BR-*`` ids are content-free refs. ``_branch_change_id`` applies the exact
    182-character normalization used by push-time reservation, including safe handling of long-name collisions:
    if any live ref maps to a capped id the lane stays; only ids with no live preimage are reclaimed.
    """
    if not isinstance(repo, str) or "/" not in repo or not repo.strip():
        raise ValueError("repo must be owner/name")
    if not isinstance(default_branch, str) or not default_branch:
        raise ValueError("default_branch must be a non-empty string")
    lister = getattr(gh, "list_repo_branch_names", None)
    if not callable(lister):
        return {"reconciled": False, "skipped": "branch inventory unavailable"}
    names = lister(repo, _BACKFILL_BRANCH_CAP)
    if not isinstance(names, list):
        raise RuntimeError("branch inventory returned a non-list result")
    if len(names) >= _BACKFILL_BRANCH_CAP:
        return {"reconciled": False, "skipped": "branch inventory truncated",
                "cap": _BACKFILL_BRANCH_CAP, "observed": len(names)}
    if any(not isinstance(name, str) or not name for name in names):
        raise RuntimeError("branch inventory contained a malformed name")
    _S = _server()
    live_change_ids = sorted({_S._branch_change_id(name) for name in names})
    raw = db("SELECT core.reconcile_repo_branch_claims_with_authority(%s,%s,%s)",
             (repo, default_branch, live_change_ids))
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise RuntimeError("branch-claim reconcile returned a malformed result")
    if raw.get("reconciled") is not True:
        raise RuntimeError("branch-claim reconcile did not confirm authority")
    released = raw.get("released_changes")
    if not isinstance(released, list) or any(not isinstance(change, str) for change in released):
        raise RuntimeError("branch-claim reconcile returned malformed released changes")
    return raw


def _queue_backfill_repo(db, gh, repo: str, repository_id=None) -> dict:
    """Queue a live-install repository without cloning on the installation webhook worker.

    The graph + onboarding latch is written in the enclosing installation transaction using only signed payload
    identity. Authoritative metadata/HEAD, open-PR discovery/replay, graph extraction, and the final Watching
    check are owned by the account-fair convergence worker after this transaction commits.
    """
    graph = request_repository_onboarding(db, repo, repository_id)
    # Resource isolation is process-level, not merely "another thread". History populate performs a blobless git
    # clone plus extraction; dispatching it from the webhook container still competes with live acknowledgements
    # for the same CPU/memory and recreates the multi-repository install convoy. Automatic history population is
    # intentionally deferred until it has a durable low-priority lane
    # which shares the graph worker's capacity controller; a fire-and-forget clone in that process could overlap
    # its next graph turn. Keep the live onboarding result explicit without starting any clone/extractor work.
    cochange = {
        "dispatched": False,
        "deferred": "durable_low_priority_lane_required",
    }
    return {
        "backfilled": repo,
        "graph": graph,
        "open_prs": 0,
        "open_prs_deferred": "durable_account_convergence",
        "default_branch": None,
        "head_sha": None,
        "cochange": cochange,
    }


def _onboard_entries(
        gh, repos: list, *, repo_gate=None,
        discover_when_empty: bool = True) -> tuple[list[dict], list[dict]]:
    """Normalize and bound one installation inventory without graph work."""
    _S = _server()
    entries = []
    for raw in _S._as_list(repos):
        obj = _S._as_obj(raw)
        full = obj.get("full_name")
        if not isinstance(full, str) or not full:
            continue
        entry = {"full_name": full}
        if obj.get("id") not in (None, ""):
            entry["id"] = obj.get("id")
        entries.append(entry)
    entry_lister = getattr(gh, "installation_repo_entries", None)
    name_lister = getattr(gh, "installation_repos", None)
    lister = entry_lister if callable(entry_lister) else name_lister
    if not entries and discover_when_empty and callable(lister):
        # All-repositories install (or a redelivered/empty selected payload): the payload carried no repo names, so
        # ask GitHub which repos this installation can see. We discover up to _ONBOARD_DISCOVER_CAP (> the onboard
        # cap) so the accepted/deferred split below is honest: live ingress durably queues only the first
        # _ONBOARD_REPO_CAP, while explicit `_onboard_repos` may cold-start that same bounded slice. Discovery and
        # enqueue are bounded independently; no clone/extract happens on live ingress. Fail-open for legacy/direct
        # callers: no installation_repos method or a transient list error means onboard nothing; ordinary
        # repository webhooks, pushes, or operator repair remain available.
        try:
            discovered = _S._as_list(lister(cap=_ONBOARD_DISCOVER_CAP))
            entries = []
            for raw in discovered:
                obj = _S._as_obj(raw)
                full = obj.get("full_name") if obj else (raw if isinstance(raw, str) else None)
                if not isinstance(full, str) or not full:
                    continue
                entry = {"full_name": full}
                if obj.get("id") not in (None, ""):
                    entry["id"] = obj.get("id")
                entries.append(entry)
            if entries:
                print(f"onboard: payload named no repos → enumerated {len(entries)} via installation_repos (All-repos install)", flush=True)
        except Exception as e:
            print(f"onboard: payload named no repos and installation_repos failed ({str(e)[:120]}) — deferring to live webhooks", flush=True)
            entries = []
    if callable(repo_gate):
        allowed_entries = []
        blocked = 0
        for entry in entries:
            if repo_gate(entry):
                allowed_entries.append(entry)
            else:
                blocked += 1
        entries = allowed_entries
        if blocked:
            print(f"onboard: repository lifecycle gate deferred {blocked} repo(s)", flush=True)
    return entries[:_ONBOARD_REPO_CAP], entries[_ONBOARD_REPO_CAP:]


def _onboard_repos(
        db, gh, repos: list, *, repo_gate=None,
        discover_when_empty: bool = True) -> tuple:
    """Explicit/CLI cold start that may clone and extract repository content.

    One repository failure remains isolated so an org install can continue.
    Live webhook routing never calls this symbol; it uses the queue-only entry
    point below.
    """
    eager, deferred = _onboard_entries(
        gh,
        repos,
        repo_gate=repo_gate,
        discover_when_empty=discover_when_empty,
    )
    results = []
    for entry in eager:
        n = entry["full_name"]
        try:
            results.append(
                backfill_repo(db, gh, n, repository_id=entry.get("id")))
        except RepositoryIdentityBindingError:
            # DB authority is not an expected per-repo GitHub/clone failure. The durable delivery must retry it;
            # swallowing it here can commit a successful graph ingest without the stable identity required for
            # offboarding and resume safety. The outer per-repo fan-out still isolates successful siblings.
            raise
        except Exception as e:
            print(f"onboard repo={n} FAILED (skipped, install continues): {str(e)[:160]}", flush=True)
            results.append({"backfilled": n, "onboard_error": str(e)[:200]})
    return results, len(deferred)


def _queue_onboard_repos(
        db, gh, repos: list, *, repo_gate=None,
        discover_when_empty: bool = True) -> tuple:
    """Live installation ingress: bounded metadata + durable graph requests.

    Every eager repository is queued in the surrounding delivery transaction.
    Any failure propagates so that transaction rolls back and durable delivery
    recovery retries the exact installation event. This call graph contains no
    clone, extractor, or history-clone function.
    """
    eager, deferred = _onboard_entries(
        gh,
        repos,
        repo_gate=repo_gate,
        discover_when_empty=discover_when_empty,
    )
    results = []
    for entry in eager:
        results.append(
            _queue_backfill_repo(
                db,
                gh,
                entry["full_name"],
                repository_id=entry.get("id"),
            )
        )
    return results, len(deferred)


def backfill_repo(db, gh, repo: str, repository_id=None) -> dict:
    """Explicit/CLI cold-start repair for one repository.

    GitHub does not replay history on install, so this operator path first ingests the default-branch graph and
    then re-runs every bounded open PR. Live installation webhooks do not call this function: they durably queue
    phased, account-fair convergence and return before any HEAD/PR read, clone, or extraction.

    EACH external step is independently guarded so a partial failure degrades HONESTLY instead of aborting:
    a repo where the App lacks one permission (e.g. metadata granted but pull_requests not), an archived/disabled
    repo, or a transient GitHub error after retries — must NOT raise out of here, because (a) backfill_open_prs's
    list-PRs fetch is a separate API call from graph ingest and can fail on its own, and (b) the explicit
    `_onboard_repos` loop should report one failed repair without stranding later repositories."""
    graph = {}                                                      # stays {} (or an error dict) if the ingest step fails
    default_branch = None
    head_sha = None                                                 # surfaced so the onboarding 'watching' signal can post
                                                                    # on the default-branch HEAD without a SECOND API round-trip
    # STEP 1 — ingest the default-branch graph. Resolve the default branch + its HEAD sha, then full-ingest it as
    # the baseline (an empty sha = a brand-new/empty repo → nothing to ingest, leave graph {}). This is a cold
    # baseline, NOT a landing, so we pass payload=None (no push facts / per-file landing record to write).
    # TWO outcomes for "no HEAD":
    #   (a) repo_default_branch_head returns sha="" — the branches API succeeded but the branch has no commits.
    #       head_sha stays None; the watcher signal fires on first push instead (cold-start path).
    #   (b) The branches API itself 404s (a brand-new repo with NO default branch yet, or a repo with no branches
    #       at all) — GitHub returns HTTP 404 on /repos/{repo}/branches/{branch}. This is NOT a product error; it
    #       is an honest empty-repo state. We catch it as `empty_repo: True`, NOT as `ingest_error`, so the
    #       onboarding audit never reads a false failure signal for a legitimately empty repository.
    try:
        branch, sha = gh.repo_default_branch_head(repo)
        default_branch = branch                                     # remember it for STEP 2's main-protection filter
        head_sha = sha or None                                      # '' (empty repo, no commits) → None → watcher fires on first push
        if sha:
            graph = ingest_push(db, gh, repo, branch, sha, None)
    except Exception as e:                                          # never let one repo's ingest abort onboarding
        if getattr(e, "code", None) == 404:
            # Empty repo: the default-branch reference does not exist yet (no commits at all).  Not an error — a
            # legitimate state.  The watcher signal will fire on the first real push to main (the cold-start path
            # in the push handler detects cold_start=True from ingest_push and posts the check then).
            graph = {"empty_repo": True}
            print(f"backfill repo={repo}: no default-branch HEAD (empty repo, no commits yet) — watcher fires on first push", flush=True)
        else:
            graph = {"ingest_error": str(e)[:200]}
    identity = _bind_onboarded_repo_identity(db, repo, repository_id) if repository_id is not None else None
    if (repository_id is not None and isinstance(graph, dict) and graph.get("mode") in ("full", "patch")
            and not (isinstance(identity, dict) and identity.get("activation_recorded"))):
        raise RepositoryIdentityBindingError("account-onboarded repository graph identity was not observed")
    # STEP 1b — SEED the content-free co-change signal from the branch's HISTORY (a SECOND, advisory detector:
    # files that keep changing together are coupled even with no code edge). DISPATCHED OFF the event worker
    # (populate_cochange_async → the dedicated co-change pool): co-change's feeder is a git clone (I/O-bound), and
    # running it inline here would stall every queued PR/push behind a slow clone AND hold this repo's advisory
    # lock for the clone's whole duration. So we fire-and-forget — backfill returns now; the pool clones/extracts
    # holding no lock, then briefly tenant-pins + locks to store. Fail-open: a repo we can't clone still onboards
    # its graph + PRs. Only when a real HEAD exists (an empty repo has no history to seed from).
    cochange = {"ok": False, "skipped": "no head"}
    if default_branch and head_sha:
        cochange = {"dispatched": bool(populate_cochange_async(
            gh, repo, default_branch, repository_id=repository_id))}
    # STEP 2 — re-run every open PR. This is a SEPARATE GitHub API call (list_open_pull_requests) and thus a
    # SEPARATE FAILURE DOMAIN from the graph ingest above: the App can hold metadata/contents read yet lack
    # pull_requests read, so the list call can 403 even when the graph ingested fine. Guard it on its own so a
    # repo whose PRs can't be listed still onboards its graph (returns an honest 'prs_error') and, crucially,
    # never aborts the rest of the org install.
    try:
        prs = backfill_open_prs(db, gh, repo, default_branch, repository_id=repository_id)
        open_prs = prs.get("count", 0)
    except Exception as e:
        print(f"open-PR backfill repo={repo} FAILED (graph kept, PRs deferred to their own webhooks): {str(e)[:160]}", flush=True)
        return {"backfilled": repo, "graph": graph, "open_prs": 0, "prs_error": str(e)[:200],
                "cochange": cochange, "default_branch": default_branch, "head_sha": head_sha,
                "repository_identity": identity}
    return {"backfilled": repo, "graph": graph, "open_prs": open_prs,
            "cochange": cochange, "default_branch": default_branch, "head_sha": head_sha,
            "repository_identity": identity}


def purge_repo(db, repo: str, repository_id=None, reason: str = "installation_removed",
               delivery_key: str | None = None, gh=None) -> dict:
    """OFFBOARDING (the symmetric counterpart to backfill_repo). The App was uninstalled, or the repo was
    removed / deleted → FORGET the content-free working set we hold for that repo across ALL its coordinates:
    the code graph (nodes/edges/version) + the live claim/lane state. The append-only event ledger (push /
    landed audit, content-free) is retained by design (the immutable tamper-evidence moat) — see RUNBOOK.
    A DB failure MUST raise: the durable inbox then releases the delivery for a bounded retry. Returning
    `ok:false` would incorrectly mark the deletion delivery done and strand the working set forever. The
    content-free delivery key lets the DB authenticate receive order for legacy payloads missing repository.id.
    Missing delivery authority fails closed. The three-argument DB surface is disabled; the one-argument name is a
    rollback-only shim that independently resolves a unique ID-bearing processing deletion and delegates to this
    same durable four-argument boundary, never a mutable-full_name purge. A DB preflight schedules a pre-fix ID-less
    delivery until its consistency grace expires before any GitHub call can consume retry budget. The new worker then
    resolves it with a current installation-token point read: a visible identity preserves the current object, a 404
    drives the dedicated confirmed-absence purge, and unreadable evidence remains unfinalized for durable recovery."""
    repo_id = str(repository_id).strip() if repository_id not in (None, "") else None
    if not delivery_key:
        raise RuntimeError(f"repository offboard needs durable delivery authority for {repo}")
    if repo_id is None:
        preflight = db(
            "SELECT core.prepare_legacy_repository_offboard_with_authority(%s,%s,%s)",
            (repo, reason, delivery_key),
        )
        if isinstance(preflight, str):
            preflight = json.loads(preflight)
        if not isinstance(preflight, dict) or not preflight.get("ok"):
            raise RuntimeError(f"legacy repository offboard preflight failed for {repo}")
        if preflight.get("deferred") is True:
            return preflight
        identity_reader = getattr(gh, "repo_current_identity", None)
        if not callable(identity_reader):
            raise RuntimeError(f"legacy repository offboard needs a current GitHub identity reader for {repo}")
        current = identity_reader(repo)
        if current is not None:
            current_full_name = current.get("full_name") if isinstance(current, dict) else None
            current_id = _canonical_repository_id(
                current.get("id") if isinstance(current, dict) else None)
            if not current_id:
                raise RuntimeError(f"legacy repository offboard received malformed current identity for {repo}")
            if current_full_name == repo:
                res = db(
                    "SELECT core.resolve_legacy_repository_offboard_with_authority(%s,%s,%s,%s)",
                    (repo, current_id, reason, delivery_key),
                )
            else:
                # GitHub redirected the old coordinate to a differently-named live repo. Purge only the old
                # coordinate; the canonical current coordinate is outside this name-only delivery's target.
                res = db("SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
                         (repo, None, reason, delivery_key))
        else:
            res = db(
                "SELECT core.confirm_absent_legacy_repository_offboard_with_authority(%s,%s,%s)",
                (repo, reason, delivery_key),
            )
    else:
        res = db("SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
                 (repo, repo_id, reason, delivery_key))
    if isinstance(res, str):
        res = json.loads(res)
    if not isinstance(res, dict) or not res.get("ok"):
        raise RuntimeError(f"repository offboard failed for {repo}")
    return res


def purge_account_working_set(db, deletion_proof: dict, delivery_key: str) -> dict:
    """UNINSTALL purge done ACCOUNT-WIDE (audit r4 — the privacy fix). FORGET the pinned tenant's ENTIRE
    content-free working set (code graph + live claim/lane state) across every repo — what the
    `installation.deleted` payload named or NOT (GitHub omits the `repositories` array for an "All repositories"
    install). The append-only event ledger is retained by design (see purge_account_working_set_with_authority).
    A DB failure MUST raise so the durable delivery is retried; success-with-residue is not offboarding. The
    authenticated durable delivery key is transaction-local context for the DB gate: its immutable received_at is
    the purge cutoff, so a newer queued reinstall event is never deleted by an older uninstall.  The explicit
    App-JWT deletion proof is mandatory; the proof-less SQL shim intentionally refuses old workers."""
    if not isinstance(deletion_proof, dict) or not deletion_proof or not delivery_key:
        raise RuntimeError("account working-set purge needs durable deletion proof")
    res = db(
        "WITH delivery_context AS MATERIALIZED ("
        " SELECT set_config('core.current_delivery_key',%s,true)"
        ") SELECT core.purge_account_working_set_with_authority(%s::jsonb)"
        " FROM delivery_context",
        (
            str(delivery_key)[:200],
            json.dumps(deletion_proof, separators=(",", ":")),
        ),
    )
    if isinstance(res, str):
        res = json.loads(res)
    if not isinstance(res, dict) or not res.get("ok"):
        raise RuntimeError("account working-set purge failed")
    return res


def _boot_reconcile_route_page(db, cap: int) -> dict:
    """Read one bounded durable lifecycle-route page.

    This is deliberately a DB capability, not an App-inventory fallback.  A bounded
    ``/app/installations`` response cannot prove completeness once the fleet exceeds
    its cap, and a per-installation repository cap permanently repeats page one.
    """
    raw = db(
        "SELECT core.read_boot_reconcile_route_page_with_authority(%s)",
        (int(cap),),
    )
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise RuntimeError("boot reconcile route page returned a malformed result")
    routes = raw.get("routes")
    cursor = raw.get("cursor")
    if not isinstance(routes, list) or not isinstance(cursor, dict):
        raise RuntimeError("boot reconcile route page omitted routes or cursor")
    return raw


def _advance_boot_reconcile_route_cursor(db, expected: dict, route: dict) -> bool:
    raw = db(
        "SELECT core.advance_boot_reconcile_route_cursor_with_authority("
        "%s,%s,%s,%s)",
        (
            expected.get("account_id"),
            expected.get("repository_id"),
            route.get("account_id"),
            route.get("repository_id"),
        ),
    )
    if isinstance(raw, str):
        raw = json.loads(raw)
    return isinstance(raw, dict) and raw.get("advanced") is True


def boot_reconcile(db, gh, cap: int = 200, dsn: str | None = None, deadline_seconds: int = 0) -> dict:
    """SELF-HEAL after a restart. The webhook queue is in-memory and we 202-ACK on enqueue, so an event in
    flight when the process restarts (deploy / crash / platform recycle) is lost — and GitHub does NOT redeliver
    an already-202'd delivery, so that PR would silently never get its check until its author next pushes. On
    boot we reconcile OPEN PRs (idempotent upsert → re-posts the right check) so every restart converges to the
    truth. Repos = VERIPSA_BACKFILL_REPOS if set, else the installation's own repos (bounded by `cap`, so a huge
    install can't make boot hammer the API). Best-effort: one repo's failure is logged and skipped, never fatal.

    PER-WAKE TIME BUDGET (defense-in-depth on top of `cap` + the skip-if-recent throttle): `cap` bounds the COUNT
    of repos the sweep touches, but a small number of huge repos (or slow GitHub responses) could still drag the
    sweep past a typical webhook-quiet window — and a stuck sweep delays nothing customer-facing (the live webhook
    path is the primary self-heal), it just wastes work that's already covered by live events. `deadline_seconds`
    (0 = unlimited, default — behaviour-preserving) BREAKS BETWEEN REPOS once the wall-clock budget is exceeded;
    the remaining repos are reported as `deferred` and pick up their own next live webhook (the same safety-net
    contract `cap`-truncation has). The deadline is checked between repos only — a repo already mid-reconcile is
    never preempted (it holds a per-repo lock + a pinned tenant + an open backfill, so an abort there would leak).
    Content-free: only the count of deferred repos is logged + returned, never a repo name.

    PER-REPO SERIALIZATION (concurrency safety): the LIVE webhook path (make_db_processor) holds a per-(account,
    repo) advisory lock (server._take_repo_lock → two-arg pg_advisory_lock(hashtext(account), hashtext(repo))) so
    two events for the SAME coordinate are processed serially — the 'no concurrent writes, no races' guarantee,
    safe even across the 2-instance overlap Render does on every rolling deploy, and without colliding a DIFFERENT
    tenant's same-named repo. boot_reconcile writes the SAME per-repo rows at startup, so a webhook for a repo arriving WHILE
    boot is reconciling that repo would INTERLEAVE / double-write without the same lock. We therefore take the
    SAME per-repo lock around each repo's reconcile and run that repo's backfill over the SINGLE locked connection
    (so the lock actually covers the work — exactly the live pattern). Deadlock-free: we lock ONE repo at a time
    and release it BEFORE moving to the next (no two locks ever held at once). Idempotent: still an upsert. When
    `dsn` is None (no live connection available — e.g. a unit test that supplies only a fake `db`) we fall back to
    the unlocked connect-per-query path so the reconcile still runs (degraded but never broken).

    MULTI-INSTALLATION + FAIRNESS: production does NOT enumerate ``GET /app/installations`` here. That API walk is
    operationally capped, so it cannot prove that an account after the cap is absent; asking every visible install
    for only its first ``cap`` repositories also repeats page one forever. PostgreSQL already holds the live,
    lifecycle-fenced installation→repository routes. One bounded page is selected from those rows with a durable
    composite keyset cursor, then every repository is reconciled with ``gh.for_installation(exact_id)``. The cursor
    advances after every attempted repository (success or fail-soft failure), and wraps after the last key, so a
    poison tenant and a large first installation cannot starve the tail. Cursor state contains only bounded account
    and stable repository ids — never repository names, paths, PR ids, source, diffs, or payloads.

    Compatibility: a test/operator that supplies the explicit ``VERIPSA_BACKFILL_REPOS`` allowlist keeps the
    singleton path, and a dsn-less legacy/unit caller with no DB runner keeps the predecessor App-enumeration path.
    The production worker always supplies a DB runner and therefore never treats a partial GitHub list as complete."""
    env_repos = [r.strip() for r in os.environ.get("VERIPSA_BACKFILL_REPOS", "").split(",") if r.strip()]
    env_repos_set = set(env_repos)

    # Work tuple:
    #   (repo, stable_repository_id, already_scoped_client, exact_installation_id, durable_route)
    # The durable path defers token minting until the per-route failure boundary so one broken installation cannot
    # prevent later accounts from running. The explicit allowlist is intentionally the singleton operator path.
    work = []
    cursor_mode = False
    cursor_expected = {"account_id": None, "repository_id": None}
    installations_seen = 0
    if env_repos:
        work = [(repo, None, gh, None, None) for repo in env_repos[:cap]]
        installations_seen = 1 if work else 0
    elif db is not None:
        cursor_mode = True
        try:
            page = _boot_reconcile_route_page(db, cap)
        except Exception as e:
            print(
                "boot reconcile: durable route page unavailable "
                f"({str(e)[:160]}) — deferring to durable/live webhooks",
                flush=True,
            )
            return {
                "reconciled": 0,
                "failed": 0,
                "deferred": 0,
                "repos": 0,
                "installations": 0,
                "error": str(e)[:200],
            }
        cursor_expected = dict(page["cursor"])
        installations = set()
        for route in page["routes"][:cap]:
            if not isinstance(route, dict):
                continue
            repo = route.get("repo")
            repository_id = route.get("repository_id")
            installation_id = route.get("github_installation_id")
            installation_created_at = route.get(
                "github_installation_created_at")
            account_id = route.get("account_id")
            if not all(isinstance(value, str) and value for value in (
                    repo, repository_id, installation_id,
                    installation_created_at, account_id)):
                continue
            installations.add(installation_id)
            work.append((repo, repository_id, None, installation_id, route))
        installations_seen = len(installations)
    else:
        # Legacy/test embedding only. Production always supplies the DB runner above. Keeping this branch lets
        # small dsn-less fakes exercise the historical behavior without making a partial App list production
        # correctness authority.
        legacy_single = not hasattr(gh, "list_app_installations")
        if not legacy_single:
            try:
                installs = gh.list_app_installations()
                inst_ids = [
                    entry.get("installation_id")
                    for entry in (installs or [])
                    if isinstance(entry, dict) and entry.get("installation_id")
                ]
            except Exception as e:
                print(
                    "boot reconcile: could not list App installations "
                    f"({str(e)[:160]}) — falling back to the primary install",
                    flush=True,
                )
                legacy_single, inst_ids = True, [None]
            else:
                if not inst_ids:
                    return {"reconciled": 0, "repos": 0, "installations": 0}
        else:
            inst_ids = [None]
        seen_repos = set()
        for installation_id in inst_ids:
            if len(work) >= cap:
                break
            gh_inst = gh.for_installation(installation_id) if installation_id is not None else gh
            installations_seen += 1
            try:
                entry_lister = getattr(gh_inst, "installation_repo_entries", None)
                raw_repos = (
                    entry_lister(cap=cap)
                    if callable(entry_lister)
                    else gh_inst.installation_repos(cap=cap)
                )
            except Exception as e:
                if legacy_single:
                    print(
                        "boot reconcile: could not list installation repos "
                        f"({str(e)[:160]}) — deferring to live webhooks",
                        flush=True,
                    )
                    return {"reconciled": 0, "repos": 0, "error": str(e)[:200]}
                print(
                    "boot reconcile: could not list repos for one installation "
                    f"({str(e)[:160]}) — skipping it",
                    flush=True,
                )
                continue
            for raw in raw_repos:
                if len(work) >= cap:
                    break
                obj = raw if isinstance(raw, dict) else {}
                repo = obj.get("full_name") if obj else (raw if isinstance(raw, str) else None)
                if not isinstance(repo, str) or not repo or repo in seen_repos:
                    continue
                if env_repos_set and repo not in env_repos_set:
                    continue
                seen_repos.add(repo)
                work.append((repo, obj.get("id"), gh_inst, None, None))

    done, failed, deferred = 0, 0, 0
    cursor_healthy = True
    # PER-WAKE WALL-CLOCK DEADLINE (0 = unlimited; behaviour-preserving). Resolved once OUTSIDE the loop so the
    # budget is genuinely "this sweep" and not extended by each iteration. monotonic() is unaffected by clock skew.
    _t0 = time.monotonic() if deadline_seconds and deadline_seconds > 0 else None
    for i, (repo, repository_id, gh_inst, installation_id, route) in enumerate(work):
        # CHECK THE DEADLINE BETWEEN REPOS, BEFORE starting a new repo's reconcile — never preempt a repo
        # mid-reconcile (a repo in flight holds the per-(account,repo) advisory lock + the tenant pin GUC on
        # one held connection and has an open backfill loop; aborting there would leak the lock / leave a
        # half-written claim). Content-free: log the count of deferred repos, never their names. The deferred
        # repos defer to their own next live webhook (the SAME safety-net contract `cap`-truncation already has).
        if _t0 is not None and (time.monotonic() - _t0) >= deadline_seconds:
            deferred = len(work) - i
            print(f"boot reconcile DEADLINE — stopped after {i}/{len(work)} repos "
                  f"({deferred} deferred to live webhooks; budget={deadline_seconds}s)", flush=True)
            break
        # PER-REPO ISOLATION: each repo is its OWN failure domain. A single repo that raises mid-sweep
        # (a since-deleted/renamed repo, a GitHub 404/410, a transient 5xx that survived the API retries)
        # must be caught, COUNTED, and skipped — never abort the repos that come after it (which would
        # strand every later open PR until its author next pushes). Content-free: we log only the repo
        # coordinate + a clamped error string — never secrets/tokens/file contents. Each repo reconciles
        # through gh_inst (the client for the installation that owns it), so the right per-tenant token is used.
        try:
            if gh_inst is None:
                gh_inst = gh.for_installation(installation_id)
                if gh_inst is None:
                    raise RuntimeError("exact installation client was unavailable")
            print(json.dumps(_reconcile_one_repo(
                db,
                gh_inst,
                repo,
                dsn,
                repository_id=repository_id,
                expected_account_id=route.get("account_id") if route else None,
                expected_installation_id=(
                    route.get("github_installation_id") if route else None),
                expected_installation_created_at=(
                    route.get("github_installation_created_at")
                    if route else None),
            )), flush=True)
            done += 1
        except Exception as e:   # never let one repo abort the sweep (the live webhook is still the primary path)
            failed += 1
            print(f"boot reconcile repo={repo} FAILED (deferred to its own webhooks): {str(e)[:160]}", flush=True)
        if cursor_mode:
            try:
                cursor_healthy = _advance_boot_reconcile_route_cursor(
                    db, cursor_expected, route)
            except Exception as e:
                cursor_healthy = False
                print(
                    "boot reconcile: durable cursor advance failed "
                    f"({str(e)[:160]}) — stopping before the next route",
                    flush=True,
                )
            if not cursor_healthy:
                deferred = len(work) - i - 1
                break
            cursor_expected = {
                "account_id": route["account_id"],
                "repository_id": route["repository_id"],
            }
    return {"reconciled": done, "failed": failed, "deferred": deferred,
            "repos": len(work), "installations": installations_seen,
            "cursor_healthy": cursor_healthy}


def _reconcile_one_repo(
        db,
        gh,
        repo: str,
        dsn: str | None,
        repository_id=None,
        *,
        expected_account_id=None,
        expected_installation_id=None,
        expected_installation_created_at=None) -> dict:
    """Reconcile ONE repo's open PRs under the SAME per-(account,repo) advisory lock the live webhook path holds
    (server._take_repo_lock, keyed by the owning-account id + repo), so a webhook for this repo arriving mid-boot
    can't interleave / double-write. Lock is SESSION-level on one held
    connection (a connect-per-query lock would release the instant its query's connection closes — useless), and
    the repo's backfill runs over THAT SAME connection so the lock genuinely covers the write. Released per repo
    in a finally (incl. on error), before the caller moves to the next repo → one lock at a time, deadlock-free.

    TENANT PIN (the restart self-heal's correctness, not just its concurrency): the backfill does gate WRITES
    (act_for_claim_with_authority via the synthesized PR events). Those writes resolve their tenant from the
    SESSION GUC core.installation_account. App-proven activation provisions that route; ordinary and background
    work may only resolve it with enter_existing_installation_with_authority. Unpinned, session_user='veripsa_app'
    falls through resolve_session_identity to the credential lookup: in a CLEAN PROD deploy there is NO veripsa_app
    credential row (RUNBOOK/render.yaml forbid it), so every write RAISES 42501 → every repo is counted failed and
    skipped → the self-heal is a silent NO-OP exactly when it's needed (open PRs get no check until their author
    next pushes). So before the backfill we pin the SAME tenant the live path would: the owning ACCOUNT id, keyed
    IDENTICALLY (live keys by repository.owner.id → 'ACCT-GH-'||<owner_id>; we resolve that owner id from the
    installation via gh.installation_account_id() and resolve that existing route). Keys
    MUST match or boot heals into a DIFFERENT tenant than live (a split-tenant bug). The pin is set on the SAME
    held connection (a session GUC, like the lock) so it covers the whole repo's backfill. If the owner id can't
    be resolved (no repos visible / malformed response) we FAIL CLOSED — skip this repo (raise) rather than write
    into an unrouted/wrong tenant; the caller counts it failed and the repo's own next webhook reconciles it.

    No dsn → no live connection to hold a session GUC on; fall back to the unlocked connect-per-query path. That
    path does NOT (cannot) pin a session tenant — a connect-per-query db opens a fresh connection per statement, so
    a GUC set on one is gone by the next. Against a REAL clean-prod DB it therefore fails CLOSED (42501, per above)
    — never a silent cross-tenant write. It exists only for unit tests that supply a fake db (backfill_open_prs
    monkeypatched → no real write). serve()'s production boot ALWAYS passes a real dsn, so the live self-heal
    always takes the pinned, locked path below; this branch is never the prod path."""
    exact_generation = (
        expected_account_id is not None
        or expected_installation_id is not None
        or expected_installation_created_at is not None
    )
    if exact_generation and not all((
            expected_account_id,
            expected_installation_id,
            expected_installation_created_at,
            repository_id)):
        raise RuntimeError("boot reconcile exact route generation is incomplete")
    if dsn is None and exact_generation:
        raise RuntimeError(
            "boot reconcile exact route generation requires a held DB session")
    if dsn is None:                                          # no live DSN → can't hold a session GUC (lock OR tenant
        if repository_id is None:
            return backfill_open_prs(db, gh, repo)           # pin); unit-test/legacy fallback
        return backfill_open_prs(db, gh, repo, repository_id=repository_id)
    # OWNING-ACCOUNT id, resolved the SAME way the live path keys the tenant (repository.owner.id). Resolved BEFORE
    # opening the held connection so a transient GitHub failure here is THIS repo's failure (caller catches it),
    # not a half-pinned connection. None (no repos / malformed) → fail closed: do not write into an unrouted tenant.
    account_key = gh.installation_account_id()
    if not account_key:
        raise RuntimeError(f"boot reconcile repo={repo}: could not resolve owning-account id "
                           f"(no repos visible / malformed response) — skipping to avoid an unrouted-tenant write")
    import psycopg2
    # ONE dedicated connection held for the whole repo reconcile. It MUST be the same connection that takes the
    # lock AND does the work — a session-level advisory lock is bound to its connection, so locking on a throwaway
    # connect-per-query would release the instant that query's connection closed (the lock would protect nothing).
    conn = psycopg2.connect(dsn)
    _S = _server()
    lifecycle_account = None
    lifecycle_lock_taken = False
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            # This autocommit background path spans many statements, so the DB gate's xact lock alone would end
            # immediately. The shared helper arms bounded waits and holds the stable-object key for the full pass.
            _S._take_repository_id_lock(cur, repository_id)
            # SAME per-(account,repo) lock KEY + SCHEME as the live webhook path (server._take_repo_lock): keyed by
            # (account_key, repo) via the two-arg pg_advisory_lock, so boot and live serialize the SAME coordinate
            # across processes and NEVER collide a different tenant's same-named repo. account_key here is the bare
            # owner id (gh.installation_account_id()) — IDENTICALLY what _event_account_key feeds the live lock.
            _S._take_repo_lock(cur, account_key, repo)
            # Background self-heal may use an existing live route but must never provision one. Only an exact
            # App-JWT-proven activation delivery creates/reactivates a tenant; this prevents a late boot pass from
            # resurrecting an erased account. The session pin still outlives every backfill statement.
            cur.execute("SELECT core.enter_existing_installation_with_authority(%s)", (account_key,))
            row = cur.fetchone()
            lifecycle_account = row[0] if row else None
            if not lifecycle_account:
                raise RuntimeError(f"boot reconcile repo={repo}: tenant route did not resolve an account")
            # ACCOUNT-LIFECYCLE SERIALIZATION: assert_account_live is only one autocommit statement; without a
            # lock spanning the full backfill, uninstall/erase can commit after that check and before later graph /
            # claim writes. Hold the SHARED session form: current-generation live work may proceed alongside this
            # read/convergence pass, while destructive lifecycle work takes the exclusive form and waits until the
            # pass has finished (then reaps its earlier writes). Lock order remains stable-id → repo → account;
            # graph writers take their coordinate lock only after joining that account fence.
            cur.execute(
                "SELECT pg_advisory_lock_shared(hashtext('core.account_lifecycle'),hashtext(%s))",
                (lifecycle_account,),
            )
            lifecycle_lock_taken = True
        try:
            # Run the backfill over THIS locked, tenant-pinned connection (via _scoped_db) so the held lock AND the
            # tenant pin genuinely cover the writes — a webhook for the same repo arriving mid-boot blocks on the
            # repo lock instead of double-writing, and every write lands in the installation's own ACCT-GH-<owner_id>.
            scoped = _S._scoped_db(conn)
            # PAGE→WRITE GENERATION FENCE: the durable page can be read before installation A is replaced by B.
            # Installation lifecycle mutations take the exclusive account lock; we now hold its shared SESSION
            # counterpart, so an exact generation check here remains true through every external GitHub mutation
            # and DB write below. A stale A client therefore performs zero reconciliation work.
            if exact_generation:
                if str(lifecycle_account) != str(expected_account_id):
                    raise RuntimeError(
                        "boot reconcile route account changed before the lifecycle fence")
                generation_current = scoped(
                    "SELECT core.boot_reconcile_route_is_current_with_authority("
                    "%s,%s::timestamptz,%s,%s)",
                    (
                        str(expected_installation_id),
                        str(expected_installation_created_at),
                        repo,
                        str(repository_id),
                    ),
                )
                if generation_current is not True:
                    raise RuntimeError(
                        "boot reconcile installation/repository generation changed")
            # RESURRECTION GUARD (audit iter-4 P1): AFTER the per-repo lock + tenant pin, BEFORE any write, re-validate
            # the account is still LIVE. Boot-reconcile re-runs open PRs (claim writes) + re-ingests main's graph for
            # every repo, holding a PER-REPO lock. The shared account-lifecycle session fence held above serializes
            # the whole multi-statement pass against installation.deleted/GDPR's exclusive account-wide fence.
            # assert_account_live also re-checks the tombstone after those locks, so a waiter behind uninstall skips
            # THIS repo instead of resurrecting its claims + graph (caller counts it failed, content-free).
            scoped("SELECT core.assert_account_live_with_authority()")
            identity = None
            if repository_id is not None:
                # The installation inventory is current, but it still does not get rename/removal authority. Apply
                # the same strict read-only lifecycle gate as account onboarding before stamping a legacy graph.
                # A tombstone, different non-null id, or same id under another coordinate skips the background
                # replay; only an exact or NULL legacy graph converges to this stable id.
                if not _onboard_repo_identity_allowed(scoped, repo, repository_id):
                    return {"backfilled": repo, "count": 0, "results": [], "open_change_ids": [],
                            "skipped": "repository removed or identity conflict"}
                identity = _bind_onboarded_repo_identity(scoped, repo, repository_id)

            # BRANCH-CLAIM SELF-HEAL (legacy/missed delete backstop). Resolve the protected branch once, then —
            # BEFORE re-posting any open PR — converge BR-* reservations to GitHub's complete live branch set.
            # Ordering matters: backfill_open_prs renders current verdicts, so cleaning first prevents it from
            # faithfully re-posting an obsolete "wait behind BR-old" check that would survive until another PR
            # event. Every step is under this repo's existing advisory lock + tenant pin. A GitHub inventory
            # error/truncation is fail-closed and leaves all BR lanes untouched; normal PR/graph reconcile proceeds.
            default_branch = None
            default_branch_authoritative = False
            branch_claims = None
            branch_claims_error = None
            try:
                branch_name_reader = getattr(gh, "repo_default_branch_name", None)
                if not callable(branch_name_reader):
                    raise RuntimeError("authoritative default-branch metadata unavailable")
                default_branch = branch_name_reader(repo)
                default_branch_authoritative = True
            except Exception as e:
                branch_claims_error = f"default branch unavailable: {str(e)[:120]}"
                print(f"branch-claim reconcile skipped repo={repo}: {branch_claims_error}", flush=True)
            if default_branch:
                try:
                    branch_claims = _reconcile_live_branch_claims(scoped, gh, repo, default_branch)
                except Exception as e:
                    branch_claims_error = str(e)[:200]
                    print(f"branch-claim reconcile skipped repo={repo}: {str(e)[:160]}", flush=True)

            result = backfill_open_prs(scoped, gh, repo, default_branch=default_branch,
                                       repository_id=repository_id,
                                       default_branch_authoritative=default_branch_authoritative)
            if identity is not None:
                result["repository_identity"] = identity
            if branch_claims is not None:
                result["branch_claims_reconciled"] = branch_claims
            if branch_claims_error is not None:
                result["branch_claims_reconcile_error"] = branch_claims_error
            # CLAIM-RECONCILE SELF-HEAL (the dropped-'closed'/merge backstop, #3): a missed 'closed'/merge delivery
            # leaves a no-longer-open PR's lanes HELD forever — every PR behind it waits until the LEASE expires (up
            # to lease_minutes), not the documented prompt self-heal. So on each reconcile pass, after re-running the
            # live open PRs, converge the repo's claim set to the live open-PR truth: release every PR-claim at the
            # protected-branch coordinate whose change_id is NOT in the current open set, promoting each freed lane's
            # next waiter. Runs over the SAME locked, tenant-pinned connection so the release+promote is serialized
            # against live events for this repo and lands in the installation's own tenant. The reconcile fn itself
            # scopes to PR-prefixed claims, so a pre-PR 'BR-<branch>' push reservation (governed by the push→PR-open
            # lifecycle, not by the open-PR set) is never wrongly reclaimed here.
            #
            # COMPLETENESS GUARD (must-not-over-release): the open set is reconcile-by-DIFFERENCE — anything not in it
            # gets released. If backfill_open_prs hit its PR cap (truncated → there are MORE open PRs than we listed),
            # the set is INCOMPLETE and reconciling would wrongly release the in-flight lanes of genuinely-open PRs
            # beyond the cap. So SKIP the reconcile when truncated — those stale-close strandings (rare, and only on
            # a > cap-PR repo) still self-heal via the lease backstop; we never trade a dropped-close fix for freeing
            # a live PR's lane. default_branch comes from the SAME backfill (the coordinate its claims live on).
            if isinstance(result, dict) and not result.get("truncated"):
                try:
                    rec = scoped("SELECT core.reconcile_repo_claims_with_authority(%s,%s,%s)",
                                 (repo, result.get("default_branch") or "main",
                                  result.get("open_change_ids") or []))
                    if isinstance(rec, str):
                        rec = json.loads(rec)
                    result = {**result, "claims_reconciled": rec}
                except Exception as e:                      # never let the self-heal abort the (already-done) backfill
                    print(f"claim reconcile skipped repo={repo}: {str(e)[:160]}", flush=True)
                    result = {**result, "claims_reconciled_error": str(e)[:200]}
            # GRAPH CONVERGENCE ON BOOT (the idle-repo gap, audit r5): boot owns inventory discovery, not the
            # scarce extractor. Resolve the current HEAD and enqueue the same durable coordinate the live
            # push/PR paths use. This preserves idle-repo convergence while preventing the 120-second boot sweep
            # from seizing the singleton graph slot ahead of a newly-arrived customer/check canary.
            heal_branch = result.get("default_branch") if isinstance(result, dict) else None
            if heal_branch:
                try:
                    _resolved_branch, _resolved_head = gh.repo_default_branch_head(repo)
                    if not _resolved_head:
                        raise RuntimeError("boot graph convergence found no default-branch HEAD")
                    healed = request_main_graph_refresh(
                        scoped, repo, _resolved_branch or heal_branch, _resolved_head,
                        repository_id)
                    if isinstance(result, dict):
                        result = {**result, "graph_refresh": healed}
                except Exception as e:
                    print(f"boot graph enqueue skipped repo={repo}: {str(e)[:160]}", flush=True)
                    if isinstance(result, dict):
                        result = {**result, "graph_refresh_error": str(e)[:200]}
            # An open-PR replay above can self-heal BEFORE this explicit boot heal.  Its synthetic payload carries
            # repository.id but historically no owner.id, so the graph writer correctly clears repo_id and that
            # replay cannot re-stamp.  The explicit heal then sees the graph already at HEAD and no-ops.  Converge
            # identity once more at the END using boot's independently authenticated inventory id + installation
            # owner id and the strict current id/owner/full point read.  A mismatch/API failure leaves NULL.
            if repository_id is not None:
                identity_restamped = _restamp_current_repo_identity(
                    scoped, gh, repo, repository_id, account_key)
                if isinstance(result, dict):
                    result = {**result, "repository_identity_restamped": identity_restamped}
            return result
        finally:
            with conn.cursor() as cur:
                if lifecycle_lock_taken:
                    cur.execute(
                        "SELECT pg_advisory_unlock_shared(hashtext('core.account_lifecycle'),hashtext(%s))",
                        (lifecycle_account,),
                    )
                _S._release_repo_lock(cur, account_key, repo)  # release THIS repo before the next (one lock at a time → deadlock-free)
    finally:
        conn.close()   # backstop: closing the connection also drops the session advisory lock even if the explicit unlock above never ran (e.g. a dead conn)
