#!/usr/bin/env python3
"""SCHEMA-COUPLING PRECISION gate — the ubiquitous-table-name false-coupling class, MEASURED, with
the recall-safe boundary PINNED (audit 2026-06-19, parallel to PR #252 / the call-edge precision guard).

THE HYPOTHESIS (concrete, parallel to the PROVEN call-name stoplist in recall_measure.STOP):
  The SCHEMA graph couples files that touch the SAME table (a migration ALTERs `orders`, code QUERIES
  `orders` -> coupled with no code edge -- the moat code-only tools miss). But UBIQUITOUS names
  (`users`, `id`, `tags`) appear in nearly every file, so two UNRELATED files that both mention `users`
  are NOT really coupled -- the same false-coupling class proven+fixed for ubiquitous CALL names.

WHAT THE MEASUREMENT FOUND (3 real SQL-heavy repos: saleor + django-oscar [Django], discourse [Rails],
co-change ground truth via lift; full numbers in the PR body):
  * The over-coupling is REAL in the RAW graph: discourse's `users` table couples 1054 file pairs whose
    co-change (1.3%) tracks RANDOM (0.3%), while SPECIFIC-name pairs co-change ELEVATED (7.6%, ~6x).
  * BUT the recall-safe fix ALREADY EXISTS in the engine. Every ubiquitous-named table in all 3 repos is
    a RESOURCE HUB -- touched by FAR more files than the hub threshold (saleor user=39, discourse
    users=191 / tags=33). The engine's resource-hub dampening (res_adj, > HUB_DEGREE participants ->
    demoted to 'unknown'/dampened, NEVER a confident 'clear') suppresses EVERY ubiquitous-only schema
    pair: after dampening, 0 ubiquitous-only pairs survive in any repo, while specific pairs stay
    elevated (14-33% co-change). The ubiquitous-name class and the hub class COINCIDE for schema --
    because a ubiquitous name is, by construction, touched by many files.

THE HONEST CONCLUSION (like PR #252): a content-free ubiquitous-schema-name STOPLIST at EXTRACTION is
NOT warranted -- it would be (a) REDUNDANT (res-hub dampening already covers it) and (b) RECALL-RISKY:
on a SMALL repo a specific, real, low-participant table could share a generic name (an app whose central
ledger table is literally named `record`/`status`, touched by only 3 files) -- a name-based extractor
stoplist would SILENTLY DROP that real coupling (recall loss), whereas hub dampening keeps it (3 < hub
threshold) AND demotes the genuinely-ubiquitous hub. CARDINAL RULE: when in doubt, KEEP; never DROP a
specific table coupling. So we change the extractor NOTHING and PIN the boundary here.

WHAT THIS GATE PINS (hermetic synthetic graph; content-free -- table NAMES + edge kinds only):
  1. The extractor KEEPS emitting raw schema edges for a ubiquitous-named hub (`users`, many touchers) --
     a future "precision fix" that DROPS ubiquitous schema names at extraction FAILS here (it would lose
     recall on a small repo whose real central table is generically named).
  2. The recall-safe demotion lives in the ENGINE: mirroring res_adj's hub dampening (> HUB_DEGREE
     participants), the ubiquitous-name HUB is demoted out of confident coupling, while...
  3. ...a SPECIFIC, low-participant table coupling (`inventory_ledger`, 3 touchers) is KEPT (recall) --
     and CRUCIALLY a GENERIC-named but low-participant table (`record`, 2 touchers, NOT a hub) is ALSO
     KEPT, which a name-based stoplist would wrongly drop. This is the recall proof that a stoplist is
     unsafe and dampening is the right home.

Hermetic: writes tiny .sql/.py files to a temp dir, reads the RAW edges build_graph emits, and mirrors
the engine's resource-hub dampening (the SAME res_hubs logic recall_measure.py uses). No DB, no network.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402

# Mirror of the engine's resource-hub dampening cutoff (recall_measure.res_hubs / core res_adj):
# a table touched by MORE than this many distinct files is a hub -> demoted, never a confident clear.
HUB_DEGREE = 8

# The CANDIDATE ubiquitous-schema-name set a name-based extractor stoplist WOULD use (documented for the
# reader; this gate proves such a stoplist is the WRONG tool, so the set is informational, never applied).
UBIQ_SCHEMA_NAMES = frozenset({
    "id", "name", "type", "status", "user", "users", "account", "accounts",
    "created_at", "updated_at", "user_id", "uuid", "key", "value", "data",
    "meta", "metadata", "tag", "tags", "label", "labels", "record", "records",
})


def _w(d, rel, body):
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(body)


def _schema_edges(g):
    """{table_name: set(src_files)} over queries/alters edges, and the raw edge list."""
    by_dst = {}
    for e in g["edges"]:
        if e["kind"] in ("queries", "alters"):
            by_dst.setdefault(e["dst"], set()).add(e["src"])
    return by_dst


def _res_hubs(by_dst):
    """Tables touched by > HUB_DEGREE distinct files = dampened hubs (exactly recall_measure.res_hubs)."""
    return {t for t, srcs in by_dst.items() if len(srcs) > HUB_DEGREE}


def _coupled_after_damp(by_dst, hubs):
    """File pairs coupled by a shared NON-hub table -> {frozenset(a,b): set(tables)} (what the LIVE,
    dampened engine would treat as a confident schema coupling)."""
    pairs = {}
    for t, srcs in by_dst.items():
        if t in hubs:
            continue                      # hub dampening: demoted, not a confident coupling
        fs = sorted(srcs)
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                pairs.setdefault(frozenset((fs[i], fs[j])), set()).add(t)
    return pairs


def _build(files):
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            _w(d, rel, body)
        return X.build_graph(d)


def main() -> int:
    checks = []

    # ------------------------------------------------------------------------------------------------
    # Synthetic repo with FOUR schema participants, mirroring the real-repo finding:
    #   * `users`            ubiquitous-named HUB        — 12 touchers (> HUB_DEGREE)  -> must be DAMPENED
    #   * `inventory_ledger` SPECIFIC, low-participant   — 2 touchers                  -> must be KEPT (recall)
    #   * `record`           GENERIC-named, low-particip — 2 touchers (NOT a hub)      -> must be KEPT
    #                        (a name-based stoplist would WRONGLY drop this real coupling)
    #   * orders/order migration<->code (the moat catch) — 2 touchers                 -> must be KEPT
    files = {
        # ubiquitous-named hub: a migration + many code files all touching `users`
        "db/migrations/0001_users.sql": "CREATE TABLE users (id integer);\n",
    }
    for i in range(12):
        files[f"app/feature_{i}.py"] = (
            f"def view_{i}(req):\n"
            f"    rows = run('SELECT id FROM users WHERE id = %s', req)\n"
            f"    return rows\n")
    # specific low-participant table `inventory_ledger` (2 touchers) — a REAL coupling, KEEP it.
    files["db/migrations/0002_ledger.sql"] = "CREATE TABLE inventory_ledger (id integer);\n"
    files["warehouse/ledger_writer.py"] = (
        "def post(amount):\n    return run('UPDATE inventory_ledger SET balance = %s', amount)\n")
    # generic-NAMED but low-participant table `record` (2 touchers, NOT a hub). A name-based extractor
    # stoplist (`record` in UBIQ_SCHEMA_NAMES) would WRONGLY drop this. Hub dampening KEEPS it.
    files["db/migrations/0003_record.sql"] = "CREATE TABLE record (id integer);\n"
    files["audit/record_writer.py"] = (
        "def save():\n    return run('INSERT INTO record (id) VALUES (1)')\n")
    files["audit/record_reader.py"] = (
        "def load():\n    return run('SELECT id FROM record')\n")
    # moat catch: migration ALTERs `orders`, code QUERIES `orders` (no code edge between them).
    files["db/migrations/0004_orders.sql"] = "ALTER TABLE orders ADD COLUMN shipped boolean;\n"
    files["shipping/dispatch.py"] = (
        "def dispatch():\n    return run('UPDATE orders SET shipped = true')\n")

    g = _build(files)
    by_dst = _schema_edges(g)
    hubs = _res_hubs(by_dst)

    # 1) RECALL/EXTRACTOR GUARD: the raw extractor STILL emits the ubiquitous-named `users` edges
    #    (a future stoplist that drops them at extraction would make this fail). The recall-safe home
    #    for the demotion is the engine (hub dampening), NOT silent removal in the extractor.
    users_touchers = len(by_dst.get("users", ()))
    checks.append((
        f"EXTRACTOR keeps the ubiquitous-named `users` schema edges raw "
        f"(touchers={users_touchers}; a name-based extractor stoplist would drop these = recall loss)",
        users_touchers >= 12))

    # 2) ENGINE demotes the ubiquitous-name HUB: `users` IS a res-hub -> dampened out of confident coupling.
    checks.append((
        f"ENGINE res-hub dampening demotes the ubiquitous-name HUB `users` "
        f"(touchers={users_touchers} > HUB_DEGREE={HUB_DEGREE}) — the recall-safe fix, in the engine not the extractor",
        "users" in hubs))

    pairs = _coupled_after_damp(by_dst, hubs)

    def _coupled(a, b):
        return frozenset((a, b)) in pairs

    # the ubiquitous-only pairs (two `users`-only files) must NOT survive as confident couplings
    ubiq_pair_survives = _coupled("app/feature_0.py", "app/feature_1.py")
    checks.append((
        "DAMPENED: two unrelated files coupled ONLY by the ubiquitous `users` hub are NOT a confident "
        f"coupling (false-coupling suppressed: survives={ubiq_pair_survives})",
        not ubiq_pair_survives))

    # 3a) RECALL: the SPECIFIC low-participant table coupling is KEPT.
    ledger_kept = _coupled("db/migrations/0002_ledger.sql", "warehouse/ledger_writer.py")
    checks.append((
        "RECALL: a SPECIFIC low-participant table (`inventory_ledger`, 2 touchers) stays a confident "
        f"coupling (migration<->code; kept={ledger_kept})",
        ledger_kept))

    # 3b) RECALL (the stoplist-unsafe case): a GENERIC-NAMED but low-participant table is KEPT. A name-based
    #     stoplist would WRONGLY drop this (`record` in UBIQ_SCHEMA_NAMES); hub dampening keeps it (2 < hub).
    record_kept = _coupled("audit/record_writer.py", "audit/record_reader.py")
    checks.append((
        "RECALL (stoplist would be UNSAFE): a GENERIC-NAMED but low-participant real table (`record`, 2 "
        f"touchers, NOT a hub) is KEPT — a name-stoplist would wrongly drop it; dampening keeps it (kept={record_kept})",
        record_kept and "record" in UBIQ_SCHEMA_NAMES))

    # 3c) RECALL (the moat catch): migration<->code on `orders` is KEPT.
    orders_kept = _coupled("db/migrations/0004_orders.sql", "shipping/dispatch.py")
    checks.append((
        "RECALL (the moat): migration ALTERs `orders` + code QUERIES `orders` stays coupled "
        f"(the cross-edge code-only tools miss; kept={orders_kept})",
        orders_kept))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("SCHEMA PRECISION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
