"""Runtime settings: each backend declares its settings; admins
add, change, remove a runtime and make one the default from the web app,
checked by sbx-control and queued for the root side (runtime_changes), a
secret only ever as a reference into the managed store; an installation
with no runtime yet; the limits a provider refused, recorded and named."""
import io
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from support import RUNTIME, Installation, Server
from test_runtimes import TwoRuntimes

KEY = "test-only-cloud-key-5f3a"


class Schema(Installation):
    def test_e2b_cloud_declares_its_key_domain_and_limits(self):
        from sbx_control.runtime.e2b_cloud import Cloud
        cloud = Cloud()
        keys = [f.key for f in cloud.settings]
        self.assertTrue(set(keys) <= set(cloud.credential_keys))
        self.assertEqual([f.key for f in cloud.settings if f.secret], [cloud.secret_key])
        self.assertEqual({f.limit for f in cloud.settings if f.limit},
                         {"timeout_seconds", "cpu", "memory_mb", "disk_mb", "sandboxes"})
        self.assertTrue(all(f.kind == "int" and f.default.isdigit() for f in cloud.settings if f.limit))
        self.assertEqual([p.name for p in cloud.presets], ["hobby", "pro"])
        for preset in cloud.presets:
            self.assertEqual(set(preset.by_key()), {f.key for f in cloud.settings if f.limit})
            self.assertIn("e2b.dev/pricing", preset.source)
        self.assertEqual(cloud.presets[0].by_key()["E2B_CLOUD_MAX_TIMEOUT"], "3600")

    def test_embed_is_install_sh_s_and_says_when_the_host_cannot_run_it(self):
        from sbx_control.runtime.embed import Embed
        embed = Embed()
        self.assertEqual(embed.settings, ())
        self.assertIn("/dev/kvm", embed.can_run({"kvm": False}))
        self.assertEqual(embed.can_run({"kvm": True, "memory_gb": 4}),
                         "needs 16 GiB of memory: 12 for the host and 4 for one sandbox; this server has 4 GiB")
        self.assertIn("disk", embed.can_run({"kvm": True, "memory_gb": 64, "disk_free_gb": 20}))
        self.assertEqual(embed.can_run({"kvm": True, "memory_gb": 64, "disk_free_gb": 500}), "")

    def test_a_backend_s_form_is_its_settings(self):
        from sbx_control import runtime_settings
        from sbx_control.runtime.e2b_cloud import Cloud
        cloud = Cloud()
        self.assertEqual(list(runtime_settings.fields(cloud)), [f.key for f in cloud.settings])


