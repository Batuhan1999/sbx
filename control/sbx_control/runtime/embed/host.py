"""Embed's project on this host (/opt/e2b, which install.sh lays out): the
keys sbx owns in its .env, the layers extensions add to it
(Connector.embed), and its systemd units and commands. install.sh writes
the .env through `python -m sbx_control.runtime.embed embed-env`, and asks
`upgrade-check` first whether a release recreates the stack (recreates)."""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Mapping

EMBED = pathlib.Path("/opt/e2b")        # Embed's compose project (install.sh)
# Embed's own compose files: the vendored one and sbx's settings over it.
EMBED_COMPOSE = ("compose.yaml", "compose.override.yaml")
EMBED_PINS = r"^(E2B_[A-Z_]+_(IMAGE|VERSION)|RUNTIME_COMMIT)$"   # the runtime's image pins (vendor/e2b-embed)
DOMAIN_KEYS = ("E2B_PUBLIC_DOMAIN", "E2B_PUBLIC_SANDBOX_URL")
UNIT = "e2b-embed.service"


@dataclass(frozen=True)
class EmbedLayer:
    """An extension's part of Embed, which write_embed_env lays into its
    project (/opt/e2b/<name>/) and its .env. Applying a change recreates
    the runtime's containers (sbx-admin network apply)."""
    name: str
    env: Mapping[str, str] = field(default_factory=dict)     # its keys in Embed's .env; "" removes one
    compose: tuple[pathlib.Path, ...] = ()                    # compose files merged over Embed's, in order
    files: tuple[pathlib.Path, ...] = ()                      # what those compose files mount (./<name>/<file>)


def layers() -> list[EmbedLayer]:
    """What the enabled extensions add to Embed (Connector.embed)."""
    from ... import extensions
    return [layer for layer in (c.embed() for c in extensions.connectors()) if layer is not None]


def embed_env() -> dict[str, str]:
    """Every key sbx owns in Embed's .env, from the settings and the
    extensions (Connector.embed); "" removes a key. Embed needs a name for
    its sandbox URLs: a placeholder until the setup's Domain step."""
    from ... import hosted_services, settings
    s = settings.current()
    domain = s.domain.name or "sbx.invalid"
    found = layers()
    compose = [*EMBED_COMPOSE, *(f"{layer.name}/{path.name}" for layer in found for path in layer.compose)]
    return {"ALLOW_SANDBOX_INTERNAL_CIDRS": ",".join(hosted_services.allowed_cidrs()),
            "E2B_PUBLIC_DOMAIN": domain, "E2B_PUBLIC_SANDBOX_URL": f"https://sandbox.{domain}",
            "COMPOSE_FILE": ":".join(compose) if len(compose) > len(EMBED_COMPOSE) else "",
            **{key: value for layer in found for key, value in layer.env.items()}}


def render_embed_env(text: str, values: dict[str, str]) -> str:
    """text with values set over it, in place where a key is there already,
    appended where not, removed where the value is ""."""
    lines, seen = [], set()
    for line in text.splitlines():
        key = line.partition("=")[0]
        if re.fullmatch(r"#?[A-Z0-9_]+", key) and key.lstrip("#") in values:
            key = key.lstrip("#")
            if key in seen or values[key] == "":
                continue
            seen.add(key)
            line = f"{key}={values[key]}"
        lines.append(line)
    lines += [f"{key}={value}" for key, value in values.items() if key not in seen and value != ""]
    return "\n".join(lines) + "\n"


def write_embed_env(*, hugepages: int | None = None, pins: pathlib.Path | None = None,
                    keys: tuple[str, ...] | None = None, into: pathlib.Path | None = None) -> tuple[str, str]:
    """The one writer of Embed's .env: the keys sbx owns (embed_env, only
    `keys` of them when given), the hugepages (install.sh) and the runtime's
    image pins (the vendored .env) when given; the rest stays as Embed wrote
    it. Also lays the extensions' compose files into Embed's project, unless
    limited to `keys`. into: a copy of the project instead (recreates).
    (text before, text after)."""
    project = into or EMBED
    path = project / ".env"
    before = path.read_text() if path.exists() else ""
    values = embed_env()
    if keys is not None:
        values = {key: values[key] for key in keys}
    if hugepages is not None:
        values["HUGEPAGES"] = str(hugepages)
    if pins is not None:
        values.update((k, v) for k, _, v in (line.partition("=") for line in pins.read_text().splitlines())
                      if re.fullmatch(EMBED_PINS, k))
    for layer in layers() if keys is None else ():
        for source in layer.compose + layer.files:
            (project / layer.name).mkdir(mode=0o755, exist_ok=True)
            shutil.copy(source, project / layer.name / source.name)
    after = render_embed_env(before, values)
    if after != before:
        temporary = path.with_suffix(".sbx-new")
        temporary.touch(mode=0o600)
        temporary.write_text(after)
        temporary.replace(path)
    return before, after


