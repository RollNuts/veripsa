#!/usr/bin/env python3
"""DAMPENING-HONEST RENDER GATE (Round-11 audit, 2026-06-26) — surface suppression, name the hub.

THE FEATURE: When hub-dampening suppresses a coupling (to avoid the importer-wall noise on heavily-shared
files) but the coupling IS REAL, the renderer must NOT pretend it is a clean 'clear'. The Round-11 audit
named the four obligations:
  (1) Surface that suppression happened (silence-visible) — render the "suppressed to avoid noise" lead,
      never a silent green check.
  (2) "Treat as unknown, not clear" — the literal phrase the customer reads, so the verdict's honesty is
      explicit (no inference required from the bold lead alone).
  (3) Name the hub file responsible — the customer needs to know WHERE the suppression hit, so they can
      coordinate on the right file.
  (4) NOT pretend it's a clean 'clear' verdict — the render-level conclusion + verdict header must reflect
      the honest 'unknown', not the engine's input 'clear' (a stale read / wire mismatch / corroboration-
      path bug could otherwise leak a silent green).

WHAT THIS GATE LOCKS:
  - SCENARIO A (round-trip baseline): a change with NO dampened_with field → renders 'clear', no comment
    (the standard less-noise policy). The hub file name is absent (there is no hub).
  - SCENARIO B (round-trip dampened): the SAME change shape + ONE uncorroborated dampened_with row naming
    the hub file → renders honest 'unknown' (NOT 'clear'/'success'), comment NAMES the hub file, and the
    customer-facing strings "suppressed to avoid noise" and "treat as unknown, not clear" both appear.
  - SCENARIO C (silence-visible on engine-leaked 'clear'): when an engine row somehow arrives with
    verdict='clear' AND a non-empty dampened_with (uncorroborated), the renderer DOWNGRADES to 'unknown'
    and surfaces the dampening note — this is the exact "silently green on a real-but-suppressed coupling"
    failure mode the audit names. Round-trip: a control row with identical fields MINUS dampened_with stays
    'clear' (so we know the downgrade is driven by the dampened signal, not by something else).
  - CORROBORATED PASS-THROUGH (the AUDIT3 relaxation): when EVERY dampened_with row is corroborated
    (lift>=2 AND co>=3 in upstream), the engine already promotes to 'warn' — the renderer must NOT
    re-downgrade that to 'unknown'. The "two independent signals" copy fires (not the "suppressed to avoid
    noise" copy), preserving the existing precision-safe relaxation.

PURE + OFFLINE: render_pr_check is a stateless function over a main_impact_surface-shaped dict; no
Postgres, no network, no deploy (mirrors gate 24/76/138/189). NEVER-CRASH: a render exception is a failure.

Run:  python3 tests/test_render_dampening_honest.py     (no DB needed)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import render as R   # noqa: E402


BASE = {"repo": "acme/app", "branch": "main"}
HUB = "hub.py"
LEAF = "leaf_0.py"


def _row(verdict, dampened_with=None):
    """The SAME change row across scenarios — only `verdict` and (optionally) `dampened_with` vary, so the
    round-trip assertion isolates the dampening signal as the ONLY driver of the verdict change."""
    row = {"change_id": "PR-1", "label": "dev PR-1", "agent": "dev",
           "verdict": verdict, "paths": [LEAF]}
    if dampened_with is not None:
        row["dampened_with"] = dampened_with
    return {**BASE, "changes": [row]}


def main() -> int:
    checks: list[tuple[str, bool]] = []

    # ─── SCENARIO A — round-trip baseline: no dampened_with → clear, no comment ───────────────────────────
    a = R.render_pr_check(_row("clear"), "PR-1")
    checks.append(("[A baseline] clean clear renders conclusion='success' (no dampening signal → no override)",
                   a.get("conclusion") == "success"))
    checks.append(("[A baseline] clean clear renders title 'Veripsa — Clear' (the canonical clear header)",
                   a.get("title") == "Veripsa — Clear"))
    checks.append(("[A baseline] clean clear posts NO comment (less-noise policy on a truly-clean PR)",
                   a.get("comment") is None))
    checks.append(("[A baseline] the hub file name is ABSENT from the summary (there is no hub here)",
                   HUB not in (a.get("summary") or "")))

    # ─── SCENARIO B — same change + ONE uncorroborated dampened_with row naming the hub ──────────────────
    b = R.render_pr_check(
        _row("unknown",
             dampened_with=[{"by": "GH-hubdev PR-HUB:hub.py", "via_hub": HUB, "corroborated": False}]),
        "PR-1")
    b_comment = b.get("comment") or ""
    checks.append(("[B dampened] verdict promoted to 'neutral' (honest 'unknown' conclusion, NOT 'success')",
                   b.get("conclusion") == "neutral"))
    checks.append(("[B dampened] the verdict header is 'Veripsa — Unknown' — never the silent green 'Clear'",
                   b.get("title", "").startswith("Veripsa — Unknown")
                   or "Unknown" in b.get("title", "")))
    checks.append(("[B dampened] a comment IS posted (silence-visible — never the no-comment less-noise path on a "
                   "real-but-suppressed coupling)",
                   b_comment != "" and b_comment is not None))
    # OBLIGATION 1 — surface suppression happened (the bold lead)
    checks.append(("[B dampened] (1) SURFACE: the 'suppressed to avoid noise' lead is rendered (silence-visible)",
                   "suppressed to avoid noise" in b_comment))
    # OBLIGATION 2 — "treat as unknown, not clear"
    checks.append(("[B dampened] (2) HONESTY: the literal 'treat as unknown, not clear' phrase is rendered",
                   "treat as unknown, not clear" in b_comment.lower()))
    # OBLIGATION 3 — name the hub file
    checks.append((f"[B dampened] (3) NAMED: the hub file responsible ({HUB!r}) is named in the comment",
                   HUB in b_comment))
    # OBLIGATION 4 — NOT a silent 'clear' verdict
    checks.append(("[B dampened] (4) NOT CLEAR: the 'no other in-flight change touches your files' silent-green "
                   "wording is absent (the verdict was downgraded, not pretend-clear)",
                   "no other in-flight change touches your files" not in b_comment))

    # ─── SCENARIO C — engine-leaked 'clear' + dampened_with: the silence-visible downgrade fires ─────────
    # Round-trip control: identical fields MINUS dampened_with → stays 'clear'. With the dampened_with row
    # present → the renderer downgrades to honest 'unknown'. This proves the dampening signal is the SOLE
    # driver of the verdict change (not a side-effect of a different field), so the audit obligation is met
    # not by a global override but by the precise "silence-visible on a suppressed coupling" rule.
    c_control = R.render_pr_check(_row("clear"), "PR-1")   # identical to A — re-render to keep the pair side-by-side
    c_leaked = R.render_pr_check(
        _row("clear",   # engine-leaked clear (stale read / wire mismatch / corroboration-path edge case)
             dampened_with=[{"by": "GH-hubdev PR-HUB:hub.py", "via_hub": HUB, "corroborated": False}]),
        "PR-1")
    c_leaked_comment = c_leaked.get("comment") or ""
    checks.append(("[C round-trip] control (no dampened_with): conclusion stays 'success' — the dampening "
                   "is the ONLY driver of the downgrade in the paired case",
                   c_control.get("conclusion") == "success" and c_control.get("comment") is None))
    checks.append(("[C round-trip] same change + dampened_with: conclusion DOWNGRADED to 'neutral' (engine-"
                   "leaked 'clear' on a suppressed coupling never rides through as silent green)",
                   c_leaked.get("conclusion") == "neutral"))
    checks.append(("[C round-trip] same change + dampened_with: the 'suppressed to avoid noise' lead fires + "
                   "names the hub (the four obligations bind even on an engine-leaked 'clear')",
                   "suppressed to avoid noise" in c_leaked_comment
                   and "treat as unknown, not clear" in c_leaked_comment.lower()
                   and HUB in c_leaked_comment))
    # And the silent-green 'clear' wording is NOT in the leaked-clear render (the downgrade is total, not
    # a both-headers-on-one-comment mish-mash)
    checks.append(("[C round-trip] the silent-green 'no other in-flight change touches your files' wording "
                   "is ABSENT on the leaked 'clear' (verdict header is honest 'Unknown')",
                   "no other in-flight change touches your files" not in c_leaked_comment))

    # ─── CORROBORATED PASS-THROUGH — the AUDIT3 relaxation must STILL fire ───────────────────────────────
    # When EVERY dampened_with row is corroborated (upstream lift>=2 AND co>=3 already promoted to 'warn'),
    # the renderer must NOT re-downgrade to 'unknown' — the "two independent signals" copy fires (not the
    # "suppressed to avoid noise" wording). This locks the precision-safe relaxation against an over-
    # aggressive dampening-honest fix that would flatten the corroborated-warn path back to unknown.
    d = R.render_pr_check(
        _row("warn",
             dampened_with=[{"by": "GH-hubdev PR-HUB:hub.py", "via_hub": HUB, "corroborated": True}]),
        "PR-1")
    d_comment = d.get("comment") or ""
    checks.append(("[D corroborated] verdict STAYS 'warn' — corroborated dampening (the AUDIT3 relaxation) "
                   "rides through; the dampening-honest fix does NOT flatten this",
                   d.get("conclusion") in ("neutral", "action_required", "failure")
                   and "Coordinate before merge" in d_comment),)
    checks.append(("[D corroborated] the 'two independent signals' WHY is rendered (the corroborated copy, "
                   "not the suppressed-to-avoid-noise copy)",
                   "two independent signals" in d_comment
                   and "suppressed to avoid noise" not in d_comment))
    checks.append((f"[D corroborated] the hub file ({HUB!r}) is still named in the corroborated copy",
                   HUB in d_comment))

    # ─── MIXED ROW — one corroborated + one uncorroborated → silence-visible MUST fire ──────────────────
    # If ANY row in dampened_with is uncorroborated AND the engine handed us a non-'unknown' verdict, the
    # downgrade must still fire (an uncorroborated suppressed coupling on a non-unknown verdict is the same
    # silent-green failure mode). The corroborated row would render its own "two independent signals" copy
    # under 'warn' upstream — but a mixed row entering as 'clear' must trip the honest downgrade.
    e = R.render_pr_check(
        _row("clear",
             dampened_with=[
                 {"by": "GH-corrobdev PR-X:other.py", "via_hub": HUB, "corroborated": True},
                 {"by": "GH-hubdev PR-HUB:hub.py", "via_hub": HUB, "corroborated": False},
             ]),
        "PR-1")
    e_comment = e.get("comment") or ""
    checks.append(("[E mixed] mixed dampened_with (1 corroborated + 1 uncorroborated) on engine-'clear' "
                   "DOWNGRADES to 'unknown' (any uncorroborated row trips the silence-visible rule)",
                   e.get("conclusion") == "neutral"
                   and "suppressed to avoid noise" in e_comment
                   and HUB in e_comment))

    # ─── REPORT ─────────────────────────────────────────────────────────────────────────────────────────
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("DAMPENING-HONEST RENDER GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
