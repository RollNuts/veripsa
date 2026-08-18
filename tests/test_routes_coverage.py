"""Gate: cross-tier ROUTE-DEFINITION framework COVERAGE (recall) — NestJS / ASP.NET / Ktor /
Phoenix / Hapi / FastAPI-APIRouter-prefix nesting.

The cross-tier route detector (_cg_routes) couples a backend file that DEFINES a route to a frontend
file that ISSUES a request to it. MEASURE-FIRST on crafted temp repos (PR fix/routes-coverage) showed
several COMMON full-stack frameworks were SILENTLY MISSED — the backend defined a route and a frontend
fetched it, but NO couple emerged because the route-def wasn't extracted (route_defs came back empty, or
only the per-method `/{}` which is ubiquitous and dropped):

  * NestJS         — `@Get(':id')` method decorators are RELATIVE to `@Controller('orders')`; without
                     joining the prefix the method route normalizes to `/{}` (ubiquitous → dropped).
  * ASP.NET Core   — `[HttpGet("{id}")]` is RELATIVE to the controller `[Route("api/orders")]`; the
                     minimal-API `app.MapGet("/api/x", ...)` was not matched at all (.cs had no branch).
  * Ktor (Kotlin)  — the `get("/x")` routing DSL was not extracted (.kt only matched Spring annotations).
  * Phoenix (Elixir) — `get "/x", Controller, :action` relative to `scope "/api" do`; `.ex/.exs` were
                     not even in BACKEND_EXT so the files were never walked.
  * Hapi (JS/TS)   — `server.route({ path: '/x' })` was not matched (only Express `app.get(...)` was).
  * FastAPI nesting — `@router.get("/{id}")` under `APIRouter(prefix="/api/items")` normalized to `/{}`;
                     the `_PY_PREFIX` regex existed but was NEVER wired into extract_route_defs.

This gate measures the fix on crafted repos (offline, no Postgres):
  (1) RECALL  — for each framework above, the backend file couples to its frontend caller.
  (2) PRECISION FLOOR HELD — a ubiquitous route (`/health`) defined via the SAME new frameworks must
                still NOT couple (the specificity floor + concrete-anchor matcher are untouched).
  (3) MULTI-DEFINER STILL SUPPRESSED — the same route DEFINED by >1 backend file (here via the new
                ASP.NET prefix-join path) is ambiguous → couples to NEITHER (the precision guard the
                prefix-join must not be able to bypass).
  (4) PREFIX-JOIN PRECISION — a bare `path:` in a build/webpack config object (NOT a Hapi `.route(...)`)
                is NOT swept as a route; member calls like `settings.get("...")` are NOT Ktor routes.

RECALL-SAFE: the prefix×route expansion is recall-biased — it emits the method routes AS-IS plus each
prefix-join, so an already-absolute path AND a controller-relative path both couple. Over-generation is
bounded by |prefixes|×|routes| per file and gated by the unchanged specificity floor + single-definer
suppression, so a fabricated join can only FAIL to match — never false-couple.

Content-free throughout: only route PATH strings + file paths are read.
Prints ROUTES COVERAGE GATE: PASS on success, ... FAIL on any failure.  Offline.
"""
import sys
import os
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import _cg_routes as R


