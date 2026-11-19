"""The Agent47 connector: the `agent47` package from PyPI (its `code-agent`
automation CLI and `agent47` terminal) in every template once it is turned
on, at the version its settings pin, with the installation's model provider
key for it.

Agent47 is a plain CLI: it has no interface through which sbx could start
its runs, so people and agents run it in a sandbox over SSH. It takes its
provider, model and key from the environment (code_agent/config.py), so the
connector hands them over in a file in the sandbox's tmpfs which only
Agent47's own commands read: the key is in no template, not in the
sandbox's env (which the app's processes and the other agents get), and
what a shell already has (a repository's secret, a developer's own key)
wins over it."""
from __future__ import annotations

import re
import shlex

from pydantic import ValidationError, field_validator

from .. import extensions, settings, vault
from ..connections.base import Check, Field, Refused, Status
from ..contributions import Check as Ready, Contribution, File, RuntimeLayer, TemplateLayer, folder

KIND = "agent47"
VENV = "/opt/sbx-agent47"                    # its own virtualenv: its dependencies never meet the app's
COMMANDS = ("code-agent", "agent47")         # the package's console scripts
ENV = f"{folder(KIND)}/env"                  # what the commands source, in the tmpfs
KEY = vault.Key("PROVIDER_KEY", KIND)        # the installation's key at the chosen provider
# provider (AGENT_PROVIDER) -> (title, the variable Agent47 reads its key from, where a key comes from)
PROVIDERS = {
    "openrouter": ("OpenRouter", "OPENROUTER_API_KEY", "https://openrouter.ai/keys"),
    "openai": ("OpenAI", "OPENAI_API_KEY", "https://platform.openai.com/api-keys"),
    "gemini": ("Google Gemini", "GEMINI_API_KEY", "https://aistudio.google.com/app/apikey"),
    "deepseek": ("DeepSeek", "DEEPSEEK_API_KEY", "https://platform.deepseek.com/api_keys"),
    "nvidia": ("NVIDIA NIM", "NVIDIA_API_KEY", "https://build.nvidia.com/explore/discover"),
}
# Each command as people call it: the installation's values where the shell
# has none of its own, then the real command in the virtualenv.
WRAPPER = f"""#!/bin/sh
if [ -r {ENV} ]; then . {ENV}; fi
exec {VENV}/bin/$(basename "$0") "$@"
"""


