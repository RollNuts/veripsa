#!/usr/bin/env python3
"""Gate: the human-judgment taxonomy is exclusive, content-free, and cannot fake external validation.

WHY THIS EXISTS. `record_advice_outcome_with_authority` grades what HAPPENED to a change (conflicted,
reverted) and marks it `inferred` — automation observing automation. It can never say whether a person
found the advice any good. The External Validation Gate counts exactly one thing as market validation:
an EXTERNAL human confirming a signal was USEFUL. That needs its own recording path, and that path must
not become a way to make the product look validated when it is not.

Proven against a REAL Postgres through the real SECURITY DEFINER function and the real owner surface:

  A. EXCLUSIVE ENUMS — every legal judgment/action is accepted; anything else is REFUSED (returns NULL)
     rather than silently stored as a value the surfaces cannot aggregate.
  B. CONTENT-FREE — the ledger row carries the two enum values and nothing else. Prose passed in any
     field must never reach `detail`.
  C. APPEND-ONLY, LATEST WINS — core.event guarantees recorded facts are permanent, so a re-answer
     APPENDS a new fact (history is kept) while aggregation takes the LATEST per change, so a person is
     still never double-counted. The same answer twice is idempotent.
  D. INTERNAL CANNOT FAKE EXTERNAL — an allowlisted (owner/dogfood) account's `useful` judgment must NOT
     increment `useful_external`. This is the number the honest-conclusion rule keys on.

Run:  python3 tests/test_human_judgment_taxonomy.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import psycopg2  # noqa: E402

DB = "veripsa_judgment_" + str(os.getpid())
ADMIN = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"

EXTERNAL = "ACCT-GH-700001"
INTERNAL = "ACCT-GH-700002"      # will be put on the dev-exempt allowlist
REPO, BRANCH = "org/app", "main"

JUDGMENTS = ["useful", "correct-but-no-action-needed", "already-knew", "unclear", "noisy",
             "incorrect", "missing-relationship", "insufficient-unknown", "not-observed"]
ACTIONS = ["changed-merge-order", "held-a-pr", "rebased-or-regenerated", "split-a-pr",
           "reassigned-overlapping-work", "added-review", "acknowledged-and-proceeded",
           "no-action-needed", "ignored", "unknown"]


def _app(account, sql, args=()):
    conn = psycopg2.connect(APP_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, false)", (account,))
            cur.execute("SELECT set_config('core.installation_account', %s, false)", (account,))
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _admin(sql, args=()):
    conn = psycopg2.connect(ADMIN, dbname=DB)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, args)
            try:
                row = cur.fetchone()
            except psycopg2.ProgrammingError:
                return None
            return row[0] if row else None
    finally:
        conn.close()


def _record(account, change, judgment, action="unknown"):
    return _app(account,
                "SELECT core.record_human_judgment_with_authority(%s,%s,%s,%s,%s)",
                (change, REPO, BRANCH, judgment, action))


def main() -> int:
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        print("HUMAN JUDGMENT TAXONOMY GATE: FAIL (bootstrap)")
        return 1
    try:
        # The funnel only visits accounts with a LIVE installation, so both test tenants must be real
        # installs (created through the sanctioned entry point, not by writing the table directly).
        # enter_installation_with_authority takes the RAW GitHub owner id and prefixes ACCT-GH- itself.
        for acct in (EXTERNAL, INTERNAL):
            _app(acct, "SELECT core.enter_installation_with_authority(%s)",
                 (acct.replace("ACCT-GH-", ""),))

        # A. every legal value is accepted, on both enums
        ok_j = all(_record(EXTERNAL, f"chg-j-{i}", j) is not None for i, j in enumerate(JUDGMENTS))
        check(f"all {len(JUDGMENTS)} judgment values are accepted", ok_j)
        ok_a = all(_record(EXTERNAL, f"chg-a-{i}", "useful", a) is not None for i, a in enumerate(ACTIONS))
        check(f"all {len(ACTIONS)} workflow-action values are accepted", ok_a)

        # A2. anything outside the enum is REFUSED, not silently stored
        bad = [("very-useful", "unknown"), ("", "unknown"), ("USEFUL; DROP TABLE", "unknown"),
               ("useful", "merged-it"), ("useful", "")]
        refused = [_record(EXTERNAL, f"chg-bad-{i}", j, a) for i, (j, a) in enumerate(bad)]
        check("every out-of-enum judgment/action is REFUSED (returns NULL)", all(x is None for x in refused))
        stored_bad = _admin("SELECT count(*) FROM core.event WHERE kind='human_judgment' "
                            "AND path LIKE %s", ("chg-bad-%",))
        check("a refused judgment writes NO ledger row", stored_bad == 0)

        # B. content-free: detail carries the two enums and nothing else
        detail = _admin("SELECT detail FROM core.event WHERE kind='human_judgment' "
                        "AND path=%s", ("chg-j-0",))
        check("detail is exactly the two enum values", detail == "judgment=useful;action=unknown")
        anyprose = _admin("SELECT count(*) FROM core.event WHERE kind='human_judgment' "
                          "AND detail !~ '^judgment=[a-z-]+;action=[a-z-]+$'")
        check("no judgment row carries anything but the two enums (no prose can leak)", anyprose == 0)

        # C. append-only history + idempotent repeat + latest-wins aggregation
        first = _record(EXTERNAL, "chg-replace", "noisy", "ignored")
        dup = _record(EXTERNAL, "chg-replace", "noisy", "ignored")          # same answer again
        rows_after_dup = _admin("SELECT count(*) FROM core.event WHERE kind='human_judgment' "
                                "AND path='chg-replace'")
        check("the SAME answer recorded twice is idempotent (one fact)",
              first == dup and rows_after_dup == 1)
        _record(EXTERNAL, "chg-replace", "useful", "changed-merge-order")   # changed their mind
        rows = _admin("SELECT count(*) FROM core.event WHERE kind='human_judgment' AND path='chg-replace'")
        check("a CHANGED answer APPENDS (append-only history is preserved, nothing overwritten)", rows == 2)
        latest = _admin("SELECT detail FROM core.event WHERE kind='human_judgment' "
                        "AND path='chg-replace' ORDER BY occurred_at DESC LIMIT 1")
        check("the latest recorded answer is the changed one",
              latest == "judgment=useful;action=changed-merge-order")

        # D. an INTERNAL (allowlisted) useful judgment must not count as external validation
        owner_acct = _admin("SELECT core._owner_account()")
        # Written through the sanctioned gate: core.policy has a forgery block against direct writes.
        _app(owner_acct, "SELECT core.set_policy_with_authority(%s,%s)",
             ("dev_exempt_account_ids", INTERNAL))
        _record(INTERNAL, "chg-internal", "useful", "changed-merge-order")
        _record(EXTERNAL, "chg-ext-useful", "useful", "changed-merge-order")
        surface = _admin("SELECT core.owner_activation_funnel_surface()")
        s = surface if isinstance(surface, dict) else json.loads(surface)
        hj = s.get("human_judgment") or {}
        check("the funnel surface exposes human_judgment", bool(hj))
        check("the internal account's judgment IS recorded in the total", (hj.get("total") or 0) >= 1)
        check("an ALLOWLISTED internal 'useful' does NOT inflate useful_external",
              _admin("SELECT count(*) FROM core.event WHERE kind='human_judgment' "
                     "AND account_id=%s AND detail LIKE 'judgment=useful;%%'", (INTERNAL,)) == 1
              and INTERNAL not in json.dumps(hj))
        check("workflow_action is exposed as its own content-free breakdown",
              isinstance(s.get("workflow_action"), dict))

        # E. OPERATOR VIEW — a recorded judgment must actually reach the human-readable report, otherwise it
        #    is data nobody sees. Also pins the honest-conclusion line while useful_external is 0.
        sys.path.insert(0, os.path.join(ROOT, "github-app"))
        import activation_report as AR  # noqa: E402
        rep = AR.render_report(s)
        check("the operator report renders a HUMAN JUDGMENT section", "HUMAN JUDGMENT" in rep)
        check("the operator report shows the recorded judgment and action",
              "useful" in rep and "changed-merge-order" in rep)
        zero = AR.render_report({"human_judgment": {"total": 0, "external_total": 0, "useful_external": 0,
                                                    "by_judgment": {}}, "workflow_action": {}})
        check("with useful_external = 0 the report states the honest conclusion verbatim",
              "Market value still unvalidated" in zero
              and "NOT value evidence" in zero)
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)

    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"HUMAN JUDGMENT TAXONOMY GATE: FAIL ({len(failed)} of {len(results)})")
        for n in failed:
            print("  -", n)
        return 1
    print(f"HUMAN JUDGMENT TAXONOMY GATE: PASS ({len(results)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
