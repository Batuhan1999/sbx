"""Create and manage repository workspaces, injected env, checkout, and app processes.

What extensions put into a sandbox's runtime comes from extensions.py as
contributions; this module writes them and never names an extension.
Personal agent credentials travel directly from the desktop to the sandbox
and are not stored by the control service."""
import contextlib
import fcntl
import json
import hashlib
import pathlib
import re
import shlex
import threading
import time
import uuid

from . import (addressing, contributions, delivery, endpoints, extensions, ip_previews, lifecycle, paths, platform,
               recipes, runtime, secret_store, settings, state, variables)
from .template_files import sbx_guest

RUN = platform.RUN
HOME = platform.HOME
APP_SCRIPTS = platform.APP_SCRIPTS
SETUP_LOCK = f"{HOME}/.sbx-setup.lock"
# The platform's helper in the sandbox: process supervision, SSH identity,
# swap, usage (template_files/sbx_guest.py). Extensions' guest scripts
# shipped next to it import it.
GUEST_SOURCE = pathlib.Path(sbx_guest.__file__)
GUEST = f"{RUN}/{GUEST_SOURCE.name}"
# A sandbox pauses this long after its last sign of use (sbx-control's
# keeper extends it while it is used) and the gate resumes it on the next
# request. Paused, it holds no memory on the host. Each pause writes a new
# memory layer, as large as what the sandbox used, that the host keeps, so
# not too soon.
TIMEOUT_SECONDS = 2 * 60 * 60
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


class SandboxError(Exception):
    pass


class NoCapacity(SandboxError):
    pass


class PauseRefused(SandboxError):
    """The runtime refused to pause a sandbox, which keeps running."""


# The runtime refuses a pause it cannot take now (runtime.Busy): it is still
# saving the sandbox's parent, say, or has no room for its snapshot. The
# sandbox keeps running; retry.
PAUSE_RETRY_SECONDS = (5, 15)


def runtime_failure(error: BaseException) -> bool:
    """Whether the error is the sandbox runtime's or the connection to it:
    it answered with an error or not at all, as while `sbx-admin network
    apply` or an upgrade recreates it."""
    return runtime.failure(error)


class NotReady(SandboxError):
    """What an extension needs is not in the sandbox's template (its ready check failed)."""


def capacity(memory_mb: int | None = None, kind: str | None = None) -> dict:
    """Room for sandboxes of memory_mb (the default size) as the runtime of
    kind (None: the default) reports it; when it can't say, {"known": False,
    "limits": the most it allows (runtime.Limits.known)}."""
    room = runtime.capacity(memory_mb or recipes.DEFAULT_SIZE["memory_mb"], kind)
    if not room.get("known"):
        room = {**room, "limits": runtime.limits(kind).known()}
    return room


def check_capacity(memory_mb: int | None = None, kind: str | None = None) -> dict:
    """capacity(), or NoCapacity when no sandbox of memory_mb fits now or
    as many as the runtime runs at once are running."""
    if why := runtime.too_many(kind):
        raise NoCapacity(why)
    room = capacity(memory_mb, kind)
    if room.get("known") and room["slots_free"] < 1:
        raise NoCapacity(
            f"no free capacity on the sandbox host for another {room['memory_mb'] // 1024} GB sandbox "
            f"({room.get('free_mb', 0) // 1024} GB of sandbox memory is free); one frees up when a sandbox "
            f"pauses after {TIMEOUT_SECONDS // 3600} hours idle, or when someone pauses or deletes one "
            f"({settings.current().base_url}/app/sandboxes)")
    return room


@contextlib.contextmanager
def reserve_capacity(memory_mb: int, kind: str | None = None):
    """Hold until the runtime of kind has placed the new sandbox, so two
    creations can't both take the last room."""
    with runtime.placing(kind):
        yield check_capacity(memory_mb, kind)


@contextlib.contextmanager
def sandbox_operation(name: str):
    """Serialize create/delete transactions for one public sandbox name."""
    valid_name(name)
    directory = paths.state_dir() / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"sandbox-{name}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def app_of(info) -> recipes.App:
    return recipes.load((info.metadata or {}).get("repo"))


def branch_of(info) -> str:
    meta = info.metadata or {}
    return meta.get("branch") or recipes.default_branch(meta.get("repo"))


def runtime_state(info) -> str:
    if info is None:
        return "missing"
    return str(getattr(info.state, "value", info.state)).lower().rpartition(".")[2]


LISTENING = "ss -Hltn"  # every listening TCP socket, one per line


def detectable(port: int, a: recipes.App) -> bool:
    """Not below 1024 but 80: those are system daemons such as the DNS stub,
    and 443 would be TLS, which the proxy in front of the sandbox doesn't
    speak to it."""
    return detectable_port(port) and addressing.preview_on(a, port) is None


def detectable_port(port: int) -> bool:
    return ((port == 80 or 1024 <= port <= 65535) and port not in platform.PLATFORM_PORTS
            and port not in runtime.ports() and port not in extensions.reserved_ports())


def detected_ports(listening: str, a: recipes.App) -> dict:
    """port -> whether it listens on loopback only, from `ss -Hltn`. A runtime's
    forwarder may reset connections to loopback-only ports under load, so those
    are reported but not exposed: the server has to listen on 0.0.0.0."""
    out = {}
    for line in listening.splitlines():
        fields = line.split()
        address, _, port = fields[3].rpartition(":") if len(fields) > 3 else ("", "", "")
        if not port.isdigit() or not detectable(int(port), a):
            continue
        address = address.strip("[]").partition("%")[0].removeprefix("::ffff:")
        loopback = address.startswith("127.") or address in ("::1", "localhost")
        out[int(port)] = out.get(int(port), True) and loopback
    return out


def suffix_port(suffix: str):
    match = re.fullmatch(r"-([1-9][0-9]{1,4})", suffix)
    return int(match.group(1)) if match and detectable_port(int(match.group(1))) else None


def preview_suffixes() -> set:
    return {suffix for repo in [*recipes.with_recipe(), None] for suffix in addressing.ports(recipes.load(repo)) if suffix}


def valid_name(name: str) -> str:
    """A name any sandbox may have had: what operating on an existing one
    checks, since a name can become reserved after its sandbox was made."""
    if not NAME_RE.match(name):
        raise SandboxError(f"name must match {NAME_RE.pattern}")
    return name


def check_name(name: str) -> str:
    """A name a new sandbox may take."""
    valid_name(name)
    if name in settings.current().reserved_names or any(name.endswith(suffix) for suffix in preview_suffixes()):
        raise SandboxError(f"{name} is reserved")
    return name


def slugify(branch: str, limit: int = 40) -> str:
    """A DNS label from a branch name: feature/ABC-1_chat ui -> feature-abc-1-chat-ui."""
    slug = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")[:limit].strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"b-{slug}"[:limit].strip("-")
    return slug


