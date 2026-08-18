#!/usr/bin/env python3
"""Veripsa GitHub App — the WEBHOOK SERVER (Part B: the live last mile, deploy-ready, $0 to trial).

The thin I/O shell around the proven brain (webhook.handle_pull_request) and renderer (render.py):

  GitHub ─webhook▶ verify HMAC ─▶ ENQUEUE ─▶ 202 (ack-fast)   │ event worker ─▶ persist facts/graph request
                                                              │ convergence worker ─▶ graph → check/comment
  (HTTP and keyed event workers never clone/extract; durable  │ exact repo-id/generation/SHA fences every turn)

DESIGN: handle_event is PURE over an injected `db(sql,args)` (authed as veripsa_app) and a `gh` client, so the
whole loop is testable offline with a Fake client + a real DB (tests/test_server.py) — no deploy, no GitHub
account. The real client (GitHubREST) plugs in App-JWT auth + REST calls for the live run. Content-free: a push
persists its exact graph coordinate and returns; the separately sized convergence service later downloads @sha
into a temp dir, extracts paths/edges only, commits under lifecycle CAS, and deletes the transient code.

LIVE (the only PO-gated, money/external part — registration + a public URL):
  env: VERIPSA_DSN (postgres as veripsa_app), GH_WEBHOOK_SECRET, GH_APP_ID, GH_PRIVATE_KEY (PEM path or value).
  GitHub App perms: Checks=write+read, Pull requests=write, Contents=read; events: pull_request, push, AND
    check_suite + check_run (the last two REQUIRED for the RED/STUCK-PR signal — stuck_prs_surface — to fire
    live: GitHub only delivers check_suite/check_run if the App subscribes to them at registration; without
    that subscription the check handler in handle_event is inert. Checks=read lets the handler observe results).
  $0 trial: create the (free) App, point its webhook at a free smee.io URL, run `python3 github-app/server.py`
  locally with `smee -u <url> -P /webhook -p 8000`. A real PR then gets the real comment — nothing deployed.
"""
from __future__ import annotations

import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webhook import handle_pull_request, refresh_inflight, _bounded_claim_id, _CHANGE_ID_CAP  # noqa: E402
try:
    from server_dbops import (_repo_lock_args, _take_repo_lock, _release_repo_lock,  # noqa: E402
                              _take_repository_id_lock,
                              _GRAPH_BULK_LOADED_GUC, _refresh_graph_stats_if_bulk_loaded,
                              _make_db)
except ImportError:  # imported as a package
    from .server_dbops import (_repo_lock_args, _take_repo_lock, _release_repo_lock,  # noqa: E402
                               _take_repository_id_lock,
                               _GRAPH_BULK_LOADED_GUC, _refresh_graph_stats_if_bulk_loaded,
                               _make_db)
try:
    from github_rest import GitHubREST  # noqa: E402
except ImportError:  # imported as a package
    from .github_rest import GitHubREST  # noqa: E402
try:
    from alerts import (AlertSink,  # noqa: E402  (evaluators → health_watchdog.py)
                        _derived_worker_restart_seconds as _worker_restart_seconds,
                        _derived_worker_stuck_seconds as _worker_stuck_seconds)
except ImportError:  # imported as a package
    from .alerts import (AlertSink,
                         _derived_worker_restart_seconds as _worker_restart_seconds,
                         _derived_worker_stuck_seconds as _worker_stuck_seconds)
try:
    from render import (cleared_comment_body, watching_check, quota_paused_check,  # noqa: E402
                        quota_paused_comment_body)
except ImportError:  # imported as a package
    from .render import (cleared_comment_body, watching_check, quota_paused_check,  # noqa: E402
                         quota_paused_comment_body)
# The ack-fast event queue lives in its own module (event_queue.py) so this hotspot file is finer-grained. It
# imports NOTHING from server.py (no circular import): the server seams it needs (_event_account_key / _event_repo
# / _branch_from_ref / make_db_processor) are INJECTED at the one construction site below. _PER_ACCOUNT_QUEUE_CAP
# is re-exported here (the cap default + the live FAIRNESS GUARD constant) for backward compatibility.
try:
    from event_queue import EventQueue, _FairQueue, _PER_ACCOUNT_QUEUE_CAP  # noqa: E402,F401
except ImportError:  # imported as a package
    from .event_queue import EventQueue, _FairQueue, _PER_ACCOUNT_QUEUE_CAP  # noqa: E402,F401
