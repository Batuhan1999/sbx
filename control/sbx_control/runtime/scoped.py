"""This installation's own, on a provider account several may share.

Every sandbox sbx creates carries the installation's id in its metadata
(INSTALL_KEY). A runtime on this host (host: Embed) serves one installation,
so all of its sandboxes are this one's and it is used as it is. A hosted
runtime is used through Scoped, which the registry puts in front of it: its
sandboxes are listed with the id as a metadata query (the provider filters),
one looked up that is another installation's is NotRunning, and its
templates and snapshots are named with the id's prefix (template_name) and
listed by it, so installations sharing a provider account never collide or
touch each other's."""
from __future__ import annotations

from .base import Backend, NotRunning

INSTALL_KEY = "sbx_install"


def _installation_id() -> str:
    from . import installation_id   # the registry's, which tests stand in for
    return installation_id()


def plain_name(name: str) -> str:
    """A template name without an installation's prefix."""
    head, sep, rest = name.partition("--")
    return rest if sep and len(head) == 8 and all(c in "0123456789abcdef" for c in head) else name


class Scoped:
    """A hosted backend as this installation sees it; everything not here is the backend's own."""

    def __init__(self, backend: Backend):
        self.backend = backend

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def template_name(self, name: str) -> str:
        """After this installation's prefix (<8 hex>--, which no template slug has)."""
        return f"{_installation_id()[:8]}--{name}"

    def mine(self, info) -> bool:
        return (info.metadata or {}).get(INSTALL_KEY) == _installation_id()

    def sandboxes(self, metadata=None, states=None):
        return self.backend.sandboxes({**(metadata or {}), INSTALL_KEY: _installation_id()}, states)

    def info(self, sandbox_id):
        found = self.backend.info(sandbox_id)
        if not self.mine(found):
            raise NotRunning(f"sandbox {sandbox_id} is another installation's")
        return found

    def snapshot(self, sandbox_id, name):
        return self.backend.snapshot(sandbox_id, self.template_name(name))

    def templates(self):
        prefix = self.template_name("")
        return [(name, status) for name, status in self.backend.templates() if name.startswith(prefix)]


def scoped(backend: Backend):
    """The backend as the registry hands it out: a hosted one Scoped."""
    return backend if backend.host else Scoped(backend)
