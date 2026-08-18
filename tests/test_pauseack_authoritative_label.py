#!/usr/bin/env python3
"""PAUSE-ACK AUTHORITATIVE-LABEL GATE — a fully synthetic regression for current-label authority.

The decision path must not trust only the per-event PAYLOAD's `pull_request.labels[]`, which varies by event
type and can omit a current acknowledgement:

  • a REAL `synchronize` from an OLD push (the head moved BEFORE the label was added) → its payload labels carry
    NO ack label, even though the PR currently HAS it;
  • a `check_suite`/`check_run` rerequested REPLAY → labels depend on whatever _rerun_replay threaded through;
  • only the `labeled` event is guaranteed to carry it;
  • the neighbor path already reads gh.pr_labels (authoritative) — so the ACTING path was the asymmetric hole.

The ack is LOST the instant ANY of those reads label_present=False and re-raises action_required. Reading the
label from the per-event payload is the ROOT FRAGILITY — #306 patched ONE event (the rerun) but a real old-push
synchronize with empty payload labels still strips a valid ack.

THE FIX (webhook_handlers.py ACTING overlay, in lane): on EVERY material pause decision read `label_present` from
the AUTHORITATIVE current label set via a SINGLE gh.pr_labels(repo, pr, strict=True) call (GET pulls/{n} →
    labels[] = the PR's CURRENT labels, independent of which event fired; the PULLS surface works without an
    `issues` permission, while an issues-scoped label read may be unavailable and must fail open
into a dead ACK tier) — NOT from `pull_request.labels[]`. So a
rerun/sync/labeled/neighbor event ALL see the same true current-label state. Bounded: the overlay fires ONLY on a
material `neutral` verdict, so a clear/non-material PR pays NO pr_labels call (kept that way). FAIL-SAFE (keeps
#303): strict=True RAISES on an unreadable label set → the outer except FAIL-OPENs (the brain's plain advisory
neutral stands) — a transient API error never undoes an ack. The strip-only-on-proven-change decision (#303) is
intact: a GENUINELY different coupling still strips.

WHAT THIS GATE PROVES — on the REAL handle_event router (acting path: handle_pull_request -> apply_pause_ack; AND
the _post_refreshes NEIGHBOR path), over a SINGLE shared non-autocommit connection authed as the REAL
    least-privilege role veripsa_app. The DISTINGUISHING synthetic fixture: the PR HAS the `veripsa-ack` label
    (gh.pr_labels returns it) but the EVENT PAYLOAD's `pull_request.labels` is EMPTY/STALE:

  SETUP    two PRs collide on the SAME file → both MATERIAL (serialize); ack PR-A (label present + matching prior
           hash) → neutral, a snapshot marker stored, label NOT stripped.
  SYNC-1   REPRODUCE (pre-fix): a REAL synchronize of the ACKED PR whose payload labels are EMPTY (an old push,
           before the label) — under the PAYLOAD read it reads label_present=False → it STRIPS the ack and
           RE-PAUSES to action_required (the pre-fix failure: the ack did NOT survive a payload-empty event).
  SYNC-2   THE FIX: the SAME payload-empty synchronize under the AUTHORITATIVE read → label_present=True (from
           gh.pr_labels) → the acked PR STAYS neutral and the label is NOT stripped.
  RERUN    a check_suite rerequested re-eval (the #306 event class) ALSO reads authoritatively → ack holds.
  NEIGHBOR a neighbor PR's sync mutates ZERO sibling surfaces and records durable convergence; one explicitly
           isolated _post_refreshes posting slice reads the label authoritatively and the ack still holds.
  BOUNDED  a CLEAR (non-material) PR's whole lifecycle makes ZERO gh.pr_labels calls (the overlay never fires →
           no extra API cost on clean PRs) — the cost bound the fix promised.
  SAFETY   a synchronize of a PR whose coupling GENUINELY changed (a real different partner+file, label still on,
           authoritative read sees it) is STILL detected stale → action_required again + the label removed (the
           authoritative read does not over-fix into "ack never expires"; the #303 stale-strip still fires).
  ENDPOINT the REAL GitHubREST.pr_labels (the prsurface mixin, no fake override) hits ONE GET /repos/{repo}/
           pulls/{n} and NO issues-scoped endpoint; strict=True still raises on a non-permission error (the
           overlay's graceful skip), strict=False still returns [].
  PULLS-SURFACE the FULL flow (pause → ack → payload-empty sync) through the REAL mixin pr_labels under a
           least-privilege permission model: the acked PR reads Acknowledged off the pulls object's labels[] and
           zero issues-scoped endpoints are touched.

Run:  python3 tests/test_pauseack_authoritative_label.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tarfile
import urllib.error

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)
import server as S              # noqa: E402  (re-exports handle_event / _scoped_db)
import render as R              # noqa: E402  (prior_snapshot_from_comment / coupling_snapshot)
import webhook_handlers as WH   # noqa: E402  (handle_event router — the wiring under test)
from github_rest_prsurface import _GitHubPRSurfaceMixin  # noqa: E402  (the REAL pr_labels under endpoint test)

DB = "veripsa_acklbl_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"          # the REAL prod least-privilege role
INSTALL_ID = 7373
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")
SHA_MAIN = "2bed5678" * 5
COLLIDE_PATH = "backend/api.py"                              # the acked PR + its partner BOTH touch this → MATERIAL
OTHER_PATH = "backend/auth.py"                               # a DIFFERENT file (for the genuinely-changed-coupling case)
ACK_LABEL = "veripsa-ack"

# RLS-isolated accounts/repos so one sub-case never leaks claims into the next on the same bootstrapped DB.
REPRO_ACCOUNT, REPRO_REPO = 717171, "acklbl/repro-app"
FIX_ACCOUNT, FIX_REPO = 727272, "acklbl/fix-app"
BOUND_ACCOUNT, BOUND_REPO = 737373, "acklbl/bound-app"
MIS_ACCOUNT, MIS_REPO = 747474, "acklbl/mismatch-app"
PULLS_ACCOUNT, PULLS_REPO = 757575, "acklbl/pulls-app"


def _repo_id(repo: str) -> int:
    return 400_000 + sum((index + 1) * ord(char) for index, char in enumerate(repo))


class LiveGitHub:
    """Recording fake. Serves the sample_app fixture so a push builds a REAL main graph; tracks labels +
    removals + a COUNT of pr_labels calls. The DISTINGUISHING behavior: the PR's CURRENT labels (gh.pr_labels /
    get_pull_request) are the AUTHORITATIVE set, but the per-event payloads we build carry EMPTY labels — exactly
    the live old-sync / degraded-replay case. `force_payload_label_read` flips the acting overlay back to the
    pre-fix PAYLOAD read (for the negative control) by hiding pr_labels from hasattr."""

    def __init__(self, account_id):
        self.account_id = account_id
        self.checks, self.comments = [], []
        self._cid, self._chid = 1000, 2000
        self.labels = {}            # pr_number -> [label name]  (the AUTHORITATIVE current set)
        self.removed = []           # (pr_number, label) on remove_label
        self.pr_labels_calls = []   # pr_number per gh.pr_labels call (to prove the cost bound)
        self.check_writes = []      # PR numbers whose checks were posted/patched
        self.comment_writes = []    # PR numbers whose comments were posted/patched
        self.open_prs = []
        self._hide_pr_labels = False

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
        check = {"id": self._chid, "sha": sha, "conclusion": conclusion, "title": title,
                 "summary": summary, "name": "Veripsa"}
        self.checks.append(check)
        return check

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
        # the AUTHORITATIVE PR object — carries the CURRENT label set (the rerun replay reads this).
        repo_id = _repo_id(repo)
        return {"number": number, "base": {"ref": "main", "sha": SHA_MAIN,
                                           "repo": {"id": repo_id}},
                "head": {"sha": f"{number:040x}", "repo": {"id": repo_id},
                         "ref": f"feature/{number}"},
                "user": {"login": "dev"}, "draft": False, "state": "open", "merged": False,
                "changed_files": len(self.list_pr_files(repo, number)),
                "labels": [{"name": n} for n in self.labels.get(number, [])]}

    # the AUTHORITATIVE current-label read the fix uses on EVERY material decision — served off the PULLS
    # object (get_pull_request → labels[]), the same surface the real client reads (the App has no
    # `issues` permission, so labels come off GET pulls/{n}, never an issues-scoped endpoint).
    def pr_labels(self, repo, number, strict=False):
        self.pr_labels_calls.append(number)
        return [lab["name"] for lab in self.get_pull_request(repo, number)["labels"]]

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


class _PayloadReadGitHub(LiveGitHub):
    """The PRE-FIX control: HIDE pr_labels from hasattr so the acting overlay falls back to the PAYLOAD read
    (_ack_label_present). Everything else identical — so a payload-empty synchronize reproduces the live strip."""

    def __getattribute__(self, name):
        if name == "pr_labels":
            raise AttributeError("pr_labels hidden for the pre-fix payload-read control")
        return super().__getattribute__(name)


def _http_error(path: str, code: int, msg: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(f"https://api.github.com{path}", code, msg, {}, io.BytesIO(b""))


class _RestEndpointHost(_GitHubPRSurfaceMixin):
    """A BARE mixin host that scripts self._api — proves WHICH endpoint the REAL GitHubREST.pr_labels hits.
    Models a least-privilege App permission set: the registration carries NO `issues` permission, so EVERY
    /issues/-scoped API call 403s; GET /repos/{repo}/pulls/{n} (pull_requests scope) serves the PR object with
    labels[]. No DB, no network."""

    def __init__(self, labels=(), fail_pulls: Exception | None = None):
        self.api_paths = []
        self._labels = list(labels)
        self._fail_pulls = fail_pulls

    def _api(self, method, path, body=None):
        self.api_paths.append((method, path))
        if "/issues/" in path:
            raise _http_error(path, 403, "Forbidden")          # the live permission reality — no issues scope
        if self._fail_pulls is not None:
            raise self._fail_pulls
        return {"number": 7, "labels": [{"name": n} for n in self._labels]}


class _PullsSurfaceGitHub(LiveGitHub, _GitHubPRSurfaceMixin):
    """LiveGitHub whose label read is the REAL mixin pr_labels (no fake override), routed through an _api that
    models a least-privilege App permission set: ANY /issues/-scoped call 403s, GET pulls/{n} serves the
    authoritative PR object (labels[] included). If any overlay label read regresses to an issues-scoped
    endpoint, the flow 403s → fail-open → the PULLS-SURFACE assertions fail (paused reads back neutral-advisory,
    the ack never reads Acknowledged). Comments stay on LiveGitHub's direct fakes — the /issues/{n}/comments
    endpoints are dual-scoped under pull_requests and kept working in prod; that is not what #848 broke."""

    def __init__(self, account_id):
        super().__init__(account_id)
        self.api_paths = []

    def _api(self, method, path, body=None):
        self.api_paths.append((method, path))
        if "/issues/" in path:
            raise _http_error(path, 403, "Forbidden")          # the live permission reality — no issues scope
        m = re.match(r"^/repos/[^/]+/[^/]+/pulls/(\d+)$", path)
        if method == "GET" and m:
            return self.get_pull_request("", int(m.group(1)))
        raise AssertionError(f"unexpected _api call: {method} {path}")

    def pr_labels(self, repo, number, strict=False):           # the REAL endpoint choice, with call recording kept
        self.pr_labels_calls.append(number)
        return _GitHubPRSurfaceMixin.pr_labels(self, repo, number, strict=strict)


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


