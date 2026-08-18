#!/usr/bin/env python3
"""Veripsa GitHub App — the webhook brain's CONTENT-FREE COERCION / GUARD / ID-BUILDER leaf.

The smallest, most-reused cluster of webhook_handlers.py: the never-crash type guards over an attacker-controlled
payload (_as_obj / _as_list / _as_int), the work-unit / comment / change identity builders (_change_id /
_branch_change_id / _branch_claim_id / _comment_marker / _marked_comment / _pr_number_from_change), the push- and
PR-time changed-path extraction + the code-coupling path filter (_push_changed_sets / _push_author_is_bot /
_code_paths), the free-tier-wall + suspend + marketplace-plan + ack-label readers (_quota_result /
_install_is_suspended / _marketplace_plan_name / _ack_label_present / _event_installation_id), the merge-queue ref
parser + the merge_group check renderer (_merge_queue_pr_numbers / _render_merge_group_check / _code), and the
synthetic re-run payload builder (_rerun_replay). Every function here is PURE over its arguments (no `db`, no `gh`
post, no live `server`-module read) — a content-free leaf the rest of the brain imports.

WHY ITS OWN MODULE: these are the byte-for-byte coercion/identity primitives the dispatch + the per-event handlers
all share. Splitting them out of webhook_handlers.py shrinks that orchestrator to the routing it owns, mirroring how
github_rest.py's read/write surfaces and _cg_schema.py's per-language extractors were each lifted into cohesive
sibling leaves. It imports NOTHING from webhook_handlers.py (one-directional: webhook_handlers ← webhook_coercion),
so there is no cycle. Its OWN dependencies are the proven leaf modules (webhook's bounded claim-id builder + the
change_id cap; render's ACK_LABEL; code_graph_extract's NFC + non-code-path predicate), pulled DIRECTLY through the
same standalone/package dual-import idiom the rest of the App uses. webhook_handlers.py RE-EXPORTS every name below
so `webhook_handlers.X` (and, through server.py's own re-export, `server.X`) keeps resolving for the gates + the
tests that reach these by name.
"""
from __future__ import annotations

import json  # used by _quota_result's tolerant result parsing

try:
    from webhook import _bounded_claim_id, _CHANGE_ID_CAP  # noqa: F401
except ImportError:  # imported as a package
    from .webhook import _bounded_claim_id, _CHANGE_ID_CAP  # noqa: F401
try:
    from render import ACK_LABEL
except ImportError:  # imported as a package
    from .render import ACK_LABEL


def _change_id(pr_number: int) -> str:
    return f"PR-{pr_number}"


# NEVER-CRASH TYPE GUARDS. A webhook body is ATTACKER-CONTROLLED (the endpoint is public; we read it before we
# can even verify the HMAC, and a valid-signature delivery can still carry semantically junk content). The
# `payload.get("X") or {}` idiom only guards a MISSING/None/falsy value — it passes a WRONG-TYPED value straight
# through: if a caller sends `installation` / `repository` / `pull_request` as a STRING (or `repositories` /
# `commits` as a string, or a list whose entries are not objects), the next `.get()` / iteration / index raises
# AttributeError/TypeError that ESCAPES handle_event. The worker's per-event try/except would catch it, but it
# would churn the 'failed' counter (and, on the live processor, do so AFTER taking a per-repo advisory lock) for
# what should be a clean no-op. GitHub never sends these shapes, but "degrades gracefully, never crashes" must
# hold for hostile input too. These coerce to the EXPECTED type or an empty one — a wrong-typed field then reads
# as absent (→ the existing 'malformed payload, skipped' no-op), never a raise.
def _as_obj(v) -> dict:
    """v if it is a dict, else {} (so `.get()` on a wrong-typed nested field is a clean miss, not AttributeError)."""
    return v if isinstance(v, dict) else {}


def _as_list(v) -> list:
    """v if it is a list, else [] (so iterating a wrong-typed array field is a clean empty loop, not a raise)."""
    return v if isinstance(v, list) else []


