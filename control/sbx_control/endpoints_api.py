"""/api/endpoints (admins): this host's private services exposed to sandboxes
on runtimes that don't reach its private network (endpoints.py). Adding and
removing one is recorded at once and applied by the root side; the web app
and `sbx endpoints` follow GET until no endpoint is applying. And the
sandboxes' own callback for new certificates (/api/sandbox/endpoint-certs)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import auth, endpoints

router = APIRouter()


class Added(BaseModel):
    server: str | None = None   # a managed server's endpoint
    name: str | None = None     # an internal host's (with target), or a managed server's own name
    target: str | None = None   # HOST:PORT within network.internal_cidrs
    repos: list[str] = []       # an internal host's: the repos whose sandboxes may use it


class Grants(BaseModel):
    repos: list[str]


def _refused(error: endpoints.Refused) -> HTTPException:
    return HTTPException(409 if isinstance(error, endpoints.Conflict) else 400, str(error))


def _missing(error: LookupError) -> HTTPException:
    return HTTPException(404, str(error).strip("'\""))


@router.get("/api/endpoints")
async def api_endpoints(email: str = Depends(auth.admin_user)):
    """{endpoints: [{name, address, kind, server, server_kind, target, repos, state: on|applying|failed,
    message, used_by}], candidates: [{server, kind, address, target}], domain, needed, applying, failure}."""
    return await run_in_threadpool(endpoints.describe)


@router.post("/api/endpoints", status_code=202)
def api_add(body: Added, email: str = Depends(auth.admin_user)):
    """{"server": NAME} or {"name": NAME, "target": "HOST:PORT", "repos": [...]}: 202 with the endpoint (applying)."""
    try:
        return endpoints.add(server=body.server, name=body.name, target=body.target, repos=body.repos, by=email)
    except endpoints.Refused as error:
        raise _refused(error) from None


@router.put("/api/endpoints/{name}")
def api_grants(name: str, body: Grants, email: str = Depends(auth.admin_user)):
    """Replace an internal host's repos; 400 for a managed server's endpoint (attach the server instead)."""
    try:
        return endpoints.set_repos(name, body.repos)
    except LookupError as error:
        raise _missing(error) from None
    except endpoints.Refused as error:
        raise _refused(error) from None


@router.delete("/api/endpoints/{name}", status_code=202)
def api_remove(name: str, email: str = Depends(auth.admin_user)):
    """409 naming the repos while sandboxes on a provider runtime use it."""
    try:
        return endpoints.remove(name)
    except LookupError as error:
        raise _missing(error) from None
    except endpoints.Refused as error:
        raise _refused(error) from None


@router.post("/api/sandbox/endpoint-certs")
def api_sandbox_certificates(request: Request):
    """The forwarder in a sandbox, with the sandbox's own credential (Bearer NAME:CREDENTIAL): new
    certificates and keys for the endpoints its repo may use now. 401 for a wrong credential, 404 for a
    sandbox that is gone, 403 when it may use none (a revoked grant). Never cached or logged."""
    name = endpoints.sandbox_of(request.headers.get("authorization", ""))
    if not name:
        raise HTTPException(401)
    try:
        found = endpoints.issue_for(name)
    except LookupError:
        raise HTTPException(404, "no such sandbox") from None
    except PermissionError as error:
        raise HTTPException(403, str(error)) from None
    from fastapi.responses import JSONResponse
    return JSONResponse({"endpoints": found}, headers={"Cache-Control": "no-store"})
