"""A runtime added, changed, removed or made the default from the web app:
sbx-control checks it (runtimes.prepare: the provider takes the settings, no
sandbox is left on a runtime it removes) and queues it for the root side; it
never edits /etc/sbx/runtime.env itself.

The request is /run/sbx/apply/runtime-change (settings.HostUnit.RUNTIME_CHANGE):
JSON with the operation, the kind and the values, a secret among them only
as a reference into the managed store (Infisical), never its value. Its path
unit runs `sbx-apply runtime-change` as root, which is `sbx-admin runtime
apply-pending`: the change made with the same code as sbx-admin runtime
add | remove | default (perform), checked the way sbx-control reads it, then
nginx regenerated and sbx-control restarted. Its result is recorded in
<state>/runtime-change.json, which GET /api/runtimes/change reads (status):
idle, queued, applying, done or failed, with what it said, and whether the
restart that takes a done change is still to come (restarting).

One change at a time: the apply lock (runtime-change.lock) is held while a
change is written to runtime.env (apply_pending, and sbx-admin runtime add |
remove | default) and while a request is written; the queue lock
(runtime-change.queue.lock) while sbx-control checks one and queues it."""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import sys
import time

from . import paths, runtime, runtimes, settings
from .settings import HostUnit

OPERATIONS = ("add", "change", "remove", "default")
# A request the root side has not taken this long is given up: its path unit
# is not running (sbx-apply-runtime-change.path).
PICKUP_SECONDS = 120
# How long after a done change the web app waits for sbx-control's restart.
RESTART_SECONDS = 120
STARTED = time.time()   # this process: a done change before it was taken by a restart


class Busy(runtimes.Conflict):
    """A change is queued or being applied already."""


def _request_file():
    return paths.run() / "apply" / HostUnit.RUNTIME_CHANGE


def _record_file():
    return paths.state_dir() / "runtime-change.json"


def _lock_file():
    return paths.state_dir() / "runtime-change.lock"


def _queue_lock_file():
    return paths.state_dir() / "runtime-change.queue.lock"


