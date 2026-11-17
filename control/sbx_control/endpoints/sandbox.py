"""The sandbox's side: on a runtime without private_networks, its values
name the forwarder's local address for every endpoint its repo may use
(Routes), its runtime carries the forwarder's config, and its certificates
are one store in RUN (RENEWED): a bundle per endpoint, written after each
runtime write, when sbx resumes it, when the keeper finds them running out,
and by the forwarder itself when it pulls new ones (issue_for). The
endpoints' SandboxLayer (Layer): core and the keeper reach this module only
through sandbox_layers."""
from __future__ import annotations

import dataclasses
import functools
import json
import secrets

from .. import contributions, paths, platform, runtime, sandbox_layers, settings, state
from . import certs
from .model import INTERNAL, LOCAL_PORTS, Refused, address, lock, domain, env_name, join, permitted, split_target
from ..tls_front import PORT   # where sandboxes reach endpoints: the HTTPS port, shared by SNI
from .access import HOSTPORT

LOCAL = "127.0.0.1"
FOLDER = "endpoints"                    # its contribution's folder in the sandbox (contributions.folder)
CONFIG = f"{contributions.EXT}/{FOLDER}/config.json"
RENEWED = f"{platform.RUN}/endpoint-certs"   # the certificates: BUNDLE files and BUNDLE_INDEX (sbx_guest.RENEWED)
BUNDLE = "{}.bundle.pem"                # an endpoint's key and certificate in one file (sbx_guest.BUNDLE)
BUNDLE_INDEX = "bundles.json"           # {name: not_after} (sbx_guest.BUNDLE_INDEX)
STATUS = f"{platform.RUN}/forwarder-status.json"   # the forwarder's {"unbound": {name: port}}
CERTS_PATH = "/api/sandbox/endpoint-certs"
MARK = "---sbx-forwarder---"


@dataclasses.dataclass(frozen=True)
class Routes:
    """For one sandbox: its values with every permitted endpoint's target
    replaced by the forwarder's address. Empty for a sandbox that reaches
    the private network itself, or whose repo may use none."""
    rows: tuple = ()          # the permitted endpoints (Endpoint)

    def placed(self, taken) -> "Routes":
        """The same endpoints on local ports nothing else in the sandbox
        listens on: each its own port, else the next free one in LOCAL_PORTS."""
        used, rows = set(taken), []
        for row in self.rows:
            port = next((p for p in (*range(row.local_port, LOCAL_PORTS.stop), *LOCAL_PORTS) if p not in used), None)
            if port is None:
                raise RuntimeError(f"no free port in {LOCAL_PORTS.start}-{LOCAL_PORTS.stop - 1} for the endpoints")
            used.add(port)
            rows.append(dataclasses.replace(row, local_port=port))
        return Routes(tuple(rows))

    @functools.cached_property
    def _ports(self) -> dict:
        return {split_target(r.target): r.local_port for r in self.rows}

    def _local(self, host: str, port: int) -> int | None:
        try:
            return self._ports.get(split_target(join(host.strip("[]"), port)))
        except Refused:
            return None

    def text(self, value):
        if not self.rows or not isinstance(value, str):
            return value

        def replace(match):
            port = self._local(match.group(1), int(match.group(2)))
            return f"{LOCAL}:{port}" if port else match.group(0)
        return HOSTPORT.sub(replace, value)

    def outputs(self, created: dict) -> dict:
        """A sandbox's services (key -> resources.Created) with their outputs
        localized: host:port in any value, and a host and port output pair."""
        if not self.rows:
            return created
        out = {}
        for key, item in created.items():
            values = {k: self.text(v) for k, v in item.outputs.items()}
            host, port = values.get("host"), values.get("port")
            if isinstance(host, str) and str(port or "").isdecimal() and (local := self._local(host, int(port))):
                values.update(host=LOCAL, port=str(local))
            out[key] = dataclasses.replace(item, outputs=values)
        return out

    def env(self, envs: dict) -> dict:
        """The env localized, with SBX_ENDPOINT_<NAME> for each internal host's endpoint."""
        if not self.rows:
            return envs
        out = {k: self.text(v) for k, v in envs.items()}
        out.update({env_name(r.name): f"{LOCAL}:{r.local_port}" for r in self.rows if r.kind == INTERNAL})
        return out

    def config(self, name: str, credential: str) -> dict:
        """The forwarder's config: where each endpoint listens and goes, and how it renews."""
        return {"endpoints": [{"name": r.name, "address": address(r.name), "port": PORT, "local_port": r.local_port}
                              for r in self.rows],
                "renew": {"url": f"{settings.current().base_url}{CERTS_PATH}", "sandbox": name,
                          "credential": credential}}


