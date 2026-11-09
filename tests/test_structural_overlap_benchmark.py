"""Scoring boundaries plus one real run of the existing synthetic collector."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'examples/structural-overlap'
SPEC = importlib.util.spec_from_file_location('structural_overlap_benchmark', HERE / 'benchmark.py')
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class ReviewPairBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads((HERE / 'expected-output.json').read_text())
        cls.labels = json.loads((HERE / 'review-labels.json').read_text())

    def test_four_labeled_pairs_expose_both_noise_and_misses(self):
        scores = BENCH.score(self.report, self.labels)['scores']
        self.assertEqual({k: scores['graph'][k] for k in ('tp', 'fp', 'fn', 'tn')},
                         dict(tp=2, fp=0, fn=1, tn=1))
        self.assertEqual(scores['graph']['recall'], 2 / 3)
        self.assertEqual(scores['cochange']['precision'], 2 / 3)
        self.assertEqual(scores['union']['precision'], 3 / 4)
        self.assertEqual(scores['union']['recall'], 1)
        self.assertEqual(scores['corroborated']['recall'], 1 / 3)

    def test_functional_relationship_label_does_not_mean_runtime_failure(self):
        result = BENCH.score(self.report, self.labels)
        compatible = next(c for c in result['cases'] if c['name'] == 'compatible_related')
        self.assertTrue(compatible['related_label'])
        self.assertTrue(compatible['signals']['corroborated'])
        self.assertEqual(compatible['integrated_runtime']['status'], 'passed')

    def test_no_candidates_has_undefined_precision_and_real_misses(self):
        altered = copy.deepcopy(self.report)
        for case in altered['cases']:
            case['relationship']['status'] = 'not_observed'
            case['cochange']['status'] = 'not_observed'
            case['combined']['union'] = 'not_observed'
            case['combined']['corroborated'] = 'not_observed'
        counts = BENCH.score(altered, self.labels)['scores']['union']
        self.assertIsNone(counts['precision'])
        self.assertEqual(counts['recall'], 0)
        self.assertEqual(counts['fn'], 3)

    def test_changed_extractor_observation_can_be_scored_without_freezing_old_answer(self):
        altered = copy.deepcopy(self.report)
        dynamic = next(c for c in altered['cases'] if c['name'] == 'dynamic_lookup')
        dynamic['relationship']['status'] = 'observed'
        dynamic['combined']['corroborated'] = 'observed'
        self.assertEqual(BENCH.score(altered, self.labels)['scores']['graph']['recall'], 1)
        # The existing demo's regression contract is still enforced separately.
        with self.assertRaises(BENCH.demo.DemoError):
            BENCH.demo._check_report(altered)

    def test_missing_duplicate_or_unrecognized_cases_cannot_be_silently_scored(self):
        for cases in (self.report['cases'][:-1], self.report['cases'] + [self.report['cases'][0]]):
            altered = copy.deepcopy(self.report)
            altered['cases'] = cases
            with self.subTest(count=len(cases)), self.assertRaises(BENCH.BenchmarkError):
                BENCH.score(altered, self.labels)

    def test_unknown_state_wrong_pair_and_forged_combination_are_rejected(self):
        for field in ('state', 'pair', 'combination'):
            altered = copy.deepcopy(self.report)
            if field == 'state':
                altered['cases'][0]['relationship']['status'] = 'safe'
            elif field == 'pair':
                altered['cases'][0]['changed_files']['change_b'] = ['web/banner.py']
            else:
                altered['cases'][0]['combined']['union'] = 'not_observed'
            with self.subTest(field=field), self.assertRaises(BENCH.BenchmarkError):
                BENCH.score(altered, self.labels)

    def test_malformed_contributor_labels_and_observations_fail_with_bounded_errors(self):
        labels = copy.deepcopy(self.labels)
        labels['cases'][0]['files'] = []
        for invalid in ([], {}, labels, dict(self.labels, cases=[None]),
                        dict(self.labels, cases='not a list')):
            with self.subTest(kind=type(invalid).__name__), self.assertRaises(BENCH.BenchmarkError):
                BENCH.score(self.report, invalid)
        for invalid in ([], {'cases': [None]}, {'cases': 'not a list'}):
            with self.subTest(kind=type(invalid).__name__), self.assertRaises(BENCH.BenchmarkError):
                BENCH.score(invalid, self.labels)

    def test_real_cli_matches_stable_scores_and_keeps_machine_measurements_separate(self):
        git = os.environ.get('VERIPSA_DEMO_GIT') or shutil.which('git') or 'git'
        with tempfile.TemporaryDirectory() as directory:
            measurement = Path(directory) / 'measurement.json'
            completed = subprocess.run([sys.executable, str(HERE / 'benchmark.py'),
                                        '--git', git, '--measurements', str(measurement)],
                                       capture_output=True, text=True, timeout=150)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), BENCH.score(self.report, self.labels))
            metrics = json.loads(measurement.read_text())
            self.assertGreater(metrics['wall_seconds'], 0)
            self.assertNotIn('wall_seconds', completed.stdout)
            self.assertNotIn(str(ROOT), measurement.read_text())
            self.assertNotIn(directory, completed.stdout)

    def test_failed_run_cannot_emit_a_score_or_raw_private_process_output(self):
        completed = subprocess.run([sys.executable, str(HERE / 'benchmark.py'),
                                    '--git', '/missing/private-setup/git'],
                                   capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, '')
        self.assertNotIn('/missing/private-setup', completed.stderr)
        self.assertNotIn('Traceback', completed.stderr)


if __name__ == '__main__':
    unittest.main()
