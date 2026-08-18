"""Cross-tier ROUTE↔CALL coupling — extraction + matching + floors + producer (PR #270 → integration).

WHAT (the coupling a single-language structural graph CANNOT see): on AI-era full-stack repos the #1
MISSED coupling is the client/server CONTRACT — a backend file DEFINES a route string (`@app.get('/api/
orders/{id}')`) and a frontend file ISSUES a request to that same route (`fetch('/api/orders/5')`).
Change the route on one side and the other breaks, yet there is NO code edge / NO shared symbol between
the two files (different languages, different dirs), so neither the call graph nor the import graph links
them. This module recovers that link and emits it as a first-class FILE→FILE coupling edge, so Veripsa
warns before merge when two in-flight PRs touch opposite ends of the same contract.

PROVEN SIGNAL (PR #270, MEASURE-FIRST): matched route↔call pairs co-change ~17× median (86% rate vs 22%
random) once the specificity floors apply. This module is the SINGLE SOURCE OF TRUTH for the proven
extraction (extract_route_defs / extract_request_urls), normalization, the specificity floor (is_specific),
and the concrete-anchor matcher (match_routes / match_pairs / scan_repo). tests/cross_tier_route_probe.py
imports these from HERE for its MEASUREMENT path, so the producer and the probe can never drift.

WHY this logic lives in a ROOT MODULE (not in tests/): the producer runs INSIDE the shipped image on every
webhook (build_graph → _routes_graph). The Dockerfile COPYs `_cg_*.py` (glob), so a root `_cg_routes.py` is
auto-included; importing the logic from `tests/` instead would make `import code_graph_extract` raise
`No module named 'tests'` in the built image — the exact #262 silent-prod-outage class. So the proven logic
is HERE; the test depends on the root module, never the reverse.

EDGE KIND (no schema change): the engine's edge_kind CHECK is constrained to
{contains,calls,imports,queries,alters,reads_config,queries_col,alters_col}; a new kind would
require a schema-contract migration.
A route↔call link is a cross-file DEPENDENCY between two FILES, exactly the shape an `imports` edge has
AFTER resolution (dst = a resolved repo FILE path). So _routes_graph emits `imports` edges with src/dst =
the two file PATHS. That flows straight through the engine's EXISTING imp_out/imp_in adjacency (file→file,
the highest-signal, unambiguous coupling) and is gated by the SAME hub-dampening discipline, ZERO schema
change.

WHY emitted AFTER _resolve_imports (build_graph wires it post-resolution): _resolve_imports enforces a
SAME-LANGUAGE filter (`_family(dst) == _family(src)`) so a real intra-language import never crosses a
language boundary. A cross-tier edge is by definition `.py`↔`.ts` / `.go`↔`.svelte` — it would be DROPPED
by that filter. So build_graph appends these edges to the ALREADY-resolved edge list; they carry resolved
file-path endpoints from the start (this producer never goes through resolution), and the family filter
never touches them. The edges then ride the normal dedup + ingest path unchanged.

PRECISION-SAFE: the specificity floors (concrete first segment + shared concrete anchor; drop bare/
ubiquitous routes like `/`, `/api`, `/health`) are #270's precision guarantee and are kept EXACTLY. When a
match has multiple definers, candidate edges remain with `reference_status=ambiguous`; effective
adjacency excludes them while persistence records why each endpoint is Unknown.

CONTENT-FREE: this reads only URL/route PATH STRINGS and file paths. Never request/response bodies, never
source semantics beyond the literal route token.
"""
from __future__ import annotations

import io
import os
import re
import tokenize

from _cg_io import _mark_incomplete, _read_capped
from _cg_languages import _sfc_executable_text
from _cg_xrepo import cross_repo_keys_enabled  # cross-repo shared-key emission flag (default OFF)

# ---------------------------------------------------------------------------------------------
# File-tier classification (content-free: by extension / path only)
# ---------------------------------------------------------------------------------------------
BACKEND_EXT = {".py", ".go", ".rb", ".java", ".kt", ".cs", ".php", ".rs", ".ex", ".exs"}
FRONTEND_EXT = {".ts", ".tsx", ".js", ".jsx", ".svelte", ".vue", ".astro"}
# .ts/.js are ambiguous (node backend OR browser frontend). We classify a .ts/.js file as
# FRONTEND only when it ISSUES requests, and as BACKEND only when it DEFINES routes; a file that
# does both (an isomorphic SDK) is allowed to appear on both sides but a self-pair is never emitted.

ROUTE_DEF_EXT = BACKEND_EXT | {".ts", ".tsx", ".js", ".jsx", ".astro"}  # JS/TS servers + Astro pages
# .go joins the REQUESTERS: a Go API CLIENT/SDK issues verb-first request calls (`c.getResponse("GET",
# "/repos/...")`) to a backend that may live in ANOTHER repo (go-sdk → a Gitea-style server) — the exact
# cross-tier/cross-repo contract a single-language graph cannot see. extract_request_urls now has a
# Go-specific verb-anchored matcher; a Go file that ONLY defines routes issues no such call → no request
# keys (inert), so adding .go here costs nothing for pure-server Go files.
REQUEST_EXT = FRONTEND_EXT | {".ts", ".js", ".tsx", ".jsx", ".go"}


def _ext(p: str) -> str:
    return os.path.splitext(p)[1].lower()


# Path SEGMENTS that declare a subtree is TEST code (same set + spirit as _cg_schema._TEST_SEGS). A
# route DEFINED only inside a test/fixture app is not a real backend the production frontend talks to,
# and a test app RE-declaring a real route would otherwise (a) false-couple a frontend to the test file
# and (b) inflate the multi-definer count below, SUPPRESSING the genuine real-backend coupling (a recall
# loss). So test-dir files are excluded as route DEFINERS. They are KEPT as REQUESTERS: a test that
# fetches a real route IS coupled to the backend (editing the route breaks the test) — dropping that
# would be a recall loss. Directory segments only (the filename is never a sole test marker).
_TEST_SEGS = frozenset({
    "test", "tests", "spec", "specs", "fixtures", "e2e", "__tests__",
})


def _is_test_path(rel: str) -> bool:
    """True when a path segment (excluding the filename) declares the subtree is test code."""
    normed = rel.replace("\\", "/").lower()
    return any(seg in _TEST_SEGS for seg in normed.split("/")[:-1])


# ---------------------------------------------------------------------------------------------
# Route-DEFINITION extraction (backend side). All patterns capture ONLY the literal path string.
# ---------------------------------------------------------------------------------------------
# Flask/FastAPI: @app.route("/x"), @router.get("/x"), @app.post('/x')
_PY_DECORATOR = re.compile(
    r"""@\w+\.(?:route|get|post|put|patch|delete|head|options|websocket)\s*\(\s*["']([^"']+)["']""")
# FastAPI APIRouter(prefix="/x") / Flask Blueprint url_prefix
_PY_PREFIX = re.compile(r"""(?:prefix|url_prefix)\s*=\s*["']([^"']+)["']""")
# Django urls.py: path("x/", ...), re_path(r"^x/$", ...)
_PY_DJANGO = re.compile(r"""\b(?:path|re_path|url)\s*\(\s*r?["']([^"']+)["']""")
_PY_TRIPLE_QUOTED = re.compile("(?is)^[rubf]*(?:'''|\"\"\")")


def _blank_py_non_code_route_text(body: str) -> str:
    """Blank Python comments and standalone triple-quoted prose before route regexes run.

    Real route decorators keep their ordinary string literals (`@app.get("/x")`), while docstrings and
    comment examples like `@app.get("/example")` no longer mint route definitions. Fail open on tokenizer
    errors so a malformed file keeps the pre-existing extraction behavior.
    """
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(body).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return body

    lines = body.splitlines(keepends=True)
    out = [list(line) for line in lines]

    def blank(start: tuple[int, int], end: tuple[int, int]) -> None:
        sr, sc = start
        er, ec = end
        for row in range(sr, er + 1):
            idx = row - 1
            if idx < 0 or idx >= len(out):
                continue
            left = sc if row == sr else 0
            right = ec if row == er else len(out[idx])
            for col in range(left, min(right, len(out[idx]))):
                if out[idx][col] != "\n":
                    out[idx][col] = " "

    prev_sig_type = None
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            blank(tok.start, tok.end)
        elif (tok.type == tokenize.STRING
              and _PY_TRIPLE_QUOTED.match(tok.string)
              and prev_sig_type in (None, tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE)):
            blank(tok.start, tok.end)
        if tok.type not in (tokenize.COMMENT, tokenize.NL):
            prev_sig_type = tok.type
    return "".join("".join(line) for line in out)

# Express / Koa / Fastify (JS/TS): app.get('/x'), router.post("/x"), app.use('/x', ...)
_JS_ROUTE = re.compile(
    r"""\b\w+\.(?:get|post|put|patch|delete|head|options|use|all)\s*\(\s*["'`]([^"'`]+)["'`]""")

# Go: http.HandleFunc("/x", ...), mux.HandleFunc, r.Get("/x", ...) (chi), e.GET("/x", ...) (echo),
#     r.GET("/x", ...) (gin), router.Handle("/x", ...)
_GO_HANDLEFUNC = re.compile(r"""\bHandleFunc\s*\(\s*["`]([^"`]+)["`]""")
_GO_METHOD = re.compile(
    r"""\b\w+\.(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Handle|Get|Post|Put|Patch|Delete|Method)\s*\(\s*["`]([^"`]+)["`]""")

