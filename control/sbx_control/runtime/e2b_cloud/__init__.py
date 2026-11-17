"""E2B Cloud: E2B's hosted sandboxes, through the same SDK adapter as Embed
(runtime.e2b) with the VMs at E2B instead of on this host. It has no host
side (HostSide): no KVM, units, firewall, disk or capacity here, so a small
server without /dev/kvm runs it.

Its sandboxes call back to this installation's public address (template
bundles, git credentials, catalog actions), so the installation needs a
domain reachable from the internet. They can't reach this host's private
network (private_networks=False: managed servers are refused). E2B applies
each sandbox's deny_out (egress_rules).

E2B answers a sandbox's ports only with that sandbox's traffic token, which
only creating or connecting returns: the backend keeps it, fetches it once
with connect after a restart (which resumes a paused sandbox, as the gate
would), and the gate hands it to nginx for this runtime's requests."""
from __future__ import annotations

import re
import threading
import time

from ..base import HOSTNAME, Capabilities, Failed, HostSide, LimitRefused, Limits, Preset, SettingField
from ..e2b import DEFAULT_DOMAIN, ENVD_PORT, Compute

KIND = "e2b-cloud"
API_KEY = "E2B_CLOUD_API_KEY"
DOMAIN_KEY = "E2B_CLOUD_DOMAIN"
TOKEN_HEADER = "e2b-traffic-access-token"
# The limits, by Limits field: the runtime.env key that sets each and its
# default, E2B's Hobby plan (a sandbox runs an hour before its timeout, up
# to 8 vCPUs, 8 GiB and 10 GiB of disk; 10 at once): an installation that
# never chose a plan is on Hobby (PRESETS).
LIMITS = {"timeout_seconds": ("E2B_CLOUD_MAX_TIMEOUT", 3600), "cpu": ("E2B_CLOUD_MAX_CPU", 8),
          "memory_mb": ("E2B_CLOUD_MAX_MEMORY_MB", 8192), "disk_mb": ("E2B_CLOUD_MAX_DISK_MB", 10240),
          "sandboxes": ("E2B_CLOUD_MAX_SANDBOXES", 10)}
# How each limit is shown and set in the web app: its label, unit and the
# range its slider covers. Timeout, disk and sandboxes at once go up to what
# E2B's plans publish (PRESETS: 24 h, 20 GiB, 100; the timeout no shorter
# than base.MIN_TIMEOUT_SECONDS). vCPUs and memory start at E2B's 1 vCPU and
# 1 GiB; Pro allows "8+" without a published number, so their range ends at
# 32 vCPUs and 64 GiB, which is sbx's bound, not E2B's: a value E2B refuses
# is said as E2B says it.
LIMIT_FORM = {"timeout_seconds": ("Longest run", "seconds", 300, 86400, 300),
              "cpu": ("vCPUs", "", 1, 32, 1),
              "memory_mb": ("Memory", "MiB", 1024, 65536, 1024),
              "disk_mb": ("Disk", "MiB", 1024, 20480, 1024),
              "sandboxes": ("Sandboxes at once", "", 1, 100, 1)}
