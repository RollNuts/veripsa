"""Background job / queue contract extraction.

AI-built full-stack apps often split async work across files without an import edge:

  - Celery worker: ``@shared_task(name="billing.close_invoice")``
  - producer: ``celery_app.send_task("billing.close_invoice")``

or in Node:

  - BullMQ producer: ``new Queue("email")``
  - BullMQ worker: ``new Worker("email", ...)``

Changing either side can break runtime behavior, but the normal call/import graph does
not see the contract. This substrate emits shared contract keys using only paths and
literal task/queue names.

This pass also appends the sibling file-stem and bounded role-feature substrates.
They live here for now so the existing code_graph_extract integration point can ship
narrow AI-era recall slices without changing the extractor's import surface again.

Precision discipline:
  - Celery task definitions require a Celery-bound decorator and a literal ``name=``.
  - Celery producers require literal ``.send_task("name")`` and are known-set gated.
  - BullMQ Queue/Worker detection requires official ``bullmq`` imports/requires.
  - Dynamic names, comments, and local classes/functions stay silent.
  - Duplicate Celery definitions plus literal producers remain as explicitly
    ambiguous evidence-only edges.

Content-free output: task/queue/stem/role-feature names, file paths, and edge kinds only.
"""
from __future__ import annotations

import os
import re
from typing import Any

from _cg_io import _mark_incomplete, _read_capped
from _cg_role_feature import (
    _FAMILY_BY_EXT as _ROLE_FAMILY_BY_EXT,
    _role_feature_graph,
)
from _cg_sibling import (
    _FAMILY_BY_EXT as _SIBLING_FAMILY_BY_EXT,
    _sibling_stem_graph,
)

_PY_EXT = ".py"
_JS_EXTS = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,120}$")

_PY_LINE_COMMENT_RE = re.compile(r"#[^\n]*")
_JS_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_JS_LINE_COMMENT_RE = re.compile(r"//[^\n]*")

_CELERY_ANCHOR_RE = re.compile(
    r"\b(?:from\s+celery\s+import|import\s+celery\b|Celery\s*\()"
)
_PY_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_CELERY_FROM_IMPORT_RE = re.compile(r"(?m)^\s*from\s+celery\s+import\s+(?P<body>[^\n]+)")
_CELERY_MODULE_IMPORT_RE = re.compile(
    rf"(?m)^\s*import\s+celery(?:\s+as\s+(?P<alias>{_PY_IDENT}))?\s*$"
)
_CELERY_APP_ASSIGN_RE = re.compile(
    rf"(?m)^\s*(?P<app>{_PY_IDENT})\s*=\s*(?P<ctor>(?:{_PY_IDENT}\.)?{_PY_IDENT})\s*\("
)
_CELERY_TASK_DECORATOR_RE = re.compile(
    rf"(?ms)^\s*@(?P<decorator>{_PY_IDENT}(?:\.{_PY_IDENT})?)\s*"
    r"\((?P<args>.*?)\)\s*"
    r"(?:\n\s*@[^\n]+)*\n\s*(?:async\s+)?def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(",
)
_CELERY_NAME_ARG_RE = re.compile(r"\bname\s*=\s*([\"'])(?P<name>[A-Za-z0-9][A-Za-z0-9_.:/-]{0,120})\1")
_CELERY_SEND_TASK_RE = re.compile(
    r"\.send_task\s*\(\s*([\"'])(?P<name>[A-Za-z0-9][A-Za-z0-9_.:/-]{0,120})\1"
)

_BULLMQ_MODULE_RE = r"bullmq"
_BULLMQ_NAMED_IMPORT_RE = re.compile(
    r"\bimport\s*\{(?P<body>[^}]+)\}\s*from\s*[\"']" + _BULLMQ_MODULE_RE + r"[\"']",
    re.MULTILINE,
)
_BULLMQ_NAMESPACE_IMPORT_RE = re.compile(
    r"\bimport\s+\*\s+as\s+(?P<alias>[A-Za-z_$][A-Za-z0-9_$]*)\s+from\s*[\"']"
    + _BULLMQ_MODULE_RE + r"[\"']",
    re.MULTILINE,
)
_BULLMQ_REQUIRE_DESTRUCTURE_RE = re.compile(
    r"\b(?:const|let|var)\s*\{(?P<body>[^}]+)\}\s*=\s*require\s*\(\s*[\"']"
    + _BULLMQ_MODULE_RE + r"[\"']\s*\)",
    re.MULTILINE,
)
_BULLMQ_REQUIRE_NAMESPACE_RE = re.compile(
    r"\b(?:const|let|var)\s+(?P<alias>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*require\s*\(\s*[\"']"
    + _BULLMQ_MODULE_RE + r"[\"']\s*\)",
    re.MULTILINE,
)


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _blank(m: re.Match) -> str:
    return "".join("\n" if c == "\n" else " " for c in m.group(0))


