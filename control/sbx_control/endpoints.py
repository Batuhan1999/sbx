"""Endpoints: this host's private services (a managed server, an operator's
internal host) for sandboxes on a runtime that doesn't reach its private
network (capabilities.private_networks false: a provider's, such as E2B
Cloud), over one TLS path shared by every service and protocol.

An endpoint is {name, target host:port}: a managed server's (`--server`) or
an internal host's within network.internal_cidrs. Off until an admin adds
one; with none, every name goes to the HTTPS server as before.

Access is per repo and explicit, whatever the repo's trust: a managed
server's endpoint is for the repos whose attachments bind to that server,
an internal host's for the repos an admin granted it (endpoint_repos). It is
enforced by certificates: every endpoint has a CA of its own, nginx verifies
a client against that endpoint's CA only, and a sandbox gets certificates
only for the endpoints its repo may use.

- The host: no port of its own. nginx's stream module owns 443 and reads
  the TLS server name without decrypting (ssl_preread): an endpoint's
  <name>.<domain> goes to an internal listener where a server per endpoint
  (server_name) terminates TLS with the installation's wildcard
  certificate, requires a client certificate from that endpoint's own CA
  (ssl_verify_client on) and proxies to its target; every other name goes
  to the HTTPS server, which moved to an internal listener and takes the
  client's address from the PROXY protocol. Written by the root side (the
  endpoints effect, apply_host; tls.sh too), never by sbx-control.
- Certificates: an endpoint's CA is made with it, its key sealed in the
  secret store (sbx-control issues the sandboxes' certificates, on demand).
  A sandbox's certificate for an endpoint lasts CERT_HOURS. It is issued when
  its runtime is written (create, a cold boot, env deliveries; the keeper
  renews running ones through a delivery), pushed when sbx resumes it
  (push), and pulled by the forwarder itself when one has less than an hour
  left (POST /api/sandbox/endpoint-certs with the sandbox's credential:
  issue_for). Nothing is revoked: a certificate runs out, and a revoked
  grant or a deleted sandbox gets no new one.
- The sandbox: the guest helper's forwarder (sbx_guest.py forward) listens
  on 127.0.0.1:<local port> for each endpoint the sandbox may use and wraps
  each connection into TLS to <name>.<domain>:PORT with that endpoint's
  certificate. Apps connect in plain text; the service's own auth (a
  per-sandbox login) stays on top.
- The values: where a sandbox's env or its services' outputs name an
  endpoint's target (host:port in a URL or an address, or host and port
  outputs), a provider runtime's sandbox gets the forwarder's address instead
  (Routes); an internal host's endpoint is also SBX_ENDPOINT_<NAME>. Embed's
  sandboxes keep the private addresses.

A repo whose sandboxes would need a private target it may not use is
refused on such a runtime, naming the command that fixes it (missing)."""
from __future__ import annotations

import contextlib
import dataclasses
import datetime
import fcntl
import ipaddress
import json
import re
import sqlite3
import subprocess
import sys
import time

from . import contributions, paths, runtime, secret_store, settings, state

PORT = 443                              # where sandboxes reach endpoints: the HTTPS port, shared by SNI
ENDPOINTS_BACKEND = "127.0.0.1:8441"    # nginx's TLS servers for the endpoints, behind the stream front on PORT
HTTPS_BACKEND = "127.0.0.1:8442"        # nginx's HTTPS server (tls.sh: HTTPS_LISTEN), behind the same front
LOCAL_PORTS = range(17400, 17800)      # the forwarder's listeners in a sandbox, one per endpoint
CERT_HOURS = 24
RENEW_HOURS = 12                        # the keeper renews a running sandbox's certificates with less than this left
PICKUP_SECONDS = 120                    # a request the root side has not taken this long has failed
WORKER_SHUTDOWN = "30s"                 # nginx: how long a reload's old workers keep their connections
ENDPOINT_IDLE = "1h"                    # nginx: an endpoint connection with no traffic this long is closed
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
REVISION = state.Setting("endpoints_revision")
FOLDER = "endpoints"                    # its contribution's folder in the sandbox (contributions.folder)
RENEWED = "endpoint-certs"              # in the sandbox's RUN: certificates pushed or pulled since the last write
BUNDLE = "{}.bundle.pem"                # there: an endpoint's key and certificate (sbx_guest.BUNDLE)
BUNDLE_INDEX = "bundles.json"           # there: {name: not_after} (sbx_guest.BUNDLE_INDEX)
STREAM_FILE = "endpoints.stream"        # in tls.sh's nginx folder, included in nginx's stream block
CA_FILES = "endpoint-{}-ca.pem"         # each endpoint's CA certificate, beside it
NGINX = "/usr/local/sbin/sbx-tls-nginx"  # tls.sh's: start nginx once, test and reload its configuration
CERTS_PATH = "/api/sandbox/endpoint-certs"
# host:port with an IPv4 address or a bracketed IPv6 one: what the values are scanned for.
HOSTPORT = re.compile(r"(?<![\w.\-])(\d{1,3}(?:\.\d{1,3}){3}|\[[0-9A-Fa-f:.]+\]):(\d{1,5})(?!\d)")
LOCAL = "127.0.0.1"


