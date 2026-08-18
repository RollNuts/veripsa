#!/usr/bin/env python3
"""PAUSE-ACK TXN ISOLATION GATE — an OPTIONAL best-effort DB surface that
PERMISSION-DENIES under least-privilege aborts the SHARED per-event transaction, which then silently SKIPS the
CUSTOMER-FACING pause-ack overlay (the pause vanishes → a material-coupling check falls back to a plain
neutral). The synthetic least-privilege reproduction is:

    co-change read skipped repo=.../pr=11: permission denied for table co_change
    coverage nudge skipped repo=.../pr=11: current transaction is aborted, commands ignored until end of ...
    pause-ack overlay skipped repo=.../pr=10: current transaction is aborted...

ROOT CAUSE: the whole webhook event runs in ONE non-autocommit transaction (event_processor.make_db_processor).
A best-effort optional read (co-change / coverage nudge / prediction telemetry) wrapped in a BARE try/except
catches the Python exception but does NOT clear Postgres's ABORTED-transaction state — so every LATER statement
in the same event, including the pause-ack overlay's brain read, then fails with "current transaction is aborted"
and the overlay is skipped. This passes the OWNER-role local gates (the owner has full table grants, so the raw
read succeeds locally) — a gates-green != prod-correct gap (same class as the cg_generated / least-privilege
misses). veripsa_app (the prod least-privilege role) has NO direct grant on core.co_change (moat design — read
only via the SECURITY-DEFINER co_change_*_with_authority functions), so a raw table read raises permission_denied.

WHAT THIS GATE PROVES, on the REAL handle_event -> handle_pull_request -> apply_pause_ack path, over a SINGLE
shared non-autocommit connection authed as the REAL least-privilege role veripsa_app (the exact prod model):

  ISO-1  REPRODUCE-THE-BUG (negative control): with the savepoint isolation DISABLED (the optional surface back
         to a bare try/except), a least-privilege permission-deny on the co-change read ABORTS the shared txn ->
         the pause-ack overlay is SKIPPED -> the material serialize check FALLS BACK to plain `neutral` (the
         pause SILENTLY VANISHES). This is the prod failure, reproduced deterministically.
  ISO-2  THE FIX: with savepoint isolation ON (origin/main + this fix), the SAME poisoned least-privilege read
         is CONTAINED to its own savepoint -> the shared txn stays clean -> the pause-ack overlay STILL posts
         `action_required` (the pause SURVIVES a poisoned-prior optional read).
  ISO-3  CO-CHANGE FAILS OPEN, VERDICT UNCHANGED: the advisory co-change line is just dropped on the read error;
         it never changes the verdict (the material coupling is still paused exactly as without co-change).

Run:  python3 tests/test_pauseack_txn_isolation.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)
import server as S              # noqa: E402  (re-exports handle_event / _scoped_db)
import webhook as W             # noqa: E402  (the brain + the _optional savepoint helper under test)
import webhook_handlers as WH   # noqa: E402  (the acting-PR pause-ack overlay; bound _optional at import)
import policy_refresh_queue as PR  # noqa: E402  (production graph-convergence claim/drain path)

DB = "veripsa_pauseack_iso_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"          # the REAL prod least-privilege role (NO co_change table grant)

INSTALL_ID = 9191
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")
SHA_MAIN = "abcd1234" * 5                                     # main's baseline graph sha (40 hex)
COLLIDE_PATH = "backend/api.py"                              # both PRs edit this SAME file → a MATERIAL serialize coupling

# Each scenario runs in its OWN account + repo (RLS-isolated, fresh claims) so the two scenarios on the SAME
# bootstrapped DB never share lane state — the bug-repro run must not leak claims into the fix run.
BUG_ACCOUNT, BUG_REPO = 535353, "iso/bug-app"
FIX_ACCOUNT, FIX_REPO = 727272, "iso/fix-app"

# The co-change read the brain issues (webhook.py) — we intercept THIS exact call and substitute the RAW table
# read that veripsa_app permission-denies on, to reproduce the least-privilege abort deterministically.
_COCHANGE_FN = "co_change_partners_with_authority"
# The raw table read veripsa_app has NO grant for (moat design) → "permission denied for table co_change".
_RAW_COCHANGE = "SELECT 1 FROM core.co_change LIMIT 1"


def _stable_repo_id(repo):
    """Deterministic positive bigint matching GitHub's rename-stable repository identity contract."""
    value = int.from_bytes(hashlib.sha256(repo.encode("utf-8")).digest()[:8], "big") & ((1 << 63) - 1)
    return value or 1


