"""E2B Cloud: E2B's hosted sandboxes, through the same SDK adapter as Embed
(runtime.e2b) with the VMs at E2B instead of on this host. It has no host
side (Backend.host): no KVM, units, firewall, disk or capacity here, so a small
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

from ...connections.base import HOWTO
from ..base import HOSTNAME, Capabilities, Failed, LimitRefused, LimitSpec, Preset, SettingField, TokenPlace
from ..e2b import DEFAULT_DOMAIN, ENVD_PORT, Compute, Handle
from ..hosted import PerSandbox, hosted_status, limit_settings, limits_from, wrapped

KIND = "e2b-cloud"
API_KEY = "E2B_CLOUD_API_KEY"
DOMAIN_KEY = "E2B_CLOUD_DOMAIN"
TOKEN_HEADER = "e2b-traffic-access-token"
# The limits: the runtime.env key that sets each and its default, E2B's
# Hobby plan (a sandbox runs an hour before its timeout, up to 8 vCPUs,
# 8 GiB and 10 GiB of disk; 10 at once): an installation that never chose a
# plan is on Hobby (PRESETS). The web app's slider for each: timeout, disk
# and sandboxes at once go up to what E2B's plans publish (PRESETS: 24 h,
# 20 GiB, 100; the timeout no shorter than base.MIN_TIMEOUT_SECONDS). vCPUs
# and memory start at E2B's 1 vCPU and 1 GiB; Pro allows "8+" without a
# published number, so their range ends at 32 vCPUs and 64 GiB, which is
# sbx's bound, not E2B's: a value E2B refuses is said as E2B says it.
LIMITS = (
    LimitSpec("timeout_seconds", "E2B_CLOUD_MAX_TIMEOUT", 3600, "Longest run", "seconds", 300, 86400, 300),
    LimitSpec("cpu", "E2B_CLOUD_MAX_CPU", 8, "vCPUs", "", 1, 32, 1),
    LimitSpec("memory_mb", "E2B_CLOUD_MAX_MEMORY_MB", 8192, "Memory", "MiB", 1024, 65536, 1024),
    LimitSpec("disk_mb", "E2B_CLOUD_MAX_DISK_MB", 10240, "Disk", "MiB", 1024, 20480, 1024),
    LimitSpec("sandboxes", "E2B_CLOUD_MAX_SANDBOXES", 10, "Sandboxes at once", "", 1, 100, 1),
)
SETTINGS = (
    SettingField(API_KEY, "API key", secret=True, help="From E2B's dashboard: Team → API keys."),
    SettingField(DOMAIN_KEY, "Domain", default=DEFAULT_DOMAIN, optional=True, kind="hostname", rekey=True,
                 help="Only for a self-hosted E2B."),
    *limit_settings(LIMITS),
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
PRESETS = tuple(p.bound(LIMITS) for p in (
    Preset("hobby", "Hobby", {"timeout_seconds": 3600, "cpu": 8, "memory_mb": 8192, "disk_mb": 10240,
                              "sandboxes": 10}, _PLAN_SOURCE),
    Preset("pro", "Pro", {"timeout_seconds": 86400, "cpu": 8, "memory_mb": 8192, "disk_mb": 20480,
                          "sandboxes": 100}, _PLAN_SOURCE, adjustable=("cpu", "memory_mb"),
           note="E2B may allow more on Pro."),
))
FETCH_RETRY_SECONDS = 15   # a failed token fetch answers from memory this long (an outage)
INGRESS_NAME = "sbx-ingress"   # any name under the sandboxes' wildcard: the apex resolves elsewhere


def _limits(secrets: dict):
    return limits_from(secrets, LIMITS, "your E2B plan")


def _domain(found: str) -> str:
    """E2B's domain, checked: it goes into nginx's upstream and the API's URL."""
    if not HOSTNAME.fullmatch(found):
        raise SystemExit(f"{DOMAIN_KEY}={found!r} in /etc/sbx/runtime.env is not a host name such as e2b.app")
    return found


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


