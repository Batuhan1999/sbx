"""/api/runtimes: the sandbox runtimes this installation runs, for the web
app (and its first-run wizard, before anyone can sign in) and `sbx runtimes`;
and, for admins (the setup token too while the first-run setup is open),
adding, changing, removing one and making one the default. Those are checked
here and queued for the root side (runtime_changes), which applies them and
restarts sbx-control; GET /api/runtimes/change follows it."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import auth, runtime, runtime_changes, runtimes, setup
from .setup_api import setup_or_admin

router = APIRouter()


def signed_in_or_setup(request: Request) -> str:
    """Anyone signed in, or the holder of the first-run setup's token while it is open."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("setup ") and setup.token_ok(header[6:].strip()):
        return "setup"
    return auth.api_user(request)


def _refused(error: runtimes.Refused) -> HTTPException:
    return HTTPException(409 if isinstance(error, runtimes.Conflict) else 400, str(error))


@router.get("/api/runtimes")
async def api_runtimes(who: str = Depends(signed_in_or_setup)):
    """{default, runtimes: [{kind, title, default, host, capabilities, limits, status: {state, summary},
    limit_hit}]}; with no runtime set up yet, {default: "", runtimes: []}."""
    return await run_in_threadpool(runtimes.describe)


@router.get("/api/runtimes/available")
async def api_available(who: str = Depends(setup_or_admin)):
    """Every runtime an installed package provides: [{kind, title, host, enabled, default, can_run, why_not,
    managed_by, settings, presets, values, secrets_set, facts, sandboxes, removable, limit_hit}]. Secrets
    are only named as set, never shown."""
    return await run_in_threadpool(runtimes.available)


@router.get("/api/runtimes/change")
def api_change(who: str = Depends(setup_or_admin)):
    """The last change: {state: idle|queued|applying|done|failed, operation, kind, message, at}."""
    return runtime_changes.status()


class Added(BaseModel):
    kind: str
    values: dict[str, str | None] = Field(default_factory=dict)


class Values(BaseModel):
    values: dict[str, str | None] = Field(default_factory=dict)


class Default(BaseModel):
    kind: str


def _queue(operation: str, kind: str, who: str, check) -> dict:
    """Check and queue under the queue lock, so a second request waits and finds
    this one queued; a secret check() sealed goes again when queueing is refused."""
    values = None
    try:
        with runtime_changes.queuing():
            if runtime_changes.busy():
                raise runtime_changes.Busy("a runtime change is being applied already; wait for it to finish")
            values = check()
            return runtime_changes.request(operation, kind, values, by=who)
    except BaseException as error:
        if values:
            runtime_changes.discard_references(values)
        if isinstance(error, runtimes.Refused):
            raise _refused(error) from None
        raise


@router.post("/api/runtimes", status_code=202)
def api_add(body: Added, who: str = Depends(setup_or_admin)):
    """Enable a provider's runtime with its settings (the secret store must be Infisical: 409 otherwise)."""
    return _queue("add", body.kind, who, lambda: runtimes.prepare(body.kind, body.values, adding=True))


@router.put("/api/runtimes/default", status_code=202)
def api_default(body: Default, who: str = Depends(setup_or_admin)):
    """202 with the queued change; 200, done, for the runtime that is the default already (nothing to restart)."""
    if runtimes.is_default(body.kind):
        return JSONResponse({"state": "done", "operation": "default", "kind": body.kind, "at": "", "restarting": False,
                             "message": f"{runtime.title(body.kind)} ({body.kind}) is the default already"})
    return _queue("default", body.kind, who, lambda: runtimes.default_refused(body.kind))


@router.put("/api/runtimes/{kind}", status_code=202)
def api_change_settings(kind: str, body: Values, who: str = Depends(setup_or_admin)):
    """New settings for an enabled provider's runtime; a secret left out (or empty) keeps its value."""
    return _queue("change", kind, who, lambda: runtimes.prepare(kind, body.values, adding=False))


@router.post("/api/runtimes/{kind}/test")
async def api_test(kind: str, who: str = Depends(setup_or_admin)):
    """An enabled runtime's checks: {kind, ok, checks: [{name, ok, detail, fix, runtime}]}; 400 when not enabled."""
    try:
        checks = await run_in_threadpool(runtimes.test, kind)
    except runtimes.Refused as error:
        raise _refused(error) from None
    return {"kind": kind, "ok": all(c["ok"] for c in checks), "checks": checks}


@router.delete("/api/runtimes/{kind}", status_code=202)
def api_remove(kind: str, who: str = Depends(setup_or_admin)):
    """Refused (409) while sandboxes run on it, naming them, or when it is the last one."""
    def check():
        backend = runtime.get(kind) if kind in runtime.enabled() else None
        if getattr(backend, "host", False):   # before any listing: install.sh's runtime is the server's to remove
            raise runtimes.Conflict(f"{backend.title} is set up by install.sh on this server; remove it there: "
                                    f"sbx-admin runtime remove {kind}")
        runtimes.removal_refused(kind)
    return _queue("remove", kind, who, check)
