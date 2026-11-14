"""Durable preparation checkpoints and fenced sandbox operations.

The lifecycle record (sandbox_lifecycle) is the one truth about a sandbox's
state: which operation runs or ran last, its phase, what is ready, and the
VM it is bound to. Everything the API reports as a sandbox's status or
readiness is computed here from it and the VM's runtime state."""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import threading
import uuid

from . import state


class Conflict(Exception):
    pass


class Superseded(Conflict):
    pass


# The machine: the phases an operation passes through, the checkpoints it may
# finish at (TERMINAL), and the transitions allowed between them (EDGES).
TERMINAL = {"workspace_ready", "services_ready", "app_ready", "app_unverified",
            "failed", "interrupted", "deleted", "unknown"}
EDGES = {
    "queued": {"allocating", "provisioning_services", "preparing_workspace", "starting_app",
               "restoring_runtime", "pausing", "deleting"},
    "provisioning_services": {"allocating", "services_ready"},
    "allocating": {"preparing_workspace", "workspace_ready"},
    "preparing_workspace": {"workspace_ready"},
    "workspace_ready": {"provisioning_services", "services_ready", "setting_up", "starting_app"},
    "services_ready": {"allocating", "preparing_workspace", "setting_up", "starting_app", "app_ready", "app_unverified"},
    "setting_up": {"starting_app"},
    "starting_app": {"app_ready", "app_unverified"},
    "restoring_runtime": {"provisioning_services", "preparing_workspace", "setting_up",
                          "starting_app", "workspace_ready", "services_ready", "app_ready", "app_unverified"},
    "pausing": TERMINAL - {"deleted"},
    "deleting": {"deleted"},
}
KINDS = {"create", "prepare", "restart", "resume", "pause", "delete", "env_update"}
# The phases each kind of operation may begin with (from queued).
FIRST = {"create": {"allocating", "provisioning_services"},
         "prepare": {"preparing_workspace"}, "restart": {"preparing_workspace", "starting_app"},
         "resume": {"restoring_runtime"}, "pause": {"pausing"}, "delete": {"deleting"},
         "env_update": {"preparing_workspace"}}
# A checkpoint is reached from the phase that produces it, or re-entered
# only when the record already holds it: phase -> (producer, field, value).
CHECKPOINTS = {"workspace_ready": ("preparing_workspace", "workspace_ready", True),
               "services_ready": ("provisioning_services", "services_ready", True),
               "app_ready": ("starting_app", "application", "healthy"),
               "app_unverified": ("starting_app", "application", "unverified")}
APPLICATION_PHASES = {"setting_up", "starting_app", "app_ready", "app_unverified"}
EFFECTS = {"workspace_ready": {"workspace_ready": True}, "services_ready": {"services_ready": True},
           "setting_up": {"application": "setting_up"}, "starting_app": {"application": "starting_app"},
           "app_ready": {"application": "healthy"}, "app_unverified": {"application": "unverified"},
           "deleted": {"runtime": "missing", "workspace_ready": False, "services_ready": False,
                       "application": "not_started"}}


def transition(record: dict, phase: str, finish: bool = False) -> dict:
    before = record["phase"]
    if before == "queued" and phase not in FIRST[record["operation"]]:
        raise Conflict(f"{record['operation']} cannot begin with {phase}")
    if phase != before and phase not in EDGES.get(before, set()):
        raise Conflict(f"invalid sandbox transition: {before} -> {phase}")
    if phase in APPLICATION_PHASES and not (record["workspace_ready"] and record["services_ready"]):
        raise Conflict("application work requires workspace and service checkpoints")
    if finish and phase not in TERMINAL:
        raise Conflict(f"{phase} is not a completed checkpoint")
    if phase in CHECKPOINTS:
        producer, field, value = CHECKPOINTS[phase]
        if before != producer and record[field] != value:
            raise Conflict(f"{phase} requires {producer} first")
    return {**record, **EFFECTS.get(phase, {}), "phase": phase, "active": not finish}


@contextlib.contextmanager
def transaction():
    with state.db() as conn:
        conn.execute("begin immediate")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _read(conn, name):
    row = conn.execute("select record from sandbox_lifecycle where name = ?", (name,)).fetchone()
    return json.loads(row[0]) if row else None


def _save(conn, name, record, event):
    record["revision"] += 1
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    conn.execute("insert into sandbox_lifecycle (name, record) values (?, ?) "
                 "on conflict(name) do update set record=excluded.record", (name, json.dumps(record)))
    conn.execute("insert into sandbox_lifecycle_events (name, revision, operation_id, phase, event) "
                 "values (?, ?, ?, ?, ?)",
                 (name, record["revision"], record["operation_id"], record["phase"], event))


def status(record: dict | None) -> str:
    """The one-word status the API, the CLI and the web app show, from the
    phase and the VM's runtime state."""
    if record is None:
        return "unknown"
    return ("deleted" if record["phase"] == "deleted" else
            "failed" if record["phase"] in {"failed", "interrupted"} else
            "creating" if record["operation"] == "create" and record["active"] else
            "starting" if record["active"] else
            "paused" if record["runtime"] == "paused" else
            "failed" if record["application"] == "failed" else
            "up" if record["application"] == "healthy" else "workspace_ready")


