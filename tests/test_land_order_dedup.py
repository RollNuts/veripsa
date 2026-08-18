#!/usr/bin/env python3
"""LAND-ORDER DEDUP GATE — the suggested-land-order list must show each DISTINCT PR/branch ONCE.

Root cause: the engine emits one suggested_order entry PER reserved path/lane, so a branch with N
overlapping paths generates N identical rows. Landing is per-PR/branch (land the branch -> all its
paths resolve), so the display must list each DISTINCT PR/branch ONCE. This gate pins that fix.

Four contract points:
  D1  A branch with N repeated entries in suggested_order renders as a SINGLE row in the comment
      (the duplicates are collapsed; first occurrence wins, foundational order preserved).
  D2  The count in the "N open PRs touch this same area" header reflects DISTINCT PRs/branches,
      not the raw per-path-reservation count.
  D3  Multiple DISTINCT PRs each appear exactly once in the rendered list, in foundational order.
  D4  The "this PR" marker and "+N more" tail are correct against the deduped list (not the raw one).

Run:  python3 tests/test_land_order_dedup.py   (PURE / offline -- no Postgres, no network)
"""
import os, re, sys
sys.path.insert(0, os.path.join("github-app"))
import render as R

FAIL = 0


def check(cond, label):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def land_rows(comment):
    return [ln for ln in (comment or "").split("\n") if re.match(r"^\d+\. ", ln)]


# ===== D1 -- a branch with 8 identical entries collapses to 1 row =====
print("D1 -- 8 identical 'example-user BR-quality/ghrest-hardening' entries render as 1 row:")

BRANCH_LABEL = "example-user BR-quality/ghrest-hardening"
N_PATHS = 8   # the live screenshot showed 8 identical lines

MULTI_PATH_IMPACT = {
    "repo": "acme/app", "branch": "main",
    "changes": [
        # The customer's PR
        {"change_id": "PR-42", "label": "alice PR-42", "agent": "alice", "verdict": "serialize",
         "paths": ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py", "g.py", "h.py"],
         "serialize_behind": [BRANCH_LABEL],
         "collision_points": [{"path": "a.py", "symbol": "fn"}]},
        # The blocking branch -- 8 path-reservations -> 8 identical suggested_order entries
        {"change_id": "BR-quality/ghrest-hardening", "label": BRANCH_LABEL,
         "agent": "example-user", "verdict": "clear", "paths": ["a.py", "b.py", "c.py", "d.py",
                                                             "e.py", "f.py", "g.py", "h.py"]},
    ],
    "clusters": [{
        "changes": ["BR-quality/ghrest-hardening", "PR-42"],
        # Engine emits one entry per path -- 8 duplicates of the branch label
        "size": N_PATHS + 1,   # raw per-path-reservation count (overstated)
        "suggested_order": [BRANCH_LABEL] * N_PATHS + ["alice PR-42"],
    }],
}

out = R.render_pr_check(MULTI_PATH_IMPACT, "PR-42")
comment = out.get("comment") or ""
rows = land_rows(comment)

branch_rows = [ln for ln in rows if "BR-quality/ghrest-hardening" in ln or "example-user" in ln]
check(len(branch_rows) == 1,
      f"the branch 'BR-quality/ghrest-hardening' appears ONCE in the land order (found {len(branch_rows)} rows)")
check(len(rows) == 2,
      f"the deduped list has exactly 2 rows: the branch + alice PR-42 (found {len(rows)})")


# ===== D2 -- the count in the header reflects distinct PRs, not raw per-path size =====
print("D2 -- header count is distinct-PR count, not raw cluster.size:")
check("2 open PRs touch this same area" in comment,
      f"the header says '2 open PRs' (distinct), not '{N_PATHS + 1}' (raw per-path size): "
      + repr([ln for ln in comment.split("\n") if "open PRs" in ln]))


# ===== D3 -- multiple distinct PRs each appear once, in foundational order =====
print("D3 -- multiple distinct PRs each appear once, in foundational order:")

# Use carol PR-3 (serialize) as the customer so a comment IS posted (clear PRs with no queued_behind
# don't post a comment -- the "less noise" gate). The suggested_order has 3 distinct entries; each
# distinct PR must appear exactly once in the rendered numbered list.
DISTINCT_IMPACT = {
    "repo": "acme/app", "branch": "main",
    "changes": [
        {"change_id": "PR-1",  "label": "alice PR-1",  "agent": "alice",  "verdict": "clear",
         "paths": ["a.py"]},
        {"change_id": "PR-2",  "label": "bob PR-2",    "agent": "bob",    "verdict": "clear",
         "paths": ["a.py"]},
        {"change_id": "PR-3",  "label": "carol PR-3",  "agent": "carol",  "verdict": "serialize",
         "paths": ["a.py"], "serialize_behind": ["alice PR-1"],
         "collision_points": [{"path": "a.py", "symbol": "fn"}]},
    ],
    "clusters": [{
        "changes": ["PR-1", "PR-2", "PR-3"],
        "size": 3,
        "suggested_order": ["alice PR-1", "bob PR-2", "carol PR-3"],
    }],
}

