#!/usr/bin/env python3
"""Veripsa — customer-surface SIZE BOUNDING for the PR comment body (content-free).

The single place that keeps the assembled PR-comment markdown within GitHub's hard 65536-char comment limit. A
GitHub PR/issue comment body the App POSTs is hard-capped by the API at 65536 chars; a body over that is rejected
(HTTP 422) and the customer gets NO Veripsa comment AT ALL — and that happens on exactly the busiest,
most-collision-prone PR (a large contention neighborhood / a pile of shared foundations), where the comment
matters most. This module holds the two bounding layers + their caps:

  * `_clamp_line`  — bounds ONE assembled line's byte length (a pathological 10 KB path/label can't render as one
    10 KB line), preserving THIS PR's `← this PR` land-order marker even when it suffixes a huge label;
  * `_cap_comment_body` — the FINAL, assembly-level guarantee: after the body is built it is clamped to
    COMMENT_BODY_CAP, deterministically, while ALWAYS keeping the header framing, THIS PR's own land-order row
    (so the customer can always see where THEY land), and the footer — eliding the middle with a content-free
    "(+N line(s) elided …)" notice.

Split OUT of render.py on purpose: HOW we BOUND the body's size (caps + clamp + elide) and WHAT the verdict copy
says are independent concerns that used to share one file and collide on nearly every PR (the hotspot lesson —
finer files = finer collision point). render.py re-exports these names + caps, so callers/tests using
`render._cap_comment_body` / `render.COMMENT_BODY_CAP` etc. are unchanged. Content-free: it moves/counts/clamps/
ellipsizes the renderer's own already-content-free lines — it never reads code. Kept in lock-step with
tests/test_comment_size_cap.py.
"""
from __future__ import annotations

# The per-section LINE caps the comment builder applies bound the NUMBER of items each loop emits (mirroring how
# `_fmt_list`/`_fmt_agents` cap inline lists: show the first N, count the rest — same FRAME / 枠を決める). A
# pathological input (hundreds/thousands of entangled PRs, or many long paths/labels) would otherwise emit ONE
# line PER item with no bound and blow the body past GitHub's 65536-char limit, the POST 422s, and the customer
# gets NO comment on exactly the busiest, most-collision-prone PR.
LIST_LINE_CAP = 8

# The per-section LINE caps bound the NUMBER of items each loop emits — but NOT the byte LENGTH of any one line,
# NOR the AGGREGATE across the ~10 independent sections. So a single pathological item (a 10 KB crafted PR label,
# a deep monorepo path) renders as ONE giant line, and a busy PR can light up many capped sections at once —
# either way the joined body can still cross GitHub's 65536-char cap, the POST 422s, and the customer gets NO
# comment on exactly the busiest, most-collision-prone PR. `_cap_comment_body` (below) is the FINAL, assembly-level
# guarantee: after the body is built it is clamped to COMMENT_BODY_CAP, deterministically, while ALWAYS keeping
# the header framing, THIS PR's own land-order row (so the customer can always see where THEY land), and the
# footer — eliding the middle with a content-free "(+N line(s) elided …)" notice. Margin under 65536 leaves room
# for the upsert marker the poster wraps the body in. Content-free: only line counts + an ellipsis, never code.
COMMENT_BODY_CAP = 60000
# A single preserved line is itself clamped to this many chars (a pathological 10 KB path/label can't render as
# one 10 KB line). Kept well under the body cap so even an all-preserved set of lines stays bounded.
COMMENT_LINE_CAP = 2000
_THIS_PR_MARKER = "← this PR"   # the land-order row tag (kept in lock-step with where it is appended below)


def _clamp_line(line: str, cap: int = COMMENT_LINE_CAP) -> str:
    """Bound ONE assembled line's length. The per-section line caps bound item COUNT, never the byte length of a
    single item — so a pathological 10 KB path/label renders as one 10 KB line, and a handful of those clear the
    body cap by themselves. Clamp any over-long line with a content-free ellipsis (only the visible glyphs are
    cut; nothing semantic is asserted). A normal line (well under the cap) is returned byte-identically.

    THIS PR's land-order row carries the `← this PR` marker as a SUFFIX after a (possibly huge) label, so a naive
    tail-cut would drop the very marker we must keep visible. So clamp the body BEFORE the marker and re-append it
    — the customer always sees the row IS theirs, even when their label is pathological."""
    if len(line) <= cap:
        return line
    if line.endswith(_THIS_PR_MARKER):
        head = line[: -len(_THIS_PR_MARKER)]
        budget = cap - len(_THIS_PR_MARKER) - 1   # room for the ellipsis + the preserved marker
        if budget > 0:
            return head[:budget].rstrip() + "…" + _THIS_PR_MARKER
    return line[: cap - 1].rstrip() + "…"


