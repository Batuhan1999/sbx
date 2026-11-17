"""A runtime added, changed, removed or made the default from the web app:
sbx-control checks it (runtime_settings.prepare: the provider takes the
settings, no sandbox is left on a runtime it removes) and queues it for the
root side as the runtime-change effect (host_requests); it never edits
/etc/sbx/runtime.env itself.

The request is JSON with the operation, the kind and the values, a secret
among them only as a reference into the managed store (Infisical), never its
value. sbx-apply runtime-change (apply_request, also `sbx-admin runtime
apply-pending`) commits it with the same code as sbx-admin runtime add |
remove | default: the change made and checked the way sbx-control reads it,
then nginx regenerated and sbx-control restarted. The root side records how
it went; GET /api/runtimes/change reads that with what the request files say
(status): idle, queued, applying, done or failed, and whether the restart
that takes a done change is still to come (restarting).

One change at a time: the effect's lock is held while a change is made (by
the root side, or sbx-admin) and while a request is written."""
from __future__ import annotations

import datetime
import json
import sys
import time

from . import host_requests, paths, runtime, runtime_settings, runtimes, settings
from .runtime import env_file
from .settings import HostUnit

KEY = HostUnit.RUNTIME_CHANGE
# How long after a done change the web app waits for sbx-control's restart.
RESTART_SECONDS = 120
STARTED = time.time()   # this process: a done change before it was taken by a restart


