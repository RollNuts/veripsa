#!/usr/bin/env python3
"""Veripsa — the COORDINATION BRIEF serializer (pure, stateless, content-free, read-only).

WHAT THIS IS: a machine-readable form of the SAME pre-merge coordination judgment Veripsa already renders as a
GitHub check + PR comment (github-app/render.py). Where render.py turns core.main_impact_surface(repo, branch)
into human markdown, this turns the SAME surface (the SAME `me` change dict render.py reads) into a stable,
versioned JSON `Coordination Brief` an in-flight AI agent can read before it merges — WITHOUT scraping prose.

DESIGN RULES (all enforced by tests/test_coordination_brief.py + docs/design/COORDINATION_BRIEF.schema.json):
  * SERIALIZE, don't recompute. Every field is a projection of what render.py / apply_pause_ack already have.
    The verdict, the effective (per-path) verdict, the material-coupling test, the coupling snapshot, the
    conflict-marker findings, the new-vs-gap path split — all come from render.py so the Brief can NEVER drift
    from the check/comment. NO new engine computation lives here.
  * CONTENT-FREE. Paths, symbol names, line numbers, in-flight PR refs, and a 12-hex coupling hash ONLY. Never a
    source body, a diff body, a secret, or a RAW GRAPH-SIZE COUNT (fan-in, symbol count, downstream-file count,
    threshold) — those are the moat. Breadth is qualitative ("wide", "and more"), never a number. In-flight PR
    refs/positions ARE customer-facing (the comment numbers them) and are allowed.
  * BOUNDED. Every string field that classifies is a fixed enum (STATES / RELATIONSHIP_KINDS / EVIDENCE_KINDS /
    RECOMMENDED_ACTIONS / CONFIDENCE / ACK_STATES). The schema pins the same enums.
  * PINNED + DETERMINISTIC. The Brief carries the exact acting_head_sha (+ base_sha when the caller supplies it).
    Same inputs → byte-identical Brief: nothing here reads a clock, a random, or the network. `evaluated_at` is a
    CALLER input, never datetime.now(), precisely so purity/determinism hold.
  * PROVENANCE-PINNED (persistent history, disposable context). The point of pinning is STALENESS DETECTION: a
    Brief must be tied to the exact state it was computed against so a consumer can tell when it is out of date.
    Beyond the acting head, the Brief pins EACH counterpart it was evaluated against to that partner's head SHA
    (evaluated_against[].head_sha → a moved partner is detectable), the code-graph snapshot the verdict ran on
    (freshness.graph_source_sha + repository.generation), and the detector/policy revision (freshness.policy_version
    — distinct from schema_version, which versions the Brief SHAPE, not the engine tuning). All of these are
    content-free caller inputs (SHAs / generations / version tokens), optional, and never a body or a count.
  * CONSISTENT WITH THE VERDICT CONTRACT. state ∈ {clear, heads_up, wait_in_line, unknown} maps 1:1 from the
    engine verdict ladder. Paused is NOT a fifth state: it is the ack OVERLAY (the `ack` block + per-relationship
    requires_human_ack + top-level action_required), exactly as apply_pause_ack overlays action_required on the
    check.
  * READ-ONLY. This module produces a value. It performs NO write, NO GitHub call, NO DB call, NO agent action,
    NO merge. Wiring it to a transport is a separate, deliberate step (see the design doc's safe-read-path
    section) — and v1 rides the EXISTING tenant-isolated GitHub check/comment delivery channel, adding no new
    authenticated read API.

WIRING POINT (not done here — design-only): the webhook poster that already calls render.render_pr_check +
render.apply_pause_ack for a PR event has, in hand, the same `impact`, `change_ref`, `is_fork`, `truncated`,
`added_paths`, `conflict_markers`, cochange rows, the ack label state, and the event's head/base SHAs. It can
call `coordination_brief(...)` with those and attach the JSON to the check run's structured output (the same
tenant-isolated surface GitHub already gates by repo read access). No new Veripsa-hosted API is introduced.
"""
from __future__ import annotations

import json
import re
import sys

