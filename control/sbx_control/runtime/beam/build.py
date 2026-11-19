"""Templates on Beam: images. Beam names no image and lists none, so the
backend keeps each template's image id, size and start command in a file of
its own on this server (Templates); a template that file no longer names is
built again by the scheduler, as any template a runtime no longer lists.

build_template replays a TemplateSpec in a builder sandbox, as root: from
the parent template's image, or, for an image step, from that registry image
as Beam's image service imports it (/images/verify-build, /images/build).
The builder's filesystem is then saved as an image
(/pods/ID/create-image-from-filesystem) and the builder deleted. A
Dockerfile step goes through runtime.dockerfile; COPY and ADD are refused
there. A Beam container runs one idle process and has no init, so a
template's start command is a script (START_FILE) that create runs in every
new sandbox."""
from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import posixpath
import shlex
import threading
import time

from ..base import CommandFailed, Failed, LimitRefused, Snapshot, TemplateGone, TemplateSpec, Timeout
from ..dockerfile import ShellWord, dockerfile_steps, export_line
from ..listening import listening
from .sandbox import TEMPLATE_FILE, Handle

START_FILE = "/etc/sbx/start.sh"
START = f"exec bash {START_FILE} >/var/log/sbx-start.log 2>&1"   # run detached (background), as root
BUILD_SECONDS = 4 * 3600   # the longest a builder runs before Beam ends it
READY_SECONDS = 300


def image_uri(image: str) -> str:
    """A Docker image name as the registry URI Beam's image service takes (docker.io/library/ubuntu:24.04)."""
    first, slash, _ = image.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        return image
    return f"docker.io/{image}" if slash else f"docker.io/library/{image}"


