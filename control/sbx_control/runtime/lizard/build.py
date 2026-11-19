"""Templates on Lizard: snapshots. Lizard has no template upload ("The
public CLI does not provide a custom-template upload flow", lizard-sdk
docs/sandboxes.md), so build_template replays a TemplateSpec in a builder
sandbox (from the parent template's snapshot, or Lizard's own base template
for an image step), snapshots it under the template's name and deletes the
builder. A snapshot keeps the files only, so the start command is a script
in the template (lifecycle.START_FILE) that the backend runs after every
boot. A Dockerfile step goes through runtime.dockerfile; COPY and ADD are
refused there.

A snapshot is found by its name in the project (Lizard addresses one by id
and lets names repeat): the newest ready one counts, and a build deletes the
older ones of its name once the new one is ready."""
from __future__ import annotations

import base64
import posixpath
import re
import shlex
import time

from ..base import CommandFailed, Failed, LimitRefused, Snapshot, TemplateGone, TemplateSpec, Timeout
from ..dockerfile import ShellWord, dockerfile_steps, export_line
from ..listening import listening
from .errors import POLL_SECONDS, LizardError
from .lifecycle import ON_TIMEOUT_KEY, START_FILE
from .plans import LIMITS, SIZES, size
from .sandbox import HOME, TEMPLATE_FILE, USER, Handle

INSTALL_KEY, BUILD_KEY = "sbx_install", "sbx_build"
BUILD_SECONDS = 4 * 3600   # a builder's lifetime at Lizard: one that outlives its build is deleted there
SNAPSHOT_SECONDS = 11 * 60   # lizard-sdk sandbox.py snapshot(): the server allows 10 minutes
SYSTEM_IMAGE = re.compile(r"(docker\.io/(library/)?)?(ubuntu|debian)(:[\w.-]+)?")
SUDOERS = f"echo '{USER} ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/sbx-user && chmod 440 /etc/sudoers.d/sbx-user"
_KEYS = {s.field: s.key for s in LIMITS}


def _installation() -> str:
    from .. import installation_id
    return installation_id()


