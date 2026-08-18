#!/usr/bin/env python3
"""NEVER-CRASH on a MALFORMED impact row — the VERDICT-RENDERING path of render_pr_check.

Premise (hardening 2026-06-18): render_pr_check turns the engine's impact-surface JSON into the customer's
PR check + comment. It TRUSTS the JSON to be well-SHAPED: every entry of `changes`/`clusters` and of each
nested verdict-detail list (shared_foundation, depends_on_changing, collision_points, conflict_points,
dampened_with) is read with `.get(...)`. So a SINGLE non-dict entry — a JSON `null` in a jsonb array, a bare
string/number from a partial or degraded read — used to raise AttributeError and take the WHOLE render down.
A render that throws means the App posts NO check AND NO comment on that PR at all: the failure is invisible,
the customer is simply never told anything. That is the worst kind of degrade for a customer surface.

Two honesty/robustness properties this gate pins:

  * NEVER-CRASH — a malformed row (None / string / number, a non-dict cluster, a non-list `changes`, a non-dict
    whole impact) must degrade to a returned dict with a check + (possibly None) comment, never an exception.
    A bad row is DROPPED; the WELL-FORMED rows beside it STILL render (honest-empty for the bad slice only).
  * UNEXPECTED VERDICT IS HONEST — a verdict the engine never emits (None, a number, a renamed/typo'd string)
    must NOT silently render as 'clear' ("nothing else touches your files" = asserting safety we cannot back),
    and must NOT echo the raw value into the comment (`**• None**` leaked a bare engine value). It maps to the
    honest 'unknown' branch ("not enough signal to call this clear").

PURE + OFFLINE (no DB, no network, no deploy): crafts the malformed surfaces in memory and drives them through
the REAL render_pr_check. Content-free throughout — only paths / labels / counts ever appear in the output.

Run:  python3 tests/test_render_malformed_impact.py     (no DB needed)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import render  # noqa: E402

FAIL = 0


def check(cond: bool, label: str):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _never_crash(label, impact, ref="PR-1"):
    """Drive a malformed surface through render_pr_check; assert it returns a well-shaped check, never raises."""
    try:
        out = render.render_pr_check(impact, ref)
    except Exception as e:   # the regression: an AttributeError here = NO check, NO comment for the customer
        check(False, f"{label}: render_pr_check raised {type(e).__name__} (must never crash) — {str(e)[:60]}")
        return None
    ok = isinstance(out, dict) and {"conclusion", "title", "summary"} <= set(out)
    check(ok, f"{label}: returned a well-shaped check, no crash")
    # content-free: a raw 'None' word for a dropped row must never leak as literal output
    check("**None**" not in (out.get("comment") or ""), f"{label}: no literal 'None' leaked into the comment")
    return out


def test_non_dict_change_rows():
    """A non-dict entry in `changes` (a JSON null, a bare string) must not crash the top-level scan."""
    _never_crash("None in changes", {"changes": [None]})
    _never_crash("string in changes", {"changes": ["PR-1"]})
    _never_crash("number in changes", {"changes": [42]})
    _never_crash("changes is None (not a list)", {"changes": None})
    _never_crash("changes is a string", {"changes": "PR-1"})
    _never_crash("whole impact is None", None)
    _never_crash("whole impact is a list", ["PR-1"])


def test_non_dict_nested_verdict_detail_rows():
    """A non-dict entry in any nested verdict-detail list (each read with .get) must drop, not crash."""
    _never_crash("shared_foundation has None",
                 {"changes": [{"change_id": "PR-1", "verdict": "warn", "shared_foundation": [None]}]})
    _never_crash("depends_on_changing has a string",
                 {"changes": [{"change_id": "PR-1", "verdict": "warn", "depends_on_changing": ["x"]}]})
    _never_crash("collision_points has a string",
                 {"changes": [{"change_id": "PR-1", "verdict": "serialize", "behind": ["a"],
                               "collision_points": ["bad"]}]})
    _never_crash("conflict_points has a number",
                 {"changes": [{"change_id": "PR-1", "verdict": "serialize", "behind": ["a"],
                               "merge_conflict_likely": True, "conflict_points": [42]}]})
    _never_crash("dampened_with has a string",
                 {"changes": [{"change_id": "PR-1", "verdict": "unknown", "dampened_with": ["x"]}]})
    _never_crash("a detail list is None, not a list",
                 {"changes": [{"change_id": "PR-1", "verdict": "warn", "shared_foundation": None,
                               "depends_on_changing": None}]})


def test_non_dict_cluster_row():
    """A non-dict entry in `clusters` must not crash the cluster lookup."""
    _never_crash("clusters has None",
                 {"changes": [{"change_id": "PR-1", "verdict": "warn"}], "clusters": [None]})
    _never_crash("clusters has a string",
                 {"changes": [{"change_id": "PR-1", "verdict": "warn"}], "clusters": ["c1"]})
    _never_crash("clusters is None",
                 {"changes": [{"change_id": "PR-1", "verdict": "warn"}], "clusters": None})


def test_well_formed_rows_survive_a_bad_neighbor():
    """A malformed row is DROPPED, but the well-formed rows beside it STILL render — honest-empty only for the
    bad slice, never going dark on the whole surface."""
    # a good shared_foundation row next to a None one: the good one must still surface its path
    out = render.render_pr_check(
        {"changes": [{"change_id": "PR-1", "verdict": "warn",
                      "shared_foundation": [None, {"path": "core/db.py", "fan_in": 5}]}]}, "PR-1")
    check("core/db.py" in (out.get("comment") or ""),
          "a well-formed shared_foundation row still renders alongside a dropped bad one")
    # a good change row next to a non-dict change row: the good one is still found + rendered
    out2 = render.render_pr_check(
        {"changes": [None, {"change_id": "PR-1", "verdict": "serialize",
                            "serialize_behind": ["alice PR-7"]}]}, "PR-1")
    check(out2["conclusion"] == "neutral" and "alice" in (out2.get("comment") or ""),
          "a well-formed change row is still found + rendered when a non-dict row precedes it")


def test_unexpected_verdict_is_honest_not_clear():
    """An unrecognised verdict (None, a number, a renamed string) must render as honest 'unknown' — never silently
    'clear' (asserting safety) and never echoing the raw value into the comment."""
    for label, bad in (("None", None), ("number", 7), ("renamed", "weird_new_verdict")):
        out = render.render_pr_check(
            {"changes": [{"change_id": "PR-1", "verdict": bad, "paths": ["a.py"]}]}, "PR-1")
        # must NOT claim 'clear' (the silent-safety regression)
        check("Clear" not in out["summary"] and "clear" not in out["title"].lower(),
              f"unexpected verdict ({label}): does NOT silently render as 'clear'")
        # must NOT echo the raw verdict value (the `**• None**` / raw-string leak). For None/the renamed string
        # the raw token is distinctive enough to assert it is absent; a bare number is not (it can appear in a
        # legit count), so we assert the title/header carry the known 'Unknown' label instead of the raw value.
        body = (out.get("comment") or "") + out["title"] + out["summary"]
        if label in ("None", "renamed"):
            check(str(bad) not in body,
                  f"unexpected verdict ({label}): the raw engine value is not echoed to the customer")
        check("Unknown" in out["title"],
              f"unexpected verdict ({label}): renders the known 'Unknown' label, not the raw verdict")
        # the conclusion is a safe non-blocking value (unknown → neutral), never a blocking one
        check(out["conclusion"] in ("success", "neutral"),
              f"unexpected verdict ({label}): conclusion stays non-blocking ({out['conclusion']})")


def test_dampened_with_dangling_name_guard():
    """DAMPENING-HONEST-RENDER (audit, dampening-honest-render lane): when the engine hands the renderer a
    DEGENERATE `dampened_with` row — null `by` (the other PR's label) and/or null `via_hub` (the shared file
    name) — the dampening copy used to render "linked to  through a heavily-shared file ()" (a double-space gap
    + a literal empty parens). Same dangling pattern in the corroborated/'warn' arm: "linked to  through a
    shared file ()". A degenerate row can arrive from an older engine, a partial read, or a malformed jsonb
    array — the same shape every other render path defends against. The four honest dampening obligations stand
    regardless of which fields landed:
      1. SILENCE-VISIBLE — the bold "A coupling here was suppressed to avoid noise" lead must always render.
      2. UNKNOWN-NOT-CLEAR — "treat as unknown, not clear" must always appear.
      3. NAME THE HUB when we have it; cleanly DROP the "(hub)" parenthetical when we don't (never "()").
      4. NEVER pretend 'clear' — the verdict header / icon stays Unknown."""
    # ── (1) UNKNOWN arm, both fields null — dangling lead used to read "linked to  through ... ()". ─────────
    out1 = render.render_pr_check({"changes": [{"change_id": "PR-D", "label": "d PR-D", "agent": "d",
        "verdict": "unknown", "paths": ["a.py"],
        "dampened_with": [{"by": None, "via_hub": None}]}]}, "PR-D")
    c1 = out1.get("comment") or ""
    check("linked to  through" not in c1,
          "(unknown) degenerate dampened_with (both null) does NOT render a dangling 'linked to  through' "
          "(double space, empty name)")
    check("()" not in c1,
          "(unknown) degenerate dampened_with (both null) does NOT render a literal empty parens '()' for "
          "the missing hub file")
    check("suppressed to avoid noise" in c1 and "treat as unknown, not clear" in c1,
          "(unknown) the honest dampening lead + 'treat as unknown, not clear' obligations still render on "
          "a degenerate row (silence-visible, never silently 'clear')")
    check("Unknown" in (out1.get("title") or ""),
          "(unknown) the verdict header stays 'Unknown' on a degenerate dampening row (never 'Clear')")

    # ── (2) UNKNOWN arm, only `via_hub` null — must keep the 'by' name + drop the empty '(hub)' parenthetical.
    out2 = render.render_pr_check({"changes": [{"change_id": "PR-E", "label": "e", "agent": "e",
        "verdict": "unknown", "paths": ["a.py"],
        "dampened_with": [{"by": "alice PR-1", "via_hub": None}]}]}, "PR-E")
    c2 = out2.get("comment") or ""
    check("alice" in c2 and "()" not in c2,
          "(unknown) `by` present + `via_hub` null: the name still renders AND no literal '()' for the hub")
    check("treat as unknown, not clear" in c2,
          "(unknown) the 'treat as unknown, not clear' obligation survives a missing via_hub")

    # ── (3) UNKNOWN arm, only `by` null — must name the hub + drop the dangling 'linked to' agent. ─────────
    out3 = render.render_pr_check({"changes": [{"change_id": "PR-F", "label": "f", "agent": "f",
        "verdict": "unknown", "paths": ["a.py"],
        "dampened_with": [{"by": None, "via_hub": "hub.py"}]}]}, "PR-F")
    c3 = out3.get("comment") or ""
    check("linked to  through" not in c3 and "`hub.py`" in c3,
          "(unknown) `via_hub` present + `by` null: the hub file is named AND no dangling 'linked to  ' "
          "(double space) for the missing other-PR label")

    # ── (4) WARN/CORROBORATED arm, both fields null — same dangling shape used to leak. ────────────────────
    out4 = render.render_pr_check({"changes": [{"change_id": "PR-W", "label": "w", "agent": "w",
        "verdict": "warn", "paths": ["a.py"],
        "dampened_with": [{"by": None, "via_hub": None, "corroborated": True}]}]}, "PR-W")
    c4 = out4.get("comment") or ""
    check("linked to  through" not in c4 and "()" not in c4,
          "(warn/corroborated) degenerate dampened_with (both null) does NOT render a dangling 'linked to  ' "
          "/ empty '()' — the same guard applies to the corroborated arm")
    check("two independent signals" in c4,
          "(warn/corroborated) the 'two independent signals' WHY still renders on a degenerate row (the "
          "corroborated framing survives, just without the dangling tokens)")

    # ── (5) Well-formed rows are unchanged (the guard is degenerate-only — no copy regression). ────────────
    out5 = render.render_pr_check({"changes": [{"change_id": "PR-G", "label": "g", "agent": "g",
        "verdict": "unknown", "paths": ["a.py"],
        "dampened_with": [{"by": "alice PR-1", "via_hub": "hub.py"}]}]}, "PR-G")
    c5 = out5.get("comment") or ""
    check("alice" in c5 and "`hub.py`" in c5 and "suppressed to avoid noise" in c5,
          "(unknown) a WELL-FORMED dampened_with row is unchanged: the name + the hub + the 'suppressed' "
          "lead all render together (the guard fires only on a degenerate row)")


def main():
    print("=== NEVER-CRASH on a malformed impact row — render_pr_check verdict path (offline) ===")
    print("-- non-dict rows in `changes` (top-level scan) --")
    test_non_dict_change_rows()
    print("-- non-dict rows in nested verdict-detail lists --")
    test_non_dict_nested_verdict_detail_rows()
    print("-- non-dict rows in `clusters` --")
    test_non_dict_cluster_row()
    print("-- well-formed rows survive a malformed neighbor (honest-empty only for the bad slice) --")
    test_well_formed_rows_survive_a_bad_neighbor()
    print("-- an unexpected verdict renders honest 'unknown', never silent 'clear' / raw value --")
    test_unexpected_verdict_is_honest_not_clear()
    print("-- DAMPENING-HONEST-RENDER: a degenerate dampened_with row has no dangling text + keeps the four obligations --")
    test_dampened_with_dangling_name_guard()
    print("------------------------------------------------------------")
    if FAIL == 0:
        print("RENDER MALFORMED-IMPACT GATE: PASS")
        return 0
    print("RENDER MALFORMED-IMPACT GATE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
