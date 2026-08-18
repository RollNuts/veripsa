"""Gate: JavaScript/CommonJS prototype-assignment definitions produce def nodes.

Pattern: `app.set = function(s,v){}` / `proto.route = function route(p){}`
These are assignment_expression nodes whose LHS is a member_expression and RHS
is a function_expression or arrow_function. Before the fix, extract_file_ts()
did not handle this shape -> no def node -> finer-collision degraded to
file-level for Express/Connect-style libraries.

This test runs offline (no Postgres, no git). It writes a tiny .js fixture in a
tempdir, runs build_graph, and asserts:
  - def named 'set' exists (function_expression with named function)
  - def named 'route' exists (function_expression with named function)
  - def named 'use' exists (anonymous function_expression)
  - def named 'get' exists (arrow_function RHS)
  - the callback ([].forEach(x => x)) did NOT produce a def
  - a non-function member assignment (obj.flag = true) did NOT produce a def

Prints JS-PROTOTYPE GATE: PASS or JS-PROTOTYPE GATE: FAIL.
"""

import sys
import os
import tempfile
import pathlib

def run():
    # Fixture: prototype-style assignments that SHOULD produce defs
    # plus two shapes that MUST NOT.
    # NOTE: semicolons are used throughout (as Express/CommonJS do in practice).
    # Without them, `function(){}\n[].forEach(...)` is parsed as a subscript
    # expression (JS ASI does not insert `;` before `[`), which changes the AST
    # shape and causes spurious parse errors. Real library code uses semicolons.
    fixture_js = """\
// Named function_expression with named function (Express app.* style)
app.set = function set(setting, val) { return this; };

// Named function_expression with anonymous function
app.use = function(fn) { return this; };

// Named function_expression with named function (prototype style)
proto.route = function route(path) { return this._router; };

// Arrow function assigned to member (should also produce a def)
proto.get = (path, fn) => { return this; };

// Chained alias: outer LHS gets RHS = assignment_expression (not a function)
// -> outer (req.header alias) does NOT match; inner (req.is) does.
req.header =
req.is = function is(types) { return types; };

// MUST NOT produce a def: inline callback passed to a call
[1, 2].forEach(function(item) { return item; });
[3, 4].map(function(x) { return x * 2; });

// MUST NOT produce a def: non-function assignment
obj.flag = true;
obj.name = 'express';
"""

    results = {"pass": [], "fail": []}

    def check(cond, desc):
        if cond:
            results["pass"].append(desc)
        else:
            results["fail"].append(desc)

    with tempfile.TemporaryDirectory() as td:
        fpath = os.path.join(td, "lib.js")
        with open(fpath, "w") as f:
            f.write(fixture_js)

        # build_graph lives one directory up from tests/
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import code_graph_extract as X

        g = X.build_graph(td)

        def_names = {n["name"] for n in g["nodes"] if n.get("kind") == "def"}
        file_nodes = [n for n in g["nodes"] if n.get("kind") == "file"]

        # --- defs that MUST exist (prototype-style assignments) ---
        check("set" in def_names,
              "def 'set' exists (app.set = function set(s,v){})")
        check("use" in def_names,
              "def 'use' exists (app.use = function(fn){})")
        check("route" in def_names,
              "def 'route' exists (proto.route = function route(p){})")
        check("get" in def_names,
              "def 'get' exists (proto.get = (path,fn) => {})")
        check("is" in def_names,
              "def 'is' exists (chained alias: req.is = function is(){})")

        # --- defs that MUST NOT exist (callbacks, non-function assignments) ---
        # process() and doStuff() are called inside callbacks — they must NOT become
        # defs (they appear as member_expression calls, not assignment targets).
        check("process" not in def_names,
              "def 'process' NOT minted (inline callback body method call)")
        # Non-function assignments: obj.flag = true, obj.name = 'express'
        check("flag" not in def_names,
              "def 'flag' NOT minted (non-function assignment obj.flag = true)")
        check("name" not in def_names,
              "def 'name' NOT minted (non-function assignment obj.name = ...)")

        # --- span sanity: every new prototype def should have start_line ---
        proto_defs = [n for n in g["nodes"]
                      if n.get("kind") == "def" and n.get("name") in ("set", "use", "route", "get", "is")]
        check(all("start_line" in n for n in proto_defs),
              "all prototype defs carry start_line (content-free line span)")

        # --- file node sanity ---
        check(len(file_nodes) == 1,
              f"exactly 1 file node for the fixture (got {len(file_nodes)})")

    print()
    if results["fail"]:
        print("FAILURES:")
        for f in results["fail"]:
            print(f"  FAIL: {f}")
        print()
        print("PASSES:")
    for p in results["pass"]:
        print(f"  PASS: {p}")

    if results["fail"]:
        print()
        print("JS-PROTOTYPE GATE: FAIL")
        sys.exit(1)
    else:
        print()
        print("JS-PROTOTYPE GATE: PASS")


if __name__ == "__main__":
    run()
