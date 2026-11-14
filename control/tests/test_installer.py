"""The installer's final report (control/install-report.sh), run from a fake
release with stand-ins for sbx-admin and the release's Python; and the
setup-infer exit status it branches on, from the real sbx-admin."""
import contextlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from support import Installation

REPORT = Path(__file__).parents[1] / 'install-report.sh'

SBX_ADMIN = '''
import os, sys
from pathlib import Path
if sys.argv[1:] == ['setup-infer']:
    print('words the script must not depend on')
    sys.exit(int(os.environ['SBX_TEST_STATE']))
elif sys.argv[1:] == ['setup-link']:
    Path(os.environ['SBX_TEST_LINK_REQUESTED']).touch()
    print('https://203.0.113.10/setup#t=test-only')
else:
    sys.exit(2)
'''

SETTINGS = '''
import os, shlex, sys
if sys.argv[1:] != ['-m', 'sbx_control.settings', 'shell']:
    sys.exit(2)
for name in ('domain', 'login'):
    print(name + '=' + shlex.quote(os.environ['SBX_TEST_' + name.upper()]))
'''


def executable(path: Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!{sys.executable}\n' + body)
    path.chmod(0o700)


class InstallReportTests(unittest.TestCase):
    SET_UP, OPEN, ERROR = '0', '3', '1'

    def report(self, *, state, domain='', login='203.0.113.10', password=''):
        with tempfile.TemporaryDirectory(prefix='sbx-install-report-') as directory:
            root = Path(directory)
            release = root / 'release'
            release.mkdir()
            shutil.copy(REPORT, release / 'install-report.sh')
            executable(release / 'venv/bin/python', SETTINGS)
            executable(root / 'bin/sbx-admin', SBX_ADMIN)
            marker = root / 'setup-link-requested'
            result = subprocess.run(['sh', str(release / 'install-report.sh')], capture_output=True, text=True, timeout=10,
                                    env={**os.environ, 'PATH': str(root / 'bin') + os.pathsep + os.environ['PATH'],
                                         'SBX_TEST_STATE': state, 'SBX_TEST_DOMAIN': domain, 'SBX_TEST_LOGIN': login,
                                         'SBX_TEST_LINK_REQUESTED': str(marker), 'SBX_PASSWORD': password})
            return result, marker.exists()

    def test_set_up_installations_print_the_control_url_without_first_run_instructions(self):
        for domain, login in (('', '203.0.113.10'), ('', '[2001:db8::10]'),
                              ('sbx.example.com', 'login.sbx.example.com')):
            with self.subTest(login=login):
                result, link_requested = self.report(state=self.SET_UP, domain=domain, login=login)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'Done: https://{login}', result.stdout)
                self.assertIn(f'curl -fsSL https://{login}/install', result.stdout)
                self.assertNotIn('finish the setup', result.stdout)
                self.assertNotIn('password', result.stdout)
                self.assertFalse(link_requested)

    def test_a_generated_password_is_shown_with_the_url(self):
        result, _ = self.report(state=self.SET_UP, password='generated-once')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Sign in with an admin's email and the password: generated-once", result.stdout)

    def test_incomplete_installations_print_the_setup_link_and_the_generated_password(self):
        result, link_requested = self.report(state=self.OPEN, password='generated-once')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Done: finish the setup in your browser', result.stdout)
        self.assertIn('https://203.0.113.10/setup#t=test-only', result.stdout)
        self.assertIn("Sign in with an admin's email and the password: generated-once", result.stdout)
        self.assertTrue(link_requested)

    def test_unreadable_setup_state_fails_without_minting_a_setup_link(self):
        result, link_requested = self.report(state=self.ERROR)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not read installation setup state', result.stderr)
        self.assertNotIn('Done:', result.stdout)
        self.assertFalse(link_requested)


ENABLE = Path(__file__).parents[2] / 'infisical/enable.sh'

# Stand-ins that log each call: the release's Python (the store already
# Infisical), docker, curl, systemctl, and id (root).
LOGGED = '''
import os, sys
with open(os.environ['SBX_TEST_LOG'], 'a') as log:
    log.write(os.path.basename(sys.argv[0]) + ' ' + ' '.join(sys.argv[1:]) + '\\n')
if os.path.basename(sys.argv[0]) == 'id':
    print(0)
elif sys.argv[1:2] == ['-c']:
    print('port=8190 enabled=1')
'''


