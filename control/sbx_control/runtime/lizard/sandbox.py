"""A Lizard sandbox as sbx's Handle: commands and files through Lizard's API
(POST /exec, /files; lizard-sdk sandbox/process.py, fs.py).

Lizard's exec takes the user and the env itself, so a command's values are
never in its text. It waits for a command at most 600 seconds and has no
detached mode, so a longer one, one without a limit and a background one are
started with nohup (as Lizard's own docs start a background process) under
RUN_DIR and polled through exec."""
from __future__ import annotations

import base64
import math
import shlex
import time
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING

from ..base import RUNNING, CommandFailed, Failed, NotRunning, Timeout, command_name
from .errors import POLL_SECONDS, LizardError

if TYPE_CHECKING:
    from . import Lizard

HOME = "/home/user"
USER = "user"                            # the templates' unprivileged account (lizard-sdk docs/sandboxes.md)
TEMPLATE_FILE = "/etc/sbx/template.sh"   # a template's env and workdir, sourced by every command
SYNC_SECONDS = 600                       # the longest command Lizard's exec waits for
RUN_DIR = "/tmp/.sbx-run"                # a detached command's pid, output and exit code
CHUNK = 256 * 1024                       # of a detached command's output per poll; exec cuts a stream at 512 KiB
FILE_CHUNK = 4 * 1024 * 1024             # a larger file is written in parts and joined
# The detached command: a session of its own (setsid), its pid, output and exit code under $1.
# Only the one command is put in the background: a list would keep a shell on the exec's output.
DETACH = ('mkdir -p "$1" && cd "$1" || exit 1; nohup setsid bash -c \'echo $$ >pid; bash -c "$1" >out 2>err; '
          'echo $? >code\' sbx "$2" >/dev/null 2>&1 </dev/null & echo started')
POLL = ('cd "$1" 2>/dev/null || { echo gone; exit 0; }; echo here; '
        'tail -c +$(($2 + 1)) out | head -c {chunk} | base64 -w0; echo; '
        'tail -c +$(($3 + 1)) err | head -c {chunk} | base64 -w0; echo; cat code 2>/dev/null; true').replace(
            "{chunk}", str(CHUNK))


def wrap(cmd: str, cwd: str | None = None) -> str:
    """cmd as one shell command: the template's env and workdir first
    (TEMPLATE_FILE), then cwd, whose failure stops it."""
    where = f"cd {shlex.quote(cwd)} || exit 1; " if cwd else 'cd "${SBX_TEMPLATE_WORKDIR:-$HOME}" 2>/dev/null; '
    return f"[ -r {TEMPLATE_FILE} ] && . {TEMPLATE_FILE}; {where}{cmd}"


class _Background:
    """A detached command: it keeps running after disconnect()."""

    def __init__(self, folder):
        self.folder, self.stdout, self.stderr, self.exit_code = folder, "", "", 0

    def disconnect(self):
        return None