# Import the EXISTING verdict/ack/marker logic so the Brief stays in lock-step with the check/comment. render.py
# lives in this same directory (github-app is not an importable package — hyphen), so a bare import works when the
# dir is on sys.path (the App and the tests both do that), with a package fallback.
try:
    from render import (
        _VERDICT_CONCLUSION, _WIDE_BLAST_TIER, LIST_LINE_CAP,
        _effective_verdict, _conflict_marker_findings, _split_unknown_paths,
        _label_ref_map, _LAND_REF_RE, _dicts, _int,
        is_material_coupling, coupling_snapshot, _ack_partner_refs, apply_pause_ack, ACK_LABEL,
    )
except ImportError:  # imported as a package
    from .render import (  # type: ignore
        _VERDICT_CONCLUSION, _WIDE_BLAST_TIER, LIST_LINE_CAP,
        _effective_verdict, _conflict_marker_findings, _split_unknown_paths,
        _label_ref_map, _LAND_REF_RE, _dicts, _int,
        is_material_coupling, coupling_snapshot, _ack_partner_refs, apply_pause_ack, ACK_LABEL,
    )

SCHEMA_VERSION = "1"
KIND = "veripsa.coordination_brief"

# ── the bounded enums (the schema pins the identical sets; the test asserts parity) ──────────────────────────
STATES = ("clear", "heads_up", "wait_in_line", "unknown")
RELATIONSHIP_KINDS = (
    "conflict_marker", "direct_collision", "likely_rebase_conflict", "holding_lane",
    "depends_on_changing_contract", "semantic_coupling", "suppressed_coupling", "blast_radius",
    "landing_order", "recurring_contention", "co_change", "main_moved",
    "unverified_new_path", "unverified_gap_path", "truncated_analysis",
)
EVIDENCE_KINDS = ("structural", "historical", "textual", "unverified")
RECOMMENDED_ACTIONS = (
    "land_in_order", "align_before_merge", "coordinate_before_merge", "rebase_before_merge",
    "resolve_conflict_marker", "free_the_lane", "consider_split", "investigate", "monitor",
)
CONFIDENCE = ("certain", "corroborated", "supported", "likely", "unverified")
ACK_STATES = ("not_material", "paused", "acknowledged", "stale_reack", "ack_verification_pending")
# Freshness lifecycle — the explicit 4-value replacement for the implicit head_matches boolean. current=head still
# matches; stale=head moved under the analysis (recompute); unknown=the caller could not determine it; unavailable=
# freshness could not be established at all (no current-head read happened). "unavailable" had NO representation in
# the head_matches boolean; naming it is the point. head_matches is kept alongside as the raw signal it derives from.
FRESHNESS_STATUS = ("current", "stale", "unknown", "unavailable")

# Map the engine verdict → the Brief's base state. serialize_soft (the softened append/idle-branch overlap) is a
# heads-up, not a wait; unknown-family stays unknown. Paused is NOT here (it is the ack overlay).
_VERDICT_STATE = {
    "clear": "clear", "warn": "heads_up", "serialize": "wait_in_line",
    "serialize_soft": "heads_up", "unknown": "unknown",
}
# Deterministic ordering weight per relationship kind (hard-fail first, then contention, then advisory/unverified).
_KIND_ORDER = {k: i for i, k in enumerate(RELATIONSHIP_KINDS)}


def _paths(value, cap: int = LIST_LINE_CAP) -> tuple[list, bool]:
    """A content-free path list, capped, with a boolean 'more' — never a remaining COUNT (moat). Drops non-str /
    blank / duplicate entries while preserving the engine's deterministic order."""
    seen: set = set()
    out: list = []
    for p in (value or []):
        if isinstance(p, str) and p and p not in seen:
            seen.add(p)
            out.append(p)
    return out[:cap], len(out) > cap


def _ref_of(label, ref_map: dict):
    """A content-free change ref (PR-<n>/BR-<x>) for a humanized partner label. Prefer a ref token embedded in the
    label ('alice PR-12' → 'PR-12'); else the engine's label→change_id map (an author-only BR- push); else the
    whitespace-normalized label itself so a partner is NEVER dropped (mirrors render._ack_partner_refs)."""
    if not isinstance(label, str) or not label.strip():
        return None
    m = _LAND_REF_RE.search(label)
    if m:
        return m.group(0)
    rid = ref_map.get(label)
    if isinstance(rid, str) and rid:
        return rid
    return " ".join(label.split())


