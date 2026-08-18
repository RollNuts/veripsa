"""Gate: SCHEMA multi-definer suppression (precision) + recall preservation.

The schema detector (_cg_schema._schema_graph) mints a `table` node for every DB table a repo
DECLARES (raw DDL in .sql, or an ORM model: __tablename__, @Entity, CreateModel, a gorm/ent
struct, …) and emits an `alters` edge from each migration/DDL source and a `queries` edge from
each model / raw-SQL consumer to that table NAME. The downstream shared-resource adjacency
(db/schema res_adj) then couples any two files that both touch the SAME table name — the moat
that code-only graphs miss.

Before this fix there was NO multi-definer guard: when a table NAME was DECLARED by >1 file in the
same role (two migrations both `CREATE TABLE orders`, or two ORM models both mapping `orders`), a
single querier false-coupled to ALL of them AND the redundant definers false-coupled to each other.
That is the exact bug class its sibling detectors were fixed for (openapi #329, iac #330,
api-contract #332, config #339, routes #340 — each 66-91% of their false couples were this class).

This gate measures the fix on crafted temp repos (offline, NO Postgres — it drives _schema_graph
directly). It proves:

  (1) PRECISION (two .sql migrations) — two REAL .sql files both `CREATE TABLE orders` + one raw-SQL
      querier: the ambiguous table anchors NO coupling at all (0 pairs).
  (2) PRECISION (two ORM models) — two REAL non-test ORM models both declare `__tablename='orders'`
      + one querier: suppressed (0 pairs). This case has NO `alters` edges, so an alters-only guard
      would MISS it — the role-split CREATOR count (sql-creators OR model-creators) catches it.
  (3) RECALL (single migration) — a SINGLE .sql migration + its one querier still couple (1 pair).
  (4) RECALL (single model) — a SINGLE ORM model + a raw-SQL querier still couple (1 pair).
  (5) RECALL CROWN-JEWEL — the legitimate moat pair (ONE migration that CREATEs a table + ONE ORM
      model that maps it = 1 sql-creator + 1 model-creator) is NOT ambiguous and survives, AND it
      stays coupled to the querier; no spurious definer↔definer is fabricated.
  (6) RECALL-SAFE TEST SHADOW — a real migration + a TEST factory declaring the same table: the test
      file is excluded as a DEFINER (Fix 1) so the table stays SINGLE-definer and the real coupling is
      preserved; the test file is never coupled.
  (7) RECALL-SAFE TEST QUERIER — a TEST file with raw SQL (`INSERT INTO users …`) stays coupled to the
      real migration (test files are excluded only as DEFINERS, kept as raw-SQL QUERIERS).
  (8) NODE HONESTY — an ambiguous table's NODE is still minted (the table genuinely exists); only its
      coupling edges are retained as explicitly ambiguous evidence.

Ambiguous evidence is excluded from the effective pair oracle below, matching the DB
adjacency contract. Content-free throughout: only table NAMES + file paths are read,
never bodies or values.
Prints SCHEMA MULTI-DEFINER GATE: PASS on success, ... FAIL on any failure.
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import _cg_schema as S


def _write(root, path, body):
    fp = os.path.join(root, path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w") as fh:
        fh.write(body)


def _src_files(root):
    out = []
    for dp, _dns, fns in os.walk(root):
        for fn in fns:
            full = os.path.join(dp, fn)
            out.append((full, os.path.splitext(fn)[1].lower()))
    return out


def _res_pairs(edges, table):
    """The shared-resource coupling anchored on `table`: every pair of DISTINCT files that both
    carry a resolved alters/queries edge to `table` (exactly how res_adj couples them).
    Ambiguous edges remain persisted evidence but are inert. Returns a set of sorted
    (a, b) file-pairs, forward-slash normalized."""
    files = set()
    for e in edges:
        if (
            e.get("dst") == table
            and e.get("kind") in ("alters", "queries")
            and e.get("reference_status") != "ambiguous"
        ):
            files.add(e["src"].replace(os.sep, "/"))
    fl = sorted(files)
    return {(fl[i], fl[j]) for i in range(len(fl)) for j in range(i + 1, len(fl))}


def _build(files):
    """Write `files` to a temp repo, run _schema_graph, return (nodes, edges). Caller cleans up
    via the returned root."""
    root = tempfile.mkdtemp(prefix="schema_multidefiner_")
    for path, body in files.items():
        _write(root, path, body)
    nodes, edges = S._schema_graph(root, _src_files(root))
    return root, nodes, edges


def _flask_select(table):
    return f"def q(c):\n    c.execute('SELECT id FROM {table} WHERE id = 1')\n"


def main():
    failures = []
    roots = []
    try:
        # (1) PRECISION: two REAL .sql migrations both CREATE TABLE orders; one raw-SQL querier.
        root, _n, edges = _build({
            "migrations/001_a.sql": "CREATE TABLE orders (id int, total int);\n",
            "migrations/002_b.sql": "CREATE TABLE orders (id int, status text);\n",
            "app/dao.py": _flask_select("orders"),
        })
        roots.append(root)
        p1 = _res_pairs(edges, "orders")
        if p1:
            print(f"FAIL [1 precision .sql]: two-migration multi-definer must couple NOTHING; got {sorted(p1)!r}")
            failures.append("multi-sql-definer-not-suppressed")
        ambiguous_order_edges = [
            edge
            for edge in edges
            if (
                edge.get("dst") in {"orders", "orders.id", "orders.total", "orders.status"}
                and edge.get("kind") in {
                    "alters", "queries", "alters_col", "queries_col"
                }
            )
        ]
        if not ambiguous_order_edges or any(
            edge.get("reference_status") != "ambiguous"
            for edge in ambiguous_order_edges
        ):
            print(
                "FAIL [1 evidence .sql]: every table/column edge must be retained "
                f"as ambiguous evidence; got {ambiguous_order_edges!r}"
            )
            failures.append("multi-sql-ambiguity-evidence-lost")

        # (2) PRECISION: two REAL non-test ORM models both declare __tablename__='orders'; one querier.
        #     These emit `queries` edges (non-migrations) — an alters-only guard would MISS this.
        root, _n, edges = _build({
            "models/order_v1.py": "class Order(Base):\n    __tablename__ = 'orders'\n",
            "models/order_v2.py": "class OrderArchive(Base):\n    __tablename__ = 'orders'\n",
            "app/dao.py": _flask_select("orders"),
        })
        roots.append(root)
        p2 = _res_pairs(edges, "orders")
        if p2:
            print(f"FAIL [2 precision ORM]: two-model multi-definer must couple NOTHING; got {sorted(p2)!r}")
            failures.append("multi-model-definer-not-suppressed")

        # (3) RECALL: a SINGLE .sql migration + one querier still couple.
        root, _n, edges = _build({
            "migrations/001_inv.sql": "CREATE TABLE invoices (id int);\n",
            "app/inv.py": _flask_select("invoices"),
        })
        roots.append(root)
        p3 = _res_pairs(edges, "invoices")
        want3 = {("app/inv.py", "migrations/001_inv.sql")}
        if p3 != want3:
            print(f"FAIL [3 recall single .sql]: single-definer must couple; expected {sorted(want3)!r}, got {sorted(p3)!r}")
            failures.append("single-sql-definer-recall")

        # (4) RECALL: a SINGLE ORM model + a raw-SQL querier still couple.
        root, _n, edges = _build({
            "models/widget.py": "class Widget(Base):\n    __tablename__ = 'widgets'\n",
            "app/wdao.py": "def w(c):\n    c.execute('INSERT INTO widgets (id) VALUES (1)')\n",
        })
        roots.append(root)
        p4 = _res_pairs(edges, "widgets")
        want4 = {("app/wdao.py", "models/widget.py")}
        if p4 != want4:
            print(f"FAIL [4 recall single model]: single-model definer must couple; expected {sorted(want4)!r}, got {sorted(p4)!r}")
            failures.append("single-model-definer-recall")

        # (5) RECALL CROWN-JEWEL: ONE migration CREATEs + ONE model maps + a querier. The migration<->model
        #     moat pair (1 sql-creator + 1 model-creator) is NOT ambiguous and survives; the querier
        #     couples to both; no test file is involved.
        root, _n, edges = _build({
            "migrations/0001_orders.sql": "CREATE TABLE orders (id int);\n",
            "app/models.py": "class Order(Base):\n    __tablename__ = 'orders'\n",
            "shipping/dispatch.py": "def d(c):\n    c.execute('UPDATE orders SET shipped = true')\n",
        })
        roots.append(root)
        p5 = _res_pairs(edges, "orders")
        moat = ("app/models.py", "migrations/0001_orders.sql")
        if moat not in p5:
            print(f"FAIL [5 crown-jewel moat]: migration<->model moat pair must survive; got {sorted(p5)!r}")
            failures.append("crown-jewel-moat-dropped")
        if len(p5) != 3:
            print(f"FAIL [5 crown-jewel recall]: migration+model+querier should give 3 pairs; got {sorted(p5)!r}")
            failures.append("crown-jewel-recall")

        # (5b) RECALL LIFECYCLE (the recall-critical guard): one migration CREATEs `accounts`, a SECOND
        #      LATER migration only ALTERs it (ADD COLUMN), and a model maps it. This is the NORMAL
        #      one-CREATE-many-ALTER lifecycle of every real Laravel/Rails/Django/Alembic repo — the
        #      second migration is an ALTER, NOT a redundant creator, so `accounts` stays SINGLE-creator
        #      and ALL of (create-migration, alter-migration, model) stay coupled. (A naive "any table
        #      touched by >1 migration is ambiguous" rule would wrongly suppress this — it did, on the
        #      ORM-DSL gate's Laravel 1-create+1-alter `users` fixture.)
        root, _n, edges = _build({
            "migrations/0001_create_accounts.sql": "CREATE TABLE accounts (id int);\n",
            "migrations/0002_add_email.sql": "ALTER TABLE accounts ADD COLUMN email text;\n",
            "app/account.py": "class Account(Base):\n    __tablename__ = 'accounts'\n",
        })
        roots.append(root)
        p5b = _res_pairs(edges, "accounts")
        # All three files share the `accounts` resource → 3 pairs; none dropped.
        if len(p5b) != 3:
            print(f"FAIL [5b lifecycle recall]: 1-CREATE + 1-ALTER + model must all stay coupled "
                  f"(3 pairs); got {sorted(p5b)!r}")
            failures.append("create-then-alter-lifecycle-recall")

        # (6) RECALL-SAFE TEST SHADOW: a real migration + a TEST factory declare the same table. The test
        #     file is excluded as a DEFINER → the table stays single-definer → real coupling preserved; the
        #     test file is never coupled.
        root, _n, edges = _build({
            "migrations/001_carts.sql": "CREATE TABLE carts (id int);\n",
            "tests/factories.py": "class CartFactory:\n    __tablename__ = 'carts'  # test-only\n",
            "app/cdao.py": _flask_select("carts"),
        })
        roots.append(root)
        p6 = _res_pairs(edges, "carts")
        want6 = {("app/cdao.py", "migrations/001_carts.sql")}
        if p6 != want6:
            print(f"FAIL [6 test-shadow recall]: real coupling must survive a test-factory shadow; "
                  f"expected {sorted(want6)!r}, got {sorted(p6)!r}")
            failures.append("test-shadow-recall")
        if any("tests/" in a or "tests/" in b for a, b in p6):
            print(f"FAIL [6 test-shadow precision]: a test factory must never be coupled; got {sorted(p6)!r}")
            failures.append("test-shadow-coupled")

        # (7) RECALL-SAFE TEST QUERIER: a TEST file with raw SQL stays coupled to the real migration
        #     (test files are excluded only as DEFINERS, kept as raw-SQL QUERIERS).
        root, _n, edges = _build({
            "migrations/001_users.sql": "CREATE TABLE users (id int, name text);\n",
            "tests/test_auth.py": "def seed(c):\n    c.execute(\"INSERT INTO users (name) VALUES ('a')\")\n",
        })
        roots.append(root)
        p7 = _res_pairs(edges, "users")
        want7 = {("migrations/001_users.sql", "tests/test_auth.py")}
        if p7 != want7:
            print(f"FAIL [7 test-querier recall]: a test file's raw SQL must stay coupled to the migration; "
                  f"expected {sorted(want7)!r}, got {sorted(p7)!r}")
            failures.append("test-querier-recall")

        # (8) EVIDENCE HONESTY: an ambiguous table's NODE and edges remain, but every edge is
        #     explicitly inert rather than becoming effective adjacency.
        root, nodes, edges = _build({
            "migrations/001_a.sql": "CREATE TABLE orders (id int);\n",
            "migrations/002_b.sql": "CREATE TABLE orders (id int);\n",
        })
        roots.append(root)
        if not any(n.get("kind") == "table" and n.get("name") == "orders" for n in nodes):
            print("FAIL [8 node honesty]: an ambiguous table's NODE must still be minted (it exists)")
            failures.append("ambiguous-node-dropped")
        order_edges = [
            edge
            for edge in edges
            if edge.get("dst") == "orders"
            and edge.get("kind") in ("alters", "queries")
        ]
        if len(order_edges) != 2 or any(
            edge.get("reference_status") != "ambiguous"
            for edge in order_edges
        ):
            print(
                "FAIL [8 edge honesty]: both ambiguous definers must remain "
                f"as inert evidence; got {order_edges!r}"
            )
            failures.append("ambiguous-edge-evidence-lost")

    finally:
        for r in roots:
            shutil.rmtree(r, ignore_errors=True)

    if failures:
        print(f"SCHEMA MULTI-DEFINER GATE: FAIL (failures: {failures})")
        sys.exit(1)
    print("SCHEMA MULTI-DEFINER GATE: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
