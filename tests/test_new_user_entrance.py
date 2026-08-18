#!/usr/bin/env python3
"""NEW-USER ENTRANCE GATE — the brand-new customer's FIRST RUN, driven through the LIVE per-event path.

Every other gate either starts WITH a graph (test_lifecycle_e2e ingests the fixture in STEP 1, then opens
PRs against it) or feeds the renderer synthetic inputs (test_no_jargon_leak). NONE of them isolates the
ENTRANCE: the rough edges a first-time install actually hits BEFORE any graph exists. This gate walks that
path end-to-end through the real EventQueue processor (server.make_db_processor → handle_event → the real
core.* engine), with only the GitHub I/O faked, and asserts the entrance is HONEST + NEVER-CRASH + content-
free + jargon-clean:

  ENTRANCE-1  INSTALL ON AN EMPTY REPO (no commits → no head sha). Onboarding must INGEST NOTHING and NOT
              CRASH (no clone of a repo with no content), the org install completes cleanly.

  ENTRANCE-2  THE FIRST PR EVER, before ANY push-to-main ingest — the repo has NO graph. The verdict must be
              the HONEST 'unknown' ('Not analyzed', neutral check, never a fake 'clear', never a fake 'warn',
              never a crash). The check is ADVISORY (neutral, never a blocking conclusion). The first comment
              a real customer sees is content-free + carries the advisory framing + leaks NO internal jargon.

  ENTRANCE-3  THE FIRST INGEST. A push-to-main finally builds the graph. The already-open cold-start PR must
              be RE-EVALUATED and its check/comment PATCHED IN PLACE — NEVER double-posted (one comment, not
              two). This is the 'no spam on first ingest' invariant.

  ENTRANCE-4  DEGENERATE FIRST PR. A docs-only PR on a graphless repo (README/image/lock) must drop to a
              clean 'no reservation' honest-empty (no scary first message, no crash, no comment).

  ENTRANCE-5  THE SILENT-INSTALL FIX. Installing on a repo that HAS code but NO open PRs used to post NOTHING
              GitHub-visible (the dead first impression — the user grants code-read and sees total silence). Now
              onboarding emits ONE content-free, ADVISORY 'Veripsa is now watching' check on the default-branch
              HEAD (counts only). It is idempotent (a re-delivered install PATCHES in place, never a second post)
              and an EMPTY repo (no head sha) still posts nothing (no commit to anchor a check on — honest).

This is a PROOF-OF-CLEAN gate: it asserts the entrance the audit found already honest stays honest (a future
edit that makes a graphless first PR fake-'clear', double-post on first ingest, or crash on an empty-repo
install fails here). Content-free throughout: only paths / counts / verdicts are observed.

Run:  python3 tests/test_new_user_entrance.py   (needs local Postgres with the veripsa roles)
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
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
import server as S  # noqa: E402
import policy_refresh_queue as PR  # noqa: E402
# REUSE the customer-surface jargon DENYLIST scanner (one source of truth — never a second copy of the list):
# the very FIRST comment a new user sees must pass the SAME leak contract every other surface does.
from test_no_jargon_leak import _scan, _customer_strings  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB. Per-PID, exactly like the other gates.
DB = "veripsa_entrance_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"

INSTALL_ID = 4242
ACCOUNT_ID = 909090                          # the org/user that owns the repo (the stable tenant key)
TENANT = f"ACCT-GH-{ACCOUNT_ID}"
EMPTY_REPO = "newco/empty"                   # a repo with NO commits (install on it ingests nothing)
FRESH_REPO = "newco/fresh"                   # a repo whose FIRST PR opens before any graph
WATCHED_REPO = "newco/watched"               # a repo with CODE but NO open PRs (the silent-install case)
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")


def _repo_id(repo):
    return 800_000 + int.from_bytes(hashlib.sha256(repo.encode("utf-8")).digest()[:6], "big")


def _repo_head(repo):
    return hashlib.sha1(repo.encode()).hexdigest()


class EntranceGitHub:
    """A recording fake for a BRAND-NEW install. `empty_repos` have no head sha (an empty repo — onboarding
    must ingest nothing without crashing). For non-empty repos it serves the sample_app fixture tarball + files
    so the FIRST ingest (ENTRANCE-3) actually populates a graph. Counts post vs patch so the no-double-post
    invariant is observable (post != patch). Mirrors the FakeGitHub used by test_server / test_lifecycle_e2e."""

    def __init__(self, files_by_pr=None, empty_repos=()):
        self.files_by_pr = files_by_pr or {}
        self.pr_objects = {}
        self.empty_repos = set(empty_repos)
        self.checks, self.comments, self.installations = [], [], []
        self.post_comment_calls, self.patch_comment_calls = 0, 0
        self.post_check_calls, self.patch_check_calls = 0, 0
        self.cloned = []                      # repos whose tarball was downloaded (must be empty for an empty repo)
        self._cid, self._chid = 1000, 2000

    def for_installation(self, installation_id):
        self.installations.append(str(installation_id))
        return self

    def app_installation_identity(self, installation_id):
        return {"installation_id": str(installation_id), "account_id": str(ACCOUNT_ID),
                "created_at": "2026-01-01T00:00:00Z", "suspended": False}

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return self.files_by_pr.get(number, [])

    def list_pr_files_with_ranges(self, repo, number, pr_changed_files=0):
        return {p: [] for p in self.files_by_pr.get(number, [])}

    def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
        changed = list(self.files_by_pr.get(number, []))
        return {"changed": changed, "changed_ranges": {p: [] for p in changed},
                "added_paths": [], "conflict_markers": [], "raw_entry_count": len(changed)}

    def get_pull_request(self, repo, number):
        return self.pr_objects[number]

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
        self.comments.append({"id": self._cid, "number": number, "body": body, "user": {"type": "Bot"}})

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

    def pull_request_head(self, repo, number):
        return self.pr_objects.get(number, {}).get("head", {}).get("sha") or f"{number:040x}"

    def pull_request_head_and_fork(self, repo, number):
        return self.pull_request_head(repo, number), False

    def compare_changed_paths_strict(self, repo, base_sha, head_ref):
        return []

    def repo_default_branch_head(self, repo):
        if repo in self.empty_repos:
            return "main", ""                 # EMPTY repo: no commits → no head sha → onboarding ingests nothing
        # a STABLE per-repo HEAD sha (deterministic from the repo name) so each repo's default-branch HEAD is
        # distinct — the onboarding 'watching' check anchors here, and per-repo shas keep them from colliding.
        return "main", _repo_head(repo)

    def repo_onboarding_head_info(self, repo):
        branch, head = self.repo_default_branch_head(repo)
        return {
            "full_name": repo,
            "repository_id": _repo_id(repo),
            "owner_id": ACCOUNT_ID,
            "default_branch": branch,
            "head_sha": head or None,
            "empty": repo in self.empty_repos,
        }

    def repo_current_identity(self, repo):
        return {"id": _repo_id(repo), "full_name": repo, "owner_id": ACCOUNT_ID}

    def download_tarball(self, repo, sha):
        if repo in self.empty_repos:
            raise AssertionError("empty repo must NEVER be cloned (no head sha → no ingest)")
        self.cloned.append(repo)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(FIXTURE, arcname="newco-" + sha[:7])
        return buf.getvalue()

    def get_file_at(self, repo, path, ref):
        full = os.path.join(FIXTURE, path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()


def _install_created(repos):
    return {"action": "created",
            "installation": {"id": INSTALL_ID, "account": {
                "id": ACCOUNT_ID, "login": "newco", "type": "Organization"}},
            "repositories": [{"id": _repo_id(r), "full_name": r} for r in repos]}


def _pr_opened(number, author, files, repo, head_sha=None):
    head_sha = head_sha or f"{number:040x}"
    return ({"action": "opened", "number": number,
             "installation": {"id": INSTALL_ID, "account": {"id": ACCOUNT_ID}},
             "repository": {"id": _repo_id(repo), "full_name": repo, "default_branch": "main",
                            "owner": {"id": ACCOUNT_ID}},
             "pull_request": {"base": {"ref": "main", "sha": _repo_head(repo),
                                       "repo": {"id": _repo_id(repo)}},
                              "head": {"sha": head_sha, "repo": {"id": _repo_id(repo)},
                                       "ref": f"feature/{number}"},
                              "user": {"login": author}, "merged": False}}, files)


def _push_main(sha, pusher, added, repo):
    return {"ref": "refs/heads/main", "after": sha,
            "installation": {"id": INSTALL_ID, "account": {"id": ACCOUNT_ID}},
            "repository": {"id": _repo_id(repo), "full_name": repo, "default_branch": "main",
                           "owner": {"id": ACCOUNT_ID}},
            "pusher": {"name": pusher},
            "commits": [{"added": added, "modified": [], "removed": []}]}


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    # pick a real code file from the fixture so the FIRST ingest (ENTRANCE-3) genuinely puts it in the graph.
    py = None
    for dirpath, _dn, fns in os.walk(FIXTURE):
        for fn in sorted(fns):
            if fn.endswith(".py"):
                py = os.path.relpath(os.path.join(dirpath, fn), FIXTURE)
                break
        if py:
            break
    assert py, "fixture must contain at least one .py file for the first-ingest re-eval"

    gh = EntranceGitHub(empty_repos=[EMPTY_REPO])
    proc = S.make_db_processor(DSN_APP)
    delivery_seq = 0

    def deliver(event_type, payload):
        nonlocal delivery_seq
        if event_type == "pull_request":
            pr = dict(payload["pull_request"])
            pr["number"] = payload["number"]
            pr["state"] = "closed" if payload.get("action") == "closed" else "open"
            pr["changed_files"] = len(gh.files_by_pr.get(payload["number"], []))
            gh.pr_objects[payload["number"]] = pr
        if event_type == "installation" and payload.get("action") in {
                "created", "unsuspend", "new_permissions_accepted"}:
            delivery_seq += 1
            key = f"entrance-{payload['action']}-{delivery_seq}"
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

    checks = []

    # ── ENTRANCE-1: INSTALL ON AN EMPTY REPO (no commits). Onboarding must ingest NOTHING + NOT CRASH. ───────
    # An empty repo has no head sha → backfill_repo's `if sha:` skips the ingest entirely; download_tarball is
    # never called (the fake asserts that). The install must complete with NO graph and NO posted check/comment.
    deliver("installation", _install_created([EMPTY_REPO]))
    nodes_empty = admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s", (EMPTY_REPO,))
    checks.append(("ENTRANCE-1 empty-repo install: ingested NOTHING (no graph, repo never cloned) + no crash + "
                   "no check/comment posted",
                   nodes_empty == 0 and EMPTY_REPO not in gh.cloned and gh.post_check_calls == 0
                   and gh.post_comment_calls == 0))

    # ── ENTRANCE-2: THE FIRST PR EVER on a graphless repo → HONEST 'unknown', never fake-clear, never crash. ─
    pr_payload, pr_files = _pr_opened(7, "alice", [py], FRESH_REPO)
    gh.files_by_pr[7] = pr_files
    deliver("pull_request", pr_payload)

    pr7_checks = [c for c in gh.checks if c["sha"] == f"{7:040x}"]
    first_check = pr7_checks[0] if pr7_checks else None
    pr7_comments = gh.list_issue_comments(FRESH_REPO, 7)
    first_comment = pr7_comments[0]["body"] if pr7_comments else None
    nodes_fresh = admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s", (FRESH_REPO,))

    checks.append(("ENTRANCE-2a first PR on a graphless repo posts EXACTLY ONE check (no crash)",
                   len(pr7_checks) == 1 and first_check is not None))
    # the honest cold-start verdict: with the self-heal (#123), the FIRST PR triggers an on-demand ingest of
    # main, so the cold-start now gets a REAL analysis — an honest 'Clear' (when nothing else is in-flight),
    # not a bare 'Unknown'. The honest contract is EARNEDNESS: a 'Clear' must be backed by a graph that was
    # genuinely ingested (never a FAKE clear with no analysis); a bare 'Unknown' must say 'analyzed'. Either
    # way it must be advisory (never blocking).
    title2b = first_check["title"] if first_check else ""
    summ2b = (first_check["summary"] if first_check else "").lower()
    checks.append(("ENTRANCE-2b first verdict is HONEST + EARNED (advisory; a 'Clear' is backed by a real "
                   f"on-demand ingest, never a fake clear; an 'Unknown' says 'analyzed') (title={title2b!r}, "
                   f"nodes={nodes_fresh})",
                   bool(first_check) and first_check["conclusion"] in ("success", "neutral")
                   and not ("Clear" in title2b and nodes_fresh == 0)            # a 'Clear' with NO graph = a fake clear
                   and not ("Unknown" in title2b and "analyzed" not in summ2b)))  # an 'Unknown' must be honest about why
    # ADVISORY-NEVER-BLOCK: the entrance check conclusion is in {success, neutral} — never a blocking value
    # (action_required / failure) that would gate a new customer's very first merge.
    checks.append(("ENTRANCE-2c first check is ADVISORY (conclusion in {success,neutral}, never a blocking value)",
                   bool(first_check) and first_check["conclusion"] in ("success", "neutral")))
    # the new user's FIRST CONTACT must carry the advisory framing. A 'Clear' posts NO comment (correct
    # anti-spam #117) and carries the framing in the CHECK SUMMARY; a warn/serialize carries it in the COMMENT.
    # So the framing must appear in the customer surface (summary ∪ comment), wherever the verdict put it — and
    # the surface must stay jargon-clean (same denylist scanner every surface holds, not a second copy).
    entrance_out = {"title": first_check["title"] if first_check else "",
                    "summary": first_check["summary"] if first_check else "",
                    "comment": first_comment}
    surface_text = ((entrance_out["summary"] or "") + " " + (first_comment or "")).lower()
    framing_ok = ("does not assert correctness" in surface_text and "advisory" in surface_text)
    leaks = []
    for label, text in _customer_strings(entrance_out):
        leaks += _scan(f"entrance/{label}", text)
    checks.append(("ENTRANCE-2d the new user's FIRST CONTACT carries the advisory framing ('advisory' + 'does "
                   "not assert correctness') in the check summary or comment, so it is honest", framing_ok))
    checks.append(("ENTRANCE-2e the FIRST customer-facing strings (check + comment) leak NO internal jargon "
                   "(role/function/DB/design terms) — same denylist as every surface", not leaks))
    if leaks:
        for v in leaks:
            print("  LEAK:", v)

    # ── ENTRANCE-3: THE FIRST INGEST. A push-to-main finally builds the graph → the already-open cold-start PR
    #    is re-evaluated and PATCHED IN PLACE, NEVER double-posted. ────────────────────────────────────────
    comments_before = len(gh.list_issue_comments(FRESH_REPO, 7))
    post_calls_before, patch_calls_before = gh.post_comment_calls, gh.patch_comment_calls
    deliver("push", _push_main("a" * 40, "bob", [py], FRESH_REPO))
    drain_graph()
    nodes_after = admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s", (FRESH_REPO,))
    comments_after = len(gh.list_issue_comments(FRESH_REPO, 7))
    checks.append(("ENTRANCE-3a first push-to-main INGESTED the graph (the repo went from graphless to a real "
                   "code graph)", nodes_after > 0))
    # NO DOUBLE-POST: the re-evaluation must not create a SECOND comment on the same PR. It either patches the
    # existing one in place (post stays flat, patch increments) or drops to clear and leaves it (still flat).
    checks.append(("ENTRANCE-3b the already-open cold-start PR is NOT double-posted on first ingest "
                   "(comment count stays flat — the re-eval patches in place, never a second comment)",
                   comments_after == comments_before and gh.post_comment_calls == post_calls_before))

    # ── ENTRANCE-4: DEGENERATE FIRST PR — a docs-only PR on a graphless repo. The non-code paths are filtered
    #    out, leaving NO reserved code path → the honest-empty branch (success check, NO comment). Never scary. ─
    docs_payload, docs_files = _pr_opened(8, "carol", ["README.md", "docs/guide.md", "logo.png"], FRESH_REPO)
    gh.files_by_pr[8] = docs_files
    pre_docs_comments = gh.post_comment_calls
    deliver("pull_request", docs_payload)
    pr8_checks = [c for c in gh.checks if c["sha"] == f"{8:040x}"]
    pr8_comments = gh.list_issue_comments(FRESH_REPO, 8)
    checks.append(("ENTRANCE-4 docs-only first PR → honest-empty: a posted check, NO scary comment, no crash "
                   "(non-code paths carry no coupling → nothing to flag)",
                   len(pr8_checks) == 1 and len(pr8_comments) == 0
                   and gh.post_comment_calls == pre_docs_comments
                   and pr8_checks[0]["conclusion"] in ("success", "neutral")))

    # ── ENTRANCE-5: THE SILENT-INSTALL FIX. Install on a repo that HAS code but NO open PRs (the dead first
    #    impression). list_open_pull_requests returns [] → backfill_open_prs processes zero PRs → WITHOUT the fix
    #    NOTHING GitHub-visible is posted. WITH the fix, onboarding emits ONE 'Veripsa is now watching' check on
    #    the default-branch HEAD: advisory (neutral), content-free (counts only), jargon-clean, idempotent. ─────
    watched_head = gh.repo_default_branch_head(WATCHED_REPO)[1]
    posts_before, patches_before = gh.post_check_calls, gh.patch_check_calls
    comments_before_5 = gh.post_comment_calls
    deliver("installation", _install_created([WATCHED_REPO]))
    drain_graph()
    watched_checks = [c for c in gh.checks if c["sha"] == watched_head and c.get("name") == "Veripsa"]
    nodes_watched = admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s", (WATCHED_REPO,))
    watching = watched_checks[0] if watched_checks else None
    # 5a: the install on a PR-less repo is now VISIBLE — exactly ONE 'now watching' check posted (not silent).
    checks.append(("ENTRANCE-5a install on a repo with code but NO open PRs is now VISIBLE: exactly ONE 'Veripsa "
                   "is now watching' check posted on the default-branch HEAD (the silent-install fix)",
                   len(watched_checks) == 1 and watching is not None
                   and gh.post_check_calls == posts_before + 1
                   and gh.post_comment_calls == comments_before_5))   # a check, NOT a noisy comment, on every repo
    # 5b: ADVISORY (neutral, never a blocking conclusion) + the right title, and the graph really did ingest.
    checks.append(("ENTRANCE-5b the watching signal is ADVISORY (conclusion 'neutral', never blocking) titled "
                   f"'Veripsa is now watching', and the repo's graph genuinely ingested (nodes={nodes_watched})",
                   bool(watching) and watching["conclusion"] == "neutral"
                   and watching["title"] == "Veripsa is now watching" and nodes_watched > 0))
    # 5c: CONTENT-FREE + JARGON-CLEAN — the first thing a silent-install user sees must hold the same leak
    # contract every customer surface does (no path/symbol/body; no internal role/function/DB token).
    watching_out = {"title": watching["title"] if watching else "", "summary": watching["summary"] if watching else "", "comment": None}
    leaks5 = []
    for label, text in _customer_strings(watching_out):
        leaks5 += _scan(f"watching/{label}", text)
    checks.append(("ENTRANCE-5c the watching signal is content-free + jargon-clean (counts only; same denylist as "
                   "every surface)", not leaks5))
    if leaks5:
        for v in leaks5:
            print("  LEAK:", v)
    # 5d: IDEMPOTENT — a re-delivered / re-installed event PATCHES the same check in place, never a SECOND post
    #     (the no-double-post invariant: a user must never see the 'now watching' check twice).
    deliver("installation", _install_created([WATCHED_REPO]))
    drain_graph()
    watched_checks_after = [c for c in gh.checks if c["sha"] == watched_head and c.get("name") == "Veripsa"]
    checks.append(("ENTRANCE-5d the watching signal is IDEMPOTENT: a re-delivered install PATCHES in place "
                   "(still exactly ONE check, no second post)",
                   len(watched_checks_after) == 1 and gh.post_check_calls == posts_before + 1))

    # ── report ──────────────────────────────────────────────────────────────────────────────────────────────
    print("\n── NEW-USER ENTRANCE ──────────────────────────────────────")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("\nNEW-USER ENTRANCE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        # SAME teardown as test_lifecycle_e2e: drop the per-PID DB. The next run's bootstrap_local also dropdb's
        # first, so this is belt-and-suspenders (a leak can't accumulate or collide across parallel runs).
        subprocess.run(["dropdb", DB], capture_output=True)
