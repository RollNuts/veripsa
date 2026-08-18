#!/usr/bin/env python3
"""Landing-order + S3b landing-plan gate — PURE, OFFLINE, deterministic and content-free.

Proves on the real `_compat_order.solve_landing_order` (no DB, no network, no file I/O):

  (1) A→B: one breaking finding ⇒ one hard edge, one cluster ordered [A, B].
  (2) A→B→C chain ⇒ one cluster ordered [A, B, C].
  (3) Two independent pairs ⇒ two clusters, deterministically sorted.
  (4) 3-cycle A→B→C→A ⇒ ONE cluster classified 'cycle_cluster_coordination_required' with the
      EXACT members and NO 'order' key — a fabricated order for a cycle is never emitted.
  (5) Both-directions-BREAKING pair ⇒ 'revision_required' (members reported), edges EMPTY,
      clusters EMPTY — unresolvable by ordering is excluded from the graph, not linearized;
      an extra ordinary edge on one member still orders normally.
  (6) DETERMINISM: repeated runs and shuffled input orders produce byte-identical output;
      tie-break is by PR NUMBER (PR-2 before PR-10 — numeric, never lexical).
  (7) Junk findings (non-dict, missing/invalid PR ids, self-edge, bad compat_result) are
      skipped + counted — never a crash, and junk around a valid finding leaves it intact.
  (8) Soft-edge-only (risky) cluster still orders; mixed hard+soft chain orders across both.
  (9) Mixed-direction pair (hard one way, SOFT the other) is a genuine 2-cycle ⇒ cycle
      cluster, NOT revision_required (only mutual-breaking is beyond ordering).

Run:  python3 tests/test_compat_order.py
"""
from __future__ import annotations

import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import _compat_order as O  # noqa: E402

FAIL = 0


def chk(cond: bool, label: str) -> None:
    global FAIL
    if cond:
        print("  [PASS]", label)
    else:
        FAIL += 1
        print("  [FAIL]", label)


def finding(prod, cons, result="breaking", rule="required_arg_added", psha="a1" * 20, csha="b1" * 20):
    return {"producer_pr": prod, "consumer_pr": cons, "producer_sha": psha,
            "consumer_sha": csha, "rule_id": rule, "compat_result": result}


def plan_finding(prod, cons, detail="positional_shortfall", result="breaking",
                 evidence="consumer_call_mismatch"):
    return {
        "producer_pr": prod,
        "consumer_pr": cons,
        "compat_result": result,
        "evidence_class": evidence,
        "reason_detail": detail,
        "rule_id": evidence,
        # Carried input metadata must never be echoed by the pure content-free plan.
        "producer_sha": "a1" * 20,
        "consumer_sha": "b1" * 20,
    }


def canon(out) -> str:
    return json.dumps(out, sort_keys=True, default=str)


EMPTY = {"edges": [], "clusters": [], "revision_required": [], "skipped": 0}


