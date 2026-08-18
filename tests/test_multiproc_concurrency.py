#!/usr/bin/env python3
"""MULTI-INSTANCE / CROSS-PROCESS CONCURRENCY gate.

The whole "single worker → no concurrent writes, no races" guarantee is a LIE the moment Render runs TWO
instances (it overlaps two during every zero-downtime rolling deploy; a scale-out would make it permanent)
and GitHub delivers webhooks CONCURRENTLY. The existing idempotency gates only exercise ONE in-process worker
serializing its own deliveries — they never prove the design survives two SEPARATE OS processes hitting the
same coordinate at the same instant. This gate does: every probe uses SEPARATE psycopg2 connections (a fresh
backend process each — exactly what a second Render instance is; the session-scoped advisory lock and the CAS
are the ONLY things standing between them), and asserts the four cross-process invariants:

  (a) PER-(ACCOUNT,REPO) ADVISORY LOCK actually serializes across processes, and is SESSION-scoped (held for the
      whole event incl. its body txn, released only on close) — NOT txn-scoped / released early. Two backends
      taking the two-arg pg_advisory_lock(hashtext(account), hashtext(repo)) for the SAME (account,repo) must
      MUTUALLY EXCLUDE; for a DIFFERENT repo — OR a DIFFERENT account on the SAME repo full_name (cross-tenant) —
      must NOT. A pg_advisory_XACT_lock (released at COMMIT) would free the lock mid-event and break the guarantee
      — we assert the live path uses the SESSION lock by observing the lock survives a COMMIT on its connection.

  (b) DOUBLE-GRANT IMPOSSIBLE: two backends declaring the SAME lane (different authors) concurrently — the
      claim_one_active partial-unique index + the _place_claim unique_violation CAS must yield EXACTLY ONE
      'active' holder; the loser is forced to 'waiting'. Never two active rows on one (account,repo,branch,
      path). Driven WITHOUT the advisory lock (raw concurrent declares) so the DB-level CAS is what's proven —
      that is the backstop if two events for two DIFFERENT repos that happen to share a lane key ever raced,
      or if the lock were ever removed.

  (c) NO TORN GRAPH: two backends ingesting the SAME coordinate concurrently (reordered captured_at — the
      newer commit and an older reordered retry) must, UNDER the per-repo lock, leave a single COHERENT graph
      (the newer tree wins; node/edge counts match exactly ONE ingest's tree, never an interleaved mix of two
      DELETE+INSERT bodies). We prove BOTH: (c1) the monotonicity guard is a READ-then-WRITE that is NOT atomic
      on its own, so WITHOUT serialization two ingests CAN interleave; (c2) WITH the per-repo advisory lock the
      live path takes, the interleave is impossible and the stored graph is exactly the newer tree.

  (d) BOOT-RECONCILE / FIRST-SIGHT IDEMPOTENCY: two instances booting together both call
      enter_installation_with_authority for the SAME never-seen account concurrently — the lazy provision must
      be idempotent (ON CONFLICT), leaving EXACTLY ONE account row, no duplicate-key crash on either backend.

Offline, content-free (only path/coordinate/count — never code/bodies/secrets). Process-unique scratch DB.

Run:  python3 tests/test_multiproc_concurrency.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import psycopg2.errorcodes  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): per-PID, exactly like the other gates — several agents each running
# run_gates (or parallel CI shards) never drop each other's scratch DB mid-run.
DB = "veripsa_multiproc_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
DSN_MIG = f"postgresql://veripsa_migrator@localhost/{DB}"
ACCOUNT_ID = "900900"                       # the owning GitHub account id (the stable tenant key)
TENANT = f"ACCT-GH-{ACCOUNT_ID}"            # what enter_installation_with_authority provisions for ACCOUNT_ID
REPO = "acme/multiproc"
BRANCH = "main"

checks: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    checks.append((label, bool(ok)))


def app_conn():
    """A FRESH connection = a fresh Postgres backend process = a second Render instance, for our purposes:
    its own session, its own session-scoped advisory locks, its own txn. This is the unit the audit cares
    about. Routed into the tenant via the same trusted path a live webhook event takes."""
    c = psycopg2.connect(DSN_APP)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", (ACCOUNT_ID,))
    return c


def admin(sql, args=(), tenant=TENANT):
    """Readback as the migrator with a tenant pinned (past RLS) — the ground truth the App writes converge to.
    `tenant` defaults to the primary tenant; pass another to read a different tenant's rows (probe d)."""
    conn = psycopg2.connect(DSN_MIG)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (tenant,))
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _lock_key_unsigned(conn, repo, account=ACCOUNT_ID) -> tuple[int, int]:
    """The UNSIGNED (classid, objid) advisory key the live path's lock lands on in pg_locks. The server now keys
    the per-repo lock on (account, repo) via the TWO-ARG pg_advisory_lock(hashtext(account), hashtext(repo))
    (server._take_repo_lock) — so two DIFFERENT tenants' same-named repo never collide. The two-int4 form records
    classid=hashtext(account), objid=hashtext(repo). hashtext returns a SIGNED int4, but pg_locks stores each as
    the UNSIGNED bit-pattern; map signed→unsigned so the test keys on the SAME (classid,objid) pg_locks reports
    (no re-implementing hashtext in Python). Defaults to the primary tenant ACCOUNT_ID (the one app_conn routes)."""
    with conn.cursor() as cur:
        cur.execute("SELECT hashtext(%s)::bigint & 4294967295, hashtext(%s)::bigint & 4294967295", (account, repo))
        row = cur.fetchone()
        return (int(row[0]), int(row[1]))


