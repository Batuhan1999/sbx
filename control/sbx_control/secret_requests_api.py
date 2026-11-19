"""/api/secret-requests: an agent (an admin, or the first-run setup link's
holder) asks the owner for keys and follows the request; the owner submits
them on the keys page, signed in as an admin (never by the link alone).
`sbx secrets request | status | ls | cancel | submit`. Values go in and
never come out (secret_requests)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import auth, secret_requests, settings
from .setup_api import setup_or_admin

router = APIRouter()


class Item(BaseModel):
    target: str
    label: str = ""
    help_url: str = ""


class Asked(BaseModel):
    items: list[Item]
    expires_in: int | None = None   # seconds; 24 hours when left out
    note: str = ""


class Submitted(BaseModel):
    values: dict[str, str | None] = Field(default_factory=dict)


def _url(request: Request, request_id: str) -> str:
    """The keys page on the installation's own address (the request's while it has none)."""
    base = settings.current().base_url or f"{request.url.scheme}://{request.url.netloc}"
    return f"{base}/keys/{request_id}"


def _out(request: Request, found: dict) -> dict:
    return {**found, "url": _url(request, found["id"])}


def _found(request_id: str) -> dict:
    found = secret_requests.get(request_id)
    if found is None:
        raise HTTPException(404, "no such key request")
    return found


def owner(email: str = Depends(auth.admin_user)) -> str:
    """Who may submit keys: an admin signed in as themselves (a session or
    their API token); not the setup link, and not an installation without
    sign-in, where anyone with the link would be "signed in"."""
    if email == auth.ANONYMOUS:
        raise HTTPException(403, "this server has no sign-in, so a key page would work for anyone with its link: "
                                 "set up sign-in first")
    return email


@router.post("/api/secret-requests", status_code=201)
def api_create(body: Asked, request: Request, who: str = Depends(setup_or_admin)):
    """{id, url, state, items, ...}; 400 for a target the server can't take or a key to reuse it doesn't hold."""
    try:
        found = secret_requests.create([i.model_dump() for i in body.items], by=who, expires_in=body.expires_in,
                                       note=body.note)
    except secret_requests.Refused as error:
        raise HTTPException(400, str(error)) from None
    return _out(request, found)


@router.get("/api/secret-requests")
def api_list(request: Request, who: str = Depends(setup_or_admin)):
    return {"requests": [_out(request, r) for r in secret_requests.listed()]}


@router.get("/api/secret-requests/{request_id}")
def api_get(request_id: str, request: Request, who: str = Depends(setup_or_admin)):
    """The request's state (pending, completed, cancelled, expired) and items; never a value."""
    return _out(request, _found(request_id))


@router.delete("/api/secret-requests/{request_id}")
def api_cancel(request_id: str, request: Request, who: str = Depends(setup_or_admin)):
    try:
        return _out(request, secret_requests.cancel(request_id, by=who))
    except KeyError:
        raise HTTPException(404, "no such key request") from None
    except secret_requests.Gone as error:
        raise HTTPException(409, str(error)) from None


@router.post("/api/secret-requests/{request_id}/submit")
async def api_submit(request_id: str, body: Submitted, request: Request, email: str = Depends(owner)):
    """{request, ok, results: [{target, ok, error}]}: values by target, each
    stored where it goes or refused with why; 409 once the request is not
    pending."""
    try:
        found = await run_in_threadpool(secret_requests.submit, request_id, body.values, by=email)
    except KeyError:
        raise HTTPException(404, "no such key request") from None
    except secret_requests.Gone as error:
        raise HTTPException(409, str(error)) from None
    except secret_requests.Refused as error:
        raise HTTPException(400, str(error)) from None
    return {**found, "request": _out(request, found["request"])}


@router.get("/keys/{request_id}", include_in_schema=False)
def keys_page(request_id: str):
    """The link an agent hands the owner: the web app's keys page."""
    if not secret_requests.ID_RE.fullmatch(request_id):
        raise HTTPException(404)
    return RedirectResponse(f"/app/keys/{request_id}")
