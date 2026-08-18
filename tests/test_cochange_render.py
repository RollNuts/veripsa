#!/usr/bin/env python3
"""CO-CHANGE RENDER gate — the empirical-coupling advisory line in the PR comment, with the locked principles.

Proves on the REAL render_pr_check (pure, offline):
  (1) a co-change partner is rendered as an ADDITIVE advisory line — the base-rate-corrected LIFT
      ("4.5× more than chance") is the customer-meaningful signal; a CLEAR PR carrying a co-change signal still
      gets the comment (co-signal), like shared_foundation. The RAW SUPPORT COUNT ("N of M changes") is NOT
      rendered: it is a raw graph co-occurrence count (the moat class the PO banned — "raw counts はダメ; coverage
      % OK"); gate 184 locks the absence. The LITERAL "**N%** of the time" probability is ALSO NOT rendered: it
      saturates at strong couplings (printing "**100%** of the time" — a literal customer-facing guarantee).
      The GitHub App must keep the same honest-copy boundary;
  (2) it is a JUDGMENT-NOT-VERDICT ("Advisory", "Check whether") and NEVER claims "0% / not coupled / safe" — a
      no-history file is "not evaluated", not "no coupling" (unknown ≠ clear);
  (3) a FORK PR NEVER shows co-change (the partner files are BASE-REPO paths the external contributor must not
      see — the is_fork redaction returns before the co-change section);
  (4) CONTENT-FREE (paths + lift only — NO raw co-occurrence count, NO literal "N%").

Run:  python3 tests/test_cochange_render.py
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


CC = [{"edited": "backend/auth.py", "partner": "backend/api.py", "prob": 0.73, "lift": 4.5, "co": 11, "n": 15}]


def main() -> int:
    clear = {"repo": "acme/app", "branch": "main",
             "changes": [{"change_id": "PR-1", "label": "PR-1", "agent": "dev", "verdict": "clear",
                          "paths": ["backend/auth.py"]}]}
    out = R.render_pr_check(clear, "PR-1", cochange=CC)
    body = out.get("comment") or ""
    # (1) additive line with the partner + lift; a clear PR with co-change still comments. The raw support count
    # ("11 of 15") is intentionally ABSENT (moat rule "raw counts はダメ"). The literal "N%" probability is ALSO
    # absent now (honest-copy rule — at strong couplings it saturated as "**100%** of the time", a guarantee in
    # customer copy). The lift "× more than chance" is the
    # interpretable signal we keep.
    chk(out.get("comment") is not None, "a CLEAR PR carrying a co-change signal still gets a comment (co-signal)")
    chk("backend/api.py" in body and "4.5×" in body,
        "co-change line shows the partner + lift (4.5×) — the interpretable, uncapped signal")
    chk("11 of 15" not in body and "of 15 changes" not in body,
        "co-change line does NOT leak the raw support count ('11 of 15' / 'N of M changes') — moat rule")
    # honest-copy rule — NO literal "N%" of any value, NOT just 0%/100%. The whole "N% of the time" segment is gone.
    import re as _re
    chk(_re.search(r"\b\d+%", body) is None and "of the time" not in body,
        "co-change line does NOT print a literal 'N%' / 'N% of the time' (honest-copy — saturates as '100%' at strong couplings)")
    # (2) judgment-not-verdict + the unknown≠clear framing; never a "0% / no coupling / safe" claim.
    chk("Advisory" in body and "Check whether" in body, "framed as advisory judgment, not a verdict")
    chk("not evaluated" in body and "no coupling" in body, "explicit 'not evaluated, NOT no-coupling' (unknown ≠ clear)")
    chk("0%" not in body and "no coupling — safe" not in body.lower(),
        "NEVER claims 0% / not-coupled-so-safe (additive only)")
    # the green check itself stays 'clear' (co-change is a comment-only co-signal, not a verdict).
    chk(out.get("conclusion") == "success", "the co-change signal does NOT change the verdict (still 'clear'/success)")

    # (3) FORK: a serialize fork PR posts a REDACTED comment with NO co-change (base-repo paths must not leak).
    fork = {"repo": "acme/app", "branch": "main",
            "changes": [{"change_id": "PR-9", "label": "PR-9", "agent": "ext", "verdict": "serialize",
                         "paths": ["backend/auth.py"], "serialize_behind": [{"change_id": "PR-1", "agent": "maint"}]}]}
    fout = R.render_pr_check(fork, "PR-9", is_fork=True, cochange=CC)
    fbody = fout.get("comment") or ""
    chk(fbody and "backend/api.py" not in fbody and "Historically changes together" not in fbody,
        "FORK PR: co-change is NOT rendered (base-repo partner paths redacted) — got a comment but no co-change")

    # (4) HONESTY (audit r5): a CLEAR PR carrying ONLY a co-change signal must NOT print the absolute
    # "nothing else in flight touches this" header above a co-change line — that flatly contradicts the line.
    # co-change is a co-signal → the non-absolute "Clear — nothing is blocking you" lead (base signal stays "Clear";
    # "Clear to land" is a forbidden base-signal name).
    chk("nothing else in flight touches this" not in body and "Clear — nothing is blocking you" in body
        and "Clear to land" not in body,
        "a clear PR with ONLY co-change uses the non-absolute header (no 'nothing else touches this' self-contradiction)")

    # (5) DEGENERATE-ROW GUARD (audit r5): a row with prob=0 / a falsy path must be DROPPED — no co-change block
    # at all, no empty `` code spans. The upstream filter (render.py: prob > 0 AND edited AND partner) still gates
    # this even after the literal "%" segment was dropped from the rendered line (2026-06-25).
    degen = R.render_pr_check(clear, "PR-1",
                              cochange=[{"edited": "a.py", "partner": "b.py", "prob": 0, "lift": 1.0, "co": 0, "n": 5},
                                        {"edited": None, "partner": None, "prob": 0.9, "lift": 5.0, "co": 9, "n": 10}])
    dbody = degen.get("comment") or ""
    chk("Historically changes together" not in dbody and "``" not in dbody,
        "a degenerate co-change row (prob=0 / null path) is dropped — no co-change block, no empty `` spans")

    print("CO-CHANGE RENDER GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
