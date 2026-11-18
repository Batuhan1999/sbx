"""The desktop CLI (cli/sbx.py) against a fake sbx-control, and the
API routes it relies on against the real app: what a sandbox's view
carries, waiting on an operation, exec, recipe apply, and the server-side
scaffold the CLI writes out."""
import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import yaml
from support import Server

LAUNCHER = Path(__file__).parents[2] / "cli/sbx.py"

# A sandbox as GET /api/sandboxes/NAME answers, with what POST adds
# (host_key); SandboxViewTests checks the server still sends these fields.
SANDBOX = {"name": "try", "branch": "main", "sandbox_id": "vm1", "readiness": "app_ready", "error": None,
           "lifecycle": {"phase": "app_ready", "active": False, "application": "healthy"},
           "urls": {"web": "https://try.sbx.example"}, "ports": [], "repo": "/home/user/app", "logs": [],
           "ssh_url": "wss://8081-vm1.sbx.example"}

ME = {"email": "me@example.test", "admin": False,
      "agents": {"claude": {"title": "Claude Code", "path": "/run/sbx/claude-env", "command": "claude",
                            "get": ["claude", "setup-token"], "env": "CLAUDE_CODE_OAUTH_TOKEN", "file": ""},
                 "codex": {"title": "Codex", "path": "/run/sbx/codex/auth.json", "command": "codex",
                           "get": ["codex", "login"], "env": "", "file": "~/.codex/auth.json"}},
      "audiences": {"all": "all sandboxes", "previews": "PR previews", "sessions": "agent sessions"}}


