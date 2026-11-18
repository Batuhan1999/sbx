"""Create and manage repository workspaces, injected env, checkout, and app processes.

What extensions put into a sandbox's runtime comes from extensions.py as
contributions; this module writes them and never names an extension.
Personal agent credentials travel directly from the desktop to the sandbox
and are not stored by the control service."""
import contextlib
import copy
import dataclasses
import fcntl
import json
import os
import re
import shlex
import threading
import time
import uuid

from . import (addressing, contributions, extensions, ip_previews, lifecycle, paths, platform, recipes,
               runtime, sandbox_layers, secret_store, settings, state, variables)
# The runtime's half (sandbox_runtime): writing it, putting it back, the guest helper, the recipe's scripts.
from .sandbox_runtime import (ENV_SPLIT, GUEST, GUEST_SOURCE, SETUP_LOCK, _envs, app_env_values,  # noqa: F401
                              app_script, ensure_started, guest, has_app_script, initialize_ssh_identity,
                              restore_runtime, runtime_layer,
                              runtime_values, ship_guest, write_env, write_runtime, written_env)
from .template_files import sbx_guest

RUN = platform.RUN
HOME = platform.HOME
APP_SCRIPTS = platform.APP_SCRIPTS
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
# The recipe's .sbx/cleanup runs before a sandbox is deleted (run_cleanup):
# this long at most, then the delete goes on without it. What it printed is
# kept to its last characters.
CLEANUP_TIMEOUT_SECONDS = 300
CLEANUP_OUTPUT_CHARS = 4000


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
            and port not in runtime.ports() and port not in extensions.reserved_ports()
            and port not in sandbox_layers.reserved_ports())


def listening_ports(listening: str) -> list[tuple[str, int]]:
    """(address, port) of each socket in `ss -Hltn`'s lines."""
    out = []
    for line in listening.splitlines():
        fields = line.split()
        address, _, port = fields[3].rpartition(":") if len(fields) > 3 else ("", "", "")
        if port.isdigit():
            out.append((address.strip("[]").partition("%")[0].removeprefix("::ffff:"), int(port)))
    return out


def detected_ports(listening: str, a: recipes.App) -> dict:
    """port -> whether it listens on loopback only, from `ss -Hltn`. A runtime's
    forwarder may reset connections to loopback-only ports under load, so those
    are reported but not exposed: the server has to listen on 0.0.0.0."""
    out = {}
    for address, port in listening_ports(listening):
        if detectable(port, a):
            loopback = address.startswith("127.") or address in ("::1", "localhost")
            out[port] = out.get(port, True) and loopback
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


def run_in(sbx, script, timeout=60, env=f"{RUN}/env", env_required=False):
    """Run with the sandbox's env as SSH shells and agents see it, or with
    the app's (env=f"{RUN}/app-env", which adds the provider keys).
    env_required: fail, without running script, when env can't be loaded."""
    load = f". {env} || {{ echo 'sbx: {env} could not be loaded' >&2; exit 1; }}; " if env_required else f". {env}; "
    return sbx.commands.run(f"bash -c {shlex.quote(load + script)}", timeout=timeout)


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
                            operation=operation, metadata=metadata, kind=kind)
        else:
            if operation:
                operation.advance("provisioning_services")
            write = sandbox_layers.Write(name, a.repo, kind)
            created = sandbox_layers.outputs(write, create_services(a, env))
            values, hidden = trusted_values(a, env, created, audience(metadata), personal(metadata))
            if operation:
                operation.advance("services_ready")
                operation.advance("allocating")
            result = _start(a, env, values, public_keys, hidden=hidden,
                            operation=operation, metadata=metadata, kind=kind, write=write)
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


def target_of(info) -> extensions.Target:
    """What contributions are for, for an existing sandbox (without its env)."""
    meta, a = info.metadata or {}, app_of(info)
    return extensions.Target(repo=a.repo, name=meta.get("sbx", ""), trusted=settings.trust(a.repo) == "trusted",
                             connector=meta.get("connector", ""))


