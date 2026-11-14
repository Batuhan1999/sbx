"""Connection registry for status, live checks, validated setup, and disconnect:
the platform's own connections, with the extensions (extensions.py: GitHub,
the agent connectors) after sign-in. Secret fields expose only whether a
value is set."""
from __future__ import annotations

from .base import Check, Connection, Field, Refused, State, Status

__all__ = ["Check", "Connection", "Field", "Refused", "State", "Status", "get", "status_all", "listed", "checks_ok"]


def _registry() -> dict:
    from .. import extensions
    from . import domain, runtime, sign_in, store
    return {c.kind: c for c in (domain.Domain(), sign_in.SignIn(), *extensions.connectors(),
                                store.Store(), runtime.Runtime())}


REGISTRY: dict = {}


def get(kind: str) -> Connection:
    if not REGISTRY:
        REGISTRY.update(_registry())
    if kind not in REGISTRY:
        raise KeyError(kind)
    return REGISTRY[kind]


def status_all() -> list[Status]:
    if not REGISTRY:
        REGISTRY.update(_registry())
    return [c.status() for c in REGISTRY.values()]


def listed() -> list[dict]:
    """What people can act on, for the web app: every connection but the internal ones."""
    if not REGISTRY:
        REGISTRY.update(_registry())
    return [c.view(c.status()) for c in REGISTRY.values() if c.group != "internal"]


def checks_ok(checks: list) -> bool:
    return all(c.ok for c in checks)
