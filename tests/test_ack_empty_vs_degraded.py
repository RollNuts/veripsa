#!/usr/bin/env python3
"""ACK EMPTY-vs-DEGRADED gate (G5) — an ACK must NOT stick through an UNCONFIRMABLE coupling recompute caused by a
DEGRADED main graph.

THE GAP (invalidation matrix row G5): `apply_pause_ack` has an `empty_recompute` escape — when the coupling
recompute comes back EMPTY (no partner / no path) but a prior ack hash is present, it KEEPS the ack (anti-storm
stickiness: an ack must survive no-op re-renders). Its OWN comment admits an empty can be caused by "a stale graph
/ the row is degraded". Treating ALL empties as keep-ack means: under the G1/G3 DEGRADED-GRAPH condition (the
stored graph is BEHIND main HEAD and self-heal could NOT bring it current) an ack STICKS and clears
`action_required` based on a NON-ANSWER — the coupling might still exist (or a NEW, never-acked one formed) but
reads empty ONLY because the graph was too stale to compute it. That is a FALSE CLEAR via ack stickiness.

THE FIX (recall-safe, code-only, no schema change): distinguish "empty because degraded/unread graph" from "empty
because genuinely uncoupled" BEFORE honoring ack stickiness. `apply_pause_ack` takes a keyword-only
`graph_degraded: bool = False` (threaded from the webhook handler via the SAME `_graph_degraded` predicate G1
uses). When the recompute is empty AND the graph is degraded, it emits an honest `ack_unconfirmable_degraded`
neutral state (SAME register as the G1 stale-graph withhold: "code graph not confirmed current, not cleared") —
it does NOT say "Acknowledged", does NOT clear the coupling as pardoned, does NOT strip the label (the human
decision persists), and preserves the PRIOR-hash marker so the NEXT non-degraded event re-confirms. Default False
= the exact anti-storm keep-ack neighbor-safety contract.

Proves on the REAL render.apply_pause_ack (pure, offline — NO GitHub, NO DB):
  (i)   label + empty_recompute + graph_degraded=True  → NOT acknowledged; the new `ack_unconfirmable_degraded`
        neutral state, label NOT stripped, prior-hash marker embedded, snapshot preserved (not cleared as acked).
  (ii)  label + empty_recompute + graph_degraded=False → acknowledged / keep-ack UNCHANGED (NEIGHBOR-SAFETY: a
        healthy-graph empty recompute — a neighbor PR's open/sync/push re-rendering an acked PR — must NOT change).
  (iii) proven_match (prior_hash == current snapshot) + graph_degraded=True → still acknowledged (a positive
        re-confirmation clears, degraded or not — a proven match is proof, never an empty).
  (iv)  graph_degraded default/omitted → today's behavior (no spurious withhold, no crash — fail-safe default).
  (v)   a genuinely DIFFERENT coupling (proven_stale) is still stripped → re-pause regardless of graph_degraded.
  (vi)  unreadable_prior (transient GitHub read failure) is NOT conflated with graph-degraded — keeps its own
        fail-safe keep-ack even when graph_degraded=True (only `empty_recompute` gets the degraded gate).

Also — the WIRING (the neighbor-refresh path must THREAD the signal, not just the acting path):
  (vii) `_post_refreshes` (the sibling-PR re-render + re-POST chokepoint) THREADS `graph_degraded` into the REAL
        apply_pause_ack. Driven OFFLINE (a fake gh + a fake db + a capturing spy that forces the empty recompute,
        exactly as the neighbor/pauseack tests drive it): for a material, acked, empty-recompute neighbor,
        `_post_refreshes(..., graph_degraded=True)` yields ack_state `ack_unconfirmable_degraded` (label NOT
        stripped) while `graph_degraded=False` yields `acknowledged` — so a sibling event can no longer re-clear an
        acking PR's honest degraded state while the graph is still stale (the false-clear flap), and
        neighbor-safety is preserved (neutral + label kept in BOTH cases; never a re-pause, never a strip).

Run:  python3 tests/test_ack_empty_vs_degraded.py
"""
from __future__ import annotations
import json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import render as R   # noqa: E402
import webhook_handlers as WH   # noqa: E402  (the neighbor-refresh chokepoint _post_refreshes; it binds apply_pause_ack by name)

FAIL = 0


def chk(c, label):
    global FAIL
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    if not c:
        FAIL = 1


