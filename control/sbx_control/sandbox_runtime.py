"""A sandbox's runtime, written by sbx-control: the guest helper, the start
contract and SSH identity of a booted VM, the env and the runtime layers in
its tmpfs (what extensions contribute and the sandbox layers add), put back
when a cold boot emptied it, and the recipe's scripts run in it."""
import hashlib
import pathlib
import re
import shlex
import time

from . import (addressing, contributions, delivery, extensions, platform, recipes, runtime, sandbox_layers,
               settings, state)
from .template_files import sbx_guest

RUN = platform.RUN
HOME = platform.HOME
APP_SCRIPTS = platform.APP_SCRIPTS
SETUP_LOCK = f"{HOME}/.sbx-setup.lock"
# The platform's helper in the sandbox: process supervision, SSH identity,
# swap, usage, the endpoints' forwarder (template_files/sbx_guest.py).
# Extensions' guest scripts shipped next to it import it.
GUEST_SOURCE = pathlib.Path(sbx_guest.__file__)
GUEST = f"{RUN}/{GUEST_SOURCE.name}"
ENV_SPLIT = "--sbx-app-env--"


def ensure_started(sbx, kind: str | None = None):
    """The start contract (platform.start) running: a VM booted from its disk
    or from a filesystem snapshot comes up without its saved processes. sbx's
    desktop is part of it only on a runtime without its own (runtime.desktop)."""
    from .core import SandboxError
    own_desktop = runtime.desktop(kind) == "sbx"
    if sbx.commands.run(platform.started(own_desktop), user="user", timeout=15).stdout.strip() == "ready":
        return
    if own_desktop:
        sbx.commands.run("DISPLAY=:0 xset q >/dev/null 2>&1 || rm -f /tmp/.X0-lock /tmp/.X11-unix/X0",
                         user="user", timeout=15)
    handle = sbx.commands.run(platform.start(own_desktop), user="user", background=True, timeout=0)
    handle.disconnect()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if sbx.commands.run(platform.started(own_desktop), user="user", timeout=15).stdout.strip() == "ready":
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
    # Its log stays in the sandbox; on failure its end is the command's error.
    sbx.commands.run(f"umask 077; {command} > {RUN}/ssh-identity.log 2>&1 || "
                     f"{{ tail -5 {RUN}/ssh-identity.log >&2; exit 1; }}", timeout=120)


def _envs(env: dict, app_env: dict, kind: str | None = None) -> dict:
    return {**app_env, **env, "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS": addressing.vite_allowed_hosts(kind)}


def runtime_layer(name: str, a: recipes.App, envs: dict, trusted: bool, metadata: dict | None,
                  write: sandbox_layers.Write | None = None):
    """(the env with what extensions add, their runtime layers as one) for the
    sandbox; with write, what the sandbox layers add to both."""
    contributed = []
    if write is not None:
        envs = sandbox_layers.values(write, envs)
        contributed = sandbox_layers.contributions(write)
    target = extensions.Target(repo=a.repo, name=name, trusted=trusted, connector=(metadata or {}).get("connector", ""),
                               env=envs)
    layer = contributions.merged_runtime([*extensions.contributions_for(target), *contributed])
    return {**envs, **layer.env}, layer


def write_runtime(sbx, name: str, envs: dict, layer: contributions.RuntimeLayer, public_keys=None,
                  hidden: set = frozenset(), write: sandbox_layers.Write | None = None) -> list:
    """The sandbox's env and the extensions' runtime layers (files under
    contributions.EXT, git config, files shells source), in its tmpfs; then
    what the sandbox layers do once it is written (write). Returns its SSH host key."""
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
    if write is not None:
        sandbox_layers.after_write(write, sbx)
    return host_key


_LOOK_UP = object()


def write_of(info, sbx=None, kind: str | None = _LOOK_UP) -> sandbox_layers.Write:
    """A runtime write of the existing sandbox (sbx: its running VM); kind
    looked up when not given (None: no runtime lists it, the default's rules)."""
    from . import core
    return sandbox_layers.Write(name=(info.metadata or {})["sbx"], repo=core.app_of(info).repo,
                                kind=runtime.kind_of(info) if kind is _LOOK_UP else kind, sbx=sbx)


def runtime_values(info, write: sandbox_layers.Write | None = None) -> tuple[dict, set, contributions.RuntimeLayer]:
    """(the sandbox's env as sbx-control would write it now, the names only
    its app gets, the extensions' and the sandbox layers' runtime layers)."""
    from . import core
    meta, a = info.metadata or {}, core.app_of(info)
    name = meta["sbx"]
    write = write or write_of(info)
    env = core.sbx_env(name, core.slug_of(info), a, meta.get("owner", ""), meta.get("branch", ""),
                       meta.get("template", ""))
    if settings.trust(a.repo) != "trusted":
        envs, layer = runtime_layer(name, a, _envs(env, core.app_env(a, env, trusted=False), write.kind), False, meta,
                                    write)
        return envs, set(), layer
    created = sandbox_layers.outputs(write, extensions.packages().refresh(name, a.repo))
    values, hidden = core.trusted_values(a, env, created, core.audience(meta), core.personal(meta))
    envs, layer = runtime_layer(name, a, _envs(env, values, write.kind), True, meta, write)
    return envs, hidden, layer


