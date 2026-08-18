#!/usr/bin/env python3
"""ORM-DSL extractor gate — Rails Active Record + Laravel Eloquent schema coverage.

THE GAP: _cg_schema._ORM_PATTERNS covered Django / SQLAlchemy / JPA and raw SQL DDL,
but not Rails Active Record (create_table :name, add_column :table, self.table_name=)
nor Laravel Eloquent (Schema::create/table, protected $table). Apps written in these
two frameworks extracted 0 table nodes → the cross-substrate crown jewel (migration
alters T + code queries T with no import/call edge) was INVISIBLE for the majority
of Ruby and PHP repos.

WHAT THIS GATE PROVES (hermetic, no DB, no network, no Postgres):
  1. Rails migration DSL mints table nodes from create_table and alter methods.
  2. Rails db/schema.rb (quoted form) mints table nodes.
  3. Rails explicit self.table_name override mints a table node.
  4. Laravel Schema::create and Schema::table (in a migration) mint table nodes with
     alters edges.
  5. Laravel protected $table (in a model) mints a table node with a queries edge.
  6. Cross-substrate coupling is lit up: a Laravel migration alters T and a Laravel
     model queries T — two files with NO import/call edge, coupled via the shared
     table node.
  7. Never-crash: pathological inputs (empty file, binary-ish content) do not raise.
  8. Content-free: no file body is ever emitted — only table NAMES, edge kinds, paths.

Print `ORM-DSL GATE: PASS` on success, `ORM-DSL GATE: FAIL` on any assertion failure.
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
# 1. Rails migration DSL: create_table (colon-symbol) + alter methods
# ---------------------------------------------------------------------------
def test_rails_migration_create_and_alter():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "db", "migrate", "20240101_create_orders.rb"), """\
class CreateOrders < ActiveRecord::Migration[7.0]
  def change
    create_table :orders do |t|
      t.string :status, null: false
      t.timestamps
    end
    add_index :orders, :status
    add_column :orders, :total_cents, :integer, default: 0
  end
end
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "orders" in tbls, f"Expected 'orders' in tables, got {tbls}"
        alters = _edges_of_kind(g, "alters")
        assert any(dst == "orders" for _, dst in alters), \
            f"Expected alters->orders edge; alters={alters}"
    print("  [ok] Rails migration: create_table + alter methods")


# ---------------------------------------------------------------------------
# 2. Rails db/schema.rb (double-quoted form, multiple tables)
# ---------------------------------------------------------------------------
def test_rails_schema_rb():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "db", "schema.rb"), """\
ActiveRecord::Schema[8.0].define(version: 2024_01_01) do
  create_table "users", charset: "utf8mb4", force: :cascade do |t|
    t.string "email", null: false
  end

  create_table "posts", charset: "utf8mb4", force: :cascade do |t|
    t.bigint "user_id", null: false
    t.text "body"
  end
end
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "users" in tbls, f"Expected 'users'; got {tbls}"
        assert "posts" in tbls, f"Expected 'posts'; got {tbls}"
    print("  [ok] Rails schema.rb: double-quoted create_table")


# ---------------------------------------------------------------------------
# 3. Rails model with explicit self.table_name override
# ---------------------------------------------------------------------------
def test_rails_self_table_name():
    with tempfile.TemporaryDirectory() as root:
        # Need a migration to mint the table first (pass-2 queries rely on tableset)
        _w(os.path.join(root, "db", "migrate", "20240101_create_legacy.rb"), """\
class CreateLegacy < ActiveRecord::Migration[6.0]
  def change
    create_table :legacy_orders do |t|
      t.timestamps
    end
  end
end
""")
        _w(os.path.join(root, "app", "models", "order.rb"), """\
class Order < ApplicationRecord
  self.table_name = "legacy_orders"