class Web(TwoRuntimes, Server):
    """Embed (a stand-in) and the fake enabled; E2B Cloud offered, its
    provider calls stood in for."""

    def setUp(self):
        super().setUp()
        from sbx_control import runtime, secret_store, settings, template_scheduler, vault
        from sbx_control.connections.base import Check
        from sbx_control.runtime.e2b_cloud import Cloud
        self.file = self.root / "etc/runtime.env"
        self.addCleanup(self.file.write_text, self.RUNTIME_ENV)
        self.checks = [Check("E2B Cloud API", True, "answers")]
        self.enterContext(patch.object(Cloud, "test", side_effect=lambda: self.checks))
        self.enterContext(patch.object(Cloud, "sandboxes", side_effect=lambda *_: []))
        self.builds = self.enterContext(patch.object(template_scheduler, "start_detached",
                                                     return_value=("unit", True)))
        settings.update("domain", {**settings.current().domain.model_dump(), "name": "sbx.example.com"}, by="test")
        # The managed store, as references: sealing makes one per value.
        self.store, made = {}, iter(range(1, 1000))

        def seal(value, *, key):
            reference = f"infisical-ref-{next(made)}"
            self.store[reference] = value
            return reference
        self.sealing = self.enterContext(patch.object(vault, "sealing", return_value=True))
        for patcher in (patch.object(secret_store, "seal", side_effect=seal),
                        patch.object(secret_store, "is_reference",
                                     side_effect=lambda v: str(v).startswith("infisical-ref-")),
                        patch.object(secret_store, "unseal", side_effect=lambda r: self.store[r]),
                        patch.object(secret_store, "discard", side_effect=lambda r: self.store.pop(r, None))):
            self.enterContext(patcher)
        self.request_file = self.root / "run/apply/runtime-change"
        self.addCleanup(lambda: (self.root / "state/runtime-change.json").unlink(missing_ok=True))
        self.addCleanup(self.request_file.unlink, missing_ok=True)
        self.runtime = runtime
        self.effects()

    def call(self, method, url, body=None):
        return self.client.request(method, url, headers=self.headers, json=body)

    def apply(self):
        from sbx_control import runtime_changes
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", out):
            code = runtime_changes.apply_pending()
        return code, out.getvalue()

    def test_available_lists_every_runtime_with_its_form_and_never_a_secret(self):
        self.file.write_text(self.RUNTIME_ENV.replace("FAKE_RUNTIME_TOKEN=test-only-fake", f"FAKE_RUNTIME_TOKEN={KEY}"))
        response = self.call("GET", "/api/runtimes/available")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(KEY, response.text)
        found = {r["kind"]: r for r in response.json()}
        self.assertEqual(set(found), {"embed", "e2b-cloud", "boat", "fake"})
        embed, cloud, fake = found["embed"], found["e2b-cloud"], found["fake"]
        self.assertEqual((embed["enabled"], embed["host"], embed["managed_by"]), (True, True, "install.sh"))
        self.assertEqual(embed["facts"], {"kvm": True, "memory_gb": 125, "disk_free_gb": 1618})
        self.assertEqual(embed["remove_blocker"]["code"], "host")
        self.assertIn("install.sh", embed["remove_blocker"]["message"])
        self.assertEqual(embed["settings"], [])                          # install.sh's keys, never a form
        self.assertEqual((cloud["enabled"], cloud["can_run"], cloud["edit_blocker"]), (False, True, None))
        self.assertEqual([p["name"] for p in cloud["presets"]], ["hobby", "pro"])
        boat = found["boat"]   # the same form machinery: its keys, plans and limits, nothing Boat-specific in core
        self.assertEqual((boat["enabled"], boat["can_run"], boat["title"]), (False, True, "Boat"))
        self.assertEqual([p["name"] for p in boat["presets"]], ["trial", "20", "100", "500", "2000"])
        self.assertIn("templates need the first payment", boat["presets"][0]["note"])
        self.assertEqual([s["key"] for s in boat["settings"]][:3], ["BOAT_API_KEY", "BOAT_API_URL", "BOAT_ORG"])
        self.assertEqual(fake["secrets_set"], ["FAKE_RUNTIME_TOKEN"])
        self.assertEqual(fake["values"], {})
        self.assertEqual((fake["sandboxes"], fake["remove_blocker"]), ([], None))
        self.assertEqual(self.client.get("/api/runtimes/available").status_code, 401)   # no token, no session

    def test_adding_seals_the_key_queues_a_reference_and_the_root_side_applies_it(self):
        response = self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {
            "E2B_CLOUD_API_KEY": KEY, "E2B_CLOUD_MAX_TIMEOUT": "86400", "E2B_CLOUD_DOMAIN": ""}})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["state"], "queued")
        self.assertNotIn(KEY, response.text)
        request = json.loads(self.request_file.read_text())
        self.assertNotIn(KEY, self.request_file.read_text())          # only a reference, never the key
        self.assertEqual(self.request_file.stat().st_mode & 0o777, 0o600)
        reference = request["values"]["E2B_CLOUD_API_KEY"]
        self.assertEqual(self.store[reference], KEY)
        self.assertEqual((request["operation"], request["kind"], request["values"]["E2B_CLOUD_MAX_TIMEOUT"]),
                         ("add", "e2b-cloud", "86400"))
        self.assertNotIn("E2B_CLOUD_DOMAIN", request["values"])        # empty and unset: no change
        self.assertFalse((self.root / "state/runtime-change.json").exists())   # the root side records
        busy = self.call("PUT", "/api/runtimes/default", {"kind": "fake"})
        self.assertEqual(busy.status_code, 409)                         # one change at a time
        from sbx_control import setup
        self.assertFalse(setup.complete())
        code, said = self.apply()   # the setup is not finished: an added runtime builds all the same
        self.assertEqual(code, 0, said)
        self.assertFalse(self.request_file.exists())
        text = self.file.read_text()
        self.assertIn("SBX_RUNTIMES=embed,fake,e2b-cloud", text)
        self.assertIn(f"E2B_CLOUD_API_KEY={reference}", text)
        self.assertNotIn(KEY, text)
        self.assertIn("E2B_CLOUD_MAX_TIMEOUT=86400", text)
        self.assertEqual(self.effects(), ["control", "tls"])
        self.builds.assert_called_once_with("e2b-cloud")
        status = self.call("GET", "/api/runtimes/change").json()
        self.assertEqual((status["state"], status["operation"], status["kind"]), ("done", "add", "e2b-cloud"))
        self.assertIn("E2B Cloud (e2b-cloud) is enabled", status["message"])
        self.assertNotIn(KEY, json.dumps(status))
        self.assertEqual(self.runtime.limits("e2b-cloud").timeout_seconds, 86400)

    def test_a_change_keeps_a_secret_left_empty_and_replaces_one_typed(self):
        self.assertEqual(self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {
            "E2B_CLOUD_API_KEY": KEY}}).status_code, 202)
        self.assertEqual(self.apply()[0], 0)
        first = next(r for r in self.store if self.store[r] == KEY)
        response = self.call("PUT", "/api/runtimes/e2b-cloud", {"values": {"E2B_CLOUD_API_KEY": "",
                                                                           "E2B_CLOUD_MAX_SANDBOXES": "100"}})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(json.loads(self.request_file.read_text())["values"], {"E2B_CLOUD_MAX_SANDBOXES": "100"})
        self.assertEqual(self.apply()[0], 0)
        self.assertIn(f"E2B_CLOUD_API_KEY={first}", self.file.read_text())   # kept
        self.assertEqual(self.call("PUT", "/api/runtimes/e2b-cloud", {"values": {
            "E2B_CLOUD_API_KEY": "test-only-second"}}).status_code, 202)
        self.assertEqual(self.apply()[0], 0)
        self.assertNotIn(first, self.store)                                  # the old one, once confirmed
        self.assertIn("test-only-second", self.store.values())
        nothing = self.call("PUT", "/api/runtimes/e2b-cloud", {"values": {"E2B_CLOUD_MAX_SANDBOXES": "100"}})
        self.assertEqual(nothing.status_code, 400)
        self.assertIn("nothing to change", nothing.json()["detail"])

    def test_a_failed_apply_puts_everything_back_and_says_why(self):
        from sbx_control import platform
        before = self.file.read_text()
        self.assertEqual(self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {
            "E2B_CLOUD_API_KEY": KEY}}).status_code, 202)
        with patch.object(platform, "connect", side_effect=[RuntimeError("the store did not answer"), None]):
            code, said = self.apply()
        self.assertEqual(code, 1)
        self.assertEqual(self.file.read_text(), before)
        self.assertEqual(self.store, {})                                # the queued reference is gone too
        self.assertEqual(self.effects(), [])
        status = self.call("GET", "/api/runtimes/change").json()
        self.assertEqual(status["state"], "failed")
        self.assertIn("the store did not answer", status["message"])
        # Refused at the root side before anything was written: the reference goes all the same.
        self.assertEqual(self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {
            "E2B_CLOUD_API_KEY": KEY}}).status_code, 202)
        from sbx_control.connections.base import Check
        self.checks = [Check("E2B Cloud API", False, "HTTP 401")]
        code, said = self.apply()
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", said)
        self.assertEqual(self.store, {})

    def test_the_local_store_refuses_adding_and_changing_but_not_removing(self):
        self.sealing.return_value = False
        response = self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {"E2B_CLOUD_API_KEY": KEY}})
        self.assertEqual(response.status_code, 409)
        self.assertIn("sbx-admin runtime add e2b-cloud", response.json()["detail"])
        listed = {r["kind"]: r for r in self.call("GET", "/api/runtimes/available").json()}
        self.assertEqual(listed["e2b-cloud"]["edit_blocker"]["code"], "local_store")              # the UI says it
        self.assertIn("sbx-admin runtime add e2b-cloud", listed["e2b-cloud"]["edit_blocker"]["message"])
        self.assertEqual(self.call("PUT", "/api/runtimes/fake", {"values": {"FAKE_RUNTIME_TOKEN": "x"}}).status_code,
                         409)
        self.assertEqual(self.store, {})
        self.assertFalse(self.request_file.exists())
        self.assertEqual(self.call("PUT", "/api/runtimes/default", {"kind": "fake"}).status_code, 202)
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(self.runtime.default(), "fake")
        self.assertEqual(self.call("DELETE", "/api/runtimes/fake").status_code, 202)
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(self.runtime.enabled(), ["embed"])

    def test_refusals_before_anything_is_queued(self):
        from sbx_control.connections.base import Check
        cases = [
            ("POST", "/api/runtimes", {"kind": "nowhere", "values": {}}, 400, "there is no runtime nowhere"),
            ("POST", "/api/runtimes", {"kind": "embed", "values": {}}, 400, "install.sh"),
            ("POST", "/api/runtimes", {"kind": "fake", "values": {}}, 409, "enabled already"),
            ("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {}}, 400, "needs its API key"),
            ("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {"E2B_CLOUD_API_KEY": KEY,
                                                                       "E2B_CLOUD_MAX_CPU": "two"}}, 400, "whole number"),
            ("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {"E2B_CLOUD_API_KEY": KEY, "OTHER": "x"}},
             400, "no setting OTHER"),
            ("PUT", "/api/runtimes/e2b-cloud", {"values": {"E2B_CLOUD_API_KEY": KEY}}, 400, "add it first"),
            ("PUT", "/api/runtimes/default", {"kind": "e2b-cloud"}, 400, "not enabled"),
            ("DELETE", "/api/runtimes/embed", None, 409, "install.sh"),
            ("DELETE", "/api/runtimes/e2b-cloud", None, 400, "not enabled"),
        ]
        for method, url, body, status, said in cases:
            with self.subTest(method=method, url=url, body=body):
                response = self.call(method, url, body)
                self.assertEqual(response.status_code, status, response.text)
                self.assertIn(said, response.json()["detail"])
        from sbx_control import howto
        self.assertIn(f"(how to: {howto.E2B_KEY})", self.call("POST", "/api/runtimes", cases[3][2]).json()["detail"])
        self.checks = [Check("E2B Cloud API", False, "HTTP 401", help_url=howto.E2B_KEY)]
        response = self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {"E2B_CLOUD_API_KEY": KEY}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("HTTP 401", response.json()["detail"])
        self.assertIn(f"(how to: {howto.E2B_KEY})", response.json()["detail"])   # the page for a refused key
        self.assertNotIn(KEY, response.text)
        self.assertEqual(self.store, {})
        self.assertFalse(self.request_file.exists())

    def test_removing_is_refused_while_sandboxes_run_there_naming_them(self):
        self.fake.create("tpl", timeout=60, metadata={"sbx": "busy-one", "sbx_install": self.runtime.installation_id()})
        response = self.call("DELETE", "/api/runtimes/fake")
        self.assertEqual(response.status_code, 409)
        self.assertIn("busy-one", response.json()["detail"])
        listed = {r["kind"]: r for r in self.call("GET", "/api/runtimes/available").json()}
        self.assertEqual(listed["fake"]["sandboxes"], ["busy-one"])
        self.assertEqual(listed["fake"]["remove_blocker"], {"code": "has_sandboxes", "sandboxes": ["busy-one"],
                                                            "message": response.json()["detail"]})

    def test_a_domain_is_needed_before_a_provider_s_runtime(self):
        from sbx_control import settings
        settings.update("domain", {**settings.current().domain.model_dump(), "name": ""}, by="test")
        response = self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {"E2B_CLOUD_API_KEY": KEY}})
        self.assertEqual(response.status_code, 409)
        self.assertIn("connect a domain first", response.json()["detail"])

    def test_the_setup_page_runs_every_step_before_the_account_with_the_setup_token_alone(self):
        """Domain → sandboxes → coding agents → the account last (which finishes): no session until then."""
        from sbx_control import settings, vault
        from sbx_control.connectors.amp import orbs
        self.assertFalse(settings.current().admins)
        self.assertFalse(self.client.cookies)
        steps = [("GET", "/api/setup", None), ("GET", "/api/runtimes/available", None),
                 ("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {"E2B_CLOUD_API_KEY": KEY}}),
                 ("GET", "/api/runtimes/change", None), ("GET", "/api/connections", None),
                 ("PUT", "/api/setup/agents", {"agents": ["amp"]}), ("GET", "/api/templates", None)]
        for method, url, body in steps:
            with self.subTest(url=url):
                response = self.call(method, url, body)
                self.assertIn(response.status_code, (200, 202), response.text)
                self.assertEqual(self.client.request(method, url, json=body).status_code, 401)   # not without it
        self.assertEqual(self.apply()[0], 0)
        self.assertIn("e2b-cloud", self.runtime.enabled())
        with patch("sbx_control.connectors.amp.active", return_value=False), patch.object(orbs, "runtime"):
            token = "aop_live_disposable_test_only"
            put = self.call("PUT", "/api/connections/amp", {"values": {"provider_token": token, "enabled": "true"}})
            self.assertEqual(put.status_code, 200, put.text)
            self.assertNotIn(token, put.text)
            self.assertEqual(vault.get(orbs.TOKEN), token)
        self.assertIn("runtimes", self.call("GET", "/api/templates").json())   # the setup token reads the builds
        self.assertFalse(self.client.cookies)                                 # still nobody signed in
        with patch("sbx_control.setup.address_ready", return_value=True):
            self.create_admin()                                              # the account, last
            # It finishes as that admin (the browser by its new session; here by their password).
            done = self.call("POST", "/api/setup/complete", {"email": "operator@example.com",
                                                             "password": "test-only-password-123"})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertTrue(done.json()["complete"])
        # Set up: admins only; the token is spent.
        from fastapi.testclient import TestClient
        with TestClient(self.app.app, base_url="https://203.0.113.10") as fresh:
            for method, url, body in steps[1:] + [("PUT", "/api/connections/amp", {"values": {}}),
                                                  ("POST", "/api/setup/admin", {"email": "x@example.com",
                                                                                "password": "test-only-password-9"})]:
                with self.subTest(after=url):
                    self.assertEqual(fresh.request(method, url, headers=self.headers, json=body).status_code, 401)

    def test_boat_key_sets_the_plan_from_boat_and_a_trial_key_is_refused(self):
        from fake_boat import FakeBoat
        from sbx_control.runtime.boat import Boat
        fake = FakeBoat()
        self.enterContext(patch.object(Boat, "test", return_value=self.checks))
        original = Boat.api

        def api(backend):
            backend.transport = fake
            return original(backend)
        self.enterContext(patch.object(Boat, "api", autospec=True, side_effect=api))
        fake.plan = "trial"
        refused = self.call("POST", "/api/runtimes", {"kind": "boat", "values": {"BOAT_API_KEY": "boat_test-only"}})
        self.assertEqual(refused.status_code, 400, refused.text)
        detail = refused.json()["detail"]
        self.assertIn("make the first payment in Boat's dashboard (Billing), then save the key again", detail["message"])
        self.assertEqual([(c["name"], c["ok"], c["field"]) for c in detail["checks"]],
                         [("Boat plan", False, "BOAT_API_KEY")])
        self.assertNotIn("boat_test-only", refused.text)
        self.assertFalse(self.request_file.exists())                         # nothing queued
        self.assertEqual(self.store, {})                                     # nor its key kept
        self.assertNotIn("boat", self.runtime.enabled())
        for plan_key, preset, cpu in (("box_20", "20", "8"), ("box_100", "100", "16")):
            with self.subTest(plan=plan_key):
                fake.plan = plan_key
                added = self.call("POST", "/api/runtimes", {"kind": "boat", "values": {
                    "BOAT_API_KEY": "boat_test-only"}})
                self.assertEqual(added.status_code, 202, added.text)
                queued = json.loads(self.request_file.read_text())
                self.assertEqual(queued["values"]["BOAT_MAX_CPU"], cpu)     # the plan's limits, from Boat's answer
                self.request_file.unlink()
                (self.root / "state/runtime-change.json").unlink(missing_ok=True)
        # A plan given wins over Boat's.
        added = self.call("POST", "/api/runtimes", {"kind": "boat", "plan": "500", "values": {
            "BOAT_API_KEY": "boat_test-only"}})
        self.assertEqual(added.status_code, 202, added.text)
        self.assertEqual(json.loads(self.request_file.read_text())["values"]["BOAT_MAX_SANDBOXES"], "1000")
        # On the server (sbx-admin runtime add, and the queue's apply): a trial key is refused there too.
        from sbx_control import runtime_settings, runtimes
        fake.plan = "trial"
        with self.assertRaisesRegex(runtimes.Refused, "then save the key again"):
            runtime_settings.add("boat", {"BOAT_API_KEY": "boat_test-only"})

    def test_admins_only_once_set_up(self):
        from sbx_control import settings
        settings.update("domain", {**settings.current().domain.model_dump(), "name": ""}, by="test")
        self.repo_admin()
        self.assertEqual(self.client.get("/api/runtimes/available").status_code, 200)   # the signed-in admin
        self.client.cookies.clear()
        self.assertEqual(self.call("GET", "/api/runtimes/available").status_code, 401)  # the token is spent

    def test_a_remove_is_applied_by_a_root_process_that_configured_nothing_yet(self):
        from sbx_control.runtime.fake import Fake
        self.assertEqual(self.call("DELETE", "/api/runtimes/fake").status_code, 202)
        listed = []

        def sandboxes(backend, *args):
            if backend.credentials is None:
                raise RuntimeError("not configured")
            listed.append(backend.kind)
            return []
        # A fresh process: no backend made or configured yet (sbx-apply runtime-change).
        with patch.dict(self.runtime._backends, clear=True), patch.object(Fake, "sandboxes", autospec=True,
                                                                          side_effect=sandboxes):
            code, said = self.apply()
        self.assertEqual(code, 0, said)
        self.assertEqual(listed, ["fake"])
        self.assertEqual(self.runtime.enabled(), ["embed"])

    def test_a_domain_is_a_host_name_and_moving_it_needs_the_key_again(self):
        self.assertEqual(self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {
            "E2B_CLOUD_API_KEY": KEY}}).status_code, 202)
        self.assertEqual(self.apply()[0], 0)
        bad = self.call("PUT", "/api/runtimes/e2b-cloud", {"values": {"E2B_CLOUD_DOMAIN": "evil.example/x;"}})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("host name", bad.json()["detail"])
        from sbx_control.runtime.e2b_cloud import Cloud
        with patch.object(Cloud, "test", side_effect=AssertionError("the stored key must not be tried")):
            moved = self.call("PUT", "/api/runtimes/e2b-cloud", {"values": {"E2B_CLOUD_DOMAIN": "other.example"}})
        self.assertEqual(moved.status_code, 400)
        self.assertIn("type the API key again", moved.json()["detail"])
        self.assertFalse(self.request_file.exists())
        typed = self.call("PUT", "/api/runtimes/e2b-cloud", {"values": {"E2B_CLOUD_DOMAIN": "Other.Example.",
                                                                         "E2B_CLOUD_API_KEY": "test-only-new"}})
        self.assertEqual(typed.status_code, 202, typed.text)
        self.assertEqual(json.loads(self.request_file.read_text())["values"]["E2B_CLOUD_DOMAIN"], "other.example")
        with self.assertRaisesRegex(SystemExit, "not a host name"):
            Cloud().configure({"E2B_CLOUD_API_KEY": "k", "E2B_CLOUD_DOMAIN": "x.example/y"})

    def test_done_waits_for_the_restart_that_takes_it(self):
        from sbx_control import runtime_changes
        self.assertEqual(self.call("PUT", "/api/runtimes/default", {"kind": "fake"}).status_code, 202)
        self.assertEqual(self.apply()[0], 0)
        status = self.call("GET", "/api/runtimes/change").json()
        self.assertEqual((status["state"], status["restarting"]), ("done", True))   # this process is the old one
        with patch.object(runtime_changes, "STARTED", runtime_changes.time.time() + 1):
            self.assertFalse(self.call("GET", "/api/runtimes/change").json()["restarting"])   # a new one answers

    def test_an_old_reference_that_will_not_go_leaves_the_change_done_with_its_effects(self):
        self.assertEqual(self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {
            "E2B_CLOUD_API_KEY": KEY}}).status_code, 202)
        self.assertEqual(self.apply()[0], 0)
        self.effects()
        self.assertEqual(self.call("PUT", "/api/runtimes/e2b-cloud", {"values": {
            "E2B_CLOUD_API_KEY": "test-only-second"}}).status_code, 202)
        from sbx_control import secret_store
        with patch.object(secret_store, "discard", side_effect=RuntimeError("the store did not answer")):
            code, said = self.apply()
        self.assertEqual(code, 0, said)
        status = self.call("GET", "/api/runtimes/change").json()
        self.assertEqual(status["state"], "done")
        self.assertIn("an old sealed value was not discarded", status["message"])
        self.assertEqual(self.effects(), ["control", "tls"])

    def test_a_refused_queue_drops_the_sealed_key_and_one_request_at_a_time(self):
        from sbx_control import runtime_changes
        with patch.object(runtime_changes, "request", side_effect=runtime_changes.Busy("busy now")):
            response = self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {"E2B_CLOUD_API_KEY": KEY}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store, {})                                 # sealed, then discarded
        self.assertEqual(self.call("PUT", "/api/runtimes/default", {"kind": "fake"}).status_code, 202)
        with self.assertRaises(runtime_changes.Busy):                    # the request is there: never overwritten
            runtime_changes.request("default", "embed", by="test")
        self.assertEqual(json.loads(self.request_file.read_text())["kind"], "fake")

    def test_a_request_the_root_side_never_takes_fails_and_goes(self):
        from sbx_control import runtime_changes
        self.assertEqual(self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {
            "E2B_CLOUD_API_KEY": KEY}}).status_code, 202)
        self.assertEqual(len(self.store), 1)
        from sbx_control import host_requests
        later = runtime_changes.time.time() + host_requests.PICKUP_SECONDS + 1
        with patch.object(runtime_changes.time, "time", return_value=later):
            status = self.call("GET", "/api/runtimes/change").json()
        self.assertEqual(status["state"], "failed")
        self.assertIn("sbx-apply-runtime-change.path", status["message"])
        self.assertFalse(self.request_file.exists())
        self.assertEqual(self.store, {})
        self.assertEqual(self.call("PUT", "/api/runtimes/default", {"kind": "fake"}).status_code, 202)   # unblocked

    def test_a_request_queued_after_the_stuck_look_is_kept(self):
        from sbx_control import host_requests
        self.assertEqual(self.call("PUT", "/api/runtimes/default", {"kind": "fake"}).status_code, 202)
        with patch.object(host_requests, "stuck", return_value=True):   # the old one, seen before this was queued
            self.assertEqual(self.call("GET", "/api/runtimes/change").json()["state"], "queued")
        self.assertTrue(self.request_file.exists())

    def test_the_cli_takes_the_same_lock(self):
        from sbx_control import admin, host_requests
        taken = []
        real = host_requests.lock

        def lock(key, **kwargs):
            taken.append(key)
            return real(key, **kwargs)
        out = io.StringIO()
        with patch.object(host_requests, "lock", side_effect=lock), patch("sys.stdout", out), \
                patch("sys.stderr", out):
            self.assertEqual(admin.main(["runtime", "default", "fake"]), 0, out.getvalue())
        self.assertEqual(taken, ["runtime-change"])

    def test_the_cli_refuses_a_change_while_one_from_the_web_app_is_queued(self):
        from sbx_control import admin, runtime
        self.assertEqual(self.call("PUT", "/api/runtimes/default", {"kind": "fake"}).status_code, 202)
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", out):
            self.assertEqual(admin.main(["runtime", "remove", "fake"]), 1, out.getvalue())
        self.assertIn("apply-pending", out.getvalue())
        self.assertIn("fake", runtime.enabled())
        self.assertTrue(self.request_file.exists())

    def test_the_cli_removes_a_runtime_whose_key_is_missing(self):
        from sbx_control import admin, runtime
        self.file.write_text(self.RUNTIME_ENV.replace("FAKE_RUNTIME_TOKEN=test-only-fake\n", ""))
        out = io.StringIO()
        with patch.object(self.fake, "configure", side_effect=SystemExit("FAKE_RUNTIME_TOKEN is not set")), \
                patch("sys.stdout", out), patch("sys.stderr", out):
            self.assertEqual(admin.main(["runtime", "remove", "fake"]), 0, out.getvalue())
        self.assertEqual(runtime.enabled(), ["embed"])
        self.assertIn("is disabled", out.getvalue())

    def test_limits_are_held_to_their_range_and_the_plan_is_named(self):
        from sbx_control import runtimes
        from sbx_control.runtime.e2b_cloud import PRESETS
        too_big = self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {
            "E2B_CLOUD_API_KEY": KEY, "E2B_CLOUD_MAX_CPU": "64"}})
        self.assertEqual(too_big.status_code, 400)
        self.assertIn("vCPUs (E2B_CLOUD_MAX_CPU) is from 1 to 32", too_big.json()["detail"])
        for bad in ("²", "1" * 40):   # never a 500: a whole number in ASCII digits
            refused = self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {
                "E2B_CLOUD_API_KEY": KEY, "E2B_CLOUD_MAX_CPU": bad}})
            self.assertEqual(refused.status_code, 400, bad)
        pro = {**PRESETS[1].by_key(), "E2B_CLOUD_MAX_CPU": "16"}   # Pro's vCPUs are "8+": still Pro
        self.assertEqual(self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {
            "E2B_CLOUD_API_KEY": KEY, **pro}}).status_code, 202)
        self.assertEqual(self.apply()[0], 0)
        listed = {r["kind"]: r for r in runtimes.describe()["runtimes"]}
        self.assertEqual(listed["e2b-cloud"]["plan"], {"name": "pro", "title": "Pro"})
        self.assertIsNone(listed["fake"]["plan"])                        # no plans: no plan named
        available = {r["kind"]: r for r in self.call("GET", "/api/runtimes/available").json()}
        self.assertEqual(available["e2b-cloud"]["plan"]["name"], "pro")
        run = next(s for s in available["e2b-cloud"]["settings"] if s["key"] == "E2B_CLOUD_MAX_TIMEOUT")
        self.assertEqual((run["min"], run["max"], run["step"], run["unit"]), (300, 86400, 300, "seconds"))
        self.assertEqual(self.call("PUT", "/api/runtimes/e2b-cloud", {"values": {
            "E2B_CLOUD_MAX_SANDBOXES": "50"}}).status_code, 202)
        self.assertEqual(self.apply()[0], 0)
        listed = {r["kind"]: r for r in runtimes.describe()["runtimes"]}
        self.assertEqual(listed["e2b-cloud"]["plan"], {"name": "custom", "title": "Custom limits"})

    def test_a_plan_is_resolved_here_and_keeps_the_limits_it_leaves_adjustable(self):
        from sbx_control.runtime.e2b_cloud import PRESETS
        hobby, pro = PRESETS
        for plan, values, said in (("gold", {}, "no plan gold"),
                                   ("hobby", {"E2B_CLOUD_MAX_TIMEOUT": "7200"}, "plan fixes E2B_CLOUD_MAX_TIMEOUT")):
            with self.subTest(plan=plan):
                refused = self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "plan": plan,
                                                              "values": {"E2B_CLOUD_API_KEY": KEY, **values}})
                self.assertEqual(refused.status_code, 400, refused.text)
                self.assertIn(said, refused.json()["detail"])
        self.assertEqual(self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "plan": "hobby", "values": {
            "E2B_CLOUD_API_KEY": KEY}}).status_code, 202)
        queued = json.loads(self.request_file.read_text())["values"]
        self.assertEqual({k: queued[k] for k in hobby.by_key()}, hobby.by_key())
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(self.call("PUT", "/api/runtimes/e2b-cloud", {"values": {
            "E2B_CLOUD_MAX_CPU": "16"}}).status_code, 202)                # no plan: the values alone
        self.assertEqual(json.loads(self.request_file.read_text())["values"], {"E2B_CLOUD_MAX_CPU": "16"})
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(self.call("PUT", "/api/runtimes/e2b-cloud", {"plan": "pro", "values": {}}).status_code, 202)
        queued = json.loads(self.request_file.read_text())["values"]
        self.assertNotIn("E2B_CLOUD_MAX_CPU", queued)                      # adjustable: the value set stays
        self.assertEqual(queued["E2B_CLOUD_MAX_TIMEOUT"], pro.by_key()["E2B_CLOUD_MAX_TIMEOUT"])

    def test_an_enabled_runtime_s_checks_over_the_api(self):
        from sbx_control import reachability, template_builds
        from sbx_control.connections.base import Check
        response = self.call("POST", "/api/runtimes/fake/test")
        self.assertEqual(response.status_code, 200, response.text)
        found = response.json()
        self.assertTrue(found["ok"])
        self.assertEqual([c["name"] for c in found["checks"]], ["Fake API", "Limits", "Base template"])
        self.assertIn("not built yet", found["checks"][2]["detail"])
        # Built: the sandboxes' reach is checked too; a failing check makes the answer not ok.
        with patch.object(template_builds, "status", return_value="built"), \
                patch.object(reachability, "check", return_value=Check("Reaches this server", False, "timed out",
                                                                       "open 443", runtime="fake")) as reach:
            found = self.call("POST", "/api/runtimes/fake/test").json()
        reach.assert_called_once_with("fake")
        self.assertFalse(found["ok"])
        self.assertEqual(found["checks"][-1], {"name": "Reaches this server", "ok": False, "detail": "timed out",
                                               "fix": "open 443", "runtime": "fake", "help_url": ""})
        refused = self.call("POST", "/api/runtimes/e2b-cloud/test")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("not enabled", refused.json()["detail"])
        self.assertEqual(self.client.post("/api/runtimes/fake/test").status_code, 401)   # admins (and the setup link)

    def test_a_change_that_stopped_part_way_is_failed_not_waited_for(self):
        from sbx_control import runtime_changes
        self.assertEqual(self.call("PUT", "/api/runtimes/default", {"kind": "fake"}).status_code, 202)
        self.assertEqual(runtime_changes.status()["state"], "queued")
        self.request_file.unlink()                                       # taken, and its process gone
        (self.root / "state/runtime-change.json").write_text(json.dumps({"state": "applying", "operation": "default"}))
        self.assertEqual(runtime_changes.status()["state"], "failed")

    def test_a_change_finished_while_status_looked_is_done_not_failed(self):
        from sbx_control import host_requests, runtime_changes
        self.assertEqual(self.call("PUT", "/api/runtimes/default", {"kind": "fake"}).status_code, 202)
        self.request_file.unlink()                                       # taken by the root side...
        record = {"state": "applying", "operation": "default", "kind": "fake"}
        (self.root / "state/runtime-change.json").write_text(json.dumps(record))

        def finished(key):   # ...which finishes right after status() saw its lock held
            (self.root / "state/runtime-change.json").write_text(json.dumps({**record, "state": "done", "message": "ok"}))
            return True
        with patch.object(host_requests, "applying", side_effect=finished):
            self.assertEqual(runtime_changes.status()["state"], "done")

    def test_a_change_taken_after_status_looked_at_the_lock_is_applying(self):
        from sbx_control import host_requests, runtime_changes
        self.assertEqual(self.call("PUT", "/api/runtimes/default", {"kind": "fake"}).status_code, 202)

        def free(key):   # the lock free when looked at; then the root side takes the request
            self.request_file.unlink()
            (self.root / "state/runtime-change.json").write_text(json.dumps({"state": "applying"}))
            return False
        with patch.object(host_requests, "applying", side_effect=free), host_requests.lock(runtime_changes.KEY):
            self.assertEqual(runtime_changes.status()["state"], "applying")

    def test_a_store_reference_is_never_taken_as_a_value(self):
        from sbx_control import secret_store
        self.store["infisical-ref-77"] = "the platform's own secret"
        for values in ({"E2B_CLOUD_API_KEY": "infisical-ref-77"}, {"E2B_CLOUD_API_KEY": KEY, "E2B_CLOUD_DOMAIN": "infisical-ref-77"}):
            with self.subTest(values=values), patch.object(secret_store, "unseal", side_effect=AssertionError("unsealed")):
                response = self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": values})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("a reference into the secret store is not a value", response.json()["detail"])
        self.assertFalse(self.request_file.exists())

    def test_an_installation_that_never_chose_a_plan_is_on_hobby(self):
        from sbx_control import runtime_settings
        from sbx_control.runtime.e2b_cloud import Cloud
        cloud = Cloud()
        self.assertEqual(cloud.limits().sandboxes, 10)
        self.assertEqual(runtime_settings.plan_of(cloud, {}), {"name": "hobby", "title": "Hobby"})

    def test_a_provider_without_a_domain_is_said_up_front(self):
        from sbx_control import settings
        settings.update("domain", {**settings.current().domain.model_dump(), "name": ""}, by="test")
        cloud = next(r for r in self.call("GET", "/api/runtimes/available").json() if r["kind"] == "e2b-cloud")
        self.assertEqual(cloud["edit_blocker"]["code"], "needs_domain")
        self.assertIn("connect a domain first", cloud["edit_blocker"]["message"])

    def test_the_default_again_is_done_at_once(self):
        from sbx_control import runtime_changes
        response = self.call("PUT", "/api/runtimes/default", {"kind": "embed"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["state"], response.json()["restarting"], response.json()["unchanged"]),
                         ("done", False, True))
        self.assertIn("is the default already", response.json()["message"])
        self.assertFalse(self.request_file.exists())
        self.assertEqual(runtime_changes.status()["state"], "idle")

    def test_a_runtime_that_cannot_be_configured_can_still_be_removed(self):
        with patch.object(self.fake, "configure", side_effect=SystemExit("missing platform secret: FAKE_RUNTIME_TOKEN")):
            listed = {r["kind"]: r for r in self.call("GET", "/api/runtimes/available").json()}
            self.assertIsNone(listed["fake"]["remove_blocker"])
            self.assertEqual(self.call("DELETE", "/api/runtimes/fake").status_code, 202)
            code, said = self.apply()
        self.assertEqual(code, 0, said)
        self.assertIn("Its sandboxes could not be listed (missing platform secret: FAKE_RUNTIME_TOKEN)", said)
        self.assertEqual(self.runtime.enabled(), ["embed"])

    def test_install_sh_s_runtime_is_refused_before_any_listing(self):
        from sbx_control import runtime_settings
        with patch.object(runtime_settings, "_listing", side_effect=AssertionError("listed")):
            response = self.call("DELETE", "/api/runtimes/embed")
        self.assertEqual(response.status_code, 409)
        self.assertIn("set up by install.sh", response.json()["detail"])

    def test_the_last_runtime_points_at_the_cli_and_the_settings(self):
        from sbx_control import runtime_settings
        self.assertEqual(self.call("DELETE", "/api/runtimes/fake").status_code, 202)
        self.assertEqual(self.apply()[0], 0)
        with patch.object(self.runtime, "enabled", return_value=["fake"]):
            found = runtime_settings.remove_blocker("fake")
        self.assertEqual(found.code, "last_runtime")
        self.assertIn("sbx runtimes add KIND, or Settings → Sandbox runtimes", found.message)


