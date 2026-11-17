"""What sbx asks of the sandbox runtime: the compute under the orchestration.

A runtime makes, lists, pauses and deletes VMs, runs commands and writes
files in them, and builds the templates they start from. Everything else
(recipes, the lifecycle, env, services, previews, extensions) is sbx's
and the same on every runtime. Results are plain values: a sandbox's
state is RUNNING or PAUSED, its times are datetimes.

An installation runs several runtimes at once (sbx_control.runtime is the
registry), so a backend keeps no process-global configuration: nothing it is
configured with may be where another backend reads it. Its credentials come
to configure() as its own keys of /etc/sbx/runtime.env (credential_keys, a
name no other backend uses) and stay on the instance; it passes them to its
SDK per call. The E2B-based backends (Embed, E2B Cloud) share one SDK, which
would read E2B_* from the process environment: they never write it, each
passes its own connection on every call (runtime.e2b).

The contract has two parts. Every backend implements the compute part
(Runtime: create, list, connect, templates, ...). The host part (systemd
units, the host's firewall, disk, capacity, readiness) is for a runtime that
runs on this host; HostSide gives every member of it a no-op, so a hosted
backend inherits it and implements nothing it does not have."""
from __future__ import annotations

import contextlib
import dataclasses
import datetime
import pathlib
from typing import Any, Callable, ContextManager, Mapping, Protocol

RUNNING, PAUSED = "running", "paused"


class Failed(Exception):
    """The runtime or the connection to it failed: it answered with an error
    or not at all."""


class CommandFailed(Failed):
    """A command in a sandbox exited with a code other than 0."""

    def __init__(self, message: str = "", *, stdout: str = "", stderr: str = "", exit_code: int = 1):
        super().__init__(message or f"Command exited with code {exit_code} and error:\n{stderr}")
        self.stdout, self.stderr, self.exit_code = stdout, stderr, exit_code


class Timeout(Failed):
    """A command or a call to the runtime ran out of time."""


class NotRunning(Failed):
    """The sandbox is not running now: the runtime paused or deleted it (on
    its own timeout, say) since it was listed. The next listing says which."""


class TemplateGone(Failed):
    """create found no template (or snapshot) of that name: it was deleted
    behind sbx's back. name is the one asked for."""

    def __init__(self, name: str, message: str = ""):
        super().__init__(message or f"the runtime has no template {name}")
        self.name = name


class Busy(Failed):
    """The runtime would not pause the sandbox now; it keeps running."""


class NoRoom(Failed):
    """The runtime has no room for another sandbox now."""


@dataclasses.dataclass(frozen=True)
class Info:
    """A sandbox as the runtime lists it."""
    sandbox_id: str
    metadata: dict
    state: str                                 # RUNNING or PAUSED
    template_id: str = ""
    started_at: datetime.datetime | None = None
    end_at: datetime.datetime | None = None    # when it pauses (running) or paused (paused)
    cpu_count: int | None = None
    memory_mb: int | None = None
    runtime: str = ""                          # its runtime's kind, as the registry tags it


@dataclasses.dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    runtime: str = ""    # the kind of the runtime that holds it, as the registry tags it


@dataclasses.dataclass(frozen=True)
class Capabilities:
    """What a runtime can do beyond the basics; extensions and settings that
    need one check it."""
    snapshots: bool = False         # filesystem snapshots a new sandbox can start from
    # Sandboxes reach private ranges of this host's network: the operator's
    # network.internal_cidrs and the managed servers', opened to every
    # sandbox alike (Runtime.apply_network).
    private_networks: bool = False
    # The runtime applies each sandbox's own deny_out, so an untrusted repo's
    # sandbox can be fenced (access.public_repos).
    egress_rules: bool = False


@dataclasses.dataclass(frozen=True)
class RuntimeLayer:
    """What an extension adds to the services that run the runtime on this
    host (Connector.runtime_layer): settings in their env and compose files
    over theirs. Each runtime that runs such services takes it its own way;
    applying a change recreates them (sbx-admin network apply)."""
    name: str
    env: Mapping[str, str] = dataclasses.field(default_factory=dict)  # its keys in their env; "" removes one
    compose: tuple[pathlib.Path, ...] = ()     # compose files merged over the runtime's, in order
    files: tuple[pathlib.Path, ...] = ()       # what those compose files mount (./<name>/<file>)