# --- GO NESTED ROUTE-GROUP PREFIXES (the gin/chi/echo `RouterGroup` idiom). A Go router mounts a SUBTREE
# under a prefix with a CLOSURE: `m.Group("/x", func(){ m.Group("/y", func(){ m.Get("/z", h) }) })` — the
# leaf route's REAL path is `/x/y/z`, but `_GO_METHOD` (regex, position-blind) captures only the bare leaf
# `/z`, which normalizes to a ubiquitous/bare token and silently drops the real contract. MEASURED on
# gitea (`m.Group` web.go) + chi (`r.Route`) + echo (`e.Group`): the dominant Go routing style, hundreds of
# leaf routes mounted under 1–4 nested prefixes, every prefix lost. FIX: a brace-depth scanner tracks the
# active prefix STACK (mirrors how the Python/NestJS/Phoenix paths join controller/scope prefixes via
# _routes_with_prefixes). Covers gin `RouterGroup.Group`, chi `Router.Route`, echo `Group` (all the same
# `<recv>.(Group|Route)("/prefix", func(){...})` shape). content-free (path literals only).
# A group OPENER: `<recv>.Group("/prefix"` or `<recv>.Route("/prefix"` (the closure-mounting forms). The
# leaf-method verbs (`.Get`/`.GET`/…) are NOT here — those are matched by _GO_METHOD and joined to the stack.
_GO_GROUP_OPEN = re.compile(r"""\b\w+\.(?:Group|Route)\s*\(\s*["`]([^"`]*)["`]""")
# Leaf method calls INSIDE a group body (same verbs as _GO_METHOD) — used by the scanner to join the active
# prefix stack to each leaf route. Kept in sync with _GO_METHOD's verb set.
_GO_LEAF_METHOD = re.compile(
    r"""\b\w+\.(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Handle|Get|Post|Put|Patch|Delete|Method)\s*\(\s*["`]([^"`]+)["`]""")


