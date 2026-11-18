"""The extension registry, and what every extension contributes to a sandbox.

Extensions are the built-in ones (BUILTIN: the GitHub connection and the
agent connectors) and any installed package's through the `sbx.connectors`
entry-point group, each a Connector subclass; the catalog's packages
contribute, and are provisioned, released and paused, through PACKAGES
(packages()). All are named by reference and loaded on first use, so this
module depends on none of them. The core asks this module (contributions,
packages, keeper_policy, origin, reserved ports); it never names an
extension, and extensions reach sandboxes only through the public domain API
(sandboxes, core.attach, core.pause, …). Their host services are installed
with the platform's (host.py).
"""
from __future__ import annotations

import dataclasses
import functools
import importlib
import importlib.metadata
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

from . import contributions, settings, state, templates
from .connections.base import Connection

GROUP = "sbx.connectors"
BUILTIN = ("sbx_control.connections.github:GitHub", "sbx_control.connectors.codex:Codex",
           "sbx_control.connectors.claude_code:ClaudeCode", "sbx_control.connectors.amp:Amp")
PACKAGES = "sbx_control.catalog.provisioning"   # the catalog's side of a sandbox's lifecycle


@dataclass(frozen=True)
class Target:
    """What a contribution is for: a repo's template (name "") or one sandbox."""
    repo: str
    name: str = ""
    trusted: bool = False
    connector: str = ""                          # the extension that made the sandbox, if one did
    env: Mapping[str, str] = field(default_factory=dict)  # the sandbox's env before the runtime layers


@dataclass(frozen=True)
class Policy:
    """How the keeper treats an extension's sandbox in a round."""
    keep_awake: bool = False    # push its timeout out
    decided: bool = False       # the extension judged its use (and paused it if it should): skip the platform's
    keep_paused: bool = False   # never delete it for having been paused long


@dataclass(frozen=True)
class Probe:
    """What a look inside a running sandbox found."""
    ssh: int = 0         # open SSH sessions
    agents: int = 0      # agent transcripts written in the last minutes (Connector.activity)
    busy: int = 0        # extensions' processes running (Connector.busy)


class Use:
    """What the keeper knows about a running sandbox's use in a round: from
    the gate without asking the sandbox, and from a look inside it (probe),
    taken once and only when something reads it."""

    def __init__(self, last_access: float, resumed_for_preview: bool, probe: Callable[[], Probe]):
        self.last_access = last_access                  # when the gate last let a request through
        self.resumed_for_preview = resumed_for_preview  # a preview request resumed it since it last paused
        self._probe = probe

    def accessed_within(self, seconds: float) -> bool:
        return time.time() - self.last_access < seconds

    @functools.cached_property
    def probe(self) -> Probe:
        return self._probe()


@dataclass(frozen=True)
class HostService:
    """A systemd service an extension runs on the host (host.py renders it;
    its working directory is the active release, host.CURRENT)."""
    name: str
    description: str
    service: str                # the [Service] section's lines
    enabled: bool               # running; else stopped and disabled
    apply: str = ""             # the host effect whose request restarts it (sbx-apply-<apply>)
    prepare: str = ""           # shell run as root before it starts: installs what it needs


Worker = Callable[[threading.Event], None]


@dataclass(frozen=True)
class Login:
    """A developer's own login for an agent: the CLI keeps it on their
    machine and `sbx open` writes it into the sandbox's tmpfs; the server
    never sees it."""
    path: str                    # where it goes in a sandbox
    command: str                 # the agent's CLI (`sbx open NAME <command>`)
    get: tuple[str, ...] = ()    # what `sbx login <kind>` runs to obtain it
    env: str = ""                # a token pasted after `get`, kept in the keychain, written as `export <env>=...`
    file: str = ""               # or the file the agent's own login writes (~: home), copied as is


class Connector(Connection):
    """An extension: an agent connector, or a platform connection that
    contributes to sandboxes (GitHub). Connection's status/test/connect/
    disconnect serve the settings UI; every hook below is optional."""
    group = "agent"
    cli: Callable[[list], int] | None = None   # `sbx-admin <kind> ...` on the host
    section: type[settings.ExtensionSection] | None = None   # its settings, stored under its kind
    # What every sandbox has of it whatever its repo, read on hot paths
    # (every preview request), so plain values, not its contribution:
    ports: tuple[int, ...] = ()       # its processes listen here in a sandbox; never a detected preview
    activity: tuple[str, ...] = ()    # directories of agent transcripts: a recent *.jsonl there is use
    busy: tuple[str, ...] = ()        # process command lines (pgrep -f patterns): one running is use
    login: Login | None = None        # a developer's own login for it (`sbx login`, `sbx open`)
    choosable = False                 # offered as a coding agent in the first-run setup (setup.choose_agents)

    def choose(self, on: bool, *, by: str):
        """Chosen (or not) as a coding agent in the setup, before any template
        is built: from now on its contribution has what its sandboxes need in
        every template, though its own key may come later."""

    def settings(self):
        """Its section of the settings now (its model's defaults until saved)."""
        return settings.current().extension(self.kind, self.section)

    def save_settings(self, value, *, by: str):
        settings.update(self.kind, value, by=by, model=self.section)

    def migrate(self, conn):
        """Make or update its own tables (conn: sbx.db); at every start, before anything runs."""

    def contribution(self, target: Target) -> contributions.Contribution | None:
        return None

    def git_access(self, repo: str):
        """A context giving git on this host its (url, env) for the repo,
        when this extension is how the installation reaches repos; else None."""
        return None

    def router(self):
        """Its HTTP routes (a FastAPI APIRouter), or None."""
        return None

    def workers(self) -> list[Worker]:
        """Run on threads from the server's start until stop is set."""
        return []

    def keeper_policy(self, info, use: Use) -> Policy | None:
        """For one of its sandboxes (metadata connector=<kind>); None: like any other."""
        return None

    def before_pause(self, info):
        """One of its running sandboxes is about to pause (core.pause), before
        any checkpoint: stop what must not be saved in its snapshot. Raising
        cancels the pause."""

    def on_resume(self, sbx, info):
        """One of its paused sandboxes was just resumed (sbx: the connected VM),
        before its runtime is restored and whatever the resume is for runs."""

    def origin(self, info) -> dict | None:
        """Where the sandbox came from, for the views: {kind, label, detail, href}."""
        return None

    def host_services(self) -> list[HostService]:
        return []

    def runtime_layer(self, kind: str):
        """What it adds to the services of the runtime of this kind
        (Backend.kind) on this host (runtime.RuntimeLayer), or None."""
        return None

    def on_restore(self, conn):
        """A backup restored on another host (conn: the restored sbx.db): settle
        its own rows that pointed at work there."""


