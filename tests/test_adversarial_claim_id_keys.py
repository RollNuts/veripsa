#!/usr/bin/env python3
"""ADVERSARIAL-NAMES KEY-FORGERY / COLLISION gate — the engine builds its internal lock keys (change_id,
claim_id) by string-formatting EXTERNAL, attacker-influenced names (PR number, head-branch ref, file path).
A crafted name must never (a) forge ANOTHER change's lane, (b) collide two DISTINCT changes/paths onto one
key (false serialize / a PK-collision crash = false clear-miss), or (c) split off the wrong change_id.

Premise (audit:adversarial-names 2026-06-18). The gate derives change_id = the FIRST ':'-segment of claim_id
(db/schema/30_gate.sql) and the core.claim PK is (account_id, repo, claim_id). The OLD builder was
`f"PR-{n}:{path}"[:200]` — a LOSSY suffix-cut: target_path is allowed up to 1024 chars but claim_id is capped
at 200, so two DIFFERENT long sibling paths under the SAME change that share a ≥~195-char prefix truncate to
the SAME claim_id. The second path's INSERT then raises an UNCAUGHT duplicate-key inside _place_claim's own
unique_violation handler → the event's atomic txn aborts (the PR coordinates NOTHING, and every redelivery
re-crashes deterministically — a poison event). The fix routes both the PR-time (webhook._claim_id) and
push-time (server._branch_claim_id) builders through webhook._bounded_claim_id, which is INJECTIVE in
(change_id, path), preserves the change_id prefix, and stays within the cap.

This gate is PURE + OFFLINE (no DB, no network). It feeds the REAL key builders the adversarial name set and
asserts the three invariants directly. It also asserts the OLD lossy form WOULD have collided (so the gate
documents exactly what is now defended and fails if anyone reintroduces a lossy `[:cap]` cut).

Run:  python3 tests/test_adversarial_claim_id_keys.py     (no DB needed)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

from webhook import _bounded_claim_id, _claim_id, _CLAIM_ID_CAP, _CHANGE_ID_CAP  # noqa: E402

FAIL = 0


def check(cond: bool, label: str):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _change_of(claim_id: str) -> str:
    """Mirror EXACTLY the gate's change_id derivation: left(split on FIRST ':', cap)."""
    seg = claim_id.split(":", 1)[0] if ":" in claim_id else claim_id
    return seg[:_CLAIM_ID_CAP]


# A path set built to be hostile to the key construction: long sibling paths that differ only past the cap,
# SQL metacharacters, NUL bytes, '../' traversal, newlines, the ':' delimiter and the 'PR-'/'BR-' sentinels
# embedded INSIDE a path, plus the boring normal case (must be untouched).
_LONG = "src/" + "a" * 250 + "/"
ADVERSARIAL_PATHS = [
    "src/app.py",                                  # normal — natural form must be preserved unchanged
    _LONG + "fileA.py",                            # long sibling A
    _LONG + "fileB.py",                            # long sibling B (differs only past the cap → the collision)
    _LONG + "fileC.py",                            # long sibling C
    "x" * 1024,                                    # max-length path (gate's target_path cap)
    "a/" + "z" * 300 + ".py",                      # long, distinct
    "weird'; DROP TABLE core.claim;--/x.py",       # SQL metacharacters in the path
    "tab\tand\nnewline.py",                         # control chars / newline
    "nul\x00byte.py",                              # embedded NUL
    "../../../etc/passwd",                         # path traversal
    "PR-999:fake.py",                              # the change_id delimiter + a 'PR-' sentinel INSIDE the path
    "BR-other-branch:evil.py",                     # a 'BR-' sentinel + delimiter inside the path
    ":leadingcolon.py",                            # a leading ':' in the path
]

# change_ids: ordinary PR/branch keys plus a maliciously long branch ref and sentinel-bearing branch names.
ADVERSARIAL_CHANGES = [
    "PR-1",
    "PR-123456789",
    "BR-feature/login",
    "BR-PR-123",                                   # a branch literally named 'PR-123' → 'BR-PR-123', NOT 'PR-123'
    "BR-" + "feature/" * 40,                        # an over-cap branch ref
    "BR-weird$name",                               # '$' is a legal git ref char
]


def test_injective_and_bounded():
    """Distinct (change_id, path) → distinct claim_id, always within the cap. This is the core anti-collision
    invariant: no two real changes/paths can ever be mapped onto one lock key."""
    seen = {}
    collisions = 0
    overcap = 0
    for cid in ADVERSARIAL_CHANGES:
        for path in ADVERSARIAL_PATHS:
            key = _bounded_claim_id(cid, path)
            if len(key) > _CLAIM_ID_CAP:
                overcap += 1
            if key in seen and seen[key] != (cid, path):
                collisions += 1
            seen[key] = (cid, path)
    check(collisions == 0, f"injective: no two distinct (change_id, path) share a claim_id ({collisions} collisions)")
    check(overcap == 0, f"bounded: every claim_id <= {_CLAIM_ID_CAP} chars ({overcap} over cap)")