def sandboxes(metadata=None, state=None):
    """What the runtime lists; state: runtime.RUNNING and PAUSED, None for its default."""
    return runtime.sandboxes(metadata, state)


def sandboxes_complete(metadata=None, state=None):
    """sandboxes(), or runtime.Incomplete when a runtime did not answer: for
    whoever acts on what is not listed (deletes, forgets, prunes)."""
    return runtime.complete(sandboxes(metadata, state))


def find(name):
    """The sandbox named name; None only when every runtime answered without
    it (runtime.Incomplete when one did not answer: it may be there)."""
    found = sandboxes({"sbx": name})
    if found:
        return found[0]
    runtime.complete(found)
    return None


def sbx_env(name: str, slug: str, a: recipes.App, owner: str = "", branch: str = "", template: str = "") -> dict:
    """What the app's scripts, shells and agents know about the
    sandbox: SBX_URL for the main preview, SBX_URL_<PREVIEW> for the rest."""
    return {"SBX_NAME": name, "SBX_SLUG": slug, "SBX_OWNER": owner, "SBX_BRANCH": branch, "SBX_REPO": a.repo,
            "SBX_TEMPLATE": template, "SBX_WORKDIR": a.workdir, "SBX_DOMAIN": addressing.domain(),
            "SBX_HOST_SUFFIX": addressing.host_suffix(), **addressing.env_urls(slug, a)}


def run_in(sbx, script, timeout=60, env=f"{RUN}/env"):
    """Run with the sandbox's env as SSH shells and agents see it, or with
    the app's (env=f"{RUN}/app-env", which adds the provider keys)."""
    return sbx.commands.run(f"bash -c {shlex.quote(f'. {env}; ' + script)}", timeout=timeout)


# $$, ${NAME}, ${secret:NAME}, ${outputs.NAME} (a service's own, in its env:)
# and ${services.KEY.NAME} (an output of the sandbox's service KEY).
ENV_REF = re.compile(r"\$\$|\$\{(?:(secret):|(outputs)\.|services\.([a-z0-9_-]+)\.)?([A-Za-z_][A-Za-z0-9_]*)\}")
# ${random:32} or ${random:hex64} in sbx.yaml's env: a secret made once per
# sandbox and variable (NEXTAUTH_SECRET, an encryption key), the same across resumes.
RANDOM_REF = re.compile(r"\$\{random:(hex|base64:)?(\d{1,3})\}")


def expand(value, where: str, lookup) -> str:
    """A value with its references filled in by lookup(kind, name, service):
    kind is secret, outputs, services or None (a plain ${NAME})."""
    def resolve(match):
        if match.group(0) == "$$":
            return "$"
        kind = match.group(1) or match.group(2) or ("services" if match.group(3) else None)
        found = lookup(kind, match.group(4), match.group(3))
        if found is None:
            what = {"secret": "secret ", "outputs": "outputs.", "services": f"services.{match.group(3)}."}.get(kind, "")
            raise SandboxError(f"{where}: no {what}{match.group(4)}")
        return found
    return ENV_REF.sub(resolve, "true" if value is True else "false" if value is False else str(value))


def sandbox_env_reference(value, owned=()):
    return isinstance(value, str) and bool(RANDOM_REF.search(value) or any(
        m.group(2) == 'outputs' or m.group(3) or
        (m.group(1) is None and (m.group(4).startswith('SBX_') or m.group(4) in owned))
        for m in ENV_REF.finditer(value) if m.group(4)))


def app_env(a: recipes.App, env: dict, trusted: bool = True, created: dict | None = None,
            owned: set | None = None) -> dict:
    """Each layer over the last: its services' env: defaults, sbx.yaml's,
    the installation's variables for "*", then for the patterns matching the
    repo. Values may name the sandbox's own (${SBX_URL_API}, or a key set
    before), a secret (${secret:NAME}: sbx.yaml only the repo's own, the
    variables also the installation's and the platform's) and an output of
    one of its services (${services.db.url}); a service's defaults name its
    own (${outputs.url}). $$ is a $. SBX_* go above these."""
    created = created or {}
    layers = [(f"service {key}", c.env, c.outputs) for key, c in created.items()]
    layers += [("sbx.yaml", a.spec.get("env") or {}, None)]
    if trusted:
        layers += [("installation", values, None) for values in variables.installation_layers(a.repo)]
    out = {}
    owned = owned if owned is not None else set()
    for source, values, outputs in layers:
        for key, value in values.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise SandboxError(f"{source} env: {key} is not a variable name")
            known = {**out, **env}
            dynamic = sandbox_env_reference(value, owned)
            owned.discard(key)
            if dynamic:
                owned.add(key)
            if source == "sbx.yaml" and isinstance(value, str) and RANDOM_REF.search(value) and env.get("SBX_NAME"):
                value = RANDOM_REF.sub(lambda m: variables.random_value(env["SBX_NAME"], key, m.group(1) == "hex", int(m.group(2)),
                                                                    base64_=m.group(1) == "base64:"),
                                       value)

            def lookup(kind, name, service):
                if kind == "secret" and source == "installation":
                    return variables.values(a.repo).get(name) or variables.values(None).get(name) \
                        or platform.credentials().all.get(name)
                if kind == "secret":
                    return variables.values(a.repo).get(name) if trusted and source == "sbx.yaml" else None
                if kind == "outputs":
                    return (outputs or {}).get(name)
                if kind == "services":
                    return created[service].outputs.get(name) if service in created else None
                return known.get(name)
            out[key] = expand(value, f"{source} env {key}", lookup)
    return out


def create_services(a: recipes.App, env: dict) -> dict:
    """with: values may name the sandbox's own (${SBX_NAME}), the repo's
    secrets and the outputs of the services above."""
    def inputs(key, with_, created):
        def lookup(kind, ref, service):
            if kind == "secret":
                return variables.values(a.repo).get(ref)
            if kind == "services":
                return created[service].outputs.get(ref) if service in created else None
            return env.get(ref) if kind is None else None
        return {k: expand(v, f"services.{key}.with.{k}", lookup) for k, v in with_.items()}
    return extensions.packages().provision(env["SBX_NAME"], a.repo, inputs)


def reconcile_services():
    existing = {(i.metadata or {}).get("sbx") for i in sandboxes_complete()} | lifecycle.busy()
    for name in variables.kept() - existing:
        try:
            variables.forget(name)
        except secret_store.StoreError:
            state.event(name, 'secret cleanup deferred', 'the store is unavailable; retained for retry', internal=True)
    extensions.packages().reconcile(existing)


