"""The Amp connector: its connection (settings and token), its orb jobs
through the public sandbox lifecycle, its project snapshots, and the base
transport its sandboxes start."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch


from support import Server


class AmpTests(Server):
    def setUp(self):
        super().setUp()
        from sbx_control import settings, state
        self.settings, self.state = settings, state

    def test_cached_systemd_boot_starts_ssh_with_fresh_keys_only_when_it_is_down(self):
        """START's SSH repair against a fake systemd: keys can't be generated
        while boot-time retries hold ssh, and a unit that failed at boot
        starts only after its failure is reset."""
        import subprocess
        import tempfile
        from sbx_control import platform
        startup = platform.START.split('nohup websocat', 1)[0].removeprefix("bash -c '\n")
        with tempfile.TemporaryDirectory(prefix='sbx-ssh-boot-') as temporary:
            root = Path(temporary)
            (root / 'init').write_text('systemd\n')
            startup = startup.replace('/proc/1/comm', str(root / 'init'))
            systemd = '''
ssh-keyscan() { test "$listening" = yes && echo "127.0.0.1 ssh-ed25519 fixture-key"; }
sudo() {
  echo "$*" >> "$touched"
  case "$*" in
    "systemctl stop ssh.socket ssh.service") stopped=yes ;;
    "ssh-keygen -A") test "$stopped" = yes || return 1; keys=yes ;;
    "systemctl reset-failed ssh.socket ssh.service") reset=yes ;;
    "systemctl start ssh.service") test "$keys" = yes && test "$reset" = yes || return 1; echo running > "$ssh" ;;
  esac
}
'''
            for listening in ('no', 'yes'):
                with self.subTest(listening=listening):
                    touched, ssh = root / f'touched-{listening}', root / f'ssh-{listening}'
                    touched.write_text('')
                    subprocess.run(['bash', '-c', f'touched={touched}; ssh={ssh}; listening={listening};\n'
                                    + systemd + startup], check=True, capture_output=True, text=True)
                    if listening == 'no':
                        self.assertEqual(ssh.read_text(), 'running\n')
                    else:
                        self.assertEqual((touched.read_text(), ssh.exists()), ('', False))

    def test_filesystem_template_starts_base_transport_only_when_missing(self):
        from sbx_control import core, platform

        class Guest:
            """A VM whose transport comes up once START runs."""
            def __init__(self, ready):
                self.ready, self.launched, self.commands = ready, [], self

            def run(self, command, background=False, **kwargs):
                if command == platform.START:
                    self.launched.append(background)
                    self.ready = True
                    return Mock()
                return SimpleNamespace(stdout='ready' if command == platform.STARTED and self.ready else '')

        cold, warm = Guest(False), Guest(True)
        core.ensure_started(cold)
        core.ensure_started(warm)
        self.assertEqual((cold.launched, warm.launched), ([True], []))

    def test_amp_connection_api_seals_token_and_disconnect_retains_vms(self):
        from sbx_control import vault
        from sbx_control.connectors.amp import orbs
        self.repo_admin()
        token = "aop_live_disposable_test_only"
        with patch("sbx_control.connectors.amp.active", return_value=False), \
                patch.object(orbs, "runtime") as runtime:
            response = self.client.put("/api/connections/amp", json={"values": {
                "provider_token": token, "enabled": "true",
            }})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["state"], "attention")
            self.assertNotIn(token, response.text)
            self.assertEqual(vault.get(orbs.TOKEN), token)
            self.assertIn("amp", self.effects())
            # Blank secret fields keep the saved value; rotation also requests restart.
            saved = self.client.put("/api/connections/amp", json={"values": {"provider_token": ""}})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(vault.get(orbs.TOKEN), token)
            response = self.client.delete("/api/connections/amp")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(orbs.current().enabled)
            self.assertIsNone(vault.get(orbs.TOKEN))
            runtime.kill.assert_not_called()

    def test_amp_validation_happens_before_secret_is_saved(self):
        from sbx_control import vault
        from sbx_control.connectors.amp import orbs
        self.repo_admin()
        for values in ({"repo": "not a repository", "provider_token": "aop_live_disposable_test_only"},
                       {"provider_token": "wrong-token"}, {"enabled": "true"}):
            with self.subTest(values=values):
                response = self.client.put("/api/connections/amp", json={"values": values})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIsNone(vault.get(orbs.TOKEN))

    def test_amp_paused_disks_are_exempt_from_sbx_idle_cleanup(self):
        from sbx_control import core, keeper
        from types import SimpleNamespace
        info = SimpleNamespace(metadata={"connector": "amp", "sbx": "amp-paused"}, sandbox_id="amp-paused-vm")
        with patch.object(core.runtime, "kill") as kill:
            keeper.delete_paused(info)
        kill.assert_not_called()

    def test_amp_snapshot_cache_is_persistent_repo_bound_and_retry_safe(self):
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from sbx_control import core, recipes
        from sbx_control.connectors.amp import orbs, snapshots
        key = 'amp-test-disposable-snapshot'
        with self.state.db() as conn:
            conn.execute('delete from amp_snapshots where key=?', (key,))
        with tempfile.TemporaryDirectory(prefix='sbx-amp-snapshot-test-') as directory:
            hook = Path(directory) / 'snapshot'
            hook.write_text('#!/bin/sh\nrm -f .env.local development.sqlite\n')
            hook.chmod(0o700)
            recipe = recipes.App('owner/app', Path(directory), {})
            with patch.object(recipes, 'load', return_value=recipe), \
                    patch.object(core, 'find', return_value=SimpleNamespace(metadata={'template': 'app-12345678'})), \
                    patch.object(snapshots, 'export',
                                 return_value=snapshots.runtime.Snapshot('prepared-cache-id', 'embed')) as export:
                snapshots.save('amp-source-test', key, 'owner/app')
                snapshots.save('amp-source-test', key, 'owner/app')
            export.assert_called_once()
            self.assertEqual(snapshots.cached(key, 'owner/app'), snapshots.runtime.Snapshot('prepared-cache-id', 'embed'))
            with self.assertRaises(orbs.JobError):
                snapshots.cached(key, 'other/repo')
            with patch.object(snapshots.runtime, 'delete_snapshot') as delete:
                snapshots.delete(key)
                snapshots.delete(key)
            delete.assert_called_once_with('prepared-cache-id', 'embed')
            self.assertIsNone(snapshots.cached(key, 'owner/app'))

    def test_prepared_snapshot_scrubs_an_offline_clone_and_never_mutates_source(self):
        import tempfile
        from sbx_control import core, recipes
        from sbx_control.connectors.amp import snapshots as prepared_snapshots
        with tempfile.TemporaryDirectory(prefix='sbx-cache-export-test-') as directory:
            hook = Path(directory) / 'snapshot'
            hook.write_text('#!/bin/sh\ntrue\n'); hook.chmod(0o700)
            recipe = recipes.App('owner/app', Path(directory), {})
            source = SimpleNamespace(sandbox_id='source-vm', metadata={'sbx': 'source-name', 'repo': 'owner/app'})
            sdk = Mock()
            sdk.kind_of.return_value = 'embed'
            sdk.info.return_value.memory_mb = 8192
            sdk.snapshot.side_effect = lambda sandbox_id, name: SimpleNamespace(
                snapshot_id={'source-vm': 'private-checkpoint', 'temporary-cache-vm': 'clean-project-cache'}[sandbox_id])
            staging = sdk.create.return_value
            staging.sandbox_id = 'temporary-cache-vm'
            with patch.object(core, 'find', return_value=source), patch.object(core, 'app_of', return_value=recipe), \
                    patch.object(core, 'reserve_capacity', return_value=contextlib.nullcontext()), \
                    patch.object(recipes, 'ship'), patch.object(core, 'run_in'), \
                    patch.object(prepared_snapshots, 'runtime', sdk):
                self.assertEqual(prepared_snapshots.export('source-name', 'cache-label').snapshot_id,
                                 'clean-project-cache')
            self.assertEqual(sdk.create.call_args.kwargs['kind'], 'embed')   # on the source's runtime
            # The clone starts without the workspace's env or any network.
            self.assertEqual(sdk.create.call_args.kwargs['envs'], {})
            self.assertEqual(sdk.create.call_args.kwargs['deny_out'], ['0.0.0.0/0'])
            sdk.connect.assert_not_called()
            sdk.kill.assert_called_once_with('temporary-cache-vm')
            sdk.delete_snapshot.assert_called_once_with('private-checkpoint', 'embed')
            # What the clone ran before it was published removed the runtime's credentials.
            self.assertTrue(any('sudo rm -rf /run/sbx' in c.args[0] for c in staging.commands.run.call_args_list))

    def test_prepared_snapshot_cleanup_failure_does_not_publish_cache(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from sbx_control import core, recipes
        from sbx_control.connectors.amp import snapshots as prepared_snapshots
        import contextlib
        with tempfile.TemporaryDirectory(prefix='sbx-cache-cleanup-test-') as directory:
            hook = Path(directory) / 'snapshot'
            hook.write_text('#!/bin/sh\nexit 1\n'); hook.chmod(0o700)
            recipe = recipes.App('owner/app', Path(directory), {})
            sdk = Mock()
            sdk.kind_of.return_value = 'embed'
            sdk.info.return_value.memory_mb = 8192
            sdk.snapshot.return_value.snapshot_id = 'private-checkpoint'
            with patch.object(core, 'find', return_value=SimpleNamespace(sandbox_id='source-vm', metadata={})), \
                    patch.object(core, 'app_of', return_value=recipe), \
                    patch.object(core, 'reserve_capacity', return_value=contextlib.nullcontext()), \
                    patch.object(recipes, 'ship'), \
                    patch.object(core, 'run_in', side_effect=RuntimeError('cleanup failed')), \
                    patch.object(prepared_snapshots, 'runtime', sdk):
                with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                    prepared_snapshots.export('source-name', 'cache-label')
            sdk.snapshot.assert_called_once()
            sdk.kill.assert_called_once_with(sdk.create.return_value.sandbox_id)
            sdk.delete_snapshot.assert_called_once_with('private-checkpoint', 'embed')

    def amp_fixture(self):
        from fastapi.testclient import TestClient
        from sbx_control import addressing, core, lifecycle, recipes, runtime, template_builds
        from sbx_control.connectors.amp import orbs as amp_orbs
        self.repo_admin()
        self.settings.add_repo("owner/app", by="amp-test")
        self.env = {"ORB_OPERATION": "spawn", "ORB_ID": "amp-disposable-test", "ORB_TOKEN": "test-only-orb-token"}
        self.name = amp_orbs.sandbox_name(self.env["ORB_ID"])
        with self.state.db() as conn:
            conn.execute("delete from sandboxes where name=?", (self.name,))
        self.local = self.enterContext(TestClient(self.app.app, base_url="http://127.0.0.1",
                                                  client=("127.0.0.1", 1234)))
        self.items = []
        self.enterContext(patch.object(core, "find", side_effect=lambda name: next(
            (i for i in self.items if i.metadata.get("sbx") == name), None)))
        self.enterContext(patch.object(amp_orbs, "routes", return_value=[{"repo": "owner/app", "orb_type": "owner-app"}]))
        self.enterContext(patch.object(template_builds, "template_for", return_value="owner-app-12345678"))
        # The default branch as the installation knows it; a spawn never asks GitHub.
        self.enterContext(patch.object(recipes, "default_branch", return_value="trunk"))
        self.enterContext(patch("sbx_control.repo_onboarding.inspect_repo", side_effect=AssertionError("asked GitHub")))
        self.recipe = recipes.App("owner/app", Path("/unused"), {"template": True})
        self.enterContext(patch.object(core, "app_of", return_value=self.recipe))
        # Every command in the guest succeeds; Amp's ready check says ready.
        self.enterContext(patch.object(core, "run_in")).return_value.stdout = "ready"
        self.enterContext(patch.object(addressing, "urls", return_value={"web": "https://preview.example.test"}))
        self.started = self.enterContext(patch.object(lifecycle, "in_background"))
        # The runtime, for core and the connector alike.
        self.sdk = Mock()
        for call in ("create", "connect", "sandboxes", "info", "kill", "pause", "set_timeout"):
            self.enterContext(patch.object(runtime, call, getattr(self.sdk, call)))
        self.enterContext(patch.object(core, "restore_runtime", return_value=False))
        def created(*args, **kwargs):
            self.items.append(SimpleNamespace(sandbox_id="amp-test-vm", state=runtime.RUNNING,
                metadata={**(kwargs.get("metadata") or {}), "sbx": args[0], "owner": args[2], "repo": "owner/app"}))
            return {"name": args[0], "sandbox_id": "amp-test-vm"}
        self.created = self.enterContext(patch.object(core, "create", side_effect=created))

    def amp_job(self, env):
        return self.local.post("/internal/amp/jobs", json=env)

    def spawned(self):
        """The last detached command run in the guest: Amp's bootstrap launch."""
        return [c for c in self.sdk.connect.return_value.commands.run.call_args_list if c.kwargs.get("background")][-1]

    def failing_bootstrap(self, error):
        """Guest commands succeed but the detached bootstrap launch, which
        raises error; what was launched is recorded."""
        launched = []

        def run(command, **kwargs):
            if kwargs.get("background"):
                launched.append(command)
                raise error
            return Mock()
        self.sdk.connect.return_value.commands.run.side_effect = run
        return launched

    def test_amp_fresh_spawn_uses_core_create_services_and_app_start(self):
        self.amp_fixture()
        response = self.amp_job(self.env)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["urls"], {"web": "https://preview.example.test"})
        self.assertEqual(self.created.call_args.args[5], "owner/app")
        self.assertEqual(self.created.call_args.args[3], "trunk")
        self.assertEqual(self.created.call_args.kwargs["metadata"]["connector"], "amp")
        # Registered to the first admin but driven by any Amp user: none of that admin's own values.
        from sbx_control import core
        self.assertFalse(core.personal(self.created.call_args.kwargs["metadata"]))
        self.assertEqual(self.created.call_args.kwargs["template"], "owner-app-12345678")
        self.started.assert_called_once()
        self.sdk.create.assert_not_called()
        self.assertNotIn(self.env["ORB_TOKEN"], response.text)
        guest = self.sdk.connect.return_value
        written = {c.args[0]: c.args[1] for c in guest.files.write.call_args_list}
        self.assertEqual(json.loads(written[self.recipe.workdir + '/.amp/portals/sbx-previews.json']),
                         {"links": [{"label": "web", "url": "https://preview.example.test"}]})
        # Amp's bootstrap is shipped into the sandbox and is what runs there, with the orb's token.
        bootstrap = (Path(__file__).parents[1] / 'sbx_control/connectors/amp/bootstrap.py').read_text()
        [shipped] = [path for path, content in written.items() if content == bootstrap]
        self.assertIn(shipped, self.spawned().args[0])
        self.assertEqual(self.spawned().kwargs["envs"]["ORB_TOKEN"], self.env["ORB_TOKEN"])
        # Then it is seen to have started, without the token.
        started = guest.commands.run.call_args
        self.assertEqual(started.args[0], f"python3 {shipped} --started")
        self.assertNotIn("envs", started.kwargs)

    def test_amp_repeated_spawn_and_pause_reuse_core_lifecycle(self):
        self.amp_fixture()
        self.assertEqual(self.amp_job(self.env).status_code, 200)
        self.items[0].state = "paused"
        from sbx_control import lifecycle
        response = self.amp_job({**self.env, "ORB_TOKEN": "test-only-new-token"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(lifecycle.get(self.name)["operation"], "resume")
        self.created.assert_called_once()
        envs = self.spawned().kwargs["envs"]
        self.assertEqual(envs["ORB_TOKEN"], "test-only-new-token")
        self.items[0].state = "running"
        from sbx_control import core
        with patch.object(core, "pause") as pause:
            self.assertEqual(self.amp_job({"ORB_OPERATION": "pause", "ORB_ID": self.env["ORB_ID"]}).status_code, 200)
            self.items[0].state = "paused"
            self.assertEqual(self.amp_job({"ORB_OPERATION": "pause", "ORB_ID": self.env["ORB_ID"]}).status_code, 200)
        pause.assert_called_once()
        self.sdk.pause.assert_not_called()

    def test_amp_delete_uses_core_resource_and_random_secret_cleanup(self):
        self.amp_fixture()
        self.assertEqual(self.amp_job(self.env).status_code, 200)
        from sbx_control import core, variables
        with patch.object(core, "delete") as delete, patch.object(variables, "forget") as forget:
            response = self.amp_job({"ORB_OPERATION": "delete", "ORB_ID": self.env["ORB_ID"]})
            self.assertEqual(response.status_code, 200, response.text)
            delete.assert_called_once_with(self.name, self.name)
            forget.assert_called_once_with(self.name)
        self.items.clear()
        with patch.object(core, "delete") as delete:
            self.assertEqual(self.amp_job({"ORB_OPERATION": "delete", "ORB_ID": self.env["ORB_ID"]}).status_code, 200)
            delete.assert_not_called()
        self.sdk.kill.assert_not_called()

    def test_amp_retry_repairs_retained_runtime_before_preparation_and_bootstrap(self):
        self.amp_fixture()
        self.assertEqual(self.amp_job(self.env).status_code,200)
        from sbx_control import core, lifecycle, sandboxes
        failed={'active':False,'phase':'failed','application':'not_started','binding_matches':True}
        order=Mock()
        # attach restores the runtime the allocation may lack (its start contract and host keys
        # included), without the app; then the sandbox is prepared again, as the API's prepare does.
        with patch.object(lifecycle,'get',return_value=failed), \
                patch.object(core,'restore_runtime',side_effect=lambda *_:order.runtime()), \
                patch.object(sandboxes,'restart',side_effect=lambda *a,**k:order.prepare()):
            response=self.amp_job(self.env)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([call[0] for call in order.mock_calls], ['runtime','prepare'])
        self.created.assert_called_once()

    def test_amp_retry_transport_failure_cannot_bootstrap(self):
        self.amp_fixture()
        self.assertEqual(self.amp_job(self.env).status_code,200)
        self.sdk.connect.return_value.commands.run.reset_mock()
        from sbx_control import core, lifecycle, sandboxes
        with patch.object(lifecycle,'get',return_value={'active':False,'phase':'failed','application':'not_started',
                                                        'binding_matches':True}), \
                patch.object(core,'restore_runtime',side_effect=core.SandboxError('not ready')), \
                patch.object(sandboxes,'restart') as prepare:
            self.assertEqual(self.amp_job(self.env).status_code,503)
        prepare.assert_not_called()
        self.sdk.connect.return_value.commands.run.assert_not_called()

    def test_amp_unknown_route_and_forged_orb_binding_cannot_create(self):
        self.amp_fixture()
        self.assertEqual(self.amp_job({**self.env, "ORB_TYPE": "unknown"}).status_code, 400)
        self.created.assert_not_called()
        self.items.append(SimpleNamespace(metadata={"sbx": self.name}))
        self.assertEqual(self.amp_job(self.env).status_code, 400)
        self.created.assert_not_called()

    def test_amp_internal_dispatch_rejects_external_and_forwarded_requests(self):
        self.amp_fixture()
        self.assertEqual(self.client.post("/internal/amp/jobs", json=self.env).status_code, 404)
        self.assertEqual(self.local.post("/internal/amp/jobs", json=self.env,
                                        headers={"X-Forwarded-For": "127.0.0.1"}).status_code, 404)
        self.created.assert_not_called()

    def test_amp_second_launch_has_a_distinct_core_resource_identity(self):
        self.amp_fixture()
        from sbx_control import core
        from sbx_control.connectors.amp import orbs as amp_orbs
        sibling = amp_orbs.sandbox_name('amp-disposable-sibling')
        with self.state.db() as conn:
            conn.execute('delete from sandboxes where name=?', (sibling,))
        try:
            first = self.amp_job(self.env)
            second = self.amp_job({**self.env, 'ORB_ID': 'amp-disposable-sibling'})
            self.assertEqual((first.status_code, second.status_code), (200, 200))
            self.assertNotEqual(first.json()['name'], second.json()['name'])
            self.assertEqual(core.audience(self.items[0].metadata), 'sessions')
        finally:
            with self.state.db() as conn:
                conn.execute('delete from sandboxes where name=?', (sibling,))

    def test_amp_cached_template_is_used_through_core_creation(self):
        self.amp_fixture()
        from sbx_control.connectors.amp import snapshots
        with self.state.db() as conn:
            conn.execute("insert or replace into amp_snapshots (key,repo,snapshot_id,source_template) values (?,?,?,?)",
                         ('cached-spawn-test', 'owner/app', 'sanitized-cache-template', 'original-prepared-template'))
        try:
            response = self.amp_job({**self.env, 'ORB_SNAPSHOT_ID': 'cached-spawn-test'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.created.call_args.kwargs['template'], 'sanitized-cache-template')
            self.assertEqual(snapshots.cached('cached-spawn-test', 'owner/app').snapshot_id, 'sanitized-cache-template')
            self.assertEqual(self.created.call_args.kwargs['kind'], 'embed')   # the runtime that holds the cache
        finally:
            with self.state.db() as conn:
                conn.execute('delete from amp_snapshots where key=?', ('cached-spawn-test',))

    def test_amp_spawn_that_fails_after_its_vm_exists_deletes_it_and_can_be_repeated(self):
        """The VM was made, then the SSH-key step failed: the spawn deletes it
        (the VM, its services and its row) as Amp's delete would."""
        self.amp_fixture()
        from sbx_control import sandboxes, variables
        created = self.created.side_effect

        def made_then_failed(*args, **kwargs):
            created(*args, **kwargs)
            raise RuntimeError("workspace SSH listener identity changed")
        self.created.side_effect = made_then_failed
        self.sdk.kill.side_effect = lambda sandbox_id: self.items.clear()
        with patch.object(variables, "forget") as forget:
            self.assertEqual(self.amp_job(self.env).status_code, 503)
        self.sdk.kill.assert_called_once_with("amp-test-vm")
        forget.assert_called_once_with(self.name)
        self.assertIsNone(sandboxes.registered_owner(self.name))
        self.started.assert_not_called()
        # The next spawn starts over; a delete after the cleanup has nothing left to do.
        self.created.side_effect = created
        self.assertEqual(self.amp_job(self.env).status_code, 200)
        self.started.assert_called_once()   # the app is prepared once the spawn succeeded
        self.items.clear()
        self.assertEqual(self.amp_job({"ORB_OPERATION": "delete", "ORB_ID": self.env["ORB_ID"]}).status_code, 200)

    def test_amp_spawn_that_fails_in_its_vm_deletes_it_before_preparing_the_app(self):
        self.amp_fixture()
        from sbx_control import core
        launched = self.failing_bootstrap(RuntimeError("bootstrap did not start"))
        with patch.object(core, "delete") as delete:
            self.assertEqual(self.amp_job(self.env).status_code, 503)
        self.assertEqual(len(launched), 1)   # it got as far as launching the bootstrap
        delete.assert_called_once_with(self.name, self.name)
        self.started.assert_not_called()

    def test_amp_bootstrap_that_exits_before_it_starts_amp_deletes_a_fresh_vm(self):
        """The runtime accepted the detached launch, but the helper failed
        before Amp ran: the check after it says so, and the spawn fails."""
        self.amp_fixture()
        from sbx_control import core, runtime

        def run(command, **kwargs):
            if command.endswith("--started"):
                raise runtime.CommandFailed(stderr="the Amp bootstrap did not start", command=command)
            return Mock()
        self.sdk.connect.return_value.commands.run.side_effect = run
        with patch.object(core, "delete") as delete:
            response = self.amp_job(self.env)
        self.assertEqual(response.status_code, 400)
        self.assertIn("bootstrap did not start", response.text)
        delete.assert_called_once_with(self.name, self.name)
        self.started.assert_not_called()

    def test_amp_spawn_that_fails_on_an_existing_vm_keeps_it(self):
        """A VM already there, running or a paused thread's resumed for a new
        spawn, holds the thread's work: a failure leaves it for Amp to spawn
        again or delete."""
        self.amp_fixture()
        self.assertEqual(self.amp_job(self.env).status_code, 200)
        from sbx_control import core, lifecycle
        for state in ("running", "paused"):
            with self.subTest(state=state):
                self.items[0].state = state
                self.sdk.connect.reset_mock()
                launched = self.failing_bootstrap(RuntimeError("bootstrap did not start"))
                with patch.object(core, "delete") as delete:
                    self.assertEqual(self.amp_job({**self.env, "ORB_TOKEN": "test-only-new-token"}).status_code, 503)
                self.assertEqual(len(launched), 1)
                delete.assert_not_called()
                self.sdk.kill.assert_not_called()
                self.assertEqual([i.sandbox_id for i in self.items], ["amp-test-vm"])   # not replaced
                self.created.assert_called_once()
                self.sdk.connect.assert_called()
                if state == "paused":
                    self.assertEqual(lifecycle.get(self.name)["operation"], "resume")

    def test_amp_failed_spawn_leaves_a_registration_that_is_not_this_orb_s(self):
        """create refused the name (someone else's registration, no VM): the
        cleanup deletes nothing of it."""
        self.amp_fixture()
        from sbx_control import core, sandboxes
        sandboxes.register(self.name, "someone-else@example.test", "trunk", self.name)
        with patch.object(core, "delete") as delete:
            self.assertEqual(self.amp_job(self.env).status_code, 503)
        delete.assert_not_called()
        self.assertEqual(sandboxes.registered_owner(self.name), "someone-else@example.test")

    def test_amp_failed_cleanup_keeps_the_spawn_s_own_error(self):
        self.amp_fixture()
        from sbx_control import core
        from sbx_control.connectors.amp import orbs as amp_orbs
        launched = self.failing_bootstrap(RuntimeError("bootstrap did not start"))
        # The cleanup's error carries the token: it is never what is logged.
        with patch.object(core, "delete", side_effect=RuntimeError(f"runtime unreachable {self.env['ORB_TOKEN']}")), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            with self.assertRaisesRegex(RuntimeError, "bootstrap did not start"):
                amp_orbs.handle_job(self.env)
        self.assertEqual(len(launched), 1)
        self.assertIn("ORB_OPERATION=delete", said.getvalue())
        self.assertNotIn(self.env["ORB_TOKEN"], said.getvalue())

    def test_amp_sdk_failure_does_not_leak_token_or_request(self):
        self.amp_fixture()
        self.created.side_effect = RuntimeError("test-only-orb-token private SDK request")
        response = self.amp_job(self.env)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("test-only-orb-token", response.text)
        self.assertNotIn("private SDK", response.text)

    def test_amp_dispatch_forwards_only_contract_fields_to_control(self):
        from sbx_control.connectors.amp import orbs as amp_orbs
        client = MagicMock()
        with patch.object(amp_orbs.httpx, "Client", return_value=client):
            amp_orbs.dispatch({"ORB_ID": "test", "ORB_OPERATION": "spawn", "ORB_TOKEN": "test-only-token",
                               "AMP_ORB_PROVIDER_TOKEN": "test-only-provider", "E2B_API_KEY": "test-only-runtime"})
        body = client.__enter__.return_value.post.call_args.kwargs["json"]
        self.assertEqual(set(body), {"ORB_ID", "ORB_OPERATION", "ORB_TOKEN"})

    def test_amp_dispatch_error_does_not_print_http_request(self):
        from sbx_control.connectors.amp import orbs as amp_orbs
        with patch.object(amp_orbs, "dispatch", side_effect=RuntimeError("test-only-token")), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(amp_orbs.main(["dispatch"]), 1)
        self.assertNotIn("test-only-token", output.getvalue())

    def test_lookup_scopes_both_running_and_paused_vms_and_reads_all_pages(self):
        from sbx_control.connectors.amp import orbs as amp_orbs
        from sbx_control import runtime
        with patch.object(runtime, "sandboxes", return_value=["first", "second"]) as listing:
            self.assertEqual(amp_orbs.sandboxes_of("amp-test"), ["first", "second"])
        listing.assert_called_once_with({"connector": "amp", "orbId": "amp-test"}, [runtime.RUNNING, runtime.PAUSED])
