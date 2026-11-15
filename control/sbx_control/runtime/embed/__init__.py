"""E2B Embed on this host: Firecracker VMs that E2B's self-hosted stack runs
from /opt/e2b (vendor/e2b-embed). The only module of sbx that imports the
E2B SDK; platform.connect points the SDK at Embed's API."""
from __future__ import annotations

import datetime

from ..base import Busy, Capabilities, CommandFailed, Info, Snapshot, TemplateSpec, Timeout


def _state(value) -> str:
    return str(getattr(value, "value", value)).lower().rpartition(".")[2]


def _info(item) -> Info:
    return Info(sandbox_id=item.sandbox_id, metadata=item.metadata or {}, state=_state(item.state),
                template_id=getattr(item, "template_id", "") or "",
                started_at=_time(getattr(item, "started_at", None)), end_at=_time(getattr(item, "end_at", None)),
                cpu_count=getattr(item, "cpu_count", None), memory_mb=getattr(item, "memory_mb", None))


def _time(value):
    return value if value is None or isinstance(value, datetime.datetime) else None


class _Commands:
    def __init__(self, commands):
        self._commands = commands

    def run(self, cmd, **kwargs):
        from e2b import TimeoutException
        from e2b.sandbox.commands.command_handle import CommandExitException
        try:
            return self._commands.run(cmd, **kwargs)
        except CommandExitException as error:
            raise CommandFailed(str(error), stdout=error.stdout, stderr=error.stderr,
                                exit_code=error.exit_code) from error
        except TimeoutException as error:
            raise Timeout(str(error)) from error


class _Files:
    def __init__(self, files):
        self._files = files

    def write(self, path, data, **kwargs):
        return self._files.write(path, data, **kwargs)

    def read(self, path, **kwargs):
        from e2b import TimeoutException
        try:
            return self._files.read(path, **kwargs)
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


class Embed:
    kind = "embed"
    title = "E2B Embed"
    capabilities = Capabilities(snapshots=True, private_networks=True, memory_pause=True)

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
        return _Handle(Sandbox.create(template, **options))

    def connect(self, sandbox_id, *, timeout=None):
        from e2b import Sandbox
        return _Handle(Sandbox.connect(sandbox_id) if timeout is None else Sandbox.connect(sandbox_id, timeout=timeout))

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
        return _info(Sandbox.get_info(sandbox_id))

    def kill(self, sandbox_id):
        from e2b import Sandbox
        return Sandbox.kill(sandbox_id)

    def pause(self, sandbox_id):
        from e2b import Sandbox
        from e2b.exceptions import ServiceBusyException
        try:
            Sandbox.pause(sandbox_id)
        except ServiceBusyException as error:
            raise Busy(str(error)) from error

    def set_timeout(self, sandbox_id, seconds):
        from e2b import Sandbox
        Sandbox.set_timeout(sandbox_id, seconds)

    def snapshot(self, sandbox_id, name):
        from e2b import Sandbox
        return Snapshot(Sandbox.create_snapshot(sandbox_id, name=name, mode="filesystem").snapshot_id)

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

