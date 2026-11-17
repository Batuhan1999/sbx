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

Every backend subclasses Backend: the compute part (create, list, connect,
templates, ...) is abstract; the host part (systemd units, the host's
firewall, disk, capacity, readiness) and every optional property have
defaults, so a hosted backend implements nothing it does not have."""
from __future__ import annotations

import contextlib
import dataclasses
import datetime
import pathlib
import re
import shlex
import abc
from typing import Any, Callable, ContextManager, Literal, Mapping, Protocol

RUNNING, PAUSED = "running", "paused"
# A sandbox the runtime lists but that failed (Boat's error state): not
# running, not paused; resuming may bring it back, else it is deleted.
FAILED = "failed"


class Failed(Exception):
    """The runtime or the connection to it failed: it answered with an error
    or not at all."""


# A flag whose value is a secret: --password, --token, --api-key, --user, a header, curl's -u and -H.
SECRET_FLAG = re.compile(r"--?[\w-]*(pass(word|wd)?|token|secret|key|auth\w*|user|header|cookie|credentials?)"
                         r"|-[uH]", re.IGNORECASE)


def command_name(command: str) -> str:
    """What a failed command was, for its message: the words of its first
    line up to 80 characters, leaving out every word with "=" in it (an
    assignment: an env value, a key, quoted or not), what follows a word
    that is a secret's flag (SECRET_FLAG) and a quote the next lines close.
    Never the command's values."""
    first = (command or "").strip().splitlines()[0] if (command or "").strip() else ""
    lexer = shlex.shlex(first, posix=True)
    lexer.whitespace_split = True
    words, value = [], False
    try:
        for word in lexer:
            if value or "=" in word:
                value = False
                continue
            value = bool(SECRET_FLAG.fullmatch(word))
            words.append(word)
            if len(" ".join(words)) > 80:
                words[-1] = "…"
                break
    except ValueError:   # a quote the next lines close: what it holds is left out
        pass
    return " ".join(words)


class CommandFailed(Failed):
    """A command in a sandbox exited with a code other than 0. command: what
    it was (command_name), said in the message with the end of its output."""

    def __init__(self, message: str = "", *, stdout: str = "", stderr: str = "", exit_code: int = 1,
                 command: str = ""):
        tail = (stderr or "").strip()[-1500:] or (stdout or "").strip()[-1500:]
        name = command_name(command)
        super().__init__(message or (f"`{name}` exited with code {exit_code}" if name else
                                     f"Command exited with code {exit_code}") + (f" and error:\n{tail}" if tail else ""))
        self.stdout, self.stderr, self.exit_code, self.command = stdout, stderr, exit_code, name


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
    state: str                                 # RUNNING or PAUSED (FAILED where the runtime has it)
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

    @classmethod
    def read(cls, specs, secrets: Mapping[str, str], source: str) -> "Limits":
        """The limits runtime.env sets (each LimitSpec's key, else its
        default); SystemExit naming the key of a value that is not one."""
        found = {}
        for spec in specs:
            value = secrets.get(spec.key) or spec.default
            try:
                found[spec.field] = int(value)
            except (TypeError, ValueError):
                raise SystemExit(f"{spec.key}={value} in /etc/sbx/runtime.env is not a whole number") from None
        try:
            return cls(**found, source=source, keys={s.field: s.key for s in specs})
        except ValueError as error:
            key = next((s.key for s in specs if s.field == "timeout_seconds"), "the timeout")
            raise SystemExit(f"{key} in /etc/sbx/runtime.env: {error}") from None


LIMIT_FIELDS = ("timeout_seconds", "cpu", "memory_mb", "disk_mb", "sandboxes")
# The shortest timeout limit a runtime may set: several of the keeper's
# renewal rounds (keeper.INTERVAL_SECONDS), so a used sandbox never times
# out between two.
MIN_TIMEOUT_SECONDS = 5 * 60


# A host name, lowercase, without a trailing dot: what a "hostname" setting
# takes (SettingField.view hands the web app the same pattern).
HOSTNAME = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")


