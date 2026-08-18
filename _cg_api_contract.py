"""API-contract cross-substrate coupling extraction (GraphQL + protobuf/gRPC).

Owns the API-CONTRACT GRAPH substrate: GraphQL schema type definitions referenced
from resolvers / operation documents, and protobuf message/service definitions
referenced from generated-stub client/server code.

WHY THIS MATTERS (the crown-jewel coupling code-only tools structurally miss):
Two files that touch the SAME API contract symbol — a GraphQL `type Order` DEFINED in
a `.graphql` schema and REFERENCED in a resolver map keyed by `Order` and in a frontend
`gql`{ order { id } }`` query, or a protobuf `service OrderService` DEFINED in an
`order.proto` and REFERENCED by a hand-written gRPC client — are coupled with NO code
edge, NO import, NO call. Neither the call graph nor the import graph can see this
coupling (the schema/proto is its own little language, and the generated code that bridges
it is excluded as vendored). This module surfaces it by recovering the named CONTRACT
SYMBOL shared across files. It mirrors _cg_iac / _cg_schema exactly: the DEFINING file
emits an `alters` edge to the contract node; a REFERENCING file emits a `queries` edge;
two such files then couple through the shared contract node (REUSING the existing
alters/queries shared-resource adjacency in the contention SQL — zero SQL changes).

CONTENT-FREE: contract symbol NAMES only (`api_type::Order`, `api_message::CreateOrderRequest`,
`api_service::OrderService`). Never field values, never message bodies, never schema field
definitions, never comment content. A type/message/service NAME is pure structural metadata.

PRECISION STRATEGY (two-pass, mirrors _cg_schema._schema_graph and _cg_iac._iac_graph):
  Pass 1: collect the LOCALLY-DEFINED contract symbol names → the known-symbol set.
  Pass 2: scan files for ANCHORED references to KNOWN names only.
  A reference that does not match a locally-defined contract symbol is ignored — a random
  type-name-shaped token in prose or arbitrary code cannot mint a coupling. References are
  only emitted from ANCHORED contexts (a gql/graphql tagged template, a `.graphql`/`.gql`
  operation file, a resolver map keyed by the type name, or a distinctive proto symbol use)
  so an ordinary English word equal to a type name never fabricates a coupling. Veripsa's
  quality bar is PRECISE SILENCE: over-firing = wallpaper. We UNDER-emit references rather
  than fabricate couplings; recall-safe here means never DROP a real DEFINITION, not match
  every prose token.

GraphQL (.graphql / .gql):
  - DEFINITIONS: user-defined `type X`, `interface X`, `input X`, `enum X`, `union X`,
    `scalar X` (custom scalars). Mint an `api_type` node per user-defined name; the schema
    file gets an `alters` edge to it.
  - REFERENCES: code/other-schema files that reference those names, but ONLY when anchored —
    inside a `gql`...`` / `graphql`...`` tagged template, inside a `.graphql`/`.gql`
    operation/fragment file, or in a resolver map keyed by the type name.
  - STOPLIST: never mint nodes for the GraphQL root operation types Query / Mutation /
    Subscription (and the Hasura/convention root names query_root / mutation_root /
    subscription_root), the built-in scalars String / Int / Float / Boolean / ID, the GraphQL
    KEYWORDS (type / interface / input / enum / union / scalar / extend / schema / …), and a set
    of measured cross-language collision words (Body / List / Root / Time / Node / Edge / …). They
    are ubiquitous and would couple everything. See _GQL_STOPLIST for the audit rationale.
  - MULTI-DEFINER: a type defined in MORE THAN ONE .graphql file is ambiguous (N schemas sharing
    a name, not one contract). Its node and edges remain evidence, tagged ambiguous so effective
    adjacency excludes them (precision without silent loss).

protobuf / gRPC (.proto):
  - DEFINITIONS: `message X`, `service Y`, `enum Z` (enums minted as api_message-style
    nodes). Mint `api_message` / `api_service` nodes; the .proto file gets an `alters` edge
    to each.
  - REFERENCES: CODE files only (an allowlist of code extensions — .sh/.md/.txt/.html/.json etc.
    are excluded so prose / expected-output / config never fabricates a coupling) that reference
    the message/service NAME (generated-stub usage, client/server code naming the type). proto
    names are distinctive PascalCase; a measured stoplist drops ones that collide with common
    library/std identifiers (State / Buffer / Stat / Extension / …). References are emitted only
    for anchored uses; unanchored ones are skipped.
  - MULTI-DEFINER: a message/service defined in MORE THAN ONE .proto keeps inert, explicitly
    ambiguous definition/reference evidence (precision without silent loss).
  - content-free: names only, never field values / bodies / comment content.

ROUTING: `.graphql`/`.gql`/`.proto` are in _SOURCE_EXT → already walked into source_files
(as bare file nodes, no grammar). The reference SIDE comes from any source file (code or
another schema file) already in source_files. This module ONLY reads files explicitly
handed to it (the guard-filtered source_files list).
"""
from __future__ import annotations

import os
import re
from typing import Any

from _cg_xrepo import cross_repo_keys_enabled  # cross-repo consumer-key emission flag (default OFF)

# ---------------------------------------------------------------------------
# Extensions this substrate cares about
# ---------------------------------------------------------------------------

# GraphQL schema / operation files (definitions live here; operation files anchor references).
_GRAPHQL_EXTS = frozenset({".graphql", ".gql"})
# Protobuf / gRPC definition files.
_PROTO_EXT = ".proto"

# Cap on the bytes scanned from a single file. The size cap in build_graph already bounds
# file size to 1.5 MB, but we additionally cap the regex-scanned slice so an adversarial
# file that slips under the size cap (and a giant tagged-template body) cannot blow up the
# scan. Real schema / proto / source files are well under this.
from _cg_io import _mark_incomplete, _read_capped  # shared bounded file reader + loss diagnostics

