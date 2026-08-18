#!/usr/bin/env python3
"""Veripsa GitHub App — the 一時停止 (PAUSE-and-ACKNOWLEDGE) tier (content-free, pure, stateless).

Split OUT of render.py on purpose: the pause/ACK STATE MACHINE (is this a material coupling? acknowledged?
stale? what hash is the ack bound to?) and the verdict COPY (render_pr_check) are independent concerns that
used to share one file and collide on nearly every PR — the hotspot lesson (finer files = finer collision
point), the same reason render_safe.py / render_bound.py were carved out. render.py re-exports these names,
so `render.apply_pause_ack` / `render.ACK_LABEL` etc. are unchanged for callers and tests.

一時停止 (PAUSE-and-ACKNOWLEDGE) tier — the conclusion that actually changes behavior.

WHY (proven, PO): the default advisory conclusion is `neutral` for every non-clear verdict ("never block").
We proved a `neutral` advisory changes NO agent behavior: a careless agent ignores "wait in line" and merges;
a careful agent re-derives the collision itself and never needed the comment. A signal that changes no behavior
has no value. So for a MATERIAL coupling — a REAL in-flight cross-PR collision — the check is NOT green until
someone EXPLICITLY acknowledges THIS specific coupling. This is NOT a blunt block: you can always proceed by
acknowledging (the `veripsa-ack` label) — it forces a conscious, RECORDED stop-and-engage moment, then clears.

STATELESS: the ack state lives entirely in GitHub — the presence of the `veripsa-ack` LABEL + the content-free
coupling-snapshot HASH embedded in Veripsa's OWN prior comment. NO DB table, NO column, NO DDL. An ack is bound
to the snapshot it was given for: if the coupling MATERIALLY CHANGES (a different partner / different paths →
a different hash) the ack is STALE and the pause re-raises (and the label is slated for removal so it reads
un-acked). CONTENT-FREE: the hash is over identifiers Veripsa ALREADY surfaces (the other in-flight PR refs +
the colliding paths/symbols) — NEVER file contents.
"""
from __future__ import annotations

import hashlib
import re  # noqa: F401  (used by the compiled ACK regexes below)

# Defensive engine-JSON coercion + the customer-surface code-span formatter live in render_safe.py;
# the final comment-body size cap lives in render_bound.py. All three are LEAF helpers (no render import),
# so this module stays a leaf too: render_safe / render_bound <- render_pauseack <- render (no cycle).
try:
    from render_safe import _dicts, _code
except ImportError:  # imported as a package
    from .render_safe import _dicts, _code
try:
    from render_bound import _cap_comment_body
except ImportError:  # imported as a package
    from .render_bound import _cap_comment_body


ACK_LABEL = "veripsa-ack"
_ACK_SNAP_PREFIX = "veripsa-ack-snap:"
# The HTML marker embedded in the posted comment that BINDS an ack to a coupling. An HTML comment is invisible in
# the rendered markdown (no customer-visible noise) but readable back by the webhook handler off the prior comment.
_ACK_SNAP_RE = re.compile(r"<!--\s*" + re.escape(_ACK_SNAP_PREFIX) + r"([0-9a-f]{12})\s*-->")
# the bare content-free CHANGE REFS ('PR-12' / 'BR-feature-x') pulled out of a (possibly humanized "alice PR-12")
# partner label — the SAME token shape webhook._behind_refs extracts. A change ref is a number / branch slug,
# never code (content-free).
_ACK_REF_RE = re.compile(r"\b(?:PR-\d+|BR-[A-Za-z0-9_./\-]+)")