# The DURABLE WEBHOOK INBOX (delivery_queue.py) is the ROOT durability boundary in front of the in-memory queue
# above: do_POST PERSISTS a SANITIZED delivery to core.webhook_delivery BEFORE the 202, the worker claims →
# processes → finishes it (wrap_processor), and start_recovery_loop re-submits queued/stale-processing rows on
# boot — so a crash / Render rolling deploy / OOM after the 202 can no longer LOSE an accepted event (a 202'd
# delivery is NOT redelivered by GitHub). The in-memory _FairQueue stays the in-PROCESS scheduler, now fed FROM
# the store. Like the other split modules it imports NOTHING from server.py (no circular import).
try:
    from delivery_queue import (DeliveryStore, start_instance_liveness_loop,  # noqa: E402,F401
                                start_recovery_loop)
except ImportError:  # imported as a package
    from .delivery_queue import (DeliveryStore, start_instance_liveness_loop,  # noqa: E402,F401
                                 start_recovery_loop)
# G4 POLICY-CHANGE REFRESH (policy_refresh_queue.py): a sibling durable-outbox drainer. When an owner tunes a
# policy knob, every canonical policy writer enqueues a content-free refresh row in the SAME txn as the policy
# commit; this background loop later re-derives that tenant's OPEN PRs under the new policy (recompute
# main_impact_surface + re-post via the idempotent _post_refreshes), OUTSIDE any DB txn. Imports nothing from
# server.py at load (no circular import); it reaches refresh_inflight/_post_refreshes/_scoped_db lazily.
try:
    from policy_refresh_queue import PolicyRefreshStore, start_policy_refresh_loop  # noqa: E402,F401
except ImportError:  # imported as a package
    from .policy_refresh_queue import PolicyRefreshStore, start_policy_refresh_loop  # noqa: E402,F401
# env_int: the VALIDATED env-knob reader (fail SAFE on a typo'd cap — a non-int or out-of-range value raises a
# loud, actionable refusal that names the var + value + range, instead of crashing with a bare ValueError or
# silently misbehaving on a 0/negative cap). The same parse→bound discipline core._policy_int applies in the DB.
try:
    from env_config import env_int  # noqa: E402
except ImportError:  # imported as a package
    from .env_config import env_int  # noqa: E402
# The HEALTH + WATCHDOG concern (worker health snapshot, the periodic monitor that turns /healthz signals into
# PUSH alerts, and the operator db-usage sample) lives in its own module (health_watchdog.py) so this hotspot
# file is finer-grained. Like event_queue.py it imports NOTHING from server.py at load time (no circular
# import): the one seam its tick needs — graph_freshness_all (re-exported just below from graph_freshness.py;
# also used by serve()'s /freshz + self_heal_main_graph) — is INJECTED at the serve() wiring below (resolved
# lazily otherwise). These four names are re-exported here for backward compatibility (serve()'s handler + gate).
try:
    from health_watchdog import (health_snapshot, db_usage_sample,  # noqa: E402,F401
                                 watchdog_tick, run_watchdog, watchdog_last_tick_seconds,
                                 app_jwt_reachable)
except ImportError:  # imported as a package
    from .health_watchdog import (health_snapshot, db_usage_sample,  # noqa: E402,F401
                                  watchdog_tick, run_watchdog, watchdog_last_tick_seconds,
                                  app_jwt_reachable)
# The GRAPH-FRESHNESS concern (the read-only "is the stored main-graph behind main HEAD?" surface) lives in its
# own module (graph_freshness.py) so this hotspot file is finer-grained. Like the modules above it imports
# NOTHING from server.py (no circular import — server.py imports IT). These names are re-exported here for
# backward compatibility: serve()'s /freshz handler + self_heal_main_graph call graph_freshness/_all, the
# watchdog's lazy fallback does `from server import graph_freshness_all`, and the gate uses S.graph_freshness*.
try:
    from graph_freshness import (graph_freshness, graph_freshness_at_target, graph_freshness_all,  # noqa: E402,F401
                                 _stored_graph_sha, _GRAPH_FRESHNESS_CAP)
except ImportError:  # imported as a package
    from .graph_freshness import (graph_freshness, graph_freshness_at_target, graph_freshness_all,  # noqa: E402,F401
                                  _stored_graph_sha, _GRAPH_FRESHNESS_CAP)
