"""The Amp connector: Amp's self-hosted orb provider runs threads in
sandboxes of their own. A paused orb is never deleted for its age: its disk
is its thread's state."""
from __future__ import annotations

import contextlib
import fcntl
import gzip
import io
import os
import pathlib
import subprocess
import tarfile
import time
import zlib

from pydantic import ValidationError

from ... import core, extensions, host, howto, paths, settings, vault
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


def tools_cache() -> pathlib.Path:
    """Where the host keeps the desktop tools, one tarball per image digest."""
    return paths.cache() / "amp-tools"


def cached_tools() -> pathlib.Path:
    return tools_cache() / f"{IMAGE.rpartition('@sha256:')[2]}.tar.gz"


def tools_runtime() -> str | None:
    """The enabled runtime that fetches the tools from IMAGE: one that builds
    from container images, the default when it does."""
    from ... import runtime
    return runtime.able("image_import")


def no_tools_runtime() -> str:
    from ... import runtime
    importers = []
    for kind in runtime.kinds():
        try:
            backend = runtime.make(kind)
        except Exception:   # a runtime whose package can't load here can't be suggested
            continue
        if backend.capabilities.image_import:
            importers.append(backend.title)
    which = f" ({', '.join(importers)})" if importers else ""
    return ("Amp's desktop tools come from its container image, and no enabled sandbox runtime builds templates from "
            f"one. Enable a runtime that does{which} and build again: the tools are then kept on this host for "
            "every runtime.")


# What a template's READY check runs from the tools: a tarball without them
# would build templates that never become ready.
TOOL_FILES = ("amp-wayland/bin/labwc", "amp-wayland/bin/waymote-gateway", "amp-wayland/libexec/waymote-streamd")


