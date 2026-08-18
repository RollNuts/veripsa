#!/usr/bin/env python3
"""Astro and stylesheet graph coverage gate.

Hermetic and offline: every assertion runs the real ``build_graph`` pipeline over a
temporary repository.  The gate locks both recall and precision:

* Astro frontmatter contributes imports, symbols, calls and request-route coupling.
* Astro pages contribute file-based routes and markup contributes local asset edges.
* CSS-family files contribute local import/use/forward/composes dependencies only.
* comments, remote stylesheet references and template-only OpenAPI lookalikes stay inert.
* OpenAPI still sees genuine references in executable Astro TypeScript.
* malformed Astro/CSS inputs never abort the graph and retain honest language labels.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402
import _cg_languages as L  # noqa: E402


PASS_MARKER = "ASTRO / CSS GRAPH GATE: PASS"


def _write(root: str, rel: str, body: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def _file_language(graph: dict, rel: str) -> str | None:
    for node in graph["nodes"]:
        if node.get("kind") == "file" and node.get("path") == rel:
            return node.get("language")
    return None


def _has_edge(graph: dict, src: str, dst: str, kind: str) -> bool:
    return any(
        edge.get("src") == src and edge.get("dst") == dst and edge.get("kind") == kind
        for edge in graph["edges"]
    )


def _outgoing(graph: dict, src: str, kind: str | None = None) -> list[dict]:
    return [
        edge for edge in graph["edges"]
        if edge.get("src") == src and (kind is None or edge.get("kind") == kind)
    ]


def test_astro_structure_assets_and_routes() -> None:
    """One Astro page exercises the extractor, asset graph and both route directions."""
    with tempfile.TemporaryDirectory(prefix="veripsa-astro-graph-") as root:
        _write(root, "src/lib/helper.ts", "export function helper() { return 'ready'; }\n")
        _write(
            root,
            "src/pages/reports/dashboard.astro",
            """---
import { helper } from "../../lib/helper";
export function loadDashboard() {
  const value = helper();
  fetch("/api/dashboard/data");
  return value;
}
---
<html>
  <head><link rel="stylesheet" href="../../../styles/site.css" /></head>
  <body>
    <script src="../../client.js"></script>
    <pre>fetch("/api/markup/noise") is documentation, not executable code.</pre>
  </body>
</html>
""",
        )
        _write(
            root,
            "src/client.js",
            "export function openDashboard() { return fetch('/reports/dashboard'); }\n",
        )
        _write(root, "styles/site.css", ".dashboard { color: navy; }\n")
        _write(
            root,
            "api/server.py",
            """from fastapi import FastAPI
app = FastAPI()

@app.get("/api/dashboard/data")
def dashboard_data():
    return {}
""",
        )
        _write(
            root,
            "api/markup_noise.py",
            """from fastapi import FastAPI
app = FastAPI()

@app.get("/api/markup/noise")
def markup_noise():
    return {}