@dataclasses.dataclass(frozen=True)
class SettingField:
    """One of a backend's settings, as the web app's form shows it: a key of
    /etc/sbx/runtime.env (one of its credential_keys). A secret is write-only
    (sealed into the store, never shown back); limit names the Limits field
    an int setting sets, which presets and the limits the provider refused
    refer to. rekey: it says where the secret is sent (a provider's domain),
    so changing it needs the secret typed again: a stored one is never sent
    to a new place."""
    key: str
    label: str
    secret: bool = False
    default: str = ""          # what it is when not set ("": nothing)
    help: str = ""
    kind: str = "text"         # "text", "int" (a whole number, at least 1) or "hostname"
    optional: bool = False     # may stay empty (its default applies)
    limit: str = ""            # the Limits field it sets, for an int limit
    rekey: bool = False
    # An int's range and step (the web app's slider; values outside are
    # refused), and its unit: "seconds", "MiB" or "" (a count).
    min: int | None = None
    max: int | None = None
    step: int | None = None
    unit: str = ""

    def view(self) -> dict:
        """As the API shows it; a hostname's pattern (HOSTNAME) for the web app's own check."""
        return {**dataclasses.asdict(self), **({"pattern": HOSTNAME.pattern} if self.kind == "hostname" else {})}

    def parse(self, raw) -> str | None:
        """A value typed for it, as runtime.env keeps it: None when empty,
        a host name lowercase without its trailing dot; ValueError saying
        why one is not a value of it."""
        value = "" if raw is None else str(raw).strip()
        if not value:
            return None
        if self.kind == "int":
            # ASCII digits only (str.isdigit takes "²" and other scripts' digits), a sane length,
            # kept canonical ("0100" is "100") so a plan's values match it.
            if not (value.isascii() and value.isdigit() and len(value) <= 12 and int(value) >= 1):
                raise ValueError(f"{self.label} ({self.key}) is a whole number, at least 1, not {value!r}")
            value = str(int(value))
            if (self.min is not None and int(value) < self.min) or (self.max is not None and int(value) > self.max):
                raise ValueError(f"{self.label} ({self.key}) is from {self.min} to {self.max}"
                                 f"{' ' + self.unit if self.unit else ''}, not {value}")
        if self.kind == "hostname":
            value = value.lower().rstrip(".")
            if not HOSTNAME.fullmatch(value):
                raise ValueError(f"{self.label} ({self.key}) is a host name such as example.com, not {value!r}")
        return value


@dataclasses.dataclass(frozen=True)
class LimitSpec:
    """One Limits field a hosted runtime's settings set: its runtime.env key
    and default, and how the web app's form shows it (a slider)."""
    field: str
    key: str
    default: int
    label: str
    unit: str = ""
    min: int | None = None
    max: int | None = None
    step: int | None = None


def limit_settings(specs) -> tuple:
    """The SettingFields of these limits, in order."""
    return tuple(SettingField(s.key, s.label, default=str(s.default), kind="int", limit=s.field, unit=s.unit,
                              min=s.min, max=s.max, step=s.step) for s in specs)


@dataclasses.dataclass(frozen=True)
class Preset:
    """A provider plan's limits, by Limits field; source says where they
    come from. adjustable: the fields the plan does not fix (the provider
    publishes no number), set by the admin starting from values; note says
    why. keys: field -> the backend's runtime.env key (bound(), from its
    LimitSpecs), which the settings form and runtime.env name."""
    name: str
    title: str
    values: Mapping[str, int]
    source: str = ""
    adjustable: tuple = ()
    note: str = ""
    keys: Mapping[str, str] = dataclasses.field(default_factory=dict)

    def bound(self, specs) -> "Preset":
        return dataclasses.replace(self, keys={s.field: s.key for s in specs})

    def _key(self, field: str) -> str:
        return self.keys[field]

    def by_key(self) -> dict:
        """Its values by runtime.env key, as strings."""
        return {self._key(field): str(value) for field, value in self.values.items()}

    def adjustable_keys(self) -> list:
        return [self._key(field) for field in self.adjustable]

    def matches(self, effective: Mapping[str, str]) -> bool:
        """Whether settings with these effective values (by key) are this plan: its fixed values alike."""
        return all(effective.get(self._key(field)) == str(value) for field, value in self.values.items()
                   if field not in self.adjustable)

    def view(self) -> dict:
        return {"name": self.name, "title": self.title, "values": self.by_key(), "source": self.source,
                "adjustable": self.adjustable_keys(), "note": self.note}


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


@dataclasses.dataclass(frozen=True)
class TokenPlace:
    """Where a runtime that answers its sandboxes' ports only with a token
    wants it: a request header (E2B Cloud's traffic token) or a cookie
    appended to the app's own (Boat's per-port token)."""
    place: Literal["header", "cookie"]
    name: str


