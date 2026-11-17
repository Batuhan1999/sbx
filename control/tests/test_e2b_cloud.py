"""E2B Cloud next to Embed: the sandbox's traffic token through the gate
and nginx (to its own runtime only), `sbx-admin runtime add|remove|default`,
install.sh's runtime.env writer, and the git credential helper's
certificate pin. The SDK itself is mocked (test_runtime.py covers the
backend); a stand-in runtime with a token header plays a provider here."""
import io
from unittest.mock import patch

from support import RUNTIME, Installation, Server
from test_runtimes import TwoRuntimes

TOKEN_HEADER = "e2b-traffic-access-token"


def with_token(fake):
    """The fake as a runtime that answers its sandboxes' ports only with their token."""
    from sbx_control.runtime import TokenPlace
    fake.upstream_token_at = TokenPlace("header", TOKEN_HEADER)
    fake.kept = set()       # the sandboxes whose token it holds (else: fetched, which may wake one)
    fake.fetched = []

    def upstream_token(sandbox_id, port=0, *, fetch=True):
        if sandbox_id not in fake.kept:
            if not fetch:
                return ""
            fake.fetched.append(sandbox_id)
            fake.kept.add(sandbox_id)
        return f"test-token-{sandbox_id}"
    fake.upstream_token = upstream_token
    fake.ingress = "https://sbx-ingress.provider.example"
    return fake


