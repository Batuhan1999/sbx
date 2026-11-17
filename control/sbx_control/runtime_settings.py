"""A runtime's settings, as admins see and change them: each backend's form
(fields, presets), what the web app shows of every runtime (available), why
one can't be edited or removed now (Blocker), and a change planned as an
Edit of /etc/sbx/runtime.env: checked against the backend's settings and
the provider's own checks, the secrets sealed into the store.

prepare() is sbx-control's half (checked, nothing written, a secret as a
store reference); add, remove and set_default plan the edit the root side
makes (runtime_changes.perform writes it, confirms it, or puts the file
back). This module names no runtime."""
from __future__ import annotations

import dataclasses
import secrets
from typing import Callable

from . import runtime
from .runtimes import Conflict, Refused

LOCAL_STORE = ("this installation's secret store is still the local one, and a runtime's keys from the web app go "
               "only to the managed store (Infisical): set up the secret store first (sbx-admin store enable), or "
               "on the server: sbx-admin runtime add KIND (the key on stdin)")
LAST_RUNTIME = ("{kind} is the only runtime enabled here; add another first: sbx runtimes add KIND, or Settings → "
                "Sandbox runtimes (on the server: sbx-admin runtime add KIND)")
CUSTOM = "custom"


def fields(backend) -> dict:
    """The backend's settings by key (Runtime.settings)."""
    return {f.key: f for f in backend.settings}


def _raw(backend) -> dict:
    """runtime.env's values of the backend's keys as written (a secret as its store reference)."""
    from . import vault
    return vault._runtime_file(backend.credential_keys)


def plan_of(backend, raw: dict | None = None) -> dict | None:
    """The backend's preset its limits match now ({name, title}), {name:
    "custom", title: "Custom limits"} when none does; None for a backend
    without presets. raw: runtime.env's values (read when not given)."""
    if not backend.presets:
        return None
    found = fields(backend)
    raw = _raw(backend) if raw is None else raw
    effective = {key: raw.get(key) or field.default for key, field in found.items()}
    for preset in backend.presets:
        if preset.matches(effective):
            return {"name": preset.name, "title": preset.title}
    return {"name": CUSTOM, "title": "Custom limits"}


# Why not now


@dataclasses.dataclass(frozen=True)
class Blocker:
    """Why a runtime can't be edited or removed now: code for clients to
    branch on, message for people, the sandboxes in the way (has_sandboxes)."""
    code: str   # host, local_store, needs_domain; host, last_runtime, unlisted, has_sandboxes
    message: str
    sandboxes: tuple = ()

    def view(self) -> dict:
        return {"code": self.code, "message": self.message, "sandboxes": list(self.sandboxes)}


def _needs_domain(backend) -> str:
    return (f"{backend.title}'s sandboxes call this server back at its login address: connect a domain first "
            "(the server address step, or Settings → General)")


def edit_blocker(kind: str, backend, *, on: bool) -> Blocker | None:
    """Why the web app can't add or change the runtime now: install.sh's, the
    secret store still the local one, or (adding) no domain yet."""
    from . import settings, vault
    if backend.host:
        return Blocker("host", f"{backend.title} is set up by install.sh on this server")
    if not vault.sealing():
        return Blocker("local_store", LOCAL_STORE.replace("KIND", kind))
    if not on and not settings.current().domain.name:
        return Blocker("needs_domain", _needs_domain(backend))
    return None


def _listing(kind: str) -> list | Exception:
    """The names of the sandboxes the runtime holds, or why it did not say."""
    try:
        listed = runtime.complete(runtime.sandboxes(kind=kind))
    except Exception as error:
        return error
    return sorted((i.metadata or {}).get("sbx") or i.sandbox_id for i in listed)


