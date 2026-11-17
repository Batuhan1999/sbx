"""The installation's settings: validated, immutable, with a revision and
its history, kept in sbx.db (the config, config_history and repos tables).

An extension keeps a section of its own (an agent connector's): it declares
the model (extension(), update(..., model=)). This package imports nothing
of sbx_control but the database and the paths."""
from __future__ import annotations

import enum
import fnmatch
import ipaddress
import json
import re
import sqlite3
import sys
import time
from typing import Callable, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .. import paths, state

REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")   # a GitHub owner/name
REPO_RE = re.compile(rf"^(?:{REPO_NAME.pattern}|[A-Za-z0-9][A-Za-z0-9-]{{0,38}}/\*)$")  # or every repo of an owner
REVISION = state.Setting("config_revision")


class HostUnit(enum.StrEnum):
    """Root-side effects sbx-control requests but never performs: it touches
    /run/sbx/apply/<unit>; sbx-apply-<unit>.path runs the matching script.
    Extensions add their own, by name (extensions.HostService.apply)."""
    TLS = "tls"                      # control/tls.sh: nginx config and certificate
    HOST_GUARD = "host-guard"        # sbx-host-guard: public ports, the egress rules
    RUNTIME_NETWORK = "runtime-network"  # what recreates the runtime: pending until sbx-admin network apply
    CONTROL = "control"              # sbx-control restart (what it reads at start: the cookie domain)
    RUNTIME_ENV = "runtime-env"      # the installation's names, to the runtime (Runtime.apply_host)
    UPGRADE = "upgrade"              # install a newer release (updates.py); the request names its tag
    RUNTIME_CHANGE = "runtime-change"  # add, change, remove a runtime or make one the default (runtime_changes.py)


class Section(BaseModel):
    """A part of the settings written as a whole. applies: the host effects a
    change asks for; a section whose effects depend on what changed says so
    in effects()."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    applies: ClassVar[tuple[str, ...]] = ()

    def effects(self, before: "Section") -> tuple[str, ...]:
        return self.applies


class ExtensionSection(Section):
    """An extension's own section. Values a later release dropped are ignored."""
    model_config = ConfigDict(frozen=True, extra="ignore")


class Domain(Section):
    applies = (HostUnit.TLS, HostUnit.RUNTIME_ENV, HostUnit.CONTROL)
    name: str = ""                     # "" until set: the installation answers on its address only
    address: str = ""                  # the IP chosen during setup; keeps bootstrap access during DNS setup
    login_host: str = "login"
    acme_email: str = ""
    # How the wildcard certificate's DNS-01 challenge is answered: delegated
    # (one NS record hands _acme-challenge.<name> to this host, which answers
    # itself: acme_dns.py; no credential), an API of the DNS provider, or
    # none (self-signed, for a trial).
    acme: Literal["delegated", "route53", "cloudflare", "none"] = "delegated"

    def effects(self, before: "Domain") -> tuple[str, ...]:
        """The names reach the runtime and the session cookie; the rest only
        the certificate, and the firewall when the ports it needs change (opened_ports)."""
        guard = (HostUnit.HOST_GUARD,) if self.needs_ports() != before.needs_ports() else ()
        if (self.name, self.login_host) != (before.name, before.login_host):
            return (*self.applies, *guard)
        return (HostUnit.TLS, *guard)

    def needs_ports(self) -> tuple[int, ...]:
        """What the certificate needs open: 80 for the address's (HTTP), 53
        for the delegated DNS-01 responder."""
        return (*((80,) if self.address else ()), *((53,) if self.name and self.acme == "delegated" else ()))

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        return str(ipaddress.ip_address(value)) if value else ""

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip().lower().rstrip(".")
        if value and (len(value) > 253 or "." not in value or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in value.split("."))):
            raise ValueError("enter a domain such as sbx.example.com")
        return value


