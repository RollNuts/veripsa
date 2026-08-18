#!/usr/bin/env python3
"""Killable, content-free graph-extraction child.

The live webhook worker never imports repository bodies into a second worker
thread.  Instead it starts this fresh interpreter for exactly one graph build
and waits for it.  The parent owns the wall-clock timeout and database write;
this child owns only attacker-controlled archive/filesystem work and writes a
content-free graph to a private temporary file.

Protocol:

    graph_extract_worker.py REQUEST_JSON GRAPH_JSON META_JSON

``META_JSON`` is deliberately small and is written for every handled outcome.
An unexpected extractor bug exits non-zero; resource ceilings return a
successful ``resource_limited`` outcome so the parent can store an honest empty
graph once, without feeding a deterministic poison delivery into retry loops.
"""
from __future__ import annotations

import json
import os
import site
import sys
import tarfile


APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP_DIR)
# ``-I`` intentionally omits the script/repository directories.  Add only the
# two code roots this owned helper needs; never honor caller-controlled
# PYTHONPATH.
sys.path.insert(0, ROOT)
sys.path.insert(0, APP_DIR)
# ``-I`` deliberately disables the caller's PYTHONPATH and current-directory
# imports, but it also drops the interpreter's per-user site-packages.  Local
# installs (and supported non-root/self-hosted installs) may place the pinned
# parser wheels there, which previously made the isolated worker silently lose
# every tree-sitter substrate and persist ``analysis_status=incomplete``.  Add
# only Python's own canonical user-site directory; repository-controlled paths
# and environment-provided paths remain excluded.
USER_SITE = site.getusersitepackages()
if isinstance(USER_SITE, str) and os.path.isdir(USER_SITE):
    sys.path.append(USER_SITE)


class _BoundedUtf8Writer:
    """A binary writer accepted by ``json.dump`` that enforces encoded bytes."""

    def __init__(self, fh, limit: int, resource_error):
        self._fh = fh
        self._limit = int(limit)
        self._written = 0
        self._resource_error = resource_error

    def write(self, value: str) -> int:
        data = value.encode("utf-8")
        if self._written + len(data) > self._limit:
            raise self._resource_error("graph_output_bytes_cap")
        self._fh.write(data)
        self._written += len(data)
        return len(value)

    def flush(self) -> None:
        self._fh.flush()


