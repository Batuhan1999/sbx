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

import threading

from ..base import Capabilities, HostSide, Limits
from ..e2b import DEFAULT_DOMAIN, ENVD_PORT, Compute

KIND = "e2b-cloud"
API_KEY = "E2B_CLOUD_API_KEY"
DOMAIN_KEY = "E2B_CLOUD_DOMAIN"
TOKEN_HEADER = "e2b-traffic-access-token"
# The limits, by Limits field: the runtime.env key that sets each and its
# default, E2B's Hobby plan (a sandbox runs an hour before its timeout, up
# to 8 vCPUs, 8 GiB and 10 GiB of disk; 20 at once).
LIMITS = {"timeout_seconds": ("E2B_CLOUD_MAX_TIMEOUT", 3600), "cpu": ("E2B_CLOUD_MAX_CPU", 8),
          "memory_mb": ("E2B_CLOUD_MAX_MEMORY_MB", 8192), "disk_mb": ("E2B_CLOUD_MAX_DISK_MB", 10240),
          "sandboxes": ("E2B_CLOUD_MAX_SANDBOXES", 20)}
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


class Cloud(Compute, HostSide):
    kind = KIND
    title = "E2B Cloud"
    capabilities = Capabilities(snapshots=True, private_networks=False, egress_rules=True)
    ingress_cleared = ()
    ports = frozenset({ENVD_PORT})
    credential_keys = (API_KEY, DOMAIN_KEY, *(key for key, _ in LIMITS.values()))
    secret_key = API_KEY
    upstream_token_header = TOKEN_HEADER

    def __init__(self):
        super().__init__()
        self._domain: str | None = None
        self._limits = _limits({})
        self._tokens: dict[str, str] = {}
        self._fetching = threading.Lock()

    # Its connection and where previews go

    def configure(self, secrets):
        key = secrets.get(API_KEY, "")
        if not key:
            raise SystemExit(f"missing platform secret: {API_KEY} (/etc/sbx/runtime.env), an API key from E2B's "
                             f"dashboard: sbx-admin runtime add {KIND} reads it from stdin")
        self._domain = secrets.get(DOMAIN_KEY) or DEFAULT_DOMAIN
        self._limits = _limits(secrets)
        self.connect_with(api_key=key, domain=self._domain, api_url=f"https://api.{self._domain}")

    @property
    def domain(self) -> str:
        """E2B's domain: E2B_CLOUD_DOMAIN, else e2b.app. Read from runtime.env
        before configure (nginx's configuration is written without it)."""
        if self._domain is None:
            from ... import vault
            return vault.runtime((DOMAIN_KEY,)).get(DOMAIN_KEY) or DEFAULT_DOMAIN
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

    def _remember(self, handle):
        if handle.traffic_access_token:
            self._tokens[handle.sandbox_id] = handle.traffic_access_token
        return handle

    def create(self, template, **options):
        return self._remember(super().create(template, **options))

    def connect(self, sandbox_id, *, timeout=None):
        return self._remember(super().connect(sandbox_id, timeout=timeout))

    def upstream_token(self, sandbox_id):
        """The sandbox's traffic token: as create or connect returned it,
        else (after a restart) from one connect, which resumes a paused
        sandbox and leaves a running one's timeout as it is."""
        if sandbox_id not in self._tokens:
            with self._fetching:
                if sandbox_id not in self._tokens:
                    self.connect(sandbox_id)
        return self._tokens.get(sandbox_id, "")

    def kill(self, sandbox_id):
        self._tokens.pop(sandbox_id, None)
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
            running = len(self.sandboxes(None, ["running"]))
        except Exception as error:
            return Status("runtime", "Sandbox runtime", "attention", f"E2B Cloud did not answer: {error}", facts,
                          (), ("test",))
        facts["running"] = running
        most = self._limits.sandboxes
        full = most is not None and running >= most
        summary = f"E2B Cloud · {running}{f' of {most}' if most else ''} running" + (" · full" if full else "")
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
