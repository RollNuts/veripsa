#!/usr/bin/env python3
"""PAUSE-ACK ACK LIVE-RECOGNITION GATE — a fully synthetic regression for cross-event acknowledgement.

A stable, matching `veripsa-ack` must survive a check-suite or check-run rerequest. The pure overlay and the
integration wiring receive the same label, prior snapshot, and current snapshot inputs on acting and neighbor
paths; content-free decision fields make a mismatch observable without retaining repository provenance.

ROOT CAUSE found by reading the inputs the overlay turns on: an ACKED PR's check re-evaluates not only on its
own `labeled` event (which carries `pull_request.labels[]` → label_present is read correctly off the payload),
but ALSO on a GitHub-auto `check_suite`/`check_run` `rerequested` event — fired whenever CI re-runs OR a human
presses "Re-run all checks" in the merge box. That path is handled by REPLAYING the PR through the live
`synchronize` route via `_rerun_replay(gh.get_pull_request(...))`. The synthesized `pull_request` payload carried
base/head/user/draft/merged but OMITTED `labels` — so the replayed synchronize read `label_present=False` on an
ALREADY-ACKED PR → the overlay re-raised `action_required`, re-pausing the acknowledged PR on every check
re-eval. The ack did NOT stick across the re-run. (`full` is the AUTHORITATIVE PR object whose `labels[]` is the
CURRENT label set, so threading it through restores the same ack recognition a real synchronize has.)

THE FIX (webhook_handlers.py — _rerun_replay, in lane): carry the authoritative PR's `labels[]` into the
synthesized synchronize so a re-run keeps an ack stuck. Content-free (label names we chose / the customer set,
never code); degrades to [] for an older client (no worse than before, and the next real event re-derives state).

WHAT THIS GATE PROVES, on the REAL handle_event router (the rerequested check path -> _rerun_replay ->
handle_pull_request -> apply_pause_ack ACTING path, AND the _post_refreshes NEIGHBOR path) over a SINGLE shared
non-autocommit connection authed as the REAL least-privilege role veripsa_app (the exact prod model):

  SETUP    two PRs collide on the SAME file → both MATERIAL (serialize); ack PR-A (label present + matching prior
           hash) → neutral, a snapshot marker stored, label NOT stripped.
  RERUN-1  DEFENSE-IN-DEPTH (updated by the authoritative-label fix, the PR after #306): a GitHub-auto
           `check_suite` rerequested re-eval of the ACKED PR, replayed through the OLD LABEL-DROPPING _rerun_replay
           (the #306 bug, restored verbatim) → the acked PR STILL keeps the ack. The ACTING overlay now reads the
           label AUTHORITATIVELY (gh.pr_labels), so a label-dropping replay no longer makes it read
           label_present=False — the payload fragility is closed at the source. The #306 replay-carries-labels
           mechanism and the authoritative read are now INDEPENDENT guarantees (the rerun is robust even if a
           future change re-breaks the replay's label threading). → STAYS neutral, label NOT stripped.
  RERUN-2  THE FIX: the SAME `check_suite` rerequested through the REAL (fixed) _rerun_replay (labels carried) →
           the acked PR STAYS neutral and the label is NOT stripped.
  NEIGHBOR a NEIGHBOR PR's sync mutates ZERO sibling surfaces and records durable convergence; one explicitly
           isolated _post_refreshes posting slice re-renders the acked PR → the ack still holds (neutral,
           label NOT stripped).
  NOOP     a subsequent clean no-op rerun re-eval keeps the ack neutral (it STAYS acked end-to-end).
  SAFETY   a check_suite re-eval of a PR whose coupling GENUINELY changed (a real different partner+file, label
           still on) is STILL detected stale → action_required again + the label removed (the fix carries the
           label so a real stale strip still fires; it does not over-fix into "ack never expires").

Run:  python3 tests/test_pauseack_ack_live_recognition.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)
import server as S              # noqa: E402  (re-exports handle_event / _scoped_db / the event caps)
import render as R              # noqa: E402  (prior_snapshot_from_comment / coupling_snapshot)
import webhook_handlers as WH   # noqa: E402  (handle_event router + _rerun_replay — the wiring under test)

DB = "veripsa_acklive_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"          # the REAL prod least-privilege role
INSTALL_ID = 9393
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")
SHA_MAIN = "1ced1234" * 5
COLLIDE_PATH = "backend/api.py"                              # the acked PR + the neighbor PR BOTH touch this → MATERIAL
OTHER_PATH = "backend/auth.py"                               # a DIFFERENT file (for the genuinely-changed-coupling safety case)
ACK_LABEL = "veripsa-ack"

# RLS-isolated accounts/repos so the repro run never leaks claims into the fix run on the same bootstrapped DB.
REPRO_ACCOUNT, REPRO_REPO = 818181, "acklive/repro-app"
FIX_ACCOUNT, FIX_REPO = 828282, "acklive/fix-app"
MIS_ACCOUNT, MIS_REPO = 838383, "acklive/mismatch-app"


def _repo_id(repo: str) -> int:
    return 300_000 + sum((index + 1) * ord(char) for index, char in enumerate(repo))


# ── the PRE-FIX _rerun_replay (the bug), restored verbatim: it omits `labels`, so the replayed synchronize reads
#    label_present=False on an already-acked PR and re-raises action_required. Used only for the RERUN-1 control. ──
def _prefix_rerun_replay(full, repo, default_branch, full_base, number):
    full = full if isinstance(full, dict) else {}
    base_obj = full.get("base") if isinstance(full.get("base"), dict) else {}
    return {
        "action": "synchronize",
        "number": number,
        "repository": {"id": _repo_id(repo), "full_name": repo,
                       "default_branch": default_branch},
        "pull_request": {
            "base": {"ref": full_base, "sha": base_obj.get("sha"),
                     "repo": base_obj.get("repo") if isinstance(base_obj.get("repo"), dict) else {}},
            "head": full.get("head") if isinstance(full.get("head"), dict) else {},
            "user": full.get("user") if isinstance(full.get("user"), dict) else {},
            "draft": bool(full.get("draft")),
            "merged": bool(full.get("merged")),
            # NO "labels" — the bug.
        },
        "_veripsa_no_neighbor_refresh": True,
    }


class LiveGitHub:
    """Recording fake. Serves the sample_app fixture so a push builds a REAL main graph; tracks labels +
    removals so we can read whether a present ack stuck or was stripped. get_pull_request returns the CURRENT
    label set (the authoritative object _rerun_replay reads), so the fix can carry the label through a re-run."""

    def __init__(self, account_id):
        self.account_id = account_id
        self.checks, self.comments = [], []
        self._cid, self._chid = 1000, 2000
        self.labels = {}            # pr_number -> [label name]
        self.removed = []           # (pr_number, label) on remove_label
        self.check_writes = []      # PR numbers whose checks were posted/patched
        self.comment_writes = []    # PR numbers whose comments were posted/patched
        self.open_prs = []

    def for_installation(self, installation_id):
        return self

    def installation_account_id(self):
        return str(self.account_id)

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return [COLLIDE_PATH]

    def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
        files = list(self.list_pr_files(repo, number))
        return {"changed": files, "changed_ranges": {p: [] for p in files},
                "added_paths": [], "conflict_markers": [], "raw_entry_count": len(files)}

    def post_check(self, repo, sha, conclusion, title, summary):
        self.check_writes.append(int(str(sha), 16))
        self._chid += 1
        self.checks.append({"id": self._chid, "sha": sha, "conclusion": conclusion, "title": title,
                            "summary": summary, "name": "Veripsa"})

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha and c.get("name") == "Veripsa"]

    def patch_check(self, repo, check_run_id, conclusion, title, summary):
        for c in self.checks:
            if c["id"] == check_run_id:
                self.check_writes.append(int(str(c["sha"]), 16))
                c.update({"conclusion": conclusion, "title": title, "summary": summary})
                return c
        raise AssertionError(f"check not found: {check_run_id}")

    def upsert_check(self, repo, sha, conclusion, title, summary):
        existing = self.list_check_runs(repo, sha)
        if existing:
            return self.patch_check(repo, existing[0]["id"], conclusion, title, summary)
        return self.post_check(repo, sha, conclusion, title, summary)

    def post_comment(self, repo, number, body):
        self.comment_writes.append(number)
        self._cid += 1
        self.comments.append({"id": self._cid, "number": number, "body": body, "user": {"type": "Bot"}})

    def _all_comments(self, number):
        return [c for c in self.comments if c["number"] == number]

    def list_issue_comments(self, repo, number):
        return self._all_comments(number)

    def patch_comment(self, repo, comment_id, body):
        for c in self.comments:
            if c["id"] == comment_id:
                self.comment_writes.append(c["number"])
                c["body"] = body
                return c
        raise AssertionError(f"comment not found: {comment_id}")

    def upsert_comment(self, repo, number, marker, body):
        for c in self._all_comments(number):
            if marker in c["body"] or (c["body"].startswith("### Veripsa") and c.get("user", {}).get("type") == "Bot"):
                return self.patch_comment(repo, c["id"], body)
        return self.post_comment(repo, number, body)

    def patch_comment_if_exists(self, repo, number, marker, body):
        for c in self._all_comments(number):
            if marker in c["body"]:
                self.patch_comment(repo, c["id"], body() if callable(body) else body)
                return True
        return False

    def list_open_pull_requests(self, repo, limit=None):
        return list(self.open_prs)

    def pull_request_head(self, repo, number):
        return f"{number:040x}"

    def pull_request_head_and_fork(self, repo, number):
        return f"{number:040x}", False

    def get_pull_request(self, repo, number):
        # the AUTHORITATIVE PR object — carries the CURRENT label set (what _rerun_replay must thread through).
        repo_id = _repo_id(repo)
        return {"number": number, "base": {"ref": "main", "sha": SHA_MAIN,
                                           "repo": {"id": repo_id}},
                "head": {"sha": f"{number:040x}", "repo": {"id": repo_id},
                         "ref": f"feature/{number}"},
                "user": {"login": "dev"}, "draft": False, "state": "open", "merged": False,
                "changed_files": len(self.list_pr_files(repo, number)),
                "labels": [{"name": n} for n in self.labels.get(number, [])]}

    def pr_labels(self, repo, number, strict=False):
        return list(self.labels.get(number, []))

    def remove_label(self, repo, number, name):
        self.removed.append((number, name))
        self.labels.setdefault(number, [])
        if name in self.labels[number]:
            self.labels[number].remove(name)
        return True

    def repo_default_branch_head(self, repo):
        return "main", SHA_MAIN

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(FIXTURE, arcname="live-" + sha[:7])
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


def _proc(account, gh):
    """event_processor's TRANSACTION MODEL (ONE shared non-autocommit connection, enter_installation pinned, one
    commit at the end) — the exact prod model — authed as veripsa_app."""
    def proc(event_type, payload):
        conn = psycopg2.connect(DSN_APP)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(account),))
            conn.autocommit = False
            db = S._scoped_db(conn)
            try:
                r = S.handle_event(event_type, payload, db, gh)
                conn.commit()
                return r
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
        finally:
            conn.close()
    return proc


def _isolated_convergence(account, repo, gh, refreshes):
    """Run one real off-webhook posting slice with its own scoped transaction."""
    conn = psycopg2.connect(DSN_APP)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(account),))
        conn.autocommit = False
        db = S._scoped_db(conn)
        try:
            progress = S._post_refreshes(
                gh, repo, refreshes or [], db=db, branch="main", return_progress=True)
            conn.commit()
            return progress
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _push_main(account, repo, sha):
    return {"ref": "refs/heads/main", "after": sha,
            "installation": {"id": INSTALL_ID, "account": {"id": account}},
            "repository": {"id": _repo_id(repo), "full_name": repo,
                           "default_branch": "main", "owner": {"id": account}},
            "pusher": {"name": "dev"},
            "commits": [{"added": [COLLIDE_PATH], "modified": [], "removed": []}]}


def _pr(action, account, repo, number, label_names=None, files=None):
    head = f"{number:040x}"
    repo_id = _repo_id(repo)
    p = {"action": action, "number": number,
         "installation": {"id": INSTALL_ID, "account": {"id": account}},
         "repository": {"id": repo_id, "full_name": repo, "default_branch": "main",
                        "owner": {"id": account}},
         "pull_request": {"base": {"ref": "main", "sha": SHA_MAIN,
                                    "repo": {"id": repo_id}},
                          "head": {"sha": head, "repo": {"id": repo_id},
                                   "ref": f"feature/{number}"},
                          "user": {"login": "dev"}, "merged": False,
                          "labels": [{"name": n} for n in (label_names or [])]}}
    if action in ("labeled", "unlabeled"):
        p["label"] = {"name": ACK_LABEL}
    return p


def _check_suite_rerequested(account, repo, pr_number):
    """A GitHub-auto check_suite `rerequested` event for one PR — what fires on a CI re-run / merge-box
    "Re-run all checks". It carries the associated PR as a SPARSE pointer (number + base) — NO labels (GitHub
    does not embed them) — so the handler must re-fetch the authoritative PR (get_pull_request) to recover the
    ack label. This is the exact event the ack failed to stick across."""
    return {"action": "rerequested",
            "installation": {"id": INSTALL_ID, "account": {"id": account}},
            "repository": {"id": _repo_id(repo), "full_name": repo,
                           "default_branch": "main", "owner": {"id": account}},
            "check_suite": {"head_sha": f"{pr_number:040x}",
                            "pull_requests": [{"number": pr_number, "base": {"ref": "main"}}]}}


def _snap_in_comment(gh, number):
    body = (gh._all_comments(number) or [{}])[-1].get("body", "")
    return R.prior_snapshot_from_comment(body)


def _concl(gh, number):
    c = gh.list_check_runs("", f"{number:040x}")
    return c[-1]["conclusion"] if c else None


def _ack_two_prs(account, repo, acked_pr, partner_pr, collide_path=COLLIDE_PATH):
    """Seed main's graph; open the partner PR then the acked PR on the SAME path → both MATERIAL (serialize);
    ack the acked PR (label + matching hash) → neutral. Returns (gh, proc) with acked_pr ACKED + neutral and a
    snapshot marker stored."""
    gh = LiveGitHub(account)
    # the acked PR + the partner both touch collide_path so they MATERIALLY collide.
    gh.list_pr_files = lambda r, n: [collide_path]
    proc = _proc(account, gh)
    proc("push", _push_main(account, repo, SHA_MAIN))
    gh.open_prs = [{"number": acked_pr}, {"number": partner_pr}]
    proc("pull_request", _pr("opened", account, repo, partner_pr))     # partner enters first
    proc("pull_request", _pr("opened", account, repo, acked_pr))       # acked_pr now paused (material), marker written
    assert _concl(gh, acked_pr) == "action_required", \
        f"setup: PR-{acked_pr} should be paused, got {_concl(gh, acked_pr)!r}"
    assert _snap_in_comment(gh, acked_pr), f"setup: PR-{acked_pr} must carry a snapshot marker"
    # ACK the PR — label + the matching prior hash → neutral (acknowledged) on the ACTING (labeled) path.
    gh.labels[acked_pr] = [ACK_LABEL]
    proc("pull_request", _pr("labeled", account, repo, acked_pr, [ACK_LABEL]))
    assert _concl(gh, acked_pr) == "neutral", \
        f"precondition: PR-{acked_pr} must be ACKED (neutral) before the re-eval, got {_concl(gh, acked_pr)!r}"
    assert (acked_pr, ACK_LABEL) not in gh.removed, "precondition: the valid ack must not be stripped on the acting path"
    return gh, proc


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    checks = []

    # ── RERUN-1 DEFENSE-IN-DEPTH (updated by the authoritative-label fix, PR-after-#306): even with the OLD
    #    LABEL-DROPPING _rerun_replay installed (the #306 bug, restored verbatim), the ACTING overlay now reads the
    #    label AUTHORITATIVELY (gh.pr_labels), so a label-dropping replay no longer loses the ack — it HOLDS. The
    #    payload fragility this test originally reproduced (the synthesized synchronize read label_present=False off
    #    the dropped payload labels) is closed AT THE SOURCE: the acting path no longer trusts the per-event payload
    #    labels at all. So the #306 mechanism (replay carries labels) and the authoritative read are independent
    #    guarantees, and the rerun is robust EVEN IF a future change re-breaks _rerun_replay's label threading. ──────
    gh_r, proc_r = _ack_two_prs(REPRO_ACCOUNT, REPRO_REPO, 10, 11)
    real_rerun_replay = WH._rerun_replay
    WH._rerun_replay = _prefix_rerun_replay                # the OLD label-dropping replay — must NO LONGER lose the ack
    try:
        proc_r("check_suite", _check_suite_rerequested(REPRO_ACCOUNT, REPRO_REPO, 10))
    finally:
        WH._rerun_replay = real_rerun_replay
    repro_concl = _concl(gh_r, 10)
    repro_stripped = (10, ACK_LABEL) in gh_r.removed
    checks.append(("RERUN-1 DEFENSE-IN-DEPTH: a GitHub-auto check_suite rerequested re-eval of an ACKED PR replayed "
                   "through the OLD label-DROPPING _rerun_replay (the #306 bug restored verbatim) STILL keeps the ack "
                   "— the ACTING overlay now reads the label AUTHORITATIVELY (gh.pr_labels), so the dropped payload "
                   "labels no longer make it read label_present=False; the acked PR stays neutral, label NOT stripped "
                   "(the payload fragility is closed at the source — rerun robustness no longer depends on the replay)",
                   repro_concl == "neutral" and not repro_stripped))
    print(f"  [defense] check_suite re-eval PR-10 (OLD label-dropping replay + authoritative read): "
          f"conclusion={repro_concl!r} stripped={repro_stripped} (expect neutral + NOT stripped — ack survives anyway)")

    # ── RERUN-2 THE FIX: the SAME check_suite rerequested through the REAL (fixed) _rerun_replay → the ack STICKS. ─
    gh_f, proc_f = _ack_two_prs(FIX_ACCOUNT, FIX_REPO, 20, 21)
    proc_f("check_suite", _check_suite_rerequested(FIX_ACCOUNT, FIX_REPO, 20))
    fix_concl = _concl(gh_f, 20)
    fix_stripped = (20, ACK_LABEL) in gh_f.removed
    checks.append(("RERUN-2 FIXED: the SAME check_suite rerequested re-eval through the real _rerun_replay (which "
                   "now carries the authoritative PR's labels) KEEPS the ack — the acked PR stays neutral and the "
                   "label is NOT stripped",
                   fix_concl == "neutral" and not fix_stripped))
    print(f"  [fix] check_suite re-eval PR-20 (real replay): conclusion={fix_concl!r} stripped={fix_stripped} "
          f"(expect neutral + NOT stripped)")

    # ── NEIGHBOR: a NEIGHBOR PR's sync records durable convergence and mutates ZERO sibling surfaces; one
    #    isolated real _post_refreshes posting slice re-renders the acked PR → the ack still holds. ────────
    nb_check_before = sum(n == 20 for n in gh_f.check_writes)
    nb_comment_before = sum(n == 20 for n in gh_f.comment_writes)
    nb_event = proc_f("pull_request", _pr("synchronize", FIX_ACCOUNT, FIX_REPO, 21))
    nb_inline_untouched = (sum(n == 20 for n in gh_f.check_writes) == nb_check_before
                           and sum(n == 20 for n in gh_f.comment_writes) == nb_comment_before)
    nb_progress = _isolated_convergence(
        FIX_ACCOUNT, FIX_REPO, gh_f, nb_event.get("refreshed") or [])
    nb_concl = _concl(gh_f, 20)
    nb_stripped = (20, ACK_LABEL) in gh_f.removed
    nb_surface_updated = (sum(n == 20 for n in gh_f.check_writes) > nb_check_before
                          and sum(n == 20 for n in gh_f.comment_writes) > nb_comment_before)
    checks.append(("NEIGHBOR FIXED: a neighbor PR's live sync mutates ZERO PR-20 surface and reports the sibling "
                   "deferred; one isolated real _post_refreshes slice then updates PR-20 and the ack STILL holds "
                   "(neutral, label NOT stripped)",
                   nb_inline_untouched and nb_event.get("refreshed_inflight") == 0
                   and nb_event.get("refresh_deferred", 0) >= 1
                   and nb_progress.get("posted", 0) >= 1 and nb_progress.get("errors") == 0
                   and nb_concl == "neutral" and not nb_stripped and nb_surface_updated))
    print(f"  [neighbor] PR-21 sync deferred PR-20; isolated slice progress={nb_progress!r}: "
          f"conclusion={nb_concl!r} stripped={nb_stripped} (expect neutral + NOT stripped)")

    # ── NOOP: a subsequent clean no-op rerun re-eval keeps the ack neutral (it STAYS acked end-to-end). ────────────
    proc_f("check_suite", _check_suite_rerequested(FIX_ACCOUNT, FIX_REPO, 20))
    noop_concl = _concl(gh_f, 20)
    noop_stripped = (20, ACK_LABEL) in gh_f.removed
    checks.append(("NOOP FIXED: a subsequent clean no-op check_suite re-eval keeps the ack neutral (it STAYS acked "
                   "end-to-end across the acting, re-run AND neighbor paths)",
                   noop_concl == "neutral" and not noop_stripped))
    print(f"  [noop] second check_suite re-eval PR-20: conclusion={noop_concl!r} stripped={noop_stripped} "
          f"(expect neutral + NOT stripped)")

    # ── SAFETY: a check_suite re-eval of a PR whose coupling GENUINELY changed (a real different partner+file, label
    #    still on) is STILL detected stale → action_required + the label removed. Drive the ACTING path: ack a
    #    coupling, overwrite the stored marker with a hash bound to a DIFFERENT coupling, then a check_suite re-eval
    #    correctly re-raises + strips (the fix carries the label, so a real stale strip still fires). ────────────────
    gh_m, proc_m = _ack_two_prs(MIS_ACCOUNT, MIS_REPO, 30, 31)
    stored = _snap_in_comment(gh_m, 30)
    assert stored, "setup: PR-30 must carry a snapshot marker"
    bogus = R.coupling_snapshot({"verdict": "serialize", "serialize_behind": ["someoneelse PR-999"],
                                 "paths": [OTHER_PATH],
                                 "collision_points": [{"path": OTHER_PATH, "symbol": "other_fn"}]})
    assert bogus != stored, "setup: the bogus coupling must differ from the stored one"
    for c in gh_m.comments:
        if c["number"] == 30:
            c["body"] = c["body"].replace(f"veripsa-ack-snap:{stored}", f"veripsa-ack-snap:{bogus}")
    proc_m("check_suite", _check_suite_rerequested(MIS_ACCOUNT, MIS_REPO, 30))
    mis_concl = _concl(gh_m, 30)
    mis_stripped = (30, ACK_LABEL) in gh_m.removed
    checks.append(("SAFETY STALE-STRIP PRESERVED: a check_suite re-eval of a PR whose ack is bound to a GENUINELY "
                   "different coupling (a real material change, not a non-confirmable read) is STILL detected stale "
                   "→ action_required AGAIN + the label removed (the fix carries the label so a real stale strip "
                   "still fires — it does not over-fix into 'ack never expires')",
                   mis_concl == "action_required" and mis_stripped))
    print(f"  [safety] genuinely-mismatched PR-30 check_suite re-eval: conclusion={mis_concl!r} "
          f"stripped={mis_stripped} (expect action_required + stripped)")

    print("\n── PAUSE-ACK ACK LIVE RECOGNITION ───────────────────────────")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("\nPAUSE ACK LIVE RECOGNITION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
    sys.exit(rc)
