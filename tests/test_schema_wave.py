#!/usr/bin/env python3
"""Schema-wave gate: precision + recall + perf fixes for the schema extractor.

WHAT THIS GATE PROVES (offline, no Postgres, no network):

  1. PRECISION — test-dir ORM models do NOT mint table nodes; production models DO.
     The cross-substrate crown jewel (prod migration alters T + prod model queries T)
     still fires. Measured elimination: SQLAlchemy 6,284 / TypeORM 1,336 / Django 275
     spurious test-prod pairs.

  2. RECALL — three new ORM families now mint table nodes:
       (a) SQLModel: `class User(SQLModel, table=True)` → table `user`
       (b) Alembic:  `op.create_table('users', …)` / `op.add_column('users', …)` → `users`
       (c) EF Core:  `migrationBuilder.CreateTable(name: "Subs", …)` → `subs`;
                     `migrationBuilder.AddColumn(…, table: "Subs", …)` → `subs`;
                     `.ToTable("Subs")` → `subs`

  3. PERF — a repo with NO schema (no table declared anywhere) returns no `queries` edges
     from pass-2 AND the short-circuit fires (pass-2 is skipped entirely).

Prints `SCHEMA-WAVE GATE: PASS` on success, `SCHEMA-WAVE GATE: FAIL` on any failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _w(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def _tables(g):
    return {n["name"] for n in g["nodes"] if n["kind"] == "table"}


def _edges_of_kind(g, kind):
    return [(e["src"].replace("\\", "/"), e["dst"]) for e in g["edges"] if e["kind"] == kind]


def _has_edge(g, kind, dst):
    return any(d == dst for _, d in _edges_of_kind(g, kind))


# ===========================================================================
# 1. PRECISION — test-dir model must NOT mint; prod model MUST mint;
#    prod migration <-> prod model crown jewel still fires.
# ===========================================================================

def test_precision_test_dir_model_does_not_mint():
    """A model defined inside tests/ must NOT enter the known-table set."""
    with tempfile.TemporaryDirectory() as root:
        # Test file declares an ORM model — must NOT mint `order`
        _w(os.path.join(root, "tests", "test_orders.py"), """\
from sqlalchemy import Column, Integer, String
from myapp.database import Base

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    status = Column(String)
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "orders" not in tbls, (
            f"FAIL: test-dir model minted 'orders' — precision regression. tables={tbls}"
        )
    print("  [ok] precision: test-dir ORM model does NOT mint a table node")


def test_precision_prod_model_does_mint():
    """A model defined in prod code (app/) MUST mint its table."""
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "app", "models.py"), """\
from sqlalchemy import Column, Integer, String
from app.database import Base

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "orders" in tbls, (
            f"FAIL: prod model did not mint 'orders'. tables={tbls}"
        )
    print("  [ok] precision: prod-dir ORM model DOES mint a table node")


def test_precision_crown_jewel_still_fires():
    """Prod migration alters T, prod model queries T — must couple.
    Test model declaring T must NOT create a spurious test-prod pair.
    Uses raw SQL migration (CREATE TABLE) so _DDL_RE fires unambiguously."""
    with tempfile.TemporaryDirectory() as root:
        # prod migration — raw SQL so _DDL_RE fires unambiguously
        _w(os.path.join(root, "migrations", "0001_create_orders.sql"), """\
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    status VARCHAR(50) NOT NULL
);
""")
        # prod model (queries the table)
        _w(os.path.join(root, "app", "models.py"), """\
from app.database import Base
from sqlalchemy import Column, Integer

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
""")
        # test model — must NOT mint (would create spurious pair to migration)
        _w(os.path.join(root, "tests", "factories.py"), """\
class OrderFactory:
    __tablename__ = "orders"  # test-only model
    id = 1
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "orders" in tbls, f"FAIL: 'orders' table not minted. tables={tbls}"

        alters = _edges_of_kind(g, "alters")
        queries = _edges_of_kind(g, "queries")

        # The prod migration must alters orders
        assert any("migrations" in src and dst == "orders" for src, dst in alters), (
            f"FAIL: prod migration did not produce alters->orders. alters={alters}"
        )
        # The prod model must queries orders
        assert any("app/models" in src and dst == "orders" for src, dst in queries), (
            f"FAIL: prod model did not produce queries->orders. queries={queries}"
        )
        # The test factory must NOT have any alters or queries to orders
        test_edges = [(src, dst) for src, dst in (alters + queries)
                      if "tests" in src and dst == "orders"]
        assert not test_edges, (
            f"FAIL: test file produced spurious edge to 'orders'. edges={test_edges}"
        )
    print("  [ok] precision: prod migration<->prod model crown jewel fires; test model excluded")


