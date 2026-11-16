"""The Amp connector: Amp's self-hosted orb provider runs threads in
sandboxes of their own. A paused orb is never deleted for its age: its disk
is its thread's state."""
from __future__ import annotations

import pathlib
import subprocess
import time

from pydantic import ValidationError

from ... import core, extensions, host, settings, vault
from ...connections.base import Check, Field, Refused, Status
from ...contributions import Artifact, Check as Ready, Contribution, TemplateLayer
from .common import CLI, KIND, Settings, TOKEN

# Amp's desktop tools come from its orb image, pinned so a template's
# fingerprint (the artifact's label) changes when the image does.
IMAGE = ("us-central1-docker.pkg.dev/amp-prod-0/amp-orb/amp-orb"
         "@sha256:199463fbe20651217164c887ba5178cb5c79607b51ed96e639649580b9593b67")
TOOLS = "sbx-amp-tools"
CHECKPOINTS = pathlib.Path(__file__).parent / "checkpoints"
SCHEMA = """
create table if not exists amp_snapshots (
    key text primary key, repo text not null, snapshot_id text not null,
    source_template text not null, created_at text not null default current_timestamp);
"""
APT = ("iptables", "xwayland", "dbus", "libglib2.0-bin", "gsettings-desktop-schemas", "xsettingsd", "wlr-randr",
       "libegl1", "libegl-mesa0", "libgles2", "libgl1-mesa-dri", "libseat1", "libinput10", "libxcb-composite0",
       "libxcb-ewmh2", "libxcb-icccm4", "libxcb-res0", "pulseaudio", "ffmpeg", "fonts-dejavu-core")
READY = ("command -v amp && command -v gsettings && command -v xsettingsd && test -x /opt/amp-wayland/bin/labwc && "
         "test -x /opt/amp-wayland/bin/waymote-gateway && test -x /opt/amp-wayland/libexec/waymote-streamd && "
         "LD_LIBRARY_PATH=/opt/amp-wayland/lib/x86_64-linux-gnu ldd /opt/amp-wayland/bin/labwc > /tmp/sbx-amp-libraries "
         "&& ! grep -q 'not found' /tmp/sbx-amp-libraries")
_active: dict = {"at": 0.0, "value": False}