# The GRAPH-INGESTION + RECONCILIATION concern (keep the stored content-free code-graph in sync with the repo:
# _full_ingest / ingest_push / self_heal_main_graph + the install/restart reconcile backfill_open_prs /
# backfill_repo / _onboard_repos / boot_reconcile / purge_repo, plus their private helpers + caps) lives in its
# own module (ingest.py) so this hotspot file is finer-grained. This is the ONE cluster whose functions CALL EACH
# OTHER, so moving them TOGETHER keeps the intra-cluster calls (boot_reconcile→backfill_open_prs→handle_event→
# ingest_push→_full_ingest; self_heal_main_graph→ingest_push) dispatching through ingest.py's globals — so a test
# that monkeypatches ingest._full_ingest / ingest.backfill_open_prs (or ingest._MAX_INGEST_FILES / ._BACKFILL_PR_CAP
# / ._BACKFILL_BRANCH_CAP / ._ONBOARD_REPO_CAP) takes effect on that path. ingest.py imports NOTHING from
# server.py at LOAD time (no
# circular import — server.py imports IT); the few server-side seams it needs (the _as_obj/_as_list payload guards,
# _push_changed_sets, _quota_result, and the handle_event router that backfill re-runs per open PR) are resolved at
# CALL time there via the same lazy `import server` idiom health_watchdog.py uses. These names are RE-EXPORTED here
# for backward compatibility: handle_event calls ingest_push/self_heal_main_graph/_onboard_repos/purge_repo, serve()
# starts boot_reconcile, and the gates/tests reach the cluster via S.<name> (constants too, where they patch caps).
try:
    from ingest import (_full_ingest, _incremental_ingest, _coordinate_paths,  # noqa: E402,F401
                        ingest_push, ingest_push_deferred, self_heal_main_graph,
                        request_main_graph_refresh, request_repository_onboarding,
                        converge_main_graph_strict, _is_sha_not_yet_fetchable,
                        _push_head_commit_time, _graph_would_regress,
                        graph_extraction_liveness,
                        backfill_open_prs, build_open_pr_backfill_plan,
                        resolve_repository_onboarding_head, replay_onboarding_pull_request,
                        surface_onboarding_quota_paused_pull_request,
                        _onboard_repos, _queue_onboard_repos,
                        backfill_repo, purge_repo,
                        boot_reconcile, _reconcile_one_repo,
                        _relocation_breaks_resolution, _added_import_goes_live,
                        _safe_extract_member_name, _safe_extractall, _count_files, _IncrementalUnsafe,
                        _INCR_CAP, _MAX_INGEST_FILES, _BACKFILL_PR_CAP, _BACKFILL_BRANCH_CAP,
                        _ONBOARD_REPO_CAP, _SELF_HEAL_GRAPH)
except ImportError:  # imported as a package
    from .ingest import (_full_ingest, _incremental_ingest, _coordinate_paths,  # noqa: E402,F401
                         ingest_push, ingest_push_deferred, self_heal_main_graph,
                         request_main_graph_refresh, request_repository_onboarding,
                         converge_main_graph_strict, _is_sha_not_yet_fetchable,
                         _push_head_commit_time, _graph_would_regress,
                         graph_extraction_liveness,
                         backfill_open_prs, build_open_pr_backfill_plan,
                         resolve_repository_onboarding_head, replay_onboarding_pull_request,
                         surface_onboarding_quota_paused_pull_request,
                         _onboard_repos, _queue_onboard_repos,
                         backfill_repo, purge_repo,
                         boot_reconcile, _reconcile_one_repo,
                         _relocation_breaks_resolution, _added_import_goes_live,
                         _safe_extract_member_name, _safe_extractall, _count_files, _IncrementalUnsafe,
                         _INCR_CAP, _MAX_INGEST_FILES, _BACKFILL_PR_CAP, _BACKFILL_BRANCH_CAP,
                         _ONBOARD_REPO_CAP, _SELF_HEAL_GRAPH)
# The LIVE PER-EVENT PROCESSOR concern (the layer that wraps each dequeued event with the DB session it needs
# BEFORE the dispatcher runs: the per-(account,repo) advisory LOCK so same-repo events serialize across instances,
# the tenant ROUTING pin, and the ATOMIC body txn — plus the event→(repo,account) routing, the install/uninstall
# per-repo LOCKED fan-out, and the /readyz service-identity self-check) lives in its own module (event_processor.py)
# so this hotspot file is finer-grained. It changes for DIFFERENT reasons than handle_event (locking/txn/scaling/
# tenant semantics — exactly where the #181 cold-stats fix + the per-repo lock churned), not when a new event TYPE
# is handled. Like the modules above it imports NOTHING from server.py at LOAD time (no circular import — server.py
# imports IT); the server-side seams it needs (the _as_obj/_as_list payload guards, the single-connection runner
# _scoped_db — KEPT here so test_external_resilience's _scoped_db monkeypatch still reaches the processor — and the
# handle_event ROUTER it dispatches to) are resolved at CALL time via the same lazy `import server` idiom ingest.py
# uses. These names are RE-EXPORTED here for backward compatibility: serve() builds make_db_processor + wires
# _event_account_key/_event_repo into the queue + calls app_identity_ok on /readyz, __main__'s backfill is unchanged,
# and the gates/tests reach the cluster via S.<name>.
try:
    from event_processor import (_event_repo, _event_account_key, _install_fanout_repos,  # noqa: E402,F401
                                 _INSTALL_FANOUT_FIELD, _process_install_event_per_repo_locked,
                                 make_db_processor, app_identity_ok, _READYZ_INSTALLATION)
