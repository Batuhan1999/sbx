"""The controller's sandboxes: the registry rows that make them this
controller's (name, owner, branch, slug), and the operations every entry
point shares, env deliveries among them. A sandbox the runtime lists is
this controller's only when its row here names the same owner; its state is
its lifecycle record's."""
import json
import re
import sqlite3
import threading
import time

from pydantic import BaseModel, Field, field_validator

from . import (addressing, core, delivery, lifecycle, recipes, repo_path, routing, runtime, runtimes, sandbox_layers,
               settings, state, template_builds, variables)
from .state import db

SLUG_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class CreateSandbox(BaseModel):
    name: str
    branch: str | None = None  # defaults to the repo's default branch
    public_keys: str = ""  # SSH public keys for the sandbox's user: authorized_keys text, or a list of keys
    slug: str | None = None  # defaults to the name
    repo: str | None = None  # owner/repo; defaults to the installation's first
    source_commit: str | None = Field(default=None, pattern=f"^{core.FULL_COMMIT}$")
    runtime: str | None = None  # the runtime's kind; defaults to the repo's setting, else the installation's

    @field_validator("repo")
    @classmethod
    def normalized(cls, repo: str | None) -> str | None:
        return repo_path.normalize(repo) if repo else repo

    @field_validator("public_keys", mode="before")
    @classmethod
    def key_lines(cls, keys):
        """A list of keys is the same authorized_keys text, one key per line."""
        if isinstance(keys, list) and all(isinstance(key, str) for key in keys):
            return "".join(key.strip() + "\n" for key in keys if key.strip())
        return keys


def registered_owner(name: str) -> str | None:
    """SDK owner metadata alone does not identify this controller's resources."""
    with db() as conn:
        row = conn.execute("select owner from sandboxes where name=?", (name,)).fetchone()
    return row[0] if row else None


def registered(infos) -> list:
    with db() as conn:
        known = dict(conn.execute('select name,owner from sandboxes'))
    return [info for info in infos if (info.metadata or {}).get('sbx') in known
            and (info.metadata or {}).get('owner') == known[(info.metadata or {}).get('sbx')]]


def register(name: str, owner: str, branch: str, slug: str, metadata: dict | None = None, kind: str = ""):
    """A name whose last sandbox failed or was deleted is free again for
    the same owner. metadata is what its creator tagged it with (core.create),
    known here before its VM exists; kind the runtime it runs on."""
    found = registered_owner(name)
    if found and found != owner:
        raise lifecycle.Conflict("the sandbox name belongs to another owner")
    current = lifecycle.get(name)
    if found and (current is None or current["phase"] in ("failed", "interrupted", "deleted")):
        if core.find(name):
            raise lifecycle.Conflict(f"{name} still has a sandbox; prepare or delete it before creating again")
        unregister(name)
    from . import endpoints
    try:
        with endpoints.lock():   # an endpoint is added under it: its name and this slug checked against each other
            _no_endpoint_at(slug)
            with db() as conn:
                conn.execute("insert into sandboxes (name, owner, branch, slug, metadata, runtime) values (?, ?, ?, ?, ?, ?)",
                             (name, owner, branch, slug, json.dumps(metadata or {}), kind or runtime.default()))
    except sqlite3.IntegrityError as error:
        raise lifecycle.Conflict(f"{name} exists") from error


def rows() -> dict:
    """Every registration by name: owner, branch, slug, metadata, times."""
    with db() as conn:
        conn.row_factory = sqlite3.Row
        found = {r["name"]: dict(r) for r in conn.execute("select * from sandboxes")}
    for row in found.values():
        row["metadata"] = json.loads(row.get("metadata") or "{}")
    return found


def slug_taken(slug: str, name: str = "") -> bool:
    """Whether another sandbox than name has the slug."""
    with db() as conn:
        return conn.execute("select 1 from sandboxes where slug = ? and name != ?", (slug, name)).fetchone() is not None


def registered_slug(name: str) -> str | None:
    with db() as conn:
        row = conn.execute("select slug from sandboxes where name = ?", (name,)).fetchone()
    return row[0] if row else None