def create(name: str, slug: str, owner: str, branch: str, public_keys: str, repo: str | None = None,
           source_commit: str | None = None, *, template: str, memory_mb: int | None = None,
           metadata: dict | None = None, kind: str | None = None) -> dict:
    """Return after transport and the requested source are usable; app setup
    is separate. template: what the VM starts from (template_builds.template_for,
    or a snapshot an extension keeps); memory_mb: the VM's memory as the
    template was built (template_builds.size_of), else the recipe's size;
    metadata: generic tags: the extension that asked for the sandbox
    (connector), whose env values it gets (audience, personal) and whether
    the team may open it (shared); kind: the runtime it runs on (None: the
    default; sandboxes.create chooses it, runtimes.choose)."""
    check_name(name)
    if repo and not settings.trust(repo.lower()):
        raise SandboxError(f"{repo} is not served here: not in the settings' repos, and public_repos is off")
    a = recipes.load(repo)
    metadata = {**(metadata or {}), "template": template}
    if source_commit is not None:
        if not a.repo or not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", source_commit):
            raise SandboxError("source_commit requires a repository and a full Git commit hash")
        source_commit = source_commit.lower()
    # The VM is the template's size: with memory_mb, a template built at
    # that memory (or a snapshot), whose cpu and disk are not known here.
    if why := runtime.beyond_limits({"memory_mb": memory_mb} if memory_mb else a.size, kind):
        raise SandboxError(why)
    with lifecycle.operation(name, "create") as operation, \
            reserve_capacity(memory_mb or a.size["memory_mb"], kind):
        operation.configure_workspace(a.repo)
        return _create_reserved(name, slug, owner, branch, public_keys, a, operation=operation,
                                source_commit=source_commit, metadata=metadata, kind=kind)


def _create_reserved(name: str, slug: str, owner: str, branch: str, public_keys: str,
                     a: recipes.App, *, operation: lifecycle.Operation | None = None,
                     source_commit=None, metadata=None, kind: str | None = None) -> dict:
    """Create after ``reserve_capacity`` has serialized the last-slot check."""
    if find(name):
        raise SandboxError(f"sbx-{name} already exists")
    ip_previews.activate(slug)
    try:
        env = sbx_env(name, slug, a, owner, branch, (metadata or {}).get("template", ""))
    except Exception:
        ip_previews.remove(slug)
        raise
    trusted = bool(a.repo) and settings.trust(a.repo) == "trusted"
    if a.spec.get("services") and not trusted:
        raise SandboxError(f"{a.repo} declares services in .sbx/sbx.yaml; only repos this installation trusts get them")
    result = None
    try:
        if not trusted:
            if operation:
                if a.repo:
                    operation.advance("provisioning_services")
                    operation.advance("services_ready")
                operation.advance("allocating")
            result = _start(a, env, app_env(a, env, trusted=False), public_keys, trusted=False,
                            operation=operation, metadata=metadata, kind=kind,
                            routes=endpoints.routes(kind, a.repo))
        else:
            if operation:
                operation.advance("provisioning_services")
            created = create_services(a, env)
            # On a runtime without private_networks: the forwarder's address for each endpoint the repo may use.
            routes = endpoints.routes(kind, a.repo)
            values, hidden = trusted_values(a, env, routes.outputs(created), audience(metadata), personal(metadata))
            if operation:
                operation.advance("services_ready")
                operation.advance("allocating")
            result = _start(a, env, values, public_keys, hidden=hidden,
                            operation=operation, metadata=metadata, kind=kind, routes=routes)
        if operation:
            operation.advance("preparing_workspace", sandbox_id=result["sandbox_id"], runtime="running")
            # A new sandbox's checkout is its template's: what the template's setup changed is nobody's edit.
            prepare_workspace(runtime.connect(result["sandbox_id"]), a, branch, checkout=True, force=True,
                              source_commit=source_commit)
            operation.advance("workspace_ready", finish=True)
            result["lifecycle"] = lifecycle.get(name)
        return result
    except Exception as error:
        checkpoint = lifecycle.get(name) if operation else None
        if checkpoint and (checkpoint.get("sandbox_id") or checkpoint.get("phase") == "allocating"):
            raise
        if result and result.get("sandbox_id"):
            runtime.kill(result["sandbox_id"])
        ip_previews.remove(slug)
        if trusted:
            extensions.packages().release(name)
        if isinstance(error, runtime.NoRoom):
            raise NoCapacity("no free capacity on the sandbox host; delete a sandbox (sbx ls, sbx down) "
                               "or try again later") from error
        if isinstance(error, extensions.packages().Error):
            raise SandboxError(str(error)) from error
        raise


def trusted_values(a: recipes.App, env: dict, created: dict, kind: str, mine: bool = True) -> tuple[dict, set]:
    """(a trusted repo's sandbox's env but SBX_*, the names only its app
    gets): what services, sbx.yaml and the installation's variables declare
    (app_env), then the repo's values as set with sbx env or the UI, then
    its owner's own (with mine: personal), each over the last."""
    try:
        repo_values, hidden = variables.for_sandbox(a.repo, kind, env["SBX_OWNER"] if mine else "")
    except secret_store.StoreError as error:
        raise SandboxError(str(error)) from error
    owned = set()
    recipe = app_env(a, env, created=created, owned=owned)
    return {**recipe, **repo_values, **{key: recipe[key] for key in owned}}, hidden


def audience(metadata: dict | None) -> str:
    """Which of a repo's env values a sandbox gets (variables.AUDIENCES), as
    its creator tagged it: previews, sessions (by default an extension's) or
    other."""
    meta = metadata or {}
    return meta.get("audience") or ("sessions" if meta.get("connector") else "other")


def personal(metadata: dict | None) -> bool:
    """Whether its owner's own values reach it: not when its creator made
    it for others to drive under that owner's name (personal: "off")."""
    return (metadata or {}).get("personal") != "off"


def shared(metadata: dict | None) -> bool:
    """Whether its creator made it the team's: anyone signed in may open it."""
    return (metadata or {}).get("shared") == "team"


def runtime_layer(name: str, a: recipes.App, envs: dict, trusted: bool, metadata: dict | None,
                  routes: endpoints.Routes | None = None):
    """(the env with what extensions add, their runtime layers as one) for the
    sandbox; with routes, its env's endpoint targets localized and the
    forwarder's files (a fresh client certificate) among the layers."""
    routes = routes or endpoints.Routes()
    envs = routes.env(envs)
    target = extensions.Target(repo=a.repo, name=name, trusted=trusted, connector=(metadata or {}).get("connector", ""),
                               env=envs)
    found = extensions.contributions_for(target)
    forwarder = routes.contribution(name, a.repo)
    layer = contributions.merged_runtime([*found, *([forwarder] if forwarder else [])])
    return {**envs, **layer.env}, layer


def target_of(info) -> extensions.Target:
    """What contributions are for, for an existing sandbox (without its env)."""
    meta, a = info.metadata or {}, app_of(info)
    return extensions.Target(repo=a.repo, name=meta.get("sbx", ""), trusted=settings.trust(a.repo) == "trusted",
                             connector=meta.get("connector", ""))


