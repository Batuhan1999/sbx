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
                              ('sbx.example.com', 'app.sbx.example.com')):
            with self.subTest(login=login):
                result, link_requested = self.report(state=self.SET_UP, domain=domain, login=login)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'Done: https://{login}', result.stdout)
                self.assertIn(f'curl -fsSL https://{login}/install', result.stdout)
                self.assertNotIn('finish the setup', result.stdout)
                self.assertNotIn('password', result.stdout)
                self.assertFalse(link_requested)

    def test_a_generated_password_is_shown_with_the_url(self):
        result, _ = self.report(state=self.SET_UP, password='operator@example.com: generated-once')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  operator@example.com: generated-once", result.stdout)

    def test_incomplete_installations_print_the_setup_link_and_the_generated_password(self):
        result, link_requested = self.report(state=self.OPEN, password='operator@example.com: generated-once')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Done: finish the setup in your browser', result.stdout)
        self.assertIn('https://203.0.113.10/setup#t=test-only', result.stdout)
        self.assertIn("  operator@example.com: generated-once", result.stdout)
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
            self.assertLess(calls.index('compose -f infisical/compose.yaml pull --quiet'),
                            calls.index('compose -f infisical/compose.yaml up -d --wait'))   # the images first

    # docker, failing `up` (and `pull`) the first N times it is asked, then answering; its logs carry a credential.
    FLAKY = LOGGED + '''
if os.path.basename(sys.argv[0]) == 'docker':
    verb = next((a for a in sys.argv[1:] if a in ('pull', 'up', 'ps', 'logs')), '')
    count = os.path.join(os.path.dirname(os.environ['SBX_TEST_LOG']), 'count-' + verb)
    done = int(open(count).read()) if os.path.exists(count) else 0
    open(count, 'w').write(str(done + 1))
    if verb == 'ps':
        print('infisical-backend-1  restarting')
    elif verb == 'logs':
        print('backend-1 | migrations running')
        print('backend-1 | DB_CONNECTION_URI=postgres://infisical:test-only-db-password@db:5432/infisical')
    elif done < int(os.environ.get('SBX_TEST_FAIL_' + verb.upper(), '0')):
        sys.exit(1)
'''

    def enable(self, **fail):
        with tempfile.TemporaryDirectory(prefix='sbx-infisical-enable-') as directory:
            root = Path(directory)
            release = root / 'release'
            (release / 'infisical').mkdir(parents=True)
            shutil.copy(ENABLE, release / 'infisical/enable.sh')
            executable(release / 'venv/bin/python', LOGGED)
            for tool in ('docker', 'curl', 'systemctl', 'id'):
                executable(root / 'bin' / tool, self.FLAKY)
            env_file = root / 'infisical.env'
            env_file.write_text('ENCRYPTION_KEY=test-only\n')
            log = root / 'log'
            result = subprocess.run(['sh', str(release / 'infisical/enable.sh'), 'admin@example.com'],
                                    capture_output=True, text=True, timeout=20,
                                    env={**os.environ, 'PATH': str(root / 'bin') + os.pathsep + os.environ['PATH'],
                                         'SBX_TEST_LOG': str(log), 'INFISICAL_ENV': str(env_file),
                                         'SBX_INFISICAL_PAUSE': '0',
                                         **{f'SBX_TEST_FAIL_{k.upper()}': str(v) for k, v in fail.items()}})
            return result, log.read_text()

    def test_a_slow_first_start_and_a_stumbling_registry_are_tried_again(self):
        result, calls = self.enable(pull=1, up=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count('pull --quiet'), 2)
        self.assertEqual(calls.count('up -d --wait'), 3)
        self.assertIn('Infisical is not healthy yet (try 2 of 4)', result.stderr)

    def test_a_start_that_never_comes_says_what_failed_without_credentials(self):
        result, calls = self.enable(up=9)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.count('up -d --wait'), 4)
        self.assertIn('infisical-backend-1  restarting', result.stderr)   # compose ps
        self.assertIn('migrations running', result.stderr)                # the logs' last lines
        self.assertNotIn('test-only-db-password', result.stderr)
        self.assertIn('FAILED: Infisical did not start', result.stderr)
        result, calls = self.enable(pull=9)
        self.assertIn('FAILED: could not pull', result.stderr)
        self.assertNotIn('up -d', calls)


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


