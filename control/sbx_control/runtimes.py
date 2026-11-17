"""Several sandbox runtimes, as the core sees them: which one a new
sandbox runs on (choose), a repo's own choice (setting), the runtimes a
repo's sandboxes use (used_by), what the API says about them (describe,
available), adding, changing and removing one (add, remove, set_default,
and prepare: a change from the web app checked before it is queued), and
`sbx-admin runtime verify`, which takes one through a sandbox's whole life.

The registry itself (which are enabled, the default, the backends) is
sbx_control.runtime's; this module names no runtime either."""
from __future__ import annotations

import dataclasses
import secrets
import sqlite3
import time
import urllib.request

from . import runtime, state
from .core import SandboxError

REPO_RUNTIME_SCHEMA = """
create table if not exists repo_runtimes (
    repo text primary key, runtime text not null, updated_by text not null,
    updated_at text not null default current_timestamp);
create table if not exists runtime_requests (
    repo text not null, runtime text not null, at text not null default current_timestamp,
    primary key (repo, runtime));
"""
# A create on a runtime the repo's sandboxes do not use yet keeps its
# templates there this long, so its first create can wait for their build.
REQUEST_HOURS = 24


class Refused(SandboxError):
    """The runtime asked for can't run the sandbox, or is no runtime here;
    the message says why (the API answers 400)."""


class Conflict(Refused):
    """Refused for what the installation holds now: the runtime has
    sandboxes, is the last one, is enabled already, or the secret store is
    still the local one (the API answers 409)."""


LOCAL_STORE = ("this installation's secret store is still the local one, and a runtime's keys from the web app go "
               "only to the managed store (Infisical): set up the secret store first (sbx-admin store enable), or "
               "on the server: sbx-admin runtime add KIND (the key on stdin)")


# A repo's own runtime


def setting(repo: str | None) -> str | None:
    """The runtime the repo's new sandboxes run on, as an admin set it; None: the installation's default."""
    if not repo:
        return None
    with state.db() as conn:
        row = conn.execute("select runtime from repo_runtimes where repo = ?", (repo.lower(),)).fetchone()
    return row[0] if row else None


def set_setting(repo: str, kind: str | None, *, by: str) -> str | None:
    """Set (or with None clear) the repo's runtime; Refused for one that is not enabled."""
    repo = repo.lower()
    with state.db() as conn:
        if kind is None:
            conn.execute("delete from repo_runtimes where repo = ?", (repo,))
            return None
        if kind not in runtime.enabled():
            raise Refused(f"no runtime {kind} is enabled here; the enabled ones are {', '.join(runtime.enabled())}")
        conn.execute("insert into repo_runtimes (repo, runtime, updated_by) values (?, ?, ?) on conflict (repo) do "
                     "update set runtime = excluded.runtime, updated_by = excluded.updated_by, "
                     "updated_at = current_timestamp", (repo, kind, by))
    return kind


def of_repo(repo: str | None) -> str:
    """The runtime the repo's templates and room are counted on: its setting
    while that runtime is enabled, else the default. A create with a setting
    that is not enabled is refused (choose), never moved to the default."""
    found = setting(repo)
    return found if found in runtime.enabled() else runtime.default()


def used_by(repo: str) -> set:
    """The enabled runtimes the repo's sandboxes use: its setting, the
    default, any a sandbox of it runs on, and any a create asked for in the
    last REQUEST_HOURS (its first sandbox there waits for the templates
    built on each of these)."""
    found = {of_repo(repo), runtime.default()}
    try:
        with state.db() as conn:
            found |= {row[0] for row in conn.execute(
                "select runtime from runtime_requests where repo = ? and at > datetime('now', ?)",
                (repo.lower(), f"-{REQUEST_HOURS} hours"))}
            found |= {row[0] for row in conn.execute(
                "select s.runtime from sandboxes s join sandbox_lifecycle l on l.name = s.name "
                "where json_extract(l.record, '$.repository') = ? and json_extract(l.record, '$.phase') != 'deleted' "
                "and s.runtime != ''", (repo.lower(),))}
    except sqlite3.OperationalError:  # before the first migrate
        pass
    return found & set(runtime.enabled())


