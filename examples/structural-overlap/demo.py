#!/usr/bin/env python3
"""Compare Git merges, code-graph evidence, and historical co-change locally.

Only synthetic fixtures are executed, inside disposable repositories. This reuses
the existing extractor and the offline evaluator's coarse adjacency projection;
it does not run the database PR gate or manufacture a hosted-service verdict.
The histories are deliberately constructed contrasts, not an accuracy benchmark.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import cochange_backtest as BT  # noqa: E402
import _cg_routes as ROUTES  # noqa: E402

PRICING = 'billing/pricing.py'
CHECKOUT = 'web/checkout.py'
BANNER = 'web/banner.py'
BASE_PRICING = 'def calculate_total(amount, fee=5):\n    return amount + fee\n'
BASE_CHECKOUT = ('from billing.pricing import calculate_total\n\n'
                 'def checkout():\n    return calculate_total(100, fee=5)\n')
DYNAMIC_CHECKOUT = ('import importlib\n\n'
                    'def checkout():\n'
                    '    calculator = getattr(importlib.import_module("billing.pricing"), "calculate_total")\n'
                    '    return calculator(100, fee=5)\n')
RUN_CHECKOUT = '''import json, os, sys
sys.path.insert(0, os.getcwd())
try:
    from web.checkout import checkout
    value = checkout()
except Exception as error:
    print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
    raise SystemExit(1)
print(json.dumps({"status": "passed", "return_value": value}))
'''
READ_COCHANGE = '''import inspect, json, sys
sys.path.insert(0, sys.argv[1])
import _cg_cochange as CC
defaults = inspect.signature(CC.cochange_pairs).parameters
parameters = {name: defaults[name].default for name in
              ("window", "max_commit_files", "min_support", "min_prob", "min_lift", "max_pairs")}
print(json.dumps({"parameters": parameters,
                  "filtered": CC.cochange_pairs(sys.argv[2], timeout=10),
                  "raw": CC.cochange_pairs(sys.argv[2], min_support=1, min_prob=0.0,
                                            min_lift=0.0, timeout=10)}))
'''


class DemoError(RuntimeError):
    """A bounded failure message safe to display without subprocess output."""


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')


class FixtureGit:
    def __init__(self, executable: str, root: Path):
        self.executable = executable
        self.root = root
        self.no_hooks = root.parent / 'empty-hooks'
        self.no_hooks.mkdir(exist_ok=True)
        # An inherited GIT_DIR/GIT_WORK_TREE/config injection must not redirect a
        # fixture command into the caller's repository or run their hooks.
        self.environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                                GIT_TERMINAL_PROMPT='0')

    def call(self, *arguments: str) -> str:
        command = [self.executable, '-c', 'user.name=Core Demo',
                   '-c', 'user.email=demo@example.invalid',
                   '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + str(self.no_hooks),
                   *arguments]
        try:
            result = subprocess.run(command, cwd=self.root, env=self.environment,
                                    capture_output=True, text=True, timeout=15,
                                    stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError):
            raise DemoError('Git fixture command could not complete; check --git.') from None
        if result.returncode:
            raise DemoError('Git fixture command failed; no successful demo is claimed.')
        return result.stdout.strip()

    def commit(self) -> None:
        self.call('add', '--all')
        self.call('commit', '--quiet', '-m', 'Synthetic fixture change')


def _runtime(root: Path) -> dict:
    try:
        result = subprocess.run([sys.executable, '-B', '-I', '-S', '-c', RUN_CHECKOUT],
                                cwd=root, capture_output=True, text=True, timeout=10,
                                stdin=subprocess.DEVNULL, check=False)
        data = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise DemoError('Synthetic Python check did not return a valid result.') from None
    if (result.returncode == 0 and set(data) == {'status', 'return_value'}
            and data['status'] == 'passed' and type(data['return_value']) is int):
        return data
    if (result.returncode == 1 and set(data) == {'status', 'error_type'}
            and data['status'] == 'failed' and data['error_type'] == 'TypeError'):
        return data
    raise DemoError('Unexpected synthetic Python result; no successful demo is claimed.')


def _relationship(graph: dict, changed_a: list[str], changed_b: list[str]) -> dict:
    files = {node['path'] for node in graph['nodes'] if node.get('kind') == 'file'}
    pairs = BT.coupled_pairs(graph, files)
    selected = []
    for left in changed_a:
        for right in changed_b:
            if left == right:
                continue
            kinds = pairs.get(frozenset((left, right)))
            if kinds:
                selected.append({'files': sorted((left, right)), 'kinds': sorted(kinds)})
    selected.sort(key=lambda pair: pair['files'])
    return {'status': 'observed' if selected else 'not_observed',
            'kinds': sorted({kind for pair in selected for kind in pair['kinds']}),
            'file_pairs': selected}


def _seed_history(name: str, git: FixtureGit) -> str:
    if name == 'function_contract':
        return 'short_history'
    peer = BANNER if name == 'unrelated' else CHECKOUT
    # Comments are intentionally edited together: the resulting association can
    # describe a batching habit, not a functional dependency or causal effect.
    for index in range(5):
        for relative in (PRICING, peer):
            path = git.root / relative
            path.write_text(path.read_text(encoding='utf-8')
                            + f'# Synthetic batch marker {index}\n', encoding='utf-8')
        git.commit()
    for index in range(8):
        _write(git.root, {'docs/progress.txt': f'Synthetic unrelated update {index}\n'})
        git.commit()
    return 'incidental_batch_updates' if name == 'unrelated' else 'repeated_joint_updates'


def _cochange(git: FixtureGit, changed_a: list[str], changed_b: list[str]) -> dict:
    # The existing producer invokes the executable named git. A private PATH
    # wrapper selects the same --git binary inside a child process; no global
    # configuration or parent environment is modified, and no math is copied.
    binary_dir = git.root.parent / 'history-git-bin'
    binary_dir.mkdir(exist_ok=True)
    executable = shutil.which(git.executable) or git.executable
    link = binary_dir / 'git'
    if link.is_symlink():
        raise DemoError('Unexpected private Git wrapper path.')
    if not link.exists():
        with link.open('x', encoding='utf-8') as script:
            script.write('#!/bin/sh\nexec ' + shlex.quote(str(Path(executable).resolve())) + ' "$@"\n')
        link.chmod(0o700)
    environment = dict(git.environment)
    environment['PATH'] = str(binary_dir) + os.pathsep + environment.get('PATH', os.defpath)
    try:
        result = subprocess.run([sys.executable, '-B', '-I', '-S', '-c', READ_COCHANGE,
                                 str(ROOT), str(git.root)], cwd=git.root, env=environment,
                                capture_output=True, text=True, timeout=30,
                                stdin=subprocess.DEVNULL, check=False)
        data = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise DemoError('Historical co-change extraction could not complete.') from None
    if result.returncode:
        raise DemoError('Historical co-change extraction failed.')
    selected_pairs = {frozenset((a, b)) for a in changed_a for b in changed_b if a != b}
    observed = {frozenset((row['a'], row['b'])) for row in data['filtered']}
    pairs = []
    for row in data['raw']:
        key = frozenset((row['a'], row['b']))
        if key not in selected_pairs:
            continue
        pairs.append({field: row[field] for field in
                      ('a', 'b', 'co', 'n_a', 'n_b', 'n_total',
                       'p_b_given_a', 'p_a_given_b', 'strength', 'lift')})
        pairs[-1]['passes_default_filters'] = key in observed
    return {'status': 'observed' if selected_pairs & observed else 'not_observed',
            'parameters': data['parameters'], 'file_pairs': pairs}


def _combined(structural: dict, cochange: dict) -> dict:
    graph_seen = structural['status'] == 'observed'
    history_seen = cochange['status'] == 'observed'
    return {'union': 'observed' if graph_seen or history_seen else 'not_observed',
            'corroborated': 'observed' if graph_seen and history_seen else 'not_observed',
            'scope': 'Offline set comparison only; not the database PR policy.'}


def _case(name: str, temporary: Path, git_path: str) -> dict:
    root = temporary / name
    root.mkdir()
    git = FixtureGit(git_path, root)
    base_checkout = DYNAMIC_CHECKOUT if name == 'dynamic_lookup' else BASE_CHECKOUT
    _write(root, {PRICING: BASE_PRICING, CHECKOUT: base_checkout,
                  BANNER: 'def banner():\n    return "Order summary"\n'})
    git.call('init', '--quiet')
    git.call('symbolic-ref', 'HEAD', 'refs/heads/main')
    git.commit()
    history_profile = _seed_history(name, git)
    # Analyze the common base, as a fixed source snapshot. Never infer the full
    # database gate's PR verdict from this offline adjacency approximation.
    graph = BT.X.build_graph(str(root))
    peer = BANNER if name == 'unrelated' else CHECKOUT
    cochange = _cochange(git, [PRICING], [peer])
    git.call('checkout', '--quiet', '-b', 'change-a')
    pricing = (BASE_PRICING.replace('fee=5', 'fee=7') if name == 'compatible_related'
               else BASE_PRICING.replace('amount, fee=5', 'amount, *, fee=5'))
    _write(root, {PRICING: pricing})
    git.commit()
    individual_a = _runtime(root)
    changed_a = git.call('diff', '--name-only', 'main...HEAD').splitlines()

    git.call('checkout', '--quiet', 'main')
    git.call('checkout', '--quiet', '-b', 'change-b')
    if name == 'unrelated':
        _write(root, {BANNER: 'def banner():\n    return "Updated order summary"\n'})
    else:
        checkout = (base_checkout.replace('100, fee=5', '200, fee=5')
                    if name == 'compatible_related'
                    else base_checkout.replace('100, fee=5', '100, 5'))
        _write(root, {CHECKOUT: checkout})
    git.commit()
    individual_b = _runtime(root)
    changed_b = git.call('diff', '--name-only', 'main...HEAD').splitlines()
    git.call('merge', '--quiet', '--no-edit', 'change-a')
    integrated = _runtime(root)
    structural = _relationship(graph, changed_a, changed_b)
    return {'name': name, 'changed_files': {'change_a': changed_a, 'change_b': changed_b},
            'git_merge': 'clean', 'relationship': structural, 'history_profile': history_profile,
            'cochange': cochange, 'combined': _combined(structural, cochange),
            'individual': {'change_a': individual_a, 'change_b': individual_b},
            'integrated': integrated}


def _probes(temporary: Path) -> list[dict]:
    route_root = temporary / 'route-probe'
    _write(route_root, {
        'api/orders.py': ('from fastapi import APIRouter\nrouter = APIRouter()\n'
                          '@router.get("/api/orders/{order_id}")\n'
                          'def order(order_id):\n    return {"id": order_id}\n'),
        'client/orders.ts': 'export const order = () => fetch("/api/orders/5");\n',
    })
    # Static parsing only: FastAPI and a JavaScript runtime are not executed.
    # The producer's existing match_pairs guards are reused without a grammar.
    scan = ROUTES.scan_repo(str(route_root), specificity_floor=True)
    route_files = ['api/orders.py', 'client/orders.ts']
    route_observed = frozenset(route_files) in scan['pairs']
    config_root = temporary / 'config-probe'
    _write(config_root, {
        'config/app.json': '{"checkout_fee": 5}\n',
        'billing/fee.py': 'value = config["checkout_fee"]\n',
        'web/summary.py': 'value = config["checkout_fee"]\n',
    })
    graph = BT.X.build_graph(str(config_root))
    config_files = ['billing/fee.py', 'web/summary.py']
    config = _relationship(graph, [config_files[0]], [config_files[1]])
    return [
        {'name': 'api_route', 'files': route_files,
         'status': 'observed' if route_observed else 'not_observed', 'evidence_kind': 'route'},
        {'name': 'config_key', 'files': config_files, 'status': config['status'],
         'evidence_kind': 'config'},
    ]


def _check_report(report: dict) -> None:
    expected = {
        'function_contract': ('observed', 105, 105, {'status': 'failed', 'error_type': 'TypeError'}),
        'compatible_related': ('observed', 105, 205, {'status': 'passed', 'return_value': 205}),
        'unrelated': ('not_observed', 105, 105, {'status': 'passed', 'return_value': 105}),
        'dynamic_lookup': ('not_observed', 105, 105, {'status': 'failed', 'error_type': 'TypeError'}),
    }
    if {case['name'] for case in report['cases']} != set(expected):
        raise DemoError('The expected fixture matrix is incomplete.')
    for case in report['cases']:
        relation, value_a, value_b, integrated = expected[case['name']]
        expected_b = BANNER if case['name'] == 'unrelated' else CHECKOUT
        if not (case['git_merge'] == 'clean' and case['relationship']['status'] == relation
                and case['changed_files'] == {'change_a': [PRICING], 'change_b': [expected_b]}
                and case['individual']['change_a'] == {'status': 'passed', 'return_value': value_a}
                and case['individual']['change_b'] == {'status': 'passed', 'return_value': value_b}
                and case['integrated'] == integrated):
            raise DemoError('The observed fixture matrix differs from the expected results.')
        history_expected = 'not_observed' if case['name'] == 'function_contract' else 'observed'
        cochange = case['cochange']
        if cochange['status'] != history_expected or len(cochange['file_pairs']) != 1:
            raise DemoError('The observed historical evidence differs from the fixture matrix.')
        pair = cochange['file_pairs'][0]
        expected_counts = (1, 1, 1, 1) if case['name'] == 'function_contract' else (6, 6, 6, 14)
        if tuple(pair[key] for key in ('co', 'n_a', 'n_b', 'n_total')) != expected_counts:
            raise DemoError('The measured historical counts differ from the constructed history.')
        if case['combined'] != _combined(case['relationship'], cochange):
            raise DemoError('The offline evidence comparison disagrees with its measured inputs.')
    if (len(report['probes']) != 2
            or any(probe['status'] != 'observed' for probe in report['probes'])):
        raise DemoError('An expected static relationship probe was not observed.')


def collect_demo(git_path: str) -> dict:
    """Collect observations without freezing a future extractor's answers.

    The normal demo below still enforces its committed regression matrix.
    A benchmark can instead score changed observations against separate labels.
    """
    with tempfile.TemporaryDirectory(prefix='core-structural-demo-') as directory:
        temporary = Path(directory)
        cases = [_case(name, temporary, git_path) for name in (
            'function_contract', 'compatible_related', 'unrelated', 'dynamic_lookup')]
        probes = _probes(temporary)
    report = {
        'demo': 'structural-overlap-v2',
        'scope': 'Synthetic local fixtures; relationship evidence, not PR safety or correctness.',
        'hosted_signal': 'not observed',
        'analysis': ('Existing extractor, coarse offline coupled_pairs, and _cg_cochange.cochange_pairs; '
                     'not the database PR gate.'),
        'cases': cases, 'probes': probes,
        'limitations': [
            'A structural relationship can exist when the combined change works.',
            'Dynamic module/symbol lookup can hide a code-graph relationship; history may add evidence.',
            'Short histories cannot meet the default co-change support floor.',
            'Constructed comment-only co-edits can pass co-change filters without a functional dependency.',
            'Union and corroboration are offline evidence comparisons, not implemented merge-control policies.',
            'These constructed contrasts measure no general precision, recall, causation, or time saved.',
            'not_observed is not Clear, no dependency, or permission to merge.',
            'Route/config probes parse synthetic source only; they do not run application frameworks.',
        ],
    }
    return report


def run_demo(git_path: str) -> dict:
    report = collect_demo(git_path)
    _check_report(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--git', default=shutil.which('git') or 'git',
                        help='Installed Git executable; no user/global Git configuration is loaded')
    args = parser.parse_args(argv)
    try:
        report = run_demo(args.git)
    except (DemoError, OSError, ValueError, KeyError, TypeError):
        print('Structural demo failed; check Python/Git setup. No successful result is claimed.',
              file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
