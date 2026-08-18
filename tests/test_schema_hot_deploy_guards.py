#!/usr/bin/env python3
"""Hot-deploy schema guard.

Render runs db/schema.sql in preDeploy while the previous app image still serves
webhooks. Top-level table DDL against existing busy tables can block behind live
transactions, hit statement_timeout, and fail the deploy. The schema may still
repair missing objects inside catalog-guarded DO blocks; it must not replay raw
owner/policy/RLS/trigger DDL every deploy.
"""
from __future__ import annotations

import hashlib
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_DIR = os.path.join(ROOT, "db", "schema")
SCHEMA_INDEX = os.path.join(ROOT, "db", "schema.sql")
ONLINE_INDEX_REPAIR = os.path.join(SCHEMA_DIR, "05_online_index_repair.sql")
SUBSTRATE = os.path.join(SCHEMA_DIR, "10_substrate.sql")
CUTOVER = os.path.join(SCHEMA_DIR, "100_schema_cutover.sql")
ONLINE_DDL_HELPERS = (
    "_ensure_column_online",
    "_ensure_column_default_online",
    "_ensure_column_not_null_online",
    "_ensure_constraint_valid_online",
)
ACL_HELPER_START = "CREATE FUNCTION core._normalize_cutover_acl_v1()"
ACL_HELPER_OWNER = (
    "ALTER FUNCTION core._normalize_cutover_acl_v1()\n"
    "  OWNER TO veripsa_migrator;"
)
ACL_HELPER_REVOKE = (
    "REVOKE ALL PRIVILEGES ON FUNCTION core._normalize_cutover_acl_v1()\n"
    "  FROM PUBLIC;"
)
ACL_HELPER_CALL = "SELECT core._normalize_cutover_acl_v1();"
ACL_HELPER_DROP = "DROP FUNCTION core._normalize_cutover_acl_v1();"
ACL_HELPER_DEFINITION = re.compile(
    r"CREATE FUNCTION core\._normalize_cutover_acl_v1\(\)\s+"
    r"RETURNS void\s+"
    r"LANGUAGE plpgsql\s+"
    r"SECURITY INVOKER\s+"
    r"SET search_path TO 'pg_catalog'\s+"
    r"AS (?P<tag>\$[A-Za-z0-9_]*\$)"
    r"(?P<body>.*?)"
    r"(?P=tag);",
    re.S,
)
# This helper is the intentionally narrow exception to the publication
# transaction's opaque-body ban. Hash its complete create/own/revoke/call/drop
# slice so a future hidden relation lock cannot evade the lightweight lexer.
ACL_HELPER_AUDITED_SHA256 = (
    "92d445dc2f9b6cd1b9cc7e1b3b7fcb3e665116c2bf012fed311a2f12c9867459"
)

