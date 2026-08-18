#!/usr/bin/env python3
"""Veripsa — customer-surface SANITIZATION (content-free).

The single place that makes a customer-controlled string (a file path, a branch name, a PR author login, or an
UNRESOLVED internal label) safe to drop into a PR check / comment:
  * markdown + HTML escaping — a crafted name can neither inject HTML (GitHub renders raw HTML in PR comments)
    nor garble the layout;
  * the internal-role SCRUB — a Postgres service-role token (e.g. `veripsa_app`, an unresolved service identity)
    can never reach a customer surface, it renders as a neutral honest stand-in.

Split OUT of render.py on purpose: HOW we sanitize and WHAT the verdict copy says are independent concerns that
used to share one file and collide on nearly every PR (the hotspot lesson — finer files = finer collision
point). render.py re-exports these names, so callers/tests using `render._code` etc. are unchanged. Content-free:
only metadata (names, paths, labels) ever passes through here — never a file body. Kept in lock-step with
tests/test_no_jargon_leak.py's role family + escaping cases.
"""
from __future__ import annotations

import re

# Kept in lock-step with tests/test_no_jargon_leak.py's role family. The substitution is conservative: it only
# fires on the role-shaped token, so real logins ("veripsa", a user literally named that, or "alice") pass.
_INTERNAL_LABEL_RE = re.compile(
    r"veripsa_(?:app|migrator|writer|reader|owner|steward|demo|acme|token|agent|service|admin|bot)\w{0,64}",
    re.IGNORECASE,
)
_SAFE_AGENT_FALLBACK = "another in-flight change"
_ASCII_ONELINE_NEEDS_RE = re.compile(r"[\s\x00-\x1f\x7f-\x9f]")


def _safe_agent(label):
    """One agent DISPLAY label, scrubbed for the customer surface. A label carrying an internal Postgres
    service-role token (an unresolved service identity, never a human author) is replaced with a neutral,
    honest stand-in so a role name can never reach a PR. A genuine author login (a single-line reference token)
    passes through — collapsed to one line so a hostile label can never carry a multi-line body run into the
    text even where a caller drops it in WITHOUT going through `_safe` (e.g. the depends_on_changing `by` field
    and the land-order rows in render.py interpolate `_safe_agent(...)` directly into bold)."""
    if label in (None, ""):
        return label
    if _INTERNAL_LABEL_RE.search(str(label)):
        return _SAFE_AGENT_FALLBACK
    return _oneline(label)


# Customer-controlled strings (file paths, branch names, PR author logins) flow straight into this markdown. A
# path like ``a`b`` or a branch named `<img src=x onerror=...>` must NEVER break the layout or inject HTML/markdown
# (GitHub renders raw HTML in PR comments). Two safe sinks:
_MD_ESCAPE = set("\\`*_[]()~|")


# BIDI / INVISIBLE-FORMAT SPOOF DEFENSE (audit:unicode). A path / branch / login / symbol is customer-controlled
# and flows verbatim onto the PR surface. Two classes of zero-information formatting char would otherwise let a
# hostile value VISUALLY SPOOF a benign one — defeating the surface's whole job (faithful, content-free display):
#   * BIDIRECTIONAL OVERRIDES / EMBEDS / ISOLATES (U+202A–U+202E, U+2066–U+2069 + the RTL/LTR marks U+200E/F,
#     U+061C) — the "Trojan Source" trick: an RLO makes the text AFTER it render reversed, so `safe⁠<RLO>evil.py`
#     displays as a benign-looking name. The reader can no longer trust what the path IS.
#   * ZERO-WIDTH / INVISIBLE chars (ZWSP U+200B, ZWNJ U+200C, ZWJ U+200D, BOM/ZWNBSP U+FEFF, the U+2060 word-joiner,
#     U+180E) — split/duplicate a label invisibly so two visually-identical names differ in bytes (a silent spoof).
# Neither is part of any legitimate path/branch/login/symbol identity, so DROPPING them is content-free and changes
# no real value byte-for-byte. We also drop the remaining Unicode FORMAT category (Cf) chars defensively — they are
# non-printing by definition; a reference token never needs one.
_BIDI_FORMAT = (
    {0x200E, 0x200F, 0x061C}                  # LRM / RLM / Arabic letter mark
    | set(range(0x202A, 0x202F))              # LRE LRO RLE RLO PDF (+ U+202F handled as space below — see guard)
    | set(range(0x2066, 0x206A))              # LRI RLI FSI PDI
)
_ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x180E}


