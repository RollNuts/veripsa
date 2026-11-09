"""Scoring boundaries plus one real run of the existing synthetic collector."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import time
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
            self.assertEqual(metrics['measurement_schema'], 'synthetic-review-pairs-measurements-v2')
            self.assertNotIn('child_ru_maxrss', metrics)
            self.assertIn('RUSAGE_SELF', metrics['memory_scope'])
            self.assertEqual(metrics['source_sha256']['examples/structural-overlap/review-labels.json'],
                             BENCH.hashlib.sha256((HERE / 'review-labels.json').read_bytes()).hexdigest())
            self.assertGreater(metrics['checkout_input_file_count'], 3)
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

    def _copy_checkout(self, temporary):
        git_path = os.environ.get('VERIPSA_DEMO_GIT') or shutil.which('git') or 'git'
        target = temporary / 'repo'
        target.mkdir()
        paths = BENCH._git_read(git_path, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')
        for relative in sorted(set(path.decode() for path in paths.split(b'\0') if path)):
            source = ROOT / relative
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        git = BENCH.demo.FixtureGit(git_path, target)
        git.call('init', '--quiet')
        git.commit()
        return target, git

    def _start_paused_collector(self, temporary, target, git_path):
        started = temporary / 'worker-started'
        release = temporary / 'worker-release'
        selected = temporary / 'selected-git'
        executable = shutil.which(git_path) or git_path
        selected.write_text('#!/bin/sh\ncase " $* " in\n'
                            '  *" init "*) if (set -C; : > ' + shlex.quote(str(started))
                            + ') 2>/dev/null; then\n'
                            '    n=0; while [ ! -e ' + shlex.quote(str(release))
                            + ' ] && [ "$n" -lt 1500 ]; do sleep 0.02; n=$((n+1)); done\n'
                            '  fi ;;\nesac\nexec ' + shlex.quote(str(Path(executable).resolve()))
                            + ' "$@"\n')
        selected.chmod(0o700)
        measurement = temporary / 'measurement.json'
        process = subprocess.Popen([sys.executable, str(target / 'examples/structural-overlap/benchmark.py'),
                                    '--git', str(selected), '--measurements', str(measurement)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        return process, started, release, measurement

    def _await_worker(self, process, started):
        deadline = time.monotonic() + 30
        while not started.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(started.exists(), 'The collector did not reach the synchronization point.')

    def _stop_test_process(self, process, release):
        release.touch(exist_ok=True)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        process.communicate(timeout=10)

    def test_unchanged_clean_checkout_keeps_stable_scores_and_matching_label_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            target, git = self._copy_checkout(temporary)
            measurement = temporary / 'measurement.json'
            commit = git.call('rev-parse', 'HEAD')
            completed = subprocess.run([sys.executable, str(target / 'examples/structural-overlap/benchmark.py'),
                                        '--git', git.executable, '--measurements', str(measurement)],
                                       capture_output=True, text=True, timeout=150)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), BENCH.score(self.report, self.labels))
            metrics = json.loads(measurement.read_text())
            self.assertEqual(metrics['core_commit'], commit)
            self.assertEqual(completed.stdout, (HERE / 'expected-scores.json').read_text())
            self.assertFalse(metrics['core_dirty'])
            label_bytes = (target / 'examples/structural-overlap/review-labels.json').read_bytes()
            self.assertEqual(metrics['source_sha256']['examples/structural-overlap/review-labels.json'],
                             BENCH.hashlib.sha256(label_bytes).hexdigest())
            self.assertEqual(git.call('status', '--porcelain'), '')

    def test_hidden_index_changes_cannot_be_reported_as_a_clean_pinned_input(self):
        for flag in ('--assume-unchanged', '--skip-worktree'):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                temporary = Path(directory)
                target, git = self._copy_checkout(temporary)
                relative = 'examples/structural-overlap/demo.py'
                git.call('update-index', flag, relative)
                changed = target / relative
                changed.write_text(changed.read_text() + '\n# Uncommitted hidden source edit.\n')
                self.assertEqual(git.call('status', '--porcelain'), '')
                measurement = temporary / 'measurement.json'
                completed = subprocess.run([sys.executable, str(target / 'examples/structural-overlap/benchmark.py'),
                                            '--git', git.executable, '--measurements', str(measurement)],
                                           capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(completed.stdout, '')
                self.assertFalse(measurement.exists())
                self.assertNotIn(str(target), completed.stderr)
                self.assertIn('Hidden or unresolved index entries', completed.stderr)

    @unittest.skipUnless(os.name == 'posix', 'The local synthetic demo uses a POSIX Git wrapper.')
    def test_commit_during_collection_cannot_attribute_old_scores_to_new_clean_source(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            target, git = self._copy_checkout(temporary)
            process, started, release, measurement = self._start_paused_collector(temporary, target, git.executable)
            try:
                self._await_worker(process, started)
                labels_file = target / 'examples/structural-overlap/review-labels.json'
                labels = json.loads(labels_file.read_text())
                next(row for row in labels['cases'] if row['name'] == 'unrelated')['related'] = True
                labels_file.write_text(json.dumps(labels, indent=2) + '\n')
                demo_file = target / 'examples/structural-overlap/demo.py'
                original = 'BANNER: \'def banner():\\n    return "Order summary"\\n\''
                related = 'BANNER: \'from billing.pricing import calculate_total\\n\\ndef banner():\\n    return calculate_total(100, fee=5)\\n\''
                text = demo_file.read_text()
                self.assertIn(original, text)
                demo_file.write_text(text.replace(original, related))
                git.commit()
                self.assertEqual(git.call('status', '--porcelain'), '')
                release.touch()
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 1)
                self.assertEqual(stdout, '')
                self.assertFalse(measurement.exists())
                self.assertNotIn(str(target), stderr)
                self.assertIn('inputs changed during collection', stderr)
                self.assertNotIn('Traceback', stderr)
            finally:
                self._stop_test_process(process, release)

    @unittest.skipUnless(os.name == 'posix', 'The local synthetic demo uses a POSIX Git wrapper.')
    def test_dirty_to_dirty_edits_are_detected_even_without_a_commit_change(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            target, git = self._copy_checkout(temporary)
            labels_file = target / 'examples/structural-overlap/review-labels.json'
            labels = json.loads(labels_file.read_text())
            labels['cases'][0]['reason'] += ' Before collection.'
            labels_file.write_text(json.dumps(labels, indent=2) + '\n')
            initial_commit = git.call('rev-parse', 'HEAD')
            self.assertTrue(git.call('status', '--porcelain'))
            process, started, release, measurement = self._start_paused_collector(temporary, target, git.executable)
            try:
                self._await_worker(process, started)
                labels['cases'][0]['reason'] += ' Changed during collection.'
                labels_file.write_text(json.dumps(labels, indent=2) + '\n')
                self.assertEqual(git.call('rev-parse', 'HEAD'), initial_commit)
                self.assertTrue(git.call('status', '--porcelain'))
                release.touch()
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 1)
                self.assertEqual(stdout, '')
                self.assertFalse(measurement.exists())
                self.assertNotIn(str(target), stderr)
                self.assertIn('inputs changed during collection', stderr)
            finally:
                self._stop_test_process(process, release)


if __name__ == '__main__':
    unittest.main()
