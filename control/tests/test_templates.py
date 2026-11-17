"""Run separately: PYTHONPATH=control python -m unittest control.tests.test_templates."""
import os
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from support import Installation


class TemplateBundleTests(Installation):
    SETTINGS = {"domain": {"address": "203.0.113.10"}}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from sbx_control import templates
        cls.templates = templates

    def test_bundles_come_from_the_installation_unless_another_origin_is_set(self):
        for origin, expected in (("", "https://203.0.113.10"), ("http://[fd00::1]:43001", "http://[fd00::1]:43001")):
            with self.subTest(origin=origin), patch.dict(os.environ, {"SBX_TEMPLATE_BUNDLE_ORIGIN": origin}):
                self.assertEqual(self.templates.bundle_origin(), expected)

    def test_private_origin_serves_real_bundle_without_changing_repo_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, bundles = root / "source", root / "bundles"
            def git(*arguments):
                return subprocess.run(["git", *map(str, arguments)], check=True, capture_output=True, text=True)
            git("init", "-b", "main", source)
            git("-C", source, "config", "user.email", "fixture@example.invalid")
            git("-C", source, "config", "user.name", "fixture")
            (source / "example.txt").write_text("useful source\n")
            git("-C", source, "add", ".")
            git("-C", source, "commit", "-m", "fixture")
            with patch.dict(os.environ, {"SBX_TEMPLATE_BUNDLE_ORIGIN": "http://172.29.50.1:43001/"}), \
                    patch.object(self.templates, "bundles", return_value=bundles):
                token, bundle = self.templates.serve_bundle(source)
                fetch = self.templates.fetch(token)
            self.assertIn("http://172.29.50.1:43001/bundles", fetch)
            self.assertEqual(bundle.parent, bundles)
            git("-C", source, "bundle", "verify", bundle)
            self.assertEqual(git("-C", source, "remote").stdout, "")

    def test_a_bundle_is_served_by_a_token_in_a_header_never_in_the_path(self):
        from fastapi.testclient import TestClient
        from sbx_control import app
        bundles = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch.object(self.templates, "bundles", return_value=bundles), \
                patch("sbx_control.setup.complete", return_value=True), TestClient(app.app) as client:
            token, _ = self.templates.serve_bytes(b"artifact")
            self.assertNotIn(token, self.templates.fetch(token).split()[-1])
            self.assertEqual(client.get("/bundles", headers={self.templates.TOKEN_HEADER: token}).content, b"artifact")
            self.assertEqual(client.get("/bundles").status_code, 404)
            self.assertEqual(client.get(f"/bundles/{token}").status_code, 404)

    def test_invalid_origin_is_rejected_before_publishing_source(self):
        for origin in ("file:///tmp/source", "https://user:password@example.test", "https://example.test/path",
                       "https://example.test?query=1", "https://example.test#fragment", "http://example.test:0",
                       "http://example.test:99999", "http://bad host", "http://bad'host", "//example.test"):
            with self.subTest(origin=origin), patch.dict(os.environ, {"SBX_TEMPLATE_BUNDLE_ORIGIN": origin}), \
                    patch.object(self.templates.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    self.templates.serve_bundle(Path("/unused"))
                run.assert_not_called()

    def test_a_failed_build_s_log_error_and_journal_mask_the_download_token(self):
        import re
        import time
        from unittest.mock import Mock
        from sbx_control import extensions, runtime, template_builds
        from sbx_control.contributions import Artifact, Contribution, TemplateLayer
        from sbx_control.state import db
        tool = Contribution("tool", template=TemplateLayer(artifacts=(Artifact("tool v1", lambda: b"tarball", "/opt"),)))
        commands = []

        def refuse(spec, *, on_log, **kwargs):
            commands.extend(str(value) for _, value in spec.steps)
            fetch = next(c for c in commands if "X-Sbx-Bundle" in c)
            on_log(f"running {fetch}")
            raise RuntimeError(f"the step failed: {fetch}")
        with db() as conn:
            conn.execute("insert into template_builds (runtime, repo, status, built_fingerprint, built_template) "
                         "values ('embed', ?, 'built', ?, 'base-fixture')", (self.templates.BASE, template_builds.plan(self.templates.BASE).fingerprint))
        journal = io.StringIO()
        with patch.object(extensions, "contributions_for", return_value=[tool]), \
                patch.object(runtime, "build_template", Mock(side_effect=refuse)), \
                patch.object(self.templates, "bundles", return_value=Path(self.enterContext(tempfile.TemporaryDirectory()))), \
                contextlib.redirect_stdout(journal):
            self.assertTrue(template_builds.start(self.templates.DEFAULT))
            deadline = time.monotonic() + 10
            while template_builds.building(self.templates.DEFAULT) and time.monotonic() < deadline:
                time.sleep(0.05)
        token = re.search(r"X-Sbx-Bundle: ([A-Za-z0-9_-]{20,})", " ".join(commands)).group(1)
        error = template_builds.state(self.templates.DEFAULT)["error"]
        self.assertIn("the step failed", error)
        for text in (error, journal.getvalue(), "\n".join(self.templates.build_log(self.templates.DEFAULT))):
            self.assertNotIn(token, text)
            self.assertIn("••••", text)

    def test_console_and_retained_build_logs_mask_credentials_and_bundle_capabilities(self):
        secret, url = "owned-synthetic-build-secret", "https://example.invalid/bundles/owned-synthetic-capability"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            log = self.templates.build_logger("owned/test", [secret, url])
            log(SimpleNamespace(level="info", message=f"fetch {url} with {secret}"))
            log(SimpleNamespace(level="debug", message=secret))
        for text in (output.getvalue(), "\n".join(self.templates.build_log("owned/test"))):
            self.assertNotIn(secret, text)
            self.assertNotIn(url, text)
            self.assertIn("••••", text)
