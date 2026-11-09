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
    # This child is the only child of this CLI process. ru_maxrss is reported
    # in its platform's units, not mislabeled as aggregate process-tree memory.
    process = subprocess.run([sys.executable, '-I', '-S', str(HERE / 'benchmark.py'),
                              '--worker', '--git', git_path], capture_output=True,
                             text=True, check=False, timeout=120,
                             stdin=subprocess.DEVNULL)
    elapsed = time.perf_counter() - started
    if process.returncode:
        raise BenchmarkError('Synthetic collection failed; no successful score is claimed.')
    data = json.loads(process.stdout)
    rss = None
    rss_units = None
    if sys.platform in ('darwin', 'linux'):
        import resource
        rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        rss_units = 'bytes' if sys.platform == 'darwin' else 'KiB'
    # Read checkout identity only after collecting RSS, so these Git reads do
    # not contaminate the single-worker resource observation above.
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
    def git_read(*arguments: str) -> str:
        read = subprocess.run([git_path, '-c', 'core.fsmonitor=false', '-C', str(ROOT), *arguments],
                              env=environment, capture_output=True, text=True,
                              timeout=15, check=False, stdin=subprocess.DEVNULL)
        if read.returncode:
            raise BenchmarkError('Could not identify the benchmark checkout.')
        return read.stdout.strip()
    commit = git_read('rev-parse', 'HEAD')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
        raise BenchmarkError('Invalid checkout identity.')
    measurements = {'wall_seconds': elapsed, 'wall_scope': 'worker startup + four fixtures + Git + extraction + synthetic runtime + probes',
                    'child_ru_maxrss': rss, 'child_ru_maxrss_units': rss_units,
                    'memory_scope': 'RUSAGE_CHILDREN high-water RSS after one worker; not sum/aggregate peak of concurrent processes',
                    'python': platform.python_version(), 'platform': sys.platform,
                    'core_commit': commit,
                    'core_dirty': bool(git_read('status', '--porcelain=v1', '--untracked-files=normal')),
                    'source_sha256': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in (HERE / 'demo.py', HERE / 'benchmark.py', HERE / 'review-labels.json')},
                    'reproducibility': 'Compare stable scores separately; timing/RSS vary by machine and caches.'}
    return data, measurements


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--git', default=shutil.which('git') or 'git')
    parser.add_argument('--measurements', type=Path,
                        help='Write separate machine measurements to a NEW file; never overwrites an existing file.')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker:
            print(json.dumps(demo.collect_demo(args.git)))
            return 0
        labels = json.loads((HERE / 'review-labels.json').read_text(encoding='utf-8'))
        data, measurements = _run(args.git)
        result = score(data, labels)
        if args.measurements:
            with args.measurements.open('x', encoding='utf-8') as handle:
                json.dump(measurements, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (BenchmarkError, demo.DemoError, OSError, ValueError, KeyError, TypeError,
            subprocess.SubprocessError):
        print('Benchmark failed; check inputs/setup/output path. No successful score is claimed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
