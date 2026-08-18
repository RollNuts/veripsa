#!/usr/bin/env python3
"""Go gorm + ent ORM extractor gate.

THE GAP: _cg_schema._ORM_PATTERNS covered Django/SQLAlchemy/JPA/Rails/Laravel + raw SQL,
but NOT Go. gorm and ent are the dominant Go ORM frameworks; apps using them extracted
0 table nodes → the cross-substrate crown jewel (migration alters T + model queries T
with NO import/call edge between them) was INVISIBLE for Go repos.

WHAT THIS GATE PROVES (hermetic, no DB, no Postgres, no network):
  1. gorm explicit TableName(): `func (User) TableName() string { return "users" }` mints
     the exact return-value table name (not a pluralisation guess).
  2. gorm struct embedding gorm.Model: `type Order struct { gorm.Model; ... }` mints
     snake_case-plural table name (`orders`).
  3. gorm struct with gorm field tag: `gorm:"column:name"` in a struct field mints the
     struct name as a snake_case-plural table candidate.
  4. ent explicit entsql.Annotation{Table: "orders"}: mints the exact table name.
  5. ent ent.Schema embedding + Fields() method: mints snake_case-plural table name.
  6. PRECISION — plain Go struct (no gorm.Model, no gorm tag, no ent.Schema): does NOT
     mint a table node. This is the critical precision guard.
  7. PRECISION — Go doc-comment examples (`// type T struct { ent.Schema }`): does NOT
     fire (comment stripping prevents false positives from framework doc comments).
  8. Cross-substrate crown jewel: a gorm migration (.go, in a migrations dir) alters T
     and a gorm model file (.go, outside migrations) queries T — coupled via the shared
     table node with CONFIRMED absence of any calls/imports edge between the two files.
  9. Never-crash: empty Go file, binary-ish content, and non-Go files do not raise.
 10. Content-free: no file body is emitted — only table NAMES, edge kinds, paths.

Print `GORM-ENT GATE: PASS` on success, `GORM-ENT GATE: FAIL` on any failure.
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
# 1. gorm explicit TableName() return value
# ---------------------------------------------------------------------------
def test_gorm_explicit_table_name():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "user.go"), """\
package models

import "gorm.io/gorm"

type User struct {
\tgorm.Model
\tEmail string
}

func (User) TableName() string {
\treturn "user_accounts"
}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "user_accounts" in tbls, \
            f"Expected 'user_accounts' from TableName(); got {tbls}"
    print("  [ok] gorm explicit TableName() return value")


# ---------------------------------------------------------------------------
# 2. gorm struct embedding gorm.Model (snake_case plural)
# ---------------------------------------------------------------------------
def test_gorm_model_embedded():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "order.go"), """\
package models

import "gorm.io/gorm"

type Order struct {
\tgorm.Model
\tAmount  float64
\tStatus  string
}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "orders" in tbls, \
            f"Expected 'orders' from gorm.Model embedding; got {tbls}"
    print("  [ok] gorm.Model embedded -> snake_case plural table name")


# ---------------------------------------------------------------------------
# 3. gorm struct with gorm field tag (no gorm.Model)
# ---------------------------------------------------------------------------
def test_gorm_struct_tag():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "product.go"), """\
package models

type Product struct {
\tID    uint   `gorm:"primaryKey"`
\tName  string `gorm:"column:name;not null"`
\tPrice float64
}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "products" in tbls, \
            f"Expected 'products' from gorm struct tag; got {tbls}"
    print("  [ok] gorm struct field tag -> snake_case plural table name")