""",
        )

        graph = X.build_graph(root)

    astro = "src/pages/reports/dashboard.astro"
    assert _file_language(graph, astro) == "astro", "Astro file must carry language=astro"
    assert _file_language(graph, "styles/site.css") == "css", "CSS file must carry language=css"

    defs = [
        node for node in graph["nodes"]
        if node.get("path") == astro and node.get("kind") == "def"
    ]
    load = next((node for node in defs if node.get("name") == "loadDashboard"), None)
    assert load is not None, f"frontmatter function was not extracted: {defs}"
    assert load.get("language") == "astro", f"Astro symbol label drifted: {load}"
    assert load.get("start_line") == 3 and load.get("end_line", 0) >= 7, (
        f"frontmatter symbol span must use outer Astro lines: {load}"
    )

    assert _has_edge(graph, astro, "src/lib/helper.ts", "imports"), (
        f"frontmatter import did not resolve: {_outgoing(graph, astro, 'imports')}"
    )
    assert _has_edge(graph, astro, "helper", "calls"), "frontmatter helper call was not extracted"
    assert _has_edge(graph, astro, "fetch", "calls"), "frontmatter fetch call was not extracted"

    # The reused HTML pass connects markup to local assets.
    assert _has_edge(graph, astro, "styles/site.css", "imports"), (
        f"Astro link href did not resolve to CSS: {_outgoing(graph, astro, 'imports')}"
    )
    assert _has_edge(graph, astro, "src/client.js", "imports"), (
        f"Astro script src did not resolve to JS: {_outgoing(graph, astro, 'imports')}"
    )

    # Frontmatter fetch couples to the backend route; the src/pages path itself defines
    # /reports/dashboard, so the JS requester points back to the Astro page.  The reverse edges
    # distinguish route coupling from the one-way markup asset edge.
    assert _has_edge(graph, "api/server.py", astro, "imports"), (
        "backend route and Astro frontmatter fetch were not coupled"
    )
    assert _has_edge(graph, astro, "api/server.py", "imports"), (
        "Astro frontmatter fetch and backend route were not coupled symmetrically"
    )
    assert _has_edge(graph, "src/client.js", astro, "imports"), (
        "Astro src/pages file-based /reports/dashboard route was not coupled to its requester"
    )
    assert not _has_edge(graph, astro, "api/markup_noise.py", "imports"), (
        "a fetch example rendered in Astro markup must not fabricate a route coupling"
    )
    assert not _has_edge(graph, "api/markup_noise.py", astro, "imports"), (
        "a fetch example rendered in Astro markup must stay silent in both directions"
    )


def test_astro_dynamic_assets_stay_content_free_and_all_scripts_are_scanned() -> None:
    """Astro expressions/comments stay inert while static assets and every real script retain recall."""
    with tempfile.TemporaryDirectory(prefix="veripsa-astro-static-assets-") as root:
        _write(root, "src/static-client.js", "export const client = true;\n")
        _write(root, "styles/static.css", ".static { display: block; }\n")
        _write(
            root,
            "src/pages/dynamic.astro",
            """---
const pageReady = true;
---
<!-- <script>export function CommentOnlyLeak() { return 0; }</script> -->
{/* <script src="BODYLEAK_ASTRO_COMMENT_ASSET_DO_NOT_STORE">
export function AstroTemplateCommentOnlyLeak() { return 0; }
</script> */}
<p>İstanbul keeps source/string indices aligned.</p>
<style>
.example::after { content: "<script>import 'BODYLEAK_STYLE_STRING_DO_NOT_STORE'</script>"; }
</style>
<div>{"<script>import 'BODYLEAK_ASTRO_STRING_DO_NOT_STORE'</script>"}</div>
<div>{'<img src="BODYLEAK_ASTRO_EXPRESSION_ASSET_DO_NOT_STORE">'}</div>
<div>{/<img src=BODYLEAK_ASTRO_REGEX_ASSET_DO_NOT_STORE>/.test("fixture")}</div>
<img src={pageReady > false ? "<script>import 'BODYLEAK_ASTRO_ATTR_COMPARISON_DO_NOT_STORE'</script>" : fallbackAsset} />
<img src={/}/.test("fixture") ? "<script>import 'BODYLEAK_ASTRO_ATTR_REGEX_DO_NOT_STORE'</script>" : fallbackAsset} />
<textarea><img src="BODYLEAK_TEXTAREA_ASSET_DO_NOT_STORE"></textarea>
{count < 5 ? "low" : "high"}
<script>export function comparisonFollowedByRealScript() { return 6; }</script>
{a <b && c}
<script>export function spacedComparisonFollowedByRealScript() { return 7; }</script>
{a<script>c}
<script>export function compactComparisonFollowedByRealScript() { return 8; }</script>
{ready && <script>import 'BODYLEAK_CONDITIONAL_EXPRESSION_DO_NOT_STORE';
export function ConditionalExpressionOnlyLeak() { return 9; }</script>}
<div data-example="<script>export function AttributeOnlyLeak() {}</script>">
  <img src={BODYLEAK_PAYLOAD_DO_NOT_EGRESS+PRIVATE_LOGIC} />
  <a href={dynamicDestination}>dynamic</a>
  <script src=../static-client.js></script>
  <link rel="stylesheet" href="../../styles/static.css" />
