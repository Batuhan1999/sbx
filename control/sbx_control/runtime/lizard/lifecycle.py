"""Lizard sandboxes' lifecycle: create from a snapshot (sbx's template),
connect (resuming a paused one), list by metadata, pause, kill.

Two things Lizard does differently, which this module carries:

- A pause keeps the files only and a resume boots the sandbox afresh
  (lizard-sdk docs/sandboxes.md), so sbx treats every resume as a cold boot
  (cold_resume) and this backend runs the template's start command after
  every boot (_boot): nothing is assumed of the sandbox's init.
- A sandbox's lifetime ends in its deletion, never in a pause, and it is not
  an idle timer. sbx's timeout means "pause when idle", so a sandbox made to
  pause (on_timeout "pause", the default) is created without a lifetime
  (timeoutMs 0) and its timeout is kept here: the deadline lives in this
  process, set_timeout moves it, and a listing (the keeper lists every
  minute) pauses what is past it. A deadline not known (after a restart of
  sbx-control) starts again at the timeout limit, at most RESTART_SECONDS.
  Only a sandbox made to be killed (on_timeout "kill": checks, builders)
  gets Lizard's own lifetime."""
from __future__ import annotations

import datetime
import time

from ..base import FAILED, PAUSED, RUNNING, Busy, Failed, Info, NotRunning, TemplateGone, Timeout
from .errors import POLL_SECONDS
from .sandbox import HOME, USER, Handle

TEMPLATE_KEY, ON_TIMEOUT_KEY = "sbx_template", "sbx_on_timeout"
GONE = ("stopped", "deleted")          # lizard-sdk sandbox.py wait_for_status
STOPPED = ("paused", "pausing")
RESTART_SECONDS = 2 * 3600             # an unknown deadline's: as long as sbx keeps an unused sandbox awake
START_FILE = "/etc/sbx/start.sh"       # the template's start command (build._start)
# What sbx needs of a Lizard machine: the unprivileged account its commands
# run as, and /run/sbx on a tmpfs of its own that runs programs and is that
# account's (env values live there: never on the disk a pause or snapshot keeps).
MACHINE = (f"id {USER} >/dev/null 2>&1 || useradd -m -d {HOME} -s /bin/bash {USER}; mkdir -p /run/sbx && "
           "{ mountpoint -q /run/sbx || mount -t tmpfs -o "
           f"nosuid,nodev,mode=0700,uid=$(id -u {USER}),gid=$(id -g {USER}),size=20% sbx-run /run/sbx; }}")
BOOT = f"[ -r {START_FILE} ] || exit 0; nohup setsid bash {START_FILE} >/tmp/sbx-start.log 2>&1 </dev/null & true"


def _when(value) -> datetime.datetime | None:
    """A time as Lizard gives it: unix milliseconds or ISO 8601."""
    try:
        if isinstance(value, (int, float)) and value > 0:
            return datetime.datetime.fromtimestamp(value / 1000, datetime.timezone.utc)
        if isinstance(value, str) and value:
            found = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return found if found.tzinfo else found.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _at(seconds: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)


