#!/usr/bin/env python3
"""OUTCOME RESULTS gate — the ANSWER-CHECK (答え合わせ): Veripsa measuring its OWN accuracy on real data.

Today Veripsa records only its PREDICTION (the serialize/warn/clear verdict). This gate proves the new
append-only OUTCOME capture: at PR-close, the App grades its advice against the eventual outcome — content-free,
records-not-correctness (we record FACTS, we NEVER assert "we were right"). It drives the REAL brain
(webhook.handle_pull_request) over the REAL gate (db/schema.sql) authed as the App identity (veripsa_app,
delegation), through the same runtime path — only the GitHub I/O is absent (the brain takes a plain event
dict). It proves the four required facts + the two headline stats + content-freeness:

  1. ignored → conflicted recorded as a TRUE POSITIVE   (Veripsa serialized, the dev merged out of order, it conflicted)
  2. followed → clean      recorded                      (a clear PR landed cleanly = the intended good path)
  3. CLEARED → conflicted  recorded as a SILENT MISS     (we said clear, it conflicted = the false negative we sell against)
  4. the confusion MATRIX + the TWO headline stats compute correctly
     (ignored-advice → conflicted rate, and silent-miss count)
  5. everything content-free (no file body anywhere in the recorded outcome facts).

Run:  python3 tests/test_outcome_results.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
from cg_schema_contract import (  # noqa: E402
    EXTRACTOR_VERSION,
    SCHEMA_CONTRACT_VERSION,
)
from webhook import handle_pull_request  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): per-PID, exactly like db/smoke.sh / test_server / run_gates — a fixed name lets
# two concurrent runs drop each other's DB mid-run.
DB = "veripsa_outcometest_" + str(os.getpid())
REPO = "acme/answer"
BRANCH = "main"
INSTALL_ACCOUNT = "808808"          # the stable owning-account id enter_installation routes by → ACCT-GH-808808
TENANT = f"ACCT-GH-{INSTALL_ACCOUNT}"

PASS, FAIL = [], []


def check(label, cond):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def make_db(role):
    """A db(sql, args) runner authed as `role`, pinning the tenant on the SAME connection the call runs on (the
    App enters the installation per event). One connection per call (matches the live per-event model)."""
    def run(sql, args=()):
        conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                if role == "veripsa_app":
                    cur.execute("SELECT core.enter_installation_with_authority(%s)", (INSTALL_ACCOUNT,))
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    return run


def readback(sql, args=()):
    """Raw SELECTs against the ledger go through the MIGRATOR with the tenant account pinned (the App writes via
    gates; a buyer/App role cannot raw-SELECT core.event past RLS). Exactly the lifecycle test's readback model."""
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, false)", (TENANT,))
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _surface(db):
    r = db("SELECT core.outcome_results_surface()")
    return r if isinstance(r, dict) else (json.loads(r) if r else {})


