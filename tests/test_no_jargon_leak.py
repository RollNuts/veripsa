#!/usr/bin/env python3
"""NO-JARGON-LEAK gate — the customer-facing surface must never leak Veripsa internals.

Premise (PO 2026-06-18): the ONLY text a customer ever sees is the PR CHECK (title + summary) and the PR
COMMENT that `github-app/render.py` produces (it is "the product's face"). Today that output is clean — but
nothing KEEPS it clean. A future edit could splice in a raw internal token: an engine verb (`serialize`), a
Postgres role name (`veripsa_app`), an internal function id (`_claim_adjacency`), a DB/design term
(`SECURITY DEFINER`, `moat`, `append-only`), or one of the internal design phrases (`待ちを作る`). This gate is
that permanent guard. It ALSO guards three more customer-surface honesty classes: OVERCLAIM (a certainty/
guarantee an advisory product cannot make), and — PO 2026-06-21 「file count はダメ」 — MOAT TOPOLOGY COUNTS: a
raw count of files / nodes / symbols / edges / downstream dependents reveals the graph's SIZE (the moat) and
must NEVER reach a customer; coverage is shown as a PERCENTAGE only. See _scan_moat_counts / MOAT_COUNT_PATTERNS.

It is PURE + OFFLINE: render_pr_check is a stateless function over a main_impact_surface-shaped dict, so this
test needs no Postgres, no network, no deploy. We drive render_pr_check across a representative set of inputs
so EVERY customer-facing branch renders (clear / warn / serialize / unknown, the empty no-reservation case,
the depends_on_changing headline, and a multi-agent contention cluster), collect EVERY emitted customer
string (title + summary + comment), and assert none contains a term from an explicit, bounded DENYLIST.

Run:  python3 tests/test_no_jargon_leak.py     (no DB needed)
"""
from __future__ import annotations

import os
import re
import sys

# Import render.py the SAME way tests/test_server.py does: github-app has a hyphen so it is NOT an importable
# package — put that directory on sys.path and import the module by bare name.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import render as R  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# DENYLIST — the FRAME ("枠を決めて"): an explicit, documented, bounded set of internal tokens that must NEVER
# appear in customer-facing text. Each is matched case-insensitively as a word-ish token (so `serialize` does
# not flag `serialized` inside an unrelated English word boundary — but the bare engine verb IS caught). This
# list is intentionally finite; it is the contract, not a heuristic.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
DENYLIST: list[str] = [
    # ── Postgres ROLE names (and the generic prefix + a role word) ───────────────────────────────────────
    "veripsa_migrator",
    "veripsa_app",
    "veripsa_writer",
    "veripsa_reader",
    "veripsa_owner",
    "veripsa_steward",
    "veripsa_demo",
    "veripsa_acme",
    "veripsa_token",
    # the generic prefix followed by ANY role-ish word (catches veripsa_demo_agent, veripsa_*_writer, …)
    r"veripsa_(?:app|migrator|writer|reader|owner|steward|demo|acme|token|agent|service|admin|bot)\w*",
    # ── internal FUNCTION / identifier names ─────────────────────────────────────────────────────────────
    "_claim_adjacency",
    "_place_claim",
    "_promote_next_waiter",
    "establish_session_write_context",
    "mark_governed_write",
    "resolve_session_identity",
    "assert_governed_write",
    "_policy_int",
    # ── internal DB / design TERMS ───────────────────────────────────────────────────────────────────────
    "forgery",
    "governed_write",
    "append-only",
    "SECURITY DEFINER",
    "search_path",
    "ROW LEVEL SECURITY",
    "RLS",
    "moat",
    "substrate",
    "manifest",
    "_with_authority",
    # ── internal design JAPANESE phrases (the distilled-product prose that lives in code comments only) ───
    "待ちを作る",
    "作業内容を改めさせる",
    "止めるだけ",
]

# ALLOWED PRODUCT VOCABULARY — do NOT block these. This is the INTENTIONAL, customer-facing product language
# (PO-approved). Listed explicitly so the guard does not over-block the real product voice. (No assertion is
# made on these here; they are documented so a future maintainer knows the line between jargon and product.)
ALLOWED_PRODUCT_VOCAB: list[str] = [
    "lane", "reserve", "reservation", "in-flight", "blast radius", "branch", "PR",
    "queued", "land", "downstream", "Veripsa", "Control Tower",
]

# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# OVERCLAIM denylist — the HONESTY contract (PO: records-not-correctness; advisory, avoids+orders but does NOT
# resolve/prove/guarantee). Veripsa's check is NON-BLOCKING (`neutral` — it never gates a merge; the customer's
# own branch-protection policy decides what blocks). So no customer string may IMPLY a certainty / guarantee /
# enforced outcome the product cannot have: it does not prove correctness, does not block a merge, and cannot
# promise "no work is lost" or that a queued change "will" land. (Found by audit 2026-06-18: the serialize
# "Wait in line" body asserted "No work is lost; you just land in order" + "It will release these lanes … is
# then promoted automatically" — an absolute outcome guarantee + certain-future mechanics on an advisory queue.)
# Each pattern below is a phrase that, in customer prose, OVERCLAIMS. Matched case-insensitively as a regex.
# This is the permanent guard: a future edit that splices in a guarantee fails the build, naming the phrase.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
OVERCLAIM_PHRASES: list[str] = [
    # absolute outcome guarantees an advisory product cannot make
    r"no work is lost",
    r"nothing is lost",
    r"won'?t lose (any )?work",
    r"never lose (any )?work",
    r"guarantee\w*",
    r"\bensure[sd]?\b",
    r"\bprevent[sed]?\b",       # advisory: it flags/avoids, it does not PREVENT a merge or a conflict
    r"\bproves?\b", r"\bproven\b",
    r"\bcertain(ly)?\b(?!-free)",   # "certain" as a promise (allow the hedge "content-free", handled by lookahead+the honest "not certain")
    r"always safe", r"completely safe", r"fully safe", r"100% safe",
    r"can'?t conflict", r"cannot conflict", r"won'?t conflict", r"never conflicts?",
    r"will definitely",
    r"safe to merge", r"guaranteed to land",
    # certain-future mechanics on a NON-enforced (advisory) queue — promotion/release is CONDITIONAL on the other
    # PR actually landing-or-withdrawing AND on the author choosing the order; never an unconditional "will".
    r"will release these lanes",
    r"promoted automatically",   # the lane RECORD advances when the change ahead lands/withdraws; never imply an automatic merge or an enforced promotion
]

# HONEST HEDGES the copy is ALLOWED to use even though they contain a flagged stem — these are the CORRECT,
# conditional framing (the opposite of an overclaim) and must not be over-blocked. The overclaim scan strips
# these spans before matching, so "likely, not certain" / "the order is a suggestion, not a block" pass.
OVERCLAIM_HEDGE_OK: list[str] = [
    r"not certain",                 # "…\"likely\", not certain" — the merge-conflict hedge
    r"content-free",                # "content-free heuristic" (the "certain"-adjacent token)
    r"is a suggestion, not a block",  # the serialize order is advisory
    r"the order is a suggestion",
]
_OVERCLAIM_PATTERNS = [(t, re.compile(t, re.IGNORECASE)) for t in OVERCLAIM_PHRASES]
_OVERCLAIM_HEDGE_RE = re.compile("|".join(OVERCLAIM_HEDGE_OK), re.IGNORECASE)

# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# PROSE-DOC OVERCLAIM LINT — the SAME honesty contract, extended past the runtime render surface to the
# customer-facing PROSE DOCS (README.md, SECURITY.md, SUPPORT.md, and PRODUCT_FACTS.md):
# the overclaim gate only scanned render.py's output, so the marketing/legal prose could silently drift into a
# guarantee ("Veripsa guarantees correctness", "prevents bugs", "always safe to merge") with NO gate going red.
# This runs the SAME OVERCLAIM_PHRASES denylist over those docs — BUT the docs legitimately use several banned
# stems in the CORRECT, honest framing the product's voice depends on:
#   (1) NEGATED / disclaimer use — "does NOT prove rework-hours-saved", "it does **not** guarantee it detects
#       every collision", "It doesn't claim to prove your code is right", "It does NOT prove …". These are the
#       OPPOSITE of an overclaim; they are the honesty itself.
#   (2) EVIDENTIARY "prove(s) the SIGNAL" use — "the backtest proves the signal is real", "the gate suite proves
#       it", "Prove the signal on YOUR repo", "What it proves" (the evaluate.py report's signal proof). These
#       claim a measured SIGNAL/backtest/gate result — never product correctness — and are true + allowed.
# So the doc-lint skips a hit that is (1) immediately preceded by a negation, or (2) part of an allowed
# evidentiary "prove the signal/it/backtest" collocation. Tuned (measured, see the gate body) so the CURRENT
# docs pass with ZERO hits while a newly-spliced bare "guarantees correctness" / "prevents bugs" / "always safe
# to merge" / "proves your code is correct" still FAILS (a negative-control proves the teeth).
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Negation tokens that, appearing just BEFORE a flagged stem, mark a disclaimer (not an overclaim). The trailing
# class tolerates markdown bold/quote/paren punctuation between the negation and the stem ("does **not** guarantee").
_DOC_NEG_BEFORE = re.compile(
    r"(?:\bnot\b|\bnever\b|\bno\b|n't|\bcannot\b|\bwithout\b|\bdoes not\b|\bdoesn't\b|\bdon't\b)"
    r"[\s,.\"'*`()\-\w]{0,30}$",
    re.IGNORECASE,
)
# Allowed EVIDENTIARY collocations for the prove-family ONLY (the one non-negated stem the honest docs use): a
# measured claim about the SIGNAL / backtest / gate, never about correctness. "proves your code is correct" has
# no honest object here → NOT skipped (it fails). "proves the signal" / "gate suite proves it" → skipped.
_DOC_PROVE_EVID = re.compile(
    r"(?:\bbacktest\b|\bgate suite\b|\bwhat it\b|sufficient to|\bthis\b)\s+\w*\s*prov"
    r"|prov(?:e|es|en|ing)\b(?:\s+\w+){0,2}\s+(?:signal|coupling|it\b|real)"
    r"|prove\s+the\s+(?:warning\s+)?signal",
    re.IGNORECASE,
)


