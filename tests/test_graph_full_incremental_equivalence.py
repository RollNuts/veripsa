#!/usr/bin/env python3
"""Full-build vs resource-aware incremental graph equivalence.

This is the end-to-end correctness gate for the graph which drives Veripsa's
review decisions.  Every case is written to PostgreSQL twice at the same end
commit:

* ``patch`` starts from the case's baseline graph and is updated through the
  real ``github-app/ingest.py::_reingest_graph`` decision path.
* ``truth`` is a full extraction of the complete end-state repository.

The persisted Node/Edge sets must be byte-for-byte equivalent after removing
only coordinate fields (account/repo/branch), and the authoritative
``graph_version.graph_hash`` values must match.  DB ids, row order and
timestamps are therefore outside the comparison by construction.

Run: ``python3 tests/test_graph_full_incremental_equivalence.py``
Requires the same local PostgreSQL roles as the other DB-backed gates.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import dataclass
import io
import json
import os
import subprocess
import sys
import tarfile
import traceback
from typing import Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import psycopg2  # noqa: E402
import code_graph_extract as X  # noqa: E402
import cg_schema_contract as C  # noqa: E402
import ingest as I  # noqa: E402
import _cg_resolve as R  # noqa: E402


DB = "veripsa_graph_equiv_" + str(os.getpid())
BRANCH = "main"
BASE_SHA = "1" * 40
END_SHA = "2" * 40
BASE_TIME = "2026-07-27T00:00:00+00:00"
END_TIME = "2026-07-27T00:01:00+00:00"


@dataclass(frozen=True)
class Case:
    name: str
    base: dict[str, str]
    end: dict[str, str]
    changed: tuple[str, ...]
    expected_edge: tuple[str, str, str] | None
    # ``patch`` is required for path-local consumer changes.  ``full`` is
    # required where a changed definition or symmetric/bidirectional pairing
    # can alter edges owned by unchanged files.
    expected_mode: str
    resource_consumer_patch: bool = False
    expected_absent_edge: tuple[str, str, str] | None = None
    expected_absent_paths: tuple[str, ...] = ()
    expected_present_paths: tuple[str, ...] = ()
    expected_baseline_absent_paths: tuple[str, ...] = ()
    expected_input_file_count: int | None = None
    expected_resolution_context_file_count: int | None = None
    expected_fallback_fragment: str | None = None
    expected_fallback_code: str | None = None
    end_symlinks: tuple[tuple[str, str], ...] = ()
    expected_nonregular_absent_paths: tuple[str, ...] = ()
    baseline_extractor_version: str | None = None
    baseline_semantic_ref_version: int | None = None
    baseline_drop_paths: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    expected_end_absent_paths: tuple[str, ...] = ()
    expected_baseline_present_paths: tuple[str, ...] = ()
    expected_ambiguous_edges: tuple[tuple[str, str, str], ...] = ()
    expected_unresolved_edges: tuple[tuple[str, str, str], ...] = ()
    expected_ambiguous_paths: tuple[str, ...] = ()
    expected_incomplete_paths: tuple[str, ...] = ()
    failed_extractor_path: str | None = None
    baseline_failed_extractor_path: str | None = None
    incomplete_extractor_path: str | None = None
    baseline_incomplete_extractor_path: str | None = None
    expected_baseline_uncertainty: bool | None = None
    expected_final_uncertainty: bool | None = None


def _cases() -> tuple[Case, ...]:
    db_schema = (
        "CREATE TABLE orders (id integer, total integer);\n"
        "CREATE TABLE users (id integer, email text);\n"
    )
    db_base = 'result = db.execute("SELECT id, total FROM orders")\n'
    db_end = 'result = db.execute("SELECT id, email FROM users")\n'
    import_ambiguous_targets = {
        "a/widget.py": "VALUE = 'a'\n",
        "b/widget.py": "VALUE = 'b'\n",
    }
    import_overcap_targets = {
        f"pkg{i}/helper.py": f"VALUE = {i}\n"
        for i in range(R._MAX_BARE_FANOUT + 1)
    }
    ruby_overcap_targets = {
        f"ruby_pkg{i}/helper.rb": f"VALUE = {i}\n"
        for i in range(R._MAX_BARE_FANOUT + 1)
    }

    openapi = (
        "openapi: 3.0.0\n"
        "info:\n  title: Equivalence\n  version: '1'\n"
        "paths:\n"
        "  /users/{id}:\n"
        "    get:\n"
        "      operationId: getUser\n"
        "      responses:\n        '200':\n          description: ok\n"
        "  /invoices/{id}:\n"
        "    get:\n"
        "      operationId: getInvoice\n"
        "      responses:\n        '200':\n          description: ok\n"
        "components:\n"
        "  schemas:\n"
        "    User:\n      type: object\n"
        "    Invoice:\n      type: object\n"
    )

    k8s_old = (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n  name: api\n  namespace: prod\n"
        "spec:\n"
        "  selector:\n    matchLabels:\n      app: api\n"
        "  template:\n"
        "    metadata:\n      labels:\n        app: api\n"
        "    spec:\n"
        "      containers:\n"
        "      - name: api\n"
        "        image: example/api:1\n"
        "        envFrom:\n"
        "        - configMapRef:\n            name: old-config\n"
    )
    k8s_new = k8s_old.replace("old-config", "new-config")
    graphql_truncated_schema = (
        (("x" * 200_000) + "\n") * 5
        + "type Invoice { id: ID! }\n"
    )

    return (
        Case(
            "imports",
            {
                "lib/alpha.py": "VALUE = 1\n",
                "lib/beta.py": "VALUE = 2\n",
                "consumer.py": "from lib import alpha\nvalue = alpha.VALUE\n",
            },
            {
                "lib/alpha.py": "VALUE = 1\n",
                "lib/beta.py": "VALUE = 2\n",
                "consumer.py": "from lib import beta\nvalue = beta.VALUE\n",
            },
            ("consumer.py",),
            ("consumer.py", "lib/beta.py", "imports"),
            "patch",
        ),
        Case(
            "imports_relative_unresolved",
            {
                "pkg/consumer.py": "value = 'baseline'\n",
            },
            {
                "pkg/consumer.py": (
                    "from .missing import x\n"
                    "value = x\n"
                ),
            },
            ("pkg/consumer.py",),
            ("pkg/consumer.py", "./missing", "imports"),
            "patch",
            expected_unresolved_edges=(
                ("pkg/consumer.py", "./missing", "imports"),
            ),
            expected_baseline_uncertainty=False,
            expected_final_uncertainty=True,
        ),
        Case(
            "imports_relative_unresolved_recovered",
            {
                "pkg/consumer.py": (
                    "from .missing import x\n"
                    "value = x\n"
                ),
            },
            {
                "pkg/consumer.py": (
                    "from .missing import x\n"
                    "value = x\n"
                ),
                "pkg/missing.py": "x = 1\n",
            },
            ("pkg/missing.py",),
            ("pkg/consumer.py", "pkg/missing.py", "imports"),
            "full",
            expected_baseline_absent_paths=("pkg/missing.py",),
            expected_fallback_fragment="stored graph contains unresolved",
            expected_fallback_code="stored_graph_uncertainty",
            expected_baseline_uncertainty=True,
            expected_final_uncertainty=False,
        ),
        Case(
            "imports_relative_mixed_statement_families",
            {
                "pkg/foo/bar.py": "thing = 1\n",
                "pkg/consumer.py": "value = 'baseline'\n",
            },
            {
                "pkg/foo/bar.py": "thing = 1\n",
                "pkg/consumer.py": (
                    "from .foo import missing\n"
                    "from .foo.bar import thing\n"
                    "value = thing\n"
                ),
            },
            ("pkg/consumer.py",),
            ("pkg/consumer.py", "pkg/foo/bar.py", "imports"),
            "patch",
            expected_unresolved_edges=(
                ("pkg/consumer.py", "./foo/missing", "imports"),
            ),
            expected_baseline_uncertainty=False,
            expected_final_uncertainty=True,
        ),
        Case(
            "imports_relative_cross_language_stem",
            {
                "src/foo.py": "VALUE = 'python'\n",
                "src/foo.ts": "export const value = 'typescript';\n",
                "src/consumer.ts": "export const baseline = true;\n",
            },
            {
                "src/foo.py": "VALUE = 'python'\n",
                "src/foo.ts": "export const value = 'typescript';\n",
                "src/consumer.ts": (
                    "import { value } from './foo';\n"
                    "export const result = value;\n"
                ),
            },
            ("src/consumer.ts",),
            ("src/consumer.ts", "src/foo.ts", "imports"),
            "patch",
            expected_baseline_uncertainty=False,
            expected_final_uncertainty=False,
        ),
        Case(
            "imports_own_package_cross_language_entry",
            {
                "package.json": (
                    '{"name":"acme","version":"1.0.0"}\n'
                ),
                "src/index.py": "VALUE = 'python decoy'\n",
                "src/index.ts": (
                    "export default 'typescript';\n"
                ),
                "src/sub.ts": "export const sub = 'typescript';\n",
                "src/consumer.ts": "export const baseline = true;\n",
            },
            {
                "package.json": (
                    '{"name":"acme","version":"1.0.0"}\n'
                ),
                "src/index.py": "VALUE = 'python decoy'\n",
                "src/index.ts": (
                    "export default 'typescript';\n"
                ),
                "src/sub.ts": "export const sub = 'typescript';\n",
                "src/consumer.ts": (
                    "import acme from 'acme';\n"
                    "import { sub } from 'acme/sub';\n"
                    "export const result = [acme, sub];\n"
                ),
            },
            ("src/consumer.ts",),
            ("src/consumer.ts", "src/index.ts", "imports"),
            "patch",
            expected_baseline_uncertainty=False,
            expected_final_uncertainty=False,
        ),
        Case(
            "imports_ambiguous_fanout",
            {
                **import_ambiguous_targets,
                "consumer.py": (
                    "value = 'baseline without a local import'\n"
                ),
            },
            {
                **import_ambiguous_targets,
                "consumer.py": (
                    "import widget\n"
                    "value = (widget.VALUE, 'changed')\n"
                ),
            },
            ("consumer.py",),
            ("consumer.py", "a/widget.py", "imports"),
            "full",
            expected_fallback_fragment="ambiguous reference evidence",
            expected_fallback_code="ambiguous_reference_detected",
            expected_ambiguous_edges=(
                ("consumer.py", "a/widget.py", "imports"),
                ("consumer.py", "b/widget.py", "imports"),
            ),
            expected_ambiguous_paths=(
                "consumer.py",
                "a/widget.py",
                "b/widget.py",
            ),
            expected_baseline_uncertainty=False,
            expected_final_uncertainty=True,
        ),
        Case(
            "imports_ambiguous_overcap",
            {
                **import_overcap_targets,
                "consumer.py": (
                    "value = 'baseline without a local import'\n"
                ),
            },
            {
                **import_overcap_targets,
                "consumer.py": (
                    "import helper\n"
                    "value = (helper.VALUE, 'changed')\n"
                ),
            },
            ("consumer.py",),
            ("consumer.py", "helper", "imports"),
            "full",
            expected_fallback_fragment="ambiguous reference evidence",
            expected_fallback_code="ambiguous_reference_detected",
            expected_ambiguous_edges=(
                ("consumer.py", "helper", "imports"),
            ),
            expected_ambiguous_paths=(
                "consumer.py",
                *tuple(sorted(import_overcap_targets)),
            ),
            expected_baseline_uncertainty=False,
            expected_final_uncertainty=True,
        ),
        Case(
            "imports_ruby_ambiguous_overcap",
            {
                **ruby_overcap_targets,
                "consumer.rb": "value = 'baseline without require'\n",
            },
            {
                **ruby_overcap_targets,
                "consumer.rb": (
                    "require 'helper'\n"
                    "value = VALUE\n"
                ),
            },
            ("consumer.rb",),
            ("consumer.rb", "helper", "imports"),
            "full",
            expected_fallback_fragment="ambiguous reference evidence",
            expected_fallback_code="ambiguous_reference_detected",
            expected_ambiguous_edges=(
                ("consumer.rb", "helper", "imports"),
            ),
            expected_ambiguous_paths=(
                "consumer.rb",
                *tuple(sorted(ruby_overcap_targets)),
            ),
            expected_baseline_uncertainty=False,
            expected_final_uncertainty=True,
        ),
        Case(
            "csharp_namespace_competing_projects",
            {
                "ProjA/App/Services/A.cs": (
                    "namespace App.Services { public class A {} }\n"
                ),
                "ProjB/App/Services/B.cs": (
                    "namespace App.Services { public class B {} }\n"
                ),
                "Web/Home.cs": (
                    "namespace Web { public class Home {} }\n"
                ),
            },
            {
                "ProjA/App/Services/A.cs": (
                    "namespace App.Services { public class A {} }\n"
                ),
                "ProjB/App/Services/B.cs": (
                    "namespace App.Services { public class B {} }\n"
                ),
                "Web/Home.cs": (
                    "using App.Services;\n"
                    "namespace Web { public class Home {} }\n"
                ),
            },
            ("Web/Home.cs",),
            (
                "Web/Home.cs",
                "ProjA/App/Services/A.cs",
                "imports",
            ),
            "full",
            expected_fallback_fragment="ambiguous reference evidence",
            expected_fallback_code="ambiguous_reference_detected",
            expected_ambiguous_edges=(
                (
                    "Web/Home.cs",
                    "ProjA/App/Services/A.cs",
                    "imports",
                ),
                (
                    "Web/Home.cs",
                    "ProjB/App/Services/B.cs",
                    "imports",
                ),
            ),
            expected_ambiguous_paths=(
                "Web/Home.cs",
                "ProjA/App/Services/A.cs",
                "ProjB/App/Services/B.cs",
            ),
            expected_baseline_uncertainty=False,
            expected_final_uncertainty=True,
        ),
        Case(
            "double_colon_path_import",
            {
                "a.py": "VALUE = 'a'\n",
                "b.py": "VALUE = 'b'\n",
                "weird::name.py": "import a\nvalue = a.VALUE\n",
            },
            {
                "a.py": "VALUE = 'a'\n",
                "b.py": "VALUE = 'b'\n",
                "weird::name.py": "import b\nvalue = b.VALUE\n",
            },
            ("weird::name.py",),
            ("weird::name.py", "b.py", "imports"),
            "patch",
            expected_absent_edge=("weird::name.py", "a.py", "imports"),
            expected_resolution_context_file_count=2,
        ),
        Case(
            "angle_bracket_path_import",
            {
                "a.js": "export const value = 'a';\n",
                "lib<x>.js": "export const value = 'x';\n",
                "consumer.js": (
                    'import { value } from "./a.js";\n'
                    "export const result = value;\n"
                ),
            },
            {
                "a.js": "export const value = 'a';\n",
                "lib<x>.js": "export const value = 'x';\n",
                "consumer.js": (
                    'import { value } from "./lib<x>.js";\n'
                    "export const result = value;\n"
                ),
            },
            ("consumer.js",),
            ("consumer.js", "lib<x>.js", "imports"),
            "patch",
            expected_absent_edge=("consumer.js", "a.js", "imports"),
            expected_resolution_context_file_count=2,
        ),
        Case(
            "calls",
            {
                "lib.py": (
                    "class Toolkit:\n    pass\n\n"
                    "def alpha():\n    return 1\n\n"
                    "def beta():\n    return 2\n"
                ),
                "consumer.py": "from lib import alpha\nresult = alpha()\n",
            },
            {
                "lib.py": (
                    "class Toolkit:\n    pass\n\n"
                    "def alpha():\n    return 1\n\n"
                    "def beta():\n    return 2\n"
                ),
                "consumer.py": "from lib import beta\nresult = beta()\n",
            },
            ("consumer.py",),
            ("consumer.py", "beta", "calls"),
            "patch",
        ),
        Case(
            "extractor_file_failed",
            {
                "target.py": "def target():\n    return 1\n",
                "failed.py": (
                    "from target import target\n"
                    "value = target()\n"
                ),
            },
            {
                "target.py": "def target():\n    return 1\n",
                "failed.py": (
                    "from target import target\n"
                    "value = target() + 1\n"
                ),
            },
            ("failed.py",),
            None,
            "full",
            expected_fallback_fragment="failed file extractor",
            expected_fallback_code="extractor_file_failed",
            failed_extractor_path="failed.py",
            expected_baseline_uncertainty=False,
            expected_final_uncertainty=True,
        ),
        Case(
            "extractor_file_recovered",
            {
                "target.py": "def target():\n    return 1\n",
                "failed.py": (
                    "from target import target\n"
                    "value = target()\n"
                ),
            },
            {
                "target.py": "def target():\n    return 1\n",
                "failed.py": (
                    "from target import target\n"
                    "value = target() + 1\n"
                ),
            },
            ("failed.py",),
            ("failed.py", "target.py", "imports"),
            "full",
            expected_fallback_fragment="stored graph contains unresolved",
            expected_fallback_code="stored_graph_uncertainty",
            baseline_failed_extractor_path="failed.py",
            expected_baseline_uncertainty=True,
            expected_final_uncertainty=False,
        ),
        Case(
            "extractor_file_incomplete",
            {
                "target.js": "export const target = 1;\n",
                "incomplete.js": (
                    "import { target } from './target.js';\n"
                    "export const value = target;\n"
                ),
            },
            {
                "target.js": "export const target = 1;\n",
                "incomplete.js": (
                    "import { target } from './target.js';\n"
                    "export const value = target + 1;\n"
                ),
            },
            ("incomplete.js",),
            ("incomplete.js", "target.js", "imports"),
            "full",
            expected_fallback_fragment="incomplete file parser",
            expected_fallback_code="extractor_file_incomplete",
            incomplete_extractor_path="incomplete.js",
            expected_baseline_uncertainty=False,
            expected_final_uncertainty=True,
        ),
        Case(
            "extractor_file_incomplete_recovered",
            {
                "target.js": "export const target = 1;\n",
                "incomplete.js": (
                    "import { target } from './target.js';\n"
                    "export const value = target;\n"
                ),
            },
            {
                "target.js": "export const target = 1;\n",
                "incomplete.js": (
                    "import { target } from './target.js';\n"
                    "export const value = target + 1;\n"
                ),
            },
            ("incomplete.js",),
            ("incomplete.js", "target.js", "imports"),
            "full",
            expected_fallback_fragment="stored graph contains unresolved",
            expected_fallback_code="stored_graph_uncertainty",
            baseline_incomplete_extractor_path="incomplete.js",
            expected_baseline_uncertainty=True,
            expected_final_uncertainty=False,
        ),
        Case(
            "regular_file_becomes_symlink",
            {
                "real.py": "def real():\n    return 'target'\n",
                "linked.py": "def linked():\n    return 'baseline'\n",
                "consumer.py": (
                    "import linked\n"
                    "value = linked.linked()\n"
                ),
            },
            {
                "real.py": "def real():\n    return 'target'\n",
                # Contents raw media follows the symlink and returns these
                # target bytes; the target tree mode remains authoritative.
                "linked.py": "def real():\n    return 'target'\n",
                "consumer.py": (
                    "import linked\n"
                    "value = linked.linked()\n"
                ),
            },
            ("linked.py",),
            None,
            "full",
            expected_absent_edge=(
                "consumer.py",
                "linked.py",
                "imports",
            ),
            expected_input_file_count=2,
            expected_fallback_fragment=(
                "target tree path is not a regular file"
            ),
            expected_fallback_code="target_tree_mode_unverified",
            end_symlinks=(("linked.py", "real.py"),),
            expected_nonregular_absent_paths=("linked.py",),
        ),
        Case(
            "gitattributes_linguist_exclusions",
            {
                ".gitattributes": (
                    "src/api_client.py linguist-generated\n"
                    "src/adapter.ts linguist-vendored\n"
                    "src/cross.gen.ts linguist-generated\n"
                    "src/cross.gen.ts -linguist-vendored\n"
                    "src/heuristic.gen.ts -linguist-vendored\n"
                    "src/reset.gen.ts linguist-generated\n"
                    "src/reset.gen.ts !linguist-generated\n"
                ),
                "src/api_client.py": (
                    "def generated_client():\n"
                    "    return 'baseline'\n"
                ),
                "src/adapter.ts": "export const adapter = 'baseline';\n",
                "src/cross.gen.ts": "export const cross = 'baseline';\n",
                "src/heuristic.gen.ts": (
                    "export const heuristic = 'baseline';\n"
                ),
                "src/reset.gen.ts": "export const reset = 'baseline';\n",
                "src/kept.py": "VALUE = 'kept'\n",
            },
            {
                ".gitattributes": (
                    "src/api_client.py linguist-generated\n"
                    "src/adapter.ts linguist-vendored\n"
                    "src/cross.gen.ts linguist-generated\n"
                    "src/cross.gen.ts -linguist-vendored\n"
                    "src/heuristic.gen.ts -linguist-vendored\n"
                    "src/reset.gen.ts linguist-generated\n"
                    "src/reset.gen.ts !linguist-generated\n"
                ),
                "src/api_client.py": (
                    "def generated_client():\n"
                    "    return 'changed-at-target-sha'\n"
                ),
                "src/adapter.ts": (
                    "export const adapter = 'changed-at-target-sha';\n"
                ),
                "src/cross.gen.ts": (
                    "export const cross = 'changed-at-target-sha';\n"
                ),
                "src/heuristic.gen.ts": (
                    "export const heuristic = 'changed-at-target-sha';\n"
                ),
                "src/reset.gen.ts": (
                    "export const reset = 'changed-at-target-sha';\n"
                ),
                "src/kept.py": "VALUE = 'kept'\n",
            },
            (
                "src/api_client.py",
                "src/adapter.ts",
                "src/cross.gen.ts",
                "src/heuristic.gen.ts",
                "src/reset.gen.ts",
            ),
            None,
            "full",
            expected_absent_paths=(
                "src/api_client.py",
                "src/adapter.ts",
                "src/cross.gen.ts",
                "src/heuristic.gen.ts",
                "src/reset.gen.ts",
            ),
            expected_present_paths=(".gitattributes", "src/kept.py"),
            expected_input_file_count=2,
            expected_fallback_fragment=(
                "changed path absent from persisted file universe"
            ),
            expected_fallback_code=(
                "changed_path_absent_from_persisted_universe"
            ),
        ),
        Case(
            "gitattributes_rule_change",
            {
                ".gitattributes": (
                    "src/client.gen.ts linguist-generated\n"
                ),
                "src/client.gen.ts": (
                    "export const client = 'retained-after-rule-change';\n"
                ),
                "app.ts": (
                    "import { client } from './src/client.gen.ts';\n"
                    "export const selected = client;\n"
                ),
            },
            {
                ".gitattributes": (
                    "src/client.gen.ts -linguist-generated\n"
                ),
                "src/client.gen.ts": (
                    "export const client = 'retained-after-rule-change';\n"
                ),
                "app.ts": (
                    "import { client } from './src/client.gen.ts';\n"
                    "export const selected = client;\n"
                ),
            },
            (".gitattributes",),
            ("app.ts", "src/client.gen.ts", "imports"),
            "full",
            expected_present_paths=(
                ".gitattributes",
                "src/client.gen.ts",
                "app.ts",
            ),
            expected_baseline_absent_paths=("src/client.gen.ts",),
            expected_input_file_count=3,
            expected_fallback_fragment="stored graph contains unresolved",
            expected_fallback_code="stored_graph_uncertainty",
            expected_baseline_uncertainty=True,
            expected_final_uncertainty=False,
        ),
        Case(
            "gitattributes_added",
            {
                "src/client.ts": (
                    "export const client = 'baseline-visible';\n"
                ),
                "app.ts": (
                    "import { client } from './src/client';\n"
                    "export const selected = client;\n"
                ),
            },
            {
                ".gitattributes": (
                    "src/client.ts linguist-generated\n"
                ),
                "src/client.ts": (
                    "export const client = 'baseline-visible';\n"
                ),
                "app.ts": (
                    "import { client } from './src/client';\n"
                    "export const selected = client;\n"
                ),
            },
            (".gitattributes",),
            None,
            "full",
            expected_absent_edge=(
                "app.ts",
                "src/client.ts",
                "imports",
            ),
            expected_present_paths=(".gitattributes", "app.ts"),
            expected_baseline_absent_paths=(".gitattributes",),
            expected_input_file_count=2,
            expected_fallback_fragment=(
                "changed path absent from persisted file universe"
            ),
            expected_fallback_code=(
                "changed_path_absent_from_persisted_universe"
            ),
            expected_end_absent_paths=("src/client.ts",),
            expected_baseline_present_paths=("src/client.ts",),
        ),
        Case(
            "gitattributes_removed",
            {
                ".gitattributes": (
                    "src/client.ts linguist-generated\n"
                ),
                "src/client.ts": (
                    "export const client = 'target-visible';\n"
                ),
                "app.ts": (
                    "import { client } from './src/client';\n"
                    "export const selected = client;\n"
                ),
            },
            {
                "src/client.ts": (
                    "export const client = 'target-visible';\n"
                ),
                "app.ts": (
                    "import { client } from './src/client';\n"
                    "export const selected = client;\n"
                ),
            },
            (),
            ("app.ts", "src/client.ts", "imports"),
            "full",
            expected_present_paths=("src/client.ts", "app.ts"),
            expected_baseline_absent_paths=("src/client.ts",),
            expected_input_file_count=2,
            expected_fallback_fragment="stored graph contains unresolved",
            expected_fallback_code="stored_graph_uncertainty",
            removed=(".gitattributes",),
            expected_end_absent_paths=(".gitattributes",),
            expected_baseline_uncertainty=True,
            expected_final_uncertainty=False,
        ),
        Case(
            "gitattributes_root_carveout",
            {
                ".gitattributes": (
                    "src/service_pb2.py -linguist-generated\n"
                ),
                "src/service_pb2.py": (
                    "def retained_client():\n"
                    "    return 'baseline'\n"
                ),
                "src/kept.py": "VALUE = 'kept'\n",
            },
            {
                ".gitattributes": (
                    "src/service_pb2.py -linguist-generated\n"
                ),
                "src/service_pb2.py": (
                    "def retained_client():\n"
                    "    return 'changed-at-target-sha'\n"
                ),
                "src/kept.py": "VALUE = 'kept'\n",
            },
            ("src/service_pb2.py",),
            None,
            "patch",
            expected_present_paths=(
                ".gitattributes",
                "src/service_pb2.py",
            ),
            expected_input_file_count=3,
            expected_resolution_context_file_count=2,
        ),
        Case(
            "gitattributes_nested_carveout",
            {
                ".gitattributes": "sdk/** linguist-generated\n",
                "sdk/.gitattributes": (
                    "client.gen.ts -linguist-generated\n"
                    "other.ts -linguist-generated\n"
                ),
                "sdk/client.gen.ts": (
                    "export const client = 'retained';\n"
                ),
                "sdk/other.ts": "export const other = 'other';\n",
                "app.ts": (
                    "import { other } from './sdk/other';\n"
                    "export const selected = other;\n"
                ),
            },
            {
                ".gitattributes": "sdk/** linguist-generated\n",
                "sdk/.gitattributes": (
                    "client.gen.ts -linguist-generated\n"
                    "other.ts -linguist-generated\n"
                ),
                "sdk/client.gen.ts": (
                    "export const client = 'retained';\n"
                ),
                "sdk/other.ts": "export const other = 'other';\n",
                "app.ts": (
                    "import { client } from './sdk/client.gen.ts';\n"
                    "export const selected = client;\n"
                ),
            },
            ("app.ts",),
            ("app.ts", "sdk/client.gen.ts", "imports"),
            "patch",
            expected_present_paths=(
                ".gitattributes",
                "sdk/.gitattributes",
                "sdk/client.gen.ts",
            ),
            expected_input_file_count=5,
            expected_resolution_context_file_count=4,
        ),
        Case(
            "gitattributes_legacy_cg3_baseline",
            {
                ".gitattributes": (
                    "src/service_pb2.py -linguist-generated\n"
                ),
                "src/service_pb2.py": (
                    "def retained_client():\n"
                    "    return 'cg3-baseline'\n"
                ),
                "app.py": (
                    "from src import service_pb2\n"
                    "value = service_pb2.retained_client()\n"
                ),
            },
            {
                ".gitattributes": (
                    "src/service_pb2.py -linguist-generated\n"
                ),
                "src/service_pb2.py": (
                    "def retained_client():\n"
                    "    return 'cg4-target'\n"
                ),
                "app.py": (
                    "from src import service_pb2\n"
                    "value = service_pb2.retained_client()\n"
                ),
            },
            ("src/service_pb2.py",),
            ("app.py", "src/service_pb2.py", "imports"),
            "full",
            expected_present_paths=(
                ".gitattributes",
                "src/service_pb2.py",
                "app.py",
            ),
            expected_baseline_absent_paths=(".gitattributes",),
            expected_input_file_count=3,
            expected_fallback_fragment=(
                "stored/current graph extractor version does not match"
            ),
            expected_fallback_code="extractor_version_mismatch",
            baseline_extractor_version="cg3",
            baseline_drop_paths=(".gitattributes",),
        ),
        Case(
            "semantic_ref_v0_baseline",
            {
                "lib<x>.js": "export const value = 'exact';\n",
                "consumer.js": (
                    'import { value } from "./lib<x>.js";\n'
                    "export const result = value;\n"
                ),
            },
            {
                "lib<x>.js": "export const value = 'exact';\n",
                "consumer.js": (
                    'import { value } from "./lib<x>.js";\n'
                    "export const result = `${value}-changed`;\n"
                ),
            },
            ("consumer.js",),
            ("consumer.js", "lib<x>.js", "imports"),
            "full",
            expected_fallback_fragment=(
                "stored/current semantic reference version does not match"
            ),
            expected_fallback_code="semantic_reference_version_mismatch",
            baseline_semantic_ref_version=0,
        ),
        Case(
            "database_table",
            {"db/schema.sql": db_schema, "consumer.py": db_base},
            {"db/schema.sql": db_schema, "consumer.py": db_end},
            ("consumer.py",),
            ("consumer.py", "users", "queries"),
            "patch",
            True,
        ),
        Case(
            "database_column",
            {"db/schema.sql": db_schema, "consumer.py": db_base},
            {"db/schema.sql": db_schema, "consumer.py": db_end},
            ("consumer.py",),
            ("consumer.py", "users.email", "queries_col"),
            "patch",
            True,
        ),
        Case(
            "database_duplicate_definition_consumer",
            {
                "db/schema_a.sql": (
                    "CREATE TABLE orders (id integer);\n"
                ),
                "db/schema_b.sql": (
                    "CREATE TABLE orders (id integer);\n"
                ),
                "consumer.py": "status = 'idle'\n",
            },
            {
                "db/schema_a.sql": (
                    "CREATE TABLE orders (id integer);\n"
                ),
                "db/schema_b.sql": (
                    "CREATE TABLE orders (id integer);\n"
                ),
                "consumer.py": (
                    'result = db.execute("SELECT id FROM orders")\n'
                ),
            },
            ("consumer.py",),
            ("consumer.py", "orders", "queries"),
            "full",
            expected_fallback_fragment="stored graph contains unresolved",
            expected_fallback_code="stored_graph_uncertainty",
            expected_ambiguous_edges=(
                ("db/schema_a.sql", "orders", "alters"),
                ("db/schema_b.sql", "orders", "alters"),
                ("consumer.py", "orders", "queries"),
                ("consumer.py", "orders.id", "queries_col"),
            ),
            expected_ambiguous_paths=(
                "db/schema_a.sql",
                "db/schema_b.sql",
                "consumer.py",
            ),
            expected_baseline_uncertainty=True,
            expected_final_uncertainty=True,
        ),
        Case(
            "database_ambiguous_to_unique",
            {
                "db/schema_a.sql": (
                    "CREATE TABLE orders (id integer);\n"
                ),
                "db/schema_b.sql": (
                    "CREATE TABLE orders (id integer);\n"
                ),
                "consumer.py": (
                    'result = db.execute("SELECT id FROM orders")\n'
                ),
            },
            {
                "db/schema_a.sql": (
                    "-- orders definition intentionally removed\n"
                ),
                "db/schema_b.sql": (
                    "CREATE TABLE orders (id integer);\n"
                ),
                "consumer.py": (
                    'result = db.execute("SELECT id FROM orders")\n'
                ),
            },
            ("db/schema_a.sql",),
            ("consumer.py", "orders", "queries"),
            "full",
            expected_fallback_fragment="stored graph contains unresolved",
            expected_fallback_code="stored_graph_uncertainty",
            expected_baseline_uncertainty=True,
            expected_final_uncertainty=False,
        ),
        Case(
            "config",
            {
                "config/app.json": json.dumps(
                    {"legacy_webhook_secret": "old", "billing_webhook_secret": "new"}
                ),
                "consumer.py": "value = cfg['legacy_webhook_secret']\n",
            },
            {
                "config/app.json": json.dumps(
                    {"legacy_webhook_secret": "old", "billing_webhook_secret": "new"}
                ),
                "consumer.py": "value = cfg['billing_webhook_secret']\n",
            },
            ("consumer.py",),
            ("consumer.py", "billing_webhook_secret", "reads_config"),
            "patch",
            True,
        ),
        Case(
            "config_node_id_collision",
            {
                "settings.json": json.dumps(
                    {"veripsa.webhook.py": "configured"}
                ),
                "cfgkey::settings.json::veripsa.webhook.py": (
                    "value = 'baseline'\n"
                ),
            },
            {
                "settings.json": json.dumps(
                    {"veripsa.webhook.py": "configured"}
                ),
                "cfgkey::settings.json::veripsa.webhook.py": (
                    "value = cfg['veripsa.webhook.py']\n"
                ),
            },
            ("cfgkey::settings.json::veripsa.webhook.py",),
            (
                "cfgkey::settings.json::veripsa.webhook.py",
                "veripsa.webhook.py",
                "reads_config",
            ),
            "patch",
            True,
            expected_resolution_context_file_count=1,
        ),
        Case(
            "config_definition_removal",
            {
                "config/app.json": json.dumps({"ONLY_KEY": "value"}),
                "consumer.py": "value = cfg['ONLY_KEY']\n",
            },
            {
                "config/app.json": json.dumps({}),
                "consumer.py": "value = cfg['ONLY_KEY']\n",
            },
            ("config/app.json",),
            None,
            "full",
            expected_absent_edge=("consumer.py", "ONLY_KEY", "reads_config"),
        ),
        Case(
            "terraform",
            {
                "infra/storage.tf": (
                    'resource "aws_s3_bucket" "logs" {}\n'
                    'resource "aws_s3_bucket" "archive" {}\n'
                ),
                "infra/output.tf": (
                    'output "selected_bucket" { value = aws_s3_bucket.logs.id }\n'
                ),
            },
            {
                "infra/storage.tf": (
                    'resource "aws_s3_bucket" "logs" {}\n'
                    'resource "aws_s3_bucket" "archive" {}\n'
                ),
                "infra/output.tf": (
                    'output "selected_bucket" { value = aws_s3_bucket.archive.id }\n'
                ),
            },
            ("infra/output.tf",),
            ("infra/output.tf", "infra::aws_s3_bucket.archive", "queries"),
            "patch",
            True,
        ),
        Case(
            "kubernetes",
            {
                "k8s/configmaps.yaml": (
                    "apiVersion: v1\nkind: ConfigMap\n"
                    "metadata:\n  name: old-config\n  namespace: prod\n"
                    "---\napiVersion: v1\nkind: ConfigMap\n"
                    "metadata:\n  name: new-config\n  namespace: prod\n"
                ),
                "k8s/deployment.yaml": k8s_old,
            },
            {
                "k8s/configmaps.yaml": (
                    "apiVersion: v1\nkind: ConfigMap\n"
                    "metadata:\n  name: old-config\n  namespace: prod\n"
                    "---\napiVersion: v1\nkind: ConfigMap\n"
                    "metadata:\n  name: new-config\n  namespace: prod\n"
                ),
                "k8s/deployment.yaml": k8s_new,
            },
            ("k8s/deployment.yaml",),
            (
                "k8s/deployment.yaml",
                "k8s::configmap::prod::new-config",
                "queries",
            ),
            "full",
        ),
        Case(
            "kubernetes_definition_removal",
            {
                "k8s/configmap.yaml": (
                    "apiVersion: v1\n"
                    "kind: ConfigMap\n"
                    "metadata:\n"
                    "  name: runtime-config\n"
                    "  namespace: prod\n"
                ),
                "k8s/deployment.yaml": (
                    "apiVersion: apps/v1\n"
                    "kind: Deployment\n"
                    "metadata:\n"
                    "  name: api\n"
                    "  namespace: prod\n"
                    "spec:\n"
                    "  template:\n"
                    "    metadata:\n"
                    "      labels:\n"
                    "        app: api\n"
                    "    spec:\n"
                    "      containers:\n"
                    "      - name: api\n"
                    "        image: example/api:1\n"
                    "        envFrom:\n"
                    "        - configMapRef:\n"
                    "            name: runtime-config\n"
                ),
            },
            {
                "k8s/configmap.yaml": (
                    "notes: this file no longer defines a Kubernetes object\n"
                ),
                "k8s/deployment.yaml": (
                    "apiVersion: apps/v1\n"
                    "kind: Deployment\n"
                    "metadata:\n"
                    "  name: api\n"
                    "  namespace: prod\n"
                    "spec:\n"
                    "  template:\n"
                    "    metadata:\n"
                    "      labels:\n"
                    "        app: api\n"
                    "    spec:\n"
                    "      containers:\n"
                    "      - name: api\n"
                    "        image: example/api:1\n"
                    "        envFrom:\n"
                    "        - configMapRef:\n"
                    "            name: runtime-config\n"
                ),
            },
            ("k8s/configmap.yaml",),
            None,
            "full",
            expected_absent_edge=(
                "k8s/deployment.yaml",
                "k8s::configmap::prod::runtime-config",
                "queries",
            ),
        ),
        Case(
            "graphql",
            {
                "schema.graphql": (
                    "type Order { id: ID! }\n"
                    "type Invoice { id: ID! }\n"
                ),
                "consumer.js": (
                    "const q = gql`{ node { ... on Order { id } } }`;\n"
                ),
            },
            {
                "schema.graphql": (
                    "type Order { id: ID! }\n"
                    "type Invoice { id: ID! }\n"
                ),
                "consumer.js": (
                    "const q = gql`{ node { ... on Invoice { id } } }`;\n"
                ),
            },
            ("consumer.js",),
            ("consumer.js", "api_type::Invoice", "queries"),
            "patch",
            True,
        ),
        Case(
            "graphql_definition_catalog_truncated",
            {
                "schema.graphql": (
                    "# complete baseline schema with no declarations\n"
                ),
                "consumer.js": (
                    "const q = gql`{ node { ... on Invoice { id } } }`;\n"
                ),
            },
            {
                "schema.graphql": graphql_truncated_schema,
                "consumer.js": (
                    "const q = gql`{ node { ... on Invoice { id } } }`;\n"
                ),
            },
            ("schema.graphql",),
            None,
            "full",
            expected_input_file_count=2,
            expected_fallback_fragment="incomplete file parser",
            expected_fallback_code="extractor_file_incomplete",
            expected_incomplete_paths=(
                "schema.graphql",
                "consumer.js",
            ),
            expected_baseline_uncertainty=False,
            expected_final_uncertainty=True,
        ),
        Case(
            "protobuf",
            {
                "contracts.proto": (
                    'syntax = "proto3";\n'
                    "message CreateOrderRequest { string id = 1; }\n"
                    "message RefundRequest { string id = 1; }\n"
                    "service OrderService { rpc Create(CreateOrderRequest) returns (CreateOrderRequest); }\n"
                    "service BillingService { rpc Refund(RefundRequest) returns (RefundRequest); }\n"
                ),
                "consumer.go": (
                    "package main\nvar req = CreateOrderRequest{}\n"
                ),
            },
            {
                "contracts.proto": (
                    'syntax = "proto3";\n'
                    "message CreateOrderRequest { string id = 1; }\n"
                    "message RefundRequest { string id = 1; }\n"
                    "service OrderService { rpc Create(CreateOrderRequest) returns (CreateOrderRequest); }\n"
                    "service BillingService { rpc Refund(RefundRequest) returns (RefundRequest); }\n"
                ),
                "consumer.go": "package main\nvar req = RefundRequest{}\n",
            },
            ("consumer.go",),
            ("consumer.go", "api_message::RefundRequest", "queries"),
            "patch",
            True,
        ),
        Case(
            "openapi",
            {
                "openapi.yaml": openapi,
                "handler.py": "def getUser(request):\n    return request\n",
            },
            {
                "openapi.yaml": openapi,
                "handler.py": "def getInvoice(request):\n    return request\n",
            },
            ("handler.py",),
            ("handler.py", "api_operation::getInvoice", "queries"),
            "patch",
        ),
        Case(
            "routes",
            {
                "server/routes.py": (
                    "from fastapi import FastAPI\napp = FastAPI()\n"
                    "@app.get('/api/orders/{id}')\n"
                    "def order(id): return id\n"
                    "@app.get('/api/invoices/{id}')\n"
                    "def invoice(id): return id\n"
                ),
                "web/client.ts": (
                    "export const load = () => fetch('/api/orders/7');\n"
                ),
            },
            {
                "server/routes.py": (
                    "from fastapi import FastAPI\napp = FastAPI()\n"
                    "@app.get('/api/orders/{id}')\n"
                    "def order(id): return id\n"
                    "@app.get('/api/invoices/{id}')\n"
                    "def invoice(id): return id\n"
                ),
                "web/client.ts": (
                    "export const load = () => fetch('/api/invoices/7');\n"
                ),
            },
            ("web/client.ts",),
            ("web/client.ts", "server/routes.py", "imports"),
            "full",
        ),
        Case(
            "github_actions_package_scripts",
            {
                "package.json": json.dumps(
                    {"scripts": {"typecheck": "tsc --noEmit", "deploy": "node deploy.js"}}
                ),
                ".github/workflows/ci.yml": (
                    "jobs:\n  run:\n    steps:\n      - run: npm run typecheck\n"
                ),
            },
            {
                "package.json": json.dumps(
                    {"scripts": {"typecheck": "tsc --noEmit", "deploy": "node deploy.js"}}
                ),
                ".github/workflows/ci.yml": (
                    "jobs:\n  run:\n    steps:\n      - run: npm run deploy\n"
                ),
            },
            (".github/workflows/ci.yml",),
            (".github/workflows/ci.yml", "ci_script::.::deploy", "queries"),
            "full",
        ),
        Case(
            "tauri",
            {
                "src-tauri/src/commands.rs": (
                    "#[tauri::command]\nfn greet() {}\n"
                    "#[tauri::command]\nfn save_file() {}\n"
                ),
                "consumer.ts": (
                    "import { invoke } from '@tauri-apps/api/core';\n"
                    "invoke('greet');\n"
                ),
            },
            {
                "src-tauri/src/commands.rs": (
                    "#[tauri::command]\nfn greet() {}\n"
                    "#[tauri::command]\nfn save_file() {}\n"
                ),
                "consumer.ts": (
                    "import { invoke } from '@tauri-apps/api/core';\n"
                    "invoke('save_file');\n"
                ),
            },
            ("consumer.ts",),
            ("consumer.ts", "tauri_command::save_file", "queries"),
            "full",
        ),
        Case(
            "tauri_add_first_pair",
            {
                "src-tauri/src/commands.rs": (
                    "#[tauri::command]\n"
                    "fn greet() {}\n"
                ),
            },
            {
                "src-tauri/src/commands.rs": (
                    "#[tauri::command]\n"
                    "fn greet() {}\n"
                ),
                "consumer.ts": (
                    "import { invoke } from '@tauri-apps/api/core';\n"
                    "invoke('greet');\n"
                ),
            },
            ("consumer.ts",),
            ("consumer.ts", "tauri_command::greet", "queries"),
            "full",
        ),
        Case(
            "celery",
            {
                "workers.py": (
                    "from celery import shared_task\n"
                    "@shared_task(name='billing.close_invoice')\n"
                    "def close_invoice(): pass\n"
                    "@shared_task(name='billing.refund_invoice')\n"
                    "def refund_invoice(): pass\n"
                ),
                "consumer.py": (
                    "from celery import Celery\n"
                    "app = Celery('billing')\n"
                    "app.send_task('billing.close_invoice')\n"
                ),
            },
            {
                "workers.py": (
                    "from celery import shared_task\n"
                    "@shared_task(name='billing.close_invoice')\n"
                    "def close_invoice(): pass\n"
                    "@shared_task(name='billing.refund_invoice')\n"
                    "def refund_invoice(): pass\n"
                ),
                "consumer.py": (
                    "from celery import Celery\n"
                    "app = Celery('billing')\n"
                    "app.send_task('billing.refund_invoice')\n"
                ),
            },
            ("consumer.py",),
            (
                "consumer.py",
                "job_task::celery::billing.refund_invoice",
                "queries",
            ),
            "full",
        ),
        Case(
            "celery_last_reference_removal",
            {
                "workers.py": (
                    "from celery import shared_task\n"
                    "@shared_task(name='billing.close_invoice')\n"
                    "def close_invoice(): pass\n"
                ),
                "consumer.py": (
                    "from celery import Celery\n"
                    "app = Celery('billing')\n"
                    "app.send_task('billing.close_invoice')\n"
                ),
            },
            {
                "workers.py": (
                    "from celery import shared_task\n"
                    "@shared_task(name='billing.close_invoice')\n"
                    "def close_invoice(): pass\n"
                ),
                "consumer.py": (
                    "from celery import Celery\n"
                    "app = Celery('billing')\n"
                    "status = 'idle'\n"
                ),
            },
            ("consumer.py",),
            None,
            "full",
            expected_absent_edge=(
                "consumer.py",
                "job_task::celery::billing.close_invoice",
                "queries",
            ),
        ),
        Case(
            "celery_add_first_pair",
            {
                "workers.py": (
                    "from celery import shared_task\n"
                    "@shared_task(name='billing.close_invoice')\n"
                    "def close_invoice(): pass\n"
                ),
            },
            {
                "workers.py": (
                    "from celery import shared_task\n"
                    "@shared_task(name='billing.close_invoice')\n"
                    "def close_invoice(): pass\n"
                ),
                "consumer.py": (
                    "from celery import Celery\n"
                    "app = Celery('billing')\n"
                    "app.send_task('billing.close_invoice')\n"
                ),
            },
            ("consumer.py",),
            (
                "consumer.py",
                "job_task::celery::billing.close_invoice",
                "queries",
            ),
            "full",
        ),
        Case(
            "bullmq",
            {
                "workers.ts": (
                    "import { Worker } from 'bullmq';\n"
                    "new Worker('normal-jobs', async job => job.name);\n"
                    "new Worker('critical-jobs', async job => job.name);\n"
                ),
                "consumer.ts": (
                    "import { Queue } from 'bullmq';\n"
                    "const queue = new Queue('normal-jobs');\n"
                ),
            },
            {
                "workers.ts": (
                    "import { Worker } from 'bullmq';\n"
                    "new Worker('normal-jobs', async job => job.name);\n"
                    "new Worker('critical-jobs', async job => job.name);\n"
                ),
                "consumer.ts": (
                    "import { Queue } from 'bullmq';\n"
                    "const queue = new Queue('critical-jobs');\n"
                ),
            },
            ("consumer.ts",),
            ("consumer.ts", "job_queue::bullmq::critical-jobs", "queries"),
            "full",
        ),
        Case(
            "bullmq_last_reference_removal",
            {
                "workers.ts": (
                    "import { Worker } from 'bullmq';\n"
                    "new Worker('billing-jobs', async job => job.name);\n"
                ),
                "consumer.ts": (
                    "import { Queue } from 'bullmq';\n"
                    "const queue = new Queue('billing-jobs');\n"
                ),
            },
            {
                "workers.ts": (
                    "import { Worker } from 'bullmq';\n"
                    "new Worker('billing-jobs', async job => job.name);\n"
                ),
                "consumer.ts": (
                    "import { Queue } from 'bullmq';\n"
                    "const status = 'idle';\n"
                ),
            },
            ("consumer.ts",),
            None,
            "full",
            expected_absent_edge=(
                "consumer.ts",
                "job_queue::bullmq::billing-jobs",
                "queries",
            ),
        ),
        Case(
            "bullmq_add_first_pair",
            {
                "workers.ts": (
                    "import { Worker } from 'bullmq';\n"
                    "new Worker('billing-jobs', async job => job.name);\n"
                ),
            },
            {
                "workers.ts": (
                    "import { Worker } from 'bullmq';\n"
                    "new Worker('billing-jobs', async job => job.name);\n"
                ),
                "consumer.ts": (
                    "import { Queue } from 'bullmq';\n"
                    "const queue = new Queue('billing-jobs');\n"
                ),
            },
            ("consumer.ts",),
            (
                "consumer.ts",
                "job_queue::bullmq::billing-jobs",
                "queries",
            ),
            "full",
        ),
        Case(
            "sibling_stem",
            {
                "internal/db/user.go": "package db\n",
                "internal/server/user.go": "package server\n",
            },
            {
                "internal/db/user.go": "package db\n",
                "internal/server/user.go": "package server\n// end-state marker\n",
            },
            ("internal/server/user.go",),
            (
                "internal/server/user.go",
                "sibling_stem::go::internal::user",
                "queries",
            ),
            "full",
        ),
        Case(
            "role_feature",
            {
                "frontend/src/components/SettingsPanel.tsx": "export const x = 1;\n",
                "frontend/src/pages/Settings.tsx": "export const y = 1;\n",
            },
            {
                "frontend/src/components/SettingsPanel.tsx": "export const x = 1;\n",
                "frontend/src/pages/Settings.tsx": (
                    "export const y = 2;\n// end-state marker\n"
                ),
            },
            ("frontend/src/pages/Settings.tsx",),
            (
                "frontend/src/pages/Settings.tsx",
                "role_feature::web_frontend::frontend::src::components__pages::setting",
                "queries",
            ),
            "full",
        ),
    )


class FakeGH:
    """Target-sha file/tarball API over one immutable in-memory repository."""

    def __init__(
        self,
        files: dict[str, str],
        symlinks: tuple[tuple[str, str], ...] = (),
    ):
        self._files = {
            path: body.encode("utf-8") for path, body in files.items()
        }
        self._symlinks = dict(symlinks)
        self.download_tarball_calls = 0

    def get_file_at(self, repo: str, path: str, ref: str) -> bytes | None:
        del repo, ref
        return self._files.get(path)

    def target_file_modes(self, repo: str, ref: str, paths) -> dict:
        del repo, ref
        wanted = set(paths or ())
        return {
            "complete": True,
            "truncated": False,
            "malformed": False,
            "over_cap": False,
            "entries": {
                path: {
                    "mode": (
                        "120000"
                        if path in self._symlinks
                        else "100644"
                    ),
                    "type": "blob",
                }
                for path in wanted
                if path in self._files
            },
        }

    def download_tarball(self, repo: str, sha: str) -> bytes:
        del repo, sha
        self.download_tarball_calls += 1
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            root = tarfile.TarInfo("repo")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            tf.addfile(root)
            for path, body in sorted(self._files.items()):
                info = tarfile.TarInfo("repo/" + path)
                if path in self._symlinks:
                    info.type = tarfile.SYMTYPE
                    info.linkname = self._symlinks[path]
                    info.mode = 0o777
                    tf.addfile(info)
                    continue
                info.size = len(body)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(body))
        return buf.getvalue()


class DBSession:
    """One transaction-capable callable, matching the production ``db`` API."""

    def __init__(self, role: str):
        self.conn = psycopg2.connect(
            f"postgresql://{role}@localhost/{DB}"
        )
        # Production's transaction-scoped runner establishes search_path
        # before the event body. Do the same once here: issuing SET before
        # every query would itself fail in an aborted transaction and prevent
        # `_reingest_graph` from executing ROLLBACK TO SAVEPOINT.
        with self.conn.cursor() as cur:
            cur.execute("SET search_path=core")
        self.conn.commit()

    def __call__(self, sql: str, args: Iterable | tuple = ()):
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            if cur.description is None:
                return None
            row = cur.fetchone()
            return row[0] if row else None

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


@contextmanager
def _forced_python_extractor_failure(relative_path: str | None):
    """Fail one Python document deterministically, restoring the global hook."""
    if relative_path is None:
        yield
        return
    original = X.extract_file_py
    original_build = X.build_graph

    def fail_selected(path, rel, *args, **kwargs):
        if str(rel).replace(os.sep, "/") == relative_path:
            raise RuntimeError("synthetic per-file extractor failure")
        return original(path, rel, *args, **kwargs)

    def build_with_failure(*args, **kwargs):
        return original_build(*args, **kwargs)

    X.extract_file_py = fail_selected
    # Fault injection is intentionally in-process. Rebinding build_graph
    # selects ingest's established injected-builder compatibility seam, while
    # the normal equivalence cases continue to exercise the fresh exec worker.
    X.build_graph = build_with_failure
    try:
        yield
    finally:
        X.build_graph = original_build
        X.extract_file_py = original


@contextmanager
def _forced_incomplete_document(relative_path: str | None):
    """Promote one real extracted document to the closed incomplete state.

    The noded adapter's parser-health fixtures cover detection itself. This
    hook isolates the App/DB rollout invariant: once an extractor reports the
    status, incremental selection must rebuild and persisted full truth must
    hash-identically.
    """
    if relative_path is None:
        yield
        return
    original = X.build_graph

    def build_with_incomplete(*args, **kwargs):
        built = original(*args, **kwargs)
        promoted = False
        for node in built.get("nodes", ()):
            if (
                node.get("kind") in {"file", "config_file"}
                and node.get("path") == relative_path
            ):
                node["analysis_status"] = "incomplete"
                promoted = True
        if not promoted:
            raise AssertionError(
                f"incomplete fixture path was not extracted: {relative_path}"
            )
        return built

    X.build_graph = build_with_incomplete
    try:
        yield
    finally:
        X.build_graph = original


def _write_graph_full(
    db: DBSession,
    files: dict[str, str],
    repo: str,
    sha: str,
    captured_at: str,
    symlinks: tuple[tuple[str, str], ...] = (),
    failed_extractor_path: str | None = None,
    incomplete_extractor_path: str | None = None,
) -> dict:
    with _forced_python_extractor_failure(failed_extractor_path):
        with _forced_incomplete_document(incomplete_extractor_path):
            result = I._full_ingest(
                db, FakeGH(files, symlinks), repo, BRANCH, sha, captured_at
            )
    db.commit()
    return result


def _json_value(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    return json.loads(value)


def _stored_graph(
    admin: DBSession, repo: str
) -> tuple[
    set[str],
    set[tuple[str, str, str]],
    set[tuple[str, str, str]],
]:
    """Canonical DB rows, excluding only their account/coordinate identity."""
    raw_nodes = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT COALESCE(
          jsonb_agg(to_jsonb(n) - 'account_id' - 'repo' - 'branch'
                    ORDER BY n.node_id),
          '[]'::jsonb
        )::text
          FROM core.code_node n
         WHERE n.repo=%s AND n.branch=%s
        """,
        (repo, BRANCH),
    )
    raw_edges = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT COALESCE(
          jsonb_agg(jsonb_build_array(
                      e.src,e.dst,e.edge_kind,e.semantic_dst_key
                    )
                    ORDER BY e.src,e.dst,e.edge_kind),
          '[]'::jsonb
        )::text
          FROM core.code_edge e
         WHERE e.repo=%s AND e.branch=%s
        """,
        (repo, BRANCH),
    )
    nodes = {
        json.dumps(n, sort_keys=True, separators=(",", ":"))
        for n in _json_value(raw_nodes, [])
    }
    edge_rows = _json_value(raw_edges, [])
    edges = {(e[0], e[1], e[2]) for e in edge_rows}
    semantic_edges = {(e[0], e[3], e[2]) for e in edge_rows}
    return nodes, edges, semantic_edges


def _stored_reference_statuses(
    admin: DBSession, repo: str
) -> dict[tuple[str, str, str], str]:
    raw = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT COALESCE(
          jsonb_agg(jsonb_build_array(
                      e.src,e.dst,e.edge_kind,e.reference_status
                    )
                    ORDER BY e.src,e.dst,e.edge_kind),
          '[]'::jsonb
        )::text
          FROM core.code_edge e
         WHERE e.repo=%s AND e.branch=%s
           AND e.reference_status IS NOT NULL
        """,
        (repo, BRANCH),
    )
    return {
        (row[0], row[1], row[2]): row[3]
        for row in _json_value(raw, [])
    }