# Cap on distinct api_* nodes minted per repo (adversarial-flood guard). Real API schemas
# rarely exceed a few thousand named types/messages/services.
_MAX_API_NODES = 20_000

# Line- and block-comment strippers for the REFERENCE side. A contract symbol that appears
# ONLY inside a comment is NOT a real use — coupling on it is over-firing (wallpaper). We
# blank out comment regions BEFORE scanning code files for references so a name in prose
# ("# the WidgetConfig describes …") cannot fabricate a coupling. Content-free: we only
# remove text before NAME matching; we never read or emit comment content. Replacement with
# spaces preserves offsets (harmless) and is never-crash (pure regex sub).
_LINE_COMMENT_RE = re.compile(r'(//|#)[^\n]*')
_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)


def _strip_comments(text: str) -> str:
    """Blank out // line, # line, and /* block */ comments (replace with spaces, keeping
    newlines so line structure and offsets are roughly preserved). Used on the REFERENCE
    side only. NOT applied to GraphQL `gql`...`` template bodies (a GraphQL body has its own
    `#` comments, but the tagged-template anchor is already a strong anchor and stripping `#`
    there is handled by the body scan ignoring stoplisted/unknown tokens). Never-crash."""
    def _blank(m: re.Match) -> str:
        return "".join("\n" if c == "\n" else " " for c in m.group(0))
    text = _BLOCK_COMMENT_RE.sub(_blank, text)
    text = _LINE_COMMENT_RE.sub(_blank, text)
    return text

# ---------------------------------------------------------------------------
# GraphQL: definitions + anchored references
# ---------------------------------------------------------------------------

# GraphQL stoplist: root operation types + built-in scalars are ubiquitous → would couple
# everything. NEVER mint nodes for these, and NEVER treat them as references. Matched
# case-INSENSITIVELY (callers test `name.lower() in _GQL_STOPLIST`), so entries are lowercase.
#
# AUDIT (2026-06-20, measured on hasura/graphql-engine): the stoplist had three precision holes
# that produced the bulk of GraphQL false couplings:
#   - GRAPHQL KEYWORDS were not stoplisted. The DEFINITION regex is permissive enough that a
#     description line of prose like `input type for incrementing numeric columns ...` matched
#     `input <name=type>` and minted an `api_type::type` node; the keyword `type` then appears as
#     a bare identifier in EVERY `.graphql` schema (`type X { ... }`), so 768 unrelated schema
#     files all "referenced" it → one node alone accounted for ~96% of hasura's coupling pairs.
#     Every GraphQL keyword has this property (ubiquitous bare token, never a real user type name),
#     so the whole keyword set is stoplisted (mint-suppressed AND reference-suppressed).
#   - Hasura's ROOT operation type names `query_root`/`mutation_root`/`subscription_root` (the
#     conventional non-default root names) were not covered (only `query`/`mutation`/`subscription`
#     were) → they collided with Rust module paths / config keys across languages.
#   - CROSS-LANGUAGE COLLISION WORDS — `Body` (a Go `request.Body`, a TS Fastify generic, a Rust
#     `axum_core::body::Body`), `Root`, `Time`, `List`, `Node`, `Edge`, `Info`, `Result` — are
#     common identifiers in many languages and coupled unrelated files through an `api_type::Body`
#     etc. that happened to be defined once in some test schema. These are NOT distinctive enough
#     to anchor a real cross-file GraphQL coupling, so they are dropped (precision over recall:
#     a user type literally named `Body` is rare and, if it exists, loses only this one coupling).
_GQL_STOPLIST = frozenset({
    "query", "mutation", "subscription",          # root operation types
    "query_root", "mutation_root", "subscription_root",  # Hasura/convention root names (CLASS B)
    "string", "int", "float", "boolean", "id",    # built-in scalars
    # GraphQL type-system + executable KEYWORDS — never legitimate user type names, and they
    # appear as bare identifiers in every document/schema (the bare-identifier reference scan
    # would otherwise couple everything). Mint- and reference-suppressed.
    "type", "interface", "input", "enum", "union", "scalar", "extend",
    "schema", "directive", "implements", "on", "fragment", "repeatable",
    # cross-language collision words measured as false couplers (CLASS B).
    "body", "list", "root", "time", "node", "edge", "info", "result",
})

# A GraphQL type-system definition header: `type X`, `interface X`, `input X`, `enum X`,
# `union X`, `scalar X`, optionally prefixed by `extend`. Group 1 = the (optional) `extend`
# keyword, group 2 = the type-system keyword, group 3 = the user-defined name.
#
# `extend type X` is NOT an independent BASE definition of X — it is a syntactically-marked
# CONTRIBUTION to a type whose base is declared elsewhere (schema stitching / federation:
# `type Product` in one file, `extend type Product` adding fields in another). The two are the
# SAME contract split across files, NOT two ambiguous same-name contracts. We therefore capture
# the `extend` prefix (group 1) so the caller can treat an extension as a REFERENCE (queries edge)
# rather than a DEFINER (alters edge). This is the multi-definer guard's discipline applied
# correctly: counting an `extend` as a 2nd definer would make a base+extension pair look ambiguous
# and SUPPRESS the whole type (a measured silent-miss — the base, the extension, and EVERY
# referencer would lose coupling). See _gql_type_decls() and _suppress_multi_definer().
_GQL_DEF_RE = re.compile(
    r'(?:^|\n)\s*(extend\s+)?(type|interface|input|enum|union|scalar)\s+'
    r'([A-Za-z_][A-Za-z0-9_]*)\b',
)

# A reference to a type name inside a GraphQL document body (operation/fragment/schema).
# In GraphQL bodies a user-defined type appears as a bare identifier in field-type position
# (`order: Order`, `[Order!]!`), as a fragment target (`... on Order`), or as a named type
# in a variable definition (`$x: OrderInput`). We scan for bare PascalCase-ish identifiers
# and keep only those in the KNOWN set (the known-set guard is what gives precision; the
# regex is deliberately permissive on the reference side, restrictive via the known set).
_GQL_IDENT_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\b')