# Choosing


def choose(repo: str | None, requested: str | None = None, size: dict | None = None) -> str:
    """The runtime of a new sandbox of the repo: the request's, else the
    repo's setting, else the default. Refused, naming the runtime and why,
    when it is not enabled, when managed servers are attached to the repo
    and its sandboxes don't reach this host's private network, or when the
    recipe's size (when given) is beyond its limits (on a runtime without
    private_networks: when a private target it uses has no endpoint, as
    endpoints.missing says); sbx never falls back to
    another runtime."""
    chosen = setting(repo)
    if not requested and chosen and chosen not in runtime.enabled():
        raise Refused(f"{repo}'s runtime setting is {chosen}, which is not enabled on this installation (enabled: "
                      f"{', '.join(runtime.enabled())}); change it on the repository's page or with "
                      f"PUT /api/repos/{repo}/runtime, or name a runtime in the request")
    if not runtime.enabled():
        raise Refused(runtime.NO_RUNTIME)
    kind = requested or of_repo(repo)
    if kind not in runtime.enabled():
        if kind in runtime.kinds():
            raise Refused(f"the {kind} runtime is not enabled on this installation (enabled: "
                          f"{', '.join(runtime.enabled())})")
        raise Refused(f"there is no runtime {kind} (enabled here: {', '.join(runtime.enabled())})")
    title = runtime.title(kind)
    if repo and not runtime.capabilities(kind).private_networks:
        from . import endpoints
        lacking = endpoints.missing(repo)
        if lacking:
            first = "" if endpoints.domain() else " (endpoints need a domain: connect one first)"
            raise Refused(f"{title} ({kind}) can't run {repo}'s sandboxes yet: they reach this host's private "
                          f"network (private_networks) only through endpoints, and "
                          f"{', '.join(what for what, _ in lacking)} {'has' if len(lacking) == 1 else 'have'} "
                          f"none; an admin adds {'it' if len(lacking) == 1 else 'them'}: "
                          f"{'; '.join(command for _, command in lacking)}{first}, or choose another runtime")
    if size and (why := runtime.beyond_limits(size, kind)):
        raise Refused(f"{title} ({kind}) can't run it: {why}")
    if repo:
        with state.db() as conn:   # keeps its templates on kind until its first sandbox there exists (used_by)
            conn.execute("insert into runtime_requests (repo, runtime) values (?, ?) on conflict (repo, runtime) "
                         "do update set at = current_timestamp", (repo.lower(), kind))
    return kind


# What the API says


def describe() -> dict:
    """GET /api/runtimes: every enabled runtime, the default first among equals."""
    first = runtime.default()
    return {"default": first, "runtimes": [_runtime(kind, kind == first) for kind in runtime.enabled()]}


def _runtime(kind: str, is_default: bool) -> dict:
    backend = runtime.get(kind)
    try:
        found = runtime.status(kind)
        status = {"state": found.state, "summary": found.summary, "running": (found.facts or {}).get("running")}
    except Exception as error:  # one runtime that does not answer must not hide the others
        status = {"state": "attention", "summary": f"{backend.title} did not answer: {type(error).__name__}: {error}"}
    try:
        limits = runtime.limits(kind).known()
    except Exception:
        limits = {}
    return {"kind": kind, "title": backend.title, "default": is_default, "host": bool(getattr(backend, "host", False)),
            "capabilities": dataclasses.asdict(backend.capabilities), "limits": limits, "status": status,
            "limit_hit": runtime.limit_hit(kind), "plan": plan_of(backend)}


def plan_of(backend, raw: dict | None = None) -> dict | None:
    """The backend's preset its limits match now ({name, title}), {name:
    "custom", title: "Custom limits"} when none does; None for a backend
    without presets. raw: runtime.env's values (read when not given)."""
    presets = getattr(backend, "presets", ())
    if not presets:
        return None
    from . import vault
    found = fields(backend)
    raw = vault._runtime_file(tuple(found)) if raw is None else raw
    effective = {key: raw.get(key) or field.default for key, field in found.items()}
    for preset in presets:
        if preset.matches(effective):
            return {"name": preset.name, "title": preset.title}
    return {"name": "custom", "title": "Custom limits"}


