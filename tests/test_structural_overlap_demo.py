"""Real Git and existing-extractor checks for the public offline demo."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'examples/structural-overlap/demo.py'
SPEC = importlib.util.spec_from_file_location('structural_overlap_demo', SCRIPT)
DEMO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEMO)


class StructuralOverlapDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.git = os.environ.get('VERIPSA_DEMO_GIT') or shutil.which('git') or 'git'
        # A poisoned inherited Git location must not redirect the fixture commands.
        with patch.dict(os.environ, {'GIT_DIR': '/not-a-demo-repository',
                                     'GIT_WORK_TREE': '/not-a-demo-worktree'}):
            cls.report = DEMO.run_demo(cls.git)
        cls.cases = {case['name']: case for case in cls.report['cases']}

    def test_committed_example_matches_the_real_demo_report(self):
        expected = json.loads((ROOT / 'examples/structural-overlap/expected-output.json').read_text())
        self.assertEqual(self.report, expected)

    def test_clean_merge_can_break_a_statically_related_function_contract(self):
        case = self.cases['function_contract']
        self.assertEqual(case['git_merge'], 'clean')
        self.assertEqual(case['changed_files'], {'change_a': [DEMO.PRICING],
                                                 'change_b': [DEMO.CHECKOUT]})
        self.assertEqual(case['relationship']['status'], 'observed')
        self.assertIn('import', case['relationship']['kinds'])
        self.assertIn('call', case['relationship']['kinds'])
        self.assertTrue(all(value['status'] == 'passed' for value in case['individual'].values()))
        self.assertEqual(case['integrated'], {'status': 'failed', 'error_type': 'TypeError'})

    def test_relationship_is_not_a_prediction_of_runtime_failure(self):
        case = self.cases['compatible_related']
        self.assertEqual(case['relationship']['status'], 'observed')
        self.assertEqual(case['integrated'], {'status': 'passed', 'return_value': 205})

    def test_unrelated_change_is_not_observed_without_claiming_clear(self):
        case = self.cases['unrelated']
        self.assertEqual(case['relationship'], {'status': 'not_observed', 'kinds': [], 'file_pairs': []})
        self.assertEqual(case['integrated']['status'], 'passed')
        self.assertNotIn('verdict', case)

    def test_dynamic_lookup_demonstrates_a_real_missed_relationship(self):
        case = self.cases['dynamic_lookup']
        self.assertEqual(case['relationship']['status'], 'not_observed')
        self.assertEqual(case['integrated'], {'status': 'failed', 'error_type': 'TypeError'})

    def test_short_history_does_not_pass_default_cochange_filters(self):
        case = self.cases['function_contract']
        history = case['cochange']
        self.assertEqual(history['status'], 'not_observed')
        self.assertEqual(history['parameters']['min_support'], 5)
        self.assertEqual(history['parameters']['min_prob'], 0.3)
        self.assertEqual(history['parameters']['min_lift'], 2.0)
        pair = history['file_pairs'][0]
        self.assertEqual((pair['co'], pair['n_a'], pair['n_b'], pair['n_total']), (1, 1, 1, 1))
        self.assertFalse(pair['passes_default_filters'])
        self.assertEqual(case['combined']['union'], 'observed')
        self.assertEqual(case['combined']['corroborated'], 'not_observed')

    def test_history_complements_the_dynamic_lookup_without_future_commits(self):
        case = self.cases['dynamic_lookup']
        self.assertEqual(case['relationship']['status'], 'not_observed')
        self.assertEqual(case['cochange']['status'], 'observed')
        pair = case['cochange']['file_pairs'][0]
        # Counts come from the common base, before either change or their merge.
        self.assertEqual((pair['co'], pair['n_a'], pair['n_b'], pair['n_total']), (6, 6, 6, 14))
        self.assertEqual(pair['lift'], 2.33)
        self.assertEqual(case['combined']['union'], 'observed')
        self.assertEqual(case['combined']['corroborated'], 'not_observed')

    def test_comment_batching_can_produce_a_noncausal_cochange_signal(self):
        case = self.cases['unrelated']
        self.assertEqual(case['history_profile'], 'incidental_batch_updates')
        self.assertEqual(case['relationship']['status'], 'not_observed')
        self.assertEqual(case['cochange']['status'], 'observed')
        self.assertEqual(case['integrated']['status'], 'passed')

    def test_corroborated_evidence_is_still_not_a_runtime_failure_prediction(self):
        case = self.cases['compatible_related']
        self.assertEqual(case['relationship']['status'], 'observed')
        self.assertEqual(case['cochange']['status'], 'observed')
        self.assertEqual(case['combined']['corroborated'], 'observed')
        self.assertEqual(case['integrated']['status'], 'passed')

    @unittest.skipUnless(os.name == 'posix', 'The local demo uses a POSIX Git wrapper.')
    def test_selected_git_wrapper_with_spaces_and_quotes_reads_real_history(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            selected = temporary / "selected git 'wrapper"
            executable = shutil.which(self.git) or self.git
            selected.write_text('#!/bin/sh\nexec ' + shlex.quote(str(Path(executable).resolve()))
                                + ' "$@"\n', encoding='utf-8')
            selected.chmod(0o700)
            root = temporary / 'repo'
            root.mkdir()
            git = DEMO.FixtureGit(str(selected), root)
            DEMO._write(root, {DEMO.PRICING: DEMO.BASE_PRICING, DEMO.CHECKOUT: DEMO.BASE_CHECKOUT})
            git.call('init', '--quiet')
            git.commit()
            history = DEMO._cochange(git, [DEMO.PRICING], [DEMO.CHECKOUT])
            self.assertEqual(history['file_pairs'][0]['co'], 1)
            self.assertEqual(history['file_pairs'][0]['n_total'], 1)

    def test_existing_route_and_config_producers_are_exercised(self):
        self.assertEqual({probe['name']: probe['status'] for probe in self.report['probes']},
                         {'api_route': 'observed', 'config_key': 'observed'})

    def test_report_is_metadata_only_and_self_check_rejects_a_false_success(self):
        serialized = json.dumps(self.report)
        for forbidden in ('def calculate_total', 'import importlib', str(ROOT), 'Traceback'):
            self.assertNotIn(forbidden, serialized)
        altered = json.loads(serialized)
        altered['cases'][0]['integrated'] = {'status': 'passed', 'return_value': 105}
        with self.assertRaises(DEMO.DemoError):
            DEMO._check_report(altered)
        altered = json.loads(serialized)
        altered['cases'][3]['cochange']['status'] = 'not_observed'
        with self.assertRaises(DEMO.DemoError):
            DEMO._check_report(altered)

    def test_cli_failure_does_not_print_process_output_or_a_success_receipt(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--git',
                                 '/missing/private-local-path/git'],
                                capture_output=True, text=True, check=False, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('/missing/private-local-path', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn('No successful result is claimed', result.stderr)


if __name__ == '__main__':
    unittest.main()
