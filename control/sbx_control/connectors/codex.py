"""The Codex connector: the `codex` CLI in every template, at the version
its settings pin, run on each developer's own login, which `sbx open codex`
copies into the sandbox's tmpfs.

Only the login is in the tmpfs. CODEX_HOME is on the sandbox's disk, with
auth.json there a link to the login: Codex keeps packages, plugins, caches,
logs and sessions in CODEX_HOME, hundreds of MB within an hour, and the
tmpfs (/run, a fifth of the VM's memory) also holds sbx's env and its
process supervisor's state, which a full /run stops. Codex reads and
replaces auth.json through the link (codex-rs login/src/auth/storage_file.rs:
"Symlinks retain their target"), so a refreshed login stays in the tmpfs."""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from pydantic import field_validator

from .. import extensions, platform, settings
from ..connections.base import Check, Field, Refused, Status
from ..contributions import Check as Ready, Contribution, RuntimeLayer, TemplateLayer

HOME = f"{platform.HOME}/.codex"          # CODEX_HOME, on disk
LOGIN = f"{platform.RUN}/codex/auth.json"  # the developer's login, in the tmpfs only


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
    login = extensions.Login(LOGIN, "codex", get=("codex", "login"), file="~/.codex/auth.json")

    def contribution(self, target) -> Contribution:
        version = self.settings().version
        return Contribution(
            "codex",
            template=TemplateLayer(root=(f"npm install -g --prefix /usr/local @openai/codex@{version} >/dev/null",)),
            runtime=RuntimeLayer(env={"CODEX_HOME": HOME}, dirs=(HOME, str(PurePosixPath(LOGIN).parent)),
                                 links={f"{HOME}/auth.json": LOGIN}),
            ready=Ready("codex --version"),
            # Its sessions and history (source, secrets) and the link to the login.
            scrub_paths=("$HOME/.codex",))

    def status(self) -> Status:
        version = self.settings().version
        return Status(self.kind, self.title, "connected",
                      "In every sandbox",
                      {}, (Field("version", "Codex version", value=version, hidden=True),), ("connect",))

    def test(self, values: dict | None = None) -> list[Check]:
        return [Check("codex CLI", True, f"version {self.settings().version}, built into every template")]

    def connect(self, values: dict, *, by: str) -> Status:
        if not values.get("version"):
            raise Refused("Codex takes only a version (of @openai/codex) to install in the templates")
        self.save_settings({**self.settings().model_dump(), "version": str(values["version"])}, by=by)
        return self.status()
