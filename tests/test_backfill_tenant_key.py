#!/usr/bin/env python3
"""BACKFILL TENANT-KEY gate — the `backfill <repo>` CLI must key the tenant by the OWNING ACCOUNT id, never the
ephemeral installation id.

The audited dual-account orphan (round-5): `enter_installation_with_authority(p)` only PREFIXES ('ACCT-GH-'||p);
it does NOT map an installation id → its owner. The live webhook path keys by repository.owner.id
(→ ACCT-GH-<owner_id>); the boot self-heal + co-change use gh.installation_account_id() (= owner id). The backfill
CLI used to pass the RAW GH_INSTALLATION_ID → ACCT-GH-<install_id>, a PHANTOM tenant the live path never
addresses. scripts/refresh_demo.sh runs `backfill`, so every demo-refresh wrote the graph into that orphan
account (the prod de75fe23 split) while the live tenant stayed cold. This is a __main__ CLI path (not unit-
callable), so this gate guards the fix structurally: the CLI resolves the owner via gh.installation_account_id()
and never pins the raw install id.

Run:  python3 tests/test_backfill_tenant_key.py
"""
from __future__ import annotations
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "github-app", "server.py")).read()
FAIL = 0


def chk(c, label):
    global FAIL
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    if not c:
        FAIL = 1


def main() -> int:
    # Isolate the backfill CLI block (from the `== "backfill"` dispatch to the serve() else).
    m = re.search(r'== "backfill".*?(?=\n    else:\n        serve\()', SRC, re.S)
    block = m.group(0) if m else ""
    chk(bool(block), "found the backfill CLI block in server.py")

    chk("gh.installation_account_id()" in block,
        "the backfill CLI resolves the OWNING account via gh.installation_account_id() (the live tenant key)")

    # the OLD bug: enter_installation_with_authority pinned with the raw install id. That exact pattern must be gone.
    chk(not re.search(r'enter_installation_with_authority%?s?["\']?\s*\)?\s*,\s*\(install_id', block)
        and "(install_id,))" not in block,
        "the CLI NEVER pins enter_installation_with_authority(install_id) (the orphan-tenant bug)")

    # whatever it pins, it must be the resolved account_key, not the raw install id.
    chk("account_key = gh.installation_account_id()" in block
        and "enter_existing_installation_with_authority(%s)\", (account_key,)" in block,
        "the non-provisioning pin uses the resolved owner account_key")

    print("BACKFILL TENANT-KEY GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