def _go_grouped_routes(body: str) -> set[str]:
    """Return Go routes with their NESTED GROUP PREFIXES joined, by a single brace-depth pass.

    Algorithm (content-free, positional): walk the body char-by-char tracking `{`/`}` brace depth (skipping
    string/char/comment spans so a brace inside a literal never moves the count). Maintain a STACK of
    (open_brace_depth, prefix): when a `<recv>.Group("/p", func(){` opener is seen, the prefix `/p`
    activates at the depth of the `{` that begins its closure body, and stays active until that brace
    closes. Each leaf method route inside is emitted with ALL currently-active prefixes joined (left→right),
    PLUS the bare leaf itself (recall-safe: an absolute-path leaf, or a misread nesting, still couples).

    Recall-biased + precision-deferred, exactly like _routes_with_prefixes: an over-joined path simply fails
    to match downstream (the specificity floor + concrete-anchor matcher + multi-definer guard), never
    false-couples. Returns RAW (un-normalized) route strings; the caller normalizes."""
    out: set[str] = set()
    # Pre-find opener and leaf spans by their match END offset, so during the brace walk we can ask "did a
    # group opener / leaf method START at this position?". We key by the match START of the receiver token.
    openers = {m.start(): m.group(1) for m in _GO_GROUP_OPEN.finditer(body)}
    leaves = {m.start(): m.group(1) for m in _GO_LEAF_METHOD.finditer(body)}
    stack: list[tuple[int, str]] = []   # (brace_depth_at_which_prefix_body_opened, prefix)
    pending: list[str] = []             # prefixes whose opener was seen but whose body `{` is not yet open
    depth = 0
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        # skip string / rune / line-comment / block-comment spans (a brace inside must not count)
        if c == '"' or c == "'" or c == "`":
            q = c
            i += 1
            while i < n and body[i] != q:
                if body[i] == "\\" and q != "`":   # raw strings (`) have no escapes
                    i += 1
                i += 1
            i += 1
            continue
        if c == "/" and i + 1 < n and body[i + 1] == "/":
            while i < n and body[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and body[i + 1] == "*":
            i += 2
            while i + 1 < n and not (body[i] == "*" and body[i + 1] == "/"):
                i += 1
            i += 2
            continue
        # a group opener / leaf method beginning exactly here?
        if c.isalpha() or c == "_":
            if i in openers:
                pending.append(openers[i])
            elif i in leaves:
                leaf = leaves[i]
                prefixes = [p for (_d, p) in stack]
                joined = leaf
                for p in reversed(prefixes):       # innermost-last → prepend outer→inner
                    joined = _join_prefix(p, joined)
                out.add(joined)
                out.add(leaf)                      # also the bare leaf (absolute-path safety / recall)
            # advance past the identifier so a substring of it isn't re-tested
            j = i
            while j < n and (body[j].isalnum() or body[j] == "_"):
                j += 1
            i = j
            continue
        if c == "{":
            depth += 1
            # the `{` that opens a group's closure body activates the MOST-RECENT pending prefix (each
            # `func(){` is its own brace → one prefix per brace, never drain all pending onto one depth).
            if pending:
                stack.append((depth, pending.pop()))
            i += 1
            continue
        if c == "}":
            # closing this depth retires every prefix whose body opened at it
            while stack and stack[-1][0] >= depth:
                stack.pop()
            depth -= 1
            i += 1
            continue
        i += 1
    return out

# Rails routes.rb: get "x", post 'x/y', resources :things (resources -> /things)
_RB_VERB = re.compile(r"""\b(?:get|post|put|patch|delete|match)\s+["']([^"']+)["']""")
_RB_RESOURCES = re.compile(r"""\bresources?\s+:(\w+)""")

# Spring (Java/Kotlin): @RequestMapping("/x"), @GetMapping("/x")
_JVM_MAPPING = re.compile(
    r"""@(?:Request|Get|Post|Put|Patch|Delete)Mapping\s*\(\s*(?:value\s*=\s*)?["']([^"']+)["']""")

# PHP (Laravel): Route::get('/x', ...)
_PHP_ROUTE = re.compile(r"""\bRoute::(?:get|post|put|patch|delete|any|match)\s*\(\s*["']([^"']+)["']""")

# Rust / Actix-web: attribute macros #[get("/path")], #[post("/path")], etc.
# Anchored on the attribute bracket so arbitrary string literals are not swept.
_RS_ACTIX = re.compile(
    r"""#\[(?:get|post|put|patch|delete|head|options)\s*\(\s*["']([^"']+)["']""")
# Rust / Axum: .route("/path", get(handler)) — the path-string in the first argument position.
# Anchored on `.route(` so only Axum-style route registrations match.
_RS_AXUM = re.compile(r"""\.route\s*\(\s*["']([^"']+)["']""")

# --- NestJS (TS/JS): @Get(':id') / @Post() method decorators under @Controller('prefix'). The method
# route is RELATIVE to the controller prefix, so we collect BOTH and join (see _routes_with_prefixes).
# Anchored on the HTTP-verb decorator name so @Injectable()/@Module()/@Input() are never swept.
_TS_NEST_METHOD = re.compile(
    r"""@(?:Get|Post|Put|Patch|Delete|Options|Head|All)\s*\(\s*["'`]([^"'`]+)["'`]""")
_TS_NEST_PREFIX = re.compile(r"""@Controller\s*\(\s*["'`]([^"'`]+)["'`]""")

# --- ASP.NET Core (C#): attribute routing [HttpGet("{id}")] under a controller [Route("api/orders")],
# AND minimal-API app.MapGet("/api/x", ...). Attribute method routes are RELATIVE to the [Route] prefix
# (joined); MapXxx paths are absolute. Anchored so [ApiController]/[Authorize]/[Produces(...)] don't match.
_CS_HTTP_METHOD = re.compile(
    r"""\[Http(?:Get|Post|Put|Patch|Delete|Head|Options)\s*\(\s*["']([^"']+)["']""")
_CS_ROUTE_PREFIX = re.compile(r"""\[Route\s*\(\s*["']([^"']+)["']""")
_CS_MINIMAL = re.compile(
    r"""\.Map(?:Get|Post|Put|Patch|Delete|Methods)\s*\(\s*["']([^"']+)["']""")

# --- Ktor (Kotlin): get("/x") / post("/x") routing-DSL builders. The leading `[^.\w]` (or line start)
# excludes member calls like `map.get("key")` / `settings.get(...)`. Nested `route("/p"){...}` prefixes
# are not positionally recoverable by regex, so we extract the method routes themselves (absolute paths
# couple; relative-under-nested-route paths degrade to recall loss, never a false positive). Kotlin Spring
# is already covered by _JVM_MAPPING above (annotations), so this only adds the Ktor DSL.
_KT_KTOR = re.compile(
    r"""(?:^|[^.\w])(?:get|post|put|patch|delete|head|options)\s*\(\s*["']([^"']+)["']""", re.M)

# --- Phoenix (Elixir, .ex/.exs): `get "/x", Controller, :action` (verb + space + string, no parens).
# `scope "/prefix" do` sets a prefix we join. resources "/things" -> /things (REST collection).
_EX_VERB = re.compile(r"""(?:^|\n)\s*(?:get|post|put|patch|delete|head|options)\s+["']([^"']+)["']""")
_EX_SCOPE = re.compile(r"""\bscope\s+["']([^"']+)["']""")
_EX_RESOURCES = re.compile(r"""\bresources\s+["']([^"']+)["']""")

# --- Hapi (JS/TS): server.route({ method, path: '/x', ... }) (object or array form). Anchored on the
# `.route(` call so a bare `path:` in a build/webpack config object is NOT swept (a real precision risk).
# ReDoS-SAFE (measured): the gap between `.route({` / `.route([{` and `path:` is capped AND cannot cross
# another object/array opener. Excluding `{`, `}`, `[`, `]` is load-bearing: a file of `.route({` repeated to
# MAX_FILE_BYTES otherwise makes every start scan the full 512-char budget before failing (still several
# seconds). This pattern fails as soon as it sees the next opener, while preserving normal compact and
# multi-line first-object Hapi route declarations. NO re.S. Gate: gates.d/195-routes_redos.gate.
_HAPI_ROUTE = re.compile(
    r"""\.route\s*\(\s*(?:\{[^\{\}\[\]]{0,512}?|\[\s*\{[^\{\}\[\]]{0,512}?)path\s*:\s*["'`]([^"'`]+)["'`]""")


def _join_prefix(prefix: str, route: str) -> str:
    """Join a controller/router/scope PREFIX to a method route, content-free. Both are normalized after.
    A route is always treated as RELATIVE to its prefix (NestJS/ASP.NET/Spring/Phoenix all prepend the
    controller/scope prefix even to a leading-slash method path)."""
    p = "/" + prefix.strip("/")
    r = route.strip("/")
    joined = p if not r else p.rstrip("/") + "/" + r
    return joined


def _routes_with_prefixes(method_routes: set[str], prefixes: set[str]) -> set[str]:
    """Recall-biased expansion: the method routes AS-IS (covers already-absolute paths) PLUS each
    prefix×route join (covers controller/scope-relative paths). Over-generation is bounded by
    |prefixes|×|routes| per file and gated downstream by the specificity floor + concrete-anchor matcher
    + multi-definer suppression, so a fabricated join only ever fails to match — never false-couples."""
    out = set(method_routes)
    for pre in prefixes:
        if not pre:
            continue
        if method_routes:
            for r in method_routes:
                out.add(_join_prefix(pre, r))
        else:
            # a prefix with HTTP verbs present but no per-method path (e.g. @Controller('orders')+@Get())
            # is the collection route at the prefix itself.
            out.add("/" + pre.strip("/"))
    return out


def _file_based_route(rel: str) -> str | None:
    """Next.js, Astro pages-router and SvelteKit file-based routes -> URL path.
    Content-free: derived from the FILE PATH only (no body read)."""
    parts = rel.replace("\\", "/").split("/")
    low = [p.lower() for p in parts]
    # Next.js app router: app/api/foo/bar/route.ts -> /api/foo/bar ; app/foo/page.tsx -> /foo
    if "app" in low:
        i = low.index("app")
        tail = parts[i + 1:]
        if tail and tail[-1].split(".")[0] in ("route", "page"):
            segs = [s for s in tail[:-1] if not (s.startswith("(") and s.endswith(")"))]
            url = "/" + "/".join(_norm_seg(s) for s in segs)
            return url if url != "/" or tail[-1].startswith("route") else url
    # SvelteKit: src/routes/foo/+server.ts -> /foo ; src/routes/foo/+page.svelte -> /foo
    if "routes" in low:
        i = low.index("routes")
        tail = parts[i + 1:]
        if tail and tail[-1].startswith("+"):
            segs = [s for s in tail[:-1] if not (s.startswith("(") and s.endswith(")"))]
            url = "/" + "/".join(_norm_seg(s) for s in segs)
            return url
    # Next.js / Astro pages router: pages/api/foo.ts or src/pages/foo.astro -> /api/foo or /foo
    if "pages" in low:
        i = low.index("pages")
        tail = parts[i + 1:]
        if tail:
            last = tail[-1].split(".")[0]
            segs = tail[:-1] + ([] if last in ("index",) else [last])
            url = "/" + "/".join(_norm_seg(s) for s in segs)
            return url
    return None


def _norm_seg(s: str) -> str:
    """[id] / [...slug] / :id -> a templated param token."""
    if (s.startswith("[") and s.endswith("]")) or s.startswith(":") or (s.startswith("{") and s.endswith("}")):
        return "{}"
    return s


def extract_route_defs(rel: str, body: str) -> set[str]:
    """All route PATH strings this backend file DEFINES (normalized). Content-free."""
    ext = _ext(rel)
    routes: set[str] = set()
    # file-based routes (Next/SvelteKit) from the PATH alone
    fb = _file_based_route(rel)
    if fb:
        routes.add(fb)
    if ext == ".py":
        py_body = _blank_py_non_code_route_text(body)
        method_routes: set[str] = set()
        for rx in (_PY_DECORATOR, _PY_DJANGO):
            method_routes.update(m.group(1) for m in rx.finditer(py_body))
        # FastAPI APIRouter(prefix="/api/items") / Flask Blueprint url_prefix="/api": a method route
        # `@router.get("/{id}")` is RELATIVE to the prefix — without joining it normalizes to /{}
        # (ubiquitous, dropped) and the real /api/items/{} contract is silently missed. Join them.
        py_prefixes = {m.group(1) for m in _PY_PREFIX.finditer(py_body)}
        routes.update(_routes_with_prefixes(method_routes, py_prefixes))
    elif ext in (".ts", ".tsx", ".js", ".jsx"):
        routes.update(m.group(1) for m in _JS_ROUTE.finditer(body))
        routes.update(m.group(1) for m in _HAPI_ROUTE.finditer(body))
        # NestJS: method routes are RELATIVE to the @Controller('prefix') — join them.
        nest_methods = {m.group(1) for m in _TS_NEST_METHOD.finditer(body)}
        nest_prefixes = {m.group(1) for m in _TS_NEST_PREFIX.finditer(body)}
        if nest_methods or nest_prefixes:
            routes.update(_routes_with_prefixes(nest_methods, nest_prefixes))
    elif ext == ".go":
        routes.update(m.group(1) for m in _GO_HANDLEFUNC.finditer(body))
        routes.update(m.group(1) for m in _GO_METHOD.finditer(body))
        # NESTED ROUTE-GROUP prefixes (gin/chi/echo closure mounting): join `m.Group("/x", func(){ m.Get(
        # "/y") })` → `/x/y`. The brace-depth scanner ALSO re-emits each bare leaf, so this is a strict
        # superset of the _GO_METHOD leaves above (recall-only; an over-joined path just fails to match).
        routes.update(_go_grouped_routes(body))
    elif ext == ".rb":
        routes.update(m.group(1) for m in _RB_VERB.finditer(body))
        routes.update("/" + m.group(1) for m in _RB_RESOURCES.finditer(body))
    elif ext in (".java", ".kt"):
        routes.update(m.group(1) for m in _JVM_MAPPING.finditer(body))
        if ext == ".kt":
            # Ktor routing DSL: get("/x") (Spring annotations already handled by _JVM_MAPPING above).
            routes.update(m.group(1) for m in _KT_KTOR.finditer(body))
    elif ext == ".php":
        routes.update(m.group(1) for m in _PHP_ROUTE.finditer(body))
    elif ext == ".rs":
        routes.update(m.group(1) for m in _RS_ACTIX.finditer(body))
        routes.update(m.group(1) for m in _RS_AXUM.finditer(body))
    elif ext == ".cs":
        # ASP.NET Core: attribute method routes [HttpGet("{id}")] are RELATIVE to the controller
        # [Route("api/orders")] prefix (join); minimal-API app.MapGet("/api/x", ...) is absolute.
        cs_methods = {m.group(1) for m in _CS_HTTP_METHOD.finditer(body)}
        cs_prefixes = {m.group(1) for m in _CS_ROUTE_PREFIX.finditer(body)}
        if cs_methods or cs_prefixes:
            routes.update(_routes_with_prefixes(cs_methods, cs_prefixes))
        routes.update(m.group(1) for m in _CS_MINIMAL.finditer(body))
    elif ext in (".ex", ".exs"):
        # Phoenix: `get "/x", Controller, :action` relative to `scope "/prefix" do`; resources "/things".
        ex_methods = {m.group(1) for m in _EX_VERB.finditer(body)}
        ex_methods.update(m.group(1) for m in _EX_RESOURCES.finditer(body))
        ex_scopes = {m.group(1) for m in _EX_SCOPE.finditer(body)}
        if ex_methods or ex_scopes:
            routes.update(_routes_with_prefixes(ex_methods, ex_scopes))
    return {normalize_route(r) for r in routes if r}


# ---------------------------------------------------------------------------------------------
# Request-URL extraction (frontend side). Capture ONLY the literal/template URL string.
# ---------------------------------------------------------------------------------------------
# fetch("/api/x"), fetch(`/api/${id}`), axios.get('/api/x'), ky.post(`/x`), $fetch('/x'),
# useSWR('/api/x'), useQuery(... '/api/x' ...) — we scan the first string arg of these callers.
# this.{get,post,put,patch,delete}("/api/x") — the SERVICE-CLIENT idiom: a class API client whose
# methods call its own base-class request helper (`class X extends APIService { m(){ return
# this.get(`/api/...`) } }`). MEASURED on plane_web: 340 such calls, 0 captured by the fetch/axios
# idioms (the request rode `this`, not a named client) → the entire client→backend contract was
# graph-blind. The verb is REQUIRED after `this.` (a bare `this.foo(...)` is NOT a request) so only the
# HTTP-verb-named base-class helpers match. PRECISION (within-repo coupling): `this.get('/x')` cannot be
# told from a backend `app.get('/x')` route DEFINITION by syntax alone, but the within-repo matcher is
# already guarded against this exact ambiguity — (a) the `is_specific` floor drops bare/ubiquitous
# routes, (b) match_pairs SKIPS a route a file BOTH defines and requests (a server defines its routes,
# it does not fetch them), and (c) multi-definer suppression drops a route declared by >1 file — so a
# `this.<verb>` request that happens to collide with a real route only ever couples to the GENUINE
# single backend definer (the intended contract), never fabricates one. (The flag-gated cross-repo
# path's `_RECEIVER_VERB_CALL` already treats `this` as an unambiguous request receiver; this brings the
# same idiom to the always-on within-repo coupling.)
_REQ_CALL = re.compile(
    r"""\b(?:fetch|axios(?:\.\w+)?|ky(?:\.\w+)?|\$fetch|useSWR|useQuery|request|http(?:\.\w+)?|api(?:\.\w+)?|this\.(?:get|post|put|patch|delete))\s*\(\s*["'`]([^"'`]+)["'`]""")
# axios.get(url) where url is a bare string assigned: also catch `url: "/api/x"` config objects
_REQ_URLKEY = re.compile(r"""\burl\s*:\s*["'`]([^"'`]+)["'`]""")
# template literals with ${} interpolation -> keep the literal prefix + a param token
_TEMPLATE_INTERP = re.compile(r"\$\{[^}]*\}")

# --- GO CLIENT/SDK request URLs (cross-tier, cross-repo gap). A Go API CLIENT issues a request by
# passing an HTTP VERB + a URL LITERAL to a request helper: `c.getResponse("GET", "/user/repos", ...)`,
# `c.getParsedResponse("PATCH", fmt.Sprintf("/repos/%s/%s", owner, name), ...)`, or the stdlib
# `http.NewRequest("GET", "/x", body)`. MEASURED on go-gitea/go-sdk: repo.go issues `getParsedResponse("GET",
# fmt.Sprintf("/repos/%s/%s", ...))` 150× across the SDK, 0 of which the JS/TS-only extractor saw — so a Go
# client that talks to a Gitea-style backend in ANOTHER repo had ZERO consumer keys. The URL is either a
# direct string literal OR the FORMAT STRING of a wrapping `fmt.Sprintf(...)` (take the literal; the `%s/%d/%v`
# verbs are template params → normalized to `{}` exactly like a JS `${id}`). Anchored on the verb-first
# request-helper call shape (a leading HTTP-verb string argument), so a non-request `Sprintf` building, say,
# a log line is NOT swept (it has no leading verb literal). content-free (verb + URL literal only).
_GO_VERB = r'"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)"'
_GO_STR = r'"([^"]+)"'   # a captured Go string literal (kept separate so the regex never ends in """")
# `<helper>("VERB", "/literal"...)` — request helper with a plain string URL. The helper name is open
# (getResponse/getParsedResponse/doRequest/Do/NewRequest/Request/…); the VERB-FIRST shape is the anchor.
_GO_REQ_LITERAL = re.compile(r"\b\w+\s*\(\s*" + _GO_VERB + r"\s*,\s*" + _GO_STR)
# `<helper>("VERB", fmt.Sprintf("/format/%s", ...)...)` — request helper whose URL is a Sprintf format string.
_GO_REQ_SPRINTF = re.compile(r"\b\w+\s*\(\s*" + _GO_VERB + r"\s*,\s*fmt\.Sprintf\(\s*" + _GO_STR)
# Go format verbs (`%s`, `%d`, `%v`, `%+v`, `%q`, width/precision like `%2d`) → a template param `{}`.
_GO_FMT_VERB = re.compile(r"%[#+\- 0]*\d*(?:\.\d+)?[a-zA-Z]")


def _go_request_urls(body: str) -> set[str]:
    """URL literals a Go client file ISSUES via verb-first request helpers (direct string OR Sprintf
    format string). `%s/%d/%v` format verbs become `{}` so the path matches the backend's `/x/{}`.
    Content-free (the verb + the URL literal/format)."""
    raw: set[str] = set()
    for m in _GO_REQ_LITERAL.finditer(body):
        raw.add(m.group(1))
    for m in _GO_REQ_SPRINTF.finditer(body):
        raw.add(_GO_FMT_VERB.sub("{}", m.group(1)))   # collapse %s/%d/%v to the template token
    return raw


def extract_request_urls(rel: str, body: str) -> set[str]:
    """All request URL strings this file ISSUES (normalized). Content-free.
    JS/TS: fetch/axios/ky/$fetch/useSWR/useQuery/this.<verb>/url-config idioms. Go: verb-first client
    request helpers (`getResponse`/`getParsedResponse`/`http.NewRequest`/…) with a string or Sprintf URL."""
    urls: set[str] = set()
    ext = _ext(rel)
    body = _sfc_executable_text(body, ext)
    if ext == ".go":
        # Go client SDKs issue requests via verb-first helpers, NOT the JS fetch/axios idioms above —
        # so a separate, verb-anchored matcher (it would be wrong to run the JS `_REQ_CALL` over Go).
        urls.update(_go_request_urls(body))
    else:
        for rx in (_REQ_CALL, _REQ_URLKEY):
            for m in rx.finditer(body):
                urls.add(m.group(1))
    out = set()
    for u in urls:
        # drop full external URLs (http(s)://...) — those are not THIS backend's contract
        if re.match(r"^[a-z]+://", u):
            continue
        if not u.startswith("/"):
            # allow `api/x` relative; prefix a slash for matching
            if re.match(r"^[\w\-/.${}]+$", u):
                u = "/" + u
            else:
                continue
        out.add(normalize_route(u))
    return {u for u in out if u}


# ---------------------------------------------------------------------------------------------
# Normalization + matching
# ---------------------------------------------------------------------------------------------
def normalize_route(r: str) -> str:
    """Canonicalize a route/url for cross-tier matching, content-free.
    - strip query string / trailing slash
    - collapse template params (`:id`, `{id}`, `${x}`, `<int:id>`, `[id]`, regex groups) -> `{}`
    - lower-case host-free path
    Returns "" for empties."""
    if not r:
        return ""
    r = r.split("?")[0].split("#")[0].strip()
    r = _TEMPLATE_INTERP.sub("{}", r)            # JS `${id}` -> {}
    r = re.sub(r":[A-Za-z_]\w*", "{}", r)         # express/rails `:id` -> {}
    r = re.sub(r"\{[^}]*\}", "{}", r)             # flask/fastapi `{id}`, django `<int:id>` handled below
    r = re.sub(r"<[^>]*>", "{}", r)               # django `<int:id>` -> {}
    r = re.sub(r"\[\.\.\.[^\]]*\]", "{}", r)      # next `[...slug]` -> {}
    r = re.sub(r"\[[^\]]*\]", "{}", r)            # next `[id]` -> {}
    r = re.sub(r"\(\?[^)]*\)|\(\.\*\)|\^|\$", "", r)  # rails/django regex artifacts
    if not r.startswith("/"):
        r = "/" + r
    if len(r) > 1:
        r = r.rstrip("/")
    # collapse duplicate slashes
    r = re.sub(r"/{2,}", "/", r)
    return r.lower()


# Ubiquitous / bare routes that must NOT couple everything.
_UBIQUITOUS = {
    "/", "/api", "/health", "/healthz", "/ping", "/status", "/login", "/logout",
    "/auth", "/me", "/user", "/users", "/index", "/home", "/data", "/v1", "/api/v1",
    "/graphql", "/", "/admin", "/test", "/callback", "/{}",
}


def segments(route: str) -> list[str]:
    return [s for s in route.split("/") if s]


def is_specific(route: str) -> bool:
    """Specificity floor (the precision guarantee). A route anchors a contract ONLY if it names a
    concrete resource. MEASURED on real AI-era repos: the naive ">=2 segments" floor still let
    LEADING-PARAM routes through (`/{id}/config`, `/{plugin}/execute`, `/{}/auth/refresh`) — those
    match ANY frontend call `fetch(`/${x}/config`)` regardless of resource, so they coupled unrelated
    files and drove matched co-change BELOW random (a false fan-out, exactly the ubiquitous-name trap).
    The fix that the data demanded: require >=2 segments AND a CONCRETE first segment (not a leading
    `{}` param). On Zen-Ai-Pentest this lifted matched co-change from 3% (lift 0.76, worse than random)
    to 100% (lift 22.0) by dropping the 181 leading-param pairs while keeping the real contracts.

    Specific  iff:  route not ubiquitous  AND  >=2 segments  AND  first segment is concrete.
    A single-segment route is specific ONLY if it is itself a templated resource id (rare; e.g. a
    REST collection-item under an empty prefix) — but never the bare `/{}`.
    """
    if not route or route in _UBIQUITOUS:
        return False
    segs = segments(route)
    if len(segs) >= 2:
        return segs[0] != "{}"          # leading-param routes match across unrelated resources -> drop
    if len(segs) == 1 and "{}" in route:
        return route != "/{}"
    return False


def _seg_eq(a: str, b: str) -> bool:
    """Two path segments are the same slot if equal, OR either side is a templated param `{}`
    (a frontend that calls `/users/5` hits the backend's `/users/{}` — the concrete value fills
    the slot). A concrete-vs-concrete mismatch is a genuine non-match."""
    return a == b or a == "{}" or b == "{}"


def _slots_align(a: list[str], b: list[str]) -> bool:
    """Every position matches (a `{}` param fills a concrete value), AND at least one CONCRETE
    segment agrees positionally. MEASURED reason: without the concrete-agreement requirement, two
    `{}` wildcards on opposite sides make `/plans/{}` match `/api/orgs/{}/avatar` (last-2 tail
    `[plans,{}]` vs `[{}, avatar]` — both slots 'match' via wildcard) = a real false positive seen
    on vstorm. Requiring a shared concrete anchor kills it while keeping `/users/{}` ~ `/users/5`."""
    if len(a) != len(b):
        return False
    concrete_agree = False
    for x, y in zip(a, b):
        if not _seg_eq(x, y):
            return False
        if x == y and x != "{}":
            concrete_agree = True
    return concrete_agree


def match_routes(backend_route: str, frontend_url: str) -> bool:
    """Do a backend-defined route and a frontend-issued url refer to the SAME contract?
    Segment-wise template-aware with a CONCRETE-anchor requirement: equal length with every slot
    matching AND >=1 concrete segment agreeing, OR the shorter route's segments are the TAIL of the
    longer (backend mount-prefix: backend `/things/{}` under a router mounted at `/api`, frontend
    `/api/things/5`) — again with a shared concrete anchor. The tail must be >=2 segments so a
    single shared segment can't couple, and two opposing `{}` wildcards can't fabricate a match."""
    if not backend_route or not frontend_url:
        return False
    bsegs, fsegs = segments(backend_route), segments(frontend_url)
    if len(bsegs) == len(fsegs):
        return _slots_align(bsegs, fsegs)
    short, long = (bsegs, fsegs) if len(bsegs) <= len(fsegs) else (fsegs, bsegs)
    if len(short) >= 2:
        return _slots_align(short, long[-len(short):])
    return False


# ---------------------------------------------------------------------------------------------
# Repo scan -> candidate cross-tier couplings
# ---------------------------------------------------------------------------------------------
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", ".next", "target",
             "__pycache__", ".venv", "venv", "site-packages", ".svelte-kit", "out", "coverage"}