class Busy(runtimes.Conflict):
    """A change is queued or being applied already."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _read(payload: str | None) -> dict:
    try:
        return json.loads(payload or "")
    except ValueError:
        return {}


def _view(state: str, record: dict, restarting: bool = False) -> dict:
    return {"state": state, "operation": record.get("operation", ""), "kind": record.get("kind", ""),
            "message": record.get("message", ""), "at": record.get("at", ""), "restarting": restarting,
            "unchanged": False}


def _give_up() -> None:
    """A request the root side never took (host_requests.stuck): it goes, with
    the store references it carried, and the change failed. Checked again
    under the lock: one queued since the first look is not this one."""
    try:
        with host_requests.lock(KEY, blocking=False):
            if not host_requests.overdue(KEY):
                return
            payload = host_requests.take(KEY)
            if payload is None:
                return
            request = _read(payload)
            discard_references(request.get("values") or {})
            host_requests.record(KEY, {"operation": request.get("operation", ""), "kind": request.get("kind", ""),
                                       "by": request.get("by", ""), "state": "failed", "at": _now(), "message": (
                "the root side did not pick up the change: check sbx-apply-runtime-change.path "
                "(systemctl status sbx-apply-runtime-change.path) and try again")})
    except BlockingIOError:   # the root side took it just now
        pass


def status() -> dict:
    """GET /api/runtimes/change: {state: idle|queued|applying|done|failed,
    operation, kind, message, at, restarting}. A request is queued (applying
    while the root side holds the lock); one never taken fails after
    host_requests.PICKUP_SECONDS. A change recorded as applying whose
    process is gone stopped before it finished. restarting: a done change
    whose sbx-control restart has not happened yet (this process started
    before it was done), for at most RESTART_SECONDS."""
    if host_requests.stuck(KEY):
        _give_up()
    applying = host_requests.applying(KEY)
    if host_requests.requested(KEY):
        try:
            request = _read(host_requests.request_file(KEY).read_text())
        except OSError:   # taken just now
            request = None
        if request is not None:
            return _view("applying" if applying else "queued", request)
    record = host_requests.last(KEY)
    found = record.get("state") or "idle"
    if found == "applying" and not applying:
        return _view("failed", {**record, "message": "the change stopped before it finished; nothing to wait for"})
    finished = record.get("finished")
    restarting = (found == "done" and finished is not None and STARTED < finished
                  and time.time() - finished < RESTART_SECONDS)
    return _view(found, record, restarting)


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
    """Queue a change sbx-control checked already (runtime_settings.prepare,
    remove_blocker, default_refused): values hold a secret only as a store
    reference. Busy while another is queued; under the lock, so it waits for
    one being applied."""
    if operation not in _PLANS:
        raise runtimes.Refused(f"no runtime change {operation}")
    with host_requests.lock(KEY):
        if host_requests.requested(KEY):
            raise Busy("a runtime change is being applied already; wait for it to finish")
        host_requests.queue(KEY, json.dumps({"operation": operation, "kind": kind, "values": values or {},
                                             "by": by, "at": _now()}))
    print(f"runtimes: {by} asked to {operation} {kind}", flush=True)
    return status()


_PLANS = {"add": runtime_settings.add, "change": runtime_settings.add,
          "remove": lambda kind, values: runtime_settings.remove(kind),
          "default": lambda kind, values: runtime_settings.set_default(kind)}


def _file_now() -> str | None:
    file = paths.runtime_env()
    return file.read_text() if file.exists() else None


def _put_back(before: str | None) -> None:
    if before is None:
        paths.runtime_env().unlink(missing_ok=True)
    else:
        host_requests.write_atomic(paths.runtime_env(), before, private=True)


def perform(operation: str, kind: str, values: dict | None = None) -> str:
    """Plan the change (runtime_settings), write it into runtime.env, check
    it the way sbx-control will read it (platform.connect), and only then let
    go of the store references it replaced: what it did. Any failure before
    that puts the file back, drops the references it made and raises. A
    failure letting go of an old reference leaves the change made (it says
    so). Call it holding host_requests.lock(KEY)."""
    from . import platform, secret_store
    plan = _PLANS.get(operation)
    if plan is None:
        raise runtimes.Refused(f"no runtime change {operation}")
    before = _file_now()
    edit = plan(kind, values or {})
    try:
        env_file.edit(edit.writes)
        said = edit.says()
        platform.connect()
    except (Exception, SystemExit):
        _put_back(before)
        for reference in edit.made:
            secret_store.discard(reference)
        try:
            platform.connect()   # the backends as the file has them again
        except (Exception, SystemExit):
            pass                 # it was so before (add mends a missing key)
        raise
    try:
        for reference in edit.stale:
            secret_store.discard(reference)
    except Exception as error:   # the change is made; an old value stays in the store
        print(f"runtime change: an old sealed value was not discarded: {error}", file=sys.stderr, flush=True)
        said += f" (an old sealed value was not discarded from the store: {type(error).__name__})"
    if operation in ("add", "change"):
        runtime.forget_limit_hit(kind)   # what the provider refused before was against the old settings
    return said


def commit(operation: str, kind: str, values: dict, *, made=lambda said: None) -> str:
    """The change made (perform), made(said) told, then what it needs: nginx's
    upstreams are per enabled runtime (tls.sh) and sbx-control configures each
    runtime at start, so both again, and an added runtime's templates start
    building. sbx-admin runtime add | remove | default and apply_request."""
    said = perform(operation, kind, values)
    made(said)
    print(said, flush=True)
    settings.request_effects(HostUnit.TLS, HostUnit.CONTROL)
    print("nginx is regenerated and sbx-control restarts within seconds", flush=True)
    if operation == "add":
        from .template_scheduler import start_template_builds
        start_template_builds(kind)
    return said


def _discard_unused(values: dict) -> None:
    """The store references a failed request carried that runtime.env does not name."""
    text = _file_now() or ""
    discard_references({k: v for k, v in (values or {}).items() if not (isinstance(v, str) and v in text)})


def apply_request(payload: str | None) -> int:
    """sbx-apply runtime-change, as root (host_requests.run: under the lock
    status() looks at, the request taken): the queued change committed, its
    outcome recorded before sbx-control restarts. 0 when it was made."""
    if payload is None:
        print("no runtime change was requested", file=sys.stderr)
        return 2
    request = _read(payload)
    if not request:
        host_requests.record(KEY, {"state": "failed", "operation": "", "kind": "",
                                   "message": "the request was unreadable", "at": _now()})
        return 1
    operation, kind, values = request.get("operation", ""), request.get("kind", ""), request.get("values") or {}
    record = {"operation": operation, "kind": kind, "by": request.get("by", ""), "at": _now()}
    host_requests.record(KEY, {**record, "state": "applying", "message": ""})
    try:
        commit(operation, kind, values, made=lambda said: host_requests.record(
            KEY, {**record, "state": "done", "message": said, "at": _now(), "finished": time.time()}))
    except (Exception, SystemExit) as error:
        _discard_unused(values)
        host_requests.record(KEY, {**record, "state": "failed", "message": str(error), "at": _now()})
        print(f"runtime change refused: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


def apply_pending() -> int:
    """sbx-admin runtime apply-pending: the queued change, as sbx-apply runtime-change takes it."""
    return host_requests.run(KEY, apply_request)
