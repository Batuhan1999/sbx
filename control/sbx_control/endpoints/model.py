"""The endpoints and their grants: the records, the names and targets they
may have, and the changes an admin makes (add, grant, revoke, remove), each
recorded at once and brought to the host (the endpoints effect) and to the
repos' running sandboxes (deliver)."""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import ipaddress
import re
import threading
import time

from .. import paths, secret_store, settings, state
from . import certs

LOCAL_PORTS = range(17400, 17800)      # the forwarder's listeners in a sandbox, one per endpoint
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
REVISION = state.Setting("endpoints_revision")
GRANTS = state.Setting("endpoints_grants")   # moves with every change of who may use what


def generation() -> str:
    """What a delivery was issued at: the endpoints' revision and grants. A
    write issued before either moved is issued again (sandbox.push)."""
    return f"{REVISION.get('0')}.{GRANTS.get('0')}"
MANAGED, INTERNAL = "managed-server", "internal"


class Refused(ValueError):
    """The request can't be done as asked (the API answers 400)."""


class Conflict(Refused):
    """Refused for what the installation holds now: the name or target is
    taken, no domain, sandboxes use it (the API answers 409)."""


@dataclasses.dataclass(frozen=True)
class Endpoint:
    name: str
    kind: str                 # MANAGED or INTERNAL
    server: str               # a managed server's name; "" for an internal host
    target: str               # host:port, as normalized() writes it
    local_port: int
    revision: int
    ca_cert: str
    grants: tuple = ()        # an internal host's repos

    @property
    def internal(self) -> bool:
        return self.kind == INTERNAL

    @property
    def unroutable(self) -> str:
        """Why it is not routed now ("" when it is): an internal host's
        target that network.internal_cidrs no longer covers."""
        if self.internal and not internal_address(split_target(self.target)[0]):
            return (f"{self.target} is outside network.internal_cidrs now, so nothing reaches it: add its range "
                    "again (sbx-admin config set network) or remove the endpoint")
        return ""


def rows() -> list[Endpoint]:
    with state.db() as conn:
        found = conn.execute("select name, kind, server, target, local_port, revision, ca_cert from endpoints "
                             "order by name").fetchall()
        grants = conn.execute("select endpoint, repo from endpoint_repos order by repo").fetchall()
    granted: dict = {}
    for endpoint, repo in grants:
        granted.setdefault(endpoint, []).append(repo)
    return [Endpoint(*row, grants=tuple(granted.get(row[0], ()))) for row in found]


def routable() -> list[Endpoint]:
    """The endpoints nginx routes: with a domain, those whose target is still in range."""
    return [r for r in rows() if not r.unroutable] if domain() else []


def find(name: str) -> Endpoint:
    found = next((r for r in rows() if r.name == name), None)
    if found is None:
        raise LookupError(f"no endpoint {name}")
    return found


def exist() -> bool:
    return bool(rows())


def domain() -> str:
    return settings.current().domain.name


def address(name: str) -> str:
    return f"{name}.{domain()}"


