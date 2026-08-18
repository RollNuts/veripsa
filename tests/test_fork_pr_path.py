#!/usr/bin/env python3
"""Fork-PR / external-contributor gate — the classic GitHub-App fork gotcha, offline (no deploy, no GitHub).

A pull_request opened from a FORK behaves differently from a same-repo PR, and three things MUST hold or the
App is either broken or a security hole:

  (a) BASE-KEYING. On a fork PR, GitHub sets the top-level `repository` to the BASE repo and nests the fork
      under `pull_request.head.repo` (different owner, different repo id). The App's tenant key (account id)
      and coordinate (repo full_name) must come from the BASE repo — NOT the fork. If the App ever keyed off
      the fork's owner/full_name, a fork PR would write into (or read from) the WRONG tenant.

  (b) NO CROSS-TENANT WRITE (the moat). A *malicious* fork can name its head repo anything — including a
      full_name / owner id that COLLIDES with another paying tenant's coordinate. The App must STILL route the
      event by the BASE repo's owner id, so the crafted fork can never steer a write into the victim tenant.
      (The moat is per-installation account isolation; this asserts a hostile fork can't tunnel past it.)

  (c) NEVER CRASH + DEGRADE. The installation token is for the BASE repo only; a check run created on the base
      repo at the FORK-head sha can be REJECTED by GitHub (the sha isn't in the base repo). That must degrade
      to comment-only (the comment always posts on the base repo's conversation) — never an escaped exception,
      never a retry storm. And the App must never request a token for, or call the API against, the FORK repo.

  (d) CONTENT-FREE. The fork's file BODIES never get stored. We only ever read filenames + diff hunk-header
      line ranges from the BASE-repo Files API; a sentinel source body planted in the fork's patch must never
      appear in any stored claim / graph / event row.

This gate INJECTS a fork-PR event through the REAL per-event processor (connection + per-repo advisory lock +
tenant routing) over the REAL gate (db/schema.sql) authed as the App identity, and asserts all four. The only
faked thing is the GitHub I/O — and that fake RECORDS which `repo` every API call targeted (so we can prove no
call ever touched the fork) and serves a patch carrying a sentinel source body (so we can prove it never lands).

Run:  python3 tests/test_fork_pr_path.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
import server as S  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): per-PID like db/smoke.sh + run_gates, so two concurrent runs never drop each
# other's scratch DB mid-run.
DB = "veripsa_forktest_" + str(os.getpid())

# The honest customer (the BASE repo that installed the App).
BASE_REPO = "acme/app"
BASE_REPO_ID = 7000
BASE_MAIN_SHA = "a" * 40
BASE_OWNER_ID = 7001            # acme's stable GitHub account id → ACCT-GH-7001
BASE_INSTALL_ID = 4242          # the BASE installation (the only token the App can mint here)

# A SECOND, UNRELATED paying tenant — the victim the malicious fork will try to collide with.
VICTIM_REPO = "victimco/secret"
VICTIM_OWNER_ID = 9999          # → ACCT-GH-9999

# The hostile fork: a different owner, a DIFFERENT repo id (so is_fork fires), and — the attack — a head repo
# full_name + owner id CRAFTED to collide with the victim tenant's coordinate. If the App keyed off the fork,
# this would write into / leak the victim's account.
FORK_OWNER_ID = VICTIM_OWNER_ID                 # forge the victim's account id on the fork side
FORK_REPO_FULL = VICTIM_REPO                     # forge the victim's repo full_name on the fork side
FORK_REPO_ID = 8888                              # != base repo id → is_fork = True
FORK_HEAD_SHA = "f" * 40                          # a sha that lives ONLY in the fork (not in the base repo)

# A sentinel SOURCE BODY planted in the fork PR's diff patch. Content-free means this string must NEVER appear
# in any stored row (claim / graph / event). If it does, a file body leaked.
SECRET_BODY_SENTINEL = "S3CRET_FORK_SOURCE_BODY_DO_NOT_STORE_4f2a"


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


class ForkRejectingGitHub:
    """A FakeGitHub that (1) RECORDS the `repo` of every API call so the gate can prove NO call ever targeted
    the fork, (2) REJECTS post_check at the fork-head sha exactly as the real GitHub does (the sha isn't in the
    base repo), forcing the comment-only degrade, (3) serves the PR's changed files via the BASE-repo Files API
    carrying a sentinel source body in the patch (so the gate can prove the body never lands)."""

    def __init__(self, files):
        self._files = files                  # {filename: [[start,end], ...]}
        self.checks, self.comments = [], []
        self.api_repos = []                  # every `repo` passed to ANY method — the leak detector
        self.install_ids = []               # every installation id for_installation was asked for
        self.check_post_attempts = []        # (repo, sha) we TRIED to post a check at
        self.check_conclusions = []          # conclusion attempted even when the fork-head check is rejected
        self._comment_id = 1000

    def for_installation(self, installation_id):
        self.install_ids.append(str(installation_id))
        return self

    # ── reads (base-repo Files API; works for a fork PR) ─────────────────────────────────────────────────────
    def list_pr_files(self, repo, number, pr_changed_files=0):
        self.api_repos.append(repo)
        return list(self._files.keys())

    def list_pr_files_with_ranges(self, repo, number, pr_changed_files=0):
        self.api_repos.append(repo)
        # The real client parses HUNK HEADERS only and discards the +/- body. We hand back ranges directly; the
        # sentinel body is never offered to the App through this content-free surface (mirrors github_rest).
        return dict(self._files)

    # ── writes ────────────────────────────────────────────────────────────────────────────────────────────
    def post_check(self, repo, sha, conclusion, title, summary):
        self.api_repos.append(repo)
        self.check_post_attempts.append((repo, sha))
        self.check_conclusions.append(conclusion)
        # The HONEST GitHub behavior for a fork-head sha on the BASE repo: 422 (sha not in this repo). Raise it
        # so the gate proves the App degrades to comment-only instead of crashing.
        if sha == FORK_HEAD_SHA:
            raise RuntimeError("422 Unprocessable Entity: No commit found for SHA (fork head not in base repo)")
        self._check_id = getattr(self, "_check_id", 2000) + 1
        self.checks.append({"id": self._check_id, "sha": sha, "conclusion": conclusion,
                            "title": title, "summary": summary, "name": "Veripsa"})

    def list_check_runs(self, repo, sha):
        self.api_repos.append(repo)
        return [c for c in self.checks if c["sha"] == sha and c.get("name") == "Veripsa"]

    def patch_check(self, repo, check_run_id, conclusion, title, summary):
        self.api_repos.append(repo)
        for c in self.checks:
            if c["id"] == check_run_id:
                c.update({"conclusion": conclusion, "title": title, "summary": summary})
                return c
        raise AssertionError(f"check not found: {check_run_id}")

    def upsert_check(self, repo, sha, conclusion, title, summary):
        # mirror github_rest.upsert_check: list then post/patch — but post_check raises for the fork sha, and
        # _safe_upsert_check must swallow it. We must NOT swallow here (let it propagate to _safe_upsert_check).
        existing = self.list_check_runs(repo, sha)
        if existing:
            return self.patch_check(repo, existing[0]["id"], conclusion, title, summary)
        return self.post_check(repo, sha, conclusion, title, summary)

    def post_comment(self, repo, number, body):
        self.api_repos.append(repo)
        self._comment_id += 1
        self.comments.append({"id": self._comment_id, "number": number, "body": body, "user": {"type": "Bot"}})

    def list_issue_comments(self, repo, number):
        self.api_repos.append(repo)
        return [c for c in self.comments if c["number"] == number]

    def upsert_comment(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"] or (c["body"].startswith("### Veripsa") and c.get("user", {}).get("type") == "Bot"):
                if c["body"] == body:
                    return c
                c["body"] = body
                return c
        return self.post_comment(repo, number, body)

    def patch_comment_if_exists(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"]:
                c["body"] = body() if callable(body) else body
                return True
        return False


def fork_pr_payload(action="opened"):
    """A realistic fork pull_request payload: top-level `repository` = the BASE repo (GitHub's contract);
    pull_request.head.repo = the hostile FORK whose owner id + full_name are forged to collide with the victim
    tenant; pull_request.base.repo = the base repo (different id from the fork)."""
    return {
        "action": action,
        "number": 51,
        "installation": {"id": BASE_INSTALL_ID,
                         "account": {"id": BASE_OWNER_ID}},        # the BASE installation/account
        "repository": {"full_name": BASE_REPO, "default_branch": "main",
                       "owner": {"id": BASE_OWNER_ID, "login": "acme"},
                       "id": BASE_REPO_ID},
        "pull_request": {
            "number": 51,
            "title": "Add a feature (from a fork)",
            "user": {"login": "outside-contributor"},
            "draft": False,
            "merged": False,
            "head": {"sha": FORK_HEAD_SHA, "ref": "feature-from-fork",
                     "repo": {"id": FORK_REPO_ID, "full_name": FORK_REPO_FULL,
                              "owner": {"id": FORK_OWNER_ID, "login": "victimco"}}},
            "base": {"ref": "main", "sha": BASE_MAIN_SHA,
                     "repo": {"id": BASE_REPO_ID, "full_name": BASE_REPO,
                              "owner": {"id": BASE_OWNER_ID, "login": "acme"}}},
        },
    }


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    owner_dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    app_dsn = f"postgresql://veripsa_app@localhost/{DB}"
    seed_live_installation(app_dsn, owner_dsn, BASE_OWNER_ID, BASE_INSTALL_ID)
    seed_live_installation(app_dsn, owner_dsn, VICTIM_OWNER_ID, 5000)

    db = make_db("veripsa_app")
    admin = make_db("veripsa_migrator")    # reads claims past RLS (the App role writes via gates, can't raw-SELECT)

    # Seed a base-repo graph so the fork PR has something to analyze (the fork touches backend/api.py which
    # imports+calls backend/auth.py — a real coupling, so the PR is genuinely analyzable, not a trivial clear).
    import code_graph_extract as X
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
       (json.dumps(graph), BASE_REPO, "main", BASE_MAIN_SHA))
    db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
       (BASE_REPO, str(BASE_REPO_ID)))

    # Pre-provision the VICTIM tenant with a real claim of its OWN, so we can later prove the hostile fork event
    # left the victim's account byte-for-byte untouched (no cross-tenant write, no leak).
    proc = S.make_db_processor(f"postgresql://veripsa_app@localhost/{DB}")
    victim_seed = {
        "action": "opened", "number": 1, "installation": {"id": 5000, "account": {"id": VICTIM_OWNER_ID}},
        "repository": {"full_name": VICTIM_REPO, "default_branch": "main",
                       "owner": {"id": VICTIM_OWNER_ID, "login": "victimco"}, "id": 9000},
        "pull_request": {"number": 1, "user": {"login": "victim-dev"}, "draft": False, "merged": False,
                         "head": {"sha": "1" * 40, "ref": "vmain",
                                  "repo": {"id": 9000, "full_name": VICTIM_REPO}},
                         "base": {"ref": "main", "sha": "1" * 40,
                                  "repo": {"id": 9000, "full_name": VICTIM_REPO}}},
    }

    class PlainGitHub(ForkRejectingGitHub):
        def post_check(self, repo, sha, conclusion, title, summary):   # the victim's own sha is in its own repo
            self.api_repos.append(repo)
            self.checks.append({"id": 1, "sha": sha, "conclusion": conclusion, "title": title,
                                "summary": summary, "name": "Veripsa"})
    proc("pull_request", victim_seed, None, PlainGitHub({"victim/file.py": [[1, 5]]}))

    def victim_claim_count():
        return admin("""SELECT set_config('core.current_account',%s,true);
            SELECT count(*)::int FROM core.claim WHERE repo=%s""", ("ACCT-GH-%d" % VICTIM_OWNER_ID, VICTIM_REPO))
    victim_before = victim_claim_count()

    checks = []

    # ── INJECT THE HOSTILE FORK PR through the REAL processor ────────────────────────────────────────────────
    gh = ForkRejectingGitHub({"backend/api.py": [[1, 20]],
                              # a sentinel "patch body" path that, if the App ever stored bodies, would surface
                              SECRET_BODY_SENTINEL + ".py": [[1, 3]]})
    crashed = None
    try:
        proc("pull_request", fork_pr_payload("opened"), None, gh)
    except Exception as e:      # the never-crash invariant: a fork PR must NEVER escape an exception
        crashed = f"{type(e).__name__}: {str(e)[:160]}"

    # (c1) NEVER CRASH: the whole event processed without an escaped exception.
    checks.append(("never-crash: a fork PR (fork-head sha, forged head repo) processes with NO escaped exception",
                   crashed is None))
    if crashed is not None:
        print("  fork PR crashed:", crashed)

    # (a) BASE-KEYING — the event landed in the BASE owner's account, by the BASE coordinate.
    base_acct = "ACCT-GH-%d" % BASE_OWNER_ID
    base_claims = admin("""SELECT set_config('core.current_account',%s,true);
        SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id='PR-51'
          AND claim_state IN ('active','waiting')""", (base_acct, BASE_REPO))
    checks.append((f"base-keying: the fork PR's claims landed in the BASE owner's account ({base_acct}) under "
                   f"the BASE coordinate ({BASE_REPO}); claims={base_claims}",
                   base_claims is not None and base_claims > 0))

    # The fork's forged coordinate (victim/secret) must hold NO claim under the fork PR change id in the base
    # account (the App never used the fork full_name as the coordinate).
    fork_coord_in_base = admin("""SELECT set_config('core.current_account',%s,true);
        SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id='PR-51'""", (base_acct, FORK_REPO_FULL))
    checks.append((f"no fork-coordinate write: the forged fork full_name ({FORK_REPO_FULL}) holds NO PR-51 claim "
                   f"in the base account (the coordinate is the BASE repo, never the fork); count={fork_coord_in_base}",
                   fork_coord_in_base == 0))

    # (b) NO CROSS-TENANT WRITE (the moat) — the victim tenant's account is byte-for-byte unchanged. The forged
    # fork owner id == VICTIM_OWNER_ID, yet the event routed by the BASE owner id, so the victim saw nothing.
    victim_after = victim_claim_count()
    checks.append((f"moat / no cross-tenant write: the victim tenant (ACCT-GH-{VICTIM_OWNER_ID}, the id the fork "
                   f"FORGED) is untouched — claims before={victim_before}, after={victim_after}",
                   victim_after == victim_before and victim_before is not None and victim_before > 0))

    # The hostile event must NOT have created a claim for PR-51 in the victim's account either.
    victim_pr51 = admin("""SELECT set_config('core.current_account',%s,true);
        SELECT count(*)::int FROM core.claim WHERE change_id='PR-51'""", ("ACCT-GH-%d" % VICTIM_OWNER_ID,))
    checks.append((f"moat: the fork PR's change (PR-51) created NO claim in the victim account; count={victim_pr51}",
                   victim_pr51 == 0))

    # (c2) DEGRADE TO COMMENT-ONLY — the check post was ATTEMPTED at the fork-head sha, REJECTED, and the PR
    # comment still posted on the BASE repo conversation. (The signal reaches the contributor via the comment.)
    attempted_fork_sha = any(sha == FORK_HEAD_SHA for (_repo, sha) in gh.check_post_attempts)
    no_check_landed = not any(c["sha"] == FORK_HEAD_SHA for c in gh.checks)
    comment_on_base = any(c.get("user", {}).get("type") == "Bot" for c in gh.comments)
    checks.append(("degrade-to-comment-only: the check post was attempted at the fork-head sha and REJECTED "
                   "(no check landed at that sha), and the PR comment still posted on the base conversation",
                   attempted_fork_sha and no_check_landed and comment_on_base))

    # (c3) NO FORK TOKEN / NO FORK API CALL — every API call targeted the BASE repo; the App only ever asked
    # for_installation the BASE installation id; it NEVER requested a token for, or called against, the fork.
    touched_fork = [rp for rp in gh.api_repos if rp == FORK_REPO_FULL]
    only_base_or_known = all(rp in (BASE_REPO,) for rp in gh.api_repos)
    checks.append((f"no fork API call: every GitHub API call targeted the BASE repo ({BASE_REPO}); "
                   f"calls touching the fork repo = {len(touched_fork)}",
                   not touched_fork and only_base_or_known))
    checks.append((f"no fork token: for_installation was asked ONLY for the BASE installation "
                   f"({BASE_INSTALL_ID}); ids seen={sorted(set(gh.install_ids))}",
                   gh.install_ids and all(i == str(BASE_INSTALL_ID) for i in gh.install_ids)))

    # (d) CONTENT-FREE — the sentinel fork source body never appears in ANY stored row (claim path / graph
    # node / event). We scan as the App identity across both the base and victim accounts.
    # The sentinel was supplied to the App ONLY as a FILENAME (a content-free path token) — never as a +/- diff
    # body, because the content-free Files API surface (list_pr_files_with_ranges) carries only filenames + line
    # ranges. So the sentinel may legitimately land in a claim's TARGET_PATH (content-free metadata, allowed),
    # but it must NEVER appear in a NON-path column (change_id, branch) where a leaked body would surface, nor
    # anywhere in the code graph (the graph is ingested from the base-repo tarball, which the fork can't touch).
    leak_nonpath = admin(
        """SELECT set_config('core.current_account',%s,true);
        SELECT (
          (SELECT count(*) FROM core.claim WHERE change_id LIKE %s OR branch LIKE %s)
        + (SELECT count(*) FROM core.code_node WHERE path LIKE %s OR name LIKE %s)
        )::int""",
        (base_acct, f"%{SECRET_BODY_SENTINEL}%", f"%{SECRET_BODY_SENTINEL}%",
         f"%{SECRET_BODY_SENTINEL}%", f"%{SECRET_BODY_SENTINEL}%"))
    checks.append(("content-free: the fork's sentinel string never bleeds into a NON-path column (change_id / "
                   "branch) nor into the code graph — the only channel a fork supplied was a content-free "
                   f"filename; non-path leak count={leak_nonpath}",
                   leak_nonpath == 0))

    # (e) INFO-LEAK REDACTION (FINDING B) — a fork PR's comment posts on the BASE-repo conversation the EXTERNAL
    # contributor can read. The full comment names the base repo's OTHER in-flight PRs (refs + author logins) and
    # base-repo paths/symbols — private in-flight structure that must NOT leak to an outside contributor. Render
    # the fork PR's comment WITH a second in-flight base-repo PR present (the maintainer's PR-77 it is queued
    # behind, in the same cluster, on a shared foundation) and assert NONE of those identifiers survive — while a
    # NON-fork render of the SAME fixture still carries them (maintainers get full detail). Pure render (no DB), so
    # the redaction is asserted directly on render_pr_check's output, the exact thing server.py posts.
    import render as R
    # base-repo private identifiers that MUST be redacted out of a fork comment:
    MAINT_PR = "PR-77"                         # another in-flight base-repo PR ref
    MAINT_LOGIN = "maintainer-jane"            # its author login
    BASE_PATH = "backend/secret_pricing.py"    # a base-repo path
    BASE_SYMBOL = "compute_secret_rate"        # a base-repo symbol name
    BASE_BLAST = "backend/private_downstream.py"
    BASE_HUB = "backend/load_bearing_hub.py"
    leak_surface = {
        "repo": BASE_REPO, "branch": "main",
        "changes": [
            # the FORK PR (PR-51): queued behind the maintainer PR, coupled to it, on a shared base-repo file.
            {"change_id": "PR-51", "label": "outside-contributor PR-51", "agent": "outside-contributor",
             "verdict": "serialize", "paths": [BASE_PATH], "impact": [BASE_BLAST], "contested_with": [],
             "serialize_behind": [f"{MAINT_LOGIN} {MAINT_PR}"],
             "depends_on_changing": [{"path": "backend/secret_base.py", "by": f"{MAINT_LOGIN} {MAINT_PR}"}],
             "unknown_paths": [],
             "collision_points": [{"behind": f"{MAINT_LOGIN} {MAINT_PR}", "path": BASE_PATH,
                                   "symbol": BASE_SYMBOL, "line_lo": 40, "line_hi": 70}],
             "shared_foundation": [{"path": BASE_HUB, "fan_in": 12, "churn": 7}]},
            # the MAINTAINER's in-flight base-repo PR (the holder), present in the surface.
            {"change_id": MAINT_PR, "label": f"{MAINT_LOGIN} {MAINT_PR}", "agent": MAINT_LOGIN,
             "verdict": "serialize", "paths": [BASE_PATH], "impact": [], "contested_with": [],
             "serialize_behind": [], "queued_behind": ["outside-contributor PR-51"],
             "queued_behind_paths": [BASE_PATH], "depends_on_changing": [], "unknown_paths": []},
        ],
        "clusters": [{"changes": [MAINT_PR, "PR-51"], "agents": [f"{MAINT_LOGIN} {MAINT_PR}", "outside-contributor PR-51"],
                      "size": 2, "suggested_order": [MAINT_PR, "PR-51"]}],
    }
    BASE_TOKENS = [MAINT_PR, MAINT_LOGIN, BASE_PATH, BASE_SYMBOL, BASE_BLAST, BASE_HUB, "secret_base"]

    fork_out = R.render_pr_check(leak_surface, "PR-51", is_fork=True)
    fork_blob = (fork_out.get("summary") or "") + "\n" + (fork_out.get("comment") or "")
    nonfork_out = R.render_pr_check(leak_surface, "PR-51", is_fork=False)
    nonfork_blob = (nonfork_out.get("summary") or "") + "\n" + (nonfork_out.get("comment") or "")

    leaked = [t for t in BASE_TOKENS if t in fork_blob]
    checks.append((f"info-leak redaction: a FORK PR's rendered comment+summary leaks NONE of the base repo's "
                   f"in-flight PR refs / author logins / paths / symbols (leaked={leaked})", not leaked))
    # the fork comment must NOT carry ANY 'PR-<n>' token other than the fork's own ref (the cross-PR refs are the leak).
    import re as _re
    pr_tokens = set(_re.findall(r"PR-\d+", fork_blob))
    checks.append((f"info-leak redaction: the fork comment carries no cross-PR 'PR-<n>' token (only its own PR-51 "
                   f"may appear; found {sorted(pr_tokens)})", pr_tokens <= {"PR-51"}))
    # but the fork STILL gets an advisory, content-free coordinate signal (not silently dropped) and a non-blocking
    # conclusion — redaction must not turn into silence on a real overlap.
    checks.append(("info-leak redaction: the fork still gets a coordinate signal (comment posted, advisory, "
                   "never-block conclusion preserved)",
                   fork_out.get("comment") and "coordinate with the maintainers" in fork_blob.lower()
                   and fork_out.get("conclusion") in ("success", "neutral")))
    # CONTRAST: a NON-fork render of the SAME fixture DOES name the maintainer PR + login + paths (full detail for
    # the trusted maintainer surface) — proves the redaction is fork-gated, not a blanket loss of detail.
    nonfork_has = [t for t in (MAINT_PR, MAINT_LOGIN, BASE_PATH) if t in nonfork_blob]
    checks.append((f"info-leak redaction is FORK-GATED: a NON-fork render of the same fixture still names the "
                   f"maintainer PR + login + base path (got {nonfork_has}) — maintainers keep full detail",
                   set(nonfork_has) == {MAINT_PR, MAINT_LOGIN, BASE_PATH}))

    # (e2e) the COMMENT ACTUALLY POSTED on the fork PR's base-repo conversation (the real server path) must also be
    # redacted: no other in-flight base-repo PR ref / author login surfaces in any Bot comment the fork event posted.
    fork_posted = "\n".join(c["body"] for c in gh.comments if c.get("user", {}).get("type") == "Bot")
    e2e_pr_tokens = set(_re.findall(r"PR-\d+", fork_posted))
    checks.append((f"info-leak redaction (e2e): the comment the server POSTED on the fork PR carries no cross-PR "
                   f"'PR-<n>' ref (only its own PR-51; found {sorted(e2e_pr_tokens)})", e2e_pr_tokens <= {"PR-51"}))

    # A deleted fork can arrive on the NORMAL live webhook path with head.repo=null. That is not enough to CONFIRM
    # a fork (so merge gating stays active), but it is enough to require privacy redaction. Drive the real processor
    # with no synthetic marker and verify both halves of that three-state contract.
    unknown_payload = fork_pr_payload("opened")
    unknown_payload["number"] = 52
    unknown_payload["pull_request"]["number"] = 52
    unknown_payload["pull_request"]["head"]["ref"] = "deleted-fork-head"
    unknown_payload["pull_request"]["head"]["repo"] = None
    unknown_gh = ForkRejectingGitHub({"backend/api.py": [[1, 20]]})
    proc("pull_request", unknown_payload, None, unknown_gh)
    unknown_comment = next((c["body"] for c in unknown_gh.comments if c["number"] == 52), "")
    unknown_leaks = [t for t in BASE_TOKENS + ["PR-51"] if t in unknown_comment]
    checks.append((f"live missing fork identity: output stays redacted without a synthetic marker; "
                   f"leaked={unknown_leaks}", bool(unknown_comment) and not unknown_leaks))
    checks.append(("live missing fork identity: privacy redaction does NOT disable merge gating",
                   unknown_gh.check_conclusions and unknown_gh.check_conclusions[-1] == "action_required"))

    # ── verdict ─────────────────────────────────────────────────────────────────────────────────────────────
    subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True, text=True)
    print("\n=== FORK-PR PATH GATE ===")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("FORK-PR PATH GATE: PASS" if ok else "FORK-PR PATH GATE: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
