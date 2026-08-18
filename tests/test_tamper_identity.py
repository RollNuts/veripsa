#!/usr/bin/env python3
"""TAMPER-RESISTANCE — forging the record's IDENTITY (the WHO / WHICH-ACCOUNT / WHEN). Security-critical.

Veripsa's product is a FAITHFUL record of who did what, when, in which account. The whole value collapses
if a caller can forge any of those four things. This gate proves — adversarially, against the live gate —
that every avenue to forge IDENTITY is refused:

  1. CROSS-ACCOUNT WRITE  — land a row under ANOTHER tenant's account (the cardinal sin). Tried via:
       (a) a tenant pre-setting core.current_account = <victim> before a gate call (the gate must re-pin),
       (b) a DIRECT INSERT into a core table under the victim account (no table grant + RLS WITH CHECK),
       (c) a tenant spoofing core.installation_account = <victim> (honored ONLY for session_user=veripsa_app),
       (d) calling the routing fn (enter_installation_with_authority) — not granted to a tenant writer,
       (e) the App ITSELF pre-setting current_account before act_for (establish_* re-derives from the pin).
  2. ACTOR FORGERY        — record a fact AS a different agent. The delegation gate (act_for_claim) attributes
       a claim to the real PR author (GH-<login>) BY DESIGN — but the actor is a content-free label and the
       account is the connection's own tenant, so a forged author can NEVER place a row in another account,
       and the author-agent is provisioned INSIDE the caller's account. A plain writer can't call act_for at all.
  3. TIMESTAMP / BACKDATE — set occurred_at / claimed_at / stated_at to a forged/backdated value. The audit
       ledger timestamps are server-set (DEFAULT now()); no gate fn takes a caller timestamp for them. The one
       caller-supplied time (ingest_graph p_captured_at = the snapshot's commit time, snapshot metadata on
       graph_version) does NOT touch the audit ledger, and its un-forgeable companion ingested_at is server now().
  4. IDENTITY-RESOLUTION SPOOF — trick resolve_session_identity into the wrong account/actor (a forged session
       GUC, an unprovisioned role). Refused: identity comes from the connection role's credential / the App's
       installation pin, never a value the caller passes.

HONEST-EMPTY is the goal: every forge attempt below is REFUSED (or silently corrected to the caller's own
identity), so the test PASSES with zero schema changes. If any forge ever SUCCEEDS, this gate FAILS — do not ship.

Run:  python3 tests/test_tamper_identity.py     (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the bootstrap drops+recreates this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / sibling tamper lanes) drop each other's DB mid-run. Per-PID, like db/smoke.sh / run_gates.
DB = "veripsa_tamperid_" + str(os.getpid())

DEMO = "ACCT-DEMO"          # the bootstrapped demo account (role veripsa_demo_agent → agent AG-A)
GH111 = "ACCT-GH-111"       # tenant created by entering installation 111 as the App
GH222 = "ACCT-GH-222"       # a SECOND tenant (installation 222) — the victim in cross-account probes


def connect(role):
    c = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    c.autocommit = True
    return c


def run(conn, sql, args=()):
    """Run one statement on an autocommit connection; return the first column of the first row (or None)."""
    with conn.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute(sql, args)
        try:
            row = cur.fetchone()
        except psycopg2.ProgrammingError:
            return None
        return row[0] if row else None


def attempt(conn, sql, args=()):
    """Run a FORGE attempt; return (ok, value_or_errtext). ok=True means it ran (a potential HOLE), ok=False
    means the gate refused it (the desired outcome for an illegitimate write)."""
    try:
        return True, run(conn, sql, args)
    except psycopg2.Error as e:
        return False, str(e).strip().splitlines()[0]


class Reader:
    """A migrator-owned reader that can pin ANY account (FORCE RLS still applies, so it reads exactly that
    account's rows). Uses a NON-autocommit txn so a txn-local current_account pin survives across the SELECT."""

    def __init__(self):
        self.c = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        self.c.autocommit = False

    def scalar(self, account, sql, args=()):
        with self.c.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account',%s,true)", (account,))
            cur.execute(sql, args)
            r = cur.fetchone()
        self.c.rollback()
        return r[0] if r else None

    def count_stmt(self, account, utterance):
        return self.scalar(account, "SELECT count(*) FROM core.statement WHERE account_id=%s AND utterance=%s",
                           (account, utterance))

    def count_event(self, account, detail):
        return self.scalar(account, "SELECT count(*) FROM core.event WHERE account_id=%s AND detail=%s",
                           (account, detail))


def app_entered(installation_id):
    """An autocommit connection AS the App service identity, having ENTERED `installation_id` (so the whole
    'event' runs in that installation's tenant — exactly the live per-webhook processor)."""
    c = connect("veripsa_app")
    run(c, "SELECT core.enter_installation_with_authority(%s)", (installation_id,))
    return c


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    checks = []

    def ck(label, passed):
        checks.append((label, bool(passed)))

    rd = Reader()

    # tenant A = the bootstrapped demo seat (a plain writer). tenant B = a second GH installation (the victim).
    A = connect("veripsa_demo_agent")           # AG-A / ACCT-DEMO  (a NON-app writer)
    appB = app_entered("222")                    # the App, routed into ACCT-GH-222 (B, the victim tenant)
    appA = app_entered("111")                    # the App, routed into ACCT-GH-111 (a clean App tenant)

    # sanity: the App's two installations resolve to two DISTINCT accounts, and A is the demo account
    ck(f"baseline: A is the demo tenant (got {run(A, 'SELECT (core.resolve_session_identity()).o_account')})",
       run(A, "SELECT (core.resolve_session_identity()).o_account") == DEMO)
    ck(f"baseline: App installation 222→{run(appB, 'SELECT current_setting(%s)', ('core.installation_account',))} "
       f"/ 111→{run(appA, 'SELECT current_setting(%s)', ('core.installation_account',))} (two distinct tenants)",
       run(appB, "SELECT current_setting('core.installation_account')") == GH222
       and run(appA, "SELECT current_setting('core.installation_account')") == GH111
       and GH222 != GH111)

    # ════════════════════════════════════════════════════════════════════════════════════════════════════
    # THREAT 1 — CROSS-ACCOUNT WRITE (forge a row under ANOTHER tenant)
    # ════════════════════════════════════════════════════════════════════════════════════════════════════

    # 1a. A pre-sets core.current_account = B (the victim) then writes a statement through the gate. The gate's
    #     establish_session_write_context() MUST re-pin current_account to A's OWN derived account, so the row
    #     lands under A — NEVER under B. (A self-pin is silently overridden, not a privileged cross-tenant write.)
    run(A, "SELECT set_config('core.current_account',%s,false)", (GH222,))
    ok, sid = attempt(A, "SELECT core.record_statement_with_authority(%s,%s)", ("FORGE-1A-CROSSPIN", "x.py"))
    landed_b = rd.count_stmt(GH222, "FORGE-1A-CROSSPIN")
    landed_a = rd.count_stmt(DEMO, "FORGE-1A-CROSSPIN")
    ck(f"1a CROSS-ACCT[pre-set current_account=victim]: row lands in A's OWN acct, NOT B "
       f"(B={landed_b}, A={landed_a}; gate re-pinned)",
       ok and landed_b == 0 and landed_a == 1)
    # reset A's leaked pin so later probes start clean (each gate call re-pins anyway)
    run(A, "SELECT set_config('core.current_account','',false)")

    # 1b. A DIRECT INSERT into a core table, even with the forgery token armed AND the victim account named, is
    #     refused — a tenant has NO table GRANT (the only write path is the gate). Belt: the forgery trigger too.
    run(A, "SELECT set_config('core.governed_write_token','event',true)")
    run(A, "SELECT set_config('core.current_account',%s,true)", (GH222,))
    ok, err = attempt(A, "INSERT INTO core.event(event_id,account_id,kind,agent_id) "
                         "VALUES ('EV-FORGE-1B',%s,'push','AG-A')", (GH222,))
    ck(f"1b CROSS-ACCT[direct INSERT under victim + armed token]: REFUSED ({'ran — HOLE' if ok else err})",
       (not ok) and rd.count_event(GH222, None) is not None and
       rd.scalar(GH222, "SELECT count(*) FROM core.event WHERE event_id='EV-FORGE-1B'") == 0)
    run(A, "SELECT set_config('core.governed_write_token','',true)")
    run(A, "SELECT set_config('core.current_account','',true)")

    # 1c. A (a NON-app role) spoofs core.installation_account = B and resolves identity. The installation pin is
    #     honored ONLY for session_user='veripsa_app'; any other role falls through to its OWN credential account.
    run(A, "SELECT set_config('core.installation_account',%s,false)", (GH222,))
    resolved = run(A, "SELECT (core.resolve_session_identity()).o_account")
    ck(f"1c CROSS-ACCT[non-app spoofs installation_account=victim]: still resolves to A's own acct "
       f"(got {resolved}, expect {DEMO})", resolved == DEMO)
    run(A, "SELECT set_config('core.installation_account','',false)")

    # 1d. A calls the routing fn directly. enter_installation_with_authority is granted to veripsa_app ONLY, so a
    #     tenant writer cannot route ITSELF into another installation's account.
    ok, err = attempt(A, "SELECT core.enter_installation_with_authority('999')")
    ck(f"1d CROSS-ACCT[non-app calls enter_installation]: REFUSED ({'ran — HOLE' if ok else err})", not ok)

    # 1e. Even the APP itself, pre-setting current_account = a DIFFERENT account before a delegated write, lands the
    #     row in its INSTALLATION-PINNED tenant (establish_session_write_context re-derives from the pin, not the GUC).
    run(appA, "SELECT set_config('core.current_account',%s,true)", (DEMO,))   # try to redirect App(111) into the demo acct
    res = run(appA, "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
              ("PR-1E:z.py", "z.py", "o/r", "main", "evilauthor"))
    res = res if isinstance(res, dict) else json.loads(res)
    acct_of_claim = rd.scalar(GH111, "SELECT account_id FROM core.claim WHERE change_id=%s", (res.get("change_id"),))
    in_demo = rd.scalar(DEMO, "SELECT count(*) FROM core.claim WHERE change_id=%s", (res.get("change_id"),))
    ck(f"1e CROSS-ACCT[App pre-sets current_account=other before act_for]: claim lands in pinned tenant "
       f"{GH111} (got {acct_of_claim}), NOT {DEMO} (count there={in_demo})",
       acct_of_claim == GH111 and in_demo == 0)

    # ════════════════════════════════════════════════════════════════════════════════════════════════════
    # THREAT 2 — ACTOR FORGERY (record a fact AS someone else)
    # ════════════════════════════════════════════════════════════════════════════════════════════════════

    # 2a. A plain writer cannot call the delegation gate at all (act_for_claim is App-delegation-ONLY). So a buyer
    #     seat can never forge "GH-victim made this change" — it can only ever write as its OWN identity via
    #     declare_claim. (THE FIX: Postgres grants EXECUTE to PUBLIC by default; without an explicit REVOKE the
    #     GRANT-to-app left act_for callable by every role. The REVOKE in 30_gate.sql closes it; this proves it.)
    ok, err = attempt(A, "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
                      ("PR-2A:p.py", "p.py", "o/r", "main", "victim"))
    ck(f"2a ACTOR[non-app calls act_for to attribute to another GH author]: REFUSED ({'RAN — HOLE' if ok else err})",
       (not ok) and ("permission denied" in (err or "")))

    # 2b. The App's delegation DOES attribute a claim to the real PR author (GH-<login>) — BY DESIGN. The actor is
    #     a CONTENT-FREE label, and crucially the account is the App's OWN installation tenant: the forged author's
    #     agent is provisioned INSIDE that tenant and the claim is account-pinned, so a cross-tenant actor row is
    #     impossible. (This is delegation, not forgery: the App acts within exactly one tenant per event.)
    res = run(appA, "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
              ("PR-2B:q.py", "q.py", "o/r", "main", "realauthor"))
    res = res if isinstance(res, dict) else json.loads(res)
    holder = rd.scalar(GH111, "SELECT agent_id FROM core.claim WHERE change_id=%s", (res.get("change_id"),))
    author_acct = rd.scalar(GH111, "SELECT account_id FROM core.agent WHERE agent_id='GH-realauthor'")
    # the author-agent must NOT exist in any OTHER tenant (it was provisioned only inside the App's pinned tenant)
    leaked = rd.scalar(GH222, "SELECT count(*) FROM core.agent WHERE agent_id='GH-realauthor'")
    ck(f"2b ACTOR[App delegation]: claim attributed to GH-realauthor (got {holder}); the author-agent lives ONLY "
       f"in the App's tenant {author_acct}, not in victim {GH222} (leaked={leaked})",
       holder == "GH-realauthor" and author_acct == GH111 and leaked == 0)

    # 2c. record_collision_with_authority IS writer-callable BY DESIGN (the buyer's own-writer path logs a held
    #     clobber as their own seat). But its OPTIONAL p_blocked_agent override (record the collision AS a different
    #     GH-<author>) is DELEGATION-ONLY: for a non-App caller the override must be IGNORED and the recorded actor
    #     must be the connection identity — else a buyer seat could forge "GH-victim was blocked here". Set up a
    #     real held lane inside ACCT-DEMO (holder = AG-A), then a SECOND non-app writer (AG-B) tries to record the
    #     collision attributed to a forged 'GH-victim'. The event's recorded agent must be AG-B, NEVER GH-victim.
    run(A, "SELECT set_config('core.current_account','',false)")   # clear any leaked pin; the gate re-derives
    run(A, "SELECT core.declare_claim_with_authority(%s,%s,%s,%s)", ("HOLD:coll.py", "coll.py", "o/r", "wb"))  # AG-A holds the lane
    A2 = connect("veripsa_demo_agent2")                            # AG-B, a SECOND non-app writer in ACCT-DEMO
    ok, ev = attempt(A2, "SELECT core.record_collision_with_authority(%s,%s,%s,%s)", ("coll.py", "o/r", "wb", "victim"))
    forged_actor = rd.scalar(DEMO, "SELECT agent_id FROM core.event WHERE event_id=%s", (ev,)) if ev else None
    ck(f"2c ACTOR[non-app record_collision p_blocked_agent override]: override IGNORED, recorded actor is the "
       f"connection identity AG-B (got {forged_actor}), NOT the forged GH-victim",
       ok and forged_actor == "AG-B")
    # and the App's legitimate delegation path STILL works: under the App, the override IS honored (the App passes
    #     the already-formed agent_id of the real blocked author, e.g. 'GH-blockeddude' — record_collision stores
    #     p_blocked_agent verbatim as the actor, the same shape the existing delegation tests use).
    run(appA, "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", ("HOLD-APP:m.py", "m.py", "o/r", "wb", "holderdude"))
    ev2 = run(appA, "SELECT core.record_collision_with_authority(%s,%s,%s,%s)", ("m.py", "o/r", "wb", "GH-blockeddude"))
    app_actor = rd.scalar(GH111, "SELECT agent_id FROM core.event WHERE event_id=%s", (ev2,)) if ev2 else None
    ck(f"2c' ACTOR[App record_collision delegation still works]: override HONORED, actor = GH-blockeddude "
       f"(got {app_actor})", app_actor == "GH-blockeddude")

    # ════════════════════════════════════════════════════════════════════════════════════════════════════
    # THREAT 3 — TIMESTAMP FORGERY / BACKDATING (corrupt the audit timeline)
    # ════════════════════════════════════════════════════════════════════════════════════════════════════

    # 3a. The audit fact ledger timestamp (event.occurred_at) is SERVER-SET — no gate fn takes a caller timestamp
    #     for it. A recorded push lands with occurred_at = now() (within a minute), never a backdated value.
    pid = run(appA, "SELECT core.record_push_with_authority(%s,%s,%s)", ("o/r", "main", "a" * 40))
    occ_recent = rd.scalar(GH111, "SELECT (now()-occurred_at) < interval '2 min' FROM core.event WHERE event_id=%s",
                           (pid,))
    ck(f"3a BACKDATE[event.occurred_at]: server-set to now() (recent={occ_recent}); no caller timestamp param exists",
       occ_recent is True)

    # 3b. claim.claimed_at and statement.stated_at are server-set too (DEFAULT now()); the gate never accepts a
    #     caller value for them. (Re-use the 2b claim + a fresh App statement.)
    claim_recent = rd.scalar(GH111, "SELECT (now()-claimed_at) < interval '2 min' FROM core.claim WHERE change_id=%s",
                             (res.get("change_id"),))
    sid2 = run(appA, "SELECT core.record_statement_with_authority(%s,%s)", ("APP-3B-STMT", "s.py"))
    stmt_recent = rd.scalar(GH111, "SELECT (now()-stated_at) < interval '2 min' FROM core.statement WHERE statement_id=%s",
                            (sid2,))
    ck(f"3b BACKDATE[claim.claimed_at / statement.stated_at]: both server-set to now() "
       f"(claim={claim_recent}, stmt={stmt_recent})", claim_recent is True and stmt_recent is True)

    # 3c. The ONE caller-supplied time — ingest_graph_with_authority(p_captured_at) — is SNAPSHOT METADATA (the
    #     commit's as-of time the App passes), written to graph_version.captured_at. It is NOT the audit ledger.
    #     Even backdated to 1999, the UN-FORGEABLE record time (graph_version.ingested_at, the server clock) is
    #     now(). So the audit timeline cannot be backdated through it.
    g = json.dumps({"nodes": [{"id": "q.py", "kind": "file", "path": "q.py", "language": "python"}], "edges": []})
    run(appA, "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s,%s)", (g, "o/r", "main", "a" * 40, "1999-01-01T00:00:00Z"))
    ing_recent = rd.scalar(GH111, "SELECT (now()-ingested_at) < interval '2 min' "
                                  "FROM core.graph_version WHERE account_id=%s AND repo='o/r' AND branch='main'", (GH111,))
    cap_is_past = rd.scalar(GH111, "SELECT captured_at < '2001-01-01' "
                                   "FROM core.graph_version WHERE account_id=%s AND repo='o/r' AND branch='main'", (GH111,))
    ck(f"3c BACKDATE[ingest p_captured_at]: caller's captured_at is snapshot metadata (past={cap_is_past}); the "
       f"un-forgeable record time ingested_at is server now() (recent={ing_recent})",
       ing_recent is True and cap_is_past is True)

    # ════════════════════════════════════════════════════════════════════════════════════════════════════
    # THREAT 4 — IDENTITY-RESOLUTION SPOOF (trick resolve_session_identity itself)
    # ════════════════════════════════════════════════════════════════════════════════════════════════════

    # 4a. An UNPROVISIONED role (veripsa_acme_agent has LOGIN but bootstrap_local never gives it a credential) is
    #     refused at resolution — no credential = no identity (42501), so it can write nothing as anyone.
    acme = connect("veripsa_acme_agent")
    ok, err = attempt(acme, "SELECT core.record_statement_with_authority(%s,%s)", ("ACME-FORGE", "y.py"))
    ck(f"4a SPOOF[unprovisioned role writes]: REFUSED at resolve_session_identity ({'ran — HOLE' if ok else err})",
       (not ok) and ("credential" in (err or "")))

    # 4b. The App with NO installation pinned and NO veripsa_app credential is refused too (a clean deploy pins an
    #     installation per event; an UN-entered App connection must not silently resolve to some default tenant).
    #     bootstrap_local DOES provision veripsa_app→AG-APP/ACCT-DEMO (the local/dogfood credential), so an
    #     un-entered App resolves to its OWN provisioned account — never an arbitrary one. Assert that exact account.
    app_unentered = connect("veripsa_app")   # NOT entered into any installation
    unentered_acct = run(app_unentered, "SELECT (core.resolve_session_identity()).o_account")
    ck(f"4b SPOOF[un-entered App]: resolves to its OWN provisioned account only (got {unentered_acct}, expect {DEMO}); "
       f"no installation pin → no arbitrary tenant", unentered_acct == DEMO)

    # 4c. A tenant forging a session GUC for the actor cannot change the recorded actor: identity comes from the
    #     credential, not a GUC. Set a bogus 'core.current_actor'-style GUC and confirm the statement is still AG-A.
    run(A, "SELECT set_config('core.current_account','',false)")
    run(A, "SELECT set_config('core.governed_write_token','',true)")
    run(A, "SELECT set_config('core.current_agent','GH-impostor',false)")  # a made-up GUC the gate never reads
    sid3 = run(A, "SELECT core.record_statement_with_authority(%s,%s)", ("ACTOR-GUC-PROBE", "g.py"))
    actor = rd.scalar(DEMO, "SELECT agent_id FROM core.statement WHERE statement_id=%s", (sid3,))
    ck(f"4c SPOOF[forged actor GUC]: recorded actor comes from the credential (got {actor}, expect AG-A), not the GUC",
       actor == "AG-A")

    # ───────────────────────────────────────────────────────────────────────────────────────────────────
    print("\n=== TAMPER IDENTITY — adversarial forge attempts (each must be REFUSED / corrected) ===")
    passed = 0
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        passed += 1 if ok else 0
    print(f"\n{passed}/{len(checks)} forge avenues refused.")
    if passed == len(checks):
        print("TAMPER IDENTITY GATE: PASS")
        return 0
    print("TAMPER IDENTITY GATE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
