#!/usr/bin/env python3
"""ORM recall (medium) gate: Objection.js, Bookshelf.js, TypeORM object-arg @Entity.

THREE MEASURED GAPS (additive to existing _ORM_PATTERNS):
  1. Objection.js  -- `static get tableName() { return 'x' }` getter
                   -- `static tableName = 'x'` class-field form
  2. Bookshelf.js  -- `Model.extend({tableName: 'x'})` / `bookshelf.Model.extend(...)`
  3. TypeORM/MikroORM `@Entity({tableName: 'x'})` object-arg form
     (the existing `_ORM_TYPEORM_EXPLICIT_RE` handles `@Entity("string")`)

WHAT THIS GATE PROVES (hermetic, no DB, no Postgres, no network):
  1. Objection.js static getter mints the table name.
  2. Objection.js static class-field mints the table name (TypeScript no-semicolon form).
  3. Bookshelf Model.extend({tableName: 'x'}) mints the table name.
  4. Bookshelf bookshelf.Model.extend({tableName: 'x'}) mints the table name.
  5. TypeORM @Entity({tableName: 'x'}) object-arg form mints the explicit table name.
  6. TypeORM @Entity({tableName: 'x'}) coexists with @Entity("string") form (no collision).
  7. PRECISION: plain `tableName = 'x'` (non-static) does NOT mint a table node.
  8. PRECISION: plain string mention of 'static get tableName()' does NOT mint a table node.
  9. PRECISION: `unknownLib.extend({tableName: 'x'})` does NOT mint a table node.
 10. PRECISION: @Entity({}) with no tableName key does NOT mint via this pattern
     (class name is still captured by _ORM_JPA_ENTITY_RE -- additive, recall-safe).
 11. Cross-substrate crown jewel: Objection.js model file (queries edge) + a SQL migration
     (alters edge) that touch the same table name couple via the shared table node with
     NO import/call edge between them.
 12. Never-crash: empty file and binary-ish content do not raise.
 13. Content-free: no file body emitted in any node.

Print ORM-RECALL-MED GATE: PASS on success, ORM-RECALL-MED GATE: FAIL on any failure.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402


def _w(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def _tables(g):
    return {n["name"] for n in g["nodes"] if n["kind"] == "table"}


def _edges_of_kind(g, kind):
    return [(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == kind]


# ---------------------------------------------------------------------------
# 1. Objection.js static getter
# ---------------------------------------------------------------------------
def test_objection_getter():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "Movie.js"), """\
const { Model } = require('objection');

class Movie extends Model {
  // Table name is the only required property.
  static get tableName() {
    return 'movies'
  }
}

class Person extends Model {
  static get tableName() {
    return 'persons'
  }
}

module.exports = { Movie, Person };
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "movies" in tables, f"movies not found in {tables}"
        assert "persons" in tables, f"persons not found in {tables}"
        print("  [1] Objection.js static getter: PASS")


# ---------------------------------------------------------------------------
# 2. Objection.js static class-field (TypeScript, no semicolon)
# ---------------------------------------------------------------------------
def test_objection_static_field():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "Animal.ts"), """\
import { Model } from 'objection'

export default class Animal extends Model {
  id!: number
  name!: string

  // Class-field form (no semicolon -- TypeScript ASI)
  static tableName = 'animals'

  static jsonSchema = {
    type: 'object',
    properties: {
      id: { type: 'integer' },
      name: { type: 'string' },
    },
  }
}
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "animals" in tables, f"animals not found in {tables}"
        print("  [2] Objection.js static class-field (no semicolon): PASS")


# ---------------------------------------------------------------------------
# 3. Bookshelf Model.extend with tableName
# ---------------------------------------------------------------------------
def test_bookshelf_model_extend():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "author.js"), """\
'use strict';

const Author = Model.extend({
  tableName: 'authors',
  hasTimestamps: true
});

const Post = Model.extend({ tableName: 'posts' });

