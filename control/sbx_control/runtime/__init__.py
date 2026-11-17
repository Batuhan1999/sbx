"""The sandbox runtimes this installation runs on, and the one way to them.

sbx's core and its extensions reach VMs only through these functions;
only a runtime's own package names its SDK (a test enforces it), and the
core names no backend (a test enforces that too).

An installation enables its runtimes in /etc/sbx/runtime.env: SBX_RUNTIMES
lists them in order (missing: embed, as before several runtimes; present and
empty: none yet, until an admin adds one) and SBX_RUNTIME_DEFAULT picks the
one new sandboxes run on (missing: the first). With none, every call that
needs a runtime raises NoRuntime, the listings are empty and the host's
calls do nothing.
Backends are the built-in ones (BUILTIN) and any installed package's
through the entry-point group `sbx.runtimes` (name = kind, value =
module:Class), loaded on first use. Each sandbox runs on exactly one runtime,
chosen when it is created: the calls that name a sandbox find its runtime
(of), the calls that make one take kind (None: the default), and the host's
calls run over every enabled runtime (only one on this host does anything)."""
from __future__ import annotations

import contextlib
import dataclasses
import importlib
import importlib.metadata
import threading

from . import env_file
from .base import (FAILED, PAUSED, RUNNING, Backend, Busy, Capabilities, CommandFailed, Failed, Handle, HostSide, Info,
                   LimitRefused, Limits, LimitSpec, NoRoom, NotRunning, Preset, Runtime, RuntimeLayer, SettingField,
                   Snapshot, TemplateGone, TemplateSpec, Timeout, TokenPlace)
from .scoped import INSTALL_KEY, plain_name, scoped

__all__ = ["FAILED", "PAUSED", "RUNNING", "Backend", "Busy", "Capabilities", "CommandFailed", "Failed", "Handle",
           "HostSide", "Info", "Limits", "LimitSpec", "LimitRefused", "NoRoom", "NotRunning", "Preset", "Runtime",
           "RuntimeLayer", "SettingField", "Snapshot", "TemplateGone", "TemplateSpec", "Timeout", "TokenPlace",
           "INSTALL_KEY", "plain_name", "kinds", "enabled", "default", "get", "of"]

GROUP = "sbx.runtimes"
BUILTIN = {"embed": "sbx_control.runtime.embed:Embed", "e2b-cloud": "sbx_control.runtime.e2b_cloud:Cloud",
           "boat": "sbx_control.runtime.boat:Boat"}
ENABLED_KEY, DEFAULT_KEY = "SBX_RUNTIMES", "SBX_RUNTIME_DEFAULT"
CONFIG_KEYS = (ENABLED_KEY, DEFAULT_KEY)
FALLBACK = "embed"   # what an installation without SBX_RUNTIMES runs on
# The kind of everything recorded before an installation ran several
# runtimes (sandboxes, template builds, Amp's caches): Embed was the only
# runtime then, whatever the default is now. The migrations that tag those
# rows use this, and no other core code names a runtime.
LEGACY = "embed"

_backends: dict = {}       # kind -> its backend, made (and configured from runtime.env) once
_index: dict = {}          # sandbox id -> kind, as the listings and creates saw it
_config: dict = {"key": None, "value": None}
_lock = threading.Lock()


# The registry


def _references() -> dict:
    """kind -> module:Class, the built-in ones first; a kind is taken once."""
    found = dict(BUILTIN)
    for entry in importlib.metadata.entry_points(group=GROUP):
        found.setdefault(entry.name, entry.value)
    return found


def kinds() -> list[str]:
    """Every runtime an installed package provides, enabled or not."""
    return list(_references())