@contextlib.contextmanager
def _flock(path, *, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def locked():
    """Held while runtime.env changes: apply_pending, and sbx-admin runtime add | remove | default."""
    return _flock(_lock_file())


def queuing():
    """Held while sbx-control checks a change and queues it: a second request waits, then finds it busy."""
    return _flock(_queue_lock_file())


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _write(path, text: str) -> None:
    """Whole, or not at all, readable by root alone: the path unit starts as soon as the name exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    temporary.touch(mode=0o600)
    temporary.chmod(0o600)
    temporary.write_text(text)
    temporary.replace(path)


def _record(record: dict) -> None:
    _write(_record_file(), json.dumps(record))


def _read_record() -> dict:
    try:
        return json.loads(_record_file().read_text())
    except (OSError, ValueError):
        return {}


def _applying() -> bool:
    """Whether a change holds the apply lock now."""
    try:
        with open(_lock_file(), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    return False


def _give_up_unclaimed(record: dict) -> dict:
    """A request queued PICKUP_SECONDS ago that the root side has not taken:
    it goes, with the store references it carried, and the change failed."""
    try:
        with _flock(_lock_file(), blocking=False):
            try:
                payload = json.loads(_request_file().read_text())
            except FileNotFoundError:
                return record
            except ValueError:
                payload = {}
            _request_file().unlink(missing_ok=True)
            discard_references(payload.get("values") or {})
            record = {**record, "state": "failed", "at": _now(), "message": (
                "the root side did not pick up the change: check sbx-apply-runtime-change.path "
                "(systemctl status sbx-apply-runtime-change.path) and try again")}
            _record(record)
            return record
    except BlockingIOError:   # the root side took it just now
        return record


def status() -> dict:
    """GET /api/runtimes/change: {state: idle|queued|applying|done|failed,
    operation, kind, message, at, restarting}. A change recorded as queued
    or applying whose request and process are both gone stopped before it
    finished; one queued that the root side never takes fails after
    PICKUP_SECONDS. restarting: a done change whose sbx-control restart has
    not happened yet (this process started before it was done), for at most
    RESTART_SECONDS."""
    record = _read_record()
    found = record.get("state") or "idle"
    if found == "queued" and _request_file().exists() and not _applying() \
            and time.time() - record.get("queued_at", time.time()) > PICKUP_SECONDS:
        record = _give_up_unclaimed(record)
        found = record.get("state") or found
    if found in ("queued", "applying") and not _request_file().exists() and not _applying():
        # The root side may have finished between the first read and these checks: read again.
        record = _read_record()
        found = record.get("state") or "idle"
        if found in ("queued", "applying"):
            found, record = "failed", {**record, "message": "the change stopped before it finished; nothing to wait for"}
    finished = record.get("finished")
    restarting = (found == "done" and record.get("restarts", False) and finished is not None
                  and STARTED < finished and time.time() - finished < RESTART_SECONDS)
    return {"state": found, "operation": record.get("operation", ""), "kind": record.get("kind", ""),
            "message": record.get("message", ""), "at": record.get("at", ""), "restarting": bool(restarting)}


def busy() -> bool:
    return status()["state"] in ("queued", "applying")


def discard_references(values: dict) -> None:
    """The store references values carry (a refused or dropped request's)."""
    from . import secret_store
    for value in (values or {}).values():
        if isinstance(value, str) and secret_store.is_reference(value):
            try:
                secret_store.discard(value)
            except Exception as error:   # gone already, or the store did not answer
                print(f"runtime change: a sealed value was not discarded: {error}", file=sys.stderr, flush=True)


def request(operation: str, kind: str, values: dict | None = None, *, by: str) -> dict:
    """Queue a change sbx-control checked already (runtimes.prepare,
    removal_refused, default_refused): values hold a secret only as a store
    reference. Busy while another is queued or applying. The request file
    goes first, then the record, both under the apply lock, so the root side
    (which takes that lock) records "applying" only after "queued"."""
    if operation not in OPERATIONS:
        raise runtimes.Refused(f"no runtime change {operation}")
    with locked():   # held: no change is being applied now, so only a request not yet taken is in the way
        if _request_file().exists():
            raise Busy("a runtime change is being applied already; wait for it to finish")
        at = _now()
        _write(_request_file(), json.dumps({"operation": operation, "kind": kind, "values": values or {}, "by": by,
                                            "at": at}))
        _record({"state": "queued", "operation": operation, "kind": kind, "message": "", "at": at, "by": by,
                 "queued_at": time.time()})
    print(f"runtimes: {by} asked to {operation} {kind}", flush=True)
    return status()


def perform(operation: str, kind: str, values: dict | None = None) -> runtimes.Change:
    """Make the change in runtime.env, check it the way sbx-control will read
    it (platform.connect), and only then let go of the store references it
    replaced; any failure before that puts the file back, drops the
    references it made and raises. A failure letting go of an old reference
    leaves the change made (said says so). Call it holding locked():
    sbx-admin runtime add | remove | default and apply-pending."""
    from . import platform
    change = None   # removal configures the one runtime it lists (runtimes.removal_refused)
    try:
        if operation in ("add", "change"):
            change = runtimes.add(kind, values or {})
        elif operation == "remove":
            change = runtimes.remove(kind)
        elif operation == "default":
            change = runtimes.set_default(kind)
        else:
            raise runtimes.Refused(f"no runtime change {operation}")
        platform.connect()
    except (Exception, SystemExit):
        if change is not None:
            change.undo()
            try:
                platform.connect()   # the backends as the file has them again
            except (Exception, SystemExit):
                pass                 # it was so before (add mends a missing key)
        raise
    try:
        change.confirm()
    except Exception as error:   # the change is made; an old value stays in the store
        print(f"runtime change: an old sealed value was not discarded: {error}", file=sys.stderr, flush=True)
        change.said += f" (an old sealed value was not discarded from the store: {type(error).__name__})"
    if operation in ("add", "change"):
        runtime.forget_limit_hit(kind)   # what the provider refused before was against the old settings
    return change


def request_effects() -> None:
    """What the change needs once runtime.env is right: nginx's upstreams are
    per enabled runtime (tls.sh), and sbx-control configures each runtime at
    start: both again."""
    settings.request_effects(HostUnit.TLS, HostUnit.CONTROL)


def _discard_unused(values: dict) -> None:
    """The store references a failed request carried that runtime.env does not name."""
    text = paths.runtime_env().read_text() if paths.runtime_env().exists() else ""
    discard_references({k: v for k, v in (values or {}).items() if not (isinstance(v, str) and v in text)})


def apply_pending() -> int:
    """sbx-admin runtime apply-pending (sbx-apply runtime-change), as root: the
    queued change, under the lock status() looks at, taken before the request
    goes so the change never looks finished in between. 0 when it was made."""
    with locked():
        try:
            payload = json.loads(_request_file().read_text())
        except FileNotFoundError:
            print("no runtime change was requested", file=sys.stderr)
            return 2
        except ValueError:
            _request_file().unlink(missing_ok=True)
            _record({"state": "failed", "operation": "", "kind": "", "message": "the request was unreadable",
                     "at": _now()})
            return 1
        _request_file().unlink(missing_ok=True)
        operation, kind, values = payload.get("operation", ""), payload.get("kind", ""), payload.get("values") or {}
        record = {"operation": operation, "kind": kind, "by": payload.get("by", ""), "at": _now()}
        _record({**record, "state": "applying", "message": ""})
        try:
            change = perform(operation, kind, values)
        except (Exception, SystemExit) as error:
            _discard_unused(values)
            _record({**record, "state": "failed", "message": str(error), "at": _now()})
            print(f"runtime change refused: {error}", file=sys.stderr, flush=True)
            return 1
        _record({**record, "state": "done", "message": change.said, "at": _now(), "finished": time.time(),
                 "restarts": True})
    print(change.said, flush=True)
    request_effects()
    if operation == "add":
        from .admin import start_template_builds
        start_template_builds(kind)
    return 0