class SignIn(Section):
    """The non-secret half; the vault holds OIDC_CLIENT_SECRET, users.py the passwords."""
    method: Literal["oidc", "password", "none"] = "password"
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    allowed_emails: tuple[str, ...] = ()  # OIDC: patterns such as *@example.com (* admits anyone), besides the people added

    def listed(self, email: str) -> bool:
        """Whether allowed_emails names email (OIDC; * for anyone the
        provider admits). auth.may_sign_in also admits the people added."""
        return any(fnmatch.fnmatchcase(email.lower(), pattern.lower()) for pattern in self.allowed_emails)

    def refusal(self) -> str:
        """Why this can't be saved ("" when it can): every writer of the
        section goes through update(), which checks it."""
        return ""


class Access(Section):
    admins: tuple[str, ...] = ()
    public_repos: bool = False           # any repo besides the served ones, untrusted


class Network(Section):
    applies = (HostUnit.TLS, HostUnit.HOST_GUARD, HostUnit.RUNTIME_NETWORK)
    host_ports: tuple[int, ...] = (443,)
    public_ports: tuple[int, ...] | Literal["all"] = (22, 443)
    ip_preview_first_port: int = Field(default=21000, ge=1024, le=65535)
    ip_preview_last_port: int = Field(default=21255, ge=1024, le=65535)
    sandbox_cidr: str = ""
    internal_cidrs: tuple[str, ...] = ()  # every sandbox of a trusted repo may reach these

    @model_validator(mode="after")
    def _cidrs(self):
        if self.ip_preview_first_port > self.ip_preview_last_port:
            raise ValueError("IP preview port range must be ordered")
        reserved = set(self.host_ports) | (set(self.public_ports) if self.public_ports != "all" else set())
        if any(self.ip_preview_first_port <= p <= self.ip_preview_last_port for p in reserved):
            raise ValueError("IP preview port range overlaps an explicitly configured host/public port")
        for cidr in self.internal_cidrs:
            ipaddress.ip_network(cidr)
        return self


class Sandboxes(Section):
    agent_hidden: tuple[str, ...] = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    paused_days: float = 3
    detected_previews: Literal["owner", "team", "off"] = "owner"
    previews: dict[str, dict] = Field(default_factory=lambda: {
        "desktop": {"port": 6080, "access": "owner", "path": "/vnc.html?autoconnect=1&resize=scale"},
        "ssh": {"port": 8081, "access": "owner", "link": False}})
    git_email: str = ""


class Variables(Section):
    """Plain values for the sandboxes of every repo a pattern matches:
    pattern -> name -> value; values may use ${secret:NAME}."""
    by_pattern: dict[str, dict[str, str]] = Field(default_factory=dict)


class Store(Section):
    """Where secret values live (secret_store.py): sealed in sbx.db (local) or
    in an Infisical project (the one the installer runs on this host, on a
    loopback port)."""
    mode: Literal["local", "infisical"] = "local"
    port: int = Field(default=8190, ge=1, le=65535)
    project: str = ""
    environment: str = "dev"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class Proxied(Section):
    """Operator services published at <name>.<domain> behind the login:
    name -> upstream host:port."""
    applies = (HostUnit.TLS,)
    services: dict[str, str] = Field(default_factory=dict)


class Backup(Section):
    """A bundle (sbx-admin backup) every every_hours hours (0: never),
    whatever the secret store, into the directory `to` (default
    <state>/backups), keeping the newest `keep` scheduled ones; bundles made
    by hand are never removed."""
    every_hours: int = Field(default=24, ge=0)
    to: str = ""
    keep: int = Field(default=14, ge=1)

    @model_validator(mode="after")
    def _target(self):
        if self.to and not self.to.startswith("/"):
            raise ValueError("backup.to must be an absolute directory")
        return self


