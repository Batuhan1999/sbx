"""The runtime boundary: Embed's backend turns the E2B SDK into plain
runtime values, and nothing else in sbx imports the SDK."""
import ast
import datetime
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

PACKAGE = Path(__file__).parents[1] / "sbx_control"


class Boundary(unittest.TestCase):
    def test_only_the_e2b_based_backends_import_the_e2b_sdk(self):
        # The shared adapter, Embed and E2B Cloud.
        allowed = [PACKAGE / "runtime" / name for name in ("e2b", "embed", "e2b_cloud")]
        found = []
        for path in PACKAGE.rglob("*.py"):
            if any(backend in path.parents for backend in allowed):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                if any(n == "e2b" or n.startswith("e2b.") for n in names):
                    found.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
        self.assertEqual(found, [], "reach the runtime through sbx_control.runtime")

    def test_only_the_runtime_package_imports_a_backend(self):
        runtime = PACKAGE / "runtime"
        backends = [f"sbx_control.runtime.{p.name}" for p in runtime.iterdir() if (p / "__init__.py").exists()]
        self.assertEqual(sorted(backends), ["sbx_control.runtime.boat", "sbx_control.runtime.e2b",
                                            "sbx_control.runtime.e2b_cloud", "sbx_control.runtime.embed",
                                            "sbx_control.runtime.fake"])
        found = []
        for path in PACKAGE.rglob("*.py"):
            if runtime in path.parents:
                continue
            package = ["sbx_control", *path.relative_to(PACKAGE).parent.parts]
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = ".".join([*package[:len(package) - node.level + 1], node.module or ""] if node.level
                                    else [node.module or ""]).rstrip(".")
                    names = [base, *(f"{base}.{a.name}" for a in node.names)]
                else:
                    continue
                if any(n == b or n.startswith(f"{b}.") for n in names for b in backends):
                    found.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
        self.assertEqual(found, [], "reach the runtime's backend through sbx_control.runtime")

    def test_a_runtime_without_a_loopback_preview_says_so(self):
        from sbx_control import runtime
        hosted = SimpleNamespace(title="Hosted", kind="embed", sandboxes=lambda *_: [])
        with patch.dict(runtime._backends, {"embed": hosted}), self.assertRaisesRegex(ValueError, "Hosted has no loopback"):
            runtime.preview("box", None, 0)

    def test_the_settings_effects_name_no_runtime(self):
        from sbx_control.settings import HostUnit
        self.assertEqual([unit for unit in HostUnit if "embed" in unit or "e2b" in unit], [])