# ---------------------------------------------------------------------------
# 4. ent explicit entsql.Annotation{Table: ...}
# ---------------------------------------------------------------------------
def test_ent_explicit_annotation():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "ent", "schema", "invoice.go"), """\
package schema

import (
\t"entgo.io/ent"
\t"entgo.io/ent/dialect/entsql"
\t"entgo.io/ent/schema"
\t"entgo.io/ent/schema/field"
)

// Invoice holds the schema definition for the Invoice entity.
type Invoice struct {
\tent.Schema
}

// Annotations of the Invoice.
func (Invoice) Annotations() []schema.Annotation {
\treturn []schema.Annotation{
\t\tentsql.Annotation{Table: "billing_invoices"},
\t}
}

// Fields of the Invoice.
func (Invoice) Fields() []ent.Field {
\treturn []ent.Field{
\t\tfield.Float("amount"),
\t}
}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "billing_invoices" in tbls, \
            f"Expected 'billing_invoices' from entsql.Annotation; got {tbls}"
    print("  [ok] ent entsql.Annotation{Table: ...} -> explicit table name")


# ---------------------------------------------------------------------------
# 5. ent.Schema embedding + Fields() -> snake_case plural
# ---------------------------------------------------------------------------
def test_ent_schema_fields():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "ent", "schema", "payment.go"), """\
package schema

import (
\t"entgo.io/ent"
\t"entgo.io/ent/schema/field"
)

// Payment holds the schema definition for the Payment entity.
type Payment struct {
\tent.Schema
}

// Fields of the Payment.
func (Payment) Fields() []ent.Field {
\treturn []ent.Field{
\t\tfield.Float("amount"),
\t\tfield.String("currency"),
\t}
}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "payments" in tbls, \
            f"Expected 'payments' from ent.Schema + Fields(); got {tbls}"
    print("  [ok] ent.Schema + Fields() -> snake_case plural table name")


# ---------------------------------------------------------------------------
# 6. PRECISION: plain Go struct (no ORM marker) must NOT mint a table
# ---------------------------------------------------------------------------
def test_precision_plain_struct():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "pkg", "types.go"), """\
package pkg

// Config is a plain Go struct with no ORM marker.
type Config struct {
\tHost string
\tPort int
\tTimeout int
}

// Response is another plain struct.
type Response struct {
\tStatus  int
\tBody    string
\tHeaders map[string]string
}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "configs" not in tbls, \
            f"PRECISION FAILURE: plain struct 'Config' minted 'configs' table; got {tbls}"
        assert "responses" not in tbls, \
            f"PRECISION FAILURE: plain struct 'Response' minted 'responses' table; got {tbls}"
    print("  [ok] PRECISION: plain Go struct without ORM marker -> no table node")


# ---------------------------------------------------------------------------
# 7. PRECISION: Go doc-comment examples must NOT mint tables
# ---------------------------------------------------------------------------
def test_precision_doc_comment():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "ent.go"), """\
// Package ent is the framework package.
// Users should embed as follows:
//
//\ttype T struct {
//\t\tent.Schema
//\t}
//
// Or for gorm:
//
//\ttype User struct {
//\t\tgorm.Model
//\t}

package ent

// Schema is the base type.
type Schema struct{}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "ts" not in tbls, \
            f"PRECISION FAILURE: doc-comment 'type T struct' minted 'ts'; got {tbls}"
        assert "users" not in tbls, \
            f"PRECISION FAILURE: doc-comment 'type User struct' minted 'users'; got {tbls}"
    print("  [ok] PRECISION: Go doc-comment ORM examples -> no table nodes")


