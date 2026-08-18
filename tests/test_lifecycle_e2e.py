#!/usr/bin/env python3
"""LIFECYCLE E2E GATE — the REAL customer journey, end-to-end, from BEFORE installation to uninstall.

Where tests/test_server.py drives ONE behavior per synthesized payload (each a focused unit over the gate),
this test drives the WHOLE journey as one continuous story through the real per-event path
(github-app/server.make_db_processor → handle_event), so it catches COMPOSE bugs — a step that passes in
isolation but breaks the NEXT step's state. The whole journey runs in ONE tenant (a single GitHub
installation), exactly as a real customer experiences it: install → baseline push → PR-A → a coupled PR-B
→ merge PR-A → grow the fleet → uninstall. Every step asserts correct state/verdict, NO crash, content-free.

Driven through `make_db_processor` (the actual EventQueue processor) so each event opens its own connection,
takes the per-repo advisory lock, and — critically — pins the installation's tenant account via
`enter_installation_with_authority`, then runs handle_event. GitHub I/O is replaced by the recording fake
(reused from test_server). State is read back through the migrator
with the tenant account pinned (the App writes via gates; raw SELECTs go through migrator past RLS), exactly
as test_server does.

Run:  python3 tests/test_lifecycle_e2e.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
import server as S  # noqa: E402
import policy_refresh_queue as PR  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets two concurrent
# runs drop each other's DB mid-run. Per-PID, exactly like db/smoke.sh / run_gates / test_server.
DB = "veripsa_e2etest_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"

# ONE customer = ONE installation. Its STABLE owning-account id (repository.owner.id / installation.account.id)
# is what enter_installation routes by → ACCT-GH-<id>. The whole journey lands in this single tenant.
INSTALL_ID = 5555
ACCOUNT_ID = 700700                  # the org/user that owns the repo (the stable tenant key)
TENANT = f"ACCT-GH-{ACCOUNT_ID}"     # what enter_installation_with_authority provisions for ACCOUNT_ID
REPO = "acme/shop"
REPO_ID = 700_701
MAIN_SHA = "c" * 40
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")


class FakeGitHub:
    """Records what the App WOULD post, and serves PR files + a repo tarball from the sample_app fixture.
    The same recording fake test_server uses — only the GitHub I/O is faked; the brain + gate are real."""

    def __init__(self, files_by_pr=None, open_prs=None):
        self.files_by_pr = files_by_pr or {}
        self.open_prs = open_prs or []
        self.prs = {}
        self.checks, self.comments = [], []
        self.check_patches, self.patches, self.installations = [], [], []
        self._comment_id = 1000
        self._check_id = 2000

    def for_installation(self, installation_id):
        self.installations.append(str(installation_id))
        return self

    def app_installation_identity(self, installation_id):
        return {"installation_id": str(installation_id), "account_id": str(ACCOUNT_ID),
                "created_at": "2026-01-01T00:00:00Z", "suspended": False}

    def app_account_installation_identity(self, account_id):
        # This journey's uninstall is authoritative: the complete App-installations scan is already absent.
        return None

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return self.files_by_pr.get(number, [])

    def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
        changed = list(self.files_by_pr.get(number, []))
        return {"changed": changed, "changed_ranges": {}, "added_paths": [],
                "conflict_markers": [], "raw_entry_count": len(changed)}

    def compare_changed_paths_strict(self, repo, base_sha, branch):
        return []

    def post_check(self, repo, sha, conclusion, title, summary):
        self._check_id += 1
        check = {"id": self._check_id, "sha": sha, "conclusion": conclusion,
                 "title": title, "summary": summary, "name": "Veripsa"}
        self.checks.append(check)
        return check

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha and c.get("name") == "Veripsa"]

    def patch_check(self, repo, check_run_id, conclusion, title, summary):
        for c in self.checks:
            if c["id"] == check_run_id:
                c.update({"conclusion": conclusion, "title": title, "summary": summary})
                self.check_patches.append(check_run_id)
                return c
        raise AssertionError(f"check not found: {check_run_id}")

    def upsert_check(self, repo, sha, conclusion, title, summary):
        existing = self.list_check_runs(repo, sha)
        if existing:
            return self.patch_check(repo, existing[0]["id"], conclusion, title, summary)
        return self.post_check(repo, sha, conclusion, title, summary)

    def post_comment(self, repo, number, body):
        self._comment_id += 1
        self.comments.append({"id": self._comment_id, "number": number, "body": body, "user": {"type": "Bot"}})

    def list_issue_comments(self, repo, number):
        return [c for c in self.comments if c["number"] == number]

    def patch_comment(self, repo, comment_id, body):
        for c in self.comments:
            if c["id"] == comment_id:
                c["body"] = body
                self.patches.append(comment_id)
                return c
        raise AssertionError(f"comment not found: {comment_id}")

    def upsert_comment(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"] or (c["body"].startswith("### Veripsa") and c.get("user", {}).get("type") == "Bot"):
                return self.patch_comment(repo, c["id"], body)
        return self.post_comment(repo, number, body)

    def patch_comment_if_exists(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"]:
                self.patch_comment(repo, c["id"], body() if callable(body) else body)
                return True
        return False

    def list_open_pull_requests(self, repo, limit=None):
        return self.open_prs if limit is None else self.open_prs[:limit]

    def pull_request_head(self, repo, number):
        return self.prs[number]["head"]["sha"]

    def get_pull_request(self, repo, number):
        return self.prs[number]

    def repo_default_branch_head(self, repo):
        return "main", "c" * 40

    def repo_onboarding_head_info(self, repo):
        return {
            "repository_id": REPO_ID,
            "owner_id": ACCOUNT_ID,
            "full_name": repo,
            "default_branch": "main",
            "head_sha": "c" * 40,
            "empty": False,
        }

    def repo_current_identity(self, repo):
        return {"id": REPO_ID, "full_name": repo, "owner_id": ACCOUNT_ID}

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(FIXTURE, arcname="acme-shop-" + sha[:7])   # GitHub nests under one top dir
        return buf.getvalue()

    def get_file_at(self, repo, path, ref):
        full = os.path.join(FIXTURE, path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()


# ── payload builders (the real webhook SHAPES, carrying the installation + the stable owner id) ─────────────
def install_created(repos):
    return {"action": "created",
            "installation": {"id": INSTALL_ID, "account": {
                "id": ACCOUNT_ID, "login": "acme", "type": "Organization"}},
            "repositories": [{"id": REPO_ID, "full_name": r} for r in repos]}


def install_deleted(repos):
    return {"action": "deleted",
            "installation": {"id": INSTALL_ID, "account": {
                "id": ACCOUNT_ID, "login": "acme", "type": "Organization"}},
            "repositories": [{"id": REPO_ID, "full_name": r} for r in repos]}


def pr_event(action, number, author, files, head_sha=None, merged=False, repo=REPO):
    head_sha = head_sha or f"{number:040x}"
    return {"action": action, "number": number,
            "installation": {"id": INSTALL_ID, "account": {"id": ACCOUNT_ID}},
            "repository": {"id": REPO_ID, "full_name": repo, "default_branch": "main",
                           "owner": {"id": ACCOUNT_ID}},
            "pull_request": {"number": number, "state": "closed" if action == "closed" else "open",
                             "changed_files": len(files),
                             "base": {"ref": "main", "sha": MAIN_SHA, "repo": {"id": REPO_ID}},
                             "head": {"sha": head_sha, "repo": {"id": REPO_ID}, "ref": f"feature/{number}"},
                             "user": {"login": author}, "merged": merged}}, files


def push_main(sha, pusher, modified, repo=REPO):
    return {"ref": "refs/heads/main", "after": sha,
            "installation": {"id": INSTALL_ID, "account": {"id": ACCOUNT_ID}},
            "repository": {"id": REPO_ID, "full_name": repo, "default_branch": "main",
                           "owner": {"id": ACCOUNT_ID}},
            "pusher": {"name": pusher},
            "commits": [{"added": [], "modified": modified, "removed": []}]}


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    # the LIVE processor: per event → connection + advisory lock + enter_installation(account) + handle_event.
    # ONE FakeGitHub instance threads the whole journey (so check/comment upserts compose across events, just
    # like a real repo's GitHub state persists between deliveries).
    gh = FakeGitHub()
    proc = S.make_db_processor(DSN_APP)
    delivery_seq = 0

    def deliver(event_type, payload):
        """Drive ONE webhook through the exact live per-event path. Returns nothing (the processor is
        fire-and-forget, like the worker) — state is observed via the surfaces / readback below."""
        if event_type == "pull_request":
            gh.prs[payload["number"]] = payload["pull_request"]
        nonlocal delivery_seq
        if event_type == "installation" and payload.get("action") in {
                "created", "unsuspend", "new_permissions_accepted", "deleted"}:
            delivery_seq += 1
            key = f"lifecycle-e2e-{payload['action']}-{delivery_seq}"
            stored = dict(payload)
            stored.pop("_veripsa_delivery_key", None)
            conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("SET search_path=core")
                    cur.execute(
                        "INSERT INTO core.webhook_delivery("
                        "delivery_key,event_type,account_key,payload,status,received_at) "
                        "VALUES (%s,%s,%s,%s::jsonb,'processing',clock_timestamp())",
                        (key, event_type, str(ACCOUNT_ID), json.dumps(stored)),
                    )
            finally:
                conn.close()
            payload["_veripsa_delivery_key"] = key
        proc(event_type, payload, None, gh)

    def drain_graph():
        result = PR._drain_policy_refreshes(
            PR.PolicyRefreshStore(DSN_APP), gh, DSN_APP, limit=20,
            graph_refresh_strict=S.converge_main_graph_strict)
        assert result.get("graph_drained", 0) >= 1, f"graph convergence failed: {result!r}"

    # readback as the migrator with the tenant account pinned (App writes via gates; raw SELECT past RLS here).
    def admin(sql, args=()):
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.current_account', %s, true)", (TENANT,))
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    # One statement AS THE APP, with the installation pinned. The convergence posting seam needs the same
    # connect-per-statement shape as production, while app_surface below additionally decodes JSON for assertions.
    def app_db(sql, args=()):
        conn = psycopg2.connect(DSN_APP)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(ACCOUNT_ID),))
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    # a surface read AS THE APP (the exact path a dashboard read takes in prod: enter_installation routes the
    # App's connection into this tenant, then RLS walls the surface to it).
    def app_surface(sql, args=()):
        v = app_db(sql, args)
        return json.loads(v) if isinstance(v, str) else v

    def post_convergence_slice():
        """Emulate only the durable worker's bounded GitHub-posting slice, never the webhook hot path."""
        refreshed = S.refresh_inflight(app_db, REPO, "main")
        entries = refreshed.get("refreshed", []) if isinstance(refreshed, dict) else []
        return S._post_refreshes(
            gh, REPO, entries, db=app_db, branch="main", return_progress=True)

    def live_states(change_id, repo=REPO):
        v = admin("""SELECT COALESCE(jsonb_agg(claim_state ORDER BY claim_state),'[]'::jsonb)::text
                       FROM core.claim WHERE repo=%s AND change_id=%s AND branch='main'
                        AND claim_state IN ('active','waiting')""", (repo, change_id))
        return json.loads(v)

    def count(table, repo=REPO):
        return admin(f"SELECT count(*)::int FROM core.{table} WHERE repo=%s", (repo,))

    checks = []

    # ── STEP 1: INSTALLATION (the cold start). The customer installs the App. GitHub does NOT replay history,
    #    so onboarding must clone+ingest the default-branch graph AND backfill any already-open PRs. Before this
    #    event the tenant doesn't even exist; after it, the repo's graph + coordinate are live and predicting.
    nodes_pre = admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s", (REPO,))
    deliver("installation", install_created([REPO]))
    drain_graph()
    nodes_after_install = count("code_node")
    edges_after_install = count("code_edge")
    versions_after_install = count("graph_version")
    # the coordinate exists + is queryable AS THE APP in this tenant (a fresh-install dashboard read).
    impact_after_install = app_surface("SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
    tenant_provisioned = admin("SELECT count(*)::int FROM core.account WHERE account_id=%s", (TENANT,))
    checks.append((f"STEP 1 install: the tenant did NOT exist before, the install provisioned it "
                   f"(account rows={tenant_provisioned})", nodes_pre == 0 and tenant_provisioned == 1))
    checks.append((f"STEP 1 install: onboarding cold-started the repo — graph INGESTED (nodes={nodes_after_install}, "
                   f"edges={edges_after_install}, versions={versions_after_install})",
                   nodes_after_install > 0 and edges_after_install > 0 and versions_after_install >= 1))
    checks.append((f"STEP 1 install: the coordinate is live + queryable as the App (impact unknown_count="
                   f"{impact_after_install.get('unknown_count')})", isinstance(impact_after_install, dict)))

    # ── STEP 2: a PUSH to main (a baseline commit on the protected branch). The graph refreshes from the
    #    pushed tree (content-free) and a push/landing is recorded on the immutable ledger. Assert the
    #    coordinate's graph reflects the fixture's files (the files exist as 'file' nodes).
    deliver("push", push_main("a1" * 20, "alice", ["backend/api.py"]))
    drain_graph()
    file_nodes = admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s AND node_kind='file'", (REPO,))
    has_auth = admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s AND node_kind='file' AND path=%s",
                     (REPO, "backend/auth.py"))
    has_api = admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s AND node_kind='file' AND path=%s",
                    (REPO, "backend/api.py"))
    pushes_recorded = admin("SELECT count(*)::int FROM core.event WHERE repo=%s AND kind='push'", (REPO,))
    landed_recorded = admin("SELECT count(*)::int FROM core.event WHERE repo=%s AND kind='landed'", (REPO,))
    checks.append((f"STEP 2 push-to-main: the coordinate's graph reflects the fixture files "
                   f"(file nodes={file_nodes}, auth.py={has_auth}, api.py={has_api})",
                   file_nodes > 0 and has_auth == 1 and has_api == 1))
    checks.append((f"STEP 2 push-to-main: a push + landing are recorded on the ledger "
                   f"(push={pushes_recorded}, landed={landed_recorded})",
                   pushes_recorded >= 1 and landed_recorded >= 1))

    # ── STEP 3: PR-A OPENS, touching backend/auth.py (the call-graph hub everything downstream imports+calls).
    #    Alone in flight, PR-A is CLEAR (a green check, no coordination needed yet) and it CLAIMS its lane.
    pa_payload, pa_files = pr_event("opened", 101, "alice", ["backend/auth.py"])
    gh.files_by_pr[101] = pa_files
    deliver("pull_request", pa_payload)
    drain_graph()
    pa_states = live_states("PR-101")
    pa_check = next((c for c in reversed(gh.checks) if c.get("sha") == f"{101:040x}"), None)
    checks.append((f"STEP 3 PR-A opens: it CLAIMS its lane (PR-101 live states={pa_states})",
                   pa_states == ["active"]))
    checks.append((f"STEP 3 PR-A opens: a check is posted; alone in flight it is a green 'success' "
                   f"(conclusion={pa_check.get('conclusion') if pa_check else None})",
                   bool(pa_check) and pa_check.get("conclusion") == "success"))

    # ── STEP 4: PR-B OPENS, touching backend/api.py — which IMPORTS + CALLS auth.py (PR-A's file). This is the
    #    CORE cross-PR collision value: PR-B is structurally coupled UPSTREAM to PR-A → it must be WARNED, its
    #    comment naming alice (PR-A's author) as the foundation it sits on. The webhook must NOT mutate PR-A;
    #    the isolated convergence posting slice then refreshes the foundation to a warn naming dependent bob.
    pa_comment_before_pb = next((c for c in gh.comments if c["number"] == 101), None)
    pa_body_before_pb = pa_comment_before_pb["body"] if pa_comment_before_pb else None
    pa_comment_patches_before_pb = (
        gh.patches.count(pa_comment_before_pb["id"]) if pa_comment_before_pb else 0)
    pa_check_patches_before_pb = gh.check_patches.count(pa_check["id"])
    pb_payload, pb_files = pr_event("opened", 102, "bob", ["backend/api.py"])
    gh.files_by_pr[102] = pb_files
    deliver("pull_request", pb_payload)
    cb = next((c for c in gh.comments if c["number"] == 102), None)
    ca_after_event = next((c for c in gh.comments if c["number"] == 101), None)
    pb_check = next((c for c in reversed(gh.checks) if c.get("sha") == f"{102:040x}"), None)
    # the surface itself must classify PR-102 as warn and PR-101 as warn (coupled), as the App sees it.
    impact = app_surface("SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
    by_change = {c["change_id"]: c for c in impact.get("changes", [])}
    pb_verdict = (by_change.get("PR-102") or {}).get("verdict")
    pa_verdict = (by_change.get("PR-101") or {}).get("verdict")
    pb_depends = (by_change.get("PR-102") or {}).get("depends_on_changing") or []
    checks.append((f"STEP 4 PR-B coupled: the surface WARNS PR-B (api.py depends on auth.py being changed by PR-A) "
                   f"(PR-102 verdict={pb_verdict}, depends_on_changing={pb_depends})",
                   pb_verdict == "warn" and any(d.get("path") == "backend/auth.py" for d in pb_depends)))
    checks.append((f"STEP 4 PR-B coupled: PR-B's comment names the real foundation author alice + 'Heads up' "
                   f"(comment={bool(cb)})",
                   bool(cb) and "Heads up" in cb["body"] and "alice" in cb["body"]))
    # PAUSE-ACK (一時停止): PR-B is a MATERIAL coupling — a warn WITH an in-flight counterpart (alice's PR-A).
    # The pause-ack tier posts 'action_required' (paused until THIS coupling is acknowledged via the veripsa-ack
    # label) rather than 'neutral'. This is the tier that actually changes behavior (a 'neutral' advisory was
    # proven to change none). It is NOT a silent block: PR-B's comment offers the proceed-by-ack path, so the
    # author can always proceed by recording a conscious acknowledgement.
    checks.append((f"STEP 4 PR-B coupled: PR-B's check is PAUSED (action_required) until the coupling is acknowledged "
                   f"(conclusion={pb_check.get('conclusion') if pb_check else None})",
                   bool(pb_check) and pb_check.get("conclusion") == "action_required"
                   and bool(cb) and "veripsa-ack" in cb["body"]))
    checks.append(("STEP 4 webhook boundary: PR-B's event performs ZERO synchronous GitHub mutation on PR-A",
                   (ca_after_event["body"] if ca_after_event else None) == pa_body_before_pb
                   and (gh.patches.count(pa_comment_before_pb["id"]) if pa_comment_before_pb else 0)
                   == pa_comment_patches_before_pb
                   and gh.check_patches.count(pa_check["id"]) == pa_check_patches_before_pb))
    step4_progress = post_convergence_slice()
    ca = next((c for c in gh.comments if c["number"] == 101), None)
    pa_check_after_convergence = next(
        (c for c in reversed(gh.checks) if c.get("sha") == f"{101:040x}"), None)
    checks.append((f"STEP 4 stale-on-open FIX: the isolated convergence slice refreshes FOUNDATION PR-A "
                   f"from clear→warn, naming dependent bob (posted={step4_progress.get('posted')}, "
                   f"PR-101 verdict={pa_verdict}, comment={bool(ca)})",
                   step4_progress.get("posted", 0) >= 1 and pa_verdict == "warn"
                   and bool(ca) and "Heads up" in ca["body"] and "bob" in ca["body"]
                   and bool(pa_check_after_convergence)
                   and pa_check_after_convergence.get("conclusion") == "action_required"))

    # ── STEP 4b: a THIRD PR on the SAME file as PR-B → the same lane → SERIALIZED ("Wait in line"), queued
    #    BEHIND PR-B (the direct same-path collision, the other half of the cross-PR value). Non-blocking check.
    pc_payload, pc_files = pr_event("opened", 103, "carol", ["backend/api.py"])
    gh.files_by_pr[103] = pc_files
    deliver("pull_request", pc_payload)
    cc = next((c for c in gh.comments if c["number"] == 103), None)
    pc_states = live_states("PR-103")
    # PAUSE-ACK: PR-C is a MATERIAL direct collision (serialize) → also 'action_required' until acknowledged. The
    # full SIGNAL ("Wait in line") + the proceed-by-ack instruction both stay in the comment; the lane state and
    # the waiter ordering are unchanged (the pause tier overlays the conclusion, it does not alter lane mechanics).
    pc_check = next((c for c in reversed(gh.checks) if c["sha"] == f"{103:040x}"), None)
    checks.append((f"STEP 4b serialize: PR-C on PR-B's exact file WAITS IN LINE behind it, paused until acknowledged "
                   f"(PR-103 live states={pc_states}, comment={bool(cc)}, conclusion={pc_check.get('conclusion') if pc_check else None})",
                   pc_states == ["waiting"] and bool(cc) and "Wait in line" in cc["body"] and "veripsa-ack" in cc["body"]
                   and bool(pc_check) and pc_check.get("conclusion") == "action_required"))

    # ── STEP 5: PR-A MERGES (closed + merged). Its landing is recorded, its lanes RELEASED. Because PR-B was
    #    coupled to PR-A's foundation, PR-B must be RE-EVALUATED (the foundation it depended on is now landed),
    #    and the WAITER PR-C — wait, PR-C waited behind PR-B, not PR-A. Assert: PR-A's lane is gone (no ghost
    #    lane), the landing is recorded, and PR-B/PR-C lanes are unaffected by PR-A's merge (PR-B still holds
    #    api.py, PR-C still waits behind PR-B). This is the precise compose check: a merge releases ONLY the
    #    merged change's lanes, never a sibling's.
    landed_before_merge = admin("SELECT count(*)::int FROM core.event WHERE repo=%s AND kind='landed'", (REPO,))
    pushes_before_merge = admin("SELECT count(*)::int FROM core.event WHERE repo=%s AND kind='push'", (REPO,))
    # FAITHFUL-LANDING SHAPE: a real merge carries a merge_commit_sha (the NEW commit that lands on main); the
    # PR's head_sha lives on the feature branch and never reaches main. AND GitHub fires TWO webhooks for one
    # merge: the pull_request:closed:merged AND a push to main @ the merge commit. Drive BOTH (the real world)
    # to prove the ledger records the landing EXACTLY ONCE — no false push at head_sha, the push-to-main dedupes.
    MERGE_SHA_A = "ab" * 20
    merge_evt = pr_event("closed", 101, "alice", ["backend/auth.py"], head_sha="f" * 40, merged=True)[0]
    merge_evt["pull_request"]["merge_commit_sha"] = MERGE_SHA_A
    deliver("pull_request", merge_evt)
    deliver("push", push_main(MERGE_SHA_A, "alice", ["backend/auth.py"]))   # the push GitHub also fires for the merge
    drain_graph()
    pa_after_merge = live_states("PR-101")
    pb_after_merge = live_states("PR-102")
    pc_after_merge = live_states("PR-103")
    landed_after_merge = admin("SELECT count(*)::int FROM core.event WHERE repo=%s AND kind='landed'", (REPO,))
    pushes_after_merge = admin("SELECT count(*)::int FROM core.event WHERE repo=%s AND kind='push'", (REPO,))
    # the push fact for THIS merge must be keyed on the REAL on-main commit (the merge sha) — never the PR head.
    false_head_push = admin("SELECT count(*)::int FROM core.event WHERE repo=%s AND kind='push' AND commit_sha=%s",
                            (REPO, "f" * 40))
    checks.append((f"STEP 5 merge: PR-A's lanes are RELEASED — NO ghost lane left behind (PR-101 live={pa_after_merge})",
                   pa_after_merge == []))
    checks.append((f"STEP 5 merge: a landing is recorded for the merge (landed {landed_before_merge}→{landed_after_merge})",
                   landed_after_merge > landed_before_merge))
    # THE FAITHFUL-LEDGER GATE (audit:records): one merge == ONE push fact on the ledger, even though GitHub
    # double-fires (pull_request + push). The landing push is keyed on the MERGE commit, so the push-to-main
    # webhook for the same sha dedupes — and NO false 'push' is ever recorded for the PR's head_sha (a commit
    # that never reached main). Before the root-fix this recorded TWO pushes (head_sha + merge_sha) and inflated
    # effect_surface.landings / board.pushes for a non-existent landing.
    checks.append((f"STEP 5 FAITHFUL LEDGER: one merge (double-fired by GitHub) records EXACTLY ONE push fact, "
                   f"keyed on the real merge commit — no false head_sha push (pushes {pushes_before_merge}→"
                   f"{pushes_after_merge}, false_head_push={false_head_push})",
                   pushes_after_merge == pushes_before_merge + 1 and false_head_push == 0))
    checks.append((f"STEP 5 merge: a merge releases ONLY the merged change — PR-B still holds its lane, PR-C still "
                   f"waits behind PR-B (PR-102={pb_after_merge}, PR-103={pc_after_merge})",
                   pb_after_merge == ["active"] and pc_after_merge == ["waiting"]))

    # ── STEP 5b: now WITHDRAW PR-B (closed, NOT merged). PR-B's lane frees and the WAITER PR-C is PROMOTED to
    #    active immediately (not stranded until the lease expires) — and PR-C, now sole holder of api.py with
    #    auth.py already landed, downgrades to CLEAR: its stale "Wait in line behind PR-B" comment is rewritten
    #    and its check reset to green. The full promote+downgrade compose.
    pc_comment_before_withdraw = next(c for c in gh.comments if c["number"] == 103)
    pc_check_before_withdraw = next(
        c for c in reversed(gh.checks) if c.get("sha") == f"{103:040x}")
    pc_body_before_withdraw = pc_comment_before_withdraw["body"]
    pc_comment_patches_before_withdraw = gh.patches.count(pc_comment_before_withdraw["id"])
    pc_check_patches_before_withdraw = gh.check_patches.count(pc_check_before_withdraw["id"])
    comment_count_before_withdraw = len(gh.comments)
    check_count_before_withdraw = len(gh.checks)
    deliver("pull_request", pr_event("closed", 102, "bob", ["backend/api.py"], merged=False)[0])
    pb_after_withdraw = live_states("PR-102")
    pc_after_withdraw = live_states("PR-103")
    pc_comment_after_event = next(c for c in gh.comments if c["number"] == 103)
    checks.append(("STEP 5b webhook boundary: PR-B's withdraw performs ZERO synchronous PR-C GitHub mutation",
                   pc_comment_after_event["body"] == pc_body_before_withdraw
                   and gh.patches.count(pc_comment_before_withdraw["id"])
                   == pc_comment_patches_before_withdraw
                   and gh.check_patches.count(pc_check_before_withdraw["id"])
                   == pc_check_patches_before_withdraw))
    step5b_progress = post_convergence_slice()
    cc_now = next((c for c in gh.comments if c["number"] == 103), None)
    pc_check = next((c for c in reversed(gh.checks) if c.get("sha") == f"{103:040x}"), None)
    checks.append((f"STEP 5b withdraw+promote: PR-B's lane freed, PR-C PROMOTED to active immediately "
                   f"(PR-102={pb_after_withdraw}, PR-103={pc_after_withdraw})",
                   pb_after_withdraw == [] and pc_after_withdraw == ["active"]))
    checks.append((f"STEP 5b downgrade-to-clear: the isolated convergence slice removes PR-C's stale "
                   f"'Wait in line', rewrites the existing comment to 'Cleared', and resets its check to green "
                   f"(posted={step5b_progress.get('posted')}, conclusion="
                   f"{pc_check.get('conclusion') if pc_check else None})",
                   step5b_progress.get("posted", 0) >= 1
                   and bool(cc_now) and cc_now["id"] == pc_comment_before_withdraw["id"]
                   and "Wait in line" not in cc_now["body"] and "Cleared" in cc_now["body"]
                   and bool(pc_check) and pc_check["id"] == pc_check_before_withdraw["id"]
                   and pc_check.get("conclusion") == "success"
                   and len(gh.comments) == comment_count_before_withdraw
                   and len(gh.checks) == check_count_before_withdraw))

    # Re-running the isolated slice is semantically idempotent: it may PATCH the same GitHub objects, but it must
    # not create a duplicate comment/check or change the already-current customer-visible state.
    pc_body_after_first_slice = cc_now["body"]
    repeat_progress = post_convergence_slice()
    cc_after_repeat = next(c for c in gh.comments if c["number"] == 103)
    pc_check_after_repeat = next(c for c in reversed(gh.checks) if c.get("sha") == f"{103:040x}")
    checks.append((f"STEP 5b convergence replay is idempotent: same PR-C comment/check objects and stable clear "
                   f"surface (repeat posted={repeat_progress.get('posted')})",
                   cc_after_repeat["id"] == pc_comment_before_withdraw["id"]
                   and cc_after_repeat["body"] == pc_body_after_first_slice
                   and pc_check_after_repeat["id"] == pc_check_before_withdraw["id"]
                   and pc_check_after_repeat.get("conclusion") == "success"
                   and len(gh.comments) == comment_count_before_withdraw
                   and len(gh.checks) == check_count_before_withdraw))

    # ── STEP 6: GROWTH — the fleet expands. A NEW author (dave) opens a PR on a different file (billing.py),
    #    growing the in-flight fleet beyond the PR-A/B/C cluster, and a 2nd pusher (erin) lands on main. The
    #    board surface (the ops/console view) must then reflect the live fleet: multiple distinct authors
    #    holding lanes + the pushes recorded. Read the board AS THE APP in this tenant (the real dashboard path).
    gh.files_by_pr[110] = ["backend/billing.py"]
    deliver("pull_request", pr_event("opened", 110, "dave", ["backend/billing.py"])[0])
    # (a 2nd push by a different pusher exercises a real multi-author landing history on main.)
    deliver("push", push_main("b2" * 20, "erin", ["backend/reports.py"]))
    drain_graph()
    board = app_surface("SELECT core.board_surface()")
    fleet_agents = {f.get("agent") for f in board.get("fleet", [])}
    summary = board.get("summary", {})
    effect = app_surface("SELECT core.effect_surface()")
    checks.append((f"STEP 6 growth: the board reflects the live fleet — multiple authors holding lanes "
                   f"(fleet agents={sorted(fleet_agents)}, active_claims={summary.get('active_claims')})",
                   "dave" in fleet_agents and "carol" in fleet_agents and summary.get("active_claims", 0) >= 2))
    checks.append((f"STEP 6 growth: the board summary counts the pushes recorded on main (pushes={summary.get('pushes')})",
                   summary.get("pushes", 0) >= 2))
    checks.append((f"STEP 6 growth: the effect ledger tallies the product's own work — warns issued > 0 (PR-B was a "
                   f"recorded prediction) (warns_issued={effect.get('warns_issued')}, landings={effect.get('landings')})",
                   effect.get("warns_issued", 0) >= 1 and effect.get("landings", 0) >= 2))

    # CONTENT-FREE INVARIANT (privacy table-stakes, asserted across the whole accumulated working set + ledger):
    # NOTHING we stored is source code. Code nodes carry only structural facts (path, symbol NAME, kind, line
    # numbers); the event ledger carries only paths/labels. The fixture's auth.py has the body line
    # 'return len(token) > 10' and api.py has 'from auth import …' — assert NO stored node name / event detail
    # contains a code BODY fragment (a 'return …', a 'from … import', a '() {' brace) — only bare symbol names.
    leaked_node = admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s AND "
                        "(COALESCE(name,'') ILIKE %s OR COALESCE(name,'') ILIKE %s OR COALESCE(name,'') ILIKE %s)",
                        (REPO, "%return %", "%import %", "%len(token)%"))
    leaked_event = admin("SELECT count(*)::int FROM core.event WHERE repo=%s AND "
                         "(COALESCE(detail,'') ILIKE %s OR COALESCE(detail,'') ILIKE %s)",
                         (REPO, "%return %", "%import %"))
    # the known real symbol 'verify_user' (a NAME, content-free) IS stored as a def node — proves we kept the
    # structural facts while storing zero body. (auth.py defines verify_user; api.py imports+calls it.)
    kept_symbol = admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s AND node_kind='def' AND name=%s",
                        (REPO, "verify_user"))
    checks.append((f"CONTENT-FREE: no stored node name / event detail carries a code BODY fragment — only bare "
                   f"symbol names + paths (leaked_node={leaked_node}, leaked_event={leaked_event}, "
                   f"kept_symbol verify_user={kept_symbol})",
                   leaked_node == 0 and leaked_event == 0 and kept_symbol >= 1))

    # ── STEP 7: UNINSTALL (offboarding / purge — the symmetric counterpart to onboarding). The customer
    #    uninstalls. The content-free WORKING SET (code graph + live claims) must be FORGOTTEN for the repo,
    #    yet the append-only event ledger (push/landed audit — the immutable, content-free the append-only guarantee
    #    moat) must be RETAINED. Assert the working set is gone and the ledger survives intact.
    nodes_before_purge = count("code_node")
    claims_before_purge = admin("SELECT count(*)::int FROM core.claim WHERE repo=%s", (REPO,))
    events_before_purge = admin("SELECT count(*)::int FROM core.event WHERE repo=%s", (REPO,))
    deliver("installation", install_deleted([REPO]))
    nodes_after_purge = count("code_node")
    edges_after_purge = count("code_edge")
    versions_after_purge = count("graph_version")
    claims_after_purge = admin("SELECT count(*)::int FROM core.claim WHERE repo=%s", (REPO,))
    events_after_purge = admin("SELECT count(*)::int FROM core.event WHERE repo=%s", (REPO,))
    checks.append((f"STEP 7 purge precondition: a real working set + ledger existed before uninstall "
                   f"(nodes={nodes_before_purge}, claims={claims_before_purge}, events={events_before_purge})",
                   nodes_before_purge > 0 and claims_before_purge > 0 and events_before_purge > 0))
    checks.append((f"STEP 7 uninstall PURGES the content-free working set — graph + claims FORGOTTEN "
                   f"(nodes={nodes_after_purge}, edges={edges_after_purge}, versions={versions_after_purge}, "
                   f"claims={claims_after_purge})",
                   nodes_after_purge == 0 and edges_after_purge == 0
                   and versions_after_purge == 0 and claims_after_purge == 0))
    checks.append((f"STEP 7 uninstall RETAINS the append-only event ledger (immutable audit moat) "
                   f"(events {events_before_purge}→{events_after_purge})",
                   events_after_purge == events_before_purge and events_after_purge > 0))
    # the working set is truly forgotten: a post-uninstall surface read returns a clean empty coordinate (the
    # repo is gone, no crash) — the customer's code structure no longer sits in our DB.
    impact_after_purge = app_surface("SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
    checks.append((f"STEP 7 forgotten: a surface read after uninstall is a clean empty coordinate, no crash "
                   f"(changes={len(impact_after_purge.get('changes', []))})",
                   isinstance(impact_after_purge, dict) and impact_after_purge.get("changes") == []))

    # ── JOURNEY-WIDE: every single delivery was a clean dict result / no escaped crash (the processor catches
    #    nothing for us — an escape would have aborted main() before here). And the customer-facing artifacts
    #    posted across the WHOLE journey (every check title/summary + every comment body) are content-free:
    #    no internal role name / db-design term leaked into what the customer reads. (The dedicated jargon gate
    #    is exhaustive; this is the integration-angle spot check over the REAL artifacts this journey produced.)
    posted_text = " ".join([c["title"] + " " + c["summary"] for c in gh.checks] + [c["body"] for c in gh.comments])
    banned = ["veripsa_app", "veripsa_migrator", "ACCT-GH-", "ACCT-DEMO", "with_authority",
              "claim_state", "core.", "installation_account", "search_path"]
    leaked_terms = [t for t in banned if t in posted_text]
    checks.append((f"JOURNEY content-free: no internal role/db term leaked into the customer-facing checks/comments "
                   f"this journey produced (leaked={leaked_terms})", leaked_terms == []))
    checks.append((f"JOURNEY no-crash: every delivery composed without an escaped exception "
                   f"({len(gh.checks)} checks + {len(gh.comments)} comments posted across the lifecycle)",
                   len(gh.checks) >= 4 and len(gh.comments) >= 3))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("LIFECYCLE E2E GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