def _locus(*, path=None, symbol=None, line=None, line_lo=None, line_hi=None):
    """A content-free finer-point object (path/symbol/line numbers, never code), or None when nothing finer than
    the file is known (the caller then keeps subject/related_paths)."""
    o = {}
    if isinstance(path, str) and path:
        o["path"] = path
    if isinstance(symbol, str) and symbol:
        o["symbol"] = symbol
    for k, v in (("line", line), ("line_lo", line_lo), ("line_hi", line_hi)):
        if isinstance(v, int) and v > 0:
            o[k] = v
    return o or None


def _rel(kind, *, related_change=None, evidence_kind="structural", recommended_action="monitor",
         requires_human_ack=False, confidence="supported", subject=None, locus=None,
         related_paths=None, more_related_paths=None, reach=None, basis=None,
         ordered_changes=None, acting_position=None) -> dict:
    """Assemble one relationship dict, omitting empty optional fields so the Brief stays minimal + schema-valid."""
    r = {
        "relationship_kind": kind,
        "related_change": related_change,
        "evidence_kind": evidence_kind,
        "recommended_action": recommended_action,
        "requires_human_ack": bool(requires_human_ack),
        "confidence": confidence,
    }
    if subject is not None:
        r["subject"] = subject
    if locus:
        r["locus"] = locus
    if related_paths is not None:
        r["related_paths"] = related_paths
        r["more_related_paths"] = bool(more_related_paths)
    if reach is not None:
        r["reach"] = reach
    if basis is not None:
        r["basis"] = basis
    if ordered_changes is not None:
        r["ordered_changes"] = ordered_changes
        r["acting_position"] = acting_position
    return r


def _sort_key(r: dict):
    return (_KIND_ORDER.get(r.get("relationship_kind"), 99),
            str(r.get("related_change") or ""), str(r.get("subject") or ""))