def _start(a: recipes.App, env: dict, app_env: dict, public_keys, trusted: bool = True,
           hidden: set = frozenset(), operation=None, metadata=None, kind: str | None = None,
           routes: endpoints.Routes | None = None) -> dict:
    name, slug = env["SBX_NAME"], env["SBX_SLUG"]
    envs, layer = runtime_layer(name, a, _envs(env, app_env, kind), trusted, metadata, routes)
    # The cloud metadata service, denied per sandbox where the runtime
    # applies egress rules (one without them may deny it already). Private
    # ranges are the runtime's to enforce (Runtime.apply_network), not per sandbox.
    try:
        sbx = runtime.create((metadata or {})["template"], timeout=TIMEOUT_SECONDS, envs=envs, kind=kind,
                             deny_out=runtime.deny_out("169.254.169.254/32", kind=kind),
                             metadata={**(metadata or {}), "sbx": name, "owner": env["SBX_OWNER"],
                                       "branch": env["SBX_BRANCH"], "slug": slug, "repo": a.repo},
                             on_timeout="pause", auto_resume=True)
    except runtime.TemplateGone as error:
        # Deleted behind sbx's back (another installation's prune on a shared
        # account): built again, and the create answered as for one not built yet.
        from . import template_builds
        said = template_builds.gone(error.name, kind)
        if said is None:
            raise
        raise template_builds.NotBuilt(said) from error
    try:
        if operation:
            operation.advance("preparing_workspace", sandbox_id=sbx.sandbox_id, runtime="running")
        ensure_started(sbx)
        initialize_ssh_identity(sbx)
        host_key = write_runtime(sbx, name, envs, layer, public_keys, hidden)
    except Exception:
        if not operation:
            runtime.kill(sbx.sandbox_id)
        raise
    return {"name": name, "sandbox_id": sbx.sandbox_id, "host_key": " ".join(host_key), "branch": env["SBX_BRANCH"],
            "repo": a.workdir, "ssh_url": addressing.ssh_url(slug, a, sbx.sandbox_id), "urls": addressing.urls(slug, a)}


def ensure_started(sbx):
    """The start contract (platform.START) running: a VM booted from its disk
    or from a filesystem snapshot comes up without its saved processes."""
    if sbx.commands.run(platform.STARTED, user="user", timeout=15).stdout.strip() == "ready":
        return
    sbx.commands.run("DISPLAY=:0 xset q >/dev/null 2>&1 || rm -f /tmp/.X0-lock /tmp/.X11-unix/X0",
                     user="user", timeout=15)
    handle = sbx.commands.run(platform.START, user="user", background=True, timeout=0)
    handle.disconnect()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if sbx.commands.run(platform.STARTED, user="user", timeout=15).stdout.strip() == "ready":
            return
        time.sleep(0.25)
    raise SandboxError("the cold-booted template's SSH/desktop transport did not start")


def ship_guest(sbx):
    """Put the guest helper into the sandbox's runtime directory, which a
    cold boot empties; every use ships it, so it matches this release."""
    sbx.commands.run(f"sudo install -d -m 700 -o user -g user {RUN}", timeout=30)
    sbx.files.write(GUEST, GUEST_SOURCE.read_text(), user="user")


def guest(*args: str, sudo: bool = False) -> str:
    return ("sudo " if sudo else "") + shlex.join(["python3", GUEST, *args])


def initialize_ssh_identity(sbx):
    """A preparation template's existing host keys belong to no new sandbox:
    give each allocation its own, once."""
    identity = hashlib.sha256(sbx.sandbox_id.encode()).hexdigest()[:32]
    ship_guest(sbx)
    command = guest("identity", identity, "--ssh-port", str(platform.SSH_PORT), sudo=True)
    sbx.commands.run(f"umask 077; {command} > {RUN}/ssh-identity.log 2>&1", timeout=120)


def _envs(env: dict, app_env: dict, kind: str | None = None) -> dict:
    return {**app_env, **env, "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS": addressing.vite_allowed_hosts(kind)}


def write_runtime(sbx, name: str, envs: dict, layer: contributions.RuntimeLayer, public_keys=None,
                  hidden: set = frozenset()) -> list:
    """The sandbox's env and the extensions' runtime layers (files under
    contributions.EXT, git config, files shells source), in its tmpfs.
    Returns its SSH host key."""
    # Shells and agents don't get the provider API keys, so agents run on the
    # developer's subscription, nor what the repo's env gives only the app.
    hidden = set(settings.current().sandboxes.agent_hidden) | set(hidden)
    exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in envs.items() if k not in hidden)
    exports += "".join(f"if [ -r {RUN}/{f} ]; then . {RUN}/{f}; fi\n" for f in layer.sources)
    app_exports = f". {RUN}/env\n" + "".join(
        f"export {k}={shlex.quote(v)}\n" for k, v in envs.items() if k in hidden)
    staging = f"{RUN}/.runtime-staging"
    ext = f"{staging}/ext"
    # A failed publication must be retryable, including on envd versions that
    # open uploads as root before assigning ownership (Linux protected_regular).
    folders = sorted({str(pathlib.PurePosixPath(f"{ext}/{path}").parent) for path in layer.files})
    sbx.commands.run(f"sudo install -d -m 700 -o user -g user {RUN} {staging} && rm -rf {ext} "
                     f"&& mkdir -p -m 700 {shlex.join([ext, *folders])}", timeout=30)
    if public_keys is not None:
        sbx.files.write(f"{HOME}/.ssh/authorized_keys", public_keys or "\n", user="user")
    sbx.files.write(f"{staging}/sbx-env", exports, user="user")
    sbx.files.write(f"{staging}/sbx-app-env", app_exports, user="user")
    for path, f in layer.files.items():
        sbx.files.write(f"{ext}/{path}", f.content, user="user")
    modes = "".join(f"&& chmod {f.mode:o} {shlex.quote(f'{ext}/{path}')} " for path, f in layer.files.items())
    git = "".join(f"&& git config --global {shlex.quote(k)} {shlex.quote(v)} " for k, v in layer.git_config.items())
    dirs = "".join(f"&& mkdir -p -m 700 {shlex.quote(d)} " for d in layer.dirs)
    # A link replaces what is at its path: a file there from before (an
    # agent's login on disk) is gone, and the link points into the tmpfs.
    dirs += "".join(f"&& mkdir -p -m 700 {shlex.quote(str(pathlib.PurePosixPath(path).parent))} "
                    f"&& ln -sfn {shlex.quote(target)} {shlex.quote(path)} " for path, target in layer.links.items())
    host_key = sbx.commands.run(
        f"chmod 600 {staging}/sbx-env {staging}/sbx-app-env {modes}"
        f"&& mv {staging}/sbx-env {RUN}/env && mv {staging}/sbx-app-env {RUN}/app-env "
        f"&& rm -rf {contributions.EXT}.old && {{ [ ! -e {contributions.EXT} ] || mv {contributions.EXT} {contributions.EXT}.old; }} "
        f"&& mv {ext} {contributions.EXT} && rm -rf {contributions.EXT}.old {dirs}"
        f"&& chmod -R go-rwx {RUN} && mkdir -p {HOME}/.ssh && chmod 700 {HOME}/.ssh "
        f"&& touch {HOME}/.ssh/authorized_keys && chmod 600 {HOME}/.ssh/authorized_keys "
        f"{git}&& cat /etc/ssh/ssh_host_ed25519_key.pub").stdout.split()[:2]
    if f"{endpoints.FOLDER}/config.json" in layer.files:
        start_forwarder(sbx)
    return host_key


