#!/usr/bin/env python3
"""COMMENT IDEMPOTENCY / ANTI-SPAM gate — the customer-facing surface under CHURN.

The PR check + comment IS the product's face. Spamming it (a duplicate comment, a check that flaps between
identical states, a stale "Wait in line" that lingers after its cause is gone, or any comment at all on a
clean-from-the-start PR) is the single fastest way a team learns to MUTE the bot. This gate drives a single PR
through an adversarial storm and asserts the four anti-spam invariants the product depends on:

  (a) EXACTLY ONE comment per PR, UPSERTED in place — never a duplicate / second comment, no matter how many
      synchronize bursts, force-pushes, reopen/draft toggles, or DUPLICATE (redelivered) webhooks arrive.
  (b) the check conclusion does not FLAP between identical states — re-rendering an UNCHANGED verdict rewrites
      the same body (a patch), it never oscillates the conclusion or the comment text under a no-op churn.
  (c) when a collision RESOLVES (the PR drops the contested path, or the other PR lands/withdraws), the stale
      "Wait in line" / "Heads up" comment is REWRITTEN to a cleared body — no lingering false warning.
  (d) a clean-from-the-start PR gets NO comment (the less-noise rule) — and a storm of churn on a clean PR
      never conjures one.

Drives github-app/server.handle_event with a FAKE GitHub client (records every post/patch, exactly like the
live App's I/O) over the REAL gate (db/schema.sql) authed as the App identity (veripsa_app, delegation).
Offline (no deploy, no GitHub account). Content-free.

Run:  python3 tests/test_comment_idempotency.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import hashlib
import json
import os
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import server as S  # noqa: E402
import webhook_handlers as WH  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): per-PID, exactly like test_server.py / db/smoke.sh — so several agents each
# running run_gates (or parallel CI shards) never drop each other's scratch DB mid-run.
DB = "veripsa_commentidem_" + str(os.getpid())
SHA = "a" * 40
# Each scenario uses its OWN repo coordinate so in-flight PRs from one scenario can never pollute another's
# neighborhood (a PR left in-flight on a shared repo would falsely couple with a later scenario's PR). This
# mirrors test_server.py's STALE_REPO / MEGA_REPO / LC_REPO isolation.
REPO_A = "acme/storm-sync"        # synchronize / force-push burst on a warned PR
REPO_B = "acme/storm-noflap"      # no check-flap between identical states
REPO_C1 = "acme/storm-neighbor"   # resolution via the coupled neighbor landing
REPO_C2 = "acme/storm-selfdrop"   # resolution via the warned PR dropping the path itself
REPO_D = "acme/storm-clean"       # a clean-from-the-start PR stays comment-free under churn
REPO_E = "acme/storm-redeliver"   # duplicate (redelivered) webhook
REPO_F = "acme/storm-decode"      # force-push that strips ALL code paths (docs-only) clears the stale warn


def _repo_id(repo):
    return 10_000 + int.from_bytes(hashlib.sha256(repo.encode("utf-8")).digest()[:6], "big")


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
    """Records what the App WOULD post, modelling GitHub's own dedup keys faithfully: a CHECK is keyed by
    (name, head sha) — a new commit (force-push / sync) legitimately gets its own check, exactly like GitHub;
    a COMMENT is keyed by PR number on the PR conversation — there must be EXACTLY ONE Veripsa comment per PR
    no matter how the head sha churns. `posts` / `comment_posts` count CREATE calls (POST), so a duplicate
    comment shows up as comment_posts>1 even if a later upsert would heal it."""

    def __init__(self, files_by_pr, open_prs=None):
        self.files_by_pr = files_by_pr
        self.open_prs = open_prs or []
        self.pull_requests = {}
        self.checks, self.comments = [], []
        self.check_patches = []
        self.patches = []
        self.comment_posts = 0          # COUNT of POST /comments (a CREATE — a second one is a duplicate)
        self.check_posts = 0            # COUNT of POST /check-runs (a CREATE)
        self.installations = []
        self._comment_id = 1000
        self._check_id = 2000

    def for_installation(self, installation_id):
        self.installations.append(str(installation_id))
        return self

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return self.files_by_pr.get(number, [])

    def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
        files = self.files_by_pr.get(number, [])
        return {"changed": list(files), "changed_ranges": {p: [] for p in files},
                "added_paths": [], "conflict_markers": [], "raw_entry_count": len(files)}

    def observe_pull_request(self, payload):
        pr = payload["pull_request"]
        number = payload["number"]
        self.pull_requests[number] = pr
        self.files_by_pr[f"head-{number}"] = pr["head"]["sha"]

    def get_pull_request(self, repo, number):
        return self.pull_requests[number]

    # ---- checks (keyed by head sha, like GitHub) ----
    def post_check(self, repo, sha, conclusion, title, summary):
        self._check_id += 1
        self.check_posts += 1
        self.checks.append({"id": self._check_id, "sha": sha, "conclusion": conclusion,
                            "title": title, "summary": summary, "name": "Veripsa"})

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

    # ---- comments (keyed by PR number, like GitHub) ----
    def post_comment(self, repo, number, body):
        self._comment_id += 1
        self.comment_posts += 1
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
        # the CURRENT head sha for this PR (set by the latest sync/force-push in the storm) — the refresh path
        # resolves a neighbor's head through this, so a force-push must be reflected here.
        return self.files_by_pr.get(f"head-{number}") or f"head-{number}"

    def repo_default_branch_head(self, repo):
        return "main", SHA

    def compare_changed_paths_strict(self, repo, base_sha, head_ref):
        return []

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


def pr_payload(gh, repo, action, number, author, head_sha=None, merged=False, draft=False):
    """A real GitHub pull_request webhook payload. `head_sha` lets the storm advance the head (a force-push /
    a new sync commit) so each event carries a distinct commit, exactly like a churning PR."""
    sha = head_sha or f"{number:040x}"
    repo_id = _repo_id(repo)
    payload = {"action": action, "number": number, "installation": {"id": 4242},
               "repository": {"full_name": repo, "default_branch": "main",
                              "owner": {"id": 777}, "id": repo_id},
               "pull_request": {"number": number,
                                "state": "closed" if action == "closed" else "open",
                                "changed_files": len(gh.files_by_pr.get(number, [])),
                                "base": {"ref": "main", "sha": SHA, "repo": {"id": repo_id}},
                                "head": {"sha": sha, "ref": f"feat/{number}", "repo": {"id": repo_id}},
                                "user": {"login": author}, "merged": merged, "draft": draft}}
    gh.observe_pull_request(payload)
    return payload


def comments_for(gh, number):
    return [c for c in gh.comments if c["number"] == number]


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    db = make_db("veripsa_app")
    sys.path.insert(0, ROOT)
    import code_graph_extract as X
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
    # ingest the SAME main graph under each scenario's own repo coordinate (isolated neighborhoods).
    for repo in (REPO_A, REPO_B, REPO_C1, REPO_C2, REPO_D, REPO_E, REPO_F):
        db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), repo, "main", SHA))
        db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
           (repo, str(_repo_id(repo))))
    # This gate isolates GitHub comment/check idempotency over an already-current graph. Durable graph
    # convergence has its own gates; keep structural PR churn from creating unrelated outbox work here.
    WH.request_main_graph_refresh_wake_only = lambda *args, **kwargs: {"queued": False, "current": True}

    checks = []

    # In the sample_app graph: backend/api.py imports+CALLS backend/auth.py (a real cross-file coupling). So a
    # PR on api.py is WARNED iff another in-flight PR touches auth.py — and CLEAR (no comment) when alone.

    # ====================================================================================================
    # SCENARIO A — SYNCHRONIZE / FORCE-PUSH STORM on a WARNED PR: exactly ONE comment, upserted in place.
    #   PR-1 (alice) edits backend/auth.py. PR-2 (bob) edits backend/api.py which CALLS+IMPORTS auth → a real
    #   coupling → PR-2 is WARNED (a comment). Now hammer PR-2 with a burst of synchronize + force-push events
    #   (each a NEW head sha). The single Veripsa comment must be UPSERTED every time — never a second comment.
    # ====================================================================================================
    gh = FakeGitHub({1: ["backend/auth.py"], 2: ["backend/api.py"]})
    S.handle_event("pull_request", pr_payload(gh, REPO_A, "opened", 1, "alice"), db, gh)
    S.handle_event("pull_request", pr_payload(gh, REPO_A, "opened", 2, "bob"), db, gh)

    c2 = comments_for(gh, 2)
    warned_at_open = len(c2) == 1 and "Heads up" in c2[0]["body"]
    # the number of CREATE-comment calls that landed specifically on PR-2 (must stay 1 through the storm):
    posts_on_pr2 = sum(1 for c in gh.comments if c["number"] == 2)

    # STORM: 12 synchronize/force-push events on PR-2, each a fresh head sha (a force-push storm).
    for i in range(12):
        S.handle_event("pull_request",
                       pr_payload(gh, REPO_A, "synchronize", 2, "bob", head_sha=f"{0xb0b00000 + i:040x}"), db, gh)
    c2_after = comments_for(gh, 2)
    checks.append((f"STORM-A: a 12-event synchronize/force-push burst on a WARNED PR keeps EXACTLY ONE comment, "
                   f"upserted in place — never a duplicate (warned_at_open={warned_at_open}, "
                   f"comments_on_PR2={len(c2_after)}, POSTs_on_PR2={posts_on_pr2})",
                   warned_at_open and len(c2_after) == 1 and posts_on_pr2 == 1
                   and "Heads up" in c2_after[0]["body"]))

    # ====================================================================================================
    # SCENARIO B — NO CHECK-FLAP between IDENTICAL states: re-rendering an UNCHANGED verdict rewrites the SAME
    #   conclusion + the SAME comment body (a patch), it never oscillates. The comment body after the storm is
    #   byte-identical to the body right after the warn first posted (the state never changed).
    # ====================================================================================================
    flap_gh = FakeGitHub({1: ["backend/auth.py"], 2: ["backend/api.py"]})
    S.handle_event("pull_request", pr_payload(flap_gh, REPO_B, "opened", 1, "alice"), db, flap_gh)
    S.handle_event("pull_request", pr_payload(flap_gh, REPO_B, "opened", 2, "bob"), db, flap_gh)
    first_body = comments_for(flap_gh, 2)[0]["body"]
    first_concl = [c["conclusion"] for c in flap_gh.checks if c["sha"] == f"{2:040x}"]
    bodies, concls = set(), set()
    for i in range(8):
        sha = f"{0xf1a00000 + i:040x}"
        S.handle_event("pull_request", pr_payload(flap_gh, REPO_B, "synchronize", 2, "bob", head_sha=sha), db, flap_gh)
        bodies.add(comments_for(flap_gh, 2)[0]["body"])
        # the check for THIS head sha (a sync posts a fresh check on the new commit, like GitHub):
        concls.update(c["conclusion"] for c in flap_gh.checks if c["sha"] == sha)
    # PAUSE-ACK (一時停止): PR-2 is a MATERIAL coupling (a warn WITH an in-flight counterpart), so the pause-ack
    # tier makes its check 'action_required' (paused until the coupling is acknowledged) rather than 'neutral'.
    # The INVARIANT this scenario guards is unchanged and still holds: across 8 no-op syncs the comment body is
    # byte-stable (1 distinct body) and the conclusion NEVER FLAPS — it just settles on 'action_required' instead
    # of 'neutral' (the content-free snapshot hash is identical every sync, so the ack-state never oscillates).
    checks.append((f"STORM-B: NO FLAP — across 8 no-op syncs the comment body is byte-stable (1 distinct "
                   f"body) and the check conclusion never oscillates (conclusions={sorted(concls) or first_concl})",
                   len(bodies) == 1 and first_body in bodies and concls.issubset({"action_required"})
                   and first_concl == ["action_required"]))

    # ====================================================================================================
    # SCENARIO C — RESOLUTION clears the stale warning (no lingering false warning).
    #   (c1) the OTHER PR LANDS: PR-1 (the auth.py foundation PR-2 is warned about) merges. PR-2's coupling is
    #        gone → PR-2's "Heads up" comment must be REWRITTEN to a cleared body (not left stale). No new comment.
    #   (c2) the warned PR DROPS the contested path itself: a warned PR synchronizes to a file whose partner is
    #        NOT in flight → its own stale "Wait in line"/"Heads up" is rewritten to cleared. No new comment.
    # ====================================================================================================
    # (c1) neighbor lands → PR-2's stale warn is cleared via the merge-refresh path.
    res_gh = FakeGitHub({1: ["backend/auth.py"], 2: ["backend/api.py"]})
    S.handle_event("pull_request", pr_payload(res_gh, REPO_C1, "opened", 1, "alice"), db, res_gh)
    S.handle_event("pull_request", pr_payload(res_gh, REPO_C1, "opened", 2, "bob"), db, res_gh)
    pr2_comment = comments_for(res_gh, 2)[0]
    pr2_check = next(c for c in res_gh.checks if c["sha"] == f"{2:040x}")
    pr2_warned = "Heads up" in pr2_comment["body"]
    body_before_land = pr2_comment["body"]
    comment_patches_before_land = res_gh.patches.count(pr2_comment["id"])
    check_patches_before_land = res_gh.check_patches.count(pr2_check["id"])
    posts_before_land = res_gh.comment_posts
    # PR-1 merges (lands auth.py). The webhook commits/rearms the durable convergence turn, but MUST perform
    # zero GitHub mutation on PR-2 while holding the event transaction/repo lock.
    land_result = S.handle_event(
        "pull_request", pr_payload(res_gh, REPO_C1, "closed", 1, "alice", merged=True), db, res_gh)
    pr2_after_event = comments_for(res_gh, 2)[0]
    event_deferred_without_mutation = (
        land_result.get("refreshed_inflight") == 0
        and land_result.get("refresh_deferred", 0) >= 1
        and pr2_after_event["body"] == body_before_land
        and res_gh.patches.count(pr2_comment["id"]) == comment_patches_before_land
        and res_gh.check_patches.count(pr2_check["id"]) == check_patches_before_land
    )
    checks.append((f"STORM-C1: the neighbor LAND webhook performs ZERO synchronous PR-2 GitHub mutation and "
                   f"defers the current surface (deferred={land_result.get('refresh_deferred')})",
                   event_deferred_without_mutation))

    # Explicitly emulate the isolated convergence posting slice through the same production seam used by the
    # durable graph worker. Only this off-webhook slice may rewrite PR-2's stale warning.
    worker_progress = S._post_refreshes(
        res_gh, REPO_C1, land_result.get("refreshed") or [], db=db, branch="main",
        return_progress=True,
    )
    c2_body = comments_for(res_gh, 2)[0]["body"]
    cleared = ("Heads up" not in c2_body and "Wait in line" not in c2_body)
    checks.append((f"STORM-C1: the isolated convergence slice rewrites the warned PR's stale 'Heads up' to "
                   f"a cleared body — no lingering false warning, no duplicate comment "
                   f"(warned={pr2_warned}, posted={worker_progress.get('posted')}, cleared_now={cleared}, "
                   f"comments={len(comments_for(res_gh,2))}, new_POSTs={res_gh.comment_posts - posts_before_land})",
                   pr2_warned and worker_progress.get("posted", 0) >= 1 and cleared
                   and len(comments_for(res_gh, 2)) == 1
                   and res_gh.comment_posts == posts_before_land))

    # (c2) the warned PR drops the contested path ITSELF → self-clear-reset rewrites its own stale comment.
    #   PR-11 (api.py) is warned by in-flight PR-10 (auth.py). PR-11 force-pushes to backend/repo.py — whose
    #   only coupling partner (reports.py) is NOT in flight → PR-11 is now clear → its own warn must self-clear.
    drop_gh = FakeGitHub({10: ["backend/auth.py"], 11: ["backend/api.py"]})
    S.handle_event("pull_request", pr_payload(drop_gh, REPO_C2, "opened", 10, "alice"), db, drop_gh)
    S.handle_event("pull_request", pr_payload(drop_gh, REPO_C2, "opened", 11, "bob"), db, drop_gh)
    pr11_warned = "Heads up" in comments_for(drop_gh, 11)[0]["body"]
    posts_before_drop = drop_gh.comment_posts
    drop_gh.files_by_pr[11] = ["backend/repo.py"]    # partner reports.py is NOT in flight → clear
    S.handle_event("pull_request", pr_payload(drop_gh, REPO_C2, "synchronize", 11, "bob", head_sha="d" * 40), db, drop_gh)
    c11_body = comments_for(drop_gh, 11)[0]["body"]
    self_cleared = ("Heads up" not in c11_body and "Wait in line" not in c11_body)
    checks.append((f"STORM-C2: when the warned PR DROPS the contested path itself (force-push), its OWN stale "
                   f"warn is rewritten to cleared — no lingering warning, no new comment "
                   f"(warned={pr11_warned}, self_cleared={self_cleared}, comments={len(comments_for(drop_gh,11))}, "
                   f"new_POSTs={drop_gh.comment_posts - posts_before_drop})",
                   pr11_warned and self_cleared and len(comments_for(drop_gh, 11)) == 1
                   and drop_gh.comment_posts == posts_before_drop))

    # ====================================================================================================
    # SCENARIO D — a CLEAN-FROM-THE-START PR gets NO comment, and a STORM of churn on it never conjures one.
    #   PR-20 (solo) edits backend/worker.py — its only coupling partner (billing.py) is NOT in flight → clear
    #   → a green CHECK, zero comments. Then hammer it with synchronize/force-push/reopen/draft-toggle churn.
    #   It must STAY comment-free (no spam) the whole way.
    # ====================================================================================================
    clean_gh = FakeGitHub({20: ["backend/worker.py"]})
    S.handle_event("pull_request", pr_payload(clean_gh, REPO_D, "opened", 20, "zoe"), db, clean_gh)
    clean_at_open = (len(comments_for(clean_gh, 20)) == 0 and clean_gh.comment_posts == 0
                     and any(c["sha"] == f"{20:040x}" for c in clean_gh.checks))   # got a check, no comment
    storm_events = [
        ("synchronize", {"head_sha": "20" + "a" * 38}),
        ("synchronize", {"head_sha": "20" + "b" * 38}),       # force-push storm
        ("converted_to_draft", {"draft": True}),
        ("ready_for_review", {"draft": False}),
        ("synchronize", {"head_sha": "20" + "c" * 38}),
        ("reopened", {}),                                      # close/reopen toggle
        ("synchronize", {"head_sha": "20" + "d" * 38}),
    ]
    for action, kw in storm_events:
        S.handle_event("pull_request", pr_payload(clean_gh, REPO_D, action, 20, "zoe", **kw), db, clean_gh)
    checks.append((f"STORM-D: a CLEAN-from-the-start PR gets a green CHECK and NEVER a comment — not at open and "
                   f"not through a sync/force-push/draft-toggle/reopen storm (no spam) "
                   f"(clean_at_open={clean_at_open}, comments={len(comments_for(clean_gh,20))}, "
                   f"comment_POSTs={clean_gh.comment_posts})",
                   clean_at_open and len(comments_for(clean_gh, 20)) == 0 and clean_gh.comment_posts == 0))

    # ====================================================================================================
    # SCENARIO E — DUPLICATE WEBHOOK (redelivery): GitHub delivers at-least-once. The SAME synchronize event
    #   delivered TWICE (identical payload, identical head sha) must NOT create a second comment — the upsert
    #   PATCHES the one comment in place. Proven on the warned PR (the comment-bearing case).
    # ====================================================================================================
    redeliver_gh = FakeGitHub({30: ["backend/auth.py"], 31: ["backend/api.py"]})
    S.handle_event("pull_request", pr_payload(redeliver_gh, REPO_E, "opened", 30, "alice"), db, redeliver_gh)
    S.handle_event("pull_request", pr_payload(redeliver_gh, REPO_E, "opened", 31, "bob"), db, redeliver_gh)
    posts_before_redeliver = redeliver_gh.comment_posts
    sync31 = pr_payload(redeliver_gh, REPO_E, "synchronize", 31, "bob", head_sha="3" * 40)
    S.handle_event("pull_request", sync31, db, redeliver_gh)        # delivery 1
    S.handle_event("pull_request", sync31, db, redeliver_gh)        # delivery 2 — EXACT redelivery (same payload)
    S.handle_event("pull_request", sync31, db, redeliver_gh)        # delivery 3 — and again
    checks.append((f"STORM-E: a synchronize webhook REDELIVERED 3× (at-least-once) keeps EXACTLY ONE comment, "
                   f"patched in place — no duplicate from redelivery "
                   f"(comments={len(comments_for(redeliver_gh,31))}, "
                   f"new_POSTs={redeliver_gh.comment_posts - posts_before_redeliver})",
                   len(comments_for(redeliver_gh, 31)) == 1
                   and redeliver_gh.comment_posts == posts_before_redeliver))

    # ====================================================================================================
    # SCENARIO F — FORCE-PUSH that strips ALL code paths (now docs-only) clears the stale warn.
    #   PR-41 (api.py) is warned by in-flight PR-40 (auth.py). PR-41 force-pushes to README.md only — no code
    #   path survives the code-filter → the engine reads the PR as 'clear' → its stale "Heads up" must be
    #   rewritten to cleared (no lingering false warning on a PR that no longer touches any code), no new comment.
    # ====================================================================================================
    decode_gh = FakeGitHub({40: ["backend/auth.py"], 41: ["backend/api.py"]})
    S.handle_event("pull_request", pr_payload(decode_gh, REPO_F, "opened", 40, "alice"), db, decode_gh)
    S.handle_event("pull_request", pr_payload(decode_gh, REPO_F, "opened", 41, "bob"), db, decode_gh)
    pr41_warned = "Heads up" in comments_for(decode_gh, 41)[0]["body"]
    posts_before_decode = decode_gh.comment_posts
    decode_gh.files_by_pr[41] = ["README.md", "docs/guide.md"]   # force-push: docs only, no code path survives
    S.handle_event("pull_request", pr_payload(decode_gh, REPO_F, "synchronize", 41, "bob", head_sha="f" * 40), db, decode_gh)
    c41_body = comments_for(decode_gh, 41)[0]["body"]
    decode_cleared = ("Heads up" not in c41_body and "Wait in line" not in c41_body)
    checks.append((f"STORM-F: a force-push that strips ALL code paths (now docs-only) clears the stale warn — no "
                   f"lingering warning, no new comment "
                   f"(warned={pr41_warned}, cleared={decode_cleared}, comments={len(comments_for(decode_gh,41))}, "
                   f"new_POSTs={decode_gh.comment_posts - posts_before_decode})",
                   pr41_warned and decode_cleared and len(comments_for(decode_gh, 41)) == 1
                   and decode_gh.comment_posts == posts_before_decode))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("COMMENT IDEMPOTENCY GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