def configuration() -> dict:
    """SBX_RUNTIMES and SBX_RUNTIME_DEFAULT as runtime.env has them: SBX_RUNTIMES
    None when the file has no such line (Embed alone, as before), "" when it
    names none; SBX_RUNTIME_DEFAULT "" when not set."""
    file = env_file.path()
    try:
        key = (str(file), file.stat().st_mtime_ns)
    except OSError:
        key = (str(file), None)
    if _config["key"] != key:
        found = {ENABLED_KEY: None, DEFAULT_KEY: ""}
        if key[1] is not None:
            found.update(env_file.read(CONFIG_KEYS))
        _config.update(key=key, value=found)
    return _config["value"]


def listed(value: str | None) -> list[str]:
    """SBX_RUNTIMES's kinds, in order: None (no such line) is Embed alone."""
    if value is None:
        return [FALLBACK]
    return list(dict.fromkeys(k.strip() for k in value.replace(" ", ",").split(",") if k.strip()))


NO_RUNTIME = ("no sandbox runtime is set up: add one in Settings → Sandbox runtimes (or on the server: "
              "sbx-admin runtime add KIND)")


class NoRuntime(Failed):
    """The installation enables no runtime yet (SBX_RUNTIMES is empty)."""

    def __init__(self, message: str = NO_RUNTIME):
        super().__init__(message)


def enabled() -> list[str]:
    """The kinds this installation runs, in order (SBX_RUNTIMES); [] when it
    names none. One no installed package provides stops sbx-control at
    start, naming those that exist."""
    found = listed(configuration()[ENABLED_KEY])
    unknown = [k for k in found if k not in _references()]
    if unknown:
        raise SystemExit(f"{ENABLED_KEY} in /etc/sbx/runtime.env names {', '.join(unknown)}, which no installed "
                         f"package provides; the runtimes there are: {', '.join(kinds())}")
    return found


def default() -> str:
    """The kind new sandboxes run on unless asked otherwise (SBX_RUNTIME_DEFAULT,
    else the first enabled); "" when none is enabled."""
    found, kinds_now = configuration()[DEFAULT_KEY], enabled()
    if found and found not in kinds_now:
        raise SystemExit(f"{DEFAULT_KEY}={found} in /etc/sbx/runtime.env is not one of {ENABLED_KEY} "
                         f"({', '.join(kinds_now) or 'none'})")
    return found or (kinds_now[0] if kinds_now else "")


def get(kind: str | None = None):
    """The enabled runtime of this kind (None: the default), a hosted one as
    this installation sees it (scoped.Scoped); NoRuntime when none is
    enabled. Made once, configured from runtime.env's values then
    (configure: platform.connect hands every one its keys again)."""
    kind = kind or default()
    if not kind:
        raise NoRuntime()
    if kind not in enabled():
        raise Failed(f"the {kind} runtime is not enabled on this installation ({ENABLED_KEY}: "
                     f"{', '.join(enabled())})")
    with _lock:
        if kind not in _backends:
            backend = make(kind)
            _configure_from_file(backend)
            _backends[kind] = backend
        return _view(_backends[kind])


_views: dict = {}   # id(backend) -> (backend, what get() hands out for it)


def _view(backend: Backend):
    """The backend as get() hands it out, the same object every time (scoped.scoped)."""
    found = _views.get(id(backend))
    if found is None or found[0] is not backend:
        found = _views[id(backend)] = (backend, scoped(backend))
    return found[1]


def _configure_from_file(backend: Backend) -> None:
    """Its settings as runtime.env has them, without its secret (which
    platform.connect unseals and hands it): what its properties need
    (nginx's configuration is written from them in processes that never
    connect). A backend that needs its secret to configure stays as made."""
    try:
        backend.configure(env_file.read(tuple(k for k in backend.credential_keys if k != backend.secret_key)))
    except SystemExit:
        pass


def make(kind: str) -> Backend:
    """A new, unconfigured backend of kind, enabled or not (sbx-admin runtime
    add checks one before enabling it); KeyError when no package provides it."""
    module, _, name = _references()[kind].partition(":")
    return getattr(importlib.import_module(module), name)()


def backends() -> list:
    """The enabled runtimes, in order."""
    return [get(kind) for kind in enabled()]