def _strip_py_comments(text: str) -> str:
    return _PY_LINE_COMMENT_RE.sub(_blank, text)


def _strip_js_comments(text: str) -> str:
    text = _JS_BLOCK_COMMENT_RE.sub(_blank, text)
    return _JS_LINE_COMMENT_RE.sub(_blank, text)


def _task_id(name: str) -> str:
    return f"job_task::celery::{name}"


def _queue_id(name: str) -> str:
    return f"job_queue::bullmq::{name}"


def _valid_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name))


def _parse_named_imports(body: str, wanted: set[str]) -> dict[str, set[str]]:
    """Return wanted imported symbol -> local aliases."""
    aliases: dict[str, set[str]] = {w: set() for w in wanted}
    for part in body.split(","):
        p = part.strip()
        if not p:
            continue
        m = re.match(
            r"^(?P<sym>Queue|Worker)(?:\s+as\s+|:\s*)(?P<alias>[A-Za-z_$][A-Za-z0-9_$]*)$",
            p,
        )
        if m and m.group("sym") in wanted:
            aliases[m.group("sym")].add(m.group("alias"))
            continue
        if p in wanted:
            aliases[p].add(p)
    return aliases


def _parse_py_imports(body: str, wanted: set[str]) -> dict[str, set[str]]:
    """Return wanted Python import symbols -> local aliases."""
    aliases: dict[str, set[str]] = {w: set() for w in wanted}
    body = body.strip().strip("()")
    for part in body.split(","):
        p = part.strip()
        if not p:
            continue
        m = re.match(rf"^(?P<sym>{_PY_IDENT})(?:\s+as\s+(?P<alias>{_PY_IDENT}))?$", p)
        if not m or m.group("sym") not in wanted:
            continue
        aliases[m.group("sym")].add(m.group("alias") or m.group("sym"))
    return aliases


def _py_rebound_between(text: str, name: str, start: int, end: int) -> bool:
    segment = text[start:end]
    pat = re.compile(
        rf"(?m)^\s*(?:def|async\s+def|class)\s+{re.escape(name)}\b"
        rf"|^\s*{re.escape(name)}\s*(?::[^=\n]*)?="
    )
    return bool(pat.search(segment))


def _celery_decorator_bindings(text: str) -> dict[str, int]:
    """Return decorator spellings known to come from Celery -> binding end offset."""
    decorators: dict[str, int] = {}
    constructors: dict[str, int] = {}

    for m in _CELERY_FROM_IMPORT_RE.finditer(text):
        parsed = _parse_py_imports(m.group("body"), {"Celery", "shared_task", "task"})
        for alias in parsed["shared_task"]:
            decorators.setdefault(alias, m.end())
        for alias in parsed["task"]:
            decorators.setdefault(alias, m.end())
        for alias in parsed["Celery"]:
            constructors.setdefault(alias, m.end())

    for m in _CELERY_MODULE_IMPORT_RE.finditer(text):
        alias = m.group("alias") or "celery"
        decorators.setdefault(f"{alias}.shared_task", m.end())
        decorators.setdefault(f"{alias}.task", m.end())
        constructors.setdefault(f"{alias}.Celery", m.end())

    for m in _CELERY_APP_ASSIGN_RE.finditer(text):
        ctor = m.group("ctor")
        if ctor in constructors and not _py_rebound_between(text, ctor.split(".", 1)[0], constructors[ctor], m.start()):
            decorators.setdefault(f"{m.group('app')}.task", m.end())

    return decorators


def _celery_task_defs(text: str) -> set[str]:
    """Literal named Celery task definitions in a file."""
    text = _strip_py_comments(text)
    if not _CELERY_ANCHOR_RE.search(text):
        return set()
    bindings = _celery_decorator_bindings(text)
    if not bindings:
        return set()
    out: set[str] = set()
    for m in _CELERY_TASK_DECORATOR_RE.finditer(text):
        decorator = m.group("decorator")
        bind_pos = bindings.get(decorator)
        if bind_pos is None:
            continue
        if _py_rebound_between(text, decorator.split(".", 1)[0], bind_pos, m.start()):
            continue
        nm = _CELERY_NAME_ARG_RE.search(m.group("args") or "")
        if nm and _valid_name(nm.group("name")):
            out.add(nm.group("name"))
    return out


def _celery_send_tasks(text: str, known: frozenset[str]) -> set[str]:
    """Literal Celery send_task producers, gated to locally-defined task names."""
    text = _strip_py_comments(text)
    if not known or not _CELERY_ANCHOR_RE.search(text):
        return set()
    return {
        m.group("name")
        for m in _CELERY_SEND_TASK_RE.finditer(text)
        if m.group("name") in known
    }


