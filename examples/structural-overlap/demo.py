#!/usr/bin/env python3
"""Compare clean Git merges with Core's offline structural relationship evidence.

Only synthetic fixtures are executed, inside disposable repositories. This reuses
the existing extractor and the offline evaluator's coarse adjacency projection;
it does not run the database PR gate or manufacture a hosted-service verdict.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
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
    # Analyze the common base, as a fixed source snapshot. Never infer the full
    # database gate's PR verdict from this offline adjacency approximation.
    graph = BT.X.build_graph(str(root))
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
    return {'name': name, 'changed_files': {'change_a': changed_a, 'change_b': changed_b},
            'git_merge': 'clean', 'relationship': _relationship(graph, changed_a, changed_b),
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
    if (len(report['probes']) != 2
            or any(probe['status'] != 'observed' for probe in report['probes'])):
        raise DemoError('An expected static relationship probe was not observed.')


def run_demo(git_path: str) -> dict:
    with tempfile.TemporaryDirectory(prefix='core-structural-demo-') as directory:
        temporary = Path(directory)
        cases = [_case(name, temporary, git_path) for name in (
            'function_contract', 'compatible_related', 'unrelated', 'dynamic_lookup')]
        probes = _probes(temporary)
    report = {
        'demo': 'structural-overlap-v1',
        'scope': 'Synthetic local fixtures; relationship evidence, not PR safety or correctness.',
        'hosted_signal': 'not observed',
        'analysis': 'Existing extractor and coarse offline coupled_pairs; not the database PR gate.',
        'cases': cases, 'probes': probes,
        'limitations': [
            'A structural relationship can exist when the combined change works.',
            'Dynamic module/symbol lookup can hide a relationship even when the combined change fails.',
            'not_observed is not Clear, no dependency, or permission to merge.',
            'Route/config probes parse synthetic source only; they do not run application frameworks.',
        ],
    }
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
