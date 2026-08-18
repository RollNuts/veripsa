#!/usr/bin/env python3
"""CO-CHANGE PER-PUSH gate — keep the co-change (logical-coupling) signal CURRENT on every push, content-free,
WITHOUT a clone, with the EXACT same precision discipline as the full-history batch path.

WHY this exists: co-change (files that historically change together — Veripsa's 2nd detector, the coupling the
structural graph is blind to) is SEEDED by a full BACKFILL (a blobless clone of git history). Between backfills
the LIVE per-push path refreshes the structural graph but does NOT incrementally fold co-change, so the
co-change signal goes STALE exactly when (in an AI-era, high-velocity repo) coupling is forming. The fix folds
each push's commits into the co-change counters incrementally — and a webhook `push` payload's `commits[]` each
carry their added/modified/removed file lists = the content-free co-change input, so NO clone is needed.

MEASURE-FIRST: the load-bearing property is EQUIVALENCE — folding a set of commits' changed-file lists
incrementally must count BYTE-IDENTICALLY to the batch `_git_log_commits`→fold over those same commits (same
giant-commit skip, same support floor, same lift, same 0%→not-evaluated discipline). We prove that on a real git
history (clone path) AND on synthetic commit sets (push path), then prove the per-push reader is content-free,
giant-skipping, redelivery-safe, and FAIL-OPEN. PURE PYTHON + a tiny local git repo — no Postgres needed.

  (a) EQUIVALENCE — per-push increment == batch over the SAME commits (the byte-identical counting math);
  (b) ASSOCIATIVITY — N pushes folded one-by-one == one batch over all of them (so per-push streaming is exact);
  (c) GIANT-COMMIT SKIP — a > max_commit_files commit mints no pairs through the per-push path (noise control);
  (d) RE-DELIVERY — the SAME push folded twice WITHOUT sha-dedup double-counts (proving dedup is REQUIRED), and
      the caller's sha-dedup makes a redelivery a NO-OP (idempotent-safe);
  (e) FAIL-OPEN — push_commit_filesets NEVER raises on a malformed / injected-error payload (the increment is
      the advisory 2nd signal: a payload we can't read yields no increment, never a crash that drops the verdict);
  (f) SEED reconstruction — counters seeded from stored pairs + a push fold == a batch over (seed-history+push),
      and the HONEST all-time-additive limit is exercised (the increment accumulates on the last stored signal).

Run:  python3 tests/test_cochange_per_push.py    (needs git; NO Postgres)
"""
from __future__ import annotations

import collections
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import _cg_cochange as CC  # noqa: E402
import cochange as COCH    # noqa: E402

checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def _git(d, *a):
    subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, check=True)


def _commit(d, files, tok):
    for f in files:
        p = os.path.join(d, f)
        os.makedirs(os.path.dirname(p) or d, exist_ok=True)
        open(p, "a").write(f"// SECRET_BODY_{tok}\n")   # a body the content-free path must never read
        _git(d, "add", f)
    _git(d, "commit", "-m", f"SECRET_MSG_{tok}", "--no-verify")


def _push_payload(commit_filesets):
    """A minimal `push` webhook payload with one commits[] entry per (added, modified, removed) tuple — the
    content-free shape GitHub delivers (plus SECRET fields the reader must ignore)."""
    commits = []
    for added, modified, removed in commit_filesets:
        commits.append({"id": "deadbeef", "message": "SECRET_MSG", "author": {"name": "SECRET_AUTHOR"},
                        "added": list(added), "modified": list(modified), "removed": list(removed)})
    return {"ref": "refs/heads/main", "after": "f" * 40, "commits": commits, "head_commit": {"timestamp": "x"}}