def _ack_partner_refs(me: dict) -> list[str]:
    """The OTHER in-flight work THIS change collides with — as a STABLE, content-free partner-identity set (sorted,
    deduped). A material coupling is a collision WITH someone, so the partner set is part of the coupling's
    identity. Reads the SAME partner fields the comment already names: serialize_behind / queued_behind (the
    direct-collision lane counterparts) and contested_with (the warn semantic-exposure counterpart). Each entry may
    be a bare ref, a humanized label ('alice PR-12' / a BR- branch push rendered author-name-only as 'alice'), or a
    dict row ({change_id|agent|label}).

    STABILITY: an ack binds to THIS hash, so the hash must
    be STABLE across re-renders of the SAME coupling or a valid ack reads stale and the pause re-raises (the label
    gets stripped) for nothing. The OLD code pulled ONLY 'PR-<n>'/'BR-<x>' tokens out — but the humanized label of a
    NON-PR change (a BR- branch push that has not yet reconciled to a PR) is the AGENT NAME ALONE (e.g. 'example-user'),
    which carries NO ref token, so such a partner was SILENTLY DROPPED from the identity entirely. That made the
    partner set INCOMPLETE and UNSTABLE: the same coupling hashed differently depending on whether a partner was
    showing as a BR- (no token → dropped) or had reconciled to a PR- (token → counted). So: take the ref token when
    one is present (the most precise stable id), ELSE fall back to the whole-space-normalized humanized label, so
    EVERY partner is represented and never silently dropped. Content-free (a change ref is a number/branch slug; a
    label is the author/agent name + optional ref Veripsa already surfaces in the comment — never code)."""
    refs: set[str] = set()
    for key in ("serialize_behind", "queued_behind", "contested_with"):
        for entry in (me.get(key) or []):
            if isinstance(entry, dict):
                entry = entry.get("change_id") or entry.get("label") or entry.get("agent") or ""
            if not (isinstance(entry, str) and entry.strip()):
                continue
            found = _ACK_REF_RE.findall(entry)
            if found:
                refs.update(found)              # a precise 'PR-<n>'/'BR-<x>' ref — the most stable partner id
            else:
                refs.add(" ".join(entry.split()))   # else the humanized label (an author-only BR- partner) — NEVER dropped
    return sorted(refs)


def _ack_coupling_paths(me: dict) -> list[str]:
    """The colliding PATHS this coupling runs through — as content-free, FRESHNESS-INDEPENDENT FILE identifiers
    (sorted, deduped). The SECOND half of a coupling's identity (which code the collision is on).

    FILE-LEVEL ONLY: an ack binds to THIS hash, so the
    hash must be STABLE across re-renders of the SAME coupling. The collision_points 'symbol' is only set when the
    main graph is provably FRESH for the file (FIX3); under a stale graph the SAME collision is kept but 'symbol'
    is NULL. Graph freshness flips BETWEEN events on the same PR (a missed push lands, self-heal re-ingests), so the
    OLD code — which keyed on the finer 'path::symbol' locus when fresh and the bare 'path' when stale — produced a
    DIFFERENT hash for the SAME file collision depending only on graph freshness at the moment of the event. That
    can strand an ack: the comment stores a stale-graph (file-level) hash, while a later event
    saw a fresh graph (symbol-level) and recomputed a different hash → the matching ack read as STALE → the label
    was stripped + the pause re-raised, with the coupling completely unchanged. The collision FILE is the stable,
    always-present locus; the symbol is a presentation refinement that must NOT restale an ack. So bind to the FILE
    only. (The comment still NAMES the symbol for the reader — only the ack-binding hash is file-level.) Content-free
    (paths only — never code bodies, and no symbol leaks into the binding)."""
    out: set[str] = set()
    for cp in _dicts(me.get("collision_points")):
        path = cp.get("path")
        if isinstance(path, str) and path:
            out.add(path)
    for p in (me.get("paths") or []):
        if isinstance(p, str) and p:
            out.add(p)
    return sorted(out)


def is_material_coupling(me: dict | None) -> bool:
    """Is THIS change in a MATERIAL coupling — a REAL in-flight cross-PR collision worth a conscious pause?
    TRUE for a HARD direct collision (verdict == serialize) OR a warn that actually has an in-flight counterpart
    (warn AND ≥1 contested_with partner). FALSE for a SOLO signal — a hotspot / split-advice / shared-foundation
    flag with NO in-flight partner is a NOTICE, never a pause (precision: we do not pause people who are not
    actually coupled). FALSE for clear / unknown / a non-dict.
    NOT serialize_soft: that is the engine's deliberately LOW-STAKES append-order heads-up (e.g. two PRs both
    appending a gate to a build/list file) — the engine renders it `neutral` ON PURPOSE so it never blocks a
    trivial git-append ordering; pausing it would negate the engine's own precision softening (a false pause)."""
    if not isinstance(me, dict):
        return False
    verdict = me.get("verdict")
    if verdict == "serialize":
        return True
    if verdict == "warn":
        return bool(_dicts(me.get("contested_with")) or [x for x in (me.get("contested_with") or []) if x])
    return False


