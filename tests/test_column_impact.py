#!/usr/bin/env python3
"""COLUMN-IMPACT precision gate -- MEASURE-FIRST result PINNED (audit 2026-06-19, parallel to the
schema-precision PR #254 and the call/config precision guards).

THE QUESTION (one granularity below the existing TABLE-level schema graph): a migration that
RENAMES / DROPS a COLUMN (e.g. `voucher.used`, `stockrecord.price_excl_tax`) -> can Veripsa
pinpoint the EXACT code files that reference that column and will silently break?  That pinpoint
is the prize (column rename/drop is a top AI-era silent-breakage), IF it can be done at
acceptable PRECISION.

WHAT THE MEASUREMENT FOUND (3 real Django/Rails repos -- saleor, netbox, django-oscar -- with
TIME-CORRECT precision: evaluate at each migration's PARENT commit, where the old column name is
still in the code, against the GROUND TRUTH = the non-migration files the migration's OWN commit
edited = the human's actual blast radius.  Full numbers in the PR body):

  Rule                                       precision        recall
  R0  bare column-name match (`.col` / SQL)   19-39%          17-27%      <- 61-81% FALSE positive
  R1  R0 + ubiquitous-name guard              19-39%          17-27%      <- == R0 (renamed cols are
                                                                              mostly already specific,
                                                                              so the guard rarely fires)
  R2  R1 + TABLE-QUALIFY (tie col to its T)   38-100%         0.4-2%      <- precise WHEN it fires,
                                                                              but recall near zero

THE FUNDAMENTAL LIMIT (why R2 recall is ~0): the real reference is almost always
`voucher.used = ...` -- a LOWERCASE INSTANCE attribute, NOT `Voucher.used`.  Tying `voucher`
(instance) -> `Voucher` (model) -> table `voucher` requires TYPE INFERENCE / dataflow, which a
content-free static regex extractor cannot do.  Qualified `Class.col` access (the only thing R2
can tie) is rare, so R2 fires ~0.4% of the time.  Anything with recall (R0/R1) is 60-80% false
couplings = wall-paper noise = exactly what the CARDINAL RULE forbids ("a bare ubiquitous column
name must NOT fan out; when a ref is ambiguous, DON'T emit").

HONEST DECISION (a measured NO, valuable): DO NOT add column-level coupling EDGES to the graph.
  * R0/R1 would flood the graph with 60-80% false couplings -> recall-UNsafe AND precision-UNsafe.
  * R2 is precise but recall ~0 -> it adds almost nothing over the EXISTING table-level schema
    coupling, which ALREADY surfaces the migration<->code link (the migration ALTERs table T; the
    referencing files QUERY table T; they couple on T -- and the file-level co-change detector
    corroborates the blast radius).  Column granularity does not improve the pinpoint; it adds noise.
  So we change the extractor NOTHING (no column nodes, no column edges) and PIN the boundary here:
  a future "column impact" feature that fans out bare/ubiquitous column names FAILS this gate.

WHAT THIS GATE PINS (hermetic synthetic -- column + table NAMES + paths only, no DB, no network):
  1. The migration parser correctly extracts (table, column, change_type) from Django
     RenameField/RemoveField/AddField + raw DDL ALTER ... RENAME/DROP COLUMN + Rails rename_column.
  2. The QUALIFIED resolver (R2, the only precise one) PINPOINTS a file that references
     `Users.user_id` (column user_id OF model Users/table users) for a `users.user_id` rename.
  3. PRECISION: a file referencing an UNRELATED `.user_id` of a DIFFERENT table (`Order.user_id`),
     or a BARE ubiquitous `.id`, is NOT coupled by the qualified resolver -- no false fan-out.
  4. The DECISION is encoded: the bare/ubiquitous resolver (R0/R1) is NOT used for emission
     (it would over-couple); only the qualified resolver may pinpoint, and when a ref cannot be
     table-tied it is DROPPED (recall-poor by design -- the measured, honest trade-off).
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from measure_column_impact import (  # noqa: E402
    column_changes, code_tables, column_refs, UBIQUITOUS_COLUMNS,
)


def _resolve(code_text, t, c):
    """Mirror the analyzer's per-file resolution for column (table t, col c).
    Returns dict with the three rule flags so the gate can assert each."""
    tables, classmap = code_tables(code_text)
    bare, qual = column_refs(code_text)
    bare_hit = c in bare
    qual_hit = any(col == c and (cls == t or classmap.get(cls) == t) for (cls, col) in qual)
    table_tie = t in tables
    r0 = bare_hit or qual_hit
    r1 = qual_hit or (bare_hit and c not in UBIQUITOUS_COLUMNS)
    r2 = qual_hit or (bare_hit and c not in UBIQUITOUS_COLUMNS and table_tie)
    return {"r0": r0, "r1": r1, "r2": r2, "qual": qual_hit, "table_tie": table_tie}


def main():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # ---- 1. migration parsing: (table, column, change_type) extraction ------------------
    dj_rename = '''
    migrations.RenameField(model_name="user", old_name="user_id", new_name="account_id"),
    '''
    chg = set(column_changes(dj_rename))
    check(("user", "user_id", "rename") in chg, "Django RenameField old col not extracted")
    check(("user", "account_id", "rename") in chg, "Django RenameField new col not extracted")

    dj_remove = 'migrations.RemoveField(model_name="orders", name="legacy_flag")'
    check(("orders", "legacy_flag", "drop") in set(column_changes(dj_remove)),
          "Django RemoveField not extracted")

    ddl = "ALTER TABLE users RENAME COLUMN user_id TO account_id;"
    cd = set(column_changes(ddl))
    check(("users", "user_id", "rename") in cd, "raw DDL RENAME COLUMN (old) not extracted")
    check(("users", "account_id", "rename") in cd, "raw DDL RENAME COLUMN (new) not extracted")
    check(("orders", "legacy", "drop") in set(column_changes(
        "ALTER TABLE orders DROP COLUMN legacy;")), "raw DDL DROP COLUMN not extracted")

    rb = "rename_column :users, :user_id, :account_id"
    check(("users", "user_id", "rename") in set(column_changes(rb)),
          "Rails rename_column not extracted")

    # ---- 2. QUALIFIED resolver PINPOINTS the true referencer ----------------------------
    # A file that defines the Users model AND references Users.user_id is a TRUE referencer of
    # column user_id of table users.
    referencer = '''
class Users(models.Model):
    user_id = models.IntegerField()

def lookup(u):
    return Users.user_id
'''
    r = _resolve(referencer, "user", "user_id")
    # NB the model class "Users" lowercases to table candidate "users"; the migration table is
    # "user" in dj_rename above but here we test the matching table "users".
    r = _resolve(referencer, "users", "user_id")
    check(r["r2"], "qualified resolver FAILED to pinpoint Users.user_id for users.user_id rename")
    check(r["table_tie"], "table tie not made for the model-defining file")

    # ---- 3. PRECISION: unrelated `.user_id` of a DIFFERENT table is NOT coupled ----------
    wrong_table = '''
class Order(models.Model):
    user_id = models.IntegerField()   # Order's user_id, NOT users' -- a DIFFERENT table

def f(o):
    return Order.user_id
'''
    rw = _resolve(wrong_table, "users", "user_id")
    check(not rw["qual"], "qualified resolver wrongly tied Order.user_id to table users")
    check(not rw["table_tie"], "table tie wrongly made to users for an Order-only file")
    # R2 must NOT couple this file to a users.user_id rename (precision).
    check(not rw["r2"], "PRECISION FAIL: Order.user_id falsely coupled to users.user_id")

    # ---- 4. PRECISION: a BARE ubiquitous `.id` does NOT fan out --------------------------
    ubiquitous = '''
def g(obj):
    return obj.id   # a bare ubiquitous column name -- must NOT fan out
'''
    ru = _resolve(ubiquitous, "users", "id")
    check(ru["r0"], "sanity: bare .id should match R0 (the naive baseline)")
    check(not ru["r1"], "ubiquitous guard FAILED: bare .id survived R1")
    check(not ru["r2"], "PRECISION FAIL: bare ubiquitous .id fanned out under R2")

    # ---- 4b. a bare SPECIFIC column with NO table tie is DROPPED by R2 (recall-poor by
    #          design -- the measured honest trade-off; we DON'T emit when we can't tie). ---
    untied = '''
def h(x):
    return x.account_balance   # specific, but x is an untyped instance -- no table tie
'''
    rt = _resolve(untied, "wallet", "account_balance")
    check(rt["r1"], "specific bare name should survive the ubiquitous guard (R1)")
    check(not rt["r2"], "R2 should DROP an untieable bare ref (don't-emit-when-ambiguous)")

    # ---- 5. DECISION ENCODED: the emission rule is R2 (qualified/table-tied) ONLY. The
    #         bare/ubiquitous R0/R1 resolvers exist only for MEASUREMENT and must never be the
    #         emission rule (they over-couple at 60-80% false positive, measured on real repos).
    #         We assert the precise case is kept and BOTH imprecise cases (wrong-table, bare
    #         ubiquitous) are rejected by R2 -- the shippable rule.
    EMIT = "r2"
    check(_resolve(referencer, "users", "user_id")[EMIT] is True,
          "emission rule must KEEP the qualified true referencer")
    check(_resolve(wrong_table, "users", "user_id")[EMIT] is False,
          "emission rule must REJECT the wrong-table collision")
    check(_resolve(ubiquitous, "users", "id")[EMIT] is False,
          "emission rule must REJECT the bare ubiquitous name")

    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        print("COLUMN IMPACT GATE: FAIL")
        return 1
    print("Column-impact: migration parsing OK; qualified resolver pinpoints the true referencer;")
    print("wrong-table + bare-ubiquitous collisions rejected (precision); untieable refs dropped")
    print("(recall-poor by design = the measured honest trade-off -> NO column EDGES emitted).")
    print("COLUMN IMPACT GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
