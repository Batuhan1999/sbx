"""An in-memory sandbox runtime: what the test suite runs sbx on, next to
Embed, and the reference for what every backend must do (the conformance
tests in tests/test_runtimes.py run against it). It is hosted, not on this
host: it inherits Backend's no-op host part.

Commands understand just enough to be checked: `echo WORDS`, `cat PATH`,
`true`, `false` and `exit N`; anything else succeeds with no output unless a
test sets Fake.respond(sandbox, command) to answer it."""
from __future__ import annotations

import dataclasses
import datetime
import itertools
import shlex
import threading
from types import SimpleNamespace

from ..base import (PAUSED, RUNNING, Busy, Capabilities, CommandFailed, Failed, Backend, Info, Limits, NoRoom,
                    NotRunning, SettingField, Snapshot, TemplateGone, TemplateSpec, step_line)

INTERNAL_HOST_SUFFIX = "fake.sandboxes.internal"
CONTROL_PORT = 49999        # its own daemon in every sandbox: never a preview


@dataclasses.dataclass
class _Sandbox:
    sandbox_id: str
    template: str
    metadata: dict
    envs: dict
    state: str = RUNNING
    files: dict = dataclasses.field(default_factory=dict)
    started_at: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    timeout: int | None = None
    deny_out: list | None = None


class _Commands:
    def __init__(self, runtime: "Fake", sandbox_id: str):
        self._runtime, self._id = runtime, sandbox_id

    def run(self, cmd, *, user=None, envs=None, background=False, timeout=60):
        box = self._runtime._running(self._id)
        self._runtime.ran.append((self._id, cmd))
        answer = self._runtime.respond(box, cmd) if self._runtime.respond else None
        if answer is None:
            answer = _interpret(box, cmd)
        stdout, exit_code = (answer, 0) if isinstance(answer, str) else answer
        if exit_code:
            raise CommandFailed(stdout=stdout, stderr=stdout, exit_code=exit_code, command=cmd)
        result = SimpleNamespace(stdout=stdout, stderr="", exit_code=0)
        if background:
            result.disconnect = lambda: None
        return result