def _scan_overclaim_doc(label: str, text) -> list[str]:
    """Overclaim scan for PROSE DOCS: the SAME OVERCLAIM_PHRASES, but a hit is allowed (skipped) when it sits in
    a NEGATED/disclaimer context or is an EVIDENTIARY 'prove the signal/backtest/gate' collocation — the honest
    framings the customer-facing docs legitimately use. Everything else (a bare guarantee/ensure/prevent/safe-to
    -merge, or 'proves your code is correct') is an overclaim and is returned, naming the phrase + line."""
    if not text:
        return []
    out: list[str] = []
    for i, line in enumerate(text.splitlines(), 1):
        scrubbed = _OVERCLAIM_HEDGE_RE.sub(" ", line)
        for term, pat in _OVERCLAIM_PATTERNS:
            for m in pat.finditer(scrubbed):
                s, e = m.span()
                if _DOC_NEG_BEFORE.search(scrubbed[max(0, s - 50):s]):
                    continue  # negated → the disclaimer, not an overclaim
                if _DOC_PROVE_EVID.search(scrubbed[max(0, s - 60):e + 30]):
                    continue  # "proves the signal / it / backtest" → a measured claim, allowed
                out.append(f"[{label}:{i}] OVERCLAIM in prose doc — phrase {term!r} (matched {m.group(0)!r}) "
                           f"implies a certainty/guarantee an ADVISORY, content-free product cannot have: "
                           f"{line.strip()!r}")
    return out


def _scan_overclaim(label: str, text) -> list[str]:
    """Return overclaim violations for one customer string. Honest hedges are blanked first so the conditional,
    correct framing ("likely, not certain" / "a suggestion, not a block") is never mistaken for a guarantee."""
    if not text:
        return []
    scrubbed = _OVERCLAIM_HEDGE_RE.sub(" ", text)
    violations = []
    for term, pat in _OVERCLAIM_PATTERNS:
        m = pat.search(scrubbed)
        if m:
            violations.append(f"[{label}] OVERCLAIM — phrase {term!r} (matched {m.group(0)!r}) implies a "
                              f"certainty/guarantee an ADVISORY product cannot have, in: {text!r}")
    return violations


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# MOAT — NO RAW GRAPH-TOPOLOGY COUNTS (PO 2026-06-21 「file count はダメ」). The CLAUDE.md moat rule: "Coverage is
# shown as a percentage ONLY — never raw file/node/edge counts." The graph's SIZE (how many files / nodes /
# symbols / edges / downstream dependents a change reaches) is the moat; exposing the NUMBER lets a customer
# reconstruct what Veripsa built. The iter-5 audit found render.py shipped "blast radius (N downstream file(s))"
# — a raw file count — plus the same class in the split-advice ("imported by N files", "defines N symbols"),
# the "+N more file/part" overflow trailers, and the stale-base nudge ("N files you're editing changed").
#
# This denylist FAILS the build on any customer-facing string that binds a NUMBER to a graph-topology noun
# (file / path / node / symbol / edge / downstream / dependent / part), or the named shapes "blast radius (N…",
# "imported by N", "defines N", "changed N times" (churn), and the bare "+N more" overflow marker. It is the
# permanent guard for THIS class, so the leak cannot regress.
#
# WHAT STAYS ALLOWED (NOT matched — these are deliberately NOT graph-size counts):
#   • the coverage PERCENTAGE ("about 42%") — the ONE allowed quantitative moat surface (the `%` is whitelisted
#     by the negative-lookbehind on the digit, so "42%" never trips the rule).
#   • OPERATIONAL / QUEUE counts that are NOT graph topology — "N in-flight PRs waiting behind this", "N open PRs
#     touch this area", "+N more PR(s) further down the order", the numbered land-order list "1. PR-7 2. PR-6".
#     These are PR identities / queue positions (a coordination signal), not the file/node/edge size of the
#     graph, so they are explicitly excluded (the "+N more" rule's lookahead spares "…more PR(s)/change(s)").
#   • the co-change advisory line's normalized % + lift ("42% of the time (3.0× more than chance)") — a
#     probability basis, not a graph-topology size. NOTE (2026-06-23): the co-change line used to ALSO carry a raw
#     support count ("N of M changes"); that raw co-occurrence count is now DROPPED at the source (render.py) under
#     the moat rule "raw counts はダメ; coverage % OK", and gate 184 locks its absence. The % + lift remain, and
#     this MOAT scanner (graph-topology counts only) was never what gated it.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# A graph-topology noun: the unit whose COUNT reveals the graph's size. (An optional qualifier — downstream /
# dependent / upstream / recurring-contention — may sit in front; we still match the count.)
_TOPO_NOUN = (r"(?:downstream|dependent|upstream|recurring[- ]contention)?[- ]*"
              r"(?:file|files|path|paths|node|nodes|symbol|symbols|edge|edges|part|parts)\b")
MOAT_COUNT_PATTERNS: list[str] = [
    # "N <noun>" / "+N more <noun>" — a digit (with up to 3 qualifier words between) directly before a topology
    # noun. The (?<![\w%]) lookbehind means a PERCENTAGE ("42%") and a digit inside a longer token never match.
    r"(?<![\w%])\+?\s*\d+\s+(?:more\s+)?(?:\w+[- ]){0,3}" + _TOPO_NOUN,
    # verb forms that expose fan-in / symbol counts: "imported by **12 other files**", "defines **40 symbols**".
    r"(?:imported by|defines|imports)\s+\*{0,2}\d+",
    # the exact named shape the audit caught: "blast radius (5 downstream file(s))".
    r"blast radius\s*\(\s*\d+",
    # churn as a raw count: "changed **7 times** recently" (a per-file change count).
    r"changed\s+\*{0,2}\d+\s+time",
    # a BARE overflow marker "+N more" inside a PATH list (a file count) — render_safe._fmt_list emits "(+N more)"
    # after a comma-separated path list. Two deliberate exemptions, both OPERATIONAL counts (NOT graph size):
    #   • the AGENT/PR-label overflow "**name** (+N more)" (render_safe._fmt_agents) — a count of the other PRs /
    #     authors you share a neighborhood / queue with (same class as "N in-flight PRs waiting"); the two
    #     leading negative-lookbehinds ("** (" / "**(") spare it. (JUDGMENT CALL — kept; see the report.)
    #   • the queue overflow "(+N more PR(s)/change(s)/open …)" — a queue position; the lookahead spares it.
    r"(?<!\*\* \()(?<!\*\*\()\+\s*\d+\s+more\b(?!\s+(?:PR|PRs|change|changes|open))",
]
_MOAT_COUNT_PATTERNS = [(t, re.compile(t, re.IGNORECASE)) for t in MOAT_COUNT_PATTERNS]


def _scan_moat_counts(label: str, text) -> list[str]:
    """Return MOAT violations for one customer string: any raw graph-topology COUNT (file/node/edge/symbol/
    downstream/dependent/part) that leaks the graph's size. The coverage % and operational PR/queue counts are
    deliberately NOT matched (see the denylist header). Each hit names the matched fragment + the offending line."""
    if not text:
        return []
    out: list[str] = []
    for term, pat in _MOAT_COUNT_PATTERNS:
        for m in pat.finditer(text):
            out.append(f"[{label}] MOAT LEAK — raw graph-topology count {m.group(0)!r} (pattern {term!r}) reaches "
                       f"the customer; the graph's size must never be exposed (coverage is shown as a % only), in: "
                       f"{text!r}")
    return out


# RAW verdict tokens that must always be MAPPED to a human title (never surface bare as the whole check title).
RAW_VERDICTS = ["serialize", "warn", "clear", "unknown"]


def _compile(term: str) -> re.Pattern:
    """Word-ish, case-insensitive matcher. If `term` already looks like a regex (has a group/quantifier) use it
    as-is; otherwise wrap the escaped literal so it matches as a token (not a substring of a larger word)."""
    looks_regex = any(ch in term for ch in "()[]?+*\\")
    pat = term if looks_regex else r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])"
    return re.compile(pat, re.IGNORECASE)


