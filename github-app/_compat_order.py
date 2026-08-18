#!/usr/bin/env python3
"""Pure, offline landing-order solver over compatibility findings (compat lane PR-4).

This is PR-4 of the compatibility program (docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md §2 "derived at read
time" + §7 item 4; docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md §5 Milestone 2). Obligations are a
deterministic FUNCTION of findings — nothing here is persisted, flipped active/inactive, or
rendered. Like `_compat_rules`, the module is deliberately **self-contained**: plain dicts in,
one plain dict out; no DB, no network, no file I/O, no imports beyond stdlib/typing.

Input contract (a list of plain finding dicts, the PR-3 shadow-analysis vocabulary)
-----------------------------------------------------------------------------------
    {
      "producer_pr":   int | str,   # the PR whose NEW shape is the contract (PR-3: the acting PR)
      "consumer_pr":   int | str,   # the PR still coded against the OLD shape (PR-3: the sibling)
      "producer_sha":  str,         # producer verified head (carried metadata; ordering ignores it)
      "consumer_sha":  str,         # consumer verified head (carried metadata; ordering ignores it)
      "rule_id":       str,         # the _compat_rules rule that fired (fixed vocabulary)
      "compat_result": "breaking" | "risky",
    }

A finding is USABLE iff it is a dict, both PR ids are an int (bools excluded) or a non-empty str,
the two PR ids differ, and compat_result is exactly 'breaking' or 'risky'. Anything else — junk
type, missing field, self-edge — is SKIPPED and counted in the output's `skipped` field; the
solver NEVER raises. The shas and rule_id are tolerated-if-malformed metadata: they can never
disqualify a finding (ordering does not depend on them); a non-str rule_id simply contributes
nothing to the edge's `rule_ids`. PR identity is by exact value (int 11 and "PR-11" are two
distinct nodes — callers feed ONE id vocabulary).

Edge semantics (producer must land BEFORE consumer)
---------------------------------------------------
A finding says: if `consumer_pr` lands while still coded against the pre-`producer_pr` shape, the
combination diverges. So the resolvable-by-ordering reading is `producer_pr` FIRST, then the
consumer updates against the new main and re-runs (plan §6). Per DIRECTED pair all findings merge
into ONE edge:

    hard edge  — at least one 'breaking' finding: landing the consumer first is a DETECTED
                 contract break; the order is required.
    soft edge  — 'risky' findings only: the order is recommended, not proven breaking.

Both kinds constrain the topological order equally — the solver never silently violates a soft
edge to make a cluster linear (deciding to accept a risky direction is a human coordination call,
never a solver fabrication).

Cycle semantics ('cycle_cluster_coordination_required')
-------------------------------------------------------
Clusters are the connected components of the UNDIRECTED view of the surviving edges. A component
whose directed edges are acyclic gets a total landing `order` (Kahn; deterministic tie-break by
PR sort key — numeric where a number is extractable). A component containing ANY directed cycle
(hard, soft, or mixed — including a 2-cycle that is not revision_required below) is NOT
linearizable: it is classified `cycle_cluster_coordination_required` with its exact members and
NO `order` key — a fabricated order for a cycle is never emitted (plan §5: "design coordination
needed").

Revision semantics ('revision_required')
----------------------------------------
An unordered pair with a HARD edge in BOTH directions is broken WHICHEVER lands first — no
landing order can resolve it; at least one head must be revised. Such a pair is reported once in
`revision_required` (members tie-break-sorted) and ALL its edges (both directions, any kind) are
EXCLUDED from `edges` — keeping either direction would fabricate an order preference that does
not exist. A mixed pair (hard one way, soft the other) or a soft/soft mutual pair stays in the
graph as a genuine 2-cycle → cycle cluster: coordination CAN resolve those (accept the risky
direction); only mutual-breaking is beyond ordering entirely.

Output contract (a plain dict, JSON-serializable, content-free — PR ids, rule ids, counts only)
-----------------------------------------------------------------------------------------------
    {
      "edges": [                       # surviving directed obligations, sorted (producer, consumer)
        {"producer_pr": .., "consumer_pr": .., "kind": "hard" | "soft",
         "rule_ids": [str, ...]},      # sorted union of the contributing findings' rule ids
        ...
      ],
      "clusters": [                    # connected components, sorted by smallest member
        {"members": [..], "classification": "ordered", "order": [..]}      # acyclic
        | {"members": [..], "classification": "cycle_cluster_coordination_required"},  # cyclic
        ...
      ],
      "revision_required": [           # mutually-breaking pairs, sorted
        {"members": [a, b], "classification": "revision_required"}, ...
      ],
      "skipped": int,                  # malformed findings dropped (never raised on)
    }

Total determinism: the same multiset of findings, in ANY input order, produces the identical
output (every list is sorted by the total PR sort key; per-pair merges are order-insensitive).
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

BREAKING = "breaking"
RISKY = "risky"
ORDERED = "ordered"
CYCLE_CLUSTER = "cycle_cluster_coordination_required"
REVISION_REQUIRED = "revision_required"

# S3b plan-layer classifications.  The low-level order solver above remains the graph primitive; these
# names describe the actual work that follows an order, including the successor revision that a proven
# consumer-call mismatch requires.
ORDERED_WITHOUT_REVISION = "ordered_without_revision"
ORDERED_WITH_SUCCESSOR_REVISION = "ordered_with_successor_revision"
REVISION_REQUIRED_NO_SAFE_UNCHANGED_ORDER = "revision_required_no_safe_unchanged_order"
REBASE_REQUIRED = "rebase_required"
CYCLE_COORDINATION_REQUIRED = "cycle_coordination_required"
UNKNOWN_PLAN = "unknown"

_CONSUMER_CALL_MISMATCH = "consumer_call_mismatch"
_REBASE_CLASSES = frozenset(("rebase_needed", "rebase_needed_observation"))
_SAFE_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:,+/-")

# PR ids longer than this are junk by construction (a GitHub PR number / "PR-<n>" change id is tiny);
# refusing them keeps a hostile caller from making the sort key chew megabyte strings.
_MAX_PR_ID_LEN = 200


def _pr_sort_key(pr: Any) -> Tuple[int, int, str]:
    """TOTAL deterministic sort key for a PR id. Ints sort by value; strings by their trailing
    integer when one exists ("PR-2" before "PR-10" — numeric, not lexical), then by the full
    string; strings with no trailing number sort after all numbered ids, lexically. The tuple
    shape is identical for every input, so any mix of valid ids compares without raising. Keys
    are unique per node (the full string is the last component). Never raises."""
    if isinstance(pr, int) and not isinstance(pr, bool):
        return (0, pr, "")
    s = str(pr)
    i = len(s)
    while i > 0 and s[i - 1].isdecimal():
        i -= 1
    if i < len(s):
        return (0, int(s[i:]), s)
    return (1, 0, s)


def _valid_pr_id(v: Any) -> bool:
    """A usable PR id: an int (bool is an int subclass — excluded: True/False as a PR id is junk)
    or a non-empty, bounded str."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    return isinstance(v, str) and 0 < len(v) <= _MAX_PR_ID_LEN