# A gql / graphql tagged-template literal in JS/TS source: ``gql`...` `` or ``graphql`...` ``.
# Group 1 = the template body (between the backticks). DOTALL so multi-line queries match.
# Non-greedy so adjacent templates are matched separately. We do NOT match arbitrary
# backtick strings — only those tagged `gql` or `graphql` (the anchor).
_GQL_TAGGED_RE = re.compile(
    r'\b(?:gql|graphql)\s*`([^`]*)`',
    re.DOTALL,
)

# A resolver-map key: an object literal keyed by a bare type name, e.g.
#   const resolvers = { Order: { ... }, Query: { ... } }
# We anchor on `Name:` appearing as an object key (identifier followed by a colon at a
# property position). This is permissive, so it is gated HARD by the known-set: only keys
# whose name is a KNOWN user-defined type become references. Group 1 = the key name.
_RESOLVER_KEY_RE = re.compile(r'(?:[{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:')


def _gql_type_decls(text: str) -> tuple[set[str], set[str]]:
    """Pass 1: scan a GraphQL schema file and split its type-system declarations into:
      - base:     names declared with a BASE definition (`type X`, `input X`, …) — true DEFINERS.
      - extended: names declared with `extend …` ONLY in this file — CONTRIBUTIONS / references,
                  not independent definers.
    A name that has BOTH a base def and an `extend` in the SAME file is reported in `base` only
    (it is locally defined here). Stoplisted names are never returned. Comments are stripped first
    so a commented-out `# type Ghost {` / `# extend type Ghost {` never mints a ghost. Content-free:
    type names only.

    WHY split: the multi-definer guard suppresses a type DEFINED (alters) by >1 file. Counting an
    `extend type X` as a definer would make the legitimate {base, extension} pair look like an
    ambiguous multi-definer and DROP the whole type — a silent-miss for the very common federation /
    schema-stitching shape. Treating an extension as a REFERENCE (queries) keeps base+extension+all
    referencers coupled while leaving the genuine 'same bare `type X` in two files' case suppressed."""
    base: set[str] = set()
    extended: set[str] = set()
    text = _strip_comments(text)
    for m in _GQL_DEF_RE.finditer(text):
        is_extend = bool(m.group(1))
        name = m.group(3)
        if name.lower() in _GQL_STOPLIST:
            continue
        if is_extend:
            extended.add(name)
        else:
            base.add(name)
    # A name with a base def here is a definer here; drop it from the extension set (no double count).
    extended -= base
    return base, extended


def _gql_defined_types(text: str) -> set[str]:
    """All locally-declared GraphQL type names (base defs OR `extend`-only contributions).
    Used where we need 'every type name this file mentions as a declaration' (the known-set and
    the own-defs exclusion for the queries scan). The base/extension distinction (which controls
    DEFINER counting) is made by callers via _gql_type_decls(). Content-free: names only."""
    base, extended = _gql_type_decls(text)
    return base | extended


def _gql_anchored_references(text: str, ext: str, known: frozenset[str]) -> set[str]:
    """Pass 2: find ANCHORED references to known GraphQL type names in a file's text.
    Anchors (in priority of breadth):
      - a `.graphql`/`.gql` file body is itself an anchored GraphQL document → scan its
        bare identifiers (known-set gated);
      - a `gql`...`` / `graphql`...`` tagged template in any source file → scan its body;
      - a resolver-map key (`Order: { ... }`) whose name is a known type.
    Only names in `known` are returned — no new symbols can be minted here. Stoplisted
    identifiers are never returned. Content-free: names only."""
    found: set[str] = set()
    if not known:
        return found

    def _scan_body(body: str) -> None:
        for m in _GQL_IDENT_RE.finditer(body):
            ident = m.group(1)
            if ident.lower() in _GQL_STOPLIST:
                continue
            if ident in known:
                found.add(ident)

    # Anchor 1: the whole file is a GraphQL document (.graphql/.gql operation/fragment file).
    # Strip GraphQL `#` comments first so a type name mentioned only in a `# comment` line is
    # not counted as a reference (precision). The known-set gate already drops unknown tokens.
    if ext in _GRAPHQL_EXTS:
        _scan_body(_strip_comments(text))

    # Anchor 2: gql / graphql tagged-template literals inside source code. Extract from the
    # ORIGINAL text (a real template is code, not a comment); the body scan ignores the
    # template's own `#`/stoplisted tokens via the known-set gate.
    for m in _GQL_TAGGED_RE.finditer(text):
        _scan_body(m.group(1))

    # Anchor 3: resolver-map keys (`Order: { ... }`) in source code. Strip comments first so a
    # `// { Order: ... }` inside a comment cannot fabricate a resolver-key reference.
    for m in _RESOLVER_KEY_RE.finditer(_strip_comments(text)):
        key = m.group(1)
        if key.lower() in _GQL_STOPLIST:
            continue
        if key in known:
            found.add(key)

    return found


# ---------------------------------------------------------------------------
# protobuf / gRPC: definitions + anchored references
# ---------------------------------------------------------------------------

# A protobuf definition header: `message X`, `service X`, `enum X`. Group 1 = keyword,
# group 2 = the name. (rpc methods live inside services; we key coupling on the service /
# message / enum name, not individual rpc method names — those are not distinctive enough.)
_PROTO_DEF_RE = re.compile(
    r'(?:^|\n)\s*(message|service|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b',
)

