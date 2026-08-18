#!/usr/bin/env python3
"""PAUSE COMMENT WORDING GATE — the customer-facing PR-comment WORDING the pause-ack tier left self-contradictory.

The pause-and-ack tier (a MATERIAL coupling pauses the check until someone adds the `veripsa-ack` label) shipped,
but the COMMENT copy around it had four real wording defects that made a genuinely-grounded analysis read hollow
on a live prod PR (verbatim from prod PR comments). This gate pins the WORDING fix on the REAL render_pr_check /
apply_pause_ack output (WORDING/RENDER only — no verdict or pause-mechanism change). Content-free throughout.

  P1  CONTRADICTION: a PAUSED PR (verdict serialize -> check action_required) showed the pause banner
      ("Veripsa paused this — to proceed add the veripsa-ack label") AND, lower in the SAME comment, the stale
      line "(Advisory — the order is a suggestion, not a block.)". A paused check is NOT "just advisory / not a
      block" — that parenthetical (leftover from the pre-pause-ack era) flatly contradicts the pause. FIX: the
      serialize body block no longer carries that "not a block" / "nothing is blocked" denial; the honest,
      non-contradicting FOOTER ("Advisory by default; your branch-protection policy decides what blocks") stays.

  P2  AUTHOR COLLAPSE in the suggested land order: several PRs by ONE author rendered as "1. example-user / 2.
      example-user / …" — bare-author noise, some entries with NO PR ref at all (a BR- push not yet reconciled to a
      PR renders author-name-only). FIX: every suggested-order entry now carries its content-free PR/branch ref
      (PR-<n>/BR-<x>), recovered from the engine's per-change rows, so a same-author cluster reads as
      "PR-1, PR-2, PR-10, PR-11", never a bare author with no ref.

  P3  CLEAR/HEAD shows a PAUSE banner: the PR at the HEAD of a contention cluster (own verdict clear/success —
      first in line) must NOT render the "Wait in line" / paused banner; its header must read the base signal
      Clear. (Already correct on current main — is_material_coupling() is FALSE for clear, so apply_pause_ack
      leaves it success and the header is "✓ Clear …"; this gate LOCKS that so a future change can't regress a
      clear head into reading as paused. NB "Clear to land" is a forbidden base-signal name — the header is the
      bare base signal "Clear".)

  P4  "Wait in line" appeared TWICE — the bold header AND the body block ("**Wait in line.** …"). FIX: the body
      block leads with the ACTION ("Land in order.") instead of re-stating the header, so the phrase appears once.

Run:  python3 tests/test_pause_comment_wording.py   (PURE / offline — no Postgres, no network)
"""
import os, re, sys
sys.path.insert(0, os.path.join("github-app"))
import render as R

base = {"repo": "acme/app", "branch": "main"}
FAIL = 0


def check(cond, label):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def land_rows(comment):
    return [ln for ln in (comment or "").split("\n") if re.match(r"^\d+\. ", ln)]


# A PAUSED serialize PR (material coupling, no ack label -> action_required), in a same-author cluster whose
# suggested_order MIXES a ref-carrying label ('example-user PR-10') with an author-ONLY label ('example-user', a BR- push
# not yet reconciled to a PR) — exactly the live prod shape.
PAUSED_IMPACT = {**base, "changes": [
    {"change_id": "PR-10", "label": "example-user PR-10", "agent": "example-user", "verdict": "serialize",
     "paths": ["render.py"], "serialize_behind": ["example-user"],
     "collision_points": [{"path": "render.py", "symbol": "render_pr_check"}]},
    {"change_id": "BR-feature-x", "label": "example-user", "agent": "example-user", "verdict": "clear", "paths": ["render.py"]},
    {"change_id": "PR-11", "label": "example-user PR-11", "agent": "example-user", "verdict": "serialize", "paths": ["render.py"]},
  ],
  "clusters": [{"changes": ["BR-feature-x", "PR-10", "PR-11"], "size": 3,
                "suggested_order": ["example-user", "example-user PR-10", "example-user PR-11"]}],
}