def _oneline(s) -> str:
    """Collapse a customer-controlled value to a SINGLE LINE of reference text: every whitespace RUN (newline,
    CR, tab, form-feed, unicode spaces) → ONE space, every C0/C1 control char is dropped, and every BIDI-control /
    zero-width / invisible FORMAT (Unicode Cf) char is DROPPED outright (a spoof char, never part of a real
    path/branch/login). CONTENT-FREE second layer: a path / branch / login / symbol is single-line, visible, and
    LTR-or-neutral by nature, so a legit value renders byte-identically — but a hostile value can no longer drag a
    multi-line body fragment, nor a Trojan-Source bidi override, nor an invisible zero-width spoof, onto the PR
    surface. (The engine's file-node JOINs already keep a graph `dst` from being a body; this guards the render
    layer itself, where `claim.target_path` / an agent label arrive only length-capped, not shape-capped.)"""
    import unicodedata
    s = "" if s is None else str(s)
    if s.isascii() and not _ASCII_ONELINE_NEEDS_RE.search(s):
        return s
    out, prev_space = [], False
    for ch in s:
        o = ord(ch)
        if o in _BIDI_FORMAT or o in _ZERO_WIDTH:        # bidi override / zero-width spoof → DROP (no space)
            continue
        if 0 <= o < 0x20 or 0x7F <= o <= 0x9F:           # C0/C1 control (incl. \n \r \t \f \v) → whitespace
            if not prev_space:
                out.append(" ")
            prev_space = True
        elif ch.isspace():                                # any other unicode whitespace (incl. U+202F NBSP) → one space
            if not prev_space:
                out.append(" ")
            prev_space = True
        elif unicodedata.category(ch) == "Cf":            # any remaining invisible FORMAT char → DROP (defensive)
            continue
        else:
            out.append(ch)
            prev_space = False
    return "".join(out).strip()


def _code(s) -> str:
    """Render an arbitrary string as an inline-code span that cannot break out. Inside a code span GitHub
    HTML-escapes the content (so `<`, `&`, markdown are inert) — the ONLY breakout is a backtick ending the span,
    so fence with one more backtick than the longest internal run and pad a space when the content touches a
    backtick edge (the CommonMark rule). A normal path/branch renders byte-identically (e.g. `src/app.py`).
    Newlines/control chars are first collapsed to single spaces (a reference token is single-line) so a hostile
    value can never carry a raw multi-line body run into the customer text."""
    s = _oneline(s)
    longest = cur = 0
    for ch in s:
        cur = cur + 1 if ch == "`" else 0
        longest = max(longest, cur)
    fence = "`" * (longest + 1)
    inner = f" {s} " if (s.startswith("`") or s.endswith("`")) else s
    return f"{fence}{inner}{fence}"


def _safe(s) -> str:
    """Escape a customer-controlled string for a NON-code markdown context (bold / plain text): collapse it to a
    single line (a reference token has no newline — see _oneline), neutralize HTML (`& < >`), and backslash-escape
    inline markdown punctuation, so a crafted name can neither inject HTML nor garble emphasis/links nor drag a
    multi-line body run into the text. A plain login like `alice PR-3` is unchanged (no active punctuation)."""
    s = _oneline(s)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "".join("\\" + ch if ch in _MD_ESCAPE else ch for ch in s)


def _fmt_list(items: list[str], limit: int = 8) -> str:
    # drop None / blanks first: a null path from the engine must never render as a literal "None" on a PR.
    items = [x for x in (items or []) if x not in (None, "")]
    if not items:
        return ""
    shown = items[:limit]
    more = len(items) - len(shown)
    s = ", ".join(_code(x) for x in shown)   # backtick-safe: a path can't break out of its code span
    return s + (f" (+{more} more)" if more > 0 else "")


def _fmt_agents(items: list[str], limit: int = 8) -> str:
    # drop None / blanks first: a null label must never surface as "behind **None**" / "shared with **None**".
    # scrub each label through the internal-label guard so an unresolved service-role id (veripsa_app) can never
    # render as a counterpart name on a PR.
    # De-dupe after scrubbing, because multiple lanes can point at the same PR/branch and multiple internal
    # service identities collapse to the same neutral fallback. The customer needs the distinct counterpart set,
    # not one repeated label per internal row.
    seen: set[str] = set()
    raw_items = items or []
    items = []
    for raw in raw_items:
        if raw in (None, ""):
            continue
        label = _safe_agent(raw)
        if label in (None, ""):
            continue
        key = str(label)
        if key in seen:
            continue
        seen.add(key)
        items.append(label)
    if not items:
        return ""
    shown = items[:limit]
    more = len(items) - len(shown)
    s = ", ".join(f"**{_safe(x)}**" for x in shown)   # escape: a crafted name can't inject HTML/markdown
    return s + (f" (+{more} more)" if more > 0 else "")

def _dicts(items) -> list:
    """Return only the dict entries of `items` (a list-ish from the engine JSON). A non-dict entry — None, a bare
    string/number, a malformed row — is dropped, so a single bad element never crashes a `.get(...)` loop and the
    well-formed rows still render. Returns [] for anything that isn't an iterable list of rows. (A defensive
    coercion helper for the render surface; lived in render.py until it was shared with render_pauseack.py.)"""
    if not isinstance(items, (list, tuple)):
        return []
    return [x for x in items if isinstance(x, dict)]


def _int(v) -> int:
    """A scalar engine field coerced to int, or 0 if it is not a clean integer (a degraded/partial read can hand
    back a STRING '3' or junk for a numeric field — `_dicts` guards a non-dict ROW, this guards a non-int FIELD).
    NEVER raises: the render's never-crash contract holds field-deep, not just row-deep. bool is excluded so a
    stray True/False reads as 0, not 1."""
    if isinstance(v, bool):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
