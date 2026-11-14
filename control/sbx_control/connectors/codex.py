"""The Codex connector: the `codex` CLI in every template, at the version
its settings pin, run on each developer's own login, which `sbx open codex`
copies into the sandbox's tmpfs."""
from __future__ import annotations

import re

from pydantic import field_validator

from .. import extensions, platform, settings
from ..connections.base import Check, Field, Refused, Status
from ..contributions import Check as Ready, Contribution, RuntimeLayer, TemplateLayer

HOME = f"{platform.RUN}/codex"   # CODEX_HOME: the developer's login and Codex's sessions, in the tmpfs


class Settings(settings.ExtensionSection):
    # The npm version of @openai/codex every template installs: in the
    # install command, so a new one changes the templates' fingerprint.
    version: str = "0.167.2"

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[0-9][0-9A-Za-z.+-]*", value):
            raise ValueError("a version of @openai/codex, such as 0.167.2")
        return value


class Codex(extensions.Connector):
    kind = "codex"
    title = "Codex"
    shown = ("version",)
    section = Settings
    activity = (f"{HOME}/sessions",)
    login = extensions.Login(f"{HOME}/auth.json", "codex", get=("codex", "login"), file="~/.codex/auth.json")

    def contribution(self, target) -> Contribution:
        version = self.settings().version
        return Contribution(
            "codex",
            template=TemplateLayer(root=(f"npm install -g --prefix /usr/local @openai/codex@{version} >/dev/null",)),
            runtime=RuntimeLayer(env={"CODEX_HOME": HOME}, dirs=(HOME,)),
            ready=Ready("codex --version"))

    def status(self) -> Status:
        version = self.settings().version
        return Status(self.kind, self.title, "connected",
                      f"version {version} in every sandbox, on each developer's own login "
                      "(sbx login codex, then sbx open NAME codex)",
                      {"version": version}, (Field("version", "Codex version", value=version),), ("connect",))

    def test(self, values: dict | None = None) -> list[Check]:
        return [Check("codex CLI", True, f"version {self.settings().version}, built into every template")]

    def connect(self, values: dict, *, by: str) -> Status:
        if not values.get("version"):
            raise Refused("Codex takes only a version (of @openai/codex) to install in the templates")
        self.save_settings({**self.settings().model_dump(), "version": str(values["version"])}, by=by)
        return self.status()