def coordination_brief(impact: dict, change_ref: str, *,
                       base_sha: str | None = None,
                       acting_head_sha: str | None = None,
                       head_matches: bool | None = None,
                       freshness_status: str | None = None,
                       graph_source_sha: str | None = None,
                       repository_generation: int | None = None,
                       policy_version: str | None = None,
                       evaluated_at: str | None = None,
                       truncated: bool = False,
                       is_fork: bool = False,
                       cochange=None,
                       added_paths: list | None = None,
                       conflict_markers: list | None = None,
                       ack: dict | None = None) -> dict:
    """Serialize core.main_impact_surface(repo, branch) into a Coordination Brief for ONE acting change.

    `impact`         : the SAME JSON render.render_pr_check reads (main_impact_surface). A non-dict → honest-empty.
    `change_ref`     : the acting change identity — PR-<n> / BR-<x> (the same ref render_pr_check takes).
    `base_sha`       : the base-branch tip SHA the caller pins the analysis to (event base.sha). None → base is
                       not represented as a single SHA (the engine tracks base per-path — see the design doc).
    `acting_head_sha`: the analyzed head SHA. Defaults to the surface's per-change head_sha when not passed.
    `head_matches`   : whether acting_head_sha still equals the change's CURRENT head at delivery (caller-supplied
                       — the webhook has both; None when unknown). Kept out of any recompute → deterministic.
    `freshness_status`: an explicit freshness verdict ∈ {current, stale, unknown, unavailable}. When the caller does
                       NOT supply it, it is DERIVED from head_matches (True→current, False→stale, None→unknown);
                       "unavailable" (no current-head read at all) has no head_matches encoding so it must be passed.
    `graph_source_sha`: the code-graph snapshot/generation SHA the verdict was computed against (freshness field).
                       None → not represented. Content-free; lets a consumer detect a regenerated graph.
    `repository_generation`: the repository/code-graph GENERATION (lifecycle ordinal) the verdict ran under, when
                       the caller supplies it. None → repository.generation is null. Content-free (never a count).
    `policy_version` : the detector/policy revision token the verdict was produced by — distinct from schema_version
                       (which versions the Brief SHAPE, not the engine tuning). None → not represented.
    `evaluated_at`   : an ISO timestamp the delivery path stamps. NEVER defaulted to now() (determinism/purity).
    `truncated`, `is_fork`, `cochange`, `added_paths`, `conflict_markers` : the SAME caller-supplied context
                       render_pr_check takes, so the Brief and the check/comment are computed from one input set.
    `ack`            : {label_present, prior_hash, prior_confirmed} — the GitHub-side ack signal the webhook
                       already reads for apply_pause_ack. Used verbatim to derive the ack overlay (no new logic).

    Returns a Brief dict (see docs/design/COORDINATION_BRIEF.schema.json). Read-only: no write/GitHub/DB/network.
    """
    impact = impact if isinstance(impact, dict) else {}
    changes = _dicts(impact.get("changes"))
    me = next((c for c in changes if c.get("change_id") == change_ref), None)
    if me is None:
        me = next((c for c in changes if c.get("agent") == change_ref or c.get("label") == change_ref), None)
    repo = impact.get("repo", "") or ""
    branch = impact.get("branch", "main") or "main"
    ref_map = _label_ref_map(changes)

    # normalize the same caller inputs render_pr_check normalizes
    if not isinstance(added_paths, (list, tuple)):
        added_paths = []
    else:
        added_paths = [p for p in added_paths if isinstance(p, str) and p]
    conflict_findings = _conflict_marker_findings(conflict_markers)
    ack = ack if isinstance(ack, dict) else {}
    label_present = bool(ack.get("label_present"))
    prior_hash = ack.get("prior_hash")
    prior_confirmed = ack.get("prior_confirmed", True)

    # ── the no-reservation case: the App saw the change but Veripsa has no reservation yet (first sync). ────────
    if me is None:
        brief = _skeleton(repo, branch, change_ref, acting_head_sha or "", base_sha,
                          state="unknown" if conflict_findings else "clear",
                          truncated=truncated, head_matches=head_matches, evaluated_at=evaluated_at,
                          freshness_status=freshness_status, graph_source_sha=graph_source_sha,
                          repository_generation=repository_generation, policy_version=policy_version)
        rels = [_conflict_rel(f) for f in conflict_findings]
        brief["relationships"] = sorted(rels, key=_sort_key)
        brief["action_required"] = bool(conflict_findings)
        brief["ack"] = _ack_block(None, change_ref, branch, is_fork, False, None, True)
        return brief

    # ── the effective (per-path) verdict — the EXACT transform render_pr_check applies ─────────────────────────
    raw_verdict = me.get("verdict")
    verdict0 = raw_verdict if isinstance(raw_verdict, str) and raw_verdict in _VERDICT_CONCLUSION else "unknown"
    paths = me.get("paths", []) or []
    unknown_paths = me.get("unknown_paths", []) or []
    dampened_with = _dicts(me.get("dampened_with"))
    verifiable = [p for p in paths if p not in set(unknown_paths)]
    verdict = _effective_verdict(verdict0, unknown_paths, dampened_with, verifiable, added_paths, truncated,
                                 engine_unknown=raw_verdict == "unknown")
    split_from_verdict = verdict != "unknown" and bool(unknown_paths)
    state = _VERDICT_STATE.get(verdict, "unknown")

    head = acting_head_sha or (me.get("head_sha") if isinstance(me.get("head_sha"), str) else "") or ""
    brief = _skeleton(repo, branch, change_ref, head, base_sha, state=state,
                      truncated=truncated, head_matches=head_matches, evaluated_at=evaluated_at,
                      freshness_status=freshness_status, graph_source_sha=graph_source_sha,
                      repository_generation=repository_generation, policy_version=policy_version)

    # ── ack overlay (derive via the REAL state machine so the Brief == the check) ──────────────────────────────
    ack_block = _ack_block(impact, change_ref, branch, is_fork, label_present, prior_hash, prior_confirmed)
    brief["ack"] = ack_block
    material = ack_block["required"]
    ack_partner_refs = set(_ack_partner_refs(me)) if material else set()

    # ── FORK REDACTION: a fork PR's Brief must not name the base repo's other in-flight PRs / paths. Keep only ──
    #    the fork's OWN conflict markers (the contributor's added code) + the state. Mirrors render_pr_check.
    if is_fork:
        brief["redacted"] = True
        brief["relationships"] = sorted([_conflict_rel(f) for f in conflict_findings], key=_sort_key)
        brief["action_required"] = bool(conflict_findings)
        return brief

    rels: list = []

    # 1. conflict markers — the ONE hard-fail (build-breaker), content-free path+line
    rels += [_conflict_rel(f) for f in conflict_findings]

    # 2. direct collision (serialize): holders THIS change waits behind, at their finest locus
    collision_points = _dicts(me.get("collision_points"))
    behind = me.get("serialize_behind", []) or []
    if verdict == "serialize":
        seen_dc: set = set()
        for cp in collision_points:
            rc = _ref_of(cp.get("behind"), ref_map)
            seen_dc.add(rc)
            rels.append(_rel(
                "direct_collision", related_change=rc, evidence_kind="structural",
                recommended_action="land_in_order",
                requires_human_ack=material and rc in ack_partner_refs,
                confidence="supported",
                subject=cp.get("symbol") or cp.get("path"),
                locus=_locus(path=cp.get("path"), symbol=cp.get("symbol"),
                             line_lo=cp.get("line_lo"), line_hi=cp.get("line_hi")),
            ))
        for lbl in behind:   # any holder without a finer collision_point row (stale graph → file-level only)
            rc = _ref_of(lbl, ref_map)
            if rc not in seen_dc:
                rels.append(_rel("direct_collision", related_change=rc, evidence_kind="structural",
                                 recommended_action="land_in_order",
                                 requires_human_ack=material and rc in ack_partner_refs, confidence="supported"))

    # 3. likely rebase conflict (mechanical line geometry) — only on a direct/soft collision
    if bool(me.get("merge_conflict_likely")) and verdict in ("serialize", "serialize_soft"):
        for cp in _dicts(me.get("conflict_points")):
            rels.append(_rel("likely_rebase_conflict", related_change=_ref_of(cp.get("behind"), ref_map),
                             evidence_kind="textual", recommended_action="rebase_before_merge",
                             confidence="likely", subject=cp.get("path"),
                             locus=_locus(path=cp.get("path"), line=cp.get("line"))))

    # 4. holding lane (the inverse: others queued behind THIS change) — appears even on a clear holder
    queued = me.get("queued_behind", []) or []
    q_paths, q_more = _paths(me.get("queued_behind_paths", []) or [])
    for lbl in queued:
        rels.append(_rel("holding_lane", related_change=_ref_of(lbl, ref_map), evidence_kind="structural",
                         recommended_action="free_the_lane", confidence="supported",
                         related_paths=q_paths, more_related_paths=q_more))

    # 5. upstream dependency being changed under this PR (the headline warn)
    for d in _dicts(me.get("depends_on_changing")):
        rels.append(_rel("depends_on_changing_contract", related_change=_ref_of(d.get("by"), ref_map),
                         evidence_kind="structural", recommended_action="align_before_merge",
                         confidence="supported", subject=d.get("path"), locus=_locus(path=d.get("path"))))

    # 6. semantic coupling (warn): shared code neighbourhood with an in-flight counterpart (mechanism NOT named)
    if verdict == "warn":
        for lbl in (me.get("contested_with", []) or []):
            rc = _ref_of(lbl, ref_map)
            rels.append(_rel("semantic_coupling", related_change=rc, evidence_kind="structural",
                             recommended_action="coordinate_before_merge",
                             requires_human_ack=material and rc in ack_partner_refs, confidence="supported"))
        # co-change-corroborated dampened coupling → a warn where TWO signals agree
        for d in dampened_with:
            if d.get("corroborated"):
                rc = _ref_of(d.get("by"), ref_map)
                rels.append(_rel("semantic_coupling", related_change=rc, evidence_kind="structural",
                                 recommended_action="coordinate_before_merge",
                                 requires_human_ack=material and rc in ack_partner_refs, confidence="corroborated",
                                 subject=_hub(d.get("via_hub"))))

    # 7. suppressed (hub-dampened, uncorroborated) coupling → surfaced as unverified so the silence is visible
    for d in dampened_with:
        if not d.get("corroborated"):
            rels.append(_rel("suppressed_coupling", related_change=_ref_of(d.get("by"), ref_map),
                             evidence_kind="unverified", recommended_action="coordinate_before_merge",
                             confidence="unverified", subject=_hub(d.get("via_hub"))))

    # 8. blast radius (downstream reach) — qualitative reach word, named paths, NEVER a count
    impact_paths = me.get("impact", []) or []
    if impact_paths:
        bp, bmore = _paths(impact_paths)
        reach = "wide" if len(impact_paths) > _WIDE_BLAST_TIER else "local"
        rels.append(_rel("blast_radius", related_change=None, evidence_kind="structural",
                         recommended_action="monitor", confidence="supported",
                         related_paths=bp, more_related_paths=bmore, reach=reach))

    # 9. recurring contention (shared foundation / split advice) — qualitative basis, NEVER fan-in/symbol counts
    for sf in _dicts(me.get("shared_foundation")):
        basis = sf.get("basis")
        basis = basis if basis in ("foundation", "god_file", "both") else "foundation"
        rels.append(_rel("recurring_contention", related_change=None, evidence_kind="structural",
                         recommended_action="consider_split", confidence="supported",
                         subject=sf.get("path"), basis=basis))

    # 10. landing order (cluster suggested order) — deduped like the renderer; refs+positions are customer-facing
    cluster = next((cl for cl in _dicts(impact.get("clusters"))
                    if change_ref in (cl.get("changes", []) or [])
                    or me.get("label") in (cl.get("agents", []) or [])), None)
    if cluster and _int(cluster.get("size")) >= 2:
        ordered, acting_pos = _landing_order(cluster.get("suggested_order", []) or [], ref_map,
                                             change_ref, me.get("label"))
        if ordered:
            rels.append(_rel("landing_order", related_change=None, evidence_kind="structural",
                             recommended_action="land_in_order", confidence="supported",
                             ordered_changes=ordered, acting_position=acting_pos))

    # 11. co-change (empirical / historical hint) — a partner FILE, not a PR; qualitative strength only (no lift #)
    for x in _dicts(cochange):
        if x.get("edited") and x.get("partner") and isinstance(x.get("prob"), (int, float)) \
                and float(x.get("prob")) > 0:
            rels.append(_rel("co_change", related_change=None, evidence_kind="historical",
                             recommended_action="monitor", confidence="likely",
                             subject=x.get("edited"), related_paths=[x.get("partner")], more_related_paths=False))

    # 12. main moved under the PR (stale base) is delivered by coordination_brief_for_event (it has the two path
    #     lists the poster intersects); the pure core takes no path-diff input, so it is added by the wrapper.

    # 13. unverified paths (new-in-PR vs extractor gap) — only when the verifiable part earned its own verdict OR
    #     the whole PR is unknown (both cases disclose honestly; never 'clear' over an unread path)
    if unknown_paths and (split_from_verdict or verdict == "unknown"):
        new_in_pr, gap = _split_unknown_paths(unknown_paths, added_paths)
        if new_in_pr:
            np, nmore = _paths(new_in_pr)
            rels.append(_rel("unverified_new_path", related_change=None, evidence_kind="unverified",
                             recommended_action="monitor", confidence="unverified",
                             related_paths=np, more_related_paths=nmore))
        if gap:
            gp, gmore = _paths(gap)
            rels.append(_rel("unverified_gap_path", related_change=None, evidence_kind="unverified",
                             recommended_action="investigate", confidence="unverified",
                             related_paths=gp, more_related_paths=gmore))

    # 14. truncated analysis (an unparsed remainder)
    if truncated:
        rels.append(_rel("truncated_analysis", related_change=None, evidence_kind="unverified",
                         recommended_action="investigate", confidence="unverified"))

    brief["relationships"] = sorted(rels, key=_sort_key)
    # PROVENANCE: pin every counterpart the relationships name to the exact head SHA the analysis saw (staleness).
    brief["evaluated_against"] = _evaluated_against(brief["relationships"], changes, ref_map,
                                                    change_ref, me.get("label"))
    # the ONLY merge-gating conditions: an unresolved marker, or a material coupling that is un-acked/stale.
    brief["action_required"] = bool(conflict_findings) or ack_block["state"] in ("paused", "stale_reack")
    return brief


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────────────
def _derive_freshness_status(head_matches) -> str:
    """The canonical freshness status. Honour an explicit caller value; otherwise DERIVE it from head_matches so the
    two never disagree. head_matches has no encoding for 'unavailable', so that only appears when passed explicitly."""
    if head_matches is True:
        return "current"
    if head_matches is False:
        return "stale"
    return "unknown"