_connectors: list | None = None


def _load(reference: str):
    module, _, name = reference.partition(":")
    return getattr(importlib.import_module(module), name)


def connectors() -> list[Connector]:
    """Every extension, the built-in ones first; a kind is taken once."""
    global _connectors
    if _connectors is None:
        found: dict = {}
        references = [*BUILTIN, *(entry.value for entry in importlib.metadata.entry_points(group=GROUP))]
        for reference in references:
            try:
                connector = _load(reference)()
            except Exception as error:  # one broken plug-in must not take the platform down
                print(f"connector {reference}: {error!r}", file=sys.stderr, flush=True)
                continue
            if not isinstance(connector, Connector):
                print(f"connector {reference}: not a Connector", file=sys.stderr, flush=True)
                continue
            found.setdefault(connector.kind, connector)
        _connectors = list(found.values())
    return _connectors


def get(kind: str) -> Connector | None:
    return next((c for c in connectors() if c.kind == kind), None)


def connector_of(info) -> Connector | None:
    return get(((getattr(info, "metadata", None) or {}).get("connector")) or "")


def _migrate(conn):
    for connector in connectors():
        connector.migrate(conn)


SCHEMA = state.Schema(upgrade=_migrate)


def start(stop: threading.Event):
    for connector in connectors():
        for work in connector.workers():
            threading.Thread(target=work, args=(stop,), name=f"{connector.kind}-{work.__name__}", daemon=True).start()


def git_access(repo: str):
    """How git on this host reaches the repo (Connector.git_access)."""
    for connector in connectors():
        access = connector.git_access(repo)
        if access is not None:
            return access
    raise LookupError(f"no extension reaches {repo}")


def routers() -> list:
    return [r for r in (c.router() for c in connectors()) if r is not None]


def packages():
    """The catalog's side of a sandbox's lifecycle: provision, refresh,
    release, paused, reconcile, contributions, scrub_paths and Error
    (catalog/provisioning.py)."""
    return importlib.import_module(PACKAGES)


def contributions_for(target: Target) -> list:
    """Everything that contributes to a sandbox or a template: the
    platform's own, every extension's, and the packages attached to it."""
    out = [templates.PLATFORM, *(c.contribution(target) for c in connectors()), *packages().contributions(target)]
    return [c for c in out if c is not None]


def scrub_paths(target: Target) -> list:
    """What no reusable snapshot of the sandbox keeps: every contribution's
    scrub_paths, read from its template contribution (name ""), so no
    runtime layer is built and no package's host code runs."""
    template = Target(repo=target.repo, trusted=target.trusted, connector=target.connector)
    found = [templates.PLATFORM, *(c.contribution(template) for c in connectors())]
    return sorted({p for c in found if c is not None for p in c.scrub_paths} | packages().scrub_paths(target.name))


def reserved_ports() -> set:
    return {port for c in connectors() for port in c.ports}


def activity_paths() -> list:
    return [path for c in connectors() for path in c.activity]


def busy_processes() -> list:
    return [pattern for c in connectors() for pattern in c.busy]


def logins() -> dict:
    """kind -> the agent's Login, as the CLI reads it."""
    return {c.kind: {"title": c.title, **dataclasses.asdict(c.login)} for c in connectors() if c.login}


def keeper_policy(info, use: Use) -> Policy | None:
    connector = connector_of(info)
    return connector.keeper_policy(info, use) if connector else None


def before_pause(info):
    connector = connector_of(info)
    if connector:
        connector.before_pause(info)


def on_resume(sbx, info):
    connector = connector_of(info)
    if connector:
        connector.on_resume(sbx, info)


def origin(info) -> dict | None:
    connector = connector_of(info)
    return connector.origin(info) if connector else None


def host_services() -> list[HostService]:
    return [service for c in connectors() for service in c.host_services()]


def on_restore(conn):
    for connector in connectors():
        connector.on_restore(conn)
