"""The guest helper (template_files/sbx_guest.py): its shared pieces against
a fake /proc and fake processes on any OS, and its process supervisor as a
real Linux process, without E2B or systemd."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
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


class FullTmpfsTests(unittest.TestCase):
    def test_a_state_write_the_disk_refuses_does_not_stop_the_app(self):
        # memos-demo on the launch lab: Codex filled /run, the supervisor died writing
        # state.json, and its cleanup stopped Memos.
        with tempfile.TemporaryDirectory(prefix="sbx-full-run-") as directory:
            root = Path(directory)
            (root / "spec.json").write_text(json.dumps({"sbxtest_full": "sleep 30"}))
            env = root / "env"
            env.write_text("")
            handlers, rounds, writes, seen = {}, [], [], []

            def write(path, value):
                writes.append(value)
                if len(writes) > 1:
                    raise OSError(28, "No space left on device")

            def tick(_):
                if len(rounds) == 5:
                    return   # the supervisor stopping: its cleanup waits for the child
                rounds.append(1)
                child = writes[0]["processes"]["sbxtest_full"]["pid"]
                seen.append(os.waitpid(child, os.WNOHANG) == (0, 0))   # still running
                if len(rounds) == 5:
                    handlers[signal.SIGTERM]()
            with patch.object(sbx_guest, "write_json", side_effect=write), \
                    patch.object(sbx_guest.signal, "signal", side_effect=lambda sig, handler: handlers.__setitem__(sig, handler)), \
                    patch.object(sbx_guest.time, "sleep", side_effect=tick), \
                    patch.object(sbx_guest, "log_path", return_value=str(root / "app.log")), \
                    patch.object(sbx_guest, "marked", return_value=[]), \
                    patch.object(sbx_guest, "say") as said:
                sbx_guest.supervise(root, str(root), str(env))
        self.assertEqual(len(rounds), 5)
        self.assertEqual(seen, [True] * 5)
        said.assert_called_once()
        self.assertIn("the processes keep running", said.call_args.args[0])


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

    def host_keys(self, folder: Path, tag: str):
        folder.mkdir(exist_ok=True)
        for kind in sbx_guest.HOST_KEYS:
            (folder / f"ssh_host_{kind}_key").write_text(f"private {kind} {tag}\n")
            (folder / f"ssh_host_{kind}_key.pub").write_text(f"ssh-{kind} {tag}-{kind} root@box\n")

    def test_a_cold_boot_s_new_host_keys_give_way_to_the_workspace_s_own(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-guest-keys-")))
        ssh, kept = root / "ssh", root / "kept"
        restarted = []
        with patch.object(sbx_guest, "SSH", ssh), patch.object(sbx_guest, "KEPT_KEYS", kept), \
                patch.object(sbx_guest, "restart_listener", lambda: restarted.append(True)), \
                patch.object(sbx_guest, "serves", lambda expected: expected == ["ssh-ed25519", "ours-ed25519"]):
            self.host_keys(ssh, "ours")
            sbx_guest.keep_host_keys()
            self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o700)
            self.host_keys(ssh, "boot")   # what the runtime's cold boot made
            sbx_guest.restore_host_keys(["ssh-ed25519", "ours-ed25519"])
            self.assertEqual((ssh / "ssh_host_rsa_key").read_text(), "private rsa ours\n")
            self.assertEqual(stat.S_IMODE((ssh / "ssh_host_rsa_key").stat().st_mode), 0o600)
            self.assertEqual(restarted, [True])
            # Keys that are not the recorded identity are never put back.
            self.host_keys(ssh, "boot")
            with self.assertRaisesRegex(sbx_guest.GuestError, "identity changed after initialization"):
                sbx_guest.restore_host_keys(["ssh-ed25519", "someone-else"])
            shutil.rmtree(kept)
            with self.assertRaisesRegex(sbx_guest.GuestError, "identity changed after initialization"):
                sbx_guest.restore_host_keys(["ssh-ed25519", "ours-ed25519"])
            self.assertEqual((ssh / "ssh_host_rsa_key").read_text(), "private rsa boot\n")

    def test_a_ready_workspace_s_listener_is_brought_back_to_its_identity(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-guest-ready-")))
        ssh, kept = root / "ssh", root / "kept"
        self.host_keys(ssh, "ours")
        ours = ["ssh-ed25519", "ours-ed25519"]
        (root / "identity.json").write_text(json.dumps({"workspace_id": "a" * 32, "phase": "ready", "ed25519_public": ours}))
        for listening, restarts in ((True, []), (False, [True])):
            with self.subTest(listening=listening):
                live, restarted = [listening], []
                with patch.object(sbx_guest, "SSH", ssh), patch.object(sbx_guest, "KEPT_KEYS", kept), \
                        patch.object(sbx_guest, "IDENTITY", root / "identity.json"), \
                        patch.object(sbx_guest, "IDENTITY_LOCK", root / "identity.lock"), \
                        patch.object(sbx_guest.os, "geteuid", return_value=0), \
                        patch.object(sbx_guest, "restart_listener", lambda: (restarted.append(True), live.append(True))), \
                        patch.object(sbx_guest, "serves", lambda expected: expected == ours and live[-1]):
                    sbx_guest.identity("a" * 32, 22)
                self.assertEqual(restarted, restarts)
                self.assertTrue((kept / "ssh_host_ed25519_key.pub").exists())

    def listener_root(self):
        """A fake /proc: 456, 458 and 459 are sshd, 457 a pid another program has now."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-guest-listener-")))
        for pid, exe in ((456, "/usr/sbin/sshd"), (457, "/usr/bin/agent"), (458, "/usr/sbin/sshd"),
                         (459, "/usr/sbin/sshd")):
            (root / str(pid)).mkdir()
            (root / str(pid) / "exe").symlink_to(exe)
        self.clock = [0.0]
        self.slept = []
        self.enterContext(patch.object(sbx_guest, "PROC", root))
        self.enterContext(patch.object(sbx_guest, "time", SimpleNamespace(
            monotonic=lambda: self.clock[0],
            sleep=lambda s: (self.slept.append(s), self.clock.__setitem__(0, self.clock[0] + s)))))
        self.opened, self.closed = [], []
        self.enterContext(patch.object(sbx_guest.os, "pidfd_open", lambda pid: self.opened.append(pid) or 1000 + pid,
                                       create=True))
        real_close = os.close

        def close(fd):   # the fake pidfds are recorded; real descriptors (a temporary directory's) really close
            if fd - 1000 in self.opened:
                self.closed.append(fd)
            else:
                real_close(fd)
        self.enterContext(patch.object(sbx_guest.os, "close", close))
        return root

    def listeners(self, *answers):
        """listener_pid answering each of answers in turn (an exception is
        raised; a callable is asked the time), then the last one."""
        answers = list(answers)
        self.listening = [None]

        def answer(timeout=10):
            one = answers.pop(0) if len(answers) > 1 else answers[0]
            if callable(one):
                one = one(self.clock[0])
            if isinstance(one, BaseException):
                raise one
            self.listening[0] = one
            return one
        return patch.object(sbx_guest, "listener_pid", answer)

    def test_a_listener_restarting_as_the_vm_resumes_is_waited_for(self):
        """A runtime's agent restarting sshd as a snapshot resumes: no pid,
        no pid file, a pid that is not sshd for a moment, then sshd again."""
        self.listener_root()
        with self.listeners(sbx_guest.GuestError("workspace SSH listener is not running"),
                            FileNotFoundError("/run/sshd.pid"), 457, 457, 456):
            self.assertEqual(sbx_guest.settled_listener(60, lambda: None), (456, 1456))
        self.assertEqual(self.opened, [457, 457, 456])
        self.assertEqual(self.closed, [1457, 1457])   # every handle but the one returned
        self.assertEqual(len(self.slept), 4)
        self.assertEqual(self.slept, sorted(self.slept))   # backing off

    def test_a_listener_that_stays_something_other_than_sshd_is_refused(self):
        self.listener_root()
        with self.listeners(457), self.assertRaisesRegex(sbx_guest.GuestError, "listener identity changed"):
            sbx_guest.settled_listener(60, lambda: None)
        self.assertAlmostEqual(sum(self.slept), 60)   # up to the deadline, and not past it
        self.assertEqual(len(self.opened), len(self.closed))
        with self.listeners(sbx_guest.GuestError("workspace SSH listener is not running")), \
                self.assertRaisesRegex(sbx_guest.GuestError, "is not running"):
            sbx_guest.settled_listener(self.clock[0] + 60, lambda: None)

    def test_sshd_back_just_before_the_deadline_is_still_found(self):
        """The pauses grow to 5 s; the last is cut short so the deadline itself is looked at."""
        self.listener_root()
        with self.listeners(lambda now: 456 if now >= 57 else 457):
            self.assertEqual(sbx_guest.settled_listener(60, lambda: None), (456, 1456))
        self.assertAlmostEqual(self.clock[0], 60)
        self.assertLess(self.slept[-1], 5)

    def fresh_identity(self, root, ticks, listeners, fresh_on=(), gone=(), sessions=("",)):
        """identity() for a fresh workspace, with fake ssh-keygen, ss and keyscan.
        Each sshd serves the key it last read: the template's, the fresh one
        once it is sent SIGHUP, or the fresh one from the start for a pid in
        fresh_on (started after the keys were replaced). A pidfd in gone has
        exited before its signal. Returns the (pidfd, signal) pairs sent."""
        ssh = root / "ssh"
        self.host_keys(ssh, "template")
        on_disk = lambda: (ssh / "ssh_host_ed25519_key.pub").read_text().split()[:2]
        sessions = list(sessions)

        def run(command, **kwargs):
            if command[0] == "ssh-keygen":
                path = Path(command[command.index("-f") + 1])
                path.write_text("private fresh\n")
                path.with_name(path.name + ".pub").write_text(f"ssh-{command[3]} fresh-{command[3]} root@box\n")
            if command[0] == "ss":
                return SimpleNamespace(stdout=sessions.pop(0) if len(sessions) > 1 else sessions[0])
            return SimpleNamespace(stdout="")
        served = {}

        def serves(expected):
            pid = self.listening[0]
            if pid not in served:
                served[pid] = on_disk() if pid in fresh_on else ["ssh-ed25519", "template-ed25519"]
            return served[pid] == expected
        signalled = []

        def send(fd, sig):
            if fd in gone:
                raise ProcessLookupError(fd)
            signalled.append((fd, sig))
            if sig == signal.SIGHUP:
                served[fd - 1000] = on_disk()
        with patch.object(sbx_guest, "SSH", ssh), patch.object(sbx_guest, "KEPT_KEYS", root / "kept"), \
                patch.object(sbx_guest, "IDENTITY", root / "identity.json"), \
                patch.object(sbx_guest, "IDENTITY_LOCK", root / "identity.lock"), \
                patch.object(sbx_guest.os, "geteuid", return_value=0), \
                patch.object(sbx_guest.os, "umask", return_value=0o022), \
                patch.object(sbx_guest.subprocess, "run", run), \
                patch.object(sbx_guest, "start_ticks", side_effect=ticks), \
                patch.object(sbx_guest.signal, "pidfd_send_signal", send, create=True), \
                patch.object(sbx_guest, "serves", serves), \
                self.listeners(*listeners):
            sbx_guest.identity("b" * 32, 22)
        self.assertEqual(json.loads((root / "identity.json").read_text())["phase"], "ready")
        self.assertEqual(served[self.listening[0]], ["ssh-ed25519", "fresh-ed25519"])
        return signalled

    def test_a_fresh_identity_waits_for_sshd_to_come_back(self):
        root = self.listener_root()
        self.assertEqual(self.fresh_identity(root, ["1", "1"], [sbx_guest.GuestError("not running"), 457, 456]),
                         [(1456, signal.SIGHUP)])

    def test_sshd_restarted_while_its_keys_are_replaced_is_checked_again_and_reloaded(self):
        root = self.listener_root()
        # 456 restarted as 458 during the rotation; between them, a pid that is not sshd.
        self.assertEqual(self.fresh_identity(root, ["1", None], [456, 457, 458]), [(1458, signal.SIGHUP)])

    def test_sshd_that_started_with_the_fresh_keys_is_not_signalled(self):
        root = self.listener_root()
        self.assertEqual(self.fresh_identity(root, ["1", None], [456, 458], fresh_on={458}), [])

    def test_a_replacement_sshd_gone_before_its_signal_is_looked_for_again(self):
        root = self.listener_root()
        self.assertEqual(self.fresh_identity(root, ["1", None], [456, 458, 459], gone={1458}),
                         [(1459, signal.SIGHUP)])

    def test_a_session_opened_while_sshd_was_waited_for_refuses_the_rotation(self):
        root = self.listener_root()
        with self.assertRaisesRegex(sbx_guest.GuestError, "unexpected SSH session"):
            self.fresh_identity(root, ["1", "1"], [457, 456], sessions=("", "ESTAB 0 0 127.0.0.1:22"))
        self.assertEqual((root / "ssh" / "ssh_host_ed25519_key.pub").read_text().split()[1], "template-ed25519")
        self.assertFalse((root / "identity.json").exists())

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


    def test_a_stopped_standalone_sshd_with_no_or_a_stale_pid_is_just_started(self):
        for pid_file in (None, "", "999999\n", "456\n", "457\n"):   # 456, 457: pids other programs have now
            with self.subTest(pid_file=pid_file), tempfile.TemporaryDirectory(prefix="sbx-guest-root-") as directory:
                root = Path(directory)
                (root / "proc/1").mkdir(parents=True)
                (root / "proc/1/comm").write_text("init\n")
                if pid_file is not None:
                    (root / "run").mkdir()
                    (root / "run/sshd.pid").write_text(pid_file)
                (root / "proc/456").mkdir()
                (root / "proc/456/exe").symlink_to("/usr/bin/postgres")
                (root / "proc/457").mkdir()
                (root / "proc/457/exe").symlink_to("/usr/sbin/sshd-helper")

                def pidfd_open(pid):
                    if not (root / "proc" / str(pid)).exists():
                        raise ProcessLookupError(pid)
                    return 77
                started, killed = [], []
                with patch.object(sbx_guest, "Path", lambda path: root / str(path).lstrip("/")), \
                        patch.object(sbx_guest, "PROC", root / "proc"), \
                        patch.object(sbx_guest.os, "pidfd_open", pidfd_open, create=True), \
                        patch.object(sbx_guest.os, "close", lambda fd: None), \
                        patch.object(sbx_guest.signal, "pidfd_send_signal", lambda fd, sig: killed.append(fd), create=True), \
                        patch.object(sbx_guest.subprocess, "run", lambda *a, **kw: started.append(a[0])), \
                        patch.object(sbx_guest, "listener_alive", return_value=True):
                    sbx_guest.restart_listener()
                self.assertEqual((started, killed), ([["/usr/sbin/sshd"]], []))

    def test_the_standalone_sshd_starts_again_only_once_the_old_one_has_let_go_of_the_port(self):
        with tempfile.TemporaryDirectory(prefix="sbx-guest-root-") as directory:
            root = Path(directory)
            for path, text in {"proc/1/comm": "init\n", "run/sshd.pid": "456\n"}.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            (root / "proc/456").mkdir()
            old = root / "proc/456/exe"
            old.symlink_to("/usr/sbin/sshd")
            started = []
            with patch.object(sbx_guest, "Path", lambda path: root / str(path).lstrip("/")), \
                    patch.object(sbx_guest, "PROC", root / "proc"), \
                    patch.object(sbx_guest.os, "pidfd_open", lambda pid: 77, create=True), \
                    patch.object(sbx_guest.os, "close", lambda fd: None), \
                    patch.object(sbx_guest.signal, "pidfd_send_signal", lambda fd, sig: threading.Timer(0.3, old.unlink).start(),
                                 create=True), \
                    patch.object(sbx_guest.subprocess, "run", lambda *a, **kw: started.append(old.is_symlink())), \
                    patch.object(sbx_guest, "listener_alive", return_value=True):
                sbx_guest.restart_listener()
            self.assertEqual(started, [False])

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
from types import SimpleNamespace
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
from types import SimpleNamespace
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
