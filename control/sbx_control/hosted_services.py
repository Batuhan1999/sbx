"""Managed servers: shared development databases (Postgres, ClickHouse) the
installation runs on its own Docker host, on a private network sandboxes
reach. `sbx-admin server up` is one way to make a connection: it starts the
container and saves a connection under the same name (catalog.connections),
so repos bind to it like to any other. This module keeps what only a managed
server has: its container, address, admin password and storage.

Its network is not an operator's setting: sandboxes of trusted repos reach
it beside network.internal_cidrs while a managed server exists
(allowed_cidrs), so no settings write drops it."""
from __future__ import annotations

import contextlib
import fcntl
import ipaddress
import json
import os
import secrets
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import cow_storage, hosted_compose, paths, postgres_cow, secret_store, settings, state
from .catalog import NAME_RE, PackageError, attachments, connections

NETWORK = "sbx-services"
CIDR = "172.29.50.0/24"
LABEL = "dev-sandboxes.managed"   # on the containers, volumes and network it owns
ADMIN = "sbx_admin"
POSTGRES_MAJOR = 18
# Docker's json-file driver keeps a container's log forever by default. A
# server that fails on every write (ClickHouse on a full disk above all)
# writes error output faster than anything frees space, and the disk stays
# full until someone steps in. Five files of 50 MiB, as Embed's services have.
LOG_LIMIT = ("--log-driver", "json-file", "--log-opt", "max-size=50m", "--log-opt", "max-file=5")
DRIVERS = {
    "postgres": {"image": "pgvector/pgvector:pg18", "port": 5432, "path": "/var/lib/postgresql",
                 "package": "postgres-server"},
    "clickhouse": {"image": "clickhouse/clickhouse-server:26.2", "port": 8123,
                   "path": "/var/lib/clickhouse", "package": "clickhouse-server"},
}
BUILT_IN = {driver["package"]: kind for kind, driver in DRIVERS.items()}   # connection package -> its kind
PACKAGE = "package"   # the kind of a server a connection package declares (hosted_compose.py)


@dataclass(frozen=True)
class Server:
    """One managed server's names on the host, in one place."""
    name: str
    kind: str

    @property
    def container(self) -> str:
        return f"sbx-server-{self.name}"

    @property
    def volume(self) -> str:
        return f"{self.container}-data"

    @property
    def unit(self) -> str:
        return f"sbx-postgres-{self.name}.service"

    @property
    def admin(self) -> str:
        return ADMIN

    @property
    def image(self) -> str:
        return DRIVERS[self.kind]["image"]

    @property
    def data_path(self) -> str:
        return DRIVERS[self.kind]["path"]

    def owns(self, labels) -> bool:
        return (labels or {}).get(LABEL) == self.name


def docker(*args, input=None, check=True):
    result = subprocess.run(["docker", *args], input=input, capture_output=True, text=True, timeout=600)
    if check and result.returncode:
        # Docker may echo configuration in an error; keep credential-bearing input private.
        raise RuntimeError(f"docker {args[0]} failed (exit {result.returncode})")
    return result