def _atomic_json(path: str, value: dict) -> None:
    partial = path + ".partial"
    try:
        with open(partial, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=True, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(partial, path)
    finally:
        try:
            os.unlink(partial)
        except FileNotFoundError:
            pass


def _write_graph(path: str, graph: dict, limit: int, resource_error) -> int:
    """Write only the DB graph contract, atomically and under ``limit`` bytes."""

    partial = path + ".partial"
    try:
        with open(partial, "xb") as raw:
            bounded = _BoundedUtf8Writer(raw, limit, resource_error)
            # ``root`` and extractor counters are process-local diagnostics.
            # The database contract needs only content-free nodes + edges.
            json.dump(
                {
                    "nodes": graph.get("nodes") if isinstance(graph.get("nodes"), list) else [],
                    "edges": graph.get("edges") if isinstance(graph.get("edges"), list) else [],
                },
                bounded,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            bounded.flush()
            os.fsync(raw.fileno())
        os.replace(partial, path)
        return os.path.getsize(path)
    finally:
        try:
            os.unlink(partial)
        except FileNotFoundError:
            pass


def _positive_int(request: dict, key: str) -> int:
    value = request.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"invalid {key}")
    return value


def _arm_linux_memory_limit(request: dict, extractor, resource_error) -> None:
    """Bound child address growth and make it the preferred cgroup OOM victim.

    RLIMIT_RSS is ignored by Linux.  RLIMIT_AS is enforceable, but an absolute
    value is brittle because Python and tree-sitter shared-library mappings
    count toward it.  Preload the grammars, measure this fresh child's current
    virtual size, then allow exactly the configured additional graph budget.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        import resource

        # Load every enabled grammar before measuring the baseline.  build_graph
        # repeats the cheap lookup using already-imported modules.
        extractor._ts_languages()
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            pages = int(fh.read().split()[0])
        current_vms = pages * int(os.sysconf("SC_PAGE_SIZE"))
        target = current_vms + _positive_int(request, "memory_budget_bytes")
        _soft, existing_hard = resource.getrlimit(resource.RLIMIT_AS)
        if existing_hard != resource.RLIM_INFINITY:
            target = min(target, int(existing_hard))
        if target <= current_vms:
            raise RuntimeError("memory limit below extractor baseline")
        resource.setrlimit(resource.RLIMIT_AS, (target, target))
    except resource_error:
        raise
    except Exception as exc:
        # Host/kernel inability to arm the configured safety boundary is
        # infrastructure, not deterministic repository shape.  Let the generic
        # error protocol make the parent preserve the healthy graph.
        raise RuntimeError("memory limit unavailable") from exc

    # Best effort: should the container-wide cgroup still run out before
    # RLIMIT_AS converts an allocation to MemoryError, prefer sacrificing this
    # disposable child over the webhook server.  Non-root may raise its own
    # score; kernels/containers that hide this control simply retain RLIMIT_AS.
    try:
        with open("/proc/self/oom_score_adj", "w", encoding="ascii") as fh:
            fh.write("500\n")
    except OSError:
        pass


def _load_universe(path: str | None) -> list[str] | None:
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, list):
        raise ValueError("invalid universe")
    # The parent obtained this content-free path universe from the DB.  Keep
    # only the shape build_graph accepts; never coerce nested/odd JSON.
    return [p for p in value if isinstance(p, str) and p]


def _full_source_root(request: dict, ingest) -> tuple[str, int, int]:
    archive_path = request.get("archive_path")
    extract_root = request.get("extract_root")
    if not isinstance(archive_path, str) or not isinstance(extract_root, str):
        raise ValueError("invalid full extraction paths")
    os.makedirs(extract_root, mode=0o700, exist_ok=False)
    with tarfile.open(archive_path, mode="r|*") as tf:
        skipped = ingest._safe_extractall(
            tf,
            extract_root,
            max_members=_positive_int(request, "max_members"),
            max_expanded_bytes=_positive_int(request, "max_expanded_bytes"),
        )
    tops = [
        os.path.join(extract_root, name)
        for name in os.listdir(extract_root)
        if os.path.isdir(os.path.join(extract_root, name))
    ]
    root = tops[0] if len(tops) == 1 else extract_root
    files = ingest._count_files(root, max_files=_positive_int(request, "max_files"))
    return root, files, skipped


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    request_path, output_path, meta_path = argv[1:]

    # Import after the tiny protocol has loaded.  ``ingest`` supplies the one
    # canonical safe-tar and file-count implementation; code_graph_extract is
    # the real production extractor.
    import ingest
    import code_graph_extract as extractor

    emergency_reserve = None
    try:
        with open(request_path, "r", encoding="utf-8") as fh:
            request = json.load(fh)
        if not isinstance(request, dict):
            raise ValueError("invalid request")
        mode = request.get("mode")
        output_cap = _positive_int(request, "max_output_bytes")
        _arm_linux_memory_limit(
            request,
            extractor,
            ingest.GraphExtractionResourceLimit,
        )
        # Released in the MemoryError arm so the child can still write its tiny
        # terminal metadata while at the address-space ceiling.
        emergency_reserve = bytearray(512 * 1024)

        source_files = None
        skipped_members = 0
        if mode == "full":
            source_root, source_files, skipped_members = _full_source_root(request, ingest)
            if source_files > _positive_int(request, "max_files"):
                _atomic_json(
                    meta_path,
                    {
                        "status": "over_cap",
                        "reason": "source_file_count_cap",
                        "source_files": source_files,
                        "skipped_members": skipped_members,
                    },
                )
                return 0
            universe = None
        elif mode == "incremental":
            source_root = request.get("source_root")
            if not isinstance(source_root, str) or not os.path.isdir(source_root):
                raise ValueError("invalid incremental source root")
            universe = _load_universe(request.get("universe_path"))
        else:
            raise ValueError("invalid extraction mode")

        graph = extractor.build_graph(source_root, universe_paths=universe)
        output_bytes = _write_graph(
            output_path,
            graph,
            output_cap,
            ingest.GraphExtractionResourceLimit,
        )
        nodes = graph.get("nodes") if isinstance(graph, dict) else []
        edges = graph.get("edges") if isinstance(graph, dict) else []
        _atomic_json(
            meta_path,
            {
                "status": "ok",
                "files": sum(
                    1 for node in nodes
                    if isinstance(node, dict) and node.get("kind") == "file"
                ),
                "edges": len(edges) if isinstance(edges, list) else 0,
                "source_files": source_files,
                "skipped_members": skipped_members,
                "output_bytes": output_bytes,
            },
        )
        return 0
    except ingest.GraphExtractionResourceLimit as exc:
        try:
            os.unlink(output_path)
        except FileNotFoundError:
            pass
        _atomic_json(
            meta_path,
            {"status": "resource_limited", "reason": exc.reason},
        )
        return 0
    except MemoryError:
        emergency_reserve = None
        try:
            os.unlink(output_path)
        except FileNotFoundError:
            pass
        try:
            _atomic_json(
                meta_path,
                {"status": "resource_limited", "reason": "memory_address_space_cap"},
            )
            return 0
        except Exception:
            # Reserved exit code lets the parent classify a native/Python
            # allocation failure even when no memory remained for metadata.
            return 75
    except Exception as exc:
        # Fail loud without serializing exception text: parser exceptions can
        # contain source fragments.  The type is sufficient for a content-free
        # parent log and bounded durable retry.
        try:
            os.unlink(output_path)
        except FileNotFoundError:
            pass
        _atomic_json(
            meta_path,
            {"status": "error", "error_type": type(exc).__name__[:80]},
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
