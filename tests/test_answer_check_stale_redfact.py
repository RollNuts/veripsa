#!/usr/bin/env python3
"""ANSWER-CHECK STALE-RED-FACT strict regression gate on the close-time 答え合わせ.

══════════════════════════════════════════════════════════════════════════════════════════════════════════════
THE HISTORICAL DEFECT (fixed and regression-tested below): the answer-check graded a CLEAN merge as `conflicted`
(with the STRONGEST confidence label `observed`) when the PR was transiently RED earlier in its life and the
author FIXED it before merging. That is a FALSE record in the effect ledger — the very numbers Veripsa sells on.

THE GRADING CONTRACT (records-not-correctness):  Veripsa's effect numbers (the sales / credibility proof) come
from the `advice_outcome` ledger — each merged PR is graded `land=clean|conflicted|reverted`, fed into the
confusion matrix (ignored→conflicted = TRUE POSITIVE; cleared→conflicted = SILENT MISS). A CLEAN merge mis-graded
`conflicted` INFLATES Veripsa's true-positive / silent-miss counts: the product reports outcomes that did not
happen — the opposite of records-not-correctness (we must never RECORD an outcome the facts do not support).

THE FIXED PATH: webhook_handlers.handle_event passes the final PR head SHA to the sha-aware
core.change_failing overload. Historical red facts remain in the append-only ledger, but only a failing fact at
the head that actually merged can grade that merge as conflicted. The three-argument overload remains available
for callers without head evidence; the close path tested here must use the final-head form.

WHAT THIS GATE ASSERTS (now enforced):  it drives the REAL App brain (server.handle_event over a Fake GitHub +
the REAL gate) through the exact (a)→(b)→(c) timeline and proves the clean merge is graded land=clean, plus a
second PR merged WHILE red is still graded land=conflicted — so nobody can quietly regress the close-path grading
back into the phantom-conflict silent-miss. records-not-correctness: this gate records the FIXED behavior as FACT.

Run:  python3 tests/test_answer_check_stale_redfact.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import psycopg2  # noqa: E402
import server as S  # noqa: E402
from cg_schema_contract import (  # noqa: E402
    EXTRACTOR_VERSION,
    SCHEMA_CONTRACT_VERSION,
)
from _server_harness import FakeGitHub  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): per-PID, exactly like db/smoke.sh / test_server / run_gates. (Our OWN DB name,
# so we define make_db locally — the harness's make_db closes over its own veripsa_servertest_<pid> constant.)
DB = "veripsa_acstale_" + str(os.getpid())
REPO = "acme/answer"
BRANCH = "main"
REPOSITORY_ID = 7107
MAIN_SHA = "c" * 40

PASS, FAIL = [], []


def check(label, cond):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def make_db(role):
    """A db(sql,args) runner authed as `role` against OUR per-PID DB — the UNPINNED App-role model the server
    gate's basic scenarios use (bootstrap_local routes veripsa_app's writes to ACCT-DEMO; the tenant pin comes
    from the payload's installation id via for_installation on the gh client, not a SET on the DB connection)."""
    def run(sql, args=()):
        conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    return run


def readback(sql, args=()):
    """Raw ledger SELECTs go through the MIGRATOR with the tenant pinned (the App role writes via gates only and
    cannot raw-SELECT past RLS). The server harness's make_db('veripsa_app') is UNPINNED → bootstrap_local routes
    its writes to ACCT-DEMO, so we read back that tenant (the same model test_server.py's basic scenarios use)."""
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, false)", ("ACCT-DEMO",))
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def seed_live_installation_route() -> None:
    """Give the direct App-role fixture the live routing row a webhook owns.

    Production reaches the graph-wake primitive only after authenticated
    installation routing has persisted this row.  This test calls the handler
    directly, so reproduce that prerequisite instead of weakening the durable
    wake acknowledgement contract.
    """
    conn = psycopg2.connect(
        f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "INSERT INTO core.installation_account("
                "installation_id,account_id) VALUES (%s,%s) "
                "ON CONFLICT (installation_id) DO UPDATE "
                "SET account_id=EXCLUDED.account_id,revoked_at=NULL",
                ("answer-check-fixture", "ACCT-DEMO"),
            )
    finally:
        conn.close()