class UpgradeFailureTests(unittest.TestCase):
    """An upgrade stops sbx-control before Embed starts. On the launch lab
    Embed's preflight refused (18 GiB free of the 20 it wants) and the
    install ended with sbx-control stopped and every preview down."""

    def test_a_failure_before_the_new_release_runs_rolls_back_and_one_after_does_not(self):
        text = INSTALL.read_text()
        start = text.index('  if [ -n "$rollback" ]; then')
        guard = text[start:text.index('  fi\n', start) + 5]
        activated = next(line for line in text.splitlines() if line.startswith('die() {') and 'nothing to roll back' in line)
        with tempfile.TemporaryDirectory(prefix='sbx-install-rollback-') as directory:
            marker, rollback = Path(directory) / 'rolled-back', Path(directory) / 'rollback.sh'
            rollback.write_text(f'touch {shlex.quote(str(marker))}\n')
            for after, rolls_back in (('', True), (activated + '\n', False)):
                with self.subTest(after_activation=bool(after)):
                    marker.unlink(missing_ok=True)
                    script = (f'rollback={shlex.quote(str(rollback))}\n' + guard + after
                              + 'die "Embed did not come up"\necho not reached\n')
                    result = subprocess.run(['sh', '-c', script], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertNotIn('not reached', result.stdout)
                    self.assertIn('FAILED: Embed did not come up', result.stderr)
                    self.assertEqual(marker.exists(), rolls_back)


# Stand-ins for what --check asks the system: ss (one listener, from
# SBX_TEST_LISTENER "port:process"), curl (the public IP), systemd-detect-virt, nproc.
CHECK_TOOLS = {
    'ss': '''
import os, sys
port, _, process = os.environ.get('SBX_TEST_LISTENER', '').partition(':')
if port and f':{port} ' in ' '.join(sys.argv[1:]) + ' ':
    print(f'tcp LISTEN 0 511 0.0.0.0:{port} 0.0.0.0:* users:(("{process}",pid=1,fd=6))')
''',
    'curl': 'print("203.0.113.10")\n',
    'systemd-detect-virt': 'import os; print(os.environ["SBX_TEST_VIRT"])\n',
    'nproc': 'print(8)\n',
}


class InstallCheckTests(unittest.TestCase):
    """install.sh --check: the host's facts and the runtimes it can run,
    decided as the install decides (the same Embed checks), changing nothing."""

    def check(self, *, ubuntu=True, kvm=True, vmx=True, virt='none', mem_mib=65536, disk_gib=300, listener='',
              installed=''):
        text = INSTALL.read_text()
        start = text.index('check_report() {')
        report = text[start:text.index('\n}\n', start) + 3]
        decide = text[text.index("# Why E2B Embed can't run"):text.index('# E2B Cloud: with its key')]
        enables = text[text.index('enables() {'):text.index('\n', text.index('enables() {')) + 1]
        facts = {'os': 'Ubuntu 24.04.1 LTS' if ubuntu else 'Debian GNU/Linux 12', 'ubuntu': '1' if ubuntu else '',
                 'kvm': '1' if kvm else '', 'tun': '1', 'vmx': '1' if vmx else '', 'mem_mib': str(mem_mib),
                 'disk_gib': str(disk_gib), 'hugepages': '', 'installed': installed,
                 'has_env': '1' if installed else '', 'check': '1'}
        script = ('set -eu\n' + ''.join(f'{k}={shlex.quote(v)}\n' for k, v in facts.items())
                  + "say() { printf '\\n==> %s\\n' \"$*\"; }\ndie() { echo \"FAILED: $*\" >&2; exit 1; }\n"
                  + f"configured() {{ {'true' if installed else 'false'}; }}\n"
                  + report + enables + decide + 'check_report\n')
        with tempfile.TemporaryDirectory(prefix='sbx-install-check-') as directory:
            for name, body in CHECK_TOOLS.items():
                executable(Path(directory) / name, body)
            return subprocess.run(['sh', '-c', script], capture_output=True, text=True, timeout=10,
                                  env={**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                                       'SBX_TEST_VIRT': virt, 'SBX_TEST_LISTENER': listener})

    def test_a_kvm_host_with_room_runs_embed_and_the_providers(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('machine:    bare metal, 8 CPUs, hardware virtualization', result.stdout)
        self.assertIn('/dev/kvm:   yes', result.stdout)
        self.assertIn('public IP:  203.0.113.10', result.stdout)
        self.assertIn('embed      yes', result.stdout)
        self.assertIn('e2b-cloud  yes: needs an E2B API key and a domain', result.stdout)
        self.assertIn('boat       yes: needs a Boat API key (a paid plan', result.stdout)
        self.assertIn('verdict:    embed', result.stdout)

    def test_without_kvm_it_says_why_and_suggests_a_provider(self):
        result = self.check(kvm=False, vmx=False, virt='kvm', mem_mib=4096, disk_gib=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('a VM without nested virtualization', result.stdout)
        self.assertIn('embed      no: no /dev/kvm', result.stdout)
        self.assertIn('verdict:    a provider runtime', result.stdout)
        result = self.check(kvm=False, vmx=True)
        self.assertIn('load its module (modprobe kvm_intel or kvm_amd)', result.stdout)

    def test_embed_takes_the_installs_thresholds(self):
        result = self.check(mem_mib=16384)
        self.assertIn('embed      no: the default budget needs 24 GiB RAM', result.stdout)
        result = self.check(disk_gib=50)
        self.assertIn('embed      no: 60 GiB of free disk at least, this has 50 GiB', result.stdout)

    def test_a_host_sbx_cant_install_on_exits_1(self):
        for options, said in (({'ubuntu': False}, 'Debian GNU/Linux 12: sbx needs Ubuntu 24.04'),
                              ({'kvm': False, 'mem_mib': 1024}, 'e2b-cloud  no: 2 GiB of memory at least')):
            with self.subTest(said=said):
                result = self.check(**options)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(said, result.stdout)
                self.assertIn("verdict:    sbx can't be installed on this host", result.stdout)

    def test_ports_in_use_name_their_process(self):
        result = self.check(listener='443:nginx')
        self.assertIn('port 443:   in use by nginx on 0.0.0.0:443', result.stdout)
        self.assertIn('port 80:    free', result.stdout)

    def test_an_installed_embed_host_that_no_longer_fits_is_reported_not_stopped_at(self):
        result = self.check(disk_gib=12, installed='embed')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn('FAILED', result.stderr)
        self.assertIn('sbx:        installed (runtimes: embed)', result.stdout)
        self.assertIn('embed      no: 15 GiB of free disk at least, this has 12 GiB', result.stdout)
        self.assertIn('e2b-cloud  yes', result.stdout)
        self.assertIn('verdict:    the upgrade refuses: this installation enables embed', result.stdout)

    def test_an_enabled_provider_is_said_to_be_enabled_not_to_need_a_key(self):
        result = self.check(kvm=False, vmx=False, virt='kvm', installed='e2b-cloud')
        self.assertIn('e2b-cloud  yes: enabled', result.stdout)
        self.assertNotIn('needs an E2B API key', result.stdout)
        self.assertIn('boat       yes: needs a Boat API key', result.stdout)

    def test_an_installed_host_gets_the_upgrade_verdict_not_the_first_installs(self):
        result = self.check(kvm=False, vmx=False, virt='kvm', installed='e2b-cloud boat')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('verdict:    installed with e2b-cloud boat; install.sh upgrades it', result.stdout)
        self.assertNotIn("the owner's API key", result.stdout)
        result = self.check(installed='unknown')
        self.assertIn('verdict:    installed; install.sh upgrades it', result.stdout)

    def inventory(self, *, uid, reader=True):
        """install.sh's read of the installed runtimes, as --check runs it, against a root-only runtime.env."""
        text = INSTALL.read_text()
        block = text[text.index('installed="" has_env=""'):text.index('enables() {')]
        with tempfile.TemporaryDirectory(prefix='sbx-install-check-') as directory:
            etc, src = Path(directory) / 'etc-sbx', Path(directory) / 'src'
            etc.mkdir()
            (etc / 'runtime.env').write_text("SBX_RUNTIMES='e2b-cloud'\n")
            (etc / 'runtime.env').chmod(0)
            if reader:
                target = src / 'control/sbx_control/runtime/env_file.py'
                target.parent.mkdir(parents=True)
                shutil.copy(Path(__file__).parents[1] / 'sbx_control/runtime/env_file.py', target)
            executable(Path(directory) / 'bin/id', f'print({uid})\n')
            script = ('set -eu\ncheck=1 src=' + shlex.quote(str(src)) + '\n'
                      + "die() { echo \"FAILED: $*\" >&2; exit 1; }\nconfigured() { true; }\n"
                      + block.replace('/etc/sbx', str(etc)) + 'echo "installed=$installed"\n')
            try:
                return subprocess.run(['sh', '-c', script], capture_output=True, text=True, timeout=10,
                                      env={**os.environ, 'PATH': f"{directory}/bin{os.pathsep}{os.environ['PATH']}"})
            finally:
                (etc / 'runtime.env').chmod(0o600)

    def test_the_check_reports_unknown_runtimes_where_it_cannot_read_runtime_env(self):
        for options in ({'uid': 1000}, {'uid': 0, 'reader': False}):
            with self.subTest(**options):
                result = self.inventory(**options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('installed=unknown', result.stdout)

    def test_the_script_stops_after_the_check_without_root(self):
        with tempfile.TemporaryDirectory(prefix='sbx-install-check-') as directory:
            for name, body in CHECK_TOOLS.items():
                executable(Path(directory) / name, body)
            result = subprocess.run(['sh', str(INSTALL), '--check'], capture_output=True, text=True, timeout=30,
                                    env={**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                                         'SBX_TEST_VIRT': 'none'})
        self.assertIn(result.returncode, (0, 1), result.stderr)
        self.assertIn('Host check (nothing is changed)', result.stdout)
        self.assertIn('runtimes this host can run:', result.stdout)
        self.assertNotIn('run as root', result.stderr)
        self.assertNotIn('Packages and Docker', result.stdout)


class SetupInferTests(Installation):
    """What install-report.sh and host-install.sh read: the exit status."""

    def infer(self):
        from sbx_control import admin
        with open(os.devnull, 'w') as out, contextlib.redirect_stdout(out):
            return admin.main(['setup-infer'])

    def test_open_until_configured_then_set_up(self):
        from sbx_control import settings, setup, users
        self.assertEqual(self.infer(), 3)
        settings.update('domain', {'address': '203.0.113.10'}, by='test')
        settings.update('access', {'admins': ['operator@example.com']}, by='test')
        users.set_password('operator@example.com', 'test-only-password', by='test')
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