def _write(root, path, body):
    fp = os.path.join(root, path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w") as fh:
        fh.write(body)


def _coupled_to(scan, f):
    """The set of files coupled to `f` (repo-relative, forward-slash)."""
    f = f.replace(os.sep, "/")
    out = set()
    for pair in scan["pairs"]:
        ps = {x.replace(os.sep, "/") for x in pair}
        if f in ps:
            out |= (ps - {f})
    return out


# ---- framework fixtures (backend defines a SPECIFIC route; frontend fetches it) ---------------
_NEST = ('import {Controller, Get, Post} from "@nestjs/common";\n'
         '@Controller("orders")\n'
         'export class OrdersController {\n'
         '  @Get(":id")\n  findOne() {}\n'
         '  @Post()\n  create() {}\n'
         '}\n')

_ASPNET_ATTR = ('using Microsoft.AspNetCore.Mvc;\n'
                '[ApiController]\n[Route("api/orders")]\n'
                'public class OrdersController : ControllerBase {\n'
                '  [HttpGet("{id}")]\n  public IActionResult Get(int id) => Ok();\n'
                '}\n')

_ASPNET_MINIMAL = ('var app = WebApplication.Create();\n'
                   'app.MapGet("/api/products/{id}", (int id) => Results.Ok());\n'
                   'app.MapPost("/api/products", () => Results.Created());\n'
                   'app.Run();\n')

_KTOR = ('import io.ktor.server.routing.*\n'
         'fun Route.sessionRoutes() {\n'
         '  get("/api/sessions/{id}") { }\n'
         '  post("/api/sessions") { }\n'
         '}\n')

_PHOENIX = ('defmodule AppWeb.Router do\n'
            '  use AppWeb, :router\n'
            '  scope "/api" do\n'
            '    get "/widgets/:id", WidgetController, :show\n'
            '  end\n'
            'end\n')

_HAPI = ('const server = Hapi.server({port:3000});\n'
         'server.route({ method: "GET", path: "/api/tasks/{id}", handler: () => {} });\n')

_FASTAPI_PREFIX = ('from fastapi import APIRouter\n'
                   'router = APIRouter(prefix="/api/items")\n'
                   '@router.get("/{id}")\n'
                   'def get_item(id: int): ...\n')


def main():
    failures = []
    root = tempfile.mkdtemp(prefix="routes_coverage_")
    try:
        # (1) RECALL — one crafted backend+frontend per framework, distinct resource each (single-definer).
        cases = [
            ("nestjs", "nest/orders.controller.ts", _NEST, "web/orders.ts", "/orders/5"),
            ("aspnet-attr", "aspnet/OrdersController.cs", _ASPNET_ATTR, "web/aspnet_orders.ts", "/api/orders/5"),
            ("aspnet-minimal", "aspnet/Program.cs", _ASPNET_MINIMAL, "web/products.ts", "/api/products/7"),
            ("ktor", "ktor/Routes.kt", _KTOR, "web/sessions.ts", "/api/sessions/3"),
            ("phoenix", "phx/router.ex", _PHOENIX, "web/widgets.ts", "/widgets/4"),
            ("hapi", "hapi/server.js", _HAPI, "web/tasks.ts", "/api/tasks/8"),
            ("fastapi-prefix", "api/routers/items.py", _FASTAPI_PREFIX, "web/items.ts", "/api/items/5"),
        ]
        for _label, bpath, bbody, fpath, furl in cases:
            _write(root, bpath, bbody)
            _write(root, fpath, f'export const f = () => fetch("{furl}")\n')

        scan = R.scan_repo(root, specificity_floor=True)

        for label, bpath, _b, fpath, _u in cases:
            coupled = _coupled_to(scan, fpath)
            want = bpath.replace(os.sep, "/")
            if want not in coupled:
                print(f"FAIL [1 recall {label}]: {fpath} must couple to {want!r}; got {sorted(coupled)!r} "
                      f"(route_defs={sorted(scan['defs'].get(want, set()))!r})")
                failures.append(f"recall-{label}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # (2) PRECISION FLOOR HELD — ubiquitous /health via the new frameworks must NOT couple.
    root = tempfile.mkdtemp(prefix="routes_coverage_floor_")
    try:
        _write(root, "nest/health.controller.ts",
               'import {Controller, Get} from "@nestjs/common";\n'
               '@Controller()\nexport class HealthController {\n  @Get("health")\n  c() {}\n}\n')
        _write(root, "aspnet/Program.cs", 'var app = WebApplication.Create();\napp.MapGet("/health", () => "ok");\n')
        _write(root, "ktor/H.kt", 'fun Route.h() {\n  get("/health") { }\n}\n')
        _write(root, "web/health.ts", 'export const f = () => fetch("/health")\n')
        scan = R.scan_repo(root, specificity_floor=True)
        c = _coupled_to(scan, "web/health.ts")
        if c:
            print(f"FAIL [2 floor]: ubiquitous /health must NOT couple; web/health.ts coupled to {sorted(c)!r}")
            failures.append("ubiquitous-coupled")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # (3) MULTI-DEFINER STILL SUPPRESSED — same route from 2 ASP.NET controllers (prefix-join path) →
    #     the caller couples to NEITHER. A single-definer control still couples (recall preserved).
    root = tempfile.mkdtemp(prefix="routes_coverage_multi_")
    try:
        _write(root, "svcA/OrdersController.cs",
               '[Route("api/orders")]\npublic class A {\n [HttpGet("{id}")]\n public X G()=>null;\n}\n')
        _write(root, "svcB/OrdersController.cs",
               '[Route("api/orders")]\npublic class B {\n [HttpGet("{id}")]\n public X G()=>null;\n}\n')
        _write(root, "web/multi_orders.ts", 'export const f=()=>fetch("/api/orders/5")\n')
        # single-definer control
        _write(root, "svcC/InvoicesController.cs",
               '[Route("api/invoices")]\npublic class C {\n [HttpGet("{id}")]\n public X G()=>null;\n}\n')
        _write(root, "web/invoices.ts", 'export const g=()=>fetch("/api/invoices/9")\n')
        scan = R.scan_repo(root, specificity_floor=True)
        cm = _coupled_to(scan, "web/multi_orders.ts")
        if cm:
            print(f"FAIL [3 multi-definer]: prefix-joined multi-definer must suppress; coupled to {sorted(cm)!r}")
            failures.append("multidefiner-not-suppressed")
        cs = _coupled_to(scan, "web/invoices.ts")
        if cs != {"svcC/InvoicesController.cs"}:
            print(f"FAIL [3 single recall]: single-definer must couple to svcC; got {sorted(cs)!r}")
            failures.append("single-definer-recall")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # (4) PREFIX-JOIN PRECISION — a bare config `path:` (not a Hapi `.route(...)`) is NOT a route, and a
    #     member `settings.get("...")` is NOT a Ktor route.
    root = tempfile.mkdtemp(prefix="routes_coverage_noise_")
    try:
        _write(root, "build/webpack.config.js",
               'module.exports = { output: { path: "/dist/static/app/main" } };\n')
        _write(root, "ktor/Cfg.kt",
               'class Cfg {\n  fun load() {\n    val v = settings.get("/dist/static/app/main")\n  }\n}\n')
        _write(root, "web/asset.ts", 'export const f=()=>fetch("/dist/static/app/main")\n')
        scan = R.scan_repo(root, specificity_floor=True)
        cn = _coupled_to(scan, "web/asset.ts")
        if cn:
            print(f"FAIL [4 prefix-join precision]: bare config path:/settings.get must not be a route; "
                  f"web/asset.ts coupled to {sorted(cn)!r}")
            failures.append("config-path-swept")

        # Direct unit check: _routes_with_prefixes joins AND keeps bare routes; the floor drops /{}.
        joined = R._routes_with_prefixes({"/{id}"}, {"/api/items"})
        norm = {R.normalize_route(x) for x in joined}
        if "/api/items/{}" not in norm:
            print(f"FAIL [4 unit join]: prefix-join must yield /api/items/{{}}; got {sorted(norm)!r}")
            failures.append("unit-prefix-join")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if failures:
        print(f"ROUTES COVERAGE GATE: FAIL (failures: {failures})")
        sys.exit(1)
    print("ROUTES COVERAGE GATE: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
