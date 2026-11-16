"""Sandboxes, recipes, previews, the host network and templates through the control API."""
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import Server


def done(stdout=""):
    return SimpleNamespace(stdout=stdout, stderr="", exit_code=0)


class VM:
    """An E2B sandbox that keeps, in order, the files and commands it is sent.
    answer(command) is a command's stdout, or None for the usual: its tmpfs
    still holds the env, every health URL answers 200, the rest prints nothing."""

    def __init__(self, sandbox_id="fixture-vm", answer=lambda command: None):
        self.sandbox_id, self.answer, self.log = sandbox_id, answer, []
        self.commands = self
        self.files = SimpleNamespace(write=lambda path, content, **_: self.log.append(("write", path, content)))

    def run(self, command, **_):
        from sbx_control import platform
        self.log.append(("run", command, None))
        if self.answer(command) is not None:
            return done(self.answer(command))
        if command == platform.STARTED:
            return done("ready")
        if command.startswith("mktemp -d "):
            return done(command.split()[-1].replace("XXXXXX", "staged"))
        if command.startswith("test -f /run/sbx/env"):
            return done("ok")
        if "curl " in command:
            return done(" ".join(["200"] * command.count("curl ")))
        return done()

    def shipped_before_started(self, script: str) -> bool:
        """Whether the recipe's start script reached the VM before the app was started."""
        shipped = next(i for i, (kind, path, content) in enumerate(self.log)
                       if kind == "write" and path.endswith("/start") and content == script.encode())
        started = next(i for i, (kind, command, _) in enumerate(self.log) if kind == "run" and '"$d/start"' in command)
        return shipped < started


