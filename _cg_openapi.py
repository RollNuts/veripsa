"""OpenAPI / Swagger REST-contract cross-substrate coupling extraction.

Owns the OPENAPI-CONTRACT GRAPH substrate: REST operations and component schemas DEFINED in
an OpenAPI 3 / Swagger 2 spec (.yaml/.yml/.json) and REFERENCED from a backend handler and a
frontend API client — coupled with NO import/call edge between them.

WHY THIS MATTERS (the crown-jewel coupling code-only tools structurally miss):
Two files that touch the SAME REST contract — an operation `getUserById` (or path `/users/{id}`)
DEFINED in an `openapi.yaml` and IMPLEMENTED by a backend handler whose function is named
`getUserById`, and CALLED by a frontend client that hits the `/users/:id` route — are coupled
with NO code edge, NO import, NO call. The spec is its own little document (often a different
language/dir from both the server and the client), so neither the call graph nor the import graph
can see it. Editing the spec's `/users/{id}` operation therefore silently couples to the handler
and the API client. This module surfaces that by recovering the named OPERATION / SCHEMA shared
across files. It mirrors _cg_api_contract / _cg_iac / _cg_schema EXACTLY: the DEFINING spec file
emits an `alters` edge to the contract node; a REFERENCING code file emits a `queries` edge; the
two then couple through the shared node (REUSING the existing alters/queries shared-resource
adjacency in the contention SQL — zero SQL changes).

CONTENT-FREE: operation / schema / path NAMES only (`api_operation::getUserById`,
`api_operation::GET /users/{}`, `api_schema::Order`). NEVER descriptions, examples, default
values, secrets, or any body content. An operationId / schema-name / path is pure structural
metadata.

PRECISION STRATEGY (two-pass, mirrors _cg_api_contract):
  Pass 1: parse every SPEC-MARKED file → the locally-defined operation / schema names → the
          known-set. A file is a spec ONLY when its parsed top level carries `openapi:` or
          `swagger:` (mirrors _cg_config._is_json_schema_file discipline) — an ordinary
          yaml/json is left entirely to _cg_config (additive: zero api_operation nodes).
  Pass 2: scan CODE files for ANCHORED references to KNOWN names only. A reference that does
          not match a locally-defined operation/schema/path is ignored. References are only
          emitted from ANCHORED contexts so a random token in prose cannot mint a coupling.
          Veripsa's quality bar is PRECISE SILENCE: over-firing = wallpaper. We UNDER-emit
          references rather than fabricate couplings; recall-safe here means never DROP a real
          DEFINITION, not match every token.

DEFINITIONS (from the spec):
  - operations: for each path under `paths:`, for each HTTP method (get/post/put/patch/delete),
    mint an `api_operation` node. PREFER `operationId` (most precise). With no operationId, use a
    normalized `METHOD path` with path params collapsed to a placeholder (`/users/{id}` and
    `/users/{userId}` → `GET /users/{}`). The spec file gets an `alters` edge to each operation.
  - schemas: each name under `components/schemas` (OpenAPI 3) or `definitions` (Swagger 2) →
    `api_schema` node; the spec file gets an `alters` edge.

REFERENCES (from code — PRECISION-FIRST):
  - operationId reference: a code file in which the operationId is a DEFINED function name or an
    exported identifier (anchored: a `def`/`function`/`func` name, an `export`, or an assignment
    to that identifier). operationIds are distinctive → a good anchor.
  - path reference: a code file containing a route/path STRING LITERAL matching a spec path
    (`{id}`/`:id` param styles normalized before comparing). Ultra-generic paths are stoplisted
    (`/health`,`/ping`,`/status`,`/metrics`,`/`).
  - schema reference: a code file using the schema NAME as a type/identifier — BUT schema names
    are often generic, so ubiquitous names are stoplisted and very-short names are skipped; in
    doubt, SKIP (under-emit references rather than fabricate couplings).
  - comments are stripped on the reference side so a path/name in a comment never couples.

YAML PARSING: pinned PyYAML (`import yaml`) is the production parser. A BOUNDED line-based
key scanner remains as defense-in-depth for constrained/offline callers and recovers the
structural keys we need (top-level `openapi:`/`swagger:` marker, `paths:` entries, `operationId:` values,
`components:`→`schemas:` / `definitions:` names). Content-free, never-crash, fail-soft to empty on
any parse error. We prefer robustness over completeness. JSON specs always use stdlib `json`.

ROUTING: `.yaml`/`.yml` are in _CONFIG_EXTS and `.json` is too → already walked into config_files
(guard-filtered). build_graph hands source_files + config_files to this pass; we CONSUME only
spec-marked files for DEFINITIONS and leave every ordinary yaml/json to _cg_config untouched.
REFERENCES come from any code source file already in source_files. This module ONLY reads files
explicitly handed to it.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from _cg_languages import _sfc_executable_text
from _cg_xrepo import cross_repo_keys_enabled  # cross-repo consumer-key emission flag (default OFF)

# ---------------------------------------------------------------------------
# Extensions this substrate cares about
# ---------------------------------------------------------------------------

# Candidate spec extensions (only those carrying the openapi/swagger marker are treated as specs).
_SPEC_EXTS = frozenset({".yaml", ".yml", ".json"})

# OpenAPI references are meaningful only in executable code.  This is deliberately a
# default-deny allowlist: stylesheets, HTML/templates, shell scripts, docs and data may
# contain a schema-looking PascalCase token or route-shaped string without using the API.
# Single-file components are admitted below, but only their executable regions are scanned.
_OPENAPI_REF_CODE_EXTS = frozenset({
    ".go", ".py", ".pyi", ".pyx", ".java", ".kt", ".kts", ".scala", ".rb", ".rs",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".m", ".mm",
    ".cs", ".swift", ".php", ".dart", ".ex", ".exs", ".erl", ".clj", ".cljs",
    ".fs", ".fsx", ".vb", ".hs", ".lhs", ".elm", ".purs", ".ml", ".mli",
    ".re", ".rei", ".lua", ".tcl", ".pl", ".pm", ".raku", ".r", ".jl",
    ".nim", ".cr", ".d", ".zig", ".v", ".astro", ".svelte", ".vue",
})

# HTTP methods that mint an operation under a path item. Other path-item keys (parameters,
# summary, description, servers, $ref, …) are NOT operations.
_HTTP_METHODS = ("get", "put", "post", "delete", "patch")

# Cap on the bytes scanned from a single file. build_graph already bounds file size to 1.5 MB;
# we additionally cap the scanned slice so an adversarial file under the size cap cannot blow up
# the scan. Real specs / source files are well under this.
from _cg_io import (  # shared bounded reader + loss diagnostics
    _mark_incomplete,
    _read_capped,
)

# Cap on distinct api_operation/api_schema nodes minted per repo (adversarial-flood guard). Real
# specs rarely exceed a few thousand operations + schemas.
_MAX_API_NODES = 20_000

# Cap on the number of lines the YAML line-scanner fallback walks (bounded never-crash).
_MAX_SCAN_LINES = 200_000

# ---------------------------------------------------------------------------
# Stoplists (precision)
# ---------------------------------------------------------------------------

# Ultra-generic paths: present in nearly every service → would couple everything. Never mint an
# operation node FROM them via the normalized "METHOD path" form, and never treat a path literal
# equal to one of these as a reference. (operationId-based operations on these paths are still fine
# — an operationId is distinctive.) Compared on the NORMALIZED path (params already collapsed).
_PATH_STOPLIST = frozenset({
    "/", "/health", "/healthz", "/ping", "/status", "/metrics", "/ready", "/readyz",
    "/live", "/livez", "/version", "/", "",
})

# Minimum length of a normalized single-segment path's segment to be a usable coupling anchor. A
# path like `/a`, `/b`, `/v1`, `/api` is a single ultra-short segment present in countless unrelated
# services → coupling through it is wallpaper (real-repo audit on go-swagger: `/a` couples unrelated
# fixtures). Treat such paths like the explicit _PATH_STOPLIST: never mint a normalized "METHOD path"
# operation FROM them and never let a path literal equal to one couple. (An operationId on such a
# path is still distinctive → unaffected; only the path-derived coupling is suppressed.) "Single
# segment" = exactly one `/` at index 0 and no other `/`; "ultra-short" = segment length ≤ this.
_MIN_PATH_SEGMENT_LEN = 2


def _is_generic_path(norm: str) -> bool:
    """True when a NORMALIZED path is too generic to anchor a coupling: it is in the explicit
    stoplist, OR it is a single ultra-short segment (`/a`, `/b`, length ≤ _MIN_PATH_SEGMENT_LEN
    after the leading slash). Content-free (structure only); never-crash (pure string ops)."""
    if not norm or norm in _PATH_STOPLIST:
        return True
    # single-segment: one leading '/', no other '/'
    if norm.startswith("/") and norm.count("/") == 1:
        seg = norm[1:]
        if len(seg) <= _MIN_PATH_SEGMENT_LEN:
            return True
    return False


# Ubiquitous schema names: generic carriers that would couple broadly. Never mint as a reference
# anchor target via the schema-name path. (They STILL mint api_schema NODES if the spec defines
# them — a definition is honest — but a code identifier equal to one of these never couples, and we
# do not even add them to the reference known-set.) Compared case-insensitively.
# TWO families: (a) DATA-SHAPE words (Error/Response/Data/Model …) — the original set; and
# (b) INFRASTRUCTURE / ROLE identifiers (Server/API/Client/Config/Handler/Service …) added after a
# real-repo audit (go-swagger): a codegen `Server` struct, `http.Server`, an `API`/`Config` type
# appear in many unrelated files and were coupling them through an `api_schema::Server` node.
_SCHEMA_STOPLIST = frozenset({
    # (a) data-shape words
    "error", "response", "request", "result", "status", "data", "object", "model",
    "input", "output", "success", "empty", "item", "items", "list", "page", "meta",
    "id", "type", "value", "name", "key", "info", "detail", "details", "message",
    "payload", "body", "entity", "record", "field", "node", "edge", "count",
    # (b) infrastructure / role identifiers (audit: false couples via these on real repos)
    "server", "api", "client", "config", "handler", "service", "context",
    "options", "manager", "logger", "reader", "writer",
})

# Minimum length for a schema name to be eligible as a reference anchor (single-/two-char names
# like `T`, `Id`, `Ok` are too generic to anchor a coupling precisely).
_MIN_SCHEMA_REF_LEN = 3

# ---------------------------------------------------------------------------
# Comment stripping (reference side only) — mirrors _cg_api_contract._strip_comments
# ---------------------------------------------------------------------------

# Blank out // line, # line, and /* block */ comments so a path/name appearing ONLY in a comment
# never fabricates a coupling. Content-free: we only remove text before NAME/path matching; we
# never read or emit comment content. Replacement with spaces preserves offsets; never-crash.
_LINE_COMMENT_RE = re.compile(r'(//|#)[^\n]*')
_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)


def _strip_comments(text: str) -> str:
    """Blank out // line, # line, and /* block */ comments (replace with spaces, keeping
    newlines). Used on the REFERENCE side only. Never-crash (pure regex sub)."""
    def _blank(m: "re.Match[str]") -> str:
        return "".join("\n" if c == "\n" else " " for c in m.group(0))
    text = _BLOCK_COMMENT_RE.sub(_blank, text)
    text = _LINE_COMMENT_RE.sub(_blank, text)
    return text


# ---------------------------------------------------------------------------
# Spec parsing — structured (PyYAML / json) with a bounded line-scanner fallback
# ---------------------------------------------------------------------------

# _read_capped + _MAX_SCAN_BYTES now live in the shared leaf _cg_io.py (imported above).


def _try_parse_structured(text: str, ext: str) -> "dict | None":
    """Parse `text` into a Python dict using json (for .json) or PyYAML (for .yaml/.yml).
    Returns the dict, or None when parsing is unavailable (PyYAML absent) or fails / the top
    level is not a mapping. Never-crash."""
    ext = ext.lower()
    if ext == ".json":
        try:
            obj = json.loads(text)
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None
    # .yaml / .yml — production pins PyYAML; fallback still handles constrained callers.
    try:
        import yaml
    except ImportError:
        return None
    try:
        obj = yaml.safe_load(text)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _normalize_path(path: str) -> str:
    """Collapse path parameters to a single placeholder so the spec form `/users/{id}` and the
    code forms `/users/{userId}` (brace), `/users/:id` (express/rails colon), and `` `/users/${id}` ``
    (JS/TS template-literal interpolation) all compare equal: every `${...}`, `{...}` or `:seg`
    becomes `{}`. Also strips a trailing slash (except the root `/`) and any query/fragment.
    Content-free (structure only).

    NOTE on `${...}`: a frontend almost always builds a REST path with a JS/TS template literal —
    `` fetch(`/orders/${id}`) ``. Without collapsing `${…}`, that literal normalized to
    `/orders/${}` and never matched the spec's `/orders/{}`, so the (extremely common) spec↔frontend
    path coupling was silently dropped. `${…}` is collapsed BEFORE the brace rule so the inner
    `{…}` of the interpolation is consumed as one unit (not left as a stray `{}`)."""
    if not path:
        return ""
    # Drop query / fragment if a literal carried one.
    path = path.split("?", 1)[0].split("#", 1)[0]
    # `${anything}` (JS/TS template-literal interpolation) -> `{}`. MUST run before the brace rule
    # so the whole `${expr}` (which may contain dots, e.g. `${u.id}`) collapses to a single param.
    path = re.sub(r'\$\{[^}]*\}', "{}", path)
    # `{anything}` -> `{}`
    path = re.sub(r'\{[^/}]*\}', "{}", path)
    # `:segment` (express/rails style) -> `{}`  (a colon-prefixed path segment)
    path = re.sub(r'(?<=/):[A-Za-z_][A-Za-z0-9_]*', "{}", path)
    # collapse duplicate slashes, strip trailing slash (keep a bare root)
    path = re.sub(r'/{2,}', "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def _operation_node_name(operation_id: "str | None", method: str, norm_path: str) -> "str | None":
    """The node NAME for an operation: PREFER operationId (distinctive). Else a normalized
    `METHOD path` (uppercased method + collapsed path). Returns None when neither is usable
    (no operationId AND the path is stoplisted/empty)."""
    if operation_id:
        oid = operation_id.strip()
        if oid:
            return oid
    if _is_generic_path(norm_path):
        return None
    return f"{method.upper()} {norm_path}"


# A definition recovered from a spec: a node name + kind + (for path-derived operations) the
# normalized path used for path-literal reference matching.
class _Defs:
    __slots__ = ("operations", "schemas", "op_path")

    def __init__(self) -> None:
        # operation node-name -> True (set of operation names)
        self.operations: dict[str, bool] = {}
        # schema node-name -> True (set of schema names)
        self.schemas: dict[str, bool] = {}
        # operation node-name -> normalized spec path it is defined on (or "" when none / stoplisted).
        # Lets a code path literal couple to the operation node (operationId ops AND METHOD-path ops).
        # Stoplisted/root paths are stored as "" so they never become a path-literal coupling anchor.
        self.op_path: dict[str, str] = {}
        # NOTE: kept as dicts (insertion-ordered, dedup) rather than sets for determinism.


def _extract_defs_structured(
    spec: dict,
    incomplete_paths_out=None,
    relative_path: "str | None" = None,
) -> "_Defs | None":
    """Pass-1 (structured): recover operations + schemas from a parsed spec dict.
    Returns a _Defs, or None when this dict is NOT an OpenAPI/Swagger spec (no marker).
    Content-free: names + structural paths only. Never-crash (every access guarded)."""
    if not isinstance(spec, dict):
        return None
    # MARKER GATE: only a dict carrying openapi:/swagger: at the top level is a spec.
    if "openapi" not in spec and "swagger" not in spec:
        return None

    defs = _Defs()

    # --- operations under paths: ---
    paths = spec.get("paths")
    if isinstance(paths, dict):
        for raw_path, item in paths.items():
            if not isinstance(raw_path, str) or not isinstance(item, dict):
                continue
            norm = _normalize_path(raw_path)
            # The path a literal can couple to: "" when generic/stoplisted/root (never a path anchor).
            coupling_path = "" if _is_generic_path(norm) else norm
            for method in _HTTP_METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                oid = op.get("operationId")
                oid = oid if isinstance(oid, str) else None
                name = _operation_node_name(oid, method, norm)
                if name is None:
                    continue
                if len(defs.operations) < _MAX_API_NODES:
                    defs.operations.setdefault(name, True)
                    # first path wins (an operationId is globally unique per spec by construction)
                    defs.op_path.setdefault(name, coupling_path)
                elif name not in defs.operations:
                    _mark_incomplete(incomplete_paths_out, relative_path)

    # --- operations under webhooks: (OpenAPI 3.1) ---
    # `webhooks:` is a top-level sibling of `paths:` whose keys are EVENT NAMES (not URL paths), each
    # holding a path-item with HTTP-method operations. A webhook operation is a real contract (the
    # spec DEFINES it; a backend handler IMPLEMENTS it) — missing it is a silent-miss. We mint ONLY
    # operationId-named webhook operations: a webhook's key is not a URL, so a normalized "METHOD
    # <eventName>" would be a bogus path that no code literal could match and could collide across
    # specs — operationId is the distinctive, safe anchor (a webhook with no operationId is skipped,
    # the precise-silence bar). No path coupling for webhooks (op_path stays "").
    webhooks = spec.get("webhooks")
    if isinstance(webhooks, dict):
        for _event_name, item in webhooks.items():
            if not isinstance(item, dict):
                continue
            for method in _HTTP_METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                oid = op.get("operationId")
                if not isinstance(oid, str) or not oid.strip():
                    continue   # webhook with no operationId: no safe distinctive anchor → skip
                name = oid.strip()
                if len(defs.operations) < _MAX_API_NODES:
                    defs.operations.setdefault(name, True)
                    defs.op_path.setdefault(name, "")   # event name is not a URL path → no path anchor
                elif name not in defs.operations:
                    _mark_incomplete(incomplete_paths_out, relative_path)

    # --- schemas: components/schemas (OpenAPI 3) or definitions (Swagger 2) ---
    schema_containers = []
    components = spec.get("components")
    if isinstance(components, dict) and isinstance(components.get("schemas"), dict):
        schema_containers.append(components["schemas"])
    if isinstance(spec.get("definitions"), dict):   # Swagger 2
        schema_containers.append(spec["definitions"])
    for container in schema_containers:
        for sname in container.keys():
            if not isinstance(sname, str):
                continue
            if len(defs.schemas) < _MAX_API_NODES:
                defs.schemas.setdefault(sname, True)
            elif sname not in defs.schemas:
                _mark_incomplete(incomplete_paths_out, relative_path)

    return defs


# ---------------------------------------------------------------------------
# Line-scanner fallback (used only when PyYAML is unavailable for a .yaml/.yml spec)
# ---------------------------------------------------------------------------

# Top-level openapi:/swagger: marker (column 0, value present). Used both to confirm the file is a
# spec and to gate the fallback (we only ever line-scan a marked file).
_MARKER_RE = re.compile(r'^(openapi|swagger)\s*:\s*\S', re.MULTILINE)

# An operationId line: `operationId: getUser` (optionally quoted). Group 1 = the value.
_OPERATION_ID_RE = re.compile(r'^\s+operationId\s*:\s*["\']?([A-Za-z_][A-Za-z0-9_]*)["\']?\s*(?:#.*)?$')

# A path-item line: a key starting with `/` under the `paths:` block (2-space-ish indent), value is
# empty or a `{` etc. Group 1 = the raw path. We additionally require we are inside the paths block.
_PATH_LINE_RE = re.compile(r'^\s+(/[^\s:]*)\s*:\s*(?:#.*)?$')

# An HTTP-method line nested under a path item. Group 1 = the method. Used to pair with the most
# recent path line to mint normalized `METHOD path` operations when no operationId is present.
_METHOD_LINE_RE = re.compile(r'^\s+(get|put|post|delete|patch)\s*:\s*(?:#.*)?$')

# A schema name line: a key directly under `schemas:` / `definitions:`. Detected structurally by
# tracking the indent of the container header and matching its immediate children.
_KEY_LINE_RE = re.compile(r'^(\s*)([A-Za-z_][A-Za-z0-9_.\-]*)\s*:\s*(?:#.*)?(.*)$')


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _extract_defs_linescan(
    text: str,
    incomplete_paths_out=None,
    relative_path: "str | None" = None,
) -> "_Defs | None":
    """Pass-1 (fallback): recover operations + schemas from a .yaml/.yml spec by a BOUNDED
    line scan, when PyYAML is unavailable. Returns a _Defs, or None when the file is not a spec
    (no top-level openapi:/swagger: marker). Content-free, never-crash.

    This is intentionally CONSERVATIVE (precision over completeness): it recovers operationId
    values, normalized `METHOD path` operations under `paths:`, and schema names under
    `schemas:` / `definitions:`. It does not attempt to model arbitrary nesting beyond what is
    needed for those keys."""
    if not _MARKER_RE.search(text):
        return None

    defs = _Defs()
    lines = text.split("\n")
    if len(lines) > _MAX_SCAN_LINES:
        _mark_incomplete(incomplete_paths_out, relative_path)
        lines = lines[:_MAX_SCAN_LINES]

    in_paths = False
    paths_indent = -1
    cur_path_norm: "str | None" = None
    cur_path_indent = -1

    # schemas / definitions container tracking
    in_schemas = False
    schemas_indent = -1

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = _indent_of(raw)
        stripped = raw.strip()

        # --- top-level section switches (column-0 keys) ---
        if indent == 0:
            in_paths = stripped.startswith("paths:")
            paths_indent = 0 if in_paths else -1
            cur_path_norm = None
            # schemas/definitions can be top-level (Swagger 2 `definitions:`) …
            if stripped.startswith("definitions:"):
                in_schemas = True
                schemas_indent = 0
            else:
                # any other top-level key ends a top-level definitions block
                if schemas_indent == 0:
                    in_schemas = False
                    schemas_indent = -1
            continue

        # --- components/schemas (OpenAPI 3): a `schemas:` key at any indent opens the block ---
        if stripped == "schemas:" or stripped.startswith("schemas:"):
            in_schemas = True
            schemas_indent = indent
            continue

        # --- operationId anywhere (strong, indent-independent anchor) ---
        m_oid = _OPERATION_ID_RE.match(raw)
        if m_oid:
            name = m_oid.group(1).strip()
            if name and len(defs.operations) < _MAX_API_NODES:
                defs.operations.setdefault(name, True)
                # associate with the enclosing path (when inside the paths block) so a code path
                # literal couples to this operationId operation; "" when no/stoplisted path.
                coupling_path = ""
                if in_paths and cur_path_norm and not _is_generic_path(cur_path_norm):
                    coupling_path = cur_path_norm
                defs.op_path.setdefault(name, coupling_path)
            elif name and name not in defs.operations:
                _mark_incomplete(incomplete_paths_out, relative_path)
            continue

        # --- paths block: path-item lines + method lines ---
        if in_paths:
            # A path key (starts with `/`).
            m_path = _PATH_LINE_RE.match(raw)
            if m_path:
                cur_path_norm = _normalize_path(m_path.group(1))
                cur_path_indent = indent
                continue
            # A method line nested under the current path -> normalized METHOD path operation.
            m_meth = _METHOD_LINE_RE.match(raw)
            if m_meth and cur_path_norm is not None and indent > cur_path_indent:
                name = _operation_node_name(None, m_meth.group(1), cur_path_norm)
                if name is not None and len(defs.operations) < _MAX_API_NODES:
                    defs.operations.setdefault(name, True)
                    coupling_path = "" if _is_generic_path(cur_path_norm) else cur_path_norm
                    defs.op_path.setdefault(name, coupling_path)
                elif name is not None and name not in defs.operations:
                    _mark_incomplete(incomplete_paths_out, relative_path)
                continue
            # If we dedent back to/above the paths header indent on a non-path key, leave paths.
            if indent <= paths_indent:
                in_paths = False
                cur_path_norm = None

        # --- schemas / definitions block: immediate children are schema names ---
        if in_schemas:
            if indent <= schemas_indent and not (stripped == "schemas:" or stripped.startswith("schemas:")):
                # dedented out of the block
                in_schemas = False
                schemas_indent = -1
            else:
                m_key = _KEY_LINE_RE.match(raw)
                if m_key and _indent_of(raw) == schemas_indent + 2:
                    # heuristically: direct children are indented exactly one level (2 spaces) in.
                    sname = m_key.group(2)
                    if sname and len(defs.schemas) < _MAX_API_NODES:
                        defs.schemas.setdefault(sname, True)
                    elif sname and sname not in defs.schemas:
                        _mark_incomplete(incomplete_paths_out, relative_path)
                elif m_key and _indent_of(raw) > schemas_indent:
                    # tolerate other indent widths: any deeper key whose parent chain is the block.
                    # Be conservative — only take keys at the SHALLOWEST child level seen.
                    pass

    return defs


def _spec_defs(
    text: str,
    ext: str,
    incomplete_paths_out=None,
    relative_path: "str | None" = None,
) -> "_Defs | None":
    """Pass-1 dispatcher: try structured parse first (json / PyYAML), fall back to the bounded
    line scanner for .yaml/.yml when PyYAML is unavailable. Returns a _Defs only for a real spec
    (openapi:/swagger: marker present); None otherwise. Never-crash."""
    try:
        spec = _try_parse_structured(text, ext)
        if spec is not None:
            return _extract_defs_structured(
                spec, incomplete_paths_out, relative_path
            )
        # structured parse unavailable/failed:
        if ext.lower() in (".yaml", ".yml"):
            if _MARKER_RE.search(text):
                # The line scanner may recover useful evidence, but it cannot
                # prove a malformed/pARSER-unavailable document was complete.
                _mark_incomplete(incomplete_paths_out, relative_path)
            return _extract_defs_linescan(
                text, incomplete_paths_out, relative_path
            )
        # .json that failed json.loads is simply not a usable spec.
        if re.search(r'"(?:openapi|swagger)"\s*:', text):
            _mark_incomplete(incomplete_paths_out, relative_path)
        return None
    except Exception:
        _mark_incomplete(incomplete_paths_out, relative_path)
        return None


# ---------------------------------------------------------------------------
# References (from code) — anchored, known-set gated
# ---------------------------------------------------------------------------

# A DEFINED function / exported / assigned identifier equal to an operationId. We anchor the
# operationId reference so a mere mention in arbitrary text does not couple. Anchors (any):
#   def getUser(            python / generic
#   function getUser(       js/ts
#   func getUser(           go
#   getUser = / getUser:    assignment / object method / class field (js/ts/py)
#   export ... getUser      export of the identifier
#   async getUser(          async method
# The known-set gate (only operationIds the spec defined) is the precision backbone; these anchors
# additionally require the identifier appear in a DEFINITION/EXPORT position, not bare prose.
def _operation_id_anchor_re(op_id: str) -> "re.Pattern[str]":
    esc = re.escape(op_id)
    return re.compile(
        r'(?:'
        r'\bdef\s+' + esc + r'\b'            # python def
        r'|\bfunction\s+' + esc + r'\b'      # js/ts function decl
        r'|\bfunc\s+(?:\([^)]*\)\s*)?' + esc + r'\b'  # go func (optionally a receiver)
        r'|\basync\s+' + esc + r'\b'         # async method shorthand
        r'|\bexport\b[^\n;]*\b' + esc + r'\b'  # export ... name (same line)
        r'|(?<![.\w])' + esc + r'\s*[:=]\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|\([^)]*\)\s*\{)'  # name = fn / name: fn
        r'|(?<![.\w])' + esc + r'\s*\([^)]*\)\s*\{'  # method shorthand: name(args) {
        r')'
    )


def _op_ids_referenced(text_stripped: str, known_ops: "frozenset[str]") -> set:
    """Operation references via operationId used as a DEFINED/EXPORTED identifier. `text_stripped`
    has comments removed. Only names in `known_ops` can match. Returns the set of matched op-id
    node-names. Content-free: names only."""
    found: set = set()
    if not known_ops:
        return found
    # PERF NOTE (AUDIT 2026-06-20): this is O(known_ops) in shape (same class as the proto bug),
    # but MILDER and intentionally left as-is. Each iteration is gated by the C-level
    # `op_id not in text_stripped` pre-check, so the (more expensive) per-op anchor regex runs only
    # for op-ids that literally appear; measured ~145 ms at known=20k (vs the proto path's seconds),
    # and node counts are bounded by _MAX_API_NODES. A safe O(idents) inversion is NOT available
    # here: a match requires the op-id to sit in a specific DEFINITION/EXPORT position (def/function/
    # func/export/`name = fn`, with lookbehinds), not a bare token — inverting that would change the
    # matching semantics and risk correctness. Precision/correctness wins over a minor speedup.
    for op_id in known_ops:
        # Cheap pre-check: the literal must appear at all before the (more expensive) anchor regex.
        if op_id not in text_stripped:
            continue
        if _operation_id_anchor_re(op_id).search(text_stripped):
            found.add(op_id)
    return found


# A route/path string LITERAL in code: a single- or double-quoted (or backtick) string whose value
# starts with `/`. Group 1/2/3 = the literal body (one per quote style). We normalize each before
# comparing to the spec's normalized paths.
_PATH_LITERAL_RE = re.compile(
    r'"(/[^"\n]*)"'
    r"|'(/[^'\n]*)'"
    r'|`(/[^`\n]*)`'
)


def _paths_referenced(text_stripped: str, known_norm_paths: "frozenset[str]") -> set:
    """Path references via a route STRING LITERAL matching a spec path. `text_stripped` has
    comments removed. Param styles are normalized (`{id}`/`:id` → `{}`) before comparison. Only
    normalized paths in `known_norm_paths` can match (stoplisted/root already excluded upstream).
    Returns the set of matched NORMALIZED paths. Content-free: paths only."""
    found: set = set()
    if not known_norm_paths:
        return found
    for m in _PATH_LITERAL_RE.finditer(text_stripped):
        raw = m.group(1) or m.group(2) or m.group(3) or ""
        norm = _normalize_path(raw)
        if _is_generic_path(norm):
            continue
        if norm in known_norm_paths:
            found.add(norm)
    return found


# A schema NAME used as a type/identifier in code. We require a word-boundary identifier match; the
# known-set + stoplist + min-length gates supply precision (schema names are often generic). We do
# NOT match a name that is only a substring of a longer identifier.
def _schemas_referenced(text_stripped: str, known_schemas: "frozenset[str]") -> set:
    """Schema references: a known, non-stoplisted, sufficiently-distinctive schema NAME used as a
    bare identifier in code. `text_stripped` has comments removed. Returns matched schema
    node-names. Content-free: names only."""
    found: set = set()
    if not known_schemas:
        return found
    # PERF NOTE (AUDIT 2026-06-20): O(known_schemas) in shape but MILDER than the proto bug and
    # left as-is. The C-level `name not in text_stripped` pre-check makes the per-name word-boundary
    # regex run only for names that literally appear (~145 ms at known=20k; bounded by
    # _MAX_API_NODES). The two obvious O(idents) inversions are both rejected: (1) tokenizing on
    # [A-Za-z0-9_]+ and intersecting would MISS schema names containing `.`/`-` (OpenAPI allows
    # `com.example.Order`, `Order-V2`) — NOT byte-identical; (2) one combined alternation regex of
    # all names is byte-identical but MEASURED SLOWER (Python `re` tries each alternative per
    # position => O(text*known); ~2x slower at known=20k). Correctness + the existing cheap guard win.
    for name in known_schemas:
        if name not in text_stripped:
            continue
        if re.search(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])', text_stripped):
            found.add(name)
    return found


# ---------------------------------------------------------------------------
# CROSS-REPO consumer-key references (feasibility-spike STEP 1) — flag-gated OFF, ADDITIVE.
# ---------------------------------------------------------------------------
# The within-repo path above gates EVERY reference behind the same-repo known-set
# (`known_ops`/`known_schemas_ref` = names this repo's spec DEFINED). That is exactly why a
# CONSUMER reference to a contract DEFINED IN ANOTHER REPO is dropped: its name is not in this
# repo's known-set. For cross-repo, the consumer must emit a `queries` edge to the STABLE contract
# id even with NO local definition, so that — once BOTH repos are ingested under one account — repo
# A's `alters api_operation::confirmOrder` and repo B's `queries api_operation::confirmOrder` land
# on the SAME code_edge.dst and the relaxed res_adj couples them.
#
# PRECISION (why over-emitting on the consumer side is bounded + safe — the SAME discipline routes
# uses): the consumer emits an anchored reference that passes its OWN self-contained floor (a name in
# a DEFINITION/EXPORT position, ≥ a min length, NOT in the ubiquitous stoplist). It does NOT couple
# anything on its own — a `queries` edge only produces a coupling when a PRODUCER `alters` edge
# exists for the SAME id (res_adj needs ≥2 files on the node). So the PRODUCER side is the precision
# filter: a consumer `def listFoos()` with no producer `api_operation::listFoos` couples nothing, and
# the producer-side multi-definer guard makes ambiguous local names inert. content-free: names only.

# Minimum length for a cross-repo operationId reference (a 1-2 char def name is never a real
# operationId and would over-emit; an operationId is a distinctive verb-noun like `getUserById`).
_MIN_XREPO_OPID_LEN = 4

# An anchored DEFINITION/EXPORT of an identifier — the SAME anchor positions _operation_id_anchor_re
# matches, but capturing the NAME (so we can emit it cross-repo without a known list). One pass over
# the file; the capture group is the defined identifier. We then apply the operationId floor.
_DEF_NAME_RE = re.compile(
    r'(?:'
    r'\bdef\s+([A-Za-z_][A-Za-z0-9_]*)'                       # python def NAME
    r'|\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)'                 # js/ts function NAME
    r'|\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)'    # go func (optional receiver) NAME
    r'|\bexport\s+(?:async\s+)?(?:function|const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)'  # export decl NAME
    r')'
)


def _xrepo_opid_refs(text_stripped: str) -> set:
    """Cross-repo: anchored operationId-like references = identifiers in a DEFINITION/EXPORT position
    that pass the operationId floor (≥ _MIN_XREPO_OPID_LEN, not a generic schema-stoplist word). No
    known-set gate (the producer side filters at join time). content-free: names only."""
    found: set = set()
    for m in _DEF_NAME_RE.finditer(text_stripped):
        name = next((g for g in m.groups() if g), None)
        if not name or len(name) < _MIN_XREPO_OPID_LEN:
            continue
        if name.lower() in _SCHEMA_STOPLIST:   # reuse the ubiquitous-name stoplist (get/list/... carriers)
            continue
        found.add(name)
    return found


# A schema NAME used as a TYPE annotation / constructor in code — anchored so a bare prose word does
# not match: `: Name` (TS/py type annotation), `Name(` (constructor/call), `new Name` (JS), `Name{`
# (Go struct literal), `*Name` (Go pointer type). PascalCase-first so we do not sweep local vars.
_TYPE_USE_RE = re.compile(
    r'(?:'
    r'(?<![A-Za-z0-9_])new\s+([A-Z][A-Za-z0-9_]*)'           # new Name
    r'|:\s*\[?\s*([A-Z][A-Za-z0-9_]*)'                       # : Name  /  : [Name]  (type annotation)
    r'|(?<![A-Za-z0-9_.])([A-Z][A-Za-z0-9_]*)\s*[\({]'        # Name(  /  Name{   (ctor / struct literal)
    r'|[\*\[]\s*([A-Z][A-Za-z0-9_]*)'                        # *Name / [Name  (Go pointer / slice type)
    r')'
)


def _xrepo_schema_refs(text_stripped: str) -> set:
    """Cross-repo: anchored schema-NAME references = PascalCase names used as a type/constructor that
    pass the schema floor (≥ _MIN_SCHEMA_REF_LEN, not in _SCHEMA_STOPLIST). No known-set gate (the
    producer side filters at join time). content-free: names only."""
    found: set = set()
    for m in _TYPE_USE_RE.finditer(text_stripped):
        name = next((g for g in m.groups() if g), None)
        if not name or len(name) < _MIN_SCHEMA_REF_LEN:
            continue
        if name.lower() in _SCHEMA_STOPLIST:
            continue
        found.add(name)
    return found


# ---------------------------------------------------------------------------
# Public entry point — called by build_graph (mirrors _api_contract_graph wiring)
# ---------------------------------------------------------------------------

def _openapi_graph(
    root: str,
    files: list,
    incomplete_paths_out=None,
) -> tuple:
    """Return (nodes, edges) for the OpenAPI/Swagger REST-contract substrate.

    `files`: a list of (abs_path, ext) tuples — source_files + config_files, already
    guard-filtered by build_graph (size/binary/generated/symlink caps). DEFINITIONS come only
    from spec-marked .yaml/.yml/.json files (top-level openapi:/swagger: marker). REFERENCES come
    from any code source file in the list. Ordinary yaml/json (no marker) is left entirely to
    _cg_config — this pass adds ZERO nodes for them (additive).

    New NODE kinds: `api_operation` (id `api_operation::<operationId>` or
    `api_operation::GET /users/{}`) and `api_schema` (id `api_schema::<name>`).
    REUSED EDGE kinds: `alters` (spec file -> contract node), `queries` (code file -> contract
    node) — so the existing shared-resource adjacency couples the two with NO SQL change.

    Content-free: operation/schema/path NAMES + file paths only. Never-crash: all reads and scans
    are bounded; per-file failures are isolated; the whole pass is wrapped so one bad file cannot
    abort the others."""
    nodes: list = []
    edges: list = []
    try:
        return _openapi_graph_impl(
            root, files, incomplete_paths_out=incomplete_paths_out
        )
    except Exception:
        spec_paths = []
        code_paths = []
        for abs_path, ext in files:
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            e = (ext or "").lower()
            if e in _OPENAPI_REF_CODE_EXTS:
                code_paths.append(rel)
            elif e in _SPEC_EXTS:
                spec_paths.append(rel)
        # At wrapper scope there is no trustworthy parse result left with
        # which to distinguish a generic config from a contract whose marker
        # appeared after a capped read. Preserve the bounded empty 2-tuple, but
        # make the complete candidate universe explicitly Unknown.
        if spec_paths:
            for rel in spec_paths + code_paths:
                _mark_incomplete(incomplete_paths_out, rel)
        return nodes, edges   # never-crash: the whole substrate fails soft to empty


def _openapi_graph_impl(
    root: str,
    files: list,
    incomplete_paths_out=None,
) -> tuple:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # Partition into spec candidates (.yaml/.yml/.json) and code reference candidates (everything
    # else). A .json/.yaml that turns out NOT to be a spec is silently dropped from the spec side
    # (and is NOT scanned as a code reference candidate — config is its own pass).
    spec_candidates: list[tuple[str, str, str]] = []   # (abs, rel, ext)
    code_candidates: list[tuple[str, str, str]] = []    # (abs, rel, ext)
    for abs_path, ext in files:
        rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
        e = (ext or "").lower()
        if e in _SPEC_EXTS:
            spec_candidates.append((abs_path, rel, e))
        elif e in _OPENAPI_REF_CODE_EXTS:
            code_candidates.append((abs_path, rel, e))

    if not spec_candidates:
        return nodes, edges

    # --- Pass 1: parse every spec-marked file; collect defs + which file defines each ---
    # operation node-name -> first rel that defines it
    op_def_rel: dict[str, str] = {}
    # schema node-name -> first rel that defines it
    schema_def_rel: dict[str, str] = {}
    # MULTI-DEFINER COUNT (P0): operation/schema node-name -> set of DISTINCT spec files defining it.
    # A name DEFINED by >1 spec file is AMBIGUOUS — different services'/fixtures' specs reusing the
    # same operationId / METHOD-path / schema name — and must NOT anchor a cross-file coupling (see
    # below). Counts kept as sets so re-defs within ONE file (or a re-walk) don't inflate the count.
    op_definers: dict[str, set] = {}
    schema_definers: dict[str, set] = {}
    # per spec file: (rel, _Defs) so we can emit alters for exactly what each file defined
    spec_defs: list[tuple[str, _Defs]] = []
    # normalized path -> set of operation node-names defined on that path (for path-literal refs)
    path_to_ops: dict[str, set] = {}

    capped = False
    definition_loss = set()
    relevant_spec_paths: set[str] = set()
    for abs_path, rel, ext in spec_candidates:
        read_loss = set()
        text = _read_capped(abs_path, read_loss, rel)
        if text is None:
            # The candidate could have been the missing definition catalog; no
            # content remains with which to disprove that. Its loss therefore
            # propagates to every code consumer below.
            _mark_incomplete(definition_loss, rel)
            relevant_spec_paths.add(rel)
            continue
        has_marker = bool(
            _MARKER_RE.search(text)
            if ext in (".yaml", ".yml")
            else re.search(r'"(?:openapi|swagger)"\s*:', text)
        )
        if read_loss:
            # A top-level marker can exist beyond the capped prefix. Treat the
            # candidate as a potentially lost definition catalog even when the
            # visible prefix does not identify it as OpenAPI.
            _mark_incomplete(definition_loss, rel)
            relevant_spec_paths.add(rel)
        if has_marker:
            relevant_spec_paths.add(rel)
        defs = _spec_defs(text, ext, definition_loss, rel)
        if defs is None:
            continue   # not a spec (no marker) -> leave to _cg_config, mint nothing
        relevant_spec_paths.add(rel)
        spec_defs.append((rel, defs))
        for op_name in defs.operations:
            if op_name not in op_def_rel and len(op_def_rel) < _MAX_API_NODES:
                op_def_rel[op_name] = rel
            elif op_name not in op_def_rel:
                capped = True
                _mark_incomplete(incomplete_paths_out, rel)
            if op_name in op_def_rel:   # only track definers for names we actually minted
                op_definers.setdefault(op_name, set()).add(rel)
        for s_name in defs.schemas:
            if s_name not in schema_def_rel and len(schema_def_rel) < _MAX_API_NODES:
                schema_def_rel[s_name] = rel
            elif s_name not in schema_def_rel:
                capped = True
                _mark_incomplete(incomplete_paths_out, rel)
            if s_name in schema_def_rel:
                schema_definers.setdefault(s_name, set()).add(rel)
        # Map normalized paths to the operations defined on them, for path-literal references. Every
        # operation (operationId-named OR "METHOD path"-named) defined on a non-stoplisted path P is
        # reachable from a code path literal that hits P. An empty op_path ("" = stoplisted/root/no
        # path) never becomes a path anchor (precision). So `getUser` on `/users/{id}` couples both
        # via its operationId AND via a `/users/:id` code literal; an operation on `/health` does not.
        for op_name, np in defs.op_path.items():
            if np and not _is_generic_path(np):
                path_to_ops.setdefault(np, set()).add(op_name)
    if capped or definition_loss:
        for _abs_path, rel, _ext in spec_candidates:
            _mark_incomplete(incomplete_paths_out, rel)
        for _abs_path, rel, _ext in code_candidates:
            _mark_incomplete(incomplete_paths_out, rel)

    if not op_def_rel and not schema_def_rel:
        return nodes, edges

    # --- P0: MULTI-DEFINER SUPPRESSION (real-repo audit found this is ~66% of false couples) ---
    # An operation/schema DEFINED by >1 spec file is AMBIGUOUS: the same operationId / normalized
    # METHOD-path / schema name appears in MULTIPLE unrelated specs (different microservices, or
    # different test/codegen fixtures — e.g. three swag test projects each declaring `GET
    # /testapi/application`, or a schema `Config` defined in 8 fixtures). Those specs are NOT the
    # same contract; coupling their files merely because the NAMES collide is wallpaper. The
    # shared-resource adjacency (res_adj) would couple every pair of definers (definer↔definer) AND
    # any referencer through the node. So we retain the node and all anchored edges as explicit
    # evidence, but tag every such edge `reference_status=ambiguous`; effective adjacency ignores
    # them and therefore couples NOTHING. This mirrors the call-graph
    # `n<=1 active / ambiguous inert` discipline (an unresolvable
    # same-name fan-out manufactures false couplings). RECALL-SAFE: a GENUINE spec↔handler couple is
    # a SINGLE-definer node (one spec defines `GET /examples/calc`, the handler references it) → it
    # is NOT ambiguous → it survives untouched.
    ambiguous_ops = frozenset(n for n, defset in op_definers.items() if len(defset) > 1)
    ambiguous_schemas = frozenset(n for n, defset in schema_definers.items() if len(defset) > 1)

    # Build the reference known-sets. Ambiguous locally-defined contracts remain
    # known so an anchored consumer reference is not silently discarded; the
    # emitted edge is tagged below and is therefore evidence-only at adjacency.
    known_ops = frozenset(op_def_rel)
    known_norm_paths = frozenset(p for p in path_to_ops)
    known_schemas_ref = frozenset(
        s for s in schema_def_rel
        if s.lower() not in _SCHEMA_STOPLIST and len(s) >= _MIN_SCHEMA_REF_LEN
    )

    # --- Mint nodes (one per name; first-definer wins the path) ---
    op_nodes: dict[str, dict] = {}
    for name, def_rel in op_def_rel.items():
        n = {"id": f"api_operation::{name}", "kind": "api_operation",
             "name": name, "path": def_rel, "language": "openapi"}
        if name in ambiguous_ops:
            n["ambiguous"] = True
        op_nodes[name] = n
        nodes.append(n)
    schema_nodes: dict[str, dict] = {}
    for name, def_rel in schema_def_rel.items():
        n = {"id": f"api_schema::{name}", "kind": "api_schema",
             "name": name, "path": def_rel, "language": "openapi"}
        if name in ambiguous_schemas:
            n["ambiguous"] = True
        schema_nodes[name] = n
        nodes.append(n)

    def _contract_edge(src: str, dst: str, kind: str, *, ambiguous: bool) -> dict:
        edge = {"src": src, "dst": dst, "kind": kind}
        if ambiguous:
            edge["reference_status"] = "ambiguous"
        return edge

    # --- alters edges: every operation / schema defined in each spec file ---
    # Multi-definer edges remain durable evidence but are explicitly inert.
    for rel, defs in spec_defs:
        for op_name in defs.operations:
            if op_name in op_nodes:
                edges.append(_contract_edge(
                    rel, f"api_operation::{op_name}", "alters",
                    ambiguous=op_name in ambiguous_ops,
                ))
        for s_name in defs.schemas:
            if s_name in schema_nodes:
                edges.append(_contract_edge(
                    rel, f"api_schema::{s_name}", "alters",
                    ambiguous=s_name in ambiguous_schemas,
                ))

    # CROSS-REPO (flag-gated OFF): the SET of contract ids DEFINED in THIS repo. A reference whose
    # definition IS local is handled by the within-repo path below; the cross-repo branch emits ONLY
    # the references with NO local definition (so it is purely additive — see _cg_xrepo).
    _xrepo_on = cross_repo_keys_enabled()
    local_op_ids = {f"api_operation::{n}" for n in op_def_rel} if _xrepo_on else frozenset()
    local_schema_ids = {f"api_schema::{n}" for n in schema_def_rel} if _xrepo_on else frozenset()

    # --- queries edges: anchored references in CODE files (read lazily) ---
    for abs_path, rel, ext in code_candidates:
        text = _read_capped(abs_path, incomplete_paths_out, rel)
        if text is None:
            continue
        stripped = _strip_comments(_sfc_executable_text(text, ext))

        # operationId references (defined/exported identifier equal to an operationId)
        for op_name in _op_ids_referenced(stripped, known_ops):
            if op_name in op_nodes:
                edges.append(_contract_edge(
                    rel, f"api_operation::{op_name}", "queries",
                    ambiguous=op_name in ambiguous_ops,
                ))

        # path-literal references -> couple to every operation defined on that path
        for norm in _paths_referenced(stripped, known_norm_paths):
            for op_name in path_to_ops.get(norm, ()):  # type: ignore[arg-type]
                if op_name in op_nodes:
                    edges.append(_contract_edge(
                        rel, f"api_operation::{op_name}", "queries",
                        ambiguous=op_name in ambiguous_ops,
                    ))

        # schema-name references (distinctive, non-stoplisted names only)
        for s_name in _schemas_referenced(stripped, known_schemas_ref):
            if s_name in schema_nodes:
                edges.append(_contract_edge(
                    rel, f"api_schema::{s_name}", "queries",
                    ambiguous=s_name in ambiguous_schemas,
                ))

        # CROSS-REPO consumer-key references (flag-gated OFF → this whole block is skipped, keeping the
        # within-repo output byte-identical). Emit a `queries` edge to the STABLE contract id for an
        # anchored operationId/schema reference whose definition is NOT in THIS repo, so it can couple
        # to a PRODUCER repo's `alters` edge for the same id via the relaxed res_adj. Gated by the
        # self-contained operationId/schema floors; the producer `alters` (+ multi-definer guard) is the
        # precision filter at join time. content-free: names only.
        if _xrepo_on:
            for op_name in _xrepo_opid_refs(stripped):
                dst = f"api_operation::{op_name}"
                if dst not in local_op_ids:
                    edges.append({"src": rel, "dst": dst, "kind": "queries"})
            for s_name in _xrepo_schema_refs(stripped):
                dst = f"api_schema::{s_name}"
                if dst not in local_schema_ids:
                    edges.append({"src": rel, "dst": dst, "kind": "queries"})

    return nodes, edges
