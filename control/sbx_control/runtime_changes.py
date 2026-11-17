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
idle, queued, applying, done or failed, with what it said."""
from __future__ import annotations

import datetime
import fcntl
import json
import sys

from . import paths, runtime, runtimes, settings
from .settings import HostUnit

OPERATIONS = ("add", "change", "remove", "default")


class Busy(runtimes.Conflict):
    """A change is queued or being applied already."""


def _request_file():
    return paths.run() / "apply" / HostUnit.RUNTIME_CHANGE


def _record_file():
    return paths.state_dir() / "runtime-change.json"


def _lock_file():
    return paths.state_dir() / "runtime-change.lock"


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


def _applying() -> bool:
    """Whether apply_pending holds the lock now."""
    try:
        with open(_lock_file(), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    return False


def status() -> dict:
    """GET /api/runtimes/change: {state: idle|queued|applying|done|failed,
    operation, kind, message, at}. A change recorded as queued or applying
    whose request and process are both gone stopped before it finished."""
    try:
        record = json.loads(_record_file().read_text())
    except (OSError, ValueError):
        record = {}
    found = record.get("state") or "idle"
    if found in ("queued", "applying") and not _request_file().exists() and not _applying():
        found, record = "failed", {**record, "message": "the change stopped before it finished; nothing to wait for"}
    return {"state": found, "operation": record.get("operation", ""), "kind": record.get("kind", ""),
            "message": record.get("message", ""), "at": record.get("at", "")}


def request(operation: str, kind: str, values: dict | None = None, *, by: str) -> dict:
    """Queue a change sbx-control checked already (runtimes.prepare,
    removal_refused, default_refused): values hold a secret only as a store
    reference. Busy while another is queued or applying."""
    if operation not in OPERATIONS:
        raise runtimes.Refused(f"no runtime change {operation}")
    if status()["state"] in ("queued", "applying"):
        raise Busy("a runtime change is being applied already; wait for it to finish")
    at = _now()
    _record({"state": "queued", "operation": operation, "kind": kind, "message": "", "at": at, "by": by})
    _write(_request_file(), json.dumps({"operation": operation, "kind": kind, "values": values or {}, "by": by,
                                        "at": at}))
    print(f"runtimes: {by} asked to {operation} {kind}", flush=True)
    return status()


def perform(operation: str, kind: str, values: dict | None = None) -> runtimes.Change:
    """Make the change in runtime.env, check it the way sbx-control will read
    it (platform.connect), and only then let go of the store references it
    replaced; any failure puts the file back, drops the references it made
    and raises. sbx-admin runtime add | remove | default and apply-pending."""
    from . import platform
    change = None
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
    change.confirm()
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
    from . import secret_store
    text = paths.runtime_env().read_text() if paths.runtime_env().exists() else ""
    for value in (values or {}).values():
        if isinstance(value, str) and secret_store.is_reference(value) and value not in text:
            try:
                secret_store.discard(value)
            except Exception as error:   # gone already, or the store did not answer
                print(f"runtime change: a sealed value was not discarded: {error}", file=sys.stderr, flush=True)


def apply_pending() -> int:
    """sbx-admin runtime apply-pending (sbx-apply runtime-change), as root: the
    queued change, under the lock status() looks at, taken before the request
    goes so the change never looks finished in between. 0 when it was made."""
    lock_path = _lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
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
        _record({**record, "state": "done", "message": change.said, "at": _now()})
    print(change.said, flush=True)
    request_effects()
    if operation == "add":
        from .admin import start_template_builds
        start_template_builds(kind)
    return 0
