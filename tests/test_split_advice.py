#!/usr/bin/env python3
"""SPLIT-ADVICE gate — Veripsa PROACTIVELY recommends splitting a chronic contention file (PO 2026-06-18:
"Veripsaに分割を勧めさせる"). core.split_candidates / the per-PR `shared_foundation` field (the SAME fan_in×churn
gate) identifies a load-bearing, frequently-moving file the PR touches. This gate locks the CUSTOMER SURFACE:
that signal is rendered as an honest, advisory "consider splitting" recommendation — and nothing more.

Locks (pure render over a synthetic main_impact_surface payload — no DB):
  (1) FIRES on a touched shared-foundation file: the comment names the file, states its contention shape
      QUALITATIVELY ("imported widely" / "changes often" — NEVER a raw fan-in/churn COUNT; PO 2026-06-21
      「file count はダメ」, the graph-size moat), and recommends splitting it into cohesive modules.
  (2) RELEVANCE-GATED: a PR that touches NO shared-foundation file gets NO split recommendation (no wallpaper).
  (3) ADVISORY: the recommendation NEVER changes the check conclusion — a 'clear' PR carrying the advice keeps
      the exact conclusion it would have without it (Veripsa enables, never blocks).
  (4) CONTENT-FREE: a hostile path (newline / markdown / secret-looking body) is collapsed to a single
      backticked reference — no body leaks and the blockquote is not broken out of.

Run:  python3 tests/test_split_advice.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import render  # noqa: E402

checks = []


def _comment(impact, ref="PR-1"):
    out = render.render_pr_check(impact, ref)
    return (out.get("comment") or ""), out


# ── (1) FIRES: a touched shared-foundation file → an explicit split recommendation naming it, with the
#        contention shape stated QUALITATIVELY (no raw fan-in/churn COUNT — that count is the graph-size moat) ──
imp1 = {"changes": [{"change_id": "PR-1", "verdict": "warn", "paths": ["github-app/server.py"],
                     "shared_foundation": [{"path": "github-app/server.py", "fan_in": 14, "churn": 9}]}]}
c1, _ = _comment(imp1)
lc1 = c1.lower()
# the recommendation fires, names the file, and gives the WHY qualitatively ("imported widely" for a high-fan-in
# foundation; "changes often" for the churn) — and MUST NOT leak the raw counts we fed in (14 / 9). The "9" guard
# is scoped to the WHY clause so an unrelated digit elsewhere (a future PR-9 ref, a line number) can't false-pass.
fires = ("consider splitting" in lc1 and "server.py" in c1
         and "imported widely" in lc1 and "changes often" in lc1
         and "14" not in c1 and "imported by **14" not in c1 and "9 time" not in c1)
checks.append(("(1) a touched shared-foundation file yields a 'consider splitting' recommendation naming the "
               "file + a QUALITATIVE contention shape (imported widely / changes often), with NO raw fan-in (14) "
               "or churn (9) count leaked (MOAT — PO 「file count はダメ」)", fires))

# ── (2) RELEVANCE-GATED: no shared-foundation touched → no split recommendation (no repo-wide nag) ──
imp2 = {"changes": [{"change_id": "PR-1", "verdict": "warn", "paths": ["src/util.py"],
                     "contested_with": [{"by": "bob PR-9"}], "shared_foundation": []}]}
c2, _ = _comment(imp2)
no_nag = "consider splitting" not in c2.lower() and "recurring contention" not in c2.lower()
checks.append(("(2) a PR touching NO shared-foundation file gets NO split recommendation (relevance-gated, "
               "not wallpaper)", no_nag))

# ── (3) ADVISORY: the recommendation never flips the check conclusion. A 'clear' PR with the advice keeps the
#        SAME conclusion it has without it. ──
clear_with = {"changes": [{"change_id": "PR-1", "verdict": "clear", "paths": ["github-app/server.py"],
                           "shared_foundation": [{"path": "github-app/server.py", "fan_in": 14, "churn": 9}]}]}
clear_without = {"changes": [{"change_id": "PR-1", "verdict": "clear", "paths": ["github-app/server.py"],
                              "shared_foundation": []}]}
_, ow = _comment(clear_with)
_, on = _comment(clear_without)
advisory = (ow.get("conclusion") == on.get("conclusion") and ow.get("conclusion") in ("success", "neutral"))
# the advice must still be PRESENT on the clear PR (it is worth a quiet line even when otherwise clear)
present_on_clear = "consider splitting" in (ow.get("comment") or "").lower()
checks.append(("(3) ADVISORY: a 'clear' PR carrying the split advice keeps the identical (non-failing) "
               f"conclusion as without it (got with={ow.get('conclusion')} without={on.get('conclusion')}) "
               "and still surfaces the advice", advisory and present_on_clear))

# ── (4) CONTENT-FREE: a hostile path is collapsed to a single backticked reference — no body / secret line
#        breaks out of the blockquote. ──
SECRET = "BODY_LEAK_4f9q2"  # gitleaks:allow -- deliberate invalid renderer sentinel
poison = f"app.py\n```\n{SECRET}\n> injected"
imp4 = {"changes": [{"change_id": "PR-1", "verdict": "warn", "paths": ["app.py"],
                     "shared_foundation": [{"path": poison, "fan_in": 7, "churn": 4}]}]}
c4, _ = _comment(imp4)
# the secret must NEVER appear at the START of a line (that would mean the newline in the path broke out of the
# code span and leaked the "body"); a sanitized path keeps the whole reference on the single blockquote line.
leaked = any(line.lstrip().startswith(SECRET) or line.lstrip().startswith("> injected") for line in c4.split("\n"))
checks.append(("(4) CONTENT-FREE: a path carrying a newline + secret-looking body is collapsed to a single "
               "reference — neither the secret body nor an injected blockquote line breaks out", not leaked))

# ── verdict ──
ok = all(p for _, p in checks)
for desc, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {desc}")
print("SPLIT-ADVICE GATE: PASS" if ok else "SPLIT-ADVICE GATE: FAIL")
sys.exit(0 if ok else 1)
