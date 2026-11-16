"""Several runtimes in one installation: Embed (a stand-in with its
capabilities and ingress) and the in-memory fake, side by side. Sandboxes
are listed and tagged, routed to their own runtime, created on the one the
request, the repo or the default chooses (or refused, saying why), get
templates built per runtime and previews through their runtime's ingress;
the host's calls run over both; `sbx-admin runtime verify` passes on the
fake. And the fake itself behaves as the contract says."""
import dataclasses
from unittest.mock import patch

from support import RUNTIME, Installation, Server

TWO = RUNTIME + "SBX_RUNTIMES=embed,fake\nFAKE_RUNTIME_TOKEN=test-only-fake\n"
FAKE = "sbx_control.runtime.fake:Fake"


def backends():
    """(Embed's stand-in, the fake), fresh."""
    from sbx_control import runtime
    from sbx_control.runtime.fake import Fake

    class EmbedStub(Fake):
        kind, title, host, id_prefix = "embed", "E2B Embed", True, "em"
        capabilities = runtime.Capabilities(snapshots=True, private_networks=True, egress_rules=True)
        ingress = "http://127.0.0.1:3002"
        ingress_cleared = ("E2b-Sandbox-Id", "E2b-Sandbox-Port")
        internal_suffix = "e2b.sandboxes.internal"
        ports = frozenset({49983})
        host_unit = "e2b-embed.service"
        credential_keys = ("E2B_API_KEY", "E2B_API_URL")
        secret_key = "E2B_API_KEY"

        def host_units(self):
            return ["embed-unit"]

        def apply_host(self, key):
            return key == "runtime-env"

        def apply_network(self):
            return {"status": "applied", "allowed_cidrs": ["10.0.0.0/8"]}

        def disk(self):
            return {"percent": 10, "free_gb": 500}

        def readiness(self, facts):
            from sbx_control.connections.base import Check
            return [Check("Virtualization", True, "KVM is available")]

    return EmbedStub(), Fake()


class TwoRuntimes:
    """setUp: the two backends, fresh, and nothing remembered of earlier sandboxes."""
    RUNTIME_ENV = TWO

    @classmethod
    def setUpClass(cls):
        from sbx_control import runtime
        cls.builtin = patch.dict(runtime.BUILTIN, {"fake": FAKE})
        cls.builtin.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.builtin.stop()

    def setUp(self):
        super().setUp()
        from sbx_control import runtime
        self.runtime = runtime
        self.embed, self.fake = backends()
        self.enterContext(patch.dict(runtime._backends, {"embed": self.embed, "fake": self.fake}))
        self.enterContext(patch.dict(runtime._index, clear=True))
        self.embed.built["tpl"] = self.fake.built["tpl"] = "ready"


