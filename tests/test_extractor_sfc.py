"""Gate 110: Svelte and Vue Single-File Component extractor smoke test.

Veripsa routes .svelte files through a tree-sitter-svelte grammar that locates
the script_element -> raw_text node, then delegates JS/TS extraction to
_walk_ts_tree with the correct file-line offset.  Vue .vue files have no pip
wheel for tree-sitter-vue, so extraction uses the shared bounded HTML scanner
to pull executable script blocks and parses them with the TypeScript/JavaScript
parser.

Both extractors produce def nodes, imports edges, and calls edges from script
content that the bare-file-node path (BEFORE the fix) produced zero of.

This test runs offline: no Postgres, no git, no network.  It writes tiny
.svelte and .vue fixtures in a tempdir, runs build_graph, and asserts that:
  - each SFC yields exactly one file node with the correct language label
  - the function defined in the script block becomes a def node
  - the import statement in the script block becomes an imports edge
  - the function call in the script block becomes a calls edge
  - start_line on each def node is the 1-based line in the OUTER file
    (i.e., the line offset is applied correctly)

Prints SFC GATE: PASS or SFC GATE: FAIL.
"""

import sys
import os
import tempfile
import pathlib
import time

_HERE = pathlib.Path(__file__).parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

import code_graph_extract as X
import _cg_languages as L


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Svelte fixture: <script> block starts at line 1 (0-based row 0), so
# the function on the 2nd line of the script block is file line 3.
# Lines (1-based):
#  1: <script lang="ts">
#  2: import { helper } from './utils';
#  3: function greet(name) { return helper(name); }
#  4: </script>
#  5: <p>hello</p>
_SVELTE_FIXTURE = """\
<script lang="ts">
import { helper } from './utils';
function greet(name) { return helper(name); }
</script>
<p>hello</p>
"""

# Vue fixture: both normal and setup scripts are legitimate TOP-LEVEL native
# blocks and must be indexed. Everything after them is presentation-only and
# must stay out of the executable graph even when it contains script-shaped text.
# Lines (1-based):
#  1: <script>
#  2: function optionsApi() { return 1; }
#  3: </script>
#  4: <script setup>
#  5: import { ref } from './reactivity';
#  6: function setup() { ref(); }
#  7: </script>
#  8+: template interpolation, PascalCase component, and custom docs decoys
_VUE_FIXTURE = """\
<script>
function optionsApi() { return 1; }
</script>
<script setup>
import { ref } from './reactivity';
function setup() { ref(); }
</script>
<template>
  <div>hi</div>
  <div>{{ "</template><script>import InterpolationGhost from './interpolation-ghost'; export function interpolationOnly() { InterpolationGhost(); }</script><template>" }}</div>
  <Script>export function nestedPascalCaseOnly() { return 2; }</Script>
</template>
<docs>
  <script>import DocsGhost from './docs-ghost'; export function docsOnly() { DocsGhost(); }</script>
</docs>
<Script>export function topLevelPascalCaseOnly() { return 3; }</Script>
"""


