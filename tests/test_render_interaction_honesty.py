import os, sys
sys.path.insert(0, os.path.join("github-app"))
import render as R
base = {"repo":"acme/app","branch":"main"}
FAIL = 0
def check(cond, label):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ")+label)
    if not cond: FAIL = 1

# ===== FINDING 1: fork redaction renders a SELF-CONTRADICTION on a clear fork PR =====
# A clear fork PR (truncated, or a clear holder) posts a redacted comment whose HEADER says "Clear"
# while the body asserts "This PR overlaps other in-flight work in this repo" — flat contradiction.
print("FINDING 1 — fork clear self-contradiction:")
# 1a: truncated clear fork, nothing nearby
out = R.render_pr_check({**base,"changes":[{"change_id":"PR-T","label":"ext PR-T","agent":"ext",
    "verdict":"clear","paths":["a.py"]}]}, "PR-T", is_fork=True, truncated=True)
c = out["comment"] or ""
header_clear = "Clear" in c.split("\n")[1]
body_overlap = "overlaps other in-flight work" in c
check(not (header_clear and body_overlap),
      "a TRUNCATED clear fork PR does not say 'Clear' in the header AND 'overlaps other in-flight work' in the body")

# 1b: clear fork lane-holder (others queued behind it — it does NOT overlap anything)
out2 = R.render_pr_check({**base,"changes":[{"change_id":"PR-50","label":"a PR-50","agent":"a","verdict":"clear",
    "paths":["a.py"], "queued_behind":[{"change_id":"PR-99","agent":"w"}], "queued_behind_paths":["a.py"]}]},
    "PR-50", is_fork=True)
c2 = out2["comment"] or ""
check(not ("Clear" in c2.split("\n")[1] and "overlaps other in-flight work" in c2),
      "a clear fork lane-HOLDER does not contradict its 'Clear' header with 'overlaps other in-flight work'")

# ===== FINDING 2: a malformed cluster.size (non-numeric) CRASHES the render =====
print("FINDING 2 — non-numeric cluster.size crashes render:")
for v in ("clear","warn","serialize","unknown"):
    try:
        R.render_pr_check({**base,"changes":[{"change_id":"P","verdict":v,"paths":["a"],"impact":["b"],
            "contested_with":["x"]}], "clusters":[{"changes":["P"],"size":"3","suggested_order":["P"]}]}, "P")
        crashed = False
    except Exception:
        crashed = True
    check(not crashed, f"a non-numeric cluster.size does not crash render (verdict={v})")

print("RENDER INTERACTION HONESTY GATE:", "FAIL" if FAIL else "PASS")
sys.exit(FAIL)