class Embed(unittest.TestCase):
    def setUp(self):
        from sbx_control.runtime.embed import Embed
        self.embed = Embed()

    def test_listing_reads_every_page_and_gives_plain_states(self):
        from e2b import Sandbox, SandboxState
        started = datetime.datetime(2026, 11, 15, tzinfo=datetime.timezone.utc)
        first = SimpleNamespace(sandbox_id="a", metadata=None, state=SandboxState.RUNNING, template_id="t",
                                started_at=started, end_at=started, cpu_count=2, memory_mb=4096)
        second = SimpleNamespace(sandbox_id="b", metadata={"sbx": "b"}, state=SandboxState.PAUSED, template_id="t",
                                 started_at=started, end_at=started, cpu_count=2, memory_mb=4096)
        pages = Mock()
        pages.next_items.side_effect = [[first], [second]]
        type(pages).has_next = PropertyMock(side_effect=[True, False])
        with patch.object(Sandbox, "list", return_value=pages) as listing:
            found = self.embed.sandboxes({"connector": "amp"}, ["running", "paused"])
        query = listing.call_args.kwargs["query"]
        self.assertEqual(query.metadata, {"connector": "amp"})
        self.assertEqual(query.state, [SandboxState.RUNNING, SandboxState.PAUSED])
        self.assertEqual([(i.sandbox_id, i.state, i.metadata) for i in found],
                         [("a", "running", {}), ("b", "paused", {"sbx": "b"})])
        self.assertEqual(found[0].end_at, started)

    def test_the_default_listing_names_no_state(self):
        from e2b import Sandbox
        pages = Mock(has_next=False)
        pages.next_items.return_value = []
        with patch.object(Sandbox, "list", return_value=pages) as listing:
            self.embed.sandboxes()
        self.assertIsNone(listing.call_args.kwargs["query"].state)

    def test_create_passes_only_what_was_asked(self):
        from e2b import Sandbox
        with patch.object(Sandbox, "create", return_value=Mock(sandbox_id="vm")) as create:
            handle = self.embed.create("tpl", timeout=60, metadata={"sbx": "x"})
        self.assertEqual(handle.sandbox_id, "vm")
        self.assertEqual(create.call_args.kwargs, {"timeout": 60, "metadata": {"sbx": "x"}})
        with patch.object(Sandbox, "create", return_value=Mock(sandbox_id="vm")) as create:
            self.embed.create("tpl", timeout=60, envs={}, deny_out=["169.254.169.254/32"], on_timeout="pause",
                              auto_resume=True)
        self.assertEqual(create.call_args.kwargs, {
            "timeout": 60, "metadata": None, "envs": {}, "network": {"deny_out": ["169.254.169.254/32"]},
            "lifecycle": {"on_timeout": "pause", "auto_resume": True}})

    def test_a_failed_command_keeps_its_output_and_its_message(self):
        from e2b import Sandbox, TimeoutException
        from e2b.sandbox.commands.command_handle import CommandExitException
        from sbx_control import runtime
        vm = Mock(sandbox_id="vm")
        vm.commands.run.side_effect = CommandExitException(stdout="out", stderr="err", exit_code=3, error=None)
        with patch.object(Sandbox, "connect", return_value=vm):
            handle = self.embed.connect("vm")
        with self.assertRaises(runtime.CommandFailed) as raised:
            handle.commands.run("false", user="user")
        self.assertEqual((raised.exception.stdout, raised.exception.stderr, raised.exception.exit_code),
                         ("out", "err", 3))
        self.assertIn("err", str(raised.exception))
        self.assertTrue(runtime.failure(raised.exception))  # as the SDK's own error was
        vm.commands.run.side_effect = TimeoutException("too slow")
        with self.assertRaises(runtime.Timeout):
            handle.commands.run("sleep 9")

    def test_connect_passes_a_timeout_only_when_given(self):
        from e2b import Sandbox
        with patch.object(Sandbox, "connect", return_value=Mock(sandbox_id="vm")) as connect:
            self.embed.connect("vm")
            self.embed.connect("vm", timeout=7200)
        self.assertEqual([c.kwargs for c in connect.call_args_list], [{}, {"timeout": 7200}])

    def test_a_busy_node_is_a_busy_runtime(self):
        from e2b import Sandbox
        from e2b.exceptions import ServiceBusyException
        from sbx_control import runtime
        with patch.object(Sandbox, "pause", side_effect=ServiceBusyException("503: node is busy")), \
                self.assertRaisesRegex(runtime.Busy, "node is busy"):
            self.embed.pause("vm")

    def test_a_sandbox_that_paused_or_went_is_not_running(self):
        from e2b import Sandbox, TimeoutException
        from e2b.exceptions import SandboxNotFoundException, SandboxNotRunningException
        from sbx_control import runtime
        gone = SandboxNotFoundException("Sandbox vm not found")
        for call, method in ((lambda: self.embed.set_timeout("vm", 60), "set_timeout"),
                             (lambda: self.embed.pause("vm"), "pause"),
                             (lambda: self.embed.connect("vm"), "connect"),
                             (lambda: self.embed.info("vm"), "get_info")):
            with self.subTest(method), patch.object(Sandbox, method, side_effect=gone), \
                    self.assertRaisesRegex(runtime.NotRunning, "not found"):
                call()
        with patch.object(Sandbox, "pause", return_value=False):   # already paused: what pause is for
            self.embed.pause("vm")
        vm = Mock(sandbox_id="vm")
        with patch.object(Sandbox, "connect", return_value=vm):
            handle = self.embed.connect("vm")
        vm.commands.run.side_effect = SandboxNotRunningException("The sandbox was not found")
        with self.assertRaises(runtime.NotRunning):
            handle.commands.run("true")
        vm.files.read.side_effect = SandboxNotRunningException("The sandbox was not found")
        with self.assertRaises(runtime.NotRunning):
            handle.files.read("/x")
        vm.commands.run.side_effect = TimeoutException("too slow")  # still a timeout, not a pause
        with self.assertRaises(runtime.Timeout) as raised:
            handle.commands.run("sleep 9")
        self.assertNotIsInstance(raised.exception, runtime.NotRunning)

    def test_the_sdk_and_its_http_client_count_as_the_runtime(self):
        from e2b.exceptions import SandboxException
        self.assertTrue(self.embed.failure(SandboxException("500")))
        self.assertFalse(self.embed.failure(ValueError("sbx's own")))

    def test_the_egress_proxy_reaches_only_the_host_ports_and_replaces_an_earlier_run(self):
        from sbx_control import host
        from sbx_control.runtime.embed import network
        old = "-A OUTPUT -m cgroup --path system.slice/docker-old.scope -j SBX-EGRESS"

        def run(command, **_):
            if command[:2] == ["docker", "inspect"]:
                return Mock(stdout="new\n", returncode=0)
            if command[2:] == ["-S", "OUTPUT"]:
                return Mock(stdout=f"-P OUTPUT ACCEPT\n{old}\n", returncode=0)
            return Mock(stdout="", returncode=1 if command[2:4] == ["-N", "SBX-EGRESS"] else 0)
        with patch.object(host, "shell", return_value={"host_ports": "443 21000"}), \
                patch.object(network.subprocess, "run", side_effect=run) as calls:
            network.egress_rules()
        v4 = [c.args[0][2:] for c in calls.call_args_list if c.args[0][0] == "iptables"]
        self.assertIn(["-D", "OUTPUT", "-m", "cgroup", "--path", "system.slice/docker-old.scope", "-j", "SBX-EGRESS"], v4)
        self.assertIn(["-F", "SBX-EGRESS"], v4)
        self.assertIn(["-A", "SBX-EGRESS", "-d", "169.254.169.254/32", "-j", "REJECT"], v4)
        for port in ("443", "21000"):
            self.assertIn(["-A", "SBX-EGRESS", "-p", "tcp", "--dport", port, "-m", "addrtype", "--dst-type", "LOCAL",
                           "-j", "RETURN"], v4)
        self.assertEqual(v4[-1], ["-I", "OUTPUT", "-m", "cgroup", "--path", "system.slice/docker-new.scope",
                                  "-j", "SBX-EGRESS"])
        v6 = [c.args[0][2:] for c in calls.call_args_list if c.args[0][0] == "ip6tables"]
        self.assertNotIn(["-A", "SBX-EGRESS", "-d", "169.254.169.254/32", "-j", "REJECT"], v6)
        self.assertIn("::1/128", v6[-2])

    def test_a_template_spec_becomes_the_same_builder_steps_in_order(self):
        from sbx_control import runtime
        from sbx_control.runtime import embed
        spec = (runtime.TemplateSpec().from_template("base-1").set_user("root").run_cmd("echo one")
                .set_envs({"A": "1"}).set_workdir("/home/user").set_start_cmd("start", 6080))
        steps = []

        class Recorder:
            def __getattr__(self, name):
                return lambda *args, **kwargs: (steps.append((name, args)), self)[1]
        with patch("e2b.Template", return_value=Recorder()), \
                patch("e2b.wait_for_port", side_effect=lambda port: f"port {port}"):
            embed.template(spec)
        self.assertEqual(steps, [("from_template", ("base-1",)), ("set_user", ("root",)), ("run_cmd", ("echo one",)),
                                 ("set_envs", ({"A": "1"},)), ("set_workdir", ("/home/user",)),
                                 ("set_start_cmd", ("start", "port 6080"))])