def _generation(value):
    """A content-free lifecycle generation ordinal, or None. bool is rejected (isinstance(True, int) is True)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _skeleton(repo, branch, change_ref, head, base_sha, *, state, truncated, head_matches, evaluated_at,
              freshness_status=None, graph_source_sha=None, repository_generation=None, policy_version=None) -> dict:
    status = freshness_status if freshness_status in FRESHNESS_STATUS else _derive_freshness_status(head_matches)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "repository": {"slug": repo, "generation": _generation(repository_generation)},
        "base_branch": branch,
        "acting_change": change_ref,
        "acting_head_sha": head,
        "base_sha": base_sha if isinstance(base_sha, str) and base_sha else None,
        "state": state,
        "action_required": False,
        "enforcement": "advisory",
        "relationships": [],
        "evaluated_against": [],
        "ack": {"label": ACK_LABEL, "required": False, "present": False,
                "state": "not_material", "coupling_snapshot": "", "label_action": None},
        "freshness": {
            "evaluated_at": evaluated_at if isinstance(evaluated_at, str) and evaluated_at else None,
            "head_matches": head_matches if isinstance(head_matches, bool) else None,
            "analysis_truncated": bool(truncated),
            "status": status,
            "graph_source_sha": graph_source_sha if isinstance(graph_source_sha, str) and graph_source_sha else None,
            "policy_version": policy_version if isinstance(policy_version, str) and policy_version else None,
        },
    }


def _evaluated_against(rels, changes, ref_map, change_ref, my_label) -> list:
    """Pin every OTHER in-flight change these relationships name to the exact head SHA the analysis saw, so a
    consumer can detect a moved partner (its current head no longer equals head_sha here → recompute). Content-free:
    a ref, a PR number, a head SHA, and the counterpart's own coordination state — never a body or a count. The
    acting change itself is excluded (it is pinned by acting_head_sha). Deterministically ordered by ref."""
    self_refs = {change_ref}
    my_ref = _ref_of(my_label, ref_map) if my_label else None
    if my_ref:
        self_refs.add(my_ref)
    by_ref: dict = {}
    for c in changes:
        cid = c.get("change_id")
        if isinstance(cid, str) and cid:
            by_ref.setdefault(cid, c)
        lref = _ref_of(c.get("label"), ref_map)
        if isinstance(lref, str) and lref:
            by_ref.setdefault(lref, c)
    seen: set = set()
    refs: list = []
    for r in rels:
        for ref in [r.get("related_change"), *(r.get("ordered_changes") or [])]:
            if isinstance(ref, str) and ref and ref not in self_refs and ref not in seen:
                seen.add(ref)
                refs.append(ref)
    out: list = []
    for ref in sorted(refs):
        c = by_ref.get(ref)
        head = c.get("head_sha") if isinstance(c, dict) and isinstance(c.get("head_sha"), str) and c.get("head_sha") \
            else None
        raw_v = c.get("verdict") if isinstance(c, dict) else None
        state = _VERDICT_STATE.get(raw_v, "unknown") if isinstance(raw_v, str) else "unknown"
        m = re.match(r"^PR-([0-9]+)$", ref)
        out.append({"ref": ref, "number": int(m.group(1)) if m else None, "head_sha": head, "state": state})
    return out


def _conflict_rel(f: dict) -> dict:
    return _rel("conflict_marker", related_change=None, evidence_kind="textual",
                recommended_action="resolve_conflict_marker", confidence="certain",
                subject=f.get("path"), locus=_locus(path=f.get("path"), line=f.get("first_line")))


def _hub(value):
    """A customer-safe hub label (drop synthetic placeholders like BODY), else None — mirrors render._display_hub_label."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or v.upper() in ("BODY", "DIFF_BODY", "SOURCE_BODY"):
        return None
    return v