def _cap_comment_body(lines: list[str], cap: int = COMMENT_BODY_CAP) -> str:
    """FINAL assembly-level guarantee that the comment body stays within GitHub's 65536-char comment limit, with
    THIS PR's own land-order row ALWAYS visible — deterministic and content-free.

    Why a global net on top of the per-section caps: each section's loop is capped to LIST_LINE_CAP LINES, but
    (a) one line's LENGTH is unbounded (a 10 KB crafted label/path), and (b) a busy PR lights up ~10 sections at
    once, so even all-short lines can AGGREGATE past the cap. Either way the POST 422s and the customer gets NO
    comment on the busiest PR — the opposite of the intent. This clamps the WHOLE body once, after assembly.

    Strategy (order-preserving, idempotent below the cap):
      * clamp every line's length first (so one giant line can't blow the budget on its own);
      * if the joined body already fits, return it unchanged — the common case pays nothing;
      * otherwise ALWAYS keep the header (the first framing lines), the footer (the trailing `<sub>…` line), and
        any line carrying THIS PR's marker; greedily keep leading lines until the budget would be exceeded, then
        emit ONE content-free elision notice counting the dropped lines, then the always-keep tail.
    Content-free: it moves/counts/ellipsizes the renderer's own already-content-free lines — it never reads code."""
    lines = [_clamp_line(x) for x in lines]
    body = "\n".join(lines)
    if len(body) <= cap:
        return body

    n = len(lines)
    # always-keep set: the header framing (first up-to-3 lines: "### Veripsa…", the verdict bold, the blank),
    # the footer (the closing "<sub>…</sub>" advisory line, by content not position so a trailing blank can't
    # hide it), and every line that carries THIS PR's land-order marker (so the customer always sees where they
    # land, even when their row sits deep past the shown slice).
    head_keep = min(3, n)
    foot_idx = next((i for i in range(n - 1, -1, -1) if lines[i].startswith("<sub>")), None)
    must_keep = set(range(head_keep))
    if foot_idx is not None:
        must_keep.add(foot_idx)
    must_keep.update(i for i, x in enumerate(lines) if _THIS_PR_MARKER in x)

    elision = "_(+{n} line(s) elided to fit GitHub's comment size limit.)_"
    # reserve room for the always-keep lines + the elision notice + the joining newlines, so the result NEVER
    # exceeds the cap regardless of how big the dropped middle was.
    fixed = [lines[i] for i in sorted(must_keep)]
    reserve = sum(len(x) for x in fixed) + len(elision.format(n=n)) + (len(fixed) + 2)

    kept: list[int] = []
    used = reserve
    for i in range(n):
        if i in must_keep:
            continue
        add = len(lines[i]) + 1
        if used + add > cap:
            continue   # skip-not-break: a later SHORT line can still fit even if THIS one is too big to add
        used += add
        kept.append(i)
        must_keep.add(i)

    dropped = n - len(must_keep)
    # rebuild in ORIGINAL order; place the single elision notice where the first dropped run begins so the body
    # reads top-to-bottom (header … kept lines … "(+N elided)" … this-PR row … footer).
    out: list[str] = []
    notice_done = dropped <= 0
    for i in range(n):
        if i in must_keep:
            out.append(lines[i])
        elif not notice_done:
            out.append(elision.format(n=dropped))
            notice_done = True
    capped = "\n".join(out)
    # belt-and-braces: if a pathological always-keep set (e.g. very many this-PR-marked rows) is itself over the
    # cap, hard-clamp the final string. Deterministic, content-free — the last resort can never 422.
    if len(capped) > cap:
        capped = capped[: cap - 1].rstrip() + "…"
    return capped