end
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "legacy_orders" in tbls, f"Expected 'legacy_orders'; got {tbls}"
        # The model file should have a queries edge (self.table_name outside migrations dir)
        q_edges = _edges_of_kind(g, "queries")
        assert any(dst == "legacy_orders" for _, dst in q_edges), \
            f"Expected queries->legacy_orders; queries edges={q_edges}"
    print("  [ok] Rails model: self.table_name explicit override")


# ---------------------------------------------------------------------------
# 4. Laravel migration: Schema::create + Schema::table (alters edges)
# ---------------------------------------------------------------------------
def test_laravel_migration_schema_dsl():
    with tempfile.TemporaryDirectory() as root:
        _w(
            os.path.join(root, "database", "migrations",
                         "2014_10_12_100000_create_users_table.php"),
            """\
<?php
use Illuminate\\Database\\Migrations\\Migration;
use Illuminate\\Database\\Schema\\Blueprint;
use Illuminate\\Support\\Facades\\Schema;

return new class extends Migration {
    public function up(): void {
        Schema::create('users', function (Blueprint $table) {
            $table->id();
            $table->string('email')->unique();
            $table->timestamps();
        });
    }
    public function down(): void {
        Schema::dropIfExists('users');
    }
};
""")
        _w(
            os.path.join(root, "database", "migrations",
                         "2024_05_01_add_slug_to_users.php"),
            """\
<?php
use Illuminate\\Database\\Migrations\\Migration;
use Illuminate\\Database\\Schema\\Blueprint;
use Illuminate\\Support\\Facades\\Schema;

return new class extends Migration {
    public function up(): void {
        Schema::table('users', function (Blueprint $table) {
            $table->string('slug')->nullable();
        });
    }
    public function down(): void {
        Schema::table('users', function (Blueprint $table) {
            $table->dropColumn('slug');
        });
    }
};
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "users" in tbls, f"Expected 'users'; got {tbls}"
        alters = _edges_of_kind(g, "alters")
        assert any(dst == "users" for _, dst in alters), \
            f"Expected alters->users; alters={alters}"
    print("  [ok] Laravel migration: Schema::create + Schema::table")


# ---------------------------------------------------------------------------
# 5. Laravel model with protected $table (queries edge)
# ---------------------------------------------------------------------------
def test_laravel_model_protected_table():
    with tempfile.TemporaryDirectory() as root:
        # Migration mints the table
        _w(
            os.path.join(root, "database", "migrations",
                         "2020_01_01_create_mention_history.php"),
            """\
<?php
use Illuminate\\Database\\Migrations\\Migration;
use Illuminate\\Database\\Schema\\Blueprint;
use Illuminate\\Support\\Facades\\Schema;

return new class extends Migration {
    public function up(): void {
        Schema::create('mention_history', function (Blueprint $table) {
            $table->id();
        });
    }
};
""")
        # Model references the same table
        _w(os.path.join(root, "app", "Models", "MentionHistory.php"), """\
<?php
namespace App\\Models;
use Illuminate\\Database\\Eloquent\\Model;

class MentionHistory extends Model
{
    protected $table = 'mention_history';
    protected $fillable = ['user_id', 'entity_id'];
}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "mention_history" in tbls, f"Expected 'mention_history'; got {tbls}"
        q_edges = _edges_of_kind(g, "queries")
        assert any(dst == "mention_history" for _, dst in q_edges), \
            f"Expected queries->mention_history; queries={q_edges}"
    print("  [ok] Laravel model: protected $table")


# ---------------------------------------------------------------------------
# 6. Cross-substrate crown jewel: Laravel migration alters T, model queries T
#    — NO import/call edge between them. This is exactly what code-only tools miss.
# ---------------------------------------------------------------------------
def test_laravel_cross_substrate_coupling():
    with tempfile.TemporaryDirectory() as root:
        _w(
            os.path.join(root, "database", "migrations",
                         "2020_01_01_create_page_revisions.php"),
            """\
<?php
use Illuminate\\Database\\Migrations\\Migration;
use Illuminate\\Database\\Schema\\Blueprint;
use Illuminate\\Support\\Facades\\Schema;

return new class extends Migration {
    public function up(): void {
        Schema::create('page_revisions', function (Blueprint $table) {
            $table->id();
            $table->string('type')->default('version');
        });
    }
};
""")
        _w(os.path.join(root, "app", "Models", "PageRevision.php"), """\
<?php
namespace App\\Models;
use Illuminate\\Database\\Eloquent\\Model;

class PageRevision extends Model
{
    protected $table = 'page_revisions';
}
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "page_revisions" in tbls, f"Missing table node for page_revisions; {tbls}"

        alters = _edges_of_kind(g, "alters")
        queries = _edges_of_kind(g, "queries")

        alters_pr = [src for src, dst in alters if dst == "page_revisions"]
        queries_pr = [src for src, dst in queries if dst == "page_revisions"]

        assert alters_pr, f"Expected alters->page_revisions from migration; alters={alters}"
        assert queries_pr, f"Expected queries->page_revisions from model; queries={queries}"

        # Confirm no import/call edge connects the migration to the model
        code_edge_kinds = {"calls", "imports"}
        mig_path = next(
            (n["path"] for n in g["nodes"] if "migrations" in n.get("path", "")), None)
        model_path = next(
            (n["path"] for n in g["nodes"] if "PageRevision.php" in n.get("path", "")), None)

        if mig_path and model_path:
            cross_edges = [
                e for e in g["edges"]
                if e["kind"] in code_edge_kinds
                and e["src"] in (mig_path, model_path)
                and e["dst"] in (mig_path, model_path)
            ]
            assert not cross_edges, \
                f"Expected NO calls/imports between migration and model; got {cross_edges}"
    print("  [ok] Laravel cross-substrate crown jewel: alters + queries, no code edge")


# ---------------------------------------------------------------------------
# 7. Never-crash: pathological inputs
# ---------------------------------------------------------------------------
def test_never_crash():
    with tempfile.TemporaryDirectory() as root:
        # Empty Rails migration
        _w(os.path.join(root, "db", "migrate", "20240101_empty.rb"), "")
        # Empty PHP migration
        _w(os.path.join(root, "database", "migrations", "2024_01_01_empty.php"), "")
        # Binary-ish content (non-UTF-8 bytes)
        path = os.path.join(root, "db", "migrate", "20240102_binary.rb")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"\x00\xff\xfe create_table :crash_test do\n")
        try:
            g = X.build_graph(root)
            # Should not raise; result may or may not find the table
        except Exception as exc:
            raise AssertionError(f"build_graph raised on pathological input: {exc}") from exc
    print("  [ok] Never-crash: empty + binary-ish inputs")


# ---------------------------------------------------------------------------
# 8. Content-free: no file BODY emitted in any node or edge value
# ---------------------------------------------------------------------------
def test_content_free():
    secret = "TOP_SECRET_SALARY_DATA_DO_NOT_EMIT"
    with tempfile.TemporaryDirectory() as root:
        _w(
            os.path.join(root, "database", "migrations", "2024_create_salaries.php"),
            f"""\
<?php
use Illuminate\\Database\\Migrations\\Migration;
use Illuminate\\Database\\Schema\\Blueprint;
use Illuminate\\Support\\Facades\\Schema;

return new class extends Migration {{
    // {secret}
    public function up(): void {{
        Schema::create('salaries', function (Blueprint $table) {{
            $table->decimal('amount', 10, 2);
        }});
    }}
}};
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
        test_rails_migration_create_and_alter,
        test_rails_schema_rb,
        test_rails_self_table_name,
        test_laravel_migration_schema_dsl,
        test_laravel_model_protected_table,
        test_laravel_cross_substrate_coupling,
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
        print("ORM-DSL GATE: PASS")
    else:
        print("ORM-DSL GATE: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
