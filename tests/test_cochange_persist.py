#!/usr/bin/env python3
"""CO-CHANGE PERSIST gate — the per-push co-change increment PERSISTED, moat-isolated, byte-identical to a batch.

#259 proved the per-push increment is byte-identical to a batch over the SAME commits, but did NOT persist it:
the co_change store only had a full DELETE+reINSERT writer + a path-FILTERED partner read — no read-back-ALL
surface and no n_total column, so the increment could neither recover the seed counters nor merge onto them. This
gate proves the persistence #259 handed off, end to end against a REAL ephemeral Postgres (the moat is live):

  (a) PERSISTS + SURVIVES A RE-READ — a push's commits, folded off-worker (_cochange_increment_task), land in the
      co_change cache and read back via co_change_all_with_authority with co/n_a/n_b/n_total intact.
  (b) TWO PUSHES FOLDED == ONE BATCH — two pushes folded one-after-another through the persisted seed produce the
      EXACT pair set a single batch ingest over all the commits produces (byte-identical, tenant-pinned). This is
      the #259 invariant carried through the STORE: read-back → fold → re-ingest does not drift from the batch.
  (c) SHA-DEDUPE — a REDELIVERED push (the SAME commits[]) folded again through increment_cochange_async is a true
      NO-OP (the sha-dedupe in the dispatcher drops the redelivery → no double-count). Without dedupe it WOULD
      double-count (proven, so the dedupe is load-bearing).
  (d) FAIL-OPEN — an injected error in the increment path (a gh that can't resolve the account, a broken payload)
      returns content-free + NEVER raises: co-change is the advisory 2nd signal; a failure must not drop the verdict.
  (e) GOVERNED-WRITE / RLS INTACT — a NON-App role cannot write the cache directly (the forgery gate refuses a raw
      INSERT), and a SECOND tenant reading the same repo coordinate sees NOTHING (FORCE RLS walls the cache).

Content-free end to end (paths + counts only). Run: python3 tests/test_cochange_persist.py (needs local Postgres).
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
import _cg_cochange as CC  # noqa: E402
import cochange as COCH    # noqa: E402

DB = "veripsa_ccpersist_" + str(os.getpid())
REPO = "acme/cc"
BRANCH = "main"
checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def tenant(install_id):
    """A db runner pinned to one installation's tenant (enter_installation), mirroring the live per-event path —
    the SAME _scoped_db shape ingest_cochange / cochange_all expect (db(sql, args) -> the single scalar)."""
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("SET search_path=core")
        c.execute("SELECT core.enter_installation_with_authority(%s)", (install_id,))

    def run(sql, args=()):
        with conn.cursor() as c:
            c.execute(sql, args)
            row = c.fetchone()
            return row[0] if row else None
    return run


def _j(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


class FakeGH:
    """The minimal gh seam _cochange_increment_task needs: the owning-account id (== the live tenant key, the
    installation id the SAME enter_installation routes to 'ACCT-GH-<id>'). No clone, no network — the per-push
    path never touches a repo body."""
    def __init__(self, install_id):
        self._id = install_id

    def installation_account_id(self):
        return self._id


def _push(payload_commits):
    """A content-free push webhook payload — only commit ids + added/modified/removed path lists (what
    push_commit_filesets reads). NO body, message, author."""
    return {"commits": payload_commits}


def _sha(tag):
    """A realistic 40-hex commit id (what a real GitHub push payload's commits[].id always is) derived from a
    test tag — so the sha-dedupe keys on a REAL hex sha, not the synthetic no-id fallback."""
    import hashlib
    return hashlib.sha1(tag.encode()).hexdigest()


def _c(tag, added=None, modified=None, removed=None):
    return {"id": _sha(tag), "added": added or [], "modified": modified or [], "removed": removed or []}


def _norm(pairs):
    """Canonicalise a pair list for byte-identical comparison: each pair as a sorted-key tuple, the whole list
    sorted. (emit_pairs already sorts deterministically; this makes the equality robust to dict ordering.)"""
    return sorted(tuple(sorted(p.items())) for p in pairs)


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1
    os.environ["VERIPSA_DSN"] = f"postgresql://veripsa_app@localhost/{DB}"

    A = tenant("111")   # ACCT-GH-111
    B = tenant("222")   # ACCT-GH-222
    ghA = FakeGH("111")

    # Two pushes that ACCUMULATE a coupling ACROSS pushes — the case a single WRITE-time support floor LOSES (the
    # #314 regression). Push 1: auth+api co-change 3× — BELOW the customer render floor (5) but >= the PERSIST floor
    # (2), so it is STORED RAW — + background files so auth/api are rare vs N (lift >> 1). Push 2: auth+api co-change
    # 2 more times → co=5. The proper fix PROVES: push1's co=3 PERSISTS (accumulation) though below the render floor;
    # after push2 the pair reaches co=5; folding push1-then-push2 onto the persisted seed == one batch (byte-identical).
    # The customer render floor (co>=5) is applied at READ (co_change_partners_with_authority), NOT at storage — so the
    # co=3 pair persists+accumulates yet is NOT surfaced to a customer until it reaches co=5.
    push1 = _push(
        [_c(f"p1c{i}", modified=["backend/auth.py", "backend/api.py"]) for i in range(3)]
        + [_c(f"p1b{k}", added=[f"misc/m{k}.py"]) for k in range(12)]
    )
    push2 = _push(
        [_c(f"p2c{i}", modified=["backend/auth.py", "backend/api.py"]) for i in range(2)]
        + [_c(f"p2b{k}", added=[f"misc/n{k}.py"]) for k in range(12)]
    )
    fs1 = COCH.push_commit_filesets(push1)    # bare sets (for the batch reference)
    fs2 = COCH.push_commit_filesets(push2)
    sfs1 = COCH.push_commit_shafilesets(push1)  # (sha, set) pairs — what the increment task folds
    sfs2 = COCH.push_commit_shafilesets(push2)

    # ── (a) PERSIST + SURVIVE A RE-READ ────────────────────────────────────────────────────────────────────────
    res1 = COCH._cochange_increment_task(ghA, REPO, BRANCH, sfs1)
    stored1 = _j(A("SELECT core.co_change_all_with_authority(%s,%s,%s)", (REPO, BRANCH, 5000)))
    pair_a = next((p for p in stored1 if p.get("a") == "backend/api.py" and p.get("b") == "backend/auth.py"), None)
    chk(res1.get("ok") and pair_a is not None and pair_a.get("co") == 3 and pair_a.get("n_total", 0) >= 15,
        f"(a) the push increment PERSISTS a SUB-RENDER-FLOOR pair (auth↔api co=3 < 5) so it can ACCUMULATE "
        f"(got {pair_a}, res={res1})")

    # ── (a2) RENDER FLOOR at READ — the co=3 pair must NOT be surfaced to a customer (precision preserved) ───────
    partners_lo = _j(A("SELECT core.co_change_partners_with_authority(%s,%s,%s,%s)", (REPO, ["backend/auth.py"], 3, 0.4)))
    chk(not any(p.get("partner") == "backend/api.py" for p in partners_lo),
        f"(a2) the persisted co=3 pair is BELOW the render floor → NOT shown to a customer (precision) (got {partners_lo})")

    # ── (b) TWO PUSHES FOLDED == ONE BATCH ─────────────────────────────────────────────────────────────────────
    # The persisted state already holds push1 (from (a)). Fold push2 onto the PERSISTED seed → the streamed result.
    COCH._cochange_increment_task(ghA, REPO, BRANCH, sfs2)
    streamed = _j(A("SELECT core.co_change_all_with_authority(%s,%s,%s)", (REPO, BRANCH, 5000)))
    # The batch: emit_pairs over ALL of push1+push2's commits in ONE fold (the source-of-truth equivalent).
    import collections
    change, co = collections.Counter(), collections.Counter()
    n = CC.fold_commits(list(fs1) + list(fs2), change, co)
    batch = CC.emit_pairs(change, co, n, min_support=CC.PERSIST_MIN_SUPPORT, min_prob=0.0, min_lift=0.0)  # store keeps raw counts
    # Re-key the batch into the STORED read-back shape (a/b/co/n_a/n_b/n_total/strength/lift, lift rounded to 2,
    # strength to 3 — exactly co_change_all_with_authority's projection) so the comparison is on the persisted shape.
    batch_stored = [{"a": p["a"], "b": p["b"], "co": p["co"], "n_a": p["n_a"], "n_b": p["n_b"],
                     "n_total": p["n_total"], "strength": round(p["strength"], 3), "lift": round(p["lift"], 2)}
                    for p in batch]
    chk(_norm(streamed) == _norm(batch_stored) and len(streamed) >= 1,
        f"(b) two pushes folded one-after-another == one batch over the same commits (byte-identical, tenant-pinned) "
        f"(streamed {len(streamed)} vs batch {len(batch_stored)})")

    # ── (b2) RENDER FLOOR at READ — the accumulated co=5 pair NOW clears the floor → shown to the customer (recall) ─
    partners_hi = _j(A("SELECT core.co_change_partners_with_authority(%s,%s,%s,%s)", (REPO, ["backend/auth.py"], 3, 0.4)))
    chk(any(p.get("partner") == "backend/api.py" and p.get("co") == 5 for p in partners_hi),
        f"(b2) the accumulated co=5 pair clears the render floor → shown to the customer (recall) (got {partners_hi})")

    # ── (c) SHA-DEDUPE — a redelivered push is a NO-OP (idempotent ACROSS DELIVERIES) ──────────────────────────
    before = _j(A("SELECT core.co_change_all_with_authority(%s,%s,%s)", (REPO, BRANCH, 5000)))
    # Re-deliver push2 through the FULL dispatcher. The persisted folded-commit ledger already holds push2's shas
    # (folded in (b)) → the gated filter returns ZERO unseen → the task folds nothing → a true no-op. Await it.
    fut = COCH.increment_cochange_async(ghA, REPO, BRANCH, push2)
    r_c = fut.result(timeout=30) if fut is not None else {}
    after = _j(A("SELECT core.co_change_all_with_authority(%s,%s,%s)", (REPO, BRANCH, 5000)))
    pair_after = next((p for p in after if p.get("a") == "backend/api.py" and p.get("b") == "backend/auth.py"), None)
    chk(_norm(before) == _norm(after) and pair_after is not None and pair_after.get("co") == 5
        and (r_c.get("commits_folded") == 0),
        f"(c) sha-dedupe: re-delivering an already-folded push is a NO-OP across deliveries (no double-count; "
        f"auth↔api stays co=5; commits_folded=0) (got {pair_after}, res={r_c})")
    # PROVE the dedupe is load-bearing: folding push2's sets AGAIN WITHOUT the ledger filter DOES double-count.
    dbl = CC.cochange_pairs_incremental(after, fs2, min_support=CC.PERSIST_MIN_SUPPORT, min_prob=0.0, min_lift=0.0)
    dbl_pair = next((p for p in dbl if p["a"] == "backend/api.py" and p["b"] == "backend/auth.py"), None)
    chk(dbl_pair is not None and dbl_pair["co"] == 7,
        f"(c2) WITHOUT dedupe the same push double-counts (co 5 -> 7) — so the persisted sha-dedupe is load-bearing "
        f"(got {dbl_pair})")

    # ── (d) FAIL-OPEN — an injected error never raises, never drops the verdict ────────────────────────────────
    base = _j(A("SELECT core.co_change_all_with_authority(%s,%s,%s)", (REPO, BRANCH, 5000)))
    # (d1) a gh that can't resolve the owning account → FAIL CLOSED on the WRITE (no cross-tenant), but the task
    # returns content-free, never raises.
    bad_gh = FakeGH(None)
    try:
        r_d1 = COCH._cochange_increment_task(bad_gh, REPO, BRANCH, sfs1)
        raised = False
    except Exception:
        raised = True
    chk((not raised) and isinstance(r_d1, dict) and not r_d1.get("ok"),
        f"(d1) unresolved account → fail-closed WRITE + fail-open RETURN (content-free, no raise) (got {r_d1})")
    # (d2) a malformed payload through the full dispatcher never raises and leaves the cache untouched.
    try:
        fut2 = COCH.increment_cochange_async(ghA, REPO, BRANCH, {"commits": "not-a-list"})
        if fut2 is not None:
            fut2.result(timeout=30)
        raised2 = False
    except Exception:
        raised2 = True
    unchanged = _j(A("SELECT core.co_change_all_with_authority(%s,%s,%s)", (REPO, BRANCH, 5000)))
    chk((not raised2) and _norm(unchanged) == _norm(base),
        "(d2) a malformed push payload never raises and stores no increment (advisory signal, fail-open)")

    # ── (e) GOVERNED-WRITE / RLS INTACT ────────────────────────────────────────────────────────────────────────
    # (e1) a NON-App writer role cannot write the cache by a raw INSERT — the forgery gate refuses it (the ONLY
    # write path is the gated ingest fn). Connect as veripsa_demo_agent (a writer seat, NOT the App) and try.
    forgery_blocked = False
    wc = psycopg2.connect(f"postgresql://veripsa_demo_agent@localhost/{DB}")
    wc.autocommit = True
    try:
        with wc.cursor() as c:
            c.execute("SET search_path=core")
            c.execute("SELECT set_config('core.current_account','ACCT-DEMO',true)")
            try:
                c.execute("INSERT INTO core.co_change(account_id,repo,path_a,path_b,co,n_a,n_b,strength,lift,n_total) "
                          "VALUES ('ACCT-DEMO',%s,'a','b',1,1,1,1.0,1.0,1)", (REPO,))
            except psycopg2.Error:
                forgery_blocked = True   # forgery gate (or RLS) refused the raw write — the moat held
    finally:
        wc.close()
    chk(forgery_blocked, "(e1) governed-write: a NON-App raw INSERT into the co_change cache is refused (forgery gate)")
    # (e2) cross-tenant isolation: tenant B reading the SAME repo coordinate sees NOTHING (FORCE RLS).
    b_all = _j(B("SELECT core.co_change_all_with_authority(%s,%s,%s)", (REPO, BRANCH, 5000)))
    chk(b_all == [], f"(e2) cross-tenant isolation: a 2nd tenant reading the same repo sees NOTHING (got {b_all})")
    # (e3) content-free: nothing but paths + counts ever crossed the boundary (no message/body/author/sha tokens).
    chk(all(set(p.keys()) <= {"a", "b", "co", "n_a", "n_b", "n_total", "strength", "lift"} for p in stored1),
        "(e3) content-free: the read-back-all surface returns ONLY paths + counts (no message / body / author / sha)")

    ok = all(checks)
    print("COCHANGE PERSIST GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