class Registry(TwoRuntimes, Installation):
    def test_the_installation_names_its_runtimes_and_its_default(self):
        self.assertEqual(self.runtime.enabled(), ["embed", "fake"])
        self.assertEqual(self.runtime.default(), "embed")
        self.assertIn("fake", self.runtime.kinds())
        file = self.root / "etc/runtime.env"
        file.write_text(TWO + "SBX_RUNTIME_DEFAULT=fake\n")
        try:
            self.assertEqual(self.runtime.default(), "fake")
            file.write_text(RUNTIME + "SBX_RUNTIMES=embed,nowhere\n")
            with self.assertRaisesRegex(SystemExit, "nowhere, which no installed package provides.*embed, fake"):
                self.runtime.enabled()
            file.write_text(RUNTIME)
            self.assertEqual(self.runtime.enabled(), ["embed"])   # missing: Embed alone, as before
        finally:
            file.write_text(TWO)

    def test_each_backend_gets_only_its_own_keys(self):
        self.runtime.configure({"E2B_API_KEY": "k", "E2B_API_URL": "u", "FAKE_RUNTIME_TOKEN": "f", "OTHER": "x"})
        self.assertEqual(self.embed.credentials, {"E2B_API_KEY": "k", "E2B_API_URL": "u"})
        self.assertEqual(self.fake.credentials, {"FAKE_RUNTIME_TOKEN": "f"})

    def test_listings_tag_each_sandbox_with_its_runtime_and_calls_go_to_it(self):
        on_embed = self.runtime.create("tpl", timeout=600, metadata={"sbx": "a"})
        on_fake = self.runtime.create("tpl", timeout=600, metadata={"sbx": "b"}, kind="fake")
        self.assertEqual((list(self.embed.boxes), list(self.fake.boxes)), ([on_embed.sandbox_id], [on_fake.sandbox_id]))
        listed = {i.metadata["sbx"]: i.runtime for i in self.runtime.sandboxes()}
        self.assertEqual(listed, {"a": "embed", "b": "fake"})
        self.runtime._index.clear()   # unknown to the index: each runtime is asked
        self.runtime.pause(on_fake.sandbox_id)
        self.assertEqual(self.fake.boxes[on_fake.sandbox_id].state, "paused")
        self.assertEqual(self.embed.boxes[on_embed.sandbox_id].state, "running")
        self.assertEqual(self.runtime.info(on_fake.sandbox_id).runtime, "fake")
        handle = self.runtime.connect(on_fake.sandbox_id)    # resumes it, on its own runtime
        self.assertEqual(handle.commands.run("echo hi").stdout, "hi\n")
        self.assertEqual(self.runtime.upstream_host(80, on_fake.sandbox_id), f"80-{on_fake.sandbox_id}.fake.sandboxes.internal")
        self.assertEqual(self.runtime.snapshot(on_fake.sandbox_id, "s").runtime, "fake")
        self.assertTrue(self.runtime.kill(on_fake.sandbox_id))
        self.assertFalse(self.runtime.kill(on_fake.sandbox_id))   # no runtime has it now
        with self.assertRaises(self.runtime.Failed):
            self.runtime.of("nobody-has-this")

    def test_timeouts_and_limits_are_each_runtime_s(self):
        self.fake.limit = self.runtime.Limits(timeout_seconds=900, memory_mb=1024, sandboxes=1)
        on_fake = self.runtime.create("tpl", timeout=7200, kind="fake")
        on_embed = self.runtime.create("tpl", timeout=7200)
        self.assertEqual((self.fake.boxes[on_fake.sandbox_id].timeout, self.embed.boxes[on_embed.sandbox_id].timeout),
                         (900, 7200))
        self.assertIn("Fake runtime", self.runtime.beyond_limits({"memory_mb": 4096}, "fake"))
        self.assertEqual(self.runtime.beyond_limits({"memory_mb": 4096}), "")
        self.assertIn("runs at most 1 at once", self.runtime.too_many("fake"))
        self.assertEqual(self.runtime.too_many(), "")

    def test_the_host_side_runs_over_every_runtime(self):
        self.assertEqual(self.runtime.host_units(), ["embed-unit"])        # the fake has none (HostSide)
        self.assertEqual(self.runtime.host_unit_names(), ["e2b-embed.service"])
        self.assertTrue(self.runtime.apply_host("runtime-env"))
        self.assertFalse(self.runtime.apply_host("something-else"))
        self.assertEqual(self.runtime.disk(), {"percent": 10, "free_gb": 500})
        self.assertIsNone(self.runtime.disk("fake"))
        self.assertEqual(self.runtime.capacity(4096, "fake"), {"known": False})
        self.assertEqual(self.runtime.ports(), frozenset({49983, 49999}))
        applied = self.runtime.apply_network()
        self.assertEqual((applied["status"], applied["allowed_cidrs"], sorted(applied["runtimes"])),
                         ("applied", ["10.0.0.0/8"], ["embed", "fake"]))
        self.assertEqual([(c.name, c.runtime) for c in self.runtime.readiness({})], [("Virtualization", "embed")])
        self.assertEqual([(c.name, c.runtime) for c in self.runtime.test()],
                         [("Fake API", "embed"), ("Fake API", "fake")])
        status = self.runtime.status()
        self.assertEqual((status.state, status.title), ("connected", "Sandbox runtimes"))
        self.assertIn("Fake runtime: Fake · 0 running", status.summary)

    def test_the_fake_follows_the_contract(self):
        fake, runtime = self.fake, self.runtime
        handle = fake.create("tpl", timeout=60, metadata={"sbx": "c"})
        handle.files.write("/tmp/x", "data")
        self.assertEqual(handle.files.read("/tmp/x"), "data")
        self.assertEqual(handle.commands.run("cat /tmp/x").stdout, "data")
        with self.assertRaises(runtime.CommandFailed) as failed:
            handle.commands.run("exit 3")
        self.assertEqual(failed.exception.exit_code, 3)
        fake.pause(handle.sandbox_id)
        fake.pause(handle.sandbox_id)          # one already paused stays paused
        with self.assertRaises(runtime.NotRunning):
            handle.commands.run("true")        # a handle's calls raise it once it paused
        with self.assertRaises(runtime.NotRunning):
            fake.set_timeout(handle.sandbox_id, 60)
        self.assertEqual([i.state for i in fake.sandboxes({"sbx": "c"})], ["paused"])
        fake.busy = True
        fake.connect(handle.sandbox_id)
        with self.assertRaises(runtime.Busy):
            fake.pause(handle.sandbox_id)
        fake.full = True
        with self.assertRaises(runtime.NoRoom):
            fake.create("tpl", timeout=60)
        self.assertTrue(fake.kill(handle.sandbox_id))
        with self.assertRaises(runtime.NotRunning):
            fake.info(handle.sandbox_id)
        with self.assertRaises(runtime.Failed):
            fake.build_template(runtime.TemplateSpec().from_template("missing"), alias="x", cpu=1, memory_mb=1,
                                disk_mb=1)


