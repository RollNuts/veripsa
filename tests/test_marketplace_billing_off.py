#!/usr/bin/env python3
"""MARKETPLACE BILLING KILL SWITCH — the free-first guard for future Marketplace billing.

ECONOMIC-SECURITY P1 (the dormant landmine this gate disarms): Veripsa is launching free-first. The
`marketplace_purchase` webhook handler (github-app/webhook_handlers.py) is wired into handle_event, and on a
`purchased` action it can call core.set_account_plan_with_authority(...) — auto-granting a paid Core tier before
Marketplace entitlement sync, platform org_entitlement, and payer↔org binding are implemented. It is dormant
TODAY, but a future Marketplace listing OR a leaked webhook secret would otherwise arm it into a free-paid-plan
grant that drifts core.account.plan out of sync with the platform entitlement model.

THE FIX this gate locks in: handle_event routes `marketplace_purchase` ONLY when VERIPSA_MARKETPLACE_BILLING=1.
DEFAULT OFF (the inverse of the project's default-ON kill switches; the same shape as server.py's default-OFF
VERIPSA_ALLOW_UNSIGNED opt-in). The handler code is KEPT INTACT for a possible future Marketplace SKU — but
default-inert.

This gate proves, OFFLINE (no Postgres — a recording fake `db` is all that's needed to see whether the plan
SETTER would be invoked), the exact security contract:

  1. FLAG OFF (DEFAULT / unset): a `marketplace_purchase` `purchased` event is a NOOP — core.set_account_plan_*
     is NEVER called (the recording db sees ZERO writes), and the result is the honest skipped/noop dict. This is
     the security property: the future Marketplace billing path cannot fire early.
  2. FLAG OFF for EVERY action (purchased / changed / cancelled / pending_change): still a noop, no setter call.
  3. FLAG ON (VERIPSA_MARKETPLACE_BILLING=1): the EXISTING behavior is PRESERVED — a `purchased` event DOES call
     core.set_account_plan_with_authority (the readied seam still works when an operator explicitly opts in).
  4. NON-'1' values ('', '0', 'true') read as OFF (only the exact string '1' arms it) — no accidental enable.
  5. The handler body is intact (the future-SKU code is not deleted) and carries the reconcile-before-enable note.

Run:  python3 tests/test_marketplace_billing_off.py   (no DB required)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import webhook_handlers as WH  # noqa: E402 — the routing brain under test (server.py re-exports handle_event)

checks = []  # (label, passed)


def add(label, passed):
    checks.append((label, passed))


class RecordingDB:
    """A fake db(sql, args) runner that RECORDS every SQL it is handed and returns a benign value. The handler's
    only DB write is `db("SELECT core.set_account_plan_with_authority(%s,%s,%s)", (...))`, so a setter call is
    visible as a recorded SQL containing 'set_account_plan'. No Postgres needed to observe the security property."""

    def __init__(self):
        self.calls = []  # list of (sql, args)

    def __call__(self, sql, args=()):
        self.calls.append((sql, args))
        return "ACCT-GH-RECORDED"  # a plausible account id so the handler's return-dict path is exercised

    def set_account_plan_calls(self):
        return [c for c in self.calls if "set_account_plan" in (c[0] or "")]


class FakeGH:
    """handle_event's marketplace branch makes NO gh calls; a marketplace_purchase payload carries no installation
    id, so for_installation is never consulted. Present only because handle_event's signature requires a client."""

    def for_installation(self, _id):
        return self


def _purchased_payload(gh_id=7777, plan_name="pro"):
    return {"action": "purchased",
            "marketplace_purchase": {"account": {"id": gh_id}, "plan": {"id": 42, "name": plan_name}}}


def _run(payload, flag):
    """Set the flag (or unset it when flag is None), run handle_event over a fresh recording db, return (db, res).
    The flag is read at CALL time inside handle_event, so a per-case os.environ mutation takes effect immediately."""
    if flag is None:
        os.environ.pop("VERIPSA_MARKETPLACE_BILLING", None)
    else:
        os.environ["VERIPSA_MARKETPLACE_BILLING"] = flag
    db = RecordingDB()
    res = WH.handle_event("marketplace_purchase", payload, db, FakeGH())
    return db, res


