"""Lizard (lizard.build): hosted sandboxes in Firecracker microVMs, through
Lizard's HTTP API as its SDK (github.com/lizard-build/lizard-sdk) calls it:
plain HTTPS from the standard library, no package. Hosted, like E2B Cloud
and Boat: it has no host part, and the registry scopes it to this
installation (Scoped).

What sbx asks for, as Lizard has it, one module each:

- plans: Lizard's sizes and plans as sbx's limits, settings and presets.
  Every sandbox of the installation runs at one size (the limits').
- errors: the HTTP calls (Api.call) and Lizard's answers as sbx's errors.
- sandbox: commands and files through Lizard's API (Handle); a command past
  Lizard's 600 seconds runs detached in the sandbox and is polled.
- lifecycle: sandboxes from snapshots, in one project; pause keeps the files
  only, so every resume is a cold boot (cold_resume). Lizard's lifetime
  deletes a sandbox, so the pause at sbx's timeout is this backend's own.
- build: templates as snapshots, built by replaying the TemplateSpec on
  Lizard's base template (no image import: image_import False).
- previews: a port is published on the gate's first request and answers
  only with the sandbox's token (a header).
- account: the plan and the project, for the settings page.

No per-sandbox egress ranges (Lizard has an allow-list of hosts, not sbx's
deny_out) and no route to this host's private network (egress_rules,
private_networks False): untrusted public repos are refused and managed
servers are reached through endpoints."""
from __future__ import annotations

import re
import urllib.error
import urllib.parse

from ... import howto
from ..base import HOSTNAME, Backend, Capabilities, Failed, Limits, TokenPlace
from ..hosted import PerSandbox
from .account import Account
from .build import Builds
from .errors import Api
from .lifecycle import Sandboxes
from .plans import (API_KEY, API_URL_KEY, BASE_TEMPLATE_KEY, DEFAULT_API_URL, DEFAULT_BASE_TEMPLATE, DEFAULT_REGION,
                    KIND, LIMITS, PREVIEW_DOMAIN_KEY, PRESETS, PROJECT_KEY, REGION_KEY, SETTINGS, SOURCE)
from .previews import RESERVED_PORTS, TOKEN_HEADER, Previews

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")   # a region id or a template name, as they go into a host name or a request


def preview_domain(region: str) -> str:
    """Where Lizard serves a region's exposed ports (lizard.build/docs/sandboxes/preview-urls)."""
    return f"sandbox.{region}.onlizard.com"


class Lizard(Api, Sandboxes, Builds, Previews, Account, Backend):
    kind = KIND
    title = "Lizard"
    help_url = howto.LIZARD_KEY
    capabilities = Capabilities(snapshots=True, private_networks=False, egress_rules=False, image_import=False)
    ingress_cleared = ()
    ports = RESERVED_PORTS
    credential_keys = (API_KEY, API_URL_KEY, PROJECT_KEY, REGION_KEY, PREVIEW_DOMAIN_KEY, BASE_TEMPLATE_KEY,
                       *(s.key for s in LIMITS))
    secret_key = API_KEY
    settings = SETTINGS
    presets = PRESETS
    upstream_token_at = TokenPlace("header", TOKEN_HEADER)
    cold_resume = True

    def __init__(self):
        self._key = ""
        self._url = DEFAULT_API_URL
        self._project = ""        # as set: an id, slug or name
        self._project_id = ""     # as Lizard resolved it (Account.project)
        self._region = DEFAULT_REGION
        self._preview_domain = preview_domain(DEFAULT_REGION)
        self._base_template = DEFAULT_BASE_TEMPLATE
        self._limits = Limits.read(LIMITS, {}, SOURCE)
        self.transport = None     # a stand-in for the HTTP requests (tests)
        self._exposed: PerSandbox = PerSandbox()   # (sandbox id, port) -> (host, token)
        self._deadlines: dict = {}   # sandbox id -> when sbx's timeout pauses it (time.time)
        self._paused: dict = {}      # sandbox id -> when this process first saw it paused

    def configure(self, secrets):
        """Its connection from its keys. The region, the preview domain and
        the limits apply without the API key (nginx's configuration is
        written from them); the key is needed (SystemExit) for the rest."""
        region = (secrets.get(REGION_KEY) or DEFAULT_REGION).strip().lower()
        if not NAME.fullmatch(region):
            raise SystemExit(f"{REGION_KEY}={region!r} in /etc/sbx/runtime.env is not a Lizard region such as "
                             f"{DEFAULT_REGION}")
        domain = (secrets.get(PREVIEW_DOMAIN_KEY) or preview_domain(region)).strip().lower()
        if not HOSTNAME.fullmatch(domain):
            raise SystemExit(f"{PREVIEW_DOMAIN_KEY}={domain!r} in /etc/sbx/runtime.env is not a host name such as "
                             f"{preview_domain(region)}")
        base = (secrets.get(BASE_TEMPLATE_KEY) or DEFAULT_BASE_TEMPLATE).strip()
        if not NAME.fullmatch(base):
            raise SystemExit(f"{BASE_TEMPLATE_KEY}={base!r} in /etc/sbx/runtime.env is not a Lizard template such "
                             f"as {DEFAULT_BASE_TEMPLATE}")
        self._region, self._preview_domain, self._base_template = region, domain, base
        self._limits = Limits.read(LIMITS, secrets, SOURCE)
        try:
            self._size()   # the limits are the installation's size: Lizard must have one that holds them
        except Failed as error:
            raise SystemExit(f"/etc/sbx/runtime.env: {error}") from None
        key = secrets.get(API_KEY, "")
        if not key:
            raise SystemExit(f"missing platform secret: {API_KEY} (/etc/sbx/runtime.env), an API key from "
                             f"lizard.build: sbx-admin runtime add {KIND} reads it from stdin")
        url = secrets.get(API_URL_KEY) or DEFAULT_API_URL
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not HOSTNAME.fullmatch(parts.hostname or ""):
            raise SystemExit(f"{API_URL_KEY}={url!r} in /etc/sbx/runtime.env is not an https URL such as "
                             f"{DEFAULT_API_URL}")
        project = (secrets.get(PROJECT_KEY) or "").strip()
        if (key, url.rstrip("/"), project) != (self._key, self._url, self._project):
            self._project_id = ""
        self._key, self._url, self._project = key, url.rstrip("/"), project

    def failure(self, error):
        return isinstance(error, urllib.error.URLError)

    def limits(self):
        return self._limits