def main() -> int:
    solve = O.solve_landing_order

    # ── (1) A→B simple ─────────────────────────────────────────────────────────────────────────
    out = solve([finding("PR-1", "PR-2")])
    chk(out["edges"] == [{"producer_pr": "PR-1", "consumer_pr": "PR-2", "kind": "hard",
                          "rule_ids": ["required_arg_added"]}],
        "A→B: one breaking finding ⇒ one hard edge producer→consumer")
    chk(out["clusters"] == [{"members": ["PR-1", "PR-2"], "classification": "ordered",
                             "order": ["PR-1", "PR-2"]}],
        "A→B: one cluster, ordered [A, B]")
    chk(out["revision_required"] == [] and out["skipped"] == 0,
        "A→B: nothing revision_required, nothing skipped")

    # ── (2) chain A→B→C ────────────────────────────────────────────────────────────────────────
    out = solve([finding("PR-2", "PR-3"), finding("PR-1", "PR-2")])
    chk(out["clusters"] == [{"members": ["PR-1", "PR-2", "PR-3"], "classification": "ordered",
                             "order": ["PR-1", "PR-2", "PR-3"]}],
        "chain: A→B plus B→C ⇒ one cluster ordered [A, B, C] (input order irrelevant)")

    # ── (3) two independent clusters ───────────────────────────────────────────────────────────
    out = solve([finding("PR-7", "PR-8"), finding("PR-1", "PR-2")])
    chk([c["members"] for c in out["clusters"]] == [["PR-1", "PR-2"], ["PR-7", "PR-8"]]
        and all(c["classification"] == "ordered" for c in out["clusters"]),
        "independent pairs: two clusters, each ordered, sorted by smallest member")

    # ── (4) 3-cycle ⇒ coordination required, exact members, NO fabricated order ────────────────
    cyc = [finding("PR-1", "PR-2"), finding("PR-2", "PR-3"), finding("PR-3", "PR-1")]
    out = solve(cyc)
    chk(out["clusters"] == [{"members": ["PR-1", "PR-2", "PR-3"],
                             "classification": "cycle_cluster_coordination_required"}],
        "3-cycle: ONE cluster classified cycle_cluster_coordination_required with exact members")
    chk(all("order" not in c for c in out["clusters"]),
        "3-cycle: no 'order' key anywhere — a cycle never gets a fabricated order")
    chk(len(out["edges"]) == 3 and out["revision_required"] == [],
        "3-cycle: the edges themselves are still reported (they are real obligations)")

    # ── (5) both-directions-breaking pair ⇒ revision_required, excluded from the graph ─────────
    out = solve([finding("PR-4", "PR-5"), finding("PR-5", "PR-4", rule="exported_symbol_removed")])
    chk(out["revision_required"] == [{"members": ["PR-4", "PR-5"],
                                      "classification": "revision_required"}],
        "mutual breaking: the pair is classified revision_required (members reported once)")
    chk(out["edges"] == [] and out["clusters"] == [],
        "mutual breaking: BOTH directions excluded from edges ⇒ no cluster is formed from them")
    # a mutual-breaking pair does not swallow an ordinary edge one member also has
    out = solve([finding("PR-4", "PR-5"), finding("PR-5", "PR-4"), finding("PR-4", "PR-9")])
    chk(out["revision_required"] == [{"members": ["PR-4", "PR-5"],
                                      "classification": "revision_required"}]
        and [e["consumer_pr"] for e in out["edges"]] == ["PR-9"]
        and out["clusters"] == [{"members": ["PR-4", "PR-9"], "classification": "ordered",
                                 "order": ["PR-4", "PR-9"]}],
        "mutual breaking + ordinary edge: the pair is reported, the ordinary edge still orders")

    # ── (6) determinism: repeat + shuffle + numeric tie-break ──────────────────────────────────
    # a corpus that exercises every classification at once: an ordered chain, a mixed 3-cycle,
    # a mutual-breaking pair, and a star with a numeric tie-break
    corpus = [finding("PR-100", "PR-101"), finding("PR-101", "PR-102", "risky"),
              finding("PR-201", "PR-202"), finding("PR-202", "PR-203"),
              finding("PR-203", "PR-201", "risky"),
              finding("PR-301", "PR-302"), finding("PR-302", "PR-301"),
              finding("PR-401", "PR-2"), finding("PR-401", "PR-10", "risky")]
    base = solve(corpus)
    stable = all(canon(solve(corpus)) == canon(base) for _ in range(5))
    rng = random.Random(20260717)
    shuffled_ok = True
    for _ in range(25):
        s = list(corpus)
        rng.shuffle(s)
        if canon(solve(s)) != canon(base):
            shuffled_ok = False
            break
    chk(stable, "determinism: repeated runs on the same input are byte-identical")
    chk(shuffled_ok, "determinism: 25 shuffled input orders all produce byte-identical output")
    star = next(c for c in base["clusters"] if "PR-401" in c["members"])
    chk(star["order"] == ["PR-401", "PR-2", "PR-10"],
        "tie-break: ready nodes drain by PR NUMBER (PR-2 before PR-10 — numeric, not lexical)")
    chk([c.get("classification") for c in base["clusters"]] ==
        ["ordered", "ordered", "cycle_cluster_coordination_required"]
        or sorted(c["classification"] for c in base["clusters"]) ==
        ["cycle_cluster_coordination_required", "ordered", "ordered"],
        "mixed corpus: the chain + star order, the mixed 2/3-cycle clusters classify as cycles")
    chk(base["revision_required"] == [{"members": ["PR-301", "PR-302"],
                                       "classification": "revision_required"}],
        "mixed corpus: exactly the mutual-breaking pair is revision_required")

    # ── (7) junk findings: skipped + counted, never a crash, valid survivors intact ────────────
    junk = [None, 42, "PR-1", [],                                   # not dicts
            {}, {"producer_pr": "PR-1"},                             # missing fields
            finding("PR-1", "PR-1"),                                 # self-edge
            finding("", "PR-2"), finding("PR-1", None),              # invalid ids
            finding(True, "PR-2"),                                   # bool id is junk
            finding("PR-1", "PR-2", result="compatible"),            # non-orderable result
            finding("PR-1", "PR-2", result="banana"),                # junk result
            {"producer_pr": "PR-1", "consumer_pr": "PR-2", "compat_result": "breaking",
             "rule_id": 99, "producer_sha": None, "consumer_sha": []}]   # junk metadata: USABLE
    out = solve(junk + [finding("PR-8", "PR-9", "risky", rule="variadic_removed")])
    chk(out["skipped"] == 12,
        "junk: 12 malformed findings skipped + counted (metadata-junk one is NOT skipped)")
    chk({(e["producer_pr"], e["consumer_pr"], e["kind"]) for e in out["edges"]} ==
        {("PR-1", "PR-2", "hard"), ("PR-8", "PR-9", "soft")},
        "junk: the valid findings still produce their edges (junk rule_id degrades to no rule id)")
    chk(solve(None) == EMPTY and solve("junk") == EMPTY and solve({}) == EMPTY
        and solve([]) == EMPTY,
        "junk: a non-list input degrades to the empty plan — never a crash")

    # ── (8) soft-only orders; mixed hard+soft orders ───────────────────────────────────────────
    out = solve([finding("PR-1", "PR-2", "risky", rule="required_arg_removed"),
                 finding("PR-2", "PR-3", "risky", rule="variadic_removed")])
    chk(out["clusters"] == [{"members": ["PR-1", "PR-2", "PR-3"], "classification": "ordered",
                             "order": ["PR-1", "PR-2", "PR-3"]}]
        and all(e["kind"] == "soft" for e in out["edges"]),
        "soft-only: risky edges alone still produce a full deterministic order")
    out = solve([finding("PR-1", "PR-2"), finding("PR-2", "PR-3", "risky")])
    chk([e["kind"] for e in out["edges"]] == ["hard", "soft"]
        and out["clusters"][0]["order"] == ["PR-1", "PR-2", "PR-3"],
        "mixed: hard + soft edges order together in one cluster")
    # per-pair merge: breaking + risky findings on the SAME directed pair ⇒ ONE hard edge
    out = solve([finding("PR-1", "PR-2", "risky", rule="required_arg_removed"),
                 finding("PR-1", "PR-2", "breaking", rule="required_arg_added")])
    chk(out["edges"] == [{"producer_pr": "PR-1", "consumer_pr": "PR-2", "kind": "hard",
                          "rule_ids": ["required_arg_added", "required_arg_removed"]}],
        "merge: same-direction findings collapse to ONE edge; hard beats soft; rule ids unioned")

    # ── (9) hard-one-way + soft-back is a CYCLE cluster, not revision_required ─────────────────
    out = solve([finding("PR-1", "PR-2", "breaking"), finding("PR-2", "PR-1", "risky")])
    chk(out["clusters"] == [{"members": ["PR-1", "PR-2"],
                             "classification": "cycle_cluster_coordination_required"}]
        and out["revision_required"] == [],
        "hard+soft 2-cycle: coordination_required (a human may accept the risky direction) — "
        "revision_required is reserved for mutual BREAKING")

    # int PR ids work exactly like strings (identity is by exact value)
    out = solve([finding(2, 10), finding(10, 30, "risky")])
    chk(out["clusters"] == [{"members": [2, 10, 30], "classification": "ordered",
                             "order": [2, 10, 30]}],
        "int ids: numeric identities order numerically (2 → 10 → 30)")

    # ── S3b landing PLAN: one proven mismatch requires successor revision ─────────────────────
    plan = O.solve_landing_plan([plan_finding("PR-7", "PR-8")])
    expected_edge = {
        "producer_pr": "PR-7",
        "consumer_pr": "PR-8",
        "kind": "hard",
        "evidence_class": "consumer_call_mismatch",
        "reason_detail": "positional_shortfall",
        "requires_successor_revision": True,
        "required_action": "update_consumer_after_predecessor",
        "merge_eligible_unchanged": False,
        "reanalysis_required": True,
    }
    expected_steps = [
        {"action": "hold", "pr": "PR-8"},
        {"action": "land_predecessor", "pr": "PR-7"},
        {"action": "update_successor", "pr": "PR-8", "after": "PR-7",
         "revision_required": True, "reanalysis_required": True},
        {"action": "land_after_clear", "pr": "PR-8"},
    ]
    chk(plan["classification"] == O.ORDERED_WITH_SUCCESSOR_REVISION
        and plan["members"] == ["PR-7", "PR-8"]
        and plan["edges"] == [expected_edge]
        and plan["steps"] == expected_steps,
        "plan single mismatch: hold → predecessor land → successor update/reanalysis → land after clear")
    chk(plan["edges"][0]["merge_eligible_unchanged"] is False,
        "plan single mismatch: consumer is NEVER represented as merge-eligible unchanged")

    # Every shipped mismatch detail has the same revision semantics; the detail remains queryable.
    for detail in ("missing_required_kwonly", "callee_removed"):
        detail_plan = O.solve_landing_plan([plan_finding("PR-7", "PR-8", detail)])
        chk(detail_plan["classification"] == O.ORDERED_WITH_SUCCESSOR_REVISION
            and detail_plan["edges"][0]["reason_detail"] == detail
            and detail_plan["edges"][0]["requires_successor_revision"] is True,
            f"plan {detail}: evidence detail preserved + successor revision required")

    # A generic order-only edge can land unchanged after its predecessor; this is distinct from mismatch.
    unchanged = O.solve_landing_plan([
        plan_finding("PR-1", "PR-2", "definition_order", evidence="order_only")])
    chk(unchanged["classification"] == O.ORDERED_WITHOUT_REVISION
        and unchanged["edges"][0]["merge_eligible_unchanged"] is True
        and unchanged["steps"][-1]["action"] == "land_successor",
        "plan order-only: ordered_without_revision is explicit and lands successor unchanged after predecessor")

    # N-PR chain is an executable work sequence, not merely [A,B,C].
    chain_plan = O.solve_landing_plan([
        plan_finding("PR-2", "PR-3"), plan_finding("PR-1", "PR-2")])
    chain_actions = [(s["action"], s.get("pr")) for s in chain_plan["steps"]]
    chk(chain_plan["classification"] == O.ORDERED_WITH_SUCCESSOR_REVISION
        and chain_plan["clusters"][0]["order"] == ["PR-1", "PR-2", "PR-3"]
        and chain_actions == [
            ("hold", "PR-2"), ("hold", "PR-3"),
            ("land_predecessor", "PR-1"),
            ("update_successor", "PR-2"), ("land_after_clear", "PR-2"),
            ("update_successor", "PR-3"), ("land_after_clear", "PR-3")],
        "plan chain: A lands, B updates/reanalyses+lands, then C updates/reanalyses+lands")

    independent = O.solve_landing_plan([
        plan_finding("PR-7", "PR-8"), plan_finding("PR-1", "PR-2")])
    chk([c["members"] for c in independent["clusters"]]
        == [["PR-1", "PR-2"], ["PR-7", "PR-8"]],
        "plan independent clusters: two deterministic, non-conflated work plans")

    # A cycle never gets an order or executable landing action.
    cycle_plan = O.solve_landing_plan([
        plan_finding("PR-1", "PR-2"), plan_finding("PR-2", "PR-3"),
        plan_finding("PR-3", "PR-1")])
    landing_actions = {"land_predecessor", "land_successor", "land_after_clear"}
    chk(cycle_plan["classification"] == O.CYCLE_COORDINATION_REQUIRED
        and all("order" not in c for c in cycle_plan["clusters"])
        and not any(s.get("action") in landing_actions for s in cycle_plan["steps"]),
        "plan cycle: cycle_coordination_required with no fabricated order or landing step")

    mutual = O.solve_landing_plan([
        plan_finding("PR-4", "PR-5"), plan_finding("PR-5", "PR-4", "callee_removed")])
    chk(mutual["classification"] == O.REVISION_REQUIRED_NO_SAFE_UNCHANGED_ORDER
        and mutual["steps"] == [{"action": "coordinate_revision", "prs": ["PR-4", "PR-5"]}]
        and not any(s.get("action") in landing_actions for s in mutual["steps"]),
        "plan mutual hard: no unchanged order is fabricated; coordinate revision instead")

    hard_soft = O.solve_landing_plan([
        plan_finding("PR-1", "PR-2", "advisory", "risky", "order_only"),
        plan_finding("PR-1", "PR-2", "positional_shortfall", "breaking")])
    chk(len(hard_soft["edges"]) == 1 and hard_soft["edges"][0]["kind"] == "hard"
        and hard_soft["edges"][0]["requires_successor_revision"] is True
        and hard_soft["classification"] == O.ORDERED_WITH_SUCCESSOR_REVISION,
        "plan hard+soft same edge: one hard edge, revision semantics dominate")

    rebase = O.solve_landing_plan([
        plan_finding("PR-1", "PR-2", "rebase_needed", "risky", "rebase_needed_observation")])
    chk(rebase["classification"] == O.REBASE_REQUIRED
        and [s["action"] for s in rebase["steps"]] == ["hold", "rebase_successor", "land_after_clear"]
        and rebase["edges"][0]["merge_eligible_unchanged"] is False,
        "plan rebase: hold + rebase + reanalysis required; never unchanged-eligible")

    explicit_unknown = O.solve_landing_plan([
        plan_finding("PR-1", "PR-2", "analysis_incomplete", "unknown", "unknown")])
    chk(explicit_unknown["classification"] == O.UNKNOWN_PLAN
        and explicit_unknown["steps"] == [{"action": "hold_pending_analysis", "prs": ["PR-1", "PR-2"]}],
        "plan unknown: incomplete evidence holds traffic and never becomes Clear")

    malformed_plan = O.solve_landing_plan([
        None, {"producer_pr": "PR-1"}, plan_finding("PR-7", "PR-8")])
    chk(malformed_plan["classification"] == O.UNKNOWN_PLAN and malformed_plan["skipped"] == 2
        and malformed_plan["clusters"][0]["classification"] == O.ORDERED_WITH_SUCCESSOR_REVISION,
        "plan malformed input: never crashes, reports Unknown globally, retains the valid bounded cluster")
    chk(O.solve_landing_plan(None)["classification"] == O.UNKNOWN_PLAN
        and O.solve_landing_plan({"junk": True})["classification"] == O.UNKNOWN_PLAN,
        "plan non-sequence input: deterministic Unknown, never a crash")

    # Shuffle determinism + stable JSON serialization across mixed independent clusters.
    plan_corpus = [
        plan_finding("PR-10", "PR-11", "callee_removed"),
        plan_finding("PR-2", "PR-3", "missing_required_kwonly"),
        plan_finding("PR-1", "PR-2"),
    ]
    plan_base = canon(O.solve_landing_plan(plan_corpus))
    plan_shuffle_ok = True
    for _ in range(25):
        shuffled = list(plan_corpus)
        rng.shuffle(shuffled)
        if canon(O.solve_landing_plan(shuffled)) != plan_base:
            plan_shuffle_ok = False
            break
    chk(plan_shuffle_ok, "plan determinism: 25 input shuffles produce byte-identical JSON")

    # Content-free: arbitrary bodies/defaults/parser junk are ignored, not echoed into any plan surface.
    poison = "SENTINEL default=(private_value) parser junk call expression body"
    poisoned = plan_finding("PR-7", "PR-8")
    poisoned.update(source_body=poison, diff_body=poison, parser_exception=poison,
                    argument_value=poison, annotation_source=poison)
    chk(poison not in canon(O.solve_landing_plan([poisoned])),
        "plan content-free: source/diff/default/argument/parser sentinels appear nowhere in JSON output")
    poisoned_id = plan_finding(poison, "PR-8")
    chk(poison not in canon(O.solve_landing_plan([poisoned_id])),
        "plan content-free: body-shaped pseudo-PR identity is refused and never reflected")

    print(f"\n-- compat order solver assertions: {FAIL} failure(s) --")
    if FAIL:
        print("COMPAT ORDER SOLVER GATE: FAIL")
        return 1
    print("COMPAT ORDER SOLVER GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
