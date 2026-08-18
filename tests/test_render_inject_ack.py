#!/usr/bin/env python3
"""RENDER INJECT/ACK GATE — three VERIFIED customer-surface render-correctness bugs in github-app/render.py.

All three are RENDER-only (no verdict/engine change), content-free, never-crash, and proven PURE/offline on the
REAL render functions (render_pr_check / apply_pause_ack / the standalone check+comment bodies — stateless over a
main_impact_surface dict; no Postgres, no network). Each section reproduces the bug shape, then asserts the fix.

  (A) HTML/MARKDOWN INJECTION via the BRANCH NAME. git check-ref-format ACCEPTS `<`, `>`, `&`, backtick, etc., so
      a repo whose default branch is e.g.  main`</sub><b>OWNED</b><sub>`x  could, when interpolated RAW into the
      inline-code markdown ( `{branch}` ), CLOSE the one-backtick code span and emit raw HTML (GitHub renders HTML
      in PR comments). Every OTHER customer string was sanitized via _code()/_safe(); the branch slipped through at
      five sites (watching_check, quota_paused_comment_body, cleared_comment_body x2 spans, and the no-reservation
      check title+summary). FIX: wrap branch in _code() (code context) / _safe() (title), at ALL five sites. We
      verify _code(evil) is a balanced backtick fence whose inner backtick runs are strictly shorter (CommonMark:
      it cannot break out), and that the angle-bracket payload never appears OUTSIDE a fenced code span in any body.

  (B) STALE "Wait in line" HEADLINE on an ACKNOWLEDGED PR. The acknowledged branch of apply_pause_ack kept the
      base render's bold headline (base_lines[1] = "⏸ Wait in line — a direct collision is ahead" for a serialize
      coupling) at the TOP and only appended "✓ Acknowledged" at the BOTTOM — so an acked PR's comment LED with the
      pause headline while the check had flipped to neutral/Acknowledged = self-contradictory. FIX: rewrite the
      headline line to an acknowledged headline; the rest of the body (land order / collision detail) still stands.

  (C) ACK-EVASION via a FAILED prior-read. On the unreadable_prior fail-safe (the prior-comment read THREW this
      event → prior_hash=None, prior_confirmed=False), the marker stamped the NEW (possibly-CHANGED) snapshot into
      the comment → the NEXT event saw prior_hash == current snapshot → proven_match → "acknowledged", for a
      coupling that was NEVER re-acked. FIX: on the kept-but-unconfirmed case embed the PRIOR hash ('' when the read
      failed), so the next event re-verifies (re-pauses) the changed coupling. The legitimate empty_recompute keep
      (snapshot recomputed empty, prior_hash present) must STILL preserve the prior hash — not regress.

Run:  python3 tests/test_render_inject_ack.py   (PURE / offline — no Postgres, no network)
"""
import os
import re
import sys

sys.path.insert(0, os.path.join("github-app"))
import render as R

FAIL = 0
EVIL = "main`</sub><b>OWNED</b><sub>`x"   # a git-legal branch name that tries to close a code span + inject HTML


def check(cond, label):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _code_is_inert(span: str) -> bool:
    """A _code() span cannot break out iff it is wrapped by a BALANCED backtick fence of length N>=1 whose every
    INTERNAL backtick run is strictly < N (CommonMark: only an N-run closes an N-fence, so all inner content —
    incl. any `<b>` — is literal code GitHub HTML-escapes). This is the precise inertness invariant."""
    lead = len(span) - len(span.lstrip("`"))
    trail = len(span) - len(span.rstrip("`"))
    inner = span[lead:len(span) - trail]
    longest_inner_run = max((len(r) for r in re.findall(r"`+", inner)), default=0)
    return lead >= 1 and lead == trail and longest_inner_run < lead


def _html_leaks_raw(body: str) -> bool:
    """Does the injection payload appear OUTSIDE a fenced code span (i.e. as raw HTML GitHub would render)? Strip
    every double-or-more-backtick fenced span (what _code emits for a value carrying a backtick), then look for the
    raw angle-bracket payload. Inside a code span GitHub escapes it; only a leak OUTSIDE a span is the bug."""
    stripped = re.sub(r"``+.*?``+", "", body, flags=re.DOTALL)
    return ("<b>OWNED</b>" in stripped) or ("</sub><b>" in stripped)


def _mk_serialize_impact(partner_label: str, path: str) -> dict:
    """A serialize PR (PR-1) in a real in-flight collision with `partner_label` on `path` — a MATERIAL coupling
    (is_material_coupling True) whose snapshot is driven by the partner ref + the path (so changing either yields a
    DIFFERENT snapshot = a materially-changed coupling)."""
    return {"repo": "acme/app", "branch": "main", "changes": [
        {"change_id": "PR-1", "agent": "alice", "label": "alice PR-1", "verdict": "serialize", "paths": [path],
         "behind": [partner_label], "serialize_behind": [{"label": partner_label}],
         "collision_points": [{"path": path}]},
        {"change_id": partner_label, "agent": "x", "label": partner_label, "verdict": "serialize", "paths": [path]},
    ]}


