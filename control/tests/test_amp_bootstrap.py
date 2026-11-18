from pathlib import Path
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


HELPER = Path(__file__).parents[1] / 'sbx_control/connectors/amp/bootstrap.py'
# In the sandbox it runs next to the guest helper it imports (core.ship_guest).
sys.path.insert(0, str(Path(__file__).parents[1] / 'sbx_control/template_files'))


class AmpBootstrapTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux' and hasattr(os, 'pidfd_open'), 'Linux process handles required')
    def test_stop_ends_seed_and_updated_bootstraps_but_keeps_executor_and_app(self):
        functions = runpy.run_path(str(HELPER))
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='sbx-amp-pause-')))
        seed, runtime = root / 'seed-amp', root / 'runtime-amp'
        for executable in (seed, runtime):
            shutil.copyfile(sys.executable, executable)
            executable.chmod(0o755)
        (root / 'orb').write_text('import time\nwhile True: time.sleep(0.1)\n')
        processes = [subprocess.Popen([str(executable), 'orb', role], cwd=root)
                     for executable, role in ((seed, 'bootstrap'), (runtime, 'bootstrap'),
                                              (runtime, 'headless'), (runtime, 'application'))]
        try:
            deadline = time.monotonic() + 5
            while not all(functions['bootstrap_process'](Path('/proc') / str(p.pid), (seed, runtime))
                          for p in processes[:2]):
                if any(p.poll() is not None for p in processes) or time.monotonic() > deadline:
                    self.fail('test bootstrap processes did not start')
                time.sleep(0.01)
            # Amp installs updates atomically: the running executable's inode is deleted.
            runtime.unlink()
            shutil.copyfile(sys.executable, runtime)
            runtime.chmod(0o755)
            functions['stop_bootstraps']((seed, runtime))
            for process in processes[:2]:
                self.assertIsNotNone(process.wait(timeout=5))
            self.assertIsNone(processes[2].poll())
            self.assertIsNone(processes[3].poll())
            functions['stop_bootstraps']((seed, runtime))  # a repeated stop is harmless
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def test_stop_matches_an_updated_bootstrap_whose_executable_was_replaced(self):
        function = runpy.run_path(str(HELPER))['bootstrap_process']
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        seed, runtime = root / 'opt/amp', root / 'home/amp'
        process = root / 'proc/4242'
        process.mkdir(parents=True)
        (process / 'exe').symlink_to(f'{runtime} (deleted)')
        (process / 'cmdline').write_bytes(b'\0'.join([str(runtime).encode(), b'orb', b'bootstrap']) + b'\0')
        self.assertTrue(function(process, (seed, runtime)))
        self.assertFalse(function(process, (seed,)))

    def test_stop_raises_when_a_bootstrap_survives(self):
        functions = runpy.run_path(str(HELPER))
        guest = functions['sbx_guest']
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / '4242').mkdir()
        with patch.object(guest, 'PROC', root), patch.object(guest, 'terminate', return_value=False), \
                patch.dict(functions['stop_bootstraps'].__globals__, bootstrap_process=lambda *_: True):
            with self.assertRaisesRegex(RuntimeError, 'did not stop'):
                functions['stop_bootstraps']((root / 'amp',))

    def test_clean_launch_can_replace_itself_and_restart_without_changing_seed(self):
        functions=runpy.run_path(str(HELPER))
        with tempfile.TemporaryDirectory(prefix='sbx-amp-update-') as temporary:
            root=Path(temporary);seed=root/'prepared-amp'
            source='''#!/bin/sh
set -eu
cat > "$AMP_TEST_INSTALL_PATH.new" <<'UPDATE'
#!/bin/sh
printf 'updated-bootstrap-ready\\n'
UPDATE
chmod +x "$AMP_TEST_INSTALL_PATH.new"
mv "$AMP_TEST_INSTALL_PATH.new" "$AMP_TEST_INSTALL_PATH"
exec "$0" "$@"
'''
            seed.write_text(source);seed.chmod(0o755)
            for name in ('first-workspace','second-workspace'):
                home=root/name
                target=functions['runtime_binary'](home,seed)
                result=subprocess.run([str(target),'orb','bootstrap'],
                    env={**os.environ,'AMP_TEST_INSTALL_PATH':str(target)},
                    capture_output=True,text=True,check=True,timeout=5)
                self.assertEqual(result.stdout,'updated-bootstrap-ready\n')
                before=target.read_bytes()
                self.assertEqual(functions['runtime_binary'](home,seed),target)
                self.assertEqual(target.read_bytes(),before)
                self.assertEqual(seed.read_text(),source)

    def test_a_bootstrap_is_only_our_users_orb_bootstrap_of_the_executables(self):
        """A /proc entry: our own process running one of the executables as `orb bootstrap`."""
        function = runpy.run_path(str(HELPER))['bootstrap_process']
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        seed, other = root / 'opt/amp', root / 'home/amp'
        for binary in (seed, other):
            binary.parent.mkdir(parents=True)
            binary.write_text('')
        process = root / 'proc/4242'
        process.mkdir(parents=True)

        def running(binary, *args):
            (process / 'exe').unlink(missing_ok=True)
            (process / 'exe').symlink_to(binary)
            (process / 'cmdline').write_bytes(b'\0'.join([str(binary).encode(), *args]) + b'\0')

        running(seed, b'orb', b'bootstrap')
        self.assertTrue(function(process, (seed.resolve(),)))
        with patch('os.getuid', return_value=os.getuid() + 1):
            self.assertFalse(function(process, (seed.resolve(),)))
        running(seed, b'headless')
        self.assertFalse(function(process, (seed.resolve(),)))
        running(other, b'orb', b'bootstrap')
        self.assertFalse(function(process, (seed.resolve(),)))
        self.assertFalse(function(root / 'proc/gone', (seed.resolve(),)))

    def test_started_is_only_this_launch_s_bootstrap_not_an_earlier_one_still_running(self):
        """A running orb keeps an earlier spawn's bootstrap; a new launch whose
        helper failed before execve must not be taken for started because of it."""
        function = runpy.run_path(str(HELPER))['bootstrap_process']
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        seed = root / 'opt/amp'
        process = root / 'proc/4242'
        process.mkdir(parents=True)
        (process / 'exe').symlink_to(f'{seed} (deleted)')   # and since updated itself
        (process / 'cmdline').write_bytes(b'\0'.join([str(seed).encode(), b'orb', b'bootstrap']) + b'\0')
        (process / 'environ').write_bytes(b'PATH=/bin\0SBX_AMP_LAUNCH=' + b'1' * 16 + b'\0')
        self.assertTrue(function(process, (seed,), '1' * 16))
        self.assertFalse(function(process, (seed,), '2' * 16))
        self.assertTrue(function(process, (seed,)))   # --stop still stops every one

    def test_started_sees_a_running_bootstrap_and_gives_up_on_a_launch_that_never_ran_one(self):
        functions = runpy.run_path(str(HELPER))
        guest = functions['sbx_guest']
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / '4242').mkdir()
        (root / 'self').mkdir()
        clock = [0.0]
        fake_time = type('T', (), {'monotonic': staticmethod(lambda: clock[0]),
                                   'sleep': staticmethod(lambda s: clock.__setitem__(0, clock[0] + s))})
        with patch.object(guest, 'PROC', root), patch.dict(functions['started'].__globals__, time=fake_time):
            with patch.dict(functions['started'].__globals__, bootstrap_process=lambda p, *_: p.name == '4242'):
                self.assertTrue(functions['started']((root / 'amp',), 'a' * 16))
            self.assertEqual(clock[0], 0)   # at once
            with patch.dict(functions['started'].__globals__, bootstrap_process=lambda *_: False):
                self.assertFalse(functions['started']((root / 'amp',), 'a' * 16, seconds=5))
            self.assertGreaterEqual(clock[0], 5)
            self.assertLess(clock[0], 6)   # bounded
