"""/api/sandboxes: a person's own sandboxes, for the CLI and the web app,
and the web app's richer lists and detail of everyone's they may see.
Each route checks access (auth) and calls the shared operations (sandboxes)."""
import re
import shlex

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import (addressing, auth, core, lifecycle, recipes, resources, runtime, sandboxes, settings, state,
               template_builds, views)
from .auth import api_user, is_admin, may_view

router = APIRouter()
RUNTIME_RETRY_SECONDS = 30


@router.get("/api/sandboxes/{name}/lifecycle")
def api_lifecycle(name: str, email: str = Depends(auth.api_user)):
    auth.require_registered(name, email)
    info = core.find(name)
    runtime = core.runtime_state(info)
    return lifecycle.get(name, runtime, info.sandbox_id if info else None) or {
        "phase": "unknown", "runtime": runtime, "readiness": "unknown"}


@router.get("/api/sandboxes")
def api_list(email: str = Depends(auth.api_user)):
    return sandboxes.owned_by(email)


@router.post("/api/sandboxes")
def api_create(body: sandboxes.CreateSandbox, start_app: bool = True, email: str = Depends(auth.api_user)):
    """Return once SSH works; the app starts in the background unless start_app is false."""
    try:
        return sandboxes.create(body, email, start_app=start_app)
    except (lifecycle.Conflict, core.SandboxError, recipes.RecipeError, template_builds.NotBuilt):
        raise
    except Exception as error:
        # The runtime failed before a VM was made (none recorded): the same request may be sent again.
        if core.runtime_failure(error) and not (lifecycle.get(body.name) or {}).get("sandbox_id"):
            title = runtime.title(body.runtime if body.runtime in runtime.enabled() else None)
            raise HTTPException(503, f"the sandbox runtime ({title}) did not take the sandbox: {error}. It may still "
                                     "be starting (sbx-admin network apply and upgrades recreate it); try again in a "
                                     "minute", headers={"Retry-After": str(RUNTIME_RETRY_SECONDS)}) from error
        raise HTTPException(502, str(error)) from error  # a service failed


@router.get("/api/sandboxes/{name}")
def api_get(name: str, wait: float = 0, email: str = Depends(auth.api_user)):
    """With wait (seconds, at most 60): once its operation finishes or moves
    to another phase, for a client following it (lifecycle.active, phase)."""
    info = auth.require_owned(name, email)
    if wait > 0:
        sandboxes.wait(name, min(wait, 60))
        info = core.find(name) or info
    return sandboxes.view(info)


class Exec(BaseModel):
    script: str
    timeout: int = Field(default=120, ge=1, le=3600)


@router.post("/api/sandboxes/{name}/exec")
def api_exec(name: str, body: Exec, email: str = Depends(auth.api_user)):
    """Run a script as the sandbox's user, with its shells' env: exit_code, stdout, stderr."""
    return sandboxes.execute(name, auth.require_owned(name, email), body.script, body.timeout)


@router.post("/api/sandboxes/{name}/restart")
def api_restart(name: str, email: str = Depends(auth.api_user)):
    return sandboxes.restart(name, auth.require_owned(name, email))


@router.post("/api/sandboxes/{name}/prepare")
def api_prepare_sandbox(name: str, email: str = Depends(auth.api_user)):
    """Reapply the recipe and setup to this checkout, preserving local source edits."""
    return sandboxes.restart(name, auth.require_owned(name, email), prepare=True)


@router.delete("/api/sandboxes/{name}")
def api_delete(name: str, email: str = Depends(auth.api_user)):
    core.valid_name(name)
    auth.require_registered(name, email)
    requested = core.find(name)
    if requested:
        auth.require_owned(name, email)
    with core.sandbox_operation(name):
        auth.require_registered(name, email)
        current = core.find(name)
        if (current.sandbox_id if current else None) != (requested.sandbox_id if requested else None):
            raise HTTPException(409, f"{name} was replaced while its deletion was waiting")
        if current:
            auth.require_owned(name, email)
        sandboxes.delete_locked(name)
        return {"deleted": name}


@router.post("/api/sandboxes/{name}/pause")
def api_pause(name: str, email: str = Depends(auth.api_user)):
    return sandboxes.pause(name, auth.require_owned(name, email))


@router.post("/api/sandboxes/{name}/resume")
def api_resume(name: str, start_app: bool = True, email: str = Depends(auth.api_user)):
    return sandboxes.resume(name, auth.require_owned(name, email), start_app=start_app)


# On request only, as it resumes a paused sandbox: the checkout's commit, the
# NAMES of its env (values never leave the sandbox) and the tail of the logs
# its sbx.yaml names, capped, with the secret values the server knows masked.
# Log paths are sbx.yaml's, which a branch can't change but the default branch
# can, so only plain files under /tmp, /var/log and /home/user, none hidden.

INSPECT_LOG_BYTES = 64 * 1024
LOG_PATH = re.compile(r"^/(?:tmp|var/log|home/user)(?:/[A-Za-z0-9_*?.-]+)+$")


def readable_log(path: str) -> bool:
    return bool(LOG_PATH.match(path)) and not any(part.startswith(".") for part in path.split("/"))