class TrafficToken(TwoRuntimes, Server):
    def setUp(self):
        super().setUp()
        from pathlib import Path
        from sbx_control import core, gate, recipes, settings
        with_token(self.fake)
        self.repo_admin()
        settings.update("domain", {**settings.current().domain.model_dump(), "name": "example.test"}, by="test")
        self.client.cookies.clear()
        app = recipes.App("owner/app", Path("/unused"), {"previews": {"": {"port": 3000, "access": "team"}}})
        self.enterContext(patch.object(core, "app_of", return_value=app))
        self.enterContext(patch.object(core, "attach"))
        meta = {"slug": "x", "owner": "operator@example.com", "repo": "owner/app"}
        self.at_provider = self.runtime.create("tpl", timeout=600, kind="fake", metadata={**meta, "sbx": "p"}).sandbox_id
        self.on_embed = self.runtime.create("tpl", timeout=600, metadata={**meta, "sbx": "e"}).sandbox_id
        gate.sandbox_owner(self.at_provider, fresh=True)

    def ask(self, sandbox_id, method="OPTIONS"):
        return self.client.get("/gate", headers={"X-Original-Host": f"3000-{sandbox_id}.example.test",
                                                 "X-Original-Method": method})

    def test_the_gate_hands_nginx_the_token_of_a_provider_sandbox_only(self):
        from sbx_control import gate
        found = gate.route(3000, self.at_provider)
        self.assertEqual(found[gate.TOKEN], f"test-token-{self.at_provider}")
        self.assertNotIn(gate.TOKEN, gate.route(3000, self.on_embed))   # Embed wants none

    def test_a_preflight_wakes_nothing_and_without_a_kept_token_is_refused(self):
        response = self.ask(self.at_provider)
        self.assertEqual((response.status_code, response.headers.get("x-sbx-gone")), (403, "1"))   # nginx: 404
        self.assertEqual(self.fake.fetched, [])        # no connect: it would resume the sandbox
        from sbx_control import gate
        found = gate.route(3000, self.at_provider)     # the browser's real request fetches (and keeps) it
        self.assertEqual((found[gate.TOKEN], self.fake.fetched), (f"test-token-{self.at_provider}", [self.at_provider]))
        self.assertEqual(self.ask(self.at_provider).status_code, 200)   # then preflights pass with it

    def test_a_sandbox_the_runtime_no_longer_has_is_not_found(self):
        from fastapi import HTTPException
        from sbx_control import gate
        with patch.object(self.fake, "upstream_token", side_effect=self.runtime.NotRunning("gone")), \
                self.assertRaises(HTTPException) as raised:
            gate.route(3000, self.at_provider)
        self.assertEqual(raised.exception.status_code, 404)

    def test_a_runtime_that_hands_over_no_token_is_refused_not_a_500(self):
        with patch.object(self.fake, "upstream_token", side_effect=self.runtime.Failed("E2B did not answer")):
            response = self.client.get("/gate", headers={"X-Original-Host": f"3000-{self.at_provider}.example.test",
                                                         "X-Original-Method": "GET",
                                                         "Authorization": f"Bearer {self.token_for_admin()}"})
        # Not a 500, and not a bare error either: nginx shows the starting page until a token comes.
        self.assertEqual((response.status_code, response.headers.get("x-sbx-page")), (403, "starting"))

    def token_for_admin(self):
        from sbx_control import auth
        return auth.mint_token("operator@example.com", "test")

    def test_a_cors_preflight_gets_the_token_too(self):
        self.fake.kept.add(self.at_provider)          # a browser's real request fetched and kept it
        response = self.ask(self.at_provider)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-sbx-upstream-token"], f"test-token-{self.at_provider}")
        self.assertEqual(response.headers["x-sbx-runtime"], "fake")
        response = self.ask(self.on_embed)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("x-sbx-upstream-token", response.headers)

    def test_nginx_sends_the_token_to_its_runtime_alone(self):
        from sbx_control import ip_previews, preview_proxy
        maps = preview_proxy.runtime_upstreams()
        self.assertIn('map $sbx_runtime $sbx_ingress_token_1 {\n  default "";\n  fake $sbx_ingress_token;\n}', maps)
        self.assertNotIn("embed $sbx_ingress_token", maps)   # an Embed sandbox's request never carries it
        lines = preview_proxy.upstream_token()
        self.assertIn("auth_request_set $sbx_ingress_token $upstream_http_x_sbx_upstream_token;", lines)
        self.assertIn(f"proxy_set_header {TOKEN_HEADER} $sbx_ingress_token_1;", lines)
        config = ip_previews.nginx("203.0.113.10", "bootstrap")
        self.assertIn('map $ip_runtime $ip_ingress_token_1 {\n  default "";\n  fake $ip_ingress_token;\n}', config)
        self.assertIn(f"proxy_set_header {TOKEN_HEADER} $ip_ingress_token_1;", config)
        self.assertIn("proxy_ssl_name $ip_upstream;", config)
        self.fake.upstream_token_at = None
        self.assertEqual(preview_proxy.upstream_token(), "")
        self.assertNotIn("_token", preview_proxy.runtime_upstreams())

    def test_the_public_never_reaches_the_gate_itself(self):
        script = (__import__("pathlib").Path(__file__).parents[1] / "tls.sh").read_text()
        control = script[script.index('cat > "$dir/nginx/control.inc"'):script.index("install_renewal_timer() {")]
        self.assertIn("location = /gate { return 404; }", control)

    def test_the_gate_cache_never_stores_a_token(self):
        script = (__import__("pathlib").Path(__file__).parents[1] / "tls.sh").read_text()
        gate_inc = script[script.index('cat > "$dir/nginx/gate.inc"'):]
        gate_inc = gate_inc[:gate_inc.index("\nNGINX\n")]
        self.assertIn("proxy_cache gate;", gate_inc)
        self.assertIn(r"proxy_no_cache \$upstream_http_x_sbx_upstream_token \$upstream_http_x_sbx_wait;", gate_inc)


