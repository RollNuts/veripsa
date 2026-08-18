#!/usr/bin/env python3
"""BRANCH-RESERVATION ADVISORY-ONLY gate (#851 ROOT MODEL) — a 'BR-<branch>' lane reservation (a pushed branch
with NO open PR) is NOT in the merge queue, so it must NEVER hard-block a real PR, REGARDLESS OF AGE. This
SUPERSEDES the #917/#923 age-threshold decay (which only softened a BR once it was idle past a knob).

THE PRINCIPLE (root, not band-aid):

  "Wait in line"/Paused means "another real in-flight PR is ahead of you in the MERGE QUEUE." A 'BR-<branch>'
  claim is, by invariant, a pushed branch with no open PR (a PR re-declares those paths as 'PR-<n>') — it is NOT
  in the merge queue. So:
    • MERGE-ORDER QUEUE = 'PR-<n>' claims ONLY. Real PRs order among themselves (claimed_at / PR number) →
      hard 'serialize' leader/follower exactly as today.
    • A 'BR-<branch>' holder is ADVISORY: a collision whose HOLDER is a non-release 'BR-' claim yields at most
      'serialize_soft' (Heads up), never a hard 'serialize'/Paused — with NO age condition. AND it must not stop
      the waiting PRs from ordering among THEMSELVES: when the active lane holder is a 'BR-', the waiting 'PR-'
      claims order among themselves so the EARLIEST PR is the effective leader (Heads-up) and later PRs
      hard-'serialize' behind that earlier PR (not behind the BR).
    • RELEASE-PATTERN EXEMPTION stays opt-in: a 'BR-' matching release_branch_patterns (default
      release/*,hotfix/*,develop,main) MAY still hard-block. Non-release 'BR-' → always advisory.

THE FIX (a read-time classifier in core.main_impact_surface, FUNCTION-ONLY, gen 7→8): waits_all computes
holder_advisory = (change_id LIKE 'BR-%' AND NOT core._branch_matches_release(<branch>)) — no age term — and adds
a synthetic (later-PR → earliest-PR) HARD wait whenever the active holder is an advisory BR, so real PRs order
among themselves. The verdict CTE ORs holder_advisory into the soft set (⇒ 'serialize_soft'), and serialize_behind
excludes advisory holders. #923's last_real_push_at column + note_branch_push_head_with_authority stay IN PLACE
(unused by the predicate) so the deploy is function-only / contention-free.

FIVE cases, all driven through the real per-event processor (make_db_processor → handle_event) with GitHub I/O
faked, the tenant pinned by the stable owner id, and the verdict read back through the
surface with the installation pinned:

  1. PR-BEHIND-BR-ONLY → LEADER: a FRESH (never backdated) non-release BR reservation + one upstream PR on the same
     path → the PR is NOT hard-serialized (verdict 'serialize_soft', serialize_behind == [], no pause/ACK). Proven
     with the BR's real-push signal ~now, so AGE cannot be the reason — the old age-decay would have hard-blocked it.
  2. PR-vs-PR STILL HARD: PR-13 leads, PR-14 hard-serializes behind PR-13 (no BR involved — the ordinary merge queue).
  3. PR-vs-PR BEHIND A BR (the demo scenario): BR-collide-a active + PR-13 + PR-14 on the same path → PR-13 is the
     LEADER (advisory Heads-up re collide-a, serialize_behind == []); PR-14 hard-serializes behind PR-13 (NOT behind
     the BR — serialize_behind names PR-13, never the BR). The real PRs order among themselves.
  4. RELEASE-PATTERN EXEMPTION: an idle-or-fresh BR-release/1.0 (matches release_branch_patterns) STILL hard-blocks
     a PR (verdict 'serialize', serialize_behind names the release BR) — the opt-in exemption is preserved.
  5. CONTENT-FREE: no source/diff BODY token from the fixture ever reaches a rendered check or comment.

Run:  python3 tests/test_stale_branch_reservation_decay.py   (needs local Postgres with the veripsa roles)
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

DB = "veripsa_bradvtest_" + str(os.getpid())
ACCOUNT_ID = 771                              # the stable owner id → enter_installation provisions ACCT-GH-771
TENANT = f"ACCT-GH-{ACCOUNT_ID}"
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
DSN_MIG = f"postgresql://veripsa_migrator@localhost/{DB}"

FILE = "backend/api.py"                        # a real file node in the sample_app fixture graph (avoids 'unknown')
# a token that lives ONLY in the BODY of backend/api.py (never a symbol name / path / branch). If it ever reaches
# a rendered check or comment, the content-free contract is broken.
BODY_TOKEN = "denied"
MAIN_SHA = "c" * 40


def _repo_id(repo):
    return 820_000 + sum((i + 1) * ord(c) for i, c in enumerate(repo))


class FakeGitHub:
    """Records what the App WOULD post; serves PR files + a repo tarball + a settable main HEAD.
    post_check/upsert_check keep title+summary so the gate can read the rendered verdict title + conclusion."""
    def __init__(self, files_by_pr=None, head_sha=MAIN_SHA):
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
        self.checks.append({"id": self._check_id, "sha": sha, "conclusion": conclusion,
                            "title": title, "summary": summary, "name": "Veripsa"})

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha and c.get("name") == "Veripsa"]

    def patch_check(self, repo, cid, conclusion, title, summary):
        for c in self.checks:
            if c["id"] == cid:
                c["conclusion"] = conclusion; c["title"] = title; c["summary"] = summary; return c
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

    seed_live_installation(DSN_APP, DSN_MIG, ACCOUNT_ID, 4242)

    gh = FakeGitHub()
    proc = S.make_db_processor(DSN_APP)

    def deliver(event_type, payload):
        proc(event_type, payload, None, gh)

    def surface(repo):
        """Read core.main_impact_surface as the App would (veripsa_app, installation pinned to the tenant)."""
        conn = psycopg2.connect(DSN_APP)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.installation_account', %s, true)", (TENANT,))
                cur.execute("SELECT core.main_impact_surface(%s,%s)", (repo, "main"))
                row = cur.fetchone()
        finally:
            conn.close()
        imp = row[0] if row else None
        if isinstance(imp, str):
            imp = json.loads(imp)
        imp = imp or {}
        return {c["change_id"]: c for c in imp.get("changes", [])}, imp

    def real_push_age(repo, cid):
        """Seconds since a BR reservation's last GENUINE push (last_real_push_at) — lets CASE 1 PROVE the BR is
        FRESH (age ~0) so 'advisory not hard' cannot be attributed to any idle-decay threshold."""
        conn = psycopg2.connect(DSN_MIG)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.current_account', %s, true)", (TENANT,))
                cur.execute("""SELECT extract(epoch FROM now()-COALESCE(last_real_push_at, claimed_at))
                                 FROM core.claim WHERE repo=%s AND change_id=%s AND claim_state='active'""",
                            (repo, cid))
                row = cur.fetchone()
        finally:
            conn.close()
        return float(row[0]) if row and row[0] is not None else None

    def check_for(repo, sha):
        runs = gh.list_check_runs(repo, sha)
        return runs[-1] if runs else None

    def comment_text(repo, number):
        return "\n".join(c["body"] for c in gh.list_issue_comments(repo, number))

    def baseline(repo):
        deliver("push", _push(repo, "main", "b" * 40))     # ingest a main graph so the analyze path has a baseline

    checks = []

    # ── CASE 1 — PR-BEHIND-BR-ONLY → LEADER (age must not matter): a FRESH non-release BR reservation + one PR ──
    R1 = "acme/leader"
    UP1 = "d1" + "0" * 38
    gh.files_by_pr = {51: [FILE]}
    baseline(R1)
    deliver("push", _push(R1, "collide-a", "e" * 40, files=[FILE]))         # reserve BR-collide-a on FILE (FRESH)
    # NO backdating: the reservation's real-push signal is ~now. Under the #923 age-decay a fresh BR HARD-blocked;
    # the root model makes it advisory anyway.
    deliver("pull_request", _pr("opened", R1, 51, "amy", UP1, head_ref="up/51"))
    rp1 = real_push_age(R1, "BR-collide-a")
    ch1, _ = surface(R1)
    v1 = ch1.get("PR-51", {}).get("verdict")
    behind1 = ch1.get("PR-51", {}).get("serialize_behind", [])
    chk1 = check_for(R1, UP1)
    concl1 = (chk1 or {}).get("conclusion")
    title1 = (chk1 or {}).get("title", "")
    body1 = comment_text(R1, 51)
    fresh1 = rp1 is not None and rp1 < 3600                                 # BR is genuinely FRESH (age ~0)
    no_pause1 = concl1 != "action_required"
    no_waitcopy1 = ("Wait in line" not in title1) and ("Wait in line" not in body1) and ("veripsa-ack" not in body1)
    checks.append((
        "PR-BEHIND-BR-ONLY → LEADER: a FRESH non-release BR-collide-a reservation does NOT hard-serialize the "
        f"upstream PR — advisory 'serialize_soft', serialize_behind == [], no pause/ACK, and the BR is genuinely "
        f"fresh so AGE is not the reason (real_push_age={None if rp1 is None else round(rp1)}s verdict={v1!r} "
        f"serialize_behind={behind1} check_conclusion={concl1!r})",
        fresh1 and v1 == "serialize_soft" and behind1 == [] and no_pause1 and no_waitcopy1))

    # ── CASE 2 — PR-vs-PR STILL HARD (no BR): PR-13 leads, PR-14 hard-serializes behind PR-13 ──
    R2 = "acme/prqueue"
    UP13 = "21" + "0" * 38
    UP14 = "22" + "0" * 38
    gh.files_by_pr = {13: [FILE], 14: [FILE]}
    baseline(R2)
    deliver("pull_request", _pr("opened", R2, 13, "bob", UP13, head_ref="up/13"))     # PR-13 → active (leader)
    deliver("pull_request", _pr("opened", R2, 14, "cara", UP14, head_ref="up/14"))    # PR-14 → waiting behind PR-13
    ch2, _ = surface(R2)
    v13 = ch2.get("PR-13", {}).get("verdict")
    behind13 = ch2.get("PR-13", {}).get("serialize_behind", [])
    v14 = ch2.get("PR-14", {}).get("verdict")
    behind14 = ch2.get("PR-14", {}).get("serialize_behind", [])
    checks.append((
        "PR-vs-PR STILL HARD: PR-13 is the leader (NOT hard, serialize_behind == []) and PR-14 hard-serializes "
        f"behind PR-13 (the ordinary merge queue is unchanged) (v13={v13!r} behind13={behind13} v14={v14!r} "
        f"behind14={behind14})",
        v13 != "serialize" and behind13 == []
        and v14 == "serialize" and behind14 != [] and all("PR-13" in x for x in behind14)))

    # ── CASE 3 — PR-vs-PR BEHIND A BR (the demo scenario): BR-collide-a active + PR-13 + PR-14 on the SAME path ──
    #    PR-13 is the LEADER (advisory Heads-up re collide-a), PR-14 hard-serializes behind PR-13 (NOT the BR). ──
    R3 = "acme/demo"
    D13 = "31" + "0" * 38
    D14 = "32" + "0" * 38
    gh.files_by_pr = {13: [FILE], 14: [FILE]}
    baseline(R3)
    deliver("push", _push(R3, "collide-a", "e" * 40, files=[FILE]))         # reserve BR-collide-a on FILE (active)
    deliver("pull_request", _pr("opened", R3, 13, "dave", D13, head_ref="up/13"))     # PR-13 → waiting behind BR
    deliver("pull_request", _pr("opened", R3, 14, "evan", D14, head_ref="up/14"))     # PR-14 → waiting behind BR
    ch3, _ = surface(R3)
    v3_13 = ch3.get("PR-13", {}).get("verdict")
    behind3_13 = ch3.get("PR-13", {}).get("serialize_behind", [])
    v3_14 = ch3.get("PR-14", {}).get("verdict")
    behind3_14 = ch3.get("PR-14", {}).get("serialize_behind", [])
    # PR-14 must be queued behind PR-13 (a real PR), never behind the BR (its label carries no 'PR-' ref).
    behind14_is_pr13_only = behind3_14 != [] and all("PR-13" in x for x in behind3_14)
    checks.append((
        "PR-vs-PR BEHIND A BR (demo): BR-collide-a active — PR-13 is the LEADER (advisory 'serialize_soft' re "
        "collide-a, serialize_behind == []); PR-14 HARD-serializes behind PR-13, NOT behind the BR "
        f"(v13={v3_13!r} behind13={behind3_13} v14={v3_14!r} behind14={behind3_14})",
        v3_13 == "serialize_soft" and behind3_13 == []
        and v3_14 == "serialize" and behind14_is_pr13_only))

    # ── CASE 4 — RELEASE-PATTERN EXEMPTION (opt-in preserved): BR-release/1.0 STILL hard-blocks a PR ──
    R4 = "acme/release"
    UP53 = "41" + "0" * 38
    gh.files_by_pr = {53: [FILE]}
    baseline(R4)
    deliver("push", _push(R4, "release/1.0", "e" * 40, files=[FILE]))       # reserve BR-release/1.0 on FILE
    deliver("pull_request", _pr("opened", R4, 53, "faye", UP53, head_ref="up/53"))
    ch4, _ = surface(R4)
    v4 = ch4.get("PR-53", {}).get("verdict")
    behind4 = ch4.get("PR-53", {}).get("serialize_behind", [])
    checks.append((
        "RELEASE-PATTERN EXEMPTION: BR-release/1.0 matches release_branch_patterns and STILL hard-serializes the "
        f"upstream PR (the opt-in exemption is preserved) (verdict={v4!r} serialize_behind={behind4})",
        v4 == "serialize" and behind4 != []))

    # ── CASE 5 — CONTENT-FREE: no source/diff body token from the fixture ever reaches a rendered check or comment ──
    blobs = [c.get("title", "") + "\x1f" + c.get("summary", "") for c in gh.checks]
    blobs += [c["body"] for c in gh.comments]
    leaked = [b for b in blobs if BODY_TOKEN in b]
    checks.append((
        "CONTENT-FREE: no file BODY token (%r from %s) appears in any rendered check title/summary or comment — "
        "the surface carries branches/paths/ids/timestamps only (checks=%d comments=%d)"
        % (BODY_TOKEN, FILE, len(gh.checks), len(gh.comments)),
        leaked == []))

    ok = all(c[1] for c in checks)
    print("\n== BRANCH-RESERVATION ADVISORY-ONLY ==")
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("BRANCH-RESERVATION ADVISORY-ONLY GATE: " + ("PASS" if ok else "FAIL"))
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