def actions(record: dict | None, status_word: str, vm: str = "") -> dict:
    """Which operations the sandbox takes now, as begin() and the operations
    check them: none while one runs (but delete, unless it is deleting),
    pause on a running one, resume on a paused one, restart on a running or
    failed one. status_word is status(); vm the runtime's state of its VM."""
    if status_word == "deleted":
        return {"pause": False, "resume": False, "restart": False, "remove": False}
    active = bool(record and record["active"])
    settled = not active and status_word not in ("creating", "starting")
    paused = status_word != "failed" and (status_word == "paused" or vm == "paused")
    running = status_word in ("up", "workspace_ready") and not paused and vm != "missing"
    return {"pause": settled and running, "resume": settled and paused,
            "restart": settled and (running or status_word == "failed"),
            "remove": not (active and record["operation"] == "delete")}


def readiness_of(record: dict) -> str:
    """How far a sandbox on its bound, running VM is usable: not_ready,
    workspace_ready, services_ready or app_ready."""
    restoring = record["active"] and record["operation"] == "resume" and record["phase"] in {"queued", "restoring_runtime"}
    if not record["workspace_ready"] or restoring or record["phase"] in {"deleted", "deleting", "pausing"}:
        return "not_ready"
    return ("app_ready" if record["application"] == "healthy" else
            "services_ready" if record["services_ready"] else "workspace_ready")


def settled(record: dict) -> str:
    """The checkpoint an operation that left the app alone finishes at."""
    return ("app_ready" if record["application"] == "healthy" else
            "app_unverified" if record["application"] == "unverified" else
            "services_ready" if record["services_ready"] else "workspace_ready")


def started(health: bool) -> str:
    return "app_ready" if health else "app_unverified"


def get(name: str, runtime: str | None = None, sandbox_id: str | None = None) -> dict | None:
    with state.db() as conn:
        record = _read(conn, name)
    if record is None:
        return None
    runtime = runtime or record["runtime"]
    binding_matches = sandbox_id is None or record["sandbox_id"] in (None, sandbox_id)
    readiness = (readiness_of(record) if binding_matches and runtime == "running" else "not_ready")
    service_state = ("ready" if record["services_ready"] else
                     "provisioning" if record["phase"] == "provisioning_services" else "pending")
    return {**record, "runtime": runtime, "readiness": readiness, "services": service_state,
            "status": status({**record, "runtime": runtime}),
            "binding_matches": binding_matches,
            "workspace_usable": record["workspace_ready"] and readiness != "not_ready"}


def assert_binding(name: str, sandbox_id: str):
    current = get(name, sandbox_id=sandbox_id)
    if current and not current["binding_matches"]:
        raise Conflict(f"{name} is bound to another VM; reconcile its lifecycle before operating on it")


@dataclass(frozen=True)
class Operation:
    name: str
    id: str

    def _current(self, conn):
        record = _read(conn, self.name)
        if record is None or record["operation_id"] != self.id or not record["active"]:
            raise Superseded(f"the operation for {self.name} is no longer current")
        return record

    def check(self):
        with state.db() as conn:
            self._current(conn)

    def configure_workspace(self, repo: str):
        with transaction() as conn:
            record = self._current(conn)
            if "repository" in record and record["repository"] != (repo or None):
                raise Conflict("an existing sandbox cannot switch repositories")
            record["repository"] = repo or None
            _save(conn, self.name, record, "workspace_configured")

    def advance(self, phase: str, *, finish=False, sandbox_id=None, runtime=None):
        with transaction() as conn:
            record = self._current(conn)
            if sandbox_id is not None and record["sandbox_id"] not in (None, sandbox_id):
                raise Conflict("an operation cannot replace its bound sandbox")
            record = transition({**record, **({"sandbox_id": sandbox_id} if sandbox_id is not None else {}),
                                 **({"runtime": runtime} if runtime is not None else {})}, phase, finish)
            _save(conn, self.name, record, "completed" if finish else "advanced")

    def fail(self, error: BaseException):
        with transaction() as conn:
            try:
                record = self._current(conn)
            except Superseded:
                return
            phase = record["phase"]
            record.update(phase="failed", active=False,
                          error=f"{record['operation']} failed during {phase} ({type(error).__name__})")
            if phase in {"setting_up", "starting_app"}:
                record["application"] = "failed"
            _save(conn, self.name, record, "failed")

    def invalidate_application(self):
        with transaction() as conn:
            record = self._current(conn)
            record["application"] = "not_started"
            _save(conn, self.name, record, "application_invalidated")


def begin(name: str, kind: str) -> Operation:
    if kind not in KINDS:
        raise ValueError(f"unknown sandbox operation: {kind}")
    with transaction() as conn:
        return _begin(conn, name, kind)


