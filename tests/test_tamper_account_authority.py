#!/usr/bin/env python3
"""TAMPER-RESISTANCE — forging the ACCOUNT-RESOLUTION authority of the webhook→tenant binding. Security-critical.

The whole identity moat rests on ONE discipline: a customer-controlled webhook payload can only become a
content-free LABEL — it must never PICK the tenant. The tenant is re-derived from the AUTHENTICATED connection
(core.installation_account, session-pinned by the trusted enter_installation route) and re-pinned before any
write. A moat red-team found two App-fed paths that broke that discipline; this gate proves BOTH are now closed,
adversarially, against the REAL gate + the REAL per-event processor (not a mock), while every LEGITIMATE flow
still succeeds:

  F1 — set_account_plan_with_authority TRUSTED the payload account.id with no ownership check. The GitHub
       Marketplace `marketplace_purchase` webhook carries account.id in the PAYLOAD; HMAC proves GitHub SENT the
       event, NOT that the purchaser owns that account.id. A validly-signed event carrying a VICTIM's account.id
       would flip the VICTIM's plan (a force-downgrade re-erects the victim's quota wall = DoS). THE FIX: the
       setter re-pins the authority from the connection — IF this connection resolves to a genuinely-routed
       account (a core.installation_account ROW exists, written ONLY by the trusted route), the payload account
       MUST agree; on MISMATCH it refuses (no-op). When NO context is pinned (a pre-install first purchase — the
       legitimate common case, since a marketplace_purchase carries no installation so nothing is pinned), it
       ALLOWS (GitHub's signature is the authority there). We assert: MISMATCH is refused; a MATCHing call and a
       NO-INSTALL first purchase both SUCCEED.

  F2 — the per-event processor picked the tenant from repository.owner.id … installation.id with NO assertion the
       two AGREE. HMAC proves GitHub delivered the event, NOT that the repo in the body belongs to the account
       that owns the DELIVERING installation. THE FIX: a STRUCTURAL check — when BOTH a repository.owner.id account
       AND an installation-derived account resolve, assert they're equal; on a PROVABLE mismatch DROP the event
       (clean no-op, content-free log), before any tenant pin / lock / dispatch. Fail-soft (a mismatch drops, not
       a 500; an unresolvable reference proceeds). We assert: a CONSISTENT event proceeds (a claim lands); an event
       whose repository.owner.id disagrees with installation.account.id is DROPPED (nothing lands in EITHER tenant).

HONEST-EMPTY: every forge below is REFUSED / dropped, and every legitimate flow still lands. If any forge ever
SUCCEEDS, or any legitimate flow is broken, this gate FAILS — do not ship.

Run:  python3 tests/test_tamper_account_authority.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
import server as S  # noqa: E402 — re-exports make_db_processor / handle_event / _scoped_db
from _installation_fixture import seed_live_installation  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): per-PID like db/smoke.sh + run_gates, so two concurrent runs never drop each
# other's scratch DB mid-run.
DB = "veripsa_acctauth_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
DSN_MIG = f"postgresql://veripsa_migrator@localhost/{DB}"

# F1 actors: an installation pinned on the connection (the routed tenant) + a SECOND, unrelated account (the
# victim a hostile marketplace payload would try to flip).
INST_OWNER = "6100"                       # connection routes to ACCT-GH-6100 (enter_installation '6100')
ACCT_INST = "ACCT-GH-6100"
VICTIM_GH = "6200"                        # a DIFFERENT GitHub account id — the force-downgrade target
ACCT_VICTIM = "ACCT-GH-6200"
FIRST_PURCHASE_GH = "6300"               # a pre-install first purchase (no connection pinned)
ACCT_FIRST = "ACCT-GH-6300"

# F2 actors: an honest installation whose repo owner matches it, and a hostile event pairing one installation's
# delivery with another account's repo owner.
HONEST_OWNER_ID = 7100                    # → ACCT-GH-7100
HONEST_REPO = "honestco/app"
HONEST_INSTALL_ID = 4300
FORGED_OWNER_ID = 7200                    # the repo claims THIS owner …
ACCT_FORGED = "ACCT-GH-7200"

# a valid 40-char HEX sha (the gate's ingest/freshness paths reject non-hex). The PR head AND the client's
# default-branch-head read-back use the SAME sha so the freshness self-heal sees "already at head" (no re-ingest).
HEAD_SHA = "abcdef0123456789abcdef0123456789abcdef01"


def admin(sql, args=()):
    """A migrator-pinned read past RLS (the App role writes via gates, cannot raw-SELECT the tenant tables)."""
    conn = psycopg2.connect(DSN_MIG)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def app_run(stmts):
    """Run a sequence of (sql, args) as veripsa_app on ONE held autocommit connection (so a session-level pin set
    by an earlier statement survives the later ones — the live per-event processor's shape). Returns the last
    statement's first column. Used to ROUTE a connection (enter_installation) then write on it, with proper
    parameter binding (never string-interpolated SQL)."""
    conn = psycopg2.connect(DSN_APP)
    try:
        conn.autocommit = True
        last = None
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            for sql, args in stmts:
                cur.execute(sql, args)
                try:
                    row = cur.fetchone()
                    last = row[0] if row else None
                except psycopg2.ProgrammingError:
                    last = None
        return last
    finally:
        conn.close()


def plan_of(account):
    """Ground-truth plan column for `account` (migrator, pinned — account is FORCE-RLS)."""
    return admin(
        "SELECT set_config('core.current_account',%s,true); SELECT plan FROM core.account WHERE account_id=%s",
        (account, account))


def account_exists(account):
    return admin("SELECT set_config('core.current_account',%s,true); "
                 "SELECT count(*)::int FROM core.account WHERE account_id=%s", (account, account))


# ── A tiny gh for the realistic PR-processing path (list_pr_files / checks / comments). The F2 structural check
#    is PAYLOAD-ONLY (repository.owner.id vs installation.account.id — both in the body), so it makes NO gh call;
#    this client only needs to serve the handler's normal reads/writes. ─────────────────────────────────────────
class FakeGH:
    def __init__(self, files=None):
        self._files = files or {}
        self.checks, self.comments, self.install_ids = [], [], []
        self._comment_id, self._check_id = 1000, 2000

    def for_installation(self, installation_id):
        self.install_ids.append(str(installation_id)); return self

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return list(self._files.keys())

    def list_pr_files_with_ranges(self, repo, number, pr_changed_files=0):
        return dict(self._files)

    def pull_request_head(self, repo, number):
        return HEAD_SHA

    def pull_request_head_and_fork(self, repo, number):
        return (HEAD_SHA, False)

    def repo_default_branch_head(self, repo):
        return ("main", HEAD_SHA)     # (default_branch, head_sha) — mirror github_rest's shape

    def upsert_check(self, repo, sha, conclusion, title, summary):
        self._check_id += 1
        self.checks.append({"id": self._check_id, "sha": sha, "conclusion": conclusion})

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha]

    def post_comment(self, repo, number, body):
        self._comment_id += 1
        self.comments.append({"id": self._comment_id, "number": number, "body": body, "user": {"type": "Bot"}})

    def list_issue_comments(self, repo, number):
        return [c for c in self.comments if c["number"] == number]

    def upsert_comment(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"] or c["body"].startswith("### Veripsa"):
                c["body"] = body
                return c
        return self.post_comment(repo, number, body)

    def patch_comment_if_exists(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"]:
                c["body"] = body() if callable(body) else body
                return True
        return False


def _pr_payload(action, repo, owner_id, install_id, inst_account_id, number):
    """A same-repo pull_request payload. owner_id vs inst_account_id is what the F2 check cross-asserts."""
    return {
        "action": action, "number": number,
        "installation": {"id": install_id, "account": {"id": inst_account_id}},
        "repository": {"full_name": repo, "default_branch": "main",
                       "owner": {"id": owner_id, "login": "x"}, "id": owner_id * 10},
        "pull_request": {"number": number, "user": {"login": "dev"}, "draft": False, "merged": False,
                         "head": {"sha": HEAD_SHA, "ref": "feature",
                                  "repo": {"id": owner_id * 10, "full_name": repo}},
                         "base": {"ref": "main", "sha": HEAD_SHA,
                                  "repo": {"id": owner_id * 10, "full_name": repo}}},
    }


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    seed_live_installation(
        DSN_APP,
        DSN_MIG,
        HONEST_OWNER_ID,
        HONEST_INSTALL_ID,
    )

    checks = []

    def ck(label, passed):
        checks.append((label, bool(passed)))

    # ════════════════════════════════════════════════════════════════════════════════════════════════════
    # F1 — set_account_plan_with_authority must not trust the payload account.id when the connection is routed
    # ════════════════════════════════════════════════════════════════════════════════════════════════════

    # Seed the victim with a PAID plan so a successful force-downgrade would be VISIBLE (and harmful: it re-erects
    # the quota wall). Done WITHOUT any connection pin (a first purchase), which is itself the F1-ALLOW path.
    app_run([("SELECT core.set_account_plan_with_authority(%s,%s)", (VICTIM_GH, "enterprise"))])
    ck(f"F1 setup: victim {ACCT_VICTIM} starts PAID ('enterprise') via a no-install first purchase (the ALLOW path)",
       plan_of(ACCT_VICTIM) == "enterprise")

    # F1-ATTACK: open ONE App connection, ROUTE it to the installation tenant (ACCT-GH-6100) via the trusted
    # route (writes the core.installation_account row + pins the GUC) — exactly the live per-event processor —
    # then, on that SAME connection, call the plan setter with the VICTIM's account id. The connection resolves to
    # ACCT-GH-6100, so the payload's 6200 MISMATCHES the routed tenant ⇒ MUST be refused (returns NULL, no-op).
    # The victim's paid plan must be byte-for-byte unchanged. (app_run uses psycopg2 so the refusal WARNING goes to
    # its own channel — the captured value is the function's true return, NULL on refusal.)
    out_attack = app_run([
        ("SELECT core.enter_installation_with_authority(%s)", (INST_OWNER,)),
        ("SELECT core.set_account_plan_with_authority(%s,%s)", (VICTIM_GH, "free")),
    ])
    ck(f"F1 ATTACK[routed conn → {ACCT_INST}, payload account={ACCT_VICTIM}]: the setter REFUSED (returned NULL); "
       f"got {out_attack!r}", out_attack is None)
    ck(f"F1 ATTACK: the victim's PAID plan is UNCHANGED ('enterprise', the force-downgrade was blocked)",
       plan_of(ACCT_VICTIM) == "enterprise")
    # the routed tenant itself was not silently provisioned/altered by the refused call either (it never wrote)
    ck(f"F1 ATTACK: the routed tenant {ACCT_INST} has NO paid plan written by the refused call "
       f"(plan='free' default, the lazy provision under enter_installation only)",
       plan_of(ACCT_INST) in ("free", None))

    # F1-MATCH: on a routed connection, calling the setter for the SAME account the connection resolves to is
    # LEGITIMATE (the routed tenant buying its own plan) ⇒ SUCCEEDS. Route to 6100, set 6100 paid.
    out_match = app_run([
        ("SELECT core.enter_installation_with_authority(%s)", (INST_OWNER,)),
        ("SELECT core.set_account_plan_with_authority(%s,%s)", (INST_OWNER, "pro")),
    ])
    ck(f"F1 MATCH[routed conn → {ACCT_INST}, payload account={ACCT_INST}]: SUCCEEDS (returns the account)",
       out_match == ACCT_INST)
    ck(f"F1 MATCH: the routed tenant's plan is now 'pro' (a legitimate own-tenant purchase still works)",
       plan_of(ACCT_INST) == "pro")

    # F1-FIRST-PURCHASE: a brand-new GitHub account buys BEFORE any installation exists — the legitimate common
    # case. No connection is routed (a marketplace_purchase pins nothing), so there is nothing to cross-check ⇒
    # ALLOW. This must NOT be over-tightened into refusing first-time purchases.
    out_first = app_run([
        ("SELECT core.set_account_plan_with_authority(%s,%s)", (FIRST_PURCHASE_GH, "team")),
    ])
    ck(f"F1 FIRST-PURCHASE[no installation pinned, payload account={ACCT_FIRST}]: SUCCEEDS (pre-install purchase "
       f"is allowed — GitHub's HMAC is the authority); returns the account", out_first == ACCT_FIRST)
    ck(f"F1 FIRST-PURCHASE: the new account's plan is 'team' (a first-time purchase is NOT refused)",
       plan_of(ACCT_FIRST) == "team")

    # ════════════════════════════════════════════════════════════════════════════════════════════════════
    # F2 — the processor must drop an event whose repository.owner.id disagrees with the delivering installation
    # ════════════════════════════════════════════════════════════════════════════════════════════════════

    proc = S.make_db_processor(DSN_APP)

    # Seed a base graph for the honest repo so its PR is genuinely analyzable (so a CONSISTENT event has real work
    # to land — proving "proceeds" is not a trivial empty no-op).
    import code_graph_extract as X
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
    app_run([
        ("SELECT core.enter_installation_with_authority(%s)", (str(HONEST_OWNER_ID),)),
        ("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
         (json.dumps(graph), HONEST_REPO, "main", "a" * 40)),
    ])

    def claims_in(account, repo, change_id):
        return admin("SELECT set_config('core.current_account',%s,true); "
                     "SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id=%s "
                     "AND claim_state IN ('active','waiting')", (account, repo, change_id))

    acct_honest = "ACCT-GH-%d" % HONEST_OWNER_ID

    # F2-CONSISTENT: repository.owner.id == installation.account.id (the overwhelmingly common live case) ⇒ the
    # event PROCEEDS and its claim lands in the honest owner's tenant.
    gh_ok = FakeGH(files={"backend/api.py": [[1, 20]]})
    crashed_ok = None
    try:
        proc("pull_request",
             _pr_payload("opened", HONEST_REPO, HONEST_OWNER_ID, HONEST_INSTALL_ID, HONEST_OWNER_ID, 11),
             None, gh_ok)
    except Exception as e:
        crashed_ok = f"{type(e).__name__}: {str(e)[:160]}"
    ck("F2 CONSISTENT: a same-repo PR whose owner.id == installation.account.id processes with NO escaped "
       f"exception ({crashed_ok or 'clean'})", crashed_ok is None)
    consistent_claims = claims_in(acct_honest, HONEST_REPO, "PR-11")
    ck(f"F2 CONSISTENT: the event PROCEEDED — its claim landed in the honest tenant ({acct_honest}); "
       f"claims={consistent_claims}", consistent_claims is not None and consistent_claims > 0)

    # F2-MISMATCH: the SAME delivering installation/account (HONEST_OWNER_ID), but the body's repository.owner.id
    # is FORGED to a DIFFERENT account (FORGED_OWNER_ID). The structural check proves owner.id != installation.
    # account.id ⇒ DROP the event (no-op). Nothing must land in EITHER the forged owner's tenant or the honest one.
    forged_repo = "forgedco/repo"
    gh_bad = FakeGH(files={"backend/api.py": [[1, 20]]})
    crashed_bad = None
    try:
        # owner_id=FORGED_OWNER_ID (the repo claims 7200) but installation.account.id=HONEST_OWNER_ID (7100)
        proc("pull_request",
             _pr_payload("opened", forged_repo, FORGED_OWNER_ID, HONEST_INSTALL_ID, HONEST_OWNER_ID, 12),
             None, gh_bad)
    except Exception as e:
        crashed_bad = f"{type(e).__name__}: {str(e)[:160]}"
    ck("F2 MISMATCH: a PR whose repository.owner.id disagrees with installation.account.id is DROPPED with NO "
       f"escaped exception (fail-soft, never a 500) ({crashed_bad or 'clean'})", crashed_bad is None)
    forged_claims = claims_in(ACCT_FORGED, forged_repo, "PR-12")
    ck(f"F2 MISMATCH: the dropped event wrote NOTHING into the FORGED owner's tenant ({ACCT_FORGED}); "
       f"claims={forged_claims}", forged_claims == 0)
    honest_pr12 = claims_in(acct_honest, forged_repo, "PR-12")
    honest_pr12b = admin("SELECT set_config('core.current_account',%s,true); "
                         "SELECT count(*)::int FROM core.claim WHERE change_id='PR-12'", (acct_honest,))
    ck(f"F2 MISMATCH: the dropped event wrote NOTHING into the delivering (honest) tenant either ({acct_honest}); "
       f"PR-12 claims there={honest_pr12b}", (forged_claims == 0) and honest_pr12b == 0)
    # the forged tenant must not even have been lazily provisioned by the dropped event (no enter_installation ran)
    ck(f"F2 MISMATCH: the forged tenant {ACCT_FORGED} was never even provisioned by the dropped event "
       f"(no tenant pin happened before the drop)", account_exists(ACCT_FORGED) == 0)

    # ── verdict ──────────────────────────────────────────────────────────────────────────────────────────
    subprocess.run(["dropdb", DB], capture_output=True, text=True)
    print("\n=== TAMPER ACCOUNT-AUTHORITY — forge the webhook→tenant binding (each forge refused; each legit "
          "flow lands) ===")
    passed = 0
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        passed += 1 if ok else 0
    print(f"\n{passed}/{len(checks)} checks passed "
          "(F1: payload-account mismatch refused; own-tenant + first-purchase succeed | "
          "F2: owner↔installation mismatch dropped; consistent event proceeds).")
    if passed == len(checks):
        print("TAMPER ACCOUNT-AUTHORITY GATE: PASS")
        return 0
    print("TAMPER ACCOUNT-AUTHORITY GATE: FAIL")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # never leak the scratch DB on an unexpected error
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
        print(f"\n[FAIL] unexpected error: {e}")
        print("TAMPER ACCOUNT-AUTHORITY GATE: FAIL")
        raise SystemExit(1)