class WorkspaceTests(Server):
    def setUp(self):
        super().setUp()
        from sbx_control import paths, settings, setup, state, variables, vault
        self.paths, self.settings, self.setup, self.state, self.variables, self.vault = (
            paths, settings, setup, state, variables, vault)

    def test_app_recipe_replacement_is_visible_without_restarting_control(self):
        from sbx_control import recipes, repo_onboarding
        self.repo_admin()
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'recipe';source.mkdir()
            (source/'sbx.yaml').write_text('previews: {web: 3000}\n')
            with patch('sbx_control.paths.apps', return_value=Path(directory)/'apps'), patch('subprocess.run') as run:
                repo_onboarding.apply_recipe('owner/app', source, by='test')
                self.assertEqual(recipes.load('owner/app').spec['previews']['web'], 3000)
                (source/'sbx.yaml').write_text('previews: {web: 4000}\n')
                repo_onboarding.apply_recipe('owner/app', source, by='test')
                self.assertEqual(recipes.load('owner/app').spec['previews']['web'], 4000)
                run.assert_not_called()

    def test_operator_recipe_scripts_override_the_checkout_but_imported_recipes_allow_branch_scripts(self):
        from sbx_control import core, recipes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed, checkout = root / 'installed', root / 'checkout'
            installed.mkdir()
            (checkout / '.sbx').mkdir(parents=True)
            for folder, marker in ((installed, 'installed'), (checkout / '.sbx', 'checkout')):
                script = folder / 'setup'
                script.write_text(f'#!/bin/sh\nprintf {marker}')
                script.chmod(0o700)
            def run(_, command, **kwargs):
                return subprocess.run(['/bin/bash', '-c', command], capture_output=True, text=True, check=True)
            operator_root = root / 'operator'
            with patch('sbx_control.paths.apps', return_value=operator_root), \
                    patch.object(core, 'APP_SCRIPTS', str(installed)), patch.object(core, 'run_in', side_effect=run):
                for source, expected in ((operator_root / 'owner/app/.sbx', 'installed'),
                                         (root / 'imported/owner/app/.sbx', 'checkout')):
                    with self.subTest(source=source):
                        app = recipes.App('owner/app', source, {'workdir': str(checkout)})
                        self.assertEqual(core.app_script(Mock(), 'setup', app).stdout, expected)

    def test_resume_restart_policy_applies_only_to_a_resumed_workspace(self):
        from sbx_control import core, recipes
        sandbox=Mock();sandbox.commands.run.return_value.stdout='ok'
        for mode,vm,expected in (('restart','paused',True),('restart','running',False),('preserve','paused',False)):
            recipe=recipes.App('owner/app',Path('/unused'),{'lifecycle':{'resume':mode}})
            with patch.object(core,'app_of',return_value=recipe):
                self.assertEqual(core.restore_runtime(sandbox,Mock(state=vm)),expected)
        with self.assertRaisesRegex(recipes.RecipeError,'lifecycle.resume'):
            recipes.App('owner/app',Path('/unused'),{'lifecycle':{'resume':'unknown'}}).resume_mode

    START = "#!/bin/sh\necho fixture-start\n"

    def resumable(self, name, vm):
        """A paused workspace of owner/app, whose recipe restarts the app on resume."""
        apps = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("sbx_control.paths.apps", return_value=apps))
        (apps / "owner/app/.sbx").mkdir(parents=True)
        (apps / "owner/app/.sbx/sbx.yaml").write_text(
            "lifecycle: {resume: restart}\nhealth: ['http://localhost:3000/']\n")
        (apps / "owner/app/.sbx/start").write_text(self.START)
        self.prepared_workspace(name, vm.sandbox_id)
        return Mock(sandbox_id=vm.sandbox_id, state="paused",
                    metadata={"sbx": name, "owner": "operator@example.com", "repo": "owner/app", "branch": "main"})

    def test_resume_ships_recipe_before_restarting_the_app(self):
        from sbx_control import core, lifecycle
        self.repo_admin()
        with self.state.db() as conn:
            conn.execute("insert or replace into sandboxes (name,owner,branch) "
                         "values ('test-resume','operator@example.com','main')")
        vm = VM("resume-vm")
        info = self.resumable("test-resume", vm)
        with patch.object(core, "find", return_value=info), patch.object(core.runtime, "connect", return_value=vm), \
                patch.object(core.time, "sleep"):
            response = self.client.post("/api/sandboxes/test-resume/resume")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(vm.shipped_before_started(self.START))
        self.assertEqual(lifecycle.get("test-resume")["phase"], "app_ready")

    def test_preview_resume_restarts_the_app_before_forwarding(self):
        from sbx_control import core, gate
        vm = VM("preview-vm")
        info = self.resumable("preview-fixture", vm)
        self.addCleanup(gate.forget_preview_resume, "preview-vm")
        with patch.object(core, "sandboxes", return_value=[info]), patch.object(core, "find", return_value=info), \
                patch.object(core.runtime, "connect", return_value=vm), patch.object(core.time, "sleep"):
            gate.ensure_running("preview-vm")
        self.assertTrue(gate.resumed_for_preview("preview-vm"))
        self.assertTrue(vm.shipped_before_started(self.START))

    def test_resource_probe_does_not_resume_a_paused_workspace(self):
        from sbx_control import core, recipes, resources
        info = Mock(sandbox_id='test-vm')
        actual = Mock(state='paused', cpu_count=2, memory_mb=4096, template_id='small')
        recipe = recipes.App('owner/app', Path('/unused'), {'resources': {'cpu': 4, 'memory_mb': 8192}})
        with patch.object(core, 'find', return_value=info), patch.object(core, 'app_of', return_value=recipe), \
                patch.object(core.runtime, 'info', return_value=actual), \
                patch.object(core.runtime, 'connect') as connect, patch.object(core, 'capacity', return_value={}):
            result = resources.snapshot('test-vm', live=True)
        connect.assert_not_called()
        self.assertIsNone(result['usage'])
        self.assertEqual(result['allocated']['memory_mb'], 4096)
        self.assertEqual(result['recipe']['memory_mb'], 8192)
        self.assertEqual(sum(issue['code'] == 'allocation_below_recipe' for issue in result['issues']), 2)

    def test_resource_probe_reports_actual_disk_pressure_and_ignored_request(self):
        import json
        from sbx_control import core, recipes, resources
        actual = Mock(state='running', cpu_count=4, memory_mb=8192, template_id='test')
        recipe = recipes.App('owner/app', Path('/unused'), {'resources': {'disk_mb': 32768}})
        runtime = Mock()
        runtime.commands.run.return_value.stdout = json.dumps({'disk': {'total_bytes': 20 * 1024**3,
                                                                         'available_bytes': 300 * 1024**2}})
        with patch.object(core, 'find', return_value=Mock(sandbox_id='test-vm')), \
                patch.object(core, 'app_of', return_value=recipe), patch.object(core.runtime, 'info', return_value=actual), \
                patch.object(core.runtime, 'connect', return_value=runtime), patch.object(core, 'capacity', return_value={}):
            result = resources.snapshot('test-vm', live=True)
        self.assertEqual({issue['code'] for issue in result['issues']}, {'low_disk_space', 'disk_request_not_applied'})

    def test_resource_request_matches_the_builders_effective_override(self):
        from sbx_control import core, recipes, resources
        recipe = recipes.App('owner/app', Path('/unused'), {
            'resources': {'cpu': 2, 'memory_mb': 4096, 'swap_mb': 2048},
            'build_resources': {'cpu': 4, 'memory_mb': 16384, 'disk_mb': 8192},
        })
        with patch.object(core, 'find', return_value=Mock(sandbox_id='vm')), \
                patch.object(core, 'app_of', return_value=recipe), patch.object(core, 'capacity', return_value={}), \
                patch.object(core.runtime, 'info', return_value=Mock(state='running', cpu_count=4, memory_mb=8192, template_id='test')):
            result = resources.snapshot('test')
        self.assertEqual(result['recipe']['allocation_source'], 'build_resources')
        self.assertEqual(result['recipe']['memory_mb'], 16384)
        self.assertEqual(result['recipe']['swap_mb'], 2048)
        self.assertEqual(result['issues'][0]['resource'], 'memory_mb')
        self.assertIn('Allocated: 4 vCPU, 8 GiB RAM.', result['agent_guidance'])
        self.assertIn('smaller than the current recipe', result['agent_guidance'])

    def test_invalid_swap_budget_is_rejected_before_touching_the_workspace(self):
        from sbx_control import core, recipes
        for value in (True, '2048', 0, 65537):
            sandbox = Mock()
            with self.assertRaises(recipes.RecipeError):
                core.apply_resources(sandbox, recipes.App('owner/app', Path('/unused'), {'resources': {'swap_mb': value}}))
            sandbox.files.write.assert_not_called()
            sandbox.commands.run.assert_not_called()

    def test_resource_endpoint_hides_other_owners_workspaces(self):
        from sbx_control import core, resources
        self.repo_admin()
        self.settings.update('access', {'admins': ['someone-else@example.com']}, by='test')
        with patch.object(core, 'find', return_value=Mock(metadata={'owner': 'other@example.com'})), \
                patch.object(resources, 'snapshot') as snapshot:
            response = self.client.get('/api/sandboxes/test-vm/resources?probe=true')
        self.assertEqual(response.status_code, 404)
        snapshot.assert_not_called()

    def test_pause_refuses_an_incomplete_setup(self):
        from sbx_control import core, lifecycle
        self.repo_admin()
        with self.state.db() as conn:
            conn.execute("insert or replace into sandboxes (name, owner, branch, slug) values ('test-pause', 'operator@example.com', 'main', 'test-pause')")
        lifecycle.begin('test-pause', 'prepare')
        info = Mock(sandbox_id='vm', metadata={'sbx': 'test-pause', 'owner': 'operator@example.com'})
        with patch.object(core, 'find', return_value=info), patch.object(core.runtime, 'pause') as pause:
            response = self.client.post('/api/sandboxes/test-pause/pause')
        self.assertEqual(response.status_code, 409)
        pause.assert_not_called()

    def prepared_workspace(self, name, vm):
        from sbx_control import lifecycle
        operation = lifecycle.begin(name, 'create')
        for phase in ('provisioning_services', 'services_ready', 'allocating'):
            operation.advance(phase)
        operation.advance('preparing_workspace', sandbox_id=vm, runtime='running')
        operation.advance('workspace_ready', finish=True)

    def test_dead_process_cannot_pass_app_readiness_even_with_healthy_http(self):
        from sbx_control import core, recipes
        app = recipes.App('owner/app', Path('/unused'), {'health': ['http://localhost:3000/']})
        # The start script prints its main process's pid, 42, which has exited when the app is probed.
        vm = VM(answer=lambda command: '42' if '"$d/start"' in command else '200 exited' if 'kill -0 42' in command else None)
        with patch.object(core.time, 'sleep'), self.assertRaises(core.SandboxError):
            core.start_app(vm, app)

    def test_app_readiness_requires_a_response_from_every_health_url(self):
        from sbx_control import core, recipes
        app = recipes.App('owner/app', Path('/unused'), {'health': ['http://localhost:3000/', 'http://localhost:3001/']})
        vm = VM(answer=lambda command: '200' if 'curl ' in command else None)  # one code for two URLs
        with patch.object(core.time, 'sleep'), patch.object(core.time, 'time', side_effect=[0, 0, 241]):
            with self.assertRaises(core.SandboxError):
                core.start_app(vm, app)

    def test_unverified_workspace_is_not_reported_as_an_app_being_up(self):
        from sbx_control import lifecycle
        finished = {'operation': 'prepare', 'active': False, 'services_ready': True, 'runtime': 'running'}
        self.assertEqual(lifecycle.status({**finished, 'phase': 'app_unverified', 'application': 'unverified'}), 'workspace_ready')
        self.assertEqual(lifecycle.status({**finished, 'phase': 'app_ready', 'application': 'healthy'}), 'up')
        self.assertEqual(lifecycle.started(False), 'app_unverified')

    def test_domainless_workspace_never_advertises_a_malformed_preview_url(self):
        self.choose_ip()
        from sbx_control import addressing, core, recipes
        app = recipes.App('owner/app', Path('/unused'), {'previews': {'': 3000}})
        with patch('sbx_control.ip_previews.address', return_value=''):
            self.assertEqual(addressing.urls('litmus-app', app), {})
        links = addressing.urls('litmus-app', app)
        self.assertRegex(links['web'], r'https://203\.0\.113\.10:21\d{3}$')
        env = core.sbx_env('litmus-app', 'litmus-app', app)
        self.assertEqual(env['SBX_URL'], links['web'])
        self.assertEqual(env['SBX_HOST_SUFFIX'], '')
        self.assertEqual(addressing.vite_allowed_hosts(), '.e2b.sandboxes.internal')
        self.settings.update('domain', {**self.settings.current().domain.model_dump(), 'name': 'sbx.example.com'}, by='test')
        self.assertEqual(addressing.urls('litmus-app', app)['web'], 'https://litmus-app.sbx.example.com')
        self.assertEqual(addressing.vite_allowed_hosts(), '.sbx.example.com')

    def test_ip_preview_gate_enforces_owner_strips_control_credentials_and_resumes(self):
        from urllib.parse import urlsplit
        from sbx_control import auth, core, gate, ip_previews, recipes, routing
        self.repo_admin()
        app = recipes.App('owner/app', Path('/unused'), {'previews': {'': {'port': 3000, 'access': 'owner'}}})
        info = Mock(sandbox_id='vm-fixture', state='paused', metadata={
            'sbx': 'ip-gate-fixture', 'slug': 'ip-gate-fixture', 'owner': 'operator@example.com', 'repo': 'owner/app'})
        owner = auth.mint_token('operator@example.com', 'test')
        other = auth.mint_token('other@example.com', 'test')
        self.addCleanup(gate.forget_preview_resume, 'vm-fixture')
        url = ip_previews.origin('ip-gate-fixture', 3000)
        headers = {'X-Original-Preview-Port': str(urlsplit(url).port), 'X-Original-Host': '203.0.113.10',
                   'X-Original-URI': '/login?one=1&two=2'}
        self.client.cookies.clear()
        response = self.client.get('/gate', headers=headers)
        self.assertEqual(response.status_code, 401)
        self.assertIn('%3Fone%3D1%26two%3D2', response.headers['x-sbx-login'])
        self.assertEqual(self.client.get('/auth/login', params={'next': url + '/login'},
                                        follow_redirects=False).status_code, 307)
        with patch.object(core, 'sandboxes', return_value=[info]), patch.object(core, 'app_of', return_value=app), \
                patch.object(core, 'attach') as resume:
            routing.slugs(fresh=True)
            response = self.client.get('/gate', headers={**headers, 'Authorization': f'Bearer {other}'})
            self.assertEqual(response.status_code, 403)
            response = self.client.get('/gate', headers={**headers, 'X-Original-Method': 'OPTIONS'})
            self.assertEqual(response.status_code, 200)
            resume.assert_not_called()
            response = self.client.get('/gate', headers={**headers, 'Authorization': f'Bearer {owner}',
                'Cookie': 'app=a; sbx_session=first; other=b; sbx_session=second'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers['x-sbx-upstream'], '3000-vm-fixture.e2b.sandboxes.internal')
            self.assertEqual(response.headers['x-sbx-cookies'], 'app=a; other=b')
            self.assertEqual(response.headers['x-sbx-auth'], 'bearer')
            resume.assert_called_once()
        ip_previews.remove('ip-gate-fixture')
        response = self.client.get('/gate', headers=headers)
        self.assertEqual((response.status_code, response.headers.get('x-sbx-gone')), (403, '1'))

    def test_a_deleted_sandbox_s_preview_is_gone(self):
        from sbx_control import auth, core, lifecycle, recipes
        self.repo_admin()
        app = recipes.App('owner/app', Path('/unused'), {'previews': {'': 3000}})
        info = Mock(sandbox_id='gone-vm', state='running',
                    metadata={'sbx': 'gone-fixture', 'owner': 'operator@example.com', 'repo': 'owner/app'})
        op = lifecycle.begin('gone-fixture', 'create')
        op.advance('allocating')
        op.advance('preparing_workspace', sandbox_id='gone-vm', runtime='running')
        op.advance('workspace_ready', finish=True)
        from sbx_control.runtime import embed
        headers = {'X-Original-Host': embed.dashboard_host(), 'X-Original-Sandbox-Id': 'gone-vm',
                   'X-Original-Sandbox-Port': '3000',
                   'Authorization': f"Bearer {auth.mint_token('operator@example.com', 'test')}"}
        with patch.object(core, 'sandboxes', return_value=[info]), patch.object(core, 'app_of', return_value=app):
            self.assertEqual(self.client.get('/gate', headers=headers).status_code, 200)
            with patch.object(core, 'find', return_value=info), patch.object(core.runtime, 'kill'), \
                    patch('sbx_control.catalog.resources.delete_all', return_value=[]):
                core.delete('gone-fixture')
            response = self.client.get('/gate', headers=headers)  # the runtime still lists the VM
        self.assertEqual((response.status_code, response.headers.get('x-sbx-gone')), (403, '1'))

    def test_preview_origin_cannot_mutate_the_control_plane_using_its_shared_host_cookie(self):
        from sbx_control import sandboxes
        self.repo_admin()
        with patch.object(sandboxes, 'restart') as restart:
            response = self.client.post('/api/sandboxes/fixture/restart',
                                        headers={'Origin': 'https://203.0.113.10:21000'})
        self.assertEqual(response.status_code, 403)
        restart.assert_not_called()

    def test_deletion_retires_the_sandbox_s_ip_preview_links(self):
        from sbx_control import core, ip_previews, recipes
        self.repo_admin()
        self.settings.add_repo('owner/ipapp', by='test')
        app = recipes.App('owner/ipapp', Path('/unused'), {'previews': {'': 3000}})
        name = 'ip-delete-fixture'
        url = ip_previews.origin(name, 3000)
        info = Mock(sandbox_id='delete-fixture-vm', metadata={'sbx': name, 'slug': name, 'repo': app.repo})
        with patch.object(core, 'find', return_value=info), patch.object(recipes, 'load', return_value=app), \
                patch.object(core.runtime, 'kill') as kill, patch('sbx_control.catalog.resources.delete_all', return_value=[]):
            core.delete(name)
        kill.assert_called_once_with('delete-fixture-vm')
        from urllib.parse import urlsplit
        self.assertIsNone(ip_previews.route(urlsplit(url).port))
        self.assertEqual(ip_previews.origin(name, 3000), '')

    def embed_env(self, text):
        """Embed's env file, with a marker that its network change is pending."""
        from sbx_control.runtime.embed import host
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = folder / '.env'
        env.write_text(text)
        self.enterContext(patch.object(host, 'EMBED', folder))
        marker = self.root / 'run/apply/runtime-network'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        self.addCleanup(marker.unlink, missing_ok=True)
        return env, marker

    @staticmethod
    def restarted_embed(run) -> bool:
        return any(call.args[0][:3] == ['docker', 'compose', 'up'] for call in run.call_args_list)

    def test_network_change_cannot_restart_a_runtime_with_existing_workspaces(self):
        from sbx_control import runtime
        from sbx_control.runtime.embed import network as host_network
        original = 'OTHER_SETTING=preserved\nALLOW_SANDBOX_INTERNAL_CIDRS=172.29.20.0/24\n'
        env, marker = self.embed_env(original)
        with patch.object(host_network.core, 'sandboxes', return_value=[Mock()]), \
                patch.object(host_network.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'before creating sandboxes'):
                runtime.apply_network()
        self.assertFalse(self.restarted_embed(run))
        self.assertEqual(env.read_text(), original)
        self.assertTrue(marker.exists())

    def test_network_apply_restarts_embed_with_the_new_ranges_and_clears_the_pending_marker(self):
        from sbx_control import runtime
        from sbx_control.runtime.embed import network as host_network
        network = self.settings.current().network.model_dump()
        self.settings.update('network', {**network, 'internal_cidrs': ['172.29.50.0/24']}, by='test')
        env, marker = self.embed_env('OTHER_SETTING=preserved\nALLOW_SANDBOX_INTERNAL_CIDRS=\n')
        with patch.object(host_network.core, 'sandboxes', return_value=[]), \
                patch.object(host_network.subprocess, 'run') as run:
            result = runtime.apply_network()
        self.assertEqual(result['status'], 'applied')
        self.assertTrue(self.restarted_embed(run))
        self.assertFalse(marker.exists())
        self.assertIn('OTHER_SETTING=preserved\n', env.read_text())
        self.assertIn('ALLOW_SANDBOX_INTERNAL_CIDRS=172.29.50.0/24\n', env.read_text())

    def test_network_apply_clears_the_pending_marker_when_configuration_is_current(self):
        from sbx_control import runtime
        from sbx_control.runtime.embed import network as host_network
        network = self.settings.current().network.model_dump()
        self.settings.update('network', {**network, 'internal_cidrs': ['172.29.50.0/24']}, by='test')
        from sbx_control.runtime.embed import host
        original = host.render_embed_env('OTHER_SETTING=preserved\n', host.embed_env())
        self.assertIn('ALLOW_SANDBOX_INTERNAL_CIDRS=172.29.50.0/24\n', original)
        env, marker = self.embed_env(original)
        with patch.object(host_network.subprocess, 'run') as run:
            result = runtime.apply_network()
        self.assertEqual(result['status'], 'unchanged')
        self.assertFalse(self.restarted_embed(run))
        self.assertFalse(marker.exists())
        self.assertEqual(env.read_text(), original)

    def managed_server(self, name='shared-pg'):
        from sbx_control import secret_store
        with self.state.db() as conn:
            conn.execute("insert into managed_servers (name, kind, config) values (?, 'postgres', ?)",
                         (name, secret_store.seal({}, key=f'managed-server/{name}')))

    def test_managed_servers_network_is_reached_beside_the_operators_ranges_whatever_they_are_set_to(self):
        from sbx_control import hosted_services
        from sbx_control.runtime.embed import host, network as host_network
        self.settings.update('network', {'internal_cidrs': ['10.0.0.0/24']}, by='test')
        self.assertEqual(host.embed_env()['ALLOW_SANDBOX_INTERNAL_CIDRS'], '10.0.0.0/24')
        self.managed_server()
        self.assertEqual(host.embed_env()['ALLOW_SANDBOX_INTERNAL_CIDRS'], f'10.0.0.0/24,{hosted_services.CIDR}')
        # What install.sh --internal or sbx-admin config set writes replaces the operator's list only.
        self.settings.update('network', {'internal_cidrs': ['10.9.0.0/24']}, by='test')
        self.assertEqual(hosted_services.allowed_cidrs(), ['10.9.0.0/24', hosted_services.CIDR])
        self.settings.update('network', {'internal_cidrs': []}, by='test')
        with patch.object(host_network.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            host_network.apply_routes()
        accepted = [c.args[0] for c in run.call_args_list if c.args[0][:3] == ['iptables', '-w', '-A']]
        self.assertIn(['iptables', '-w', '-A', 'SBX-SERVICES', '-i', 'veth-+', '-d', hosted_services.CIDR,
                       '-p', 'tcp', '-j', 'ACCEPT'], accepted)

    def test_public_repos_and_managed_servers_exclude_each_other(self):
        from sbx_control import hosted_services
        self.managed_server()
        with self.assertRaisesRegex(self.settings.Invalid, 'managed servers'):
            self.settings.update('access', {'public_repos': True}, by='test')
        with self.state.db() as conn:
            conn.execute('delete from managed_servers')
        self.settings.update('access', {'public_repos': True}, by='test')
        with self.assertRaisesRegex(ValueError, 'public_repos'):
            hosted_services.up('shared-ch', 'clickhouse')

    def test_a_runtime_whose_sandboxes_cant_reach_this_host_gets_no_managed_server(self):
        from sbx_control import hosted_services, runtime
        with patch.object(runtime, 'capabilities', return_value=runtime.Capabilities()), \
                patch.object(hosted_services, 'docker') as docker, \
                self.assertRaisesRegex(ValueError, "can't reach this host's private network"):
            hosted_services.up('shared-ch', 'clickhouse')
        docker.assert_not_called()

    def test_server_up_writes_no_network_setting_and_asks_the_host_to_apply_its_network(self):
        import json
        from sbx_control import hosted_services
        from sbx_control.catalog import connections
        revision = self.settings.update('network', {'internal_cidrs': ['10.0.0.0/24']}, by='test').revision
        self.effects()
        network = {'Labels': {hosted_services.LABEL: 'true'},
                   'IPAM': {'Config': [{'Subnet': hosted_services.CIDR}]}, 'Containers': {}}

        def docker(*args, **kwargs):
            if args[:2] == ('network', 'inspect'):
                return Mock(returncode=0, stdout=json.dumps([network]))
            return Mock(returncode=1 if 'inspect' in args else 0, stdout='')
        with patch.object(hosted_services, 'docker', side_effect=docker), patch.object(connections, 'save'):
            result = hosted_services.up('shared-ch', 'clickhouse')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(self.settings.current().network.internal_cidrs, ('10.0.0.0/24',))
        self.assertEqual(self.settings.current().revision, revision)
        self.assertEqual(self.effects(), ['host-guard', 'runtime-network'])
        self.assertEqual(hosted_services.networks(), [hosted_services.CIDR])

    def test_base64_encryption_key_is_saved_per_sandbox_and_has_requested_byte_length(self):
        import base64
        first = self.variables.random_value('test-base64-a', 'ENCRYPTION_KEY', False, 32, base64_=True)
        again = self.variables.random_value('test-base64-a', 'ENCRYPTION_KEY', False, 32, base64_=True)
        other = self.variables.random_value('test-base64-b', 'ENCRYPTION_KEY', False, 32, base64_=True)
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual(len(base64.b64decode(first, validate=True)), 32)

    def test_repo_inspection_accepts_github_urls_without_mutating_settings(self):
        from sbx_control import repo_onboarding
        self.repo_admin()
        response = Mock(status_code=200)
        response.json.return_value = {'description': 'Sample app', 'default_branch': 'develop', 'private': False}
        with patch.object(repo_onboarding.github_app, 'settings', return_value={}), patch.object(repo_onboarding.httpx, 'get', return_value=response) as get:
            result = self.client.post('/api/repo-onboarding/inspect', json={'repo': 'https://github.com/Owner/App.git/'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['repo'], 'owner/app')
        self.assertEqual(result.json()['branch'], 'develop')
        self.assertEqual(self.settings.current().repos, ())
        self.assertEqual(get.call_args.args[0], 'https://api.github.com/repos/owner/app')
        for value in ('https://evil.example/owner/app', 'https://github.com/owner/app?token=x', '../etc/passwd', 'owner/*', 'owner/..'):
            result = self.client.post('/api/repo-onboarding/inspect', json={'repo': value})
            self.assertEqual(result.status_code, 400, value)

    def test_github_picker_lists_accessible_repositories_without_secrets(self):
        import httpx
        from sbx_control import github_app
        self.repo_admin()
        def handle(request):
            path = request.url.path
            if path == '/app/installations':
                return httpx.Response(200, json=[{'id': 1}, {'id': 2, 'suspended_at': '2026-01-01'}])
            if path == '/app/installations/1/access_tokens':
                self.assertEqual(__import__('json').loads(request.content), {'permissions': {'metadata': 'read'}})
                return httpx.Response(201, json={'token': 'private-test-token'})
            if path == '/installation/repositories':
                page = int(request.url.params['page'])
                repos = [{'full_name': f'Owner/Repo{i}', 'private': True} for i in range(100)] if page == 1 else [
                    {'full_name': 'Other/Archived', 'archived': True}, {'full_name': 'Other/Disabled', 'disabled': True}]
                return httpx.Response(200, json={'repositories': repos})
            raise AssertionError(path)
        client = httpx.Client(transport=httpx.MockTransport(handle))
        with patch.object(github_app, 'settings', return_value={'id': '1'}), patch.object(github_app, 'app_headers', return_value={'Authorization': 'Bearer test-jwt'}), patch.object(github_app.httpx, 'Client', return_value=client):
            response = self.client.get('/api/github/repositories')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['repositories']), 101)
        self.assertNotIn('private-test-token', response.text)
        self.assertNotIn('test-jwt', response.text)
        self.assertEqual(self.settings.current().repos, ())

    def test_github_picker_requires_admin_and_sanitizes_upstream_errors(self):
        import httpx
        from sbx_control import github_app
        self.repo_admin()
        request = httpx.Request('GET', 'https://api.github.com/app/installations')
        error = httpx.HTTPStatusError('sensitive upstream details', request=request, response=httpx.Response(403, request=request))
        with patch.object(github_app, 'repositories', side_effect=error):
            response = self.client.get('/api/github/repositories')
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('sensitive upstream details', response.text)
        self.settings.update('access', {'admins': ['other@example.com']}, by='test')
        with patch.object(github_app, 'repositories') as listing:
            self.assertEqual(self.client.get('/api/github/repositories').status_code, 403)
            listing.assert_not_called()

    def test_connect_repo_appends_and_retries_without_replacing_existing_repos(self):
        self.repo_admin()
        self.settings.set_repos(['other/existing'], by='test')
        before = self.settings.current().revision
        with patch('sbx_control.repo_onboarding.inspect_repo', return_value={'repo': 'owner/app', 'registered': False}):
            for _ in range(2):
                result = self.client.post('/api/repo-onboarding/connect', json={'repo': 'owner/app'})
                self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.settings.current().repos, ('other/existing', 'owner/app'))
        self.assertEqual(self.settings.current().revision, before + 1)

    def test_preparing_recipe_reads_metadata_without_building_or_executing_code(self):
        import base64
        from sbx_control import recipes, repo_onboarding
        self.repo_admin()
        self.settings.add_repo('owner/app', by='test')
        raw = 'template: owner-app\nservices:\n  db:\n    uses: neon-branch\nsecrets: [EXTERNAL_KEY]\n'
        files = {'.sbx/sbx.yaml': raw, '.sbx/setup': '#!/bin/sh\necho prepared\n', '.sbx/config/app.json': '{}', '.sbx/stack': '#!/bin/sh\necho stack\n'}
        def response(url, **kwargs):
            result = Mock(status_code=200)
            if url.endswith('/git/trees/HEAD'):
                result.json.return_value = {'tree': [{'path': '.sbx', 'type': 'tree', 'sha': 'recipe-tree'}]}
            elif url.endswith('/git/trees/recipe-tree'):
                result.json.return_value = {'tree': [{'path': p.removeprefix('.sbx/'), 'type': 'blob', 'mode': '100755' if p.endswith(('/setup', '/stack')) else '100644', 'sha': p, 'size': len(v)} for p, v in files.items()]}
            else:
                path = url.split('/git/blobs/')[1]
                result.json.return_value = {'content': base64.b64encode(files[path].encode()).decode()}
            return result
        with patch.object(repo_onboarding.github_app, 'settings', return_value={}), patch.object(repo_onboarding.httpx, 'get', side_effect=response), patch('sbx_control.template_builds.start') as build:
            result = self.client.post('/api/repos/owner/app/prepare')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['configured'])
        self.assertEqual(recipes.load('owner/app').spec['services']['db']['uses'], 'neon-branch')
        self.assertEqual((recipes.load('owner/app').dir / 'setup').read_text(), files['.sbx/setup'])
        self.assertEqual((recipes.load('owner/app').dir / 'config/app.json').read_text(), '{}')
        self.assertTrue((recipes.load('owner/app').dir / 'stack').stat().st_mode & 0o100)
        self.assertNotIn('EXTERNAL_KEY', result.text)
        build.assert_not_called()
        from sbx_control import template_builds
        self.assertFalse(template_builds.state('owner/app'))
        with patch('sbx_control.views.collect', return_value=[]):
            readiness = self.client.get('/api/repos/owner/app/readiness').json()
        self.assertEqual(next(step for step in readiness['steps'] if step['step'] == 'template')['status'], 'todo')

    def test_repo_setup_endpoints_require_an_admin(self):
        self.repo_admin()
        self.settings.update('access', {'admins': ['someone-else@example.com']}, by='test')
        with patch('sbx_control.repo_onboarding.inspect_repo') as inspect:
            for path, body in (('/api/repo-onboarding/inspect', {'repo': 'owner/app'}),
                               ('/api/repo-onboarding/connect', {'repo': 'owner/app'}),
                               ('/api/repos/owner/app/setup-pr', {'files': {'.sbx/sbx.yaml': 'template: app'}})):
                response = self.client.post(path, json=body)
                self.assertEqual(response.status_code, 403, response.text)
            inspect.assert_not_called()

    def test_missing_or_invalid_recipe_does_not_claim_to_be_configured(self):
        from sbx_control import repo_onboarding
        self.repo_admin()
        self.settings.add_repo('owner/app', by='test')
        with patch.object(repo_onboarding.github_app, 'settings', return_value={}), patch.object(repo_onboarding.httpx, 'get', return_value=Mock(status_code=404)):
            result = self.client.post('/api/repos/owner/app/prepare')
        self.assertEqual(result.json()['configured'], False)
        import base64
        response = Mock(status_code=200)
        response.json.return_value = {'tree': [{'path': '.sbx', 'type': 'tree', 'sha': 'recipe-tree'}]}
        tree = Mock(status_code=200)
        tree.json.return_value = {'tree': [{'path': 'sbx.yaml', 'type': 'blob', 'mode': '100644', 'sha': 'abc', 'size': 12}]}
        blob = Mock(status_code=200)
        blob.json.return_value = {'content': base64.b64encode(b'services: []').decode()}
        with patch.object(repo_onboarding.github_app, 'settings', return_value={}), patch.object(repo_onboarding.httpx, 'get', side_effect=[response, tree, blob]):
            result = self.client.post('/api/repos/owner/app/prepare')
        self.assertEqual(result.status_code, 400)

    def test_invalid_recipe_replacement_leaves_the_working_recipe_intact(self):
        from sbx_control import recipes, repo_onboarding
        self.repo_admin()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'recipe'
            source.mkdir()
            (source / 'sbx.yaml').write_text('previews: {web: 3000}\n')
            (source / 'setup').write_text('echo working\n')
            with patch('sbx_control.paths.apps', return_value=Path(directory) / 'apps'):
                repo_onboarding.apply_recipe('owner/app', source, by='test')
                (source / 'sbx.yaml').write_text('services: []\n')
                with self.assertRaisesRegex(ValueError, 'services must be a mapping'):
                    repo_onboarding.apply_recipe('owner/app', source, by='test')
                self.assertEqual(recipes.load('owner/app').spec['previews']['web'], 3000)
                self.assertEqual((recipes.load('owner/app').dir / 'setup').read_text(), 'echo working\n')

    def test_env_import_hides_credentials_even_when_the_name_has_no_secret_hint(self):
        from sbx_control import core
        self.repo_admin()
        self.settings.add_repo('owner/app', by='test')
        with patch.object(core, 'sandboxes', return_value=[]):
            response = self.client.post('/api/repos/owner/app/env-import', json={
                'text': 'DATABASE_URL=postgresql://dev:fixture@db/app\nLOG_LEVEL=debug\nEMPTY=\n',
                'agent': False,
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['set'], {'DATABASE_URL': True, 'LOG_LEVEL': True})
            response = self.client.get('/api/repos/owner/app/env')
            self.assertNotIn('postgresql://dev:fixture@db/app', response.text)
            rows = {v['name']: v for v in response.json()['variables']}
            self.assertTrue(rows['DATABASE_URL']['secret'])
            self.assertFalse(rows['DATABASE_URL']['agent'])
            response = self.client.put('/api/repos/owner/app/env/PUBLIC_ORIGIN', json={'value': 'https://example.test', 'secret': False})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('https://example.test', self.client.get('/api/repos/owner/app/env').text)

    def test_template_refresh_keeps_companion_files_and_removes_stale_files(self):
        from sbx_control import recipes
        self.repo_admin()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recipe = root / '.sbx'
            (recipe / 'config').mkdir(parents=True)
            (recipe / 'sbx.yaml').write_text('previews: {web: 3000}\n')
            (recipe / 'setup').write_text('#!/bin/sh\necho prepared\n')
            (recipe / 'setup').chmod(0o700)
            (recipe / 'config/app.json').write_text('{}')
            def git(*args):
                return subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                                       'user.email=test@example.com', '-c', 'commit.gpgsign=false', *args],
                                      check=True, capture_output=True)
            git('init', '-q')
            git('add', '.sbx')
            git('commit', '-qm', 'recipe')
            recipes.import_from('owner/app', recipes.from_mirror(root))
            installed = recipes.load('owner/app').dir
            self.assertTrue((installed / 'setup').stat().st_mode & 0o100)
            self.assertEqual((installed / 'config/app.json').read_text(), '{}')
            (recipe / 'config/app.json').unlink()
            git('add', '-u')
            git('commit', '-qm', 'remove stale config')
            recipes.import_from('owner/app', recipes.from_mirror(root))
            self.assertFalse((installed / 'config/app.json').exists())

    def test_template_clone_keeps_github_credentials_out_of_git_arguments(self):
        from sbx_control import extensions, github_app, recipes
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(recipes, 'mirrors', return_value=Path(directory)), \
                patch.object(github_app, 'access', return_value='app'), \
                patch.object(github_app, 'token', return_value='fixture-github-token'), \
                patch.object(recipes.subprocess, 'run') as run:
            recipes.mirror('owner/app', extensions.git_access('owner/app'))
        self.assertNotIn('fixture-github-token', str([call.args for call in run.call_args_list]))
        args = run.call_args.args[0]
        self.assertIn('https://github.com/owner/app.git', args)
        env = run.call_args.kwargs['env']
        index = int(env['GIT_CONFIG_COUNT']) - 1
        self.assertEqual(env[f'GIT_CONFIG_KEY_{index}'], 'http.https://github.com/.extraheader')
        self.assertTrue(env[f'GIT_CONFIG_VALUE_{index}'].startswith('Authorization: Basic '))

    def test_an_anonymous_mirror_fetch_says_whether_the_repo_works_without_the_app(self):
        from sbx_control import extensions, github_app, recipes, repos, settings
        settings.add_repo('owner/app', by='test')
        self.assertFalse(repos.info('owner/app')['github_anonymous'])
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(recipes, 'mirrors', return_value=Path(directory)), \
                patch.object(github_app, 'access', return_value='anonymous'), \
                patch.object(recipes.subprocess, 'run') as run:
            recipes.mirror('owner/app', extensions.git_access('owner/app'))
            info = repos.info('owner/app')
            self.assertEqual((info['github'], info['github_anonymous']), ('anonymous', True))
            run.side_effect = subprocess.CalledProcessError(128, 'git')
            with self.assertRaises(subprocess.CalledProcessError):
                recipes.mirror('owner/app', extensions.git_access('owner/app'))
            self.assertFalse(repos.info('owner/app')['github_anonymous'])

    def test_the_keeper_learns_whether_a_repo_served_before_any_record_works_anonymously(self):
        from sbx_control import github_app, keeper, repos, settings, views
        settings.add_repo('owner/app', by='test')
        settings.add_repo('owner/private', by='test')
        settings.add_repo('owner/offline', by='test')
        answers = {'owner/app': Mock(returncode=0, stderr=''),
                   'owner/private': Mock(returncode=128, stderr="fatal: could not read Username for "
                                                                "'https://github.com': terminal prompts disabled"),
                   'owner/offline': Mock(returncode=128, stderr='fatal: unable to access: Could not resolve host')}

        def git(args, **kwargs):
            self.assertEqual(kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
            return answers[args[-2].removeprefix('https://github.com/').removesuffix('.git')]
        with patch.object(views, 'known_repos', return_value=['owner/app', 'owner/private', 'owner/offline',
                                                               'stranger/repo']), \
                patch.object(github_app, 'access', return_value='anonymous'), \
                patch.object(github_app.subprocess, 'run', side_effect=git) as run:
            keeper.learn_repo_access()
            self.assertEqual(sorted(c.args[0][-2] for c in run.call_args_list),
                             [f'https://github.com/owner/{n}.git' for n in ('app', 'offline', 'private')])
            self.assertTrue(repos.info('owner/app')['github_anonymous'])
            self.assertFalse(repos.info('owner/private')['github_anonymous'])
            self.assertIsNone(github_app._anonymous('owner/offline').get())  # no answer: asked again next round
            run.reset_mock()
            keeper.learn_repo_access()  # a repo known to work is not asked again
            self.assertEqual(sorted(c.args[0][-2] for c in run.call_args_list),
                             [f'https://github.com/owner/{n}.git' for n in ('offline', 'private')])
        with patch.object(views, 'known_repos', return_value=['owner/private']), \
                patch.object(github_app, 'access', return_value='app'), \
                patch.object(github_app.subprocess, 'run') as run:
            keeper.learn_repo_access()  # the App reaches it: nothing to learn
        run.assert_not_called()

    def test_build_status_survives_restart_and_interrupted_build_is_retryable(self):
        import importlib
        import threading
        import time
        from sbx_control import template_builds, templates
        key = templates.DEFAULT

        def settle(status):
            deadline = time.monotonic() + 5
            while template_builds.state(key).get('status') != status:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)

        with patch('sbx_control.templates.build') as build:
            self.assertTrue(template_builds.start(key))
            settle('built')
        # The base came first: the default builds on it.
        self.assertEqual([c.args[0].key for c in build.call_args_list], [templates.BASE, key])
        importlib.reload(template_builds)  # a restart: nothing in memory survives it
        self.assertEqual(template_builds.state(key)['status'], 'built')
        self.assertTrue(template_builds.state(key)['template'].startswith('default-'))
        release = threading.Event()
        with patch('sbx_control.templates.build', side_effect=lambda *a, **k: release.wait(5)):
            before = set(threading.enumerate())
            self.assertTrue(template_builds.start(key))
            worker = next(t for t in threading.enumerate() if t not in before)
            self.assertEqual(template_builds.state(key)['status'], 'building')
            self.assertFalse(template_builds.start(key))
            importlib.reload(template_builds)
            result = template_builds.state(key)
            release.set()
            worker.join(5)  # the cut-off build's thread, before the next test: it can't settle the build now
        self.assertEqual(result['status'], 'failed')
        self.assertIn('restarted', result['error'])
        self.assertEqual(template_builds.state(key)['status'], 'failed')
        with patch('sbx_control.templates.build'):
            template_builds.build_now(key)  # retryable
        self.assertEqual(template_builds.state(key)['status'], 'built')

    def test_setup_pr_is_draft_scoped_to_sbx_and_idempotent(self):
        import httpx
        from sbx_control import setup_pr
        self.repo_admin()
        self.settings.add_repo('owner/app', by='test')
        files = {'.sbx/sbx.yaml': 'template: owner-app\n', '.sbx/setup': '#!/bin/sh\necho setup\n'}
        writes = []
        opened = []
        def handle(request):
            self.assertEqual(request.headers['authorization'], 'Bearer fixture-token')
            path = request.url.path
            if request.method == 'POST':
                import json
                body = json.loads(request.content)
                writes.append((path, body))
                if path.endswith('/git/trees'):
                    self.assertEqual(body['base_tree'], 'base-tree')
                    self.assertEqual({f['path'] for f in body['tree']}, set(files))
                    return httpx.Response(201, json={'sha': 'new-tree'})
                if path.endswith('/git/commits'):
                    self.assertEqual(body['parents'], ['base-head'])
                    return httpx.Response(201, json={'sha': 'new-head'})
                if path.endswith('/git/refs'):
                    return httpx.Response(201, json={})
                if path.endswith('/pulls'):
                    self.assertTrue(body['draft'])
                    self.assertEqual(body['base'], 'develop')
                    pr = {'number': 42, 'html_url': 'https://github.com/owner/app/pull/42', 'state': 'open', 'draft': True, 'head': {'ref': body['head']}}
                    opened.append(pr)
                    return httpx.Response(201, json=pr)
            if path.endswith('/pulls'):
                return httpx.Response(200, json=opened)
            if path.endswith('/pulls/42'):
                return httpx.Response(200, json={**opened[0], 'merged_at': '2026-01-01'})
            if path.endswith('/git/ref/heads/develop'):
                return httpx.Response(200, json={'object': {'sha': 'base-head'}})
            if '/git/ref/heads/sbx' in path:
                return httpx.Response(404, json={})
            if path.endswith('/git/commits/base-head'):
                return httpx.Response(200, json={'tree': {'sha': 'base-tree'}})
            return httpx.Response(200, json={'default_branch': 'develop'})
        github = httpx.Client
        with patch.object(setup_pr.github_app, 'token', return_value='fixture-token'), \
                patch.object(setup_pr.httpx, 'Client', side_effect=lambda **kwargs: github(transport=httpx.MockTransport(handle), **kwargs)):
            one = self.client.post('/api/repos/owner/app/setup-pr', json={'files': files})
            two = self.client.post('/api/repos/owner/app/setup-pr', json={'files': files})
            status = self.client.get('/api/repos/owner/app/setup-pr')
        self.assertEqual(one.status_code, 200, one.text)
        self.assertEqual(two.json()['url'], one.json()['url'])
        self.assertEqual(status.json()['pull_request']['state'], 'merged')
        self.assertEqual(len(writes), 4)
        with patch.object(setup_pr.httpx, 'Client') as client:
            for path in ('../file', '.sbx/../file', '.sbx/.env', '.sbx/private.key'):
                result = self.client.post('/api/repos/owner/app/setup-pr', json={'files': {**files, path: 'no'}})
                self.assertEqual(result.status_code, 400)
            client.assert_not_called()