def main():
    print("VERIPSA MARKETPLACE BILLING KILL SWITCH — default-OFF noop / opt-in ON (offline gate)")

    # ── 1) FLAG UNSET (the true default an operator ships): purchased ⇒ NOOP, setter NEVER called. ──────────────
    db, res = _run(_purchased_payload(), flag=None)
    add("DEFAULT (unset): a 'purchased' event does NOT call core.set_account_plan_* (Marketplace billing cannot fire early)",
        db.set_account_plan_calls() == [])
    add("DEFAULT (unset): NO DB write at all is issued (a clean, content-free noop)", db.calls == [])
    add("DEFAULT (unset): the result is an honest noop/skipped dict (event=marketplace_purchase, noop)",
        res.get("event") == "marketplace_purchase" and res.get("noop") is True and "skipped" in res)
    add("DEFAULT (unset): the helper reports the switch OFF", WH._marketplace_billing_enabled() is False)

    # ── 2) FLAG EXPLICITLY '0' and other non-'1' values read as OFF (only the exact '1' arms it). ───────────────
    for val in ("0", "", "true", "yes", "off"):
        db, res = _run(_purchased_payload(), flag=val)
        add(f"OFF for VERIPSA_MARKETPLACE_BILLING={val!r}: noop, setter NOT called (only '1' enables)",
            db.set_account_plan_calls() == [] and res.get("noop") is True)

    # ── 3) FLAG OFF for EVERY action — none of purchased/changed/cancelled/pending_change writes a plan. ───────
    for action in ("purchased", "changed", "cancelled", "pending_change"):
        p = {"action": action, "marketplace_purchase": {"account": {"id": 8800}, "plan": {"name": "pro"}}}
        db, res = _run(p, flag=None)
        add(f"OFF: action={action!r} is a noop — core.set_account_plan_* NOT called",
            db.set_account_plan_calls() == [] and res.get("noop") is True)

    # ── 4) FLAG ON (VERIPSA_MARKETPLACE_BILLING=1): the EXISTING behavior is preserved — purchased DOES call the
    #       gated plan setter (the readied seam still works on explicit opt-in). This is the byte-for-byte route the
    #       DB-backed tests/test_marketplace_billing.py exercises end-to-end; here we assert only that the SETTER is
    #       reached (the recording db captures the exact SQL), which is the half this kill-switch gate owns. ──────
    db, res = _run(_purchased_payload(gh_id=7777, plan_name="pro"), flag="1")
    setter_calls = db.set_account_plan_calls()
    add("ON ('1'): a 'purchased' event DOES call core.set_account_plan_with_authority (existing behavior preserved)",
        len(setter_calls) == 1 and "set_account_plan_with_authority" in setter_calls[0][0])
    add("ON ('1'): the setter is handed the resolved account id + plan label ('pro') from the payload",
        len(setter_calls) == 1 and setter_calls[0][1][0] == "7777" and setter_calls[0][1][1] == "pro")
    add("ON ('1'): the result carries the mapped plan (the handler's normal return, not a skip)",
        res.get("event") == "marketplace_purchase" and res.get("plan") == "pro" and "skipped" not in res)
    add("ON ('1'): the helper reports the switch ON", WH._marketplace_billing_enabled() is True)

    # ── 5) ON + cancelled still resets to 'free' (the enabled path's full behavior is untouched by the gate). ───
    db, res = _run({"action": "cancelled",
                    "marketplace_purchase": {"account": {"id": 7777}, "plan": {"name": "pro"}}}, flag="1")
    add("ON ('1'): a 'cancelled' event calls the setter with plan='free' (downgrade path intact)",
        len(db.set_account_plan_calls()) == 1 and db.set_account_plan_calls()[0][1][1] == "free")

    # ── 6) The handler body is KEPT INTACT (not deleted) for a future Marketplace SKU, and carries the
    #       reconcile-before-enable note (entitlement tables + payer↔org binding). A pure source check. ──────────
    import inspect
    src = inspect.getsource(WH._handle_marketplace_event)
    add("HANDLER intact: _handle_marketplace_event still contains the plan-setter write (future-SKU code preserved)",
        "set_account_plan_with_authority" in src)
    gate_src = inspect.getsource(WH._marketplace_billing_enabled) + inspect.getsource(WH.handle_event)
    add("RECONCILE NOTE present: enabling the flag is documented to REQUIRE the platform entitlement tables + the "
        "payer↔org binding (so a future turn-on cannot mint paid Core access for the wrong org)",
        ("entitlement" in gate_src and "payer" in gate_src and "Marketplace" in gate_src))

    # leave the environment clean for any later in-process gate
    os.environ.pop("VERIPSA_MARKETPLACE_BILLING", None)

    # ── verdict ──────────────────────────────────────────────────────────────────────────────────────────────
    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    failed = [label for label, ok in checks if not ok]
    print(f"\n-- {passed}/{total} marketplace-billing-OFF assertions passed "
          "(default-OFF noop: set_account_plan NEVER called unless VERIPSA_MARKETPLACE_BILLING=1; only '1' arms "
          "it; ON preserves the existing plan-mapping behavior; handler kept intact w/ reconcile-before-enable note) --")
    if failed:
        print(f"\n[FAIL] {len(failed)} assertion(s) FAILED — the Marketplace billing guard is unsafe:")
        for f in failed[:40]:
            print(f"   - {f}")
        print("\nMARKETPLACE BILLING OFF GATE: FAIL")
        sys.exit(1)
    print("\nHONEST: this proves the marketplace_purchase route is INERT by default for the free-first launch, and "
          "only the explicit VERIPSA_MARKETPLACE_BILLING=1 opt-in arms the readied seam. Turning it on for a real "
          "Marketplace SKU still REQUIRES reconciling with the platform entitlement tables + payer↔org model — see "
          "github-app/webhook_handlers.py._marketplace_billing_enabled.")
    print("MARKETPLACE BILLING OFF GATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # an unexpected error must FAIL the gate, never pass silently
        print(f"\n[FAIL] unexpected error: {e}")
        print("MARKETPLACE BILLING OFF GATE: FAIL")
        sys.exit(1)