@router.get("/api/sandboxes/{name}/inspect")
def api_inspect(name: str, lines: int = 200, email: str = Depends(auth.api_user)):
    info = auth.require_viewable(name, email)
    a = core.app_of(info)
    lines = max(10, min(lines, 1000))
    paths = [p for p in core.app_logs(a) if readable_log(p)]
    script = (f"printf 'C:'; (cd {shlex.quote(a.workdir)} && git log -1 --format='%H%x09%s') 2>/dev/null | head -n1; "
              f"printf '\\nE:'; sed -n 's/^export \\([A-Za-z_][A-Za-z0-9_]*\\)=.*/\\1/p' {core.RUN}/env "
              f"{core.RUN}/app-env 2>/dev/null | sort -u | tr '\\n' ' '; printf '\\n'; "
              + "".join(f"for f in {p}; do [ -f \"$f\" ] && printf '\\036%s\\n' \"$f\" && tail -n {lines} \"$f\" "
                        f"| tail -c {INSPECT_LOG_BYTES}; done; " for p in paths) + "true")
    out = core.attach(name, "to inspect it", info=info).run(script, timeout=30).stdout
    head, _, rest = out.partition("\x1e")
    found = {line[:2]: line[2:] for line in head.splitlines() if line[:2] in ("C:", "E:")}
    commit, _, subject = found.get("C:", "").partition("\t")
    first = ["", found.get("E:", "")]
    logs = []
    for chunk in (rest.split("\x1e") if rest else []):
        path, _, text = chunk.partition("\n")
        logs.append({"path": path, "text": core.mask(text, info)})
    return {"name": name, "commit": commit.strip(), "subject": core.mask(subject.strip(), info),
            "env": sorted(set(first[1].split())), "logs": logs, "lines": lines,
            "skipped": [p for p in core.app_logs(a) if not readable_log(p)]}


@router.get("/api/sandboxes/{name}/resources")
def api_sandbox_resources(name: str, probe: bool = False, email: str = Depends(api_user)):
    info = core.find(name)
    if not info or not may_view((info.metadata or {}).get("owner"), core.shared(info.metadata), email):
        raise HTTPException(404, f"no sandbox {name}")
    return resources.snapshot(name, live=probe)


@router.get("/api/sandbox-list")
async def api_sandboxes_all(scope: str = "mine", repo: str = "", email: str = Depends(api_user)):
    """Every sandbox in a scope: mine, previews (PR previews, the team's),
    sessions (my agent connectors' sessions), all (admins). Richer than
    /api/sandboxes (the CLI's): origin, VM state, creation time, slug."""
    if scope == "all" and not is_admin(email):
        raise HTTPException(403, "admins see everyone's sandboxes")
    items = await run_in_threadpool(views.collect)
    if scope == "sessions":
        items = [i for i in items if i["origin"]["kind"] == "session" and i.get("owner") == email]
    else:
        items = [i for i in items if views.visible(i, email, scope)]
    items = [_item(i, email) for i in sorted(items, key=lambda i: i.get("created_at") or "", reverse=True)]
    if repo:
        items = [i for i in items if (i.get("app") or "").lower() == repo.lower()]
    return {"sandboxes": items, **await run_in_threadpool(capacities)}


def capacities() -> dict:
    """The default runtime's room (capacity, naming its kind), and with
    several runtimes each one's by kind (capacities)."""
    first = runtime.default()
    out = {"capacity": {**core.capacity(kind=first), "runtime": first}}
    if len(runtime.enabled()) > 1:
        out["capacities"] = {kind: {**core.capacity(kind=kind), "runtime": kind} for kind in runtime.enabled()}
    return out


def _item(i: dict, email: str) -> dict:
    """The sandbox as email sees it: its actions are what email may do now
    (the operations are its owner's; anyone who may view it reads its logs)."""
    out = {k: v for k, v in i.items() if k not in ("logs", "repo")}
    if "lifecycle" not in out:  # still being created (or failed) before its VM existed
        record = lifecycle.get(out["name"])
        out.update(lifecycle=record, readiness=record["readiness"] if record else "unknown",
                   app=out.get("app") or (record or {}).get("repository") or "")
    owner = out.get("owner") == email
    out["actions"] = {**{action: allowed and owner for action, allowed in out["actions"].items()},
                      "logs": bool(out.get("sandbox_id")) and may_view(out.get("owner"), bool(out.get("shared")), email)}
    if out.get("end_at") is not None and not isinstance(out["end_at"], str):
        out["end_at"] = out["end_at"].isoformat()
    if out.get("created_at") is not None and not isinstance(out["created_at"], str):
        out["created_at"] = out["created_at"].isoformat()
    return out


@router.get("/api/sandboxes/{name}/detail")
async def api_sandbox_detail(name: str, email: str = Depends(api_user)):
    """The sandbox page's view: the item, its services, activity, lifecycle
    facts. Its owner, admins, and anyone for a team-shared one."""
    found = await run_in_threadpool(views.find_item, name)
    if not found or not may_view(found.get("owner"), bool(found.get("shared")), email):
        raise HTTPException(404, f"no sandbox {name}")
    item = _item(found, email)
    services = await run_in_threadpool(views.services_of, name)
    events = [{"at": at, "kind": kind, "detail": detail, "internal": internal}
              for at, kind, detail, internal in state.events(name)]
    usage = views.snapshot_usage(item.get("sandbox_id") or "")
    a = recipes.load(item.get("app") or None) if item.get("app") else None
    previews = []
    if a:
        for suffix, p in addressing.previews(a).items():
            label = suffix.lstrip("-") or "web"
            previews.append({"name": label, "port": p["port"], "access": p["access"], "link": p.get("link", True),
                             "url": item["urls"].get(label, "")})
    return {**item, "services": services, "events": events, "snapshot": usage, "previews": previews,
            "paused_days": settings.current().sandboxes.paused_days}