class Limits(unittest.TestCase):
    """The core checks a runtime's limits before asking it, and says which
    limit and how to raise it; a backend only fills in the values."""

    def setUp(self):
        from sbx_control import runtime
        from sbx_control.runtime.embed import Embed
        self.runtime = runtime
        self.backend = Embed()
        self.limits = runtime.Limits(timeout_seconds=3600, cpu=8, memory_mb=8192, disk_mb=10240, sandboxes=20,
                                     source="your plan", keys={"memory_mb": "MAX_MEMORY_MB", "cpu": "MAX_CPU",
                                                               "sandboxes": "MAX_SANDBOXES"})
        for patcher in (patch.dict(runtime._backends, {"embed": self.backend}),
                        patch.object(self.backend, "limits", side_effect=lambda: self.limits),
                        patch.object(self.backend, "capacity", return_value={"known": False}),
                        patch.object(runtime, "installation_id", return_value="0123456789abcdef")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_size_beyond_a_limit_names_it_and_how_to_raise_it(self):
        why = self.runtime.beyond_limits({"cpu": 2, "memory_mb": 16384, "disk_mb": 8192})
        self.assertIn("resources.memory_mb is 16384 MiB", why)
        self.assertIn("at most 8192 MiB", why)
        self.assertIn("if your plan allows more, set MAX_MEMORY_MB in /etc/sbx/runtime.env", why)
        self.assertEqual(self.runtime.beyond_limits({"cpu": 8, "memory_mb": 8192, "disk_mb": 10240}), "")

    def test_a_runtime_without_limits_allows_anything(self):
        self.limits = self.runtime.Limits()
        self.assertEqual(self.runtime.beyond_limits({"cpu": 64, "memory_mb": 1 << 20, "disk_mb": 1 << 20}), "")
        self.assertEqual(self.runtime.too_many(), "")
        self.assertEqual(self.runtime._capped(7200, self.backend), 7200)

    def test_timeouts_stay_within_the_limit(self):
        from e2b import Sandbox
        sandbox = SimpleNamespace(sandbox_id="sb1", commands=None, files=None)
        with patch.object(Sandbox, "create", return_value=sandbox) as create, \
                patch.object(Sandbox, "connect", return_value=sandbox) as connect, \
                patch.object(Sandbox, "set_timeout") as set_timeout:
            self.runtime.create("t", timeout=7200)
            self.runtime.connect("sb1", timeout=7200)
            self.runtime.set_timeout("sb1", 600)
        self.assertEqual((create.call_args.kwargs["timeout"], connect.call_args.kwargs["timeout"],
                          set_timeout.call_args.args[1]), (3600, 3600, 600))

    def test_a_timeout_limit_leaves_the_keeper_several_renewals(self):
        from sbx_control import keeper
        from sbx_control.runtime import base
        self.assertGreaterEqual(base.MIN_TIMEOUT_SECONDS, 3 * keeper.INTERVAL_SECONDS)
        with self.assertRaisesRegex(ValueError, "at least 300 seconds"):
            self.runtime.Limits(timeout_seconds=60)
        self.assertEqual(self.runtime.Limits(timeout_seconds=300).timeout_seconds, 300)

    def test_the_keeper_keeps_a_sandbox_awake_while_an_operation_holds_it(self):
        from sbx_control import keeper
        info = SimpleNamespace(sandbox_id="sb1", state="running", metadata={"sbx": "box"}, end_at=None)
        idle = Mock(accessed_within=Mock(return_value=False), probe=SimpleNamespace(ssh=False, agents=False, busy=False))
        quiet = SimpleNamespace(sandboxes=SimpleNamespace(detected_previews="off", paused_days=0))
        with patch.object(keeper.sandboxes, "record_transition"), patch.object(keeper, "notice_stopped_app"), \
                patch.object(keeper.settings, "current", return_value=quiet), \
                patch.object(keeper, "use_of", return_value=idle), \
                patch.object(keeper.extensions, "keeper_policy", return_value=None), \
                patch.object(keeper, "keep_awake") as keep_awake:
            self.assertTrue(keeper.look_at(info, held=set()))
            keep_awake.assert_not_called()   # idle and not held: it times out
            self.assertTrue(keeper.look_at(info, held={"box"}))   # its setup still runs
            keep_awake.assert_called_once_with(info)

    def test_embed_limits_come_from_the_host(self):
        from sbx_control.runtime.embed import Embed, capacity
        with patch.object(capacity, "room", return_value={"known": True, "total_mb": 30720}), \
                patch("os.cpu_count", return_value=16):
            self.assertEqual(Embed().limits().known(), {"cpu": 16, "memory_mb": 30720})
        with patch.object(capacity, "room", return_value={"known": False}):
            self.assertIsNone(Embed().limits().memory_mb)

    def test_running_sandboxes_at_the_limit_refuse_another(self):
        running = [self.runtime.Info("vm", {}, "running")] * 20
        with patch.object(self.backend, "sandboxes", return_value=running) as listed:
            why = self.runtime.too_many()
        self.assertEqual(listed.call_args.args, (None, [self.runtime.RUNNING]))
        self.assertIn("20 sandboxes are running", why)
        self.assertIn("MAX_SANDBOXES", why)
        with patch.object(self.backend, "sandboxes", return_value=running[:19]):
            self.assertEqual(self.runtime.too_many(), "")

    def test_a_create_checks_the_templates_memory_when_it_is_given(self):
        from sbx_control import core

        class Reached(Exception):
            pass
        recipe = SimpleNamespace(repo="", size={"cpu": 2, "memory_mb": 4096, "disk_mb": 8192})
        with patch.object(core.recipes, "load", return_value=recipe), patch.object(core, "check_name"), \
                patch.object(core.lifecycle, "operation", side_effect=Reached):
            with self.assertRaisesRegex(core.SandboxError, "resources.memory_mb is 16384 MiB"):
                core.create("box", "box", "me", "main", "", template="snap", memory_mb=16384)
            recipe.size["memory_mb"] = 16384
            with self.assertRaises(Reached):  # a template within the limit passes, whatever the recipe says
                core.create("box", "box", "me", "main", "", template="snap", memory_mb=4096)
            with self.assertRaisesRegex(core.SandboxError, "resources.memory_mb is 16384 MiB"):
                core.create("box", "box", "me", "main", "", template="t")

    def test_core_refuses_a_create_beyond_the_limit_before_the_runtime(self):
        from sbx_control import core
        with patch.object(self.backend, "sandboxes", return_value=[self.runtime.Info("vm", {}, "running")] * 20), \
                self.assertRaisesRegex(core.NoCapacity, "runs at most 20 at once"):
            core.check_capacity(4096)

    def test_unknown_capacity_shows_the_limits(self):
        from sbx_control import core
        room = core.capacity(4096)
        self.assertEqual((room["known"], room["limits"]["sandboxes"]), (False, 20))

    def test_a_recipe_beyond_the_limits_is_refused_when_applied(self):
        import tempfile
        from sbx_control import recipes
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "sbx.yaml").write_text("resources:\n  cpu: 16\n")
            with self.assertRaisesRegex(recipes.RecipeError, "resources.cpu is 16 vCPUs.*MAX_CPU"):
                recipes.validate("owner/app", Path(folder))
            Path(folder, "sbx.yaml").write_text("resources:\n  cpu: 8\n")
            recipes.validate("owner/app", Path(folder))