@contextlib.contextmanager
def lock():
    """Endpoint changes, and a sandbox's registration (sandboxes.register):
    a name checked against the other kind's and recorded as one step."""
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    with (paths.state_dir() / "endpoints.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        yield


def split_target(target: str) -> tuple[str, int]:
    """(the address, as ipaddress writes it, the port) of host:port or
    [v6]:port; Refused for anything else (a host name: endpoints reach
    addresses, as network.internal_cidrs names ranges)."""
    host, sep, port = str(target).strip().rpartition(":")
    if not sep or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise Refused(f"{target!r} is not HOST:PORT (an IP address and a port, such as 10.0.0.5:7233)")
    try:
        ip = ipaddress.ip_address(host[1:-1] if host.startswith("[") and host.endswith("]") else host)
    except ValueError:
        raise Refused(f"{target!r}: the host must be an IP address within network.internal_cidrs") from None
    return str(ip), int(port)


def join(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def normalized(target: str) -> str:
    return join(*split_target(target))


def label(server: str) -> str:
    return server.lower().replace("_", "-")


def env_name(name: str) -> str:
    return "SBX_ENDPOINT_" + name.upper().replace("-", "_")


def internal_address(host: str) -> bool:
    ip = ipaddress.ip_address(host)
    return any(ip in ipaddress.ip_network(cidr, strict=False) for cidr in settings.current().network.internal_cidrs)


def _check_name(name: str):
    """A host name label no other host of the installation has or may get: on
    443 an endpoint's name wins over a preview's (endpoints are routed first)."""
    if not LABEL.fullmatch(name or ""):
        raise Refused(f"{name!r} is not a host name label: 1 to 63 lowercase letters, digits and hyphens, "
                      "starting and ending with a letter or digit")
    if name in settings.current().reserved_names:
        raise Conflict(f"{name} is taken by this installation's own addresses; choose another name")
    if re.match(r"\d+-", name):
        raise Conflict(f"{name} has the form of a sandbox's preview host (<port>-<id>); choose another name")
    with state.db() as conn:
        slugs = [r[0] for r in conn.execute("select slug from sandboxes where slug is not null and slug != ''")]
    for slug in slugs:
        if name == slug or name.startswith(slug + "-"):
            raise Conflict(f"{name} is a preview host of the sandbox {slug} ({slug}, {slug}-<port>, "
                           f"{slug}-<preview>); choose another name")


def slug_taken(slug: str) -> str:
    """Which endpoint a sandbox slug's hosts would collide with ("" for none):
    the slug itself, or <slug>-<anything>."""
    return next((r.name for r in rows() if r.name == slug or r.name.startswith(slug + "-")), "")


def _repos(repos) -> list[str]:
    out = []
    for repo in repos or []:
        repo = str(repo).strip().lower()
        if not settings.REPO_NAME.fullmatch(repo):
            raise Refused(f"{repo!r} is not a repository (owner/name)")
        if repo not in out:
            out.append(repo)
    return sorted(out)


def _seal_ca(name: str) -> tuple[str, str]:
    """(a new CA's certificate, the reference to its key, sealed in the secret store)."""
    ca_cert, ca_key = certs.make_ca(name)
    return ca_cert, secret_store.seal({"key": ca_key}, key=f"endpoints/{name}/ca/{time.time_ns()}")


def _next_revision(conn) -> int:
    revision = int(REVISION.get("0", conn=conn)) + 1
    REVISION.set(revision, conn=conn)
    return revision


def add(*, server: str | None = None, name: str | None = None, target: str | None = None, repos=None,
        by: str) -> dict:
    """A managed server's endpoint (server, optionally named; for the repos
    attached to it) or an internal host's (name and target within
    network.internal_cidrs; for the repos given). Recorded at once with a CA
    of its own; the host applies it (state applying, then on or failed)."""
    from .. import hosted_services
    if not domain():
        raise Conflict("connect a domain first: endpoints are reached at <name>.<domain> with its certificate")
    if server:
        if target:
            raise Refused("give either a managed server or a target, not both")
        if repos:
            raise Refused("a managed server's endpoint is for the repos attached to it: attach the server to "
                          "the repo instead")
        found = hosted_services.target(server)
        if found is None:
            raise Refused(f"there is no managed server {server} (sbx-admin server ls lists them)")
        kind, name, target = MANAGED, name or label(server), normalized(found)
    else:
        if not name or not target:
            raise Refused("give a managed server (server), or a name and a target HOST:PORT")
        kind, target, server = INTERNAL, normalized(target), ""
        if not internal_address(split_target(target)[0]):
            cidrs = ", ".join(settings.current().network.internal_cidrs) or "none set"
            raise Refused(f"{target} is outside network.internal_cidrs ({cidrs}); an endpoint reaches only "
                          "those ranges, or a managed server (--server)")
    grants = _repos(repos)
    with lock():
        _check_name(name)
        existing = rows()
        if any(r.name == name for r in existing):
            raise Conflict(f"there is an endpoint {name} already")
        if server and (same := next((r for r in existing if r.server == server), None)):
            raise Conflict(f"the managed server {server} has an endpoint already: {same.name}")
        if same := next((r for r in existing if r.target == target), None):
            raise Conflict(f"{target} has an endpoint already: {same.name}")
        used = {r.local_port for r in existing}
        port = next((p for p in LOCAL_PORTS if p not in used), None)
        if port is None:
            raise Conflict(f"this installation has {len(LOCAL_PORTS)} endpoints, the most it keeps")
        ca_cert, reference = _seal_ca(name)
        with state.db() as conn:
            conn.execute("begin immediate")
            conn.execute("insert into endpoints (name, kind, server, target, local_port, revision, created_by, "
                         "ca_cert, ca_key) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (name, kind, server or "", target, port, _next_revision(conn), by, ca_cert, reference))
            conn.executemany("insert into endpoint_repos (endpoint, repo) values (?, ?)", [(name, r) for r in grants])
            conn.execute("commit")
    settings.request_effects(settings.HostUnit.ENDPOINTS)
    deliver(name, repos_of(find(name)))
    return _answer(name)


def set_repos(name: str, repos) -> dict:
    """Replace an internal host's grants (the web app's full list)."""
    grants = _repos(repos)
    return _change_grants(name, lambda old: grants)


def grant(name: str, repo: str) -> dict:
    """One more repo whose sandboxes may use an internal host's endpoint."""
    repo = _repos([repo])[0]
    return _change_grants(name, lambda old: sorted({*old, repo}))


def revoke(name: str, repo: str) -> dict:
    """One repo less; LookupError when it was not granted."""
    repo = _repos([repo])[0]

    def without(old):
        if repo not in old:
            raise LookupError(f"{repo} is not granted the endpoint {name}")
        return [r for r in old if r != repo]
    return _change_grants(name, without)


def _change_grants(name: str, change) -> dict:
    """Grants changed under the lock (no other change runs meanwhile). A
    revoke makes the endpoint's CA anew, so what the revoked repo's
    sandboxes hold (and any copy of it) no longer passes once nginx has the
    new one; the sandboxes still granted get certificates from it at once."""
    with lock():
        row = find(name)
        if not row.internal:
            raise Refused(f"{name} is a managed server's endpoint, for the repos attached to it: attach the "
                          "server to the repo instead")
        old = list(row.grants)
        new = change(old)
        revoked = set(old) - set(new)
        # Sealed before the transaction: the store may write to sbx.db too.
        ca_cert, reference = _seal_ca(name) if revoked else (None, None)
        with state.db() as conn:
            conn.execute("begin immediate")
            GRANTS.set(int(GRANTS.get("0", conn=conn)) + 1, conn=conn)
            conn.execute("delete from endpoint_repos where endpoint = ?", (name,))
            conn.executemany("insert into endpoint_repos (endpoint, repo) values (?, ?)", [(name, r) for r in new])
            if revoked:
                old_key = conn.execute("select ca_key from endpoints where name = ?", (name,)).fetchone()[0]
                conn.execute("update endpoints set ca_cert = ?, ca_key = ?, revision = ? where name = ?",
                             (ca_cert, reference, _next_revision(conn), name))
            conn.execute("commit")
    if revoked:
        secret_store.discard(old_key)
        settings.request_effects(settings.HostUnit.ENDPOINTS)
        deliver(name, set(old) | set(new))   # new certificates for all, none for the revoked
    else:
        deliver(name, set(new) - set(old))
    return _answer(name)


def _answer(name: str) -> dict:
    """The endpoint as the API shows it."""
    from .status import one
    return one(name)


def remove(name: str) -> dict:
    """Refused (Conflict) while sandboxes on a provider runtime use it."""
    with lock():
        row = find(name)
        using = certs.used_by().get(name)
        if using:
            raise Conflict(f"sandboxes of {', '.join(using)} use the endpoint {name}; delete them first "
                           "(sbx ls, sbx down NAME)")
        _delete(row)
    return {"name": name, "deleted": True}


def forget_server(server: str):
    """A managed server deleted with its data: its endpoint goes too."""
    with lock():
        for row in rows():
            if row.server == server:
                _delete(row)


def _delete(row: Endpoint):
    """Under the lock."""
    with state.db() as conn:
        conn.execute("begin immediate")
        conn.execute("delete from endpoint_repos where endpoint = ?", (row.name,))
        secret_store.delete_rows(conn, "endpoints", "name = ?", (row.name,))
        _next_revision(conn)
        conn.execute("commit")
    settings.request_effects(settings.HostUnit.ENDPOINTS)
    deliver(row.name, repos_of(row))


# Who may use one


def _bound_repos(server: str) -> list[str]:
    """The repos whose attachments bind to the managed server."""
    from .. import hosted_services
    from ..catalog import attachments
    return sorted(r for r in attachments.repos() if server in hosted_services.attached_to(r))


def repos_of(row: Endpoint) -> list[str]:
    return list(row.grants) if row.internal else _bound_repos(row.server)


def permitted(repo: str) -> list[Endpoint]:
    """The routed endpoints the repo's sandboxes may use: its attached
    managed servers', and the internal hosts' granted to it."""
    from .. import hosted_services
    if not repo:
        return []
    repo = repo.lower()
    found = routable()
    servers = set(hosted_services.attached_to(repo)) if any(not r.internal for r in found) else set()
    return [r for r in found if (repo in r.grants if r.internal else r.server in servers)]


# Delivery to the sandboxes


def deliver(name: str, repos):
    """Bring the repos' sandboxes an endpoint change without restarting their
    apps: their runtime written again (sandboxes.deliver_env, which keeps it
    while the runtime does not answer), so the forwarder's config and
    certificates and SBX_ENDPOINT_<NAME> in the env are what the repo may
    use now. Off the admin's request: listing the sandboxes asks the runtimes."""
    from .. import sandboxes
    for repo in sorted(set(repos)):
        _background(lambda repo=repo: sandboxes.deliver_env(repo, [env_name(name)], restart=False))


def _background(work):
    threading.Thread(target=work, name="endpoint-delivery", daemon=True).start()


SCHEMA = state.Schema(tables="""
create table if not exists endpoints (
    name text primary key, kind text not null, server text not null default '', target text not null unique,
    local_port integer not null unique, revision integer not null, created_by text not null,
    created_at text not null default current_timestamp, ca_cert text not null, ca_key text not null);
create table if not exists endpoint_repos (endpoint text not null, repo text not null, primary key (endpoint, repo));
create table if not exists endpoint_certs (
    sandbox text primary key, repo text not null default '', endpoints text not null, not_after real not null);
create table if not exists endpoint_credentials (sandbox text primary key, digest text not null, repo text not null);
""", sealed=(("endpoints", "ca_key"),))