# Proto symbols that collide with ubiquitous words → never mint / never reference (precision).
# proto names are usually distinctive PascalCase, but a `message Service` or `enum Error`
# would couple broadly. Matched case-INSENSITIVELY (`name.lower() in _PROTO_STOPLIST`).
#
# AUDIT (2026-06-20, measured on grpc/grpc-go): the proto REFERENCE side scans bare identifiers,
# so PascalCase proto message names that double as common library/std identifiers coupled large
# numbers of unrelated code files through one node:
#   - `State`  — Go connectivity/channel `State` (162 unrelated refs ⇒ ~80% of grpc-go's pairs).
#   - `Buffer` — `bytes.Buffer` (38 refs);   `Timer` — `time.Timer` (18 refs).
#   - `Stat`   — `os.Stat` (the audit example);   `Extension` — `x509`/proto2 ext;
#   - `Scope`/`Method`/`Node`/`Location`/`Server`/`Client`/`Handler`/`Config`/`Context` — common
#     across Go/std/grpc code; stoplisted as precision insurance (harmless where not a proto name).
# NOTE deliberately NOT stoplisted: `Point` and `Feature` — measured as the GENUINE route_guide
# crown-jewel (route_guide.proto ↔ server.go + client.go). Stoplisting them would be a recall
# regression (kill a real .proto↔client/server coupling), which violates the precision-first /
# recall-safe bar. The non-code-file scan exclusion below removes `Feature`'s one false ref
# (examples_test.sh) without touching the two genuine ones.
_PROTO_STOPLIST = frozenset({
    "service", "message", "enum", "error", "status", "result", "request", "response",
    "type", "data", "value", "key", "name", "id", "list", "map", "string", "int",
    "bool", "float", "double", "empty", "any", "timestamp",
    # measured high-collision PascalCase identifiers (CLASS B bare-identifier collisions):
    "state", "buffer", "timer",                       # dominant grpc-go false couplers
    "stat", "extension",                              # os.Stat / x509.Extension (audit examples)
    "scope", "method", "node", "location", "server",  # common Go/grpc/std identifiers
    "client", "handler", "config", "context",
})

# A reference to a proto symbol in client/server code: the symbol name appearing as a bare
# identifier. proto-generated stubs name the message/service type directly (e.g.
# `OrderServiceClient`, `CreateOrderRequest(...)`, `pb.CreateOrderRequest{}`). We scan bare
# identifiers and keep only those whose name is a KNOWN proto symbol — the known-set is the
# precision gate. The match also fires on an exact identifier embedded as a prefix in a
# generated stub name (`OrderServiceClient`) via a separate prefix scan below.
_PROTO_IDENT_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\b')

# Proto references are real ONLY in CODE that uses the generated stub (a Go/Py/Java/… file naming
# the message/service type). A proto symbol name appearing in a shell script, a markdown doc, a
# README, a JSON fixture, or an HTML page is NOT a stub use — it is prose / expected-output text /
# config and coupling on it is over-firing (the precise-silence bar). AUDIT (2026-06-20): grpc-go's
# `examples/examples_test.sh` contains the expected-output string `Feature: ...`, which coupled it
# to `api_message::Feature` (and similar `.md`/`.txt`/`.html` hits). We restrict proto reference
# scanning to recognized CODE extensions. This is an ALLOWLIST (default-deny) so an unknown/data
# extension never fabricates a proto coupling; .proto-to-.proto references are handled separately
# (those are real imports/uses, scanned regardless of this set).
_PROTO_REF_CODE_EXTS = frozenset({
    ".go", ".py", ".pyi", ".java", ".kt", ".kts", ".scala", ".rb", ".rs",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".m", ".mm",
    ".cs", ".swift", ".php", ".dart", ".ex", ".exs", ".erl", ".clj", ".cljs",
})


def _proto_defined_symbols(text: str) -> dict[str, str]:
    """Pass 1: scan a .proto file for message/service/enum definitions.
    Returns {name: kind} where kind ∈ {"api_message", "api_service"}:
      - `message X`  → api_message
      - `enum X`     → api_message  (enums are message-style value carriers)
      - `service X`  → api_service
    Stoplisted names are skipped. Comments are stripped first so a commented-out
    `// message Ghost {` never mints a ghost node. Content-free: names only."""
    found: dict[str, str] = {}
    text = _strip_comments(text)
    for m in _PROTO_DEF_RE.finditer(text):
        kw = m.group(1).lower()
        name = m.group(2)
        if name.lower() in _PROTO_STOPLIST:
            continue
        kind = "api_service" if kw == "service" else "api_message"
        # First definition wins the kind (a name should not be both, but be deterministic).
        found.setdefault(name, kind)
    return found


def _proto_references(text: str, known: frozenset[str]) -> set[str]:
    """Pass 2: find references to known proto symbol names in a code file's text.
    A reference is the EXACT known name as a bare identifier, OR the known name as a
    PascalCase PREFIX of a generated-stub identifier (`OrderService` → `OrderServiceClient`,
    `OrderServiceServer`, `OrderServiceStub`). Only names in `known` (exact or as a prefix)
    are returned — no new symbols can be minted here. Stoplisted identifiers are skipped.
    Content-free: names only."""
    found: set[str] = set()
    if not known:
        return found
    for m in _PROTO_IDENT_RE.finditer(text):
        ident = m.group(1)
        if ident in known:
            found.add(ident)
            continue
        # Generated-stub suffix forms: a known service/message name followed by a common
        # gRPC stub suffix. Only fire when the ident is a known name with a known stub
        # suffix appended (precision: arbitrary `OrderXyz` does not match).
        #
        # PERF (AUDIT 2026-06-20): this previously looped the ENTIRE `known` set per ident
        # (startswith + suffix check) => O(idents * known). On large proto repos (googleapis:
        # known ~= 23k) one build_graph extrapolated to ~41 min; ~15 s per file at known=20k.
        # We INVERT the match: strip each FIXED stub suffix (a small set, ~9) off the ident and
        # test exact membership of the base in `known`. This is O(idents * suffixes) = O(idents),
        # independent of `known` size. It is BYTE-IDENTICAL: a stub match requires the ident to
        # equal base+suffix with base in `known` and suffix in _PROTO_STUB_SUFFIXES, and (proven)
        # no suffix is a proper string-suffix of another, so at most ONE such (base, suffix) split
        # exists for any ident — i.e. the old loop's set-iteration order never affected the result,
        # and this finds that same unique base. `len(ident) > len(suf)` keeps base non-empty (the
        # old `ident != known_name` guard); exact matches are already handled above.
        for suf in _PROTO_STUB_SUFFIXES:
            if ident.endswith(suf) and len(ident) > len(suf):
                base = ident[: -len(suf)]
                if base in known:
                    found.add(base)
                    break
    return found