except ImportError:  # imported as a package
    from .event_processor import (_event_repo, _event_account_key, _install_fanout_repos,  # noqa: E402,F401
                                  _INSTALL_FANOUT_FIELD, _process_install_event_per_repo_locked,
                                  make_db_processor, app_identity_ok, _READYZ_INSTALLATION)
# The WEBHOOK EVENT-ROUTING BRAIN — the single biggest cohesive cluster of this god-file (the most-churned file
# in the repo, the repeated merge-conflict source #181/#190): the handle_event router (PR / push / installation /
# repository / check_suite / check_run / marketplace routing) + its GitHub-post helpers (_post_refreshes /
# _post_watching_signal / _safe_upsert_check / the comment-marker builders), the content-free payload/path/id
# guards it shares (_as_obj / _as_list / _code_paths / _push_changed_sets / the branch- and change-id builders /
# _quota_result), the push-time lane reservation (reserve_branch_lanes), and the three event caps (_MAX_PR_FILES /
# _FAILING_CONCLUSIONS / _RERUN_PR_CAP) live in their OWN module (webhook_handlers.py) so this hotspot file is
# finer-grained. That module changes when a new event TYPE / its routing logic changes — a DIFFERENT reason than
# the HTTP/queue/worker plumbing that stays HERE (signature verify, bounded body read, the daemon worker +
# watchdog, serve()'s HTTP handler, the boot/backfill CLI). Like the modules above it imports NOTHING from
# server.py at LOAD time (no circular import — server.py imports IT). These names are RE-EXPORTED here for
# backward compatibility: serve() wires _branch_from_ref into the queue + the gates/tests reach the cluster via
# S.<name> (server.handle_event / server._code_paths / …), AND the lazy back-references in ingest.py /
# event_processor.py reach server._as_obj / server._as_list / server._push_changed_sets / server._push_author_is_bot
# / server._quota_result / server.handle_event through exactly these re-exports.
try:
    from webhook_handlers import (handle_event, reserve_branch_lanes,  # noqa: E402,F401
                                  _as_obj, _as_list, _change_id, _branch_from_ref, _branch_change_id,
                                  _branch_claim_id, _quota_result, _comment_marker, _marked_comment,
                                  _event_installation_id, _marketplace_plan_name, _safe_upsert_check,
                                  _post_watching_signal, _post_refreshes, _default_branch_for,
                                  _code_paths, _push_changed_sets, _push_author_is_bot, _pr_number_from_change,
                                  _validated_installation_proof, _activation_installation_proof,
                                  _current_account_installation_proof,
                                  _ACTIVATION_PROOF_MARKER, _SUSPEND_PROOF_MARKER,
                                  _STALE_UNINSTALL_MARKER, _ALLREPOS_DISCOVERY_MARKER)
except ImportError:  # imported as a package
    from .webhook_handlers import (handle_event, reserve_branch_lanes,  # noqa: E402,F401
                                   _as_obj, _as_list, _change_id, _branch_from_ref, _branch_change_id,
                                   _branch_claim_id, _quota_result, _comment_marker, _marked_comment,
                                   _event_installation_id, _marketplace_plan_name, _safe_upsert_check,
                                   _post_watching_signal, _post_refreshes, _default_branch_for,
                                   _code_paths, _push_changed_sets, _push_author_is_bot, _pr_number_from_change,
                                   _validated_installation_proof, _activation_installation_proof,
                                   _current_account_installation_proof,
                                   _ACTIVATION_PROOF_MARKER, _SUSPEND_PROOF_MARKER,
                                   _STALE_UNINSTALL_MARKER, _ALLREPOS_DISCOVERY_MARKER)

# The WEBHOOK REQUEST-INGRESS layer — the first, security-critical line of the live HTTP path (HMAC signature
# verify + the bounded-body DoS guard: verify_signature / _MAX_BODY_BYTES / _checked_content_length /
# read_bounded_body) — lives in its OWN module (webhook_ingress.py) so this hotspot file is finer-grained. It
# changes for a DIFFERENT reason than the daemon worker / watchdog / serve() wiring / boot CLI that stay HERE: it
# changes when the request-ingress / DoS / signature-verification rules change. Like the modules above it imports
# NOTHING from server.py at LOAD time (no circular import — server.py imports IT; its only dep is env_int, pulled
# straight from env_config.py). These names are RE-EXPORTED here for backward compatibility: serve()'s do_POST
# calls read_bounded_body + verify_signature, and the gates/tests reach them via S.<name> (S.verify_signature /
# S.read_bounded_body / S._checked_content_length).
try:
    from webhook_ingress import (verify_signature, read_bounded_body,  # noqa: E402,F401
                                 _checked_content_length, _MAX_BODY_BYTES)
