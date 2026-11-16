"""What sandboxes may reach, as far as the runtime enforces it: the access
and network settings it can't enforce are refused (runtime.Capabilities),
in words that name the capability, so no setting silently does nothing.
Every writer of those sections imports this module (settings_api,
sbx-admin config set)."""
from . import runtime, settings

SHARED = ("public_repos and network.internal_cidrs can't both be set: the runtime opens the internal ranges to "
          "every sandbox alike (private_networks), so untrusted repos would reach your shared services. Serve "
          "public repos from another installation.")


def offered() -> dict:
    """The settings this runtime can enforce, by name: what the settings page offers."""
    caps = runtime.capabilities()
    return {"public_repos": caps.egress_rules, "internal_cidrs": caps.private_networks}


@settings.check("access")
def _access(before, after):
    if not after.public_repos:
        return
    if not runtime.capabilities().egress_rules:
        raise ValueError("public_repos needs a runtime that applies each sandbox's egress rules (egress_rules), "
                         "and this one doesn't: an untrusted repo's sandbox couldn't be fenced")
    if settings.current().network.internal_cidrs:
        raise ValueError(SHARED)


@settings.check("network")
def _network(before, after):
    if not after.internal_cidrs:
        return
    if not runtime.capabilities().private_networks:
        raise ValueError("network.internal_cidrs needs a runtime whose sandboxes reach this host's private network "
                         "(private_networks), and this one's don't: the ranges would do nothing")
    if settings.current().access.public_repos:
        raise ValueError(SHARED)