def _marker(snap: str) -> str:
    return f"<!-- veripsa-ack-snap:{snap} -->"


# A material direct collision WITH a real partner + path → a NON-EMPTY coupling snapshot (what the ack binds to).
SERIALIZE = {"repo": "acme/app", "branch": "main", "changes": [
    {"change_id": "PR-9", "label": "PR-9", "agent": "alice", "verdict": "serialize",
     "paths": ["svc/auth.py"], "serialize_behind": ["maint PR-3"],
     "collision_points": [{"symbol": "login", "path": "svc/auth.py"}]},
]}
# The SAME PR-9 rendered material (verdict serialize) but with NO partner + NO path — the DEGRADED-GRAPH recompute:
# the row is material enough to reach the ack logic (is_material_coupling True) yet its identity is EMPTY
# (coupling_snapshot == ''), exactly the "stale graph / degraded row" case apply_pause_ack's own comment names.
EMPTY_SERIALIZE = {"repo": "acme/app", "branch": "main", "changes": [
    {"change_id": "PR-9", "label": "PR-9", "agent": "alice", "verdict": "serialize",
     "paths": [], "serialize_behind": [], "collision_points": []},
]}
# The SAME PR-9 but a MATERIALLY DIFFERENT coupling (different partner + different file) → a DIFFERENT non-empty
# hash (a genuine coupling change — proven_stale, must re-pause).
SERIALIZE_CHANGED = {"repo": "acme/app", "branch": "main", "changes": [
    {"change_id": "PR-9", "label": "PR-9", "agent": "alice", "verdict": "serialize",
     "paths": ["svc/billing.py"], "serialize_behind": ["bob PR-77"],
     "collision_points": [{"symbol": "charge", "path": "svc/billing.py"}]},
]}

# ── OFFLINE harness for the NEIGHBOR-refresh wiring case (vii): a fake GitHub + a fake db callable, just enough
#    to drive the REAL `_post_refreshes` -> apply_pause_ack overlay with NO Postgres and NO network. Modeled on the
#    StickGitHub fake the pauseack-sticks gate uses, trimmed to exactly what the material-neighbor overlay path
#    touches. `impact_override` feeds the surface directly (no main_impact_surface read). ──────────────────────────
_HEAD = "a" * 40
ACK_LABEL = R.ACK_LABEL
# The impact the neighbor path re-reads: PR-9 is a MATERIAL coupling (verdict serialize + partner + path) so it
# renders `neutral` and reaches the pause-ack overlay; head_sha matches the PR head so the evidence check passes.
NEIGHBOR_IMPACT = {"repo": "acme/app", "branch": "main", "changes": [
    {"change_id": "PR-9", "label": "PR-9", "agent": "alice", "verdict": "serialize", "head_sha": _HEAD,
     "paths": ["svc/auth.py"], "serialize_behind": ["maint PR-3"],
     "collision_points": [{"symbol": "login", "path": "svc/auth.py"}]},
]}
_NEIGHBOR_SNAP = R.coupling_snapshot(NEIGHBOR_IMPACT["changes"][0])   # the prior ack the neighbor carries (non-empty)


class _FakeGH:
    """Just enough GitHub client for the material-neighbor overlay path of `_post_refreshes` (evidence read →
    label read → prior-hash read → check/comment upsert). Records posted checks + label removals."""

    def __init__(self):
        self.checks = []          # every upsert_check (conclusion/title) — the posted verdict
        self.removed = []         # (pr, label) on remove_label — proves the ack label was (not) stripped
        # a prior Veripsa comment carrying the ack-snapshot marker so _prior_ack_snapshot returns a real prior hash
        self.comments = [{"id": 1, "number": 9, "user": {"type": "Bot"},
                          "body": f"{WH._comment_marker(9)}\n{_marker(_NEIGHBOR_SNAP)}\nprior verdict body"}]
        self.labels = {9: [ACK_LABEL]}

    def get_pull_request(self, repo, number):
        return {"number": number, "state": "open", "changed_files": 1,
                "head": {"sha": _HEAD, "repo": {"id": 1}, "ref": "feature/9"},
                "base": {"ref": "main", "sha": "b" * 40, "repo": {"id": 1}}}

    def list_pr_file_metadata(self, repo, number, declared_files=0, declared_pages=0):
        return {"changed": ["svc/auth.py"], "changed_ranges": {"svc/auth.py": []},
                "added_paths": [], "conflict_markers": [], "raw_entry_count": 1}

    def compare_changed_paths_strict(self, repo, base_sha, ref):
        return []

    def pr_labels(self, repo, number, strict=False):
        return list(self.labels.get(number, []))

    def list_issue_comments(self, repo, number):
        return [c for c in self.comments if c["number"] == number]

    def upsert_comment(self, repo, number, marker, body, **kw):
        return {"id": 999}

    def upsert_check(self, repo, sha, conclusion, title, summary, **kw):
        self.checks.append({"conclusion": conclusion, "title": title})
        return {"id": 456, "conclusion": conclusion}

    def patch_comment_if_exists(self, repo, number, marker, body):
        return False

    def remove_label(self, repo, number, name):
        self.removed.append((number, name))
        return True

    def pull_request_head(self, repo, number):
        return _HEAD