class Backend(abc.ABC):
    """A sandbox runtime: what sbx asks of one. The compute part is abstract;
    every other member has a default, so a hosted backend implements only
    what it has. The host part (systemd units, the host's firewall, disk,
    capacity, readiness) is for a runtime on this host (host=True), which
    overrides it."""
    kind: str
    title: str
    capabilities: Capabilities = Capabilities()
    host = False          # it runs on this server (its host part does something)
    disk_path = ""        # where it keeps templates and snapshots, as messages name it
    # The last hop of a preview: nginx proxies to ingress with the Host
    # upstream_host names, after clearing the request headers in
    # ingress_cleared (a browser must not pick the VM). Without a domain the
    # app sees hosts ending in internal_suffix.
    ingress = ""
    ingress_cleared: tuple = ()
    internal_suffix = ""
    ports: frozenset = frozenset()   # its own ports in every sandbox: never a preview
    host_unit: str | None = None     # the systemd unit that runs it on this host, which the host's guard needs
    credential_keys: tuple = ()      # what it reads from /etc/sbx/runtime.env (vault.runtime)
    secret_key = ""                  # the one of them that is a secret, sealed into the store
    # What an admin sets in the web app (SettingFields, keys of credential_keys);
    # empty for a runtime install.sh sets up (on this host). presets: plans
    # the form offers for its limits.
    settings: tuple = ()
    presets: tuple = ()
    # Where it wants the token it answers its sandboxes' ports with (None:
    # it needs none). The gate hands nginx the token, nginx adds it on that
    # runtime's requests only; a browser never sees it.
    upstream_token_at: TokenPlace | None = None
    # Which desktop a sandbox has: "sbx" (sbx's own noVNC on
    # platform.NOVNC_PORT, which the templates' start command runs and the
    # desktop preview proxies to) or "provider" (the provider's own, which
    # desktop_url opens; sbx starts none of its own there).
    desktop = "sbx"
    # Every resume boots the VM afresh (a provider without memory
    # snapshots): its tmpfs is empty and nothing of what ran is running, so
    # sbx re-ships what lives in platform.RUN and restarts the recipe's
    # processes on every resume, whatever lifecycle.resume says.
    cold_resume = False

    # The compute part

    @abc.abstractmethod
    def configure(self, secrets: dict) -> None:
        """Take its credentials from the platform secrets (runtime.env);
        SystemExit when they are missing."""

    @abc.abstractmethod
    def upstream_host(self, port: int, sandbox_id: str) -> str:
        """The Host nginx sends a sandbox port's requests to ingress with
        (and, to an https ingress, the name it asks for: SNI)."""

    @abc.abstractmethod
    def create(self, template: str, *, timeout: int, envs: dict | None = None, metadata: dict | None = None,
               deny_out: list | None = None, on_timeout: str | None = None,
               auto_resume: bool | None = None) -> Handle:
        """A new sandbox from a template or snapshot. deny_out: ranges it may
        not reach, given only with egress_rules; on_timeout: "pause" or
        "kill" when its timeout passes; auto_resume: traffic resumes it."""

    @abc.abstractmethod
    def connect(self, sandbox_id: str, *, timeout: int | None = None) -> Handle:
        """The running sandbox, resuming a paused one; timeout: seconds until it
        pauses. Raises NotRunning when the runtime has no such sandbox, and a
        handle's calls raise it when the sandbox paused or went meanwhile."""

    def handle(self, sandbox_id: str) -> Handle:
        """A running sandbox's handle with no side effect of its own (no
        resume, no setup), for a look such as the gate's port probe; its
        calls raise NotRunning when it is not running. By default connect()
        once info() says it runs (NotRunning otherwise: never a resume); a
        backend whose connect does more overrides it."""
        if self.info(sandbox_id).state != RUNNING:
            raise NotRunning(f"sandbox {sandbox_id} is not running")
        return self.connect(sandbox_id)

    @abc.abstractmethod
    def sandboxes(self, metadata: dict | None = None, states: list | None = None) -> list[Info]: ...

    @abc.abstractmethod
    def info(self, sandbox_id: str) -> Info:
        """Raises NotRunning when the runtime has no such sandbox."""

    @abc.abstractmethod
    def kill(self, sandbox_id: str) -> bool: ...

    @abc.abstractmethod
    def pause(self, sandbox_id: str) -> None:
        """Raises Busy when the runtime would not pause it now, NotRunning
        when it has no such sandbox; one already paused stays paused."""

    @abc.abstractmethod
    def set_timeout(self, sandbox_id: str, seconds: int) -> None:
        """Raises NotRunning when it is not running (paused or gone)."""

    @abc.abstractmethod
    def snapshot(self, sandbox_id: str, name: str) -> Snapshot: ...

    @abc.abstractmethod
    def delete_snapshot(self, snapshot_id: str) -> None: ...

    @abc.abstractmethod
    def build_template(self, spec: TemplateSpec, *, alias: str, cpu: int, memory_mb: int, disk_mb: int,
                       on_log: Callable | None = None) -> Any: ...

    @abc.abstractmethod
    def templates(self) -> list[tuple[str, str]]:
        """Every template the runtime holds: (name, build status)."""

    @abc.abstractmethod
    def delete_template(self, name: str) -> bool:
        """False when it has no template of that name."""

    @abc.abstractmethod
    def limits(self) -> Limits:
        """The most it allows per sandbox and at once."""

    @abc.abstractmethod
    def status(self):
        """Its connections.Status on the settings page."""

    @abc.abstractmethod
    def test(self) -> list:
        """Its checks (connections.Check), as sbx-admin doctor runs them."""

    def failure(self, error: BaseException) -> bool:
        """Whether an error is the runtime's or the connection's to it."""
        return False

    def upstream_token(self, sandbox_id: str, port: int, *, fetch: bool = True) -> str:
        """The sandbox's token for its port, for a runtime with upstream_token_at
        (one with one token per sandbox ignores port). Without fetch only one
        the backend kept ("" when none: asking the provider may wake the
        sandbox or expose the port); with fetch it asks, raising (Failed,
        NotRunning) rather than answering without one."""
        return ""

    def desktop_url(self, sandbox_id: str) -> str:
        """For a "provider" desktop: a fresh URL that opens the sandbox's
        desktop (it may carry a short-lived token)."""
        raise Failed(f"{self.title} has no desktop of its own")

    def own_route(self, host: str, sandbox_id: str, port: str):
        """For a request to the runtime's own host (Embed's dashboard): True
        to let it through ungated, (port, sandbox_id) to gate it like any
        preview; None for every other host."""
        return None

    def template_name(self, name: str) -> str:
        """The name a template or snapshot of sbx's has on the runtime: as is,
        but on a hosted one (scoped.Scoped) after the installation's prefix."""
        return name

    def listed(self, sandbox_ids: set) -> None:
        """After a full listing: forget what it keeps per sandbox (a token)
        for one no longer there, whoever ended it."""

    def preview(self, name: str, port: int | None, listen: int, *, preserve_host: bool = False) -> None:
        """sbx-admin preview: serve a sandbox's app port on this host's
        loopback until interrupted; a runtime without a local proxy says so."""
        raise ValueError(f"{self.title} has no loopback preview on this host; open the sandbox's preview URL instead")

    def why_busy(self, memory_mb: int | None) -> str:
        """Why it refused to pause a sandbox of memory_mb (Busy), as what to do about it."""
        return "the runtime is busy; pause again in a minute"

    def snapshot_usage(self, sandbox_id: str) -> dict:
        """What a sandbox's snapshots hold: {"gb", "snapshots"}; {} when unknown."""
        return {}

    # The host part: nothing, for a runtime that does not run on this host

    def can_run(self, facts: dict) -> str:
        """Why it can't run on this host, given setup.host_facts ("" when it can)."""
        return ""

    def host_units(self) -> list:
        """Its systemd units on this host (host.Unit), installed before the platform's."""
        return []

    def host_commands(self, into) -> dict:
        """Its commands in /usr/local/sbin: name -> script, into(command) runs one in the active release."""
        return {}

    def guard_host(self) -> None:
        """Its own rules on this host's firewall, after the platform's
        (sbx-host-guard): what its sandboxes' traffic may reach here."""

    def apply_host(self, key: str) -> bool:
        """A settings change's root-side effect (sbx-apply KEY) when key is
        the runtime's; False when it is not. settings.HostUnit.RUNTIME_ENV
        is always the runtime's: the installation's names changed (True,
        whether or not it has anything to do)."""
        from ..settings import HostUnit
        return key == HostUnit.RUNTIME_ENV

    def readiness(self, facts: dict) -> list:
        """What the first-run setup checks before its own test (connections.Check),
        given setup.host_facts."""
        return []

    def apply_network(self) -> dict:
        """Make the runtime enforce what sandboxes may reach now
        (hosted_services.allowed_cidrs, empty without private_networks) and
        take what extensions add to it:
        {"status": "applied" or "unchanged", "allowed_cidrs": [...]};
        refused (ValueError) when it can't while sandboxes exist."""
        return {"status": "unchanged", "allowed_cidrs": []}

    def disk(self) -> dict | None:
        """Its disk: {"percent": used, "free_gb"}; None when it can't be read."""
        return None

    def free_disk_mb(self) -> int | None:
        """Free MiB for templates and snapshots; None when it keeps none here."""
        return None

    def collect_storage(self) -> None:
        """Reclaim what nothing uses now (a failed build's layers, a deleted template's)."""

    def jobs(self) -> list:
        """Its upkeep as jobs.Job entries (Settings → Operations, sbx-admin jobs)."""
        return []

    def capacity(self, memory_mb: int) -> dict:
        """Room for sandboxes of memory_mb: {"known": True, "memory_mb",
        "slots_free", "free_mb", ...}, or {"known": False} when it can't say."""
        return {"known": False}

    def placing(self) -> ContextManager:
        """Held while a sandbox is placed, so two creations can't both take
        the last room."""
        return contextlib.nullcontext()