def _bullmq_imports(text: str) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Return BullMQ Queue/Worker/namespace aliases -> binding end offsets."""
    text = _strip_js_comments(text)
    queue_aliases: dict[str, int] = {}
    worker_aliases: dict[str, int] = {}
    namespaces: dict[str, int] = {}

    for m in list(_BULLMQ_NAMED_IMPORT_RE.finditer(text)) + list(_BULLMQ_REQUIRE_DESTRUCTURE_RE.finditer(text)):
        parsed = _parse_named_imports(m.group("body"), {"Queue", "Worker"})
        for alias in parsed["Queue"]:
            queue_aliases.setdefault(alias, m.end())
        for alias in parsed["Worker"]:
            worker_aliases.setdefault(alias, m.end())

    for m in list(_BULLMQ_NAMESPACE_IMPORT_RE.finditer(text)) + list(_BULLMQ_REQUIRE_NAMESPACE_RE.finditer(text)):
        namespaces.setdefault(m.group("alias"), m.end())

    return queue_aliases, worker_aliases, namespaces


def _js_rebound_between(text: str, name: str, start: int, end: int) -> bool:
    segment = text[start:end]
    pat = re.compile(
        rf"(?m)^\s*(?:class|function)\s+{re.escape(name)}\b"
        rf"|^\s*(?:const|let|var)\s+{re.escape(name)}\b"
        rf"|^\s*(?:const|let|var)\s*\{{[^}}\n]*\b{re.escape(name)}\b[^}}\n]*\}}\s*="
        rf"|^\s*import\s+\{{[^}}\n]*\b{re.escape(name)}\b[^}}\n]*\}}\s+from\b"
        rf"|^\s*import\s+\*\s+as\s+{re.escape(name)}\b"
        rf"|^\s*{re.escape(name)}\s*="
    )
    return bool(pat.search(segment))


def _literal_new_names(text: str, aliases: dict[str, int], namespaces: dict[str, int], member: str) -> set[str]:
    """Literal first argument to ``new Alias("name")`` / ``new ns.Member("name")``."""
    text = _strip_js_comments(text)
    found: set[str] = set()
    name = r"(?P<name>[A-Za-z0-9][A-Za-z0-9_.:/-]{0,120})"

    for alias, bind_pos in aliases.items():
        pat = re.compile(rf"(?<![A-Za-z0-9_$])new\s+{re.escape(alias)}\s*\(\s*([\"'`]){name}\1")
        for m in pat.finditer(text):
            if _js_rebound_between(text, alias, bind_pos, m.start()):
                continue
            if _valid_name(m.group("name")):
                found.add(m.group("name"))

    for ns, bind_pos in namespaces.items():
        pat = re.compile(
            rf"(?<![A-Za-z0-9_$])new\s+{re.escape(ns)}\s*\.\s*{member}\s*\(\s*([\"'`]){name}\1"
        )
        for m in pat.finditer(text):
            if _js_rebound_between(text, ns, bind_pos, m.start()):
                continue
            if _valid_name(m.group("name")):
                found.add(m.group("name"))

    return found


def _bullmq_queue_and_worker_names(text: str) -> tuple[set[str], set[str]]:
    """Return literal BullMQ producer Queue names and Worker names."""
    queue_aliases, worker_aliases, namespaces = _bullmq_imports(text)
    if not queue_aliases and not worker_aliases and not namespaces:
        return set(), set()
    queues = _literal_new_names(text, queue_aliases, namespaces, "Queue")
    workers = _literal_new_names(text, worker_aliases, namespaces, "Worker")
    return queues, workers


def _job_queue_graph(
    root: str,
    source_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> tuple[list, list]:
    """Backward-compatible diagnostics wrapper for job/derived contracts."""
    try:
        return _job_queue_graph_impl(
            root,
            source_files,
            incomplete_paths_out=incomplete_paths_out,
        )
    except Exception:
        relevant_exts = (
            {_PY_EXT}
            | set(_JS_EXTS)
            | set(_SIBLING_FAMILY_BY_EXT)
            | set(_ROLE_FAMILY_BY_EXT)
        )
        for abs_path, ext in source_files:
            if ext in relevant_exts:
                _mark_incomplete(incomplete_paths_out, _rel(root, abs_path))
        return [], []


def _job_queue_graph_impl(
    root: str,
    source_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> tuple[list, list]:
    """Return (nodes, edges) for explicit async job/queue and sibling-stem contracts."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    py_files: list[tuple[str, str]] = []
    js_files: list[tuple[str, str]] = []
    for abs_path, ext in source_files:
        rel = _rel(root, abs_path)
        if ext == _PY_EXT:
            py_files.append((abs_path, rel))
        elif ext in _JS_EXTS:
            js_files.append((abs_path, rel))

    # Celery: literal named task definitions + literal send_task producers.
    celery_definers: dict[str, set[str]] = {}
    py_texts: dict[str, str] = {}
    py_loss = set()
    for abs_path, rel in py_files:
        text = _read_capped(abs_path, py_loss, rel)
        if text is None:
            continue
        py_texts[rel] = text
        for name in _celery_task_defs(text):
            celery_definers.setdefault(name, set()).add(rel)
    if py_loss:
        for _abs_path, rel in py_files:
            _mark_incomplete(incomplete_paths_out, rel)

    single_task_definers = {
        name: next(iter(paths))
        for name, paths in celery_definers.items()
        if len(paths) == 1
    }
    ambiguous_task_definers = {
        name: paths
        for name, paths in celery_definers.items()
        if len(paths) > 1
    }
    for name, paths in sorted(celery_definers.items()):
        if len(paths) > 1:
            nodes.append({
                "id": _task_id(name),
                "kind": "job_task",
                "name": name,
                "path": sorted(paths)[0],
                "language": "celery",
                "ambiguous": True,
            })

    # Scan against every locally-known literal task name. A duplicate definition
    # makes resolution ambiguous, but must not make the producer reference vanish.
    celery_queries: set[tuple[str, str]] = set()
    if celery_definers:
        known_tasks = frozenset(celery_definers)
        for rel, text in py_texts.items():
            for name in _celery_send_tasks(text, known_tasks):
                if rel not in celery_definers[name]:
                    celery_queries.add((rel, name))

    for name, paths in sorted(ambiguous_task_definers.items()):
        for rel in sorted(paths):
            edges.append({
                "src": rel,
                "dst": _task_id(name),
                "kind": "alters",
                "reference_status": "ambiguous",
            })
        for rel, query_name in sorted(celery_queries):
            if query_name == name:
                edges.append({
                    "src": rel,
                    "dst": _task_id(name),
                    "kind": "queries",
                    "reference_status": "ambiguous",
                })

    if single_task_definers:
        single_queries = {
            (rel, name)
            for rel, name in celery_queries
            if name in single_task_definers
        }
        touched_tasks = {name for _rel, name in single_queries} | {
            name for name, rel in single_task_definers.items()
            if any(qn == name for _qr, qn in single_queries)
        }
        for name in sorted(touched_tasks):
            rel = single_task_definers[name]
            nodes.append({
                "id": _task_id(name),
                "kind": "job_task",
                "name": name,
                "path": rel,
                "language": "celery",
            })
            edges.append({"src": rel, "dst": _task_id(name), "kind": "alters"})
        for rel, name in sorted(single_queries):
            edges.append({"src": rel, "dst": _task_id(name), "kind": "queries"})

    # BullMQ: official Queue/Worker import with literal queue name.
    bull_queues_by_file: dict[str, set[str]] = {}
    bull_workers_by_file: dict[str, set[str]] = {}
    js_loss = set()
    for abs_path, rel in js_files:
        text = _read_capped(abs_path, js_loss, rel)
        if text is None:
            continue
        queues, workers = _bullmq_queue_and_worker_names(text)
        if queues:
            bull_queues_by_file[rel] = queues
        if workers:
            bull_workers_by_file[rel] = workers
    if js_loss:
        for _abs_path, rel in js_files:
            _mark_incomplete(incomplete_paths_out, rel)

    worker_names = {name for names in bull_workers_by_file.values() for name in names}
    queue_names = {name for names in bull_queues_by_file.values() for name in names}
    coupled_queue_names = worker_names & queue_names
    for name in sorted(coupled_queue_names):
        worker_paths = sorted(rel for rel, names in bull_workers_by_file.items() if name in names)
        path = worker_paths[0] if worker_paths else ""
        nodes.append({
            "id": _queue_id(name),
            "kind": "job_queue",
            "name": name,
            "path": path,
            "language": "bullmq",
        })
        for rel in worker_paths:
            edges.append({"src": rel, "dst": _queue_id(name), "kind": "alters"})
        for rel in sorted(rel for rel, names in bull_queues_by_file.items() if name in names):
            if rel not in worker_paths:
                edges.append({"src": rel, "dst": _queue_id(name), "kind": "queries"})

    sibling_nodes, sibling_edges = _sibling_stem_graph(
        root, source_files, incomplete_paths_out=incomplete_paths_out
    )
    nodes.extend(sibling_nodes)
    edges.extend(sibling_edges)

    role_nodes, role_edges = _role_feature_graph(
        root, source_files, incomplete_paths_out=incomplete_paths_out
    )
    nodes.extend(role_nodes)
    edges.extend(role_edges)

    return nodes, edges
