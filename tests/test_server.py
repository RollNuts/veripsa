#!/usr/bin/env python3
"""Webhook SERVER gate — the full live loop, offline (no deploy, no GitHub account).

Drives github-app/server.handle_event with a FAKE GitHub client (records what would be posted) over the REAL
gate (db/schema.sql) authed as the App identity (veripsa_app, delegation). Proves: a real GitHub pull_request
payload → the brain → a check + comment posted to the right PR, naming the real author; a same-path second PR
→ serialize ("Wait in line"); a push to main → the graph is ingested from the tarball (content-free) + landing
recorded; and HMAC signature verification rejects forgeries. This is exactly what the live App does — only the
GitHub I/O is faked.

Run:  python3 tests/test_server.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

# The SELF-CONTAINED harness (FakeGitHub + the shared helpers + the per-process DB name + the REPO/SHA
# coordinates) lives in tests/_server_harness.py — extracted to shrink this god-file + cut its merge-conflict
# surface (Veripsa's own god-file signal). ROOT/DB/REPO/SHA, make_db, _json_scalar, FakeGitHub, pr_payload and
# _try_put are imported below and used UNCHANGED; the scenarios + the gate marker stay here.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _server_harness import (  # noqa: E402
    DB, REPO, REPO_ID, ROOT, SHA, FakeGitHub, _fixture_repo_id, _json_scalar, _try_put, make_db, pr_payload,
    seed_processing_delivery,
)

sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import server as S  # noqa: E402
import server_http as SH  # noqa: E402
# The graph-ingestion + reconciliation cluster (ingest_push / _full_ingest / backfill_open_prs / _onboard_repos
# + their private caps _MAX_INGEST_FILES / _BACKFILL_PR_CAP / _ONBOARD_REPO_CAP) lives in ingest.py; server
# re-exports the public names. A cap monkeypatch must rebind the constant WHERE IT LIVES (on `ingest`), because
# the cluster functions read it from ingest.py's own global — rebinding S.<cap> would leave that global (and so
# the behavior under test) unchanged. _MAX_PR_FILES stays on S (read by handle_event/reserve_branch_lanes there).
import ingest  # noqa: E402


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    db = make_db("veripsa_app")
    # This first block deliberately calls handle_event directly (the rest of the gate separately proves the
    # production make_db_processor tenant pin).  Give its local ACCT-DEMO fixture the scheduler route that a live
    # authenticated installation always has; otherwise the durable graph enqueue correctly refuses a tenant the
    # convergence worker cannot enumerate.  Keep 4242 free for the later real-route scenarios.
    with psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}") as fixture_conn:
        with fixture_conn.cursor() as fixture_cur:
            fixture_cur.execute(
                "INSERT INTO core.installation_account("
                "installation_id,account_id,github_installation_id,"
                "github_installation_created_at) "
                "VALUES (%s,%s,%s,%s::timestamptz) ON CONFLICT DO NOTHING",
                (
                    "424200000",
                    "ACCT-DEMO",
                    "424200000",
                    "2026-01-01T00:00:00Z",
                ),
            )
    sys.path.insert(0, ROOT)
    import cg_schema_contract as C
    import code_graph_extract as X
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", SHA))
    identity = _json_scalar(
        db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (REPO, str(REPO_ID)))
    )
    if (not identity.get("ok") or identity.get("repo") != REPO
            or str(identity.get("repo_id")) != str(REPO_ID)
            or identity.get("activation_recorded") is not True):
        print("server fixture repository identity bootstrap failed:", identity)
        return 1

    checks = []
    # PR-1 (alice) edits backend/auth.py; PR-2 (bob) edits backend/api.py which CALLS+IMPORTS auth → warn.
    # PR-3 (carol) edits backend/api.py too → same lane as PR-2 → serialize ("Wait in line").
    gh = FakeGitHub({1: ["backend/auth.py"], 2: ["backend/api.py"], 3: ["backend/api.py"]})
    r1 = S.handle_event("pull_request", pr_payload("opened", 1, "alice"), db, gh)
    c1_after_own_event = next((c for c in gh.comments if c["number"] == 1), None)
    c1_body_after_own_event = c1_after_own_event["body"] if c1_after_own_event else None
    c1_patches_after_own_event = (
        gh.patches.count(c1_after_own_event["id"]) if c1_after_own_event else 0)
    r2 = S.handle_event("pull_request", pr_payload("opened", 2, "bob"), db, gh)
    r3 = S.handle_event("pull_request", pr_payload("opened", 3, "carol"), db, gh)

    c1_before_convergence = next((c for c in gh.comments if c["number"] == 1), None)
    c2 = next((c for c in gh.comments if c["number"] == 2), None)
    c3 = next((c for c in gh.comments if c["number"] == 3), None)
    chk2 = gh.checks[1] if len(gh.checks) > 1 else {}
    # Every PR gets a CHECK; only the ones with something to coordinate (warn/serialize) get a COMMENT.
    checks.append(("server posts a check to EVERY PR + a marker comment only where there is coordination",
                   len(gh.checks) >= 3 and len(gh.comments) >= 2
                   and all(f"<!-- veripsa:PR-{c['number']} -->" in c["body"] for c in gh.comments)))
    # Neighbor surfaces are deliberately not posted inside the webhook transaction. The acting PR remains
    # immediate; the existing durable graph turn receives one coalesced wake and its bounded posting slice applies
    # the final current neighborhood. Simulate that isolated posting seam here after proving the live event did no
    # neighbor mutation; scheduler/lease/pagination losslessness is covered by test_policy_refresh_scheduler_scale.
    checks.append(("STALE-ON-OPEN: live events defer every sibling mutation to one durable convergence turn",
                   (c1_before_convergence["body"] if c1_before_convergence else None)
                   == c1_body_after_own_event
                   and (gh.patches.count(c1_after_own_event["id"]) if c1_after_own_event else 0)
                   == c1_patches_after_own_event
                   and r2.get("refreshed_inflight") == 0
                   and r3.get("refreshed_inflight") == 0
                   and r2.get("refresh_deferred", 0) >= 1
                   and r3.get("refresh_deferred", 0) >= 1))
    initial_worker_progress = S._post_refreshes(
        gh, REPO, r3.get("refreshed") or [], db=db, branch="main",
        return_progress=True,
    )
    c1 = next((c for c in gh.comments if c["number"] == 1), None)
    c2 = next((c for c in gh.comments if c["number"] == 2), None)
    c3 = next((c for c in gh.comments if c["number"] == 3), None)
    checks.append(("STALE-ON-OPEN: isolated convergence posts the final current warning for the foundation PR",
                   initial_worker_progress.get("posted", 0) >= 1
                   and bool(c1) and "Heads up" in c1["body"]
                   and ("bob" in c1["body"] or "carol" in c1["body"])))
    checks.append(("PR-2 (api.py) is WARNED: comment names the real author bob's neighbor + 'Heads up'",
                   bool(c2) and "Heads up" in c2["body"] and "alice" in c2["body"]))
    # PAUSE-ACK (一時停止): PR-2 is a MATERIAL coupling — a warn WITH an in-flight counterpart (alice's PR-1). The
    # pause-ack tier makes a material coupling post 'action_required' (NOT green) until the SPECIFIC coupling is
    # acknowledged (the veripsa-ack label). This is the tier that actually changes behavior — a 'neutral' advisory
    # was proven to change none. It is NOT a silent block: the comment offers the proceed-by-ack path (add the
    # label), so the merge is never gated WITHOUT a recorded, conscious acknowledgement the author can always give.
    checks.append(("PR-2 check conclusion is action_required (material warn coupling — paused until acknowledged)",
                   chk2.get("conclusion") == "action_required"))
    checks.append(("PR-2's comment offers the proceed-by-ack path (enable, not only stop)",
                   bool(c2) and "veripsa-ack" in c2["body"]))
    # PR-3 (same lane as PR-2) is a MATERIAL direct collision (serialize) → also 'action_required' until acked. The
    # full SIGNAL ("Wait in line") stays in the comment, plus the proceed-by-ack instruction. NO clear/green PR is
    # ever paused: every clear check stays 'success' (a pause is reserved for a REAL in-flight coupling).
    checks.append(("PR-3 (same lane as PR-2) is SERIALIZED: 'Wait in line' + paused (action_required) + ack path offered",
                   bool(c3) and "Wait in line" in c3["body"] and "veripsa-ack" in c3["body"]
                   and any(c["conclusion"] == "action_required" for c in gh.checks)
                   and all(c["conclusion"] in ("success", "neutral", "action_required") for c in gh.checks if c["sha"])))
    before_comments = len(gh.comments)
    before_patches, before_check_patches = len(gh.patches), len(gh.check_patches)
    S.handle_event("pull_request", pr_payload("synchronize", 2, "bob"), db, gh)
    # A replayed (identical) synchronize must NEVER duplicate the comment — and, with the no-churn fix, an
    # UNCHANGED verdict must not re-PATCH the comment OR check either (a content-identical PATCH bumps
    # updated_at + re-notifies = wallpaper). PR-2's cluster is unchanged by the replay, so the whole event
    # is a clean no-op: zero new comments, zero new patches.
    checks.append(("replayed PR delivery is a clean NO-OP — no duplicate comment AND no churn re-patch",
                   len(gh.comments) == before_comments
                   and len(gh.patches) == before_patches and len(gh.check_patches) == before_check_patches))
    checks.append(("server routes GitHub REST through the webhook installation id",
                   "4242" in gh.installations))

    # installation/backfill path: installing after PRs already exist must process every open PR.
    backfill_gh = FakeGitHub(
        {1: ["backend/auth.py"], 2: ["backend/api.py"]},
        open_prs=[
            {"number": 1, "base": {"ref": "main"}, "head": {"sha": "backfill-1"}, "user": {"login": "alice"}},
            {"number": 2, "base": {"ref": "main"}, "head": {"sha": "backfill-2"}, "user": {"login": "bob"}},
        ],
    )
    backfilled = S.backfill_open_prs(db, backfill_gh, REPO)
    checks.append(("backfill processes every currently-open PR and posts checks/comments",
                   backfilled["count"] == 2
                   and {c["number"] for c in backfill_gh.comments} == {1, 2}
                   and len(backfill_gh.checks) >= 2))

    # BOOT-RECONCILE PER-(ACCOUNT,REPO) ADVISORY LOCK (concurrency safety). boot_reconcile writes the SAME per-repo
    # rows as the live webhook path (make_db_processor), which serializes same-coordinate events under the two-arg
    # pg_advisory_lock(hashtext(account), hashtext(repo)). Without the SAME lock+key, a webhook for a repo arriving
    # WHILE boot reconciles that repo would interleave / double-write (the 2-instance overlap Render does on every
    # rolling deploy makes this a real race). Assert the SAME lock is HELD WHILE a repo's reconcile work runs, and
    # RELEASED before the next repo / when boot returns. PROBE FROM A SEPARATE SESSION: inside the locked per-repo
    # work (list_open_pull_requests is called from within backfill_open_prs, i.e. under the held lock) a fresh
    # connection tries pg_try_advisory_lock(hashtext(account), hashtext(repo)) on the SAME owner-id key; a FALSE =
    # the lock is held by the worker = proof it's taken. After boot returns, the same try must SUCCEED for every
    # repo = proof it was released. (A non-blocking try → the test can't deadlock against the worker's lock.)
    BR_DSN = f"postgresql://veripsa_app@localhost/{DB}"

    def _direct_install_activation(payload, gh_client, delivery_key, event_type="installation"):
        """Capture handle_event's return while preserving the live durable-proof + tenant-pin preconditions."""
        # Real installation payloads carry repository.id.  Most historical scenarios declare only full_name for
        # readability, so normalize that shorthand at the shared fixture boundary instead of allowing production
        # onboarding/graph enqueue to guess a stable identity.
        for repository_key in ("repositories", "repositories_added", "repositories_removed"):
            repositories = payload.get(repository_key)
            if not isinstance(repositories, list):
                continue
            for repository in repositories:
                if not isinstance(repository, dict) or repository.get("id") not in (None, ""):
                    continue
                full_name = repository.get("full_name")
                if isinstance(full_name, str) and full_name:
                    repository["id"] = _fixture_repo_id(full_name)
        installation = payload.setdefault("installation", {})
        installation.setdefault("id", 1)
        account = installation.get("account") if isinstance(installation.get("account"), dict) else {}
        account_id = account.get("id")
        if account_id in (None, ""):
            account_id = gh_client.installation_account_id()
        if account_id in (None, ""):
            raise AssertionError("activation fixture needs an authenticated owning-account id")
        account_login = account.get("login") or f"org{account_id}"
        account_type = account.get("type") or "Organization"
        installation["account"] = {
            "id": account_id, "login": account_login, "type": account_type,
        }
        seed_processing_delivery(BR_DSN, event_type, payload, delivery_key)
        payload[S._ACTIVATION_PROOF_MARKER] = S._activation_installation_proof(gh_client, payload)
        conn = psycopg2.connect(BR_DSN)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(account_id),))
                cur.execute(
                    "SELECT core.note_installation_account_metadata_with_authority(%s,%s,%s)",
                    (str(account_id), str(account_login), str(account_type)),
                )
            conn.autocommit = False
            result = S.handle_event(event_type, payload, S._scoped_db(conn), gh_client)
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
                            (delivery_key, 0))
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT core.release_webhook_delivery_with_authority(%s,%s,%s,%s)",
                        (delivery_key, "direct test processor failed", 3, 0),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _activate_empty_installation(owner_id, installation_id, delivery_key):
        """Provision a live route only through the same durable, App-proven activation contract as production."""
        gh_client = FakeGitHub({}, owner_id=owner_id)
        return _direct_install_activation(
            {"action": "created", "installation": {
                "id": installation_id,
                "account": {"id": owner_id, "login": f"org{owner_id}", "type": "Organization"},
            }, "repositories": []},
            gh_client, delivery_key,
        )

    def _gh_account_scalar(sql, args=()):
        conn = psycopg2.connect(BR_DSN)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.enter_installation_with_authority(%s)", ("4242",))
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    # The boot path keys the per-repo lock on (account, repo) — account = gh.installation_account_id() (the bare
    # owner id), the SAME two-arg scheme the live path uses (server._take_repo_lock). FakeGitHub's default owner_id
    # is 4242, so the boot reconcile holds the lock under ("4242", repo); the probe must key on the SAME pair.
    BOOT_LOCK_ACCOUNT = "4242"

    def _try_take_advisory(repo, account=BOOT_LOCK_ACCOUNT):
        """From a SEPARATE session: can we acquire (account, repo)'s advisory lock? True = it was free (we then
        release it so we don't pollute the next probe); False = some OTHER session holds it (the worker)."""
        probe = psycopg2.connect(BR_DSN)
        try:
            probe.autocommit = True
            with probe.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))", (account, repo))
                got = cur.fetchone()[0]
                if got:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))", (account, repo))
                return got
        finally:
            probe.close()

    class LockProbingGitHub(FakeGitHub):
        """Records, per repo, whether the per-repo advisory lock was HELD at the moment that repo's reconcile work
        ran. list_open_pull_requests is the first GitHub call inside backfill_open_prs, so it fires UNDER the lock."""
        def __init__(self, files_by_pr, open_prs):
            super().__init__(files_by_pr, open_prs)
            self.lock_held_during = {}    # repo -> True if the advisory lock was held (by the worker) during its work
        def list_open_pull_requests(self, repo, limit=None):
            # held-during-work == a separate session CANNOT take the lock right now (probed on the boot owner id key)
            self.lock_held_during[repo] = not _try_take_advisory(repo, str(self.installation_account_id()))
            return super().list_open_pull_requests(repo, limit)

    BR1, BR2 = "acme/boot-one", "acme/boot-two"
    br_gh = LockProbingGitHub(
        {11: ["backend/auth.py"], 22: ["backend/api.py"]},
        open_prs=[{"number": 11, "base": {"ref": "main"}, "head": {"sha": "br-11"}, "user": {"login": "alice"}},
                  {"number": 22, "base": {"ref": "main"}, "head": {"sha": "br-22"}, "user": {"login": "bob"}}],
    )
    # Restart/background work is deliberately non-provisioning. Seed the route through an authenticated,
    # durable installation.created delivery; boot_reconcile may only resolve that already-live installation.
    _activate_empty_installation(4242, 4242, "server-boot-route-4242")
    # pin the repo set so boot_reconcile doesn't need installation_repos; pass the REAL dsn so it locks for real.
    _saved_backfill_repos = os.environ.get("VERIPSA_BACKFILL_REPOS")
    os.environ["VERIPSA_BACKFILL_REPOS"] = f"{BR1},{BR2}"
    try:
        br_result = S.boot_reconcile(db, br_gh, cap=200, dsn=BR_DSN)
    finally:
        if _saved_backfill_repos is None:
            os.environ.pop("VERIPSA_BACKFILL_REPOS", None)
        else:
            os.environ["VERIPSA_BACKFILL_REPOS"] = _saved_backfill_repos

    # 1) the per-repo lock was HELD WHILE each repo's reconcile work ran (taken, and covering the write)
    checks.append(("boot_reconcile holds pg_advisory_lock(hashtext(account), hashtext(repo)) WHILE each repo's "
                   f"reconcile work runs (held-during: {br_gh.lock_held_during})",
                   br_gh.lock_held_during.get(BR1) is True and br_gh.lock_held_during.get(BR2) is True
                   and br_result.get("reconciled") == 2))
    # 2) every repo's lock is RELEASED after boot_reconcile returns (a fresh session can take each freely)
    released = {r: _try_take_advisory(r) for r in (BR1, BR2)}
    checks.append(("boot_reconcile RELEASES each repo's advisory lock before moving on / on return "
                   f"(post-run free: {released})",
                   released.get(BR1) is True and released.get(BR2) is True))
    # 3) it still actually reconciled (idempotent upsert path is intact under the lock): both PRs got a check
    checks.append(("boot_reconcile STILL reconciles under the lock (each repo's open PR got its check)",
                   {c["sha"] for c in br_gh.checks} >= {"br-11", "br-22"}))
    # 4) fall-back: with NO dsn (a unit context that supplies only a fake db) it still reconciles, unlocked
    fb_gh = FakeGitHub({33: ["backend/api.py"]},
                       open_prs=[{"number": 33, "base": {"ref": "main"}, "head": {"sha": "fb-33"},
                                  "user": {"login": "carol"}}])
    _saved2 = os.environ.get("VERIPSA_BACKFILL_REPOS")
    os.environ["VERIPSA_BACKFILL_REPOS"] = "acme/boot-fallback"
    try:
        fb_result = S.boot_reconcile(db, fb_gh, cap=200, dsn=None)
    finally:
        if _saved2 is None:
            os.environ.pop("VERIPSA_BACKFILL_REPOS", None)
        else:
            os.environ["VERIPSA_BACKFILL_REPOS"] = _saved2
    checks.append(("boot_reconcile with NO dsn falls back to the unlocked path and still reconciles "
                   "(degraded, never broken)",
                   fb_result.get("reconciled") == 1 and any(c["sha"] == "fb-33" for c in fb_gh.checks)))

    # BOOT-RECONCILE TENANT PIN (the restart self-heal's CORRECTNESS — the HIGH bug). boot_reconcile does gate
    # WRITES (act_for_claim) whose tenant comes ONLY from the session GUC core.installation_account, pinned ONLY by
    # enter_installation_with_authority. The LIVE path pins it per event (keyed by repository.owner.id →
    # 'ACCT-GH-'||<owner_id>); the boot path has no webhook payload, so it must resolve that SAME owner id
    # (gh.installation_account_id()) and pin the SAME route BEFORE the backfill. Two failures this guards:
    #   (a) UNPINNED in CLEAN PROD → veripsa_app has no credential → resolve_session_identity RAISES 42501 → every
    #       repo counted failed → the self-heal is a silent NO-OP exactly when needed. (Local tests MISS this
    #       because bootstrap_local provisions a veripsa_app→ACCT-DEMO credential, so an unpinned write silently
    #       lands in ACCT-DEMO instead.) (b) WRONG KEY → boot heals into a DIFFERENT tenant than live (split-tenant).
    # PROOF (stronger than ordering-only): drive boot for a repo owned by a DISTINCT owner id and assert the
    # backfill's claim landed in THAT owner's account ACCT-GH-<owner_id> — the IDENTICAL account the live path
    # routes the same owner id to (see the make_db_processor multi-tenant check below: owner 4242 → ACCT-GH-4242).
    # If the pin were absent, the write would land in ACCT-DEMO (the credential fallback) — not in ACCT-GH-7777;
    # if the pin came AFTER the write, the write would have no routed tenant at all. So an ACCT-GH-7777 claim, and
    # NO ACCT-DEMO claim for it, proves the pin was issued, keyed to the live owner id, BEFORE the backfill writes.
    BR_OWNER = 7777                                       # a tenant with NO prior rows → an ACCT-DEMO leak is visible
    BR_TENANT = "tenant/boot-pin"
    tp_gh = FakeGitHub({44: ["backend/auth.py"]},
                       open_prs=[{"number": 44, "base": {"ref": "main"}, "head": {"sha": "tp-44"},
                                  "user": {"login": "dana"}}],
                       owner_id=BR_OWNER)
    _activate_empty_installation(BR_OWNER, 9999, "server-boot-route-7777")
    _saved3 = os.environ.get("VERIPSA_BACKFILL_REPOS")
    os.environ["VERIPSA_BACKFILL_REPOS"] = BR_TENANT
    try:
        tp_result = S.boot_reconcile(db, tp_gh, cap=200, dsn=BR_DSN)
    finally:
        if _saved3 is None:
            os.environ.pop("VERIPSA_BACKFILL_REPOS", None)
        else:
            os.environ["VERIPSA_BACKFILL_REPOS"] = _saved3

    _mig = make_db("veripsa_migrator")                    # read claims past RLS (the App role writes via gates only)

    # BOOT-RECONCILE STABLE ID CONVERGENCE: the installation inventory now carries repository.id. A graph created
    # before that metadata was persisted has repo_id=NULL; boot must pass the strict tombstone/conflict gate and
    # stamp only this legacy identity, not leave the coordinate permanently ambiguous.
    BI_OWNER, BI_REPO, BI_REPO_ID = 7878, "tenant/boot-identity", "909090"
    _bi_conn = psycopg2.connect(BR_DSN)
    try:
        _bi_conn.autocommit = True
        with _bi_conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(BI_OWNER),))
            cur.execute("SELECT core.ingest_graph_with_authority(%s::jsonb,%s,%s,%s)",
                        (json.dumps(graph), BI_REPO, "main", "f" * 40))
    finally:
        _bi_conn.close()
    bi_before = _mig(
        "SELECT set_config('core.current_account',%s,true); "
        "SELECT repo_id FROM core.graph_version WHERE repo=%s AND branch='main'",
        (f"ACCT-GH-{BI_OWNER}", BI_REPO),
    )
    class BICurrentIdentityGitHub(FakeGitHub):
        def repo_current_identity(self, repo):
            return {"id": BI_REPO_ID, "owner_id": BI_OWNER, "full_name": BI_REPO}

    bi_result = ingest._reconcile_one_repo(
        db, BICurrentIdentityGitHub({}, open_prs=[], owner_id=BI_OWNER), BI_REPO, BR_DSN,
        repository_id=BI_REPO_ID,
    )
    bi_after = _mig(
        "SELECT set_config('core.current_account',%s,true); "
        "SELECT repo_id FROM core.graph_version WHERE repo=%s AND branch='main'",
        (f"ACCT-GH-{BI_OWNER}", BI_REPO),
    )
    bi_activation = _mig(
        "SELECT set_config('core.current_account',%s,true); "
        "SELECT count(*)::int FROM core.repository_lifecycle_activation "
        "WHERE account_id=%s AND repo=%s AND repository_id=%s",
        (f"ACCT-GH-{BI_OWNER}", f"ACCT-GH-{BI_OWNER}", BI_REPO, BI_REPO_ID),
    )
    checks.append((f"boot_reconcile converges a legacy repo_id=NULL graph to the current stable id only after the "
                   f"lifecycle gate (before={bi_before}, after={bi_after}, activation={bi_activation})",
                   bi_before is None and str(bi_after) == BI_REPO_ID and bi_activation == 1
                   and bi_result.get("repository_identity", {}).get("activation_recorded") is True))

    def _claims_in(account, repo):
        return _mig("""SELECT set_config('core.current_account',%s,true);
            SELECT count(*)::int FROM core.claim WHERE repo=%s AND claim_state IN ('active','waiting')""",
                    (account, repo))
    tp_in_owner = _claims_in(f"ACCT-GH-{BR_OWNER}", BR_TENANT)   # where the pin SHOULD route the write
    tp_in_demo = _claims_in("ACCT-DEMO", BR_TENANT)             # where an UNPINNED write would leak (credential fallback)
    checks.append((f"boot_reconcile PINS the tenant before backfill writes: the reconcile landed in the "
                   f"installation's OWN account ACCT-GH-{BR_OWNER} (claims={tp_in_owner}), NOT the credential-"
                   f"fallback ACCT-DEMO (claims={tp_in_demo}) — proves the pin (keyed by the live owner id) ran "
                   f"BEFORE the write; without it a clean-prod write would 42501 and a dogfood write would leak here",
                   tp_result.get("reconciled") == 1 and tp_in_owner is not None and tp_in_owner > 0
                   and tp_in_demo == 0))
    # KEY MATCH WITH THE LIVE PATH (no split tenant): run the SAME owner id through the REAL live processor and
    # assert its event lands in the SAME ACCT-GH-<owner_id> the boot path just wrote to. Live keys by
    # repository.owner.id; boot keys by installation_account_id() → both must resolve to ACCT-GH-7777.
    live_pin_gh = FakeGitHub({45: ["backend/api.py"]}, owner_id=BR_OWNER)
    S.make_db_processor(BR_DSN)("pull_request",
        {"action": "opened", "number": 45, "installation": {"id": 9999},
         "repository": {"full_name": BR_TENANT, "default_branch": "main",
                        "id": _fixture_repo_id(BR_TENANT),
                        "owner": {"id": BR_OWNER, "login": f"org{BR_OWNER}"}},
         "pull_request": {"base": {"ref": "main", "sha": SHA},
                          "head": {"sha": f"{45:040x}"},
                          "user": {"login": "erin"}, "merged": False}}, None, live_pin_gh)
    live_in_owner = _claims_in(f"ACCT-GH-{BR_OWNER}", BR_TENANT)   # now holds BOTH boot's PR-44 and live's PR-45
    checks.append((f"boot + live agree on the tenant key (no split tenant): the live processor for the SAME owner "
                   f"id {BR_OWNER} also landed in ACCT-GH-{BR_OWNER} (claims now {live_in_owner} ≥ boot's {tp_in_owner})",
                   live_in_owner is not None and live_in_owner > tp_in_owner))
    # FAIL-CLOSED when the owner id is UNRESOLVABLE: a gh that can't surface an owner (None) must NOT silently
    # write into an unrouted/wrong tenant — boot_reconcile counts the repo failed (caller's per-repo except) and
    # defers it to its own webhook. Proves the pin is REQUIRED, never skipped-and-write-anyway.
    noid_gh = FakeGitHub({46: ["backend/api.py"]},
                         open_prs=[{"number": 46, "base": {"ref": "main"}, "head": {"sha": "ni-46"},
                                    "user": {"login": "frank"}}],
                         owner_id=None)
    _saved4 = os.environ.get("VERIPSA_BACKFILL_REPOS")
    os.environ["VERIPSA_BACKFILL_REPOS"] = "tenant/no-owner-id"
    try:
        ni_result = S.boot_reconcile(db, noid_gh, cap=200, dsn=BR_DSN)
    finally:
        if _saved4 is None:
            os.environ.pop("VERIPSA_BACKFILL_REPOS", None)
        else:
            os.environ["VERIPSA_BACKFILL_REPOS"] = _saved4
    ni_leak = _claims_in("ACCT-DEMO", "tenant/no-owner-id")
    checks.append((f"boot_reconcile FAILS CLOSED on an unresolvable owner id (no unrouted-tenant write): repo "
                   f"counted failed (failed={ni_result.get('failed')}), nothing leaked into ACCT-DEMO (claims={ni_leak})",
                   ni_result.get("failed") == 1 and ni_result.get("reconciled") == 0 and ni_leak == 0
                   and not any(c["sha"] == "ni-46" for c in noid_gh.checks)))

    # BOOT-RECONCILE CLAIM SELF-HEAL (the dropped-'closed'/merge backstop, #3 — was DEAD CODE: the gate fn
    # reconcile_repo_claims_with_authority had ZERO runtime callers, so a dropped close stranded waiters until the
    # LEASE expired, not the documented PROMPT self-heal). boot_reconcile now wires it: per repo, under the per-
    # repo lock, after re-running the live open PRs it converges the claim set to the live open-PR truth — releases
    # PR-claims no longer open + promotes their waiters. Two invariants proven here against a REAL DB:
    #   (i)  a stranded PR-claim (its close was DROPPED → not in the live open set) is RELEASED, and the PR WAITING
    #        behind it on the same lane is PROMOTED — the prompt self-heal, not a lease-timeout wait.
    #   (ii) a pre-PR 'BR-<branch>' push reservation is NOT in the open-PR set but must NOT be reclaimed (it is
    #        governed by the push→PR-open lifecycle, not the backfill) — proves the fn's PR-only scope is wired.
    RC_OWNER = 8181
    RC_TENANT = f"ACCT-GH-{RC_OWNER}"
    RC_REPO = "tenant/reconcile-selfheal"
    RC_PATH = "backend/auth.py"           # exists in the fixture graph
    RC_BR_PATH = "backend/worker.py"      # a DIFFERENT path, reserved by a pre-PR branch push (must survive)
    rc_live = S.make_db_processor(BR_DSN)
    # seed the protected-branch graph for RC_REPO in its own tenant so PR analysis is real (route via the live pin)
    _seed_conn = psycopg2.connect(BR_DSN)
    try:
        _seed_conn.autocommit = True
        with _seed_conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(RC_OWNER),))
            cur.execute("SELECT core.ingest_graph_with_authority(%s::jsonb,%s,%s,%s)",
                        (json.dumps(graph), RC_REPO, "main", "d" * 40))
    finally:
        _seed_conn.close()
    # PR-101 opens → claims RC_PATH (active). PR-102 opens on the SAME path → WAITS behind 101.
    rc_gh_seed = FakeGitHub({101: [RC_PATH], 102: [RC_PATH]}, owner_id=RC_OWNER)
    for _n, _author in ((101, "alice"), (102, "bob")):
        rc_live("pull_request",
                {"action": "opened", "number": _n, "installation": {"id": 5151},
                 "repository": {"full_name": RC_REPO, "default_branch": "main",
                                "id": _fixture_repo_id(RC_REPO), "owner": {"id": RC_OWNER}},
                 "pull_request": {"base": {"ref": "main", "sha": SHA},
                                  "head": {"sha": f"{_n:040x}", "ref": f"feat-{_n}"},
                                  "user": {"login": _author}, "merged": False}}, None, rc_gh_seed)
    # a pre-PR branch push reserves a DIFFERENT path under 'BR-feat-x' (active) — it must SURVIVE the reconcile.
    rc_live("push",
            {"ref": "refs/heads/feat-x", "after": "e" * 40,
             "repository": {"full_name": RC_REPO, "default_branch": "main", "owner": {"id": RC_OWNER}},
             "pusher": {"name": "carol"}, "commits": [{"added": [], "modified": [RC_BR_PATH], "removed": []}]},
            None, rc_gh_seed)

    def _rc_states(change_id):
        return _mig("""SELECT set_config('core.current_account',%s,true);
            SELECT COALESCE(jsonb_agg(claim_state ORDER BY claim_state),'[]'::jsonb)::text
              FROM core.claim WHERE repo=%s AND change_id=%s AND branch='main'
                AND claim_state IN ('active','waiting')""", (RC_TENANT, RC_REPO, change_id))
    rc_101_before = json.loads(_rc_states("PR-101"))
    rc_102_before = json.loads(_rc_states("PR-102"))
    rc_br_before = json.loads(_rc_states("BR-feat-x"))
    checks.append((f"reconcile self-heal precondition: PR-101 active, PR-102 waiting behind it on the same lane, "
                   f"BR-feat-x reserved on a different path (101={rc_101_before}, 102={rc_102_before}, "
                   f"BR={rc_br_before})",
                   rc_101_before == ["active"] and rc_102_before == ["waiting"] and rc_br_before == ["active"]))

    # Now PR-101's 'closed' delivery was DROPPED: the live open set lists ONLY PR-102 (101 is gone). boot_reconcile
    # re-runs the open PRs THEN reconciles claims to that set → PR-101's stranded lane is released, PR-102 promoted.
    rc_gh = FakeGitHub({102: [RC_PATH]}, owner_id=RC_OWNER,
                       open_prs=[{"number": 102, "base": {"ref": "main"},
                                  "head": {"sha": f"{102:040x}", "ref": "feat-102"}, "user": {"login": "bob"}}])
    _saved_rc = os.environ.get("VERIPSA_BACKFILL_REPOS")
    os.environ["VERIPSA_BACKFILL_REPOS"] = RC_REPO
    try:
        rc_result = S.boot_reconcile(db, rc_gh, cap=200, dsn=BR_DSN)
    finally:
        if _saved_rc is None:
            os.environ.pop("VERIPSA_BACKFILL_REPOS", None)
        else:
            os.environ["VERIPSA_BACKFILL_REPOS"] = _saved_rc
    rc_101_after = json.loads(_rc_states("PR-101"))
    rc_102_after = json.loads(_rc_states("PR-102"))
    rc_br_after = json.loads(_rc_states("BR-feat-x"))
    checks.append((f"reconcile self-heal: a dropped-close PR-101's stranded lane is RELEASED + the waiter PR-102 is "
                   f"PROMOTED to active (prompt self-heal, not a lease-timeout wait) — 101 live={rc_101_after}, "
                   f"102 live={rc_102_after}",
                   rc_101_after == [] and rc_102_after == ["active"]))
    checks.append((f"reconcile self-heal: a pre-PR 'BR-<branch>' reservation is NOT reclaimed by the open-PR "
                   f"backfill (PR-only scope) — BR-feat-x still active ({rc_br_after})", rc_br_after == ["active"]))

    # INSTALL INGRESS IS QUEUE-ONLY.  An installation lifecycle delivery must persist routing plus one durable
    # graph/onboarding request per repository and return; GitHub PR reads, graph extraction, and customer-surface
    # mutations belong to the account-fair convergence worker.  Therefore the live event neither enters the old
    # per-repository remote-work lock convoy nor calls list_open_pull_requests.  The ordinary repo lock remains
    # free after the event for concurrent structural webhook traffic.
    IL_OWNER = 6262
    IL_REPO = "tenant/install-locked"
    il_lock_held = {}
    class InstallLockProbeGitHub(FakeGitHub):
        def list_open_pull_requests(self, repo, limit=None):
            il_lock_held[repo] = not _try_take_advisory(repo, str(IL_OWNER))
            return super().list_open_pull_requests(repo, limit)
    il_gh = InstallLockProbeGitHub({}, owner_id=IL_OWNER)
    il_payload = {"action": "created",
                  "installation": {"id": 6363, "account": {
                      "id": IL_OWNER, "login": "install-lock", "type": "Organization"}},
                  "repositories": [{"full_name": IL_REPO, "id": _fixture_repo_id(IL_REPO)}]}
    seed_processing_delivery(BR_DSN, "installation", il_payload, "server-install-lock-created")
    S.make_db_processor(BR_DSN)("installation", il_payload, None, il_gh)
    make_db("veripsa_app")(
        "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
        ("server-install-lock-created", 0),
    )
    checks.append((f"install ingress performs no inline GitHub PR read or per-repo remote-work lock "
                   f"(unexpected calls={il_lock_held})",
                   il_lock_held == {} and il_gh.checks == []))
    il_free_after = _try_take_advisory(IL_REPO, str(IL_OWNER))
    checks.append((f"install fan-out RELEASES the per-repo lock after the event returns (post-run free={il_free_after})",
                   il_free_after is True))

    # ONBOARDING: installation ingress must queue each repository's graph without cloning in the webhook
    # process. PR discovery and its first honest signal are part of that queued convergence turn; the webhook must
    # not publish a speculative Unknown before it has authoritative current-PR and graph evidence.
    fresh = "acme/onboard-fresh"
    onb_gh = FakeGitHub({7: ["backend/api.py"]},
                        open_prs=[{"number": 7, "base": {"ref": "main"}, "head": {"sha": "of7"}, "user": {"login": "alice"}}])
    onb = _direct_install_activation(
        {"action": "created", "installation": {"id": 4242},
         "repositories": [{"full_name": fresh}]},
        onb_gh, "server-direct-onboard-fresh",
    )
    onb_graph = (onb.get("onboarded") or [{}])[0].get("graph", {})
    checks.append(("onboarding: install event durably queues the default-branch graph (no inline files/edges)",
                   onb_graph.get("queued") is True and onb_graph.get("indexing") is True
                   and "files" not in onb_graph and "edges" not in onb_graph))
    checks.append(("onboarding: install ingress publishes no speculative PR surface before queued convergence",
                   onb_gh.checks == [] and onb_gh.comments == []))

    # ONBOARDING ISOLATION (first-impression critical): a multi-repo ORG install durably enqueues every repository
    # without making any inline per-repository PR call.  A future permission gap is isolated to that repository's
    # fair convergence turn; it cannot abort ingress or strand repositories listed after it.
    class OnePermGapGitHub(FakeGitHub):
        def __init__(self, files_by_pr, open_prs, denied_repo):
            super().__init__(files_by_pr, open_prs)
            self.denied_repo = denied_repo
            self.open_pr_calls = []
        def list_open_pull_requests(self, repo, limit=None):
            self.open_pr_calls.append(repo)
            if repo == self.denied_repo:
                from urllib.error import HTTPError
                raise HTTPError(repo, 403, "Resource not accessible by integration", {}, None)
            return super().list_open_pull_requests(repo, limit)
    gap_repos = [{"full_name": "acme/iso-A"}, {"full_name": "acme/iso-denied"}, {"full_name": "acme/iso-C"}]
    gap_gh = OnePermGapGitHub({}, open_prs=[], denied_repo="acme/iso-denied")
    iso = _direct_install_activation(
        {"action": "created", "installation": {"id": 4242}, "repositories": gap_repos},
        gap_gh, "server-direct-onboard-isolation",
    )
    iso_results = iso.get("onboarded") or []
    iso_by_repo = {r.get("backfilled"): r for r in iso_results}
    checks.append(("onboarding isolation: a one-repo permission gap does NOT abort the org install (all 3 repos onboarded)",
                   len(iso_results) == 3 and set(iso_by_repo) == {"acme/iso-A", "acme/iso-denied", "acme/iso-C"}))
    checks.append(("onboarding isolation: ingress performs no inline PR call and invents no premature prs_error",
                   gap_gh.open_pr_calls == []
                   and all("prs_error" not in result for result in iso_results)))
    checks.append(("onboarding isolation: every repository, including the one listed after a future gap, is queued",
                   all((iso_by_repo.get(repo) or {}).get("graph", {}).get("queued") is True
                       for repo in ("acme/iso-A", "acme/iso-denied", "acme/iso-C"))))

    # CROSS-REPO CLAIM IDENTITY (multi-repo org, first-impression critical): PR numbers AND paths are PER-REPO, so
    # two DIFFERENT repos in the SAME account routinely share a claim_id ('PR-9:backend/api.py' both have a PR #9
    # touching that file) — and a multi-repo org install puts every repo in ONE account. The claim_id (and the
    # change_id derived from it) is NOT repo-namespaced; before the PK was widened to (account_id, repo, claim_id)
    # the SECOND repo's claim INSERT hit a claim_pkey unique_violation, and _place_claim's handler re-INSERTed the
    # same PK uncaught → the event CRASHED (no check/comment on that PR). Assert: both repos' PR #9 analyze cleanly,
    # AND each repo's claim is a DISTINCT live row (in-repo lane semantics unaffected — they don't cross-collide).
    xr_gh = FakeGitHub({9: ["backend/api.py"]})
    XR_A, XR_B = "acme/xrepo-a", "acme/xrepo-b"
    xr_graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
    for r in (XR_A, XR_B):
        db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(xr_graph), r, "main", SHA))
    xr_a = S.handle_event("pull_request", {"action": "opened", "number": 9, "installation": {"id": 4242},
                          "repository": {"full_name": XR_A, "default_branch": "main",
                                         "id": _fixture_repo_id(XR_A)},
                          "pull_request": {"base": {"ref": "main", "sha": SHA},
                                           "head": {"sha": f"{9:040x}", "repo": {"id": 1}},
                                           "user": {"login": "alice"}, "merged": False}}, db, xr_gh)
    xr_b_crashed = None
    try:
        xr_b = S.handle_event("pull_request", {"action": "opened", "number": 9, "installation": {"id": 4242},
                              "repository": {"full_name": XR_B, "default_branch": "main",
                                             "id": _fixture_repo_id(XR_B)},
                              "pull_request": {"base": {"ref": "main", "sha": SHA},
                                               "head": {"sha": f"{9:040x}", "repo": {"id": 1}},
                                               "user": {"login": "bob"}, "merged": False}}, db, xr_gh)
    except Exception as e:
        xr_b_crashed = str(e)[:120]
    xr_admin = make_db("veripsa_migrator")   # read claims past RLS (App role writes via gates, can't raw-SELECT)
    def _xr_live(r):
        return xr_admin(
            "SELECT set_config('core.current_account','ACCT-DEMO',true);"
            "SELECT count(*)::int FROM core.claim WHERE repo=%s AND claim_id=%s AND claim_state IN ('active','waiting')",
            (r, "PR-9:backend/api.py"))
    xr_a_live = _xr_live(XR_A)
    xr_b_live = _xr_live(XR_B)
    checks.append((f"cross-repo claim identity: two repos with the SAME PR-9:path claim_id both analyze (no claim_pkey crash) — crash={xr_b_crashed}",
                   xr_b_crashed is None and "check" in xr_a and "check" in xr_b))
    checks.append((f"cross-repo claim identity: each repo holds its OWN distinct claim row (a_live={xr_a_live}, b_live={xr_b_live})",
                   str(xr_a_live) == "1" and str(xr_b_live) == "1"))

    # GitHub-authoritative facts: direct writers/readers cannot forge push/landed facts; the App can.
    writer = make_db("veripsa_demo_agent")
    admin = make_db("veripsa_migrator")
    writer_push_error = writer_land_error = None
    try:
        writer("SELECT core.record_push_with_authority(%s,%s,%s,%s)", (REPO, "main", "c" * 40, None))
    except psycopg2.Error as e:
        writer_push_error = e.pgcode
    try:
        writer("SELECT core.land_change_on_main_with_authority(%s,%s,%s,%s,%s)",
               ("PR-1", REPO, "e" * 40, None, "main"))
    except psycopg2.Error as e:
        writer_land_error = e.pgcode
    reader_has_push = admin(
        "SELECT has_function_privilege('veripsa_reader','core.record_push_with_authority(text,text,text,text)','EXECUTE')"
    )
    writer_has_land = admin(
        "SELECT has_function_privilege('veripsa_demo_agent','core.land_change_on_main_with_authority(text,text,text,text,text)','EXECUTE')"
    )
    checks.append(("only the GitHub App role can record push/landing facts",
                   writer_push_error == "42501" and writer_land_error == "42501"
                   and reader_has_push is False and writer_has_land is False))

    merge_result = S.handle_event("pull_request", pr_payload("closed", 1, "alice", "f" * 40, merged=True), db, gh)
    released_claims = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int
          FROM core.claim
         WHERE repo=%s AND branch='main' AND change_id='PR-1' AND claim_state IN ('active','waiting')
        """,
        (REPO,),
    )
    pr3_open = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int
          FROM core.claim
         WHERE repo=%s AND branch='main' AND change_id='PR-3' AND claim_state IN ('active','waiting')
        """,
        (REPO,),
    )
    claim_snapshot = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT COALESCE(json_agg(json_build_array(change_id, agent_id, target_path, claim_state)
                                ORDER BY change_id, target_path, claim_state)::text, '[]')
          FROM core.claim
         WHERE repo=%s AND branch='main'
        """,
        (REPO,),
    )
    checks.append((f"hosted merge releases GH-author claims for only that PR change_id "
                   f"(landed={merge_result.get('landed')}, pr1_open={released_claims}, pr3_open={pr3_open}, claims={claim_snapshot})",
                   merge_result.get("landed", {}).get("released")
                   and released_claims == 0 and pr3_open > 0))

    # WITHDRAW lifecycle (closed WITHOUT merge): PR-2 (bob, api.py) is the active holder with PR-3 (carol)
    # waiting behind it on the same lane. Closing PR-2 unmerged must RELEASE PR-2's lane and PROMOTE PR-3 to
    # active IMMEDIATELY — not strand it until the lease expires. (Records NO landing — nothing reached main.)
    withdraw_result = S.handle_event("pull_request", pr_payload("closed", 2, "bob", merged=False), db, gh)
    pr2_open = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int
          FROM core.claim
         WHERE repo=%s AND branch='main' AND change_id='PR-2' AND claim_state IN ('active','waiting')
        """,
        (REPO,),
    )
    pr3_active = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int
          FROM core.claim
         WHERE repo=%s AND branch='main' AND change_id='PR-3' AND claim_state='active'
        """,
        (REPO,),
    )
    checks.append((f"hosted withdraw (closed, NOT merged) releases the PR's lanes + promotes the waiter "
                   f"(action={withdraw_result.get('action')}, pr2_open={pr2_open}, pr3_active={pr3_active})",
                   withdraw_result.get("action") == "withdrawn"
                   and (withdraw_result.get("released") or {}).get("released")
                   and pr2_open == 0 and pr3_active > 0))
    # A structural PR event now durably requests graph convergence. Until that worker turn commits, a promotion
    # must never greenwash the prior conservative verdict: the lane is active, but structural proof is pending.
    # The convergence worker's replay performs the eventual clear reset once the exact graph coordinate lands.
    c3_now = next((c for c in gh.comments if c["number"] == 3), None)
    pr3_head = gh.pull_request_head(REPO, 3)
    pr3_check = next((c for c in reversed(gh.checks) if c.get("sha") == pr3_head), None)
    checks.append(("graph-pending promotion: PR-3 is active but is not reset to a false green before convergence",
                   pr3_active > 0 and bool(c3_now) and bool(pr3_check)
                   and pr3_check.get("conclusion") != "success"))

    # A live main push records facts + durably queues the exact graph coordinate.  The webhook process must not
    # download a tarball or run extraction; the isolated convergence worker performs that turn later.
    pushed = S.handle_event("push", {"ref": "refs/heads/main", "after": "b" * 40,
                                     "repository": {"full_name": REPO, "default_branch": "main",
                                                    "id": REPO_ID}}, db, gh)
    checks.append(("push to main records facts + durably queues graph convergence without inline extraction",
                   pushed.get("mode") == "queued"
                   and pushed.get("deferred") == "graph_refresh_queued"
                   and pushed.get("graph_refresh", {}).get("queued") is True
                   and "files" not in pushed and "edges" not in pushed))
    landings = db("SELECT (core.effect_surface()->>'landings')")
    checks.append(("the landing is recorded on the effect ledger", str(landings) >= "1"))

    # UNCERTAINTY SAFETY: persist one explicit, closed-contract uncertainty
    # marker at the exact base SHA. The sample fixture itself is deliberately
    # clean, so this scenario does not depend on optional parser availability
    # or mistake an unresolved-reference counter for persisted uncertainty.
    # Adding a node status leaves every count metric unchanged; only the
    # extraction graph hash changes and must be recomputed honestly.
    uncertain_graph = json.loads(json.dumps(graph))
    uncertain_node = next(
        node for node in uncertain_graph["nodes"]
        if node.get("kind") == "file" and node.get("path") == "backend/api.py"
    )
    uncertain_node["analysis_status"] = "incomplete"
    uncertain_graph["metrics"]["extraction_graph_hash"] = (
        C.canonical_graph_hash(uncertain_graph)
    )
    C.assert_valid_graph(
        uncertain_graph, persisted=True, require_resource_metadata=True
    )
    uncertain_write = _json_scalar(db(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(uncertain_graph), REPO, "main", "b" * 40),
    ))
    uncertain_baseline = _json_scalar(
        db("SELECT core.coordinate_graph_sha(%s,%s)", (REPO, "main"))
    )
    checks.append((
        "uncertainty fixture is explicit, governed, and read back at the exact base SHA",
        uncertain_write.get("ok") is True
        and uncertain_baseline.get("commit_sha") == "b" * 40
        and uncertain_baseline.get("has_graph_uncertainty") is True
        and uncertain_baseline.get("extractor_version") == C.EXTRACTOR_VERSION
        and uncertain_baseline.get("graph_hash") == uncertain_write.get("graph_hash"),
    ))
    # A changed-file list is not enough proof that a path-local patch over
    # this uncertain baseline would stay equivalent to a full rebuild. The
    # server must choose correctness and re-ingest fully. Clean-baseline
    # incremental bandwidth is covered independently by test_incremental.py
    # and test_graph_full_incremental_equivalence.py.
    incr = S.handle_event("push", {
        "ref": "refs/heads/main", "before": "b" * 40, "after": "1a" * 20,
        "repository": {"full_name": REPO, "default_branch": "main", "id": REPO_ID},
        "pusher": {"name": "alice"},
        "commits": [{"added": [], "modified": ["backend/api.py"], "removed": []}],
    }, db, gh)
    checks.append((f"push with an uncertain baseline defers the correctness-preserving rebuild to the graph worker "
                   f"(mode={incr.get('mode')}, deferred={incr.get('deferred')})",
                   incr.get("mode") == "queued"
                   and incr.get("graph_refresh", {}).get("queued") is True
                   and "files" not in incr and "fallback_reason_codes" not in incr))

    # FORCE-PUSH safety: a forced (history-rewrite) push's commits[] does NOT faithfully describe the diff vs
    # our stored graph, so an incremental patch would drift. forced=true must fall back to a FULL re-ingest
    # (clone @sha, rebuild the coordinate = always correct), even on a repo WITH a baseline.
    forced = S.handle_event("push", {
        "ref": "refs/heads/main", "after": "2b" * 20, "forced": True,
        "repository": {"full_name": REPO, "default_branch": "main", "id": REPO_ID},
        "pusher": {"name": "alice"},
        "commits": [{"added": [], "modified": ["backend/api.py"], "removed": []}],
    }, db, gh)
    checks.append((f"force-push queues an isolated full-coordinate convergence turn instead of extracting inline "
                   f"(mode={forced.get('mode')}, deferred={forced.get('deferred')})",
                   forced.get("mode") == "queued"
                   and forced.get("graph_refresh", {}).get("queued") is True
                   and "files" not in forced))

    # ── UNIFIED LANDING MODEL (root fix): two DIRECT pushes by DIFFERENT pushers on the SAME file
    #    → collisions_occurred ≥ 1 and collisions_on_main names that path.
    #    A same-author push or a distinct-file push does NOT count as a collision.
    COLL_REPO = "acme/collision-test"
    SHARED_PATH = "src/shared.py"
    SHA_ALICE = "aa" * 20   # 40 hex chars, pusher=alice
    SHA_BOB   = "bb" * 20   # pusher=bob   (DIFFERENT author, SAME file → real collision on main)
    SHA_CAROL = "cc" * 20   # same alice again on shared.py (same author → NOT a new collision pair here)
    SHA_BOB2  = "dd" * 20   # bob on a DIFFERENT file → NOT a collision with alice on shared.py

    def push_payload(sha, pusher, paths, repo=COLL_REPO, branch="main"):
        commits = [{"added": [], "modified": paths, "removed": []}]
        return {"ref": f"refs/heads/{branch}", "after": sha,
                "repository": {"full_name": repo, "default_branch": "main",
                               "id": _fixture_repo_id(repo)},
                "pusher": {"name": pusher}, "commits": commits}

    push_gh = FakeGitHub({})
    # Push 1: alice changes src/shared.py
    S.handle_event("push", push_payload(SHA_ALICE, "alice", [SHARED_PATH]), db, push_gh)
    # Push 2: bob also changes src/shared.py → COLLISION (different author, same file on main)
    S.handle_event("push", push_payload(SHA_BOB, "bob", [SHARED_PATH]), db, push_gh)
    # Push 3: alice again on src/shared.py → extends alice's coverage but no NEW inter-author pair beyond above
    S.handle_event("push", push_payload(SHA_CAROL, "alice", [SHARED_PATH]), db, push_gh)
    # Push 4: bob on a DIFFERENT file → should NOT add to collisions for shared.py
    S.handle_event("push", push_payload(SHA_BOB2, "bob", ["src/other.py"]), db, push_gh)

    collisions_raw = db("SELECT core.collisions_on_main(%s,%s,%s)", (COLL_REPO, "main", "14 days"))
    import json as _json
    collisions = _json.loads(collisions_raw) if isinstance(collisions_raw, str) else (collisions_raw or {})
    effect_raw = db("SELECT core.effect_surface()")
    effect = _json.loads(effect_raw) if isinstance(effect_raw, str) else (effect_raw or {})

    collisions_count = collisions.get("collisions_count", 0)
    recent = collisions.get("recent") or []
    collision_paths = [r.get("path") for r in recent]
    collisions_occurred = effect.get("collisions_occurred", 0)

    checks.append((
        f"unified landing: two direct pushes (alice+bob) on same file → collisions_on_main.collisions_count ≥ 1 "
        f"(got {collisions_count})",
        collisions_count >= 1))
    checks.append((
        f"unified landing: collisions_on_main names the shared path {SHARED_PATH!r} "
        f"(got {collision_paths})",
        SHARED_PATH in collision_paths))
    checks.append((
        f"unified landing: effect_surface.collisions_occurred ≥ 1 (got {collisions_occurred})",
        collisions_occurred >= 1))

    # Control: distinct-file pushes (alice on shared.py, bob on other.py) are NOT a collision on shared.py
    # — there must be zero 'other.py'-named collisions where the ONLY other-file change is bob's.
    # Verify: a fresh repo with one pusher only → no collision.
    SOLO_REPO = "acme/solo-test"
    SHA_SOLO = "ee" * 20
    S.handle_event("push", push_payload(SHA_SOLO, "alice", ["solo/file.py"], repo=SOLO_REPO), db, push_gh)
    solo_raw = db("SELECT core.collisions_on_main(%s,%s,%s)", (SOLO_REPO, "main", "14 days"))
    solo = _json.loads(solo_raw) if isinstance(solo_raw, str) else (solo_raw or {})
    checks.append((
        f"unified landing: single-author push produces no collision (got {solo.get('collisions_count',0)})",
        solo.get("collisions_count", 0) == 0))

    # ── COLLISION DETECTION AT PUSH TIME (the pre-merge window opens at PUSH, not only at PR). A push to a
    #    NON-main feature branch reserves lanes NOW (change_id 'BR-<branch>'), so two feature branches that
    #    touch the SAME path contend BEFORE either opens a PR — and the later branch WAITS IN LINE.
    BR_REPO = "acme/branch-collision-test"
    BR_PATH = "src/widget.py"

    def branch_push(branch, pusher, paths, repo=BR_REPO, sha=None):
        # a non-main branch push (ref = refs/heads/<branch>); default_branch stays 'main' so the handler
        # treats it as a feature branch and reserves lanes on main's namespace (never ingests its graph).
        sha = sha or (("%040x" % (abs(hash((branch, pusher))) % (16 ** 40))))
        return {"ref": f"refs/heads/{branch}", "after": sha,
                "repository": {"full_name": repo, "default_branch": "main",
                               "id": _fixture_repo_id(repo)},
                "pusher": {"name": pusher}, "commits": [{"added": [], "modified": paths, "removed": []}]}

    def branch_delete(branch, pusher, repo=BR_REPO):
        # GitHub branch-delete push shape: ref names the deleted branch and after is all zeroes. The handler
        # must release push-time BR lanes, not treat it as a harmless no-op.
        return {"ref": f"refs/heads/{branch}", "after": "0" * 40, "deleted": True,
                "repository": {"full_name": repo, "default_branch": "main",
                               "id": _fixture_repo_id(repo)},
                "pusher": {"name": pusher}, "commits": []}

    def br_claim_states(repo, change_id):
        # only LIVE lane states (active/waiting) — a released/expired claim no longer holds the lane.
        return admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
            SELECT COALESCE(jsonb_agg(claim_state ORDER BY claim_state),'[]'::jsonb)::text
              FROM core.claim WHERE repo=%s AND change_id=%s AND branch='main'
                AND claim_state IN ('active','waiting')""", (repo, change_id))

    # Branch 1 (alice) pushes feature/a touching widget.py → free lane → GRANTED (active).
    r_b1 = S.handle_event("push", branch_push("feature/a", "alice", [BR_PATH]), db, push_gh)
    # Branch 2 (bob) pushes feature/b touching THE SAME widget.py → lane held by another work-unit → WAITING.
    r_b2 = S.handle_event("push", branch_push("feature/b", "bob", [BR_PATH]), db, push_gh)
    states_b1 = _json.loads(br_claim_states(BR_REPO, "BR-feature/a"))
    states_b2 = _json.loads(br_claim_states(BR_REPO, "BR-feature/b"))
    checks.append((
        f"push-time collision: a feature-branch push reserves lanes (BR-feature/a active before any PR) "
        f"(states={states_b1})", states_b1 == ["active"]))
    checks.append((
        f"push-time collision: a SECOND branch on the SAME path WAITS IN LINE (no PR involved) — "
        f"the pre-merge window opened at PUSH (BR-feature/b states={states_b2})", states_b2 == ["waiting"]))
    checks.append((
        f"push-time collision: the handler reports the reservation (reserved={r_b1.get('reserved')}, "
        f"change_id={r_b1.get('change_id')})",
        r_b1.get("reserved") == 1 and r_b1.get("change_id") == "BR-feature/a"
        and r_b2.get("reserved") == 1))
    # the held collision is RECORDED to the append-only ledger (not just the waiting state) — naming the path,
    # BEFORE any PR exists. This is the product signal "two works contend here" firing at PUSH time.
    br_collisions = admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int FROM core.event WHERE kind='collision_held' AND repo=%s AND path=%s""",
                          (BR_REPO, BR_PATH))
    checks.append((
        f"push-time collision: a held-collision event is RECORDED for the shared path before any PR exists "
        f"(collision_held rows={br_collisions})", br_collisions is not None and br_collisions >= 1))

    # Control: a branch push touching a DIFFERENT path does NOT collide (own free lane → active).
    S.handle_event("push", branch_push("feature/c", "carol", ["src/unrelated.py"]), db, push_gh)
    states_b3 = _json.loads(br_claim_states(BR_REPO, "BR-feature/c"))
    checks.append((
        f"push-time collision: a branch on a DIFFERENT path gets its own free lane (active, no false collision) "
        f"(states={states_b3})", states_b3 == ["active"]))

    # Branch delete: a pre-PR feature branch can disappear without a PR close/open event. The delete push must
    # release its BR-* lanes immediately and promote the next waiter, otherwise admin/PR views show stale
    # blockers for deleted branches until lease expiry.
    DEL_REPO = "acme/branch-delete-release-test"
    DEL_PATH = "src/delete-release.py"
    S.handle_event("push", branch_push("feature/delete-me", "dina", [DEL_PATH], repo=DEL_REPO), db, push_gh)
    S.handle_event("push", branch_push("feature/delete-waiter", "erin", [DEL_PATH], repo=DEL_REPO), db, push_gh)
    del_before = _json.loads(br_claim_states(DEL_REPO, "BR-feature/delete-me"))
    del_waiter_before = _json.loads(br_claim_states(DEL_REPO, "BR-feature/delete-waiter"))
    del_release = S.handle_event("push", branch_delete("feature/delete-me", "dina", repo=DEL_REPO), db, push_gh)
    del_after = _json.loads(br_claim_states(DEL_REPO, "BR-feature/delete-me"))
    del_waiter_after = _json.loads(br_claim_states(DEL_REPO, "BR-feature/delete-waiter"))
    checks.append((
        f"push-time collision: deleting a feature branch releases its BR lane immediately "
        f"(before={del_before}, after={del_after}, result={del_release.get('change_id')})",
        del_before == ["active"] and del_after == []
        and del_release.get("deleted") is True and del_release.get("change_id") == "BR-feature/delete-me"))
    checks.append((
        f"push-time collision: branch-delete release promotes the next waiter "
        f"(before={del_waiter_before}, after={del_waiter_after})",
        del_waiter_before == ["waiting"] and del_waiter_after == ["active"]))

    # ── PUSH↔PR RECONCILIATION (no SELF-collision): a branch push followed by ITS OWN PR must NOT collide with
    #    itself. The PR (change_id 'PR-<n>') re-claims the SAME path the branch already held (change_id
    #    'BR-<branch>'). The server RELEASES the branch's claims when the PR opens, so the PR re-acquires the
    #    lane GRANTED (active), never queued behind itself. (Both run through plain handle_event with no
    #    installation → both land in the role's own account ACCT-DEMO, the same tenant — exactly the shared
    #    lane namespace the self-collision would occur in.)
    REC_REPO = "acme/reconcile-test"
    REC_PATH = "backend/auth.py"      # exists in the fixture graph so the PR analysis is real
    REC_BRANCH = "feature/login"
    push_gh.files_by_pr[50] = [REC_PATH]
    # seed the protected-branch graph so the PR path has a real baseline (else everything reads 'unknown')
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REC_REPO, "main", SHA))
    # Step 1: push the feature branch → reserves BR-feature/login on backend/auth.py (active).
    S.handle_event("push", branch_push(REC_BRANCH, "dave", [REC_PATH], repo=REC_REPO), db, push_gh)
    rec_branch_before = _json.loads(br_claim_states(REC_REPO, "BR-feature/login"))
    # Step 2: the PR for that head branch OPENS. Reconciliation releases BR-feature/login, then the PR claims
    # PR-50 on the same path → it must be GRANTED (active), proving no self-collision.
    rec_pr_payload = {
        "action": "opened", "number": 50,
        "repository": {"full_name": REC_REPO, "default_branch": "main",
                       "id": _fixture_repo_id(REC_REPO)},
        "pull_request": {"base": {"ref": "main", "sha": SHA},
                         "head": {"sha": f"{50:040x}", "ref": REC_BRANCH},
                         "user": {"login": "dave"}, "merged": False}}
    S.handle_event("pull_request", rec_pr_payload, db, push_gh)  # PR opens → reconcile → claim PR-50

    rec_br_after = _json.loads(br_claim_states(REC_REPO, "BR-feature/login"))   # only live (active/waiting) states
    rec_pr_after = _json.loads(br_claim_states(REC_REPO, "PR-50"))
    checks.append((
        f"reconcile precondition: the branch push reserved its lane before the PR (BR active={rec_branch_before})",
        rec_branch_before == ["active"]))
    checks.append((
        f"reconcile: the PR claims the SAME lane GRANTED (active), NO self-collision (PR-50 live states={rec_pr_after})",
        rec_pr_after == ["active"]))
    checks.append((
        f"reconcile: the branch's lane is no longer live once the PR took it over (BR-feature/login live "
        f"states={rec_br_after}) — the lane is continuous push→PR", rec_br_after == []))

    # ── BRANCH PUSH → OPEN PR REPLAY: the push event itself must keep the PR surface current. GitHub normally
    #    sends a pull_request.synchronize after a same-repo branch push, but the durable queue/dogfood found a
    #    real gap: if the only delivered signal is the branch push (especially docs-only / empty-diff), Veripsa
    #    reserved lanes or skipped as "no code paths changed" and left the PR with NO current check. A branch push
    #    whose head matches an open same-repo PR is therefore replayed through the SAME pull_request synchronize
    #    path. This posts the check, reconciles BR-* → PR-*, and releases stale PR lanes when a PR becomes
    #    docs-only — without adding a second analyzer.
    BPR_REPO = "acme/branch-push-replay-test"
    BPR_CODE_BRANCH, BPR_DOC_BRANCH, BPR_EMPTY_BRANCH, BPR_STALE_BRANCH, BPR_TOMBSTONE_BRANCH = (
        "feature/replay-code", "feature/replay-docs", "feature/replay-empty", "feature/replay-stale",
        "feature/replay-tombstoned")
    BPR_STALE_PATH = "backend/worker.py"
    BPR_CODE_SHA, BPR_DOC_SHA, BPR_EMPTY_SHA, BPR_STALE_SHA, BPR_TOMBSTONE_SHA = (
        "a1" * 20, "b2" * 20, "e0" * 20, "c3" * 20, "f4" * 20)

    def bpr_obj(number, branch, author, sha, changed_files=1, base_repo=BPR_REPO):
        return {"number": number, "base": {"ref": "main", "sha": SHA, "repo": {"id": 1, "full_name": base_repo}},
                "head": {"sha": sha, "ref": branch, "repo": {"id": 1, "full_name": base_repo}},
                "user": {"login": author}, "draft": False, "merged": False, "changed_files": changed_files}

    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), BPR_REPO, "main", SHA))
    bpr_gh = FakeGitHub(
        {90: [REC_PATH], 91: ["README.md"], 92: [BPR_STALE_PATH]},
        open_prs=[
            bpr_obj(90, BPR_CODE_BRANCH, "zoe", BPR_CODE_SHA),
            bpr_obj(91, BPR_DOC_BRANCH, "yuki", BPR_DOC_SHA),
            bpr_obj(92, BPR_STALE_BRANCH, "max", BPR_STALE_SHA),
            bpr_obj(93, BPR_EMPTY_BRANCH, "ivy", BPR_EMPTY_SHA, changed_files=0),
            bpr_obj(94, BPR_TOMBSTONE_BRANCH, "ted", BPR_TOMBSTONE_SHA),
        ],
    )
    bpr_gh.pr_objects.update({90: bpr_gh.open_prs[0], 91: bpr_gh.open_prs[1],
                              92: bpr_gh.open_prs[2], 93: bpr_gh.open_prs[3],
                              94: bpr_gh.open_prs[4]})
    bpr_gh.files_by_pr[94] = [REC_PATH]

    # Code push while PR #90 is already open: push alone reserves the branch lane, then replays PR #90 and posts
    # a check on the PR head. The BR claim is gone because the PR synchronize path owns reconciliation.
    bpr_code = S.handle_event("push", branch_push(BPR_CODE_BRANCH, "zoe", [REC_PATH], repo=BPR_REPO, sha=BPR_CODE_SHA), db, bpr_gh)
    bpr90_br_live = _json.loads(br_claim_states(BPR_REPO, f"BR-{BPR_CODE_BRANCH}"))
    bpr90_pr_live = _json.loads(br_claim_states(BPR_REPO, "PR-90"))
    checks.append(("branch-push replay: an open same-repo PR gets an honest graph-pending check from the push event",
                   bpr_code.get("replayed_prs") == ["PR-90"]
                   and any(c["sha"] == BPR_CODE_SHA and c["conclusion"] == "neutral"
                           and "code graph not confirmed current" in (c.get("title") or "")
                           for c in bpr_gh.checks)))
    checks.append(("branch-push replay: PR synchronize reconciliation releases the pre-PR BR lane",
                   bpr90_br_live == [] and bpr90_pr_live == ["active"]))

    # Docs-only push while PR #91 is open: lane reservation says "no code paths changed", but the open PR still
    # needs a green/current check in GitHub. It should not comment (no coordination signal).
    bpr_docs = S.handle_event("push", branch_push(BPR_DOC_BRANCH, "yuki", ["README.md"], repo=BPR_REPO, sha=BPR_DOC_SHA), db, bpr_gh)
    checks.append(("branch-push replay: docs-only open PR still gets a success check, with no PR comment spam",
                   bpr_docs.get("reserved") == 0 and bpr_docs.get("replayed_prs") == ["PR-91"]
                   and any(c["sha"] == BPR_DOC_SHA and c["conclusion"] == "success" for c in bpr_gh.checks)
                   and not any(c["number"] == 91 for c in bpr_gh.comments)))

    # Empty-diff push while PR #93 is open: this is the shape of an empty commit / metadata-only update. The
    # feature push reserves no lanes, but the open PR still needs a current check so the merge box does not look
    # like Veripsa missed it.
    bpr_empty = S.handle_event("push", branch_push(BPR_EMPTY_BRANCH, "ivy", [], repo=BPR_REPO, sha=BPR_EMPTY_SHA), db, bpr_gh)
    checks.append(("branch-push replay: empty-diff open PR still gets a success check, with no PR comment spam",
                   bpr_empty.get("reserved") == 0 and bpr_empty.get("replayed_prs") == ["PR-93"]
                   and any(c["sha"] == BPR_EMPTY_SHA and c["conclusion"] == "success" for c in bpr_gh.checks)
                   and not any(c["number"] == 93 for c in bpr_gh.comments)))

    # Stale-lane cleanup: PR #92 first opens as a code PR, then the next branch push narrows it to README-only.
    # The push replay must run PR reconcile([]), releasing the old PR lane immediately instead of leaving a
    # phantom blocker until a separate pull_request.synchronize happens to arrive.
    S.handle_event("pull_request", {"action": "opened", "number": 92,
                                    "repository": {"full_name": BPR_REPO, "default_branch": "main",
                                                   "id": _fixture_repo_id(BPR_REPO)},
                                    "pull_request": {"base": bpr_gh.pr_objects[92]["base"],
                                                     "head": bpr_gh.pr_objects[92]["head"],
                                                     "user": bpr_gh.pr_objects[92]["user"],
                                                     "merged": False, "changed_files": 1}}, db, bpr_gh)
    bpr92_before = _json.loads(br_claim_states(BPR_REPO, "PR-92"))
    bpr_gh.files_by_pr[92] = ["README.md"]
    bpr_gh.pr_objects[92] = bpr_obj(92, BPR_STALE_BRANCH, "max", BPR_STALE_SHA, changed_files=1)
    bpr_gh.open_prs[2] = bpr_gh.pr_objects[92]
    bpr_stale = S.handle_event("push", branch_push(BPR_STALE_BRANCH, "max", ["README.md"], repo=BPR_REPO, sha=BPR_STALE_SHA), db, bpr_gh)
    bpr92_after = _json.loads(br_claim_states(BPR_REPO, "PR-92"))
    checks.append(("branch-push replay: a docs-only force-push releases stale PR lanes immediately",
                   bpr92_before == ["active"] and bpr_stale.get("replayed_prs") == ["PR-92"] and bpr92_after == []))

    bpr_no_pr_gh = FakeGitHub({}, open_prs=[])
    bpr_no_pr = S.handle_event("push", branch_push("feature/no-open-pr", "nina", ["README.md"],
                                                   repo=BPR_REPO, sha="d4" * 20), db, bpr_no_pr_gh)
    checks.append(("branch-push replay: a no-code branch push with no matching open PR stays a quiet no-op",
                   bpr_no_pr.get("replayed_prs") == [] and not bpr_no_pr_gh.checks and not bpr_no_pr_gh.comments))

    # A stale conclusion tombstone can be left by an out-of-order close/withdraw edge. If the PR is currently
    # open on GitHub, the branch-push/check-suite backstop fetches that AUTHORITATIVE current PR and must
    # re-activate it instead of letting change_concluded suppress every new head forever. This is the dogfood
    # shape observed on PR #724: push and pull_request.synchronize were processed, but the new head had no
    # Veripsa check because the replay was treated as stale.
    db("SELECT core.release_change_on_main_with_authority(%s,%s,%s)", ("PR-94", BPR_REPO, "main"))
    tombstone_before = db("SELECT core.change_concluded(%s,%s)", (BPR_REPO, "PR-94"))
    bpr_tomb = S.handle_event("push", branch_push(BPR_TOMBSTONE_BRANCH, "ted", [REC_PATH],
                                                  repo=BPR_REPO, sha=BPR_TOMBSTONE_SHA), db, bpr_gh)
    bpr94_live = _json.loads(br_claim_states(BPR_REPO, "PR-94"))
    tombstone_after = db("SELECT core.change_concluded(%s,%s)", (BPR_REPO, "PR-94"))
    checks.append((f"branch-push replay: an authoritative current-open PR bypasses a stale conclusion tombstone, "
                   f"re-activates the PR, and posts a current check (before={tombstone_before}, "
                   f"after={tombstone_after}, "
                   f"replayed={bpr_tomb.get('replayed_prs')}, live={bpr94_live}, "
                   f"checks={[c for c in bpr_gh.checks if c['sha'] == BPR_TOMBSTONE_SHA]})",
                   tombstone_before is True
                   and bpr_tomb.get("replayed_prs") == ["PR-94"]
                   and any(c["sha"] == BPR_TOMBSTONE_SHA for c in bpr_gh.checks)
                   and bpr94_live in (["active"], ["waiting"])
                   and tombstone_after is False))

    # ── SYNCHRONIZE DROPS A FILE → its lane is RELEASED (no phantom block / cry-wolf). A PR's file set SHRINKS
    #    between pushes (force-push, revert a file, narrow scope — common). The per-path declare loop only ADDS
    #    claims for the CURRENT files; left alone it would NEVER release a claim for a file the PR no longer
    #    touches, so that orphaned 'active' claim keeps blocking other PRs forever. handle_pull_request now
    #    reconciles the PR's lanes to its live file set on every synchronize, so a dropped file is freed AND
    #    whoever waited behind it is promoted. (Plain handle_event with the App's shared db runner → the App's
    #    own account ACCT-DEMO — like the merge/withdraw/reconcile tests above; act_for is on, so reconcile runs.)
    SD_REPO = "acme/sync-drop-test"
    SD_KEEP, SD_DROP = "backend/auth.py", "backend/worker.py"   # both exist in the fixture graph
    def sd_pr(action, number, author, sha):                     # a PR event targeting SD_REPO (not the shared acme/app)
        return {"action": action, "number": number, "installation": {"id": 4242},
                "repository": {"full_name": SD_REPO, "default_branch": "main",
                               "id": _fixture_repo_id(SD_REPO)},
                "pull_request": {"base": {"ref": "main", "sha": SHA}, "head": {"sha": sha},
                                 "user": {"login": author}, "merged": False}}
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), SD_REPO, "main", SHA))
    push_gh.files_by_pr[80] = [SD_KEEP, SD_DROP]
    push_gh.files_by_pr[81] = [SD_DROP]                          # a SECOND PR contends on the file PR-80 will drop
    # PR-80 (alice) opens touching BOTH files → both lanes GRANTED (active).
    S.handle_event("pull_request", sd_pr("opened", 80, "alice", f"{80:040x}"), db, push_gh)
    sd80_open = _json.loads(br_claim_states(SD_REPO, "PR-80"))
    # PR-81 (bob) opens touching ONLY the soon-to-be-dropped file → that lane is held by PR-80 → bob WAITS.
    S.handle_event("pull_request", sd_pr("opened", 81, "bob", f"{81:040x}"), db, push_gh)
    sd81_before = _json.loads(br_claim_states(SD_REPO, "PR-81"))
    # PR-80 synchronizes with ONLY the keep file (worker.py dropped). Reconcile must RELEASE PR-80's worker.py
    # claim (and KEEP auth.py), which PROMOTES PR-81 to active — the dropped file no longer phantom-blocks.
    push_gh.files_by_pr[80] = [SD_KEEP]
    S.handle_event("pull_request", sd_pr("synchronize", 80, "alice", f"{80:040x}"), db, push_gh)
    sd80_after_paths = admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT COALESCE(jsonb_agg(target_path ORDER BY target_path),'[]'::jsonb)::text
          FROM core.claim WHERE repo=%s AND change_id='PR-80' AND branch='main'
            AND claim_state IN ('active','waiting')""", (SD_REPO,))
    sd80_dropped_live = admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id='PR-80' AND branch='main'
            AND target_path=%s AND claim_state IN ('active','waiting')""", (SD_REPO, SD_DROP))
    sd81_after = _json.loads(br_claim_states(SD_REPO, "PR-81"))
    checks.append((
        f"synchronize-drop precondition: PR-80 holds both lanes; PR-81 WAITS behind the file PR-80 will drop "
        f"(PR-80={sd80_open}, PR-81={sd81_before})",
        sorted(sd80_open) == ["active", "active"] and sd81_before == ["waiting"]))
    checks.append((
        f"synchronize-drop: the dropped file's claim is RELEASED (no longer live); the kept file stays "
        f"(PR-80 live paths after sync={sd80_after_paths}, dropped-file live={sd80_dropped_live})",
        sd80_dropped_live == 0 and _json.loads(sd80_after_paths) == [SD_KEEP]))
    checks.append((
        f"synchronize-drop: the dropped file no longer phantom-blocks — PR-81 is PROMOTED to active "
        f"(PR-81 live states={sd81_after})", sd81_after == ["active"]))

    # WEBHOOK IDEMPOTENCY (GitHub redelivers events at-least-once): the SAME event delivered twice must record
    # the landing/push exactly ONCE — never a duplicate row that corrupts the records ledger or inflates counts.
    # (a) function level: record_landing twice with identical (repo,branch,sha,paths) → first records N, the
    #     redelivery records 0, and the ledger holds exactly one 'landed' per path.
    IDEM_REPO = "acme/idem-test"
    IDEM_SHA = "1f" * 20
    n1 = db("SELECT core.record_landing_with_authority(%s,%s,%s,%s,%s)",
            (IDEM_REPO, "main", IDEM_SHA, ["a/x.py", "a/y.py"], "alice"))
    n2 = db("SELECT core.record_landing_with_authority(%s,%s,%s,%s,%s)",
            (IDEM_REPO, "main", IDEM_SHA, ["a/x.py", "a/y.py"], "alice"))
    idem_landed = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int FROM core.event WHERE kind='landed' AND repo=%s AND commit_sha=%s
        """, (IDEM_REPO, IDEM_SHA))
    checks.append((f"idempotent landing: a re-delivery records 0 + the ledger holds each landing once "
                   f"(n1={n1}, n2={n2}, rows={idem_landed})",
                   n1 == 2 and n2 == 0 and idem_landed == 2))

    # (b) through the server: RE-DELIVER alice's COLL_REPO push (same sha) — no new 'landed'/'push' row, and
    #     the collision count is unchanged.
    def _count_events(kind, repo, sha, path=None):
        if path is None:
            return admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
                SELECT count(*)::int FROM core.event WHERE kind=%s AND repo=%s AND commit_sha=%s""",
                         (kind, repo, sha))
        return admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
            SELECT count(*)::int FROM core.event WHERE kind=%s AND repo=%s AND path=%s AND commit_sha=%s""",
                     (kind, repo, path, sha))
    before_l = _count_events("landed", COLL_REPO, SHA_ALICE, SHARED_PATH)
    before_p = _count_events("push", COLL_REPO, SHA_ALICE)
    S.handle_event("push", push_payload(SHA_ALICE, "alice", [SHARED_PATH]), db, push_gh)   # GitHub redelivery
    after_l = _count_events("landed", COLL_REPO, SHA_ALICE, SHARED_PATH)
    after_p = _count_events("push", COLL_REPO, SHA_ALICE)
    coll_after_raw = db("SELECT core.collisions_on_main(%s,%s,%s)", (COLL_REPO, "main", "14 days"))
    coll_after = (_json.loads(coll_after_raw) if isinstance(coll_after_raw, str) else (coll_after_raw or {})).get("collisions_count", 0)
    checks.append((f"idempotent redelivery via the server: landed {before_l}→{after_l}, push {before_p}→{after_p}, "
                   f"collisions {collisions_count}→{coll_after} (all unchanged)",
                   before_l == 1 and after_l == 1 and before_p == 1 and after_p == 1 and coll_after == collisions_count))

    # Destructive uninstall semantics now require an exact claimed durable delivery plus live App proof. The former
    # direct handler invocation in this broad server smoke bypassed both authorities and became a misleading second
    # lifecycle test. Account-wide purge/retention is covered end-to-end by test_lifecycle_e2e,
    # test_uninstall_resurrection and test_account_lifecycle_fencing; keep only the privilege boundary here.
    PURGE_REPO = "acme/purge-test"
    purge_writer_error = None
    try:
        make_db("veripsa_demo_agent")("SELECT core.purge_repo_with_authority(%s)", (PURGE_REPO,))
    except psycopg2.Error as e:
        purge_writer_error = e.pgcode
    checks.append(("a plain writer cannot call the rollback-compatible repository purge shim",
                   purge_writer_error == "42501"))

    # ACK-FAST: webhooks are verified + enqueued, then processed on a SINGLE background worker so do_POST can
    # ACK within GitHub's ~10s timeout (a slow clone/extract or a rate-limit wait no longer blocks the reply).
    # The worker calls handle_event (here: a recording fake, so no DB needed). Processing stays serialized.
    seen = []
    eq = S.EventQueue(db=None, gh=None, process=lambda et, pl, d, g: seen.append((et, pl.get("number"))), account_of=S._event_account_key, maxsize=8).start()
    assert eq.submit("pull_request", {"number": 99})
    eq.wait_idle(2.0)
    checks.append(("ack-fast: a submitted event is processed by the background worker (serialized)",
                   seen == [("pull_request", 99)]))

    # the worker survives a per-event exception (one bad event must not kill the queue)
    survived = []
    def _boom(et, pl, d, g):
        if pl.get("number") == 1:
            raise RuntimeError("boom")
        survived.append(pl.get("number"))
    eq2 = S.EventQueue(db=None, gh=None, process=_boom, account_of=S._event_account_key,
                       maxsize=8, retry_attempts=1).start()
    eq2.submit("push", {"number": 1})    # raises in the worker → caught + logged
    eq2.submit("push", {"number": 2})    # must still be processed
    eq2.wait_idle(2.0)
    checks.append(("ack-fast: a worker exception on one event does NOT kill the worker (the next still runs)",
                   survived == [2]))

    # 202-THEN-FAIL DELIVERY SAFETY: once do_POST has returned 202, GitHub will not reliably redeliver a worker
    # exception. A transient DB/network failure must be retried inside EventQueue, not counted as a terminal loss.
    retry_seen = []
    def _flaky(et, pl, d, g):
        retry_seen.append(pl.get("number"))
        if len(retry_seen) == 1:
            raise RuntimeError("temporary")
    retryq = S.EventQueue(db=None, gh=None, process=_flaky, account_of=S._event_account_key,
                          maxsize=8, retry_attempts=2, retry_base_seconds=0).start()
    retryq.submit("pull_request", {"number": 404})
    retryq.wait_idle(2.0)
    retry_snap = S.health_snapshot(retryq)
    checks.append((f"ack-fast: a post-202 transient worker failure is internally retried and then processed "
                   f"(seen={retry_seen}, proc={retry_snap['processed']}, failed={retry_snap['failed']}, "
                   f"retried={retry_snap['retried']})",
                   retry_seen == [404, 404] and retry_snap["processed"] == 1
                   and retry_snap["failed"] == 0 and retry_snap["retried"] == 1))

    terminal_attempts = []
    def _always_bad(et, pl, d, g):
        terminal_attempts.append(pl.get("number"))
        raise RuntimeError("permanent")
    terminalq = S.EventQueue(db=None, gh=None, process=_always_bad, account_of=S._event_account_key,
                             maxsize=8, retry_attempts=2, retry_base_seconds=0).start()
    terminalq.submit("pull_request", {"number": 500})
    terminalq.wait_idle(2.0)
    terminal_snap = S.health_snapshot(terminalq)
    checks.append((f"ack-fast: a permanent post-202 worker failure is bounded and counted once after retry "
                   f"(attempts={len(terminal_attempts)}, failed={terminal_snap['failed']}, "
                   f"retried={terminal_snap['retried']})",
                   terminal_attempts == [500, 500] and terminal_snap["failed"] == 1
                   and terminal_snap["retried"] == 1))

    # DURABLE DELIVERY INBOX (root ack-fast fix): a verified webhook is persisted BEFORE 202 as a SANITIZED
    # operational delivery. If the process dies after insert but before/while the worker runs, boot recovery can
    # re-submit the row from DB. Success clears the stored payload to `{}`; direct table access stays denied by
    # the grant gate (covered in test_tamper_grants).
    import delivery_queue as DQ
    store = S.DeliveryStore(BR_DSN, max_pending=20, max_attempts=2, stale_seconds=1)
    raw_push = {"repository": {"full_name": "acme/durable", "default_branch": "main", "owner": {"id": 4242}},
                "ref": "refs/heads/main", "after": "a" * 40,
                "head_commit": {"timestamp": "2026-06-21T00:00:00Z", "message": "SECRET"},
                "pusher": {"name": "alice"}, "sender": {"login": "alice", "type": "User"},
                "commits": [{"id": "c1", "message": "SECRET", "added": ["src/a.py"],
                             "modified": [], "removed": []}]}
    dsub = store.submit("push", raw_push, "durable-delivery-1", account_key="4242", repo="acme/durable")
    dpending = [r for r in store.pending() if r.get("key") == dsub.get("key")]
    stored_payload_text = json.dumps(dpending[0]["payload"], sort_keys=True) if dpending else ""
    checks.append(("durable delivery: enqueue stores a sanitized payload (paths/shas kept, commit messages stripped)",
                   dsub.get("accepted") and dpending and "src/a.py" in stored_payload_text
                   and "SECRET" not in stored_payload_text and "message" not in stored_payload_text))
    raw_suite = {"action": "requested", "installation": {"id": 4242},
                 "repository": {"full_name": "acme/durable", "default_branch": "main", "owner": {"id": 4242}},
                 "check_suite": {"head_sha": "b" * 40,
                                 "app": {"slug": "veripsa-core", "name": "Veripsa Core",
                                         "description": "freeform app text", "html_url": "https://example.invalid"},
                                 "pull_requests": [{"number": 608, "base": {"ref": "main"}}]}}
    sanitized_suite = DQ.sanitize_payload("check_suite", raw_suite)
    sanitized_suite_text = json.dumps(sanitized_suite, sort_keys=True)
    checks.append(("durable delivery: check_suite sanitizer preserves app slug/name for the Veripsa backstop",
                   sanitized_suite.get("check_suite", {}).get("app") == {"name": "Veripsa Core", "slug": "veripsa-core"}
                   and "description" not in sanitized_suite_text and "html_url" not in sanitized_suite_text
                   and sanitized_suite.get("check_suite", {}).get("pull_requests", [{}])[0].get("number") == 608))
    raw_run = {"action": "rerequested", "installation": {"id": 4242},
               "repository": {"full_name": "acme/durable", "default_branch": "main", "owner": {"id": 4242}},
               "check_run": {"head_sha": "c" * 40,
                             "app": {"slug": "veripsa-core", "name": "Veripsa Core", "owner": {"login": "noise"}},
                             "check_suite": {"app": {"slug": "veripsa-core", "name": "Veripsa Core",
                                                     "external_url": "https://example.invalid"},
                                             "pull_requests": [{"number": 609, "base": {"ref": "main"}}]}}}
    sanitized_run = DQ.sanitize_payload("check_run", raw_run)
    sanitized_run_text = json.dumps(sanitized_run, sort_keys=True)
    checks.append(("durable delivery: check_run sanitizer preserves nested suite app slug/name without extra app text",
                   sanitized_run.get("check_run", {}).get("app") == {"name": "Veripsa Core", "slug": "veripsa-core"}
                   and sanitized_run.get("check_run", {}).get("check_suite", {}).get("app") == {"name": "Veripsa Core", "slug": "veripsa-core"}
                   and "external_url" not in sanitized_run_text and "noise" not in sanitized_run_text
                   and sanitized_run.get("check_run", {}).get("check_suite", {}).get("pull_requests", [{}])[0].get("number") == 609))

    durable_attempts = []
    def _durable_flaky(et, pl, d, g, coalesce=None):
        durable_attempts.append(pl.get("after"))
        if len(durable_attempts) == 1:
            raise RuntimeError("temporary durable failure")
    dq = S.EventQueue(db=None, gh=None, process=store.wrap_processor(_durable_flaky),
                      account_of=S._event_account_key, repo_of=S._event_repo,
                      branch_from_ref=S._branch_from_ref, maxsize=8,
                      retry_attempts=2, retry_base_seconds=0).start()
    dq.submit(dsub["event_type"], dsub["payload"], dsub["delivery"])
    dq.wait_idle(3.0)
    first_generation_state = admin(
        "SELECT status || ':' || attempts::text FROM core.webhook_delivery WHERE delivery_key=%s",
        (dsub["key"],),
    )
    first_generation_retries = dq.retried()
    # One in-memory dequeue owns exactly one durable generation. Recovery, not
    # EventQueue's inline loop, publishes the next DB generation.
    dq.submit(
        dsub["event_type"],
        dsub["payload"],
        dsub["delivery"],
        register_push=False,
        recovered=True,
    )
    dq.wait_idle(3.0)
    depth_after_done = store.depth()
    done_payload = admin("SELECT payload::text FROM core.webhook_delivery WHERE delivery_key=%s", (dsub["key"],))
    checks.append((f"durable delivery: one failed dequeue releases generation 1; a later recovery generation "
                   f"succeeds and clears payload (first={first_generation_state}, attempts={len(durable_attempts)}, "
                   f"depth={depth_after_done}, payload={done_payload})",
                   durable_attempts == ["a" * 40, "a" * 40]
                   and first_generation_state == "queued:1"
                   and first_generation_retries == 0
                   and int(depth_after_done.get("done", 0)) >= 1 and done_payload == "{}"))

    dsub2 = store.submit("push", raw_push, "durable-delivery-2", account_key="4242", repo="acme/durable")
    recovered_seen = []
    def _durable_record(et, pl, d, g, coalesce=None):
        recovered_seen.append(pl.get("after"))
    recoverq = S.EventQueue(db=None, gh=None, process=store.wrap_processor(_durable_record),
                            account_of=S._event_account_key, repo_of=S._event_repo,
                            branch_from_ref=S._branch_from_ref, maxsize=8,
                            retry_attempts=2, retry_base_seconds=0).start()
    for row in store.pending():
        if row.get("key") == dsub2.get("key"):
            recoverq.submit(row["event_type"], DQ.with_delivery_key(row["payload"], row["key"]), row["delivery"])
    recoverq.wait_idle(3.0)
    checks.append(("durable delivery: boot/recovery can process a delivery inserted before the worker saw it",
                   recovered_seen == ["a" * 40]
                   and admin("SELECT status FROM core.webhook_delivery WHERE delivery_key=%s", (dsub2["key"],)) == "done"))

    dsub3 = store.submit("push", raw_push, "durable-delivery-3", account_key="4242", repo="acme/durable")
    terminal_durable_attempts = []
    def _durable_always_bad(et, pl, d, g, coalesce=None):
        terminal_durable_attempts.append(pl.get("after"))
        raise RuntimeError("permanent durable failure")
    failedq = S.EventQueue(db=None, gh=None, process=store.wrap_processor(_durable_always_bad),
                           account_of=S._event_account_key, repo_of=S._event_repo,
                           branch_from_ref=S._branch_from_ref, maxsize=8,
                           retry_attempts=2, retry_base_seconds=0).start()
    failedq.submit(dsub3["event_type"], dsub3["payload"], dsub3["delivery"])
    failedq.wait_idle(3.0)
    failedq.submit(
        dsub3["event_type"],
        dsub3["payload"],
        dsub3["delivery"],
        register_push=False,
        recovered=True,
    )
    failedq.wait_idle(3.0)
    failed_state = admin("SELECT status || ':' || attempts::text FROM core.webhook_delivery WHERE delivery_key=%s",
                         (dsub3["key"],))
    redelivered = store.submit("push", raw_push, "durable-delivery-3", account_key="4242", repo="acme/durable")
    redelivered_state = admin("SELECT status || ':' || attempts::text FROM core.webhook_delivery WHERE delivery_key=%s",
                              (dsub3["key"],))
    checks.append((f"durable delivery: a terminal failed delivery can be manually redelivered and becomes claimable "
                   f"again (before={failed_state}, after={redelivered_state}, attempts={len(terminal_durable_attempts)})",
                   failed_state == "failed:2" and redelivered.get("queued") is True
                   and redelivered_state == "queued:0"))

    # bounded queue: with no worker draining it, a full queue rejects further submits → caller 503s → redeliver
    eq3 = S.EventQueue(db=None, gh=None, process=lambda *a: None, account_of=S._event_account_key, maxsize=1)   # NOT started → nothing drains
    first_ok = eq3.submit("push", {"n": "a"})
    second_ok = eq3.submit("push", {"n": "b"})
    checks.append(("ack-fast: a full bounded queue rejects further submits (caller 503s → GitHub redelivers)",
                   first_ok is True and second_ok is False))

    # HEALTH / OBSERVABILITY: health_snapshot reflects the worker's REAL state (worker_alive, queue_depth,
    # processed/failed counts) — what /healthz returns. Content-free (counts only, no secrets/customer data).
    import threading as _thr, time as _time
    gate = _thr.Event()
    drained = []
    def _slow(et, pl, d, g):
        gate.wait(2.0); drained.append(pl.get("n"))
    hq = S.EventQueue(db=None, gh=None, process=_slow, account_of=S._event_account_key, maxsize=8).start()
    snap0 = S.health_snapshot(hq)
    checks.append((f"health: worker_alive + healthy true, zero counts at start "
                   f"(alive={snap0['worker_alive']}, depth={snap0['queue_depth']}, proc={snap0['processed']})",
                   snap0["worker_alive"] is True and snap0["healthy"] is True
                   and snap0["queue_depth"] == 0 and snap0["processed"] == 0 and snap0["failed"] == 0
                   and snap0["queue_maxsize"] == 8))
    hq.submit("push", {"n": 1}); hq.submit("push", {"n": 2})
    _time.sleep(0.05)                                  # worker picks up #1 and blocks on the gate; #2 stays queued
    snapd = S.health_snapshot(hq)
    checks.append((f"health: queue_depth reflects the backlog while the worker is busy (depth={snapd['queue_depth']})",
                   snapd["queue_depth"] >= 1))
    # STUCK-WORKER OBSERVABILITY: while the worker is WEDGED inside event #1 (blocked on the gate), inflight_age
    # is a real, growing number — the signal a wedged-but-alive worker (the lying-green) would expose to the
    # watchdog. snap0 (idle, before any submit) had it None. This is the live-worker wiring behind worker_stuck.
    checks.append((f"health: inflight_age_seconds is a number while the worker is wedged on an event "
                   f"(idle={snap0['inflight_age_seconds']}, busy={snapd['inflight_age_seconds']})",
                   snap0["inflight_age_seconds"] is None
                   and isinstance(snapd["inflight_age_seconds"], (int, float)) and snapd["inflight_age_seconds"] >= 0))
    gate.set(); hq.wait_idle(2.0)
    snap2 = S.health_snapshot(hq)
    checks.append((f"health: processed increments after the worker drains (proc={snap2['processed']}, fail={snap2['failed']})",
                   snap2["processed"] == 2 and snap2["failed"] == 0))
    checks.append((f"health: inflight_age_seconds clears to None once the worker is idle again "
                   f"(after-drain={snap2['inflight_age_seconds']})",
                   snap2["inflight_age_seconds"] is None))

    def _raise(et, pl, d, g):
        raise RuntimeError("boom")
    fq = S.EventQueue(db=None, gh=None, process=_raise, account_of=S._event_account_key,
                      maxsize=4, retry_attempts=1).start()
    fq.submit("push", {"n": 1}); fq.wait_idle(2.0)
    checks.append((f"health: failed increments when the worker catches an exception (fail={S.health_snapshot(fq)['failed']})",
                   S.health_snapshot(fq)["failed"] == 1))

    dead = S.EventQueue(db=None, gh=None, process=lambda *a: None, account_of=S._event_account_key)             # NOT started → thread not alive
    deadsnap = S.health_snapshot(dead)
    checks.append(("health: an unstarted/dead worker reports worker_alive=false + healthy=false (→ /healthz 503)",
                   deadsnap["worker_alive"] is False and deadsnap["healthy"] is False))

    # ---- MULTI-TENANT FAIRNESS (the noisy-neighbor guard) ---------------------------------------------------
    # ONE worker drains the queue, so the DRAIN ORDER is the whole fairness story. A strict global FIFO lets one
    # tenant's force-push BURST (a fleet hammering a big monorepo) head-of-line-block every OTHER tenant. The
    # _FairQueue drains per-account round-robin + caps each account's queued slice. Tenant key = the stable owner
    # account id (the SAME key make_db_processor routes RLS by), via _event_account_key.
    def _acct_event(owner_id, n):                                  # a minimal but real-shaped event payload
        return {"repository": {"full_name": f"{owner_id}/r", "owner": {"id": owner_id}}, "n": n}

    # (a) ANTI-STARVATION: account A floods 50 events, then account B submits ONE last (B is dead-last in
    #     wall-clock order). Under strict FIFO B would drain at position 50; under fair round-robin B must come
    #     out within the first couple — its verdict is NOT minutes-late behind A's monorepo burst.
    fq_star = S._FairQueue(maxsize=1000, per_account_cap=1000, account_of=S._event_account_key)
    for i in range(50):
        fq_star.put_nowait(("push", _acct_event("A", i), None))
    fq_star.put_nowait(("push", _acct_event("B", 0), None))
    drained = []
    for _ in range(51):
        _, pl, _d = fq_star.get(); drained.append(pl["repository"]["owner"]["id"]); fq_star.task_done()
    b_pos = drained.index("B")
    checks.append((f"fairness: a flooding tenant (A×50) does NOT starve another tenant's event "
                   f"(B drained at position {b_pos}/51; strict FIFO would be 50)",
                   b_pos <= 1 and fq_star.unfinished_tasks == 0))

    # (b) PER-ACCOUNT CAP: one runaway/free tenant can hold at most per_account_cap queued events — past it ITS
    #     submits are rejected (it 503s → redelivers its own later) while a DIFFERENT tenant's global room is
    #     untouched (so a flood can't 503 every other tenant's deliveries out — that is itself a fairness break).
    fq_cap = S._FairQueue(maxsize=1000, per_account_cap=5, account_of=S._event_account_key)
    a_ok = sum(1 for i in range(20) if _try_put(fq_cap, ("push", _acct_event("A", i), None)))
    b_ok = _try_put(fq_cap, ("push", _acct_event("B", 0), None))   # different tenant: still accepted
    checks.append((f"fairness: a single tenant is capped at its per-account slice (A accepted={a_ok}/20, cap=5) "
                   f"while another tenant is unaffected (B accepted={b_ok})",
                   a_ok == 5 and b_ok is True))

    # (c) INVARIANTS PRESERVED: the GLOBAL bound still rejects at maxsize (→ 503 → redeliver), and order WITHIN a
    #     single account stays FIFO (push→PR causality + per-repo serialization must not be reordered by fairness).
    fq_glob = S._FairQueue(maxsize=3, per_account_cap=100, account_of=S._event_account_key)
    glob_ok = sum(1 for i in range(10) if _try_put(fq_glob, ("push", _acct_event(i, 0), None)))
    fq_fifo = S._FairQueue(maxsize=100, per_account_cap=100, account_of=S._event_account_key)
    for i in range(5):
        fq_fifo.put_nowait(("push", _acct_event("A", i), None))
    in_acct_order = [fq_fifo.get()[1]["n"] for _ in range(5)]
    checks.append((f"fairness: global bound still caps total queue (accepted {glob_ok} at maxsize=3) AND "
                   f"within-account order stays FIFO ({in_acct_order})",
                   glob_ok == 3 and in_acct_order == [0, 1, 2, 3, 4]))

    # (d) END-TO-END through the live EventQueue + a real round-robin DRAIN by the single worker: A floods, B is
    #     submitted last, and the worker (one slow processor) must SERVICE B early — not after all of A. Proves
    #     the fairness lives in the actual EventQueue.submit→worker path, not only the bare queue.
    import threading as _thr2
    release = _thr2.Event()
    drain_order = []
    def _record(et, pl, d, g):
        release.wait(2.0)                                          # hold turn 1 so the burst is fully enqueued
        drain_order.append(pl["repository"]["owner"]["id"])
    fairq = S.EventQueue(db=None, gh=None, process=_record, account_of=S._event_account_key, maxsize=1000, per_account_cap=1000).start()
    fairq.submit("push", _acct_event("A", -1))                    # worker grabs this one and blocks on release
    _time.sleep(0.03)
    for i in range(20):                                            # A floods while the worker is held
        fairq.submit("push", _acct_event("A", i))
    fairq.submit("push", _acct_event("B", 0))                     # B arrives LAST
    release.set(); fairq.wait_idle(3.0)
    e2e_b_pos = drain_order.index("B") if "B" in drain_order else 999
    checks.append((f"fairness (end-to-end via EventQueue + worker): B serviced early despite arriving after an "
                   f"A-flood (B at drain position {e2e_b_pos}/{len(drain_order)})",
                   e2e_b_pos <= 2 and len(drain_order) == 22))

    # DB PROCESSOR (the live worker's processor): one pooled connection + a per-repo advisory lock per event
    # (safe even across instances / scale-out). Assert an event processed through it lands its writes.
    proc_gh = FakeGitHub({77: ["backend/helpers.py"]})
    S.make_db_processor(f"postgresql://veripsa_app@localhost/{DB}")("pull_request", pr_payload("opened", 77, "zoe"), None, proc_gh)
    # pr_payload carries installation id 4242 → the processor routes the event into THAT installation's own
    # tenant account (ACCT-GH-4242), NOT the shared ACCT-DEMO. So the claim lands there (multi-tenant routing).
    proc_claim = admin(
        """
        SELECT set_config('core.current_account','ACCT-GH-4242',true);
        SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id='PR-77' AND claim_state IN ('active','waiting')
        """, (REPO,))
    demo_claim = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id='PR-77'
        """, (REPO,))
    checks.append((f"db processor + multi-tenant: the event landed in installation 4242's OWN account "
                   f"(ACCT-GH-4242 claims={proc_claim}), NOT the shared ACCT-DEMO ({demo_claim})",
                   proc_claim is not None and proc_claim > 0 and demo_claim == 0))

    # TWO TENANTS through the FULL server pipeline (no second real GitHub account needed — a correct
    # multi-tenant design is N-tenant by construction). Two PR events with DIFFERENT owning accounts, each run
    # through the REAL per-event processor (connection + advisory lock + enter + handle_event) with its own
    # FakeGitHub. Keyed by the STABLE account id (repository.owner.id) — note installation.id differs from it.
    def tenant_pr(owner_id, repo_full, author):
        return {"action": "opened", "number": 30, "installation": {"id": 9000 + owner_id},
                "repository": {"full_name": repo_full, "default_branch": "main",
                               "id": _fixture_repo_id(repo_full),
                               "owner": {"id": owner_id, "login": f"org{owner_id}"}},
                "pull_request": {"base": {"ref": "main", "sha": SHA},
                                 "head": {"sha": f"{30:040x}"},
                                 "user": {"login": author}, "merged": False}}
    _activate_empty_installation(111, 9111, "server-route-tenant-111")
    _activate_empty_installation(222, 9222, "server-route-tenant-222")
    proc = S.make_db_processor(f"postgresql://veripsa_app@localhost/{DB}")
    proc("pull_request", tenant_pr(111, "orgA/svc", "alice"), None,
         FakeGitHub({30: ["svc/a.py"]}, owner_id=111))
    proc("pull_request", tenant_pr(222, "orgB/svc", "bob"), None,
         FakeGitHub({30: ["svc/b.py"]}, owner_id=222))

    def claims_in(account, repo):
        return admin("""SELECT set_config('core.current_account',%s,true);
            SELECT count(*)::int FROM core.claim WHERE repo=%s AND claim_state IN ('active','waiting')""", (account, repo))
    a_own, b_own = claims_in("ACCT-GH-111", "orgA/svc"), claims_in("ACCT-GH-222", "orgB/svc")
    a_cross, b_cross = claims_in("ACCT-GH-111", "orgB/svc"), claims_in("ACCT-GH-222", "orgA/svc")
    checks.append((f"two tenants via the full pipeline: each lands in its OWN account keyed by owner id, NOT "
                   f"the install id (A→ACCT-GH-111={a_own}, B→ACCT-GH-222={b_own})", a_own >= 1 and b_own >= 1))
    checks.append((f"two tenants ISOLATED end-to-end: neither account holds the other's repo (A↛orgB={a_cross}, "
                   f"B↛orgA={b_cross})", a_cross == 0 and b_cross == 0))

    # POISON-EVENT TOLERANCE: a malformed / partial webhook payload must be a CLEAN no-op (skipped), never an
    # exception — a single bad delivery can't be allowed to churn the worker's 'failed' counter or get dropped.
    pr_poison = S.handle_event("pull_request", {"action": "opened", "number": 7}, db, FakeGitHub({}))   # no repo/pull_request
    push_poison = S.handle_event("push", {"ref": "refs/heads/main", "after": "a" * 40}, db, FakeGitHub({}))  # no repository
    checks.append(("poison-event: a malformed pull_request payload is a clean no-op (skipped), not a crash",
                   isinstance(pr_poison, dict) and pr_poison.get("skipped") is not None))
    checks.append(("poison-event: a malformed push payload (no repository) is a clean no-op (skipped)",
                   isinstance(push_poison, dict) and push_poison.get("skipped") is not None))

    # ── ADVERSARIAL PAYLOAD ROBUSTNESS (never-crash invariant). The webhook endpoint is PUBLIC and the body is
    #    ATTACKER-CONTROLLED: we read it before we can verify the HMAC, and even a valid-signature delivery can
    #    carry semantically junk content. A crash that ESCAPES handle_event would (on the live processor) leave a
    #    per-repo advisory lock held until connection-close and churn the worker's 'failed' counter for what must
    #    be a clean no-op — and the product sells "degrades gracefully, never crashes". The pre-fix gap: the
    #    `payload.get("X") or {}` idiom guards a MISSING/None/falsy field but PASSES a WRONG-TYPED one straight
    #    into the next `.get()`/iteration/index, raising AttributeError/TypeError. These feed hostile SHAPES at
    #    every event type and assert handle_event RETURNS a dict (a clean no-op / honest skip), never raises.
    adversarial_delivery_seq = 0

    def _no_crash(label, event_type, payload):
        """handle_event must return a dict (or raise nothing) for ANY payload. Returns (label, passed)."""
        nonlocal adversarial_delivery_seq
        try:
            action = payload.get("action") if isinstance(payload, dict) else None
            if (event_type, action) in {
                    ("installation", "created"), ("installation", "unsuspend"),
                    ("installation", "new_permissions_accepted"),
                    ("installation_repositories", "added")}:
                adversarial_delivery_seq += 1
                owner_id = 980000 + adversarial_delivery_seq
                copied = json.loads(json.dumps(payload))
                installation = copied.setdefault("installation", {})
                if not isinstance(installation, dict):
                    return (label, True)  # immutable poison shape: the live processor drops it before admission
                installation["id"] = owner_id
                installation["account"] = {"id": owner_id, "login": f"org{owner_id}",
                                           "type": "Organization"}
                r = _direct_install_activation(
                    copied, FakeGitHub({}, owner_id=owner_id),
                    f"server-adversarial-activation-{adversarial_delivery_seq}", event_type,
                )
            elif (event_type, action) == ("installation", "deleted"):
                # A malformed destructive delivery is immutable poison, not retryable authority. Exercise the
                # live admission boundary (which drops it) rather than calling the privileged handler directly.
                S.make_db_processor(BR_DSN)(event_type, json.loads(json.dumps(payload)), None, FakeGitHub({}))
                r = {}
            else:
                r = S.handle_event(event_type, payload, db, FakeGitHub({}))
            return (label, isinstance(r, dict))
        except Exception as e:                      # ANY escaped exception is the failure this audit hunts
            return (f"{label} — ESCAPED {type(e).__name__}: {str(e)[:80]}", False)

    # (1) WRONG TYPES for nested objects: a string where a dict is expected (installation / repository /
    #     pull_request / its base+head / user / head.repo), a string where a list is expected (repositories /
    #     commits), and a list whose entries are not objects. Each must be a clean no-op, never a crash.
    adversarial = [
        ("adversarial type: installation is a STRING (pull_request)", "pull_request",
         {"installation": "nope", "action": "opened", "number": 1,
          "repository": {"full_name": "a/b", "default_branch": "main"},
          "pull_request": {"base": {"ref": "main"}, "head": {"sha": "a"}}}),
        ("adversarial type: installation is a STRING (push)", "push",
         {"installation": "nope", "ref": "refs/heads/x", "after": "a" * 40, "repository": {"full_name": "a/b"}}),
        ("adversarial type: repository is a STRING (pull_request)", "pull_request",
         {"action": "opened", "number": 1, "repository": "nope",
          "pull_request": {"base": {"ref": "main"}, "head": {"sha": "a"}}}),
        ("adversarial type: pull_request is a STRING", "pull_request",
         {"action": "opened", "number": 1, "repository": {"full_name": "a/b", "default_branch": "main"},
          "pull_request": "nope"}),
        ("adversarial type: pull_request.base / .head are STRINGS", "pull_request",
         {"action": "opened", "number": 1, "repository": {"full_name": "a/b", "default_branch": "main"},
          "pull_request": {"base": "nope", "head": "nope", "user": "nope"}}),
        ("adversarial type: pull_request.head.repo is a STRING (fork detect)", "pull_request",
         {"action": "opened", "number": 1, "installation": {"id": 1},
          "repository": {"full_name": "a/b", "default_branch": "main"},
          "pull_request": {"base": {"ref": "main", "repo": "nope"}, "head": {"sha": "a", "repo": "nope"},
                           "user": {"login": "x"}}}),
        ("adversarial type: push commits is a STRING", "push",
         {"ref": "refs/heads/x", "after": "a" * 40,
          "repository": {"full_name": "a/b", "default_branch": "main"}, "commits": "nope"}),
        ("adversarial type: push commits entries are NON-objects", "push",
         {"ref": "refs/heads/feature/x", "after": "a" * 40,
          "repository": {"full_name": "a/b", "default_branch": "main"}, "commits": ["x", 1, None]}),
        ("adversarial type: push ref is a NON-string, after is a LIST, repository a STRING", "push",
         {"ref": 123, "after": ["x"], "repository": "nope"}),
        ("adversarial type: push after is an INT", "push",
         {"ref": "refs/heads/main", "after": 99, "repository": {"full_name": "a/b", "default_branch": "main"}}),
        ("adversarial type: installation repositories is a STRING", "installation",
         {"action": "created", "installation": {"id": 1}, "repositories": "nope"}),
        ("adversarial type: installation repositories entries are NON-objects", "installation",
         {"action": "created", "installation": {"id": 1}, "repositories": ["x", 1, None]}),
        ("adversarial type: uninstall repositories entries are NON-objects", "installation",
         {"action": "deleted", "installation": {"id": 1}, "repositories": ["x", 1, None]}),
        ("adversarial type: installation_repositories added is a STRING", "installation_repositories",
         {"action": "added", "installation": {"id": 1}, "repositories_added": "nope"}),
        ("adversarial type: check_suite node is a STRING", "check_suite",
         {"action": "completed", "installation": {"id": 1}, "repository": {"full_name": "a/b"},
          "check_suite": "nope"}),
        ("adversarial type: check_suite head_sha is an INT, pull_requests a STRING", "check_suite",
         {"action": "completed", "repository": {"full_name": "a/b"},
          "check_suite": {"conclusion": "failure", "head_sha": 123, "pull_requests": "nope"}}),
        ("adversarial type: check_suite pull_requests entries are NON-objects", "check_suite",
         {"action": "completed", "repository": {"full_name": "a/b"},
          "check_suite": {"conclusion": "failure", "head_sha": "f" * 40, "pull_requests": ["x", 1, None]}}),
        ("adversarial type: repository renamed payload is all STRINGS", "repository",
         {"action": "renamed", "repository": "nope", "changes": "nope"}),
        ("adversarial type: repository deleted with a STRING repository", "repository",
         {"action": "deleted", "repository": "nope"}),
        ("adversarial type: repository transferred payload is all STRINGS", "repository",
         {"action": "transferred", "repository": "nope", "changes": "nope"}),
        ("adversarial type: repository archived with a STRING repository", "repository",
         {"action": "archived", "repository": "nope"}),
        # top-level payload shapes that aren't even the right container
        ("adversarial type: empty payload {}", "pull_request", {}),
        ("adversarial type: payload has only junk keys", "push", {"hello": "world", "x": [1, 2, 3]}),
        ("adversarial type: unknown event_type with a junk payload", "membership", {"a": {"b": "c"}}),
        # TOP-LEVEL payload is NOT EVEN A DICT. do_POST does `json.loads(body or b"{}")`, and valid JSON
        # `null` / `[...]` / `"x"` / `42` parses to None/list/str/int — sailing PAST do_POST's `except
        # ValueError` and into handle_event, where the FIRST payload.get(...) used to raise AttributeError
        # that ESCAPED the handler (churning the worker's 'failed' counter + holding the per-repo advisory
        # lock open). handle_event must coerce the container at the door → clean no-op dict, never a crash.
        ("adversarial type: top-level payload is None (JSON null)", "pull_request", None),
        ("adversarial type: top-level payload is a LIST (JSON array)", "push", [1, 2, 3]),
        ("adversarial type: top-level payload is a STRING (JSON string)", "pull_request", "garbage"),
        ("adversarial type: top-level payload is an INT (JSON number)", "installation", 42),
        ("adversarial type: top-level payload is a BOOL (JSON true)", "check_suite", True),
    ]
    for label, et, pl in adversarial:
        checks.append(_no_crash(label, et, pl))

    # (2) HOSTILE VALUES (control chars / unicode / RTL override / null byte / very long) in the string fields we
    #     route on — repo full_name, branch ref, author login. These are content-free strings; they must never
    #     crash the handler at the Python level (a value the DB rejects is caught by the worker, but the FIELD
    #     ACCESS + routing in handle_event must not raise). We send a NON-default base so the PR is skipped before
    #     any DB write — proving the value flows through the guards/branching without a crash.
    hostile_values = ["x" * 9000, "a\tb\nc", "café/auth.py", "‮evil", "ctrl\x01\x02", "emoji\U0001f4a5"]
    for v in hostile_values:
        checks.append(_no_crash(f"hostile value in repo/ref/login ({v[:16]!r})", "pull_request",
                                {"action": "opened", "number": 1, "installation": {"id": 1},
                                 "repository": {"full_name": v, "default_branch": "main"},
                                 "pull_request": {"base": {"ref": "not-main"}, "head": {"sha": "a", "ref": v},
                                                  "user": {"login": v}}}))
        checks.append(_no_crash(f"hostile value in push ref ({v[:16]!r})", "push",
                                {"ref": f"refs/heads/{v}", "after": "0" * 40,           # after=000… → branch-delete no-op
                                 "repository": {"full_name": "a/b", "default_branch": "main"}}))

    # (3) HUGE ARRAYS (DoS via fan-out): 10k repositories in one install, 10k commits each naming 10k files, and a
    #     10k-file PR. These must be BOUNDED (the ingest/PR/onboard caps) and NEVER OOM-or-crash the handler. We
    #     only assert no crash + a dict result here (the dedicated cap tests above assert the exact bound values).
    big_repos = {"action": "created", "installation": {"id": 1},
                 "repositories": [{"full_name": f"acme/huge-{i}"} for i in range(10000)]}
    checks.append(_no_crash("huge array: 10k repositories in one install event (bounded, no crash)",
                            "installation", big_repos))
    big_push = {"ref": "refs/heads/feature/huge", "after": "a" * 40,
                "repository": {"full_name": "acme/huge-push", "default_branch": "main"},
                "pusher": {"name": "bot"},
                "commits": [{"added": [], "modified": [f"src/f{i}.py" for i in range(10000)], "removed": []}]}
    checks.append(_no_crash("huge array: a push naming 10k changed files (bounded, no crash)", "push", big_push))
    mega = FakeGitHub({900: [f"src/g{i}.py" for i in range(10000)]})
    try:
        mega_res = S.handle_event("pull_request",
                                  {"action": "opened", "number": 900, "installation": {"id": 1},
                                   "repository": {"full_name": "acme/huge-pr", "default_branch": "main"},
                                   "pull_request": {"base": {"ref": "main"}, "head": {"sha": "a" * 40},
                                                    "user": {"login": "bot"}}}, db, mega)
        mega_ok = isinstance(mega_res, dict)
    except Exception as e:
        mega_ok = False
        mega_res = f"{type(e).__name__}: {str(e)[:80]}"
    checks.append((f"huge array: a 10k-file PR is handled (bounded by the per-PR cap, no crash) ({str(mega_res)[:50]})",
                   mega_ok))

    # (4) do_POST RECEIPT SEAMS (offline — exercise the exact branches do_POST runs before enqueue without a
    #     socket). An empty body parses to {} → handle_event no-ops (not a crash). A VALID signature over junk
    #     content still must not crash the brain. And the bounded queue rejects a flood (caller 503s).
    empty_payload = json.loads(b"" or b"{}")                 # do_POST's exact `json.loads(body or b"{}")`
    checks.append(("receipt: an EMPTY body parses to {} and handle_event no-ops (not a crash)",
                   empty_payload == {} and isinstance(S.handle_event("push", empty_payload, db, FakeGitHub({})), dict)))
    import hashlib as _hl, hmac as _hm
    class _KillSwitchWorker:
        def __init__(self):
            self.calls = []

        def submit(self, event_type, payload, delivery):
            self.calls.append((event_type, payload, delivery))
            return True

    class _KillSwitchStore:
        def __init__(self):
            self.calls = []

        def submit(self, event_type, payload, delivery, *, account_key=None, repo=None):
            self.calls.append((event_type, delivery, account_key, repo))
            stored = dict(payload)
            stored["_veripsa_delivery_key"] = delivery
            return {
                "accepted": True,
                "queued": True,
                "event_type": event_type,
                "payload": stored,
                "delivery": delivery,
            }

        def depth(self):
            return {}

    def _kill_switch_post(event_type, payload):
        body = json.dumps(payload).encode()
        sig = "sha256=" + _hm.new(b"sek", body, _hl.sha256).hexdigest()
        worker = _KillSwitchWorker()
        store = _KillSwitchStore()
        Handler = SH.make_handler(secret="sek", store=store, worker=worker, db=lambda *_a, **_k: None,
                                  dsn="", gh=None, persist_all=False)
        handler = Handler.__new__(Handler)
        handler.headers = {
            "Content-Length": str(len(body)),
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": event_type,
            "X-GitHub-Delivery": "D-KILL-SWITCH-OFFBOARD",
        }
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        statuses = []
        handler.send_response = lambda status: statuses.append(status)
        handler.end_headers = lambda: None
        handler.do_POST()
        return statuses[-1] if statuses else None, worker.calls, store.calls, handler.wfile.getvalue()

    _life_base = {
        "installation": {"id": 4242, "account": {"id": 6262, "login": "acme"}},
        "repository": {"id": 7007, "full_name": "acme/offboard-kill-switch"},
    }
    _delete_status, _delete_calls, _delete_store, _delete_body = _kill_switch_post(
        "repository", dict(_life_base, action="deleted"))
    _remove_status, _remove_calls, _remove_store, _remove_body = _kill_switch_post(
        "installation_repositories", {
            "action": "removed",
            "installation": _life_base["installation"],
            "repositories_removed": [_life_base["repository"]],
        })
    _transfer_status, _transfer_calls, _transfer_store, _transfer_body = _kill_switch_post(
        "repository", {
            "action": "transferred",
            "installation": _life_base["installation"],
            "repository": {"id": 7007, "name": "offboard-kill-switch",
                           "full_name": "new-acme/offboard-kill-switch",
                           "owner": {"id": 6262, "login": "new-acme"}},
            "changes": {"owner": {"from": {"organization": {
                "id": 5252, "login": "old-acme"}}}},
        })
    _push_status, _push_calls, _push_store, _push_body = _kill_switch_post(
        "push", dict(_life_base, ref="refs/heads/main", after="a" * 40))
    checks.append((
        "durable-inbox kill switch keeps destructive offboarding persisted and delivery-keyed, "
        "while ordinary events retain the memory-queue escape hatch",
        _delete_status == 202 and len(_delete_store) == 1 and len(_delete_calls) == 1
        and _delete_calls[0][1].get("_veripsa_delivery_key")
        and _delete_body == b"accepted"
        and _remove_status == 202 and len(_remove_store) == 1 and len(_remove_calls) == 1
        and _remove_calls[0][1].get("_veripsa_delivery_key")
        and _remove_body == b"accepted"
        and _transfer_status == 202 and len(_transfer_store) == 1 and len(_transfer_calls) == 1
        and _transfer_calls[0][1].get("_veripsa_delivery_key")
        and _transfer_body == b"accepted"
        and _push_status == 202 and _push_store == [] and len(_push_calls) == 1
        and "_veripsa_delivery_key" not in _push_calls[0][1] and _push_body == b"accepted",
    ))

    junk_body = json.dumps({"action": "opened", "pull_request": "this-is-not-an-object",
                            "repository": 12345, "number": [1, 2]}).encode()
    good_sig = "sha256=" + _hm.new(b"sek", junk_body, _hl.sha256).hexdigest()
    sig_ok = S.verify_signature("sek", junk_body, good_sig)  # a correctly-signed but semantically-junk delivery
    junk_payload = json.loads(junk_body)
    checks.append(("receipt: a VALID-signature but semantically-junk payload verifies AND no-ops (no crash)",
                   sig_ok and isinstance(S.handle_event("pull_request", junk_payload, db, FakeGitHub({})), dict)))

    # (5) END-TO-END through the ACK-FAST worker: a BATCH of poison events must all be processed as clean no-ops —
    #     the worker SURVIVES every one and its 'failed' counter stays 0 (a malformed delivery is a skip, not a
    #     failure). This is the live guarantee: one tenant's hostile payload can never stop processing for others.
    pois_done = []
    def _process_poison(et, pl, d, g):
        nonlocal adversarial_delivery_seq
        action = pl.get("action") if isinstance(pl, dict) else None
        if (et, action) in {
                ("installation", "created"), ("installation", "unsuspend"),
                ("installation", "new_permissions_accepted"),
                ("installation_repositories", "added")}:
            adversarial_delivery_seq += 1
            owner_id = 990000 + adversarial_delivery_seq
            copied = json.loads(json.dumps(pl))
            installation = copied.setdefault("installation", {})
            if not isinstance(installation, dict):
                result = {}
                pois_done.append(result)
                return
            installation["id"] = owner_id
            installation["account"] = {"id": owner_id, "login": f"org{owner_id}",
                                       "type": "Organization"}
            result = _direct_install_activation(
                copied, FakeGitHub({}, owner_id=owner_id),
                f"server-worker-activation-{adversarial_delivery_seq}", et)
        elif (et, action) == ("installation", "deleted"):
            S.make_db_processor(BR_DSN)(et, json.loads(json.dumps(pl)), None, g)
            result = {}
        else:
            result = S.handle_event(et, pl, d, g)
        pois_done.append(result)

    pois_q = S.EventQueue(db=db, gh=FakeGitHub({}),
                          process=_process_poison,
                          account_of=S._event_account_key, repo_of=S._event_repo,
                          branch_from_ref=S._branch_from_ref, maxsize=64).start()
    poison_batch = [(et, pl) for (_lbl, et, pl) in adversarial] + [
        ("pull_request", {}), ("push", {}), ("installation", {"action": "created"}),
        ("check_run", {"action": "completed", "check_run": "nope"}),
    ]
    for et, pl in poison_batch:
        assert pois_q.submit(et, pl), "poison submit rejected (queue too small for the batch)"
    pois_q.wait_idle(5.0)
    pois_snap = S.health_snapshot(pois_q)
    checks.append((f"never-crash worker: a batch of {len(poison_batch)} adversarial events is fully processed "
                   f"as no-ops — worker ALIVE, failed=0 (alive={pois_snap['worker_alive']}, "
                   f"processed={pois_snap['processed']}, failed={pois_snap['failed']})",
                   pois_snap["worker_alive"] is True and pois_snap["failed"] == 0
                   and pois_snap["processed"] == len(poison_batch)
                   and all(isinstance(r, dict) for r in pois_done)))

    # (5b) THROUGH THE *REAL* LIVE PROCESSOR (make_db_processor), not just handle_event. The batch above wires the
    #      worker's `process` to a handle_event lambda, so it proves handle_event coerces — but it SKIPS
    #      make_db_processor.process, whose FIRST acts (_event_repo / _event_account_key, for the per-repo advisory
    #      lock + tenant routing) do payload.get(...) BEFORE handle_event is ever reached. A non-dict top-level body
    #      (JSON null / [] / "x" / 42 / true) used to raise AttributeError THERE → counted a worker 'failed' (→
    #      needless GitHub redelivery churn, per-repo lock momentarily taken) even though handle_event itself was
    #      safe. So drive the actual production processor over those non-dict bodies and assert it no-ops: a direct
    #      call must not raise, and end-to-end through the worker the 'failed' counter must stay 0.
    nondict_dsn = f"postgresql://veripsa_app@localhost/{DB}"
    nondict_payloads = [None, [1, 2, 3], "garbage", 42, True]
    direct_proc = S.make_db_processor(nondict_dsn)
    direct_ok = True
    for pl in nondict_payloads:
        try:
            direct_proc("pull_request", pl, None, FakeGitHub({}))   # the REAL per-event processor, non-dict body
        except Exception as e:
            direct_ok = False
            print(f"make_db_processor raised on non-dict payload {pl!r}: {type(e).__name__}: {str(e)[:120]}", flush=True)
    checks.append(("real processor: make_db_processor.process no-ops on a non-dict top-level payload "
                   "(null/[]/str/int/bool) — no AttributeError before handle_event", direct_ok))

    ndq = S.EventQueue(db=db, gh=FakeGitHub({}),
                       process=S.make_db_processor(nondict_dsn),     # the LIVE processor IS the worker's process
                       account_of=S._event_account_key, repo_of=S._event_repo,
                       branch_from_ref=S._branch_from_ref, maxsize=16).start()
    for pl in nondict_payloads:
        assert ndq.submit("pull_request", pl), "non-dict submit rejected (queue too small)"
    ndq.wait_idle(5.0)
    nd_snap = S.health_snapshot(ndq)
    checks.append((f"real processor end-to-end: a batch of {len(nondict_payloads)} non-dict payloads through the "
                   f"LIVE make_db_processor worker no-ops — worker ALIVE, failed=0 "
                   f"(alive={nd_snap['worker_alive']}, processed={nd_snap['processed']}, failed={nd_snap['failed']})",
                   nd_snap["worker_alive"] is True and nd_snap["failed"] == 0
                   and nd_snap["processed"] == len(nondict_payloads)))

    # MAIN-PROTECTION SCOPE: Veripsa only governs PRs targeting the protected (default) branch. A PR whose base
    # is some OTHER branch (feature/release/stacked) is out of scope — skipped, no check, no comment, no noise.
    bb_gh = FakeGitHub({50: ["backend/api.py"]})
    bb_payload = pr_payload("opened", 50, "dave")
    bb_payload["pull_request"]["base"]["ref"] = "develop"          # default_branch stays "main"
    bb_result = S.handle_event("pull_request", bb_payload, db, bb_gh)
    checks.append(("main-protection scope: a PR targeting a NON-default base is skipped (no check, no comment)",
                   bb_result.get("skipped") is not None and not bb_gh.checks and not bb_gh.comments))

    # FORK PR: the head sha lives in the contributor's fork, so creating a check run on the BASE repo at that
    # sha can be rejected by GitHub. That must NOT abort the event — it degrades to comment-only (the PR comment
    # always posts on the base repo's conversation, carrying the full signal).
    class _CheckRejectingGitHub(FakeGitHub):
        def upsert_check(self, repo, sha, conclusion, title, summary):
            raise RuntimeError("No commit found for SHA (fork head not in base repo)")
    posted = S._safe_upsert_check(_CheckRejectingGitHub({}), REPO, "deadbeef", "neutral", "t", "s", is_fork=True)
    checks.append(("_safe_upsert_check swallows a rejected check post (returns False, never raises)", posted is False))

    FORK_REPO = "acme/fork-test"
    def _fork_pr(number, author, head_repo_id, head_sha):
        return {"action": "opened", "number": number, "installation": {"id": 4242},
                "repository": {"full_name": FORK_REPO, "default_branch": "main",
                               "id": _fixture_repo_id(FORK_REPO)},
                "pull_request": {"base": {"ref": "main", "sha": SHA, "repo": {"id": 1}},
                                 "head": {"sha": head_sha, "repo": {"id": head_repo_id}},
                                 "user": {"login": author}, "merged": False}}
    # a same-repo PR holds shared/x.py, then a FORK PR (head repo id 2 ≠ base 1) collides on the SAME path →
    # serialize → a comment IS produced; the fork's check post is rejected, but the event still completes.
    S.handle_event("pull_request", _fork_pr(61, "maintainer", 1, f"{61:040x}"), db, FakeGitHub({61: ["shared/x.py"]}))
    fork_gh = _CheckRejectingGitHub({62: ["shared/x.py"]})
    fork_result = S.handle_event("pull_request", _fork_pr(62, "ext", 2, f"{62:040x}"), db, fork_gh)
    checks.append(("fork PR: a rejected check post does NOT crash the event; the comment still carries the signal",
                   isinstance(fork_result, dict) and not fork_gh.checks and len(fork_gh.comments) >= 1))

    # COST/SCALE GUARD — the live process never evaluates the repository file cap because it never downloads or
    # extracts repository content. Even with an artificially tiny cap, ingress only records + queues the turn.
    old_files_cap = ingest._MAX_INGEST_FILES        # the cap moved to ingest.py with _full_ingest/_count_files
    ingest._MAX_INGEST_FILES = 1
    try:
        big = S.handle_event("push", {"ref": "refs/heads/main", "after": "d" * 40,
                                      "repository": {"full_name": "acme/bigrepo", "default_branch": "main",
                                                     "id": _fixture_repo_id("acme/bigrepo")}},
                             db, FakeGitHub({}))
    finally:
        ingest._MAX_INGEST_FILES = old_files_cap
    checks.append(("big-repo live ingress is constant-work: file cap/extractor stay outside webhook, exact turn queued",
                   big.get("mode") == "queued"
                   and big.get("graph_refresh", {}).get("queued") is True
                   and "over_cap" not in big and "files" not in big and "edges" not in big))

    # COST/SCALE GUARD — BACKFILL PR CAP: install-time backfill processes at most CAP open PRs (no API storm on a
    # busy repo); the rest are flagged 'truncated' and picked up later by their own webhooks (not lost).
    many = [{"number": 200 + i, "base": {"ref": "main"}, "head": {"sha": f"mp{i}"}, "user": {"login": "u"}} for i in range(5)]
    cap_pr_gh = FakeGitHub({200 + i: ["backend/api.py"] for i in range(5)}, open_prs=many)
    old_pr_cap = ingest._BACKFILL_PR_CAP            # the cap moved to ingest.py with backfill_open_prs
    ingest._BACKFILL_PR_CAP = 2
    try:
        bf = S.backfill_open_prs(db, cap_pr_gh, "acme/manyprs")
    finally:
        ingest._BACKFILL_PR_CAP = old_pr_cap
    checks.append(("big-repo backfill cap: only CAP open PRs processed on install (truncated flagged); rest deferred to their own webhooks",
                   bf["count"] == 2 and bf.get("truncated") is True))

    # REPO RENAME: a repo renamed on GitHub keeps the same owner but a new full_name. The coordinate keys the
    # whole content-free working set (graph + claims) by full_name, so a rename must RE-POINT it (else the repo
    # reads 'unknown' until its next push). The append-only event ledger keeps the historical name (correct).
    RN_OLD, RN_NEW = "acme/oldname", "acme/newname"
    RN_ID = _fixture_repo_id(RN_OLD)
    rn_gh = FakeGitHub({400: ["backend/api.py"]})
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
       (json.dumps(graph), RN_OLD, "main", "e" * 40))                                      # graph under OLD
    S.handle_event("pull_request",
                   {"action": "opened", "number": 400, "installation": {"id": 4242},
                    "repository": {"full_name": RN_OLD, "default_branch": "main",
                                   "id": RN_ID},
                    "pull_request": {"base": {"ref": "main", "sha": SHA},
                                     "head": {"sha": f"{400:040x}"},
                                     "user": {"login": "amy"}, "merged": False}}, db, rn_gh)            # claim under OLD
    old_paths_before = db("SELECT array_length(core.coordinate_file_paths(%s,%s),1)", (RN_OLD, "main"))
    rn = S.handle_event("repository",
                        {"action": "renamed", "repository": {"id": RN_ID, "full_name": RN_NEW,
                                                             "owner": {"login": "acme"}},
                         "changes": {"repository": {"name": {"from": "oldname"}}}}, db, rn_gh)
    new_paths = db("SELECT array_length(core.coordinate_file_paths(%s,%s),1)", (RN_NEW, "main"))
    old_paths_after = db("SELECT coalesce(array_length(core.coordinate_file_paths(%s,%s),1),0)", (RN_OLD, "main"))
    new_claims = admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int FROM core.claim WHERE repo=%s AND claim_state IN ('active','waiting')""", (RN_NEW,))
    checks.append(("repo rename: the graph re-points to the new full_name (old coordinate emptied, new populated)",
                   (old_paths_before or 0) > 0 and (new_paths or 0) > 0 and (old_paths_after or 0) == 0
                   and isinstance(rn, dict) and rn.get("new") == RN_NEW))
    checks.append(("repo rename: the in-flight claim moves with the repo (now under the new coordinate)",
                   new_claims is not None and new_claims >= 1))

    # REPO TRANSFER: a transfer changes the OWNER (and thus full_name) while the short name stays. The coordinate
    # keys the whole content-free working set by full_name, so a transfer must RE-POINT it old→new exactly like a
    # rename (only the source of the old name differs: the old OWNER comes from changes.owner.from). GitHub's
    # transferred payload gives the new full_name + the unchanged short name + the old owner login.
    XF_OLD, XF_NEW = "oldorg/svc", "neworg/svc"
    xf_gh = FakeGitHub({420: ["svc/handler.py"]})
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
       (json.dumps(graph), XF_OLD, "main", "a" * 40))                                      # graph under OLD owner
    S.handle_event("pull_request",
                   {"action": "opened", "number": 420, "installation": {"id": 4242},
                    "repository": {"full_name": XF_OLD, "default_branch": "main",
                                   "id": _fixture_repo_id(XF_OLD)},
                    "pull_request": {"base": {"ref": "main", "sha": "a" * 40},
                                     "head": {"sha": f"{420:040x}"},
                                     "user": {"login": "ned"}, "merged": False}}, db, xf_gh)             # claim under OLD owner
    xf_old_before = db("SELECT array_length(core.coordinate_file_paths(%s,%s),1)", (XF_OLD, "main"))
    xf = S.handle_event("repository",
                        {"action": "transferred",
                         "repository": {"full_name": XF_NEW, "name": "svc", "owner": {"login": "neworg"}},
                         "changes": {"owner": {"from": {"user": {"login": "oldorg"}}}}}, db, xf_gh)
    xf_new_paths = db("SELECT array_length(core.coordinate_file_paths(%s,%s),1)", (XF_NEW, "main"))
    xf_old_after = db("SELECT coalesce(array_length(core.coordinate_file_paths(%s,%s),1),0)", (XF_OLD, "main"))
    xf_new_claims = admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int FROM core.claim WHERE repo=%s AND claim_state IN ('active','waiting')""", (XF_NEW,))
    checks.append(("repo transfer: the graph + in-flight claim re-key to the new owner's full_name (old coordinate emptied)",
                   (xf_old_before or 0) > 0 and (xf_new_paths or 0) > 0 and (xf_old_after or 0) == 0
                   and isinstance(xf, dict) and xf.get("new") == XF_NEW and xf.get("old") == XF_OLD
                   and (xf_new_claims or 0) >= 1))

    # REPO TRANSFER — CROSS-ACCOUNT (the P1 the audit reproduced live; the test ABOVE misses it). The transfer test
    # above fires with NO owner.id on either side, so _event_account_key collapses BOTH owners to ACCT-DEMO and the
    # same-account re-key trivially "works" — masking the bug. A real org/user move
    # puts the graph under the OLD owner's tenant (ACCT-GH-<old>) while the webhook runs SESSION-PINNED to the NEW
    # owner (ACCT-GH-<new>); the old same-owner re-point UPDATE … WHERE account_id=SESSION=new matched ZERO old rows
    # → a SILENT-FALSE ok:true and the old tenant's content-free graph STRANDED. This gate uses TWO DISTINCT tenants
    # and asserts (a) the OLD tenant has ZERO stranded rows afterward, (b) the result HONESTLY signals re-ingest (no
    # false ok:true-with-zero-work; the old coordinate was purged), and (c) a same-owner rename still works unchanged.
    XA_OLD_ID, XA_NEW_ID, XA_REPO_ID = 910001, 910002, 910010
    XA_OLD_ACCT, XA_NEW_ACCT = f"ACCT-GH-{XA_OLD_ID}", f"ACCT-GH-{XA_NEW_ID}"
    XA_NAME = "svc"
    XA_OLD_FULL, XA_NEW_FULL = f"org{XA_OLD_ID}/{XA_NAME}", f"org{XA_NEW_ID}/{XA_NAME}"
    xa_live = S.make_db_processor(BR_DSN)
    _activate_empty_installation(XA_OLD_ID, 9001, "server-route-transfer-old")
    _activate_empty_installation(XA_NEW_ID, 9002, "server-route-transfer-new")
    # ingest a graph + an in-flight claim UNDER THE OLD OWNER'S TENANT (route via the live pin, exactly the boot-
    # reconcile seed pattern above: resolve the already-activated route, then ingest into ACCT-GH-<old>).
    _xa_seed = psycopg2.connect(BR_DSN)
    try:
        _xa_seed.autocommit = True
        with _xa_seed.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_existing_installation_with_authority(%s)", (str(XA_OLD_ID),))
            cur.execute("SELECT core.ingest_graph_with_authority(%s::jsonb,%s,%s,%s)",
                        (json.dumps(graph), XA_OLD_FULL, "main", "c" * 40))
            cur.execute("SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
                        (XA_OLD_FULL, str(XA_REPO_ID)))
    finally:
        _xa_seed.close()
    # an in-flight claim under the OLD owner too (a PR opened before the transfer) — routed by repository.owner.id=old.
    xa_live("pull_request",
            {"action": "opened", "number": 920, "installation": {"id": 9001},
             "repository": {"full_name": XA_OLD_FULL, "default_branch": "main",
                            "id": XA_REPO_ID,
                            "owner": {"id": XA_OLD_ID, "login": f"org{XA_OLD_ID}"}},
             "pull_request": {"base": {"ref": "main", "sha": "c" * 40},
                              "head": {"sha": f"{XA_OLD_ID:040x}"},
                              "user": {"login": "olen"}, "merged": False}}, None,
            FakeGitHub({920: ["backend/auth.py"]}, owner_id=XA_OLD_ID))

    def _rows_in(account, repo):
        # graph/activation rows are stable-id attributable and transferable; claims intentionally remain because
        # they have no repository id/generation and a delayed ID1 transfer must never release ID2 work by name.
        return _mig("""SELECT set_config('core.current_account',%s,true);
            SELECT ( (SELECT count(*) FROM core.code_node     WHERE account_id=%s AND repo=%s)
                   + (SELECT count(*) FROM core.code_edge     WHERE account_id=%s AND repo=%s)
                   + (SELECT count(*) FROM core.graph_version WHERE account_id=%s AND repo=%s)
                   + (SELECT count(*) FROM core.repository_lifecycle_activation
                       WHERE account_id=%s AND repo=%s) )::int""",
                    (account, account, repo, account, repo, account, repo, account, repo))
    xa_old_before = _rows_in(XA_OLD_ACCT, XA_OLD_FULL)
    # fire the CROSS-ACCOUNT transfer through the LIVE processor: repository.owner.id = NEW (session pins ACCT-GH-new),
    # changes.owner.from.organization.id = OLD (the stranded source tenant). This is the exact org-move payload shape.
    xa_key = "D-XA-LIVE-TRANSFER"
    xa_payload = {"action": "transferred",
                  "installation": {"id": 9002, "account": {
                      "id": XA_NEW_ID, "login": f"org{XA_NEW_ID}", "type": "Organization"}},
                  "repository": {"id": XA_REPO_ID, "full_name": XA_NEW_FULL, "name": XA_NAME,
                                 "owner": {"id": XA_NEW_ID, "login": f"org{XA_NEW_ID}"}},
                  "changes": {"owner": {"from": {"organization": {
                      "login": f"org{XA_OLD_ID}", "id": XA_OLD_ID}}}},
                  "_veripsa_delivery_key": xa_key}
    _mig("INSERT INTO core.webhook_delivery("
         "delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at,lease_generation) "
         "VALUES (%s,'repository',%s,%s,%s::jsonb,'processing',1,clock_timestamp()-interval '2 hours',"
         "clock_timestamp(),1); SELECT 1",
         (xa_key, str(XA_NEW_ID), XA_NEW_FULL, json.dumps(xa_payload)))

    class XATransferGitHub(FakeGitHub):
        def repo_current_identity(self, repo):
            return {"id": XA_REPO_ID, "owner_id": XA_NEW_ID, "full_name": XA_NEW_FULL}

    xa = xa_live("repository", xa_payload, None, XATransferGitHub({}, owner_id=XA_NEW_ID))
    xa_old_after = _rows_in(XA_OLD_ACCT, XA_OLD_FULL)
    xa_t = _json_scalar(_mig(
        "SELECT payload->'_veripsa_transfer_completed' FROM core.webhook_delivery WHERE delivery_key=%s",
        (xa_key,)))
    xa_new_activation = _mig(
        "SELECT set_config('core.current_account',%s,true); "
        "SELECT count(*)::int FROM core.repository_lifecycle_activation "
        "WHERE account_id=%s AND repository_id=%s AND repo=%s AND lifecycle_authoritative",
        (XA_NEW_ACCT, XA_NEW_ACCT, str(XA_REPO_ID), XA_NEW_FULL))
    checks.append((f"cross-account transfer PRECONDITION: the OLD owner's tenant {XA_OLD_ACCT} holds exact-ID graph state "
                   f"before transfer (rows={xa_old_before})",
                   (xa_old_before or 0) > 0))
    # (a) ZERO stable-ID-attributable graph rows under the FORMER owner. Unversioned claims/authority are preserved
    # fail-closed until explicit offboarding, because transfer cannot distinguish delayed ID1 from replacement ID2.
    checks.append((f"cross-account transfer: the OLD owner's exact-ID graph state is purged "
                   f"(rows {xa_old_before}->{xa_old_after})",
                   xa_old_after == 0))
    # (b) Durable completion and new-account activation commit atomically.
    checks.append((f"cross-account transfer: durable outcome is honest and NEW coordinate is activated "
                   f"atomically (marker={xa_t}, activation={xa_new_activation})",
                   xa_t.get("old_account") == XA_OLD_ACCT and xa_t.get("repository_id") == str(XA_REPO_ID)
                   and xa_t.get("outcome") == "purged" and xa_new_activation == 1))
    # (c) the SAME-OWNER re-point is UNCHANGED — proven two ways under a fresh DISTINCT tenant (so it is the
    #     behaviour-preserving in-place re-key, never a cross-account purge):
    #   (c1) a same-owner repository.RENAMED (the canonical same-owner/new-name path) still re-points in place; and
    #   (c2) a same-owner repository.TRANSFERRED (old owner id == new owner id) takes the SAME in-place
    #        rename_repo_coordinate branch (result carries 'repointed', NOT 'cross_account') — never the purge.
    XS_ID = 910003
    XS_ACCT = f"ACCT-GH-{XS_ID}"
    XS_OLD_FULL, XS_NEW_FULL = f"org{XS_ID}/oldname", f"org{XS_ID}/newname"
    _xs_seed = psycopg2.connect(BR_DSN)
    try:
        _xs_seed.autocommit = True
        with _xs_seed.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(XS_ID),))
            cur.execute("SELECT core.ingest_graph_with_authority(%s::jsonb,%s,%s,%s)",
                        (json.dumps(graph), XS_OLD_FULL, "main", "d" * 40))
    finally:
        _xs_seed.close()
    xs_old_before = _rows_in(XS_ACCT, XS_OLD_FULL)
    # (c1) a same-owner RENAME (owner.id stays XS_ID; changes.repository.name.from gives the old short name).
    xa_live("repository",
            {"action": "renamed",
             "repository": {"full_name": XS_NEW_FULL, "owner": {"id": XS_ID, "login": f"org{XS_ID}"}},
             "changes": {"repository": {"name": {"from": "oldname"}}}}, None, FakeGitHub({}))
    xs_new_after = _rows_in(XS_ACCT, XS_NEW_FULL)         # the graph re-pointed to the NEW name, SAME account
    xs_old_after = _rows_in(XS_ACCT, XS_OLD_FULL)         # the old name emptied
    checks.append((f"same-owner rename is UNCHANGED: re-points in place within the one account {XS_ACCT} "
                   f"(old {xs_old_before}->{xs_old_after}, new ->{xs_new_after}) — not a cross-account purge",
                   (xs_old_before or 0) > 0 and xs_old_after == 0 and (xs_new_after or 0) > 0))
    # (c2) a same-owner TRANSFER (old id == new id, same short name 'newname') takes the in-place rename branch:
    #      the result reports 'repointed' (the rename_repo_coordinate path) and is NOT flagged cross_account.
    xs_same = S.handle_event("repository",
                 {"action": "transferred",
                  "repository": {"full_name": XS_NEW_FULL, "name": "newname",
                                 "owner": {"id": XS_ID, "login": f"org{XS_ID}"}},
                  "changes": {"owner": {"from": {"organization": {"login": f"org{XS_ID}", "id": XS_ID}}}}},
                 db, FakeGitHub({}))
    checks.append(("same-owner transfer takes the UNCHANGED in-place re-point branch (result 'repointed', not "
                   f"'cross_account') — old==new owner id is never a cross-account purge (result keys={sorted(xs_same)})",
                   isinstance(xs_same, dict) and "repointed" in xs_same and not xs_same.get("cross_account")))

    # REPO ARCHIVE: an archived repo accepts no further pushes and merges nothing — its in-flight lanes (every
    # active/waiting claim across all branches) must be RELEASED (else they read as falsely in-flight until each
    # lease expires and strand any waiter behind them) while the code graph + the append-only event ledger are
    # KEPT (archived structure is still real history; only deletion's purge forgets the graph). Set up a repo
    # with a graph + an in-flight claim, fire `repository` archived, and assert: claims released, graph retained.
    AR_REPO = "acme/archive-test"
    AR_REPO_ID = _fixture_repo_id(AR_REPO)
    ar_gh = FakeGitHub({430: ["lib/core.py"]})
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
       (json.dumps(graph), AR_REPO, "main", SHA))                                            # graph
    S.handle_event("pull_request",
                   {"action": "opened", "number": 430, "installation": {"id": 4242},
                    "repository": {"full_name": AR_REPO, "default_branch": "main",
                                   "id": AR_REPO_ID},
                    "pull_request": {"base": {"ref": "main", "sha": SHA},
                                     "head": {"sha": f"{430:040x}"},
                                     "user": {"login": "olive"}, "merged": False}}, db, ar_gh)            # in-flight claim
    def _ar_live():
        return admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
            SELECT count(*)::int FROM core.claim WHERE repo=%s AND claim_state IN ('active','waiting')""", (AR_REPO,))
    def _ar_graph():
        return db("SELECT coalesce(array_length(core.coordinate_file_paths(%s,%s),1),0)", (AR_REPO, "main"))
    ar_live_before, ar_graph_before = _ar_live(), _ar_graph()
    ar = S.handle_event("repository",
                        {"action": "archived", "repository": {"id": AR_REPO_ID, "full_name": AR_REPO,
                                                               "default_branch": "main"}},
                        db, ar_gh)
    ar_live_after, ar_graph_after = _ar_live(), _ar_graph()
    checks.append((f"repo archive: precondition — an in-flight claim + a graph exist before archive "
                   f"(claims={ar_live_before}, graph_paths={ar_graph_before})",
                   (ar_live_before or 0) >= 1 and (ar_graph_before or 0) > 0))
    checks.append((f"repo archive: the in-flight lanes are RELEASED (archived repos land nothing) "
                   f"(claims {ar_live_before}->{ar_live_after})",
                   ar_live_after == 0 and isinstance(ar, dict)
                   and isinstance(ar.get("released"), dict) and ar["released"].get("ok") is True
                   and (ar["released"].get("released") or 0) >= 1))
    checks.append((f"repo archive: the code graph is RETAINED (archive ≠ delete; structure is still real history) "
                   f"(graph_paths {ar_graph_before}->{ar_graph_after})",
                   ar_graph_after == ar_graph_before and ar_graph_after > 0))

    # WEBHOOK ORDER-INDEPENDENCE: GitHub does not guarantee delivery order. A stale 'synchronize' redelivered
    # AFTER a merge must NOT resurrect the merged PR's claims (else it shows as falsely in-flight until lease
    # expiry). change_concluded() makes opened/synchronize idempotent w.r.t. a closed PR.
    ORD_REPO = "acme/order-test"
    ord_gh = FakeGitHub({500: ["svc/x.py"]})
    def _ord_pr(action, merged=False):
        return {"action": action, "number": 500, "installation": {"id": 4242},
                "repository": {"full_name": ORD_REPO, "default_branch": "main",
                               "id": _fixture_repo_id(ORD_REPO)},
                "pull_request": {"base": {"ref": "main", "sha": SHA},
                                 "head": {"sha": f"{500:040x}"},
                                 "user": {"login": "ivy"}, "merged": merged}}
    def _ord_live():
        return admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
            SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id='PR-500' AND claim_state IN ('active','waiting')""", (ORD_REPO,))
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
       (json.dumps(graph), ORD_REPO, "main", SHA))
    S.handle_event("pull_request", _ord_pr("opened"), db, ord_gh)
    live_open = _ord_live()
    S.handle_event("pull_request", _ord_pr("closed", merged=True), db, ord_gh)   # merge → claims released
    live_merged = _ord_live()
    stale = S.handle_event("pull_request", _ord_pr("synchronize"), db, ord_gh)   # stale redelivery AFTER merge
    live_stale = _ord_live()
    checks.append(("webhook order-independence: a stale synchronize after merge is skipped — claims NOT resurrected "
                   f"(open={live_open}, merged={live_merged}, after-stale={live_stale})",
                   (live_open or 0) >= 1 and live_merged == 0 and live_stale == 0 and stale.get("skipped") is not None))

    # WEBHOOK ORDER-INDEPENDENCE (merged-PR resurrection): `reopened` / `ready_for_review` are EXEMPT from the
    # change_concluded guard (a genuine reopen of a NON-merged PR must re-activate its lanes). But GitHub never
    # lets you reopen — or mark ready — a PR that already MERGED, so a `reopened`/`ready_for_review` whose payload
    # says merged=true is provably a STALE, REORDERED redelivery (at-least-once + no ordering) of a pre-merge
    # action arriving AFTER the merge. It must NOT resurrect the merged PR's claims (else the merged PR shows as
    # falsely in-flight, blocking every future PR on those files behind a ghost until the lease expires). Drive
    # opened→merge→stale-reopened(merged)→stale-ready_for_review(merged) and assert: after each stale event the
    # merged PR's lanes stay at 0 (NOT resurrected) and the event reports a skip.
    ORD2_REPO = "acme/order-merged-test"
    ord2_gh = FakeGitHub({510: ["svc/y.py"]})
    def _ord2_pr(action, merged=False):
        return {"action": action, "number": 510, "installation": {"id": 4242},
                "repository": {"full_name": ORD2_REPO, "default_branch": "main",
                               "id": _fixture_repo_id(ORD2_REPO)},
                "pull_request": {"base": {"ref": "main", "sha": SHA},
                                 "head": {"sha": f"{510:040x}", "ref": "feat/510"},
                                 "user": {"login": "judy"}, "merged": merged}}
    def _ord2_live():
        return admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
            SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id='PR-510' AND claim_state IN ('active','waiting')""", (ORD2_REPO,))
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
       (json.dumps(graph), ORD2_REPO, "main", SHA))
    S.handle_event("pull_request", _ord2_pr("opened"), db, ord2_gh)
    ord2_open = _ord2_live()
    S.handle_event("pull_request", _ord2_pr("closed", merged=True), db, ord2_gh)   # merge → claims released
    ord2_merged = _ord2_live()
    stale_reopen = S.handle_event("pull_request", _ord2_pr("reopened", merged=True), db, ord2_gh)   # stale, post-merge
    ord2_after_reopen = _ord2_live()
    stale_ready = S.handle_event("pull_request", _ord2_pr("ready_for_review", merged=True), db, ord2_gh)
    ord2_after_ready = _ord2_live()
    checks.append(("webhook order-independence: a stale reopened/ready_for_review carrying merged=true after the "
                   "merge is skipped — the merged PR's claims are NOT resurrected "
                   f"(open={ord2_open}, merged={ord2_merged}, after-reopen={ord2_after_reopen}, after-ready={ord2_after_ready})",
                   (ord2_open or 0) >= 1 and ord2_merged == 0
                   and ord2_after_reopen == 0 and stale_reopen.get("skipped") is not None
                   and ord2_after_ready == 0 and stale_ready.get("skipped") is not None))

    # DRAFT PRs ARE the SCOUT WINDOW (PO 2026-06-25 round-2): full analysis runs — claims are declared, a check
    # is posted, the PR participates in the contention picture (cross-state softening of serialize→warn lives in
    # the verdict CTE, not in the eligibility guard). When the author marks it ready, GitHub fires
    # `ready_for_review`, which we ALSO analyze — but it is now NOT the FIRST analysis the PR ever received.
    DRAFT_REPO = "acme/draft-test"
    draft_gh = FakeGitHub({600: ["svc/d.py"]})
    S.handle_event("push", {"ref": "refs/heads/main", "after": f"{600:040x}",
                            "repository": {"full_name": DRAFT_REPO, "default_branch": "main",
                                           "id": _fixture_repo_id(DRAFT_REPO)}}, db, draft_gh)
    def _draft_pr(action, draft):
        return {"action": action, "number": 600, "installation": {"id": 4242},
                "repository": {"full_name": DRAFT_REPO, "default_branch": "main",
                               "id": _fixture_repo_id(DRAFT_REPO)},
                "pull_request": {"base": {"ref": "main", "sha": SHA},
                                 "head": {"sha": f"{600:040x}"},
                                 "user": {"login": "dan"}, "draft": draft, "merged": False}}
    _draft_checks_before = len(draft_gh.checks)             # push posts a cold-start watching check at the head sha
    _draft_check_patches_before = len(draft_gh.check_patches)
    draft_res = S.handle_event("pull_request", _draft_pr("opened", True), db, draft_gh)
    draft_claims = admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id='PR-600' AND claim_state IN ('active','waiting')""", (DRAFT_REPO,))
    # The PR's head_sha == the push's sha (both 6*40), so upsert_check PATCHES the existing watching check in place
    # (no churn = no new row, just an updated conclusion/title). Verify the draft path FULLY analyzed by checking
    # the result carries a 'check' payload AND a claim was declared AND the check was upserted (post OR patch).
    _check_event = (len(draft_gh.checks) > _draft_checks_before
                    or len(draft_gh.check_patches) > _draft_check_patches_before)
    checks.append(("draft PR: a draft is fully analyzed (scout window) — claims declared + a check posted/patched",
                   draft_res.get("skipped") is None and "check" in (draft_res or {})
                   and (draft_claims or 0) >= 1 and _check_event))
    ready_res = S.handle_event("pull_request", _draft_pr("ready_for_review", False), db, draft_gh)
    ready_claims = admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id='PR-600' AND claim_state IN ('active','waiting')""", (DRAFT_REPO,))
    checks.append(("draft PR: ready_for_review re-analyzes it (claim still present + check posted)",
                   "check" in ready_res and (ready_claims or 0) >= 1 and len(draft_gh.checks) >= 1))

    # ── PR LIFECYCLE — CLAIM-RELEASE COMPLETENESS. A claim that is never released = a lane blocked FOREVER (every
    #    future PR touching that file is told to "wait in line" behind a ghost). Each of the lifecycle paths below
    #    must either RELEASE the PR's lanes (close/merge/retarget-off-main) or leave them correctly held (a still-
    #    open draft) — and a closed PR's lanes must be FREE for the next PR. These DOCUMENT the lifecycle's
    #    release-or-no-leak contract end-to-end through handle_event (no live GitHub; the gate's claim table read
    #    back via the migrator past RLS, the same path the merge/withdraw tests above use).
    LC_REPO = "acme/lifecycle"
    lc_gh = FakeGitHub({})
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
       (json.dumps(graph), LC_REPO, "main", "a" * 40))
    def _lc_pr(action, number, author, base="main", draft=False, merged=False):
        return {"action": action, "number": number, "installation": {"id": 4242},
                "repository": {"full_name": LC_REPO, "default_branch": "main",
                               "id": _fixture_repo_id(LC_REPO)},
                "pull_request": {"base": {"ref": base, "sha": "a" * 40},
                                 "head": {"sha": f"{number:040x}", "ref": f"feat/{number}"},
                                 "user": {"login": author}, "draft": draft, "merged": merged}}
    def _lc_live(change_id, branch="main"):
        # LIVE lane states only (active/waiting); a released/expired claim no longer holds the lane.
        return admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
            SELECT count(*)::int FROM core.claim WHERE repo=%s AND branch=%s AND change_id=%s
              AND claim_state IN ('active','waiting')""", (LC_REPO, branch, change_id))

    # (1) BASE-RETARGET LEAK (an `edited` whose base moved OFF the protected branch): PR-1000 opens on main +
    #     reserves backend/api.py; PR-1001 queues behind it on the same lane. Then PR-1000 is RETARGETED to a
    #     release branch (out of Veripsa's scope). Before the fix, handle_event bailed at the protected-branch
    #     filter and PR-1000's main claim stayed active FOREVER — PR-1001 (and every later PR on api.py) stranded
    #     in line behind a PR that no longer heads to main. The fix RELEASES the retargeted PR's protected-branch
    #     lanes (and promotes the waiter) before bailing, exactly like a withdraw.
    lc_files = {1000: ["backend/api.py"], 1001: ["backend/api.py"]}
    lc_gh.files_by_pr.update(lc_files)
    S.handle_event("pull_request", _lc_pr("opened", 1000, "alice"), db, lc_gh)
    S.handle_event("pull_request", _lc_pr("opened", 1001, "zoe"), db, lc_gh)
    retarget_open_1000 = _lc_live("PR-1000")
    retarget_open_1001 = _lc_live("PR-1001")
    retarget_res = S.handle_event("pull_request", _lc_pr("edited", 1000, "alice", base="release-1.0"), db, lc_gh)
    retarget_leak = _lc_live("PR-1000")            # MUST be 0 — the off-main PR holds no protected-branch lane
    promoted_1001 = admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int FROM core.claim WHERE repo=%s AND branch='main' AND change_id='PR-1001'
          AND claim_state='active'""", (LC_REPO,))
    checks.append(("PR lifecycle / base-retarget: a PR edited OFF the protected branch RELEASES its main lanes "
                   f"(no ghost) + promotes the waiter (open_1000={retarget_open_1000}, after_retarget={retarget_leak}, "
                   f"pr1001_active={promoted_1001}, released={ (retarget_res.get('released_off_protected') or {}).get('released') })",
                   retarget_open_1000 == 1 and retarget_open_1001 == 1
                   and retarget_leak == 0 and promoted_1001 == 1
                   and bool((retarget_res.get("released_off_protected") or {}).get("released"))
                   and retarget_res.get("skipped") is not None))
    # (1b) a PR genuinely OPENED on a non-protected base never had main lanes → the retarget-release is a clean
    #      no-op (it frees nothing), so the fix introduces no false side effect on truly-out-of-scope PRs.
    lc_gh.files_by_pr[1002] = ["backend/db.py"]
    open_off_res = S.handle_event("pull_request", _lc_pr("opened", 1002, "bob", base="release-1.0"), db, lc_gh)
    checks.append(("PR lifecycle / base-retarget: a PR opened ON a non-protected base frees nothing (clean no-op, "
                   f"no false release) — main lane stays empty ({_lc_live('PR-1002')})",
                   _lc_live("PR-1002") == 0
                   and (open_off_res.get("released_off_protected") or {}).get("released") == []))

    # (2) REOPENED (#39 REGRESSION, server level): a concluded PR's claims persist as 'released' (PK keeps the
    #     row). `reopened` re-declares the SAME claim_id → before the gate fix, _place_claim fell through to a
    #     free-lane INSERT → claim_pkey unique_violation → the handler's own retry INSERT collided AGAIN, uncaught
    #     → the worker counted a FAILED delivery and posted nothing. Drive the FULL open→close→reopen lifecycle
    #     through handle_event and assert: no crash, and the lane is RE-RESERVED (live again) — the PR is back
    #     in flight, not a falsely-concluded ghost.
    lc_gh.files_by_pr[1010] = ["backend/api.py"]
    S.handle_event("pull_request", _lc_pr("opened", 1010, "carol"), db, lc_gh)
    reopen_open = _lc_live("PR-1010")
    S.handle_event("pull_request", _lc_pr("closed", 1010, "carol", merged=False), db, lc_gh)
    reopen_closed = _lc_live("PR-1010")            # released by the withdraw path
    reopen_crashed = None
    try:
        reopen_res = S.handle_event("pull_request", _lc_pr("reopened", 1010, "carol"), db, lc_gh)
    except Exception as e:
        reopen_crashed, reopen_res = str(e)[:160], {}
    reopen_live = _lc_live("PR-1010")              # MUST be back to live — reopen re-reserves the lane
    checks.append(("PR lifecycle / reopened (#39): open→close→reopen does NOT crash + RE-RESERVES the lane "
                   f"(open={reopen_open}, closed={reopen_closed}, reopened={reopen_live}, crash={reopen_crashed})",
                   reopen_crashed is None and reopen_open == 1 and reopen_closed == 0
                   and reopen_live == 1 and "check" in reopen_res))

    # (3) CONVERTED_TO_DRAFT then back to READY: a PR that opens (lanes reserved) and is later converted to a
    #     draft is STILL AN OPEN PR intending to land on main — it correctly KEEPS its lane (a draft is paused,
    #     not abandoned; the close/merge paths free it). The convert event re-analyzes and refreshes the check,
    #     but does NOT double-claim. Marking it ready_for_review again must NOT create a SECOND claim on the same
    #     lane (no duplicate) — the lane count stays exactly 1 across open→draft→ready.
    lc_gh.files_by_pr[1020] = ["backend/db.py"]
    S.handle_event("pull_request", _lc_pr("opened", 1020, "dee"), db, lc_gh)
    draft_open = _lc_live("PR-1020")
    conv_res = S.handle_event("pull_request", _lc_pr("converted_to_draft", 1020, "dee", draft=True), db, lc_gh)
    draft_held = _lc_live("PR-1020")               # still held — a still-open draft keeps its lane
    S.handle_event("pull_request", _lc_pr("ready_for_review", 1020, "dee", draft=False), db, lc_gh)
    draft_ready = _lc_live("PR-1020")              # exactly 1 — no double-claim on the round-trip
    checks.append(("PR lifecycle / draft round-trip: open→converted_to_draft KEEPS the lane (open PR, paused not "
                   f"abandoned) + ready_for_review does not double-claim (open={draft_open}, draft={draft_held}, "
                   f"ready={draft_ready})",
                   draft_open == 1 and draft_held == 1 and draft_ready == 1
                   and "check" in conv_res and conv_res.get("skipped") is None))
    # a draft that is CLOSED (abandoned) must still free its lane — the held-while-open lane is not a leak.
    S.handle_event("pull_request", _lc_pr("converted_to_draft", 1020, "dee", draft=True), db, lc_gh)
    S.handle_event("pull_request", _lc_pr("closed", 1020, "dee", merged=False), db, lc_gh)
    checks.append(("PR lifecycle / draft round-trip: closing a (drafted) PR still RELEASES its lane (no leak)",
                   _lc_live("PR-1020") == 0))

    # (4) CLOSED PR's LANE IS FREE FOR THE NEXT PR: after PR-1030 MERGES (records landing + releases), a brand-new
    #     PR-1031 touching the SAME file must NOT be told to "Wait in line" behind the merged PR — its lane is
    #     free, so the new PR is the sole holder (clear, no serialize). This is the end-to-end proof that a
    #     concluded PR leaves no lane held.
    lc_gh.files_by_pr.update({1030: ["backend/auth.py"], 1031: ["backend/auth.py"]})
    S.handle_event("pull_request", _lc_pr("opened", 1030, "eve"), db, lc_gh)
    S.handle_event("pull_request", _lc_pr("closed", 1030, "eve", merged=True), db, lc_gh)
    free_after_merge = _lc_live("PR-1030")         # the merged PR holds nothing
    S.handle_event("pull_request", _lc_pr("opened", 1031, "finn"), db, lc_gh)
    c1031 = next((c for c in lc_gh.comments if c["number"] == 1031), None)
    next_pr_clear = (c1031 is None) or ("Wait in line" not in c1031["body"])
    checks.append(("PR lifecycle / lane reuse: a MERGED PR's lane is FREE — the next PR on the same file is NOT "
                   f"serialized behind the ghost (merged_held={free_after_merge}, next_pr_clear={next_pr_clear})",
                   free_after_merge == 0 and _lc_live("PR-1031") == 1 and next_pr_clear))

    # PER-PR FILE CAP: a mega-PR (sweeping refactor / generated code) reserves at most _MAX_PR_FILES claims so a
    # single event can't create thousands of claims. Shrink the cap to prove a 7-file PR reserves only 3.
    MEGA_REPO = "acme/megapr"
    mega_gh = FakeGitHub({700: [f"src/f{i}.py" for i in range(7)]})
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
       (json.dumps(graph), MEGA_REPO, "main", "a" * 40))
    old_pr_files = S._MAX_PR_FILES
    S._MAX_PR_FILES = 3
    try:
        S.handle_event("pull_request",
                       {"action": "opened", "number": 700, "installation": {"id": 4242},
                        "repository": {"full_name": MEGA_REPO, "default_branch": "main",
                                       "id": _fixture_repo_id(MEGA_REPO)},
                        "pull_request": {"base": {"ref": "main", "sha": "a" * 40},
                                         "head": {"sha": f"{700:040x}"},
                                         "user": {"login": "mae"}, "merged": False}}, db, mega_gh)
    finally:
        S._MAX_PR_FILES = old_pr_files
    mega_claims = admin("""SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id='PR-700'""", (MEGA_REPO,))
    checks.append(("per-PR file cap: a 7-file PR with cap=3 reserves only 3 claims (mega-PR cost guard)",
                   mega_claims == 3))

    # ONBOARDING REPO CAP: a big-org install must not cold-start ALL its repos in one event (serial clones
    # stall the worker). Cap eager cold-starts; the rest defer to their own first push. Shrink cap to prove it.
    onb_repos = [{"full_name": f"acme/org-repo-{i}"} for i in range(4)]
    onb_delivery = "server-cap-reinstall"
    old_onb_cap = ingest._ONBOARD_REPO_CAP          # the cap moved to ingest.py with _onboard_repos
    ingest._ONBOARD_REPO_CAP = 2
    try:
        onb_cap = _direct_install_activation(
            {"action": "created", "installation": {"id": 4242}, "repositories": onb_repos},
            FakeGitHub({}), onb_delivery,
        )
    finally:
        ingest._ONBOARD_REPO_CAP = old_onb_cap
    checks.append(("onboarding repo cap: a big-org install cold-starts only CAP repos eagerly, defers the rest",
                   len(onb_cap.get("onboarded") or []) == 2 and onb_cap.get("deferred_repos") == 2))

    # signature verification: a forged body is rejected; a correctly-signed one passes.
    import hashlib, hmac
    sig = "sha256=" + hmac.new(b"sek", b"hello", hashlib.sha256).hexdigest()
    checks.append(("HMAC signature: correct signature accepted", S.verify_signature("sek", b"hello", sig)))
    checks.append(("HMAC signature: forged/altered body rejected", not S.verify_signature("sek", b"tampered", sig)))

    # DoS GUARD: a forged/oversize Content-Length is rejected (None → 413) BEFORE the body is ever read.
    checks.append(("body cap: a normal length is accepted", S._checked_content_length("1024") == 1024))
    checks.append(("body cap: an oversize length is rejected before reading (None → 413)",
                   S._checked_content_length(str(99 * 1024 ** 3)) is None))
    checks.append(("body cap: a non-numeric / negative length is rejected",
                   S._checked_content_length("not-a-number") is None and S._checked_content_length("-1") is None))
    checks.append(("body cap: a missing length is treated as 0 (empty body)", S._checked_content_length(None) == 0))

    # ── STUCK / RED PR (the GitHub-inbox signal): a check_suite `completed`/`failure` for an open PR's head →
    #    record_pr_failing_with_authority, so stuck_prs_surface tells a human. NOTIFY-ONLY (we post no failing
    #    check of our own). A `success` conclusion is a clean no-op. Read back via the steward (same tenant).
    steward = make_db("veripsa_demo_steward")
    def check_payload(conclusion, pr_number, head_sha, action="completed"):
        return {"action": action,
                "installation": {"id": 4242},
                "repository": {"id": REPO_ID, "full_name": REPO, "default_branch": "main"},
                "check_suite": {"conclusion": conclusion, "head_sha": head_sha,
                                "pull_requests": [{"number": pr_number, "base": {"ref": "main"}}]}}
    # PR-91 opens on main, then its CI suite fails → it must surface as stuck (reason ci_failed).
    fail_gh = FakeGitHub({91: ["backend/auth.py"], 92: ["backend/api.py"]})
    S.handle_event("pull_request", pr_payload("opened", 91, "wanda"), db, fail_gh)
    checks_before_fail = len(fail_gh.checks)               # the PR-open already posted its own check
    fail_res = S.handle_event("check_suite", check_payload("failure", 91, f"{91:040x}"), db, fail_gh)
    stuck_after_fail = steward("""SELECT count(*) FROM (SELECT jsonb_array_elements(core.stuck_prs_surface()->'items') i) x
                                   WHERE i->>'pr'='PR-91' AND i->>'reason'='ci_failed'""")
    checks.append(("stuck-pr handler: a check_suite failure for an open PR records it stuck (PR-91, ci_failed)",
                   fail_res.get("stuck_prs") == ["PR-91"] and stuck_after_fail == 1))
    # NOTIFY-ONLY: handling the failed check posts NO Veripsa check of its own (the check-handler never blocks).
    checks.append(("stuck-pr handler: NOTIFY-ONLY — handling a check failure posts no Veripsa check",
                   len(fail_gh.checks) == checks_before_fail))
    # a SUCCESS conclusion is a clean no-op: PR-92 never recorded as stuck (handler skips non-failures).
    S.handle_event("pull_request", pr_payload("opened", 92, "vision"), db, fail_gh)
    succ_res = S.handle_event("check_suite", check_payload("success", 92, f"{92:040x}"), db, fail_gh)
    stuck_after_succ = steward("""SELECT count(*) FROM (SELECT jsonb_array_elements(core.stuck_prs_surface()->'items') i) x
                                   WHERE i->>'pr'='PR-92'""")
    checks.append(("stuck-pr handler: a passing check is a clean no-op (PR-92 not stuck)",
                   not succ_res.get("stuck_prs") and stuck_after_succ == 0))

    # ── RE-RUN REQUEST (the merge-box surface): the customer presses "Re-run all checks" (check_suite
    #    action=rerequested) or "Re-run" on the Veripsa check row (check_run action=rerequested). GitHub does NOT
    #    re-send a pull_request event for it, so this event is the ONLY signal to refresh Veripsa's verdict. Before
    #    the fix the handler no-op'd anything that wasn't a `completed` failure → the button did nothing and the
    #    box kept a stale verdict. After the fix we replay each associated, in-window PR through the SAME live
    #    pull_request path → the check is re-posted (here: PATCHED, since PR-91 already has one).
    # The rerequest's pull_requests[] is a SPARSE ref (number + base only) — the handler re-fetches the
    # AUTHORITATIVE PR via get_pull_request, so register the real PR objects (same authors the opens recorded:
    # PR-91=wanda, PR-92=vision) so the replay re-declares each claim under its OWN author (idempotent, no crash).
    # The replay must carry the coordinate's actual activated stable id. A
    # made-up but lifecycle-stub-approved id used to be sufficient here; the
    # convergence queue now repeats the DB generation fence itself and
    # correctly rejects that split authority.
    RERUN_REPO_ID = REPO_ID
    fail_gh.pr_objects[91] = {"number": 91,
                              "base": {"ref": "main", "repo": {"id": RERUN_REPO_ID}},
                              "head": {"sha": f"{91:040x}"},
                              "user": {"login": "wanda"}, "draft": False, "merged": False}
    fail_gh.pr_objects[92] = {"number": 92, "base": {"ref": "main"}, "head": {"sha": f"{92:040x}"},
                              "user": {"login": "vision"}, "draft": False, "merged": False}
    def rerun_payload(node_key, pr_number, action="rerequested", base_ref="main", app_slug=None):
        # a SPARSE check ref: number + base only (NO trustworthy author/head — the handler re-fetches the PR).
        node = {"head_sha": f"{pr_number:040x}",
                "pull_requests": [{"number": pr_number, "base": {"ref": base_ref}}]}
        if app_slug:
            node["app"] = {"slug": app_slug, "name": app_slug}
        return {"action": action, "installation": {"id": 4242},
                "repository": {"id": REPO_ID, "full_name": REPO, "default_branch": "main"},
                node_key: node}
    # PR-91 is open (its open posted a check). A check_run rerequested for PR-91 must RE-ANALYZE it (replay the
    # live pull_request path) — proving the button is WIRED, not ignored, and that the re-declare under the real
    # author (wanda) is idempotent (no claim_pkey crash).
    rr_payload = rerun_payload("check_run", 91)
    rr_payload["repository"]["id"] = RERUN_REPO_ID
    replay_lifecycle_ids = []

    def activated_replay_db(sql, args=()):
        # Model a live activation boundary: name-only synthetic work is rejected, and only the exact stable id is
        # allowed. The shared handler must carry that id on both the outer check event and the replayed PR event.
        if "repository_event_allowed_with_authority" in sql:
            observed_id = args[1] if len(args) > 1 else None
            replay_lifecycle_ids.append(observed_id)
            return str(observed_id) == str(RERUN_REPO_ID)
        return db(sql, args)

    rr_res = S.handle_event("check_run", rr_payload, activated_replay_db, fail_gh)
    checks.append(("rerequest handler: a check_run `rerequested` re-runs the associated PR (PR-91 re-analyzed)",
                   rr_res.get("reran") == ["PR-91"]))
    checks.append(("rerequest handler: an activated repository's stable id survives the shared synthetic replay "
                   f"lifecycle guard (observed={replay_lifecycle_ids})",
                   replay_lifecycle_ids == [str(RERUN_REPO_ID), str(RERUN_REPO_ID)]))
    # The check still EXISTS after the re-run (re-posted or, when the verdict is unchanged, left in place — both
    # are a correct refresh; a stuck-"expected" box would have NO Veripsa check at all).
    checks.append(("rerequest handler: the Veripsa check is present after the re-run (box not left stuck 'expected')",
                   len(fail_gh.list_check_runs(REPO, f"{91:040x}")) == 1))
    # NOTIFY-vs-REFRESH separation: a rerequest is NOT a CI failure, so it records NO stuck/failing fact.
    stuck_after_rerun = steward("""SELECT count(*) FROM (SELECT jsonb_array_elements(core.stuck_prs_surface()->'items') i) x
                                    WHERE i->>'pr'='PR-91' AND i->>'reason'='ci_failed'""")
    checks.append(("rerequest handler: a re-run records no new ci_failed fact (refresh, not a red signal)",
                   "stuck_prs" not in rr_res and stuck_after_rerun == stuck_after_fail))
    # check_suite rerequested (the merge-box "Re-run all checks") goes through the SAME path. PR-92 is open & clear.
    rr_suite = S.handle_event("check_suite", rerun_payload("check_suite", 92), db, fail_gh)
    checks.append(("rerequest handler: a check_suite `rerequested` (merge-box re-run all) replays the PR too",
                   rr_suite.get("reran") == ["PR-92"]))
    # CHECK-SUITE REQUESTED BACKSTOP: when GitHub has created Veripsa's own suite for a fresh PR head but the
    # pull_request/push delivery is missing or delayed, the requested suite should replay the PR instead of
    # leaving GitHub's merge box at "Veripsa expected/queued" with no check-run.
    fail_gh.files_by_pr[94] = ["backend/auth.py"]
    fail_gh.pr_objects[94] = {"number": 94, "base": {"ref": "main"}, "head": {"sha": f"{94:040x}"},
                              "user": {"login": "iris"}, "draft": False, "merged": False}
    req_suite = S.handle_event("check_suite",
                               rerun_payload("check_suite", 94, action="requested", app_slug="veripsa-core"),
                               db, fail_gh)
    checks.append(("check_suite requested backstop: Veripsa's own requested suite replays the associated PR",
                   req_suite.get("backstop") == "check_suite.requested"
                   and req_suite.get("associated_prs") == 1
                   and req_suite.get("reran") == ["PR-94"]
                   and any(c["sha"] == f"{94:040x}" for c in fail_gh.checks)))
    other_suite = S.handle_event("check_suite",
                                 rerun_payload("check_suite", 95, action="requested", app_slug="other-ci"),
                                 db, fail_gh)
    checks.append(("check_suite requested backstop: another app's requested suite is ignored (no redundant fanout)",
                   other_suite.get("noop") is True
                   and not any(c["sha"] == f"{95:040x}" for c in fail_gh.checks)))
    missing_app_suite = S.handle_event("check_suite",
                                       rerun_payload("check_suite", 96, action="requested"),
                                       db, fail_gh)
    checks.append(("check_suite requested backstop: missing app metadata fails closed (no replay)",
                   missing_app_suite.get("noop") is True
                   and not any(c["sha"] == f"{96:040x}" for c in fail_gh.checks)))
    malformed_app_payload = rerun_payload("check_suite", 97, action="requested")
    malformed_app_payload["check_suite"]["app"] = "veripsa-core"
    malformed_app_suite = S.handle_event("check_suite", malformed_app_payload, db, fail_gh)
    checks.append(("check_suite requested backstop: malformed app metadata fails closed (no replay)",
                   malformed_app_suite.get("noop") is True
                   and not any(c["sha"] == f"{97:040x}" for c in fail_gh.checks)))
    fail_gh.files_by_pr[98] = ["backend/session.py"]
    fail_gh.pr_objects[98] = {"number": 98, "base": {"ref": "main"}, "head": {"sha": f"{98:040x}"},
                              "user": {"login": "mallory"}, "draft": False, "merged": False}
    requested_action_suite = S.handle_event("check_suite",
                                            rerun_payload("check_suite", 98, action="requested_action",
                                                          app_slug="other-ci"),
                                            db, fail_gh)
    checks.append(("check_suite requested_action: non-Veripsa suite cannot bypass the requested backstop guard",
                   requested_action_suite.get("noop") is True
                   and not any(c["sha"] == f"{98:040x}" for c in fail_gh.checks)))
    malformed_requested_suite = S.handle_event("check_suite",
                                               {"action": "requested", "installation": {"id": 4242},
                                                "repository": {"id": REPO_ID, "full_name": REPO,
                                                               "default_branch": "main"},
                                                "check_suite": "nope"}, db, fail_gh)
    checks.append(("check_suite requested backstop: malformed suite node fails closed (no replay)",
                   malformed_requested_suite.get("noop") is True
                   and not any(c["sha"] == f"{99:040x}" for c in fail_gh.checks)))
    # SCOPE: a rerequest whose AUTHORITATIVE base is a NON-default branch is out of Veripsa's window → no re-run.
    # (PR-93's real object targets release-1.x even though the sparse check ref claimed main — the re-fetch wins.)
    fail_gh.pr_objects[93] = {"number": 93, "base": {"ref": "release-1.x"}, "head": {"sha": f"{93:040x}"},
                              "user": {"login": "wanda"}, "draft": False, "merged": False}
    rr_offbase = S.handle_event("check_run", rerun_payload("check_run", 93), db, fail_gh)
    checks.append(("rerequest handler: a re-run for a PR off the protected branch is skipped (out of window)",
                   rr_offbase.get("reran") == []))
    # NEVER-CRASH: a poison rerequest (node is a STRING, pull_requests entries are non-objects) is a clean no-op.
    rr_poison = S.handle_event("check_suite",
                               {"action": "rerequested", "installation": {"id": 4242},
                                "repository": {"id": REPO_ID, "full_name": REPO,
                                               "default_branch": "main"},
                                "check_suite": "nope"}, db, fail_gh)
    rr_poison2 = S.handle_event("check_run",
                                {"action": "rerequested", "installation": {"id": 4242},
                                 "repository": {"id": REPO_ID, "full_name": REPO,
                                                "default_branch": "main"},
                                 "check_run": {"head_sha": 123, "pull_requests": ["x", 1, None]}}, db, fail_gh)
    checks.append(("rerequest handler: a poison rerequest (string node / non-object PRs) never crashes",
                   rr_poison.get("reran") == [] and rr_poison2.get("reran") == []))
    # BOUNDED: many PRs on one shared commit cannot fan out past the cap on a single button-press.
    old_rerun_cap = S._RERUN_PR_CAP
    S._RERUN_PR_CAP = 1
    try:
        many = {"head_sha": f"{91:040x}",
                "pull_requests": [{"number": n, "base": {"ref": "main"}, "head": {"sha": f"{n:040x}"},
                                   "user": {"login": "wanda"}} for n in (91, 92, 93)]}
        rr_cap = S.handle_event("check_run",
                                {"action": "rerequested", "installation": {"id": 4242},
                                 "repository": {"id": REPO_ID, "full_name": REPO,
                                                "default_branch": "main"},
                                 "check_run": many}, db, fail_gh)
    finally:
        S._RERUN_PR_CAP = old_rerun_cap
    checks.append(("rerequest handler: the per-event re-run is BOUNDED (one press can't fan out an API storm)",
                   len(rr_cap.get("reran") or []) == 1 and rr_cap.get("rerun_capped") is True))

    # A direct push to main is an ingress-only operation: it records facts and queues exact graph convergence.
    # It performs neither extraction nor synchronous PR fan-out, so webhook latency is independent of repo size
    # and open-PR count. The convergence worker owns graph commit + in-flight refresh.
    STALE_REPO = "acme/stale-verdict"
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), STALE_REPO, "main", SHA))

    def stale_pr(action, number, author):
        return {"action": action, "number": number, "installation": {"id": 4242},
                "repository": {"full_name": STALE_REPO, "default_branch": "main", "id": 70040},
                "pull_request": {"base": {"ref": "main", "sha": SHA, "repo": {"id": 70040}},
                                 "head": {"sha": f"{number:040x}", "repo": {"id": 70040}},
                                 "user": {"login": author}, "merged": False}}

    def stale_push(sha, paths):
        return {"ref": "refs/heads/main", "after": sha,
                "repository": {"full_name": STALE_REPO, "default_branch": "main", "id": 70040},
                "pusher": {"name": "carol"}, "commits": [{"added": [], "modified": paths, "removed": []}]}

    stale_gh = FakeGitHub({40: ["backend/auth.py"]})
    S.handle_event("pull_request", stale_pr("opened", 40, "alice"), db, stale_gh)
    # A push to main queues convergence without synchronously re-posting PR-40.
    clear_patches_before = len(stale_gh.check_patches) + len(stale_gh.patches)
    p1 = S.handle_event("push", stale_push("e" * 40, ["backend/worker.py"]), db, stale_gh)
    clear_noise = (len(stale_gh.check_patches) + len(stale_gh.patches)) - clear_patches_before
    checks.append((f"main-push ingress: durable graph queue, zero synchronous PR fan-out/noise "
                   f"(mode={p1.get('mode')}, refreshed={p1.get('refreshed_inflight')}, re-posts={clear_noise})",
                   p1.get("mode") == "queued"
                   and p1.get("graph_refresh", {}).get("queued") is True
                   and not p1.get("refreshed_inflight") and clear_noise == 0))

    # Now make PR-40 NON-clear: PR-42 (dave) edits backend/api.py, which calls+imports auth.py → PR-40 & PR-42
    # are coupled in-flight (warn). The live event must only wake durable convergence; its isolated posting slice
    # then replaces PR-40's graph-pending advisory with the current warning.
    pr40_before = next((c for c in stale_gh.comments if c["number"] == 40), None)
    pr40_before_body = pr40_before["body"] if pr40_before else None
    pr40_patch_count = stale_gh.patches.count(pr40_before["id"]) if pr40_before else 0
    stale_gh.files_by_pr[42] = ["backend/api.py"]
    r42 = S.handle_event("pull_request", stale_pr("opened", 42, "dave"), db, stale_gh)
    pr40_after_event = next((c for c in stale_gh.comments if c["number"] == 40), None)
    checks.append(("stale-verdict live event defers PR-40 and performs zero synchronous neighbor mutation",
                   r42.get("refreshed_inflight") == 0
                   and r42.get("refresh_deferred", 0) >= 1
                   and (pr40_after_event["body"] if pr40_after_event else None) == pr40_before_body
                   and (stale_gh.patches.count(pr40_before["id"]) if pr40_before else 0)
                   == pr40_patch_count))
    stale_worker_progress = S._post_refreshes(
        stale_gh, STALE_REPO, r42.get("refreshed") or [], db=db, branch="main",
        return_progress=True,
    )
    pr40_comment = next((c for c in stale_gh.comments if c["number"] == 40), None)
    checks.append((f"stale-verdict isolated turn refreshes the now-warned neighbor "
                   f"(posted={stale_worker_progress.get('posted')}, PR-40 has a comment={pr40_comment is not None})",
                   stale_worker_progress.get("posted", 0) >= 1 and pr40_comment is not None
                   and "Heads up" in (pr40_comment["body"] if pr40_comment else "")))
    # A subsequent push also remains constant-work: it only advances the durable target.
    warn_posts_before = len(stale_gh.patches)
    p2 = S.handle_event("push", stale_push("f" * 40, ["backend/worker.py"]), db, stale_gh)
    warn_reposts = len(stale_gh.patches) - warn_posts_before
    checks.append((f"main-push ingress remains constant-work with multiple in-flight PRs "
                   f"(mode={p2.get('mode')}, refreshed={p2.get('refreshed_inflight')}, "
                   f"comment-updates={warn_reposts})",
                   p2.get("mode") == "queued"
                   and p2.get("graph_refresh", {}).get("queued") is True
                   and not p2.get("refreshed_inflight") and warn_reposts == 0))

    # ── OFF-WORKER NEIGHBOR REFRESH + NO-CHURN: a new PR coalesces one durable wake. The bounded isolated turn
    #    posts the current siblings; a later unchanged event neither mutates siblings synchronously nor causes
    #    content-identical patches. Own repo coordinate so earlier checks cannot perturb the counts. ────────────
    NBR_REPO = "acme/neighbor-refresh"
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), NBR_REPO, "main", SHA))

    def nbr_pr(action, number, author):
        return {"action": action, "number": number, "installation": {"id": 4242},
                "repository": {"full_name": NBR_REPO, "default_branch": "main", "id": 70060},
                "pull_request": {"base": {"ref": "main", "sha": SHA, "repo": {"id": 70060}},
                                 "head": {"sha": f"{number:040x}", "repo": {"id": 70060}},
                                 "user": {"login": author}, "merged": False}}

    nbr_gh = FakeGitHub({60: ["backend/auth.py"]})
    # PR-60 queues graph convergence and stays Unknown until the isolated worker commits the exact turn.
    S.handle_event("pull_request", nbr_pr("opened", 60, "alice"), db, nbr_gh)
    pr60_before = next((c for c in nbr_gh.comments if c["number"] == 60), None)
    pr60_check_before = next((c for c in reversed(nbr_gh.checks)
                              if c.get("sha") == f"{60:040x}"), None)
    checks.append(("neighbor-refresh: an isolated graph-pending PR is honestly Unknown, not false-green",
                   bool(pr60_before) and "treated as unknown rather than cleared" in pr60_before["body"]
                   and bool(pr60_check_before)
                   and pr60_check_before.get("conclusion") == "neutral"
                   and "code graph not confirmed current" in (pr60_check_before.get("title") or "")))
    # PR-61 (bob) opens editing api.py, which CALLS+IMPORTS auth.py → it couples to PR-60. PR-61's own surface is
    # immediate; PR-60 must stay byte-stable until the isolated convergence posting slice runs.
    pr60_original_body = pr60_before["body"]
    pr60_original_patch_count = nbr_gh.patches.count(pr60_before["id"])
    nbr_gh.files_by_pr[61] = ["backend/api.py"]
    r61 = S.handle_event("pull_request", nbr_pr("opened", 61, "bob"), db, nbr_gh)
    pr60_after_event = next((c for c in nbr_gh.comments if c["number"] == 60), None)
    checks.append(("neighbor-refresh: colliding PR records a durable wake with zero synchronous PR-60 patch",
                   r61.get("refreshed_inflight") == 0
                   and r61.get("refresh_deferred", 0) >= 1
                   and pr60_after_event["body"] == pr60_original_body
                   and nbr_gh.patches.count(pr60_before["id"]) == pr60_original_patch_count))
    nbr_worker_progress = S._post_refreshes(
        nbr_gh, NBR_REPO, r61.get("refreshed") or [], db=db, branch="main",
        return_progress=True,
    )
    pr60_after = next((c for c in nbr_gh.comments if c["number"] == 60), None)
    checks.append((f"neighbor-refresh: isolated turn posts the current PR-60 warning "
                   f"(posted={nbr_worker_progress.get('posted')})",
                   nbr_worker_progress.get("posted", 0) >= 1 and pr60_after is not None
                   and "veripsa:PR-60" in pr60_after["body"] and "Heads up" in pr60_after["body"]))
    # NO-CHURN: re-deliver a SYNCHRONIZE on PR-61 that changes NOTHING about the cluster (same files → same
    # verdicts for every PR). The neighbor (60) is still coupled = still 'warn', but its rendered content is
    # IDENTICAL to what is already posted → it must NOT be re-PATCHed (comment OR check). The acting PR (61) is
    # likewise unchanged. Zero comment patches + zero check patches across the whole cluster = earned silence.
    patches_before = len(nbr_gh.patches)
    check_patches_before = len(nbr_gh.check_patches)
    S.handle_event("pull_request", nbr_pr("synchronize", 61, "bob"), db, nbr_gh)
    churn_comments = len(nbr_gh.patches) - patches_before
    churn_checks = len(nbr_gh.check_patches) - check_patches_before
    checks.append((f"neighbor-refresh NO-CHURN: a re-sync with an UNCHANGED cluster re-PATCHes NOTHING "
                   f"(comment re-patches={churn_comments}, check re-patches={churn_checks})",
                   churn_comments == 0 and churn_checks == 0))
    # And the content-aware skip must NOT block a REAL change: when PR-61 narrows OUT of the coupling, the event
    # again only wakes convergence; the isolated slice then performs PR-60's clear_reset.
    nbr_gh.files_by_pr[61] = ["backend/worker.py"]      # worker.py does not couple to auth.py
    pr60_id = pr60_after["id"]
    pr60_patches_before = nbr_gh.patches.count(pr60_id)
    r61_narrow = S.handle_event("pull_request", nbr_pr("synchronize", 61, "bob"), db, nbr_gh)
    pr60_patches_after_event = nbr_gh.patches.count(pr60_id)
    narrow_worker_progress = S._post_refreshes(
        nbr_gh, NBR_REPO, r61_narrow.get("refreshed") or [], db=db, branch="main",
        return_progress=True,
    )
    pr60_patches_after_worker = nbr_gh.patches.count(pr60_id)
    checks.append((f"neighbor-refresh: a REAL verdict change is deferred, then its clear_reset fires off-worker "
                   f"(posted={narrow_worker_progress.get('posted')})",
                   r61_narrow.get("refreshed_inflight") in (None, 0)
                   and (r61_narrow.get("refresh_deferred", 0) >= 1
                        or r61_narrow.get("graph_heal", {}).get("queued") is True)
                   and pr60_patches_after_event == pr60_patches_before
                   and pr60_patches_after_worker > pr60_patches_after_event))

    # ── GRAPH-FRESHNESS SELF-HEAL: a missed push leaves the graph behind. The next PR must durably request the
    #    exact current coordinate and return promptly; clone/extract belongs only to the isolated worker. Until
    #    that worker commits, freshness and the PR surface remain honestly behind/Unknown.
    HEAL_REPO = "acme/self-heal"
    OLD_SHA = "1" * 40
    NEW_HEAD = "2" * 40

    class HealGitHub(FakeGitHub):
        """A FakeGitHub whose main HEAD is settable + that COUNTS tarball downloads (= full re-ingests), so the
        test can assert the self-heal re-ingested exactly when the stored graph was behind HEAD, not otherwise."""
        def __init__(self, files_by_pr, head_sha):
            super().__init__(files_by_pr)
            self._head_sha = head_sha
            self.tarball_downloads = 0

        def repo_default_branch_head(self, repo):
            return "main", self._head_sha

        def download_tarball(self, repo, sha):
            self.tarball_downloads += 1
            return super().download_tarball(repo, sha)

    HEAL_REPO_ID = _fixture_repo_id(HEAL_REPO)
    # 1) seed main's graph at OLD_SHA (the last push the App actually saw) and bind its stable identity.
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), HEAL_REPO, "main", OLD_SHA))
    db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (HEAL_REPO, str(HEAL_REPO_ID)))
    stored_before = _json_scalar(db("SELECT core.coordinate_graph_sha(%s,%s)", (HEAL_REPO, "main")))
    checks.append(("self-heal: read-back reports the stored graph sha (the freshness primitive)",
                   stored_before.get("commit_sha") == OLD_SHA))

    # 2) a push to main happened but the App MISSED it: point HEAD at NEW_HEAD without delivering the push event.
    heal_gh = HealGitHub({50: ["backend/api.py"]}, head_sha=NEW_HEAD)
    for _heal_number, _heal_author in ((50, "erin"), (51, "frank")):
        heal_gh.pr_objects[_heal_number] = {
            "number": _heal_number,
            "base": {"ref": "main", "sha": NEW_HEAD,
                     "repo": {"id": HEAL_REPO_ID, "full_name": HEAL_REPO}},
            "head": {"sha": f"{_heal_number:040x}",
                     "repo": {"id": HEAL_REPO_ID, "full_name": HEAL_REPO}},
            "user": {"login": _heal_author},
            "draft": False,
            "merged": False,
        }
    # freshness must now report BEHIND (stored OLD_SHA != HEAD NEW_HEAD).
    fresh_before = S.graph_freshness(db, heal_gh, HEAL_REPO, "main")
    checks.append((f"self-heal: freshness reports BEHIND when stored != HEAD "
                   f"(stored={fresh_before.get('stored_sha','')[:4]}, head={fresh_before.get('head_sha','')[:4]})",
                   fresh_before.get("behind") is True
                   and fresh_before.get("stored_sha") == OLD_SHA and fresh_before.get("head_sha") == NEW_HEAD))

    # 3) the NEXT PR eval queues HEAD and performs zero tarball reads/extractions in the webhook process.
    dl_before = heal_gh.tarball_downloads
    heal_res = S.handle_event("pull_request",
                               {"action": "opened", "number": 50, "installation": {"id": 4242},
                               "repository": {"full_name": HEAL_REPO, "default_branch": "main",
                                              "id": HEAL_REPO_ID},
                               "pull_request": {"base": {"ref": "main", "sha": NEW_HEAD},
                                                "head": {"sha": f"{50:040x}"},
                                                "user": {"login": "erin"}, "merged": False}},
                              db, heal_gh)
    healed_to = _json_scalar(db("SELECT core.coordinate_graph_sha(%s,%s)", (HEAL_REPO, "main")))
    checks.append((f"self-heal: the next PR durably queues stale main @HEAD without inline extraction "
                   f"(queued={heal_res.get('graph_heal',{}).get('queued')}, "
                   f"downloads={heal_gh.tarball_downloads - dl_before}, stored_now={healed_to.get('commit_sha','')[:4]})",
                   heal_res.get("graph_heal", {}).get("queued") is True
                   and heal_res.get("graph_heal", {}).get("head_sha") == NEW_HEAD
                   and (heal_gh.tarball_downloads - dl_before) == 0
                   and healed_to.get("commit_sha") == OLD_SHA))

    # 4) BOUNDED: another PR may latest-wins refresh the durable coordinate, but still performs zero content reads.
    dl_before2 = heal_gh.tarball_downloads
    heal_gh.files_by_pr[51] = ["backend/auth.py"]
    heal_res2 = S.handle_event("pull_request",
                                {"action": "opened", "number": 51, "installation": {"id": 4242},
                                "repository": {"full_name": HEAL_REPO, "default_branch": "main",
                                               "id": HEAL_REPO_ID},
                                "pull_request": {"base": {"ref": "main", "sha": NEW_HEAD},
                                                 "head": {"sha": f"{51:040x}"},
                                                 "user": {"login": "frank"}, "merged": False}},
                               db, heal_gh)
    checks.append((f"self-heal ingress is BOUNDED: repeated PR performs no clone/extract "
                   f"(queued={heal_res2.get('graph_heal',{}).get('queued')}, "
                   f"downloads={heal_gh.tarball_downloads - dl_before2})",
                   heal_res2.get("graph_heal", {}).get("queued") is True
                   and (heal_gh.tarball_downloads - dl_before2) == 0))

    # 5) FRESHNESS SURFACE + ALERT: pending work is not greenwashed; it stays behind until worker commit.
    # The fleet observer now rotates one coordinate per account so a
    # graph-heavy tenant cannot monopolize every sample. Assert this exact
    # coordinate through the singular freshness primitive; the aggregate
    # alert is exercised separately below.
    heal_now = S.graph_freshness(db, heal_gh, HEAL_REPO, "main")
    checks.append(("self-heal: freshness stays honestly BEHIND while the durable worker turn is pending",
                   bool(heal_now) and heal_now.get("behind") is True))
    fresh_all_drift = S.graph_freshness_all(db, heal_gh)
    import alerts as A  # the alert evaluator (pure)

    class _Rec:
        def __init__(self): self.fired = []
        def fire(self, key, level, msg, fields=None): self.fired.append((key, level, fields or {}))
        def resolve(self, key): pass
    rec = _Rec()
    n_behind = A.evaluate_graph_freshness(rec, fresh_all_drift, behind_count_threshold=1)
    checks.append((f"self-heal: a re-introduced drift makes the freshness ALERT fire (behind={n_behind})",
                   n_behind >= 1 and any(k == "graph_stale" for k, _, _ in rec.fired)
                   and all(set(f).issubset({"behind_count", "worst_age_seconds", "stale_threshold_seconds"})
                           for _, _, f in rec.fired)))

    # 6) the watchdog tick wires the freshness alert end-to-end (the live PUSH path).
    class _WorkerStub:
        def is_alive(self): return True
        def qsize(self): return 0
        def maxsize(self): return 1000
        def processed(self): return 0
        def failed(self): return 0
        def uptime(self): return 1.0
    wd_rec = _Rec()
    S.watchdog_tick(wd_rec, _WorkerStub(), db, 0, gh=heal_gh)
    checks.append(("self-heal: the watchdog tick fires graph_stale when a coordinate is behind HEAD",
                   any(k == "graph_stale" for k, _, _ in wd_rec.fired)))

    # 7) OPERATOR DB-USAGE alert is DRIVEN by the SAME watchdog tick (the periodic path). Set a tiny cap so the
    #    real bootstrapped DB is over the critical line, run a tick, and assert db_usage_high fires end-to-end
    #    (surface read → evaluate_db_usage → AlertSink). The sample is content-free (only percent + byte counts).
    saved_cap = os.environ.get("VERIPSA_DB_SIZE_CAP_MB")
    os.environ["VERIPSA_DB_SIZE_CAP_MB"] = "1"   # 1 MiB cap — any real DB is far over → CRITICAL fires
    try:
        usage_rec = _Rec()
        S.watchdog_tick(usage_rec, _WorkerStub(), db, 0, gh=None)   # no gh needed: db-usage only needs the DB
        usage_fired = [(k, lvl, f) for k, lvl, f in usage_rec.fired if k == "db_usage_high"]
        checks.append(("operator db-usage: the watchdog tick fires db_usage_high when the DB exceeds the cap",
                       bool(usage_fired)))
        checks.append(("operator db-usage: the tick's alert is content-free (only percent + byte counts; no rows)",
                       bool(usage_fired) and all(set(f).issubset(
                           {"pct_used", "warn_pct", "critical_pct", "db_total_bytes", "cap_mb"})
                           for _, _, f in usage_fired)))
    finally:
        if saved_cap is None:
            os.environ.pop("VERIPSA_DB_SIZE_CAP_MB", None)
        else:
            os.environ["VERIPSA_DB_SIZE_CAP_MB"] = saved_cap

    # WEBHOOK BODY BOUND (memory-DoS guard): the public webhook endpoint must read the request body with a HARD
    # bound on the ACTUAL bytes, BEFORE the HMAC check or json.loads — an unauthenticated caller cannot make us
    # buffer an arbitrary multi-MB body. read_bounded_body returns (body, status): None status = ok to process.
    BCAP = 1024
    # (a) UNDER cap: an honest small body is read intact and handed on for processing (status None).
    under = b'{"action":"opened"}'
    u_body, u_err = S.read_bounded_body(str(len(under)), io.BytesIO(under), cap=BCAP)
    checks.append((f"webhook body-bound: an UNDER-cap body still processes (err={u_err}, len={len(under)})",
                   u_err is None and u_body == under and json.loads(u_body)["action"] == "opened"))
    # (b) OVER cap by declared Content-Length: rejected 413 WITHOUT reading the body (the cheap header line).
    big = b"x" * (BCAP + 5000)
    o_body, o_err = S.read_bounded_body(str(len(big)), io.BytesIO(big), cap=BCAP)
    checks.append((f"webhook body-bound: an OVER-cap declared length is 413'd before buffering (err={o_err})",
                   o_err == 413 and o_body is None))
    # (c) LYING small Content-Length but a huge streamed body: we read EXACTLY the declared bytes and NO more —
    #     the attacker's extra megabytes are left UNREAD in the socket (never buffered or parsed), so the memory
    #     bound holds; the short body then fails the HMAC check downstream. (The previous `rfile.read(cap+1)`
    #     PROBE here is what HUNG every live POST — it waited for cap+1 bytes a keep-alive socket never sends.)
    lie_body, lie_err = S.read_bounded_body("10", io.BytesIO(big), cap=BCAP)
    checks.append((f"webhook body-bound: a LYING small Content-Length reads ONLY the declared bytes, not the stream (len={len(lie_body or b'')})",
                   lie_err is None and lie_body == big[:10] and len(lie_body) == 10))
    # (d) a body EXACTLY at the cap is accepted (boundary, not off-by-one); cap+1 is rejected.
    at_cap = b"y" * BCAP
    ac_body, ac_err = S.read_bounded_body(str(BCAP), io.BytesIO(at_cap), cap=BCAP)
    over_one = b"z" * (BCAP + 1)
    oo_body, oo_err = S.read_bounded_body(str(BCAP + 1), io.BytesIO(over_one), cap=BCAP)
    checks.append((f"webhook body-bound: exactly-at-cap accepted, cap+1 rejected (at={ac_err}, over={oo_err})",
                   ac_err is None and len(ac_body) == BCAP and oo_err == 413 and oo_body is None))
    # (e) over-cap rejection does NOT crash + never reaches json.loads (the worker is untouched: no enqueue path
    #     is exercised because read_bounded_body short-circuits with a status before any parse).
    crashed = False
    try:
        S.read_bounded_body(str(len(big)), io.BytesIO(big), cap=BCAP)
    except Exception:
        crashed = True
    checks.append(("webhook body-bound: an over-cap body is rejected WITHOUT crashing the worker (no exception)",
                   not crashed))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("SERVER GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