def _start(a: recipes.App, env: dict, app_env: dict, public_keys, trusted: bool = True,
           hidden: set = frozenset(), operation=None, metadata=None, kind: str | None = None,
           write: sandbox_layers.Write | None = None) -> dict:
    name, slug = env["SBX_NAME"], env["SBX_SLUG"]
    write = write or sandbox_layers.Write(name, a.repo, kind)
    envs, layer = runtime_layer(name, a, _envs(env, app_env, kind), trusted, metadata, write)
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
        ensure_started(sbx, kind)
        initialize_ssh_identity(sbx)
        host_key = write_runtime(sbx, name, envs, layer, public_keys, hidden, write=write)
    except Exception:
        if not operation:
            runtime.kill(sbx.sandbox_id)
        raise
    return {"name": name, "sandbox_id": sbx.sandbox_id, "host_key": " ".join(host_key), "branch": env["SBX_BRANCH"],
            "repo": a.workdir, "ssh_url": addressing.ssh_url(slug, a, sbx.sandbox_id), "urls": addressing.urls(slug, a)}


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
        sandbox_layers.on_resume(sbx, info)
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
    if runtime_state(info) == "running" and _paused_by_us(name, info):
        # A provider's listing can lag its pause by seconds (E2B): sbx's own record wins, so this
        # is a resume (the app restarted when the recipe asks, the certificates pushed), not a
        # connect that wakes the VM unseen.
        info = dataclasses.replace(info, state="paused") if dataclasses.is_dataclass(info) else copy.copy(info)
        if not dataclasses.is_dataclass(info):
            info.state = "paused"
    if runtime_state(info) == "paused":
        return _resume(name, why, info, run_app, wait)
    sbx, info = connect(name, why, info)
    if restore_runtime(sbx, info) and run_app and app_of(info).repo:
        if wait:
            restart(name)
        else:
            lifecycle.in_background(name, "restart", lambda operation: restart(name, operation))
    return sbx, info


def _paused_by_us(name: str, info) -> bool:
    """Whether sbx paused this very VM last (its lifecycle record) and saw no resume since."""
    record = lifecycle.get(name) or {}
    return record.get("runtime") == "paused" and record.get("sandbox_id") == info.sandbox_id


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
        return write_env(sbx, info, restart, operation)


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


def mask(text: str, info, used: dict | None = None) -> str:
    """text (a sandbox's logs, an error from it) with every value the
    sandbox gets hidden, for whoever may view it: the platform's, the
    installation's, its repo's for its kind, its owner's own, the ones it
    generated (${random:}) and the env extensions add to its runtime, and
    used: values read from the sandbox itself. When those can't be read,
    none of text is shown."""
    meta = info.metadata or {}
    name = meta.get("sbx", "")
    try:
        a = app_of(info)
        trusted = settings.trust(a.repo) == "trusted"
        known = dict(variables.generated(name)) if name else {}
        if trusted:
            known.update(variables.for_sandbox(a.repo, audience(meta), meta.get("owner", ""))[0])
        target = extensions.Target(repo=a.repo, name=name, trusted=trusted, connector=meta.get("connector", ""))
        known.update(contributions.merged_runtime(extensions.contributions_for(target)).env)
        known.update(used or {})
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
    seconds = a.spec.get("health_timeout", 240)
    deadline, state = time.time() + seconds, []
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
    raise SandboxError(f"the app did not come up: {health_said(a.health, state, seconds)}\n{tail}")   # its callers mask it (masked)


def health_said(urls: list, state: list, seconds) -> str:
    """Why start_app gave up, from the last probe: a process exited, or what
    each health URL answered (curl's 000: no answer)."""
    if "exited" in state[len(urls):]:
        return "a process of the app exited"
    codes = [*state[:len(urls)], *[""] * len(urls)]
    return f"after {seconds} s, " + ", ".join(
        f"{url} " + (f"answered {code}" if code.strip("0") else "did not answer") for url, code in zip(urls, codes))


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
    if not restore_runtime(sbx, info):
        # Its runtime as it would be written now (the env, what the sandbox layers add), before the app starts.
        _refresh_env(sbx, info, False)
    operation.configure_workspace(app_of(info).repo)
    _bring_up(sbx, info, operation)


def delete(name: str, slug: str | None = None) -> dict | None:
    """The VM and its services, after the recipe's .sbx/cleanup (run_cleanup:
    its outcome, or None). slug: its preview host as registered, for when the
    VM is gone already. A sandbox deleted already (no VM left) is left as it is."""
    current = lifecycle.get(valid_name(name))
    if current and current["phase"] == "deleted" and not current["active"]:
        info = find(name)  # the runtime lists a killed VM for a while
        if info:
            runtime.kill(info.sandbox_id)
        return None
    with lifecycle.operation(name, "delete") as operation:
        operation.advance("deleting")
        cleanup = _delete(name, slug, operation)
        operation.advance("deleted", finish=True)
    return cleanup


