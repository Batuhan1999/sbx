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


if __name__ == "__main__":
    unittest.main()
