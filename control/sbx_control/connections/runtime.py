"""The sandbox runtimes on the settings page: what each enabled runtime
reports about itself (runtime.status and test), and whether its sandboxes
reach this server at its public address (reachability)."""
from __future__ import annotations

from .base import Check, Connection, Status


class Runtime(Connection):
    kind = "runtime"
    title = "Sandbox runtime"
    group = "internal"

    def status(self) -> Status:
        from .. import runtime
        return runtime.status()

    def test(self, values: dict | None = None) -> list[Check]:
        """Each runtime's own checks, then, on each one whose checks pass,
        whether its sandboxes reach this server."""
        from .. import reachability, runtime
        if not runtime.enabled():
            return [Check("Sandbox runtime", False, runtime.NO_RUNTIME,
                          "add one in the web app (Settings → Sandbox runtimes) or: sbx-admin runtime add KIND")]
        found = []
        for kind in runtime.enabled():
            checks = runtime.test(kind)
            found += checks
            if all(c.ok for c in checks):  # else no sandbox to check from
                found.append(reachability.check(kind))
        return found