MAX_FILE_BYTES = 400_000

# OUTPUT CAP (the analogue of _cg_schema._MAX_TABLES / _cg_config._MAX_CONFIG_KEYS). match_pairs emits at
# most one couple per (definer, requester) FILE pair, so this caps the number of emitted couples. The bug
# (verified HIGH DoS): the pre-fix nested loop `for bfile in defs: for ffile in reqs: for br in broutes:
# for fu in furls: match_routes(br,fu)` is O(B×F×R×U) with NO ceiling and NO time budget. An adversarial
# repo of N backend files each defining a distinct specific single-definer route + N frontend files each
# requesting all N routes drives O(N⁴): MEASURED match 20→0.23s, 40→3.54s, 60→17.84s, 80→55.7s; ~800 files
# ≈ 10 HOURS — and there is NO timeout around build_graph, so the single event worker hangs for hours and
# starves every other repo. The _MAX_INGEST_FILES file-count guard is on COUNT, not time, so it does not
# help. This bound guarantees termination: once _MAX_ROUTE_PAIRS couples are emitted we STOP (degrade to no
# further coupling — recall-safe, the same discipline as _routes_graph's except→[] and the sibling output
# caps). 5000 is generously above any real repo's cross-tier contract count (the proven probe measured tens
# of couples on real AI-era repos); a repo that exceeds it is pathological/adversarial, not a real contract.
_MAX_ROUTE_PAIRS = 5000