def _fake_db(sql, params=None):
    """Answer only the reads the neighbor overlay path makes; SAVEPOINT/RELEASE/ROLLBACK are no-ops (best-effort
    txn control), co-change → [] (a valid empty list), coverage → {} (nudge resolves to None), everything else None."""
    s = sql if isinstance(sql, str) else ""
    low = s.lstrip().lower()
    if low.startswith(("savepoint", "release", "rollback")):
        return None
    if "co_change_partners_with_authority" in s:
        return []
    if "account_coverage_surface" in s:
        return {}
    if "main_impact_surface" in s:
        return NEIGHBOR_IMPACT
    return None


def _run_neighbor_refresh(graph_degraded, *, thread=True):
    """Drive the REAL `_post_refreshes` for ONE material, acked neighbor whose recompute is forced EMPTY at the
    overlay (via a capturing spy on apply_pause_ack that blanks the target change, then delegates to the REAL
    render.apply_pause_ack). Returns (captured, gh, touched). `thread=False` simulates the PRE-FIX wiring — it
    strips graph_degraded from the kwargs before delegating — to prove fail-before."""
    captured = {}
    real_apply = R.apply_pause_ack

    def _spy(rendered, impact, change_ref, **kwargs):
        captured["graph_degraded"] = kwargs.get("graph_degraded", "<absent>")
        if not thread:
            kwargs.pop("graph_degraded", None)              # PRE-FIX: the neighbor path did not thread the signal
        # force the DEGRADED empty recompute: keep PR-9 MATERIAL (verdict serialize) but blank partner+paths so
        # coupling_snapshot recomputes to '' — the exact stale-graph read the acting path already gates.
        imp = json.loads(json.dumps(impact)) if isinstance(impact, dict) else impact
        for ch in (imp.get("changes") or []):
            if isinstance(ch, dict) and ch.get("change_id") == change_ref:
                for k in ("serialize_behind", "queued_behind", "contested_with", "paths", "collision_points"):
                    ch[k] = []
        ov = real_apply(rendered, imp, change_ref, **kwargs)
        captured["ack_state"] = ov.get("ack_state")
        captured["label_action"] = ov.get("label_action")
        captured["snapshot"] = ov.get("snapshot")
        return ov

    gh = _FakeGH()
    refreshes = [{"change": "PR-9", "agent": "alice", "conclusion": "neutral",
                  "title": "Veripsa", "summary": "s", "comment": "### Veripsa\nbody\nx"}]
    WH.apply_pause_ack = _spy
    try:
        touched = WH._post_refreshes(gh, "acme/app", refreshes, db=_fake_db, branch="main",
                                     impact_override=NEIGHBOR_IMPACT, graph_degraded=graph_degraded)
    finally:
        WH.apply_pause_ack = real_apply
    return captured, gh, touched