_DENY_PATTERNS = [(t, _compile(t)) for t in DENYLIST]


def _scan(label: str, text) -> list[str]:
    """Return a list of human-readable violation strings for one customer-facing string."""
    if not text:
        return []
    violations = []
    for term, pat in _DENY_PATTERNS:
        m = pat.search(text)
        if m:
            violations.append(f"[{label}] leaked internal term {term!r} (matched {m.group(0)!r}) in: {text!r}")
    return violations


def _customer_strings(out: dict) -> list[tuple[str, str]]:
    """Every string a customer can actually SEE from one render: the check title, the check summary, and the
    comment body (if any). conclusion is an enum GitHub renders as an icon, not free text → not customer prose."""
    pairs = [("title", out.get("title")), ("summary", out.get("summary"))]
    if out.get("comment"):
        pairs.append(("comment", out.get("comment")))
    return pairs


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# REPRESENTATIVE INPUTS — a main_impact_surface-shaped dict per customer-facing branch. The shape mirrors
# core.main_impact_surface: { repo, branch, changes:[{change_id, label, agent, verdict, paths, impact,
# contested_with, serialize_behind, depends_on_changing, unknown_paths}], clusters:[{changes, agents, size,
# suggested_order}] }. Each scenario is (description, impact_dict, change_ref) and is chosen so the rendered
# output exercises a distinct code path in render_pr_check.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def scenarios() -> list[tuple[str, dict, str]]:
    base = {"repo": "acme/app", "branch": "main"}

    clear = {**base, "changes": [
        {"change_id": "PR-1", "label": "PR-1", "agent": "alice", "verdict": "clear",
         "paths": ["backend/auth.py"], "impact": [], "contested_with": [], "serialize_behind": [],
         "depends_on_changing": [], "unknown_paths": []},
    ]}

    warn = {**base, "changes": [
        {"change_id": "PR-2", "label": "PR-2", "agent": "bob", "verdict": "warn",
         "paths": ["backend/api.py"], "impact": ["backend/handlers.py", "backend/routes.py"],
         "contested_with": ["alice"], "serialize_behind": [], "depends_on_changing": [], "unknown_paths": []},
    ]}

    serialize = {**base, "changes": [
        {"change_id": "PR-3", "label": "PR-3", "agent": "carol", "verdict": "serialize",
         "paths": ["backend/api.py"], "impact": [], "contested_with": [],
         "serialize_behind": ["bob"], "depends_on_changing": [], "unknown_paths": []},
    ]}

    # FINER (symbol-level) collision point: a direct collision that names the SYMBOL the two PRs both edit (and,
    # in a second change, a LINE RANGE when no symbol could be resolved). Both render branches must stay
    # jargon-clean — the symbol name + line numbers are content-free, but no internal token may sneak in.
    serialize_finer = {**base, "changes": [
        {"change_id": "PR-3a", "label": "carol PR-3a", "agent": "carol", "verdict": "serialize",
         "paths": ["backend/api.py"], "impact": [], "contested_with": [],
         "serialize_behind": ["bob"], "depends_on_changing": [], "unknown_paths": [],
         "collision_points": [{"behind": "bob", "path": "backend/api.py", "symbol": "handle_charge",
                               "line_lo": 40, "line_hi": 70}]},
        {"change_id": "PR-3b", "label": "dee PR-3b", "agent": "dee", "verdict": "serialize",
         "paths": ["backend/util.py"], "impact": [], "contested_with": [],
         "serialize_behind": ["bob"], "depends_on_changing": [], "unknown_paths": [],
         "collision_points": [{"behind": "bob", "path": "backend/util.py", "symbol": None,
                               "line_lo": 140, "line_hi": 160}]},
    ]}

    unknown = {**base, "changes": [
        {"change_id": "PR-4", "label": "PR-4", "agent": "dave", "verdict": "unknown",
         "paths": ["new/thing.rs"], "impact": [], "contested_with": [], "serialize_behind": [],
         "depends_on_changing": [], "unknown_paths": ["new/thing.rs", "new/other.rs"]},
    ]}

    # empty / no-reservation: the App saw a PR but the engine has no record for it (first sync) → render_pr_check
    # returns its honest-empty branch (me is None).
    empty = {**base, "changes": []}

    # depends_on_changing headline: a foundation this PR builds on is shifting under it right now.
    depends = {**base, "changes": [
        {"change_id": "PR-5", "label": "PR-5", "agent": "erin", "verdict": "warn",
         "paths": ["service/consumer.py"], "impact": ["service/x.py"],
         "contested_with": ["frank"], "serialize_behind": [],
         "depends_on_changing": [{"path": "service/base.py", "by": "frank"}], "unknown_paths": []},
    ]}

    # multi-agent contention cluster: a neighborhood of entangled PRs with a suggested land order.
    multi = {**base,
             "changes": [
                 {"change_id": "PR-6", "label": "PR-6", "agent": "gina", "verdict": "warn",
                  "paths": ["core/engine.py"], "impact": ["core/a.py", "core/b.py"],
                  "contested_with": ["henry", "iris"], "serialize_behind": [],
                  "depends_on_changing": [], "unknown_paths": []},
             ],
             "clusters": [
                 {"changes": ["PR-6", "PR-7", "PR-8"], "agents": ["gina", "henry", "iris"], "size": 3,
                  "suggested_order": ["PR-7", "PR-6", "PR-8"]},
             ]}

    # shared foundation: an otherwise-CLEAR PR touches a load-bearing file (many import it AND it changes often) →
    # the note must reach whoever touches it, and must stay jargon-clean (no "fan-in"/"churn" raw, no internals).
    foundation = {**base, "changes": [
        {"change_id": "PR-9", "label": "PR-9", "agent": "judy", "verdict": "clear",
         "paths": ["core/config.py"], "impact": [], "contested_with": [], "serialize_behind": [],
         "depends_on_changing": [], "unknown_paths": [],
         "shared_foundation": [{"path": "core/config.py", "fan_in": 12, "churn": 7}]},
    ]}

    # SERVICE-ROLE COUNTERPART (the real leak this gate must hold): an agent DISPLAY label reaches the renderer
    # from core.agent_name(agent_id) = COALESCE(display_name, agent_id). A SERVICE identity with no display_name
    # row resolves to its RAW agent_id — which in the hosted path is the literal Postgres role string
    # `veripsa_app` (resolve_session_identity's COALESCE(o_agent, 'veripsa_app')). If a counterpart/holder/land
    # -order entry carries that, the renderer would put an internal ROLE name onto a customer PR. This scenario
    # injects it into EVERY agent-display field at once — contested_with, serialize_behind, depends_on_changing
    # .by, and the cluster's agents + suggested_order — so the DENYLIST scan above catches a regression in any of
    # them. (The renderer scrubs these to a neutral stand-in; this proves it, on a path the clean fixtures miss.)
    service_leak = {**base,
        "changes": [
            {"change_id": "PR-10", "label": "kara PR-10", "agent": "kara", "verdict": "warn",
             "paths": ["svc/a.py"], "impact": ["svc/b.py"],
             "contested_with": ["veripsa_app"], "serialize_behind": ["veripsa_app"],
             "depends_on_changing": [{"path": "svc/base.py", "by": "veripsa_app"}], "unknown_paths": []},
        ],
        "clusters": [
            {"changes": ["PR-10", "PR-11"], "agents": ["kara PR-10", "veripsa_app"], "size": 2,
             "suggested_order": ["veripsa_app", "kara PR-10"]},
        ]}

    return [
        ("clear (check only, no comment)", clear, "PR-1"),
        ("warn (semantic A→B exposure)", warn, "PR-2"),
        ("serialize (direct same-file collision, wait in line)", serialize, "PR-3"),
        ("serialize finer (names the symbol the two PRs both edit)", serialize_finer, "PR-3a"),
        ("serialize finer (names the line range when no symbol resolved)", serialize_finer, "PR-3b"),
        ("unknown (paths not in main's graph)", unknown, "PR-4"),
        ("empty / no reservation recorded yet", empty, "PR-1"),
        ("depends_on_changing headline (foundation shifting)", depends, "PR-5"),
        ("multi-agent contention cluster (suggested land order)", multi, "PR-6"),
        ("shared foundation (clear PR touches a load-bearing file)", foundation, "PR-9"),
        ("service-role counterpart in every agent-display field (unresolved service identity)", service_leak, "PR-10"),
    ]