def test(kind: str) -> list:
    """POST /api/runtimes/{kind}/test, sbx runtimes test: an enabled runtime's
    own checks (its provider answers with the stored key), its limits, its
    base template (built, building or failed) and, when its checks pass and
    the base is built, whether its sandboxes reach this server. Each
    {name, ok, detail, fix, runtime}; Refused for a runtime not enabled."""
    from . import reachability, template_builds, templates
    from .connections.base import Check
    if kind not in runtime.enabled():
        raise Refused(f"the {kind} runtime is not enabled here (enabled: {', '.join(runtime.enabled()) or 'none'})")
    try:
        found = list(runtime.test(kind))
    except Exception as error:   # the backend's checks themselves failed: that is the answer
        found = [Check(f"{runtime.title(kind)} API", False, f"{type(error).__name__}: {error}", runtime=kind)]
    try:
        limits = runtime.limits(kind).known()
        found.append(Check("Limits", True, ", ".join(f"{k}={v}" for k, v in limits.items()) or "none stated",
                           runtime=kind))
    except Exception as error:
        found.append(Check("Limits", False, f"{type(error).__name__}: {error}", runtime=kind))
    row = template_builds.state_row(templates.BASE, kind)
    build = template_builds.status(templates.BASE, row, kind)
    if build == "built":
        found.append(Check("Base template", True, "built", runtime=kind))
    elif build == "failed":
        found.append(Check("Base template", False, f"its last build failed: {(row or {}).get('error') or '?'}",
                           f"build it again: sbx-admin jobs run template@{kind}:{templates.BASE}", runtime=kind))
    else:
        found.append(Check("Base template", True, "building" if build == "building" else "not built yet; the "
                           "scheduler builds it within minutes", runtime=kind))
    if build == "built" and all(c.ok for c in found):
        found.append(reachability.check(kind))
    return [{"name": c.name, "ok": c.ok, "detail": c.detail, "fix": c.fix, "runtime": c.runtime or kind} for c in found]


def fields(backend) -> dict:
    """The backend's settings by key (Runtime.settings); one that declares
    none has its secret_key as its only one; a runtime on this host has
    none (install.sh writes its keys)."""
    found = {f.key: f for f in getattr(backend, "settings", ())}
    secret = getattr(backend, "secret_key", "")
    if secret and secret not in found and not getattr(backend, "host", False):
        found = {secret: runtime.SettingField(secret, secret, secret=True), **found}
    return found


def available() -> list:
    """GET /api/runtimes/available: every runtime an installed package
    provides, enabled or not: whether this host can run it (can_run, why_not),
    its settings form (settings, presets), its non-secret values now and
    which secrets are set (never a secret's value), why the web app can't
    add or change it (not_editable: "" when it can; install.sh's on this
    host, or the secret store still the local one), and for an enabled one
    the sandboxes on it and why it can't be removed ("" when it can)."""
    from . import settings, setup, vault
    facts = setup.host_facts()
    sealing = vault.sealing()
    domain = bool(settings.current().domain.name)
    kinds_now, first = runtime.enabled(), runtime.default()
    out = []
    for kind in runtime.kinds():
        on = kind in kinds_now
        try:
            backend = runtime.get(kind) if on else runtime.make(kind)
        except Exception as error:   # a package that does not load: not offered
            print(f"runtime {kind}: not offered: {error!r}", flush=True)
            continue
        found = fields(backend)
        raw = vault._runtime_file(tuple(found)) if on else {}
        host = bool(getattr(backend, "host", False))
        why_not = backend.can_run(facts) if callable(getattr(backend, "can_run", None)) else ""
        needs_domain = not host and not on and not domain
        entry = {"kind": kind, "title": backend.title, "host": host, "enabled": on, "default": kind == first,
                 "can_run": not why_not, "why_not": why_not, "managed_by": "install.sh" if host else "",
                 "not_editable": (f"{backend.title} is set up by install.sh on this server" if host
                                  else LOCAL_STORE.replace("KIND", kind) if not sealing
                                  else _needs_domain(backend) if needs_domain else ""),
                 "needs_domain": needs_domain,
                 "settings": [f.view() for f in found.values()],
                 "presets": [p.view() for p in getattr(backend, "presets", ())],
                 "plan": plan_of(backend, raw) if on else None,
                 "values": {k: raw.get(k, "") for k, f in found.items() if not f.secret},
                 "secrets_set": [k for k, f in found.items() if f.secret and raw.get(k)],
                 "facts": {k: facts[k] for k in ("kvm", "memory_gb", "disk_free_gb") if k in facts} if host else {},
                 "sandboxes": [], "removable": "", "limit_hit": runtime.limit_hit(kind) if on else None}
        if on:
            failed = ""
            try:
                entry["sandboxes"] = _sandbox_names(kind)
            except Exception as error:
                entry["sandboxes"] = None
                failed = f"{backend.title} did not say which sandboxes it holds ({type(error).__name__})"
            entry["removable"] = _why_not_removable(kind, entry["sandboxes"], failed)
        out.append(entry)
    return out


