#!/usr/bin/env python3
"""Ecto/Phoenix ORM extractor gate.

THE GAP: _cg_schema._ORM_PATTERNS covered Django/SQLAlchemy/JPA/Rails/Laravel/
Knex/Drizzle/Mongoose/Sequelize/EF Core/Prisma + raw SQL + Go gorm/ent, but NOT
Ecto (Phoenix/Elixir's ORM). Phoenix apps extracted 0 table nodes -- the cross-
substrate crown jewel (migration alters T + schema model queries T with NO
import/call edge) was INVISIBLE for Elixir/Phoenix repos.

Ecto declares tables two ways, both in .ex/.exs files:
  (a) Ecto schema macro in model files:
        schema "users" do
          field :email, :string
          ...
        end
      -> table "users"
  (b) Ecto migration macros in priv/repo/migrations/*.exs:
        create table(:users) do ... end
        alter table(:messages) do ... end
        create table("posts", primary_key: false) do ... end
      -> tables "users", "messages", "posts"

WHAT THIS GATE PROVES (hermetic, no DB, no Postgres, no network):
  1. Ecto schema macro: schema "users" do -> mints table node 'users'.
  2. Ecto migration create table atom: create table(:orders) -> mints 'orders'.
  3. Ecto migration create table with options: create table(:items, primary_key: false)
     -> mints 'items' (extra args after the name do not interfere).
  4. Ecto migration alter table: alter table(:messages) -> mints 'messages'.
  5. String-literal form: create table("posts") -> mints 'posts'.
  6. CROSS-SUBSTRATE CROWN JEWEL: Ecto migration (priv/repo/migrations/*.exs,
     alters edge) + Ecto schema model (lib/**/*.ex, queries edge) share the same
     table node with CONFIRMED absence of any calls/imports edge between them.
  7. PRECISION - plain Elixir string "users" NOT preceded by the schema macro ->
     does NOT mint a table node.
  8. PRECISION - 'use Ecto.Schema' line -> does NOT mint a table.
  9. Never-crash: empty .ex file, malformed bytes, non-.ex files do not raise.
 10. Content-free: no file body or field values appear in the graph.

Prints ECTO GATE: PASS on success, ECTO GATE: FAIL on any failure.
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
    return {n["name"] for n in g["nodes"] if n.get("kind") == "table"}


def _edges_of_kind(g, kind):
    return [(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == kind]


# ---------------------------------------------------------------------------
# 1. Ecto schema macro: schema "users" do -> table node 'users'
# ---------------------------------------------------------------------------
def test_ecto_schema_macro():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "lib", "myapp", "accounts", "user.ex"), """\
defmodule MyApp.Accounts.User do
  use Ecto.Schema
  import Ecto.Changeset

  @primary_key {:id, :binary_id, autogenerate: true}
  @foreign_key_type :binary_id
  schema "users" do
    field :email, :string
    field :name, :string
    field :role, :string, default: "user"
    timestamps()
  end

  def changeset(user, attrs) do
    user
    |> cast(attrs, [:email, :name])
    |> validate_required([:email])
  end
end
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "users" in tbls, f"Expected 'users' from schema macro; got {tbls}"
    print("  [ok] Ecto schema macro 'schema \"users\" do' -> table node 'users'")


# ---------------------------------------------------------------------------
# 2. Ecto migration create table atom form: create table(:orders) do
# ---------------------------------------------------------------------------
def test_ecto_migration_create_table_atom():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "priv", "repo", "migrations",
                        "20240101_create_orders.exs"), """\
defmodule MyApp.Repo.Migrations.CreateOrders do
  use Ecto.Migration

  def change do
    create table(:orders) do
      add :amount, :decimal
      add :status, :string
      timestamps()
    end
  end
end
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "orders" in tbls, \
            f"Expected 'orders' from create table(:orders); got {tbls}"
    print("  [ok] Ecto migration 'create table(:orders) do' -> table node 'orders'")


# ---------------------------------------------------------------------------
# 3. Ecto migration create table with options: create table(:items, primary_key: false)
# ---------------------------------------------------------------------------
def test_ecto_migration_create_table_with_options():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "priv", "repo", "migrations",
                        "20240102_create_items.exs"), """\