def paused_comment():
    out = R.render_pr_check(PAUSED_IMPACT, "PR-10")
    out = R.apply_pause_ack(out, PAUSED_IMPACT, "PR-10", label_present=False, prior_hash=None)
    return out


# ===== P1 — a PAUSED PR's comment does not contradict the pause with an "advisory ... not a block" line =====
print("P1 — paused PR does not contradict its pause with an 'advisory ... not a block' line:")
o = paused_comment()
c = o["comment"] or ""
check(o["conclusion"] == "action_required" and o.get("ack_state") == "paused",
      "the test PR is actually in the PAUSE state (action_required / paused) — the precondition for P1")
check("⏸ Veripsa paused this" in c, "the pause banner ('Veripsa paused this …') is present")
# the SPECIFIC stale contradictions of the pause — must be gone from the body:
check("the order is a suggestion, not a block" not in c,
      "the body does NOT carry '(Advisory — the order is a suggestion, not a block.)' (it contradicts the pause)")
check("nothing is blocked" not in c,
      "the body does NOT carry 'Advisory — nothing is blocked.' (it contradicts the pause)")
check("never a block" not in c,
      "the body does NOT carry a 'never a block' parenthetical (it contradicts the pause)")
# the honest, non-contradicting FOOTER must STAY (it is TRUE and must remain):
check("your branch-protection policy decides what blocks" in c
      or "your branch-protection decides what blocks" in c,
      "the honest footer ('your branch-protection … decides what blocks') is KEPT")


# ===== P2 — every suggested-order entry carries a PR/branch ref (no bare-author entry) =====
print("P2 — every suggested-order entry carries a PR/branch ref:")
rows = land_rows(c)
check(len(rows) == 3, "the land order rendered all 3 cluster entries")
bare = [ln for ln in rows if not re.search(r"PR-\d+|BR-[A-Za-z0-9_./\-]+", ln)]
check(not bare, "NO land-order entry is a bare author with no ref (every row has a PR-/BR- ref): "
      + (str(bare) if bare else "all rows carry a ref"))
# the author-only BR- entry recovered its ref (the collapse fix), and the same-author cluster is now distinguishable:
check(any("BR-feature-x" in ln for ln in rows),
      "the author-only ('example-user') entry recovered its content-free ref (BR-feature-x)")


# ===== P3 — a clear/head PR's header reads clear-to-land, NOT the paused / 'Wait in line' banner =====
print("P3 — a clear cluster-HEAD reads clear-to-land, not paused / wait-in-line:")
HEAD_IMPACT = {**base, "changes": [
    {"change_id": "PR-1", "label": "example-user PR-1", "agent": "example-user", "verdict": "clear", "paths": ["render.py"],
     "queued_behind": ["bob PR-7"], "queued_behind_paths": ["render.py"]},
  ],
  "clusters": [{"changes": ["PR-1", "PR-7"], "size": 2, "suggested_order": ["example-user PR-1", "bob PR-7"]}],
}
ho = R.render_pr_check(HEAD_IMPACT, "PR-1")
ho = R.apply_pause_ack(ho, HEAD_IMPACT, "PR-1", label_present=False, prior_hash=None)
hc = ho["comment"] or ""
header = hc.split("\n")[1] if len(hc.split("\n")) > 1 else ""
check(ho["conclusion"] == "success" and ho.get("ack_state") == "not_material",
      "a clear cluster-head is success / not-material (never paused into action_required)")
check("Wait in line" not in header and "paused" not in header.lower(),
      "the clear-head comment HEADER is not 'Wait in line' / 'paused'")
check(header.startswith("**✓") and "Clear" in header and "Clear to land" not in header,
      "the clear-head HEADER reads the base signal Clear (✓ Clear …), never the forbidden 'Clear to land': " + repr(header))