class Builds:
    """The template part of the Lizard backend (mixed into Lizard)."""

    def _snapshots(self) -> list:
        return self.call("GET", f"/api/projects/{self.project()}/snapshots") or []

    def _snapshot(self, name: str) -> dict | None:
        """The newest ready snapshot of that name in the project."""
        found = [s for s in self._snapshots() if s.get("name") == name and s.get("status") == "ready"]
        return max(found, key=lambda s: s.get("createdAt") or 0, default=None)

    def _save(self, sandbox_id: str, name: str, seconds: float = 900) -> dict:
        """Snapshot the sandbox as name and wait until it is ready; the
        older snapshots of the name go once it is."""
        # poolSize: warm copies of an older kind of snapshot; Firecracker's ignore it (lizard-sdk).
        saved = self.call("POST", f"/api/sandboxes/{sandbox_id}/snapshot", {"name": name, "poolSize": 1},
                          sandbox_id=sandbox_id, timeout=SNAPSHOT_SECONDS) or {}
        if not saved.get("id"):
            raise Failed(f"Lizard saved {name} without a snapshot id")
        deadline = time.monotonic() + seconds
        while saved.get("status") != "ready":
            if saved.get("status") == "failed":
                raise Failed(f"Lizard could not save {name}: {saved.get('error') or 'failed'}")
            if time.monotonic() > deadline:
                raise Timeout(f"Lizard was still saving {name} after {int(seconds)} s ({saved.get('status')})")
            time.sleep(POLL_SECONDS)
            saved = self.call("GET", f"/api/sandbox-snapshots/{saved['id']}") or {}
        for older in self._snapshots():
            if older.get("name") == name and older.get("id") != saved["id"]:
                try:
                    self.call("DELETE", f"/api/sandbox-snapshots/{older['id']}")
                except Failed as error:   # it stays, older than the one create picks
                    print(f"Lizard: could not delete the older snapshot {older.get('id')} of {name}: {error}",
                          flush=True)
        return saved

    def snapshot(self, sandbox_id, name):
        self._save(sandbox_id, name)
        return Snapshot(name)

    def delete_snapshot(self, snapshot_id):
        self.delete_template(snapshot_id)

    def templates(self):
        newest: dict = {}
        for found in sorted(self._snapshots(), key=lambda s: s.get("createdAt") or 0):
            if found.get("name"):
                newest[found["name"]] = found.get("status") or ""
        return list(newest.items())

    def delete_template(self, name):
        """Every snapshot of that name; False when there was none."""
        found = [s for s in self._snapshots() if s.get("name") == name]
        for snapshot in found:
            try:
                self.call("DELETE", f"/api/sandbox-snapshots/{snapshot['id']}")
            except LizardError as error:
                if not error.gone():
                    raise
        return bool(found)

    def _size(self) -> str:
        """The installation's size: the smallest of Lizard's that holds the limits."""
        return size(self._limits.cpu, self._limits.memory_mb, self._limits.disk_mb)

    def build_template(self, spec: TemplateSpec, *, alias, cpu, memory_mb, disk_mb, on_log=None):
        """Replay spec in a builder sandbox, snapshot it as alias, delete the
        builder. A template without a parent is built at the installation's
        size; one from a parent runs at the parent's, which Lizard decides,
        and is refused when that is smaller than what it asks for."""
        log = on_log or (lambda line: print(line, flush=True))
        steps = []
        for what, value in spec.steps:
            steps += dockerfile_steps(value) if what == "dockerfile" else [(what, value)]
        parent = next((value for what, value in steps if what == "template"), None)
        image = next((value for what, value in steps if what == "image"), None)
        if image is not None and not SYSTEM_IMAGE.fullmatch(image):
            raise Failed(f"Lizard starts every sandbox from its own templates and imports no image; a template "
                         f"from {image} can't be built there. Start it from another template.")
        mine = self._size()
        wanted = size(cpu, memory_mb, disk_mb)   # LimitRefused when Lizard has none that large
        names = [s[0] for s in SIZES]
        if names.index(wanted) > names.index(mine):
            raise LimitRefused(
                f"this template asks for {cpu} vCPUs, {memory_mb} MiB and {disk_mb} MiB of disk (Lizard's {wanted}), "
                f"and this installation's Lizard sandboxes are {mine}: raise {_KEYS['cpu']}, {_KEYS['memory_mb']} "
                f"and {_KEYS['disk_mb']} to that size, then rebuild the base template",
                field="cpu", asked=cpu, allowed=self._limits.cpu)
        body = {"projectId": self.project(),
                "timeoutMs": min(BUILD_SECONDS, self._limits.timeout_seconds or BUILD_SECONDS) * 1000,
                "metadata": {INSTALL_KEY: _installation(), BUILD_KEY: alias, ON_TIMEOUT_KEY: "kill"}}
        if parent:
            found = self._snapshot(parent)
            if found is None:
                raise TemplateGone(parent, f"Lizard has no snapshot named {parent} in project {self.project()}")
            body.update(snapshotId=found["id"], **({"region": found["region"]} if found.get("region") else {}))
            log(f"Lizard: starting a builder from {parent}")
        else:
            body.update(template=self._base_template, size=mine, region=self._region)
            log(f"Lizard: starting a {mine} builder from Lizard's {self._base_template} template"
                + (f" (not from {image}: Lizard imports no image)" if image else ""))
        created = self.call("POST", "/api/sandboxes", body,
                            asked={"cpu": cpu, "memory_mb": memory_mb, "disk_mb": disk_mb}) or {}
        builder = created.get("sandboxId") or created.get("id")
        if not builder:
            raise Failed("Lizard created a builder without an id")
        try:
            running = self._ready(builder)
            self._fits(parent, running, cpu, memory_mb)
            self._machine_ready(builder, start=False)
            handle = Handle(self, builder)
            if not parent:
                self._prepare(handle, log)
            self._replay(handle, steps, log)
            log(f"Lizard: saving {alias}")
            self._save(builder, alias)
            log(f"Lizard: {alias} is ready")
            return alias
        finally:
            try:
                self.kill(builder)
            except Exception as error:   # its lifetime (BUILD_SECONDS) ends it
                log(f"Lizard: could not delete builder {builder}: {error}")

    def _fits(self, parent, running: dict, cpu: int, memory_mb: int):
        """A builder from a parent runs at the parent's size: refuse a template that asks for more."""
        has_cpu, has_memory = running.get("cpus"), running.get("memoryMb")
        if parent and ((isinstance(has_cpu, (int, float)) and has_cpu < cpu)
                       or (isinstance(has_memory, (int, float)) and has_memory < memory_mb)):
            raise LimitRefused(
                f"Lizard runs a sandbox from a snapshot at the size the snapshot was taken at: {parent} has "
                f"{has_cpu} vCPUs and {has_memory} MiB, and this template asks for {cpu} vCPUs and {memory_mb} MiB. "
                f"Rebuild the base template at the installation's size ({self._size()}), then this one",
                field="cpu", asked=cpu, allowed=int(has_cpu) if isinstance(has_cpu, (int, float)) else None)

    @staticmethod
    def _prepare(handle: Handle, log):
        """What sbx's templates count on that Lizard's base may not have:
        apt (the steps install with it) and sudo without a password for the
        unprivileged account, as on sbx's other runtimes."""
        commands = handle.commands
        system = commands.run(". /etc/os-release 2>/dev/null; echo \"${PRETTY_NAME:-unknown}\"", user="root",
                              cwd="/", timeout=60).stdout.strip()
        log(f"Lizard: the base system is {system}")
        try:
            commands.run("command -v apt-get >/dev/null", user="root", cwd="/", timeout=60)
        except CommandFailed:
            raise Failed(f"Lizard's base template ({system}) has no apt-get; sbx's templates install with it") from None
        commands.run(f"mkdir -p /etc/sudoers.d /etc/sbx && {SUDOERS}", user="root", cwd="/", timeout=60)

    def _replay(self, handle: Handle, steps: list, log):
        # env: every ENV's pairs in order, one export per ENV, so a value that took a key ($A)
        # keeps what it was before that ENV, as in Docker.
        user, workdir, env, inherited = USER, HOME, [], ""
        commands = handle.commands
        for what, value in steps:
            if what == "template":
                inherited, workdir = self._inherited(commands)
                continue
            if what == "image":
                continue
            if what == "user":
                user = value
            elif what == "workdir":
                given = (commands.run(f"printf %s {value}", user="root", timeout=60).stdout
                         if isinstance(value, ShellWord) else value)
                workdir = posixpath.join(workdir, given)
                commands.run(f"mkdir -p {shlex.quote(workdir)} && chown {shlex.quote(user)} {shlex.quote(workdir)} "
                             "2>/dev/null || true", user="root", cwd="/", timeout=60)
            elif what == "env":
                env.append(list(value.items()))
            elif what == "run":   # the env so far is in TEMPLATE_FILE, which every command reads
                log(f"RUN {value[:200]}")
                commands.run(value, user=user, cwd=workdir, timeout=0, on_output=lambda text: [
                    log(line) for line in text.splitlines() if line.strip()])
            elif what == "start":
                self._persist(commands, env, workdir, inherited)
                command, ready = value
                self._start(handle, command, ready, log)
                continue
            else:
                raise ValueError(f"unknown template step {what}")
            self._persist(commands, env, workdir, inherited)

    @staticmethod
    def _inherited(commands) -> tuple[str, str]:
        """The parent template's env (its TEMPLATE_FILE but the workdir) and its workdir."""
        text = commands.run(f"cat {TEMPLATE_FILE} 2>/dev/null || true", user="root", cwd="/", timeout=60).stdout
        lines, workdir = [], HOME
        for line in text.splitlines():
            if line.startswith("export SBX_TEMPLATE_WORKDIR="):
                workdir = shlex.split(line)[1].partition("=")[2] or HOME
            elif line.strip():
                lines.append(line + "\n")
        return "".join(lines), workdir

    @staticmethod
    def _persist(commands, env: list, workdir: str, inherited: str = ""):
        """The template's env and workdir as every later command and shell
        sees them: the parent's env (inherited) first, the template's own after."""
        text = inherited + "".join(export_line(pairs) for pairs in env)
        text += f"export SBX_TEMPLATE_WORKDIR={shlex.quote(workdir)}\n"
        encoded = base64.b64encode(text.encode()).decode()
        commands._exec(["bash", "-c", f"mkdir -p /etc/sbx /etc/profile.d && echo {encoded} | base64 -d >{TEMPLATE_FILE} "
                        f"&& chmod 644 {TEMPLATE_FILE} && echo '[ -r {TEMPLATE_FILE} ] && . {TEMPLATE_FILE}' "
                        ">/etc/profile.d/sbx-template.sh"], user="root", seconds=60)

    @staticmethod
    def _start(handle: Handle, command: str, ready, log):
        """The start command as START_FILE, which the backend runs after every
        boot (lifecycle.BOOT); run now and waited for, so a template whose
        start command never gets ready is not saved."""
        script = (f"#!/bin/bash\n[ -r {TEMPLATE_FILE} ] && . {TEMPLATE_FILE}\ncd \"${{SBX_TEMPLATE_WORKDIR:-$HOME}}\"\n"
                  f"exec bash -c {shlex.quote(command)}\n")
        commands = handle.commands
        encoded = base64.b64encode(script.encode()).decode()
        commands._exec(["bash", "-c", f"echo {encoded} | base64 -d >{START_FILE} && chmod 755 {START_FILE}"],
                       user="root", seconds=60)
        commands.run(f"bash {START_FILE}", cwd="/", background=True, timeout=0).disconnect()
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                if isinstance(ready, int):
                    up = listening(handle, ready, timeout=30) is not None
                else:
                    commands.run(ready, timeout=30)
                    up = True
            except (CommandFailed, Timeout):
                up = False
            if up:
                log("Lizard: the start command is ready")
                return
            time.sleep(POLL_SECONDS)
        raise Failed(f"the template's start command was not ready after 300 s "
                     f"({'nothing on port ' + str(ready) if isinstance(ready, int) else ready})")
