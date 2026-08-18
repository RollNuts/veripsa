#!/usr/bin/env python3
"""Pure gate for the content-free schema generation manifest."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "github-app"))

from schema_manifest import (  # noqa: E402
    CLAIM_V2_PREDECESSOR_BODY_MD5,
    CLAIM_V2_SAFE_BODY_MD5,
    SchemaManifest,
    _ClaimV2Catalog,
    _claim_v2_catalog_state,
    _claim_v2_preflight_rejection,
    _classify_claim_v2_catalog,
    build_manifest,
    decide,
    parse_marker,
)


def _manifest(generation: int = 1, digest: str = "a" * 64) -> SchemaManifest:
    return SchemaManifest(generation=generation, digest=digest, files=(("db/schema.sql", 1, "b" * 64),))


def _fixture() -> Path:
    root = Path(tempfile.mkdtemp(prefix="veripsa-schema-manifest-"))
    (root / "db" / "schema").mkdir(parents=True)
    (root / "db" / "schema_generation").write_text("1\n", encoding="ascii")
    (root / "db" / "schema.sql").write_text(
        "-- index\n\\ir schema/10_a.sql\n\\ir schema/20_b.sql\n",
        encoding="utf-8",
    )
    (root / "db" / "schema" / "10_a.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (root / "db" / "schema" / "20_b.sql").write_text("SELECT 2;\n", encoding="utf-8")
    return root


def _claim_v2_catalog(*, safe: bool = False) -> _ClaimV2Catalog:
    return _ClaimV2Catalog(
        body_md5=(
            CLAIM_V2_SAFE_BODY_MD5
            if safe
            else CLAIM_V2_PREDECESSOR_BODY_MD5
        ),
        language="sql" if safe else "plpgsql",
        security_definer=True,
        config=["search_path=core, pg_catalog"],
        owner="veripsa_migrator",
        returns_jsonb=True,
        returns_set=False,
        default_count=0,
        arg_names=[
            "p_key",
            "p_stale_seconds",
            "p_max_attempts",
            "p_protocol",
        ],
        arg_modes=None,
        all_arg_types=None,
        strict=False,
        volatility="v",
        parallel="u",
        leakproof=False,
        kind="f",
        not_variadic=True,
        cost=100.0,
        rows=0.0,
        no_support_function=True,
        binary_is_null=True,
    )


class _CatalogCursor:
    def __init__(self, row):
        self.row = row
        self.sql = ""

    def execute(self, sql):
        self.sql = sql

    def fetchone(self):
        return self.row


def main() -> int:
    checks: list[tuple[str, bool]] = []
    fixture = _fixture()
    try:
        one = build_manifest(fixture)
        two = build_manifest(fixture)
        checks.append(("manifest digest is deterministic", one == two))
        checks.append((
            "manifest is path-aware and carries only path/byte-count/digest metadata",
            one.generation == 1
            and len(one.files) == 3
            and [item[0] for item in one.files]
            == ["db/schema.sql", "db/schema/10_a.sql", "db/schema/20_b.sql"]
            and all(isinstance(size, int) and len(digest) == 64 for _, size, digest in one.files),
        ))
        original = one.digest
        (fixture / "db" / "schema" / "20_b.sql").write_text("SELECT 3;\n", encoding="utf-8")
        checks.append(("schema byte change changes the digest", build_manifest(fixture).digest != original))
        (fixture / "db" / "schema" / "20_b.sql").write_text("SELECT 2;\n", encoding="utf-8")
        (fixture / "db" / "schema_generation").write_text("2\n", encoding="ascii")
        checks.append(("generation change changes the digest", build_manifest(fixture).digest != original))

        marker = _manifest().marker("applied")
        parsed = parse_marker(marker)
        checks.append((
            "marker round-trips exact generation/digest/disposition",
            parsed is not None
            and parsed.generation == 1
            and parsed.digest == "a" * 64
            and parsed.disposition == "applied",
        ))
        checks.append(("fresh database applies", decide(core_exists=False, comment=None, manifest=_manifest()) == "apply_fresh"))
        checks.append(("generation 1 may adopt an unmarked existing database", decide(core_exists=True, comment=None, manifest=_manifest(1)) == "adopt_existing"))
        checks.append(("generation greater than 1 applies the current schema to an unmarked DB (DR/legacy bring-current, not fail_unmarked)", decide(core_exists=True, comment=None, manifest=_manifest(2)) == "apply_upgrade"))
        checks.append(("same generation and digest skips", decide(core_exists=True, comment=marker, manifest=_manifest()) == "skip_current"))
        checks.append(("same generation with another digest fails closed", decide(core_exists=True, comment=marker, manifest=_manifest(1, "c" * 64)) == "fail_same_generation_digest"))
        checks.append(("older live generation upgrades", decide(core_exists=True, comment=_manifest(0).marker("applied"), manifest=_manifest(1)) == "apply_upgrade"))
        checks.append(("newer live generation makes a rollback skip", decide(core_exists=True, comment=_manifest(2).marker("applied"), manifest=_manifest(1)) == "skip_newer_live"))
        malformed = (
            "unrelated comment",
            "veripsa-schema/v1/1/short/applied",
            "veripsa-schema/v2/1/" + "a" * 64 + "/applied",
            "veripsa-schema/v1/1/" + "a" * 64 + "/unknown",
        )
        checks.append((
            "malformed and foreign comments fail closed",
            all(decide(core_exists=True, comment=value, manifest=_manifest()) == "fail_malformed" for value in malformed),
        ))

        operational_catalog = _claim_v2_catalog()
        safe_catalog = _claim_v2_catalog(safe=True)
        checks.append((
            "legacy /4 catalog classifier admits only the exact production and current-safe definitions",
            CLAIM_V2_PREDECESSOR_BODY_MD5
            == "8bdf79d0259b27c37f48a9f52c0416b6"
            and CLAIM_V2_SAFE_BODY_MD5
            == "877a1f799a016bcd42a7a57b61750c25"
            and _classify_claim_v2_catalog(None) == "missing"
            and _classify_claim_v2_catalog(operational_catalog)
            == "exact_public_predecessor"
            and _classify_claim_v2_catalog(safe_catalog)
            == "exact_current_safe",
        ))
        metadata_mutations = (
            {"body_md5": "0" * 32},
            {"language": "sql"},
            {"security_definer": False},
            {"config": ["search_path=pg_catalog, core"]},
            {"owner": "veripsa_app"},
            {"returns_jsonb": False},
            {"returns_set": True},
            {"default_count": 1},
            {"arg_names": ["p_key", "p_stale_seconds", "p_max_attempts", "other"]},
            {"arg_modes": ["i", "i", "i", "i"]},
            {"all_arg_types": [25, 23, 23, 23]},
            {"strict": True},
            {"volatility": "s"},
            {"parallel": "s"},
            {"leakproof": True},
            {"kind": "p"},
            {"not_variadic": False},
            {"cost": 99.0},
            {"rows": 1.0},
            {"no_support_function": False},
            {"binary_is_null": False},
        )
        checks.append((
            "every catalog ABI/security/execution metadata drift is unknown",
            all(
                _classify_claim_v2_catalog(
                    operational_catalog._replace(**mutation)
                )
                == "unknown"
                for mutation in metadata_mutations
            ),
        ))
        missing_cursor = _CatalogCursor(None)
        exact_cursor = _CatalogCursor(tuple(operational_catalog))
        checks.append((
            "catalog read uses the exact /4 signature and preserves missing/exact classification",
            _claim_v2_catalog_state(missing_cursor) == "missing"
            and _claim_v2_catalog_state(exact_cursor)
            == "exact_public_predecessor"
            and "to_regprocedure(" in exact_cursor.sql
            and "p.proretset" in exact_cursor.sql
            and "p.proargmodes" in exact_cursor.sql
            and "p.proallargtypes" in exact_cursor.sql,
        ))
        old_marker = _manifest(12).marker("applied")
        later_marker = _manifest(13).marker("applied")
        checks.append((
            "apply preflight rejects old-marker safe residue and every unknown fingerprint",
            _claim_v2_preflight_rejection(
                "exact_current_safe", old_marker
            )
            == "known_partial"
            and _claim_v2_preflight_rejection("unknown", old_marker)
            == "unknown"
            and _claim_v2_preflight_rejection("unknown", None)
            == "unknown",
        ))
        checks.append((
            "apply preflight permits missing, exact prod, and non-stranded current-safe states",
            _claim_v2_preflight_rejection("missing", old_marker) is None
            and _claim_v2_preflight_rejection(
                "exact_public_predecessor", old_marker
            )
            is None
            and _claim_v2_preflight_rejection(
                "exact_current_safe", later_marker
            )
            is None
            and _claim_v2_preflight_rejection(
                "exact_current_safe", None
            )
            is None,
        ))

        unsafe = _fixture()
        try:
            (unsafe / "db" / "schema.sql").write_text("\\ir ../escape.sql\n", encoding="utf-8")
            try:
                build_manifest(unsafe)
                rejected = False
            except ValueError:
                rejected = True
            checks.append(("unsafe include path is rejected", rejected))
        finally:
            shutil.rmtree(unsafe)

        sentinel = "customer-secret-default-expression"
        output_shape = marker + repr(one.files)
        checks.append(("manifest output contains no arbitrary content sentinel", sentinel not in output_shape))
    finally:
        shutil.rmtree(fixture)

    ok = True
    for name, condition in checks:
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
        ok = ok and condition
    print("SCHEMA GENERATION MANIFEST GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