SETTINGS = (
    SettingField(API_KEY, "API key", secret=True, help="From E2B's dashboard: Team → API keys."),
    SettingField(DOMAIN_KEY, "Domain", default=DEFAULT_DOMAIN, optional=True, kind="hostname", rekey=True,
                 help="Only for a self-hosted E2B."),
    *(SettingField(key, LIMIT_FORM[field][0], default=str(default), kind="int", limit=field, unit=LIMIT_FORM[field][1],
                   min=LIMIT_FORM[field][2], max=LIMIT_FORM[field][3], step=LIMIT_FORM[field][4])
      for field, (key, default) in LIMITS.items()),
)
# E2B's plans as limits, from https://e2b.dev/pricing and
# https://e2b.dev/docs/sandbox-template/customize-cpu-ram (both read
# 2026-11-17). Hobby: "Up to 1 hour per sandbox session", "Up to 10
# concurrently running sandboxes", "10 GiB of sandbox disk", and "Max vCPUs
# per build: 8 on Hobby", RAM "8 GiB on Hobby": fixed. Pro: "Up to 24 hours
# per sandbox session", "Up to 100 concurrently running sandboxes", "20 GiB
# of sandbox disk" are fixed; its vCPUs and RAM are "8+" ("Custom sandbox CPU
# and RAM"), no published number, so they stay adjustable from 8 and 8 GiB.
# LIMITS' defaults are Hobby's.
_PLAN_SOURCE = "https://e2b.dev/pricing"
PRESETS = (
    Preset("hobby", "Hobby", {"E2B_CLOUD_MAX_TIMEOUT": "3600", "E2B_CLOUD_MAX_CPU": "8",
                              "E2B_CLOUD_MAX_MEMORY_MB": "8192", "E2B_CLOUD_MAX_DISK_MB": "10240",
                              "E2B_CLOUD_MAX_SANDBOXES": "10"}, _PLAN_SOURCE),
    Preset("pro", "Pro", {"E2B_CLOUD_MAX_TIMEOUT": "86400", "E2B_CLOUD_MAX_CPU": "8",
                          "E2B_CLOUD_MAX_MEMORY_MB": "8192", "E2B_CLOUD_MAX_DISK_MB": "20480",
                          "E2B_CLOUD_MAX_SANDBOXES": "100"}, _PLAN_SOURCE,
           adjustable=("E2B_CLOUD_MAX_CPU", "E2B_CLOUD_MAX_MEMORY_MB"),
           note="E2B may allow more on Pro."),
)
FETCH_RETRY_SECONDS = 15   # a failed token fetch answers from memory this long (an outage)
INGRESS_NAME = "sbx-ingress"   # any name under the sandboxes' wildcard: the apex resolves elsewhere


def _limits(secrets: dict) -> Limits:
    found = {}
    for field, (key, default) in LIMITS.items():
        value = secrets.get(key) or default
        try:
            found[field] = int(value)
        except (TypeError, ValueError):
            raise SystemExit(f"{key}={value} in /etc/sbx/runtime.env is not a whole number") from None
    try:
        return Limits(**found, source="your E2B plan", keys={f: key for f, (key, _) in LIMITS.items()})
    except ValueError as error:
        raise SystemExit(f"{LIMITS['timeout_seconds'][0]} in /etc/sbx/runtime.env: {error}") from None



_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def _duration(seconds) -> str:
    seconds = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size} {unit}"
    return f"{seconds} s"


def refused_limit(error: BaseException, asked: dict) -> LimitRefused | None:
    """E2B's answer as the limit it refused, when it is one sbx can read for
    sure: a timeout past the plan's ("Timeout cannot be greater than 1
    hours", as E2B answered on a Hobby plan). Any other refusal (sandboxes
    at once, a sandbox's size) is passed on in E2B's own words. asked: what
    sbx asked for, by Limits field."""
    found = re.search(r"timeout cannot be greater than (\d+)\s*(second|minute|hour|day)", str(error).lower())
    if not found:
        return None
    allowed = int(found.group(1)) * _UNITS[found.group(2)]
    wanted = asked.get("timeout_seconds")
    return LimitRefused(f"E2B refused {'a ' + _duration(wanted) + ' timeout' if wanted else 'the timeout'}: "
                        f"your plan allows {_duration(allowed)}", field="timeout_seconds", asked=wanted,
                        allowed=allowed)


