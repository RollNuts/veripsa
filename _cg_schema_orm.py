"""ORM / ActiveRecord schema extraction for the code-graph extractor.

Owns the ORM substrate of the schema graph for every framework that declares tables in
application code (not raw SQL and not Go): `_ORM_PATTERNS` (Django / SQLAlchemy / SQLModel /
JPA / TypeORM / Rails ActiveRecord / Laravel Eloquent / Drizzle / Mongoose / Sequelize /
Prisma / Knex / Ecto / Alembic / EF Core / Objection / Bookshelf), the Django/JPA/TypeORM
model-NAME → table-name bridge patterns, the BLOCK-AWARE Rails column-DDL detector (so a
column-DDL call inside a create_table block is not mis-minted as a table), and the
CREATE-class subset used for multi-definer creator tracking.

The Go gorm/ent path lives in `_cg_schema_go` (its forms need comment-stripping first); the
ORM-level entry points `_orm_table_refs` / `_orm_creator_refs` call into it, so this module
imports from `_cg_schema_go` (one-directional — Go never imports back, so no cycle). Split
out of the former god-file `_cg_schema.py` as a PURE behaviour-preserving move (no logic /
regex / signature change). `_norm_table` and `_SQL_NONTABLE` are shared and imported from
`_cg_schema_shared`. Content-free: table / model NAMES only.
"""
import re

from _cg_schema_shared import _norm_table, _SQL_NONTABLE
from _cg_schema_go import (
    _go_explicit_refs, _go_gorm_struct_names, _go_ent_schema_names,
)