def _landing_order(order, ref_map, change_ref, my_label) -> tuple[list, int | None]:
    """Dedup the engine's per-path suggested_order to one ref per distinct change (first-occurrence wins,
    foundational order preserved), and find the acting change's 1-based position. Content-free (refs + an ordinal;
    the comment already numbers these) — mirrors render._render_land_order's dedup."""
    seen: set = set()
    out: list = []
    for entry in order:
        if not isinstance(entry, str):
            continue
        m = _LAND_REF_RE.search(entry)
        key = m.group(0) if m else entry
        if key in seen:
            continue
        seen.add(key)
        out.append(_ref_of(entry, ref_map))
    # the acting change's 1-based position: match the deduped refs against its ref / label / mapped label.
    my_ref = ref_map.get(my_label) if my_label else None
    pos = next((i for i, ref in enumerate(out, 1)
                if ref in (change_ref, my_label, my_ref) and ref is not None), None)
    return out, pos


def _ack_block(impact, change_ref, branch, is_fork, label_present, prior_hash, prior_confirmed) -> dict:
    """Derive the ack overlay via the REAL apply_pause_ack state machine, so the Brief's ack == the check's ack.
    We feed a throwaway rendered dict and read only the three overlay outputs (snapshot / ack_state / label_action)
    — no comment/markdown is produced or kept."""
    if not isinstance(impact, dict):
        return {"label": ACK_LABEL, "required": False, "present": bool(label_present),
                "state": "not_material", "coupling_snapshot": "", "label_action": None}
    ov = apply_pause_ack({"comment": None, "conclusion": "neutral", "title": "", "summary": ""},
                         impact, change_ref, label_present=bool(label_present), prior_hash=prior_hash,
                         branch=branch, is_fork=is_fork, prior_confirmed=prior_confirmed)
    changes = _dicts(impact.get("changes"))
    me = next((c for c in changes if c.get("change_id") == change_ref), None)
    return {
        "label": ACK_LABEL,
        "required": bool(is_material_coupling(me)) and not is_fork,
        "present": bool(label_present),
        "state": ov.get("ack_state", "not_material"),
        "coupling_snapshot": ov.get("snapshot", "") or "",
        "label_action": ov.get("label_action"),
    }