</div>
<script type="application/json">{"private":"JSON_BODY_DO_NOT_PARSE"}</script>
<script type=application/json>export function UnquotedDataOnlyLeak() {}</script>
<script data-note=" type=application/json">
export function dataNoteClientBlock() { return 3; }
</script>
<script>
export function firstClientBlock() { return 1; }
</script>
<script type="module">
export function secondClientBlock() { return 2; }
</script>
<script type="">
export function emptyTypeClientBlock() { return 4; }
</script>
<script type>
export function valuelessTypeClientBlock() { return 5; }
</script>
<plaintext><script>import 'BODYLEAK_PLAINTEXT_DO_NOT_STORE'</script>
""",
        )

        graph = X.build_graph(root)

    astro = "src/pages/dynamic.astro"
    outgoing = _outgoing(graph, astro, "imports")
    serialized = json.dumps(graph, sort_keys=True)
    assert "BODYLEAK_" not in serialized, (
        f"an Astro expression escaped into the content-free graph: {outgoing}"
    )
    assert "dynamicDestination" not in serialized, (
        f"a dynamic href escaped into the content-free graph: {outgoing}"
    )
    assert _has_edge(graph, astro, "src/static-client.js", "imports"), (
        f"a static unquoted asset lost recall: {outgoing}"
    )
    assert _has_edge(graph, astro, "styles/static.css", "imports"), (
        f"a static quoted asset lost recall: {outgoing}"
    )

    defs = {
        node.get("name") for node in graph["nodes"]
        if node.get("path") == astro and node.get("kind") == "def"
    }
    assert {
        "firstClientBlock",
        "secondClientBlock",
        "dataNoteClientBlock",
        "emptyTypeClientBlock",
        "valuelessTypeClientBlock",
        "comparisonFollowedByRealScript",
        "spacedComparisonFollowedByRealScript",
        "compactComparisonFollowedByRealScript",
    } <= defs, (
        f"all executable client scripts must be scanned: {defs}"
    )
    inert_defs = {
        "CommentOnlyLeak",
        "AstroTemplateCommentOnlyLeak",
        "AttributeOnlyLeak",
        "UnquotedDataOnlyLeak",
        "ConditionalExpressionOnlyLeak",
    }
    assert not inert_defs & defs, (
        f"comment/attribute/data examples must not be parsed as scripts: {defs}"
    )
    assert "JSON_BODY_DO_NOT_PARSE" not in serialized, (
        "a non-executable application/json script body entered the graph"
    )


def test_css_local_dependencies_and_precision() -> None:
    """Stylesheet directives resolve locally; comments and remote schemes emit nothing."""
    with tempfile.TemporaryDirectory(prefix="veripsa-css-graph-") as root:
        _write(
            root,
            "styles/site.scss",
            """.unicode::before { content: "İ"; }