def restore_runtime(sbx, info) -> bool:
    """When the runtime had no room for a pause snapshot, it boots the
    sandbox again from its disk, and its tmpfs comes back empty: put the env,
    key and git credential back from what sbx-control keeps. True when it
    had to, or the recipe requests an app restart after resume."""
    from . import core
    meta = info.metadata if isinstance(info.metadata, dict) else {}
    try:
        kind = runtime.kind_of(info)
    except Exception:   # not found by any runtime now: the default's rules
        kind = None
    if core.runtime_state(info) == "paused":
        extensions.on_resume(sbx, info)
    # A runtime whose resumes are treated as cold boots (runtime.cold_resume):
    # everything below, and the app restarted.
    cold = core.runtime_state(info) != "running" and runtime.cold_resume(kind)
    if not cold and sbx.commands.run(f"test -f {RUN}/env && echo ok || true", timeout=30).stdout.strip() == "ok":
        paused = core.runtime_state(info) == "paused"
        name = meta.get("sbx")
        pending = delivery.pending(name) if isinstance(name, str) and name else None
        if pending and paused:
            _, restart, revision = pending
            delivered = core._refresh_env(sbx, info, restart)
            delivery.acknowledge(name, revision)
            state.event(name, "env updated", "queued changes applied")
            if delivered["restarted"]:
                return False
        return paused and core.app_of(info).resume_mode == "restart"
    # Booted from its disk, or an allocation whose creation failed part way.
    ensure_started(sbx, kind)
    initialize_ssh_identity(sbx)
    write = write_of(info, sbx, kind)
    envs, hidden, layer = runtime_values(info, write)
    write_runtime(sbx, write.name, envs, layer, hidden=hidden, write=write)
    state.event(write.name, "runtime restored",
                "every resume on this runtime is treated as a cold boot: env, credentials and processes put back" if cold else
                "its tmpfs was empty (likely: booted from its disk, as the host had no room for its pause snapshot), "
                "so its env and credentials were gone", internal=True)
    return True


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


def write_env(sbx, info, restart, operation):
    """The runtime written as it would be now; the app restarted when its env changed and restart asks."""
    from . import core
    write = write_of(info, sbx)
    envs, hidden, layer = runtime_values(info, write)
    before = written_env(sbx)
    differs = sorted(k for k in set(before) | set(envs) if before.get(k) != envs.get(k))
    must_restart = bool(core.app_of(info).repo and restart and (differs or delivery.restart_required(write.name)))
    if must_restart:
        delivery.restart_required(write.name, mark=True)
    write_runtime(sbx, write.name, envs, layer, hidden=hidden, write=write)
    if must_restart:
        core._run_app(sbx, info, operation)
    return {"changed": differs, "restarted": must_restart}


def app_script(sbx, script: str, a: recipes.App, env=f"{RUN}/env", timeout=60, operation=None, process_scope=None):
    """Run one of the recipe's scripts: the shipped copy when it is the
    operator's, else the checkout's .sbx/ (a branch may change its scripts)."""
    from . import core
    source = f"d={APP_SCRIPTS}" if a.operator else f"d=.sbx; [ -d .sbx ] || d={APP_SCRIPTS}"
    scope = f"export {sbx_guest.START_SCOPE}={shlex.quote(process_scope)}; " if process_scope else ""
    command = (f"cd {shlex.quote(a.workdir)} && {{ {source}; }} "
               f"&& {{ {scope}true; }} "
               f'&& {{ [ ! -x "$d/{script}" ] || "$d/{script}"; }}')
    if operation and script == "setup":
        operation.check()
        command = f"umask 077; exec > {sbx_guest.SETUP_LOG} 2>&1; " + command
        command = f"flock -n {shlex.quote(SETUP_LOCK)} bash -c {shlex.quote(command)}"
    try:
        return core.run_in(sbx, command, timeout=timeout, env=env)
    except runtime.CommandFailed as error:
        if not (operation and script == "setup"):
            raise
        # Its output went to the setup log: the end of that is the error (its callers mask it).
        try:
            tail = sbx.commands.run(f"tail -20 {sbx_guest.SETUP_LOG} 2>/dev/null || true", timeout=30).stdout
        except Exception:
            tail = ""
        raise runtime.CommandFailed(stdout=error.stdout, stderr=tail or error.stderr, exit_code=error.exit_code,
                                    command=f".sbx/{script}") from error