class Refused(ValueError):
    """The request can't be done as asked (the API answers 400)."""


class Conflict(Refused):
    """Refused for what the installation holds now: the name or target is
    taken, no domain, sandboxes use it (the API answers 409)."""


# The records


def _rows() -> list[dict]:
    try:
        with state.db() as conn:
            rows = conn.execute("select name, kind, server, target, local_port, revision, ca_cert from endpoints "
                                "order by name").fetchall()
            grants = conn.execute("select endpoint, repo from endpoint_repos order by repo").fetchall()
    except sqlite3.OperationalError:   # before the first migrate
        return []
    granted: dict = {}
    for endpoint, repo in grants:
        granted.setdefault(endpoint, []).append(repo)
    return [{"name": n, "kind": k, "server": s, "target": t, "local_port": p, "revision": r, "ca_cert": c,
             "grants": granted.get(n, [])} for n, k, s, t, p, r, c in rows]


def exist() -> bool:
    return bool(_rows())


def domain() -> str:
    return settings.current().domain.name


def address(name: str) -> str:
    return f"{name}.{domain()}"


@contextlib.contextmanager
def _lock():
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


def _join(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def normalized(target: str) -> str:
    return _join(*split_target(target))


def _label(server: str) -> str:
    return server.lower().replace("_", "-")


def env_name(name: str) -> str:
    return "SBX_ENDPOINT_" + name.upper().replace("-", "_")


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
    for slug in _slugs():
        if name == slug or name.startswith(slug + "-"):
            raise Conflict(f"{name} is a preview host of the sandbox {slug} ({slug}, {slug}-<port>, "
                           f"{slug}-<preview>); choose another name")


def _slugs() -> list[str]:
    try:
        with state.db() as conn:
            return [r[0] for r in conn.execute("select slug from sandboxes where slug is not null and slug != ''")]
    except sqlite3.OperationalError:
        return []


def slug_taken(slug: str) -> str:
    """Which endpoint a sandbox slug's hosts would collide with ("" for none):
    the slug itself, or <slug>-<anything>."""
    for row in _rows():
        if row["name"] == slug or row["name"].startswith(slug + "-"):
            return row["name"]
    return ""


def _internal(host: str) -> bool:
    ip = ipaddress.ip_address(host)
    return any(ip in ipaddress.ip_network(cidr, strict=False) for cidr in settings.current().network.internal_cidrs)


def unroutable(row: dict) -> str:
    """Why an endpoint is not routed now ("" when it is): an internal host's
    target that network.internal_cidrs no longer covers."""
    if row["kind"] == "internal" and not _internal(split_target(row["target"])[0]):
        return (f"{row['target']} is outside network.internal_cidrs now, so nothing reaches it: add its range "
                "again (sbx-admin config set network) or remove the endpoint")
    return ""


def _repos(repos) -> list[str]:
    out = []
    for repo in repos or []:
        repo = str(repo).strip().lower()
        if not settings.REPO_NAME.fullmatch(repo):
            raise Refused(f"{repo!r} is not a repository (owner/name)")
        if repo not in out:
            out.append(repo)
    return sorted(out)


def add(*, server: str | None = None, name: str | None = None, target: str | None = None, repos=None,
        by: str) -> dict:
    """A managed server's endpoint (server, optionally named; for the repos
    attached to it) or an internal host's (name and target within
    network.internal_cidrs; for the repos given). Recorded at once with a CA
    of its own; the host applies it (state applying, then on or failed)."""
    from . import hosted_services
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
        kind, name, target = "managed-server", name or _label(server), normalized(found)
    else:
        if not name or not target:
            raise Refused("give a managed server (server), or a name and a target HOST:PORT")
        kind, target, server = "internal", normalized(target), ""
        if not _internal(split_target(target)[0]):
            cidrs = ", ".join(settings.current().network.internal_cidrs) or "none set"
            raise Refused(f"{target} is outside network.internal_cidrs ({cidrs}); an endpoint reaches only "
                          "those ranges, or a managed server (--server)")
    _check_name(name)
    grants = _repos(repos)
    with _lock():
        rows = _rows()
        if any(r["name"] == name for r in rows):
            raise Conflict(f"there is an endpoint {name} already")
        if server and (same := next((r for r in rows if r["server"] == server), None)):
            raise Conflict(f"the managed server {server} has an endpoint already: {same['name']}")
        if same := next((r for r in rows if r["target"] == target), None):
            raise Conflict(f"{target} has an endpoint already: {same['name']}")
        used = {r["local_port"] for r in rows}
        port = next((p for p in LOCAL_PORTS if p not in used), None)
        if port is None:
            raise Conflict(f"this installation has {len(LOCAL_PORTS)} endpoints, the most it keeps")
        ca_cert, ca_key = _make_ca(name)
        reference = secret_store.seal({"key": ca_key}, key=f"endpoints/{name}/ca")
        with state.db() as conn:
            conn.execute("begin immediate")
            revision = int(REVISION.get("0", conn=conn)) + 1
            conn.execute("insert into endpoints (name, kind, server, target, local_port, revision, created_by, "
                         "ca_cert, ca_key) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (name, kind, server or "", target, port, revision, by, ca_cert, reference))
            conn.executemany("insert into endpoint_repos (endpoint, repo) values (?, ?)", [(name, r) for r in grants])
            REVISION.set(revision, conn=conn)
            conn.execute("commit")
    settings.request_effects(settings.HostUnit.ENDPOINTS)
    added = one(name)
    deliver(name, added["repos"])
    return added


def deliver(name: str, repos):
    """Bring the repos' sandboxes an endpoint change without restarting their
    apps: their runtime written again (sandboxes.deliver_env), so the
    forwarder's config and certificates and SBX_ENDPOINT_<NAME> in the env
    are what the repo may use now. Running ones get it within seconds,
    paused ones when they resume; running processes keep their env until
    they restart."""
    repos = sorted(set(repos))
    if repos:   # listing the sandboxes asks the runtimes: not in the admin's request
        _background(lambda: _deliver(name, repos))


def _background(work):
    import threading
    threading.Thread(target=work, name="endpoint-delivery", daemon=True).start()


def _deliver(name: str, repos: list):
    from . import sandboxes
    for repo in repos:
        try:
            sandboxes.deliver_env(repo, [env_name(name)], restart=False)
        except Exception as error:   # the change is recorded; the next runtime write brings it
            print(f"endpoints: delivering {name} to {repo}'s sandboxes: {error!r}", file=sys.stderr, flush=True)


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
    """Grants changed under the lock, read and written in one transaction. A
    revoke rotates the endpoint's CA, so what the revoked repo's sandboxes
    hold (and any copy of it) no longer passes once nginx has the new one;
    the sandboxes still granted get certificates from the new CA at once."""
    with _lock():
        # A CA in case this revokes: sealed before the transaction (the store may write to sbx.db too).
        ca_cert, ca_key = _make_ca(name)
        reference = secret_store.seal({"key": ca_key}, key=f"endpoints/{name}/ca/{time.time_ns()}")
        discard = reference
        with state.db() as conn:
            conn.execute("begin immediate")
            found = conn.execute("select kind from endpoints where name = ?", (name,)).fetchone()
            if not found:
                conn.execute("rollback")
                secret_store.discard(reference)
                raise LookupError(f"no endpoint {name}")
            if found[0] != "internal":
                conn.execute("rollback")
                secret_store.discard(reference)
                raise Refused(f"{name} is a managed server's endpoint, for the repos attached to it: attach the "
                              "server to the repo instead")
            old = [r[0] for r in conn.execute("select repo from endpoint_repos where endpoint = ? order by repo",
                                              (name,))]
            try:
                new = change(old)
            except BaseException:
                conn.execute("rollback")
                secret_store.discard(reference)
                raise
            conn.execute("delete from endpoint_repos where endpoint = ?", (name,))
            conn.executemany("insert into endpoint_repos (endpoint, repo) values (?, ?)", [(name, r) for r in new])
            revoked = set(old) - set(new)
            if revoked:
                discard = _rotate_ca(conn, name, ca_cert, reference)
            conn.execute("commit")
    secret_store.discard(discard)   # the old CA's key after a revoke, else the one not needed
    if revoked:
        settings.request_effects(settings.HostUnit.ENDPOINTS)
        deliver(name, set(old) | set(new))   # new certificates for all, none for the revoked
    else:
        deliver(name, set(new) - set(old))
    return one(name)


def _rotate_ca(conn, name: str, ca_cert: str, reference: str) -> str:
    """The endpoint's new CA, in the transaction; the old key's reference, to discard after it."""
    old = conn.execute("select ca_key from endpoints where name = ?", (name,)).fetchone()[0]
    revision = int(REVISION.get("0", conn=conn)) + 1
    conn.execute("update endpoints set ca_cert = ?, ca_key = ?, revision = ? where name = ?",
                 (ca_cert, reference, revision, name))
    REVISION.set(revision, conn=conn)
    return old


def remove(name: str) -> dict:
    """Refused (Conflict) while sandboxes on a provider runtime use it."""
    with _lock():
        if not any(r["name"] == name for r in _rows()):
            raise LookupError(f"no endpoint {name}")
        using = used_by().get(name)
        if using:
            raise Conflict(f"sandboxes of {', '.join(using)} use the endpoint {name}; delete them first "
                           "(sbx ls, sbx down NAME)")
        _delete(name)
    return {"name": name, "deleted": True}


def _delete(name: str):
    row = next((r for r in _rows() if r["name"] == name), None)
    with state.db() as conn:
        conn.execute("begin immediate")
        conn.execute("delete from endpoint_repos where endpoint = ?", (name,))
        secret_store.delete_rows(conn, "endpoints", "name = ?", (name,))
        REVISION.set(int(REVISION.get("0", conn=conn)) + 1, conn=conn)
        conn.execute("commit")
    settings.request_effects(settings.HostUnit.ENDPOINTS)
    if row:
        deliver(name, repos_of(row))


def forget_server(server: str):
    """A managed server deleted with its data: its endpoint goes too."""
    for row in _rows():
        if row["server"] == server:
            _delete(row["name"])


# Who may use one


def _attached(repo: str) -> list[str]:
    from . import hosted_services
    try:
        return hosted_services.attached_to(repo)
    except Exception:   # a store that does not answer: none until it does
        return []


def _bound_repos(server: str) -> list[str]:
    """The repos whose attachments bind to the managed server."""
    from .catalog import attachments
    try:
        return sorted(r for r in attachments.repos() if server in _attached(r))
    except Exception:
        return []


def repos_of(row: dict) -> list[str]:
    return _bound_repos(row["server"]) if row["kind"] == "managed-server" else row["grants"]


def permitted(repo: str, rows: list | None = None) -> list[dict]:
    """The endpoints the repo's sandboxes may use: its attached managed
    servers', and the internal hosts' granted to it."""
    if not repo:
        return []
    repo = repo.lower()
    rows = _rows() if rows is None else rows
    servers = set(_attached(repo)) if any(r["kind"] == "managed-server" for r in rows) else set()
    return [r for r in rows if not unroutable(r)
            and (r["server"] in servers if r["kind"] == "managed-server" else repo in r["grants"])]


# What the API says


def _applied_file():
    return paths.state_dir() / "endpoints-applied.json"


def applied() -> dict:
    """The root side's last apply: {revision, ok, message, ok_revision, listening, at}."""
    try:
        return json.loads(_applied_file().read_text())
    except (OSError, ValueError):
        return {}


def _request_file():
    return paths.run() / "apply" / settings.HostUnit.ENDPOINTS


def _not_picked_up() -> str:
    """Why the request failed when the root side has not taken it for PICKUP_SECONDS; "" otherwise."""
    try:
        age = time.time() - _request_file().stat().st_mtime
    except OSError:
        return ""
    return ("the server did not pick up the change: check sbx-apply-endpoints.path "
            "(systemctl status sbx-apply-endpoints.path)") if age > PICKUP_SECONDS else ""


def _state(revision: int, record: dict) -> tuple[str, str]:
    if record.get("ok_revision", 0) >= revision:
        return "on", ""
    if record.get("revision", 0) >= revision and not record.get("ok"):
        return "failed", record.get("message") or "the server could not apply it"
    stuck = _not_picked_up()
    return ("failed", stuck) if stuck else ("applying", "")


def _payload(row: dict, record: dict, using: dict, kinds: dict) -> dict:
    state_now, message = _state(row["revision"], record)
    if why := unroutable(row):
        state_now, message = "failed", why
    return {"name": row["name"], "address": address(row["name"]), "kind": row["kind"], "server": row["server"],
            "server_kind": kinds.get(row["server"], "") if row["server"] else "",
            "target": row["target"], "repos": repos_of(row), "state": state_now, "message": message,
            "used_by": using.get(row["name"], [])}


def _server_kinds() -> dict:
    from . import hosted_services
    try:
        return {s["name"]: s["kind"] for s in hosted_services.records()}
    except sqlite3.OperationalError:
        return {}


def one(name: str) -> dict:
    row = next((r for r in _rows() if r["name"] == name), None)
    if row is None:
        raise LookupError(f"no endpoint {name}")
    return _payload(row, applied(), used_by(), _server_kinds())


def describe() -> dict:
    """GET /api/endpoints: {endpoints, candidates, domain, needed, applying, failure}. Each endpoint's
    repos: who may use it (an internal host's grants; the repos attached to a managed server); used_by:
    the repos whose sandboxes DELETE is refused for. candidates: the managed servers without an endpoint,
    with the address one would get; needed: an enabled runtime's sandboxes don't reach this host's
    private network; applying: a change (an added or removed endpoint) is not applied yet; failure: why
    the last change failed ("" when it did not)."""
    from . import hosted_services
    rows, record, using, kinds = _rows(), applied(), used_by(), _server_kinds()
    exposed = {r["server"] for r in rows if r["server"]}
    candidates = []
    for server in hosted_services.records():
        if server["name"] not in exposed:
            candidates.append({"server": server["name"], "kind": server["kind"],
                               "address": address(_label(server["name"])) if domain() else "",
                               "target": hosted_services.target(server["name"]) or ""})
    overall, message = _state(int(REVISION.get("0")), record)
    return {"endpoints": [_payload(r, record, using, kinds) for r in rows], "candidates": candidates, "domain": domain(),
            "needed": needed(), "applying": overall == "applying", "failure": message if overall == "failed" else ""}


def needed() -> bool:
    try:
        return any(not runtime.capabilities(kind).private_networks for kind in runtime.enabled())
    except Exception:
        return False


def used_by() -> dict:
    """endpoint name -> the repos whose registered sandboxes hold a certificate for it."""
    out: dict = {}
    try:
        with state.db() as conn:
            rows = conn.execute("select c.repo, c.endpoints from endpoint_certs c "
                                "join sandboxes s on s.name = c.sandbox").fetchall()
    except sqlite3.OperationalError:
        return out
    for repo, names in rows:
        for name in json.loads(names or "[]"):
            found = out.setdefault(name, [])
            if repo and repo not in found:
                found.append(repo)
    return {k: sorted(v) for k, v in out.items()}


# The gate: what a repo's sandboxes on a provider runtime need


def _addresses(text) -> list[tuple[str, int]]:
    found = []
    for match in HOSTPORT.finditer(str(text)):
        try:
            found.append(split_target(f"{match.group(1)}:{match.group(2)}"))
        except Refused:
            continue
    return found


def needs(repo: str) -> list[dict]:
    """The private targets the repo's sandboxes reach: the managed servers its
    attachments bind to, then the internal hosts (within
    network.internal_cidrs) its bound connections' values and its sbx.yaml
    env name. Each {server, connection, target}."""
    from . import hosted_services, recipes
    from .catalog import attachments
    out, seen = [], set()
    servers = _attached(repo)
    for server in servers:
        target = hosted_services.target(server)
        target = normalized(target) if target else ""
        seen.add(target)
        out.append({"server": server, "connection": server, "target": target, "source": "server"})
    if not settings.current().network.internal_cidrs:
        return out
    sources = []
    try:
        for att in attachments.effective(repo):
            for key in att["connections"].values():
                if key in servers:
                    continue
                found = attachments.connection(key)
                if found:
                    sources += [(key, "connection", value) for value in found["inputs"].values()]
    except Exception:   # a connection or the store that does not answer is the create's to report
        pass
    try:
        sources += [("sbx.yaml", "env", value) for value in (recipes.load(repo).spec.get("env") or {}).values()]
    except Exception:
        pass
    for key, source, value in sources:
        for host, port in _addresses(value):
            target = _join(host, port)
            if target not in seen and _internal(host):
                seen.add(target)
                out.append({"server": "", "connection": key, "target": target, "source": source})
    return out


def missing(repo: str, sources=("server", "connection")) -> list[tuple[str, str]]:
    """(what, the command that lets the repo's sandboxes reach it) for each
    private target they need and may not use: no endpoint yet, or an internal
    host's endpoint not granted to the repo. The create gate refuses for the
    managed servers and connections the repo's attachments bind (the
    default); an address in sbx.yaml's env is advice (sources=("env",))."""
    rows = [r for r in _rows() if not unroutable(r)]
    by_target = {r["target"]: r for r in rows}
    servers = {r["server"] for r in rows if r["server"]}
    out = []
    for need in needs(repo):
        if need["source"] not in sources:
            continue
        if need["server"]:
            if need["server"] not in servers:
                out.append((f"the managed server {need['server']}", f"sbx endpoints add --server {need['server']}"))
            continue
        row = by_target.get(need["target"])
        what = (f"the internal host {need['target']} ({need['connection']})" if need["source"] == "connection" else
                f"the internal host {need['target']} named in sbx.yaml's env")
        if row is None:
            name = _label(need["connection"]) if need["connection"] != "sbx.yaml" else "NAME"
            out.append((what, f"sbx endpoints add --name {name} --target {need['target']} --repo {repo}"))
        elif row["kind"] == "internal" and repo.lower() not in row["grants"]:
            out.append((what, f"sbx endpoints grant {row['name']} {repo}"))
    return out


# The sandbox's side


@dataclasses.dataclass
class Routes:
    """For one sandbox: its values with every permitted endpoint's target
    replaced by the forwarder's address (text, outputs, env), and the
    forwarder's files with a certificate per permitted endpoint
    (contribution). Empty for a sandbox that reaches the private network
    itself, or whose repo may use none."""
    rows: list = dataclasses.field(default_factory=list)          # the permitted endpoints

    def placed(self, taken) -> "Routes":
        """The same endpoints on local ports nothing else in the sandbox
        listens on: each its own port, else the next free one."""
        used, rows = set(taken), []
        for row in self.rows:
            port = row["local_port"]
            while port in used and port < 65535:
                port += 1
            used.add(port)
            rows.append({**row, "local_port": port})
        return Routes(rows)

    @property
    def by_target(self) -> dict:
        return {split_target(r["target"]): r for r in self.rows}

    def text(self, value):
        if not self.rows or not isinstance(value, str):
            return value
        targets = self.by_target

        def replace(match):
            try:
                found = targets.get(split_target(f"{match.group(1)}:{match.group(2)}"))
            except Refused:
                found = None
            return f"{LOCAL}:{found['local_port']}" if found else match.group(0)
        return HOSTPORT.sub(replace, value)

    def outputs(self, created: dict) -> dict:
        """A sandbox's services (key -> resources.Created) with their outputs
        localized: host:port in any value, and a host and port output pair."""
        if not self.rows:
            return created
        targets, out = self.by_target, {}
        for key, item in created.items():
            values = {k: self.text(v) for k, v in item.outputs.items()}
            host, port = values.get("host"), values.get("port")
            if isinstance(host, str) and str(port or "").isdecimal():
                try:
                    found = targets.get(split_target(_join(host.strip("[]"), int(port))))
                except Refused:
                    found = None
                if found:
                    values.update(host=LOCAL, port=str(found["local_port"]))
            out[key] = dataclasses.replace(item, outputs=values)
        return out

    def env(self, envs: dict) -> dict:
        """The env localized, with SBX_ENDPOINT_<NAME> for each internal host's endpoint."""
        if not self.rows:
            return envs
        out = {k: self.text(v) for k, v in envs.items()}
        out.update({env_name(r["name"]): f"{LOCAL}:{r['local_port']}" for r in self.rows if r["kind"] == "internal"})
        return out

    def contribution(self, name: str, repo: str):
        """The forwarder's files with fresh certificates; None (and none on
        record) when the sandbox may use no endpoint."""
        if not self.rows:
            _record_certificate(name, repo, [], 0)
            return None
        issued = issue_all(name, repo, self.rows)
        files, endpoints = {}, []
        for row in self.rows:
            cert, key, not_after = issued[row["name"]]
            files[f"{row['name']}.pem"] = contributions.File(cert)
            files[f"{row['name']}-key.pem"] = contributions.File(key)
            endpoints.append({"name": row["name"], "address": address(row["name"]), "port": PORT,
                              "local_port": row["local_port"], "cert": f"{row['name']}.pem",
                              "key": f"{row['name']}-key.pem", "not_after": not_after})
        config = {"endpoints": endpoints, "renew": renew_config(name, repo)}
        trust = _self_signed()
        if trust:
            config["trust"] = "server.pem"
            files["server.pem"] = contributions.File(trust)
        files["config.json"] = contributions.File(json.dumps(config, indent=1))
        return contributions.Contribution(FOLDER, runtime=contributions.RuntimeLayer(files=files))


def routes(kind: str | None, repo: str | None) -> Routes:
    """The sandbox's routes on runtime kind: the endpoints its repo may use,
    on a runtime without private_networks, with a domain; else empty."""
    if not repo or not domain():
        return Routes()
    try:
        if runtime.capabilities(kind or None).private_networks:
            return Routes()
    except Exception:
        return Routes()
    return Routes(permitted(repo))


def renew_config(name: str, repo: str) -> dict:
    """How the forwarder pulls new certificates: sbx-control's URL and a
    credential of this sandbox's own, random, made with each runtime write
    (so a sandbox made again under the name, or for another repo, has
    another; only its digest is kept, with the repo)."""
    import hashlib
    import secrets
    credential = secrets.token_urlsafe(32)
    with state.db() as conn:
        conn.execute("insert into endpoint_credentials (sandbox, digest, repo) values (?, ?, ?) on conflict (sandbox) "
                     "do update set digest = excluded.digest, repo = excluded.repo",
                     (name, hashlib.sha256(credential.encode()).hexdigest(), (repo or "").lower()))
    return {"url": f"{settings.current().base_url}{CERTS_PATH}", "sandbox": name, "credential": credential}


def sandbox_of(authorization: str) -> str | None:
    """The sandbox a callback's `Bearer NAME:CREDENTIAL` proves (its latest credential), or None."""
    import hashlib
    import hmac
    name, _, credential = authorization.removeprefix("Bearer ").partition(":")
    if not name or not credential:
        return None
    try:
        with state.db() as conn:
            row = conn.execute("select digest from endpoint_credentials where sandbox = ?", (name,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not hmac.compare_digest(row[0], hashlib.sha256(credential.encode()).hexdigest()):
        return None
    return name


def _self_signed() -> str:
    """The installation's certificate when it is self-signed (--dns none), as the forwarder's trust anchor."""
    from . import templates
    if not templates.pinned_key():
        return ""
    return (paths.tls() / "letsencrypt/live" / domain() / "fullchain.pem").read_text()


def _record_certificate(name: str, repo: str, names: list, not_after: float):
    try:
        with state.db() as conn:
            if not names:
                conn.execute("delete from endpoint_certs where sandbox = ?", (name,))
                return
            conn.execute("insert into endpoint_certs (sandbox, repo, endpoints, not_after) values (?, ?, ?, ?) "
                         "on conflict (sandbox) do update set repo = excluded.repo, endpoints = excluded.endpoints, "
                         "not_after = excluded.not_after", (name, (repo or "").lower(), json.dumps(names), not_after))
    except sqlite3.OperationalError:
        pass


def holders() -> set:
    """The registered sandboxes that hold endpoint certificates (have a forwarder config)."""
    try:
        with state.db() as conn:
            return {r[0] for r in conn.execute("select c.sandbox from endpoint_certs c join sandboxes s "
                                               "on s.name = c.sandbox")}
    except sqlite3.OperationalError:
        return set()


def due(now: float | None = None) -> list[str]:
    """The registered sandboxes whose certificates run out within RENEW_HOURS: the keeper renews them."""
    limit = (now or time.time()) + RENEW_HOURS * 3600
    try:
        with state.db() as conn:
            return [r[0] for r in conn.execute("select c.sandbox from endpoint_certs c join sandboxes s "
                                               "on s.name = c.sandbox where c.not_after < ? order by c.sandbox",
                                               (limit,))]
    except sqlite3.OperationalError:
        return []


def issue_all(name: str, repo: str, rows: list) -> dict:
    """endpoint name -> (certificate, key, not_after) for each row, recorded for the sandbox."""
    issued = {row["name"]: issue(name, row) for row in rows}
    _record_certificate(name, repo, sorted(issued), min((v[2] for v in issued.values()), default=0))
    return issued


def issue_for(name: str) -> list[dict]:
    """POST /api/sandbox/endpoint-certs: new certificates for the sandbox's
    endpoints, those its repo may use now. LookupError for a sandbox that is
    gone; PermissionError when it may use none (a revoked grant, a runtime
    that reaches the private network itself)."""
    from . import core, sandboxes
    if not sandboxes.registered_owner(name):
        raise LookupError(f"no sandbox {name}")
    info = core.find(name)
    if not info:
        raise LookupError(f"no sandbox {name}")
    repo = core.app_of(info).repo
    with state.db() as conn:
        bound = conn.execute("select repo from endpoint_credentials where sandbox = ?", (name,)).fetchone()
    if not bound or bound[0] != (repo or "").lower():
        raise LookupError(f"{name} is another sandbox now")
    found = routes(runtime.kind_of(info), repo)
    if not found.rows:
        raise PermissionError(f"{name}'s repo may use no endpoint now")
    issued = issue_all(name, repo, found.rows)
    return [{"name": n, "cert": c, "key": k, "not_after": t} for n, (c, k, t) in sorted(issued.items())]


def push(sbx, info):
    """When sbx resumes a sandbox: fresh certificates into its RUN, where the
    forwarder takes them over older ones, so the first connection after a
    long pause need not renew. Nothing for one that may use no endpoint."""
    from . import core
    name = (info.metadata or {}).get("sbx")
    repo = core.app_of(info).repo
    found = routes(runtime.kind_of(info), repo)
    if not name or not found.rows:
        return False
    issued = issue_all(name, repo, found.rows)
    # The forwarder of this release first: one an upgrade left running is replaced before
    # anything of the new layout is written.
    core.ensure_forwarder(sbx)
    folder = f"{core.RUN}/{RENEWED}"
    staging = f"{folder}/.push"
    sbx.commands.run(f"install -d -m 700 {folder} && rm -rf {staging} && install -d -m 700 {staging}", timeout=30)
    for endpoint, (cert, key, _) in issued.items():
        sbx.files.write(f"{staging}/{BUNDLE.format(endpoint)}", key + cert, user="user")
    sbx.files.write(f"{staging}/{BUNDLE_INDEX}", json.dumps({n: v[2] for n, v in issued.items()}), user="user")
    # Each file replaced whole, the certificates before the times that point at them; then
    # what an earlier release's forwarder wrote here goes.
    names = " ".join(BUNDLE.format(e) for e in issued)
    sbx.commands.run(f"cd {staging} && chmod 600 * && for f in {names} {BUNDLE_INDEX}; do mv -f \"$f\" ../; done "
                     f"&& cd .. && rmdir {staging} && rm -f renewed.json *-key.pem "
                     f"&& find . -maxdepth 1 -name '*.pem' ! -name '*.bundle.pem' -delete", timeout=30)
    return True


# The CAs and the sandboxes' certificates


def _ca_of(row: dict) -> tuple[str, str]:
    with state.db() as conn:
        found = conn.execute("select ca_cert, ca_key from endpoints where name = ?", (row["name"],)).fetchone()
    if not found:
        raise LookupError(f"no endpoint {row['name']}")
    return found[0], secret_store.unseal(found[1])["key"]


def _make_ca(endpoint: str) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         f"sbx endpoint {endpoint} CA {state.installation_id()[:8]}")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


def issue(sandbox: str, row: dict, hours: float = CERT_HOURS) -> tuple[str, str, float]:
    """(a client certificate for the sandbox at the endpoint, its key, when it
    runs out as a timestamp): from that endpoint's CA, CERT_HOURS long, for
    client authentication only."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    ca_pem, ca_key_pem = _ca_of(row)
    authority = x509.load_pem_x509_certificate(ca_pem.encode())
    signer = serialization.load_pem_private_key(ca_key_pem.encode(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    until = now + datetime.timedelta(hours=hours)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"sbx sandbox {sandbox}")]))
            .issuer_name(authority.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(until)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
            .sign(signer, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode(), until.timestamp())


# The host's side (root): nginx's stream configuration


def stream_config(rows: list | None = None, name: str | None = None) -> str:
    """nginx's stream configuration: the front on 443, which reads the TLS
    server name without terminating (ssl_preread) and sends an endpoint's
    <name>.<domain> to ENDPOINTS_BACKEND and everything else to the HTTPS
    server on HTTPS_BACKEND, both with the client's address in the PROXY
    protocol; and, with a domain, a server per endpoint on ENDPOINTS_BACKEND,
    chosen by server_name, verifying clients against that endpoint's own CA."""
    rows = _rows() if rows is None else rows
    name = domain() if name is None else name
    rows = [r for r in rows if not unroutable(r)] if name else []
    routes_ = "".join(f"  {r['name']}.{name} {ENDPOINTS_BACKEND};\n" for r in rows)
    out = [f"""# sbx's front on 443 (endpoints.py): the TLS server name picks the backend, nothing is
# decrypted here. Endpoints go to their TLS servers below; everything else to the HTTPS
# server (tls.sh), which takes the client's address from the PROXY protocol.
map $ssl_preread_server_name $sbx_backend {{
  default {HTTPS_BACKEND};
{routes_}}}
server {{
  listen {PORT};
  ssl_preread on;
  proxy_pass $sbx_backend;
  proxy_protocol on;
  proxy_connect_timeout 10s;
  proxy_timeout 1d;
}}"""]
    if rows:
        out.append(f"""# Endpoints: TLS with the installation's certificate; a server per endpoint, chosen by
# SNI, verifies the client against that endpoint's own CA. Any other name is rejected.
server {{
  listen {ENDPOINTS_BACKEND} ssl proxy_protocol;
  ssl_reject_handshake on;
}}""")
    for row in rows:
        out.append(f"""server {{
  listen {ENDPOINTS_BACKEND} ssl proxy_protocol;
  server_name {row['name']}.{name};
  ssl_certificate /etc/letsencrypt/live/{name}/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/{name}/privkey.pem;
  ssl_protocols TLSv1.2 TLSv1.3;
  ssl_client_certificate /etc/nginx/conf.d/{CA_FILES.format(row['name'])};
  ssl_verify_client on;
  ssl_verify_depth 1;
  ssl_handshake_timeout 10s;
  proxy_pass {row['target']};
  proxy_connect_timeout 10s;
  proxy_timeout {ENDPOINT_IDLE};
}}""")
    return "\n".join(out) + "\n"


MAIN_CONF = f"""# nginx's main configuration (tls.sh, endpoints.py): the image's own, the client's
# address from the PROXY protocol (the stream front on 443 passes it to the HTTPS
# server on {HTTPS_BACKEND}), and the stream block (conf.d/*.stream).
user nginx;
worker_processes auto;
# A reload's old workers end their connections after this: a revoked or removed
# endpoint's sessions with them (endpoints.py), and long previews (they reconnect).
worker_shutdown_timeout {WORKER_SHUTDOWN};
error_log /var/log/nginx/error.log notice;
pid /run/nginx.pid;
events {{
  worker_connections 1024;
}}
http {{
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  set_real_ip_from 127.0.0.1;
  real_ip_header proxy_protocol;
  log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                  '$status $body_bytes_sent "$http_referer" "$http_user_agent" "$http_x_forwarded_for"';
  access_log /var/log/nginx/access.log main;
  sendfile on;
  keepalive_timeout 65;
  include /etc/nginx/conf.d/*.conf;
}}
stream {{
  include /etc/nginx/conf.d/*.stream;
}}
"""


def write_nginx(folder):
    """The stream configuration and each endpoint's CA certificate in nginx's folder."""
    rows = [r for r in _rows() if not unroutable(r)] if domain() else []
    keep = {CA_FILES.format(r["name"]) for r in rows}
    for old in folder.glob(CA_FILES.format("*")):
        if old.name not in keep:
            old.unlink()
    for row in rows:
        (folder / CA_FILES.format(row["name"])).write_text(row["ca_cert"])
    (folder / STREAM_FILE).write_text(stream_config())


def apply_host() -> int:
    """sbx-apply endpoints, as root: nginx's stream configuration from the
    records now, nginx tested and reloaded (the configuration before kept
    when it fails). Its outcome in endpoints-applied.json, which describe()
    reads; nginx's own words go to the journal only."""
    folder = paths.tls() / "nginx"
    revision = int(REVISION.get("0"))
    record = applied()
    names = [STREAM_FILE, *(p.name for p in folder.glob(CA_FILES.format("*")))] if folder.is_dir() else []
    before = {f: (folder / f).read_text() if (folder / f).exists() else None for f in names}
    try:
        if not (folder.is_dir() and (paths.tls() / "nginx.conf").exists()):
            raise RuntimeError("nginx is not set up on this server yet (connect a domain first)")
        write_nginx(folder)
        result = subprocess.run([NGINX], capture_output=True, text=True, timeout=300)
        if result.returncode:
            print(result.stdout + result.stderr, file=sys.stderr, flush=True)
            for path in folder.glob(CA_FILES.format("*")):
                if path.name not in before:
                    path.unlink()
            for name, text in before.items():
                if text is None:
                    (folder / name).unlink(missing_ok=True)
                else:
                    (folder / name).write_text(text)
            subprocess.run([NGINX], capture_output=True, timeout=300)
            raise RuntimeError("nginx did not take the endpoints' configuration; the one before stays "
                               "(journalctl -u sbx-apply-endpoints says why)")
        record = {**record, "revision": revision, "ok": True, "message": "", "ok_revision": revision}
        code = 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        record = {**record, "revision": revision, "ok": False, "message": str(error)}
        print(f"endpoints: {error}", file=sys.stderr, flush=True)
        code = 1
    record["at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    _applied_file().write_text(json.dumps(record))
    _applied_file().chmod(0o644)
    return code


def doctor_lines() -> list[str]:
    """sbx-admin doctor: the exposed endpoints, one line each."""
    return [f"  {e['state']:<9} {e['address']} -> {e['target']} ({e['server'] or 'internal'}; "
            f"for {', '.join(e['repos']) or 'no repo yet'})" + (f": {e['message']}" if e["message"] else "")
            for e in describe()["endpoints"]]


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


if __name__ == "__main__":
    if sys.argv[1:2] == ["nginx"] and len(sys.argv) == 3:
        from pathlib import Path
        write_nginx(Path(sys.argv[2]))
    elif sys.argv[1:] == ["main-conf"]:
        print(MAIN_CONF, end="")
    elif sys.argv[1:] == ["https-listen"]:
        print(f"{HTTPS_BACKEND} ssl proxy_protocol")
    else:
        raise SystemExit("usage: python -m sbx_control.endpoints nginx FOLDER | main-conf | https-listen")