@dataclasses.dataclass(frozen=True)
class Limits:
    """The most a runtime allows, which sbx checks before asking it; None
    where it sets no limit or can't say. keys names, by field, the
    runtime.env setting that raises it, and source what sets the limits, as
    messages name them ("your <provider> plan")."""
    timeout_seconds: int | None = None  # how long a sandbox runs before its timeout
    cpu: int | None = None              # vCPUs per sandbox
    memory_mb: int | None = None        # per sandbox
    disk_mb: int | None = None          # per sandbox
    sandboxes: int | None = None        # running at once
    source: str = ""
    keys: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        if self.timeout_seconds is not None and self.timeout_seconds < MIN_TIMEOUT_SECONDS:
            raise ValueError(f"a runtime's timeout limit must be at least {MIN_TIMEOUT_SECONDS} seconds, not "
                             f"{self.timeout_seconds}: the keeper renews a used sandbox's timeout once a minute, "
                             "and a shorter limit pauses it between renewals")

    def known(self) -> dict:
        """The limits it sets, by field."""
        return {f: getattr(self, f) for f in LIMIT_FIELDS if getattr(self, f) is not None}


LIMIT_FIELDS = ("timeout_seconds", "cpu", "memory_mb", "disk_mb", "sandboxes")
# The shortest timeout limit a runtime may set: several of the keeper's
# renewal rounds (keeper.INTERVAL_SECONDS), so a used sandbox never times
# out between two.
MIN_TIMEOUT_SECONDS = 5 * 60


