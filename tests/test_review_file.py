"""Local-only file-review integration and strict history regression cases."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import _cg_cochange as CC
import code_graph_extract as X
from tools import review_file as R

ROOT = Path(__file__).resolve().parents[1]


class FileReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "checkout"
        self.repo.mkdir()
        self.git("init", "-q")

    def git(self, *args):
        with R._git_environment():
            return subprocess.run(
                ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "-C", str(self.repo), *args],
                check=True, capture_output=True, text=True,
            ).stdout

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "Synthetic change")

    def run_cli(self, target="target.py", *, bytecode=True, environment=None):
        env = dict(os.environ)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        if environment:
            env.update(environment)
        return subprocess.run(
            [sys.executable, *(["-B"] if bytecode else []), "-m", "tools.review_file",
             str(self.repo), target], cwd=ROOT, capture_output=True, text=True,
            env=env, timeout=40,
        )

    def test_static_and_incidental_history_are_separate_and_no_target_writes(self):
        self.write("target.py", "def invoice_total():\n    return 5\n")
        self.write("caller.py", "from target import invoice_total\nvalue = invoice_total()\n")
        self.write("peer.py", "def historic_peer():\n    return 7\n")
        self.write("unrelated.py", "def independent_value():\n    return 9\n")
        self.write("progress.txt", "0\n")
        self.commit()
        for i in range(5):
            for name in ("target.py", "peer.py"):
                with (self.repo / name).open("a") as f:
                    f.write(f"# batch {i}\n")
            self.commit()
        for i in range(8):
            self.write("progress.txt", f"{i + 1}\n")
            self.commit()
        def snapshot():
            return {p.relative_to(self.repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.repo.rglob("*") if p.is_file()}
        before = snapshot()
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual([r["file"] for r in data["static"]["candidates"]], ["caller.py"])
        self.assertEqual(data["static"]["candidates"][0]["evidence_kinds"], ["call", "import"])
        self.assertEqual([r["file"] for r in data["history"]["candidates"]], ["peer.py"])
        self.assertEqual(data["history"]["candidates"][0]["co"], 6)
        self.assertEqual(data["history"]["commits_retained"], 14)
        self.assertEqual(data["provenance"]["static_input"], "current_worktree")
        self.assertEqual(data["provenance"]["history_input"], "committed_HEAD")
        self.assertNotIn(str(self.repo), result.stdout + result.stderr)
        self.assertNotIn("fixture@example.invalid", result.stdout)
        self.assertEqual(snapshot(), before)
        other = json.loads(self.run_cli("unrelated.py").stdout)
        self.assertEqual(other["static"]["status"], "not_observed")
        self.assertEqual(other["history"]["candidates"], [])
        self.assertIn("not proof", " ".join(other["limitations"]))

    def test_syntax_error_and_missing_grammar_are_uncertain(self):
        self.write("target.py", "def broken(:\n")
        self.write("foreign.rs", "pub fn unique_value() {}\n")
        self.commit()
        report = R.review(self.repo, "target.py")
        self.assertEqual(report["static"]["status"], "uncertain")
        self.assertGreater(report["static"]["coverage"]["files_failed"], 0)
        with patch.object(X, "_ts_languages", return_value={}):
            report = R.review(self.repo, "foreign.rs")
        self.assertEqual(report["static"]["status"], "uncertain")
        self.assertIn("target_analysis_incomplete", report["static"]["warnings"])
        self.assertGreater(report["static"]["coverage"]["files_skipped"], 0)

    def test_other_ambiguous_documents_degrade_uncertainty_with_zero_parser_errors(self):
        self.write("target.py", "value = 5\n")
        self.write("shared.py", "value = 1\n")
        self.write("shared/__init__.py", "value = 2\n")
        self.write("caller.py", "import shared\n")
        self.commit()
        report = R.review(self.repo, "target.py")
        self.assertEqual(report["static"]["coverage"]["files_failed"], 0)
        self.assertEqual(report["static"]["coverage"]["files_skipped"], 0)
        self.assertEqual(report["static"]["coverage"]["document_analysis_status_counts"],
                         {"failed": 0, "incomplete": 0, "ambiguous": 3})
        self.assertEqual(report["static"]["status"], "uncertain")
        self.assertIn("checkout_analysis_incomplete", report["static"]["warnings"])
        self.assertEqual(report["static"]["candidates"], [])
        related = R.review(self.repo, "caller.py")
        self.assertIn("target_analysis_incomplete", related["static"]["warnings"])
        self.assertEqual({row["file"] for row in related["static"]["candidates"]},
                         {"shared.py", "shared/__init__.py"})

    def test_config_document_status_is_counted_even_when_parser_totals_are_zero(self):
        self.write("target.py", "value = 5\n")
        self.commit()
        graph = X.build_graph(str(self.repo))
        graph["nodes"].append({"id": "settings.json", "kind": "config_file",
                               "path": "settings.json", "analysis_status": "incomplete"})
        with patch.object(X, "build_graph", return_value=graph):
            report = R.review(self.repo, "target.py")
        self.assertEqual(report["static"]["coverage"]["files_failed"], 0)
        self.assertEqual(report["static"]["coverage"]["files_skipped"], 0)
        self.assertEqual(report["static"]["coverage"]["document_analysis_status_counts"]["incomplete"], 1)
        self.assertEqual(report["static"]["status"], "uncertain")

    def test_unborn_history_and_non_source_target_stay_uncertain(self):
        self.write("target.py", "value = 5\n")
        report = R.review(self.repo, "target.py")
        self.assertIsNone(report["provenance"]["head"])
        self.assertEqual(report["history"]["status"], "uncertain")
        self.assertIn("unborn_head", report["history"]["warnings"])
        self.write("notes.txt", "An intentionally unparsed text file\n")
        report = R.review(self.repo, "notes.txt")
        self.assertEqual(report["static"]["status"], "uncertain")
        self.assertIn("target_not_parsed_as_source", report["static"]["warnings"])

    def test_shallow_history_is_explicit(self):
        self.write("target.py", "value = 5\n")
        self.commit()
        shallow = self.base / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", self.repo.as_uri(), str(shallow)],
                       check=True, capture_output=True)
        report = R.review(shallow, "target.py")
        self.assertTrue(report["provenance"]["shallow"])
        self.assertIn("shallow_history", report["history"]["warnings"])

    def test_invalid_paths_symlinks_and_size_limit_are_rejected(self):
        self.write("target.py", "value = 5\n")
        self.commit()
        for value in ("../target.py", str(self.repo / "target.py"), ".git/config", "missing.py", "."):
            result = self.run_cli(value)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["status"], "error")
            self.assertNotIn(str(self.repo), result.stdout + result.stderr)
        (self.repo / "alias.py").symlink_to(self.repo / "target.py")
        self.assertEqual(json.loads(self.run_cli("alias.py").stdout)["error"], "symlink_input")
        self.write("large.py", "x" * (R.MAX_TARGET_BYTES + 1))
        self.assertEqual(json.loads(self.run_cli("large.py").stdout)["error"],
                         "target_exceeds_size_limit")

    def test_target_source_is_not_executed_and_bytecode_startup_is_required(self):
        self.write("target.py", "from pathlib import Path\nPath('executed.txt').write_text('bad')\n")
        self.commit()
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertFalse((self.repo / "executed.txt").exists())
        result = self.run_cli(bytecode=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "bytecode_startup_required")

    def test_changed_input_and_hidden_index_flags_fail_closed(self):
        self.write("target.py", "value = 5\n")
        self.commit()
        original = X.build_graph
        def changed(root):
            graph = original(root)
            self.write("target.py", "value = 6\n")
            return graph
        with patch.object(X, "build_graph", side_effect=changed):
            with self.assertRaisesRegex(R.ReviewError, "checkout_changed"):
                R.review(self.repo, "target.py")
        self.git("update-index", "--assume-unchanged", "target.py")
        with self.assertRaisesRegex(R.ReviewError, "hidden_index_flags"):
            R.review(self.repo, "target.py")

    def test_git_failures_do_not_become_successful_empty_history(self):
        self.write("target.py", "value = 5\n")
        self.commit()
        with patch.object(CC, "_git_log_commits", side_effect=ValueError("private raw error")):
            with self.assertRaisesRegex(R.ReviewError, "history_read_failed"):
                R.review(self.repo, "target.py")
        fake_bin = self.base / "bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/bin/sh\necho 'private raw error' >&2\nexit 7\n")
        fake_git.chmod(0o700)
        result = self.run_cli(environment={"PATH": str(fake_bin)})
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("private raw error", result.stdout + result.stderr)

    def test_unsupported_history_path_is_not_a_valid_empty_candidate_report(self):
        self.write("target.py", "value = 5\n")
        self.write("padded.py ", "value = 7\n")
        self.commit()
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout),
                         {"status": "error", "error": "history_read_failed"})
        self.assertNotIn("padded.py", result.stdout + result.stderr)

    def test_git_transport_is_disabled_and_environment_restored(self):
        with patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "https", "GIT_DIR": "inherited"}):
            with R._git_environment():
                self.assertEqual(os.environ["GIT_ALLOW_PROTOCOL"], "")
                self.assertEqual(os.environ["GIT_NO_LAZY_FETCH"], "1")
                self.assertEqual(os.environ["GIT_OPTIONAL_LOCKS"], "0")
                self.assertNotIn("GIT_DIR", os.environ)
            self.assertEqual(os.environ["GIT_ALLOW_PROTOCOL"], "https")
            self.assertEqual(os.environ["GIT_DIR"], "inherited")

    def test_missing_promisor_object_cannot_start_transport(self):
        self.write("old.py", "\n".join(f"value_{i} = {i}" for i in range(30)) + "\n")
        self.commit()
        blob = self.git("rev-parse", "HEAD:old.py").strip()
        self.git("mv", "old.py", "target.py")
        with (self.repo / "target.py").open("a") as f:
            f.write("extra_value = 31\n")
        self.commit()
        marker = self.base / "transport-started"
        transport = self.base / "transport"
        transport.write_text("#!/bin/sh\nprintf called > " + shlex.quote(str(marker)) + "\nexit 1\n")
        transport.chmod(0o700)
        self.git("config", "core.repositoryformatversion", "1")
        self.git("config", "extensions.partialClone", "origin")
        self.git("config", "remote.origin.url", "ext::" + str(transport))
        self.git("config", "remote.origin.promisor", "true")
        self.git("config", "remote.origin.partialCloneFilter", "blob:none")
        self.git("config", "protocol.ext.allow", "always")
        (self.repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "error")
        self.assertFalse(marker.exists())
        self.assertNotIn(str(transport), result.stdout + result.stderr)

    def test_signed_history_cannot_start_configured_signature_verifier(self):
        self.write("target.py", "value = 5\n")
        self.commit()
        tree = self.git("rev-parse", "HEAD^{tree}").strip()
        parent = self.git("rev-parse", "HEAD").strip()
        body = (f"tree {tree}\nparent {parent}\n"
                "author Fixture <fixture@example.invalid> 1 +0000\n"
                "committer Fixture <fixture@example.invalid> 1 +0000\n"
                "gpgsig -----BEGIN PGP SIGNATURE-----\n"
                " synthetic-signature\n -----END PGP SIGNATURE-----\n\nSynthetic signed commit\n")
        oid = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-t", "commit", "-w", "--stdin"],
                             input=body, text=True, capture_output=True, check=True).stdout.strip()
        self.git("update-ref", "HEAD", oid)
        marker = self.base / "verifier-started"
        verifier = self.base / "verifier"
        verifier.write_text("#!/bin/sh\nprintf called > " + shlex.quote(str(marker)) + "\nexit 1\n")
        verifier.chmod(0o700)
        self.git("config", "gpg.program", str(verifier))
        self.git("config", "log.showSignature", "true")
        self.git("log", "--show-signature", "-1")
        self.assertTrue(marker.exists(), "positive control must actually invoke verifier")
        marker.unlink()
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertNotIn(str(verifier), result.stdout + result.stderr)

    def test_status_filter_from_included_config_is_rejected_before_execution(self):
        self.write("target.py", "value = 5\n")
        self.write(".gitattributes", "target.py filter=canary\n")
        self.commit()
        marker = self.base / "filter-started"
        cleaner = self.base / "cleaner"
        cleaner.write_text("#!/bin/sh\nprintf called > " + shlex.quote(str(marker)) + "\ncat\n")
        cleaner.chmod(0o700)
        included = self.base / "filters.config"
        included.write_text('[filter "canary"]\n clean = ' + str(cleaner) + '\n required = true\n')
        self.git("config", "include.path", str(included))
        self.write("target.py", "value = 7\n")
        self.git("status", "--porcelain=v1")
        self.assertTrue(marker.exists(), "positive control must actually invoke cleaner")
        marker.unlink()
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout),
                         {"status": "error", "error": "unsupported_git_filters"})
        self.assertFalse(marker.exists())
        self.assertNotIn(str(cleaner), result.stdout + result.stderr)


class StrictHistoryTests(unittest.TestCase):
    def test_old_default_preserved_and_strict_errors_are_bounded(self):
        bad = subprocess.CompletedProcess(["git"], 7, stdout="", stderr="private")
        with patch.object(CC.subprocess, "run", return_value=bad):
            self.assertEqual(CC._git_log_commits("unused", 10, 1), [])
            with self.assertRaisesRegex(ValueError, "Git history read failed"):
                CC._git_log_commits("unused", 10, 1, strict=True)
        for error in (OSError("private"), subprocess.TimeoutExpired("git", 1)):
            with patch.object(CC.subprocess, "run", side_effect=error):
                self.assertEqual(CC._git_log_commits("unused", 10, 1), [])
                with self.assertRaisesRegex(ValueError, "Git history read failed"):
                    CC._git_log_commits("unused", 10, 1, strict=True)

    def test_malformed_or_quoted_paths_rejected_and_unicode_retained(self):
        record = CC._REC + "a" * 40 + "\n"
        for output in ("garbage", record + 'M\t"quoted\\tname.py"\n',
                       record + "M\t padded.py\n", record + "M\ttrailing.py \n",
                       record + "M\tone.py\textra\n", CC._REC + "bad\n"):
            result = subprocess.CompletedProcess(["git"], 0, stdout=output)
            with patch.object(CC.subprocess, "run", return_value=result):
                with self.assertRaises(ValueError):
                    CC._git_log_commits("unused", 10, 1, strict=True)
        result = subprocess.CompletedProcess(["git"], 0, stdout=record + "M\t設定.py\n")
        with patch.object(CC.subprocess, "run", return_value=result):
            self.assertEqual(CC._git_log_commits("unused", 10, 1, strict=True), [{"設定.py"}])


if __name__ == "__main__":
    unittest.main()
