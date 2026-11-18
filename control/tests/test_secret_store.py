"""The secret store: values by namespace and sealed payloads, in the local
store and in Infisical (a fake one over HTTP), moving between them, the
watch that brings Infisical's own edits to sandboxes, and env delivery."""
import importlib
import hashlib
import hmac
import json
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from fake_infisical import FakeInfisical
from support import Server

APP = "owner/app"
PERSON = "person@example.com"


class SecretStoreTests(Server):
    SETTINGS = {"repos": [APP]}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeInfisical()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        from sbx_control import secret_store, settings, state, variables, vault
        self.secret_store, self.settings, self.state, self.variables, self.vault = (
            secret_store, settings, state, variables, vault)
        self.fake.secrets.clear()
        self.fake.webhooks.clear()
        self.fake.down = self.fake.lose_writes = False
        self.repo_admin()

    def infisical(self):
        """Switch to Infisical: a project of its own per test, so nothing is cached across tests."""
        local = self.secret_store.local()
        local.put(self.secret_store.BOOTSTRAP, "INFISICAL_CLIENT_ID", "fixture-id")
        local.put(self.secret_store.BOOTSTRAP, "INFISICAL_CLIENT_SECRET", "fixture-secret")
        self.settings.update("store", {"mode": "infisical", "port": self.fake.port, "project": self._testMethodName,
                                       "environment": "dev"}, by="test")
        return self.secret_store.store()

    def remote(self, namespace):
        return self.fake.secrets.get(self.secret_store.Infisical.path(namespace), {})

    def on_disk(self) -> bytes:
        """Everything sbx.db holds on this host, its write-ahead log included."""
        from sbx_control import paths
        database = paths.database()
        return b"".join(p.read_bytes() for p in (database, database.with_name(database.name + "-wal")) if p.exists())

    def test_values_go_to_their_scopes_folder_and_nothing_stays_local(self):
        store = self.infisical()
        for repo, person, value in ((APP, None, "fixture-shared"), (APP, PERSON, "fixture-personal"),
                                    (None, None, "fixture-installation")):
            self.variables.put(repo, "FEATURE_SECRET", value, "test", person=person)
        self.assertEqual(self.variables.for_sandbox(APP, "other", PERSON)[0]["FEATURE_SECRET"], "fixture-personal")
        self.assertEqual(self.variables.for_sandbox(APP, "other", "other@example.com")[0]["FEATURE_SECRET"], "fixture-shared")
        self.assertEqual(self.variables.values(None)["FEATURE_SECRET"], "fixture-installation")
        self.assertEqual(self.remote(f"repo:{APP}"), {"FEATURE_SECRET": "fixture-shared"})
        self.assertEqual(self.remote(f"repo:{APP}|user:{PERSON}"), {"FEATURE_SECRET": "fixture-personal"})
        self.assertEqual(self.remote(""), {"FEATURE_SECRET": "fixture-installation"})
        self.assertEqual(self.secret_store.local().entries(), [(self.secret_store.BOOTSTRAP, "INFISICAL_CLIENT_ID", "fixture-id"),
                                                               (self.secret_store.BOOTSTRAP, "INFISICAL_CLIENT_SECRET", "fixture-secret")])
        self.assertEqual(store.name, "infisical")

    def test_a_connections_values_are_one_payload_rewritten_in_place_and_gone_with_it(self):
        from sbx_control.catalog import connections
        self.infisical()
        connections.save("fixture-conn", "static", {"token": "fixture-private-one"}, by="test")
        connections.save("fixture-conn", "static", {"token": "fixture-private-two"}, by="test")
        self.assertNotIn(b"fixture-private", self.on_disk())
        self.assertEqual(len(self.remote("objects")), 1)
        self.assertIn("fixture-private-two", json.dumps(self.remote("objects")))
        self.assertNotIn("fixture-private-two", json.dumps(connections.listing()))
        connections.remove("fixture-conn")
        self.assertEqual(self.remote("objects"), {})

    def test_platform_values_are_remote_but_the_stores_own_login_stays_local(self):
        self.infisical()
        self.vault.put(self.vault.key("TEST_PROVIDER"), "fixture-platform-value", by="test")
        self.vault.put(self.vault.INFISICAL_CLIENT_SECRET, "fixture-secret", by="test")
        self.assertEqual(self.vault.get(self.vault.key("TEST_PROVIDER")), "fixture-platform-value")
        self.assertIn("fixture-platform-value", json.dumps(self.fake.secrets))
        self.assertNotIn("fixture-secret", json.dumps(self.fake.secrets))
        self.fake.down = True   # signing in to the store must not need the store
        self.assertEqual(self.vault.get(self.vault.INFISICAL_CLIENT_SECRET), "fixture-secret")

    def test_the_store_reports_the_identity_it_signs_in_with(self):
        self.infisical()  # as the Infisical setup leaves it: the identity in the local store only
        from sbx_control import connections
        shown = connections.get("store").status()
        self.assertEqual((shown.state, shown.summary), ("connected", "Infisical · live updates from its UI"))
        self.secret_store.local().delete(self.secret_store.BOOTSTRAP, "INFISICAL_CLIENT_SECRET")
        self.assertEqual(connections.get("store").status().state, "attention")

    def test_a_payload_infisical_did_not_keep_is_not_referenced(self):
        self.infisical()
        self.fake.lose_writes = True
        with self.assertRaises(self.secret_store.StoreError):
            self.vault.put(self.vault.key("TEST_PROVIDER"), "fixture-private", by="test")
        self.assertIsNone(self.vault.get(self.vault.key("TEST_PROVIDER")))

    def test_a_sandbox_made_for_others_to_drive_gets_no_personal_values(self):
        from sbx_control import core, recipes
        self.variables.put(APP, "FEATURE_SECRET", "fixture-shared", "test")
        self.variables.put(APP, "FEATURE_SECRET", "fixture-personal", "test", person=PERSON)
        a, env = recipes.App(APP, Path("/unused"), {}), {"SBX_OWNER": PERSON}
        values = lambda metadata: core.trusted_values(a, env, {}, "sessions", core.personal(metadata))[0]
        self.assertEqual(values({"connector": "claude"})["FEATURE_SECRET"], "fixture-personal")
        self.assertEqual(values({"connector": "amp", "personal": "off"})["FEATURE_SECRET"], "fixture-shared")

    def test_an_app_that_did_not_come_up_reports_its_log_tail_masked(self):
        # The tail goes into the sandbox's events, which everyone who may view it reads.
        from sbx_control import core, recipes
        self.variables.put(APP, "FEATURE_SECRET", "fixture-repo-secret", "test")
        sbx = Mock()
        sbx.commands.run.return_value.stdout = ("Error: auth failed with fixture-repo-secret\n"
                                                "Error: postgres://app:hunter22@db/app refused\n")
        info = SimpleNamespace(metadata={"sbx": "fixture", "repo": APP, "owner": PERSON})
        with patch.object(core, "start_processes", return_value=""), \
                patch.object(core, "app_script", return_value=SimpleNamespace(stdout="123")), \
                patch.object(core, "run_in", return_value=SimpleNamespace(stdout="exited")), \
                patch.object(core.time, "sleep"), self.assertRaises(core.SandboxError) as raised, core.masked(info):
            core.start_app(sbx, recipes.App(APP, Path("/unused"), {}))
        self.assertNotIn("fixture-repo-secret", str(raised.exception))
        self.assertNotIn("hunter22", str(raised.exception))
        self.assertIn("auth failed with ••••", str(raised.exception))

    def test_every_value_a_sandbox_gets_is_masked_in_what_its_viewers_read(self):
        # A failing .sbx/start's stderr reaches the sandbox's events unredirected; a
        # preview's whole team reads them, and its inspect logs.
        from sbx_control import contributions, core, extensions, lifecycle, recipes, runtime
        self.variables.put(APP, "OWN_TOKEN", "fixture-personal-value", "test", person=PERSON)
        generated = self.variables.random_value("fixture", "SESSION_KEY", False, 24)
        extension = contributions.Contribution("fixture-ext", runtime=contributions.RuntimeLayer(
            env={"EXT_TOKEN": "fixture-extension-token"}))
        info = SimpleNamespace(sandbox_id="fixture-vm", state="running",
                               metadata={"sbx": "fixture", "repo": APP, "owner": PERSON})
        stderr = f"fixture-personal-value {generated} fixture-extension-token"
        op = lifecycle.begin("fixture", "prepare")
        with patch.object(core.recipes, "load", return_value=recipes.App(APP, Path("/unused"), {})), \
                patch.object(extensions, "contributions_for", return_value=[extension]), \
                patch.object(core, "connect", return_value=(Mock(), info)), \
                patch.object(core, "_refresh_env"), patch.object(core, "apply_resources"), \
                patch.object(core, "_prepare",
                             side_effect=runtime.CommandFailed(stderr=stderr, stdout="", exit_code=1)):
            with self.assertRaises(core.SandboxError) as raised:
                core.prepare_and_start("fixture", "main", operation=op)
            inspected = core.mask(stderr, info)
        for shown in (str(raised.exception), inspected):
            for value in ("fixture-personal-value", generated, "fixture-extension-token"):
                self.assertNotIn(value, shown)

    def test_deleting_a_personal_value_keeps_the_repos(self):
        self.infisical()
        self.variables.put(APP, "FEATURE_SECRET", "fixture-shared", "test")
        self.variables.put(APP, "FEATURE_SECRET", "fixture-personal", "test", person=PERSON)
        self.variables.delete(APP, "FEATURE_SECRET", person=PERSON)
        self.assertEqual(self.variables.for_sandbox(APP, "other", PERSON)[0]["FEATURE_SECRET"], "fixture-shared")

    def test_the_store_cannot_move_away_from_the_values_it_holds(self):
        self.infisical()
        self.settings.update("store", {**self.settings.current().store.model_dump(), "project": "moved"}, by="test")
        for hold in (lambda: self.variables.put(APP, "HELD", "fixture-env", "test"),            # an env value
                     lambda: self.variables.random_value("store-test", "TOKEN", False, 32)):   # a sealed payload
            self.infisical()
            hold()
            for change in ({"project": "different-project"}, {"mode": "local"}, {"port": 1}):
                with self.subTest(change=change), self.assertRaises(self.settings.Invalid):
                    self.settings.update("store", {**self.settings.current().store.model_dump(), **change}, by="test")
            self.variables.delete(APP, "HELD")

    def test_every_local_value_moves_to_infisical_verified_and_once(self):
        from sbx_control.catalog import connections, resources
        self.variables.put(APP, "MIGRATE_SECRET", "fixture-repo", "test")
        self.variables.put(APP, "MIGRATE_SECRET", "fixture-personal", "test", person=PERSON)
        self.variables.put(None, "MIGRATE_INSTALLATION", "fixture-installation", "test")
        self.vault.put(self.vault.key("MIGRATE_PLATFORM"), "fixture-platform", by="test")
        generated = self.variables.random_value("migration-test", "GENERATED", False, 32)
        connections.save("migrate-conn", "static", {"url": "postgresql://fixture:private@localhost/db"}, by="test")
        resources.create("migration-test", APP, "endpoint", "static", {"base_url": "https://fixture.invalid"}, 0)
        self.infisical()
        moved = self.secret_store.migrate()
        self.assertGreater(moved, 0)
        self.assertEqual(self.variables.for_sandbox(APP, "other", PERSON)[0]["MIGRATE_SECRET"], "fixture-personal")
        self.assertEqual(self.variables.values(None)["MIGRATE_INSTALLATION"], "fixture-installation")
        self.assertEqual(self.vault.get(self.vault.key("MIGRATE_PLATFORM")), "fixture-platform")
        self.assertEqual(self.variables.random_value("migration-test", "GENERATED", False, 32), generated)
        self.assertEqual(resources.ready("migration-test")["endpoint"].outputs, {"base_url": "https://fixture.invalid"})
        self.assertTrue(all(self.secret_store.Infisical.owns(r) for r in self.secret_store.references()))
        before = json.dumps(self.fake.secrets, sort_keys=True)
        self.assertEqual(self.secret_store.migrate(), 0)
        self.assertEqual(json.dumps(self.fake.secrets, sort_keys=True), before)

    def test_shared_defaults_cannot_replace_values_a_sandbox_owns(self):
        from sbx_control import catalog, core, recipes
        from sbx_control.catalog import resources
        self.infisical()
        for key in ("DATABASE_URL", "DIRECT_DATABASE_URL", "WEB_URL", "GENERATED_SECRET"):
            self.variables.put(APP, key, "shared-do-not-use", "test")
        self.variables.put(APP, "FEATURE_SECRET", "shared-feature", "test")
        app = recipes.App(APP, Path("/unused"), {"env": {
            "DIRECT_DATABASE_URL": "${services.db.url}", "WEB_URL": "${SBX_URL}", "GENERATED_SECRET": "${random:32}"}})
        package = catalog.parse("fixture", {"package.yaml": 'kind: provision\nruns: {create: c.sh}\n'
                                                            'env: {DATABASE_URL: "${outputs.url}"}\n', "c.sh": ""}, "shipped")
        db = resources.Created(package, {"url": "postgresql://workspace-a/db-a"})
        generated = []
        for name in ("a", "b"):
            env = core.sbx_env("store-" + name, name, app, PERSON, "main")
            env["SBX_URL"] = f"https://{name}.example.invalid"
            result, _ = core.trusted_values(app, env, {"db": db}, "other")
            self.assertEqual(result["DATABASE_URL"], db.outputs["url"])
            self.assertEqual(result["DIRECT_DATABASE_URL"], db.outputs["url"])
            self.assertEqual(result["WEB_URL"], env["SBX_URL"])
            self.assertEqual(result["FEATURE_SECRET"], "shared-feature")
            self.assertNotEqual(result["GENERATED_SECRET"], "shared-do-not-use")
            generated.append(result["GENERATED_SECRET"])
        self.assertNotEqual(*generated)

    def test_the_signed_webhook_refuses_tampering_and_old_events(self):
        from sbx_control import infisical_api
        self.vault.put(self.vault.INFISICAL_WEBHOOK_TOKEN, "fixture-signing-key", by="test")

        def deliver(stamp, corrupt=False):
            body = json.dumps({"event": "secrets.modified", "timestamp": stamp}).encode()
            signature = hmac.new(b"fixture-signing-key", body, hashlib.sha256).hexdigest()
            return self.client.post("/webhooks/infisical", content=body + (b" " if corrupt else b""),
                                    headers={"x-infisical-signature": f"t={stamp};{signature}"})
        synced = threading.Event()
        with patch.object(infisical_api, "sync", side_effect=synced.set) as sync:
            self.assertEqual(deliver(int(time.time() * 1000)).status_code, 200)
            self.assertTrue(synced.wait(5))
            self.assertEqual(deliver(int(time.time() * 1000), corrupt=True).status_code, 404)
            self.assertEqual(deliver(int((time.time() - 600) * 1000)).status_code, 404)
        self.assertEqual(sync.call_count, 1)

    def sync(self, repos=(APP,), **apply):
        from sbx_control import infisical_api, sandboxes, views
        with patch.object(views, "known_repos", return_value=list(repos)), \
                patch.object(sandboxes, "deliver_env", **apply) as delivered:
            infisical_api.sync()
        return delivered

    def test_edits_in_infisical_reach_the_repos_sandboxes_by_name_only(self):
        self.infisical()
        self.variables.put(APP, "FEATURE_SECRET", "fixture-old", "test")
        self.variables.for_sandbox(APP, "other")  # what a sandbox read: the baseline
        folder = self.fake.secrets[self.secret_store.Infisical.path(f"repo:{APP}")]
        folder.update(FEATURE_SECRET="fixture-new", ADDED_SECRET="fixture-added")
        delivered = self.sync()
        self.assertEqual(set(delivered.call_args.args[1]), {"FEATURE_SECRET", "ADDED_SECRET"})
        self.assertNotIn("fixture-new", str(delivered.call_args))
        del folder["FEATURE_SECRET"]
        self.assertEqual(self.sync().call_args.args[1], ["FEATURE_SECRET"])
        self.assertNotIn("FEATURE_SECRET", self.variables.for_sandbox(APP, "other")[0])

    def test_an_installation_value_reaches_every_trusted_repo(self):
        self.infisical()
        self.settings.add_repo("owner/other", by="test")
        self.variables.values(None)
        self.fake.secrets.setdefault("/_sbx/installation", {})["SHARED"] = "new"
        delivered = self.sync(repos=(APP, "owner/other", "stranger/repo"))
        self.assertEqual({c.args[0] for c in delivered.call_args_list if c.args[1] == ["SHARED"]}, {APP, "owner/other"})

    def test_a_personal_edit_reaches_only_its_owners_sandboxes(self):
        self.infisical()
        self.variables.put(APP, "PERSONAL_SECRET", "old", "test", person=PERSON)
        self.variables.for_sandbox(APP, "other", PERSON)
        self.fake.secrets[self.secret_store.Infisical.path(f"repo:{APP}|user:{PERSON}")]["PERSONAL_SECRET"] = "new"
        self.sync().assert_called_once_with(APP, ["PERSONAL_SECRET"], person=PERSON)

    def test_an_edit_stays_pending_when_its_delivery_fails(self):
        store = self.infisical()
        self.variables.put(APP, "FEATURE_SECRET", "old", "test")
        self.variables.for_sandbox(APP, "other")
        self.fake.secrets[self.secret_store.Infisical.path(f"repo:{APP}")]["FEATURE_SECRET"] = "new"
        self.sync(side_effect=RuntimeError("fixture runtime unavailable"))
        self.assertEqual(store.changes(f"repo:{APP}")[0], ["FEATURE_SECRET"])

    def test_a_deletion_is_noticed_when_the_value_was_read_between_polls(self):
        store = self.infisical()
        store.list(f"repo:{APP}")  # the first poll's empty baseline
        self.variables.put(APP, "SHORT_LIVED", "fixture-ephemeral", "test")
        self.assertEqual(store.list(f"repo:{APP}")["SHORT_LIVED"], "fixture-ephemeral")
        del self.fake.secrets[self.secret_store.Infisical.path(f"repo:{APP}")]["SHORT_LIVED"]  # in Infisical's UI
        names, snapshot = store.changes(f"repo:{APP}")
        self.assertEqual(names, ["SHORT_LIVED"])
        store.acknowledge(f"repo:{APP}", snapshot)
        self.assertEqual(store.changes(f"repo:{APP}")[0], [])

    def test_sbx_s_own_writes_do_not_come_back_as_changes_but_edits_in_infisical_do(self):
        # A sandbox's services sealed while it is made would otherwise bring every
        # running sandbox an env_update at the next poll, a failed one's too.
        self.infisical()
        self.variables.put(APP, "FEATURE_SECRET", "fixture-before", "test")
        self.sync()  # the first look
        reference = self.secret_store.seal({"password": "fixture"}, key="service/fixture-db/inputs")
        self.secret_store.seal({"password": "fixture"}, key="service/fixture-cache/inputs")
        self.variables.put(APP, "FEATURE_SECRET", "fixture-own", "test")
        self.variables.put(APP, "ADDED_SECRET", "fixture-added", "test")
        self.variables.delete(APP, "ADDED_SECRET")
        self.sync().assert_not_called()
        self.secret_store.discard(reference)
        self.sync().assert_not_called()
        objects = self.remote(self.secret_store.OBJECTS)
        objects[next(iter(objects))] = "edited in Infisical"
        self.remote(f"repo:{APP}")["FEATURE_SECRET"] = "fixture-edited"
        delivered = self.sync()
        self.assertIn(((APP, []),), [c[:1] for c in delivered.call_args_list])
        self.assertIn(((APP, ["FEATURE_SECRET"]),), [c[:1] for c in delivered.call_args_list])

    def test_the_webhook_is_set_up_for_every_folder(self):
        from sbx_control import infisical_api
        self.infisical()
        self.settings.update("domain", {**self.settings.current().domain.model_dump(), "name": "sbx.example.com"}, by="test")
        self.sync()
        self.assertEqual(self.fake.webhooks, [{"url": "https://app.sbx.example.com/webhooks/infisical", "secretPath": "/**"}])
        self.assertTrue(self.vault.get(self.vault.INFISICAL_WEBHOOK_TOKEN))
        infisical_api.sync()
        self.assertEqual(len(self.fake.webhooks), 1)

    def test_a_value_deleted_in_infisical_can_still_be_removed_through_the_api(self):
        from sbx_control import sandboxes
        store = self.infisical()
        self.variables.put(APP, "REMOVED_EXTERNALLY", "fixture-private", "test")
        store.delete(f"repo:{APP}", "REMOVED_EXTERNALLY")
        with patch.object(sandboxes, "deliver_env", return_value={"running": 0, "paused": 0}) as deliver:
            response = self.client.delete(f"/api/repos/{APP}/env/REMOVED_EXTERNALLY")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(deliver.call_args.args[:2], (APP, ["REMOVED_EXTERNALLY"]))
        self.assertNotIn("REMOVED_EXTERNALLY", self.variables.names(APP))

    def test_gone_sandboxes_values_are_removed_and_retried_while_the_store_is_down(self):
        from sbx_control import core, delivery, lifecycle
        from sbx_control.catalog import resources
        self.infisical()
        for name in ("secret-live", "secret-creating", "secret-gone"):
            self.variables.random_value(name, "TOKEN", False, 32)
        delivery.queue("secret-gone", ["FEATURE"], True)
        lifecycle.begin("secret-creating", "create")
        with patch.object(core, "sandboxes", return_value=[Mock(metadata={"sbx": "secret-live"})]), \
                patch.object(resources, "reconcile"):
            self.fake.down = True
            core.reconcile_services()
            self.assertIsNotNone(delivery.pending("secret-gone"))
            self.fake.down = False
            core.reconcile_services()
        self.assertEqual(self.variables.kept(), {"secret-live", "secret-creating"})
        self.assertIsNone(delivery.pending("secret-gone"))
        self.assertEqual(len(self.remote("objects")), 2)

    def test_env_api_queues_delivery_for_the_running_and_paused_sandboxes_it_concerns(self):
        from sbx_control import core, delivery, recipes, sandboxes
        self.infisical()
        running = Mock(metadata={"sbx": "store-running", "owner": "operator@example.com"}, state="running")
        paused = Mock(metadata={"sbx": "store-paused", "owner": "operator@example.com"}, state="paused")
        other = Mock(metadata={"sbx": "store-other", "owner": "someone@example.com"}, state="running")
        with patch.object(core, "sandboxes", return_value=[running, paused, other]), \
                patch.object(core, "app_of", return_value=recipes.App(APP, Path("/unused"), {})), \
                patch.object(sandboxes, "deliver_pending"):
            response = self.client.put(f"/api/repos/{APP}/env/PERSONAL_SECRET", json={
                "value": "fixture-personal", "secret": True, "mine": True, "restart": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("fixture-personal", response.text)
        self.assertIsNotNone(delivery.pending("store-running"))
        self.assertIsNotNone(delivery.pending("store-paused"))
        self.assertIsNone(delivery.pending("store-other"))

    def test_a_value_set_while_the_runtime_is_down_is_stored_and_delivered_later(self):
        from sbx_control import core, delivery, keeper, recipes, sandboxes
        self.infisical()
        with patch.object(core, "sandboxes", side_effect=ConnectionError("fixture runtime down")):
            response = self.client.put(f"/api/repos/{APP}/env/LATE_VALUE", json={"value": "fixture-late"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["pending"])
        self.assertEqual(self.variables.values(APP)["LATE_VALUE"], "fixture-late")
        running = Mock(metadata={"sbx": "late-running", "owner": "operator@example.com"}, state="running")
        with patch.object(core, "sandboxes", return_value=[running]), \
                patch.object(sandboxes, "registered", return_value=[]), \
                patch.object(core, "app_of", return_value=recipes.App(APP, Path("/unused"), {})), \
                patch.object(sandboxes, "deliver_pending"):
            keeper.look_once()
        self.assertEqual(delivery.pending("late-running")[0], ["LATE_VALUE"])
        self.assertEqual(delivery.take_repos(), [])

    def test_an_installation_secret_reaches_every_trusted_repo(self):
        from sbx_control import sandboxes
        with patch.object(sandboxes, "deliver_env", return_value={"running": 1, "paused": 0}) as deliver:
            response = self.client.put("/api/secrets/SHARED_KEY", json={"value": "fixture-shared"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.variables.values(None)["SHARED_KEY"], "fixture-shared")
        self.assertIn((APP, ["SHARED_KEY"]), [c.args[:2] for c in deliver.call_args_list])
        self.assertEqual(response.json()["running"], deliver.call_count)   # the sum over the repos

    def test_installation_secrets_are_no_second_way_to_set_a_repo_s_values(self):
        from sbx_control import sandboxes
        with patch.object(sandboxes, "deliver_env", return_value={"running": 0, "paused": 0}):
            response = self.client.put(f"/api/secrets/ONLY_SHARED?repo={APP}", json={"value": "fixture-shared"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.variables.values(None)["ONLY_SHARED"], "fixture-shared")
        self.assertNotIn("ONLY_SHARED", self.variables.values(APP))

    def test_backup_seals_infisicals_files_and_restores_them(self):
        from sbx_control import backup, paths
        infisical_store = importlib.import_module("sbx_control.secret_store.infisical")   # the module, not secret_store.infisical()
        self.infisical()
        (paths.etc() / "infisical.env").write_text("AUTH_SECRET=fixture-secret-backup")
        (paths.etc() / "infisical-admin").write_text("fixture-private-admin")
        key = paths.secret_key().read_text().strip()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.tgz"
            with patch.object(infisical_store.subprocess, "run", return_value=Mock(stdout=b"-- the store's dump")) as run:
                manifest = backup.bundle(path)
                self.assertIn("pg_dump", run.call_args.args[0])
                # Scheduled bundles: the newest `keep` stay, one made by hand too.
                self.settings.update("backup", {"to": directory, "keep": 1}, by="test")
                (Path(directory) / "by-hand.tar.gz").write_bytes(b"")
                made = []
                for stamp in ("20260101-000000", "20260102-000000"):
                    with patch.object(backup.time, "strftime",
                                      side_effect=lambda fmt, *a, stamp=stamp: fmt.replace("%Y%m%d-%H%M%S", stamp)
                                      if fmt.startswith(backup.SCHEDULED) else "2026-01-01T00:00:00Z"):
                        made.append(backup.scheduled())
            self.assertEqual(sorted(p.name for p in Path(directory).glob("*.tar.gz")),
                             ["by-hand.tar.gz", made[-1].name])
            self.assertIn("infisical.sql.gz", manifest["parts"])
            with tarfile.open(path) as archive:
                self.assertNotIn(b"fixture-private-admin", archive.extractfile("infisical-admin.sealed").read())
                self.assertNotIn(b"fixture-secret-backup", archive.extractfile("infisical.env.sealed").read())
            (paths.etc() / "infisical-admin").unlink()
            backup.restore(path, key)
            self.assertEqual((paths.etc() / "infisical-admin").read_text(), "fixture-private-admin")
        (paths.etc() / "infisical.sql.gz").unlink()

    def shared_password(self) -> str:
        """An installation from before passwords per person, its shared one in Infisical."""
        self.infisical()
        self.settings.update("access", {"admins": ["first@example.com"]}, by="test")
        self.settings.update("sign_in", {"method": "password"}, by="test")
        reference = self.secret_store.seal("old-shared-password", key="platform/LOGIN_PASSWORD")
        with self.state.db() as conn:
            conn.execute("insert into sealed (key, value, updated_by) values ('LOGIN_PASSWORD', ?, 'test')", (reference,))
        return reference

    def test_an_upgrade_keeps_the_shared_passwords_payload_while_a_rollback_may_need_it(self):
        import shutil
        import sqlite3
        from contextlib import closing
        from sbx_control import paths, users
        reference = self.shared_password()
        backup = paths.state_dir() / "upgrade-backups" / "20261117-221926"
        backup.mkdir(parents=True)
        with closing(sqlite3.connect(paths.database())) as live, closing(sqlite3.connect(backup / "sbx.db")) as copy:
            live.backup(copy)   # what install.sh keeps before it starts the new release
        self.state.migrate()
        self.assertIsNotNone(users.check_password("first@example.com", "old-shared-password"))
        self.secret_store.discard_released()
        # rollback.sh brings back the row; the release before reads its payload when it starts.
        self.assertEqual(self.secret_store.unseal(reference), "old-shared-password")
        # While a rollback may bring it back, the store can't move away from it.
        with self.assertRaisesRegex(Exception, "holds sealed values"):
            self.settings.update("store", {"project": "elsewhere"}, by="test")
        shutil.rmtree(backup)
        self.secret_store.discard_released()
        with self.assertRaises(self.secret_store.StoreError):
            self.secret_store.unseal(reference)
        self.secret_store.discard_released()   # nothing left to do

    def test_a_platform_secret_whose_payload_is_gone_does_not_stop_sbx(self):
        from sbx_control import users
        reference = self.shared_password()
        self.secret_store.discard(reference)
        self.vault.platform_secrets()   # what starting sbx reads; LOGIN_PASSWORD is just missing
        self.state.migrate()            # nothing to carry over: the row goes
        with self.state.db() as conn:
            self.assertIsNone(conn.execute("select 1 from sealed where key = 'LOGIN_PASSWORD'").fetchone())
        self.assertIsNone(users.check_password("first@example.com", "old-shared-password"))


class DeliveryTests(Server):
    """Env deliveries: retried until applied, also across a pause. The VM is
    faked where sbx-control reaches it: the E2B SDK (find, connect) and the
    core functions that read, write and start things inside it."""
    NAME = "delivery-fixture"

    def setUp(self):
        super().setUp()
        from sbx_control import core, delivery, lifecycle, recipes, sandboxes
        self.core, self.delivery, self.sandboxes = core, delivery, sandboxes
        self.repo_admin()
        op = lifecycle.begin(self.NAME, "create")   # a sandbox whose workspace and services are ready
        op.advance("provisioning_services")
        op.advance("services_ready")
        op.advance("allocating")
        op.advance("preparing_workspace", sandbox_id="delivery-vm", runtime="running")
        op.advance("workspace_ready", finish=True)
        self.info = SimpleNamespace(sandbox_id="delivery-vm", state="running", metadata={"sbx": self.NAME})
        self.vm = Mock()
        self.vm.commands.run.return_value.stdout = "ok"
        self.enterContext(patch.object(core, "find", return_value=self.info))
        self.enterContext(patch.object(core.runtime, "connect", return_value=self.vm))
        self.enterContext(patch.object(core, "app_of", return_value=recipes.App(APP, Path("/unused"), {})))
        self.enterContext(patch.object(core, "open_desktop_browser"))
        self.enterContext(patch.object(core.recipes, "ship"))

    def deliver(self, name, names):
        """A change queued for the sandbox, then delivered to it."""
        self.delivery.queue(name, names, True)
        self.sandboxes.deliver_pending(name)

    def env(self, wanted, written):
        """The env sbx-control would write now, and what the VM's env files hold (one read per call)."""
        from sbx_control import sandbox_runtime
        self.enterContext(patch.object(sandbox_runtime, "runtime_values", return_value=(wanted, set(), Mock())))
        self.enterContext(patch.object(sandbox_runtime, "written_env", side_effect=written))
        return self.enterContext(patch.object(sandbox_runtime, "write_runtime"))

    def test_a_failed_delivery_is_retried_and_a_newer_change_is_not_acknowledged(self):
        from sbx_control import core
        with patch.object(core, "refresh_env", side_effect=RuntimeError("fixture unavailable")):
            self.deliver("delivery-test", ["FIRST"])
        self.assertIsNotNone(self.delivery.pending("delivery-test"))
        calls = []

        def deliver(name, restart):
            calls.append(name)
            if len(calls) == 1:
                self.delivery.queue(name, ["SECOND"], True)
            return {"changed": ["FIRST"], "restarted": True}
        with patch.object(core, "refresh_env", side_effect=deliver):
            self.deliver("delivery-test", ["FIRST"])
        self.assertEqual(len(calls), 2)  # the change that came in meanwhile is delivered too
        self.assertIsNone(self.delivery.pending("delivery-test"))

    def test_a_delivery_waits_for_the_sandbox_s_create_or_delete(self):
        """A spawn holding the sandbox's operation lock (creating it, then
        starting its app) is not interrupted by an env_update."""
        import threading
        from sbx_control import core
        delivered, contending = threading.Event(), threading.Event()
        flock = core.fcntl.flock
        worker = None

        def observed(fd, operation):   # the worker reaching the real lock, before it blocks on it
            if threading.current_thread() is worker:
                contending.set()
            return flock(fd, operation)
        with patch.object(core, "refresh_env", side_effect=lambda name, restart: (
                delivered.set(), {"changed": [], "restarted": False})[1]), \
                patch.object(core.fcntl, "flock", observed):
            with core.sandbox_operation("delivery-test"):
                self.delivery.queue("delivery-test", ["FIRST"], True)
                worker = threading.Thread(target=self.sandboxes.deliver_pending, args=("delivery-test",), daemon=True)
                worker.start()
                self.assertTrue(contending.wait(5))
                self.assertFalse(delivered.wait(0.3))
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(delivered.is_set())
        self.assertIsNone(self.delivery.pending("delivery-test"))

    def test_a_value_the_sandbox_no_longer_gets_leaves_its_env(self):
        write = self.env({"KEPT": "1"}, [{"KEPT": "1", "REMOVED": "old"}])
        done = self.core.refresh_env(self.NAME, restart=False)
        self.assertEqual(done, {"changed": ["REMOVED"], "restarted": False})
        self.assertEqual(write.call_args.args[2], {"KEPT": "1"})

    def test_resume_delivers_queued_changes_before_returning(self):
        self.delivery.queue(self.NAME, ["FEATURE"], restart=True)
        self.info.state = "paused"
        write = self.env({"FEATURE": "new"}, [{"FEATURE": "old"}])
        with patch.object(self.core, "start_app") as start:
            self.assertFalse(self.core.restore_runtime(self.vm, self.info))   # the app already restarted
        self.assertEqual(write.call_args.args[2], {"FEATURE": "new"})
        start.assert_called_once()
        self.assertIsNone(self.delivery.pending(self.NAME))

    def test_resume_keeps_a_failed_delivery(self):
        self.delivery.queue(self.NAME, ["FEATURE"], restart=True)
        self.info.state = "paused"
        self.env({"FEATURE": "new"}, [{"FEATURE": "old"}]).side_effect = OSError("fixture transient outage")
        with self.assertRaises(self.core.SandboxError):   # masked: its viewers read it
            self.core.restore_runtime(self.vm, self.info)
        self.assertIsNotNone(self.delivery.pending(self.NAME))

    def test_a_failed_app_restart_is_retried_after_the_env_files_changed(self):
        self.env({"FEATURE": "two"}, [{"FEATURE": "one"}, {"FEATURE": "two"}])
        with patch.object(self.core, "start_app", side_effect=[RuntimeError("fixture startup failed"), None]) as start:
            self.deliver(self.NAME, ["FEATURE"])
            self.assertIsNotNone(self.delivery.pending(self.NAME))
            self.deliver(self.NAME, ["FEATURE"])   # the env files now match: the restart still runs
        self.assertEqual(start.call_count, 2)
        self.assertIsNone(self.delivery.pending(self.NAME))
