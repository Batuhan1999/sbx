"""The sandbox runtime on the settings page: what the installed runtime
reports about itself (runtime.current().status and test), and whether its
sandboxes reach this server at its public address (reachability)."""
from __future__ import annotations

from .base import Check, Connection, Status


class Runtime(Connection):
    kind = "runtime"
    title = "Sandbox runtime"
    group = "internal"

    def status(self) -> Status:
        from .. import runtime
        return runtime.current().status()

    def test(self, values: dict | None = None) -> list[Check]:
        from .. import reachability, runtime
        checks = runtime.current().test()
        if not all(c.ok for c in checks):  # no sandbox to check from
            return checks
        return [*checks, reachability.check()]