class Cloud(Compute, HostSide):
    kind = KIND
    title = "E2B Cloud"
    capabilities = Capabilities(snapshots=True, private_networks=False, egress_rules=True)
    ingress_cleared = ()
    ports = frozenset({ENVD_PORT})
    credential_keys = (API_KEY, DOMAIN_KEY, *(key for key, _ in LIMITS.values()))
    secret_key = API_KEY
    settings = SETTINGS
    presets = PRESETS
    upstream_token_header = TOKEN_HEADER
    # Explicit, not the E2B project's default: a sandbox's ports answer only
    # with its traffic token.
    network_defaults = {"allow_public_traffic": False}

    def __init__(self):
        super().__init__()
        self._domain: str | None = None
        self._limits = _limits({})
        self._tokens: dict[str, str] = {}
        self._locks: dict[str, threading.Lock] = {}   # one token fetch per sandbox at a time
        self._locks_lock = threading.Lock()
        self._failed: dict = {}   # sandbox id -> (when, the error) of a token fetch that failed

    # Its connection and where previews go

    def configure(self, secrets):
        key = secrets.get(API_KEY, "")
        if not key:
            raise SystemExit(f"missing platform secret: {API_KEY} (/etc/sbx/runtime.env), an API key from E2B's "
                             f"dashboard: sbx-admin runtime add {KIND} reads it from stdin")
        domain = secrets.get(DOMAIN_KEY) or DEFAULT_DOMAIN
        if not HOSTNAME.fullmatch(domain):   # it goes into nginx's upstream and the API's URL
            raise SystemExit(f"{DOMAIN_KEY}={domain!r} in /etc/sbx/runtime.env is not a host name such as e2b.app")
        self._domain = domain
        self._limits = _limits(secrets)
        self.connect_with(api_key=key, domain=self._domain, api_url=f"https://api.{self._domain}")

    @property
    def domain(self) -> str:
        """E2B's domain: E2B_CLOUD_DOMAIN, else e2b.app. Read from runtime.env
        before configure (nginx's configuration is written without it)."""
        if self._domain is None:
            from ... import vault
            found = vault.runtime((DOMAIN_KEY,)).get(DOMAIN_KEY) or DEFAULT_DOMAIN
            if not HOSTNAME.fullmatch(found):
                raise SystemExit(f"{DOMAIN_KEY}={found!r} in /etc/sbx/runtime.env is not a host name such as e2b.app")
            return found
        return self._domain

    @property
    def ingress(self) -> str:
        """https to a name under the sandboxes' wildcard; nginx sends each
        sandbox's own host as SNI and Host (upstream_host)."""
        return f"https://{INGRESS_NAME}.{self.domain}"

    @property
    def internal_suffix(self) -> str:
        return self.domain

    def upstream_host(self, port, sandbox_id):
        return f"{port}-{sandbox_id}.{self.domain}"

    def own_route(self, host, sandbox_id, port):
        return None

    def limits(self):
        return self._limits

    # The traffic token

    def _handle(self, sandbox):
        handle = super()._handle(sandbox)
        handle._on_kill = lambda: self._forget(handle.sandbox_id)
        if handle.traffic_access_token:
            self._tokens[handle.sandbox_id] = handle.traffic_access_token
        return handle

    def _forget(self, sandbox_id):
        self._tokens.pop(sandbox_id, None)
        self._failed.pop(sandbox_id, None)
        with self._locks_lock:
            self._locks.pop(sandbox_id, None)

    def _limited(self, call, asked: dict):
        """call's result; a refusal for a limit of the plan as LimitRefused."""
        try:
            return call()
        except Failed:
            raise
        except Exception as error:
            found = refused_limit(error, asked)
            if found is None:
                raise
            raise found from error

    def set_timeout(self, sandbox_id, seconds):
        return self._limited(lambda: super(Cloud, self).set_timeout(sandbox_id, seconds),
                             {"timeout_seconds": seconds})

    def create(self, template, **options):
        """A new sandbox whose ports answer only with its traffic token
        (allow_public_traffic off, whatever the E2B project's default); one
        that comes back without a token is killed, never served without one.
        A limit of the plan it refused is LimitRefused."""
        handle = self._limited(lambda: super(Cloud, self).create(template, **options),
                               {"timeout_seconds": options.get("timeout")})
        if not handle.traffic_access_token:
            try:
                handle.kill()
            except Exception as error:   # it times out on its own
                print(f"E2B Cloud: could not kill {handle.sandbox_id}: {error!r}", flush=True)
            raise Failed(f"E2B Cloud created sandbox {handle.sandbox_id} without a traffic access token, so its "
                         "ports would be open to anyone; it was deleted. Check the E2B project's network settings "
                         "(private sandbox URLs) and the SDK version.")
        return handle

    def upstream_token(self, sandbox_id, port=0, *, fetch=True):
        """The sandbox's traffic token (one for all its ports): as create or connect returned it,
        else (after a restart, with fetch) from one connect, which resumes a
        paused sandbox and leaves a running one's timeout as it is; one
        fetch per sandbox at a time. "" without fetch when none is kept;
        Failed when connect returns none either."""
        if sandbox_id in self._tokens or not fetch:
            return self._tokens.get(sandbox_id, "")
        failed = self._failed.get(sandbox_id)
        if failed and time.monotonic() - failed[0] < FETCH_RETRY_SECONDS:
            raise failed[1]   # asked moments ago: not E2B again for every request of an outage
        with self._locks_lock:
            lock = self._locks.setdefault(sandbox_id, threading.Lock())
        with lock:
            if sandbox_id not in self._tokens:
                failed = self._failed.get(sandbox_id)   # again: a caller waiting here met the same outage
                if failed and time.monotonic() - failed[0] < FETCH_RETRY_SECONDS:
                    raise failed[1]
                try:
                    self.connect(sandbox_id)
                    if not self._tokens.get(sandbox_id):
                        raise Failed(f"E2B Cloud returned no traffic access token for sandbox {sandbox_id}; its "
                                     "previews are not served without one")
                except Exception as error:
                    raised = error if isinstance(error, Failed) else \
                        Failed(f"E2B Cloud did not hand over sandbox {sandbox_id}'s token: {type(error).__name__}: "
                               f"{error}")
                    self._failed[sandbox_id] = (time.monotonic(), raised)
                    raise raised from error
        self._failed.pop(sandbox_id, None)
        return self._tokens[sandbox_id]

    def listed(self, sandbox_ids: set) -> None:
        """After a full listing (the registry's hook): forget what it kept of
        sandboxes no longer there, whoever ended them."""
        for kept in (self._tokens, self._failed):
            for sandbox_id in [s for s in list(kept) if s not in sandbox_ids]:
                kept.pop(sandbox_id, None)
        with self._locks_lock:
            for sandbox_id in [s for s in self._locks if s not in sandbox_ids and not self._locks[s].locked()]:
                del self._locks[sandbox_id]

    def kill(self, sandbox_id):
        self._forget(sandbox_id)
        return super().kill(sandbox_id)

    # What the settings page and the first-run setup show

    def why_busy(self, memory_mb):
        return "E2B Cloud would not pause the sandbox now; pause again in a minute"

    def readiness(self, facts):
        from ... import vault
        from ...connections.base import Check
        return [Check("E2B Cloud configured", bool(vault.runtime((API_KEY,)).get(API_KEY)), "an E2B Cloud API key",
                      f"Add it: sbx-admin runtime add {KIND} (the key on stdin).")]

    def status(self):
        from ...connections.base import Field, Status
        if not self._connection.get("api_key"):
            return Status("runtime", "Sandbox runtime", "not_connected", f"E2B Cloud: no {API_KEY} in runtime.env",
                          {}, (), ("test",))
        facts: dict = {"domain": self.domain}
        try:
            from ... import runtime   # this installation's sandboxes, not the whole E2B team's
            running = len(runtime.sandboxes(None, [runtime.RUNNING], kind=self.kind))
        except Exception as error:
            return Status("runtime", "Sandbox runtime", "attention", f"E2B Cloud did not answer: {error}", facts,
                          (), ("test",))
        facts["running"] = running
        most = self._limits.sandboxes
        full = most is not None and running >= most
        # The plan's limit is the E2B team's, which other installations on the same key share.
        summary = (f"E2B Cloud · {running} running here" + (f" (the plan runs {most} at once per E2B team)"
                                                             if most else "") + (" · full" if full else ""))
        return Status("runtime", "Sandbox runtime", "attention" if full else "connected", summary, facts,
                      (Field("domain", "Domain", value=self.domain),), ("test",))

    def test(self):
        from ...connections.base import Check
        try:
            count = len(self.templates())
        except Exception as error:
            return [Check("E2B Cloud API", False, f"{type(error).__name__}: {error}",
                          f"check {API_KEY} in /etc/sbx/runtime.env (sbx-admin runtime add {KIND})")]
        return [Check("E2B Cloud API", True, f"answers; {count} template names")]
