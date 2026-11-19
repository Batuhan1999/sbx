"""What packages make for sandboxes (`sandbox_services`), and running their
scripts; what a package does for a sandbox beyond its env: its runtime
layer (files and env in the sandbox: contributions()), its hooks (pause:
after the sandbox paused) and its actions (host-side calls the sandbox
makes with its resource's token: action()).

An installation package's scripts run on the host; a repo's own run in a
throwaway sandbox. A package's Python code (runtime, actions, a .py check)
runs in this process, and only an installation package's. A resource keeps
the package that made it (Package.snapshot), so its later scripts are the
same version. A resource is recorded, with a stable resource ID, before its
create script runs: a failure or a crash leaves something for delete.

A script gets its action as its first argument and $SBX_ACTION, INPUT_<NAME>
for each input, its connections' exposed fields, SBX_ID (the resource ID),
SBX_NAME, SBX_REPO, SBX_SERVICE, SBX_PREVIOUS_OUTPUT and SBX_RESOURCE_STATUS,
and a package with actions SBX_ACTION_TOKEN (the token the platform issues
its resource, its `token` output); create writes key=value lines to
$SBX_OUTPUT."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote

from .. import contributions as contrib, paths, runtime, secret_store, settings, state
from . import NAME_RE, VAR_RE, Package, PackageError, attachments, code, resolve, resource_names

RUNNER_DIR = "/tmp/sbx-service"
RUNNER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
TOKEN = "token"   # the output a package with actions gets from the platform: what its sandbox calls them with


@dataclass
class Created:
    package: Package
    outputs: dict

    @property
    def env(self) -> dict:
        return self.package.env


def _ids(name: str, repo: str, key: str, resource_id: str) -> dict:
    return {"SBX_NAME": name, "SBX_ID": resource_id, "SBX_REPO": repo, "SBX_SERVICE": key}


@contextlib.contextmanager
def _resource_lock(name, key):
    root = paths.state_dir() / "service-locks"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / hashlib.sha256(f"{name}:{key}".encode()).hexdigest()).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _run(package: Package, action: str, ids: dict, inputs: dict, bound: dict, label: str) -> tuple[str, dict]:
    """Run one of its scripts (runs: or hooks:): an installation package's
    here, a repo's in a runner sandbox. Its stdout and the key=value lines
    it wrote to $SBX_OUTPUT; a create's partial outputs are kept (sealed)
    even when it fails, for its retry to resume."""
    previous, status, saved = {}, "creating", None
    if action in ("create", "delete"):
        with state.db() as conn:
            saved = conn.execute("select resource_id, status, outputs, sensitive from sandbox_services "
                                 "where sandbox = ? and key = ?", (ids["SBX_NAME"], ids["SBX_SERVICE"])).fetchone()
        if saved and saved[0] == ids["SBX_ID"]:
            status = saved[1]
            if saved[3]:
                previous = {**json.loads(saved[2] or "{}"), **secret_store.unseal(saved[3])}
        else:
            saved = None
    env = {**attachments.resolve(package, bound, label)[1], **{f"INPUT_{name.upper()}": value for name, value in inputs.items()}, **ids,
           "SBX_ACTION": action, "SBX_PREVIOUS_OUTPUT": json.dumps(previous), "SBX_RESOURCE_STATUS": status}
    returncode, stdout, stderr, output = (_in_runner if package.source == "repo" else _here)(package, action, env, label)
    if stderr.strip():
        print(f"{label}: {action}: {stderr.strip()[-2000:]}", file=sys.stderr)
    outputs = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if not line.strip():
            continue
        if not sep or not VAR_RE.fullmatch(key):
            raise PackageError(f"{label}: {action} wrote a line to $SBX_OUTPUT that is not key=value")
        outputs[key] = value
    if action == "create" and saved and status == "creating" and outputs:
        with state.db() as conn:
            conn.execute("update sandbox_services set sensitive = ?, updated_at = current_timestamp "
                         "where sandbox = ? and key = ? and resource_id = ? and status = 'creating'",
                         (secret_store.seal({**previous, **outputs}, key=f"service/{saved[0]}/outputs"),
                          ids["SBX_NAME"], ids["SBX_SERVICE"], saved[0]))
    if returncode:
        raise PackageError(f"{label}: {action} failed ({returncode}): {stderr.strip()[-500:]}")
    return stdout, outputs


def _script(package: Package, action: str) -> str:
    return package.runs.get(action) or package.hooks[action]


def _here(package: Package, action: str, env: dict, label: str) -> tuple[int, str, str, str]:
    """On this host, from a private copy of the folder: (exit code, stdout, stderr, $SBX_OUTPUT)."""
    with tempfile.TemporaryDirectory(prefix="sbx-service-") as tmp:
        folder, output = pathlib.Path(tmp) / "service", pathlib.Path(tmp) / "output"
        for rel, text in package.files.items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_text(text)
            (folder / rel).chmod(0o700)
        output.touch(0o600)
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": tmp, **env, "SBX_OUTPUT": str(output)}
        # A process group of its own: a timeout ends what the script started too.
        proc = subprocess.Popen([str(folder / _script(package, action)), action], cwd=folder, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=package.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            return 124, stdout, stderr + f"\n{action} timed out after {package.timeout}s", output.read_text()
        return proc.returncode, stdout, stderr, output.read_text()


def _in_runner(package: Package, action: str, env: dict, label: str) -> tuple[int, str, str, str]:
    """In a throwaway sandbox from the base template, so a repo's scripts
    never run on this host; like the repo's own sandboxes it reaches
    private ranges (hosted_services.allowed_cidrs) and the internet. Killed when done, whatever
    happened. The script sees its env file and nothing of the sandbox's."""
    from .. import template_builds, templates
    try:
        sbx = runtime.create(template_builds.built(templates.BASE), timeout=package.timeout + 300,
                             deny_out=runtime.deny_out("169.254.169.254/32"),
                             metadata={"sbx-service": package.name, "repo": package.repo})
    except Exception as error:
        raise PackageError(f"{label}: {action}: its runner sandbox did not start: {error}") from error
    root = RUNNER_DIR
    try:
        for rel, text in package.files.items():
            sbx.files.write(f"{root}/service/{rel}", text)
        env = {"PATH": RUNNER_PATH, "HOME": f"{root}/home", **env, "SBX_OUTPUT": f"{root}/output"}
        sbx.files.write(f"{root}/env", "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items()))
        inner = f". {root}/env && rm -f {root}/env && exec ./{shlex.quote(_script(package, action))} {action}"
        # Always exits 0, with the script's code in rc: a failing script is an
        # answer, not an error of the runner.
        command = (f"mkdir -p {root}/home && touch {root}/output && chmod -R go-rwx {root} && chmod -R u+x "
                   f"{root}/service && cd {root}/service && env -i /bin/bash -c {shlex.quote(inner)}; "
                   f"echo $? > {root}/rc")
        result = sbx.commands.run(f"bash -c {shlex.quote(command)}", timeout=package.timeout)
        returncode = int(sbx.files.read(f"{root}/rc").strip() or 1)
        return returncode, result.stdout, result.stderr, sbx.files.read(f"{root}/output")
    except runtime.Timeout:
        raise PackageError(f"{label}: {action} timed out after {package.timeout}s")
    except Exception as error:
        raise PackageError(f"{label}: {action}: its runner sandbox failed: {error!r}") from error
    finally:
        try:
            sbx.kill()
        except Exception as error:  # it times out on its own
            print(f"{label}: killing its runner sandbox: {error!r}", file=sys.stderr)


def create(name: str, repo: str, key: str, uses: str, given: dict, position: int, chosen: dict | None = None) -> Created:
    with attachments.lock(shared=True), _resource_lock(name, key):
        return _create(name, repo, key, uses, given, position, chosen or {})


def _create(name, repo, key, uses, given, position, chosen) -> Created:
    label = f"service {key} ({uses}) of sbx-{name}"
    if not NAME_RE.fullmatch(key):
        raise PackageError(f"sbx.yaml services: {key!r} is not a service key (a-z, 0-9, - and _)")
    with state.db() as conn:
        row = conn.execute("select uses, repo, definition, inputs, connections, resource_id from sandbox_services "
                           "where sandbox = ? and key = ?", (name, key)).fetchone()
    if row:
        bound = json.loads(row[4] or "{}")
        if row[0] != uses or row[1] != repo or (chosen and not set(chosen.items()) <= set(bound.items())):
            raise PackageError(f"{label}: an existing resource has a different package or connection; "
                               "remove it before replacing its binding")
        package, inputs, resource_id = Package.from_snapshot(row[2]), secret_store.unseal(row[3]), row[5]
    else:
        package = resolve(uses, repo)
        inputs = package.with_defaults(given, label)
        bound = attachments.bind(package, chosen, label)[0]
        resource_id = f"{name[:8]}-{secrets.token_hex(10)}"
        with state.db() as conn:
            conn.execute("insert into sandbox_services (sandbox, key, uses, position, repo, definition, inputs, "
                         "connections, resource_id, status) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'creating')",
                         (name, key, uses, position, repo, package.snapshot(),
                          secret_store.seal(inputs, key=f"service/{resource_id}/inputs"), json.dumps(bound), resource_id))
    ids = _with_token(package, _ids(name, repo, key, resource_id))
    if "create" in package.runs:
        outputs = _run(package, "create", ids, inputs, bound, label)[1]
        missing = [o for o in package.outputs if o not in outputs]
        if missing:
            raise PackageError(f"{label}: create wrote no {', '.join(missing)} to $SBX_OUTPUT")
    else:  # values only (static): its inputs are its outputs
        outputs = dict(inputs)
    if package.actions:
        outputs[TOKEN] = ids["SBX_ACTION_TOKEN"]
    public = {k: v for k, v in outputs.items() if k in package.outputs and not package.outputs[k].get("sensitive")}
    sensitive = {k: v for k, v in outputs.items() if k not in public}
    reference = secret_store.seal(sensitive, key=f"service/{resource_id}/outputs")
    with state.db() as conn:
        old = conn.execute("select sensitive from sandbox_services where sandbox = ? and key = ?", (name, key)).fetchone()
        conn.execute("update sandbox_services set outputs = ?, sensitive = ?, status = 'ready', "
                     "updated_at = current_timestamp where sandbox = ? and key = ?",
                     (json.dumps(public), reference, name, key))
    if old and old[0] and old[0] != reference:
        secret_store.discard(old[0])
    return Created(package, outputs)


def _with_token(package: Package, ids: dict) -> dict:
    """ids with SBX_ACTION_TOKEN for a package with actions: the token the
    platform issues its resource once, kept sealed with the resource's
    outputs (so a retried create gets the same one) and added to them."""
    if not package.actions:
        return ids
    with state.db() as conn:
        sealed = conn.execute("select sensitive from sandbox_services where sandbox = ? and key = ?",
                              (ids["SBX_NAME"], ids["SBX_SERVICE"])).fetchone()[0]
    kept = secret_store.unseal(sealed) if sealed else {}
    if not kept.get(TOKEN):
        kept[TOKEN] = secrets.token_urlsafe(32)
        with state.db() as conn:
            conn.execute("update sandbox_services set sensitive = ? where sandbox = ? and key = ?",
                         (secret_store.seal(kept, key=f"service/{ids['SBX_ID']}/outputs"), ids["SBX_NAME"],
                          ids["SBX_SERVICE"]))
        if sealed:
            secret_store.discard(sealed)
    return {**ids, "SBX_ACTION_TOKEN": kept[TOKEN]}


def delete_one(name: str, key: str) -> bool:
    """Run the resource's delete with the package that made it; its record
    stays when that fails."""
    with attachments.lock(shared=True), _resource_lock(name, key):
        with state.db() as conn:
            row = conn.execute("select repo, definition, inputs, connections, resource_id from sandbox_services "
                               "where sandbox = ? and key = ?", (name, key)).fetchone()
        if not row:
            return False
        repo, snapshot, inputs, bound, resource_id = row
        package = Package.from_snapshot(snapshot)
        if "delete" in package.runs:
            _run(package, "delete", _ids(name, repo, key, resource_id), secret_store.unseal(inputs),
                 json.loads(bound or "{}"), f"service {key} ({package.name}) of sbx-{name}")
        with state.db() as conn:
            secret_store.delete_rows(conn, "sandbox_services", "sandbox = ? and key = ?", (name, key))
        return True


def delete_all(name: str) -> list:
    """Delete the sandbox's resources, last made first. A record stays when
    its delete fails, for the reconciler to try again; returns the errors."""
    with state.db() as conn:
        rows = conn.execute("select key from sandbox_services where sandbox = ? order by position desc", (name,)).fetchall()
    errors = []
    for (key,) in rows:
        try:
            delete_one(name, key)
        except Exception as error:
            print(f"service {key} of sbx-{name}: delete: {error}", file=sys.stderr)
            errors.append(str(error))
    return errors


def reconcile(existing: set, min_age: int = 3600):
    """Delete the recorded resources of sandboxes that are gone, after the
    creation grace period. A shared server may hold other installations'
    resources: a name prefix is not proof of ownership, so unrecorded ones
    are left for explicit recovery."""
    with state.db() as conn:
        stale = {r[0] for r in conn.execute("select distinct sandbox from sandbox_services "
                                            "where updated_at < datetime('now', ?)", (f"-{int(min_age)} seconds",))}
    for name in stale - existing:
        print(f"reconcile: deleting the services of sbx-{name}, which is gone", file=sys.stderr)
        delete_all(name)


def _ready(name: str) -> list:
    """(key, uses, repo, package, outputs, bound, resource_id, sealed inputs) of the sandbox's ready resources."""
    with state.db() as conn:
        rows = conn.execute("select key, uses, repo, definition, outputs, sensitive, connections, resource_id, inputs "
                            "from sandbox_services where sandbox = ? and status = 'ready' order by position",
                            (name,)).fetchall()
    return [(key, uses, repo, Package.from_snapshot(definition),
             {**json.loads(outputs or "{}"), **(secret_store.unseal(sensitive) if sensitive else {})},
             json.loads(bound or "{}"), resource_id, inputs)
            for key, uses, repo, definition, outputs, sensitive, bound, resource_id, inputs in rows]


def scrub_paths(name: str) -> set:
    """What the packages of the sandbox's resources say no reusable snapshot
    of it keeps, without running their host code."""
    with state.db() as conn:
        rows = conn.execute("select definition from sandbox_services where sandbox = ? and status = 'ready'",
                            (name,)).fetchall()
    return {path for (definition,) in rows for path in Package.from_snapshot(definition).scrub}


def ready(name: str) -> dict:
    """key -> Created, for the sandbox's ready resources as they are."""
    return {key: Created(package, outputs) for key, _, _, package, outputs, *_ in _ready(name)}


def users(key: str) -> list:
    """sandbox:key of the resources made on the connection."""
    with state.db() as conn:
        rows = conn.execute("select sandbox, key, connections from sandbox_services").fetchall()
    return [f"sandbox {sandbox}:{service}" for sandbox, service, bound in rows if key in json.loads(bound or "{}").values()]


def reconcile_live(name: str, repo: str):
    """The repo's attachments of live packages, on an existing sandbox: made
    when missing, made again when their inputs or binding changed, without
    touching its other resources."""
    attached = []
    for key, uses, given, chosen in attachments.for_create(repo):
        try:
            package = resolve(uses, repo)
        except PackageError:
            continue
        if package.live:
            attached.append((key, uses, given, chosen, package))
    if not attached:
        return
    recorded = {row[0]: row for row in _ready(name)}
    for key, uses, given, chosen, package in attached:
        row = recorded.get(key)
        if row:
            label = f"service {key} ({uses}) of sbx-{name}"
            inputs = row[3].with_defaults(given, label)
            if row[1] == uses and secret_store.unseal(row[7]) == inputs and row[5] == attachments.bind(row[3], chosen, label)[0]:
                continue
            delete_one(name, key)  # a rebound identity's old capability goes, nothing else
        with state.db() as conn:
            position = conn.execute("select coalesce(max(position), -1) + 1 from sandbox_services where sandbox = ?",
                                    (name,)).fetchone()[0]
        create(name, repo, key, uses, given, position, chosen)


@dataclass(frozen=True)
class Resource:
    """One of the sandbox's resources, as a package's code sees it."""
    key: str
    outputs: dict
    connections: dict  # slot -> the bound connection's inputs, secret ones included


@dataclass(frozen=True)
class RuntimeContext:
    sandbox: str
    repo: str
    env: Mapping       # the sandbox's env so far
    folder: str        # where its runtime files are in the sandbox
    resources: tuple   # Resource, one per resource of the package
    base_url: str
    files: Mapping     # the package's own files, as its resources were made with

    def action_url(self, key: str, action: str) -> str:
        return f"{self.base_url.rstrip('/')}/api/sandbox/actions/{quote(self.sandbox, safe='')}/{quote(key, safe='')}/{action}"


@dataclass(frozen=True)
class ActionContext:
    sandbox: str
    resource: Resource
    token: str         # what the sandbox authenticated with: the token the platform issued the resource
    body: dict


class Unauthorized(Exception):
    """An action call without the sandbox's valid token for a live resource."""


def _template_layer(package: Package):
    t = package.template
    if not t:
        return None
    return contrib.TemplateLayer(apt=tuple(t.get("apt", ())), root=tuple(t.get("root", ())), user=tuple(t.get("user", ())),
                                 files={path: contrib.File(f["content"], f["mode"]) for path, f in t.get("files", {}).items()})


def contributions(target) -> list:
    """What a trusted repo's packages contribute: for its template, each
    attached package's template layer; for one of its sandboxes, one
    contribution per package it has resources of, with its template layer,
    its scrub paths and, when it has one, its runtime layer from all its
    resources."""
    if not target.trusted:
        return []
    if not target.name:
        attached = {}
        for _, uses in attachments.enabled(target.repo):
            try:
                package = resolve(uses, target.repo)
            except PackageError:  # the attachment shows its problem; the template goes without it
                continue
            attached.setdefault(package.name, package)
        return [contrib.Contribution(name, template=_template_layer(package))
                for name, package in attached.items() if package.template]
    live = {key for key, _ in attachments.enabled(target.repo)}
    grouped: dict = {}
    for key, _, _, package, outputs, bound, _, _ in _ready(target.name):
        if package.live and key not in live:
            continue
        grouped.setdefault(package.name, (package, []))[1].append(
            Resource(key, outputs, attachments.resolve(package, bound, f"service {key}")[0]))
    out = []
    for name, (package, found) in grouped.items():
        layer = None
        if package.runtime:
            context = RuntimeContext(target.name, target.repo, dict(target.env), contrib.folder(name), tuple(found),
                                     settings.current().base_url, package.files)
            given = code(package, package.runtime).runtime(context) or {}
            files = {path: contrib.File(text) for path, text in (given.get("files") or {}).items()}
            layer = contrib.RuntimeLayer(env=given.get("env") or {}, files=files)
        out.append(contrib.Contribution(name, template=_template_layer(package), runtime=layer,
                                        scrub_paths=tuple(package.scrub)))
    return out


def run_hooks(name: str, hook: str) -> list:
    """Run every resource's hook (each its own package version's); the failures, as text."""
    failed = []
    for key, uses, repo, package, _, bound, resource_id, inputs in _ready(name):
        if hook not in package.hooks:
            continue
        try:
            _run(package, hook, _ids(name, repo, key, resource_id), secret_store.unseal(inputs), bound,
                 f"service {key} ({uses}) of sbx-{name}")
        except Exception as error:
            failed.append(f"{key}: {hook}: {type(error).__name__}")
    return failed


def _authorized(name: str, key: str, action: str, token: str):
    row = next((r for r in _ready(name) if r[0] == key), None)
    if row is None or settings.trust(row[2]) != "trusted":
        raise Unauthorized("invalid sandbox credential")
    package, outputs = row[3], row[4]
    if package.source == "repo" or action not in package.actions or not hmac.compare_digest(outputs.get("token", ""), token):
        raise Unauthorized("invalid sandbox credential")
    return row


def action(name: str, key: str, action: str, token: str, body: dict | None = None) -> dict:
    """Call a package's action for the sandbox's resource key: authorized by
    the resource's token output, and only while the repo still attaches it,
    bound to the same connections. ValueError: the action refused (safe to show)."""
    if not token:
        raise Unauthorized("invalid sandbox credential")
    _authorized(name, key, action, token)
    with attachments.lock(shared=True), _resource_lock(name, key):
        row = _authorized(name, key, action, token)
        uses, repo, package, outputs, bound = row[1], row[2], row[3], row[4], row[5]
        active = next((a for a in attachments.for_create(repo) if a[0] == key and a[1] == uses), None)
        if active is None:
            raise Unauthorized("the sandbox's attachment was removed or disabled; prepare the sandbox again")
        if attachments.bind(package, active[3], f"service {key}")[0] != bound:
            raise Unauthorized("the attachment's binding changed; prepare the sandbox again")
        resource = Resource(key, outputs, attachments.resolve(package, bound, f"service {key}")[0])
        return getattr(code(package, package.actions[action]), action)(ActionContext(name, resource, token, body or {}))


def trial(name: str, inputs: dict, chosen: dict) -> dict:
    """A provision package's create, a second create (it must give the same
    outputs), list (it must name the resource) and delete, for a throwaway
    sandbox name on this host, as a real sandbox would; or a connection
    package's check. Each step's outcome; non-sensitive outputs shown."""
    from . import check, get
    package = get(name)
    if package.kind != "provision":
        why = check(package, package.with_defaults(inputs, name))
        return {"name": name, "kind": package.kind, "ok": not why,
                "steps": [{"step": "check", "ok": not why, "detail": why or "passed"}]}
    sandbox = f"t{secrets.token_hex(8)}"
    steps, shown, ok = [], {}, True
    try:
        created = create(sandbox, "test/test", "test", name, inputs, 0, chosen)
        shown = {k: (v if k in package.outputs and not package.outputs[k].get("sensitive") else "(sensitive)")
                 for k, v in created.outputs.items()}
        steps.append({"step": "create", "ok": True, "detail": "resource recorded; declared outputs present"})
        with state.db() as conn:
            given, bound, resource_id = conn.execute("select inputs, connections, resource_id from sandbox_services "
                                                     "where sandbox = ? and key = 'test'", (sandbox,)).fetchone()
        ids = _with_token(package, _ids(sandbox, "test/test", "test", resource_id))

        def run(what):
            return _run(package, what, ids, secret_store.unseal(given), json.loads(bound), f"test {name}")
        again = {**run("create")[1], **({TOKEN: ids["SBX_ACTION_TOKEN"]} if package.actions else {})}
        stable = again == created.outputs
        steps.append({"step": "retry", "ok": stable, "detail": "outputs stable" if stable else "retry changed outputs"})
        ok &= stable
        if "list" in package.runs:
            found = bool(resource_names(resource_id) & set(run("list")[0].split()))
            steps.append({"step": "list", "ok": found, "detail": "includes the owned resource" if found else "test resource absent"})
            ok &= found
    except PackageError as error:
        steps.append({"step": "provision", "ok": False, "detail": str(error)[-600:]})
        ok = False
    finally:
        errors = delete_all(sandbox)
        steps.append({"step": "delete", "ok": not errors, "detail": "; ".join(errors) or "owned resources deleted"})
        ok &= not errors
    return {"name": name, "kind": package.kind, "ok": ok, "sandbox": sandbox, "steps": steps, "outputs": shown}


SCHEMA = state.Schema(tables="""
create table if not exists sandbox_services (
    sandbox text not null, key text not null, uses text not null, position integer not null, repo text not null,
    definition text not null, inputs text not null, outputs text not null default '{}', sensitive text,
    status text not null,
    created_at text not null default current_timestamp, updated_at text not null default current_timestamp,
    primary key (sandbox, key));
""", columns=(("sandbox_services", "connections", "text not null default '{}'"),
              ("sandbox_services", "resource_id", "text not null default ''")),
    sealed=(("sandbox_services", "inputs"), ("sandbox_services", "sensitive")))