class RuntimeAdmin(Installation):
    """sbx-admin runtime add | remove | default on an Embed installation."""
    RUNTIME_ENV = RUNTIME

    def setUp(self):
        super().setUp()
        from sbx_control import runtime
        from sbx_control.connections.base import Check
        from sbx_control.runtime.e2b_cloud import Cloud
        self.runtime, self.file = runtime, self.root / "etc/runtime.env"
        self.addCleanup(self.file.write_text, RUNTIME)
        self.enterContext(patch.dict(runtime._backends, clear=True))
        self.checks = [Check("E2B Cloud API", True, "answers")]
        self.enterContext(patch.object(Cloud, "test", side_effect=lambda: self.checks))
        self.listed = []
        self.enterContext(patch.object(Cloud, "sandboxes", side_effect=lambda *_: self.listed))
        from sbx_control import template_scheduler
        self.real_start_detached = template_scheduler.start_detached   # never run for real: it starts builds
        self.builds = self.enterContext(patch.object(template_scheduler, "start_detached",
                                                     return_value=("sbx-template-builds-e2b-cloud.service", True)))
        self.effects()

    def test_add_starts_the_new_runtime_s_base_and_default_templates_now(self):
        code, said = self.admin("add", "e2b-cloud", stdin="test-only-cloud-key")
        self.assertEqual(code, 0, said)
        self.builds.assert_called_once_with("e2b-cloud")
        self.assertIn("building sbx-base and default on E2B Cloud now", said)
        self.assertIn("`sbx-admin runtime verify e2b-cloud` works once they're built", said)
        self.builds.reset_mock()
        self.admin("default", "e2b-cloud")
        self.builds.assert_not_called()                                 # only add starts builds

    def test_builds_run_in_a_process_of_their_own(self):
        import subprocess
        from sbx_control import template_scheduler
        self.assertIsNot(self.real_start_detached, template_scheduler.start_detached)   # setUp's stand-in
        done = lambda code: subprocess.CompletedProcess([], code, "", "")   # noqa: E731
        # Not running yet (is-active: 3): systemd-run starts the unit.
        with patch.object(subprocess, "run", side_effect=[done(3), done(0)]) as run, \
                patch.object(subprocess, "Popen") as popen:
            where, started = self.real_start_detached("e2b-cloud")
        self.assertTrue(started)
        self.assertEqual(run.call_args_list[0].args[0][:3], ["systemctl", "is-active", "--quiet"])
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["systemd-run", "--no-block", "--collect", "--unit=sbx-template-builds-e2b-cloud"])
        self.assertIn("-m sbx_control.template_builds base --runtime e2b-cloud --if-stale && ", command[-1])
        self.assertIn("-m sbx_control.template_builds default --runtime e2b-cloud --if-stale", command[-1])
        self.assertIn("sbx-template-builds-e2b-cloud", where)
        popen.assert_not_called()
        # Running already (a second runtime add): no second chain, in parallel or otherwise.
        with patch.object(subprocess, "run", side_effect=[done(0)]) as run, patch.object(subprocess, "Popen") as popen:
            where, started = self.real_start_detached("e2b-cloud")
        self.assertFalse(started)
        self.assertEqual(run.call_count, 1)
        popen.assert_not_called()
        # systemd refused the unit: an error, not a detached chain beside it.
        with patch.object(subprocess, "run", side_effect=[done(3), done(1)]), patch.object(subprocess, "Popen") as popen, \
                self.assertRaises(RuntimeError):
            self.real_start_detached("e2b-cloud")
        popen.assert_not_called()
        # No systemd at all: a detached process.
        with patch.object(subprocess, "run", side_effect=FileNotFoundError("systemctl")), \
                patch.object(subprocess, "Popen") as popen:
            where, started = self.real_start_detached("e2b-cloud")
        self.assertTrue(started)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])    # outlives the command
        self.assertTrue(where.endswith("sbx-template-builds-e2b-cloud.log"))

    def test_add_while_the_builds_run_says_so(self):
        self.builds.return_value = ("sbx-template-builds-e2b-cloud.service", False)
        code, said = self.admin("add", "e2b-cloud", stdin="test-only-cloud-key")
        self.assertEqual(code, 0, said)
        self.assertIn("builds on E2B Cloud are already running", said)

    def admin(self, *argv, stdin=""):
        from sbx_control import admin
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin)), patch("sys.stdout", out), patch("sys.stderr", err):
            code = admin.main(["runtime", *argv])
        return code, out.getvalue() + err.getvalue()

    def test_add_takes_the_key_from_stdin_and_enables_it_after_embed(self):
        code, said = self.admin("add", "e2b-cloud", stdin="test-only-cloud-key\n")
        self.assertEqual(code, 0, said)
        text = self.file.read_text()
        self.assertIn("SBX_RUNTIMES=embed,e2b-cloud", text)
        self.assertIn("E2B_CLOUD_API_KEY=test-only-cloud-key", text)
        self.assertIn("E2B_API_KEY=test-only", text)                  # Embed's keys stay
        self.assertNotIn("test-only-cloud-key", said)                   # never echoed
        self.assertEqual(self.runtime.enabled(), ["embed", "e2b-cloud"])
        self.assertEqual(self.runtime.default(), "embed")
        self.assertEqual(self.effects(), ["control", "tls"])           # what runtime apply does
        self.assertEqual(self.runtime.get("e2b-cloud").connection()["api_key"], "test-only-cloud-key")

    def test_add_refuses_a_key_the_provider_does_not_take_and_changes_nothing(self):
        from sbx_control.connections.base import Check
        self.checks = [Check("E2B Cloud API", False, "HTTP 401")]
        before = self.file.read_text()
        code, said = self.admin("add", "e2b-cloud", stdin="test-only-wrong")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", said)
        self.assertEqual(self.file.read_text(), before)
        self.assertEqual(self.effects(), [])
        self.assertEqual(self.admin("add", "e2b-cloud")[0], 1)          # no key on stdin
        code, said = self.admin("add", "embed", stdin="x")
        self.assertIn("install.sh", said)                               # the host's runtime is the installer's
        self.assertEqual(self.admin("add", "nowhere", stdin="x")[0], 1)

    def test_add_seals_the_key_once_the_store_is_infisical(self):
        from sbx_control import secret_store, vault
        with patch.object(vault, "sealing", return_value=True), \
                patch.object(secret_store, "seal", return_value="sealed-reference") as seal:
            code, said = self.admin("add", "e2b-cloud", stdin="test-only-cloud-key")
        self.assertEqual(code, 0, said)
        self.assertTrue(seal.call_args.kwargs["key"].startswith("runtime/E2B_CLOUD_API_KEY/"))
        self.assertIn("E2B_CLOUD_API_KEY=sealed-reference", self.file.read_text())
        self.assertNotIn("test-only-cloud-key", self.file.read_text())

    def sealed(self):
        """The store as references: seal makes a new one each time, unseal reads it, discard drops it."""
        from sbx_control import secret_store, vault
        store, made = {}, iter(range(1, 100))

        def seal(value, *, key):
            reference = f"infisical-ref-{next(made)}"
            store[reference] = value
            return reference
        for patcher in (patch.object(vault, "sealing", return_value=True),
                        patch.object(secret_store, "seal", side_effect=seal),
                        patch.object(secret_store, "is_reference", side_effect=lambda v: str(v).startswith("infisical-ref-")),
                        patch.object(secret_store, "unseal", side_effect=lambda r: store[r]),
                        patch.object(secret_store, "discard", side_effect=lambda r: store.pop(r, None))):
            self.enterContext(patcher)
        return store

    def test_a_new_key_replaces_the_old_reference_only_once_confirmed(self):
        from sbx_control import platform
        store = self.sealed()
        self.assertEqual(self.admin("add", "e2b-cloud", stdin="test-only-first")[0], 0)
        self.assertEqual(list(store.values()), ["test-only-first"])
        first = next(iter(store))
        # The new configuration fails to load: the file and the old reference stay, the new one goes.
        before = self.file.read_text()
        real = platform.connect
        calls = []

        def connect():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("the store did not answer")
            return real()
        with patch.object(platform, "connect", side_effect=connect):
            code, said = self.admin("add", "e2b-cloud", stdin="test-only-second")
        self.assertEqual(code, 1)
        self.assertIn("the store did not answer", said)
        self.assertEqual(self.file.read_text(), before)
        self.assertEqual(store, {first: "test-only-first"})
        # Confirmed: the new reference in the file, the old one discarded only now.
        self.assertEqual(self.admin("add", "e2b-cloud", stdin="test-only-second")[0], 0)
        self.assertEqual(list(store.values()), ["test-only-second"])
        self.assertNotIn(first, store)
        self.assertIn(f"E2B_CLOUD_API_KEY={next(iter(store))}", self.file.read_text())
        # remove: the reference stays until the configuration without it loads.
        with patch.object(platform, "connect", side_effect=[SystemExit("nope"), None]):   # the check, then put back
            self.assertEqual(self.admin("remove", "e2b-cloud")[0], 1)
        self.assertEqual(list(store.values()), ["test-only-second"])
        self.assertEqual(self.admin("remove", "e2b-cloud")[0], 0)
        self.assertEqual(store, {})

    def test_default_and_remove(self):
        self.assertEqual(self.admin("add", "e2b-cloud", stdin="test-only-cloud-key")[0], 0)
        code, said = self.admin("default", "e2b-cloud")
        self.assertEqual(code, 0, said)
        self.assertEqual(self.runtime.default(), "e2b-cloud")
        self.assertEqual(self.admin("default", "nowhere")[0], 1)
        self.listed = [self.runtime.Info("vm1", {"sbx": "busy"}, "running")]
        code, said = self.admin("remove", "e2b-cloud")
        self.assertEqual(code, 1)
        self.assertIn("busy", said)                                     # refused while it has sandboxes
        self.assertIn("e2b-cloud", self.runtime.enabled())
        self.listed = []
        code, said = self.admin("remove", "e2b-cloud")
        self.assertEqual(code, 0, said)
        text = self.file.read_text()
        self.assertIn("SBX_RUNTIMES=embed", text)
        self.assertNotIn("E2B_CLOUD", text)                             # its key and the default naming it go
        self.assertNotIn("SBX_RUNTIME_DEFAULT", text)
        self.assertEqual(self.runtime.default(), "embed")
        code, said = self.admin("remove", "embed")
        self.assertEqual(code, 1)
        self.assertIn("only runtime", said)


