"""The sandbox runtime on the settings page: what the installed runtime
reports about itself (runtime.current().status and test)."""
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
        from .. import runtime
        return runtime.current().test()