def _delete(name, slug, operation):
    info, cleanup = find(name), None
    if info:
        lifecycle.assert_binding(name, info.sandbox_id)
        cleanup = run_cleanup(name, info, operation)
        runtime.kill(info.sandbox_id)  # connect would resume a paused one first
        slug = slug_of(info)
    operation.advance("deleting", runtime="missing")
    # What delete_all can't remove stays recorded for the reconciler.
    try:
        extensions.packages().release(name)
    finally:
        ip_previews.remove(slug or name)
    return cleanup


def run_cleanup(name: str, info, operation=None) -> dict | None:
    """The recipe's .sbx/cleanup in the sandbox, before its VM is deleted:
    for what the app made outside the sandbox (a webhook, a cloud resource).
    It runs from the workdir with the app's env, like setup; a paused sandbox
    is resumed for it, its env put back but its app not started. Best
    effort: a failure, a timeout (CLEANUP_TIMEOUT_SECONDS), a hook the
    sandbox's checkout lacks or a resume that fails is recorded as the
    sandbox's event and never stops the delete. None when the recipe sbx
    reads has no cleanup; else {"exit_code", "output"} or {"error"}, masked."""
    try:
        a = app_of(info)
    except Exception:   # its sbx.yaml is there but can't be read: whether it has a hook is unknown
        outcome = {"error": "the recipe's sbx.yaml could not be read, so .sbx/cleanup did not run"}
        state.event(name, "cleanup failed", f"{outcome['error']}; deleting the sandbox anyway")
        return outcome
    hook = a.dir / "cleanup"
    if not (a.has_recipe and hook.is_file()):
        return None
    if not os.access(hook, os.X_OK):
        outcome = {"error": ".sbx/cleanup is not executable, so it did not run"}
        state.event(name, "cleanup skipped", outcome["error"])
        return outcome
    paused = runtime_state(info) == "paused"
    try:
        sbx, info = connect(name, "to run .sbx/cleanup before it is deleted", info)
        # A sandbox booted from its disk gets its env back; its app, which the delete stops, is not started.
        try:
            restored = restore_runtime(sbx, info, start_app=False)
        except Exception:
            if operation:   # it may have stopped the app part way
                operation.invalidate_application()
            raise
        if restored and operation:
            operation.invalidate_application()
        recipes.ship(sbx, a, APP_SCRIPTS)
        if not has_app_script(sbx, "cleanup", a):
            outcome = {"error": "the sandbox's checkout has a .sbx/ without an executable cleanup, so it did not run"}
            state.event(name, "cleanup skipped", outcome["error"])
            return outcome
        used = app_env_values(sbx)   # what the hook gets, should the store have changed since
        try:
            result = app_script(sbx, "cleanup", a, env=f"{RUN}/app-env", timeout=CLEANUP_TIMEOUT_SECONDS,
                                env_required=True)
        except runtime.CommandFailed as failed:
            result = failed
        output = "".join(filter(None, (result.stdout, result.stderr)))
        outcome = {"exit_code": result.exit_code, "output": _cleanup_output(output, info, used)[-CLEANUP_OUTPUT_CHARS:]}
    except Exception as error:
        why = f"{'resuming the sandbox or ' if paused else ''}running it failed: {error}"
        outcome = {"error": mask(why, info)[-CLEANUP_OUTPUT_CHARS:]}
    if "error" in outcome:
        state.event(name, "cleanup failed", f"{outcome['error']}; deleting the sandbox anyway")
    else:
        tail = outcome["output"].strip()[-1500:]
        state.event(name, "cleanup ran" if outcome["exit_code"] == 0 else "cleanup failed",
                    f"exit {outcome['exit_code']}" + (f": {tail}" if tail else ""))
    return outcome


def _cleanup_output(output: str, info, used: dict | None) -> str:
    """The hook's output masked with the values it ran with as well; none of
    it when those can't be read, or when it holds a secret too short for
    mask() to hide."""
    if used is None:
        return "(hidden: the env it ran with could not be read to mask its output)"
    masked = mask(output, info, used)
    meta = info.metadata or {}
    try:
        # The installation's too: a repo's or owner's secret of the same name shadows it here, not in the env.
        scopes = (variables.secret_values(None), variables.secret_values(app_of(info).repo, meta.get("owner", "")))
    except Exception:
        return "(hidden: the values to mask in it could not be read)"
    short = {v for secret in scopes for v in (*secret.values(), *(used[k] for k in secret if k in used))
             if 0 < len(v) < 6}
    if any(v in masked for v in short):
        return "(hidden: it may hold a secret too short to mask)"
    return masked


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
