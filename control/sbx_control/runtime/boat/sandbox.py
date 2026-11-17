"""A Boat sandbox as sbx's Handle: commands and files through Boat's API,
which takes no env and no user, so wrap() makes every command one shell
command that loads the template's env, the command's env file and its
working directory first."""
from __future__ import annotations

import base64
import re
import shlex
import time
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING

from ..base import CommandFailed, Timeout, command_name
from .errors import POLL_SECONDS, BoatError

HOME = "/home/user"
TEMPLATE_FILE = "/etc/sbx/template.sh"   # a template's env and workdir, sourced by every command
SYNC_SECONDS = 600       # the longest command Boat's API waits for; longer ones run detached
FILE_LIMIT = 5 * 1024 * 1024

if TYPE_CHECKING:
    from . import Boat

ENV_DIR = "/tmp/.sbx-env"  # where a command's env waits for it (a new machine's /tmp is empty)


def wrap(cmd: str, *, user: str | None = None, env_file: str | None = None, cwd: str | None = None) -> str:
    """cmd as one shell command for Boat's API, which runs as `user` in its
    home and takes no env or user: the template's env and workdir first
    (TEMPLATE_FILE), then the command's env from env_file (written through
    the files API, read once and removed: values never appear in the command
    Boat keeps), then cwd, whose failure stops it; another user through sudo
    (the env file handed over as theirs, 0600)."""
    where = f"cd {shlex.quote(cwd)} || exit 1; " if cwd else 'cd "${SBX_TEMPLATE_WORKDIR:-$HOME}" 2>/dev/null; '
    other = bool(user and user != "user")
    # Another user can't read ENV_DIR (the default user's, 0700): its copy goes in /tmp, 0600, its own.
    mine = f"/tmp/.sbx-env-{env_file.rsplit('/', 1)[1]}.{user}" if env_file and other else env_file
    load = f"{{ . {mine} && rm -f {mine}; }} || exit 1; " if env_file else ""
    script = f"[ -r {TEMPLATE_FILE} ] && . {TEMPLATE_FILE}; {load}{where}{cmd}"
    if not other:
        return f"bash -c {shlex.quote(script)}"
    hand = (f"sudo install -o {shlex.quote(user)} -m 600 {env_file} {mine} && rm -f {env_file} || exit 1; "
            if env_file else "")
    return f"bash -c {shlex.quote(hand + 'exec sudo -H -u ' + shlex.quote(user) + ' bash -c "$1"')} sbx {shlex.quote(script)}"


class _Background:
    """A detached command: it keeps running after disconnect()."""

    def __init__(self, process_id):
        self.process_id, self.stdout, self.stderr, self.exit_code = process_id, "", "", 0

    def disconnect(self):
        return None


