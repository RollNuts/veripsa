#!/usr/bin/env python3
"""BUILTIN / SHORT-NAME CALL STOPLIST gate (FALSE-COUPLING audit 2026-06-21, measured on flask/nestjs/hugo).

Veripsa's quality is PRECISE SILENCE: a false graph edge → a false "Heads up" warn → wallpaper that destroys
trust. A measured MED precision FP class (~8–10% of shipped pairs, ~90–100% false within the class): a coupling
whose SOLE basis is a BUILTIN / STDLIB / SHORT method-name call resolved via _claim_adjacency's import-UNCONFIRMED
single-definer rule. The extractor records a method call by its BARE attribute tail —
  Promise.all → 'all'   JSON.parse → 'parse'   dict.pop → 'pop'   Object.assign → 'assign'
  strings.HasPrefix → 'hasprefix'   sync.Once.Do → 'do'   json.NewDecoder → 'newdecoder'
— so the bare builtin name matches a LONE class method of the same name in some unrelated file and the
single-definer rule false-couples the caller to it. A single-LETTER target (m/a/f/x) is worse: it false-couples
even cross-language (a Go test to a JS bundle).

THE FIX (recall-safe — guards the import-UNCONFIRMED single-definer branch ONLY, in out_adj / in_adj of
core._claim_adjacency AND calls_h of core._dampened_adjacency):
  (1) char_length(call target) > 2          — a ≤2-char un-imported name is never a coupling anchor.
  (2) lower(call target) ∉ a BUILTIN/STDLIB/container/IO/parse method-name set (CRUD-domain verbs EXCLUDED).
An IMPORT-CONFIRMED call to any of these names SURVIVES via the EXISTS(imports …) branch; domain names are
untouched. The backtest_cochange.py STOP mirror + MIN_NAME_LEN are kept in sync.

This proves the LIVE core.main_impact_surface (the customer-facing brain) on crafted cases:
  A) BUILTIN '.pop()' un-imported single-definer  → NOT contested (the FP this audit drops).        CLEAR
  B) SINGLE-LETTER '.m()' un-imported single-definer → NOT contested (the ≤2-char guard).            CLEAR
  C) RECALL — IMPORT-CONFIRMED '.pop()' → MUST still warn (import branch survives; recall-safe).      WARN
  D) RECALL — DOMAIN single-definer ('transmogrify', no import) → MUST still warn (domain untouched). WARN
Plus STRUCTURAL: the guard is present in all THREE single-definer branches, and the backtest mirror matches.

PROCESS-UNIQUE scratch DB (parallel-safe), exactly like the other engine-path gates. content-free (names only).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
import code_graph_extract as X  # noqa: E402

DB = "veripsa_builtinfp_" + str(os.getpid())


def db(sql, args=()):
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _w(d, rel, body):
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(body)


def _ingest(repo, files):
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            _w(d, rel, body)
        g = X.build_graph(d)
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(g), repo, "main", "a" * 40))


def _claim(cid, path, author, repo):
    db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, repo, "main", author))


def _surface(repo):
    imp = db("SELECT core.main_impact_surface(%s,%s)", (repo, "main"))
    if isinstance(imp, str):
        imp = json.loads(imp)
    return {c["change_id"]: c for c in imp.get("changes", [])}


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    checks = []
    try:
        # ──────────────────────────────────────────────────────────────────────────────────────────
        # A) BUILTIN '.pop()' — un-imported single-definer. A worker calls a builtin list .pop(); an
        # UNRELATED data class defines a lone method named `pop`. NO import between them. Before the fix
        # the single-definer rule false-coupled them; the builtin stoplist must drop it. CLEAR.
        repo_a = "bfp/builtin-pop"
        _ingest(repo_a, {
            "store/inventory.py": "class Inventory:\n    def pop(self):\n        return self._x.pop()\n",
            "jobs/sweeper.py":    "def sweep(items):\n    x = []\n    return x.pop()\n",   # builtin .pop(), no import
        })
        _claim("PR-INV:store/inventory.py", "store/inventory.py", "invdev", repo_a)
        _claim("PR-SWP:jobs/sweeper.py", "jobs/sweeper.py", "swpdev", repo_a)
        sa = _surface(repo_a)
        swp = sa.get("PR-SWP", {})
        inv = sa.get("PR-INV", {})
        checks.append(("A: builtin '.pop()' (un-imported, lone same-name method) does NOT couple sweeper↔inventory "
                       f"(sweeper verdict='{swp.get('verdict')}', contested_with={swp.get('contested_with')})",
                       swp.get("verdict") == "clear" and not (swp.get("contested_with") or [])))
        checks.append(("A: the other side (inventory) is likewise CLEAR — symmetric, no false warn "
                       f"(verdict='{inv.get('verdict')}')",
                       inv.get("verdict") == "clear" and not (inv.get("contested_with") or [])))

        # ──────────────────────────────────────────────────────────────────────────────────────────
        # B) SINGLE-LETTER '.m()' — un-imported single-definer. A ≤2-char target must be dropped by the
        # min-name-length guard (m/a/f/x are never a meaningful coupling anchor un-imported). CLEAR.
        repo_b = "bfp/single-letter"
        _ingest(repo_b, {
            "util/short.py": "class S:\n    def m(self):\n        return 1\n",   # lone method 'm'
            "app/use.py":    "def go(obj):\n    return obj.m()\n",               # calls .m(), no import
        })
        _claim("PR-SHORT:util/short.py", "util/short.py", "shortdev", repo_b)
        _claim("PR-USE:app/use.py", "app/use.py", "usedev", repo_b)
        sb = _surface(repo_b)
        use = sb.get("PR-USE", {})
        checks.append(("B: single-letter '.m()' (≤2 chars, un-imported, lone def) does NOT couple use↔short "
                       f"(use verdict='{use.get('verdict')}', contested_with={use.get('contested_with')})",
                       use.get("verdict") == "clear" and not (use.get("contested_with") or [])))

        # ──────────────────────────────────────────────────────────────────────────────────────────
        # C) RECALL — IMPORT-CONFIRMED '.pop()'. The SAME builtin name, but the caller IMPORTS the class
        # and calls .pop() on it → a real coupling that MUST survive (the import branch is untouched). WARN.
        repo_c = "bfp/import-confirmed-pop"
        _ingest(repo_c, {
            "store/inventory.py": "class Inventory:\n    def pop(self):\n        return 1\n",
            "jobs/picker.py":     "from store.inventory import Inventory\n\n"
                                  "def pick():\n    inv = Inventory()\n    return inv.pop()\n",
        })
        _claim("PR-INV:store/inventory.py", "store/inventory.py", "invdev", repo_c)
        _claim("PR-PICK:jobs/picker.py", "jobs/picker.py", "pickdev", repo_c)
        sc = _surface(repo_c)
        pick = sc.get("PR-PICK", {})
        inv_c = sc.get("PR-INV", {})
        pick_cw = pick.get("contested_with", []) or []
        checks.append(("C: RECALL — IMPORT-CONFIRMED '.pop()' STILL warns picker vs inventory (import branch survives) "
                       f"(picker verdict='{pick.get('verdict')}', contested_with={pick_cw})",
                       pick.get("verdict") == "warn" and any("invdev" in str(x) for x in pick_cw)))
        checks.append(("C: inventory side mirrors the warn (symmetric, recall preserved) "
                       f"(verdict='{inv_c.get('verdict')}')",
                       inv_c.get("verdict") == "warn"))

        # ──────────────────────────────────────────────────────────────────────────────────────────
        # D) RECALL — DOMAIN single-definer, no import. A domain function name defined exactly once and
        # called with no import is unambiguous and is NOT in the builtin set → MUST still warn. WARN.
        repo_d = "bfp/domain-single"
        _ingest(repo_d, {
            "core/engine.py": "def transmogrify(x):\n    return x\n",
            "app/caller.py":  "def run():\n    return transmogrify(1)\n",   # no import, exactly one definer
        })
        _claim("PR-ENG:core/engine.py", "core/engine.py", "engdev", repo_d)
        _claim("PR-CALL:app/caller.py", "app/caller.py", "calldev", repo_d)
        sd = _surface(repo_d)
        call = sd.get("PR-CALL", {})
        eng = sd.get("PR-ENG", {})
        checks.append(("D: RECALL — DOMAIN single-definer 'transmogrify' (no import) STILL warns (not a builtin; recall kept) "
                       f"(caller verdict='{call.get('verdict')}', definer verdict='{eng.get('verdict')}')",
                       call.get("verdict") == "warn" and eng.get("verdict") == "warn"))

        # ──────────────────────────────────────────────────────────────────────────────────────────
        # STRUCTURAL — the guard is present in ALL THREE single-definer branches (out_adj, in_adj, calls_h),
        # and the backtest_cochange.py mirror (STOP set + MIN_NAME_LEN length guard) is in sync. A future
        # regression that drops the guard from one branch, or lets the mirror drift, FAILS here.
        social = open(os.path.join(ROOT, "db", "schema", "70_social.sql"), encoding="utf-8").read()
        # the builtin set + the char_length guard must each appear THREE times (out_adj, in_adj, calls_h).
        n_len_guard = len(re.findall(r"char_length\(ce\.dst\)\s*>\s*2", social))
        n_set_guard = len(re.findall(r"lower\(ce\.dst\)\s*<>\s*ALL", social))
        checks.append((f"STRUCTURAL: min-name-length guard `char_length(ce.dst) > 2` present in all 3 branches (found {n_len_guard})",
                       n_len_guard == 3))
        checks.append((f"STRUCTURAL: builtin-set guard `lower(ce.dst) <> ALL (ARRAY[…])` present in all 3 branches (found {n_set_guard})",
                       n_set_guard == 3))
        # a few representative builtin names from the audit examples must be in the SQL set.
        sql_has = all(("'%s'" % nm) in social for nm in ("pop", "all", "stringify", "assign", "hasprefix", "do", "newdecoder"))
        checks.append(("STRUCTURAL: the measured audit builtin names (pop/all/stringify/assign/hasprefix/do/newdecoder) are in the SQL set",
                       sql_has))
        # the CRUD-domain verbs must NOT be in the builtin set (recall protection — they can be real couplings).
        # scope the search to the single-definer guard blocks (avoid matching them in unrelated prose/identifiers).
        guard_blocks = "".join(re.findall(r"lower\(ce\.dst\) <> ALL \(ARRAY\[(.*?)\]\)", social, flags=re.S))
        domain_leak = [v for v in ("create", "build", "find", "save", "update", "delete", "process", "validate")
                       if ("'%s'" % v) in guard_blocks]
        checks.append((f"STRUCTURAL: CRUD-domain verbs are EXCLUDED from the builtin set (recall-safe) — leaked={domain_leak}",
                       not domain_leak))
        # backtest mirror in sync: import the module, assert STOP ⊇ builtin set and MIN_NAME_LEN==3.
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        import backtest_cochange as B  # noqa: E402
        mirror_names = {"pop", "all", "stringify", "assign", "hasprefix", "do", "newdecoder", "keys", "values", "items"}
        mirror_ok = mirror_names <= B.STOP and getattr(B, "MIN_NAME_LEN", None) == 3
        mirror_no_domain = not ({"create", "build", "find", "save", "update", "delete"} & B.STOP)
        checks.append((f"STRUCTURAL: backtest_cochange mirror in sync (STOP ⊇ builtin set, MIN_NAME_LEN=={getattr(B,'MIN_NAME_LEN',None)}, no CRUD verbs)",
                       mirror_ok and mirror_no_domain))
    finally:
        subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True, text=True)

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("BUILTIN CALL STOPLIST GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