def routes(kind: str | None, repo: str | None) -> Routes:
    """The sandbox's routes on runtime kind: the endpoints its repo may use,
    on a runtime without private_networks, with a domain; else empty."""
    if not repo or not domain() or runtime.capabilities(kind or None).private_networks:
        return Routes()
    return Routes(tuple(permitted(repo)))


def listening_and_own(sbx) -> tuple[set, set]:
    """(the ports something in the sandbox listens on, the forwarder's own among them)."""
    from .. import core
    output = sbx.commands.run(f"{core.LISTENING}; echo {MARK}; cat {CONFIG} 2>/dev/null || true", timeout=30).stdout
    listening, _, config = output.partition(MARK)
    try:
        own = {int(e["local_port"]) for e in json.loads(config)["endpoints"]}
    except (ValueError, KeyError, TypeError):
        own = set()
    return {port for _, port in core.listening_ports(listening)}, own


def _self_signed() -> str:
    """The installation's certificate when it is self-signed (--dns none), as the forwarder's trust anchor."""
    from .. import templates
    if not templates.pinned_key():
        return ""
    return (paths.tls() / "letsencrypt/live" / domain() / "fullchain.pem").read_text()


def write_bundles(sbx, issued: dict):
    """The certificates into RENEWED, each key and certificate one file
    replaced whole, the files before the index that points at them; a
    revoked endpoint's go."""
    staging = f"{RENEWED}/.push"
    sbx.commands.run(f"install -d -m 700 {RENEWED} && rm -rf {staging} && install -d -m 700 {staging}", timeout=30)
    for endpoint, (cert, key, _) in issued.items():
        sbx.files.write(f"{staging}/{BUNDLE.format(endpoint)}", key + cert, user="user")
    sbx.files.write(f"{staging}/{BUNDLE_INDEX}", json.dumps({n: v[2] for n, v in issued.items()}), user="user")
    names = [BUNDLE.format(e) for e in issued]
    keep = "".join(f" ! -name {n}" for n in names)
    sbx.commands.run(f"cd {staging} && chmod 600 * && for f in {' '.join(names)} {BUNDLE_INDEX}; do mv -f \"$f\" ../; "
                     f"done && cd .. && rmdir {staging} && find . -maxdepth 1 -name '*.bundle.pem'{keep} -delete",
                     timeout=30)


def ensure_forwarder(sbx) -> dict:
    """With a config in the sandbox, its forwarder running (started again
    when it died). Its status ({"unbound": {name: port}}), {} without a config."""
    from .. import sandbox_runtime
    command = (f"[ -f {CONFIG} ] || exit 0; {sandbox_runtime.guest('forward', 'ensure', CONFIG)} "
               f">> /tmp/sbx-endpoints.log 2>&1; cat {STATUS} 2>/dev/null || true")
    output = sbx.commands.run(command, timeout=60).stdout.strip()
    try:
        return json.loads(output) if output else {}
    except ValueError:
        return {}


def _issue(name: str, repo: str, kind: str | None, only=None) -> dict:
    """certs.issue_all for the endpoints (of only, when given) the repo may
    use now, chosen and signed under the endpoints' lock: a revoke's new CA
    never signs for the repo it revoked. {} when it may use none."""
    with lock():
        names = [r.name for r in routes(kind, repo).rows if only is None or r.name in only]
        return certs.issue_all(name, repo, names) if names else {}


def push(sbx, info) -> bool:
    """Fresh certificates into the running sandbox, for the endpoints its
    repo may use now; False for one that may use none."""
    from .. import core
    name, repo = (info.metadata or {}).get("sbx"), core.app_of(info).repo
    issued = _issue(name, repo, runtime.kind_of(info)) if name else {}
    if not issued:
        return False
    write_bundles(sbx, issued)
    ensure_forwarder(sbx)
    return True


