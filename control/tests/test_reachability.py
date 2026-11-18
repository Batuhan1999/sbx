"""Whether sandboxes reach this server at its public address: the check
behind sbx-admin doctor and the first-run setup, with the runtime faked."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from support import Installation, Server


class ReachabilityTests(Installation):
    SETTINGS = {"domain": {"name": "sbx.example.com"}}

    def setUp(self):
        super().setUp()
        from sbx_control import reachability, runtime, template_builds, templates
        self.reachability, self.runtime = reachability, runtime
        self.enterContext(patch.object(template_builds, "built", return_value="sbx-base-1234"))
        self.enterContext(patch.object(templates, "pinned_key", return_value=""))
        self.sandbox = MagicMock(sandbox_id="probe-1")
        self.create = self.enterContext(patch.object(runtime, "create", return_value=self.sandbox))

    def answer(self, stdout: str, stderr: str = ""):
        self.sandbox.commands.run.return_value = SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=0)
        return self.reachability.check()

    def test_a_sandbox_that_fetches_this_servers_version_passes_and_is_killed(self):
        check = self.answer("0 200\ndev")
        self.assertTrue(check.ok, check)
        self.assertIn("https://login.sbx.example.com/dist/version", check.detail)
        self.assertEqual(self.create.call_args.args[0], "sbx-base-1234")
        self.assertEqual(self.create.call_args.kwargs["on_timeout"], "kill")
        self.assertNotIn("sbx", self.create.call_args.kwargs["metadata"])   # not a workspace (core.find)
        self.assertIn("https://login.sbx.example.com/dist/version", self.sandbox.commands.run.call_args.args[0])
        self.sandbox.kill.assert_called_once()

    def test_each_failure_is_named_with_what_to_fix_and_the_sandbox_is_killed(self):
        cases = [("6 000\n", "curl: (6) Could not resolve host: login.sbx.example.com", "does not resolve", "DNS record"),
                 ("7 000\n", "curl: (7) Failed to connect", "refused or is unreachable", "port 443"),
                 ("28 000\n", "curl: (28) Connection timed out", "timed out", "firewall"),
                 ("60 000\n", "curl: (60) SSL certificate problem", "TLS", "certificate"),
                 ("90 000\n", "curl: (90) pinned public key mismatch", "not this server's", "something else"),
                 ("0 502\nbad gateway", "", "HTTP 502", "proxy"),
                 ("0 200\n9.9.9", "", "not as this server", "another sbx"),
                 ("", "", "exit -1", "curl")]
        for stdout, stderr, detail, fix in cases:
            with self.subTest(stdout=stdout):
                self.sandbox.reset_mock()
                check = self.answer(stdout, stderr)
                self.assertFalse(check.ok)
                self.assertIn(detail, check.detail)
                self.assertIn(fix, check.fix)
                self.sandbox.kill.assert_called_once()

    def test_a_runtime_error_in_the_sandbox_still_kills_it(self):
        self.sandbox.commands.run.side_effect = self.runtime.Timeout("no answer")
        check = self.reachability.check()
        self.assertFalse(check.ok)
        self.assertIn("no answer", check.detail)
        self.sandbox.kill.assert_called_once()

    def test_a_failing_kill_does_not_hide_the_answer(self):
        self.sandbox.kill.side_effect = RuntimeError("gone")
        with patch("sys.stderr"):
            self.assertTrue(self.answer("0 200\ndev").ok)

    def test_no_sandbox_starts_without_an_address_or_a_base_template(self):
        from sbx_control import settings, template_builds
        self.create.side_effect = self.runtime.NoRoom("full")
        check = self.reachability.check()
        self.assertFalse(check.ok)
        self.assertIn("no sandbox to check from", check.detail)
        self.sandbox.kill.assert_not_called()

        with patch.object(template_builds, "built", side_effect=template_builds.NotBuilt("building")), \
                patch.object(template_builds, "state", return_value={"status": "building"}):
            check = self.reachability.check()
        self.assertTrue(check.ok)
        self.assertIn("not checked yet", check.detail)

        settings.update("domain", {"name": ""}, by="test")
        check = self.reachability.check()
        self.assertFalse(check.ok)
        self.assertIn("no public address", check.detail)

    def test_a_failed_base_build_fails_with_its_error_and_how_to_build_again(self):
        from sbx_control import template_builds
        with patch.object(template_builds, "built", side_effect=template_builds.NotBuilt("failed")), \
                patch.object(template_builds, "state", return_value={"status": "failed", "error": "apt-get: 404"}):
            check = self.reachability.check()
        self.assertFalse(check.ok)
        self.assertIn("build failed: apt-get: 404", check.detail)
        self.assertIn("sbx-admin template rm sbx-base", check.fix)
        self.create.assert_not_called()

    def test_a_self_signed_certificate_is_pinned_like_a_template_build(self):
        from sbx_control import templates
        with patch.object(templates, "pinned_key", return_value="--insecure --pinnedpubkey sha256//abc="):
            self.answer("0 200\ndev")
        self.assertIn("--pinnedpubkey sha256//abc=", self.sandbox.commands.run.call_args.args[0])

    def test_doctor_and_setup_run_it_after_the_runtime_checks_pass(self):
        from sbx_control import connections, runtime, setup
        from sbx_control.connections.base import Check
        fake = MagicMock(capabilities=runtime.Capabilities(), kind="embed")
        fake.test.return_value = [Check("Embed API", True, "answers")]
        fake.readiness.return_value = []
        fake.disk.return_value = None
        self.sandbox.commands.run.return_value = SimpleNamespace(stdout="6 000\n", stderr="", exit_code=0)
        with patch.dict(runtime._backends, {"embed": fake}):
            names = [c.name for c in connections.get("runtime").test()]
            ready = setup.readiness()
            fake.test.return_value = [Check("Embed API", False, "down")]
            down = [c.name for c in connections.get("runtime").test()]
        self.assertEqual(names, ["Embed API", self.reachability.NAME])
        self.assertFalse(ready["ok"])
        self.assertEqual(down, ["Embed API"])   # no sandbox started when the runtime itself is down


class SetupGateTests(Server):
    def test_the_probed_path_alone_answers_while_setup_is_incomplete(self):
        from sbx_control import reachability, setup
        self.assertFalse(setup.complete())
        response = self.client.get(reachability.PATH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.text, "dev")
        for path in ("/dist/version/x", "/dist/versions", "/dist/sbx.tgz.sha256/x", "/install/x", "/api/sandboxes"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 503)

    def test_the_cli_installer_and_sbx_login_answer_while_setup_is_incomplete(self):
        """So an agent signs the CLI in as the admin once the account exists and finishes the setup itself."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from sbx_control import dist_api
        dist = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (dist / "install.sh").write_text("echo @SBX_CONTROL@\n")
        (dist / "sbx.tgz.sha256").write_text("0" * 64 + "  sbx.tgz\n")
        with patch.object(dist_api, "DIST", dist):
            self.assertEqual(self.client.get("/install").status_code, 200)
            self.assertNotEqual(self.client.get("/dist/sbx.tgz.sha256").status_code, 503)
        self.assertEqual(self.client.get("/cli/login", params={"port": 4000, "state": "s", "challenge": "c" * 43},
                                         follow_redirects=False).status_code, 307)
        self.assertEqual(self.client.get("/api/me").status_code, 401)


if __name__ == "__main__":
    unittest.main()