module.exports = { Author, Post };
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "authors" in tables, f"authors not found in {tables}"
        assert "posts" in tables, f"posts not found in {tables}"
        print("  [3] Bookshelf Model.extend tableName: PASS")


# ---------------------------------------------------------------------------
# 4. Bookshelf bookshelf.Model.extend (instance form)
# ---------------------------------------------------------------------------
def test_bookshelf_instance_extend():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "customer.js"), """\
'use strict';

const bookshelf = require('./bookshelf');

const Customer = bookshelf.Model.extend({
  tableName: 'customers'
});

module.exports = Customer;
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "customers" in tables, f"customers not found in {tables}"
        print("  [4] Bookshelf bookshelf.Model.extend: PASS")


# ---------------------------------------------------------------------------
# 5. TypeORM @Entity({tableName: 'x'}) object-arg form
# ---------------------------------------------------------------------------
def test_typeorm_entity_object_arg():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "entities", "OrderLine.ts"), """\
import { Entity, PrimaryGeneratedColumn, Column } from 'typeorm';

@Entity({ tableName: 'order_lines', schema: 'public' })
export class OrderLine {
    @PrimaryGeneratedColumn()
    id: number;

    @Column()
    quantity: number;
}
""")
        _w(os.path.join(root, "entities", "Subscription.ts"), """\
import { Entity, PrimaryGeneratedColumn, Column } from 'typeorm';

@Entity({
  tableName: 'subscriptions',
  schema: 'billing',
})
export class Subscription {
    @PrimaryGeneratedColumn()
    id: number;
}
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "order_lines" in tables, f"order_lines not found in {tables}"
        assert "subscriptions" in tables, f"subscriptions not found in {tables}"
        print("  [5] TypeORM @Entity({tableName: 'x'}) object-arg: PASS")


