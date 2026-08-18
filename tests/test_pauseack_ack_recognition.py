#!/usr/bin/env python3
"""PAUSE-ACK ACK-RECOGNITION GATE — a PRESENT, MATCHING `veripsa-ack` label must remain acknowledged.

The fully synthetic regression fixture proves that a stable embedded coupling snapshot is not re-paused and
that the App does not strip the label without a positively proven coupling change.

ROOT CAUSE (in the INTEGRATION/WIRING — the pure render.apply_pause_ack UNIT gate already passes): the coupling
SNAPSHOT an ack binds to was UNSTABLE across re-renders of the SAME coupling, so a valid ack read STALE on the
`labeled` event → the pause re-raised + the label was slated for removal. Two instabilities, both reproduced here:

  (a) FRESHNESS-DEPENDENT collision LOCUS. _ack_coupling_paths keyed on the FINER 'path::symbol' locus when the
      main graph was provably FRESH for the file, and the bare 'path' when it was STALE. Graph freshness FLIPS
      between events on the same PR (a missed push lands; boot/self-heal re-ingests main). So the SAME file
      collision hashed DIFFERENTLY depending only on graph freshness at the moment of the event: the comment was
      written under a stale graph (file-level hash) and the `labeled` event recomputed under a fresh graph
      (symbol-level hash) → mismatch → the matching ack read STALE → the label was stripped. The synthetic fixture
      below proves that the pre-fix helper changes hash while the fixed helper stays stable.
  (b) A NON-PR partner SILENTLY DROPPED. _ack_partner_refs pulled ONLY 'PR-<n>'/'BR-<x>' ref tokens out of the
      humanized partner labels — but a BR- branch push that has not yet reconciled to a PR renders AUTHOR-NAME-ONLY
      (e.g. 'synthetic-author', NO ref token), so such a partner was dropped from the coupling identity entirely.
      A dropped partner makes the
      identity incomplete and the hash unstable as a partner flips BR- (dropped) ↔ PR- (counted).

THE FIX (render.py, in lane — the wiring that computes the snapshot the overlay binds an ack to):
  • _ack_coupling_paths binds to the FILE only (drop the freshness-dependent symbol qualifier) → a graph-freshness
    flip can no longer restale a valid ack.
  • _ack_partner_refs takes the ref token when present, ELSE the whole humanized label → a BR- (author-only)
    partner is NEVER dropped → the partner set is complete and stable across a BR-/PR- flip.

WHAT THIS GATE PROVES, on the REAL handle_event -> handle_pull_request -> apply_pause_ack LABELED path, over a
SINGLE shared non-autocommit connection authed as the REAL least-privilege role veripsa_app (the exact prod model):

  ACK-1  REPRODUCE-THE-BUG (negative control): with the PRE-FIX snapshot helpers restored AND a graph-freshness
         flip injected between the comment-write event and the `labeled` event (the prod stimulus), a PRESENT,
         MATCHING ack reads STALE -> the check STAYS action_required AND the App STRIPS the label. The prod failure,
         reproduced deterministically on the real wiring.
  ACK-2  THE FIX: with the FIXED helpers (origin/main + this fix), the SAME present+matching label over the SAME
         freshness flip clears the pause to `neutral` (acknowledged) and the label is NOT stripped.
  ACK-3  STALE-STRIP STILL FIRES ONLY ON A REAL MISMATCH: a GENUINELY different coupling (a different partner on a
         different file) with the label still on is correctly detected stale -> action_required again + the label
         removed (the safety behavior is preserved — the fix does not blanket-suppress stale detection).

Run:  python3 tests/test_pauseack_ack_recognition.py   (needs local Postgres with the veripsa roles)
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
import server as S              # noqa: E402  (re-exports handle_event / _scoped_db)
import render as R              # noqa: E402  (the snapshot helpers under test — apply_pause_ack binds to them)
import webhook_handlers as WH   # noqa: E402  (the acting-PR overlay; it bound `apply_pause_ack` by name at import)
# coupling_snapshot / apply_pause_ack now live in render_pauseack.py (split out of render.py; render re-exports
# them). Their INTRA-module calls to _ack_coupling_paths / _ack_partner_refs resolve in THAT module's namespace,
# so the pre-fix negative-control below must inject the buggy helpers THERE — patching render's re-exported alias
# would not reach the intra-module call. Resolve the home module FROM the function so this stays correct whether
# the helper lives in render (pre-split) or render_pauseack (post-split), and across any future move.
_PA = sys.modules[R.coupling_snapshot.__module__]   # noqa: E402

DB = "veripsa_pauseack_ack_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"          # the REAL prod least-privilege role
INSTALL_ID = 9191
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")
SHA_MAIN = "abcd1234" * 5
COLLIDE_PATH = "backend/api.py"                              # the acting PR + a real PR- PARTNER both touch this → MATERIAL serialize (a non-release BR- push here is advisory-only, #851 gen 8)
ACK_LABEL = "veripsa-ack"

# Each scenario runs in its OWN account+repo (RLS-isolated, fresh lanes) so a repro run never leaks claims into
# the fix run on the same bootstrapped DB.
BUG_ACCOUNT, BUG_REPO = 616161, "ack/bug-app"
FIX_ACCOUNT, FIX_REPO = 626262, "ack/fix-app"
MIS_ACCOUNT, MIS_REPO = 636363, "ack/mismatch-app"


# ── the PRE-FIX snapshot helpers (the bug), restored verbatim for the negative control ───────────────────────
def _prefix_ack_coupling_paths(me: dict) -> list[str]:
    """PRE-FIX: keyed on the FINER 'path::symbol' locus when present (freshness-dependent) → unstable across a
    graph-freshness flip on the same coupling."""
    out: set = set()
    for cp in (me.get("collision_points") or []):
        if not isinstance(cp, dict):
            continue
        sym, path = cp.get("symbol"), cp.get("path")
        if isinstance(sym, str) and sym:
            out.add((f"{path}::" if isinstance(path, str) and path else "") + sym)
        elif isinstance(path, str) and path:
            out.add(path)
    for p in (me.get("paths") or []):
        if isinstance(p, str) and p:
            out.add(p)
    return sorted(out)


def _prefix_ack_partner_refs(me: dict) -> list[str]:
    """PRE-FIX: pulls ONLY 'PR-<n>'/'BR-<x>' ref tokens → an author-only BR- partner is silently dropped."""
    refs: set = set()
    for key in ("serialize_behind", "queued_behind", "contested_with"):
        for entry in (me.get(key) or []):
            if isinstance(entry, dict):
                entry = entry.get("change_id") or entry.get("label") or entry.get("agent") or ""
            if isinstance(entry, str):
                refs.update(R._ACK_REF_RE.findall(entry))
    return sorted(refs)


def _repo_id(repo: str) -> int:
    """Stable positive GitHub repository id for each synthetic coordinate."""
    return 100_000 + sum((index + 1) * ord(char) for index, char in enumerate(repo))


class AckGitHub:
    """Recording fake. Serves the sample_app fixture so the push builds a REAL main graph; tracks labels +
    label removals so we can read whether a present ack cleared the pause or was stripped. `_name_symbol`
    toggles the FRESHNESS FLIP: when True, collision_points carry a SYMBOL (a fresh graph names the locus);
    when False, they stay file-level — the exact stimulus that stranded the prod ack."""

    def __init__(self, account_id):
        self.account_id = account_id
        self.checks, self.comments = [], []
        self.head_sha = SHA_MAIN
        self._cid, self._chid = 1000, 2000
        self.labels = {}            # pr_number -> [label name]
        self.removed = []           # (pr_number, label) on remove_label
        self.open_prs = []

    def for_installation(self, installation_id):
        return self

    def installation_account_id(self):
        return str(self.account_id)

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return [COLLIDE_PATH]

    def post_check(self, repo, sha, conclusion, title, summary):
        self._chid += 1
        self.checks.append({"id": self._chid, "sha": sha, "conclusion": conclusion, "title": title,
                            "summary": summary, "name": "Veripsa"})

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
        return list(self.open_prs)

    def pull_request_head(self, repo, number):
        return f"{number:040x}"

    def get_pull_request(self, repo, number):
        repo_id = _repo_id(repo)
        return {"number": number, "base": {"ref": "main", "sha": self.head_sha,
                                           "repo": {"id": repo_id}},
                "head": {"sha": f"{number:040x}", "repo": {"id": repo_id},
                         "ref": f"feature/{number}"},
                "user": {"login": "dev"}, "draft": False, "merged": False,
                "labels": [{"name": n} for n in self.labels.get(number, [])],
                "state": "open", "changed_files": 1}

    def pr_labels(self, repo, number, strict=False):
        return list(self.labels.get(number, []))

    def remove_label(self, repo, number, name):
        self.removed.append((number, name))
        self.labels.setdefault(number, [])
        if name in self.labels[number]:
            self.labels[number].remove(name)
        return True

    def repo_default_branch_head(self, repo):
        return "main", self.head_sha

    def list_repo_branch_names(self, repo, limit=None):
        # This gate intentionally exercises a GENUINE live BR coupling. The production client now verifies that
        # truth before allowing it into the pause/ACK snapshot, so the fake must expose the live branch inventory
        # it seeded through _push_branch (rather than looking like an API outage / honest-unknown case).
        return ["main", "feature-partner"][:limit]

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(FIXTURE, arcname="ack-" + sha[:7])
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
    """event_processor.make_db_processor's TRANSACTION MODEL (ONE shared non-autocommit connection,
    enter_installation pinned, one commit at the end) — the exact prod model — authed as veripsa_app."""
    def proc(event_type, payload):
        conn = psycopg2.connect(DSN_APP)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(account),))
            conn.autocommit = False                 # BODY: one transaction (the prod model)
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


def _push_main(account, repo, sha):
    return {"ref": "refs/heads/main", "after": sha,
            "installation": {"id": INSTALL_ID, "account": {"id": account}},
            "repository": {"id": _repo_id(repo), "full_name": repo,
                           "default_branch": "main", "owner": {"id": account}},
            "pusher": {"name": "dev"},
            "commits": [{"added": [COLLIDE_PATH], "modified": [], "removed": []}]}


def _push_branch(account, repo, branch, pusher):
    """A push to a NON-main feature branch — reserves BR-<branch> lanes on main (the partner that renders
    AUTHOR-NAME-ONLY, e.g. 'synthetic-author', with NO PR ref — the regression shape)."""
    return {"ref": f"refs/heads/{branch}",
            "after": f"{sum(ord(c) for c in branch) + 5000:040x}",
            "installation": {"id": INSTALL_ID, "account": {"id": account}},
            "repository": {"id": _repo_id(repo), "full_name": repo,
                           "default_branch": "main", "owner": {"id": account}},
            "pusher": {"name": pusher},
            "commits": [{"added": [], "modified": [COLLIDE_PATH], "removed": []}]}


def _pr(action, account, repo, number, label_names=None):
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
    if action == "labeled":
        p["label"] = {"name": ACK_LABEL}
    return p


def _snap_in_comment(gh, repo, number):
    body = (gh.list_issue_comments(repo, number) or [{}])[-1].get("body", "")
    return R.prior_snapshot_from_comment(body)


def _concl(gh, number):
    c = gh.list_check_runs("", f"{number:040x}")  # repo unused in the fake's keying
    return c[-1]["conclusion"] if c else None


# The FRESHNESS FLIP, injected through apply_pause_ack's `impact` by wrapping main_impact_surface's per-change
# rows: on the second (labeled) event we ADD a `symbol` to the colliding change's collision_points (a fresh graph
# names the locus). The pure helpers under test read THIS — so the pre-fix path's recompute differs from the
# file-level hash stored in the comment on the first event; the fixed path's recompute does not.
_name_symbol = {"on": False}
_orig_apply_pause_ack = WH.apply_pause_ack             # the binding the acting-PR overlay actually calls


def _flipping_apply_pause_ack(rendered, impact, change_ref, **kw):
    if _name_symbol["on"] and isinstance(impact, dict):
        for ch in (impact.get("changes") or []):
            if not isinstance(ch, dict):
                continue
            for cp in (ch.get("collision_points") or []):
                if isinstance(cp, dict) and cp.get("path") == COLLIDE_PATH and not cp.get("symbol"):
                    cp["symbol"] = "serve_request"     # the fresh graph names the colliding symbol on this event
    return _orig_apply_pause_ack(rendered, impact, change_ref, **kw)


def _run_labeled_scenario(account, repo, pr_number):
    """Seed main's graph; push a BR- branch on the collide path (the synthetic author-only advisory partner);
    open a real PR- PARTNER first on the SAME path (the effective merge-queue LEADER), then open
    the acting PR on that path so it HARD-serializes behind the PR- partner (a real PR-caused pause →
    action_required + the snapshot marker comment). #851 ROOT MODEL (gen 8): a non-release BR- reservation is
    advisory-only, so the pause is now driven by the PR- partner while the BR- rides ALONGSIDE as advisory
    context in the same coupling (its author-only 'synthetic-author' is still surfaced in the acting PR's collision
    points — the exact author-only-partner shape part (b) guards, proven directly in the pure-function anchor).
    Add the ack label with the FRESHNESS FLIP armed; fire the `labeled` event. Return (conclusion, stripped, gh)."""
    partner = pr_number - 1                                                    # the real PR- leader (opens first → earliest claim)
    gh = AckGitHub(account)
    proc = _proc(account, gh)
    proc("push", _push_main(account, repo, SHA_MAIN))
    proc("push", _push_branch(account, repo, "feature-partner", "example-user"))   # advisory BR- partner on the collide path (author-only label)
    gh.open_prs = [{"number": partner}, {"number": pr_number}]
    proc("pull_request", _pr("opened", account, repo, partner))               # PR- partner opens FIRST → effective leader on the collide path
    proc("pull_request", _pr("opened", account, repo, pr_number))             # acting PR opens SECOND → HARD serialize behind PR-<partner>; comment written file-level (stale graph)
    # the PR must be paused with a snapshot marker (the precondition for an ack to bind)
    assert _concl(gh, pr_number) == "action_required", f"setup: PR-{pr_number} should be paused, got {_concl(gh, pr_number)!r}"
    assert _snap_in_comment(gh, repo, pr_number), f"setup: PR-{pr_number} comment must carry a snapshot marker"

    # add the ack label, arm the freshness flip, fire the labeled event (the prod moment)
    gh.labels[pr_number] = [ACK_LABEL]
    _name_symbol["on"] = True
    try:
        proc("pull_request", _pr("labeled", account, repo, pr_number, [ACK_LABEL]))
    finally:
        _name_symbol["on"] = False
    stripped = (pr_number, ACK_LABEL) in gh.removed
    return _concl(gh, pr_number), stripped, gh


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    checks = []
    WH.apply_pause_ack = _flipping_apply_pause_ack               # arm the freshness flip on the (labeled) overlay event

    # ── PURE-FUNCTION ANCHOR: a fully synthetic coupling changes under the PRE-FIX helper when only graph
    # freshness adds a symbol, while the FIXED helper keeps the two identities equal. ──────────────────────────────
    me_stale = {"verdict": "serialize", "serialize_behind": ["synthetic-author"], "queued_behind": [],
                "contested_with": [], "paths": ["src/handlers.py", "tests/test_handlers.py"],
                "collision_points": [{"behind": "synthetic-author", "path": "src/handlers.py", "symbol": None}]}
    me_fresh = dict(me_stale, collision_points=[{"behind": "synthetic-author", "path": "src/handlers.py",
                                                 "symbol": "handle_request"}])

    def _prefix_snapshot(me):
        """Compute a snapshot with the PRE-FIX helpers against only synthetic inputs."""
        f, g = _PA._ack_coupling_paths, _PA._ack_partner_refs
        try:
            _PA._ack_coupling_paths, _PA._ack_partner_refs = _prefix_ack_coupling_paths, _prefix_ack_partner_refs
            return R.coupling_snapshot(me)
        finally:
            _PA._ack_coupling_paths, _PA._ack_partner_refs = f, g

    checks.append(("ANCHOR: the synthetic PRE-FIX file-level coupling hash is deterministic and bounded",
                   _prefix_snapshot(me_stale) == _prefix_snapshot(dict(me_stale))
                   and len(_prefix_snapshot(me_stale)) == 12))
    checks.append(("ANCHOR: under the PRE-FIX helper the SAME coupling restales on a freshness flip (file-level "
                   "hash != symbol-level hash) — the bug; the FIXED helper makes the two hashes IDENTICAL (the fix)",
                   _prefix_snapshot(me_stale) != _prefix_snapshot(me_fresh)
                   and R.coupling_snapshot(me_stale) == R.coupling_snapshot(me_fresh)))
    print(f"  [anchor] PRE-FIX file-level={_prefix_snapshot(me_stale)!r} PRE-FIX fresh={_prefix_snapshot(me_fresh)!r} "
          f"| FIXED file-level={R.coupling_snapshot(me_stale)!r} FIXED fresh={R.coupling_snapshot(me_fresh)!r}")

    # ── ACK-1: REPRODUCE THE BUG. PRE-FIX helpers → the freshness flip restales a PRESENT, MATCHING ack → the
    #    check STAYS action_required AND the label is STRIPPED (the pre-fix failure). ──────────────────────────────
    _fix_paths, _fix_refs = _PA._ack_coupling_paths, _PA._ack_partner_refs
    try:
        _PA._ack_coupling_paths = _prefix_ack_coupling_paths
        _PA._ack_partner_refs = _prefix_ack_partner_refs
        bug_concl, bug_stripped, _ = _run_labeled_scenario(BUG_ACCOUNT, BUG_REPO, 11)
    finally:
        _PA._ack_coupling_paths, _PA._ack_partner_refs = _fix_paths, _fix_refs
    checks.append(("ACK-1 REPRODUCED: with the PRE-FIX snapshot helpers + a graph-freshness flip, a PRESENT, "
                   "MATCHING ack reads STALE → the check STAYS action_required (the ack is IGNORED — the pause "
                   "does NOT clear, matching the pre-fix behavior)",
                   bug_concl == "action_required"))
    checks.append(("ACK-1 REPRODUCED: ...AND the App STRIPS the veripsa-ack label (the pre-fix strip)",
                   bug_stripped))
    print(f"  [bug repro] labeled PR-11 conclusion={bug_concl!r} stripped={bug_stripped} (expect action_required + stripped)")

    # ── ACK-2: THE FIX. The REAL (fixed) helpers → the SAME present+matching ack over the SAME freshness flip
    #    clears the pause to `neutral` (acknowledged) and the label is NOT stripped. ──────────────────────────────
    fix_concl, fix_stripped, fix_gh = _run_labeled_scenario(FIX_ACCOUNT, FIX_REPO, 21)
    checks.append(("ACK-2 FIXED: with the FIXED helpers, the SAME present+matching ack over the SAME freshness "
                   "flip clears the pause to `neutral` (acknowledged — proceed)",
                   fix_concl == "neutral"))
    checks.append(("ACK-2 FIXED: a FRESH, matching ack is NOT stripped (the label stays on)",
                   not fix_stripped))
    fix_body = (fix_gh.list_issue_comments(FIX_REPO, 21) or [{}])[-1].get("body", "")
    checks.append(("ACK-2 FIXED: the cleared comment records the acknowledgement (records-not-correctness)",
                   "Acknowledged" in fix_body))
    print(f"  [fix] labeled PR-21 conclusion={fix_concl!r} stripped={fix_stripped} (expect neutral + NOT stripped)")

    # ── ACK-3: the stale-strip STILL fires on a GENUINE mismatch (the safety behavior is preserved). A label
    #    present whose stored snapshot was bound to a DIFFERENT coupling → action_required again + the label
    #    removed. We drive it by writing the comment for one coupling, then mutating the impact so the labeled
    #    event sees a genuinely different partner+file → a real (non-freshness) hash mismatch. ────────────────────
    gh3 = AckGitHub(MIS_ACCOUNT)
    proc3 = _proc(MIS_ACCOUNT, gh3)
    proc3("push", _push_main(MIS_ACCOUNT, MIS_REPO, SHA_MAIN))
    proc3("push", _push_branch(MIS_ACCOUNT, MIS_REPO, "feature-partner", "example-user"))   # advisory BR- partner (context)
    gh3.open_prs = [{"number": 30}, {"number": 31}]
    proc3("pull_request", _pr("opened", MIS_ACCOUNT, MIS_REPO, 30))   # PR- leader on the collide path (opens first)
    proc3("pull_request", _pr("opened", MIS_ACCOUNT, MIS_REPO, 31))   # acting PR → HARD serialize behind PR-30 (the PR-caused pause)
    stored_snap = _snap_in_comment(gh3, MIS_REPO, 31)
    # Overwrite the comment's snapshot marker with a hash bound to a DIFFERENT coupling (a real material change:
    # a different partner on a different file) — the genuine stale case.
    bogus = R.coupling_snapshot({"verdict": "serialize", "serialize_behind": ["someoneelse PR-999"],
                                 "paths": ["totally/other.py"],
                                 "collision_points": [{"path": "totally/other.py", "symbol": "other_fn"}]})
    for c in gh3.comments:
        if c["number"] == 31:
            c["body"] = c["body"].replace(f"veripsa-ack-snap:{stored_snap}", f"veripsa-ack-snap:{bogus}")
    gh3.labels[31] = [ACK_LABEL]
    proc3("pull_request", _pr("labeled", MIS_ACCOUNT, MIS_REPO, 31, [ACK_LABEL]))
    mis_concl, mis_stripped = _concl(gh3, 31), (31, ACK_LABEL) in gh3.removed
    checks.append(("ACK-3 STALE-STRIP PRESERVED: a label bound to a GENUINELY different coupling (different "
                   "partner + file = a real material change, NOT a freshness flip) is correctly detected stale → "
                   "action_required AGAIN + the label removed (the fix does not blanket-suppress stale detection)",
                   mis_concl == "action_required" and mis_stripped and bogus != stored_snap))
    print(f"  [stale guard] genuinely-mismatched PR-31 conclusion={mis_concl!r} stripped={mis_stripped} (expect action_required + stripped)")

    WH.apply_pause_ack = _orig_apply_pause_ack

    print("\n── PAUSE-ACK ACK RECOGNITION ───────────────────────────────")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("\nPAUSE ACK RECOGNITION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
    sys.exit(rc)