def issue_for(authorization: str) -> list[dict]:
    """POST /api/sandbox/endpoint-certs: new certificates for the endpoints
    of the sandbox the callback's `Bearer NAME:CREDENTIAL` names, those its
    repo may use now. LookupError for a sandbox that is gone, or that holds
    another credential by now (made again under the name); PermissionError
    when it may use none (a revoked grant, a runtime that reaches the
    private network itself)."""
    from .. import core, sandboxes
    name = authorization.removeprefix("Bearer ").partition(":")[0]
    info = core.find(name) if name and sandboxes.registered_owner(name) else None
    if not info:
        raise LookupError(f"no sandbox {name}")
    repo = core.app_of(info).repo
    if certs.bound_repo(authorization) != (repo or "").lower():   # the credential checked again, with its repo
        raise LookupError(f"{name} is another sandbox now")
    issued = _issue(name, repo, runtime.kind_of(info))
    if not issued:
        raise PermissionError(f"{name}'s repo may use no endpoint now")
    return [{"name": n, "cert": c, "key": k, "not_after": t} for n, (c, k, t) in sorted(issued.items())]


class Layer(sandbox_layers.SandboxLayer):
    name = "endpoints"
    ports = frozenset(LOCAL_PORTS)

    def __init__(self):
        self._holders: set | None = None   # this round's sandboxes with certificates
        self._unbound_said: dict = {}      # sandbox -> the {name: port} its forwarder couldn't listen on, said once

    def _routes(self, write) -> Routes:
        mine = write.of(self)
        if "routes" not in mine:
            found = routes(write.kind, write.repo)
            if found.rows and write.sbx is not None:
                taken, own = listening_and_own(write.sbx)
                found = found.placed(taken - own)
            mine["routes"] = found
        return mine["routes"]

    def outputs(self, write, created):
        return self._routes(write).outputs(created)

    def values(self, write, envs):
        return self._routes(write).env(envs)

    def contribution(self, write):
        """The forwarder's config, with a new credential (bound once it is written)."""
        found = self._routes(write)
        if not found.rows:
            return None
        credential = write.of(self)["credential"] = secrets.token_urlsafe(32)
        config, files = found.config(write.name, credential), {}
        if trust := _self_signed():
            config["trust"] = "server.pem"
            files["server.pem"] = contributions.File(trust)
        files["config.json"] = contributions.File(json.dumps(config, indent=1))
        return contributions.Contribution(FOLDER, runtime=contributions.RuntimeLayer(files=files))

    def after_write(self, write, sbx):
        """What the written config names, put in place: its credential bound,
        its certificates issued and written, the forwarder running. A sandbox
        that may use none holds none."""
        from .. import sandbox_runtime
        found, mine = self._routes(write), write.of(self)
        if not found.rows:
            certs.forget(write.name)
            sbx.commands.run(f"rm -rf {RENEWED}", timeout=30)
            return
        certs.bind(write.name, write.repo, mine["credential"])
        write_bundles(sbx, _issue(write.name, write.repo, write.kind, {r.name for r in found.rows}))
        sandbox_runtime.ship_guest(sbx)
        ensure_forwarder(sbx)

    def forget(self, name):
        """A deleted sandbox's credential goes with it: one made again under the name never takes it."""
        certs.forget(name)

    def on_resume(self, sbx, info):
        push(sbx, info)   # its certificates may have run out while it slept

    def tick(self, info):
        """Its forwarder running, and a port it can't listen on said once as an event."""
        name = (info.metadata or {}).get("sbx")
        if not domain():   # no domain, no endpoint: nothing to look at
            return
        if self._holders is None:
            self._holders = certs.holders()
        if not name or name not in self._holders:
            return
        unbound = ensure_forwarder(runtime.connect(info.sandbox_id)).get("unbound") or {}
        if unbound and unbound != self._unbound_said.get(name):
            state.event(name, "endpoint port taken", ", ".join(
                f"{endpoint} can't listen on 127.0.0.1:{port}: something else there does; stop it, or the next "
                "env delivery or restart places the endpoint on a free port" for endpoint, port in sorted(unbound.items())))
        self._unbound_said[name] = unbound

    def round(self, infos, running):
        """Running sandboxes whose certificates run out within RENEW_HOURS get
        new ones (paused ones get them when they resume)."""
        self._holders = None
        if not domain():
            return
        due = set(certs.due()) & set(running)
        for info in infos:
            if (info.metadata or {}).get("sbx") in due:
                push(runtime.connect(info.sandbox_id), info)