# ============================================================================================================
# (A) — the branch name cannot inject HTML / break a code span at ANY of the five affected sites
# ============================================================================================================
print("(A) HTML/markdown injection via the branch name is neutralized at every affected site:")
check(_code_is_inert(R._code(EVIL)),
      "_code(evil-branch) is a balanced backtick fence whose inner runs are shorter — it cannot break out")
# a benign branch must still render byte-identically (the fix must not garble the common case):
check(R._code("main") == "`main`", "a normal branch ('main') still renders as a plain inline-code span")

for name, body in [
    ("watching_check.summary", R.watching_check(branch=EVIL)["summary"]),
    ("quota_paused_comment_body", R.quota_paused_comment_body(branch=EVIL)),
    ("cleared_comment_body", R.cleared_comment_body(branch=EVIL)),
]:
    check(not _html_leaks_raw(body), f"{name}: the branch payload never escapes a code span as raw HTML")

# the no-reservation path (me is None) — both the check TITLE (non-code context → _safe) and the summary (_code):
imp_none = {"repo": "r", "branch": EVIL, "changes": [{"change_id": "PR-9", "agent": "z", "label": "z"}]}
mn = R.render_pr_check(imp_none, "PR-1")   # change_ref absent from changes → the me-is-None branch
check(not _html_leaks_raw(mn["title"]), "no-reservation check TITLE: the branch payload does not surface as raw HTML")
check("&lt;" in mn["title"], "no-reservation check TITLE: the angle brackets are HTML-escaped (_safe)")
check(not _html_leaks_raw(mn["summary"]), "no-reservation check SUMMARY: the branch payload stays inside a code span")
# a benign branch in the title is still readable, unescaped punctuation-free:
check("Veripsa — heading to main" in R.render_pr_check(
    {"repo": "r", "branch": "main", "changes": [{"change_id": "PR-9", "agent": "z", "label": "z"}]}, "PR-1")["title"],
    "a normal branch still reads cleanly in the no-reservation title")


# ============================================================================================================
# (B) — an ACKNOWLEDGED serialize PR's comment does NOT lead with "Wait in line"
# ============================================================================================================
print("(B) an acknowledged serialize PR's comment leads with an Acknowledged headline, not 'Wait in line':")
imp = _mk_serialize_impact("bob PR-2", "src/core.py")
snap = R.coupling_snapshot(imp["changes"][0])
rendered = R.render_pr_check(imp, "PR-1")
base_first_bold = next((l for l in rendered["comment"].split("\n") if l.strip().startswith("**")), "")
check("Wait in line" in base_first_bold,
      "precondition: the BASE (un-acked) serialize comment really leads with 'Wait in line' (the bug's source)")

acked = R.apply_pause_ack(rendered, imp, "PR-1", label_present=True, prior_hash=snap)
check(acked["ack_state"] == "acknowledged" and acked["conclusion"] == "neutral",
      "precondition: the PR is in the ACKNOWLEDGED state (neutral) — the fix's context")
acked_lines = acked["comment"].split("\n")
first_bold = next((l for l in acked_lines if l.strip().startswith("**")), "")
check("Wait in line" not in first_bold,
      "the acked comment's FIRST bold line is NOT 'Wait in line': " + repr(first_bold))
check("acknowledged" in first_bold.lower(),
      "the acked comment's first bold line reads as an acknowledged headline: " + repr(first_bold))
check(any("✓ Acknowledged — you recorded that you saw this coupling snapshot." in l for l in acked_lines),
      "the acknowledgement note (the > blockquote) is still present (the ack record is not lost)")
# ACK-IS-NOT-A-PARDON honesty (Core risk B, audit 2026-06-25): the ack copy must NEVER read as "Veripsa approved
# this PR" / "Veripsa says it is safe to merge". The honest framing is: you (the author/agent) recorded that you
# saw THIS coupling snapshot and chose to proceed deliberately; Veripsa does not approve, pardon, or sign off,
# and branch protection still decides what merges.
ack_blob = acked["comment"] or ""
check("NOT an approval" in ack_blob and "pardon" in ack_blob,
      "the acked comment EXPLICITLY says the ack is NOT an approval or pardon (Core risk B — ACK is not a pardon)")
check("proceeding past" not in ack_blob.lower(),
      "the acked comment does NOT use 'proceeding past' (reads as Veripsa-let-you-through, not as your-deliberate-choice)")
check("turns the check green" not in ack_blob.lower(),
      "the acked comment does NOT say the ack 'turns the check green' (reads as Veripsa-approved, not records-only)")
# the COLLISION DETAIL below the headline still stands — only the HEADLINE line (index 1) was rewritten, so the
# reserves line + the "Land in order." serialize body block (the genuinely-useful detail) are KEPT:
check("This PR reserves:" in acked["comment"] and "Land in order." in acked["comment"],
      "the collision detail (reserves + 'Land in order.' body) is KEPT below the new headline — only line 1 changed")
