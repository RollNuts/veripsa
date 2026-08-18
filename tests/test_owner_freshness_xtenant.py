#!/usr/bin/env python3
"""OWNER GRAPH-FRESHNESS (cross-tenant) gate — the watchdog/freshness must see EVERY tenant's graph staleness,
not the single account a session happens to resolve to.

The audited bug (round-5): core.graph_freshness_surface() is pinned to resolve_session_identity's ONE account;
the watchdog's unpinned veripsa_app connection resolved to a credential-default/orphan account, so it watched
ONE (wrong) tenant and was BLIND to drift on every other (and in one incident it read an orphan fixture row and
falsely paged "stale" forever while the live tenant was fresh). Fix: a cross-tenant owner lens
core.owner_graph_freshness_surface() (the same owner-only model + lock as owner_cost_surface) — NO NEW TABLE, a
pure read over the existing graph_version per tenant. graph_freshness.py + server.py now read it.

Proves (real scratch DB, two tenants):
  (1) the cross-tenant lens returns coordinates from BOTH accounts (sees every tenant), while the single-account
      lens pinned to ONE tenant returns only that tenant's — the exact blindness the bug exploited;
  (2) it is OWNER-ONLY: a buyer/tenant role (no veripsa_app) cannot execute it (no cross-tenant leak);
  (3) shape compatible (coordinates + coordinate_count + max_age_seconds) so the Python readers swap cleanly;
  (4) LIVENESS: a SUSPENDED install is EXCLUDED from the surface (the App stops keeping a dead install's graph
      warm) while the LIVE tenant remains — even though suspend RETAINS the suspended tenant's graph_version rows
      (suspend is reversible), so the exclusion is the liveness filter, not a graph purge. The enumerator restricts
      the installation_account branch to live installs (revoked_at IS NULL), matching list_installation_ids().

Run:  python3 tests/test_owner_freshness_xtenant.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import psycopg2  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402
from _lifecycle_fixture import current_suspend_proof, seed_processing_installation_event  # noqa: E402

DB = "veripsa_xtfresh_" + str(os.getpid())
checks = []


def chk(c, label):
    print(("  [PASS] " if c else "  [FAIL] ") + label)
    checks.append(bool(c))


def tenant(install_id):
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("SET search_path=core")
        c.execute("SELECT core.enter_installation_with_authority(%s)", (install_id,))

    def run(sql, args=()):
        with conn.cursor() as c:
            c.execute(sql, args)
            row = c.fetchone()
            return row[0] if row else None
    return run


def _j(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    app_dsn = f"postgresql://veripsa_app@localhost/{DB}"
    owner_dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    seed_live_installation(app_dsn, owner_dsn, "111", "111")
    seed_live_installation(app_dsn, owner_dsn, "222", "222")

    g = json.dumps({"nodes": [{"id": "f", "kind": "file", "path": "x.py", "name": "x.py"}], "edges": []})
    A = tenant("111")   # the "live" tenant, fresh sha
    B = tenant("222")   # the "orphan" tenant, a stale sha — the kind the watchdog used to read in isolation
    A("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (g, "acme/app", "main", "a" * 40))
    B("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (g, "acme/app", "main", "b" * 40))

    # (1) cross-tenant lens (read as veripsa_app, pinned to tenant 111) sees BOTH accounts; single-account sees one.
    xt = _j(A("SELECT core.owner_graph_freshness_surface()"))
    coords = xt.get("coordinates") or []
    accts = sorted({c.get("account_id") for c in coords})
    shas = sorted({(c.get("commit_sha") or "")[:1] for c in coords})
    chk(xt.get("coordinate_count") == 2 and accts == ["ACCT-GH-111", "ACCT-GH-222"] and shas == ["a", "b"],
        f"owner_graph_freshness_surface sees EVERY tenant ({xt.get('coordinate_count')} coords, accts={accts}, shas={shas})")

    single = _j(A("SELECT core.graph_freshness_surface()"))
    s_accts = {c.get("repo") for c in (single.get("coordinates") or [])}
    chk(single.get("coordinate_count") == 1,
        f"the single-account lens (pinned to 111) sees ONLY its own tenant ({single.get('coordinate_count')} coord) — the blindness the bug exploited")

    # (3) shape compatible — the Python readers expect coordinates / coordinate_count / max_age_seconds.
    chk("coordinates" in xt and "coordinate_count" in xt and "max_age_seconds" in xt,
        "shape matches graph_freshness_surface (coordinates + coordinate_count + max_age_seconds) — readers swap cleanly")

    # (2) OWNER-ONLY: a tenant/buyer role (veripsa_demo_agent — inherits veripsa_writer, NOT veripsa_app) is refused.
    tconn = psycopg2.connect(f"postgresql://veripsa_demo_agent@localhost/{DB}")
    tconn.autocommit = True
    refused = False
    try:
        with tconn.cursor() as c:
            c.execute("SET search_path=core")
            c.execute("SELECT core.owner_graph_freshness_surface()")
    except psycopg2.errors.InsufficientPrivilege:
        refused = True
    except Exception as e:
        refused = "permission denied" in str(e).lower()
    chk(refused, "OWNER-ONLY: a buyer/tenant role cannot execute the cross-tenant lens (no cross-tenant leak)")

    # (4) LIVENESS — a SUSPENDED install must DROP OUT of the freshness surface so the App stops keeping a dead
    #     install's graph warm. Suspend (release_account_claims_with_authority) is reversible: it releases the lanes
    #     + stamps installation_account.revoked_at but RETAINS the graph_version rows (only a GDPR erase deletes the
    #     row; an UNINSTALL drops out indirectly because the purge deletes graph_version — a SUSPEND does NOT). So
    #     before the liveness filter the cross-tenant lens still walked the suspended tenant's retained graph.
    suspend_key = "owner-freshness-suspend-222"
    seed_processing_installation_event(
        owner_dsn,
        suspend_key,
        "222",
        "222",
        "suspend",
    )
    susp = _j(B("SELECT core.release_account_claims_with_authority(%s,%s::jsonb)",
                (suspend_key, json.dumps(current_suspend_proof("222", "222")))))
    chk(susp.get("installations_revoked") == 1,
        f"suspending tenant 222 revokes its install (installations_revoked={susp.get('installations_revoked')})")
    # the suspended tenant's graph is RETAINED — its OWN single-account lens (pinned to 222, no liveness filter on
    # graph_version) still sees its 1 coordinate. This is what makes the exclusion below a LIVENESS decision, not a purge.
    retained = _j(B("SELECT core.graph_freshness_surface()"))
    chk(retained.get("coordinate_count") == 1,
        f"the SUSPENDED tenant's graph_version is RETAINED — its single-account lens still sees it ({retained.get('coordinate_count')} coord; suspend is reversible)")
    # the cross-tenant owner lens, read as the LIVE tenant 111 (the watchdog's realistic vantage — never the
    # suspended customer), now ENUMERATES ONLY live installs (revoked_at IS NULL) → 222 is gone, 111 stays.
    xt2 = _j(A("SELECT core.owner_graph_freshness_surface()"))
    accts2 = sorted({c.get("account_id") for c in (xt2.get("coordinates") or [])})
    chk(xt2.get("coordinate_count") == 1 and accts2 == ["ACCT-GH-111"],
        f"a SUSPENDED install is EXCLUDED from owner_graph_freshness_surface while the LIVE one remains ({xt2.get('coordinate_count')} coord, accts={accts2})")

    print("OWNER-FRESHNESS-XTENANT GATE:", "PASS" if all(checks) else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