def start_forwarder(sbx):
    """The endpoints' forwarder (sbx_guest.py forward), started unless it
    runs; it follows its config's changes and stops once the config is gone."""
    ship_guest(sbx)
    config = f"{contributions.folder(endpoints.FOLDER)}/config.json"
    sbx.commands.run(f"{guest('forward', 'ensure', config)} >> /tmp/sbx-endpoints.log 2>&1", timeout=60)


def check_app(info) -> str | None:
    """For a running sandbox whose app was last seen ready: why its processes
    no longer run (the supervisor or a process gave up, often because /run
    filled), or None when they run or the recipe has none."""
    a = app_of(info)
    if not a.spec.get("processes"):
        return None
    sbx = runtime.connect(info.sandbox_id)
    if sbx.commands.run(f"{guest('processes', 'probe')} 2>/dev/null || true", timeout=30).stdout.strip() != "exited":
        return None
    return (run_space(sbx) or f"its processes are not running; see {sbx_guest.SUPERVISOR_LOG} and the process logs "
            "(sbx logs NAME)")


def runtime_values(info) -> tuple[dict, set, contributions.RuntimeLayer]:
    """(the sandbox's env as sbx-control would write it now, the names only
    its app gets, the extensions' runtime layers)."""
    meta, a = info.metadata or {}, app_of(info)
    name = meta["sbx"]
    env = sbx_env(name, slug_of(info), a, meta.get("owner", ""), meta.get("branch", ""), meta.get("template", ""))
    kind = runtime.kind_of(info)
    routes = endpoints.routes(kind, a.repo)
    if settings.trust(a.repo) != "trusted":
        envs, layer = runtime_layer(name, a, _envs(env, app_env(a, env, trusted=False), kind), False, meta, routes)
        return envs, set(), layer
    created = extensions.packages().refresh(name, a.repo)
    values, hidden = trusted_values(a, env, routes.outputs(created), audience(meta), personal(meta))
    envs, layer = runtime_layer(name, a, _envs(env, values, kind), True, meta, routes)
    return envs, hidden, layer


def restore_runtime(sbx, info) -> bool:
    """When the runtime had no room for a pause snapshot, it boots the
    sandbox again from its disk, and its tmpfs comes back empty: put the env,
    key and git credential back from what sbx-control keeps. True when it
    had to, or the recipe requests an app restart after resume."""
    meta = info.metadata if isinstance(info.metadata, dict) else {}
    if runtime_state(info) == "paused":
        extensions.on_resume(sbx, info)
    if sbx.commands.run(f"test -f {RUN}/env && echo ok || true", timeout=30).stdout.strip() == "ok":
        paused = runtime_state(info) == "paused"
        name = meta.get("sbx")
        pending = delivery.pending(name) if isinstance(name, str) and name else None
        if pending and paused:
            _, restart, revision = pending
            delivered = _refresh_env(sbx, info, restart)
            delivery.acknowledge(name, revision)
            state.event(name, "env updated", "queued changes applied")
            if delivered["restarted"]:
                return False
        return paused and app_of(info).resume_mode == "restart"
    # Booted from its disk, or an allocation whose creation failed part way.
    ensure_started(sbx)
    initialize_ssh_identity(sbx)
    name = (info.metadata or {})["sbx"]
    envs, hidden, layer = runtime_values(info)
    write_runtime(sbx, name, envs, layer, hidden=hidden)
    state.event(name, "runtime restored", "its tmpfs was empty (likely: booted from its disk, as the host had "
                                          "no room for its pause snapshot), so its env and credentials were gone",
                internal=True)
    return True


def connect(name: str, why: str, info=None):
    """Connecting resumes a paused VM."""
    info = info or find(name)
    if not info:
        raise SandboxError(f"no sbx-{name}")
    lifecycle.assert_binding(name, info.sandbox_id)
    paused = runtime_state(info) == "paused"
    began = time.time()
    try:
        sbx = runtime.connect(info.sandbox_id, timeout=TIMEOUT_SECONDS)
    except Exception as error:
        if paused:
            state.event(name, "resume failed", f"{why}, after {time.time() - began:.0f}s: {error}")
        raise
    if paused:
        lifecycle.observe(name, info.sandbox_id, "running")
        state.event(name, "resumed", f"{why}, in {time.time() - began:.0f}s")
        try:   # its endpoint certificates may have run out while it slept
            endpoints.push(sbx, info)
        except Exception as error:   # the forwarder pulls them itself then
            state.event(name, "endpoint certificates not pushed", type(error).__name__, internal=True)
    return sbx, info


_attaching: dict = {}  # name -> lock: one resume of a sandbox at a time in this process


class Attached:
    """A sandbox's running VM, for what works in it (an extension, the API's
    exec): commands with its shells' env, files, its checkout, the recipe.
    core.attach hands it out."""

    def __init__(self, name: str, sbx, a: recipes.App):
        self.name, self.sbx, self.app = name, sbx, a

    @property
    def sandbox_id(self) -> str:
        return self.sbx.sandbox_id

    def run(self, script: str, timeout: int = 60):
        """script with the env its SSH shells get; raises on a nonzero exit."""
        return run_in(self.sbx, script, timeout=timeout)

    def write(self, path: str, content, **kwargs):
        self.sbx.files.write(path, content, **kwargs)

    def spawn(self, command: str, envs: dict):
        """command in the background as the sandbox's user, left running."""
        self.sbx.commands.run(command, user="user", envs=envs, background=True, timeout=0).disconnect()

    def checkout(self, rev: str):
        """The repo at rev, detached (a SHA or a pull ref), cloned first when
        the template has no checkout."""
        ensure_checkout(self.sbx, self.app)
        self.run(f"cd {shlex.quote(self.app.workdir)} && git fetch -q origin {shlex.quote(rev)} "
                 "&& git checkout -q --detach FETCH_HEAD", timeout=300)

    def ship_recipe(self):
        """The recipe's .sbx/ at platform.APP_SCRIPTS, as this release reads it."""
        recipes.ship(self.sbx, self.app, APP_SCRIPTS)

    def ship_guest(self):
        ship_guest(self.sbx)

    @property
    def logs(self) -> list:
        """Where the app's setup, start script and processes write."""
        return app_logs(self.app)

    def restart_command(self) -> str:
        """What a shell in the sandbox runs to restart the recipe's processes
        as last started; "" when the recipe has none."""
        return restart_processes(self.app, f'"$(cat {RUN}/processes/spec.json)"') if self.app.spec.get("processes") else ""