def _outcome_detail(pr):
    rows = readback(
        "SELECT detail FROM core.event WHERE kind='advice_outcome' AND path=%s AND repo=%s LIMIT 1",
        (f"PR-{pr}", REPO))
    return rows[0][0] if rows else None


def pr_payload(action, number, author, head_sha, *, merged=False):
    return {"action": action, "number": number,
            "installation": {"id": 4242},
            "repository": {"id": REPOSITORY_ID, "full_name": REPO,
                           "default_branch": BRANCH},
            "pull_request": {
                "base": {"ref": BRANCH, "sha": MAIN_SHA,
                         "repo": {"id": REPOSITORY_ID, "full_name": REPO}},
                "head": {"sha": head_sha,
                         "repo": {"id": REPOSITORY_ID, "full_name": REPO}},
                             "user": {"login": author}, "merged": merged,
                             "merge_commit_sha": ("e" * 40) if merged else None}}   # hex merge sha (land needs hex)


def check_suite_payload(conclusion, pr_number, head_sha):
    """A check_suite `completed` event carrying the PR — the path that records (only) FAILING facts."""
    return {"action": "completed", "installation": {"id": 4242},
            "repository": {"id": REPOSITORY_ID, "full_name": REPO,
                           "default_branch": BRANCH},
            "check_suite": {"head_sha": head_sha, "conclusion": conclusion,
                            "pull_requests": [{
                                "number": pr_number,
                                "base": {
                                    "ref": BRANCH,
                                    "repo": {
                                        "id": REPOSITORY_ID,
                                        "full_name": REPO,
                                    },
                                },
                            }]}}


