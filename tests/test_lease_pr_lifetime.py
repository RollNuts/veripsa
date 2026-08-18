#!/usr/bin/env python3
"""LEASE×PR-LIFETIME GATE: CLAIM LEASE vs PR LIFETIME — the false-clear / lost-serialize this locks shut.

A claim carries a LEASE (lease_expires_at). expire_stale_claims() — called at the top of
main_impact_surface / contention_surface and on every PR event — flips an 'active' claim to
'expired' once its lease lapses and AUTO-PROMOTES the oldest waiter onto that lane. The lease
is renewed ONLY by a PR event for that change (synchronize/opened/reopened → _place_claim
self-heartbeat) or an App restart (boot_reconcile). NOTHING renews the lease of a STILL-OPEN
but QUIET PR (no push for the lease window, no restart).

A real GitHub PR can stay OPEN for DAYS. So a quiet, still-in-flight holder's lease lapses while
its PR is genuinely live. This probe injects that and asserts the interaction is sound:

  (a) LOST-SERIALIZE: holder PR-A quiet > lease, waiter PR-B behind it. Does the lane get
      FALSELY released + PR-B promoted, so the (still real, still in-flight) collision SILENTLY
      stops being held? Assert: it must NOT silently drop — recall-safe (re-held or still surfaced).
  (b) RE-HELD ON RESYNC: after the lapse+promote, PR-A pushes again (synchronize). Correct
      re-queue (PR-A goes BEHIND the promoted PR-B), NOT a self-collision, NOT a crash.
  (c) RECONCILED CLOSE: after a lapse, the eventual real merge/close of PR-A still reconciles
      (no orphan active row, no double-release error).
  (d) symmetry of (b): the originally-promoted waiter keeps its lane; the lapsed holder is the
      one that re-queues.

Run:  python3 tests/test_lease_pr_lifetime.py
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402

DB = "veripsa_leaseprobe_" + str(os.getpid())   # PID-unique, parallel-safe
REPO = "acme/lease"


def app(sql, args=()):
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def mig(sql, args=(), fetch=True):
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account','ACCT-DEMO',true)")
            cur.execute("SELECT set_config('core.governed_write_token','claim',true)")
            cur.execute(sql, args)
            if fetch:
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()


def ingest(graph):
    app("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
        (json.dumps(graph), REPO, "main", "a" * 40))


def claim(cid, path, author, ranges=None):
    rj = json.dumps(ranges) if ranges else None
    return app("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s)",
               (cid, path, REPO, "main", author, rj, None))


def surface():
    imp = app("SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
    return json.loads(imp) if isinstance(imp, str) else imp


def verdicts():
    s = surface()
    return {c["change_id"]: c for c in s.get("changes", [])}


def claim_states():
    """The live claim rows (change_id, state, lease vs now) — migrator-direct read."""
    rows = mig("""SELECT coalesce(jsonb_agg(jsonb_build_object(
                    'change', change_id, 'state', claim_state,
                    'lapsed', lease_expires_at < now()) ORDER BY change_id, claim_state),'[]'::jsonb)
                  FROM core.claim WHERE account_id='ACCT-DEMO' AND repo=%s AND target_path='render.py'""",
               (REPO,))
    return rows


def expire_lease(change_id):
    """Inject the lease LAPSE of a still-open holder: push lease_expires_at into the past. This is
    EXACTLY what time does to a quiet open PR after lease_minutes — no push, no restart."""
    mig("UPDATE core.claim SET lease_expires_at = now() - interval '1 hour' "
        "WHERE account_id='ACCT-DEMO' AND repo=%s AND change_id=%s AND claim_state='active'",
        (REPO, change_id), fetch=False)


def reset():
    mig("UPDATE core.claim SET claim_state='released', released_at=now() WHERE claim_state IN ('active','waiting')",
        fetch=False)


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    checks = []

    # A real graph: render.py with ONE function alpha (lines 1-3). Both PRs edit alpha → same-symbol → serialize.
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "render.py"), "w") as fh:
            fh.write("def alpha(x):\n    y = x + 1\n    return y\n")
        graph = X.build_graph(d)
    ingest(graph)

    # ── BASELINE: PR-A holds render.py, PR-B serializes behind it (real, in-flight, same file). ──────────────
    claim("PR-A:render.py", "render.py", "alice")
    claim("PR-B:render.py", "render.py", "bob")
    v = verdicts()
    base_ok = (v.get("PR-A", {}).get("verdict") in ("clear", "warn", "unknown")  # A is the holder (not waiting)
               and v.get("PR-B", {}).get("verdict") == "serialize"
               and "PR-A" in json.dumps(v.get("PR-B", {}).get("serialize_behind", [])))
    checks.append((f"BASELINE: PR-B serializes behind holder PR-A on render.py "
                   f"(A='{v.get('PR-A',{}).get('verdict')}', B='{v.get('PR-B',{}).get('verdict')}')", base_ok))

    # ══ PROBE (a) — LOST-SERIALIZE: PR-A is a STILL-OPEN but QUIET PR; its lease lapses. Sweep + promote. ════
    expire_lease("PR-A")                        # time passes on a quiet open PR-A; nothing renewed its lease
    v = verdicts()                              # main_impact_surface sweeps stale claims at the top
    st = claim_states()
    a_state = next((x["state"] for x in st if x["change"] == "PR-A"), None)
    b_state = next((x["state"] for x in st if x["change"] == "PR-B"), None)
    b_verdict = v.get("PR-B", {}).get("verdict")
    a_present = "PR-A" in v
    # THE HOLE if it exists: PR-A (still open!) swept to 'expired', PR-B promoted to active, B now 'clear' —
    # a still-real, still-in-flight collision SILENTLY stops being held. Recall-safe REQUIRES one of:
    #   B still 'serialize'/'unknown' (still surfaced)  OR  A re-held active (re-held)  — NOT B 'clear' & A gone.
    silent_drop = (b_verdict == "clear" and a_state == "expired")
    checks.append((f"(a) LOST-SERIALIZE after PR-A's lease lapses on a STILL-OPEN PR: the collision must NOT "
                   f"silently drop to 'clear' (A.state='{a_state}', B.verdict='{b_verdict}', A in surface={a_present})",
                   not silent_drop))

    # ══ PROBE (b) — RE-HELD ON RESYNC: PR-A pushes again (synchronize). It must re-queue correctly, no self-collide. ══
    res_a = claim("PR-A:render.py", "render.py", "alice")   # synchronize on PR-A → _place_claim self-heartbeat path
    res_a = json.loads(res_a) if isinstance(res_a, str) else res_a
    st = claim_states()
    a_state2 = next((x["state"] for x in st if x["change"] == "PR-A"), None)
    b_state2 = next((x["state"] for x in st if x["change"] == "PR-B"), None)
    # after re-declare: exactly one active holder on the lane, PR-A is NOT colliding with itself (no two A rows
    # active), and the lane is consistently held by exactly one of {A,B} with the other waiting.
    n_active = sum(1 for x in st if x["state"] == "active")
    no_self_collision = (res_a is not None and res_a.get("ok") is True
                         and n_active == 1
                         and {a_state2, b_state2} == {"active", "waiting"})
    checks.append((f"(b) RE-HELD ON RESYNC: PR-A pushes again → exactly one active holder, the other waits, "
                   f"no self-collision/crash (A='{a_state2}', B='{b_state2}', active_count={n_active})",
                   no_self_collision))

    # ══ PROBE (c) — RECONCILED CLOSE: the eventual real close/merge of PR-A reconciles cleanly. ════════════════
    # release_change_on_main(change_id, repo, branch) releases that change's lanes + promotes the next waiter.
    # After (b) PR-A is the WAITER and PR-B holds; closing PR-A must cleanly drop A's queued claim, no orphan/error.
    closed = app("SELECT core.release_change_on_main_with_authority(%s,%s,%s)", ("PR-A", REPO, "main"))
    closed = json.loads(closed) if isinstance(closed, str) else closed
    st = claim_states()
    a_active_after = any(x["change"] == "PR-A" and x["state"] == "active" for x in st)
    b_active_after = any(x["change"] == "PR-B" and x["state"] == "active" for x in st)
    reconciled = (closed is not None and closed.get("ok") is True and not a_active_after)
    checks.append((f"(c) RECONCILED CLOSE: closing PR-A frees its lane (no orphan active A; the lane reconciles) "
                   f"(close_ok={closed.get('ok') if closed else None}, A_active_after={a_active_after}, "
                   f"B_active_after={b_active_after})", reconciled))

    # ══ PROBE (d) — symmetry: whoever is promoted on the lapse keeps the lane; the lapsed one re-queues behind. ══
    reset()
    claim("PR-C:render.py", "render.py", "carol")   # holder
    claim("PR-D:render.py", "render.py", "dave")    # waiter
    expire_lease("PR-C")
    verdicts()                                       # sweep → promote PR-D
    st = claim_states()
    d_state = next((x["state"] for x in st if x["change"] == "PR-D"), None)
    promoted_d = (d_state == "active")
    # now lapsed holder PR-C resyncs → must go BEHIND the promoted PR-D (re-queue), not steal the lane back.
    claim("PR-C:render.py", "render.py", "carol")
    st = claim_states()
    c_state = next((x["state"] for x in st if x["change"] == "PR-C"), None)
    d_state2 = next((x["state"] for x in st if x["change"] == "PR-D"), None)
    n_active2 = sum(1 for x in st if x["state"] == "active")
    symmetry_ok = (promoted_d and d_state2 == "active" and c_state == "waiting" and n_active2 == 1)
    checks.append((f"(d) SYMMETRY: lapsed holder PR-C re-queues BEHIND the promoted PR-D (D keeps the lane) "
                   f"(D='{d_state2}', C='{c_state}', active_count={n_active2})", symmetry_ok))

    # ══ PROBE (e) — SELF-HEALING + NO STRAND: a lapsed holder that is re-queued and NEVER resyncs must not strand
    #    its lane forever. When the promoted waiter LANDS, the re-queued (oldest) waiter is promoted back; the lane
    #    keeps moving. (And a genuinely-dead holder then re-lapses and is swept again — convergence, no infinite hold.)
    reset()
    claim("PR-E:render.py", "render.py", "erin")    # holder, will lapse and NEVER resync (truly abandoned)
    claim("PR-F:render.py", "render.py", "fred")    # waiter
    expire_lease("PR-E")
    verdicts()                                       # sweep → PR-F promoted active, PR-E re-queued waiting (lapsed)
    st = claim_states()
    e1 = next((x["state"] for x in st if x["change"] == "PR-E"), None)
    f1 = next((x["state"] for x in st if x["change"] == "PR-F"), None)
    requeued = (e1 == "waiting" and f1 == "active")
    # PR-F lands → its lane frees → the next-in-line (PR-E, the oldest waiter) is promoted: the lane is NOT stranded.
    app("SELECT core.release_change_on_main_with_authority(%s,%s,%s)", ("PR-F", REPO, "main"))
    st = claim_states()
    e2 = next((x["state"] for x in st if x["change"] == "PR-E"), None)
    no_strand = (requeued and e2 == "active")        # the re-queued holder got the lane back when it cleared
    checks.append((f"(e) SELF-HEALING / NO STRAND: a re-queued lapsed holder is promoted back when the lane frees "
                   f"(never stranded); the lane keeps moving (E after re-queue='{e1}', after F lands='{e2}')", no_strand))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("LEASE×PR-LIFETIME GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