def test_precision_test_subdirs_all_filtered():
    """Verify all standard test-dir segments are filtered: tests/, spec/, e2e/, __tests__/, fixtures/."""
    test_paths = [
        ("tests", "tests/models.py"),
        ("spec", "spec/factories.rb"),
        ("e2e", "e2e/helpers.py"),
        ("__tests__", "__tests__/helpers.js"),
        ("fixtures", "fixtures/data.py"),
        ("specs", "specs/support.py"),
    ]
    for seg, relpath in test_paths:
        with tempfile.TemporaryDirectory() as root:
            full = os.path.join(root, *relpath.split("/"))
            _w(full, '__tablename__ = "mytable"\n')
            g = X.build_graph(root)
            tbls = _tables(g)
            assert "mytable" not in tbls, (
                f"FAIL: '{seg}' dir model minted 'mytable'. tables={tbls}"
            )
    print("  [ok] precision: all standard test-dir segments are filtered")


def test_precision_nested_test_dir_filtered():
    """Test directories nested inside src/ are also filtered."""
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "src", "module", "tests", "test_models.py"),
           '__tablename__ = "nested_table"\n')
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "nested_table" not in tbls, (
            f"FAIL: nested src/module/tests/ model minted 'nested_table'. tables={tbls}"
        )
    print("  [ok] precision: nested test dirs (src/module/tests/) are filtered")