def test_long_siblings_dont_collide():
    """THE EXACT HOLE: two long sibling paths under one change must get DISTINCT claim_ids (no PK-collision
    crash), while the OLD lossy `[:cap]` form WOULD have collided them (proof the fix is load-bearing)."""
    a = _LONG + "fileA.py"
    b = _LONG + "fileB.py"
    old_a = f"PR-7:{a}"[:_CLAIM_ID_CAP]            # the pre-fix lossy form
    old_b = f"PR-7:{b}"[:_CLAIM_ID_CAP]
    check(old_a == old_b, "regression-witness: the OLD lossy form collided these siblings (the bug)")
    new_a = _claim_id(7, a)
    new_b = _claim_id(7, b)
    check(new_a != new_b, "fixed: the bounded builder gives the two siblings DISTINCT claim_ids")
    check(len(new_a) <= _CLAIM_ID_CAP and len(new_b) <= _CLAIM_ID_CAP, "fixed siblings stay within the cap")


def test_change_id_prefix_stable_across_paths():
    """The gate splits claim_id on the FIRST ':' to recover change_id. The builder must derive the SAME change_id
    for a given change REGARDLESS of the path — otherwise one change's files scatter across change_ids (false
    miss) or a short-path file and a long-path file of the same change land under different keys. The canonical
    change_id is the input capped to _CHANGE_ID_CAP (the same cap server._branch_change_id applies, so the
    release path and the claim path agree)."""
    bad = 0
    for cid in ADVERSARIAL_CHANGES:
        canonical = cid[:_CHANGE_ID_CAP]
        derived = {_change_of(_bounded_claim_id(cid, path)) for path in ADVERSARIAL_PATHS}
        if derived != {canonical}:
            bad += 1
            print(f"     change_id NOT stable for cid={cid[:24]!r}...: derived set size {len(derived)} (want 1)")
    check(bad == 0, "change_id is stable across all paths of a change — gate recovers ONE work-unit per change")


def test_no_cross_namespace_forgery():
    """A branch literally named 'PR-123' (a legal git ref) reserves under change_id 'BR-PR-123', NEVER the
    real PR's 'PR-123'. The 'BR-'/'PR-' prefix namespacing must hold so a crafted HEAD branch name cannot
    forge / hijack an actual PR's lane."""
    # server._branch_change_id is 'BR-<branch>'; a path-built claim for it must derive a 'BR-' change_id.
    branch_key = _bounded_claim_id("BR-PR-123", "any.py")
    derived = _change_of(branch_key)
    real_pr_key = _claim_id(123, "any.py")
    check(derived == "BR-PR-123", "a branch named 'PR-123' keys as 'BR-PR-123' (namespaced)")
    check(_change_of(real_pr_key) == "PR-123", "the real PR #123 keys as 'PR-123'")
    check(derived != _change_of(real_pr_key), "no forgery: the crafted branch can't impersonate the real PR's lane")


def test_push_pr_reconciliation_change_id_matches():
    """PUSH↔PR reconciliation: a feature branch reserves lanes at push time under change_id
    server._branch_change_id(branch); when its PR opens, server RELEASES that exact change_id and re-claims as
    'PR-<n>'. So the change_id the gate DERIVES from each push-time per-path claim_id MUST equal the change_id
    the release path passes — even for a maliciously long head ref. If they drift, the release misses the
    push-time claims (lanes stranded) — a real false-serialize. Import the server builders to assert end-to-end."""
    import importlib
    srv = importlib.import_module("server")
    branches = ["feature/login", "PR-123", "weird$ref", "feature/" * 40, "x" * 400]
    bad = 0
    for branch in branches:
        release_change = srv._branch_change_id(branch)                    # what the release path keys on
        for path in ("a.py", "src/" + "q" * 300 + "/deep.py"):
            claim = srv._branch_claim_id(branch, path)                    # what the push path inserts
            derived = _change_of(claim)                                   # what the gate splits back out
            if derived != release_change:
                bad += 1
                print(f"     reconcile drift branch={branch[:20]!r}: release={release_change[:30]!r} != claim-derived={derived[:30]!r}")
    check(bad == 0, "push-time claim_id derives the SAME change_id the release path uses, for every branch+path")


def test_idempotent_for_fitting_keys():
    """Every claim_id that already fits the cap is UNCHANGED by the fix — so existing live claims keep their
    identity (idempotency / reopen-recycle preserved); only the over-cap minority is rewritten."""
    samples = [("PR-5", "src/a.py"), ("BR-feature/x", "pkg/mod.py"), ("PR-42", "a/b/c/d.py")]
    ok = all(_bounded_claim_id(c, p) == f"{c}:{p}" for c, p in samples)
    check(ok, "fitting (in-cap) keys keep the exact natural '<change_id>:<path>' form (no churn)")


def main() -> int:
    print("=== ADVERSARIAL-NAMES KEY-FORGERY / COLLISION gate ===")
    print("-- injective + bounded across the adversarial name set --")
    test_injective_and_bounded()
    print("-- the long-sibling truncation collision (the hole) is closed --")
    test_long_siblings_dont_collide()
    print("-- change_id is stable across every path of a change (gate splits on first ':') --")
    test_change_id_prefix_stable_across_paths()
    print("-- no cross-namespace (BR-/PR-) forgery --")
    test_no_cross_namespace_forgery()
    print("-- push->PR reconciliation keys agree for every (branch, path) --")
    test_push_pr_reconciliation_change_id_matches()
    print("-- in-cap keys are unchanged (idempotency preserved) --")
    test_idempotent_for_fitting_keys()
    print("------------------------------------------------------------")
    if FAIL == 0:
        print("ADVERSARIAL CLAIM-ID KEYS GATE: PASS")
        return 0
    print("ADVERSARIAL CLAIM-ID KEYS GATE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