class LearnedLimits(TwoRuntimes, Installation):
    def test_a_refused_limit_is_recorded_named_and_never_raised_by_sbx(self):
        from sbx_control import runtimes
        from sbx_control.runtime import LimitRefused, SettingField
        self.fake.settings = (SettingField("FAKE_MAX_TIMEOUT", "Longest run (seconds)", kind="int",
                                           limit="timeout_seconds"),)
        refused = LimitRefused("Fake refused a 2 h timeout: your plan allows 1 h", field="timeout_seconds",
                               asked=7200, allowed=3600)
        with patch.object(self.fake, "create", side_effect=refused), self.assertRaises(LimitRefused) as raised:
            self.runtime.create("tpl", timeout=7200, kind="fake")
        self.assertIn("your plan allows 1 h; change Longest run (seconds) (FAKE_MAX_TIMEOUT)", str(raised.exception))
        hit = self.runtime.limit_hit("fake")
        self.assertEqual((hit["field"], hit["asked"], hit["allowed"], hit["setting"]),
                         ("timeout_seconds", 7200, 3600, "FAKE_MAX_TIMEOUT"))
        listed = {r["kind"]: r for r in runtimes.describe()["runtimes"]}
        self.assertIn("2 h timeout", listed["fake"]["limit_hit"]["message"])
        self.assertIsNone(listed["embed"]["limit_hit"])
        self.assertIsNone(self.fake.limit.timeout_seconds)               # nothing raised by itself
        self.runtime.forget_limit_hit("fake")
        self.assertIsNone(self.runtime.limit_hit("fake"))

    def test_e2b_s_answers_read_as_the_limit_they_refuse(self):
        from sbx_control.runtime.e2b_cloud import refused_limit
        found = refused_limit(Exception("400: Timeout cannot be greater than 1 hours"), {"timeout_seconds": 7200})
        self.assertEqual((found.field, found.asked, found.allowed), ("timeout_seconds", 7200, 3600))
        self.assertEqual(str(found), "E2B refused a 2 h timeout: your plan allows 1 h")
        # Only the refusal sbx reads for sure; any other is E2B's own message.
        self.assertIsNone(refused_limit(Exception("You have reached the maximum number of concurrent E2B sandboxes (20)"), {}))
        self.assertIsNone(refused_limit(Exception("Memory 16384 MiB exceeds the team limit of 8192"), {"memory_mb": 16384}))
        self.assertIsNone(refused_limit(Exception("build failed: out of memory"), {}))
        self.assertIsNone(refused_limit(Exception("connection reset"), {}))

    def test_the_cloud_backend_raises_it_from_create(self):
        from sbx_control.runtime import LimitRefused
        from sbx_control.runtime.e2b import Compute
        from sbx_control.runtime.e2b_cloud import Cloud
        cloud = Cloud()
        with patch.object(Compute, "create", side_effect=Exception("Timeout cannot be greater than 1 hours")), \
                self.assertRaises(LimitRefused) as raised:
            cloud.create("tpl", timeout=7200)
        self.assertEqual(raised.exception.allowed, 3600)