@contextlib.contextmanager
def _lock():
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    with (paths.state_dir() / "managed-servers.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        yield


def _record(name: str):
    """(server, its sealed values, status), or None."""
    with state.db() as conn:
        row = conn.execute("select kind, config, status from managed_servers where name = ?", (name,)).fetchone()
    return (Server(name, row[0]), secret_store.unseal(row[1]), row[2]) if row else None


def _save(server: Server, values: dict, status: str | None = None):
    reference = secret_store.seal(values, key=f"managed-server/{server.name}")
    with state.db() as conn:
        old = conn.execute("select config from managed_servers where name = ?", (server.name,)).fetchone()
        conn.execute("insert into managed_servers (name, kind, config, status) values (?, ?, ?, coalesce(?, 'preparing')) "
                     "on conflict (name) do update set config = excluded.config, "
                     "status = coalesce(?, managed_servers.status)", (server.name, server.kind, reference, status, status))
    if old and old[0] != reference:
        secret_store.discard(old[0])


def allowed_cidrs() -> list[str]:
    """What every sandbox of a trusted repo may reach: the operator's
    network.internal_cidrs and the managed servers' networks, computed,
    never stored as one list, each written by its own owner. The runtime
    enforces it (runtime.apply_network)."""
    return list(dict.fromkeys([*settings.current().network.internal_cidrs, *networks()]))


def attached_to(repo: str) -> list[str]:
    """The managed servers the repo's attachments bind to (by choice or as
    the only connection of their kind): its sandboxes must reach this host's
    private network (runtimes.choose)."""
    try:
        with state.db() as conn:
            names = {row[0] for row in conn.execute("select name from managed_servers")}
    except sqlite3.OperationalError:
        return []
    if not names:
        return []
    bound = {key for att in attachments.effective(repo) if not att.get("disabled")
             for key in att["connections"].values()}
    return sorted(bound & names)


def target(name: str) -> str | None:
    """The managed server's address and port on its private network (what an
    endpoint routes to), or None for no managed server of that name."""
    found = _record(name)
    if not found or not found[1].get("address"):
        return None
    return f"{found[1]['address']}:{_port(*found[:2])}"


def _port(server: Server, values: dict) -> int:
    return values["port"] if server.kind == PACKAGE else DRIVERS[server.kind]["port"]


def networks() -> list[str]:
    """The private networks of the managed servers this installation keeps
    (a stopped one's too: its record and network stay)."""
    try:
        with state.db() as conn:
            found = conn.execute("select 1 from managed_servers limit 1").fetchone()
    except sqlite3.OperationalError:  # no tables yet (a host before its first migrate)
        return []
    return [CIDR] if found else []


def _network_changed():
    """The managed network joined or left what sandboxes reach: the host's
    routes now (the guard), the runtime's at the next network apply."""
    settings.request_effects(settings.HostUnit.HOST_GUARD, settings.HostUnit.RUNTIME_NETWORK)


@settings.check("access")
def _public_repos(before, after):
    if after.public_repos and networks():
        raise ValueError("public_repos can't be set while managed servers run: every sandbox reaches their network, "
                         "untrusted repos' too. Serve public repos from another installation.")


def _status(name: str, status: str):
    with state.db() as conn:
        conn.execute("update managed_servers set status = ? where name = ?", (status, name))


def _old_postgres(server: Server, values: dict) -> bool:
    """Postgres from before PostgreSQL 18 on verified CoW storage: its data stays as it is."""
    return server.kind == "postgres" and (values.get("postgres_major") != POSTGRES_MAJOR or not values.get("storage"))


def records():
    with state.db() as conn:
        names = [r[0] for r in conn.execute("select name from managed_servers order by name")]
    return [record(name) for name in names]


def record(name: str) -> dict:
    """A managed server as its listings show it (never a secret)."""
    server, values, status = _record(name)
    return {"name": name, "kind": server.kind,
            "package": values.get("package") if server.kind == PACKAGE else DRIVERS[server.kind]["package"],
            "status": status, "error": values.get("error", "") if status == "failed" else "",
            "address": values.get("address"), "port": _port(server, values),
            "postgres_major": values.get("postgres_major"), "storage": values.get("storage"),
            "cow": values.get("cow"), "legacy": _old_postgres(server, values), "used_by": connections.users(name)}


def execute(name, database, stream):
    """Stream operator SQL into a managed server without exporting its credentials."""
    with _lock():
        found = _record(name)
        if not found:
            raise ValueError("no managed server with this name; use the provider's own tools for other connections")
        server = found[0]
        if server.kind == PACKAGE:
            raise ValueError("only the Postgres and ClickHouse servers take SQL; use this server's own tools")
        if not server.owns(json.loads(docker("inspect", server.container).stdout)[0]["Config"].get("Labels")):
            raise ValueError("the container is not owned by this managed server")
        if server.kind == "postgres":
            command = ["psql", "-U", ADMIN, "-X", "-v", "ON_ERROR_STOP=1", "-d", database or "postgres"]
        else:
            command = ["sh", "-c", 'exec clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" '
                       '--multiquery --database "$1"', "sh", database or "default"]
        return subprocess.run(["docker", "exec", "-i", server.container, *command], stdin=stream).returncode


def _network() -> dict:
    inspected = docker("network", "inspect", NETWORK, check=False)
    if inspected.returncode:
        routes = json.loads(subprocess.run(["ip", "-j", "-4", "route"], capture_output=True, text=True, check=True).stdout)
        if any(row.get("dst") != "default" and ipaddress.ip_network(row.get("dst", "0.0.0.0/0"), strict=False)
               .overlaps(ipaddress.ip_network(CIDR)) for row in routes):
            raise ValueError(f"{CIDR} is already routed; choose a different service network before provisioning")
        docker("network", "create", "--label", f"{LABEL}=true", "--subnet", CIDR, NETWORK)
        inspected = docker("network", "inspect", NETWORK)
    network = json.loads(inspected.stdout)[0]
    if network.get("Labels", {}).get(LABEL) != "true" or network["IPAM"]["Config"][0]["Subnet"] != CIDR:
        raise ValueError(f"the existing {NETWORK} network is not owned by this installation")
    return network


def _address(network: dict) -> str:
    used = {v["IPv4Address"].partition("/")[0] for v in network.get("Containers", {}).values()}
    with state.db() as conn:
        names = [r[0] for r in conn.execute("select name from managed_servers")]
    used |= {_record(n)[1]["address"] for n in names}
    hosts = list(ipaddress.ip_network(CIDR).hosts())[9:249]
    found = next((str(a) for a in hosts if str(a) not in used), None)
    if not found:
        raise ValueError("the shared service network has no free addresses")
    return found


def _package(name: str):
    """The connection package whose server this host runs, or ValueError."""
    from . import catalog
    try:
        found = catalog.get(name)
    except PackageError as error:
        raise ValueError(str(error)) from None
    if not found.server:
        raise ValueError(f"{found.title} ({name}) connects to a server somebody else runs; a package this host "
                         "runs the server of has server: in its package.yaml")
    return found


def runs(package) -> bool:
    """Whether this host can run the server a connection package is to."""
    return package.name in BUILT_IN or bool(package.server)


def up(name, kind=None, *, package=None, data_root=None, storage_gb=None, presets=None):
    """Run the managed server and save its connection: one of the two built
    in (kind, or their connection package) or the one a connection package
    declares (package). presets: values for secrets the package would
    otherwise make, for a new server only."""
    with _lock():
        server, values, details = _begin(name, kind, package, data_root, storage_gb, presets)
        return _finish(server, values, details)


def begin(name, kind=None, *, package=None, storage_gb=None, presets=None) -> dict:
    """The quick half of up, for a caller that answers before the server runs
    (the API): everything that can refuse, and the server's record. finish()
    is the rest."""
    with _lock():
        _begin(name, kind, package, None, storage_gb, presets)
    return record(name)


def finish(name) -> dict:
    with _lock():
        found = _record(name)
        if not found:
            raise ValueError("no managed server with this name")
        server, values, _ = found
        return _finish(server, values, _details(server))


def _details(server: Server) -> dict | None:
    """docker inspect of a built-in server's container, None when it has none."""
    if server.kind == PACKAGE:
        return None
    existing = docker("inspect", server.container, check=False)
    return json.loads(existing.stdout)[0] if not existing.returncode else None


def _begin(name, kind, package, data_root, storage_gb, presets):
    if not NAME_RE.fullmatch(name) or len(name) > 40:
        raise ValueError("server names use at most 40 lowercase letters, numbers, hyphens or underscores")
    if bool(kind) == bool(package):
        raise ValueError("give the server's kind (postgres, clickhouse) or the connection package that runs it")
    declared = None
    if package:
        kind = BUILT_IN.get(package, PACKAGE)
        declared = _package(package) if kind == PACKAGE else None
    if kind not in DRIVERS and kind != PACKAGE:
        raise ValueError("supported managed servers are postgres, clickhouse and the packages that run a server")
    if data_root is not None and storage_gb is not None:
        raise ValueError("--data-root and --storage-gb are alternatives")
    if data_root is not None and kind != "postgres":
        raise ValueError("--data-root is Postgres's")
    if storage_gb is not None and kind == "clickhouse":
        raise ValueError("ClickHouse keeps its data in a Docker volume without a size")
    if presets and kind != PACKAGE:
        raise ValueError("only a package's server takes secrets")
    if settings.current().access.public_repos:
        raise ValueError("managed servers are reachable from every sandbox, so not with access.public_repos: "
                         "untrusted repos would reach them")
    from . import reach, runtime
    if not reach.private_reachable():
        titles = ", ".join(runtime.title(kind) for kind in runtime.enabled())
        raise ValueError(f"sandboxes on {titles} can't reach this host's private network, so a managed "
                         "server here would be out of their reach until it has an endpoint, which needs a domain: "
                         "connect a domain first, or use a hosted database instead")
    server = Server(name, kind)
    found = _record(server.name)
    if found and found[0].kind != server.kind:
        raise ValueError("this server name already has a different kind")
    if found and kind == PACKAGE and found[1].get("package") != package:
        raise ValueError(f"this server runs {found[1].get('package')}")
    if found and presets:
        raise ValueError("an existing server keeps the secrets it was made with")
    if found and kind == PACKAGE and storage_gb is not None and storage_gb != found[1]["storage"].get("size_gb"):
        raise ValueError("an existing server's disk cannot change size; remove the server and run it again")
    if found and server.kind == "postgres":
        previous = found[1]
        if _old_postgres(server, previous):
            raise ValueError("this Postgres predates PostgreSQL 18 storage; its data is unchanged. Create a new server "
                             "name and migrate with pg_dump/pg_restore")
        if data_root is not None and str(Path(data_root).resolve() / f"sbx-{server.name}") != previous["storage"]["path"]:
            raise ValueError("an existing server cannot change data roots; create a new server and migrate")
        if storage_gb is not None and storage_gb != previous["storage"].get("size_gb"):
            raise ValueError("an existing volume cannot change size through server up")
    network = _network()
    if found:
        values = found[1]
    else:
        if attachments.connection(server.name):
            raise ValueError("a connection of this name exists already and is not a managed server; choose another name")
        if not docker("inspect", server.container, check=False).returncode:
            raise ValueError("a container already exists without a managed record; choose a new server name")
        if not docker("volume", "inspect", server.volume, check=False).returncode:
            raise ValueError("a data volume already exists without a managed record; it is unchanged")
        joins = not networks()
        values = {"address": _address(network)}
        if kind == PACKAGE:
            if hosted_compose.exists(server.name):
                raise ValueError("containers of this server's name exist without a managed record; choose a new "
                                 "server name")
            unknown = sorted(set(presets or {}) - set(declared.server["secrets"]))
            if unknown:
                raise ValueError(f"{declared.title} has no secret {', '.join(unknown)} "
                                 f"({', '.join(declared.server['secrets']) or 'it has none'})")
            size = storage_gb if storage_gb is not None else declared.server["storage_gb"]
            values.update(package=package, port=declared.server["port"],
                          storage=cow_storage.volume_plan(server.name, size),
                          secrets={key: (presets or {}).get(key) or secrets.token_hex(32)
                                   for key in declared.server["secrets"]})
        else:
            values["password"] = secrets.token_hex(32)
        if server.kind == "postgres":
            values.update(postgres_major=POSTGRES_MAJOR,
                          storage=cow_storage.plan(server, data_root, storage_gb if storage_gb is not None else 32))
        _save(server, values, "preparing")
        if joins:
            _network_changed()
    details = _details(server)
    if details and not server.owns(details["Config"].get("Labels")):
        raise ValueError("refusing to adopt a container not owned by this server")
    if server.kind == "postgres" and details and not _matches(server, details, values):
        raise ValueError("the existing container does not match its PostgreSQL 18 storage record; it is unchanged")
    return server, values, details


def _finish(server: Server, values: dict, details: dict | None):
    """The slow half: the server runs, answers and has its connection. A
    failure is kept with the server (records), for whoever asked and left."""
    try:
        result = _run(server, values, details)
    except Exception as error:
        _save(server, {**values, "error": str(error)[:400]}, "failed")
        raise
    return result


def _run(server: Server, values: dict, details: dict | None):
    address = values["address"]
    deadline = time.monotonic() + 120
    if server.kind == PACKAGE:
        from . import catalog
        declared = _package(values["package"])
        made = {key: values["secrets"].get(key) or secrets.token_hex(32) for key in declared.server["secrets"]}
        if made != values["secrets"] or values.get("port") != declared.server["port"]:   # the package changed
            values = {**values, "secrets": made, "port": declared.server["port"]}
            _save(server, values)
        volumes = cow_storage.volume_prepare(server.name, values["storage"], declared.server["volumes"])
        hosted_compose.up(server.name, declared, values, NETWORK, LABEL, volumes)
        package, inputs = values["package"], catalog.render_server_inputs(declared, address, values["secrets"])
    else:
        mount_unit = cow_storage.prepare(values["storage"]) if server.kind == "postgres" else ""
        if details and not _log_limited(details):
            # Made before LOG_LIMIT: a container's log options are fixed when it is
            # created, so it is made again around the same data.
            if server.kind == "postgres" and (cow_storage.UNITS / server.unit).exists():
                cow_storage.run("systemctl", "stop", server.unit)
            docker("rm", "-f", server.container)
            details = None
        if details is None:
            _create_container(server, values)
        if server.kind == "postgres":
            cow_storage.start(server, values["storage"], mount_unit)
        elif details:
            docker("start", server.container)
        package, inputs = DRIVERS[server.kind]["package"], connection_inputs(server.kind, address, values["password"])
        if server.kind == "postgres":
            _status(server.name, "checking")
            while docker("exec", server.container, "pg_isready", "-U", ADMIN, "-d", "postgres", check=False).returncode:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Postgres did not become ready; retry the same command or inspect its service "
                                       "journal")
                time.sleep(2)
            values = _check(server)
    while True:  # the server may answer before it accepts its admin
        try:
            connections.save(server.name, package, inputs, by="sbx-admin server up")
            break
        except PackageError as error:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"the server did not pass its connection check ({error}); "
                                   "retry the same command") from None
            time.sleep(2)
    if values.get("error"):
        _save(server, {k: v for k, v in values.items() if k != "error"})
    _status(server.name, "ready")
    return {"name": server.name, "kind": server.kind, "package": package, "status": "ready",
            "container": server.container, "address": address, "port": _port(server, values), "public_ports": [],
            "storage": values.get("storage"), "cow": values.get("cow"),
            "next": "sbx-admin network apply (once, before sandboxes use a new private network)"}