defmodule MyApp.Repo.Migrations.CreateItems do
  use Ecto.Migration

  def change do
    create table(:items, primary_key: false) do
      add :id, :binary_id, primary_key: true
      add :name, :string, null: false
      timestamps()
    end
  end
end
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "items" in tbls, \
            f"Expected 'items' from create table(:items, ...); got {tbls}"
    print("  [ok] 'create table(:items, primary_key: false)' -> table node 'items'")


# ---------------------------------------------------------------------------
# 4. Ecto migration alter table: alter table(:messages) do
# ---------------------------------------------------------------------------
def test_ecto_migration_alter_table():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "priv", "repo", "migrations",
                        "20240103_add_body_to_messages.exs"), """\
defmodule MyApp.Repo.Migrations.AddBodyToMessages do
  use Ecto.Migration

  def change do
    alter table(:messages) do
      add :body, :text
      add :sent_at, :utc_datetime
    end
  end
end
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "messages" in tbls, \
            f"Expected 'messages' from alter table(:messages); got {tbls}"
    print("  [ok] Ecto migration 'alter table(:messages) do' -> table node 'messages'")


# ---------------------------------------------------------------------------
# 5. String-literal form: create table("posts") do
# ---------------------------------------------------------------------------
def test_ecto_migration_create_table_string():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "priv", "repo", "migrations",
                        "20240104_create_posts.exs"), """\
defmodule MyApp.Repo.Migrations.CreatePosts do
  use Ecto.Migration

  def change do
    create table("posts") do
      add :title, :string
      add :body, :text
    end
  end
end
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "posts" in tbls, \
            f"Expected 'posts' from create table(\"posts\"); got {tbls}"
    print("  [ok] Ecto migration 'create table(\"posts\")' (string form) -> table node 'posts'")


# ---------------------------------------------------------------------------
# 6. CROSS-SUBSTRATE CROWN JEWEL:
#    Ecto migration (priv/repo/migrations/) alters T (-> alters edge)
#    Ecto schema model (lib/) queries T (-> queries edge)
#    No import/call edge between the two files.
# ---------------------------------------------------------------------------
def test_ecto_cross_substrate_crown_jewel():
    with tempfile.TemporaryDirectory() as root:
        # Migration file — in priv/repo/migrations/ -> _is_migration() fires -> alters edge
        _w(os.path.join(root, "priv", "repo", "migrations",
                        "20240110_create_accounts.exs"), """\
defmodule MyApp.Repo.Migrations.CreateAccounts do
  use Ecto.Migration

  def change do
    create table(:accounts) do
      add :name, :string, null: false
      add :subdomain, :string, null: false
      timestamps()
    end

    create unique_index(:accounts, [:subdomain])
  end
end
""")
        # Schema model file — outside migrations -> queries edge
        _w(os.path.join(root, "lib", "myapp", "accounts", "account.ex"), """\
defmodule MyApp.Accounts.Account do
  use Ecto.Schema
  import Ecto.Changeset

  @primary_key {:id, :binary_id, autogenerate: true}
  schema "accounts" do
    field :name, :string
    field :subdomain, :string
    timestamps()
  end

  def changeset(account, attrs) do
    account
    |> cast(attrs, [:name, :subdomain])
    |> validate_required([:name, :subdomain])
  end
end
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "accounts" in tbls, f"Expected 'accounts' table node; got {tbls}"

        alters = _edges_of_kind(g, "alters")
        queries = _edges_of_kind(g, "queries")

        assert any(dst == "accounts" for _, dst in alters), \
            f"Expected alters->accounts from migration; alters={alters}"
        assert any(dst == "accounts" for _, dst in queries), \
            f"Expected queries->accounts from schema model; queries={queries}"

        # Confirm no import/call edge between the migration file and the schema model
        code_edge_kinds = {"calls", "imports"}
        mig_src = next(
            (e["src"] for e in g["edges"] if e["kind"] == "alters" and e["dst"] == "accounts"),
            None)
        model_src = next(
            (e["src"] for e in g["edges"] if e["kind"] == "queries" and e["dst"] == "accounts"),
            None)
        assert mig_src is not None, "No alters-edge source found for 'accounts'"
        assert model_src is not None, "No queries-edge source found for 'accounts'"
        cross = [
            e for e in g["edges"]
            if e["kind"] in code_edge_kinds
            and set([e["src"], e["dst"]]) == set([mig_src, model_src])
        ]
        assert not cross, \
            f"Expected NO calls/imports between migration and schema model; got {cross}"

    print("  [ok] CROWN JEWEL: Ecto migration alters 'accounts' + schema model queries "
          "'accounts' — no code edge between them")


# ---------------------------------------------------------------------------
# 7. PRECISION: plain Elixir string not preceded by schema macro -> no table
# ---------------------------------------------------------------------------
def test_precision_plain_string():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "lib", "myapp", "helpers.ex"), """\
defmodule MyApp.Helpers do
  @doc "Returns the table prefix for a given name."
  def table_prefix(name) do
    "users_" <> name
  end

  def describe do
    "users are stored in the users table"
  end
end
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        # The string "users" appears here but NOT after the schema macro -> no table node
        assert "users" not in tbls, \
            f"PRECISION FAILURE: 'users' minted from plain Elixir string; got {tbls}"
    print("  [ok] PRECISION: plain Elixir string 'users' without schema macro -> no table")


# ---------------------------------------------------------------------------
# 8. PRECISION: 'use Ecto.Schema' line -> does NOT mint a table
# ---------------------------------------------------------------------------
def test_precision_use_ecto_schema():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "lib", "myapp", "base.ex"), """\
defmodule MyApp.Base do
  use Ecto.Schema
  import Ecto.Changeset

  # This module uses Ecto.Schema but does not declare a schema table.
  def changeset(struct, attrs) do
    struct
    |> cast(attrs, [])
  end
end
""")
        g = X.build_graph(root)
        tbls = _tables(g)
        assert "ecto" not in tbls, \
            f"PRECISION FAILURE: 'ecto' minted from 'use Ecto.Schema'; got {tbls}"
        assert "schema" not in tbls, \
            f"PRECISION FAILURE: 'schema' minted as table; got {tbls}"
        # No table at all should be minted from this file
        assert len(tbls) == 0, \
            f"PRECISION FAILURE: unexpected tables minted: {tbls}"
    print("  [ok] PRECISION: 'use Ecto.Schema' without schema macro -> no table node")