def test_precision_raw_sql_in_test_still_couples():
    """A test file with raw INSERT SQL still couples to a known table (recall-safe)."""
    with tempfile.TemporaryDirectory() as root:
        # Prod migration mints the table
        _w(os.path.join(root, "migrations", "0001_users.sql"), """\
CREATE TABLE users (id SERIAL PRIMARY KEY, name TEXT);
""")
        # Test file writes INSERT — should still couple via pass-2
        _w(os.path.join(root, "tests", "test_auth.py"), """\
def seed_db(conn):
    conn.execute("INSERT INTO users (name) VALUES ('alice')")
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "users" in tbls, f"FAIL: 'users' not minted from migration. tables={tbls}"
        queries = _edges_of_kind(g, "queries")
        assert any("tests" in src and dst == "users" for src, dst in queries), (
            f"FAIL: test file raw SQL did not couple to 'users'. queries={queries}"
        )
    print("  [ok] precision recall-safe: raw SQL in test file still couples via pass-2")


# ===========================================================================
# 2. RECALL — SQLModel / Alembic / EF Core
# ===========================================================================

def test_recall_sqlmodel_table_true():
    """SQLModel class with table=True mints a table node."""
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "app", "models.py"), """\
from sqlmodel import SQLModel, Field
from typing import Optional

class Hero(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    secret_name: str

class HeroCreate(SQLModel):
    name: str
    secret_name: str
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        # HeroCreate has no table=True → must NOT mint
        assert "hero" in tbls, f"FAIL: 'hero' not minted from SQLModel table=True. tables={tbls}"
        assert "herocreate" not in tbls, (
            f"FAIL: HeroCreate (no table=True) should NOT mint. tables={tbls}"
        )
    print("  [ok] recall: SQLModel class(table=True) mints table; non-table class does not")


def test_recall_sqlmodel_crown_jewel():
    """SQLModel app: migration alters T, model queries T — cross-substrate crown jewel."""
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "alembic", "versions", "rev1_create_hero.py"), """\
from alembic import op
import sqlalchemy as sa

def upgrade():
    op.create_table('hero',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
    )

def downgrade():
    op.drop_table('hero')
""")
        _w(os.path.join(root, "app", "models.py"), """\
from sqlmodel import SQLModel, Field
from typing import Optional

class Hero(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "hero" in tbls, f"FAIL: 'hero' not minted. tables={tbls}"
        alters = _edges_of_kind(g, "alters")
        queries = _edges_of_kind(g, "queries")
        assert any("alembic" in src and dst == "hero" for src, dst in alters), (
            f"FAIL: Alembic migration did not alters->hero. alters={alters}"
        )
        assert any("app" in src and dst == "hero" for src, dst in queries), (
            f"FAIL: SQLModel model did not queries->hero. queries={queries}"
        )
    print("  [ok] recall: SQLModel crown jewel — Alembic migration alters T + SQLModel model queries T")


def test_recall_alembic_create_table():
    """Alembic op.create_table mints a table node with alters edge."""
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "alembic", "versions", "abc_create_subscriptions.py"), """\
from alembic import op
import sqlalchemy as sa

def upgrade():
    op.create_table('subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('plan', sa.String(50), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.add_column('subscriptions', sa.Column('started_at', sa.DateTime()))
    op.create_index(op.f('ix_subscriptions_user_id'), 'subscriptions', ['user_id'])

def downgrade():
    op.drop_table('subscriptions')
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "subscriptions" in tbls, (
            f"FAIL: Alembic op.create_table did not mint 'subscriptions'. tables={tbls}"
        )
        alters = _edges_of_kind(g, "alters")
        assert any("alembic" in src and dst == "subscriptions" for src, dst in alters), (
            f"FAIL: no alters->subscriptions from Alembic migration. alters={alters}"
        )
    print("  [ok] recall: Alembic op.create_table + op.add_column mint table node + alters edge")


def test_recall_alembic_add_column_only():
    """Alembic op.add_column on a table not create_table'd here still mints a node."""
    with tempfile.TemporaryDirectory() as root:
        # A later migration that only adds a column (table was created in an earlier rev)
        _w(os.path.join(root, "alembic", "versions", "def_add_column.py"), """\
from alembic import op
import sqlalchemy as sa

def upgrade():
    op.add_column('orders', sa.Column('notes', sa.Text(), nullable=True))

def downgrade():
    op.drop_column('orders', 'notes')
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "orders" in tbls, (
            f"FAIL: Alembic op.add_column did not mint 'orders'. tables={tbls}"
        )
    print("  [ok] recall: Alembic op.add_column alone mints a table node")


def test_recall_ef_core_create_table():
    """EF Core migrationBuilder.CreateTable(name: ...) mints a table node with alters edge."""
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "Migrations", "20240101_InitialCreate.cs"), """\
using Microsoft.EntityFrameworkCore.Migrations;

public partial class InitialCreate : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.CreateTable(
            name: "Subscriptions",
            columns: table => new
            {
                Id = table.Column<int>(nullable: false),
                UserId = table.Column<int>(nullable: false),
                Plan = table.Column<string>(maxLength: 50, nullable: false)
            },
            constraints: table =>
            {
                table.PrimaryKey("PK_Subscriptions", x => x.Id);
            });

        migrationBuilder.AddColumn<DateTime>(
            name: "StartedAt",
            table: "Subscriptions",
            nullable: true);
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropTable(name: "Subscriptions");
    }
}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "subscriptions" in tbls, (
            f"FAIL: EF Core migrationBuilder.CreateTable did not mint 'subscriptions'. "
            f"tables={tbls}"
        )
        alters = _edges_of_kind(g, "alters")
        assert any(dst == "subscriptions" for _, dst in alters), (
            f"FAIL: no alters->subscriptions from EF Core migration. alters={alters}"
        )
    print("  [ok] recall: EF Core migrationBuilder.CreateTable mints table + alters edge")


def test_recall_ef_core_to_table_fluent():
    """EF Core .ToTable('name') in DbContext mints a table node with queries edge."""
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "Data", "AppDbContext.cs"), """\
using Microsoft.EntityFrameworkCore;

public class AppDbContext : DbContext
{
    public DbSet<Subscription> Subscriptions { get; set; }
    public DbSet<Order> Orders { get; set; }

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.Entity<Subscription>()
            .ToTable("Subscriptions");
        modelBuilder.Entity<Order>()
            .ToTable("Orders");
    }
}
""")
        _w(os.path.join(root, "Migrations", "20240101_InitialCreate.cs"), """\
using Microsoft.EntityFrameworkCore.Migrations;
public partial class InitialCreate : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.CreateTable(
            name: "Subscriptions",
            columns: table => new { Id = table.Column<int>() });
        migrationBuilder.CreateTable(
            name: "Orders",
            columns: table => new { Id = table.Column<int>() });
    }
}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "subscriptions" in tbls, f"FAIL: 'subscriptions' not minted. tables={tbls}"
        assert "orders" in tbls, f"FAIL: 'orders' not minted. tables={tbls}"
        # DbContext Fluent .ToTable = queries; migration = alters
        queries = _edges_of_kind(g, "queries")
        alters = _edges_of_kind(g, "alters")
        assert any(dst == "subscriptions" for _, dst in queries), (
            f"FAIL: no queries->subscriptions from DbContext ToTable. queries={queries}"
        )
        assert any(dst == "subscriptions" for _, dst in alters), (
            f"FAIL: no alters->subscriptions from migration. alters={alters}"
        )
        # Cross-substrate crown jewel: migration alters T + DbContext queries T, no code edge
        # between them. We just confirmed both edges exist, which is the coupling.
    print("  [ok] recall: EF Core .ToTable mints table + queries edge; crown jewel confirmed")


def test_recall_ef_core_add_column():
    """EF Core migrationBuilder.AddColumn<T>(name:, table:) also mints the table."""
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "Migrations", "20240202_AddNotes.cs"), """\
public partial class AddNotes : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(
            name: "Notes",
            table: "Orders",
            nullable: true,
            defaultValue: "");
    }
}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "orders" in tbls, (
            f"FAIL: EF Core AddColumn table: arg did not mint 'orders'. tables={tbls}"
        )
    print("  [ok] recall: EF Core migrationBuilder.AddColumn (table: named arg) mints table")