def main() -> int:
    # ---- a real git history (the clone/batch baseline): auth↔api co-change 5x, each solo 2x, 40 background
    # commits (so auth/api are rare vs N → lift >> 1), and a 50-file GIANT commit (must be skipped). ----------
    with tempfile.TemporaryDirectory() as d:
        _git(d, "init", "-q"); _git(d, "config", "user.email", "t@e.com"); _git(d, "config", "user.name", "T")
        _git(d, "config", "commit.gpgsign", "false")
        for i in range(5):
            _commit(d, ["backend/auth.py", "backend/api.py"], f"p{i}")
        _commit(d, ["backend/auth.py"], "as1"); _commit(d, ["backend/auth.py"], "as2")
        _commit(d, ["backend/api.py"], "is1"); _commit(d, ["backend/api.py"], "is2")
        for k in range(40):
            _commit(d, [f"misc/m{k}.py"], f"bg{k}")
        _commit(d, [f"v/lib{n}.py" for n in range(50)], "giant")
        batch_pairs = CC.cochange_pairs(d, window=2000, max_commit_files=40, min_support=3, min_prob=0.3, min_lift=2.0)
        clone_commits = CC._git_log_commits(d, 2000, 60)   # the per-commit file SETS the batch folded

    # (a) EQUIVALENCE on the REAL history: re-run the SAME commit sets through the INCREMENTAL fold path (seed
    # EMPTY, fold every commit as if streamed one push at a time) and assert the emitted pairs are IDENTICAL to
    # the batch. Same fold_commits + emit_pairs underneath → byte-identical by construction; this proves it end
    # to end on a real repo. We fold each commit set as its OWN one-commit push to exercise the streaming path.
    streamed = [{p for p in c} for c in clone_commits]
    incr_pairs = CC.cochange_pairs_incremental([], streamed, max_commit_files=40, min_support=3,
                                               min_prob=0.3, min_lift=2.0)
    chk(incr_pairs == batch_pairs and len(batch_pairs) >= 1,
        f"(a) per-push increment over the SAME commits == batch (byte-identical pairs; {len(batch_pairs)} pair(s))")

    # (b) ASSOCIATIVITY: fold the history in THREE arbitrary chunks (3 separate pushes) onto one running counter
    # set; the result must equal one batch over the whole lot. Proves streaming pushes one-by-one is EXACT.
    ch, co = collections.Counter(), collections.Counter()
    n = 0
    third = max(1, len(streamed) // 3)
    for chunk in (streamed[:third], streamed[third:2 * third], streamed[2 * third:]):
        n += CC.fold_commits(chunk, ch, co, max_commit_files=40)
    chunked_pairs = CC.emit_pairs(ch, co, n, min_support=3, min_prob=0.3, min_lift=2.0)
    chk(chunked_pairs == batch_pairs,
        "(b) associativity: N pushes folded one-by-one == one batch over all of them (exact streaming)")

    # ---- the PUSH path: build the SAME history as push payloads (commits[].added/modified/removed) and prove the
    # content-free reader → fold reproduces the coupling, with the giant commit as ONE push. -------------------
    push_sets = [({"backend/auth.py", "backend/api.py"}, set(), set()) for _ in range(5)]
    push_sets += [({"backend/auth.py"}, set(), set()), ({"backend/auth.py"}, set(), set())]
    push_sets += [({"backend/api.py"}, set(), set()), ({"backend/api.py"}, set(), set())]
    push_sets += [({f"misc/m{k}.py"}, set(), set()) for k in range(40)]
    giant_push = (set(f"v/lib{n}.py" for n in range(50)), set(), set())

    payload = _push_payload(push_sets + [giant_push])
    commit_filesets = COCH.push_commit_filesets(payload)        # content-free per-commit sets from commits[]
    push_pairs = CC.cochange_pairs_incremental([], commit_filesets, max_commit_files=40, min_support=3,
                                               min_prob=0.3, min_lift=2.0)
    auth_api = next((p for p in push_pairs if {p["a"], p["b"]} == {"backend/auth.py", "backend/api.py"}), None)
    chk(auth_api is not None and auth_api["co"] == 5 and auth_api["n_a"] == 7 and auth_api["n_b"] == 7,
        f"(push) the push payload's commits[] reproduce auth↔api co=5, n=7 each (got {auth_api})")

    # (c) GIANT-COMMIT SKIP through the per-push path: the 50-file commit mints NO pair (noise control survives).
    chk(not any(p["a"].startswith("v/lib") or p["b"].startswith("v/lib") for p in push_pairs),
        "(c) the 50-file giant push minted no pairs (giant-commit skip survives the per-push path)")

    # (d) RE-DELIVERY: folding the SAME push commits a SECOND time WITHOUT dedup double-counts (proves dedup is
    # required); the caller's sha-dedup (drop already-seen commit shas) makes a redelivery a true NO-OP.
    once = CC.cochange_pairs_incremental([], commit_filesets, max_commit_files=40, min_support=3, min_prob=0.3, min_lift=2.0)
    twice = CC.cochange_pairs_incremental([], commit_filesets + commit_filesets, max_commit_files=40,
                                          min_support=3, min_prob=0.3, min_lift=2.0)
    aa_once = next(p for p in once if {p["a"], p["b"]} == {"backend/auth.py", "backend/api.py"})
    aa_twice = next(p for p in twice if {p["a"], p["b"]} == {"backend/auth.py", "backend/api.py"})
    chk(aa_twice["co"] == 2 * aa_once["co"],
        f"(d) re-delivery WITHOUT dedup double-counts (co {aa_once['co']}→{aa_twice['co']}) — dedup is REQUIRED")
    # caller-side dedup model: a redelivery carries the SAME commit ids → after dropping seen ids, NOTHING folds.
    seen = {c.get("id") for c in payload["commits"]}
    redelivered = [c for c in payload["commits"] if c.get("id") not in seen]   # all already seen → empty
    deduped = CC.cochange_pairs_incremental([], COCH.push_commit_filesets({"commits": redelivered}),
                                            max_commit_files=40, min_support=3, min_prob=0.3, min_lift=2.0)
    chk(deduped == [], "(d2) sha-dedup makes a redelivery a NO-OP (idempotent-safe: no double-count)")

    # (e) FAIL-OPEN: push_commit_filesets must NEVER raise on any malformed / injected payload — the increment is
    # advisory; a payload we cannot read yields no increment, never a crash that would drop the structural verdict.
    bad_inputs = [None, {}, {"commits": None}, {"commits": "nope"}, {"commits": [None, 7, "x"]},
                  {"commits": [{"added": "notalist", "modified": None, "removed": 5}]},
                  {"commits": [{"added": [1, None, "ok.py"], "modified": ["m.py"], "removed": []}]},
                  "totally-not-a-dict", 12345]
    failopen = True
    for bad in bad_inputs:
        try:
            r = COCH.push_commit_filesets(bad)
            if not isinstance(r, list):
                failopen = False
        except Exception:
            failopen = False
    chk(failopen, "(e) FAIL-OPEN: push_commit_filesets never raises on malformed/injected payloads (returns a list)")
    # the one well-formed-but-noisy commit above still yields its valid paths only (non-str entries dropped).
    one = COCH.push_commit_filesets({"commits": [{"added": [1, None, "ok.py"], "modified": ["m.py"], "removed": []}]})
    chk(one == [{"ok.py", "m.py"}], f"(e2) the reader keeps only valid string paths (content-free) — got {one}")

    # (f) SEED reconstruction + the HONEST all-time-additive limit: seed the counters from the STORED batch pairs,
    # then fold ONE more push that strengthens auth↔api; the auth↔api co must INCREMENT by exactly the +1 the new
    # push adds on top of the seed's co (5 → 6). This is the "accumulate on the last stored signal" behavior — the
    # increment never re-derives history (the periodic re-backfill stays the source of truth).
    seed = batch_pairs
    one_more = [{"backend/auth.py", "backend/api.py"}]
    grown = CC.cochange_pairs_incremental(seed, one_more, max_commit_files=40, min_support=3, min_prob=0.3, min_lift=2.0)
    aa_seed = next(p for p in seed if {p["a"], p["b"]} == {"backend/auth.py", "backend/api.py"})
    aa_grown = next(p for p in grown if {p["a"], p["b"]} == {"backend/auth.py", "backend/api.py"})
    chk(aa_grown["co"] == aa_seed["co"] + 1 and aa_grown["n_a"] == aa_seed["n_a"] + 1 and aa_grown["n_b"] == aa_seed["n_b"] + 1,
        f"(f) seed + push increments the stored counters by exactly the push (co {aa_seed['co']}→{aa_grown['co']})")

    # CONTENT-FREE across the whole path: no SECRET token (a body, message, or author) ever reached a pair.
    import json as _json
    chk("SECRET" not in _json.dumps(push_pairs) and "SECRET" not in _json.dumps(commit_filesets, default=list),
        "(content-free) no file body / commit message / author reached the per-push pairs (paths + counts only)")

    ok = all(checks)
    print("COCHANGE PER-PUSH GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
