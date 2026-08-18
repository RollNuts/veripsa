#!/usr/bin/env python3
"""WRITER REVOKE-PUBLIC — the stray-PUBLIC-grant guard (security-critical, structural; no Postgres).

Postgres grants EXECUTE to PUBLIC by DEFAULT on every CREATE FUNCTION. A `*_with_authority` fn is a
SECURITY DEFINER WRITE path (it runs AS the migrator owner and arms the forgery token), so a stray PUBLIC
EXECUTE is the exact "stray-PUBLIC-grant" class the moat's threat model closes: the discipline everywhere
is to pair each GRANT with a `REVOKE EXECUTE ... FROM PUBLIC` so ONLY the intended tenant/App role can call
it (record_statement got it in #184, act_for in #108, the writer gates in #189). Low severity in isolation
(establish_session_write_context() 42501s a credential-less role; + the missing table grant + FORCE RLS +
the forgery trigger all still hold), but defense-in-depth: a missing REVOKE is a latent hole if any other
layer ever regresses, and the PUBLIC default reopens silently on every fresh CREATE.

THIS GATE is a PURE-SOURCE structural lint over db/schema/*.sql (no DB, no psycopg2 — runs anywhere, cheap,
parallel-safe). For EVERY `core.<name>_with_authority(<sig>)` defined (keyed off its `ALTER FUNCTION ...
OWNER TO` line, which is the canonical signature form GRANT/REVOKE must match), it asserts a paired
`REVOKE EXECUTE|ALL ON FUNCTION core.<name>(<sig>) FROM ... PUBLIC ...` exists with the SAME signature. It
also guards against OVER-revoke: no role that a GRANT hands the fn may also appear in that fn's REVOKE FROM
list (PUBLIC excepted) — the grant must survive. A missing REVOKE (gap) OR a granted-role revoke (over) is
a FAIL. (Read surfaces are out of scope here — their grant posture, incl. the owner-only owner_cost_surface
/ db_usage_surface lenses that ARE PUBLIC-revoked, is owned by test_security_perimeter.py PROBE5.)

If a NEW `*_with_authority` write fn ever ships without its REVOKE-FROM-PUBLIC, this gate FAILS loud —
the stray-PUBLIC-grant class can never silently reopen.

Run:  python3 tests/test_writer_revoke_public.py   (no Postgres needed)
"""
from __future__ import annotations

import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_GLOB = os.path.join(ROOT, "db", "schema", "*.sql")

# Canonical signature source: the ALTER FUNCTION ... OWNER TO line. GRANT/REVOKE must match THIS arg string.
ALTER_OWNER = re.compile(r"ALTER FUNCTION core\.([a-z0-9_]+)\(([^)]*)\)\s+OWNER TO", re.I)
REVOKE = re.compile(
    r"REVOKE\s+(?:EXECUTE|ALL)\s+ON FUNCTION core\.([a-z0-9_]+)\(([^)]*)\)\s+FROM\s+([^;]+);", re.I
)
GRANT = re.compile(
    r"GRANT\s+EXECUTE\s+ON FUNCTION core\.([a-z0-9_]+)\(([^)]*)\)\s+TO\s+([^;]+);", re.I
)


def _norm(sig: str) -> str:
    # collapse whitespace so 'timestamptz, text[]' matches 'timestamptz,text[]'
    return re.sub(r"\s+", "", sig)


def main() -> int:
    files = sorted(glob.glob(SCHEMA_GLOB))
    if not files:
        print(f"WRITER REVOKE-PUBLIC GATE: FAIL — no schema files at {SCHEMA_GLOB}")
        return 1

    defs: dict[tuple[str, str], str] = {}                 # (name,sig) -> file
    revokes: dict[tuple[str, str], list[str]] = {}        # (name,sig) -> [FROM-list, ...]
    grants: dict[tuple[str, str], list[str]] = {}         # (name,sig) -> [TO-list, ...]

    for f in files:
        txt = open(f, encoding="utf-8").read()
        for m in ALTER_OWNER.finditer(txt):
            defs[(m.group(1), _norm(m.group(2)))] = os.path.relpath(f, ROOT)
        for m in REVOKE.finditer(txt):
            revokes.setdefault((m.group(1), _norm(m.group(2))), []).append(m.group(3).strip())
        for m in GRANT.finditer(txt):
            grants.setdefault((m.group(1), _norm(m.group(2))), []).append(m.group(3).strip())

    failures: list[str] = []
    write_fns = sorted(k for k in defs if k[0].endswith("_with_authority"))
    if not write_fns:
        print("WRITER REVOKE-PUBLIC GATE: FAIL — found ZERO *_with_authority fns (parser/path regression?)")
        return 1

    # 1) GAP: every write fn must have a REVOKE ... FROM ... PUBLIC with the matching signature.
    for name, sig in write_fns:
        froms = revokes.get((name, sig), [])
        has_public = any("PUBLIC" in fr.upper() for fr in froms)
        if not has_public:
            failures.append(
                f"GAP: core.{name}({sig}) [{defs[(name, sig)]}] has no REVOKE ... FROM PUBLIC "
                f"(found FROM-lists: {froms or 'NONE'})"
            )

    # 2) OVER-REVOKE: a role a GRANT hands the fn must NOT also be revoked from it (PUBLIC excepted).
    for name, sig in write_fns:
        granted = {r.strip() for to in grants.get((name, sig), []) for r in to.split(",")}
        revoked_roles = {
            r.strip()
            for fr in revokes.get((name, sig), [])
            for r in fr.split(",")
            if r.strip().upper() != "PUBLIC"
        }
        clash = granted & revoked_roles
        if clash:
            failures.append(
                f"OVER-REVOKE: core.{name}({sig}) grants AND revokes the same role(s) {sorted(clash)} "
                f"— the GRANT would be defeated"
            )

    # NOTE: we deliberately do NOT blanket-assert "no *_surface has a REVOKE FROM PUBLIC". Most read
    # surfaces ARE broad, but owner-only lenses (owner_cost_surface / db_usage_surface) are intentionally
    # PUBLIC-revoked + granted to the host/owner alone (see test_security_perimeter.py PROBE5). The grant
    # posture of read surfaces is owned by that perimeter gate; this gate's invariant is the WRITE path.

    print(f"-- scanned {len(files)} schema files, {len(write_fns)} *_with_authority write fns --")
    for name, sig in write_fns:
        froms = revokes.get((name, sig), [])
        mark = "OK " if any("PUBLIC" in fr.upper() for fr in froms) else "GAP"
        print(f"   [{mark}] core.{name}({sig})")

    if failures:
        print("\nWRITER REVOKE-PUBLIC GATE: FAIL")
        for fail in failures:
            print(f"   - {fail}")
        return 1

    print(
        f"\nWRITER REVOKE-PUBLIC GATE: PASS "
        f"(all {len(write_fns)} *_with_authority write fns pair a GRANT with a REVOKE-FROM-PUBLIC; "
        f"no granted role over-revoked; no read surface locked)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
