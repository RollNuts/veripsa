#!/usr/bin/env python3
"""WEBHOOK DELIVERY ORDERING / CAUSALITY gate — GitHub does NOT guarantee delivery ORDER (and re-delivers).

Idempotency (DUPLICATES) is covered by the comment-idempotency + server replay tests; this gate covers the
ORTHOGONAL axis: events arriving OUT OF ORDER, late, and with gaps. It proves the state stays causally
consistent — no stranded lanes, no resurrected merged PRs, no graph regressing to an older commit, no crash,
content-free — under the six adversarial orderings GitHub's at-least-once + no-ordering delivery can produce:

  1. `closed/merged` BEFORE its `opened` (reorder)         — landing records sanely; a LATER stale opened/sync
                                                              does NOT resurrect the never-seen-open merged PR.
  2. `synchronize`/`reopened` AFTER `closed-merged` (late) — no zombie claims (merged-PR resurrection guard).
  3. `push` to main for a repo whose `installation` has    — onboard-on-demand (lazy tenant provision), never a
     NOT yet arrived                                          crash / a write under a non-existent tenant.
  4. `pull_request` for a repo just `removed`              — the stable-id repo tombstone rejects a stale delivery;
     via installation_repositories                            no graph/claim/check is resurrected.
  5. OUT-OF-ORDER pushes: an OLDER sha AFTER a NEWER sha   — the stored main graph does NOT regress to the older
     (a retry)                                                sha (monotonic by ingest order; self-heal as backstop).
  6. a PR after its branch's push events reordered/dropped — push↔PR lane reconciliation stays consistent (no
                                                              self-collision, no orphan branch lane).

Run:  python3 tests/test_webhook_ordering.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import policy_refresh_queue as PR  # noqa: E402
import server as S  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402

DB = "veripsa_ordertest_" + str(os.getpid())   # PROCESS-UNIQUE (parallel-safe), like the other gates
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
DSN_MIG = f"postgresql://veripsa_migrator@localhost/{DB}"
SHA40 = "a" * 40


def _stable_repo_id(repo):
    """Deterministic positive bigint matching GitHub's rename-stable repository identity contract."""
    value = int.from_bytes(hashlib.sha256(repo.encode("utf-8")).digest()[:8], "big") & ((1 << 63) - 1)
    return value or 1


def make_db(role):
    def run(sql, args=()):
        conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    return run