except ImportError:  # imported as a package
    from .webhook_ingress import (verify_signature, read_bounded_body,  # noqa: E402,F401
                                  _checked_content_length, _MAX_BODY_BYTES)

# The serve() function is now a THIN composition root over two split modules so this hotspot file is finer-grained
# (serve() was a 245-line function inlining the env/config read, the runtime wiring, AND the HTTP Handler class).
#   server_boot.py  — load_config() (read VERIPSA_DSN/GH_*/every VERIPSA_* knob + build GitHubREST) and
#                     wire_runtime() (store + processor + worker + boot-reconcile + recovery loop + watchdog).
#   server_http.py  — make_handler() (the do_POST/do_GET request-routing Handler CLASS + its probe helpers).
# Both import NOTHING from server.py at LOAD time (no circular import — server.py imports THEM); the server-resident
# seams they need at runtime (make_db_processor / _event_account_key / _event_repo / _branch_from_ref /
# boot_reconcile / graph_freshness_all / read_bounded_body / verify_signature / health_snapshot / app_identity_ok /
# the DeliveryStore + EventQueue + run_watchdog + start_recovery_loop collaborators) are resolved at CALL time off
# the `server` module via the same lazy `_server()` idiom event_processor.py uses — so every monkeypatch seam the
# tests/operator set on `server` stays live. The EMPTY-SECRET STARTUP GUARD deliberately stays INLINE in serve()
# below (the perimeter gate pins GH_WEBHOOK_SECRET/SystemExit/VERIPSA_ALLOW_UNSIGNED to inspect.getsource(serve)),
# as do the ThreadingHTTPServer bind + the `timeout = _req_timeout` per-request socket timeout (the no-hang gate
# pins those two substrings to server.py — the structural resilience guards).
try:
    from server_boot import load_config, wire_runtime  # noqa: E402,F401
except ImportError:  # imported as a package
    from .server_boot import load_config, wire_runtime  # noqa: E402,F401
try:
    from server_http import make_handler  # noqa: E402,F401
except ImportError:  # imported as a package
    from .server_http import make_handler  # noqa: E402,F401

# THE EVENT-HANDLING CAPS stay DEFINED HERE on server.py (NOT moved to webhook_handlers.py) — by the codebase's
# monkeypatch CONTRACT: tests rebind `server._MAX_PR_FILES` / `server._RERUN_PR_CAP` AT RUNTIME (the per-PR
# file-cap + rerun-bound gates in tests/test_server.py do `S._MAX_PR_FILES = 3` / `S._RERUN_PR_CAP = 1`) and
# expect handle_event / reserve_branch_lanes to read the NEW value. webhook_handlers.handle_event /
# reserve_branch_lanes therefore read these at CALL time off the `server` module (server._MAX_PR_FILES, …) — NOT
# at import — so a patch is seen, exactly as before the split. (This is the same lazy-`import server` seam idiom
# ingest.py / event_processor.py use to reach server-resident state.) _MAX_BODY_BYTES already lives here too.

# A single PR reserves one claim PER changed file. A mega-PR (a sweeping refactor / generated-code PR touching
# thousands of files) would otherwise create thousands of claims per event. Bound it: reserve + analyze the
# first N files (the contention signal is the same — overlap shows in the first files too), flag truncation.
# A POSITIVE cap (a 0/negative would silently disable the path). env_int refuses a non-int or <1 LOUDLY at
# start (names the var) instead of crashing bare or misbehaving silently.
_MAX_PR_FILES = env_int("VERIPSA_MAX_PR_FILES", 500, min_value=1)

# A check_suite / check_run `completed` event with one of these conclusions means the PR's checks did NOT pass —
# the PR is RED. We record it (notify-only) so stuck_prs_surface can tell a human. `neutral`/`success`/`skipped`
# are NOT failures (no record). (`stale` means GitHub superseded the run — not an actionable red, so excluded.)
_FAILING_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "action_required"})

# RE-RUN cap: a single check_suite/check_run `rerequested` (the customer pressed "Re-run all checks" in the merge
# box, or "Re-run" on the Veripsa check) names the PRs associated with that commit in `pull_requests[]`. A commit
# is almost always the head of one PR, but a shared commit can sit on several — bound how many we re-analyze per
# rerequest event so one button-press can never fan out into an unbounded API storm. The rest are NOT lost: each
# is re-analyzed the next time it receives a normal pull_request webhook (or a `backfill`).
_RERUN_PR_CAP = env_int("VERIPSA_RERUN_PR_CAP", 20, min_value=1)

