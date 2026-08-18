#!/usr/bin/env python3
"""SELF-HEAL × FRESHNESS gate — after the boot/PR-time self-heal RE-INGESTS main's graph, the FRESHNESS
coordinate the operator watches (the /freshz + /readyz body, and the stale-graph alert input) MUST advance to
the re-ingested HEAD sha. Otherwise /freshz pages `behind: True` with an ever-growing age FOREVER on a
coordinate that has, in fact, already been re-ingested — predictions then run on a graph the App THINKS is
stale, and the operator sees a perpetual graph_stale alert (the round-5 prod symptom).

WHAT THIS LOCKS (the regression-prevention invariant): the self-heal path (self_heal_main_graph → ingest_push →
_full_ingest → core.ingest_graph_with_authority) and the normal push-ingest path MUST both update the SAME
freshness coordinate (core.graph_version.commit_sha / captured_at / ingested_at) that graph_freshness reads back
via core.coordinate_graph_sha — so a re-ingest is never SILENTLY divorced from the freshness it should refresh.
Content-free (a commit sha is public git metadata + a timestamp), moat unchanged (it drives the existing gate
functions only), fail-open (a freshness-update error never crashes the ingest — the self-heal stays never-crash).

DRIVES THE REAL FUNCTIONS (no SQL-stubbing the property away) over a real scratch Postgres:
  A) ingest main's graph at sha A (the live ingest_push path) → graph_freshness reads A, behind=False (fresh).
  B) main HEAD MOVES to sha B but the App MISSED the push (a dropped delivery / restart mid-event) → the stored
     graph is still A while HEAD is B → graph_freshness reads behind=True with the OLD sha A (the stale drift).
  C) self_heal_main_graph runs (exactly as boot/PR-time does) → it re-ingests main @HEAD (B) → and now the
     freshness coordinate reads B, behind=False, with a FRESH ingested_at (NOT the stale A). The /freshz surface
     (graph_freshness_all → owner_graph_freshness_surface) reads B too — the alert input resolves, not pages.

This is the BEFORE→AFTER the bug report describes: BEFORE self-heal the coordinate is provably stale at A
(behind=True), AFTER self-heal it is provably fresh at B (behind=False) — the perpetual graph_stale is killed
the moment a re-ingest actually happens, because the re-ingest carries its sha into the freshness record.

Run:  python3 tests/test_self_heal_freshness.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402

DB = "veripsa_selfheal_fresh_" + str(os.getpid())   # PER-PID (parallel-safe), like every DB-driven gate here
REPO = "acme/app"
BRANCH = "main"
SHA_A = "a" * 40   # the originally-ingested HEAD
SHA_B = "b" * 40   # main's NEW HEAD after a push the App MISSED — what self-heal must re-ingest + freshen to
SHA_D = "d" * 40   # a later cold revival after retention pruned the rebuildable graph/cache

checks = []


def chk(c, label):
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    checks.append(bool(c))


class FakeGH:
    """Minimal GitHub client for the ingest + freshness paths. `head` is the CURRENT main HEAD the freshness
    read + self-heal resolve against — we flip it from A→B to simulate a push the App missed. download_tarball
    serves a tiny real tarball so _full_ingest's safe-extract + count step runs (the GRAPH itself comes from the
    monkeypatched build_graph, so the tarball contents are irrelevant). get_file_at is unused on the full path."""

    def __init__(self):
        self.head = SHA_A

    def for_installation(self, installation_id):
        return self

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"# placeholder (the real graph comes from the patched extractor)\n"
            ti = tarfile.TarInfo("repo-" + sha[:7] + "/app.py")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    def repo_default_branch_head(self, repo):
        # (default_branch, head_sha) — exactly what graph_freshness + self_heal_main_graph read.
        return (BRANCH, self.head)

    def get_file_at(self, *a, **k):
        return None


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    import ingest as I
    import graph_freshness as F
    import code_graph_extract as X

    # A tiny content-free graph fed through the LIVE ingest path — _full_ingest calls X.build_graph(root) on the
    # extracted tarball, so patch it to return this graph deterministically (the extractor is out of scope here).
    # This fixture stands in for the CURRENT production extractor, so it must
    # carry the producer-owned top-level version that real build_graph returns.
    # Omitting it intentionally means a legacy/unknown producer and therefore
    # keeps freshness behind=True under the cg2 fail-closed contract.
    GRAPH = {
        "extractor_version": X.EXTRACTOR_VERSION,
        "metrics": {"schema_contract_version": X.SCHEMA_CONTRACT_VERSION},
        "nodes": [
            {
                "id": "app.py",
                "kind": "file",
                "path": "app.py",
                "name": "app.py",
                "language": "python",
            }
        ],
        "edges": [],
    }
    orig_build = X.build_graph
    orig_populate = I.populate_cochange_async
    X.build_graph = lambda *a, **k: {
        "extractor_version": GRAPH["extractor_version"],
        "metrics": GRAPH["metrics"],
        "nodes": GRAPH["nodes"],
        "edges": GRAPH["edges"],
    }
    populate_calls = []

    def fake_populate(gh_arg, repo_arg, branch_arg, window=800, repository_id=None):
        populate_calls.append((repo_arg, branch_arg, repository_id))
        return True

    I.populate_cochange_async = fake_populate

    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("SET search_path=core")
        c.execute("SELECT core.enter_installation_with_authority(%s)", ("4242",))
    with psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}") as route_conn:
        with route_conn.cursor() as c:
            c.execute(
                "UPDATE core.installation_account "
                "SET github_installation_id=%s, "
                "    github_installation_created_at=%s::timestamptz, "
                "    revoked_at=NULL "
                "WHERE installation_id=%s",
                ("4242", "2026-01-01T00:00:00Z", "4242"),
            )

    def db(sql, args=()):
        with conn.cursor() as c:
            c.execute(sql, args)
            row = c.fetchone()
            return row[0] if row else None

    gh = FakeGH()

    try:
        # ── A) ingest main's graph at sha A (the live push-ingest path) → freshness reads A, fresh ──────────
        I.ingest_push(db, gh, REPO, BRANCH, SHA_A, payload=None, coalesce=None)
        f_a = F.graph_freshness(db, gh, REPO, BRANCH)
        chk(f_a.get("stored_sha") == SHA_A and f_a.get("head_sha") == SHA_A and f_a.get("behind") is False,
            f"A) after ingest @A the freshness coordinate is FRESH: stored={(f_a.get('stored_sha') or '')[:7]} "
            f"head={(f_a.get('head_sha') or '')[:7]} behind={f_a.get('behind')}")

        # ── B) main HEAD moves to B but the App MISSED the push → stored still A, HEAD B → behind=True ──────
        gh.head = SHA_B
        f_before = F.graph_freshness(db, gh, REPO, BRANCH)
        chk(f_before.get("stored_sha") == SHA_A and f_before.get("head_sha") == SHA_B
            and f_before.get("behind") is True,
            f"B) a MISSED push leaves the coordinate STALE BEFORE self-heal: stored={(f_before.get('stored_sha') or '')[:7]} "
            f"(still A) head={(f_before.get('head_sha') or '')[:7]} (now B) behind={f_before.get('behind')} (=True)")
        # and the /freshz surface (graph_freshness_all → owner_graph_freshness_surface) also reads STALE = behind:True.
        all_before = F.graph_freshness_all(db, gh)
        row_before = next((c for c in all_before if c.get("repo") == REPO and c.get("branch") == BRANCH), None)
        chk(row_before is not None and row_before.get("stored_sha") == SHA_A and row_before.get("behind") is True,
            f"B) the /freshz surface ALSO pages STALE before self-heal: {row_before}")

        # ── C) self-heal runs (exactly as boot/PR-time does) → re-ingest @HEAD (B) → freshness MUST advance ─
        healed = I.self_heal_main_graph(db, gh, REPO, BRANCH)
        chk(healed.get("healed") is True and healed.get("head_sha") == SHA_B,
            f"C) self_heal_main_graph re-ingested main @HEAD: healed={healed.get('healed')} head={(healed.get('head_sha') or '')[:7]}")
        chk(populate_calls == [],
            "C) non-cold self-heal does not reseed co-change (stored graph existed; no extra advisory job)")

        f_after = F.graph_freshness(db, gh, REPO, BRANCH)
        chk(f_after.get("stored_sha") == SHA_B and f_after.get("head_sha") == SHA_B
            and f_after.get("behind") is False,
            f"C) AFTER self-heal the freshness coordinate ADVANCED to the re-ingested HEAD: "
            f"stored={(f_after.get('stored_sha') or '')[:7]} (now B, NOT stale A) head={(f_after.get('head_sha') or '')[:7]} "
            f"behind={f_after.get('behind')} (=False) — the perpetual graph_stale is KILLED")

        # the /freshz surface (the operator's alert input) now resolves too: stored=B, behind=False, fresh age.
        all_after = F.graph_freshness_all(db, gh)
        row_after = next((c for c in all_after if c.get("repo") == REPO and c.get("branch") == BRANCH), None)
        chk(row_after is not None and row_after.get("stored_sha") == SHA_B and row_after.get("behind") is False
            and isinstance(row_after.get("age_seconds"), (int, float)) and row_after.get("age_seconds") < 120,
            f"C) the /freshz surface RESOLVES after self-heal (NOT a perpetual stale page): {row_after}")

        # CONSISTENCY: the self-heal freshness write is the SAME write the NORMAL push-ingest does — prove the
        # ordinary push path also advances the coordinate (so the two paths can never drift to where only one
        # updates freshness). A normal push to a third sha must move the coordinate to it, behind=False.
        SHA_C = "c" * 40
        gh.head = SHA_C
        I.ingest_push(db, gh, REPO, BRANCH, SHA_C, payload=None, coalesce=None)
        f_push = F.graph_freshness(db, gh, REPO, BRANCH)
        chk(f_push.get("stored_sha") == SHA_C and f_push.get("behind") is False,
            f"CONSISTENCY: a NORMAL push-ingest advances the SAME coordinate too (stored={(f_push.get('stored_sha') or '')[:7]} "
            f"behind={f_push.get('behind')}) — push + self-heal both freshen freshness, never one without the other")

        # COLD-REPO RETENTION REVIVAL: simulate retention deleting the rebuildable graph/cache for an inactive
        # repo. A later PR-time self-heal sees stored_sha=NULL, cold-starts the graph, and must also queue a
        # history-based co-change populate so the advisory second signal recovers beyond the current push.
        with psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}") as mconn:
            with mconn.cursor() as c:
                c.execute("SET search_path=core")
                c.execute("SELECT account_id FROM core.installation_account WHERE installation_id=%s", ("4242",))
                acct = c.fetchone()[0]
                c.execute("SELECT set_config('core.current_account', %s, true)", (acct,))
                c.execute("DELETE FROM core.code_edge WHERE account_id=%s AND repo=%s", (acct, REPO))
                c.execute("DELETE FROM core.code_node WHERE account_id=%s AND repo=%s", (acct, REPO))
                c.execute("DELETE FROM core.graph_version WHERE account_id=%s AND repo=%s", (acct, REPO))
                c.execute("DELETE FROM core.co_change WHERE account_id=%s AND repo=%s", (acct, REPO))
                c.execute("DELETE FROM core.co_change_seen_commit WHERE account_id=%s AND repo=%s", (acct, REPO))
        populate_calls.clear()
        gh.head = SHA_D
        healed_cold = I.self_heal_main_graph(db, gh, REPO, BRANCH)
        chk(healed_cold.get("healed") is True and healed_cold.get("stored_sha") is None
            and healed_cold.get("head_sha") == SHA_D,
            f"COLD) self-heal cold-starts after retention pruned the graph/cache: "
            f"healed={healed_cold.get('healed')} stored={healed_cold.get('stored_sha')} "
            f"head={(healed_cold.get('head_sha') or '')[:7]}")
        chk(populate_calls == [(REPO, BRANCH, None)]
            and healed_cold.get("cochange_populate", {}).get("dispatched") is True,
            f"COLD) cold self-heal queues history-based co-change reseed exactly once "
            f"(calls={populate_calls}, result={healed_cold.get('cochange_populate')})")
    finally:
        X.build_graph = orig_build
        I.populate_cochange_async = orig_populate
        conn.close()
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)

    print("SELF-HEAL FRESHNESS GATE: " + ("PASS" if all(checks) else "FAIL"))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
