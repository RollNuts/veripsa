#!/usr/bin/env python3
"""COORDINATION-BRIEF gate — the machine-readable coordination surface must stay schema-valid, content-free,
deterministic, enum-bounded, and consistent with the existing verdict + pause/ACK contract.

Premise: github-app/coordination_brief.py serializes core.main_impact_surface (the SAME dict render.py reads)
into a stable, versioned, read-only JSON `Coordination Brief`. Like render.py it is PURE + OFFLINE, so this test
needs no Postgres, no network, no deploy: it drives the serializer across the fixtures + extra synthetic inputs
and asserts the invariants that make the Brief safe to hand an in-flight agent.

Invariants checked here:
  * SCHEMA — every produced Brief validates against docs/design/COORDINATION_BRIEF.schema.json (draft 2020-12).
  * ENUM PARITY — the serializer's enum tuples exactly equal the schema's enum sets (no drift).
  * ENUM-BOUNDED — every classifying field in every Brief is a member of its declared enum.
  * CONTENT-FREE — no raw graph-size count leaks (fan_in / churn / symbols / impact_count / a numeric count),
    no forbidden keys, no obviously-body-shaped strings.
  * PINNED — every Brief carries an exact acting_head_sha; base_sha is a string or null.
  * DETERMINISTIC — same inputs → byte-identical Brief (no clock / random / network).
  * FIXTURE REGRESSION — the serializer reproduces each committed fixture Brief exactly.
  * PAUSED-IS-OVERLAY — state is always one of the FOUR base states; a pause/re-ack lives in ack.state +
    action_required + requires_human_ack, never as a fifth state.
  * VERDICT-CONTRACT CONSISTENCY — the Brief's ack overlay matches render.apply_pause_ack on the same input, and
    action_required ⟺ (a conflict marker OR a paused/stale material coupling).
  * PROVENANCE-PINNED — every counterpart the Brief names is pinned to the head SHA the analysis saw
    (evaluated_against, excludes self, deterministically ordered), freshness.status is the explicit 4-value verdict
    derived from head_matches, and the additive provenance fields (repository.generation / graph_source_sha /
    policy_version) stay content-free AND optional (a Brief remains valid + deterministic without them).

Run:  python3 tests/test_coordination_brief.py     (no DB needed)
   or pytest tests/test_coordination_brief.py -q
"""
from __future__ import annotations

import glob
import json
import os
import sys

from jsonschema import Draft202012Validator

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import coordination_brief as CB  # noqa: E402
import render as R  # noqa: E402

SCHEMA_PATH = os.path.join(ROOT, "docs", "design", "COORDINATION_BRIEF.schema.json")
FIXTURE_GLOB = os.path.join(ROOT, "docs", "design", "coordination_brief_fixtures", "*.json")


def _schema():
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def _validator():
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _fixtures():
    out = []
    for path in sorted(glob.glob(FIXTURE_GLOB)):
        with open(path) as f:
            doc = json.load(f)
        out.append((os.path.basename(path), doc))
    return out


def _regen(doc):
    """Re-run the serializer from a fixture's recorded input."""
    inp = doc["input"]
    return CB.coordination_brief(inp["impact"], inp["change_ref"], **inp["kwargs"])


