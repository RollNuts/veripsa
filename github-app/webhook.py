#!/usr/bin/env python3
"""Veripsa GitHub App — the webhook BRAIN (Part B core, deploy-free + testable).

Given a GitHub `pull_request` webhook event, this drives the gate end-to-end:
  opened/synchronize → reserve the PR's changed paths against the protected branch (a claim per file,
                       claim_id namespaced by the PR number) → predict via core.main_impact_surface →
                       render the PR check + comment (render.py).
  closed (merged)    → core.land_change_on_main_with_authority (record the landing, release this PR's lanes,
                       promote the waiters) → return the OTHER still-in-flight PRs whose comment should
                       refresh (their neighborhood just changed).
  closed (no merge)  → core.release_change_on_main_with_authority (release this PR's lanes + promote the
                       waiters, but record NO landing — nothing reached main) → same neighborhood refresh.
                       Without this, an abandoned blocker strands its queue until the lease expires.

PURE over an injected `db(sql, args) -> scalar_text` runner that is authed as the acting seat. The hosted
server (the PO-gated last mile: a small web process + the GitHub App registration + a webhook secret)
supplies `db` and posts the returned check/comment through the GitHub REST API. Nothing here needs deploy.

HONEST Part-B boundary (the moat's identity model): declare_claim attributes the reservation to the
CONNECTING ROLE's agent. So the hosted App needs a DELEGATION path — one service identity reserving on
behalf of each PR author (an account-scoped "act-for-agent" gate, a sibling of `grant`). Until that exists,
this brain is driven per-author in the trial (each author's own seat). The VALUE loop is identical.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

# The gate's claim_id cap (db/schema/30_gate.sql derives change_id = the first ':'-segment of claim_id, capped at
# 200, and the core.claim PK is (account_id, repo, claim_id)). Keep this in lockstep with that cap.
_CLAIM_ID_CAP = 200
# The disambiguating digest width appended to an over-cap claim_id (see _bounded_claim_id). Fixed width so the
# room reserved for it is constant.
_CLAIM_HASH_HEX = 16
# The change_id cap. A change_id is used DIRECTLY as a key on the release/landing path (server._branch_change_id
# → release_change_on_main_with_authority) AND as the prefix the gate splits out of a claim_id — so BOTH paths
# must produce the SAME bounded change_id. We cap it BELOW _CLAIM_ID_CAP, reserving permanent room for the
# ':' + '#' + the digest, so an over-cap path can always append the disambiguating hash WITHOUT ever having to
# shorten the change_id (which would split one work-unit into two keys). The gate's own left(...,200) is a no-op
# on an already-<=180 change_id, so the two stay identical.
_CHANGE_ID_CAP = _CLAIM_ID_CAP - 2 - _CLAIM_HASH_HEX  # 200 - 2('#'/':') - 16 = 182

try:
    from render import render_pr_check, coverage_nudge_line, stale_base_nudge_line
except ImportError:  # imported as a package
    from .render import render_pr_check, coverage_nudge_line, stale_base_nudge_line

try:
    from _compat_analysis import compat_shadow_enabled, run_compat_shadow
except ImportError:  # imported as a package
    from ._compat_analysis import compat_shadow_enabled, run_compat_shadow


def _branch_inventory_unknown_check(branch: str) -> dict:
    """Resolve the new customer copy lazily so narrow legacy render stubs remain import-compatible."""
    try:
        import render as _render
    except ImportError:
        from . import render as _render  # type: ignore
    work = getattr(_render, "branch_inventory_unknown_check", None)
    if callable(work):
        return work(branch)
    return {"conclusion": "neutral", "title": "Veripsa — branch state not verified, retrying",
            "summary": "GitHub branch state could not be verified. Veripsa will retry automatically; "
                       "no acknowledgement is needed."}


def _branch_inventory_unknown_note() -> str:
    """Resolve the additive mixed-cluster note without widening the module-load render seam."""
    try:
        import render as _render
    except ImportError:
        from . import render as _render  # type: ignore
    work = getattr(_render, "branch_inventory_unknown_note", None)
    if callable(work):
        return work()
    return ("**Branch verification pending:** Veripsa will retry automatically; no acknowledgement or manual "
            "branch operation is needed for the unverified branch.")


# PER-EVENT TRACE-ID (Round-2 observability follow-up): every per-event log line in the webhook → ingest → render
# hot path carries a content-free trace_id so an on-call can grep ONE event's complete trail end-to-end. The id is
# an opaque uuid4 (never derived from PII / payload content / shas) minted ONCE at handle_event entry and stashed
# on the event payload as `_veripsa_trace_id` (an internal underscore-prefixed key, the same idiom delivery_queue
# uses for `_veripsa_delivery_key`). Downstream helpers read it via _trace_of(payload) / _trace_log_prefix(payload)
# — a missing trace_id is a degraded but valid signal (older code paths / unit tests that bypass handle_event still
# log cleanly, just without the prefix). The prefix is a stable, parseable token so a future log-pipeline shipper
# can lift it out without parsing the rest of the human-readable message. Content-free: opaque random bytes only.
_TRACE_KEY = "_veripsa_trace_id"
# Short tag width keeps the prefix compact in the log (an on-call only needs the first ~12 hex chars to disambiguate
# a few hundred concurrent in-flight events — the full uuid4 is still on the payload for callers who want it).
_TRACE_TAG_WIDTH = 12
# Pull request actions that should recompute the in-flight lane state and refresh the GitHub check/comment. Draft
# PRs are scout windows, so both opening as draft and later converting an existing PR to draft must run the same
# content-free analysis path.
_PR_ANALYZE_ACTIONS = ("opened", "synchronize", "reopened", "ready_for_review", "converted_to_draft")
# Private, content-free proof consumed only by merge-group replay.  A posted Check is not itself evidence that
# the graph produced a verdict (quota/branch-unknown/no-reservation paths also post Checks), so the normal brain
# return carries the exact surface identity + structural verdict separately.
_ANALYSIS_VERDICT_KEY = "_veripsa_analysis_verdict"
_ANALYSIS_VERDICTS = frozenset(("clear", "warn", "serialize_soft", "serialize", "unknown"))


def _analysis_verdict_proof(impact, change_id: str, repo: str, branch: str, event: dict) -> dict | None:
    """Return an exact acting-row verdict proof, or None when the rendered Check is not a graph verdict.

    Keep the proof stricter than the customer-facing Check: the latter deliberately has useful success/neutral
    fallbacks for no reservation and temporary uncertainty.  Merge queue authority must never infer from those
    strings.  Unknown stays explicit so callers can distinguish it from a missing proof, but neither may earn
    Clear.  The two render-time honesty downgrades that can turn a raw non-unknown row into Unknown are mirrored
    here (partial file inventory and uncorroborated hub dampening).  The Files snapshot and persisted row must
    also agree on the exact non-empty PR head; PR identity alone cannot bind an old verdict to a newer Check.
    """
    if (not isinstance(impact, dict)
            or impact.get("repo") != repo or impact.get("branch") != branch
            or not isinstance(event, dict) or bool(event.get("conflict_markers"))
            or event.get("head_snapshot_verified") is not True):
        return None
    head_sha = event.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        return None
    changes = impact.get("changes")
    if not isinstance(changes, list):
        return None
    rows = [c for c in changes if isinstance(c, dict) and c.get("change_id") == change_id]
    if len(rows) != 1:
        return None
    row = rows[0]
    if row.get("head_sha") != head_sha:
        return None
    verdict = row.get("verdict")
    if verdict not in _ANALYSIS_VERDICTS:
        return None
    dampened = row.get("dampened_with")
    if (isinstance(dampened, list)
            and any(isinstance(d, dict) and not d.get("corroborated") for d in dampened)):
        verdict = "unknown"
    if verdict == "clear" and bool(event.get("truncated_files")):
        verdict = "unknown"
    return {"repo": repo, "branch": branch, "change_id": change_id,
            "head_sha": head_sha, "verdict": verdict}


def mint_trace_id() -> str:
    """Mint a fresh opaque trace_id (uuid4 hex). Content-free: random bytes only, never derived from any payload
    field / PII / sha. Idempotent at the call site: a NEW id every call — the caller decides whether to reuse one
    already on the payload (see _ensure_trace_id)."""
    return uuid.uuid4().hex


def _ensure_trace_id(payload) -> str:
    """Read the event's trace_id from `payload[_TRACE_KEY]`; mint and STASH a fresh one if absent. Idempotent on a
    payload that already carries a trace_id (handle_event might be re-entered on a redelivery's payload that still
    has the previous attempt's id — but the normal path mints fresh per attempt because sanitize_payload drops the
    underscore-prefixed internal key on every persisted-then-replayed event). A non-dict payload (the poison-event
    short-circuit) gets a trace_id back but NOT stashed (the dispatcher would no-op anyway). NEVER raises."""
    if not isinstance(payload, dict):
        return mint_trace_id()
    existing = payload.get(_TRACE_KEY)
    if isinstance(existing, str) and existing:
        return existing
    tid = mint_trace_id()
    payload[_TRACE_KEY] = tid
    return tid


def _trace_of(payload) -> str:
    """Read the event's trace_id off the payload. Returns '' when absent (a degraded but valid log line — better a
    missing prefix than a crash on a code path that bypassed handle_event, e.g. a unit test or the boot backfill).
    NEVER raises."""
    if isinstance(payload, dict):
        v = payload.get(_TRACE_KEY)
        if isinstance(v, str) and v:
            return v
    return ""


def _trace_log_prefix(payload) -> str:
    """Build the log-line prefix carrying the event's trace_id, or '' when absent. Stable, parseable token shape
    (`trace_id=<12-hex-chars> `) — a future log-pipeline shipper can lift it out without parsing the rest of the
    message, and the human reader can grep one event end-to-end. Width-bounded (the full uuid stays on the payload
    for callers who want it via _trace_of)."""
    tid = _trace_of(payload)
    if not tid:
        return ""
    return f"trace_id={tid[:_TRACE_TAG_WIDTH]} "


# THE ANSWER-CHECK land-order extractor: a change's serialize_behind / suggested_order labels are humanized
# ("alice PR-12") — the LEDGER needs the bare content-free CHANGE REF ('PR-12' / 'BR-feature-x'). Pull every
# 'PR-<n>' / 'BR-<x>' token out of the labels (bounded ≤8, deduped). Content-free (a change ref is a number/
# branch slug, never code). Used to record what this change was told to land AFTER, so the close-time outcome
# can grade "followed vs ignored" against ledger truth.
_REF_RE = re.compile(r"\b(?:PR-\d+|BR-[A-Za-z0-9_./\-]+)")


def _behind_refs(labels) -> list:
    out, seen = [], set()
    for lab in (labels or []):
        if not isinstance(lab, str):
            continue
        for m in _REF_RE.findall(lab):
            if m not in seen:
                seen.add(m)
                out.append(m)
                if len(out) >= 8:
                    return out
    return out


# TODO (answer-check, two clearly-scoped follow-ups, intentionally NOT half-built here):
#  1) USER-REPORTED LABEL TIER (higher confidence): read a 👍/👎 reaction on Veripsa's OWN PR comment back as a
#     human-confirmed outcome label (a second, higher-confidence tier than the inferred/observed signals). This
#     needs the App to subscribe to the `reaction`/`issue_comment` events + a content-free reactions read on its
#     own comment id; the captured label would record an advice_outcome with confidence='user_reported'. Left as
#     a TODO (not started) rather than half-built — the capture path + the confidence vocabulary already admit it.
#  2) CROSS-PR REVERT LINKAGE: when a "Revert …" PR merges, flip the EARLIER merge it undoes to land='reverted'.
#     That is a cross-PR UPDATE of an already-recorded outcome, which would violate the append-only first-wins
#     ledger; doing it right needs a separate 'revert_of' fact + a surface that overlays it (not an UPDATE). v1
#     records the revert boolean on the reverting change only (server.py derives it content-free from the title /
#     head-ref shape). Both are additive; neither is started here.


def _bounded_claim_id(change_id: str, path: str) -> str:
    """Build claim_id = '<change_id>:<path>' bounded to the gate cap, INJECTIVELY in (change_id, path).

    THE TRUNCATION KEY-COLLISION (audit: adversarial-names). The old form `f"PR-{n}:{path}"[:200]` is a LOSSY
    suffix-cut: the gate accepts a target_path up to 1024 chars but claim_id is capped at 200, so two DIFFERENT
    long sibling paths under the SAME change that share a ≥~195-char prefix (e.g. 'src/<250 chars>/fileA.py' and
    '…/fileB.py') truncate to the SAME claim_id. The core.claim PK is (account_id, repo, claim_id), so the second
    path's INSERT raises an UNCAUGHT duplicate-key (claim_pkey) inside _place_claim's own unique_violation
    handler — exactly the crash the schema warns about for reopened PRs — which aborts the event's atomic txn:
    the PR coordinates NOTHING (its lanes roll back = false clear/miss) and every redelivery re-crashes
    deterministically (a poison event). A crafted path name thus suppresses Veripsa on a whole PR.

    FIX — keep the natural readable form when it fits (no behavior change, idempotency preserved for every claim
    that already fit), and when it would overflow, make the over-cap tail a deterministic, content-free hash of
    the FULL path so two distinct paths can NEVER share a claim_id. The change_id prefix (everything before the
    first ':') is always preserved intact, so the gate's split-on-first-':' still derives the right change_id and
    the whole change still groups under one work-unit. The hash is content-free (a digest of a path string — a
    file location, never code) and bounded.

    Invariant: distinct (change_id, path) → distinct claim_id, and len(result) <= _CLAIM_ID_CAP.

    Note the change_id can ITSELF be long (a deeply-nested 'BR-<feature/…>' head ref). The over-cap branch is
    INJECTIVE IN THE PATH regardless: the change_id is first bounded to _CHANGE_ID_CAP (which reserves permanent
    room for ':' + a fixed-width hash of the full path), then the hash is ALWAYS appended — never elided by a long
    change_id — so two distinct paths under one over-cap change can never share a key, and the change_id prefix is
    IDENTICAL for every path of a change (the same _CHANGE_ID_CAP server._branch_change_id applies, so the
    release-path key and the per-path claim keys agree). Grouping two genuinely-distinct branches that both
    truncate to the same _CHANGE_ID_CAP prefix is a separate, upstream concern (server._branch_change_id); a
    single change's own files never self-collide, which is the PK-crash this guards.)"""
    # Cap the change_id to _CHANGE_ID_CAP FIRST, identically on every path — so the prefix the gate splits out is
    # the SAME regardless of how long the path is (a long path must never shift which change_id a file groups
    # under). _branch_change_id already caps the branch to this, so for real callers this is a no-op.
    cid = (change_id or "")[:_CHANGE_ID_CAP]
    natural = f"{cid}:{path}"
    if len(natural) <= _CLAIM_ID_CAP:
        return natural                                    # common case: exact natural form, no churn, idempotent
    # Over cap → disambiguate with a fixed-width, content-free digest of the FULL path (sha256, _CLAIM_HASH_HEX
    # hex chars — collision-resistant for path-vs-path within one change). '#' marks the elision (cosmetic). The
    # hash is ALWAYS present and the change_id is NEVER shortened here, so injectivity in path holds and the
    # work-unit grouping is stable. _CHANGE_ID_CAP reserves exactly the room this tail needs.
    tail = "#" + hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()[:_CLAIM_HASH_HEX]
    head_budget = _CLAIM_ID_CAP - len(cid) - 1 - len(tail)  # leftover for a readable path head before the tail
    head = path[:head_budget] if head_budget > 0 else ""
    return f"{cid}:{head}{tail}"[:_CLAIM_ID_CAP]


