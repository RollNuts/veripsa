#!/usr/bin/env python3
"""Score four synthetic review pairs, separately from runtime failures.

No network, model, database, or target application is used. The existing demo
executes only its own invented Python fixtures in disposable Git repositories.
Scores describe this deliberately constructed population, not general accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import demo  # noqa: E402

MODES = ('graph', 'cochange', 'union', 'corroborated')


class BenchmarkError(RuntimeError):
    pass


def _observed(value: object) -> bool:
    if value not in ('observed', 'not_observed'):
        raise BenchmarkError('Unknown evidence state; no score is reported.')
    return value == 'observed'


def score(report: dict, labels: dict) -> dict:
    if not isinstance(labels, dict) or labels.get('schema') != 'synthetic-review-pairs-v1':
        raise BenchmarkError('Unsupported label schema.')
    rows = labels.get('cases', [])
    if (not isinstance(rows, list) or not rows
            or not isinstance(labels.get('task'), str) or not isinstance(labels.get('population'), str)):
        raise BenchmarkError('Invalid label collection.')
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('name'), str)
                or not re.fullmatch(r'[a-z][a-z0-9_]*', row['name'])
                or type(row.get('related')) is not bool
                or not isinstance(row.get('files'), list) or len(row['files']) != 2
                or not all(isinstance(path, str) and path for path in row['files'])):
            raise BenchmarkError('Invalid labeled pair.')
    if not isinstance(report, dict) or not isinstance(report.get('cases'), list):
        raise BenchmarkError('Invalid observation collection.')
    cases = report['cases']
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get('name'), str)
                or not all(isinstance(case.get(field), dict) for field in
                           ('changed_files', 'relationship', 'cochange', 'combined', 'integrated'))
                or not all('status' in case[field] for field in ('relationship', 'cochange'))):
            raise BenchmarkError('Invalid observed case.')
        runtime = case['integrated']
        if not ((set(runtime) == {'status', 'return_value'} and runtime['status'] == 'passed'
                 and type(runtime['return_value']) is int)
                or runtime == {'status': 'failed', 'error_type': 'TypeError'}):
            raise BenchmarkError('Invalid synthetic runtime observation.')
    names = [row['name'] for row in rows]
    actual = [case['name'] for case in cases]
    if not rows or len(set(names)) != len(names) or len(set(actual)) != len(actual) or set(actual) != set(names):
        raise BenchmarkError('Missing, duplicate, or unexpected cases; no partial score.')
    observed = {case['name']: case for case in cases}
    values = {mode: dict(tp=0, fp=0, fn=0, tn=0) for mode in MODES}
    evidence = []
    for label in rows:
        case = observed[label['name']]
        changed = case['changed_files']
        if changed != {'change_a': [label['files'][0]], 'change_b': [label['files'][1]]}:
            raise BenchmarkError('Labels do not match the selected input pairs.')
        graph = _observed(case['relationship']['status'])
        history = _observed(case['cochange']['status'])
        signals = dict(graph=graph, cochange=history, union=graph or history,
                       corroborated=graph and history)
        # Recompute comparisons from measured inputs, rather than trusting a
        # forged/precomputed combined verdict in the input report.
        if (case['combined']['union'] != ('observed' if signals['union'] else 'not_observed')
                or case['combined']['corroborated'] != ('observed' if signals['corroborated'] else 'not_observed')):
            raise BenchmarkError('Combined evidence disagrees with its inputs.')
        for mode, prediction in signals.items():
            key = ('tp' if prediction else 'fn') if label['related'] else ('fp' if prediction else 'tn')
            values[mode][key] += 1
        evidence.append({'name': label['name'], 'related_label': label['related'],
                         'signals': signals, 'integrated_runtime': case['integrated']})
    for counts in values.values():
        precision_denom = counts['tp'] + counts['fp']
        recall_denom = counts['tp'] + counts['fn']
        counts['precision'] = counts['tp'] / precision_denom if precision_denom else None
        counts['recall'] = counts['tp'] / recall_denom if recall_denom else None
    return {'benchmark': 'synthetic-review-pairs-v1', 'case_count': len(rows),
            'task': labels['task'], 'population': labels['population'],
            'scores': values, 'cases': evidence,
            'not_measured': ['general precision/recall', 'Top-K ranking', 'human review time',
                             'bug or vulnerability detection', 'unknown OSS cases',
                             'aggregate concurrent process-tree peak memory'],
            'hosted_signal': 'not observed'}


def _run(git_path: str) -> tuple[dict, dict]:
    started = time.perf_counter()
    process = subprocess.run([sys.executable, '-I', '-S', str(HERE / 'benchmark.py'),
                              '--worker', '--git', git_path], capture_output=True,
                             text=True, check=False, timeout=120,
                             stdin=subprocess.DEVNULL)
    elapsed = time.perf_counter() - started
    if process.returncode:
        raise BenchmarkError('Synthetic collection failed; no successful score is claimed.')
    envelope = json.loads(process.stdout)
    if (not isinstance(envelope, dict) or set(envelope) != {'report', 'worker_memory'}
            or not isinstance(envelope['worker_memory'], dict)
            or set(envelope['worker_memory']) != {'ru_maxrss', 'units'}):
        raise BenchmarkError('Invalid worker observation.')
    memory = envelope['worker_memory']
    if not ((type(memory['ru_maxrss']) in (int, float) and memory['ru_maxrss'] >= 0
             and memory['units'] in ('bytes', 'KiB'))
            or memory == {'ru_maxrss': None, 'units': None}):
        raise BenchmarkError('Invalid worker memory observation.')
    measurements = {'measurement_schema': 'synthetic-review-pairs-measurements-v2',
                    'wall_seconds': elapsed,
                    'wall_scope': 'worker startup + four fixtures + Git + extraction + synthetic runtime + probes; excludes input-identity checks',
                    'worker_ru_maxrss': memory['ru_maxrss'], 'worker_ru_maxrss_units': memory['units'],
                    'memory_scope': 'RUSAGE_SELF high-water RSS inside the worker only; excludes parent, Git and synthetic Python descendants; not aggregate concurrent process-tree peak memory',
                    'python': platform.python_version(), 'platform': sys.platform,
                    'reproducibility': 'Compare stable scores separately; timing/RSS vary by machine and caches.'}
    return envelope['report'], measurements


def _git_read(git_path: str, *arguments: str) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
    read = subprocess.run([git_path, '-c', 'core.fsmonitor=false', '-C', str(ROOT), *arguments],
                          env=environment, capture_output=True,
                          timeout=15, check=False, stdin=subprocess.DEVNULL)
    if read.returncode:
        raise BenchmarkError('Could not identify the benchmark checkout.')
    return read.stdout


def _input_state(git_path: str) -> tuple[dict, bytes]:
    """Fingerprint input bytes without publishing the checkout's file list.

    This detects ordinary edits/commits across collection, including edits to
    an already dirty checkout. It is not an atomic filesystem snapshot.
    """
    commit = _git_read(git_path, 'rev-parse', 'HEAD').decode('ascii').strip()
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
        raise BenchmarkError('Invalid checkout identity.')
    index = _git_read(git_path, 'ls-files', '-v', '-z')
    if any(not entry.startswith(b'H ') for entry in index.split(b'\0') if entry):
        # In particular, h/other lowercase tags mean assume-unchanged and S
        # means skip-worktree. status alone can falsely call those bytes clean.
        raise BenchmarkError('Hidden or unresolved index entries are unsupported; use a full checkout.')
    listed = _git_read(git_path, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')
    paths = sorted(set(path.decode('utf-8') for path in listed.split(b'\0') if path))
    source_paths = {str(path.relative_to(ROOT)) for path in
                    (HERE / 'demo.py', HERE / 'benchmark.py', HERE / 'review-labels.json')}
    source_hashes = {}
    labels_bytes = None
    records = []
    for relative in paths:
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts:
            raise BenchmarkError('Invalid checkout input path.')
        absolute = ROOT / path
        info = absolute.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise BenchmarkError('Checkout inputs must be regular files; no symlinks or submodules.')
        with absolute.open('rb') as handle:
            # Do not follow a replacement symlink or silently mix file versions
            # while fingerprinting. The outer before/after guard remains needed.
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise BenchmarkError('Checkout input changed while opening it.')
            body = handle.read()
            finished = os.fstat(handle.fileno())
            if (opened.st_size, opened.st_mtime_ns) != (finished.st_size, finished.st_mtime_ns):
                raise BenchmarkError('Checkout input changed while reading it.')
        digest = hashlib.sha256(body).hexdigest()
        records.append([relative, bool(info.st_mode & stat.S_IXUSR), digest])
        if relative in source_paths:
            source_hashes[relative] = digest
        if absolute == HERE / 'review-labels.json':
            labels_bytes = body
    if set(source_hashes) != source_paths or labels_bytes is None:
        raise BenchmarkError('Required benchmark source inputs are missing.')
    state = {'core_commit': commit,
             'core_dirty': bool(_git_read(git_path, 'status', '--porcelain=v1', '--untracked-files=normal').strip()),
             'checkout_input_sha256': hashlib.sha256(json.dumps(records, ensure_ascii=False,
                                                               separators=(',', ':')).encode('utf-8')).hexdigest(),
             'checkout_input_file_count': len(records), 'source_sha256': source_hashes,
             'input_identity_scope': 'Tracked and nonignored untracked regular-file bytes, executable flags and Git HEAD/dirty state, checked before/after collection before writing outputs; hidden index flags/symlinks/submodules rejected; ignored files excluded; not an atomic filesystem snapshot'}
    return state, labels_bytes


def _worker_memory() -> dict:
    if sys.platform not in ('darwin', 'linux'):
        return {'ru_maxrss': None, 'units': None}
    import resource
    return {'ru_maxrss': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'units': 'bytes' if sys.platform == 'darwin' else 'KiB'}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--git', default=shutil.which('git') or 'git')
    parser.add_argument('--measurements', type=Path,
                        help='Write separate machine measurements to a NEW file; never overwrites an existing file.')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker:
            report = demo.collect_demo(args.git)
            print(json.dumps({'report': report, 'worker_memory': _worker_memory()}))
            return 0
        before, labels_bytes = _input_state(args.git)
        labels = json.loads(labels_bytes.decode('utf-8'))
        data, measurements = _run(args.git)
        after, after_labels = _input_state(args.git)
        if before != after or labels_bytes != after_labels:
            raise BenchmarkError('Benchmark inputs changed during collection; no score is reported.')
        measurements.update(before)
        result = score(data, labels)
        if args.measurements:
            with args.measurements.open('x', encoding='utf-8') as handle:
                json.dump(measurements, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except BenchmarkError as error:
        # All BenchmarkError messages are fixed descriptions, never subprocess
        # output, input content, or interpolated filesystem paths.
        print('Benchmark failed: ' + str(error) + ' No successful score is claimed.', file=sys.stderr)
        return 1
    except (demo.DemoError, OSError, ValueError, KeyError, TypeError,
            subprocess.SubprocessError):
        print('Benchmark failed; check inputs/setup/output path. No successful score is claimed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