class IsoGitHub:
    """Recording fake. Serves the sample_app fixture so the push builds a REAL main graph; records the check
    conclusions so we can read whether the pause survived."""

    def __init__(self, account_id):
        self.account_id = account_id
        self.checks, self.comments = [], []
        self.head_sha = SHA_MAIN
        self._cid, self._chid = 1000, 2000

    def for_installation(self, installation_id):
        return self

    def installation_account_id(self):
        return str(self.account_id)

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return [COLLIDE_PATH]

    def post_check(self, repo, sha, conclusion, title, summary):
        self._chid += 1
        check = {"id": self._chid, "sha": sha, "conclusion": conclusion, "title": title,
                 "summary": summary, "name": "Veripsa"}
        self.checks.append(check)
        return check

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha and c.get("name") == "Veripsa"]

    def patch_check(self, repo, check_run_id, conclusion, title, summary):
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
        self._cid += 1
        self.comments.append({"id": self._cid, "number": number, "body": body, "user": {"type": "Bot"}})

    def list_issue_comments(self, repo, number):
        return [c for c in self.comments if c["number"] == number]

    def patch_comment(self, repo, comment_id, body):
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
        return f"{number:040x}"

    def get_pull_request(self, repo, number):
        repo_id = _stable_repo_id(repo)
        return {"number": number, "base": {"ref": "main", "sha": SHA_MAIN,
                                          "repo": {"id": repo_id, "full_name": repo}},
                "head": {"sha": f"{number:040x}", "repo": {"id": repo_id, "full_name": repo},
                         "ref": f"feature/{number}"},
                "user": {"login": "dev"}, "draft": False, "merged": False, "labels": [],
                "state": "open", "changed_files": 1}

    def pr_labels(self, repo, number, strict=False):
        return []

    def repo_default_branch_head(self, repo):
        return "main", self.head_sha

    def repo_current_identity(self, repo):
        return {"id": _stable_repo_id(repo), "full_name": repo, "owner_id": self.account_id}

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(FIXTURE, arcname="iso-" + sha[:7])
        return buf.getvalue()

    def get_file_at(self, repo, path, ref):
        full = os.path.join(FIXTURE, path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()

    def compare_changed_paths(self, repo, base_sha, head_sha):
        return []

    compare_changed_paths_strict = compare_changed_paths


def _push_main(account, repo, sha):
    repo_id = _stable_repo_id(repo)
    return {"ref": "refs/heads/main", "after": sha,
            "installation": {"id": INSTALL_ID, "account": {"id": account}},
            "repository": {"id": repo_id, "full_name": repo, "default_branch": "main",
                           "owner": {"id": account}},
            "pusher": {"name": "dev"},
            "commits": [{"added": [COLLIDE_PATH], "modified": [], "removed": []}]}


def _pr_opened(account, repo, number):
    head = f"{number:040x}"
    repo_id = _stable_repo_id(repo)
    return {"action": "opened", "number": number,
            "installation": {"id": INSTALL_ID, "account": {"id": account}},
            "repository": {"id": repo_id, "full_name": repo, "default_branch": "main",
                           "owner": {"id": account}},
            "pull_request": {"base": {"ref": "main", "sha": SHA_MAIN,
                                      "repo": {"id": repo_id, "full_name": repo}},
                             "head": {"sha": head, "repo": {"id": repo_id, "full_name": repo},
                                      "ref": f"feature/{number}"},
                             "user": {"login": "dev"}, "merged": False, "labels": []}}


def _drain_main_graph(gh):
    """Run the same durable graph claim/strict-convergence turn as the production background worker."""
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

    result = PR._drain_policy_refreshes(
        PR.PolicyRefreshStore(DSN_APP), gh, DSN_APP, limit=20,
        graph_refresh_strict=S.converge_main_graph_strict,
        post_refreshes=discard_refresh_surfaces)
    if result.get("graph_drained", 0) < 1:
        raise AssertionError(f"graph convergence failed: {result!r}")


def _poisoning_processor(dsn, account, gh):
    """A faithful copy of event_processor.make_db_processor's TRANSACTION MODEL (ONE shared non-autocommit
    connection, enter_installation pinned, one commit at the end) — the exact prod model the bug lives in — with
    ONE controlled twist: the co-change authority-fn SELECT the brain issues is SWAPPED for the RAW core.co_change
    table read that veripsa_app permission-denies on (the real least-privilege gap, reproduced deterministically).
    Whether the optional surface is savepoint-isolated (the fix) or bare (the bug) is controlled by the caller
    monkeypatching webhook._optional / webhook_handlers._optional. Returns the per-event runner."""
    def proc(event_type, payload):
        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(account),))
            conn.autocommit = False                 # BODY: one transaction (the prod model)
            base_db = S._scoped_db(conn)

            def db(sql, args=()):
                # Reproduce the prod least-privilege abort: when the brain reaches the co-change authority-fn
                # read, run the RAW core.co_change table read instead (veripsa_app has NO grant → permission
                # denied for table co_change → the shared txn aborts unless the read is savepoint-isolated).
                if _COCHANGE_FN in sql:
                    return base_db(_RAW_COCHANGE)
                return base_db(sql, args)
            try:
                S.handle_event(event_type, payload, db, gh)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
        finally:
            conn.close()
    return proc


