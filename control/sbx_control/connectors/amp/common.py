"""What the Amp connector's modules share: its kind, settings, provider
token and CLI, the job error, and the per-orb lock."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import re
import time
from pathlib import Path

from pydantic import field_validator, model_validator

from ... import paths, settings, vault

KIND = "amp"
APPLY = "amp"   # the host effect that restarts the provider (a new token, or its settings)
TOKEN = vault.Key("ORB_PROVIDER_TOKEN", KIND, applies=(APPLY,))
CLI = Path("/usr/local/lib/sbx-amp/bin/amp")


class JobError(ValueError):
    pass


class Settings(settings.ExtensionSection):
    applies = (APPLY,)
    enabled: bool = False
    chosen: bool = False            # chosen in the setup: in every template before the token is added
    # The Amp CLI release templates and the host install (AMP_VERSION for
    # ampcode.com/install.sh, which checks the release's sha256): in the
    # install command, so a new one changes the templates' fingerprint.
    cli_version: str = "0.0.1794600035-g3776ae"
    # Filesystem-only snapshots (checkpoints/build.sh): the runtime's API image
    # and orchestrator binary built with them. Both or neither.
    checkpoint_api_image: str = ""
    orchestrator_binary: str = ""

    def effects(self, before: "Settings") -> tuple[str, ...]:
        """New checkpoint builds recreate the runtime: pending until `sbx-admin network apply`."""
        moved = (self.checkpoint_api_image, self.orchestrator_binary) != (before.checkpoint_api_image,
                                                                          before.orchestrator_binary)
        return self.applies + ((settings.HostUnit.RUNTIME_NETWORK,) if moved else ())

    @model_validator(mode="after")
    def _checkpoints(self):
        if bool(self.checkpoint_api_image) != bool(self.orchestrator_binary):
            raise ValueError("checkpoint_api_image and orchestrator_binary go together")
        if self.orchestrator_binary and not self.orchestrator_binary.startswith("/"):
            raise ValueError("orchestrator_binary must be an absolute path on the host")
        return self

    @field_validator("cli_version")
    @classmethod
    def _cli_version(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[0-9][0-9A-Za-z.+-]*", value):
            raise ValueError("an Amp CLI release, such as 0.0.1794600035-g3776ae")
        return value


def current() -> Settings:
    return settings.current().extension(KIND, Settings)


@contextlib.contextmanager
def orb_lock(orb_id: str):
    directory = paths.state_dir() / "amp-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Keep lock inodes: unlinking them can let two waiters lock different files.
    with (directory / hashlib.sha256(orb_id.encode()).hexdigest()).open("a") as lock:
        deadline = time.monotonic() + 240
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("another job still holds this orb; retry the job")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
