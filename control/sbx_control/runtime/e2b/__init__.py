"""What the E2B-based backends share (E2B Embed on this host, E2B Cloud):
the compute part of the contract over the E2B SDK, its handles and errors as
the runtime's, listings as Info and a TemplateSpec as an E2B Template.

Each backend keeps its own connection (api_key, api_url, sandbox_url,
domain: ConnectionConfig's options) on the instance and passes it to every
SDK call, so two of them run side by side in one process. Nothing goes in
the process environment, where the SDK would read it for every backend."""
from __future__ import annotations

import datetime

from ..base import Backend, Busy, CommandFailed, Info, NoRoom, NotRunning, Snapshot, TemplateGone, TemplateSpec, Timeout

DEFAULT_DOMAIN = "e2b.app"   # the SDK's own default: E2B Cloud
ENVD_PORT = 49983            # E2B's control daemon in every sandbox; it checks its own access token
# ConnectionConfig's options a backend may set; any other is refused.
CONNECTION_OPTIONS = ("api_key", "api_url", "sandbox_url", "domain")


def _state(value) -> str:
    return str(getattr(value, "value", value)).lower().rpartition(".")[2]


def _time(value):
    return value if value is None or isinstance(value, datetime.datetime) else None


def as_info(item) -> Info:
    """A listed or looked-up sandbox as the runtime's Info."""
    return Info(sandbox_id=item.sandbox_id, metadata=item.metadata or {}, state=_state(item.state),
                template_id=getattr(item, "template_id", "") or "",
                started_at=_time(getattr(item, "started_at", None)), end_at=_time(getattr(item, "end_at", None)),
                cpu_count=getattr(item, "cpu_count", None), memory_mb=getattr(item, "memory_mb", None))


def _not_running(error) -> bool:
    """The SDK's answers for a sandbox that is not running: the API has no
    such running sandbox (404), or the proxy in front of it found none."""
    from e2b.exceptions import SandboxNotFoundException, SandboxNotRunningException
    return isinstance(error, (SandboxNotFoundException, SandboxNotRunningException))


def _template_missing(error) -> bool:
    """The API's answer to a create from a template (or snapshot) it does not
    have: a 404 whose message names the template, e.g. "404: template 'x' not
    found" (handle_api_exception: a SandboxException carrying status_code)."""
    from e2b.exceptions import SandboxException
    return (isinstance(error, SandboxException) and getattr(error, "status_code", None) == 404
            and "template" in str(error).lower())


def call(function, *args, **kwargs):
    """function's result, a sandbox that is not running as NotRunning."""
    try:
        return function(*args, **kwargs)
    except Exception as error:
        if _not_running(error):
            raise NotRunning(str(error)) from error
        raise


class _Commands:
    def __init__(self, commands):
        self._commands = commands

    def run(self, cmd, **kwargs):
        from e2b import TimeoutException
        from e2b.sandbox.commands.command_handle import CommandExitException
        try:
            return call(self._commands.run, cmd, **kwargs)
        except CommandExitException as error:
            raise CommandFailed(stdout=error.stdout, stderr=error.stderr, exit_code=error.exit_code,
                                command=cmd) from error
        except TimeoutException as error:
            raise Timeout(str(error)) from error


class _Files:
    def __init__(self, files):
        self._files = files

    def write(self, path, data, **kwargs):
        return call(self._files.write, path, data, **kwargs)

    def read(self, path, **kwargs):
        from e2b import TimeoutException
        try:
            return call(self._files.read, path, **kwargs)
        except TimeoutException as error:
            raise Timeout(str(error)) from error


class Handle:
    """An E2B sandbox object, its command errors as the runtime's. The SDK
    keeps the connection it was made with for its own calls."""

    def __init__(self, sandbox, on_kill=None):
        self._sandbox = sandbox
        self._on_kill = on_kill    # the backend forgets what it kept of the sandbox (its token)
        self.sandbox_id = sandbox.sandbox_id
        self.commands = _Commands(sandbox.commands)
        self.files = _Files(sandbox.files)

    @property
    def traffic_access_token(self) -> str:
        """The token the provider answers the sandbox's ports with ("" when none)."""
        token = getattr(self._sandbox, "traffic_access_token", None)
        return token if isinstance(token, str) else ""

    def kill(self):
        if self._on_kill:
            self._on_kill()
        return self._sandbox.kill()


