"""Boat sandboxes' lifecycle: create from a saved snapshot, connect
(resuming a stopped one: treated as a cold boot), list by label, pause
(Boat's stop), kill."""
from __future__ import annotations

import time
import uuid

from ..dockerfile import export_line
from ..base import FAILED, PAUSED, RUNNING, Busy, Failed, Info, NotRunning, Timeout, TemplateGone
from .build import TEMPLATE_LABEL, given, saved_name
from .errors import POLL_SECONDS, BoatError
from .sandbox import ENV_DIR, Handle

# States as Boat's API names them (docs.boat.dev/api/v1, Sandbox lifecycle).
UP = ("init", "provisioning", "provisioned", "cloning", "ready", "idle", "running")
READY = ("ready", "idle", "running")
STOPPED = ("archiving", "archived")
GONE = ("cancelled",)
# /run/sbx as its own tmpfs, executable, the default user's, a fifth of the
# memory as /run is (core.RUN_FULL_PERCENT watches it); kept if mounted.
RUN_MOUNT = ("sudo mkdir -p /run/sbx && { mountpoint -q /run/sbx || sudo mount -t tmpfs -o "
             "nosuid,nodev,mode=0700,uid=$(id -u),gid=$(id -g),size=20% sbx-run /run/sbx; }")