def _version(admin: DBSession, repo: str) -> dict:
    raw = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT jsonb_build_object(
          'commit_sha',gv.commit_sha,
          'node_count',gv.node_count,
          'edge_count',gv.edge_count,
          'graph_hash',gv.graph_hash,
          'has_graph_uncertainty',
            EXISTS (
              SELECT 1 FROM core.code_node n
               WHERE n.account_id=gv.account_id
                 AND n.repo=gv.repo AND n.branch=gv.branch
                 AND n.analysis_status IS NOT NULL
            )
            OR EXISTS (
              SELECT 1 FROM core.code_edge e
               WHERE e.account_id=gv.account_id
                 AND e.repo=gv.repo AND e.branch=gv.branch
                 AND e.reference_status IS NOT NULL
            ),
          'extractor_version',gv.extractor_version,
          'current_extractor_version',core.current_extractor_version(),
          'semantic_ref_version',gv.semantic_ref_version,
          'current_semantic_ref_version',core.current_semantic_ref_version(),
          'observability',gv.observability
        )::text
          FROM core.graph_version gv
         WHERE gv.repo=%s AND gv.branch=%s
        """,
        (repo, BRANCH),
    )
    return _json_value(raw, {})


def _downgrade_semantic_refs(admin: DBSession, repo: str) -> None:
    """Model a real pre-v1 coordinate: display fields only, no exact keys."""
    admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT core.mark_governed_write('code_node');
        UPDATE core.code_node
           SET semantic_key=NULL
         WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s;
        SELECT core.mark_governed_write('code_edge');
        UPDATE core.code_edge
           SET semantic_dst_key=NULL
         WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s;
        SELECT core.mark_governed_write('graph_version');
        UPDATE core.graph_version
           SET semantic_ref_version=0,
               graph_hash=core._coordinate_graph_hash(
                 'ACCT-DEMO',%s,%s
               )
         WHERE account_id='ACCT-DEMO' AND repo=%s AND branch=%s
        RETURNING semantic_ref_version
        """,
        (repo, BRANCH, repo, BRANCH, repo, BRANCH, repo, BRANCH),
    )
    admin.commit()