class Choosing(TwoRuntimes, Installation):
    def setUp(self):
        super().setUp()
        from sbx_control import runtimes
        self.runtimes = runtimes

    def test_the_request_then_the_repo_then_the_default(self):
        self.assertEqual(self.runtimes.choose("owner/app"), "embed")
        self.runtimes.set_setting("owner/app", "fake", by="test")
        self.assertEqual(self.runtimes.choose("owner/app"), "fake")
        self.assertEqual(self.runtimes.choose("owner/app", "embed"), "embed")
        self.assertEqual(self.runtimes.used_by("owner/app"), {"embed", "fake"})
        self.assertEqual(self.runtimes.used_by("owner/other"), {"embed"})
        self.runtimes.set_setting("owner/app", None, by="test")
        self.assertEqual(self.runtimes.choose("owner/app"), "embed")

    def test_refusals_name_the_runtime_and_the_reason(self):
        with self.assertRaisesRegex(self.runtimes.Refused, "there is no runtime nope"):
            self.runtimes.choose("owner/app", "nope")
        with patch.dict(self.runtime.BUILTIN, {"other": FAKE}), \
                self.assertRaisesRegex(self.runtimes.Refused, "the other runtime is not enabled"):
            self.runtimes.choose("owner/app", "other")
        with self.assertRaisesRegex(self.runtimes.Refused, "no runtime nope is enabled"):
            self.runtimes.set_setting("owner/app", "nope", by="test")
        with patch("sbx_control.hosted_services.attached_to", return_value=["pg"]):
            with self.assertRaisesRegex(self.runtimes.Refused, r"Fake runtime \(fake\).*managed server pg.*private_networks"):
                self.runtimes.choose("owner/app", "fake")
            self.assertEqual(self.runtimes.choose("owner/app", "embed"), "embed")
        self.fake.limit = self.runtime.Limits(memory_mb=1024)
        with self.assertRaisesRegex(self.runtimes.Refused, r"Fake runtime \(fake\) can't run it: resources.memory_mb is 4096"):
            self.runtimes.choose("owner/app", "fake", {"cpu": 2, "memory_mb": 4096, "disk_mb": 8192})

    def test_a_managed_server_bound_by_the_repo_counts(self):
        from sbx_control import hosted_services
        with self.state().db() as conn:
            conn.execute("insert into managed_servers (name, kind, config) values ('pg', 'postgres', 'x')")
        with patch.object(hosted_services.attachments, "effective", return_value=[{"connections": {"db": "pg"}}]):
            self.assertEqual(hosted_services.attached_to("owner/app"), ["pg"])
        with patch.object(hosted_services.attachments, "effective", return_value=[{"connections": {"db": "neon"}}]):
            self.assertEqual(hosted_services.attached_to("owner/app"), [])

    def state(self):
        from sbx_control import state
        return state

    def test_a_create_runs_on_the_chosen_runtime_and_records_it(self):
        from sbx_control import core, sandboxes, template_builds
        self.runtimes.set_setting("owner/app", "fake", by="test")
        with patch.object(template_builds, "template_for", return_value="tpl") as template_for, \
                patch.object(core, "create", return_value={"sandbox_id": "x"}) as create, \
                patch.object(core, "find", return_value=None), patch.object(sandboxes.lifecycle, "in_background"):
            sandboxes.create_locked(sandboxes.CreateSandbox(name="boxed", repo="owner/app"), "me@example.test")
        self.assertEqual(template_for.call_args.args, ("owner/app", "fake"))
        self.assertEqual(create.call_args.kwargs["kind"], "fake")
        self.assertEqual(sandboxes.rows()["boxed"]["runtime"], "fake")


