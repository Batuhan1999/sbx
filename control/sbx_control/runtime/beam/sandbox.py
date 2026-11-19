"""A Beam sandbox as sbx's Handle: commands and files through Beam's API.

Beam runs a command as the container's user (root) and takes its env and
working directory with it, so a command's values are never in its text.
wrap() makes each one a shell command that loads the template's env and
workdir first (TEMPLATE_FILE) and runs as sbx's default user (`user`, which
the base template makes; root until then) or the one asked for.

A command is started detached (exec with "wait": false) and polled: Beam's
status answers exit code -1 while it runs, and its stdout and stderr routes
hand out what was written since they were last read. A file is written in
4 MiB chunks to a staging path (the upload route), then moved into place by
one command, and read through a command (base64): the download route takes
the file's path inside the URL, where an absolute path's leading slash
makes a double slash that is not known to arrive intact."""
from __future__ import annotations

import base64
import shlex
import time
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING

from ..base import CommandFailed, Timeout, command_name
from .api import READY_SECONDS

if TYPE_CHECKING:
    from . import Beam

DEFAULT_USER = "user"
TEMPLATE_FILE = "/etc/sbx/template.sh"   # a template's env and workdir, sourced by every command
CHUNK = 4 * 1024 * 1024                  # an upload's chunk, as Beam's SDK sends them
# Statuses of an ended process, for an answer without an exit code (beta9's SANDBOX_TERMINAL_STATUSES).
ENDED = ("complete", "completed", "exited", "failed", "error", "stopped", "terminated")


def wrap(cmd: str, *, user: str | None = None, cwd: str | None = None) -> str:
    """cmd as the one command line Beam's exec takes: the template's env and
    workdir first, then cwd (whose failure stops it); as `user` through su,
    which keeps the env Beam gave the process. Without a user: sbx's
    default one when the image has it, else the container's own."""
    where = f"cd {shlex.quote(cwd)} || exit 1; " if cwd else 'cd "${SBX_TEMPLATE_WORKDIR:-$HOME}" 2>/dev/null; '
    script = f"[ -r {TEMPLATE_FILE} ] && . {TEMPLATE_FILE}; {where}{cmd}"
    if user == "root":
        return f"bash -c {shlex.quote(script)}"
    switch = f"exec su -s /bin/bash -c \"$1\" {shlex.quote(user or DEFAULT_USER)}"
    if user is None:
        switch = f"if id -u {DEFAULT_USER} >/dev/null 2>&1; then {switch}; else exec bash -c \"$1\"; fi"
    return f"bash -c {shlex.quote(switch)} sbx {shlex.quote(script)}"


class _Background:
    """A detached command: it keeps running after disconnect()."""

    def __init__(self, pid):
        self.pid, self.stdout, self.stderr, self.exit_code = pid, "", "", 0

    def disconnect(self):
        return None


class _Commands:
    def __init__(self, backend: "Beam", sandbox_id: str, ready: float = READY_SECONDS):
        self._backend, self._id, self._ready = backend, sandbox_id, ready

    def _call(self, method, rest, body=None, **options):
        return self._backend.sandbox_call(self._id, method, rest, body, **options)

    def run(self, cmd, *, user=None, envs=None, background=False, timeout=60, cwd=None, on_output=None):
        body = {"command": wrap(cmd, user=user, cwd=cwd), "wait": False,
                **({"env": {k: str(v) for k, v in envs.items()}} if envs else {})}
        started = self._call("POST", "/exec", body, ready_seconds=self._ready)
        pid = started.get("pid") or 0
        if pid <= 0:
            raise CommandFailed(f"Beam started no process for `{command_name(cmd)}` in sandbox {self._id}", command=cmd)
        if background:
            return _Background(pid)
        return self._wait(pid, timeout, on_output, cmd)

    def _drain(self, pid: int, stream: str) -> str:
        """What the process wrote to stdout or stderr since it was last asked."""
        parts = []
        for _ in range(1000):
            text = self._call("GET", f"/{stream}", query={"pid": pid}).get(stream) or ""
            if not text:
                break
            parts.append(text)
        return "".join(parts)

    def _wait(self, pid: int, timeout, on_output, cmd=""):
        deadline = time.monotonic() + timeout if timeout else None
        out, err, pause = [], [], 0.1
        while True:
            found = self._call("GET", "/status", query={"pid": pid}, read=True)
            code = found.get("exitCode")
            if code is None and str(found.get("status") or "").lower() in ENDED:
                code = 0
            done = isinstance(code, int) and code >= 0
            if on_output or done:
                text = self._drain(pid, "stdout")
                out.append(text)
                if on_output and text:
                    on_output(text)
            if done:
                err.append(self._drain(pid, "stderr"))
                stdout, stderr = "".join(out), "".join(err)
                if code != 0:
                    raise CommandFailed(stdout=stdout, stderr=stderr, exit_code=code, command=cmd)
                return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=0)
            if deadline is not None and time.monotonic() > deadline:
                try:
                    self._call("POST", "/kill", {"pid": pid})
                except Exception:   # it ends with the sandbox
                    pass
                raise Timeout(f"`{command_name(cmd)}` ran out of its {timeout} s in Beam sandbox {self._id}")
            time.sleep(pause)
            pause = min(pause * 1.5, 2.0)


class _Files:
    def __init__(self, backend: "Beam", sandbox_id: str, commands: _Commands):
        self._backend, self._id, self._commands = backend, sandbox_id, commands

    def write(self, path, data, *, user=None):
        """Uploaded to a staging file, then moved into place as its user's by one command (as root)."""
        raw = data.encode() if isinstance(data, str) else bytes(data)
        staging = f"/tmp/.sbx-upload-{uuid.uuid4().hex}"
        for offset in range(0, max(len(raw), 1), CHUNK):
            self._backend.sandbox_call(self._id, "POST", "/files/upload", {
                "containerPath": staging, "mode": 0o600, "offset": offset,
                "data": base64.b64encode(raw[offset:offset + CHUNK]).decode()}, ready_seconds=READY_SECONDS)
        owner = shlex.quote(user) if user else f"$(id -u {DEFAULT_USER} 2>/dev/null || id -u)"
        folder, target = shlex.quote(path.rsplit("/", 1)[0] or "/"), shlex.quote(path)
        self._commands.run(f"mkdir -p {folder} && mv -f {staging} {target} && chown {owner} {target} && "
                           f"chmod 644 {target} || {{ rm -f {staging}; exit 1; }}", user="root", cwd="/", timeout=60)

    def read(self, path, *, format="text"):
        raw = base64.b64decode(self._commands.run(f"base64 -w0 -- {shlex.quote(path)}", user="root", cwd="/",
                                                  timeout=300).stdout)
        return raw if format == "bytes" else raw.decode(errors="replace")


class Handle:
    def __init__(self, backend: "Beam", sandbox_id: str, ready: float = READY_SECONDS):
        """ready: how long a sandbox that is still starting is waited for (0: a look, never a wait)."""
        self._backend = backend
        self.sandbox_id = sandbox_id
        self.commands = _Commands(backend, sandbox_id, ready)
        self.files = _Files(backend, sandbox_id, self.commands)

    def kill(self):
        return self._backend.kill(self.sandbox_id)
