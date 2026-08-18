#!/usr/bin/env python3
"""ALL-REPOSITORIES INSTALL — PER-REPO ONBOARDING ISOLATION GATE.

THE DEFECT this locks (HIGH, onboarding-scale): GitHub OMITS the `repositories` array from the
`installation.created` webhook when the install scope is "All repositories" (repository_selection == 'all') —
the DEFAULT, most-common org install. event_processor.make_db_processor handles the EXPLICIT-list install via
_process_install_event_per_repo_locked (each repo onboarded in its OWN advisory lock + OWN connection + OWN
transaction → one repo's DB error can never roll back the others). But the ALL-REPOS install named no repos, so
the fan-out returned [] and the event FELL THROUGH to the single shared body transaction (repo None → NO lock):
handle_event → ingest._onboard_repos then ENUMERATES the install's repos (gh.installation_repos) and cold-starts
up to ~50 of them ALL inside that ONE transaction. So a single DB error on ONE repo mid-onboarding ABORTED the
shared transaction → every LATER repo's writes failed with "current transaction is aborted" → the final commit
rolled the WHOLE org's onboarding back. One poison repo SILENTLY blocked the entire org from ever onboarding.

THE FIX (event_processor.make_db_processor): when an install-level ONBOARDING action named no repos, ENUMERATE
the install's repos (gh.installation_repos — the SAME source boot_reconcile/_onboard_repos already use) and route
them through the SAME per-repo LOCKED fan-out the explicit-list path uses. Each repo onboards in its OWN locked
transaction, so one repo's failure is contained and the rest still onboard. Bounded by the eager onboard cap +
fail-open (enumerate nothing → the empty-onboard body, never crash).

WHAT THIS GATE PROVES (driven through the REAL make_db_processor over the REAL gate, authed as the REAL
least-privilege role veripsa_app, against a scratch DB — the exact prod transaction model):

  ALLREPOS-1  REPRODUCE-ON-ORIGIN-SEMANTICS / FIX (the load-bearing assertion): an All-repos install whose
              enumerated set is [good-A, POISON, good-C] where POISON raises a real DB error while durably
              enqueueing its graph — the OTHER repos (good-A, good-C) are STILL queued. The live webhook never
              clones/extracts; queue commit is the onboarding durability boundary.
  ALLREPOS-2  THE POISON IS ISOLATED, NOT SILENTLY SWALLOWED: the poison repo has no committed queue row — its
              failed transaction cannot leave half-onboarded graph work.
  ALLREPOS-3  IT REALLY TOOK THE ALL-REPOS PATH: the enumerate fallback (installation_repos) was consulted —
              i.e. this is the all-repos branch, not the named-list branch.
  ALLREPOS-4  A post-ingest stable-identity failure escapes the bounded sweep so the durable delivery retries.
  ALLREPOS-5  Successful siblings from that retry scenario commit on their own transactions.
  ALLREPOS-6  The failed repo's earlier graph enqueue is rolled back atomically instead of surviving half-onboarded.

Run:  python3 tests/test_allrepos_onboard.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)
import server as S        # noqa: E402  (re-exports make_db_processor / handle_event / _scoped_db)
import ingest             # noqa: E402  (the onboarding cluster; we patch backfill_repo to inject one poison repo)

DB = "veripsa_allrepos_iso_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"          # the REAL prod least-privilege role
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")

ACCOUNT = 8181                                                # the owning-account id (stable tenant key)
INSTALL_ID = 9292
GOOD_A, POISON, GOOD_C = "bigorg/good-a", "bigorg/poison", "bigorg/good-c"
ENUM_REPOS = [GOOD_A, POISON, GOOD_C]                         # POISON in the MIDDLE: on origin/main it strands good-c after it
RETRY_GOOD_A, RETRY_POISON, RETRY_GOOD_C = (
    "bigorg/retry-good-a", "bigorg/retry-poison", "bigorg/retry-good-c")
RETRY_REPOS = [RETRY_GOOD_A, RETRY_POISON, RETRY_GOOD_C]


class AllReposGitHub:
    """Recording fake for an All-repositories install. installation_repos enumerates the org's repos (the
    all-repos fallback source). Serves the sample_app fixture so each good repo's onboarding ingests a REAL
    graph that COMMITS. Records nothing it does not need — content-free."""

    def __init__(self, account_id, account_repos):
        self.account_id = account_id
        self.account_repos = list(account_repos)
        self.list_repos_calls = 0
        self.checks, self.comments = [], []
        self._chid = 3000

    def for_installation(self, installation_id):
        return self

    def app_installation_identity(self, installation_id):
        return {"installation_id": str(installation_id), "account_id": str(self.account_id),
                "created_at": "2026-01-01T00:00:00Z", "suspended": False}

    def installation_account_id(self):
        return str(self.account_id)

    # the All-repos fallback source (the SAME method boot_reconcile / _onboard_repos enumerate from)
    def installation_repos(self, cap=200):
        self.list_repos_calls += 1
        return self.account_repos[:cap]

    def installation_repo_entries(self, cap=200):
        import hashlib
        self.list_repos_calls += 1
        return [{"full_name": repo,
                 "id": str(100000000 + int(hashlib.sha1(repo.encode()).hexdigest()[:10], 16))}
                for repo in self.account_repos[:cap]]

    def repo_default_branch_head(self, repo):
        import hashlib
        return "main", hashlib.sha1(repo.encode()).hexdigest()

    def download_tarball(self, repo, sha):
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
        return []

    # the 'watching' check channel (onboarding posts one per repo) — recorded so the onboarding path runs fully
    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c.get("repo") == repo and c.get("sha") == sha]

    def upsert_check(self, repo, sha, conclusion, title, summary):
        self._chid += 1
        self.checks.append({"id": self._chid, "repo": repo, "sha": sha, "name": "Veripsa"})
        return self.checks[-1]


def _all_repos_payload():
    # An "All repositories" install: repository_selection == 'all', and GitHub OMITS the `repositories` array.
    return {"action": "created",
            "installation": {"id": INSTALL_ID, "account": {
                "id": ACCOUNT, "login": "bigorg", "type": "Organization"}},
            "repository_selection": "all"}


def _deliver_created(gh, delivery_key):
    """Call the live processor from the state its durable wrapper normally establishes."""
    payload = _all_repos_payload()
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "INSERT INTO core.webhook_delivery("
                "delivery_key,event_type,account_key,payload,status,received_at) "
                "VALUES (%s,'installation',%s,%s::jsonb,'processing',clock_timestamp())",
                (delivery_key, str(ACCOUNT), json.dumps(payload)),
            )
    finally:
        conn.close()
    payload["_veripsa_delivery_key"] = delivery_key
    return S.make_db_processor(DSN_APP)("installation", payload, None, gh)


def _graph_queued(repo: str) -> bool:
    """Fresh-session readback of the live onboarding durability boundary."""
    # Scratch-cluster superuser read: FORCE RLS deliberately hides another
    # tenant from both App and migrator sessions unless they carry the exact
    # execution route. This assertion observes committed state, not a buyer
    # read surface.
    conn = psycopg2.connect(f"postgresql://localhost/{DB}")
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SELECT count(*)::int FROM core.policy_refresh_outbox "
                "WHERE request_kind='graph' AND repo=%s AND done_at IS NULL",
                (repo,),
            )
            row = cur.fetchone()
        return bool(row and row[0] == 1)
    finally:
        conn.close()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    # No co-change clone in this unit (the live onboarding path must not start one).
    ingest.populate_cochange_async = lambda gh, repo, branch, window=800, repository_id=None: True

    # INJECT exactly ONE poison repo at the queue write. Live onboarding is metadata+DB only: clone/extract is
    # forbidden here. The deliberate DB error aborts only this repository's per-repo transaction.
    _real_request = ingest.request_repository_onboarding

    def _poison_request(db, repo, repository_id, **kwargs):
        if repo == POISON:
            db("SELECT 1 / 0")          # raises division_by_zero → aborts the current transaction
        return _real_request(db, repo, repository_id, **kwargs)

    checks = []
    poison_retry_raised = False
    try:
        ingest.request_repository_onboarding = _poison_request
        gh = AllReposGitHub(ACCOUNT, ENUM_REPOS)
        # Drive the All-repos install through the REAL live processor (the exact prod transaction model).
        try:
            _deliver_created(gh, "allrepos-created-poison")
        except RuntimeError as exc:
            # The final budget-tightening SET also proves the per-repo
            # transaction is still committable. A handler-swallowed DB error
            # leaves it aborted, so the failed repo must remain durable/retryable
            # after the healthy sibling transactions commit.
            poison_retry_raised = "failed for 1 repository" in str(exc)
    finally:
        ingest.request_repository_onboarding = _real_request

    good_a_ok = _graph_queued(GOOD_A)
    good_c_ok = _graph_queued(GOOD_C)
    poison_ok = _graph_queued(POISON)

    # ALLREPOS-1 (load-bearing): the OTHER repos onboard despite one poison repo. FAILS on origin/main's
    # whole-event transaction; PASSES on the fix because each good repo commits its durable request separately.
    checks.append(("ALLREPOS-1 an All-repos install isolates a poison repo: the OTHER repos STILL onboard "
                   f"(graph queued) — good-a={good_a_ok}, good-c={good_c_ok} "
                   "(both must be True)",
                   good_a_ok and good_c_ok))

    # ALLREPOS-2: the poison is contained, not silently swallowed — it really did not onboard.
    checks.append(("ALLREPOS-2 the poison repo itself did NOT onboard (its DB error is real + contained to its "
                   f"own repo, never masking the others) — poison queued={poison_ok} (must be False)",
                   not poison_ok))
    checks.append(("ALLREPOS-2b the poison DB failure leaves the delivery retryable instead of finishing green",
                   poison_retry_raised))

    # ALLREPOS-3: this exercised the ALL-REPOS branch (the enumerate fallback was consulted), not the named path.
    checks.append(("ALLREPOS-3 it took the all-repos branch: the installation_repos enumerate fallback was "
                   f"consulted (calls={gh.list_repos_calls} ≥ 1)", gh.list_repos_calls >= 1))

    # IDENTITY AUTHORITY FAILURE: inject after the real queue write, before its transaction commits. It must escape
    # the inner queue loop and leave this delivery retryable. Successful siblings commit independently, while the
    # failed repo's request rolls back instead of surviving without its stable authority.
    # Use distinct coordinates from ALLREPOS-1/2 so no assertion can pass on state committed by the first scenario.
    def _identity_failure_request(db, repo, repository_id, **kwargs):
        result = _real_request(db, repo, repository_id, **kwargs)
        if repo == RETRY_POISON:
            raise ingest.RepositoryIdentityBindingError("identity binding unavailable")
        return result

    identity_retry_raised = False
    try:
        ingest.request_repository_onboarding = _identity_failure_request
        retry_gh = AllReposGitHub(ACCOUNT, RETRY_REPOS)
        try:
            _deliver_created(retry_gh, "allrepos-created-identity-retry")
        except RuntimeError as exc:
            identity_retry_raised = "failed for 1 repository" in str(exc)
    finally:
        ingest.request_repository_onboarding = _real_request
    retry_good_a_ok = _graph_queued(RETRY_GOOD_A)
    retry_good_c_ok = _graph_queued(RETRY_GOOD_C)
    retry_poison_ok = _graph_queued(RETRY_POISON)
    checks.append(("ALLREPOS-4 a repository identity authority failure escapes the sweep for durable retry",
                   identity_retry_raised))
    checks.append(("ALLREPOS-5 fresh successful sibling repositories commit when identity retry is raised "
                   f"(good-a={retry_good_a_ok}, good-c={retry_good_c_ok})",
                   retry_good_a_ok and retry_good_c_ok))
    checks.append(("ALLREPOS-6 post-enqueue identity failure rolls back that repo's request atomically "
                   f"(retry-poison queued={retry_poison_ok}, must be False)",
                   not retry_poison_ok))

    print("\n── ALL-REPOS ONBOARDING ISOLATION ──────────────────────────")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("\nALLREPOS ONBOARD ISOLATION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
    sys.exit(rc)
