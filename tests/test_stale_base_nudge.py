#!/usr/bin/env python3
"""POST-MERGE STALENESS NUDGE gate — the pre-conflict heads-up (OFFLINE; no Postgres, no GitHub account).

Veripsa's in-flight collision detector compares CONCURRENTLY-open PRs. It is BLIND to a change that has ALREADY
MERGED into the protected branch — so a PR whose files were touched by a landing AFTER the PR branched only
discovers the conflict at rebase/merge time (too late). The stale-base nudge closes that gap: on an opened/
synchronize, if the protected branch advanced since this PR's base AND a landed file overlaps a file THIS PR is
editing, the App appends ONE content-free advisory line to the PR check summary ("`main` moved under you —
rebase before merge so you work through any overlap on your branch now, not at merge time"). HONEST: rebasing
does not make the overlap vanish; it moves the work onto your branch deliberately instead of a blocked merge.

This gate proves the two new pieces in ISOLATION, with NO database (the feature is GitHub-API-only):
  (a) OVERLAP → a nudge line that NAMES the overlapping path(s), with NO code body.
  (b) NO OVERLAP (and empty / garbled inputs) → None (nothing to nudge — never a false heads-up).
  (c) the rendered line is CONTENT-FREE — only path strings (the author's own files), capped ≤3 with a
      qualitative "and more"; NEVER a raw file COUNT (PO 2026-06-21 「file count はダメ」), a diff/body, or jargon.
  (d) FAIL-OPEN: the render fn never raises on hostile input; and the thin REST method
      (GitHubREST.compare_changed_paths) returns only filename STRINGS from a fake _api and returns [] on any
      error / odd-shaped response (so a bad ref can never escalate past an advisory add-on).

Pure-function + fake-_api test (mirrors tests/test_render_interaction_honesty.py — no deploy, no GitHub I/O,
no DB). Header/import idiom mirrors tests/test_comment_idempotency.py (ROOT + sys.path.insert github-app).

Run:  python3 tests/test_stale_base_nudge.py     (no DB needed)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import render as R          # noqa: E402
import github_rest as GR    # noqa: E402

FAIL = 0


def check(cond, label):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


# A unified-diff / code-body shape — if ANY of this ever leaked into the customer line, the feature would NOT be
# content-free. The line must carry path STRINGS + a count only, never one of these. (Only DIFF/CODE-specific
# markers — ordinary prose punctuation like ';' is legitimate copy and is NOT a leak.)
_CODE_BODY_MARKERS = ["@@", "\n+", "\n-", "def ", "import ", "return ", "console.log", "secret_code"]
# Internal tokens / overclaims the customer copy must never contain (a focused mirror of test_no_jargon_leak's
# contract for THIS line: no engine identifier, no certainty/guarantee an advisory product cannot make).
_JARGON_OR_OVERCLAIM = [
    "veripsa_app", "_with_authority", "_claim_adjacency", "search_path", "RLS", "moat", "manifest",
    "guarantee", "ensure", "prevent", "proven", "always safe", "cannot conflict", "never conflicts",
]


def _has_body(s: str) -> bool:
    return any(m in s for m in _CODE_BODY_MARKERS)


def _has_jargon(s: str) -> bool:
    low = s.lower()
    return any(t.lower() in low for t in _JARGON_OR_OVERCLAIM)


# ===== (a) OVERLAP → a content-free nudge that names the overlapping path(s) =====
print("(a) overlap → nudge naming the overlapping path(s), no code body:")
branch = ["src/app.py", "README.md", "src/util.py"]   # files that landed on main since this PR branched
pr = ["src/app.py", "tests/test_app.py"]              # files THIS PR edits  → overlap = src/app.py
line = R.stale_base_nudge_line(branch, pr)
check(line is not None, "an overlap returns a nudge line (not None)")
check(line is not None and "src/app.py" in line, "the nudge NAMES the overlapping path (src/app.py)")
check(line is not None and "README.md" not in line and "src/util.py" not in line,
      "the nudge does NOT name a branch path the PR did not touch (only the OVERLAP)")
check(line is not None and "tests/test_app.py" not in line,
      "the nudge does NOT name a PR path that did not land on main (only the OVERLAP)")
check(line is not None and not _has_body(line), "the overlap nudge carries NO code/diff body")
check(line is not None and not _has_jargon(line), "the overlap nudge carries NO internal jargon / overclaim")
check(line is not None and "main" in line, "the nudge mentions the protected branch (main) so it is actionable")
# the REDUNDANCY angle (PO 2026-06-20: "can Veripsa detect 'already fixed'?"). The same overlap that warns of a
# rebase conflict ALSO means a just-landed change touched the code you're editing — so the nudge must prompt a
# check for ALREADY-DONE work (don't redo it), content-free + honest ("check whether", never "it is already
# fixed" — that semantic call is the customer's, not Veripsa's).
check(line is not None and "redo" in line.lower() and "already" in line.lower(),
      "the nudge prompts a REDUNDANCY check (don't redo work a landed change may already have done)")
# singular vs plural agreement (a small honesty: "File … has" not "Files … have"). MOAT (PO 2026-06-21 「file
# count はダメ」): the nudge no longer states the NUMBER of files ("2 files") — that is a raw file count — it
# names the author's own paths and agrees the noun/verb on the count WITHOUT printing it. So assert the word
# form ("File … has" singular / "Files … have" plural) and that the bare count ("2 files") is GONE.
one = R.stale_base_nudge_line(["only/one.py"], ["only/one.py", "x/y.py"])
check(one is not None and "File you're editing" in one and "has changed" in one and "Files you're editing" not in one,
      "a single-file overlap reads 'File … has changed' (singular agreement, no raw count)")
two = R.stale_base_nudge_line(["a.py", "b.py"], ["a.py", "b.py"])
check(two is not None and "Files you're editing" in two and "have changed" in two and "2 file" not in two,
      "a two-file overlap reads 'Files … have changed' (plural agreement) and never the raw count '2 files' (MOAT)")

# ===== (b) NO OVERLAP / empty / garbled → None (never a false heads-up) =====
print("(b) no overlap (and empty/garbled inputs) → None:")
check(R.stale_base_nudge_line(["src/a.py"], ["src/b.py"]) is None, "disjoint path sets → None")
check(R.stale_base_nudge_line([], ["src/a.py"]) is None, "empty branch list → None (main did not advance)")
check(R.stale_base_nudge_line(["src/a.py"], []) is None, "empty PR list → None (nothing to collide)")
check(R.stale_base_nudge_line(None, None) is None, "None inputs → None (a malformed event, never a crash)")
check(R.stale_base_nudge_line([1, 2, {"x": 1}], [1, 2]) is None, "non-string junk entries → None (no real overlap)")
check(R.stale_base_nudge_line(["", "  "], [""]) is None, "empty/blank path strings are not a real overlap → None")

# ===== (c) CONTENT-FREE on a WIDE overlap → capped (≤3 + qualitative "and more"), body-free + jargon-free =====
# MOAT (PO 2026-06-21 「file count はダメ」): the overflow trailer is a QUALITATIVE "and more", NOT the raw
# "+N more" count (that count is a file count). Assert ≤3 paths are named, the qualitative trailer is present,
# and the raw "+7 more" count is GONE.
print("(c) wide overlap → paths capped (≤3 + qualitative 'and more'), content-free:")
wide_branch = [f"pkg/mod_{i}.py" for i in range(10)]
wide_pr = [f"pkg/mod_{i}.py" for i in range(10)]       # 10-way overlap
wide = R.stale_base_nudge_line(wide_branch, wide_pr)
_named = sum(1 for i in range(10) if f"`pkg/mod_{i}.py`" in (wide or ""))
check(wide is not None and ", and more" in wide and "+7 more" not in wide and _named == 3,
      "a 10-way overlap caps the named paths to 3 and ends with the qualitative 'and more' (no raw '+7 more' count — MOAT)")
# exactly 3 code-spans for the 3 named paths (`pkg/mod_*.py`), never all 10 inlined
check(wide is not None and wide.count("`pkg/mod_") == 3, "exactly 3 paths are inlined (the cap), not the whole set")
# MOAT INVERSION (PO 2026-06-21 「file count はダメ」): the line must NOT report the full overlap size as a raw
# count ("10 files") — that is a file count = a graph-size leak. The breadth is conveyed by the "and more"
# trailer instead. (This assertion previously REQUIRED "10 files"; it now requires its ABSENCE.)
check(wide is not None and "10 file" not in wide and "10 files" not in wide,
      "the line does NOT leak the raw overlap size ('10 files') — MOAT: breadth is shown qualitatively, not as a count")
check(wide is not None and not _has_body(wide) and not _has_jargon(wide),
      "the capped wide-overlap line is still body-free and jargon-free")

# ===== (d) FAIL-OPEN — render fn never raises; thin method returns only filenames + [] on any error =====
print("(d) fail-open: render fn pure + thin REST method with a fake _api:")
# (d1) the render fn is total — even on hostile / mixed input it returns str|None, never raises.
raised = False
try:
    for b, p in (([None, 123, "ok/x.py"], ["ok/x.py"]), ("not-a-list", 7), ({"k": "v"}, ["ok/x.py"])):
        _ = R.stale_base_nudge_line(b, p)
except Exception:
    raised = True
check(not raised, "stale_base_nudge_line never raises on hostile/odd input (fail-open render)")

# (d2) the thin REST method: build a GitHubREST WITHOUT touching the network — override the ONE I/O seam (_api).
gh = GR.GitHubREST("app-id", "key", "42")

# happy path: a compare response → returns ONLY the filename strings, body discarded.
def _api_ok(method, url, body=None, accept="application/vnd.github+json"):
    assert "/compare/" in url, f"unexpected url {url!r}"
    return {"files": [
        {"filename": "src/app.py", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+new"},
        {"filename": "src/util.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+secret_code()"},
        {"status": "removed"},                 # no filename → skipped
        "garbage",                              # non-dict entry → skipped
    ]}
gh._api = _api_ok
paths = gh.compare_changed_paths("acme/app", "b" * 40, "main")
check(paths == ["src/app.py", "src/util.py"], "compare_changed_paths returns ONLY the filename strings (in order)")
check(all(isinstance(p, str) for p in paths) and not any(_has_body(p) for p in paths),
      "compare_changed_paths never returns a patch/body — only path strings")

# error path: _api raises → method swallows it and returns [] (advisory add-on never escalates).
def _api_boom(method, url, body=None, accept="application/vnd.github+json"):
    raise RuntimeError("simulated GitHub 500 / network error")
gh._api = _api_boom
check(gh.compare_changed_paths("acme/app", "b" * 40, "main") == [],
      "compare_changed_paths returns [] when the underlying _api raises (fail-soft)")

# odd-shaped responses → [] (a non-dict, or a non-list `files`), never a crash.
gh._api = lambda *a, **k: ["not", "a", "dict"]
check(gh.compare_changed_paths("acme/app", "b" * 40, "main") == [], "a non-dict compare response → [] (no crash)")
gh._api = lambda *a, **k: {"files": "not-a-list"}
check(gh.compare_changed_paths("acme/app", "b" * 40, "main") == [], "a non-list `files` → [] (no crash)")

# absent inputs short-circuit BEFORE any _api call (the fake would assert if reached) → [].
def _api_must_not_run(*a, **k):
    raise AssertionError("compare_changed_paths called _api on absent inputs (should have short-circuited)")
gh._api = _api_must_not_run
check(gh.compare_changed_paths("", "b" * 40, "main") == [], "empty repo → [] without an API call")
check(gh.compare_changed_paths("acme/app", "", "main") == [], "empty base_sha → [] without an API call")
check(gh.compare_changed_paths("acme/app", "b" * 40, "") == [], "empty head_ref → [] without an API call")

# refs are URL-quoted (a branch can carry '/' or other URL-significant chars) — assert the compare URL is built
# from quoted refs, not raw.
captured = {}
def _api_capture(method, url, body=None, accept="application/vnd.github+json"):
    captured["url"] = url
    return {"files": []}
gh._api = _api_capture
gh.compare_changed_paths("acme/app", "release/1.0", "feature/x y")
check("release%2F1.0" in captured.get("url", ""), "the base ref is URL-quoted in the compare URL")
check("feature%2Fx%20y" in captured.get("url", ""), "the head ref is URL-quoted in the compare URL")

print("STALE BASE NUDGE GATE:", "FAIL" if FAIL else "PASS")
sys.exit(FAIL)
