#!/usr/bin/env python3
"""DRAFT BRANCH-LANE LEAK gate — a closed PR must NEVER strand its head branch's push-time 'BR-<head>' lane.

THE DEFECT (a stale-lane / phantom-in-flight leak, reproduced LIVE over the real handlers + DB):

  A feature-branch push reserves lanes the moment it lands, BEFORE any PR exists, under the work-unit change_id
  'BR-<head>' (webhook_handlers.reserve_branch_lanes). When the PR for that branch later OPENS, the push↔PR
  reconciliation RELEASES 'BR-<head>' and re-claims the same paths as 'PR-<n>' — but that reconcile runs ONLY on
  an ANALYZE action of a NON-draft PR (it is gated by `should_analyze and not draft`). A DRAFT PR returns EARLY
  ("draft — not yet heading to the protected branch") before the reconcile ever runs, so 'BR-<head>' stays live.

  Now the worst sequence: the author NEVER marks the draft ready (a common abandon) and just CLOSES it. The close
  handler releases the 'PR-<n>' change_id (which has ZERO claims — they were never declared for a draft) and the
  'BR-<head>' lane is NEVER touched. It leaks: it stays 'active' FOREVER (until the lease expires), a phantom
  in-flight that SERIALIZES / WARNS every future PR and every future push touching those same files behind a
  ghost change that no longer exists. (This is the "7 open PRs inflated a count" class — a stale claim nobody
  can see that mis-protects the repo.)

  The push↔PR reconcile correctly converges the NON-draft and draft→ready→close paths (proven below as the two
  CONTROLS). The leak is specific to the draft-then-closed-without-ready path.

THE FIX (webhook_handlers, the closed branch of the pull_request handler): a `closed` event ALSO releases the
head branch's 'BR-<head>' lanes (idempotent — a no-op when the open path already reconciled them to 'PR-<n>',
because release on an already-released change_id frees nothing). This is the withdraw half of the lifecycle
applied to the branch reservation the abandoned draft never converted. Content-free (a branch ref + paths);
advisory; never-crash (a release error is logged, never raised out of the handler). The happy path is unchanged
(the non-draft / draft→ready paths already had BR released at open time → the close-time release is a no-op).

Driven through the real per-event processor (make_db_processor → handle_event), with GitHub I/O replaced by a
recording fake and the tenant pinned through the same runtime routing seam (enter_installation by
the stable owner id). State is read back through the migrator with the account pinned (App writes via gates;
raw SELECT past RLS for the readback). Content-free.

Run:  python3 tests/test_draft_branch_lane_leak.py   (needs local Postgres with the veripsa roles)
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

import psycopg2  # noqa: E402
import server as S  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402

DB = "veripsa_draftleaktest_" + str(os.getpid())
ACCOUNT_ID = 770                              # the stable owner id → enter_installation provisions ACCT-GH-770
TENANT = f"ACCT-GH-{ACCOUNT_ID}"
MAIN_SHA = "c" * 40


def _repo_id(repo):
    return 830_000 + sum((i + 1) * ord(c) for i, c in enumerate(repo))
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"


class FakeGitHub:
    """Records what the App WOULD post; serves PR files + a repo tarball + a settable main HEAD."""
    def __init__(self, files_by_pr=None, head_sha="c" * 40):
        self.files_by_pr = files_by_pr or {}
        self.head_sha = head_sha
        self.checks, self.comments = [], []
        self._comment_id, self._check_id = 1000, 2000

    def for_installation(self, installation_id):
        return self

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return self.files_by_pr.get(number, [])

    def list_pr_files_with_ranges(self, repo, number, pr_changed_files=0):
        return {p: [] for p in self.files_by_pr.get(number, [])}

    def list_open_pull_requests(self, repo, cap):
        return []

    def post_check(self, repo, sha, conclusion, title, summary):
        self._check_id += 1
        self.checks.append({"id": self._check_id, "sha": sha, "conclusion": conclusion, "name": "Veripsa"})

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha and c.get("name") == "Veripsa"]

    def patch_check(self, repo, cid, conclusion, title, summary):
        for c in self.checks:
            if c["id"] == cid:
                c["conclusion"] = conclusion; return c
        raise AssertionError(f"check not found: {cid}")

    def upsert_check(self, repo, sha, conclusion, title, summary):
        ex = self.list_check_runs(repo, sha)
        if ex:
            return self.patch_check(repo, ex[0]["id"], conclusion, title, summary)
        return self.post_check(repo, sha, conclusion, title, summary)

    def post_comment(self, repo, number, body):
        self._comment_id += 1
        self.comments.append({"id": self._comment_id, "number": number, "body": body, "user": {"type": "Bot"}})

    def list_issue_comments(self, repo, number):
        return [c for c in self.comments if c["number"] == number]

    def patch_comment(self, repo, cid, body):
        for c in self.comments:
            if c["id"] == cid:
                c["body"] = body; return c
        raise AssertionError("comment not found")

    def upsert_comment(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"]:
                return self.patch_comment(repo, c["id"], body)
        return self.post_comment(repo, number, body)

    def patch_comment_if_exists(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"]:
                self.patch_comment(repo, c["id"], body() if callable(body) else body); return True
        return False

    def repo_default_branch_head(self, repo):
        return "main", self.head_sha

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        src = os.path.join(ROOT, "tests", "fixtures", "sample_app")
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(src, arcname="acme-app-" + sha[:7])
        return buf.getvalue()

    def get_file_at(self, repo, path, ref):
        return None


def _pr(action, repo, number, author, head_sha, base="main", merged=False, head_ref=None, draft=False):
    repo_id = _repo_id(repo)
    head = {"sha": head_sha, "repo": {"id": repo_id}}
    if head_ref:
        head["ref"] = head_ref
    return {"action": action, "number": number, "installation": {"id": 4242},
            "repository": {"id": repo_id, "full_name": repo, "default_branch": "main",
                           "owner": {"id": ACCOUNT_ID}},
            "pull_request": {"base": {"ref": base, "sha": MAIN_SHA, "repo": {"id": repo_id}},
                             "head": head, "user": {"login": author},
                             "merged": merged, "draft": draft}}


def _push(repo, branch, sha, files=None):
    commits = [{"added": files or [], "modified": [], "removed": []}] if files is not None else []
    return {"ref": f"refs/heads/{branch}", "after": sha, "installation": {"id": 4242},
            "repository": {"id": _repo_id(repo), "full_name": repo, "default_branch": "main",
                           "owner": {"id": ACCOUNT_ID}},
            "commits": commits, "pusher": {"name": "dev"}}


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    seed_live_installation(
        DSN_APP,
        f"postgresql://veripsa_migrator@localhost/{DB}",
        ACCOUNT_ID,
        4242,
    )

    gh = FakeGitHub()
    proc = S.make_db_processor(DSN_APP)

    def deliver(event_type, payload):
        proc(event_type, payload, None, gh)

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

    def live_cid(repo, cid):
        return admin("""SELECT count(*)::int FROM core.claim
                          WHERE repo=%s AND change_id=%s AND claim_state IN ('active','waiting')""", (repo, cid)) or 0

    def live_all(repo):
        return admin("""SELECT count(*)::int FROM core.claim
                          WHERE repo=%s AND claim_state IN ('active','waiting')""", (repo,)) or 0

    def baseline(repo):
        deliver("push", _push(repo, "main", "b" * 40))     # ingest a main graph so the analyze path has a baseline

    checks = []
    FILE = "backend/api.py"

    # ── THE DEFECT (PO 2026-06-25 SCOUT-WINDOW reframe): a feature push reserves BR-<head>; the PR opens AS A
    #    DRAFT. UNDER THE NEW POLICY a draft is the SCOUT WINDOW and receives FULL analysis — its open path
    #    reconciles BR-<head>→PR-<n> exactly like a non-draft, so the original "draft returns early, BR not
    #    reconciled" leak is gone at its ROOT. Closing the draft unmerged then releases PR-<n>, leaving zero
    #    live lanes — by reconciliation at open, not by a close-time BR release fallback. The close-time BR
    #    release stays as belt-and-suspenders (a no-op on the happy path). ──
    RA = "acme/draft-abandoned"
    gh.files_by_pr = {10: [FILE]}
    baseline(RA)
    deliver("push", _push(RA, "feat/10", "e" * 40, files=[FILE]))        # reserve BR-feat/10
    br_after_push = live_cid(RA, "BR-feat/10")
    deliver("pull_request", _pr("opened", RA, 10, "amy", "e" * 40, head_ref="feat/10", draft=True))
    br_after_draft_open = live_cid(RA, "BR-feat/10")                      # 0 — DRAFT now reconciles too (scout-window analysis)
    pr_after_draft_open = live_cid(RA, "PR-10")                          # 1 — a draft now declares its PR-<n> claims
    deliver("pull_request", _pr("closed", RA, 10, "amy", "e" * 40, head_ref="feat/10", draft=True, merged=False))
    br_after_close = live_cid(RA, "BR-feat/10")                          # MUST be 0 — no leak
    total_after_close = live_all(RA)                                     # MUST be 0 — no phantom in-flight remains
    checks.append((
        "DRAFT SCOUT-WINDOW: a draft PR open+close-unmerged leaves zero live lanes — the open path reconciles "
        f"BR-<head>→PR-<n> like a non-draft, the close releases PR-<n>, total converges to zero "
        f"(br_after_push={br_after_push} br_after_draft_open={br_after_draft_open} "
        f"pr_after_draft_open={pr_after_draft_open} br_after_close={br_after_close} total={total_after_close})",
        br_after_push == 1 and br_after_draft_open == 0 and pr_after_draft_open == 1
        and br_after_close == 0 and total_after_close == 0))

    # ── PHANTOM-CONTENTION PROOF: the leaked BR lane (if not released) would serialize a LATER unrelated PR that
    #    touches the SAME file behind a ghost. After the fix, a fresh PR on that file is the SOLE live change. ──
    gh.files_by_pr[11] = [FILE]
    deliver("pull_request", _pr("opened", RA, 11, "bea", "f" * 40, head_ref="feat/11"))
    pr11_live = live_cid(RA, "PR-11")
    total_with_pr11 = live_all(RA)
    checks.append((
        "NO PHANTOM CONTENTION: a fresh PR on the same file after the abandoned draft is the SOLE live change "
        f"(not serialized behind a ghost BR lane) (PR-11 live={pr11_live} total={total_with_pr11})",
        pr11_live == 1 and total_with_pr11 == 1))

    # ── CONTROL 1 (happy path unchanged): a NON-draft PR opened then closed-unmerged. BR is reconciled to PR at
    #    open, PR released at close → the close-time BR release is a harmless no-op; converges to zero. ──
    RB = "acme/nondraft"
    gh.files_by_pr = {20: [FILE]}
    baseline(RB)
    deliver("push", _push(RB, "feat/20", "e" * 40, files=[FILE]))
    deliver("pull_request", _pr("opened", RB, 20, "carl", "e" * 40, head_ref="feat/20"))
    br_open_nondraft = live_cid(RB, "BR-feat/20")                        # 0 — reconciled to PR at open
    pr_open_nondraft = live_cid(RB, "PR-20")                            # 1
    deliver("pull_request", _pr("closed", RB, 20, "carl", "e" * 40, head_ref="feat/20", merged=False))
    total_nondraft = live_all(RB)                                        # 0
    checks.append((
        "CONTROL (happy path unchanged): a NON-draft open→close-unmerged still converges to zero — open reconciled "
        f"BR→PR, close released PR, the added close-time BR release is a no-op (br_open={br_open_nondraft} "
        f"pr_open={pr_open_nondraft} total_after_close={total_nondraft})",
        br_open_nondraft == 0 and pr_open_nondraft == 1 and total_nondraft == 0))

    # ── CONTROL 2: a MERGED PR (the normal landing) still lands + releases everything; the close-time BR release
    #    is a no-op (BR was reconciled to PR at open). The landing fact must still record. ──
    RC = "acme/merged"
    gh.files_by_pr = {30: [FILE]}
    baseline(RC)
    deliver("push", _push(RC, "feat/30", "e" * 40, files=[FILE]))
    deliver("pull_request", _pr("opened", RC, 30, "dot", "e" * 40, head_ref="feat/30"))
    deliver("pull_request", _pr("closed", RC, 30, "dot", "e" * 40, head_ref="feat/30", merged=True))
    total_merged = live_all(RC)
    landed = admin("SELECT count(*)::int FROM core.event WHERE repo=%s AND kind='landed'", (RC,)) or 0
    checks.append((
        "CONTROL (merge unchanged): a merged PR releases all lanes (BR release is a no-op) and STILL records the "
        f"landing fact (total_live={total_merged} landed_events={landed})",
        total_merged == 0 and landed >= 1))

    ok = all(c[1] for c in checks)
    print("\n== DRAFT BRANCH-LANE LEAK ==")
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("DRAFT BRANCH-LANE LEAK GATE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        try:
            c = psycopg2.connect("postgresql://veripsa_migrator@localhost/postgres")
            c.autocommit = True
            with c.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS {DB}")
            c.close()
        except Exception:
            pass