def _unconfigurable(kind: str) -> str:
    """Why the enabled runtime of kind can't be configured from runtime.env
    now (its key missing, its reference not resolving): "" when it can. It is
    configured with what it reads, so a listing that follows uses it."""
    from . import vault
    backend = runtime.get(kind)
    try:
        backend.configure(vault.runtime(backend.credential_keys))
    except (Exception, SystemExit) as error:
        return str(error) if isinstance(error, SystemExit) else f"{type(error).__name__}: {error}"
    return ""


def remove_blocker(kind: str, *, host_ok: bool = False, listed: list | Exception | None = None) -> Blocker | None:
    """Why the enabled runtime can't be removed now: install.sh's (unless
    host_ok: sbx-admin may), the last one, its sandboxes (listed: the
    listing already made), or a listing that failed. A runtime that can't be
    configured lists nothing and may go. Refused when it is not enabled."""
    enabled = runtime.enabled()
    if kind not in enabled:
        raise Refused(f"the {kind} runtime is not enabled here (enabled: {', '.join(enabled) or 'none'})")
    backend = runtime.get(kind)
    if backend.host and not host_ok:
        return Blocker("host", f"{backend.title} is set up by install.sh on this server; remove it there: "
                               f"sbx-admin runtime remove {kind}")
    if len(enabled) == 1:
        return Blocker("last_runtime", LAST_RUNTIME.format(kind=kind))
    listed = _listing(kind) if listed is None else listed
    if isinstance(listed, Exception):
        if _unconfigurable(kind):
            return None
        return Blocker("unlisted", f"{backend.title} did not say which sandboxes it holds "
                                   f"({type(listed).__name__}: {listed})")
    if listed:
        return Blocker("has_sandboxes", f"{len(listed)} sandbox{'es' if len(listed) > 1 else ''} run on "
                       f"{backend.title} ({', '.join(listed)}): delete them first (sbx down NAME, or the web app)",
                       tuple(listed))
    return None


def refuse(blocker: Blocker | None) -> None:
    """A blocker as the refusal the API answers: 400 for a listing that failed, else 409."""
    if blocker is None:
        return
    if blocker.code == "unlisted":
        raise Refused(f"{blocker.message}; nothing was changed")
    raise Conflict(blocker.message)


# What the web app shows


def available() -> list:
    """GET /api/runtimes/available: every runtime an installed package
    provides, enabled or not: whether this host can run it (can_run, why_not),
    its settings form (settings, presets), its non-secret values now and
    which secrets are set (never a secret's value), why the web app can't
    add or change it (edit_blocker) and, for an enabled one, the sandboxes on
    it and why it can't be removed (remove_blocker)."""
    from . import setup
    facts = setup.host_facts()
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
        raw = _raw(backend) if on else {}
        why_not = backend.can_run(facts)
        blocker = edit_blocker(kind, backend, on=on)
        entry = {"kind": kind, "title": backend.title, "host": backend.host, "enabled": on, "default": kind == first,
                 "can_run": not why_not, "why_not": why_not, "managed_by": "install.sh" if backend.host else "",
                 "edit_blocker": blocker.view() if blocker else None,
                 "settings": [f.view() for f in found.values()],
                 "presets": [p.view() for p in backend.presets],
                 "plan": plan_of(backend, raw) if on else None,
                 "values": {k: raw.get(k, "") for k, f in found.items() if not f.secret},
                 "secrets_set": [k for k, f in found.items() if f.secret and raw.get(k)],
                 "facts": {k: facts[k] for k in ("kvm", "memory_gb", "disk_free_gb") if k in facts}
                 if backend.host else {},
                 "sandboxes": [], "remove_blocker": None, "limit_hit": runtime.limit_hit(kind) if on else None}
        if on:
            listed = _listing(kind)
            entry["sandboxes"] = None if isinstance(listed, Exception) else listed
            removal = remove_blocker(kind, listed=listed)
            entry["remove_blocker"] = removal.view() if removal else None
        out.append(entry)
    return out


# A change, planned