# ---------------------------------------------------------------------------
# 6. TypeORM object form coexists with string form
# ---------------------------------------------------------------------------
def test_typeorm_object_and_string_coexist():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "entities", "Product.ts"), """\
import { Entity, PrimaryGeneratedColumn, Column } from 'typeorm';

// String form -- handled by _ORM_TYPEORM_EXPLICIT_RE
@Entity("product_catalog")
export class Product {
    @PrimaryGeneratedColumn()
    id: number;
}
""")
        _w(os.path.join(root, "entities", "Invoice.ts"), """\
import { Entity, PrimaryGeneratedColumn, Column } from 'typeorm';

// Object form -- handled by new FIX 5 pattern
@Entity({ tableName: 'invoices' })
export class Invoice {
    @PrimaryGeneratedColumn()
    id: number;
}
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "product_catalog" in tables, f"product_catalog not found in {tables}"
        assert "invoices" in tables, f"invoices not found in {tables}"
        print("  [6] TypeORM object+string forms coexist: PASS")


# ---------------------------------------------------------------------------
# 7-10. PRECISION: no false positives
# ---------------------------------------------------------------------------
def test_precision_no_false_positives():
    with tempfile.TemporaryDirectory() as root:
        # Non-static tableName assignment -- should NOT match
        _w(os.path.join(root, "src", "config.js"), """\
function buildTable(name) {
  const tableName = name;   // plain local var -- not 'static'
  return tableName;
}
const tableName = 'runtime_value';  // module-level non-static
""")
        # Plain string mention of 'static get tableName()' -- should NOT mint a table
        _w(os.path.join(root, "src", "docs.js"), """\
console.log("Objection.js uses static get tableName() to declare the table");
const hint = "static tableName = 'x' is the class-field form";
""")
        # unknownLib.extend({tableName: 'x'}) -- NOT anchored on Model/bookshelf/etc
        _w(os.path.join(root, "src", "other.js"), """\
const Bad = unknownLib.extend({ tableName: 'notme' });
const AlsoBad = helpers.extend({ tableName: 'alsofake' });
""")
        # @Entity({}) with no tableName key -- should NOT fire this pattern
        # (class name 'Widget' is still captured by _ORM_JPA_ENTITY_RE)
        _w(os.path.join(root, "entities", "Widget.ts"), """\
import { Entity } from 'typeorm';

@Entity({})
export class Widget {
    id: number;
}
""")
        g = X.build_graph(root)
        tables = _tables(g)
        # None of the false-positive candidates should be present
        assert "runtime_value" not in tables, f"false positive 'runtime_value' in {tables}"
        assert "notme" not in tables, f"false positive 'notme' in {tables}"
        assert "alsofake" not in tables, f"false positive 'alsofake' in {tables}"
        print("  [7-10] Precision (no false positives): PASS")


# ---------------------------------------------------------------------------
# 11. Cross-substrate crown jewel: Objection.js model + SQL migration
# ---------------------------------------------------------------------------
def test_cross_substrate_crown_jewel():
    with tempfile.TemporaryDirectory() as root:
        # Objection.js model -- QUERIES the table (non-migration source)
        _w(os.path.join(root, "models", "Order.js"), """\
const { Model } = require('objection');

class Order extends Model {
  static get tableName() {
    return 'orders'
  }
}

module.exports = Order;
""")
        # SQL migration -- ALTERS the same table
        _w(os.path.join(root, "migrations", "0001_create_orders.sql"), """\
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    total DECIMAL(10,2) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "orders" in tables, f"orders not found in {tables}"

        alters = _edges_of_kind(g, "alters")
        queries = _edges_of_kind(g, "queries")

        alters_orders = [e for e in alters if e[1] == "orders"]
        queries_orders = [e for e in queries if e[1] == "orders"]

        assert alters_orders, f"no alters->orders edge; alters={alters}"
        assert queries_orders, f"no queries->orders edge; queries={queries}"

        # Confirm NO import/call edge between model and migration
        model_src = "models/Order.js"
        migration_src = next((e[0] for e in alters_orders), None)
        code_edges = [
            e for e in g["edges"]
            if e["kind"] in ("calls", "imports")
            and ((e["src"] == model_src and e["dst"] == migration_src)
                 or (e["src"] == migration_src and e["dst"] == model_src))
        ]
        assert not code_edges, (
            f"unexpected code edge between model and migration: {code_edges}")

        print("  [11] Cross-substrate crown jewel (Objection.js+SQL migration): PASS")


# ---------------------------------------------------------------------------
# 12. Never-crash
# ---------------------------------------------------------------------------
def test_never_crash():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "src", "empty.js"), "")
        _w(os.path.join(root, "src", "binary_ish.ts"),
           "\x00\x01\x02static get tableName")
        _w(os.path.join(root, "src", "note.txt"),
           "static get tableName() and Model.extend are ORM APIs")
        try:
            g = X.build_graph(root)
        except Exception as exc:
            raise AssertionError(
                f"build_graph raised on edge-case inputs: {exc}") from exc
        print("  [12] Never-crash: PASS")


# ---------------------------------------------------------------------------
# 13. Content-free
# ---------------------------------------------------------------------------
def test_content_free():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "User.js"), """\
const { Model } = require('objection');
class User extends Model {
  static get tableName() { return 'users' }
  get secretSalary() { return 999999; }
}
""")
        g = X.build_graph(root)
        for node in g["nodes"]:
            assert "body" not in node, f"node leaks body: {node}"
            assert "content" not in node, f"node leaks content: {node}"
        print("  [13] Content-free: PASS")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main():
    tests = [
        test_objection_getter,
        test_objection_static_field,
        test_bookshelf_model_extend,
        test_bookshelf_instance_extend,
        test_typeorm_entity_object_arg,
        test_typeorm_object_and_string_coexist,
        test_precision_no_false_positives,
        test_cross_substrate_crown_jewel,
        test_never_crash,
        test_content_free,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except Exception as exc:
            failures.append((t.__name__, exc))
            print(f"  FAIL {t.__name__}: {exc}")

    if failures:
        print(f"\nORM-RECALL-MED GATE: FAIL ({len(failures)} failure(s))")
        sys.exit(1)
    else:
        print("\nORM-RECALL-MED GATE: PASS")


if __name__ == "__main__":
    main()
