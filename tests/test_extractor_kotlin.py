"""Gate 98: Kotlin extractor smoke test.

Verifies that .kt files yield def/class nodes and at least one calls edge — the
two structural signal types a cross-PR collision check depends on. Without this
gate, tree-sitter-kotlin's has_error==True on the (incorrect) inline probe snippet
caused _grammar_passes_probe to reject the grammar and silently produce ≈0
structural edges (a missed collision reads as "clear" — the worst outcome).

No Postgres, no network, no GitHub. Writes a temporary .kt fixture in a tempdir,
runs build_graph, asserts node/edge counts, and exits with KOTLIN GATE: PASS/FAIL.
"""
import sys
import os
import tempfile
import pathlib

# Allow running from the repo root or the tests/ directory.
_HERE = pathlib.Path(__file__).parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

import code_graph_extract as X


_KT_FIXTURE = """\
package example

import kotlin.collections.List

class Repository {
    fun findById(id: Int): String {
        val result = lookup(id)
        return result
    }

    fun lookup(id: Int): String {
        return "item-$id"
    }
}

fun buildRepository(): Repository {
    return Repository()
}
"""


def run():
    fails = []
    with tempfile.TemporaryDirectory() as td:
        kt_path = os.path.join(td, "Repo.kt")
        with open(kt_path, "w") as fh:
            fh.write(_KT_FIXTURE)

        g = X.build_graph(td)

    nodes = g["nodes"]
    edges = g["edges"]

    kt_files = [n for n in nodes if n.get("path", "").endswith(".kt") and n["kind"] == "file"]
    kt_defs = [n for n in nodes if n.get("path", "").endswith(".kt") and n["kind"] in ("def", "class")]
    kt_calls = [e for e in edges if e.get("src", "").endswith(".kt") and e.get("kind") == "calls"]

    # --- assertions -----------------------------------------------------------

    # At least one .kt file node
    if not kt_files:
        fails.append("no .kt file node found — build_graph did not ingest the fixture")

    # Expect class nodes: Repository
    class_names = {n["name"] for n in kt_defs if n["kind"] == "class"}
    if "Repository" not in class_names:
        fails.append(f"class node 'Repository' missing — got class names: {sorted(class_names)}")

    # Expect def nodes: findById, lookup, buildRepository
    def_names = {n["name"] for n in kt_defs if n["kind"] == "def"}
    for expected_fn in ("findById", "lookup", "buildRepository"):
        if expected_fn not in def_names:
            fails.append(f"def node '{expected_fn}' missing — got def names: {sorted(def_names)}")

    # At least one calls edge from the .kt file (e.g. lookup(), Repository())
    if not kt_calls:
        fails.append("no calls edges from .kt file — Kotlin call_expression extraction not working")

    # --- report ---------------------------------------------------------------
    print(f"kt file nodes  : {len(kt_files)}")
    print(f"kt def/class   : {len(kt_defs)}  (defs={sorted(def_names)}, classes={sorted(class_names)})")
    print(f"kt calls edges : {len(kt_calls)}  (sample: {[(e['src'].split('/')[-1], e['dst']) for e in kt_calls[:3]]})")

    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        print("KOTLIN GATE: FAIL")
        sys.exit(1)
    else:
        print("KOTLIN GATE: PASS")


if __name__ == "__main__":
    run()
