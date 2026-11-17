"""Runtime settings (phase 3): each backend declares its settings; admins
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
        self.assertIn("memory", embed.can_run({"kvm": True, "memory_gb": 4}))
        self.assertIn("disk", embed.can_run({"kvm": True, "memory_gb": 64, "disk_free_gb": 20}))
        self.assertEqual(embed.can_run({"kvm": True, "memory_gb": 64, "disk_free_gb": 500}), "")

    def test_a_backend_that_declares_none_has_its_secret(self):
        from sbx_control import runtimes
        from sbx_control.runtime.fake import Fake
        found = runtimes.fields(Fake())
        self.assertEqual(list(found), ["FAKE_RUNTIME_TOKEN"])
        self.assertTrue(found["FAKE_RUNTIME_TOKEN"].secret)


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
        self.assertIn("install.sh", embed["removable"])
        self.assertEqual(embed["settings"], [])                          # install.sh's keys, never a form
        self.assertEqual((cloud["enabled"], cloud["can_run"], cloud["not_editable"]), (False, True, ""))
        self.assertEqual([p["name"] for p in cloud["presets"]], ["hobby", "pro"])
        boat = found["boat"]   # the same form machinery: its keys, plans and limits, nothing Boat-specific in core
        self.assertEqual((boat["enabled"], boat["can_run"], boat["title"]), (False, True, "Boat"))
        self.assertEqual([p["name"] for p in boat["presets"]], ["trial", "20", "100", "500", "2000"])
        self.assertIn("templates need the first payment", boat["presets"][0]["note"])
        self.assertEqual([s["key"] for s in boat["settings"]][:3], ["BOAT_API_KEY", "BOAT_API_URL", "BOAT_ORG"])
        self.assertEqual(fake["secrets_set"], ["FAKE_RUNTIME_TOKEN"])
        self.assertEqual(fake["values"], {})
        self.assertEqual((fake["sandboxes"], fake["removable"]), ([], ""))
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
        self.assertNotIn(KEY, (self.root / "state/runtime-change.json").read_text())
        busy = self.call("PUT", "/api/runtimes/default", {"kind": "fake"})
        self.assertEqual(busy.status_code, 409)                         # one change at a time
        code, said = self.apply()
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
        self.assertIn("sbx-admin runtime add e2b-cloud", listed["e2b-cloud"]["not_editable"])   # the UI says it
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
        self.checks = [Check("E2B Cloud API", False, "HTTP 401")]
        response = self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {"E2B_CLOUD_API_KEY": KEY}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("HTTP 401", response.json()["detail"])
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
        self.assertIn("busy-one", listed["fake"]["removable"])

    def test_a_domain_is_needed_before_a_provider_s_runtime(self):
        from sbx_control import settings
        settings.update("domain", {**settings.current().domain.model_dump(), "name": ""}, by="test")
        response = self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {"E2B_CLOUD_API_KEY": KEY}})
        self.assertEqual(response.status_code, 409)
        self.assertIn("connect a domain first", response.json()["detail"])

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
        later = runtime_changes.time.time() + runtime_changes.PICKUP_SECONDS + 1
        with patch.object(runtime_changes.time, "time", return_value=later):
            status = self.call("GET", "/api/runtimes/change").json()
        self.assertEqual(status["state"], "failed")
        self.assertIn("sbx-apply-runtime-change.path", status["message"])
        self.assertFalse(self.request_file.exists())
        self.assertEqual(self.store, {})
        self.assertEqual(self.call("PUT", "/api/runtimes/default", {"kind": "fake"}).status_code, 202)   # unblocked

    def test_the_cli_takes_the_same_lock(self):
        from sbx_control import admin, runtime_changes
        taken = []
        real = runtime_changes.locked

        def locked():
            taken.append(1)
            return real()
        out = io.StringIO()
        with patch.object(runtime_changes, "locked", side_effect=locked), patch("sys.stdout", out), \
                patch("sys.stderr", out):
            self.assertEqual(admin.main(["runtime", "default", "fake"]), 0, out.getvalue())
        self.assertEqual(taken, [1])

    def test_limits_are_held_to_their_range_and_the_plan_is_named(self):
        from sbx_control import runtimes
        from sbx_control.runtime.e2b_cloud import PRESETS
        too_big = self.call("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {
            "E2B_CLOUD_API_KEY": KEY, "E2B_CLOUD_MAX_CPU": "64"}})
        self.assertEqual(too_big.status_code, 400)
        self.assertIn("vCPUs (E2B_CLOUD_MAX_CPU) is from 1 to 32", too_big.json()["detail"])
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
                                               "fix": "open 443", "runtime": "fake"})
        refused = self.call("POST", "/api/runtimes/e2b-cloud/test")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("not enabled", refused.json()["detail"])
        self.assertEqual(self.client.post("/api/runtimes/fake/test").status_code, 401)   # admins (and the setup link)

    def test_a_change_that_stopped_part_way_is_failed_not_waited_for(self):
        from sbx_control import runtime_changes
        self.assertEqual(self.call("PUT", "/api/runtimes/default", {"kind": "fake"}).status_code, 202)
        self.assertEqual(runtime_changes.status()["state"], "queued")
        self.request_file.unlink()                                       # taken, and its process gone
        self.assertEqual(runtime_changes.status()["state"], "failed")

    def test_a_change_finished_while_status_looked_is_done_not_failed(self):
        from sbx_control import runtime_changes
        self.assertEqual(self.call("PUT", "/api/runtimes/default", {"kind": "fake"}).status_code, 202)
        self.request_file.unlink()                                       # taken by the root side...
        record = json.loads((self.root / "state/runtime-change.json").read_text())

        def finished():   # ...which finishes between status()'s first read and its checks
            (self.root / "state/runtime-change.json").write_text(json.dumps({**record, "state": "done", "message": "ok"}))
            return False
        with patch.object(runtime_changes, "_applying", side_effect=finished):
            self.assertEqual(runtime_changes.status()["state"], "done")

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
        from sbx_control import runtimes
        from sbx_control.runtime.e2b_cloud import Cloud
        cloud = Cloud()
        self.assertEqual(cloud.limits().sandboxes, 10)
        self.assertEqual(runtimes.plan_of(cloud, {}), {"name": "hobby", "title": "Hobby"})

    def test_a_provider_without_a_domain_is_said_up_front(self):
        from sbx_control import settings
        settings.update("domain", {**settings.current().domain.model_dump(), "name": ""}, by="test")
        cloud = next(r for r in self.call("GET", "/api/runtimes/available").json() if r["kind"] == "e2b-cloud")
        self.assertTrue(cloud["needs_domain"])
        self.assertIn("connect a domain first", cloud["not_editable"])

    def test_the_default_again_is_done_at_once(self):
        from sbx_control import runtime_changes
        response = self.call("PUT", "/api/runtimes/default", {"kind": "embed"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["state"], response.json()["restarting"]), ("done", False))
        self.assertIn("is the default already", response.json()["message"])
        self.assertFalse(self.request_file.exists())
        self.assertEqual(runtime_changes.status()["state"], "idle")

    def test_a_runtime_that_cannot_be_configured_can_still_be_removed(self):
        with patch.object(self.fake, "configure", side_effect=SystemExit("missing platform secret: FAKE_RUNTIME_TOKEN")):
            listed = {r["kind"]: r for r in self.call("GET", "/api/runtimes/available").json()}
            self.assertEqual(listed["fake"]["removable"], "")
            self.assertEqual(self.call("DELETE", "/api/runtimes/fake").status_code, 202)
            code, said = self.apply()
        self.assertEqual(code, 0, said)
        self.assertIn("Its sandboxes could not be listed (missing platform secret: FAKE_RUNTIME_TOKEN)", said)
        self.assertEqual(self.runtime.enabled(), ["embed"])

    def test_install_sh_s_runtime_is_refused_before_any_listing(self):
        from sbx_control import runtimes
        with patch.object(runtimes, "removal_refused", side_effect=AssertionError("listed")):
            response = self.call("DELETE", "/api/runtimes/embed")
        self.assertEqual(response.status_code, 409)
        self.assertIn("set up by install.sh", response.json()["detail"])

    def test_the_last_runtime_points_at_the_cli_and_the_settings(self):
        from sbx_control import runtimes
        self.assertEqual(self.call("DELETE", "/api/runtimes/fake").status_code, 202)
        self.assertEqual(self.apply()[0], 0)
        with patch.object(self.runtime, "enabled", return_value=["fake"]):
            with self.assertRaises(runtimes.Conflict) as refused:
                runtimes.removal_refused("fake")
        self.assertIn("sbx runtimes add KIND, or Settings → Sandbox runtimes", str(refused.exception))


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

    def test_sbx_control_runs_and_says_what_to_do(self):
        from sbx_control import ip_previews, jobs, runtimes
        self.assertEqual((self.runtime.enabled(), self.runtime.default()), ([], ""))
        self.assertEqual(self.client.get("/api/runtimes", headers=self.headers).json(), {"default": "", "runtimes": []})
        with self.assertRaisesRegex(runtimes.Refused, "no sandbox runtime is set up: add one in Settings"):
            runtimes.choose("owner/app")
        with self.assertRaises(self.runtime.NoRuntime):
            self.runtime.get()
        self.assertEqual(list(self.runtime.sandboxes()), [])
        self.assertEqual(self.runtime.status().state, "not_connected")
        self.assertIn("default http://127.0.0.1:9;", ip_previews.runtime_upstreams())
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
        from sbx_control import runtimes, secret_store, settings, vault
        from sbx_control.connections.base import Check
        from sbx_control.runtime.e2b_cloud import Cloud
        settings.update("domain", {**settings.current().domain.model_dump(), "name": "sbx.example.com"}, by="test")
        with patch.object(Cloud, "test", return_value=[Check("E2B Cloud API", True, "answers")]), \
                patch.object(vault, "sealing", return_value=True), \
                patch.object(secret_store, "seal", return_value="infisical-ref-1"), \
                patch.object(secret_store, "is_reference", side_effect=lambda v: v == "infisical-ref-1"), \
                patch.object(secret_store, "unseal", return_value=KEY):
            values = runtimes.prepare("e2b-cloud", {"E2B_CLOUD_API_KEY": KEY}, adding=True)
            self.assertEqual(values, {"E2B_CLOUD_API_KEY": "infisical-ref-1"})
            runtimes.add("e2b-cloud", values)
        self.assertEqual(self.runtime.enabled(), ["e2b-cloud"])
        with self.assertRaisesRegex(runtimes.Conflict, "only runtime"):
            runtimes.removal_refused("e2b-cloud")


class SetupWithoutABrowser(Server):
    """sbx setup finish --admin: the last step with the setup link and the admin's password, no session."""

    def test_finish_with_the_admin_s_password_instead_of_a_session(self):
        from fastapi.testclient import TestClient
        self.choose_ip()
        self.create_admin()
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
                (None, False, "none=1 embed= cloud="),
                ("SBX_RUNTIMES=''\n", False, "none=1 embed= cloud="),
                ("E2B_API_KEY=x\n", False, "FAILED"),
                (None, True, "none= embed=1 cloud="),                   # a first install where Embed fits
                ("SBX_RUNTIMES=''\n", True, "none=1 embed= cloud="),    # none enabled stays so: no auto-enable
            )
            for before, kvm, expected in cases:
                with self.subTest(before=before, kvm=kvm):
                    env.unlink(missing_ok=True)
                    if before is not None:
                        env.write_text(before)
                    device = directory if kvm else "/nonexistent-kvm"   # a path that exists stands for /dev/kvm
                    script = ("set -eu\nmem_mib=64000 disk_gib=500 hugepages= cloud_key= first=1 email= admin= "
                              "sign_in= domain=\nconfigured() { false; }\ndie() { echo \"FAILED: $*\"; exit 1; }\n"
                              + step.replace("/etc/sbx/runtime.env", str(env)).replace("/dev/kvm", device)
                              .replace("/dev/net/tun", directory if kvm else "/nonexistent-tun")
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
