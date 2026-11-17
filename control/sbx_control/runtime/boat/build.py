"""Templates on Boat: saved snapshots. build_template replays a TemplateSpec
in a builder sandbox (from the parent template's snapshot, or Boat's own
Ubuntu 24.04 for an image step), saves it under the template's name and
deletes the builder. A Dockerfile step goes through runtime.dockerfile; COPY
and ADD are refused. The start command is an enabled systemd unit, which
Boat starts again whenever a resume boots a new machine."""
from __future__ import annotations

import base64
import datetime
import hashlib
import posixpath
import re
import shlex
import time
import uuid

from ..base import CommandFailed, Failed, LimitRefused, Snapshot, TemplateGone, TemplateSpec, Timeout
from ..dockerfile import ShellWord, dockerfile_steps, export_line
from ..listening import listening
from .errors import POLL_SECONDS, BoatError
from .plans import LIMITS, MACHINES, PLAN_CACHE_SECONDS, TRIAL_REFUSAL, machine
from .sandbox import HOME, TEMPLATE_FILE, Handle

INSTALL_LABEL, TEMPLATE_LABEL, BUILD_LABEL = "sbx_install", "sbx_template", "sbx_build"
START_UNIT = "sbx-start"
CPU_KEY = next(s.key for s in LIMITS if s.field == "cpu")
BUILD_SECONDS = 4 * 3600   # the longest a builder runs; one older is an orphan
SAVED_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def saved_name(name: str) -> str:
    """A template's or snapshot's name as a Boat saved-snapshot name (1-63
    lowercase letters, digits or dashes): as is when it is one (sbx's
    template names are), else made one, the same every time."""
    if SAVED_NAME.fullmatch(name):
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return (re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:54].strip("-") or "s") + "-" + digest


def given(**fields) -> dict:
    """The fields that have a value: a request model sends a field set to
    None as null (Boat's ttlSeconds null: auto-stop off)."""
    return {k: v for k, v in fields.items() if v is not None}


def _installation() -> str:
    from .. import installation_id
    return installation_id()