def _push_main(account, repo, sha, path=COLLIDE_PATH):
    return {"ref": "refs/heads/main", "after": sha,
            "installation": {"id": INSTALL_ID, "account": {"id": account}},
            "repository": {"id": _repo_id(repo), "full_name": repo,
                           "default_branch": "main", "owner": {"id": account}},
            "pusher": {"name": "dev"},
            "commits": [{"added": [path], "modified": [], "removed": []}]}


def _pr(action, account, repo, number, label_names=None):
    """A pull_request event. NOTE: label_names is what goes into the PAYLOAD's pull_request.labels[] — distinct
    from gh.labels[number] (the AUTHORITATIVE set). The whole point of this gate: a real old-sync carries EMPTY
    payload labels while the PR currently HAS the ack label."""
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


def _title(gh, number):
    c = gh.list_check_runs("", f"{number:040x}")
    return c[-1]["title"] if c else None


def _ack_two_prs(gh_cls, account, repo, acked_pr, partner_pr, collide_path=COLLIDE_PATH):
    """Seed main's graph; open the partner PR then the acked PR on the SAME path → both MATERIAL (serialize); ack
    the acked PR (label set on gh.labels = the AUTHORITATIVE set + a labeled event whose PAYLOAD carries it) →
    neutral. Returns (gh, proc) with acked_pr ACKED + neutral and a snapshot marker stored."""
    gh = gh_cls(account)
    gh.list_pr_files = lambda r, n: [collide_path]
    proc = _proc(account, gh)
    proc("push", _push_main(account, repo, SHA_MAIN, collide_path))
    gh.open_prs = [{"number": acked_pr}, {"number": partner_pr}]
    proc("pull_request", _pr("opened", account, repo, partner_pr))     # partner enters first
    proc("pull_request", _pr("opened", account, repo, acked_pr))       # acked_pr now paused (material), marker written
    assert _concl(gh, acked_pr) == "action_required", \
        f"setup: PR-{acked_pr} should be paused, got {_concl(gh, acked_pr)!r}"
    assert _snap_in_comment(gh, acked_pr), f"setup: PR-{acked_pr} must carry a snapshot marker"
    # ACK: the label goes on the AUTHORITATIVE set AND the labeled-event payload carries it (the one event that
    # always does) → neutral (acknowledged) on the ACTING path.
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

    # ── ENDPOINT: the REAL GitHubREST.pr_labels reads GET /repos/{repo}/pulls/{n} (labels[] on the
    #    PR object — the pulls surface the App's pull_requests scope covers) and NEVER an /issues/-scoped
    #    endpoint (the issue-GET is exactly what 403'd in prod and killed the ACK overlay everywhere). ──────────
    host = _RestEndpointHost(labels=[ACK_LABEL, "bug"])
    got = host.pr_labels("acklbl/endpoint-app", 7, strict=True)
    checks.append(("ENDPOINT: the REAL pr_labels serves the label names off ONE GET /repos/{repo}/pulls/{n} "
                   "(pull_requests scope) and touches NO issues-scoped endpoint — under the live no-`issues`-"
                   "permission App this read works instead of 403ing",
                   got == [ACK_LABEL, "bug"]
                   and host.api_paths == [("GET", "/repos/acklbl/endpoint-app/pulls/7")]))
    print(f"  [endpoint] pr_labels: got={got} api_paths={host.api_paths} "
          f"(expect the labels via exactly one pulls GET, zero issues calls)")

    # ── ENDPOINT ERRORS: the strict error contract is UNCHANGED by the endpoint move — strict=True RAISES on a
    #    non-permission pulls-read error (the overlay's outer except fail-opens: graceful skip, the advisory
    #    verdict stands), strict=False masks to [] (best-effort). ───────────────────────────────────────────────
    err_host = _RestEndpointHost(fail_pulls=_http_error("/repos/acklbl/endpoint-app/pulls/7", 502, "Bad Gateway"))
    strict_raised = False
    try:
        err_host.pr_labels("acklbl/endpoint-app", 7, strict=True)
    except urllib.error.HTTPError:
        strict_raised = True
    lax = err_host.pr_labels("acklbl/endpoint-app", 7)
    checks.append(("ENDPOINT ERRORS: a non-permission error on the pulls read keeps the strict contract — "
                   "strict=True RAISES (the overlay's outer except fail-opens → graceful skip) and strict=False "
                   "returns [] (best-effort)", strict_raised and lax == []))
    print(f"  [endpoint-errors] 502 on pulls GET: strict_raised={strict_raised} lax={lax} "
          f"(expect raised + [])")

    # ── SYNC-1 REPRODUCE (pre-fix PAYLOAD read): a REAL synchronize of the ACKED PR whose payload labels are EMPTY
    #    (an old push, before the label) → label_present=False off the payload → the ack is STRIPPED + re-paused. ───
    gh_r, proc_r = _ack_two_prs(_PayloadReadGitHub, REPRO_ACCOUNT, REPRO_REPO, 10, 11)
    # the PR STILL HAS the ack (authoritative set), but a real old-push sync carries EMPTY payload labels:
    proc_r("pull_request", _pr("synchronize", REPRO_ACCOUNT, REPRO_REPO, 10, label_names=[]))
    repro_concl = _concl(gh_r, 10)
    checks.append(("SYNC-1 REPRODUCED: a REAL synchronize of an ACKED PR whose EVENT PAYLOAD labels are EMPTY (an "
                   "old push before the label) reads label_present=False off the payload → the overlay re-reads the "
                   "PR as un-acked and RE-PAUSES to action_required (the pre-fix failure — per-event payload labels "
                   "vary by event and lose a still-present ack, turning the acked check red on every payload-empty event)",
                   repro_concl == "action_required"))
    print(f"  [repro] payload-empty synchronize PR-10 (pre-fix payload read): conclusion={repro_concl!r} "
          f"(expect action_required — the ack is lost: the payload says no-label so the acked PR re-pauses)")

    # ── SYNC-2 THE FIX (authoritative read): the SAME payload-empty synchronize → label_present=True from
    #    gh.pr_labels → the acked PR STAYS neutral and the label is NOT stripped. ─────────────────────────────────
    gh_f, proc_f = _ack_two_prs(LiveGitHub, FIX_ACCOUNT, FIX_REPO, 20, 21)
    proc_f("pull_request", _pr("synchronize", FIX_ACCOUNT, FIX_REPO, 20, label_names=[]))
    fix_concl = _concl(gh_f, 20)
    fix_stripped = (20, ACK_LABEL) in gh_f.removed
    checks.append(("SYNC-2 FIXED: the SAME payload-empty synchronize under the AUTHORITATIVE read (gh.pr_labels) "
                   "sees label_present=True and KEEPS the ack — the acked PR stays neutral, the label is NOT stripped",
                   fix_concl == "neutral" and not fix_stripped))
    print(f"  [fix] payload-empty synchronize PR-20 (authoritative read): conclusion={fix_concl!r} "
          f"stripped={fix_stripped} (expect neutral + NOT stripped)")

    # ── RERUN: a check_suite rerequested re-eval (the #306 event class) ALSO reads authoritatively → ack holds. ───
    proc_f("check_suite", _check_suite_rerequested(FIX_ACCOUNT, FIX_REPO, 20))
    rr_concl = _concl(gh_f, 20)
    rr_stripped = (20, ACK_LABEL) in gh_f.removed
    checks.append(("RERUN FIXED: a GitHub-auto check_suite rerequested re-eval of the acked PR reads the label "
                   "authoritatively too → the ack STILL holds (neutral, label NOT stripped)",
                   rr_concl == "neutral" and not rr_stripped))
    print(f"  [rerun] check_suite re-eval PR-20: conclusion={rr_concl!r} stripped={rr_stripped} "
          f"(expect neutral + NOT stripped)")

    # ── NEIGHBOR: the live event mutates ZERO sibling surfaces; one isolated durable slice then re-renders
    #    the acked PR and must read its label authoritatively before preserving the ack.
    nb_check_before = sum(n == 20 for n in gh_f.check_writes)
    nb_comment_before = sum(n == 20 for n in gh_f.comment_writes)
    nb_label_reads_before = gh_f.pr_labels_calls.count(20)
    nb_event = proc_f("pull_request", _pr("synchronize", FIX_ACCOUNT, FIX_REPO, 21))
    nb_inline_untouched = (sum(n == 20 for n in gh_f.check_writes) == nb_check_before
                           and sum(n == 20 for n in gh_f.comment_writes) == nb_comment_before
                           and gh_f.pr_labels_calls.count(20) == nb_label_reads_before)
    nb_progress = _isolated_convergence(
        FIX_ACCOUNT, FIX_REPO, gh_f, nb_event.get("refreshed") or [])
    nb_concl = _concl(gh_f, 20)
    nb_stripped = (20, ACK_LABEL) in gh_f.removed
    nb_surface_updated = (sum(n == 20 for n in gh_f.check_writes) > nb_check_before
                          and sum(n == 20 for n in gh_f.comment_writes) > nb_comment_before)
    nb_label_read_offworker = gh_f.pr_labels_calls.count(20) > nb_label_reads_before
    checks.append(("NEIGHBOR FIXED: a neighbor PR's live sync mutates ZERO PR-20 surface/label read and reports "
                   "the sibling deferred; one isolated real _post_refreshes slice reads PR-20's label "
                   "authoritatively, updates its surface, and the ack STILL holds (neutral, label NOT stripped)",
                   nb_inline_untouched and nb_event.get("refreshed_inflight") == 0
                   and nb_event.get("refresh_deferred", 0) >= 1
                   and nb_progress.get("posted", 0) >= 1 and nb_progress.get("errors") == 0
                   and nb_label_read_offworker and nb_concl == "neutral"
                   and not nb_stripped and nb_surface_updated))
    print(f"  [neighbor] PR-21 sync deferred PR-20; isolated slice progress={nb_progress!r}: "
          f"label_read_offworker={nb_label_read_offworker} conclusion={nb_concl!r} stripped={nb_stripped} "
          f"(expect authoritative read + neutral + NOT stripped)")

    # ── BOUNDED: a CLEAR (non-material) PR's whole lifecycle makes ZERO gh.pr_labels calls — the overlay never
    #    fires on a clear verdict, so a clean PR pays no extra API cost (the cost bound the fix promised). ──────────
    gh_b = LiveGitHub(BOUND_ACCOUNT)
    clean_path = "README.md"
    gh_b.list_pr_files = lambda r, n: [clean_path]       # docs-only, graph-independent and always CLEAR
    proc_b = _proc(BOUND_ACCOUNT, gh_b)
    proc_b("push", _push_main(BOUND_ACCOUNT, BOUND_REPO, SHA_MAIN, clean_path))
    gh_b.open_prs = [{"number": 40}]
    proc_b("pull_request", _pr("opened", BOUND_ACCOUNT, BOUND_REPO, 40))
    proc_b("pull_request", _pr("synchronize", BOUND_ACCOUNT, BOUND_REPO, 40))
    clear_concl = _concl(gh_b, 40)
    bound_calls = list(gh_b.pr_labels_calls)
    checks.append(("BOUNDED: a CLEAR (non-material) PR's full open+sync lifecycle made ZERO gh.pr_labels calls — "
                   "the authoritative read fires ONLY when a pause decision is actually being made (a material "
                   "neutral verdict), so a clean PR pays NO extra API cost",
                   clear_concl in ("success", None) and bound_calls == []))
    print(f"  [bounded] CLEAR PR-40 open+sync: conclusion={clear_concl!r} pr_labels_calls={bound_calls} "
          f"(expect a non-material verdict + ZERO pr_labels calls)")

    # ── SAFETY: a synchronize of a PR whose coupling GENUINELY changed (a real different partner+file, label still
    #    on, the authoritative read SEES it) is STILL detected stale → action_required + label removed. The
    #    authoritative read does not over-fix into "ack never expires"; the #303 strip-only-on-proven-change fires. ─
    gh_m, proc_m = _ack_two_prs(LiveGitHub, MIS_ACCOUNT, MIS_REPO, 30, 31)
    stored = _snap_in_comment(gh_m, 30)
    assert stored, "setup: PR-30 must carry a snapshot marker"
    bogus = R.coupling_snapshot({"verdict": "serialize", "serialize_behind": ["someoneelse PR-999"],
                                 "paths": [OTHER_PATH],
                                 "collision_points": [{"path": OTHER_PATH, "symbol": "other_fn"}]})
    assert bogus != stored, "setup: the bogus coupling must differ from the stored one"
    for c in gh_m.comments:
        if c["number"] == 30:
            c["body"] = c["body"].replace(f"veripsa-ack-snap:{stored}", f"veripsa-ack-snap:{bogus}")
    # the label is STILL on (authoritative + payload), but the ack is now bound to a DIFFERENT coupling → stale.
    proc_m("pull_request", _pr("synchronize", MIS_ACCOUNT, MIS_REPO, 30, label_names=[ACK_LABEL]))
    mis_concl = _concl(gh_m, 30)
    mis_stripped = (30, ACK_LABEL) in gh_m.removed
    checks.append(("SAFETY STALE-STRIP PRESERVED: a synchronize of a PR whose ack is bound to a GENUINELY different "
                   "coupling (a real material change, label still on, the authoritative read SEES it) is STILL "
                   "detected stale → action_required AGAIN + the label removed (the authoritative read does not "
                   "over-fix into 'ack never expires'; the #303 strip-only-on-proven-change still fires)",
                   mis_concl == "action_required" and mis_stripped))
    print(f"  [safety] genuinely-mismatched PR-30 synchronize: conclusion={mis_concl!r} "
          f"stripped={mis_stripped} (expect action_required + stripped)")

    # ── PULLS-SURFACE: the FULL pause→ack→payload-empty-sync flow through the REAL mixin pr_labels
    #    under the LIVE permission model (every /issues/-scoped API call 403s). Under the OLD issue-GET read this
    #    exact client 403'd on every label read → the overlay fail-opened → no pause on open (the _ack_two_prs
    #    setup asserts would already fail) and never an Acknowledged title. With the pulls-surface read the acked
    #    PR reads Acknowledged off GET pulls/{n} labels[] and ZERO issues-scoped endpoints are ever touched. ─────
    gh_p, proc_p = _ack_two_prs(_PullsSurfaceGitHub, PULLS_ACCOUNT, PULLS_REPO, 50, 51)
    proc_p("pull_request", _pr("synchronize", PULLS_ACCOUNT, PULLS_REPO, 50, label_names=[]))
    p_concl = _concl(gh_p, 50)
    p_title = _title(gh_p, 50)
    issues_hits = [p for _, p in gh_p.api_paths if "/issues/" in p]
    pulls_reads = [p for m, p in gh_p.api_paths if m == "GET" and re.search(r"/pulls/\d+$", p)]
    checks.append(("PULLS-SURFACE: the REAL GitHubREST.pr_labels ran the whole pause→ack→payload-"
                   "empty-sync flow under the LIVE no-`issues`-permission model (issues-scoped calls 403) — the "
                   "acked PR reads Acknowledged (neutral) off GET pulls/{n} labels[] and ZERO issues-scoped "
                   "endpoints were touched (the outage's issue-GET can not silently come back)",
                   p_concl == "neutral" and p_title == "Veripsa — Acknowledged"
                   and issues_hits == [] and len(pulls_reads) >= 1))
    print(f"  [pulls-surface] PR-50 acked flow via real pr_labels: conclusion={p_concl!r} title={p_title!r} "
          f"issues_hits={issues_hits} pulls_reads={len(pulls_reads)} "
          f"(expect neutral + Acknowledged title, zero issues-scoped hits, ≥1 pulls read)")

    print("\n── PAUSE-ACK AUTHORITATIVE LABEL ────────────────────────────")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("\nPAUSE ACK AUTHORITATIVE-LABEL GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
    sys.exit(rc)