EMBED_KEYS = {"E2B_API_KEY": "test-only-embed", "E2B_API_URL": "http://127.0.0.1:3000",
              "E2B_SANDBOX_URL": "http://127.0.0.1:3002"}
CLOUD_KEYS = {"E2B_CLOUD_API_KEY": "test-only-cloud"}


class SharedAdapter(unittest.TestCase):
    """Embed and E2B Cloud share the SDK adapter (runtime.e2b) and each pass
    their own connection on every call: nothing in the process environment."""

    def setUp(self):
        from sbx_control.runtime.e2b_cloud import Cloud
        from sbx_control.runtime.embed import Embed
        self.embed, self.cloud = Embed(), Cloud()

    def test_configure_writes_nothing_into_the_process_environment(self):
        import os
        with patch.dict(os.environ, clear=True):
            self.embed.configure(EMBED_KEYS)
            self.cloud.configure(CLOUD_KEYS)
            self.assertEqual({k: v for k, v in os.environ.items() if k.startswith("E2B")}, {})

    def test_embed_keeps_its_keys_and_urls(self):
        self.embed.configure(EMBED_KEYS)
        self.assertEqual(self.embed.connection(), {"api_key": "test-only-embed", "api_url": "http://127.0.0.1:3000",
                                                    "sandbox_url": "http://127.0.0.1:3002"})
        self.embed.configure({"E2B_API_KEY": "test-only-embed", "E2B_HOST": "10.0.0.5"})
        self.assertEqual(self.embed.connection(), {"api_key": "test-only-embed", "api_url": "http://10.0.0.5:3000",
                                                    "sandbox_url": "http://sandbox.10.0.0.5:3002"})
        with self.assertRaisesRegex(SystemExit, "E2B_API_KEY"):
            self.embed.configure({})

    def test_two_backends_in_one_process_each_call_with_their_own_connection(self):
        from e2b import Sandbox
        self.embed.configure(EMBED_KEYS)
        self.cloud.configure(CLOUD_KEYS)
        vm = SimpleNamespace(sandbox_id="vm", commands=None, files=None, traffic_access_token="test-token")
        pages = Mock(has_next=False)
        pages.next_items.return_value = []
        with patch.object(Sandbox, "create", return_value=vm) as create, \
                patch.object(Sandbox, "connect", return_value=vm) as connect, \
                patch.object(Sandbox, "list", return_value=pages) as listing, \
                patch.object(Sandbox, "pause") as pause, patch.object(Sandbox, "kill") as kill, \
                patch.object(Sandbox, "set_timeout") as set_timeout:
            for backend in (self.embed, self.cloud, self.embed):
                backend.create("tpl", timeout=60)
                backend.connect("vm")
                backend.sandboxes()
                backend.pause("vm")
                backend.set_timeout("vm", 600)
                backend.kill("vm")
        for call in (create, connect, listing, pause, kill, set_timeout):
            keys = [c.kwargs.get("api_key") for c in call.call_args_list]
            self.assertEqual(keys, ["test-only-embed", "test-only-cloud", "test-only-embed"], call)
        self.assertEqual(create.call_args_list[1].kwargs["api_url"], "https://api.e2b.app")
        self.assertEqual(create.call_args_list[0].kwargs["api_url"], "http://127.0.0.1:3000")
        self.assertNotIn("sandbox_url", create.call_args_list[1].kwargs)

    def test_template_calls_carry_the_connection_too(self):
        from e2b.api.client.api.templates import get_templates
        from sbx_control import runtime
        self.cloud.configure(CLOUD_KEYS)
        seen = []
        response = SimpleNamespace(status_code=200, parsed=[SimpleNamespace(aliases=["sbx-base"], build_status="ready")])
        with patch.object(get_templates, "sync_detailed",
                          side_effect=lambda client: (seen.append(client), response)[1]), \
                patch("e2b.api.client_sync.get_api_client", side_effect=lambda config: config.api_key):
            self.assertEqual(self.cloud.templates(), [("sbx-base", "ready")])
        self.assertEqual(seen, ["test-only-cloud"])
        with patch("e2b.Template") as template:
            self.cloud.build_template(runtime.TemplateSpec().from_image("ubuntu"), alias="a", cpu=2, memory_mb=2048,
                                      disk_mb=4096)
        self.assertEqual(template.build.call_args.kwargs["api_key"], "test-only-cloud")