# ---------------------------------------------------------------------------
# 9. Never-crash: empty .ex file, malformed bytes, non-.ex files
# ---------------------------------------------------------------------------
def test_never_crash():
    with tempfile.TemporaryDirectory() as root:
        # Empty Elixir file
        _w(os.path.join(root, "lib", "empty.ex"), "")
        # Malformed bytes with Ecto-like pattern embedded
        path = os.path.join(root, "lib", "bad.exs")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe" + b'schema "crash" do\n')
        # Non-Elixir file with same text (should not mint a table via ORM path)
        _w(os.path.join(root, "docs", "notes.txt"),
           'schema "notes" do\n  field :body, :string\nend\n')
        try:
            g = X.build_graph(root)
        except Exception as exc:
            raise AssertionError(
                f"build_graph raised on pathological input: {exc}") from exc
    print("  [ok] Never-crash: empty + binary-ish + non-.ex inputs")


# ---------------------------------------------------------------------------
# 10. Content-free: no file body or field values appear in the graph
# ---------------------------------------------------------------------------
def test_content_free():
    secret = "SUPER_SECRET_SALARY_FIELD_DO_NOT_EMIT"
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "lib", "myapp", "salaries", "salary.ex"), f"""\
defmodule MyApp.Salaries.Salary do
  use Ecto.Schema

  # This field contains a secret: {secret}
  @primary_key {{:id, :binary_id, autogenerate: true}}
  schema "salaries" do
    field :{secret}, :string
    field :amount, :decimal
    timestamps()
  end
end
""")
        g = X.build_graph(root)
        graph_str = str(g)
        assert secret not in graph_str, \
            f"Content-free violated: secret literal found in graph output"
        tbls = _tables(g)
        assert "salaries" in tbls, "Expected 'salaries' table node to be minted"
    print("  [ok] Content-free: secret field name does not appear in the graph")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main():
    tests = [
        test_ecto_schema_macro,
        test_ecto_migration_create_table_atom,
        test_ecto_migration_create_table_with_options,
        test_ecto_migration_alter_table,
        test_ecto_migration_create_table_string,
        test_ecto_cross_substrate_crown_jewel,
        test_precision_plain_string,
        test_precision_use_ecto_schema,
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
        print("ECTO GATE: PASS")
    else:
        print("ECTO GATE: FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
