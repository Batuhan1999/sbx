"""The guest helper (template_files/sbx_guest.py): its shared pieces against
a fake /proc and fake processes on any OS, and its process supervisor as a
real Linux process, without E2B or systemd."""
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HELPER = Path(__file__).parents[1] / "sbx_control/template_files/sbx_guest.py"
sys.path.insert(0, str(HELPER.parent))
import sbx_guest  # noqa: E402


def fake_proc(root: Path, pid: int, comm: str = "worker", state: str = "S", ticks: int = 4242, environ: dict = None):
    """/proc/<pid>/stat and environ as Linux writes them: comm in parentheses,
    then state and 49 more fields, the 22nd field the start time."""
    folder = root / str(pid)
    folder.mkdir(parents=True, exist_ok=True)
    fields = [state, *["0"] * 18, str(ticks), *["0"] * 30]
    (folder / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")
    (folder / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in (environ or {}).items()) + b"\0")


class SharedPiecesTests(unittest.TestCase):
    def setUp(self):
        self.proc = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-guest-proc-")))

    def test_start_ticks_survive_a_comm_with_spaces_and_parentheses(self):
        fake_proc(self.proc, 10, comm="evil) S 1 2 (name", ticks=777)
        self.assertEqual(sbx_guest.start_ticks(10, self.proc), "777")

    def test_a_zombie_or_missing_process_has_no_start_time(self):
        fake_proc(self.proc, 11, state="Z")
        self.assertIsNone(sbx_guest.start_ticks(11, self.proc))
        self.assertIsNone(sbx_guest.start_ticks(12, self.proc))

    def test_a_reused_pid_is_not_the_recorded_process(self):
        fake_proc(self.proc, 13, ticks=100)
        entry = sbx_guest.record(13, self.proc)
        self.assertTrue(sbx_guest.alive(entry, self.proc))
        fake_proc(self.proc, 13, ticks=200)
        self.assertFalse(sbx_guest.alive(entry, self.proc))
        self.assertFalse(sbx_guest.alive({}, self.proc))

    def test_marked_finds_only_processes_carrying_the_exact_marker(self):
        fake_proc(self.proc, 20, environ={"PATH": "/bin", "_SBX_PROCESS_SCOPE": "abc"})
        fake_proc(self.proc, 21, environ={"_SBX_PROCESS_SCOPE": "abcd"})
        fake_proc(self.proc, 22, state="Z", environ={"_SBX_PROCESS_SCOPE": "abc"})
        fake_proc(self.proc, 23, environ={"OTHER": "_SBX_PROCESS_SCOPE=abc"})
        (self.proc / "self").mkdir()
        self.assertEqual([e["pid"] for e in sbx_guest.marked("_SBX_PROCESS_SCOPE", "abc", self.proc)], [20])
        self.assertEqual(sbx_guest.marked("_SBX_PROCESS_SCOPE", "", self.proc), [])

    def drain(self, processes, **budget):
        """Fake processes: name -> the signal that ends it (None: nothing does)."""
        sent = []

        def signal_all(sig):
            sent.append(sig)
            for name, ends in list(processes.items()):
                if ends == sig or (ends == signal.SIGTERM and sig == signal.SIGKILL):
                    processes.pop(name)
        result = sbx_guest.drain(signal_all, lambda: bool(processes), interval=0.01, **budget)
        return result, sent

    def test_drain_stops_at_sigterm_when_everything_exits(self):
        self.assertEqual(self.drain({"app": signal.SIGTERM}, grace=1, kill_grace=1), (True, [signal.SIGTERM]))

    def test_drain_kills_what_ignores_sigterm(self):
        self.assertEqual(self.drain({"app": signal.SIGTERM, "stubborn": signal.SIGKILL}, grace=0.05, kill_grace=1),
                         (True, [signal.SIGTERM, signal.SIGKILL]))

    def test_drain_reports_a_survivor(self):
        self.assertEqual(self.drain({"unkillable": None}, grace=0.05, kill_grace=0.05),
                         (False, [signal.SIGTERM, signal.SIGKILL]))

    def test_json_is_replaced_whole_and_unreadable_json_is_empty(self):
        path = self.proc / "state.json"
        sbx_guest.write_json(path, {"pid": 1})
        sbx_guest.write_json(path, {"pid": 2})
        self.assertEqual(sbx_guest.read_json(path), {"pid": 2})
        self.assertEqual([p.name for p in self.proc.iterdir()], ["state.json"])
        path.write_text("{partial")
        self.assertEqual(sbx_guest.read_json(path), {})
        self.assertEqual(sbx_guest.read_json(self.proc / "missing.json"), {})

    def test_a_refusal_is_the_command_s_error(self):
        result = subprocess.run([sys.executable, str(HELPER), "processes", "restart", "--root", str(self.proc / "p"),
                                 "--spec", json.dumps({"../escape": "sleep 1"})], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("sbx-guest processes: processes must map simple names", result.stderr)


class IdentityTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
    def test_generated_host_public_keys_are_readable_under_private_umask(self):
        with tempfile.TemporaryDirectory(prefix="sbx-ssh-identity-") as directory:
            root = Path(directory)
            mask = os.umask(0o077)
            try:
                for kind in sbx_guest.HOST_KEYS:
                    subprocess.run(["ssh-keygen", "-q", "-t", kind, "-N", "", "-f", str(root / f"ssh_host_{kind}_key")],
                                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            finally:
                os.umask(mask)
            sbx_guest.key_permissions(root)
            for path in root.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644 if path.name.endswith(".pub") else 0o600)

    def test_ssh_listener_is_systemd_s_or_the_standalone_sshd(self):
        """A fake / (its /proc/1/comm and /run/sshd.pid) and a fake systemctl."""
        for init, managed, standalone, expected in [("systemd", "123", "456", 123), ("systemd", "0", "456", 456),
                                                    ("init", "0", "456", 456), ("init", "0", "0", None)]:
            with self.subTest(init=init, managed=managed, standalone=standalone), \
                    tempfile.TemporaryDirectory(prefix="sbx-guest-root-") as directory:
                root = Path(directory)
                for path, text in {"proc/1/comm": f"{init}\n", "run/sshd.pid": f"{standalone}\n",
                                   "bin/systemctl": f"#!/bin/sh\necho {managed}\n"}.items():
                    (root / path).parent.mkdir(parents=True, exist_ok=True)
                    (root / path).write_text(text)
                (root / "bin/systemctl").chmod(0o755)
                with patch.object(sbx_guest, "Path", lambda path: root / str(path).lstrip("/")), \
                        patch.dict(os.environ, {"PATH": f"{root}/bin:{os.environ['PATH']}"}):
                    if expected is None:
                        self.assertRaises(sbx_guest.GuestError, sbx_guest.listener_pid)
                    else:
                        self.assertEqual(sbx_guest.listener_pid(), expected)


@unittest.skipUnless(sys.platform == "linux", "sandbox process identities use Linux /proc")
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sbx-process-test-")
        self.root = Path(self.directory.name)
        self.env = self.root / "applied env"
        self.env.write_text("export LITMUS_VALUE='applied value'\n[ -r /nonexistent-sbx-optional-env ] && . /nonexistent-sbx-optional-env\n")
        self.base = [sys.executable, str(HELPER), "processes"]
        self.options = ["--root", str(self.root / "state"), "--workdir", str(self.root), "--env-file", str(self.env)]

    def tearDown(self):
        self.command("stop")
        self.directory.cleanup()

    def command(self, action, spec=None, check=True):
        return subprocess.run([*self.base, action, *self.options, "--spec", json.dumps(spec or {})],
                              capture_output=True, text=True, check=check, timeout=35)

    def state(self):
        return json.loads((self.root / "state/state.json").read_text())

    def wait(self, predicate):
        if not sbx_guest.wait_until(predicate, 8):
            self.fail("condition was not reached")

    def test_restart_injects_env_and_gracefully_stops_previous_children(self):
        child = self.root / "worker.py"
        child.write_text('''import os, signal, time
from pathlib import Path
Path("started").write_text(os.environ["LITMUS_VALUE"])
def stop(*_):
    Path("stopped").write_text("graceful")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
while True: time.sleep(0.1)
''')
        spec = {"sbxtest_worker": f"{shlex.quote(sys.executable)} {shlex.quote(str(child))}"}
        self.command("restart", spec)
        self.wait(lambda: (self.root / "started").exists())
        self.assertEqual((self.root / "started").read_text(), "applied value")
        old_pid = self.state()["processes"]["sbxtest_worker"]["pid"]
        self.command("restart", spec)
        self.assertEqual((self.root / "stopped").read_text(), "graceful")
        self.assertNotEqual(old_pid, self.state()["processes"]["sbxtest_worker"]["pid"])
        self.assertEqual(self.command("probe").stdout.strip(), "alive")
        self.command("stop")
        self.assertEqual(self.command("probe").stdout.strip(), "exited")

    def test_failure_is_restarted_and_clean_exit_is_reported(self):
        command = "if [ ! -f retried ]; then touch retried; exit 1; fi; sleep 60"
        self.command("restart", {"sbxtest_retry": command})
        self.wait(lambda: self.state()["processes"]["sbxtest_retry"].get("failures") == 1
                  and self.state()["processes"]["sbxtest_retry"]["status"] == "running")
        time.sleep(0.5)
        self.assertTrue((self.root / "retried").exists())
        self.assertEqual(self.command("probe").stdout.strip(), "alive")
        self.command("restart", {"sbxtest_retry": "exit 0"})
        self.wait(lambda: self.command("probe").stdout.strip() == "exited")

    def test_bad_spec_cannot_stop_running_app(self):
        self.command("restart", {"sbxtest_safe": "sleep 60"})
        before = self.state()["pid"]
        rejected = self.command("restart", {"../escape": "sleep 60"}, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(before, self.state()["pid"])
        self.assertEqual(self.command("probe").stdout.strip(), "alive")

    def test_restart_stops_a_descendant_that_started_another_session(self):
        detached = self.root / "detached.py"
        detached.write_text('''import os, signal, time
from pathlib import Path
def stop(*_):
    Path("detached-stopped").write_text("stopped")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
Path("detached-pid").write_text(str(os.getpid()))
while True: time.sleep(0.1)
''')
        launcher = self.root / "launcher.py"
        launcher.write_text('''import subprocess, sys, time
subprocess.Popen([sys.executable, "detached.py"], start_new_session=True)
while True: time.sleep(0.1)
''')
        self.command("restart", {"sbxtest_detached": f"{shlex.quote(sys.executable)} {shlex.quote(str(launcher))}"})
        self.wait(lambda: (self.root / "detached-pid").exists())
        old_pid = int((self.root / "detached-pid").read_text())
        self.command("restart", {"sbxtest_detached": "sleep 60"})
        self.assertEqual((self.root / "detached-stopped").read_text(), "stopped")
        self.assertIsNone(sbx_guest.start_ticks(old_pid))

    def test_terminate_kills_what_ignores_sigterm_and_spares_what_is_no_longer_owned(self):
        command = [sys.executable, "-c", "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(300)"]
        stubborn, spared = subprocess.Popen(command), subprocess.Popen(command)
        try:
            time.sleep(.1)
            sbx_guest.terminate(stubborn.pid, lambda: True, grace=0.2)
            sbx_guest.terminate(spared.pid, lambda: False, grace=0.2)
            self.assertEqual(stubborn.wait(timeout=5), -signal.SIGKILL)
            self.assertIsNone(spared.poll())
        finally:
            spared.kill()
            spared.wait(timeout=5)

    def test_stop_drains_tagged_start_hook_that_ignores_term_without_killing_unrelated_app(self):
        marker = os.urandom(16).hex()
        command = [sys.executable, "-c", "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(300)"]
        child = subprocess.Popen(command, env={**os.environ, sbx_guest.START_SCOPE: marker}, start_new_session=True)
        unrelated = subprocess.Popen(command, start_new_session=True)
        try:
            (self.root / sbx_guest.START_SCOPE_FILE).write_text(json.dumps({"scope": marker}))
            time.sleep(.1)
            self.command("stop")
            self.assertIsNotNone(child.poll())
            self.assertIsNone(unrelated.poll())
            self.assertFalse((self.root / sbx_guest.START_SCOPE_FILE).exists())
        finally:
            for process in (child, unrelated):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