class InstallerEnv(Installation):
    """install.sh's runtime.env writer (python -m sbx_control.runtime env):
    values from the environment, kinds added to SBX_RUNTIMES, the rest kept."""
    RUNTIME_ENV = RUNTIME

    def setUp(self):
        super().setUp()
        self.file = self.root / "etc/runtime.env"
        self.addCleanup(self.file.write_text, RUNTIME)

    def write(self, names, kinds, environ):
        from sbx_control.runtime.__main__ import write_env
        return write_env(names, kinds, environ)

    def test_a_fresh_install_without_kvm_enables_e2b_cloud_alone(self):
        self.file.unlink()
        self.assertEqual(self.write(["E2B_CLOUD_API_KEY"], ["e2b-cloud"], {"E2B_CLOUD_API_KEY": "test-only"}),
                         ["e2b-cloud"])
        text = self.file.read_text()
        self.assertIn("SBX_RUNTIMES=e2b-cloud\n", text)
        self.assertIn("E2B_CLOUD_API_KEY=test-only\n", text)
        self.assertNotIn("E2B_API_KEY", text)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)

    def test_a_fresh_install_with_both_puts_embed_first(self):
        self.file.unlink()
        self.write(["E2B_API_KEY", "E2B_API_URL"], ["embed"], {"E2B_API_KEY": "k", "E2B_API_URL": "u"})
        self.write(["E2B_CLOUD_API_KEY"], ["e2b-cloud"], {"E2B_CLOUD_API_KEY": "c"})
        self.assertIn("SBX_RUNTIMES=embed,e2b-cloud\n", self.file.read_text())

    def test_an_embed_upgrade_keeps_the_file_as_it_was_but_embed_s_keys(self):
        self.file.write_text("# by hand\nE2B_API_KEY=old\nE2B_HOST=10.0.0.5\nSBX_RUNTIMES=embed,e2b-cloud\n"
                             "SBX_RUNTIME_DEFAULT=e2b-cloud\nE2B_CLOUD_API_KEY=kept\n")
        self.write(["E2B_API_KEY", "E2B_API_URL", "E2B_HOST"], ["embed"], {"E2B_API_KEY": "new", "E2B_API_URL": "u"})
        self.assertEqual(self.file.read_text(), "# by hand\nE2B_API_KEY=new\nSBX_RUNTIMES=embed,e2b-cloud\n"
                                                "SBX_RUNTIME_DEFAULT=e2b-cloud\nE2B_CLOUD_API_KEY=kept\nE2B_API_URL=u\n")

    def test_a_single_embed_install_without_the_selection_stays_without_it(self):
        self.file.write_text(RUNTIME)
        self.write(["E2B_API_KEY"], ["embed"], {"E2B_API_KEY": "test-only"})
        self.assertNotIn("SBX_RUNTIMES", self.file.read_text())
        self.write(["E2B_CLOUD_API_KEY"], ["e2b-cloud"], {"E2B_CLOUD_API_KEY": "c"})
        self.assertIn("SBX_RUNTIMES=embed,e2b-cloud", self.file.read_text())   # missing meant Embed

    def test_the_installer_reads_the_enabled_runtimes_as_sbx_does(self):
        import subprocess
        from pathlib import Path
        from sbx_control import runtime
        script = (Path(__file__).parents[2] / "install.sh").read_text()
        line = next(x for x in script.splitlines() if x.strip().startswith("installed=$(python3"))
        command = (f"src={Path(__file__).parents[2]}; " + line.strip().rstrip("| ").replace("/etc/sbx/runtime.env", '"$1"')
                   + '; printf %s "$installed"')
        for text in ("SBX_RUNTIMES=embed,e2b-cloud\n", "  SBX_RUNTIMES = 'e2b-cloud'\n", "\tSBX_RUNTIMES=e2b-cloud\n"):
            with self.subTest(text=text):
                self.file.write_text(text)
                found = subprocess.run(["sh", "-c", command, "sh", str(self.file)], capture_output=True, text=True,
                                       check=True).stdout.split()
                self.assertEqual(found, runtime.enabled())     # what sbx-control will run

    def test_no_key_in_arguments_and_the_cloud_key_only_where_written(self):
        from pathlib import Path
        script = (Path(__file__).parents[2] / "install.sh").read_text()
        self.assertNotIn("env -u", script)
        unset = script.index("unset E2B_CLOUD_API_KEY")
        self.assertLess(unset, script.index('say "Preflight"'))
        after = script[unset + len("unset E2B_CLOUD_API_KEY"):]
        self.assertNotIn("${E2B_CLOUD_API_KEY", after)
        self.assertEqual(after.count('E2B_CLOUD_API_KEY="$cloud_key" venv/bin/python -m sbx_control.runtime env'), 1)
        self.assertIn("unset cloud_key", after)

    def test_the_writer_takes_values_from_its_environment_as_install_sh_passes_them(self):
        import os
        import subprocess
        import sys
        from pathlib import Path
        control = Path(__file__).parents[1]
        # As install.sh runs it: prefix assignments in a subshell, nothing in argv.
        script = ('cloud_key=test-only-cloud; '
                  '(unset E2B_HOST E2B_DOMAIN && E2B_CLOUD_API_KEY="$cloud_key" "$PY" -m sbx_control.runtime env '
                  '--enable e2b-cloud E2B_CLOUD_API_KEY); unset cloud_key; '
                  '[ -z "${E2B_CLOUD_API_KEY:-}" ] && [ -z "${cloud_key:-}" ]')
        subprocess.run(["sh", "-c", script], check=True, capture_output=True,
                       env={**os.environ, "PY": sys.executable, "PYTHONPATH": str(control)})
        self.assertIn("E2B_CLOUD_API_KEY=test-only-cloud", self.file.read_text())
        self.assertIn("SBX_RUNTIMES=embed,e2b-cloud", self.file.read_text())