class Updates(Section):
    """Newer releases (updates.py): the published releases of `repository`
    on GitHub (public, carrying the release archive), looked at every few
    hours unless `check` is off (an air-gapped or private installation);
    `prereleases` offers release candidates too (a staging installation)."""
    check: bool = True
    repository: str = "Batuhan1999/sbx"
    prereleases: bool = False

    @field_validator("repository")
    @classmethod
    def _repository(cls, value: str) -> str:
        if not REPO_NAME.fullmatch(value):
            raise ValueError("updates.repository is a GitHub owner/name, such as Batuhan1999/sbx")
        return value


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    revision: int = 0
    domain: Domain = Domain()
    sign_in: SignIn = SignIn()
    access: Access = Access()
    network: Network = Network()
    sandboxes: Sandboxes = Sandboxes()
    variables: Variables = Variables()
    store: Store = Store()
    proxied: Proxied = Proxied()
    backup: Backup = Backup()
    updates: Updates = Updates()
    # Repos the installation serves (owner/repo, owner/*), in order; the first
    # exact one is the default repo. Kept in the `repos` table, read in here.
    repos: tuple[str, ...] = ()
    # Extensions' sections as stored, by name; read through extension().
    extensions: dict[str, dict] = Field(default_factory=dict)

    @field_validator("repos")
    @classmethod
    def lowercase(cls, repos: tuple) -> tuple:
        """GitHub names are case-insensitive; every lookup by repo is by its lowercase."""
        return tuple(dict.fromkeys(r.lower() for r in repos))

    @model_validator(mode="after")
    def check(self):
        for repo in self.repos:
            if not REPO_RE.match(repo):
                raise ValueError(f"repos: {repo} is not owner/repo or owner/*")
        return self

    @property
    def base_url(self) -> str:
        if self.domain.name:
            return f"https://{self.domain.login_host}.{self.domain.name}"
        address = self.domain.address
        return (f"https://[{address}]" if ":" in address else f"https://{address}") if address else ""

    @property
    def default_repo(self) -> str:
        return next((r for r in self.repos if "*" not in r), "")

    @property
    def admins(self) -> set:
        return {e.lower() for e in self.access.admins}

    @property
    def git_email(self) -> str:
        return self.sandboxes.git_email or f"sandbox@{self.domain.name}"

    @property
    def reserved_names(self) -> set:
        """Sandbox names a host of the installation's own would collide with."""
        return {self.domain.login_host, "sandbox", *self.proxied.services}

    def serves(self, repo: str) -> bool:
        return any(fnmatch.fnmatchcase(repo, pattern.lower()) for pattern in self.repos)

    def extension(self, name: str, model: type[ExtensionSection]):
        return model.model_validate(self.extensions.get(name) or {})


SECTIONS = tuple(name for name, field in Settings.model_fields.items()
                 if isinstance(field.annotation, type) and issubclass(field.annotation, Section))


class Conflict(Exception):
    """The revision the writer saw is no longer the current one."""


class Invalid(Exception):
    pass


_cache: dict = {"key": None, "settings": None, "checked": 0.0}
_checks: dict[str, list[Callable]] = {}


def check(section: str):
    """Register fn(before, after) for a section: it raises ValueError to refuse
    a change. The owner of what a section's values affect registers it (IP
    previews for network, the secret store for store)."""
    def register(fn):
        _checks.setdefault(section, []).append(fn)
        return fn
    return register


@check("sign_in")
def _sign_in(before: SignIn, after: SignIn):
    if after.refusal():
        raise ValueError(after.refusal())


def current() -> Settings:
    """The settings now; cached by revision, looked at once a second."""
    now = time.monotonic()
    where = str(paths.database())
    if _cache["settings"] is not None and _cache["key"] and _cache["key"][0] == where and now - _cache["checked"] < 1:
        return _cache["settings"]
    revision = _revision()
    if _cache["key"] != (where, revision):
        _cache.update(settings=_load(), key=(where, revision))
    _cache["checked"] = now
    return _cache["settings"]


def forget():
    _cache.update(settings=None, key=None, checked=0.0)


def trust(repo: str | None):
    """trusted (its secrets and services, the allowed private ranges),
    public (public_repos: any other repo, cloned anonymously, with none of
    those), or None: not served."""
    s = current()
    if repo and s.serves(repo):
        return "trusted"
    return "public" if s.access.public_repos and repo else None