def coordination_brief_for_event(impact, change_ref, *, main_moved_paths=None, **kw) -> dict:
    """Thin convenience wrapper that adds the ONE relationship the pure core can't get from `impact`: main_moved
    (files that already landed on the base since the acting change branched — the poster intersects
    github_rest.compare_changed_paths with the PR's own changed files, exactly as render.stale_base_nudge_line
    does). Everything else is the pure `coordination_brief`. Kept separate so the core stays a pure surface
    projection and this stays the delivery-context adapter (the wiring seam)."""
    brief = coordination_brief(impact, change_ref, **kw)
    mp, more = _paths(main_moved_paths or [])
    if mp and not brief.get("redacted"):
        brief["relationships"].append(_rel("main_moved", related_change=None, evidence_kind="textual",
                                           recommended_action="rebase_before_merge", confidence="likely",
                                           related_paths=mp, more_related_paths=more))
        brief["relationships"].sort(key=_sort_key)
    return brief


def _main(argv: list[str]) -> int:
    """CLI: feed main_impact_surface JSON on stdin, name the change ref → print the Brief JSON.
        psql ... -tAc "SELECT core.main_impact_surface('repo','main')" | python3 coordination_brief.py PR-18
    """
    if len(argv) < 2:
        print("usage: coordination_brief.py <PR-n|BR-x>   (main_impact_surface JSON on stdin)", file=sys.stderr)
        return 2
    impact = json.load(sys.stdin)
    print(json.dumps(coordination_brief(impact, argv[1]), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