def check_slug(slug: str, name: str = "") -> str:
    """A sandbox's preview host: a DNS label that is no host of the
    installation's own, ends like no preview host, is no other sandbox's
    slug plus -<port>, and no other sandbox than name has."""
    if not SLUG_RE.fullmatch(slug):
        raise core.SandboxError(f"slug must be a DNS label ({SLUG_RE.pattern})")
    if slug in settings.current().reserved_names or any(slug.endswith(suffix) for suffix in core.preview_suffixes()):
        raise core.SandboxError(f"{slug} is reserved")
    if routing.port_host(slug):
        raise core.SandboxError(f"{slug} is taken: it is the host of a port in {slug.rpartition('-')[0]}")
    if slug_taken(slug, name):
        raise core.SandboxError(f"{slug} is another sandbox's")
    _no_endpoint_at(slug)
    return slug


def _no_endpoint_at(slug: str) -> None:
    from . import endpoints
    if slug and (found := endpoints.slug_taken(slug)):
        raise core.SandboxError(f"{slug} is taken: {found}.<domain> is an endpoint (sbx endpoints), which a "
                                f"preview host of {slug} would be; choose another name or slug")


def set_branch(name: str, branch: str):
    with db() as conn:
        conn.execute("update sandboxes set branch = ?, updated_at = current_timestamp where name = ?", (branch, name))


# A prepare asked for while the sandbox was busy with another operation (a
# push to a PR preview while it is still being made): kept here, and run by
# the keeper's next round once the sandbox is free (prepare_waiting).


def want_prepare(name: str, branch: str, *, force: bool = False):
    """Prepare the sandbox on branch's head once it is free; force: discard
    local edits (core.prepare_workspace)."""
    with db() as conn:
        conn.execute("update sandboxes set branch = ?, prepare_wanted = ?, updated_at = current_timestamp "
                     "where name = ?", (branch, "force" if force else "checkout", name))


def prepare_waiting(present: set):
    """The keeper's round: the waiting prepares of the sandboxes in present
    (those the runtime lists) that no operation holds now."""
    with db() as conn:
        rows = conn.execute("select name, branch, prepare_wanted from sandboxes where prepare_wanted != ''").fetchall()
    for name, branch, wanted in rows:
        current = lifecycle.get(name)
        if name not in present or (current and current["active"]):
            continue
        with db() as conn:
            conn.execute("update sandboxes set prepare_wanted = '' where name = ?", (name,))
        try:
            lifecycle.in_background(name, "prepare", lambda operation, name=name, branch=branch, force=wanted == "force":
                                    core.prepare_and_start(name, branch, checkout=True, operation=operation, force=force))
        except lifecycle.Conflict:  # one began meanwhile: the next round
            want_prepare(name, branch, force=wanted == "force")


def unregister(name: str):
    with db() as conn:
        conn.execute("delete from sandboxes where name = ?", (name,))
    sandbox_layers.forget(name)


def forget(name: str):
    """After core.delete: the row and the sandbox's own generated values."""
    variables.forget(name)
    unregister(name)


def delete(name: str) -> dict | None:
    with core.sandbox_operation(name):
        return delete_locked(name)


def delete_locked(name: str) -> dict | None:
    """delete() for a caller that holds core.sandbox_operation(name): the VM,
    its services and its row. Returns what the recipe's .sbx/cleanup did
    (core.run_cleanup)."""
    cleanup = core.delete(name, registered_slug(name))
    forget(name)
    return cleanup


def record_transition(info, vm: str):
    """Pauses and resumes no one here asked for: Embed pausing a sandbox when
    its timeout ran out, a resume through the runtime's API."""
    name = (info.metadata or {}).get("sbx")
    before = lifecycle.observe(name, info.sandbox_id, vm) if name else None
    if before in ("running", "paused"):
        state.event(name, vm, "idle until its timeout" if vm == "paused" else "", internal=vm not in ("running", "paused"))


def describe(info) -> dict:
    """The sandbox for the API; ports are the detected ones, with the links
    also among urls as port-<n>."""
    meta = info.metadata or {}
    links, ports = routing.detected_urls(info)
    vm = core.runtime_state(info)
    current = lifecycle.get(meta["sbx"], vm, info.sandbox_id)
    a = core.app_of(info)
    status = lifecycle.status(current)
    return {"name": meta["sbx"], "owner": meta.get("owner"), "branch": meta.get("branch", ""),
            "sandbox_id": info.sandbox_id, "state": str(info.state), "runtime": runtime.kind_of(info),
            "status": status, "readiness": current["readiness"] if current else "unknown",
            "actions": lifecycle.actions(current, status, vm),
            "lifecycle": current, "error": current["error"] if current else None,
            "audience": core.audience(meta), "shared": core.shared(meta),
            "urls": {**addressing.urls(core.slug_of(info), a), **links}, "ports": ports,
            "preview_access": {"mode": addressing.mode()},
            "ssh_url": addressing.ssh_url(core.slug_of(info), a, info.sandbox_id),
            "repo": a.workdir, "app": a.repo, "logs": core.app_logs(a)}