class NoRuntimeYet(Server):
    """install.sh on a host without KVM and without a provider's key."""
    RUNTIME_ENV = "SBX_RUNTIMES=''\n"

    def setUp(self):
        super().setUp()
        from sbx_control import runtime
        self.runtime = runtime
        self.file = self.root / "etc/runtime.env"
        self.addCleanup(self.file.write_text, self.RUNTIME_ENV)

    def test_a_recipe_s_size_is_checked_without_a_runtime(self):
        from sbx_control import recipes
        with tempfile.TemporaryDirectory() as folder:
            for bad in ("false", "0", "-2", "1.5", "lots"):
                with self.subTest(cpu=bad):
                    Path(folder, "sbx.yaml").write_text(f"resources:\n  cpu: {bad}\n")
                    with self.assertRaisesRegex(recipes.RecipeError, "resources.cpu must be a positive integer"):
                        recipes.validate("owner/app", Path(folder))
            Path(folder, "sbx.yaml").write_text("resources:\n  cpu: 64\n")
            recipes.validate("owner/app", Path(folder))   # no runtime's limits to hold it to yet

    def test_sbx_control_runs_and_says_what_to_do(self):
        from sbx_control import jobs, preview_proxy, runtimes
        self.assertEqual((self.runtime.enabled(), self.runtime.default()), ([], ""))
        self.assertEqual(self.client.get("/api/runtimes", headers=self.headers).json(), {"default": "", "runtimes": []})
        with self.assertRaisesRegex(runtimes.Refused, "no sandbox runtime is set up: add one in Settings"):
            runtimes.choose("owner/app")
        with self.assertRaises(self.runtime.NoRuntime):
            self.runtime.get()
        self.assertEqual(list(self.runtime.sandboxes()), [])
        self.assertEqual(self.runtime.status().state, "not_connected")
        self.assertIn("default http://127.0.0.1:9;", preview_proxy.runtime_upstreams())
        jobs.jobs()                                                     # nothing of a runtime's
        from sbx_control import template_builds
        out = io.StringIO()
        with patch("sys.stderr", out):
            self.assertEqual(template_builds.main(["base", "--if-stale"]), 0)
        self.assertIn("no sandbox runtime is set up", out.getvalue())

    def test_routes_that_need_a_runtime_say_so(self):
        self.repo_admin_without_runtime()
        listed = self.client.get("/api/sandbox-list?scope=mine")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["capacity"], {"known": False, "runtime": ""})
        from fastapi.testclient import TestClient
        client = TestClient(self.app.app, base_url="https://203.0.113.10", raise_server_exceptions=False)
        self.addCleanup(client.close)
        client.cookies.update(self.client.cookies)
        self.assertEqual(client.get("/api/repos/owner/app/template").status_code, 200)
        from sbx_control import sandboxes_api
        with patch.object(sandboxes_api, "capacities", side_effect=self.runtime.NoRuntime()):   # any call to get()
            response = client.get("/api/sandbox-list?scope=mine")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("no sandbox runtime is set up", response.json()["detail"])
        from sbx_control import app
        failed = app.unexpected(None, self.runtime.Failed("x"))
        import asyncio
        self.assertEqual(asyncio.run(failed).status_code, 503)           # no title to name: still an answer

    def test_a_create_through_the_api_is_refused_with_what_to_do(self):
        self.repo_admin_without_runtime()
        response = self.client.post("/api/sandboxes", json={"name": "try-1", "repo": "owner/app"})
        self.assertIn(response.status_code, (400, 409, 503), response.text)
        self.assertIn("no sandbox runtime is set up", response.text)

    def repo_admin_without_runtime(self):
        from sbx_control import setup
        self.choose_ip()
        self.create_admin()
        setup.mark_complete("test")

    def test_doctor_and_the_cli_report_it(self):
        from sbx_control import admin
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", out):
            self.assertEqual(admin.main(["doctor"]), 1)
            admin.main(["runtime", "list"])
        self.assertIn("no sandbox runtime is set up", out.getvalue())

    def test_the_wizard_needs_a_runtime_before_it_finishes(self):
        from sbx_control import setup
        self.choose_ip()
        self.create_admin()
        state = self.client.get("/api/setup", headers=self.headers).json()
        step = next(s for s in state["steps"] if s["step"] == "runtime")
        self.assertFalse(step["done"])
        self.assertFalse(state["can_complete"])
        response = self.client.post("/api/setup/complete", headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("sandbox runtime", response.text)
        self.assertFalse(setup.configured())                             # install-report keeps the link open

    def test_the_first_runtime_is_added_from_none(self):
        from sbx_control import runtime_changes, runtime_settings, secret_store, settings, vault
        from sbx_control.connections.base import Check
        from sbx_control.runtime.e2b_cloud import Cloud
        settings.update("domain", {**settings.current().domain.model_dump(), "name": "sbx.example.com"}, by="test")
        with patch.object(Cloud, "test", return_value=[Check("E2B Cloud API", True, "answers")]), \
                patch.object(vault, "sealing", return_value=True), \
                patch.object(secret_store, "seal", return_value="infisical-ref-1"), \
                patch.object(secret_store, "is_reference", side_effect=lambda v: v == "infisical-ref-1"), \
                patch.object(secret_store, "unseal", return_value=KEY):
            values = runtime_settings.prepare("e2b-cloud", {"E2B_CLOUD_API_KEY": KEY}, adding=True)
            self.assertEqual(values, {"E2B_CLOUD_API_KEY": "infisical-ref-1"})
            runtime_changes.perform("add", "e2b-cloud", values)
        self.assertEqual(self.runtime.enabled(), ["e2b-cloud"])
        self.assertEqual(runtime_settings.remove_blocker("e2b-cloud").code, "last_runtime")


class SetupWithoutABrowser(Server):
    """sbx setup finish --admin: the last step with the setup link and the admin's password, no session."""

    def test_finish_with_the_admin_s_password_instead_of_a_session(self):
        from fastapi.testclient import TestClient
        self.choose_ip()
        self.create_admin()
        self.choose_agents()
        fresh = TestClient(self.app.app, base_url="https://203.0.113.10")   # no session cookie: an agent's CLI
        self.addCleanup(fresh.close)
        wrong = fresh.post("/api/setup/complete", headers=self.headers,
                           json={"email": "operator@example.com", "password": "not-the-password"})
        self.assertEqual(wrong.status_code, 403)
        from sbx_control import users
        users.set_password("dev@example.com", "test-only-password-456", by="test")   # a person, not an admin
        not_admin = fresh.post("/api/setup/complete", headers=self.headers,
                               json={"email": "dev@example.com", "password": "test-only-password-456"})
        self.assertEqual(not_admin.status_code, 403)
        self.assertEqual(fresh.post("/api/setup/complete", headers=self.headers).status_code, 400)   # nobody signed in
        done = fresh.post("/api/setup/complete", headers=self.headers,
                          json={"email": "Operator@example.com", "password": "test-only-password-123"})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertTrue(done.json()["complete"])
        self.assertNotIn("test-only-password-123", done.text)


class InstallerWithoutARuntime(Installation):
    RUNTIME_ENV = RUNTIME

    def test_a_first_install_without_kvm_or_key_names_no_runtime(self):
        from sbx_control.runtime.__main__ import write_env
        file = self.root / "etc/runtime.env"
        self.addCleanup(file.write_text, RUNTIME)
        file.unlink()
        self.assertEqual(write_env([], [], {}), [])
        self.assertIn("SBX_RUNTIMES=''\n", file.read_text())
        from sbx_control import runtime
        self.assertEqual(runtime.enabled(), [])
        self.assertEqual(write_env([], [], {}), [])                     # again: as it was
        self.assertEqual(write_env(["E2B_CLOUD_API_KEY"], ["e2b-cloud"], {"E2B_CLOUD_API_KEY": "test-only"}),
                         ["e2b-cloud"])                                 # a key on a later run enables it
        self.assertIn("SBX_RUNTIMES=e2b-cloud\n", file.read_text())

    def test_the_preflight_goes_on_without_a_runtime(self):
        """install.sh's runtime choice, cut from the script, on a host without
        /dev/kvm and no key: no FAILED, none chosen."""
        text = (Path(__file__).parents[2] / "install.sh").read_text()
        step = text[text.index('installed="" has_env=""\n'):text.index('echo "ok: $PRETTY_NAME')]
        with tempfile.TemporaryDirectory(prefix="sbx-install-none-") as directory:
            env = Path(directory) / "runtime.env"
            cases = (
                (None, False, "none=1 embed= cloud=", False),
                ("SBX_RUNTIMES=''\n", False, "none=1 embed= cloud=", False),
                ("E2B_API_KEY=x\n", False, "FAILED", False),
                (None, True, "none= embed=1 cloud=", False),            # a first install where Embed fits
                ("SBX_RUNTIMES=''\n", True, "none=1 embed= cloud=", False),   # none enabled stays so: no auto-enable
                # An installation without runtime.env runs Embed (as sbx reads it): never none written over it.
                (None, True, "none= embed=1 cloud=", True),
                (None, False, "FAILED: no /dev/kvm", True),
            )
            for before, kvm, expected, installed in cases:
                with self.subTest(before=before, kvm=kvm, installed=installed):
                    env.unlink(missing_ok=True)
                    if before is not None:
                        env.write_text(before)
                    device = "1" if kvm else ""   # the preflight's facts: /dev/kvm and /dev/net/tun
                    script = (f"set -eu\nsrc={Path(__file__).parents[2]}\n"
                              "mem_mib=64000 disk_gib=500 hugepages= cloud_key= first=1 email= admin= "
                              f"sign_in= domain= check= kvm={device} tun={device}\nconfigured() {{ {str(installed).lower()}; }}\n"
                              "die() { echo \"FAILED: $*\"; exit 1; }\n"
                              + step.replace("/etc/sbx/runtime.env", str(env))
                              + 'echo "none=$none embed=$embed cloud=$cloud"\n')
                    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=10)
                    self.assertIn(expected, result.stdout + result.stderr)
        # The none path writes runtime.env only where there is none.
        self.assertIn('if [ -n "$none" ] && [ -z "$has_env" ]; then', text)


