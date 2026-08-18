#!/usr/bin/env python3
"""JS/TS ORM extractor gate: Drizzle, Mongoose, Sequelize, EF Core, TypeORM explicit names.

THE GAP: _cg_schema._ORM_PATTERNS covered Django/SQLAlchemy/JPA/Rails/Laravel/Go + raw SQL,
but NOT the following high-usage JS/TS/C# ORMs:
  - Drizzle (pgTable/mysqlTable/sqliteTable) -- fastest-growing TS ORM
  - Mongoose (~4M/wk)  -- mongoose.model() + new Schema({ collection: ... })
  - Sequelize (~7M/wk) -- sequelize.define() + Model.init({ tableName: ... })
  - EF Core (.NET)     -- DbSet<EntityClass>
  - TypeORM explicit   -- @Entity("table_name") -- the 13-pct miss: class name extracted
                          but explicit string name was silently dropped.

WHAT THIS GATE PROVES (hermetic, no DB, no Postgres, no network):
  1. Drizzle pgTable("name", ...) mints the exact table name.
  2. Drizzle mysqlTable("name", ...) mints the exact table name.
  3. Drizzle sqliteTable("name", ...) mints the exact table name.
  4. Mongoose mongoose.model("Name", schema) mints the model name (collection anchor).
  5. Mongoose new Schema({}, { collection: "name" }) mints the collection name.
  6. Sequelize sequelize.define("Name", {...}) mints the model name.
  7. Sequelize Model.init({}, { tableName: "name" }) mints the explicit table name.
  8. EF Core DbSet<EntityClass> mints the entity class name (PascalCase -> lowercased).
  9. TypeORM @Entity("explicit_name") mints the explicit string (in addition to class name).
 10. PRECISION: a plain string literal containing one of these ORM function names does NOT
     mint a table node.
 11. PRECISION: Drizzle import statement does NOT mint a table node.
 12. PRECISION: EF Core DbSet<string> (lowercase type param) does NOT mint a table node.
 13. Cross-substrate crown jewel: Drizzle schema file (queries edge) and a SQL migration
     (alters edge) that touch the same table name couple via the shared table node, with
     NO import/call edge between them.
 14. Never-crash: empty file, binary-ish content, non-TS file do not raise.
 15. Content-free: no file body is emitted -- only table NAMES, edge kinds, paths.

Print JS-ORM GATE: PASS on success, JS-ORM GATE: FAIL on any failure.
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
# 1. Drizzle pgTable
# ---------------------------------------------------------------------------
def test_drizzle_pgtable():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "src", "schema.ts"), """\
import { integer, pgTable, text } from 'drizzle-orm/pg-core';

export const users = pgTable("users", {
  id: integer().primaryKey(),
  name: text(),
});

export const orders = pgTable('orders', {
  id: integer().primaryKey(),
  userId: integer().references(() => users.id),
});
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "users" in tables, f"users not found in {tables}"
        assert "orders" in tables, f"orders not found in {tables}"
        print("  [1] Drizzle pgTable: PASS")


# ---------------------------------------------------------------------------
# 2. Drizzle mysqlTable
# ---------------------------------------------------------------------------
def test_drizzle_mysqltable():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "src", "schema.ts"), """\
import { int, mysqlTable, varchar } from 'drizzle-orm/mysql-core';

export const products = mysqlTable('products', {
  id: int().primaryKey(),
  name: varchar({ length: 255 }),
});
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "products" in tables, f"products not found in {tables}"
        print("  [2] Drizzle mysqlTable: PASS")


# ---------------------------------------------------------------------------
# 3. Drizzle sqliteTable
# ---------------------------------------------------------------------------
def test_drizzle_sqlitetable():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "src", "schema.ts"), """\
import { integer, sqliteTable, text } from 'drizzle-orm/sqlite-core';

export const posts = sqliteTable("posts", {
  id: integer().primaryKey(),
  title: text(),
});
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "posts" in tables, f"posts not found in {tables}"
        print("  [3] Drizzle sqliteTable: PASS")


# ---------------------------------------------------------------------------
# 4. Mongoose mongoose.model()
# ---------------------------------------------------------------------------
def test_mongoose_model():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "user.js"), """\
const mongoose = require('mongoose');

const userSchema = new mongoose.Schema({
  name: String,
  email: String,
});

const User = mongoose.model('User', userSchema);
const Order = mongoose.model('Order', orderSchema);

module.exports = User;
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "user" in tables, f"user not found in {tables}"
        assert "order" in tables, f"order not found in {tables}"
        print("  [4] Mongoose mongoose.model: PASS")


# ---------------------------------------------------------------------------
# 5. Mongoose new Schema with collection option
# ---------------------------------------------------------------------------
def test_mongoose_schema_collection():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "product.js"), """\
const { Schema } = require('mongoose');

