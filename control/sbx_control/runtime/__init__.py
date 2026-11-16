"""The sandbox runtime this installation runs on, and the one way to it.

sbx's core and its extensions reach VMs only through these functions;
only a runtime's own package names its SDK (a test enforces it). Today
the runtime is E2B Embed on this host (runtime.embed); the core names
no backend (a test enforces that too)."""
from .base import (PAUSED, RUNNING, Busy, Capabilities, CommandFailed, Failed, Handle, Info, NoRoom, Runtime,
                   RuntimeLayer, Snapshot, TemplateSpec, Timeout)

__all__ = ["PAUSED", "RUNNING", "Busy", "Capabilities", "CommandFailed", "Failed", "Handle", "Info", "NoRoom",
           "Runtime", "RuntimeLayer", "Snapshot", "TemplateSpec", "Timeout", "current"]

_current: Runtime | None = None


def current() -> Runtime:
    global _current
    if _current is None:
        from .embed import Embed
        _current = Embed()
    return _current


def create(template: str, *, timeout: int, envs: dict | None = None, metadata: dict | None = None,
           deny_out: list | None = None, on_timeout: str | None = None, auto_resume: bool | None = None) -> Handle:
    return current().create(template, timeout=timeout, envs=envs, metadata=metadata, deny_out=deny_out,
                            on_timeout=on_timeout, auto_resume=auto_resume)


def deny_out(*cidrs: str) -> list | None:
    """A sandbox's deny_out for create: these ranges where the runtime
    applies them (egress_rules), None where it can't; the settings refuse
    what would need them there (reach)."""
    return list(cidrs) if capabilities().egress_rules else None


def connect(sandbox_id: str, *, timeout: int | None = None) -> Handle:
    return current().connect(sandbox_id, timeout=timeout)


def sandboxes(metadata: dict | None = None, states=None) -> list[Info]:
    """Every sandbox whose metadata has these values; states: RUNNING and
    PAUSED, None for what the runtime lists by default."""
    return current().sandboxes(metadata, states)


def info(sandbox_id: str) -> Info:
    return current().info(sandbox_id)


def kill(sandbox_id: str) -> bool:
    return current().kill(sandbox_id)


def pause(sandbox_id: str) -> None:
    current().pause(sandbox_id)


def set_timeout(sandbox_id: str, seconds: int) -> None:
    current().set_timeout(sandbox_id, seconds)


def snapshot(sandbox_id: str, name: str) -> Snapshot:
    return current().snapshot(sandbox_id, name)


def delete_snapshot(snapshot_id: str) -> None:
    current().delete_snapshot(snapshot_id)


def build_template(spec: TemplateSpec, *, alias: str, cpu: int, memory_mb: int, disk_mb: int, on_log=None):
    return current().build_template(spec, alias=alias, cpu=cpu, memory_mb=memory_mb, disk_mb=disk_mb, on_log=on_log)


def templates() -> list[tuple[str, str]]:
    return current().templates()


def delete_template(name: str) -> bool:
    return current().delete_template(name)


def failure(error: BaseException) -> bool:
    """Whether the error is the runtime's or the connection's to it: it
    answered with an error or not at all, as while an upgrade recreates it."""
    return isinstance(error, Failed) or current().failure(error)


def capacity(memory_mb: int) -> dict:
    return current().capacity(memory_mb)


def placing():
    return current().placing()


def apply_network() -> dict:
    """sbx-admin network apply: done, it is no longer pending
    (settings.HostUnit.RUNTIME_NETWORK)."""
    from .. import paths, settings
    result = current().apply_network()
    (paths.run() / "apply" / settings.HostUnit.RUNTIME_NETWORK).unlink(missing_ok=True)
    return result


def disk() -> dict | None:
    return current().disk()


def free_disk_mb() -> int | None:
    return current().free_disk_mb()


def why_busy(memory_mb: int | None) -> str:
    return current().why_busy(memory_mb)


def snapshot_usage(sandbox_id: str) -> dict:
    return current().snapshot_usage(sandbox_id)


def collect_storage() -> None:
    current().collect_storage()


def jobs() -> list:
    return current().jobs()


def upstream_host(port: int, sandbox_id: str) -> str:
    return current().upstream_host(port, sandbox_id)


def own_route(host: str, sandbox_id: str, port: str):
    return current().own_route(host, sandbox_id, port)


def preview(name: str, port: int | None, listen: int, *, preserve_host: bool = False) -> None:
    """sbx-admin preview; ValueError when the runtime has no local proxy."""
    serve = getattr(current(), "preview", None)
    if serve is None:
        raise ValueError(f"{title()} has no loopback preview on this host; open the sandbox's preview URL instead")
    serve(name, port, listen, preserve_host=preserve_host)


def capabilities() -> Capabilities:
    return current().capabilities


def title() -> str:
    return current().title