class Templates:
    """name -> {"image", "cpu", "memory_mb", "start"}: this installation's
    templates and snapshots on Beam, in one JSON file (0600), written whole.
    A change holds the file's lock, so a build in another thread or process
    (sbx-admin jobs run) never loses one."""
    _threads = threading.Lock()

    def __init__(self, path):
        self.path = path

    @contextlib.contextmanager
    def _changing(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._threads, open(self.path.with_name(self.path.name + ".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def all(self) -> dict:
        try:
            found = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def get(self, name: str) -> dict | None:
        found = self.all().get(name)
        return found if isinstance(found, dict) and found.get("image") else None

    def _write(self, found: dict) -> None:
        fresh = self.path.with_name(self.path.name + ".new")
        with open(os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
            json.dump(found, file, indent=1, sort_keys=True)
        os.replace(fresh, self.path)

    def put(self, name: str, entry: dict) -> None:
        with self._changing():
            self._write({**self.all(), name: entry})

    def drop(self, name: str) -> bool:
        with self._changing():
            found = self.all()
            if found.pop(name, None) is None:
                return False
            self._write(found)
            return True


class Builds:
    """The template part of the Beam backend (mixed into Beam)."""

    def templates(self):
        return [(name, "ready") for name, entry in sorted(self.index().all().items())
                if isinstance(entry, dict) and entry.get("image")]

    def delete_template(self, name):
        """Forgotten here; Beam's API deletes no image (its storage stays Beam's to collect)."""
        return self.index().drop(name)

    def delete_snapshot(self, snapshot_id):
        self.index().drop(snapshot_id)

    def _save(self, sandbox_id: str, stub_id: str) -> str:
        found = self.sandbox_call(sandbox_id, "POST", "/create-image-from-filesystem", {"stubId": stub_id},
                                  timeout=3600)
        if not found.get("imageId"):
            raise Failed(f"Beam saved sandbox {sandbox_id}'s filesystem without naming the image")
        return found["imageId"]

    def snapshot(self, sandbox_id, name):
        """The sandbox's filesystem as an image new sandboxes start from (its processes are not in it)."""
        meta = self._meta_of(sandbox_id)
        source = self.index().get(meta.get("sbx_template", "")) or {}
        image = self._save(sandbox_id, self._stub_of(sandbox_id))
        self.index().put(name, {"image": image, "cpu": source.get("cpu") or 1,
                                "memory_mb": source.get("memory_mb") or 1024, "start": bool(source.get("start"))})
        return Snapshot(name)

    def _image(self, image: str, log) -> str:
        """The registry image as a Beam image id, built (imported) when Beam has none yet."""
        request = {"existingImageUri": image_uri(image), "ignorePython": True}
        found = self.api().call("POST", "/images/verify-build", request, timeout=120)
        if found.get("valid") is False:
            raise Failed(f"Beam can't build from the image {image}")
        if found.get("exists") and found.get("imageId"):
            return found["imageId"]
        log(f"Beam: importing {request['existingImageUri']}")
        last = {}
        for message in self.api().lines("/images/build", request):
            if message.get("msg") and not message.get("done"):
                log(str(message["msg"]).rstrip())
            if message.get("done"):
                last = message
        if not last.get("success"):
            raise Failed(f"Beam could not import the image {image}: {last.get('msg') or 'the build did not finish'}")
        return last.get("imageId") or found.get("imageId") or ""

    def build_template(self, spec: TemplateSpec, *, alias, cpu, memory_mb, disk_mb, on_log=None):
        log = on_log or (lambda line: print(line, flush=True))
        steps = []
        for what, value in spec.steps:
            steps += dockerfile_steps(value) if what == "dockerfile" else [(what, value)]
        parent = next((value for what, value in steps if what == "template"), None)
        image = next((value for what, value in steps if what == "image"), None)
        limits = self.limits()
        for field, asked in (("cpu", cpu), ("memory_mb", memory_mb)):
            most = getattr(limits, field)
            if most is not None and asked and asked > most:
                raise LimitRefused(f"a template with {field} {asked} is beyond {limits.keys.get(field, field)}={most}",
                                   field=field, asked=asked, allowed=most)
        inherited = None
        if parent is not None:
            inherited = self.index().get(parent)
            if inherited is None:
                raise TemplateGone(parent, f"Beam has no image for the template {parent}")
            base = inherited["image"]
        elif image is not None:
            base = self._image(image, log)
        else:
            raise Failed("a template starts from an image or from another template")
        log(f"Beam: starting a builder ({cpu} vCPUs, {memory_mb} MiB) from {parent or image}")
        builder, stub = self._start(base, cpu=cpu, memory_mb=memory_mb, timeout=BUILD_SECONDS, envs=None)
        try:
            started = self._replay(Handle(self, builder), steps, log)
            log(f"Beam: saving {alias}")
            self.index().put(alias, {"image": self._save(builder, stub), "cpu": cpu, "memory_mb": memory_mb,
                                     "start": started or bool(inherited and inherited.get("start"))})
            log(f"Beam: {alias} is ready")
            return alias
        finally:
            try:
                self.kill(builder)
            except Exception as error:   # its timeout ends it
                log(f"Beam: could not delete builder {builder}: {error}")

    def _replay(self, handle: Handle, steps: list, log) -> bool:
        """Run the steps in the builder; whether the template has a start command of its own."""
        user, workdir, env, inherited, started = "root", "/", [], "", False
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
                continue
            elif what == "start":
                self._persist(commands, env, workdir, inherited)
                self._start_command(handle, *value, log)
                started = True
                continue
            else:
                raise ValueError(f"unknown template step {what}")
            self._persist(commands, env, workdir, inherited)
        return started

    @staticmethod
    def _inherited(commands) -> tuple[str, str]:
        """The parent template's env (its TEMPLATE_FILE but the workdir) and its workdir."""
        text = commands.run(f"cat {TEMPLATE_FILE} 2>/dev/null || true", user="root", timeout=60).stdout
        lines, workdir = [], "/"
        for line in text.splitlines():
            if line.startswith("export SBX_TEMPLATE_WORKDIR="):
                workdir = shlex.split(line)[1].partition("=")[2] or "/"
            elif line.strip():
                lines.append(line + "\n")
        return "".join(lines), workdir

    @staticmethod
    def _put(commands, path: str, text: str) -> None:
        encoded = base64.b64encode(text.encode()).decode()
        commands.run(f"mkdir -p {shlex.quote(posixpath.dirname(path))} && echo {encoded} | base64 -d > "
                     f"{shlex.quote(path)} && chmod 644 {shlex.quote(path)}", user="root", cwd="/", timeout=60)

    def _persist(self, commands, env: list, workdir: str, inherited: str = ""):
        """The template's env and workdir as every later command sees them (TEMPLATE_FILE)."""
        text = inherited + "".join(export_line(pairs) for pairs in env)
        self._put(commands, TEMPLATE_FILE, text + f"export SBX_TEMPLATE_WORKDIR={shlex.quote(workdir)}\n")
        self._put(commands, "/etc/profile.d/sbx-template.sh", f"[ -r {TEMPLATE_FILE} ] && . {TEMPLATE_FILE}\n")

    def _start_command(self, handle: Handle, command: str, ready, log):
        """The start command as START_FILE, started now and waited for, as it is in every new sandbox."""
        self._put(handle.commands, START_FILE, f"#!/bin/bash\n[ -r {TEMPLATE_FILE} ] && . {TEMPLATE_FILE}\n"
                  f"cd \"${{SBX_TEMPLATE_WORKDIR:-/}}\"\nexec bash -c {shlex.quote(command)}\n")
        handle.commands.run(START, user="root", cwd="/", background=True).disconnect()
        deadline = time.monotonic() + READY_SECONDS
        while time.monotonic() < deadline:
            try:
                if isinstance(ready, int):
                    up = listening(handle, ready, timeout=30) is not None
                else:
                    handle.commands.run(ready, timeout=30)
                    up = True
            except (CommandFailed, Timeout):
                up = False
            if up:
                log("Beam: the start command is ready")
                return
            time.sleep(2)
        raise Failed(f"the template's start command was not ready after {READY_SECONDS} s "
                     f"({'nothing on port ' + str(ready) if isinstance(ready, int) else ready})")
