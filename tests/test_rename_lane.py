#!/usr/bin/env python3
"""RENAMED-FILE LANE gate — a PATHOLOGICAL changed-file SHAPE the GitHub Files API surfaces only as its NEW
name, which used to leave the OLD path UN-coordinated (a false CLEAR).

THE HOLE (this gate is the proof it is closed): GitHub's Pull-Request Files API returns a renamed file ONLY as
the NEW `filename` (+ `status:"renamed"` + `previous_filename` = the old path). The App read `filename` and
`patch` and IGNORED `status`/`previous_filename`. So a PR that renames `auth.py → login.py` reserved a lane on
`login.py` ALONE — the OLD path `auth.py` it just REMOVED was invisible to the lane layer. A concurrent PR still
editing `auth.py` therefore did NOT collide with the rename, even though git WILL conflict (one side
renames/deletes the file, the other modifies it). That is exactly the failure class this audit targets: a
FALSE CLEAR produced by a degenerate file shape (no false collide / no false clear from a file shape).

THE FIX (content-free, bounded, never-crash): github_rest._rename_source surfaces a `renamed` entry's
`previous_filename` so the PR ALSO reserves a FILE-LEVEL lane on the old path (no new-side lines exist there →
[] → file-level collision = the recall-safe unit). Restricted to `renamed` (the source is genuinely gone = a
guaranteed conflict); a `copied` source is LEFT in place by git so it is NOT surfaced (no cry-wolf). Only the
old path STRING crosses — the same content-free coordinate as every other path; never any diff body.

This gate proves it on TWO levels:
  (A) UNIT — the REAL fetch (GitHubREST.list_pr_files_with_ranges / list_pr_files), driven through the REAL
      _api seam serving crafted Files-API pages (renamed-with-edits / pure-rename / copied / binary / deleted),
      surfaces the rename SOURCE as a file-level entry, KEEPS the new path's symbol ranges, does NOT surface a
      copied source, and never lets a diff BODY string cross (content-free).
  (B) E2E — the REAL gate (db/schema.sql): two concurrent open PRs — PR-A renames mod_old.py→mod_new.py, PR-B
      edits mod_old.py — and the lane on mod_old.py COLLIDES (PR-B is sent to 'waiting' behind PR-A).
  (C) CONTROL (load-bearing) — the SAME pair with PR-A claiming ONLY the new path (the OLD behavior) leaves
      mod_old.py un-contended (both 'active') = the fix is real, not a no-op.

Needs local Postgres with the veripsa roles. Run:  python3 tests/test_rename_lane.py
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
from github_rest import GitHubREST  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): per-PID so concurrent run_gates shards never drop each other's DB mid-run.
DB = "veripsa_renamelane_" + str(os.getpid())
REPO = "acme/rename"

FAIL = 0


def check(cond: bool, label: str):
    global FAIL
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        FAIL += 1


# A SECRET-looking string living ONLY in a diff BODY (a +/- content line), never in a hunk header. If it ever
# reaches the fetched ranges dict, the content-free contract is broken.
SECRET = "RENAME_BODY_SECRET_qqq777"


def fake_rest_serving(pages):
    """A real GitHubREST whose ONE network seam (_api) is overridden to serve the given Files-API `pages`
    (a list of page lists). Everything else — the rename-source logic, the hunk parser, pagination — is the
    REAL production code path, so this gate cannot rot to a mock that re-implements the behavior.
    _api_with_link is also overridden so the Link-header-aware pagination path (list_pr_files / _page_pr_files_raw)
    also uses the fake pages: `has_next` is True when more pages remain in `pages`, False on the last page."""
    c = GitHubREST("app-id", "key", "inst-id")
    state = {"i": 0}

    def _api(method, url, body=None, accept=None):
        i = state["i"]
        state["i"] += 1
        return pages[i] if i < len(pages) else []

    def _api_with_link(method, url, body=None, accept=None):
        i = state["i"]
        state["i"] += 1
        page = pages[i] if i < len(pages) else []
        # Synthesize a Link header: if there are more pages after this one, emit rel="next"; otherwise empty.
        link = '<https://api.github.com/next>; rel="next"' if i + 1 < len(pages) else ""
        return page, link

    c._api = _api
    c._api_with_link = _api_with_link
    return c


def unit_probes():
    print("-- (A) UNIT: real GitHubREST fetch over crafted Files-API pages --")
    # A realistic single page mixing every pathological shape.
    page = [
        # renamed WITH edits: NEW path keeps symbol ranges; the +/- body carries the SECRET (must be discarded).
        {"filename": "src/login.py", "status": "renamed", "previous_filename": "src/auth.py",
         "patch": f"@@ -10,3 +10,4 @@\n def check():\n-    pw = {SECRET}\n+    pw = {SECRET}_v2\n+    log()\n"},
        # pure rename: NO patch key at all (GitHub omits it for a 100%-similar rename).
        {"filename": "src/moved.py", "status": "renamed", "previous_filename": "src/orig.py"},
        # copied: source LEFT in place → must NOT be surfaced (no cry-wolf).
        {"filename": "src/copy.py", "status": "copied", "previous_filename": "src/template.py"},
        # plain modify.
        {"filename": "src/plain.py", "status": "modified", "patch": "@@ -1,2 +1,3 @@\n a\n+b\n c\n"},
        # binary: no patch.
        {"filename": "assets/logo.png", "status": "modified"},
        # deleted: a deletion-only hunk (no new-side lines) → [].
        {"filename": "src/gone.py", "status": "removed", "patch": "@@ -1,3 +0,0 @@\n-x\n-y\n-z\n"},
        # junk previous_filename (hostile / malformed entry) must not crash or surface.
        {"filename": "src/weird.py", "status": "renamed", "previous_filename": 12345},
    ]
    c = fake_rest_serving([page])
    rng = c.list_pr_files_with_ranges(REPO, 1)

    check("src/auth.py" in rng, "renamed SOURCE 'src/auth.py' IS surfaced (the false-clear fix)")
    check(rng.get("src/auth.py") == [], "renamed source maps to [] = FILE-LEVEL (no new-side lines exist there)")
    check(rng.get("src/login.py") == [[10, 12]], "renamed NEW path keeps its symbol-level BASE-side ranges [[10,12]]")
    check("src/orig.py" in rng, "PURE-rename SOURCE 'src/orig.py' surfaced even with NO patch")
    check("src/template.py" not in rng, "COPIED source 'src/template.py' is NOT surfaced (copy leaves it in place — no cry-wolf)")
    check(rng.get("assets/logo.png") == [], "binary file → [] (file-level fallback, no crash)")
    check(rng.get("src/gone.py") == [[1, 3]], "deleted file's deletion hunk → its BASE-side range [[1,3]] (the "
          "deleted symbols; base-side correctly registers a deletion — new-side dropped it as empty = a latent miss)")
    check("src/weird.py" in rng and 12345 not in rng.values() and not any(v == [12345] for v in rng.values()),
          "junk previous_filename (non-str) does not crash and is not surfaced as a path")

    # CONTENT-FREE: no diff BODY text (the SECRET) may appear ANYWHERE in the fetched structure — keys or values.
    blob = repr(rng)
    check(SECRET not in blob, "CONTENT-FREE: the diff +/- BODY (the secret) never reaches the fetched ranges dict")

    # list_pr_files (the names-only fallback) ALSO surfaces the rename source.
    c2 = fake_rest_serving([page])
    names = c2.list_pr_files(REPO, 1)
    check("src/auth.py" in names and "src/login.py" in names,
          "list_pr_files (fallback) surfaces BOTH the new path AND the rename source")
    check("src/template.py" not in names, "list_pr_files: copied source NOT surfaced (consistent with with_ranges)")
    check(SECRET not in repr(names), "CONTENT-FREE: list_pr_files carries no diff body")

    # PAGINATION across the boundary: a rename source on a LATER page is surfaced (the loop, not just page 1).
    p1 = [{"filename": f"src/f{n}.py", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b\n"} for n in range(100)]
    p2 = [{"filename": "src/renamed_late.py", "status": "renamed", "previous_filename": "src/old_late.py"}]
    cpag = fake_rest_serving([p1, p2])
    rpag = cpag.list_pr_files_with_ranges(REPO, 2)
    check("src/old_late.py" in rpag and rpag["src/old_late.py"] == [],
          "PAGINATION: a rename source on page 2 is surfaced (the whole loop, not just page 1)")


INSTALL_ID = "575757"   # → enter_installation provisions account ACCT-GH-575757 (pinned on the App connection)


def declare(app, change_id, path, author):
    """Reserve a lane exactly as the App's per-path declare does (act_for the PR author, content-free), on the
    SAME persistent App connection that already pinned the installation (so the session write-context routes to
    the same tenant — exactly the production single-connection-per-event shape)."""
    with app.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
                    (f"{change_id}:{path}", path, REPO, "main", author))


def claim_state(mig, change_id, path):
    # Pin the tenant + read in ONE transaction: set_config(...,is_local=true) is TRANSACTION-scoped, so the pin
    # and the SELECT must share a txn (FORCE RLS is on — a lost pin reads nothing). `with mig` = one txn.
    with mig, mig.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT set_config('core.current_account', %s, true)", ("ACCT-GH-" + INSTALL_ID,))
        cur.execute("""SELECT claim_state FROM core.claim
                       WHERE change_id=%s AND target_path=%s AND repo=%s AND branch='main'
                       ORDER BY claimed_at DESC LIMIT 1""", (change_id, path, REPO))
        row = cur.fetchone()
        return row[0] if row else None


def e2e_probes():
    print("-- (B) E2E: rename SOURCE lane collides with a concurrent edit (real gate) --")
    # ONE persistent App connection (autocommit) pins the installation, then declares — the production shape.
    app = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    app.autocommit = True
    mig = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")   # NOT autocommit: claim_state needs a txn
    try:
        with app.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (INSTALL_ID,))

        OLD, NEW = "mod_old.py", "mod_new.py"
        # PR-A renames mod_old.py → mod_new.py: with the fix it reserves BOTH the new path AND the rename source.
        declare(app, "PR-A", NEW, "alice")
        declare(app, "PR-A", OLD, "alice")        # the surfaced rename SOURCE (file-level)
        # PR-B (concurrent, different author) EDITS the original mod_old.py.
        declare(app, "PR-B", OLD, "bob")

        check(claim_state(mig, "PR-A", OLD) == "active",
              "PR-A holds the rename-source lane mod_old.py = 'active' (it claimed first)")
        check(claim_state(mig, "PR-B", OLD) == "waiting",
              "PR-B editing mod_old.py is sent to 'waiting' behind the rename = COLLISION (the false-clear is CLOSED)")
        check(claim_state(mig, "PR-A", NEW) == "active",
              "PR-A's new path mod_new.py is independently 'active'")

        print("-- (C) CONTROL: WITHOUT the surfaced source, the same pair does NOT collide (fix is load-bearing) --")
        # PR-C renames the file but (OLD behavior) claims ONLY the new path; PR-D edits the old path → no contention.
        declare(app, "PR-C", "ctl_new.py", "carol")   # NEW only — the rename source ctl_old.py is NOT claimed
        declare(app, "PR-D", "ctl_old.py", "dave")
        check(claim_state(mig, "PR-C", "ctl_new.py") == "active" and claim_state(mig, "PR-D", "ctl_old.py") == "active",
              "CONTROL: without the rename source on the lane, the edit to the old path stays UN-contended (both "
              "'active') = the fix genuinely changes the verdict, it is not a no-op")
    finally:
        app.close()
        mig.close()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1
    try:
        unit_probes()
        e2e_probes()
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)

    print()
    if FAIL == 0:
        print("RENAME LANE GATE: PASS")
        return 0
    print(f"RENAME LANE GATE: FAIL ({FAIL} failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
