#!/usr/bin/env python3
"""PAUSE-ACK gate (一時停止) — the tier that actually CHANGES BEHAVIOR.

WHY (proven, PO): the default advisory conclusion is `neutral` for every non-clear verdict ("never block"). We
proved a `neutral` advisory changes NO agent behavior — a careless agent ignores "wait in line" and merges; a
careful agent re-derives the collision itself and never needed the comment. A signal that changes no behavior has
no value. So for a MATERIAL coupling (a REAL in-flight cross-PR collision) the check is NOT green until someone
EXPLICITLY acknowledges THIS specific coupling (the `veripsa-ack` label). It is NOT a blunt block — you proceed by
acknowledging (a conscious, RECORDED stop-and-engage), then it clears. STATELESS: the ack state is the GitHub
LABEL + the content-free coupling-snapshot HASH embedded in Veripsa's OWN prior comment (no DB, no DDL).

Proves on the REAL render.apply_pause_ack (pure, offline — NO GitHub, NO DB) the FULL behavior-change lifecycle:
  (1) MATERIAL coupling (serialize), NO ack → conclusion `action_required` + the content-free snapshot marker
      embedded in the comment + the proceed-by-ack instruction (enable, not only stop).
  (2) + the `veripsa-ack` label, prior-hash == current → conclusion `neutral` ("acknowledged"; recorded).
  (3) the coupling MATERIALLY CHANGES (different partner/paths → a DIFFERENT hash), label still present →
      `action_required` AGAIN (re-raised) + the stale ack is detected (label slated for removal) + a
      "re-acknowledge" instruction.
  (4) a SOLO hotspot / split-advice (no in-flight partner) → stays `neutral` (NO false pause — precision: we do
      not pause people who are not actually coupled).
  (5) `clear` → `success` (unchanged — a clean PR is never paused).
Also: content-free (the snapshot hashes ONLY change refs + paths/symbols Veripsa already surfaces — never a body),
and the snapshot is STABLE (same coupling → same hash across a re-render; a changed coupling → a different hash).

Run:  python3 tests/test_pause_ack.py
"""
from __future__ import annotations
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import render as R   # noqa: E402

FAIL = 0


def chk(c, label):
    global FAIL
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    if not c:
        FAIL = 1


def _marker(snap: str) -> str:
    return f"<!-- veripsa-ack-snap:{snap} -->"


# A material direct collision: this PR is queued behind PR-3 on a real source file/symbol.
SERIALIZE = {"repo": "acme/app", "branch": "main", "changes": [
    {"change_id": "PR-9", "label": "PR-9", "agent": "alice", "verdict": "serialize",
     "paths": ["svc/auth.py"], "serialize_behind": ["maint PR-3"],
     "collision_points": [{"symbol": "login", "path": "svc/auth.py"}]},
]}
# The SAME PR-9 but the coupling MATERIALLY CHANGED — a different partner (PR-77) on a different file/symbol.
SERIALIZE_CHANGED = {"repo": "acme/app", "branch": "main", "changes": [
    {"change_id": "PR-9", "label": "PR-9", "agent": "alice", "verdict": "serialize",
     "paths": ["svc/billing.py"], "serialize_behind": ["bob PR-77"],
     "collision_points": [{"symbol": "charge", "path": "svc/billing.py"}]},
]}