# TOTAL-WORK BUDGET (defensive, the part that makes the time bound HOLD even when the inverted index
# degenerates). The index buckets request URLs by CONCRETE segment token; for repos whose contracts use
# DISTINCT resource names this makes the work ~linear (each request meets only its real definer). But a
# pathological repo can defeat any token index by making one concrete token UBIQUITOUS — e.g. every route
# shares the segment `api` (`/api/back0/{}`, `/api/back1/{}`, …): the `api` bucket then holds all request
# URLs and every definer route probes all of them, re-creating the O(N⁴) cross product THROUGH the index.
# So we also cap the TOTAL number of match_routes comparisons; once exceeded we stop probing further (the
# same graceful degradation as the output cap — the absence of a few route-only couples is the pre-#270
# behavior, never a crash, never a regression of another detector). Keep this deliberately low: the route
# matcher is pure advisory, while an unbounded route scan starves the single webhook worker.
_MAX_ROUTE_PAIR_PROBES = 75_000

# PER-ROUTE CANDIDATE CAP. A single ubiquitous concrete token (`api`) can put every request URL in one bucket.
# Probe the smallest concrete-token buckets first, and stop adding candidates for one backend route once this
# many distinct request URLs are queued. Real route contracts are in the tens; a route that can only be matched
# by hundreds of same-token candidates is already low-confidence fan-out, so this is the right fail-soft point.
_MAX_ROUTE_CANDIDATES_PER_ROUTE = 512


def _concrete_anchors(route: str) -> set:
    """The CONCRETE (non-`{}`) path segments of a route. EVERY True from match_routes requires at least
    one concrete segment that AGREES positionally between the two routes (the `concrete_agree` requirement
    in _slots_align; match_routes also rejects a <2-segment short side, never matching on a lone `{}`). So
    two routes that share NO concrete segment can NEVER match — the index below uses this to skip the full
    R×U cross product (compare a requester only against definer routes that share a concrete token), which
    is recall-safe: it can only avoid comparisons that were guaranteed to be non-matches."""
    return {s for s in segments(route) if s != "{}"}


def _walk(root: str):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            full = os.path.join(dp, fn)
            # Skip symlinked FILES (path-traversal guard). os.walk already does not RECURSE into
            # symlinked dirs by default, but a symlinked regular file (e.g. `evil.js -> /etc/passwd`
            # or `-> /proc/self/environ`) would otherwise be open()/read()/route-scanned, exfiltrating
            # a host file's content into the graph. Not reachable from prod App ingest (the tarball
            # _safe_extractall drops symlink members; co-change uses --no-checkout), but scan_repo is
            # ALSO driven by the local CLI (dogfood.sh / watch.py / evaluate.py / build_graph) over a
            # real checked-out tree, where a crafted repo could plant such a link. Every OTHER walk in
            # the engine already routes through _passes_file_guards/islink — this was the one exception.
            if os.path.islink(full):
                continue
            yield full


def scan_repo(root: str, specificity_floor: bool = True, source_files=None,
              incomplete_paths_out=None) -> dict:
    """Returns route-defs per backend file, request-urls per frontend file, and matched
    cross-tier file pairs (backend_file, frontend_file). All paths are repo-relative.

    When ``source_files`` is supplied it is the authoritative, already guard-filtered
    file universe from ``build_graph``. This is load-bearing: independently walking
    ``root`` here used to re-admit paths excluded by .gitattributes and applied a
    different 400 KiB size policy than every other extraction pass.
    """
    root = os.path.abspath(root)
    defs: dict[str, set[str]] = {}      # rel -> {routes}
    reqs: dict[str, set[str]] = {}      # rel -> {urls}
    # CROSS-REPO extraction (flag-gated, ADDITIVE). When the flag is OFF these stay EMPTY and nothing
    # below changes — the within-repo `defs`/`reqs`/`pairs` (and the byte-identical graph) are untouched.
    # When ON, they collect the DISAMBIGUATED definer routes + the BROADENED consumer URLs (the service-
    # client `this.<verb>` idiom) over the SAME walk/body (no second walk).
    xrepo = cross_repo_keys_enabled()
    xrepo_defs: dict[str, set[str]] = {}   # rel -> {routes this file genuinely DEFINES (disambiguated)}
    xrepo_reqs: dict[str, set[str]] = {}   # rel -> {urls this file ISSUES (incl. service-client idiom)}
    route_exts = ROUTE_DEF_EXT | REQUEST_EXT
    if source_files is None:
        candidates = [
            (full, _ext(full))
            for full in _walk(root)
            if _ext(full) in route_exts
        ]
    else:
        candidates = [
            (full, ext)
            for full, ext in source_files
            if ext in route_exts
        ]
    candidate_paths = {
        os.path.relpath(full, root).replace(os.sep, "/")
        for full, _extn in candidates
    }
    catalog_incomplete = False
    for full, ext in candidates:
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        read_loss: set[str] = set()
        body = _read_capped(full, read_loss, rel)
        if body is None:
            catalog_incomplete = True
            _mark_incomplete(incomplete_paths_out, rel)
            continue
        if read_loss:
            catalog_incomplete = True
            _mark_incomplete(incomplete_paths_out, rel)
        # Route DEFINITIONS from a test/fixture app are not a real backend contract: skip them as
        # definers (a test app re-declaring a real route must not false-couple a frontend to the test
        # file, nor inflate the multi-definer count below and suppress the genuine coupling). Test files
        # are still scanned as REQUESTERS just below — a test that calls a real route IS coupled to it.
        try:
            if ext in ROUTE_DEF_EXT and not _is_test_path(rel):
                d = extract_route_defs(rel, body)
                if d:
                    defs[rel] = d
            if ext in REQUEST_EXT:
                r = extract_request_urls(rel, body)
                if r:
                    reqs[rel] = r
            if xrepo:
                # CROSS-REPO definer routes (disambiguated: a web/client file or a `this.<verb>` request is
                # NOT a definition). Test-dir definers are still excluded (same rationale as the within-repo
                # path) so a test app can't false-couple / inflate the multi-definer count cross-repo.
                if ext in ROUTE_DEF_EXT and not _is_test_path(rel):
                    xd = _xrepo_route_defs(rel, body)
                    if xd:
                        xrepo_defs[rel] = xd
                # CROSS-REPO consumer URLs (broadened to the service-client `this.<verb>(url)` idiom).
                if ext in REQUEST_EXT:
                    xr = _xrepo_request_urls(rel, body)
                    if xr:
                        xrepo_reqs[rel] = xr
        except Exception:
            # Every route-bearing source may change the route definition/request catalog and therefore
            # multi-definer resolution for other files. Keep the partial graph, but make the loss visible.
            catalog_incomplete = True
            _mark_incomplete(incomplete_paths_out, rel)
    if catalog_incomplete:
        for rel in candidate_paths:
            _mark_incomplete(incomplete_paths_out, rel)
    ambiguous_pairs: dict = {}
    pairs = match_pairs(
        defs, reqs, specificity_floor, ambiguous_out=ambiguous_pairs,
        incomplete_paths_out=incomplete_paths_out,
    )
    out = {
        "defs": defs,
        "reqs": reqs,
        "pairs": pairs,
        "ambiguous_pairs": ambiguous_pairs,
    }
    if xrepo:
        out["xrepo_defs"] = xrepo_defs
        out["xrepo_reqs"] = xrepo_reqs
    return out


# Component extensions that are DEFINITELY UI (never an Express/Koa server file).
_UI_COMPONENT_EXT = {".tsx", ".jsx", ".svelte", ".vue", ".astro"}


def _is_cross_tier(definer: str, requester: str) -> bool:
    """Drop the one same-tier artifact: BOTH files are UI components (`.tsx/.jsx/.svelte/.vue`).
    MEASURED reason: the JS route regex (`app.get('/x')`) cannot be told apart from a frontend
    REQUEST call (`api.get('/x')`) by syntax, so a UI component is sometimes mis-read as a route
    DEFINER, producing component↔component SAME-tier pairs that are NOT a backend↔frontend contract.
    Two UI components are never a client/server contract -> drop. Everything else is kept: a real
    backend-language file (.py/.go/...) on either side, OR a `.ts/.js` Express server file paired
    with a UI component or another `.ts/.js` client (genuinely ambiguous but a legitimate contract
    shape we keep for recall — the specificity + leading-param floors carry the precision there)."""
    de, re_ = _ext(definer), _ext(requester)
    if de in _UI_COMPONENT_EXT and re_ in _UI_COMPONENT_EXT:
        return False
    return True