@charset "UTF-8"; @import "./same-line.css";
@import "./reset.css";
@import"./minified.css";
@import url('./theme.css') screen;
@use "./tokens";
@use"./minified-token";
@forward "./foundation";
@use "./code-only";
@use "./direct";
@use "./explicit.scss";
@use "./theme.dark";
.title { composes: heading from "./typography.module.css"; }
.multiline {
  composes:
    heading
    from "./multiline.module.css";
}
.example::before { content: 'composes: fake from "./string-only.module.css"'; }
.directive-example::before { content: "prefix\\
@import 'BODYLEAK_CSS_STRING_DIRECTIVE_DO_NOT_STORE';
suffix"; }
.custom-property { --payload: { composes: fake from "BODYLEAK_CUSTOM_PROPERTY_DO_NOT_STORE"; }; }
.property-value-braces { foo:{composes: fake from "BODYLEAK_PROPERTY_BRACES_DO_NOT_STORE";}; }
.detached-value { @detached:{composes: fake from "BODYLEAK_DETACHED_RULESET_DO_NOT_STORE";}; }
.function-value { filter:func({composes: fake from "BODYLEAK_FUNCTION_VALUE_DO_NOT_STORE";}); }
.custom-directive-value { --payload:
@import "BODYLEAK_CUSTOM_DIRECTIVE_VALUE_DO_NOT_STORE";
}
.ordinary-directive-value { foo:
@import "BODYLEAK_ORDINARY_DIRECTIVE_VALUE_DO_NOT_STORE";
}
.interpolated-#{composes: fake from "BODYLEAK_SASS_INTERPOLATION_DO_NOT_STORE"} { color: red; }

@mixin nested-import-recall {
  @import "./mixin-real.css";
}
@mixin inline-import-recall { @import "./inline-nested.css"; }
.nested-recall {
  .child {
    composes: heading from "./nested.module.css";
  }
}

/* @import "./commented.css"; */
/* .bad { composes: bad from "./commented.module.css"; } */
@import "https://cdn.example.test/remote.css";
@import url("//cdn.example.test/protocol-relative.css");
@import "data:text/css,body{}";
@use "sass:math";
@import "#fragment";
""",
        )
        for rel in (
            "styles/reset.css",
            "styles/minified.css",
            "styles/theme.css",
            "styles/_tokens.scss",
            "styles/_minified-token.scss",
            "styles/foundation/_index.scss",
            "styles/direct.scss",
            "styles/direct/_index.scss",
            "styles/_explicit.scss",
            "styles/_theme.dark.scss",
            "styles/typography.module.css",
            "styles/multiline.module.css",
            "styles/mixin-real.css",
            "styles/inline-nested.css",
            "styles/nested.module.css",
            "styles/same-line.css",
            "styles/string-only.module.css",
            # Real files make a comment-extraction regression resolve visibly.
            "styles/commented.css",
            "styles/commented.module.css",
        ):
            _write(root, rel, ".fixture { display: block; }\n")
        # Same-stem code is a precision decoy: the explicit stylesheet extension must win.
        _write(root, "styles/reset.ts", "export const reset = true;\n")
        _write(root, "styles/tokens.ts", "export const tokens = true;\n")
        _write(root, "styles/code-only.ts", "export const codeOnly = true;\n")
        _write(root, "styles/theme.ts", "export const theme = true;\n")
        _write(
            root,
            "styles/plain.css",
            '@use "./tokens";\n@forward "./foundation";\n@import "./css-only";\n',
        )
        _write(root, "styles/css-only.css", ".plain { display: block; }\n")
        _write(root, "styles/_css-only.scss", ".wrong { display: none; }\n")
        _write(
            root,
            "styles/plain.less",
            '@import "./less-token";\n'
            '@import (reference) "./reference-token.less";\n'
            '@import (reference, optional, less) "./multi-option.less";\n'
            '@import (less) url("./option-url.less");\n'
            '@payload: `"prefix\\n@import \'BODYLEAK_LESS_BACKTICK_DO_NOT_STORE\';"`;\n'
            '@import "./css-only";\n'
            '@import "./theme.php";\n',
        )
        _write(root, "styles/less-token.less", ".less { display: block; }\n")
        _write(root, "styles/reference-token.less", ".reference { display: block; }\n")
        _write(root, "styles/multi-option.less", ".multi { display: block; }\n")
        _write(root, "styles/option-url.less", ".url { display: block; }\n")
        _write(root, "styles/_less-token.scss", ".wrong { display: none; }\n")
        _write(root, "styles/theme.php.less", ".wrong { display: none; }\n")
        _write(
            root,
            "styles/plain.sass",
            ".fixture\n"
            "  color: red\n"
            "payload:\n"
            "  @import \"BODYLEAK_SASS_CONTINUATION_DO_NOT_STORE\"\n"
            "@use \"./sass-tokens\"\n"
            "@import \"./sass-import.css\"\n",
        )
        _write(root, "styles/_sass-tokens.sass", "$token: navy\n")
        _write(root, "styles/sass-import.css", ".sass-import { display: block; }\n")
        _write(
            root,
            "styles/plain.styl",
            '@import "./stylus-theme"\n@import "./stylus.php"\n',
        )
        _write(root, "styles/stylus-theme/index.styl", ".stylus\n  display block\n")
        _write(root, "styles/stylus-theme.css", ".wrong { display: none; }\n")
        _write(root, "styles/stylus.php.styl", ".wrong\n  display none\n")
        _write(
            root,
            "src/ProductCard.tsx",
            "import styles from '../styles/ProductCard.module.scss';\n"
            "export const ProductCard = () => <article className={styles.card} />;\n",
        )
        _write(root, "styles/ProductCard.module.scss", ".card { display: block; }\n")
        _write(
            root,
            "styles/line-comments.scss",
            '// @use "./line-commented.scss";\n// .bad { composes: bad from "./line-commented.scss"; }\n',
        )
        _write(root, "styles/line-commented.scss", ".must-stay-unreferenced { color: red; }\n")

        graph = X.build_graph(root)

    src = "styles/site.scss"
    assert _file_language(graph, src) == "css"
    assert _file_language(graph, "styles/_tokens.scss") == "css"

    expected = {
        "styles/reset.css",
        "styles/minified.css",
        "styles/theme.css",
        "styles/_tokens.scss",
        "styles/_minified-token.scss",
        "styles/foundation/_index.scss",
        "styles/direct.scss",
        "styles/_explicit.scss",
        "styles/_theme.dark.scss",
        "styles/typography.module.css",
        "styles/multiline.module.css",
        "styles/mixin-real.css",
        "styles/inline-nested.css",
        "styles/nested.module.css",
        "styles/same-line.css",
    }
    actual = {
        edge["dst"] for edge in _outgoing(graph, src, "imports")
        if edge.get("dst") in expected
    }
    assert actual == expected, f"local CSS dependency coverage drifted: expected={expected}, got={actual}"
    assert not _has_edge(graph, src, "styles/reset.ts", "imports"), (
        "an explicit reset.css dependency must not bind to a same-stem TypeScript file"
    )
    assert not _has_edge(graph, src, "styles/tokens.ts", "imports"), (
        "an extensionless Sass partial must not bind to a same-stem TypeScript file"
    )
    assert not _has_edge(graph, src, "styles/code-only.ts", "imports"), (
        "an unresolved stylesheet module must stay unresolved instead of binding to code"
    )
    assert not _has_edge(graph, src, "styles/direct/_index.scss", "imports"), (
        "Sass must not add a directory index after a direct module already resolved"
    )
    assert not _has_edge(graph, src, "styles/theme.ts", "imports"), (
        "a dotted Sass basename must not fall through to a same-stem TypeScript file"
    )
    assert _has_edge(graph, "styles/plain.css", "styles/css-only.css", "imports")
    assert not _has_edge(graph, "styles/plain.css", "styles/_css-only.scss", "imports"), (
        "plain CSS resolution must not inherit Sass partial rules"
    )
    assert not _has_edge(graph, "styles/plain.css", "styles/_tokens.scss", "imports"), (
        "plain CSS must ignore Sass-only @use/@forward directives"
    )
    assert _has_edge(graph, "styles/plain.less", "styles/less-token.less", "imports")
    assert _has_edge(
        graph, "styles/plain.less", "styles/reference-token.less", "imports"
    ), "Less @import (reference) must retain its local dependency"
    assert _has_edge(
        graph, "styles/plain.less", "styles/multi-option.less", "imports"
    ), "Less comma-separated import options must retain their local dependency"
    assert _has_edge(
        graph, "styles/plain.less", "styles/option-url.less", "imports"
    ), "Less import options followed by url() must retain their local dependency"
    assert not _has_edge(graph, "styles/plain.less", "styles/_less-token.scss", "imports"), (
        "Less resolution must not fan out into Sass partials"
    )
    assert _has_edge(
        graph, "styles/plain.sass", "styles/_sass-tokens.sass", "imports"
    ), "indented Sass must resume @use scanning after a newline-terminated declaration"
    assert _has_edge(
        graph, "styles/plain.sass", "styles/sass-import.css", "imports"
    ), "indented Sass must retain a later top-level @import"
    assert not _has_edge(graph, "styles/plain.less", "styles/css-only.css", "imports"), (
        "an extensionless Less import must not fall back to a same-stem CSS file"
    )
    assert not _has_edge(graph, "styles/plain.less", "styles/theme.php.less", "imports"), (
        "an explicit arbitrary Less suffix must not gain an extra .less extension"
    )
    assert _has_edge(
        graph, "styles/plain.styl", "styles/stylus-theme/index.styl", "imports"
    ), "an extensionless Stylus directory import must resolve to index.styl"
    assert not _has_edge(graph, "styles/plain.styl", "styles/stylus-theme.css", "imports"), (
        "an extensionless Stylus import must not fall back to a same-stem CSS file"
    )
    assert not _has_edge(graph, "styles/plain.styl", "styles/stylus.php.styl", "imports"), (
        "an explicit arbitrary Stylus suffix must not gain an extra .styl extension"
    )
    assert _has_edge(
        graph, "src/ProductCard.tsx", "styles/ProductCard.module.scss", "imports"
    ), "a TSX CSS Modules import must resolve to its explicit local .module.scss file"
    assert not _outgoing(graph, "styles/line-comments.scss", "imports"), (
        "Sass-family line comments must not emit dependency edges"
    )
    assert not _has_edge(
        graph, src, "styles/string-only.module.css", "imports"
    ), "a composes-like CSS generated-content string must stay inert"
    serialized = json.dumps(graph, sort_keys=True)
    assert "BODYLEAK_" not in serialized, (
        "CSS strings/custom values/interpolation must not enter persisted-shape graph values"
    )

    forbidden_fragments = (
        "commented.css",
        "commented.module.css",
        "cdn.example.test",
        "data:text",
        "sass:math",
        "#fragment",
    )
    bad = [
        edge for edge in _outgoing(graph, src, "imports")
        if any(fragment in str(edge.get("dst")) for fragment in forbidden_fragments)
    ]
    assert not bad, f"commented or remote CSS dependencies must stay inert: {bad}"


def test_openapi_scans_only_executable_astro_not_css_or_markup() -> None:
    """OpenAPI keeps Astro TS recall without schema/path lookalikes from presentation text."""
    with tempfile.TemporaryDirectory(prefix="veripsa-astro-openapi-") as root:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "fixture", "version": "1"},
            "paths": {
                "/api/orders/list": {
                    "get": {
                        "operationId": "listOrders",
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
            "components": {"schemas": {"BrandPalette": {"type": "object"}}},
        }
        _write(root, "openapi.json", json.dumps(spec))
        _write(
            root,
            "styles/noise.css",
            '.BrandPalette { background-image: url("/api/orders/list"); }\n',
        )
        _write(
            root,
            "src/components/MarkupNoise.astro",
            """---
const harmless = "presentation only";
---
<div class="BrandPalette">
  <a href="/api/orders/list">not executable TypeScript</a>
</div>
""",
        )
        _write(
            root,
            "src/components/ExecutableUse.astro",
            """---
type Palette = BrandPalette;
export function endpoint(): string {
  return "/api/orders/list";
}
---
<div>real executable reference above</div>
""",
        )

        graph = X.build_graph(root)

    op = "api_operation::listOrders"
    schema = "api_schema::BrandPalette"
    css_queries = _outgoing(graph, "styles/noise.css", "queries")
    markup_queries = _outgoing(graph, "src/components/MarkupNoise.astro", "queries")
    executable_queries = _outgoing(graph, "src/components/ExecutableUse.astro", "queries")

    assert not css_queries, f"CSS schema/path lookalikes fabricated OpenAPI coupling: {css_queries}"
    assert not markup_queries, f"Astro markup fabricated OpenAPI coupling: {markup_queries}"
    assert _has_edge(graph, "src/components/ExecutableUse.astro", op, "queries"), (
        f"Astro executable route literal was missed: {executable_queries}"
    )
    assert _has_edge(graph, "src/components/ExecutableUse.astro", schema, "queries"), (
        f"Astro TypeScript schema reference was missed: {executable_queries}"
    )


def test_malformed_inputs_never_crash_and_remain_labelled() -> None:
    """Broken fences/comments degrade to labelled file nodes without invented structure."""
    with tempfile.TemporaryDirectory(prefix="veripsa-astro-css-bad-") as root:
        _write(
            root,
            "src/pages/bad.astro",
            """---
export function ShouldNotMint( {
  fetch("/api/not-closed");
<section>{ definitely broken
""",
        )
        _write(
            root,
            "styles/bad.css",
            "/* unclosed comment with @import \"./ghost.css\";\n.bad { color: red;\n",
        )

        graph = X.build_graph(root)

    assert _file_language(graph, "src/pages/bad.astro") == "astro"
    assert _file_language(graph, "styles/bad.css") == "css"
    assert not any(
        node.get("path") == "src/pages/bad.astro" and node.get("name") == "ShouldNotMint"
        for node in graph["nodes"]
    ), "an unclosed Astro frontmatter fence must not be scanned as executable TypeScript"
    assert not _outgoing(graph, "styles/bad.css", "imports"), (
        "an import inside an unclosed CSS comment must stay inert"
    )


def test_adversarial_sfc_and_composes_scans_are_bounded() -> None:
    """Sub-size-cap unmatched constructs must stay linear, not monopolize the worker."""
    script_craft = "<script data-kind='module'>" * 20_000
    started = time.perf_counter()
    assert L._first_script_block(script_craft) is None
    script_elapsed = time.perf_counter() - started
    assert script_elapsed < 1.5, (
        f"unclosed script scan exceeded the linear-time budget: {script_elapsed:.3f}s"
    )

    astro_tags_craft = "<div data-static='ok'></div>" * 20_000
    started = time.perf_counter()
    assert L._script_blocks(astro_tags_craft, astro_template_comments=True) == []
    astro_tags_elapsed = time.perf_counter() - started
    assert astro_tags_elapsed < 1.5, (
        "comment-free Astro tag scan exceeded the linear-time budget: "
        f"{astro_tags_elapsed:.3f}s"
    )

    astro_comments_craft = "{/* inert */}" * 20_000
    started = time.perf_counter()
    assert L._script_blocks(astro_comments_craft, astro_template_comments=True) == []
    astro_comments_elapsed = time.perf_counter() - started
    assert astro_comments_elapsed < 1.5, (
        "tag-free Astro template-comment scan exceeded the linear-time budget: "
        f"{astro_comments_elapsed:.3f}s"
    )

    expression_craft = (
        '{"<script>import \'BODYLEAK_MASK_CRAFT\'</script>"}' * 12_000
    )
    started = time.perf_counter()
    safe_expression_craft = L._mask_astro_non_executable(expression_craft)
    assert L._script_blocks(safe_expression_craft, astro_template_comments=True) == []
    expression_elapsed = time.perf_counter() - started
    assert expression_elapsed < 1.5, (
        "Astro expression-string mask exceeded the linear-time budget: "
        f"{expression_elapsed:.3f}s"
    )

    comparison_craft = (
        "{" + ("a <b && " * 40_000) + "true}"
        "<script>export function RealAfterComparisons() {}</script>"
    )
    started = time.perf_counter()
    safe_comparison_craft = L._mask_astro_non_executable(comparison_craft)
    comparison_blocks = L._script_blocks(
        safe_comparison_craft, astro_template_comments=True
    )
    comparison_elapsed = time.perf_counter() - started
    assert any("RealAfterComparisons" in body for body, _line in comparison_blocks)
    assert comparison_elapsed < 1.5, (
        "comparison-heavy Astro mask exceeded the linear-time budget: "
        f"{comparison_elapsed:.3f}s"
    )

    markup_candidate_craft = (
        "{" + ("true && <B " * 30_000) + "done}"
        "<script>export function RealAfterMarkupCandidates() {}</script>"
    )
    started = time.perf_counter()
    safe_markup_candidate_craft = L._mask_astro_non_executable(markup_candidate_craft)
    markup_candidate_blocks = L._script_blocks(
        safe_markup_candidate_craft, astro_template_comments=True
    )
    markup_candidate_elapsed = time.perf_counter() - started
    assert any(
        "RealAfterMarkupCandidates" in body
        for body, _line in markup_candidate_blocks
    )
    assert markup_candidate_elapsed < 1.5, (
        "markup-candidate Astro mask exceeded the linear-time budget: "
        f"{markup_candidate_elapsed:.3f}s"
    )

    composes_craft = ".x { composes:" + (" className" * 60_000) + " }"
    started = time.perf_counter()
    assert L._css_composes_refs(composes_craft) == []
    composes_elapsed = time.perf_counter() - started
    assert composes_elapsed < 1.5, (
        f"missing-from composes scan exceeded the linear-time budget: {composes_elapsed:.3f}s"
    )

    directive_craft = (
        '.x{content:"prefix\\\n@import \'BODYLEAK_DIRECTIVE_CRAFT\';'
        + (" payload" * 60_000)
        + '";}'
    )
    started = time.perf_counter()
    assert L._css_directive_refs(directive_craft, "scss") == []
    directive_elapsed = time.perf_counter() - started
    assert directive_elapsed < 1.5, (
        f"CSS string-aware directive scan exceeded the linear-time budget: {directive_elapsed:.3f}s"
    )

    with tempfile.TemporaryDirectory(prefix="veripsa-less-options-dos-") as root:
        less_craft = "@import (" + ("reference," * 60_000) + ' "./never.less";\n'
        _write(root, "styles/pathological.less", less_craft)
        started = time.perf_counter()
        _nodes, edges, ok = L.extract_file_css(
            os.path.join(root, "styles/pathological.less"),
            "styles/pathological.less",
        )
        less_elapsed = time.perf_counter() - started
    assert ok and not edges
    assert less_elapsed < 1.5, (
        f"unclosed Less option scan exceeded the linear-time budget: {less_elapsed:.3f}s"
    )


def test_indented_css_declaration_scan_has_linear_work() -> None:
    """Later braces must not make every Sass statement rescan the remaining file."""

    stylus_selector = (
        "a:hover\n"
        '  @import "./nested.css"\n'
        '  composes: child from "./nested.module.css"\n'
        "  .child { color: red }\n"
    )
    assert L._css_directive_refs(stylus_selector, "styl") == ["./nested.css"]
    assert L._css_composes_refs(stylus_selector, "styl") == [
        "./nested.module.css"
    ], "an indented pseudo-selector must not swallow nested dependency declarations"

    brace_less_selector = (
        "a:hover\n"
        '  @import "./brace-less.css"\n'
        '  composes: child from "./brace-less.module.css"\n'
    )
    assert L._css_directive_refs(brace_less_selector, "styl") == ["./brace-less.css"]
    assert L._css_composes_refs(brace_less_selector, "styl") == [
        "./brace-less.module.css"
    ]

    custom_value = (
        '.x { --payload: token { @import "./value-leak.css"; }; '
        '@import "./real.css"; }'
    )
    assert L._css_directive_refs(custom_value, "css") == ["./real.css"]
    assert L._css_directive_refs(
        '.x:not({ @import "./group-leak.css";', "scss"
    ) == []
    assert L._css_composes_refs(
        '.x[bad={ composes: a from "./group-leak.module.css";', "scss"
    ) == []
    composes_selector = 'composes:hover\n  from "./selector-not-module.css"\n'
    assert L._css_composes_refs(composes_selector, "styl") == []
    for pseudo in ("-webkit-any(.a)", "global(.a)", "local(.a)", "deep(.a)", "slotted(.a)"):
        nested = (
            f"button:{pseudo}\n"
            '  @import "./pseudo.css"\n'
            '  composes: child from "./pseudo.module.css"\n'
        )
        assert L._css_directive_refs(nested, "styl") == ["./pseudo.css"]
        assert L._css_composes_refs(nested, "styl") == ["./pseudo.module.css"]
    assert L._css_directive_refs(
        '.x:not(]) { @import "./mismatch-leak.css"; }', "scss"
    ) == []
    assert L._css_composes_refs(
        '.x[bad=)] { composes: a from "./mismatch-leak.module.css"; }', "scss"
    ) == []

    class CountingSource(str):
        accesses = 0

        def __getitem__(self, key):
            type(self).accesses += 1
            return super().__getitem__(key)

    def scan_accesses(lines: int, scanner) -> int:
        source = CountingSource(("a:hover\n" * lines) + "{")
        CountingSource.accesses = 0
        assert scanner(source) == []
        return CountingSource.accesses

    for scanner in (
        lambda source: L._css_directive_refs(source, "sass"),
        lambda source: L._css_composes_refs(source, "sass"),
    ):
        small = scan_accesses(256, scanner)
        large = scan_accesses(512, scanner)
        assert large <= small * 3, (
            "doubling indentation-based CSS input must not quadruple declaration "
            f"lookahead work: {small} -> {large} source accesses"
        )

    def blank_scan_accesses(lines: int, scanner) -> int:
        source = CountingSource("payload:\n" + ("\n" * lines) + "  continuation")
        CountingSource.accesses = 0
        assert scanner(source) == []
        return CountingSource.accesses

    for scanner in (
        lambda source: L._css_directive_refs(source, "styl"),
        lambda source: L._css_composes_refs(source, "styl"),
    ):
        small = blank_scan_accesses(2_048, scanner)
        large = blank_scan_accesses(4_096, scanner)
        assert large <= small * 3, (
            "doubling blank-line continuation input must keep bounded work: "
            f"{small} -> {large} source accesses"
        )


TESTS = (
    test_astro_structure_assets_and_routes,
    test_astro_dynamic_assets_stay_content_free_and_all_scripts_are_scanned,
    test_css_local_dependencies_and_precision,
    test_openapi_scans_only_executable_astro_not_css_or_markup,
    test_malformed_inputs_never_crash_and_remain_labelled,
    test_adversarial_sfc_and_composes_scans_are_bounded,
    test_indented_css_declaration_scan_has_linear_work,
)


def main() -> None:
    failures: list[str] = []
    for test in TESTS:
        try:
            test()
            print(f"  [PASS] {test.__name__}")
        except Exception as exc:
            failures.append(f"{test.__name__}: {exc}")
            print(f"  [FAIL] {test.__name__}: {exc}")
    if failures:
        print(f"\nASTRO / CSS GRAPH GATE: FAIL ({len(failures)} failure(s))")
        raise SystemExit(1)
    print(f"\n{PASS_MARKER} ({len(TESTS)} scenarios)")


if __name__ == "__main__":
    main()