def recreates(source: pathlib.Path, hugepages: int | None = None) -> list[str]:
    """The running services of Embed that installing the release in source
    would recreate, by compose's own test (a service's config hash): what
    Embed's project would recreate already, against its running containers,
    and what install.sh lays into it (the vendored compose, sbx's override,
    the .env keys sbx owns, the image pins, the hugepages), as a copy of the
    project before and after, both at one path so relative bind mounts
    hash alike. Raises when Docker does not answer."""
    import tempfile
    project = EMBED.name
    if not EMBED.is_dir():
        return []    # a first install
    listed = subprocess.run(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}", "--format",
                             '{{.Label "com.docker.compose.service"}} {{.Label "com.docker.compose.config-hash"}}'],
                            capture_output=True, text=True, check=True, timeout=60).stdout
    running = dict(line.split() for line in listed.splitlines() if len(line.split()) == 2)
    if not running:
        return []

    def hashes(folder: pathlib.Path) -> dict[str, str]:
        found = subprocess.run(["docker", "compose", "-p", project, "config", "--hash", "*"], cwd=folder,
                               capture_output=True, text=True, check=True, timeout=120).stdout
        return dict(line.split() for line in found.splitlines() if len(line.split()) == 2)
    pending = hashes(EMBED)
    vendored = source / "vendor/e2b-embed/compose"
    with tempfile.TemporaryDirectory(prefix="sbx-embed-check-") as directory:
        stage = pathlib.Path(directory) / project
        shutil.copytree(EMBED, stage, symlinks=True)
        before = hashes(stage)
        shutil.copytree(vendored, stage, symlinks=True, dirs_exist_ok=True,
                        ignore=lambda folder, names: [".env"] if pathlib.Path(folder) == vendored else [])
        shutil.copy(source / "embed/compose.override.yaml", stage / "compose.override.yaml")
        write_embed_env(hugepages=hugepages, pins=vendored / ".env", into=stage)
        after = hashes(stage)
    # A service the release drops stays as it runs (compose leaves orphans).
    return sorted(service for service, digest in running.items()
                  if pending.get(service, digest) != digest or after.get(service, before.get(service)) != before.get(service))


def units() -> list:
    """Embed's stack and its storage GC's timer (sbx_control.host installs them first)."""
    from ...host import SBIN, Unit, _service, _timer
    return [
        Unit(UNIT, _service(
            "E2B Embed stack", f"Type=oneshot\nRemainAfterExit=yes\nWorkingDirectory={EMBED}\n"
            "ExecStart=/usr/bin/docker compose up -d --wait\nTimeoutStartSec=30min",
            after="docker.service network-online.target",
            unit_extra="Requires=docker.service\nWants=network-online.target\n"), action="enable"),
        Unit("e2b-storage-gc.service", _service(
            "Delete template layers no build needs", f"Type=oneshot\nExecStart={SBIN}/e2b-storage-gc --delete",
            install=False), action=""),
        Unit("e2b-storage-gc.timer", _timer("Hourly template storage cleanup", "OnCalendar=hourly\nPersistent=true"),
             action="start"),
    ]


def commands(into) -> dict[str, str]:
    return {"e2b-storage-gc": into('venv/bin/python embed/storage-gc.py "$@"'),
            "e2b-compact-snapshot": into('venv/bin/python embed/compact-snapshot.py "$@"')}


def apply(key: str) -> bool:
    """A settings change's effect on Embed; False when key is not Embed's."""
    from ... import settings
    if key != settings.HostUnit.RUNTIME_ENV:
        return False
    # Only the domain: the rest (runtime-network, extensions) recreates the
    # runtime, which runtime.apply_network refuses under running sandboxes.
    write_embed_env(keys=DOMAIN_KEYS)
    subprocess.run(["docker", "compose", "up", "-d", "--wait"], cwd=EMBED, check=True)
    return True

