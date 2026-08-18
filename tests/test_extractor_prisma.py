#!/usr/bin/env python3
"""Prisma ORM extractor gate.

THE GAP: .prisma files were NOT in _SOURCE_EXT (never walked) and _ORM_PATTERNS had no
`model {}` pattern, so Prisma apps minted 0 table nodes. The cross-substrate crown jewel
(a .sql migration alters `users` + a .ts file queries `users` + the `model User {}` in
schema.prisma declares the table) was invisible.

WHAT THIS GATE PROVES (hermetic, no DB, no Postgres, no network):
  1. `model User { ... }` in schema.prisma mints a table node named "user".
  2. A second model `model Post { ... }` mints a second table node "post".
  3. `enum Role { ... }` does NOT mint a table node (wrong keyword, not `model`).
  4. `datasource db { ... }` does NOT mint a table node.
  5. `generator client { ... }` does NOT mint a table node.
  6. CROSS-SUBSTRATE CROWN JEWEL: a SQL migration that ALTERs `user` and a .ts file
     that queries `user` are coupled via the shared table node from schema.prisma, with
     NO import/call edge between the two files.
  7. Never-crash: empty .prisma file does not raise.
  8. Content-free: no file body is emitted -- only table NAMES, edge kinds, paths.

Print PRISMA GATE: PASS on success, PRISMA GATE: FAIL on any failure.
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
# 1+2+3+4+5: Model nodes minted, enum/datasource/generator do NOT fire
# ---------------------------------------------------------------------------
def test_prisma_models_and_precision():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "prisma", "schema.prisma"), """\
datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

generator client {
  provider = "prisma-client-js"
}

enum Role {
  ADMIN
  USER
}

model User {
  id    String @id @default(uuid())
  email String @unique
  role  Role   @default(USER)
}

model Post {
  id      String @id @default(uuid())
  title   String
  content String
}
""")
        g = X.build_graph(root)
        tables = _tables(g)

        # 1. model User -> table node
        assert "user" in tables, f"expected 'user' table, got: {tables}"
        # 2. model Post -> table node
        assert "post" in tables, f"expected 'post' table, got: {tables}"
        # 3. enum Role must NOT mint a table
        assert "role" not in tables, f"'role' (enum) must not be a table, got: {tables}"
        # 4. datasource must NOT mint a table
        assert "db" not in tables, f"'db' (datasource) must not be a table, got: {tables}"
        # 5. generator must NOT mint a table
        assert "client" not in tables, f"'client' (generator) must not be a table, got: {tables}"


# ---------------------------------------------------------------------------
# 6. Cross-substrate crown jewel
#    schema.prisma declares model User (table node "user")
#    migration.sql ALTERs the users/user table (alters edge)
#    query.ts SELECTs FROM user (queries edge)
#    Result: migration and query are coupled via the shared table node —
#    even though there is NO import/call edge between them
# ---------------------------------------------------------------------------
def test_cross_substrate_crown_jewel():
    with tempfile.TemporaryDirectory() as root:
        # Prisma schema declares the table
        _w(os.path.join(root, "prisma", "schema.prisma"), """\
model User {
  id    String @id
  email String @unique
}
""")
        # SQL migration touches the same table
        _w(os.path.join(root, "prisma", "migrations", "001_init", "migration.sql"), """\
ALTER TABLE "user" ADD COLUMN "createdAt" TIMESTAMP NOT NULL DEFAULT now();
""")
        # TypeScript code queries the same table
        _w(os.path.join(root, "src", "getUsers.ts"), """\
import { db } from './db';
async function getUsers() {
  return db.query('SELECT id, email FROM "user" WHERE 1=1');
}
""")
        g = X.build_graph(root)
        tables = _tables(g)

        # Crown jewel table must exist
        assert "user" in tables, f"crown-jewel table 'user' not found; tables={tables}"

        alters = _edges_of_kind(g, "alters")
        queries = _edges_of_kind(g, "queries")

        # Identify the paths (relpath, OS-normalised)
        migration_rel = os.path.relpath(
            os.path.join(root, "prisma", "migrations", "001_init", "migration.sql"), root
        ).replace(os.sep, "/")
        ts_rel = os.path.relpath(
            os.path.join(root, "src", "getUsers.ts"), root
        ).replace(os.sep, "/")

        alters_srcs = [src for src, dst in alters if dst == "user"]
        queries_srcs = [src for src, dst in queries if dst == "user"]

        assert migration_rel in alters_srcs, (
            f"migration must have alters->user edge; alters={alters}"
        )
        assert ts_rel in queries_srcs, (
            f"ts file must have queries->user edge; queries={queries}"
        )

        # Confirm NO import/call edge exists between the two files (the crown jewel)
        direct_edges = [
            e for e in g["edges"]
            if e["kind"] in ("imports", "calls")
            and {e["src"], e["dst"]} == {migration_rel, ts_rel}
        ]
        assert not direct_edges, (
            f"crown jewel broken: unexpected direct edge between migration and ts: {direct_edges}"
        )


# ---------------------------------------------------------------------------
# 7. Never-crash: empty .prisma file
# ---------------------------------------------------------------------------
def test_empty_prisma_no_crash():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "prisma", "schema.prisma"), "")
        try:
            g = X.build_graph(root)
        except Exception as exc:
            raise AssertionError(f"build_graph raised on empty .prisma: {exc}") from exc
        # No table nodes expected
        tables = _tables(g)
        assert not tables, f"empty .prisma should mint no tables; got: {tables}"


# ---------------------------------------------------------------------------
# 8. Content-free: no file body emitted
# ---------------------------------------------------------------------------
def test_content_free():
    secret = "my_super_secret_password_XYZ"
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "prisma", "schema.prisma"), f"""\
// this file contains a secret: {secret}
model Order {{
  id String @id
}}
""")
        g = X.build_graph(root)
        graph_str = str(g)
        assert secret not in graph_str, (
            f"content-free violated: secret found in graph output"
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    failures = []
    tests = [
        test_prisma_models_and_precision,
        test_cross_substrate_crown_jewel,
        test_empty_prisma_no_crash,
        test_content_free,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:
            failures.append(f"{t.__name__}: {exc}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print("PRISMA GATE: FAIL")
        sys.exit(1)
    else:
        print("PRISMA GATE: PASS")
        sys.exit(0)