class Unavailable(Failed):
    """The sandbox runs on a runtime that is not enabled (any more)."""


def of(sandbox_id: str):
    """The runtime a sandbox runs on: from what the listings and creates
    saw, else by asking each enabled runtime (a hosted one answers only for
    this installation's). NotRunning (a Failed) when none has it;
    Unavailable when it was seen on a runtime that is not enabled now:
    another runtime never answers for it."""
    kinds_now = enabled()
    recorded = _index.get(sandbox_id)
    if recorded:
        if recorded not in kinds_now:
            raise Unavailable(f"sandbox {sandbox_id} runs on the {recorded} runtime, which is not enabled here "
                              f"({ENABLED_KEY}: {', '.join(kinds_now)})")
        return get(recorded)
    if len(kinds_now) == 1 and (only := get(kinds_now[0])).host:
        return only   # alone on this host, every sandbox there is this installation's
    failed = None
    for kind in kinds_now:
        try:
            get(kind).info(sandbox_id)
        except NotRunning:
            continue
        except Exception as error:  # that runtime did not answer: it may be the one
            failed = error
            continue
        _index[sandbox_id] = kind
        return get(kind)
    if failed is not None:
        raise failed
    raise NotRunning(f"no runtime here ({', '.join(kinds_now)}) has sandbox {sandbox_id}")


def kind_of(info) -> str:
    """The kind of a listed sandbox's runtime: its tag, else of()."""
    return info.runtime or of(info.sandbox_id).kind


def configure(secrets: dict) -> None:
    """Hand each enabled runtime its own keys of the platform secrets
    (runtime.env): only the names it reads (credential_keys). An unknown
    kind or a default that is not enabled stops it first (SystemExit)."""
    default()
    for backend in backends():
        backend.configure({k: secrets[k] for k in backend.credential_keys if k in secrets})


def credential_keys() -> tuple:
    """Every name the enabled runtimes read from runtime.env."""
    return tuple(dict.fromkeys(k for b in backends() for k in b.credential_keys))


def _tag(info: Info, kind: str) -> Info:
    _index[info.sandbox_id] = kind
    return dataclasses.replace(info, runtime=kind)


# This installation's own (scoped.py)


def installation_id() -> str:
    from .. import state
    return state.installation_id()


def shared(kind: str | None = None) -> bool:
    """Whether the runtime of kind may be shared by several installations
    (it does not run on this host)."""
    return not get(kind).host


def template_name(name: str, kind: str | None = None) -> str:
    """The name a template or snapshot of sbx's has on the runtime of kind
    (Backend.template_name: prefixed on a hosted one)."""
    return get(kind).template_name(name)


# A sandbox's calls: its own runtime's


def create(template: str, *, timeout: int, envs: dict | None = None, metadata: dict | None = None,
           deny_out: list | None = None, on_timeout: str | None = None, auto_resume: bool | None = None,
           kind: str | None = None) -> Handle:
    """A new sandbox on the runtime of kind (None: the default); timeout:
    seconds until it pauses, within that runtime's limits().timeout_seconds.
    Its metadata carries this installation's id (INSTALL_KEY)."""
    backend = get(kind)
    metadata = {**(metadata or {}), INSTALL_KEY: installation_id()}   # every sandbox sbx makes, on any runtime
    with _limits_learned(backend):
        handle = backend.create(template, timeout=_capped(timeout, backend), envs=envs, metadata=metadata,
                                deny_out=deny_out, on_timeout=on_timeout, auto_resume=auto_resume)
    _index[handle.sandbox_id] = backend.kind
    return handle


def deny_out(*cidrs: str, kind: str | None = None) -> list | None:
    """A sandbox's deny_out for create: these ranges where the runtime
    applies them (egress_rules), None where it can't; the settings refuse
    what would need them there (reach)."""
    return list(cidrs) if capabilities(kind).egress_rules else None