class MissingValuesTests(Server):
    """What a repo still needs: the repo page (secrets_missing) and its env
    view (missing) say the same, by the rules a sandbox's env is made with."""
    SETTINGS = {"repos": ["owner/app"]}

    def test_the_repo_page_and_the_env_view_cannot_disagree(self):
        from sbx_control import env, repos, settings, variables
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("sbx_control.paths.apps", return_value=directory))
        (directory / "owner/app/.sbx").mkdir(parents=True)
        (directory / "owner/app/.sbx/sbx.yaml").write_text(
            "secrets: [REPO_SET, FROM_INSTALLATION, FROM_PATTERN, UNSET]\n"
            "env: {A: '${secret:REPO_SET}', B: '${secret:FROM_INSTALLATION}'}\n"
            "services:\n  db:\n    uses: static\n    with: {token: '${secret:WITH_UNSET}'}\n")
        variables.put("owner/app", "REPO_SET", "x", "test")
        variables.put(None, "FROM_INSTALLATION", "x", "test")
        settings.update("variables", {"by_pattern": {"*": {"FROM_INSTALLATION": "${secret:FROM_INSTALLATION}"},
                                                     "owner/*": {"FROM_PATTERN": "x"}, "other/*": {"UNSET": "x"}}},
                        by="test")
        # A ${secret:} in sbx.yaml resolves from the repo's values only; a secrets: name is also
        # given by an installation variable of a pattern that matches the repo.
        expected = ["FROM_INSTALLATION", "UNSET", "WITH_UNSET"]
        self.assertEqual(env.missing("owner/app"), expected)
        self.assertEqual(repos.info("owner/app")["secrets_missing"], expected)
        self.assertEqual(env.repo_env("owner/app", "operator@example.com")["missing"], expected)
        settings.set_repos([], by="test")
        self.assertEqual(repos.info("owner/app")["secrets_missing"],
                         ["FROM_INSTALLATION", "FROM_PATTERN", "REPO_SET", "UNSET", "WITH_UNSET"])

    def test_a_person_s_own_values_count_for_them(self):
        # sbx repo connect stores a non-admin's values as their own; their sandboxes get them.
        from sbx_control import env, repos, variables
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("sbx_control.paths.apps", return_value=directory))
        (directory / "owner/app/.sbx").mkdir(parents=True)
        (directory / "owner/app/.sbx/sbx.yaml").write_text("secrets: [APP_SECRET]\nservices:\n  db: {uses: static}\n")
        variables.put("owner/app", "APP_SECRET", "x", "person@example.com", person="person@example.com")
        self.assertEqual(env.repo_env("owner/app", "person@example.com")["missing"], [])
        self.assertEqual(env.repo_env("owner/app", "other@example.com")["missing"], ["APP_SECRET"])
        self.assertEqual(env.missing("owner/app"), ["APP_SECRET"])   # the repo list: the repo's values only
        self.assertEqual(repos.info("owner/app")["services"], ["db"])

    def test_a_service_s_values_set_on_the_server_go_over_sbx_yaml_s(self):
        # Sandboxes are made with a service's server-set values over sbx.yaml's with:, and not with
        # a disabled service at all, so neither leaves a ${secret:} to ask for.
        from sbx_control import env
        from sbx_control.catalog import attachments
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("sbx_control.paths.apps", return_value=directory))
        (directory / "owner/app/.sbx").mkdir(parents=True)
        (directory / "owner/app/.sbx/sbx.yaml").write_text(
            "services:\n"
            "  api:\n    uses: static\n    with: {base_url: '${secret:API_BASE_URL}'}\n"
            "  metrics:\n    uses: static\n    with: {token: '${secret:METRICS_TOKEN}'}\n")
        self.assertEqual(env.missing("owner/app"), ["API_BASE_URL", "METRICS_TOKEN"])
        attachments.attach("owner/app", "static", "api", {"base_url": "https://api.example.test"}, {},
                           by="test", run_check=False)
        attachments.detach("owner/app", "metrics", by="test")
        self.assertEqual(attachments.for_create("owner/app"),
                         [("api", "static", {"base_url": "https://api.example.test"}, {})])
        self.assertEqual(env.missing("owner/app"), [])
        self.assertEqual(env.repo_env("owner/app", "operator@example.com")["missing"], [])

    def test_installation_variables_are_the_server_s_and_audiences_come_from_me(self):
        from sbx_control import env, settings
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("sbx_control.paths.apps", return_value=directory))
        (directory / "owner/app/.sbx").mkdir(parents=True)
        (directory / "owner/app/.sbx/sbx.yaml").write_text("env: {A: '1'}\n")
        settings.update("variables", {"by_pattern": {"owner/*": {"FROM_PATTERN": "x"}}}, by="test")
        view = env.repo_env("owner/app", "operator@example.com")
        row = next(r for r in view["variables"] if r["name"] == "FROM_PATTERN")
        self.assertEqual((row["kind"], row["source"]), ("installation", "server"))   # no config.yaml file exists
        self.assertNotIn("kinds", view)   # /api/me has the audiences