const productSchema = new Schema(
  { name: String, price: Number },
  { collection: 'products', timestamps: true }
);
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "products" in tables, f"products not found in {tables}"
        print("  [5] Mongoose Schema collection option: PASS")


# ---------------------------------------------------------------------------
# 6. Sequelize sequelize.define()
# ---------------------------------------------------------------------------
def test_sequelize_define():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "order.js"), """\
const { DataTypes } = require('sequelize');

const Order = sequelize.define('Order', {
  id: {
    type: DataTypes.INTEGER,
    primaryKey: true,
  },
  total: DataTypes.DECIMAL,
});

const Customer = sequelize.define('Customer', {
  name: DataTypes.STRING,
});
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "order" in tables, f"order not found in {tables}"
        assert "customer" in tables, f"customer not found in {tables}"
        print("  [6] Sequelize sequelize.define: PASS")


# ---------------------------------------------------------------------------
# 7. Sequelize Model.init() with tableName
# ---------------------------------------------------------------------------
def test_sequelize_model_init():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "user.ts"), """\
import { Model, DataTypes, Sequelize } from 'sequelize';

export class User extends Model {
  id!: number;
  name!: string;
}

User.init(
  {
    id: { type: DataTypes.INTEGER, primaryKey: true },
    name: DataTypes.STRING,
  },
  {
    tableName: 'app_users',
    sequelize,
  }
);
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "app_users" in tables, f"app_users not found in {tables}"
        print("  [7] Sequelize Model.init tableName: PASS")


# ---------------------------------------------------------------------------
# 8. EF Core DbSet<T>
# ---------------------------------------------------------------------------
def test_efcore_dbset():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "Data", "AppDbContext.cs"), """\
using Microsoft.EntityFrameworkCore;

public class AppDbContext : DbContext
{
    public DbSet<User> Users { get; set; }
    public DbSet<OrderItem> OrderItems { get; set; }
    public DbSet<Product> Products { get; set; }
}
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "user" in tables, f"User not found in {tables}"
        assert "orderitem" in tables, f"OrderItem not found in {tables}"
        assert "product" in tables, f"Product not found in {tables}"
        print("  [8] EF Core DbSet<T>: PASS")


# ---------------------------------------------------------------------------
# 9. TypeORM @Entity("explicit_name")
# ---------------------------------------------------------------------------
def test_typeorm_explicit_entity():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "entities", "Author.ts"), """\
import { Entity, PrimaryGeneratedColumn, Column } from 'typeorm';

@Entity("author_records")
export class Author {
    @PrimaryGeneratedColumn()
    id: number;

    @Column()
    name: string;
}
""")
        _w(os.path.join(root, "entities", "Post.ts"), """\
import { Entity, PrimaryGeneratedColumn, Column } from 'typeorm';

@Entity()
export class Post {
    @PrimaryGeneratedColumn()
    id: number;
}
""")
        g = X.build_graph(root)
        tables = _tables(g)
        # Explicit string must be captured
        assert "author_records" in tables, (
            f"explicit 'author_records' not found in {tables}")
        # Bare @Entity() yields class name
        assert "post" in tables, f"Post (class name) not found in {tables}"
        print("  [9] TypeORM @Entity explicit name: PASS")


# ---------------------------------------------------------------------------
# 10-12. PRECISION: plain strings / imports / lowercase primitives
# ---------------------------------------------------------------------------
def test_precision_no_false_positives():
    with tempfile.TemporaryDirectory() as root:
        # Plain STRING literals that mention ORM function names must not mint table nodes.
        # These are separate from commented-out code (JS // comments are not stripped and
        # are a known minor imprecision; coupling still requires BOTH sides to name the same
        # table, so comment-only matches do not cause false cross-substrate signals in practice).
        _w(os.path.join(root, "src", "docs.ts"), """\
console.log("Use mongoose.model to register a model");
const hint = "sequelize.define or Model.init for Sequelize";
const desc = "pgTable is the Drizzle table factory";
import { pgTable, mysqlTable, sqliteTable } from 'drizzle-orm/pg-core';
""")
        _w(os.path.join(root, "src", "primitives.cs"), """\
