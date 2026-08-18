"""BOOT-TIME SCHEMA↔RUNTIME CONTRACT — fail-closed defense against schema drift.

PROBLEM THIS MODULE ELIMINATES:
  Application code can call a new function signature or column while the database still exposes the previous
  shape. Every event then fails with an undefined-function or undefined-column error even though a shallow
  process probe can remain green. The service applies the idempotent schema before promotion; this assertion is
  an independent fail-closed backstop if that step is skipped, miswired, or incomplete.

THE DEFENSE — boot-time schema↔runtime contract assertion. On boot, the App queries pg_catalog for the EXACT
functions + arities and columns its Python calls. If ANY required
function/arity is MISSING from the target DB, or any required column is missing/wrong-type, the App REFUSES to mark
itself healthy: /healthz returns 503 with a body naming the violation. The deploy health gate fails, so the
new build is not promoted. The class of "Python ahead of DB
schema" deploys becomes MECHANICALLY IMPOSSIBLE — every code change that requires a schema delta either ships
behind a successful migration, or the deploy refuses (the safe failure mode).

WHY NOT auto-derive from db/schema/*.sql: silent drift in the OTHER direction. If the contract were derived from
the SQL files, a change that updates both Python and SQL but does not apply the migration would still pass the
file-level check. Hard-coding the list HERE means
a Python change that needs a new signature MUST add it to this list — an INTENTIONAL gate that forces the author
to think about deploy ordering. (The schema_runtime_contract gate enforces that this list and the live SQL stay
in sync, so a forgotten entry is caught by ./run_gates.sh before merge.)

KILL SWITCH: VERIPSA_SCHEMA_CONTRACT=0 disables the assertion (for an emergency forward-compat hotfix where the
operator KNOWS the contract is stale and wants the deploy through anyway). Default ON.

CONTENT-FREE: this module reads pg_catalog metadata only — function names, arities, column names + types. It
never reads a row of tenant data. It is safe to run as the least-privilege veripsa_app role.
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
from typing import NamedTuple, Optional

try:
    import db_connect as _db_connect
except ImportError:  # imported as a package
    from . import db_connect as _db_connect
try:
    from env_config import env_int
except ImportError:  # imported as a package
    from .env_config import env_int


_SCHEMA_CONTRACT_TIMEOUT_SECONDS = env_int(
    "VERIPSA_SCHEMA_CONTRACT_TIMEOUT_SECONDS",
    15,
    min_value=1,
    max_value=60,
)
_SCHEMA_CONTRACT_CONNECT_TIMEOUT_SECONDS = env_int(
    "VERIPSA_SCHEMA_CONTRACT_CONNECT_TIMEOUT_SECONDS",
    10,
    min_value=1,
    max_value=30,
)
_SCHEMA_CONTRACT_STATEMENT_TIMEOUT_MS = env_int(
    "VERIPSA_SCHEMA_CONTRACT_STATEMENT_TIMEOUT_MS",
    5000,
    min_value=100,
    max_value=60000,
)
_SCHEMA_CONTRACT_LOCK_TIMEOUT_MS = env_int(
    "VERIPSA_SCHEMA_CONTRACT_LOCK_TIMEOUT_MS",
    1000,
    min_value=100,
    max_value=10000,
)

# THE CONTRACT — every (schema, function-name, arity) the runtime Python calls in pg_proc. Adding a Python call
# that uses a new signature → ADD a row HERE. The gate guarantees the live SQL agrees with this list, so a missing
# entry fails ./run_gates.sh; a missing FUNCTION on the live DB fails the boot health-check (Render rejects the
# deploy). pronamespace is resolved via 'core'::regnamespace at query time, so we only carry the bare name + arity.
#
# The current set covers the known drift fixture, the surrounding always-called wrappers, and
# runtime-owned ops/readiness functions that the server, watchdog, retention jobs, and recovery tools call
# directly. If these drift, the app can otherwise boot green and fail later on /freshz, /readyz, or background
# maintenance.
#   * act_for_claim_with_authority(9 args) — the per-PR-event gate call.
#   * declare_claim_with_authority(7 args) — the buyer's own writer path (act_for=False trial; tests).
#   * _set_claim_is_draft(7 args)         — internal helper called by both wrappers above. Listed for
#                                           defense in depth (if it were missing, both wrappers would 42883 on every call).
#   * set_change_head_sha_with_authority(4 args) — binds reconciled PR claims to their analyzed commit.
# Future additions: when a Python change needs a NEW signature on the DB, add it here in the SAME commit. The
# gate (gates.d/197-schema_runtime_contract.gate) confirms the live SQL exposes every listed (name, arity).
_EXPECTED_FUNCTIONS: tuple[tuple[str, int], ...] = (
    # cg4 graph-integrity publication unit. These are the full/patch writers and read contracts used by normal
    # push handling; boot must refuse if Python can reach an older DB that cannot enforce the current graph.
    ("graph_schema_inventory", 0),
    ("current_extractor_version", 0),
    ("current_semantic_ref_version", 0),
    ("_assert_unique_graph_identities", 2),
    ("ingest_graph_with_authority", 5),
    ("patch_graph_with_authority", 7),
    # Background convergence releases PostgreSQL before clone/extract, then finalizes through an exact stable
    # repository-id + lifecycle-activation CAS in the graph writer's own short transaction.
    ("capture_repository_graph_generation_with_authority", 2),
    ("ingest_graph_with_authority_for_repository_generation", 7),
    ("patch_graph_with_authority_for_repository_generation", 9),
    ("coordinate_file_paths", 2),
    ("coordinate_resource_catalog", 2),
    ("coordinate_graph_sha", 2),
    ("coordinate_inert_imports", 2),
    ("act_for_claim_with_authority", 9),
    ("declare_claim_with_authority", 7),
    ("_set_claim_is_draft", 7),
    ("set_change_head_sha_with_authority", 4),
    # issue #851: the branch-reservation real-push signal. reserve_branch_lanes calls this once per feature-branch
    # push to record the branch head and advance core.claim.last_real_push_at only on a genuine head change, so the
    # stale-reservation decay keys off a signal an idempotent re-declaration does NOT refresh.
    ("note_branch_push_head_with_authority", 4),
    ("reconcile_repo_claims_with_authority", 3),
    ("reconcile_repo_branch_claims_with_authority", 3),
    ("enqueue_webhook_delivery_with_authority", 7),
    ("pending_webhook_deliveries_with_authority", 3),
    # One exact durable/live repository-lane classifier: malformed or missing repository ids and
    # account-scoped event types become conservative account-wide barriers in both schedulers.
    ("_webhook_delivery_repository_lane", 2),
    ("claim_webhook_delivery_with_authority", 6),
    # Rolling /5 ABI remains while older atomic-owner workers coexist.
    ("claim_webhook_delivery_with_authority", 5),
    # Rolling /4 ABI remains boot-gated while an older worker can coexist with the new atomic-owner runtime.
    ("claim_webhook_delivery_with_authority", 4),
    ("recover_ambiguous_webhook_claim_with_authority", 4),
    ("beat_webhook_worker_instance_with_authority", 1),
    ("stamp_webhook_owner_instance_with_authority", 3),
    ("reap_dead_instance_leases_with_authority", 3),
    ("resolve_webhook_delivery_release_with_authority", 4),
    ("resolve_webhook_delivery_defer_with_authority", 4),
    ("resolve_webhook_delivery_commit_with_authority", 4),
    ("_canonical_webhook_delivery_fanout_plan", 1),
    ("_webhook_delivery_fanout_completion_valid", 2),
    ("prepare_webhook_delivery_fanout_with_authority", 3),
    ("complete_webhook_delivery_fanout_repository_with_authority", 3),
    ("resolve_webhook_delivery_fanout_defer_with_authority", 4),
    ("finish_webhook_delivery_with_authority", 2),
    ("release_webhook_delivery_with_authority", 4),
    ("expire_webhook_delivery_lease_with_authority", 4),
    # issue #847: aged-deferred lane escalation — the recovery loop re-arms an aged 'failed' lane head that is
    # blocking aged queued work, so a 'blocked_by_earlier' deferral cannot sit unreplayed behind a dead head.
    ("rearm_failed_webhook_deliveries_with_authority", 3),
    ("rearm_failed_webhook_deliveries_with_authority", 2),
    ("recover_terminal_webhook_deliveries_with_authority", 2),
    ("continue_terminal_webhook_deliveries_with_authority", 3),
    ("claim_operator_github_redelivery_with_authority", 4),
    ("record_operator_github_redelivery_outcome_with_authority", 4),
    ("operator_github_redelivery_audit_with_authority", 4),
    ("terminal_webhook_delivery_recovery_status_with_authority", 1),
    ("escalate_blocked_webhook_deliveries_with_authority", 3),
    ("escalate_blocked_webhook_deliveries_with_authority", 2),
    ("webhook_delivery_depth_with_authority", 0),
    ("list_installation_ids", 0),
    ("db_usage_surface", 2),
    ("owner_cost_surface", 1),
    ("owner_graph_freshness_surface", 1),
    ("advance_graph_freshness_cursor_with_authority", 4),
    ("raise_active_alert_with_authority", 4),
    ("clear_active_alert_with_authority", 1),
    ("active_alerts_with_authority", 1),
    ("read_boot_reconcile_last_run_with_authority", 0),
    ("mark_boot_reconcile_run_with_authority", 1),
    ("read_boot_reconcile_route_page_with_authority", 1),
    ("boot_reconcile_route_is_current_with_authority", 4),
    ("advance_boot_reconcile_route_cursor_with_authority", 4),
    ("prune_all_accounts_with_authority", 4),
    # compat lane PR-2: the 'compat_finding' event-kind writer. INERT (no production caller yet — the
    # analysis wiring lands behind VERIPSA_COMPAT_ANALYSIS in a later PR), but listed NOW so the deploy
    # that ships the wiring can never run Python ahead of a DB missing the function (the exact
    # schema-drift class), and so the retention sweep's 'compat_finding' kind has its writer asserted.
    ("record_compat_finding_with_authority", 7),
    # corrective lane S3a: the 9-arg overload stamping the explicit classification (fact_class) + the
    # detector identity/version (detector) — the signature the live shadow analysis calls. The 7-arg
    # signature above stays as the delegating legacy wrapper (NULL class/detector), so BOTH arities are
    # asserted (the reactivate_account / offboard_repository two-arity precedent).
    ("record_compat_finding_with_authority", 9),
    ("export_durable_rows_with_authority", 1),
    ("repository_event_allowed_with_authority", 2),
    ("repository_account_onboarding_allowed_with_authority", 2),
    ("enter_existing_installation_with_authority", 1),
    ("reactivate_account_with_authority", 1),
    ("reactivate_account_with_authority", 2),
    ("admit_event_installation_generation_with_authority", 2),
    ("reactivate_repository_with_authority", 3),
    ("offboard_repository_with_authority", 3),
    ("offboard_repository_with_authority", 4),
    ("_defer_webhook_delivery_with_authority", 3),
    ("defer_webhook_delivery_with_authority", 4),
    ("purge_account_working_set_with_authority", 1),
    ("release_account_claims_with_authority", 1),
    ("release_account_claims_with_authority", 2),
    ("prepare_legacy_repository_offboard_with_authority", 3),
    ("confirm_absent_legacy_repository_offboard_with_authority", 3),
    ("resolve_legacy_repository_offboard_with_authority", 4),
    ("transfer_repo_coordinate_with_authority", 8),
    ("begin_github_delivery_recovery_scan_with_authority", 0),
    ("observe_github_delivery_with_authority", 6),
    ("advance_github_delivery_recovery_scan_with_authority", 8),
    ("reset_github_delivery_recovery_scan_with_authority", 2),
    ("claim_github_delivery_redelivery_with_authority", 0),
    ("record_github_delivery_redelivery_attempt_with_authority", 4),
    ("prune_github_delivery_recovery_with_authority", 0),
    ("rearm_exhausted_github_delivery_recovery_with_authority", 2),
    ("github_delivery_recovery_depth_with_authority", 0),
    # G4 policy-change refresh (97_policy_refresh.sql). The policy writers enqueue via _enqueue_policy_refresh in
    # the SAME txn as the policy commit. Generation 21 extends that outbox into the account-convergence scheduler:
    # live webhooks enqueue an exact stable-repository-id/SHA graph fact, while a dedicated background process
    # claims one globally-fair account turn and completes it through exact epoch CAS. Keep both the new capability
    # ABI and the policy-only rolling ABI asserted: schema-first deploys can coexist with the predecessor image,
    # but an incomplete schema must never boot either process green and then 42883 on its first turn.
    ("_next_account_convergence_epoch", 1),
    ("_enqueue_policy_refresh", 1),
    ("_lock_live_graph_refresh_coordinate_with_authority", 2),
    ("_rename_graph_refresh_coordinate", 3),
    ("enqueue_graph_refresh_with_authority", 4),
    ("enqueue_repository_onboarding_with_authority", 2),
    ("wake_graph_refresh_candidate_with_authority", 4),
    ("wake_graph_refresh_candidate_state_with_authority", 4),
    ("_graph_refresh_fulfilled", 5),
    ("graph_onboarding_snapshot_with_authority", 4),
    ("_sync_account_convergence_due", 4),
    ("_sync_graph_claim_router", 2),
    ("claim_policy_refresh_with_authority", 7),
    ("claim_policy_refresh_with_authority", 6),
    ("claim_policy_refresh_with_authority", 5),
    ("claim_policy_refresh_with_authority", 4),
    ("policy_refresh_install_generation_current_with_authority", 3),
    ("policy_refresh_turn_is_current_with_authority", 5),
    ("policy_refresh_turn_is_current_with_authority", 7),
    ("policy_refresh_external_write_fence_with_authority", 6),
    ("policy_refresh_external_write_fence_with_authority", 8),
    ("ingest_graph_with_authority_for_convergence_lease", 10),
    ("patch_graph_with_authority_for_convergence_lease", 12),
    ("finish_policy_refresh_turn_with_authority", 5),
    ("finish_policy_refresh_turn_with_authority", 7),
    ("fail_policy_refresh_turn_with_authority", 7),
    ("fail_policy_refresh_turn_with_authority", 6),
    ("fail_policy_refresh_turn_with_authority", 9),
    ("defer_graph_refresh_turn_with_authority", 6),
    ("defer_graph_refresh_turn_with_authority", 8),
    ("release_superseded_convergence_lease_with_authority", 2),
    ("release_superseded_convergence_lease_with_authority", 4),
    ("requeue_graph_refresh_page_with_authority", 7),
    ("resolve_graph_onboarding_head_with_authority", 8),
    ("resolve_graph_onboarding_head_with_authority", 10),
    ("requeue_graph_onboarding_with_authority", 12),
    ("requeue_graph_onboarding_with_authority", 14),
    ("requeue_policy_refresh_slice_with_authority", 4),
    ("requeue_policy_refresh_page_with_authority", 6),
    ("account_convergence_depth_with_authority", 0),
    ("_materialize_legacy_policy_refresh_lease", 2),
    ("finish_policy_refresh_with_authority", 2),
    ("fail_policy_refresh_with_authority", 3),
    ("account_inflight_refresh_coordinates_with_authority", 1),
    ("account_inflight_refresh_coordinates_after_with_authority", 3),
)

# These functions are called directly by the live durable-delivery runtime.
# Presence alone is not enough: a missed GRANT recreates the same green-boot /
# every-event-42501 failure class as a missing function. Internal helpers in
# _EXPECTED_FUNCTIONS are deliberately excluded because SECURITY DEFINER
# wrappers, not veripsa_app, execute them.
_EXPECTED_EXECUTABLE_FUNCTIONS: tuple[tuple[str, int], ...] = (
    ("enqueue_webhook_delivery_with_authority", 7),
    ("pending_webhook_deliveries_with_authority", 3),
    ("claim_webhook_delivery_with_authority", 6),
    ("recover_ambiguous_webhook_claim_with_authority", 4),
    ("beat_webhook_worker_instance_with_authority", 1),
    ("stamp_webhook_owner_instance_with_authority", 3),
    ("reap_dead_instance_leases_with_authority", 3),
    ("resolve_webhook_delivery_release_with_authority", 4),
    ("resolve_webhook_delivery_defer_with_authority", 4),
    ("resolve_webhook_delivery_commit_with_authority", 4),
    ("prepare_webhook_delivery_fanout_with_authority", 3),
    ("complete_webhook_delivery_fanout_repository_with_authority", 3),
    ("resolve_webhook_delivery_fanout_defer_with_authority", 4),
    ("finish_webhook_delivery_with_authority", 2),
    ("expire_webhook_delivery_lease_with_authority", 4),
    ("webhook_delivery_depth_with_authority", 0),
    ("rearm_failed_webhook_deliveries_with_authority", 3),
    ("recover_terminal_webhook_deliveries_with_authority", 2),
    ("continue_terminal_webhook_deliveries_with_authority", 3),
    ("claim_operator_github_redelivery_with_authority", 4),
    ("record_operator_github_redelivery_outcome_with_authority", 4),
    ("operator_github_redelivery_audit_with_authority", 4),
    ("terminal_webhook_delivery_recovery_status_with_authority", 1),
    ("escalate_blocked_webhook_deliveries_with_authority", 3),
    # Fleet freshness is a durable, bounded account-keyset rotation. Presence and direct App EXECUTE are both
    # required: otherwise a release can boot green but leave every page pinned forever at the first accounts.
    ("owner_graph_freshness_surface", 1),
    ("advance_graph_freshness_cursor_with_authority", 4),
    # Worker-only boot reconciliation walks durable DB lifecycle routes instead of treating the bounded
    # App-installation inventory (or the first repository page) as a complete fleet enumerator.
    ("read_boot_reconcile_route_page_with_authority", 1),
    ("boot_reconcile_route_is_current_with_authority", 4),
    ("advance_boot_reconcile_route_cursor_with_authority", 4),
    # Account-convergence foreground worker. These are called directly as veripsa_app; checking pg_proc presence
    # without EXECUTE would recreate the same green-boot/every-turn-42501 incident class.
    ("capture_repository_graph_generation_with_authority", 2),
    ("ingest_graph_with_authority_for_repository_generation", 7),
    ("patch_graph_with_authority_for_repository_generation", 9),
    ("enqueue_graph_refresh_with_authority", 4),
    ("enqueue_repository_onboarding_with_authority", 2),
    ("wake_graph_refresh_candidate_with_authority", 4),
    ("wake_graph_refresh_candidate_state_with_authority", 4),
    ("graph_onboarding_snapshot_with_authority", 4),
    ("claim_policy_refresh_with_authority", 7),
    ("claim_policy_refresh_with_authority", 6),
    ("claim_policy_refresh_with_authority", 5),
    ("claim_policy_refresh_with_authority", 4),
    ("policy_refresh_install_generation_current_with_authority", 3),
    ("policy_refresh_turn_is_current_with_authority", 5),
    ("policy_refresh_turn_is_current_with_authority", 7),
    ("policy_refresh_external_write_fence_with_authority", 6),
    ("policy_refresh_external_write_fence_with_authority", 8),
    ("ingest_graph_with_authority_for_convergence_lease", 10),
    ("patch_graph_with_authority_for_convergence_lease", 12),
    ("finish_policy_refresh_turn_with_authority", 5),
    ("finish_policy_refresh_turn_with_authority", 7),
    ("fail_policy_refresh_turn_with_authority", 7),
    ("fail_policy_refresh_turn_with_authority", 6),
    ("fail_policy_refresh_turn_with_authority", 9),
    ("defer_graph_refresh_turn_with_authority", 6),
    ("defer_graph_refresh_turn_with_authority", 8),
    ("release_superseded_convergence_lease_with_authority", 2),
    ("release_superseded_convergence_lease_with_authority", 4),
    ("requeue_graph_refresh_page_with_authority", 7),
    ("resolve_graph_onboarding_head_with_authority", 8),
    ("resolve_graph_onboarding_head_with_authority", 10),
    ("requeue_graph_onboarding_with_authority", 12),
    ("requeue_graph_onboarding_with_authority", 14),
    ("requeue_policy_refresh_slice_with_authority", 4),
    ("requeue_policy_refresh_page_with_authority", 6),
    ("account_convergence_depth_with_authority", 0),
    ("finish_policy_refresh_with_authority", 2),
    ("fail_policy_refresh_with_authority", 3),
    ("account_inflight_refresh_coordinates_with_authority", 1),
    ("account_inflight_refresh_coordinates_after_with_authority", 3),
)

# THE COLUMN CONTRACT — every (schema, table, column, expected pg_type typname) the runtime relies on. A missing
# claim.is_draft would silently fail PR handling; a missing webhook_delivery.not_before would break the durable
# lifecycle consistency scheduler; missing activation provenance could let ordinary work defeat offboarding order.
# Listed here so the boot check catches any mismatch before accepting webhooks.
# typname is the canonical pg_type lowercase name — 'bool' for boolean, 'text' for text, 'int4' for
# integer, etc. We resolve via pg_catalog (pg_attribute + pg_type), not information_schema: information_schema
# only shows columns the CURRENT ROLE has SELECT on, and the live veripsa_app role has no direct table SELECT
# (security boundary — every table read goes through a SECURITY DEFINER gate). pg_catalog is publicly readable,
# so the boot-time check sees the schema regardless of the role's table grants.
_EXPECTED_COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    ("core", "claim", "is_draft", "bool"),
    ("core", "claim", "analyzed_head_sha", "text"),
    # issue #851: the branch-reservation real-push signal the stale-reservation decay keys off (advanced only on a
    # genuine head change, never an idempotent re-declaration that re-stamps heartbeat_at). A missing column would
    # make note_branch_push_head_with_authority 42703 on its first call — refused at boot (the silent-break class).
    ("core", "claim", "last_real_push_at", "timestamptz"),
    ("core", "webhook_delivery", "not_before", "timestamptz"),
    ("core", "webhook_delivery", "lease_generation", "int8"),
    ("core", "webhook_delivery", "causal_order_version", "int2"),
    ("core", "webhook_delivery", "owner_instance", "text"),
    ("core", "webhook_delivery", "retry_window_expires_at", "timestamptz"),
    ("core", "webhook_delivery", "auto_rearm_count", "int2"),
    ("core", "webhook_delivery", "operator_recovery_id", "text"),
    ("core", "webhook_delivery", "operator_recovered_at", "timestamptz"),
    ("core", "webhook_delivery", "operator_recovery_batch_size", "int2"),
    ("core", "webhook_delivery", "operator_recovery_batch_token", "uuid"),
    ("core", "webhook_delivery", "operator_recovery_count", "int2"),
    ("core", "webhook_delivery", "operator_continuation_id", "text"),
    ("core", "webhook_delivery", "operator_continued_at", "timestamptz"),
    ("core", "webhook_delivery", "operator_continuation_sha", "text"),
    ("core", "webhook_delivery", "operator_continuation_count", "int2"),
    ("core", "webhook_delivery", "operator_github_redelivery_delivery_id", "int8"),
    ("core", "webhook_delivery", "operator_github_redelivery_sha", "text"),
    ("core", "webhook_delivery", "operator_github_redelivery_spent_at", "timestamptz"),
    ("core", "webhook_delivery", "operator_github_redelivery_outcome", "text"),
    ("core", "webhook_delivery", "operator_github_redelivery_count", "int2"),
    ("core", "repository_lifecycle_activation", "lifecycle_authoritative", "bool"),
    ("core", "repository_lifecycle_activation", "generation_started_at", "timestamptz"),
    ("core", "repository_lifecycle_tombstone", "generation_started_at", "timestamptz"),
    ("core", "account_lifecycle_tombstone", "active", "bool"),
    ("core", "account_lifecycle_tombstone", "last_event_received_at", "timestamptz"),
    ("core", "account_lifecycle_tombstone", "last_delivery_key", "text"),
    ("core", "account_lifecycle_tombstone", "blocked_installation_id", "text"),
    ("core", "installation_account", "github_installation_id", "text"),
    ("core", "installation_account", "github_installation_created_at", "timestamptz"),
    # Generation-21 global account scheduler. These content-free router fields replace an O(N-account) scan with
    # one indexed due-account claim and carry the account-wide lease generation that fences late workers.
    ("core", "installation_account", "policy_refresh_due_at", "timestamptz"),
    ("core", "installation_account", "graph_refresh_due_at", "timestamptz"),
    ("core", "installation_account", "legacy_graph_refresh_due_at", "timestamptz"),
    ("core", "installation_account", "convergence_claimed_until", "timestamptz"),
    ("core", "installation_account", "convergence_claimed_by", "text"),
    ("core", "installation_account", "convergence_claim_epoch", "int8"),
    ("core", "installation_account", "convergence_graph_claim_count", "int4"),
    ("core", "installation_account", "convergence_graph_reclaim_at", "timestamptz"),
    ("core", "installation_account", "convergence_pending_count", "int4"),
    ("core", "installation_account", "convergence_retry_exhausted_count", "int4"),
    ("core", "installation_account", "convergence_quota_deferred_count", "int4"),
    ("core", "installation_account", "convergence_stall_started_at", "timestamptz"),
    ("core", "policy_refresh_outbox", "surface_dirty", "bool"),
    ("core", "installation_account", "convergence_schema_version", "int4"),
    ("core", "installation_account", "convergence_next_epoch", "int8"),
    ("core", "co_change", "generation_observed_at", "timestamptz"),
    ("core", "co_change_seen_commit", "generation_observed_at", "timestamptz"),
    # code_node COMPATIBILITY SIGNATURE SHAPE (content-free: names + counts + flags + a hash fingerprint —
    # never defaults/annotations/bodies). The governed ingest INSERTs (ingest_graph / patch_graph) reference
    # these columns, so a deploy whose DB never got the schema applied would 42703 on every graph ingest —
    # exactly the schema-drift class this contract exists to refuse at boot. Nothing READS them yet
    # (PR-1 of the compatibility lane persists only). '_text' is pg_type's typname for text[].
    ("core", "code_node", "required_arity", "int4"),
    ("core", "code_node", "optional_arity", "int4"),
    ("core", "code_node", "has_varargs", "bool"),
    ("core", "code_node", "has_kwargs", "bool"),
    ("core", "code_node", "param_names", "_text"),
    ("core", "code_node", "kwonly_names", "_text"),
    ("core", "code_node", "shape_fingerprint", "text"),
    # cg3 first-class resource identity + semantic-reference v1, extended by
    # cg4's closed uncertainty fields. Nullable columns preserve legacy rows;
    # governed full/patch writes populate bounded evidence on new coordinates.
    ("core", "code_node", "canonical_key", "text"),
    ("core", "code_node", "resource_scope", "text"),
    ("core", "code_node", "extractor", "text"),
    ("core", "code_node", "confidence", "float8"),
    ("core", "code_node", "provenance", "jsonb"),
    ("core", "code_node", "semantic_key", "text"),
    ("core", "code_node", "analysis_status", "text"),
    ("core", "code_edge", "semantic_dst_key", "text"),
    ("core", "code_edge", "reference_status", "text"),
    # event COMPAT-FINDING extensions (compat lane PR-2, content-free: two head SHAs + a fingerprint —
    # never code). Typed nullable columns on the one event ledger (the `model` precedent); written only
    # by record_compat_finding_with_authority (inert until the analysis wiring lands). A deploy whose DB
    # never got the schema applied would 42703 inside that gate fn on its first call — refused at boot
    # instead (the schema-drift class this contract exists for).
    ("core", "event", "counterparty_sha", "text"),
    ("core", "event", "fact_fingerprint", "text"),
    # event COMPAT TAXONOMY + DETECTOR STAMP (corrective lane S3a, content-free: a bounded classification
    # code + a bounded detector identity/version token — never code). Written by the 9-arg
    # record_compat_finding_with_authority; read by owner_compat_shadow_surface's per-class split. A deploy
    # whose DB never got the schema applied would 42703 inside the gate fn — refused at boot instead.
    ("core", "event", "fact_class", "text"),
    ("core", "event", "detector", "text"),
    # graph_version EXTRACTOR VERSION STAMP (G3, content-free: a bounded extraction-logic version token — never
    # bodies/counts). ingest_graph_with_authority / patch_graph_with_authority INSERT this column and
    # coordinate_graph_sha reads it back, so a deploy whose DB never got the schema applied would 42703 on every
    # graph ingest / freshness read — exactly the schema-drift class this contract refuses at boot. The
    # freshness read (graph_freshness) additionally tolerates the column's ABSENCE defensively (gen-agnostic), but
    # the contract still requires it so the predeploy-apply → deploy ordering is mechanically enforced.
    ("core", "graph_version", "extractor_version", "text"),
    ("core", "graph_version", "graph_hash", "text"),
    ("core", "graph_version", "observability", "jsonb"),
    ("core", "graph_version", "graph_revision", "int8"),
    ("core", "graph_version", "semantic_ref_version", "int2"),
    ("core", "github_delivery_recovery_scan", "scan_epoch", "int8"),
    ("core", "github_delivery_recovery_scan", "cursor", "text"),
    ("core", "github_delivery_recovery_scan", "page_tail_delivery_id", "int8"),
    ("core", "github_delivery_recovery_scan", "page_tail_delivered_at", "timestamptz"),
    ("core", "github_delivery_recovery_scan", "archived_unresolved_count", "int8"),
    ("core", "github_delivery_recovery_scan", "archived_terminal_count", "int8"),
    ("core", "github_delivery_recovery_scan", "archived_exhausted_count", "int8"),
    ("core", "github_delivery_recovery_scan", "redelivery_not_before", "timestamptz"),
    ("core", "github_delivery_recovery", "delivery_guid", "text"),
    ("core", "github_delivery_recovery", "latest_delivery_id", "int8"),
    ("core", "github_delivery_recovery", "attempt_generation", "int8"),
    ("core", "github_delivery_recovery", "window_expires_at", "timestamptz"),
    # G4 policy-change refresh outbox (97_policy_refresh.sql, content-free: account id + a monotonic epoch +
    # status timestamps + a bounded error code — never a repo/path/PR body). The enqueue path (inside every
    # hooked policy setter) and the drainer's claim/finish/fail all reference these columns, so a deploy whose DB
    # never got the schema applied would 42703 on the first policy write / drain tick — refused at boot instead.
    ("core", "policy_refresh_outbox", "account_id", "text"),
    ("core", "policy_refresh_outbox", "request_kind", "text"),
    ("core", "policy_refresh_outbox", "repository_id", "text"),
    ("core", "policy_refresh_outbox", "repo", "text"),
    ("core", "policy_refresh_outbox", "branch", "text"),
    ("core", "policy_refresh_outbox", "target_sha", "text"),
    ("core", "policy_refresh_outbox", "not_before", "timestamptz"),
    ("core", "policy_refresh_outbox", "terminal_reason", "text"),
    ("core", "policy_refresh_outbox", "policy_cursor_repo", "text"),
    ("core", "policy_refresh_outbox", "policy_cursor_branch", "text"),
    ("core", "policy_refresh_outbox", "change_cursor", "text"),
    ("core", "policy_refresh_outbox", "onboarding_pending", "bool"),
    ("core", "policy_refresh_outbox", "onboarding_head_pending", "bool"),
    ("core", "policy_refresh_outbox", "onboarding_plan", "_int8"),
    ("core", "policy_refresh_outbox", "onboarding_index", "int4"),
    ("core", "policy_refresh_outbox", "onboarding_truncated", "bool"),
    ("core", "policy_refresh_outbox", "onboarding_watching_done", "bool"),
    ("core", "policy_refresh_outbox", "policy_epoch", "int8"),
    ("core", "policy_refresh_outbox", "enqueued_at", "timestamptz"),
    ("core", "policy_refresh_outbox", "claimed_at", "timestamptz"),
    ("core", "policy_refresh_outbox", "claimed_by", "text"),
    ("core", "policy_refresh_outbox", "done_at", "timestamptz"),
    ("core", "policy_refresh_outbox", "attempts", "int4"),
    ("core", "policy_refresh_outbox", "last_error", "text"),
    ("core", "graph_convergence_lease", "account_id", "text"),
    ("core", "graph_convergence_lease", "slot", "int2"),
    ("core", "graph_convergence_lease", "lease_epoch", "int8"),
    ("core", "graph_convergence_lease", "request_epoch", "int8"),
    ("core", "graph_convergence_lease", "repository_id", "text"),
    ("core", "graph_convergence_lease", "repo", "text"),
    ("core", "graph_convergence_lease", "branch", "text"),
    ("core", "graph_convergence_lease", "target_sha", "text"),
    ("core", "graph_convergence_lease", "claimed_by", "text"),
    ("core", "graph_convergence_lease", "claimed_until", "timestamptz"),
)

# Critical non-table relations used inside governed SQL writers. If this
# sequence is missing, a boot can look healthy until the first graph write
# reaches nextval(); assert it before accepting webhooks.
_EXPECTED_RELATIONS: tuple[tuple[str, str, str], ...] = (
    ("core", "graph_revision_seq", "S"),
)

# Equality reads use the legacy-compatible COALESCE expression. A plain index
# on only the nullable stored digest is not usable for that expression.
_EXPECTED_INDEXES: tuple[tuple[str, str, str, str], ...] = (
    (
        "core", "code_node", "code_node_coord_kind_effective_semantic",
        "account_id,repo,branch,node_kind,"
        "COALESCE(semantic_key,core._node_semantic_key("
        "node_kind,node_id,path,name,canonical_key))",
    ),
    (
        "core", "code_edge",
        "code_edge_coord_kind_effective_semantic_dst",
        "account_id,repo,branch,edge_kind,"
        "COALESCE(semantic_dst_key,core._semantic_ref_key(dst))",
    ),
    (
        "core", "code_node", "code_node_coord_uncertain",
        "account_id,repo,branch WHERE analysis_status IS NOT NULL",
    ),
    (
        "core", "code_edge", "code_edge_coord_uncertain",
        "account_id,repo,branch WHERE reference_status IS NOT NULL",
    ),
    (
        "core", "webhook_delivery", "webhook_delivery_failed_auto_rearm",
        "received_at,delivery_key WHERE status='failed' "
        "AND auto_rearm_count<1 "
        "AND (causal_order_version>=1 OR event_type='ping')",
    ),
    (
        "core", "webhook_delivery",
        "webhook_delivery_operator_recovery_id_v1",
        "operator_recovery_id,delivery_key "
        "WHERE operator_recovery_id IS NOT NULL",
    ),
    (
        "core", "webhook_delivery",
        "webhook_delivery_operator_batch_token_v1",
        "operator_recovery_batch_token "
        "WHERE operator_recovery_batch_token IS NOT NULL",
    ),
    (
        # PRIMARY KEY USING INDEX renames the concurrently-built expansion index to the constraint name.
        "core", "policy_refresh_outbox", "policy_refresh_outbox_identity_uq",
        "account_id,request_kind,repository_id",
    ),
    (
        "core", "installation_account", "policy_refresh_account_policy_due",
        "policy_refresh_due_at,account_id WHERE revoked_at IS NULL "
        "AND policy_refresh_due_at IS NOT NULL",
    ),
    (
        "core", "installation_account", "policy_refresh_account_graph_due",
        "graph_refresh_due_at,account_id WHERE revoked_at IS NULL "
        "AND graph_refresh_due_at IS NOT NULL",
    ),
    (
        "core", "installation_account", "policy_refresh_account_legacy_graph_due",
        "legacy_graph_refresh_due_at,account_id WHERE revoked_at IS NULL "
        "AND legacy_graph_refresh_due_at IS NOT NULL",
    ),
    (
        "core", "installation_account", "policy_refresh_account_route",
        "account_id INCLUDE (installation_id)",
    ),
    (
        "core", "installation_account", "policy_refresh_account_convergence_due",
        "LEAST(policy_refresh_due_at,graph_refresh_due_at),account_id "
        "WHERE revoked_at IS NULL "
        "AND (policy_refresh_due_at IS NOT NULL OR graph_refresh_due_at IS NOT NULL)",
    ),
    (
        "core", "installation_account", "policy_refresh_account_claimed",
        "convergence_claimed_until,account_id WHERE revoked_at IS NULL "
        "AND convergence_claimed_until IS NOT NULL",
    ),
    (
        "core", "installation_account", "policy_refresh_account_graph_claimed",
        "account_id INCLUDE (convergence_graph_claim_count) WHERE revoked_at IS NULL "
        "AND convergence_graph_claim_count>0",
    ),
    (
        "core", "installation_account", "policy_refresh_account_exception_depth",
        "account_id INCLUDE (convergence_retry_exhausted_count,"
        "convergence_quota_deferred_count) WHERE revoked_at IS NULL "
        "AND (convergence_retry_exhausted_count>0 OR convergence_quota_deferred_count>0)",
    ),
    (
        "core", "installation_account", "policy_refresh_account_stall_started",
        "convergence_stall_started_at,account_id WHERE revoked_at IS NULL "
        "AND convergence_stall_started_at IS NOT NULL",
    ),
    (
        "core", "installation_account", "policy_refresh_account_stall_missing",
        "account_id WHERE revoked_at IS NULL "
        "AND convergence_stall_started_at IS NULL "
        "AND (convergence_pending_count>0 OR convergence_retry_exhausted_count>0)",
    ),
    (
        "core", "installation_account", "policy_refresh_account_schema_bridge",
        "account_id WHERE convergence_schema_version<1",
    ),
    (
        "core", "installation_account", "policy_refresh_account_schema_bridge_v2",
        "account_id WHERE convergence_schema_version<2",
    ),
    (
        "core", "policy_refresh_outbox", "policy_refresh_outbox_unfinished_due",
        "account_id,request_kind,COALESCE(not_before,enqueued_at),repository_id "
        "WHERE done_at IS NULL",
    ),
    (
        "core", "policy_refresh_outbox", "policy_refresh_outbox_claimable_due",
        "account_id,request_kind,COALESCE(not_before,enqueued_at),repository_id "
        "WHERE done_at IS NULL AND attempts<5",
    ),
    (
        "core", "policy_refresh_outbox", "policy_refresh_outbox_slow_retry_due",
        "account_id,request_kind,COALESCE(not_before,enqueued_at),repository_id "
        "WHERE done_at IS NULL AND attempts>=5",
    ),
    (
        "core", "policy_refresh_outbox", "policy_refresh_outbox_unfinished_enqueued",
        "account_id,enqueued_at,request_kind,repository_id "
        "WHERE done_at IS NULL AND terminal_reason IS NULL",
    ),
    (
        "core", "graph_convergence_lease", "graph_convergence_lease_pkey",
        "account_id,slot",
    ),
    (
        "core", "graph_convergence_lease", "graph_convergence_lease_repo_uq",
        "account_id,repository_id",
    ),
)

# Exact cg3+ kind walls. Checking columns alone is insufficient: a live DB can have every new metadata column yet
# retain the old CHECK and silently reject/drop new extractor kinds at the ingest filter. The boot check parses
# the two named CHECK definitions and requires exact set equality (missing OR unexpected kinds fail closed).
_EXPECTED_KIND_CONSTRAINTS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("core", "code_node", "code_node_kind_check", (
        "file", "def", "class", "table", "column", "config_file", "config_key",
        "iac_resource", "k8s_resource", "api_type", "api_message", "api_service", "api_operation", "api_schema",
        "ci_script", "app_command", "job_task", "job_queue", "sibling_stem", "role_feature",
    )),
    ("core", "code_edge", "code_edge_kind_check", (
        "contains", "calls", "imports", "queries", "alters", "reads_config", "alters_col", "queries_col",
    )),
)

_EXPECTED_CHECK_CONSTRAINTS: tuple[
    tuple[str, str, str, str], ...
] = (
    (
        "core", "code_node", "code_node_semantic_key_shape",
        "CHECK (semantic_key IS NULL OR "
        "(length(semantic_key)=64 AND semantic_key ~ '^[0-9a-f]{64}$'))",
    ),
    (
        "core", "code_edge", "code_edge_semantic_dst_key_shape",
        "CHECK (semantic_dst_key IS NULL OR "
        "(length(semantic_dst_key)=64 AND "
        "semantic_dst_key ~ '^[0-9a-f]{64}$'))",
    ),
    (
        "core", "code_node", "code_node_analysis_status_check",
        "CHECK (analysis_status IS NULL OR "
        "(node_kind = ANY (ARRAY['file','config_file'])) AND "
        "(analysis_status = ANY "
        "(ARRAY['failed','ambiguous','incomplete'])))",
    ),
    (
        "core", "code_edge", "code_edge_reference_status_check",
        "CHECK (reference_status IS NULL OR "
        "(reference_status = ANY (ARRAY['ambiguous','unresolved'])))",
    ),
    (
        "core", "graph_version", "graph_version_semantic_ref_version_shape",
        "CHECK (semantic_ref_version = ANY (ARRAY[0,1]))",
    ),
    (
        "core", "webhook_delivery", "webhook_delivery_auto_rearm_count_ok",
        "CHECK (auto_rearm_count >= 0)",
    ),
    (
        "core", "webhook_delivery", "webhook_delivery_operator_recovery_ok",
        "CHECK ("
        "(operator_recovery_count=0 AND operator_recovery_id IS NULL "
        "AND operator_recovered_at IS NULL "
        "AND operator_recovery_batch_size IS NULL "
        "AND operator_recovery_batch_token IS NULL) OR "
        "(operator_recovery_count=1 AND operator_recovery_id IS NOT NULL "
        "AND operator_recovered_at IS NOT NULL "
        "AND length(operator_recovery_id)>=1 "
        "AND length(operator_recovery_id)<=120 "
        "AND operator_recovery_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$' "
        "AND operator_recovery_batch_size IS NOT NULL "
        "AND operator_recovery_batch_size>=1 "
        "AND operator_recovery_batch_size<=10 "
        "AND operator_recovery_batch_token IS NOT NULL)"
        ")",
    ),
    (
        "core", "webhook_delivery", "webhook_delivery_operator_continuation_ok",
        "CHECK ("
        "(operator_continuation_count=0 "
        "AND operator_continuation_id IS NULL "
        "AND operator_continued_at IS NULL "
        "AND operator_continuation_sha IS NULL) OR "
        "(operator_continuation_count=1 AND operator_recovery_count=1 "
        "AND operator_continuation_id IS NOT NULL "
        "AND length(operator_continuation_id)>=1 "
        "AND length(operator_continuation_id)<=120 "
        "AND operator_continuation_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$' "
        "AND operator_continued_at IS NOT NULL "
        "AND operator_continuation_sha IS NOT NULL "
        "AND operator_continuation_sha ~ '^[0-9a-f]{40}$')"
        ")",
    ),
    (
        "core", "webhook_delivery", "webhook_delivery_operator_github_redelivery_ok",
        "CHECK ("
        "(operator_github_redelivery_count=0 "
        "AND operator_github_redelivery_delivery_id IS NULL "
        "AND operator_github_redelivery_sha IS NULL "
        "AND operator_github_redelivery_spent_at IS NULL "
        "AND operator_github_redelivery_outcome IS NULL) OR "
        "(operator_github_redelivery_count=1 "
        "AND operator_recovery_count=1 "
        "AND operator_continuation_count=1 "
        "AND operator_github_redelivery_delivery_id IS NOT NULL "
        "AND operator_github_redelivery_delivery_id>0 "
        "AND operator_github_redelivery_sha IS NOT NULL "
        "AND operator_github_redelivery_sha ~ '^[0-9a-f]{40}$' "
        "AND operator_github_redelivery_spent_at IS NOT NULL "
        "AND (operator_github_redelivery_outcome IS NULL OR "
        "operator_github_redelivery_outcome = ANY (ARRAY["
        "'accepted','transport_ambiguous','redirect_rejected','auth_rejected',"
        "'rate_limited','request_rejected','server_rejected','unexpected_status']))"
        ")"
        ")",
    ),
    (
        "core", "graph_convergence_lease",
        "graph_convergence_lease_account_cap_one",
        "CHECK (slot = 1)",
    ),
    (
        "core", "policy_refresh_outbox",
        "policy_refresh_outbox_onboarding_shape",
        "CHECK ("
        "onboarding_index>=0 AND onboarding_index<=300 "
        "AND (onboarding_plan IS NULL OR cardinality(onboarding_plan)<=300) "
        "AND (onboarding_plan IS NULL OR onboarding_index<=cardinality(onboarding_plan)) "
        "AND (onboarding_plan IS NOT NULL OR (onboarding_index=0 "
        "AND NOT onboarding_truncated AND NOT onboarding_watching_done)) "
        "AND (NOT onboarding_watching_done OR (onboarding_plan IS NOT NULL "
        "AND onboarding_index=cardinality(onboarding_plan))) "
        "AND (NOT onboarding_head_pending OR (onboarding_pending "
        "AND request_kind='graph' AND onboarding_plan IS NULL "
        "AND onboarding_index=0 AND NOT onboarding_truncated "
        "AND NOT onboarding_watching_done)) "
        "AND (branch<>'__veripsa_onboarding_head__' OR target_sha<>'0000000' "
        "OR onboarding_head_pending) "
        "AND (onboarding_pending OR (NOT onboarding_head_pending "
        "AND onboarding_plan IS NULL AND onboarding_index=0 "
        "AND NOT onboarding_truncated AND NOT onboarding_watching_done))"
        ")",
    ),
    (
        "core", "policy_refresh_outbox",
        "policy_refresh_outbox_onboarding_active",
        "CHECK (NOT onboarding_pending OR (request_kind='graph' AND done_at IS NULL))",
    ),
)

# The trigger clears the executable window on every terminal transition,
# including later lifecycle functions which do not know this module's column.
# A partial hot deploy that left the column/functions but lost this trigger
# would otherwise boot green and retain stale windows indefinitely.
_EXPECTED_TRIGGERS: tuple[
    tuple[str, str, str, str], ...
] = (
    (
        "core", "webhook_delivery",
        "webhook_delivery_clear_terminal_retry_window",
        "_clear_terminal_webhook_retry_window",
    ),
    (
        "core", "policy_refresh_outbox",
        "trg_lock_convergence_delete_router",
        "_lock_convergence_delete_router",
    ),
    (
        "core", "policy_refresh_outbox",
        "trg_cleanup_deleted_convergence_requests",
        "_cleanup_deleted_convergence_requests",
    ),
    (
        "core", "repository_lifecycle_tombstone",
        "trg_cancel_tombstoned_graph_refresh",
        "_cancel_tombstoned_graph_refresh",
    ),
    (
        "core", "graph_convergence_lease",
        "trg_governed_graph_convergence_lease",
        "assert_governed_write",
    ),
)

# The writer payload declares this producer token explicitly. Checking only that
# current_extractor_version() exists is insufficient: a stale DB body returning
# cg1/cg2/cg3 would reject every cg4 payload, while a future body paired with old Python
# would recreate the schema-first version-skew class.
_EXPECTED_EXTRACTOR_VERSION = "cg4"
_EXPECTED_SEMANTIC_REF_VERSION = 1


def _canonical_check_definition(definition: object) -> str:
    """Normalize only PostgreSQL's presentation noise, not semantics.

    Exact equality after this transform deliberately rejects weakened checks
    such as ``OR TRUE`` or ``IN (0,1,2)`` even though they retain all expected
    identifier/literal substrings.
    """
    value = str(definition or "").replace("::text", "").replace('"', "")
    return re.sub(r"[\s()]+", "", value)


def _canonical_index_definition(definition: object) -> str:
    value = str(definition or "").replace("::text", "")
    return re.sub(r'[\s()"]+', "", value)


class ContractViolation(NamedTuple):
    """One mismatch between the runtime's expectation and the live DB. The full list is JSON-serialized into the
    /healthz 503 body so an operator sees EXACTLY what is missing — no guessing from logs."""
    kind: str           # missing/wrong function, column or kind constraint; or check_error
    name: str           # the qualified target — e.g. "core.act_for_claim_with_authority" or "core.claim.is_draft"
    expected: str       # the expected signature/type — e.g. "arity=9" or "udt_name=bool"
    actual: str         # what the live DB reports — e.g. "arity=8" or "MISSING" or "udt_name=text"


class ContractResult(NamedTuple):
    """The result of one boot-time contract check. `healthy` is True iff every expected function/column/check matched.
    `violations` is the empty tuple on healthy; otherwise it names every mismatch. `checked` is the count of
    (functions + columns) actually examined — surfaced on /healthz so an operator can see the check ran.
    `skipped` is True when VERIPSA_SCHEMA_CONTRACT=0 (the kill switch) — then `healthy` is True regardless."""
    healthy: bool
    violations: tuple[ContractViolation, ...]
    checked: int
    skipped: bool
    phase: str = "complete"
    elapsed_ms: int = 0

    @property
    def check_error(self) -> bool:
        """True only when the catalog assertion itself did not complete.

        A catalog transport/deadline failure is operationally different from
        a completed assertion which proved immutable schema drift.  The web
        boot path restarts on the former before binding HTTP, while retaining
        the latter as a stable /healthz diagnostic.
        """
        return any(v.kind == "check_error" for v in self.violations)

    @property
    def mismatch(self) -> bool:
        """True only for a completed, unhealthy schema comparison."""
        return not self.healthy and not self.check_error


def _contract_enabled() -> bool:
    """The kill switch: VERIPSA_SCHEMA_CONTRACT=0 disables the assertion. Read at CALL time so an operator can
    flip it without restart (the boot path reads it once, but a test reaching the check directly sees the live
    value). Default ON — refusing to start on schema drift is the safer failure mode."""
    return os.environ.get("VERIPSA_SCHEMA_CONTRACT", "1") != "0"


class _DeadlineCursor:
    """Refuse to start another statement after the absolute boot wall.

    The connection arms statement_timeout once.  This wrapper deliberately
    does not issue SET before each catalog batch: per-row re-arming was the
    source of hundreds of avoidable round trips during Render web boot.
    """

    def __init__(self, cursor, deadline: float):
        self._cursor = cursor
        self._deadline = float(deadline)

    def execute(self, sql, args=None):
        if time.monotonic() >= self._deadline:
            raise _db_connect.DatabaseConnectDeadlineExceeded(
                "schema contract absolute deadline exceeded")
        if args is None:
            return self._cursor.execute(sql)
        return self._cursor.execute(sql, args)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


def _check_schema_contract_sync(
        dsn: str, *, deadline: float) -> ContractResult:
    """Boot-time schema↔runtime contract check. Connects to `dsn` as the live App role (veripsa_app — the same role
    every webhook event uses), queries pg_proc for every (name, arity) in _EXPECTED_FUNCTIONS, and
    pg_catalog for every expected column and graph-kind CHECK. Returns a ContractResult naming every violation
    (or empty + healthy=True if the live DB matches).

    Fail-closed: any unexpected error during the check (a DB blip, a permission denial, a network glitch) is
    REPORTED as a single "check_error" violation, NOT silently swallowed. The web boot path treats that transient
    result differently from completed schema drift and exits before HTTP bind, allowing Render to restart cleanly.

    Content-free: pg_catalog metadata only. No tenant rows are read.

    Kill switch: VERIPSA_SCHEMA_CONTRACT=0 → returns ContractResult(healthy=True, violations=(), checked=0,
    skipped=True). The boot path logs the SKIP explicitly so it never goes silent.
    """
    import psycopg2  # imported lazily so unit-only imports stay cheap.

    started_at = time.monotonic()
    violations: list[ContractViolation] = []
    checked = 0
    phase = "connect"
    conn = None
    try:
        remaining = max(1, int(float(deadline) - time.monotonic()))
        conn = _db_connect.connect(
            psycopg2.connect,
            dsn,
            deadline=deadline,
            connect_timeout=min(
                _SCHEMA_CONTRACT_CONNECT_TIMEOUT_SECONDS, remaining),
            options=(
                f"-c statement_timeout="
                f"{_SCHEMA_CONTRACT_STATEMENT_TIMEOUT_MS}ms "
                f"-c lock_timeout={_SCHEMA_CONTRACT_LOCK_TIMEOUT_MS}ms"
            ),
        )
        conn.autocommit = True
        with conn.cursor() as raw_cur:
            # One setup for the whole bounded pass.  The startup option already
            # protects this SET; tightening once to the remaining absolute wall
            # avoids the former SET + business-query pair for every expectation.
            phase = "timeout_setup"
            remaining_ms = int(
                max(0.0, float(deadline) - time.monotonic()) * 1000)
            if remaining_ms < 1:
                raise _db_connect.DatabaseConnectDeadlineExceeded(
                    "schema contract absolute deadline exceeded")
            statement_ms = max(
                1, min(_SCHEMA_CONTRACT_STATEMENT_TIMEOUT_MS, remaining_ms))
            raw_cur.execute(f"SET statement_timeout = {statement_ms}")
            cur = _DeadlineCursor(raw_cur, deadline)

            # Batch 1/7: every expected function overload plus its EXECUTE
            # decision.  PostgreSQL can have several type-distinct overloads
            # with the same name and arity.  Because this contract does not yet
            # pin argument identities, fail closed unless *every* same-key OID
            # is executable; an executable decoy must not hide the runtime
            # target's revoked privilege.  Retain the OID count as a contract:
            # the runtime cannot select an exact target by (name, arity) once
            # that key is ambiguous, regardless of aggregate ACL state.
            phase = "functions"
            expected_fn_names = sorted(
                {name for name, _ in _EXPECTED_FUNCTIONS}
                | {name for name, _ in _EXPECTED_EXECUTABLE_FUNCTIONS}
            )
            cur.execute(
                "/* schema-contract:functions */ "
                "SELECT p.proname,p.pronargs,count(*)::bigint,"
                "bool_and(has_function_privilege("
                "current_user,p.oid,'EXECUTE') IS TRUE) "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='core' AND p.proname=ANY(%s) "
                "GROUP BY p.proname,p.pronargs",
                (expected_fn_names,),
            )
            function_rows = cur.fetchall()
            overload_counts: dict[tuple[str, int], int] = {}
            executable: dict[tuple[str, int], bool] = {}
            for row in function_rows:
                key = (str(row[0]), int(row[1]))
                overload_counts[key] = (
                    overload_counts.get(key, 0) + int(row[2]))
                row_executable = row[3] is True
                executable[key] = (
                    executable[key] and row_executable
                    if key in executable
                    else row_executable
                )
            present = set(overload_counts)
            present_names = {name for name, _ in present}

            for name, arity in _EXPECTED_FUNCTIONS:
                checked += 1
                key = (name, arity)
                if key in present:
                    oid_count = overload_counts[key]
                    if oid_count != 1:
                        violations.append(ContractViolation(
                            kind="ambiguous_function_overload",
                            name=f"core.{name}/{arity}",
                            expected="oid_count=1",
                            actual=f"oid_count={oid_count}",
                        ))
                    continue
                if name in present_names:
                    actual_arities = sorted(
                        {a for n, a in present if n == name})
                    violations.append(ContractViolation(
                        kind="wrong_arity",
                        name=f"core.{name}",
                        expected=f"arity={arity}",
                        actual=f"arity={actual_arities}",
                    ))
                else:
                    violations.append(ContractViolation(
                        kind="missing_function",
                        name=f"core.{name}",
                        expected=f"arity={arity}",
                        actual="MISSING",
                    ))

            for name, arity in _EXPECTED_EXECUTABLE_FUNCTIONS:
                checked += 1
                key = (name, arity)
                # A missing signature is already named by the function check;
                # avoid manufacturing a second ACL violation for no object.
                if (
                    overload_counts.get(key) == 1
                    and executable.get(key) is not True
                ):
                    violations.append(ContractViolation(
                        kind="missing_execute_privilege",
                        name=f"core.{name}/{arity}",
                        expected="veripsa_app EXECUTE=true",
                        actual="EXECUTE=false",
                    ))

            # Batch 2/7: the two governed producer values are runtime semantics, not mere
            # catalog presence. Execute the present functions together in one
            # fixed business query and retain the former missing-function and
            # wrong-value classifications.
            phase = "producer_values"
            value_calls: list[str] = []
            value_expectations: list[tuple[str, object]] = []
            if ("current_extractor_version", 0) in present:
                value_calls.append("core.current_extractor_version()")
                value_expectations.append((
                    "current_extractor_version", _EXPECTED_EXTRACTOR_VERSION))
            if ("current_semantic_ref_version", 0) in present:
                value_calls.append("core.current_semantic_ref_version()")
                value_expectations.append((
                    "current_semantic_ref_version",
                    _EXPECTED_SEMANTIC_REF_VERSION,
                ))
            values_row = None
            if value_calls:
                cur.execute(
                    "/* schema-contract:producer-values */ SELECT "
                    + ",".join(value_calls)
                )
                values_row = cur.fetchone()
            for offset, (name, expected_value) in enumerate(value_expectations):
                actual_value = (
                    values_row[offset]
                    if values_row is not None and len(values_row) > offset
                    else None
                )
                if actual_value != expected_value:
                    violations.append(ContractViolation(
                        kind="wrong_value",
                        name=f"core.{name}",
                        expected=f"value={expected_value}",
                        actual=f"value={actual_value}",
                    ))
            # Both value expectations count even when their functions are
            # absent; absence was already classified above.
            checked += 2

            # Batch 3/7: all required columns.  Parallel unnest supplies one
            # expected row per tuple, while pg_catalog remains visible to the
            # no-table-SELECT application role.
            phase = "columns"
            cur.execute(
                "/* schema-contract:columns */ "
                "WITH expected(schema_name,table_name,column_name) AS ("
                "SELECT * FROM unnest(%s::text[],%s::text[],%s::text[])) "
                "SELECT e.schema_name,e.table_name,e.column_name,t.typname "
                "FROM expected e "
                "LEFT JOIN pg_namespace n ON n.nspname=e.schema_name "
                "LEFT JOIN pg_class c ON c.relnamespace=n.oid "
                " AND c.relname=e.table_name "
                "LEFT JOIN pg_attribute a ON a.attrelid=c.oid "
                " AND a.attname=e.column_name AND a.attnum>0 "
                " AND NOT a.attisdropped "
                "LEFT JOIN pg_type t ON t.oid=a.atttypid",
                (
                    [row[0] for row in _EXPECTED_COLUMNS],
                    [row[1] for row in _EXPECTED_COLUMNS],
                    [row[2] for row in _EXPECTED_COLUMNS],
                ),
            )
            columns = {
                (str(row[0]), str(row[1]), str(row[2])): row[3]
                for row in cur.fetchall()
            }
            for schema, table, column, expected_udt in _EXPECTED_COLUMNS:
                checked += 1
                actual_udt = columns.get((schema, table, column))
                if actual_udt is None:
                    violations.append(ContractViolation(
                        kind="missing_column",
                        name=f"{schema}.{table}.{column}",
                        expected=f"udt_name={expected_udt}",
                        actual="MISSING",
                    ))
                elif actual_udt != expected_udt:
                    violations.append(ContractViolation(
                        kind="wrong_column_type",
                        name=f"{schema}.{table}.{column}",
                        expected=f"udt_name={expected_udt}",
                        actual=f"udt_name={actual_udt}",
                    ))

            # Batch 4/7: non-table relations.
            phase = "relations"
            cur.execute(
                "/* schema-contract:relations */ "
                "WITH expected(schema_name,relation_name) AS ("
                "SELECT * FROM unnest(%s::text[],%s::text[])) "
                "SELECT e.schema_name,e.relation_name,c.relkind "
                "FROM expected e "
                "LEFT JOIN pg_namespace n ON n.nspname=e.schema_name "
                "LEFT JOIN pg_class c ON c.relnamespace=n.oid "
                " AND c.relname=e.relation_name",
                (
                    [row[0] for row in _EXPECTED_RELATIONS],
                    [row[1] for row in _EXPECTED_RELATIONS],
                ),
            )
            relations = {
                (str(row[0]), str(row[1])): row[2]
                for row in cur.fetchall()
            }
            for schema, relation, expected_relkind in _EXPECTED_RELATIONS:
                checked += 1
                actual_relkind = relations.get((schema, relation))
                qualified = f"{schema}.{relation}"
                if actual_relkind is None:
                    violations.append(ContractViolation(
                        kind="missing_relation",
                        name=qualified,
                        expected=f"relkind={expected_relkind}",
                        actual="MISSING",
                    ))
                elif actual_relkind != expected_relkind:
                    violations.append(ContractViolation(
                        kind="wrong_relation_kind",
                        name=qualified,
                        expected=f"relkind={expected_relkind}",
                        actual=f"relkind={actual_relkind}",
                    ))

            # Batch 5/7: index publication state and full definition.
            phase = "indexes"
            cur.execute(
                "/* schema-contract:indexes */ "
                "WITH expected(schema_name,table_name,index_name) AS ("
                "SELECT * FROM unnest(%s::text[],%s::text[],%s::text[])) "
                "SELECT e.schema_name,e.table_name,e.index_name,tbl.relname,"
                "i.indisvalid,i.indisready,pg_get_indexdef(i.indexrelid) "
                "FROM expected e "
                "LEFT JOIN pg_namespace n ON n.nspname=e.schema_name "
                "LEFT JOIN pg_class idx ON idx.relnamespace=n.oid "
                " AND idx.relname=e.index_name "
                "LEFT JOIN pg_index i ON i.indexrelid=idx.oid "
                "LEFT JOIN pg_class tbl ON tbl.oid=i.indrelid "
                " AND tbl.relname=e.table_name",
                (
                    [row[0] for row in _EXPECTED_INDEXES],
                    [row[1] for row in _EXPECTED_INDEXES],
                    [row[2] for row in _EXPECTED_INDEXES],
                ),
            )
            indexes = {
                (str(row[0]), str(row[1]), str(row[2])): row[3:]
                for row in cur.fetchall()
            }
            for schema, table, index, expected_keys in _EXPECTED_INDEXES:
                checked += 1
                row = indexes.get((schema, table, index))
                qualified = f"{schema}.{index}"
                if row is None or row[0] != table or row[3] is None:
                    violations.append(ContractViolation(
                        kind="missing_index",
                        name=qualified,
                        expected=(
                            f"table={table},valid=true,ready=true,"
                            f"keys={expected_keys}"
                        ),
                        actual="MISSING",
                    ))
                    continue
                actual_index = _canonical_index_definition(row[3])
                expected_index = _canonical_index_definition(expected_keys)
                if (
                    row[1] is not True
                    or row[2] is not True
                    or expected_index not in actual_index
                ):
                    violations.append(ContractViolation(
                        kind="wrong_index",
                        name=qualified,
                        expected=(
                            f"valid=true,ready=true,keys={expected_index}"
                        ),
                        actual=(
                            f"valid={bool(row[1])},ready={bool(row[2])},"
                            f"definition={actual_index}"
                        ),
                    ))

            # Batch 6/7: both exact kind sets and semantic CHECK definitions.
            phase = "constraints"
            expected_constraints = tuple(
                (row[0], row[1], row[2])
                for row in (
                    _EXPECTED_KIND_CONSTRAINTS
                    + _EXPECTED_CHECK_CONSTRAINTS
                )
            )
            cur.execute(
                "/* schema-contract:constraints */ "
                "WITH expected(schema_name,table_name,constraint_name) AS ("
                "SELECT * FROM unnest(%s::text[],%s::text[],%s::text[])) "
                "SELECT e.schema_name,e.table_name,e.constraint_name,"
                "pg_get_constraintdef(con.oid),con.convalidated "
                "FROM expected e "
                "LEFT JOIN pg_namespace n ON n.nspname=e.schema_name "
                "LEFT JOIN pg_class c ON c.relnamespace=n.oid "
                " AND c.relname=e.table_name "
                "LEFT JOIN pg_constraint con ON con.conrelid=c.oid "
                " AND con.conname=e.constraint_name AND con.contype='c'",
                (
                    [row[0] for row in expected_constraints],
                    [row[1] for row in expected_constraints],
                    [row[2] for row in expected_constraints],
                ),
            )
            constraints = {
                (str(row[0]), str(row[1]), str(row[2])): row[3:]
                for row in cur.fetchall()
            }
            for schema, table, constraint, expected_values in _EXPECTED_KIND_CONSTRAINTS:
                checked += 1
                row = constraints.get((schema, table, constraint))
                qualified = f"{schema}.{table}.{constraint}"
                if row is None or row[0] is None:
                    violations.append(ContractViolation(
                        kind="missing_constraint",
                        name=qualified,
                        expected=f"kinds={sorted(expected_values)}",
                        actual="MISSING",
                    ))
                    continue
                actual_values = {
                    token.replace("''", "'")
                    for token in re.findall(
                        r"'((?:''|[^'])*)'", str(row[0] or ""))
                }
                expected_set = set(expected_values)
                if actual_values != expected_set or row[1] is not True:
                    violations.append(ContractViolation(
                        kind="wrong_constraint",
                        name=qualified,
                        expected=(
                            f"kinds={sorted(expected_set)},validated=true"
                        ),
                        actual=(
                            f"kinds={sorted(actual_values)},"
                            f"validated={bool(row[1])}"
                        ),
                    ))

            for schema, table, constraint, expected_definition in _EXPECTED_CHECK_CONSTRAINTS:
                checked += 1
                row = constraints.get((schema, table, constraint))
                qualified = f"{schema}.{table}.{constraint}"
                if row is None or row[0] is None:
                    violations.append(ContractViolation(
                        kind="missing_constraint",
                        name=qualified,
                        expected=expected_definition,
                        actual="MISSING",
                    ))
                    continue
                expected_canonical = _canonical_check_definition(
                    expected_definition)
                actual_canonical = _canonical_check_definition(row[0])
                if actual_canonical != expected_canonical or row[1] is not True:
                    violations.append(ContractViolation(
                        kind="wrong_constraint",
                        name=qualified,
                        expected=f"{expected_canonical},validated=true",
                        actual=(
                            f"{actual_canonical},validated={bool(row[1])}"
                        ),
                    ))

            # Batch 7/7: trigger enablement and target function identity.
            phase = "triggers"
            cur.execute(
                "/* schema-contract:triggers */ "
                "WITH expected(schema_name,table_name,trigger_name) AS ("
                "SELECT * FROM unnest(%s::text[],%s::text[],%s::text[])) "
                "SELECT e.schema_name,e.table_name,e.trigger_name,t.tgenabled,"
                "p.proname,np.nspname "
                "FROM expected e "
                "LEFT JOIN pg_namespace n ON n.nspname=e.schema_name "
                "LEFT JOIN pg_class c ON c.relnamespace=n.oid "
                " AND c.relname=e.table_name "
                "LEFT JOIN pg_trigger t ON t.tgrelid=c.oid "
                " AND t.tgname=e.trigger_name AND NOT t.tgisinternal "
                "LEFT JOIN pg_proc p ON p.oid=t.tgfoid "
                "LEFT JOIN pg_namespace np ON np.oid=p.pronamespace",
                (
                    [row[0] for row in _EXPECTED_TRIGGERS],
                    [row[1] for row in _EXPECTED_TRIGGERS],
                    [row[2] for row in _EXPECTED_TRIGGERS],
                ),
            )
            triggers = {
                (str(row[0]), str(row[1]), str(row[2])): row[3:]
                for row in cur.fetchall()
            }
            for schema, table, trigger, expected_function in _EXPECTED_TRIGGERS:
                checked += 1
                row = triggers.get((schema, table, trigger))
                qualified = f"{schema}.{table}.{trigger}"
                if row is None or row[0] is None:
                    violations.append(ContractViolation(
                        kind="missing_trigger",
                        name=qualified,
                        expected=(
                            f"enabled=O,function=core.{expected_function}"
                        ),
                        actual="MISSING",
                    ))
                elif (
                    row[0] != "O"
                    or row[1] != expected_function
                    or row[2] != "core"
                ):
                    violations.append(ContractViolation(
                        kind="wrong_trigger",
                        name=qualified,
                        expected=(
                            f"enabled=O,function=core.{expected_function}"
                        ),
                        actual=(
                            f"enabled={row[0]},function={row[2]}.{row[1]}"
                        ),
                    ))
            phase = "complete"
    except Exception as exc:
        # Never include an exception message: connector errors can contain a
        # DSN, while server/catalog messages may contain identifiers outside
        # this deliberately content-free contract.  Type + fixed phase are
        # sufficient for a restart decision and operator correlation.
        violations.append(ContractViolation(
            kind="check_error",
            name=f"schema_contract_check.{phase}",
            expected="bounded successful catalog check",
            actual=f"{type(exc).__name__}: catalog_check_failed",
        ))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return ContractResult(
        healthy=not violations,
        violations=tuple(violations),
        checked=checked,
        skipped=False,
        phase=phase,
        elapsed_ms=max(0, int((time.monotonic() - started_at) * 1000)),
    )


_CHECK_WORKER_LOCK = threading.Lock()
_ACTIVE_CHECK_WORKER: Optional[threading.Thread] = None


def _operational_check_error(
        *, phase: str, actual_type: str, elapsed_ms: int = 0) -> ContractResult:
    """Build one content-free transient catalog-check failure."""
    return ContractResult(
        healthy=False,
        violations=(ContractViolation(
            kind="check_error",
            name=f"schema_contract_check.{phase}",
            expected="bounded successful catalog check",
            actual=f"{actual_type}: catalog_check_failed",
        ),),
        checked=0,
        skipped=False,
        phase=phase,
        elapsed_ms=max(0, int(elapsed_ms)),
    )


def check_schema_contract(dsn: str) -> ContractResult:
    """Run the fail-closed catalog assertion behind one absolute boot wall.

    libpq timeouts cannot interrupt every post-connect kernel/network wait.
    The catalog worker is therefore one fixed daemon: if it does not publish a
    result before the wall, boot receives a diagnostic check_error and exits
    before HTTP bind.  A still-unwinding timed-out worker prevents another
    check from being started in this process, so retries cannot multiply stuck
    connector/query threads.
    """
    if not _contract_enabled():
        return ContractResult(
            healthy=True, violations=(), checked=0, skipped=True,
            phase="skipped", elapsed_ms=0)

    started_at = time.monotonic()
    timeout = float(_SCHEMA_CONTRACT_TIMEOUT_SECONDS)
    deadline = started_at + timeout
    results: queue.Queue[tuple[float, ContractResult]] = queue.Queue(
        maxsize=1)

    def _run() -> None:
        global _ACTIVE_CHECK_WORKER
        try:
            result = _check_schema_contract_sync(
                dsn, deadline=deadline)
        except BaseException as exc:
            result = _operational_check_error(
                phase="worker",
                actual_type=type(exc).__name__,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
            )
        # Capture completion before lock scheduling or queue publication.  The
        # caller may itself be descheduled after join() reaches the wall; only
        # work that actually completed by the absolute deadline is admissible.
        finished_at = time.monotonic()
        # _check_schema_contract_sync closes its connection before returning.
        # Release the singleton only after DB work is finished, then publish.
        with _CHECK_WORKER_LOCK:
            if _ACTIVE_CHECK_WORKER is threading.current_thread():
                _ACTIVE_CHECK_WORKER = None
        try:
            results.put_nowait((finished_at, result))
        except queue.Full:
            pass

    worker = threading.Thread(
        target=_run,
        name="veripsa-schema-contract",
        daemon=True,
    )
    global _ACTIVE_CHECK_WORKER
    with _CHECK_WORKER_LOCK:
        if (
            _ACTIVE_CHECK_WORKER is not None
            and _ACTIVE_CHECK_WORKER.is_alive()
        ):
            return _operational_check_error(
                phase="in_progress",
                actual_type="SchemaContractCheckInProgress",
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
            )
        _ACTIVE_CHECK_WORKER = worker
        try:
            worker.start()
        except Exception as exc:
            _ACTIVE_CHECK_WORKER = None
            return _operational_check_error(
                phase="worker_start",
                actual_type=type(exc).__name__,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
            )
    worker.join(max(0.0, deadline - time.monotonic()))
    try:
        finished_at, result = results.get_nowait()
    except queue.Empty:
        pass
    else:
        if finished_at <= deadline:
            return result
    return _operational_check_error(
        phase="timeout",
        actual_type="DatabaseConnectDeadlineExceeded",
        elapsed_ms=int((time.monotonic() - started_at) * 1000),
    )


# MODULE-LEVEL HEALTH FLAG — set by the boot path (server_boot.wire_runtime) after running check_schema_contract.
# server_http.do_GET("/healthz") reads this through _server() so a violation immediately flips /healthz to 503,
# and the server_http handler stays read-only with respect to the contract (the boot path is the sole writer).
# Default values are the "no check has been run yet" shape (healthy=True so a test that imports the module without
# booting through wire_runtime doesn't spuriously 503; the boot path is the one that sets it for real).
_SCHEMA_HEALTHY: bool = True
_SCHEMA_RESULT: Optional[ContractResult] = None


def set_boot_result(result: ContractResult) -> None:
    """The boot path calls this exactly once after running check_schema_contract. The /healthz handler reads the
    module-level flag through get_boot_result() so a violation propagates to the probe with no extra plumbing.
    A test that wants to simulate a violation calls this directly with a synthetic ContractResult."""
    global _SCHEMA_HEALTHY, _SCHEMA_RESULT
    _SCHEMA_RESULT = result
    _SCHEMA_HEALTHY = result.healthy


def get_boot_result() -> Optional[ContractResult]:
    """The /healthz handler reads this. None = the check has not been run (so the probe behaves as before — a
    test importing server_http without booting through wire_runtime keeps its old 200/503 semantics). A non-None
    ContractResult drives the probe: healthy=False → 503 with the violations in the body."""
    return _SCHEMA_RESULT


def is_healthy() -> bool:
    """Fast path the /healthz handler uses to decide if it should 503 on contract grounds. True when no check has
    been run yet (default) or when the last check passed; False when the last check found a violation."""
    return _SCHEMA_HEALTHY


def reset_for_test() -> None:
    """Tests that exercise multiple boot scenarios reset the module-level flag between runs so the previous run's
    state never leaks. Not part of the prod runtime contract."""
    global _SCHEMA_HEALTHY, _SCHEMA_RESULT
    _SCHEMA_HEALTHY = True
    _SCHEMA_RESULT = None