def load_cli(home: Path, control: str = "https://control.invalid"):
    """A fresh launcher module whose ~ is home."""
    with patch.dict(os.environ, {"HOME": str(home), "SBX_CONTROL": control}):
        spec = importlib.util.spec_from_file_location("sbx_launcher_under_test", LAUNCHER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class FakeControl:
    """sbx-control's HTTP surface as a table: (method, path) -> handler(query, body)
    returning a JSON value, or (status, value)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.calls.append((self.command, url.path, parse_qs(url.query), body,
                                   self.headers.get("Authorization")))
                handler = fake.routes.get((self.command, url.path))
                result = handler(parse_qs(url.query), body) if handler else (404, {"detail": "no route"})
                status, value = result if isinstance(result, tuple) else (200, result)
                data = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = answer

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-cli-")))
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh/id_ed25519.pub").write_text("ssh-ed25519 AAAA me@mac\n")
        self.routes = {("GET", "/api/me"): lambda q, body: ME}
        self.control = FakeControl(self.routes)
        self.addCleanup(self.control.close)
        self.cli = load_cli(self.home, self.control.url)
        self.enterContext(patch.object(self.cli, "keychain_get",
                                       side_effect=lambda service: "sbx_test" if service == self.cli.API_TOKEN_SERVICE else None))

    def run_cli(self, *argv, stdin="", tty=False):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        args = self.cli.parser().parse_args(argv)
        terminal = io.StringIO(stdin)
        terminal.isatty = lambda: tty
        with patch("sys.stdin", terminal), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                args.func(args)
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def test_up_uses_what_the_server_returns_and_leaves_ssh_config_alone(self):
        (self.home / ".ssh/config").write_text("Host mine\n  User me\n")
        phases = iter([{"phase": "starting_app", "active": True, "application": "starting_app"},
                       SANDBOX["lifecycle"]])
        self.routes[("POST", "/api/sandboxes")] = lambda q, body: {
            **SANDBOX, "host_key": "ssh-ed25519 HOSTKEY", "readiness": "workspace_ready",
            "lifecycle": {"phase": "preparing_workspace", "active": True, "application": "not_started"}}
        self.routes[("GET", "/api/sandboxes/try")] = lambda q, body: {**SANDBOX, "lifecycle": next(phases)}
        code, out, err = self.run_cli("up", "try", "--repo", "owner/app", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["readiness"], "app_ready")
        post = next(c for c in self.control.calls if c[0] == "POST")
        self.assertEqual(post[3]["repo"], "owner/app")
        self.assertEqual(post[3]["public_keys"], "ssh-ed25519 AAAA me@mac\n")   # the test home's key, not the real one
        self.assertEqual(post[4], "Bearer sbx_test")
        waits = [c[2] for c in self.control.calls if c[0] == "GET" and c[1] == "/api/sandboxes/try"]
        self.assertTrue(waits and all(q.get("wait") for q in waits), "readiness is followed with a server-side wait")
        self.assertIn("starting app", err)
        # ssh reaches the sandbox through the server's bridge, pinned to the
        # host key the server returned, without the user's ~/.ssh/config.
        target = self.cli.ssh_target("try")
        self.assertEqual(target[-1], "sbx-try")
        options = dict(o.split("=", 1) for o in target[1:-1:2])
        self.assertEqual(options["HostName"], "vm1")
        self.assertEqual(options["StrictHostKeyChecking"], "yes")
        self.assertTrue(options["ProxyCommand"].endswith("tunnel %h wss://8081-vm1.sbx.example"))
        self.assertIn("sbx-try ssh-ed25519 HOSTKEY", (self.home / ".ssh/sbx/known_hosts").read_text())
        self.assertEqual((self.home / ".ssh/config").read_text(), "Host mine\n  User me\n")

    def test_a_failed_operation_exits_with_its_error(self):
        self.routes[("POST", "/api/sandboxes/try/prepare")] = lambda q, body: {"status": "starting"}
        self.routes[("GET", "/api/sandboxes/try")] = lambda q, body: {
            **SANDBOX, "error": "setup exited 1", "lifecycle": {"phase": "failed", "active": False, "application": "failed"}}
        code, out, err = self.run_cli("sandbox", "prepare", "try")
        self.assertIn("setup exited 1", str(code))
        self.assertEqual(out, "")
        # The failure event says why: the command, its exit code, the end of its output.
        self.routes[("GET", "/api/sandboxes/try/detail")] = lambda q, body: {"events": [
            {"kind": "prepare failed", "detail": "`.sbx/setup` was killed (exit code 137: out of memory?)"},
            {"kind": "create failed", "detail": "older"}]}
        code, out, err = self.run_cli("sandbox", "prepare", "try")
        self.assertIn("setup exited 1\n`.sbx/setup` was killed (exit code 137: out of memory?)\nfix it", str(code))
        self.assertNotIn("older", str(code))

    def test_lifecycle_operations_print_only_the_sandboxs_json_on_stdout(self):
        phases = iter([{"phase": "starting_app", "active": True, "application": "starting_app"}, SANDBOX["lifecycle"]])
        self.routes[("GET", "/api/sandboxes/try")] = lambda q, body: {**SANDBOX, "lifecycle": next(phases, SANDBOX["lifecycle"])}
        for action in ("restart", "resume", "pause"):
            self.routes[("POST", f"/api/sandboxes/try/{action}")] = lambda q, body: SANDBOX
        progress = ""
        for action in ("restart", "resume", "pause"):
            with self.subTest(action=action):
                code, out, err = self.run_cli("sandbox", action, "try")
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["name"], "try")
                progress += err
        self.assertIn("starting app", progress)   # the progress lines, on stderr

    def test_repo_brief_prints_the_servers_setup_brief(self):
        self.routes[("GET", "/api/repos/owner/app/setup-brief")] = lambda q, body: b"# Set up owner/app\n"
        code, out, err = self.run_cli("repo", "brief", "--repo", "owner/app")
        self.assertEqual((code, out), (0, "# Set up owner/app\n\n"))

    def test_catalog_show_lists_outputs_the_env_they_set_and_saved_connections(self):
        self.routes[("GET", "/api/catalog/postgres-clone")] = lambda q, body: {
            "title": "Postgres clone", "kind": "provision", "source": "builtin", "description": "A database per sandbox.",
            "inputs": {}, "outputs": {"url": {"sensitive": True}, "host": {}}, "env": {"DATABASE_URL": "${outputs.url}"},
            "requires": {"server": {"kind": "postgres-server", "expose": {"url": "PG_ADMIN_URL"}}}, "connections": []}
        self.routes[("GET", "/api/catalog/postgres-server")] = lambda q, body: {"connections": [{"key": "dev-pg"}]}
        code, out, err = self.run_cli("catalog", "show", "postgres-clone")
        self.assertEqual(code, 0, err)
        self.assertIn("output url (secret): ${services.KEY.url}", out)
        self.assertIn("output host: ${services.KEY.host}", out)
        self.assertIn("sets DATABASE_URL: ${outputs.url}", out)
        self.assertIn("requires server: a postgres-server connection (url -> PG_ADMIN_URL); saved: dev-pg", out)

    def test_exec_sends_stdin_and_exits_with_the_scripts_code(self):
        self.routes[("POST", "/api/sandboxes/try/exec")] = lambda q, body: {
            "exit_code": 3, "stdout": f"ran {len(body['script'])}\n", "stderr": "warn\n"}
        code, out, err = self.run_cli("sandbox", "exec", "try", "--timeout", "30", stdin="echo hi\n")
        self.assertEqual((code, out, err), (3, "ran 8\n", "warn\n"))
        self.assertEqual(self.control.calls[-1][3], {"script": "echo hi\n", "timeout": 30})

    def test_a_refusal_shows_the_servers_detail(self):
        self.routes[("GET", "/api/sandboxes/try")] = lambda q, body: (409, {"detail": "try is busy"})
        code, out, err = self.run_cli("urls", "try")
        self.assertEqual(code, "sbx-control: try is busy")
        self.routes[("GET", "/api/sandboxes/try")] = lambda q, body: (503, b"The runtime is unavailable.")
        code, out, err = self.run_cli("urls", "try")
        self.assertEqual(code, "sbx-control: The runtime is unavailable.")

    def test_recipe_apply_sends_the_checkouts_files_and_modes(self):
        recipe = self.home / "checkout/.sbx"
        recipe.mkdir(parents=True)
        (recipe / "sbx.yaml").write_text("previews: {web: 3000}\n")
        (recipe / "setup").write_text("#!/bin/sh\n")
        (recipe / "setup").chmod(0o755)
        self.routes[("PUT", "/api/repos/owner/app/recipe")] = lambda q, body: {
            "repo": "owner/app", "files": len(body["files"]), "prebuilt": False}
        code, out, err = self.run_cli("recipe", "apply", str(recipe.parent), "--repo", "owner/app")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.control.calls[-1][3], {"files": {".sbx/setup": "#!/bin/sh\n",
                                                               ".sbx/sbx.yaml": "previews: {web: 3000}\n"},
                                                     "executable": [".sbx/setup"]})

    def test_down_shows_what_the_recipe_cleanup_printed_and_warns_when_it_failed(self):
        answers = iter([{"deleted": "a", "cleanup": {"exit_code": 0, "output": "released the webhook\n"}},
                        {"deleted": "a", "cleanup": {"exit_code": 2, "output": "half done\n"}},
                        {"deleted": "a", "cleanup": {"error": "resuming the sandbox or running it failed: gone"}},
                        {"deleted": "a", "cleanup": None}])
        self.routes[("DELETE", "/api/sandboxes/a")] = lambda q, body: next(answers)
        code, out, err = self.run_cli("down", "a")
        self.assertEqual((code, out.strip(), err.strip()), (0, "sbx-a deleted", "released the webhook"))
        code, out, err = self.run_cli("down", "a")
        self.assertEqual(code, 0)   # the sandbox is gone; the failure is a warning
        self.assertIn("half done", err)
        self.assertIn("warning: .sbx/cleanup failed (it exited with 2)", err)
        code, out, err = self.run_cli("down", "a")
        self.assertIn("running it failed: gone", err)
        code, out, err = self.run_cli("down", "a", "--json")
        self.assertEqual((code, json.loads(out)), (0, {"deleted": "a", "cleanup": None}))

    def test_down_without_an_answer_in_time_says_the_outcome_is_unknown(self):
        def slow(q, body):
            threading.Event().wait(1)   # not time.sleep: some subclasses patch it
            return {"deleted": "a", "cleanup": None}
        self.routes[("DELETE", "/api/sandboxes/a")] = slow
        with patch.object(self.cli, "DOWN_TIMEOUT_SECONDS", 0.2):
            code, out, err = self.run_cli("down", "a")
        self.assertIn("unknown", str(code))
        self.assertIn("sbx ls", str(code))
        self.assertNotIn("deleted\n", out)

    def test_down_with_a_dropped_reply_says_the_outcome_is_unknown_and_never_resends(self):
        import http.client
        import urllib.error
        sent = []
        def dropped(*args, **kwargs):
            sent.append(args)
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        with patch.object(self.cli.Client, "request", dropped):
            code, out, err = self.run_cli("down", "a")
        self.assertIn("unknown", str(code))
        self.assertIn("sbx ls", str(code))
        self.assertEqual(len(sent), 1)
        def unreachable(*args, **kwargs):
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        with patch.object(self.cli.Client, "request", unreachable):
            code, out, err = self.run_cli("down", "a")
        self.assertIn("cannot reach", str(code))
        self.assertNotIn("unknown", str(code))

    def test_recipe_rm_and_template_rm_call_the_delete_routes(self):
        self.routes[("DELETE", "/api/repos/owner/app/recipe")] = lambda q, body: {
            "repo": "owner/app", "removed": True, "recipe": "none"}
        self.routes[("DELETE", "/api/repos/owner/app/template")] = lambda q, body: {
            "key": "owner/app", "deleted": ["owner-app-aaaaaaaa"], "kept_in_use": [], "rebuilds": False}
        code, out, err = self.run_cli("recipe", "rm", "--repo", "owner/app")
        self.assertEqual(code, 0, err)
        self.assertIn("it needs a recipe now", out)
        code, out, err = self.run_cli("template", "rm", "--repo", "owner/app")
        self.assertEqual(code, 0, err)
        self.assertIn("deleted owner-app-aaaaaaaa", out)

    def test_agent_logins_go_into_the_sandbox_where_the_server_says(self):
        (self.home / ".codex").mkdir()
        (self.home / ".codex/auth.json").write_text('{"token": "codex"}')
        self.cli.keychain_get.side_effect = {self.cli.API_TOKEN_SERVICE: "sbx_test",
                                             "sbx-claude-oauth-token": "tok en"}.get
        with patch.object(self.cli, "ssh_target", return_value=["sbx-try"]), \
                patch.object(self.cli.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="")) as run:
            self.cli.hand_credentials("try")
        sent = {call.args[0][-1].split("cat > ")[-1]: call.kwargs["input"] for call in run.call_args_list}
        self.assertEqual(sent, {"/run/sbx/claude-env": "export CLAUDE_CODE_OAUTH_TOKEN='tok en'\n",
                                "/run/sbx/codex/auth.json": '{"token": "codex"}'})

    def test_the_tunnel_waits_while_the_sandbox_gets_ready_then_hands_over_to_websocat(self):
        answers = iter([(503, "1", None), (503, None, None), (101, None, None)])
        with patch.object(self.cli, "bridge_status", side_effect=lambda url, token, timeout: next(answers)) as status, \
                patch.object(self.cli.time, "sleep") as sleep, \
                patch.object(self.cli, "websocat", return_value="/bin/websocat"), \
                patch.object(self.cli.os, "execv") as execv:
            code, _, err = self.run_cli("tunnel", "bx_3f8tnb7k", "wss://8081-bx-5f3f8tnb7k.sbx.example")
        self.assertEqual(code, 0, err)
        self.assertEqual(status.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0])   # Retry-After, else 2 s
        self.assertEqual(err.count("waiting for the sandbox to be ready"), 1)
        self.assertEqual(execv.call_args.args[1][-1], "wss://8081-bx-5f3f8tnb7k.sbx.example")

    def test_the_tunnel_says_what_to_do_when_the_bridge_names_no_sandbox_or_refuses(self):
        for answer, words in (((404, None, None), "received its key, so use that Mac"),
                              ((302, None, None), "`sbx login` again"),
                              ((502, None, "failed"), "runtime reports an error"),
                              ((502, None, None), "still not ready"),   # a raw 502: the bridge not up yet
                              ((503, "2", None), "still not ready")):
            now = [0.0]   # a fake clock: only the tunnel's own sleeps move it, up to the deadline

            def sleep(seconds, now=now):
                now[0] += seconds

            with self.subTest(answer=answer), patch.object(self.cli, "bridge_status", return_value=answer), \
                    patch.object(self.cli.time, "monotonic", side_effect=lambda now=now: now[0]), \
                    patch.object(self.cli.time, "sleep", side_effect=sleep), \
                    patch.object(self.cli, "websocat", return_value="/bin/websocat"), \
                    patch.object(self.cli.os, "execv") as execv:
                with self.assertRaises(SystemExit) as exit:
                    self.cli.tunnel(SimpleNamespace(sandbox_id="vm1", url="wss://8081-vm1.sbx.example"))
            self.assertIn(words, str(exit.exception.code))
            self.assertNotIn("sbx down", str(exit.exception.code))   # never a destructive way out
            execv.assert_not_called()

    def test_the_tunnel_asks_the_gate_for_a_websocket_upgrade_with_the_token(self):
        seen = {}

        class Bridge(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.update(self.headers)
                self.send_response(503)
                self.send_header("Retry-After", "2")
                self.send_header("X-Sbx-Page", "waking")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        plain = lambda host, port, timeout: http.client.HTTPConnection(host, port, timeout=timeout)   # TLS aside
        with patch.object(self.cli.http.client, "HTTPSConnection", side_effect=plain):
            self.assertEqual(self.cli.bridge_status(f"wss://127.0.0.1:{server.server_address[1]}", "sbx_test"),
                             (503, "2", "waking"))
        self.assertEqual((seen["Authorization"], seen["Upgrade"]), ("Bearer sbx_test", "websocket"))

    def test_the_tunnel_sends_the_token_only_over_tls(self):
        with patch.object(self.cli, "bridge_status") as status, patch.object(self.cli.os, "execv") as execv, \
                patch.object(self.cli, "websocat", return_value="/bin/websocat"):
            with self.assertRaises(SystemExit) as exit:
                self.cli.tunnel(SimpleNamespace(sandbox_id="vm1", url="ws://8081-vm1.sbx.example"))
        self.assertIn("must be a wss:// URL", str(exit.exception.code))
        status.assert_not_called()
        execv.assert_not_called()
        with self.assertRaises(ValueError):
            self.cli.bridge_status("ws://8081-vm1.sbx.example", "sbx_test")

    def test_the_tunnel_keeps_to_its_deadline_and_reads_a_date_as_two_seconds(self):
        clock = iter([0, 115, 117, 117, 120])   # deadline, probe timeout, after the probe, the sleep, after it
        with patch.object(self.cli, "bridge_status", return_value=(503, "Wed, 21 Oct 2026 07:28:00 GMT", None)), \
                patch.object(self.cli.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(self.cli.time, "sleep") as sleep, patch.object(self.cli.os, "execv") as execv, \
                patch.object(self.cli, "websocat", return_value="/bin/websocat"):
            with self.assertRaises(SystemExit) as exit:
                self.cli.tunnel(SimpleNamespace(sandbox_id="vm1", url="wss://8081-vm1.sbx.example"))
        self.assertIn("still not ready", str(exit.exception.code))
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0])
        execv.assert_not_called()

    def test_open_points_the_ssh_config_at_the_bridge_the_server_names_now(self):
        self.cli.write_host("try", {**SANDBOX, "ssh_url": "wss://8081-bx_3f8tnb7k.sbx.example", "host_key": "k"})
        self.cli.refresh_host("try", {**SANDBOX, "ssh_url": "wss://8081-bx-5f3f8tnb7k.sbx.example"})
        conf = (self.cli.SSH_DIR / "try.conf").read_text()
        self.assertIn("tunnel %h wss://8081-bx-5f3f8tnb7k.sbx.example\n", conf)
        self.assertNotIn("bx_", conf)
        self.assertIn("  HostKeyAlias sbx-try\n", conf)   # the rest as `sbx up` wrote it

    def test_open_also_refreshes_the_copy_ssh_config_apps_made(self):
        self.cli.write_host("try", {**SANDBOX, "ssh_url": "wss://8081-old.sbx.example", "host_key": "k"})
        self.assertEqual(self.run_cli("ssh-config", "--apps")[0], 0)
        self.cli.refresh_host("try", {**SANDBOX, "ssh_url": "wss://8081-new.sbx.example"})
        copied = self.cli.SSH_CONFIG.read_text()
        self.assertIn("tunnel %h wss://8081-new.sbx.example\n", copied)
        self.assertNotIn("8081-old", copied)

    def test_open_refreshes_a_stale_copy_even_when_the_sandbox_conf_is_current(self):
        self.cli.write_host("try", {**SANDBOX, "ssh_url": "wss://8081-old.sbx.example", "host_key": "k"})
        self.cli.write_host("other", {**SANDBOX, "ssh_url": "wss://8081-other.sbx.example", "host_key": "k"})
        self.assertEqual(self.run_cli("ssh-config", "--apps")[0], 0)
        self.cli.write_host("try", {**SANDBOX, "ssh_url": "wss://8081-new.sbx.example", "host_key": "k"})
        self.cli.refresh_host("try", {**SANDBOX, "ssh_url": "wss://8081-new.sbx.example"})
        copied = self.cli.SSH_CONFIG.read_text()
        self.assertIn("tunnel %h wss://8081-new.sbx.example\n", copied)
        self.assertNotIn("8081-old", copied)
        self.assertIn("tunnel %h wss://8081-other.sbx.example\n", copied)   # other hosts untouched

    def test_edits_of_the_ssh_config_hold_its_lock(self):
        self.cli.write_host("try", {**SANDBOX, "ssh_url": "wss://8081-old.sbx.example", "host_key": "k"})
        held, reads, locked = [], [], [False]
        real_lock, real_write, real_read = self.cli.ssh_config_lock, self.cli.write_atomic, pathlib.Path.read_text

        @contextlib.contextmanager
        def lock():
            with real_lock():
                locked[0] = True
                try:
                    yield
                finally:
                    locked[0] = False

        def write_atomic(path, *args, **kwargs):
            held.append((pathlib.Path(path).name, locked[0]))
            return real_write(path, *args, **kwargs)

        def read_text(path, *args, **kwargs):
            if path.suffix == ".conf":
                reads.append(locked[0])
            return real_read(path, *args, **kwargs)

        with patch.object(self.cli, "ssh_config_lock", lock), patch.object(self.cli, "write_atomic", write_atomic), \
                patch.object(pathlib.Path, "read_text", read_text):
            self.assertEqual(self.run_cli("ssh-config", "--apps")[0], 0)
            self.cli.refresh_host("try", {**SANDBOX, "ssh_url": "wss://8081-new.sbx.example"})
        self.assertEqual(held, [("config", True), ("try.conf", True), ("config", True)])
        self.assertTrue(reads and all(reads))   # the copies are read under the lock that publishes them

    def test_the_ssh_config_is_replaced_whole_keeping_its_link_and_mode(self):
        real = self.home / "dotfiles-ssh-config"
        real.write_text("Host mine\n  User me\n")
        real.chmod(0o640)
        self.cli.SSH_CONFIG.unlink(missing_ok=True)
        self.cli.SSH_CONFIG.symlink_to(real)
        self.assertEqual(self.run_cli("ssh-config")[0], 0)
        self.assertTrue(self.cli.SSH_CONFIG.is_symlink())
        self.assertIn("Host mine", real.read_text())
        self.assertEqual(real.stat().st_mode & 0o777, 0o600)   # sbx's config: owner only
        self.assertEqual([p.name for p in real.parent.glob("*.sbx-tmp")], [])

    def test_open_still_starts_ssh_when_the_gui_copy_cannot_be_written(self):
        self.cli.write_host("try", {**SANDBOX, "ssh_url": "wss://8081-old.sbx.example", "host_key": "k"})
        self.assertEqual(self.run_cli("ssh-config", "--apps")[0], 0)
        real_write = self.cli.write_atomic

        def write_atomic(path, *args, **kwargs):
            if pathlib.Path(path).name == "config":
                raise PermissionError("read-only")
            return real_write(path, *args, **kwargs)

        err = io.StringIO()
        with patch.object(self.cli, "write_atomic", write_atomic), contextlib.redirect_stderr(err):
            self.cli.refresh_host("try", {**SANDBOX, "ssh_url": "wss://8081-new.sbx.example"})
        self.assertIn("tunnel %h wss://8081-new.sbx.example\n", (self.cli.SSH_DIR / "try.conf").read_text())
        self.assertIn("was not updated", err.getvalue())

    def test_a_huge_retry_after_waits_the_cap_and_leading_zeros_count_as_seconds(self):
        for retry, wait in (("9" * 5000, 10.0), ("0003", 3.0), ("000", 0.0)):
            answers = iter([(503, retry, None), (101, None, None)])
            with self.subTest(retry=retry[:8]), \
                    patch.object(self.cli, "bridge_status", side_effect=lambda url, token, timeout: next(answers)), \
                    patch.object(self.cli.time, "sleep") as sleep, \
                    patch.object(self.cli, "websocat", return_value="/bin/websocat"), \
                    patch.object(self.cli.os, "execv"):
                self.cli.tunnel(SimpleNamespace(sandbox_id="vm1", url="wss://8081-vm1.sbx.example"))
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [wait])

    def test_the_tunnel_gives_the_last_probe_only_the_time_left(self):
        clock = iter([0, 119.5, 119.8, 119.8, 120])   # deadline, probe timeout, after it, the sleep, after it
        with patch.object(self.cli, "bridge_status", return_value=(503, "2", None)) as status, \
                patch.object(self.cli.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(self.cli.time, "sleep"), patch.object(self.cli.os, "execv") as execv, \
                patch.object(self.cli, "websocat", return_value="/bin/websocat"):
            with self.assertRaises(SystemExit) as exit:
                self.cli.tunnel(SimpleNamespace(sandbox_id="vm1", url="wss://8081-vm1.sbx.example"))
        self.assertIn("still not ready", str(exit.exception.code))
        self.assertAlmostEqual(status.call_args.kwargs["timeout"], 0.5)
        execv.assert_not_called()

    def test_the_tunnel_reads_only_nonnegative_integer_retry_after(self):
        for retry, wait in (("3", 3.0), ("60", 10.0), ("0", 0.0), ("-1", 2.0), ("1.5", 2.0), ("nan", 2.0), ("²", 2.0)):
            answers = iter([(503, retry, None), (101, None, None)])
            with self.subTest(retry=retry), \
                    patch.object(self.cli, "bridge_status", side_effect=lambda url, token, timeout: next(answers)), \
                    patch.object(self.cli.time, "sleep") as sleep, \
                    patch.object(self.cli, "websocat", return_value="/bin/websocat"), \
                    patch.object(self.cli.os, "execv"):
                self.cli.tunnel(SimpleNamespace(sandbox_id="vm1", url="wss://8081-vm1.sbx.example"))
            self.assertEqual(sleep.call_args.args[0], wait)

    def test_env_set_never_echoes_a_value_unless_plain(self):
        self.routes[("PUT", "/api/repos/owner/app/env/TOKEN")] = lambda q, body: {"secret": body["secret"] is not False}
        with patch.object(self.cli.getpass, "getpass", return_value="s3cret") as hidden, patch("builtins.input") as visible:
            code, out, err = self.run_cli("env", "set", "TOKEN", "--repo", "owner/app", tty=True)
        self.assertEqual(code, 0, err)
        hidden.assert_called_once()
        visible.assert_not_called()
        self.assertEqual(self.control.calls[-1][3]["value"], "s3cret")
        self.assertIsNone(self.control.calls[-1][3]["secret"])  # the server keeps a new value secret
        self.assertNotIn("s3cret", out + err)
        with patch.object(self.cli.getpass, "getpass") as hidden, patch("builtins.input", return_value="shown") as visible:
            code, out, err = self.run_cli("env", "set", "TOKEN", "--plain", "--repo", "owner/app", tty=True)
        hidden.assert_not_called()
        visible.assert_called_once()
        self.assertEqual((self.control.calls[-1][3]["value"], self.control.calls[-1][3]["secret"]), ("shown", False))

    def test_env_set_succeeds_when_delivery_waits_for_the_runtime(self):
        self.routes[("PUT", "/api/repos/owner/app/env/TOKEN")] = lambda q, body: {
            "secret": True, "running": 0, "paused": 0, "pending": True, "detail": "the sandbox runtime did not answer"}
        code, out, err = self.run_cli("env", "set", "TOKEN", "value", "--repo", "owner/app")
        self.assertEqual(code, 0, err)
        self.assertIn("stored; delivery pending", out)

    def test_env_set_installation_sets_an_installation_secret(self):
        self.routes[("PUT", "/api/secrets/SHARED_KEY")] = lambda q, body: {"name": "SHARED_KEY", "running": 1, "paused": 0}
        code, out, err = self.run_cli("env", "set", "SHARED_KEY", "value", "--installation")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.control.calls[-1][3], {"value": "value"})
        self.assertIn("for this installation; applies to 1 running sandbox now", out)

    def test_login_exchanges_the_browsers_one_time_code_with_its_verifier(self):
        import base64
        import urllib.request
        opened = {}

        def browser(url):  # the server redirects the signed-in browser to the CLI's listener with a code
            query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            opened.update(query)
            callback = f"http://127.0.0.1:{query['port']}/callback?state={query['state']}&code=one-time"
            threading.Thread(target=lambda: urllib.request.urlopen(callback, timeout=5).read(), daemon=True).start()

        def exchange(query, body):
            digest = base64.urlsafe_b64encode(hashlib.sha256(body["verifier"].encode()).digest()).rstrip(b"=").decode()
            if body["code"] != "one-time" or digest != opened["challenge"]:
                return 400, {"detail": "bad code"}
            return {"token": "sbx_from_code"}

        self.routes[("POST", "/cli/token")] = exchange
        stored = {}
        with patch.object(self.cli.webbrowser, "open", side_effect=browser), \
                patch.object(self.cli, "keychain_set", side_effect=lambda service, value: stored.update({service: value})):
            code, out, err = self.run_cli("login")
        self.assertEqual(code, 0, err)
        self.assertEqual(stored, {self.cli.API_TOKEN_SERVICE: "sbx_from_code"})
        self.assertNotIn("token", opened)
        self.assertIn("signed in as me@example.test", out)

    def test_ssh_config_includes_the_sandboxes_once_and_removes_cleanly(self):
        original = "Host mine\n  User me\n"
        (self.home / ".ssh/config").write_text(original)
        for _ in range(2):
            self.assertEqual(self.run_cli("ssh-config")[0], 0)
        text = (self.home / ".ssh/config").read_text()
        self.assertEqual(text.count(f"Include \"{self.home}/.ssh/sbx/*.conf\""), 1)
        self.assertTrue(text.endswith(original))
        self.assertEqual(self.run_cli("ssh-config", "--remove")[0], 0)
        self.assertEqual((self.home / ".ssh/config").read_text(), original)

    def test_apps_get_copies_and_claude_desktop_only_the_current_sandboxes(self):
        desktop = self.home / "Library/Application Support/Claude"
        desktop.mkdir(parents=True)
        own = {"name": "box", "sshHost": "box", "id": "1"}
        (desktop / "ssh_configs.json").write_text(json.dumps({"configs": [own], "trustedHosts": ["box"]}))
        self.routes[("POST", "/api/sandboxes")] = lambda q, body: {**SANDBOX, "name": "a", "host_key": "k"}
        self.routes[("DELETE", "/api/sandboxes/a")] = lambda q, body: {}
        self.assertEqual(self.run_cli("up", "a", "--repo", "owner/app", "--no-wait")[0], 0)
        self.assertEqual(self.run_cli("ssh-config", "--apps")[0], 0)
        self.assertIn("Host sbx-a", (self.home / ".ssh/config").read_text())
        saved = json.loads((desktop / "ssh_configs.json").read_text())
        self.assertEqual([c["sshHost"] for c in saved["configs"]], ["box", "sbx-a"])
        self.assertEqual(self.run_cli("down", "a")[0], 0)
        self.assertEqual(self.run_cli("ssh-config", "--apps")[0], 0)
        self.assertNotIn("Host sbx-a", (self.home / ".ssh/config").read_text())
        saved = json.loads((desktop / "ssh_configs.json").read_text())
        self.assertEqual((saved["configs"], saved["trustedHosts"]), ([own], ["box"]))

    def release(self, version, extra=None):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            for name, text in {"./sbx.py": "print()\n", "./VERSION": f"{version}\n", **(extra or {})}.items():
                info = tarfile.TarInfo(name)
                info.size = len(text.encode())
                archive.addfile(info, io.BytesIO(text.encode()))
        return data.getvalue()

    def test_update_installs_only_a_release_that_matches_its_digest(self):
        root = self.home / ".local/share/sbx"
        good = self.release("2")
        for tarball, digest, version in ((good, "0" * 64, "2"), (self.release("3"), None, "2"),
                                         (self.release("2", {"../escape": "x"}), None, "2")):
            with self.subTest(version=version), self.assertRaises(SystemExit):
                self.cli.install(tarball, digest or hashlib.sha256(tarball).hexdigest(), version, root)
            self.assertFalse((root / "current").exists())
        self.assertFalse((self.home / ".local/share/escape").exists())
        self.routes[("GET", "/dist/version")] = lambda q, body: (200, b"2")
        self.routes[("GET", "/dist/sbx.tgz")] = lambda q, body: (200, good)
        self.routes[("GET", "/dist/sbx.tgz.sha256")] = lambda q, body: (200, hashlib.sha256(good).hexdigest().encode())
        self.cli.VERSION = "1"  # a release's VERSION file; a checkout runs as "dev" and never updates
        code, out, err = self.run_cli("update")
        self.assertEqual(code, 0, err)
        self.assertEqual((root / "current").resolve(), (root / "2").resolve())


# GET /api/runtimes/available's E2B Cloud, as the server declares it (runtime/e2b_cloud).
CLOUD_KEY = "test-only-cli-key-91c2"
HOBBY = {"E2B_CLOUD_MAX_TIMEOUT": "3600", "E2B_CLOUD_MAX_CPU": "8", "E2B_CLOUD_MAX_MEMORY_MB": "8192",
         "E2B_CLOUD_MAX_DISK_MB": "10240", "E2B_CLOUD_MAX_SANDBOXES": "10"}
PRO = {**HOBBY, "E2B_CLOUD_MAX_TIMEOUT": "86400", "E2B_CLOUD_MAX_DISK_MB": "20480", "E2B_CLOUD_MAX_SANDBOXES": "100"}
AVAILABLE = [
    {"kind": "embed", "title": "E2B Embed", "host": True, "enabled": False, "default": False, "can_run": False,
     "why_not": "needs /dev/kvm", "managed_by": "install.sh", "settings": [],
     "edit_blocker": {"code": "host", "message": "set up by install.sh", "sandboxes": []},
     "presets": [], "values": {}, "secrets_set": [], "facts": {"kvm": False}, "sandboxes": [], "remove_blocker": None,
     "limit_hit": None, "plan": None},
    {"kind": "e2b-cloud", "title": "E2B Cloud", "host": False, "enabled": False, "default": False, "can_run": True,
     "why_not": "", "managed_by": "", "edit_blocker": None, "values": {}, "secrets_set": [], "facts": {},
     "sandboxes": [], "remove_blocker": None, "limit_hit": None, "plan": None,
     "settings": [{"key": "E2B_CLOUD_API_KEY", "label": "API key", "secret": True, "default": "", "unit": ""},
                  {"key": "E2B_CLOUD_DOMAIN", "label": "Domain", "secret": False, "default": "e2b.app", "unit": ""},
                  *({"key": key, "label": key, "secret": False, "default": value, "unit": "", "min": 1, "max": 99999}
                    for key, value in HOBBY.items())],
     "presets": [{"name": "hobby", "title": "Hobby", "values": HOBBY, "adjustable": []},
                 {"name": "pro", "title": "Pro", "values": PRO,
                  "adjustable": ["E2B_CLOUD_MAX_CPU", "E2B_CLOUD_MAX_MEMORY_MB"]}]},
]


def change(state, operation="add", message="", restarting=False):
    return {"state": state, "operation": operation, "kind": "e2b-cloud", "message": message, "at": "now",
            "restarting": restarting}


class RuntimeCliTests(CliTests):
    """sbx runtimes: list, available, and an admin's add | set | remove | default over the HTTP API,
    following the queued change through sbx-control's restart."""

    def setUp(self):
        super().setUp()
        self.routes[("GET", "/api/runtimes/available")] = lambda q, body: AVAILABLE
        self.enterContext(patch.object(self.cli.time, "sleep"))   # polls without waiting

    def polls(self, *answers):
        """GET /api/runtimes/change answers these in turn, the last one again."""
        found = list(answers)
        self.routes[("GET", "/api/runtimes/change")] = lambda q, body: found.pop(0) if len(found) > 1 else found[0]

    def test_available_says_what_can_run_its_plans_and_settings(self):
        code, out, err = self.run_cli("runtimes", "available")
        self.assertEqual(code, 0, err)
        self.assertIn("embed        E2B Embed: can't run here: needs /dev/kvm", out)
        self.assertIn("e2b-cloud    E2B Cloud: can be added", out)
        self.assertIn("--plan pro", out)
        self.assertIn("adjustable: E2B_CLOUD_MAX_CPU (from 8)", out)
        self.assertIn("--secret E2B_CLOUD_API_KEY (API key: not set; the value on stdin)", out)
        code, out, err = self.run_cli("runtimes", "available", "--json")
        self.assertEqual(json.loads(out), AVAILABLE)

    def test_add_with_a_plan_and_the_key_on_stdin_waits_through_the_restart(self):
        self.routes[("POST", "/api/runtimes")] = lambda q, body: (202, change("queued"))
        done = change("done", message="E2B Cloud (e2b-cloud) is enabled")
        self.polls(change("applying"), {**done, "restarting": True}, (502, b"Bad Gateway"), done)
        code, out, err = self.run_cli("runtimes", "add", "e2b-cloud", "--plan", "pro", "--set",
                                      "E2B_CLOUD_MAX_CPU=16", "--secret", "E2B_CLOUD_API_KEY", stdin=CLOUD_KEY + "\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "E2B Cloud (e2b-cloud) is enabled\n")
        self.assertIn("waiting for sbx-control to restart", err)
        posted = next(c for c in self.control.calls if c[0] == "POST")[3]
        self.assertEqual(posted, {"kind": "e2b-cloud", "plan": "pro", "values": {"E2B_CLOUD_MAX_CPU": "16",
                                                                                 "E2B_CLOUD_API_KEY": CLOUD_KEY}})
        self.assertNotIn(CLOUD_KEY, out + err)
        self.assertGreaterEqual(sum(c[1] == "/api/runtimes/change" for c in self.control.calls), 4)

    def test_secrets_stay_off_the_command_line_and_the_plan_is_the_server_s_to_check(self):
        for argv, said in (
                (("--set", f"E2B_CLOUD_API_KEY={CLOUD_KEY}"), "is a secret: --secret E2B_CLOUD_API_KEY reads it from stdin"),
                (("--set", "NOPE"), "expected KEY=VALUE"),
                (("--secret", "E2B_CLOUD_DOMAIN"), "E2B_CLOUD_DOMAIN is no secret of E2B Cloud"),
                (("--secret", "E2B_CLOUD_API_KEY"), "no value for E2B_CLOUD_API_KEY on stdin")):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli("runtimes", "add", "e2b-cloud", *argv)
                self.assertIn(said, str(code))
        self.assertFalse(any(c[0] == "POST" for c in self.control.calls))   # nothing was sent

    def test_set_custom_limits_and_a_refusal_shows_the_server_s_detail(self):
        self.routes[("PUT", "/api/runtimes/e2b-cloud")] = lambda q, body: (409, {"detail": "set up the secret store first"})
        code, out, err = self.run_cli("runtimes", "set", "e2b-cloud", "--plan", "custom", "--set",
                                      "E2B_CLOUD_MAX_SANDBOXES=50")
        self.assertEqual(code, "sbx-control: set up the secret store first")
        self.assertEqual(self.control.calls[-1][3], {"plan": "custom", "values": {"E2B_CLOUD_MAX_SANDBOXES": "50"}})

    def test_a_failed_change_exits_non_zero_with_why(self):
        self.routes[("PUT", "/api/runtimes/default")] = lambda q, body: (202, change("queued", "default"))
        self.polls(change("failed", "default", "the e2b-cloud runtime is not enabled here"))
        code, out, err = self.run_cli("runtimes", "default", "e2b-cloud")
        self.assertEqual(code, "the runtime change failed: the e2b-cloud runtime is not enabled here")
        self.assertEqual(self.control.calls[-2][3] if self.control.calls[-2][0] == "PUT" else
                         next(c[3] for c in self.control.calls if c[0] == "PUT"), {"kind": "e2b-cloud"})

    def test_test_prints_each_check_and_fails_when_one_does(self):
        checks = [{"name": "E2B Cloud API", "ok": True, "detail": "answers; 3 template names", "fix": "", "runtime": "e2b-cloud"},
                  {"name": "Base template", "ok": False, "detail": "its last build failed: boom",
                   "fix": "build it again: sbx-admin jobs run template@e2b-cloud:sbx-base", "runtime": "e2b-cloud"}]
        self.routes[("POST", "/api/runtimes/e2b-cloud/test")] = lambda q, body: {"kind": "e2b-cloud", "ok": False, "checks": checks}
        code, out, err = self.run_cli("runtimes", "test", "e2b-cloud")
        self.assertEqual(code, 1)
        self.assertIn("ok  E2B Cloud API: answers; 3 template names", out)
        self.assertIn("NO  Base template: its last build failed: boom — build it again", out)
        self.assertNotIn("how to:", out)   # no page for it
        checks[0] = {**checks[0], "ok": False, "detail": "AuthenticationException: 401",
                     "help_url": "https://github.com/o/sbx/blob/main/docs/howto/e2b.md#api-key"}
        code, out, err = self.run_cli("runtimes", "test", "e2b-cloud")
        self.assertIn("NO  E2B Cloud API: AuthenticationException: 401\n    how to: "
                      "https://github.com/o/sbx/blob/main/docs/howto/e2b.md#api-key", out)
        code, out, err = self.run_cli("runtimes", "test", "e2b-cloud", "--json")
        self.assertEqual(json.loads(out)["checks"][0]["help_url"], checks[0]["help_url"])
        checks[0] = {**checks[0], "ok": True}
        self.routes[("POST", "/api/runtimes/e2b-cloud/test")] = lambda q, body: {"kind": "e2b-cloud", "ok": True, "checks": checks[:1]}
        code, out, err = self.run_cli("runtimes", "test", "e2b-cloud", "--json")
        self.assertEqual((code, json.loads(out)["ok"]), (0, True))
        self.routes[("POST", "/api/runtimes/nope/test")] = lambda q, body: (400, {"detail": "the nope runtime is not enabled here"})
        self.assertEqual(self.run_cli("runtimes", "test", "nope")[0], "sbx-control: the nope runtime is not enabled here")

    def test_remove_without_waiting(self):
        self.routes[("DELETE", "/api/runtimes/e2b-cloud")] = lambda q, body: (202, change("queued", "remove"))
        code, out, err = self.run_cli("runtimes", "remove", "e2b-cloud", "--no-wait")
        self.assertEqual(code, 0, err)
        self.assertIn("queued: remove e2b-cloud", out)
        self.assertFalse(any(c[1] == "/api/runtimes/change" for c in self.control.calls))

    def test_the_list_says_when_none_is_set_up_and_names_a_plan(self):
        self.routes[("GET", "/api/runtimes")] = lambda q, body: {"default": "", "runtimes": []}
        self.assertIn("no sandbox runtime is set up", self.run_cli("runtimes")[1])
        self.routes[("GET", "/api/runtimes")] = lambda q, body: {"default": "e2b-cloud", "runtimes": [{
            "kind": "e2b-cloud", "title": "E2B Cloud", "default": True, "host": False, "plan": {"name": "pro", "title": "Pro"},
            "status": {"state": "connected", "summary": "0 running"}}]}
        self.assertIn("E2B Cloud (Pro plan): 0 running", self.run_cli("runtimes")[1])

    def test_add_without_a_plan_is_on_hobby_and_set_without_one_sends_only_what_it_names(self):
        self.routes[("POST", "/api/runtimes")] = lambda q, body: (202, change("queued"))
        self.routes[("PUT", "/api/runtimes/e2b-cloud")] = lambda q, body: (202, change("queued", "change"))
        self.polls(change("done", message="ok"))
        code, out, err = self.run_cli("runtimes", "add", "e2b-cloud", "--secret", "E2B_CLOUD_API_KEY",
                                      stdin=CLOUD_KEY + "\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(next(c for c in self.control.calls if c[0] == "POST")[3],
                         {"kind": "e2b-cloud", "plan": "hobby", "values": {"E2B_CLOUD_API_KEY": CLOUD_KEY}})
        code, out, err = self.run_cli("runtimes", "set", "e2b-cloud", "--set", "E2B_CLOUD_MAX_CPU=16")
        self.assertEqual(code, 0, err)
        self.assertEqual(next(c for c in self.control.calls if c[0] == "PUT")[3],
                         {"values": {"E2B_CLOUD_MAX_CPU": "16"}})       # set without --plan: the plan stays

    def test_waiting_retries_only_a_restart_and_json_prints_the_outcome(self):
        self.routes[("PUT", "/api/runtimes/default")] = lambda q, body: (202, change("queued", "default"))
        done = change("done", "default", "new sandboxes run on E2B Cloud")
        self.polls((503, b"starting"), (504, b"gateway"), done)
        code, out, err = self.run_cli("runtimes", "default", "e2b-cloud", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), done)
        self.polls((404, {"detail": "no route"}), done)                # anything else: at once
        code, out, err = self.run_cli("runtimes", "default", "e2b-cloud")
        self.assertEqual(code, "sbx-control: no route")
        self.routes[("PUT", "/api/runtimes/default")] = lambda q, body: change("done", "default", "already the default")
        code, out, err = self.run_cli("runtimes", "default", "e2b-cloud")
        self.assertEqual((code, out), (0, "already the default\n"))


SETUP_STATE = {"complete": False, "can_complete": False, "host": {"address": "203.0.113.7"}, "link": "https://x/setup",
               "access": {"url": "https://203.0.113.7", "certificate_error": ""},
               "steps": [{"step": "address", "done": False, "summary": "not chosen"},
                         {"step": "sign_in", "done": False, "summary": "none set yet"},
                         {"step": "runtime", "done": False, "summary": "no sandbox runtime is set up"}]}


def endpoint(state, **more):
    return {"name": "dev-pg", "address": "dev-pg.sbx.example.com", "kind": "managed-server", "server": "dev_pg",
            "server_kind": "postgres", "target": "172.29.50.10:5432", "state": state, "message": "", "used_by": [],
            **more}


def endpoints(*found, applying=False, failure="", candidates=()):
    return {"endpoints": list(found), "candidates": list(candidates), "domain": "sbx.example.com", "needed": True,
            "applying": applying, "failure": failure}


class EndpointCliTests(CliTests):
    """sbx endpoints: list, add and rm over the HTTP API, waiting until the server applied the change."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(self.cli.time, "sleep"))

    def polls(self, *answers):
        found = list(answers)
        self.routes[("GET", "/api/endpoints")] = lambda q, body: found.pop(0) if len(found) > 1 else found[0]

    def test_ls_shows_each_and_the_servers_without_one(self):
        self.polls(endpoints(endpoint("on", used_by=["owner/app"], repos=["owner/app"]),
                             candidates=[{"server": "dev_ch", "kind": "clickhouse", "address": "dev-ch.sbx.example.com"}]))
        code, out, err = self.run_cli("endpoints")
        self.assertEqual(code, 0, err)
        self.assertIn("dev-pg               on        dev-pg.sbx.example.com -> 172.29.50.10:5432 (dev_pg; for owner/app); used by owner/app", out)
        self.assertIn("sbx endpoints add --server dev_ch", out)
        code, out, err = self.run_cli("endpoints", "ls", "--json")
        self.assertEqual(json.loads(out)["endpoints"][0]["name"], "dev-pg")

    def test_add_waits_until_it_is_on(self):
        self.routes[("POST", "/api/endpoints")] = lambda q, body: (202, endpoint("applying"))
        self.polls(endpoints(endpoint("applying"), applying=True), endpoints(endpoint("on")))
        code, out, err = self.run_cli("endpoints", "add", "--server", "dev_pg")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "dev-pg: on, at dev-pg.sbx.example.com\n")
        self.assertEqual(next(c for c in self.control.calls if c[0] == "POST")[3], {"server": "dev_pg"})

    def test_add_an_internal_host_for_its_repos_without_waiting(self):
        self.routes[("POST", "/api/endpoints")] = lambda q, body: (202, endpoint("applying", name="temporal"))
        code, out, err = self.run_cli("endpoints", "add", "--name", "temporal", "--target", "10.0.0.5:7233",
                                      "--repo", "owner/app", "--repo", "other/app", "--no-wait", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["state"], "applying")
        self.assertEqual(self.control.calls[-1][3], {"name": "temporal", "target": "10.0.0.5:7233",
                                                     "repos": ["owner/app", "other/app"]})

    def test_grant_and_revoke_one_repo_at_a_time(self):
        temporal = endpoint("on", name="temporal", kind="internal", server="", repos=["owner/app"])
        self.routes[("POST", "/api/endpoints/temporal/repos")] = lambda q, body: {
            **temporal, "repos": sorted({*temporal["repos"], body["repo"]})}
        code, out, err = self.run_cli("endpoints", "grant", "temporal", "Other/App")
        self.assertEqual((code, out), (0, "temporal: for other/app, owner/app\n"))
        self.assertEqual(self.control.calls[-1][:4], ("POST", "/api/endpoints/temporal/repos", {}, {"repo": "other/app"}))
        self.routes[("DELETE", "/api/endpoints/temporal/repos/owner/app")] = lambda q, body: {**temporal, "repos": []}
        code, out, err = self.run_cli("endpoints", "revoke", "temporal", "owner/app")
        self.assertEqual((code, out), (0, "temporal: for no repo\n"))
        self.routes[("DELETE", "/api/endpoints/temporal/repos/owner/app")] = lambda q, body: (
            404, {"detail": "owner/app is not granted the endpoint temporal"})
        code, _, _ = self.run_cli("endpoints", "revoke", "temporal", "owner/app")
        self.assertEqual(code, "sbx-control: owner/app is not granted the endpoint temporal")
        self.routes[("POST", "/api/endpoints/dev-pg/repos")] = lambda q, body: (400, {"detail": "attach the server to the repo instead"})
        code, _, _ = self.run_cli("endpoints", "grant", "dev-pg", "owner/app")
        self.assertEqual(code, "sbx-control: attach the server to the repo instead")
        self.routes[("POST", "/api/endpoints")] = lambda q, body: (400, {"detail": "attach the server to the repo instead"})
        code, _, _ = self.run_cli("endpoints", "add", "--server", "dev_pg", "--repo", "owner/app")
        self.assertEqual(code, "sbx-control: attach the server to the repo instead")   # the server's to refuse

    def test_refusals_and_failures_exit_non_zero(self):
        code, _, _ = self.run_cli("endpoints", "add", "--server", "a", "--target", "10.0.0.5:1")
        self.assertIn("--server NAME, or --name NAME --target HOST:PORT", code)
        self.routes[("POST", "/api/endpoints")] = lambda q, body: (409, {"detail": "connect a domain first: ..."})
        code, _, _ = self.run_cli("endpoints", "add", "--server", "dev_pg")
        self.assertEqual(code, "sbx-control: connect a domain first: ...")
        self.routes[("POST", "/api/endpoints")] = lambda q, body: (202, endpoint("applying"))
        self.polls(endpoints(endpoint("failed", message="nginx did not take the endpoints' configuration")))
        code, _, _ = self.run_cli("endpoints", "add", "--server", "dev_pg")
        self.assertEqual(code, "the endpoint dev-pg failed: nginx did not take the endpoints' configuration")
        self.routes[("DELETE", "/api/endpoints/dev-pg")] = lambda q, body: (409, {"detail": "sandboxes of owner/app use it"})
        code, _, _ = self.run_cli("endpoints", "rm", "dev-pg")
        self.assertEqual(code, "sbx-control: sandboxes of owner/app use it")

    def test_rm_waits_until_the_server_applied_it(self):
        self.routes[("DELETE", "/api/endpoints/dev-pg")] = lambda q, body: (202, {"name": "dev-pg", "deleted": True})
        self.polls(endpoints(applying=True), endpoints())
        code, out, err = self.run_cli("endpoints", "rm", "dev-pg")
        self.assertEqual((code, out), (0, "dev-pg: removed\n"))


class SetupCliTests(CliTests):
    """sbx setup: a fresh server set up without a browser, with the setup link (SBX_SETUP_LINK, never argv)
    as the Setup authorization and every secret on stdin."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(self.cli.time, "sleep"))
        self.enterContext(patch.dict(os.environ, {"SBX_SETUP_LINK": f"{self.control.url}/setup#t=one-time-token"}))
        self.state = json.loads(json.dumps(SETUP_STATE))
        self.routes[("GET", "/api/setup")] = lambda q, body: self.state
        self.routes[("GET", "/api/runtimes/available")] = lambda q, body: AVAILABLE

    polls = RuntimeCliTests.polls

    def done(self, step):
        next(s for s in self.state["steps"] if s["step"] == step)["done"] = True

    def test_every_step_goes_with_the_setup_link_and_secrets_on_stdin(self):
        code, out, err = self.run_cli("setup", "status")
        self.assertEqual(code, 0, err)
        self.assertIn("todo  runtime", out)
        self.routes[("POST", "/api/setup/address")] = lambda q, body: (self.done("address"), self.state)[1]
        code, out, err = self.run_cli("setup", "address", "--ip")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.control.calls[-2][3], {"address": "203.0.113.7"})
        self.routes[("POST", "/api/runtimes")] = lambda q, body: (202, change("queued"))
        self.polls(change("done", message="E2B Cloud (e2b-cloud) is enabled"))
        code, out, err = self.run_cli("setup", "runtime", "add", "e2b-cloud", "--secret", "E2B_CLOUD_API_KEY",
                                      stdin=CLOUD_KEY + "\n")
        self.assertEqual((code, out), (0, "E2B Cloud (e2b-cloud) is enabled\n"), err)
        self.routes[("POST", "/api/setup/admin")] = lambda q, body: self.state
        code, out, err = self.run_cli("setup", "admin", "ada@example.com", stdin="test-only-password\n")
        self.assertEqual(code, 0, err)
        self.routes[("POST", "/api/setup/complete")] = lambda q, body: {**self.state, "complete": True}
        code, out, err = self.run_cli("setup", "finish", "--admin", "ada@example.com", stdin="test-only-password\n")
        self.assertEqual(code, 0, err)
        self.assertIn("Next: sbx login", out)
        sent = {(c[0], c[1]): c[3] for c in self.control.calls}
        self.assertEqual(sent[("POST", "/api/setup/admin")], {"email": "ada@example.com", "password": "test-only-password"})
        self.assertEqual(sent[("POST", "/api/setup/complete")], {"email": "ada@example.com", "password": "test-only-password"})
        self.assertEqual(sent[("POST", "/api/runtimes")]["values"]["E2B_CLOUD_API_KEY"], CLOUD_KEY)
        self.assertEqual({c[4] for c in self.control.calls}, {"Setup one-time-token"})   # never a Bearer token
        self.assertNotIn("one-time-token", out + err)

    def test_a_refused_domain_names_each_failed_check_and_its_how_to(self):
        page = "https://github.com/o/sbx/blob/main/docs/howto/cloudflare.md#dns-records"
        self.routes[("PUT", "/api/connections/domain")] = lambda q, body: (400, {"detail": {
            "message": "Check the DNS records and certificate settings below.",
            "checks": [{"name": "*.sbx.example.com resolves", "ok": False, "detail": "no address", "help_url": page},
                       {"name": "delegation", "ok": False, "detail": "no NS record yet", "help_url": ""}]}})
        code, out, err = self.run_cli("setup", "address", "--domain", "sbx.example.com", "--email", "a@example.com",
                                      "--insecure")
        self.assertEqual(code, "sbx-control: Check the DNS records and certificate settings below.; "
                               f"*.sbx.example.com resolves: no address (how to: {page}); delegation: no NS record yet")

    def test_a_domain_for_a_first_test_takes_a_self_signed_certificate(self):
        self.routes[("PUT", "/api/connections/domain")] = lambda q, body: (self.done("address"), {})[1]
        code, out, err = self.run_cli("setup", "address", "--domain", "203-0-113-7.sslip.io", "--dns", "none", "--insecure")
        self.assertEqual(code, 0, err)
        sent = next(c[3] for c in self.control.calls if c[:2] == ("PUT", "/api/connections/domain"))
        self.assertEqual((sent["values"]["name"], sent["values"]["acme"]), ("203-0-113-7.sslip.io", "none"))

    def test_the_link_comes_from_the_environment_or_a_file_never_argv(self):
        with patch.dict(os.environ, {"SBX_SETUP_LINK": ""}):
            code, out, err = self.run_cli("setup", "status")
            self.assertIn("never on the command line", str(code))
            link = self.home / "link"
            link.write_text(f"{self.control.url}/setup#t=from-a-file\n")
            self.assertEqual(self.run_cli("setup", "status", "--link-file", str(link))[0], 0)
        self.assertEqual(self.control.calls[-1][4], "Setup from-a-file")
        for bad in ("https://203.0.113.7/setup", "http://203.0.113.7/setup#t=x", "ftp://127.0.0.1/setup#t=x"):
            with patch.dict(os.environ, {"SBX_SETUP_LINK": bad}):   # http only to this machine
                self.assertIn("not a setup link", str(self.run_cli("setup", "status")[0]), bad)
        self.routes[("GET", "/api/setup")] = lambda q, body: (401, {"detail": "the setup token"})
        self.assertIn("the setup link was refused", str(self.run_cli("setup", "status")[0]))


    def test_a_server_out_of_reach_is_said_not_a_traceback(self):
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        for link, hint in ((f"https://127.0.0.1:{port}/setup#t=x", True), (f"http://localhost:{port}/setup#t=x", True)):
            with patch.dict(os.environ, {"SBX_SETUP_LINK": link}):
                said = str(self.run_cli("setup", "status")[0])
            self.assertIn(f"cannot reach {link.split('/setup')[0]}", said)
            self.assertEqual("add --insecure" in said, hint)
        with patch.dict(os.environ, {"SBX_SETUP_LINK": f"https://127.0.0.1:{port}/setup#t=x"}):
            self.assertNotIn("add --insecure", str(self.run_cli("setup", "status", "--insecure")[0]))