class InfisicalEnableTests(unittest.TestCase):
    def test_an_upgrade_brings_the_running_store_to_the_pinned_images_without_setting_it_up_again(self):
        with tempfile.TemporaryDirectory(prefix='sbx-infisical-enable-') as directory:
            root = Path(directory)
            release = root / 'release'
            (release / 'infisical').mkdir(parents=True)
            shutil.copy(ENABLE, release / 'infisical/enable.sh')
            executable(release / 'venv/bin/python', LOGGED)
            for tool in ('docker', 'curl', 'systemctl', 'id'):
                executable(root / 'bin' / tool, LOGGED)
            env_file = root / 'infisical.env'
            env_file.write_text('ENCRYPTION_KEY=test-only\n')
            log = root / 'log'
            result = subprocess.run(['sh', str(release / 'infisical/enable.sh'), 'admin@example.com'],
                                    capture_output=True, text=True, timeout=10,
                                    env={**os.environ, 'PATH': str(root / 'bin') + os.pathsep + os.environ['PATH'],
                                         'SBX_TEST_LOG': str(log), 'INFISICAL_ENV': str(env_file)})
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text()
            self.assertIn('docker compose -f infisical/compose.yaml up -d --wait', calls)
            self.assertIn('curl ', calls)
            for skipped in ('backup', 'systemctl stop', 'secret_store', 'sbx_control.host install'):
                self.assertNotIn(skipped, calls)
            self.assertEqual(env_file.read_text(), 'ENCRYPTION_KEY=test-only\n')


INSTALL = Path(__file__).parents[2] / 'install.sh'


class InstallSettingsTests(unittest.TestCase):
    """install.sh's settings step, cut from the script between its markers,
    with sbx-admin standing in: options are settings on a first install only."""

    def settings_step(self, *, first, **options):
        text = INSTALL.read_text()
        step = text[text.index('list() {'):text.index('echo "settings in sbx.db')]
        with tempfile.TemporaryDirectory(prefix='sbx-install-settings-') as directory:
            log = Path(directory) / 'log'
            names = {'domain': '', 'email': '', 'dns': '', 'sign_in': '', 'repos': '', 'public_repos': '',
                     'admin': '', 'internal': '', **options}
            script = ('set -eu\n' + ''.join(f'{k}={shlex.quote(v)}\n' for k, v in names.items())
                      + f'first={"1" if first else ""}\n'
                      + f'release_admin() {{ echo "$*" >> {shlex.quote(str(log))}; }}\n'
                      + 'die() { echo "FAILED: $*" >&2; exit 1; }\n' + step + 'echo "infisical: $store_admin"\n')
            result = subprocess.run(['sh', '-c', script], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout, log.read_text() if log.exists() else ''

    OPTIONS = {'repos': 'owner/app', 'admin': 'admin@example.com', 'internal': '10.0.0.0/24', 'domain': 'sbx.example.com',
               'email': 'admin@example.com', 'sign_in': 'none'}

    def test_a_first_install_writes_every_option_given(self):
        out, calls = self.settings_step(first=True, **self.OPTIONS)
        self.assertIn('config set repos ["owner/app"]', calls)
        self.assertIn('config set access {"admins": ["admin@example.com"]}', calls)
        self.assertIn('config set network {"internal_cidrs": ["10.0.0.0/24"]}', calls)
        self.assertIn('config set domain', calls)
        self.assertIn('config set sign_in {"method": "none"}', calls)
        self.assertNotIn('ignored', out)

    def test_an_upgrade_keeps_the_settings_and_names_the_options_it_ignored(self):
        out, calls = self.settings_step(first=False, **self.OPTIONS)
        self.assertEqual(calls, '')
        self.assertIn('ignored --domain --email --login --repos --admin --internal', out)
        self.assertIn('infisical: admin@example.com', out)
        out, calls = self.settings_step(first=False)
        self.assertEqual(calls, '')
        self.assertNotIn('ignored', out)


class SetupInferTests(Installation):
    """What install-report.sh and host-install.sh read: the exit status."""

    def infer(self):
        from sbx_control import admin
        with open(os.devnull, 'w') as out, contextlib.redirect_stdout(out):
            return admin.main(['setup-infer'])

    def test_open_until_configured_then_set_up(self):
        from sbx_control import settings, setup, vault
        self.assertEqual(self.infer(), 3)
        settings.update('domain', {'address': '203.0.113.10'}, by='test')
        vault.put(vault.LOGIN_PASSWORD, 'test-only-password', by='test')
        self.assertEqual(self.infer(), 0)
        self.assertTrue(setup.complete())
        self.assertEqual(self.infer(), 0)


class ImportTests(unittest.TestCase):
    def test_sbx_admin_imports_without_deprecation_warnings_from_its_dependencies(self):
        # Every sbx-admin command imports the app; a warning there is printed on each one.
        result = subprocess.run([sys.executable, '-W', 'always', '-c', 'import sbx_control.admin, sbx_control.app'],
                                cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('AuthlibDeprecationWarning', result.stderr)


if __name__ == '__main__':
    unittest.main()
