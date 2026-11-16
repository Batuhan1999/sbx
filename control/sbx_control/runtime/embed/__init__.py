"""E2B Embed on this host: Firecracker VMs that E2B's self-hosted stack runs
from /opt/e2b (vendor/e2b-embed). The only module of sbx that imports the
E2B SDK; configure points the SDK at Embed's API."""
from __future__ import annotations

import datetime
import os

from ..base import (Busy, Capabilities, CommandFailed, HostSide, Info, Limits, NoRoom, NotRunning, Snapshot,
                    TemplateSpec, Timeout)
from . import capacity, storage


def _state(value) -> str:
    return str(getattr(value, "value", value)).lower().rpartition(".")[2]


def _info(item) -> Info:
    return Info(sandbox_id=item.sandbox_id, metadata=item.metadata or {}, state=_state(item.state),
                template_id=getattr(item, "template_id", "") or "",
                started_at=_time(getattr(item, "started_at", None)), end_at=_time(getattr(item, "end_at", None)),
                cpu_count=getattr(item, "cpu_count", None), memory_mb=getattr(item, "memory_mb", None))


def _not_running(error) -> bool:
    """The SDK's answers for a sandbox that is not running: the API has no
    such running sandbox (404), or the proxy in front of it found none."""
    from e2b.exceptions import SandboxNotFoundException, SandboxNotRunningException
    return isinstance(error, (SandboxNotFoundException, SandboxNotRunningException))


def _call(function, *args, **kwargs):
    """function's result, a sandbox that is not running as NotRunning."""
    try:
        return function(*args, **kwargs)
    except Exception as error:
        if _not_running(error):
            raise NotRunning(str(error)) from error
        raise


def _time(value):
    return value if value is None or isinstance(value, datetime.datetime) else None


class _Commands:
    def __init__(self, commands):
        self._commands = commands

    def run(self, cmd, **kwargs):
        from e2b import TimeoutException
        from e2b.sandbox.commands.command_handle import CommandExitException
        try:
            return _call(self._commands.run, cmd, **kwargs)
        except CommandExitException as error:
            raise CommandFailed(str(error), stdout=error.stdout, stderr=error.stderr,
                                exit_code=error.exit_code) from error
        except TimeoutException as error:
            raise Timeout(str(error)) from error


class _Files:
    def __init__(self, files):
        self._files = files

    def write(self, path, data, **kwargs):
        return _call(self._files.write, path, data, **kwargs)

    def read(self, path, **kwargs):
        from e2b import TimeoutException
        try:
            return _call(self._files.read, path, **kwargs)
        except TimeoutException as error:
            raise Timeout(str(error)) from error


class _Handle:
    """An E2B sandbox object, its command errors as the runtime's."""

    def __init__(self, sandbox):
        self._sandbox = sandbox
        self.sandbox_id = sandbox.sandbox_id
        self.commands = _Commands(sandbox.commands)
        self.files = _Files(sandbox.files)

    def kill(self):
        return self._sandbox.kill()


CLIENT_PROXY_PORT = 3002    # Embed's client-proxy, which reaches <port>-<sandbox id> hosts
# The <port>-<id> host suffix client-proxy routes when there is no domain.
INTERNAL_HOST_SUFFIX = "e2b.sandboxes.internal"
ENVD_PORT = 49983           # E2B's control daemon in every sandbox; it checks its own access token
# Embed's PAUSE_ADMISSION_DISK_HEADROOM_MIB (vendor/e2b-embed compose.yaml): a
# pause needs room on disk for the sandbox's whole memory plus this.
PAUSE_DISK_HEADROOM_MB = 1024


