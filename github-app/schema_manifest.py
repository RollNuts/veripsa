"""Content-free schema generation manifest for the Render pre-deploy gate.

The live marker is stored as ``COMMENT ON SCHEMA core`` rather than in a
table.  It contains only a format version, monotonically increasing schema
generation, SHA-256 digest, and whether generation 1 was adopted or applied.
No tenant row or database content participates in the digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

try:
    from schema_contract import check_schema_contract
except ImportError:  # pragma: no cover - package import path
    from .schema_contract import check_schema_contract


MARKER_PREFIX = "veripsa-schema/v1"
MARKER_RE = re.compile(
    r"^veripsa-schema/v1/(0|[1-9][0-9]*)/([0-9a-f]{64})/(adopted|applied)$"
)
LOCK_NAMESPACE = "veripsa.schema_manifest"
LOCK_RESOURCE = "core"
STRANDED_LEGACY_CLAIM_MAX_GENERATION = 12
CLAIM_V2_PREDECESSOR_BODY_MD5 = "8bdf79d0259b27c37f48a9f52c0416b6"
CLAIM_V2_SAFE_BODY_MD5 = "877a1f799a016bcd42a7a57b61750c25"
CLAIM_V2_ARG_NAMES = (
    "p_key",
    "p_stale_seconds",
    "p_max_attempts",
    "p_protocol",
)


@dataclass(frozen=True)
class SchemaManifest:
    generation: int
    digest: str
    files: tuple[tuple[str, int, str], ...]

    def marker(self, disposition: Literal["adopted", "applied"]) -> str:
        return f"{MARKER_PREFIX}/{self.generation}/{self.digest}/{disposition}"


class LiveMarker(NamedTuple):
    generation: int
    digest: str
    disposition: str


class _ClaimV2Catalog(NamedTuple):
    body_md5: str
    language: str
    security_definer: bool
    config: list[str] | None
    owner: str
    returns_jsonb: bool
    returns_set: bool
    default_count: int
    arg_names: list[str] | None
    arg_modes: list[str] | None
    all_arg_types: list[int] | None
    strict: bool
    volatility: str
    parallel: str
    leakproof: bool
    kind: str
    not_variadic: bool
    cost: float
    rows: float
    no_support_function: bool
    binary_is_null: bool


ClaimV2State = Literal[
    "missing",
    "exact_public_predecessor",
    "exact_current_safe",
    "unknown",
]
ClaimV2Rejection = Literal["known_partial", "unknown"]


Decision = Literal[
    "apply_fresh",
    "adopt_existing",
    "skip_current",
    "apply_upgrade",
    "skip_newer_live",
    "fail_unmarked",
    "fail_malformed",
    "fail_same_generation_digest",
]


def _generation(root: Path) -> int:
    raw = (root / "db" / "schema_generation").read_text(encoding="ascii")
    if not re.fullmatch(r"[1-9][0-9]*\n?", raw):
        raise ValueError("db/schema_generation must contain one positive decimal integer")
    return int(raw.strip())


def _schema_paths(root: Path) -> tuple[Path, ...]:
    """Return the authoritative schema index followed by its ordered includes."""
    index = root / "db" / "schema.sql"
    includes: list[Path] = []
    seen: set[str] = set()
    for line in index.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith(r"\ir"):
            continue
        parts = stripped.split()
        if len(parts) != 2:
            raise ValueError("schema.sql contains a malformed include")
        rel = parts[1]
        if not re.fullmatch(r"schema/[A-Za-z0-9_.-]+\.sql", rel):
            raise ValueError("schema.sql contains an unsafe include path")
        if rel in seen:
            raise ValueError("schema.sql contains a duplicate include")
        seen.add(rel)
        path = root / "db" / rel
        if not path.is_file():
            raise ValueError("schema.sql references a missing include")
        includes.append(path)
    if not includes:
        raise ValueError("schema.sql contains no schema modules")
    return (index, *includes)


def build_manifest(root: Path) -> SchemaManifest:
    """Hash a canonical, path-aware manifest of the checked-in schema files."""
    root = root.resolve()
    generation = _generation(root)
    files: list[tuple[str, int, str]] = []
    for path in _schema_paths(root):
        body = path.read_bytes()
        rel = path.relative_to(root).as_posix()
        files.append((rel, len(body), hashlib.sha256(body).hexdigest()))
    canonical = json.dumps(
        {
            "files": [
                {"bytes": size, "path": path, "sha256": digest}
                for path, size, digest in files
            ],
            "format": 1,
            "generation": generation,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return SchemaManifest(
        generation=generation,
        digest=hashlib.sha256(canonical).hexdigest(),
        files=tuple(files),
    )


def parse_marker(comment: str | None) -> LiveMarker | None:
    if comment is None or comment == "":
        return None
    match = MARKER_RE.fullmatch(comment)
    if match is None:
        raise ValueError("malformed schema manifest marker")
    return LiveMarker(int(match.group(1)), match.group(2), match.group(3))


def decide(*, core_exists: bool, comment: str | None, manifest: SchemaManifest) -> Decision:
    """Pure state transition; callers must fail closed for every ``fail_*`` result."""
    if not core_exists:
        return "apply_fresh"
    try:
        live = parse_marker(comment)
    except ValueError:
        return "fail_malformed"
    if live is None:
        # An unmarked existing core schema. At generation 1 this is the ONE-TIME bootstrap of the
        # pre-marker production DB: adopt it (contract-gated, DDL-skipped) so the known-good original is
        # not needlessly re-applied. At generation > 1 an unmarked DB is a DR restore or a legacy DB that
        # is BEHIND the current generation; blocking it (the former fail_unmarked) would strand disaster
        # recovery. Apply the full schema.sql IDEMPOTENTLY to bring it current, then stamp — strictly safer
        # than either adopting (skip-DDL, could leave drift) or failing closed (blocks DR).
        return "adopt_existing" if manifest.generation == 1 else "apply_upgrade"
    if live.generation == manifest.generation:
        if live.digest != manifest.digest:
            return "fail_same_generation_digest"
        return "skip_current"
    if live.generation < manifest.generation:
        return "apply_upgrade"
    return "skip_newer_live"


def _read_live_state(cursor) -> tuple[bool, str | None]:
    cursor.execute(
        "SELECT n.oid, obj_description(n.oid, 'pg_namespace') "
        "FROM pg_namespace n WHERE n.nspname = 'core'"
    )
    row = cursor.fetchone()
    if row is None:
        return False, None
    return True, row[1]


def _classify_claim_v2_catalog(
    row: _ClaimV2Catalog | tuple | None,
) -> ClaimV2State:
    """Classify the complete callable /4 catalog contract before any DDL.

    A body hash alone is not compatibility proof: SETOF, STRICT, volatility,
    parallel-safety, argument modes, or a support function can change the
    runtime contract while leaving ``prosrc`` byte-identical. Only the public
    synthetic predecessor and the exact current fail-closed wrapper are known.
    """
    if row is None:
        return "missing"
    catalog = row if isinstance(row, _ClaimV2Catalog) else _ClaimV2Catalog(*row)
    common_exact = (
        catalog.security_definer is True
        and catalog.config == ["search_path=core, pg_catalog"]
        and catalog.owner == "veripsa_migrator"
        and catalog.returns_jsonb is True
        and catalog.returns_set is False
        and catalog.default_count == 0
        and tuple(catalog.arg_names or ()) == CLAIM_V2_ARG_NAMES
        and catalog.arg_modes is None
        and catalog.all_arg_types is None
        and catalog.strict is False
        and catalog.volatility == "v"
        and catalog.parallel == "u"
        and catalog.leakproof is False
        and catalog.kind == "f"
        and catalog.not_variadic is True
        and catalog.cost == 100.0
        and catalog.rows == 0.0
        and catalog.no_support_function is True
        and catalog.binary_is_null is True
    )
    if not common_exact:
        return "unknown"
    if (
        catalog.body_md5 == CLAIM_V2_PREDECESSOR_BODY_MD5
        and catalog.language == "plpgsql"
    ):
        return "exact_public_predecessor"
    if (
        catalog.body_md5 == CLAIM_V2_SAFE_BODY_MD5
        and catalog.language == "sql"
    ):
        return "exact_current_safe"
    return "unknown"


def _claim_v2_catalog_state(cursor) -> ClaimV2State:
    """Read and classify the exact legacy claim overload under the manifest lock."""
    cursor.execute(
        "SELECT md5(p.prosrc),l.lanname,p.prosecdef,p.proconfig,r.rolname,"
        "p.prorettype='jsonb'::regtype,p.proretset,p.pronargdefaults,"
        "p.proargnames,p.proargmodes,p.proallargtypes,p.proisstrict,"
        "p.provolatile,p.proparallel,p.proleakproof,p.prokind,"
        "p.provariadic=0,p.procost,p.prorows,p.prosupport=0,p.probin IS NULL "
        "FROM pg_proc p "
        "JOIN pg_language l ON l.oid=p.prolang "
        "JOIN pg_roles r ON r.oid=p.proowner "
        "WHERE p.oid=to_regprocedure("
        "'core.claim_webhook_delivery_with_authority(text,integer,integer,integer)')"
    )
    return _classify_claim_v2_catalog(cursor.fetchone())


def _claim_v2_preflight_rejection(
    state: ClaimV2State,
    comment: str | None,
) -> ClaimV2Rejection | None:
    """Reject unsafe upgrade states before psql can mutate the serving ABI."""
    try:
        live = parse_marker(comment)
    except ValueError:
        return "unknown"
    if (
        state == "exact_current_safe"
        and live is not None
        and live.generation <= STRANDED_LEGACY_CLAIM_MAX_GENERATION
    ):
        return "known_partial"
    if state == "unknown":
        return "unknown"
    return None


def _adoption_contract_passes() -> bool:
    app_dsn = os.environ.get("VERIPSA_DSN", "")
    if not app_dsn:
        print(
            "[predeploy_schema] FATAL: generation-1 adoption requires VERIPSA_DSN "
            "for the read-only runtime schema contract.",
            flush=True,
        )
        return False
    result = check_schema_contract(app_dsn)
    if result.skipped or not result.healthy:
        kinds = sorted({violation.kind for violation in result.violations})
        print(
            "[predeploy_schema] FATAL: unmarked existing schema was not adopted; "
            f"runtime contract checked={result.checked} skipped={result.skipped} "
            f"violation_kinds={','.join(kinds) or 'none'}.",
            flush=True,
        )
        return False
    print(
        f"[predeploy_schema] generation-1 adoption contract passed ({result.checked} metadata checks).",
        flush=True,
    )
    return True


def _apply_schema(root: Path, owner_dsn: str, applied_marker: str) -> int:
    psql = shutil.which("psql")
    if psql is None:
        print(
            "[predeploy_schema] FATAL: psql not found in image — install postgresql-client.",
            flush=True,
        )
        return 2
    print(
        "[predeploy_schema] applying db/schema.sql via psql -v ON_ERROR_STOP=1 "
        "under the schema-manifest lock.",
        flush=True,
    )
    completed = subprocess.run(
        [
            psql,
            owner_dsn,
            "-X",
            "-v",
            f"veripsa_schema_marker={applied_marker}",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "schema.sql",
        ],
        cwd=root / "db",
        check=False,
    )
    if completed.returncode != 0:
        print(
            f"[predeploy_schema] FATAL: psql exited non-zero ({completed.returncode}) — "
            "schema apply FAILED; live marker not advanced.",
            flush=True,
        )
    return completed.returncode


def run(root: Path, owner_dsn: str) -> int:
    try:
        manifest = build_manifest(root)
    except (OSError, ValueError) as exc:
        print(
            f"[predeploy_schema] FATAL: invalid checked-in schema manifest ({type(exc).__name__}); "
            "no database action taken.",
            flush=True,
        )
        return 2

    print(
        f"[predeploy_schema] image schema generation={manifest.generation} "
        f"digest={manifest.digest} files={len(manifest.files)}.",
        flush=True,
    )

    import psycopg2

    conn = None
    try:
        conn = psycopg2.connect(owner_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            # Session lock spans inspect -> optional psql apply -> stamp. A second deploy
            # re-reads the marker only after the first has finished, so each generation
            # applies at most once even when Render starts duplicate pre-deploy jobs.
            cur.execute(
                "SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))",
                (LOCK_NAMESPACE, LOCK_RESOURCE),
            )
            core_exists, comment = _read_live_state(cur)
            action = decide(core_exists=core_exists, comment=comment, manifest=manifest)

            if action == "skip_current":
                print("[predeploy_schema] schema manifest already current; DDL skipped.", flush=True)
                return 0
            if action == "skip_newer_live":
                print(
                    "[predeploy_schema] live schema generation is newer than this rollback image; "
                    "DDL and marker write skipped.",
                    flush=True,
                )
                return 0
            if action.startswith("fail_"):
                print(
                    f"[predeploy_schema] FATAL: schema manifest state rejected ({action}); "
                    "DDL and marker write skipped.",
                    flush=True,
                )
                return 3
            if action == "adopt_existing":
                if not _adoption_contract_passes():
                    return 3
                marker = manifest.marker("adopted")
                cur.execute("COMMENT ON SCHEMA core IS %s", (marker,))
                print(
                    "[predeploy_schema] existing schema adopted at generation 1; full-schema DDL skipped.",
                    flush=True,
                )
                return 0

            if action == "apply_upgrade":
                claim_v2_state = _claim_v2_catalog_state(cur)
                claim_v2_rejection = _claim_v2_preflight_rejection(
                    claim_v2_state, comment
                )
                if claim_v2_rejection == "known_partial":
                    print(
                        "[predeploy_schema] FATAL: known partial schema publication detected "
                        "(legacy claim ABI is already fenced while the live marker is old); "
                        "DDL and marker write skipped.",
                        flush=True,
                    )
                    return 3
                if claim_v2_rejection == "unknown":
                    print(
                        "[predeploy_schema] FATAL: unknown legacy claim ABI fingerprint; "
                        "DDL and marker write skipped to preserve the serving predecessor.",
                        flush=True,
                    )
                    return 3

            expected_marker = manifest.marker("applied")
            rc = _apply_schema(root, owner_dsn, expected_marker)
            if rc != 0:
                return rc
            # The final schema module publishes this exact marker only after
            # every expansion succeeds. Existing legacy claim ABIs deliberately
            # remain compatible in this generation until the /6 runtime is
            # promoted; fencing them in pre-deploy would not be atomic with
            # Render image promotion.
            core_after, comment_after = _read_live_state(cur)
            if not core_after:
                print(
                    "[predeploy_schema] FATAL: schema apply returned success but core schema is absent; "
                    "live marker not advanced.",
                    flush=True,
                )
                return 3
            if comment_after != expected_marker:
                print(
                    "[predeploy_schema] FATAL: schema apply returned success without the exact "
                    "atomic schema-cutover marker; deploy rejected.",
                    flush=True,
                )
                return 3
            print(
                f"[predeploy_schema] schema apply OK; generation {manifest.generation} marker stamped.",
                flush=True,
            )
            return 0
    except Exception as exc:
        print(
            f"[predeploy_schema] FATAL: schema manifest operation failed ({type(exc).__name__}); "
            "live marker not advanced.",
            flush=True,
        )
        return 3
    finally:
        if conn is not None:
            try:
                conn.close()  # releases the session advisory lock
            except Exception:
                pass


def main() -> int:
    root = Path(os.environ.get("VERIPSA_REPO_ROOT", Path(__file__).resolve().parents[1]))
    owner_dsn = os.environ.get("OWNER_DSN", "")
    if not owner_dsn:
        # The shell wrapper preserves the historical missing-secret behavior. Keep
        # the helper safe when invoked directly as well.
        print("[predeploy_schema] OWNER_DSN missing — skipping schema apply.", flush=True)
        return 0
    return run(root, owner_dsn)


if __name__ == "__main__":
    raise SystemExit(main())