DOLLAR_TAG = re.compile(r"\$[A-Za-z0-9_]*\$")
UNSAFE_TOP_LEVEL = [
    # Every idempotent table alteration must inspect pg_catalog first and issue
    # DDL only when the contract is genuinely absent/different. PostgreSQL
    # otherwise takes AccessExclusiveLock even for ADD COLUMN IF NOT EXISTS,
    # same-default, and already-NOT-NULL no-ops.
    re.compile(r"ALTER\s+TABLE\s+(?:ONLY\s+)?core\.", re.I),
    # ALTER SEQUENCE OWNER conflicts with live nextval/currval/setval users
    # even when ownership is already correct.
    re.compile(r"ALTER\s+SEQUENCE\s+core\.", re.I),
    re.compile(r"DROP\s+POLICY\s+IF\s+EXISTS\b", re.I),
    re.compile(r"CREATE\s+OR\s+REPLACE\s+TRIGGER\b", re.I),
    re.compile(r"CREATE\s+TRIGGER\s+trg_", re.I),
    # Normal CREATE INDEX takes ShareLock before IF NOT EXISTS can return, even
    # for an already-valid name. It conflicts with ordinary RowExclusive live
    # traffic and can convoy later writers behind the deploy.
    re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!CONCURRENTLY\b)", re.I),
]
UNSAFE_TOP_LEVEL_TEXT = [
    re.compile(r"ALTER\s+TABLE\s+(?:ONLY\s+)?core\.", re.I),
    re.compile(r"ALTER\s+SEQUENCE\s+core\.", re.I),
    re.compile(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!CONCURRENTLY\b)",
        re.I,
    ),
]
CONCURRENT_INDEX_START = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\b",
    re.I,
)
CONCURRENT_INDEX_DEF = re.compile(
    r"^CREATE\s+(UNIQUE\s+)?INDEX\s+CONCURRENTLY\s+IF\s+NOT\s+EXISTS\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s+ON\s+core\."
    r"([A-Za-z_][A-Za-z0-9_]*)\b",
    re.I | re.S,
)
REPAIR_INDEX = re.compile(
    r"^\s*\('([A-Za-z_][A-Za-z0-9_]*)',"
    r"'([A-Za-z_][A-Za-z0-9_]*)',(true|false),"
    r"'([0-9a-f]{32})'\)(?:,|;)?\s*$",
    re.I,
)
TX_BEGIN = re.compile(r"^BEGIN\s*;\s*$", re.I)
TX_COMMIT = re.compile(r"^(?:COMMIT|ROLLBACK)\s*;\s*$", re.I)
# A relation lock or row lock acquired by one of these statements is retained
# until COMMIT. Keeping any of them in a long function-publication transaction
# turns an otherwise brief hot-deploy DDL lock into a live-worker convoy. A DO
# block is forbidden wholesale in that transaction: its body is deliberately
# opaque to the lightweight dollar-quote lexer, so allowing the wrapper would
# let `DO $$ BEGIN ALTER TABLE ...` evade every top-level DDL check.
LOCK_RETAINING_IN_TX = re.compile(
    r"^(?:"
    r"DO\b|"
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\b|"
    r"CREATE\s+TABLE\b|"
    r"ALTER\s+TABLE\b|"
    r"ALTER\s+SEQUENCE\b|"
    r"DROP\s+(?:TABLE|INDEX|TRIGGER)\b|"
    r"CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\b|"
    r"(?:CREATE|ALTER|DROP)\s+POLICY\b|"
    r"(?:GRANT|REVOKE)\b.*\bON\s+TABLE\b|"
    r"TRUNCATE\b|"
    r"(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+core\."
    r")",
    re.I,
)
DO_BLOCK = re.compile(
    r"\bDO\s+(?P<tag>\$[A-Za-z0-9_]*\$)"
    r"(?P<body>.*?)"
    r"(?P=tag)\s*;",
    re.I | re.S,
)
UNSAFE_DO_BODY = (
    re.compile(
        r"ALTER\s+TABLE\s+(?:ONLY\s+)?core\.[^;]*"
        r"\bADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\b",
        re.I | re.S,
    ),
    re.compile(
        r"ALTER\s+TABLE\s+(?:ONLY\s+)?core\.[^;]*"
        r"\bALTER\s+COLUMN\b[^;]*\bSET\s+(?:DEFAULT|NOT\s+NULL)\b",
        re.I | re.S,
    ),
    re.compile(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!CONCURRENTLY\b)"
        r"[^;]*\bON\s+(?:ONLY\s+)?core\.",
        re.I | re.S,
    ),
)
LOCAL_INDEX_CLEANUP = re.compile(
    r"DROP\s+INDEX\s+CONCURRENTLY",
    re.I,
)


def _schema_files() -> list[str]:
    return [SCHEMA_INDEX] + [
        os.path.join(SCHEMA_DIR, name)
        for name in sorted(os.listdir(SCHEMA_DIR))
        if name.endswith(".sql")
    ]


def _toggle_dollar_blocks(line: str, stack: list[str]) -> None:
    for match in DOLLAR_TAG.findall(line):
        if stack and stack[-1] == match:
            stack.pop()
        else:
            stack.append(match)


def _scan_lines(lines, rel: str) -> list[str]:
    failures: list[str] = []
    stack: list[str] = []
    transaction_start: int | None = None
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        if not stack:
            if TX_BEGIN.fullmatch(stripped):
                if transaction_start is not None:
                    failures.append(
                        f"{rel}:{lineno}: nested top-level BEGIN "
                        f"(transaction began at line {transaction_start})"
                    )
                transaction_start = lineno
            elif TX_COMMIT.fullmatch(stripped):
                if transaction_start is None:
                    failures.append(
                        f"{rel}:{lineno}: top-level COMMIT without BEGIN")
                transaction_start = None
            elif (
                transaction_start is not None
                and LOCK_RETAINING_IN_TX.search(stripped)
            ):
                failures.append(
                    f"{rel}:{lineno}: relation/data lock inside publication "
                    f"transaction begun at line {transaction_start}: {stripped}"
                )
            for pattern in UNSAFE_TOP_LEVEL:
                if pattern.search(stripped):
                    failures.append(f"{rel}:{lineno}: {stripped}")
                    break
        _toggle_dollar_blocks(line, stack)
    if transaction_start is not None:
        failures.append(
            f"{rel}:{transaction_start}: top-level transaction has no COMMIT/ROLLBACK"
        )
    return failures


