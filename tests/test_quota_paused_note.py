#!/usr/bin/env python3
"""FREE-TIER WALL VISIBILITY GATE — the SILENT-PAYWALL fix, driven through the LIVE per-event path.

THE CONVERSION-MOMENT DEATH it fixes: when an account crosses the free-tier wall, the DB gate refuses further
writes (db/schema/30_gate.sql quota path) and the graph silently goes stale. A bare comment in server.py once
CLAIMED 'the user-facing note is posted on the PR path' — but it was UNIMPLEMENTED: the wall was a SILENT break
(the bot just quietly stops, which reads as 'broke'), never a visible fair-use prompt. This gate proves the wall
is now a VISIBLE CONVERSION event: when a PR is analyzed while the account is over the line, Veripsa posts an
ADVISORY, content-free, jargon-clean early-access-limit check + comment, IN PLACE of the
now-stale verdict — idempotently (no flap), once.

How the wall is exercised (the REAL gate, no schema edit): bootstrap, ingest main's graph UNDER the line, then
tighten free_max_graph_units to 0 via the owner setter so the account is now OVER the line (the identical lever
test_marketplace_billing uses). main's HEAD then MOVES — the next ingest is REFUSED (quota_exceeded), so the
stored graph goes STALE behind HEAD. When a PR opens, self_heal_main_graph tries to re-ingest main, hits the
wall (reason='free-tier limit'), and the PR path posts the fair-use note instead of a stale verdict.

  QUOTA-1  the over-line PR posts the ADVISORY fair-use note: a `neutral` check (NEVER blocking/failing) titled
           for the early-access limit + a PR comment saying reduce scope/contact support. The limit is now VISIBLE.
  QUOTA-2  the note is CONTENT-FREE + JARGON-CLEAN (same denylist + overclaim scanners every surface holds —
           no path/symbol/count that names the account's code, no internal role/function/DB token, no overclaim).
  QUOTA-3  IDEMPOTENT: a re-delivered/synchronize event PATCHES the note in place — never a SECOND comment, no
           flap (the same upsert the normal verdict uses).
  QUOTA-4  the verdict is WITHHELD: over the wall the graph is stale, so Veripsa does NOT post a confident
           'clear'/'warn' — only the honest paused note (the result carries quota_paused=True).

Run:  python3 tests/test_quota_paused_note.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import psycopg2  # noqa: E402
import server as S  # noqa: E402
import policy_refresh_queue as PR  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402
# REUSE the customer-surface jargon + overclaim scanners (one source of truth — never a second copy):
from test_no_jargon_leak import _scan, _scan_overclaim, _customer_strings  # noqa: E402

DB = "veripsa_quota_note_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
DSN_MIG = f"postgresql://veripsa_migrator@localhost/{DB}"

INSTALL_ID = 7373
ACCOUNT_ID = 818181
REPO_ID = 919191
REPO = "payco/app"
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")

SHA_UNDER = "1111aaaa" * 5          # the under-line ingest (40 hex) — stored as main's baseline graph
SHA_MOVED = "2222bbbb" * 5          # main HEAD MOVES here AFTER the wall — the next ingest is refused


class QuotaGitHub:
    """A recording fake. Serves the sample_app fixture so the under-line ingest builds a REAL graph; after the
    wall it reports a MOVED HEAD (SHA_MOVED) so the stored graph is BEHIND HEAD and the PR's self-heal tries to
    re-ingest (and gets walled). Counts post vs patch so the no-double-post invariant is observable."""

    def __init__(self):
        self.checks, self.comments = [], []
        self.post_comment_calls, self.patch_comment_calls = 0, 0
        self.post_check_calls, self.patch_check_calls = 0, 0
        self.head_sha = SHA_UNDER
        self._cid, self._chid = 1000, 2000

    def for_installation(self, installation_id):
        return self

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return ["app/core.py"]

    def list_pr_files_with_ranges(self, repo, number, pr_changed_files=0):
        return {"app/core.py": []}

    def base_blob_shas(self, repo, base_sha, paths):
        return {}

    def post_check(self, repo, sha, conclusion, title, summary):
        self.post_check_calls += 1
        self._chid += 1
        check = {"id": self._chid, "sha": sha, "conclusion": conclusion,
                 "title": title, "summary": summary, "name": "Veripsa"}
        self.checks.append(check)
        return check

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha and c.get("name") == "Veripsa"]

    def patch_check(self, repo, check_run_id, conclusion, title, summary):
        self.patch_check_calls += 1
        for c in self.checks:
            if c["id"] == check_run_id:
                c.update({"conclusion": conclusion, "title": title, "summary": summary})
                return c
        raise AssertionError(f"check not found: {check_run_id}")

    def upsert_check(self, repo, sha, conclusion, title, summary):
        existing = self.list_check_runs(repo, sha)
        if existing:
            return self.patch_check(repo, existing[0]["id"], conclusion, title, summary)
        return self.post_check(repo, sha, conclusion, title, summary)

    def post_comment(self, repo, number, body):
        self.post_comment_calls += 1
        self._cid += 1
        comment = {"id": self._cid, "number": number, "body": body, "user": {"type": "Bot"}}
        self.comments.append(comment)
        return comment

    def list_issue_comments(self, repo, number):
        return [c for c in self.comments if c["number"] == number]

    def patch_comment(self, repo, comment_id, body):
        self.patch_comment_calls += 1
        for c in self.comments:
            if c["id"] == comment_id:
                c["body"] = body
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
        return []

    def repo_default_branch_head(self, repo):
        return "main", self.head_sha

    def repo_current_identity(self, repo):
        return {"id": REPO_ID, "full_name": REPO, "owner_id": ACCOUNT_ID}

    def pull_request_head(self, repo, number):
        return "f" * 40

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(FIXTURE, arcname="payco-" + sha[:7])
        return buf.getvalue()

    def get_file_at(self, repo, path, ref):
        full = os.path.join(FIXTURE, path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()


def _push_main(sha):
    return {"ref": "refs/heads/main", "after": sha,
            "installation": {"id": INSTALL_ID, "account": {"id": ACCOUNT_ID}},
            "repository": {"id": REPO_ID, "full_name": REPO, "default_branch": "main",
                           "owner": {"id": ACCOUNT_ID}},
            "pusher": {"name": "dev"},
            "commits": [{"added": ["app/core.py"], "modified": [], "removed": []}]}


def _pr_opened(number, head_sha):
    return {"action": "opened", "number": number,
            "installation": {"id": INSTALL_ID, "account": {"id": ACCOUNT_ID}},
            "repository": {"id": REPO_ID, "full_name": REPO, "default_branch": "main",
                           "owner": {"id": ACCOUNT_ID}},
            "pull_request": {"base": {"ref": "main", "sha": SHA_MOVED,
                                      "repo": {"id": REPO_ID, "full_name": REPO}},
                             "head": {"sha": head_sha, "repo": {"id": REPO_ID, "full_name": REPO},
                                      "ref": f"feature/{number}"},
                             "user": {"login": "dev"}, "merged": False}}


def _pr_sync(number, head_sha):
    p = _pr_opened(number, head_sha)
    p["action"] = "synchronize"
    return p


def _set_free_line(key, value):
    subprocess.run(["psql", DSN_MIG, "-v", "ON_ERROR_STOP=1", "-tAc",
                    f"SET search_path=core; SELECT core.set_free_line_with_authority('{key}',{value});"],
                   capture_output=True, text=True)


def _set_plan_graph_units(plan, value):
    # graph_units is now a PER-PLAN HARD line (core._plan_graph_units_limit, NOT _free_line) — tighten it via the
    # per-plan setter so the wall bites for this (free-plan) account in the gate.
    subprocess.run(["psql", DSN_MIG, "-v", "ON_ERROR_STOP=1", "-tAc",
                    f"SET search_path=core; SELECT core.set_plan_graph_units_limit_with_authority('{plan}',{value});"],
                   capture_output=True, text=True)


def _graph_queue_state():
    """Exact queue/lease snapshot proving a PR wake cannot supersede quota-deferred work."""
    conn = psycopg2.connect(DSN_MIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('core.current_account', %s, true)",
                        (f"ACCT-GH-{ACCOUNT_ID}",))
            cur.execute(
                "SELECT jsonb_build_object("
                "'repo',q.repo,'branch',q.branch,'target_sha',q.target_sha,"
                "'request_epoch',q.policy_epoch,'not_before',q.not_before,"
                "'terminal_reason',q.terminal_reason,'claimed_at',q.claimed_at,"
                "'claimed_by',q.claimed_by,'done_at',q.done_at,"
                "'leases',COALESCE(("
                " SELECT jsonb_agg(jsonb_build_object("
                "  'slot',l.slot,'lease_epoch',l.lease_epoch,"
                "  'request_epoch',l.request_epoch,'target_sha',l.target_sha)"
                "  ORDER BY l.slot)"
                " FROM core.graph_convergence_lease l"
                " WHERE l.account_id=q.account_id"
                "   AND l.repository_id=q.repository_id),'[]'::jsonb))"
                " FROM core.policy_refresh_outbox q"
                " WHERE q.account_id=%s AND q.request_kind='graph'"
                " AND q.repository_id=%s AND q.branch='main' AND q.done_at IS NULL",
                (f"ACCT-GH-{ACCOUNT_ID}", str(REPO_ID)),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    seed_live_installation(DSN_APP, DSN_MIG, ACCOUNT_ID, INSTALL_ID)

    gh = QuotaGitHub()
    proc = S.make_db_processor(DSN_APP)

    def deliver(event_type, payload, *, converge=False):
        result = proc(event_type, payload, None, gh)
        if converge:
            drained = PR._drain_policy_refreshes(
                PR.PolicyRefreshStore(DSN_APP), gh, DSN_APP, limit=20,
                graph_refresh_strict=S.converge_main_graph_strict)
            assert (drained.get("graph_drained", 0) >= 1
                    or drained.get("quota_deferred", 0) >= 1), f"graph convergence failed: {drained!r}"
        return result

    checks = []

    # ── SETUP: ingest main's graph UNDER the free line (a real baseline graph), then move the wall in. ────────
    deliver("push", _push_main(SHA_UNDER), converge=True)     # stored graph = SHA_UNDER, account UNDER the line
    # tighten the wall: the free plan's graph_units line = 0 → the account (now holding a graph) is OVER the line →
    # the next ingest is refused. (graph_units is the PER-PLAN HARD line now, set via the per-plan setter.)
    _set_plan_graph_units("free", 0)
    gh.head_sha = SHA_MOVED                                   # main HEAD MOVED → the stored graph is now BEHIND HEAD

    # ── QUOTA-1..4: open a PR while OVER the line. self_heal tries to re-ingest @SHA_MOVED → walled (free-tier
    #    limit) → the PR path posts the advisory fair-use note instead of a stale verdict. ─────────────────────
    deliver("pull_request", _pr_opened(11, "f" * 40), converge=True)
    pr_checks = gh.list_check_runs(REPO, "f" * 40)
    pr_comments = gh.list_issue_comments(REPO, 11)
    note_check = pr_checks[0] if pr_checks else None
    note_comment = pr_comments[0]["body"] if pr_comments else None
    blob = ((note_check["summary"] if note_check else "") + " " + (note_comment or "")).lower()

    checks.append(("QUOTA-1a over-line PR posts the fair-use note as an ADVISORY check (exactly one, conclusion "
                   "'neutral' — never a blocking/failing value)",
                   len(pr_checks) == 1 and note_check is not None and note_check["conclusion"] == "neutral"))
    checks.append(("QUOTA-1b the note is a real fair-use prompt: it says early-access limit and how to get more room "
                   "(not a silent stop)",
                   note_comment is not None and "early-access" in blob and "contact support" in blob and "paused" in blob
                   and "upgrade" not in blob))

    # QUOTA-2: content-free + jargon-clean + no overclaim (the same contract every customer surface holds).
    note_out = {"title": note_check["title"] if note_check else "",
                "summary": note_check["summary"] if note_check else "", "comment": note_comment}
    leaks, overclaims = [], []
    for label, text in _customer_strings(note_out):
        leaks += _scan(f"quota/{label}", text)
        overclaims += _scan_overclaim(f"quota/{label}", text)
    checks.append(("QUOTA-2 the paused note is content-free + jargon-clean + overclaim-free (same denylist + "
                   "overclaim scanners as every surface)", not leaks and not overclaims))
    for v in leaks + overclaims:
        print("  LEAK:", v)

    # QUOTA-3: IDEMPOTENT — a synchronize (re-delivery) PATCHES the same comment in place, never a SECOND post.
    posts_before, patches_before = gh.post_comment_calls, gh.patch_comment_calls
    check_posts_before = gh.post_check_calls
    comments_before_sync = gh.list_issue_comments(REPO, 11)
    checks_before_sync = gh.list_check_runs(REPO, "f" * 40)
    comment_before_sync = dict(comments_before_sync[0]) if comments_before_sync else None
    check_before_sync = dict(checks_before_sync[0]) if checks_before_sync else None
    queue_before_sync = _graph_queue_state()
    deliver("pull_request", _pr_sync(11, "f" * 40))
    queue_after_sync = _graph_queue_state()
    comments_after_sync = gh.list_issue_comments(REPO, 11)
    pr_checks = gh.list_check_runs(REPO, "f" * 40)
    note_check = pr_checks[0] if pr_checks else None
    queue_invariants = {
        key: (
            isinstance(queue_before_sync, dict)
            and isinstance(queue_after_sync, dict)
            and key in queue_before_sync
            and key in queue_after_sync
            and queue_before_sync[key] == queue_after_sync[key]
        )
        for key in ("target_sha", "request_epoch", "not_before", "terminal_reason", "leases")
    }
    durable_quota_preserved = (
        queue_invariants["terminal_reason"]
        and queue_before_sync["terminal_reason"] == "quota_paused"
        and queue_after_sync["terminal_reason"] == "quota_paused"
    )
    same_surface_ids = (
        isinstance(comment_before_sync, dict)
        and isinstance(check_before_sync, dict)
        and len(comments_after_sync) == len(comments_before_sync) == 1
        and len(pr_checks) == len(checks_before_sync) == 1
        and comments_after_sync[0].get("id") == comment_before_sync.get("id")
        and pr_checks[0].get("id") == check_before_sync.get("id")
    )
    paused_surface_preserved = (
        same_surface_ids
        and comments_after_sync[0].get("body") == comment_before_sync.get("body")
        and pr_checks[0].get("title") == check_before_sync.get("title")
        and pr_checks[0].get("summary") == check_before_sync.get("summary")
        and "early-access" in (comments_after_sync[0].get("body") or "").lower()
        and "paused" in (comments_after_sync[0].get("body") or "").lower()
    )
    checks.append(("QUOTA-3 the paused note is IDEMPOTENT: a re-delivered/synchronize event PATCHES in place "
                   "(comment count stays flat — no second post, no flap)",
                   same_surface_ids
                   and paused_surface_preserved
                   and gh.post_comment_calls == posts_before
                   and gh.post_check_calls == check_posts_before
                   and gh.patch_comment_calls > patches_before))
    checks.append((f"QUOTA-3b synchronize reads the durable quota state without changing the unfinished graph "
                   f"target/epoch/lease/not-before/terminal-reason ({queue_invariants})",
                   all(queue_invariants.values()) and durable_quota_preserved))

    # QUOTA-4: the VERDICT is WITHHELD — over the wall the graph is stale, so no confident clear/warn is shown;
    #          only the honest paused note. The check title is the free-tier note, never a normal verdict title.
    checks.append(("QUOTA-4 the verdict is WITHHELD over the wall: the check is the free-tier paused note "
                   "(never a confident 'Clear'/'Heads up'/'Wait in line' over a stale graph)",
                   bool(note_check) and "early-access limit" in (note_check["title"] or "").lower()))

    print("\n── FREE-TIER WALL VISIBILITY ──────────────────────────────")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("\nQUOTA PAUSED NOTE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