def connect(sandbox_id: str, *, timeout: int | None = None) -> Handle:
    backend = of(sandbox_id)
    with _limits_learned(backend):
        return backend.connect(sandbox_id, timeout=_capped(timeout, backend))


class Listing(list):
    """What the runtimes listed. failed: kind -> the error of each runtime
    that did not answer, whose sandboxes are missing from it."""

    def __init__(self, items=(), failed: dict | None = None):
        super().__init__(items)
        self.failed = dict(failed or {})

    @property
    def complete(self) -> bool:
        return not self.failed


class Incomplete(Failed):
    """A listing that misses a runtime's sandboxes, where acting on what is
    missing would be wrong (deleting, pruning, forgetting)."""


def sandboxes(metadata: dict | None = None, states=None, kind: str | None = None) -> Listing:
    """This installation's sandboxes (scoped.py) on every enabled runtime (or
    on kind's) whose metadata has these
    values, each tagged with its runtime (Info.runtime); states: RUNNING and
    PAUSED, None for what each runtime lists by default. A runtime that does
    not answer is left out and named in the listing's failed, so the others'
    sandboxes keep working; when none answers (with one runtime: when it
    doesn't), its error is raised. Whoever deletes or forgets what is not
    listed asks complete() instead."""
    found = Listing()
    chosen = [get(kind)] if kind else backends()
    kinds_now = [b.kind for b in chosen]
    if not kinds_now:
        return found
    for backend in chosen:
        try:
            listed = backend.sandboxes(metadata, states)
        except Exception as error:
            print(f"runtime {backend.kind} did not list its sandboxes: {error!r}", flush=True)
            found.failed[backend.kind] = error
            continue
        if metadata is None and states is None:   # a full listing: what it keeps of sandboxes gone goes
            backend.listed({i.sandbox_id for i in listed})
        found += [_tag(info, backend.kind) for info in listed]
    if len(found.failed) == len(kinds_now):
        raise next(iter(found.failed.values()))
    return found


def complete(listing) -> list:
    """The listing, or Incomplete naming the runtimes it misses."""
    failed = getattr(listing, "failed", None)
    if failed:
        raise Incomplete("not every runtime listed its sandboxes ("
                         + "; ".join(f"{kind}: {type(error).__name__}: {error}" for kind, error in failed.items())
                         + "); nothing is deleted or pruned until they all answer")
    return listing


def info(sandbox_id: str) -> Info:
    """NotRunning for a sandbox that is another installation's."""
    backend = of(sandbox_id)
    return _tag(backend.info(sandbox_id), backend.kind)


def kill(sandbox_id: str) -> bool:
    """False when no runtime has it (any more)."""
    try:
        backend = of(sandbox_id)
    except NotRunning:
        return False
    return backend.kill(sandbox_id)


def pause(sandbox_id: str) -> None:
    of(sandbox_id).pause(sandbox_id)


def set_timeout(sandbox_id: str, seconds: int) -> None:
    backend = of(sandbox_id)
    with _limits_learned(backend):
        backend.set_timeout(sandbox_id, _capped(seconds, backend))


def snapshot(sandbox_id: str, name: str) -> Snapshot:
    """A snapshot of the sandbox on its own runtime, which a new sandbox on
    that runtime can start from (Snapshot.runtime)."""
    backend = of(sandbox_id)
    return dataclasses.replace(backend.snapshot(sandbox_id, name), runtime=backend.kind)


def delete_snapshot(snapshot_id: str, kind: str | None = None) -> None:
    get(kind).delete_snapshot(snapshot_id)


def upstream_host(port: int, sandbox_id: str) -> str:
    return of(sandbox_id).upstream_host(port, sandbox_id)


def upstream_token(sandbox_id: str, port: int, kind: str | None = None, *,
                   fetch: bool = True) -> tuple[TokenPlace, str] | None:
    """(where, token) nginx adds to a request for the sandbox's port, for a
    runtime that answers its sandboxes' ports only with a token
    (Backend.upstream_token_at); None for any other runtime. kind: the
    sandbox's runtime, when the caller knows it. Without fetch the token is
    only one the backend kept ("" when none)."""
    backend = get(kind) if kind else of(sandbox_id)
    where = backend.upstream_token_at
    return (where, backend.upstream_token(sandbox_id, port, fetch=fetch)) if where else None