def coupling_snapshot(me: dict | None) -> str:
    """A STABLE, CONTENT-FREE hash identifying THIS coupling — what an ack is bound to. Computed over the SORTED
    set of {the other in-flight PR refs this PR collides with} + {the colliding paths/symbols} — identifiers
    Veripsa ALREADY surfaces in the comment. NEVER hashes file contents. The hash is short (12 hex) and stable:
    the same coupling (same partners, same paths) always yields the same hash, so an ack stays valid across a
    re-render; a MATERIALLY changed coupling (a different partner / different paths) yields a DIFFERENT hash, so
    the prior ack reads stale and the pause re-raises. Returns '' for a non-material / empty coupling (nothing to
    bind an ack to)."""
    if not isinstance(me, dict):
        return ""
    refs = _ack_partner_refs(me)
    paths = _ack_coupling_paths(me)
    if not (refs or paths):
        return ""
    # \x1e (record separator) between the two field groups so a ref can never collide with a path string; sorted
    # already, so the digest is order-independent of how the engine listed them. surrogatepass: a path may carry
    # lone surrogates (a hostile name) — never crash the digest.
    material = ("\x1f".join(refs) + "\x1e" + "\x1f".join(paths)).encode("utf-8", "surrogatepass")
    return hashlib.sha256(material).hexdigest()[:12]


def _ack_snap_marker(snapshot: str) -> str:
    """The invisible HTML marker that embeds the coupling snapshot in the posted comment (an ack binds to it)."""
    return f"<!-- {_ACK_SNAP_PREFIX}{snapshot} -->"


def prior_snapshot_from_comment(comment_body: str | None) -> str | None:
    """The coupling snapshot hash embedded in Veripsa's PREVIOUS posted comment, or None if there is no marker
    (the PR had no Veripsa comment, or a pre-pause-tier comment). Stateless ack readback: this is the 'what was
    last acknowledged-for' the webhook handler compares against the CURRENT snapshot to decide fresh vs stale."""
    if not isinstance(comment_body, str):
        return None
    m = _ACK_SNAP_RE.search(comment_body)
    return m.group(1) if m else None


