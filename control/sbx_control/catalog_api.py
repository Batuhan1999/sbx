"""/api/catalog: packages, the installation's connections and a repo's
attachments; a sandbox calling its packages' actions. Anyone signed in
reads the catalog and a repo's attachments; admins write. No secret value
or sensitive output comes back."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import catalog, hosted_services, repo_path, repos, settings
from .auth import admin_user, api_user, is_admin
from .catalog import NoSuchPackage, PackageError, attachments, connections, resources

router = APIRouter()


@router.get("/api/catalog")
def api_catalog(kind: str | None = None, email: str = Depends(api_user)):
    used = attachments.used_by()
    return {"packages": [{**p.as_dict(sorted(used.get(p.name, ()))), "server": hosted_services.runs(p)}
                         for p in catalog.listing(kind)],
            "problems": catalog.problems()}


# The installation's connections (before /api/catalog/{name}, which would take the name).

def _connections() -> list:
    managed = {m["name"]: {"kind": m["kind"], "status": m["status"]} for m in hosted_services.records()}
    return [{**c, "managed": managed.get(c["key"])} for c in connections.listing()]


@router.get("/api/catalog/connections")
def api_connections(email: str = Depends(admin_user)):
    return {"connections": _connections()}


class ConnectionIn(BaseModel):
    package: str
    key: str = ""
    inputs: dict[str, str] = {}


@router.post("/api/catalog/connections")
async def api_connection_save(body: ConnectionIn, email: str = Depends(admin_user)):
    try:
        return await run_in_threadpool(connections.save, body.key or "main", body.package, body.inputs, by=email)
    except PackageError as error:
        raise HTTPException(400, str(error))


@router.post("/api/catalog/connections/{key}/check")
async def api_connection_check(key: str, email: str = Depends(admin_user)):
    try:
        why = await run_in_threadpool(connections.check, key)
    except PackageError as error:
        raise HTTPException(404, str(error))
    return {"key": key, "ok": not why, "detail": why}


@router.delete("/api/catalog/connections/{key}")
def api_connection_remove(key: str, email: str = Depends(admin_user)):
    if any(server["name"] == key for server in hosted_services.records()):
        # Its connection alone would leave the server running with nothing showing it.
        raise HTTPException(409, f"{key} is a managed server: remove the server (DELETE /api/servers/{key}, "
                                 f"sbx servers rm {key}), which removes its connection")
    try:
        if not connections.remove(key):
            raise HTTPException(404, f"no connection {key}")
    except PackageError as error:
        raise HTTPException(409, str(error))
    return {"key": key, "removed": True}


@router.get("/api/catalog/{name}")
def api_catalog_entry(name: str, email: str = Depends(api_user)):
    try:
        package = catalog.get(name)
    except PackageError as error:
        raise HTTPException(404, str(error))
    found = ([c for c in _connections() if c["package"] == name]
             if package.kind == "connection" and is_admin(email) else [])
    return {**package.as_dict(sorted(attachments.used_by().get(name, ()))), "readme": package.files.get("README.md", ""),
            "connections": found, "scripts": sorted(k for k in package.files if k not in ("package.yaml", package.logo))}


class PackageFiles(BaseModel):
    files: dict[str, str]
    message: str = ""


@router.get("/api/catalog/{name}/files")
def api_catalog_files(name: str, email: str = Depends(admin_user)):
    """A package's files, to copy from (`sbx catalog new --from`) or edit."""
    try:
        package = catalog.get(name)
    except PackageError as error:
        raise HTTPException(404, str(error))
    return {"name": name, "kind": package.kind, "source": package.source, "files": package.files}


@router.put("/api/catalog/{name}")
def api_catalog_push(name: str, body: PackageFiles, email: str = Depends(admin_user)):
    """An admin's version of a package, kept with its history and used from
    now on; the shipped and the operator's folders are untouched."""
    try:
        catalog.save(name, body.files, email, body.message or "sbx catalog push")
        package = catalog.get(name)
    except PackageError as error:
        raise HTTPException(400, f"not a package: {error}")
    return {"name": name, "kind": package.kind, "saved": True}


