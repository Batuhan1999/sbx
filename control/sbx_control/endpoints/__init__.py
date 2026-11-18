"""Endpoints: this host's private services (a managed server, an operator's
internal host) for sandboxes on a runtime that doesn't reach its private
network (capabilities.private_networks false: a provider's, such as E2B
Cloud), over one TLS path shared by every service and protocol.

An endpoint is {name, target host:port}: a managed server's (`--server`) or
an internal host's within network.internal_cidrs. Off until an admin adds
one; with none, every name goes to the HTTPS server as before. Access is
per repo and explicit, whatever the repo's trust, and enforced by
certificates: every endpoint has a CA of its own, nginx verifies a client
against that endpoint's CA only, and a sandbox gets certificates only for
the endpoints its repo may use.

- model: the records and grants, and an admin's changes.
- access: what a repo's sandboxes need and may not use (the gate, advice).
- certs: the CAs, the sandboxes' certificates, the forwarder's credential.
- sandbox: the values a sandbox gets, its forwarder's config and
  certificates (the endpoints' SandboxLayer, LAYER).
- nginx: the host's stream configuration, applied as root.
- status: what the API, the CLI and doctor say."""
from .access import advice, missing, needs, refusal
from .certs import issue, sandbox_of, used_by
from .model import (Conflict, Endpoint, Refused, add, domain, env_name, exist, forget_server, grant, lock,
                    permitted, remove, revoke, rows, set_repos, slug_taken)
from .nginx import apply_host, stream_config, write_nginx
from .sandbox import Layer, Routes, issue_for, routes
from .status import describe, doctor_lines, needed, one

LAYER = Layer()

__all__ = ["Conflict", "Endpoint", "LAYER", "Refused", "Routes", "add", "advice", "apply_host", "describe",
           "doctor_lines", "domain", "env_name", "exist", "forget_server", "grant", "issue", "issue_for", "lock", "missing",
           "needed", "needs", "one", "permitted", "refusal", "remove", "revoke", "routes", "rows", "sandbox_of",
           "set_repos", "slug_taken", "stream_config", "used_by", "write_nginx"]