def verify_ready(sbx, info):
    """The ready check of what the extension that made the sandbox
    contributes (metadata connector): NotReady when its template lacks it."""
    connector = extensions.connector_of(info)
    c = connector.contribution(target_of(info)) if connector else None
    # Judged by its exit status: a check may print (command -v does).
    if c and c.ready and run_in(sbx, f"{{ {c.ready.command}; }} >/dev/null 2>&1 && echo ready || true",
                                timeout=c.ready.timeout).stdout.strip() != "ready":
        raise NotReady(f"this sandbox's template has no working {c.name} (its ready check failed); "
                       "build its template again")


def attach(name: str, why: str, *, info=None, run_app: bool = True, wait: bool = True,
           ready: bool = False) -> Attached:
    """The sandbox's VM, ready for work in it. Its runtime comes back if it
    booted from its disk; then, or when the recipe's lifecycle.resume is
    restart, its app restarts (unless not run_app): before this returns, or
    on a thread when not wait. With ready, what the extension that made the
    sandbox contributes is verified to be in it (verify_ready)."""
    with _attaching.setdefault(name, threading.Lock()):
        if info is None or runtime_state(info) == "paused":
            info = find(name)  # another caller may have resumed it meanwhile
        try:
            sbx, info = _attach(name, why, info, run_app, wait)
        except runtime.NotRunning:
            # The runtime paused or deleted it since it was listed: as it is now.
            sbx, info = _attach(name, why, find(name), run_app, wait)
    if ready:
        verify_ready(sbx, info)
    return Attached(name, sbx, app_of(info))


def _attach(name, why, info, run_app, wait):
    if not info:
        raise SandboxError(f"no sbx-{name}")
    if runtime_state(info) == "paused":
        return _resume(name, why, info, run_app, wait)
    sbx, info = connect(name, why, info)
    if restore_runtime(sbx, info) and run_app and app_of(info).repo:
        if wait:
            restart(name)
        else:
            lifecycle.in_background(name, "restart", lambda operation: restart(name, operation))
    return sbx, info


def _resume(name, why, info, run_app, wait):
    a = app_of(info)
    with lifecycle.operation(name, "resume") as operation:
        operation.configure_workspace(a.repo)
        operation.advance("restoring_runtime")
        sbx, info = connect(name, why, info)
        restored = restore_runtime(sbx, info)
        if restored:
            operation.invalidate_application()
        start_now = restored and run_app and bool(a.repo)
        if start_now and wait:
            _bring_up(sbx, info, operation)
        else:
            if not lifecycle.get(name)["workspace_ready"]:
                with masked(info):
                    _prepare(sbx, info, operation)
            operation.advance(lifecycle.settled(lifecycle.get(name)), finish=True, runtime="running")
    if start_now and not wait:
        lifecycle.in_background(name, "restart", lambda operation: restart(name, operation))
    return sbx, info


ENV_SPLIT = "--sbx-app-env--"


def written_env(sbx) -> dict:
    text = sbx.commands.run(f"cat {RUN}/env; echo {ENV_SPLIT}; cat {RUN}/app-env 2>/dev/null || true",
                            timeout=30).stdout
    # Whole-file tokens: a quoted value may span lines (a key file's).
    tokens, out = shlex.split(text), {}
    for word, pair in zip(tokens, tokens[1:]):
        key, sep, value = pair.partition("=")
        if word == "export" and sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            out[key] = value
    return out


def refresh_env(name: str, restart: bool = True) -> dict:
    """Connecting resumes a paused sandbox: callers pass only running ones."""
    with lifecycle.operation(name, "env_update") as operation:
        sbx, info = connect(name, "to deliver env changes")
        with masked(info):
            _prepare(sbx, info, operation)
        result = _refresh_env(sbx, info, restart, operation=operation)
        operation.advance(lifecycle.settled(lifecycle.get(name)), finish=True)
        return result


def _refresh_env(sbx, info, restart, operation=None):
    with masked(info):
        return _write_env(sbx, info, restart, operation)


def _write_env(sbx, info, restart, operation):
    name = (info.metadata or {})["sbx"]
    envs, hidden, layer = runtime_values(info)
    before = written_env(sbx)
    differs = sorted(k for k in set(before) | set(envs) if before.get(k) != envs.get(k))
    must_restart = bool(app_of(info).repo and restart and (differs or delivery.restart_required(name)))
    if must_restart:
        delivery.restart_required(name, mark=True)
    write_runtime(sbx, name, envs, layer, hidden=hidden)
    if must_restart:
        _run_app(sbx, info, operation)
    return {"changed": differs, "restarted": must_restart}


def slug_of(info) -> str:
    return (info.metadata or {}).get("slug") or (info.metadata or {}).get("sbx")


def _prepare(sbx, info, operation, *, branch: str | None = None, checkout: bool = False, force: bool = False):
    """The workspace stage of every operation that brings a sandbox up: its
    checkout usable (workspace_ready), its services' checkpoint passed."""
    operation.advance("preparing_workspace", sandbox_id=info.sandbox_id, runtime="running")
    prepare_workspace(sbx, app_of(info), branch or branch_of(info), checkout=checkout, force=force)
    operation.advance("workspace_ready")
    if lifecycle.get(operation.name)["services_ready"]:
        operation.advance("services_ready")


def _run_app(sbx, info, operation=None, *, setup: bool = False, finish: bool = False):
    """The app stage: the recipe shipped, .sbx/setup run when asked, the app
    started and the desktop's browser opened on it; with an operation,
    through its phases."""
    a = app_of(info)
    recipes.ship(sbx, a, APP_SCRIPTS)
    if setup:
        operation.advance("setting_up")
        app_script(sbx, "setup", a, env=f"{RUN}/app-env", timeout=1200, operation=operation)
    if operation:
        operation.advance("starting_app")
    start_app(sbx, a)
    open_desktop_browser(sbx, a, addressing.urls(slug_of(info), a).get("web"))
    if operation:
        operation.advance(lifecycle.started(bool(a.health)), finish=finish)


def mask(text: str, info) -> str:
    """text (a sandbox's logs, an error from it) with every value the
    sandbox gets hidden, for whoever may view it: the platform's, the
    installation's, its repo's for its kind, its owner's own, the ones it
    generated (${random:}) and the env extensions add to its runtime. When
    those can't be read, none of text is shown."""
    meta, a = info.metadata or {}, app_of(info)
    name = meta.get("sbx", "")
    try:
        trusted = settings.trust(a.repo) == "trusted"
        known = dict(variables.generated(name)) if name else {}
        if trusted:
            known.update(variables.for_sandbox(a.repo, audience(meta), meta.get("owner", ""))[0])
        target = extensions.Target(repo=a.repo, name=name, trusted=trusted, connector=meta.get("connector", ""))
        known.update(contributions.merged_runtime(extensions.contributions_for(target)).env)
        return variables.mask(text, a.repo, known)
    except Exception as error:
        return f"(hidden: the values to mask in it could not be read: {type(error).__name__})"


