#!/usr/bin/env python3
"""PAUSE-ACK ACK-STICKS GATE — a fully synthetic regression for the STALE-vs-ACK decision and cross-path
neighbor re-render. A valid, stable, matching `veripsa-ack` must remain neutral and must not be stripped unless a
coupling change is positively proven.

ROOT CAUSE (verified by the pure-function reproductions below): `apply_pause_ack` declared an ack STALE — and
STRIPPED the label + re-raised `action_required` — on ANY non-match, INCLUDING the two NON-PROOF cases a valid,
stable ack hits on a re-render:

  (1) prior_hash is None — the prior-comment hash could NOT be read THIS event. The NEIGHBOR-refresh path reads
      the label (gh.pr_labels) and the prior hash (list_issue_comments) via SEPARATE API calls AFTER another PR's
      event re-renders the acked PR; a transient list error or GitHub eventual-consistency makes the prior-hash
      read return None. "I couldn't read what it was acked for" is NOT "the coupling changed".
  (2) snapshot == '' — the recompute produced an EMPTY/unconfirmable coupling identity (the brain momentarily
      returned a material verdict with no partner/path under a stale graph, or a once-lazily-read impact row is
      degraded). An empty identity is "I can't confirm the coupling right now", NOT "it is a DIFFERENT coupling".

Both stripped a STILL-VALID ack on a no-op event. Worst on the NEIGHBOR path: opening/syncing/pushing ANOTHER
in-flight PR fans a refresh across the in-flight set, re-rendering the ACKED PR through apply_pause_ack — and a
possibly-stale/empty read on that path UNDID the acting path's acknowledgement (the ack did not STICK).

THE FIX (render.py — the stale DECISION; + github_rest.pr_labels strict + webhook_handlers neighbor seam):
  • apply_pause_ack now strips + re-raises ONLY on a POSITIVELY-PROVEN coupling change: label present AND BOTH the
    prior hash and the current snapshot are real, non-empty content-free identities AND they DIFFER. A
    non-confirmable read (prior_hash None, or an empty recompute) KEEPS the ack (neutral, label NOT stripped) and
    PRESERVES the prior hash in the embedded marker (so the binding is not erased to '' by one bad re-render).
  • The neighbor overlay reads the label STRICTLY (gh.pr_labels(strict=True)) so an UNREADABLE label set RAISES →
    the neighbor FAIL-OPENs to its plain advisory neutral instead of masking the error as "no label" and
    re-pausing an acked neighbor — the SAME ack-stickiness contract as the acting path (which reads the payload).

WHAT THIS GATE PROVES — on the REAL handle_event -> handle_pull_request -> apply_pause_ack ACTING and NEIGHBOR
paths, over a SINGLE shared non-autocommit connection authed as the REAL least-privilege role veripsa_app:

  PURE-1/2  the two non-proof cases (prior_hash None; empty recompute) under the PRE-FIX decision STRIP a valid
            ack; under the fix they KEEP it — and a GENUINE change still strips (the decision, isolated).
  STICK-1   REPRODUCE — ack a material PR (label present + matching prior hash) -> neutral; then a NEIGHBOR PR
            event records a durable wake and mutates ZERO sibling surfaces. One explicitly isolated convergence
            posting slice re-renders the acked PR WITH a non-confirmable read on the neighbor path: under the
            PRE-FIX decision the ack reads stale -> action_required and the label is STRIPPED (the prod failure).
  STICK-2   FIX — the SAME isolated durable neighbor refresh under the fixed decision -> the ack STILL holds
            (neutral, label NOT stripped); a subsequent queued no-op + isolated slice keeps it neutral too.
  STICK-3   SAFETY — a GENUINELY different coupling (a different partner on a different file) with the label still
            on is STILL detected stale -> action_required again + the label removed (the fix does not over-correct
            into "ack never expires").

Run:  python3 tests/test_pauseack_ack_sticks.py   (needs local Postgres with the veripsa roles)
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
import render as R              # noqa: E402  (apply_pause_ack — the stale-vs-ack decision under test)
import webhook_handlers as WH   # noqa: E402  (the acting + neighbor overlays; they bound apply_pause_ack by name)

DB = "veripsa_acksticks_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"          # the REAL prod least-privilege role
INSTALL_ID = 9292
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")
SHA_MAIN = "feed1234" * 5
COLLIDE_PATH = "backend/api.py"                              # the acked PR + the neighbor PR BOTH touch this → MATERIAL
ACK_LABEL = "veripsa-ack"

# RLS-isolated accounts/repos so a repro run never leaks claims into the fix run on the same bootstrapped DB.
REPRO_E_ACCOUNT, REPRO_E_REPO = 717171, "stick/repro-empty-app"      # empty-recompute reproduction
FIX_E_ACCOUNT, FIX_E_REPO = 727272, "stick/fix-empty-app"            # empty-recompute fix
REPRO_R_ACCOUNT, REPRO_R_REPO = 747474, "stick/repro-readfail-app"   # prior-hash read-failure reproduction
FIX_R_ACCOUNT, FIX_R_REPO = 757575, "stick/fix-readfail-app"         # prior-hash read-failure fix
MIS_ACCOUNT, MIS_REPO = 737373, "stick/mismatch-app"


# ── the PRE-FIX stale DECISION (the bug), restored verbatim for the negative control. This is apply_pause_ack as
#    it stood AFTER #300 (snapshot stable) but BEFORE this fix: any non-match — INCLUDING prior_hash None or an
#    empty recompute — is treated as stale and STRIPS the label. ────────────────────────────────────────────────
def _prefix_apply_pause_ack(rendered, impact, change_ref, *, label_present, prior_hash,
                            branch="main", is_fork=False, prior_confirmed=True,
                            graph_degraded=False):
    # prior_confirmed is IGNORED here on purpose — the PRE-FIX decision had no such signal; any non-match strips.
    out = dict(rendered)
    impact = impact if isinstance(impact, dict) else {}
    changes = R._dicts(impact.get("changes"))
    me = next((c for c in changes if c.get("change_id") == change_ref), None)
    if me is None:
        me = next((c for c in changes if c.get("agent") == change_ref or c.get("label") == change_ref), None)
    if is_fork or not R.is_material_coupling(me):
        out["snapshot"] = ""
        out["ack_state"] = "not_material"
        out["label_action"] = None
        return out
    snapshot = R.coupling_snapshot(me)
    marker = R._ack_snap_marker(snapshot)
    # THE BUG: any non-match is stale (prior_hash None → stale; empty snapshot → stale).
    acknowledged = bool(label_present) and prior_hash is not None and prior_hash == snapshot
    stale = bool(label_present) and not acknowledged
    body = out.get("comment")
    base_lines = body.split("\n") if isinstance(body, str) else ["### Veripsa"]
    if acknowledged:
        out["conclusion"] = "neutral"
        out["title"] = "Veripsa — Acknowledged"
        out["ack_state"] = "acknowledged"
        out["label_action"] = None
        new_lines = [marker, *base_lines, "", "> Acknowledged."]
    else:
        out["conclusion"] = "action_required"
        out["title"] = "Veripsa — Paused (acknowledge to proceed)"
        if stale:
            out["label_action"] = "remove"
            out["ack_state"] = "stale_reack"
        else:
            out["label_action"] = None
            out["ack_state"] = "paused"
        new_lines = [marker, *base_lines]
    out["snapshot"] = snapshot
    out["comment"] = R._cap_comment_body(new_lines)
    return out


def _repo_id(repo: str) -> int:
    return 200_000 + sum((index + 1) * ord(char) for index, char in enumerate(repo))


class StickGitHub:
    """Recording fake. Serves the sample_app fixture so a push builds a REAL main graph; tracks labels +
    removals so we can read whether a present ack stuck or was stripped. Two injectable non-proof instabilities
    mirror the cross-path prod stimulus on the NEIGHBOR-refresh event:
      _empty_recompute (via the _wrap_apply impact mutation) : the brain momentarily returns the change MATERIAL
                        (serialize) but with an EMPTY partner/path set → coupling_snapshot recomputes to '' (an
                        unconfirmable identity) — the prod-aligned case (stable stored hash, single comment, yet
                        the re-render can't confirm the coupling); must NOT strip the ack.
      _raise_prior   : a set of pr_numbers whose list_issue_comments RAISES this event (a transient GitHub list
                        error) → the prior-hash read fails → prior_confirmed False; must NOT strip the ack."""

    def __init__(self, account_id):
        self.account_id = account_id
        self.checks, self.comments = [], []
        self._cid, self._chid = 1000, 2000
        self.labels = {}            # pr_number -> [label name]
        self.removed = []           # (pr_number, label) on remove_label
        self.open_prs = []
        self._raise_prior = set()   # pr_numbers whose comment-list READ FAILS this event (a transient error → prior_confirmed False)

    def for_installation(self, installation_id):
        return self

    def installation_account_id(self):
        return str(self.account_id)

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return [COLLIDE_PATH]

    def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
        # Neighbor refresh now requires the same one-pass, evidence-complete file surface as the acting path.
        # Keep this integration fake authoritative so STICK-1/2 continue to exercise the pause-ACK overlay rather
        # than (correctly) stopping at the metadata safety boundary.
        return {"changed": [COLLIDE_PATH], "changed_ranges": {COLLIDE_PATH: []},
                "added_paths": [], "conflict_markers": [], "raw_entry_count": 1}

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

    def _all_comments(self, number):
        return [c for c in self.comments if c["number"] == number]

    def list_issue_comments(self, repo, number):
        if number in self._raise_prior:                      # a transient GitHub list error on the prior-hash read
            raise RuntimeError("transient list_issue_comments error (502)")
        return self._all_comments(number)

    def patch_comment(self, repo, comment_id, body):
        for c in self.comments:
            if c["id"] == comment_id:
                c["body"] = body
                return c
        raise AssertionError(f"comment not found: {comment_id}")

    def upsert_comment(self, repo, number, marker, body):
        # upsert reads the REAL comment store (not the hidden view) so a write still lands; only the pause-ack
        # PRIOR-hash readback (list_issue_comments) is what the hide injects a miss into.
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
        repo_id = _repo_id(repo)
        return {"number": number, "base": {"ref": "main", "sha": SHA_MAIN,
                                           "repo": {"id": repo_id}},
                "head": {"sha": f"{number:040x}", "repo": {"id": repo_id},
                         "ref": f"feature/{number}"},
                "user": {"login": "dev"}, "draft": False, "state": "open", "merged": False,
                "changed_files": 1,
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
            tf.add(FIXTURE, arcname="stick-" + sha[:7])
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


def _surface_state(gh, number):
    """Immutable sibling-surface snapshot, so an in-place PATCH cannot hide webhook mutation."""
    return (
        tuple((c.get("id"), c.get("conclusion"), c.get("title"), c.get("summary"))
              for c in gh.list_check_runs("", f"{number:040x}")),
        tuple((c.get("id"), c.get("body")) for c in gh._all_comments(number)),
        tuple(gh.labels.get(number, [])),
        tuple(gh.removed),
    )


def _push_main(account, repo, sha):
    return {"ref": "refs/heads/main", "after": sha,
            "installation": {"id": INSTALL_ID, "account": {"id": account}},
            "repository": {"id": _repo_id(repo), "full_name": repo,
                           "default_branch": "main", "owner": {"id": account}},
            "pusher": {"name": "dev"},
            "commits": [{"added": [COLLIDE_PATH], "modified": [], "removed": []}]}


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
    if action in ("labeled", "unlabeled"):
        p["label"] = {"name": ACK_LABEL}
    return p


def _snap_in_comment(gh, number):
    body = (gh._all_comments(number) or [{}])[-1].get("body", "")
    return R.prior_snapshot_from_comment(body)


def _concl(gh, number):
    c = gh.list_check_runs("", f"{number:040x}")
    return c[-1]["conclusion"] if c else None


# ── the EMPTY-RECOMPUTE instability, injected through apply_pause_ack's `impact` on the NEIGHBOR-refresh event:
#    the change `_empty_for` is kept MATERIAL (verdict serialize) but its partner/path set is BLANKED, so
#    coupling_snapshot recomputes to '' (an unconfirmable identity) — the prod-aligned non-proof (a stored stable
#    hash, a single comment, yet this event can't confirm the coupling). A valid ack must NOT strip on this. ───────
_empty_for = {"pr": None}


def _wrap_apply(inner):
    def wrapped(rendered, impact, change_ref, **kw):
        target = _empty_for["pr"]
        if target is not None and change_ref == f"PR-{target}" and isinstance(impact, dict):
            for ch in (impact.get("changes") or []):
                if isinstance(ch, dict) and ch.get("change_id") == f"PR-{target}":
                    # keep it MATERIAL (serialize) but strip the partners + paths → snapshot recomputes to ''
                    for k in ("serialize_behind", "queued_behind", "contested_with", "paths", "collision_points"):
                        ch[k] = []
        return inner(rendered, impact, change_ref, **kw)
    return wrapped


def _setup_acked_pr(account, repo, pr_number):
    """Seed main's graph; open the PR alone first → it is the only in-flight change so it is CLEAR (no partner);
    open a SECOND PR on the SAME path → now both are MATERIAL (serialize). Ack the first PR (label + matching
    hash) → neutral. Return (gh, proc). Leaves PR `pr_number` ACKED + neutral with a stored snapshot marker."""
    gh = StickGitHub(account)
    proc = _proc(account, gh)
    proc("push", _push_main(account, repo, SHA_MAIN))
    # PR-A opens with a partner already in flight so it is MATERIAL from the start: open the partner PR FIRST.
    return gh, proc


def _run_neighbor_stick(account, repo, acked_pr, neighbor_pr, *, prefix, mode):
    """The cross-path scenario. (1) two PRs on the same path → both MATERIAL; (2) ack acked_pr → neutral; (3) the
    NEIGHBOR PR sync queues convergence while mutating ZERO sibling surfaces; (4) one isolated real
    _post_refreshes slice re-renders acked_pr WITH an EMPTY-RECOMPUTE of acked_pr's coupling or a transient
    prior-hash read failure → assert whether the ack STICKS. `prefix`=True installs the PRE-FIX stale decision."""
    gh, proc = _setup_acked_pr(account, repo, acked_pr)
    # both PRs in flight on the same path → MATERIAL serialize for each
    gh.open_prs = [{"number": acked_pr}, {"number": neighbor_pr}]
    proc("pull_request", _pr("opened", account, repo, neighbor_pr))   # partner enters first
    proc("pull_request", _pr("opened", account, repo, acked_pr))      # acked_pr now paused (material), marker written
    assert _concl(gh, acked_pr) == "action_required", \
        f"setup: PR-{acked_pr} should be paused, got {_concl(gh, acked_pr)!r}"
    assert _snap_in_comment(gh, acked_pr), f"setup: PR-{acked_pr} must carry a snapshot marker"

    # ACK the PR — label + the matching prior hash → neutral (acknowledged) on the ACTING path.
    gh.labels[acked_pr] = [ACK_LABEL]
    proc("pull_request", _pr("labeled", account, repo, acked_pr, [ACK_LABEL]))
    assert _concl(gh, acked_pr) == "neutral", \
        f"precondition: PR-{acked_pr} must be ACKED (neutral) before the neighbor refresh, got {_concl(gh, acked_pr)!r}"
    assert (acked_pr, ACK_LABEL) not in gh.removed, "precondition: the valid ack must not be stripped on the acting path"

    # NOW the cross-path stimulus: the NEIGHBOR PR sync records a durable convergence wake, but the live webhook
    # must mutate ZERO sibling surfaces. Arm exactly ONE non-proof instability for the subsequent isolated posting
    # slice (isolated so each maps to one root cause):
    #   mode='empty'    → the acked PR's coupling recomputes EMPTY (material verdict, blanked partner/path) =
    #                     coupling_snapshot '' (the prod-aligned case: stable stored hash, can't confirm now).
    #   mode='readfail' → the neighbor path's SEPARATE prior-hash read (list_issue_comments) RAISES =
    #                     prior_confirmed False (a transient GitHub error / eventual-consistency).
    inner = _prefix_apply_pause_ack if prefix else R.apply_pause_ack
    WH.apply_pause_ack = _wrap_apply(inner)
    if mode == "empty":
        _empty_for["pr"] = acked_pr
    elif mode == "readfail":
        gh._raise_prior = {acked_pr}
    try:
        before = _surface_state(gh, acked_pr)
        event_result = proc("pull_request", _pr("synchronize", account, repo, neighbor_pr))
        assert _surface_state(gh, acked_pr) == before, \
            "live webhook must not mutate the sibling PR surface"
        assert event_result.get("refreshed_inflight") == 0 \
            and event_result.get("refresh_deferred", 0) >= 1, \
            "neighbor event must report zero live posts and at least one deferred sibling"
        progress = _isolated_convergence(
            account, repo, gh, event_result.get("refreshed") or [])
        assert progress.get("posted", 0) >= 1 and progress.get("errors") == 0, \
            f"isolated convergence did not post a complete sibling slice: {progress!r}"
    finally:
        _empty_for["pr"] = None
        gh._raise_prior = set()
        WH.apply_pause_ack = R.apply_pause_ack
    stripped = (acked_pr, ACK_LABEL) in gh.removed
    return _concl(gh, acked_pr), stripped, gh


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    checks = []

    # ── PURE-1/2: the stale DECISION in isolation. A material coupling + a valid label; vary the read. ───────────
    me = {"change_id": "PR-11", "verdict": "serialize", "serialize_behind": [{"change_id": "PR-12"}],
          "collision_points": [{"path": COLLIDE_PATH, "symbol": "h"}]}
    snap = R.coupling_snapshot(me)
    rendered = {"conclusion": "neutral", "title": "Veripsa", "summary": "s", "comment": "### Veripsa\nbody\nx"}
    impact = {"changes": [me]}
    me_empty = {"change_id": "PR-11", "verdict": "serialize"}          # material verdict, but NO partner/path → snapshot ''
    me_diff = {"change_id": "PR-11", "verdict": "serialize", "serialize_behind": [{"change_id": "PR-99"}],
               "collision_points": [{"path": "totally/other.py"}]}     # a GENUINELY different coupling

    # PRE-FIX: any non-match strips (prior_hash None → stale; empty snapshot → stale), regardless of why.
    pre_none = _prefix_apply_pause_ack(rendered, impact, "PR-11", label_present=True, prior_hash=None)
    pre_empty = _prefix_apply_pause_ack(rendered, {"changes": [me_empty]}, "PR-11", label_present=True, prior_hash=snap)
    # FIX: an UNCONFIRMED None (the read FAILED, prior_confirmed=False) keeps the ack; a CONFIRMED None (genuinely
    # no prior comment, prior_confirmed=True) still PAUSES (the ack has nothing to bind to — case 2b of the unit gate).
    fix_none_unconf = R.apply_pause_ack(rendered, impact, "PR-11", label_present=True, prior_hash=None, prior_confirmed=False)
    fix_none_conf = R.apply_pause_ack(rendered, impact, "PR-11", label_present=True, prior_hash=None, prior_confirmed=True)
    fix_empty = R.apply_pause_ack(rendered, {"changes": [me_empty]}, "PR-11", label_present=True, prior_hash=snap)
    fix_diff = R.apply_pause_ack(rendered, {"changes": [me_diff]}, "PR-11", label_present=True, prior_hash=snap)
    fix_match = R.apply_pause_ack(rendered, impact, "PR-11", label_present=True, prior_hash=snap)

    checks.append(("PURE-1 REPRODUCE: PRE-FIX decision strips a valid ack when prior_hash is None — it cannot tell a "
                   "FAILED read from a genuinely-absent one, so a transient read miss is treated as 'changed'",
                   pre_none["ack_state"] == "stale_reack" and pre_none["label_action"] == "remove"))
    checks.append(("PURE-1 FIX: an UNCONFIRMED None (the read FAILED) KEEPS the ack (neutral, NOT stripped); a "
                   "CONFIRMED None (genuinely no prior comment yet) still PAUSES (binds on the marker written now) "
                   "— the unit gate's case 2b is preserved",
                   fix_none_unconf["ack_state"] == "acknowledged" and fix_none_unconf["label_action"] is None
                   and fix_none_conf["ack_state"] == "paused" and fix_none_conf["conclusion"] == "action_required"))
    checks.append(("PURE-2 REPRODUCE: PRE-FIX decision strips a valid ack when the recompute is EMPTY (snapshot '') "
                   "— an unconfirmable identity treated as 'a different coupling'",
                   pre_empty["ack_state"] == "stale_reack" and pre_empty["label_action"] == "remove"))
    checks.append(("PURE-2 FIX: an EMPTY recompute now KEEPS the ack AND preserves the prior hash in the embedded "
                   "marker (the binding is not erased to '' by one bad re-render)",
                   fix_empty["ack_state"] == "acknowledged" and fix_empty["conclusion"] == "neutral"
                   and fix_empty["label_action"] is None and fix_empty["snapshot"] == snap))
    checks.append(("PURE-3 SAFETY: a positively-proven different coupling (real partner+file change) STILL strips + "
                   "re-raises; and a real MATCH still clears (the fix neither over- nor under-corrects)",
                   fix_diff["ack_state"] == "stale_reack" and fix_diff["label_action"] == "remove"
                   and fix_match["ack_state"] == "acknowledged"))
    print(f"  [pure] PRE-FIX(None)={pre_none['ack_state']} FIX(None,unconf)={fix_none_unconf['ack_state']} "
          f"FIX(None,conf)={fix_none_conf['ack_state']} | PRE-FIX(empty)={pre_empty['ack_state']} "
          f"FIX(empty)={fix_empty['ack_state']} | FIX(diff)={fix_diff['ack_state']} FIX(match)={fix_match['ack_state']}")

    # ── STICK-1: REPRODUCE on the REAL cross-path neighbor refresh, for BOTH non-proof instabilities. The
    #    live event performs no sibling write; the isolated slice under the PRE-FIX decision strips + re-pauses.
    e_repro_concl, e_repro_stripped, _ = _run_neighbor_stick(REPRO_E_ACCOUNT, REPRO_E_REPO, 11, 12, prefix=True, mode="empty")
    r_repro_concl, r_repro_stripped, _ = _run_neighbor_stick(REPRO_R_ACCOUNT, REPRO_R_REPO, 41, 42, prefix=True, mode="readfail")
    checks.append(("STICK-1a REPRODUCED (empty-recompute): a NEIGHBOR PR's sync performs ZERO sibling mutation; "
                   "the isolated durable slice re-renders the acked PR whose coupling recomputes EMPTY (snapshot "
                   "'') — under the PRE-FIX decision it goes back to action_required and the label is STRIPPED",
                   e_repro_concl == "action_required" and e_repro_stripped))
    checks.append(("STICK-1b REPRODUCED (prior-hash read failure): a NEIGHBOR PR's sync performs ZERO sibling "
                   "mutation; the isolated durable slice's prior-hash read RAISES (prior_hash None) — under the "
                   "PRE-FIX decision action_required re-raises and the label is STRIPPED",
                   r_repro_concl == "action_required" and r_repro_stripped))
    print(f"  [repro] empty-recompute PR-11: {e_repro_concl!r} stripped={e_repro_stripped} | "
          f"read-failure PR-41: {r_repro_concl!r} stripped={r_repro_stripped} (expect action_required + stripped)")

    # ── STICK-2: THE FIX. The SAME cross-path neighbor refreshes under the FIXED decision → the ack STICKS. ───────
    e_fix_concl, e_fix_stripped, e_fix_gh = _run_neighbor_stick(FIX_E_ACCOUNT, FIX_E_REPO, 21, 22, prefix=False, mode="empty")
    r_fix_concl, r_fix_stripped, _ = _run_neighbor_stick(FIX_R_ACCOUNT, FIX_R_REPO, 51, 52, prefix=False, mode="readfail")
    checks.append(("STICK-2a FIXED (empty-recompute): the SAME cross-path neighbor refresh now KEEPS the ack — the "
                   "acked PR stays neutral and the label is NOT stripped",
                   e_fix_concl == "neutral" and not e_fix_stripped))
    checks.append(("STICK-2b FIXED (prior-hash read failure): a transient prior-hash read failure on the neighbor "
                   "path now FAILS SAFE — the ack stays neutral and the label is NOT stripped",
                   r_fix_concl == "neutral" and not r_fix_stripped))
    # A SUBSEQUENT clean no-op event still mutates no sibling inline; its isolated posting slice keeps the ack
    # neutral too.
    proc2 = _proc(FIX_E_ACCOUNT, e_fix_gh)
    clean_before = _surface_state(e_fix_gh, 21)
    clean_event = proc2("pull_request", _pr("synchronize", FIX_E_ACCOUNT, FIX_E_REPO, 22))
    clean_inline_untouched = _surface_state(e_fix_gh, 21) == clean_before
    clean_progress = _isolated_convergence(
        FIX_E_ACCOUNT, FIX_E_REPO, e_fix_gh, clean_event.get("refreshed") or [])
    checks.append(("STICK-2c FIXED: a subsequent clean no-op neighbor event mutates ZERO sibling surface inline; "
                   "one isolated durable slice keeps the ack neutral (the binding survived the empty re-render)",
                   clean_inline_untouched and clean_event.get("refreshed_inflight") == 0
                   and clean_event.get("refresh_deferred", 0) >= 1
                   and clean_progress.get("posted", 0) >= 1 and clean_progress.get("errors") == 0
                   and _concl(e_fix_gh, 21) == "neutral" and (21, ACK_LABEL) not in e_fix_gh.removed))
    print(f"  [fix] empty-recompute PR-21: {e_fix_concl!r} stripped={e_fix_stripped} | read-failure PR-51: "
          f"{r_fix_concl!r} stripped={r_fix_stripped} (expect neutral + NOT stripped); after no-op: {_concl(e_fix_gh, 21)!r}")

    # ── STICK-3: SAFETY — a GENUINELY changed coupling still goes stale + strips (regression preserved). Drive the
    #    ACTING path: ack a coupling, then overwrite the stored marker with a hash bound to a DIFFERENT coupling
    #    (a real material change) → the labeled re-evaluation correctly re-raises + strips. ──────────────────────────
    gh3 = StickGitHub(MIS_ACCOUNT)
    proc3 = _proc(MIS_ACCOUNT, gh3)
    proc3("push", _push_main(MIS_ACCOUNT, MIS_REPO, SHA_MAIN))
    gh3.open_prs = [{"number": 31}, {"number": 32}]
    proc3("pull_request", _pr("opened", MIS_ACCOUNT, MIS_REPO, 32))
    proc3("pull_request", _pr("opened", MIS_ACCOUNT, MIS_REPO, 31))
    stored = _snap_in_comment(gh3, 31)
    assert stored, "setup: PR-31 must carry a snapshot marker"
    bogus = R.coupling_snapshot({"verdict": "serialize", "serialize_behind": ["someoneelse PR-999"],
                                 "paths": ["totally/other.py"],
                                 "collision_points": [{"path": "totally/other.py", "symbol": "other_fn"}]})
    for c in gh3.comments:
        if c["number"] == 31:
            c["body"] = c["body"].replace(f"veripsa-ack-snap:{stored}", f"veripsa-ack-snap:{bogus}")
    gh3.labels[31] = [ACK_LABEL]
    proc3("pull_request", _pr("labeled", MIS_ACCOUNT, MIS_REPO, 31, [ACK_LABEL]))
    mis_concl, mis_stripped = _concl(gh3, 31), (31, ACK_LABEL) in gh3.removed
    checks.append(("STICK-3 STALE-STRIP PRESERVED: a label bound to a GENUINELY different coupling (different "
                   "partner + file = a real material change, not a non-confirmable read) is STILL detected stale → "
                   "action_required AGAIN + the label removed (the fix does not over-fix into 'ack never expires')",
                   mis_concl == "action_required" and mis_stripped and bogus != stored))
    print(f"  [stale guard] genuinely-mismatched PR-31: conclusion={mis_concl!r} stripped={mis_stripped} "
          f"(expect action_required + stripped)")

    print("\n── PAUSE-ACK ACK STICKS ─────────────────────────────────────")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("\nPAUSE ACK STICKS GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
    sys.exit(rc)