def wait(name: str, seconds: float):
    """Until the sandbox's operation finishes or moves to another phase, at
    most seconds: a client follows an operation without polling."""
    first = lifecycle.get(name)
    deadline = time.monotonic() + seconds
    while first and first["active"] and time.monotonic() < deadline:
        time.sleep(0.5)
        now = lifecycle.get(name)
        if not now or not now["active"] or now["phase"] != first["phase"]:
            return


def view(info) -> dict:
    """describe(), after looking again at what listens in a running one: a
    server started a moment ago shows up here, not only after the keeper's
    next round."""
    if settings.current().sandboxes.detected_previews != "off" and core.runtime_state(info) == "running" \
            and routing.detection_due(info.sandbox_id):
        routing.detect_ports(info.sandbox_id, core.app_of(info))
    return describe(info)


def owned_by(email: str) -> list:
    infos = [info for info in core.sandboxes({"owner": email})
             if (info.metadata or {}).get("owner") == email
             and registered_owner((info.metadata or {}).get("sbx")) == email]
    return [describe(i) for i in infos if (i.metadata or {}).get("sbx")]


def create(body: CreateSandbox, email: str, *, start_app: bool = True, metadata: dict | None = None,
           start_from: str | None = None):
    """Return once SSH works; the app starts in the background unless start_app is false."""
    core.check_name(body.name)
    with core.sandbox_operation(body.name):
        return create_locked(body, email, start_app=start_app, metadata=metadata, start_from=start_from)


def create_locked(body: CreateSandbox, email: str, *, start_app: bool = True, metadata: dict | None = None,
                  start_from: str | None = None):
    """create() for a caller that holds core.sandbox_operation(body.name).
    metadata tags the sandbox (core.create); start_from is a snapshot to
    start from instead of the repo's template (an extension's cache), on the
    runtime body.runtime names (the one that holds it). The runtime is the
    request's, else the repo's, else the default (runtimes.choose)."""
    slug = check_slug(body.slug or body.name, body.name)
    branch = body.branch or recipes.default_branch(body.repo)
    repo = body.repo or settings.current().default_repo
    kind = runtimes.choose(repo, body.runtime, None if start_from else recipes.load(body.repo).size)
    template = start_from or template_builds.template_for(repo, kind)
    register(body.name, email, branch, slug, metadata, kind)
    advise_endpoints(body.name, repo, kind)
    try:
        result = core.create(body.name, slug, email, branch, body.public_keys, body.repo,
                             source_commit=body.source_commit, template=template,
                             memory_mb=template_builds.size_of(template), metadata=metadata, kind=kind)
    except Exception:
        if lifecycle.get(body.name) is None:  # refused before anything was made
            unregister(body.name)
        raise
    if start_app:
        lifecycle.in_background(body.name, "prepare",
                                lambda operation: core.prepare_and_start(body.name, branch, operation=operation))
    info = core.find(body.name)
    return {**result, **(describe(info) if info else {})}


def advise_endpoints(name: str, repo: str | None, kind: str):
    """An internal address in the recipe's env that its sandboxes on this
    runtime won't reach (no endpoint, or not granted to the repo): not a
    refusal, an event that says what would make it reachable."""
    from . import endpoints, runtime
    if not repo or runtime.capabilities(kind).private_networks:
        return
    try:
        said = endpoints.advice(repo)
    except Exception as error:   # advice only: the create goes on
        print(f"endpoints: advice for {repo}: {error!r}", flush=True)
        return
    for line in said:
        state.event(name, "endpoint suggested", line)


def restart(name: str, info, prepare: bool = False):
    """Restart the app, or with prepare reapply the recipe and setup to the
    checkout (preserving local source edits), on a thread."""
    if prepare:
        lifecycle.in_background(name, "prepare", lambda operation: core.prepare_and_start(
            name, core.branch_of(info), operation=operation))
    else:
        lifecycle.in_background(name, "restart", lambda operation: core.restart(name, operation))
    return {"status": "starting"}


def pause(name: str, info):
    core.pause(name, info)
    state.event(name, "paused", "explicit operator request")
    # Its status as the API shows it: a failed sandbox stays failed, its VM paused.
    return {"name": name, "state": (lifecycle.get(name) or {}).get("status", "paused"), "vm": "paused"}