@contextlib.contextmanager
def masked(info):
    """Errors from work in the sandbox carry its commands' output, which its
    events and its viewers get: raised again with mask() applied."""
    try:
        yield
    except lifecycle.Conflict:
        raise
    except Exception as error:
        raise (type(error) if isinstance(error, SandboxError) else SandboxError)(mask(str(error), info)) from error


def _bring_up(sbx, info, operation, *, branch: str | None = None, checkout: bool = False, force: bool = False,
              setup: bool = False):
    """Workspace, then app: what resume, prepare and restart share."""
    with masked(info):
        _prepare(sbx, info, operation, branch=branch, checkout=checkout, force=force)
        _run_app(sbx, info, operation, setup=setup, finish=True)


def prepare_and_start(name: str, branch: str, checkout=False, operation=None, *, force: bool = False):
    """Runs in the background after create. checkout: move the checkout to
    branch's head first, which refuses a tree with local edits unless force
    (a sandbox nobody edits in, as a PR preview)."""
    with lifecycle.operation(name, "prepare", operation) as current, explained(name):
        return _prepare_and_start(name, branch, checkout, force, current)


def _prepare_and_start(name, branch, checkout, force, operation):
    sbx, info = connect(name, "to prepare it")
    a = app_of(info)
    operation.configure_workspace(a.repo)
    _refresh_env(sbx, info, False)
    with masked(info):
        apply_resources(sbx, a)
    _bring_up(sbx, info, operation, branch=branch, checkout=checkout, force=force, setup=True)


def ensure_checkout(sbx, a: recipes.App):
    """Clone the repo when the template has no checkout of it, as the
    default template has none."""
    root = shlex.quote(a.workdir)
    run_in(sbx, f"[ -e {root}/.git ] || git clone -q {shlex.quote(recipes.remote(a.repo))} {root}", timeout=900)


def prepare_workspace(sbx, a: recipes.App, branch: str, *, checkout: bool, force: bool = False,
                      source_commit: str | None = None):
    """checkout: move the checkout to branch's head (or source_commit). A
    tree with local edits is refused, naming them, unless force, which
    discards them."""
    root, ref = shlex.quote(a.workdir), shlex.quote(branch)
    if checkout:
        ensure_checkout(sbx, a)
        if source_commit is not None and not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", source_commit):
            raise SandboxError("source_commit must be a full Git commit hash")
        target = "--detach" if source_commit is None and (re.fullmatch(r"[0-9a-fA-F]{40,64}", branch) or branch.startswith("refs/")) else f"-B {ref}"
        selected = shlex.quote(source_commit or branch)
        if not force:
            edits = run_in(sbx, f"cd {root} && git status --porcelain --untracked-files=no", timeout=60).stdout.strip()
            if edits:
                listed = "\n".join(edits.splitlines()[:40])
                raise SandboxError(f"{a.workdir} has local edits, so it was not moved to {source_commit or branch}; "
                                   f"commit, stash or discard them first:\n{listed}")
        run_in(sbx, f"cd {root} && git fetch -q origin {selected} "
                    f"&& git checkout -q {'-f ' if force else ''}{target} FETCH_HEAD", timeout=300)
    run_in(sbx, f"cd {root} && git rev-parse --verify HEAD", timeout=30)


def apply_resources(sbx, a: recipes.App):
    swap = a.swap_mb
    if swap is not None:
        ship_guest(sbx)
        run_in(sbx, guest("swap", "--mb", str(swap), sudo=True), timeout=120)


def app_script(sbx, script: str, a: recipes.App, env=f"{RUN}/env", timeout=60, operation=None, process_scope=None):
    """Run one of the recipe's scripts: the shipped copy when it is the
    operator's, else the checkout's .sbx/ (a branch may change its scripts)."""
    source = f"d={APP_SCRIPTS}" if a.operator else f"d=.sbx; [ -d .sbx ] || d={APP_SCRIPTS}"
    scope = f"export {sbx_guest.START_SCOPE}={shlex.quote(process_scope)}; " if process_scope else ""
    command = (f"cd {shlex.quote(a.workdir)} && {{ {source}; }} "
               f"&& {{ {scope}true; }} "
               f'&& {{ [ ! -x "$d/{script}" ] || "$d/{script}"; }}')
    if operation and script == "setup":
        operation.check()
        command = f"umask 077; exec > {sbx_guest.SETUP_LOG} 2>&1; " + command
        command = f"flock -n {shlex.quote(SETUP_LOCK)} bash -c {shlex.quote(command)}"
    return run_in(sbx, command, timeout=timeout, env=env)


def start_processes(sbx, a: recipes.App) -> str:
    processes = a.spec.get("processes") or {}
    ship_guest(sbx)
    run_in(sbx, restart_processes(a, shlex.quote(json.dumps(processes))))
    return guest("processes", "probe") if processes else ""


def restart_processes(a: recipes.App, spec: str) -> str:
    """The command that (re)starts the recipe's processes; spec is a shell
    word that expands to their JSON."""
    return f'{guest("processes", "restart", "--workdir", a.workdir)} --spec {spec}'


def app_logs(a: recipes.App) -> list:
    return [sbx_guest.SETUP_LOG, *(a.spec.get("logs") or []),
            *(sbx_guest.log_path(name) for name in a.spec.get("processes") or {})]


def start_app(sbx, a: recipes.App):
    """Start the app: its processes, then .sbx/start. Wait for every health
    URL to answer below 400. start prints the pid of its main process; that
    process exiting, or a process failing, ends the wait early."""
    probe_processes = start_processes(sbx, a)
    scope = uuid.uuid4().hex
    sbx.files.write(f"{RUN}/{sbx_guest.START_SCOPE_FILE}", json.dumps({"scope": scope}))
    pid = (app_script(sbx, "start", a, env=f"{RUN}/app-env", timeout=120, process_scope=scope).stdout.split() or [""])[-1]
    probe = "".join(f"curl -s -m3 -o /dev/null -w '%{{http_code}} ' {shlex.quote(url)}; " for url in a.health)
    if pid.isdigit():
        probe += f"kill -0 {pid} 2>/dev/null || echo exited; "
    probe += probe_processes or "true"
    deadline = time.time() + a.spec.get("health_timeout", 240)
    while time.time() < deadline:
        time.sleep(1)
        try:
            state = run_in(sbx, probe).stdout.split()
        except Exception:  # a probe can fail while the dev servers boot; ask again
            continue
        if "exited" in state[len(a.health):]:
            break
        if len(state) >= len(a.health) and all(code[:1] in ("2", "3") for code in state[:len(a.health)]):
            return
    logs = shlex.join([*app_logs(a), sbx_guest.SUPERVISOR_LOG])
    tail = sbx.commands.run(f"grep -hE 'ERROR|Error|error' {logs} 2>/dev/null | grep -v '^ *at ' | tail -15").stdout
    raise SandboxError(f"the app did not come up:\n{tail}")   # its callers mask it (masked)