@dataclasses.dataclass
class Edit:
    """An edit of runtime.env, not yet made: the keys it writes (None removes
    one), the store references it makes (discarded when it is not confirmed)
    and those it no longer uses (discarded once it is), and what it did, said
    once it is made (says)."""
    writes: dict
    says: Callable[[], str]
    made: list = dataclasses.field(default_factory=list)
    stale: list = dataclasses.field(default_factory=list)


def _references(values) -> list:
    from . import secret_store
    return [v for v in values if v and secret_store.is_reference(v)]


def _runtimes_now() -> str:
    return f"the runtimes: {', '.join(runtime.enabled())} (default {runtime.default()})"


def _settable(kind: str):
    """A new backend of kind that an admin may add or change: a provider's,
    not one on this host (install.sh's)."""
    if kind not in runtime.kinds():
        raise Refused(f"there is no runtime {kind}; the runtimes here are {', '.join(runtime.kinds())}")
    backend = runtime.make(kind)
    if backend.host:
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
            if not runtime.base.HOSTNAME.fullmatch(value):
                raise Refused(f"{field.label} ({key}) is a host name such as example.com, not {value!r}")
        if value != current.get(key):
            out[key] = value
    # The stored secret never goes where a setting that says where it is sent now points.
    secret = backend.secret_key
    moved = [found[k] for k in out if found[k].rekey]
    if moved and secret and current.get(secret) and not out.get(secret):
        raise Refused(f"{moved[0].label} changes where {backend.title}'s {found[secret].label} is sent: type the "
                      f"{found[secret].label} again (the one set is never sent to a new {moved[0].label.lower()})")
    return out


def _check(backend, kind: str, merged: dict) -> None:
    """The backend configured with merged and its own checks against the
    provider: Refused, naming what failed, else nothing."""
    from . import secret_store
    secret = backend.secret_key
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


def _validated(backend, kind: str, values: dict, current: dict, *, changing: bool) -> dict:
    """values checked against the settings and, merged with current, by the provider: the changes."""
    changes = _changes(backend, values, current)
    if not changes and changing:
        raise Refused("nothing to change: the settings are as they were")
    _check(backend, kind, {k: v for k, v in {**current, **changes}.items() if v})
    return changes


def with_plan(backend, plan: str | None, values: dict, current: dict, *, adding: bool) -> dict:
    """values with a plan's limits: the ones it fixes, and the ones it leaves
    adjustable at their values now (changing) or the plan's own; values may
    set only those it leaves adjustable. No plan, or custom: values as they are."""
    if not plan or plan == CUSTOM:
        return values
    preset = next((p for p in backend.presets if p.name == plan), None)
    if preset is None:
        names = [p.name for p in backend.presets] + [CUSTOM]
        raise Refused(f"{backend.title} has no plan {plan}; its plans: {', '.join(names)}")
    by_key, adjustable = preset.by_key(), preset.adjustable_keys()
    fixed = [key for key in values if key in by_key and key not in adjustable]
    if fixed:
        raise Refused(f"the {preset.title} plan fixes {', '.join(fixed)}; choose the {CUSTOM} plan to set it")
    planned = {key: (current.get(key) if not adding and key in adjustable else "") or value
               for key, value in by_key.items()}
    return {**planned, **values}


def prepare(kind: str, values: dict, *, adding: bool, plan: str | None = None) -> dict:
    """A change from the web app, checked as add() will make it but
    nothing written: the runtime (a provider's), enabled or not as asked,
    its edit_blocker (the store, the domain), the values with the plan's
    limits against its settings, and the provider's own checks with them.
    Returns the values to queue for the root side (runtime_changes): a
    secret sealed into the store as a reference, never its value."""
    from . import secret_store, vault
    backend = _settable(kind)
    on = kind in runtime.enabled()
    if adding and on:
        raise Conflict(f"{backend.title} is enabled already; change its settings instead")
    if not adding and not on:
        raise Refused(f"the {kind} runtime is not enabled here; add it first")
    refuse(edit_blocker(kind, backend, on=on))
    # A value from the API is the value itself: a reference into the store would be unsealed
    # and sent to the provider (any secret of the installation, to a domain the caller names).
    sent = [key for key, value in (values or {}).items() if isinstance(value, str) and secret_store.is_reference(value.strip())]
    if sent:
        raise Refused(f"{', '.join(sent)}: a reference into the secret store is not a value; send the value itself")
    current = vault.runtime(backend.credential_keys) if on else {}
    changes = _validated(backend, kind, with_plan(backend, plan, values or {}, current, adding=adding), current,
                         changing=not adding)
    found = fields(backend)
    return {key: secret_store.seal(value, key=f"runtime/{key}/{secrets.token_hex(6)}")
            if found[key].secret and value else value for key, value in changes.items()}


