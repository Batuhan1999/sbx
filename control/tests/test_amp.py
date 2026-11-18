"""The Amp connector: its connection (settings and token), its orb jobs
through the public sandbox lifecycle, its project snapshots, and the base
transport its sandboxes start."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch


from support import Installation, Server


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
        self.assertIn(shipped, guest.commands.run.call_args.args[0])
        self.assertEqual(guest.commands.run.call_args.kwargs["envs"]["ORB_TOKEN"], self.env["ORB_TOKEN"])

    def test_amp_repeated_spawn_and_pause_reuse_core_lifecycle(self):
        self.amp_fixture()
        self.assertEqual(self.amp_job(self.env).status_code, 200)
        self.items[0].state = "paused"
        from sbx_control import lifecycle
        response = self.amp_job({**self.env, "ORB_TOKEN": "test-only-new-token"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(lifecycle.get(self.name)["operation"], "resume")
        self.created.assert_called_once()
        envs = self.sdk.connect.return_value.commands.run.call_args.kwargs["envs"]
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


def _tarball(gateway: bool = True, executable: bool = True) -> bytes:
    import io
    import tarfile
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for path in ("amp-wayland/bin/labwc", "amp-wayland/bin/waymote-gateway", "amp-wayland/libexec/waymote-streamd"):
            if not gateway and path.endswith("waymote-gateway"):
                continue
            entry = tarfile.TarInfo(path)
            entry.size, entry.mode = 4, 0o755 if executable else 0o644
            tar.addfile(entry, io.BytesIO(b"\x7fELF"))
    return out.getvalue()


TARBALL = _tarball()


class AmpToolsTests(Installation):
    """Amp's desktop tools come from its container image: fetched once on a
    runtime that imports images, whichever is the default, and kept on the
    host for every runtime's builds."""

    def setUp(self):
        super().setUp()
        from sbx_control import runtime
        from sbx_control.connectors import amp
        from sbx_control.runtime.fake import Fake

        class BoatLike(Fake):
            """Starts only from its own Ubuntu, as Boat does."""
            kind, id_prefix = "boat", "bt"
            capabilities = runtime.Capabilities(snapshots=True)

            def build_template(self, spec, **kwargs):
                if any(what == "image" and value != "ubuntu:24.04" for what, value in spec.steps):
                    raise runtime.Failed("Boat starts every sandbox from its own Ubuntu 24.04")
                return super().build_template(spec, **kwargs)

        def tools(box, command):
            if command.startswith("tar -C /opt"):
                box.files["/tmp/sbx-amp-tools.tar.gz"] = TARBALL
            return None

        self.runtime, self.amp, self.boat, self.fake = runtime, amp, BoatLike(), Fake()
        self.fake.respond = tools
        self.enterContext(patch.dict(runtime._backends, {"boat": self.boat, "fake": self.fake}))
        self.enterContext(patch.object(runtime, "enabled", return_value=["boat", "fake"]))
        self.enterContext(patch.object(runtime, "default", return_value="boat"))
        if amp.tools_cache().exists():
            for path in amp.tools_cache().iterdir():
                path.unlink()

    def fetches(self, backend) -> int:
        return sum(cmd.startswith("tar -C /opt") for _, cmd in backend.ran)

    def test_a_default_runtime_without_image_import_fetches_on_one_that_has_it(self):
        self.assertEqual(self.amp.tools_runtime(), "fake")
        self.assertEqual(self.amp.Amp._tools_check().detail, "fetched on Fake runtime at the next template build")
        self.assertEqual(self.amp.wayland_tools(), TARBALL)
        self.assertEqual((self.fetches(self.fake), self.boat.ran, self.boat.boxes), (1, [], {}))
        self.assertEqual((self.fake.boxes, self.fake.built), ({}, {}))   # its builder and template are gone
        self.assertEqual(self.amp.cached_tools().read_bytes(), TARBALL)

    def test_the_kept_tools_are_reused_and_other_digests_dropped(self):
        stale = self.amp.tools_cache() / "0000.tar.gz"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"older image")
        self.amp.wayland_tools()
        self.assertEqual(self.amp.wayland_tools(), TARBALL)
        self.assertEqual(self.fetches(self.fake), 1)                  # the second build reused it
        self.assertFalse(stale.exists())
        self.assertEqual(self.amp.Amp._tools_check().detail, "kept on this host")
        with patch.object(self.runtime, "enabled", return_value=["boat"]):   # Boat alone builds from the copy
            self.assertEqual(self.amp.wayland_tools(), TARBALL)
        self.assertEqual(self.fetches(self.fake), 1)

    def test_no_runtime_that_imports_images_says_what_to_enable(self):
        with patch.object(self.runtime, "enabled", return_value=["boat"]):
            self.assertIsNone(self.amp.tools_runtime())
            check = self.amp.Amp._tools_check()
            with self.assertRaisesRegex(RuntimeError, "no enabled sandbox runtime builds templates from one"):
                self.amp.wayland_tools()
        self.assertFalse(check.ok)
        self.assertIn("Enable a runtime that does (E2B Embed, E2B Cloud", check.fix)
        self.assertEqual((self.boat.ran, self.boat.built), ([], {}))
        self.assertFalse(self.amp.cached_tools().exists())

    def test_a_damaged_kept_copy_is_fetched_again(self):
        flipped = bytearray(TARBALL)
        flipped[len(flipped) // 2] ^= 0xFF   # the compressed payload damaged
        for damaged in (b"", TARBALL[:len(TARBALL) // 2], TARBALL[:-4], bytes(flipped), _tarball(gateway=False), _tarball(executable=False)):
            self.amp.cached_tools().parent.mkdir(parents=True, exist_ok=True)
            self.amp.cached_tools().write_bytes(damaged)
            self.assertFalse(self.amp.Amp._tools_check().detail == "kept on this host")
            self.assertEqual(self.amp.wayland_tools(), TARBALL)
            self.assertEqual(self.amp.cached_tools().read_bytes(), TARBALL)
        self.assertEqual(self.fetches(self.fake), 6)

    def test_the_throwaway_template_is_this_installations(self):
        names = []
        build = self.fake.build_template
        self.enterContext(patch.object(self.fake, "build_template",
                                       side_effect=lambda spec, alias, **kw: names.append(alias) or build(spec, alias=alias, **kw)))
        self.amp.wayland_tools()
        self.assertEqual(names, [self.runtime.template_name(self.amp.TOOLS, "fake")])
        self.assertNotEqual(names[0], self.amp.TOOLS)
        self.assertEqual(self.fake.built, {})

    def test_a_refused_template_delete_keeps_the_tools(self):
        with patch.object(self.fake, "delete_template", side_effect=RuntimeError("HTTP 503")):
            self.assertEqual(self.amp.wayland_tools(), TARBALL)
        self.assertEqual(self.amp.cached_tools().read_bytes(), TARBALL)

    def test_the_template_is_deleted_when_the_sandbox_cleanup_fails(self):
        with patch.object(self.runtime, "kill", side_effect=self.runtime.Failed("kill refused")), \
                self.assertRaisesRegex(self.runtime.Failed, "kill refused"):
            self.amp.wayland_tools()
        self.assertEqual(self.fake.built, {})
        self.assertFalse(self.amp.cached_tools().exists())

    def test_a_directory_at_the_cache_path_fails_the_check(self):
        self.amp.cached_tools().mkdir(parents=True)
        self.addCleanup(self.amp.cached_tools().rmdir)
        check = self.amp.Amp._tools_check()
        self.assertFalse(check.ok)
        self.assertIn("is a directory", check.detail)

    def test_the_first_error_wins_when_the_template_delete_fails_too(self):
        with patch.object(self.runtime, "kill", side_effect=self.runtime.Failed("kill refused")), \
                patch.object(self.fake, "delete_template", side_effect=ValueError("delete broke")) as delete, \
                self.assertRaisesRegex(self.runtime.Failed, "kill refused"):
            self.amp.wayland_tools()
        delete.assert_called_once_with(self.runtime.template_name(self.amp.TOOLS, "fake"))
        self.assertFalse(self.amp.cached_tools().exists())

    def test_a_default_that_imports_images_is_chosen_without_making_the_others(self):
        self.enterContext(patch.object(self.runtime, "default", return_value="fake"))
        self.runtime._backends.pop("boat")
        with patch.object(self.runtime, "make", side_effect=ImportError("no boat package")) as make:
            self.assertEqual(self.amp.tools_runtime(), "fake")
            make.assert_not_called()
            self.enterContext(patch.object(self.runtime, "default", return_value="boat"))
            self.assertEqual(self.amp.tools_runtime(), "fake")   # a runtime that can't be made is passed over