def match_pairs(defs: dict, reqs: dict, specificity_floor: bool,
                ambiguous_out=None, incomplete_paths_out=None) -> dict:
    """Returns {frozenset(backend_file, frontend_file): {shared_route, ...}}.
    With the specificity floor, only SPECIFIC routes can anchor a coupling; the cross-tier guard
    drops same-tier (mis-read JS-definer) pairs so only genuine backend↔frontend contracts emit."""
    # MULTI-DEFINER SUPPRESSION (mirrors the _cg_openapi / _cg_iac P0 guard). A route DEFINED by >1
    # backend file is AMBIGUOUS — the same path declared by multiple services (a monorepo), or a real
    # app plus a second copy — so we cannot know WHICH backend a frontend call binds to. Coupling the
    # caller to ALL definers (and the definers to each other) is wallpaper: the sibling detectors
    # measured 66-79% of their false couples were exactly this class. Such a route anchors NO coupling.
    # SINGLE-definer routes — the genuine one-backend contract — are unaffected (recall preserved).
    # Counted over DISTINCT files on the NORMALIZED route string (test-app definers are already excluded
    # in scan_repo, so a real route shadowed by a test copy stays single-definer and keeps coupling).
    route_definers: dict = {}
    for bfile, broutes in defs.items():
        bf_reqs = reqs.get(bfile, ())   # routes this SAME file also issues requests to
        for br in broutes:
            # A file that both DEFINES and REQUESTS the same route is a FRONTEND client mis-read as a
            # definer: `axios.get('/r')` parses as BOTH a route-def and a request (the .ts/.js def vs
            # request ambiguity _is_cross_tier handles at the pair level). It is NOT a real backend
            # definer of R — a server defines its routes but does not fetch them — so it must not inflate
            # R's definer count and falsely SUPPRESS a genuine single-backend contract (the A2 regression).
            if br in bf_reqs:
                continue
            route_definers.setdefault(br, set()).add(bfile)

    # INVERTED INDEX (the fix for the O(B×F×R×U) DoS). Instead of the full nested cross product, index every
    # SPECIFIC request URL by its CONCRETE segments → for a definer route we only test the requests that
    # share a concrete token (a necessary condition for ANY match — see _concrete_anchors). For a definer
    # route with multiple concrete tokens, probe the SMALLEST buckets first and cap the per-route candidate
    # set. This is load-bearing: a token like `api` appears in almost every route and defeats a naive token
    # index; the rarer segment (`orders`, `invoices`, `resource42`) is the useful anchor. If every anchor is
    # huge, the route is low-confidence fan-out and the candidate cap intentionally degrades to silence.
    #
    # url_index: concrete-token → list of (normalized_url, requester_file). Built once over all requesters.
    url_index: dict = {}
    seen_url_per_file: dict = {}     # requester_file → set of urls already indexed (dedup per file)
    for ffile, furls in reqs.items():
        for fu in furls:
            if specificity_floor and not is_specific(fu):
                continue
            anchors = _concrete_anchors(fu)
            if not anchors:
                continue            # a request with no concrete segment can never match → never indexed
            seen = seen_url_per_file.setdefault(ffile, set())
            if fu in seen:
                continue
            seen.add(fu)
            for tok in anchors:
                url_index.setdefault(tok, []).append((fu, ffile))

    relevant_paths = set(defs) | set(reqs)

    def _record_cap() -> None:
        for rel in relevant_paths:
            _mark_incomplete(incomplete_paths_out, rel)

    pairs: dict = {}
    capped = False
    probes = 0        # per-phase comparisons — bounded by _MAX_ROUTE_PAIR_PROBES
    work_items = []
    for bfile, broutes in defs.items():
        for br in broutes:
            if specificity_floor and not is_specific(br):
                continue
            ambiguous_route = len(route_definers.get(br, ())) > 1
            if ambiguous_route and ambiguous_out is None:
                continue
            work_items.append((ambiguous_route, bfile, br))

    # Exact pairs retain their historical budget and are always evaluated first;
    # collecting uncertainty must never starve resolved review behavior. Stable
    # sort preserves the prior iteration order within each phase.
    work_items.sort(key=lambda item: item[0])
    ambiguous_phase = False
    for ambiguous_route, bfile, br in work_items:
        if capped:
            break
        if ambiguous_route and not ambiguous_phase:
            ambiguous_phase = True
            probes = 0
        # Only requests that share a CONCRETE segment with br can match (necessary condition). Gather
        # candidate (url, requester) couples from the smallest buckets first, de-duplicating a request
        # that shares MORE THAN ONE concrete token with br. The per-route cap prevents a ubiquitous
        # anchor (`api`) from recreating the old cross product through the index.
        tested: set = set()
        buckets = sorted(
            ((len(url_index.get(tok, ())), tok) for tok in _concrete_anchors(br) if url_index.get(tok)),
            key=lambda x: (x[0], x[1]),
        )
        for _size, tok in buckets:
            for fu, ffile in url_index.get(tok, ()):
                if bfile == ffile:
                    continue
                if not _is_cross_tier(bfile, ffile):
                    continue
                if (fu, ffile) in tested:
                    continue
                tested.add((fu, ffile))
                if len(tested) > _MAX_ROUTE_CANDIDATES_PER_ROUTE:
                    _record_cap()
                    break
                probes += 1
                if probes > _MAX_ROUTE_PAIR_PROBES:
                    # WORK BUDGET hit — a pathological token-collision repo (e.g. every route shares
                    # `api`) defeated the index; stop probing. Degrade gracefully (recall-safe, same
                    # discipline as the output cap). Never reached by a realistic repo's contracts.
                    capped = True
                    _record_cap()
                    break
                if match_routes(br, fu):
                    key = frozenset((bfile, ffile))
                    if ambiguous_route:
                        # Ambiguous routes were skipped above unless a sink exists.
                        target = ambiguous_out
                    else:
                        target = pairs
                    shared = target.get(key)
                    if shared is None:
                        if len(target) >= _MAX_ROUTE_PAIRS:
                            # Ambiguous evidence has its own bounded budget
                            # and must never starve resolved route pairs.
                            if ambiguous_route:
                                capped = True
                                _record_cap()
                                break
                            # OUTPUT CEILING hit — stop emitting new file-pair couples. Bounds total work
                            # so a pathological repo can't blow time (degrade gracefully, recall-safe).
                            capped = True
                            _record_cap()
                            break
                        shared = target[key] = set()
                    shared.add(br if len(br) >= len(fu) else fu)
            if len(tested) > _MAX_ROUTE_CANDIDATES_PER_ROUTE:
                break
            if capped:
                break
    return pairs


# ---------------------------------------------------------------------------------------------
# GRAPH PRODUCER — the build_graph integration (mirrors _cg_schema / _cg_config signatures)
# ---------------------------------------------------------------------------------------------
def _routes_graph(root, source_files=None, incomplete_paths_out=None):
    """Return (nodes, edges) for the cross-tier route↔call coupling of the repo at `root`.

    nodes: [] — this producer mints NO new nodes. A cross-tier edge couples two FILES that ALREADY have
    `file` nodes (a backend source file + a frontend source file, both walked by _iter_source_files); we
    never invent a synthetic node. (Both endpoints being real file nodes is exactly what the engine's
    imp_out/imp_in adjacency requires — it joins the edge's dst to a code_node of node_kind='file'.)

    edges: file→file `imports` edges, ONE per matched contract pair, content-free (file paths only). We
    emit BOTH directions (backend→frontend and frontend→backend) so the coupling is symmetric — the engine
    reads adjacency undirected for collision/contention, but emitting both makes the file-level dependency
    visible from either edited side regardless of which way imp_out vs imp_in happens to fire.

    ``source_files`` is the authoritative, guard-filtered universe supplied by build_graph. It is passed
    through to scan_repo rather than independently walking ``root``. Never raises: a scan error returns
    the historical empty 2-tuple and records every relevant candidate as incomplete."""
    try:
        scan = scan_repo(
            root,
            specificity_floor=True,
            source_files=source_files,
            incomplete_paths_out=incomplete_paths_out,
        )
    except Exception:
        # A producer must NEVER abort build_graph (same discipline as the per-file extractor guards).
        candidates = source_files
        if candidates is None:
            candidates = [
                (full, _ext(full))
                for full in _walk(os.path.abspath(root))
            ]
        for full, ext in candidates:
            if ext in (ROUTE_DEF_EXT | REQUEST_EXT):
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                _mark_incomplete(incomplete_paths_out, rel)
        return [], []
    edges = []
    for pair in scan.get("pairs", {}):
        a, b = tuple(pair)               # frozenset of two repo-relative file paths (backend, frontend)
        # repo-relative, OS-native → forward-slash (matches every other coordinate the extractor emits).
        a = a.replace(os.sep, "/")
        b = b.replace(os.sep, "/")
        if a == b:                       # a self-pair is never a client/server contract (match_pairs excludes it too)
            continue
        # FILE→FILE `imports` edge, both directions (symmetric). dst is a real repo FILE path = exactly the
        # shape _resolve_imports produces and imp_out/imp_in consume. No schema change (kind ∈ the CHECK set).
        edges.append({"src": a, "dst": b, "kind": "imports"})
        edges.append({"src": b, "dst": a, "kind": "imports"})
    resolved_pairs = set(scan.get("pairs", {}))
    for pair in scan.get("ambiguous_pairs", {}):
        # If another unambiguous route connects this exact file pair, the normal
        # edge is authoritative and a duplicate inert edge would be redundant.
        if pair in resolved_pairs:
            continue
        a, b = tuple(pair)
        a = a.replace(os.sep, "/")
        b = b.replace(os.sep, "/")
        if a == b:
            continue
        edges.append({
            "src": a,
            "dst": b,
            "kind": "imports",
            "reference_status": "ambiguous",
        })
        edges.append({
            "src": b,
            "dst": a,
            "kind": "imports",
            "reference_status": "ambiguous",
        })
    # CROSS-REPO shared-key EDGES (flag-gated OFF → adds nothing): emitted from the SAME `scan` (no second
    # walk). When OFF this appends ZERO edges, so the within-repo `imports` output above is byte-identical.
    edges.extend(_routes_xrepo_edges(scan, incomplete_paths_out=incomplete_paths_out))
    return [], edges


# ---------------------------------------------------------------------------------------------
# CROSS-REPO shared-key producer (feasibility-spike STEP 1) — STRICTLY ADDITIVE, flag-gated OFF.
# ---------------------------------------------------------------------------------------------
# The within-repo _routes_graph above couples two files in the SAME repo by emitting a resolved
# file→file `imports` edge — there is no shared `dst` NODE, so it can NEVER couple files in two
# DIFFERENT repos (a frontend in repo B has no file-path edge to a backend in repo A). To make the
# SAME route contract couple ACROSS repos, we emit edges to a stable, repo-independent `route::<norm
# path>` KEY in code_edge.dst — the SAME alters/queries shape the other contract substrates use:
#   - a route DEFINER (backend that declares the path)  -> `alters` route::<path>
#   - a request ISSUER (frontend that fetches the path) -> `queries` route::<path>
# (We emit EDGES only, no node — an api_*/route node is dropped at ingest by the node_kind filter in
# db/schema/30_gate.sql, so cross-repo coupling rides the shared `dst` STRING in these edges alone,
# exactly as the within-repo api_* substrates do.) Two such files then couple through the shared
# `route::<path>` dst via res_adj (the SAME shared-resource adjacency `api_*` already rides) — and the
# cross-repo READ (db/schema/70_social.sql, behind VERIPSA_CROSS_REPO via the veripsa.cross_repo GUC)
# relaxes res_adj's repo-equality on the shared-resource side so the
# DEFINER in repo A and the ISSUER in repo B couple. The KEY is path-only (the request side carries
# no reliable HTTP method), gated by the EXISTING `is_specific` specificity floor (concrete first
# segment, drop bare/ubiquitous paths) so noise does not explode — the same precision guarantee the
# within-repo route matcher uses. content-free (route path strings + file paths only).
#
# PRECISION — the within-repo path matcher also enforces the MULTI-DEFINER guard (a route declared by
# >1 backend file is ambiguous → anchors no effective coupling). We mirror it here: a
# `route::<path>` DEFINED by >1 file retains inert alters/queries evidence, exactly like the api_*
# multi-definer handling in _cg_api_contract / _cg_openapi. A single-definer route↔requester
# is untouched.
#
# WHY folded INTO _routes_graph (not a new build_graph call site): _routes_graph already computes the
# scan_repo result and is the ONLY routes producer build_graph wires — so the cross-repo edges reuse the
# SAME scan (no second walk) and need NO change to code_graph_extract.py (which is another agent's lane).
# The whole emission is guarded by cross_repo_keys_enabled() so with the flag OFF it appends ZERO edges
# and the within-repo `imports` output is byte-identical (the determinism gate proves it).