def key_request(state="pending", done=(), **over):
    """A key request as GET /api/secret-requests/ID answers it."""
    items = [{"target": "runtime:boat:BOAT_API_KEY", "label": "Boat API key", "reuse": None},
             {"target": "dns:cloudflare", "label": "Cloudflare DNS token", "reuse": None},
             {"target": "repo:owner/app:BOAT_API_KEY=from:runtime:boat:BOAT_API_KEY", "label": "Boat key for the app",
              "reuse": "runtime:boat:BOAT_API_KEY"}]
    items = [{**i, "done": n in done, "help_url": "", "what": "", "hint": ""} for n, i in enumerate(items)]
    return {"id": "req12345", "url": "https://app.sbx.example.com/keys/req12345", "state": state,
            "expires_at": "2099-01-01T00:00:00Z", "created_by": "ada@example.com", "items": items,
            "remaining": sum(1 for i in items if not i["done"]), **over}


class SecretsCliTests(CliTests):
    """sbx secrets: the agent asks for keys and waits; the owner's keys go on stdin, never argv."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(self.cli.time, "sleep"))
        self.routes[("POST", "/api/secret-requests")] = lambda q, body: (201, key_request())

    def polls(self, *answers):
        found = iter(answers)
        last = answers[-1]
        self.routes[("GET", "/api/secret-requests/req12345")] = lambda q, body: next(found, last)

    def test_request_sends_each_target_with_its_label_and_prints_the_page(self):
        code, out, err = self.run_cli("secrets", "request", "runtime:boat", "Boat API key", "dns:cloudflare",
                                      "Cloudflare DNS token", "--expires", "2h", "--note", "for the first sandbox",
                                      "--help-url", "dns:cloudflare=https://example.com/howto")
        self.assertEqual((code, out), (0, "https://app.sbx.example.com/keys/req12345\n"), err)
        self.assertIn("sbx secrets status req12345 --wait", err)
        method, path, _, body, authorization = self.control.calls[-1]
        self.assertEqual((method, path, authorization), ("POST", "/api/secret-requests", "Bearer sbx_test"))
        self.assertEqual(body, {"items": [{"target": "runtime:boat", "label": "Boat API key"},
                                          {"target": "dns:cloudflare", "label": "Cloudflare DNS token",
                                           "help_url": "https://example.com/howto"}],
                                "expires_in": 7200, "note": "for the first sandbox"})
        code, out, err = self.run_cli("secrets", "request", "runtime:boat", "--json")
        self.assertIn("each target with its label", str(code))
        code, out, err = self.run_cli("secrets", "request", "dns:cloudflare", "x", "--expires", "soon")
        self.assertIn("a duration", str(code))

    def test_request_wait_follows_until_every_key_is_in(self):
        self.polls(key_request(done=(0,)), (502, b"restarting"), key_request("completed", done=(0, 1, 2)))
        code, out, err = self.run_cli("secrets", "request", "--wait", "--json", "runtime:boat", "Boat API key")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["state"], "completed")      # only the final answer on stdout
        self.assertIn("https://app.sbx.example.com/keys/req12345", err)
        self.assertIn("waiting for the owner: 0/3 keys in", err)
        self.assertIn("waiting for the owner: 1/3 keys in", err)

    def test_wait_follows_the_runtime_keys_until_the_server_applied_them(self):
        def applying(state, message=""):
            return key_request("completed", done=(0, 1, 2), runtime_changes={"state": state, "message": message})
        self.polls(applying("queued"), applying("applying"), (502, b"restarting"), applying("restarting"),
                   applying("done", "Boat (boat) is enabled"))
        code, out, err = self.run_cli("secrets", "status", "req12345", "--wait")
        self.assertEqual(code, 0, err)
        self.assertIn("the runtime keys are queued for the server", err)
        self.assertIn("sbx-control is restarting", err)
        self.polls(applying("applying"), applying("failed", "boat was not changed: Boat API: 401 key revoked"))
        code, out, err = self.run_cli("secrets", "request", "--wait", "runtime:boat", "Boat API key")
        self.assertIn("did not apply the runtime keys: boat was not changed: Boat API: 401 key revoked", str(code))
        code, out, err = self.run_cli("secrets", "status", "req12345")   # no --wait: said, and non-zero
        self.assertEqual(code, 1)

    def test_wait_exits_non_zero_when_the_request_expires_or_is_cancelled(self):
        for state in ("expired", "cancelled"):
            with self.subTest(state):
                self.polls(key_request(state))
                code, out, err = self.run_cli("secrets", "status", "req12345", "--wait")
                self.assertNotEqual(code, 0)
                self.polls(key_request(), key_request(state))
                code, out, err = self.run_cli("secrets", "request", "--wait", "dns:cloudflare", "DNS token")
                self.assertIn(f"is {state}", str(code))
        self.polls(key_request(done=(0,)))
        code, out, err = self.run_cli("secrets", "status", "req12345")
        self.assertEqual(code, 0, err)
        self.assertIn("1/3 keys in", out)
        self.assertIn("in  runtime:boat:BOAT_API_KEY", out)

    def test_submit_reads_each_key_from_stdin_and_fails_when_one_is_refused(self):
        self.polls(key_request())
        results = {"ok": False, "request": key_request(done=(0, 2)), "results": [
            {"target": "runtime:boat:BOAT_API_KEY", "ok": True, "error": ""},
            {"target": "dns:cloudflare", "ok": False, "error": "Cloudflare refused the token"},
            {"target": "repo:owner/app:BOAT_API_KEY=from:runtime:boat:BOAT_API_KEY", "ok": True, "error": ""}]}
        self.routes[("POST", "/api/secret-requests/req12345/submit")] = lambda q, body: results
        code, out, err = self.run_cli("secrets", "submit", "req12345", stdin="test-only-boat\ntest-only-dns\n")
        self.assertEqual(code, 1)
        self.assertEqual(self.control.calls[-1][3], {"values": {"runtime:boat:BOAT_API_KEY": "test-only-boat",
                                                                "dns:cloudflare": "test-only-dns"}})
        self.assertIn("NO  dns:cloudflare: Cloudflare refused the token", out)
        self.assertNotIn("test-only", out + err)
        code, out, err = self.run_cli("secrets", "submit", "req12345", "dns:cloudflare", stdin="")
        self.assertIn("no value for dns:cloudflare", str(code))

    def test_ls_and_cancel(self):
        self.routes[("GET", "/api/secret-requests")] = lambda q, body: {"requests": [key_request()]}
        self.routes[("DELETE", "/api/secret-requests/req12345")] = lambda q, body: key_request("cancelled")
        code, out, err = self.run_cli("secrets", "ls")
        self.assertIn("req12345  pending   0/3 in", out)
        code, out, err = self.run_cli("secrets", "cancel", "req12345", "--json")
        self.assertEqual((code, json.loads(out)["state"]), (0, "cancelled"))
        self.routes[("DELETE", "/api/secret-requests/req12345")] = lambda q, body: (409, {"detail": "the request is completed already"})
        self.assertIn("completed already", str(self.run_cli("secrets", "cancel", "req12345")[0]))

    def test_a_setup_link_stands_in_for_the_session_while_the_server_is_set_up(self):
        with patch.dict(os.environ, {"SBX_SETUP_LINK": f"{self.control.url}/setup#t=one-time-token"}):
            code, out, err = self.run_cli("secrets", "request", "dns:cloudflare", "DNS token")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.control.calls[-1][4], "Setup one-time-token")


class CliApiTests(Server):
    def test_scaffold_names_the_files_it_reads_then_drafts_the_recipe(self):
        self.repo_admin()
        paths = ["package.json", "package-lock.json", "src/main.ts", "apps/web/vite.config.ts",
                 ".env.example", "node_modules/x/.env.example"]
        first = self.client.post("/api/repos/owner/app/scaffold", json={"paths": paths}).json()
        self.assertEqual(first, {"read": [".env.example", "apps/web/vite.config.ts", "package.json"]})
        files = {"package.json": json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "5"}}),
                 "apps/web/vite.config.ts": "server: { port: 4100 }", ".env.example": "API_KEY=\n# x\nexport DB_URL=x\n"}
        r = self.client.post("/api/repos/owner/app/scaffold", json={"paths": paths, "files": files}).json()
        recipe = yaml.safe_load(r["files"][".sbx/sbx.yaml"])
        self.assertEqual(recipe["processes"]["web"], "npm run dev -- --host 0.0.0.0")
        self.assertEqual(recipe["previews"][""], 4100)
        self.assertEqual(recipe["secrets"], ["API_KEY", "DB_URL"])
        self.assertNotIn("template", recipe)
        self.assertEqual(r["found"]["secrets"], ["API_KEY", "DB_URL"])
        self.assertEqual(r["executable"], [".sbx/setup"])

    def test_a_scaffold_names_a_port_only_where_the_checkout_shows_one(self):
        from sbx_control import repo_onboarding
        go = repo_onboarding.detect({"go.mod", "go.sum", "main.go"}, {})
        self.assertIsNone(go["port"])
        recipe = repo_onboarding.scaffold("owner/app", go)["files"][".sbx/sbx.yaml"]
        self.assertNotIn("3000", recipe)
        self.assertEqual(yaml.safe_load(recipe)["previews"], {})
        from sbx_control import recipes   # the draft applies as it is
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "sbx.yaml").write_text(recipe)
            recipes.validate("owner/app", Path(directory))
        express = repo_onboarding.detect({"package.json"}, {"package.json": json.dumps(
            {"scripts": {"dev": "node server.js"}, "dependencies": {"express": "4"}})})
        recipe = yaml.safe_load(repo_onboarding.scaffold("owner/app", express)["files"][".sbx/sbx.yaml"])
        self.assertEqual(recipe["processes"]["web"], "npm run dev")
        self.assertNotIn("health", recipe)
        self.assertNotIn("env", recipe)
        nextjs = repo_onboarding.detect({"package.json"}, {"package.json": json.dumps(
            {"scripts": {"dev": "next dev"}, "dependencies": {"next": "15"}})})
        recipe = yaml.safe_load(repo_onboarding.scaffold("owner/app", nextjs)["files"][".sbx/sbx.yaml"])
        self.assertEqual((recipe["previews"][""], recipe["env"]["PORT"]), (3000, "3000"))

    def test_the_cli_writes_the_recipe_the_server_drafts_for_its_checkout(self):
        self.repo_admin()
        with tempfile.TemporaryDirectory() as directory:
            cli = load_cli(Path(directory))
            checkout = Path(directory) / "app"
            checkout.mkdir()
            (checkout / "manage.py").write_text("")
            (checkout / "uv.lock").write_text("")
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)

            def api(method, path, body=None, raw=False, timeout=600):
                response = self.client.request(method, path, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()
            with patch.object(cli, "api", api):
                r = cli.scaffold(str(checkout), "owner/app", prebuild=False)
            recipe = yaml.safe_load((checkout / ".sbx/sbx.yaml").read_text())
            self.assertEqual(recipe["processes"]["web"], "python manage.py runserver 0.0.0.0:8000")
            setup = checkout / ".sbx/setup"
            self.assertIn("uv sync", setup.read_text())
            self.assertTrue(os.access(setup, os.X_OK))
            self.assertEqual(r["found"]["port"], 8000)

    def test_a_prebuilt_setup_reinstalls_only_when_the_lockfile_changed(self):
        from sbx_control import repo_onboarding
        found = {"install": "npm ci", "lockfile": "package-lock.json", "pm": "npm", "dev": "npm run dev",
                 "port": 3000, "previews": {}, "secrets": [], "tools": []}
        drafted = repo_onboarding.scaffold("owner/app", found, prebuild=True)
        self.assertIs(yaml.safe_load(drafted["files"][".sbx/sbx.yaml"])["template"], True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            (root / "bin/npm").write_text(f"#!/bin/sh\ntouch {root}/installed\n")
            (root / "bin/npm").chmod(0o755)
            (root / "setup").write_text(drafted["files"][".sbx/setup"])
            env = {**os.environ, "HOME": str(root), "PATH": f"{root}/bin:{os.environ['PATH']}",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}

            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], env=env, check=True,
                                      capture_output=True, text=True).stdout.strip()

            def setup():
                (root / "installed").unlink(missing_ok=True)
                subprocess.run(["bash", str(root / "setup")], cwd=root, env=env, check=True)
                return (root / "installed").exists()
            git("init", "-q")
            (root / "package-lock.json").write_text("1")
            git("add", "package-lock.json")
            git("commit", "-qm", "one")
            self.assertTrue(setup(), "no template commit: install")
            (root / ".template-commit").write_text(git("rev-parse", "HEAD"))
            self.assertFalse(setup(), "the template's lockfile: no install")
            (root / "package-lock.json").write_text("2")
            git("commit", "-qam", "two")
            self.assertTrue(setup(), "a changed lockfile: install")

    def test_recipe_apply_trusts_the_repo_and_keeps_file_modes(self):
        from sbx_control import recipes, settings
        self.repo_admin()
        body = {"files": {".sbx/sbx.yaml": "previews: {web: 3000}\n", ".sbx/tool": "#!/bin/sh\n"},
                "executable": [".sbx/tool"]}
        response = self.client.put("/api/repos/Owner/App/recipe", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"repo": "owner/app", "configured": True, "files": 2, "prebuilt": False})
        self.assertEqual(settings.trust("owner/app"), "trusted")
        a = recipes.load("owner/app")
        self.assertEqual(a.spec["previews"]["web"], 3000)
        self.assertTrue(os.access(a.dir / "tool", os.X_OK))
        bad = self.client.put("/api/repos/owner/app/recipe", json={"files": {".sbx/sbx.yaml": "services: []\n"}})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(recipes.load("owner/app").spec["previews"]["web"], 3000)

    def test_an_admin_reads_the_setup_brief_before_the_repo_is_served(self):
        from sbx_control import auth, settings
        self.repo_admin()
        self.assertIsNone(settings.trust("owner/new"))
        response = self.client.get("/api/repos/owner/new/setup-brief")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("# Set up owner/new", response.text)
        self.app.app.dependency_overrides[auth.api_user] = lambda: "someone@example.test"
        self.addCleanup(self.app.app.dependency_overrides.clear)
        refused = self.client.get("/api/repos/owner/new/setup-brief")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("not served here", refused.text)

    def test_an_applied_recipe_is_removed_once_its_sandboxes_are_gone(self):
        from types import SimpleNamespace
        from sbx_control import core, recipes, settings, template_builds
        self.repo_admin()
        body = {"files": {".sbx/sbx.yaml": "template: yes\n", ".sbx/build": "#!/bin/sh\n"}, "executable": [".sbx/build"]}
        self.assertEqual(self.client.put("/api/repos/owner/app/recipe", json=body).status_code, 200)
        running = SimpleNamespace(sandbox_id="vm-1", metadata={"sbx": "try-a", "repo": "owner/app"})
        with patch.object(core, "sandboxes", return_value=[running]):
            refused = self.client.delete("/api/repos/owner/app/recipe")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIn("try-a", refused.text)
        self.assertTrue(recipes.load("owner/app").has_recipe)
        removed_template = {"key": "owner/app", "deleted": ["owner-app-aaaaaaaa"], "kept_in_use": [], "rebuilds": False}
        with patch.object(core, "sandboxes", return_value=[]), \
                patch.object(template_builds, "remove", return_value=removed_template) as remove:
            response = self.client.delete("/api/repos/owner/app/recipe")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"repo": "owner/app", "removed": True, "recipe": "none",
                                           "template": removed_template})
        remove.assert_called_once_with("owner/app")
        self.assertFalse(recipes.load("owner/app").has_recipe)
        self.assertFalse(recipes.operator_dir("owner/app").parent.exists())
        self.assertEqual(settings.trust("owner/app"), "trusted")   # still served: it needs a recipe now
        with patch.object(core, "sandboxes", return_value=[]):
            self.assertEqual(self.client.delete("/api/repos/owner/app/recipe").status_code, 404)

    def test_removing_a_template_while_it_builds_is_refused(self):
        from sbx_control import template_builds
        self.repo_admin()
        with patch.object(template_builds, "remove", side_effect=template_builds.Busy("owner/app's template is building")):
            self.assertEqual(self.client.delete("/api/repos/owner/app/template").status_code, 409)
        with patch.object(template_builds, "remove", side_effect=RuntimeError("the runtime did not list its templates")):
            self.assertEqual(self.client.delete("/api/repos/owner/app/template").status_code, 503)

    def test_a_build_without_room_is_refused_with_what_is_missing(self):
        from sbx_control import template_builds
        self.repo_admin()
        self.assertEqual(self.client.put("/api/repos/owner/app/recipe", json={"files": {".sbx/sbx.yaml": "template: yes\n"}}).status_code, 200)
        with patch.object(template_builds, "start", side_effect=template_builds.NoRoom("needs 16.0 GB of free disk")):
            response = self.client.post("/api/repos/owner/app/template")
        self.assertEqual((response.status_code, response.json()["detail"]), (503, "needs 16.0 GB of free disk"))

    def test_sbx_admin_mints_a_token_shown_once_lists_it_without_its_value_and_revokes_it(self):
        import contextlib
        import io
        from sbx_control import admin
        self.repo_admin()
        self.client.cookies.clear()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(admin.main(["token", "create", "CI@Example.com", "--label", "ci"]), 0)
        token = out.getvalue().strip()
        self.assertTrue(token.startswith("sbx_"))
        self.assertIn("shown once", err.getvalue())
        me = self.client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me.status_code, 200, me.text)
        self.assertIn("ci@example.com", me.text)
        listed = io.StringIO()
        with contextlib.redirect_stdout(listed):
            self.assertEqual(admin.main(["token", "ls"]), 0)
        self.assertNotIn(token, listed.getvalue())
        line = next(line for line in listed.getvalue().splitlines() if "ci@example.com" in line)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(admin.main(["token", "revoke", line.split()[0]]), 0)
        self.assertEqual(self.client.get("/api/sandboxes", headers={"Authorization": f"Bearer {token}"}).status_code, 401)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(admin.main(["token", "revoke", line.split()[0]]), 1)

    def test_a_recipe_with_an_unknown_service_key_is_refused_naming_connections(self):
        self.repo_admin()
        recipe = "services:\n  db:\n    uses: postgres-clone\n    servers: {server: dev-pg}\n"
        response = self.client.put("/api/repos/owner/app/recipe", json={"files": {".sbx/sbx.yaml": recipe}})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("servers", response.text)
        self.assertIn("connections:", response.text)

    def test_a_repo_is_one_repo_whatever_its_case(self):
        from sbx_control import sandboxes, settings
        self.assertEqual(sandboxes.CreateSandbox(name="x", public_keys="", repo="Owner/App").repo, "owner/app")
        settings.set_repos(["Owner/App", "Other/*"], by="test")
        self.assertEqual(settings.current().repos, ("owner/app", "other/*"))
        settings.add_repo("Third/Repo", by="test")
        settings.forget()
        self.assertEqual(settings.current().repos, ("owner/app", "other/*", "third/repo"))
        self.assertEqual(settings.current().default_repo, "owner/app")

    def test_cli_sign_in_hands_over_a_one_time_code_not_the_token(self):
        import base64
        self.repo_admin()   # signed in with the session cookie
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        response = self.client.get("/cli/login", params={"port": 4321, "state": "s1", "challenge": challenge},
                                   follow_redirects=False)
        location = urlparse(response.headers["location"])
        self.assertEqual((location.hostname, location.port), ("127.0.0.1", 4321))
        query = parse_qs(location.query)
        self.assertEqual(query["state"], ["s1"])
        self.assertNotIn("token", query)
        self.assertNotIn("sbx_", response.headers["location"])
        code = query["code"][0]
        self.assertEqual(self.client.post("/cli/token", json={"code": code, "verifier": "wrong"}).status_code, 400)
        # a wrong verifier spends the code: it is good once
        response = self.client.get("/cli/login", params={"port": 4321, "state": "s1", "challenge": challenge},
                                   follow_redirects=False)
        code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
        token = self.client.post("/cli/token", json={"code": code, "verifier": verifier})
        self.assertEqual(token.status_code, 200, token.text)
        self.assertEqual(token.headers["cache-control"], "no-store")
        me = self.client.get("/api/me", headers={"Authorization": f"Bearer {token.json()['token']}"})
        self.assertEqual(me.json()["email"], "operator@example.com")
        self.assertEqual(self.client.post("/cli/token", json={"code": code, "verifier": verifier}).status_code, 400)

    def test_the_cli_tarball_is_served_with_its_digest(self):
        from sbx_control import dist_api
        self.repo_admin()
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "sbx.tgz").write_bytes(b"release")
            with patch.object(dist_api, "DIST", Path(directory)):  # where host-install packs the release
                tarball = self.client.get("/dist/sbx.tgz").content
                self.assertEqual(tarball, b"release")
                self.assertEqual(self.client.get("/dist/sbx.tgz.sha256").text, hashlib.sha256(tarball).hexdigest())


class SandboxViewTests(Server):
    """GET and exec on /api/sandboxes/NAME, with the E2B runtime faked."""

    def setUp(self):
        super().setUp()
        from sbx_control import auth, core, lifecycle, state
        self.core, self.lifecycle = core, lifecycle
        with state.db() as conn:
            conn.execute("insert into sandboxes (name, owner, branch, slug) values ('try', 'me@example.test', 'main', 'try')")
        self.app.app.dependency_overrides[auth.api_user] = lambda: "me@example.test"
        self.addCleanup(self.app.app.dependency_overrides.clear)
        self.enterContext(patch("sbx_control.setup.complete", return_value=True))
        # paused: the server does not look into the VM for listening ports
        self.info = SimpleNamespace(sandbox_id="vm1", state="paused",
                                    metadata={"sbx": "try", "owner": "me@example.test", "repo": "owner/app"})
        self.enterContext(patch.object(core, "find", return_value=self.info))

    def test_the_view_carries_every_field_the_cli_reads(self):
        response = self.client.get("/api/sandboxes/try")
        self.assertEqual(response.status_code, 200, response.text)
        view = response.json()
        self.assertLessEqual(set(SANDBOX), set(view))
        self.assertNotIn("e2b_host", view)

    def test_me_names_each_agents_login_and_who_values_go_to(self):
        me = self.client.get("/api/me").json()
        self.assertEqual(set(me["agents"]), {"claude", "codex"})
        self.assertTrue(all(a["path"].startswith("/") and a["get"] and (a["env"] or a["file"]) for a in me["agents"].values()))
        self.assertEqual(set(me["audiences"]), {"all", "previews", "sessions"})

    def test_a_wait_returns_when_the_operation_moves_on(self):
        op = self.lifecycle.begin("try", "create")
        op.advance("allocating")
        threading.Timer(0.7, lambda: op.advance("preparing_workspace")).start()
        started = time.monotonic()
        response = self.client.get("/api/sandboxes/try", params={"wait": 10})
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(response.json()["lifecycle"]["phase"], "preparing_workspace")

    def test_exec_runs_as_the_owner_and_reports_the_exit_code(self):
        from sbx_control import runtime
        ran = []

        class Commands:
            def run(self, command, timeout=None, **_):
                ran.append((command, timeout))
                raise runtime.CommandFailed(stdout="partial\n", stderr="boom\n", exit_code=2)
        vm = self.core.Attached("try", SimpleNamespace(commands=Commands()), None)
        with patch.object(self.core, "attach", return_value=vm):
            response = self.client.post("/api/sandboxes/try/exec", json={"script": "false", "timeout": 30})
        self.assertEqual(response.json(), {"exit_code": 2, "stdout": "partial\n", "stderr": "boom\n"})
        [(command, timeout)] = ran
        self.assertIn("false", command)
        self.assertEqual(timeout, 30)
        self.app.app.dependency_overrides[self.app.auth.api_user] = lambda: "other@example.test"
        with patch.object(self.core, "attach", return_value=vm) as attach:
            self.assertEqual(self.client.post("/api/sandboxes/try/exec", json={"script": "id"}).status_code, 404)
        attach.assert_not_called()
        self.assertEqual(len(ran), 1)


if __name__ == "__main__":
    unittest.main()