class Sandboxes:
    """The lifecycle part of the Lizard backend (mixed into Lizard)."""

    def _sandbox(self, sandbox_id) -> dict:
        found = self.call("GET", f"/api/sandboxes/{sandbox_id}", sandbox_id=sandbox_id) or {}
        if found.get("status") in GONE:
            raise NotRunning(f"Lizard sandbox {sandbox_id} is {found.get('status')}")
        return found

    def _info(self, sandbox: dict) -> Info:
        sandbox_id = sandbox.get("sandboxId") or sandbox["id"]
        status, metadata = sandbox.get("status"), dict(sandbox.get("metadata") or {})
        state = PAUSED if status in STOPPED else FAILED if status == "failed" else RUNNING
        if state == RUNNING:
            self._paused.pop(sandbox_id, None)
            end = (_when(sandbox.get("endAt") or sandbox.get("expiresAt")) if metadata.get(ON_TIMEOUT_KEY) == "kill"
                   else _at(self._deadline(sandbox_id)))
        else:   # Lizard does not say when it paused: since this process first saw it paused
            end = _at(self._paused.setdefault(sandbox_id, time.time()))
        return Info(sandbox_id=sandbox_id, metadata=metadata, state=state, template_id=metadata.get(TEMPLATE_KEY, ""),
                    started_at=_when(sandbox.get("startedAt")), end_at=end, cpu_count=sandbox.get("cpus"),
                    memory_mb=sandbox.get("memoryMb"))

    def _deadline(self, sandbox_id: str) -> float:
        """When a sandbox made to pause is paused; one not known starts again."""
        return self._deadlines.setdefault(
            sandbox_id, time.time() + min(RESTART_SECONDS, self._limits.timeout_seconds or RESTART_SECONDS))

    def create(self, template, *, timeout, envs=None, metadata=None, deny_out=None, on_timeout=None,
               auto_resume=None):
        """A new sandbox from the template's snapshot, in the snapshot's
        region and at its size, with sbx's metadata; ready once it runs, has
        what sbx needs of the machine and its start command is started."""
        snapshot = self._snapshot(template)
        if snapshot is None:
            raise TemplateGone(template, f"Lizard has no snapshot named {template} in project {self.project()}")
        labels = {k: str(v) for k, v in (metadata or {}).items() if v is not None}
        labels[TEMPLATE_KEY] = template
        kill = on_timeout == "kill"
        if kill:
            labels[ON_TIMEOUT_KEY] = "kill"
        body = {"snapshotId": snapshot["id"], "projectId": self.project(), "metadata": labels,
                # 0: no lifetime. Lizard's ends in a deletion; this backend pauses at sbx's timeout.
                "timeoutMs": int(timeout) * 1000 if kill else 0}
        if snapshot.get("region"):
            body["region"] = snapshot["region"]
        if envs:
            body["envs"] = {k: str(v) for k, v in envs.items()}
        created = self.call("POST", "/api/sandboxes", body, asked={"timeout_seconds": timeout}) or {}
        sandbox_id = created.get("sandboxId") or created.get("id")
        if not sandbox_id:
            raise Failed("Lizard created a sandbox without an id")
        if not kill:
            self._deadlines[sandbox_id] = time.time() + timeout
        try:
            self._ready(sandbox_id)
            self._machine_ready(sandbox_id)
        except Exception:
            try:
                self.kill(sandbox_id)   # else it runs, and bills, with no lifetime
            except Exception as error:
                print(f"Lizard: could not delete {sandbox_id}, which failed to start: {error!r}", flush=True)
            raise
        return Handle(self, sandbox_id)

    def _ready(self, sandbox_id: str, seconds: float = 300) -> dict:
        """Wait until Lizard says the sandbox runs."""
        deadline = time.monotonic() + seconds
        while True:
            found = self._sandbox(sandbox_id)
            status = found.get("status")
            if status in (None, "running"):
                return found
            if status == "failed" or found.get("pauseError"):
                raise Failed(f"Lizard sandbox {sandbox_id} failed: {found.get('pauseError') or status}")
            if status == "paused":
                raise NotRunning(f"Lizard sandbox {sandbox_id} paused while starting")
            if time.monotonic() > deadline:
                raise Timeout(f"Lizard sandbox {sandbox_id} was not running after {int(seconds)} s ({status})")
            time.sleep(POLL_SECONDS)

    def _machine_ready(self, sandbox_id: str, start: bool = True):
        """After every boot (a new sandbox, a resume): MACHINE, then the
        template's start command (start False: a builder, which runs its own)."""
        commands = Handle(self, sandbox_id).commands
        found = commands._exec(["bash", "-c", MACHINE], user="root", seconds=60)
        if found.get("exitCode") not in (0, None):
            raise Failed(f"Lizard sandbox {sandbox_id} could not be prepared (the account {USER}, /run/sbx): "
                         f"{(found.get('stderr') or '').strip()[-300:]}")
        if start:
            commands._exec(["bash", "-c", BOOT], user=USER, seconds=60)

    def connect(self, sandbox_id, *, timeout=None):
        """The running sandbox, resuming a paused one (a fresh boot: what sbx
        needs of the machine is made again, the start command started, and
        its ports are exposed again on their next request)."""
        found = self._sandbox(sandbox_id)
        if found.get("status") == "pausing":
            found = self._settle(sandbox_id)
        if found.get("status") in ("paused", "failed"):
            self._exposed.drop(sandbox_id)
            self.call("POST", f"/api/sandboxes/{sandbox_id}/resume", sandbox_id=sandbox_id)
            found = self._ready(sandbox_id)
            self._paused.pop(sandbox_id, None)
            self._deadlines.pop(sandbox_id, None)
            self._machine_ready(sandbox_id)
        if timeout is not None:
            self._set_timeout(sandbox_id, found, timeout)
        return Handle(self, sandbox_id)

    def handle(self, sandbox_id):
        """A look: one question to Lizard, never a resume."""
        if self.info(sandbox_id).state != RUNNING:
            raise NotRunning(f"Lizard sandbox {sandbox_id} is not running")
        return Handle(self, sandbox_id)

    def _settle(self, sandbox_id: str, seconds: float = 300) -> dict:
        """Wait while Lizard pauses a sandbox."""
        deadline = time.monotonic() + seconds
        while True:
            found = self._sandbox(sandbox_id)
            if found.get("status") != "pausing":
                return found
            if time.monotonic() > deadline:
                raise Busy(f"Lizard is still pausing sandbox {sandbox_id}")
            time.sleep(POLL_SECONDS)

    def sandboxes(self, metadata=None, states=None):
        """The project's sandboxes with this metadata (Lizard's listing takes
        no filter). Pauses the listed ones past their timeout first."""
        listed = self.call("GET", f"/api/projects/{self.project()}/sandboxes") or []
        found = []
        for sandbox in listed:
            labels = sandbox.get("metadata") or {}
            if sandbox.get("status") in GONE or any(labels.get(k) != v for k, v in (metadata or {}).items()):
                continue
            info = self._info(sandbox)
            if info.state == RUNNING and labels.get(ON_TIMEOUT_KEY) != "kill" \
                    and time.time() > self._deadline(info.sandbox_id):
                info = self._timed_out(info)
            if states is None or info.state in states:
                found.append(info)
        return found

    def _timed_out(self, info: Info) -> Info:
        """Pause a sandbox past sbx's timeout; as it is listed after."""
        try:
            self.pause(info.sandbox_id)
            return self.info(info.sandbox_id)
        except Exception as error:   # the next listing tries again
            print(f"Lizard: could not pause {info.sandbox_id} at its timeout: {error!r}", flush=True)
            return info

    def info(self, sandbox_id):
        return self._info(self._sandbox(sandbox_id))

    def _forget(self, sandbox_id: str):
        self._exposed.drop(sandbox_id)
        self._deadlines.pop(sandbox_id, None)
        self._paused.pop(sandbox_id, None)

    def kill(self, sandbox_id):
        self._forget(sandbox_id)
        try:
            self.call("DELETE", f"/api/sandboxes/{sandbox_id}", sandbox_id=sandbox_id)
        except NotRunning:
            return False
        return True

    def pause(self, sandbox_id):
        """Lizard's pause: the files are kept, the processes end."""
        found = self._sandbox(sandbox_id)
        if found.get("status") == "paused":
            return
        self._exposed.drop(sandbox_id)
        if found.get("status") != "pausing":
            try:
                self.call("POST", f"/api/sandboxes/{sandbox_id}/pause", sandbox_id=sandbox_id)
            except NotRunning:
                raise
            except Failed as error:
                raise Busy(f"Lizard would not pause sandbox {sandbox_id} now: {error}") from error
        deadline = time.monotonic() + 300
        while True:
            found = self._sandbox(sandbox_id)
            if found.get("status") == "paused":
                self._deadlines.pop(sandbox_id, None)
                self._paused[sandbox_id] = time.time()
                return
            if found.get("pauseError") or time.monotonic() > deadline:
                raise Busy(f"Lizard did not pause sandbox {sandbox_id}: "
                           f"{found.get('pauseError') or 'it is ' + str(found.get('status'))}")
            time.sleep(POLL_SECONDS)

    def set_timeout(self, sandbox_id, seconds):
        self._set_timeout(sandbox_id, self._sandbox(sandbox_id), seconds)

    def _set_timeout(self, sandbox_id: str, found: dict, seconds: int):
        if found.get("status") in STOPPED or found.get("status") == "failed":
            raise NotRunning(f"Lizard sandbox {sandbox_id} is {found.get('status')}")
        if (found.get("metadata") or {}).get(ON_TIMEOUT_KEY) == "kill":
            self.call("POST", f"/api/sandboxes/{sandbox_id}/timeout", {"timeoutMs": int(seconds) * 1000},
                      sandbox_id=sandbox_id, asked={"timeout_seconds": seconds})
        else:
            self._deadlines[sandbox_id] = time.time() + seconds

    def listed(self, sandbox_ids: set) -> None:
        """After a full listing: forget what it kept of sandboxes no longer there."""
        self._exposed.keep(sandbox_ids)
        for kept in (self._deadlines, self._paused):
            for sandbox_id in set(kept) - set(sandbox_ids):
                kept.pop(sandbox_id, None)