def _claim_id(pr_number, path: str) -> str:
    return _bounded_claim_id(f"PR-{pr_number}", path)


def _json(db, sql, args=()):
    r = db(sql, args)
    if r is None:
        return None
    if isinstance(r, (dict, list)):  # psycopg2 already adapts jsonb → dict/list
        return r
    return json.loads(r)


# SAVEPOINT ISOLATION for an OPTIONAL best-effort DB surface in the shared per-event transaction.
# The whole webhook event runs in ONE transaction (event_processor.make_db_processor: conn.autocommit=False,
# one commit at the end). A bare `try/except` around an optional DB read (co-change / coverage nudge /
# prediction telemetry) catches the Python exception but does NOT clear Postgres's ABORTED-transaction state —
# so EVERY later statement in the same event, including the CUSTOMER-FACING pause-ack overlay + check/comment
# (the product output), then fails with "current transaction is aborted ..." and is silently skipped. Observed
# LIVE in prod: a least-privilege `permission denied for table co_change` aborted the txn → the pause vanished.
# Wrapping each optional surface in a SAVEPOINT means its failure rolls back ONLY itself; the transaction stays
# usable for the pause-ack overlay. This is the standard Postgres pattern for a best-effort step in a shared txn
# (the same idiom ingest.py already uses around the incremental-ingest attempt). The runner is the same
# `db(sql, args)` callable: SAVEPOINT / RELEASE / ROLLBACK TO are plain statements on the shared connection.
# Returns the work's result on success, or `default` if the optional work raised (its partial effect rolled
# back). NEVER raises — an optional surface must never break the event. If the savepoint DDL itself can't run
# (e.g. an autocommit test connection with no open txn), it falls through and the inner try still contains the
# error (best-effort, like ingest.py's _sp fallback).
def _optional(db, label, work, *, default=None, repo="", pr="", trace_id=""):
    """Run `work()` (a thunk doing best-effort DB reads) inside its OWN savepoint so a failure rolls back ONLY
    itself and can NEVER abort the shared per-event transaction the pause-ack overlay + check/comment need.

    TWO transaction models, ONE helper:
      • LIVE (event_processor._scoped_db): every db() statement runs on ONE shared non-autocommit connection.
        SAVEPOINT / RELEASE / ROLLBACK TO are real and contain `work`'s effect to its own savepoint — so a
        permission-deny inside `work` leaves the txn CLEAN for the pause-ack overlay (the regression fixed here).
      • TEST harness (tests/_server_harness.make_db): each db() opens a FRESH autocommit connection. A SAVEPOINT
        there is a no-op across connections (the next statement is a new backend) — so the savepoint DDL is
        best-effort and, crucially, a RELEASE/ROLLBACK failure must NEVER discard a SUCCESSFUL `work` result.
    So: the savepoint set / release / rollback are each independently best-effort (swallowed), and `work`'s
    success or failure is decided ONLY by `work` itself — exactly the resilience an optional surface needs.
    NEVER raises (an optional surface must never break the event); returns `work`'s result, or `default` on a
    `work` failure (its partial effect rolled back to the savepoint)."""
    sp = "vp_opt"
    have_sp = _txn_cmd(db, "SAVEPOINT %s" % sp)        # True iff the savepoint was really established on a shared txn
    try:
        out = work()
    except Exception as e:
        if have_sp:
            _txn_cmd(db, "ROLLBACK TO SAVEPOINT %s" % sp)   # clear any aborted state → the shared txn is clean for the next statement
        _tp = f"trace_id={trace_id[:_TRACE_TAG_WIDTH]} " if trace_id else ""
        print(f"{_tp}{label} skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
        return default
    if have_sp:
        _txn_cmd(db, "RELEASE SAVEPOINT %s" % sp)       # work succeeded → release; a release error must NOT discard `out`
    return out


# Run a transaction-control statement (SAVEPOINT / RELEASE / ROLLBACK TO) through the injected `db(sql, args)`
# runner, which is shaped for SELECTs: it does cur.execute THEN cur.fetchone(), and fetchone() RAISES
# "no results to fetch" on a no-row statement like SAVEPOINT — even though the statement ITSELF succeeded. So we
# must treat that specific post-execute fetch error as SUCCESS (the savepoint WAS set), and only a REAL execute
# failure (a per-connection autocommit test runner where SAVEPOINT can't apply, or no open txn) as "not set".
# Returns True iff the control statement took effect on a shared transaction (so the caller knows it can later
# ROLLBACK TO / RELEASE it). NEVER raises.
def _NO_RESULT_MARKERS():
    return ("no results to fetch", "no result")
def _txn_cmd(db, sql) -> bool:
    try:
        db(sql)
        return True
    except Exception as e:
        # The execute succeeded but the SELECT-shaped runner's fetchone() found no row → the statement applied.
        if any(m in str(e).lower() for m in _NO_RESULT_MARKERS()):
            return True
        return False                                    # a genuine failure (e.g. autocommit per-connection runner) → no savepoint


def _change_of(pr) -> str:
    """A PR is ONE change (bundle of files). claim_id = 'PR-<n>:<path>' → the gate derives change_id='PR-<n>'
    (split on ':'), so the whole PR groups under one change. We render / land BY this change_id, never by the
    author name (one author can have several open PRs — agent name is ambiguous)."""
    return f"PR-{pr}"


def _refresh_changes(impact: dict, exclude_change: str | None = None) -> list:
    """Render the refresh payload for EVERY in-flight change in `impact`. Shared by the merge/withdraw refresh,
    the push-to-main refresh, AND the open/sync neighbor refresh. A change that still has something to coordinate
    (warn/serialize/unknown) is emitted with its full re-rendered comment. A change that comes back 'clear' is
    emitted as a `clear_reset` entry: the server corrects its check + comment back to green ONLY IF it previously
    had a verdict (an always-clear PR has no marker comment → the server touches nothing → no spam, no extra cost).
    `exclude_change` drops one change_id — the OPEN/SYNC caller already posts the ACTING PR's own check+comment
    directly, so it must not also re-post it via the refresh path (one PR, two writes). Returns the
    [{agent,change,conclusion,title,summary,comment,fork_title?,fork_summary?,fork_comment?,clear_reset?}] list
    the server posts (the `title` is render_pr_check's rich per-verdict title, threaded so the refresh path posts
    the PR's REAL verdict title instead of a degraded bare "Veripsa")."""
    out = []
    for c in impact.get("changes", []) or []:
        if exclude_change is not None and c["change_id"] == exclude_change:
            continue
        # FORK INFO-LEAK GUARD (the NEIGHBOR path — audit r3): a fork PR that is an in-flight NEIGHBOR (not the
        # acting PR) ALSO gets a refresh comment posted on ITS conversation, which the EXTERNAL contributor reads —
        # so it must be REDACTED of the base repo's other in-flight PR identities / paths, exactly like the
        # acting-PR path. The engine/impact has NO fork concept (content-free), so we render BOTH the full comment
        # AND a redacted `fork_*` variant here; the POST layer (_post_refreshes, which resolves each neighbor's
        # fork status from GitHub) swaps to the redacted variant for a fork neighbor. (If a caller PRE-annotates a
        # change's is_fork, the full render is redacted too — harmless belt.) The clear_reset comment is already
        # generic (no other-PR identifiers), so it needs no fork variant.
        is_fork = bool(c.get("is_fork"))
        o = render_pr_check(impact, c["change_id"], is_fork=is_fork)
        if o.get("comment") is None:
            # 'clear' (or shared-foundation-only that came back clean). This PR has nothing to (re)coordinate.
            # It may have been ALWAYS clear (a green check, no comment → leave it; re-posting is spam) OR it may
            # have JUST DROPPED from warn/serialize to clear because its blocker withdrew/landed or the coupling
            # cleared — in which case its STALE neutral check + verdict comment must be corrected back to green.
            # We cannot tell from the surface alone which it is, so we emit a clear_reset entry: the server resets
            # the check to green and PATCHES the verdict comment ONLY IF one already exists (proof it was
            # previously non-clear). An always-clear PR has no marker comment → the server's patch_if_exists is a
            # no-op → still no spam (the less-noise rule holds). content-free.
            out.append({"agent": c.get("label") or c.get("agent"), "change": c["change_id"],
                        "conclusion": o["conclusion"], "title": o["title"], "summary": o["summary"], "comment": None,
                        "clear_reset": True})
            continue
        of = o if is_fork else render_pr_check(impact, c["change_id"], is_fork=True)   # redacted variant for a fork neighbor
        # CARRY THE RENDERED TITLE (title non-determinism fix). render_pr_check produces a rich
        # per-verdict title ("Veripsa — Unknown" / "… — Wait in line" / …); the refresh payload used to discard it,
        # so the POST layer (_post_refreshes) fell back to a hard-coded bare "Veripsa" — meaning whenever a SIBLING
        # PR moved and re-rendered an in-flight neighbor, that neighbor's title silently DEGRADED. The summary
        # already round-trips through this payload; the title must too (and the redacted fork title alongside it),
        # so the same PR always shows its real verdict title regardless of which event last rendered it.
        out.append({"agent": c.get("label") or c.get("agent"), "change": c["change_id"],
                    "conclusion": o["conclusion"], "title": o["title"], "summary": o["summary"], "comment": o["comment"],
                    "fork_title": of["title"], "fork_summary": of["summary"], "fork_comment": of["comment"]})
    return out


def _branch_cluster_change_ids(impact: dict) -> set[str]:
    """PR ids whose structural cluster contains at least one BR-* participant."""
    out: set[str] = set()
    if not isinstance(impact, dict):
        return out
    for raw in impact.get("clusters") or []:
        cluster = raw if isinstance(raw, dict) else {}
        changes = [change for change in (cluster.get("changes") or []) if isinstance(change, str)]
        if any(change.startswith("BR-") for change in changes):
            # A lone pre-PR branch lane has no customer PR surface to refresh and must not spend one branches API
            # read on every main push/close. Reconcile only when a PR row could actually be re-posted from it.
            out.update(change for change in changes if change.startswith("PR-"))
    return out


def _refreshes_with_branch_authority(db, repo: str, branch: str, impact: dict,
                                     reconcile_branch_claims=None, trace_id: str = "") -> tuple[dict, list, dict | None]:
    """Reconcile repo-wide BR truth before a global refresh, or suppress every unverified BR cluster.

    Merge/withdraw and protected-branch pushes refresh the whole repo rather than one acting cluster. They must
    therefore obtain one complete repo inventory before re-rendering any BR-containing cluster. On uncertainty the
    DB is untouched and those clusters keep their last authoritative GitHub surface; independent PR-only clusters
    still refresh normally.
    """
    blocked = _branch_cluster_change_ids(impact)
    reconciled = None
    authoritative = False
    if blocked and callable(reconcile_branch_claims):
        unavailable = object()
        raw = _optional(db, "global branch-claim reconcile", reconcile_branch_claims, default=unavailable,
                        repo=repo, trace_id=trace_id)
        if isinstance(raw, dict):
            reconciled = raw
            authoritative = raw.get("reconciled") is True
        if authoritative and isinstance(raw.get("released_changes"), list) and raw.get("released_changes"):
            impact = _json(db, "SELECT core.main_impact_surface(%s,%s)", (repo, branch)) or {}
        blocked = set() if authoritative else _branch_cluster_change_ids(impact)
    refreshes = _refresh_changes(impact)
    if blocked:
        refreshes = [row for row in refreshes if row.get("change") not in blocked]
    return impact, refreshes, reconciled


def refresh_inflight(db, repo: str, branch: str = "main", reconcile_branch_claims=None,
                     trace_id: str = "") -> dict:
    """STALE-VERDICT FIX (a push to main re-ingests main's graph → every in-flight PR's blast radius/coupling
    can change). The merge/withdraw paths already refresh the neighborhood, but a DIRECT push to main (a
    hotfix pushed straight to the protected branch, an admin commit, a squash that arrives only as a push)
    re-ingests the graph WITHOUT touching any open PR — so an in-flight PR keeps showing a STALE verdict (e.g.
    still 'clear' while its foundation just shifted under it) until its author happens to push again, which may
    be hours/days/never. That is exactly the missed-coupling we sell against. So after a push lands on main,
    recompute main_impact_surface and return the refresh payload for every in-flight change that now has
    something to coordinate. The server posts these (idempotent upsert — it PATCHES each PR's existing check +
    marker comment, so a re-render of an UNCHANGED verdict rewrites the same body, never a new comment = no
    spam). Content-free; the same render the live event uses. Returns {repo, branch, refreshed:[...]}."""
    impact = _json(db, "SELECT core.main_impact_surface(%s,%s)", (repo, branch)) or {}
    _impact, refreshes, reconciled = _refreshes_with_branch_authority(
        db, repo, branch, impact, reconcile_branch_claims=reconcile_branch_claims, trace_id=trace_id)
    result = {"repo": repo, "branch": branch, "refreshed": refreshes}
    if reconciled is not None:
        result["branch_claims_reconciled"] = reconciled
    return result


def _cluster_change_ids(impact: dict, change_id: str) -> list[str]:
    """Every member in ``change_id``'s structural contention cluster, bounded and content-free."""
    if not isinstance(impact, dict):
        return []
    for raw in impact.get("clusters") or []:
        cluster = raw if isinstance(raw, dict) else {}
        changes = cluster.get("changes") if isinstance(cluster.get("changes"), list) else []
        if change_id not in changes:
            continue
        return sorted({c for c in changes if isinstance(c, str)})[:64]
    return []


def _cluster_branch_changes(impact: dict, change_id: str) -> list[str]:
    """The BR-* members in ``change_id``'s structural contention cluster, bounded and content-free."""
    return [change for change in _cluster_change_ids(impact, change_id) if change.startswith("BR-")][:32]


def _impact_without_branch_changes(db, repo: str, branch: str, branch_changes: list[str],
                                   trace_id: str = "") -> dict | None:
    """Read the exact PR-only impact while preserving uncertain BR rows.

    Under the live shared transaction, release only the unverified BR members inside a savepoint, read
    ``main_impact_surface``, then ALWAYS roll the savepoint back. This preserves genuine PR↔PR collisions in a
    mixed cluster without committing a release that GitHub branch truth did not authorize. A runner that cannot
    establish a real savepoint performs no mutation and returns ``None`` (honest unknown).
    """
    sp = "vp_br_view"
    if not _txn_cmd(db, f"SAVEPOINT {sp}"):
        return None
    try:
        for change in branch_changes[:32]:
            db("SELECT core.release_change_on_main_with_authority(%s,%s,%s)", (change, repo, branch))
        impact = _json(db, "SELECT core.main_impact_surface(%s,%s)", (repo, branch))
    except Exception as e:
        rolled_back = _txn_cmd(db, f"ROLLBACK TO SAVEPOINT {sp}")
        _txn_cmd(db, f"RELEASE SAVEPOINT {sp}")
        if not rolled_back:
            raise RuntimeError("could not roll back speculative branch view") from e
        _tp = f"trace_id={trace_id[:_TRACE_TAG_WIDTH]} " if trace_id else ""
        print(f"{_tp}PR-only branch view skipped repo={repo}: {str(e)[:120]}", flush=True)
        return None
    rolled_back = _txn_cmd(db, f"ROLLBACK TO SAVEPOINT {sp}")
    released = _txn_cmd(db, f"RELEASE SAVEPOINT {sp}")
    if not rolled_back or not released:
        raise RuntimeError("could not close speculative branch view")
    return impact if isinstance(impact, dict) else None


def handle_pull_request(db, event: dict, author_display: str | None = None, act_for: bool = False,
                        reconcile_branch_claims=None, gh=None) -> dict:
    """Process one GitHub `pull_request` webhook event. Returns what the App would post (check + comment),
    or, on a merge, the landing result + the set of comments to refresh. Identity is the PR's change_id.

    act_for=True is the HOSTED path: the connection is the App's service identity (veripsa_app) and each
    claim is reserved on behalf of the PR's git AUTHOR via the delegation gate — so the board/comment name the
    real author. act_for=False is the per-author trial path (each author connects as its own seat).

    `gh` (optional, additive): the GitHub REST client, used ONLY by the flag-gated compat shadow analysis
    (VERIPSA_COMPAT_ANALYSIS — default OFF) to read shared files at verified PR heads. Absent/None → every
    existing caller is unaffected; with the flags OFF the compat path returns before any work either way."""
    action = event.get("action")
    repo = event["repo"]
    branch = event.get("base_branch", "main")
    pr = event["pr_number"]
    cid = _change_of(pr)
    author = author_display or event.get("author") or "unknown"
    # TRACE-ID PLUMB (Round-2 observability follow-up): the dispatcher (handle_event) mints a per-event uuid4 and
    # stashes it on the event payload; threading it into _optional + every per-event log line here lets an on-call
    # grep ONE event's complete trail (webhook → ingest → render). Read defensively: a unit-test caller that builds
    # an event by hand (no dispatcher entry) still works — just without the prefix.
    trace_id = _trace_of(event)
    # SEAT METERING: is the PR author a Bot? The login is sanitized before storage (the '[bot]' marker is
    # stripped — dependabot[bot] → dependabotbot), so the gate cannot tell a bot from the stored login. The
    # ONLY honest signal is the webhook payload's user.type == 'Bot' (server.py computes it into the event).
    # A human → a SEAT (the PLG value line); a bot → free (the AI-fleet wedge). Content-free (a boolean).
    author_is_bot = bool(event.get("author_is_bot"))

    # A base-retarget back onto the protected branch (action=='edited' carrying changes.base) is analyzed like an
    # open. It is exempt from the concluded guard below (it is NOT in the guarded 'opened'/'synchronize' set), so
    # a PR retargeted off-then-back-onto main RE-ACTIVATES its lanes instead of staying tombstoned forever (the
    # audited silent-miss: GitHub fires only `edited` on a base change, never `synchronize`).
    base_retarget = bool(event.get("base_retarget_onto_main"))
    if action in _PR_ANALYZE_ACTIONS or base_retarget:
        # ORDER-INDEPENDENCE: GitHub does not guarantee webhook delivery order. If a stale 'opened'/'synchronize'
        # is redelivered AFTER this PR already landed/withdrew, re-declaring its claims would RESURRECT a closed
        # PR as falsely in-flight. Skip it. A genuine `reopened` is exempt from the concluded-guard — it SHOULD
        # re-activate the lanes (the legitimate close→reopen of a NON-merged PR).
        authoritative_pr_replay = bool(event.get("_veripsa_authoritative_pr_replay"))
        if (action in ("opened", "synchronize", "converted_to_draft")
                and not authoritative_pr_replay
                and db("SELECT core.change_concluded(%s,%s)", (repo, cid))):
            return {"pr": pr, "action": action, "noop": True, "skipped": "stale event after the PR concluded"}
        # MERGED-PR RESURRECTION GUARD: a `reopened`/`ready_for_review` whose payload says merged=true is
        # IMPOSSIBLE as a genuine action — GitHub never lets you reopen (or mark ready) a PR that already MERGED.
        # So such an event is provably a STALE, REORDERED redelivery of a pre-merge action arriving AFTER the
        # merge (at-least-once + no ordering). Re-declaring its claims would resurrect the merged PR's lanes as
        # falsely in-flight — blocking every future PR on those files behind a ghost until the lease expires.
        # The concluded-guard above deliberately exempts these two actions (a genuine reopen must re-activate),
        # so the merged flag is the only honest signal that distinguishes a real reopen from a stale one. Skip it.
        if action in ("reopened", "ready_for_review", "converted_to_draft") and bool(event.get("merged")):
            return {"pr": pr, "action": action, "noop": True,
                    "skipped": "stale reopened/ready_for_review/converted_to_draft after the PR merged (cannot resurrect a merged PR)"}
        changed_paths = list(event.get("changed_files", []) or [])
        # FINER COLLISION: per-path content-free changed line ranges ([[start,end],…] from diff HUNK HEADERS).
        # Passed to the gate as jsonb so the claim carries WHERE on the file it edits — the engine then drops a
        # false same-file wait when two changes confidently touch DISJOINT symbols. Absent → file-level (safety net).
        changed_ranges = event.get("changed_ranges") or {}
        # FRESHNESS KEY (the staleness-gated demotion): per-path content hash of THIS file AT THE PR'S BASE. The
        # App already has each changed file's blob sha (git-blob-sha) from the GitHub Files/tree API, so this is
        # content-free and free to carry. The gate demotes a file-level collision to the finer symbol verdict ONLY
        # when this hash == the hash main's graph stored for the file (spans provably valid); otherwise it keeps
        # the file-level serialize (recall-safe). Absent for a path → None → file-level fallback (the safety net).
        base_hashes = event.get("base_hashes") or {}
        # SCOUT-WINDOW state (PO 2026-06-25): pass the PR's draft flag through so the engine can SOFTEN a
        # draft↔non-draft same-file overlap to 'warn' (never the hard 'serialize' that would pause a non-draft
        # behind a still-iterating scout). Absent / None → caller doesn't carry the signal → the gate leaves the
        # column unchanged (back-compat: every existing test path / older event with no `draft` key is unaffected).
        is_draft = event.get("draft")
        is_draft_arg = bool(is_draft) if is_draft is not None else None
        for path in changed_paths:
            ranges = changed_ranges.get(path)
            ranges_json = json.dumps(ranges) if ranges else None   # None → no ranges → file-level fallback
            base_hash = base_hashes.get(path) or None              # None → no base hash → file-level fallback
            if act_for:
                db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
                   (_claim_id(pr, path), path, repo, branch, author, ranges_json, base_hash, author_is_bot, is_draft_arg))
            else:
                db("SELECT core.declare_claim_with_authority(%s,%s,%s,%s,%s::jsonb,%s,%s)",
                   (_claim_id(pr, path), path, repo, branch, ranges_json, base_hash, is_draft_arg))
        # CONVERGE this PR's lanes to its CURRENT file set. The declare loop above only ADDS claims for the files
        # the PR touches NOW; it can never RELEASE a claim for a file the PR USED to touch but dropped (a
        # force-push, a reverted file, a narrowed scope — common on synchronize). Left active, that orphaned claim
        # keeps serialize/warn-ing other PRs forever (cry-wolf — the #1 way a team learns to ignore the bot).
        # reconcile_change_claims releases every active/waiting claim of this change whose path is no longer in the
        # live set (promoting whoever waited behind it) and idempotently ensures one for each path still present.
        # We run it AFTER the declare loop so identity is already correct: the current paths' claims exist
        # attributed to the real author, so reconcile's release-the-missing half is the only thing left to do, its
        # ensure half is a no-op, and re-running on the same file set changes nothing (idempotent, notify-only).
        # HOSTED path only: reconcile is an App-DELEGATION backstop (it can release another author's stranded
        # claim), so it is granted to the App service identity (veripsa_app) alone. The act_for=False per-author
        # trial connects as the author's own seat (no delegation right) — it never drives the real hosted lane
        # state, so skipping reconcile there is correct (and avoids a permission_denied on a non-App seat).
        if act_for:
            db("SELECT core.reconcile_change_claims_with_authority(%s,%s,%s,%s)",
               (cid, repo, branch, changed_paths))
            # Bind the reconciled path/range set to the exact PR head that produced it. This is a separate,
            # additive gate (rather than changing the hot claim function arity): every existing caller remains
            # compatible, while neighbor refresh can prove DB evidence and GitHub Files belong to one commit.
            head_sha = event.get("head_sha") if event.get("head_snapshot_verified") is True else None
            db("SELECT core.set_change_head_sha_with_authority(%s,%s,%s,%s)",
               (cid, repo, branch, head_sha))
        impact = _json(db, "SELECT core.main_impact_surface(%s,%s)", (repo, branch)) or {}
        # LEGACY/MISSED BRANCH-DELETE SELF-HEAL. A BR-* member in THIS PR's structural cluster is the only point
        # where a stale branch reservation can affect the acting verdict. The hosted orchestrator injects one
        # complete-inventory reconciler; direct/trial callers omit it and keep their prior behavior. Run before
        # prediction telemetry, rendering, pause-ACK, and neighbor refresh so a deleted branch is never shown as
        # real work and never asks for a manual acknowledgement. Live branches remain in the gate's supplied set.
        #
        # SAVEPOINT isolation is mandatory: GitHub/DB uncertainty preserves the existing coupling and must not
        # abort the customer event's shared transaction. If one or more stale changes were released, recompute the
        # surface once; otherwise reuse the first result (no extra heavy query on the normal/live-branch path).
        branch_claims_reconciled = None
        branch_inventory_unknown = False
        branch_unknown_cluster_changes: list[str] = []
        branch_verified_cluster_changes: set[str] = set()
        branch_filtered_impact = None
        branch_changes = _cluster_branch_changes(impact, cid)
        if act_for and callable(reconcile_branch_claims) and branch_changes:
            unavailable = object()
            branch_claims_reconciled = _optional(
                db, "branch-claim reconcile", reconcile_branch_claims, default=unavailable,
                repo=repo, pr=pr, trace_id=trace_id)
            authoritative = (isinstance(branch_claims_reconciled, dict)
                             and branch_claims_reconciled.get("reconciled") is True)
            if authoritative:
                branch_verified_cluster_changes = set(_cluster_change_ids(impact, cid))
            if not authoritative:
                # Preserve every lane in the DB, but strip ONLY the unverified BR participants from a
                # savepoint-scoped VIEW of the impact. Independent PR↔PR collisions remain real and must retain
                # their verdict/neighbor refresh; only BR-derived coupling becomes unknown/no-ACK. The releases
                # used for this read are always rolled back, so uncertainty never mutates authority state.
                branch_filtered_impact = _impact_without_branch_changes(
                    db, repo, branch, branch_changes, trace_id=trace_id)
                if (not isinstance(branch_filtered_impact, dict)
                        or _cluster_branch_changes(branch_filtered_impact, cid)):
                    # A runner without real savepoints cannot produce the safe PR-only view. Keep the DB state and
                    # use the narrow honest-unknown fallback; the live shared transaction reaches the path above.
                    truncation_note = (
                        "**Partial analysis:** this PR exceeded the per-PR file limit. The unanalyzed remainder "
                        "is unknown, not clear."
                    ) if event.get("truncated_files") else ""
                    if event.get("conflict_markers"):
                        out = render_pr_check(
                            {"repo": repo, "branch": branch, "changes": []}, cid,
                            is_fork=bool(event.get("is_fork")),
                            conflict_markers=event.get("conflict_markers") or [])
                        pending_note = _branch_inventory_unknown_note()
                        out["summary"] = (out.get("summary") or "") + "\n\n" + pending_note
                        if truncation_note:
                            out["summary"] += "\n\n" + truncation_note
                        out["comment"] = (out.get("comment") or out["summary"]) + "\n\n---\n\n" + pending_note
                        if truncation_note:
                            out["comment"] += "\n\n" + truncation_note
                    else:
                        out = _branch_inventory_unknown_check(branch)
                        if truncation_note:
                            out["summary"] = (out.get("summary") or "") + "\n\n" + truncation_note
                        out = {**out, "comment": out.get("summary")}
                    safe_reconcile = (branch_claims_reconciled if isinstance(branch_claims_reconciled, dict)
                                      else {"reconciled": False, "skipped": "branch inventory unavailable"})
                    return {
                        "pr": pr, "action": action,
                        "check": {"conclusion": out["conclusion"], "title": out["title"],
                                  "summary": out["summary"]},
                        "comment": out.get("comment"), "refreshed": [],
                        "branch_inventory_unknown": True,
                        "branch_claims_reconciled": safe_reconcile,
                    }
                branch_inventory_unknown = True
                branch_unknown_cluster_changes = _cluster_change_ids(impact, cid)
                if not isinstance(branch_claims_reconciled, dict):
                    branch_claims_reconciled = {
                        "reconciled": False, "skipped": "branch inventory unavailable"}
                impact = branch_filtered_impact
            released_changes = (branch_claims_reconciled.get("released_changes")
                                if isinstance(branch_claims_reconciled, dict) else None)
            if isinstance(released_changes, list) and released_changes:
                impact = _json(db, "SELECT core.main_impact_surface(%s,%s)", (repo, branch)) or {}
        # EFFECT measurement: persist the prediction. When THIS change is warned (a structural A→B exposure),
        # record it so effect_surface can tally predictions made (a steer on record, auditable later). Only on
        # the FIRST analysis ('opened', or 'ready_for_review' when the PR opened as a draft) — synchronize
        # re-renders without re-recording.
        if action in ("opened", "ready_for_review") and not branch_inventory_unknown:
            me = next((c for c in impact.get("changes", []) if c.get("change_id") == cid), None)
            if me and me.get("verdict") == "warn":
                for p in (me.get("paths") or [])[:1]:    # one prediction per warned change (content-free)
                    db("SELECT core.record_warn_with_authority(%s,%s,%s,%s)", (p, repo, branch, cid[:200]))
            # ANSWER-CHECK (答え合わせ) — snapshot THIS change's PREDICTION so the close-time outcome can grade it.
            # The live verdict + the recorded land order EXIST ONLY in main_impact_surface (recomputed every event
            # from the in-flight set, which is gone once the PR closes), so they must be persisted NOW or the
            # outcome can never be measured. record_prediction is idempotent (first analysis wins). The 'behind'
            # list = the change refs THIS change was told to land AFTER (serialize_behind), extracted from the
            # surface's labels (content-free change refs only — 'PR-<n>'/'BR-<x>'). Bounded + fail-open: a record
            # error is swallowed (telemetry must NEVER abort the customer-facing check/comment). HOSTED path only
            # (act_for) — the prediction fact is App-delegation-recorded (a buyer writer must not forge it).
            if act_for and me is not None:
                # SAVEPOINT-ISOLATED: this is best-effort telemetry — its failure (e.g. a least-privilege grant
                # gap) must roll back ONLY itself and NEVER abort the txn the customer-facing check/comment +
                # pause-ack overlay need (see _optional). A bare try/except would leave the txn aborted.
                def _rec_pred():
                    behind = _behind_refs(me.get("serialize_behind") or [])
                    db("SELECT core.record_prediction_with_authority(%s,%s,%s,%s,%s)",
                       (cid, repo, branch, me.get("verdict") or "unknown", behind or None))
                _optional(db, "prediction record", _rec_pred, repo=repo, pr=pr, trace_id=trace_id)
        # HONESTY: a mega-PR whose file set was capped (server's _MAX_PR_FILES) was only PARTIALLY analyzed for
        # coupling — pass that through so the surface discloses it instead of a confident "clear" over unread files.
        # FORK INFO-LEAK GUARD: server.py computes is_fork (head.repo.id != base.repo.id) and threads it on the
        # event; pass it so the ACTING fork PR's own comment (posted on the base-repo conversation the external
        # contributor can read) is REDACTED — it must not name the base repo's other in-flight PRs / paths. The
        # NEIGHBOR refresh below is NOT redacted: those comments post on the base-repo MAINTAINERS' own PRs (trusted),
        # so they keep full detail (naming the fork PR to a maintainer is fine).
        # CO-CHANGE (empirical / logical coupling): the "you touched A; historically B comes with it" hint the
        # dependency graph is blind to. Fetched ONLY for a NON-fork acting PR (the partner files are base-repo
        # paths a fork contributor must not see) and ONLY for this PR's edited files. FAIL-OPEN + advisory: a read
        # error (or an empty co_change cache — not populated yet) just drops the line, never breaks the check.
        # Lift-ranked + content-free; render_pr_check renders it as an additive line, never a verdict.
        cc = None
        if not bool(event.get("is_fork")):
            # SAVEPOINT-ISOLATED advisory read. This is the surface the prod log caught aborting the shared txn
            # ("permission denied for table co_change" → "current transaction is aborted" on every later
            # statement, incl. the pause-ack overlay). It MUST route through the granted SECURITY-DEFINER
            # authority function (NOT a raw core.co_change table read — veripsa_app has no table grant by the
            # moat design), AND its failure must be CONTAINED to its own savepoint so a co-change hiccup can
            # never silently drop the pause. Fail-open: an error just drops the advisory line.
            cc = _optional(
                db, "co-change read",
                lambda: _json(db, "SELECT core.co_change_partners_with_authority(%s,%s,%s,%s)",
                              (repo, changed_paths, 3, 0.4)),
                repo=repo, pr=pr, trace_id=trace_id)
        # COMPAT SHADOW ANALYSIS (compat lane PR-3 — flag-gated DORMANT; docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md
        # §3/§4). Runs ONLY when VERIPSA_COMPAT_ANALYSIS is truthy AND this repo is on the VERIPSA_COMPAT_REPOS
        # allowlist — both read at call time; either off → compat_shadow_enabled returns False before ANY work
        # (zero DB statements, zero GitHub calls, zero log lines), so the OFF path is byte-identical to the
        # pre-PR webhook. SHADOW ONLY: it records 'compat_finding' ledger rows (S1 baseline-anchored
        # classifications — contract_delta:<rule> / rebase_needed / divergent_definitions — plus lane S2's
        # evidence-gated consumer_call_mismatch:<detail>) + ONE
        # content-free counts log line, and feeds NOTHING into render_pr_check below — no render/verdict/
        # comment change anywhere (that is a later flag's PR). HOSTED path only (act_for): the recorder fn is
        # App-delegation-only. FORK GUARD: skipped for a fork acting PR — the sibling heads/paths it would
        # read are base-repo facts (same discipline as the co-change read above). SAVEPOINT-ISOLATED like
        # every optional surface: a compat failure rolls back ONLY itself and can never abort the shared txn
        # the pause-ack overlay + check/comment need. BUDGETED inside the module: pair cap + total-GitHub-
        # file-reads cap; a capped/failed pair degrades to UNKNOWN, never 'compatible' (truncated_files
        # precedent). Bodies are parsed transiently in memory and dropped — fingerprints + counts persist.
        # Head freshness: analyze at the VERIFIED head only (the same head_snapshot_verified condition the
        # claim-head stamp above uses); unverified → the module degrades the affected pairs to unknown.
        if act_for and not bool(event.get("is_fork")) and compat_shadow_enabled(repo):
            _compat_head = event.get("head_sha") if event.get("head_snapshot_verified") is True else None
            _optional(db, "compat shadow analysis",
                      lambda: run_compat_shadow(db, gh, repo=repo, branch=branch, change_id=cid,
                                                head_sha=_compat_head, changed_paths=changed_paths,
                                                impact=impact, trace_id=trace_id, pr=str(pr)),
                      repo=repo, pr=pr, trace_id=trace_id)
        out = render_pr_check(impact, cid, truncated=bool(event.get("truncated_files")),
                              is_fork=bool(event.get("is_fork")), cochange=cc,
                              # ADDED-PATH STATUS (PO 2026-06-25 honest-verdict refinement): the subset of changed
                              # files NEW in this PR (Files-API status='added'). Lets the renderer split the lumped
                              # Unknown copy into "(a) new in this PR — expected, computable after merge" vs
                              # "(b) modified path NOT in main's graph — possible extractor gap". Falls back to
                              # today's lumped copy when absent.
                              added_paths=event.get("added_paths") or [],
                              # CONFLICT MARKERS (PO 2026-06-25 dogfood hole-fix, PRs #111/#114): findings of
                              # unresolved git merge markers introduced by this PR's added lines. When non-empty,
                              # renderer escalates to action_required (hard fail — build-breaker) with a top-of-
                              # comment block naming the path + line. Content-free; absent = no escalation.
                              conflict_markers=event.get("conflict_markers") or [])   # render BY change_id (this PR), not the ambiguous author
        # UPGRADE NUDGE (PO 2026-06-19 "課金促すように"): append the account's billing-coverage hint to THIS PR's
        # check SUMMARY only — NOT the neighbor refresh below (that would nag every in-flight PR), and NEVER on a
        # FORK PR (its summary is readable by an external contributor — the account's file count/plan must not
        # leak). Advisory; content-free (a count + plan label); FAIL-OPEN: a nudge read error must never break the
        # customer-facing check. account_coverage_surface resolves THIS tenant from the session (RLS-isolated).
        if not bool(event.get("is_fork")):
            # SAVEPOINT-ISOLATED advisory read (same reason as the co-change read above): a coverage-nudge read
            # error must roll back ONLY itself, never abort the txn the pause-ack overlay needs.
            _nudge = _optional(
                db, "coverage nudge",
                lambda: coverage_nudge_line(_json(db, "SELECT core.account_coverage_surface()") or {}),
                repo=repo, pr=pr, trace_id=trace_id)
            if _nudge:
                out["summary"] = (out.get("summary") or "") + "\n\n" + _nudge
        # POST-MERGE STALENESS NUDGE (a PRE-CONFLICT heads-up): the in-flight collision detector only sees
        # CONCURRENTLY-open PRs — it is blind to a change that ALREADY LANDED on the protected branch. So if the
        # branch advanced since this PR's base AND a landed file overlaps a file THIS PR is editing, the author
        # would only hit the conflict at rebase/merge time. branch_changed_paths (files that changed on the branch
        # since this PR's base) is threaded on the event by webhook_handlers via gh.compare_changed_paths;
        # stale_base_nudge_line intersects it with this PR's changed_files and returns a content-free line on an
        # overlap (None otherwise). Appended to THIS PR's check SUMMARY only — same guards as the coverage nudge:
        # NON-fork (the branch's other-landing paths are base-repo paths an external fork contributor must not see)
        # and FAIL-OPEN (any error logged + swallowed — an advisory add-on must NEVER break the customer-facing
        # check). Content-free (paths + a count); advisory (never blocks).
        if not bool(event.get("is_fork")):
            try:
                _stale = stale_base_nudge_line(event.get("branch_changed_paths") or [], changed_paths)
                if _stale:
                    out["summary"] = (out.get("summary") or "") + "\n\n" + _stale
            except Exception as e:
                _tp = f"trace_id={trace_id[:_TRACE_TAG_WIDTH]} " if trace_id else ""
                print(f"{_tp}stale-base nudge skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
        if branch_inventory_unknown:
            pending_note = _branch_inventory_unknown_note()
            if out.get("conclusion") == "success" and not event.get("conflict_markers"):
                # The PR-only slice is clear, but the branch participant is still unverified: never overclaim
                # full Clear. Preserve any independent holder/co-change detail as an additive section.
                prior_comment = out.get("comment")
                pending = _branch_inventory_unknown_check(branch)
                out = {**pending, "comment": pending.get("summary")}
                if prior_comment:
                    out["comment"] += "\n\n---\n\n" + prior_comment
            else:
                # A PR-only collision/unknown/conflict is independently valid. Keep its exact title/conclusion
                # and add the branch uncertainty as context; the pause overlay later receives the filtered impact,
                # so action_required can only come from the genuine PR materiality (or conflict marker).
                out["summary"] = (out.get("summary") or "") + "\n\n" + pending_note
                if out.get("comment"):
                    out["comment"] += "\n\n---\n\n" + pending_note
                else:
                    out["comment"] = pending_note
        # STALE-VERDICT-ON-OPEN FIX: a NEW PR entering (or a synchronize re-scoping) the neighborhood changes the
        # OTHER in-flight changes' blast radius/coupling too. The acting PR gets its own check+comment above, but
        # the change it now couples to would otherwise keep showing its PRE-collision verdict (the classic case:
        # the FOUNDATIONAL PR sits on a stale 'clear' while the dependent PR that just opened is correctly warned
        # and even names it in its suggested land order — yet the foundation's author sees nothing). The merge/
        # withdraw and direct-push paths already refresh neighbors via _refresh_changes; an open/sync did not.
        # Refresh every OTHER in-flight change (exclude THIS PR — it is posted directly, never double-posted),
        # under the SAME less-noise rule (a neighbor that is still 'clear' is skipped → no comment spam; the
        # server's idempotent upsert PATCHES an existing verdict, so a re-render that did NOT change rewrites the
        # same body). Content-free; the same render the live event uses.
        refreshed = _refresh_changes(impact, exclude_change=cid)
        if act_for and not branch_inventory_unknown:
            # A PR event only has current branch authority for its own BR-containing cluster. Do not let a normal
            # event re-render an unrelated cluster whose BR truth was never checked; that cluster is retried by its
            # own event or boot reconciliation. A just-verified acting cluster remains eligible for refresh.
            refreshed = [
                row for row in refreshed
                if not _cluster_branch_changes(impact, row.get("change") or "")
                or row.get("change") in branch_verified_cluster_changes
            ]
        if branch_inventory_unknown:
            # The speculative view removed BRs only from the acting cluster. Refresh that cluster's real PR
            # neighbors (the claims this event changed), but never fan the override across unrelated clusters
            # whose own BR truth remains unverified.
            affected = set(branch_unknown_cluster_changes)
            refreshed = [row for row in refreshed if row.get("change") in affected]
        result = {
            "pr": pr, "action": action,
            "check": {"conclusion": out["conclusion"], "title": out["title"], "summary": out["summary"]},
            "comment": out["comment"],
            "refreshed": refreshed,
        }
        if not branch_inventory_unknown:
            verdict_proof = _analysis_verdict_proof(impact, cid, repo, branch, event)
            if verdict_proof is not None:
                result[_ANALYSIS_VERDICT_KEY] = verdict_proof
        if branch_inventory_unknown:
            result["branch_inventory_unknown"] = True
            result["_branch_filtered_impact"] = branch_filtered_impact
            result["_branch_unknown_changes"] = branch_unknown_cluster_changes
        if branch_claims_reconciled is not None:
            result["branch_claims_reconciled"] = branch_claims_reconciled
        return result

    if action == "closed":
        if event.get("merged"):
            # ANSWER-CHECK (答え合わせ) — grade Veripsa's advice on THIS change against the eventual outcome, BEFORE
            # land_change releases its lanes. ORDER MATTERS: the followed/ignored axis is computed from ledger
            # truth (did any predecessor this change was told to wait behind still hold a live lane?). land_change
            # below RELEASES this PR's claims and promotes waiters — so the outcome MUST be captured FIRST, while
            # the in-flight set still reflects the moment of merge (a predecessor still holding its lane = it had
            # not landed first = this change jumped the queue = advice ignored). The conflict/revert facts are
            # content-free booleans the App derived from the close signals (merge needed conflict resolution / a
            # later revert references it / main's required check went red on the merge commit) — each carries a
            # CONFIDENCE label (records-not-correctness: a follow-up "fix" may be unrelated; we never assert we
            # were right). Bounded + fail-open: an outcome-record error is swallowed (telemetry must NEVER abort
            # the landing). HOSTED path only (act_for) — the outcome fact is App-delegation-recorded.
            if act_for:
                try:
                    db("SELECT core.record_advice_outcome_with_authority(%s,%s,%s,%s,%s,%s)",
                       (cid, repo, branch, bool(event.get("conflicted")), bool(event.get("reverted")),
                        event.get("outcome_confidence") or "inferred"))
                except Exception as e:
                    _tp = f"trace_id={trace_id[:_TRACE_TAG_WIDTH]} " if trace_id else ""
                    print(f"{_tp}advice-outcome record skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
            # Record the landing push at the commit that ACTUALLY lands on main — the MERGE commit (land_sha),
            # never the PR's feature-branch head_sha (that commit is not on main). Keying on the real on-main sha
            # makes this push fact DEDUPE against the separate push-to-main webhook GitHub fires for the same
            # merge (idempotent EV-PUSH-<sha>) → ONE landing fact per merge, not a false second at head_sha.
            land_sha = event.get("land_sha") or event.get("head_sha")
            landed = _json(db, "SELECT core.land_change_on_main_with_authority(%s,%s,%s,%s,%s)",
                           (cid, repo, land_sha, event.get("model"), branch))  # land: record + release this PR's lanes
            result = {"pr": pr, "action": "merged", "landed": landed}
        else:
            # closed WITHOUT a merge (abandoned / superseded): NOTHING landed, but this PR's reserved lanes must
            # free NOW so anyone queued behind it is promoted immediately — never stranded until the lease expires.
            released = _json(db, "SELECT core.release_change_on_main_with_authority(%s,%s,%s)", (cid, repo, branch))
            result = {"pr": pr, "action": "withdrawn", "released": released}
        impact = _json(db, "SELECT core.main_impact_surface(%s,%s)", (repo, branch)) or {}
        # the neighborhood changed (this PR's lanes freed + waiters promoted) → refresh the OTHER in-flight PRs.
        # Shared less-noise rule (a 'clear' neighbor is skipped) via _refresh_changes — the SAME path a push to
        # main uses (refresh_inflight), so merge-refresh and push-refresh can never drift.
        _impact, result["refreshed"], branch_reconciled = _refreshes_with_branch_authority(
            db, repo, branch, impact, reconcile_branch_claims=reconcile_branch_claims, trace_id=trace_id)
        if branch_reconciled is not None:
            result["branch_claims_reconciled"] = branch_reconciled
        return result

    return {"pr": pr, "action": action, "noop": True}
