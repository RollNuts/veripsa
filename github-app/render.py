#!/usr/bin/env python3
"""Veripsa GitHub App — THE PRODUCT SURFACE (the customer's whole UX for v1).

Premise (PO 2026-06-17): Veripsa is delivered AS a GitHub App, not as a website you log into. The value
shows up INSIDE the pull request — a check (pass/advisory/action) + a comment — rendered by GitHub, where
the developer already is. There is no console to visit for the core job (the real-time dashboard is
deferred / retention-only). So THIS file is the product's face.

This module is PURE + STATELESS + CONTENT-FREE: it turns the output of `core.main_impact_surface(repo,
branch)` (JSON: in-flight PRs against the protected branch, each with verdict / impact / contention) into
the two things GitHub shows on a PR — the CHECK and the COMMENT. It carries only paths, agent names, and
counts — never code bodies. It needs no deploy, no secret, no network: the webhook handler (Part B,
PO-gated) imports `render_pr_check` and posts the result; here it is fully testable offline.

The verdicts come straight from the engine and ARE the product distilled to two moves (PO):
  serialize  待ちを作る      — a DIRECT same-file collision: this PR is queued behind a holder; land in order.
  warn       作業内容を改めさせる — a SEMANTIC A→B exposure: your change's blast radius meets another in-flight
                              change; revise against it now (steer, never a blunt block).
  clear                      — neither; nothing else in flight touches your neighborhood.
…all caught BEFORE the merge (= 手戻りを減らす).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys

# Customer-surface SANITIZATION (markdown/HTML escaping + the internal-role scrub) lives in render_safe.py —
# split out so a change to HOW we sanitize and a change to the verdict COPY don't collide on the same file
# (finer files = finer collision point). Re-exported here so `render._code` / `render._safe_agent` etc. keep
# working unchanged for callers and tests.
try:
    from render_safe import (_INTERNAL_LABEL_RE, _SAFE_AGENT_FALLBACK, _safe_agent,
                             _MD_ESCAPE, _code, _safe, _fmt_list, _fmt_agents, _dicts, _int)
except ImportError:  # imported as a package
    from .render_safe import (_INTERNAL_LABEL_RE, _SAFE_AGENT_FALLBACK, _safe_agent,
                              _MD_ESCAPE, _code, _safe, _fmt_list, _fmt_agents, _dicts, _int)

# Customer-surface SIZE BOUNDING (the per-section LINE caps + the line-length clamp + the FINAL assembly-level
# body cap that keeps the comment under GitHub's 65536-char limit) lives in render_bound.py — split out so a
# change to HOW we bound the body's size and a change to the verdict COPY don't collide on the same file (finer
# files = finer collision point). Re-exported here so `render.LIST_LINE_CAP` / `render._cap_comment_body` /
# `render.COMMENT_BODY_CAP` etc. keep working unchanged for callers and tests.
try:
    from render_bound import (LIST_LINE_CAP, COMMENT_BODY_CAP, COMMENT_LINE_CAP,
                              _THIS_PR_MARKER, _clamp_line, _cap_comment_body)
except ImportError:  # imported as a package
    from .render_bound import (LIST_LINE_CAP, COMMENT_BODY_CAP, COMMENT_LINE_CAP,
                               _THIS_PR_MARKER, _clamp_line, _cap_comment_body)

# The 一時停止 (PAUSE-and-ACKNOWLEDGE) tier — the pause/ACK state machine that actually changes a check's
# conclusion — lives in render_pauseack.py, split OUT so the ack logic and the verdict COPY (render_pr_check)
# stop colliding on one file (same leaf-split as render_safe / render_bound). Re-exported so render.ACK_LABEL /
# render.apply_pause_ack / etc. keep working unchanged for callers and tests.
try:
    from render_pauseack import (ACK_LABEL, _ACK_SNAP_PREFIX, _ACK_SNAP_RE, _ACK_REF_RE,
                                 _ack_partner_refs, _ack_coupling_paths, is_material_coupling,
                                 coupling_snapshot, _ack_snap_marker, prior_snapshot_from_comment, apply_pause_ack)
except ImportError:  # imported as a package
    from .render_pauseack import (ACK_LABEL, _ACK_SNAP_PREFIX, _ACK_SNAP_RE, _ACK_REF_RE,
                                  _ack_partner_refs, _ack_coupling_paths, is_material_coupling,
                                  coupling_snapshot, _ack_snap_marker, prior_snapshot_from_comment, apply_pause_ack)

# A PR check has a conclusion. Default policy is ENABLE-not-only-stop (止めるだけ=コンフリクトと同じ): we never
# hard-FAIL on a prediction AND we never silently BLOCK a merge. A team that just installed the App did not ask
# Veripsa to gate their merges, so the check is INFORMATIONAL by default — clear→success (green) and BOTH warn
# AND serialize→neutral (passes, advisory; a 'neutral' check never blocks a merge even where GitHub treats the
# check as required). The serialize/warn SIGNAL is carried in full by the check TITLE + the comment body
# ("⏸ Wait in line" / "Heads up" stay) — so steering is never lost, only the silent block is. A team that WANTS
# a hard gate marks the Veripsa check required themselves; that is their config, not our default.
#
# DISPLAY NOTE — "skipping" is NOT a bug, and NOT a check-vs-comment disagreement: the GitHub Checks API
# conclusion `neutral` is a real, successfully-posted result, but the `gh` CLI's `pr checks` view buckets a
# check's conclusion into pass / fail / pending / skipping / cancel — and it labels the `neutral` conclusion
# as "skipping". So a serialize/warn PR that posts BOTH a `neutral` check AND a "Wait in line" / "Heads up"
# comment can show "skipping" in `gh` while the comment posts: the two agree (both posted) — only `gh`'s word
# for `neutral` is the confusing one. This is intentionally NOT remapped here: a non-blocking conclusion that
# `gh` does NOT bury would be `action_required`, but that is a BLOCKING value (it gates a merge where the check
# is required) — exactly the silent block this policy forbids — and the (server) gate also pins these to
# {success, neutral}. The actionable signal lives in the comment + the check TITLE, both of which `gh`/the PR
# page show in full. (Where a customer wants serialize/warn loud in the checks row, that is a later, opt-in
# branch-protection / "hard gate" feature — not a conclusion remap.)
# 'serialize_soft' (PRECISION / anti-wallpaper): a SURVIVING direct collision that lands ONLY on append-mostly
# build/test-RUNNER or registration-list files (run_gates.sh and kin) — a trivial append-ORDER git conflict, not
# logic coupling. It is STILL surfaced (we never go silent on a real textual conflict) but as a low-stakes "heads
# up", not the hard "wait in line": same non-blocking conclusion, softer icon + copy. The moment the SAME PR pair
# also collides on a real source file, the engine emits hard 'serialize' instead — the low-value file can never
# MASK a real collision (the verdict logic in 80_contention.sql enforces that, not the renderer).
_VERDICT_CONCLUSION = {"clear": "success", "warn": "neutral", "serialize": "neutral", "serialize_soft": "neutral", "unknown": "neutral"}
_VERDICT_ICON = {"clear": "✓", "warn": "⚠", "serialize": "⏸", "serialize_soft": "⚠", "unknown": "❓"}
_VERDICT_TITLE = {
    "clear": "Clear — nothing else in flight touches this",
    "warn": "Heads up — your change meets other in-flight work",
    "serialize": "Wait in line — a direct collision is ahead",
    "serialize_soft": "Heads up — a minor overlap on a build/list file",
    "unknown": "Unknown — not enough signal to call this clear",
}

# MOAT — BLAST-RADIUS SEVERITY TIER (PO 2026-06-21 「file count はダメ」): the customer-facing copy must NEVER state
# the raw COUNT of downstream/dependent/foundation files (that leaks the graph's size — the moat). Instead the
# blast-radius wording is qualified by a SEVERITY WORD: once a change's downstream reach exceeds this threshold it
# is called "wide", below it "its/touches". The threshold is an internal display tier only (never shown), so a
# future tweak of the boundary changes no customer count — the customer sees a word, never a number.
_WIDE_BLAST_TIER = 3
_SYNTHETIC_HUB_LABELS = {"BODY", "DIFF_BODY", "SOURCE_BODY"}


# NEVER-CRASH on a malformed impact row (robustness, customer surface): the engine's JSON is TRUSTED to be
# well-SHAPED — every entry of `changes`/`clusters` and of each nested verdict-detail list (shared_foundation,
# depends_on_changing, collision_points, conflict_points, dampened_with) is read with `.get(...)`, so ONE non-dict
# entry (a JSON `null` in a jsonb array, a bare string/number from a partial or degraded read) used to raise
# AttributeError and take the WHOLE render down — and a render that throws means the App posts NO check AND NO
# comment on that PR at all (the failure is invisible to the customer, who is simply never told anything). A
# malformed row is not a reason to go dark; it is a reason to render the rows we CAN read and drop the ones we
# can't (honest-empty for that slice). `_dicts` keeps only the dict entries of a list — mirroring how
# `_fmt_list`/`_fmt_agents` already drop None/blank before formatting — so a stray non-dict simply does not render,
# rather than crashing the surface. Content-free: it only filters by type, it reads no bodies.
# _code / _safe / _fmt_list / _fmt_agents / _dicts / _int now live in render_safe.py (imported above). _fmt_collision_point is
# the ONE new finer-collision formatter — it composes those sanitizers, so it stays here with the verdict copy.
def _fmt_collision_point(cp: dict) -> str:
    """Name WHERE a direct collision actually is — the SYMBOL the two changes both edit (the finer win:
    "in `render_pr_check`"), or, when we could only place it at line granularity, the line RANGE
    ("at lines 140–160 of `render.py`"). Content-free: a path, a symbol NAME, and line numbers — never code.
    Returns '' when there is nothing finer than the file to say (the caller then keeps the file-level phrasing)."""
    if not cp:
        return ""
    sym = cp.get("symbol")
    path = cp.get("path")
    if sym:
        return f"in {_code(sym)}" + (f" (in {_code(path)})" if path else "")
    lo, hi = cp.get("line_lo"), cp.get("line_hi")
    if isinstance(lo, int) and isinstance(hi, int) and path:
        rng = f"line {lo}" if lo == hi else f"lines {lo}–{hi}"
        return f"at {rng} of {_code(path)}"
    return ""


def _display_hub_label(value) -> str | None:
    """Return a customer-safe hub path/symbol label, or None for synthetic placeholders.

    `dampened_with.via_hub` should normally be a content-free path or symbol name. During degraded/dogfood
    surfaces it can also carry placeholder tokens such as `BODY`; showing those reads like an internal parser
    artifact and confuses the user. Drop only the known placeholder family and let the surrounding copy fall back
    to "a shared file" / "a heavily-shared file". Real paths and symbols still render through `_fmt_list`.
    """
    if not isinstance(value, str):
        return None
    label = value.strip()
    if not label:
        return None
    if label.upper() in _SYNTHETIC_HUB_LABELS:
        return None
    return label


def _fmt_hub_clause(values) -> str:
    hubs = sorted({hub for hub in (_display_hub_label(v) for v in (values or [])) if hub})
    return f" ({_fmt_list(hubs)})" if hubs else ""


# A content-free CHANGE REF — 'PR-12' / 'BR-feature-x' — the SAME token shape webhook._behind_refs and
# _ack_partner_refs extract. A change ref is a number / branch slug, never code (content-free). Defined here (a
# duplicate of _ACK_REF_RE further down) so the land-order formatter below — which renders ABOVE the pause-ack
# section in this file — has it without a forward reference.
_LAND_REF_RE = re.compile(r"\b(?:PR-\d+|BR-[A-Za-z0-9_./\-]+)")


def _fmt_paths_no_count(items: list, limit: int = LIST_LINE_CAP) -> str:
    """MOAT-SAFE path-list formatter (PO 2026-06-21 「file count はダメ」). Same shape as render_safe._fmt_list — a
    capped, code-escaped, comma-joined list of paths — EXCEPT the overflow trailer is QUALITATIVE ("…, and more")
    instead of `_fmt_list`'s "(+N more)", because for a FILE/TOPOLOGY list (blast radius downstream paths, un-
    indexed paths) the raw "+N" is a count of graph files = a moat leak. _fmt_list still serves the surfaces where
    the count is allowed (agent/PR labels, reserved paths). Content-free: only path strings, code-escaped."""
    items = [x for x in (items or []) if x not in (None, "")]
    if not items:
        return ""
    shown = items[:limit]
    s = ", ".join(_code(x) for x in shown)   # backtick-safe — a path can't break out of its code span
    return s + (", and more" if len(items) > len(shown) else "")


def _label_ref_map(changes: list) -> dict:
    """Map every in-flight change's humanized LABEL → its content-free change REF ('PR-<n>'/'BR-<x>'), built from
    the engine's per-change rows (each carries both `label` and `change_id`). Used to recover the ref for a
    land-order entry whose humanized label is AUTHOR-ONLY — a BR- branch push that has not yet reconciled to a PR
    renders as the bare author name (e.g. 'example-user'), carrying NO ref token. Without this, several PRs by the same
    author collapse to '1. example-user / 2. example-user / …' — author noise with no PR identity. The change_id IS that
    content-free identity (a number / branch slug Veripsa already surfaces elsewhere — never code)."""
    out: dict = {}
    for c in _dicts(changes):
        cid = c.get("change_id")
        lbl = c.get("label")
        if isinstance(lbl, str) and lbl and isinstance(cid, str) and cid:
            out.setdefault(lbl, cid)   # first writer wins; engine change_ids are unique per change
    return out


def _draft_label_set(changes: list) -> set:
    """Build the set of in-flight LABELS whose change is currently in DRAFT (scout) state — read from the engine's
    per-change `is_draft` boolean. The renderer suffixes a partner label with " (draft)" when it appears in this
    set, so a reader can tell at a glance which referenced PRs are still iterating. Content-free (a label string +
    a boolean Veripsa already exposes). Missing field / non-dict row → not draft (graceful on older engines)."""
    out: set = set()
    for c in _dicts(changes):
        lbl = c.get("label")
        if isinstance(lbl, str) and lbl and bool(c.get("is_draft")):
            out.add(lbl)
    return out


def _mark_draft(labels: list, draft_set: set) -> list:
    """Decorate a list of partner labels with a ' (draft)' suffix when the partner is in DRAFT (scout) state. The
    decoration is added BEFORE _fmt_agents so the ' (draft)' suffix rides INSIDE the **bold** wrap the same way
    'PR-12' does (a reader sees **alice PR-12 (draft)** in one bolded unit). When the engine doesn't expose draft
    state (older surface / empty set), the labels pass through byte-identical."""
    if not draft_set or not labels:
        return list(labels or [])
    return [(lbl + " (draft)" if isinstance(lbl, str) and lbl in draft_set else lbl) for lbl in labels]


def _pr_link(s: str, repo: str) -> str:
    """Turn each 'PR-<n>' token in a rendered land-order label into a clickable Markdown link that KEEPS the
    'PR-<n>' text: '[PR-<n>](https://github.com/<repo>/pull/<n>)'. The comment lives on a PR in <repo> and the
    colliding PRs are in that SAME repo, so the link resolves. Applied ONLY on the non-fork land order (a fork
    PR's neighbor detail is redacted before any ref renders, so a link can never point a fork reader at a
    base-repo PR). No repo (an older/degraded surface) → text unchanged. Branch refs (BR-<x>) have no PR URL and
    are left as-is. Content-free (a repo path + a PR number); applied AFTER _safe_agent so the brackets survive."""
    if not repo:
        return s
    return re.sub(
        r"\bPR-(\d+)\b",
        lambda m: f"[PR-{m.group(1)}](https://github.com/{repo}/pull/{m.group(1)})",
        s,
    )


def _land_order_label(entry: str, ref_map: dict) -> str:
    """The display label for ONE suggested-land-order entry, GUARANTEED to carry a PR/branch REF. If the
    humanized label already contains a ref token ('alice PR-12'), it is returned unchanged. If it is author-only
    ('example-user', a BR- push not yet reconciled to a PR), append its recovered ref so a same-author cluster reads
    'PR-1, PR-2, PR-10, PR-11' instead of 'example-user, example-user, …'. No entry is ever a bare author with no ref:
    when no ref can be recovered (a degraded row), fall back to a generic content-free placeholder rather than a
    nameless author. The author name is kept ALONGSIDE the ref when present. Content-free (an author login + a
    change ref Veripsa already surfaces — never code)."""
    if not isinstance(entry, str):
        return "an in-flight change"
    if _LAND_REF_RE.search(entry):
        return entry                                   # already carries its ref — leave it
    ref = ref_map.get(entry)                            # recover the ref from the engine's change rows
    if ref:
        e = entry.strip()
        return f"{e} {ref}" if e else ref
    e = entry.strip()
    return f"{e} (in-flight change)" if e else "an in-flight change"


def _conflict_marker_findings(conflict_markers: list | None) -> list:
    """Sanitize + de-dup conflict-marker findings into a stable per-file list (PO 2026-06-25 dogfood hole-fix —
    PRs #111/#114). Input is the event-side findings list [{"path","line","kind"}, …]; the file-level pair gate
    (both `<<<<<<<` AND `>>>>>>>` present in a file) is already enforced upstream in
    conflict_markers_from_patch — so any path that shows up here genuinely has both markers in this PR's added
    lines (high precision, not just a stray `=======` divider).

    Returns a list of {"path": str, "first_line": int (smallest), "count": int} dicts, ONE per path, sorted by
    path for a stable render. Content-free (paths + line numbers + a count); ignores any malformed entry rather
    than crashing the renderer. Returns [] for None / non-list / empty input — the orchestrator then skips the
    escalation entirely (behavior-preserving for clean PRs)."""
    if not isinstance(conflict_markers, (list, tuple)):
        return []
    by_path: dict = {}
    for f in conflict_markers:
        if not isinstance(f, dict):
            continue
        path = f.get("path")
        line = f.get("line")
        if not isinstance(path, str) or not path:
            continue
        if not isinstance(line, int) or line <= 0:
            continue
        entry = by_path.get(path)
        if entry is None:
            by_path[path] = {"path": path, "first_line": line, "count": 1}
        else:
            entry["count"] += 1
            if line < entry["first_line"]:
                entry["first_line"] = line
    return sorted(by_path.values(), key=lambda e: e["path"])


# CONFLICT-MARKER DETECTION: per-file finding cap on render so a degenerate PR doesn't blow the comment body.
_CONFLICT_MARKER_PATH_CAP = 8


def _render_conflict_markers(L: list, findings: list) -> None:
    """Prepend the UNRESOLVED MERGE-MARKER block to the comment body when this PR's diff added literal
    `<<<<<<<` / `=======` / `>>>>>>>` lines (the PO 2026-06-25 dogfood hole — PRs #111/#114 / build-breaker).
    This block sits ABOVE every other section because it is the ONE hard-fail (action_required) signal the
    renderer can emit — every other contention/coupling/co-change line is advisory. CONTENT-FREE: we name the
    PATH and the FIRST line number of the marker; the line BODY is NEVER rendered (matches the upstream
    detector's contract — see conflict_markers_from_patch). Bounded by _CONFLICT_MARKER_PATH_CAP so a
    pathological PR with many marker-laden files renders the first N + "…, and more" (the same overflow shape
    every other path-list uses; PO 「file count はダメ」). No-op when findings is empty."""
    if not findings:
        return
    L.append("")
    if len(findings) == 1:
        f = findings[0]
        line_word = "line" if f["count"] == 1 else "lines"
        L.append(f"**⚠ Unresolved merge conflict marker in {_code(f['path'])} "
                 f"(first marker at line {f['first_line']}).** "
                 f"This will fail your build. Most likely a botched rebase — "
                 f"re-resolve the conflict and re-push.")
    else:
        shown = findings[:_CONFLICT_MARKER_PATH_CAP]
        more = len(findings) > len(shown)
        L.append("**⚠ Unresolved merge conflict markers in this PR — this will fail your build. "
                 "Most likely a botched rebase; re-resolve the conflict(s) and re-push.**")
        for f in shown:
            line_word = "line" if f["count"] == 1 else "lines"
            L.append(f"  - {_code(f['path'])} — first marker at line {f['first_line']}")
        if more:
            L.append("  - …, and more")


def _conflict_marker_summary_line(findings: list) -> str:
    """The one-line summary that lands in the GitHub check's title row when this PR has unresolved markers.
    Names the FIRST path (or `<N> files` when there are many) + the kind of failure. Content-free (path +
    count). Used by both the no-reservation early-return AND the main path's summary override."""
    if not findings:
        return ""
    if len(findings) == 1:
        f = findings[0]
        return (f"⚠ Unresolved merge conflict marker in {_code(f['path'])} (first at line {f['first_line']}). "
                f"This will fail your build — re-resolve the conflict and re-push.")
    shown_path = findings[0]["path"]
    return (f"⚠ Unresolved merge conflict markers in {len(findings)} files (first in {_code(shown_path)}). "
            f"This will fail your build — re-resolve the conflicts and re-push.")


def _build_conflict_marker_result(findings: list, branch: str) -> dict:
    """The action_required short-circuit result for the no-reservation early-return + the bare-bones path.
    Returns a {conclusion, title, summary, comment} dict matching render_pr_check's contract — same as the
    main path, but skipping the contention render (we only carry the marker block). Content-free."""
    L: list[str] = []
    L.append(f"### Veripsa — heading to {_code(branch)}")
    _render_conflict_markers(L, findings)
    L.append("")
    L.append("<sub>Veripsa records what is heading to "
             f"{_code(branch)} and who reserved what — it does not assert correctness. "
             "Advisory by default; this unresolved-merge-marker check is a hard fail because the marker "
             "will break your build.</sub>")
    return {
        "conclusion": "action_required",
        "title": "Veripsa — Unresolved merge conflict markers",
        "summary": _conflict_marker_summary_line(findings),
        "comment": _cap_comment_body(L),
    }


def _fmt_conflict_location(cp: dict) -> str:
    """Name WHERE a likely git merge conflict sits — the file + a content-free "near line N" (the overlap /
    same-insertion-point line). Returns '' when there is no usable location. Line numbers + path only, never code."""
    if not cp:
        return ""
    path = cp.get("path")
    line = cp.get("line")
    if not path:
        return ""
    return (f"{_code(path)} near line {line}" if isinstance(line, int) else f"{_code(path)}")


def watching_check(files: int = 0, edges: int = 0, branch: str = "main", indexing: bool = False,
                   over_cap: bool = False) -> dict:
    """The ONE-TIME 'Veripsa is now watching' signal posted on a freshly-onboarded repo's default-branch HEAD.

    THE DEAD FIRST IMPRESSION it fixes: on a fresh install a repo with NO open PRs gets nothing GitHub-visible —
    the user grants code-read and sees total silence (looks broken). This is a content-free, ADVISORY check-run
    (conclusion `neutral` — it never blocks anything; the default branch is not a merge target so this can never
    gate a merge) that says, in plain language, 'I indexed your code and I'll flag pre-merge overlap on your next
    PR.' Content-free — no raw counts (graph file/edge counts are never customer-facing), no path / symbol /
    body.

    Three cases:
      over_cap=True  — the repo genuinely exceeds the file-count threshold for this tier.  An empty graph is
                       stored (honest 'unknown').  We must NOT promise that 'indexing will complete on the next
                       push' because it will NOT (every push re-trips the cap).  Honest copy surfaces the limit.
      indexing=True  — a durable background convergence turn has been queued. The isolated graph worker will
                       update this same Check after exact-HEAD indexing commits; no further customer push is
                       required.
      default        — the repo was fully indexed; report the result honestly.

    Returns {conclusion, title, summary} for an idempotent check upsert."""
    if over_cap:
        # HONEST OVER-CAP COPY: this repo exceeds the indexing threshold on the current tier.  Do NOT promise
        # that analysis will complete — it will not (every push re-trips the cap and re-stores an empty graph).
        # Honest, content-free, no false promise, no jargon beyond what is necessary.  Uses 'may' not 'will'.
        summary = (
            "Veripsa is watching this repository, but it may not index it on the current tier — "
            "the repository exceeds the file-count threshold Veripsa indexes. "
            "Pull request checks may surface 'unknown' rather than a concrete overlap verdict until "
            "the repository is within the indexable threshold.\n\n"
            "Veripsa records what is heading to your default branch and flags overlapping in-flight work before "
            "merge — it does not assert correctness. Advisory by default; your branch-protection policy decides "
            "what blocks."
        )
    elif indexing:
        summary = (
            "Veripsa is now watching this repository. Indexing is in progress in the background — no additional "
            "push is required. Veripsa will update this Check after the current default-branch view is ready.\n\n"
            "Veripsa records what is heading to your default branch and flags overlapping in-flight work before "
            "merge — it does not assert correctness. Advisory by default; your branch-protection policy decides "
            "what blocks."
        )
    else:
        summary = (
            f"Veripsa is now watching this repository — your {_code(branch)} branch is indexed. "
            "It will flag overlapping in-flight work on your next pull request.\n\n"
            "Veripsa records what is heading to your default branch and flags overlapping in-flight work before "
            "merge — it does not assert correctness. Advisory by default; your branch-protection policy decides "
            "what blocks."
        )
    return {"conclusion": "neutral", "title": "Veripsa is now watching", "summary": summary}


def quota_paused_check() -> dict:
    """The check half of the EARLY-ACCESS FAIR-USE WALL note. When an account crosses the free line
    the DB gate refuses further writes and the graph silently goes stale — the user sees the bot quietly stop,
    which reads as 'broke'. This turns the wall into a VISIBLE service-limit event. ADVISORY (`neutral` — never a
    blocking/failing conclusion; Veripsa never gates a merge), content-free (no counts that name the account's
    structure), jargon-free. Returns {conclusion, title, summary} for an idempotent upsert on the PR's head."""
    return {"conclusion": "neutral", "title": "Veripsa paused — early-access limit reached",
            "summary": quota_paused_comment_body()}


def quota_paused_comment_body(branch: str = "main") -> str:
    """The PR-comment body for the EARLY-ACCESS FAIR-USE WALL. Posted/patched in place on the PR
    conversation (same idempotent upsert the normal verdict uses — no flap) so the moment the account crosses
    the free line becomes a visible, honest limit prompt instead of a silent break. Content-free (no
    paths/counts that name the account's code), advisory, jargon-free."""
    return (
        "### Veripsa — paused on this repository\n"
        "**Veripsa paused here — the early-access fair-use limit has been reached.**\n\n"
        "Veripsa has stopped updating its view of this repository while the free early-access band is at its "
        "limit, so coordination on new pull requests is on hold. Reduce the covered scope or contact support if "
        "you need more room. Future paid plans are expected through GitHub Marketplace.\n\n"
        "<sub>Veripsa records what is heading to "
        f"{_code(branch)} and flags overlapping in-flight work before merge — it does not assert correctness. "
        "Advisory by default; your branch-protection policy decides what blocks.</sub>"
    )


def unread_files_check() -> dict:
    """The check half of the EMPTY-FILES-API WRONG-CLEAR guard (an audited silent-miss). GitHub's Files API can
    return an EMPTY list on a 200 WITHOUT raising (a proxy/CDN edge serving an empty body, a 204, eventual-
    consistency right after a push). An empty file list is INDISTINGUISHABLE from a genuine 0-file PR, so the
    brain would clear the PR (`success`) and release its lanes — a SILENT MISS over a real overlap. When the PR's
    own `changed_files` count says it DID change files, the App posts THIS advisory `neutral` instead of a clear:
    it could not read the changed files, so it did NOT analyze (it did not release any lanes). ADVISORY (`neutral`
    — never blocks; Veripsa never gates a merge), content-free (no paths/counts that name the code), jargon-free.
    Returns {conclusion, title, summary} for an idempotent upsert on the PR's head."""
    return {"conclusion": "neutral", "title": "Veripsa — changed files not read, not analyzed",
            "summary": unread_files_comment_body()}


def unread_files_comment_body(branch: str = "main") -> str:
    """The PR-comment body for the EMPTY-FILES-API WRONG-CLEAR guard. Posted/patched in place on the PR
    conversation (the same idempotent upsert the normal verdict uses — no flap) so a transient read failure is an
    HONEST 'not analyzed' note rather than a silent, false 'clear to land'. The next event (a push / synchronize)
    re-reads the files and Veripsa picks the coordination back up. Content-free (no paths/counts that name the
    code), advisory, jargon-free."""
    return (
        "### Veripsa — heading to " + _code(branch) + "\n"
        "**Couldn't read this pull request's changed files — it was not analyzed.**\n\n"
        "GitHub returned no file list for this pull request even though it reports changed files, so Veripsa "
        "did not analyze it this time and did not clear it. This is usually transient — the next push or update "
        "will re-read the files and Veripsa will flag any overlapping in-flight work then.\n\n"
        "<sub>Veripsa records what is heading to "
        f"{_code(branch)} and flags overlapping in-flight work before merge — it does not assert correctness. "
        "Advisory by default; your branch-protection policy decides what blocks.</sub>"
    )


def stale_graph_unknown_check() -> dict:
    """The check half of the STALE-GRAPH WITHHELD-CLEAR guard (G1) — the GENERAL case of the free-tier wall's
    silent-stale symptom. When main's stored graph is BEHIND its current HEAD (a push was missed/lagged) and the
    pre-analysis self-heal could NOT bring it current for ANY reason OTHER than the early-access wall (a transient
    GitHub read error, a network/timeout, a permission blip, a re-ingest failure), the coordination view is
    computed against a STALE baseline — so a would-be `clear` cannot be trusted (a real overlap whose graph link
    only exists at the newer HEAD would be missed → a SILENT false clear). Rather than emit that unconfirmed
    clear, Veripsa withholds it and posts THIS advisory `neutral` instead: the pull request is treated as unknown,
    not cleared, and re-runs when the graph catches up. ADVISORY (`neutral` — never blocks; Veripsa never gates a
    merge), content-free (no paths/counts that name the code), jargon-free. RECALL-SAFE: this ONLY replaces a
    would-be clear — a heads-up / wait-in-line verdict on the same stale graph is still shown (over-flagging is
    safe). Returns {conclusion, title, summary} for an idempotent upsert on the PR's head."""
    return {"conclusion": "neutral", "title": "Veripsa — code graph not confirmed current, not cleared",
            "summary": stale_graph_unknown_comment_body()}


def stale_graph_unknown_comment_body(branch: str = "main") -> str:
    """The PR-comment body for the STALE-GRAPH WITHHELD-CLEAR guard (G1). Posted/patched in place on the PR
    conversation (the same idempotent upsert the normal verdict uses — no flap) so a transient inability to bring
    the graph up to date is an HONEST 'not cleared this time' note rather than a false 'clear to land' over a
    stale view. The next event (a push / synchronize) refreshes the view and Veripsa picks the coordination back
    up. Content-free (no paths/counts that name the code), advisory, jargon-free."""
    return (
        f"### Veripsa — heading to {_code(branch)}\n"
        f"**Couldn't confirm the code graph is up to date for {_code(branch)} — this pull request was not "
        "cleared.**\n\n"
        f"Veripsa's view of {_code(branch)} is behind its latest commit and could not be refreshed this time, so "
        "this pull request is treated as unknown rather than cleared. This is usually transient — the next push "
        "or update refreshes the view and Veripsa will flag any overlapping in-flight work then.\n\n"
        "<sub>Veripsa records what is heading to "
        f"{_code(branch)} and flags overlapping in-flight work before merge — it does not assert correctness. "
        "Advisory by default; your branch-protection policy decides what blocks.</sub>"
    )


def stale_graph_head_unknown_check() -> dict:
    """The check half of the HEAD-UNRESOLVABLE WITHHELD-CLEAR guard (G1 tail) — a SIBLING of
    stale_graph_unknown_check for the ONE currency state the shared `_graph_degraded` predicate deliberately
    EXCLUDES. `stale_graph_unknown_check` covers "the stored graph is BEHIND a RESOLVABLE HEAD and self-heal did
    not close the gap"; THIS covers "main's HEAD could not be read AT ALL" (a repo_default_branch_head API
    failure → self_heal_main_graph returns reason 'main HEAD unresolvable', head_sha None, behind None). When HEAD
    is unknown Veripsa cannot compare the stored graph's commit_sha to it, so it cannot CONFIRM the stored view is
    current — and a would-be `clear` computed over an unconfirmable-currency baseline cannot be trusted (a real
    overlap whose graph link only exists at the true, unread HEAD would be missed → a SILENT false clear). Rather
    than emit that unconfirmed clear, Veripsa withholds it and posts THIS advisory `neutral` instead: the pull
    request is treated as unknown, not cleared, and re-runs when HEAD becomes readable again. Distinct wording
    from the behind-HEAD sibling ("currency not confirmed" vs "behind its latest commit") because here we
    genuinely could not READ the latest commit — we do not claim it is behind, only that we could not confirm it.
    ADVISORY (`neutral` — never blocks; Veripsa never gates a merge), content-free, jargon-free. RECALL-SAFE:
    this ONLY replaces a would-be clear — a heads-up / wait-in-line verdict is still shown (over-flagging is
    safe). Returns {conclusion, title, summary} for an idempotent upsert on the PR's head."""
    return {"conclusion": "neutral", "title": "Veripsa — code graph currency not confirmed, not cleared",
            "summary": stale_graph_head_unknown_comment_body()}


def stale_graph_head_unknown_comment_body(branch: str = "main") -> str:
    """The PR-comment body for the HEAD-UNRESOLVABLE WITHHELD-CLEAR guard (G1 tail). Posted/patched in place on
    the PR conversation (the same idempotent upsert the normal verdict uses — no flap) so a transient inability
    to READ main's latest commit is an HONEST 'not cleared this time' note rather than a false 'clear to land'
    over a view whose currency could not be confirmed. Distinct from stale_graph_unknown_comment_body: we do NOT
    claim the graph is behind (we could not read HEAD to know) — only that its currency could not be confirmed.
    The next event (a push / synchronize, once HEAD is readable) refreshes the view and Veripsa picks the
    coordination back up. Content-free (no paths/counts that name the code), advisory, jargon-free."""
    return (
        f"### Veripsa — heading to {_code(branch)}\n"
        f"**Couldn't confirm the code graph is current for {_code(branch)} — this pull request was not "
        "cleared.**\n\n"
        f"Veripsa couldn't read the latest commit on {_code(branch)} this time, so it could not confirm its view "
        "of the code is up to date. This pull request is treated as unknown rather than cleared. This is usually "
        "transient — the next push or update re-checks and Veripsa will flag any overlapping in-flight work "
        "then.\n\n"
        "<sub>Veripsa records what is heading to "
        f"{_code(branch)} and flags overlapping in-flight work before merge — it does not assert correctness. "
        "Advisory by default; your branch-protection policy decides what blocks.</sub>"
    )


def branch_inventory_unknown_check(branch: str = "main") -> dict:
    """Advisory result when a BR-* coupling exists but GitHub branch truth is temporarily unavailable."""
    body = branch_inventory_unknown_comment_body(branch)
    return {"conclusion": "neutral", "title": "Veripsa — branch state not verified, retrying",
            "summary": body}


def branch_inventory_unknown_note() -> str:
    """Additive copy used when independent PR evidence remains valid but a BR-* participant is unverified."""
    return (
        "**Branch verification pending:** GitHub's live branch list could not be verified safely. Veripsa kept "
        "the branch record and will retry automatically; no acknowledgement or manual branch operation is "
        "needed for that unverified branch."
    )


def branch_inventory_unknown_comment_body(branch: str = "main") -> str:
    """No-ACK copy for an unverified branch collision; the service retries and preserves DB state safely."""
    return (
        "### Veripsa — heading to " + _code(branch) + "\n"
        "**An overlapping branch could not be verified, so no coordination verdict was made this time.**\n\n"
        + branch_inventory_unknown_note() + " Veripsa will retry on the next pull-request update or service "
        "restart.\n\n"
        "<sub>Veripsa records what is heading to "
        f"{_code(branch)} and flags overlapping in-flight work before merge — it does not assert correctness. "
        "Advisory by default; your branch-protection policy decides what blocks.</sub>"
    )


def cleared_comment_body(branch: str = "main") -> str:
    """The comment body that REPLACES a stale warn/serialize comment once a PR drops back to 'clear' (its blocker
    withdrew/landed, the coupling cleared). We never DELETE the comment (the thread is an audit record); we
    rewrite it to say the contention resolved, so the author is never left reading "wait in line behind PR-X"
    for a PR-X that no longer exists. content-free."""
    return (
        f"### Veripsa — heading to {_code(branch)}\n"
        "**✓ Cleared — the earlier overlap has resolved**\n\n"
        "A change this PR was waiting behind or coupled to has landed or withdrawn. Nothing else in flight now "
        "touches your files, so no blocking PR traffic is visible.\n\n"
        "<sub>Veripsa records what is heading to "
        f"{_code(branch)} and who reserved what — it does not assert correctness. "
        "Advisory by default; your branch-protection policy decides what blocks.</sub>"
    )


# The plan/limits target. ONE place for the URL so fair-use copy never scatters it.
# Veripsa Core is GitHub App first and free while in early access. Future paid billing is expected through
# GitHub Marketplace, not a direct Paddle checkout.
LIMITS_URL = "https://veripsa.com/pricing"


def coverage_nudge_line(coverage: dict | None) -> str | None:
    """An HONEST, content-free early-access limit nudge for the account's coverage.

    `coverage` is core.account_coverage_surface(): {plan, file_limit, file_count, over_by, near}. Returns ONE
    GitHub-markdown line, or None when there is nothing to nudge — under the line, unlimited (Enterprise / an
    unmapped paid plan → file_limit is None: NEVER nudge), or a missing/garbled surface. ADVISORY: the
    line states a fact (the codebase is past the plan's analyzed-file allowance) and points to plan/limit details; it
    never blocks a merge. Content-free + PO 2026-06-19 "ユーザーに見せるのは%だけ": shows a coverage PERCENTAGE + the
    plan label only — never a raw file/Unit count (the count and the internal billing metric stay private — moat),
    never a path or a body. The App appends
    it to the ACTING PR's check SUMMARY (not a new comment, not the neighbor refresh) so it is gentle, not spam.
    """
    if not isinstance(coverage, dict):
        return None
    limit = coverage.get("file_limit")
    if limit is None:                       # unlimited (Enterprise / unmapped paid) — never nudge
        return None
    try:
        over = int(coverage.get("over_by") or 0)
        count = int(coverage.get("file_count") or 0)
        near = bool(coverage.get("near"))
        limit = int(limit)
    except (TypeError, ValueError):
        return None
    plan = str(coverage.get("plan") or "free")
    if over > 0:
        # over_by>0 ⇒ count>limit ⇒ Veripsa covers <100% of the codebase. Show the COVERAGE % (how much of their
        # code is watched), never the raw count/limit (those + the internal billing metric stay private — moat).
        covered = max(1, min(99, round(limit / count * 100))) if count > 0 else 0
        return (f"📈 Veripsa is covering about **{covered}%** of your codebase on the **{plan}** plan — the rest "
                f"isn't watched yet. [Plan & limits]({LIMITS_URL}) explains the free early-access band, or exclude "
                f"files you don't need Veripsa to watch (generated code, vendored deps). Advisory; your merges are "
                f"never blocked.")
    if near:
        # near the limit but under it: show how full the plan is, as a % (never the raw count/limit).
        used = min(99, round(count / limit * 100)) if limit > 0 else 0
        return (f"📈 You're at about **{used}%** of your **{plan}** plan's coverage. "
                f"[Plan & limits]({LIMITS_URL}) explains the free early-access band — advisory, never blocks.")
    return None


# How many overlapping paths to NAME inline before collapsing the rest to "+N more" — same capped, content-free
# style as the shared_foundation / depends_on_changing lists (a handful of paths, never the whole set, never a
# body). Keeps the line short on a wide overlap and bounds the customer text.
_STALE_NUDGE_PATH_CAP = 3


def stale_base_nudge_line(branch_changed_paths, pr_changed_paths) -> str | None:
    """POST-MERGE STALENESS nudge (a pre-conflict heads-up). Veripsa's collision detector compares CONCURRENTLY-
    open PRs — it is BLIND to a change that has ALREADY MERGED into the protected branch. So a PR whose files were
    touched by a landing that happened AFTER the PR branched only discovers the conflict at rebase/merge time (too
    late). This intersects the files that changed on the protected branch SINCE this PR's base
    (`branch_changed_paths`, from github_rest.compare_changed_paths) with the PR's OWN changed files
    (`pr_changed_paths`): a non-empty overlap means the branch moved under the PR on files it is editing.

    Returns ONE GitHub-markdown line, or None when there is no overlap (nothing to nudge) / either list is
    empty/garbled. ADVISORY — it states a fact (these files moved on the branch) and suggests a rebase; it NEVER
    blocks (the App's check stays `neutral`; the customer's branch-protection decides what blocks). CONTENT-FREE:
    only path strings (the author's OWN changed files) — never a diff, never a body, and never a raw file COUNT
    (PO 2026-06-21 「file count はダメ」): the named paths are CAPPED (≤ _STALE_NUDGE_PATH_CAP, then a qualitative
    "and more") and code-escaped via `_code`, exactly like coverage_nudge_line /
    shared_foundation. The App appends it to the ACTING PR's check SUMMARY only (not a comment, not the neighbor
    refresh) so it is gentle, not spam. JARGON-FREE + HONEST: rebasing does NOT make the overlap vanish — it moves
    the work onto your branch NOW (deliberate) instead of surfacing as a blocked merge later. So the copy says
    'work through any overlap now, not at merge time' (a conditional, not a promise the conflict is avoided) and
    'never blocks', not a guarantee. (PO 2026-07-24: 'avoid a conflict' read as 'rebase → no conflict', which is
    false when edits overlap the landed lines; the nudge de-risks the TIMING/surprise, not the conflict itself.)"""
    # Defensive: accept only real, non-empty path strings from a LIST/TUPLE/SET (a malformed event — a non-
    # iterable, a string, junk entries — must yield None, never crash; this is an advisory add-on). A bare str is
    # rejected as a whole (iterating it would yield characters = nonsense paths).
    def _paths(x):
        if not isinstance(x, (list, tuple, set)):
            return set()
        return {p for p in x if isinstance(p, str) and p}
    branch_set = _paths(branch_changed_paths)
    pr_set = _paths(pr_changed_paths)
    overlap = sorted(branch_set & pr_set)
    if not overlap:
        return None
    n = len(overlap)
    shown = overlap[:_STALE_NUDGE_PATH_CAP]
    listed = ", ".join(_code(p) for p in shown)
    # MOAT (PO 2026-06-21 「file count はダメ」): the overlapping paths are NAMED (the author's own changed files —
    # content-free), but neither the count of them ("N files") nor the "+N more" overflow may be a raw NUMBER (a
    # file count). Lead with the named paths and a qualitative "and more"; the plural reads honestly off `n`.
    if n > len(shown):
        listed += ", and more"
    file_word = "file" if n == 1 else "files"
    verb = "has" if n == 1 else "have"
    return (f"⏳ **Heads up — `main` moved under you.** {file_word.capitalize()} you're editing ({listed}) {verb} "
            f"changed on `main` since your branch started; rebase before merge so you work through any overlap on "
            f"your branch now, not at merge time — and check whether that change already does what you're adding, so "
            f"you don't redo it. Advisory — never blocks.")


def _redacted_fork_summary(verdict: str, icon: str) -> str:
    """The ONE-LINE check summary for a FORK PR — the verdict's SEVERITY kept, every cross-PR identifier and
    base-repo path/symbol DROPPED. A fork PR's comment posts on the base-repo conversation, which the external
    fork author can read; the normal summary names other in-flight base-repo PRs (`serialize_behind`/`contested_
    with`) + base-repo paths (`finer_point`), which would leak the private base repo's in-flight structure to an
    outside contributor. So a fork gets a GENERIC, content-free severity line — coordinate, but no specifics."""
    if verdict in ("serialize", "serialize_soft"):
        return (f"{icon} This PR overlaps other in-flight work in this repo — coordinate with the maintainers "
                "before merge.")
    if verdict == "warn":
        return (f"{icon} This PR's changes meet other in-flight work in this repo — coordinate with the "
                "maintainers before merge.")
    if verdict == "unknown":
        return (f"{icon} Veripsa can't fully analyze this PR's overlap — coordinate with the maintainers before "
                "merge.")
    # clear (incl. clear-with-co-signal): nothing is blocking THIS PR; if there is overlap nearby, the generic
    # coordinate note (in the comment) covers it without naming the base repo's other PRs.
    return (f"{icon} No blocking PR traffic is visible. Veripsa flags overlap before merge "
            "(advisory — it does not assert correctness).")


def _redacted_fork_comment(branch: str, icon: str, verdict_title: str, verdict: str) -> str:
    """The REDACTED PR comment for a FORK PR. Keeps THIS PR's own verdict header + a GENERIC coordinate line, and
    DROPS every cross-PR identifier (other PR refs / author logins from serialize_behind / queued_behind /
    contested_with, the cluster suggested-order list, dampened_with's other-PR name + hub) and every base-repo
    path/symbol detail (finer_point, shared_foundation / blast-radius paths). A fork PR's comment is readable by
    the external contributor, so naming the private base repo's other in-flight PRs / file structure would leak
    it. Same advisory framing + content-free contract as the full comment; just no specifics.

    HONEST per verdict (audit r4): a 'clear' fork PR that STILL posts a comment (it is a lane HOLDER others wait
    behind / touches a shared foundation / was truncated) must NOT claim it 'overlaps other in-flight work' —
    that flatly contradicts its own 'Clear' header. A clear PR gets a coordinate-neutral line that asserts no
    overlap; warn/serialize/serialize_soft/unknown keep the overlap-coordinate line."""
    if verdict == "clear":
        coordinate_line = (
            "No other in-flight work currently requires coordination on this PR. Veripsa keeps the per-PR "
            "coordination detail out of this comment on a pull request from a fork (the maintainers see the "
            "full picture)."
        )
    else:
        coordinate_line = (
            "This PR overlaps other in-flight work in this repo — coordinate with the maintainers before merge. "
            "(Veripsa shows the full overlap detail to the repo maintainers; on a pull request from a fork it "
            "keeps the specifics out of this comment.)"
        )
    return (
        f"### Veripsa — heading to {_code(branch)}\n"
        f"**{icon} {verdict_title}**\n\n"
        f"{coordinate_line}\n\n"
        "<sub>Veripsa records what is heading to "
        f"{_code(branch)} and flags overlap before merge — it does not assert correctness. "
        "Advisory by default; your branch-protection policy decides what blocks.</sub>"
    )


# ──────────────────────────────────────────────────────────────────────────────────────────────────────────
# PER-SECTION RENDERERS for render_pr_check, extracted so the orchestrator below stays THIN and each customer-
# surface block is one cohesive, separately-readable unit (the same finer-collision-point split as render_safe /
# render_bound / render_pauseack — finer functions = a finer place to change one section's copy without touching
# the others). EACH is PURE + CONTENT-FREE and BEHAVIOR-PRESERVING — the bodies are the verbatim statements lifted
# from the old inline render_pr_check, so the assembled check/comment is byte-identical. The comment-building
# helpers take the running line list `L` and APPEND to it in place (the same "\n".join(L) idiom the orchestrator
# used inline), so section ORDER and the leading-blank paragraph breaks are unchanged.
# ──────────────────────────────────────────────────────────────────────────────────────────────────────────


def _effective_verdict(verdict: str, unknown_paths: list, dampened_with: list, verifiable_paths: list,
                       added_paths: list | None = None, truncated: bool = False,
                       engine_unknown: bool = True) -> str:
    """PER-PATH verdict (the BLANKET-UNKNOWN root fix, 2026-06-23). The engine emits ONE verdict per change, and
    its ladder (80_contention.sql) returns 'unknown' the moment ANY reserved path is absent from main's graph — a
    NEW file (a test, a new module), an unsupported language, an un-indexed dir. But that 'unknown' was BLANKET: a
    PR that ALSO modifies coupled EXISTING (in-graph) files got the whole-PR "❓ Not analyzed", SUPPRESSING the
    real verdict on the in-graph part. Since many PRs add at least one new file, blanket unknown could hide a
    useful verdict for the in-graph subset.

    This computes the verdict for the VERIFIABLE (in-graph) subset of the change. It is a NARROW, evidence-gated
    transform: an engine 'unknown' becomes 'clear' ONLY when every unknown path is positively identified as NEW in
    this PR by the Files API, at least one verifiable path exists, no dampened coupling exists, and the analyzed
    path set was not truncated:

      - The engine's ladder already lets serialize / serialize_soft / warn WIN over the unknown-path test (a real
        collision/coupling on an in-graph file is NEVER masked by a new path — it returns serialize/warn, not
        unknown). So a non-'unknown' verdict is ALREADY the verifiable part's verdict — pass it straight through.
      - 'unknown' has TWO honest causes: (a) new/not-in-graph paths (`unknown_paths`), and (b) a REAL coupling that
        hub-dampening suppressed on an IN-GRAPH file (`dampened_with`, AUDIT3). Cause (b) is a genuine "not clear"
        on a verifiable file, so when `dampened_with` is present we KEEP 'unknown' (never claim the in-graph part
        is clear when a suppressed coupling runs through it).
      - With ONLY cause (a) — every unknown path is present in `added_paths`, no dampened coupling, a known path,
        and no unparsed remainder — every VERIFIABLE path passed the engine's full collision/contest/dampen ladder
        with no signal. Downgrade the blanket 'unknown' to 'clear' for the verdict/conclusion/summary, and surface
        the new paths SEPARATELY (count-free, PO 「file count はダメ」).
      - A missing/failed `added_paths` read cannot prove that an unknown path is new. Likewise, one unknown path
        absent from `added_paths` is an existing graph gap. Both stay honest-'unknown'; missing evidence is not
        evidence for Clear.
      - A missing/invalid engine verdict is normalized to `unknown` for display, but it is not an engine-provided
        Unknown and cannot earn promotion. `engine_unknown` preserves that provenance through normalization.
      - A truncated path set has an unparsed remainder. A would-be Clear (including an engine-provided `clear`)
        becomes 'unknown'; a known warn/serialize signal still passes through.
      - DEGENERATE GUARD: if EVERY reserved path is unknown (no verifiable subset survives), there is nothing to
        call clear — stay honest-'unknown' (today's behavior). KEEP the honest-unknown principle: we never claim
        'safe' for a path we couldn't verify; the only change is that unverifiable paths stop blanking the whole
        verdict.
    """
    # DAMPENING-HONEST RENDER (Round-11 audit, 2026-06-26): a non-empty `dampened_with` means hub-dampening
    # SUPPRESSED a real coupling — the silence must be visible. If the engine somehow handed the renderer a
    # non-'unknown' verdict (a 'clear' from the corroboration path, a stale read, or a wire mismatch) WHILE
    # `dampened_with` rows are present AND uncorroborated, we MUST NOT pass that 'clear' through — that is the
    # exact "silently green on a real-but-suppressed coupling" failure mode the audit names. The four obligations
    # below the helper (the `_render_warn_unknown_detail` "suppressed to avoid noise — treat as unknown, not
    # clear. … through a heavily-shared file (<hub>)" copy) only render under `verdict in {warn, unknown}` —
    # forcing 'unknown' here makes them fire, naming the hub. CORROBORATED dampened_with (the AUDIT3 relaxation:
    # structural edge + co-change agree) earns its own 'warn' upstream and renders the "two independent signals"
    # copy — so we pass non-'unknown' through whenever EVERY dampened_with row is corroborated (the engine's
    # earned verdict), and only escalate when AT LEAST ONE row is uncorroborated (the real silence-visible case).
    if verdict != "unknown":
        if dampened_with and any(not d.get("corroborated") for d in dampened_with):
            return "unknown"                 # silence-visible: a suppressed uncorroborated coupling rides under a non-unknown verdict → force honest 'unknown'
        if verdict == "clear" and truncated:
            return "unknown"                 # partial analysis has an unparsed remainder → never render a confident Clear
        return verdict                       # serialize/warn/clear already ARE the verifiable part's verdict (ladder)
    if dampened_with:
        return "unknown"                     # a real suppressed coupling on an IN-GRAPH file → genuinely not 'clear'
    if truncated:
        return "unknown"                     # even proven-new unknown paths cannot clear an unparsed remainder
    if not engine_unknown:
        return "unknown"                     # missing/invalid engine state was normalized for display, not earned
    added_set = {p for p in (added_paths or []) if isinstance(p, str) and p}
    unknown_paths_are_proven_new = bool(unknown_paths) and all(
        isinstance(p, str) and p in added_set for p in unknown_paths
    )
    if unknown_paths_are_proven_new and verifiable_paths:
        return "clear"                       # ONLY proven additions are unknown; at least one analyzed path is clear
    return "unknown"                         # graph gap / missing added-path evidence / no known path → honest unknown


def _split_unknown_paths(unknown_paths: list, added_paths: list | None) -> tuple:
    """SPLIT (PO 2026-06-25 honest-verdict refinement): partition `unknown_paths` into the two
    semantically-distinct cases the old single "Unknown" copy lumped together.
      • new_in_pr  — paths that are NEW in this PR (Files-API status='added'). EXPECTED to be absent from main's
        graph (main does not have them yet); coupling becomes computable after merge. NOT a warning.
      • gap_paths  — paths NOT marked added → existed in main but absent from Veripsa's graph. A real extractor
        gap (unsupported language, un-indexed area, .meta/asset/etc.). Warrants investigation.
    Degenerate / fail-open: when `added_paths` is None or empty (the renderer wasn't told — e.g. neighbor refresh,
    fork variant, older Fake client), EVERY path falls into `gap_paths` so the caller can render the legacy lumped
    "Unknown" copy unchanged — strictly behaviour-preserving for callers that don't yet pass the data. Returns
    (new_in_pr, gap_paths) as lists in the original `unknown_paths` order (preserves the deterministic order the
    engine emits)."""
    added_set = {p for p in (added_paths or []) if isinstance(p, str)}
    if not added_set:
        return [], list(unknown_paths or [])
    new_in_pr: list = []
    gap_paths: list = []
    for p in (unknown_paths or []):
        (new_in_pr if p in added_set else gap_paths).append(p)
    return new_in_pr, gap_paths


def _render_unverified_paths_note(L: list, unknown_paths: list, split_from_verdict: bool,
                                  added_paths: list | None = None) -> None:
    """The SEPARATE, additive honesty note for new / not-in-graph paths — rendered alongside ANY verifiable verdict
    so the unverifiable paths are reported HONESTLY without suppressing it (the per-path root fix). When the whole
    change is 'unknown' (no verifiable subset), `_render_warn_unknown_detail` already owns this copy and this is a
    no-op (split_from_verdict is False) — so the note is never duplicated. When the verifiable part got its own
    verdict (clear/warn/serialize) but the PR ALSO touches new paths, THIS adds the standalone note. Content-free
    (the paths, code-escaped; never the COUNT — `_fmt_paths_no_count` ends "…, and more", PO 「file count はダメ」).

    SPLIT (PO 2026-06-25): when `added_paths` is supplied, partitions the lumped "Unknown" arm into the EXPECTED
    "(a) new in this PR — coupling computable after merge" sentence and the GAP "(b) modified path NOT in main's
    graph — possible extractor gap" sentence. When `added_paths` is None/empty, falls back to today's lumped copy
    (strictly behaviour-preserving)."""
    if not (split_from_verdict and unknown_paths):
        return
    # BEHAVIOR-PRESERVING DEFAULT: when the caller didn't supply added_paths (older sites: neighbor refresh,
    # fork variant, the test renderer call) we fall back to today's lumped copy unchanged. This keeps every
    # existing call site / snapshot byte-identical and limits the SPLIT to the acting-PR path that knows the
    # data. (Skipping the split-render branch entirely.)
    if not added_paths:
        L.append("")
        L.append(f"**Some new paths aren't analyzed yet (unverified, not 'safe').** The verdict above covers your "
                 f"changed files that are in main's graph. These other paths aren't in main's graph yet — a new file, "
                 f"an unsupported language, or an un-indexed area — so Veripsa can't compute their coupling: "
                 f"{_fmt_paths_no_count(unknown_paths)}. Treat those as unknown, not clear.")
        return
    new_in_pr, gap_paths = _split_unknown_paths(unknown_paths, added_paths)
    if new_in_pr and not gap_paths:
        # CASE (a) ONLY — every unknown path is NEW IN THIS PR. EXPECTED, not a warning: main doesn't have these
        # paths yet, so of course they're absent from main's graph. Frame INFORMATIVELY — no "extractor gap" /
        # "unsupported language" / "un-indexed area" wording (that implies a defect; case (a) is by design).
        L.append("")
        L.append(f"**New paths in this PR — coupling will be computable after merge.** The verdict above covers "
                 f"your changed files that are in main's graph. These paths are NEW in this PR, so there is no "
                 f"main-side graph to compare against yet (expected, not a warning): {_fmt_paths_no_count(new_in_pr)}. "
                 f"Veripsa will compute their coupling once this PR lands.")
        return
    if gap_paths and not new_in_pr:
        # CASE (b) ONLY — every unknown path is an EXISTING path absent from main's graph. A real extractor gap,
        # framed cautiously (treat as unknown, not 'clear').
        L.append("")
        L.append(f"**Some paths aren't in Veripsa's graph yet (unverified, not 'safe').** The verdict above "
                 f"covers your changed files that are in main's graph. These other paths are in main but "
                 f"NOT in Veripsa's graph — an unsupported language, an un-indexed area, or an extractor gap — "
                 f"so Veripsa can't compute their coupling: {_fmt_paths_no_count(gap_paths)}. "
                 f"Treat those as unknown, not clear.")
        return
    # BOTH CASES on one PR — render the two sentences as TWO paragraphs so the customer sees them as
    # semantically distinct. Case (a) first (the more common case, framed as expected); case (b) second
    # (the rarer one that warrants attention).
    L.append("")
    L.append(f"**New paths in this PR — coupling will be computable after merge.** These paths are NEW in this "
             f"PR, so there is no main-side graph to compare against yet (expected, not a warning): "
             f"{_fmt_paths_no_count(new_in_pr)}. Veripsa will compute their coupling once this PR lands.")
    L.append("")
    L.append(f"**Some other paths aren't in Veripsa's graph yet (unverified, not 'safe').** These paths are "
             f"in main but NOT in Veripsa's graph — an unsupported language, an un-indexed area, or an "
             f"extractor gap — so Veripsa can't compute their coupling: {_fmt_paths_no_count(gap_paths)}. "
             f"Treat those as unknown, not clear.")


def _verdict_summary(verdict: str, icon: str, finer_point: str, behind: list, impact_paths: list,
                     depends_on_changing: list, contested_with: list, dampened_with: list,
                     unknown_paths: list, clear_has_cosignal: bool, branch: str,
                     truncated: bool = False) -> str:
    """The ONE-LINE check summary (shows in the checks list) for THIS verdict — the only text a truly-clean PR
    surfaces, so it carries the product framing inline on `clear`. Content-free (counts + agent labels + the finer
    locus only). Returns the summary string; the orchestrator assigns it to `summary`."""
    if verdict == "serialize":
        where = (" — you collide " + finer_point) if finer_point else ""
        # DEGENERATE GUARD: _fmt_agents([]) is "" (an empty serialize_behind from the engine), so an unconditional
        # "queued behind " renders a dangling "queued behind  — land in order." (double space, no name). Fall back
        # to a generic, content-free counterpart — same as serialize_soft does — so the summary always names whom.
        who = _fmt_agents(behind)
        return (f"{icon} Direct collision: this PR is queued behind "
                + (who if who else "another in-flight change") + where + " — land in order.")
    if verdict == "serialize_soft":
        # a textual overlap ONLY on append-mostly build/test-RUNNER / list files (run_gates.sh and kin): a
        # 5-second git-conflict on append order, NOT logic coupling — so a low-stakes heads-up, not a hard wait.
        where = (" " + finer_point) if finer_point else ""
        who = _fmt_agents(behind)
        # STALE BRANCH-RESERVATION DECAY (#851): serialize_soft with NO hard-blocker holder (empty serialize_behind)
        # is the idle-branch-reservation downgrade, not a build/list append overlap — honest copy, don't say
        # "build/list file". GATED on empty `who`: every existing (low_value/append) serialize_soft has a holder, so
        # they keep the "build/list file" wording exactly.
        if not who:
            return (f"{icon} Minor overlap{where} with an idle branch reservation (no open PR) — not blocking; "
                    "it clears when that branch opens a PR or is cleaned up.")
        return (f"{icon} Minor overlap{where} with " + who
                + " on a build/list file — likely a quick append-order conflict, not a logic collision.")
    if verdict == "warn":
        # a warn can be driven by DOWNSTREAM blast (your change's dependents are in flight) and/or an UPSTREAM
        # dependency being edited under you. Lead with whichever is REAL — never "affects 0 downstream" when the
        # driver is upstream — and name the other party only when it's known (never a dangling "shared with .").
        if impact_paths:
            # MOAT (PO 2026-06-21 「file count はダメ」): the raw count of downstream files is a graph-size leak —
            # qualify the blast radius with a SEVERITY TIER word ("a wide" once it fans out past a handful, else
            # "its") so the customer still learns this change reaches widely, WITHOUT the topology count.
            reach = "a wide" if len(impact_paths) > _WIDE_BLAST_TIER else "its"
            summary = f"{icon} {reach.capitalize()} blast radius from your change meets other in-flight work"
        elif depends_on_changing:
            summary = f"{icon} You are building on a file another in-flight change is editing right now"
        else:
            summary = f"{icon} Your change shares a code neighborhood with other in-flight work"
        who = _fmt_agents(contested_with)
        summary += (" — shared with " + who + ".") if who else "."
        return summary
    if verdict == "unknown":
        # 'unknown' has TWO honest causes: (a) paths not in main's graph (can't analyze), and (b) a REAL coupling
        # that hub-dampening suppressed to avoid noise (AUDIT3 — we HAD the edge, so it is unknown, not clear).
        if dampened_with and not unknown_paths:
            return (f"{icon} Unknown (not 'clear'): a coupling with other in-flight work runs through a heavily-"
                    f"shared file — suppressed to avoid noise, so coordinate manually.")
        if unknown_paths:
            # MOAT (PO 2026-06-21 「file count はダメ」): state THAT some changed paths aren't analyzable, never the
            # COUNT — a raw "N path(s)" is a file count.
            return f"{icon} Not analyzed: some of your changed paths aren't in main's graph — coupling unverified."
        if truncated:
            return (f"{icon} Not fully analyzed: this PR has an unparsed file remainder — treat the result as "
                    "unknown, not clear.")
        # A missing/invalid engine verdict has no more specific reason metadata. Keep the summary aligned with the
        # Unknown title without inventing a graph gap that was not reported.
        return f"{icon} Unknown: not enough signal to call this clear."
    # CLEAR PRs post NO comment (the "less noise" policy below) UNLESS a co-signal attaches (then a comment IS
    # posted — see the comment-gate at the end), so this CHECK SUMMARY is the only text the customer sees on a
    # truly-clean PR — and for a freshly-installed repo it is often their FIRST contact with Veripsa. So it
    # carries the one-line product framing (advisory; records what is heading to `branch`, not correctness)
    # inline. Content-free, no extra noise: still one line, still the green check. HONESTY: when this clear PR
    # carries a co-signal (it holds a lane others wait on / reserves a hotspot / sits in a cluster), the
    # absolute "no other in-flight change touches your files" would CONTRADICT the comment's co-signal sections
    # — so the summary says "nothing is blocking you" instead (true: the PR itself is not waiting/coupled).
    if clear_has_cosignal:
        return (f"{icon} Clear — nothing is blocking you. Other in-flight work overlaps this area "
                f"(see the PR comment). Veripsa records what is heading to {_code(branch)} and flags overlap "
                "before merge (advisory — it does not assert correctness).")
    return (f"{icon} Clear — no other in-flight change touches your files or their blast radius. "
            f"Veripsa records what is heading to {_code(branch)} and flags overlap before merge "
            "(advisory — it does not assert correctness).")


def _render_split_advice(L: list, shared_foundation: list, verdict: str) -> None:
    """SPLIT-ADVICE / 独り占め禁止 (PO 2026-06-18 "Veripsaに分割を勧めさせる"): a load-bearing file (many import it AND it
    changes often) is a CHRONIC contention point — the same structural signal as core.split_candidates — so beyond
    flagging it, Veripsa PROACTIVELY recommends splitting it into cohesive modules. Relevance-gated to the files
    THIS PR touches (no repo-wide nag), content-free (path + a QUALITATIVE shape — never a raw fan-in/symbol count,
    which would leak the graph's size; PO 2026-06-21 「file count はダメ」), advisory. Appends to L in place."""
    if not shared_foundation:
        return
    L.append("")
    # One ">" blockquote line per foundation file, capped at LIST_LINE_CAP; a pile of foundations must not
    # blow the comment past GitHub's 65536-char limit (a rejected POST = NO comment on the busiest PR).
    for sf in shared_foundation[:LIST_LINE_CAP]:
        n = sf.get("fan_in", 0)
        c = sf.get("churn", 0)
        # MOAT (PO 2026-06-21 「file count はダメ」): the WHY must keep the structural SIGNAL but never the raw
        # numbers — fan_in is an incoming-edge/file count and `symbols` a node count (the graph's size = the moat),
        # and the churn integer reads as a count too. So we state the SHAPE qualitatively ("imported widely" /
        # "defines many distinct pieces" / "changes often") instead of "imported by N files" / "defines N symbols"
        # / "changed N times". The recommendation stays just as actionable; the topology count never leaks.
        #   foundation = many files import it (splitting frees downstream waiters)
        #   god_file   = it DEFINES many symbols (one file doing too many things — splitting separates concerns)
        basis = sf.get("basis") or ("foundation" if n else "god_file")
        churn_clause = (", and changes often" if c else "")
        if basis == "god_file":
            why = (f"defines many distinct pieces — one file doing many things{churn_clause}")
        elif basis == "both":
            why = (f"is imported widely across this repo AND defines many distinct pieces{churn_clause}")
        else:
            why = (f"is imported widely across this repo{churn_clause}")
        # MIXED-SIGNAL GUARD: on a 'serialize' PR the DOMINANT instruction is "wait in line" (this PR is itself
        # queued behind a holder). A "land it promptly" nudge here would tell the author to hurry AND wait at
        # once — undercutting the headline. So gate the promptness nudge: drop it when the verdict is serialize
        # (the split observation still stands; the "free the lane" urgency does not, because this PR is waiting).
        promptly = ("" if verdict in ("serialize", "serialize_soft")
                    else " Meanwhile keep this change focused and land it promptly so you are not holding a "
                         "shared lane longer than needed.")
        L.append(f"> **♻️ Recurring contention point — consider splitting:** {_code(sf.get('path'))} {why}, so "
                 f"independent changes keep colliding on it. Splitting it into smaller, cohesive modules would let "
                 f"those changes land in parallel instead of queueing on one lane.{promptly} "
                 f"(A structural observation.)")
    # MOAT (PO 2026-06-21 「file count はダメ」): "+N more files" is a raw file count → drop the NUMBER, keep the
    # "there are more" signal qualitatively. The customer still learns this PR touches several recurring-contention
    # files without learning how many (the count would leak the graph's size).
    if len(shared_foundation) > LIST_LINE_CAP:
        L.append("> _(This PR touches further recurring-contention files as well.)_")


def _render_queued_behind(L: list, queued_behind: list, queued_behind_paths: list, verdict: str) -> None:
    """NOTIFY-THE-HOLDER (PO 2026-06-18): THIS PR holds a lane other in-flight PR(s) now wait behind. The inverse of
    "wait in line" — the holder was never told work is blocked ON it; that reinforces "land it promptly so you free
    the lane". Content-free (counts + labels + shared lane paths, all capped). Appends to L in place."""
    if not queued_behind:
        return
    L.append("")
    n = len(queued_behind)
    on_lanes = (" on: " + _fmt_list(queued_behind_paths)) if queued_behind_paths else ""
    # MIXED-SIGNAL GUARD: when THIS PR is itself a waiter ('serialize' — it is queued behind a holder), telling
    # it to "land promptly to free the lane" contradicts the dominant "wait in line" headline (hurry AND wait).
    # So gate the promptness nudge on verdict: a clear/holder PR (not itself waiting) keeps "free the lane"; a
    # serialize PR instead gets the consistent order — it lands once the change AHEAD of it does, and only then
    # do its own waiters promote. The "who's waiting behind you" fact still surfaces either way (it's true).
    if verdict in ("serialize", "serialize_soft"):
        # This PR is itself a waiter — it lands once the change AHEAD of it does, then its own waiters promote.
        # No "land promptly to free the lane" (it can't hurry; it is waiting). State the consistent order.
        free_lane = (" You land once the change ahead of you lands or withdraws — and then it is their turn.")
    else:
        free_lane = " Keep this change focused and land it promptly so you free the lane."
    L.append(f"> **⏳ {n} in-flight PR{'s' if n != 1 else ''} {'are' if n != 1 else 'is'} waiting behind this one**"
             f"{on_lanes} — {_fmt_agents(queued_behind)}. They are queued on a lane this PR holds; when it lands "
             f"or withdraws, the next reservation in line advances.{free_lane}")


def _render_depends_on(L: list, depends_on_changing: list) -> None:
    """The headline UPSTREAM warning: a foundation THIS PR builds on is shifting under it right now (another
    in-flight change is editing it). One blockquote per dependency, capped. Content-free (path + author). Appends."""
    if not depends_on_changing:
        return
    L.append("")
    # One blockquote line per upstream dependency under edit, capped like the others to stay under GitHub's
    # 65536-char comment limit; the trailer below flags that more exist beyond the cap (qualitatively — MOAT).
    for d in depends_on_changing[:LIST_LINE_CAP]:
        L.append(f"> **⚠ A part you depend on is being changed right now:** {_code(d.get('path'))} "
                 f"(by **{_safe_agent(d.get('by'))}**). You are building on top of it — align before merging, or you may "
                 f"have to redo this once that change lands.")
    # MOAT (PO 2026-06-21 「file count はダメ」): "+N more parts" is a raw count of upstream dependency files → drop
    # the NUMBER, keep the "there are more" signal. The customer still learns more of their dependencies are in
    # motion, without learning how many (that count would leak the graph's size).
    if len(depends_on_changing) > LIST_LINE_CAP:
        L.append("> _(Further parts you depend on are also being changed right now — align before merging.)_")


def _render_collision_prose(L: list, verdict: str, behind: list, finer_point: str,
                            merge_conflict_likely: bool, conflict_points: list) -> None:
    """The full-prose CONTENTION copy for a direct collision — the hard 'serialize' "land in order", the soft
    'serialize_soft' "minor overlap", and (on BOTH) the MECHANICAL merge-conflict anticipation (a different axis
    from severity). Content-free (finer locus + a path + a line number, never code). Appends to L in place."""
    if verdict == "serialize" and behind:
        # The headline "wait in line" copy for a hard direct collision (mirrors the one-line summary built above,
        # but in full prose for the comment body).
        L.append("")
        # NAME THE FINER POINT: when both sides resolved to the SAME symbol (or a line range), say exactly where
        # you collide — so the developer knows it is a real overlap, not just "same file". When we could only
        # tell at file level (un-mappable change / no spans), we fall back to the honest same-file phrasing.
        where = (f" You collide {finer_point} — that's the same code, not just the same file."
                 if finer_point else " A direct, same-file collision.")
        # HONEST FRAMING (no overclaim): Veripsa is ADVISORY by default — the check is `neutral`, it does not gate
        # the merge (the footer says your branch-protection policy decides what blocks). So the queue is a RECORD,
        # not an enforced mechanism: we must NOT promise an outcome ("No work is lost") or state internal promotion
        # as a certainty ("will release … is then promoted automatically"). Promotion is CONDITIONAL on the other PR
        # actually landing (it may instead be closed/withdrawn), and on the author choosing to land in the suggested
        # order (we never force it). Frame the value as what landing-in-order AVOIDS (rebasing onto a change still in
        # flight), in conditional mood — same hedge the merge-conflict line already uses.
        #
        # NO-DOUBLE-HEADLINE + NO-PAUSE-CONTRADICTION (wording audit 2026-06-20): the bold "⏸ Wait in line" lead is
        # ALREADY the comment header (line ~564) — repeating "**Wait in line.**" here is the same phrase TWICE. And
        # the OLD parenthetical "(Advisory — the order is a suggestion, not a block.)" directly CONTRADICTS the
        # pause-ack tier: when this same serialize PR is paused (action_required), the pause banner above says "to
        # proceed add the `veripsa-ack` label" — saying "not a block" lower in the SAME comment is a self-
        # contradiction. So lead with the ACTION ("Land in order.") instead of re-stating the header, and DROP the
        # "not a block" parenthetical. The honest, non-contradicting footer ("Advisory by default; your branch-
        # protection policy decides what blocks") stays at the bottom of every comment and carries the advisory frame
        # once, correctly — true whether or not this PR is in the pause tier.
        L.append(f"**Land in order.**{where} You are queued behind {_fmt_agents(behind)}. "
                 f"Landing in this order means you rebase onto their change once — not redo work after both land "
                 f"out of order. When the change ahead lands or withdraws, your reservation advances; "
                 f"if it withdraws, this clears.")
    elif verdict == "serialize_soft" and behind:
        L.append("")
        # SOFT: the overlap is ONLY on append-mostly build/test-RUNNER / registration files (run_gates.sh and
        # kin) — every PR appends its own line, so two PRs touch adjacent lines but the "conflict" is append
        # ORDER, resolved in seconds. We SURFACE it (it is a real textual overlap) but do NOT hold you in line:
        # a hard wait here would be over-serialization — wallpaper. If you ALSO overlapped a real source file,
        # this would read "Wait in line" instead (the engine only softens when EVERY overlap is low-value).
        where = (f" {finer_point}" if finer_point else "")
        L.append(f"**Heads up — minor overlap.** You overlap {_fmt_agents(behind)}{where}, but only on an "
                 f"append-mostly build/test-runner or list file. That is typically a quick append-order git "
                 f"conflict, not a logic collision — no need to wait in line; just expect a trivial rebase/merge "
                 f"if you both land close together. (If you had also overlapped real source code, this would be a "
                 f"hard 'wait in line' instead.)")
    elif verdict == "serialize_soft" and not behind:
        # STALE BRANCH-RESERVATION DECAY (#851): serialize_soft with NO hard-blocker holder is the idle-branch-
        # reservation downgrade. A path here is reserved by a long-lived branch push that never opened a PR and has
        # not been updated recently — so it is not real in-flight work to queue behind. Surface it as a non-blocking
        # heads-up (content-free: the finer locus only, never who or a body). GATED on empty `behind` → unreachable
        # for the existing low_value/append serialize_soft cases (which always have a holder).
        L.append("")
        where = (f" {finer_point}" if finer_point else "")
        L.append(f"**Heads up — an idle branch reservation is on your path{where}.** It was reserved by a branch "
                 f"push with no open PR that has not been updated in a while, so it is not blocking you. It clears "
                 f"when that branch opens a PR or is cleaned up; no need to wait in line.")

    # MECHANICAL merge-conflict anticipation (a DIFFERENT axis from the severity copy above). Surface it on BOTH
    # the soft AND the hard collision — and CRUCIALLY on the SOFTENED low-value case, which is the regression that
    # bit us: three PRs appended near the same line of run_gates.sh, the severity got softened to a "heads up", and
    # the GUARANTEED git merge conflict on rebase was a SURPRISE because softening dropped the forewarning. So when
    # the line geometry says a conflict is LIKELY, add a low-stakes, HONESTLY-FRAMED heads-up naming the file +
    # approximate line. HONEST: "likely" / "expect", never "will definitely" — this is a content-free heuristic
    # (line numbers only, no 3-way merge of file bodies). For a HARD serialize we keep it brief (the "wait in line"
    # copy already carries the weight) — a one-liner that the same overlap will also surface as a small conflict on
    # rebase, not a second loud warning. Content-free: a path + a line number, never code.
    if merge_conflict_likely and conflict_points and verdict in ("serialize", "serialize_soft"):
        L.append("")
        locs = [p for p in (_fmt_conflict_location(cp) for cp in conflict_points) if p]
        loc = locs[0] if locs else ""
        where = (" in " + loc) if loc else ""
        if verdict == "serialize_soft":
            # PRECISION (PO 2026-06-25 #4): the soft case sits on an append-mostly low-value file (run_gates.sh and
            # kin) and the conflict is anticipated from LINE-RANGE GEOMETRY alone (overlap-or-adjacent within the
            # 2-line tolerance) — no 3-way merge of bodies. So the strong "Likely a small merge conflict on rebase"
            # headline (read as "you WILL conflict") overclaims for a heuristic with no body-level signal: the same
            # geometry routinely auto-merges in practice (adjacent appends on different statements / separated by
            # context lines git can stitch). Soften the HEADLINE to "you may need a small rebase" — same fact, lower
            # confidence — and keep the explanation that git may flag a small textual conflict. The "trivial to
            # resolve, not a logic issue" hedge stays. The original regression (softening dropped the heads-up
            # entirely → surprise conflict) is preserved: the heads-up still appears, just at honest strength.
            L.append(f"> **⏳ You may need a small rebase{where}.** You and {_fmt_agents(behind)} touch a similar "
                     f"spot, so a small textual merge conflict is likely when one of you rebases — trivial to "
                     f"resolve, not a logic issue. (Content-free heuristic from line ranges, so \"likely\", not "
                     f"certain — a true 3-way merge needs the file contents, which Veripsa does not read.)")
        else:
            # the hard case: reinforce "land in order", don't double-warn loudly. Kept the existing "likely" hedge —
            # same geometry, same heuristic limit (no body inspection) — but lead with the softer "small rebase may
            # be needed" frame so it does not double-warn loudly over the wait-in-line lead above.
            L.append(f"> **⏳ A small rebase may also be needed{where}** — the overlap above is likely to surface as "
                     f"a textual merge conflict on rebase, which landing in order avoids. (Content-free heuristic — "
                     f"\"likely\", not certain.)")


def _render_warn_unknown_detail(L: list, verdict: str, contested_with: list,
                                unknown_paths: list, dampened_with: list,
                                added_paths: list | None = None) -> None:
    """The detail prose for a WARN (the semantic "coordinate before merge") and for an UNKNOWN (un-analyzable
    paths and/or a hub-dampened-suppressed coupling, AUDIT3 — surfaced so the silence is visible). Content-free
    (agent labels + paths + the hub file). Appends to L in place.

    `added_paths` (PO 2026-06-25 honest-verdict refinement): the SUBSET of changed files NEW IN THIS PR. When
    supplied, the lumped "Not analyzed" sentence on a whole-PR Unknown is SPLIT into "(a) new in this PR —
    coupling computable after merge" and "(b) modified path NOT in Veripsa's graph — possible extractor gap".
    When None/empty (older call site / fork variant / fetch error), falls back to today's lumped copy
    byte-identical."""
    if verdict == "warn":
        if contested_with:
            L.append("")
            # PRECISION (PO 2026-06-25 #4): a WARN is semantic coupling ONLY — git's textual 3-way merge will not
            # surface anything here (no overlapping line ranges, no insertion-point clash). The previous copy buried
            # "not a textual conflict git would catch" mid-sentence, which let the customer skim past it and read the
            # bold "Coordinate before merge" as a git-conflict claim. Lead the bold tag with the no-textual-conflict
            # framing so it lands FIRST, then explain the structural link below. Same fact; clearer priority.
            L.append(f"**Coordinate before merge — semantic coupling, not a git conflict.** Your change shares a "
                     f"code neighborhood with {_fmt_agents(contested_with)} — a structural dependency links your "
                     f"changes (a reference between your files, a shared table, or a shared config key). This is "
                     f"not a textual conflict git would catch on rebase (no line overlap); revising now avoids "
                     f"rework after merge.")
        # CO-CHANGE-CORROBORATED hub coupling (AUDIT3 relaxation): the WHY behind a dampened coupling earning a
        # 'warn'. A coupling hub-dampening would normally suppress (it runs through a heavily-shared file), but git
        # co-change INDEPENDENTLY confirms these files repeatedly change together — TWO signals agree, so it is a
        # real coordinate-before-merge, not noise. Render only the corroborated entries (the verdict's cause).
        corrob = [d for d in dampened_with if d.get("corroborated")]
        if corrob:
            L.append("")
            hubs = [d.get("via_hub") for d in corrob]
            who = _fmt_agents([d.get("by") for d in corrob])
            # DANGLING-NAME GUARD (audit, dampening-honest-render): a degenerate dampened_with row (engine-side null
            # `by` / `via_hub`, malformed JSON, older engine) used to render "linked to  through a shared file ()" —
            # double-space + empty parens. Honesty wins over a dangling sentence: when a piece is empty, fall back to
            # a generic, content-free counterpart ("other in-flight work" / "a shared file") instead of leaving the
            # token blank. The bold lead + the "two independent signals" framing still carries the WHY; we just never
            # print a literal "()" or "linked to  ". Mirrors the same degenerate-guard pattern _verdict_summary uses
            # for an empty serialize_behind.
            who_clause = who if who else "other in-flight work"
            hub_clause = _fmt_hub_clause(hubs)
            # PRECISION (PO 2026-06-25 #4): lead the bold tag with the no-textual-conflict framing here too — the
            # corroborated-hub case is a semantic coupling for the same reason as the contested_with case above.
            L.append(f"**Coordinate before merge — semantic coupling, not a git conflict.** Your change is linked to "
                     f"{who_clause} through a shared file{hub_clause}, and these files repeatedly change together "
                     f"in this repo's history — two independent signals (a structural link plus that co-change) "
                     f"agree they are coupled. Not a textual conflict git would catch on rebase; revising together "
                     f"avoids rework after merge.")
    elif verdict == "unknown":
        L.append("")
        if unknown_paths:
            # SPLIT (PO 2026-06-25): when added_paths is supplied, partition the lumped "Not analyzed" sentence
            # into (a) new in this PR (expected, computable after merge) and (b) modified path NOT in main's
            # graph (possible extractor gap). When added_paths is None/empty, keep today's lumped copy.
            new_in_pr, gap_paths = _split_unknown_paths(unknown_paths, added_paths) if added_paths else ([], [])
            if new_in_pr and gap_paths:
                L.append(f"**New paths in this PR — coupling will be computable after merge.** These paths are "
                         f"NEW in this PR, so there is no main-side graph to compare against yet (expected, not a "
                         f"warning): {_fmt_paths_no_count(new_in_pr)}. Veripsa will compute their coupling once "
                         f"this PR lands.")
                L.append("")
                L.append(f"**Some other paths aren't in Veripsa's graph yet (unverified, not 'safe').** These "
                         f"paths are in main but NOT in Veripsa's graph — an unsupported language, an un-indexed "
                         f"area, or an extractor gap — so Veripsa can't compute their coupling: "
                         f"{_fmt_paths_no_count(gap_paths)}. Treat as unknown, not clear.")
            elif new_in_pr and not gap_paths:
                # EVERY unknown path is NEW IN THIS PR — frame INFORMATIVELY (no "extractor gap" wording).
                L.append(f"**New paths in this PR — coupling will be computable after merge.** These paths are "
                         f"NEW in this PR, so there is no main-side graph to compare against yet (expected, not a "
                         f"warning): {_fmt_paths_no_count(new_in_pr)}. Veripsa will compute their coupling once "
                         f"this PR lands.")
            elif gap_paths and not new_in_pr:
                # EVERY unknown path is an EXISTING path absent from main's graph — frame as a real gap.
                L.append(f"**Not in Veripsa's graph yet (unverified, not 'safe').** These paths are in main but "
                         f"NOT in Veripsa's graph — an unsupported language, an un-indexed area, or an extractor "
                         f"gap — so Veripsa can't compute their coupling: {_fmt_paths_no_count(gap_paths)}. "
                         f"Treat as unknown, not clear.")
            else:
                # FALLBACK (no added_paths supplied): keep today's lumped copy unchanged.
                L.append(f"**Not analyzed (unverified, not 'safe').** These paths aren't in main's graph yet — a new file, an "
                         f"unsupported language, or an un-indexed area — so Veripsa can't compute their blast radius: "
                         f"{_fmt_paths_no_count(unknown_paths)}. Treat as unknown, not clear.")   # MOAT: no '+N more' file count
        # SUPPRESSED-ONLY couplings (uncorroborated): on 'unknown' every dampened entry is uncorroborated (a
        # corroborated one promotes the verdict to 'warn', handled above), but filter defensively so the copy can
        # never mislabel a corroborated coupling as mere noise-suppression.
        suppressed = [d for d in dampened_with if not d.get("corroborated")]
        if suppressed:
            # BLANK-LINE GUARD (run-on fix): when BOTH unknown_paths AND dampened_with are present, the two
            # `**bold**` blocks would otherwise be joined by a single "\n" — GitHub renders that as ONE run-on
            # paragraph (every other multi-block section prefixes an L.append("")). Add the paragraph break so the
            # two distinct facts read as two paragraphs. Only needed when the first block actually rendered.
            if unknown_paths:
                L.append("")
            # AUDIT3: a real, extracted coupling with other in-flight work was suppressed because it runs through a
            # heavily-shared file (to avoid a wall of warnings on every popular file). We do NOT claim 'clear' — we
            # surface it so the silence is visible and you can coordinate. Names the other PR + the shared file.
            hubs = [d.get("via_hub") for d in suppressed]
            who = _fmt_agents([d.get("by") for d in suppressed])
            # DANGLING-NAME GUARD (audit, dampening-honest-render): a degenerate dampened_with row (engine-side null
            # `by` / `via_hub`, malformed JSON, older engine) used to render "linked to  through a heavily-shared
            # file ()" — double-space + empty parens. The four honest dampening obligations stand regardless: the
            # SUPPRESSION is surfaced (the bold lead always renders), we say "treat as unknown, not clear", we name
            # the hub when we have it, and we never call this 'clear'. When a piece is empty fall back to a generic,
            # content-free counterpart ("other in-flight work" / drop the "(hub)" parenthetical) instead of leaving
            # the token blank — same degenerate-guard pattern _verdict_summary uses for an empty serialize_behind.
            who_clause = who if who else "other in-flight work"
            hub_clause = _fmt_hub_clause(hubs)
            L.append(f"**A coupling here was suppressed to avoid noise — treat as unknown, not clear.** Your change is "
                     f"linked to {who_clause} through a heavily-shared file{hub_clause} that many files depend on. "
                     f"Veripsa does not auto-warn on every popular file (that would be noise), but it will not call this "
                     f"'clear' either — there is a real structural link, so coordinate before merge.")


def _render_land_order(L: list, cluster: dict, changes: list, my_label: str, change_ref: str, repo: str) -> None:
    """The cluster (contention neighborhood) SUGGESTED LAND-ORDER — the N-scale unit: coordinate the GROUP, not N²
    pairs. DEDUPED to one row per distinct PR/branch (the engine emits one row per reserved path), CAPPED to stay
    under GitHub's comment limit (always keeping THIS PR's own row + true position visible), and every row carries
    a content-free PR/branch ref (never a bare author). Content-free (refs + counts). Appends to L in place."""
    if not (cluster and _int(cluster.get("size")) >= 2):
        return
    order = cluster.get("suggested_order", []) or []
    L.append("")
    # DANGLING-HEADER GUARD: the cluster gate is size>=2, but `suggested_order` can be empty/NULL (no order was
    # computed). The "Suggested order to land them:" header + the numbered-list scaffolding below must hang ONLY
    # off a NON-EMPTY order, or we print a header with no list under it. The "N PRs touch this area" sentence is
    # still true for an order-less cluster, so it stays; the order clause is appended only when there is an order.
    #
    # DEDUP (2026-06-22): the engine emits one suggested_order entry PER reserved path/lane, so a branch with N
    # overlapping paths generates N identical rows (same label, same ref). Landing is per-PR/branch (land the
    # branch → all its paths resolve), so the display must list each DISTINCT PR/branch ONCE. Collapse the raw
    # order to unique-by-ref entries (first occurrence wins, preserving foundational order). The dedup key is the
    # content-free ref token (PR-<n> / BR-<branch>) extracted by _LAND_REF_RE; for entries with no ref yet
    # (an author-only BR- label not yet reconciled), the full entry text is the key so it too deduplicates.
    # The COUNT in the header becomes the number of DISTINCT PRs/branches, which is the honest figure — the raw
    # cluster.size is per-path-reservation and overstates when any branch holds multiple paths.
    ref_map = _label_ref_map(changes)
    _seen_refs: set = set()
    deduped_order: list = []
    for _entry in order:
        _m = _LAND_REF_RE.search(_entry) if isinstance(_entry, str) else None
        _key = _m.group(0) if _m else (_entry or "")
        if _key not in _seen_refs:
            _seen_refs.add(_key)
            deduped_order.append(_entry)
    distinct_count = len(deduped_order)   # honest count: distinct PRs/branches, not per-path rows
    if deduped_order:
        L.append(f"**Overlapping PRs** — {distinct_count} open PRs touch this same area. "
                 f"Suggested order to land them (most foundational first, so the others only revise once):")
        L.append("")
    else:
        # order was empty/non-empty-but-all-dupes: fall through to the count-only line
        L.append(f"**Overlapping PRs** — {_int(cluster.get('size'))} open PRs touch this same area.")
    # CAP the land order: a pathological neighborhood (hundreds/thousands of entangled PRs) renders one line
    # PER entry — uncapped, a long-labelled pile blows the comment past GitHub's 65536-char limit, so the POST
    # 422s and the customer gets NO comment on exactly the busiest, most-collision-prone PR. Show the first N
    # (foundational-first = the most actionable head of the queue), count the rest. ALWAYS keep THIS PR's own
    # row visible (with its true position) even if it sits past the cap, so the customer can still see where
    # they land. The "← this PR" marker preserves the original position number, never a renumbered one.
    is_me = lambda a: a == my_label or a == change_ref   # identify the customer's own row, by either id form
    # ALWAYS-A-REF (wording audit 2026-06-20): a land-order entry is a humanized label that, for a BR- push not
    # yet reconciled to a PR, is the AUTHOR NAME ALONE (no ref) — so a same-author cluster used to collapse to
    # '1. example-user / 2. example-user / …', author noise with no PR identity. Recover each entry's content-free ref
    # from the engine's per-change rows so EVERY row reads 'PR-1, PR-2, PR-10, PR-11' (the ref is the identity
    # already surfaced elsewhere); the author name is kept alongside the ref when the label carried one.
    shown = deduped_order[:LIST_LINE_CAP]
    # Render a numbered land-order list (1-based), tagging the customer's own PR with "← this PR".
    for i, a in enumerate(shown, 1):
        mark = " ← this PR" if is_me(a) else ""
        L.append(f"{i}. **{_pr_link(_safe_agent(_land_order_label(a, ref_map)), repo)}**{mark}")   # scrub + always-a-ref + clickable PR link: no service-role id, no bare-author row
    rest = deduped_order[LIST_LINE_CAP:]
    if rest:
        # keep the customer's OWN PR visible even when it falls beyond the shown slice (1-based true position).
        my_pos = next((j for j, a in enumerate(deduped_order, 1) if is_me(a)), None)
        if my_pos is not None and my_pos > LIST_LINE_CAP:
            L.append("…")
            L.append(f"{my_pos}. **{_pr_link(_safe_agent(_land_order_label(deduped_order[my_pos - 1], ref_map)), repo)}** ← this PR")
            remaining = len(rest) - 1   # the rest, minus this PR's own row we just surfaced
            if remaining > 0:
                L.append(f"_(+{remaining} more PR(s) further down the order.)_")
        else:
            L.append(f"_(+{len(rest)} more PR(s) further down the order.)_")


def _render_cochange(L: list, cc_rows: list) -> None:
    """CO-CHANGE (empirical / logical coupling) — the "you touched A; historically B comes with it" hint the
    structural graph is BLIND to. PURELY ADDITIVE — shown ONLY for a strong, base-rate-corrected (lift) positive
    coupling, NEVER a "0% = safe" claim. Advisory, never a verdict. Content-free (paths + lift only — NO raw
    co-occurrence count, NO literal "N%" probability, per the raw-count privacy boundary and the honest-copy
    rule against presenting a bounded observation as a guarantee). NOT rendered on a fork PR (the orchestrator
    returns before this on a fork). Appends to L in place."""
    cc = cc_rows
    if not cc:
        return
    L.append("")
    L.append("**📊 Historically changes together** (empirical — a coupling the dependency graph can't see):")
    for x in cc[:4]:   # cap: a few strongest pairs, never a firehose
        lift = x.get("lift")
        liftx = f"{float(lift):.1f}× more than chance" if isinstance(lift, (int, float)) else "above chance"
        # HONEST-COPY RULE: the saturating "**N%** of the
        # time" segment is dropped. At strong couplings it printed "**100%** of the time" — a literal guarantee in
        # customer-facing copy, the exact overclaim the honest-copy rule bans. The MULTIPLIER ("3.6× more than
        # chance") is an interpretable, uncapped lift — keep it; that's the customer-meaningful signal.
        # RAW-COUNT MOAT RULE (PO 「raw counts はダメ; coverage % OK」): the old "(co of n changes)" parenthetical
        # already went too (it leaked the RAW graph co-occurrence count, exposing the graph's size/history depth).
        L.append(f"- {_code(x.get('edited'))} also tends to change with {_code(x.get('partner'))} "
                 f"({liftx}).")
    L.append("> Advisory — an implicit coupling with no code edge for the dependency graph to see. Check whether "
             "the partner file(s) also need updating. History-based: a brand-new or just-split file has NO signal "
             "here — treat that as **not evaluated**, not as “no coupling”.")


def render_pr_check(impact: dict, change_ref: str, truncated: bool = False, is_fork: bool = False, cochange=None,
                    added_paths: list | None = None,
                    conflict_markers: list | None = None) -> dict:
    """Render the GitHub PR check + comment for ONE pull request.

    `impact` is the JSON returned by core.main_impact_surface(repo, branch).
    `change_ref` is the PR/change identity (for GitHub App: PR-123). For older CLI/demo calls, an agent
    display name still works as a fallback.
    `truncated` (HONESTY): this PR changed more code files than the App's per-PR cap, so only the first N were
    reserved + analyzed for coupling. A confident verdict over a PARTIAL file set would overclaim — so a would-be
    `clear` becomes `unknown`/neutral and the surface discloses the unparsed remainder.
    `is_fork` (INFO-LEAK GUARD): this PR comes from a FORK, so its comment posts on the base-repo conversation
    that the EXTERNAL contributor can read. The full comment names the base repo's OTHER in-flight PRs (refs +
    author logins) and base-repo paths/symbols — private in-flight structure that must not leak to an outside
    contributor. When is_fork, the surface renders a REDACTED comment+summary: THIS PR's own verdict + a generic
    "coordinate with the maintainers" line, with every cross-PR identifier and base-repo path/symbol dropped.
    (server.py computes is_fork from head.repo.id != base.repo.id and threads it here via webhook.py.)
    `added_paths` (PO 2026-06-25 honest-verdict refinement): the SUBSET of this PR's changed files whose
    Files-API `status` is `added` — i.e. NEW IN THIS PR (not present at base). When provided, the renderer
    SPLITS the lumped `unknown_paths` arm into two semantically-distinct sentences:
      (a) "These paths are NEW in this PR — coupling becomes computable after merge. Expected, not a warning."
      (b) "These paths are in main but not in Veripsa's graph yet — possible extractor gap. Treat as unknown."
    Lumping both as one "Unknown" trains customers to ignore the verdict (most PRs add at least one new file,
    so most PRs hit the Unknown copy → habituation → real (b) gaps get missed). The same data is also the proof
    required for an engine `unknown` to become `clear`: every unknown path must be in this list. When
    `added_paths` is None / empty (older call sites or a fetch error), the renderer stays conservatively Unknown
    and falls back to the lumped reason copy.

    `conflict_markers` (PO 2026-06-25 dogfood hole-fix, PRs #111/#114): findings list
    [{"path","line","kind"}, …] of UNRESOLVED git conflict markers introduced by this PR's added lines (the
    `<<<<<<<` / `=======` / `>>>>>>>` shape at line-start that breaks every build). When non-empty, the
    renderer ESCALATES this PR to `action_required` (the one hard-fail conclusion the otherwise
    advisory-only check ever emits — these are 100%-certain build-breakers) and prepends a top-of-comment
    block naming the path + line of the first marker per file. Content-free: paths + line numbers only,
    never the line body. Even on a FORK PR the escalation fires (the marker is the contributor's own added
    code — naming the path leaks no base-repo structure). Default None / empty = no escalation, fully
    behavior-preserving for clean PRs + existing call sites.

    Returns {conclusion, title, summary, comment} — comment is GitHub-flavored markdown (or None if there is
    nothing recorded for this PR yet, so the App can skip commenting).
    """
    impact = impact if isinstance(impact, dict) else {}     # a non-dict surface (None / partial read) → honest-empty, never a crash
    # Normalize the ADDED-PATH EVIDENCE into a clean list of path strings up front. A non-list arg (None / a
    # malformed event shape) MUST NOT crash the renderer, and MUST NOT earn Clear: empty / invalid means the
    # renderer cannot prove unknown paths are new, so it stays Unknown and uses the lumped reason copy.
    if not isinstance(added_paths, (list, tuple)):
        added_paths = []
    else:
        added_paths = [p for p in added_paths if isinstance(p, str) and p]
    # CONFLICT-MARKER FINDINGS (PO 2026-06-25 dogfood hole-fix, PRs #111/#114): sanitize + de-dup into a stable
    # per-file list. The file-level precision gate (both `<<<<<<<` AND `>>>>>>>` markers present) is enforced
    # upstream in conflict_markers_from_patch — so any non-empty list here is a TRUE build-breaker signal. The
    # escalation block (top-of-comment + conclusion override) is composed below ONLY when non-empty.
    _conflict_findings = _conflict_marker_findings(conflict_markers)
    changes = _dicts(impact.get("changes"))                 # drop any malformed (non-dict) row so one bad entry can't take down the whole render
    me = next((c for c in changes if c.get("change_id") == change_ref), None)
    if me is None:
        me = next((c for c in changes if c.get("agent") == change_ref or c.get("label") == change_ref), None)
    repo = impact.get("repo", "")
    branch = impact.get("branch", "main")
    my_label = me.get("label", change_ref) if me else change_ref

    if me is None:
        # The App saw a PR but Veripsa has no reservation for it yet (e.g. first sync). Honest-empty: a
        # passing check, no noisy comment. The summary still names what Veripsa IS (advisory; records what is
        # heading to `branch`, not correctness) — for a freshly-installed repo this check may be the customer's
        # FIRST contact with the product, and the clear/empty paths post no comment to carry that framing.
        # EXCEPTION: an UNRESOLVED MERGE-MARKER finding is a build-breaker that we DO speak on even without a
        # reservation — it is a 100%-certain hard fail in code the PR adds, and "wait until you get a reservation"
        # would silently let a literal `<<<<<<<` ride to merge. Escalate exactly like the main path.
        if _conflict_findings:
            return _build_conflict_marker_result(_conflict_findings, branch)
        return {
            "conclusion": "success",
            "title": "Veripsa — heading to " + _safe(branch),
            "summary": "No in-flight reservation recorded for this PR yet. "
                       f"Veripsa records what is heading to {_code(branch)} and flags overlap before merge "
                       "(advisory — it does not assert correctness).",
            "comment": None,
        }

    # HONESTY on an UNEXPECTED verdict: the engine emits one of {clear, warn, serialize, serialize_soft, unknown}.
    # A verdict we don't recognise (None, a number, a renamed/typo'd engine string) must NOT silently fall through
    # to the 'clear' copy ("nothing else touches your files") — that would assert safety we cannot back — NOR render
    # raw (`**• None**` leaked a bare engine value into the comment header). An unrecognised verdict means we don't
    # know the call, so it maps to 'unknown' (the honest "not enough signal to call this clear" branch), whose copy
    # and titles already exist. Content-free: we substitute a known label, we never echo the raw value.
    raw_verdict = me.get("verdict")
    verdict = (raw_verdict if isinstance(raw_verdict, str) and raw_verdict in _VERDICT_CONCLUSION
               else "unknown")
    paths = me.get("paths", []) or []
    impact_paths = me.get("impact", []) or []
    contested_with = me.get("contested_with", []) or []
    behind = me.get("serialize_behind", []) or []
    queued_behind = me.get("queued_behind", []) or []          # in-flight PR(s) WAITING behind this one (the inverse of `behind`): this PR is the lane holder, they are the waiters
    queued_behind_paths = me.get("queued_behind_paths", []) or []   # the shared lane path(s) those waiting PRs are blocked on
    # DRAFT (scout-window) suffix (PO 2026-06-25): build the set of partner labels currently in DRAFT state, then
    # decorate every partner-label list so the rendered name carries a " (draft)" suffix — "alice PR-12 (draft)".
    # Content-free (a label string + a boolean Veripsa already exposes). Older engines that don't carry is_draft
    # produce an empty set; _mark_draft is a no-op then (back-compatible).
    _draft_set = _draft_label_set(impact.get("changes", []) or [])
    contested_with = _mark_draft(contested_with, _draft_set)
    behind = _mark_draft(behind, _draft_set)
    queued_behind = _mark_draft(queued_behind, _draft_set)
    # These five are lists of DICT rows, each read with `.get(...)` below — filter to dicts so a malformed (non-dict)
    # entry from the engine drops out instead of crashing the render (the scalar lists above are already safe: they
    # only ever flow through _fmt_list/_fmt_agents, which drop None/blank).
    collision_points = _dicts(me.get("collision_points"))   # finer (symbol/line-range) locus of a direct collision
    conflict_points = _dicts(me.get("conflict_points"))     # MECHANICAL: where a git merge conflict on rebase is LIKELY (overlap/same-insertion-point lines)
    merge_conflict_likely = bool(me.get("merge_conflict_likely"))
    depends_on_changing = _dicts(me.get("depends_on_changing"))   # upstream files THIS PR builds on that another in-flight change is editing right now
    shared_foundation = _dicts(me.get("shared_foundation"))   # reserved paths that many files import (high fan-in) AND that change often — load-bearing, wide blast radius
    dampened_with = _dicts(me.get("dampened_with"))           # real couplings hub-dampening suppressed (AUDIT3) → unknown, surfaced
    unknown_paths = me.get("unknown_paths", []) or []           # reserved paths not in main's graph (new file / unsupported language / un-indexed) — blast radius uncomputable

    # PER-PATH VERDICT (the BLANKET-UNKNOWN root fix): the engine's single per-change
    # 'unknown' fires whenever ANY reserved path is absent from main's graph (a new file/test/module), which used
    # to blank out the verdict for the WHOLE PR even when it ALSO modifies coupled IN-GRAPH files — suppressing the
    # useful (clear/warn/serialize) call on the verifiable part. Most real PRs add ≥1 new file, so most PRs landed
    # on blanket 'unknown' = rarely a useful verdict. We resolve the verdict for the VERIFIABLE (in-graph) subset
    # of the change (= reserved paths minus unknown_paths) and report the new paths SEPARATELY (the standalone
    # `_render_unverified_paths_note` below), so the unverifiable paths no longer suppress the verifiable verdict.
    # The engine ladder already lets a real collision/coupling on an in-graph file WIN over the unknown-path test
    # (serialize/warn beats unknown), and `_effective_verdict` only downgrades the blanket 'unknown'→'clear' in the
    # narrow case where 'unknown' was caused SOLELY by new paths AND a verifiable subset exists — never when a
    # hub-dampened coupling (`dampened_with`) runs through an in-graph file, never when an unknown path is not
    # positively identified in `added_paths`, never when EVERY path is new, and never on a truncated analysis.
    # Honest-unknown is PRESERVED per-path: missing evidence is not Clear.
    verifiable_paths = [p for p in paths if p not in set(unknown_paths)]
    effective_verdict = _effective_verdict(
        verdict, unknown_paths, dampened_with, verifiable_paths, added_paths, truncated,
        engine_unknown=raw_verdict == "unknown",
    )
    # the standalone new-paths note attaches ONLY when the verifiable part got its OWN verdict (we split away from a
    # blanket 'unknown') AND new paths exist — i.e. exactly when `_render_warn_unknown_detail` does NOT already own
    # the unknown-paths copy (that helper still owns it for a genuinely-whole-PR 'unknown'). No double-render.
    split_from_verdict = effective_verdict != "unknown" and bool(unknown_paths)
    verdict = effective_verdict   # from here on the renderer speaks for the VERIFIABLE part (conclusion/icon/title/summary/copy)
    conclusion = _VERDICT_CONCLUSION.get(verdict, "neutral")
    icon = _VERDICT_ICON.get(verdict, "•")

    # the cluster (contention neighborhood) this PR belongs to — the N-scale unit: coordinate the group,
    # not N² pairs.
    cluster = next(
        (cl for cl in _dicts(impact.get("clusters"))   # drop any malformed (non-dict) cluster row before the .get(...) scan
         if change_ref in (cl.get("changes", []) or []) or my_label in (cl.get("agents", []) or [])),
        None,
    )

    # the finest locus we can name for a direct collision (a SYMBOL both PRs edit, else a line range) — the
    # "中身を持たずに、衝突点を細かくする" payoff (locate the clash without reading code). Pick the first
    # collision_point that resolved to something finer than the file; "" means we can only speak at file level.
    finer_point = next((p for p in (_fmt_collision_point(cp) for cp in collision_points) if p), "")

    # CLEAR-COPY HONESTY (the self-contradiction fix): the engine legitimately attaches CO-SIGNALS to a 'clear'
    # change — it can be a lane HOLDER others wait behind (`queued_behind`, the notify-holder design), it can
    # reserve a recurring hotspot others depend on (`shared_foundation`), and it can sit in a contention cluster
    # (cluster.size >= 2). 'clear' means "nothing is blocking _you_" (this PR is not itself waiting / coupled), NOT
    # "nothing else in flight touches this". The absolute clear wording ("nothing else touches this") flatly
    # contradicts the very sections that render below it ("N PRs are waiting behind this one" / "N PRs touch this
    # same area") — a customer-visible contradiction on exactly the busiest holder PRs. So when ANY co-signal is
    # present we switch to a NON-absolute phrasing that is true for the holder ("nothing is blocking you") and does
    # not deny the co-signals; with NO co-signal we keep today's exact absolute copy. Condition on "is this PR
    # itself blocked/coupled?" (the co-signals), not on the bare verdict string.
    cluster_size = _int(cluster.get("size")) if cluster else 0   # _int: a degraded read may hand back a STRING size → never crash the `>= 2` below
    # a co-change advisory line ("Historically changes together") renders below for a clear PR carrying a strong
    # coupling — it IS a co-signal, so it must flip the absolute "nothing else touches this" header off too,
    # exactly like queued_behind / shared_foundation (audit r5: without this, a clear PR with ONLY a co-change
    # line printed the self-contradiction "nothing else in flight touches this" directly above the co-change line).
    # cc_rows = the rows that will ACTUALLY render (degenerate rows dropped — see the guard below), computed ONCE
    # so this header decision and the render block can never disagree.
    cc_rows = [x for x in _dicts(cochange) if x.get("edited") and x.get("partner")
               and isinstance(x.get("prob"), (int, float)) and float(x.get("prob")) > 0]
    cc_present = bool(cc_rows)
    clear_has_cosignal = verdict == "clear" and bool(queued_behind or shared_foundation or cluster_size >= 2 or cc_present)
    # the human title lead for THIS render (the comment bold header + the check title). Default = the canonical
    # per-verdict title; for a clear PR carrying a co-signal, swap to the non-contradicting lead.
    if clear_has_cosignal:
        verdict_title = "Clear — nothing is blocking you"
    else:
        verdict_title = _VERDICT_TITLE.get(verdict, verdict)
    # STALE BRANCH-RESERVATION DECAY (#851): a serialize_soft with NO hard-blocker holder (serialize_behind empty)
    # is the IDLE-BRANCH-RESERVATION downgrade, NOT an append overlap on a build/list file — the engine dropped the
    # decayed holder from serialize_behind so it never reads as a "wait in line behind". The default serialize_soft
    # title says "on a build/list file", which is wrong here (the overlap is on real source held by an idle branch
    # reservation), so use honest copy. The CHECK title is split on " — " → still "Veripsa — Heads up" (unchanged).
    # GATED on empty `behind`: every EXISTING serialize_soft (low_value/append) always has a non-empty holder, so
    # this branch is unreachable for them → their copy is byte-identical.
    if verdict == "serialize_soft" and not behind:
        verdict_title = "Heads up — queued behind an idle branch reservation"

    # FORK INFO-LEAK GUARD: a fork PR's comment posts on the base-repo conversation the external contributor can
    # read. The full check below names the base repo's OTHER in-flight PRs (refs + author logins) and base-repo
    # paths/symbols — private in-flight structure. So for a fork we short-circuit to a REDACTED render: keep THIS
    # PR's own verdict (conclusion + human title) and a generic "coordinate with the maintainers" line; drop every
    # cross-PR identifier and base-repo path/symbol. We post a comment under the SAME less-noise gate as the full
    # render (something to coordinate / a shared foundation this PR holds / a holder with waiters / truncated) — but
    # the redacted body never reveals WHO or WHERE. A truly-clear fork PR with nothing nearby still posts none.
    if is_fork:
        # CONFLICT-MARKER ESCALATION on a fork: the marker is in the FORK contributor's own added code (they
        # see the path in their own diff already), so naming the file leaks no base-repo structure. Hard-fail.
        if _conflict_findings:
            return _build_conflict_marker_result(_conflict_findings, branch)
        title = "Veripsa — " + (verdict_title or "Heads up").split(" — ")[0]
        summary = _redacted_fork_summary(verdict, icon)
        post = verdict != "clear" or shared_foundation or queued_behind or truncated or split_from_verdict
        if post:
            body = _redacted_fork_comment(branch, icon, verdict_title, verdict)
            if truncated:   # the truncation disclosure is content-free (a count/notice, no other-PR id) → safe to keep
                body += ("\n\n> **Note — this PR is large; only the first files changed were analyzed for "
                         "coupling.** The verdict above covers the analyzed files only. Treat the unanalyzed "
                         "remainder as unknown, not clear.")
            # PER-PATH SPLIT on a fork: the verifiable part got its verdict; disclose that THIS PR's own new paths
            # aren't analyzed yet. These are the fork's OWN additions (the external contributor already sees them in
            # their diff), so naming them leaks no base-repo structure — but we keep the count out (PO 「file count
            # はダメ」) and frame them as unverified, never 'safe'. Additive; does not contradict the verdict above.
            # SPLIT (PO 2026-06-25): when added_paths is supplied, distinguish (a) new in this PR / expected from
            # (b) modified path NOT in Veripsa's graph / possible extractor gap. When None/empty, keep today's
            # lumped fork copy unchanged.
            if split_from_verdict and unknown_paths:
                _fnew, _fgap = _split_unknown_paths(unknown_paths, added_paths) if added_paths else ([], [])
                if _fnew and _fgap:
                    body += ("\n\n> **New paths in this PR — coupling will be computable after merge.** These paths "
                             f"are NEW in this PR, so there is no main-side graph to compare against yet (expected, "
                             f"not a warning): {_fmt_paths_no_count(_fnew)}. Veripsa will compute their coupling "
                             "once this PR lands.\n>\n"
                             "> **Some other paths aren't in this repo's graph yet (unverified, not 'safe').** "
                             "These paths are in main but NOT in the graph — an unsupported language, an un-indexed "
                             f"area, or an extractor gap: {_fmt_paths_no_count(_fgap)}. Treat as unknown, not clear.")
                elif _fnew and not _fgap:
                    body += ("\n\n> **New paths in this PR — coupling will be computable after merge.** These paths "
                             f"are NEW in this PR, so there is no main-side graph to compare against yet (expected, "
                             f"not a warning): {_fmt_paths_no_count(_fnew)}. Veripsa will compute their coupling "
                             "once this PR lands.")
                elif _fgap and not _fnew:
                    body += ("\n\n> **Some paths aren't in this repo's graph yet (unverified, not 'safe').** "
                             "The verdict above covers your changed files that are in the graph. These other "
                             "paths are in main but NOT in the graph — an unsupported language, an un-indexed "
                             f"area, or an extractor gap: {_fmt_paths_no_count(_fgap)}. Treat as unknown, not clear.")
                else:
                    body += ("\n\n> **Some new paths aren't analyzed yet (unverified, not 'safe').** The verdict above "
                             "covers your changed files that are in this repo's graph. These other paths aren't in the "
                             f"graph yet (a new file / unsupported language / un-indexed area): {_fmt_paths_no_count(unknown_paths)}. "
                             "Treat those as unknown, not clear.")
            comment = _cap_comment_body(body.split("\n"))
        else:
            comment = None
        return {"conclusion": conclusion, "title": title, "summary": summary, "comment": comment}

    # ---- the one-line summary (shows in the checks list) ----
    summary = _verdict_summary(verdict, icon, finer_point, behind, impact_paths, depends_on_changing,
                               contested_with, dampened_with, unknown_paths, clear_has_cosignal, branch,
                               truncated=truncated)

    # ---- the PR comment (markdown) ----
    # Build the comment body line-by-line into L; "\n".join(L) at the end yields the posted markdown. Each
    # section below appends a leading "" first to keep a blank line (paragraph break) between blocks. Order is
    # deliberate: header → CONFLICT-MARKER BLOCK (when present; sits ABOVE the verdict because it is the only
    # hard-fail signal) → what this PR reserves → shared-foundation flag → who's queued behind → upstream-
    # dependency warning → blast radius → the verdict's headline copy → cluster land-order → honesty footer.
    L: list[str] = []
    L.append(f"### Veripsa — heading to {_code(branch)}")
    # CONFLICT-MARKER ESCALATION (PO 2026-06-25 dogfood hole-fix, PRs #111/#114): a literal unresolved
    # `<<<<<<<` / `=======` / `>>>>>>>` triplet in code this PR added is a 100%-certain build-breaker — the
    # ONE hard-fail signal this otherwise advisory check ever emits. We RENDER THE BLOCK FIRST (above the
    # verdict copy) so a reader sees the build-breaker before any coupling/contention prose AND OVERRIDE the
    # conclusion → action_required + title → "Unresolved merge conflict markers". The contention render below
    # still runs (the PR may ALSO be in a coupling neighborhood — that signal isn't lost), just downgraded
    # under the hard-fail header.
    if _conflict_findings:
        _render_conflict_markers(L, _conflict_findings)
        L.append("")
    L.append(f"**{icon} {verdict_title}**")   # non-absolute when a clear PR carries a co-signal (see verdict_title)
    # DANGLING-LABEL GUARD: _fmt_paths_no_count([]) is "" (a null/empty reserve set from the engine), so an
    # unconditional "This PR reserves: " would render the label with nothing after it. Render the line — and its
    # leading blank — only when there is a non-empty reserve set; every following section prepends its own blank,
    # so skipping this leaves no double-blank. The other sections (blast radius, verdict copy) still carry the
    # signal. MOAT: route through _fmt_paths_no_count (not _fmt_list) so a long reserve set ends "…, and more",
    # never "(+N more)" — the count of reserved files is a file count (PO 2026-06-21 「file count はダメ」).
    reserves = _fmt_paths_no_count(paths)
    if reserves:
        L.append("")
        L.append(f"This PR reserves: {reserves}")

    # SPLIT-ADVICE: a load-bearing recurring-contention file this PR touches → "consider splitting".
    _render_split_advice(L, shared_foundation, verdict)

    # NOTIFY-THE-HOLDER: other in-flight PR(s) are now queued behind THIS PR's lane.
    _render_queued_behind(L, queued_behind, queued_behind_paths, verdict)

    # UPSTREAM-DEPENDENCY warning: a foundation you build on is shifting under you right now.
    _render_depends_on(L, depends_on_changing)

    if impact_paths:
        L.append("")
        # MOAT (PO 2026-06-21 「file count はダメ」): the downstream paths are NAMED (content-free), but the
        # overflow trailer must NOT be the raw "+N more" file count — _fmt_paths_no_count ends with a qualitative
        # "…, and more" so a wide fan-out reveals breadth, never the graph-size number.
        L.append(f"**Blast radius** (structurally downstream on {_code(branch)}): {_fmt_paths_no_count(impact_paths)}")

    # CONTENTION PROSE: the hard/soft direct-collision headline copy + the mechanical merge-conflict anticipation.
    # WARN-EMPHASIS DEFENSE-IN-DEPTH (PO 2026-06-25 finding 4): the mechanical merge-conflict heads-up is ONLY honest
    # on a verdict the engine derived from line-range geometry (serialize / serialize_soft — a direct same-path
    # collision with overlapping or same-insertion-point ranges). A WARN is semantic-only (a structural reference,
    # a shared table/config — never a textual line overlap), an UNKNOWN has no analyzable line ranges by construction,
    # and a CLEAR has no contention at all. `_render_collision_prose` already gates the heads-up on
    # `verdict in ("serialize", "serialize_soft")`, but defense-in-depth: scrub the inputs at the CALL SITE so a stray
    # engine flag (a future engine bug, a malformed payload, a fork-redaction edge case) cannot reach the renderer for
    # a non-collision verdict. Scrubbing here is byte-identical to today for every honest input (the inner gate would
    # have rejected it anyway); it just makes the no-merge-conflict-on-warn contract a HARD invariant the renderer
    # can't accidentally drop on a future refactor. Content-free (the scrub drops a bool + a list; no copy change).
    _mcl = merge_conflict_likely if verdict in ("serialize", "serialize_soft") else False
    _cps = conflict_points if verdict in ("serialize", "serialize_soft") else []
    _render_collision_prose(L, verdict, behind, finer_point, _mcl, _cps)

    # WARN coordinate copy + UNKNOWN detail (un-analyzable paths / a hub-dampened-suppressed coupling). When the
    # WHOLE PR is 'unknown' (no verifiable subset), this owns the unknown-paths copy.
    _render_warn_unknown_detail(L, verdict, contested_with, unknown_paths, dampened_with,
                                added_paths=added_paths)
    # PER-PATH SPLIT (the blanket-unknown root fix): when the verifiable part got its OWN verdict but the PR ALSO
    # touches new / not-in-graph paths, surface those SEPARATELY + honestly (never claim them 'safe') — additive,
    # it does not suppress the verifiable verdict above. No-op when the whole PR is 'unknown' (handled just above).
    _render_unverified_paths_note(L, unknown_paths, split_from_verdict, added_paths=added_paths)

    # CLUSTER LAND-ORDER: the deduped, capped, always-a-ref suggested order for the contention neighborhood.
    _render_land_order(L, cluster, changes, my_label, change_ref, repo)

    if truncated:   # HONESTY: a verdict over a PARTIAL file set must say so — never a confident "clear" about files Veripsa never read.
        L.append("")
        L.append("> **Note — this PR is large; only the first files changed were analyzed for coupling.** "
                 "The verdict above covers the analyzed files only; the rest were not checked for overlap. "
                 "Treat the unanalyzed remainder as unknown, not clear.")

    # CO-CHANGE (empirical / logical coupling): the "you touched A; historically B comes with it" hint the
    # structural graph is BLIND to. cc_rows was computed once near the top (the SAME degenerate-row guard the
    # header decision used) so this block and the clear-header decision can never disagree. NOT rendered on a fork
    # PR (the is_fork branch returned far above — these are base-repo paths the external contributor must not see).
    cc = cc_rows
    _render_cochange(L, cc)

    L.append("")
    L.append("<sub>Veripsa records what is heading to "
             f"{_code(branch)} and who reserved what — it does not assert correctness. "
             "Advisory by default; your branch-protection policy decides what blocks.</sub>")

    # LESS NOISE: a 'clear' PR (nothing else in flight touches it) gets the CHECK only — no comment. Commenting
    # on every clean PR is noise that trains the team to ignore Veripsa. We only comment when there is something
    # to coordinate (warn / serialize), to flag (unknown), to note a shared foundation this PR touches (a
    # load-bearing file is worth one quiet line even on an otherwise-clear PR), to tell a lane HOLDER that other
    # PR(s) are now queued behind it (queued_behind — a clear PR can still be holding up others, and "free the
    # lane" is actionable), OR when the analysis was TRUNCATED (a confident-looking 'clear' over a partial file
    # set must carry its honesty disclosure). The green check still says "clear"; the comment adds the fact.
    # _cap_comment_body is the FINAL net: the per-section loops bound item COUNT, this bounds the assembled body's
    # total LENGTH to GitHub's comment cap (and any single over-long line) while ALWAYS keeping THIS PR's own
    # land-order row + the framing — so the busiest, most-collision-prone PR never 422s into NO comment.
    # PER-PATH SPLIT also opens the comment gate: a PR whose verifiable part is 'clear' but that ALSO touches new /
    # not-in-graph paths (split_from_verdict) must post the standalone honest "some new paths aren't analyzed" note
    # — otherwise the green-check clear path posts no comment and the honest unverified-paths disclosure is lost.
    # CONFLICT-MARKER ESCALATION also opens the gate: an unresolved-marker PR ALWAYS posts (it is a hard fail
    # — silence on a build-breaker is the worst possible outcome).
    comment = _cap_comment_body(L) if (verdict != "clear" or shared_foundation or queued_behind or truncated
                                       or cc or split_from_verdict or _conflict_findings) else None

    # CONFLICT-MARKER FINAL OVERRIDE (PO 2026-06-25 dogfood hole-fix): when this PR has unresolved markers we
    # report `action_required` + a build-breaker title + a build-breaker summary that LEADS in the checks row.
    # The contention render already happened (the block rendered first, the verdict copy under it), so a real
    # coupling on the same PR is still visible in the comment body — only the conclusion/title/summary lead is
    # promoted to the hard-fail signal.
    if _conflict_findings:
        return {
            "conclusion": "action_required",
            "title": "Veripsa — Unresolved merge conflict markers",
            "summary": _conflict_marker_summary_line(_conflict_findings),
            "comment": comment,
        }

    return {
        "conclusion": conclusion,
        # human lead (e.g. "Wait in line"), never the raw engine verb — this is a customer surface. Derives from
        # verdict_title, so both the absolute and the clear-with-co-signal clear read the base signal "Clear" here
        # ("Clear to land" is forbidden as a base-signal name — the four base signals are Clear / Heads up / Wait
        # in line / Unknown). The co-signal distinction lives in the summary + comment header ("nothing is blocking
        # you"), not the title; the always-visible checks row stays in lock-step with the header + summary above.
        "title": "Veripsa — " + (verdict_title or "Heads up").split(" — ")[0],
        "summary": summary,
        "comment": comment,
    }


def _main(argv: list[str]) -> int:
    """CLI: feed main_impact_surface JSON on stdin, name the PR's agent → print the check + comment.
        psql ... -tAc "SELECT core.main_impact_surface('repo','main')" | python3 github-app/render.py <agent>
    """
    if len(argv) < 2:
        print("usage: render.py <change-id-or-agent-name>   (main_impact_surface JSON on stdin)", file=sys.stderr)
        return 2
    change_ref = argv[1]
    impact = json.load(sys.stdin)
    out = render_pr_check(impact, change_ref)
    print("── CHECK " + "─" * 60)
    print(f"conclusion : {out['conclusion']}")
    print(f"title      : {out['title']}")
    print(f"summary    : {out['summary']}")
    print("── COMMENT " + "─" * 59)
    print(out["comment"] if out["comment"] is not None else "(no comment — honest-empty)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