def run():
    fails = []

    def check(cond, desc):
        if not cond:
            fails.append(desc)

    with tempfile.TemporaryDirectory() as td:
        svelte_path = os.path.join(td, "App.svelte")
        vue_path = os.path.join(td, "Comp.vue")
        with open(svelte_path, "w") as fh:
            fh.write(_SVELTE_FIXTURE)
        with open(vue_path, "w") as fh:
            fh.write(_VUE_FIXTURE)
        # Real local targets make any presentation-only import leak resolve into
        # a visible false file-to-file coupling instead of remaining an inert raw edge.
        with open(os.path.join(td, "interpolation-ghost.ts"), "w") as fh:
            fh.write("export default function InterpolationGhost() {}\n")
        with open(os.path.join(td, "docs-ghost.ts"), "w") as fh:
            fh.write("export default function DocsGhost() {}\n")

        g = X.build_graph(td)

    nodes = g["nodes"]
    edges = g["edges"]

    # ---- Svelte ---------------------------------------------------------------
    svelte_file_nodes = [n for n in nodes if n.get("path", "").endswith(".svelte")
                         and n["kind"] == "file"]
    svelte_defs = [n for n in nodes if n.get("path", "").endswith(".svelte")
                   and n["kind"] == "def"]
    svelte_imports = [e for e in edges if e.get("src", "").endswith(".svelte")
                      and e["kind"] == "imports"]
    svelte_calls = [e for e in edges if e.get("src", "").endswith(".svelte")
                    and e["kind"] == "calls"]

    # exactly one file node
    check(len(svelte_file_nodes) == 1,
          f"svelte: expected 1 file node, got {len(svelte_file_nodes)}")
    # language label
    if svelte_file_nodes:
        check(svelte_file_nodes[0].get("language") == "svelte",
              f"svelte: file node language should be 'svelte', got {svelte_file_nodes[0].get('language')!r}")

    # def for 'greet'
    svelte_def_names = {n["name"] for n in svelte_defs}
    check("greet" in svelte_def_names,
          f"svelte: def 'greet' not found, got {sorted(svelte_def_names)}")

    # imports edge to './utils'
    svelte_import_dsts = {e["dst"] for e in svelte_imports}
    check("./utils" in svelte_import_dsts,
          f"svelte: imports edge to './utils' not found, got {sorted(svelte_import_dsts)}")

    # calls edge to 'helper'
    svelte_call_dsts = {e["dst"] for e in svelte_calls}
    check("helper" in svelte_call_dsts,
          f"svelte: calls edge to 'helper' not found, got {sorted(svelte_call_dsts)}")

    # line offset: greet is on file line 3 (script block at line 1, function body at line 3)
    greet_nodes = [n for n in svelte_defs if n.get("name") == "greet"]
    if greet_nodes:
        sl = greet_nodes[0].get("start_line")
        check(sl == 3,
              f"svelte: greet start_line should be 3 (file-absolute), got {sl}")

    # ---- Vue ------------------------------------------------------------------
    vue_file_nodes = [n for n in nodes if n.get("path", "").endswith(".vue")
                      and n["kind"] == "file"]
    vue_defs = [n for n in nodes if n.get("path", "").endswith(".vue")
                and n["kind"] == "def"]
    vue_imports = [e for e in edges if e.get("src", "").endswith(".vue")
                   and e["kind"] == "imports"]
    vue_calls = [e for e in edges if e.get("src", "").endswith(".vue")
                 and e["kind"] == "calls"]

    # exactly one file node
    check(len(vue_file_nodes) == 1,
          f"vue: expected 1 file node, got {len(vue_file_nodes)}")
    # language label
    if vue_file_nodes:
        check(vue_file_nodes[0].get("language") == "vue",
              f"vue: file node language should be 'vue', got {vue_file_nodes[0].get('language')!r}")

    # def for 'setup'
    vue_def_names = {n["name"] for n in vue_defs}
    check("setup" in vue_def_names,
          f"vue: def 'setup' not found, got {sorted(vue_def_names)}")
    check("optionsApi" in vue_def_names,
          f"vue: normal + setup script blocks were not both indexed, got {sorted(vue_def_names)}")
    presentation_only_defs = {
        "interpolationOnly", "nestedPascalCaseOnly", "docsOnly", "topLevelPascalCaseOnly",
    }
    check(not presentation_only_defs & vue_def_names,
          "vue: template/custom/PascalCase presentation content entered executable defs: "
          f"{sorted(presentation_only_defs & vue_def_names)}")

    # imports edge to './reactivity'
    vue_import_dsts = {e["dst"] for e in vue_imports}
    check("./reactivity" in vue_import_dsts,
          f"vue: imports edge to './reactivity' not found, got {sorted(vue_import_dsts)}")
    check("interpolation-ghost.ts" not in vue_import_dsts and "docs-ghost.ts" not in vue_import_dsts,
          "vue: presentation-only imports resolved into false file couplings: "
          f"{sorted(vue_import_dsts)}")

    # Route/OpenAPI contract scanners consume this same executable-region boundary.
    vue_executable_text = L._sfc_executable_text(_VUE_FIXTURE, ".vue")
    check("optionsApi" in vue_executable_text and "setup" in vue_executable_text,
          "vue: contract scanner lost one of the legitimate top-level script blocks")
    check(not any(name in vue_executable_text for name in presentation_only_defs),
          "vue: contract scanner received template/custom/PascalCase presentation content")

    # calls edge to 'ref'
    vue_call_dsts = {e["dst"] for e in vue_calls}
    check("ref" in vue_call_dsts,
          f"vue: calls edge to 'ref' not found, got {sorted(vue_call_dsts)}")

    # line offset: setup is on file line 6 after the preceding normal script block.
    setup_nodes = [n for n in vue_defs if n.get("name") == "setup"]
    if setup_nodes:
        sl = setup_nodes[0].get("start_line")
        check(sl == 6,
              f"vue: setup start_line should be 6 (file-absolute), got {sl}")

    # ---- NEVER-CRASH: malformed / no-script SFCs ------------------------------
    with tempfile.TemporaryDirectory() as td2:
        # Svelte with no script block
        with open(os.path.join(td2, "NoScript.svelte"), "w") as fh:
            fh.write("<p>template only</p>\n")
        # Vue with no script block
        with open(os.path.join(td2, "NoScript.vue"), "w") as fh:
            fh.write("<template><div>template only</div></template>\n")
        # Completely malformed SFC (not even valid HTML)
        with open(os.path.join(td2, "Bad.svelte"), "w") as fh:
            fh.write("not html at all just garbage <<<\n")
        g2 = X.build_graph(td2)

    check(len(g2["nodes"]) >= 3,
          f"never-crash: expected at least 3 file nodes for no-script/malformed SFCs, "
          f"got {len(g2['nodes'])}")
    check(not any(n["kind"] not in ("file",) for n in g2["nodes"] if "svelte" in n.get("path", "")),
          "never-crash: malformed .svelte should yield only a bare file node (no defs/classes)")

    # ---- BOUNDED: top-level filtering must remain linear on large templates ----
    vue_template_craft = (
        "<template>" + "<div data-static='ok'></div>" * 20_000 + "</template>"
        "<script>export function afterLargeTemplate() {}</script>"
    )
    started = time.perf_counter()
    craft_blocks = L._vue_script_blocks(vue_template_craft)
    craft_elapsed = time.perf_counter() - started
    check(any("afterLargeTemplate" in body for body, _line in craft_blocks),
          "vue: top-level scanner lost the real script after a large template")
    check(craft_elapsed < 1.5,
          f"vue: top-level template scan exceeded linear budget: {craft_elapsed:.3f}s")

    # ---- RAW TOP-LEVEL LANGUAGES: '<' is source text, not HTML markup ---------
    vue_raw_top_level = """\
<template lang="pug">
count < limit
script export function pugGhost() {}
</template>
<script setup>
export function afterPug() {}
</script>
<docs lang="md">
a < b
<script>export function docsGhost() {}</script>
</docs>
<script>
export function afterDocs() {}
</script>
"""
    raw_blocks = L._vue_script_blocks(vue_raw_top_level)
    raw_bodies = [body for body, _line in raw_blocks]
    raw_offsets = [line for _body, line in raw_blocks]
    check(len(raw_blocks) == 2,
          f"vue: preprocessed/custom raw blocks changed executable block count: {len(raw_blocks)}")
    check(any("afterPug" in body for body in raw_bodies),
          "vue: Pug comparison consumed the following real script setup block")
    check(any("afterDocs" in body for body in raw_bodies),
          "vue: custom Markdown comparison consumed the following real script block")
    check(not any("pugGhost" in body or "docsGhost" in body for body in raw_bodies),
          "vue: non-HTML top-level block leaked presentation text as executable code")
    check(raw_offsets == [4, 11],
          f"vue: raw block skipping corrupted outer-file line offsets: {raw_offsets}")

    # Native HTML templates still require same-name nesting; the inner closing tag
    # must not expose the decoy script as a top-level executable block.
    vue_native_nested = """\
<template>
  <template><script>export function nestedGhost() {}</script></template>
  <div>{{ count < limit ? "low" : "high" }}</div>
</template>
<script setup>export function afterNestedTemplate() {}</script>
"""
    nested_blocks = L._vue_script_blocks(vue_native_nested)
    check(len(nested_blocks) == 1 and "afterNestedTemplate" in nested_blocks[0][0],
          "vue: native template nesting no longer preserves the following top-level script")
    check("nestedGhost" not in nested_blocks[0][0] if nested_blocks else True,
          "vue: nested native-template script leaked into executable blocks")

    raw_craft = (
        '<template lang="pug">\n' + ("count < limit\n" * 40_000) + "</template>\n"
        '<docs lang="md">\n' + ("a < b\n" * 40_000) + "</docs>\n"
        "<script>export function afterLargeRawBlocks() {}</script>"
    )
    started = time.perf_counter()
    raw_craft_blocks = L._vue_script_blocks(raw_craft)
    raw_craft_elapsed = time.perf_counter() - started
    check(len(raw_craft_blocks) == 1 and
          "afterLargeRawBlocks" in raw_craft_blocks[0][0],
          "vue: adversarial raw-language '<' bytes consumed the trailing real script")
    check(raw_craft_elapsed < 1.5,
          f"vue: raw top-level scan exceeded linear budget: {raw_craft_elapsed:.3f}s")

    # ---- Report ---------------------------------------------------------------
    print(f"svelte file nodes : {len(svelte_file_nodes)}")
    print(f"svelte defs       : {len(svelte_defs)}  names={sorted(svelte_def_names)}")
    print(f"svelte imports    : {len(svelte_imports)}  dsts={sorted(svelte_import_dsts)}")
    print(f"svelte calls      : {len(svelte_calls)}  dsts={sorted(svelte_call_dsts)}")
    if greet_nodes:
        print(f"svelte greet start_line : {greet_nodes[0].get('start_line')} (expected 3)")
    print()
    print(f"vue file nodes    : {len(vue_file_nodes)}")
    print(f"vue defs          : {len(vue_defs)}  names={sorted(vue_def_names)}")
    print(f"vue imports       : {len(vue_imports)}  dsts={sorted(vue_import_dsts)}")
    print(f"vue calls         : {len(vue_calls)}  dsts={sorted(vue_call_dsts)}")
    if setup_nodes:
        print(f"vue setup start_line   : {setup_nodes[0].get('start_line')} (expected 6)")

    if fails:
        print()
        print("FAILURES:")
        for f in fails:
            print(f"  FAIL: {f}")
        print()
        print("SFC GATE: FAIL")
        sys.exit(1)
    else:
        print()
        print("SFC GATE: PASS")


if __name__ == "__main__":
    run()