class Sandboxes:
    """The lifecycle part of the Boat backend (mixed into Boat)."""

    def _sandbox(self, sandbox_id):
        found = self.call(lambda: self.api().get(sandbox_id), sandbox_id=sandbox_id).sandbox
        if found.state in GONE:
            raise NotRunning(f"Boat sandbox {sandbox_id} was cancelled")
        return found

    def _info(self, sandbox) -> Info:
        state = (RUNNING if sandbox.state in UP else PAUSED if sandbox.state in STOPPED else FAILED)
        if sandbox.subdomain:
            self._kept_of(sandbox.id).subdomain = sandbox.subdomain
        memory = sandbox.memory_gb * 1024 if sandbox.memory_gb else None
        return Info(sandbox_id=sandbox.id, metadata=dict(sandbox.labels or {}), state=state,
                    template_id=(sandbox.labels or {}).get(TEMPLATE_LABEL, ""), started_at=sandbox.created_at,
                    end_at=sandbox.archive_after if state == RUNNING else sandbox.stopped_at,
                    cpu_count=sandbox.vcpu, memory_mb=memory)

    def create(self, template, *, timeout, envs=None, metadata=None, deny_out=None, on_timeout=None,
               auto_resume=None):
        """A new sandbox from the template's saved snapshot: no account
        secrets of Boat's (noEnv), sbx's metadata as labels."""
        from boat_sdk.models import CreateSandboxRequest
        labels = {k: str(v) for k, v in (metadata or {}).items() if v is not None}
        labels[TEMPLATE_LABEL] = template
        if len(labels) > 20 or any(len(v.encode()) > 256 for v in labels.values()):
            raise Failed("Boat keeps at most 20 labels of 256 bytes each; this sandbox's metadata is larger")
        request = CreateSandboxRequest(var_from=saved_name(template), labels=labels, env=dict(envs or {}) or None,
                                       no_env=True, environment=None, type=None, name=labels.get("sbx"),
                                       **given(ttl_seconds=timeout), **self._org_args())
        once = str(uuid.uuid4())   # one key for every retry: a lost answer never makes a second sandbox
        try:
            created = self.call(lambda: self.api().create(idempotency_key=once, create_sandbox_request=request),
                                asked={"timeout_seconds": timeout})
        except BoatError as error:
            if error.gone_snapshot():
                raise TemplateGone(template, f"Boat has no saved snapshot {saved_name(template)}") from error
            raise
        sandbox_id = created.sandbox.id
        try:
            self._ready(sandbox_id)
            self._machine_ready(sandbox_id)
        except Exception:
            try:
                self.kill(sandbox_id)   # else it runs, and bills, until its timeout
            except Exception:   # its timeout still ends it
                pass
            raise
        return Handle(self, sandbox_id)

    def _ready(self, sandbox_id: str, seconds: float = 600):
        """Wait until Boat says the sandbox takes commands and has every file
        of its snapshot on its disk (hydrated). A sandbox from a saved
        snapshot is ready before its files have all come in, and a setup that
        replaces folders meanwhile (a toolchain swapped in, node_modules
        purged) failed at random: "Cannot call rmtree on a symbolic link",
        pnpm finding a modules folder to remove. A new sandbox restores
        nothing (hydrated None)."""
        deadline = time.monotonic() + seconds
        while True:
            found = self._sandbox(sandbox_id)
            if found.state in READY and found.hydrated is not False:
                return found
            if found.state in READY and time.monotonic() > deadline:
                print(f"Boat: sandbox {sandbox_id} still restoring its files after {int(seconds)} s; going on",
                      flush=True)
                return found
            if found.state == "error":
                raise Failed(f"Boat sandbox {sandbox_id} failed to start: {found.error or 'error'}")
            if found.state == "archived":
                raise NotRunning(f"Boat sandbox {sandbox_id} stopped while starting")
            if time.monotonic() > deadline:
                raise Timeout(f"Boat sandbox {sandbox_id} was not ready after {int(seconds)} s ({found.state})")
            time.sleep(POLL_SECONDS)

    def connect(self, sandbox_id, *, timeout=None):
        """The running sandbox, resuming a stopped one (treated as a cold
        boot: past Boat's 10 minutes its machine is new; its exposed ports'
        tokens are asked again)."""
        from boat_sdk.models import ResumeRequest, UpdateSandboxRequest
        found = self._sandbox(sandbox_id)
        if found.state == "archiving":
            found = self._settle(sandbox_id)
        if found.state in ("archived", "error"):
            self._forget(sandbox_id)
            # No ttlSeconds without a timeout: null would turn its auto-stop off for good.
            self.call(lambda: self.api().resume(sandbox_id, ResumeRequest(**given(ttl_seconds=timeout))),
                      sandbox_id=sandbox_id, asked={"timeout_seconds": timeout})
        elif timeout is not None:
            self.call(lambda: self.api().update(sandbox_id, UpdateSandboxRequest(ttl_seconds=timeout)),
                      sandbox_id=sandbox_id, asked={"timeout_seconds": timeout})
        self._ready(sandbox_id)
        self._machine_ready(sandbox_id)
        return Handle(self, sandbox_id)

    def handle(self, sandbox_id):
        """No resume and no mount: Boat answers a call on a stopped sandbox itself."""
        return Handle(self, sandbox_id)

    def _machine_ready(self, sandbox_id: str):
        """What sbx needs of a Boat machine that its image doesn't give:
        /run/sbx on a tmpfs of its own that runs programs. Boat's /run is
        mounted noexec (Ubuntu's default), and the recipe's scripts
        (.sbx/setup, start) run from /run/sbx: there they would be skipped
        as not executable. Once per machine (a resume may get a new one)."""
        Handle(self, sandbox_id).commands.run(RUN_MOUNT, timeout=60)

    def _settle(self, sandbox_id: str, seconds: float = 900):
        """Wait while Boat saves a stopping sandbox's disk."""
        deadline = time.monotonic() + seconds
        while True:
            found = self._sandbox(sandbox_id)
            if found.state != "archiving":
                return found
            if time.monotonic() > deadline:
                raise Busy(f"Boat is still saving sandbox {sandbox_id}'s disk")
            time.sleep(POLL_SECONDS)

    def sandboxes(self, metadata=None, states=None):
        wanted = None
        if states is not None:
            wanted = ",".join(s for state in states for s in (UP if state == RUNNING else STOPPED if state == PAUSED
                                                              else ("error",)))
        labels = [f"{k}={v}" for k, v in (metadata or {}).items()]
        found, cursor = [], None
        while True:
            page = self.call(lambda: self.api().sandboxes(limit=200, cursor=cursor, state=wanted, label=labels or None,
                                                          **self._org_args()))
            found += [self._info(s) for s in page.sandboxes if s.state not in GONE
                      and all((s.labels or {}).get(k) == v for k, v in (metadata or {}).items())]
            info = page.page_info
            if not info or not info.has_more or not info.next_cursor:
                return found
            cursor = info.next_cursor

    def info(self, sandbox_id):
        return self._info(self._sandbox(sandbox_id))

    def kill(self, sandbox_id):
        """Delete it and its snapshots (Boat finishes on its own: async)."""
        self._forget(sandbox_id, gone=True)
        try:
            self.call(lambda: self.api().delete_sandbox(x_ascii_confirm_delete=sandbox_id, sandbox_id=sandbox_id),
                      sandbox_id=sandbox_id)
        except NotRunning:
            return False
        return True

    def pause(self, sandbox_id):
        """Boat's stop: the disk is kept, the machine goes after 10 minutes (cold boot on a later resume)."""
        found = self._sandbox(sandbox_id)
        if found.state == "archived":
            return
        self._forget(sandbox_id)
        if found.state != "archiving":
            try:
                self.call(lambda: self.api().stop(sandbox_id), sandbox_id=sandbox_id, retry_seconds=0)
            except NotRunning:
                raise
            except Failed as error:
                if not (isinstance(error, BoatError) and error.code == "sandbox_stopping"):
                    raise Busy(f"Boat would not stop sandbox {sandbox_id} now: {error}") from error
        found = self._settle(sandbox_id)
        if found.state != "archived":
            raise Busy(f"Boat did not stop sandbox {sandbox_id}: it is {found.state}")

    def set_timeout(self, sandbox_id, seconds):
        from boat_sdk.models import UpdateSandboxRequest
        found = self.call(lambda: self.api().update(sandbox_id, UpdateSandboxRequest(ttl_seconds=seconds)),
                          sandbox_id=sandbox_id, asked={"timeout_seconds": seconds}).sandbox
        if found.state not in UP:
            raise NotRunning(f"Boat sandbox {sandbox_id} is {found.state}")


    def env_dir(self, sandbox_id: str) -> str:
        """ENV_DIR on the sandbox's machine (0700, the default user's), made once per machine."""
        kept = self._kept_of(sandbox_id)
        if not kept.env_dir:
            Handle(self, sandbox_id).commands.run(f"install -d -m 700 {ENV_DIR}", timeout=30)
            kept.env_dir = True
        return ENV_DIR

    def env_file(self, sandbox_id: str, envs: dict) -> str:
        """envs as a file in the sandbox (ENV_DIR, 0700), through the files
        API: what a command reads once and removes (sandbox.wrap)."""
        path = f"{self.env_dir(sandbox_id)}/{uuid.uuid4().hex}"
        Handle(self, sandbox_id).files._put(path, "".join(export_line(k, v) for k, v in envs.items()).encode())
        return path