# ---------------------------------------------------------------------------
# 8. Cross-substrate crown jewel: gorm migration alters T, model queries T
#    — no import/call edge between the two files.
# ---------------------------------------------------------------------------
def test_gorm_cross_substrate_coupling():
    with tempfile.TemporaryDirectory() as root:
        # Migration file — in a migrations/ dir so _is_migration() fires -> alters edge
        _w(os.path.join(root, "migrations", "20240101_create_orders.go"), """\
package migrations

import "gorm.io/gorm"

type OrderV1 struct {
\tgorm.Model
\tStatus string
}

func MigrateOrders(db *gorm.DB) error {
\treturn db.AutoMigrate(&OrderV1{})
}
""")
        # Model file — outside migrations/ -> queries edge
        _w(os.path.join(root, "models", "order.go"), """\
package models

import "gorm.io/gorm"

type OrderV1 struct {
\tgorm.Model
\tStatus string
}

func FindOrder(db *gorm.DB, id uint) (*OrderV1, error) {
\tvar o OrderV1
\tresult := db.First(&o, id)
\treturn &o, result.Error
}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "order_v1s" in tbls, f"Expected 'order_v1s' table node; got {tbls}"

        alters = _edges_of_kind(g, "alters")
        queries = _edges_of_kind(g, "queries")

        assert any(dst == "order_v1s" for _, dst in alters), \
            f"Expected alters->order_v1s from migration file; alters={alters}"
        assert any(dst == "order_v1s" for _, dst in queries), \
            f"Expected queries->order_v1s from model file; queries={queries}"

        # Confirm no import/call edge between the migration and the model file
        code_edge_kinds = {"calls", "imports"}
        mig_src = next(
            (e["src"] for e in g["edges"] if e["kind"] == "alters" and e["dst"] == "order_v1s"),
            None)
        model_src = next(
            (e["src"] for e in g["edges"] if e["kind"] == "queries" and e["dst"] == "order_v1s"),
            None)
        if mig_src and model_src:
            cross = [
                e for e in g["edges"]
                if e["kind"] in code_edge_kinds
                and {e["src"], e["dst"]} == {mig_src, model_src}
            ]
            assert not cross, \
                f"Expected NO calls/imports between migration and model; got {cross}"

    print("  [ok] Cross-substrate crown jewel: gorm migration alters + model queries, no code edge")


# ---------------------------------------------------------------------------
# 9. Never-crash: empty file, binary-ish content, non-Go files
# ---------------------------------------------------------------------------
def test_never_crash():
    with tempfile.TemporaryDirectory() as root:
        # Empty Go file
        _w(os.path.join(root, "empty.go"), "")
        # Binary-ish content with embedded gorm pattern
        path = os.path.join(root, "mixed.go")
        with open(path, "wb") as fh:
            fh.write(b"\x00\xff\xfe type Crash struct { gorm.Model }\n")
        # Non-Go file
        _w(os.path.join(root, "README.txt"), "type User struct { gorm.Model }")
        try:
            g = X.build_graph(root)
        except Exception as exc:
            raise AssertionError(f"build_graph raised on pathological input: {exc}") from exc
    print("  [ok] Never-crash: empty + binary-ish + non-Go inputs")


# ---------------------------------------------------------------------------
# 10. Content-free: no file body emitted
# ---------------------------------------------------------------------------
def test_content_free():
    secret = "TOP_SECRET_SALARY_DATA_DO_NOT_EMIT"
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "models", "salary.go"), f"""\
package models

import "gorm.io/gorm"

// {secret}
type Salary struct {{
\tgorm.Model
\tAmount float64 `gorm:"column:amount"`
}}
""")
        g = X.build_graph(root)
        graph_str = str(g)
        assert secret not in graph_str, \
            f"Secret file body leaked into graph output: found {secret!r}"
    print("  [ok] Content-free: no file body emitted")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main():
    tests = [
        test_gorm_explicit_table_name,
        test_gorm_model_embedded,
        test_gorm_struct_tag,
        test_ent_explicit_annotation,
        test_ent_schema_fields,
        test_precision_plain_struct,
        test_precision_doc_comment,
        test_gorm_cross_substrate_coupling,
        test_never_crash,
        test_content_free,
    ]
    passed = 0
    failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            print(f"  [FAIL] {fn.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  [ERROR] {fn.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed}/{len(tests)} checks passed")
    if failed == 0:
        print("GORM-ENT GATE: PASS")
    else:
        print("GORM-ENT GATE: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