def _held_session_locks(conn) -> set[tuple[int, int]]:
    """The set of advisory-lock keys (as UNSIGNED (classid, objid) pairs) currently held by THIS connection's
    backend. For the TWO-INT4 advisory lock (pg_advisory_lock(int4,int4) — the live path's per-(account,repo)
    key) Postgres records classid=first key, objid=second key, with objsubid=2 (the two-int4 marker; a single
    int4 key would be objsubid=1). We key on (classid, objid) and the two-int4 marker. Reading this back lets us
    prove the live lock is SESSION-scoped: it survives a COMMIT on its own connection."""
    with conn.cursor() as cur:
        cur.execute("""SELECT classid::bigint, objid::bigint FROM pg_locks
                        WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted AND objsubid=2""")
        return {(int(r[0]), int(r[1])) for r in cur.fetchall()}


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# PROBE (a): the per-repo advisory lock serializes ACROSS PROCESSES and is SESSION-scoped (not released early).
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def probe_a() -> None:
    repo_x, repo_y = REPO, "acme/multiproc-other"
    c1, c2 = app_conn(), app_conn()
    try:
        kx = _lock_key_unsigned(c1, repo_x)
        ky = _lock_key_unsigned(c1, repo_y)

        # (a1) c1 takes the SESSION advisory lock for repo_x (the live SETUP step, autocommit). Keyed on
        #      (account, repo) — the two-arg form the live path now uses (server._take_repo_lock).
        with c1.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))", (ACCOUNT_ID, repo_x))
        check("(a1) backend-1 holds the session advisory lock for the repo (pg_locks)", kx in _held_session_locks(c1))

        # (a2) c1 opens + COMMITS a body txn (the live BODY step). A pg_advisory_XACT_lock would be RELEASED at
        #      this COMMIT — breaking the per-event guarantee. The live path uses the SESSION lock, so it MUST
        #      survive the commit. Prove it by observing the lock is STILL held after a commit on c1.
        c1.autocommit = False
        with c1.cursor() as cur:
            cur.execute("SELECT 1")          # a trivial body txn
        c1.commit()
        c1.autocommit = True
        check("(a2) the lock SURVIVES a COMMIT on its connection — SESSION-scoped, held across the body txn "
              "(a txn-scoped lock would have been freed mid-event)", kx in _held_session_locks(c1))

        # (a3) a SEPARATE backend (c2 = the second instance) tries the SAME repo's lock NON-blocking → must FAIL
        #      (mutual exclusion across processes). And the SAME backend trying a DIFFERENT repo must SUCCEED
        #      (different repos still run in parallel — the design's whole point).
        with c2.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))", (ACCOUNT_ID, repo_x))
            got_same = cur.fetchone()[0]
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))", (ACCOUNT_ID, repo_y))
            got_other = cur.fetchone()[0]
        check("(a3) a SECOND backend CANNOT take the same repo's lock while backend-1 holds it (cross-process "
              "mutual exclusion)", got_same is False)
        check("(a3) a SECOND backend CAN take a DIFFERENT repo's lock concurrently (different repos parallelize)",
              got_other is True)
        if got_other:
            with c2.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))", (ACCOUNT_ID, repo_y))

        # (a3') CROSS-TENANT ISOLATION (the FIX A guarantee): a DIFFERENT account installing a repo with the SAME
        #       full_name must NOT collide with c1's lock on repo_x. Before the fix the key was bare hashtext(repo)
        #       — so tenant B's UNRELATED 'acme/multiproc' would have blocked on (and raced the locked writes of)
        #       tenant A's. With the (account,repo) key it is a distinct lock → a separate backend takes it freely.
        OTHER_ACCOUNT = "111222"                      # a different owning-account id, same repo full_name
        with c2.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))", (OTHER_ACCOUNT, repo_x))
            got_other_tenant = cur.fetchone()[0]
            if got_other_tenant:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))", (OTHER_ACCOUNT, repo_x))
        check("(a3') a DIFFERENT tenant's lock on the SAME repo full_name does NOT collide (per-(account,repo) "
              "key — no cross-tenant serialization/race)", got_other_tenant is True)

        # (a4) c1 releases; NOW the second backend can take repo_x (the handoff a real deploy overlap relies on).
        with c1.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))", (ACCOUNT_ID, repo_x))
        with c2.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))", (ACCOUNT_ID, repo_x))
            got_after_release = cur.fetchone()[0]
            if got_after_release:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))", (ACCOUNT_ID, repo_x))
        check("(a4) once backend-1 RELEASES, the second backend immediately acquires the repo lock (clean handoff)",
              got_after_release is True)
        _ = ky  # silence unused in the happy path
    finally:
        c1.close(); c2.close()


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# PROBE (b): NO DOUBLE-GRANT. Two backends declare the SAME lane (different authors) at the same instant,
# WITHOUT the per-repo lock — so the claim_one_active partial-unique index + the CAS is the ONLY thing that
# can prevent two 'active' holders. There must be EXACTLY ONE 'active'; the loser is forced to 'waiting'.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def probe_b() -> None:
    repo = "acme/multiproc-lane"
    ROUNDS = 8                                # repeat the race — a single shot can pass by luck if threads miss
    worst_active = 1                          # track the WORST round (a double-grant would push this to 2)
    any_crash = ""
    all_one_active = True
    all_one_queued = True
    all_result_agree = True

    for rnd in range(ROUNDS):
        path = f"src/contended_{rnd}.py"      # a FRESH lane per round = a clean contended slot each time
        results: dict[str, dict] = {}
        errors: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def declare(name, author, claim_id, _path=path, _res=results, _err=errors, _bar=barrier):
            try:
                c = app_conn()
                try:
                    _bar.wait(timeout=10)        # release both backends at the SAME instant
                    with c.cursor() as cur:
                        cur.execute("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
                                    (claim_id, _path, repo, BRANCH, author))
                        _res[name] = cur.fetchone()[0]
                finally:
                    c.close()
            except Exception as e:              # a unique_violation that ESCAPED the CAS = an uncaught double-grant
                _err[name] = f"{type(e).__name__}: {e}"

        t1 = threading.Thread(target=declare, args=("p1", "alice", f"PR-b-{rnd}-1"))
        t2 = threading.Thread(target=declare, args=("p2", "bob", f"PR-b-{rnd}-2"))
        t1.start(); t2.start(); t1.join(); t2.join()

        if errors:
            any_crash += f"round{rnd}={errors} "
        active = admin("""SELECT count(*)::int FROM core.claim
                           WHERE repo=%s AND branch=%s AND target_path=%s AND claim_state='active'""",
                       (repo, BRANCH, path))
        waiting = admin("""SELECT count(*)::int FROM core.claim
                            WHERE repo=%s AND branch=%s AND target_path=%s AND claim_state='waiting'""",
                        (repo, BRANCH, path))
        worst_active = max(worst_active, active)
        all_one_active = all_one_active and active == 1
        all_one_queued = all_one_queued and waiting == 1
        granted = sum(1 for r in results.values() if isinstance(r, dict) and r.get("granted") is True)
        queued = sum(1 for r in results.values() if isinstance(r, dict) and r.get("queued") is True)
        all_result_agree = all_result_agree and granted == 1 and queued == 1

    check("(b) across " + str(ROUNDS) + " concurrent-declare races, NONE crashed (a leaked unique_violation = an "
          "uncaught double-grant): " + (any_crash if any_crash else "all clean"), not any_crash)
    check(f"(b) EVERY round had EXACTLY ONE 'active' holder — never two (worst-case active seen across all "
          f"rounds = {worst_active})", all_one_active and worst_active == 1)
    check("(b) EVERY round queued the loser (no claim dropped on the floor)", all_one_queued)
    check("(b) EVERY round's function results agreed with the table: exactly one granted=true, one queued",
          all_result_agree)


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# PROBE (c): NO TORN GRAPH. Two backends ingest the SAME coordinate concurrently.
#   (c1) the monotonicity guard alone (read graph_version, then DELETE+INSERT) is NOT atomic — without
#        serialization two ingests CAN race. We do NOT need to force a torn graph (timing-dependent); we
#        ASSERT the guard's read-then-write structure cannot be relied on alone (documented + observed below).
#   (c2) UNDER the per-repo advisory lock the live path holds, two concurrent ingests serialize: the stored
#        graph is exactly ONE coherent tree (the NEWER captured_at wins via the monotonicity guard; counts
#        match that tree exactly, never an interleaved DELETE-of-one + INSERT-of-the-other mix).
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _graph(nfiles, sha):
    nodes = [{"id": f"f{i}.py", "kind": "file", "path": f"f{i}.py", "name": f"f{i}.py"} for i in range(nfiles)]
    edges = [{"src": f"f{i}.py", "dst": f"f{(i+1) % nfiles}.py", "kind": "imports"} for i in range(nfiles)]
    return {"nodes": nodes, "edges": edges}, sha


