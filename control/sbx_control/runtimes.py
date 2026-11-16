"""Several sandbox runtimes, as the core sees them: which one a new
sandbox runs on (choose), a repo's own choice (setting), the runtimes a
repo's sandboxes use (used_by), what the API says about them (describe), and
`sbx-admin runtime verify`, which takes one through a sandbox's whole life.

The registry itself (which are enabled, the default, the backends) is
sbx_control.runtime's; this module names no runtime either."""
from __future__ import annotations

import dataclasses
import secrets
import sqlite3
import time
import urllib.request

from . import runtime, state
from .core import SandboxError

REPO_RUNTIME_SCHEMA = """
create table if not exists repo_runtimes (
    repo text primary key, runtime text not null, updated_by text not null,
    updated_at text not null default current_timestamp);
"""


class Refused(SandboxError):
    """The runtime asked for can't run the sandbox, or is no runtime here;
    the message says why (the API answers 400)."""


# A repo's own runtime


def setting(repo: str | None) -> str | None:
    """The runtime the repo's new sandboxes run on, as an admin set it; None: the installation's default."""
    if not repo:
        return None
    with state.db() as conn:
        row = conn.execute("select runtime from repo_runtimes where repo = ?", (repo.lower(),)).fetchone()
    return row[0] if row else None


def set_setting(repo: str, kind: str | None, *, by: str) -> str | None:
    """Set (or with None clear) the repo's runtime; Refused for one that is not enabled."""
    repo = repo.lower()
    with state.db() as conn:
        if kind is None:
            conn.execute("delete from repo_runtimes where repo = ?", (repo,))
            return None
        if kind not in runtime.enabled():
            raise Refused(f"no runtime {kind} is enabled here; the enabled ones are {', '.join(runtime.enabled())}")
        conn.execute("insert into repo_runtimes (repo, runtime, updated_by) values (?, ?, ?) on conflict (repo) do "
                     "update set runtime = excluded.runtime, updated_by = excluded.updated_by, "
                     "updated_at = current_timestamp", (repo, kind, by))
    return kind


def of_repo(repo: str | None) -> str:
    """The runtime the repo's templates and room are counted on: its setting
    while that runtime is enabled, else the default. A create with a setting
    that is not enabled is refused (choose), never moved to the default."""
    found = setting(repo)
    return found if found in runtime.enabled() else runtime.default()


def used_by(repo: str) -> set:
    """The enabled runtimes the repo's sandboxes use: its setting, the
    default, and any a sandbox of it runs on (the templates it needs are
    built on each of them)."""
    found = {of_repo(repo), runtime.default()}
    try:
        with state.db() as conn:
            found |= {row[0] for row in conn.execute(
                "select s.runtime from sandboxes s join sandbox_lifecycle l on l.name = s.name "
                "where json_extract(l.record, '$.repository') = ? and json_extract(l.record, '$.phase') != 'deleted' "
                "and s.runtime != ''", (repo.lower(),))}
    except sqlite3.OperationalError:  # before the first migrate
        pass
    return found & set(runtime.enabled())


# Choosing


def choose(repo: str | None, requested: str | None = None, size: dict | None = None) -> str:
    """The runtime of a new sandbox of the repo: the request's, else the
    repo's setting, else the default. Refused, naming the runtime and why,
    when it is not enabled, when managed servers are attached to the repo
    and its sandboxes don't reach this host's private network, or when the
    recipe's size (when given) is beyond its limits; sbx never falls back to
    another runtime."""
    chosen = setting(repo)
    if not requested and chosen and chosen not in runtime.enabled():
        raise Refused(f"{repo}'s runtime setting is {chosen}, which is not enabled on this installation (enabled: "
                      f"{', '.join(runtime.enabled())}); change it on the repository's page or with "
                      f"PUT /api/repos/{repo}/runtime, or name a runtime in the request")
    kind = requested or of_repo(repo)
    if kind not in runtime.enabled():
        if kind in runtime.kinds():
            raise Refused(f"the {kind} runtime is not enabled on this installation (enabled: "
                          f"{', '.join(runtime.enabled())})")
        raise Refused(f"there is no runtime {kind} (enabled here: {', '.join(runtime.enabled())})")
    title = runtime.title(kind)
    if repo and not runtime.capabilities(kind).private_networks:
        from . import hosted_services
        servers = hosted_services.attached_to(repo)
        if servers:
            raise Refused(f"{title} ({kind}) can't run {repo}'s sandboxes: the managed server"
                          f"{'s' if len(servers) > 1 else ''} {', '.join(servers)} attached to it "
                          "run on this host's private network, which its sandboxes don't reach "
                          "(private_networks); choose another runtime")
    if size and (why := runtime.beyond_limits(size, kind)):
        raise Refused(f"{title} ({kind}) can't run it: {why}")
    return kind


# What the API says


def describe() -> dict:
    """GET /api/runtimes: every enabled runtime, the default first among equals."""
    first = runtime.default()
    return {"default": first, "runtimes": [_runtime(kind, kind == first) for kind in runtime.enabled()]}