check("⏸ Veripsa paused this" not in hc, "a clear head shows NO pause banner anywhere in the comment")
# its land-order refs are present too (the P2 fix applies on every cluster comment):
check(not [ln for ln in land_rows(hc) if not re.search(r"PR-\d+|BR-", ln)],
      "the clear-head land order also carries a ref on every entry")


# ===== P4 — the 'Wait in line' idea appears at most once in a paused serialize comment =====
print("P4 — 'Wait in line' appears at most once:")
n = c.count("Wait in line")
check(n <= 1, f"'Wait in line' appears at most once in the paused comment (found {n})")
# and the body now leads with the ACTION, not a duplicate headline:
check("**Land in order.**" in c, "the serialize body block leads with the action ('Land in order.'), not a duplicated 'Wait in line.'")


# ===== P5 — the paused comment has a SINGLE headline + the disclaimer is NOT triplicated =====
# Marketplace-UX pass: (a) the pause banner is the SINGLE lead — the base render's "⏸ Wait in line" verdict
# headline is dropped so the comment no longer stacks two near-identical ⏸ bold lines back-to-back; (b) the
# advisory / "does not assert correctness" / branch-protection disclaimer lives ONLY in the <sub> footer (it
# used to appear ~3× in one comment — twice in the banner, once in the footer).
print("P5 — the paused comment leads with a SINGLE headline and does not triplicate the disclaimer:")
_lines = c.split("\n")
# SIGNAL CONTRACT: `Wait in line` is the BASE SIGNAL and Paused is an acknowledgement OVERLAY on top of it, so
# the base-signal headline MUST stay — erasing it would drop the base signal from the comment entirely (and it is
# what the storm/idempotency gates key on). The de-duplication the UX audit asked for is achieved in the BANNER,
# which no longer restates the verdict: it states only why it paused and how to proceed.
_first_bold = next((ln for ln in _lines if ln.lstrip().startswith("**") or ln.lstrip().startswith("> **")), "")
check("⏸ Wait in line" in _first_bold,
      "the FIRST bold lead is the BASE SIGNAL headline ('⏸ Wait in line …'): " + repr(_first_bold))
check(c.count("⏸ Veripsa paused this") == 1,
      f"the pause banner headline appears exactly once (found {c.count('⏸ Veripsa paused this')})")
check(c.count("⏸ Wait in line") == 1,
      f"the base-signal headline appears exactly once — banner does not restate it (found {c.count('⏸ Wait in line')})")
# no double blank line before the reserves block (#6):
check("\n\n\nThis PR reserves:" not in c,
      "there is no stray double blank line before 'This PR reserves:'")
# the disclaimer is NOT triplicated — it survives ONLY in the footer:
check(c.count("does not assert correctness") == 1,
      f"'does not assert correctness' appears exactly once — the <sub> footer only (found {c.count('does not assert correctness')})")
check(c.count("branch-protection policy") == 1,
      f"'branch-protection policy' appears exactly once — the footer only (found {c.count('branch-protection policy')})")
check("neutral by default" not in c and "is the enforcer" not in c,
      "the banner's duplicated 'neutral by default / … is the enforcer' disclaimer is gone (footer subsumes it)")
check("records who acknowledged" not in c,
      "the banner's duplicated 'records who acknowledged … does not assert correctness' sentence is gone (footer subsumes it)")
# the honest footer and the proceed-by-ack path both survive:
check("your branch-protection policy decides what blocks" in c,
      "the honest <sub> footer disclaimer is KEPT (the single place it now lives)")
check("not an approval or a pardon" in c,
      "the banner keeps the one records-not-a-pardon clause (ACK is not an approval/pardon)")


print("PAUSE COMMENT WORDING GATE:", "FAIL" if FAIL else "PASS")
sys.exit(FAIL)