class CredentialHelper(Installation):
    def test_the_helper_reads_the_pin_when_it_runs(self):
        import os
        import stat
        import subprocess
        import tempfile
        from pathlib import Path
        from sbx_control import github_app
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        pin_file, bin_dir = folder / "tls-pin", folder / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"   # a stand-in that says what it was asked
        curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
        helper = folder / "credential"
        helper.write_text(github_app.credential_helper("box", "secret-credential", str(pin_file)))
        helper.chmod(0o700)
        run = lambda: subprocess.run([str(helper), "get"], input="protocol=https\nhost=github.com\n\n",   # noqa: E731
                                     capture_output=True, text=True, check=True,
                                     env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}).stdout.split("\n")
        self.assertNotIn("--pinnedpubkey", run())                         # no pin file: a public CA's
        pin_file.write_text("--insecure --pinnedpubkey sha256//first=")
        self.assertIn("sha256//first=", run())
        pin_file.write_text("--insecure --pinnedpubkey sha256//second=")  # the certificate changed
        self.assertIn("sha256//second=", run())                          # the same helper follows it

    def test_the_runtime_layer_carries_the_pin_of_now(self):
        from sbx_control import extensions, github_app, templates
        from sbx_control.connections import github
        with patch.object(github, "connected", return_value=True), \
                patch.object(github_app, "access", return_value="app"), \
                patch.object(templates, "pinned_key", return_value="--insecure --pinnedpubkey sha256//now="):
            layer = github.GitHub().contribution(extensions.Target(repo="owner/app", name="box", trusted=True)).runtime
        self.assertEqual(layer.files["tls-pin"].content, "--insecure --pinnedpubkey sha256//now=")
        self.assertIn(f"cat {github.PIN}", layer.files["credential"].content)

    def test_a_listing_that_missed_a_runtime_delivers_to_what_it_has(self):
        from sbx_control import delivery, templates, tls_pin
        with patch.object(templates, "pinned_key", return_value="--pinnedpubkey sha256//one="):
            # One runtime did not answer: the listed sandboxes get the pin now, once.
            self.assertTrue(tls_pin.redeliver(["a"], complete=False))
            self.assertEqual(delivery.waiting(), {"a"})
            delivery.forget("a")
            self.assertFalse(tls_pin.redeliver(["a"], complete=False))
            self.assertEqual(delivery.waiting(), set())
            # Every runtime answers: the ones missed before, and the pin counts as delivered.
            self.assertTrue(tls_pin.redeliver(["a", "b"], complete=True))
            self.assertEqual(delivery.waiting(), {"b"})
            delivery.forget("b")
            self.assertFalse(tls_pin.redeliver(["a", "b"], complete=True))
        self.assertEqual(delivery.waiting(), set())

    def test_going_back_to_the_pin_before_a_partial_delivery_queues_again(self):
        from sbx_control import delivery, templates, tls_pin
        with patch.object(templates, "pinned_key", return_value="--pinnedpubkey sha256//one="):
            tls_pin.redeliver(["a"], complete=True)   # pin one delivered everywhere
            delivery.forget("a")
        with patch.object(templates, "pinned_key", return_value="--pinnedpubkey sha256//two="):
            # A listing that missed a runtime: "a" gets pin two, the round is incomplete.
            self.assertTrue(tls_pin.redeliver(["a"], complete=False))
            delivery.forget("a")
        with patch.object(templates, "pinned_key", return_value="--pinnedpubkey sha256//one="):
            # Back to pin one: "a" holds two now, so it is queued again.
            self.assertTrue(tls_pin.redeliver(["a"], complete=True))
        self.assertEqual(delivery.waiting(), {"a"})
        delivery.forget("a")

    def test_a_certificate_change_redelivers_every_sandbox_s_runtime_layer(self):
        from sbx_control import delivery, templates, tls_pin
        with patch.object(templates, "pinned_key", return_value=""):
            self.assertFalse(tls_pin.redeliver(["a", "b"]))   # a public CA's: nothing
        self.assertEqual(delivery.waiting(), set())
        with patch.object(templates, "pinned_key", return_value="--pinnedpubkey sha256//one="):
            self.assertTrue(tls_pin.redeliver(["a", "b", None]))
            self.assertEqual(delivery.waiting(), {"a", "b"})
            self.assertEqual(delivery.pending("a")[:2], ([], False))         # no values, no app restart
            for name in ("a", "b"):
                delivery.forget(name)
            self.assertFalse(tls_pin.redeliver(["a", "b"]))   # unchanged: once
        with patch.object(templates, "pinned_key", return_value="--pinnedpubkey sha256//two="):
            self.assertTrue(tls_pin.redeliver(["a"]))
        self.assertEqual(delivery.waiting(), {"a"})