def tools_valid(data: bytes) -> bool:
    """Whether data is a whole gzipped tarball holding the files READY needs,
    each executable. Streamed, so the expanded archive is never held whole;
    the gzip stream is then read to its end, which checks its trailer."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                modes = {m.name.removeprefix("./"): m.mode for m in tar if m.isfile()}
            while stream.read(1 << 20):   # past tar's end-of-archive marker, through the trailer
                pass
    except (OSError, EOFError, zlib.error, tarfile.TarError):
        return False
    return all(f in modes and modes[f] & 0o111 for f in TOOL_FILES)


def wayland_tools() -> bytes:
    """The tarball of /opt/amp-wayland from IMAGE: the host's copy for its
    digest, else (or when that copy is damaged) fetched once on a runtime
    that imports images and kept, dropping the copies of other digests."""
    found = cached_tools()
    found.parent.mkdir(parents=True, exist_ok=True)
    with (found.parent / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # builds on several runtimes fetch once
        data = found.read_bytes() if found.is_file() else b""
        if not tools_valid(data):
            kind = tools_runtime()
            if kind is None:
                raise RuntimeError(no_tools_runtime())
            data = _fetch_tools(kind)
            if not tools_valid(data):
                raise RuntimeError(f"the desktop tools fetched from {IMAGE} are not a gzipped amp-wayland tarball")
            partial = found.with_name(found.name + ".partial")
            partial.write_bytes(data)
            os.replace(partial, found)
        for other in found.parent.iterdir():
            if other not in (found, found.parent / ".lock"):
                other.unlink(missing_ok=True)
    return data


def _fetch_tools(kind: str) -> bytes:
    from ... import runtime
    name = runtime.template_name(TOOLS, kind)   # this installation's on a runtime several may share
    runtime.build_template(runtime.TemplateSpec().from_image(IMAGE).set_start_cmd("sleep infinity", "true"),
                           alias=name, cpu=1, memory_mb=1024, disk_mb=1024, kind=kind)
    vm, primary = None, None
    try:
        try:
            with core.reserve_capacity(1024, kind):
                vm = runtime.create(name, timeout=180, metadata={"sbx_cache_build": TOOLS}, on_timeout="kill",
                                    kind=kind)
            vm.commands.run("tar -C /opt -czf /tmp/sbx-amp-tools.tar.gz amp-wayland", timeout=60)
            data = vm.files.read("/tmp/sbx-amp-tools.tar.gz", format="bytes")
        finally:
            if vm is not None:
                runtime.kill(vm.sandbox_id)
    except BaseException as error:   # the error this fetch ends with, which a failed delete must not replace
        primary = error
        raise
    finally:
        try:   # the host keeps the tools; the template would only hold the runtime's storage
            runtime.delete_template(name, kind)
        except Exception as error:   # a runtime's refusal must not cost the tools already read
            if primary is None and not (isinstance(error, RuntimeError) or _refused_by(kind, error)):
                raise
            with contextlib.suppress(OSError):   # a diagnostic only: it must cost neither the tools nor the error
                print(f"could not delete the {name} template on {kind}: {error}", flush=True)
    if len(data) > 64 * 1024 * 1024:
        raise RuntimeError("Amp desktop tools exceed the expected build artifact size")
    return data


def _refused_by(kind: str, error: BaseException) -> bool:
    """Whether the runtime that fetched the tools counts error as its own
    failure; asked of that runtime alone, so another enabled runtime that
    can't load here can't replace the error."""
    from ... import runtime
    try:
        return isinstance(error, runtime.Failed) or runtime.get(kind).failure(error)
    except Exception:
        return False


class Amp(extensions.Connector):
    kind = KIND
    title = "Amp"
    help_url = howto.AMP_PROVIDER
    section = Settings
    choosable = True

    def choose(self, on: bool, *, by: str):
        """Its layer in every template from the first build on; the provider runs once its token is added."""
        if self.settings().chosen != on:
            self.save_settings({**self.settings().model_dump(), "chosen": on}, by=by)

    @property
    def cli(self):
        from .orbs import main
        return main

    def migrate(self, conn):
        conn.executescript(SCHEMA)
        from ...state import columns
        if "runtime" not in columns(conn, "amp_snapshots"):
            conn.execute("alter table amp_snapshots add column runtime text not null default ''")
        # Caches from before several runtimes are on the one runtime there was then.
        from ... import runtime
        conn.execute("update amp_snapshots set runtime = ? where runtime = ''", (runtime.LEGACY,))

    def ready(self) -> Ready:
        return Ready(READY)

    def contribution(self, target) -> Contribution | None:
        if not (self.settings().enabled or self.settings().chosen):
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
        summary = {"not_connected": "In every template; add an orb provider token from Amp Settings → Orbs"
                   if amp.chosen else "Add an orb provider token from Amp Settings → Orbs",
                   "off": "Provider stopped; existing orb disks are retained",
                   "attention": "Provider configured; check its service and Amp connection",
                   "connected": "Provider running · verify its connection in Amp Settings → Orbs"}[state]
        return Status(self.kind, self.title, state, summary, {}, (
            Field("provider_token", "Orb provider token", secret=True, set=token.set,
                  help="From Amp Settings → Orbs → Add Provider; stored in the secret vault"),
            Field("repo", "Default repository", value=amp.repo,
                  help="owner/repository; with multiple repos, Amp orb types use the routes shown by sbx-admin amp routes"),
            # Chosen in the setup counts as on: saving the card or the keys page with the
            # token keeps Amp in the templates and starts its provider, instead of dropping it.
            Field("enabled", "Enabled", value="true" if amp.enabled or amp.chosen else "false",
                  help="Enabled, Amp is in every template; templates rebuild by themselves"),
        ), ("connect", "test", "disconnect"))

    def test(self, values: dict | None = None) -> list[Check]:
        token = bool((values or {}).get("provider_token")) or vault.facts(TOKEN).set
        return [Check("provider token", token, "set" if token else "not set", "add a provider in Amp Settings → Orbs",
                      help_url=howto.AMP_PROVIDER),
                Check("Amp CLI", CLI.is_file(), "installed" if CLI.is_file() else "missing", "save the enabled connection"),
                self._tools_check(),
                Check("provider service", active(), "check connection status in Amp Settings → Orbs",
                      "journalctl -u sbx-amp; verify the token and outbound HTTPS", help_url=howto.AMP_CONNECTION)]

    @staticmethod
    def _tools_check() -> Check:
        try:
            kept = cached_tools().read_bytes()
        except IsADirectoryError:   # a refill can't replace a directory with the archive
            return Check("desktop tools", False, f"{cached_tools()} is a directory",
                         f"move or remove {cached_tools()}; the next template build fetches the tools again")
        except FileNotFoundError:   # never fetched, or cleared since
            kept = b""
        if tools_valid(kept):
            return Check("desktop tools", True, "kept on this host")
        kind = tools_runtime()
        if kind is None:
            return Check("desktop tools", False, "no enabled runtime builds from container images", no_tools_runtime())
        from ... import runtime
        return Check("desktop tools", True, f"fetched on {runtime.title(kind)} at the next template build")

    def connect(self, values: dict, *, by: str) -> Status:
        amp = self.settings().model_dump()
        amp["enabled"] = str(values.get("enabled", "true")).lower() in ("true", "1", "on", "yes")
        if not amp["enabled"]:   # off: out of the templates too, as before it was chosen
            amp["chosen"] = False
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
        self.save_settings({**self.settings().model_dump(), "enabled": False, "chosen": False}, by=by)
        return self.status()