def active() -> bool:
    if time.monotonic() - _active["at"] > 30:
        try:
            value = subprocess.run(["systemctl", "is-active", "--quiet", "sbx-amp"],
                                   capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            value = False
        _active.update(at=time.monotonic(), value=value)
    return _active["value"]


def wayland_tools() -> bytes:
    from ... import runtime
    runtime.build_template(runtime.TemplateSpec().from_image(IMAGE).set_start_cmd("sleep infinity", "true"),
                           alias=TOOLS, cpu=1, memory_mb=1024, disk_mb=1024)
    vm = None
    try:
        with core.reserve_capacity(1024):
            vm = runtime.create(TOOLS, timeout=180, metadata={"sbx_cache_build": TOOLS}, on_timeout="kill")
        vm.commands.run("tar -C /opt -czf /tmp/sbx-amp-tools.tar.gz amp-wayland", timeout=60)
        data = vm.files.read("/tmp/sbx-amp-tools.tar.gz", format="bytes")
    finally:
        if vm is not None:
            runtime.kill(vm.sandbox_id)
    if len(data) > 64 * 1024 * 1024:
        raise RuntimeError("Amp desktop tools exceed the expected build artifact size")
    return data


class Amp(extensions.Connector):
    kind = KIND
    title = "Amp"
    section = Settings

    @property
    def cli(self):
        from .orbs import main
        return main

    def migrate(self, conn):
        conn.executescript(SCHEMA)
        from ...state import columns
        if "runtime" not in columns(conn, "amp_snapshots"):  # '': the installation's default runtime
            conn.execute("alter table amp_snapshots add column runtime text not null default ''")

    def ready(self) -> Ready:
        return Ready(READY)

    def contribution(self, target) -> Contribution | None:
        if not self.settings().enabled:
            return None
        return Contribution(
            "amp",
            template=TemplateLayer(
                apt=APT,
                root=("curl -fsSL https://ampcode.com/install.sh -o /tmp/sbx-install-amp && "
                      f"AMP_VERSION={self.settings().cli_version} HOME=/root AMP_HOME=/opt/sbx-amp bash /tmp/sbx-install-amp && "
                      "ln -sfn /opt/sbx-amp/bin/amp /usr/local/bin/amp && rm /tmp/sbx-install-amp",),
                artifacts=(Artifact(f"amp-wayland from {IMAGE}", wayland_tools, "/opt"),)),
            ready=self.ready(),
            scrub_paths=("$HOME/.amp", "$HOME/.config/amp", "$HOME/.local/share/amp", "$HOME/.cache/amp",
                         "$HOME/.local/state/amp"))

    def router(self):
        from . import api
        return api.router

    def keeper_policy(self, info, use: extensions.Use) -> extensions.Policy:
        running = self.settings().enabled and active()
        return extensions.Policy(keep_awake=running, decided=True, keep_paused=True)

    def before_pause(self, info):
        from ... import runtime
        from .orbs import stop_bootstrap
        stop_bootstrap(runtime.connect(info.sandbox_id))

    def on_resume(self, sbx, info):
        """A resume that was not a spawn (a preview woke it) must not leave an
        older snapshot's bootstrap retrying its rejected token."""
        from .orbs import stop_bootstrap
        stop_bootstrap(sbx)

    def origin(self, info) -> dict:
        return {"kind": "session", "label": "Amp thread", "detail": (info.metadata or {}).get("orbId", ""), "href": ""}

    def host_services(self) -> list[extensions.HostService]:
        root = host.CURRENT
        prepare = (f"[ -x {CLI} ] || {{ installer=$(mktemp /tmp/sbx-install-amp.XXXXXX) && "
                   f"curl -fsSL https://ampcode.com/install.sh -o \"$installer\" && "
                   f"AMP_VERSION={self.settings().cli_version} HOME=/root AMP_HOME={CLI.parents[1]} bash \"$installer\" >/dev/null; "
                   "status=$?; rm -f \"$installer\"; exit $status; }")
        return [extensions.HostService(
            "sbx-amp", "Amp self-hosted orb provider",
            f"WorkingDirectory={root}\nExecStart={root}/venv/bin/python -m sbx_control.connectors.amp provider\n"
            "Environment=HOME=/root\nRestart=on-failure\nRestartSec=15\nTimeoutStopSec=300\nUMask=0077",
            enabled=self.settings().enabled, apply=Settings.applies[0], prepare=prepare)]

    def runtime_layer(self, kind):
        """Embed built with filesystem-only snapshots, when the settings name it."""
        from ...runtime import RuntimeLayer
        if kind != "embed":
            return None
        amp = self.settings()
        on = bool(amp.checkpoint_api_image)
        return RuntimeLayer(
            KIND, env={"SBX_CHECKPOINT_API_IMAGE": amp.checkpoint_api_image,
                       "SBX_ORCHESTRATOR_BINARY": amp.orchestrator_binary},
            compose=(CHECKPOINTS / "compose.yaml",) if on else (),
            files=(CHECKPOINTS / "orchestrator-launch.sh",) if on else ())

    def status(self) -> Status:
        amp = self.settings()
        token = vault.facts(TOKEN)
        state = "not_connected" if not token.set else "off" if not amp.enabled else "attention"
        if token.set and amp.enabled and active():
            state = "connected"
        summary = {"not_connected": "Add an orb provider token from Amp Settings → Orbs",
                   "off": "Provider stopped; existing orb disks are retained",
                   "attention": "Provider configured; check its service and Amp connection",
                   "connected": "Provider running · verify its connection in Amp Settings → Orbs"}[state]
        return Status(self.kind, self.title, state, summary, {}, (
            Field("provider_token", "Orb provider token", secret=True, set=token.set,
                  help="From Amp Settings → Orbs → Add Provider; stored in the secret vault"),
            Field("repo", "Default repository", value=amp.repo,
                  help="owner/repository; with multiple repos, Amp orb types use the routes shown by sbx-admin amp routes"),
            Field("enabled", "Enabled", value="true" if amp.enabled else "false",
                  help="Enabled, Amp is in every template; templates rebuild by themselves"),
        ), ("connect", "test", "disconnect"))

    def test(self, values: dict | None = None) -> list[Check]:
        token = bool((values or {}).get("provider_token")) or vault.facts(TOKEN).set
        return [Check("provider token", token, "set" if token else "not set", "add a provider in Amp Settings → Orbs"),
                Check("Amp CLI", CLI.is_file(), "installed" if CLI.is_file() else "missing", "save the enabled connection"),
                Check("provider service", active(), "check connection status in Amp Settings → Orbs",
                      "journalctl -u sbx-amp; verify the token and outbound HTTPS")]

    def connect(self, values: dict, *, by: str) -> Status:
        amp = self.settings().model_dump()
        amp["enabled"] = str(values.get("enabled", "true")).lower() in ("true", "1", "on", "yes")
        if "repo" in values:
            amp["repo"] = str(values["repo"]).strip()
        try:
            validated = Settings.model_validate(amp)
        except ValidationError:
            raise Refused("enter a GitHub owner/repository")
        if validated.repo and settings.trust(validated.repo) != "trusted":
            raise Refused("connect and prepare that repository in sbx first")
        token = str(values.get("provider_token") or "").strip()
        if token and (not token.startswith("aop_live_") or len(token) < 16 or any(c.isspace() for c in token)):
            raise Refused("enter the orb provider token from Amp Settings → Orbs")
        if validated.enabled and not (token or vault.facts(TOKEN).set):
            raise Refused("add an orb provider token before enabling Amp")
        if token:  # a rotation restarts the provider even when the settings stay: the key asks for it
            vault.put(TOKEN, token, by=by)
        self.save_settings(validated, by=by)
        return self.status()

    def disconnect(self, *, by: str) -> Status:
        vault.delete(TOKEN, by=by)
        self.save_settings({**self.settings().model_dump(), "enabled": False}, by=by)
        return self.status()