def main() -> int:
    checks: list[tuple[str, bool]] = []
    all_violations: list[str] = []
    rendered_any_comment = False
    human_titles_seen: list[str] = []     # the mapped, human-readable verdict titles (they render in the COMMENT)

    for desc, impact, change_ref in scenarios():
        out = R.render_pr_check(impact, change_ref)
        # collect text from EVERY customer surface for the coverage self-check (human signals render in the
        # check title lead AND the comment body).
        human_titles_seen.append(" ".join(str(out.get(k) or "") for k in ("title", "summary", "comment")))

        # (1) the check TITLE must be a HUMAN phrase — never the raw lowercase engine verb, bare OR prefixed
        # ("Veripsa — serialize"). Found by dogfood: the real PR check title used to read "Veripsa — serialize".
        # Case-SENSITIVE on the lowercase tokens, so the human title "Veripsa — Clear" passes while a raw
        # "Veripsa — clear"/"… — serialize" fails (the engine verdict tokens are lowercase).
        title = (out.get("title") or "").strip()
        raw_titles = set(RAW_VERDICTS) | {f"Veripsa — {v}" for v in RAW_VERDICTS}
        bare = title in raw_titles
        checks.append((f"[{desc}] check title is a human title, not the raw verdict verb (title={title!r})", not bare))
        if bare:
            all_violations.append(f"[title] raw verdict verb used as the whole check title: {title!r}")

        # (2) no denylisted internal token AND no OVERCLAIM phrase in ANY customer-facing string (title + summary
        #     + comment). Jargon = leaks an internal; overclaim = implies a certainty/guarantee an advisory
        #     product cannot have. Both are customer-surface honesty failures; both fail the gate.
        for label, text in _customer_strings(out):
            v = _scan(label, text)
            oc = _scan_overclaim(label, text)
            mc = _scan_moat_counts(label, text)   # MOAT: no raw graph/file/node/edge/downstream count (PO 「file count はダメ」)
            checks.append((f"[{desc}] no internal jargon in {label}", not v))
            checks.append((f"[{desc}] no overclaim (advisory honesty) in {label}", not oc))
            checks.append((f"[{desc}] no raw graph-topology count (MOAT) in {label}", not mc))
            all_violations.extend(v)
            all_violations.extend(oc)
            all_violations.extend(mc)
            if label == "comment" and text:
                rendered_any_comment = True

    # ── PAUSE-ACK (一時停止) SURFACE: the pause / acknowledged / re-acknowledge copy is a NEW customer-facing
    #    surface (apply_pause_ack overlays the rendered check). It must hold the SAME jargon + overclaim contract —
    #    no internal token (ack_state names, role names) and no certainty/guarantee (it is advisory; the comment
    #    must say "never blocks by itself / does not assert correctness", never "ensures/prevents/guarantees").
    pa_base = {"repo": "acme/app", "branch": "main", "changes": [
        {"change_id": "PR-PA", "label": "PR-PA", "agent": "pa", "verdict": "serialize", "paths": ["svc/x.py"],
         "serialize_behind": ["maint PR-3"], "collision_points": [{"symbol": "f", "path": "svc/x.py"}]}]}
    pa_rendered = R.render_pr_check(pa_base, "PR-PA")
    pa_paused = R.apply_pause_ack(pa_rendered, pa_base, "PR-PA", label_present=False, prior_hash=None, branch="main")
    pa_snap = pa_paused["snapshot"]
    pa_acked = R.apply_pause_ack(pa_rendered, pa_base, "PR-PA", label_present=True, prior_hash=pa_snap, branch="main")
    pa_changed = {"repo": "acme/app", "branch": "main", "changes": [
        {"change_id": "PR-PA", "label": "PR-PA", "agent": "pa", "verdict": "serialize", "paths": ["svc/y.py"],
         "serialize_behind": ["bob PR-77"], "collision_points": [{"symbol": "g", "path": "svc/y.py"}]}]}
    pa_stale = R.apply_pause_ack(R.render_pr_check(pa_changed, "PR-PA"), pa_changed, "PR-PA",
                                 label_present=True, prior_hash=pa_snap, branch="main")
    for pa_desc, pa_out in (("pause-ack paused", pa_paused), ("pause-ack acknowledged", pa_acked),
                            ("pause-ack stale-reack", pa_stale)):
        for pa_label, pa_text in (("title", pa_out.get("title")), ("comment", pa_out.get("comment"))):
            pa_v = _scan(pa_label, pa_text)
            pa_oc = _scan_overclaim(pa_label, pa_text)
            pa_mc = _scan_moat_counts(pa_label, pa_text)   # MOAT: the pause/ack surface must not leak a topology count either
            checks.append((f"[{pa_desc}] no internal jargon in {pa_label}", not pa_v))
            checks.append((f"[{pa_desc}] no overclaim (advisory honesty) in {pa_label}", not pa_oc))
            checks.append((f"[{pa_desc}] no raw graph-topology count (MOAT) in {pa_label}", not pa_mc))
            all_violations.extend(pa_v)
            all_violations.extend(pa_oc)
            all_violations.extend(pa_mc)

    # ── CORRECTNESS (not just jargon): the customer text must say something TRUE. Found by audit:
    #    C1 the warn summary read "affects 0 downstream file(s)" when the driver was an UPSTREAM dependency;
    #    C2 it dangled "shared with ." when no counterpart was known; C3 a null label printed a literal "None".
    cbase = {"repo": "acme/app", "branch": "main"}
    warn_upstream = {**cbase, "changes": [{"change_id": "PR-U", "label": "PR-U", "agent": "u", "verdict": "warn",
        "paths": ["a.py"], "impact": [], "contested_with": ["PR-V"], "serialize_behind": [],
        "depends_on_changing": [{"path": "base.py", "by": "PR-V"}], "unknown_paths": []}]}
    su = R.render_pr_check(warn_upstream, "PR-U").get("summary") or ""
    checks.append(("C1: an upstream-driven warn names the real driver, never 'affects 0 downstream'",
                   "affects 0" not in su and "0 downstream" not in su))

    warn_noparty = {**cbase, "changes": [{"change_id": "PR-W", "label": "PR-W", "agent": "w", "verdict": "warn",
        "paths": ["a.py"], "impact": ["b.py"], "contested_with": [], "serialize_behind": [],
        "depends_on_changing": [], "unknown_paths": []}]}
    sw = R.render_pr_check(warn_noparty, "PR-W").get("summary") or ""
    checks.append(("C2: a warn with no known counterpart never dangles 'shared with .'",
                   "shared with ." not in sw and "shared with  " not in sw))

    none_label = {**cbase, "changes": [{"change_id": "PR-N", "label": "PR-N", "agent": "n", "verdict": "serialize",
        "paths": ["a.py"], "impact": [], "contested_with": [], "serialize_behind": [None, "carol"],
        "depends_on_changing": [], "unknown_paths": []}]}
    on = R.render_pr_check(none_label, "PR-N")
    checks.append(("C3: a null label never renders as a literal 'None' in the customer output",
                   "None" not in (on.get("summary") or "") and "None" not in (on.get("comment") or "")))

    # ── R1 escaping: customer-controlled paths/labels (file names, branches, author logins) flow into this
    #    markdown — a backtick must not break a code span, and a crafted name must not inject HTML/markdown.
    checks.append(("R1: a normal path renders byte-identically as inline code", R._code("src/app.py") == "`src/app.py`"))
    checks.append(("R1: a backtick in a path is fenced — can't break out of the code span", R._code("a`b") == "``a`b``"))
    checks.append(("R1: a path touching a backtick edge is space-padded (CommonMark)", R._code("`x") == "`` `x ``"))
    checks.append(("R1: a crafted label is HTML-escaped — no raw <img>",
                   "<img" not in R._safe("<img src=x onerror=alert(1)>") and "&lt;img" in R._safe("<img src=x onerror=alert(1)>")))
    checks.append(("R1: markdown emphasis in a label is neutralized (no live **/__)", R._safe("a*b_c") == "a\\*b\\_c"))
    evil = {**cbase, "changes": [{"change_id": "PR-X", "label": "PR-X", "agent": "x", "verdict": "warn",
        "paths": ["a.py"], "impact": ["b.py"], "contested_with": ["<img src=x onerror=alert(1)>"], "serialize_behind": [],
        "depends_on_changing": [], "unknown_paths": []}]}
    ev = R.render_pr_check(evil, "PR-X")
    evblob = (ev.get("summary") or "") + (ev.get("comment") or "")
    checks.append(("R1: an HTML-payload agent name never lands as raw HTML in the customer output",
                   "<img" not in evblob and "onerror=alert" in evblob.replace("&lt;", "<").replace("&gt;", ">")))
    #    C4 a counterpart label that resolved to the raw service-role string (an unresolved service identity →
    #    core.agent_name returns the literal `veripsa_app`) must be SCRUBBED to a neutral, honest stand-in — never
    #    rendered as the role name (that token is on the DENYLIST, so the scan would also catch it; this asserts the
    #    POSITIVE replacement happened, so the guard isn't merely "absent token" by luck). All four agent-display
    #    surfaces in one input: summary, depends_on_changing.by, the coordinate line, and the cluster land order.
    svc = {**cbase,
        "changes": [{"change_id": "PR-S", "label": "sam PR-S", "agent": "sam", "verdict": "warn",
            "paths": ["a.py"], "impact": ["b.py"], "contested_with": ["veripsa_app"],
            "serialize_behind": ["veripsa_app"],
            "depends_on_changing": [{"path": "base.py", "by": "veripsa_app"}], "unknown_paths": []}],
        "clusters": [{"changes": ["PR-S", "PR-T"], "agents": ["sam PR-S", "veripsa_app"], "size": 2,
            "suggested_order": ["veripsa_app", "sam PR-S"]}]}
    os_ = R.render_pr_check(svc, "PR-S")
    svc_blob = (os_.get("summary") or "") + "\n" + (os_.get("comment") or "")
    checks.append(("C4: an unresolved service-role counterpart is scrubbed to a neutral stand-in, not a role name",
                   "veripsa_app" not in svc_blob and "another in-flight change" in svc_blob))
    # C4b: a GENUINE author login that merely CONTAINS 'veripsa' (e.g. a user named 'veripsa') must NOT be
    # over-blocked — only the role-shaped token is scrubbed. The stand-in must NOT appear for a real login.
    real = {**cbase, "changes": [{"change_id": "PR-R2", "label": "veripsa PR-R2", "agent": "veripsa",
        "verdict": "serialize", "paths": ["a.py"], "impact": [], "contested_with": [],
        "serialize_behind": ["veripsa"], "depends_on_changing": [], "unknown_paths": []}]}
    or_ = R.render_pr_check(real, "PR-R2")
    or_blob = (or_.get("summary") or "") + "\n" + (or_.get("comment") or "")
    checks.append(("C4b: a real login containing 'veripsa' is NOT over-blocked (only the role token is scrubbed)",
                   "veripsa" in or_blob and "another in-flight change" not in or_blob))

    #    C5 HONESTY (partial analysis): a mega-PR whose file set was capped is only PARTIALLY analyzed. A
    #    truncated 'clear' must NOT post a bare green check with no comment — it must disclose that the verdict
    #    covers the analyzed files only (never a confident "clear" about files Veripsa never read). And the
    #    disclosure itself must be jargon-clean. A NON-truncated clear still posts no comment (less-noise intact).
    tclear = {**cbase, "changes": [{"change_id": "PR-TC", "label": "tom PR-TC", "agent": "tom", "verdict": "clear",
        "paths": ["a.py"], "impact": [], "contested_with": [], "serialize_behind": [],
        "depends_on_changing": [], "unknown_paths": []}]}
    trunc = R.render_pr_check(tclear, "PR-TC", truncated=True)
    checks.append(("C5: a TRUNCATED clear PR posts a comment that discloses the analysis was partial",
                   bool(trunc.get("comment")) and "analyzed" in (trunc.get("comment") or "").lower()
                   and "unknown, not clear" in (trunc.get("comment") or "").lower()))
    checks.append(("C5b: a NON-truncated clear PR still posts NO comment (less-noise policy intact)",
                   R.render_pr_check(tclear, "PR-TC", truncated=False).get("comment") is None))
    for label, text in _customer_strings(trunc):   # the disclosure must also be jargon-clean, overclaim-free AND count-free
        all_violations.extend(_scan("truncated-" + label, text))
        all_violations.extend(_scan_overclaim("truncated-" + label, text))
        all_violations.extend(_scan_moat_counts("truncated-" + label, text))

    #    H1 ADVISORY HONESTY (POSITIVE): the serialize "Wait in line" body and the lane-HOLDER body are the two
    #    places that describe Veripsa's reservation QUEUE — the spot most tempting to overclaim (it reads like a
    #    real lock). The check is NON-BLOCKING, so the queue is a RECORD, not enforced: the body must (a) carry an
    #    explicit advisory hedge, and (b) frame promotion as CONDITIONAL on the change ahead landing-OR-withdrawing
    #    (never an unconditional "will release … is then promoted"). Assert the honest framing is PRESENT (the
    #    overclaim scan above asserts the bad phrasing is ABSENT; this asserts the good phrasing did not silently
    #    vanish in a future reword, which would leave a bare, mechanism-y queue claim).
    ser = {**cbase, "changes": [{"change_id": "PR-WL", "label": "wil PR-WL", "agent": "wil", "verdict": "serialize",
        "paths": ["a.py"], "impact": [], "contested_with": [], "serialize_behind": ["carol"],
        "depends_on_changing": [], "unknown_paths": []}]}
    ser_c = (R.render_pr_check(ser, "PR-WL").get("comment") or "").lower()
    # WORDING AUDIT 2026-06-20: the serialize body NO LONGER carries an inner "(advisory — … not a block)"
    # parenthetical — that line CONTRADICTED the pause-ack tier (a paused serialize check IS gated until acked, so
    # "not a block" lower in the SAME comment fought the pause banner). The advisory frame is carried ONCE, at the
    # comment level, by the honest footer ("advisory by default; your branch-protection policy decides what blocks")
    # — true whether or not this PR is in the pause tier. So assert the advisory framing is present at the COMMENT
    # level (the footer), not that the serialize body re-asserts "not a block".
    checks.append(("H1: the serialize comment is framed ADVISORY at the comment level (the honest footer: 'advisory "
                   "by default; your branch-protection policy decides what blocks') — and the body no longer "
                   "contradicts a possible pause with an inner 'not a block' parenthetical",
                   "advisory by default" in ser_c and "decides what blocks" in ser_c
                   and "the order is a suggestion, not a block" not in ser_c))
    checks.append(("H1b: serialize promotion is CONDITIONAL on the change ahead landing OR withdrawing (no "
                   "unconditional 'will release … promoted')",
                   ("withdraw" in ser_c or "withdraws" in ser_c) and "will release these lanes" not in ser_c))
    hold = {**cbase, "changes": [{"change_id": "PR-HD", "label": "hod PR-HD", "agent": "hod", "verdict": "clear",
        "paths": ["a.py"], "impact": [], "contested_with": [], "serialize_behind": [],
        "queued_behind": ["PR-2", "PR-3"], "queued_behind_paths": ["a.py"],
        "depends_on_changing": [], "unknown_paths": []}]}
    hold_c = (R.render_pr_check(hold, "PR-HD").get("comment") or "").lower()
    # WORDING AUDIT 2026-06-20: the lane-HOLDER block NO LONGER carries the "(advisory — nothing is blocked.)"
    # parenthetical — it denied blocking inside a comment the pause-ack tier can convert to action_required (a
    # contradiction). The advisory frame is carried once by the comment-level footer; the holder body stays
    # CONDITIONAL (the reservation advances when the change ahead lands OR withdraws) with no mechanical
    # "promoted automatically". Assert the conditional, non-overclaiming framing survives + the comment-level
    # advisory footer is present (the honest frame did not silently vanish).
    checks.append(("H1c: the lane-HOLDER body stays CONDITIONAL (lands OR withdraws) with no mechanical 'promoted "
                   "automatically', and the comment carries the advisory footer ('advisory by default') — no inner "
                   "'nothing is blocked' denial that would contradict a pause",
                   "advisory by default" in hold_c and "withdraw" in hold_c
                   and "promoted automatically" not in hold_c and "nothing is blocked" not in hold_c))

    #    P1 PARAGRAPH-STRUCTURE (run-on fix): an 'unknown' PR that carries BOTH unverified paths (unknown_paths)
    #    AND a hub-dampened coupling (dampened_with) renders TWO distinct `**bold**` facts. Every other multi-block
    #    section prefixes a blank line so GitHub renders separate paragraphs; the dampened_with block used to be
    #    joined to the unknown_paths block by a single "\n", so GitHub ran the two bolded statements into ONE
    #    paragraph (audit). Assert the blank line is present between them (a literal blank line = two consecutive
    #    "\n" → "\n\n" in the joined body) so a future edit can't re-introduce the run-on. The two bolded leads are
    #    "**Not analyzed" (unknown_paths) and "**A coupling" (dampened_with).
    unk_both = {**cbase, "changes": [{"change_id": "PR-UB", "label": "PR-UB", "agent": "ub", "verdict": "unknown",
        "paths": ["new/mod.py"], "impact": [], "contested_with": [], "serialize_behind": [],
        "depends_on_changing": [], "unknown_paths": ["new/mod.py"],
        "dampened_with": [{"by": "vee", "via_hub": "util/common.py"}]}]}
    ub_c = R.render_pr_check(unk_both, "PR-UB").get("comment") or ""
    checks.append(("P1: unknown with BOTH unknown_paths and a dampened coupling renders the two bold blocks as "
                   "SEPARATE paragraphs (a blank line between them — no run-on)",
                   "**Not analyzed" in ub_c and "**A coupling" in ub_c
                   and "not clear.\n\n**A coupling" in ub_c))

    #    P2 MIXED-SIGNAL (the hurry-AND-wait fix): a 'serialize' PR is itself queued behind a holder — its dominant
    #    instruction is "wait in line". When it ALSO holds a lane (queued_behind) and/or touches a shared foundation,
    #    the holder/foundation copy used to tell the author to "land it promptly so you free the lane / so you are not
    #    holding a shared lane" — telling them to HURRY while the headline says WAIT (audit). On a serialize verdict
    #    that promptness nudge must be GATED OFF (the wait headline must not be undercut); on a clear holder it stays.
    ser_mix = {**cbase, "changes": [{"change_id": "PR-MX", "label": "PR-MX", "agent": "mx", "verdict": "serialize",
        "paths": ["core/engine.py"], "impact": [], "contested_with": [], "serialize_behind": ["zoe"],
        "queued_behind": ["yan"], "queued_behind_paths": ["core/engine.py"],
        "shared_foundation": [{"path": "core/engine.py", "fan_in": 12, "churn": 5, "basis": "foundation"}],
        "depends_on_changing": [], "unknown_paths": []}]}
    mx_c = (R.render_pr_check(ser_mix, "PR-MX").get("comment") or "")
    # WORDING AUDIT 2026-06-20: the serialize body now leads with the ACTION "**Land in order.**" instead of
    # re-stating the bold "**Wait in line.**" — the "Wait in line" headline is ALREADY the comment header, so the
    # body block was the same phrase TWICE (P4). The hurry-AND-wait gate is unchanged: a serialize PR must not be
    # told to "land it promptly". Assert both: no hurry nudge, and the body leads with the action (not a duplicate
    # headline). The "Wait in line" headline still leads the comment HEADER (the first bold line).
    mx_header = (mx_c.split("\n")[1] if len(mx_c.split("\n")) > 1 else "")
    checks.append(("P2: a serialize PR that also holds a lane / shared foundation does NOT tell the author to "
                   "'land promptly' (no hurry-AND-wait); the body leads with 'Land in order.' (not a duplicated "
                   "'Wait in line.'), and the 'Wait in line' headline stays in the comment HEADER",
                   "land it promptly" not in mx_c.lower() and "**Land in order.**" in mx_c
                   and "**Wait in line." not in mx_c and "Wait in line" in mx_header))
    # P2b: a CLEAR lane-holder (not itself waiting) KEEPS the 'free the lane' urgency — the gate is verdict-specific.
    clr_hold = {**cbase, "changes": [{"change_id": "PR-CH", "label": "PR-CH", "agent": "ch", "verdict": "clear",
        "paths": ["a.py"], "impact": [], "contested_with": [], "serialize_behind": [],
        "queued_behind": ["wim"], "queued_behind_paths": ["a.py"], "depends_on_changing": [], "unknown_paths": []}]}
    ch_c = (R.render_pr_check(clr_hold, "PR-CH").get("comment") or "")
    checks.append(("P2b: a CLEAR lane-holder (not itself waiting) still gets the 'land promptly so you free the "
                   "lane' urgency (the mixed-signal gate is verdict-specific, not a blanket removal)",
                   "free the lane" in ch_c.lower()))

    #    P3 DEGENERATE / DANGLING copy (polish): a 'serialize' with an EMPTY serialize_behind used to render a
    #    dangling "queued behind  — land in order." (double space, no name); fall back to a generic counterpart.
    #    And an empty 'paths' used to render "This PR reserves: " with nothing after — that line must be skipped.
    deg_ser = {**cbase, "changes": [{"change_id": "PR-DG", "label": "PR-DG", "agent": "dg", "verdict": "serialize",
        "paths": ["a.py"], "impact": [], "contested_with": [], "serialize_behind": [],
        "depends_on_changing": [], "unknown_paths": []}]}
    dg_sum = R.render_pr_check(deg_ser, "PR-DG").get("summary") or ""
    checks.append(("P3: a serialize with an empty serialize_behind names a generic counterpart, never a dangling "
                   "'queued behind  ' (double space)",
                   "queued behind  " not in dg_sum and "another in-flight change" in dg_sum))
    empty_paths = {**cbase, "changes": [{"change_id": "PR-EP", "label": "PR-EP", "agent": "ep", "verdict": "warn",
        "paths": [], "impact": ["b.py"], "contested_with": ["pal"], "serialize_behind": [],
        "depends_on_changing": [], "unknown_paths": []}]}
    ep_c = R.render_pr_check(empty_paths, "PR-EP").get("comment") or ""
    checks.append(("P3b: a PR with an empty reserve set SKIPS the 'This PR reserves:' line (no dangling label)",
                   "This PR reserves:" not in ep_c))

    #    P4 NOISY-DOGFOOD POLISH (#624): repeated partner labels should collapse to the distinct counterpart set,
    #    and synthetic hub placeholders such as BODY must not surface as if they were real customer paths/symbols.
    #    Unknown stays unknown; only the displayed names are cleaned.
    noisy_dampened = {**cbase, "changes": [{"change_id": "PR-ND", "label": "PR-ND", "agent": "nd",
        "verdict": "unknown", "paths": ["x.py"], "impact": [], "contested_with": [],
        "serialize_behind": [], "depends_on_changing": [], "unknown_paths": [],
        "dampened_with": [
            {"by": "bob PR-2", "via_hub": "BODY"},
            {"by": "bob PR-2", "via_hub": "BODY"},
        ]}]}
    nd_c = R.render_pr_check(noisy_dampened, "PR-ND").get("comment") or ""
    checks.append(("P4: duplicate dampened_with partner labels render once, and BODY placeholder is scrubbed",
                   nd_c.count("bob PR-2") == 1 and "`BODY`" not in nd_c and
                   "through a heavily-shared file that many files depend on" in nd_c))

    noisy_order = {**cbase,
        "changes": [{"change_id": "PR-1", "label": "PR-1", "agent": "a", "verdict": "warn",
            "paths": ["x.py"], "impact": ["y.py"], "contested_with": ["bob PR-2", "bob PR-2"],
            "serialize_behind": [], "depends_on_changing": [], "unknown_paths": []}],
        "clusters": [{"changes": ["PR-2", "PR-2", "PR-1", "PR-1"], "agents": ["bob", "bob", "a"],
            "size": 4, "suggested_order": ["bob PR-2", "bob PR-2", "PR-1", "PR-1"]}]}
    no_c = R.render_pr_check(noisy_order, "PR-1").get("comment") or ""
    checks.append(("P4b: duplicate partner labels and land-order rows collapse to distinct PRs",
                   no_c.count("bob PR-2") == 1 and "2 open PRs touch this same area" in no_c
                   and "3. **" not in no_c))

    #    S1 COMMENT-SIZE BOUND (robustness): GitHub REJECTS a PR/issue comment body over 65536 chars (HTTP 422) —
    #    and a rejected POST means the customer gets NO Veripsa comment AT ALL, on EXACTLY the busiest, most-
    #    collision-prone PR (a huge contention neighborhood / a pile of shared foundations), where the comment is
    #    needed most. render.py builds the body from lists the customer controls the SIZE of: the cluster
    #    suggested_order, shared_foundation, and depends_on_changing each render ONE line PER item. Probe each
    #    pathological input (2000 entries, long labels/paths) and the all-at-once worst case, and assert the
    #    rendered comment stays under a SAFE bound (60000 — headroom below GitHub's 65536). The bound is what is
    #    asserted: if render.py caps these lists, the comment stays small; if a future edit removes a cap, this
    #    fails. The capped/overflow text must ALSO be jargon-clean, so it is fed through the leak scan too.
    SAFE_COMMENT_BOUND = 60000   # GitHub's hard cap is 65536; we keep clear headroom
    LBL = "agent-with-a-very-long-display-name-%d-" + "x" * 40   # ~80-char labels (real org/team display names get long)
    PTH = "core/some/deeply/nested/module/path/file-%d-" + "x" * 40 + ".py"
    bigN = range(2000)

    # S1a: a 2000-PR contention neighborhood with long labels (the suggested_order loop).
    order_big = [LBL % i for i in bigN]
    cluster_big = {**cbase,
        "changes": [{"change_id": "PR-BIG", "label": "PR-BIG", "agent": "a", "verdict": "warn",
            "paths": ["core/engine.py"], "impact": ["core/a.py"], "contested_with": ["x"],
            "serialize_behind": [], "depends_on_changing": [], "unknown_paths": []}],
        "clusters": [{"changes": order_big, "agents": order_big, "size": 2000, "suggested_order": order_big}]}
    cb = R.render_pr_check(cluster_big, "PR-BIG").get("comment") or ""
    checks.append((f"S1a: a 2000-PR contention neighborhood (long labels) keeps the comment < {SAFE_COMMENT_BOUND} "
                   f"chars (GitHub rejects > 65536) — got {len(cb)}", len(cb) < SAFE_COMMENT_BOUND))

    # S1b: 2000 shared-foundation files with long paths (the shared_foundation loop) — an otherwise-clear PR.
    sf_big = [{"path": PTH % i, "fan_in": 12, "churn": 7} for i in bigN]
    found_big = {**cbase, "changes": [{"change_id": "PR-SF", "label": "PR-SF", "agent": "a", "verdict": "clear",
        "paths": ["x.py"], "impact": [], "contested_with": [], "serialize_behind": [],
        "depends_on_changing": [], "unknown_paths": [], "shared_foundation": sf_big}]}
    sfb = R.render_pr_check(found_big, "PR-SF").get("comment") or ""
    checks.append((f"S1b: 2000 shared-foundation files (long paths) keep the comment < {SAFE_COMMENT_BOUND} chars "
                   f"— got {len(sfb)}", len(sfb) < SAFE_COMMENT_BOUND))

    # S1c: 2000 upstream dependencies being changed (the depends_on_changing loop), long paths + long labels.
    doc_big = [{"path": PTH % i, "by": LBL % i} for i in bigN]
    dep_big = {**cbase, "changes": [{"change_id": "PR-DC", "label": "PR-DC", "agent": "a", "verdict": "warn",
        "paths": ["x.py"], "impact": [], "contested_with": [], "serialize_behind": [],
        "depends_on_changing": doc_big, "unknown_paths": []}]}
    dcb = R.render_pr_check(dep_big, "PR-DC").get("comment") or ""
    checks.append((f"S1c: 2000 shifting upstream dependencies (long paths/labels) keep the comment < "
                   f"{SAFE_COMMENT_BOUND} chars — got {len(dcb)}", len(dcb) < SAFE_COMMENT_BOUND))

    # S1d: EVERY size-controlled list pathological AT ONCE + long paths/impact/contested/unknown + truncated — the
    # true worst case the busiest PR can produce. All of it together must still fit under the safe bound.
    paths_big = [PTH % i for i in bigN]
    all_big = {**cbase,
        "changes": [{"change_id": "PR-ALL", "label": "PR-ALL", "agent": "a", "verdict": "warn",
            "paths": paths_big, "impact": paths_big, "contested_with": [LBL % i for i in bigN],
            "serialize_behind": [LBL % i for i in bigN], "depends_on_changing": doc_big,
            "unknown_paths": paths_big, "shared_foundation": sf_big}],
        "clusters": [{"changes": order_big, "agents": order_big, "size": 2000, "suggested_order": order_big}]}
    ab = R.render_pr_check(all_big, "PR-ALL", truncated=True).get("comment") or ""
    checks.append((f"S1d: EVERY size-controlled list pathological at once keeps the comment < {SAFE_COMMENT_BOUND} "
                   f"chars (the busiest-PR worst case) — got {len(ab)}", len(ab) < SAFE_COMMENT_BOUND))

    # S1e: when THIS PR sits PAST the shown slice of a huge land order, its OWN row must still be visible (with its
    # true position) — the customer must always be able to see where they land — AND the comment stays bounded.
    order_pos = ["PR-%d" % i for i in range(1, 2001)]
    me_far = {**cbase,
        "changes": [{"change_id": "PR-1500", "label": "PR-1500", "agent": "a", "verdict": "warn",
            "paths": ["x.py"], "impact": ["y.py"], "contested_with": ["x"], "serialize_behind": [],
            "depends_on_changing": [], "unknown_paths": []}],
        "clusters": [{"changes": order_pos, "agents": order_pos, "size": 2000, "suggested_order": order_pos}]}
    mf = R.render_pr_check(me_far, "PR-1500").get("comment") or ""
    checks.append((f"S1e: in a 2000-PR land order, THIS PR (position 1500) is still shown with its true position "
                   f"and a CLICKABLE PR link, and the comment stays < {SAFE_COMMENT_BOUND} chars — got {len(mf)}",
                   "1500. **[PR-1500](https://github.com/acme/app/pull/1500)** ← this PR" in mf
                   and len(mf) < SAFE_COMMENT_BOUND))

    # the capped/overflow text on these pathological inputs must ALSO be jargon-clean AND count-free — these are
    # the worst-case inputs (2000 foundations / deps / cluster), so their overflow trailers are exactly where a
    # "+N more file(s)" topology count would surface; the MOAT scan proves they don't leak the graph's size.
    for blob_label, blob in (("size-cluster", cb), ("size-foundation", sfb), ("size-depends", dcb),
                             ("size-all", ab), ("size-mePast", mf)):
        all_violations.extend(_scan(blob_label, blob))
        all_violations.extend(_scan_overclaim(blob_label, blob))
        all_violations.extend(_scan_moat_counts(blob_label, blob))

    # Coverage self-check: the run must actually have produced comments (the richest leak surface), else the
    # guard would be vacuously green. warn/serialize/unknown/depends/multi all emit a comment.
    checks.append(("coverage: at least one scenario rendered a non-empty comment (the richest leak surface)",
                   rendered_any_comment))
    # Coverage self-check: every verdict-driven human signal actually appeared SOMEWHERE in the customer output
    # (so all branches truly rendered). The human lead now renders in BOTH the check title and the comment body;
    # "Clear" appears in the title/summary because a clear PR gets no comment.
    blob = " ".join(human_titles_seen)
    checks.append(("coverage: clear / warn / serialize / unknown human signals all rendered",
                   "Clear" in blob and "Heads up" in blob and "Wait in line" in blob and "Not analyzed" in blob))

    # ONBOARDING CLARITY (a freshly-installed repo's FIRST PR is often 'clear' or 'no reservation yet' — and
    # those branches post NO comment, so the CHECK SUMMARY is the customer's only contact with the product).
    # Both must carry the records-not-correctness / advisory framing inline, or a first customer sees a bare
    # green check with zero idea what Veripsa is or that it is advisory. Assert the framing is present in BOTH.
    clear_scen = next(i for d, i, _ in scenarios() if d.startswith("clear"))
    empty_scen = next(i for d, i, _ in scenarios() if d.startswith("empty"))
    clear_sum = (R.render_pr_check(clear_scen, "PR-1").get("summary") or "")
    empty_sum = (R.render_pr_check(empty_scen, "PR-1").get("summary") or "")
    framing = lambda s: ("records" in s.lower() and "advisory" in s.lower()
                         and ("not assert correctness" in s.lower() or "not correctness" in s.lower()))
    checks.append(("onboarding clarity: the CLEAR check summary states Veripsa is advisory + records-not-correctness "
                   "(the only text a clear PR shows — a first customer's first contact)", framing(clear_sum)))
    checks.append(("onboarding clarity: the no-reservation (first-sync) check summary states advisory + "
                   "records-not-correctness too (also comment-free)", framing(empty_sum)))

    # ── OTHER CUSTOMER SURFACES (audit 2026-06-19): render_pr_check is not the only customer text. watching_check
    #    (install signal), quota_paused_check + quota_paused_comment_body (free-tier wall), cleared_comment_body
    #    (overlap resolved) and coverage_nudge_line (plan nudge) all post to GitHub on real events but were
    #    UNGUARDED by this gate. Scan them through the SAME jargon + overclaim denylists so they cannot drift, and
    #    assert the two MOAT invariants on the count-bearing surfaces (no raw graph/file counts ever reach a customer).
    other_surfaces: list[tuple[str, str | None]] = []
    wc = R.watching_check(files=123, edges=4567, branch="main")
    other_surfaces += [("watching_check.title", wc.get("title")), ("watching_check.summary", wc.get("summary"))]
    other_surfaces.append(("watching_check.indexing", R.watching_check(indexing=True).get("summary")))
    qp = R.quota_paused_check()
    other_surfaces += [("quota_paused_check.title", qp.get("title")), ("quota_paused_check.summary", qp.get("summary"))]
    other_surfaces.append(("quota_paused_comment_body", R.quota_paused_comment_body()))
    sg = R.stale_graph_unknown_check()
    other_surfaces += [("stale_graph_unknown_check.title", sg.get("title")),
                       ("stale_graph_unknown_check.summary", sg.get("summary"))]
    other_surfaces.append(("stale_graph_unknown_comment_body", R.stale_graph_unknown_comment_body()))
    other_surfaces.append(("cleared_comment_body", R.cleared_comment_body()))
    cov_over = R.coverage_nudge_line({"plan": "Starter", "file_limit": 250, "file_count": 900, "over_by": 650, "near": False})
    cov_near = R.coverage_nudge_line({"plan": "Starter", "file_limit": 250, "file_count": 235, "over_by": 0, "near": True})
    other_surfaces += [("coverage_nudge.over", cov_over), ("coverage_nudge.near", cov_near)]
    # stale_base_nudge_line (post-merge staleness heads-up, appended to the check summary): it NAMES the author's
    # own overlapping files but must not emit a raw file COUNT ("N files you're editing", "+N more"). Both the
    # single-file and the past-cap (many-files) shapes are scanned below + asserted count-free here (MOAT).
    stale_one = R.stale_base_nudge_line(["a.py", "z.py"], ["a.py"])
    stale_many = R.stale_base_nudge_line([f"f{i}.py" for i in range(9)], [f"f{i}.py" for i in range(9)])
    other_surfaces += [("stale_base_nudge.one", stale_one), ("stale_base_nudge.many", stale_many)]
    # MOAT (PO: 「グラフの数だとバレる」/「ユーザーに見せるのは%だけ」): the coverage nudge must show a % and NEVER the
    # raw file count/limit we fed in; the watching signal must NEVER leak the raw graph file/edge counts.
    checks.append(("MOAT: coverage nudge shows a % and never the raw file count/limit (those stay private)",
                   "%" in (cov_over or "") and "900" not in (cov_over or "")
                   and "250" not in (cov_over or "") and "650" not in (cov_over or "")))
    checks.append(("MOAT: watching_check never leaks raw graph file/edge counts (only an 'indexed' statement)",
                   "123" not in (wc.get("summary") or "") and "4567" not in (wc.get("summary") or "")))
    # MOAT (PO 2026-06-21 「file count はダメ」): the coverage nudge keeps its % but trips NO topology-count rule;
    # the stale-base nudge must carry NO raw file count in either the single-file or the past-cap shape.
    checks.append(("MOAT: coverage nudge's % is allowed and does NOT trip the topology-count rule",
                   not _scan_moat_counts("coverage_nudge.over", cov_over)))
    checks.append(("MOAT: the stale-base staleness nudge carries no raw file count (single-file shape)",
                   not _scan_moat_counts("stale_base_nudge.one", stale_one)))
    checks.append(("MOAT: the stale-base staleness nudge carries no raw file count (past-cap many-files shape)",
                   not _scan_moat_counts("stale_base_nudge.many", stale_many)))
    for label, text in other_surfaces:
        v = _scan(label, text)
        oc = _scan_overclaim(label, text)
        mc = _scan_moat_counts(label, text)   # MOAT: no raw graph/file/node/edge count on any other customer surface
        checks.append((f"other-surface: no internal jargon in {label}", not v))
        checks.append((f"other-surface: no overclaim in {label}", not oc))
        checks.append((f"other-surface: no raw graph-topology count (MOAT) in {label}", not mc))
        all_violations.extend(v)
        all_violations.extend(oc)
        all_violations.extend(mc)

    # ── PROSE-DOC OVERCLAIM LINT: the overclaim contract above guards render.py's runtime
    #    output — but the customer ALSO reads the public prose docs. Run the
    #    SAME OVERCLAIM_PHRASES denylist over them, via the negation/evidentiary-aware doc scan (so the honest
    #    "does not guarantee …" / "the backtest proves the signal" framings the docs DEPEND on are not over-
    #    blocked). The docs are scanned by repo-relative path so this stays deploy-free + offline.
    doc_paths = [
        os.path.join(ROOT, "README.md"),
        os.path.join(ROOT, "SECURITY.md"),
        os.path.join(ROOT, "SUPPORT.md"),
        os.path.join(ROOT, "docs", "PRODUCT_FACTS.md"),
    ]
    docs_scanned = 0
    for dp in doc_paths:
        if not os.path.exists(dp):
            continue   # a doc may not exist in every checkout; only lint what's present
        docs_scanned += 1
        rel = os.path.relpath(dp, ROOT)
        with open(dp, encoding="utf-8") as fh:
            dtext = fh.read()
        doc_oc = _scan_overclaim_doc(rel, dtext)
        checks.append((f"DOC-LINT: prose doc {rel} carries no overclaim (records-not-correctness; advisory)",
                       not doc_oc))
        all_violations.extend(doc_oc)
    # The lint must actually have scanned the docs (else it is vacuously green if a path/glob silently breaks).
    checks.append(("DOC-LINT coverage: all four canonical public prose documents were scanned",
                   docs_scanned == len(doc_paths)))

    # NEGATIVE CONTROL (teeth): the doc-lint must FAIL on a freshly-spliced bare overclaim — otherwise a green
    # result above proves nothing. Feed planted guarantee/efficacy strings (the exact shapes a careless edit
    # introduces) and assert each is CAUGHT, AND assert the honest, negated/evidentiary framings the real docs
    # use are NOT over-blocked (so the lint has teeth without false-positives). This is the lint's self-proof.
    teeth_bad = [
        "Veripsa guarantees correctness.",
        "Veripsa prevents bugs before they merge.",
        "This ensures every collision is caught.",
        "It is always safe to merge once Veripsa is green.",
        "Veripsa proves your code is correct.",
        "We guarantee no work is lost.",
    ]
    for bad in teeth_bad:
        checks.append((f"DOC-LINT teeth: a planted overclaim is CAUGHT — {bad!r}",
                       bool(_scan_overclaim_doc("negative-control", bad))))
    teeth_ok = [
        "Veripsa records what is heading to main; it does not assert correctness.",
        "It does **not** guarantee it detects every collision.",
        "It doesn't claim to prove your code is right.",
        "The backtest proves the signal is real; a deployed A/B is the next evidence step.",
        "Prove the signal on YOUR repo before you install anything.",
    ]
    for good in teeth_ok:
        checks.append((f"DOC-LINT no-false-positive: an honest negated/evidentiary line PASSES — {good!r}",
                       not _scan_overclaim_doc("fp-control", good)))

    # ── MOAT-COUNT TEETH (PO 2026-06-21 「file count はダメ」): the self-proof that the topology-count gate has
    #    TEETH and the RIGHT teeth. The audit named "blast radius (N downstream file(s))" specifically — assert
    #    that EXACT pre-fix string FAILS, alongside the whole class (fan-in / symbol / "+N more file/part" / churn
    #    / stale "N files" / node / edge counts). And assert the post-fix reworded copy + the deliberately-allowed
    #    surfaces (the coverage %, the operational PR/queue counts, the co-change historical count) PASS — so the
    #    rule has no false positives. Without this, a green run above could be vacuous (a broken regex passes
    #    everything). This block is the negative+positive control for the moat-count class.
    moat_teeth_bad = [
        # the EXACT pre-fix leak the iter-5 audit named (render.py ~:517) — must be caught:
        "⚠ Your change's blast radius (5 downstream file(s)) meets other in-flight work",
        # the rest of the class the fix removed:
        "Not analyzed: 3 path(s) aren't in main's graph — coupling unverified.",
        "`core/config.py` is imported by **12 other files** here AND defines **40 symbols**",
        "`core/util.py` defines **30 symbols** — one file doing many things",
        "> _(+7 more recurring-contention file(s) this PR touches.)_",
        "> _(+4 more part(s) you depend on are being changed right now — align before merging.)_",
        "Files you're editing (`a.py`, +2 more) have changed on `main`",
        ", and has changed **7 times** recently",
        "5 files you're editing changed on `main` since your branch started",
        # node / edge counts (the other moat units the rule must also catch, even if render.py never emits them today):
        "12 nodes and 34 edges are reachable downstream",
    ]
    for bad in moat_teeth_bad:
        checks.append((f"MOAT teeth: a raw graph-topology count is CAUGHT — {bad!r}",
                       bool(_scan_moat_counts("moat-negative-control", bad))))
    moat_teeth_ok = [
        # the POST-FIX reworded copy (the actual strings render.py now emits) — must PASS:
        "⚠ A wide blast radius from your change meets other in-flight work — shared with **bob**.",
        "Not analyzed: some of your changed paths aren't in main's graph — coupling unverified.",
        "`core/config.py` is imported widely across this repo AND defines many distinct pieces, and changes often",
        "> _(This PR touches further recurring-contention files as well.)_",
        "> _(Further parts you depend on are also being changed right now — align before merging.)_",
        "Files you're editing (`a.py`, `b.py`, `c.py`, and more) have changed on `main`",
        # the ALLOWED coverage % — the one quantitative moat surface that MUST keep passing:
        "📈 Veripsa is covering about **42%** of your codebase on the **Starter** plan",
        "📈 You're at about **88%** of your **Starter** plan's coverage.",
        # the ALLOWED operational / queue counts (PR identities / positions — NOT graph size): the judgment-call
        # surfaces the fix deliberately KEPT. They must NOT be over-blocked.
        "> **⏳ 3 in-flight PRs are waiting behind this one** on: `a.py` — **bob**, **carol**.",
        "**Overlapping PRs** — 4 open PRs touch this same area. Suggested order to land them:",
        "_(+12 more PR(s) further down the order.)_",
        "1. **PR-7**  2. **PR-6** ← this PR  3. **PR-8**",
        # the ALLOWED agent/PR-label overflow from _fmt_agents — a count of the OTHER PRs/authors you share a
        # neighborhood with (operational, not graph size). JUDGMENT CALL — deliberately kept (see the report).
        "Your change shares a code neighborhood with **alice**, **bob** (+1992 more) — a structural dependency",
        # historical co-change shape (no longer emitted as of 2026-06-25 — the literal "N% of the time" was dropped
        # under the honest-copy rule because it saturated at strong couplings as "100%"; render.py now emits just
        # "(3.0× more than chance)"). KEPT here as a MOAT no-false-positive control: a % + a lift multiplier are
        # NOT graph-topology counts, so even this hypothetical string would PASS the moat scanner. Belt + suspenders.
        "**42%** of the time (3.0× more than chance).",
    ]
    for good in moat_teeth_ok:
        checks.append((f"MOAT no-false-positive: an allowed % / queue / reworded line PASSES — {good!r}",
                       not _scan_moat_counts("moat-fp-control", good)))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)

    # ANY collected leak fails the gate even if it has no paired check line (e.g. the truncation-disclosure scan):
    # a violation in all_violations is a customer-surface leak, full stop.
    ok = ok and not all_violations

    if all_violations:
        print("\n-- LEAKS FOUND (current render.py is NOT clean — do NOT edit render.py; report these) --")
        for v in all_violations:
            print("   * " + v)

    print("NO-JARGON-LEAK GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