class VerifyBeforeTheBaseIsBuilt(TwoRuntimes, Installation):
    """runtime verify on a runtime whose base template is not built yet
    says so, as its own state, instead of failing on NotBuilt."""

    def verify(self):
        from sbx_control import runtimes
        said = []
        self.assertFalse(runtimes.verify("fake", say=said.append))
        self.assertEqual(self.fake.boxes, {})            # nothing was created
        return "\n".join(said)

    def test_a_base_that_is_building_is_reported_as_building(self):
        import os
        from sbx_control import template_builds, templates
        template_builds._transition(templates.BASE, "building", "fake", started_at=template_builds._utc(),
                                    pid=os.getppid())     # another live process: the build sbx-admin runtime add started
        said = self.verify()
        self.assertIn("the base template is building on Fake runtime; try again when it's built", said)
        self.assertNotIn("NotBuilt", said)
        self.assertTrue(said.endswith("not verified"))

    def test_a_base_never_built_or_failed_says_what_to_do(self):
        from sbx_control import template_builds, templates
        self.assertIn("is not built on Fake runtime yet", self.verify())
        self.assertIn(f"sbx-admin jobs run template@fake:{templates.BASE}", self.verify())
        template_builds._transition(templates.BASE, "building", "fake", pid=None, error="out of disk")   # cut off: failed
        self.assertIn("the base template's build failed on Fake runtime: out of disk", self.verify())