class FakeGitHub:
    """Minimal fake: serves PR files + a repo tarball + a (settable) main HEAD; records what would be posted.
    head_sha drives the freshness/self-heal read-back (repo_default_branch_head)."""
    def __init__(self, files_by_pr=None, head_sha="c" * 40, tarball_sha_marker=None, repo_ids=None):
        self.files_by_pr = files_by_pr or {}
        self.head_sha = head_sha
        self.repo_ids = dict(repo_ids or {})
        self.checks, self.comments, self.patches, self.check_patches, self.installations = [], [], [], [], []
        self._comment_id, self._check_id = 1000, 2000

    def for_installation(self, installation_id):
        self.installations.append(str(installation_id)); return self

    def app_installation_identity(self, installation_id):
        return {"installation_id": str(installation_id), "account_id": "770",
                "created_at": "2026-01-01T00:00:00Z", "suspended": False}

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return self.files_by_pr.get(number, [])

    def list_pr_files_with_ranges(self, repo, number, pr_changed_files=0):
        return {p: [] for p in self.files_by_pr.get(number, [])}

    def post_check(self, repo, sha, conclusion, title, summary):
        self._check_id += 1
        check = {"id": self._check_id, "sha": sha, "conclusion": conclusion,
                 "title": title, "summary": summary, "name": "Veripsa"}
        self.checks.append(check)
        return check

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha and c.get("name") == "Veripsa"]

    def patch_check(self, repo, cid, conclusion, title, summary):
        for c in self.checks:
            if c["id"] == cid:
                c.update({"conclusion": conclusion, "title": title, "summary": summary}); self.check_patches.append(cid); return c
        raise AssertionError(f"check not found: {cid}")

    def upsert_check(self, repo, sha, conclusion, title, summary):
        ex = self.list_check_runs(repo, sha)
        if ex:
            cur = ex[0]
            if cur.get("conclusion") == conclusion and (cur.get("title") or "") == (title or "") and (cur.get("summary") or "") == (summary or ""):
                return cur
            return self.patch_check(repo, cur["id"], conclusion, title, summary)
        return self.post_check(repo, sha, conclusion, title, summary)

    def post_comment(self, repo, number, body):
        self._comment_id += 1
        comment = {"id": self._comment_id, "number": number, "body": body, "user": {"type": "Bot"}}
        self.comments.append(comment)
        return comment

    def list_issue_comments(self, repo, number):
        return [c for c in self.comments if c["number"] == number]

    def patch_comment(self, repo, cid, body):
        for c in self.comments:
            if c["id"] == cid:
                c["body"] = body; self.patches.append(cid); return c
        raise AssertionError("comment not found")

    def upsert_comment(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"]:
                if c["body"] == body:
                    return c
                return self.patch_comment(repo, c["id"], body)
        return self.post_comment(repo, number, body)

    def patch_comment_if_exists(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"]:
                nb = body() if callable(body) else body
                if c["body"] != nb:
                    self.patch_comment(repo, c["id"], nb)
                return True
        return False

    def repo_default_branch_head(self, repo):
        return "main", self.head_sha

    def repo_current_identity(self, repo):
        return {"id": self.repo_ids.get(repo, _stable_repo_id(repo)),
                "full_name": repo, "owner_id": 770}

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        src = os.path.join(ROOT, "tests", "fixtures", "sample_app")
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(src, arcname="acme-app-" + sha[:7])
        return buf.getvalue()

    def get_file_at(self, repo, path, ref):
        full = os.path.join(ROOT, "tests", "fixtures", "sample_app", path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()


def _pr(action, repo, number, author, head_sha, base="main", merged=False, head_ref=None, inst=4242,
        repo_id=None):
    repo_id = repo_id if repo_id is not None else _stable_repo_id(repo)
    head = {"sha": head_sha, "repo": {"id": repo_id, "full_name": repo}}
    if head_ref:
        head["ref"] = head_ref
    repository = {"id": repo_id, "full_name": repo, "default_branch": "main", "owner": {"id": 770}}
    return {"action": action, "number": number, "installation": {"id": inst},
            "repository": repository,
            "pull_request": {"base": {"ref": base, "sha": "b" * 40,
                                      "repo": {"id": repo_id, "full_name": repo}},
                             "head": head, "user": {"login": author}, "merged": merged}}


def _push(repo, branch, sha, owner_id=770, inst=4242, files=None, head_time=None, repo_id=None):
    commits = [{"added": files or [], "modified": [], "removed": []}] if files is not None else []
    repo_id = repo_id if repo_id is not None else _stable_repo_id(repo)
    repository = {"id": repo_id, "full_name": repo, "default_branch": "main", "owner": {"id": owner_id}}
    p = {"ref": f"refs/heads/{branch}", "after": sha, "installation": {"id": inst},
         "repository": repository,
         "commits": commits, "pusher": {"name": "dev"}}
    if head_time is not None:                                # head_commit.timestamp = the delivery-order clock
        p["head_commit"] = {"id": sha, "timestamp": head_time}
    return p


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    seed_live_installation(DSN_APP, DSN_MIG, 770, 4242)
    db = PR._FreshAccountDB(DSN_APP, "770")
    admin = make_db("veripsa_migrator")
    sys.path.insert(0, ROOT)
    import code_graph_extract as X

    def live(repo, change_id, account="ACCT-GH-770"):
        return admin("""SELECT set_config('core.current_account',%s,true);
            SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id=%s AND claim_state IN ('active','waiting')""",
                     (account, repo, change_id)) or 0

    def acct_for_owner(owner_id):
        # the live tenant for a given GH owner id (enter_installation keys by stable owner id → ACCT-GH-<id>).
        return admin("SELECT account_id FROM core.installation_account ORDER BY installation_id") or None

    checks = []

    captured = {}
    original_handle_event = S.handle_event

    def recording_handle_event(event_type, payload, body_db, gh, coalesce=None):
        result = original_handle_event(event_type, payload, body_db, gh, coalesce=coalesce)
        captured["result"] = result
        return result

    S.handle_event = recording_handle_event
    processor = S.make_db_processor(DSN_APP)

    def discard_refresh_surfaces(_gh, _repo, entries, **kwargs):
        pending = sorted(
            (entry for entry in (entries or [])
             if isinstance(entry, dict) and isinstance(entry.get("change"), str)
             and entry["change"] > str(kwargs.get("after_change") or "")),
            key=lambda entry: entry["change"],
        )
        return {"posted": 0, "processed": len(pending),
                "cursor": pending[-1]["change"] if pending else str(kwargs.get("after_change") or ""),
                "has_more": False, "errors": 0}

    def drain_graph(gh):
        result = PR._drain_policy_refreshes(
            PR.PolicyRefreshStore(DSN_APP), gh, DSN_APP, limit=20,
            graph_refresh_strict=S.converge_main_graph_strict,
            post_refreshes=discard_refresh_surfaces)
        assert result.get("graph_drained", 0) >= 1, f"graph convergence failed: {result!r}"

    def process_event(event_type, payload, gh):
        captured.clear()
        processor(event_type, payload, None, gh)
        result = captured.get("result")
        if not isinstance(result, dict):
            raise AssertionError(f"live processor did not dispatch {event_type}: {result!r}")
        queued = result.get("graph_refresh")
        if event_type == "pull_request":
            queued = result.get("graph_heal")
        if isinstance(queued, dict) and queued.get("queued"):
            drain_graph(gh)
        return result

    # ── PROBE 1: closed/merged BEFORE its opened (reorder). The merge for a PR whose `opened` was NEVER seen
    #    must record a sane landing (no orphan land fact, no crash) AND a LATER stale opened/synchronize must
    #    NOT resurrect that merged PR as in-flight (no claims declared on a concluded change). ───────────────
    R1 = "acme/p1-merge-before-open"
    gh1 = FakeGitHub({701: ["backend/auth.py"]}, head_sha="b" * 40)
    # seed a main graph for this repo (so analysis has a baseline; via a push to main).
    S.process_one = None
    g1 = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
    # route into the right tenant + ingest the baseline graph for R1 as the App (owner-keyed tenant).
    # (the push handler does the tenant routing live; here we drive handle_event directly so pin via a push.)
    process_event("push", _push(R1, "main", "b" * 40), gh1)
    merged_first = process_event("pull_request", _pr("closed", R1, 701, "amy", "f" * 40, merged=True), gh1)
    after_merge_live = live(R1, "PR-701")
    # the LATE, reordered opened (merged=false, as GitHub sends on an `opened`):
    stale_open = process_event("pull_request", _pr("opened", R1, 701, "amy", "f" * 40, merged=False), gh1)
    after_stale_open_live = live(R1, "PR-701")
    stale_sync = process_event("pull_request", _pr("synchronize", R1, 701, "amy", "f" * 40, merged=False), gh1)
    after_stale_sync_live = live(R1, "PR-701")
    checks.append((
        "PROBE 1: merge-before-open records a sane landing (no crash) AND a later stale opened/synchronize does "
        f"NOT resurrect the merged PR (after_merge={after_merge_live}, after_stale_open={after_stale_open_live}, "
        f"after_stale_sync={after_stale_sync_live}, merged_ok={bool(merged_first.get('landed'))})",
        bool(merged_first.get("landed")) and after_merge_live == 0
        and after_stale_open_live == 0 and after_stale_sync_live == 0
        and stale_open.get("skipped") is not None and stale_sync.get("skipped") is not None))

    # ── PROBE 2: synchronize/reopened AFTER closed-merged when the open WAS seen (late retry of an old delivery).
    #    Covered by test_server's order-independence checks too; re-assert here for the ordering gate's locality. ─
    R2 = "acme/p2-late-retry"
    gh2 = FakeGitHub({702: ["backend/api.py"]}, head_sha="b" * 40)
    process_event("push", _push(R2, "main", "b" * 40), gh2)
    process_event("pull_request", _pr("opened", R2, 702, "ben", "9" * 40, head_ref="feat/702"), gh2)
    open_live = live(R2, "PR-702")
    process_event("pull_request", _pr("closed", R2, 702, "ben", "9" * 40, head_ref="feat/702", merged=True), gh2)
    merged_live = live(R2, "PR-702")
    late_sync = process_event("pull_request", _pr("synchronize", R2, 702, "ben", "9" * 40, head_ref="feat/702"), gh2)
    late_reopen = process_event("pull_request", _pr("reopened", R2, 702, "ben", "9" * 40, head_ref="feat/702", merged=True), gh2)
    after_late_live = live(R2, "PR-702")
    checks.append((
        "PROBE 2: a late synchronize + a merged-flag reopened after the merge are both skipped — no zombie claims "
        f"(open={open_live}, merged={merged_live}, after_late={after_late_live})",
        open_live >= 1 and merged_live == 0 and after_late_live == 0
        and late_sync.get("skipped") is not None and late_reopen.get("skipped") is not None))

    # ── PROBE 3: push to main for a repo whose installation (onboarding) has NOT arrived yet. The processor's
    #    enter_installation lazily provisions the tenant keyed by the STABLE owner id → onboard-on-demand. Here
    #    we drive handle_event directly (the live processor does enter_installation); the invariant we assert is
    #    that the push handler never crashes + ingests under the (newly seen) coordinate, never a write failure. ─
    R3 = "acme/p3-push-no-install"
    gh3 = FakeGitHub(head_sha="d" * 40)
    push_res = process_event("push", _push(R3, "main", "d" * 40), gh3)
    stored3 = db("SELECT core.coordinate_graph_sha(%s,%s)", (R3, "main"))
    stored3 = stored3 if isinstance(stored3, dict) else json.loads(stored3)
    checks.append((
        "PROBE 3: a push for a not-yet-onboarded repo ingests on demand (no crash, graph stored at the pushed sha) "
        f"(ingested={push_res.get('ingested')}, stored_sha={(stored3.get('commit_sha') or '')[:7]})",
        push_res.get("ingested") == R3 and (stored3.get("commit_sha") or "") == "d" * 40))

    # ── PROBE 4: a pull_request for a repo just removed via installation_repositories `removed`. GitHub delivery
    #    order is not guaranteed, so an older PR delivery may be replayed after the removal. The stable repository
    #    id tombstone must make that delivery a true no-op: no graph, no claim, no check, and no bounded-or-not
    #    residual. A deleted repo is not an authorized write scope. ─────────────────────────────────────────────
    R4 = "acme/p4-removed-repo"
    gh4 = FakeGitHub({704: ["backend/auth.py"]}, head_sha="b" * 40, repo_ids={R4: 4004})
    process_event("push", _push(R4, "main", "b" * 40, repo_id=4004), gh4)
    process_event("pull_request", _pr("opened", R4, 704, "del", "e" * 40,
                                      head_ref="feat/704", repo_id=4004), gh4)
    before_purge_live = live(R4, "PR-704")
    remove_payload = {"action": "removed", "installation": {"id": 4242, "account": {"id": 770}},
                      "repository_selection": "selected",
                      "repositories_removed": [{"full_name": R4, "id": 4004}],
                      "_veripsa_delivery_key": "D-ORDER-REPO-REMOVED"}
    admin(
        "INSERT INTO core.webhook_delivery("
        "delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at) "
        "VALUES (%s,'installation_repositories','ACCT-GH-770',NULL,%s::jsonb,'processing',1,"
        "clock_timestamp(),clock_timestamp()) RETURNING 1",
        ("D-ORDER-REPO-REMOVED", json.dumps(remove_payload)),
    )
    purge = process_event("installation_repositories", remove_payload, gh4)
    after_purge_live = live(R4, "PR-704")
    purged_one = (purge.get("purged") or [{}])[0] if isinstance(purge.get("purged"), list) else {}
    # the content-free WORKING SET is FORGOTTEN (privacy contract: claims for the repo are 0 after purge):
    remaining_claims = admin("""SELECT set_config('core.current_account','ACCT-GH-770',true);
        SELECT count(*)::int FROM core.claim WHERE repo=%s""", (R4,)) or 0
    # a STALE pull_request for the removed repo (reordered redelivery after removal) composes WITHOUT crashing:
    crashed = False
    try:
        stale_after_remove = process_event("pull_request", _pr("synchronize", R4, 704, "del", "e" * 40,
                                                                head_ref="feat/704", repo_id=4004), gh4)
    except Exception:
        crashed = True
        stale_after_remove = {}
    after_stale_live = live(R4, "PR-704")
    checks.append((
        "PROBE 4: installation_repositories removed FORGETS the working set and a stale post-removal PR is "
        f"revocation-skipped with no resurrection (claims-after-purge={remaining_claims}, "
        f"after-stale={after_stale_live}, before_purge={before_purge_live}, "
        f"after_purge={after_purge_live}, crashed={crashed})",
        before_purge_live >= 1 and after_purge_live == 0 and remaining_claims == 0
        and after_stale_live == 0 and stale_after_remove.get("repository_revoked") is True
        and isinstance(purge.get("purged"), list) and bool(purged_one.get("ok")) and not crashed))

    # ── PROBE 5: OUT-OF-ORDER pushes — an OLDER sha delivered AFTER a NEWER sha (a retry). The stored main graph
    #    must NOT regress to the older sha. ─────────────────────────────────────────────────────────────────
    R5 = "acme/p5-oo-pushes"
    NEW, OLD = "1" * 40, "2" * 40
    T_NEW, T_OLD = "2026-06-18T12:00:00+00:00", "2026-06-18T11:00:00+00:00"   # NEW commit is one hour LATER
    gh5 = FakeGitHub(head_sha=NEW)
    process_event("push", _push(R5, "main", NEW, head_time=T_NEW), gh5)  # the NEWER push first (current HEAD)
    s_after_new = db("SELECT core.coordinate_graph_sha(%s,%s)", (R5, "main"))
    s_after_new = s_after_new if isinstance(s_after_new, dict) else json.loads(s_after_new)
    oo = process_event("push", _push(R5, "main", OLD, head_time=T_OLD), gh5)  # a RETRY of an OLDER push, late
    s_after_old = db("SELECT core.coordinate_graph_sha(%s,%s)", (R5, "main"))
    s_after_old = s_after_old if isinstance(s_after_old, dict) else json.loads(s_after_old)
    old_push_deferred = (
        bool(oo.get("stale"))
        or (isinstance(oo.get("graph_refresh"), dict) and oo["graph_refresh"].get("queued") is True)
    )
    checks.append((
        "PROBE 5: an OLDER-commit push arriving AFTER a NEWER one does NOT regress the stored main graph to the "
        f"older sha (after_new={(s_after_new.get('commit_sha') or '')[:7]}, after_old={(s_after_old.get('commit_sha') or '')[:7]}, "
        f"stale_or_durably_deferred={old_push_deferred})",
        (s_after_new.get("commit_sha") or "") == NEW and (s_after_old.get("commit_sha") or "") == NEW
        and old_push_deferred))

    # ── PROBE 6: a PR arriving after its branch's push events were reordered/dropped. The push reserved BR-<branch>
    #    lanes; the PR re-claims the SAME paths under PR-<n>. The reconciliation must leave exactly ONE live change
    #    on the lane (the PR), the branch reservation released — no self-collision, no orphan BR lane. ──────────
    R6 = "acme/p6-push-then-pr"
    gh6 = FakeGitHub({706: ["backend/auth.py"]}, head_sha="b" * 40)
    process_event("push", _push(R6, "main", "b" * 40), gh6)               # seed main graph
    process_event("push", _push(R6, "feat/706", "7" * 40, files=["backend/auth.py"]), gh6)  # branch push reserves BR lane
    br_live = live(R6, "BR-feat/706")
    process_event("pull_request", _pr("opened", R6, 706, "pat", "7" * 40, head_ref="feat/706"), gh6)
    br_after = live(R6, "BR-feat/706")
    pr_after = live(R6, "PR-706")
    checks.append((
        "PROBE 6: push reserves a BR lane; the later PR releases it + re-claims under PR-<n> — one live change on "
        f"the lane, no self-collision (BR_before={br_live}, BR_after={br_after}, PR_after={pr_after})",
        br_live >= 1 and br_after == 0 and pr_after >= 1))

    # ── report ───────────────────────────────────────────────────────────────────────────────────────────
    ok = True
    print("\n== WEBHOOK ORDERING / CAUSALITY ==")
    for label, passed in checks:
        print(("  [PASS] " if passed else "  [FAIL] ") + label)
        ok = ok and passed
    print("WEBHOOK ORDERING GATE: " + ("PASS" if ok else "FAIL"))
    S.handle_event = original_handle_event
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
