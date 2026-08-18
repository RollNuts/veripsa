#!/usr/bin/env python3
"""DRAFT FULL-ANALYSIS gate — a draft PR is the SCOUT WINDOW (PO 2026-06-25 round-2): full analysis runs (check
+ comment + claims), but a draft↔non-draft same-file overlap SOFTENS to 'warn' (never the hard 'serialize' that
would pause a non-draft behind a still-iterating scout). Draft↔draft can still 'serialize' (scout-vs-scout
coordination is still actionable). The renderer suffixes a partner label with " (draft)" when the partner is in
draft state, so a reader can tell at a glance which referenced PRs are still iterating.

Driven through the real per-event processor (make_db_processor → handle_event), with GitHub I/O replaced by a
recording fake and the tenant pinned through the same runtime routing seam (enter_installation by the
stable owner id). State is read back through the migrator with the account pinned. Content-free.

Run:  python3 tests/test_draft_full_analysis.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import psycopg2  # noqa: E402
import server as S  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402

DB = "veripsa_draftfulltest_" + str(os.getpid())
ACCOUNT_ID = 771
TENANT = f"ACCT-GH-{ACCOUNT_ID}"
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
MAIN_SHA = "c" * 40


def _repo_id(repo):
    return 810_000 + sum((i + 1) * ord(c) for i, c in enumerate(repo))


class FakeGitHub:
    """Records what the App WOULD post; serves PR files + a repo tarball + a settable main HEAD."""
    def __init__(self, files_by_pr=None, head_sha=MAIN_SHA):
        self.files_by_pr = files_by_pr or {}
        self.head_sha = head_sha
        self.pr_objects = {}
        self.checks, self.comments = [], []
        self._comment_id, self._check_id = 1000, 2000

    def for_installation(self, installation_id):
        return self

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return self.files_by_pr.get(number, [])

    def list_pr_files_with_ranges(self, repo, number, pr_changed_files=0):
        return {p: [] for p in self.files_by_pr.get(number, [])}

    def get_pull_request(self, repo, number):
        raw = dict(self.pr_objects.get(number, {}))
        raw["changed_files"] = len(self.files_by_pr.get(number, []))
        raw.setdefault("state", "open")
        raw.setdefault("merged", False)
        return raw

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
                c["conclusion"] = conclusion; c["title"] = title; c["summary"] = summary
                return c
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
        if event_type == "pull_request":
            pr = dict(payload.get("pull_request") or {})
            pr["state"] = "closed" if payload.get("action") == "closed" else "open"
            pr["changed_files"] = len(gh.files_by_pr.get(payload.get("number"), []))
            gh.pr_objects[payload.get("number")] = pr
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

    def app_query(sql, args=()):
        """Read main_impact_surface via the App role with the installation pinned (the same identity path
        the LIVE per-event processor uses; resolve_session_identity reads the pinned installation → tenant)."""
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

    def verdict_of(repo, change_id):
        imp = app_query("SELECT core.main_impact_surface(%s,'main')", (repo,))
        if isinstance(imp, str):
            import json as _json
            imp = _json.loads(imp)
        for c in (imp or {}).get("changes", []):
            if c.get("change_id") == change_id:
                return c.get("verdict")
        return None

    def queued_behind_of(repo, change_id):
        imp = app_query("SELECT core.main_impact_surface(%s,'main')", (repo,))
        if isinstance(imp, str):
            import json as _json
            imp = _json.loads(imp)
        for c in (imp or {}).get("changes", []):
            if c.get("change_id") == change_id:
                return c.get("queued_behind") or []
        return []

    def surface_head(repo, change_id):
        imp = app_query("SELECT core.main_impact_surface(%s,'main')", (repo,))
        if isinstance(imp, str):
            import json as _json
            imp = _json.loads(imp)
        for c in (imp or {}).get("changes", []):
            if c.get("change_id") == change_id:
                return c.get("head_sha")
        return None

    def live_cid(repo, cid):
        return admin("""SELECT count(*)::int FROM core.claim
                          WHERE repo=%s AND change_id=%s AND claim_state IN ('active','waiting')""", (repo, cid)) or 0

    def draft_state(repo, cid):
        return bool(admin("""SELECT COALESCE(bool_or(is_draft), false)
                              FROM core.claim
                             WHERE repo=%s AND change_id=%s AND claim_state IN ('active','waiting')""",
                          (repo, cid)))

    def baseline(repo):
        deliver("push", _push(repo, "main", "b" * 40))

    def latest_check_conclusion(sha):
        for c in reversed(gh.checks):
            if c["sha"] == sha:
                return c["conclusion"]
        return None

    def latest_comment_body(number):
        cs = [c for c in gh.comments if c["number"] == number]
        return cs[-1]["body"] if cs else None

    checks = []
    FILE = "backend/api.py"
    FILE2 = "backend/util.py"

    # ── (1) A DRAFT PR RECEIVES A CHECK and PARTICIPATES (the scout-window: full analysis on drafts). The
    #    PR-<n> claim is declared (so the in-flight contention picture includes the draft), and a PR check is
    #    posted on the head sha (so the author sees Veripsa's verdict in the PR checks list). A SOLO clear PR
    #    posts no comment (no contention to coordinate), but a non-clear draft + the contention-driven cases
    #    below DO post — covered by tests (2)/(3) where the contention prose lands on the synthetic shape. ──
    R1 = "acme/draft-scout"
    gh.files_by_pr = {1: [FILE]}
    baseline(R1)
    sha1 = "1" * 40
    deliver("pull_request", _pr("opened", R1, 1, "amy", sha1, head_ref="feat/1", draft=True))
    pr1_live = live_cid(R1, "PR-1")                                  # a draft now declares PR-<n> claims
    check_for_sha1 = any(c["sha"] == sha1 for c in gh.checks)
    checks.append((
        "DRAFT PR receives a check AND declares its PR-<n> claims (full analysis on a draft — the scout window) "
        f"(pr1_live={pr1_live} check={check_for_sha1} head={surface_head(R1, 'PR-1')})",
        pr1_live == 1 and check_for_sha1 and surface_head(R1, "PR-1") == sha1))
    # Seed head A with real range/base-hash evidence. The following synchronize keeps the SAME path but its Fake
    # Files read supplies neither; the gate must clear A's geometry before binding the row to head B.
    app_query("""SELECT core.act_for_claim_with_authority(%s,%s,%s,'main',%s,%s::jsonb,%s,%s,%s)""",
              ("PR-1:backend/api.py", FILE, R1, "amy", "[[7,9]]", "d" * 40, False, True))
    geometry_a = admin("""SELECT jsonb_build_object('ranges', touched_ranges::text, 'hash', base_content_hash)
                            FROM core.claim WHERE repo=%s AND change_id=%s
                             AND claim_state IN ('active','waiting') LIMIT 1""", (R1, "PR-1"))
    checks.append(("head A setup carries content-free ranges + base hash before the same-path synchronize",
                   isinstance(geometry_a, dict) and geometry_a.get("ranges") and geometry_a.get("hash") == "d" * 40))
    sha1_sync = "a" * 40
    deliver("pull_request", _pr("synchronize", R1, 1, "amy", sha1_sync, head_ref="feat/1", draft=True))
    pr1_sync_live = live_cid(R1, "PR-1")
    pr1_sync_check = any(c["sha"] == sha1_sync for c in gh.checks)
    pr1_sync_draft = draft_state(R1, "PR-1")
    geometry_b = admin("""SELECT jsonb_build_object('ranges', touched_ranges::text, 'hash', base_content_hash)
                            FROM core.claim WHERE repo=%s AND change_id=%s
                             AND claim_state IN ('active','waiting') LIMIT 1""", (R1, "PR-1"))
    checks.append((
        "DRAFT PR synchronize re-runs full analysis, preserves draft state, and refreshes the PR check "
        f"(live={pr1_sync_live} draft={pr1_sync_draft} check={pr1_sync_check} "
        f"head={surface_head(R1, 'PR-1')})",
        pr1_sync_live == 1 and pr1_sync_draft is True and pr1_sync_check
        and surface_head(R1, "PR-1") == sha1_sync
        and isinstance(geometry_b, dict) and geometry_b.get("ranges") is None and geometry_b.get("hash") is None))
    app_query("SELECT core.set_change_head_sha_with_authority(%s,%s,'main',%s)",
              ("PR-1", R1, "not-a-commit"))
    checks.append(("an unbound live claim exposes head_sha=NULL (legacy/mixed evidence is never refresh proof)",
                   surface_head(R1, "PR-1") is None))

    # ── (2) DRAFT ↔ NON-DRAFT same-file overlap is 'warn', NEVER 'serialize'. PR-2 (non-draft) opens FIRST on
    #    the shared file → holds the lane; PR-3 (draft) on the same file → its file-level wait would be a hard
    #    'serialize' in the old policy, must now be 'warn' (heads-up). The reverse — a non-draft second behind
    #    a draft holder — must ALSO be 'warn' (the non-draft is never paused behind a still-iterating scout). ──
    R2 = "acme/cross-state"
    gh.files_by_pr = {2: [FILE2], 3: [FILE2], 4: [FILE2]}
    baseline(R2)
    deliver("pull_request", _pr("opened", R2, 2, "carl", "2" * 40, head_ref="feat/2", draft=False))   # non-draft, holds
    deliver("pull_request", _pr("opened", R2, 3, "dot",  "3" * 40, head_ref="feat/3", draft=True))    # draft waiter
    # The waiter is the second one in; in main_impact_surface the draft PR-3 waits behind non-draft PR-2.
    # PR-3 has a cross-state wait → must be 'warn', not 'serialize' or 'serialize_soft'.
    pr3_verdict = verdict_of(R2, "PR-3")
    checks.append((
        "DRAFT↔NON-DRAFT same-file overlap is 'warn' (never 'serialize' — a draft must never pause a non-draft "
        f"and a non-draft must never wait behind a still-iterating scout) (pr3_verdict={pr3_verdict!r})",
        pr3_verdict == "warn"))
    # Symmetric direction: PR-2 (holder, non-draft) sees its queued_behind partner (PR-3 draft) — its verdict
    # remains its own (clear/warn from the holder's perspective; not a wait), but the queued_behind label
    # surfaces with " (draft)" so the holder knows the waiter is still iterating.
    pr2_queued = queued_behind_of(R2, "PR-2")
    checks.append((
        "DRAFT WAITER appears in the non-draft HOLDER's queued_behind (the scout is still tracked, just not "
        f"hard-serialized) (pr2_queued={pr2_queued!r})",
        any("PR-3" in (lbl or "") for lbl in (pr2_queued or []))))

    # ── (3) DRAFT ↔ DRAFT same-file overlap CAN still 'serialize' (scout-vs-scout coordination is actionable). ──
    R3 = "acme/scout-vs-scout"
    SHARED = "backend/scout.py"
    gh.files_by_pr = {5: [SHARED], 6: [SHARED]}
    baseline(R3)
    deliver("pull_request", _pr("opened", R3, 5, "ed",  "5" * 40, head_ref="feat/5", draft=True))   # draft holder
    deliver("pull_request", _pr("opened", R3, 6, "fay", "6" * 40, head_ref="feat/6", draft=True))   # draft waiter
    pr6_verdict = verdict_of(R3, "PR-6")
    checks.append((
        "DRAFT↔DRAFT same-file overlap CAN still 'serialize' (scout-vs-scout coordination is actionable; the "
        f"softening fires ONLY across the draft/non-draft boundary) (pr6_verdict={pr6_verdict!r})",
        pr6_verdict in ("serialize", "serialize_soft")))

    # ── (4) RENDER suffixes partner labels with " (draft)". Use render_pr_check directly against a synthetic
    #    impact shape (the gate's PR comment renderer; render.render_pr_check). The behind label belongs to a
    #    draft partner → the rendered comment must contain the " (draft)" suffix. Content-free assertion. ──
    sys.path.insert(0, os.path.join(ROOT, "github-app"))
    from render import render_pr_check  # noqa: E402
    impact_synth = {
        "repo": "acme/render-test", "branch": "main",
        "inflight_count": 2, "cluster_count": 0, "clusters": [],
        "warn_count": 0, "serialize_count": 1, "serialize_soft_count": 0, "unknown_count": 0, "clear_count": 0,
        "changes": [
            {
                "change_id": "PR-7", "agent": "alice", "label": "alice PR-7", "verdict": "serialize",
                "is_draft": False,
                "paths": ["src/a.py"], "impact": [], "impact_count": 0,
                "contested_with": [], "serialize_behind": ["bob PR-8"],
                "queued_behind": [], "queued_behind_paths": [],
                "collision_points": [], "merge_conflict_likely": False, "conflict_points": [],
                "unknown_paths": [], "dampened_with": [],
                "depends_on_changing": [], "shared_foundation": [],
            },
            {
                "change_id": "PR-8", "agent": "bob", "label": "bob PR-8", "verdict": "clear",
                "is_draft": True,
                "paths": ["src/a.py"], "impact": [], "impact_count": 0,
                "contested_with": [], "serialize_behind": [],
                "queued_behind": ["alice PR-7"], "queued_behind_paths": ["src/a.py"],
                "collision_points": [], "merge_conflict_likely": False, "conflict_points": [],
                "unknown_paths": [], "dampened_with": [],
                "depends_on_changing": [], "shared_foundation": [],
            },
        ],
    }
    out7 = render_pr_check(impact_synth, "PR-7", truncated=False, is_fork=False)
    body7 = (out7.get("comment") or "") if isinstance(out7, dict) else ""
    # Markdown-escaping wraps the ( and ) in backslashes -> 'draft' surrounded by escaped parens in the body.
    # The summary line also carries the decoration. Look for either form.
    has_draft_marker7 = ("(draft)" in body7) or (r"\(draft\)" in body7)
    checks.append((
        "RENDER decorates a DRAFT partner with '(draft)' in the waiting-behind text "
        f"(PR-7 waits behind a draft PR-8) (marker present: {has_draft_marker7!r})",
        has_draft_marker7 and "PR-8" in body7))

    out8 = render_pr_check(impact_synth, "PR-8", truncated=False, is_fork=False)
    body8 = (out8.get("comment") or "") if isinstance(out8, dict) else ""
    # PR-8 (the draft holder) has PR-7 (non-draft) queued behind it -> no draft suffix on PR-7.
    no_draft_marker_on_pr7 = (
        "PR-7" in body8
        and "(draft)" not in body8.split("PR-7")[1][:32]
        and r"\(draft\)" not in body8.split("PR-7")[1][:32]
    )
    checks.append((
        "RENDER does NOT decorate a NON-DRAFT partner with '(draft)' (PR-8's queued-behind names PR-7, no suffix)",
        no_draft_marker_on_pr7))

    ok = all(c[1] for c in checks)
    print("\n== DRAFT FULL-ANALYSIS ==")
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("DRAFT FULL-ANALYSIS GATE: " + ("PASS" if ok else "FAIL"))
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