class JobsRunDetached(TwoRuntimes, Installation):
    """sbx-admin jobs run template@KIND:KEY starts the build in a process of its own: the command returns at
    once, and the build goes on without it (it would end with an in-process thread, leaving a building row)."""

    def jobs_run(self, job):
        from sbx_control import admin
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", out):
            code = admin.main(["jobs", "run", job])
        return code, out.getvalue()

    def test_the_build_runs_detached_and_says_where_to_follow_it(self):
        from sbx_control import template_builds, template_scheduler, templates
        unit = "sbx-template-builds-fake.service (journalctl -u sbx-template-builds-fake)"
        with patch.object(template_scheduler, "start_detached", return_value=(unit, True)) as detached, \
                patch.object(template_builds, "start") as in_process, \
                patch.object(template_builds, "base_not_ready", return_value=""):
            code, said = self.jobs_run(f"template@fake:{templates.BASE}")
            self.assertEqual(code, 0, said)
            detached.assert_called_once_with("fake", [["key", templates.BASE]], if_stale=False)
            in_process.assert_not_called()
            self.assertIn(f"building {templates.BASE} on fake in {unit}", said)
            detached.return_value = (unit, False)   # one runs there already: say so, non-zero
            code, said = self.jobs_run(f"template@fake:{templates.BASE}")
            self.assertEqual(code, 1)
            self.assertIn(f"a template build runs on fake already; follow it: {unit}", said)

    def test_the_detached_command_builds_that_key_even_when_current(self):
        import subprocess
        from sbx_control import template_scheduler
        done = lambda code: subprocess.CompletedProcess([], code, "", "")   # noqa: E731
        with patch.object(subprocess, "run", side_effect=[done(3), done(0)]) as run:
            template_scheduler.start_detached("fake", [["key", "owner/app"]], if_stale=False)
        self.assertIn("-m sbx_control.template_builds key owner/app --runtime fake", run.call_args.args[0][-1])
        self.assertNotIn("--if-stale", run.call_args.args[0][-1])
