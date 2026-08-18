#!/usr/bin/env python3
"""RENDER QUALITY gate — a DENYLIST-SCAN regression lock on the CO-CHANGE customer surface + the mission-named
internal tokens the canonical jargon gate does not yet name.

WHY THIS GATE EXISTS (the coverage gap it closes — measured, not guessed):
  tests/test_no_jargon_leak.py (gate 16) owns the AUTHORITATIVE customer-surface contract: the canonical internal
  DENYLIST (role names / internal fn ids / DB+design terms / the Japanese design prose) + OVERCLAIM_PHRASES
  (records-not-correctness) + the MOAT count invariants. tests/test_fork_surface_honesty.py (gate 128) re-runs
  those canonical scanners over the fork-redacted surface. BUT measuring the ACTUAL renderer output showed two
  surfaces the canonical scan never reaches:

    (A) THE CO-CHANGE LINE. Every render_pr_check call in gate 16 OMITS `cochange=`, so the empirical-coupling
        advisory block ("📊 Historically changes together …") is NEVER scanned through the canonical DENYLIST /
        OVERCLAIM lists. gate 76 (test_cochange_render) checks that block's SPECIFIC honesty phrases, but not the
        full jargon/overclaim contract — so the co-change copy could drift in a raw internal token or an absolute
        guarantee and every gate would stay green. This gate renders the co-change-BEARING output across verdicts
        and runs it through the SAME canonical scanners (single-sourced, never a hand-rolled subset).

    (B) MISSION-NAMED INTERNAL TOKENS not in the canonical denylist. The product brief names a set of internal
        terms that must never reach a customer; a probe confirmed several are NOT in gate 16's DENYLIST today:
        `co_change`, `account_id`, `contention`, `blast_radius`, `plan_file_limit`, `verdict`, plus the engine's
        internal FIELD names that flow through render (`serialize_behind`, `collision_points`, `dampened_with`,
        `fan_in`, `churn`, `unknown_paths`). They do NOT leak today — but nothing keeps it so. CRUCIAL DISTINCTION:
        these are the snake_case / identifier FORMS. The customer-facing PRODUCT VOCABULARY ("blast radius" with a
        space, "main's graph", "co-change"/"changes together") is INTENTIONAL and must NOT be over-blocked, so the
        patterns below match only the underscored identifier token (`\bblast_radius\b` never matches "blast
        radius"; `\bco_change\b` never matches "co-change"). A POSITIVE check asserts the product phrasing stays.

WHAT THIS GATE LOCKS (purely ADDITIVE — widens the lock; it does not change any verdict or extractor behavior):
  (1) The co-change customer surface (clear / warn / serialize that ALSO carry a cochange signal) is clean against
      the CANONICAL jargon + overclaim scanners (imported from gate 16 so the contract is single-sourced).
  (2) An EXTENDED denylist of the mission-named internal identifier tokens is absent from EVERY representative
      customer surface (the co-change line, the standard verdicts, pause-ack, the coverage nudge, watching/quota
      surfaces) — so a FUTURE edit that splices one in FAILS here, naming the exact token.
  (3) The intentional product vocabulary is NOT over-blocked (a positive presence check on "blast radius" /
      "graph" / the co-change phrasing).

PURE + OFFLINE: render_pr_check / apply_pause_ack are stateless functions over a main_impact_surface-shaped dict;
no Postgres, no network, no deploy (mirrors gate 16 / 76 / 128). NEVER-CRASH: a render exception is a failure.

Run:  python3 tests/test_render_quality.py     (no DB needed)
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import render as R                  # noqa: E402
# Single-source the contract: the SAME canonical denylist + overclaim scanners gate 16 uses, so the co-change
# lock can never silently diverge from the authoritative one (add a term there → it guards the co-change surface
# here too). Identical pattern to tests/test_fork_surface_honesty.py.
import test_no_jargon_leak as J     # noqa: E402


BASE = {"repo": "acme/app", "branch": "main"}

# A representative co-change payload (strongest-first, as core emits it): partner path + directional confidence +
# base-rate-corrected lift + raw support. Content-free (paths + a % + counts only).
CC = [{"edited": "backend/auth.py", "partner": "backend/api.py", "prob": 0.73, "lift": 4.5, "co": 11, "n": 15}]


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# EXTENDED DENYLIST — the mission-named internal IDENTIFIER tokens that are NOT in the canonical gate-16 list
# (verified by probe). Each is the underscored / snake_case form; matched on word boundaries so it does NOT
# touch the intentional product phrasing ("blast radius" w/ space, "co-change" w/ hyphen, "main's graph"). This
# is the FRAME: an explicit, bounded, documented set — the permanent guard against a future edit leaking one.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
EXTENDED_DENYLIST: list[str] = [
    # DB-/engine-internal terms the brief names
    r"\bco_change\b",        # the column/term — the customer sees "changes together" / "co-change", never co_change
    r"\baccount_id\b",       # tenant key — never a customer token
    r"\bcontention\b",       # internal verb for the entangled-neighborhood concept (customer: "in-flight work")
    r"\bblast_radius\b",     # the IDENTIFIER — customer phrasing "blast radius" (space) is product vocab, allowed
    r"\bplan_file_limit\b",  # the metering knob — customer sees a % coverage nudge, never the raw limit identifier
    r"\bverdict\b",          # the engine's classification field — customer sees the mapped human title
    # engine-internal FIELD names that flow INTO render_pr_check's input dict (must be CONSUMED, never echoed)
    r"\bserialize_behind\b",
    r"\bcollision_points\b",
    r"\bconflict_points\b",
    r"\bdepends_on_changing\b",
    r"\bdampened_with\b",
    r"\bunknown_paths\b",
    r"\bcontested_with\b",
    r"\bshared_foundation\b",
    r"\bqueued_behind\b",
    r"\bfan_in\b",
    r"\bchurn\b",
    # raw engine verdict-internal spellings (the underscored tier names — the human title must be used instead)
    r"\bserialize_soft\b",
    r"\bnot_material\b",
    r"\baction_required\b",  # the GitHub conclusion enum — an internal mechanism token, not customer prose
]
_EXTENDED_PATTERNS = [(t, re.compile(t, re.IGNORECASE)) for t in EXTENDED_DENYLIST]


def _scan_extended(label: str, text) -> list[str]:
    """Return extended-denylist violations for one customer string (the mission-named internal identifier tokens
    that the canonical list does not yet name). Identifier forms only — product phrasing is never matched."""
    if not text:
        return []
    out = []
    for term, pat in _EXTENDED_PATTERNS:
        m = pat.search(text)
        if m:
            out.append(f"[{label}] leaked internal IDENTIFIER token {term!r} (matched {m.group(0)!r}) in: {text!r}")
    return out


def _cochange_scenarios():
    """Co-change-BEARING engine rows per verdict (the surface gate 16 never scans because it omits cochange=)."""
    clear = {**BASE, "changes": [{"change_id": "PR-1", "label": "dev PR-1", "agent": "dev", "verdict": "clear",
                                  "paths": ["backend/auth.py"]}]}
    warn = {**BASE, "changes": [{"change_id": "PR-2", "label": "bob PR-2", "agent": "bob", "verdict": "warn",
                                 "paths": ["backend/auth.py"], "impact": ["backend/handlers.py"],
                                 "contested_with": ["alice PR-1"]}]}
    serialize = {**BASE, "changes": [{"change_id": "PR-3", "label": "carol PR-3", "agent": "carol",
                                      "verdict": "serialize", "paths": ["backend/auth.py"],
                                      "serialize_behind": ["bob PR-2"],
                                      "collision_points": [{"behind": "bob PR-2", "path": "backend/auth.py",
                                                            "symbol": "login", "line_lo": 5, "line_hi": 20}]}]}
    # a degenerate co-change row alongside a good one (prob 0 / null path) — the renderer must DROP it, and the
    # dropped/kept output must still be clean (this is the path most prone to a stray "0%" or empty `` span).
    cc_mixed = CC + [{"edited": "x.py", "partner": "y.py", "prob": 0, "lift": 1.0, "co": 0, "n": 5},
                     {"edited": None, "partner": None, "prob": 0.9, "lift": 5.0, "co": 9, "n": 10}]
    return [
        ("co-change clear", clear, "PR-1", CC),
        ("co-change warn", warn, "PR-2", CC),
        ("co-change serialize (collision + co-signal)", serialize, "PR-3", CC),
        ("co-change clear (degenerate rows mixed in)", clear, "PR-1", cc_mixed),
    ]


def main() -> int:
    checks: list[tuple[str, bool]] = []
    violations: list[str] = []
    scanned_cochange_comment = False
    all_blobs: list[tuple[str, str]] = []   # (label, text) for the extended-denylist sweep over EVERY surface

    # ── (1) CO-CHANGE surface × the CANONICAL jargon + overclaim scanners (the gap gate 16 never reaches) ─────
    for desc, impact, ref, cc in _cochange_scenarios():
        try:
            out = R.render_pr_check(impact, ref, cochange=cc)
        except Exception as e:               # NEVER-CRASH: a render exception on a valid input is a failure
            checks.append((f"[{desc}] render_pr_check did not crash on a co-change input", False))
            violations.append(f"[{desc}] render_pr_check raised {e!r}")
            continue
        checks.append((f"[{desc}] render_pr_check did not crash on a co-change input", True))
        for label, text in (("title", out.get("title")), ("summary", out.get("summary")),
                            ("comment", out.get("comment"))):
            v = J._scan(f"{desc} {label}", text)
            oc = J._scan_overclaim(f"{desc} {label}", text)
            checks.append((f"[{desc}] canonical: no internal jargon in {label}", not v))
            checks.append((f"[{desc}] canonical: no overclaim (advisory honesty) in {label}", not oc))
            violations.extend(v)
            violations.extend(oc)
            if text:
                all_blobs.append((f"{desc}.{label}", text))
            if label == "comment" and text:
                scanned_cochange_comment = True

    # coverage self-check: a co-change comment was actually produced + scanned (else the canonical scan above is
    # vacuous on this surface — the whole reason this gate exists).
    checks.append(("coverage: at least one CO-CHANGE comment was rendered + scanned (the gap this gate closes)",
                   scanned_cochange_comment))
    # and the co-change block really rendered (the product phrasing is present) — so we are scanning the real thing.
    # The literal "73%" was dropped 2026-06-25 (honest-copy — it saturated at strong couplings as "100%"); the lift
    # ("4.5×") is the customer-meaningful signal we keep + check on.
    cc_clear_body = R.render_pr_check(_cochange_scenarios()[0][1], "PR-1", cochange=CC).get("comment") or ""
    checks.append(("coverage: the co-change advisory block actually rendered (the empirical-coupling line is present)",
                   "Historically changes together" in cc_clear_body and "4.5×" in cc_clear_body))

    # ── (2) EXTENDED DENYLIST sweep over EVERY representative customer surface ────────────────────────────────
    # Add the standard verdicts, pause-ack, the coverage nudge, and the watching/quota surfaces to the blob set,
    # then assert NONE carries a mission-named internal IDENTIFIER token. (Co-change blobs were already added.)
    base = {"repo": "acme/app", "branch": "main"}

    def grab(out, tag):
        for k in ("title", "summary", "comment"):
            t = out.get(k)
            if t:
                all_blobs.append((f"{tag}.{k}", t))

    # standard verdicts (no cochange) so the underscored-field guard covers the plain path too
    grab(R.render_pr_check({**base, "changes": [{"change_id": "PR-W", "label": "bob PR-W", "agent": "bob",
        "verdict": "warn", "paths": ["a.py"], "impact": ["b.py", "c.py"], "contested_with": ["alice PR-1"]}]},
        "PR-W"), "warn")
    grab(R.render_pr_check({**base, "changes": [{"change_id": "PR-U", "label": "u PR-U", "agent": "u",
        "verdict": "unknown", "paths": ["new/x.rs"], "unknown_paths": ["new/x.rs", "new/y.rs"]}]}, "PR-U"),
        "unknown")
    ser_impact = {**base, "changes": [{"change_id": "PR-S", "label": "carol PR-S", "agent": "carol",
        "verdict": "serialize", "paths": ["a.py"], "serialize_behind": ["bob PR-2"],
        "collision_points": [{"behind": "bob PR-2", "path": "a.py", "symbol": "f", "line_lo": 5, "line_hi": 9}],
        "conflict_points": [{"path": "a.py", "line_lo": 5, "line_hi": 6}], "merge_conflict_likely": True}]}
    ser = R.render_pr_check(ser_impact, "PR-S")
    grab(ser, "serialize")
    # pause-ack overlay (paused / acknowledged) — a NEW customer surface that also must hold the extended contract
    pa_snap = R.apply_pause_ack(ser, ser_impact, "PR-S", label_present=False, prior_hash=None, branch="main")
    grab(pa_snap, "pause-paused")
    grab(R.apply_pause_ack(ser, ser_impact, "PR-S", label_present=True, prior_hash=pa_snap.get("snapshot"),
                           branch="main"), "pause-acked")
    # coverage nudge (over + near) and the install/quota/cleared surfaces
    for tag, ln in (("coverage-over", R.coverage_nudge_line({"plan": "Starter", "file_limit": 250,
                        "file_count": 900, "over_by": 650, "near": False})),
                    ("coverage-near", R.coverage_nudge_line({"plan": "Starter", "file_limit": 250,
                        "file_count": 235, "over_by": 0, "near": True}))):
        if ln:
            all_blobs.append((tag, ln))
    grab(R.quota_paused_check(), "quota")
    all_blobs.append(("quota-comment", R.quota_paused_comment_body()))
    all_blobs.append(("cleared-comment", R.cleared_comment_body()))
    grab(R.watching_check(files=123, edges=4567, branch="main"), "watching")

    for label, text in all_blobs:
        ev = _scan_extended(label, text)
        checks.append((f"extended: no mission-named internal identifier token in {label}", not ev))
        violations.extend(ev)

    # ── (3) DO-NOT-OVER-BLOCK: the intentional product vocabulary MUST survive (a positive presence check). The
    #    underscored guards above must never have been written to match the product phrasing. Assert the product
    #    words are actually present in the rendered output (so we know the guard is precise, not a blunt ban).
    warn_body = R.render_pr_check({**base, "changes": [{"change_id": "PR-BR", "label": "b PR-BR", "agent": "b",
        "verdict": "warn", "paths": ["a.py"], "impact": ["b.py"], "contested_with": ["alice PR-1"]}]},
        "PR-BR").get("comment") or ""
    checks.append(("not-over-blocked: the product phrase 'blast radius' (with a space) is KEPT — only the "
                   "identifier 'blast_radius' is guarded", "blast radius" in warn_body.lower()
                   and not _scan_extended("warn-body", warn_body)))
    checks.append(("not-over-blocked: the product word 'graph' (e.g. \"main's graph\") is KEPT — it is product "
                   "vocab, not an internal token", "graph" in (R.render_pr_check({**base, "changes":
                       [{"change_id": "PR-G", "label": "g PR-G", "agent": "g", "verdict": "unknown",
                         "paths": ["n.rs"], "unknown_paths": ["n.rs"]}]}, "PR-G").get("comment") or "").lower()))
    checks.append(("not-over-blocked: the co-change product phrasing ('changes together' / 'co-change') is KEPT — "
                   "only the identifier 'co_change' is guarded",
                   "changes together" in cc_clear_body.lower() and not _scan_extended("cc-body", cc_clear_body)))

    # ── SANITY: this lock borrows a REAL canonical contract and adds a REAL extension ─────────────────────────
    checks.append(("sanity: the canonical DENYLIST is non-empty (this lock single-sources a real contract)",
                   len(J.DENYLIST) > 5))
    checks.append(("sanity: the canonical OVERCLAIM_PHRASES is non-empty", len(J.OVERCLAIM_PHRASES) > 5))
    checks.append(("sanity: the extended denylist names tokens NOT already in the canonical list (real coverage "
                   "extension, not a duplicate)",
                   any(t.strip(r"\b").lower() not in " ".join(J.DENYLIST).lower() for t in EXTENDED_DENYLIST)))
    # coverage self-check: the extended sweep actually scanned a meaningful number of distinct surfaces.
    checks.append((f"coverage: the extended denylist swept many customer surfaces (got {len(all_blobs)})",
                   len(all_blobs) >= 12))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    ok = ok and not violations

    if violations:
        print("\n-- RENDER-QUALITY VIOLATIONS (a customer surface is NOT clean — fix the WORDING, not this gate) --")
        for v in violations:
            print("   * " + v)

    print("RENDER QUALITY GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