def open_desktop_browser(sbx, a: recipes.App, web_url):
    if not web_url or "-desktop" not in addressing.previews(a):
        return
    sbx.commands.run("[ -f /tmp/chromium.pid ] && kill -- -$(cat /tmp/chromium.pid) 2>/dev/null; "
                     "DISPLAY=:0 setsid nohup chromium --no-first-run "
                     "--no-default-browser-check --disable-dev-shm-usage --start-maximized "
                     f"--user-data-dir={HOME}/.config/chromium-desktop {shlex.quote(web_url)} "
                     ">/tmp/chromium.log 2>&1 < /dev/null & echo $! > /tmp/chromium.pid")


# A sandbox's /run is a tmpfs of a fifth of its memory: sbx's env, the
# shipped recipe and the process supervisor's state live there, and what
# fills it (an agent's files) stops the supervisor and fails every write.
RUN_FULL_PERCENT = 95
NO_SPACE = ("No space left on device", "not enough disk space")


def run_space(sbx) -> str | None:
    """Why the sandbox's /run refuses writes, with what fills it; None while it has room."""
    # Two du runs: one du counts a file once, so /run/sbx/* after /run would read empty.
    out = sbx.commands.run("df -Pk /run | tail -1; { du -xsk /run/* 2>/dev/null; du -xsk /run/sbx/* 2>/dev/null; } "
                           "| sort -n | tail -4", timeout=30).stdout.split("\n")
    fields = out[0].split()
    try:
        size_kb, used_percent = int(fields[1]), int(fields[4].rstrip("%"))
    except (IndexError, ValueError):
        return None
    if used_percent < RUN_FULL_PERCENT:
        return None
    largest = [line.split("\t") for line in out[1:] if "\t" in line]
    biggest = ", ".join(f"{path} {int(kb) // 1024} MB" for kb, path in reversed(largest)
                        if path != "/run/sbx" and kb.isdigit() and int(kb) >= 1024)
    return (f"the sandbox's /run is full ({used_percent}% of {size_kb // 1024} MB, a tmpfs in its memory that holds "
            f"sbx's env and its process supervisor's state); largest there: {biggest or 'unknown'}. Remove what an "
            "agent left there, or create the sandbox again")


@contextlib.contextmanager
def explained(name: str, info=None):
    """A failed write says when the sandbox's /run is full and what fills it."""
    try:
        yield
    except Exception as error:
        if not any(text in str(error) for text in NO_SPACE):
            raise
        try:
            info = info or find(name)
            why = run_space(runtime.connect(info.sandbox_id)) if info else None
        except Exception:  # the explanation is best effort; the failure stands
            why = None
        if why:
            raise SandboxError(f"{error}: {why}") from error
        raise


def restart(name: str, operation=None):
    with lifecycle.operation(name, "restart", operation) as current, explained(name):
        return _restart(name, current)


def _restart(name, operation):
    sbx, info = connect(name, "to restart its app")
    restore_runtime(sbx, info)
    operation.configure_workspace(app_of(info).repo)
    _bring_up(sbx, info, operation)


def delete(name: str, slug: str | None = None):
    """The VM and its services. slug: its preview host as registered, for
    when the VM is gone already. A sandbox deleted already (no VM left) is
    left as it is."""
    current = lifecycle.get(valid_name(name))
    if current and current["phase"] == "deleted" and not current["active"]:
        info = find(name)  # the runtime lists a killed VM for a while
        if info:
            runtime.kill(info.sandbox_id)
        return
    with lifecycle.operation(name, "delete") as operation:
        operation.advance("deleting")
        _delete(name, slug, operation)
        operation.advance("deleted", finish=True)


def _delete(name, slug, operation):
    info = find(name)
    if info:
        lifecycle.assert_binding(name, info.sandbox_id)
        runtime.kill(info.sandbox_id)  # connect would resume a paused one first
        slug = slug_of(info)
    operation.advance("deleting", runtime="missing")
    # What delete_all can't remove stays recorded for the reconciler.
    try:
        extensions.packages().release(name)
    finally:
        ip_previews.remove(slug or name)


def _pause_vm(info):
    """Pause the VM, retrying while the runtime says it is busy; the last
    refusal when it never took the pause, else None."""
    for wait in (*PAUSE_RETRY_SECONDS, None):
        try:
            runtime.pause(info.sandbox_id)
            return None
        except runtime.Busy as error:
            if wait is None:
                return error
            time.sleep(wait)


def pause_refusal(info, error) -> str:
    """Why the runtime would not pause the sandbox, in words a person can act on."""
    tries = len(PAUSE_RETRY_SECONDS) + 1
    retries = f"{tries} tries over {sum(PAUSE_RETRY_SECONDS)} s" if tries > 1 else "once"
    cause = runtime.why_busy(getattr(info, "memory_mb", None), runtime.kind_of(info))
    return (f"the runtime refused the pause ({retries}): {cause}. The sandbox keeps running. "
            f"Runtime: {str(error)[-300:]}")


def pause(name: str, info=None, *, before_pause=None):
    """Pause the VM, then run its services' pause hooks while the operation
    still holds the sandbox (nothing resumes it meanwhile). In a running VM,
    the extension that made it stops what its snapshot must not keep
    (Connector.before_pause), then before_pause runs (a connector's checkpoint)."""
    with lifecycle.operation(name, "pause") as operation:
        info = info or find(name)
        if not info:
            raise SandboxError(f"no sbx-{name}")
        lifecycle.assert_binding(name, info.sandbox_id)
        before = lifecycle.get(name)["previous_phase"]
        operation.advance("pausing")
        if runtime_state(info) == "running":   # a paused VM is left as it is, not woken
            extensions.before_pause(info)
        if before_pause:
            before_pause()
        refused = _pause_vm(info)
        if refused:
            now = find(name)
            if runtime_state(now) != "running":
                raise refused  # the VM did not survive the refusal: the operation fails
            why = pause_refusal(info, refused)
            operation.advance(before if before in lifecycle.TERMINAL - {"deleted"} else "unknown",
                              finish=True, sandbox_id=info.sandbox_id, runtime="running")
            state.event(name, "pause refused", why)
            raise PauseRefused(why)
        for failed in extensions.packages().paused(name):
            state.event(name, "service pause hook deferred", failed, internal=True)
        operation.advance(before if before in lifecycle.TERMINAL - {"deleted"} else "unknown",
                          finish=True, sandbox_id=info.sandbox_id, runtime="paused")
