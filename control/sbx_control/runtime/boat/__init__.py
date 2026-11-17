"""Boat (boat.dev): hosted Ubuntu sandboxes, through Boat's Python SDK
(boat-sdk), which only this package imports. It has no host side
(HostSide), like E2B Cloud.

What sbx asks for, as Boat has it, one module each:

- plans: Boat's plans and machines as sbx's limits, settings and presets.
- errors: Boat's answers as sbx's errors (call, BoatError).
- sandbox: commands and files through Boat's API (Handle).
- lifecycle: sandboxes from saved snapshots; pause is Boat's stop, so every
  resume is a cold boot (cold_resume): sbx re-ships /run/sbx and restarts
  the recipe's processes. Isolation: every sandbox carries labels
  (sbx_install, and sbx_template, since Boat lists no template), listed with
  label filters; templates and snapshots are named with the installation's
  prefix (runtime.template_name).
- build: templates as saved snapshots, built by replaying the TemplateSpec.
- previews: a port answers only with its own token (a cookie), exposed on
  the gate's first request; Boat's own desktop.
- account: the plan and the key's expiry, for the settings page.

No per-sandbox egress rules and no route to this host's private network
(egress_rules, private_networks False): untrusted public repos are refused
and managed servers are reached through endpoints."""
from __future__ import annotations

import threading
import urllib.parse
import urllib.request

from ..base import HOSTNAME, Capabilities, Failed, HostSide
from .account import Account
from .build import Builds
from .errors import call, is_failure
from .lifecycle import Sandboxes
from .plans import API_KEY, API_URL_KEY, DEFAULT_API_URL, KIND, LIMITS, ORG_KEY, PRESETS, SETTINGS, limits_from
from .previews import COOKIE, HOSTED_DOMAIN, RESERVED_PORTS, Previews


class Boat(Sandboxes, Builds, Previews, Account, HostSide):
    kind = KIND
    title = "Boat"
    capabilities = Capabilities(snapshots=True, private_networks=False, egress_rules=False)
    ingress_cleared = ()
    ports = RESERVED_PORTS
    credential_keys = (API_KEY, API_URL_KEY, ORG_KEY, *(key for key, _ in LIMITS.values()))
    secret_key = API_KEY
    settings = SETTINGS
    presets = PRESETS
    upstream_token_header = None
    upstream_token_cookie = COOKIE
    desktop = "provider"
    cold_resume = True
    internal_suffix = HOSTED_DOMAIN
    call = staticmethod(call)

    def __init__(self):
        self._key = ""
        self._url = DEFAULT_API_URL
        self._org = ""
        self._limits = limits_from({})
        self._local = threading.local()
        self.transport = None   # a stand-in for the SDK's HTTP client (tests)
        self.http_open = None   # a stand-in for the signed-URL transfers (tests)
        self._kept: dict = {}   # sandbox id -> previews.Kept
        self._plan: tuple = (0.0, "")   # (when, Boat's plan key) as last asked
        self._lock = threading.Lock()

    def configure(self, secrets):
        key = secrets.get(API_KEY, "")
        if not key:
            raise SystemExit(f"missing platform secret: {API_KEY} (/etc/sbx/runtime.env), an API key from Boat's "
                             f"dashboard: sbx-admin runtime add {KIND} reads it from stdin")
        url = secrets.get(API_URL_KEY) or DEFAULT_API_URL
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not HOSTNAME.fullmatch(parts.hostname or ""):
            raise SystemExit(f"{API_URL_KEY}={url!r} in /etc/sbx/runtime.env is not an https URL such as "
                             f"{DEFAULT_API_URL}")
        self._key, self._url, self._org = key, url.rstrip("/"), secrets.get(ORG_KEY, "")
        self._limits = limits_from(secrets)

    def api(self):
        """The SDK's API object over this backend's own connection, one per thread."""
        from boat_sdk import ApiClient, Configuration
        from boat_sdk.api.boat_api import BoatApi
        if not self._key:
            raise Failed(f"Boat is not configured: no {API_KEY} (sbx-admin runtime add {KIND})")
        found = getattr(self._local, "api", None)
        if found is None or getattr(self._local, "key", None) != (self._key, self._url):
            client = ApiClient(Configuration(host=self._url, access_token=self._key))
            if self.transport is not None:
                client.rest_client = self.transport
            found = self._local.api = BoatApi(client)
            self._local.key = (self._key, self._url)
        return found

    def _org_args(self) -> dict:
        return {"org": self._org} if self._org else {}

    def http(self, url: str, *, method: str, data: bytes | None = None) -> bytes:
        """A signed upload or download URL's transfer (no API key: it carries its own)."""
        if self.http_open is not None:
            return self.http_open(url, method, data)
        request = urllib.request.Request(url, data=data, method=method)
        with urllib.request.urlopen(request, timeout=600) as response:
            return response.read()

    def failure(self, error):
        return is_failure(error)

    def limits(self):
        return self._limits
