"""What every connection is: its status for the settings UI (fields, facts,
the actions the UI may offer), live checks, and refusals."""
from __future__ import annotations

import dataclasses
from typing import Literal

State = Literal["connected", "attention", "not_connected", "off"]
Group = Literal["platform", "agent", "internal"]


@dataclasses.dataclass(frozen=True)
class Field:
    name: str
    label: str
    secret: bool = False
    set: bool = False                 # secret fields: a value exists
    value: str | None = None          # non-secret fields only
    help: str = ""


@dataclasses.dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    runtime: str | None = None        # the kind of the sandbox runtime it checks, if it checks one


@dataclasses.dataclass(frozen=True)
class Status:
    kind: str
    title: str
    state: State
    summary: str
    facts: dict
    fields: tuple[Field, ...]
    actions: tuple[str, ...] = ()     # what the UI may offer: connect, test, disconnect

    def as_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "state": self.state, "summary": self.summary,
                "facts": self.facts, "actions": list(self.actions),
                "fields": [dataclasses.asdict(f) for f in self.fields]}


class Refused(Exception):
    """A connect that can't be done: bad input, or a failed test."""

    def __init__(self, message: str, checks: list | None = None):
        super().__init__(message)
        self.checks = checks or []


class Connection:
    kind: str
    title: str
    # platform: the installation's own (domain, sign-in, GitHub); agent: a
    # coding agent's connector; internal: the server's (nobody acts on it,
    # so the web app does not list it).
    group: Group = "platform"
    shown: tuple[str, ...] = ()       # the facts worth showing people, in order

    def view(self, status: Status) -> dict:
        """The status for the web app: its group, and the shown facts that
        say something the summary does not, as label/value."""
        display = [{"label": name.replace("_", " ").capitalize(), "value": str(status.facts[name])}
                   for name in self.shown
                   if status.facts.get(name) not in ("", None) and str(status.facts[name]) not in status.summary]
        return {**status.as_dict(), "group": self.group, "display": display}

    def status(self) -> Status:
        raise NotImplementedError

    def test(self, values: dict | None = None) -> list[Check]:
        raise NotImplementedError

    def connect(self, values: dict, *, by: str) -> Status:
        raise NotImplementedError

    def disconnect(self, *, by: str) -> Status:
        raise Refused(f"{self.title} can't be disconnected")
