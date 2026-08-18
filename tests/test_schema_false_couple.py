#!/usr/bin/env python3
"""SCHEMA FALSE-COUPLE PRECISION gate (NO DB, offline). The ORM/DDL extractors must not mint a FALSE table
(one that is not really declared): a false table -> a false cross-substrate coupling -> a FALSE PAUSE now that
a material coupling blocks a PR. Locks 3 precision-audit findings, each also asserting the REAL pattern still
works (recall preserved):

  A - @Entity inline-comment leak: `@Entity()  // uses class name` used to mint the English comment words
      'name'/'definition' as tables (the old re.S `[^;{]*?` crossed the newline into the comment). Now the
      regex is line-anchored: it consumes ONLY the @Entity decorator line, then the real class.
  B - Sequelize .init over-match: the bare `tableName:` key matched ANY `.init({tableName})` (connect-pg-simple,
      express-mysql-session, bookshelf). Now it requires `sequelize` to co-occur in the .init options.
  C - SQL commented-out DDL: `-- CREATE TABLE old_x` / `/* CREATE TABLE legacy */` (inline rollback docs in
      migrations) used to mint ghost tables. SQL comments are now stripped before the DDL scan.
  D - Rails add_column-IN-BLOCK column-as-table mis-capture: a column-DDL call (`add_column :url, :text`)
      INSIDE a `create_table` / `change_table` / `.table :t do` block (discourse's `Schema.table :uploads do`
      tooling) used to mint the COLUMN name as a false table. Now the Rails column-DDL methods are block-aware:
      the first symbol is a table ONLY at top level; inside a table-DSL block it is a column (the enclosing
      opener mints the real table). Measured on discourse: 28 false generic tables (url/type/path/data/message
      /...) removed; the real tables gain touchers; genuine couples preserved.
  E - prose-FROM (`Creating posts from message batches` -> queries table::message): this only fired because a
      FALSE `message` table existed (minted by D's bug or similar). With D fixed, the prose word has no known
      table to hit and the existing known-table guard drops it — WITHOUT tightening the FROM regex. A name-based
      FROM-context tightening was MEASURED recall-UNSAFE on real repos (drops multi-line heredoc SQL, UPDATE...
      FROM, short-aliased `FROM t a`), so the recall-safe home is NOT minting false tables (D) + the known-table
      guard, not the FROM matcher. This section pins both directions: prose mints no edge; real FROM still does.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import code_graph_extract as X  # noqa: E402
import _cg_schema as S  # noqa: E402


def _tables(graph):
    return {n["name"] for n in graph["nodes"] if n.get("kind") == "table"}


def _refs(text):
    return {str(t).lower() for t in S._orm_table_refs(text)}


def _w(p, b):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(b)


def main():
    checks = []

    # ── A — @Entity inline comment must NOT leak comment words as tables; the real class name IS extracted ──
    a_false = _refs("@Entity()  // No explicit table name - uses class name\npublic class Category {\n}\n")
    checks.append(("A: @Entity inline comment does NOT mint 'name'/'definition' as a table",
                   "name" not in a_false and "definition" not in a_false))
    checks.append(("A: @Entity over a class still bridges to the real class table (recall kept)",
                   "category" in a_false))
    a_javadoc = _refs("/**\n * @Entity class for managing things\n */\npublic class Widget {\n}\n")
    checks.append(("A: a Javadoc mentioning '@Entity class for' does NOT mint 'for' as a table",
                   "for" not in a_javadoc))
    a_stacked = _refs("@Entity\n@Table(name = \"orders\")\npublic class Order {\n}\n")
    checks.append(("A: stacked decorators (@Entity then @Table) still reach the class (recall kept)",
                   "order" in a_stacked))

    # ── B — Sequelize .init: a NON-Sequelize .init({tableName}) mints NO table; a real Sequelize .init does ──
    b_false = _refs("pgSession.init({ tableName: 'user_sessions', pool: pgPool })")
    checks.append(("B: a non-Sequelize .init({tableName}) (no `sequelize`) mints NO table",
                   "user_sessions" not in b_false))
    b_real = _refs("Order.init({ id: DataTypes.INTEGER }, { tableName: 'orders', sequelize })")
    checks.append(("B: a real Sequelize .init (with `sequelize`) still mints the table (recall kept)",
                   "orders" in b_real))

    # ── C — SQL commented-out DDL mints no ghost table; real DDL still does (end-to-end via build_graph) ──
    with tempfile.TemporaryDirectory() as d:
        _w(os.path.join(d, "db", "0001.sql"),
           "-- CREATE TABLE old_orders (id int);\n"
           "/* rollback:\n   CREATE TABLE legacy_users (id int);\n*/\n"
           "CREATE TABLE orders (id int, total int);\n")
        tabs = _tables(X.build_graph(d))
    checks.append(("C: commented-out DDL (-- and /* */) mints NO ghost table",
                   "old_orders" not in tabs and "legacy_users" not in tabs))
    checks.append(("C: real CREATE TABLE still mints the table (recall kept)", "orders" in tabs))
    stripped = S._strip_sql_comments("-- gone line\nCREATE TABLE keep (x int);\n/* gone block */")
    checks.append(("C: _strip_sql_comments removes -- and /* */ but keeps the real DDL",
                   "CREATE TABLE keep" in stripped and "gone line" not in stripped and "gone block" not in stripped))

    # ── D — Rails add_column-IN-BLOCK column-as-table mis-capture (audit residue, the higher-value fix) ──
    # `Migrations::Tooling::Schema.table :uploads do  add_column :url, :text  end` (discourse custom migration
    # tooling) used to mint the COLUMN names (url/type/path/data/message) as FALSE low-participant tables that
    # slip past resource-hub dampening and false-couple (and get queried by prose). Fix: a Rails column-DDL
    # method's first symbol is a table ONLY at top level; inside a create_table/change_table/.table block it
    # is a COLUMN. RECALL: the enclosing opener still mints the real table; a top-level migration add_column
    # :t, :col is unchanged (the first arg IS the table).
    d_block = _refs(
        "Migrations::Tooling::Schema.table :uploads do\n"
        "  add_column :id, :text\n"
        "  add_column :url, :text\n"
        "  add_column :type, :text\n"
        "end\n")
    checks.append(("D: add_column INSIDE a `.table :uploads do` block does NOT mint the COLUMN `url` as a table",
                   "url" not in d_block and "type" not in d_block))
    checks.append(("D: the enclosing `.table :uploads do` opener STILL mints the real `uploads` table (recall kept)",
                   "uploads" in d_block))
    # RECALL: a standalone top-level `add_column :products, :price` — first arg is the TABLE, must be kept.
    d_top = _refs("add_column :products, :price, :decimal\n")
    checks.append(("D: a standalone top-level add_column :products, :price still mints `products` (recall kept)",
                   "products" in d_top))
    # RECALL: a top-level add_column inside a plain `def up ... end` (NOT a table-DSL block) is still a table.
    d_defup = _refs(
        "class AddApprovedToUsers < ActiveRecord::Migration\n"
        "  def up\n"
        "    add_column :users, :approved, :boolean\n"
        "  end\n"
        "end\n")
    checks.append(("D: add_column inside `def up`/`class` (NOT a table-DSL block) still mints `users` (recall kept)",
                   "users" in d_defup and "approved" not in d_defup))
    # RECALL: change_table block opener mints the table; add_column inside it does NOT mint the column.
    d_change = _refs("change_table :widgets do |t|\n  add_column :color, :string\nend\n")
    checks.append(("D: change_table block mints `widgets` (opener) but not the in-block column `color`",
                   "widgets" in d_change and "color" not in d_change))

    # ── E — prose-FROM is recall-safely backstopped by Bug 1 + the known-table guard (NOT a recall-risky FROM-tightening) ──
    # The audit's prose-FROM false edge ("Creating posts from message batches" -> queries table::message) only
    # fired because Bug 1 (and similar) minted a FALSE `message` table. With Bug 1 fixed, the prose word has NO
    # table to hit, so the known-table guard drops it WITHOUT tightening the FROM regex. A real SELECT ... FROM
    # orders MUST still couple. NOTE: a name-based FROM-context tightening was MEASURED to be recall-UNSAFE on
    # real repos (it drops multi-line heredoc SQL, UPDATE...FROM, and short-aliased `FROM t a` couples), so the
    # fix lives in NOT minting false tables (Bug 1) + the existing known-table guard, not in the FROM matcher.
    with tempfile.TemporaryDirectory() as d:
        # No file independently declares a `message` table (the block-DSL bug that used to mint it is FIXED),
        # so a prose mention `from message` has no known table to couple to.
        _w(os.path.join(d, "app", "jobs.py"),
           "def run():\n"
           "    # Creating posts from message batches and loading email from message queue\n"
           "    return db('SELECT id FROM posts')\n")
        _w(os.path.join(d, "db", "0001_posts.sql"), "CREATE TABLE posts (id int);\n")
        ge = X.build_graph(d)
        e_dsts = {e["dst"] for e in ge["edges"] if e["kind"] == "queries"}
        e_tables = _tables(ge)
    checks.append(("E: prose `from message` mints NO `message` table and so NO queries edge (Bug 1 + known-table guard)",
                   "message" not in e_tables and "message" not in e_dsts))
    # RECALL: a real SELECT ... FROM <known table> still emits a queries edge.
    with tempfile.TemporaryDirectory() as d:
        _w(os.path.join(d, "db", "0001_orders.sql"), "CREATE TABLE orders (id int);\n")
        _w(os.path.join(d, "app", "read.py"),
           "def read():\n    return db('SELECT * FROM orders WHERE id = 1')\n")
        ge2 = X.build_graph(d)
        e2_dsts = {e["dst"] for e in ge2["edges"] if e["kind"] == "queries" and e["src"].endswith("read.py")}
    checks.append(("E: a real SELECT * FROM orders STILL emits a queries edge to `orders` (FROM recall kept)",
                   "orders" in e2_dsts))

    ok = all(c[1] for c in checks)
    print("\n=== SCHEMA FALSE-COUPLE PRECISION (offline) ===")
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    print("\nSCHEMA FALSE-COUPLE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
