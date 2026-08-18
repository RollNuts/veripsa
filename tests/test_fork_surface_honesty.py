#!/usr/bin/env python3
"""FORK-SURFACE HONESTY gate — the CANONICAL jargon/overclaim/moat lock, EXTENDED to the fork-REDACTED surface.

WHY THIS GATE EXISTS (the coverage gap it closes):
  tests/test_no_jargon_leak.py owns the AUTHORITATIVE contract for customer-facing text — the canonical internal
  DENYLIST (role names, internal fn ids, DB/design terms, the Japanese design prose), the OVERCLAIM_PHRASES
  (records-not-correctness: never "prevents"/"guarantees"/"safe to merge"/"will release"), and the two MOAT
  invariants (no raw graph/file counts cross to a customer). BUT it drives ONLY the NON-fork render path:
  every render_pr_check call in that gate omits `is_fork`, so it defaults to False. The fork-REDACTED surface —
  `render_pr_check(..., is_fork=True)` plus the fork pause-ack overlay — is the HIGHEST-stakes customer text
  Veripsa posts: a fork PR's comment lands on the BASE-repo conversation that the EXTERNAL contributor can read,
  so a leak there is a cross-contributor leak = moat death (the base repo's OTHER in-flight PRs / paths / logins
  exposed to an outsider). Yet NO gate scanned that surface through the canonical denylist + overclaim list:
  test_render_interaction_honesty drives the fork branch but asserts only self-contradiction + never-crash;
  test_fork_pr_path / test_fork_neighbor_refresh_leak assert the cross-PR-ref redaction but not the jargon /
  overclaim contract. So the fork copy could drift in a raw role name or a "prevents conflicts" guarantee and
  every gate would stay green.

WHAT THIS GATE LOCKS (purely ADDITIVE — it widens the existing lock to a surface it never reached):
  (1) JARGON + OVERCLAIM, on the SAME canonical lists: import _scan / _scan_overclaim / DENYLIST / OVERCLAIM_
      PHRASES from test_no_jargon_leak (NOT a hand-rolled subset — so this lock can never drift from the
      authoritative one) and run them over EVERY fork-redacted surface: the fork title + summary + comment for
      EACH verdict (clear / warn / serialize / serialize_soft / unknown), the fork clear-that-still-posts cases
      (lane holder, shared foundation, truncated), and the fork pause-ack overlay (paused / acknowledged /
      stale_reack — though a fork is non-material so it returns the notice unchanged; we assert that too).
  (2) CROSS-CONTRIBUTOR NON-LEAK (the moat): drive a fork PR whose engine row is FULLY populated with leak
      material — other PR refs, maintainer logins, base-repo paths/symbols, a cluster order, a hub, conflict
      points — and assert NONE of those base-repo identities reaches the external-contributor-readable comment.
      This is the redaction's whole reason to exist; this gate makes its breach a build failure.
  (3) FORK NEVER PAUSES (precision + UX): a fork check posted on the fork head sha does not stick on the base
      repo, so an action_required pause there would be a confusing banner with no real gate, AND a fork
      contributor cannot add the base repo's ack label. apply_pause_ack(..., is_fork=True) must return the
      notice UNCHANGED (ack_state 'not_material', conclusion not action_required) — and its copy stays clean.

PURE + OFFLINE: render_pr_check / apply_pause_ack are stateless functions over a main_impact_surface-shaped
dict, so this needs no Postgres, no network, no deploy (mirrors test_no_jargon_leak / test_render_interaction
_honesty). It REUSES the canonical scanners so the contract is single-sourced.

Run:  python3 tests/test_fork_surface_honesty.py     (no DB needed)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import render as R                  # noqa: E402
# Single-source the contract: the SAME canonical denylist + overclaim scanners the non-fork gate uses, so the
# fork lock can never silently diverge from the authoritative one (add a term there → it guards here too).
import test_no_jargon_leak as J     # noqa: E402


BASE = {"repo": "private/base", "branch": "main"}


def _fork_scenarios():
    """A fork-PR engine row per verdict, each populated with the cross-PR / base-repo material a fork comment
    must REDACT (partner refs, maintainer logins, base-repo paths/symbols), so the redacted output is the thing
    scanned. The label/agent carry a maintainer-shaped login; paths carry a SECRET-marked base-repo path."""
    def row(verdict, **extra):
        d = {"change_id": "PR-99", "label": "attacker PR-99", "agent": "attacker",
             "verdict": verdict, "paths": ["SECRET/internal_module.py"]}
        d.update(extra)
        return {**BASE, "changes": [d]}

    serialize_extra = {
        "serialize_behind": ["maintainer_bob PR-42"],
        "collision_points": [{"behind": "maintainer_bob", "path": "SECRET/internal_module.py",
                              "symbol": "process_payment", "line_lo": 10, "line_hi": 40}],
        "conflict_points": [{"path": "SECRET/internal_module.py", "line_lo": 10, "line_hi": 12}],
        "merge_conflict_likely": True,
    }
    warn_extra = {"impact": ["SECRET/downstream.py"], "contested_with": ["maintainer_alice PR-50"]}
    unknown_extra = {"unknown_paths": ["SECRET/newfile.py"],
                     "dampened_with": [{"by": "maintainer_eve PR-22", "via_hub": "SECRET/hub.py"}]}
    holder_extra = {"queued_behind": ["maintainer_carol PR-77"], "queued_behind_paths": ["SECRET/lane.py"]}
    foundation_extra = {"shared_foundation": [{"path": "SECRET/core_config.py", "fan_in": 99, "churn": 50}]}
    depends_extra = {"depends_on_changing": [{"path": "SECRET/base.py", "by": "maintainer_dave PR-33"}]}

    return [
        ("fork clear", row("clear"), "PR-99"),
        ("fork warn", row("warn", **warn_extra), "PR-99"),
        ("fork serialize", row("serialize", **serialize_extra), "PR-99"),
        ("fork serialize_soft", row("serialize_soft", serialize_behind=["maintainer_bob PR-42"]), "PR-99"),
        ("fork unknown", row("unknown", **unknown_extra), "PR-99"),
        ("fork clear holder (still posts)", row("clear", **holder_extra), "PR-99"),
        ("fork clear shared foundation (still posts)", row("clear", **foundation_extra), "PR-99"),
        ("fork warn + depends + cluster",
         {**BASE, "changes": [{"change_id": "PR-99", "label": "attacker PR-99", "agent": "attacker",
                               "verdict": "warn", "paths": ["SECRET/internal_module.py"],
                               "impact": ["SECRET/downstream.py"], "contested_with": ["maintainer_alice PR-50"],
                               **depends_extra}],
          "clusters": [{"changes": ["PR-99", "PR-50"], "agents": ["attacker PR-99", "maintainer_alice PR-50"],
                        "size": 3, "suggested_order": ["maintainer_bob PR-42", "attacker PR-99",
                                                       "maintainer_alice PR-50"]}]},
         "PR-99"),
    ]


# Every base-repo identity that must NEVER reach the fork (external-contributor-readable) comment.
_LEAK_TOKENS = [
    "SECRET", "internal_module", "downstream", "core_config", "newfile", "hub.py", "lane.py",
    "process_payment", "maintainer_alice", "maintainer_bob", "maintainer_carol", "maintainer_dave",
    "maintainer_eve", "PR-50", "PR-42", "PR-77", "PR-33", "PR-22",
]


def main() -> int:
    checks: list[tuple[str, bool]] = []
    violations: list[str] = []
    scanned_comment = False

    # ── (1) JARGON + OVERCLAIM on every fork-redacted surface, using the CANONICAL scanners ──────────────────
    for desc, impact, ref in _fork_scenarios():
        out = R.render_pr_check(impact, ref, is_fork=True)
        for label, text in (("title", out.get("title")), ("summary", out.get("summary")),
                            ("comment", out.get("comment"))):
            v = J._scan(f"{desc} {label}", text)
            oc = J._scan_overclaim(f"{desc} {label}", text)
            mc = J._scan_moat_counts(f"{desc} {label}", text)   # MOAT: no raw graph-topology count on the fork surface either (PO 「file count はダメ」)
            checks.append((f"[{desc}] no internal jargon in fork {label}", not v))
            checks.append((f"[{desc}] no overclaim (advisory honesty) in fork {label}", not oc))
            checks.append((f"[{desc}] no raw graph-topology count (MOAT) in fork {label}", not mc))
            violations.extend(v)
            violations.extend(oc)
            violations.extend(mc)
            if label == "comment" and text:
                scanned_comment = True

        # ── (2) CROSS-CONTRIBUTOR NON-LEAK (moat): no base-repo identity in the fork comment/summary/title ──
        blob = " ".join(str(out.get(k) or "") for k in ("title", "summary", "comment"))
        leaked = [t for t in _LEAK_TOKENS if t in blob]
        checks.append((f"[{desc}] MOAT: fork output names NO base-repo PR ref / login / path / symbol "
                       f"(leaked={leaked})", not leaked))
        if leaked:
            violations.append(f"[{desc}] CROSS-CONTRIBUTOR LEAK — base-repo identities {leaked} reached the "
                              f"external-contributor-readable fork surface, in: {blob!r}")

    # coverage self-check: a fork comment was actually produced + scanned (else the jargon scan is vacuous).
    checks.append(("coverage: at least one fork scenario rendered + scanned a non-empty comment", scanned_comment))

    # ── (3) FORK NEVER PAUSES — apply_pause_ack(is_fork=True) returns the notice unchanged, and its copy is clean ──
    # A serialize fork PR is a MATERIAL coupling in the non-fork world; on a fork it must NOT pause (no real gate
    # on the base repo, no way for the outside contributor to add the ack label). Drive all three ack inputs.
    ser_impact = {**BASE, "changes": [{"change_id": "PR-99", "label": "attacker PR-99", "agent": "attacker",
        "verdict": "serialize", "paths": ["SECRET/internal_module.py"], "serialize_behind": ["maintainer_bob PR-42"],
        "collision_points": [{"path": "SECRET/internal_module.py", "symbol": "process_payment"}]}]}
    rendered = R.render_pr_check(ser_impact, "PR-99", is_fork=True)
    for ack_desc, label_present, prior in (("fork paused-input", False, None),
                                           ("fork ack-input", True, "deadbeefdead"),
                                           ("fork stale-input", True, "0000aaaa1111")):
        pa = R.apply_pause_ack(rendered, ser_impact, "PR-99", label_present=label_present,
                               prior_hash=prior, branch="main", is_fork=True)
        # never an enforced pause on a fork
        checks.append((f"[{ack_desc}] fork pause-ack is a NOTICE, not a gate (ack_state='not_material', "
                       f"conclusion != action_required) — got {pa.get('ack_state')!r}/{pa.get('conclusion')!r}",
                       pa.get("ack_state") == "not_material" and pa.get("conclusion") != "action_required"))
        # and its copy holds the jargon + overclaim + moat contract too
        pblob = " ".join(str(pa.get(k) or "") for k in ("title", "comment"))
        pv = J._scan(f"{ack_desc}", pblob)
        poc = J._scan_overclaim(f"{ack_desc}", pblob)
        pmc = J._scan_moat_counts(f"{ack_desc}", pblob)   # MOAT: the fork pause-ack copy must not leak a topology count either
        pleak = [t for t in _LEAK_TOKENS if t in pblob]
        checks.append((f"[{ack_desc}] fork pause-ack copy: no jargon / no overclaim / no raw count / no base-repo "
                       f"leak (leak={pleak})", not pv and not poc and not pmc and not pleak))
        violations.extend(pv)
        violations.extend(poc)
        violations.extend(pmc)
        if pleak:
            violations.append(f"[{ack_desc}] fork pause-ack leaked base-repo identities {pleak}: {pblob!r}")

    # ── SANITY: the canonical contract this gate borrows is non-trivial (it really has terms to enforce) ─────
    checks.append(("sanity: the canonical DENYLIST is non-empty (this lock borrows a real contract)",
                   len(J.DENYLIST) > 5))
    checks.append(("sanity: the canonical OVERCLAIM_PHRASES is non-empty",
                   len(J.OVERCLAIM_PHRASES) > 5))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    ok = ok and not violations

    if violations:
        print("\n-- FORK-SURFACE LEAKS / OVERCLAIMS FOUND (the redacted surface is NOT clean) --")
        for v in violations:
            print("   * " + v)

    print("FORK-SURFACE HONESTY GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