# and the stale pause LEAD ("⏸ Wait in line ...") that the headline used to be is gone from the acked comment:
check("⏸ Wait in line" not in acked["comment"],
      "the stale '⏸ Wait in line' pause lead is gone from the acknowledged comment (it was the headline)")


# ============================================================================================================
# (C) — ack-evasion via a failed prior-read on a CHANGED coupling is closed (next event re-pauses)
# ============================================================================================================
print("(C) a changed coupling whose prior read FAILED is not auto-acknowledged on the next event:")
imp0 = _mk_serialize_impact("bob PR-2", "src/core.py")
imp1 = _mk_serialize_impact("carol PR-9", "src/auth.py")   # a DIFFERENT partner AND file = changed coupling
snap0 = R.coupling_snapshot(imp0["changes"][0])
snap1 = R.coupling_snapshot(imp1["changes"][0])
check(bool(snap0) and bool(snap1) and snap0 != snap1,
      "precondition: the coupling materially changed (snap0 != snap1, both real)")

r1 = R.render_pr_check(imp1, "PR-1")
# event1: label still present, coupling changed, prior-comment read THREW this event (prior_hash=None, unconfirmed):
e1 = R.apply_pause_ack(r1, imp1, "PR-1", label_present=True, prior_hash=None, prior_confirmed=False)
check(e1["snapshot"] != snap1,
      "event1 (failed prior read) does NOT stamp the NEW snapshot into the comment (no evasion seed): "
      + repr(e1["snapshot"]))
check(e1["snapshot"] == "",
      "event1 embeds '' (unconfirmed binding), forcing the next event to re-verify")
# event2: the prior read now succeeds and returns whatever event1 embedded; same (changed) coupling, label still on:
e2 = R.apply_pause_ack(r1, imp1, "PR-1", label_present=True, prior_hash=e1["snapshot"], prior_confirmed=True)
check(e2["ack_state"] != "acknowledged",
      "event2 is NOT auto-acknowledged for the never-re-acked changed coupling (ack-evasion closed): "
      + e2["ack_state"])
check(e2["ack_state"] in ("paused", "stale_reack") and e2["conclusion"] == "action_required",
      "event2 re-pauses (paused/stale_reack → action_required) so the changed coupling must be re-acknowledged")


# ============================================================================================================
# (C) regressions — the fail-safe stickiness and the normal paths must NOT change
# ============================================================================================================
print("(C) regressions: legitimate keep / proven-match / fresh-pause / proven-stale paths are unchanged:")
# empty_recompute_keep: snapshot recomputes EMPTY (no refs/paths) but prior_hash present → KEEP ack, preserve hash.
imp_empty = {"repo": "r", "branch": "main",
             "changes": [{"change_id": "PR-1", "agent": "a", "label": "a", "verdict": "serialize"}]}
me_empty = imp_empty["changes"][0]
check(R.is_material_coupling(me_empty) and R.coupling_snapshot(me_empty) == "",
      "precondition: a material serialize with no refs/paths recomputes an EMPTY snapshot")
ae = R.apply_pause_ack(R.render_pr_check(imp_empty, "PR-1"), imp_empty, "PR-1",
                       label_present=True, prior_hash="abc123def456", prior_confirmed=True)
check(ae["ack_state"] == "acknowledged" and ae["snapshot"] == "abc123def456",
      "empty-recompute STILL keeps the ack AND preserves the prior hash (not erased to '') — no regression")

# normal proven_match: label + prior == current snapshot → acknowledged, embeds the (matching) snapshot.
an = R.apply_pause_ack(R.render_pr_check(imp0, "PR-1"), imp0, "PR-1",
                       label_present=True, prior_hash=snap0, prior_confirmed=True)
check(an["ack_state"] == "acknowledged" and an["snapshot"] == snap0,
      "normal proven-match is acknowledged and embeds the snapshot — no regression")

# fresh pause (no label, real coupling, no prior) → paused, BINDS the snapshot so a later ack can match it.
ap = R.apply_pause_ack(R.render_pr_check(imp0, "PR-1"), imp0, "PR-1",
                       label_present=False, prior_hash=None, prior_confirmed=True)
check(ap["ack_state"] == "paused" and ap["snapshot"] == snap0,
      "a fresh pause still BINDS the current snapshot (so a subsequent ack can prove-match) — no regression")

# proven_stale: label + both hashes real + different → strip + re-ack.
aps = R.apply_pause_ack(R.render_pr_check(imp1, "PR-1"), imp1, "PR-1",
                        label_present=True, prior_hash=snap0, prior_confirmed=True)
check(aps["ack_state"] == "stale_reack" and aps["label_action"] == "remove",
      "a proven-stale (both hashes real + different) still strips the label and asks to re-ack — no regression")


print("RENDER INJECT/ACK GATE:", "FAIL" if FAIL else "PASS")
sys.exit(FAIL)
