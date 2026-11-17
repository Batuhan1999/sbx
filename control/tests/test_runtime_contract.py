"""The shared pieces of the runtime contract: SettingField.parse, LimitSpec
and Preset by field, PerSandbox and runtime.env's reader."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sbx_control.runtime import env_file
from sbx_control.runtime.base import Limits, LimitSpec, Preset, SettingField, limit_settings
from sbx_control.runtime.hosted import PerSandbox

SPECS = (LimitSpec("timeout_seconds", "X_TIMEOUT", 3600, "Longest run", "seconds", 300, 86400, 300),
         LimitSpec("cpu", "X_CPU", 8, "vCPUs", "", 1, 32, 1))


class Settings(unittest.TestCase):
    def test_parse_normalizes_or_says_why(self):
        number = limit_settings(SPECS)[1]
        self.assertEqual(number.parse(" 16 "), "16")
        self.assertIsNone(number.parse(""))
        with self.assertRaisesRegex(ValueError, r"vCPUs \(X_CPU\) is from 1 to 32"):
            number.parse("64")
        for bad in ("two", "²", "٣", "1" * 13, "0", "-1", "1.0"):
            with self.assertRaisesRegex(ValueError, "whole number"):
                number.parse(bad)
        self.assertEqual(number.parse("0016"), "16")   # canonical: a plan's values match it
        host = SettingField("X_DOMAIN", "Domain", kind="hostname")
        self.assertEqual(host.parse("E2B.App."), "e2b.app")
        with self.assertRaisesRegex(ValueError, "host name"):
            host.parse("not a host")

    def test_limits_and_presets_by_field(self):
        found = Limits.read(SPECS, {"X_CPU": "4"}, "your plan")
        self.assertEqual((found.timeout_seconds, found.cpu, found.keys["cpu"]), (3600, 4, "X_CPU"))
        with self.assertRaisesRegex(SystemExit, "X_CPU=many"):
            Limits.read(SPECS, {"X_CPU": "many"}, "")
        plan = Preset("pro", "Pro", {"timeout_seconds": 86400, "cpu": 8}, adjustable=("cpu",)).bound(SPECS)
        self.assertEqual(plan.view()["values"], {"X_TIMEOUT": "86400", "X_CPU": "8"})
        self.assertEqual(plan.view()["adjustable"], ["X_CPU"])
        self.assertTrue(plan.matches({"X_TIMEOUT": "86400", "X_CPU": "16"}))
        self.assertFalse(plan.matches({"X_TIMEOUT": "3600", "X_CPU": "8"}))


class Handle(unittest.TestCase):
    def test_the_default_handle_never_resumes(self):
        from sbx_control.runtime import NotRunning
        from sbx_control.runtime.fake import Fake
        fake = Fake()
        fake.built["tpl"] = "ready"
        made = fake.create("tpl", timeout=600)
        self.assertEqual(fake.handle(made.sandbox_id).sandbox_id, made.sandbox_id)
        fake.pause(made.sandbox_id)
        with patch.object(fake, "connect") as connect, self.assertRaises(NotRunning):
            fake.handle(made.sandbox_id)
        connect.assert_not_called()


class Kept(unittest.TestCase):
    def test_fetched_once_failures_remembered_and_pruned(self):
        kept, calls = PerSandbox(), []

        def fetch():
            calls.append(1)
            raise RuntimeError("down")
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                kept.get_or_fetch("a", fetch, retry_after=60)
        self.assertEqual(len(calls), 1)
        kept.put(("b", 80), "t")
        self.assertEqual(kept.get_or_fetch(("b", 80), fetch, retry_after=60), "t")
        kept.keep({"c"})
        self.assertEqual((kept.keys(), kept._failed), ([], {}))


class EnvFile(unittest.TestCase):
    def test_reads_and_edits_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "runtime.env"
            with patch.object(env_file, "path", return_value=file):
                env_file.edit({"A": "1 2", "B": None}, header="# header")
                self.assertEqual(file.read_text(), "# header\nA='1 2'\n")
                file.write_text(file.read_text() + " SBX_RUNTIMES = 'embed, boat'\n#B=x\n")
                env_file.edit({"A": None, "C": "3"})
                self.assertEqual(env_file.read(), {"SBX_RUNTIMES": "embed, boat", "C": "3"})
                self.assertEqual(env_file.listed(env_file.read()["SBX_RUNTIMES"]), ["embed", "boat"])
                self.assertEqual(oct(file.stat().st_mode & 0o777), "0o600")
        self.assertEqual(env_file.listed(None), ["embed"])
        self.assertEqual(env_file.listed(""), [])


class CommandName(unittest.TestCase):
    def test_a_failed_commands_name_leaves_its_values_out(self):
        from sbx_control.runtime.base import CommandFailed, command_name
        self.assertEqual(command_name("npm ci --prefer-offline\nnext line"), "npm ci --prefer-offline")
        self.assertEqual(command_name("curl --user acct:hunter2 https://example.test"), "curl --user https://example.test")
        self.assertEqual(command_name("curl -fsSL -H 'X-Token: hunter2' https://example.test"),
                         "curl -fsSL -H https://example.test")
        self.assertEqual(command_name("TOKEN='hunter2 more' false"), "false")
        self.assertEqual(command_name("bash -c 'export K=hunter2\nrun'"), "bash -c")
        self.assertNotIn("hunter2", str(CommandFailed(command="tool --api-key hunter2 go", exit_code=2)))
