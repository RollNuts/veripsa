#!/usr/bin/env python3
"""Veripsa GitHub App — FORCE-PUSH COALESCING, the collaborator split out of EventQueue (event_queue.py) so that
hotspot is finer-grained (a self-contained, behavior-IDENTICAL module: NO logic change — a PURE extraction of the
push-coalescing cluster the EventQueue used to inline).

THE PROBLEM IT SOLVES. A fleet force-pushing a big monorepo enqueues a STORM of pushes to the SAME (repo,branch).
Re-ingesting every one is wasted work: only the LATEST sha's graph survives. So for a main-branch push about to
re-ingest we coalesce — skip the superseded pushes (recording only their facts) and make the latest do ONE full
re-ingest that stands in for all the skipped ones. The whole correctness burden is "a 'skip' may be returned ONLY
when a NEWER push is provably still queued behind it" — otherwise a skip would leave a permanently stale graph.

STATE (all under ONE lock — submit() registers on the HTTP thread, the worker decides/prunes on its own thread):
  * _latest_push : (repo,branch) -> the LATEST queued sha for that branch. The newest-push tracking.
  * _coalesced   : the set of (repo,branch) for which we SKIPPED a push, so the eventual latest knows it must
                   FULL-reingest (an incremental patch would miss the skipped pushes' files).

DEPENDENCY INJECTION (imports NOTHING from server.py or event_queue.py — no circular import): branch_from_ref(ref)
→ the branch name from a push ref (server's _branch_from_ref), needed to derive the (repo,branch) registry key.
Omitted (None) → a push payload never yields a registry key (registry_key returns None) → every push sees 'normal'
at processing time and re-ingests = always safe, just no coalescing.
"""
from __future__ import annotations

import threading

# Sentinel for "no prior _latest_push value" so a roll-back can distinguish absent-key from a real prior sha.
_UNSET = object()