def resume(name: str, info, start_app: bool = True):
    core.attach(name, "explicit operator request", info=info, run_app=start_app)
    return {"name": name, "state": "running"}


def execute(name: str, info, script: str, timeout: int) -> dict:
    """A script in the sandbox with the env its SSH shells get (resuming a
    paused one): what its owner could run over SSH, for agents and tools
    that have only the API."""
    sandbox = core.attach(name, "to run a command in it", info=info)
    try:
        result = sandbox.run(script, timeout=timeout)
    except runtime.CommandFailed as error:
        result = error
    return {"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}


# Env deliveries: a change to the values a repo's sandboxes get reaches the
# running ones within seconds (their env written again, the app restarted)
# and the paused ones when they next run (core.restore_runtime, else the
# keeper's next round). delivery.py keeps what waits.

_delivering: dict = {}  # sandbox -> lock: one delivery at a time in this process


def deliver_env(repo: str, names, audiences=None, person: str | None = None, restart: bool = True) -> dict:
    """Bring the repo's sandboxes the change: the running ones now (on a
    thread each), the paused ones when they next run. audiences limits it to
    the kinds of sandbox the values go to (core.audience); person to that
    person's. When the runtime does not answer, the change waits for the
    keeper's next round."""
    running = paused = 0
    try:
        infos = core.sandboxes()
    except Exception as error:  # the value is stored; only who gets it is unknown yet
        delivery.queue_repo(repo, names, audiences, person, restart)
        return {"running": 0, "paused": 0, "pending": True,
                "detail": f"the sandbox runtime did not answer ({type(error).__name__}); "
                          "its sandboxes get the change once it does"}
    for info in infos:
        meta = info.metadata or {}
        sandbox, owner = meta.get("sbx"), meta.get("owner", "")
        if not sandbox or core.app_of(info).repo != repo or (person and owner != person):
            continue
        if audiences and "all" not in audiences and core.audience(meta) not in audiences:
            continue
        delivery.queue(sandbox, names, restart)
        if core.runtime_state(info) == "running":
            threading.Thread(target=deliver_pending, args=(sandbox,), daemon=True).start()
            running += 1
        else:
            paused += 1
    return {"running": running, "paused": paused}


def deliver_pending(sandbox: str):
    """What waits for the running sandbox, until nothing does; a sandbox
    busy with an operation gets it at the keeper's next round. It waits for
    whoever is creating or deleting the sandbox (core.sandbox_operation), so
    a delivery never starts in the middle of that and makes it conflict."""
    guard = _delivering.setdefault(sandbox, threading.Lock())
    if not guard.acquire(blocking=False):
        return
    try:
        with core.sandbox_operation(sandbox):
            for _ in range(3):
                current = lifecycle.get(sandbox)
                if current and current["active"]:
                    return
                waiting = delivery.pending(sandbox)
                if not waiting:
                    return
                _, restart, revision = waiting
                done = core.refresh_env(sandbox, restart)
                delivery.acknowledge(sandbox, revision)
                if done["changed"]:
                    state.event(sandbox, "env updated", ", ".join(done["changed"])
                                + ("; app restarted" if done["restarted"] else "; app restart required"))
    except Exception as error:
        state.event(sandbox, "env update failed", "retained for retry: " + str(error)[-1000:])
    finally:
        guard.release()


def deliver_waiting(running: set):
    """The keeper's round: the repo changes kept while the runtime did not
    answer, then what waits for each running sandbox."""
    for repo, names, audiences, person, restart in delivery.take_repos():
        deliver_env(repo, names, audiences, person, restart)  # kept again if the runtime fails now
    for name in delivery.waiting() & running:
        threading.Thread(target=deliver_pending, args=(name,), daemon=True).start()


def _runtimes(conn):
    """Sandboxes registered before each recorded its runtime ran on the one
    runtime there was then (runtime.LEGACY), whatever the default is now."""
    conn.execute("update sandboxes set runtime = ? where runtime = ''", (runtime.LEGACY,))


SCHEMA = state.Schema(tables="""
create table if not exists sandboxes (
    name text primary key, owner text not null, branch text not null,
    slug text unique, metadata text not null default '{}',
    created_at text not null default current_timestamp, updated_at text not null default current_timestamp);
""", columns=(("sandboxes", "prepare_wanted", "text not null default ''"),
              ("sandboxes", "runtime", "text not null default ''")), upgrade=_runtimes)