def _runtime(kind: str, is_default: bool) -> dict:
    backend = runtime.get(kind)
    try:
        found = runtime.status(kind)
        status = {"state": found.state, "summary": found.summary}
    except Exception as error:  # one runtime that does not answer must not hide the others
        status = {"state": "attention", "summary": f"{backend.title} did not answer: {type(error).__name__}: {error}"}
    try:
        limits = runtime.limits(kind).known()
    except Exception:
        limits = {}
    return {"kind": kind, "title": backend.title, "default": is_default, "host": bool(getattr(backend, "host", False)),
            "capabilities": dataclasses.asdict(backend.capabilities), "limits": limits, "status": status}


# sbx-admin runtime verify

VERIFY_PORT = 8765
VERIFY_FILE = "/tmp/sbx-verify"
VERIFY_TIMEOUT = 600


def _fetch(url: str, host: str) -> str:
    """GET url with this Host, as nginx sends a preview's request to a runtime's ingress."""
    request = urllib.request.Request(url, headers={"Host": host})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode(errors="replace")


def verify(kind: str | None = None, say=print) -> bool:
    """Take a throwaway sandbox on the runtime of kind (None: the default)
    through its life: create it from the base template, run a command, write
    and read a file, pause and resume it, reach a preview port through the
    gate's route and the runtime's ingress, delete it. Each step is said as
    it ends; what it made is deleted whatever happens. True when every step
    passed."""
    from . import gate, template_builds, templates
    kind = kind or runtime.default()
    say(f"verifying {runtime.title(kind)} ({kind})")
    made = None
    passed = True

    def step(name, work):
        nonlocal passed
        if not passed:
            say(f"  --  {name}: skipped")
            return None
        began = time.monotonic()
        try:
            detail = work()
        except Exception as error:
            passed = False
            say(f"  NO  {name}: {type(error).__name__}: {error}")
            return None
        say(f"  ok  {name}: {detail} ({time.monotonic() - began:.1f}s)")
        return detail

    token = secrets.token_hex(8)
    try:
        handle = None

        def create():
            nonlocal handle, made
            template = template_builds.built(templates.BASE, kind)
            handle = runtime.create(template, timeout=VERIFY_TIMEOUT, metadata={"sbx_check": "verify"},
                                    on_timeout="kill", kind=kind)
            made = handle.sandbox_id
            return f"{made} from {template}"
        step("create from the base template", create)

        def command():
            out = handle.commands.run(f"echo {token}", timeout=30).stdout.strip()
            if out != token:
                raise AssertionError(f"echo printed {out[:80]!r}")
            return "echo answered"
        step("run a command", command)

        def files():
            handle.files.write(VERIFY_FILE, token)
            read = handle.files.read(VERIFY_FILE)
            shown = handle.commands.run(f"cat {VERIFY_FILE}", timeout=30).stdout
            if read != token or shown != token:
                raise AssertionError(f"read back {read[:80]!r}, cat showed {shown[:80]!r}")
            return VERIFY_FILE
        step("write and read a file", files)

        def pause():
            runtime.pause(made)
            found = runtime.info(made).state
            if found != runtime.PAUSED:
                raise AssertionError(f"the runtime lists it {found}")
            return "paused"
        step("pause", pause)

        def resume():
            nonlocal handle
            handle = runtime.connect(made, timeout=VERIFY_TIMEOUT)
            if runtime.info(made).state != runtime.RUNNING:
                raise AssertionError("the runtime does not list it running")
            if handle.files.read(VERIFY_FILE) != token:
                raise AssertionError(f"{VERIFY_FILE} did not survive the pause")
            return "running, its file kept"
        step("resume", resume)

        def preview():
            handle.commands.run(f"cd /tmp && python3 -m http.server {VERIFY_PORT} --bind 0.0.0.0 >/dev/null 2>&1",
                                background=True, timeout=0).disconnect()
            route = gate.route(VERIFY_PORT, made)
            if route.get("X-Sbx-Runtime") != kind:
                raise AssertionError(f"the gate routes it to {route.get('X-Sbx-Runtime')!r}")
            ingress = runtime.get(kind).ingress
            last = None
            for _ in range(20):
                try:
                    body = _fetch(f"{ingress}/{VERIFY_FILE.rsplit('/', 1)[1]}", route["X-Sbx-Upstream"])
                    if body.strip() == token:
                        return f"{route['X-Sbx-Upstream']} through {ingress}"
                    last = f"answered {body[:80]!r}"
                except Exception as error:
                    last = f"{type(error).__name__}: {error}"
                time.sleep(1)
            raise AssertionError(f"port {VERIFY_PORT} through {ingress}: {last}")
        step("reach a preview port through the gate", preview)

        def delete():
            nonlocal made
            runtime.kill(made)
            gone, made = made, None
            if any(i.sandbox_id == gone for i in runtime.get(kind).sandboxes(None, [runtime.RUNNING])):
                raise AssertionError("the runtime still lists it running")
            return gone
        before = passed
        passed = True     # what it made is deleted whatever failed before
        if made:
            step("delete", delete)
        passed = passed and before
    finally:
        if made:
            try:
                runtime.kill(made)
                say(f"  deleted {made}")
            except Exception as error:
                say(f"  could not delete {made}: {error}; it times out on its own")
    say("passed" if passed else "failed")
    return passed


SCHEMA = state.Schema(tables=REPO_RUNTIME_SCHEMA)