# Per-key cap (adversarial-flood guard; mirrors _cg_api_contract._MAX_API_NODES spirit). Bounds the number
# of distinct route:: keys emitted so a pathological repo cannot flood the cross-repo coupling surface.
_MAX_ROUTE_KEYS = 20_000

# A control/whitespace character anywhere in a "path" means it was NOT a clean route literal. Python
# comments and triple-quoted prose are blanked before route-definition extraction, and this remains the
# final generic guard for wrapped strings or malformed captures. A real route/url path token has no
# whitespace. Reject such keys because the string becomes a cross-repo coupling anchor.
_ROUTE_KEY_BAD_CHARS = re.compile(r"\s")


def _xrepo_key_ok(path: str) -> bool:
    """True iff `path` is a clean, SPECIFIC route key usable as a cross-repo coupling anchor: it passes
    the EXISTING specificity floor (is_specific) AND contains no whitespace/control character (so a
    docstring/prose example that captured a newline never mints a noise key). content-free."""
    if not path or _ROUTE_KEY_BAD_CHARS.search(path):
        return False
    return is_specific(path)


# ---------------------------------------------------------------------------------------------
# CROSS-REPO FIX #1+#2 — mount-prefix-tolerant SUFFIX keys with a terminal-concrete-segment floor.
# ---------------------------------------------------------------------------------------------
# The exact normalized path is NOT a cross-repo-stable key: a backend route DEFINED under a mount
# prefix in a DIFFERENT file (Django `path("api/", include(...))`, an Express `app.use("/api", r)`)
# normalizes to the UN-prefixed path (`/workspaces/{}/projects`), while the frontend issues the FULL
# mounted path (`/api/workspaces/{}/projects`). The exact keys differ → no cross-repo collision (on
# makeplane/plane: exact-key overlap was 1). The WITHIN-repo matcher already solves this with TAIL
# alignment (match_routes: the shorter route's segments are the TAIL of the longer); we reuse that
# SAME logic in the KEYING so a definer's key and a consumer's key COLLIDE on a shared suffix regardless
# of mount prefix. Tail-aware keying lifted the plane overlap 1 → 557.
#
# THE PRECISION FLOOR (fix #2 — the 72% noise was wildcard-tail fan-out: a route ending in `{}/{}`
# matching ANY `.../{}/x`). We REQUIRE the TERMINAL CONCRETE (non-`{}`) segment to agree before two
# routes can collide. Concretely: a suffix key is anchored to END at the route's terminal concrete
# segment (TRAILING wildcards stripped) AND to START at a concrete segment (LEADING wildcards dropped),
# so `/x/{}/{}` cannot collide with `/y/{}/issues` (terminal concrete `x` vs `issues` differ) and a pure
# leading-wildcard tail `{}/restore` cannot fan out across unrelated resources. MEASURED on the
# makeplane/plane apps/api↔apps/web re-split: the floor took the cross-repo links to 189 (key-level) at
# 98.9% precision / 100% recall on single-definer couplings (every true coupling kept; the routes dropped
# by the multi-definer guard are genuinely defined by two backend surfaces). content-free.

# Bound the number of suffix keys emitted per route (a deep path emits a few nested tails; cap so a
# pathological very-deep path can't fan out). 5 covers a mount prefix up to several segments deep
# (the deepest real mount prefixes — `/api/v1/...` — are 1-2 segments) while staying tight.
_MAX_SUFFIX_KEYS_PER_ROUTE = 5

# A suffix must be at least this many segments (mirrors match_routes' `len(short) >= 2` tail floor:
# a single shared segment — even concrete — is too weak to anchor a cross-repo couple).
_MIN_SUFFIX_SEGMENTS = 2


def _suffix_keys(route: str, incomplete_paths_out=None, relative_path=None) -> set[str]:
    """The mount-prefix-tolerant SUFFIX keys for a normalized route, each anchored to END at the route's
    TERMINAL CONCRETE segment (fix #1 tail-awareness + fix #2 terminal-concrete floor). A definer route
    `/workspaces/{}/projects` and a consumer url `/api/workspaces/{}/projects` SHARE the suffix
    `workspaces/{}/projects`, so they collide across the mount prefix; but `/x/{}/{}` (terminal concrete
    `x`) and `/y/{}/issues` (terminal concrete `issues`) share NO terminal-concrete-anchored suffix, so
    they never collide. Returns a set of suffix strings (NO leading slash — they are TAILS, never full
    absolute paths). Empty for an all-wildcard route (`/{}/{}`), which anchors nothing (precision).

    Mirrors match_routes' tail semantics exactly: every emitted suffix has >= _MIN_SUFFIX_SEGMENTS
    segments AND ends at a concrete segment, so two suffixes collide ONLY when a concrete segment agrees
    positionally — the same `concrete_agree` requirement _slots_align enforces. content-free (path only)."""
    segs = segments(route)
    if not segs:
        return set()
    # Terminal CONCRETE segment index — strip trailing `{}` wildcards so the key ENDS at a concrete
    # segment (the terminal-concrete-agreement floor). An all-wildcard route has none → anchors nothing.
    tc = None
    for i, s in enumerate(segs):
        if s != "{}":
            tc = i
    if tc is None:
        return set()
    core = segs[: tc + 1]                       # the path up to & including the terminal concrete segment
    if len(core) < _MIN_SUFFIX_SEGMENTS:
        return set()                            # nothing >= 2 segments ending at a concrete → too weak
    out: set[str] = set()
    # Every suffix of `core` (>= _MIN_SUFFIX_SEGMENTS segments) that STARTS at a CONCRETE segment. The
    # longest is the most specific (full un-prefixed path); shorter ones strip leading mount-prefix
    # segments so a consumer carrying a mount prefix the definer never saw still meets it. Requiring a
    # CONCRETE FIRST segment (not just a concrete terminal) completes the terminal-concrete floor: a pure
    # LEADING-WILDCARD suffix like `{}/restore` matches ANY `.../{}/restore` across unrelated resources
    # (the residual wildcard-tail fan-out). MEASURED on plane: dropping leading-wildcard suffixes took the
    # links 308 → 189 (the validation's exact terminal-concrete-floor target) while losing ZERO true
    # couplings (the only dropped pair was a false positive). Bounded by _MAX_SUFFIX_KEYS_PER_ROUTE.
    n = len(core)
    for start in range(0, n - _MIN_SUFFIX_SEGMENTS + 1):
        if core[start] == "{}":
            continue                            # leading-wildcard suffix → fan-out, drop (precision floor)
        if len(out) >= _MAX_SUFFIX_KEYS_PER_ROUTE:
            _mark_incomplete(incomplete_paths_out, relative_path)
            break
        out.add("/".join(core[start:]))
    return out


# ---------------------------------------------------------------------------------------------
# CROSS-REPO FIX #3 — service-client consumer idiom + def/request DISAMBIGUATION.
# ---------------------------------------------------------------------------------------------
# THE GAP (measured on plane): the frontend issues requests through a CUSTOM service base class —
# `this.get(url)` / `this.post(url, data)` (a `class FooService extends APIService` whose base wraps
# axios). The within-repo `_REQ_CALL` matcher only knows the bare-fetch/axios/ky idioms, so it MISSES
# all 438+ `this.<verb>(url)` calls — the consumer side emits nothing, so NO cross-repo couple forms.
# WORSE: `_JS_ROUTE` (`\b\w+\.(get|post|...)\(("...")`) MIS-classifies `this.get('/api/...')` as a
# server route DEFINITION (`alters`), so the service file would emit the WRONG direction — a frontend
# consumer masquerading as a backend definer. That both breaks the direction-aware definer→consumer
# link AND inflates the multi-definer count, suppressing the genuine backend's couple.
#
# THE FIX (cross-repo-ONLY, flag-gated — the WITHIN-repo extract_request_urls/extract_route_defs are
# UNTOUCHED, so the flag-OFF within-repo graph stays byte-identical):
#   (a) broaden the request matcher to the service-client idiom `this.<verb>(url)` AND a generic
#       `<client>.<verb>(url)` where the receiver is a known HTTP-client name (axios/api/http/client/
#       ...) — capturing the URL-LITERAL first argument; and
#   (b) DISAMBIGUATE def vs request: a server route DEFINITION registers a handler on a ROUTER/APP/
#       SERVER receiver (app.get/router.post/server.route); a REQUEST is issued on `this` or an HTTP-
#       client receiver. So when classifying JS/TS route DEFINITIONS for the cross-repo path we DROP
#       any `<receiver>.<verb>(url)` whose receiver is `this` or a known client (it is a request, not a
#       definition), AND we suppress route-definition extraction entirely for files living in CLIENT/WEB
#       code (a `.../web/.../*.service.ts` never DEFINES a backend route — it consumes one). This makes
#       the direction-aware definer→consumer link correct. content-free (URL literals + receiver names).

