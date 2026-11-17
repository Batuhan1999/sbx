"""What a repo's sandboxes on a runtime without private_networks need, and
what is missing for them: the create gate's refusal (the managed servers and
connections the repo's attachments bind) and an event's advice (an address
in sbx.yaml's env), each naming the command that fixes it."""
from __future__ import annotations

import re

from .. import settings
from .model import INTERNAL, Refused, domain, internal_address, join, label, routable, split_target

# host:port with an IPv4 address or a bracketed IPv6 one: what the values are scanned for.
HOSTPORT = re.compile(r"(?<![\w.\-])(\d{1,3}(?:\.\d{1,3}){3}|\[[0-9A-Fa-f:.]+\]):(\d{1,5})(?!\d)")
GATED = ("server", "connection")       # what refuses a create; "env" is advice
NO_DOMAIN = " (endpoints need a domain: connect one first)"


def addresses(text) -> list[tuple[str, int]]:
    found = []
    for match in HOSTPORT.finditer(str(text)):
        try:
            found.append(split_target(f"{match.group(1)}:{match.group(2)}"))
        except Refused:
            continue
    return found


def needs(repo: str, sources=GATED) -> list[dict]:
    """The private targets the repo's sandboxes reach, from the sources
    asked for: the managed servers its attachments bind to ("server"), the
    internal hosts (within network.internal_cidrs) its bound connections'
    values ("connection") and its sbx.yaml env ("env") name. Each {server,
    connection, target, source}. A store that does not answer is an error,
    not an empty answer: the gate refuses rather than lets a create through."""
    from .. import hosted_services, recipes
    from ..catalog import attachments
    out, seen = [], set()
    servers = hosted_services.attached_to(repo)
    for server in servers:
        target = hosted_services.target(server)
        target = join(*split_target(target)) if target else ""
        seen.add(target)
        if "server" in sources:
            out.append({"server": server, "connection": server, "target": target, "source": "server"})
    if not settings.current().network.internal_cidrs:
        return out
    values = []
    if "connection" in sources:
        for att in attachments.effective(repo):
            for key in att["connections"].values():
                found = None if key in servers else attachments.connection(key)
                if found:
                    values += [(key, "connection", value) for value in found["inputs"].values()]
    if "env" in sources:
        values += [("sbx.yaml", "env", value) for value in (recipes.load(repo).spec.get("env") or {}).values()]
    for key, source, value in values:
        for host, port in addresses(value):
            target = join(host, port)
            if target not in seen and internal_address(host):
                seen.add(target)
                out.append({"server": "", "connection": key, "target": target, "source": source})
    return out


def missing(repo: str, sources=GATED) -> list[tuple[str, str]]:
    """(what, the command that lets the repo's sandboxes reach it) for each
    private target they need and may not use: no endpoint yet, or an internal
    host's endpoint not granted to the repo."""
    found = routable()
    by_target = {r.target: r for r in found}
    servers = {r.server for r in found if r.server}
    out = []
    for need in needs(repo, sources):
        if need["server"]:
            if need["server"] not in servers:
                out.append((f"the managed server {need['server']}", f"sbx endpoints add --server {need['server']}"))
            continue
        row = by_target.get(need["target"])
        what = (f"the internal host {need['target']} ({need['connection']})" if need["source"] == "connection" else
                f"the internal host {need['target']} named in sbx.yaml's env")
        if row is None:
            name = label(need["connection"]) if need["connection"] != "sbx.yaml" else "NAME"
            out.append((what, f"sbx endpoints add --name {name} --target {need['target']} --repo {repo}"))
        elif row.kind == INTERNAL and repo.lower() not in row.grants:
            out.append((what, f"sbx endpoints grant {row.name} {repo}"))
    return out


def refusal(repo: str, title: str) -> str:
    """Why the runtime titled so can't run the repo's sandboxes yet ("" when it can)."""
    lacking = missing(repo)
    if not lacking:
        return ""
    one = len(lacking) == 1
    return (f"{title} can't run {repo}'s sandboxes yet: they reach this host's private network "
            f"(private_networks) only through endpoints, and {', '.join(what for what, _ in lacking)} "
            f"{'has' if one else 'have'} none; an admin adds {'it' if one else 'them'}: "
            f"{'; '.join(command for _, command in lacking)}{'' if domain() else NO_DOMAIN}, or choose another runtime")


def advice(repo: str) -> list[str]:
    """For each address in the recipe's env its sandboxes won't reach: what an admin adds."""
    return [f"its sandboxes on this runtime don't reach {what}; an admin adds it: {command}"
            f"{'' if domain() else NO_DOMAIN}" for what, command in missing(repo, ("env",))]