def _sandbox_names(kind: str) -> list:
    listed = runtime.complete(runtime.sandboxes(kind=kind))
    return sorted((i.metadata or {}).get("sbx") or i.sandbox_id for i in listed)


def _why_not_removable(kind: str, names: list | None, failed: str = "") -> str:
    backend = runtime.get(kind)
    if getattr(backend, "host", False):
        return f"{backend.title} is set up by install.sh on this server"
    if len(runtime.enabled()) == 1:
        return f"{backend.title} is the only runtime here; add another first"
    if names is None:   # one that can't be configured lists nothing and may go (remove says so)
        return "" if _unconfigurable(kind) else failed
    if names:
        return (f"{len(names)} sandbox{'es' if len(names) > 1 else ''} run on {backend.title} ({', '.join(names)}): "
                "delete them first")
    return ""


# sbx-admin runtime add | remove | default: runtime.env's selection and keys


@dataclasses.dataclass
class Change:
    """An edit of runtime.env, not yet confirmed: what it did (said), the
    file as it was (before: None when there was none), the store references
    it no longer uses (stale: discarded once the new configuration is
    confirmed) and those it made (made: discarded when it is not).
    sbx-admin confirms it with platform.connect (admin.do_runtime_change)."""
    said: str
    before: str | None
    stale: list = dataclasses.field(default_factory=list)
    made: list = dataclasses.field(default_factory=list)

    def confirm(self) -> None:
        from . import secret_store
        for reference in self.stale:
            secret_store.discard(reference)

    def undo(self) -> None:
        """runtime.env as it was, and the references it made gone."""
        from . import paths, secret_store
        file = paths.runtime_env()
        if self.before is None:
            file.unlink(missing_ok=True)
        else:
            temporary = file.with_suffix(".new")
            temporary.touch(mode=0o600)
            temporary.write_text(self.before)
            temporary.replace(file)
        for reference in self.made:
            secret_store.discard(reference)


def _file_now() -> str | None:
    from . import paths
    file = paths.runtime_env()
    return file.read_text() if file.exists() else None


def _references(values) -> list:
    from . import secret_store
    return [v for v in values if v and secret_store.is_reference(v)]


def _needs_domain(backend) -> str:
    return (f"{backend.title}'s sandboxes call this server back at its login address: connect a domain first "
            "(the server address step, or Settings → General)")


def _settable(kind: str):
    """A new backend of kind that an admin may add or change: a provider's,
    not one on this host (install.sh's)."""
    if kind not in runtime.kinds():
        raise Refused(f"there is no runtime {kind}; the runtimes here are {', '.join(runtime.kinds())}")
    backend = runtime.make(kind)
    if getattr(backend, "host", False):
        raise Refused(f"{backend.title} ({kind}) runs on this host: install.sh enables it where the host can run it")
    return backend