def _as_int(v) -> int:
    """v coerced to a non-negative int, else 0 (a wrong-typed / missing count then reads as 'no count' — the
    empty-files cross-check below treats 0 as 'do not fire the guard', so a malformed payload degrades to
    today's behavior rather than a false unknown). bool is rejected (a JSON true/false is not a file count)."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v if v > 0 else 0
    return 0


def _branch_from_ref(ref: str | None) -> str:
    """The branch name from a push ref. A push ref is 'refs/heads/<branch>' and a branch name may itself
    contain slashes (feature/x, dependabot/npm_and_yarn/...). Strip ONLY the 'refs/heads/' prefix — never
    split on '/' and take the last segment (that silently truncates 'feature/login' → 'login', which would
    mis-key the branch's reserved lanes AND break push↔PR reconciliation, since the PR's head.ref keeps the
    full 'feature/login'). A tag push ('refs/tags/...') or any non-branch ref returns '' (the caller skips
    it — Veripsa governs branch pushes heading toward the protected branch, not tags)."""
    r = ref if isinstance(ref, str) else ""   # a non-string ref (malformed payload) → no branch, never a crash
    if r.startswith("refs/heads/"):
        return r[len("refs/heads/"):]
    return ""  # not a branch ref (tag/other) → caller treats as no branch


def _branch_change_id(branch: str) -> str:
    """The work-unit identity for a NON-main BRANCH push (a feature branch reserves lanes the moment it is
    pushed, BEFORE any PR exists). Keyed by the BRANCH, never the author — a collision is work-unit, not
    author: two works on the same path contend regardless of who pushed (the contention key is the path).
    Bounded to the gate's change_id cap (200). When the PR for this head branch later opens, server.py
    RELEASES this change so the PR re-claims the same lanes as 'PR-<n>' with no self-collision (see the push
    handler + handle_event's pull_request 'opened' reconciliation).

    Bounded to _CHANGE_ID_CAP (NOT the raw 200 gate cap): this exact string is used DIRECTLY as the change_id on
    the release path AND is the prefix _bounded_claim_id splits out of each per-path claim_id. Capping both at the
    SAME _CHANGE_ID_CAP keeps them identical even for a maliciously long head ref (so the PR-open reconcile still
    matches the push-time claims) and reserves room for the per-path disambiguating hash in an over-cap claim_id."""
    return f"BR-{branch}"[:_CHANGE_ID_CAP]


def _branch_claim_id(branch: str, path: str) -> str:
    """claim_id = 'BR-<branch>:<path>' → the gate derives change_id='BR-<branch>' (it splits on the FIRST ':').
    Mirrors webhook._claim_id's 'PR-<n>:<path>' shape so both the push-time and PR-time claims group by one
    change_id. Built through the SHARED bounded builder so a long path can never truncate-collide two distinct
    paths onto one claim_id (the PK-collision crash — see webhook._bounded_claim_id)."""
    return _bounded_claim_id(_branch_change_id(branch), path)


def _quota_result(res):
    """FREE-TIER WALL detector. A DB-growing gate fn (ingest_graph / patch_graph / record_push) returns a
    structured `{"quota_exceeded": true, "dimension": …, "limit": …, "usage": …}` instead of writing when the
    account is over the free line (advisory — the gate never raises). The db() runner hands back row[0]: a JSON
    STRING (no jsonb adapter) or already a dict. Parse it tolerantly and return the quota dict IFF it signals
    quota_exceeded, else None (a normal result — an event id string, a stats dict, anything). Never raises: a
    junk/None result is simply "not a quota signal" → None (so detection can't itself crash the never-crash worker)."""
    try:
        if isinstance(res, str):
            res = json.loads(res)
        if isinstance(res, dict) and res.get("quota_exceeded"):
            return res
    except Exception:
        pass
    return None


def _comment_marker(pr_number: int) -> str:
    return f"<!-- veripsa:{_change_id(pr_number)} -->"


def _marked_comment(pr_number: int, body: str) -> str:
    marker = _comment_marker(pr_number)
    return body if body.startswith(marker) else f"{marker}\n{body}"


def _ack_label_present(prj: dict) -> bool:
    """PAUSE-ACK: is the `veripsa-ack` label on this PR right now? The pull_request payload carries the PR's
    CURRENT label set in `pull_request.labels[]` ([{name, …}, …]) — so this is read from the webhook itself, NO
    extra API call. Content-free (a label name is a short string we chose, never customer data). Never-crash: a
    missing / wrong-typed labels array (a malformed payload) reads as no label, never a raise."""
    for lab in _as_list(_as_obj(prj).get("labels")):
        if _as_obj(lab).get("name") == ACK_LABEL:
            return True
    return False


def _event_installation_id(payload: dict):
    installation = _as_obj(payload.get("installation"))
    return installation.get("id") or payload.get("installation_id")


def _install_is_suspended(payload: dict) -> bool:
    """Is the installation that delivered this event CURRENTLY suspended on GitHub? GitHub stamps the webhook's
    `installation` object with `suspended_at` (an ISO timestamp) for the WHOLE time the install is suspended, and
    clears it (null) once resumed — so EVERY event delivered for a suspended install (a redelivery of a queued
    event, an event racing the rolling-deploy 2-instance overlap during the suspend transition) carries this
    flag. Content-free: a bare timestamp, the SAME class of field as the install id we already read. We treat any
    non-null/non-empty value as 'suspended' (we never parse it — its mere PRESENCE is the signal)."""
    suspended_at = _as_obj(payload.get("installation")).get("suspended_at")
    return suspended_at not in (None, "")


def _marketplace_plan_name(purchase: dict) -> str:
    """The plan LABEL from a Marketplace `marketplace_purchase` object — content-free (a short plan name, the
    SAME class of identifier as a repo full_name; never customer data). GitHub's marketplace_purchase.plan carries
    a stable numeric `id` + a human `name`. Use the human `name` (what shows in logs/the plan column), NORMALIZED
    to lowercase so it is CANONICAL — the abuse-wall's free-detection (core._account_over_quota) compares the
    stored plan against the literal 'free', so a label that differs only in CASE (GitHub's Marketplace FREE plan
    is listed as "Free" — capital F) must NOT read as a paid plan and skip the free-tier wall. A MISSING/empty
    name maps to 'free' (the WALLED default), NEVER to the numeric `id`: an id like "42" is non-'free', so falling
    back to it would silently grant the unlimited paid override to a name-less FREE plan (the launch-blocking hole).
    The DB setter ALSO lowercases + bounds it (lower(btrim(...)); left(...,64); empty ⇒ 'free') as defense-in-depth,
    so storage is canonical lowercase regardless of entry point. Never raises."""
    plan = _as_obj(purchase.get("plan"))
    name = plan.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip().lower()
    return "free"   # no usable name ⇒ the free wall (never an arbitrary id string = a false 'paid' label)


def _marketplace_effective_date(payload: dict):
    """The Marketplace event's EFFECTIVE DATE — the content-free billing timestamp (an ISO-8601 string GitHub puts
    at the TOP level of a `marketplace_purchase` delivery, e.g. '2017-10-25T00:00:00+00:00'; for an immediate
    `purchased`/`changed` it is the moment it took effect, for a `cancelled`/scheduled `changed` it is when it
    WILL). Threaded into the DB plan setter as its event-ordering high-water mark (audit iter-5 P2): the setter
    REFUSES a plan write whose effective date is STRICTLY OLDER than the last applied, so a re-delivered/reordered
    cancelled→free can not overwrite a later purchased→pro. Returns the ISO string (psycopg2 casts it to
    timestamptz), or None when absent/blank/non-string — in which case the guard is INERT (ingest normally; value-
    idempotency still holds). Content-free (a timestamp, never customer data). Never raises."""
    ed = payload.get("effective_date")
    if isinstance(ed, str) and ed.strip():
        return ed.strip()
    return None


def _rerun_replay(full: dict, repo: str, default_branch: str, full_base: str, number,
                  *, default_branch_authoritative: bool = False) -> dict:
    """Build the synthetic 'synchronize' payload for ONE re-run PR (check_suite/check_run rerequested) from the
    AUTHORITATIVE PR object `full` (gh.get_pull_request). It MUST carry the same fork-/freshness-bearing fields a
    real pull_request webhook does, or the replay silently degrades:
      • base.repo (head.repo is already on `full.head`) → the synchronize path can compute is_fork, and its stable
        id is copied to top-level repository.id so the repository-lifecycle guard recognizes this authoritative
        replay as work for the currently activated base repository. WITHOUT
        base.repo it reads base_repo_id=None ⇒ is_fork=False ⇒ a FORK PR's check/comment posts the FULL,
        non-redacted body (the base repo's OTHER in-flight PR identities + paths) on an external contributor's
        PR — an info leak (the re-run twin of the neighbor-path fork guard).
      • base.sha → the finer-collision freshness read runs; without it the verdict silently degrades to
        file-level, so a re-run yields a coarser verdict than the original event.
      • _veripsa_no_neighbor_refresh → a re-run is ONE PR's button press; replay only THAT PR. Without it each
        replayed PR fans a neighbor-refresh across the whole in-flight set (N×M GitHub posts on one click); the
        neighbors refresh on their own next event (the same suppressor the backfill loop uses). The branch-push
        replay path removes this suppressor after building because that is a REAL head update, not a button press.
      • labels → the PAUSE-ACK signal. A live `pull_request` webhook carries `pull_request.labels[]`, and the
        acting overlay reads the `veripsa-ack` label OFF THAT PAYLOAD (_ack_label_present, no extra API call). A
        synthesized re-run that OMITTED labels made the replayed synchronize read `label_present=False` on an
        ALREADY-ACKED PR → the overlay re-raised `action_required` (re-pausing it) on EVERY auto check_suite/
        check_run re-eval (GitHub fires `rerequested` on a CI re-run AND on the merge-box "Re-run all checks"
        button) — the ack did NOT stick across the re-run (the live launch-blocker: an acked PR kept bouncing back
        to paused whenever its checks re-evaluated). `full` is the AUTHORITATIVE PR object (gh.get_pull_request),
        whose `labels[]` is the CURRENT label set, so threading it through restores the same ack recognition a real
        synchronize has. Content-free (label names we chose / the customer set, never code). Degrades to [] if
        absent (older client) → no worse than before, and the next real event re-derives the state.
      • _veripsa_authoritative_pr_replay → the stale-conclusion guard may be bypassed. A live branch-push or
        check-suite backstop re-fetches the CURRENT PR from GitHub before replaying it; if a stale conclusion
        tombstone exists from an older close/withdraw edge, that authoritative current-open replay must be able to
        re-activate the PR and post a check. Ordinary webhook redeliveries do NOT carry this flag and remain
        protected by the concluded guard.
      • _veripsa_default_branch_authoritative → whether ``default_branch`` came from the signed repository object
        rather than the caller's legacy ``main`` fallback. A guessed coordinate may drive conservative analysis,
        but it can never authorize the BR release-by-difference callback.
    Fail-soft: a sparse/missing base or head object degrades to {} (never raises)."""
    full = _as_obj(full)
    base_obj = _as_obj(full.get("base"))
    base_repo = _as_obj(base_obj.get("repo"))
    repository = {"full_name": repo, "default_branch": default_branch}
    if base_repo.get("id") not in (None, ""):
        repository["id"] = base_repo.get("id")
    replay = {
        "action": "synchronize",
        "number": number,
        "repository": repository,
        "pull_request": {
            "base": {"ref": full_base, "sha": base_obj.get("sha"), "repo": base_repo},
            "head": _as_obj(full.get("head")),
            "user": _as_obj(full.get("user")),
            "draft": full.get("draft") is True,
            "merged": full.get("merged") is True,
            "changed_files": _as_int(full.get("changed_files")),
            "labels": _as_list(full.get("labels")),   # PAUSE-ACK: carry the ack label so a re-run keeps an ack stuck
        },
    }
    replay["_veripsa_no_neighbor_refresh"] = True
    replay["_veripsa_authoritative_pr_replay"] = True
    replay["_veripsa_default_branch_authoritative"] = bool(default_branch_authoritative)
    head_repo = _as_obj(_as_obj(full.get("head")).get("repo"))
    if base_repo.get("id") in (None, "") or head_repo.get("id") in (None, ""):
        replay["_veripsa_fork_identity_unknown"] = True
    return replay


# MERGE QUEUE (merge_group). GitHub's merge queue batches one-or-more enqueued PRs onto a temporary
# `gh-readonly-queue/<base>/...` ref and runs the required checks on THAT batch commit before it lands on the
# protected branch — the PRs are NOT merged directly. A required Veripsa check that never reports a status on the
# merge_group commit DEADLOCKS the queue entry (it waits forever for a status that never arrives); and even with
# an OPTIONAL check, the batch lands UN-analyzed (the exact concurrent-collision case Veripsa sells against).
# The head_ref encodes the enqueued PR number(s) as `gh-readonly-queue/<base>/pr-<n>-<sha>` segments (a batch
# concatenates several). These two pure helpers extract those refs CONTENT-FREELY (ref text only, never a file
# body) so the merge_group handler can name + re-analyze the batched changes and ALWAYS post a check on the head.
_MQ_PR_RE = None


def _merge_queue_pr_numbers(head_ref) -> list[int]:
    """The enqueued PR NUMBER(s) parsed from a merge_group head_ref. GitHub names the queue ref
    `[refs/heads/]gh-readonly-queue/<base>/pr-<n>-<headsha>`; a multi-PR batch concatenates the `pr-<n>-<sha>`
    segments. We extract every `pr-<n>-` occurrence (content-free — only the integer PR numbers, never a body),
    DEDUPED + order-preserving + BOUNDED (a hostile/huge ref can't fan out an unbounded re-list). Returns [] for
    a missing/wrong-typed/parse-miss ref (the handler then falls back to the full in-flight surface) — never raises."""
    global _MQ_PR_RE
    if _MQ_PR_RE is None:
        import re
        _MQ_PR_RE = re.compile(r"pr-(\d+)-")
    if not isinstance(head_ref, str):
        return []
    out, seen = [], set()
    for m in _MQ_PR_RE.finditer(head_ref):
        try:
            n = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if n not in seen:
            seen.add(n)
            out.append(n)
        if len(out) >= 64:                                 # BOUNDED: a real batch is small; cap a pathological ref
            break
    return out


# The order a merge-queue batch verdict escalates in. Only an authoritative clear is `success`; overlap and
# Unknown are `neutral`. We deliberately NEVER return `failure`/`action_required` on the merge-group commit:
# that is the status the queue waits on, so a blocking conclusion here would deadlock the queue entry.
_MQ_VERDICT_RANK = {"clear": 0, "unknown": 1, "warn": 2, "serialize_soft": 2, "serialize": 3}


def _render_merge_group_check(impact, batched_refs: list) -> dict:
    """The Veripsa check to post ON THE MERGE_GROUP HEAD COMMIT for a queued batch. ADVISORY + NEVER-STALL:
    the conclusion is `success` when nothing the batch touches is in flight, else `neutral` (a status the queue
    can ALWAYS proceed past — never `failure`/`action_required`, which would deadlock the queue entry). The
    summary is CONTENT-FREE (counts + the batch's own change refs only — never a path/symbol/body).

    `impact` is core.main_impact_surface(repo, branch) (the SAME in-flight surface the PR path renders from);
    `batched_refs` are the enqueued change refs ('PR-<n>') parsed from the head_ref, used to scope the verdict to
    the batched changes when known (else the whole in-flight set). A missing, partial, or malformed surface is
    explicitly `unknown`/`neutral`: inability to read the graph must never be presented as a clear analysis."""
    refs = [r for r in _as_list(batched_refs) if isinstance(r, str) and r][:64]
    raw_changes = impact.get("changes") if isinstance(impact, dict) else None
    branch = impact.get("branch") if isinstance(impact, dict) else None
    repo = impact.get("repo") if isinstance(impact, dict) else None
    surface_authoritative = (
        isinstance(repo, str) and bool(repo)
        and isinstance(branch, str) and bool(branch)
        and isinstance(raw_changes, list)
        and all(isinstance(c, dict)
                and isinstance(c.get("change_id"), str) and bool(c.get("change_id"))
                and c.get("verdict") in _MQ_VERDICT_RANK
                for c in raw_changes)
    )
    # A valid surface can still be incomplete for this batch.  An empty scoped set previously fell through to
    # `worst=clear`, so a stale/missing PR row made the merge-group Check green.  Every named batch member must
    # have exactly one verdict row; duplicates are ambiguous and fail closed too.
    if surface_authoritative and refs:
        ref_counts = {ref: 0 for ref in refs}
        for change in raw_changes:
            change_id = change.get("change_id")
            if change_id in ref_counts:
                ref_counts[change_id] += 1
        surface_authoritative = len(ref_counts) == len(refs) and all(n == 1 for n in ref_counts.values())
    changes = raw_changes if surface_authoritative else []
    # Scope to named batch refs; a pure/offline caller may omit refs to inspect the whole authoritative surface.
    # The live handler does not pass a readable surface when the batch identity itself was unparseable.
    batched = set(refs)
    scoped = [c for c in changes if c.get("change_id") in batched] if batched else changes
    worst, worst_rank = "clear", 0
    for c in scoped:
        v = c.get("verdict")
        r = _MQ_VERDICT_RANK.get(v, 0)
        if r > worst_rank:
            worst, worst_rank = (v if v in _MQ_VERDICT_RANK else "unknown"), r
    # CONTENT-FREE batch identity for the summary: the batched change refs we parsed, capped (never a body).
    ref_list = ", ".join(_code(r) for r in refs[:20]) if refs else ""
    n = len(scoped)                                          # changes considered for the verdict (telemetry only)
    if not surface_authoritative or worst == "unknown":
        conclusion = "neutral"
        worst = "unknown"
        summary = (f"Veripsa: this merge-queue batch"
                   + (f" ({ref_list})" if ref_list else "")
                   + " was not analyzed to an authoritative clear-or-overlap result. The structural result is "
                   "Unknown, not clear. ADVISORY: this neutral check lets the merge queue proceed; review the "
                   "individual PR checks before merging.")
        title = "Veripsa — merge queue: not analyzed"
    elif worst == "clear":
        conclusion = "success"
        summary = (f"Veripsa: no in-flight structural overlap recorded for this merge-queue batch"
                   + (f" ({ref_list})" if ref_list else "")
                   + f". Records what is heading to {_code(branch)} and flags overlap before merge "
                   "(advisory — it does not block the queue or assert correctness).")
        title = "Veripsa — merge queue: clear"
    else:
        conclusion = "neutral"
        summary = (f"Veripsa: this merge-queue batch"
                   + (f" ({ref_list})" if ref_list else "")
                   + f" touches code that other in-flight changes to {_code(branch)} also touch — review the "
                   "individual PR checks for who must wait or revise. ADVISORY: Veripsa never blocks the merge "
                   "queue (this check always reports a status so the queue can proceed); it records overlap, it "
                   "does not assert correctness.")
        title = "Veripsa — merge queue: review overlap"
    return {"conclusion": conclusion, "title": title, "summary": summary,
            "verdict": worst, "scoped_changes": n}


def _code(s) -> str:
    """Backtick-wrap a short content-free token (branch / change ref) for the GitHub-markdown summary, guarding a
    non-string (never raise on a junk surface)."""
    return f"`{s}`" if isinstance(s, str) and s else "``"


# The gate's claim target_path cap. db/schema/20_core.sql `claim_path_len` (CHECK length(target_path) <= 1024)
# and db/schema/30_gate.sql `_place_claim` RAISE 'target_path too long (max 1024)' (ERRCODE 23514). The brain
# feeds each changed path STRAIGHT to declare_claim / act_for_claim, so a path OVER this cap RAISEs inside the
# per-path claim loop — an exception that ESCAPES handle_event, ABORTS the shared per-event txn (the PR
# coordinates nothing = false clear), and re-crashes on every redelivery (a POISON event). _code_paths bounds
# it here so a single over-cap filename can never poison a whole PR.
_PATH_LEN_CAP = 1024


def _code_paths(paths):
    """Drop paths that DEFINITIONALLY carry no code coupling (docs / images / lock files) from a changed-file
    list, so they never enter coupling coordination. Without this, one README touched alongside code dragged
    the WHOLE PR to the '❓ Not analyzed' verdict (core.main_impact_surface flags a change 'unknown' when any
    of its paths is absent from main's graph — and a doc is never in the graph). Conservative by design: an
    unsupported-language source file is KEPT (honestly reported 'unknown', never silently dropped). See
    code_graph_extract.is_noncode_path. Order-preserving + DEDUPED (a rename now surfaces both its new and old
    path, and the same path can legitimately recur across the file list — a duplicate must not double-count
    against the mega-PR cap nor spend a redundant per-path claim). NON-STRING entries (a malformed Files-API
    response / hostile payload) are dropped — is_noncode_path runs os.path on its arg and would raise on a
    non-str, so guard it here (never crash on a junk changed-files list)."""
    import code_graph_extract as X
    # UNICODE NORMALIZATION (audit:unicode #173): fold each changed path to NFC, the SAME canonical form the
    # extractor now stores graph node paths in — the engine's `path = ANY(touched)` join is Postgres codepoint
    # equality, so an un-normalized touched-set would MISS the canonically-equal graph node (a false miss).
    # ORDER-PRESERVING DEDUP (#190): a rename surfaces two paths (old + new); dedup on the CANONICAL (NFC) form so
    # neither double-counts the mega-PR cap nor spends a redundant claim (and NFC/NFD duplicates collapse too).
    # Content-free, idempotent; non-str entries dropped (is_noncode_path runs os.path and would raise on junk).
    out, seen = [], set()
    for p in _as_list(paths):
        if not (isinstance(p, str) and not X.is_noncode_path(p)):
            continue
        q = X._nfc(p)
        # NEVER-CRASH PATH GUARD (audit: never-crash 2026-06-20). The brain passes each path STRAIGHT to the gate's
        # declare_claim / act_for_claim, and the gate RAISEs (ERRCODE 23514) on a target_path > _PATH_LEN_CAP while
        # psycopg2 refuses a string literal carrying a NUL (0x00) byte. Either RAISE escapes handle_event, ABORTS the
        # shared per-event txn (the PR coordinates nothing = silent miss), and re-crashes on every GitHub redelivery
        # = a POISON event — a single crafted over-length / NUL filename in any PR file would invisibly suppress
        # Veripsa on that whole PR. So DROP a path the gate could never accept. Measured on the CANONICAL (NFC) form,
        # the exact string the brain would bind — NFC can change the length, so cap AFTER normalizing. Recall-safe:
        # dropping one pathological path (a clean miss on that file, the REST of the PR still analyzed) is strictly
        # safer than crashing + poisoning the event. Content-free; a no-op on every realistic path (a real source
        # file is far under 1024 chars and carries no NUL) → valid input is unchanged.
        if len(q) > _PATH_LEN_CAP or "\x00" in q:
            continue
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def _push_changed_sets(payload: dict | None):
    """(changed, removed) repo-relative paths from a push payload — FILENAMES only (content-free).
    changed = added ∪ modified across all commits (the files to re-extract); removed = removed (to forget).
    A path both modified and removed in the same push stays in `changed`; its fetch then 404s at the head
    sha and it gets deleted — correct. pusher = who pushed (for the landing attribution)."""
    added, modified, removed = set(), set(), set()
    payload = _as_obj(payload)
    for commit in _as_list(payload.get("commits")):
        commit = _as_obj(commit)                       # a non-object commit entry → contributes nothing, no crash
        added.update(p for p in _as_list(commit.get("added")) if isinstance(p, str))
        modified.update(p for p in _as_list(commit.get("modified")) if isinstance(p, str))
        removed.update(p for p in _as_list(commit.get("removed")) if isinstance(p, str))
    pusher = (_as_obj(payload.get("pusher")).get("name") or _as_obj(payload.get("sender")).get("login"))
    # UNICODE NORMALIZATION (audit:unicode): fold push-changed/removed paths to NFC — the SAME canonical form
    # the extractor stores graph node paths in — so patch_graph's DELETE/re-INSERT by `path = ANY(touched)`
    # (Postgres codepoint equality) hits the right node for a non-ASCII path, instead of leaving an NFD twin
    # undeleted + inserting an NFC duplicate. Content-free (path bytes are metadata, never code); idempotent.
    import code_graph_extract as X
    return sorted({X._nfc(p) for p in (added | modified)}), sorted({X._nfc(p) for p in removed}), pusher


def _push_author_is_bot(payload: dict | None) -> bool:
    """SEAT METERING (content-free): is the PUSHER a Bot? A push payload has no per-pusher user.type, but its
    `sender` (the GitHub account that triggered the delivery) does — sender.type == 'Bot' for a bot push (a
    GitHub App / automation), 'User'/'Organization' otherwise. The pusher login is sanitized before storage
    (the '[bot]' marker is stripped), so this webhook signal is the ONLY honest way to keep a bot OUT of the
    seat count (the AI-fleet wedge: bots are free; only human operators are seats). A boolean, never PII."""
    return _as_obj(_as_obj(payload).get("sender")).get("type") == "Bot"


def _pr_number_from_change(change_id) -> int | None:
    if not isinstance(change_id, str) or not change_id.startswith("PR-"):
        return None
    try:
        return int(change_id[3:])
    except ValueError:
        return None
