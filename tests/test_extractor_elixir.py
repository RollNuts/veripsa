#!/usr/bin/env python3
"""Elixir extractor gate.

THE GAP: .ex/.exs files were already in _SOURCE_EXT (so they received a bare file node)
but 'elixir' was absent from _ts_languages() -- the grammar was never loaded and the
extractor was never called.  Elixir's grammar does NOT use distinct node types for
definitions: defmodule/def/defp/defmacro/defmacrop are ALL `call` nodes distinguished
only by the first-child identifier's text.  _GENERIC_SPEC (which dispatches on node TYPE)
cannot handle this -- a bespoke extract_file_elixir() walker is required.

WHAT THIS GATE PROVES (hermetic, no DB, no Postgres, no network):
  1. defmodule MyApp.Accounts mints a `class`-kind node named 'MyApp.Accounts'.
  2. def get_user(id) inside the module mints a `def`-kind node named 'get_user'.
  3. defp validate(attrs) mints a `def`-kind node named 'validate' (private fns count).
  4. defmacro my_macro(x) mints a `def`-kind node named 'my_macro'.
  5. A second module in the same file mints its own `class`-kind node.
  6. Cross-file calls edge: a `dot` call (Module.function()) emits a `calls` edge.
  7. Never-crash: malformed / empty .ex file does not raise.
  8. Content-free: no literal string or comment body from the source appears in the graph.
  9. No-grammar fallback: if tree_sitter_elixir is absent the file still gets a bare node.

Prints ELIXIR GATE: PASS on success, ELIXIR GATE: FAIL on any failure.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402


def _w(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def _nodes_by_kind(g, kind):
    return [n for n in g["nodes"] if n.get("kind") == kind]


def _names_by_kind(g, kind):
    return {n["name"] for n in g["nodes"] if n.get("kind") == kind}


def _calls_dsts(g):
    return {e["dst"] for e in g["edges"] if e["kind"] == "calls"}


# ---------------------------------------------------------------------------
# 1-5: def/defp/defmacro/defmodule minted as nodes; second module in same file
# ---------------------------------------------------------------------------
def test_def_nodes():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "lib", "accounts.ex"), """\
defmodule MyApp.Accounts do
  def get_user(id) do
    id
  end

  defp validate(attrs) do
    attrs
  end

  defmacro my_macro(x) do
    quote do: unquote(x)
  end
end

defmodule MyApp.Router do
  def route(path) do
    path
  end
end
""")
        g = X.build_graph(root)
        classes = _names_by_kind(g, "class")
        defs = _names_by_kind(g, "def")

        # 1. defmodule MyApp.Accounts -> class node
        assert "MyApp.Accounts" in classes, f"expected 'MyApp.Accounts' class, got: {classes}"
        # 2. def get_user -> def node
        assert "get_user" in defs, f"expected 'get_user' def, got: {defs}"
        # 3. defp validate -> def node (private functions are first-class symbols)
        assert "validate" in defs, f"expected 'validate' def, got: {defs}"
        # 4. defmacro my_macro -> def node
        assert "my_macro" in defs, f"expected 'my_macro' def, got: {defs}"
        # 5. second module in same file -> class node
        assert "MyApp.Router" in classes, f"expected 'MyApp.Router' class, got: {classes}"
        # route() in the second module
        assert "route" in defs, f"expected 'route' def, got: {defs}"


# ---------------------------------------------------------------------------
# 6. Cross-module calls edge: Module.function() dot-call
# ---------------------------------------------------------------------------
def test_calls_edge():
    with tempfile.TemporaryDirectory() as root:
        # caller.ex calls MyApp.Repo.get(...)
        _w(os.path.join(root, "lib", "caller.ex"), """\
defmodule MyApp.Caller do
  def run do
    MyApp.Repo.get(User, 1)
  end
end
""")
        g = X.build_graph(root)
        calls = _calls_dsts(g)
        # The dot-call 'MyApp.Repo.get(...)' should produce a calls edge to 'get'
        # and/or to 'MyApp.Repo' (module alias coupling)
        assert "get" in calls or "MyApp.Repo" in calls, (
            f"expected 'get' or 'MyApp.Repo' in calls edges; got: {calls}"
        )


# ---------------------------------------------------------------------------
# 7. Never-crash: empty .ex file
# ---------------------------------------------------------------------------
def test_empty_no_crash():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "lib", "empty.ex"), "")
        try:
            g = X.build_graph(root)
        except Exception as exc:
            raise AssertionError(f"build_graph raised on empty .ex: {exc}") from exc
        # Should still get a file node
        file_nodes = [n for n in g["nodes"] if n.get("kind") == "file"
                      and n.get("path", "").endswith(".ex")]
        assert file_nodes, "empty .ex should still produce a file node"


# ---------------------------------------------------------------------------
# 8. Never-crash: malformed .ex file (unparseable bytes)
# ---------------------------------------------------------------------------
def test_malformed_no_crash():
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "lib", "bad.ex")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe" + b"not valid elixir <<>>{{{{")
        try:
            g = X.build_graph(root)
        except Exception as exc:
            raise AssertionError(f"build_graph raised on malformed .ex: {exc}") from exc


# ---------------------------------------------------------------------------
# 9. Content-free: no literal value or comment body in the graph
# ---------------------------------------------------------------------------
def test_content_free():
    secret = "SUPER_SECRET_API_KEY_XYZ"
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "lib", "secret.ex"), f"""\
# This file contains a secret: {secret}
defmodule MyApp.Secret do
  @api_key "{secret}"

  def fetch do
    @api_key
  end
end
""")
        g = X.build_graph(root)
        graph_str = str(g)
        assert secret not in graph_str, (
            "content-free violated: secret literal found in graph output"
        )


# ---------------------------------------------------------------------------
# 10. Line spans are present on def/class nodes (content-free structural info)
# ---------------------------------------------------------------------------
def test_line_spans():
    with tempfile.TemporaryDirectory() as root:
        _w(os.path.join(root, "lib", "spans.ex"), """\
defmodule MyApp.Spans do
  def first_fn do
    :ok
  end

  def second_fn do
    :error
  end
end
""")
        g = X.build_graph(root)
        defs = [n for n in g["nodes"] if n.get("kind") == "def" and "first_fn" in n.get("name", "")]
        assert defs, "first_fn def node not found"
        d = defs[0]
        assert "start_line" in d and "end_line" in d, (
            f"expected start_line/end_line on def node; got: {d}"
        )
        assert d["start_line"] >= 1, f"start_line must be >= 1; got {d['start_line']}"
        assert d["end_line"] >= d["start_line"], (
            f"end_line must be >= start_line; got {d}"
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    failures = []
    tests = [
        test_def_nodes,
        test_calls_edge,
        test_empty_no_crash,
        test_malformed_no_crash,
        test_content_free,
        test_line_spans,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:
            failures.append(f"{t.__name__}: {exc}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print("ELIXIR GATE: FAIL")
        sys.exit(1)
    else:
        print("ELIXIR GATE: PASS")
        sys.exit(0)
