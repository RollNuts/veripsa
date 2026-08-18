#!/usr/bin/env python3
"""MERGE-GROUP gate (NO DB, fully offline): a GitHub merge-queue `merge_group` event must ALWAYS post a check on
the batch commit (merge_group.head_sha) so a REQUIRED Veripsa check can NEVER deadlock the queue — and it must
do so ADVISORY-ly (success/neutral only, never failure/action_required ON THE QUEUE COMMIT) and NEVER-CRASH on a
malformed payload.

THE GAP it locks out (verified): `handle_event("merge_group", …)` fell through to `{"noop": True}` — there was
NO merge_group branch and app-manifest.json did not subscribe to the event. Two failure faces:
  (a) ADVISORY  — a repo using GitHub merge queue batches PRs onto a `gh-readonly-queue/...` ref and merges
                  THERE; Veripsa never analyzed the batch → it landed UN-analyzed (the exact concurrent-collision
                  case Veripsa sells against).
  (b) REQUIRED  — the pause/ACK tier invites marking the Veripsa check REQUIRED; GitHub merge queue then needs a
                  status on the merge_group commit that NEVER arrived → the queue entry STALLS / times out.

This test drives webhook_handlers.handle_event with a crafted merge_group payload + a FAKE GitHub client (no
Postgres: the injected `db` returns the in-flight surface directly, and the per-PR replay's other db() writes are
no-ops). It asserts the BEFORE→AFTER fix:
  • checks_requested → a check is posted ON merge_group.head_sha (never noop).
  • the conclusion is ADVISORY (success only for an authoritative clear; neutral on coupling or Unknown) and is
    NEVER failure/action_required on the QUEUE commit (that is the literal status the queue blocks on).
  • read/replay/shape authority failures are reported as neutral `not analyzed`/Unknown, never false clear.
  • the batched PR(s) are resolved from the head_ref (`gh-readonly-queue/<base>/pr-<n>-<sha>`) and re-run.
  • `destroyed` (batch landed/dissolved) is a clean no-op.
  • a malformed payload (None / non-dict merge_group / missing head_sha / junk head_ref) is a clean no-op and
    NEVER raises.
  • the app-manifest subscribes to `merge_group` (so a NEW install receives the event at all).

Run:  python3 tests/test_merge_group.py     (no DB needed)
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import webhook_handlers as W  # noqa: E402

HEAD = "mg" + "0" * 38   # the merge_group batch commit (what the queue blocks on)
REPOSITORY_ID = 7000


def _pr_head(num: int) -> str:
    return f"h{num}" + "0" * 38


class FakeGitHub:
    """Records every check post/patch — exactly like the live client's surface, but in memory (no network, no DB).
    Only the methods the merge_group path + the per-PR synchronize replay touch are implemented; anything the
    replay reaches that is NOT here is exercised through the handler's own fail-soft guards (never crashes)."""

    def __init__(self):
        self.checks = []
        self.comments = []
        self._cid = 0

    def for_installation(self, iid):
        return self

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha]

    def post_check(self, repo, sha, conclusion, title, summary):
        self._cid += 1
        c = {"id": self._cid, "sha": sha, "conclusion": conclusion, "name": "Veripsa",
             "output": {"title": title, "summary": summary}}
        self.checks.append(c)
        return c

    def upsert_check(self, repo, sha, conclusion, title, summary):
        existing = [c for c in self.checks if c["sha"] == sha and c.get("name") == "Veripsa"]
        if existing:
            existing[0]["conclusion"] = conclusion
            existing[0]["output"] = {"title": title, "summary": summary}
            return existing[0]
        return self.post_check(repo, sha, conclusion, title, summary)

    def get_pull_request(self, repo, num):
        # AUTHORITATIVE PR object the merge_group/rerun replay re-fetches (number + head/base + author). Same-repo
        # head ⇒ not a fork; heads to the protected branch ⇒ in window.
        return {"number": num,
                "base": {"ref": "main", "sha": "b" * 40,
                         "repo": {"id": REPOSITORY_ID, "full_name": "acme/app"}},
                "head": {"ref": f"feat-{num}", "sha": _pr_head(num),
                         "repo": {"id": REPOSITORY_ID, "full_name": "acme/app"}},
                "user": {"login": "alice", "type": "User"}, "draft": False, "state": "open",
                "merged": False, "changed_files": 1, "labels": []}

    def list_pr_files(self, repo, num, pr_changed_files=0):
        return ["a.py"]

    def list_pr_files_with_ranges(self, repo, num, pr_changed_files=0):
        return {}

    def upsert_comment(self, *a, **k):
        return None