def pr_event(action, pr, author, *, paths, merged=False, conflicted=False, reverted=False, confidence="inferred"):
    """A content-free pull_request event dict (the shape server.py builds + handle_pull_request consumes)."""
    return {
        "action": action, "repo": REPO, "base_branch": BRANCH, "pr_number": pr,
        "changed_files": list(paths), "changed_ranges": {}, "author": author,
        "head_sha": (str(pr) * 40)[:40].rjust(40, "0"), "land_sha": (str(pr) * 40)[:40].rjust(40, "0"),
        "merged": merged, "model": None, "truncated_files": False,
        "conflicted": conflicted, "reverted": reverted, "outcome_confidence": confidence,
    }


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    try:
        db = make_db("veripsa_app")
        # enter the installation once up-front too (provisions the tenant account so reads resolve to it).
        db("SELECT 1")
        # Seed a MINIMAL main graph so the files are KNOWN nodes — otherwise every path is 'unknown' (honest
        # recall: a path not in main's graph can't be predicted) and we could never get a genuine 'clear' verdict
        # for the silent-miss scenario. Three INDEPENDENT file nodes (no edges → no coupling) so a lone PR on any
        # one resolves to 'clear'. Content-free (paths + a file node kind only). This is the same ingest the live
        # App runs on a push to main, just hand-seeded to a tiny shape.
        graph = {
            "extractor_version": EXTRACTOR_VERSION,
            "metrics": {"schema_contract_version": SCHEMA_CONTRACT_VERSION},
            "nodes": [
                {"id": "src/core.py", "kind": "file", "path": "src/core.py", "name": "core.py"},
                {"id": "src/widget.py", "kind": "file", "path": "src/widget.py", "name": "widget.py"},
                {"id": "src/report.py", "kind": "file", "path": "src/report.py", "name": "report.py"},
            ],
            "edges": [],
        }
        db("SELECT core.ingest_graph_with_authority(%s::jsonb,%s,%s,%s)",
           (json.dumps(graph), REPO, BRANCH, "a" * 40))

        # ── SCENARIO A — ignored → conflicted = TRUE POSITIVE ────────────────────────────────────────────────
        # PR-1 and PR-2 both touch the SAME file. The gate serializes PR-2 behind PR-1 (a real serialize verdict
        # from main_impact_surface over the live claim lanes — no graph needed for a same-file direct collision).
        # Then PR-2 is MERGED out of order (PR-1 still in-flight) and the merge conflicted → graded ignored+conflicted.
        SHARED = "src/core.py"
        a1 = handle_pull_request(db, pr_event("opened", 1, "alice", paths=[SHARED]), "alice", act_for=True)
        a2 = handle_pull_request(db, pr_event("opened", 2, "bob", paths=[SHARED]), "bob", act_for=True)
        # PR-2 must be the one serialized behind PR-1 (the second claimant waits). Confirm the gate said so.
        v2 = (a2.get("check") or {}).get("conclusion")
        check("scenario A: the same-file second PR is serialized by the gate (a real 'wait in line' prediction)",
              "serialize" in json.dumps(a2).lower() or v2 in ("action_required", "neutral", "failure"))
        # a prediction fact for PR-2 was snapshotted (verdict + behind PR-1) at open — the answer-check substrate.
        pred2 = readback("SELECT detail FROM core.event WHERE kind='prediction' AND path='PR-2' AND repo=%s LIMIT 1", (REPO,))
        check("scenario A: PR-2's prediction snapshot is on record (verdict + the land order it was told to follow)",
              isinstance(pred2, str) and "behind=" in pred2 and "PR-1" in pred2)
        # MERGE PR-2 out of order, conflicted (observed). This is recorded BEFORE land_change releases PR-2's lanes,
        # while PR-1 still holds its lane → PR-2 jumped the queue → advice IGNORED.
        handle_pull_request(db, pr_event("closed", 2, "bob", paths=[SHARED], merged=True,
                                         conflicted=True, confidence="observed"), "bob", act_for=True)
        o2 = readback("SELECT detail FROM core.event WHERE kind='advice_outcome' AND path='PR-2' AND repo=%s LIMIT 1", (REPO,))
        check("scenario A: PR-2 outcome = ignored + conflicted = TRUE POSITIVE (we were right, on record as a fact)",
              isinstance(o2, str) and "adv=ignored" in o2 and "land=conflicted" in o2 and "conf=observed" in o2)
        # now PR-1 lands cleanly (a clear PR, followed → clean) — the good path.
        handle_pull_request(db, pr_event("closed", 1, "alice", paths=[SHARED], merged=True), "alice", act_for=True)

        # ── SCENARIO B — followed → clean ────────────────────────────────────────────────────────────────────
        # PR-3 touches a lone file (no collision → 'clear'), merges cleanly. Followed (no outstanding predecessor) + clean.
        SOLO = "src/widget.py"
        b3 = handle_pull_request(db, pr_event("opened", 3, "carol", paths=[SOLO]), "carol", act_for=True)
        handle_pull_request(db, pr_event("closed", 3, "carol", paths=[SOLO], merged=True), "carol", act_for=True)
        o3 = readback("SELECT detail FROM core.event WHERE kind='advice_outcome' AND path='PR-3' AND repo=%s LIMIT 1", (REPO,))
        check("scenario B: PR-3 outcome = followed + clean recorded (the intended good path, on record)",
              isinstance(o3, str) and "adv=followed" in o3 and "land=clean" in o3)

        # ── SCENARIO C — CLEARED → conflicted = SILENT MISS (false negative) ──────────────────────────────────
        # PR-4 was predicted 'clear' (a lone file, no in-flight coupling Veripsa could see) but conflicted ANYWAY
        # — a missed coupling, the failure Veripsa sells against. This is THE most important class to capture.
        LONE = "src/report.py"
        handle_pull_request(db, pr_event("opened", 4, "dave", paths=[LONE]), "dave", act_for=True)
        pred4 = readback("SELECT detail FROM core.event WHERE kind='prediction' AND path='PR-4' AND repo=%s LIMIT 1", (REPO,))
        check("scenario C: PR-4 was predicted 'clear' (no coupling Veripsa could see)",
              isinstance(pred4, str) and "verdict=clear" in pred4)
        handle_pull_request(db, pr_event("closed", 4, "dave", paths=[LONE], merged=True,
                                         conflicted=True, confidence="observed"), "dave", act_for=True)
        o4 = readback("SELECT detail FROM core.event WHERE kind='advice_outcome' AND path='PR-4' AND repo=%s LIMIT 1", (REPO,))
        check("scenario C: PR-4 outcome = cleared + conflicted = the SILENT MISS (false negative) captured",
              isinstance(o4, str) and "pred=clear" in o4 and "land=conflicted" in o4)

        # ── THE CONFUSION MATRIX + THE TWO HEADLINE STATS ────────────────────────────────────────────────────
        surf = _surface(db)
        m = surf.get("matrix") or {}
        check("matrix: ignored_conflicted_tp == 1 (scenario A)", m.get("ignored_conflicted_tp") == 1)
        check("matrix: followed_clean >= 1 (scenario B)", (m.get("followed_clean") or 0) >= 1)
        check("matrix: cleared_conflicted_fn == 1 (scenario C, the silent miss)", m.get("cleared_conflicted_fn") == 1)
        # headline stat 1: ignored-advice → conflicted rate. Exactly 1 ignored non-clear (PR-2) and it conflicted → 1.0.
        rate = surf.get("ignored_advice_conflicted_rate")
        denom = surf.get("ignored_advice_denominator")
        check("headline 1: ignored_advice_conflicted_rate == 1.000 over denominator 1 (PR-2)",
              denom == 1 and rate is not None and abs(float(rate) - 1.0) < 1e-9)
        # headline stat 2: silent-miss count = cleared-but-conflicted = exactly 1 (PR-4).
        check("headline 2: silent_miss_count == 1 (PR-4 cleared-but-conflicted)", surf.get("silent_miss_count") == 1)
        check("total_outcomes == 3 (PR-1 clear-clean + PR-2 + PR-3 + PR-4 ... 4 merges, 4 graded)",
              surf.get("total_outcomes") == 4)

        # ── CONTENT-FREE: the recorded outcome/prediction facts carry NO code body — only verdict tokens, a
        # change ref, booleans/labels. Scan every detail of the two new kinds for any of the file paths' basenames
        # appearing as code (paths themselves are content-free, but a BODY fragment would be a leak). We assert the
        # bounded shape: detail matches the closed grammar (pred=…;adv=…;land=…;conf=… / verdict=…;behind=…) only.
        details = readback(
            "SELECT array_agg(detail) FROM core.event WHERE kind IN ('prediction','advice_outcome') AND repo=%s",
            (REPO,)) or []
        import re
        grammar = re.compile(r"^(verdict=[a-z_]+;behind=[A-Za-z0-9_:.,\-]*|pred=[a-z_]+;adv=[a-z]+;land=[a-z]+;conf=[a-z]+)$")
        bad = [d for d in details if not grammar.match(d or "")]
        check("content-free: every prediction/outcome detail matches the closed content-free grammar (no body leak)",
              not bad)

        print("OUTCOME RESULTS GATE: PASS" if not FAIL else f"OUTCOME RESULTS GATE: FAIL ({len(FAIL)} failed)")
        return 0 if not FAIL else 1
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