def _walk(node):
    """Yield (key, value) for every dict entry and (None, value) for every scalar, recursively."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


# ── SCHEMA SET EXTRACTION (to assert serializer↔schema enum parity) ──────────────────────────────────────────
def _schema_enum(schema, *path):
    node = schema
    for p in path:
        node = node[p]
    return set(node["enum"])


# ── the tests ────────────────────────────────────────────────────────────────────────────────────────────────
def test_schema_is_valid_draft2020():
    _validator()   # check_schema raises if the schema itself is malformed


def test_enum_parity_serializer_matches_schema():
    """The serializer's enum tuples must equal the schema's enum sets, so neither drifts silently."""
    schema = _schema()
    rel = schema["$defs"]["relationship"]["properties"]
    assert set(CB.STATES) == _schema_enum(schema, "properties", "state")
    assert set(CB.RELATIONSHIP_KINDS) == set(rel["relationship_kind"]["enum"])
    assert set(CB.EVIDENCE_KINDS) == set(rel["evidence_kind"]["enum"])
    assert set(CB.RECOMMENDED_ACTIONS) == set(rel["recommended_action"]["enum"])
    assert set(CB.CONFIDENCE) == set(rel["confidence"]["enum"])
    assert set(CB.ACK_STATES) == set(schema["$defs"]["ack"]["properties"]["state"]["enum"])
    assert set(CB.FRESHNESS_STATUS) == _schema_enum(schema, "$defs", "freshness", "properties", "status")
    # the counterpart-provenance state reuses the four base states — assert the evaluated_change def stays in parity
    assert set(CB.STATES) == set(schema["$defs"]["evaluated_change"]["properties"]["state"]["enum"])


def test_fixtures_validate_against_schema():
    v = _validator()
    for name, doc in _fixtures():
        errors = sorted(v.iter_errors(doc["brief"]), key=lambda e: list(e.path))
        assert not errors, f"{name}: " + "; ".join(f"{list(e.path)} {e.message}" for e in errors)


def test_fixture_regression_serializer_reproduces_committed_brief():
    """The committed Brief must be exactly what the serializer produces from the recorded input (regression +
    determinism)."""
    for name, doc in _fixtures():
        assert _regen(doc) == doc["brief"], f"{name}: serializer output drifted from committed fixture"


def test_determinism_same_input_same_brief():
    for name, doc in _fixtures():
        a = json.dumps(_regen(doc), sort_keys=True)
        b = json.dumps(_regen(doc), sort_keys=True)
        assert a == b, f"{name}: non-deterministic serialization"


def test_no_clock_injected_evaluated_at_stays_none():
    """The pure serializer must NEVER stamp evaluated_at itself (that would break determinism) — it is null unless
    the caller supplies it."""
    _, doc = _fixtures()[0]
    inp = doc["input"]
    kw = dict(inp["kwargs"])
    kw.pop("evaluated_at", None)
    brief = CB.coordination_brief(inp["impact"], inp["change_ref"], **kw)
    assert brief["freshness"]["evaluated_at"] is None


def test_every_brief_is_pinned_to_a_head():
    for name, doc in _fixtures():
        b = doc["brief"]
        assert isinstance(b["acting_head_sha"], str) and b["acting_head_sha"], f"{name}: no acting_head_sha"
        assert b["base_sha"] is None or isinstance(b["base_sha"], str), f"{name}: base_sha not str|null"


def test_enum_bounded_every_classifier_in_range():
    for name, doc in _fixtures():
        b = doc["brief"]
        assert b["state"] in CB.STATES, f"{name}: state"
        assert b["ack"]["state"] in CB.ACK_STATES, f"{name}: ack.state"
        for r in b["relationships"]:
            assert r["relationship_kind"] in CB.RELATIONSHIP_KINDS, f"{name}: kind {r['relationship_kind']}"
            assert r["evidence_kind"] in CB.EVIDENCE_KINDS, f"{name}: evidence {r['evidence_kind']}"
            assert r["recommended_action"] in CB.RECOMMENDED_ACTIONS, f"{name}: action"
            assert r["confidence"] in CB.CONFIDENCE, f"{name}: confidence"


# CONTENT-FREE: a Brief may carry paths / symbol names / line numbers / PR refs / a 12-hex hash — but NEVER a raw
# graph-size count (the moat) nor a source/diff body. Enforced structurally by the schema's additionalProperties:
# false, and by this explicit key/shape scan so a future serializer change that adds a leak fails loudly.
_FORBIDDEN_KEYS = {
    "fan_in", "churn", "symbols", "impact_count", "count", "size", "cluster_count", "inflight_count",
    "distinct_blocked", "distinct_holders", "warn_count", "serialize_count", "unknown_count", "clear_count",
    "n_a", "n_b", "co", "strength", "lift", "prob", "body", "diff", "source", "patch",
}


def test_content_free_no_forbidden_keys():
    for name, doc in _fixtures():
        for k, _v in _walk(doc["brief"]):
            if k is None:
                continue
            assert k not in _FORBIDDEN_KEYS, f"{name}: forbidden key {k!r} leaked into the Brief"


def test_content_free_no_raw_graph_counts_across_many_inputs():
    """Drive the serializer over inputs that carry raw graph counts (fan_in/churn/symbols/impact_count/lift) and
    assert NONE of those numbers reach the Brief — only qualitative reach/basis words do."""
    impact = {
        "repo": "acme/app", "branch": "main", "clusters": [],
        "changes": [{
            "change_id": "PR-90", "head_sha": "abc0011223", "agent": "me", "label": "me PR-90", "verdict": "warn",
            "is_draft": False, "paths": ["core/hub.py"],
            "impact": ["a.py", "b.py", "c.py", "d.py", "e.py"], "impact_count": 5,
            "contested_with": ["zoe PR-88"], "serialize_behind": [], "queued_behind": [], "queued_behind_paths": [],
            "collision_points": [], "merge_conflict_likely": False, "conflict_points": [], "unknown_paths": [],
            "dampened_with": [], "depends_on_changing": [],
            # distinctive sentinel counts that can't collide with a PR number / line number / SHA
            "shared_foundation": [{"path": "core/hub.py", "fan_in": 90071, "churn": 90072,
                                   "symbols": 90073, "basis": "both"}],
        }, {"change_id": "PR-88", "head_sha": "z88", "agent": "zoe", "label": "zoe PR-88", "verdict": "clear",
            "paths": ["core/hub.py"]}],
    }
    cc = [{"edited": "core/hub.py", "partner": "core/dep.py", "prob": 1.0, "lift": 90074.0}]
    brief = CB.coordination_brief(impact, "PR-90", cochange=cc,
                                  ack={"label_present": False, "prior_hash": None, "prior_confirmed": True})
    blob = json.dumps(brief)
    for leak in ("90071", "90072", "90073", "90074", "impact_count", "fan_in", "lift", "\"5\""):
        assert leak not in blob, f"raw graph count/keyword {leak!r} leaked: {blob}"
    # the qualitative signals ARE present
    kinds = {r["relationship_kind"] for r in brief["relationships"]}
    assert "blast_radius" in kinds and "recurring_contention" in kinds
    reach = next(r for r in brief["relationships"] if r["relationship_kind"] == "blast_radius")["reach"]
    assert reach == "wide"  # 5 downstream > _WIDE_BLAST_TIER, expressed as a word, not a number


def test_paused_is_overlay_not_a_fifth_state():
    """The four base states are exactly four. A material coupling that is un-acked / stale must NOT invent a
    'paused' state — the pause rides ack.state + action_required + requires_human_ack, base state unchanged."""
    for name, doc in _fixtures():
        b = doc["brief"]
        assert b["state"] in CB.STATES
        assert b["state"] != "paused" and b["state"] != "action_required"
        if b["ack"]["state"] in ("paused", "stale_reack"):
            # the overlay is engaged → action_required, and the BASE state is still the verdict state
            assert b["action_required"] is True, f"{name}: paused overlay must set action_required"
            assert b["state"] in ("heads_up", "wait_in_line"), f"{name}: material coupling base state"


def test_requires_human_ack_matches_the_coupling_snapshot_binding():
    """requires_human_ack is True on exactly the relationships whose related_change is a partner the ack snapshot
    binds to — and only when the change is material. Mirrors render.apply_pause_ack precisely."""
    for name, doc in _fixtures():
        b = doc["brief"]
        inp = doc["input"]
        me = next((c for c in R._dicts(inp["impact"].get("changes"))
                   if c.get("change_id") == inp["change_ref"]), None)
        material = bool(R.is_material_coupling(me)) and not inp["kwargs"].get("is_fork", False)
        partner_refs = set(R._ack_partner_refs(me)) if (me and material) else set()
        for r in b["relationships"]:
            if r["requires_human_ack"]:
                assert material, f"{name}: requires_human_ack set on a non-material change"
                assert r["relationship_kind"] in ("direct_collision", "semantic_coupling"), \
                    f"{name}: requires_human_ack on {r['relationship_kind']}"
                assert r["related_change"] in partner_refs, \
                    f"{name}: requires_human_ack partner {r['related_change']} not in ack snapshot set"


def test_conflict_marker_is_action_required_but_not_ack():
    """An unresolved conflict marker is action_required (a hard build-breaker) but is NOT the ACK path — resolve
    the marker, there is no label to add. It is content-free (path + line only) and confidence 'certain'."""
    impact = {"repo": "acme/app", "branch": "main", "clusters": [],
              "changes": [{"change_id": "PR-99", "head_sha": "deadbeef99", "agent": "me", "label": "me PR-99",
                           "verdict": "clear", "is_draft": False, "paths": ["app.py"], "impact": [],
                           "contested_with": [], "serialize_behind": [], "queued_behind": [],
                           "queued_behind_paths": [], "collision_points": [], "merge_conflict_likely": False,
                           "conflict_points": [], "unknown_paths": [], "dampened_with": [],
                           "depends_on_changing": [], "shared_foundation": []}]}
    markers = [{"path": "app.py", "line": 12, "kind": "ours"}, {"path": "app.py", "line": 20, "kind": "theirs"}]
    brief = CB.coordination_brief(impact, "PR-99", conflict_markers=markers,
                                  ack={"label_present": False, "prior_hash": None, "prior_confirmed": True})
    assert brief["action_required"] is True
    cm = [r for r in brief["relationships"] if r["relationship_kind"] == "conflict_marker"]
    assert cm and all(r["requires_human_ack"] is False for r in cm)
    assert cm[0]["confidence"] == "certain"
    assert cm[0]["recommended_action"] == "resolve_conflict_marker"
    # the marker never carries the acknowledge path
    assert brief["ack"]["state"] == "not_material"
    _validator().validate(brief)


def test_ack_overlay_matches_apply_pause_ack():
    """The Brief's ack block must agree with render.apply_pause_ack on the same input — the Brief serializes the
    ack state machine, it does not re-invent it."""
    for name, doc in _fixtures():
        inp = doc["input"]
        ackin = inp["kwargs"].get("ack") or {}
        ov = R.apply_pause_ack({"comment": None, "conclusion": "neutral", "title": "", "summary": ""},
                               inp["impact"], inp["change_ref"],
                               label_present=bool(ackin.get("label_present")),
                               prior_hash=ackin.get("prior_hash"),
                               branch=inp["impact"].get("branch", "main"),
                               is_fork=inp["kwargs"].get("is_fork", False),
                               prior_confirmed=ackin.get("prior_confirmed", True))
        b = doc["brief"]["ack"]
        assert b["state"] == ov["ack_state"], f"{name}: ack.state != apply_pause_ack.ack_state"
        assert b["coupling_snapshot"] == (ov["snapshot"] or ""), f"{name}: snapshot mismatch"
        assert b["label_action"] == ov["label_action"], f"{name}: label_action mismatch"


def test_fork_pr_is_redacted():
    """A fork PR's Brief must not name the base repo's other in-flight PRs / paths — only the fork's own conflict
    markers survive; ack is never material on a fork."""
    _, doc = next(d for d in _fixtures() if d[0] == "wait_in_line.json")
    inp = doc["input"]
    brief = CB.coordination_brief(inp["impact"], inp["change_ref"], is_fork=True,
                                  ack={"label_present": False, "prior_hash": None, "prior_confirmed": True})
    assert brief.get("redacted") is True
    assert all(r["relationship_kind"] == "conflict_marker" for r in brief["relationships"])
    assert brief["ack"]["required"] is False
    assert brief["ack"]["state"] == "not_material"
    _validator().validate(brief)


def test_no_reservation_yet_is_honest_and_valid():
    """A change with no reservation recorded yet (first sync) → an honest, schema-valid Brief (clear/empty), never
    a crash."""
    brief = CB.coordination_brief({"repo": "acme/app", "branch": "main", "changes": []}, "PR-1",
                                  acting_head_sha="cafe123456")
    _validator().validate(brief)
    assert brief["state"] == "clear" and brief["relationships"] == []
    assert brief["acting_head_sha"] == "cafe123456"


def test_never_crashes_on_malformed_surface():
    """A non-dict / partial / malformed surface must yield an honest skeleton, not an exception (the never-crash
    contract render.py holds, held here too). A totally-empty surface legitimately lacks a repo/head, so we assert
    structural sanity + enum-boundedness rather than full schema validity (which requires a pinned head)."""
    for bad in (None, {}, {"changes": [None, "x", 3]}, {"changes": [{"change_id": "PR-2"}]}):
        brief = CB.coordination_brief(bad, "PR-2")
        assert brief["schema_version"] == CB.SCHEMA_VERSION and brief["kind"] == CB.KIND
        assert brief["state"] in CB.STATES
        assert brief["ack"]["state"] in CB.ACK_STATES
        assert isinstance(brief["relationships"], list)
    # WITH the caller supplying the head+repo the webhook always has, even a malformed surface is fully valid.
    brief = CB.coordination_brief({"repo": "acme/app", "changes": []}, "PR-2", acting_head_sha="cafef00d1234")
    _validator().validate(brief)


# ── PROVENANCE PINNING (additive fields) ─────────────────────────────────────────────────────────────────────
def test_provenance_fields_serialize_and_validate():
    """The additive provenance fields serialize into every fixture and keep the Brief schema-valid: repository is
    an object with slug + generation; freshness carries status / graph_source_sha / policy_version; there is an
    evaluated_against array."""
    v = _validator()
    for name, doc in _fixtures():
        b = doc["brief"]
        assert isinstance(b["repository"], dict), f"{name}: repository not promoted to an object"
        assert isinstance(b["repository"]["slug"], str) and b["repository"]["slug"], f"{name}: repository.slug"
        assert b["repository"]["generation"] is None or isinstance(b["repository"]["generation"], int), \
            f"{name}: repository.generation"
        fr = b["freshness"]
        assert fr["status"] in CB.FRESHNESS_STATUS, f"{name}: freshness.status not enum-bounded"
        assert fr["graph_source_sha"] is None or isinstance(fr["graph_source_sha"], str), f"{name}: graph_source_sha"
        assert fr["policy_version"] is None or isinstance(fr["policy_version"], str), f"{name}: policy_version"
        assert isinstance(b["evaluated_against"], list), f"{name}: evaluated_against missing"
        assert not sorted(v.iter_errors(b), key=lambda e: list(e.path)), f"{name}: schema errors with provenance"


def test_evaluated_against_pins_each_counterpart_head():
    """The MOST MATERIAL field: every OTHER change the Brief names is pinned to the exact head SHA the analysis saw
    (so a moved partner is detectable), the acting change itself is excluded, and entries are deterministically
    ordered by ref."""
    _, doc = next(d for d in _fixtures() if d[0] == "wait_in_line.json")
    b = doc["brief"]
    ea = b["evaluated_against"]
    refs = [e["ref"] for e in ea]
    assert refs == sorted(refs), "evaluated_against not deterministically ordered by ref"
    assert b["acting_change"] not in refs, "the acting change must not pin itself in evaluated_against"
    # every counterpart named in a relationship (related_change / ordered_changes) except self is pinned
    named = set()
    for r in b["relationships"]:
        if r["related_change"]:
            named.add(r["related_change"])
        named.update(r.get("ordered_changes") or [])
    named.discard(b["acting_change"])
    assert named == set(refs), f"evaluated_against {set(refs)} != counterparts named {named}"
    # each entry pins a real head SHA the input carried, with a bounded state + a matching PR number
    inp_heads = {c["change_id"]: c.get("head_sha")
                 for c in R._dicts(doc["input"]["impact"].get("changes")) if c.get("change_id")}
    for e in ea:
        assert set(e) == {"ref", "number", "head_sha", "state"}, f"unexpected keys in {e}"
        assert e["head_sha"] == inp_heads.get(e["ref"]), f"{e['ref']}: head_sha not pinned to the analyzed head"
        assert e["state"] in CB.STATES, f"{e['ref']}: state not enum-bounded"
        assert e["number"] == int(e["ref"].split("-")[1]), f"{e['ref']}: PR number mismatch"


def test_evaluated_against_is_content_free():
    """A counterpart pin carries ONLY a ref, a PR number, a head SHA, and a bounded state — never a body/diff/count.
    Driven over every fixture + a synthetic BR- counterpart (number null)."""
    impact = {"repo": "acme/app", "branch": "main", "clusters": [],
              "changes": [{"change_id": "PR-70", "head_sha": "aa70", "agent": "me", "label": "me PR-70",
                           "verdict": "warn", "is_draft": False, "paths": ["core/x.py"], "impact": [],
                           "contested_with": ["BR-featzz"], "serialize_behind": [], "queued_behind": [],
                           "queued_behind_paths": [], "collision_points": [], "merge_conflict_likely": False,
                           "conflict_points": [], "unknown_paths": [], "dampened_with": [], "depends_on_changing": [],
                           "shared_foundation": []},
                          {"change_id": "BR-featzz", "head_sha": "bb99feat", "agent": "zed", "label": "zed BR-featzz",
                           "verdict": "clear", "paths": ["core/x.py"]}]}
    brief = CB.coordination_brief(impact, "PR-70",
                                  ack={"label_present": False, "prior_hash": None, "prior_confirmed": True})
    ea = brief["evaluated_against"]
    assert [e["ref"] for e in ea] == ["BR-featzz"]
    only = ea[0]
    assert only["number"] is None and only["head_sha"] == "bb99feat" and only["state"] == "clear"
    for name, doc in _fixtures():
        for e in doc["brief"]["evaluated_against"]:
            assert set(e) <= {"ref", "number", "head_sha", "state"}, f"{name}: extra key in a counterpart pin"


def test_freshness_status_derives_from_head_matches_and_accepts_unavailable():
    """status is the explicit 4-value freshness verdict. When not supplied it is DERIVED from head_matches
    (True->current, False->stale, None->unknown); 'unavailable' — which head_matches cannot encode — passes through
    only when supplied. Deterministic, no clock/network."""
    base = {"repo": "acme/app", "branch": "main", "changes": []}
    assert CB.coordination_brief(base, "PR-1", acting_head_sha="cafe123456",
                                 head_matches=True)["freshness"]["status"] == "current"
    assert CB.coordination_brief(base, "PR-1", acting_head_sha="cafe123456",
                                 head_matches=False)["freshness"]["status"] == "stale"
    assert CB.coordination_brief(base, "PR-1", acting_head_sha="cafe123456",
                                 head_matches=None)["freshness"]["status"] == "unknown"
    b = CB.coordination_brief(base, "PR-1", acting_head_sha="cafe123456",
                              head_matches=None, freshness_status="unavailable")
    assert b["freshness"]["status"] == "unavailable" and b["freshness"]["head_matches"] is None
    _validator().validate(b)
    # an out-of-enum freshness_status is ignored (falls back to the derived value) — never leaks a bad enum
    b2 = CB.coordination_brief(base, "PR-1", acting_head_sha="cafe123456",
                               head_matches=True, freshness_status="bogus")
    assert b2["freshness"]["status"] == "current"


def test_brief_is_valid_and_deterministic_without_provenance_inputs():
    """A caller that supplies NO provenance values must still produce a valid, deterministic Brief: generation null,
    graph_source_sha null, policy_version null, status derived, evaluated_against present. And a Brief with the
    optional provenance fields stripped entirely must STILL validate (the fields are additive, not required)."""
    for name, doc in _fixtures():
        inp = doc["input"]
        kw = {k: v for k, v in inp["kwargs"].items()
              if k not in ("graph_source_sha", "repository_generation", "policy_version", "freshness_status")}
        b1 = CB.coordination_brief(inp["impact"], inp["change_ref"], **kw)
        b2 = CB.coordination_brief(inp["impact"], inp["change_ref"], **kw)
        assert json.dumps(b1, sort_keys=True) == json.dumps(b2, sort_keys=True), f"{name}: non-deterministic"
        assert b1["repository"]["generation"] is None, f"{name}: generation should be null without input"
        assert b1["freshness"]["graph_source_sha"] is None and b1["freshness"]["policy_version"] is None
        _validator().validate(b1)  # valid with the provenance values ABSENT (null)
        # now strip the optional provenance keys entirely — the Brief must remain schema-valid (additive, not required)
        stripped = json.loads(json.dumps(b1))
        del stripped["evaluated_against"]
        del stripped["repository"]["generation"]
        for k in ("status", "graph_source_sha", "policy_version"):
            stripped["freshness"].pop(k, None)
        _validator().validate(stripped)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = True
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  [FAIL] {t.__name__}: {e}")
    print("COORDINATION-BRIEF GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
