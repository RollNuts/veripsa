#!/usr/bin/env python3
"""ALL-REPOSITORIES ONBOARDING gate — the SILENT-INSTALL-FOR-ORGS fix.

THE DEFECT this locks: GitHub's `installation.created` webhook payload OMITS the `repositories` array when the
install scope is "All repositories" (repository_selection == "all") — the DEFAULT, most-common org install path.
The App's own offboarding handler already accounts for this (purge_account_working_set is account-WIDE precisely
because "GitHub omits the `repositories` array for an 'All repositories' install"). But the ONBOARDING handler
read ONLY payload['repositories'] and, finding it absent, onboarded ZERO repos: no durable graph work, no open-PR
backfill, and no "Veripsa is now watching" signal. An org grants code-read on its WHOLE account and sees TOTAL
SILENCE until each repo's next push to main — the exact dead-first-impression _post_watching_signal exists to
prevent, defeated for the All-repos install because the iterating loop had nothing to iterate.

THE FIX (ingest._onboard_repos): when the payload names NO repos (the All-repos / "selected but the array was
omitted/redelivered empty" case) AND the installation client can enumerate its own repos, FALL BACK to
gh.installation_repos(cap=_ONBOARD_REPO_CAP) — the SAME source boot_reconcile already self-heals from. Bounded
by the onboard cap (a huge org can't make one install storm the API); guarded (no installation_repos method, or
a transient list error → onboard nothing, never crash — the live webhooks remain the primary path); and ONLY a
fallback (a payload that DID name repos is honored exactly as before — happy path unchanged).

This gate is PURE + OFFLINE (no Postgres, no network): a recording fake drives the real ingest._onboard_repos +
the real handle_event onboarding route. Without the fix, ONBOARD-1/2/3 FAIL (zero onboarded on an All-repos
payload); with it they pass. The selected-install path (a payload WITH repositories) is asserted UNCHANGED.

Run:  python3 tests/test_onboard_all_repos.py     (no DB needed)
"""
from __future__ import annotations

import io
import os
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)

import ingest  # noqa: E402
import event_processor  # noqa: E402
import render_pauseack  # noqa: E402
import server as S  # noqa: E402
import webhook_handlers  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")

FAIL = 0


def check(cond: bool, label: str):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