def add(kind: str, values: dict) -> Edit:
    """Enable a runtime that runs elsewhere (a provider) with its settings
    (values by key: Runtime.settings), or change an enabled one's: checked
    first (its configure, then its own checks against the provider). A
    secret goes into the store under a new reference once that is Infisical
    (one sealed already, from the web app's queue, is written as it is); the
    old one goes only when the change is confirmed. A runtime on this host is
    install.sh's (it sets up the host side)."""
    from . import secret_store, vault
    backend = _settable(kind)
    enabled = runtime.enabled()
    raw = _raw(backend)
    changes = _validated(backend, kind, values, vault.runtime(backend.credential_keys), changing=kind in enabled)
    found = fields(backend)
    edit = Edit({}, lambda: "")
    for key, value in changes.items():
        if found[key].secret and value is not None:
            edit.stale += _references([raw.get(key)])
            if not secret_store.is_reference(value) and vault.sealing():
                # A new reference: the old one still serves until the change is confirmed.
                value = secret_store.seal(value, key=f"runtime/{key}/{secrets.token_hex(6)}")
            if secret_store.is_reference(value):
                edit.made.append(value)
        edit.writes[key] = value
    if kind not in enabled:
        edit.writes[runtime.ENABLED_KEY] = ",".join([*enabled, kind])
    what = (("has a new key" if set(changes) == {backend.secret_key} else "has new settings")
            if kind in enabled else "is enabled")
    edit.says = lambda: f"{backend.title} ({kind}) {what}; {_runtimes_now()}"
    return edit


def remove(kind: str) -> Edit:
    """Disable a runtime: refused while it has sandboxes (running or paused)
    or when it is the only one (remove_blocker). A provider's keys leave
    runtime.env (and, once the change is confirmed, the store); a runtime on
    this host keeps install.sh's. A default naming it goes, so the first
    that stays is the default."""
    unconfigurable = _unconfigurable(kind) if kind in runtime.enabled() else ""
    refuse(remove_blocker(kind, host_ok=True, listed=[] if unconfigurable else None))
    backend = runtime.get(kind)
    writes: dict = {runtime.ENABLED_KEY: ",".join(k for k in runtime.enabled() if k != kind)}
    if runtime.configuration()[runtime.DEFAULT_KEY] == kind:
        writes[runtime.DEFAULT_KEY] = None
    stale = []
    if not backend.host:
        stale = _references(_raw(backend).values())
        writes.update(dict.fromkeys(backend.credential_keys))
    unlisted = (f". Its sandboxes could not be listed ({unconfigurable}): any it still has at the provider were "
                "not seen and are left there" if unconfigurable else "")
    return Edit(writes, lambda: f"{backend.title} ({kind}) is disabled; {_runtimes_now()}{unlisted}", stale=stale)


def default_refused(kind: str) -> None:
    if kind not in runtime.enabled():
        raise Refused(f"the {kind} runtime is not enabled here (enabled: {', '.join(runtime.enabled()) or 'none'})")


def set_default(kind: str) -> Edit:
    """The runtime new sandboxes run on unless the request or the repo names another."""
    default_refused(kind)
    return Edit({runtime.DEFAULT_KEY: kind}, lambda: f"new sandboxes run on {runtime.title(kind)} ({kind}) unless "
                "the request or the repo names another")