def main() -> int:
    print("PAUSE-ACK gate — the behavior-change lifecycle on the real render.apply_pause_ack")

    # ── (1) MATERIAL coupling, NO ack → action_required + snapshot marker + proceed-by-ack copy ──────────────
    base = R.render_pr_check(SERIALIZE, "PR-9")
    chk(base["conclusion"] == "neutral", "(setup) the base advisory render of a serialize is 'neutral' (never-block default)")
    paused = R.apply_pause_ack(base, SERIALIZE, "PR-9", label_present=False, prior_hash=None, branch="main")
    snap = paused["snapshot"]
    chk(paused["conclusion"] == "action_required",
        "(1) MATERIAL coupling + NO ack → conclusion 'action_required' (the pause that changes behavior)")
    chk(paused["ack_state"] == "paused", "(1) ack_state is 'paused'")
    chk(bool(snap) and _marker(snap) in (paused["comment"] or ""),
        "(1) the content-free snapshot marker is embedded in the posted comment")
    body = paused["comment"] or ""
    chk("veripsa-ack" in body and ("add the" in body or "add-label" in body),
        "(1) the comment shows the PROCEED-BY-ACK path (enable, not only stop — not a wall)")
    chk("never blocks by itself" in body or "does not assert correctness" in body,
        "(1) honest framing — advisory / records-not-correctness / your branch-protection decides")

    # ── (2) + the ack label, prior-hash == current snapshot → neutral (acknowledged, recorded) ────────────────
    acked = R.apply_pause_ack(base, SERIALIZE, "PR-9", label_present=True, prior_hash=snap, branch="main")
    chk(acked["conclusion"] == "neutral",
        "(2) label present + prior-hash == current → conclusion 'neutral' (acknowledged — proceed past a recorded coupling)")
    chk(acked["ack_state"] == "acknowledged", "(2) ack_state is 'acknowledged'")
    chk("Acknowledged" in (acked["title"] or ""), "(2) the check title reads as acknowledged")
    chk(acked["label_action"] is None, "(2) a FRESH ack does NOT remove the label")
    # a label present but with NO prior hash (the author labelled before any Veripsa comment / a pre-tier comment)
    # is NOT a valid ack — the pause must stand (fail-safe: an ack is only valid bound to a snapshot).
    no_prior = R.apply_pause_ack(base, SERIALIZE, "PR-9", label_present=True, prior_hash=None, branch="main")
    chk(no_prior["conclusion"] == "action_required",
        "(2b) label present but NO prior snapshot hash → still 'action_required' (an ack must bind to a snapshot)")

    # ── (3) coupling MATERIALLY CHANGES → re-raised + stale ack detected (label slated for removal) ───────────
    changed_base = R.render_pr_check(SERIALIZE_CHANGED, "PR-9")
    stale = R.apply_pause_ack(changed_base, SERIALIZE_CHANGED, "PR-9", label_present=True, prior_hash=snap, branch="main")
    chk(stale["snapshot"] != snap, "(3) the coupling changed → a DIFFERENT snapshot hash (the ack no longer matches)")
    chk(stale["conclusion"] == "action_required",
        "(3) coupling changed, stale label still present → 'action_required' AGAIN (the pause re-raises)")
    chk(stale["ack_state"] == "stale_reack", "(3) ack_state is 'stale_reack' (re-acknowledge required)")
    chk(stale["label_action"] == "remove",
        "(3) the STALE ack label is slated for removal (so the PR visibly reads un-acked)")
    chk("re-add" in (stale["comment"] or "").lower() or "re-acknowledge" in (stale["comment"] or "").lower(),
        "(3) the comment asks the author to RE-acknowledge the new coupling")

    # ── (4) SOLO hotspot / split-advice (no in-flight partner) → stays neutral (NO false pause) ───────────────
    solo = {"repo": "acme/app", "branch": "main", "changes": [
        {"change_id": "PR-5", "label": "PR-5", "agent": "x", "verdict": "warn",
         "paths": ["core/util.py"], "contested_with": [],
         "shared_foundation": [{"path": "core/util.py", "fan_in": 9, "churn": 4}]},
    ]}
    solo_base = R.render_pr_check(solo, "PR-5")
    solo_out = R.apply_pause_ack(solo_base, solo, "PR-5", label_present=False, prior_hash=None, branch="main")
    chk(solo_out["conclusion"] == "neutral",
        "(4) a SOLO hotspot/split notice (no in-flight partner) stays 'neutral' — NO false pause (precision)")
    chk(solo_out["ack_state"] == "not_material", "(4) ack_state is 'not_material' (a notice, never a pause)")
    chk(solo_out["snapshot"] == "" and solo_out["label_action"] is None,
        "(4) a non-material change has no snapshot to bind and never touches a label")
    # an 'unknown' (paths not in main's graph) is likewise NOT a material coupling → never paused.
    unk = {"repo": "acme/app", "branch": "main", "changes": [
        {"change_id": "PR-6", "label": "PR-6", "verdict": "unknown", "paths": ["new.py"], "unknown_paths": ["new.py"]},
    ]}
    unk_out = R.apply_pause_ack(R.render_pr_check(unk, "PR-6"), unk, "PR-6", label_present=False, prior_hash=None, branch="main")
    chk(unk_out["conclusion"] == "neutral" and unk_out["ack_state"] == "not_material",
        "(4b) 'unknown' is not a material coupling → stays 'neutral' (never paused on an un-analyzable PR)")
    # (4c) serialize_soft is the engine's deliberately LOW-STAKES append-order heads-up (e.g. two PRs both
    # appending a gate to run_gates.sh / gates.d) — render makes it 'neutral' ON PURPOSE so it never blocks a
    # trivial git-append ordering. It must NOT be escalated to action_required (that false pause would negate the
    # engine's own precision softening — the modal multi-agent collision).
    soft = {"repo": "acme/app", "branch": "main", "changes": [
        {"change_id": "PR-8", "label": "PR-8", "agent": "y", "verdict": "serialize_soft",
         "paths": ["run_gates.sh"], "serialize_behind": ["maint PR-2"],
         "collision_points": [{"symbol": None, "path": "run_gates.sh"}]},
    ]}
    soft_out = R.apply_pause_ack(R.render_pr_check(soft, "PR-8"), soft, "PR-8",
                                label_present=False, prior_hash=None, branch="main")
    chk(soft_out["conclusion"] != "action_required",
        "(4c) serialize_soft (low-stakes append-order heads-up) is NOT escalated to a pause (no false block on a trivial build/list-file append)")
    chk(soft_out["ack_state"] == "not_material",
        "(4c) serialize_soft ack_state is 'not_material' (a heads-up, never a pause)")
    # (4d) a FORK PR never pauses: a check on a fork's head sha does not stick on the base repo, so action_required
    # would only put a confusing 'paused' banner on a still-mergeable PR; a fork author cannot add the base repo's
    # ack label anyway. A material coupling on a fork → treated as a notice (not_material), conclusion as rendered.
    fork_out = R.apply_pause_ack(base, SERIALIZE, "PR-9", label_present=False, prior_hash=None, branch="main", is_fork=True)
    chk(fork_out["conclusion"] != "action_required" and fork_out["ack_state"] == "not_material",
        "(4d) a FORK PR with a material coupling is NOT paused (no sticking check on a fork → no false 'paused' banner)")

    # ── (5) clear → success (unchanged — a clean PR is never paused) ──────────────────────────────────────────
    clear = {"repo": "acme/app", "branch": "main", "changes": [
        {"change_id": "PR-1", "label": "PR-1", "agent": "d", "verdict": "clear", "paths": ["z.py"]},
    ]}
    clear_base = R.render_pr_check(clear, "PR-1")
    clear_out = R.apply_pause_ack(clear_base, clear, "PR-1", label_present=False, prior_hash=None, branch="main")
    chk(clear_out["conclusion"] == "success",
        "(5) 'clear' → 'success' (unchanged — a clean PR is never paused, even with the ack tier on)")
    chk(clear_out["ack_state"] == "not_material", "(5) ack_state is 'not_material' for a clear PR")
    # even with a stray ack label on a clear PR (someone left it), a clear PR is not material → stays success.
    clear_labeled = R.apply_pause_ack(clear_base, clear, "PR-1", label_present=True, prior_hash="deadbeef0000", branch="main")
    chk(clear_labeled["conclusion"] == "success",
        "(5b) a clear PR with a stray ack label stays 'success' (a label can never CREATE a pause)")

    # ── content-free + snapshot stability ────────────────────────────────────────────────────────────────────
    me9 = SERIALIZE["changes"][0]
    chk(R.coupling_snapshot(me9) == R.coupling_snapshot(me9),
        "(content-free) the snapshot is STABLE — the same coupling yields the same hash across re-renders")
    chk(R.coupling_snapshot(me9) != R.coupling_snapshot(SERIALIZE_CHANGED["changes"][0]),
        "(content-free) a MATERIALLY-different coupling yields a different hash (ack binding is precise)")
    # the snapshot is a 12-hex digest of REFS + PATHS only — never a file body. It must contain only the
    # identifiers the renderer already surfaces. (We can't 'prove a negative' over all bodies, but we assert the
    # hash is a bare hex digest and that the marker carries ONLY that digest — no path/body leaks into the marker.)
    chk(len(snap) == 12 and all(c in "0123456789abcdef" for c in snap),
        "(content-free) the snapshot is a short hex digest (over refs+paths) — never a file body")
    chk("svc/auth.py" not in _marker(snap) and "login" not in _marker(snap),
        "(content-free) the embedded marker carries ONLY the digest — no path/symbol/body leaks into it")

    print("PAUSE ACK GATE: " + ("PASS" if FAIL == 0 else "FAIL"))
    return FAIL


if __name__ == "__main__":
    raise SystemExit(main())
