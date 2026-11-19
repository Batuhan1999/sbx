"""Admin connection API and browser handoff for GitHub App registration."""
import json
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from . import connections, extensions, github_app, template_builds, template_scheduler
from .auth import admin_user
from .setup_api import admin_or_first

router = APIRouter()


class Values(BaseModel):
    values: dict = {}


def _connection(kind: str) -> connections.Connection:
    try:
        return connections.get(kind)
    except KeyError:
        raise HTTPException(404, f"no connection {kind}")


@router.get("/api/connections")
def api_connections(email: str = Depends(admin_or_first)):
    return {"connections": connections.listed()}


@router.post("/api/connections/{kind}/test")
async def api_connection_test(kind: str, body: Values | None = None, email: str = Depends(admin_or_first)):
    checks = await run_in_threadpool(_connection(kind).test, (body.values if body else None))
    return {"ok": connections.checks_ok(checks), "checks": [c.__dict__ for c in checks]}


def _changing_templates(connection, change):
    """change() for an extension, and what it did to the templates
    (template_builds.rebuilds: how many rebuild on each runtime), whose
    builds then start; (status, None) for any other connection."""
    if extensions.get(connection.kind) is not connection:
        return change(), None
    before = template_builds.planned()
    status = change()
    report = template_builds.rebuilds(before)
    if report["building"]:
        template_scheduler.kick()
    return status, report


def _view(connection, status, report) -> dict:
    view = connection.view(status)
    return view if report is None else {**view, "rebuilds": report}


@router.put("/api/connections/{kind}")
async def api_connection_put(kind: str, body: Values, email: str = Depends(admin_or_first)):
    connection = _connection(kind)
    try:
        status, report = await run_in_threadpool(
            _changing_templates, connection, lambda: connection.connect(body.values, by=email))
    except connections.Refused as error:
        raise HTTPException(400, {"message": str(error), "checks": [c.__dict__ for c in error.checks]})
    return _view(connection, status, report)


@router.delete("/api/connections/{kind}")
async def api_connection_delete(kind: str, email: str = Depends(admin_user)):
    connection = _connection(kind)
    try:
        status, report = await run_in_threadpool(
            _changing_templates, connection, lambda: connection.disconnect(by=email))
    except connections.Refused as error:
        raise HTTPException(400, str(error))
    return _view(connection, status, report)


@router.get("/api/templates")
def api_templates(email: str = Depends(admin_or_first)):
    """Every template sandboxes start from, per enabled runtime, and where its
    build stands (template_builds.progress): what `sbx templates --wait` and
    `sbx extensions ... --wait` follow, and the onboarding page."""
    return template_builds.progress()


# The web app's pages GitHub may send the browser back to after the App is installed.
RETURN_PAGES = ("/app/extensions", "/app/repos", "/app/repos/new")


class ManifestStart(BaseModel):
    owner: str = ""
    return_to: str = "/app/extensions"


@router.post("/api/connections/github/manifest")
def api_github_manifest(request: Request, body: ManifestStart, email: str = Depends(admin_or_first)):
    """What the browser posts to GitHub (a form, so GitHub shows the App for
    confirmation); GitHub comes back to /connections/github/callback."""
    owner = body.owner.strip()
    if owner and not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise HTTPException(400, "not a GitHub organization name")
    nonce = secrets.token_urlsafe(16)
    request.session["github_state"] = nonce
    target = (f"https://github.com/organizations/{owner}/settings/apps/new" if owner
              else "https://github.com/settings/apps/new")
    if body.return_to not in RETURN_PAGES:
        raise HTTPException(400, "Invalid GitHub return page.")
    manifest = github_app.manifest(owner or email, body.return_to)
    return {"action_url": f"{target}?state={nonce}", "manifest": json.dumps(manifest)}


@router.get("/connections/github/callback")
def github_callback(request: Request, code: str, state: str):
    """GitHub's return: the nonce in the session proves the manifest was
    started here, by an admin."""
    if not secrets.compare_digest(state, request.session.pop("github_state", "") or ""):
        raise HTTPException(400, "this link is stale; start again in Extensions")
    admin_or_first(request)
    created = github_app.convert(code)
    return RedirectResponse(f"https://github.com/apps/{created['slug']}/installations/new")
