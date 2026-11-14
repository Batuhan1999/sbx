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
        from e2b.sandbox.commands.command_handle import CommandExitException
        ran = []

        class Commands:
            def run(self, command, timeout=None, **_):
                ran.append((command, timeout))
                raise CommandExitException(stdout="partial\n", stderr="boom\n", exit_code=2, error=None)
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