class OnboardGitHub:
    """A recording fake. `selected_repos` are the repo full_names this installation can ENUMERATE via
    installation_repositories (what an All-repos install must fall back to). The live installation path must
    enqueue graph work without downloading a tarball; this fake records any accidental clone plus each visible
    indexing/watching Check."""

    def __init__(self, account_repos):
        self.account_repos = list(account_repos)
        self.cloned = []
        self.watching_checks = []           # (repo, sha) of each 'watching' check posted
        self.list_repos_calls = 0
        self.default_head_calls = 0
        self.open_pr_calls = 0
        self._chid = 5000

    def for_installation(self, installation_id):
        return self

    # the All-repos fallback source (same one boot_reconcile uses)
    def installation_repos(self, cap=200):
        self.list_repos_calls += 1
        return self.account_repos[:cap]

    def installation_repo_entries(self, cap=200):
        self.list_repos_calls += 1
        return [{"full_name": repo, "id": str(10000 + i)}
                for i, repo in enumerate(self.account_repos[:cap])]

    def repo_default_branch_head(self, repo):
        import hashlib
        self.default_head_calls += 1
        return "main", hashlib.sha1(repo.encode()).hexdigest()

    def download_tarball(self, repo, sha):
        self.cloned.append(repo)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(FIXTURE, arcname="acct-" + sha[:7])
        return buf.getvalue()

    def get_file_at(self, repo, path, ref):
        full = os.path.join(FIXTURE, path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()

    def list_open_pull_requests(self, repo, limit=None):
        self.open_pr_calls += 1
        return []

    # the 'watching' check channel (upsert path the onboarding signal uses)
    def list_check_runs(self, repo, sha):
        return [c for c in self.watching_checks if c["repo"] == repo and c["sha"] == sha]

    def upsert_check(self, repo, sha, conclusion, title, summary):
        self._chid += 1
        self.watching_checks.append({"id": self._chid, "repo": repo, "sha": sha, "name": "Veripsa"})
        return self.watching_checks[-1]


_FAKE_GRAPH_COORDINATES = {}
_FAKE_GRAPH_HASH = "c" * 64
_FAKE_GRAPH_REQUESTS = []
_FAKE_GRAPH_EPOCH = 0


def _fake_db(sql, params=None):
    """Offline DB double with onboarding authority and writer/readback acknowledgements."""
    global _FAKE_GRAPH_EPOCH
    if "repository_account_onboarding_allowed_with_authority" in sql:
        return True
    if "reconcile_repo_identity_with_authority" in sql and params:
        return {
            "ok": True,
            "reconciled": False,
            "activation_recorded": True,
            "repo": params[0],
            "repo_id": str(params[1]),
        }
    if "ingest_graph_with_authority" in sql and params:
        coordinate = (params[1], params[2])
        _FAKE_GRAPH_COORDINATES[coordinate] = {
            "commit_sha": params[3],
            "graph_hash": _FAKE_GRAPH_HASH,
            "semantic_ref_version": ingest._SEMANTIC_REF_VERSION,
        }
        return {
            "ok": True,
            "graph_hash": _FAKE_GRAPH_HASH,
            "semantic_ref_version": ingest._SEMANTIC_REF_VERSION,
        }
    if "patch_graph_with_authority" in sql and params:
        coordinate = (params[1], params[2])
        _FAKE_GRAPH_COORDINATES[coordinate] = {
            "commit_sha": params[5],
            "graph_hash": _FAKE_GRAPH_HASH,
            "semantic_ref_version": ingest._SEMANTIC_REF_VERSION,
        }
        return {
            "ok": True,
            "mode": "patch",
            "commit_sha": params[5],
            "graph_hash": _FAKE_GRAPH_HASH,
            "semantic_ref_version": ingest._SEMANTIC_REF_VERSION,
        }
    if "coordinate_graph_sha" in sql:
        return dict(_FAKE_GRAPH_COORDINATES.get((params[0], params[1]), {}))
    if "enqueue_repository_onboarding_with_authority" in sql and params:
        _FAKE_GRAPH_EPOCH += 1
        _FAKE_GRAPH_REQUESTS.append((params[0], None, None, params[1]))
        return _FAKE_GRAPH_EPOCH
    if "enqueue_graph_refresh_with_authority" in sql and params:
        _FAKE_GRAPH_EPOCH += 1
        _FAKE_GRAPH_REQUESTS.append(tuple(params))
        return _FAKE_GRAPH_EPOCH
    if "coordinate_paths" in sql or "coordinate_inert" in sql:
        return []
    return None


def _all_repos_payload():
    # An "All repositories" install: repository_selection == 'all', and GitHub OMITS the `repositories` array.
    return {"action": "created",
            "installation": {"id": 4242, "account": {"id": 999, "login": "bigorg"}},
            "repository_selection": "all"}


def _selected_payload(repos):
    return {"action": "created",
            "installation": {"id": 4242, "account": {"id": 999, "login": "bigorg"}},
            "repository_selection": "selected",
            "repositories": [{"full_name": r, "id": str(20000 + i)}
                             for i, r in enumerate(repos)]}


def main() -> int:
    global _FAKE_GRAPH_EPOCH
    _FAKE_GRAPH_COORDINATES.clear()
    _FAKE_GRAPH_REQUESTS.clear()
    _FAKE_GRAPH_EPOCH = 0

    # No co-change clone in a unit test (the dedicated pool is exercised by its own gate); stub the dispatch so
    # onboarding's STEP 1b is a content-free no-op here.
    ingest.populate_cochange_async = lambda gh, repo, branch, window=800, repository_id=None: True
    ingest._ONBOARD_REPO_CAP = 50

    ORG_REPOS = ["bigorg/api", "bigorg/web", "bigorg/infra"]

    # ── ONBOARD-1/2/3: the All-repos install (NO `repositories` array) still onboards via the enumerate fallback.
    print("-- All-repositories install (repositories array omitted) --")
    gh = OnboardGitHub(account_repos=ORG_REPOS)
    res = S.handle_event("installation", _all_repos_payload(), _fake_db, gh)
    onboarded = [r.get("backfilled") for r in (res.get("onboarded") or [])]
    check(sorted(onboarded) == sorted(ORG_REPOS),
          f"ONBOARD-1 an All-repos install onboards every account repo via installation_repos fallback "
          f"(got {sorted(onboarded)})")
    graphs = [r.get("graph", {}) for r in (res.get("onboarded") or [])]
    requested = sorted(p[0] for p in _FAKE_GRAPH_REQUESTS)
    check(gh.cloned == []
          and all(g.get("queued") is True and g.get("indexing") is True for g in graphs)
          and requested == sorted(ORG_REPOS),
          f"ONBOARD-2 each repo is durably queued without cloning on the live worker "
          f"(cloned={gh.cloned}, requested={requested})")
    deferred_surfaces = [r.get("open_prs_deferred") for r in (res.get("onboarded") or [])]
    check(gh.watching_checks == [] and gh.default_head_calls == 0 and gh.open_pr_calls == 0
          and deferred_surfaces == ["durable_account_convergence"] * len(ORG_REPOS),
          "ONBOARD-3 live installation performs zero HEAD/PR/Check I/O; each visible surface is durably deferred "
          f"(head_reads={gh.default_head_calls}, pr_reads={gh.open_pr_calls}, checks={len(gh.watching_checks)})")
    check(gh.list_repos_calls >= 1, "ONBOARD-4 the fallback actually called installation_repos (the All-repos source)")

    # ── ONBOARD-5: BOUNDED — the enumerate fallback still honors _ONBOARD_REPO_CAP (a huge org can't storm the API).
    print("-- bounded: a huge org's fallback respects the onboard cap --")
    ingest._ONBOARD_REPO_CAP = 2
    gh2 = OnboardGitHub(account_repos=["o/r%d" % i for i in range(10)])
    res2 = S.handle_event("installation", _all_repos_payload(), _fake_db, gh2)
    onboarded2 = res2.get("onboarded") or []
    deferred2 = res2.get("deferred_repos")
    check(len(onboarded2) == 2, f"ONBOARD-5 the fallback onboards at most _ONBOARD_REPO_CAP repos (got {len(onboarded2)})")
    check(deferred2 == 8, f"ONBOARD-6 the rest are honestly DEFERRED (cold-start on first push) — deferred={deferred2}")
    ingest._ONBOARD_REPO_CAP = 50

    # ── ONBOARD-7: NEVER-CRASH — a client with NO installation_repos (an older fake) onboards nothing, no exception.
    print("-- never-crash: no enumerate method → onboard nothing, no raise --")
    class BareGH:
        def for_installation(self, i): return self
    res3 = S.handle_event("installation", _all_repos_payload(), _fake_db, BareGH())
    check((res3.get("onboarded") or []) == [],
          "ONBOARD-7 an installation client that cannot enumerate repos onboards nothing (no crash, live webhooks remain primary)")

    # ── ONBOARD-8: NEVER-CRASH — installation_repos raising a transient error must not abort the install.
    print("-- never-crash: a transient enumerate error degrades to 'onboard nothing' --")
    class RaisingGH(OnboardGitHub):
        def installation_repo_entries(self, cap=200):
            raise RuntimeError("api down")

        def installation_repos(self, cap=200):
            raise RuntimeError("api down")
    gh4 = RaisingGH(account_repos=ORG_REPOS)
    res4 = S.handle_event("installation", _all_repos_payload(), _fake_db, gh4)
    check((res4.get("onboarded") or []) == [],
          "ONBOARD-8 a transient installation_repos error degrades to onboard-nothing (never aborts the install)")

    # The live processor performs inventory discovery BEFORE the shared transaction so discovered repositories can
    # fan out under their own locks. A first empty/failure must not be retried by _onboard_repos inside that shared
    # transaction: a stateful second read could return a repo and cold-start it without a repository lock.
    class EmptyThenVisibleGitHub(OnboardGitHub):
        def installation_repo_entries(self, cap=200):
            self.list_repos_calls += 1
            return [] if self.list_repos_calls == 1 else [{"full_name": "bigorg/late", "id": "51515"}]

    empty_then_visible = EmptyThenVisibleGitHub(account_repos=[])
    empty_payload = _all_repos_payload()
    first_empty = event_processor._install_allrepos_onboard_repos(
        "installation", empty_payload, empty_then_visible)
    empty_result = S.handle_event("installation", empty_payload, _fake_db, empty_then_visible)
    check(first_empty == [] and empty_then_visible.list_repos_calls == 1
          and (empty_result.get("onboarded") or []) == [] and empty_then_visible.cloned == [],
          "ONBOARD-19 an empty outer inventory read is not repeated inside the unlocked shared transaction")

    class FailureThenVisibleGitHub(OnboardGitHub):
        def installation_repo_entries(self, cap=200):
            self.list_repos_calls += 1
            if self.list_repos_calls == 1:
                raise RuntimeError("transient inventory failure")
            return [{"full_name": "bigorg/late", "id": "51515"}]

    failure_then_visible = FailureThenVisibleGitHub(account_repos=[])
    failed_payload = _all_repos_payload()
    first_failed = event_processor._install_allrepos_onboard_repos(
        "installation", failed_payload, failure_then_visible)
    failed_result = S.handle_event("installation", failed_payload, _fake_db, failure_then_visible)
    check(first_failed == [] and failure_then_visible.list_repos_calls == 1
          and (failed_result.get("onboarded") or []) == [] and failure_then_visible.cloned == [],
          "ONBOARD-20 a failed outer inventory read is not repeated inside the unlocked shared transaction")

    class BudgetExpiredGitHub(OnboardGitHub):
        def installation_repo_entries(self, cap=200):
            raise event_processor._event_budget.EventBudgetExceeded(
                "installation inventory exhausted the event budget")

    budget_expired = False
    try:
        event_processor._install_allrepos_onboard_repos(
            "installation", _all_repos_payload(), BudgetExpiredGitHub(account_repos=[]))
    except event_processor._event_budget.EventBudgetExceeded:
        budget_expired = True
    check(budget_expired,
          "ONBOARD-24 an all-repositories inventory deadline is re-raised, never converted to a successful empty install")

    # Positive control: no processor marker means this is a legitimate direct dispatch and its fallback remains.
    direct_gh = OnboardGitHub(account_repos=["bigorg/direct"])
    direct_result = S.handle_event("installation", _all_repos_payload(), _fake_db, direct_gh)
    check([r.get("backfilled") for r in (direct_result.get("onboarded") or [])] == ["bigorg/direct"]
          and direct_gh.list_repos_calls == 1 and direct_gh.cloned == []
          and direct_result["onboarded"][0].get("graph", {}).get("queued") is True,
          "ONBOARD-21 direct installation dispatch still performs legitimate All-repositories discovery")

    empty_delta_gh = OnboardGitHub(account_repos=["bigorg/must-not-be-discovered"])
    empty_delta_result = S.handle_event(
        "installation_repositories",
        {"action": "added", "installation": {"id": 4242, "account": {"id": 999}},
         "repositories_added": []},
        _fake_db, empty_delta_gh)
    check((empty_delta_result.get("onboarded") or []) == [] and empty_delta_gh.list_repos_calls == 0
          and empty_delta_gh.cloned == [],
          "ONBOARD-22 an empty repository-added delta is authoritative and never enumerates the whole install")

    legacy_onboard_calls = []
    real_onboard = webhook_handlers._queue_onboard_repos
    try:
        def legacy_three_arg_onboard(_db, _gh, repos):
            legacy_onboard_calls.append(list(repos))
            return [], 0

        webhook_handlers._queue_onboard_repos = legacy_three_arg_onboard
        legacy_result = webhook_handlers._handle_installation_event(
            "installation_repositories",
            {"action": "added", "installation": {"id": 4242, "account": {"id": 999}},
             "repositories_added": []},
            _fake_db, empty_delta_gh)
    finally:
        webhook_handlers._queue_onboard_repos = real_onboard
    check((legacy_result.get("onboarded") or []) == [] and legacy_onboard_calls == [[]],
          "ONBOARD-23 historical injected three-argument onboarding seams remain callable")

    # ── ONBOARD-9: HAPPY PATH UNCHANGED — a SELECTED install (payload NAMES repos) is honored exactly as before;
    #    the enumerate fallback is NEVER consulted when the payload already named repos.
    print("-- happy path unchanged: a selected install does NOT consult the fallback --")
    gh5 = OnboardGitHub(account_repos=["bigorg/SHOULD-NOT-APPEAR"])
    res5 = S.handle_event("installation", _selected_payload(["bigorg/api", "bigorg/web"]), _fake_db, gh5)
    onboarded5 = sorted(r.get("backfilled") for r in (res5.get("onboarded") or []))
    check(onboarded5 == ["bigorg/api", "bigorg/web"],
          f"ONBOARD-9 a selected install onboards exactly the named repos (got {onboarded5})")
    check(gh5.list_repos_calls == 0,
          "ONBOARD-10 the enumerate fallback is NOT consulted when the payload already named repos (happy path unchanged)")

    # ── ONBOARD-11: AUTHORITY MUST BE OBSERVED — a schema/adapter drift returning None cannot become permission
    #    to clone. The durable worker retries the installation delivery after the DB contract is restored.
    def unobserved_authority_db(sql, params=None):
        if "repository_account_onboarding_allowed_with_authority" in sql:
            return None
        return _fake_db(sql, params)

    authority_failed_closed = False
    try:
        S.handle_event("installation", _selected_payload(["bigorg/api"]), unobserved_authority_db,
                       OnboardGitHub(account_repos=[]))
    except RuntimeError as exc:
        authority_failed_closed = "authority was not observed" in str(exc)
    check(authority_failed_closed,
          "ONBOARD-11 a missing repository authority result fails closed for durable retry (never cold-starts)")

    # ── ONBOARD-12: IDENTITY VERIFICATION MOVED OFF THE LIVE WORKER. The signed stable id must be present in the
    #    durable graph request; graph-write identity reconciliation belongs to strict background convergence.
    #    Calling it synchronously here would reintroduce graph work into the installation delivery.
    identity_calls = []

    def failed_identity_db(sql, params=None):
        if "repository_account_onboarding_allowed_with_authority" in sql:
            return True
        if "reconcile_repo_identity_with_authority" in sql:
            identity_calls.append(tuple(params or ()))
            return {"ok": False}
        return _fake_db(sql, params)

    identity_payload = _selected_payload(["bigorg/api"])
    identity_payload["repositories"][0]["id"] = "19191"
    identity_result = S.handle_event(
        "installation", identity_payload, failed_identity_db, OnboardGitHub(account_repos=[]))
    check(len(identity_result.get("onboarded") or []) == 1
          and identity_result["onboarded"][0].get("graph", {}).get("queued") is True
          and identity_calls == []
          and _FAKE_GRAPH_REQUESTS[-1][0] == "bigorg/api"
          and str(_FAKE_GRAPH_REQUESTS[-1][3]) == "19191",
          "ONBOARD-12 the live install durably records stable identity without synchronous graph reconciliation")

    # ── ONBOARD-13: NAME-ONLY BACKFILL PRESERVES CURRENT GITHUB ID WITHOUT WEAKENING THE GENERIC WORK GUARD.
    #    List/get PR responses already contain base.repo.id. Promote it only when base.repo.full_name matches the
    #    requested coordinate; a malformed cross-repo base object must not lend its id to the synthetic replay.
    class NameOnlyBackfillGitHub:
        def list_open_pull_requests(self, repo, limit=None):
            return [
                {"number": 71,
                 "base": {"ref": "main", "sha": "c" * 40,
                          "repo": {"full_name": repo, "id": 31337}},
                 "head": {"sha": "a" * 40, "ref": "fork-feature",
                          "repo": {"full_name": "outside/api", "id": 42424}},
                 "user": {"login": "agent-a"}, "draft": True, "changed_files": 3,
                 "labels": [{"name": "veripsa-ack"}, {"name": "reviewed"}]},
                {"number": 72,
                 "base": {"ref": "main", "repo": {"full_name": "other/repo", "id": 99999}},
                 "head": {"sha": "b" * 40}, "user": {"login": "agent-b"}, "draft": "false"},
            ]

    replay_payloads = []
    _real_handle_event = S.handle_event
    try:
        S.handle_event = lambda event_type, payload, db, gh: (
            replay_payloads.append(payload), {"event": event_type})[1]
        replay = ingest.backfill_open_prs(
            _fake_db, NameOnlyBackfillGitHub(), "bigorg/api", default_branch="main")
    finally:
        S.handle_event = _real_handle_event
    check(replay.get("count") == 2
          and replay_payloads[0]["repository"].get("id") == "31337"
          and "id" not in replay_payloads[1]["repository"],
          "ONBOARD-13 name-only backfill carries matching base.repo.id but rejects a foreign coordinate id")
    short_circuit, replay_fields = webhook_handlers._pr_eligibility(replay_payloads[0], _fake_db)
    replay_pr = replay_payloads[0]["pull_request"]
    check(short_circuit is None and replay_fields.get("is_fork") is True
          and replay_pr["base"].get("sha") == "c" * 40
          and replay_pr["head"].get("ref") == "fork-feature"
          and replay_pr.get("draft") is True and replay_pr.get("changed_files") == 3
          and replay_pr.get("labels") == [{"name": "veripsa-ack"}, {"name": "reviewed"}],
          "ONBOARD-16 backfill preserves fork, freshness, draft, changed-file, and ACK metadata")
    sparse_short_circuit, sparse_fields = webhook_handlers._pr_eligibility(replay_payloads[1], _fake_db)
    redacted_event = webhook_handlers._pr_build_event(
        sparse_fields, [], {}, {}, [], False, False, False, "inferred")
    sparse_overlay = render_pauseack.apply_pause_ack(
        {"conclusion": "neutral", "title": "redacted", "summary": "redacted", "comment": "redacted"},
        {"changes": [{"change_id": "PR-72", "verdict": "serialize", "paths": ["private/path.py"],
                      "serialize_behind": [{"change_id": "PR-1"}]}]},
        "PR-72", label_present=False, prior_hash=None, is_fork=sparse_fields.get("is_fork", False))
    check(sparse_short_circuit is None and sparse_fields.get("is_fork") is False
          and sparse_fields.get("redact_external") is True and redacted_event.get("is_fork") is True
          and sparse_overlay.get("conclusion") == "action_required"
          and replay_payloads[1].get("_veripsa_fork_identity_unknown") is True
          and replay_payloads[1]["pull_request"].get("draft") is False,
          "ONBOARD-17 unknown fork identity redacts without disabling merge gating; malformed draft stays strict")
    check(ingest._repo_id_from_payload({"repository": {"id": "00031337"}}) == "31337"
          and ingest._repo_id_from_payload({"repository": {"id": 0}}) is None
          and ingest._repo_id_from_payload({"repository": {"id": True}}) is None
          and ingest._repo_id_from_payload({"repository": {"id": "²"}}) is None
          and ingest._repo_id_from_payload({"repository": {"id": "١٢"}}) is None,
          "ONBOARD-18 repository ids are positive canonical integers")

    # ── ONBOARD-14/15: REPOSITORY ADD MUST OBSERVE DURABLE LIFECYCLE AUTHORITY BEFORE CLONE/BACKFILL.
    #    The emergency VERIPSA_DURABLE_INBOX=0 path supplies no delivery key. It must fail closed rather than rebuild
    #    a graph behind a tombstone; a current DB-observed lifecycle result remains the positive control.
    def added_payload(delivery_key=None):
        payload = {
            "action": "added",
            "installation": {"id": 4242, "account": {"id": 999, "login": "bigorg"}},
            "repositories_added": [{"full_name": "bigorg/readded", "id": "41414"}],
        }
        if delivery_key is not None:
            payload["_veripsa_delivery_key"] = delivery_key
        return payload

    no_authority_gh = OnboardGitHub(account_repos=[])
    no_authority = S.handle_event(
        "installation_repositories", added_payload(), _fake_db, no_authority_gh)
    check(no_authority.get("onboarded") == []
          and no_authority.get("stale_repositories_skipped") == 1
          and no_authority_gh.cloned == [],
          "ONBOARD-14 repository add without durable lifecycle authority never clones or onboards")

    def observed_lifecycle_db(sql, params=None):
        if "reactivate_repository_with_authority" in sql:
            return {"ok": True, "cleared": 0, "activated": True, "stale_lifecycle_event": False}
        return _fake_db(sql, params)

    observed_gh = OnboardGitHub(account_repos=[])
    observed = S.handle_event(
        "installation_repositories", added_payload("D-OBSERVED-ADD"), observed_lifecycle_db, observed_gh)
    check(len(observed.get("onboarded") or []) == 1
          and observed.get("stale_repositories_skipped") == 0
          and observed_gh.cloned == []
          and observed["onboarded"][0].get("graph", {}).get("queued") is True
          and _FAKE_GRAPH_REQUESTS[-1][0] == "bigorg/readded"
          and str(_FAKE_GRAPH_REQUESTS[-1][3]) == "41414",
          "ONBOARD-15 current DB-observed repository add durably queues without cloning on the live worker")

    print("ALL-REPOS ONBOARDING GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
