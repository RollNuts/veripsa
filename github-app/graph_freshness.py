#!/usr/bin/env python3
"""Veripsa GitHub App — the GRAPH-FRESHNESS concern (extracted from server.py to cut its out-degree).

This is the content-free FRESHNESS surface — "is the stored main-graph behind main's current HEAD?" — lifted
whole out of the webhook server so the server hotspot file is finer-grained (the same move that already split
out event_queue.py / webhook.py / health_watchdog.py):

  _stored_graph_sha(db, repo, branch)   read back a coordinate's STORED main-graph commit_sha (+ ingested_at +
                                        stored/current extractor and semantic-reference generation stamps)
  graph_freshness(db, gh, repo, branch) ONE coordinate's stored sha vs main HEAD + behind? (the /healthz +
                                        status-surface signal, and the input self_heal_main_graph reads)
  graph_freshness_all(db, gh, cap=None) ALL coordinates' stored sha vs main HEAD (the /freshz + /readyz body
                                        and the watchdog's freshness-alert input)

DESIGN (mirrors event_queue.py / health_watchdog.py): this module imports NOTHING from server.py (no circular
import — server.py imports THIS and RE-EXPORTS these names for backward compatibility, so `server.graph_freshness`
/ `server.graph_freshness_all` keep working for serve()'s handler, self_heal_main_graph, the watchdog's lazy
`from server import graph_freshness_all` fallback, and the test suite's `S.graph_freshness*`). The only build
dependency is env_config.env_int (the VALIDATED env-knob reader, already its own module) for the cap — imported
with the same dual standalone/package idiom the rest of the App uses (run as `python3 github-app/server.py` OR
imported as a package).

These functions are READ-ONLY over (db, gh): they never ingest/patch the graph (the self-heal that re-ingests
on drift stays in server.py with the ingest cluster). They are PURE over their injected (db, gh) seams — db is
the scoped-query callable, gh the GitHub REST client — exactly as before the move.

The extraction originally was a pure move. graph_freshness_all now additionally resolves HEADs with bounded
parallelism across DISTINCT actual GitHub clients. Coordination is process-wide across overlapping watchdog and
/freshz samples, so one client/token is never concurrent and their combined work stays under one worker budget.
Its signature, source-order return shape, orphan/default-branch/rename decisions, never-crash fail-open behavior,
and content-free guarantees remain unchanged.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
import json
import threading
import time

# env_int: the VALIDATED env-knob reader (fail SAFE on a typo'd cap — a non-int or out-of-range value raises a
# loud, actionable refusal that names the var + value + range, instead of crashing with a bare ValueError or
# silently misbehaving on a 0/negative cap). Same standalone/package dual-import idiom server.py uses.
try:
    from env_config import env_int  # noqa: E402
except ImportError:  # imported as a package
    from .env_config import env_int  # noqa: E402


# DEAD-COORDINATE BACKOFF (#834): ghost coordinates — a deleted repo (HEAD resolve 404s forever) or an
# uninstalled/unresolved account — were re-attempted and re-logged EVERY watchdog cycle (~50s), spamming the
# logs and burning one GitHub call per ghost per cycle with a permanently identical outcome. This in-memory
# exponential backoff (10 min doubling to a 6 h cap, process-local, resets on restart — acceptable: the spam
# window after a deploy is minutes, not days) skips re-attempts until the window passes. SURGICAL: only a
# 404/Not Found HEAD failure and the no-installation account branch register here — transient errors
# (network, 5xx, rate limit) keep the existing every-cycle retry so a live coordinate is never slowed down.
# Within a backoff window the coordinate reports unknown (None head), exactly what a fresh failed attempt
# would have produced — the /freshz surface shape is unchanged. A later SUCCESS clears the key, so a repo
# that comes back (undelete/reinstall) resumes normal resolution at the next attempt (≤6 h). Content-free:
# keys are (kind, coordinate-name) only. The graph_version ROW itself is untouched (row cleanup stays a
# PO-gated op, per the orphan-exclusion contract above).
_DEAD_COORD_LOCK = threading.Lock()
_DEAD_COORD_BACKOFF: dict[tuple[str, str], list] = {}
_DEAD_COORD_BASE_SECONDS = 600
_DEAD_COORD_CAP_SECONDS = 21600


def _dead_coord_should_attempt(key: tuple[str, str]) -> bool:
    """True unless `key` is inside its backoff window. NEVER-CRASH: any error means attempt (fail-open)."""
    try:
        with _DEAD_COORD_LOCK:
            entry = _DEAD_COORD_BACKOFF.get(key)
            return entry is None or time.monotonic() >= entry[1]
    except Exception:
        return True


def _dead_coord_mark_dead(key: tuple[str, str]) -> float:
    """Register a dead-outcome attempt for `key`; returns the chosen delay seconds (for the log line)."""
    try:
        with _DEAD_COORD_LOCK:
            entry = _DEAD_COORD_BACKOFF.get(key)
            failures = (entry[0] + 1) if entry else 1
            delay = min(_DEAD_COORD_BASE_SECONDS * (2 ** (failures - 1)), _DEAD_COORD_CAP_SECONDS)
            _DEAD_COORD_BACKOFF[key] = [failures, time.monotonic() + delay]
            return delay
    except Exception:
        return 0.0


def _dead_coord_clear(key: tuple[str, str]) -> None:
    """A live outcome for `key` — forget any backoff so normal every-cycle resolution resumes."""
    try:
        with _DEAD_COORD_LOCK:
            _DEAD_COORD_BACKOFF.pop(key, None)
    except Exception:
        pass


def _canonical_installation_id(value) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    value = str(value).strip()
    if (
        not value
        or not value.isascii()
        or not value.isdigit()
        or value.startswith("0")
        or len(value) > 64
    ):
        return None
    return value


def _stored_graph_sha(
    db, repo: str, branch: str
) -> tuple[str | None, object, str | None, str | None, int | None, int | None]:
    """Read back the STORED main-graph's commit_sha (+ its ingested_at + the extractor-version stamp + the CURRENT
    extractor version) for a coordinate via the gate's coordinate_graph_sha read-back. Content-free (a commit sha
    is public git metadata; the extractor version is a bounded token). Returns
    (commit_sha_or_None, ingested_at, stored_extractor_version_or_None,
    current_extractor_version_or_None, stored_semantic_ref_version_or_None,
    current_semantic_ref_version_or_None).
    NEVER-CRASH: any read error → six None values (treated as 'unknown stored sha' = behind everything →
    self-heal/cold-start, never a false 'current').

    GEN-AGNOSTIC (G3): current_extractor_version is present ONLY on the NEW schema (coordinate_graph_sha returns
    core.current_extractor_version()). On the OLD (pre-predeploy) schema the key is ABSENT → None → the caller's
    version check stays INERT, so a boot against the not-yet-applied schema behaves exactly like the SHA-only path
    (never crashes, never a spurious behind). A legacy row on the NEW schema has stored_extractor_version None (never
    stamped) while current is present → the caller treats that as behind (one forced re-ingest re-stamps it)."""
    try:
        raw = db("SELECT core.coordinate_graph_sha(%s,%s)", (repo, branch))
        info = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
    except Exception as e:
        print(f"graph-sha read skipped repo={repo}@{branch}: {str(e)[:120]}", flush=True)
        return None, None, None, None, None, None
    sha = info.get("commit_sha")
    stored_ev = info.get("extractor_version")
    current_ev = info.get("current_extractor_version")
    stored_sv = info.get("semantic_ref_version")
    current_sv = info.get("current_semantic_ref_version")
    return (sha if isinstance(sha, str) else None), info.get("ingested_at"), \
        (stored_ev if isinstance(stored_ev, str) else None), \
        (current_ev if isinstance(current_ev, str) else None), \
        (stored_sv if isinstance(stored_sv, int) and not isinstance(stored_sv, bool) else None), \
        (current_sv if isinstance(current_sv, int) and not isinstance(current_sv, bool) else None)


def graph_freshness(db, gh, repo: str, branch: str) -> dict:
    """The content-free FRESHNESS of main's STORED graph: the stored commit_sha + main's CURRENT HEAD sha +
    whether the stored graph is BEHIND HEAD (i.e. a push was missed/lagged → predictions run stale). Used by
    the /healthz + status surfaces (visibility) and by the freshness alert (loud on silent drift). NEVER-CRASH:
    a HEAD-resolve error leaves head_sha None + behind=None ('unknown' — we don't claim freshness we can't see).
    A graph never ingested (stored_sha None) AND a resolvable HEAD ⇒ behind=True (cold-start due)."""
    stored_sha, ingested_at, stored_ev, current_ev, stored_sv, current_sv = (
        _stored_graph_sha(db, repo, branch)
    )
    head_sha = None
    head_committed_at = None
    try:
        # repo_default_branch_head returns (default_branch, head_sha) — one cheap GitHub API read (public metadata).
        # Prefer the 4-value variant when the client offers it: the SAME response already carries the head commit's
        # time, which a payload-less self-heal needs as its delivery-order clock (writing NULL there erased the
        # stored clock and disarmed the reordered-delivery guard). hasattr-guarded so older clients and test fakes
        # that only implement the 2-value form keep working unchanged, with head_committed_at simply None.
        if hasattr(gh, "repo_default_branch_head_info_at"):
            _b, _h, _full, _at = gh.repo_default_branch_head_info_at(repo)
            head_committed_at = _at if isinstance(_at, str) and _at else None
        else:
            _b, _h = gh.repo_default_branch_head(repo)
        head_sha = _h if isinstance(_h, str) and _h else None
    except Exception as e:
        print(f"freshness HEAD-resolve skipped repo={repo}: {str(e)[:120]}", flush=True)
    # behind = the stored graph sha does not equal main's HEAD sha (a missed/lagged push) OR the stored graph was
    # produced by a STALE extractor version (G3). We can only tell when HEAD is resolvable; otherwise it stays None
    # (honestly 'unknown', never a false 'current'). A never-ingested coordinate with a resolvable HEAD is behind
    # (the cold-start is owed).
    if head_sha is None:
        behind = None
    elif stored_sha is None:
        behind = True
    else:
        sha_behind = stored_sha != head_sha
        # EXTRACTOR-VERSION FRESHNESS (G3): the stored graph's edges were produced by ONE extractor version. When the
        # DB reports a CURRENT extractor version (i.e. the NEW schema is live) and the stored stamp DIFFERS from it —
        # OR is NULL (a legacy row never re-ingested since the stamp landed) — the stored coupling is computed on
        # OLD-extractor edges, so a coupling only the NEW extractor finds would be SILENTLY missed. Treat that EXACTLY
        # like a lagged SHA: behind=True. This feeds the SAME self-heal (re-ingest re-stamps current) and the SAME G1
        # withhold (a failed re-ingest → the would-be clear is withheld as `unknown`, never a false clear). NULL/absent
        # current_ev = the OLD (pre-predeploy) schema → the check is INERT (behavior identical to the SHA-only path).
        version_behind = current_ev is not None and stored_ev != current_ev
        semantic_behind = current_sv is not None and stored_sv != current_sv
        behind = sha_behind or version_behind or semantic_behind
    return {"repo": repo, "branch": branch, "stored_sha": stored_sha, "head_sha": head_sha,
            "behind": behind, "ingested_at": str(ingested_at) if ingested_at is not None else None,
            "head_committed_at": head_committed_at}


def graph_freshness_at_target(db, repo: str, branch: str, target_sha: str | None) -> dict:
    """Compare the persisted graph with an already-authenticated target SHA, without a GitHub call.

    Live PR and push payloads already carry the exact protected-branch commit they are about to analyze or
    schedule.  Re-resolving HEAD on that latency-sensitive path is both redundant and subtly weaker: the ref may
    move between the signed payload and the unversioned metadata read.  This helper applies the same SHA,
    extractor-version, and semantic-reference-version freshness rules as :func:`graph_freshness`, but treats the
    supplied target as the authority.  A missing/malformed target remains honestly unknown (``behind=None``);
    a DB read failure returns no stored SHA and therefore ``behind=True`` for a valid target, causing callers to
    enqueue/retry rather than claim a stale graph is current.
    """
    stored_sha, ingested_at, stored_ev, current_ev, stored_sv, current_sv = (
        _stored_graph_sha(db, repo, branch)
    )
    target = target_sha if isinstance(target_sha, str) and target_sha else None
    if target is None:
        behind = None
    elif stored_sha is None:
        behind = True
    else:
        version_behind = current_ev is not None and stored_ev != current_ev
        semantic_behind = current_sv is not None and stored_sv != current_sv
        behind = stored_sha != target or version_behind or semantic_behind
    return {
        "repo": repo,
        "branch": branch,
        "stored_sha": stored_sha,
        "head_sha": target,
        "behind": behind,
        "ingested_at": str(ingested_at) if ingested_at is not None else None,
        "head_committed_at": None,
    }


_GRAPH_FRESHNESS_CAP = env_int("VERIPSA_GRAPH_FRESHNESS_CAP", 100, min_value=1)
_GRAPH_FRESHNESS_WORKERS = env_int("VERIPSA_GRAPH_FRESHNESS_WORKERS", 4, min_value=1, max_value=32)
_GRAPH_FRESHNESS_SAMPLE_SECONDS = env_int(
    "VERIPSA_GRAPH_FRESHNESS_SAMPLE_SECONDS", 45,
    min_value=31, max_value=120)
_GRAPH_FRESHNESS_CALL_RESERVE_SECONDS = 30.0

# graph_freshness_all has two independent callers in the live process: the public /freshz sampler and the
# watchdog. A per-call ThreadPoolExecutor alone does NOT bound their combined concurrency, and two overlapping
# calls can otherwise drive the SAME mutable GitHubREST installation client/token at once. Coordinate at module
# scope: one lock per actual client identity, plus a process-wide active-group budget. The registry is bounded in
# practice by the App installation cap; retaining an old lock is harmless even if an object id is later reused.
_HEAD_CLIENT_LOCKS: dict[int, threading.Lock] = {}
_HEAD_CLIENT_LOCKS_GUARD = threading.Lock()
_HEAD_GROUP_CONDITION = threading.Condition()
_HEAD_ACTIVE_GROUPS = 0
_FRESHNESS_SAMPLE_LOCK = threading.Lock()


class FreshnessSample(list):
    """List-compatible freshness page with explicit observation coverage."""

    def __init__(
            self, values=(), *, coverage_complete: bool = False,
            cursor_healthy: bool = True, timed_out: bool = False,
            accounts_scanned: int = 0):
        super().__init__(values)
        self.coverage_complete = bool(coverage_complete)
        self.cursor_healthy = bool(cursor_healthy)
        self.timed_out = bool(timed_out)
        self.accounts_scanned = max(0, int(accounts_scanned or 0))


def _head_client_lock(gh_for) -> threading.Lock:
    with _HEAD_CLIENT_LOCKS_GUARD:
        return _HEAD_CLIENT_LOCKS.setdefault(id(gh_for), threading.Lock())


@contextmanager
def _head_group_slot(deadline: float | None = None):
    """One process-wide worker slot, honoring the validated runtime limit across overlapping samples."""
    global _HEAD_ACTIVE_GROUPS
    with _HEAD_GROUP_CONDITION:
        limit = max(1, int(_GRAPH_FRESHNESS_WORKERS))
        while _HEAD_ACTIVE_GROUPS >= limit:
            if deadline is None:
                _HEAD_GROUP_CONDITION.wait()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "freshness sample group-slot deadline elapsed")
                _HEAD_GROUP_CONDITION.wait(remaining)
            limit = max(1, int(_GRAPH_FRESHNESS_WORKERS))
        _HEAD_ACTIVE_GROUPS += 1
    try:
        yield
    finally:
        with _HEAD_GROUP_CONDITION:
            _HEAD_ACTIVE_GROUPS -= 1
            _HEAD_GROUP_CONDITION.notify_all()


def _resolve_head_group(
        gh_for, repos: list[str], deadline: float | None = None
) -> dict[str, tuple[str | None, str | None, str | None]]:
    """Resolve one actual GitHub client's repos SERIALly.

    GitHubREST clients own an installation token/cache, so the same client must never be driven concurrently.
    graph_freshness_all submits exactly one of these tasks per client identity and only parallelizes DIFFERENT
    clients. A per-repo failure keeps the prior fail-open semantics and lets later repos in the group resolve.
    """
    resolved = {}
    # Acquire the client lock BEFORE the global slot: duplicate calls for one client wait without consuming the
    # fleet-wide budget, so one hot installation cannot starve unrelated clients.
    client_lock = _head_client_lock(gh_for)
    if deadline is None:
        client_lock.acquire()
        acquired = True
    else:
        acquired = client_lock.acquire(
            timeout=max(0.0, deadline - time.monotonic()))
    if not acquired:
        return resolved
    try:
        with _head_group_slot(deadline):
            for repo in repos:
                if (
                    deadline is not None
                    and deadline - time.monotonic()
                    < _GRAPH_FRESHNESS_CALL_RESERVE_SECONDS
                ):
                    break
                head_sha = None
                live_branch = None
                live_repo = None
                if not _dead_coord_should_attempt(("head", repo)):
                    # Ghost repo inside its backoff window: same unknown outcome, no API call, no log spam.
                    resolved[repo] = (None, None, None)
                    continue
                try:
                    if hasattr(gh_for, "repo_default_branch_head_info"):
                        _b, _h, _full = gh_for.repo_default_branch_head_info(repo)
                    else:
                        _b, _h = gh_for.repo_default_branch_head(repo)
                        _full = repo
                    head_sha = _h if isinstance(_h, str) and _h else None
                    live_branch = _b if isinstance(_b, str) and _b else None
                    live_repo = _full if isinstance(_full, str) and _full else repo
                    _dead_coord_clear(("head", repo))
                except Exception as e:
                    msg = str(e)
                    if "404" in msg or "Not Found" in msg:
                        delay = _dead_coord_mark_dead(("head", repo))
                        print(f"freshness HEAD-resolve skipped repo={repo}: {msg[:120]} "
                              f"(dead-coordinate backoff {int(delay)}s)", flush=True)
                    else:
                        print(f"freshness HEAD-resolve skipped repo={repo}: {msg[:120]}", flush=True)
                resolved[repo] = (head_sha, live_branch, live_repo)
    except TimeoutError:
        pass
    finally:
        client_lock.release()
    return resolved


def graph_freshness_all(db, gh, cap: int | None = None) -> list:
    """Run at most one process-local freshness sample at a time.

    /freshz and the watchdog can overlap. A second observation does not queue
    behind a slow GitHub sample (which recreated the long-lived worker shape);
    it returns explicit incomplete/Unknown metadata immediately.
    """
    if not _FRESHNESS_SAMPLE_LOCK.acquire(blocking=False):
        return FreshnessSample(
            [], coverage_complete=False, cursor_healthy=True,
            timed_out=True, accounts_scanned=0)
    try:
        return _graph_freshness_all_locked(db, gh, cap=cap)
    finally:
        _FRESHNESS_SAMPLE_LOCK.release()


def _graph_freshness_all_locked(db, gh, cap: int | None = None) -> list:
    """The App's per-coordinate freshness, joined with main's current HEAD — the input to the freshness ALERT
    and the /freshz + /readyz surfaces. Reads the DB-only freshness surface (stored sha + age, cheap) and, for
    each coordinate (bounded by `cap`), resolves main HEAD once to mark `behind`. NEVER-CRASH: a surface/HEAD
    error degrades a record to behind=None ('unknown', never a false page). Content-free (sha = public git
    metadata, plus a count/age). Returns a list of {repo, branch, stored_sha, head_sha, behind, age_seconds}.

    ORPHAN/PHANTOM-COORDINATE EXCLUSION (mirrors alerts.evaluate_graph_freshness — #338): a (repo, branch) can
    have MORE THAN ONE stored coordinate — the LIVE one predictions run on (it tracks HEAD), plus an ABANDONED
    duplicate written into a dead/renamed-away account (a backfill/old-account graph_version the webhook never
    re-addresses). No PR will ever touch that dead account, so the per-PR self-heal can NEVER fix it: the orphan
    is BEHIND main HEAD FOREVER and the /freshz surface would report `any_behind:true` perpetually even though
    the LIVE coordinate is fresh. Rule (identical grouping to the alert): a (repo, branch) that has ANY CURRENT
    (behind=False) sibling is tracked fresh, so a behind=True record for that SAME (repo, branch) is a stale
    orphan/duplicate, NOT live drift — its `behind` is downgraded to False (so every consumer's `behind is True`
    filter — /freshz's behind_count/any_behind, the watchdog — excludes it CONSISTENTLY) and it is tagged
    `orphan:true` for ops visibility. A behind coordinate whose (repo, branch) has NO fresh sibling is genuine
    drift and STILL reports behind (the exclusion is surgical, keyed on repo+branch, never a blanket silence).
    This is SURFACE-ONLY: the underlying graph_version row is NOT touched (orphan ROW cleanup is a separate
    PO-gated op). Content-free (only the repo/branch grouping + counts, already in the records). FAIL-OPEN: the
    exclusion runs over the already-built `out` and tolerates any record shape, so it never changes never-crash.

    DEFAULT-BRANCH-CHANGE EXCLUSION (audit, the SECOND orphan shape): the coordinate is (account_id, repo,
    DEFAULT branch), so a repo's default-branch MOVE (main → master) leaves the old (…, main) coordinate FROZEN
    while the webhook ingests a fresh (…, master) one — and the (repo,branch)-sibling rule above can't save it
    (the fresh sibling is at a DIFFERENT (repo, master) key). repo_default_branch_head already returns the repo's
    LIVE default branch; each record carries `live_default` (branch == live default?). A behind record CONFIRMED
    on a non-current default branch (live_default is False) is downgraded behind:false + tagged orphan:true (so
    consumers exclude it consistently). live_default None (HEAD unresolvable) is never excluded; the same-branch
    reingest case (live_default True) is untouched. Auto-heals any future default-branch change — no DDL.

    EXTRACTOR-VERSION FRESHNESS (G3) — DELIBERATELY SHA-ONLY AT THE FLEET LEVEL. The singular graph_freshness
    (the verdict/heal path) IS version-aware: a graph produced by a STALE extractor version reads behind=True
    there, so the per-PR self-heal re-ingests it and the G1 withhold downgrades an un-healable would-be Clear to
    `unknown`. This aggregate twin — the input to /freshz, /readyz, and the graph_stale watchdog — intentionally
    does NOT fold version-staleness into `behind`. An extractor bump (and the first deploy that lands the stamp)
    transiently marks EVERY coordinate version-behind until its next FULL re-ingest re-stamps it; folding that into
    the fleet `behind` would flip /readyz not-ready and FLOOD graph_stale for the whole fleet at once — an
    un-actionable page the operator mutes, then misses a LATER real SHA drift: the SAME failure mode the
    orphan-exclusion rules above exist to prevent. Version freshness is a per-coordinate verdict-path concern that
    self-heals on the next event, NOT fleet drift. (An active, de-stormed operator signal for a coordinate
    PERSISTENTLY stuck version-behind — quota-walled / repeatedly-failing re-ingest — is a separate scoped
    observability follow-up, not this path.)"""
    cap = cap if cap is not None else _GRAPH_FRESHNESS_CAP
    deadline = time.monotonic() + float(
        _GRAPH_FRESHNESS_SAMPLE_SECONDS)
    try:
        raw = db(
            "SELECT core.owner_graph_freshness_surface(%s)",
            (int(cap),),
        )
        surface = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
    except Exception as e:
        print(f"freshness surface read skipped: {str(e)[:120]}", flush=True)
        return FreshnessSample(
            [], coverage_complete=False, cursor_healthy=False,
            timed_out=False, accounts_scanned=0)
    coords = surface.get("coordinates") if isinstance(surface, dict) else None
    coords = coords if isinstance(coords, list) else []
    raw_entries = (
        surface.get("entries")
        if isinstance(surface, dict)
        and isinstance(surface.get("entries"), list)
        else None
    )
    scan_entries = []
    if raw_entries is not None:
        coords = []
        for entry in raw_entries[:cap]:
            if not isinstance(entry, dict):
                continue
            coordinate = entry.get("coordinate")
            scan_entry = {
                "account_id": (
                    entry.get("account_id")
                    if isinstance(entry.get("account_id"), str)
                    else ""
                ),
                "coordinate_index": None,
                "processed": coordinate is None,
            }
            if isinstance(coordinate, dict):
                scan_entry["coordinate_index"] = len(coords)
                coords.append(coordinate)
            scan_entries.append(scan_entry)
    else:
        # Rolling/fixture compatibility: old surfaces expose only coordinates
        # and have no durable cursor contract.
        scan_entries = [
            {
                "account_id": (
                    c.get("account_id")
                    if isinstance(c, dict)
                    and isinstance(c.get("account_id"), str)
                    else ""
                ),
                "coordinate_index": i,
                "processed": False,
            }
            for i, c in enumerate(coords[:cap])
            if isinstance(c, dict)
        ]
    out = []
    # MULTI-INSTALLATION: this surface is CROSS-TENANT (owner_graph_freshness_surface visits every tenant), so a
    # coordinate can belong to a NON-primary installation. The singleton `gh` is pinned to the PRIMARY install, so
    # resolving HEAD with it 404/403s on every non-primary repo (the prod 'freshness HEAD-resolve skipped repo=
    # owner/repo: HTTP 404' spam) — leaving those tenants' graphs perpetually 'behind'. So resolve the client
    # for THAT coordinate's account (gh.for_account(account_id) → for_installation under the hood; ONE App-
    # installations read, then cached) and resolve HEAD through it — exactly as the webhook worker does.
    #
    # MARKETPLACE LATENCY: resolving every coordinate serially made a 28-coordinate /freshz sample take ~70s.
    # Normalize the bounded coordinate slice first, then resolve for_account ON THIS calling thread exactly once
    # per account. Group by the returned client's ACTUAL identity (not account_id): one GitHubREST instance owns
    # one installation token/cache and is therefore driven by exactly ONE worker, with that group's distinct repos
    # resolved serially. Only DIFFERENT client identities run concurrently, bounded by the validated worker knob.
    # This also makes the HEAD cache correctly (client identity, repo), so two account aliases returning the same
    # client never duplicate a request. Results are joined back onto `normalized` below, preserving source order.
    # None = no installation for this account: it is never submitted, so no network call is possible and every
    # coordinate on it remains honestly unknown. The cached triple retains the current-default + canonical-name
    # metadata used by the unchanged default-branch/rename orphan rules below.
    normalized = []
    for coordinate_index, c in enumerate(coords[:cap]):
        if not isinstance(c, dict):
            continue
        repo_raw, branch_raw = c.get("repo"), c.get("branch")
        normalized.append({
            "repo": repo_raw if isinstance(repo_raw, str) else "",
            "branch": branch_raw if isinstance(branch_raw, str) else "",
            "account_id": c.get("account_id") if isinstance(c.get("account_id"), str) else "",
            # Current schemas carry the exact durable installation id. This
            # makes watchdog/freshz routing O(coordinate-cap), independent of
            # total App installations, and removes the bounded-but-partial
            # GET /app/installations map from the normal production path.
            "github_installation_id": _canonical_installation_id(
                c.get("github_installation_id")),
            "_coordinate_index": coordinate_index,
            "stored_sha": c.get("commit_sha") if isinstance(c.get("commit_sha"), str) else None,
            "age_seconds": c.get("age_seconds") if isinstance(c.get("age_seconds"), (int, float)) else None,
        })

    gh_for_account: dict[str, object | None] = {}
    # for_installation() lazily populates a shared sibling-client cache. Legacy
    # coordinates without a durable id still use for_account(), whose partial
    # App map is fallback-only. The watchdog and /freshz may overlap, so
    # resolve the WHOLE per-sample set under the root-client lock; two samples
    # cannot mint distinct sibling objects and evade the identity-keyed worker
    # lock below. Release it before submitting HEAD work.
    with _head_client_lock(gh):
        for c in normalized:
            account_id = c["account_id"]
            if account_id in gh_for_account:
                continue
            try:
                installation_id = c["github_installation_id"]
                if installation_id is not None:
                    resolver = getattr(gh, "for_installation", None)
                    if not callable(resolver):
                        raise RuntimeError(
                            "exact installation resolver unavailable")
                    gh_for_account[account_id] = resolver(installation_id)
                elif raw_entries is None:
                    # Rolling fixture compatibility only. The new durable
                    # surface uses entries and always carries an exact
                    # installation id; if that authority is missing below we
                    # refuse the fleet-wide inventory fallback. Predeploy
                    # installs the new surface before this code can run.
                    gh_for_account[account_id] = (
                        gh.for_account(account_id)
                        if hasattr(gh, "for_account")
                        else gh
                    )
                else:
                    gh_for_account[account_id] = None
                    print(
                        "freshness client-resolve skipped "
                        f"account={account_id or '(none)'}: "
                        "durable installation id unavailable",
                        flush=True,
                    )
            except Exception as e:
                # The surface is observational and never-crash: one broken mapping cannot blind other tenants.
                print(f"freshness client-resolve skipped account={account_id or '(none)'}: {str(e)[:120]}",
                      flush=True)
                gh_for_account[account_id] = None

    groups: dict[int, dict] = {}
    unresolved_seen = set()
    for c in normalized:
        gh_for = gh_for_account[c["account_id"]]
        c["client_id"] = id(gh_for) if gh_for is not None else None
        if gh_for is None:
            unresolved_key = (c["account_id"], c["repo"])
            if unresolved_key not in unresolved_seen:
                unresolved_seen.add(unresolved_key)
                # No network call happens on this branch either way — the backoff only silences the
                # once-per-cycle log line for a chronically uninstalled/unresolved account (#834).
                if _dead_coord_should_attempt(("acct", c["account_id"])):
                    delay = _dead_coord_mark_dead(("acct", c["account_id"]))
                    print(f"freshness HEAD-resolve skipped repo={c['repo']}: no installation for account "
                          f"{c['account_id'] or '(none)'} (uninstalled/unresolved) — not stale, just "
                          f"unservable here (dead-coordinate backoff {int(delay)}s)", flush=True)
            continue
        _dead_coord_clear(("acct", c["account_id"]))
        group = groups.setdefault(c["client_id"], {"client": gh_for, "repos": [], "repo_set": set()})
        if c["repo"] not in group["repo_set"]:
            group["repo_set"].add(c["repo"])
            group["repos"].append(c["repo"])

    # Preseed every cache entry as unknown. If executor setup, submit, or an entire group task fails, that group's
    # entries remain unknown while successful groups still populate normally (never a cross-tenant all-or-nothing).
    head_cache: dict[tuple[int, str], tuple[str | None, str | None, str | None]] = {
        (client_id, repo): (None, None, None)
        for client_id, group in groups.items()
        for repo in group["repos"]
    }
    resolved_keys: set[tuple[int, str]] = set()
    timed_out = False
    if groups:
        pool = None
        try:
            pool = ThreadPoolExecutor(
                max_workers=min(_GRAPH_FRESHNESS_WORKERS, len(groups)),
                thread_name_prefix="freshness-head",
            )
            futures = {}
            for client_id, group in groups.items():
                if (
                    deadline - time.monotonic()
                    < _GRAPH_FRESHNESS_CALL_RESERVE_SECONDS
                ):
                    timed_out = True
                    break
                future = pool.submit(
                    _resolve_head_group,
                    group["client"],
                    group["repos"],
                    deadline,
                )
                futures[future] = (client_id, group)

            remaining = max(0.0, deadline - time.monotonic())
            done, pending = wait(futures, timeout=remaining)
            if pending:
                timed_out = True
            for future in pending:
                future.cancel()
            for future in done:
                client_id, group = futures[future]
                try:
                    for repo, value in future.result().items():
                        key = (client_id, repo)
                        head_cache[key] = value
                        resolved_keys.add(key)
                except Exception as e:
                    print(
                        "freshness HEAD-resolve group skipped "
                        f"repos={len(group['repos'])}: {str(e)[:120]}",
                        flush=True,
                    )
        except Exception as e:
            # A pool-level failure is exceptionally rare; the preseeded unknowns preserve the never-crash surface.
            print(f"freshness HEAD-resolve pool skipped groups={len(groups)}: {str(e)[:120]}", flush=True)
        finally:
            # A context-manager shutdown waits for every future and defeats
            # the absolute sample wall. Running GitHub calls are already
            # transport-bounded and hold the per-client/global guards until
            # they finish; queued work is cancelled and this observer returns
            # Unknown at the deadline.
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)

    for c in normalized:
        repo = c["repo"]
        branch = c["branch"]
        stored_sha = c["stored_sha"]
        client_id = c["client_id"]
        if client_id is None:
            head_sha, live_branch, live_repo = None, None, None
        else:
            head_sha, live_branch, live_repo = head_cache[(client_id, repo)]
        coordinate_index = c["_coordinate_index"]
        if client_id is None or (client_id, repo) in resolved_keys:
            for scan_entry in scan_entries:
                if scan_entry["coordinate_index"] == coordinate_index:
                    scan_entry["processed"] = True
                    break
        if head_sha is None:
            behind = None                                   # can't see HEAD → unknown (never a false page)
        elif stored_sha is None:
            behind = True
        else:
            behind = stored_sha != head_sha
        # live_default: is THIS coordinate's branch the repo's CURRENT default branch? True only when we resolved a
        # live default AND it equals this branch; False when we resolved a live default that DIFFERS (a non-current
        # branch — e.g. the old `main` after the repo's default moved to `master`); None when the live default is
        # unknown (HEAD unresolvable) — exclusion only fires on a CONFIRMED mismatch, never on unknown.
        live_default = (branch == live_branch) if live_branch is not None else None
        out.append({"repo": repo, "branch": branch, "stored_sha": stored_sha, "head_sha": head_sha,
                    "behind": behind, "live_default": live_default,
                    "age_seconds": c["age_seconds"],
                    "_live_repo": live_repo})

    # ORPHAN/PHANTOM-COORDINATE EXCLUSION — mirror alerts.evaluate_graph_freshness's EXACT grouping so the
    # /freshz surface and the operator alert agree. A coordinate KEY = (repo, branch); an empty repo OR branch
    # is NOT a usable key (we cannot prove a fresh sibling), so it never silences anything. Any (repo, branch)
    # with a CURRENT (behind is False) record is tracked fresh → a behind=True record for that SAME (repo,
    # branch) is a stale orphan/duplicate. We downgrade its `behind` to False (so the consumers' `behind is
    # True` filters exclude it consistently) and tag `orphan:true`. A behind record with NO fresh sibling is
    # left untouched (genuine drift still reports behind). Surface-only; fail-open (tolerates any record shape).
    def _coord_key(f):
        repo = f.get("repo") or ""
        branch = f.get("branch") or ""
        return (repo, branch) if repo and branch else None
    fresh_keys = {_coord_key(f) for f in out
                  if f.get("behind") is False and _coord_key(f) is not None}
    for f in out:
        if f.get("behind") is True and _coord_key(f) in fresh_keys:
            f["behind"] = False        # a fresh live sibling exists for this (repo,branch) → stale orphan, not live drift
            f["orphan"] = True         # ops visibility: this row is behind-forever under a dead/renamed account

    # OWNER-LOGIN / REPO-RENAME ORPHAN EXCLUSION — the same stale-row class, but the fresh sibling has a DIFFERENT
    # repo full_name because GitHub renamed the owner or repo (e.g. example-user/veripsa-core-old -> RollNuts/veripsa).
    # GitHub's repo read resolves the old name to the canonical current full_name. If a behind old-name coordinate
    # resolves to canonical repo X, and a fresh coordinate for X exists at the same branch + HEAD, the old-name row
    # is a renamed-away orphan, not live drift. Surface-only; requires a confirmed canonical name and matching HEAD.
    def _canonical_key(f):
        live_repo = f.get("_live_repo") or ""
        branch = f.get("branch") or ""
        head = f.get("head_sha") or ""
        return (live_repo, branch, head) if live_repo and branch and head else None
    fresh_canonical_keys = {_canonical_key(f) for f in out
                            if f.get("behind") is False and _canonical_key(f) is not None}
    for f in out:
        if (f.get("behind") is True
                and f.get("_live_repo")
                and f.get("_live_repo") != f.get("repo")
                and _canonical_key(f) in fresh_canonical_keys):
            f["behind"] = False
            f["orphan"] = True

    # NON-CURRENT-DEFAULT-BRANCH EXCLUSION (default-branch-change orphan, audit-confirmed) — the second orphan
    # shape the (repo,branch)-sibling rule above CANNOT catch. Veripsa ingests a repo's DEFAULT branch, so the
    # graph coordinate is (account_id, repo, default_branch). When a repo's default MOVES (e.g. main → master),
    # the webhook starts ingesting `master` → a NEW (…, master) coordinate that tracks HEAD, while the old
    # (…, main) coordinate FREEZES (no push ever re-addresses `main` again). The frozen `main` is BEHIND main
    # HEAD FOREVER, and the sibling rule does NOT save it: its fresh live sibling is at (repo, MASTER), a
    # DIFFERENT (repo, branch) key — so (repo, main) has no fresh sibling at (repo, main) and would page
    # `graph_stale` every interval (an un-actionable flood → mute → a LATER real drift missed). Rule: a
    # coordinate whose `branch` is CONFIRMED NOT the repo's current default branch (live_default is False — we
    # resolved a live default and it differs) is a non-live coordinate predictions never run on → downgrade its
    # `behind` to False and tag orphan:true (so behind_count/any_behind and the watchdog exclude it CONSISTENTLY).
    # This auto-heals ANY future default-branch change with no DDL / data migration. SURGICAL + content-free: keyed
    # only on the live-vs-stored default branch (public git metadata); live_default is None (HEAD unresolvable) is
    # NEVER excluded (we never silence on what we can't see), and the SAME-branch synchronous-reingest case
    # (live_default True) flows through unchanged — the (repo,branch)-sibling rule above still owns that. SURFACE-
    # ONLY: the dormant old-branch graph_version row is NOT touched (row cleanup / content migration is a separate
    # PO-gated op, consistent with the orphan-row treatment above).
    for f in out:
        if f.get("behind") is True and f.get("live_default") is False:
            f["behind"] = False        # this branch is NOT the repo's current default → predictions don't run on it
            f["orphan"] = True         # ops visibility: a frozen non-current-default-branch coordinate (e.g. old `main`)
    for f in out:
        f.pop("_live_repo", None)

    expected_cursor = (
        surface.get("expected_cursor")
        if isinstance(surface, dict)
        and isinstance(surface.get("expected_cursor"), dict)
        else None
    )
    next_cursor = (
        surface.get("next_cursor")
        if isinstance(surface, dict)
        and isinstance(surface.get("next_cursor"), dict)
        else None
    )

    def _valid_cursor(value):
        if not isinstance(value, dict):
            return None
        after = value.get("after_account")
        cycle = value.get("cycle")
        if after is not None and not isinstance(after, str):
            return None
        if (
            isinstance(cycle, bool)
            or not isinstance(cycle, int)
            or cycle < 0
        ):
            return None
        return {"after_account": after or None, "cycle": cycle}

    expected_cursor = _valid_cursor(expected_cursor)
    next_cursor = _valid_cursor(next_cursor)
    has_cursor_contract = expected_cursor is not None and next_cursor is not None
    cursor_healthy = has_cursor_contract
    processed_prefix = 0
    for entry in scan_entries:
        if not entry["processed"]:
            break
        processed_prefix += 1

    if has_cursor_contract:
        target_cursor = None
        if processed_prefix == len(scan_entries):
            target_cursor = next_cursor
        elif processed_prefix:
            last_account = scan_entries[processed_prefix - 1]["account_id"]
            if last_account:
                target_cursor = {
                    "after_account": last_account,
                    "cycle": expected_cursor["cycle"],
                }
            else:
                cursor_healthy = False
        if target_cursor is not None and target_cursor != expected_cursor:
            try:
                advanced = db(
                    "SELECT "
                    "core.advance_graph_freshness_cursor_with_authority"
                    "(%s,%s,%s,%s)",
                    (
                        expected_cursor["after_account"],
                        expected_cursor["cycle"],
                        target_cursor["after_account"],
                        target_cursor["cycle"],
                    ),
                )
                if isinstance(advanced, str):
                    advanced = json.loads(advanced)
                cursor_healthy = advanced is True
            except Exception as e:
                cursor_healthy = False
                print(
                    "freshness cursor advance skipped: "
                    f"{str(e)[:120]}",
                    flush=True,
                )
    else:
        # Old rolling surfaces have no durable fairness proof. Preserve the
        # list-compatible records, but never promote a bounded prefix to
        # complete/clear.
        cursor_healthy = raw_entries is None

    all_entries_processed = processed_prefix == len(scan_entries)
    full_fleet_from_start = (
        has_cursor_contract
        and expected_cursor["after_account"] is None
        and surface.get("coverage_complete") is True
    )
    coverage_complete = bool(
        full_fleet_from_start
        and all_entries_processed
        and not timed_out
        and cursor_healthy
    )
    accounts_scanned = (
        surface.get("accounts_scanned")
        if isinstance(surface, dict)
        and isinstance(surface.get("accounts_scanned"), int)
        and not isinstance(surface.get("accounts_scanned"), bool)
        else len(scan_entries)
    )
    return FreshnessSample(
        out,
        coverage_complete=coverage_complete,
        cursor_healthy=cursor_healthy,
        timed_out=timed_out,
        accounts_scanned=accounts_scanned,
    )