def _changes(backend, values: dict, current: dict) -> dict:
    """What values change, checked against the backend's settings: key ->
    the new text, None to remove it (back to its default). A secret left
    empty keeps its value; an int is a whole number, at least 1."""
    found = fields(backend)
    out = {}
    for key, value in (values or {}).items():
        field = found.get(key)
        if field is None:
            raise Refused(f"{backend.title} has no setting {key}; its settings: {', '.join(found)}")
        value = "" if value is None else str(value).strip()
        if field.secret:
            if value:
                out[key] = value
            continue
        if not value:
            if current.get(key):
                out[key] = None
            continue
        if field.kind == "int" and not (value.isdigit() and int(value) >= 1):
            raise Refused(f"{field.label} ({key}) is a whole number, at least 1, not {value!r}")
        if field.kind == "int" and ((field.min is not None and int(value) < field.min)
                                    or (field.max is not None and int(value) > field.max)):
            raise Refused(f"{field.label} ({key}) is from {field.min} to {field.max}"
                          f"{' ' + field.unit if field.unit else ''}, not {value}")
        if field.kind == "hostname":
            value = value.lower().rstrip(".")
            if not HOSTNAME.fullmatch(value):
                raise Refused(f"{field.label} ({key}) is a host name such as example.com, not {value!r}")
        if value != current.get(key):
            out[key] = value
    # The stored secret never goes where a setting that says where it is sent now points.
    secret = getattr(backend, "secret_key", "")
    moved = [found[k] for k in out if found[k].rekey]
    if moved and secret and current.get(secret) and not out.get(secret):
        raise Refused(f"{moved[0].label} changes where {backend.title}'s {found[secret].label} is sent: type the "
                      f"{found[secret].label} again (the one set is never sent to a new {moved[0].label.lower()})")
    return out


HOSTNAME = runtime.base.HOSTNAME


def _check(backend, kind: str, merged: dict) -> None:
    """The backend configured with merged and its own checks against the
    provider: Refused, naming what failed, else nothing."""
    from . import secret_store
    secret = getattr(backend, "secret_key", "")
    if secret and not merged.get(secret):
        label = fields(backend)[secret].label
        raise Refused(f"{backend.title} needs its {label} ({secret}); on the server it comes on stdin "
                      f"(sbx-admin runtime add {kind})")
    plain = {k: secret_store.unseal(v) if secret_store.is_reference(v) else v for k, v in merged.items()}
    try:
        backend.configure(plain)
    except SystemExit as error:
        raise Refused(str(error)) from None
    failed = [c for c in backend.test() if not c.ok]
    if failed:
        raise Refused(f"{backend.title} did not take the settings: "
                      + "; ".join(f"{c.name}: {c.detail}" for c in failed) + "; nothing was changed")


def prepare(kind: str, values: dict, *, adding: bool) -> dict:
    """A change from the web app, checked as add() will make it but
    nothing written: the runtime (a provider's), enabled or not as asked,
    the installation's domain (a provider's sandboxes call it back), the
    store (Infisical: Conflict on the local one), the values against its
    settings, and the provider's own checks with them. Returns the values to
    queue for the root side (runtime_changes): a secret sealed into the store
    as a reference, never its value."""
    from . import secret_store, settings, vault
    backend = _settable(kind)
    on = kind in runtime.enabled()
    if adding and on:
        raise Conflict(f"{backend.title} is enabled already; change its settings instead")
    if not adding and not on:
        raise Refused(f"the {kind} runtime is not enabled here; add it first")
    if not vault.sealing():
        raise Conflict(LOCAL_STORE.replace("KIND", kind))
    if adding and not settings.current().domain.name:
        raise Conflict(_needs_domain(backend))
    # A value from the API is the value itself: a reference into the store would be unsealed
    # and sent to the provider (any secret of the installation, to a domain the caller names).
    sent = [key for key, value in (values or {}).items() if isinstance(value, str) and secret_store.is_reference(value.strip())]
    if sent:
        raise Refused(f"{', '.join(sent)}: a reference into the secret store is not a value; send the value itself")
    current = vault.runtime(backend.credential_keys) if on else {}
    changes = _changes(backend, values, current)
    if not changes and not adding:
        raise Refused("nothing to change: the settings are as they were")
    _check(backend, kind, {k: v for k, v in {**current, **changes}.items() if v})
    found = fields(backend)
    return {key: secret_store.seal(value, key=f"runtime/{key}/{secrets.token_hex(6)}")
            if found[key].secret and value else value for key, value in changes.items()}


