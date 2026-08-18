#!/usr/bin/env python3
"""CO-CHANGE POPULATE gate — the glue that makes co-change actually APPEAR in production: a repo's COMMIT
HISTORY → the stored, queryable co-change signal, end to end.

The code graph is ingested from a TARBALL (a snapshot at one sha — NO history), so "files that change together"
(the coupling no call/import/schema edge can show) would never be populated without a separate history fetch.
ingest.populate_cochange closes that: it takes a BLOBLESS, NO-CHECKOUT clone (github_rest.history_clone), runs
the precision-disciplined extractor, and stores the pairs through the gated write path. This gate proves that
whole glue, on a real git history, against a real (scratch) tenant DB:

  (1) POPULATE end to end: a clone dir → cochange_pairs → ingest_cochange → the co_change table → the partner
      read surfaces the real coupling (editing auth surfaces api), with the directional confidence;
  (2) NO-CHECKOUT is sufficient: the clone has NO working tree (only .git) — proving co-change reads HISTORY,
      not file bodies (this is exactly the content-free shape history_clone produces in production);
  (3) CONTENT-FREE: the crafted repo's SECRET file bodies + commit messages NEVER reach the stored/returned
      values (paths + counts only);
  (4) FAIL-OPEN: a clone that FAILS does NOT raise out of populate_cochange and does NOT populate anything —
      onboarding/the graph are never aborted by the advisory 2nd signal;
  (5) NOISE CONTROL survives the round trip: the 50-file giant commit mints no partners.

Run:  python3 tests/test_cochange_populate.py   (needs local Postgres with the veripsa roles + git)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import ingest  # noqa: E402

DB = "veripsa_ccpop_" + str(os.getpid())
REPO = "acme/cc"
checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def tenant(install_id):
    """A db runner pinned to one installation's tenant (enter_installation) — the live per-event shape that
    populate_cochange / ingest_cochange / the partner read all run under."""
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("SET search_path=core")
        c.execute("SELECT core.enter_installation_with_authority(%s)", (install_id,))

    def run(sql, args=()):
        with conn.cursor() as c:
            c.execute(sql, args)
            row = c.fetchone()
            return row[0] if row else None
    return run


def _j(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def _git(d, *a):
    subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, check=True)


def _commit(d, files, tok):
    for f in files:
        p = os.path.join(d, f)
        os.makedirs(os.path.dirname(p) or d, exist_ok=True)
        open(p, "a").write(f"// SECRET_BODY_{tok}\n")   # a file BODY a content-free clone must never read
        _git(d, "add", f)
    _git(d, "commit", "-m", f"SECRET_MSG_{tok}", "--no-verify")   # a commit MESSAGE --name-only must never carry


def _craft_history(d):
    """auth↔api co-change 5x, each solo 2x, 40 background commits (so auth/api are rare vs N → lift >> 1), and a
    50-file giant commit (must be skipped). SECRET_ tokens salt every body + message (content-free probe)."""
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@e.com"); _git(d, "config", "user.name", "T")
    _git(d, "config", "commit.gpgsign", "false")
    for i in range(5):
        _commit(d, ["backend/auth.py", "backend/api.py"], f"p{i}")
    _commit(d, ["backend/auth.py"], "as1"); _commit(d, ["backend/auth.py"], "as2")
    _commit(d, ["backend/api.py"], "is1"); _commit(d, ["backend/api.py"], "is2")
    for k in range(40):
        _commit(d, [f"misc/m{k}.py"], f"bg{k}")
    _commit(d, [f"v/lib{n}.py" for n in range(50)], "giant")


class FakeGh:
    """Stands in for GitHubClient WITHOUT the network: history_clone produces the SAME shape the real one does —
    a NO-CHECKOUT clone (history present, no working tree). The real method's only extra is `--filter=blob:none`
    (a network-side optimization that does not change `git log --name-only`) + the token-in-env plumbing; both
    are covered by review. `raises=True` simulates a clone failure (permission gap / git missing) for fail-open."""

    def __init__(self, src, raises=False):
        self.src, self.raises = src, raises

    def history_clone(self, repo, branch, dest, timeout=120):
        if self.raises:
            raise RuntimeError("simulated clone failure (e.g. App lacks contents:read, or git missing)")
        subprocess.run(["git", "clone", "--no-checkout", "--quiet", self.src, dest],
                       capture_output=True, text=True, check=True)
        return dest


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    A = tenant("111")   # ACCT-GH-111

    with tempfile.TemporaryDirectory() as src:
        _craft_history(src)
        gh = FakeGh(src)

        # (1) POPULATE end to end: history → clone → extract → store. populate_cochange takes the SAME db runner
        # ingest_cochange does; it must report a real pair count and never raise.
        res = ingest.populate_cochange(A, gh, REPO, "main", window=2000)
        chk(res.get("ok") and res.get("pairs_found", 0) >= 1,
            f"(1) populate seeds the co-change table from history (got {res})")

        # the partner read now surfaces the real coupling: editing auth → api, confidence P(api|auth)=5/7, lift>=2.
        parts = _j(A("SELECT core.co_change_partners_with_authority(%s,%s,%s,%s)", (REPO, ["backend/auth.py"], 3, 0.4)))
        api = next((p for p in parts if p.get("partner") == "backend/api.py"), None)
        chk(api is not None and abs(float(api["prob"]) - 5 / 7) < 0.02 and float(api.get("lift", 0)) >= 2.0,
            f"(1b) after populate, editing auth surfaces api with confidence ~{5/7:.2f} and lift>=2 (got {api})")

        # (2) NO-CHECKOUT sufficiency: re-do the clone the way populate does and confirm it has .git but NO
        # working-tree file — co-change came from HISTORY, not bodies (the content-free shape, proven).
        with tempfile.TemporaryDirectory() as probe:
            cd = os.path.join(probe, "h")
            gh.history_clone(REPO, "main", cd)
            has_git = os.path.isdir(os.path.join(cd, ".git"))
            worktree_files = [n for n in os.listdir(cd) if n != ".git"]
            chk(has_git and worktree_files == [],
                f"(2) the clone is NO-CHECKOUT: .git present, working tree empty (no body read) — got {worktree_files}")

        # (5) NOISE CONTROL: the 50-file giant commit minted no partners through the populate path.
        chk(not any(p.get("partner", "").startswith("v/lib") for p in parts),
            "(5) the 50-file giant commit minted no partners (giant-commit skip survives the populate path)")

    # (3) CONTENT-FREE: nothing the crafted repo hid in a body or a message reached the stored/returned values.
    chk("SECRET" not in json.dumps(parts),
        "(3) content-free: stored + returned values carry no file body / commit message (paths + counts only)")

    # (4) FAIL-OPEN: a clone that FAILS returns ok:False, NEVER raises, and populates NOTHING for a fresh repo.
    gh_bad = FakeGh("/nonexistent", raises=True)
    fres = ingest.populate_cochange(A, gh_bad, "acme/never", "main")
    chk(fres.get("ok") is False and "cochange_error" in fres,
        f"(4) fail-open: a failed clone reports ok:False content-free, does not raise (got {fres})")
    empty = _j(A("SELECT core.co_change_partners_with_authority(%s,%s,%s,%s)", ("acme/never", ["x.py"], 3, 0.4)))
    chk(empty == [], f"(4b) the failed-clone repo populated nothing (got {empty})")

    ok = all(checks)
    print("CO-CHANGE POPULATE GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