class Builds:
    """The template part of the Boat backend (mixed into Boat)."""

    def _save(self, sandbox_id: str, name: str, seconds: float = 3600) -> str:
        from boat_sdk.models import SavedSnapshotSaveRequest
        name = saved_name(name)
        self.call(lambda: self.api().save_saved_snapshot(SavedSnapshotSaveRequest(sandbox_id=sandbox_id, name=name)),
                  sandbox_id=sandbox_id)
        deadline = time.monotonic() + seconds
        while True:
            found = self.call(lambda: self.api().get_saved_snapshot(name)).snapshot
            if found.status == "ready":
                return name
            if found.status == "failed":
                raise Failed(f"Boat could not save {name}: {found.error or 'failed'}")
            if time.monotonic() > deadline:
                raise Timeout(f"Boat was still saving {name} after {int(seconds)} s")
            time.sleep(POLL_SECONDS)

    def snapshot(self, sandbox_id, name):
        self._refuse_on_trial()
        return Snapshot(self._save(sandbox_id, name))

    def plan(self, fresh: bool = False) -> str:
        """Boat's plan key for the account ("trial" before the first payment), a few minutes old at most."""
        when, found = self._plan
        if fresh or not found or time.monotonic() - when > PLAN_CACHE_SECONDS:
            found = self.call(lambda: self.api().limits(**self._org_args())).sandbox_plan_key or ""
            self._plan = (time.monotonic(), found)
        return found

    def _refuse_on_trial(self):
        """A save is refused on the trial: say so before a builder is started for nothing."""
        if self.plan() == "trial":
            raise LimitRefused(TRIAL_REFUSAL, field="plan")

    def delete_snapshot(self, snapshot_id):
        self.delete_template(snapshot_id)

    def templates(self):
        found = self.call(lambda: self.api().list_saved_snapshots())
        return [(s.name, s.status) for s in found.snapshots]

    def delete_template(self, name):
        try:
            self.call(lambda: self.api().delete_saved_snapshot(saved_name(name)))
        except BoatError as error:
            if error.gone():
                return False
            raise
        return True

    def build_template(self, spec: TemplateSpec, *, alias, cpu, memory_mb, disk_mb, on_log=None):
        """Replay spec in a builder sandbox, save it as alias, delete the
        builder. Refused on the trial before a builder starts; builders this
        installation left behind (older than BUILD_SECONDS) are deleted first."""
        from boat_sdk.models import CreateSandboxRequest
        log = on_log or (lambda line: print(line, flush=True))
        self._refuse_on_trial()
        self._reclaim_builders(log)
        steps = []
        for what, value in spec.steps:
            steps += dockerfile_steps(value) if what == "dockerfile" else [(what, value)]
        parent = next((value for what, value in steps if what == "template"), None)
        image = next((value for what, value in steps if what == "image"), None)
        if image is not None and not re.fullmatch(r"(docker\.io/(library/)?)?ubuntu:24\.04", image):
            raise Failed(f"Boat starts every sandbox from its own Ubuntu 24.04; a template from {image} can't be built "
                         "there. Start it from ubuntu:24.04 or from another template.")
        kind = machine(cpu, memory_mb, disk_mb)
        if kind not in [m[0] for m in MACHINES if m[1] <= (self._limits.cpu or 16)]:
            raise LimitRefused(f"a {cpu} vCPU template needs Boat's {kind} machines, beyond {CPU_KEY}",
                               field="cpu", asked=cpu, allowed=self._limits.cpu)
        request = CreateSandboxRequest(type=kind, ttl_seconds=min(BUILD_SECONDS, self._limits.timeout_seconds or BUILD_SECONDS),
                                       no_env=True, environment=None, name=f"build {alias}"[:120],
                                       labels={INSTALL_LABEL: _installation(), BUILD_LABEL: alias},
                                       **given(var_from=saved_name(parent) if parent else None), **self._org_args())
        log(f"Boat: starting a {kind} builder" + (f" from {parent}" if parent else " from Boat's Ubuntu 24.04"))
        once = str(uuid.uuid4())   # one key for every retry
        try:
            created = self.call(lambda: self.api().create(idempotency_key=once, create_sandbox_request=request),
                                asked={"cpu": cpu, "memory_mb": memory_mb, "disk_mb": disk_mb})
        except BoatError as error:
            if parent and error.gone_snapshot():
                raise TemplateGone(parent, f"Boat has no saved snapshot {saved_name(parent)}") from error
            raise
        builder = created.sandbox.id
        try:
            self._ready(builder)
            self._replay(Handle(self, builder), steps, log)
            log(f"Boat: saving {alias}")
            self._save(builder, alias)
            log(f"Boat: {alias} is ready")
            return alias
        finally:
            try:
                self.kill(builder)
            except Exception as error:   # its auto-stop ends it; Boat keeps its disk 14 days
                log(f"Boat: could not delete builder {builder}: {error}")

    def _reclaim_builders(self, log):
        """Delete this installation's builders older than BUILD_SECONDS: a
        build that died with sbx-control left them (labelled sbx_build)."""
        try:
            found = self.call(lambda: self.api().sandboxes(limit=200, label=[f"{INSTALL_LABEL}={_installation()}"],
                                                          **self._org_args())).sandboxes
        except Exception as error:   # the build goes on; the next one looks again
            log(f"Boat: could not list old builders: {error}")
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        for sandbox in found:
            if BUILD_LABEL in (sandbox.labels or {}) and sandbox.created_at and \
                    (now - sandbox.created_at).total_seconds() > BUILD_SECONDS:
                log(f"Boat: deleting builder {sandbox.id}, left from an earlier build")
                try:
                    self.kill(sandbox.id)
                except Exception as error:
                    log(f"Boat: could not delete builder {sandbox.id}: {error}")

    def _replay(self, handle: Handle, steps: list, log):
        # env: every ENV's pairs in order, a key set again kept where it was too, so a value
        # that took it ($A) keeps what it was then, as in Docker.
        user, workdir, env, inherited = "user", HOME, [], ""
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
                # A Dockerfile's (a ShellWord) as the path its $VAR give, with the env so far; a
                # relative one under the workdir before it, as Docker has it.
                given = commands.run(f"printf %s {value}", timeout=60).stdout if isinstance(value, ShellWord) else value
                workdir = posixpath.join(workdir, given)
                commands.run(f"sudo mkdir -p {shlex.quote(workdir)} && sudo chown {shlex.quote(user)} "
                             f"{shlex.quote(workdir)} 2>/dev/null || true", timeout=60)
            elif what == "env":
                env += value.items()
            elif what == "run":   # the env so far is in TEMPLATE_FILE, which every command reads
                log(f"RUN {value[:200]}")
                commands.run(value, user=user, cwd=workdir, timeout=0, on_output=lambda text: [
                    log(line) for line in text.splitlines() if line.strip()])
            elif what == "start":
                self._persist(commands, env, workdir, inherited)
                command, ready = value
                self._start_unit(handle, command, ready, log)
                continue
            else:
                raise ValueError(f"unknown template step {what}")
            self._persist(commands, env, workdir, inherited)

    @staticmethod
    def _inherited(commands) -> tuple[str, str]:
        """The parent template's env (its TEMPLATE_FILE but the workdir) and its workdir."""
        text = commands.run(f"cat {TEMPLATE_FILE} 2>/dev/null || true", timeout=60).stdout
        lines, workdir = [], HOME
        for line in text.splitlines():
            if line.startswith("export SBX_TEMPLATE_WORKDIR="):
                workdir = shlex.split(line)[1].partition("=")[2] or HOME
            elif line.strip():
                lines.append(line + "\n")
        return "".join(lines), workdir

    @staticmethod
    def _persist(commands, env: list, workdir: str, inherited: str = ""):
        """The template's env ((key, value) in order) and workdir as every
        later command and shell sees them: the parent's env (inherited)
        first, the template's own after it."""
        text = inherited + "".join(export_line(k, v) for k, v in env)
        text += f"export SBX_TEMPLATE_WORKDIR={shlex.quote(workdir)}\n"
        encoded = base64.b64encode(text.encode()).decode()
        commands.run(f"sudo mkdir -p /etc/sbx && echo {encoded} | base64 -d | sudo tee {TEMPLATE_FILE} >/dev/null "
                     f"&& sudo chmod 644 {TEMPLATE_FILE} && echo '[ -r {TEMPLATE_FILE} ] && . {TEMPLATE_FILE}' | "
                     "sudo tee /etc/profile.d/sbx-template.sh >/dev/null", timeout=60)

    @staticmethod
    def _start_unit(handle: Handle, command: str, ready, log):
        """The start command as an enabled systemd unit (Boat starts enabled
        units again when a resume boots a new machine), started now and waited for: restarted,
        as a builder from a parent template runs the parent's already."""
        script = f"#!/bin/bash\n[ -r {TEMPLATE_FILE} ] && . {TEMPLATE_FILE}\ncd \"${{SBX_TEMPLATE_WORKDIR:-$HOME}}\"\n" \
                 f"exec bash -c {shlex.quote(command)}\n"
        unit = ("[Unit]\nDescription=sbx start command\nAfter=network.target\n[Service]\nUser=user\n"
                "ExecStart=/bin/bash /etc/sbx/start.sh\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n")
        commands = handle.commands
        for path, text in (("/etc/sbx/start.sh", script), (f"/etc/systemd/system/{START_UNIT}.service", unit)):
            encoded = base64.b64encode(text.encode()).decode()
            commands.run(f"echo {encoded} | base64 -d | sudo tee {path} >/dev/null", timeout=60)
        commands.run(f"sudo systemctl daemon-reload && sudo systemctl enable {START_UNIT} && sudo systemctl restart {START_UNIT}", timeout=120)
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
                log("Boat: the start command is ready")
                return
            time.sleep(POLL_SECONDS)
        raise Failed(f"the template's start command was not ready after 300 s "
                     f"({'nothing on port ' + str(ready) if isinstance(ready, int) else ready})")