def add(kind: str, values) -> Change:
    """Enable a runtime that runs elsewhere (a provider) with its settings
    (values by key: Runtime.settings; a string is its secret_key's value, as
    sbx-admin runtime add reads it from stdin), or change an enabled one's:
    checked first (its configure, then its own checks against the provider),
    then written into runtime.env. A secret goes into the store under a new
    reference once that is Infisical (one sealed already, from the web app's
    queue, is written as it is); the old one goes only when the change is
    confirmed. A runtime on this host is install.sh's (it sets up the host
    side)."""
    from . import secret_store, vault
    backend = _settable(kind)
    if isinstance(values, str):
        values = {backend.secret_key: values}
    enabled = runtime.enabled()
    raw = vault._runtime_file(backend.credential_keys)
    current = vault.runtime(backend.credential_keys)
    changes = _changes(backend, values, current)
    if not changes and kind in enabled:
        raise Refused("nothing to change: the settings are as they were")
    _check(backend, kind, {k: v for k, v in {**current, **changes}.items() if v})
    found = fields(backend)
    change = Change("", _file_now())
    written = {}
    for key, value in changes.items():
        if not found[key].secret or value is None:
            written[key] = value
            continue
        change.stale += _references([raw.get(key)])
        if secret_store.is_reference(value):
            change.made.append(value)
        elif vault.sealing():   # a new reference: the old one still serves until the change is confirmed
            value = secret_store.seal(value, key=f"runtime/{key}/{secrets.token_hex(6)}")
            change.made.append(value)
        written[key] = value
    if kind not in enabled:
        written[runtime.ENABLED_KEY] = ",".join([*enabled, kind])
    try:
        vault.edit_runtime_file(written)
        what = ("has a new key" if set(changes) == {backend.secret_key} else "has new settings") \
            if kind in enabled else "is enabled"
        change.said = (f"{backend.title} ({kind}) {what}; the runtimes: {', '.join(runtime.enabled())} "
                       f"(default {runtime.default()})")
    except BaseException:
        change.undo()
        raise
    return change


LAST_RUNTIME = ("{kind} is the only runtime enabled here; add another first: sbx runtimes add KIND, or Settings → "
                "Sandbox runtimes (on the server: sbx-admin runtime add KIND)")


def _unconfigurable(kind: str) -> str:
    """Why the enabled runtime of kind can't be configured from runtime.env
    now (its key missing, its reference not resolving): "" when it can. It is
    configured with what it reads, so a listing that follows uses it."""
    from . import vault
    backend = runtime.get(kind)
    try:
        backend.configure(vault.runtime(backend.credential_keys))
    except (Exception, SystemExit) as error:
        return f"{type(error).__name__}: {error}" if not isinstance(error, SystemExit) else str(error)
    return ""


def removal_refused(kind: str) -> str:
    """Why the runtime can't be removed now (Refused; Conflict for what the
    installation holds: it is the last one, or sandboxes run on it). Else ""
    or, for a runtime that can't be configured (so lists nothing), why: the
    removal goes ahead and says so."""
    enabled = runtime.enabled()
    if kind not in enabled:
        raise Refused(f"the {kind} runtime is not enabled here (enabled: {', '.join(enabled) or 'none'})")
    if len(enabled) == 1:
        raise Conflict(LAST_RUNTIME.format(kind=kind))
    backend = runtime.get(kind)
    unconfigurable = _unconfigurable(kind)
    if unconfigurable:
        return unconfigurable
    try:
        listed = runtime.complete(runtime.sandboxes(kind=kind))
    except Exception as error:
        raise Refused(f"{backend.title} did not say which sandboxes it holds ({type(error).__name__}: {error}); "
                      "nothing was changed") from None
    if listed:
        names = ", ".join(sorted((i.metadata or {}).get("sbx") or i.sandbox_id for i in listed))
        raise Conflict(f"{len(listed)} sandbox{'es' if len(listed) > 1 else ''} run on {backend.title} ({names}): "
                       "delete them first (sbx down NAME, or the web app)")
    return ""


