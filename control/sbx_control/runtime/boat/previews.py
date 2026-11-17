"""Previews on Boat: each port answers only with its own token. A port is
exposed the first time the gate asks for it (private, never public; relayed
from loopback when the app listens only there) and its token kept; nginx
appends it to the request's Cookie (upstream_token_cookie). Boat's own
desktop (desktop = "provider") replaces sbx's noVNC.

What the backend keeps of a sandbox lives in one Kept per sandbox: a stop
resets what its machine had (reset_machine), and a sandbox that is gone is
dropped whole."""
from __future__ import annotations

import dataclasses
import threading
import time
import urllib.parse

from ..base import CommandFailed, Failed, NotRunning, Timeout
from ..listening import LOOPBACK, listening
from .errors import POLL_SECONDS
from .sandbox import Handle

HOSTED_DOMAIN = "on.boat.dev"   # where Boat serves a sandbox's exposed ports: <subdomain>-<port>.on.boat.dev
INGRESS_NAME = "sbx-ingress"    # any name under that wildcard; nginx asks by each sandbox's own name (SNI)
COOKIE = "_port_auth"           # the cookie a private port's token goes in (docs.boat.dev/hosting)
RELOOK_SECONDS = 3     # how often a port nothing answered on yet is looked at again
# Boat's own ports in every sandbox (docs.boat.dev/hosting): never a preview.
RESERVED_PORTS = frozenset({22, 6080, 8090, 8911, *range(47984, 48011)})


@dataclasses.dataclass(frozen=True)
class Exposed:
    """A port as Boat exposes it: its host and token, whether Boat relays it
    from loopback, whether something answered on it (settled) and when it
    was last looked at."""
    host: str
    token: str
    relay: bool
    settled: bool
    checked: float

    def fresh(self) -> bool:
        """Whether it stands without looking again: something answered, or it was looked at moments ago."""
        return self.settled or time.monotonic() - self.checked < RELOOK_SECONDS


@dataclasses.dataclass
class Port:
    """A port of a sandbox: its exposure on the current machine, the relay as
    last seen (kept across stops: with nothing listening yet after a resume,
    exposing it without the relay would turn Boat's relay off) and the lock
    of one exposure at a time."""
    exposed: Exposed | None = None
    relay: bool = False
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)


@dataclasses.dataclass
class Kept:
    """What the backend keeps of a sandbox."""
    subdomain: str = ""
    env_dir: bool = False   # its machine has ENV_DIR
    ports: dict = dataclasses.field(default_factory=dict)   # port -> Port

    def reset_machine(self):
        """Its machine went (a stop): its exposures and env folder with it."""
        self.env_dir = False
        for port in self.ports.values():
            port.exposed = None


class Previews:
    """The preview part of the Boat backend (mixed into Boat)."""

    def _kept_of(self, sandbox_id: str) -> Kept:
        with self._lock:
            return self._kept.setdefault(sandbox_id, Kept())

    def _port(self, sandbox_id: str, port: int) -> Port:
        kept = self._kept_of(sandbox_id)
        with self._lock:
            return kept.ports.setdefault(port, Port())

    def _forget(self, sandbox_id: str, *, gone: bool = False):
        """What it kept of a sandbox whose machine went (a stop), or, gone, all of it."""
        if gone:
            with self._lock:
                self._kept.pop(sandbox_id, None)
        elif sandbox_id in self._kept:
            self._kept[sandbox_id].reset_machine()

    def listed(self, sandbox_ids: set) -> None:
        """After a full listing: forget what it kept of sandboxes no longer there."""
        with self._lock:
            for sandbox_id in set(self._kept) - set(sandbox_ids):
                del self._kept[sandbox_id]

    @property
    def ingress(self) -> str:
        return f"https://{INGRESS_NAME}.{HOSTED_DOMAIN}"

    def own_route(self, host, sandbox_id, port):
        return None

    def upstream_host(self, port, sandbox_id):
        """<subdomain>-<port>.on.boat.dev, as exposing the port returned it."""
        kept = self._kept.get(sandbox_id)
        found = kept.ports.get(int(port)) if kept else None
        if found and found.exposed:
            return found.exposed.host
        subdomain = kept.subdomain if kept else ""
        if not subdomain:
            subdomain = self._sandbox(sandbox_id).subdomain or ""
            if subdomain:
                self._kept_of(sandbox_id).subdomain = subdomain
        return f"{subdomain or sandbox_id}-{int(port)}.{HOSTED_DOMAIN}"

    def upstream_token(self, sandbox_id, port, *, fetch=True):
        """The port's token, exposing the port on its first request: private
        (never public), relayed from loopback when the app listens only
        there. Until something answers on the port, each request (at most
        every few seconds) looks at how it listens again and exposes it again
        when that changed; once it answers, the exposure is kept until the
        sandbox stops or goes."""
        kept = self._kept.get(sandbox_id)
        found = kept.ports.get(int(port)) if kept else None
        exposed = found.exposed if found else None
        if not fetch or (exposed and exposed.fresh()):
            return exposed.token if exposed else ""
        state = self._port(sandbox_id, int(port))
        with state.lock:
            if state.exposed and state.exposed.fresh():
                return state.exposed.token
            seen = self._listening_on(sandbox_id, int(port))
            if seen is not None:
                state.relay = seen == LOOPBACK
            if state.exposed and state.exposed.relay == state.relay:
                state.exposed = dataclasses.replace(state.exposed, settled=seen is not None, checked=time.monotonic())
            else:
                host, token = self._expose(sandbox_id, int(port), state.relay)
                state.exposed = Exposed(host, token, state.relay, seen is not None, time.monotonic())
            return state.exposed.token

    def _expose(self, sandbox_id: str, port: int, loopback: bool) -> tuple[str, str]:
        from boat_sdk.models import HostPortRequest
        found = self.call(lambda: self.api().host_port(sandbox_id, HostPortRequest(port=port, public=False,
                                                                                   localhost=loopback)),
                          sandbox_id=sandbox_id)
        parts = urllib.parse.urlsplit(found.url or "")
        token = urllib.parse.parse_qs(parts.query).get("_token", [""])[0]
        if not parts.hostname or not token:
            raise Failed(f"Boat exposed port {port} of sandbox {sandbox_id} without a token; its previews are not "
                         "served without one")
        return parts.hostname, token

    def _listening_on(self, sandbox_id: str, port: int) -> str | None:
        """How the port listens now (listening.listening); None also when the sandbox did not answer."""
        try:
            return listening(Handle(self, sandbox_id), port)
        except (CommandFailed, Timeout, NotRunning):
            return None

    def desktop_url(self, sandbox_id):
        """A fresh URL of Boat's desktop for the sandbox (it carries a token
        that works for 10 minutes)."""
        for _ in range(30):
            found = self.call(lambda: self.api().desktop(sandbox_id), sandbox_id=sandbox_id)
            if found.desktop_url:
                return found.desktop_url
            time.sleep(POLL_SECONDS)
        raise Failed(f"Boat did not open the desktop of sandbox {sandbox_id}")