class Cloud(unittest.TestCase):
    """E2B Cloud: limits from its keys (the Hobby plan by default), what it
    can do, previews through an https ingress with the sandbox's token."""

    def setUp(self):
        from sbx_control.runtime.e2b_cloud import Cloud
        self.cloud = Cloud()
        self.cloud.configure(CLOUD_KEYS)

    def test_it_is_built_in_and_runs_elsewhere(self):
        from sbx_control import runtime
        self.assertIn("e2b-cloud", runtime.kinds())
        backend = runtime.make("e2b-cloud")
        self.assertEqual((backend.kind, backend.title, backend.host), ("e2b-cloud", "E2B Cloud", False))
        self.assertEqual(backend.capabilities, runtime.Capabilities(snapshots=True, private_networks=False,
                                                                    egress_rules=True))
        self.assertEqual((backend.host_units(), backend.host_commands(None), backend.jobs()), ([], {}, []))
        self.assertEqual(backend.capacity(4096), {"known": False})
        self.assertTrue(backend.apply_host("runtime-env"))
        self.assertEqual(backend.secret_key, "E2B_CLOUD_API_KEY")
        self.assertNotIn("E2B_API_KEY", backend.credential_keys)   # never Embed's

    def test_limits_default_to_the_hobby_plan_and_follow_the_keys(self):
        self.assertEqual(self.cloud.limits().known(), {"timeout_seconds": 3600, "cpu": 8, "memory_mb": 8192,
                                                       "disk_mb": 10240, "sandboxes": 10})
        self.assertEqual(self.cloud.limits().keys["memory_mb"], "E2B_CLOUD_MAX_MEMORY_MB")
        self.cloud.configure({**CLOUD_KEYS, "E2B_CLOUD_MAX_TIMEOUT": "86400", "E2B_CLOUD_MAX_SANDBOXES": "100"})
        self.assertEqual((self.cloud.limits().timeout_seconds, self.cloud.limits().sandboxes), (86400, 100))
        with self.assertRaisesRegex(SystemExit, "E2B_CLOUD_MAX_CPU=many"):
            self.cloud.configure({**CLOUD_KEYS, "E2B_CLOUD_MAX_CPU": "many"})
        with self.assertRaisesRegex(SystemExit, "E2B_CLOUD_API_KEY"):
            self.cloud.configure({})

    def test_previews_go_to_an_https_name_under_the_sandbox_wildcard(self):
        self.assertEqual(self.cloud.ingress, "https://sbx-ingress.e2b.app")
        self.assertEqual(self.cloud.upstream_host(3000, "abc"), "3000-abc.e2b.app")
        self.cloud.configure({**CLOUD_KEYS, "E2B_CLOUD_DOMAIN": "sandboxes.example.test"})
        self.assertEqual(self.cloud.upstream_host(3000, "abc"), "3000-abc.sandboxes.example.test")
        self.assertEqual(self.cloud.connection()["api_url"], "https://api.sandboxes.example.test")
        self.assertIsNone(self.cloud.own_route("anything", "", ""))

    def test_the_traffic_token_is_kept_from_create_and_fetched_once_after_a_restart(self):
        from e2b import Sandbox
        made = SimpleNamespace(sandbox_id="vm1", commands=None, files=None, traffic_access_token="test-token-1")
        with patch.object(Sandbox, "create", return_value=made):
            self.cloud.create("tpl", timeout=600)
        with patch.object(Sandbox, "connect") as connect:
            self.assertEqual(self.cloud.upstream_token("vm1"), "test-token-1")
        connect.assert_not_called()
        again = SimpleNamespace(sandbox_id="vm2", commands=None, files=None, traffic_access_token="test-token-2")
        with patch.object(Sandbox, "connect", return_value=again) as connect:   # unknown here: a restart
            self.assertEqual(self.cloud.upstream_token("vm2"), "test-token-2")
            self.assertEqual(self.cloud.upstream_token("vm2"), "test-token-2")
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(connect.call_args.kwargs["api_key"], "test-only-cloud")
        self.assertNotIn("timeout", connect.call_args.kwargs)   # a running sandbox's timeout stays as it is
        with patch.object(Sandbox, "kill", return_value=True):
            self.cloud.kill("vm2")
        with patch.object(Sandbox, "connect", side_effect=__import__("e2b").exceptions.SandboxNotFoundException("gone")):
            from sbx_control import runtime
            with self.assertRaises(runtime.NotRunning):
                self.cloud.upstream_token("vm2")

    def test_sandboxes_are_created_with_private_ports_whatever_the_project_default(self):
        from e2b import Sandbox
        made = SimpleNamespace(sandbox_id="vm1", commands=None, files=None, traffic_access_token="test-token-1")
        with patch.object(Sandbox, "create", return_value=made) as create:
            self.cloud.create("tpl", timeout=600)
            self.cloud.create("tpl", timeout=600, deny_out=["10.0.0.0/8"])
        self.assertEqual(create.call_args_list[0].kwargs["network"], {"allow_public_traffic": False})
        self.assertEqual(create.call_args_list[1].kwargs["network"],
                         {"allow_public_traffic": False, "deny_out": ["10.0.0.0/8"]})

    def test_a_sandbox_without_a_token_is_deleted_not_served(self):
        from e2b import Sandbox
        from sbx_control import runtime
        made = Mock(sandbox_id="vm1", traffic_access_token=None)
        with patch.object(Sandbox, "create", return_value=made), \
                self.assertRaisesRegex(runtime.Failed, "without a traffic access token"):
            self.cloud.create("tpl", timeout=600)
        made.kill.assert_called_once()
        tokenless = SimpleNamespace(sandbox_id="vm2", commands=None, files=None, traffic_access_token="")
        with patch.object(Sandbox, "connect", return_value=tokenless) as connect, \
                self.assertRaisesRegex(runtime.Failed, "no traffic access token"):
            self.cloud.upstream_token("vm2")
        self.assertEqual(connect.call_count, 1)                 # one ask, then an error: never a loop
        self.assertEqual(self.cloud.upstream_token("vm3", fetch=False), "")   # without fetch: only what it kept

    def test_a_killed_handle_forgets_its_token_and_fetches_lock_per_sandbox(self):
        import threading
        import time
        from e2b import Sandbox
        made = Mock(sandbox_id="vm1", traffic_access_token="test-token-1")
        with patch.object(Sandbox, "create", return_value=made):
            handle = self.cloud.create("tpl", timeout=600)
        handle.kill()
        self.assertEqual(self.cloud.upstream_token("vm1", fetch=False), "")
        # A slow fetch for one sandbox does not hold up another's.
        slow = threading.Event()

        def connect(sandbox_id, **_):
            if sandbox_id == "slow":
                slow.wait(5)
            return SimpleNamespace(sandbox_id=sandbox_id, commands=None, files=None, traffic_access_token=f"t-{sandbox_id}")
        with patch.object(Sandbox, "connect", side_effect=connect):
            waiting = threading.Thread(target=self.cloud.upstream_token, args=("slow",))
            waiting.start()
            time.sleep(0.1)
            began = time.monotonic()
            self.assertEqual(self.cloud.upstream_token("fast"), "t-fast")
            self.assertLess(time.monotonic() - began, 2)
            slow.set()
            waiting.join(5)
        self.assertEqual(self.cloud.upstream_token("slow", fetch=False), "t-slow")

    def test_a_failed_token_fetch_is_remembered_briefly(self):
        from e2b import Sandbox
        from e2b.exceptions import SandboxException
        from sbx_control import runtime
        from sbx_control.runtime import e2b_cloud
        with patch.object(Sandbox, "connect", side_effect=SandboxException("502: bad gateway")) as connect:
            for _ in range(3):   # an outage: one call to E2B, the same answer after
                with self.assertRaisesRegex(runtime.Failed, "did not hand over"):
                    self.cloud.upstream_token("vm9")
        self.assertEqual(connect.call_count, 1)
        later = e2b_cloud.time.monotonic() + e2b_cloud.FETCH_RETRY_SECONDS + 1
        good = SimpleNamespace(sandbox_id="vm9", commands=None, files=None, traffic_access_token="test-token-9")
        with patch.object(e2b_cloud.time, "monotonic", return_value=later), \
                patch.object(Sandbox, "connect", return_value=good):
            self.assertEqual(self.cloud.upstream_token("vm9"), "test-token-9")   # asked again once it passed

    def test_concurrent_requests_in_an_outage_ask_e2b_once(self):
        import threading
        from e2b import Sandbox
        from e2b.exceptions import SandboxException
        from sbx_control import runtime
        entered, release = threading.Event(), threading.Event()

        def connect(*_, **__):
            entered.set()
            release.wait(5)     # the others queue on the sandbox's lock meanwhile
            raise SandboxException("502: bad gateway")
        errors = []

        def ask():
            try:
                self.cloud.upstream_token("vm9")
            except runtime.Failed as error:
                errors.append(error)
        with patch.object(Sandbox, "connect", side_effect=connect) as called:
            first = threading.Thread(target=ask)
            first.start()
            entered.wait(5)
            rest = [threading.Thread(target=ask) for _ in range(5)]
            for thread in rest:
                thread.start()
            release.set()
            for thread in (first, *rest):
                thread.join(5)
        self.assertEqual(called.call_count, 1)
        self.assertEqual(len(errors), 6)

    def test_what_it_keeps_of_sandboxes_that_went_is_pruned_on_a_full_listing(self):
        from e2b import Sandbox
        from sbx_control import runtime
        for sandbox_id in ("kept", "went"):
            made = SimpleNamespace(sandbox_id=sandbox_id, commands=None, files=None, traffic_access_token=f"t-{sandbox_id}")
            with patch.object(Sandbox, "create", return_value=made):
                self.cloud.create("tpl", timeout=600)
        self.cloud._failed["went-too"] = (0, runtime.Failed("x"))
        listed = [runtime.Info("kept", {"sbx_install": "0123456789abcdef"}, "running")]
        with patch.dict(runtime._backends, {"e2b-cloud": self.cloud}), \
                patch.object(runtime, "enabled", return_value=["e2b-cloud"]), \
                patch.object(runtime, "installation_id", return_value="0123456789abcdef"), \
                patch.object(runtime, "_legacy", return_value=set()), \
                patch.object(self.cloud, "sandboxes", return_value=listed):
            runtime.sandboxes({"sbx": "kept"})            # filtered: nothing is pruned on it
            self.assertIn("went", self.cloud._tokens)
            runtime.sandboxes()
        self.assertEqual(set(self.cloud._tokens), {"kept"})
        self.assertEqual(self.cloud._failed, {})

    def test_its_check_lists_the_templates_and_says_what_failed(self):
        with patch.object(self.cloud, "templates", return_value=[("sbx-base", "ready")]):
            self.assertTrue(all(c.ok for c in self.cloud.test()))
        with patch.object(self.cloud, "templates", side_effect=RuntimeError("HTTP 401")):
            check, = self.cloud.test()
        self.assertFalse(check.ok)
        self.assertIn("HTTP 401", check.detail)
        self.assertIn("E2B_CLOUD_API_KEY", check.fix)


if __name__ == "__main__":
    unittest.main()