class TrialIn(BaseModel):
    inputs: dict[str, str] = {}
    connections: dict[str, str] = {}


@router.post("/api/catalog/{name}/test")
async def api_catalog_test(name: str, body: TrialIn, email: str = Depends(admin_user)):
    """A provision package's create, retry, list and delete for a throwaway
    sandbox on this host; a connection package's check (resources.trial)."""
    try:
        return await run_in_threadpool(resources.trial, name, body.inputs, body.connections)
    except PackageError as error:
        raise HTTPException(404 if isinstance(error, NoSuchPackage) else 400, str(error))


@router.get("/api/catalog-brief", response_class=PlainTextResponse)
def api_catalog_brief(what: str = "a service", name: str = "my-service", email: str = Depends(admin_user)):
    """What an agent gets to write a package: the format, the script contract, the loop."""
    safe = "".join(c for c in name.lower() if c.isalnum() or c in "-_") or "my-service"
    return repos.render_brief("package-brief.md", what=what, name=safe, control=settings.current().base_url)


@router.get("/api/repos/{owner}/{name}/attachments")
def api_attachments(email: str = Depends(api_user), repo: str = Depends(repo_path.trusted)):
    return {"attachments": attachments.effective(repo)}


class AttachIn(BaseModel):
    package: str
    key: str = ""
    inputs: dict[str, str] = {}
    connections: dict[str, str] = {}
    check: bool = True


@router.post("/api/repos/{owner}/{name}/attachments")
async def api_attach(body: AttachIn, email: str = Depends(admin_user), repo: str = Depends(repo_path.trusted)):
    """Attach a package, or give a key the repo's sbx.yaml declares its values;
    sandboxes made from now on get it (a live package's, when prepared)."""
    try:
        found = await run_in_threadpool(attachments.attach, repo, body.package, body.key or None, body.inputs,
                                        body.connections, by=email, run_check=body.check)
    except PackageError as error:
        raise HTTPException(400, str(error))
    return {"attachment": found}


@router.delete("/api/repos/{owner}/{name}/attachments/{key}")
def api_detach(key: str, email: str = Depends(admin_user), repo: str = Depends(repo_path.trusted)):
    try:
        what = attachments.detach(repo, key, by=email)
    except PackageError as error:
        raise HTTPException(404, str(error))
    return {"key": key, what: True}


@router.post("/api/repos/{owner}/{name}/attachments/{key}/check")
def api_attachment_check(key: str, email: str = Depends(api_user), repo: str = Depends(repo_path.trusted)):
    found = next((a for a in attachments.effective(repo) if a["key"] == key), None)
    if not found:
        raise HTTPException(404, f"{repo} has no {key}")
    return {"key": key, "ok": found["state"] == "ready", "detail": found["problem"] or ""}


# A sandbox calling one of its packages' actions (resources.action) with its
# resource's own token: no user session, never cached.

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


@router.post("/api/sandbox/actions/{name}/{key}/{action}")
async def api_sandbox_action(name: str, key: str, action: str, request: Request):
    header = request.headers.get("authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    raw = await request.body()
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise HTTPException(400, "the body is not JSON", headers=NO_STORE)
    try:
        result = await run_in_threadpool(resources.action, name, key, action, token, body if isinstance(body, dict) else {})
    except resources.Unauthorized as error:
        raise HTTPException(401, str(error), headers=NO_STORE)
    except ValueError as error:
        raise HTTPException(400, str(error), headers=NO_STORE)
    except NoSuchPackage as error:
        raise HTTPException(404, str(error), headers=NO_STORE)
    except PackageError as error:
        raise HTTPException(409, str(error), headers=NO_STORE)
    return JSONResponse(result, headers=NO_STORE)