class DoctorWithSealedKeys(Installation):
    """sbx-admin doctor reads a runtime's key as its backend does: a reference into the store, unsealed."""
    RUNTIME_ENV = "SBX_RUNTIMES=e2b-cloud\nE2B_CLOUD_API_KEY=placeholder\n"

    def test_a_sealed_key_reads_as_set(self):
        from sbx_control import admin, connections, runtime, secret_store
        from sbx_control.runtime.e2b_cloud import Cloud
        reference = secret_store.seal("e2b_test_only_key", key="runtime/E2B_CLOUD_API_KEY/test")
        (self.root / "etc/runtime.env").write_text(f"SBX_RUNTIMES=e2b-cloud\nE2B_CLOUD_API_KEY={reference}\n")
        self.enterContext(patch.dict(runtime._backends, clear=True))   # a fresh process: nothing configured yet
        self.enterContext(patch.object(Cloud, "sandboxes", return_value=[]))
        self.enterContext(patch.object(connections, "status_all",
                                       side_effect=lambda: [connections.get("runtime").status()]))
        self.enterContext(patch.object(connections.get("runtime"), "test", return_value=[]))
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", out):
            self.assertEqual(admin.do_doctor(), 0, out.getvalue())
        self.assertIn("connected · E2B Cloud · 0 running here", out.getvalue())
        self.assertNotIn("no E2B_CLOUD_API_KEY", out.getvalue())
        self.assertNotIn("e2b_test_only_key", out.getvalue())
