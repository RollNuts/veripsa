#!/usr/bin/env python3
"""DJANGO POSITIONAL CreateModel recall gate — the positional-first-argument form of a Django
migration's CreateModel op now mints a table node (audit 2026-06-20, parallel to the keyword-form
coverage already in _cg_schema._ORM_CREATEMODEL_RE).

THE BUG (measured on the real django repo):
  _cg_schema mints table nodes from Django migrations via _ORM_CREATEMODEL_RE, which matched ONLY
  the KEYWORD form `CreateModel(name="Order", ...)`. Django equally allows the POSITIONAL form
  `CreateModel("Order", [...])` (its signature is CreateModel(name, fields, ...)). On the real
  django repo, 43 of 69 (62%) CreateModel calls use the positional form and were MISSED -> those
  migrations never minted a table node -> the migration file did not couple (via the shared table)
  to the model files that QUERY that table -> Veripsa FALSELY CLEARED a real migration<->model
  collision. Confirmed example: tests/migrations/test_migrations/0001_initial.py creates "Author"
  and "Tribble" positionally; the extractor returned only ['tribble'] (caught via a separate
  AddField(model_name=...) path) and missed "Author".

THE FIX (additive, recall-only, content-free):
  A sibling regex _ORM_CREATEMODEL_POS_RE captures the FIRST positional string argument of a
  CreateModel( call, consulted at the SAME call site as the keyword form. _norm_table dedup means a
  keyword-form and a positional-form table of the same name produce one identical normalized table.
  Precision guards: anchored from the open paren straight to the first token, so strings DEEPER in
  the argument list (field names / options) cannot match; a variable first arg (CreateModel(var, ..))
  does not match (string-literal only).

WHAT THIS GATE PINS (hermetic — tiny inline string fixtures; content-free, NAMES only; no DB, no
network, no clone):
  1. POSITIONAL CreateModel("Author", [...]) mints the table `author`.
  2. KEYWORD CreateModel(name="Order", ...) STILL mints `order` (no regression).
  3. A non-model positional STRING deeper in the args (a field name) does NOT mint a spurious table.
  4. The realistic django example (Author + Tribble both POSITIONAL) yields BOTH tables.
  5. Dedup: the same model name in both keyword and positional forms yields ONE normalized table.
  6. A variable (non-string) first argument does NOT match (string-literal-anchored precision).
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import _cg_schema as S  # noqa: E402


def main() -> int:
    checks = []

    # 1) POSITIONAL form mints the table.
    pos = 'migrations.CreateModel("Author", [("id", models.AutoField())])'
    pos_tables = S._orm_table_refs(pos)
    checks.append((
        f'POSITIONAL CreateModel("Author", [...]) mints `author` (got {sorted(pos_tables)})',
        "author" in pos_tables))

    # 2) KEYWORD form still mints the table (no regression).
    kw = 'migrations.CreateModel(name="Order", fields=[])'
    kw_tables = S._orm_table_refs(kw)
    checks.append((
        f'KEYWORD CreateModel(name="Order", ...) still mints `order` (no regression; got {sorted(kw_tables)})',
        "order" in kw_tables))

    # 3) A non-model positional STRING deeper in the args (a field name) does NOT mint a table.
    deep = 'migrations.CreateModel("Author", [("should_not_be_a_table", models.CharField())])'
    deep_tables = S._orm_table_refs(deep)
    checks.append((
        "PRECISION: a field-name string deeper in the args does NOT mint a spurious table "
        f"(only `author`; got {sorted(deep_tables)})",
        "author" in deep_tables and "should_not_be_a_table" not in deep_tables))

    # 4) The realistic django example (tests/migrations/test_migrations/0001_initial.py): Author +
    #    Tribble both POSITIONAL -> BOTH tables. (Before the fix this returned only ['tribble'].)
    example = (
        "class Migration(migrations.Migration):\n"
        "    operations = [\n"
        "        migrations.CreateModel(\n"
        '            "Author",\n'
        '            [("id", models.AutoField(primary_key=True)),\n'
        '             ("name", models.CharField(max_length=255))],\n'
        "        ),\n"
        "        migrations.CreateModel(\n"
        '            "Tribble",\n'
        '            [("fluffy", models.BooleanField(default=True))],\n'
        "        ),\n"
        "    ]\n")
    ex_tables = S._orm_table_refs(example)
    checks.append((
        "the django example (Author + Tribble both POSITIONAL) yields BOTH tables "
        f"(got {sorted(ex_tables)})",
        "author" in ex_tables and "tribble" in ex_tables))

    # 5) Dedup: same model name via keyword AND positional -> ONE normalized table.
    both = 'CreateModel(name="Order") ; CreateModel("Order", [])'
    both_tables = S._orm_table_refs(both)
    checks.append((
        "DEDUP: same name in keyword + positional forms yields exactly one normalized table "
        f"(got {sorted(both_tables)})",
        [t for t in both_tables if t == "order"] == ["order"]))

    # 6) A variable (non-string) first argument does NOT match (string-literal precision guard).
    var = 'migrations.CreateModel(model_cls_name, [])'
    var_tables = S._orm_table_refs(var)
    checks.append((
        f"PRECISION: a variable (non-string) first argument does NOT mint a table (got {sorted(var_tables)})",
        "model_cls_name" not in var_tables))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("DJANGO-POSITIONAL-CREATEMODEL GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