# NEIGHBOR-REFRESH cap: EVERY push-to-main (refresh_inflight) AND every PR open/sync re-renders the verdict for
# EVERY in-flight change in core.main_impact_surface and POSTS it (_post_refreshes makes up to 3 GitHub API calls
# per non-clear neighbor — head fetch + check upsert + comment upsert) — ALL while the per-(account,repo) advisory
# lock is HELD (the lock spans the whole event incl. its GitHub calls). On a busy monorepo (100s–1000s of
# overlapping open PRs all touching a shared foundation), ONE push to main fanned this out to hundreds of posts
# under a single held lock → that repo's subsequent events serialized behind a multi-minute post storm (observed
# live: 3 rapid merges churned the worker). The merge_group / check-rerun replay paths were already bounded by
# _RERUN_PR_CAP; this is the matching bound for the LIVE push-to-main + open/sync neighbor refresh — _post_refreshes
# posts AT MOST this many neighbors per event, so one push can't fan out into an unbounded post storm. The rest are
# NOT lost: each in-flight PR re-renders on its OWN next webhook (push/sync) or a `backfill`. main_impact_surface
# returns `changes` ORDER BY label (NOT materiality), so this is a deterministic bound (not a "top-materiality"
# slice) + a content-free truncation log. A POSITIVE cap (a 0/negative would silently disable the refresh); env_int
# refuses a non-int or <1 LOUDLY at start (names the var). Read off `server` at call time so the test patch is seen.
_NEIGHBOR_REFRESH_CAP = env_int("VERIPSA_NEIGHBOR_REFRESH_CAP", 30, min_value=1)


# NOTE: _make_db (the connect-per-query runner for one-off startup work like the boot self-heal + the __main__
# backfill CLI) was EXTRACTED to server_dbops.py, where _scoped_db's sibling lives — pure DB plumbing, no module
# state, no test monkeypatch contract. Re-exported above so callers + server_boot._server()._make_db(dsn) reach it
# unchanged. _scoped_db STAYS HERE below: test_external_resilience / test_suspend_handler / test_pauseack_* /
# test_tamper_account_authority monkeypatch server._scoped_db to inject mid-event connection drops; moving it would
# change which module those tests must patch (out of behavior-preserving scope).


def _scoped_db(conn):
    """A db runner bound to ONE already-open (autocommit) connection — so a whole webhook event runs over a
    single connection instead of opening/closing one per query (the cheap managed Postgres would otherwise
    exhaust its connection limit at ~17 connections/event)."""
    def run(sql, args=()):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    return run


# NOTE: health_snapshot / db_usage_sample / watchdog_tick / run_watchdog were EXTRACTED to health_watchdog.py,
# and the live PER-EVENT PROCESSOR cluster (_event_repo / _event_account_key / _install_fanout_repos /
# _process_install_event_per_repo_locked / make_db_processor / app_identity_ok / _READYZ_INSTALLATION) was
# EXTRACTED to event_processor.py — both imported + re-exported at the top of this file (unchanged), to cut this
# hotspot file's out-degree. serve()'s handler + __main__ + the server gate reach them via those re-exports.