class Templates(TwoRuntimes, Installation):
    def setUp(self):
        super().setUp()
        from sbx_control import template_builds, templates
        self.builds, self.templates = template_builds, templates

    def test_templates_are_built_and_found_per_runtime(self):
        self.builds.build_now(self.templates.BASE, "fake")
        name = self.builds.built(self.templates.BASE, "fake")
        self.assertIn(name, self.fake.built)
        self.assertNotIn(name, self.embed.built)
        with self.assertRaisesRegex(self.builds.NotBuilt, "not built on E2B Embed yet"):
            self.builds.built(self.templates.BASE)
        self.assertEqual(self.builds.state(self.templates.BASE, "fake")["runtime"], "fake")
        self.assertEqual(self.builds.stale("fake"), [self.templates.DEFAULT])
        self.assertEqual(self.builds.stale("embed"), [self.templates.BASE, self.templates.DEFAULT])
        self.builds.build_now(self.templates.DEFAULT, "fake")
        self.assertEqual(self.builds.template_for(None, "fake"), self.builds.plan(self.templates.DEFAULT).name)
        self.assertEqual(self.builds.remove(self.templates.DEFAULT, "fake")["deleted"],
                         [self.builds.plan(self.templates.DEFAULT).name])
        self.assertEqual(self.builds.state(self.templates.BASE, "fake")["status"], "built")   # the other key stays

    def test_a_repo_s_template_only_on_the_runtimes_its_sandboxes_use(self):
        from sbx_control import runtimes
        with patch.object(self.builds.recipes, "with_recipe", return_value=["owner/app"]), \
                patch.object(self.builds.settings, "trust", return_value="trusted"), \
                patch.object(self.builds, "own", return_value=True):
            self.assertNotIn("owner/app", self.builds.keys("fake"))
            self.assertIn("owner/app", self.builds.keys("embed"))      # the default always
            runtimes.set_setting("owner/app", "fake", by="test")
            self.assertIn("owner/app", self.builds.keys("fake"))

    def test_rows_from_before_several_runtimes_become_the_default_s(self):
        from sbx_control import state
        with state.db() as conn:
            conn.execute("drop table template_builds")
            conn.execute("create table template_builds (repo text primary key, status text not null, started_at text, "
                         "finished_at text, error text, fingerprint text, built_fingerprint text, built_template text, "
                         "pid integer, built_memory_mb integer)")
            conn.execute("insert into template_builds (repo, status, built_template) values ('sbx-base', 'built', 'b-1')")
        state.migrate()
        with state.db() as conn:
            rows = conn.execute("select runtime, repo, status, built_template from template_builds").fetchall()
        self.assertEqual(rows, [("embed", "sbx-base", "built", "b-1")])
        self.assertEqual(self.builds.built(self.templates.BASE), "b-1")

    def test_the_repo_template_status_names_its_runtime_and_lists_the_others(self):
        from sbx_control import readiness
        found = readiness.template("owner/app")
        self.assertEqual(found["runtime"], "embed")
        self.assertEqual([r["runtime"] for r in found["runtimes"]], ["fake"])