# Receivers that denote a server route REGISTRATION (the framework router/app/server handle). A
# `<one-of-these>.<verb>(...)` is a route DEFINITION. (Express/Koa/Fastify/Hapi/Nest-ish surfaces.)
_ROUTE_DEF_RECEIVERS = frozenset({
    "app", "router", "server", "route", "routes", "api_router", "apirouter",
    "blueprint", "bp", "fastify", "koa", "express", "r", "v1", "v2",
})
# Receivers that denote an HTTP-CLIENT request (the consumer side), used by the request MATCHER. `this`
# is the service-base-class idiom; the rest are common client-instance names. A `<one-of-these>.<verb>
# (url)` in client code is a REQUEST. (Some — `api`/`client`/`http` — DOUBLE as Express router names in
# BACKEND code; the request matcher may over-include those, which is harmless: a consumer key couples
# NOTHING unless a real producer `alters` the same key. The DEFINITION disambiguator below is stricter.)
_REQUEST_RECEIVERS = frozenset({
    "this", "axios", "http", "https", "client", "httpclient", "apiclient",
    "api", "ky", "request", "fetcher", "instance", "axiosinstance", "service",
})
# The UNAMBIGUOUS request receivers — names a server route framework NEVER registers routes on (no web
# framework writes `this.get('/x', handler)` / `axios.get(...)` to DEFINE a route). ONLY these are used
# to SUBTRACT a mis-classified request from a file's route DEFINITIONS, so a legitimate backend Express
# router happening to be named `api`/`client`/`http` (`const api = express.Router(); api.get('/x', h)`)
# is NOT wrongly stripped of its real route defs. The web/client-PATH guard handles the frontend wholesale,
# so an ambiguous receiver in frontend code is already covered by location; this set guards the rest.
_UNAMBIGUOUS_REQUEST_RECEIVERS = frozenset({
    "this", "axios", "ky", "httpclient", "apiclient", "fetcher", "axiosinstance",
})
# `<receiver>.<verb>(  "url-literal"  )` — the verb + the leading STRING-LITERAL argument. Used by BOTH
# the consumer-idiom request matcher (keep client/this receivers) and the def-disambiguator (drop them).
_RECEIVER_VERB_CALL = re.compile(
    r"""\b(\w+)\.(get|post|put|patch|delete|head|options)\s*\(\s*["'`]([^"'`]+)["'`]""")


def _is_client_web_path(rel: str) -> bool:
    """True when a path segment declares the file lives in CLIENT/WEB/FRONTEND code (a service-client
    that CONSUMES routes, never a server that DEFINES them). Directory segments only (content-free)."""
    low = rel.replace("\\", "/").lower()
    segs = low.split("/")[:-1]
    return any(s in segs for s in ("web", "frontend", "client", "ui", "webapp", "www"))


def _xrepo_request_urls(rel: str, body: str) -> set[str]:
    """CROSS-REPO consumer-side URLs for ONE file: the within-repo request URLs (the proven fetch/axios/
    ky/$fetch idioms via extract_request_urls) PLUS the service-client idiom `this.<verb>(url)` and a
    generic `<client>.<verb>(url)` (receiver in _REQUEST_RECEIVERS). This recovers the 438+ plane
    `this.get(...)` calls the within-repo matcher misses. Normalized + content-free (URL literals only).
    NOT called on the within-repo path → the within-repo `reqs` (and its byte-identical graph) is
    untouched."""
    urls = set(extract_request_urls(rel, body))          # the proven within-repo idioms (already normalized)
    request_body = _sfc_executable_text(body, _ext(rel))
    raw: set[str] = set()
    for m in _RECEIVER_VERB_CALL.finditer(request_body):
        recv = m.group(1).lower()
        if recv in _REQUEST_RECEIVERS:                   # this.get / axios.get / api.post / client.patch ...
            raw.add(m.group(3))
    for u in raw:
        if re.match(r"^[a-z]+://", u):                   # external absolute URL → not this backend's contract
            continue
        if not u.startswith("/"):
            if re.match(r"^[\w\-/.${}]+$", u):
                u = "/" + u
            else:
                continue
        nu = normalize_route(u)
        if nu:
            urls.add(nu)
    return urls


def _xrepo_route_defs(rel: str, body: str) -> set[str]:
    """CROSS-REPO definer-side routes for ONE file, with def/request DISAMBIGUATION. Starts from the
    proven within-repo route defs (extract_route_defs) then REMOVES the ones that are actually REQUESTS:
      - a file in CLIENT/WEB code DEFINES no backend route → return NOTHING (it only consumes);
      - otherwise drop any route that the within-repo JS/TS extractor captured from a `this.<verb>(url)`
        or `<client>.<verb>(url)` REQUEST call (receiver in _REQUEST_RECEIVERS / not a router receiver) —
        `_JS_ROUTE` cannot tell `this.get('/x')` (a request) from `app.get('/x')` (a definition) by
        syntax, so we subtract the request-receiver hits here. The result is the file's GENUINE server
        route definitions only. content-free (URL literals + receiver names). NOT on the within-repo
        path → the within-repo `defs` (and its byte-identical graph) is untouched."""
    # A service-client in web/frontend code is never a backend definer — it consumes. Emit nothing as a
    # definer (it still emits as a CONSUMER via _xrepo_request_urls). This is the strongest disambiguator.
    if _is_client_web_path(rel):
        return set()
    defs = set(extract_route_defs(rel, body))
    ext = _ext(rel)
    if ext in (".ts", ".tsx", ".js", ".jsx"):
        # Subtract routes that came from an UNAMBIGUOUS REQUEST receiver (`this`/axios/ky/…) — those are
        # consumer calls mis-read as definitions by _JS_ROUTE (a server never DEFINES a route via
        # `this.get(...)`). We deliberately use the UNAMBIGUOUS set (not every client name) so a legitimate
        # backend Express router named `api`/`client`/`http` keeps its real route defs (the web/client-PATH
        # guard already drops genuine frontend service files wholesale). DEFINITION-receiver (app/router/
        # server) `.verb(...)` and the framework-anchored Nest/Hapi/file-based patterns survive as defs.
        request_routes: set[str] = set()
        def_receiver_routes: set[str] = set()
        for m in _RECEIVER_VERB_CALL.finditer(body):
            recv = m.group(1).lower()
            nr = normalize_route(m.group(3))
            if not nr:
                continue
            if recv in _UNAMBIGUOUS_REQUEST_RECEIVERS:
                request_routes.add(nr)
            elif recv in _ROUTE_DEF_RECEIVERS:
                def_receiver_routes.add(nr)
        # Drop an unambiguous-request route UNLESS a genuine definition-receiver also declares it in the
        # same file (a file that both defines `app.get('/x')` and calls `this.get('/x')` keeps the def).
        defs -= (request_routes - def_receiver_routes)
    return defs


def _routes_xrepo_edges(scan, incomplete_paths_out=None):
    """Return the cross-repo shared-key EDGES for an ALREADY-computed `scan` (scan_repo output), so the
    SAME route contract can couple ACROSS two repos via the shared `route::<path>` `dst` + the relaxed
    res_adj. Edges ONLY (no nodes): an `api_*`/`route` node is DROPPED at ingest by the node_kind filter
    (db/schema/30_gate.sql), so cross-repo coupling rides the SHARED `dst` STRING in the alters/queries
    EDGES alone — exactly how the within-repo api_* substrates couple. Reuses the caller's `scan` (no
    second walk).

    ADDITIVE + flag-gated: returns [] immediately when VERIPSA_CROSS_REPO_KEYS is OFF (so the within-repo
    `imports` output is byte-identical). When ON: every route DEFINER emits `alters route::<suffix>` and
    every request ISSUER emits `queries route::<suffix>` for each mount-prefix-tolerant SUFFIX key (fix #1)
    anchored at the route's TERMINAL CONCRETE segment (fix #2), with the DISAMBIGUATED definer/consumer
    sides (fix #3), gated by the EXISTING `is_specific` floor + a clean-key check + the multi-definer
    guard (a suffix key declared by >1 file couples nothing). content-free: path strings + file paths only."""
    if not cross_repo_keys_enabled():
        return []
    # Use the DISAMBIGUATED cross-repo extraction (service-client consumer idiom + def/request split)
    # populated by scan_repo when the flag is on. Fall back to the within-repo defs/reqs if absent (e.g. a
    # caller that built `scan` directly) so this stays robust.
    defs = scan.get("xrepo_defs", scan.get("defs", {}))   # rel -> {normalized routes this file DEFINES}
    reqs = scan.get("xrepo_reqs", scan.get("reqs", {}))   # rel -> {normalized urls this file ISSUES}

    # FIX #1+#2: expand every specific, clean route into its mount-prefix-tolerant SUFFIX keys (each
    # anchored at the terminal concrete segment). A definer's suffix and a consumer's suffix COLLIDE
    # across the mount prefix; the terminal-concrete anchoring is the precision floor. We index files by
    # SUFFIX key (the cross-repo coupling anchor), not the full path.
    relevant_paths = set(defs) | set(reqs)
    suffix_loss: set[str] = set()
    def_keys: dict[str, set] = {}     # suffix key -> set of definer files
    for rel, routes in defs.items():
        rel = rel.replace(os.sep, "/")
        for r in routes:
            if not _xrepo_key_ok(r):
                continue
            for sk in _suffix_keys(r, suffix_loss, rel):
                def_keys.setdefault(sk, set()).add(rel)
    req_keys: dict[str, set] = {}     # suffix key -> set of requester files
    for rel, urls in reqs.items():
        rel = rel.replace(os.sep, "/")
        for u in urls:
            if not _xrepo_key_ok(u):
                continue
            for sk in _suffix_keys(u, suffix_loss, rel):
                req_keys.setdefault(sk, set()).add(rel)

    if suffix_loss:
        # Losing a definer suffix changes which consumers resolve to a unique route key, so the whole
        # route contract universe is Unknown rather than silently treating absent keys as Clear.
        for rel in relevant_paths:
            _mark_incomplete(incomplete_paths_out, rel)

    # Every suffix key seen on EITHER side anchors a coupling (a definer in repo A and a requester in
    # repo B each only ever see their own side — the shared suffix key is what lets them meet cross-repo).
    all_keys = set(def_keys) | set(req_keys)
    if len(all_keys) > _MAX_ROUTE_KEYS:
        for rel in relevant_paths:
            _mark_incomplete(incomplete_paths_out, rel)

    # MULTI-DEFINER guard (mirrors the within-repo matcher + api_* handling): a suffix key DEFINED
    # by >1 file is ambiguous → retain all edges with an inert status. The request side never
    # "defines", so a suffix requested by many files but defined by exactly one stays active.
    edges = []
    keyed = 0
    for sk in sorted(all_keys):           # sorted → deterministic emission order
        ambiguous = len(def_keys.get(sk, ())) > 1
        if keyed >= _MAX_ROUTE_KEYS:
            break
        keyed += 1
        key = "route::" + sk
        for f in sorted(def_keys.get(sk, ())):
            edge = {"src": f, "dst": key, "kind": "alters"}
            if ambiguous:
                edge["reference_status"] = "ambiguous"
            edges.append(edge)
        for f in sorted(req_keys.get(sk, ())):
            edge = {"src": f, "dst": key, "kind": "queries"}
            if ambiguous:
                edge["reference_status"] = "ambiguous"
            edges.append(edge)
    return edges