class Settings(settings.ExtensionSection):
    enabled: bool = False
    # The version of agent47 on PyPI every template installs: in the install
    # command, so a new one changes the templates' fingerprint.
    version: str = "0.1.0"
    provider: str = "openrouter"
    model: str = ""              # AGENT_MODEL; "" leaves Agent47's own default

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[0-9][0-9A-Za-z.+-]*", value):
            raise ValueError("a version of agent47 on PyPI, such as 0.1.0")
        return value

    @field_validator("provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in PROVIDERS:
            raise ValueError(f"one of {', '.join(PROVIDERS)}")
        return value

    @field_validator("model")
    @classmethod
    def _model(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*", value):
            raise ValueError("a model name of the provider, such as qwen/qwen3-coder")
        return value


def _default(name: str, value: str) -> str:
    """A line of ENV: the value, unless the shell already has one."""
    return f'[ -n "${{{name}:-}}" ] || export {name}={shlex.quote(value)}\n'


class Agent47(extensions.Connector):
    kind = KIND
    title = "Agent47"
    shown = ("version", "provider", "model")
    section = Settings
    busy = (f"{VENV}/bin/",)     # a run in progress (in tmux, say) is use: its interpreter's path

    def contribution(self, target) -> Contribution | None:
        s = self.settings()
        if not s.enabled:
            return None
        runtime = None
        if target.name and target.trusted:   # a sandbox of a trusted repository: the installation's key may go in
            lines = _default("AGENT_PROVIDER", s.provider)
            if s.model:
                lines += _default("AGENT_MODEL", s.model)
            key = vault.get(KEY)
            if key:
                lines += _default(PROVIDERS[s.provider][1], key)
            runtime = RuntimeLayer(files={"env": File(lines)})
        # Its run history (source, prompts) is in the checkout's .code-agent, its profile in ~/.config.
        scrub = ("$HOME/.config/agent47",
                 *((f"$HOME/{target.repo.rpartition('/')[2]}/.code-agent",) if target.repo else ()))
        return Contribution(
            KIND,
            template=TemplateLayer(
                apt=("python3-venv",),
                files={f"/usr/local/bin/{command}": File(WRAPPER, 0o755) for command in COMMANDS},
                root=(f"python3 -m venv {VENV} && {VENV}/bin/pip install --quiet --no-cache-dir "
                      f"--disable-pip-version-check agent47=={s.version}",)),
            runtime=runtime,
            ready=Ready(f"code-agent --version | grep -qF {shlex.quote(s.version)}"),
            scrub_paths=scrub)

    @property
    def help_url(self) -> str:
        return PROVIDERS[self.settings().provider][2]

    def status(self) -> Status:
        s = self.settings()
        key = vault.facts(KEY)
        title = PROVIDERS[s.provider][0]
        if not s.enabled:
            state, summary = "off", ""
        elif key.set:
            state, summary = "connected", f"In every sandbox, with this installation's {title} key"
        else:
            state, summary = "connected", "In every sandbox; each developer brings a key"
        fields = (
            Field("provider", "Model provider", value=s.provider, kind="choice",
                  options=tuple((name, found[0]) for name, found in PROVIDERS.items())),
            Field("api_key", "Provider API key", secret=True, set=key.set, optional=True,
                  help=f"Of {title} ({PROVIDERS[s.provider][2]}). Without one, developers export their own "
                       f"{PROVIDERS[s.provider][1]} in the sandbox."),
            Field("model", "Model", value=s.model, optional=True, placeholder="Agent47's default",
                  help="The provider's name for it, such as qwen/qwen3-coder."),
            Field("version", "Agent47 version", value=s.version, hidden=True))
        facts = {"version": s.version, "provider": title, "model": s.model} if s.enabled else {}
        return Status(self.kind, self.title, state, summary, facts, fields,
                      ("connect", "test", "disconnect") if s.enabled else ("connect",))

    def test(self, values: dict | None = None) -> list[Check]:
        s = self.settings()
        title, variable, where = PROVIDERS[s.provider]
        has = vault.facts(KEY).set or bool((values or {}).get("api_key"))
        return [Check("agent47 CLI", s.enabled,
                      f"version {s.version}, built into every template" if s.enabled else "off: in no template",
                      "save the extension to turn it on"),
                # No key is a way to run it, not a failure.
                Check(f"{title} key", True, "set: in every trusted repository's sandboxes" if has
                      else f"not set: developers export {variable} themselves", help_url=where)]

    def connect(self, values: dict, *, by: str) -> Status:
        """Saving turns it on (enabled=false: off, keeping the rest). A key
        belongs to its provider: changing the provider takes the new one's."""
        old = self.settings()
        new = {**old.model_dump(), "enabled": True}
        if "enabled" in values:
            new["enabled"] = str(values["enabled"]).lower() in ("1", "true", "yes", "on")
        for name in ("provider", "model", "version"):
            if values.get(name) is not None and (name == "model" or values[name] != ""):
                new[name] = str(values[name])
        try:
            validated = Settings.model_validate(new)
        except ValidationError as error:
            first = error.errors()[0]
            raise Refused(f"{first['loc'][0]}: {first['msg'].removeprefix('Value error, ')}")
        key = str(values.get("api_key") or "").strip()
        if key and (len(key) < 8 or any(c.isspace() for c in key)):
            raise Refused(f"that does not look like a {PROVIDERS[validated.provider][0]} API key")
        if validated.provider != old.provider and not key and vault.facts(KEY).set:
            raise Refused(f"the saved key is {PROVIDERS[old.provider][0]}'s: enter the "
                          f"{PROVIDERS[validated.provider][0]} key with the new provider")
        if key:
            vault.put(KEY, key, by=by)
        self.save_settings(validated, by=by)
        return self.status()

    def disconnect(self, *, by: str) -> Status:
        vault.delete(KEY, by=by)
        self.save_settings({**self.settings().model_dump(), "enabled": False}, by=by)
        return self.status()