@dataclasses.dataclass(frozen=True)
class SettingField:
    """One of a backend's settings, as the web app's form shows it: a key of
    /etc/sbx/runtime.env (one of its credential_keys). A secret is write-only
    (sealed into the store, never shown back); limit names the Limits field
    an int setting sets, which presets and the limits the provider refused
    refer to."""
    key: str
    label: str
    secret: bool = False
    default: str = ""          # what it is when not set ("": nothing)
    help: str = ""
    kind: str = "text"         # "text" or "int" (a whole number, at least 1)
    optional: bool = False     # may stay empty (its default applies)
    limit: str = ""            # the Limits field it sets, for an int limit

    def view(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Preset:
    """A provider plan's limits, as values of the backend's settings by key;
    source says where they come from."""
    name: str
    title: str
    values: Mapping[str, str]
    source: str = ""

    def view(self) -> dict:
        return {"name": self.name, "title": self.title, "values": dict(self.values), "source": self.source}


class LimitRefused(Failed):
    """The provider refused a request because of a limit of its own (the
    plan's timeout, sandboxes at once, a sandbox's size): field is the Limits
    field, asked what sbx asked for, allowed what the provider said it allows
    (None when it did not say). sbx records it and names the setting to
    change; it never raises a limit by itself."""

    def __init__(self, message: str, *, field: str, asked=None, allowed=None):
        super().__init__(message)
        self.field, self.asked, self.allowed = field, asked, allowed


class TemplateSpec:
    """A template as steps, in order: where it starts (an image or another
    template), then users, working directories, env and commands, then how
    its VMs start. Each runtime builds it its own way."""

    def __init__(self):
        self.steps: list[tuple[str, Any]] = []

    def _add(self, what: str, value) -> "TemplateSpec":
        self.steps.append((what, value))
        return self

    def from_image(self, image: str):
        return self._add("image", image)

    def from_template(self, name: str):
        return self._add("template", name)

    def set_user(self, user: str):
        return self._add("user", user)

    def set_workdir(self, path: str):
        return self._add("workdir", path)

    def set_envs(self, envs: dict):
        return self._add("env", dict(envs))

    def run_cmd(self, command: str):
        return self._add("run", command)

    def dockerfile(self, text: str):
        """A Dockerfile's instructions; its FROM is ignored, the template has started already."""
        return self._add("dockerfile", text)

    def set_start_cmd(self, command: str, ready: int | str):
        """What every VM of the template runs when it starts, and when it is
        ready: a port that listens (int) or a command that exits 0 (str)."""
        return self._add("start", (command, ready))


class Commands(Protocol):
    def run(self, cmd: str, *, user: str | None = None, envs: dict | None = None, background: bool = False,
            timeout: float | None = 60) -> Any:
        """The command's result (stdout, stderr, exit_code), or with background
        a handle whose disconnect() leaves it running. Raises CommandFailed on
        a code other than 0 and Timeout when it runs out of time."""


class Files(Protocol):
    def write(self, path: str, data, *, user: str | None = None) -> Any: ...

    def read(self, path: str, *, format: str = "text") -> Any: ...


class Handle(Protocol):
    """A connected sandbox."""
    sandbox_id: str
    commands: Commands
    files: Files

    def kill(self) -> Any: ...


class Runtime(Protocol):
    kind: str
    title: str
    host: bool        # it runs on this server (its host part does something)
    capabilities: Capabilities
    disk_path: str    # where it keeps templates and snapshots, as messages name it
    # The last hop of a preview: nginx proxies to ingress with the Host
    # upstream_host names, after clearing the request headers in
    # ingress_cleared (a browser must not pick the VM). Without a domain the
    # app sees hosts ending in internal_suffix.
    ingress: str
    ingress_cleared: tuple
    internal_suffix: str
    ports: frozenset  # its own ports in every sandbox: never a preview
    host_unit: str | None   # the systemd unit that runs it on this host, which the host's guard needs
    credential_keys: tuple  # what it reads from /etc/sbx/runtime.env (vault.runtime)
    secret_key: str         # the one of them that is a secret, sealed into the store
    # What an admin sets in the web app (SettingFields, keys of credential_keys);
    # empty for a runtime install.sh sets up (on this host). presets: plans
    # the form offers for its limits.
    settings: tuple
    presets: tuple

    def can_run(self, facts: dict) -> str:
        """Why it can't run on this host, given setup.host_facts ("" when it can)."""

    def host_units(self) -> list:
        """Its systemd units on this host (host.Unit), installed before the platform's."""

    def host_commands(self, into) -> dict:
        """Its commands in /usr/local/sbin: name -> script, into(command) runs one in the active release."""

    def guard_host(self) -> None:
        """Its own rules on this host's firewall, after the platform's
        (sbx-host-guard): what its sandboxes' traffic may reach here."""

    def apply_host(self, key: str) -> bool:
        """A settings change's root-side effect (sbx-apply KEY) when key is
        the runtime's; False when it is not. settings.HostUnit.RUNTIME_ENV
        is always the runtime's: the installation's names changed (True,
        whether or not it has anything to do)."""

    def readiness(self, facts: dict) -> list:
        """What the first-run setup checks before its own test (connections.Check),
        given setup.host_facts."""

    def configure(self, secrets: dict) -> None:
        """Take its credentials from the platform secrets (runtime.env);
        SystemExit when they are missing."""

    def upstream_host(self, port: int, sandbox_id: str) -> str:
        """The Host nginx sends a sandbox port's requests to ingress with
        (and, to an https ingress, the name it asks for: SNI)."""

    # The request header a runtime that answers a sandbox's ports only with
    # that sandbox's own token wants it in (E2B Cloud's traffic token); None
    # for one that needs none. The gate hands nginx the token, nginx adds the
    # header to that runtime's requests only; a browser never sees it.
    upstream_token_header: str | None

    def upstream_token(self, sandbox_id: str, *, fetch: bool = True) -> str:
        """The sandbox's token for upstream_token_header. Without fetch only
        one the backend kept ("" when none: asking the provider may wake the
        sandbox); with fetch it asks, raising (Failed, NotRunning) rather
        than answering without one."""

    def own_route(self, host: str, sandbox_id: str, port: str):
        """For a request to the runtime's own host (Embed's dashboard): True
        to let it through ungated, (port, sandbox_id) to gate it like any
        preview; None for every other host."""

    def create(self, template: str, *, timeout: int, envs: dict | None = None, metadata: dict | None = None,
               deny_out: list | None = None, on_timeout: str | None = None,
               auto_resume: bool | None = None) -> Handle:
        """A new sandbox from a template or snapshot. deny_out: ranges it may
        not reach, given only with egress_rules; on_timeout: "pause" or
        "kill" when its timeout passes; auto_resume: traffic resumes it."""

    def connect(self, sandbox_id: str, *, timeout: int | None = None) -> Handle:
        """The running sandbox, resuming a paused one; timeout: seconds until it
        pauses. Raises NotRunning when the runtime has no such sandbox, and a
        handle's calls raise it when the sandbox paused or went meanwhile."""

    def sandboxes(self, metadata: dict | None = None, states: list | None = None) -> list[Info]: ...

    def info(self, sandbox_id: str) -> Info:
        """Raises NotRunning when the runtime has no such sandbox."""

    def kill(self, sandbox_id: str) -> bool: ...

    def pause(self, sandbox_id: str) -> None:
        """Raises Busy when the runtime would not pause it now, NotRunning
        when it has no such sandbox; one already paused stays paused."""

    def set_timeout(self, sandbox_id: str, seconds: int) -> None:
        """Raises NotRunning when it is not running (paused or gone)."""

    def snapshot(self, sandbox_id: str, name: str) -> Snapshot: ...

    def delete_snapshot(self, snapshot_id: str) -> None: ...

    def build_template(self, spec: TemplateSpec, *, alias: str, cpu: int, memory_mb: int, disk_mb: int,
                       on_log: Callable | None = None) -> Any: ...

    def templates(self) -> list[tuple[str, str]]:
        """Every template the runtime holds: (name, build status)."""

    def delete_template(self, name: str) -> bool:
        """False when it has no template of that name."""

    def failure(self, error: BaseException) -> bool:
        """Whether an error is the runtime's or the connection's to it."""

    def limits(self) -> Limits:
        """The most it allows per sandbox and at once."""

    def capacity(self, memory_mb: int) -> dict:
        """Room for sandboxes of memory_mb: {"known": True, "memory_mb",
        "slots_free", "free_mb", ...}, or {"known": False} when it can't say."""

    def apply_network(self) -> dict:
        """Make the runtime enforce what sandboxes may reach now
        (hosted_services.allowed_cidrs, empty without private_networks) and
        take what extensions add to it:
        {"status": "applied" or "unchanged", "allowed_cidrs": [...]};
        refused (ValueError) when it can't while sandboxes exist."""

    def disk(self) -> dict | None:
        """Its disk: {"percent": used, "free_gb"}; None when it can't be read."""

    def free_disk_mb(self) -> int | None:
        """Free MiB for templates and snapshots; None when it keeps none here."""

    def why_busy(self, memory_mb: int | None) -> str:
        """Why it refused to pause a sandbox of memory_mb (Busy), as what to do about it."""

    def snapshot_usage(self, sandbox_id: str) -> dict:
        """What a sandbox's snapshots hold: {"gb", "snapshots"}; {} when unknown."""

    def collect_storage(self) -> None:
        """Reclaim what nothing uses now (a failed build's layers, a deleted template's)."""

    def jobs(self) -> list:
        """Its upkeep as jobs.Job entries (Settings → Operations, sbx-admin jobs)."""

    def status(self):
        """Its connections.Status on the settings page."""

    def test(self) -> list:
        """Its checks (connections.Check), as sbx-admin doctor runs them."""

    def preview(self, name: str, port: int | None, listen: int, *, preserve_host: bool = False) -> None:
        """sbx-admin preview: serve a sandbox's app port on this host's
        loopback until interrupted. Optional: a runtime without a local
        proxy leaves it out (runtime.preview says so)."""

    def placing(self) -> ContextManager:
        """Held while a sandbox is placed, so two creations can't both take
        the last room."""


class HostSide:
    """The host part of the contract, as nothing: what a runtime that does
    not run on this host (a hosted provider) inherits. A runtime on this host
    (Embed) overrides what it has."""
    host = False
    settings: tuple = ()
    presets: tuple = ()

    def can_run(self, facts: dict) -> str:
        return ""
    host_unit: str | None = None
    disk_path = ""

    def host_units(self) -> list:
        return []

    def host_commands(self, into) -> dict:
        return {}

    def guard_host(self) -> None:
        return None

    def apply_host(self, key: str) -> bool:
        """The installation's names changed (RUNTIME_ENV) are every runtime's
        to take, with nothing to do here; any other key is not its."""
        from ..settings import HostUnit
        return key == HostUnit.RUNTIME_ENV

    def apply_network(self) -> dict:
        return {"status": "unchanged", "allowed_cidrs": []}

    def disk(self) -> dict | None:
        return None

    def free_disk_mb(self) -> int | None:
        return None

    def collect_storage(self) -> None:
        return None

    def jobs(self) -> list:
        return []

    def capacity(self, memory_mb: int) -> dict:
        return {"known": False}

    def placing(self) -> ContextManager:
        return contextlib.nullcontext()

    def readiness(self, facts: dict) -> list:
        return []

    def why_busy(self, memory_mb: int | None) -> str:
        return "the runtime is busy; pause again in a minute"

    def snapshot_usage(self, sandbox_id: str) -> dict:
        return {}