# Common gRPC generated-stub suffixes appended to a service/message name.
_PROTO_STUB_SUFFIXES = frozenset({
    "Client", "Server", "Stub", "Service", "Servicer", "Handler",
    "Request", "Response", "Reply",
})


# ---------------------------------------------------------------------------
# MULTI-DEFINER effective-adjacency suppression — shared by both substrates
# ---------------------------------------------------------------------------

def _suppress_multi_definer(
    nodes: list,
    edges: list,
    incomplete_paths_out=None,
) -> tuple[list, list]:
    """Retain ambiguous api_* nodes and their edges as inert evidence.

    WHY (AUDIT 2026-06-20, measured on hasura/graphql-engine): the cross-file coupling rule
    (db/schema/70_social.sql `res_adj`) couples two files when BOTH have an alters/queries edge to
    the SAME node. A contract symbol DEFINED in multiple files (the SAME `type X`/`input X` or
    `message X` appearing in several schema/proto files — test fixtures, per-package duplicates) is
    AMBIGUOUS: the definitions are different contracts that merely share a name, not one shared
    contract. Without this guard every pair of those definers (and any referencer) coupled falsely
    (~1/3 of hasura's coupling NODES were multi-definer). This mirrors the single-definer
    `n<=1 active / ambiguous inert` discipline used elsewhere.

    RECALL: a genuine single-definer→N-referencer contract (the crown jewel: one `.graphql`
    schema's `type Order`, or `route_guide.proto`'s `Point`) has exactly ONE definer and is
    UNTOUCHED. Recall cost is ≈0 — a symbol defined in >1 file was never one shared contract.

    The resource still exists as an observed, explicitly ambiguous contract
    fact, so its node is retained and marked for provenance/observability.
    Its existing ``alters``/``queries`` evidence is retained with
    ``reference_status="ambiguous"``. Persistence can therefore account for the
    uncertainty without effective adjacency coupling any of the candidates.
    Content-free, never-crash (pure list filtering)."""
    definers: dict[str, set[str]] = {}
    for e in edges:
        if e.get("kind") == "alters":
            dst = e.get("dst")
            src = e.get("src")
            if isinstance(dst, str):
                definers.setdefault(dst, set()).add(src)
    ambiguous = {dst for dst, srcs in definers.items() if len(srcs) > 1}
    if not ambiguous:
        return nodes, edges
    kept_nodes = []
    for node in nodes:
        if node.get("id") in ambiguous:
            node = {**node, "ambiguous": True}
        kept_nodes.append(node)
    kept_edges = [
        ({**edge, "reference_status": "ambiguous"}
         if edge.get("dst") in ambiguous else edge)
        for edge in edges
    ]
    return kept_nodes, kept_edges


# ---------------------------------------------------------------------------
# CROSS-REPO consumer-key references (feasibility-spike STEP 1) — flag-gated OFF, ADDITIVE.
# ---------------------------------------------------------------------------
# The within-repo path gates EVERY reference behind the same-repo known-set (`known` = names this
# repo DEFINED). A CONSUMER reference to a GraphQL type / proto service DEFINED IN ANOTHER REPO is
# therefore dropped. For cross-repo, the consumer emits a `queries` edge to the STABLE id even with
# no local definition, so it couples to the PRODUCER repo's `alters` edge for the same id via the
# relaxed res_adj. PRECISION (same discipline as routes / the openapi cross-repo path): we emit only
# ANCHORED references that pass a self-contained floor (a distinctive PascalCase name in a real
# anchor — a gql/graphql template, a resolver-map key, or a generated-stub form — NOT in the
# stoplist). The reference couples NOTHING on its own — only a PRODUCER `alters` for the same id
# produces a res_adj coupling — so the producer side (+ its multi-definer guard) is the precision
# filter at join time. content-free: names only.

# Minimum length for a cross-repo contract-name reference (a 1-2 char token is never a distinctive
# GraphQL type / proto message and would over-emit).
_MIN_XREPO_NAME_LEN = 3


def _gql_xrepo_references(text: str, ext: str) -> set[str]:
    """Cross-repo: anchored GraphQL type-name references WITHOUT a known-set — distinctive PascalCase
    names appearing inside a real GraphQL anchor (a `.graphql`/`.gql` body, a gql/graphql tagged
    template, or a resolver-map key) that are NOT stoplisted and are ≥ _MIN_XREPO_NAME_LEN. The
    producer `alters` (+ multi-definer guard) filters at join time. content-free: names only."""
    found: set[str] = set()

    def _keep(ident: str) -> bool:
        # Distinctive type-name floor: PascalCase (leading uppercase), ≥ min len, not stoplisted. A
        # lowercase field name (`order`, `id`) or a stoplisted/ubiquitous word never anchors a couple.
        return (len(ident) >= _MIN_XREPO_NAME_LEN
                and ident[:1].isupper()
                and ident.lower() not in _GQL_STOPLIST)

    def _scan_body(body: str) -> None:
        for m in _GQL_IDENT_RE.finditer(body):
            ident = m.group(1)
            if _keep(ident):
                found.add(ident)

    if ext in _GRAPHQL_EXTS:
        _scan_body(_strip_comments(text))
    for m in _GQL_TAGGED_RE.finditer(text):
        _scan_body(m.group(1))
    for m in _RESOLVER_KEY_RE.finditer(_strip_comments(text)):
        key = m.group(1)
        if _keep(key):
            found.add(key)
    return found