def _begin(conn, name, kind):
    record = _read(conn, name)
    if record and record["active"]:
        raise Conflict(f"{name} is busy with {record['operation']} ({record['phase']})")
    if record and record["phase"] == "deleted" and kind != "create":
        raise Conflict(f"{name} was deleted")
    if record is None or kind == "create":
        revision = record["revision"] if record else 0
        record = {"revision": revision, "phase": "unknown", "runtime": "unknown",
                  "workspace_ready": False, "services_ready": False,
                  "application": "not_started", "sandbox_id": None}
    record.update(operation=kind, operation_id=uuid.uuid4().hex, active=True,
                  previous_phase=record["phase"], phase="queued",
                  error=record.get("error") if kind in {"pause", "resume"} else None)
    if kind in {"prepare", "restart"}:
        record["application"] = "not_started"
    _save(conn, name, record, "requested")
    return Operation(name, record["operation_id"])


@contextlib.contextmanager
def operation(name: str, kind: str, existing: Operation | None = None):
    current = existing or begin(name, kind)
    if current.name != name:
        raise Conflict("operation belongs to another sandbox")
    current.check()
    with state.db() as conn:
        if _read(conn, name)["operation"] != kind:
            raise Conflict("operation kind does not match the requested action")
    try:
        yield current
        with state.db() as conn:
            record = _read(conn, name)
        if record and record["operation_id"] == current.id and record["active"]:
            raise Conflict(f"{kind} exited without completing its lifecycle checkpoint")
    except BaseException as error:
        current.fail(error)
        raise


def in_background(name: str, kind: str, work) -> Operation:
    """Begin the operation here, so a busy sandbox refuses the caller, then
    run work(operation) on a thread. A failure is the operation's (work runs
    it under operation()), and its message goes to the sandbox's events."""
    current = begin(name, kind)

    def run():
        try:
            work(current)
        except Exception as error:
            current.fail(error)
            state.event(name, f"{kind} failed", str(error)[-1500:])
    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception as error:
        current.fail(error)
        raise
    return current


def busy() -> set:
    with state.db() as conn:
        return {name for name, raw in conn.execute("select name, record from sandbox_lifecycle")
                if json.loads(raw)["active"]}


RUNNING = ("up", "creating", "starting")   # the statuses that hold a VM's memory


def running_count(names=None) -> int:
    """Sandboxes up or on their way up, of names (an iterable) or of all."""
    found = statuses()
    return sum(found.get(name) in RUNNING for name in (found if names is None else names))


def statuses() -> dict:
    with state.db() as conn:
        return {name: status(json.loads(raw)) for name, raw in conn.execute("select name, record from sandbox_lifecycle")}


def observe(name: str, sandbox_id: str, runtime: str) -> str | None:
    """Record the bound VM's runtime state as the runtime reports it; the
    state it replaced, or None when nothing changed."""
    with transaction() as conn:
        record = _read(conn, name)
        if record is None or record["sandbox_id"] not in (None, sandbox_id):
            return None
        if record["sandbox_id"] is None and record["active"]:
            return None
        if record["runtime"] == runtime:
            return None
        before, record["runtime"] = record["runtime"], runtime
        _save(conn, name, record, "runtime_observed")
        return before


def app_stopped(name: str, sandbox_id: str, why: str) -> bool:
    """Record that the app of a settled sandbox, last seen ready, is no longer
    running (the keeper found its process supervisor gone): its status is
    failed, which restart clears. False when that is not what the record says."""
    with transaction() as conn:
        record = _read(conn, name)
        if (record is None or record["active"] or record["sandbox_id"] != sandbox_id
                or record["runtime"] != "running" or record["application"] not in ("healthy", "unverified")):
            return False
        record.update(application="failed", error=f"the app stopped: {why}"[:1500])
        _save(conn, name, record, "application_stopped")
        return True


def abandon_all(conn, reason: str):
    """On a database moved to another host: none of its sandboxes exist there."""
    for name, raw in conn.execute("select name, record from sandbox_lifecycle").fetchall():
        record = json.loads(raw)
        if record["phase"] != "deleted":
            record.update(active=False, phase="failed", runtime="missing", error=reason)
            _save(conn, name, record, "abandoned")


def recover_interrupted():
    """The single control worker owns operations; startup never replays setup blindly."""
    with transaction() as conn:
        rows = conn.execute("select name, record from sandbox_lifecycle").fetchall()
        for name, raw in rows:
            record = json.loads(raw)
            if record["active"]:
                record.update(active=False, phase="interrupted", application="unknown",
                              error=f"control restarted during {record['operation']}; inspect and retry")
                _save(conn, name, record, "interrupted")


SCHEMA = state.Schema(tables="""
create table if not exists sandbox_lifecycle (name text primary key, record text not null);
create table if not exists sandbox_lifecycle_events (
    name text not null, revision integer not null, operation_id text not null,
    phase text not null, event text not null, at text not null default current_timestamp,
    primary key (name, revision));
""")