def _extract_graph(
    files: dict[str, str],
    symlinks: tuple[tuple[str, str], ...] = (),
    failed_extractor_path: str | None = None,
    incomplete_extractor_path: str | None = None,
) -> dict:
    """Build only to validate that each fixture actually exercises its substrate."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="vp-graph-equiv-fixture-") as root:
        symlink_map = dict(symlinks)
        for rel, body in files.items():
            if rel in symlink_map:
                continue
            dest = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(body)
        for rel, target in symlinks:
            dest = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.symlink(target, dest)
        with _forced_python_extractor_failure(failed_extractor_path):
            with _forced_incomplete_document(incomplete_extractor_path):
                return X.build_graph(root)


def _bounded_diff(left: set, right: set, limit: int = 5) -> str:
    missing = sorted(right - left)[:limit]
    extra = sorted(left - right)[:limit]
    return f"missing={missing!r} extra={extra!r}"


def main() -> int:
    cases = _cases()
    expected_names = {
        "imports", "imports_relative_unresolved",
        "imports_relative_unresolved_recovered",
        "imports_relative_mixed_statement_families",
        "imports_relative_cross_language_stem",
        "imports_own_package_cross_language_entry",
        "imports_ambiguous_fanout",
        "imports_ambiguous_overcap", "double_colon_path_import",
        "imports_ruby_ambiguous_overcap",
        "csharp_namespace_competing_projects",
        "angle_bracket_path_import", "calls", "extractor_file_failed",
        "extractor_file_recovered",
        "extractor_file_incomplete",
        "extractor_file_incomplete_recovered",
        "database_table", "database_column", "config",
        "config_node_id_collision",
        "regular_file_becomes_symlink",
        "gitattributes_linguist_exclusions",
        "gitattributes_rule_change", "gitattributes_added",
        "gitattributes_removed",
        "gitattributes_root_carveout", "gitattributes_nested_carveout",
        "gitattributes_legacy_cg3_baseline",
        "semantic_ref_v0_baseline",
        "database_duplicate_definition_consumer",
        "database_ambiguous_to_unique", "config_definition_removal",
        "terraform", "kubernetes", "kubernetes_definition_removal",
        "graphql", "graphql_definition_catalog_truncated",
        "protobuf", "openapi", "routes",
        "github_actions_package_scripts", "tauri", "tauri_add_first_pair",
        "celery", "celery_last_reference_removal", "celery_add_first_pair",
        "bullmq", "bullmq_last_reference_removal", "bullmq_add_first_pair",
        "sibling_stem", "role_feature",
    }
    if {case.name for case in cases} != expected_names:
        print("GRAPH FULL/INCREMENTAL EQUIVALENCE GATE: FAIL")
        print("fixture inventory drift:", sorted(case.name for case in cases))
        return 1

    bootstrap = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if bootstrap.returncode != 0:
        print("GRAPH FULL/INCREMENTAL EQUIVALENCE GATE: FAIL")
        print("bootstrap failed:", bootstrap.stderr[-1200:])
        return 1

    db = DBSession("veripsa_app")
    admin = DBSession("veripsa_migrator")
    failures: list[str] = []
    matrix: list[dict] = []
    exercised_node_kinds: set[str] = set()
    exercised_edge_kinds: set[str] = set()
    try:
        static_inventory = C.schema_inventory_from_paths(
            os.path.join(ROOT, "db/schema/20_core.sql"),
            os.path.join(ROOT, "db/schema/30_gate.sql"),
            os.path.join(ROOT, "db/schema/70_social.sql"),
        )
        if not static_inventory.ok:
            failures.append(
                "extractor/constraint/full-writer/patch-writer/adjacency "
                f"schema contract drift: {static_inventory.errors!r}"
            )
        for stage, kinds in static_inventory.acceptance.node_stages.items():
            if kinds != C.EXTRACTOR_NODE_KINDS:
                failures.append(
                    f"{stage} Node allowlist differs from extractor contract: "
                    + _bounded_diff(set(kinds), set(C.EXTRACTOR_NODE_KINDS))
                )
        for stage, kinds in static_inventory.acceptance.edge_stages.items():
            if kinds != C.EXTRACTOR_EDGE_KINDS:
                failures.append(
                    f"{stage} Edge allowlist differs from extractor contract: "
                    + _bounded_diff(set(kinds), set(C.EXTRACTOR_EDGE_KINDS))
                )

        with open(I.__file__, encoding="utf-8") as ingest_source:
            ingest_tree = ast.parse(
                ingest_source.read(),
                filename=I.__file__,
            )
        unsafe_without_reason_code = sorted(
            node.lineno
            for node in ast.walk(ingest_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_IncrementalUnsafe"
            and not any(
                keyword.arg == "reason_code"
                for keyword in node.keywords
            )
        )
        if unsafe_without_reason_code:
            failures.append(
                "_IncrementalUnsafe calls omit required reason_code at lines "
                f"{unsafe_without_reason_code!r}"
            )

        schema_inventory = _json_value(
            admin("SELECT core.graph_schema_inventory()::text"), {}
        )
        inventory_set_contracts = {
            "extractor_node_kinds": set(C.EXTRACTOR_NODE_KINDS),
            "extractor_edge_kinds": set(C.EXTRACTOR_EDGE_KINDS),
            "db_node_kinds": set(C.PERSISTED_NODE_KINDS),
            "db_edge_kinds": set(C.PERSISTED_EDGE_KINDS),
            "effective_adjacency_node_kinds": set(
                C.EFFECTIVE_ADJACENCY_NODE_KINDS
            ),
            "effective_adjacency_edge_kinds": set(
                C.EFFECTIVE_ADJACENCY_EDGE_KINDS
            ),
            "evidence_only_edge_kinds": set(C.EVIDENCE_ONLY_EDGE_KINDS),
            "observability_substrates": (
                set(C.SUBSTRATE_CONTRACTS)
                | {"ambiguous", "unresolved"}
            ),
        }
        for inventory_key, expected_values in inventory_set_contracts.items():
            actual_values = set(schema_inventory.get(inventory_key, []))
            if actual_values != expected_values:
                failures.append(
                    f"DB inventory {inventory_key} differs from Python "
                    "schema contract: "
                    + _bounded_diff(actual_values, expected_values)
                )
        db_fallback_codes = set(
            schema_inventory.get("observability_fallback_reason_codes", [])
        )
        if db_fallback_codes != I._FALLBACK_FULL_REBUILD_REASON_CODES:
            failures.append(
                "producer/DB fallback reason-code contracts differ: "
                + _bounded_diff(
                    set(I._FALLBACK_FULL_REBUILD_REASON_CODES),
                    db_fallback_codes,
                )
            )

        for index, case in enumerate(cases):
            patch_repo = f"equivalence/{index:02d}-{case.name}-patch"
            truth_repo = f"equivalence/{index:02d}-{case.name}-truth"

            end_graph = _extract_graph(
                case.end,
                case.end_symlinks,
                case.failed_extractor_path,
                case.incomplete_extractor_path,
            )
            exercised_node_kinds.update(
                str(node.get("kind")) for node in end_graph.get("nodes", [])
            )
            exercised_edge_kinds.update(
                str(edge.get("kind")) for edge in end_graph.get("edges", [])
            )
            extracted_edges = {
                (e.get("src"), e.get("dst"), e.get("kind"))
                for e in end_graph.get("edges", [])
            }
            extracted_edge_records = {
                (e.get("src"), e.get("dst"), e.get("kind")): e
                for e in end_graph.get("edges", [])
            }
            extracted_node_paths = {
                str(node.get("path"))
                for node in end_graph.get("nodes", [])
                if node.get("path")
            }
            extracted_attr_nodes = {
                str(node.get("path"))
                for node in end_graph.get("nodes", [])
                if node.get("kind") == "config_file"
                and node.get("language") == "gitattributes"
                and node.get("path")
            }
            if case.expected_absent_paths and (
                not case.base.get(".gitattributes")
                or case.base.get(".gitattributes")
                != case.end.get(".gitattributes")
            ):
                failures.append(
                    f"{case.name}: exclusion fixture must keep .gitattributes "
                    "present and byte-identical across the incremental change"
                )
            for absent_path in case.expected_absent_paths:
                if (
                    absent_path not in case.base
                    or absent_path not in case.end
                    or absent_path not in case.changed
                ):
                    failures.append(
                        f"{case.name}: excluded-path fixture is not a real "
                        f"changed repository path: {absent_path!r}"
                    )
                    continue
                if absent_path in extracted_node_paths:
                    failures.append(
                        f"{case.name}: full end-state unexpectedly extracted "
                        f".gitattributes-excluded path {absent_path!r}"
                    )
            for absent_path in case.expected_end_absent_paths:
                if absent_path in extracted_node_paths:
                    failures.append(
                        f"{case.name}: full end-state unexpectedly extracted "
                        f"path expected absent: {absent_path!r}"
                    )
                if (
                    absent_path in case.base
                    and absent_path not in case.end
                    and absent_path not in case.removed
                ):
                    failures.append(
                        f"{case.name}: end-absent repository path is not "
                        f"declared removed: {absent_path!r}"
                    )
            for absent_path in case.expected_baseline_absent_paths:
                if absent_path not in case.end:
                    failures.append(
                        f"{case.name}: baseline-only absence is not represented "
                        f"in the end repository: {absent_path!r}"
                    )
            end_symlink_paths = {path for path, _target in case.end_symlinks}
            for absent_path in case.expected_nonregular_absent_paths:
                if (
                    absent_path not in case.base
                    or absent_path not in case.end
                    or absent_path not in case.changed
                    or absent_path not in end_symlink_paths
                ):
                    failures.append(
                        f"{case.name}: non-regular fixture is not a real "
                        f"regular-to-symlink changed path: {absent_path!r}"
                    )
                if absent_path in extracted_node_paths:
                    failures.append(
                        f"{case.name}: full end-state extracted non-regular "
                        f"path {absent_path!r}"
                    )
            for present_path in case.expected_present_paths:
                if present_path not in extracted_node_paths:
                    failures.append(
                        f"{case.name}: full end-state did not extract required "
                        f"context path {present_path!r}"
                    )
                if (
                    os.path.basename(present_path) == ".gitattributes"
                    and present_path not in extracted_attr_nodes
                ):
                    failures.append(
                        f"{case.name}: {present_path!r} was not extracted as a "
                        "gitattributes config_file node"
                    )
            if (
                case.expected_input_file_count is not None
                and end_graph.get("metrics", {}).get("input_file_count")
                != case.expected_input_file_count
            ):
                failures.append(
                    f"{case.name}: offline input_file_count="
                    f"{end_graph.get('metrics', {}).get('input_file_count')!r}, "
                    f"expected {case.expected_input_file_count!r}"
                )
            if (
                case.expected_edge is not None
                and case.expected_edge not in extracted_edges
            ):
                failures.append(
                    f"{case.name}: fixture did not emit {case.expected_edge!r}; "
                    f"nearby={sorted(e for e in extracted_edges if e[2] == case.expected_edge[2])[:8]!r}"
                )
                continue
            for expected_ambiguous_edge in case.expected_ambiguous_edges:
                edge = extracted_edge_records.get(expected_ambiguous_edge)
                if (
                    edge is None
                    or edge.get("reference_status") != "ambiguous"
                ):
                    failures.append(
                        f"{case.name}: expected ambiguous Edge evidence "
                        f"{expected_ambiguous_edge!r}, got {edge!r}"
                    )
            for expected_unresolved_edge in case.expected_unresolved_edges:
                edge = extracted_edge_records.get(expected_unresolved_edge)
                if (
                    edge is None
                    or edge.get("reference_status") != "unresolved"
                ):
                    failures.append(
                        f"{case.name}: expected unresolved Edge evidence "
                        f"{expected_unresolved_edge!r}, got {edge!r}"
                    )
            for ambiguous_path in case.expected_ambiguous_paths:
                ambiguous_nodes = [
                    node
                    for node in end_graph.get("nodes", [])
                    if (
                        node.get("kind") in {"file", "config_file"}
                        and node.get("path") == ambiguous_path
                    )
                ]
                if not ambiguous_nodes or any(
                    node.get("analysis_status") != "ambiguous"
                    for node in ambiguous_nodes
                ):
                    failures.append(
                        f"{case.name}: ambiguous endpoint {ambiguous_path!r} "
                        "was not promoted on every document node: "
                        f"{ambiguous_nodes!r}"
                    )
            for incomplete_path in case.expected_incomplete_paths:
                incomplete_nodes = [
                    node
                    for node in end_graph.get("nodes", [])
                    if (
                        node.get("kind") in {"file", "config_file"}
                        and node.get("path") == incomplete_path
                    )
                ]
                if not incomplete_nodes or any(
                    node.get("analysis_status") != "incomplete"
                    for node in incomplete_nodes
                ):
                    failures.append(
                        f"{case.name}: dedicated-pass incomplete endpoint "
                        f"{incomplete_path!r} was not promoted on every "
                        f"document node: {incomplete_nodes!r}"
                    )
            if case.failed_extractor_path is not None:
                failed_nodes = [
                    node
                    for node in end_graph.get("nodes", [])
                    if (
                        node.get("kind") == "file"
                        and node.get("path") == case.failed_extractor_path
                    )
                ]
                if (
                    len(failed_nodes) != 1
                    or failed_nodes[0].get("analysis_status") != "failed"
                    or end_graph.get("files_failed") != 1
                ):
                    failures.append(
                        f"{case.name}: forced extractor failure did not retain "
                        f"one failed document fact: nodes={failed_nodes!r}, "
                        f"files_failed={end_graph.get('files_failed')!r}"
                    )
            if case.incomplete_extractor_path is not None:
                incomplete_nodes = [
                    node
                    for node in end_graph.get("nodes", [])
                    if (
                        node.get("kind") in {"file", "config_file"}
                        and node.get("path")
                        == case.incomplete_extractor_path
                    )
                ]
                if (
                    len(incomplete_nodes) != 1
                    or incomplete_nodes[0].get("analysis_status")
                    != "incomplete"
                ):
                    failures.append(
                        f"{case.name}: forced incomplete parser did not retain "
                        f"one incomplete document fact: "
                        f"{incomplete_nodes!r}"
                    )
            if (
                case.expected_ambiguous_edges
                and end_graph.get("metrics", {}).get(
                    "ambiguous_reference_count", 0
                ) <= 0
            ):
                failures.append(
                    f"{case.name}: ambiguous evidence was not reflected in "
                    "ambiguous_reference_count"
                )
            if (
                case.expected_absent_edge is not None
                and case.expected_absent_edge in extracted_edges
            ):
                failures.append(
                    f"{case.name}: end-state fixture still emits stale Edge "
                    f"{case.expected_absent_edge!r}"
                )
                continue
            if case.expected_absent_edge is not None:
                base_graph = _extract_graph(case.base)
                base_edges = {
                    (e.get("src"), e.get("dst"), e.get("kind"))
                    for e in base_graph.get("edges", [])
                }
                if case.expected_absent_edge not in base_edges:
                    failures.append(
                        f"{case.name}: baseline fixture never emitted stale Edge "
                        f"{case.expected_absent_edge!r}"
                    )
                    continue

            if case.baseline_extractor_version:
                baseline_graph = _extract_graph(
                    case.base,
                    failed_extractor_path=case.baseline_failed_extractor_path,
                    incomplete_extractor_path=(
                        case.baseline_incomplete_extractor_path
                    ),
                )
                dropped_paths = set(case.baseline_drop_paths)
                baseline_graph["nodes"] = [
                    node for node in baseline_graph.get("nodes", [])
                    if node.get("path") not in dropped_paths
                ]
                baseline_graph["edges"] = [
                    edge for edge in baseline_graph.get("edges", [])
                    if str(edge.get("src") or "").split("::", 1)[0]
                    not in dropped_paths
                ]
                baseline_graph["extractor_version"] = (
                    case.baseline_extractor_version
                )
                baseline_graph["metrics"] = {
                    "schema_contract_version": 1,
                }
                baseline_write = db(
                    "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s,%s)",
                    (
                        json.dumps(baseline_graph),
                        patch_repo,
                        BRANCH,
                        BASE_SHA,
                        BASE_TIME,
                    ),
                )
                I._validated_graph_write(
                    db, baseline_write, patch_repo, BRANCH, BASE_SHA, "full"
                )
                db.commit()
            else:
                _write_graph_full(
                    db,
                    case.base,
                    patch_repo,
                    BASE_SHA,
                    BASE_TIME,
                    failed_extractor_path=(
                        case.baseline_failed_extractor_path
                    ),
                    incomplete_extractor_path=(
                        case.baseline_incomplete_extractor_path
                    ),
                )
            if case.baseline_semantic_ref_version is not None:
                _downgrade_semantic_refs(admin, patch_repo)
            baseline_paths = set(I._coordinate_paths(db, patch_repo, BRANCH))
            baseline_version = _version(admin, patch_repo)
            if (
                case.expected_baseline_uncertainty is not None
                and baseline_version.get("has_graph_uncertainty")
                is not case.expected_baseline_uncertainty
            ):
                failures.append(
                    f"{case.name}: baseline has_graph_uncertainty="
                    f"{baseline_version.get('has_graph_uncertainty')!r}, "
                    f"expected {case.expected_baseline_uncertainty!r}"
                )
            if case.baseline_extractor_version and (
                baseline_version.get("extractor_version")
                != case.baseline_extractor_version
                or baseline_version.get("current_extractor_version")
                != X.EXTRACTOR_VERSION
            ):
                failures.append(
                    f"{case.name}: upgrade precondition is not a stale "
                    f"{case.baseline_extractor_version} coordinate under "
                    f"current {X.EXTRACTOR_VERSION}: {baseline_version!r}"
                )
            if (
                case.baseline_semantic_ref_version is not None
                and baseline_version.get("semantic_ref_version")
                != case.baseline_semantic_ref_version
            ):
                failures.append(
                    f"{case.name}: semantic migration precondition is not "
                    f"v{case.baseline_semantic_ref_version}: "
                    f"{baseline_version!r}"
                )
            for absent_path in case.expected_absent_paths:
                if absent_path in baseline_paths:
                    failures.append(
                        f"{case.name}: excluded path unexpectedly entered the "
                        f"persisted baseline universe: {absent_path!r}"
                    )
            for absent_path in case.expected_baseline_absent_paths:
                if absent_path in baseline_paths:
                    failures.append(
                        f"{case.name}: path expected to be excluded only in "
                        f"the baseline entered its universe: {absent_path!r}"
                    )
            for present_path in case.expected_baseline_present_paths:
                if present_path not in baseline_paths:
                    failures.append(
                        f"{case.name}: path required in the persisted baseline "
                        f"universe is missing: {present_path!r}"
                    )
            for present_path in case.expected_present_paths:
                if (
                    present_path not in case.expected_baseline_absent_paths
                    and present_path not in baseline_paths
                ):
                    failures.append(
                        f"{case.name}: required context path missing from the "
                        f"persisted baseline universe: {present_path!r}"
                    )
            _write_graph_full(
                db, case.end, truth_repo, END_SHA, END_TIME,
                case.end_symlinks,
                case.failed_extractor_path,
                case.incomplete_extractor_path,
            )
            patch_gh = FakeGH(case.end, case.end_symlinks)
            with _forced_python_extractor_failure(
                case.failed_extractor_path
            ):
                with _forced_incomplete_document(
                    case.incomplete_extractor_path
                ):
                    result = I._reingest_graph(
                        db=db,
                        gh=patch_gh,
                        repo=patch_repo,
                        branch=BRANCH,
                        sha=END_SHA,
                        payload={},
                        decision="incremental",
                        changed=list(case.changed),
                        removed=list(case.removed),
                        head_time=END_TIME,
                        changed_set_base_sha=BASE_SHA,
                    )
            db.commit()

            patch_nodes, patch_edges, patch_semantic_edges = _stored_graph(
                admin, patch_repo
            )
            truth_nodes, truth_edges, truth_semantic_edges = _stored_graph(
                admin, truth_repo
            )
            patch_reference_statuses = _stored_reference_statuses(
                admin, patch_repo
            )
            truth_reference_statuses = _stored_reference_statuses(
                admin, truth_repo
            )
            patch_node_paths = {
                str(json.loads(node).get("path"))
                for node in patch_nodes
                if json.loads(node).get("path")
            }
            patch_version = _version(admin, patch_repo)
            truth_version = _version(admin, truth_repo)
            patch_document_nodes = [
                json.loads(node)
                for node in patch_nodes
                if json.loads(node).get("node_kind")
                in {"file", "config_file"}
            ]
            truth_document_nodes = [
                json.loads(node)
                for node in truth_nodes
                if json.loads(node).get("node_kind")
                in {"file", "config_file"}
            ]
            for expected_path, expected_status in (
                *((path, "ambiguous")
                  for path in case.expected_ambiguous_paths),
                *((path, "incomplete")
                  for path in case.expected_incomplete_paths),
            ):
                for graph_label, document_nodes in (
                    ("patch", patch_document_nodes),
                    ("truth", truth_document_nodes),
                ):
                    statuses = {
                        node.get("analysis_status")
                        for node in document_nodes
                        if node.get("path") == expected_path
                    }
                    if statuses != {expected_status}:
                        failures.append(
                            f"{case.name}: persisted {graph_label} document "
                            f"{expected_path!r} has statuses "
                            f"{sorted(statuses, key=str)!r}, expected "
                            f"{expected_status!r}"
                        )
            if (
                case.expected_final_uncertainty is not None
                and (
                    patch_version.get("has_graph_uncertainty")
                    is not case.expected_final_uncertainty
                    or truth_version.get("has_graph_uncertainty")
                    is not case.expected_final_uncertainty
                )
            ):
                failures.append(
                    f"{case.name}: final uncertainty mismatch "
                    f"patch={patch_version.get('has_graph_uncertainty')!r} "
                    f"truth={truth_version.get('has_graph_uncertainty')!r} "
                    f"expected={case.expected_final_uncertainty!r}"
                )
            if patch_reference_statuses != truth_reference_statuses:
                failures.append(
                    f"{case.name}: persisted reference statuses differ: "
                    f"patch={patch_reference_statuses!r} "
                    f"truth={truth_reference_statuses!r}"
                )
            for edge_identity, expected_status in (
                *((edge, "ambiguous")
                  for edge in case.expected_ambiguous_edges),
                *((edge, "unresolved")
                  for edge in case.expected_unresolved_edges),
            ):
                for graph_label, statuses in (
                    ("patch", patch_reference_statuses),
                    ("truth", truth_reference_statuses),
                ):
                    if statuses.get(edge_identity) != expected_status:
                        failures.append(
                            f"{case.name}: persisted {graph_label} Edge "
                            f"{edge_identity!r} has reference_status="
                            f"{statuses.get(edge_identity)!r}, expected "
                            f"{expected_status!r}"
                        )

            mode = result.get("mode")
            graph_hash = patch_version.get("graph_hash")
            truth_hash = truth_version.get("graph_hash")
            row = {
                "substrate": case.name,
                "mode": mode,
                "nodes": len(patch_nodes),
                "edges": len(patch_edges),
                "hash": graph_hash,
                "expected_edge": case.expected_edge,
                "expected_absent_edge": case.expected_absent_edge,
                "expected_absent_paths": case.expected_absent_paths,
                "expected_present_paths": case.expected_present_paths,
                "expected_nonregular_absent_paths": (
                    case.expected_nonregular_absent_paths
                ),
            }
            matrix.append(row)

            if mode != case.expected_mode:
                failures.append(
                    f"{case.name}: mode={mode!r}, expected {case.expected_mode!r}; "
                    f"fallback_reasons={result.get('fallback_reasons')!r}"
                )
            expected_tarball_calls = 1 if case.expected_mode == "full" else 0
            if patch_gh.download_tarball_calls != expected_tarball_calls:
                failures.append(
                    f"{case.name}: target tarball calls="
                    f"{patch_gh.download_tarball_calls}, expected "
                    f"{expected_tarball_calls} for mode "
                    f"{case.expected_mode!r}"
                )
            if case.expected_mode == "full" and not result.get("fallback_reasons"):
                failures.append(
                    f"{case.name}: full fallback has no observable reason"
                )
            if case.expected_fallback_fragment and not any(
                case.expected_fallback_fragment in str(reason)
                for reason in (result.get("fallback_reasons") or [])
            ):
                failures.append(
                    f"{case.name}: expected fallback reason fragment "
                    f"{case.expected_fallback_fragment!r}, got "
                    f"{result.get('fallback_reasons')!r}"
                )
            if (
                case.expected_fallback_code
                and case.expected_fallback_code
                not in (result.get("fallback_reason_codes") or [])
            ):
                failures.append(
                    f"{case.name}: expected fallback code "
                    f"{case.expected_fallback_code!r}, got "
                    f"{result.get('fallback_reason_codes')!r}"
                )
            if patch_nodes != truth_nodes:
                failures.append(
                    f"{case.name}: persisted Node sets differ: "
                    + _bounded_diff(patch_nodes, truth_nodes)
                )
            if patch_edges != truth_edges:
                failures.append(
                    f"{case.name}: persisted Edge sets differ: "
                    + _bounded_diff(patch_edges, truth_edges)
                )
            if patch_semantic_edges != truth_semantic_edges:
                failures.append(
                    f"{case.name}: persisted semantic Edge sets differ: "
                    + _bounded_diff(
                        patch_semantic_edges, truth_semantic_edges
                    )
                )
            if (
                case.expected_edge is not None
                and case.expected_edge not in patch_edges
                and (
                    case.expected_edge[0],
                    I._semantic_ref_key(case.expected_edge[1]),
                    case.expected_edge[2],
                ) not in patch_semantic_edges
            ):
                failures.append(
                    f"{case.name}: concrete persisted Edge missing: "
                    f"{case.expected_edge!r}"
                )
            if (
                case.expected_absent_edge is not None
                and (
                    case.expected_absent_edge in patch_edges
                    or (
                        case.expected_absent_edge[0],
                        I._semantic_ref_key(
                            case.expected_absent_edge[1]
                        ),
                        case.expected_absent_edge[2],
                    ) in patch_semantic_edges
                )
            ):
                failures.append(
                    f"{case.name}: removed reference survived incremental update: "
                    f"{case.expected_absent_edge!r}"
                )
            for absent_path in case.expected_absent_paths:
                if absent_path in patch_node_paths:
                    failures.append(
                        f"{case.name}: incremental decision inserted "
                        f".gitattributes-excluded path {absent_path!r}"
                    )
            for absent_path in case.expected_end_absent_paths:
                if absent_path in patch_node_paths:
                    failures.append(
                        f"{case.name}: incremental decision retained "
                        f"end-absent path {absent_path!r}"
                    )
            for present_path in case.expected_present_paths:
                if present_path not in patch_node_paths:
                    failures.append(
                        f"{case.name}: required context path missing after "
                        f"incremental decision: {present_path!r}"
                    )
            for absent_path in case.expected_nonregular_absent_paths:
                if absent_path in patch_node_paths:
                    failures.append(
                        f"{case.name}: incremental decision retained "
                        f"non-regular path {absent_path!r}"
                    )
            if not graph_hash or graph_hash != truth_hash:
                failures.append(
                    f"{case.name}: graph_hash mismatch/empty: "
                    f"patch={graph_hash!r} truth={truth_hash!r}"
                )
            if patch_version.get("commit_sha") != END_SHA:
                failures.append(
                    f"{case.name}: patch coordinate did not advance to end SHA"
                )
            if patch_version.get("extractor_version") != X.EXTRACTOR_VERSION:
                failures.append(
                    f"{case.name}: final coordinate extractor_version="
                    f"{patch_version.get('extractor_version')!r}, expected "
                    f"{X.EXTRACTOR_VERSION!r}"
                )
            if (
                patch_version.get("semantic_ref_version")
                != C.SEMANTIC_REF_VERSION
                or patch_version.get("current_semantic_ref_version")
                != C.SEMANTIC_REF_VERSION
            ):
                failures.append(
                    f"{case.name}: semantic reference version mismatch: "
                    f"{patch_version.get('semantic_ref_version')!r}/"
                    f"{patch_version.get('current_semantic_ref_version')!r}"
                )
            if (
                patch_version.get("node_count") != len(patch_nodes)
                or patch_version.get("edge_count") != len(patch_edges)
            ):
                failures.append(
                    f"{case.name}: graph_version counts disagree with stored rows: "
                    f"version=({patch_version.get('node_count')},"
                    f"{patch_version.get('edge_count')}) "
                    f"rows=({len(patch_nodes)},{len(patch_edges)})"
                )
            observability = patch_version.get("observability")
            if not isinstance(observability, dict):
                failures.append(
                    f"{case.name}: graph_version.observability is NULL"
                )
            else:
                required_observability = {
                    "input_file_count", "nodes_by_substrate",
                    "edges_by_substrate", "unresolved_reference_count",
                    "ambiguous_reference_count",
                    "fallback_full_rebuild_reasons", "extraction_graph_hash",
                    "persisted_graph_hash", "persistence",
                }
                missing_observability = required_observability - set(observability)
                if missing_observability:
                    failures.append(
                        f"{case.name}: observability keys missing: "
                        f"{sorted(missing_observability)!r}"
                    )
                fallback_codes = observability.get(
                    "fallback_full_rebuild_reasons"
                )
                if not isinstance(fallback_codes, list):
                    failures.append(
                        f"{case.name}: durable fallback reasons are not a list: "
                        f"{fallback_codes!r}"
                    )
                else:
                    unknown_fallback_codes = (
                        set(fallback_codes)
                        - I._FALLBACK_FULL_REBUILD_REASON_CODES
                    )
                    if unknown_fallback_codes:
                        failures.append(
                            f"{case.name}: durable fallback reasons contain "
                            f"unbounded values: {sorted(unknown_fallback_codes)!r}"
                        )
                    if (
                        case.expected_fallback_code
                        and case.expected_fallback_code not in fallback_codes
                    ):
                        failures.append(
                            f"{case.name}: graph_version observability omitted "
                            f"fallback code {case.expected_fallback_code!r}: "
                            f"{fallback_codes!r}"
                        )
                    if (
                        case.expected_mode == "patch"
                        and fallback_codes
                    ):
                        failures.append(
                            f"{case.name}: patch persisted unexpected fallback "
                            f"codes: {fallback_codes!r}"
                        )
                exclusions = (
                    observability.get("persistence", {})
                    .get("exclusions", {})
                )
                excluded_nodes = exclusions.get("nodes", {}).get("count")
                excluded_edges = exclusions.get("edges", {}).get("count")
                if excluded_nodes != 0 or excluded_edges != 0:
                    failures.append(
                        f"{case.name}: persistence exclusions are non-zero: "
                        f"{exclusions!r}"
                    )
                if observability.get("persisted_graph_hash") != graph_hash:
                    failures.append(
                        f"{case.name}: observability persisted_graph_hash "
                        f"does not match graph_version: "
                        f"{observability.get('persisted_graph_hash')!r} != "
                        f"{graph_hash!r}"
                    )
                if (
                    case.expected_input_file_count is not None
                    and observability.get("input_file_count")
                    != (
                        len(set(case.changed))
                        if case.expected_mode == "patch"
                        else case.expected_input_file_count
                    )
                ):
                    expected_observed_input_count = (
                        len(set(case.changed))
                        if case.expected_mode == "patch"
                        else case.expected_input_file_count
                    )
                    failures.append(
                        f"{case.name}: persisted input_file_count="
                        f"{observability.get('input_file_count')!r}, expected "
                        f"{expected_observed_input_count!r}"
                    )
                if (
                    case.expected_resolution_context_file_count is not None
                    and observability.get("resolution_context_file_count")
                    != case.expected_resolution_context_file_count
                ):
                    failures.append(
                        f"{case.name}: persisted resolution context count="
                        f"{observability.get('resolution_context_file_count')!r}, "
                        f"expected "
                        f"{case.expected_resolution_context_file_count!r}"
                    )

        # A complete path list is meaningful only relative to the exact commit
        # it was diffed from.  Reproduce a missed-push state: changed paths
        # describe Q→END, while the persisted graph is still P.  A P-based
        # patch would silently omit P→Q; the selector must rebuild END in full.
        mismatch_case = cases[0]
        mismatch_repo = "equiv/changed-set-base-mismatch"
        mismatch_truth_repo = "equiv/changed-set-base-mismatch-truth"
        stored_p_sha = "8" * 40
        changed_set_q_sha = "9" * 40
        _write_graph_full(
            db, mismatch_case.base, mismatch_repo, stored_p_sha, BASE_TIME
        )
        _write_graph_full(
            db, mismatch_case.end, mismatch_truth_repo, END_SHA, END_TIME,
            mismatch_case.end_symlinks,
        )
        mismatch_gh = FakeGH(
            mismatch_case.end, mismatch_case.end_symlinks
        )
        mismatch_result = I._reingest_graph(
            db=db,
            gh=mismatch_gh,
            repo=mismatch_repo,
            branch=BRANCH,
            sha=END_SHA,
            payload={},
            decision="incremental",
            changed=list(mismatch_case.changed),
            removed=list(mismatch_case.removed),
            head_time=END_TIME,
            changed_set_base_sha=changed_set_q_sha,
        )
        db.commit()
        mismatch_nodes, mismatch_edges, mismatch_semantic_edges = _stored_graph(
            admin, mismatch_repo
        )
        (
            mismatch_truth_nodes,
            mismatch_truth_edges,
            mismatch_truth_semantic_edges,
        ) = _stored_graph(
            admin, mismatch_truth_repo
        )
        mismatch_version = _version(admin, mismatch_repo)
        mismatch_truth_version = _version(admin, mismatch_truth_repo)
        if not (
            mismatch_result.get("mode") == "full"
            and "changed_set_base_mismatch"
            in (mismatch_result.get("fallback_reason_codes") or [])
            and mismatch_gh.download_tarball_calls == 1
            and mismatch_nodes == mismatch_truth_nodes
            and mismatch_edges == mismatch_truth_edges
            and mismatch_semantic_edges == mismatch_truth_semantic_edges
            and mismatch_version.get("graph_hash")
            == mismatch_truth_version.get("graph_hash")
            and mismatch_version.get("commit_sha") == END_SHA
            and "changed_set_base_mismatch"
            in (
                (mismatch_version.get("observability") or {}).get(
                    "fallback_full_rebuild_reasons"
                )
                or []
            )
        ):
            failures.append(
                "changed-set base mismatch did not force an exact full graph: "
                f"result={mismatch_result!r}, "
                f"hash={mismatch_version.get('graph_hash')!r}, "
                f"truth={mismatch_truth_version.get('graph_hash')!r}"
            )

        compare_repo = "equiv/compare-history-unproven"
        _write_graph_full(
            db, mismatch_case.base, compare_repo, BASE_SHA, BASE_TIME
        )
        compare_gh = FakeGH(
            mismatch_case.end, mismatch_case.end_symlinks
        )
        compare_result = I._reingest_graph(
            db=db,
            gh=compare_gh,
            repo=compare_repo,
            branch=BRANCH,
            sha=END_SHA,
            payload=None,
            decision="incremental",
            changed=[],
            removed=[],
            head_time=END_TIME,
            changed_set_complete=False,
            changed_set_failure_code="compare_history_unproven",
        )
        db.commit()
        compare_nodes, compare_edges, compare_semantic_edges = _stored_graph(
            admin, compare_repo
        )
        compare_version = _version(admin, compare_repo)
        if not (
            compare_result.get("mode") == "full"
            and compare_gh.download_tarball_calls == 1
            and compare_nodes == mismatch_truth_nodes
            and compare_edges == mismatch_truth_edges
            and compare_semantic_edges == mismatch_truth_semantic_edges
            and compare_version.get("graph_hash")
            == mismatch_truth_version.get("graph_hash")
            and "compare_history_unproven"
            in (
                (compare_version.get("observability") or {}).get(
                    "fallback_full_rebuild_reasons"
                )
                or []
            )
        ):
            failures.append(
                "unproven Compare history was not persisted as a reasoned "
                f"exact full rebuild: result={compare_result!r}, "
                f"version={compare_version!r}"
            )

        if exercised_node_kinds != C.EXTRACTOR_NODE_KINDS:
            failures.append(
                "real equivalence fixtures do not exercise exactly the extractor "
                "Node-kind contract: "
                + _bounded_diff(
                    exercised_node_kinds, set(C.EXTRACTOR_NODE_KINDS)
                )
            )
        if exercised_edge_kinds != C.EXTRACTOR_EDGE_KINDS:
            failures.append(
                "real equivalence fixtures do not exercise exactly the extractor "
                "Edge-kind contract: "
                + _bounded_diff(
                    exercised_edge_kinds, set(C.EXTRACTOR_EDGE_KINDS)
                )
            )

        patched_resource_cases = {
            row["substrate"]
            for row in matrix
            if row["mode"] == "patch"
            and next(
                c.resource_consumer_patch
                for c in cases
                if c.name == row["substrate"]
            )
        }
        if not patched_resource_cases:
            failures.append(
                "no consumer-only first-class resource case used incremental patch"
            )

        print("Full vs Incremental Test Matrix")
        print("substrate                         mode   nodes edges graph_hash")
        for row in matrix:
            print(
                f"{row['substrate']:<33} {str(row['mode']):<6} "
                f"{row['nodes']:>5} {row['edges']:>5} {row['hash']}"
            )
            if row["expected_edge"] is not None:
                print(f"  concrete Edge: {row['expected_edge']!r}")
            if row["expected_absent_edge"] is not None:
                print(
                    "  absent stale Edge: "
                    f"{row['expected_absent_edge']!r}"
                )
            for absent_path in row["expected_absent_paths"]:
                print(f"  absent excluded path: {absent_path!r}")
            for present_path in row["expected_present_paths"]:
                print(f"  retained context path: {present_path!r}")
            for absent_path in row["expected_nonregular_absent_paths"]:
                print(f"  absent non-regular path: {absent_path!r}")
        print(
            "consumer-only resource patches:",
            ", ".join(sorted(patched_resource_cases)) or "(none)",
        )
    except Exception as exc:
        db.conn.rollback()
        failures.append(
            f"unhandled test error: {type(exc).__name__}: {exc}\n"
            + traceback.format_exc()
        )
    finally:
        admin.close()
        db.close()

    if failures:
        print("GRAPH FULL/INCREMENTAL EQUIVALENCE GATE: FAIL")
        for failure in failures:
            print(" -", failure)
        return 1
    print("GRAPH FULL/INCREMENTAL EQUIVALENCE GATE: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
