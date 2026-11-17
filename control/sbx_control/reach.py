"""What sandboxes may reach, as far as the runtimes enforce it: the access
and network settings they can't enforce are refused (runtime.Capabilities),
in words that name the capability, so no setting silently does nothing.
With several runtimes, public_repos needs every one to fence a sandbox
(an untrusted repo's may run on any), and internal_cidrs one whose
sandboxes reach them. Every writer of those sections imports this module
(settings_api, sbx-admin config set)."""
from . import runtime, settings

SHARED = ("public_repos and network.internal_cidrs can't both be set: the runtime opens the internal ranges to "
          "every sandbox alike (private_networks), so untrusted repos would reach your shared services. Serve "
          "public repos from another installation.")


def _fence() -> bool:
    return all(runtime.capabilities(kind).egress_rules for kind in runtime.enabled())


def _private() -> bool:
    """Whether internal ranges do something: a runtime's sandboxes reach them,
    or, with a domain, sandboxes on any runtime through endpoints (endpoints.py)."""
    return any(runtime.capabilities(kind).private_networks for kind in runtime.enabled()) \
        or bool(settings.current().domain.name)


def offered() -> dict:
    """The settings the runtimes can enforce, by name: what the settings page offers."""
    return {"public_repos": _fence(), "internal_cidrs": _private()}


@settings.check("access")
def _access(before, after):
    if not after.public_repos:
        return
    if not _fence():
        raise ValueError("public_repos needs a runtime that applies each sandbox's egress rules (egress_rules), "
                         "and this one doesn't: an untrusted repo's sandbox couldn't be fenced")
    if settings.current().network.internal_cidrs:
        raise ValueError(SHARED)


@settings.check("network")
def _network(before, after):
    if not after.internal_cidrs:
        return
    if not _private():
        raise ValueError("network.internal_cidrs needs a runtime whose sandboxes reach this host's private network "
                         "(private_networks), and this one's don't: the ranges would do nothing until a domain is "
                         "connected and they get endpoints")
    if settings.current().access.public_repos:
        raise ValueError(SHARED)