def _proto_xrepo_references(text: str) -> set[str]:
    """Cross-repo: anchored proto symbol references WITHOUT a known-set — a distinctive PascalCase
    name used in a generated-STUB form (`<Name>Client`/`<Name>Server`/`<Name>Stub`/…) or as a
    constructor/struct-literal (`<Name>{` / `<Name>(`), NOT stoplisted, ≥ _MIN_XREPO_NAME_LEN. The
    stub-suffix form is the most distinctive proto-use anchor; the producer `alters` filters at join
    time. content-free: names only."""
    found: set[str] = set()
    for m in _PROTO_IDENT_RE.finditer(text):
        ident = m.group(1)
        # Generated-stub form: strip a known stub suffix, keep the base if distinctive.
        for suf in _PROTO_STUB_SUFFIXES:
            if ident.endswith(suf) and len(ident) > len(suf):
                base = ident[: -len(suf)]
                if (len(base) >= _MIN_XREPO_NAME_LEN and base[:1].isupper()
                        and base.lower() not in _PROTO_STOPLIST):
                    found.add(base)
                break
    # Constructor / struct-literal use: `Name(` or `Name{` for a distinctive PascalCase name.
    for m in re.finditer(r'(?<![A-Za-z0-9_.])([A-Z][A-Za-z0-9_]*)\s*[\({]', text):
        ident = m.group(1)
        if len(ident) >= _MIN_XREPO_NAME_LEN and ident.lower() not in _PROTO_STOPLIST:
            found.add(ident)
    return found


# ---------------------------------------------------------------------------
# Public entry point — called by build_graph (mirrors _iac_graph wiring)
# ---------------------------------------------------------------------------