def remove(kind: str) -> Change:
    """Disable a runtime: refused while it has sandboxes (running or paused)
    or when it is the only one (removal_refused). A provider's keys leave
    runtime.env (and, once the change is confirmed, the store); a runtime on
    this host keeps install.sh's. A default naming it goes, so the first
    that stays is the default."""
    from . import vault
    unlisted = removal_refused(kind)
    enabled = runtime.enabled()
    backend = runtime.get(kind)
    values: dict = {runtime.ENABLED_KEY: ",".join(k for k in enabled if k != kind)}
    if runtime.configuration()[runtime.DEFAULT_KEY] == kind:
        values[runtime.DEFAULT_KEY] = None
    change = Change("", _file_now())
    if not getattr(backend, "host", False):
        change.stale = _references(vault._runtime_file(backend.credential_keys).values())
        values.update(dict.fromkeys(backend.credential_keys))
    try:
        vault.edit_runtime_file(values)
        change.said = (f"{backend.title} ({kind}) is disabled; the runtimes: {', '.join(runtime.enabled())} "
                       f"(default {runtime.default()})"
                       + (f". Its sandboxes could not be listed ({unlisted}): any it still has at the provider were "
                          "not seen and are left there" if unlisted else ""))
    except BaseException:
        change.undo()
        raise
    return change


def default_refused(kind: str) -> None:
    if kind not in runtime.enabled():
        raise Refused(f"the {kind} runtime is not enabled here (enabled: {', '.join(runtime.enabled()) or 'none'})")


def is_default(kind: str) -> bool:
    return kind in runtime.enabled() and runtime.default() == kind


def set_default(kind: str) -> Change:
    """The runtime new sandboxes run on unless the request or the repo names another."""
    from . import vault
    default_refused(kind)
    change = Change("", _file_now())
    try:
        vault.edit_runtime_file({runtime.DEFAULT_KEY: kind})
    except BaseException:
        change.undo()
        raise
    change.said = f"new sandboxes run on {runtime.title(kind)} ({kind}) unless the request or the repo names another"
    return change


# sbx-admin runtime verify

VERIFY_PORT = 8765
VERIFY_FILE = "/home/user/.sbx-verify"   # what every runtime's pause keeps (Boat's stop drops /tmp)
VERIFY_TIMEOUT = 600


def _fetch(url: str, host: str, headers: dict | None = None) -> str:
    """GET url with this Host (and these headers: a sandbox's token), as nginx
    sends a preview's request to a runtime's ingress."""
    request = urllib.request.Request(url, headers={"Host": host, **(headers or {})})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode(errors="replace")