class Cloud(Compute):
    kind = KIND
    title = "E2B Cloud"
    capabilities = Capabilities(snapshots=True, private_networks=False, egress_rules=True)
    ingress_cleared = ()
    ports = frozenset({ENVD_PORT})
    credential_keys = (API_KEY, DOMAIN_KEY, *(spec.key for spec in LIMITS))
    secret_key = API_KEY
    settings = SETTINGS
    presets = PRESETS
    help_url = f"{HOWTO}/e2b.md"
    upstream_token_at = TokenPlace("header", TOKEN_HEADER)
    # Explicit, not the E2B project's default: a sandbox's ports answer only
    # with its traffic token.
    network_defaults = {"allow_public_traffic": False}

    def __init__(self):
        super().__init__()
        self.domain = DEFAULT_DOMAIN
        self._limits = _limits({})
        self._tokens: PerSandbox[str] = PerSandbox()

    # Its connection and where previews go

    def configure(self, secrets):
        """Its connection from its keys; the domain and limits apply without
        the API key (nginx's configuration is written from them), the key is
        needed (SystemExit) for everything else (configured)."""
        self.domain = _domain(secrets.get(DOMAIN_KEY) or DEFAULT_DOMAIN)
        self._limits = _limits(secrets)
        key = secrets.get(API_KEY, "")
        if not key:
            raise SystemExit(f"missing platform secret: {API_KEY} (/etc/sbx/runtime.env), an API key from E2B's "
                             f"dashboard: sbx-admin runtime add {KIND} reads it from stdin")
        self.connect_with(api_key=key, domain=self.domain, api_url=f"https://api.{self.domain}")

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

    def limits(self):
        return self._limits

    # The traffic token

    def _handle(self, sandbox):
        handle = Handle(sandbox, on_kill=lambda: self._tokens.drop(sandbox.sandbox_id))
        if handle.traffic_access_token:
            self._tokens.put(handle.sandbox_id, handle.traffic_access_token)
        return handle

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
        """The sandbox's traffic token (one for all its ports): as create or
        connect returned it, else (after a restart, with fetch) from one
        connect, which resumes a paused sandbox and leaves a running one's
        timeout as it is; one fetch per sandbox at a time. "" without fetch
        when none is kept; Failed when connect returns none either."""
        if not fetch:
            return self._tokens.get(sandbox_id, "")
        return self._tokens.get_or_fetch(sandbox_id, lambda: self._fetch_token(sandbox_id), FETCH_RETRY_SECONDS)

    def _fetch_token(self, sandbox_id: str) -> str:
        try:
            self.connect(sandbox_id)   # _handle keeps the token it returns
        except Exception as error:
            raise wrapped(error, f"E2B Cloud did not hand over sandbox {sandbox_id}'s token") from error
        token = self._tokens.get(sandbox_id)
        if not token:
            raise Failed(f"E2B Cloud returned no traffic access token for sandbox {sandbox_id}; its previews are "
                         "not served without one")
        return token

    def listed(self, sandbox_ids: set) -> None:
        self._tokens.keep(sandbox_ids)

    def kill(self, sandbox_id):
        self._tokens.drop(sandbox_id)
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
        from ...connections.base import Field
        # The plan's limit is the E2B team's, which other installations on the same key share.
        return hosted_status(self, configured=bool(self._connection.get("api_key")), facts={"domain": self.domain},
                             fields=(Field("domain", "Domain", value=self.domain),),
                             limit_note=" (the plan runs {most} at once per E2B team)")

    def test(self):
        from ...connections.base import Check
        try:
            count = len(self.templates())
        except Exception as error:
            return [Check("E2B Cloud API", False, f"{type(error).__name__}: {error}",
                          f"check {API_KEY} in /etc/sbx/runtime.env (sbx-admin runtime add {KIND})")]
        return [Check("E2B Cloud API", True, f"answers; {count} template names")]
