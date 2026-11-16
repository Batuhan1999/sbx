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
    def test_only_the_embed_backend_imports_the_e2b_sdk(self):
        backend = PACKAGE / "runtime" / "embed"
        found = []
        for path in PACKAGE.rglob("*.py"):
            if backend in path.parents:
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
        self.assertEqual(sorted(backends), ["sbx_control.runtime.embed", "sbx_control.runtime.fake"])
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
                        patch.object(self.backend, "capacity", return_value={"known": False})):
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
        running = [Mock()] * 20
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
        with patch.object(self.backend, "sandboxes", return_value=[Mock()] * 20), \
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


if __name__ == "__main__":
    unittest.main()