def deliver_pull_request(gh, db, payload):
    """Keep the fake authoritative PR read on the exact snapshot carried by this delivery."""
    number = payload["number"]
    current = dict(payload["pull_request"])
    current.update({"number": number,
                    "state": "closed" if payload["action"] == "closed" else "open",
                    "changed_files": len(gh.files_by_pr.get(number, []))})
    gh.pr_objects[number] = current
    return S.handle_event("pull_request", payload, db, gh)


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    try:
        db = make_db("veripsa_app")
        seed_live_installation_route()
        # A tiny main graph: one LONE file node → a solo PR on it resolves to a genuine 'clear' verdict (so the
        # merge can be honestly clean, no in-flight coupling). Content-free (one file node, no edges). Ingest it AT
        # the sha FakeGitHub reports as main's HEAD ('c'*40, repo_default_branch_head) so the pre-analyze self-heal
        # sees the stored graph already current and does NOT re-ingest the sample_app fixture over our tiny graph.
        graph = {"extractor_version": EXTRACTOR_VERSION,
                 "metrics": {"schema_contract_version": SCHEMA_CONTRACT_VERSION},
                 "nodes": [{"id": "src/solo.py", "kind": "file", "path": "src/solo.py", "name": "solo.py"}],
                 "edges": []}
        db("SELECT core.ingest_graph_with_authority(%s::jsonb,%s,%s,%s)", (json.dumps(graph), REPO, BRANCH, MAIN_SHA))

        LONE = "src/solo.py"
        HEAD_A = "1" * 40           # the early head CI ran RED on
        HEAD_B = "2" * 40           # the fixed head the PR finally merged at
        gh = FakeGitHub({7: [LONE]})

        # 1. PR-7 OPENS clean → predicted 'clear' (a lone file, nothing in flight).
        deliver_pull_request(gh, db, pr_payload("opened", 7, "erin", HEAD_A))
        preds = readback("SELECT detail FROM core.event WHERE kind='prediction' AND path='PR-7' AND repo=%s LIMIT 1", (REPO,))
        pred = preds[0][0] if preds else None
        check("PR-7 opened clean → predicted 'clear' (no coupling Veripsa could see)",
              isinstance(pred, str) and "verdict=clear" in pred)

        # 2. CI goes RED at the EARLY head (a flaky/typo failure the author will fix) → records a 'pr_failing' fact.
        S.handle_event("check_suite", check_suite_payload("failure", 7, HEAD_A), db, gh)
        prf = readback("SELECT count(*) FROM core.event WHERE kind='pr_failing' AND path='PR-7' AND repo=%s", (REPO,))
        check("a transient RED check recorded a pr_failing fact for PR-7 (the early failure)",
              (prf[0][0] if prf else 0) >= 1)

        # 3. The author FIXES it: PR-7 syncs to a new head, CI goes GREEN. A `success` check_suite is a NO-OP —
        #    it does NOT retract the earlier red fact (the append-only ledger keeps it). This is the real timeline.
        deliver_pull_request(gh, db, pr_payload("synchronize", 7, "erin", HEAD_B))
        S.handle_event("check_suite", check_suite_payload("success", 7, HEAD_B), db, gh)

        # 4. PR-7 MERGES CLEANLY at the fixed head — NO conflict, NO revert (the close payload says so).
        deliver_pull_request(gh, db, pr_payload("closed", 7, "erin", HEAD_B, merged=True))

        # 5. THE ANSWER-CHECK — what the grading machinery RECORDED. The HONEST outcome is land=clean (the PR
        #    merged with no conflict, no revert; the early red was fixed). The fix LANDED (sha-aware
        #    core.change_failing + the close path passes head_sha), so this gate asserts the CORRECT grade. The
        #    transient early red at
        #    HEAD_A no longer mis-grades the clean merge at HEAD_B — the false SILENT-MISS is gone.
        o7 = _outcome_detail(7)
        print(f"      recorded advice_outcome for PR-7: {o7}")
        check("answer-check actually graded PR-7 (a row was recorded at merge)", isinstance(o7, str))
        # ENFORCED: the clean merge is graded land=clean — NOT conflicted — and is no longer a false silent-miss.
        fixed = isinstance(o7, str) and "pred=clear" in o7 and "land=clean" in o7 and "land=conflicted" not in o7
        check("ENFORCED (#334 fix): a transient-red-then-fixed-then-merged-clean PR is graded "
              "pred=clear;land=clean (NOT conflicted) — the false SILENT-MISS is removed (sha-aware change_failing)",
              fixed)

        # 6. RECALL-SAFE — a PR genuinely merged WHILE RED (its red fact is AT the head that merged) MUST still be
        #    graded land=conflicted. The sha-aware scope keys on the MERGED head, so a red fact there is still seen;
        #    the fix removes ONLY the transient-earlier-red phantom, never a real bad landing. PR-8: opens clean,
        #    CI goes RED at HEAD_C, and it MERGES at HEAD_C (the SAME red head — no fix). Expect conflicted+observed.
        HEAD_C = "3" * 40
        gh8 = FakeGitHub({8: [LONE]})
        deliver_pull_request(gh8, db, pr_payload("opened", 8, "finn", HEAD_C))
        S.handle_event("check_suite", check_suite_payload("failure", 8, HEAD_C), db, gh8)
        deliver_pull_request(gh8, db, pr_payload("closed", 8, "finn", HEAD_C, merged=True))
        o8 = _outcome_detail(8)
        print(f"      recorded advice_outcome for PR-8 (merged WHILE red): {o8}")
        recall = isinstance(o8, str) and "land=conflicted" in o8 and "conf=observed" in o8
        check("RECALL-SAFE (#334): a PR merged WHILE red (red fact AT the merged head) is STILL graded "
              "land=conflicted;conf=observed — the sha-aware scope catches a real bad landing, never silenced",
              recall)

        # SCOPE NOTE (the lock's value): the HAPPY PATH is unchanged — a PR that NEVER went red, or one genuinely
        # red AT its merged head, still grades correctly. This gate isolates the transient-red-then-fixed case
        # (now graded clean) from the merged-while-red case (still conflicted), so it can never mask a regression.

        print("ANSWER-CHECK STALE-RED-FACT GATE: PASS" if not FAIL else
              f"ANSWER-CHECK STALE-RED-FACT GATE: FAIL ({len(FAIL)} failed)")
        return 0 if not FAIL else 1
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