class Previews(TwoRuntimes, Installation):
    def test_the_gate_names_the_sandbox_s_runtime(self):
        from sbx_control import gate
        handle = self.runtime.create("tpl", timeout=600, metadata={"sbx": "p", "owner": "me"}, kind="fake")
        self.assertEqual(gate.route(3000, handle.sandbox_id),
                         {"X-Sbx-Upstream": f"3000-{handle.sandbox_id}.fake.sandboxes.internal", "X-Sbx-Runtime": "fake"})
        embed = self.runtime.create("tpl", timeout=600, metadata={"sbx": "q", "owner": "me"})
        self.assertEqual(gate.route(3000, embed.sandbox_id)["X-Sbx-Runtime"], "embed")
        with patch.object(self.embed, "own_route", return_value=True):
            self.assertEqual(self.runtime.own_route("sandbox.example.test", "", ""), ("embed", True))
        self.assertIsNone(self.runtime.own_route("app.example.test", "", ""))

    def test_nginx_has_an_upstream_per_runtime_and_a_map_from_the_kind(self):
        from sbx_control import ip_previews
        config = ip_previews.nginx("203.0.113.10", "bootstrap")
        self.assertIn("upstream sbx_runtime_embed { server 127.0.0.1:3002; }", config)
        self.assertIn("upstream sbx_runtime_fake { server 127.0.0.1:3999; }", config)
        self.assertIn("map $ip_runtime $ip_ingress {\n  default http://sbx_runtime_embed;\n"
                      "  embed http://sbx_runtime_embed;\n  fake http://sbx_runtime_fake;\n}", config)
        self.assertIn("auth_request_set $ip_runtime $upstream_http_x_sbx_runtime;", config)
        self.assertIn("proxy_pass $ip_ingress;", config)
        for header in ("E2b-Sandbox-Id", "E2b-Sandbox-Port", "X-Fake-Sandbox"):
            self.assertIn(f'proxy_set_header {header} "";', config)
        domain = ip_previews.runtime_upstreams()
        self.assertIn("map $sbx_runtime $sbx_ingress {", domain)
        self.assertEqual(ip_previews.default_ingress(), "http://sbx_runtime_embed")

    def test_one_runtime_keeps_one_upstream_to_its_ingress(self):
        from sbx_control import ip_previews
        with patch.object(self.runtime, "enabled", return_value=["embed"]):
            found = ip_previews.runtime_upstreams()
        self.assertEqual(found.count("upstream sbx_runtime_"), 1)
        self.assertIn("upstream sbx_runtime_embed { server 127.0.0.1:3002; }", found)
        self.assertIn("default http://sbx_runtime_embed;", found)


