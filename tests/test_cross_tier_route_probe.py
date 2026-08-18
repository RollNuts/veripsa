#!/usr/bin/env python3
"""CROSS-TIER ROUTE↔CALL probe gate (hermetic, deterministic, content-free).

This pins the PROBE's contract on KNOWN-TRUTH constructed fixtures:

  1) EXTRACTION — backend route-definition strings (Flask/FastAPI/Express/Go/Rails/SvelteKit/Next
     file-based) and frontend request URLs (fetch/axios/$fetch/useSWR) are captured as PATH STRINGS.
  2) MATCHING — a backend route and a frontend url for the SAME contract match (exact, mount-prefix
     suffix, and path-template aware `/users/{id}` ~ `/users/${x}` ~ `/users/:id`).
  3) PRECISION-SAFETY (the cardinal rule) — a UBIQUITOUS / short route (`/`, `/api`, `/health`,
     `/login`) with the specificity floor ON must NOT couple unrelated files; only SPECIFIC routes
     (≥2 segments OR a templated param) anchor a coupling. The same bare route WITHOUT the floor
     over-couples — the test proves the floor is what removes the false fan-out.
  4) CONTENT-FREE — the probe only ever returns route/url path strings + file paths.

Hermetic: writes tiny source files to a temp dir, scans them with the probe. No network, no DB,
no checkout. Deterministic PASS/FAIL marker for the release gate.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import cross_tier_route_probe as P  # noqa: E402


def _write(d, rel, body):
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(body)


def main() -> int:
    checks = []

    def chk(name, cond):
        checks.append((name, bool(cond)))

    # ---- unit: normalization collapses every template syntax to the same canonical form --------
    forms = ["/users/{id}", "/users/${x}", "/users/:id", "/users/<int:id>", "/users/[id]"]
    norms = {P.normalize_route(f) for f in forms}
    chk(f"normalize collapses all param syntaxes to one canonical route (got {sorted(norms)})",
        len(norms) == 1 and norms.pop() == "/users/{}")

    # ---- unit: specificity floor — ubiquitous/short routes are NOT specific -------------------
    chk("specificity floor: '/' '/api' '/health' '/login' are NOT specific (won't anchor)",
        not any(P.is_specific(P.normalize_route(r)) for r in ["/", "/api", "/health", "/login", "/me"]))
    chk("specificity floor: '/api/orders/{id}' and '/projects/settings' ARE specific (anchor OK)",
        P.is_specific(P.normalize_route("/api/orders/{id}")) and P.is_specific(P.normalize_route("/projects/settings")))
    # MEASURED precision fix: LEADING-PARAM routes (`/{id}/config`, `/{plugin}/execute`) match any
    # resource -> they must NOT anchor (this is what drove Zen-Ai-Pentest's matched co-change below
    # random until the floor required a concrete first segment).
    chk("specificity floor: LEADING-PARAM routes '/{}/config' '/{}/execute' '/{}/auth/refresh' are NOT specific",
        not any(P.is_specific(P.normalize_route(r)) for r in ["/:id/config", "/${plugin}/execute", "/{x}/auth/refresh"]))

    # ============================================================================================
    # SCENARIO A — TRUE cross-tier contracts (must EMIT), each on a different backend framework.
    # ============================================================================================
    with tempfile.TemporaryDirectory() as d:
        # FastAPI backend
        _write(d, "backend/api/orders.py",
               '''from fastapi import APIRouter\nrouter = APIRouter()\n\n'''
               '''@router.get("/api/orders/{order_id}")\ndef get_order(order_id): ...\n'''
               '''@router.post("/api/orders")\ndef create_order(): ...\n''')
        # Express backend (TS)
        _write(d, "server/routes/projects.ts",
               '''import { Router } from "express"\nconst r = Router()\n'''
               '''r.get("/api/projects/:id/settings", (req,res)=>{})\n'''
               '''r.delete("/api/projects/:id", (req,res)=>{})\n''')
        # Go backend
        _write(d, "internal/handlers/widgets.go",
               '''package handlers\nfunc Register(){ http.HandleFunc("/api/widgets/list", listWidgets) }\n''')
        # SvelteKit file-based route
        _write(d, "src/routes/api/reports/+server.ts", '''export function GET(){ return new Response() }\n''')

        # Frontend clients that hit each contract
        _write(d, "web/src/orders.tsx",
               '''export async function loadOrder(id){\n'''
               '''  return fetch(`/api/orders/${id}`).then(r=>r.json())\n}\n'''
               '''export async function newOrder(){ return fetch("/api/orders", {method:"POST"}) }\n''')
        _write(d, "web/src/projects.ts",
               '''import axios from "axios"\n'''
               '''export const getSettings = (id) => axios.get(`/api/projects/${id}/settings`)\n''')
        _write(d, "web/src/widgets.svelte",
               '''<script>\n  const data = await fetch("/api/widgets/list").then(r=>r.json())\n</script>\n''')
        _write(d, "web/src/reports.ts",
               '''export const reports = () => $fetch("/api/reports")\n''')

        scan = P.scan_repo(d, specificity_floor=True)
        pairs = scan["pairs"]
        pf = {frozenset((a, b)): v for a, b in [tuple(k) for k in pairs] for v in [pairs[frozenset((a, b))]]}

        def coupled(a, b):
            return frozenset((a, b)) in pairs

        chk(f"A1: FastAPI orders.py ↔ orders.tsx coupled via /api/orders/* (template + exact). "
            f"shared={pairs.get(frozenset(('backend/api/orders.py','web/src/orders.tsx')))}",
            coupled("backend/api/orders.py", "web/src/orders.tsx"))
        chk("A2: Express projects.ts ↔ projects.ts(web) coupled via /api/projects/{}/settings",
            coupled("server/routes/projects.ts", "web/src/projects.ts"))
        chk("A3: Go widgets.go ↔ widgets.svelte coupled via /api/widgets/list (cross-language .go↔.svelte)",
            coupled("internal/handlers/widgets.go", "web/src/widgets.svelte"))
        chk("A4: SvelteKit file-based /api/reports ↔ reports.ts coupled (route derived from FILE PATH)",
            coupled("src/routes/api/reports/+server.ts", "web/src/reports.ts"))
        # content-free: every shared token is a path string, never a body
        all_shared = [s for v in pairs.values() for s in v]
        chk(f"A5: every emitted coupling token is a route/url path string (content-free). tokens={all_shared}",
            all(isinstance(s, str) and s.startswith("/") for s in all_shared))

    # ============================================================================================
    # SCENARIO B — PRECISION: a UBIQUITOUS bare route must NOT couple unrelated files (floor ON),
    # and the SAME fixture WITHOUT the floor over-couples — proving the floor removes false fan-out.
    # ============================================================================================
    with tempfile.TemporaryDirectory() as d:
        # Two unrelated backends each define ONLY a bare ubiquitous route
        _write(d, "svc_a/health.py", '''@app.route("/health")\ndef h(): return "ok"\n''')
        _write(d, "svc_b/auth.py", '''@app.route("/login")\ndef login(): ...\n''')
        # Two unrelated frontends each hit a bare ubiquitous route
        _write(d, "ui/a.ts", '''export const ping = () => fetch("/health")\n''')
        _write(d, "ui/b.ts", '''export const signin = () => fetch("/login")\n''')
        # A real specific contract that SHOULD survive the floor
        _write(d, "svc_a/orders.py", '''@app.route("/api/orders/{oid}/items")\ndef items(oid): ...\n''')
        _write(d, "ui/orders.ts", '''export const items = (id) => fetch(`/api/orders/${id}/items`)\n''')

        floor_on = P.scan_repo(d, specificity_floor=True)["pairs"]
        floor_off = P.scan_repo(d, specificity_floor=False)["pairs"]

        def has(pairs, a, b):
            return frozenset((a, b)) in pairs

        chk("B1: FLOOR ON — /health bare route does NOT couple health.py↔a.ts (ubiquitous dropped)",
            not has(floor_on, "svc_a/health.py", "ui/a.ts"))
        chk("B2: FLOOR ON — /login bare route does NOT couple auth.py↔b.ts (ubiquitous dropped)",
            not has(floor_on, "svc_b/auth.py", "ui/b.ts"))
        chk("B3: FLOOR ON — the SPECIFIC /api/orders/{}/items contract IS kept (recall preserved)",
            has(floor_on, "svc_a/orders.py", "ui/orders.ts"))
        chk(f"B4: FLOOR OFF over-couples on bare routes ({len(floor_off)} pairs) vs FLOOR ON "
            f"({len(floor_on)} pair) — the floor is what removes the false fan-out",
            len(floor_off) > len(floor_on) and len(floor_on) == 1)

    # ---- unit: WILDCARD-COLLISION precision (measured on vstorm) ------------------------------
    # `/plans/{}` (backend) must NOT match `/api/orgs/{}/avatar` (frontend) just because two `{}`
    # wildcards line up in a suffix — a match needs a shared CONCRETE segment anchor.
    chk("wildcard-collision: '/plans/{}' does NOT match '/api/orgs/{}/avatar' (no concrete anchor)",
        not P.match_routes(P.normalize_route("/plans/{id}"), P.normalize_route("/api/orgs/${o}/avatar")))
    chk("template recall kept: '/users/{}' matches '/users/5' and '/api/orders/{}' ~ '/api/orders/9'",
        P.match_routes(P.normalize_route("/users/:id"), P.normalize_route("/users/5"))
        and P.match_routes(P.normalize_route("/api/orders/{id}"), P.normalize_route("/api/orders/9")))

    # ============================================================================================
    # SCENARIO C — external URLs and self-pairs must NOT be emitted (boundary correctness).
    # ============================================================================================
    with tempfile.TemporaryDirectory() as d:
        _write(d, "be/x.py", '''@app.route("/api/things/{id}")\ndef t(id): ...\n''')
        # frontend calls an EXTERNAL service on the same-looking path — not THIS backend's contract
        _write(d, "fe/x.ts", '''fetch("https://other.example.com/api/things/5")\n'''
                             '''fetch("/api/things/9")\n''')
        scan = P.scan_repo(d, specificity_floor=True)
        urls = scan["reqs"].get("fe/x.ts", set())
        chk(f"C1: external https:// URL is NOT extracted; the relative concrete url IS (urls={sorted(urls)})",
            "/api/things/9" in urls and not any("other.example.com" in u for u in urls)
            and not any(u.startswith("/api/things/9".replace("9", "other")) for u in urls))
        chk("C2: the relative /api/things/{} call DOES couple be/x.py↔fe/x.ts",
            frozenset(("be/x.py", "fe/x.ts")) in scan["pairs"])

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond
    print("CROSS-TIER ROUTE PROBE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