def update(section: str, value: dict | BaseModel, *, by: str, expect_revision: int | None = None,
           model: type[ExtensionSection] | None = None) -> Settings:
    """The only writer of one section; an extension's names its model.
    expect_revision: optimistic concurrency (Conflict when it moved).
    Idempotent: an equal value writes nothing."""
    before = current()
    if expect_revision is not None and expect_revision != before.revision:
        raise Conflict(f"settings changed meanwhile (revision {before.revision}, not {expect_revision})")
    if section in SECTIONS:
        model = type(getattr(before, section))
        old = getattr(before, section)
    elif model is None or section in Settings.model_fields:
        raise Invalid(f"no settings section {section}")
    else:
        old = before.extension(section, model)
    new = value if isinstance(value, model) else _validate(lambda: model.model_validate(value))
    if new == old and (section in SECTIONS or section in before.extensions):
        return before
    for fn in _checks.get(section, ()):
        _validate(lambda: fn(old, new))
    if section in SECTIONS:
        after = _validate(lambda: _with(before, {section: new}))
    else:
        after = _validate(lambda: _with(before, {"extensions": {**before.extensions,
                                                                section: new.model_dump(mode="json")}}))
    _save(after, {section}, by)
    forget()
    request_effects(*new.effects(old))
    return current()


def set_repos(repos: list, *, by: str) -> Settings:
    after = _validate(lambda: _with(current(), {"repos": tuple(repos)}))
    _save_repos(after, by)
    forget()
    return current()


def add_repo(repo: str, *, by: str) -> Settings:
    """Append one repo without replacing another admin's repo list."""
    _validate(lambda: _with(current(), {"repos": (*current().repos, repo)}))
    _append_repo(repo.lower(), by)
    forget()
    return current()


def history(limit: int = 100) -> list:
    return _history(limit)


def request_effects(*units: str):
    """Ask the host for these effects now: what a secret declares it affects
    (vault.Key.applies), or a restored installation."""
    for unit in units:
        _request(unit)


def pending_effects() -> list[str]:
    return _pending()


def _with(s: Settings, changes: dict) -> Settings:
    """A new Settings with the next revision, validated whole (model_copy
    would skip the validators)."""
    doc = s.model_dump()
    doc.update({k: (v.model_dump() if isinstance(v, BaseModel) else v) for k, v in changes.items()})
    doc["revision"] = s.revision + 1
    return Settings.model_validate(doc)


def _validate(make):
    try:
        return make()
    except (ValidationError, ValueError) as error:
        raise Invalid(str(error)) from error


SCHEMA = state.Schema(tables="""
create table if not exists config (
    section text primary key, value text not null, updated_by text not null,
    updated_at text not null default current_timestamp);
create table if not exists config_history (
    id integer primary key, section text not null, value text not null, by text not null,
    at text not null default current_timestamp);
create table if not exists repos (
    repo text primary key, position integer not null default 0,
    added_by text not null default 'install', added_at text not null default current_timestamp);
""")


def opened_ports(s: Settings) -> tuple[int, ...] | Literal["all"]:
    """The ports the host accepts from anywhere: the network's, and what the
    domain's certificate needs."""
    ports = s.network.public_ports
    return "all" if ports == "all" else tuple(dict.fromkeys((*ports, *s.domain.needs_ports())))


def shell_lines(s: Settings, extensions: dict | None = None) -> list[str]:
    """`python -m sbx_control.settings shell`: the keys tls.sh, host-install.sh
    and sbx-host-guard read, then <extension>_<field> for each extension's
    section (a connector's host script reads its own)."""
    import shlex
    public = opened_ports(s)
    preview_ports = [f'{s.network.ip_preview_first_port}:{s.network.ip_preview_last_port}'] \
        if s.domain.address and not s.domain.name else []
    pairs = {"domain": s.domain.name, "login": s.base_url.removeprefix("https://"),
             "bootstrap_address": s.domain.address, "email": s.domain.acme_email,
             "dns": s.domain.acme, "backup_every_hours": str(s.backup.every_hours or ""),
             "sandbox_cidr": s.network.sandbox_cidr,
             "host_ports": " ".join(map(str, [*s.network.host_ports, *preview_ports])),
             "public_ports": public if public == "all" else " ".join(map(str, [*public, *preview_ports])),
             "services": " ".join(f"{n}:{u}" for n, u in s.proxied.services.items()),
             "internal_cidrs": " ".join(s.network.internal_cidrs)}
    for name, section in (extensions or {}).items():
        for field, value in section.model_dump().items():
            if isinstance(value, (str, int, float, bool)):
                pairs[f"{name.replace('-', '_')}_{field}"] = ("1" if value else "") if isinstance(value, bool) else str(value)
    return [f"{k}={shlex.quote(v)}" for k, v in pairs.items()]