def _interpret(box: _Sandbox, cmd: str):
    """(stdout, exit code) of the few commands the fake runs itself."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        return "", 0
    if words[:1] == ["echo"]:
        return " ".join(words[1:]) + "\n", 0
    if words[:1] == ["cat"] and len(words) == 2:
        return (box.files[words[1]], 0) if words[1] in box.files else (f"cat: {words[1]}: No such file\n", 1)
    if words == ["false"]:
        return "", 1
    if words[:1] == ["exit"] and len(words) == 2 and words[1].isdigit():
        return "", int(words[1])
    return "", 0


class _Files:
    def __init__(self, runtime: "Fake", sandbox_id: str):
        self._runtime, self._id = runtime, sandbox_id

    def write(self, path, data, *, user=None):
        self._runtime._running(self._id).files[path] = data if isinstance(data, str) else bytes(data).decode()

    def read(self, path, *, format="text"):
        box = self._runtime._running(self._id)
        if path not in box.files:
            raise CommandFailed(f"{path}: no such file", exit_code=1)
        data = box.files[path]
        return (data if isinstance(data, bytes) else data.encode()) if format == "bytes" else data


class _Handle:
    def __init__(self, runtime: "Fake", sandbox_id: str):
        self.sandbox_id = sandbox_id
        self.commands = _Commands(runtime, sandbox_id)
        self.files = _Files(runtime, sandbox_id)
        self._runtime = runtime

    def kill(self):
        return self._runtime.kill(self.sandbox_id)


class Fake(Backend):
    kind = "fake"
    title = "Fake runtime"
    capabilities = Capabilities(snapshots=True, private_networks=False, egress_rules=False,
                                image_import=True)
    ingress = "http://127.0.0.1:3999"
    ingress_cleared = ("X-Fake-Sandbox",)
    internal_suffix = INTERNAL_HOST_SUFFIX
    ports = frozenset({CONTROL_PORT})
    credential_keys = ("FAKE_RUNTIME_TOKEN",)
    secret_key = "FAKE_RUNTIME_TOKEN"
    settings = (SettingField("FAKE_RUNTIME_TOKEN", "Token", secret=True),)
    id_prefix = "fk"

    def __init__(self):
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.boxes: dict[str, _Sandbox] = {}
        self.built: dict[str, str] = {}          # template alias -> build status
        self.snapshots: dict[str, dict] = {}     # snapshot id -> the files it holds
        self.credentials: dict = {}
        self.limit = Limits()
        self.busy = False                        # pause raises Busy while set
        self.full = False                        # create raises NoRoom while set
        self.respond = None                      # (sandbox, command) -> stdout, (stdout, code) or None
        self.ran: list = []                      # (sandbox id, command) of every command, in order

    def configure(self, secrets):
        """Its own keys only, kept on the instance (no process-global state)."""
        self.credentials = dict(secrets)

    def _running(self, sandbox_id) -> _Sandbox:
        box = self.boxes.get(sandbox_id)
        if box is None or box.state != RUNNING:
            raise NotRunning(f"sandbox {sandbox_id} is not running")
        return box

    def _info(self, box: _Sandbox) -> Info:
        return Info(sandbox_id=box.sandbox_id, metadata=dict(box.metadata), state=box.state, template_id=box.template,
                    started_at=box.started_at, cpu_count=2, memory_mb=4096)

    def upstream_host(self, port, sandbox_id):
        return f"{port}-{sandbox_id}.{INTERNAL_HOST_SUFFIX}"

    def own_route(self, host, sandbox_id, port):
        return None

    def create(self, template, *, timeout, envs=None, metadata=None, deny_out=None, on_timeout=None,
               auto_resume=None):
        if self.full:
            raise NoRoom("the fake runtime is full")
        if template not in self.built and template not in self.snapshots:
            raise TemplateGone(template, f"the fake runtime has no template or snapshot {template}")
        with self._lock:
            sandbox_id = f"{self.id_prefix}{next(self._ids):06d}"
            self.boxes[sandbox_id] = _Sandbox(sandbox_id, template, dict(metadata or {}), dict(envs or {}),
                                              files=dict(self.snapshots.get(template, {})), timeout=timeout,
                                              deny_out=deny_out)
        return _Handle(self, sandbox_id)

    def connect(self, sandbox_id, *, timeout=None):
        box = self.boxes.get(sandbox_id)
        if box is None:
            raise NotRunning(f"no sandbox {sandbox_id}")
        box.state = RUNNING
        if timeout is not None:
            box.timeout = timeout
        return _Handle(self, sandbox_id)

    def sandboxes(self, metadata=None, states=None):
        wanted = states or [RUNNING, PAUSED]
        return [self._info(box) for box in list(self.boxes.values()) if box.state in wanted
                and all(box.metadata.get(k) == v for k, v in (metadata or {}).items())]

    def info(self, sandbox_id):
        box = self.boxes.get(sandbox_id)
        if box is None:
            raise NotRunning(f"no sandbox {sandbox_id}")
        return self._info(box)

    def kill(self, sandbox_id):
        return self.boxes.pop(sandbox_id, None) is not None

    def pause(self, sandbox_id):
        box = self.boxes.get(sandbox_id)
        if box is None:
            raise NotRunning(f"no sandbox {sandbox_id}")
        if self.busy:
            raise Busy("the fake runtime is busy")
        box.state = PAUSED

    def set_timeout(self, sandbox_id, seconds):
        self._running(sandbox_id).timeout = seconds

    def snapshot(self, sandbox_id, name):
        box = self.boxes.get(sandbox_id)
        if box is None:
            raise NotRunning(f"no sandbox {sandbox_id}")
        snapshot_id = f"{name}-{sandbox_id}"
        self.snapshots[snapshot_id] = dict(box.files)
        return Snapshot(snapshot_id)

    def delete_snapshot(self, snapshot_id):
        self.snapshots.pop(snapshot_id, None)

    def build_template(self, spec: TemplateSpec, *, alias, cpu, memory_mb, disk_mb, on_log=None):
        for what, value in spec.steps:
            if what == "template" and value not in self.built:
                raise Failed(f"the fake runtime has no template {value} to build {alias} from")
        if on_log:   # each step as a runtime's builder logs it (step_line), then the end
            for number, (what, _) in enumerate(spec.steps, 1):
                on_log(step_line(number, len(spec.steps), what.upper()))
            on_log(f"built {alias} ({len(spec.steps)} steps)")
        self.built[alias] = "ready"
        return alias

    def templates(self):
        return list(self.built.items())

    def delete_template(self, name):
        return self.built.pop(name, None) is not None

    def failure(self, error):
        return False

    def limits(self):
        return self.limit

    def status(self):
        from ...connections.base import Status
        running = sum(box.state == RUNNING for box in self.boxes.values())
        return Status("runtime", self.title, "connected", f"Fake · {running} running", {"running": running}, (),
                      ("test",))

    def test(self):
        from ...connections.base import Check
        return [Check("Fake API", True, "in memory")]