def apply_pause_ack(rendered: dict, impact: dict, change_ref: str, *, label_present: bool,
                    prior_hash: str | None, branch: str = "main", is_fork: bool = False,
                    prior_confirmed: bool = True, preserve_ack_on_uncertainty: bool = False,
                    graph_degraded: bool = False) -> dict:
    """THE PAUSE-AND-ACK TRANSFORM. Take a already-rendered {conclusion, title, summary, comment} (from
    render_pr_check) and apply the 一時停止 tier on top of it — PURE, content-free, stateless.

    Inputs that come from GitHub (the webhook handler supplies them; this function never calls out):
      label_present   : is the `veripsa-ack` LABEL on the PR right now (the ack signal — agent- or human-set).
      prior_hash      : the snapshot hash embedded in Veripsa's PREVIOUS comment (None if none) — what the ack was
                        bound to. Compared to the CURRENT snapshot to tell a FRESH ack from a STALE one.
      prior_confirmed : did the prior-comment read SUCCEED this event? `prior_hash=None` is AMBIGUOUS — it means
                        EITHER "there is genuinely no prior Veripsa comment yet" (the label was added before any
                        comment / a pre-tier comment → the ack has nothing to bind to → PAUSE, write the marker so
                        the next event can recognize it) OR "a prior comment exists but its hash could NOT be read
                        this event" (a transient list error / GitHub eventual-consistency on the neighbor path's
                        SEPARATE read → must NOT strip a valid ack). The pure function cannot tell these apart from
                        prior_hash alone; the HANDLER knows (it performed the read), so it passes prior_confirmed=
                        False when the read FAILED. Default True keeps the existing "no comment yet → pause"
                        semantics for callers/tests that supply a reliable None.
      preserve_ack_on_uncertainty : the caller has an independently safe, filtered impact but cannot verify a
                        BR-* participant against GitHub. A transient authority read must never strip, rebind, or
                        newly recognize an existing ACK. When true, a present ACK and its prior marker are kept
                        unchanged behind a neutral verification-pending result until an authoritative event can
                        decide; genuine un-acked PR↔PR materiality still pauses normally.
      graph_degraded  : (G5) is the stored main graph DEGRADED this event — behind HEAD and self-heal could NOT
                        bring it current (the SAME G1 predicate `_graph_degraded`, threaded from the handler so
                        the two can never diverge)? An EMPTY coupling recompute means TWO different things: under
                        a HEALTHY graph it is "empty because genuinely uncoupled / a once-lazy read" (keep the ack
                        — anti-storm stickiness), but under a DEGRADED graph it is "empty because the graph was
                        too stale to compute the coupling" — a NON-ANSWER that must NOT clear the pause via ack
                        stickiness (the coupling may still exist, or a new one formed, but reads empty). When
                        True AND the recompute is empty (`empty_recompute`), the ack is neither honored nor
                        stripped: an honest `ack_unconfirmable_degraded` neutral state is surfaced, the label is
                        KEPT, and the PRIOR hash marker is preserved so the NEXT (non-degraded) event re-confirms.
                        DEFAULT False = today's EXACT behavior (fail-safe / gen-agnostic: a caller that omits it
                        — the Brief, a narrow injected fake — gets the shipped anti-storm keep-ack, never a
                        spurious withhold, never a crash). Applies ONLY to `empty_recompute`; `proven_match`
                        (a positive re-confirmation) still clears and `unreadable_prior` (a transient GitHub read
                        failure — a DIFFERENT empty, not a graph-staleness signal) keeps its own fail-safe.

    Returns the rendered dict with three pause-tier fields ADDED:
      snapshot     : the current coupling snapshot hash ('' when not material — nothing to bind).
      ack_state    : 'not_material' | 'paused' | 'acknowledged' | 'stale_reack' |
                     'ack_verification_pending' | 'ack_unconfirmable_degraded'.
                     ('ack_unconfirmable_degraded' is the G5 check-path state — see `graph_degraded` below. The
                     Brief never emits it: the Brief passes graph_degraded=False, so it stays in the shipped
                     5-state set `ACK_STATES` and behaves as today for the degraded case.)
      label_action : 'remove' when a STALE ack's label must be taken off (so it reads un-acked) — else None. The
                     handler guards the removal so it can never loop (only remove when present AND stale).

    NOT MATERIAL (clear / unknown / a solo hotspot-or-split notice with no in-flight partner) → returned UNCHANGED
    (conclusion stays success/neutral) — we never pause someone who is not actually coupled (precision).

    MATERIAL:
      • acknowledged (label present AND the ack is not POSITIVELY proven stale) → conclusion `neutral`, title
        "Acknowledged — coupling snapshot recorded"; the comment notes the ack is recorded. The pause overlay
        cleared because someone consciously engaged with the coupling — NOT because Veripsa approved the change
        or pardoned the coupling (records-not-correctness; branch-protection still decides what blocks). This is
        the proven-match case (prior_hash == current snapshot)
        AND — critically — the FAIL-SAFE case: a present label whose staleness CANNOT be confirmed (prior_hash
        unreadable this event, or the current snapshot recomputed EMPTY/unconfirmable) KEEPS the ack rather than
        stripping it on a non-proof. An ack must STICK across no-op re-renders (acting AND neighbor paths) until
        the coupling ACTUALLY changes.
      • paused (no label) → conclusion `action_required` (the ONE GitHub conclusion that both reads as "you must
        act" AND gates a *required* check) + a pause banner whose FIRST instruction is the proceed-by-ack path.
      • stale_reack — ONLY on a POSITIVELY-PROVEN coupling change: label present AND BOTH the prior hash and the
        current snapshot are real, non-empty, content-free identities AND they DIFFER (the partners/files actually
        moved). → action_required + label_action='remove' + a "re-acknowledge" line. A non-confirmable read is
        NEVER treated as stale (it would strip a still-valid ack).

    The current snapshot's invisible HTML marker is embedded in the returned comment so the NEXT event can read
    back what THIS coupling was, binding any ack to it. Content-free throughout (refs + paths + a hash, never a
    body). On a FORK PR we keep the redaction the renderer already applied (no partner refs / paths surface in the
    comment); the pause copy added here names NO partner (it points only to the generic ack mechanism)."""
    out = dict(rendered)
    impact = impact if isinstance(impact, dict) else {}
    changes = _dicts(impact.get("changes"))
    me = next((c for c in changes if c.get("change_id") == change_ref), None)
    if me is None:
        me = next((c for c in changes if c.get("agent") == change_ref or c.get("label") == change_ref), None)

    # FORK PRs never pause: a check posted on a fork's head sha does not stick on the base repo (it silently
    # fails), so an `action_required` here would only put a confusing "paused" banner on the fork while the PR
    # stays mergeable — an inconsistent UX with no actual gate. (Mirrors the neighbor-refresh fork guard.) A fork
    # contributor cannot add the base repo's `veripsa-ack` label anyway. So treat a fork as a non-material notice.
    if is_fork:
        out["snapshot"] = ""
        out["ack_state"] = "not_material"
        out["label_action"] = None
        return out

    # AUTHORITY UNCERTAINTY IS NOT AN ACK EVENT. The PR-only speculative surface is safe enough to retain an
    # independent collision verdict, but it is not the full coupling identity while a BR-* participant remains
    # unverified. Therefore an already-present label must not be interpreted as acknowledging that filtered
    # snapshot, and a mismatching prior marker must not be stripped or rebound to it. Keep the prior binding (when
    # readable), publish a neutral pending result, and let the next authoritative event make the normal decision.
    # If the prior comment itself was unreadable, do not overwrite it: that is the only copy of the marker.
    if preserve_ack_on_uncertainty and label_present:
        pending_note = (
            "**Acknowledgement verification pending:** Veripsa left the existing `veripsa-ack` label and its "
            "snapshot binding unchanged while branch state is unavailable. It will retry automatically; no "
            "re-acknowledgement is needed for the unverified branch."
        )
        body = out.get("comment")
        if prior_hash is None and not prior_confirmed:
            body = None
        elif isinstance(body, str):
            marker = _ack_snap_marker(prior_hash) if prior_hash else ""
            if marker and marker not in body:
                body = marker + "\n" + body
            body = _cap_comment_body([*body.split("\n"), "", pending_note])
        out["conclusion"] = "neutral"
        out["title"] = "Veripsa — acknowledgement verification pending"
        out["summary"] = ((out.get("summary") or "") + "\n\n" + pending_note).strip()
        out["comment"] = body
        out["snapshot"] = prior_hash or ""
        out["ack_state"] = "ack_verification_pending"
        out["label_action"] = None
        return out

    if not is_material_coupling(me):
        # a notice, never a pause: leave the conclusion (success / neutral) exactly as rendered.
        out["snapshot"] = ""
        out["ack_state"] = "not_material"
        out["label_action"] = None
        return out

    snapshot = coupling_snapshot(me)
    # ACK STICKINESS — only a POSITIVELY-PROVEN coupling change may strip a present ack. The OLD logic stripped
    # whenever `label_present AND prior_hash != snapshot`, which silently
    # included two NON-PROOF cases that a valid, stable, MATCHING ack hit on a re-render:
    #   (1) prior_hash is None *because the read FAILED this event* — a transient list-comments error, or GitHub
    #       eventual-consistency on the SEPARATE API read the NEIGHBOR path makes after seeing the label. "I
    #       couldn't read what it was acked for" is NOT "the coupling changed". (DISTINCT from a CONFIRMED None =
    #       there is genuinely no prior comment yet → the ack has nothing to bind to → that still PAUSES, below.)
    #   (2) snapshot == '' — the recompute produced an EMPTY/unconfirmable identity (e.g. the brain momentarily
    #       returned a material verdict with no partner/path under a stale graph, or main_impact_surface was read
    #       once-lazily and the row is degraded). An empty identity is "I can't confirm the coupling right now",
    #       NOT "the coupling is a DIFFERENT one".
    # Both stripped a still-valid ack and re-raised action_required — undoing the acknowledgement on a no-op
    # event (and the NEIGHBOR refresh, firing on ANOTHER PR's open/sync/push, did this to an acked PR it merely
    # re-rendered). FAIL-SAFE: a non-confirmable read KEEPS the ack against the PRIOR hash. We ONLY declare a
    # stale (changed-coupling) ack when BOTH hashes are real, non-empty, content-free identities AND they differ —
    # a positive signal that the partners/files actually moved. Then-and-only-then strip + ask to re-acknowledge.
    proven_match = prior_hash is not None and bool(snapshot) and prior_hash == snapshot   # the normal clear path
    # the two non-proof escapes — a present label we CANNOT prove stale, so we keep it (and re-bind to prior_hash):
    unreadable_prior = (prior_hash is None) and (not prior_confirmed)   # the read FAILED (transient / eventual-consistency)
    empty_recompute = (not snapshot) and bool(prior_hash)              # this event can't confirm the identity; trust prior
    # G5 — EMPTY-BECAUSE-DEGRADED vs EMPTY-BECAUSE-UNCOUPLED. `empty_recompute` above admits (see the comment
    # block) that an empty identity can be caused by "a stale graph / the row is degraded". Treating ALL empties
    # identically KEEPS the ack → `acknowledged` → clears `action_required` on a NON-ANSWER: under the G1/G3
    # degraded-graph condition the coupling might still exist (or a NEW, never-acked coupling formed) but reads
    # empty ONLY because the graph was too stale to compute it — a false clear via ACK stickiness. So split the
    # empty: an empty recompute UNDER A DEGRADED GRAPH is NOT trustworthy stickiness — it gets the honest
    # `ack_unconfirmable_degraded` branch below (neutral, label KEPT, prior marker preserved, NOT cleared as
    # acknowledged). An empty recompute under a HEALTHY graph keeps its shipped anti-storm keep-ack EXACTLY (the
    # neighbor-safety contract — a neighbor's open/sync/push re-rendering an acked PR must not re-pause).
    # `unreadable_prior` is a DIFFERENT empty (a transient GitHub read failure, prior_hash None) — NOT a graph-
    # staleness signal — so it is deliberately NOT gated on graph_degraded and keeps its own fail-safe.
    degraded_empty = bool(label_present) and empty_recompute and bool(graph_degraded)
    keep_unconfirmed = bool(label_present) and not proven_match and (
        unreadable_prior or (empty_recompute and not degraded_empty))
    acknowledged = bool(label_present) and (proven_match or keep_unconfirmed)
    # stale = a POSITIVELY-PROVEN change: both hashes real + non-empty + different. A confirmed-None (genuinely no
    # prior comment yet) is NOT stale — it falls through to `paused` below (the ack binds on the marker we write now).
    proven_stale = (bool(label_present) and bool(prior_hash) and bool(snapshot) and prior_hash != snapshot)
    stale = proven_stale
    # The marker embeds the coupling's IDENTITY so the NEXT event can read it back and re-bind the ack. Content-free.
    #   • normal/paused/stale: embed the CURRENT snapshot (it is the identity the ack is — or will be — bound to).
    #   • keep_unconfirmed (we KEPT an ack on a NON-PROOF — empty recompute OR an UNREADABLE prior read): embed the
    #     PRIOR hash, NEVER the freshly-recomputed snapshot. For the empty-recompute case prior_hash is present, so
    #     this preserves the binding (a single unconfirmable re-render must not erase it to '' and orphan the ack).
    #     For the UNREADABLE-PRIOR case prior_hash is None, so we embed '' — the ACK-EVASION FIX:
    #     if the coupling MATERIALLY CHANGED on the very event the prior read failed, adopting the new snapshot here
    #     would stamp the changed coupling's identity into the comment, and the NEXT event would read prior==current
    #     → proven_match → auto-"acknowledged" for a coupling that was NEVER re-acked. Embedding '' instead forces
    #     the next event to fail proven_match and re-verify (re-pause / re-ack) the changed coupling. (The KEPT ack
    #     this event is fail-safe stickiness across a transient read failure; it must not silently RE-BIND to a
    #     possibly-different coupling.)
    #   • degraded_empty (G5 — we KEPT the ack-binding but did NOT honor it as acknowledged, because the empty
    #     recompute was caused by a degraded graph): embed the PRIOR hash (present, since empty_recompute requires
    #     it), NEVER the empty snapshot — the binding must survive so the NEXT non-degraded event re-confirms it.
    embed_hash = (prior_hash or "") if (keep_unconfirmed or degraded_empty) else snapshot
    marker = _ack_snap_marker(embed_hash)
    body = out.get("comment")
    # the material render always produces a comment (serialize/serialize_soft/warn all comment); guard anyway.
    base_lines = body.split("\n") if isinstance(body, str) else [f"### Veripsa — heading to {_code(branch)}"]

    if acknowledged:
        out["conclusion"] = "neutral"
        out["title"] = "Veripsa — Acknowledged"
        # HONEST ACK FRAMING (Core risk B — "ACK is NOT a pardon"): the prior copy read "proceeding past a recorded
        # coupling" / "is no longer pausing this check", which can land as "Veripsa let you through" / "Veripsa
        # says this PR is approved". An ack is NEITHER. It is the AUTHOR'S recorded statement "I have seen this
        # specific coupling snapshot and am choosing to proceed deliberately"; the coupling itself is unchanged.
        # Veripsa records the acknowledgement — it does NOT approve, pardon, or vouch for the change, and the
        # check moves out of action_required because the OVERLAY cleared, not because the coupling did. Branch-
        # protection still decides what blocks.
        ack_note = (
            "> **✓ Acknowledged — you recorded that you saw this coupling snapshot.** Someone added the "
            f"`{ACK_LABEL}` label, so Veripsa recorded that you saw this specific coupling and chose to proceed "
            "deliberately. **This is NOT an approval, a pardon, or a sign-off** — Veripsa does not assert the "
            "change is correct, and adding the label only records your acknowledgement; whether the PR can merge "
            "is decided by your branch-protection policy and reviewers, not by Veripsa. If the coupling changes "
            "(a different overlapping PR or files), Veripsa will pause again until you re-acknowledge the new "
            "snapshot. (Records-not-correctness — Veripsa records the acknowledgement; it never blocks by itself "
            "and it never approves by itself.)")
        # HEADLINE HONESTY (render-only): the base render's bold headline at index 1 is the verdict lead (for a
        # serialize coupling: "⏸ Wait in line — a direct collision is ahead"). On an ACKNOWLEDGED PR the check has
        # flipped to neutral/Acknowledged, so leaving that pause headline at the TOP made the comment LEAD with
        # "Wait in line" and only append "✓ Acknowledged" at the bottom = self-contradictory. Rewrite ONLY the
        # headline line (the `**…**` at index 1) to an acknowledged headline; the rest of the body (land order /
        # collision detail) is still useful and stays. Guard the index so a degenerate one-line body never throws.
        # The headline says "you acknowledged" (your action) NOT "proceeding past" (which read as Veripsa-let-you-through).
        acked_lines = list(base_lines)
        if len(acked_lines) >= 2 and acked_lines[1].lstrip().startswith("**"):
            acked_lines[1] = "**✓ You acknowledged this coupling snapshot — branch protection still decides what merges**"
        new_lines = [marker, *acked_lines, "", ack_note]
        out["ack_state"] = "acknowledged"
        out["label_action"] = None
    elif degraded_empty:
        # G5 — ACK CAN'T BE CONFIRMED BECAUSE THE CODE GRAPH ISN'T CONFIRMED CURRENT. The label is on and the
        # recompute came back EMPTY, but this event ran against a DEGRADED main graph (behind HEAD, self-heal did
        # not bring it current), so the empty is a NON-ANSWER, not a proof of "still the same coupling". We do NOT
        # honor ack stickiness here (that would clear the pause on a non-answer — a false clear), and we do NOT
        # strip the label (the human decision persists — this is not a coupling change). Instead: a neutral,
        # honest "not confirmed" state in the SAME register as the G1 stale-graph withhold — the ack re-confirms
        # once the graph catches up. RECALL-SAFE: this only turns a would-be acknowledged-clear into an honest
        # unknown/pending; it never suppresses a real collision and never strips the label.
        out["conclusion"] = "neutral"
        out["title"] = "Veripsa — acknowledgement not confirmed (code graph not confirmed current)"
        degraded_note = (
            "> **Couldn't confirm your acknowledgement — the code graph isn't confirmed current.** The "
            f"`{ACK_LABEL}` label is still on this PR (your acknowledgement was **kept, not removed**), but "
            f"Veripsa's view of {_code(branch)} is behind its latest commit and could not be refreshed this "
            "time, so it cannot confirm the coupling you acknowledged is still the current one — this check is "
            "treated as **not confirmed**, not cleared. This is usually transient: the next push or update "
            "refreshes the view and Veripsa re-confirms your acknowledgement automatically (or pauses again if "
            "the coupling has actually changed). Nothing to re-do — this is **not a new pause** and your "
            "acknowledgement was **not dropped**. (Advisory by default; your branch-protection policy decides "
            "what blocks.)")
        out["summary"] = degraded_note
        new_lines = [marker, *base_lines[:2], "", degraded_note, "", *base_lines[2:]] if len(base_lines) >= 2 \
            else [marker, degraded_note, "", *base_lines]
        out["ack_state"] = "ack_unconfirmable_degraded"
        out["label_action"] = None
    else:
        out["conclusion"] = "action_required"
        out["title"] = "Veripsa — Paused (acknowledge to proceed)"
        # WORDING HONESTY (PO #2, 2026-06-25) + MOBILE-READABILITY TRIM (Marketplace UX): the pause-ack tier is
        # the ONE place Veripsa emits `action_required`, and `action_required` IS a merge blocker under branch
        # protection when the Veripsa check is marked required. The banner is now TWO short sentences — (a) why it
        # paused + how to proceed (add `veripsa-ack`, or wait for the other PR to land), (b) the one records-not-a-
        # pardon clause — instead of the old ~120-word single-blockquote wall. The "neutral by default / branch-
        # protection is the enforcer" and the "records who acknowledged / does not assert correctness" sentences
        # were DROPPED here because they DUPLICATE the <sub> footer ("Advisory by default; your branch-protection
        # policy decides what blocks" + "it does not assert correctness"), which stays as the SINGLE place that
        # advisory/correctness disclaimer lives (it otherwise appeared 3× in one comment).
        if stale:
            banner = (
                "> **⏸ Veripsa re-paused this — the coupling changed since you acknowledged.** The "
                f"`{ACK_LABEL}` label was removed; to proceed, re-add it once you have looked at the new overlap "
                "(or wait for the other PR(s) to land and this clears automatically). Re-adding the label records "
                "your acknowledgement of the new coupling — not an approval or a pardon.")
            out["label_action"] = "remove"
            out["ack_state"] = "stale_reack"
        else:
            banner = (
                "> **⏸ Veripsa paused this — you are in a real coupling with other in-flight work.** To proceed, "
                f"add the `{ACK_LABEL}` label (an agent can run `gh pr edit --add-label {ACK_LABEL}`; a human can "
                "click it), or wait for the other PR(s) to land and this clears automatically. Adding the label "
                "records your acknowledgement — not an approval or a pardon.")
            out["label_action"] = None
            out["ack_state"] = "paused"
        # CHECK SUMMARY agrees with the title (#5): the title is "Paused (acknowledge to proceed)", so the
        # the pause banner goes at the TOP (right under the marker), so the proceed-by-ack instruction is the first
        # thing read; the full verdict detail (who/where) stays below it unchanged. KEEP the base render's bold
        # verdict headline at index 1 ("⏸ Wait in line — a direct collision is ahead"): `Wait in line` is the BASE
        # SIGNAL and Paused is an acknowledgement OVERLAY on top of it, so dropping that line would erase the base
        # signal from the comment and break the four-signal contract (it also broke the storm/idempotency gates
        # that key on it). The redundancy the audit saw is removed from the BANNER instead (it no longer restates
        # the verdict — it states only why it paused and how to proceed). Strip any leading blank off the tail so
        # there is no double blank line before "This PR reserves:" (#6).
        if len(base_lines) >= 2:
            rest = base_lines[2:]
            while rest and rest[0] == "":
                rest = rest[1:]
            new_lines = [marker, *base_lines[:2], "", banner, "", *rest]
        else:
            new_lines = [marker, banner, "", *base_lines]

    out["snapshot"] = embed_hash   # the IDENTITY this ack is bound to (the surviving prior hash on an empty recompute)
    out["comment"] = _cap_comment_body(new_lines)
    return out
