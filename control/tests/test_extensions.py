"""The extension interfaces: the connector registry, what contributions do to
a template's plan (fingerprint, name, staleness) and to a sandbox's runtime.
The connectors' host units are in test_host.py."""
from pathlib import Path
import sys
import tempfile
import types
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import Installation


def connector_settings(kind, **changes):
    """Change a connector's own settings section."""
    from sbx_control import extensions
    connector = extensions.get(kind)
    connector.save_settings({**connector.settings().model_dump(), **changes}, by="test")


class ExtensionTests(Installation):
    SETTINGS = {"domain": {"address": "203.0.113.10"}}

    def setUp(self):
        super().setUp()
        from sbx_control import extensions
        connector_settings("claude", version="2.0.0", enabled=True)
        connector_settings("amp", enabled=False)
        self.addCleanup(setattr, extensions, "_connectors", None)

    def recipe(self, repo, text):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("sbx_control.paths.apps", return_value=directory))
        (directory / repo / ".sbx").mkdir(parents=True)
        (directory / repo / ".sbx/sbx.yaml").write_text(text)
        return directory / repo / ".sbx"

    # The registry.

    def test_builtin_connectors_load_first_and_plugins_come_through_entry_points(self):
        from sbx_control import extensions
        module = types.ModuleType("sbx_fixture_connector")

        class Fixture(extensions.Connector):
            kind, title = "fixture", "Fixture"

            def origin(self, info):
                return {"kind": "session", "label": "Fixture run", "detail": "", "href": ""}

            def keeper_policy(self, info, use):
                return extensions.Policy(keep_paused=True, decided=True)

        class Impostor(extensions.Connector):
            kind, title = "claude", "Not Claude"

        module.Fixture, module.Impostor, module.Broken = Fixture, Impostor, object
        self.enterContext(patch.dict(sys.modules, {"sbx_fixture_connector": module}))
        entries = [SimpleNamespace(value=f"sbx_fixture_connector:{name}") for name in ("Fixture", "Impostor", "Broken")]
        extensions._connectors = None  # as at a start of the control service
        installed = lambda group: entries if group == "sbx.connectors" else []  # noqa: E731 (what plugin packages declare)
        with patch.object(extensions.importlib.metadata, "entry_points", side_effect=installed):
            kinds = [c.kind for c in extensions.connectors()]
        self.assertEqual(kinds, ["github", "codex", "claude", "amp", "fixture"])
        self.assertEqual(extensions.get("claude").title, "Claude Code")
        made = SimpleNamespace(metadata={"connector": "fixture", "sbx": "f"})
        self.assertEqual(extensions.origin(made)["label"], "Fixture run")
        unused = extensions.Use(0.0, False, extensions.Probe)
        self.assertTrue(extensions.keeper_policy(made, unused).keep_paused)
        self.assertIsNone(extensions.keeper_policy(SimpleNamespace(metadata={"sbx": "plain"}), unused))
        from sbx_control import connections
        connections.REGISTRY.clear()
        self.addCleanup(connections.REGISTRY.clear)
        self.assertEqual(connections.get("fixture").title, "Fixture")

    def test_doctor_skips_extensions_that_are_off_or_not_set_up(self):
        import contextlib
        import io
        from sbx_control import admin, connections, extensions
        from sbx_control.connections import Check, Status

        class Platform:
            kind, title = "runtime", "Runtime"

            def __init__(self, ok):
                self.ok = ok

            def status(self):
                return Status(self.kind, self.title, "connected", "", {}, ())

            def test(self):
                return [Check("Embed API", self.ok)]

        connector_settings("claude", enabled=False)
        self.addCleanup(connections.REGISTRY.clear)
        for ok, code in ((True, 0), (False, 1)):
            connections.REGISTRY.clear()
            connections.REGISTRY.update({c.kind: c for c in (*extensions.connectors(), Platform(ok))})
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(admin.do_doctor(), code, out.getvalue())
        # Claude Code off, Amp and the GitHub App never set up: listed, not checked.
        self.assertIn("Claude Code: off", out.getvalue())
        self.assertIn("GitHub: not_connected", out.getvalue())
        self.assertNotIn("GitHub App", out.getvalue())
        self.assertIn("NO  Embed API", out.getvalue())

    def test_connectors_contribute_their_ports_and_agent_transcripts(self):
        from sbx_control import core, extensions
        self.assertIn(8089, extensions.reserved_ports())
        self.assertFalse(core.detectable_port(8089))
        self.assertIn("/home/user/.claude/projects", extensions.activity_paths())

    # Templates: what goes in, its fingerprint and name, and staleness.

    def test_platform_names_templates_by_key_and_fingerprint(self):
        from sbx_control import templates
        from sbx_control.contributions import Contribution, TemplateLayer
        plain = templates.plan(templates.DEFAULT, [])
        tool = templates.plan(templates.DEFAULT, [Contribution("tool", template=TemplateLayer(apt=("jq",)))])
        self.assertNotEqual(plain.fingerprint, tool.fingerprint)
        self.assertEqual(tool.name, f"default-{tool.fingerprint[:8]}")
        self.assertEqual(plain.base.key, templates.BASE)
        self.assertEqual(plain.base.name, f"sbx-base-{plain.base.fingerprint[:8]}")
        # Only what goes into the template counts: a runtime-only contribution changes nothing.
        from sbx_control.contributions import RuntimeLayer
        runtime = templates.plan(templates.DEFAULT, [Contribution("env", runtime=RuntimeLayer(env={"A": "1"}))])
        self.assertEqual(runtime.fingerprint, plain.fingerprint)

    def test_repo_templates_follow_their_recipe_and_never_share_a_name(self):
        from sbx_control import templates
        recipe = self.recipe("owner/app", "template: anything\nprevious: 1\n")
        (recipe.parent.parent / "other/.sbx").mkdir(parents=True)
        (recipe.parent.parent / "other/.sbx/sbx.yaml").write_text("template: anything\nprevious: 1\n")
        first = templates.plan("owner/app", [])
        self.assertTrue(first.name.startswith("owner-app-"))
        self.assertNotEqual(first.name, templates.plan("owner/other", []).name)
        (recipe / "build").write_text("#!/bin/sh\nmake\n")
        built = templates.plan("owner/app", []).fingerprint
        self.assertNotEqual(built, first.fingerprint)
        # What runs in sandboxes, not in the build, rebuilds nothing (a verify script edit rebuilt n8n's 15 steps).
        (recipe / "setup").write_text("#!/bin/sh\npnpm install\n")
        (recipe / "verify.py").write_text("print('ok')\n")
        (recipe / "sbx.yaml").write_text("template: anything\nprevious: 1\nenv: {A: b}\nhealth: [http://127.0.0.1:1/]\n")
        self.assertEqual(templates.plan("owner/app", []).fingerprint, built)
        (recipe / "Dockerfile").write_text("FROM sbx-base\nRUN true\n")
        with_dockerfile = templates.plan("owner/app", []).fingerprint
        self.assertNotEqual(with_dockerfile, built)
        (recipe / "sbx.yaml").write_text("template: anything\nworkdir: /home/user/elsewhere\n")
        self.assertNotEqual(templates.plan("owner/app", []).fingerprint, with_dockerfile)
        (recipe / "sbx.yaml").write_text("previews: {web: 3000}\n")
        self.assertIsNone(templates.plan("owner/app", []).recipe)   # the default layer, for its packages' layers

    def test_enabling_a_connector_or_bumping_its_version_makes_templates_stale(self):
        from sbx_control import template_builds, templates
        with patch.object(templates, "build"):  # E2B's builder
            template_builds.build_now(templates.DEFAULT)
        self.assertEqual(template_builds.stale(), [])
        built = template_builds.template_for(None)
        self.assertTrue(built.startswith("default-"))
        connector_settings("claude", version="2.0.1")
        self.assertEqual(template_builds.stale(), [templates.DEFAULT])
        self.assertTrue(template_builds.state(templates.DEFAULT)["stale"])
        self.assertEqual(template_builds.template_for(None), built)  # until the rebuild, the last one built
        # A build of that plan failing is not tried again every round, only once its inputs change.
        with patch.object(templates, "build", side_effect=RuntimeError("fixture build failure")):
            with self.assertRaises(RuntimeError):
                template_builds.build_now(templates.DEFAULT)
        self.assertEqual(template_builds.state(templates.DEFAULT)["status"], "failed")
        self.assertEqual(template_builds.stale(), [])
        before = template_builds.plan(templates.DEFAULT).fingerprint
        connector_settings("amp", enabled=True)
        self.assertNotEqual(template_builds.plan(templates.DEFAULT).fingerprint, before)
        self.assertIn("amp", [c.name for c in template_builds.plan(templates.DEFAULT).contributions])
        self.assertEqual(template_builds.stale(), [templates.DEFAULT])

    def test_a_new_codex_or_amp_cli_release_makes_templates_stale(self):
        # The F5 rule: what a template installs is pinned, so the fingerprint follows it.
        from sbx_control import template_builds, templates
        connector_settings("amp", enabled=True)
        before = template_builds.plan(templates.DEFAULT).fingerprint
        connector_settings("codex", version="0.160.0")
        codex = next(c for c in template_builds.plan(templates.DEFAULT).contributions if c.name == "codex")
        self.assertIn("@openai/codex@0.160.0", codex.template.root[0])
        self.assertNotEqual(template_builds.plan(templates.DEFAULT).fingerprint, before)
        before = template_builds.plan(templates.DEFAULT).fingerprint
        connector_settings("amp", cli_version="0.0.1700000000-gabcdef")
        amp = next(c for c in template_builds.plan(templates.DEFAULT).contributions if c.name == "amp")
        self.assertIn("AMP_VERSION=0.0.1700000000-gabcdef ", amp.template.root[0])
        self.assertNotEqual(template_builds.plan(templates.DEFAULT).fingerprint, before)
        from sbx_control import settings
        with self.assertRaises(settings.Invalid):
            connector_settings("codex", version="1.0; rm -rf /")

    def test_a_public_repo_never_has_a_template_of_its_own(self):
        # Only trusted repos' templates are built: a public one asking for one would show
        # "Not built" forever, and a manual build would run its .sbx/build.
        from sbx_control import settings, template_builds, templates
        self.recipe("owner/app", "template: yes\n")
        settings.update("access", {**settings.current().access.model_dump(), "public_repos": True}, by="test")
        self.assertEqual(settings.trust("owner/app"), "public")
        self.assertFalse(template_builds.own("owner/app"))
        self.assertEqual(template_builds.default_for("owner/app"), templates.DEFAULT)
        self.assertNotIn("owner/app", template_builds.keys())

    def test_applying_a_recipe_says_when_a_package_gives_the_repo_a_template(self):
        # The CLI's "it has a template of its own: sbx template build" hint reads it.
        import shutil
        from sbx_control import paths, repo_onboarding, settings
        from sbx_control.catalog import attachments
        folder = paths.packages() / "with-tools"
        folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, folder)
        (folder / "package.yaml").write_text("kind: provision\nruns: {create: run.sh}\ntemplate: {apt: [jq]}\n")
        (folder / "run.sh").write_text("#!/bin/sh\n")
        self.recipe("owner/app", "previews: {web: 3000}\n")
        settings.add_repo("owner/app", by="test")
        attachments.attach("owner/app", "with-tools", None, {}, {}, by="test")
        source = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (source / "sbx.yaml").write_text("previews: {web: 3000}\n")
        self.assertTrue(repo_onboarding.apply_recipe("owner/app", source, by="test")["prebuilt"])

    def test_template_plans_never_ask_the_secret_store(self):
        # own() runs on every create and every scheduler round: a store that is down
        # (Infisical) must not stop them, nor ask it once per attachment.
        import shutil
        from sbx_control import paths, secret_store, settings, template_builds
        from sbx_control.catalog import attachments
        folder = paths.packages() / "with-tools"
        folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, folder)
        (folder / "package.yaml").write_text("kind: provision\nruns: {create: run.sh}\ntemplate: {apt: [jq]}\n"
                                             "inputs: {token: {secret: true}}\n")
        (folder / "run.sh").write_text("#!/bin/sh\n")
        self.recipe("owner/app", "previews: {web: 3000}\n")
        settings.add_repo("owner/app", by="test")
        attachments.attach("owner/app", "with-tools", None, {"token": "fixture-token"}, {}, by="test", run_check=False)
        with patch.object(secret_store, "unseal", side_effect=secret_store.StoreError("fixture: the store is down")):
            self.assertTrue(template_builds.own("owner/app"))
            self.assertIn("with-tools", [c.name for c in template_builds.plan("owner/app").contributions])
            self.assertIn("owner/app", template_builds.keys())

    def test_a_package_attached_to_a_trusted_repo_adds_its_template_layer(self):
        import shutil
        from sbx_control import paths, settings, template_builds
        from sbx_control.catalog import attachments
        folder = paths.packages() / "with-tools"
        folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, folder)
        (folder / "package.yaml").write_text("kind: provision\nruns: {create: run.sh}\ntemplate:\n"
                                             "  apt: [postgresql-client]\n  files: {/etc/tool.conf: 'x = 1'}\n")
        (folder / "run.sh").write_text("#!/bin/sh\n")
        self.recipe("owner/app", "template: anything\n")
        settings.add_repo("owner/app", by="test")
        before = template_builds.plan("owner/app")
        self.assertNotIn("with-tools", [c.name for c in before.contributions])
        attachments.attach("owner/app", "with-tools", None, {}, {}, by="test")
        after = template_builds.plan("owner/app")
        layer = next(c.template for c in after.contributions if c.name == "with-tools")
        self.assertEqual((layer.apt, layer.files["/etc/tool.conf"].content), (("postgresql-client",), "x = 1"))
        self.assertNotEqual(after.fingerprint, before.fingerprint)
        attachments.detach("owner/app", "with-tools", by="test")
        self.assertEqual(template_builds.plan("owner/app").fingerprint, before.fingerprint)

    def test_a_package_template_layer_reaches_a_repo_on_the_default_template(self):
        import shutil
        from sbx_control import paths, settings, template_builds, templates
        from sbx_control.catalog import attachments
        folder = paths.packages() / "with-tools"
        folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, folder)
        (folder / "package.yaml").write_text("kind: provision\nruns: {create: run.sh}\ntemplate: {apt: [jq]}\n")
        (folder / "run.sh").write_text("#!/bin/sh\n")
        self.recipe("owner/app", "previews: {web: 3000}\n")   # no template of its own
        settings.add_repo("owner/app", by="test")
        self.assertNotIn("owner/app", template_builds.keys())
        attachments.attach("owner/app", "with-tools", None, {}, {}, by="test")
        self.assertIn("owner/app", template_builds.keys())
        p = template_builds.plan("owner/app")
        self.assertIn("with-tools", [c.name for c in p.contributions])
        self.assertIsNone(p.recipe)
        with patch.object(templates, "build"), patch.object(template_builds.recipes, "mirror", return_value=Path("/unused")), \
                patch.object(template_builds.recipes, "import_from", return_value=True):
            template_builds.build_now(templates.BASE)
            template_builds.build_now("owner/app")
        self.assertEqual(template_builds.template_for("owner/app"), p.name)

    def test_a_repo_without_its_own_built_template_is_refused_and_its_build_starts(self):
        from sbx_control import readiness, recipes, settings, template_builds, templates
        self.recipe("owner/app", "template: yes\n")
        settings.add_repo("owner/app", by="test")
        with patch.object(templates, "build"):  # E2B's builder
            template_builds.build_now(templates.DEFAULT)
            # The default is no stand-in for a template of its own: create and readiness agree.
            with patch.object(template_builds, "start") as start:
                with self.assertRaisesRegex(template_builds.NotBuilt, "owner/app template is building"):
                    template_builds.template_for("owner/app")
            start.assert_called_once_with("owner/app", "embed")
            self.assertEqual(readiness.template_key(True, template_builds.current("owner/app")), "missing")
            with patch.object(recipes, "mirror", return_value=Path("/unused")):  # the repo's clone
                template_builds.build_now("owner/app")
        self.assertEqual(template_builds.template_for("owner/app"), template_builds.plan("owner/app").name)

    def test_a_row_without_a_built_template_reads_as_missing_and_stale(self):
        """Built while the repo had no template of its own: status built, no built template."""
        from sbx_control import readiness, settings, state, template_builds
        self.recipe("owner/app", "template: yes\n")
        settings.add_repo("owner/app", by="test")
        with state.db() as conn:
            conn.execute("insert into template_builds (runtime, repo, status, finished_at) values ('embed', 'owner/app', 'built', '2026-11-01')")
        build = template_builds.current("owner/app")
        self.assertEqual(readiness.template_key(True, build), "missing")
        self.assertIn("owner/app", template_builds.stale())
        self.assertIsNone(next(r for r in readiness.summaries() if r["repo"] == "owner/app")["built_at"])
        with patch.object(template_builds, "start") as start, self.assertRaises(template_builds.NotBuilt):
            template_builds.template_for("owner/app")
        start.assert_called_once_with("owner/app", "embed")

    def test_a_template_whose_last_build_failed_is_not_restarted_by_a_create(self):
        from sbx_control import settings, template_builds
        self.recipe("owner/app", "template: yes\n")
        settings.add_repo("owner/app", by="test")
        with patch.object(template_builds.recipes, "mirror", return_value=Path("/unused")), \
                patch.object(template_builds.templates, "build", side_effect=[None, RuntimeError("clone refused")]), \
                self.assertRaises(RuntimeError):
            template_builds.build_now("owner/app")   # the base builds, then the repo's fails
        with patch.object(template_builds, "start") as start, \
                self.assertRaisesRegex(template_builds.NotBuilt, "last build failed \\(clone refused\\)"):
            template_builds.template_for("owner/app")
        start.assert_not_called()

    def test_no_default_template_is_planned_at_the_size_of_a_repo_with_its_own(self):
        # The launch lab built default:4cpu-8192mb-* beside n8n's own template: nobody starts from it.
        from sbx_control import settings, template_builds, templates
        self.recipe("owner/app", "template: yes\nresources: {cpu: 4, memory_mb: 8192}\n")
        settings.add_repo("owner/app", by="test")
        self.assertTrue(template_builds.own("owner/app"))
        self.assertEqual(template_builds.keys(), [templates.BASE, templates.DEFAULT, "owner/app"])
        self.recipe("owner/app", "resources: {cpu: 4, memory_mb: 8192}\n")   # the same size, no template
        self.assertEqual(template_builds.keys(), [templates.BASE, templates.DEFAULT, "default:4cpu-8192mb-8192mb"])

    def test_a_recipe_size_applies_with_or_without_a_template_of_its_own(self):
        from sbx_control import recipes, template_builds, templates
        self.recipe("owner/app", "resources: {cpu: 4, memory_mb: 8192, swap_mb: 1024}\n")
        key = template_builds.default_for("owner/app")
        self.assertEqual(key, "default:4cpu-8192mb-8192mb")
        sized, plain = templates.plan(key, []), templates.plan(templates.DEFAULT, [])
        self.assertEqual(sized.size, {"cpu": 4, "memory_mb": 8192, "disk_mb": 8192})
        self.assertEqual(plain.size, recipes.DEFAULT_SIZE)
        self.assertNotEqual(sized.name, plain.name)
        self.assertTrue(sized.name.startswith("default-4cpu-8192mb-8192mb-"))
        with patch.object(template_builds.settings, "trust", return_value="trusted"):
            self.assertIn(key, template_builds.keys())
        # The default size's template is no stand-in for another size.
        with patch.object(templates, "build") as build:  # E2B's builder
            template_builds.build_now(templates.DEFAULT)
            with patch.object(template_builds, "start"), \
                    self.assertRaisesRegex(template_builds.NotBuilt, "default:4cpu"):
                template_builds.template_for("owner/app")
            template_builds.build_now(key)
        self.assertEqual(build.call_args.args[0].size["memory_mb"], 8192)
        built = template_builds.template_for("owner/app")
        self.assertEqual(built, template_builds.plan(key).name)
        self.assertEqual(template_builds.size_of(built), 8192)
        self.assertIsNone(template_builds.size_of("a-snapshot"))
        # A prebuilt template is built at the recipe's size too.
        self.recipe("owner/big", "template: yes\nresources: {memory_mb: 16384}\n")
        self.assertEqual(templates.plan("owner/big", []).size, {"cpu": 2, "memory_mb": 16384, "disk_mb": 8192})
        with self.assertRaises(recipes.RecipeError):
            recipes.App("owner/bad", Path("/unused"), {"resources": {"cpu": "4"}}).size

    def test_a_build_runs_contributions_and_their_ready_checks(self):
        from sbx_control import templates
        from sbx_control.contributions import Artifact, Check, Contribution, File, TemplateLayer
        layer = TemplateLayer(apt=("gh",), files={"/usr/local/bin/tool": File("#!/bin/sh\n", 0o755)},
                              root=("echo root",), user=("echo user",),
                              artifacts=(Artifact("tools v1", lambda: b"tarball", "/opt"),))
        p = templates.plan(templates.DEFAULT, [Contribution("tool", template=layer, ready=Check("tool --version"))])
        with patch.object(templates.runtime, "build_template") as build, \
                patch.object(templates, "bundles", return_value=Path(self.enterContext(tempfile.TemporaryDirectory()))):
            templates.build(p)
        steps = build.call_args.args[0].steps
        commands = [value for what, value in steps if what == "run"]
        self.assertTrue(any("apt-get install" in c and " gh " in c for c in commands))
        self.assertTrue(any("/usr/local/bin/tool" in c and "chmod 755" in c for c in commands))
        self.assertTrue(any("tar -xz -C /opt" in c for c in commands))
        self.assertEqual(commands[-1], "tool --version")
        self.assertEqual(("template", p.base.name), steps[0])
        self.assertEqual(build.call_args.kwargs["alias"], p.name)
        self.assertEqual(list(templates.bundles().glob("*")), [])  # the artifact was served once

    # A sandbox's runtime.

    def test_the_runtime_env_round_trips_and_shells_never_get_the_app_only_values(self):
        from sbx_control import core
        from sbx_control.contributions import File, RuntimeLayer
        written = {}
        sandbox = Mock()
        sandbox.files.write.side_effect = lambda path, content, **_: written.__setitem__(path.rsplit("/", 1)[-1], content)
        sandbox.commands.run.return_value.stdout = "ssh-ed25519 key"
        layer = RuntimeLayer(env={"TOOL_HOME": "/run/sbx/ext/tool"}, files={"tool/config": File("x = 1\n")})
        envs = {"APP": "1", "TOOL_HOME": "/run/sbx/ext/tool", "APP_ONLY": "app-only-value"}
        core.write_runtime(sandbox, "fixture", envs, layer, hidden={"APP_ONLY"})
        self.assertEqual(written["config"], "x = 1\n")
        self.assertNotIn("app-only-value", written["sbx-env"])  # what SSH shells and agents source
        # What the sandbox prints for its env file, then its app's.
        sandbox.commands.run.return_value.stdout = f"{written['sbx-env']}{core.ENV_SPLIT}\n{written['sbx-app-env']}"
        self.assertEqual(core.written_env(sandbox), envs)

    def test_codex_keeps_only_the_login_in_the_tmpfs(self):
        # memos-demo on the launch lab: CODEX_HOME in /run grew to 784 MB and filled it.
        from sbx_control import core, extensions, platform
        from sbx_control.connectors.codex import Codex
        from sbx_control.contributions import merged_runtime
        made = Codex().contribution(extensions.Target(repo="owner/app", name="fixture", trusted=True))
        home = f"{platform.HOME}/.codex"
        self.assertEqual(made.runtime.env, {"CODEX_HOME": home})
        self.assertEqual(made.runtime.links, {f"{home}/auth.json": f"{platform.RUN}/codex/auth.json"})
        self.assertEqual(Codex.login.path, f"{platform.RUN}/codex/auth.json")   # where sbx open codex writes it
        self.assertIn("$HOME/.codex", made.scrub_paths)
        self.assertEqual(Codex.activity, (f"{home}/sessions",))
        sandbox = Mock()
        sandbox.commands.run.return_value.stdout = "ssh-ed25519 key"
        core.write_runtime(sandbox, "fixture", {}, merged_runtime([made]))
        command = sandbox.commands.run.call_args_list[-1].args[0]
        self.assertIn(f"mkdir -p -m 700 {platform.RUN}/codex ", command)
        self.assertIn(f"ln -sfn {platform.RUN}/codex/auth.json {home}/auth.json", command)

    def test_the_claude_runner_starts_without_the_agents_hidden_credentials(self):
        from sbx_control import core, recipes
        from sbx_control.connectors.claude_code import ClaudeCode, runner
        connector, commands = ClaudeCode(), []

        def run_in(_sandbox, script, **_):
            commands.append(script)
            return Mock(stdout="started")
        sandbox = core.Attached("fixture", Mock(), recipes.App("owner/app", Path("/unused"), {}))
        with patch.object(core, "run_in", side_effect=run_in), patch.object(recipes, "ship"), \
                patch("sbx_control.resources.snapshot", return_value=None):
            runner.start(sandbox, {}, "order", "fixture", connector.settings())
        self.assertIn("unset ANTHROPIC_API_KEY OPENAI_API_KEY CLAUDE_CODE_OAUTH_TOKEN", commands[-1])
        # Over SSH when the sandbox's env says so (GitHub's deploy-key layer), without asking GitHub here.
        self.assertIn("${GIT_SSH_COMMAND:+--git-ssh-rewrite github.com}", commands[-1])

    def test_a_reusable_snapshot_scrubs_agent_transcripts_without_building_runtime_layers(self):
        from sbx_control import core, extensions
        from sbx_control.connectors.amp import snapshots
        target = extensions.Target(repo="owner/app", name="fixture", trusted=True, connector="amp")
        with patch.object(core, "target_of", return_value=target), \
                patch.object(extensions.packages(), "contributions", side_effect=AssertionError("package host code ran")):
            script = snapshots.scrub(SimpleNamespace(metadata={"sbx": "fixture"}))
        self.assertIn('"$HOME"/.claude ', script)   # Claude Code's transcripts and login
        self.assertIn('"$HOME"/.claude.json', script)
        self.assertIn('"$HOME"/.config/chromium', script)   # the platform's

    def test_github_contributes_gh_and_its_credentials_only_when_connected(self):
        from sbx_control import extensions, github_app, platform
        from sbx_control.connections import github
        target = extensions.Target(repo="owner/app", name="fixture", trusted=True)
        self.assertIsNone(github.GitHub().contribution(target))
        with patch.object(github_app, "settings", return_value={"id": "1"}), \
                patch.object(github_app, "access", return_value="app"), \
                patch.object(platform, "session_key", return_value="fixture-key"):
            made = github.GitHub().contribution(target)
        self.assertIn("gh", made.template.apt)
        self.assertEqual(made.runtime.git_config, {"credential.https://github.com.helper": github.CREDENTIAL})
        self.assertIn("credential", made.runtime.files)
        self.assertEqual(made.runtime.env, {})
        with patch.object(github_app, "settings", return_value={"id": "1"}), \
                patch.object(github_app, "access", return_value="key"), \
                patch.object(platform, "credentials", return_value=Mock(github_key="fixture-deploy-key")):
            made = github.GitHub().contribution(target)
        self.assertEqual(made.runtime.env, {"GIT_SSH_COMMAND": f"ssh -i {github.KEY} -o IdentitiesOnly=yes"})
        self.assertEqual(made.runtime.files["key"].content, "fixture-deploy-key")