# ORM table declarations (content-free: a table NAME or a model/entity NAME → table
# candidate). Precision-biased — each pattern is anchored on an ORM-specific token
# (__tablename__, db_table, @Table/@Entity, `class …(models.Model|db.Model|Base)`,
# migration model_name/CreateModel, pgTable/mysqlTable/sqliteTable, mongoose.model,
# sequelize.define/.init tableName, DbSet<T>), so prose / ordinary class attributes
# cannot match.
# A declared table populates the known-table set and makes its file a shared-resource
# participant, letting a migration that touches T couple with code that touches T.
_ORM_PATTERNS = [
    # SQLAlchemy declarative: `__tablename__ = "orders"`
    re.compile(r'__tablename__\s*=\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    # Django model Meta `db_table = "orders"` AND Django migration CreateModel
    # options `"db_table": "orders"` — the key may itself be quoted, so an optional
    # closing quote is allowed between the key and the `:`/`=`.
    re.compile(r'db_table[\'"]?\s*[:=]\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    # JPA: `@Table(name = "orders")` / `@Table(name="orders", schema=…)`
    re.compile(r'@Table\s*\([^)]*\bname\s*=\s*[\'"]([A-Za-z_][\w.]*)[\'"]', re.I),
    # Rails Active Record: `create_table :orders` / `create_table "orders"` (in
    # migrations and db/schema.rb). The symbol form `:name` is the idiomatic Rails style;
    # the quoted form `"name"` appears in schema.rb and some migrations.
    re.compile(r'\bcreate_table\s+(?::([A-Za-z_]\w*)|[\'"]([A-Za-z_][\w.]*)[\'"])'),
    # Rails block-DSL table opener: `change_table :orders do |t|` and the custom
    # `Schema.table :uploads do` form (discourse migration tooling
    # `Migrations::Tooling::Schema.table :uploads do`). Both NAME the table the block
    # operates on, then the block body uses `t.column`/`add_column` for COLUMNS. We mint
    # the table from the opener; the column-DDL methods inside are handled by the
    # block-aware `_rails_column_ddl_refs` (which suppresses their first arg as a table
    # while inside such a block — see Bug 1 below). `change_table` matches as a bare
    # method (`change_table :t`); `.table` matches the dotted receiver form (`Schema.table :t`)
    # so a bare local call `table(:x)` does not fire.
    re.compile(r'\bchange_table\s+(?::([A-Za-z_]\w*)|[\'"]([A-Za-z_][\w.]*)[\'"])'),
    re.compile(r'\.\s*table\s+(?::([A-Za-z_]\w*)|[\'"]([A-Za-z_][\w.]*)[\'"])\s+do\b'),
    # NOTE: the Rails column/index/fk DDL methods (add_column / add_index / add_foreign_key /
    # remove_* / rename_column / change_column*) are NOT a plain _ORM_PATTERNS entry anymore —
    # they are handled by _rails_column_ddl_refs() so that a column-DDL call INSIDE a
    # create_table/change_table/.table block (where its first symbol is a COLUMN, not a table)
    # does not mint the column name as a false table (Bug 1). At top level (a real Rails
    # migration `add_column :orders, :total`) the first symbol IS the table and is still minted.
    # Rails model explicit override: `self.table_name = "orders"` / `= :orders`
    re.compile(r'\bself\.table_name\s*=\s*(?::([A-Za-z_]\w*)|[\'"]([A-Za-z_][\w.]*)[\'"])'),
    # Laravel Eloquent migration DSL: `Schema::create('orders', ...)` and
    # `Schema::table('orders', ...)`. Only `create` and `table` are table-name arguments;
    # `Schema::drop`/`dropIfExists`/`rename`/`hasTable` are excluded by the capture group.
    re.compile(r'\bSchema\s*::\s*(?:create|table)\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]', re.I),
    # Laravel Eloquent model: `protected $table = 'orders'` — explicit table binding.
    # Anchored on `protected $table` (a PHP class property) so array variables named
    # `$table` in helper code do not match.
    re.compile(r'\bprotected\s+\$table\s*=\s*[\'"]([A-Za-z_][\w.]*)[\'"]\s*;'),
    # Drizzle ORM (TS/JS, fastest-growing): `pgTable("users", {...})` /
    # `mysqlTable("orders", {...})` / `sqliteTable("posts", {...})`.
    # Anchored on the ORM-specific function name at a word boundary, followed immediately
    # by `(` and a string literal — the first arg is always the table name. Variable names
    # (pgTable(myVar, {})) and imports (import { pgTable }) do not match.
    re.compile(r'\b(?:pgTable|mysqlTable|sqliteTable)\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    # Mongoose (~4M/wk): `mongoose.model("User", schema)` — model name is the collection
    # name (Mongoose pluralizes it, but the first arg is the canonical identifier used in
    # queries; we keep it as-is for coupling). Anchored on `mongoose.model(` so
    # `models.model(` or plain `model(` do not match.
    re.compile(r'\bmongoose\s*\.\s*model\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    # Mongoose explicit collection name: `new Schema({}, { collection: "users" })`.
    # Only fires when the string "collection:" appears inside the Schema options object
    # (bounded to 512 chars), so bare `new Schema({...})` without the option is silent.
    re.compile(
        r'\bnew\s+(?:mongoose\.)?Schema\s*\([^)]{0,512},\s*\{[^}]{0,512}\bcollection\s*:'
        r'\s*[\'"]([A-Za-z_][\w.]*)[\'"]', re.S),
    # Sequelize (~7M/wk): `sequelize.define("Order", {...})` — model name = table candidate.
    # Anchored on `sequelize.define(` so other `.define(` calls don't match.
    re.compile(r'\bsequelize\s*\.\s*define\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    # Sequelize explicit table name: `MyModel.init({...}, { tableName: "orders", sequelize })`.
    # REQUIRES `sequelize` to ALSO appear in the same .init options (the Sequelize `.init` API always takes the
    # `sequelize` instance). Without this guard the bare `tableName:` key matched NON-Sequelize `.init({tableName})`
    # calls (connect-pg-simple, express-mysql-session, bookshelf), minting false tables → false couplings →
    # false pauses now that a material coupling blocks. Anchored on `.init(`; scans up to 1024 chars.
    re.compile(
        r'\.\s*init\s*\((?=[^;]{0,1024}\bsequelize\b)[^;]{0,1024}\btableName\s*:\s*[\'"]([A-Za-z_][\w.]*)[\'"]', re.S),
    # EF Core (.NET dominant): `public DbSet<User> Users { get; set; }` on a DbContext.
    # The type parameter is the entity class name → table candidate. Precision guard:
    # requires uppercase first letter (entity classes are PascalCase; primitives like
    # `string`/`int`/`bool` are lowercase and never real entity types).
    re.compile(r'\bDbSet\s*<\s*([A-Z][A-Za-z_]\w*)\s*>'),
    # Prisma ORM (fastest-growing TS/JS ORM, ~4M/wk npm): `model User { ... }` in a
    # .prisma schema file — each `model` block declares one DB table. The keyword
    # `model` is anchored at a word boundary followed by a PascalCase/identifier name
    # and a literal `{`, so ordinary Prisma block types (enum/type/generator/datasource)
    # NEVER match — they each start with their own keyword, not `model`. Prose, JS/TS
    # code, and comments that happen to contain the word "model" followed by a name are
    # also excluded because (a) .prisma files are only walked when `.prisma` is in
    # _SOURCE_EXT, and (b) the `{` anchor stops non-block usages. Content-free: the
    # model NAME (PascalCase identifier) is the table candidate; no body is extracted.
    re.compile(r'\bmodel\s+([A-Za-z_]\w*)\s*\{'),
    # Knex.js schema builder migrations (Node.js ecosystem, ~3M/wk npm): table declarations
    # via `.createTable('name', ...)` / `.createTableIfNotExists('name', ...)` (creation)
    # and `.alterTable('name', ...)` (alteration). All three forms anchor on the dot-prefix
    # so that bare function calls without a chained object (e.g. `createTable(...)` as a
    # standalone helper), string literals that happen to contain "createTable", and import
    # statements (`import { createTable }`) do NOT match. The dot anchor gives the same
    # precision guarantee as the ORM-specific-function anchors for Drizzle/Mongoose above.
    # `createTableIfNotExists` is captured by the same group (the `(?:IfNotExists)?` makes
    # the suffix optional) so the table name is always extracted regardless of the variant.
    # Content-free: only the table NAME (first string argument) is captured; the callback
    # body and column definitions are never extracted.
    re.compile(r'\.createTable(?:IfNotExists)?\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    re.compile(r'\.alterTable\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    # Ecto schema macro (Phoenix/Elixir ORM) in model files (.ex/.exs):
    #   `schema "users" do` — declares the DB table this module maps to. The keyword
    # `schema` is a first-class Elixir macro imported from Ecto.Schema; it is ALWAYS
    # followed immediately by a string literal (the exact table name). Precision guard:
    # `use Ecto.Schema` / `import Ecto.Schema` / bare `schema` variable references do NOT
    # match because they lack the trailing string literal. The `\b` anchor prevents
    # `myschema "…"` from firing. Content-free: only the table NAME is captured.
    re.compile(r'\bschema\s+[\'"]([A-Za-z_][\w.]*)[\'"]\s'),
    # Ecto migration macros (Phoenix/Elixir) in priv/repo/migrations/*.exs:
    #   `create table(:users) do` / `create table(:users, primary_key: false) do`
    #   `alter table(:messages) do`
    # Two name forms: atom `:name` (idiomatic Elixir) captured in group 1, or string
    # `"name"` captured in group 2 (rare but valid). Anchored on `create table(` /
    # `alter table(` as two space-separated tokens — `create_table(` (Rails) and
    # `.createTable(` (Knex) are distinct and do NOT match here. The `\b` prevents
    # `recreate table(` or `precreate table(` from firing. Content-free: only the table
    # NAME (first argument, before any comma or closing paren) is captured.
    re.compile(r'\bcreate\s+table\s*\(\s*(?::([A-Za-z_]\w*)|[\'"]([A-Za-z_][\w.]*)[\'"]\s*)'),
    re.compile(r'\balter\s+table\s*\(\s*(?::([A-Za-z_]\w*)|[\'"]([A-Za-z_][\w.]*)[\'"]\s*)'),
    # ---- FIX 2a: SQLModel (FastAPI) ----
    # `class User(SQLModel, table=True):` — the class NAME (snake_case/lower) is the table
    # candidate. Precision anchor: `table=True` inside the class-base-list is the unique
    # SQLModel marker; a plain `class User(Base):` (SQLAlchemy declarative) does NOT fire here
    # (it is caught by _ORM_MODEL_CLASS_RE / __tablename__ above). Content-free: class NAME only.
    # No plural / no s-suffix — SQLModel uses the class name directly as the table name unless
    # overridden by __tablename__; keeping the name verbatim matches what the DB actually sees.
    re.compile(r'\bclass\s+([A-Za-z_]\w*)\s*\([^)]*\btable\s*=\s*True'),
    # ---- FIX 2b: Alembic migration ops ----
    # `op.create_table('users', ...)` / `op.add_column('users', ...)` /
    # `op.drop_table('users')` / `op.rename_table('old', 'new')` /
    # `op.create_index(..., 'users', ...)` (table name is 3rd positional arg — omitted here
    # for simplicity; captured by _DDL_RE / _INDEX_RE if the op emits raw SQL).
    # The highest-recall forms are create_table and add_column which ALWAYS carry the table
    # name as first string arg. Anchored on `op.` so a bare `create_table(` (Knex) / Rails
    # `create_table :name` do NOT fire. Content-free: table NAME only (first string arg).
    re.compile(r'\bop\.\s*(?:create_table|add_column|drop_table|drop_index)\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    re.compile(r'\bop\.\s*(?:create_index|create_unique_constraint|drop_constraint|bulk_insert)\s*\([^,]{0,128},\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    # ---- FIX 2c: EF Core (.NET) ----
    # migrationBuilder.CreateTable(name: "Subscriptions", ...) → table `subscriptions`
    # migrationBuilder.AddColumn<T>(name:"x", table:"Subscriptions", ...)
    # Both forms use NAMED ARGUMENTS (C# named-arg syntax: `name:` / `table:`).
    # Anchored on `migrationBuilder.` (migration context only; `modelBuilder.` which builds the
    # in-memory model is intentionally excluded — it is a `queries` source not an `alters`).
    # Content-free: table NAME only (value of the `name:` or `table:` named arg).
    re.compile(r'\bmigrationBuilder\s*\.\s*(?:Create|Drop|Rename|Alter)Table\s*\([^;]{0,512}\bname\s*:\s*[\'"]([A-Za-z_][\w.]*)[\'"]', re.S),
    re.compile(r'\bmigrationBuilder\s*\.\s*\w+\s*(?:<[^>]*>)?\s*\([^;]{0,512}\btable\s*:\s*[\'"]([A-Za-z_][\w.]*)[\'"]', re.S),
    # EF Core Fluent API: `entity.ToTable("Subscriptions")` / `.ToTable("name")` (model builder).
    # This is a QUERIES-side declaration (the DbContext model configuration, not a migration).
    # Anchored on `.ToTable(` so a bare `ToTable(` standalone call is excluded. Content-free.
    re.compile(r'\.ToTable\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    # ---- FIX 3a: Objection.js static getter `static get tableName() { return 'Name' }` ----
    # The ONLY first-class way Objection.js declares a table. Anchored on `static get tableName`
    # (a class-level keyword sequence unique to this ORM pattern; no other JS/TS idiom writes
    # `static get tableName()`). Scans up to 256 chars inside the brace body to find the
    # `return 'Name'` statement. Never-crash (bounded). Content-free.
    re.compile(
        r'\bstatic\s+get\s+tableName\s*\(\s*\)\s*\{[^}]{0,256}\breturn\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    # ---- FIX 3b: Objection.js static field `static tableName = 'name'` ----
    # Class-field form (TypeScript / modern JS). Anchored on `static tableName =` at a word
    # boundary so a plain `tableName =` assignment in a function body cannot match (it lacks
    # the `static` keyword). No semicolon required (TypeScript files often omit ASI semicolons).
    # Content-free.
    re.compile(r'\bstatic\s+tableName\s*=\s*[\'"]([A-Za-z_][\w.]*)[\'"]\s*(?:;|\n|\r)'),
    # ---- FIX 4: Bookshelf.js `Model.extend({tableName: 'posts'})` ----
    # Knex-based ORM. Anchored on `Model` / `bookshelf` / `Bookshelf` / `Collection` before
    # `.extend(` or `.model(` so that `unknownLib.extend({tableName: 'x'})` does NOT fire.
    # The [^)]{0,512} scan is bounded and stops at the first `)` — handles the common
    # `tableName` as the first or early key; misses cases where a method with `()` precedes
    # the key (acceptable: precision > recall here). Content-free.
    re.compile(
        r'(?:Model|bookshelf|Bookshelf|Collection)\s*\.\s*(?:extend|model)\s*\([^)]{0,512}\btableName\s*:\s*[\'"]([A-Za-z_][\w.]*)[\'"]\s*', re.S),
    # ---- FIX 5: TypeORM/MikroORM `@Entity({tableName: 'x'})` object-argument form ----
    # The existing `_ORM_TYPEORM_EXPLICIT_RE` handles `@Entity("string")`. This handles the
    # object-literal form `@Entity({tableName: 'x'})` / `@Entity({name:'x',...})`. Anchored
    # on `@Entity` followed immediately by `({` so bare `@Entity()`, `@Entity("str")`, and
    # `@Entity(myVar)` do NOT match (they are handled by other patterns). Bounded to 512
    # chars inside the braces. Runs BEFORE `_ORM_JPA_ENTITY_RE` via position in list.
    # Content-free.
    re.compile(
        r'@Entity\s*\(\s*\{[^}]{0,512}\btableName\s*:\s*[\'"]([A-Za-z_][\w.]*)[\'"]\s*'),
]
# Django migration ops that name a table by its (lowercased) MODEL name — the bridge for
# the common case where a model has no explicit db_table (Django derives the table from the
# model). `model_name="individualmember"` in a migration must collide with
# `class IndividualMember(models.Model)` in models.py on `individualmember`.
_ORM_MODELNAME_RE = re.compile(r'\bmodel_name\s*=\s*[\'"]([A-Za-z_]\w*)[\'"]', re.I)
# CreateModel(name="Ticket", ...) — a Django migration creating a model (its table).
_ORM_CREATEMODEL_RE = re.compile(r'\bCreateModel\s*\(\s*name\s*=\s*[\'"]([A-Za-z_]\w*)[\'"]')
# CreateModel("Ticket", [...]) — the POSITIONAL form of the same Django migration op. Django's
# CreateModel signature is (name, fields, ...), so the FIRST positional argument is the model
# name; on the real django repo 43 of 69 (62%) CreateModel calls use this form and were MISSED
# by the keyword-only pattern above, so those migrations never minted a table node and could not
# couple (via the shared table) to the models that query it — a false-clear on a real
# migration<->model collision. Anchored on `CreateModel(` immediately followed by a string
# literal (no `name=`), the capture is the FIRST positional arg ONLY — strings deeper in the
# argument list (field names / options) cannot match because the regex consumes from the open
# paren straight to the first token. Content-free: only the model NAME (an identifier) is taken.
# Additive to the keyword form; _norm_table dedup means a keyword-form and positional-form table
# of the same name produce one identical normalized table.
_ORM_CREATEMODEL_POS_RE = re.compile(r'\bCreateModel\s*\(\s*[\'"]([A-Za-z_]\w*)[\'"]')
# A Django/SQLAlchemy ORM model class: `class Releaser(models.Model)` /
# `class Foo(Base)` / `class Bar(db.Model)`. The model NAME, lowercased, is the table
# candidate (precision-biased: only a base that screams ORM model; it only COUPLES if
# another source names the same table).
_ORM_MODEL_CLASS_RE = re.compile(
    r'\bclass\s+([A-Za-z_]\w*)\s*\(\s*[\w.]*\b(?:models\.Model|db\.Model|Base)\b')
# TypeORM explicit table name: `@Entity("custom_table")` / `@Entity("orders", {...})`.
# This MUST run BEFORE _ORM_JPA_ENTITY_RE so that the explicit string is extracted as a
# table name in addition to (not instead of) the class name. The existing JPA entity
# pattern will also fire and yield the class name as a coupling anchor — both are kept for
# recall safety. Anchored on `@Entity(` followed immediately by a string literal; bare
# `@Entity()` (no arg) and `@Entity(myVar)` (variable arg) do not match.
_ORM_TYPEORM_EXPLICIT_RE = re.compile(
    r'@Entity\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]')
# JPA entity without explicit @Table: `@Entity` then `class Order { ... }` — the entity
# CLASS name (lowercased) is the table candidate (JPA default = the entity name).
# NOTE: line-anchored, NOT re.S. The old `@Entity\b[^;{]*?\bclass\s+(\w+)` with re.S crossed newlines and
# matched the words "class name" / "class definition" inside an INLINE COMMENT on the `@Entity` line
# (`@Entity()  // uses class name`), minting English comment words ('name'/'definition') as table nodes — a
# false coupling that now causes a false PAUSE. The fix consumes ONLY the `@Entity` decorator line (no newline
# crossing), then any stacked `@decorator` lines, then the class declaration on its own line.
_ORM_JPA_ENTITY_RE = re.compile(
    r'@Entity\b[^\n;{]*\n'                 # the @Entity decorator line itself — cannot cross into a // or /* comment that mentions "class X"
    r'(?:[ \t]*@[^\n]*\n)*'                # any further stacked @-decorator lines between @Entity and the class
    r'[ \t]*(?:public\s+|export\s+|export\s+default\s+|abstract\s+|final\s+|sealed\s+|data\s+|open\s+)*'
    r'\bclass\s+([A-Za-z_]\w*)')           # the class declaration (line-start modifiers then `class Name`)


# ---- Bug 1: Rails column-DDL methods, BLOCK-AWARE (column-as-table mis-capture fix) ----
# `add_column :orders, :total` at TOP LEVEL → first symbol is the TABLE (keep). But the SAME
# `add_column :url, :text` INSIDE a `create_table`/`change_table`/`.table :t do` block (e.g.
# discourse's `Migrations::Tooling::Schema.table :uploads do … add_column :url, :text … end`)
# has its first symbol be a COLUMN, not a table — the enclosing block already named the table.
# The old plain-regex entry minted those column names (`url`/`type`/`path`/`data`/`message`) as
# FALSE low-participant tables that slip past resource-hub dampening and false-couple (and, with
# the prose-FROM bug, get queried by English prose). Fix: emit the first symbol as a table ONLY
# when NOT inside an enclosing table-DSL block. RECALL-SAFE: the real table is still minted by the
# enclosing create_table/change_table/.table opener (verified), so suppressing the misread loses
# zero real tables; top-level migration `add_column :t, :col` is unchanged.
_RAILS_COL_DDL_RE = re.compile(
    r'\b(?:add_column|remove_column|rename_column|change_column(?:_null|_default)?'
    r'|add_index|remove_index|add_foreign_key|remove_foreign_key'
    r'|add_timestamps|remove_timestamps)\s*\(?\s*'
    r'(?::([A-Za-z_]\w*)|[\'"]([A-Za-z_][\w.]*)[\'"])')
# A line that OPENS a Rails TABLE-DSL block — the block whose body's add_column first-arg is a
# COLUMN (not a table): `create_table :t do |t|`, `change_table :t do`, and the dotted-receiver
# `Schema.table :t do` (discourse `Migrations::Tooling::Schema.table :uploads do`). Must carry a
# trailing `do` (a block). The `.table` form requires the dot so a bare local `table(:x) do` does
# not fire.
_RAILS_TBL_BLOCK_OPEN_RE = re.compile(
    r'\b(?:create_table|change_table)\b[^\n]*\bdo\b'
    r'|\.\s*table\s+[:\'"][A-Za-z_][\w.]*[\'"]?[^\n]*\bdo\b')
# A line that opens ANY Ruby block that must be closed by `end` — a trailing `do` (with optional
# `|block, args|`), OR a leading block keyword (def/class/module/begin/case/while/until/for, and a
# NON-modifier leading if/unless). Counting these keeps the block STACK balanced so a generic block
# nested INSIDE a table-DSL block (`create_table … do |t| … [1,2].each do |i| … end … end`) does not
# let the inner `end` prematurely pop the table-DSL frame. Statement modifiers (`x if y`, `a.map { }`)
# do NOT match (they are not block-keyword-leading and use braces, not do/end).
_RUBY_BLOCK_OPEN_RE = re.compile(
    r'\bdo\b\s*(?:\|[^|]*\|)?\s*$'
    r'|^\s*(?:def|class|module|begin|case|while|until|for|if|unless)\b')
# A line that closes a Ruby block: a bare `end` token (start of line or after whitespace, end of line).
_RUBY_END_RE = re.compile(r'(?:^|\s)end\b\s*$')


def _rails_column_ddl_refs(text):
    """Rails column/index/fk DDL methods → table NAMES, BLOCK-AWARE (Bug 1).

    Scans line by line maintaining a STACK of Ruby block frames (each tagged table-DSL or not). A
    column-DDL method's first symbol is minted as a table ONLY when NO enclosing frame is a TABLE-DSL
    block (create_table / change_table / `.table :x do`). A top-level migration op (`add_column
    :users, :col`, even inside a plain `def up`/`class` block) keeps its first arg as the table; a
    column-DDL call inside a create_table/change_table/.table block has a COLUMN as its first arg and
    is suppressed (the enclosing opener already minted the real table). The full block stack (not a
    table-DSL-only counter) ensures a generic block (`.each do`, `reversible do`) nested inside a
    table-DSL block cannot let its `end` pop the table-DSL frame early. Content-free: NAMES only."""
    out = set()
    stack = []   # list of bool: is this frame a TABLE-DSL block?
    table_dsl_depth = 0   # count of TRUE frames on `stack` == any(stack); maintained in O(1) per
                          # open/close so the per-line enclosing-context test is O(1), not O(depth)
                          # (an `any(stack)` re-scan per line is quadratic in line count on a deeply
                          # nested / pathological schema — DoS gate). Invariant: table_dsl_depth ==
                          # sum(stack); `not any(stack)` <=> table_dsl_depth == 0.
    for line in text.split("\n"):
        # Check column-DDL calls against the CURRENT enclosing context (before this line's own opener
        # takes effect — an add_column never shares a line with a create_table…do opener in practice).
        if table_dsl_depth == 0:
            for m in _RAILS_COL_DDL_RE.finditer(line):
                raw = m.group(1) or m.group(2)
                t = _norm_table(raw)
                if t and t not in _SQL_NONTABLE:
                    out.add(t)
        # Maintain the block stack. A line can close (an `end`) and/or open a frame; in Ruby an
        # `end` line does not also open a block, so handling them on separate branches is correct.
        if _RUBY_END_RE.search(line):
            if stack:
                table_dsl_depth -= stack.pop()   # bool: -1 iff the popped frame was a TABLE-DSL block
        if _RUBY_BLOCK_OPEN_RE.search(line):
            is_tbl = bool(_RAILS_TBL_BLOCK_OPEN_RE.search(line))
            stack.append(is_tbl)
            table_dsl_depth += is_tbl
    # DETERMINISM: emit stably (the caller re-adds into another set, but keep it canonical like
    # its sibling ref fns) — sort by table NAME, content-free.
    return sorted(out)


def _orm_table_refs(text):
    """ORM table declarations in a source file → a set of normalized table NAMES.
    Content-free (names only). Each name was anchored on an ORM-specific token, so
    ordinary code / prose does not contribute.

    Some patterns (Rails create_table/alter methods) use two capture groups — group 1
    for the colon-symbol form (`:orders`) and group 2 for the quoted form (`"orders"`).
    We pick whichever group matched (`m.group(1) or m.group(2)`)."""
    out = set()
    for pat in _ORM_PATTERNS:
        for m in pat.finditer(text):
            # Patterns may have 1 or 2 capture groups; take the first non-None.
            raw = next((g for g in m.groups() if g is not None), None)
            t = _norm_table(raw)
            if t and t not in _SQL_NONTABLE:
                out.add(t)
    # Bug 1: Rails column-DDL methods (add_column / add_index / ...) — BLOCK-AWARE. At top level
    # the first symbol is the table (kept); inside a create_table/change_table/.table block it is a
    # COLUMN and is suppressed (the enclosing opener already minted the real table).
    for t in _rails_column_ddl_refs(text):
        out.add(t)
    # Model/entity/migration NAMES → lowercased table candidate (the no-explicit-db_table
    # bridge). `_norm_table` lowercases; class/model names carry no dots so the
    # last-segment split is a no-op.
    # _ORM_TYPEORM_EXPLICIT_RE runs before _ORM_JPA_ENTITY_RE so the explicit string is
    # captured; the JPA pattern also fires (class name stays as a coupling anchor — additive,
    # recall-safe).
    for pat in (_ORM_MODELNAME_RE, _ORM_CREATEMODEL_RE, _ORM_CREATEMODEL_POS_RE,
                _ORM_MODEL_CLASS_RE,
                _ORM_TYPEORM_EXPLICIT_RE, _ORM_JPA_ENTITY_RE):
        for m in pat.finditer(text):
            t = _norm_table(m.group(1))
            if t and t not in _SQL_NONTABLE:
                out.add(t)
    # Go gorm/ent: explicit names (TableName() / gorm table: tag / entsql.Annotation),
    # run after comment stripping so doc-comment examples do not fire.
    for t in _go_explicit_refs(text):
        out.add(t)
    # Go gorm: struct names (snake_case pluralized) from gorm.Model embedding + gorm tags.
    for t in _go_gorm_struct_names(text):
        if t and t not in _SQL_NONTABLE:
            out.add(t)
    # Go ent: struct names (snake_case pluralized) from ent.Schema embedding + Fields/Annotations.
    for t in _go_ent_schema_names(text):
        if t and t not in _SQL_NONTABLE:
            out.add(t)
    # DETERMINISM: the consumer (_cg_schema._schema_graph) iterates this set DIRECTLY into
    # `queries`/`alters` edge emission, so its order fixes graph edge order. A bare set's str
    # iteration order varies by PYTHONHASHSEED → byte-churn across identical runs. Sort by the
    # table NAME (content-free — same names, fixed order). MEASURED divergence: this fn's own
    # ORM refs (`table::stringX` vs `table::stringY` swap) was the first cross-seed divergence.
    return sorted(out)


# ---- MULTI-DEFINER: CREATE-class ("creator") detection (precision, recall-critical) ----
# The multi-definer ambiguity for SCHEMA is NOT "a table touched by >1 file". A DB table has a
# CREATE-then-ALTER lifecycle: ONE migration CREATEs `users`, then N LATER migrations ALTER it
# (add_column, add_index, …) — that is the NORMAL, correct Laravel/Rails/Django/Alembic pattern,
# and those migrations DO genuinely couple (they all evolve the same `users` schema). Counting
# every migration that touches a table as a "definer" would suppress essentially every evolved
# table in every real migration repo = a catastrophic recall loss (MEASURED: the ORM-DSL gate's
# Laravel fixture is exactly 1 CREATE + 1 ALTER of `users`).
#
# The genuinely ambiguous case is >1 file that CREATES the table FROM SCRATCH — two `CREATE TABLE
# orders`, two `Schema::create('orders')`, two `CreateModel('Ticket')`, or two ORM models mapping
# the same `__tablename__`. So suppression counts CREATORS only; ALTER-class touches never count.
# An ORM MODEL declaration is a creator (it owns/maps the table); a migration ALTER op is not.
#
# CREATE-class ORM forms (the table is BORN here). Anchored exactly like their _ORM_PATTERNS
# counterparts; the ALTER-class siblings (Schema::table / change_table / add_column / op.add_column /
# migrationBuilder.AddColumn / alter table( / .alterTable / AddField / model_name=) are deliberately
# ABSENT so they are not creators. Content-free: table/model NAME only.
_CREATE_ORM_PATTERNS = [
    # SQLAlchemy / SQLModel / Django model class — a model OWNS its table mapping (a creator).
    re.compile(r'__tablename__\s*=\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    re.compile(r'db_table[\'"]?\s*[:=]\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    re.compile(r'\bclass\s+([A-Za-z_]\w*)\s*\([^)]*\btable\s*=\s*True'),        # SQLModel table=True
    re.compile(r'\bclass\s+([A-Za-z_]\w*)\s*\(\s*[\w.]*\b(?:models\.Model|db\.Model|Base)\b'),  # ORM model class
    re.compile(r'\bself\.table_name\s*=\s*(?::([A-Za-z_]\w*)|[\'"]([A-Za-z_][\w.]*)[\'"])'),  # Rails explicit
    re.compile(r'\bprotected\s+\$table\s*=\s*[\'"]([A-Za-z_][\w.]*)[\'"]\s*;'),  # Laravel Eloquent model
    # JPA / TypeORM entity (the entity class IS the table owner).
    re.compile(r'@Table\s*\([^)]*\bname\s*=\s*[\'"]([A-Za-z_][\w.]*)[\'"]', re.I),
    re.compile(r'@Entity\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    re.compile(r'@Entity\s*\(\s*\{[^}]{0,512}\btableName\s*:\s*[\'"]([A-Za-z_][\w.]*)[\'"]\s*'),
    # CREATE-class migration / schema-builder ops.
    re.compile(r'\bcreate_table\s+(?::([A-Za-z_]\w*)|[\'"]([A-Za-z_][\w.]*)[\'"])'),   # Rails create_table
    re.compile(r'\bSchema\s*::\s*create\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]', re.I),    # Laravel Schema::create
    re.compile(r'\.createTable(?:IfNotExists)?\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),   # Knex .createTable
    re.compile(r'\bop\.\s*create_table\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),           # Alembic op.create_table
    re.compile(r'\bmigrationBuilder\s*\.\s*CreateTable\s*\([^;]{0,512}\bname\s*:\s*[\'"]([A-Za-z_][\w.]*)[\'"]', re.S),  # EF Core
    re.compile(r'\bcreate\s+table\s*\(\s*(?::([A-Za-z_]\w*)|[\'"]([A-Za-z_][\w.]*)[\'"]\s*)'),  # Ecto create table(
    # Drizzle / Mongoose / Sequelize / Prisma / Objection / Bookshelf — each DECLARES the table/model.
    re.compile(r'\b(?:pgTable|mysqlTable|sqliteTable)\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    re.compile(r'\bmongoose\s*\.\s*model\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    re.compile(r'\bsequelize\s*\.\s*define\s*\(\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    re.compile(r'\bmodel\s+([A-Za-z_]\w*)\s*\{'),                                       # Prisma model
    re.compile(r'\bstatic\s+get\s+tableName\s*\(\s*\)\s*\{[^}]{0,256}\breturn\s*[\'"]([A-Za-z_][\w.]*)[\'"]'),
    re.compile(r'\bstatic\s+tableName\s*=\s*[\'"]([A-Za-z_][\w.]*)[\'"]\s*(?:;|\n|\r)'),
]
# CREATE-class Django/JPA model-name forms (the model NAME → lowercased table). CreateModel CREATES
# the model's table; AddField / model_name= (an ALTER) is intentionally EXCLUDED.
_CREATE_NAME_PATTERNS = [
    re.compile(r'\bCreateModel\s*\(\s*name\s*=\s*[\'"]([A-Za-z_]\w*)[\'"]'),
    re.compile(r'\bCreateModel\s*\(\s*[\'"]([A-Za-z_]\w*)[\'"]'),
    _ORM_TYPEORM_EXPLICIT_RE,
    _ORM_JPA_ENTITY_RE,
]


def _orm_creator_refs(text):
    """Tables this file CREATES from scratch (a creator), content-free NAMES only. A creator =
    raw CREATE TABLE (handled separately for .sql) OR an ORM CREATE-class op OR a model declaration
    that OWNS the table. ALTER-class touches (add_column / Schema::table / op.add_column / AddField /
    CREATE INDEX/TRIGGER / raw ALTER TABLE) are NOT creators and are excluded, so the normal
    one-create-many-alter migration lifecycle keeps a table SINGLE-creator (recall-safe)."""
    out = set()
    for pat in _CREATE_ORM_PATTERNS:
        for m in pat.finditer(text):
            raw = next((g for g in m.groups() if g is not None), None)
            t = _norm_table(raw)
            if t and t not in _SQL_NONTABLE:
                out.add(t)
    for pat in _CREATE_NAME_PATTERNS:
        for m in pat.finditer(text):
            t = _norm_table(m.group(1))
            if t and t not in _SQL_NONTABLE:
                out.add(t)
    # Go gorm/ent: explicit names + struct declarations are model declarations → creators.
    for t in _go_explicit_refs(text):
        out.add(t)
    for t in _go_gorm_struct_names(text):
        if t and t not in _SQL_NONTABLE:
            out.add(t)
    for t in _go_ent_schema_names(text):
        if t and t not in _SQL_NONTABLE:
            out.add(t)
    # DETERMINISM: the caller iterates creators into a per-table creator SET keyed by table
    # name; the set values don't reach edge order, but emit stably anyway so this fn is
    # canonical like its siblings (content-free — sort by table NAME).
    return sorted(out)
