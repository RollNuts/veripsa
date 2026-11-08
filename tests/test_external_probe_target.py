#!/usr/bin/env python3
"""Offline guard checks: the deployment reference probe needs an explicit target.

Run: python3 tests/test_external_probe_target.py
No network, deployment, credentials, or database is used.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock
from urllib.parse import urlsplit


PROBE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "external_probe.py"
REVIEWED_SHA = "a" * 40


@contextmanager
def configured_probe(environment: dict[str, str]):
    # Import-time defaults must be exercised without the caller's environment.
    with mock.patch.dict(os.environ, environment, clear=True):
        spec = importlib.util.spec_from_file_location("external_probe_under_test", PROBE_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


class ExternalProbeTargetTests(unittest.TestCase):
    def test_missing_or_blank_target_fails_before_fetch(self):
        for target in (None, "", "   "):
            with self.subTest(target=target):
                environment = {"EXPECTED_PLATFORM_SHA": REVIEWED_SHA}
                if target is not None:
                    environment["APP_URL"] = target
                with configured_probe(environment) as probe:
                    output = io.StringIO()
                    with mock.patch.object(probe, "fetch") as fetch, redirect_stdout(output):
                        self.assertEqual(probe.main(), 2)
                    fetch.assert_not_called()
                    self.assertIn("APP_URL must explicitly name", output.getvalue())
                    self.assertIn("suspended", output.getvalue())

    def test_explicit_target_keeps_version_and_public_route_checks(self):
        environment = {
            "APP_URL": " https://probe.example.invalid/ ",
            "EXPECTED_PLATFORM_SHA": REVIEWED_SHA,
        }
        requests = []

        def fake_urlopen(request, *, timeout):
            requests.append(request)
            self.assertEqual(timeout, 20)
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Cache-control"), "no-cache, no-store, max-age=0")
            self.assertEqual(request.get_header("Pragma"), "no-cache")
            response = mock.MagicMock()
            response.status = 200
            response.headers = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
            response.read.return_value = (
                json.dumps({"commit": REVIEWED_SHA}).encode("utf-8")
                if urlsplit(request.full_url).path == "/api/version"
                else b"public fixture"
            )
            response.__enter__.return_value = response
            return response

        with configured_probe(environment) as probe:
            with mock.patch.object(probe.urllib.request, "urlopen", side_effect=fake_urlopen), redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(), 0)
            self.assertEqual(probe.APP_URL, "https://probe.example.invalid")
            urls = [request.full_url for request in requests]
            self.assertTrue(all(url.startswith(probe.APP_URL + "/") for url in urls))
            self.assertEqual(urls[0], probe.APP_URL + "/api/version")
            self.assertTrue(urls[1].startswith(probe.APP_URL + "/api/version?external_probe="))
            self.assertEqual([urlsplit(url).path for url in urls[2:]], list(probe.PUBLIC_PATHS))

    def test_production_secret_guard_still_runs_before_fetch(self):
        environment = {
            "APP_URL": "https://probe.example.invalid",
            "EXPECTED_PLATFORM_SHA": REVIEWED_SHA,
        }
        for name in ("RENDER_API_KEY", "PLATFORM_READ_TOKEN"):
            with self.subTest(name=name), configured_probe({**environment, name: "fixture marker"}) as probe:
                with mock.patch.object(probe, "fetch") as fetch, redirect_stdout(io.StringIO()):
                    self.assertEqual(probe.main(), 2)
                fetch.assert_not_called()

    def test_invalid_reviewed_sha_still_fails_before_fetch(self):
        with configured_probe({"APP_URL": "https://probe.example.invalid", "EXPECTED_PLATFORM_SHA": "short"}) as probe:
            with mock.patch.object(probe, "fetch") as fetch, redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(), 1)
            fetch.assert_not_called()

    def test_mismatched_public_sha_cannot_verify(self):
        with configured_probe({"APP_URL": "https://probe.example.invalid", "EXPECTED_PLATFORM_SHA": REVIEWED_SHA}) as probe:
            response = (200, {"cache-control": ["no-store"], "x-robots-tag": ["noindex"]},
                        json.dumps({"commit": "b" * 40}).encode("utf-8"))
            with mock.patch.object(probe, "fetch", return_value=response) as fetch, redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(), 1)
            self.assertEqual(fetch.call_count, 2)  # canonical + cache-busted only; no public-route sweep


if __name__ == "__main__":
    unittest.main()