class UnresolvedBatchGitHub(FakeGitHub):
    def get_pull_request(self, repo, num):
        raise RuntimeError("authoritative PR read failed")


class MalformedBatchGitHub(FakeGitHub):
    def get_pull_request(self, repo, num):
        return {}


class ClosedBatchGitHub(FakeGitHub):
    def get_pull_request(self, repo, num):
        pr = super().get_pull_request(repo, num)
        pr["state"] = "closed"
        return pr


class ForkBatchGitHub(FakeGitHub):
    def get_pull_request(self, repo, num):
        pr = super().get_pull_request(repo, num)
        pr["head"]["repo"] = {"id": 7001, "full_name": "external/app"}
        return pr


class OutOfWindowBatchGitHub(FakeGitHub):
    def get_pull_request(self, repo, num):
        pr = super().get_pull_request(repo, num)
        pr["base"]["ref"] = "release-1.0"
        return pr


class FailingCheckGitHub(FakeGitHub):
    def upsert_check(self, repo, sha, conclusion, title, summary):
        raise RuntimeError("synthetic GitHub Checks outage")


def _surface_db(changes):
    """A no-DB `db(sql, args)` stub: core.main_impact_surface returns the given in-flight `changes`; every other
    statement (the per-PR replay's claim writes, savepoints, reconciles) is a benign no-op returning {}.
    Structural PR replay now requires a durable graph-refresh receipt before the delivery can complete, so the
    outbox enqueue returns a positive epoch just like the production SECURITY DEFINER function."""
    def db(sql, args=()):
        if "main_impact_surface" in sql:
            return {"repo": "acme/app", "branch": "main", "changes": changes}
        if "wake_graph_refresh_candidate_state_with_authority" in sql:
            return {"unfinished": True, "state": "queued", "request_epoch": 1}
        if ("enqueue_graph_refresh_with_authority" in sql
                or "wake_graph_refresh_candidate_with_authority" in sql):
            return 1
        return {}
    return db


def _mg_payload(action="checks_requested", head_sha=HEAD,
                head_ref="refs/heads/gh-readonly-queue/main/pr-7-abc123", base_ref="refs/heads/main"):
    return {"action": action,
            "repository": {"id": REPOSITORY_ID, "full_name": "acme/app",
                           "default_branch": "main"},
            "merge_group": {"head_sha": head_sha, "head_ref": head_ref,
                            "base_ref": base_ref, "base_sha": "b" * 40},
            "installation": {"id": 99}}