class _Commands:
    def __init__(self, backend: "Boat", sandbox_id: str):
        self._backend, self._id = backend, sandbox_id

    def run(self, cmd, *, user=None, envs=None, background=False, timeout=60, cwd=None, on_output=None):
        """Synchronous up to SYNC_SECONDS; a longer (or unbounded) one runs
        detached and is polled; background returns once it started."""
        from boat_sdk.models import CommandRequest
        env_file = self._backend.env_file(self._id, envs) if envs else None
        command = wrap(cmd, user=user, env_file=env_file, cwd=cwd)
        api = self._backend.api()
        if background or not timeout or timeout > SYNC_SECONDS:
            started = self._backend.call(lambda: api.command(self._id, CommandRequest(command=command, detached=True)),
                                         sandbox_id=self._id).actual_instance
            if background:
                return _Background(started.process_id)
            return self._wait(started.process_id, timeout, on_output, cmd)
        result = self._backend.call(lambda: api.command(self._id, CommandRequest(
            command=command, timeout_seconds=max(1, min(SYNC_SECONDS, int(-(-timeout // 1)))))),
            sandbox_id=self._id).actual_instance
        if result.timed_out:
            raise Timeout(f"`{command_name(cmd)}` ran out of its {timeout} s in Boat sandbox {self._id}")
        return self._done(result.stdout or "", result.stderr or "", result.exit_code, cmd)

    def _wait(self, process_id, timeout, on_output, cmd=""):
        api = self._backend.api()
        deadline = time.monotonic() + timeout if timeout else None
        offsets = {"out": 0, "err": 0}
        out, err = [], []
        while True:
            found = self._backend.call(lambda: api.command_status(
                self._id, process_id, stdout_offset=offsets["out"], stderr_offset=offsets["err"]), sandbox_id=self._id)
            out.append(found.stdout or "")
            err.append(found.stderr or "")
            if on_output and found.stdout:
                on_output(found.stdout)
            offsets["out"] = found.stdout_next_offset or offsets["out"] + len((found.stdout or "").encode())
            offsets["err"] = found.stderr_next_offset or offsets["err"] + len((found.stderr or "").encode())
            if not found.running:
                return self._done("".join(out), "".join(err), found.exit_code, cmd)
            if deadline is not None and time.monotonic() > deadline:
                try:
                    api.stop_command(self._id, process_id)
                except Exception:   # it ends with the sandbox
                    pass
                raise Timeout(f"the command ran out of its {timeout} s in Boat sandbox {self._id}")
            if not found.more:
                time.sleep(POLL_SECONDS)

    @staticmethod
    def _done(stdout, stderr, exit_code, cmd=""):
        if exit_code not in (0, None):
            raise CommandFailed(stdout=stdout, stderr=stderr, exit_code=exit_code, command=cmd)
        return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code or 0)


def _allowed_path(path: str) -> bool:
    """Where Boat's file API reads and writes: under /home/user or /tmp."""
    clean = re.sub(r"/+", "/", path)
    return not re.search(r"(^|/)\.\.(/|$)", clean) and (
        clean.startswith(HOME + "/") or clean.startswith("/tmp/") or not clean.startswith("/"))


class _Files:
    def __init__(self, backend: "Boat", sandbox_id: str, commands: _Commands):
        self._backend, self._id, self._commands = backend, sandbox_id, commands

    def write(self, path, data, *, user=None):
        raw = data.encode() if isinstance(data, str) else bytes(data)
        if _allowed_path(path) and user in (None, "user"):
            return self._put(path, raw)
        staging = f"/tmp/.sbx-upload-{uuid.uuid4().hex}"
        self._put(staging, raw)
        folder = shlex.quote(path.rsplit("/", 1)[0] or "/")
        self._commands.run(f"mkdir -p {folder} && mv -f {staging} {shlex.quote(path)}", user=user, timeout=60)

    def _put(self, path, raw: bytes):
        from boat_sdk.models import FileUrlRequest, FileWriteRequest
        api = self._backend.api()
        if len(raw) <= FILE_LIMIT:
            return self._backend.call(lambda: api.write_file(self._id, FileWriteRequest(
                path=path, content=base64.b64encode(raw).decode(), encoding="base64")), sandbox_id=self._id)
        url = self._backend.call(lambda: api.create_upload_url(self._id, FileUrlRequest(path=path)),
                                 sandbox_id=self._id)
        self._backend.http(url.url, method=url.method or "PUT", data=raw)

    def read(self, path, *, format="text"):
        from boat_sdk.models import FileUrlRequest
        api = self._backend.api()
        if not _allowed_path(path):
            raw = base64.b64decode(self._commands.run(f"base64 -w0 {shlex.quote(path)}", timeout=120).stdout)
        else:
            try:
                found = self._backend.call(lambda: api.read_file(self._id, path, encoding="base64"),
                                           sandbox_id=self._id)
                raw = base64.b64decode(found.content)
            except BoatError as error:
                if error.code != "file_too_large":
                    raise
                url = self._backend.call(lambda: api.create_download_url(self._id, FileUrlRequest(path=path)),
                                         sandbox_id=self._id)
                raw = self._backend.http(url.url, method="GET")
        return raw if format == "bytes" else raw.decode(errors="replace")


class Handle:
    def __init__(self, backend: "Boat", sandbox_id: str):
        self._backend = backend
        self.sandbox_id = sandbox_id
        self.commands = _Commands(backend, sandbox_id)
        self.files = _Files(backend, sandbox_id, self.commands)

    def kill(self):
        return self._backend.kill(self.sandbox_id)