class PushCoalescer:
    """Owns the FORCE-PUSH COALESCING state + decision for EventQueue. EventQueue delegates the five coalescing
    operations here and exposes the same seams (_register_push / _push_coalesce / _latest_push / ...) by forwarding
    to this collaborator, so the extraction is invisible to callers and tests.

    INJECTED: branch_from_ref(ref) → branch name from a push ref (server's _branch_from_ref) — for the registry key.
    """

    def __init__(self, branch_from_ref=None):
        self._branch_from_ref = branch_from_ref  # _branch_from_ref — branch from a push ref (coalescing)
        # FORCE-PUSH COALESCING state (complements _FairQueue's cross-tenant fairness): the LATEST queued push sha
        # per (repo,branch) + the branches for which we skipped a push (so the latest knows to FULL-reingest).
        # Lock-guarded — submit() runs on the HTTP thread, the decision on the worker thread.
        self._latest_push: dict = {}
        self._coalesced: set = set()
        self._push_lock = threading.Lock()

    def register(self, payload: dict) -> None:
        """Record the LATEST queued push sha per (repo,branch). Kept for tests/back-compat; submit() now inlines
        the register (BEFORE enqueue) so the worker can never coalesce-decide on a latest missing this push."""
        registry = self.registry_key(payload)
        if registry is not None:
            rb_key, sha = registry
            with self._push_lock:
                self._latest_push[rb_key] = sha

    def registry_key(self, payload: dict):
        """Extract ((repo, branch), sha) from a push payload, or None when the payload is malformed / not
        coalesceable (no repo/branch/real sha). The (repo, branch) tuple is the _latest_push / _coalesced key;
        the sha is the value. Best-effort — a malformed payload simply doesn't register (that push then sees
        'normal' at processing time and re-ingests = safe)."""
        try:
            repo = (payload.get("repository") or {}).get("full_name")
            branch = self._branch_from_ref(payload.get("ref")) if self._branch_from_ref else None
            sha = payload.get("after") or ""
            if repo and branch and sha and sha.strip("0"):
                return (repo, branch), sha
        except Exception:
            pass
        return None

    def register_pending(self, payload: dict):
        """REGISTER-BEFORE-ENQUEUE (the coalesce visibility invariant). submit() calls this for a live push BEFORE
        making the queue item dequeuable: record THIS push's sha as the (repo,branch) latest and return a rollback
        token capturing the prior latest. The worker drains on its own thread and can get() + coalesce() the
        instant the enqueue's notify fires; if we registered AFTER, the worker could decide on a _latest_push that
        does NOT yet contain the very push it is deciding, and conclude 'skip' against a stale-but-older latest —
        for the NEWEST push in a burst (nothing queued behind it to rebuild) that 'skip' would leave a PERMANENTLY
        stale graph. Registering first closes that window. Returns None for a non-coalesceable payload (no token →
        nothing to roll back); else a token to pass to rollback() iff the enqueue is then rejected."""
        registry = self.registry_key(payload)
        if registry is None:
            return None
        rb_key, sha = registry
        with self._push_lock:                                   # register BEFORE the item is visible
            prev_push = self._latest_push.get(rb_key, _UNSET)
            self._latest_push[rb_key] = sha
        return (rb_key, sha, prev_push)

    def rollback(self, token) -> None:
        """Undo a register_pending() when the enqueue was rejected (queue.Full → 503): the push never entered the
        queue, so it must not masquerade as a queued 'latest' (Core's failed-delivery recovery handles the 503). Restore the prior
        latest ONLY if no concurrent submit already advanced it past ours."""
        if token is None:
            return
        rb_key, sha, prev_push = token
        with self._push_lock:
            # restore the prior latest ONLY if no concurrent submit already advanced it past ours
            if self._latest_push.get(rb_key) == sha:
                if prev_push is _UNSET:
                    self._latest_push.pop(rb_key, None)
                else:
                    self._latest_push[rb_key] = prev_push

    def prune(self, payload: dict) -> None:
        """LIFETIME RECLAIM for the coalescing maps (the locking is already correct; this fixes only that the maps
        GREW one entry per (repo,branch) ever seen and were never reclaimed — a slow unbounded leak that survived
        even uninstall/purge). Called by the worker AFTER it finishes a push: if this push's sha is STILL the
        latest registered for its (repo,branch) — i.e. nothing newer is queued behind it — its bookkeeping is
        spent, so drop both the _latest_push key and any matching _coalesced entry. CONCURRENCY-SAFE: if a NEWER
        push registered while this one was processing, _latest_push no longer equals our sha → we leave the entry
        untouched so that newer push still rebuilds (the coalesce 'never leave a stale graph' invariant holds). All
        access stays under _push_lock (same lock submit()/_push_coalesce use). Best-effort: a malformed payload
        (no registry key) is a no-op."""
        registry = self.registry_key(payload)
        if registry is None:
            return
        rb_key, sha = registry
        with self._push_lock:
            # Only reclaim when WE are still the latest (no newer push queued behind us). If a newer push advanced
            # _latest_push past our sha, leave both maps as-is so that newer push coalesce-decides correctly.
            if self._latest_push.get(rb_key) == sha:
                self._latest_push.pop(rb_key, None)
                self._coalesced.discard(rb_key)

    def coalesce(self, repo: str, branch: str, sha: str) -> str:
        """The coalescing decision for a main-branch push about to re-ingest (called by ingest_push on the worker):
          'skip'   — a NEWER push for this (repo,branch) is already queued → record facts, skip the re-ingest;
          'full'   — this IS the latest but earlier pushes were skipped → REQUIRE a full re-ingest;
          'normal' — no coalescing in play → today's incremental/full choice.
        FAIL-SAFE: any uncertainty → 'normal' (re-ingest). 'skip' is returned only when a newer push is provably
        queued, so coalescing can never leave a stale graph."""
        try:
            with self._push_lock:
                latest = self._latest_push.get((repo, branch))
                if latest is not None and latest != sha:
                    self._coalesced.add((repo, branch))      # skipping this one → the latest must full-reingest
                    return "skip"
                if (repo, branch) in self._coalesced:
                    self._coalesced.discard((repo, branch))
                    return "full"
                return "normal"
        except Exception:
            return "normal"

    def stable_coalesce(self, cache: dict):
        """A per-event memoizing wrapper over coalesce(), used for the in-process RETRY of a push. The coalesce
        decision is DESTRUCTIVE — 'skip' arms _coalesced for the (repo,branch) and 'full' CONSUMES it. If a retry
        recomputed the decision, a push that decided 'full' on attempt 1 (its _coalesced entry already cleared)
        would downgrade to 'normal' on attempt 2 → an incremental patch that misses the skipped pushes' files → a
        stale graph while the delivery looks processed. So within ONE dequeued event we compute the decision ONCE
        per (repo,branch,sha) and REPLAY it on every retry, mutating the shared coalescing state only on the first
        call. (Different args within one event — a multi-branch payload — each memoize once.)"""
        def coalesce(repo, branch, sha):
            k = (repo, branch, sha)
            if k not in cache:
                cache[k] = self.coalesce(repo, branch, sha)
            return cache[k]
        return coalesce
