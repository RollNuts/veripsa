#!/usr/bin/env python3
"""JOB / QUEUE CONTRACT GATE.

Async job systems hide a real cross-dir contract behind runtime names:

  1. Celery: a named task definition couples to a literal send_task producer.
  2. BullMQ: an official Worker queue name couples to an official Queue producer.
  3. Dynamic names, comments, local/shadowed Queue/Worker/task names, and local send_task helpers stay silent.
  4. Duplicate Celery definitions and literal producers are retained as
     explicitly ambiguous, non-adjacent evidence.
  5. Content-free: task bodies, payloads, and comments never appear in graph output.
  6. Additive: a non-job repo produces no job contract nodes/edges.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import code_graph_extract as X  # noqa: E402


def _w(d: str, rel: str, body: str) -> None:
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)


def _build(files: dict[str, str]) -> dict:
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            _w(d, rel, body)
        return X.build_graph(d)


def _nodes_of_kind(g: dict, kind: str) -> dict[str, dict]:
    return {n["id"]: n for n in g["nodes"] if n.get("kind") == kind}


def _edges_to(g: dict, dst: str, kind: str) -> list[str]:
    return [e["src"] for e in g["edges"] if e.get("dst") == dst and e.get("kind") == kind]


def _edge_records_to(g: dict, dst: str, kind: str) -> list[dict]:
    return [
        e for e in g["edges"]
        if e.get("dst") == dst and e.get("kind") == kind
    ]


def _direct_code_edges_between(g: dict, a: str, b: str) -> list[dict]:
    out = []
    for e in g["edges"]:
        if e.get("kind") not in ("calls", "imports"):
            continue
        s = str(e.get("src", ""))
        d = str(e.get("dst", ""))
        if (a in s and b in d) or (b in s and a in d):
            out.append(e)
    return out


def _has_edge(g: dict, dst: str, kind: str, src_part: str) -> bool:
    return any(src_part in src for src in _edges_to(g, dst, kind))


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # (1) Celery crown jewel: producer and worker share a named task with no import edge.
    g1 = _build({
        "workers/tasks.py": (
            "from celery import shared_task\n"
            "# SECRET_COMMENT_SHOULD_NOT_LEAK\n"
            "@shared_task(name='billing.close_invoice')\n"
            "def close_invoice(order_id):\n"
            "    token = 'SECRET_BODY_SHOULD_NOT_LEAK'\n"
            "    return order_id\n"
        ),
        "api/checkout.py": (
            "from celery import Celery\n"
            "celery_app = Celery('billing')\n"
            "def paid(order_id):\n"
            "    return celery_app.send_task('billing.close_invoice', args=[order_id, 'SECRET_PAYLOAD_SHOULD_NOT_LEAK'])\n"
        ),
    })
    celery_id = "job_task::celery::billing.close_invoice"
    check(celery_id in _nodes_of_kind(g1, "job_task"),
          f"(1) Celery task node missing; nodes={list(_nodes_of_kind(g1, 'job_task'))}")
    check(_has_edge(g1, celery_id, "alters", "workers/tasks.py"),
          f"(1) Celery task def must alter the task key; alters={_edges_to(g1, celery_id, 'alters')}")
    check(_has_edge(g1, celery_id, "queries", "api/checkout.py"),
          f"(1) Celery send_task must query the task key; queries={_edges_to(g1, celery_id, 'queries')}")
    check(
        all(
            "reference_status" not in e
            for kind in ("alters", "queries")
            for e in _edge_records_to(g1, celery_id, kind)
        ),
        "(1) unambiguous Celery edges must remain unchanged (no status marker)",
    )
    check(not _direct_code_edges_between(g1, "workers/tasks.py", "api/checkout.py"),
          f"(1) coupling must be via job_task key, not direct code edge; got={_direct_code_edges_between(g1, 'workers/tasks.py', 'api/checkout.py')}")

    # (2) BullMQ crown jewel: producer Queue and consumer Worker share the queue name.
    g2 = _build({
        "worker/email.ts": (
            "import { Worker } from 'bullmq';\n"
            "export const worker = new Worker('email-jobs', async job => job.name);\n"
        ),
        "api/signup.ts": (
            "import { Queue as BullQueue } from 'bullmq';\n"
            "const q = new BullQueue('email-jobs');\n"
            "export function enqueue() { return q.add('welcome', { secret: 'SECRET_JOB_DATA_SHOULD_NOT_LEAK' }); }\n"
        ),
    })
    queue_id = "job_queue::bullmq::email-jobs"
    check(queue_id in _nodes_of_kind(g2, "job_queue"),
          f"(2) BullMQ queue node missing; nodes={list(_nodes_of_kind(g2, 'job_queue'))}")
    check(_has_edge(g2, queue_id, "alters", "worker/email.ts"),
          f"(2) BullMQ Worker must alter queue key; alters={_edges_to(g2, queue_id, 'alters')}")
    check(_has_edge(g2, queue_id, "queries", "api/signup.ts"),
          f"(2) BullMQ Queue producer must query queue key; queries={_edges_to(g2, queue_id, 'queries')}")

    # (2b) Namespace/CommonJS imports work when they still come from bullmq.
    g2b = _build({
        "worker/report.js": "const Bull = require('bullmq');\nnew Bull.Worker('reports', async () => null);\n",
        "api/report.js": "const { Queue: Q } = require('bullmq');\nnew Q('reports');\n",
    })
    reports_id = "job_queue::bullmq::reports"
    check(_has_edge(g2b, reports_id, "alters", "worker/report.js")
          and _has_edge(g2b, reports_id, "queries", "api/report.js"),
          f"(2b) BullMQ require/namespace aliases should couple; edges={_edges_to(g2b, reports_id, 'alters') + _edges_to(g2b, reports_id, 'queries')}")

    # (3) Precision: local classes/helpers, comments, and dynamic names do not couple.
    g3 = _build({
        "workers/tasks.py": (
            "from celery import shared_task\n"
            "@shared_task(name='billing.real')\n"
            "def real(): pass\n"
        ),
        "api/fake.py": (
            "class Fake:\n"
            "    def send_task(self, name): return name\n"
            "fake = Fake()\n"
            "# fake.send_task('billing.real')\n"
            "name = 'billing.real'\n"
            "fake.send_task(name)\n"
        ),
        "worker/local.ts": (
            "class Worker { constructor(name: string, f: any) {} }\n"
            "class Queue { constructor(name: string) {} }\n"
            "// new Worker('local-only', async () => null)\n"
            "const q = 'local-only';\n"
            "new Worker(q, async () => null);\n"
            "new Queue('local-only');\n"
        ),
    })
    check(not _edges_to(g3, "job_task::celery::billing.real", "queries"),
          f"(3) local/dynamic/comment send_task must not query Celery task; queries={_edges_to(g3, 'job_task::celery::billing.real', 'queries')}")
    check(not _nodes_of_kind(g3, "job_queue"),
          f"(3) local Queue/Worker classes must not mint BullMQ queues; nodes={list(_nodes_of_kind(g3, 'job_queue'))}")

    g3b = _build({
        "tasks/local.py": (
            "def shared_task(**kwargs):\n"
            "    def wrap(fn): return fn\n"
            "    return wrap\n"
            "@shared_task(name='billing.fake')\n"
            "def fake(): pass\n"
        ),
        "api/run.py": "class App:\n    def send_task(self, name): return name\nApp().send_task('billing.fake')\n",
    })
    check(not _nodes_of_kind(g3b, "job_task"),
          f"(3b) local shared_task helper must not mint Celery tasks; nodes={list(_nodes_of_kind(g3b, 'job_task'))}")

    g3c = _build({
        "tasks/shadow.py": (
            "from celery import shared_task, Celery\n"
            "def task(**kwargs):\n"
            "    def wrap(fn): return fn\n"
            "    return wrap\n"
            "@task(name='billing.shadow')\n"
            "def shadow(): pass\n"
            "shared_task = task\n"
            "@shared_task(name='billing.rebound')\n"
            "def rebound(): pass\n"
        ),
        "api/run.py": (
            "from celery import Celery\n"
            "app = Celery('billing')\n"
            "app.send_task('billing.shadow')\n"
            "app.send_task('billing.rebound')\n"
        ),
    })
    check(not _nodes_of_kind(g3c, "job_task"),
          f"(3c) shadowed Celery decorator names must not mint tasks; nodes={list(_nodes_of_kind(g3c, 'job_task'))}")

    g3d = _build({
        "tasks/multiline.py": (
            "from celery import shared_task\n"
            "@shared_task(\n"
            "    name='billing.multiline'\n"
            ")\n"
            "def multiline(): pass\n"
        ),
        "api/multiline.py": (
            "from celery import Celery\n"
            "app = Celery('billing')\n"
            "app.send_task('billing.multiline')\n"
        ),
    })
    multiline_id = "job_task::celery::billing.multiline"
    check(_has_edge(g3d, multiline_id, "alters", "tasks/multiline.py")
          and _has_edge(g3d, multiline_id, "queries", "api/multiline.py"),
          f"(3d) multiline Celery decorator name should couple; edges={_edges_to(g3d, multiline_id, 'alters') + _edges_to(g3d, multiline_id, 'queries')}")

    g3e = _build({
        "worker/shadow.ts": (
            "import { Worker } from 'bullmq';\n"
            "class Worker { constructor(name: string, f: any) {} }\n"
            "new Worker('shadowed', async () => null);\n"
        ),
        "api/shadow.ts": (
            "import { Queue } from 'bullmq';\n"
            "const Queue = class { constructor(name: string) {} };\n"
            "new Queue('shadowed');\n"
        ),
        "worker/ns.ts": (
            "let Bull = require('bullmq');\n"
            "Bull = { Worker: class { constructor(name: string, f: any) {} } };\n"
            "new Bull.Worker('ns-shadowed', async () => null);\n"
        ),
        "api/ns.ts": (
            "const Bull = require('bullmq');\n"
            "const Other = Bull;\n"
            "new Bull.Queue('ns-shadowed');\n"
        ),
    })
    for qid in ("job_queue::bullmq::shadowed", "job_queue::bullmq::ns-shadowed"):
        check(qid not in _nodes_of_kind(g3e, "job_queue"),
              f"(3e) shadowed BullMQ aliases must not mint {qid}; nodes={list(_nodes_of_kind(g3e, 'job_queue'))}")

    # (4) Duplicate Celery task definitions/references remain inert evidence.
    g4 = _build({
        "a/tasks.py": "from celery import shared_task\n@shared_task(name='billing.dup')\ndef a(): pass\n",
        "b/tasks.py": "from celery import shared_task\n@shared_task(name='billing.dup')\ndef b(): pass\n",
        "api/run.py": "from celery import Celery\napp = Celery('x')\napp.send_task('billing.dup')\n",
    })
    dup_id = "job_task::celery::billing.dup"
    duplicate_node = _nodes_of_kind(g4, "job_task").get(dup_id)
    check(
        duplicate_node is not None
        and (duplicate_node.get("provenance") or {}).get("ambiguous") is True,
        "(4) duplicate Celery task must remain as explicit ambiguous evidence",
    )
    check(
        (g4.get("metrics") or {}).get("ambiguous_reference_count", 0) >= 1,
        "(4) duplicate Celery task must increment ambiguity observability",
    )
    duplicate_edges = (
        _edge_records_to(g4, dup_id, "alters")
        + _edge_records_to(g4, dup_id, "queries")
    )
    check(
        {e["src"] for e in duplicate_edges}
        == {"a/tasks.py", "b/tasks.py", "api/run.py"},
        f"(4) duplicate Celery task must preserve definitions and literal send_task; "
        f"edges={duplicate_edges}",
    )
    check(
        duplicate_edges
        and all(e.get("reference_status") == "ambiguous" for e in duplicate_edges),
        f"(4) every duplicate Celery edge must be explicitly ambiguous; edges={duplicate_edges}",
    )
    duplicate_file_statuses = {
        n.get("path"): n.get("analysis_status")
        for n in g4["nodes"]
        if n.get("kind") == "file"
        and n.get("path") in {"a/tasks.py", "b/tasks.py", "api/run.py"}
    }
    check(
        set(duplicate_file_statuses) == {"a/tasks.py", "b/tasks.py", "api/run.py"}
        and set(duplicate_file_statuses.values()) == {"ambiguous"},
        f"(4) all files touching the ambiguous Celery task must be locally Unknown; "
        f"statuses={duplicate_file_statuses}",
    )

    # (5) Content-free.
    blob = json.dumps([g1, g2], sort_keys=True)
    for secret in (
        "SECRET_COMMENT_SHOULD_NOT_LEAK",
        "SECRET_BODY_SHOULD_NOT_LEAK",
        "SECRET_PAYLOAD_SHOULD_NOT_LEAK",
        "SECRET_JOB_DATA_SHOULD_NOT_LEAK",
    ):
        check(secret not in blob, f"(5) content-free violation: {secret} leaked into graph")

    # (6) Additive: pure source with matching words but no framework anchors produces no job contracts.
    g6 = _build({
        "src/app.py": "def send_task(name): return name\nsend_task('billing.close_invoice')\n",
        "src/app.ts": "new Queue('email-jobs'); new Worker('email-jobs', () => null);\n",
    })
    check(not _nodes_of_kind(g6, "job_task") and not _nodes_of_kind(g6, "job_queue"),
          f"(6) non-framework repo must not mint job contracts; task={list(_nodes_of_kind(g6, 'job_task'))} queue={list(_nodes_of_kind(g6, 'job_queue'))}")
    check(not [e for e in g6["edges"] if str(e.get("dst", "")).startswith(("job_task::", "job_queue::"))],
          "(6) non-framework repo must not emit job contract edges")

    if failures:
        print("JOB / QUEUE CONTRACT GATE: FAIL")
        for f in failures:
            print("  -", f)
        return 1

    print("JOB / QUEUE CONTRACT GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