def _scenario(account, repo):
    """In an ISOLATED account+repo: seed main's graph + open two PRs on the SAME file (a material serialize).
    Return PR-2's check conclusion (action_required = the pause SURVIVED; anything else = the pause VANISHED)
    and the GitHub fake. The co-change read is forced to permission-deny under veripsa_app on EVERY pull_request
    event (the poison)."""
    gh = IsoGitHub(account)
    proc = _poisoning_processor(DSN_APP, account, gh)
    # The live push transaction records facts and durably requests graph work; the isolated production drainer
    # then claims that exact stable-id/SHA turn and builds main's real graph before the PR events begin.
    proc("push", _push_main(account, repo, SHA_MAIN))
    _drain_main_graph(gh)
    proc("pull_request", _pr_opened(account, repo, 1))   # PR-1: first claim on COLLIDE_PATH
    proc("pull_request", _pr_opened(account, repo, 2))   # PR-2: same file → MATERIAL serialize → pause-ack should fire
    pr2_checks = gh.list_check_runs(repo, f"{2:040x}")
    return (pr2_checks[-1]["conclusion"] if pr2_checks else None), gh


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    checks = []

    # ── ISO-1: REPRODUCE THE BUG. Savepoint isolation OFF (W._optional → bare try/except) → the least-privilege
    #    co-change permission-deny ABORTS the shared txn → the pause-ack overlay is skipped → the material
    #    serialize check FALLS BACK to plain `neutral` (the pause silently VANISHES — the prod failure). ─────────
    _real_optional = W._optional

    def _bare_optional(db, label, work, *, default=None, repo="", pr="", trace_id=""):
        """The PRE-FIX behavior: a bare try/except with NO savepoint — catches the Python error but leaves the
        shared Postgres transaction ABORTED, so every later statement (incl. the pause-ack overlay) fails.

        `trace_id=""` accepts the per-event trace-id kwarg the real _optional gained (Round-2 observability
        follow-up: a content-free uuid4 threaded into the skipped-surface log line). Kept as a tolerated kwarg
        so this test's monkeypatch stays signature-compatible with the production caller and continues to
        isolate the BEHAVIOR (no savepoint = abort the shared txn) it actually exercises."""
        try:
            return work()
        except Exception as e:
            _tp = f"trace_id={trace_id[:12]} " if trace_id else ""
            print(f"{_tp}{label} skipped repo={repo} pr={pr}: {str(e)[:120]}", flush=True)
            return default

    try:
        W._optional = _bare_optional
        WH._optional = _bare_optional                        # the handler module bound the name at import
        bug_conclusion, _ = _scenario(BUG_ACCOUNT, BUG_REPO)
    finally:
        W._optional = _real_optional
        WH._optional = _real_optional

    checks.append(("ISO-1 REPRODUCED: with the savepoint isolation OFF, a least-privilege co-change "
                   "permission-deny poisons the shared txn → the pause-ack overlay is SKIPPED → the material "
                   "serialize check does NOT post action_required (the pause silently VANISHES)",
                   bug_conclusion != "action_required"))
    print(f"  [bug repro] PR-2 conclusion with isolation OFF = {bug_conclusion!r} (expect NOT 'action_required' = pause lost)")

    # ── ISO-2 + ISO-3: THE FIX. The REAL W._optional (savepoint isolation) runs. The SAME poisoned co-change
    #    read is contained to its own savepoint → the shared txn stays clean → the pause-ack overlay STILL posts
    #    `action_required` (the pause SURVIVES), and co-change failing open did NOT change the verdict. ──────────
    fix_conclusion, gh = _scenario(FIX_ACCOUNT, FIX_REPO)
    checks.append(("ISO-2 FIXED: with savepoint isolation ON, the SAME poisoned least-privilege co-change read is "
                   "contained → the pause-ack overlay STILL posts 'action_required' (the pause SURVIVES a "
                   "poisoned-prior optional read)",
                   fix_conclusion == "action_required"))
    print(f"  [fix] PR-2 conclusion with isolation ON = {fix_conclusion!r} (expect 'action_required' = pause survived)")

    # ISO-3: co-change failed OPEN — the advisory line is just dropped; the comment still carries the material
    # serialize signal ("Wait in line") + the proceed-by-ack path, and the verdict is unchanged from above.
    pr2_comment = (gh.list_issue_comments(FIX_REPO, 2) or [{}])[-1].get("body", "")
    checks.append(("ISO-3 co-change FAILS OPEN, verdict UNCHANGED: the dropped advisory read did NOT change the "
                   "material verdict — PR-2 still carries the serialize signal + the proceed-by-ack path, paused",
                   "Wait in line" in pr2_comment and "veripsa-ack" in pr2_comment
                   and fix_conclusion == "action_required"))

    print("\n── PAUSE-ACK TXN ISOLATION ─────────────────────────────────")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("\nPAUSEACK TXN ISOLATION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
    sys.exit(rc)