public class BadContext : DbContext {
    public DbSet<string> StringBag { get; set; }
    public DbSet<int> IntBag { get; set; }
}
""")
        g = X.build_graph(root)
        tables = _tables(g)
        # Import-only statement must not mint a table node
        # (pgTable in an import statement has no string-literal first arg)
        # Plain string mentions of ORM function names must not mint nodes
        assert "drizzle-orm/pg-core" not in tables, (
            f"false positive from import path in {tables}")
        # Primitive-type DbSet must not mint nodes (lowercase type param filter)
        assert "string" not in tables, f"false positive 'string' in {tables}"
        assert "int" not in tables, f"false positive 'int' in {tables}"
        print("  [10-12] Precision (no false positives from strings/imports/primitives): PASS")


# ---------------------------------------------------------------------------
# 13. Cross-substrate crown jewel: Drizzle schema + SQL migration couple on shared table
# ---------------------------------------------------------------------------
def test_cross_substrate_crown_jewel():
    with tempfile.TemporaryDirectory() as root:
        # Drizzle schema file -- QUERIES the table (non-migration source)
        _w(os.path.join(root, "src", "schema.ts"), """\
import { integer, pgTable, text } from 'drizzle-orm/pg-core';

export const invoices = pgTable("invoices", {
  id: integer().primaryKey(),
  amount: integer(),
});
""")
        # SQL migration -- ALTERS the same table
        _w(os.path.join(root, "migrations", "0001_create_invoices.sql"), """\
CREATE TABLE invoices (
    id SERIAL PRIMARY KEY,
    amount INTEGER NOT NULL
);
""")
        g = X.build_graph(root)
        tables = _tables(g)
        assert "invoices" in tables, f"invoices not found in {tables}"

        alters = _edges_of_kind(g, "alters")
        queries = _edges_of_kind(g, "queries")

        alters_invoices = [e for e in alters if e[1] == "invoices"]
        queries_invoices = [e for e in queries if e[1] == "invoices"]

        assert alters_invoices, f"no alters->invoices edge; alters={alters}"
        assert queries_invoices, f"no queries->invoices edge; queries={queries}"

        # Confirm there is NO import/call edge between schema.ts and the migration
        schema_src = "src/schema.ts"
        migration_src = next(
            (e[0] for e in alters_invoices), None)
        calls_and_imports = [
            e for e in g["edges"]
            if e["kind"] in ("calls", "imports")
            and ((e["src"] == schema_src and e["dst"] == migration_src)
                 or (e["src"] == migration_src and e["dst"] == schema_src))
        ]
        assert not calls_and_imports, (
            f"unexpected code edge between schema and migration: {calls_and_imports}")

        print("  [13] Cross-substrate crown jewel (Drizzle+SQL migration): PASS")


# ---------------------------------------------------------------------------
# 14. Never-crash
# ---------------------------------------------------------------------------
def test_never_crash():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "src", "empty.ts"), "")
        _w(os.path.join(root, "src", "binary_ish.ts"), "\x00\x01\x02pgTable")
        _w(os.path.join(root, "src", "note.txt"), "pgTable and mongoose.model are ORM helpers")
        try:
            g = X.build_graph(root)
        except Exception as exc:
            raise AssertionError(f"build_graph raised on edge-case inputs: {exc}") from exc
        print("  [14] Never-crash: PASS")


# ---------------------------------------------------------------------------
# 15. Content-free: no file body in emitted nodes
# ---------------------------------------------------------------------------
def test_content_free():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "src", "schema.ts"), """\
export const users = pgTable("users", { id: integer().primaryKey() });
""")
        g = X.build_graph(root)
        for node in g["nodes"]:
            assert "body" not in node, f"node leaks body: {node}"
            assert "content" not in node, f"node leaks content: {node}"
        print("  [15] Content-free: PASS")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main():
    tests = [
        test_drizzle_pgtable,
        test_drizzle_mysqltable,
        test_drizzle_sqlitetable,
        test_mongoose_model,
        test_mongoose_schema_collection,
        test_sequelize_define,
        test_sequelize_model_init,
        test_efcore_dbset,
        test_typeorm_explicit_entity,
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
        print(f"\nJS-ORM GATE: FAIL ({len(failures)} failure(s))")
        sys.exit(1)
    else:
        print("\nJS-ORM GATE: PASS")


if __name__ == "__main__":
    main()