out3 = R.render_pr_check(DISTINCT_IMPACT, "PR-3")
c3 = out3.get("comment") or ""
rows3 = land_rows(c3)

check(len(rows3) == 3, f"3 distinct PRs render as 3 rows (found {len(rows3)})")
alice_pos = next((i for i, ln in enumerate(rows3, 1) if "alice" in ln or "PR-1" in ln), None)
bob_pos   = next((i for i, ln in enumerate(rows3, 1) if "bob" in ln   or "PR-2" in ln), None)
carol_pos = next((i for i, ln in enumerate(rows3, 1) if "carol" in ln or "PR-3" in ln), None)
check(alice_pos == 1, f"alice PR-1 is first (foundational) in order (pos={alice_pos})")
check(bob_pos == 2,   f"bob PR-2 is second (pos={bob_pos})")
check(carol_pos == 3, f"carol PR-3 is third (pos={carol_pos})")


# ===== D4 -- 'this PR' marker and '+N more' tail correct against deduped list =====
print("D4 -- 'this PR' marker and '+N more' tail correct against deduped list:")

# The customer's PR (alice PR-42) sits at position 2 in the deduped order (after the branch).
this_pr_row = next((ln for ln in rows if "PR-42" in ln), None)
check(this_pr_row is not None and "this PR" in this_pr_row,
      "the 'this PR' marker appears on alice PR-42's row: " + repr(this_pr_row))
check(this_pr_row is not None and this_pr_row.startswith("2. "),
      "the 'this PR' row is numbered 2 (deduped position): " + repr(this_pr_row))

# No spurious "+N more" tail when the entire deduped list fits within LIST_LINE_CAP
check("more PR(s) further" not in comment,
      "no spurious '+N more' tail when the deduped list is short")

# Now verify +N more with a capped scenario: many DISTINCT PRs, customer near the tail
from render_bound import LIST_LINE_CAP   # noqa: E402
MANY_DISTINCT = list(range(1, LIST_LINE_CAP + 3))   # one more than cap, customer at the end
many_changes = [
    {"change_id": f"PR-{n}", "label": f"user{n} PR-{n}", "agent": f"user{n}", "verdict": "serialize",
     "paths": [f"f{n}.py"]}
    for n in MANY_DISTINCT
]
my_n = MANY_DISTINCT[-1]
many_order = [f"user{n} PR-{n}" for n in MANY_DISTINCT]

CAP_IMPACT = {
    "repo": "acme/app", "branch": "main",
    "changes": many_changes,
    "clusters": [{"changes": [f"PR-{n}" for n in MANY_DISTINCT], "size": len(MANY_DISTINCT),
                  "suggested_order": many_order}],
}
out_cap = R.render_pr_check(CAP_IMPACT, f"PR-{my_n}")
c_cap = out_cap.get("comment") or ""
rows_cap = land_rows(c_cap)
check(len(rows_cap) == LIST_LINE_CAP + 1,
      f"capped list shows LIST_LINE_CAP + 1 rows (cap + this PR forced in): found {len(rows_cap)}, cap={LIST_LINE_CAP}")
my_row_cap = next((ln for ln in c_cap.split("\n") if f"PR-{my_n}" in ln and "this PR" in ln), None)
check(my_row_cap is not None, f"'this PR' row for PR-{my_n} is visible beyond the cap")
expected_pos = len(MANY_DISTINCT)
check(my_row_cap is not None and my_row_cap.startswith(f"{expected_pos}. "),
      f"the forced-in row carries the DEDUPED position {expected_pos}: " + repr(my_row_cap))
# the "+N more" count should be total - cap - 1 (the this-PR row) = 1
remaining_count = len(MANY_DISTINCT) - LIST_LINE_CAP - 1
check(f"+{remaining_count} more PR(s)" in c_cap,
      f"'+{remaining_count} more PR(s)' in capped tail: " + repr([ln for ln in c_cap.split("\n") if "more" in ln]))


print("LAND ORDER DEDUP GATE:", "FAIL" if FAIL else "PASS")
sys.exit(FAIL)
