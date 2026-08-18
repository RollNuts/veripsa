#!/usr/bin/env python3
"""CO-CHANGE STORE gate — the content-free co-change cache + ingest + partner read surface, end to end,
moat-isolated.

Proves the storage layer that turns _cg_cochange (the extractor) into a live, per-tenant, content-free signal:
  (1) ingest_cochange_with_authority stores a repo's co-change pairs (gated write, governed-write forgery gate);
  (2) co_change_partners_with_authority returns the "you touched A; B historically comes with it" hint with the
      DIRECTIONAL conditional probability P(partner | edited) — not a raw count;
  (3) MOAT: a SECOND tenant reading the SAME repo coordinate sees NOTHING (FORCE RLS walls the cache per
      account — co-change must not leak across tenants any more than the code graph does);
  (4) re-ingest is idempotent (DELETE+reINSERT — a derived cache);
  (5) CONTENT-FREE: nothing but paths + counts is stored or returned.

Run:  python3 tests/test_cochange_store.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import _cg_cochange as CC  # noqa: E402

DB = "veripsa_ccstore_" + str(os.getpid())
REPO = "acme/cc"
checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def tenant(install_id):
    """A db runner pinned to one installation's tenant (enter_installation), mirroring the live per-event path."""
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
    return conn, run


def _j(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    # STORE-layer input starts at the extractor's content-free boundary: per-commit path sets. Real-Git history
    # extraction is covered by test_cochange.py; history-to-store glue is covered by test_cochange_populate.py.
    commit_paths = (
        [{"backend/auth.py", "backend/api.py"} for _ in range(5)]
        + [{"backend/auth.py"} for _ in range(2)]
        + [{"backend/api.py"} for _ in range(2)]
        + [{f"misc/m{k}.py"} for k in range(40)]   # background keeps auth/api rare vs N, so lift = 5
        + [{f"v/lib{n}.py" for n in range(50)}]    # giant commit: skipped, including from n_total
    )
    change = collections.Counter()
    co = collections.Counter()
    n_total = CC.fold_commits(commit_paths, change, co, max_commit_files=40)
    pairs = CC.emit_pairs(change, co, n_total, min_support=3, min_prob=0.3, min_lift=2.0)
    auth_api = next((p for p in pairs if {p["a"], p["b"]} == {"backend/auth.py", "backend/api.py"}), None)
    chk(n_total == 49 and change["backend/auth.py"] == 7 and change["backend/api.py"] == 7
        and all(change[f"misc/m{k}.py"] == 1 for k in range(40))
        and not any(path.startswith("v/lib") for path in change)
        and auth_api is not None and auth_api["co"] == 5 and auth_api["n_a"] == 7 and auth_api["n_b"] == 7
        and auth_api["n_total"] == 49 and float(auth_api["lift"]) == 5.0 and len(pairs) == 1,
        "(0) path-set fold keeps co=5, n_auth=7, n_api=7, n_total=49, lift=5 and skips the 50-file giant commit")

    # Inject adjacent history content as unknown fields at the STORE boundary. The writer must whitelist the
    # path/count fields and never persist or return these message/body/author/SHA sentinels.
    content_sentinels = {
        "commit_message": "SECRET_MSG_store_sentinel",
        "file_body": "SECRET_BODY_store_sentinel",
        "author": "SECRET_AUTHOR_store_sentinel",
        "sha": "SECRET_SHA_store_sentinel",
    }
    ingest_pairs = [{**pair, **content_sentinels} for pair in pairs]

    a_conn, A = tenant("111")   # ACCT-GH-111
    b_conn, B = tenant("222")   # ACCT-GH-222

    res = A("SELECT core.ingest_cochange_with_authority(%s,%s)", (json.dumps(ingest_pairs), REPO))
    res = res if isinstance(res, dict) else json.loads(res)
    chk(res.get("ok") and res.get("pairs", 0) >= 1, f"(1) ingest stores the repo's co-change pairs (got {res})")

    # (2) the partner hint with the DIRECTIONAL probability P(api | auth) = co/n_auth = 5/7 ≈ 0.71.
    parts = _j(A("SELECT core.co_change_partners_with_authority(%s,%s,%s,%s)", (REPO, ["backend/auth.py"], 3, 0.4)))
    api = next((p for p in parts if p.get("partner") == "backend/api.py"), None)
    chk(api is not None and abs(float(api["prob"]) - 5 / 7) < 0.001 and api["co"] == 5 and api["n"] == 7
        and float(api.get("lift", 0)) == 5.0,
        f"(2) partner hint: editing auth surfaces api with confidence ~{5/7:.2f} AND lift = 5x (got {api})")
    # the giant commit's vendor files never became partners.
    chk(not any(p.get("partner", "").startswith("v/lib") for p in parts),
        "(2b) the 50-file giant commit minted no partners (noise control survives the round-trip)")

    # (3) MOAT: tenant B reading the SAME repo coordinate sees NOTHING (FORCE RLS walls the cache).
    b_parts = _j(B("SELECT core.co_change_partners_with_authority(%s,%s,%s,%s)", (REPO, ["backend/auth.py"], 3, 0.4)))
    chk(b_parts == [], f"(3) cross-tenant isolation: a 2nd tenant reading the same repo sees NOTHING (got {b_parts})")

    # (4) re-ingest is idempotent (DELETE+reINSERT) — same pair count, no duplication.
    res2 = A("SELECT core.ingest_cochange_with_authority(%s,%s)", (json.dumps(ingest_pairs), REPO))
    res2 = res2 if isinstance(res2, dict) else json.loads(res2)
    chk(res2.get("pairs") == res.get("pairs"), f"(4) re-ingest is idempotent ({res.get('pairs')} -> {res2.get('pairs')})")

    # (5) CONTENT-FREE: injected history content is absent, and the read surface remains paths + counts only.
    round_trip = json.dumps([res, parts, b_parts, res2], sort_keys=True)
    chk(all(sentinel not in round_trip for sentinel in content_sentinels.values())
        and all(set(part) == {"edited", "partner", "co", "prob", "lift", "n"} for part in parts),
        "(5) content-free: stored + returned values are paths + counts only (no message / body / author / SHA)")

    a_conn.close()
    b_conn.close()
    cleanup = subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    chk(cleanup.returncode == 0, "(6) scratch database is removed after tenant connections close")

    ok = all(checks)
    print("CO-CHANGE STORE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