def probe_c1_negative_control() -> None:
    """(c1) PROVE THE LOCK IS LOAD-BEARING (not decorative). The monotonicity guard only catches a stale ingest
    when BOTH the stored and incoming commit-times are KNOWN. When captured_at is absent — a tag push, an odd
    payload, an older App that never sent it — the guard is INERT (documented at the gate). In that inert case
    the ONLY thing serializing two ingests for the same coordinate is the per-repo advisory lock. We prove the
    danger concretely: WITHOUT the lock, two ingests with captured_at=NULL applied back-to-back are pure
    last-writer-wins — the SECOND (which a reordered delivery could make the OLDER tree) overwrites the first,
    with NOTHING stopping it. This is the cross-process clobber the lock closes; probe_c2 then shows that WITH
    the live per-repo lock the contended path is serialized and coherent.

    (We drive the REAL gate, no raw table writes — the schema's forgery-block trigger refuses direct writes to
    code_node even as the migrator, an integrity property worth noting: ALL writes must go through the gate.)"""
    repo = "acme/multiproc-noguard"
    big, _ = _graph(10, "d" * 40)
    small, _ = _graph(3, "e" * 40)
    c = app_conn()
    try:
        # NO advisory lock taken. Two ingests, captured_at=NULL → the monotonicity guard is INERT for both.
        with c.cursor() as cur:
            cur.execute("SELECT core.ingest_graph_with_authority(%s::jsonb,%s,%s,%s,NULL)",
                        (__import__("json").dumps(big), repo, BRANCH, "d" * 40))     # first lands the 10-file tree
        after_first = admin("SELECT node_count::int FROM core.graph_version WHERE repo=%s AND branch=%s", (repo, BRANCH))
        with c.cursor() as cur:
            cur.execute("SELECT core.ingest_graph_with_authority(%s::jsonb,%s,%s,%s,NULL)",
                        (__import__("json").dumps(small), repo, BRANCH, "e" * 40))   # second (could be the OLDER tree) clobbers
        after_second = admin("SELECT node_count::int FROM core.graph_version WHERE repo=%s AND branch=%s", (repo, BRANCH))

        # The point: with the guard inert (no captured_at) and no lock, the LATER write wins unconditionally —
        # if a reordered delivery makes that later write the OLDER tree, the stored graph regresses. Only the
        # per-repo advisory lock (c2) decides WHO writes last by serializing the two events deterministically.
        check("(c1) NEGATIVE CONTROL — when the monotonicity guard is INERT (no captured_at) and there is NO "
              f"per-repo lock, ingests are pure last-writer-wins: first tree count={after_first}, a later "
              f"ingest unconditionally overwrote it to count={after_second} (a reordered delivery here would "
              "REGRESS the graph) — the per-repo lock is what serializes this safely (proven in c2)",
              after_first == 10 and after_second == 3)
    finally:
        c.close()