class Embed(HostSide):
    kind = "embed"
    title = "E2B Embed"
    host = True
    capabilities = Capabilities(snapshots=True, private_networks=True, egress_rules=True)
    disk_path = str(storage.ROOT)
    ingress = f"http://127.0.0.1:{CLIENT_PROXY_PORT}"
    ingress_cleared = ("E2b-Sandbox-Id", "E2b-Sandbox-Port")   # client-proxy routes on them too
    internal_suffix = INTERNAL_HOST_SUFFIX
    ports = frozenset({ENVD_PORT})
    host_unit = "e2b-embed.service"
    credential_keys = ("E2B_API_KEY", "E2B_API_URL", "E2B_SANDBOX_URL", "E2B_HOST", "E2B_DOMAIN")
    secret_key = "E2B_API_KEY"

    def configure(self, secrets):
        """install.sh copies the key and URLs from Embed's sdk.env into runtime.env."""
        if "E2B_API_KEY" not in secrets or not ({"E2B_API_URL", "E2B_HOST"} & secrets.keys()):
            raise SystemExit("missing platform secrets: E2B_API_KEY and E2B_API_URL (/etc/sbx/runtime.env, written "
                             "by install.sh)")
        host = secrets.get("E2B_HOST", "")
        # The SDK reads these on every call; this is the one place that sets them.
        os.environ["E2B_API_KEY"] = secrets["E2B_API_KEY"]
        os.environ["E2B_API_URL"] = secrets.get("E2B_API_URL") or f"http://{host}:3000"
        # A sandbox. hostname makes client-proxy route by header (proxy/host.go).
        os.environ["E2B_SANDBOX_URL"] = secrets.get("E2B_SANDBOX_URL") or f"http://sandbox.{host}:{CLIENT_PROXY_PORT}"

    def upstream_host(self, port, sandbox_id):
        from ... import addressing
        return f"{port}-{sandbox_id}.{addressing.domain() or INTERNAL_HOST_SUFFIX}"

    def own_route(self, host, sandbox_id, port):
        """sandbox.<domain> is the E2B dashboard's header-routed host: the
        target is in the headers, so the same rules apply to it as to the
        preview hosts, but for envd, which checks its own token."""
        if host != dashboard_host():
            return None
        if port == str(ENVD_PORT):
            return True
        return int(port or 0), sandbox_id

    def create(self, template, *, timeout, envs=None, metadata=None, deny_out=None, on_timeout=None,
               auto_resume=None):
        from e2b import Sandbox
        options = {"timeout": timeout, "metadata": metadata}
        if envs is not None:
            options["envs"] = envs
        if deny_out is not None:
            options["network"] = {"deny_out": list(deny_out)}
        if on_timeout is not None:
            options["lifecycle"] = {"on_timeout": on_timeout,
                                    **({"auto_resume": auto_resume} if auto_resume is not None else {})}
        try:
            return _Handle(Sandbox.create(template, **options))
        except Exception as error:
            if "Failed to place sandbox" in str(error):
                raise NoRoom(str(error)) from error
            raise

    def connect(self, sandbox_id, *, timeout=None):
        from e2b import Sandbox
        return _Handle(_call(Sandbox.connect, sandbox_id, **({} if timeout is None else {"timeout": timeout})))

    def sandboxes(self, metadata=None, states=None):
        from e2b import Sandbox, SandboxQuery, SandboxState
        wanted = None if states is None else [SandboxState(_state(s)) for s in states]
        pages = Sandbox.list(query=SandboxQuery(metadata=metadata, state=wanted))
        items = pages.next_items()
        while pages.has_next:
            items += pages.next_items()
        return [_info(item) for item in items]

    def info(self, sandbox_id):
        from e2b import Sandbox
        return _info(_call(Sandbox.get_info, sandbox_id))

    def kill(self, sandbox_id):
        from e2b import Sandbox
        return Sandbox.kill(sandbox_id)

    def pause(self, sandbox_id):
        from e2b import Sandbox
        from e2b.exceptions import ServiceBusyException
        try:
            _call(Sandbox.pause, sandbox_id)
        except ServiceBusyException as error:
            raise Busy(str(error)) from error

    def set_timeout(self, sandbox_id, seconds):
        from e2b import Sandbox
        _call(Sandbox.set_timeout, sandbox_id, seconds)

    def snapshot(self, sandbox_id, name):
        from e2b import Sandbox
        return Snapshot(_call(Sandbox.create_snapshot, sandbox_id, name=name, mode="filesystem").snapshot_id)

    def delete_snapshot(self, snapshot_id):
        from e2b import Sandbox
        Sandbox.delete_snapshot(snapshot_id)

    def build_template(self, spec: TemplateSpec, *, alias, cpu, memory_mb, disk_mb, on_log=None):
        from e2b import Template, default_build_logger
        return Template.build(template(spec), alias=alias, cpu_count=cpu, memory_mb=memory_mb,
                              min_free_disk_mb=disk_mb, on_build_logs=on_log or default_build_logger())

    def templates(self):
        from e2b.api.client.api.templates import get_templates
        from e2b.api.client_sync import get_api_client
        from e2b.connection_config import ConnectionConfig
        response = get_templates.sync_detailed(client=get_api_client(ConnectionConfig()))
        if response.status_code >= 300 or not isinstance(response.parsed, list):
            raise RuntimeError(f"the runtime did not list its templates (HTTP {response.status_code})")
        return [(alias, str(t.build_status)) for t in response.parsed for alias in t.aliases]

    def delete_template(self, name):
        from e2b.api.client.api.templates import delete_templates_template_id
        from e2b.api.client_sync import get_api_client
        from e2b.connection_config import ConnectionConfig
        response = delete_templates_template_id.sync_detailed(name, client=get_api_client(ConnectionConfig()))
        if response.status_code == 404:
            return False
        if response.status_code >= 300:
            raise RuntimeError(f"the runtime refused to delete {name} (HTTP {response.status_code}): "
                               f"{response.content[:300].decode(errors='replace')}")
        return True

    def failure(self, error):
        return type(error).__module__.split(".")[0] in ("e2b", "httpx", "httpcore")

    def capacity(self, memory_mb):
        return capacity.room(memory_mb)

    def limits(self):
        """This host's: a VM has at most its cores and all of the hugepages
        that back sandbox memory. How many run at once is capacity()'s."""
        return Limits(cpu=os.cpu_count(), memory_mb=capacity.hugepages_mb())

    def placing(self):
        return capacity.placing()

    def apply_network(self):
        from . import network
        return network.apply()

    def disk(self):
        return storage.disk()

    def free_disk_mb(self):
        return storage.free_mb()

    def why_busy(self, memory_mb):
        free = storage.free_mb()
        if memory_mb and free is not None and free < memory_mb + PAUSE_DISK_HEADROOM_MB:
            return (f"the runtime's disk has {free / 1024:.1f} GB free and a pause needs room for the sandbox's "
                    f"whole memory ({memory_mb / 1024:.1f} GB) plus {PAUSE_DISK_HEADROOM_MB // 1024} GB; free disk "
                    "(delete sandboxes or templates nobody uses, sbx-admin template prune) and pause again")
        return ("the runtime is still saving the template or snapshot the sandbox started from, or another "
                "sandbox's snapshot; pause again in a minute")

    def snapshot_usage(self, sandbox_id):
        return storage.usage().get(sandbox_id) or {}

    def collect_storage(self):
        """Start the storage GC (e2b-storage-gc) now, on a host that runs Embed."""
        import subprocess
        if not storage.ROOT.exists():
            return
        try:
            subprocess.run(["systemctl", "start", "--no-block", "e2b-storage-gc.service"], capture_output=True,
                           timeout=10)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"storage GC: {error!r}", flush=True)

    def jobs(self):
        from ...jobs import Job
        usage = storage.USAGE
        return [
            Job("e2b-storage-gc", "storage", "Template storage cleanup",
                lambda: "Deletes template layers no build or live sandbox needs.", lambda: "hourly", "timer",
                runnable=True),
            Job("compact", "storage", "Snapshot compaction",
                lambda: "A paused sandbox's chain of pause snapshots folded into one build: "
                        "`e2b-compact-snapshot <sandbox id> --apply` on the host."
                        + (f" Usage per sandbox in {usage}." if usage.exists() else ""),
                lambda: "", "manual"),
        ]

    def preview(self, name, port, listen, *, preserve_host=False):
        from .proxy import serve
        serve(name, port, listen, preserve_host=preserve_host)

    def host_units(self):
        from . import host
        return host.units()

    def host_commands(self, into):
        from . import host
        return host.commands(into)

    def apply_host(self, key):
        from . import host
        return host.apply(key)

    def guard_host(self):
        from . import network
        network.guard()

    def readiness(self, facts):
        from ... import vault
        from ...connections.base import Check
        return [Check("Virtualization", facts.get("kvm", False),
                      "KVM is available" if facts.get("kvm") else "KVM is unavailable",
                      "The server needs /dev/kvm; rerun install.sh after enabling it."),
                Check("Runtime configured", bool(vault.runtime().get("E2B_API_KEY")),
                      "Sandbox runtime credentials", "Run install.sh to finish installing the runtime.")]

    def status(self):
        from . import connection
        return connection.status()

    def test(self):
        from . import connection
        return connection.test()


def dashboard_host() -> str:
    """sandbox.<domain>: the E2B dashboard's header-routed host."""
    from ... import addressing
    return f"sandbox.{addressing.domain()}"


def template(spec: TemplateSpec):
    """The E2B template builder for spec, step by step."""
    from e2b import Template, wait_for_port
    from e2b.template.dockerfile_parser import parse_dockerfile
    t = Template()
    for what, value in spec.steps:
        if what == "image":
            t = t.from_image(value)
        elif what == "template":
            t = t.from_template(value)
        elif what == "user":
            t = t.set_user(value)
        elif what == "workdir":
            t = t.set_workdir(value)
        elif what == "env":
            t = t.set_envs(value)
        elif what == "run":
            t = t.run_cmd(value)
        elif what == "dockerfile":
            parse_dockerfile(value, t)
        elif what == "start":
            command, ready = value
            t = t.set_start_cmd(command, wait_for_port(ready) if isinstance(ready, int) else ready)
        else:
            raise ValueError(f"unknown template step {what}")
    return t