def _top_level_text(lines) -> str:
    """Return SQL outside dollar-quoted function/DO bodies for multiline guards."""
    visible: list[str] = []
    stack: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stack and not stripped.startswith("--"):
            visible.append(line)
        _toggle_dollar_blocks(line, stack)
    return "".join(visible)


def _top_level_concurrent_indexes(
    lines,
) -> tuple[list[tuple[str, str, bool]], list[str]]:
    """Inventory every top-level CIC independent of physical line layout."""
    indexes: list[tuple[str, str, bool]] = []
    failures: list[str] = []
    visible_sql = _top_level_text(lines)
    for start in CONCURRENT_INDEX_START.finditer(visible_sql):
        end = visible_sql.find(";", start.start())
        if end < 0:
            failures.append(
                "unterminated top-level concurrent index: "
                + visible_sql[start.start():].strip()
            )
            continue
        sql = visible_sql[start.start():end + 1]
        match = CONCURRENT_INDEX_DEF.search(sql)
        if match:
            indexes.append((
                match.group(2),
                match.group(3),
                match.group(1) is not None,
            ))
        else:
            failures.append(
                "could not parse top-level concurrent index: "
                + " ".join(sql.split())
            )
    return indexes, failures


def main() -> int:
    failures: list[str] = []
    managed_indexes: list[tuple[str, str, bool]] = []
    for path in _schema_files():
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        failures.extend(
            _scan_lines(lines, os.path.relpath(path, ROOT)))
        visible_sql = _top_level_text(lines)
        for pattern in UNSAFE_TOP_LEVEL_TEXT:
            if pattern.search(visible_sql):
                failures.append(
                    f"{os.path.relpath(path, ROOT)}: multiline unsafe "
                    f"hot-deploy DDL matched {pattern.pattern!r}"
                )
        indexes, parse_failures = _top_level_concurrent_indexes(lines)
        managed_indexes.extend(indexes)
        failures.extend(parse_failures)
        raw_sql = "".join(lines)
        if (
            os.path.normpath(path)
            != os.path.normpath(ONLINE_INDEX_REPAIR)
            and (
                LOCAL_INDEX_CLEANUP.search(raw_sql)
                or r"\gexec" in raw_sql
            )
        ):
            failures.append(
                f"{os.path.relpath(path, ROOT)}: managed invalid-index "
                "cleanup must be centralized in 05_online_index_repair.sql"
            )
        for block in DO_BLOCK.finditer(raw_sql):
            for pattern in UNSAFE_DO_BODY:
                if pattern.search(block.group("body")):
                    failures.append(
                        f"{os.path.relpath(path, ROOT)}: unsafe replayed "
                        f"table/index DDL hidden in DO body matched "
                        f"{pattern.pattern!r}"
                    )

    with open(CUTOVER, encoding="utf-8") as f:
        cutover_sql = f.read()
    helper_definitions = list(ACL_HELPER_DEFINITION.finditer(cutover_sql))
    helper_token_counts = {
        token: cutover_sql.count(token)
        for token in (
            ACL_HELPER_START,
            ACL_HELPER_OWNER,
            ACL_HELPER_REVOKE,
            ACL_HELPER_CALL,
            ACL_HELPER_DROP,
        )
    }
    if len(helper_definitions) != 1 or any(
        count != 1 for count in helper_token_counts.values()
    ):
        failures.append(
            "100_schema_cutover ACL helper must have exactly one audited "
            "SECURITY INVOKER definition and one owner/revoke/call/drop sequence"
        )
    else:
        definition = helper_definitions[0]
        owner_pos = cutover_sql.find(ACL_HELPER_OWNER, definition.end())
        revoke_pos = cutover_sql.find(ACL_HELPER_REVOKE, owner_pos)
        call_pos = cutover_sql.find(ACL_HELPER_CALL, revoke_pos)
        drop_pos = cutover_sql.find(ACL_HELPER_DROP, call_pos)
        if not (
            definition.start() < definition.end()
            <= owner_pos < revoke_pos < call_pos < drop_pos
        ):
            failures.append(
                "100_schema_cutover ACL helper publication order must be "
                "create -> owner -> revoke -> call -> drop"
            )
        else:
            audited_slice = cutover_sql[
                definition.start():drop_pos + len(ACL_HELPER_DROP)
            ]
            actual_digest = hashlib.sha256(
                audited_slice.encode("utf-8")
            ).hexdigest()
            if actual_digest != ACL_HELPER_AUDITED_SHA256:
                failures.append(
                    "100_schema_cutover ACL helper body changed outside its "
                    "audited no-relation-lock contract"
                )

    with open(ONLINE_INDEX_REPAIR, encoding="utf-8") as f:
        repair_sql = f.read()
        repair_indexes = [
            (match.group(1), match.group(2), match.group(3).lower() == "true")
            for line in repair_sql.splitlines()
            if (match := REPAIR_INDEX.fullmatch(line.rstrip("\n")))
        ]
    for unique_guard in ("AND NOT m.is_unique", "AND NOT i.indisunique"):
        if unique_guard not in repair_sql:
            failures.append(
                "invalid-index cleanup may auto-drop a UNIQUE invariant "
                f"({unique_guard!r} missing)"
            )
    duplicate_managed = sorted({
        item[0]
        for item in managed_indexes
        if sum(candidate[0] == item[0] for candidate in managed_indexes) > 1
    })
    duplicate_repair = sorted({
        item[0]
        for item in repair_indexes
        if sum(candidate[0] == item[0] for candidate in repair_indexes) > 1
    })
    if duplicate_managed:
        failures.append(
            "duplicate top-level managed index definitions: "
            + ", ".join(duplicate_managed)
        )
    if duplicate_repair:
        failures.append(
            "duplicate online-index repair registry entries: "
            + ", ".join(duplicate_repair)
        )
    missing_repair = sorted(set(managed_indexes) - set(repair_indexes))
    stale_repair = sorted(set(repair_indexes) - set(managed_indexes))
    if missing_repair:
        failures.append(
            "concurrent index contracts missing from invalid-shell repair "
            f"registry: {missing_repair!r}"
        )
    if stale_repair:
        failures.append(
            "invalid-shell repair registry contracts with no managed index: "
            f"{stale_repair!r}"
        )

    # SECURITY DEFINER deployment helpers must never be published as a
    # CREATE/OWNER/REVOKE sequence across autocommit boundaries. Default
    # privileges protect a first creation, but CREATE OR REPLACE retains a
    # historical ACL; atomic publication closes both that window and an
    # interrupted-apply window.
    with open(SUBSTRATE, encoding="utf-8") as f:
        substrate_visible = _top_level_text(f.readlines())
    first_helper = substrate_visible.find(
        "CREATE OR REPLACE FUNCTION core._ensure_column_online(")
    helper_begin = substrate_visible.rfind("BEGIN;", 0, first_helper)
    helper_commit = substrate_visible.find("COMMIT;", first_helper)
    if first_helper < 0 or helper_begin < 0 or helper_commit < 0:
        failures.append(
            "owner-only online DDL helpers lack an explicit publication transaction"
        )
    else:
        helper_block = substrate_visible[helper_begin:helper_commit + len("COMMIT;")]
        for helper in ONLINE_DDL_HELPERS:
            required = (
                f"CREATE OR REPLACE FUNCTION core.{helper}(",
                f"ALTER FUNCTION core.{helper}(",
                f"REVOKE EXECUTE ON FUNCTION core.{helper}(",
            )
            for clause in required:
                if clause not in helper_block:
                    failures.append(
                        f"owner-only online DDL helper {helper} is not "
                        f"atomically created/owned/revoked ({clause!r} missing)"
                    )

    # The current code graph does not resolve psql \ir edges or SQL helper
    # calls. Lock the load-bearing source order explicitly: repair registry,
    # helper definitions, all consumers/index builders, final verifier, ACL
    # backstop, then cutover marker.
    with open(SCHEMA_INDEX, encoding="utf-8") as f:
        schema_index_sql = f.read()
    includes = re.findall(
        r"^\s*\\ir\s+(schema/[A-Za-z0-9_.-]+\.sql)\s*$",
        schema_index_sql,
        re.M,
    )
    ordered_sentinels = (
        "schema/05_online_index_repair.sql",
        "schema/10_substrate.sql",
        "schema/98_online_index_verify.sql",
        "schema/99_least_privilege.sql",
        "schema/100_schema_cutover.sql",
    )
    try:
        sentinel_positions = [includes.index(item) for item in ordered_sentinels]
    except ValueError:
        failures.append(
            "schema.sql is missing an online-DDL ordering sentinel: "
            + ", ".join(ordered_sentinels)
        )
        sentinel_positions = []
    if sentinel_positions and sentinel_positions != sorted(sentinel_positions):
        failures.append(
            "schema.sql online-DDL order must be repair -> helpers -> "
            "verify -> ACL backstop -> cutover"
        )
    if sentinel_positions:
        repair_pos, helper_pos, verify_pos, _, cutover_pos = sentinel_positions
        if repair_pos != 0 or cutover_pos != len(includes) - 1:
            failures.append(
                "schema.sql must load online-index repair first and cutover last"
            )
        for position, include in enumerate(includes):
            module_path = os.path.join(ROOT, "db", include)
            with open(module_path, encoding="utf-8") as f:
                module_sql = f.read()
            if (
                "SELECT core._ensure_" in module_sql
                and position <= helper_pos
            ):
                failures.append(
                    f"{include}: online DDL helper called before "
                    "10_substrate publishes it"
                )
            if (
                CONCURRENT_INDEX_START.search(
                    _top_level_text(module_sql.splitlines(keepends=True))
                )
                and not (repair_pos < position < verify_pos)
            ):
                failures.append(
                    f"{include}: managed concurrent index is outside "
                    "05-repair -> 98-verify boundary"
                )

    # Regression fixture for the original blind spot: dollar-quoted bodies are
    # intentionally not parsed, so the opening DO itself must be rejected
    # while the publication transaction is active.
    do_fixture = [
        "BEGIN;\n",
        "DO $$\n",
        "BEGIN\n",
        "  ALTER TABLE core.webhook_delivery ADD COLUMN hidden_lock int;\n",
        "END $$;\n",
        "COMMIT;\n",
    ]
    if not any(
        "relation/data lock inside publication" in item
        for item in _scan_lines(do_fixture, "<do-wrapper-regression>")
    ):
        failures.append(
            "<guard-self-test>: DO-wrapped table DDL escaped the publication transaction guard"
        )
    multiline_fixtures = {
        "raw-add-column": [
            "ALTER\n", "  TABLE core.busy\n",
            "  ADD COLUMN IF NOT EXISTS replayed int;\n",
        ],
        "plain-index": [
            "CREATE\n", "  INDEX IF NOT EXISTS busy_idx\n",
            "  ON core.busy(id);\n",
        ],
        "raw-sequence-owner": [
            "ALTER\n", "  SEQUENCE core.graph_revision_seq\n",
            "  OWNER TO veripsa_migrator;\n",
        ],
    }
    for fixture_name, fixture_lines in multiline_fixtures.items():
        visible_sql = _top_level_text(fixture_lines)
        if not any(
            pattern.search(visible_sql)
            for pattern in UNSAFE_TOP_LEVEL_TEXT
        ):
            failures.append(
                f"<guard-self-test>:{fixture_name}: multiline unsafe DDL escaped"
            )
    multiline_cic = [
        "CREATE\n",
        "  INDEX CONCURRENTLY IF NOT EXISTS split_idx\n",
        "  ON core.agent(account_id);\n",
    ]
    parsed_cic, parsed_cic_failures = _top_level_concurrent_indexes(
        multiline_cic
    )
    if (
        parsed_cic != [("split_idx", "agent", False)]
        or parsed_cic_failures
    ):
        failures.append(
            "<guard-self-test>: multiline concurrent index escaped managed "
            f"inventory ({parsed_cic!r}, {parsed_cic_failures!r})"
        )
    do_body_fixture = [
        "DO $$\n",
        "BEGIN\n",
        "  ALTER TABLE core.busy\n",
        "    ADD COLUMN IF NOT EXISTS replayed int;\n",
        "END $$;\n",
    ]
    do_fixture_sql = "".join(do_body_fixture)
    if not any(
        pattern.search(match.group("body"))
        for match in DO_BLOCK.finditer(do_fixture_sql)
        for pattern in UNSAFE_DO_BODY
    ):
        failures.append(
            "<guard-self-test>: raw table DDL hidden in DO body escaped"
        )

    for item in failures:
        print(f"  [FAIL] unsafe hot-deploy DDL: {item}")
    ok = not failures
    print("SCHEMA HOT-DEPLOY GUARD:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