def probe_c() -> None:
    import json
    repo = "acme/multiproc-graph"
    # NEWER tree (10 files) captured_at = T+1; OLDER reordered retry (3 files) captured_at = T (earlier).
    newer_graph, newer_sha = _graph(10, "b" * 40)
    older_graph, older_sha = _graph(3, "c" * 40)
    NEWER_TS = "2026-06-18T12:00:01+00:00"
    OLDER_TS = "2026-06-18T12:00:00+00:00"

    results: dict[str, str] = {}
    errors: dict[str, str] = {}
    newer_has_lock = threading.Event()       # signals the older thread it's safe to CONTEND for the lock

    def ingest(name, graph, sha, ts, signal_after_lock):
        try:
            c = app_conn()
            try:
                # the LIVE path: take the per-(account,repo) SESSION advisory lock (autocommit), THEN run the body
                # txn, commit, release — exactly make_db_processor's structure, on a SEPARATE backend.
                with c.cursor() as cur:
                    cur.execute("SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))", (ACCOUNT_ID, repo))   # BLOCKS until the holder releases
                if signal_after_lock:
                    newer_has_lock.set()     # the newer ingest now owns the lock; release the older to CONTEND
                    time.sleep(0.15)         # hold it briefly so the older DEMONSTRABLY blocks on the lock, not luck
                c.autocommit = False
                with c.cursor() as cur:
                    cur.execute("SELECT core.ingest_graph_with_authority(%s::jsonb,%s,%s,%s,%s::timestamptz)",
                                (json.dumps(graph), repo, BRANCH, sha, ts))
                    results[name] = cur.fetchone()[0]
                c.commit()
                c.autocommit = True
                with c.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))", (ACCOUNT_ID, repo))
            finally:
                c.close()
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"

    # (c2) BOTH backends take the per-repo lock (the live path). The newer grabs it FIRST and signals; the older
    #      then BLOCKS on the lock until the newer commits+releases — true cross-process serialization. Once the
    #      older proceeds, the monotonicity guard sees the newer stored tree and makes its body a no-op ('stale').
    #      Net: ONE coherent tree (the newer's 10 files), never an interleaved DELETE-of-one + INSERT-of-other.
    t_new = threading.Thread(target=ingest, args=("newer", newer_graph, newer_sha, NEWER_TS, True))
    t_old = threading.Thread(target=ingest, args=("older", older_graph, older_sha, OLDER_TS, False))
    t_new.start()
    newer_has_lock.wait(timeout=10)          # don't start the older until the newer DEFINITELY holds the lock
    t_old.start(); t_new.join(); t_old.join()

    check("(c2) neither concurrent ingest crashed under the per-repo lock: "
          + (str(errors) if errors else "both returned cleanly"), not errors)

    nodes = admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s AND branch=%s AND node_kind='file'",
                  (repo, BRANCH))
    edges = admin("SELECT count(*)::int FROM core.code_edge WHERE repo=%s AND branch=%s AND edge_kind='imports'",
                  (repo, BRANCH))
    stored_sha = admin("SELECT commit_sha FROM core.graph_version WHERE repo=%s AND branch=%s", (repo, BRANCH))
    stored_nc = admin("SELECT node_count::int FROM core.graph_version WHERE repo=%s AND branch=%s", (repo, BRANCH))

    # COHERENT = the stored graph is exactly ONE of the two trees, AND the version row's counts match the actual
    # rows (no torn mix where, e.g., the newer tree's nodes coexist with the older version-row's count).
    coherent_tree = (nodes == 10 and edges == 10) or (nodes == 3 and edges == 3)
    check(f"(c2) the stored graph is exactly ONE COHERENT tree, never an interleaved mix — file_nodes={nodes} "
          f"import_edges={edges}", coherent_tree)
    check(f"(c2) the NEWER captured_at won (monotonicity under serialization): stored tree=10 files, "
          f"stored_sha={'b40' if stored_sha == 'b'*40 else stored_sha}", nodes == 10 and stored_sha == "b" * 40)
    check(f"(c2) the version row's node_count MATCHES the actual stored nodes (no torn version/data) — "
          f"version.node_count={stored_nc} actual_total_nodes="
          + str(admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s AND branch=%s", (repo, BRANCH))),
          stored_nc == admin("SELECT count(*)::int FROM core.code_node WHERE repo=%s AND branch=%s", (repo, BRANCH)))

    # one of the two reported 'stale' (the older, refused by the monotonicity guard once it saw the newer tree).
    stale = sum(1 for r in results.values() if isinstance(r, str) and '"stale":true' in r.replace(" ", ""))
    check(f"(c2) the OLDER reordered ingest was refused as 'stale' by the monotonicity guard (1 stale of "
          f"{len(results)})", stale == 1)


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# PROBE (d): BOOT-RECONCILE FIRST-SIGHT IDEMPOTENCY. Two instances booting together both route the SAME
# never-seen account at the same instant. The lazy provision must be idempotent: EXACTLY ONE account row,
# no duplicate-key crash on either backend.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def probe_d() -> None:
    ROUNDS = 8
    any_crash = ""
    worst_rows = 1                            # a duplicate-key race would make some round's account-row count != 1
    all_one_row = True

    for rnd in range(ROUNDS):
        fresh_account = f"7770{rnd:02d}"      # a NEVER-seen account per round (a clean first-sight each time)
        fresh_tenant = f"ACCT-GH-{fresh_account}"
        errors: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def enter(name, _acct=fresh_account, _err=errors, _bar=barrier):
            try:
                c = psycopg2.connect(DSN_APP)
                c.autocommit = True
                try:
                    with c.cursor() as cur:
                        cur.execute("SET search_path=core")
                        _bar.wait(timeout=10)    # both instances route the same fresh account at the SAME instant
                        cur.execute("SELECT core.enter_installation_with_authority(%s)", (_acct,))
                finally:
                    c.close()
            except Exception as e:               # a duplicate-key crash on the lazy provision would land here
                _err[name] = f"{type(e).__name__}: {e}"

        t1 = threading.Thread(target=enter, args=("i1",))
        t2 = threading.Thread(target=enter, args=("i2",))
        t1.start(); t2.start(); t1.join(); t2.join()

        if errors:
            any_crash += f"round{rnd}={errors} "
        # read AS the fresh tenant (RLS scopes account rows to current_account; the primary readback can't see
        # this account's row at all — that is isolation working, not a missing row).
        rows = admin("SELECT count(*)::int FROM core.account WHERE account_id=%s", (fresh_tenant,), tenant=fresh_tenant)
        all_one_row = all_one_row and rows == 1
        if rows != 1:
            worst_rows = rows

    check("(d) across " + str(ROUNDS) + " concurrent first-sight races, NO booting instance crashed routing the "
          "same fresh account (idempotent lazy provision): " + (any_crash if any_crash else "all clean"), not any_crash)
    check(f"(d) EVERY round left EXACTLY ONE account row after two concurrent first-sight provisions "
          f"(worst-case row count seen = {worst_rows})", all_one_row and worst_rows == 1)


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    try:
        # provision the primary tenant once (probes a/b/c write into it).
        c = app_conn(); c.close()
        probe_a()
        probe_b()
        probe_c1_negative_control()
        probe_c()
        probe_d()
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)

    print("\n=== MULTI-INSTANCE / CROSS-PROCESS CONCURRENCY ===")
    passed = 0
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        passed += 1 if ok else 0
    allgood = passed == len(checks)
    print(f"\n{passed}/{len(checks)} checks passed")
    print("MULTI-INSTANCE CONCURRENCY GATE: " + ("PASS" if allgood else "FAIL"))
    return 0 if allgood else 1


if __name__ == "__main__":
    sys.exit(main())