def upstream_token_places() -> dict:
    """kind -> where each enabled runtime that wants a token wants it (nginx's maps)."""
    return {b.kind: b.upstream_token_at for b in backends() if b.upstream_token_at}


# Shims for gate.py and ip_previews.py (phase 5's files) until they read upstream_token_places().
def upstream_token_headers() -> dict:
    return {k: p.name for k, p in upstream_token_places().items() if p.place == "header"}


def upstream_token_cookies() -> dict:
    return {k: p.name for k, p in upstream_token_places().items() if p.place == "cookie"}


def desktop(kind: str | None = None) -> str:
    """Which desktop the runtime's sandboxes have: "sbx" (sbx's noVNC) or
    "provider" (its own, opened through desktop_url)."""
    return get(kind).desktop


def desktop_url(sandbox_id: str) -> str:
    """A fresh URL of the sandbox's desktop on a runtime with its own."""
    return of(sandbox_id).desktop_url(sandbox_id)


def cold_resume(kind: str | None = None) -> bool:
    """Whether every resume on the runtime boots the sandbox afresh (Runtime.cold_resume)."""
    return get(kind).cold_resume


def snapshot_usage(sandbox_id: str) -> dict:
    if not sandbox_id:
        return {}
    try:
        return of(sandbox_id).snapshot_usage(sandbox_id)
    except NotRunning:
        return {}


def own_route(host: str, sandbox_id: str, port: str):
    """(kind, answer) of the enabled runtime whose own host this is
    (Runtime.own_route: True or (port, sandbox_id)); None for any other host."""
    for backend in backends():
        found = backend.own_route(host, sandbox_id, port)
        if found is not None:
            return backend.kind, found
    return None


def preview(name: str, port: int | None, listen: int, *, preserve_host: bool = False) -> None:
    """sbx-admin preview, on the runtime of the sandbox named name; ValueError
    when that runtime has no local proxy."""
    found = sandboxes({"sbx": name})
    (get(found[0].runtime) if found else get()).preview(name, port, listen, preserve_host=preserve_host)


# Limits, per runtime


def _capped(seconds: int | None, backend: Runtime) -> int | None:
    """A timeout within the runtime's limit: the keeper extends it while
    the sandbox is used, so a shorter one only pauses an idle sandbox sooner.
    The limit is never below base.MIN_TIMEOUT_SECONDS, several renewals."""
    most = backend.limits().timeout_seconds
    return seconds if seconds is None or most is None else min(seconds, most)


def limits(kind: str | None = None) -> Limits:
    return get(kind).limits()


# Limits the provider refused: the backend raises LimitRefused, the registry
# keeps the last one per runtime (shown on its row in Settings → Sandbox
# runtimes until its settings change) and says which setting to change. It
# never raises a limit by itself.

def _limit_hit_setting(kind: str):
    from .. import state
    return state.Setting(f"runtime_limit_hit:{kind}", encoded=True)


def setting_for(backend, field: str):
    """The backend's SettingField that sets the Limits field (None when none does)."""
    return next((s for s in backend.settings if s.limit == field), None)