def _revision() -> int:
    try:
        return int(REVISION.get("0"))
    except sqlite3.OperationalError:  # a host whose database is not made yet: the defaults
        return -1


def _load() -> Settings:
    try:
        with state.db() as conn:
            rows = dict(conn.execute("select section, value from config").fetchall())
            repos = [r[0] for r in conn.execute("select repo from repos order by position, repo")]
            rev = int(REVISION.get("0", conn=conn))
    except sqlite3.OperationalError:
        return Settings()
    return Settings.model_validate({**{s: json.loads(v) for s, v in rows.items() if s in SECTIONS},
                                    "extensions": {s: json.loads(v) for s, v in rows.items() if s not in SECTIONS},
                                    "repos": repos, "revision": rev})


def _value(s: Settings, section: str) -> str:
    found = getattr(s, section).model_dump(mode="json") if section in SECTIONS else s.extensions[section]
    return json.dumps(found)


def _save(s: Settings, sections: set, by: str):
    with state.db() as conn:
        conn.execute("begin")
        for section in sections:
            value = _value(s, section)
            conn.execute("insert into config (section, value, updated_by) values (?, ?, ?) on conflict (section) "
                         "do update set value = excluded.value, updated_by = excluded.updated_by, "
                         "updated_at = current_timestamp", (section, value, by))
            conn.execute("insert into config_history (section, value, by) values (?, ?, ?)", (section, value, by))
        REVISION.set(s.revision, conn=conn)
        conn.execute("commit")


def _save_repos(s: Settings, by: str):
    with state.db() as conn:
        conn.execute("begin")
        conn.execute("delete from repos")
        for i, repo in enumerate(s.repos):
            conn.execute("insert into repos (repo, position, added_by) values (?, ?, ?)", (repo, i, by))
        conn.execute("insert into config_history (section, value, by) values ('repos', ?, ?)",
                     (json.dumps(list(s.repos)), by))
        REVISION.set(s.revision, conn=conn)
        conn.execute("commit")


def _append_repo(repo: str, by: str):
    with state.db() as conn:
        conn.execute("begin immediate")
        rows = [r[0] for r in conn.execute("select repo from repos order by position, repo")]
        if repo not in rows:
            position = conn.execute("select coalesce(max(position), -1) + 1 from repos").fetchone()[0]
            conn.execute("insert into repos (repo, position, added_by) values (?, ?, ?)", (repo, position, by))
            conn.execute("insert into config_history (section, value, by) values ('repos', ?, ?)",
                         (json.dumps([*rows, repo]), by))
            REVISION.set(int(REVISION.get("0", conn=conn)) + 1, conn=conn)
        conn.execute("commit")


def _history(limit: int = 100) -> list:
    with state.db() as conn:
        return [{"section": s, "by": by, "at": at} for s, by, at in conn.execute(
            "select section, by, at from config_history order by id desc limit ?", (limit,))]


def _request(unit: str):
    try:
        folder = paths.run() / "apply"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / str(unit)).write_text(f"{time.time():.0f}\n")
    except OSError as error:  # no /run/sbx (a development machine): the effect is the operator's
        print(f"settings: could not request {unit}: {error}", file=sys.stderr, flush=True)


def _pending() -> list[str]:
    folder = paths.run() / "apply"
    return sorted(p.name for p in folder.glob("*")) if folder.exists() else []
