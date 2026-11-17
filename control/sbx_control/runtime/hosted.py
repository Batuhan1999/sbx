"""What the hosted backends (a provider's sandboxes: E2B Cloud, Boat) share:
their limits read from runtime.env, the settings form's limit sliders, what
they keep per sandbox (a token) and their status on the settings page."""
from __future__ import annotations

import threading
import time
from typing import Callable, Generic, Hashable, TypeVar

from .base import Failed, Limits, limit_settings

__all__ = ["limits_from", "limit_settings", "PerSandbox", "hosted_status"]

T = TypeVar("T")


def limits_from(secrets: dict, specs, source: str) -> Limits:
    """The limits runtime.env's values set (Limits.read)."""
    return Limits.read(specs, secrets, source)


def _sandbox_of(key: Hashable):
    return key[0] if isinstance(key, tuple) else key


class PerSandbox(Generic[T]):
    """What a backend keeps per sandbox (a key: its id, or (id, port)), fetched
    once at a time per key: callers waiting on a fetch take its value, and a
    fetch that failed answers its error again for retry_after seconds (an
    outage is not a call to the provider for every request)."""

    def __init__(self):
        self._values: dict = {}
        self._failed: dict = {}    # key -> (when, the error)
        self._locks: dict = {}
        self._lock = threading.Lock()

    def get(self, key, default=None):
        return self._values.get(key, default)

    def put(self, key, value: T) -> None:
        self._values[key] = value
        self._failed.pop(key, None)

    def __contains__(self, key) -> bool:
        return key in self._values

    def keys(self) -> list:
        return list(self._values)

    def _raise_recent(self, key, retry_after: float) -> None:
        failed = self._failed.get(key)
        if failed and time.monotonic() - failed[0] < retry_after:
            raise failed[1]

    def get_or_fetch(self, key, fetch: Callable[[], T], retry_after: float) -> T:
        """The kept value, else fetch()'s, kept; its error is kept retry_after seconds."""
        if key in self._values:
            return self._values[key]
        self._raise_recent(key, retry_after)
        with self._lock:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            if key in self._values:
                return self._values[key]
            self._raise_recent(key, retry_after)   # a caller waiting here met the same outage
            try:
                value = fetch()
            except Exception as error:
                self._failed[key] = (time.monotonic(), error)
                raise
            self.put(key, value)
            return value

    def drop(self, key) -> None:
        """Forget key; a sandbox id also forgets its (id, port) keys."""
        for kept in (self._values, self._failed):
            for found in [k for k in kept if k == key or _sandbox_of(k) == key]:
                kept.pop(found, None)
        with self._lock:
            for found in [k for k in self._locks if (k == key or _sandbox_of(k) == key)
                          and not self._locks[k].locked()]:
                del self._locks[found]

    def keep(self, sandbox_ids) -> None:
        """After a full listing: forget every sandbox not in it."""
        ids = set(sandbox_ids)
        gone = {_sandbox_of(k) for k in [*self._values, *self._failed, *self._locks]} - ids
        for sandbox_id in gone:
            self.drop(sandbox_id)


def hosted_status(backend, *, configured: bool, facts: dict | None = None, account: Callable[[], dict] = dict,
                  notes: Callable[[dict], list] = lambda facts: [], fields: tuple = (), limit_note: str = ""):
    """A hosted backend's connections.Status: not connected without its key;
    else this installation's running sandboxes (not the whole account's)
    against its limit, the provider's account facts (account()) and notes
    (notes(facts)); attention when it did not answer, is full or notes say so.
    limit_note: said after the count when the backend has a limit ({most})."""
    from .. import runtime
    from ..connections.base import Status
    title = backend.title
    if not configured:
        return Status("runtime", "Sandbox runtime", "not_connected", f"{title}: no {backend.secret_key} in runtime.env",
                      {}, (), ("test",))
    found = dict(facts or {})
    try:
        running = len(runtime.sandboxes(None, [runtime.RUNNING], kind=backend.kind))
        found.update(account())
    except Exception as error:
        return Status("runtime", "Sandbox runtime", "attention", f"{title} did not answer: {error}", found, (),
                      ("test",))
    found["running"] = running
    said = notes(found)
    most = backend.limits().sandboxes
    full = most is not None and running >= most
    head = f"{title} · {running} running here" + (limit_note.format(most=most) if most and limit_note else "")
    summary = " · ".join([head, *said, *(["full"] if full else [])])
    return Status("runtime", "Sandbox runtime", "attention" if said or full else "connected", summary, found, fields,
                  ("test",))


def wrapped(error: BaseException, message: str) -> Failed:
    """error as Failed, said with message when it is not one already."""
    return error if isinstance(error, Failed) else Failed(f"{message}: {type(error).__name__}: {error}")