def _valid_plan_pr_id(v: Any) -> bool:
    """The plan surface carries exact GitHub PR change ids, not arbitrary low-level node labels.  This
    stricter wall prevents a hostile/body-shaped string from being reflected through content-free output."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return v > 0
    return (isinstance(v, str) and v.startswith("PR-") and v[3:].isdecimal()
            and 3 < len(v) <= _MAX_PR_ID_LEN and int(v[3:]) > 0)


def solve_landing_order(findings: Any) -> Dict[str, Any]:
    """Solve the landing order for a batch of compat findings. See the module docstring for the
    full input/output contract. Pure + totally deterministic; NEVER raises — junk findings are
    skipped and counted, and a defect anywhere degrades to the empty plan rather than an
    exception (the shadow telemetry path must never be able to abort a customer event)."""
    try:
        return _solve(findings)
    except Exception:
        # Backstop only — every anticipated junk shape is handled in _solve. Deterministic: the
        # same pathological input takes this same path every time.
        n = len(findings) if isinstance(findings, (list, tuple)) else 0
        return {"edges": [], "clusters": [], "revision_required": [], "skipped": n}


def _solve(findings: Any) -> Dict[str, Any]:
    if not isinstance(findings, (list, tuple)):
        findings = []

    # ── 1. Normalize: merge findings into ONE edge per DIRECTED pair (hard beats soft) ──────────
    skipped = 0
    directed: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    for f in findings:
        if not isinstance(f, dict):
            skipped += 1
            continue
        prod, cons, cr = f.get("producer_pr"), f.get("consumer_pr"), f.get("compat_result")
        if not _valid_pr_id(prod) or not _valid_pr_id(cons) or prod == cons \
                or cr not in (BREAKING, RISKY):
            skipped += 1
            continue
        e = directed.setdefault((prod, cons), {"hard": False, "rule_ids": set()})
        if cr == BREAKING:
            e["hard"] = True
        rid = f.get("rule_id")
        if isinstance(rid, str) and rid:
            e["rule_ids"].add(rid)

    def _edge_key(pc: Tuple[Any, Any]) -> Tuple[Tuple[int, int, str], Tuple[int, int, str]]:
        return (_pr_sort_key(pc[0]), _pr_sort_key(pc[1]))

    # ── 2. Mutual-breaking pairs → revision_required; drop ALL their edges (both directions) ────
    revision: List[Dict[str, Any]] = []
    dropped: set = set()
    for (a, b) in sorted(directed, key=_edge_key):          # sorted walk ⇒ deterministic report order
        if (a, b) in dropped or (b, a) not in directed:
            continue
        if directed[(a, b)]["hard"] and directed[(b, a)]["hard"]:
            members = sorted((a, b), key=_pr_sort_key)
            revision.append({"members": members, "classification": REVISION_REQUIRED})
            dropped.add((a, b))
            dropped.add((b, a))

    # ── 3. Surviving edges, deterministically sorted ────────────────────────────────────────────
    edges: List[Dict[str, Any]] = []
    succ: Dict[Any, set] = {}
    undirected: Dict[Any, set] = {}
    for (p, c) in sorted(directed, key=_edge_key):
        if (p, c) in dropped:
            continue
        e = directed[(p, c)]
        edges.append({"producer_pr": p, "consumer_pr": c,
                      "kind": "hard" if e["hard"] else "soft",
                      "rule_ids": sorted(e["rule_ids"])})
        succ.setdefault(p, set()).add(c)
        succ.setdefault(c, set())
        undirected.setdefault(p, set()).add(c)
        undirected.setdefault(c, set()).add(p)

    # ── 4. Connected components (undirected view), each ordered or classified as a cycle ────────
    clusters: List[Dict[str, Any]] = []
    visited: set = set()
    for start in sorted(undirected, key=_pr_sort_key):      # smallest member found first ⇒ stable order
        if start in visited:
            continue
        comp: List[Any] = []
        stack = [start]
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            comp.append(n)
            stack.extend(sorted(undirected[n], key=_pr_sort_key, reverse=True))
        members = sorted(comp, key=_pr_sort_key)

        # Kahn topological sort over the component's directed edges; deterministic tie-break:
        # always take the smallest ready node by PR sort key.
        indeg = {n: 0 for n in members}
        for n in members:
            for m in succ.get(n, ()):
                indeg[m] += 1
        ready = sorted((n for n in members if indeg[n] == 0), key=_pr_sort_key)
        order: List[Any] = []
        while ready:
            n = ready.pop(0)                                # smallest ready (list kept sorted below)
            order.append(n)
            freed = []
            for m in succ.get(n, ()):
                indeg[m] -= 1
                if indeg[m] == 0:
                    freed.append(m)
            if freed:
                ready = sorted(ready + freed, key=_pr_sort_key)
        if len(order) == len(members):
            clusters.append({"members": members, "classification": ORDERED, "order": order})
        else:
            # A directed cycle: NEVER fabricate an order — name the members, demand coordination.
            clusters.append({"members": members, "classification": CYCLE_CLUSTER})

    return {"edges": edges, "clusters": clusters, "revision_required": revision, "skipped": skipped}


def _safe_token(value: Any, limit: int = 200) -> str:
    """Return one bounded content-free reason/class token, else ''.  Plan output must never echo an
    arbitrary input value (source snippets and parser junk can ride in ignored input fields)."""
    if not isinstance(value, str) or not (0 < len(value) <= limit):
        return ""
    return value if all(ch in _SAFE_TOKEN_CHARS for ch in value) else ""


def _evidence_parts(finding: Dict[str, Any]) -> Tuple[str, str]:
    """Normalize the typed plan evidence.  `evidence_class`/`reason_detail` are the preferred S3b
    vocabulary.  The two narrow fallbacks keep the plan usable with S3a in-memory findings and ledger
    detail codes while never deriving PR identity from a SHA or fingerprint."""
    evidence = _safe_token(finding.get("evidence_class"), 64)
    rule_id = _safe_token(finding.get("rule_id"), 200)
    fact_class = _safe_token(finding.get("fact_class"), 64)
    full_detail = _safe_token(finding.get("detail"), 200)
    reason_detail = _safe_token(finding.get("reason_detail"), 200)

    if not evidence and rule_id == _CONSUMER_CALL_MISMATCH:
        evidence = _CONSUMER_CALL_MISMATCH
    if not evidence and fact_class in _REBASE_CLASSES:
        evidence = fact_class
    if not evidence and full_detail == "rebase_needed":
        evidence = "rebase_needed"
    if evidence == _CONSUMER_CALL_MISMATCH and not reason_detail:
        prefix = _CONSUMER_CALL_MISMATCH + ":"
        if full_detail.startswith(prefix):
            reason_detail = _safe_token(full_detail[len(prefix):], 200)
    if not reason_detail and evidence != _CONSUMER_CALL_MISMATCH:
        reason_detail = rule_id or full_detail
    return evidence, reason_detail


def solve_landing_plan(findings: Any) -> Dict[str, Any]:
    """Build a deterministic, content-free WORK PLAN over compatibility findings.

    Unlike :func:`solve_landing_order`, this layer states whether a successor may land unchanged and emits
    hold/update/reanalyse/land steps.  It is still pure: no DB, network, mutable active bit, or rendering.
    Malformed input never raises; any skipped item makes the repository-level classification `unknown`
    while retaining bounded per-cluster diagnostic plans.
    """
    try:
        return _solve_plan(findings)
    except Exception:
        n = len(findings) if isinstance(findings, (list, tuple)) else 0
        return {
            "classification": UNKNOWN_PLAN,
            "members": [],
            "edges": [],
            "clusters": [],
            "steps": [],
            "skipped": n,
        }


def _solve_plan(findings: Any) -> Dict[str, Any]:
    input_is_sequence = isinstance(findings, (list, tuple))
    items = findings if input_is_sequence else []
    skipped = 0 if input_is_sequence else 1
    directed: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    low_level: List[Dict[str, Any]] = []

    for f in items:
        if not isinstance(f, dict):
            skipped += 1
            continue
        producer, consumer = f.get("producer_pr"), f.get("consumer_pr")
        if (not _valid_plan_pr_id(producer) or not _valid_plan_pr_id(consumer)
                or producer == consumer):
            skipped += 1
            continue
        result = f.get("compat_result")
        evidence, detail = _evidence_parts(f)
        rebase = evidence in _REBASE_CLASSES
        unknown = result == "unknown"
        if result not in (BREAKING, RISKY, "unknown") and not rebase:
            skipped += 1
            continue
        # A consumer mismatch without a bounded reason detail is not precise enough to plan as a proven
        # revision.  Preserve the exact PR identities but degrade this component to Unknown.
        if evidence == _CONSUMER_CALL_MISMATCH and not detail:
            unknown = True

        key = (producer, consumer)
        meta = directed.setdefault(key, {
            "hard": False,
            "unknown": False,
            "rebase": False,
            "requires_revision": False,
            "evidence_classes": set(),
            "reason_details": set(),
            "rule_ids": set(),
        })
        meta["hard"] = bool(meta["hard"] or result == BREAKING)
        meta["unknown"] = bool(meta["unknown"] or unknown)
        meta["rebase"] = bool(meta["rebase"] or rebase)
        meta["requires_revision"] = bool(
            meta["requires_revision"]
            or (evidence == _CONSUMER_CALL_MISMATCH and result == BREAKING and bool(detail))
        )
        if evidence:
            meta["evidence_classes"].add(evidence)
        if detail:
            meta["reason_details"].add(detail)
        rule_id = _safe_token(f.get("rule_id"), 200)
        if rule_id:
            meta["rule_ids"].add(rule_id)

        if result in (BREAKING, RISKY) and not rebase and not unknown:
            # The existing solver remains the graph/order primitive.  Only the bounded fields it consumes
            # are forwarded; SHAs, paths, fingerprints and arbitrary payload fields never reach output.
            low_level.append({
                "producer_pr": producer,
                "consumer_pr": consumer,
                "compat_result": result,
                "rule_id": rule_id,
            })

    low = solve_landing_order(low_level)
    mutual_pairs = {
        frozenset(r.get("members") or [])
        for r in (low.get("revision_required") or [])
        if isinstance(r, dict) and len(r.get("members") or []) == 2
    }
    low_cycles = [
        frozenset(c.get("members") or [])
        for c in (low.get("clusters") or [])
        if isinstance(c, dict) and c.get("classification") == CYCLE_CLUSTER
    ]
    low_orders = {
        frozenset(c.get("members") or []): list(c.get("order") or [])
        for c in (low.get("clusters") or [])
        if isinstance(c, dict) and isinstance(c.get("order"), list)
    }

    def pair_key(pair: Tuple[Any, Any]):
        return (_pr_sort_key(pair[0]), _pr_sort_key(pair[1]))

    edges: List[Dict[str, Any]] = []
    undirected: Dict[Any, set] = {}
    for (producer, consumer) in sorted(directed, key=pair_key):
        meta = directed[(producer, consumer)]
        classes = sorted(meta["evidence_classes"])
        details = sorted(meta["reason_details"])
        if meta["unknown"]:
            kind = "unknown"
            action = "hold_pending_analysis"
        elif meta["rebase"]:
            kind = "soft"
            action = "rebase_successor"
        else:
            kind = "hard" if meta["hard"] else "soft"
            action = ("update_consumer_after_predecessor" if meta["requires_revision"]
                      else "land_after_predecessor")
        edge = {
            "producer_pr": producer,
            "consumer_pr": consumer,
            "kind": kind,
            "evidence_class": classes[0] if len(classes) == 1 else ("mixed" if classes else "compatibility_finding"),
            "reason_detail": details[0] if len(details) == 1 else ("mixed" if details else "unspecified"),
            "requires_successor_revision": bool(meta["requires_revision"]),
            "required_action": action,
            "merge_eligible_unchanged": bool(not meta["requires_revision"] and not meta["rebase"]
                                             and not meta["unknown"]),
            "reanalysis_required": bool(meta["requires_revision"] or meta["rebase"] or meta["unknown"]),
        }
        if len(classes) > 1:
            edge["evidence_classes"] = classes
        if len(details) > 1:
            edge["reason_details"] = details
        edges.append(edge)
        undirected.setdefault(producer, set()).add(consumer)
        undirected.setdefault(consumer, set()).add(producer)

    clusters: List[Dict[str, Any]] = []
    visited: set = set()
    for start in sorted(undirected, key=_pr_sort_key):
        if start in visited:
            continue
        stack = [start]
        component: List[Any] = []
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            component.append(node)
            stack.extend(sorted(undirected[node], key=_pr_sort_key, reverse=True))
        members = sorted(component, key=_pr_sort_key)
        member_set = frozenset(members)
        component_meta = [
            (pair, meta) for pair, meta in directed.items()
            if pair[0] in member_set and pair[1] in member_set
        ]
        has_unknown = any(meta["unknown"] for _, meta in component_meta)
        has_mutual = any(pair <= member_set for pair in mutual_pairs)
        has_cycle = any(cycle <= member_set for cycle in low_cycles)
        has_rebase = any(meta["rebase"] for _, meta in component_meta)
        has_revision = any(meta["requires_revision"] for _, meta in component_meta)

        if has_unknown:
            classification = UNKNOWN_PLAN
            steps = [{"action": "hold_pending_analysis", "prs": members}]
        elif has_mutual:
            classification = REVISION_REQUIRED_NO_SAFE_UNCHANGED_ORDER
            steps = [{"action": "coordinate_revision", "prs": members}]
        elif has_cycle:
            classification = CYCLE_COORDINATION_REQUIRED
            steps = [{"action": "coordinate_cycle", "prs": members}]
        elif has_rebase:
            classification = REBASE_REQUIRED
            steps = _rebase_steps(members, component_meta)
        else:
            classification = (ORDERED_WITH_SUCCESSOR_REVISION if has_revision
                              else ORDERED_WITHOUT_REVISION)
            order = low_orders.get(member_set, [])
            steps = _ordered_plan_steps(order, component_meta)
        cluster = {"classification": classification, "members": members, "steps": steps}
        if classification in (ORDERED_WITHOUT_REVISION, ORDERED_WITH_SUCCESSOR_REVISION):
            cluster["order"] = low_orders.get(member_set, [])
        clusters.append(cluster)

    members = sorted(undirected, key=_pr_sort_key)
    classifications = {c["classification"] for c in clusters}
    precedence = (
        UNKNOWN_PLAN,
        REVISION_REQUIRED_NO_SAFE_UNCHANGED_ORDER,
        CYCLE_COORDINATION_REQUIRED,
        REBASE_REQUIRED,
        ORDERED_WITH_SUCCESSOR_REVISION,
        ORDERED_WITHOUT_REVISION,
    )
    if skipped:
        classification = UNKNOWN_PLAN
    elif not clusters:
        classification = ORDERED_WITHOUT_REVISION if input_is_sequence else UNKNOWN_PLAN
    else:
        classification = next(name for name in precedence if name in classifications)
    steps = [step for cluster in clusters for step in cluster["steps"]]
    return {
        "classification": classification,
        "members": members,
        "edges": edges,
        "clusters": clusters,
        "steps": steps,
        "skipped": skipped,
    }


def _after_value(predecessors: List[Any]) -> Any:
    return predecessors[0] if len(predecessors) == 1 else predecessors


def _ordered_plan_steps(order: List[Any], component_meta: List[Tuple[Tuple[Any, Any], Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not order:
        return []
    incoming: Dict[Any, List[Tuple[Any, Dict[str, Any]]]] = {node: [] for node in order}
    for (producer, consumer), meta in component_meta:
        if not meta["rebase"] and not meta["unknown"]:
            incoming.setdefault(consumer, []).append((producer, meta))
    for node in incoming:
        incoming[node].sort(key=lambda item: _pr_sort_key(item[0]))

    steps: List[Dict[str, Any]] = []
    for node in order:
        if any(meta["requires_revision"] for _, meta in incoming.get(node, [])):
            steps.append({"action": "hold", "pr": node})
    for node in order:
        predecessors = [producer for producer, _ in incoming.get(node, [])]
        if not predecessors:
            steps.append({"action": "land_predecessor", "pr": node})
            continue
        revision = any(meta["requires_revision"] for _, meta in incoming[node])
        if revision:
            steps.append({
                "action": "update_successor",
                "pr": node,
                "after": _after_value(predecessors),
                "revision_required": True,
                "reanalysis_required": True,
            })
            steps.append({"action": "land_after_clear", "pr": node})
        else:
            steps.append({
                "action": "land_successor",
                "pr": node,
                "after": _after_value(predecessors),
                "revision_required": False,
                "reanalysis_required": False,
            })
    return steps


def _rebase_steps(members: List[Any], component_meta: List[Tuple[Tuple[Any, Any], Dict[str, Any]]]) -> List[Dict[str, Any]]:
    predecessors: Dict[Any, set] = {}
    for (producer, consumer), meta in component_meta:
        if meta["rebase"]:
            predecessors.setdefault(consumer, set()).add(producer)
    steps: List[Dict[str, Any]] = []
    for consumer in sorted(predecessors, key=_pr_sort_key):
        steps.append({"action": "hold", "pr": consumer})
    for consumer in sorted(predecessors, key=_pr_sort_key):
        after = sorted(predecessors[consumer], key=_pr_sort_key)
        steps.append({
            "action": "rebase_successor",
            "pr": consumer,
            "after": _after_value(after),
            "revision_required": True,
            "reanalysis_required": True,
        })
        steps.append({"action": "land_after_clear", "pr": consumer})
    return steps


__all__ = [
    "solve_landing_order", "solve_landing_plan",
    "ORDERED", "CYCLE_CLUSTER", "REVISION_REQUIRED",
    "ORDERED_WITHOUT_REVISION", "ORDERED_WITH_SUCCESSOR_REVISION",
    "REVISION_REQUIRED_NO_SAFE_UNCHANGED_ORDER", "REBASE_REQUIRED",
    "CYCLE_COORDINATION_REQUIRED", "UNKNOWN_PLAN",
]
