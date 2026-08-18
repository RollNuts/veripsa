#!/usr/bin/env python3
"""BASE-RETARGET gate — a PR retargeted ONTO the protected branch must be analyzed, not silently missed.

The audited silent-miss (round-5): GitHub fires ONLY `pull_request.edited` (with `changes.base`) when a PR's
base branch changes — never `synchronize` (no head commit moved). Since `edited` was not an analyze action, a PR
retargeted ONTO main declared NO lanes and got NO check = a real in-flight PR heading to main, invisible. Worse,
a PR opened ON main → retargeted OFF (which tombstones it via release_change_on_main) → retargeted BACK ONTO main
was PERMANENTLY silenced (the concluded-guard skipped the later synchronize).

Proves on the REAL handle_event (FakeGitHub harness + real scratch DB):
  (1) an `edited` retargeting a never-seen PR ONTO main IS analyzed (a check is posted; result is not noop/skip);
  (2) NO over-trigger: a plain `edited` (a title/body edit, no `changes.base`) is still a clean noop;
  (3) the off→back round-trip RE-ACTIVATES: open-on-main → edited-off-main (released+tombstoned) →
      edited-back-onto-main is analyzed again (not permanently silenced).

Run:  python3 tests/test_base_retarget.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from _server_harness import (  # noqa: E402
    DB,
    REPO,
    REPO_ID,
    SHA,
    FakeGitHub,
    make_db,
    pr_payload,
)

sys.path.insert(0, os.path.join(ROOT, "github-app"))
import server as S  # noqa: E402
import webhook_handlers as WH  # noqa: E402

checks = []


def chk(c, label):
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    checks.append(bool(c))


def _edited(number, author, base_ref, base_change=True):
    """A `pull_request.edited` payload. base_change=True adds `changes.base` (a real base retarget); False is a
    plain title/body edit (changes carries no base)."""
    p = pr_payload("edited", number, author)
    p["pull_request"]["base"]["ref"] = base_ref
    p["changes"] = {"base": {"ref": {"from": "feature/x"}, "sha": {"from": "deadbeef"}}} if base_change else {"title": {"from": "old"}}
    return p


def _sha(n):
    return f"{n:040x}"


def _checks_for(gh, n):
    return [c for c in gh.checks if c["sha"] == _sha(n)]


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    db = make_db("veripsa_app")
    sys.path.insert(0, ROOT)
    import code_graph_extract as X
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", SHA))

    gh = FakeGitHub({50: ["backend/api.py"], 51: ["backend/api.py"], 52: ["backend/api.py"]})
    wake_epochs = []

    def durable_wake(_db, repo, branch, target_sha, repository_id, **_kwargs):
        """Faithful ACK for the convergence side effect outside this gate.

        Base-retarget behavior is the subject here; the real durable outbox is
        covered by its own gates.  Still require the signed stable repository
        identity and return the same positive-epoch receipt as production.
        """
        assert (repo, branch, repository_id) == (REPO, "main", REPO_ID)
        wake_epochs.append(len(wake_epochs) + 1)
        return {
            "healed": False,
            "queued": True,
            "reason": "refresh wake recorded",
            "stored_sha": SHA,
            "head_sha": target_sha,
            "queue_epoch": wake_epochs[-1],
        }

    WH.request_main_graph_refresh_wake_only = durable_wake

    # (1) a never-seen PR retargeted ONTO main via `edited` (changes.base, base=main) IS analyzed.
    res = S.handle_event("pull_request", _edited(50, "alice", "main", base_change=True), db, gh)
    chk("noop" not in res and "skipped" not in res and _checks_for(gh, 50),
        f"a base-retarget ONTO main is ANALYZED — a check is posted, not silently missed (res={ {k:res.get(k) for k in ('noop','skipped')} }, checks={len(_checks_for(gh,50))})")

    # (2) NO over-trigger: a plain `edited` (title/body edit, no changes.base) stays a clean noop (no check).
    res_t = S.handle_event("pull_request", _edited(51, "bob", "main", base_change=False), db, gh)
    chk((res_t.get("noop") or "skipped" in res_t) and not _checks_for(gh, 51),
        f"a plain title/body edit (no changes.base) does NOT trigger analysis (res={ {k:res_t.get(k) for k in ('noop','skipped')} }, checks={len(_checks_for(gh,51))})")

    # (3) off→back round-trip RE-ACTIVATES (the tombstone recovery).
    S.handle_event("pull_request", pr_payload("opened", 52, "carol"), db, gh)
    chk(bool(_checks_for(gh, 52)), "round-trip setup: PR opened ON main gets a check")
    off = S.handle_event("pull_request", _edited(52, "carol", "feature/x", base_change=True), db, gh)  # retarget OFF main → released + tombstoned
    chk("skipped" in off, f"retarget OFF main is released + skipped (out of scope) (res={off.get('skipped','')[:40]})")
    back = S.handle_event("pull_request", _edited(52, "carol", "main", base_change=True), db, gh)       # retarget BACK onto main
    chk("noop" not in back and "skipped" not in back,
        f"retarget BACK onto main RE-ACTIVATES (not permanently tombstoned) — analyzed again (res={ {k:back.get(k) for k in ('noop','skipped')} })")

    ok = all(checks)
    print("BASE-RETARGET GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