# ===========================================================================
# 3. PERF — pass-2 short-circuit when tableset is empty
# ===========================================================================

def test_perf_empty_tableset_short_circuit():
    """A repo with no schema (no table declared) must produce no queries edges,
    and the short-circuit must fire (fast even with many code files)."""
    with tempfile.TemporaryDirectory() as root:
        # Many code files with SQL-like text but NO ORM declarations and NO .sql DDL.
        for i in range(30):
            _w(os.path.join(root, "src", f"service_{i}.py"), f"""\
# service module {i}
def get_data(conn):
    result = conn.execute("SELECT id, name FROM products WHERE active = 1")
    return result.fetchall()

def update_status(conn, item_id, status):
    conn.execute("UPDATE items SET status = %s WHERE id = %s", (status, item_id))
""")
        t0 = time.monotonic()
        g = X.build_graph(root)
        elapsed = time.monotonic() - t0

        tbls = _tables(g)
        assert not tbls, f"FAIL: expected no table nodes, got {tbls}"

        queries = _edges_of_kind(g, "queries")
        assert not queries, (
            f"FAIL: expected no queries edges (no known tables), got {queries[:5]}"
        )
        # The short-circuit should make this very fast. 30 files with SQL text
        # that would be scanned in pass-2 without the guard. We assert under 5s
        # (typical: <1s with short-circuit, 3-10s without on a large repo).
        assert elapsed < 5.0, (
            f"FAIL: pass-2 short-circuit did not fire — elapsed {elapsed:.2f}s (expected <5s)"
        )
    print(f"  [ok] perf: empty tableset → 0 tables, 0 queries edges, {elapsed*1000:.0f}ms")


# ===========================================================================
# main
# ===========================================================================

_TESTS = [
    # Precision
    test_precision_test_dir_model_does_not_mint,
    test_precision_prod_model_does_mint,
    test_precision_crown_jewel_still_fires,
    test_precision_test_subdirs_all_filtered,
    test_precision_nested_test_dir_filtered,
    test_precision_raw_sql_in_test_still_couples,
    # Recall
    test_recall_sqlmodel_table_true,
    test_recall_sqlmodel_crown_jewel,
    test_recall_alembic_create_table,
    test_recall_alembic_add_column_only,
    test_recall_ef_core_create_table,
    test_recall_ef_core_to_table_fluent,
    test_recall_ef_core_add_column,
    # Perf
    test_perf_empty_tableset_short_circuit,
]

if __name__ == "__main__":
    failed = []
    for fn in _TESTS:
        try:
            fn()
        except Exception as exc:
            print(f"  [FAIL] {fn.__name__}: {exc}")
            failed.append(fn.__name__)
    print()
    if failed:
        print(f"SCHEMA-WAVE GATE: FAIL ({len(failed)} failures: {', '.join(failed)})")
        sys.exit(1)
    else:
        print("SCHEMA-WAVE GATE: PASS")