def _api_contract_graph(
    root: str,
    source_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> tuple[list, list]:
    """Return (api_nodes, api_edges). Two substrates:

      - GraphQL (.graphql/.gql): user-defined type/interface/input/enum/union/scalar
        definitions → cross-file ANCHORED references (gql tagged templates, operation
        files, resolver-map keys). Two-pass known-set precision + stoplist.
      - protobuf/gRPC (.proto): message/service/enum definitions → cross-file references
        from client/server code (exact name or generated-stub-prefixed). Two-pass
        known-set precision + stoplist.

    `source_files`: the (abs_path, ext) list already filtered by build_graph's guards
    (size/binary/generated/symlink caps). DEFINITIONS come from .graphql/.gql/.proto files;
    REFERENCES come from any source file (code or another schema file). Only the relevant
    files are read.

    New NODE kinds: `api_type` (GraphQL), `api_message` / `api_service` (protobuf).
    REUSED EDGE kinds: `alters` (definer → contract node), `queries` (referencer → contract
    node) — so the existing shared-resource adjacency couples the two with NO SQL change.

    Content-free: contract symbol NAMES + file paths only — never field values, message
    bodies, schema field definitions, or comment content.
    Never-crash: all file reads and scans are guarded; each substrate is isolated so one
    failing does not abort the other.
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    try:
        g_nodes, g_edges = _graphql_graph(
            root, source_files, incomplete_paths_out=incomplete_paths_out
        )
        nodes.extend(g_nodes)
        edges.extend(g_edges)
    except Exception:
        # GraphQL references may live in any guarded source file.  A substrate
        # failure makes the whole candidate surface incomplete, not Clear.
        if any(ext in _GRAPHQL_EXTS for _path, ext in source_files):
            for abs_path, _ext in source_files:
                _mark_incomplete(
                    incomplete_paths_out,
                    os.path.relpath(abs_path, root).replace(os.sep, "/"),
                )

    try:
        p_nodes, p_edges = _proto_graph(
            root, source_files, incomplete_paths_out=incomplete_paths_out
        )
        nodes.extend(p_nodes)
        edges.extend(p_edges)
    except Exception:
        has_proto = any(ext == _PROTO_EXT for _path, ext in source_files)
        if has_proto:
            for abs_path, ext in source_files:
                if ext == _PROTO_EXT or ext in _PROTO_REF_CODE_EXTS:
                    _mark_incomplete(
                        incomplete_paths_out,
                        os.path.relpath(abs_path, root).replace(os.sep, "/"),
                    )

    return nodes, edges


# _read_capped + _MAX_SCAN_BYTES now live in the shared leaf _cg_io.py (imported above).


def _graphql_graph(
    root: str,
    source_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> tuple[list, list]:
    """GraphQL substrate: api_type nodes + alters/queries edges."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # Collect (abs, rel, ext, text) for every file we might read. DEFINITIONS only come from
    # .graphql/.gql; REFERENCES can come from any source file (code or schema). We read the
    # schema files always, and code files only if there is at least one schema file (no
    # definitions → no known set → references can't survive the guard → skip the read).
    schema_files: list[tuple[str, str, str]] = []   # (rel, ext, text)
    other_files: list[tuple[str, str, str]] = []     # (rel, ext, text) — read lazily below
    schema_abs: list[tuple[str, str]] = []           # (abs, rel)
    other_abs: list[tuple[str, str, str]] = []       # (abs, rel, ext)

    for abs_path, ext in source_files:
        rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
        if ext in _GRAPHQL_EXTS:
            schema_abs.append((abs_path, rel))
        else:
            other_abs.append((abs_path, rel, ext))

    if not schema_abs:
        return nodes, edges   # no GraphQL schema files → nothing to define

    # --- Pass 1: collect all declared type names + which file declares each ---
    # A name becomes KNOWN (mints a node) if ANY schema file declares it with a base def OR an
    # `extend` (so a base+extension pair, or even an extension whose base is in-repo, is coupleable).
    # The node `path` (def-site metadata, content-free) prefers a BASE definer over an extension.
    all_defined: dict[str, str] = {}     # type name -> rel chosen for the node's def path
    has_base_path: set[str] = set()       # names whose chosen path is a base def (so we don't downgrade)
    capped = False
    definition_loss = set()
    for abs_path, rel in schema_abs:
        text = _read_capped(abs_path, definition_loss, rel)
        if text is None:
            continue
        schema_files.append((rel, ".graphql", text))
        base_here, ext_here = _gql_type_decls(text)
        for name in base_here:
            if name not in all_defined and len(all_defined) >= _MAX_API_NODES:
                capped = True
                _mark_incomplete(incomplete_paths_out, rel)
                continue
            # base def: claim the path (and upgrade an earlier extension-only path to this base).
            if name not in all_defined or name not in has_base_path:
                all_defined[name] = rel
                has_base_path.add(name)
        for name in ext_here:
            if name not in all_defined:
                if len(all_defined) >= _MAX_API_NODES:
                    capped = True
                    _mark_incomplete(incomplete_paths_out, rel)
                    continue
                all_defined[name] = rel   # extension-only path (may be upgraded by a later base def)
    if capped or definition_loss:
        # A dropped definition may be referenced by any GraphQL/code candidate.
        # Conservatively prevent unaffected-looking consumers from becoming Clear.
        for _abs_path, rel in schema_abs:
            _mark_incomplete(incomplete_paths_out, rel)
        for _abs_path, rel, _ext in other_abs:
            _mark_incomplete(incomplete_paths_out, rel)

    if not all_defined:
        return nodes, edges

    known = frozenset(all_defined)

    # Mint api_type nodes (one per name; first-definer wins the path).
    type_nodes: dict[str, dict] = {}
    for name, def_rel in all_defined.items():
        n = {"id": f"api_type::{name}", "kind": "api_type",
             "name": name, "path": def_rel, "language": "graphql"}
        type_nodes[name] = n
        nodes.append(n)

    # --- alters edges (DEFINERS) + extension queries edges (CONTRIBUTIONS) per schema file ---
    # A BASE def (`type X`) emits `alters` (it DEFINES the contract → counts toward the multi-definer
    # guard). An `extend X` is NOT a definer: it emits `queries` (it CONTRIBUTES to / references a type
    # whose base lives elsewhere), so a {base, extension} pair couples through the node WITHOUT the
    # extension inflating the definer count (which would falsely suppress the whole type). Per-file
    # dedup so a name declared twice in one file does not emit duplicate edges.
    for rel, ext, text in schema_files:
        base_here, ext_here = _gql_type_decls(text)
        for name in base_here:
            if name in type_nodes:
                edges.append({"src": rel, "dst": f"api_type::{name}", "kind": "alters"})
        for name in ext_here:
            if name in type_nodes:
                edges.append({"src": rel, "dst": f"api_type::{name}", "kind": "queries"})

    # --- queries edges: anchored references in schema files (other than own defs) + code ---
    # Schema-file references (e.g. a fragment file referencing a type defined elsewhere). We exclude
    # only this file's OWN declarations (base or extension) so the body scan of an `extend X { … }`
    # block does not emit a SECOND queries edge for X (the extension already got one above).
    for rel, ext, text in schema_files:
        defs_here = _gql_defined_types(text)
        refs = _gql_anchored_references(text, ext, known)
        for name in refs:
            if name not in defs_here and name in type_nodes:
                edges.append({"src": rel, "dst": f"api_type::{name}", "kind": "queries"})

    # Code-file references (gql tagged templates, resolver maps). Read lazily here.
    # CROSS-REPO (flag-gated OFF): also emit a `queries` edge to api_type::<Name> for an anchored
    # reference whose definition is NOT in THIS repo (local_type_ids), so it couples to a PRODUCER
    # repo's `alters` for the same id via the relaxed res_adj. Skipped entirely when the flag is OFF
    # (within-repo output byte-identical). content-free: names only.
    _xrepo_on = cross_repo_keys_enabled()
    local_type_ids = {f"api_type::{n}" for n in all_defined} if _xrepo_on else frozenset()
    for abs_path, rel, ext in other_abs:
        text = _read_capped(abs_path, incomplete_paths_out, rel)
        if text is None:
            continue
        refs = _gql_anchored_references(text, ext, known)
        for name in refs:
            if name in type_nodes:
                edges.append({"src": rel, "dst": f"api_type::{name}", "kind": "queries"})
        if _xrepo_on:
            for name in _gql_xrepo_references(text, ext):
                dst = f"api_type::{name}"
                if dst not in local_type_ids:
                    edges.append({"src": rel, "dst": dst, "kind": "queries"})

    # --- PRECISION: mark MULTI-DEFINER GraphQL evidence inert ---
    # The SAME `type X`/`input X`/… defined in more than one .graphql file (test fixtures, e.g.
    # `SampleInput` defined in 5 files in hasura) is ambiguous — N different schemas that share a
    # name, not one shared contract — and all their definers would otherwise couple through the one
    # `api_type::X` node. Mark their retained edges inert. A single-definer type↔resolver/operation
    # crown-jewel (n_def==1) is untouched. See _suppress_multi_definer().
    nodes, edges = _suppress_multi_definer(
        nodes, edges, incomplete_paths_out=incomplete_paths_out
    )

    return nodes, edges


def _proto_graph(
    root: str,
    source_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> tuple[list, list]:
    """protobuf/gRPC substrate: api_message/api_service nodes + alters/queries edges."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    proto_abs: list[tuple[str, str]] = []           # (abs, rel)
    other_abs: list[tuple[str, str, str]] = []       # (abs, rel, ext) — code reference candidates

    for abs_path, ext in source_files:
        rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
        if ext == _PROTO_EXT:
            proto_abs.append((abs_path, rel))
        elif ext in _PROTO_REF_CODE_EXTS:
            # Only CODE files can be stub-use referencers. Non-code files (.sh/.md/.txt/.html/.json/
            # …) are excluded BEFORE the read so a proto name in prose / expected-output / config
            # never fabricates a coupling (precision; audit found examples_test.sh ↔ Feature).
            other_abs.append((abs_path, rel, ext))

    if not proto_abs:
        return nodes, edges

    # --- Pass 1: collect all defined proto symbols + their kinds + defining file ---
    all_defined: dict[str, str] = {}          # name -> first rel defining it
    all_kind: dict[str, str] = {}             # name -> kind (api_message / api_service)
    proto_texts: list[tuple[str, str]] = []   # (rel, text)
    capped = False
    definition_loss = set()
    for abs_path, rel in proto_abs:
        text = _read_capped(abs_path, definition_loss, rel)
        if text is None:
            continue
        proto_texts.append((rel, text))
        for name, kind in _proto_defined_symbols(text).items():
            if name not in all_defined:
                if len(all_defined) >= _MAX_API_NODES:
                    capped = True
                    _mark_incomplete(incomplete_paths_out, rel)
                    continue
                all_defined[name] = rel
                all_kind[name] = kind
    if capped or definition_loss:
        for _abs_path, rel in proto_abs:
            _mark_incomplete(incomplete_paths_out, rel)
        for _abs_path, rel, _ext in other_abs:
            _mark_incomplete(incomplete_paths_out, rel)

    if not all_defined:
        return nodes, edges

    known = frozenset(all_defined)

    # Mint proto symbol nodes (one per name; first-definer wins path + kind).
    sym_nodes: dict[str, dict] = {}
    for name, def_rel in all_defined.items():
        kind = all_kind[name]
        n = {"id": f"{kind}::{name}", "kind": kind,
             "name": name, "path": def_rel, "language": "protobuf"}
        sym_nodes[name] = n
        nodes.append(n)

    # --- alters edges: every symbol defined in each .proto file ---
    for rel, text in proto_texts:
        defs_here = _proto_defined_symbols(text)
        for name in defs_here:
            if name in sym_nodes:
                kind = all_kind[name]
                edges.append({"src": rel, "dst": f"{kind}::{name}", "kind": "alters"})

    # --- queries edges: references in OTHER .proto files (import/use) ---
    for rel, text in proto_texts:
        defs_here = _proto_defined_symbols(text)
        refs = _proto_references(_strip_comments(text), known)
        for name in refs:
            if name not in defs_here and name in sym_nodes:
                kind = all_kind[name]
                edges.append({"src": rel, "dst": f"{kind}::{name}", "kind": "queries"})

    # --- queries edges: references in CODE files (client/server / generated-stub usage) ---
    # other_abs is already restricted to code extensions (_PROTO_REF_CODE_EXTS) so non-code files
    # cannot fabricate a coupling. Strip comments first: a proto symbol name that appears ONLY in a
    # comment is prose, not a real stub use — coupling on it would be over-firing (precise-silence).
    # CROSS-REPO (flag-gated OFF): also emit a `queries` edge for an anchored proto-symbol reference
    # whose definition is NOT in THIS repo, so it couples to a PRODUCER repo's `alters` for the same id
    # via the relaxed res_adj. A cross-repo reference has no local node → no local kind, so it is keyed
    # as api_message:: (the message form; a producer that defined it as a service mints api_service::,
    # and the stub-suffix anchor — <Name>Client/Server — is exactly the service-use form, so we ALSO
    # emit the api_service:: id for stub-form refs to cover both). Skipped when the flag is OFF.
    _xrepo_on = cross_repo_keys_enabled()
    local_sym_ids = ({f"api_message::{n}" for n in all_defined}
                     | {f"api_service::{n}" for n in all_defined}) if _xrepo_on else frozenset()
    for abs_path, rel, ext in other_abs:
        text = _read_capped(abs_path, incomplete_paths_out, rel)
        if text is None:
            continue
        refs = _proto_references(_strip_comments(text), known)
        for name in refs:
            if name in sym_nodes:
                kind = all_kind[name]
                edges.append({"src": rel, "dst": f"{kind}::{name}", "kind": "queries"})
        if _xrepo_on:
            # A cross-repo ref's PRODUCER may have defined the name as a service OR a message; emit BOTH
            # candidate ids (only the one a producer actually `alters` will couple via res_adj — the
            # other lands as a harmless no-coupling edge). content-free: names only.
            for name in _proto_xrepo_references(_strip_comments(text)):
                for dst in (f"api_message::{name}", f"api_service::{name}"):
                    if dst not in local_sym_ids:
                        edges.append({"src": rel, "dst": dst, "kind": "queries"})

    # --- PRECISION: mark MULTI-DEFINER proto evidence inert ---
    # When the SAME name is DEFINED (`message X`/`service X`/`enum X`) in MORE THAN ONE .proto file
    # it is ambiguous — the two (or N) definitions are different contracts that happen to share a
    # name (test fixtures, per-package duplicates), not one shared contract. Letting their definers
    # (and any referencers) couple through the single `api_*::X` node fabricates a coupling. We retain
    # the node and all alters/queries evidence with an ambiguous status, mirroring
    # the single-definer `n<=1 active / ambiguous inert` discipline. A genuine 1-definer→N-referencer
    # contract (the route_guide crown-jewel) is unaffected (n_def==1). proto names are unique per
    # package so this is rarely hit in real code — it is a correctness guard symmetric with the
    # GraphQL one below. (Recall cost ≈ 0: a name defined in >1 .proto was never a single shared
    # contract.) See `nodes`/`edges` annotation via _suppress_multi_definer().
    nodes, edges = _suppress_multi_definer(
        nodes, edges, incomplete_paths_out=incomplete_paths_out
    )

    return nodes, edges
