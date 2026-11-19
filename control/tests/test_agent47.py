"""The Agent47 connector: off until saved, then its pinned CLI in every
template and, in trusted sandboxes, the installation's provider settings in
a file only its own commands read."""
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock

from support import Server, forget_extensions

KEY = "sk-or-test-only-0123456789"


class Agent47Tests(Server):
    SETTINGS = {"domain": {"address": "203.0.113.10"}}

    def setUp(self):
        super().setUp()
        self.addCleanup(forget_extensions)

    def put(self, **values):
        return self.client.put("/api/connections/agent47", headers=self.headers, json={"values": values})

    def listed(self):
        found = self.client.get("/api/connections", headers=self.headers).json()["connections"]
        return next(c for c in found if c["kind"] == "agent47")

    def contribution(self, **target):
        from sbx_control import extensions
        return extensions.get("agent47").contribution(extensions.Target(repo="owner/app", **target))

    def test_it_is_off_and_in_no_template_until_saved(self):
        from sbx_control import extensions, template_builds, templates
        listed = self.listed()
        self.assertEqual((listed["group"], listed["state"], listed["actions"]), ("agent", "off", ["connect"]))
        self.assertIsNone(self.contribution())
        for key in (templates.BASE, templates.DEFAULT):
            self.assertNotIn("agent47", [c.name for c in template_builds.plan(key).contributions])
        self.assertNotIn("agent47", [c.kind for c in extensions.connectors() if c.choosable])
        self.assertNotIn("agent47", extensions.logins())

    def test_saving_it_through_the_api_builds_its_pinned_cli_into_the_templates(self):
        from sbx_control import template_builds, templates
        from sbx_control.connectors import agent47
        before = template_builds.plan(templates.DEFAULT).fingerprint
        base = template_builds.plan(templates.BASE).fingerprint
        saved = self.put(provider="openrouter", api_key=KEY)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn(KEY, saved.text)
        body = saved.json()
        self.assertEqual((body["state"], body["summary"]),
                         ("connected", "In every sandbox, with this installation's OpenRouter key"))
        self.assertIn("rebuilds", body)   # what `sbx extensions set agent47 --wait` waits for
        fields = {f["name"]: f for f in body["fields"]}
        self.assertEqual((fields["api_key"]["secret"], fields["api_key"]["set"], fields["api_key"]["value"]),
                         (True, True, None))
        self.assertEqual((fields["provider"]["kind"], fields["provider"]["value"]), ("choice", "openrouter"))
        self.assertTrue(fields["version"]["hidden"])   # pinned by sbx: the API and CLI set it, no form field
        plan = template_builds.plan(templates.DEFAULT)
        self.assertNotEqual(plan.fingerprint, before)
        self.assertEqual(template_builds.plan(templates.BASE).fingerprint, base)   # not in the base template
        layer = next(c for c in plan.contributions if c.name == "agent47")
        self.assertEqual(layer.template.apt, ("python3-venv",))
        self.assertIn("/opt/sbx-agent47/bin/pip install", layer.template.root[0])
        self.assertTrue(layer.template.root[0].endswith(" agent47==0.1.0"))
        self.assertEqual(set(layer.template.files), {"/usr/local/bin/code-agent", "/usr/local/bin/agent47"})
        self.assertEqual({f.mode for f in layer.template.files.values()}, {0o755})
        self.assertEqual(layer.ready.command, "code-agent --version | grep -qF 0.1.0")
        self.assertIsNone(layer.runtime)   # a template gets no key
        self.assertNotIn(KEY, repr(layer))
        self.assertEqual(agent47.Agent47.busy, ("/opt/sbx-agent47/bin/",))
        # A new pinned version is a new template.
        self.assertEqual(self.put(version="0.1.1").status_code, 200)
        newer = template_builds.plan(templates.DEFAULT)
        self.assertNotEqual(newer.fingerprint, plan.fingerprint)
        self.assertIn("agent47==0.1.1", next(c for c in newer.contributions if c.name == "agent47").template.root[0])

    def test_a_trusted_sandbox_gets_the_key_in_a_file_only_its_commands_read(self):
        from sbx_control import core, extensions
        from sbx_control.contributions import merged_runtime
        self.assertEqual(self.put(provider="gemini", api_key=KEY, model="gemini-3.5-flash").status_code, 200)
        self.assertIsNone(self.contribution(name="fixture", trusted=False).runtime)   # never in a public repo's sandbox
        made = self.contribution(name="fixture", trusted=True)
        self.assertEqual(made.runtime.env, {})   # not in the sandbox's env: the app and other agents never get it
        self.assertEqual(made.runtime.sources, ())   # nor in what every shell sources
        self.assertEqual(made.runtime.files["env"].mode, 0o600)
        self.assertEqual(made.runtime.files["env"].content.splitlines(), [
            '[ -n "${AGENT_PROVIDER:-}" ] || export AGENT_PROVIDER=gemini',
            '[ -n "${AGENT_MODEL:-}" ] || export AGENT_MODEL=gemini-3.5-flash',
            f'[ -n "${{GEMINI_API_KEY:-}}" ] || export GEMINI_API_KEY={KEY}'])
        self.assertEqual(made.scrub_paths, ("$HOME/.config/agent47", "$HOME/app/.code-agent"))
        self.assertIn("$HOME/app/.code-agent", extensions.scrub_paths(extensions.Target(repo="owner/app")))
        written, sandbox = {}, Mock()
        sandbox.files.write.side_effect = lambda path, content, **_: written.__setitem__(path, content)
        sandbox.commands.run.return_value.stdout = "ssh-ed25519 key"
        core.write_runtime(sandbox, "fixture", {"APP": "1"}, merged_runtime([made]))
        holding = [path for path, content in written.items() if KEY in content]
        self.assertEqual([path.rsplit("/ext/", 1)[-1] for path in holding], ["agent47/env"])
        self.assertIn("chmod 600", sandbox.commands.run.call_args_list[-1].args[0])

    def test_without_a_key_developers_bring_their_own(self):
        saved = self.put()
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["summary"], "In every sandbox; each developer brings a key")
        made = self.contribution(name="fixture", trusted=True)
        self.assertEqual(made.runtime.files["env"].content,
                         '[ -n "${AGENT_PROVIDER:-}" ] || export AGENT_PROVIDER=openrouter\n')
        checks = self.client.post("/api/connections/agent47/test", headers=self.headers).json()
        self.assertTrue(checks["ok"])
        self.assertIn("developers export OPENROUTER_API_KEY themselves", checks["checks"][1]["detail"])

    def test_the_commands_take_the_installation_s_values_only_where_the_shell_has_none(self):
        """The wrapper as the template installs it, against a stand-in for the
        virtualenv's command that prints what it was given."""
        from sbx_control.connectors import agent47
        self.assertEqual(self.put(provider="openrouter", api_key=KEY).status_code, 200)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "venv/bin").mkdir(parents=True)
        real = root / "venv/bin/code-agent"
        real.write_text('#!/bin/sh\necho "$AGENT_PROVIDER $OPENROUTER_API_KEY $*"\n')
        real.chmod(0o755)
        env = root / "env"
        env.write_text(self.contribution(name="fixture", trusted=True).runtime.files["env"].content)
        wrapper = root / "code-agent"
        wrapper.write_text(agent47.WRAPPER.replace(agent47.ENV, str(env)).replace(agent47.VENV, str(root / "venv")))
        wrapper.chmod(0o755)

        def run(**shell):
            done = subprocess.run([str(wrapper), "run", "a task"], capture_output=True, text=True, timeout=30,
                                  env={"PATH": os.environ["PATH"], **shell})
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout.strip()
        self.assertEqual(run(), f"openrouter {KEY} run a task")
        self.assertEqual(run(OPENROUTER_API_KEY="the-developer-s-own"), "openrouter the-developer-s-own run a task")
        env.unlink()   # a public repository's sandbox, or the extension saved without a key
        self.assertEqual(run(AGENT_PROVIDER="gemini"), "gemini  run a task")

    def test_a_key_stays_with_its_provider_and_bad_values_are_refused(self):
        from sbx_control import extensions, vault
        from sbx_control.connectors import agent47
        self.assertEqual(self.put(provider="openrouter", api_key=KEY).status_code, 200)
        moved = self.put(provider="deepseek")
        self.assertEqual(moved.status_code, 400, moved.text)
        self.assertIn("enter the DeepSeek key", moved.json()["detail"]["message"])
        self.assertEqual(extensions.get("agent47").settings().provider, "openrouter")
        for values in ({"version": "0.1.0; rm -rf /"}, {"provider": "elsewhere"}, {"model": "x $(id)"},
                       {"api_key": "two words"}):
            refused = self.put(**values)
            self.assertEqual(refused.status_code, 400, refused.text)
            self.assertNotIn(KEY, refused.text)
        self.assertEqual(self.put(provider="deepseek", api_key="sk-deepseek-test-only").status_code, 200)
        self.assertIn("export DEEPSEEK_API_KEY=sk-deepseek-test-only",
                      self.contribution(name="fixture", trusted=True).runtime.files["env"].content)
        # Off keeps the key; disconnecting (an admin's) removes it.
        self.assertEqual(self.put(enabled="false").json()["state"], "off")
        self.assertTrue(vault.facts(agent47.KEY).set)
        self.assertIsNone(self.contribution(name="fixture", trusted=True))
        extensions.get("agent47").disconnect(by="test")
        self.assertFalse(vault.facts(agent47.KEY).set)
        self.assertEqual(self.listed()["state"], "off")

    def test_a_key_request_links_where_the_provider_gives_keys(self):
        from sbx_control import extensions
        self.assertEqual(extensions.get("agent47").help_url, "https://openrouter.ai/keys")
        self.assertEqual(self.put(provider="nvidia").status_code, 200)
        self.assertEqual(extensions.get("agent47").help_url, "https://build.nvidia.com/explore/discover")