def main() -> int:
    print("ACK EMPTY-vs-DEGRADED gate (G5) — an ack must not stick through an unconfirmable recompute over a degraded graph")

    base = R.render_pr_check(SERIALIZE, "PR-9")   # a realistic material neutral render (base body being overlaid)
    snap = R.coupling_snapshot(SERIALIZE["changes"][0])   # the coupling the prior ACK was bound to
    chk(bool(snap), "(setup) the normal coupling has a NON-EMPTY snapshot (the prior ack binds to it)")
    chk(R.coupling_snapshot(EMPTY_SERIALIZE["changes"][0]) == "",
        "(setup) the degraded recompute has an EMPTY snapshot (material verdict, no partner/path)")
    chk(R.is_material_coupling(EMPTY_SERIALIZE["changes"][0]),
        "(setup) the degraded recompute is still is_material_coupling (verdict serialize) — it reaches the ack logic")

    # ── (i) label + empty_recompute + graph_degraded=True → NOT acknowledged; honest unconfirmable-degraded ──────
    degraded = R.apply_pause_ack(base, EMPTY_SERIALIZE, "PR-9",
                                 label_present=True, prior_hash=snap, branch="main", graph_degraded=True)
    chk(degraded["ack_state"] == "ack_unconfirmable_degraded",
        "(i) empty recompute UNDER a degraded graph → ack_state 'ack_unconfirmable_degraded' (NOT 'acknowledged')")
    chk(degraded["ack_state"] != "acknowledged",
        "(i) it is NOT treated as acknowledged — the ack is not honored on a non-answer (no false clear)")
    chk(degraded["conclusion"] == "neutral",
        "(i) conclusion is 'neutral' — an honest 'not confirmed / not cleared' (same register as the G1 withhold)")
    chk("Acknowledged" not in (degraded["title"] or ""),
        "(i) the title does NOT say 'Acknowledged' (it reads 'not confirmed')")
    chk(("not confirmed" in (degraded["title"] or "").lower())
        or ("not confirmed" in (degraded["comment"] or "").lower()),
        "(i) the copy says the code graph is not confirmed current (honest, recall-safe)")
    chk(degraded["label_action"] is None,
        "(i) the ack LABEL is KEPT (label_action None) — the human decision persists, nothing to re-do")
    chk(degraded["snapshot"] == snap and _marker(snap) in (degraded["comment"] or ""),
        "(i) the PRIOR-hash marker is preserved (never the empty snapshot) so the next fresh event re-confirms")

    # ── (ii) label + empty_recompute + graph_degraded=False → keep-ack UNCHANGED (neighbor-safety) ─────────────
    healthy = R.apply_pause_ack(base, EMPTY_SERIALIZE, "PR-9",
                                label_present=True, prior_hash=snap, branch="main", graph_degraded=False)
    chk(healthy["ack_state"] == "acknowledged",
        "(ii) empty recompute under a HEALTHY graph keeps the shipped anti-storm keep-ack ('acknowledged')")
    chk(healthy["conclusion"] == "neutral" and "Acknowledged" in (healthy["title"] or ""),
        "(ii) neutral + 'Acknowledged' — a neighbor re-render of an acked PR does NOT re-pause (no storm)")
    chk(healthy["label_action"] is None and healthy["snapshot"] == snap
        and _marker(snap) in (healthy["comment"] or ""),
        "(ii) label untouched + prior-hash marker preserved — byte-for-byte the current empty-recompute behavior")

    # ── (iii) proven_match + graph_degraded=True → still acknowledged (a re-confirmation clears, degraded or not) ─
    proven = R.apply_pause_ack(base, SERIALIZE, "PR-9",
                               label_present=True, prior_hash=snap, branch="main", graph_degraded=True)
    chk(proven["ack_state"] == "acknowledged" and proven["conclusion"] == "neutral",
        "(iii) proven_match (prior_hash == current snapshot) still clears as 'acknowledged' even under a degraded graph")
    chk("Acknowledged" in (proven["title"] or "") and proven["label_action"] is None,
        "(iii) a positive re-confirmation is proof, not an empty — the degraded gate does not touch it")

    # ── (iv) graph_degraded default/omitted → today's behavior (fail-safe default, no spurious withhold) ─────────
    default = R.apply_pause_ack(base, EMPTY_SERIALIZE, "PR-9",
                                label_present=True, prior_hash=snap, branch="main")   # graph_degraded omitted
    chk(default["ack_state"] == "acknowledged" and default["conclusion"] == "neutral" and default["snapshot"] == snap,
        "(iv) OMITTING graph_degraded == today's exact keep-ack ('acknowledged') — fail-safe / gen-agnostic default")

    # ── (v) a genuinely DIFFERENT coupling (proven_stale) is still stripped → re-pause regardless of graph ───────
    changed_base = R.render_pr_check(SERIALIZE_CHANGED, "PR-9")
    stale = R.apply_pause_ack(changed_base, SERIALIZE_CHANGED, "PR-9",
                              label_present=True, prior_hash=snap, branch="main", graph_degraded=True)
    chk(stale["conclusion"] == "action_required" and stale["ack_state"] == "stale_reack",
        "(v) a POSITIVELY-PROVEN coupling change (both hashes real + different) still re-pauses — degraded flag irrelevant")
    chk(stale["label_action"] == "remove",
        "(v) the stale ack label is still slated for removal (a real change is NOT an empty; the degraded gate never fires)")

    # ── (vi) unreadable_prior (transient GitHub read failure) is NOT conflated with graph-degraded ───────────────
    unreadable = R.apply_pause_ack(base, SERIALIZE, "PR-9",
                                   label_present=True, prior_hash=None, prior_confirmed=False,
                                   branch="main", graph_degraded=True)
    chk(unreadable["ack_state"] == "acknowledged" and unreadable["conclusion"] == "neutral",
        "(vi) a transient prior-read FAILURE keeps its own fail-safe keep-ack even when graph_degraded=True (not conflated)")
    chk(unreadable["label_action"] is None and unreadable["snapshot"] == "",
        "(vi) only empty_recompute gets the degraded gate; unreadable_prior still embeds '' (the ack-evasion fix, unchanged)")

    # ── (vii) WIRING: _post_refreshes (the neighbor-refresh re-render + re-POST) must THREAD graph_degraded into
    #    the REAL apply_pause_ack — else a sibling PR's event re-clears an acked neighbor's honest state while the
    #    graph is still stale (the false-clear flap). Driven OFFLINE (fake gh + fake db + a spy that forces the
    #    empty recompute and delegates to the REAL render.apply_pause_ack). ────────────────────────────────────────
    cap_t, gh_t, touched_t = _run_neighbor_refresh(True)
    chk(touched_t == 1, "(vii-setup) the material acked neighbor was actually processed by _post_refreshes (overlay ran)")
    chk(cap_t["graph_degraded"] is True,
        "(vii-a) _post_refreshes THREADS graph_degraded=True into apply_pause_ack (it reaches the state machine)")
    chk(cap_t["ack_state"] == "ack_unconfirmable_degraded",
        "(vii-a) with graph_degraded=True the acked empty-recompute neighbor yields 'ack_unconfirmable_degraded' (NOT re-cleared)")
    chk(cap_t["label_action"] is None and (9, ACK_LABEL) not in gh_t.removed,
        "(vii-a) the ack label is KEPT on the neighbor (not stripped) — neighbor-safety preserved")
    chk(bool(gh_t.checks) and gh_t.checks[-1]["conclusion"] == "neutral"
        and "not confirmed" in gh_t.checks[-1]["title"].lower(),
        "(vii-a) the POSTED neighbor check is the honest neutral 'not confirmed' state, not 'Acknowledged'")

    cap_f, gh_f, touched_f = _run_neighbor_refresh(False)
    chk(cap_f["graph_degraded"] == "<absent>",
        "(vii-b) graph_degraded=False threads nothing (apply_pause_ack default) — no spurious withhold")
    chk(cap_f["ack_state"] == "acknowledged" and (9, ACK_LABEL) not in gh_f.removed,
        "(vii-b) with graph_degraded=False the SAME neighbor keeps the shipped keep-ack ('acknowledged') — behavior unchanged")
    chk(bool(gh_f.checks) and gh_f.checks[-1]["conclusion"] == "neutral"
        and "Acknowledged" in gh_f.checks[-1]["title"],
        "(vii-b) the posted neighbor check reads 'Acknowledged' when the graph is healthy (neighbor-safety, no re-pause)")

    # fail-before control: with the PRE-FIX wiring (graph_degraded NOT threaded), graph_degraded=True STILL yields
    # 'acknowledged' on the neighbor path — the exact false-clear this fix removes.
    cap_pre, _, _ = _run_neighbor_refresh(True, thread=False)
    chk(cap_pre["ack_state"] == "acknowledged",
        "(vii-c) fail-before control: WITHOUT threading, graph_degraded=True still re-clears to 'acknowledged' (the gap)")

    print("ACK EMPTY VS DEGRADED GATE: " + ("PASS" if FAIL == 0 else "FAIL"))
    return FAIL


if __name__ == "__main__":
    raise SystemExit(main())