class _Commands:
    def __init__(self, backend: "Lizard", sandbox_id: str):
        self._backend, self._id = backend, sandbox_id

    def _exec(self, cmd, *, user=None, envs=None, seconds: float = 60) -> dict:
        """One exec: cmd a shell command or an argv list (run with no shell).
        A failure on a sandbox that is not running now is NotRunning."""
        seconds = max(1, min(SYNC_SECONDS, math.ceil(seconds)))
        body = {"cmd": cmd, "user": user or USER, "timeoutMs": seconds * 1000}
        if envs:
            body["envs"] = {k: str(v) for k, v in envs.items()}
        try:
            return self._backend.call("POST", f"/api/sandboxes/{self._id}/exec", body, sandbox_id=self._id,
                                      timeout=seconds + 30) or {}
        except (LizardError, Timeout) as error:
            if isinstance(error, LizardError) and self._backend.info(self._id).state != RUNNING:
                raise NotRunning(f"Lizard sandbox {self._id} is not running: {error}") from error
            raise

    def run(self, cmd, *, user=None, envs=None, background=False, timeout=60, cwd=None, on_output=None):
        """Synchronous up to SYNC_SECONDS; a longer (or unbounded) one runs
        detached and is polled; background returns once it started."""
        script = wrap(cmd, cwd)
        if background or not timeout or timeout > SYNC_SECONDS:
            folder = f"{RUN_DIR}/{uuid.uuid4().hex}"
            started = self._exec(["bash", "-c", DETACH, "sbx", folder, script], user=user, envs=envs, seconds=60)
            if started.get("exitCode") not in (0, None):
                raise Failed(f"`{command_name(cmd)}` did not start in Lizard sandbox {self._id}: "
                             f"{(started.get('stderr') or '').strip()[-300:]}")
            if background:
                return _Background(folder)
            return self._wait(folder, timeout, on_output, cmd)
        try:
            result = self._exec(["bash", "-c", script], user=user, envs=envs, seconds=timeout)
        except Timeout as error:
            raise Timeout(f"`{command_name(cmd)}` ran out of its {timeout} s in Lizard sandbox {self._id}") from error
        return self._done(result.get("stdout") or "", result.get("stderr") or "", result.get("exitCode"), cmd)

    def _wait(self, folder, timeout, on_output, cmd=""):
        deadline = time.monotonic() + timeout if timeout else None
        offsets, out, err = [0, 0], [], []
        while True:
            found = self._exec(["bash", "-c", POLL, "sbx", folder, str(offsets[0]), str(offsets[1])], user="root",
                               seconds=60)
            lines = (found.get("stdout") or "").split("\n")
            if lines[0] != "here":
                raise NotRunning(f"`{command_name(cmd)}` is gone from Lizard sandbox {self._id}: the sandbox "
                                 "started afresh while it ran")
            chunks = [base64.b64decode(part) for part in (lines + ["", ""])[1:3]]
            code = (lines + ["", "", ""])[3].strip()
            for index, (chunk, kept) in enumerate(zip(chunks, (out, err))):
                offsets[index] += len(chunk)
                kept.append(chunk.decode(errors="replace"))
            if on_output and chunks[0]:
                on_output(chunks[0].decode(errors="replace"))
            if code and not any(chunks):
                self._exec(["rm", "-rf", folder], user="root", seconds=30)
                return self._done("".join(out), "".join(err), int(code), cmd)
            if deadline is not None and time.monotonic() > deadline:
                try:   # its whole session, then what it left
                    self._exec(["bash", "-c", 'pkill -KILL -s "$(cat "$1/pid")"; rm -rf "$1"', "sbx", folder],
                               user="root", seconds=30)
                except Exception:   # it ends with the sandbox
                    pass
                raise Timeout(f"`{command_name(cmd)}` ran out of its {timeout} s in Lizard sandbox {self._id}")
            if not any(chunks):
                time.sleep(POLL_SECONDS)

    @staticmethod
    def _done(stdout, stderr, exit_code, cmd=""):
        if exit_code not in (0, None):
            raise CommandFailed(stdout=stdout, stderr=stderr, exit_code=exit_code, command=cmd)
        return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code or 0)


class _Files:
    def __init__(self, backend: "Lizard", sandbox_id: str, commands: _Commands):
        self._backend, self._id, self._commands = backend, sandbox_id, commands

    def _put(self, path: str, raw: bytes, user: str):
        self._backend.call("POST", f"/api/sandboxes/{self._id}/files", {
            "path": path, "content": base64.b64encode(raw).decode(), "encoding": "base64", "user": user},
            sandbox_id=self._id)

    def write(self, path, data, *, user=None):
        """As (and owned by) user; Lizard makes the parent folders. A file
        past FILE_CHUNK goes in parts, joined in the sandbox."""
        raw, user = data.encode() if isinstance(data, str) else bytes(data), user or USER
        if len(raw) <= FILE_CHUNK:
            return self._put(path, raw, user)
        part = f"/tmp/.sbx-upload-{uuid.uuid4().hex}"
        count = math.ceil(len(raw) / FILE_CHUNK)
        names = [f"{part}.{index}" for index in range(count)]
        try:
            for index, name in enumerate(names):
                self._put(name, raw[index * FILE_CHUNK:(index + 1) * FILE_CHUNK], user)
            joined = self._commands._exec(
                ["bash", "-c", 'into=$1; shift; mkdir -p "$(dirname "$into")" && cat "$@" >"$into"', "sbx", path,
                 *names], user=user, seconds=300)
            if joined.get("exitCode") not in (0, None):
                raise Failed(f"could not write {path} in Lizard sandbox {self._id}: "
                             f"{(joined.get('stderr') or '').strip()[-300:]}")
        finally:
            try:
                self._commands._exec(["rm", "-f", *names], user="root", seconds=60)
            except Exception:   # they go with the sandbox's /tmp
                pass

    def read(self, path, *, format="text"):
        """A missing file is Lizard's 404 (LizardError.gone()); a sandbox that is gone, NotRunning."""
        try:
            raw = self._backend.call("GET", f"/api/sandboxes/{self._id}/files", query={"path": path, "user": USER},
                                     raw=True, read=False)
        except LizardError as error:
            if self._backend.info(self._id).state != RUNNING:   # raises NotRunning when Lizard has none
                raise NotRunning(f"Lizard sandbox {self._id} is not running: {error}") from error
            raise
        return raw if format == "bytes" else raw.decode(errors="replace")


class Handle:
    def __init__(self, backend: "Lizard", sandbox_id: str):
        self._backend = backend
        self.sandbox_id = sandbox_id
        self.commands = _Commands(backend, sandbox_id)
        self.files = _Files(backend, sandbox_id, self.commands)

    def kill(self):
        return self._backend.kill(self.sandbox_id)