def serve(port: int = 8000):
    """Run the live HTTP receiver (reads config from env). The PO-gated last mile.

    THIN COMPOSITION ROOT (the body was a 245-line function): (1) read config (server_boot.load_config) →
    (2) the inline empty-secret STARTUP GUARD (kept here — the perimeter gate pins it to serve()'s own source) →
    (3) wire the runtime: store + processor + worker + boot-reconcile + recovery loop + watchdog
    (server_boot.wire_runtime) → (4) build the request Handler (server_http.make_handler) → (5) bind + run it."""
    from http.server import ThreadingHTTPServer
    # OWNER_DSN is a preDeploy-only credential.  The Docker entrypoint removes
    # it before exec so it cannot appear even in /proc/<pid>/environ; keep this
    # independent refusal so a future command override cannot silently put a
    # database-owner bypass inside the public webhook runtime.
    if os.environ.get("OWNER_DSN"):
        raise SystemExit(
            "FATAL: OWNER_DSN is preDeploy-only and must not exist in the web runtime environment"
        )
    cfg = load_config()
    secret = cfg.secret
    # SECURITY STARTUP GUARD (edge#5): the webhook endpoint is PUBLIC once deployed, and verify_signature treats an
    # EMPTY secret as "unsigned dev mode" (returns True for everything) — so a public deploy with no secret would
    # ACCEPT ALL FORGERIES. Refuse to start a server with no signing secret. The unsigned dev mode stays available
    # ONLY when explicitly opted into (VERIPSA_ALLOW_UNSIGNED=1) for local smee.io trials — never by accident.
    # (Kept INLINE in serve() — NOT pushed into load_config — because the perimeter gate asserts this fail-closed
    # refusal from inspect.getsource(server.serve): GH_WEBHOOK_SECRET / SystemExit / VERIPSA_ALLOW_UNSIGNED must
    # appear in serve()'s OWN source, and it is the refusal test_app_deploy_resolution.py's S.serve(0) exercises.)
    if not secret and os.environ.get("VERIPSA_ALLOW_UNSIGNED", "") != "1":
        raise SystemExit(
            "FATAL: GH_WEBHOOK_SECRET is empty — refusing to start. Without it the PUBLIC webhook accepts ALL "
            "forged deliveries (verify_signature returns True for an empty secret). Set GH_WEBHOOK_SECRET to the "
            "value configured in the GitHub App. (Local-only unsigned trials: set VERIPSA_ALLOW_UNSIGNED=1.)"
        )

    # WIRE THE RUNTIME around the proven brain (server_boot.wire_runtime, unchanged order): the boot-reconcile
    # thread, the durable inbox store + the per-event processor, the in-process worker fed from the store, the
    # delivery recovery loop, and the proactive watchdog. Returns the in-process scheduler the handler submits to
    # + the durable store do_POST persists to before its 202.
    rt = wire_runtime(cfg)
    worker, store = rt.worker, rt.store

    # A per-request socket timeout: a slow/incomplete client (a half-sent body, a slowloris) can otherwise wedge
    # a request handler forever. BaseHTTPRequestHandler.timeout sets the connection's socket timeout, so a stalled
    # read raises socket.timeout and the handler unwinds instead of hanging. Bounds the read_bounded_body read too.
    _req_timeout = env_int("VERIPSA_REQUEST_TIMEOUT", 15, min_value=1)

    # BUILD the request Handler (server_http.make_handler: the do_POST/do_GET routing + probe helpers), passing the
    # wired deps (the SAME values the old inline closure saw). Subclass ONLY to stamp the per-request socket
    # timeout — `timeout = _req_timeout` stays HERE textually (the no-hang gate pins it to server.py).
    _BaseHandler = make_handler(secret=secret, store=store, worker=worker,
                                db=cfg.db, dsn=cfg.dsn, gh=cfg.gh,
                                persist_all=rt.persist_all)

    class Handler(_BaseHandler):
        timeout = _req_timeout

    print(f"Veripsa webhook server on :{port} "
          f"(events: pull_request, push, repository, check_suite, check_run, merge_group) "
          f"— ack-fast + background worker")
    # THREADING (not a single-threaded HTTPServer): one slow/incomplete request must NEVER block every other
    # request. A single-threaded server serves one connection at a time, so a stalled webhook POST also starves
    # the /healthz probe → the platform marks the container unhealthy and RESTARTS it (a restart loop). With a
    # thread-per-request server, /healthz keeps answering while any one POST is in flight. Safe here: the handler
    # is ack-fast (verify → enqueue → respond), worker.submit is a thread-safe queue, and the per-event DB work
    # already runs under per-repo advisory locks (designed for scale-out / concurrency). Threads are daemon, so
    # they never block the graceful-shutdown drain below.
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True

    # GRACEFUL SHUTDOWN: on a deploy/rollout the platform sends SIGTERM before SIGKILL (Render gives a grace
    # period). Drain the in-flight queue so events already ACCEPTED finish their write, instead of being cut
    # mid-flight. With the durable inbox ON, anything still queued/in-flight at SIGKILL is NOT lost regardless:
    # its row is persisted (queued, or processing→reclaimed as stale) and the next boot's recovery loop replays it
    # — the drain just lets clean shutdowns finish promptly rather than waiting on recovery. (A 202'd delivery is
    # NOT redelivered by GitHub, so this durable replay — not GitHub — is what makes a deploy lossless.)
    import signal
    # Render's SIGTERM→SIGKILL grace is ~30s; the old 8s drain forfeited most of it, so an in-flight event that
    # merely needed 10-15s was cut and replayed (or, pre-expiry, stranded its lane). 20s lets the common case
    # finish CLEANLY while leaving headroom for the lease expiry + exit below. Validated knob: a typo refuses to
    # start (env_int contract) rather than silently draining for 0s.
    _drain_seconds = float(env_int("VERIPSA_SHUTDOWN_DRAIN_SECONDS", 20, min_value=1, max_value=25))
    def _graceful(signum, _frame):
        print(f"signal {signum}: draining in-flight events before exit", flush=True)
        try:
            drained = worker.wait_idle(timeout=_drain_seconds)
            # Drain timed out with the worker still mid-event: its durable row would stay 'processing' and block
            # its whole account/repo causal lane until the FULL stale window reclaims it (the queued=41 lane-freeze
            # incident). Expire just that lease so recovery reclaims it within the short shutdown grace instead.
            # The row stays 'processing' (never requeued here — the dying processor may still commit+finish, and
            # requeueing would race that commit into a double-processed delivery). Best-effort: on any error the
            # stale-window reclaim remains the backstop.
            if not drained and store is not None:
                try:
                    if store.expire_inflight_lease():
                        print("shutdown: drain timed out — in-flight durable lease expired early for recovery",
                              flush=True)
                except Exception as e:
                    print(f"shutdown lease expiry failed (stale-window recovery remains the backstop): "
                          f"{str(e)[:160]}", flush=True)
        finally:
            os._exit(0)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _graceful)
        except (ValueError, OSError):
            pass            # not the main thread / unsupported platform — best-effort
    httpd.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "backfill":
        pkey = os.environ.get("GH_PRIVATE_KEY", "")
        if pkey and os.path.exists(pkey):
            pkey = open(pkey).read()
        install_id = os.environ.get("GH_INSTALLATION_ID", "")
        gh = GitHubREST(os.environ.get("GH_APP_ID", ""), pkey, install_id)
        # TENANT PIN (the prod-recovery fix). Mirror the LIVE per-event processor (make_db_processor): the
        # backfill writes (record_push / act_for_claim / ingest_graph) all go through establish_session_write_
        # context → resolve_session_identity, which needs the connection PINNED to an existing account first. _make_db is
        # connect-per-query, so any pin on it would die with the query that set it → the very FIRST write hits a
        # clean prod (no pinned installation, no veripsa_app credential) and RAISES 42501, which backfill_repo
        # honestly swallows into an {ingest_error/prs_error} dict having written ZERO rows — the RUNBOOK's
        # documented `backfill <repo>` recovery was a silent NO-OP in prod. Fix: hold ONE connection, pin it
        # ONCE (autocommit, session-level so the pin survives every query), and run the whole backfill over THAT
        # scoped connection. The non-provisioning route maps the owner id to its existing live account and routes
        # every subsequent write into that tenant under RLS —
        # exactly what the webhook path does. (refresh_demo.sh / RUNBOOK already export GH_INSTALLATION_ID before
        # invoking this CLI, so the id is in hand.)
        import psycopg2
        conn = psycopg2.connect(os.environ["VERIPSA_DSN"])
        try:
            conn.autocommit = True   # session-level pin (must outlive each per-query cursor — like the live path)
            # TENANT KEY = the OWNING ACCOUNT id, NOT the ephemeral installation id (audit r5 — the dual-account
            # orphan). enter_installation_with_authority only PREFIXES its argument ('ACCT-GH-'||arg); it does NOT
            # map an installation id → its owner. The LIVE webhook path keys by repository.owner.id
            # (→ ACCT-GH-<owner_id>); the boot self-heal + co-change use gh.installation_account_id() (= owner id).
            # Passing the raw GH_INSTALLATION_ID here would pin ACCT-GH-<install_id> — a PHANTOM tenant the live
            # path never addresses — so refresh_demo.sh's `backfill` wrote the graph into an orphan account
            # (the prod de75fe23 split) while the live tenant stayed cold. Resolve the owner id the SAME way every
            # other path does. Empty GH_INSTALLATION_ID = the local/dogfood path → pin nothing (peer-auth role's
            # own account). A configured install whose owner can't be resolved → FAIL LOUD (never an orphan write).
            account_key = None
            if install_id:
                account_key = gh.installation_account_id()
                if not account_key:
                    raise SystemExit(
                        f"FATAL: backfill could not resolve the OWNING account for GH_INSTALLATION_ID={install_id!r} "
                        "(installation_account_id returned None — no repos visible / API error). Refusing to run: "
                        "pinning the raw install id would write into a PHANTOM ACCT-GH-<install_id> tenant the live "
                        "webhook never addresses (an orphan-account split). Check the App install + permissions."
                    )
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                if account_key:
                    cur.execute("SELECT core.enter_existing_installation_with_authority(%s)", (account_key,))
                    row = cur.fetchone()
                    pinned = row[0] if row else None   # the resolved account id; NULL = pin genuinely failed
                    if not pinned:
                        raise SystemExit(
                            f"FATAL: backfill could not pin the owning account {account_key!r}. Refusing to run — "
                            "an unpinned backfill writes ZERO rows in clean prod (every gate write raises 42501 and "
                            "is swallowed)."
                        )
            print(json.dumps(backfill_repo(_scoped_db(conn), gh, sys.argv[2]), indent=2))
        finally:
            conn.close()   # drops the session pin and returns the connection
    else:
        serve(env_int("PORT", 8000, min_value=1, max_value=65535))