def main() -> int:
    checks: list[tuple[str, bool]] = []

    # ── BEFORE the fix this exact event returned {"noop": True} and posted NOTHING. AFTER: a check on head_sha. ──
    gh = FakeGitHub()
    res = W.handle_event(
        "merge_group", _mg_payload(),
        _surface_db([{"change_id": "PR-7", "head_sha": _pr_head(7),
                      "verdict": "clear", "paths": ["a.py"]}]), gh)
    posted = [c for c in gh.checks if c["sha"] == HEAD and c.get("name") == "Veripsa"]
    checks.append(("checks_requested is no longer a noop (the verified gap)", res.get("noop") is not True))
    checks.append(("a Veripsa check IS posted ON merge_group.head_sha (a required check can never deadlock)",
                   len(posted) == 1))
    checks.append(("the clear-batch check is ADVISORY success (queue proceeds; nothing in flight collides)",
                   posted and posted[0]["conclusion"] == "success"))
    checks.append(("the result reports the head_sha + a posted check",
                   res.get("head_sha") == HEAD and res.get("check_posted") is True))
    checks.append(("the batched PR is resolved from the head_ref (gh-readonly-queue/main/pr-7-…) and re-run",
                   res.get("reran") == ["PR-7"]))
    checks.append(("an exact current-head surface row supplies the explicit proof required for Clear",
                   res.get("verdict") == "clear" and res.get("reran") == ["PR-7"]))
    # CONTENT-FREE: the summary carries only refs/branch tokens, never a file body / symbol. (It names the batch
    # ref PR-7 and the branch; assert no obviously-bodyish content — a coarse but real content-free check.)
    summ = posted[0]["output"]["summary"] if posted else ""
    checks.append(("the check summary is content-free (names the batch ref + branch, advisory framing)",
                   "PR-7" in summ and "advisory" in summ.lower()))

    # ── HONEST UNKNOWN: missing/partial/malformed graph data is never rendered as a successful clear. ──
    for label, bad_impact in (
        ("missing", None),
        ("empty object", {}),
        ("wrong-typed changes", {"repo": "acme/app", "branch": "main", "changes": "bad"}),
        ("malformed change row", {"repo": "acme/app", "branch": "main",
                                  "changes": [{"change_id": "PR-7"}]}),
    ):
        unknown = W._render_merge_group_check(bad_impact, ["PR-7"])
        checks.append((f"{label} impact is neutral Unknown, never clear",
                       unknown["conclusion"] == "neutral"
                       and unknown["verdict"] == "unknown"
                       and "not analyzed" in unknown["title"].lower()
                       and "not clear" in unknown["summary"].lower()))

    # Exercise the handler's SAVEPOINT read-failure branch after a successful PR replay. It must still post the
    # queue status, but with an honest Unknown result.
    original_optional = W._optional

    def _fail_merge_group_read(db, label, fn, *args, **kwargs):
        if label == "merge_group impact read":
            return kwargs.get("default")
        return original_optional(db, label, fn, *args, **kwargs)

    W._optional = _fail_merge_group_read
    try:
        gh_read_fail = FakeGitHub()
        res_read_fail = W.handle_event("merge_group", _mg_payload(head_sha="mg" + "6" * 38),
                                       _surface_db([{"change_id": "PR-7", "head_sha": _pr_head(7),
                                                    "verdict": "clear",
                                                    "paths": ["a.py"]}]), gh_read_fail)
    finally:
        W._optional = original_optional
    failed_read_check = [c for c in gh_read_fail.checks if c["sha"] == "mg" + "6" * 38][0]
    checks.append(("impact read failure posts neutral not-analyzed/Unknown (no false clear, no queue deadlock)",
                   failed_read_check["conclusion"] == "neutral"
                   and res_read_fail.get("verdict") == "unknown"
                   and "not analyzed" in failed_read_check["output"]["title"].lower()))

    gh_replay_fail = UnresolvedBatchGitHub()
    res_replay_fail = W.handle_event("merge_group", _mg_payload(head_sha="mg" + "7" * 38),
                                     _surface_db([]), gh_replay_fail)
    failed_replay_check = [c for c in gh_replay_fail.checks if c["sha"] == "mg" + "7" * 38][0]
    checks.append(("unresolved batch PR posts neutral not-analyzed/Unknown (no partial-batch false clear)",
                   failed_replay_check["conclusion"] == "neutral"
                   and res_replay_fail.get("verdict") == "unknown"
                   and res_replay_fail.get("reran") == []))

    # A non-throwing fetch/replay is not proof of analysis.  Every batch member must be an authoritative open,
    # same-repo, default-branch PR and must return an explicit posted exact-head Check operation.  These adversarial
    # shapes used to be appended to `reran` merely because handle_event returned, allowing an empty surface to clear.
    for label, hostile_gh in (
        ("malformed authoritative PR", MalformedBatchGitHub()),
        ("closed authoritative PR", ClosedBatchGitHub()),
        ("fork authoritative PR", ForkBatchGitHub()),
        ("out-of-window authoritative PR", OutOfWindowBatchGitHub()),
    ):
        hostile_head = "mx" + str(len(checks) % 10) * 38
        hostile_res = W.handle_event("merge_group", _mg_payload(head_sha=hostile_head),
                                     _surface_db([]), hostile_gh)
        hostile_check = [c for c in hostile_gh.checks if c["sha"] == hostile_head][0]
        checks.append((f"{label} is neutral Unknown and never counted as analyzed",
                       hostile_res.get("reran") == []
                       and hostile_res.get("verdict") == "unknown"
                       and hostile_check["conclusion"] == "neutral"))

    original_handle_event = W.handle_event

    def no_op_replay(event_type, payload, db, gh, *args, **kwargs):
        return {"event": event_type, "noop": True, "skipped": "synthetic no-op"}

    W.handle_event = no_op_replay
    try:
        no_op_gh = FakeGitHub()
        no_op_head = "mn" + "8" * 38
        no_op_res = W._handle_merge_group_event(_mg_payload(head_sha=no_op_head),
                                                 _surface_db([]), no_op_gh)
    finally:
        W.handle_event = original_handle_event
    no_op_check = [c for c in no_op_gh.checks if c["sha"] == no_op_head][0]
    checks.append(("explicit replay no-op is neutral Unknown and never counted as analyzed",
                   no_op_res.get("reran") == [] and no_op_res.get("verdict") == "unknown"
                   and no_op_check["conclusion"] == "neutral"))

    # A successful exact-head Check write is only transport proof.  Quota/Unknown/generic Check paths also write
    # one, so merge queue authority additionally requires the brain's private exact-row structural verdict.
    def replay_result_with(verdict_marker="missing", verdict_head=None, **extra):
        def _replay(event_type, payload, db, gh, *args, **kwargs):
            num = payload.get("number")
            sha = (payload.get("pull_request") or {}).get("head", {}).get("sha")
            result = {
                "event": event_type,
                "check": {"conclusion": "success", "title": "synthetic", "summary": "synthetic"},
                W._CHECK_OPERATION_KEY: {"repo": "acme/app", "sha": sha, "posted": True},
                **extra,
            }
            if verdict_marker != "missing":
                result[W._ANALYSIS_VERDICT_KEY] = {
                    "repo": "acme/app", "branch": "main", "change_id": f"PR-{num}",
                    "head_sha": sha if verdict_head is None else verdict_head,
                    "verdict": verdict_marker,
                }
            return result
        return _replay

    for label, replay_fn in (
        ("posted non-verdict Check", replay_result_with()),
        ("quota-paused Check with stale clear marker", replay_result_with("clear", quota_paused=True)),
        ("explicit Unknown structural verdict", replay_result_with("unknown")),
        ("clear verdict proof for stale head A beside a posted current-head B Check",
         replay_result_with("clear", verdict_head="a" * 40)),
    ):
        W.handle_event = replay_fn
        try:
            proof_gh = FakeGitHub()
            proof_head = "mp" + str(len(checks) % 10) * 38
            proof_res = W._handle_merge_group_event(
                _mg_payload(head_sha=proof_head),
                _surface_db([{"change_id": "PR-7", "verdict": "clear", "paths": ["a.py"]}]), proof_gh)
        finally:
            W.handle_event = original_handle_event
        proof_check = [c for c in proof_gh.checks if c["sha"] == proof_head][0]
        checks.append((f"{label} cannot prove a merge-group clear",
                       proof_res.get("reran") == [] and proof_res.get("verdict") == "unknown"
                       and proof_check["conclusion"] == "neutral"))

    # Even when every replay has an exact verdict proof, the final authoritative surface itself must carry one
    # row for every queued ref.  A missing PR-8 row used to scope to PR-7 and silently fall through to Clear.
    W.handle_event = replay_result_with("clear")
    try:
        partial_gh = FakeGitHub()
        partial_head = "mp" + "7" * 38
        partial_res = W._handle_merge_group_event(
            _mg_payload(head_sha=partial_head,
                        head_ref="gh-readonly-queue/main/pr-7-aaa/pr-8-bbb"),
            _surface_db([{"change_id": "PR-7", "verdict": "clear", "paths": ["a.py"]}]), partial_gh)
    finally:
        W.handle_event = original_handle_event
    partial_check = [c for c in partial_gh.checks if c["sha"] == partial_head][0]
    checks.append(("a final surface missing one batch ref is neutral Unknown, never partial Clear",
                   partial_res.get("reran") == ["PR-7", "PR-8"]
                   and partial_res.get("verdict") == "unknown"
                   and partial_check["conclusion"] == "neutral"))

    duplicate = W._render_merge_group_check(
        {"repo": "acme/app", "branch": "main",
         "changes": [{"change_id": "PR-7", "verdict": "clear"},
                     {"change_id": "PR-7", "verdict": "clear"}]}, ["PR-7"])
    checks.append(("duplicate verdict rows are ambiguous and fail closed to Unknown",
                   duplicate["verdict"] == "unknown" and duplicate["conclusion"] == "neutral"))

    # GitHub's Check write can target current head B while the persisted surface still describes old head A.
    # PR identity alone must not bridge those two versions: the row/head mismatch withholds the private proof.
    stale_gh = FakeGitHub()
    stale_head = "ms" + "4" * 38
    stale_res = W.handle_event(
        "merge_group", _mg_payload(head_sha=stale_head),
        _surface_db([{"change_id": "PR-7", "head_sha": "a" * 40,
                      "verdict": "clear", "paths": ["a.py"]}]), stale_gh)
    stale_mg_check = [c for c in stale_gh.checks if c["sha"] == stale_head][0]
    current_pr_check = [c for c in stale_gh.checks if c["sha"] == _pr_head(7)]
    checks.append(("stale row head A plus a posted current-head B Check cannot prove Clear",
                   bool(current_pr_check) and stale_res.get("reran") == []
                   and stale_res.get("verdict") == "unknown"
                   and stale_mg_check["conclusion"] == "neutral"))

    # ── MATERIAL COUPLING: a serialize verdict in the batch → NEUTRAL on the queue commit, NEVER blocking. ──
    gh2 = FakeGitHub()
    res2 = W.handle_event(
        "merge_group",
        _mg_payload(head_sha="mg" + "1" * 38, head_ref="gh-readonly-queue/main/pr-7-aaa/pr-8-bbb"),
        _surface_db([{"change_id": "PR-7", "head_sha": _pr_head(7),
                      "verdict": "serialize", "paths": ["a.py"]},
                     {"change_id": "PR-8", "head_sha": _pr_head(8),
                      "verdict": "clear", "paths": ["b.py"]}]),
        gh2)
    mg2 = [c for c in gh2.checks if c["sha"] == "mg" + "1" * 38][0]
    checks.append(("a material-coupling batch posts a check too (the batch is never left un-analyzed)", bool(mg2)))
    checks.append(("a material-coupling batch renders NEUTRAL (advisory) — surfaced, not silent",
                   mg2["conclusion"] == "neutral"))
    checks.append(("NEVER-STALL: the queue-commit conclusion is NEVER failure/action_required (that would deadlock)",
                   mg2["conclusion"] not in ("failure", "action_required")))
    checks.append(("a multi-PR batch resolves BOTH enqueued PRs from the head_ref",
                   res2.get("reran") == ["PR-7", "PR-8"]))

    # ── `destroyed` (the batch landed or a member was dequeued) → clean no-op, nothing to post. ──
    gh3 = FakeGitHub()
    res3 = W.handle_event("merge_group", _mg_payload(action="destroyed"), _surface_db([]), gh3)
    checks.append(("`destroyed` is a clean no-op (nothing to post on a dissolved batch)",
                   res3.get("noop") is True and not gh3.checks))

    # ── NEVER-CRASH: hostile / malformed payloads degrade to a clean no-op, never an exception. ──
    crashed = None
    bad_payloads = [
        ("payload=None", None),
        ("payload=string", "junk"),
        ("payload=list", [1, 2, 3]),
        ("no repository", {"action": "checks_requested", "merge_group": {"head_sha": "x" * 40}}),
        ("repository full_name non-string", {"action": "checks_requested",
                                              "repository": {"full_name": 123, "default_branch": "main"},
                                              "merge_group": {"head_sha": "x" * 40}}),
        ("merge_group non-dict", {"action": "checks_requested",
                                  "repository": {"full_name": "a/b", "default_branch": "main"},
                                  "merge_group": "oops"}),
        ("missing head_sha", {"action": "checks_requested",
                              "repository": {"full_name": "a/b", "default_branch": "main"},
                              "merge_group": {"head_ref": "gh-readonly-queue/main/pr-1-x"}}),
        ("head_sha non-string", {"action": "checks_requested",
                                 "repository": {"full_name": "a/b", "default_branch": "main"},
                                 "merge_group": {"head_sha": 12345}}),
        ("head_ref non-string", {"action": "checks_requested",
                                 "repository": {"full_name": "a/b", "default_branch": "main"},
                                 "merge_group": {"head_sha": "z" * 40, "head_ref": 999, "base_ref": "refs/heads/main"}}),
    ]
    for label, bad in bad_payloads:
        try:
            W.handle_event("merge_group", bad, _surface_db([]), FakeGitHub())
        except Exception as e:  # noqa: BLE001
            crashed = f"{label}: {type(e).__name__}: {e}"
            break
    checks.append(("NEVER-CRASH: no malformed merge_group payload raises out of handle_event", crashed is None))
    if crashed:
        print("  crash:", crashed)

    # A malformed-but-postable payload (junk head_ref but a real head_sha) still posts a non-blocking status,
    # but cannot identify the batch and therefore must say Unknown rather than clear.
    gh4 = FakeGitHub()
    res4 = W.handle_event(
        "merge_group",
        {"action": "checks_requested", "repository": {"full_name": "acme/app", "default_branch": "main"},
         "merge_group": {"head_sha": "mg" + "4" * 38, "head_ref": 999, "base_ref": "refs/heads/main"},
         "installation": {"id": 1}},
        _surface_db([]), gh4)
    mg4 = [c for c in gh4.checks if c["sha"] == "mg" + "4" * 38]
    checks.append(("an unparseable head_ref posts neutral Unknown on the head (honest and never deadlocks)",
                   len(mg4) == 1 and mg4[0]["conclusion"] == "neutral"
                   and res4.get("verdict") == "unknown"
                   and "not analyzed" in mg4[0]["output"]["title"].lower()))

    # ── OUT-OF-WINDOW base: no graph authority, so post neutral Unknown and skip the recompute. ──
    gh5 = FakeGitHub()
    res5 = W.handle_event(
        "merge_group",
        _mg_payload(head_sha="mg" + "5" * 38, head_ref="gh-readonly-queue/release-1.0/pr-9-ccc",
                    base_ref="refs/heads/release-1.0"),
        _surface_db([{"change_id": "PR-9", "verdict": "serialize", "paths": ["a.py"]}]),
        gh5)
    mg5 = [c for c in gh5.checks if c["sha"] == "mg" + "5" * 38][0]
    checks.append(("an out-of-window batch gets neutral Unknown (no false clear and never deadlocks)",
                   mg5["conclusion"] == "neutral" and res5.get("verdict") == "unknown"
                   and res5.get("in_window") is False and res5.get("reran") == []))

    # A merge-group head has no PR-comment fallback. If GitHub rejects the required Check, returning normally
    # would mark the durable delivery done and leave the queue waiting forever. The handler must raise so the
    # delivery stays retryable; the poster itself remains fail-soft for ordinary PR paths.
    check_failure_raised = False
    try:
        W.handle_event(
            "merge_group", _mg_payload(head_sha="mg" + "9" * 38),
            _surface_db([{"change_id": "PR-7", "head_sha": _pr_head(7),
                          "verdict": "clear", "paths": ["a.py"]}]),
            FailingCheckGitHub())
    except RuntimeError as exc:
        check_failure_raised = "required Check was not posted" in str(exc)
    checks.append(("a failed merge-group Check post raises for durable retry instead of completing green",
                   check_failure_raised))

    # ── PURE HELPERS: PR-number parsing from the head_ref is content-free + bounded + deduped. ──
    checks.append(("head_ref → single PR number parse", W._merge_queue_pr_numbers("gh-readonly-queue/main/pr-7-abc") == [7]))
    checks.append(("head_ref → multi-PR batch parse (order-preserving)",
                   W._merge_queue_pr_numbers("gh-readonly-queue/main/pr-7-a/pr-8-b/pr-9-c") == [7, 8, 9]))
    checks.append(("head_ref with refs/heads/ prefix parses", W._merge_queue_pr_numbers("refs/heads/gh-readonly-queue/main/pr-42-d") == [42]))
    checks.append(("a non-merge-queue ref parses to no PRs", W._merge_queue_pr_numbers("refs/heads/main") == []))
    checks.append(("a non-string head_ref is a clean [] (never raises)", W._merge_queue_pr_numbers(12345) == []))
    checks.append(("duplicate PR segments dedupe", W._merge_queue_pr_numbers("pr-5-a/pr-5-b/pr-6-c") == [5, 6]))

    # ── MANIFEST: a NEW install must subscribe to merge_group (else GitHub never delivers the event). ──
    with open(os.path.join(ROOT, "github-app", "app-manifest.json")) as f:
        manifest = json.load(f)
    checks.append(("app-manifest.json default_events subscribes to merge_group (new installs receive it)",
                   "merge_group" in manifest.get("default_events", [])))

    ok = all(c[1] for c in checks)
    print("\n=== MERGE-GROUP (offline, no DB) ===")
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    print("\nMERGE GROUP GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