@contextlib.contextmanager
def _limits_learned(backend):
    try:
        yield
    except LimitRefused as error:
        found = setting_for(backend, error.field)
        change = (f"; change {found.label} ({found.key}) in Settings → Sandbox runtimes → {backend.title} to what "
                  "your plan allows" if found else "")
        message = f"{error}{change}"
        try:
            import datetime
            _limit_hit_setting(backend.kind).set({
                "field": error.field, "asked": error.asked, "allowed": error.allowed, "message": message,
                "setting": found.key if found else "",
                "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
        except Exception as recording:   # the refusal is said all the same
            print(f"runtime {backend.kind}: could not record the limit it refused: {recording!r}", flush=True)
        raise LimitRefused(message, field=error.field, asked=error.asked, allowed=error.allowed) from error


def limit_hit(kind: str) -> dict | None:
    """The last limit the runtime's provider refused: {field, asked, allowed,
    message, setting, at}; None when none since its settings last changed."""
    try:
        return _limit_hit_setting(kind).get()
    except Exception:
        return None


def forget_limit_hit(kind: str) -> None:
    """Its settings changed: what it refused before no longer holds."""
    try:
        _limit_hit_setting(kind).delete()
    except Exception:
        pass


SIZE_UNITS = {"cpu": "vCPUs", "memory_mb": "MiB", "disk_mb": "MiB"}


def _raise_it(found: Limits, field: str) -> str:
    """How to allow more, as the end of a message."""
    key = found.keys.get(field)
    if not key:
        return ""
    return f", or if {found.source or 'your plan'} allows more, set {key} in /etc/sbx/runtime.env"


def beyond_limits(size: dict, kind: str | None = None) -> str:
    """Why a sandbox of size ({cpu, memory_mb, disk_mb}, recipes.App.size)
    can't run on the runtime of kind, naming the limit and how to raise it;
    "" when it can."""
    found = limits(kind)
    for field, unit in SIZE_UNITS.items():
        most = getattr(found, field)
        if most is not None and size.get(field, 0) > most:
            return (f"resources.{field} is {size[field]} {unit}, and the sandbox runtime ({title(kind)}) allows at "
                    f"most {most} {unit} per sandbox; lower it in .sbx/sbx.yaml{_raise_it(found, field)}")
    return ""


def too_many(kind: str | None = None) -> str:
    """Why another sandbox can't start on the runtime of kind now, when as
    many as it runs at once are running; "" when one can."""
    found = limits(kind)
    if found.sandboxes is None:
        return ""
    running = len(sandboxes(None, [RUNNING], kind=kind or default()))
    if running < found.sandboxes:
        return ""
    return (f"{running} sandboxes are running, and the sandbox runtime ({title(kind)}) runs at most "
            f"{found.sandboxes} at once; pause or delete one (sbx ls, sbx down){_raise_it(found, 'sandboxes')}")


# Templates, per runtime


def build_template(spec: TemplateSpec, *, alias: str, cpu: int, memory_mb: int, disk_mb: int, on_log=None,
                   kind: str | None = None):
    backend = get(kind)
    with _limits_learned(backend):
        return backend.build_template(spec, alias=alias, cpu=cpu, memory_mb=memory_mb, disk_mb=disk_mb,
                                      on_log=on_log)


def templates(kind: str | None = None) -> list[tuple[str, str]]:
    """The runtime's templates that are this installation's: on a hosted
    runtime only those named with its prefix (template_name)."""
    return get(kind).templates()


def delete_template(name: str, kind: str | None = None) -> bool:
    return get(kind).delete_template(name)


def failure(error: BaseException) -> bool:
    """Whether the error is a runtime's or the connection's to it: it
    answered with an error or not at all, as while an upgrade recreates it."""
    return isinstance(error, Failed) or any(backend.failure(error) for backend in backends())


# The host's calls: over every enabled runtime (only one on this host has anything)


def capacity(memory_mb: int, kind: str | None = None) -> dict:
    return get(kind).capacity(memory_mb)


def placing(kind: str | None = None):
    return get(kind).placing()


def apply_network() -> dict:
    """sbx-admin network apply on every enabled runtime: done, it is no
    longer pending (settings.HostUnit.RUNTIME_NETWORK). With several, each
    one's result under runtimes."""
    from .. import paths, settings
    results = {backend.kind: backend.apply_network() for backend in backends()}
    (paths.run() / "apply" / settings.HostUnit.RUNTIME_NETWORK).unlink(missing_ok=True)
    if len(results) == 1:
        return next(iter(results.values()))
    applied = any(r.get("status") == "applied" for r in results.values())
    cidrs = list(dict.fromkeys(c for r in results.values() for c in r.get("allowed_cidrs", [])))
    return {"status": "applied" if applied else "unchanged", "allowed_cidrs": cidrs, "runtimes": results}


def _first(kind: str | None, ask):
    """kind's answer, else the first enabled runtime's that is not None."""
    if kind:
        return ask(get(kind))
    return next((found for found in (ask(b) for b in backends()) if found is not None), None)


def disk(kind: str | None = None) -> dict | None:
    return _first(kind, lambda b: b.disk())


def free_disk_mb(kind: str | None = None) -> int | None:
    return _first(kind, lambda b: b.free_disk_mb())


def disk_path(kind: str | None = None) -> str:
    return get(kind).disk_path


def why_busy(memory_mb: int | None, kind: str | None = None) -> str:
    return get(kind).why_busy(memory_mb)


def collect_storage(kind: str | None = None) -> None:
    for backend in [get(kind)] if kind else backends():
        backend.collect_storage()


def jobs() -> list:
    return [job for backend in backends() for job in backend.jobs()]


def host_units() -> list:
    return [unit for backend in backends() for unit in backend.host_units()]


def host_unit_names() -> list[str]:
    """The systemd units that run the enabled runtimes on this host."""
    return [b.host_unit for b in backends() if b.host_unit]


def host_commands(into) -> dict:
    return {name: script for backend in backends() for name, script in backend.host_commands(into).items()}


def guard_host() -> None:
    for backend in backends():
        backend.guard_host()


def apply_host(key: str) -> bool:
    """Whether key is an enabled runtime's effect (each one that has it applies it)."""
    return any([backend.apply_host(key) for backend in backends()])


def _checks(backend, checks) -> list:
    return [dataclasses.replace(c, runtime=backend.kind) if dataclasses.is_dataclass(c) else c for c in checks]


def readiness(facts: dict) -> list:
    """What the first-run setup checks for each enabled runtime, each
    check tagged with its kind (Check.runtime)."""
    return [c for backend in backends() for c in _checks(backend, backend.readiness(facts))]


def test(kind: str | None = None) -> list:
    """Each enabled runtime's own checks (or kind's), tagged with its kind."""
    return [c for backend in ([get(kind)] if kind else backends()) for c in _checks(backend, backend.test())]


def status(kind: str | None = None):
    """kind's connections.Status; without kind, the installation's: the one
    runtime's, or with several, the worst state and each one's summary."""
    from ..connections.base import Status
    if not kind and not enabled():
        return Status("runtime", "Sandbox runtime", "not_connected", NO_RUNTIME, {}, (), ("test",))
    if kind or len(enabled()) == 1:
        return get(kind).status()
    found = {backend.kind: backend.status() for backend in backends()}
    order = ("not_connected", "attention", "off", "connected")
    worst = min((s.state for s in found.values()), key=order.index)
    summary = " · ".join(f"{get(k).title}: {s.summary}" for k, s in found.items())
    return Status("runtime", "Sandbox runtimes", worst, summary, {k: s.facts for k, s in found.items()},
                  tuple(f for s in found.values() for f in s.fields), ("test",))


def capabilities(kind: str | None = None) -> Capabilities:
    return get(kind).capabilities


def title(kind: str | None = None) -> str:
    return get(kind).title


def ports() -> frozenset:
    """Every enabled runtime's own ports in its sandboxes: never a preview."""
    return frozenset(p for backend in backends() for p in backend.ports)


def internal_suffix(kind: str | None = None) -> str:
    return get(kind).internal_suffix


def ingresses() -> dict:
    """kind -> (ingress, ingress_cleared) of every enabled runtime, the default first: nginx's upstreams."""
    first = default()
    return {b.kind: (b.ingress, tuple(b.ingress_cleared))
            for b in sorted(backends(), key=lambda b: b.kind != first)}
