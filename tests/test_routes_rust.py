"""Gate: Rust route extraction for Actix-web and Axum.

Writes tiny .rs fixtures with one Actix attribute-macro route and one Axum .route() call,
runs extract_route_defs, asserts both routes are found.
Offline -- no Postgres required.
Prints RUST-ROUTES GATE: PASS on success, RUST-ROUTES GATE: FAIL on any failure.
"""
import sys
import os
import tempfile
import pathlib

# Ensure the repo root is on the path so _cg_routes imports cleanly from the worktree.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _cg_routes import extract_route_defs, normalize_route

_ACTIX_BODY = """\
use actix_web::{get, post, HttpResponse, Responder};

#[get("/users")]
async fn list_users() -> impl Responder {
    HttpResponse::Ok().body("ok")
}

#[post("/users")]
async fn create_user() -> impl Responder {
    HttpResponse::Created().body("created")
}

#[get("/users/{id}")]
async fn get_user() -> impl Responder {
    HttpResponse::Ok().body("user")
}
"""

_AXUM_BODY = """\
use axum::{Router, routing::get};

async fn list_orders() -> &'static str { "orders" }
async fn create_order() -> &'static str { "created" }

pub fn router() -> Router {
    Router::new()
        .route("/orders", get(list_orders))
        .route("/orders/{id}", get(list_orders))
}
"""

_COMBINED_BODY = _ACTIX_BODY + "\n" + _AXUM_BODY


def _check(label, body, expected_normalized):
    routes = extract_route_defs("src/main.rs", body)
    missing = [r for r in expected_normalized if r not in routes]
    if missing:
        print(f"FAIL [{label}]: expected routes {missing!r} not found in {sorted(routes)!r}")
        return False
    return True


def main():
    failures = []

    # Actix: #[get("/users")] -> /users
    ok = _check(
        "actix get /users",
        _ACTIX_BODY,
        [normalize_route("/users")],
    )
    if not ok:
        failures.append("actix get /users")

    # Actix: #[post("/users")] -> /users (same path, different verb -- route string still extracted)
    ok = _check(
        "actix post /users",
        _ACTIX_BODY,
        [normalize_route("/users")],
    )
    if not ok:
        failures.append("actix post /users")

    # Actix: template param #[get("/users/{id}")] -> /users/{} after normalization
    ok = _check(
        "actix get /users/{id}",
        _ACTIX_BODY,
        [normalize_route("/users/{id}")],
    )
    if not ok:
        failures.append("actix get /users/{id}")

    # Axum: .route("/orders", get(handler)) -> /orders
    ok = _check(
        "axum /orders",
        _AXUM_BODY,
        [normalize_route("/orders")],
    )
    if not ok:
        failures.append("axum /orders")

    # Axum: .route("/orders/{id}", ...) -> /orders/{} after normalization
    ok = _check(
        "axum /orders/{id}",
        _AXUM_BODY,
        [normalize_route("/orders/{id}")],
    )
    if not ok:
        failures.append("axum /orders/{id}")

    # Combined: both frameworks in one file
    ok = _check(
        "combined actix+axum",
        _COMBINED_BODY,
        [normalize_route("/users"), normalize_route("/orders")],
    )
    if not ok:
        failures.append("combined actix+axum")

    # Precision: a bare string that looks like a route but is NOT an Actix attr or Axum .route()
    # should NOT be picked up.
    non_route_body = """\
fn not_a_route() {
    let s = "/users";          // bare string -- not a route
    let path = "/orders";      // assigned to variable, not #[] or .route(
}
"""
    routes = extract_route_defs("src/not_route.rs", non_route_body)
    if routes:
        print(f"FAIL [precision]: bare Rust strings should not be extracted; got {sorted(routes)!r}")
        failures.append("precision bare strings")

    if failures:
        print(f"RUST-ROUTES GATE: FAIL (failures: {failures})")
        sys.exit(1)
    else:
        print("RUST-ROUTES GATE: PASS")
        sys.exit(0)


if __name__ == "__main__":
    main()