def connection_inputs(kind: str, address: str, password: str) -> dict:
    """The saved connection's inputs. The managed Postgres speaks plain TCP on
    the private service network (the image has no certificate), so its URL
    says sslmode=disable: clients whose default is require (Go's lib/pq) would
    refuse it, and postgres-clone hands the same options to each sandbox."""
    if kind == "postgres":
        return {"url": f"postgresql://{ADMIN}:{password}@{address}:5432/postgres?sslmode=disable"}
    return {"url": f"http://{address}:8123", "user": ADMIN, "password": password}


def _matches(server: Server, details: dict, values: dict) -> bool:
    return details["Config"]["Image"] == server.image and any(
        m.get("Source") == values["storage"]["path"] and m.get("Destination") == server.data_path
        for m in details.get("Mounts", []))


def _log_limited(details: dict) -> bool:
    """Whether the container was created with a size limit on its log."""
    return bool(((details.get("HostConfig") or {}).get("LogConfig") or {}).get("Config", {}).get("max-size"))


def _container_size(cpus: int = 4, memory_mb: int = 4096) -> tuple[int, int]:
    """A managed server's CPU and memory cap: 4 CPUs and 4 GiB, or what a
    smaller host has (Docker refuses more CPUs than the host has; half its
    memory leaves room for sbx and the rest)."""
    host_cpus = os.cpu_count() or 1
    try:
        with open("/proc/meminfo") as found:
            total_mb = next(int(line.split()[1]) // 1024 for line in found if line.startswith("MemTotal:"))
    except (OSError, StopIteration, ValueError):
        total_mb = memory_mb * 2
    return min(cpus, host_cpus), max(512, min(memory_mb, total_mb // 2))


def _create_container(server: Server, values: dict):
    if server.kind == "postgres":
        env = {"POSTGRES_USER": ADMIN, "POSTGRES_PASSWORD": values["password"], "POSTGRES_DB": "postgres"}
        extra = ["--health-cmd", f"pg_isready -U {ADMIN} -d postgres", "--health-interval", "2s"]
        # Logical replication for consumers such as ElectricSQL (postgres-clone's replication input).
        command = ["postgres", "-c", "file_copy_method=clone", "-c", "wal_level=logical",
                   "-c", "max_replication_slots=100", "-c", "max_wal_senders=100"]
        mount = f"type=bind,src={values['storage']['path']},dst={server.data_path}"
        run = ["create", "--restart", "no"]   # its systemd unit starts it, after its storage is mounted
    else:
        found = docker("volume", "inspect", "--format", "{{json .Labels}}", server.volume, check=False)
        if not found.returncode and not server.owns(json.loads(found.stdout)):
            raise ValueError("refusing to adopt a volume not owned by this server")
        docker("volume", "create", "--label", f"{LABEL}={server.name}", server.volume)
        env = {"CLICKHOUSE_USER": ADMIN, "CLICKHOUSE_PASSWORD": values["password"],
               "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT": "1"}
        extra, command = [], []
        mount = f"type=volume,src={server.volume},dst={server.data_path}"
        run = ["run", "-d", "--restart", "unless-stopped"]
    cpus, memory_mb = _container_size()
    docker(*run, "--name", server.container, "--label", f"{LABEL}={server.name}",
           "--network", NETWORK, "--ip", values["address"], "--memory", f"{memory_mb}m", "--cpus", str(cpus),
           *LOG_LIMIT, "--mount", mount, "--env-file", "/dev/stdin", *extra, server.image, *command,
           input="".join(f"{k}={v}\n" for k, v in env.items()))


def check(name):
    with _lock():
        found = _record(name)
        if not found or found[0].kind != "postgres":
            raise ValueError("choose a managed PostgreSQL 18 server")
        values = _check(found[0])
        return {"name": name, "cow": values["cow"], "storage": values["storage"]}


def _check(server: Server) -> dict:
    """Verify its storage shares blocks between a populated database and its
    clone; recorded with the server either way."""
    values = _record(server.name)[1]
    if _old_postgres(server, values):
        raise ValueError("this Postgres has no verified CoW storage; create a new server and migrate")
    details = json.loads(docker("inspect", server.container).stdout)[0]
    if not server.owns(details["Config"].get("Labels")):
        raise ValueError("the container is not owned by this server")
    try:
        if not _matches(server, details, values):
            raise ValueError("the container does not match its recorded PostgreSQL 18 storage")
        if cow_storage.run("findmnt", "-n", "-o", "FSTYPE", "--target", values["storage"]["path"]) != "xfs":
            raise ValueError("Postgres storage is not mounted on XFS")
        values["cow"] = {**postgres_cow.verify(server, values["storage"], docker),
                         "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    except (RuntimeError, OSError, ValueError):
        values["cow"] = {"verified": False}
        _save(server, values, "failed")
        raise
    _save(server, values, "ready")
    return values


def down(name, *, delete_data=False):
    """Stop the managed server and remove its connection (refused while a
    repo uses it); with delete_data its data and its record too, so nothing
    of it is left on the host."""
    with _lock():
        found = _record(name)
        if not found:
            raise ValueError("no managed server with this name")
        server, values, _ = found
        if _old_postgres(server, values):
            raise ValueError("this Postgres predates PostgreSQL 18 storage; migrate it before removing it")
        inspected = docker("inspect", server.container, check=False)
        if not inspected.returncode and not server.owns(json.loads(inspected.stdout)[0]["Config"].get("Labels")):
            raise ValueError("refusing to remove a container not owned by this server")
        volume = None
        if delete_data and server.kind == "clickhouse":
            volume = docker("volume", "inspect", "--format", "{{json .Labels}}", server.volume, check=False)
            if not volume.returncode and not server.owns(json.loads(volume.stdout)):
                raise ValueError("refusing to remove a data volume not owned by this server")
        connections.remove(name)
        if server.kind == PACKAGE:
            hosted_compose.down(name, values, delete_data=delete_data)
            if delete_data:
                cow_storage.volume_remove(values["storage"])
        else:
            if server.kind == "postgres" and (cow_storage.UNITS / server.unit).exists():
                cow_storage.run("systemctl", "disable", "--now", server.unit)
            if not inspected.returncode:
                docker("rm", "-f", server.container)
        if delete_data:
            if server.kind == "postgres":
                cow_storage.remove(server, values["storage"])
            elif server.kind == "clickhouse" and not volume.returncode:
                docker("volume", "rm", server.volume)
            from . import endpoints
            endpoints.forget_server(name)
            with state.db() as conn:
                secret_store.delete_rows(conn, "managed_servers", "name = ?", (name,))
            if not networks():
                _network_changed()
        else:
            _status(name, "stopped")
        return {"name": name, "stopped": True, "data_deleted": delete_data}


SCHEMA = state.Schema(tables="""
create table if not exists managed_servers (
    name text primary key, kind text not null, config text not null, status text not null default 'preparing');
""", sealed=(("managed_servers", "config"),))