class Verify(TwoRuntimes, Installation):
    def setUp(self):
        super().setUp()
        from sbx_control import runtimes, template_builds, templates
        self.runtimes = runtimes
        template_builds.build_now(templates.BASE, "fake")
        box = lambda: next(iter(self.fake.boxes.values()))   # noqa: E731
        self.enterContext(patch.object(runtimes, "_fetch", side_effect=lambda url, host: box().files[runtimes.VERIFY_FILE]))
        self.enterContext(patch.object(runtimes.time, "sleep"))

    def test_verify_takes_a_sandbox_through_its_life_and_deletes_it(self):
        said = []
        self.assertTrue(self.runtimes.verify("fake", say=said.append))
        steps = [line.split(":")[0].strip() for line in said if line.startswith("  ")]
        self.assertEqual(steps, ["ok  create from the base template", "ok  run a command", "ok  write and read a file",
                                 "ok  pause", "ok  resume", "ok  reach a preview port through the gate", "ok  delete"])
        self.assertEqual(self.fake.boxes, {})
        self.assertEqual(said[-1], "passed")

    def test_a_failed_step_skips_the_rest_and_still_deletes_the_sandbox(self):
        self.fake.busy = True
        said = []
        self.assertFalse(self.runtimes.verify("fake", say=said.append))
        self.assertTrue(any(line.startswith("  NO  pause: Busy") for line in said))
        self.assertTrue(any(line.startswith("  --  resume: skipped") for line in said))
        self.assertTrue(any(line.startswith("  ok  delete") for line in said))
        self.assertEqual(self.fake.boxes, {})
        self.assertEqual(said[-1], "failed")

    def test_admin_lists_and_verifies(self):
        from sbx_control import admin
        with patch("sbx_control.platform.connect"), patch.object(self.runtimes, "verify", return_value=True) as verify:
            self.assertEqual(admin.main(["runtime", "verify", "fake"]), 0)
            self.assertEqual(admin.main(["runtime", "verify", "nope"]), 2)
        verify.assert_called_once_with("fake")
        with patch("sbx_control.platform.connect"), patch("sys.stdout") as out:
            self.assertEqual(admin.main(["runtime", "list"]), 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("fake", printed)
        self.assertIn("E2B Embed", printed)


class Api(TwoRuntimes, Server):
    def test_the_wizard_lists_the_runtimes_with_the_setup_token(self):
        self.assertEqual(self.client.get("/api/runtimes").status_code, 401)
        found = self.client.get("/api/runtimes", headers=self.headers)
        self.assertEqual(found.status_code, 200, found.text)
        body = found.json()
        self.assertEqual(body["default"], "embed")
        embed, fake = body["runtimes"]
        self.assertEqual({k: embed[k] for k in ("kind", "title", "default", "host")},
                         {"kind": "embed", "title": "E2B Embed", "default": True, "host": True})
        self.assertEqual(fake["capabilities"], {"snapshots": True, "private_networks": False, "egress_rules": False})
        self.assertEqual(fake["status"], {"state": "connected", "summary": "Fake · 0 running"})
        self.assertEqual(fake["limits"], {})
        self.assertEqual((fake["host"], fake["default"]), (False, False))

    def test_a_repo_s_runtime_is_set_by_admins_and_a_create_refusal_is_a_400(self):
        self.repo_admin()
        self.settings_add("owner/app")
        changed = self.client.put("/api/repos/owner/app/runtime", json={"runtime": "fake"})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(self.client.get("/api/repos/owner/app").json()["runtime"], "fake")
        self.assertEqual(self.client.put("/api/repos/owner/app/runtime", json={"runtime": "nope"}).status_code, 400)
        self.assertEqual(self.client.put("/api/repos/owner/app/runtime", json={"runtime": None}).status_code, 200)
        self.assertIsNone(self.client.get("/api/repos/owner/app").json()["runtime"])
        refused = self.client.post("/api/sandboxes", json={"name": "boxed", "repo": "owner/app", "runtime": "nope"})
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertIn("nope", refused.json()["detail"])
        listed = self.client.get("/api/sandbox-list").json()
        self.assertEqual(listed["capacity"]["runtime"], "embed")
        self.assertEqual(sorted(listed["capacities"]), ["embed", "fake"])

    def test_sandbox_payloads_name_their_runtime(self):
        from sbx_control import sandboxes
        handle = self.runtime.create("tpl", timeout=600, kind="fake",
                                     metadata={"sbx": "seen", "owner": "me@example.test", "slug": "seen"})
        info = self.runtime.info(handle.sandbox_id)
        self.assertEqual(sandboxes.describe(info)["runtime"], "fake")
        self.assertEqual(sandboxes.describe(dataclasses.replace(info, runtime=""))["runtime"], "fake")   # of()

    def settings_add(self, repo):
        from sbx_control import settings
        settings.add_repo(repo, by="test")


class Upgrade(Installation):
    """An installation from before several runtimes: its sandboxes ran on the default."""

    def test_existing_sandbox_rows_get_the_default_runtime(self):
        from sbx_control import sandboxes, state
        with state.db() as conn:
            conn.execute("insert into sandboxes (name, owner, branch, slug, runtime) values ('old', 'me', 'main', 'old', '')")
        try:
            state.migrate()
            self.assertEqual(sandboxes.rows()["old"]["runtime"], "embed")
        finally:
            with state.db() as conn:
                conn.execute("delete from sandboxes")

    def test_one_runtime_routes_without_asking(self):
        from sbx_control import runtime
        with patch.object(runtime, "get", wraps=runtime.get) as get:
            self.assertEqual(runtime.of("anything").kind, "embed")
        get.assert_called_once_with("embed")
        self.assertNotIn("anything", runtime._index)