def verify(kind: str | None = None, say=print) -> bool:
    """Take a throwaway sandbox on the runtime of kind (None: the default)
    through its life: create it from the base template, run a command, write
    and read a file, pause and resume it, reach a preview port through the
    gate's route and the runtime's ingress, delete it. Each step is said as
    it ends; what it made is deleted whatever happens. True when every step
    passed."""
    from . import gate, template_builds, templates
    kind = kind or runtime.default()
    say(f"verifying {runtime.title(kind)} ({kind})")
    try:
        template_builds.built(templates.BASE, kind)
    except template_builds.NotBuilt:
        # Not a failure of the runtime: there is nothing to start a sandbox from yet.
        row = template_builds.state_row(templates.BASE, kind)
        build = template_builds.status(templates.BASE, row, kind)
        if build == "building":
            say(f"  ..  the base template is building on {runtime.title(kind)}; try again when it's built "
                f"(sbx-admin jobs shows its progress)")
        elif build == "failed":
            say(f"  NO  the base template's build failed on {runtime.title(kind)}: {(row or {}).get('error') or '?'}"
                f"; build it again: sbx-admin jobs run template@{kind}:{templates.BASE}")
        else:
            say(f"  ..  the base template is not built on {runtime.title(kind)} yet; it builds by itself shortly, "
                f"or start it now: sbx-admin jobs run template@{kind}:{templates.BASE}")
        say("not verified")
        return False
    made = None
    passed = True

    def step(name, work):
        nonlocal passed
        if not passed:
            say(f"  --  {name}: skipped")
            return None
        began = time.monotonic()
        try:
            detail = work()
        except Exception as error:
            passed = False
            say(f"  NO  {name}: {type(error).__name__}: {error}")
            return None
        say(f"  ok  {name}: {detail} ({time.monotonic() - began:.1f}s)")
        return detail

    token = secrets.token_hex(8)
    try:
        handle = None

        def create():
            nonlocal handle, made
            template = template_builds.built(templates.BASE, kind)
            handle = runtime.create(template, timeout=VERIFY_TIMEOUT, metadata={"sbx_check": "verify"},
                                    on_timeout="kill", kind=kind)
            made = handle.sandbox_id
            return f"{made} from {template}"
        step("create from the base template", create)

        def command():
            out = handle.commands.run(f"echo {token}", timeout=30).stdout.strip()
            if out != token:
                raise AssertionError(f"echo printed {out[:80]!r}")
            return "echo answered"
        step("run a command", command)

        def files():
            handle.files.write(VERIFY_FILE, token)
            read = handle.files.read(VERIFY_FILE)
            shown = handle.commands.run(f"cat {VERIFY_FILE}", timeout=30).stdout
            if read != token or shown != token:
                raise AssertionError(f"read back {read[:80]!r}, cat showed {shown[:80]!r}")
            return VERIFY_FILE
        step("write and read a file", files)

        def pause():
            runtime.pause(made)
            found = runtime.info(made).state
            if found != runtime.PAUSED:
                raise AssertionError(f"the runtime lists it {found}")
            return "paused"
        step("pause", pause)

        def resume():
            nonlocal handle
            handle = runtime.connect(made, timeout=VERIFY_TIMEOUT)
            if runtime.info(made).state != runtime.RUNNING:
                raise AssertionError("the runtime does not list it running")
            if handle.files.read(VERIFY_FILE) != token:
                raise AssertionError(f"{VERIFY_FILE} did not survive the pause")
            return "running, its file kept"
        step("resume", resume)

        def preview():
            handle.commands.run(f"cd {VERIFY_FILE.rsplit('/', 1)[0]} && python3 -m http.server {VERIFY_PORT} --bind 0.0.0.0 >/dev/null 2>&1",
                                background=True, timeout=0).disconnect()
            route = gate.route(VERIFY_PORT, made)
            if route.get("X-Sbx-Runtime") != kind:
                raise AssertionError(f"the gate routes it to {route.get('X-Sbx-Runtime')!r}")
            ingress = runtime.get(kind).ingress
            # An https ingress is asked by the sandbox's own host (SNI), as nginx does.
            base = f"https://{route['X-Sbx-Upstream']}" if ingress.startswith("https://") else ingress
            traffic = route.get(gate.TOKEN)
            backend = runtime.get(kind)
            cookie = getattr(backend, "upstream_token_cookie", None)
            extra = ({} if not traffic else {"headers": {"Cookie": f"{cookie}={traffic}"}} if cookie
                     else {"headers": {backend.upstream_token_header: traffic}})
            last = None
            for _ in range(20):
                try:
                    body = _fetch(f"{base}/{VERIFY_FILE.rsplit('/', 1)[1]}", route["X-Sbx-Upstream"], **extra)
                    if body.strip() == token:
                        return f"{route['X-Sbx-Upstream']} through {ingress}"
                    last = f"answered {body[:80]!r}"
                except Exception as error:
                    last = f"{type(error).__name__}: {error}"
                time.sleep(1)
            raise AssertionError(f"port {VERIFY_PORT} through {ingress}: {last}")
        step("reach a preview port through the gate", preview)

        def delete():
            nonlocal made
            runtime.kill(made)
            gone, made = made, None
            if any(i.sandbox_id == gone for i in runtime.sandboxes(None, [runtime.RUNNING], kind=kind)):
                raise AssertionError("the runtime still lists it running")
            return gone
        before = passed
        passed = True     # what it made is deleted whatever failed before
        if made:
            step("delete", delete)
        passed = passed and before
    finally:
        if made:
            try:
                runtime.kill(made)
                say(f"  deleted {made}")
            except Exception as error:
                say(f"  could not delete {made}: {error}; it times out on its own")
    say("passed" if passed else "failed")
    return passed


SCHEMA = state.Schema(tables=REPO_RUNTIME_SCHEMA)