class Compute(Backend):
    """The compute part of the contract over the E2B SDK, every call with
    this backend's own connection (connect_with, from configure)."""
    # What every create's network has beneath its deny_out (E2B Cloud: its
    # ports answer only with the sandbox's traffic token).
    network_defaults: dict = {}

    def __init__(self):
        self._connection: dict = {}

    def connect_with(self, **options) -> None:
        """The connection every SDK call of this backend uses: ConnectionConfig's
        options, None or "" left out (the SDK's default)."""
        unknown = set(options) - set(CONNECTION_OPTIONS)
        if unknown:
            raise ValueError(f"not an E2B connection option: {', '.join(sorted(unknown))}")
        self._connection = {k: v for k, v in options.items() if v}

    def connection(self) -> dict:
        """What every SDK call of this backend passes."""
        return dict(self._connection)

    def _handle(self, sandbox) -> Handle:
        return Handle(sandbox)

    def create(self, template, *, timeout, envs=None, metadata=None, deny_out=None, on_timeout=None,
               auto_resume=None):
        from e2b import Sandbox
        options = {"timeout": timeout, "metadata": metadata}
        if envs is not None:
            options["envs"] = envs
        network = {**self.network_defaults, **({"deny_out": list(deny_out)} if deny_out is not None else {})}
        if network:
            options["network"] = network
        if on_timeout is not None:
            options["lifecycle"] = {"on_timeout": on_timeout,
                                    **({"auto_resume": auto_resume} if auto_resume is not None else {})}
        try:
            return self._handle(Sandbox.create(template, **options, **self._connection))
        except Exception as error:
            if "Failed to place sandbox" in str(error):
                raise NoRoom(str(error)) from error
            if _template_missing(error):
                raise TemplateGone(template, str(error)) from error
            raise

    def connect(self, sandbox_id, *, timeout=None):
        from e2b import Sandbox
        return self._handle(call(Sandbox.connect, sandbox_id, **({} if timeout is None else {"timeout": timeout}),
                           **self._connection))

    def sandboxes(self, metadata=None, states=None):
        from e2b import Sandbox, SandboxQuery, SandboxState
        wanted = None if states is None else [SandboxState(_state(s)) for s in states]
        pages = Sandbox.list(query=SandboxQuery(metadata=metadata, state=wanted), **self._connection)
        items = pages.next_items()
        while pages.has_next:
            items += pages.next_items()
        return [as_info(item) for item in items]

    def info(self, sandbox_id):
        from e2b import Sandbox
        return as_info(call(Sandbox.get_info, sandbox_id, **self._connection))

    def kill(self, sandbox_id):
        from e2b import Sandbox
        return Sandbox.kill(sandbox_id, **self._connection)

    def pause(self, sandbox_id):
        from e2b import Sandbox
        from e2b.exceptions import ServiceBusyException
        try:
            call(Sandbox.pause, sandbox_id, **self._connection)
        except ServiceBusyException as error:
            raise Busy(str(error)) from error

    def set_timeout(self, sandbox_id, seconds):
        from e2b import Sandbox
        call(Sandbox.set_timeout, sandbox_id, seconds, **self._connection)

    def snapshot(self, sandbox_id, name):
        from e2b import Sandbox
        return Snapshot(call(Sandbox.create_snapshot, sandbox_id, name=name, mode="filesystem",
                             **self._connection).snapshot_id)

    def delete_snapshot(self, snapshot_id):
        from e2b import Sandbox
        Sandbox.delete_snapshot(snapshot_id, **self._connection)

    def build_template(self, spec: TemplateSpec, *, alias, cpu, memory_mb, disk_mb, on_log=None):
        from e2b import Template, default_build_logger
        return Template.build(template(spec), alias=alias, cpu_count=cpu, memory_mb=memory_mb,
                              min_free_disk_mb=disk_mb, on_build_logs=on_log or default_build_logger(),
                              **self._connection)

    def _client(self):
        from e2b.api.client_sync import get_api_client
        from e2b.connection_config import ConnectionConfig
        return get_api_client(ConnectionConfig(**self._connection))

    def templates(self):
        from e2b.api.client.api.templates import get_templates
        response = get_templates.sync_detailed(client=self._client())
        if response.status_code >= 300 or not isinstance(response.parsed, list):
            raise RuntimeError(f"the runtime did not list its templates (HTTP {response.status_code})")
        return [(alias, str(t.build_status)) for t in response.parsed for alias in t.aliases]

    def delete_template(self, name):
        from e2b.api.client.api.templates import delete_templates_template_id
        response = delete_templates_template_id.sync_detailed(name, client=self._client())
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
