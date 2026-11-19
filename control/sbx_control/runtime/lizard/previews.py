"""Previews on Lizard: a port is served at
https://<sandbox-id>-<port>.sandbox.<region>.onlizard.com once it is
published (POST /expose/<port>), and only with the sandbox's access token,
which nginx sends as the X-Lizard-Access-Token header (upstream_token_at);
Lizard strips it before the app (lizard.build/docs/sandboxes/preview-urls).
A port is published the first time the gate asks for it and what Lizard
answered (its host, the token) kept until the sandbox pauses or goes.

Lizard serves only what listens on 0.0.0.0: it has no relay for an app on
loopback."""
from __future__ import annotations

from ..base import Failed
from ..hosted import wrapped

TOKEN_HEADER = "X-Lizard-Access-Token"
INGRESS_NAME = "sbx-ingress"       # any name under the region's wildcard; nginx asks by each sandbox's own (SNI)
RESERVED_PORTS = frozenset({49983})   # Lizard's own: "you cannot expose it"
FETCH_RETRY_SECONDS = 15           # a failed publish answers from memory this long (an outage)


class Previews:
    """The preview part of the Lizard backend (mixed into Lizard)."""

    @property
    def ingress(self) -> str:
        return f"https://{INGRESS_NAME}.{self._preview_domain}"

    @property
    def internal_suffix(self) -> str:
        return self._preview_domain

    def upstream_host(self, port, sandbox_id):
        """As publishing the port answered it, else as Lizard's docs form it."""
        kept = self._exposed.get((sandbox_id, int(port)))
        return kept[0] if kept else f"{sandbox_id}-{int(port)}.{self._preview_domain}"

    def upstream_token(self, sandbox_id, port, *, fetch=True):
        """The sandbox's access token, publishing the port on its first
        request (one publish per port at a time). "" without fetch when the
        port is not published yet."""
        key = (sandbox_id, int(port))
        if not fetch:
            return (self._exposed.get(key) or ("", ""))[1]
        return self._exposed.get_or_fetch(key, lambda: self._expose(sandbox_id, int(port)), FETCH_RETRY_SECONDS)[1]

    def _expose(self, sandbox_id: str, port: int) -> tuple[str, str]:
        try:
            found = self.call("POST", f"/api/sandboxes/{sandbox_id}/expose/{port}", sandbox_id=sandbox_id) or {}
        except Exception as error:
            raise wrapped(error, f"Lizard did not publish port {port} of sandbox {sandbox_id}") from error
        host, token = str(found.get("hostname") or "").lower(), found.get("accessToken") or ""
        if not host or not token:
            raise Failed(f"Lizard published port {port} of sandbox {sandbox_id} without a "
                         f"{'token' if host else 'hostname'}; its previews are not served without one")
        if not host.endswith("." + self._preview_domain):
            raise Failed(f"Lizard serves sandbox {sandbox_id} at {host}, outside {self._preview_domain}, where this "
                         "installation sends previews: set LIZARD_REGION to the sandboxes' region (or "
                         "LIZARD_PREVIEW_DOMAIN to that host's domain)")
        return host, token
