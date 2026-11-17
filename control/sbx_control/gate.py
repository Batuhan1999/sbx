"""The gate in front of every preview.

nginx on the Embed host (and on this host, for the operator's services) asks
GET /gate before every request to *.<domain>, with the original Host in
X-Original-Host. 200 lets it through, 401 sends a browser to the login, 403
refuses; a 403 with X-Sbx-Gone means nothing is there, which nginx answers
with 404. A 200 for a sandbox names the Host nginx sends it to the runtime
with (X-Sbx-Upstream) and that runtime's kind (X-Sbx-Runtime), which nginx
maps to the runtime's ingress; for a runtime that answers a sandbox's ports
only with its token (E2B Cloud), also the token (X-Sbx-Upstream-Token), which
nginx adds in that runtime's header to that runtime's requests only. nginx
keeps /gate itself from the public (tls.sh, control.inc): only its own
subrequests reach it. Answers come from memory: a page load makes hundreds.
A request for a paused sandbox resumes it first."""
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from . import addressing, auth, core, ip_previews, lifecycle, routing, runtime

router = APIRouter()
GONE = "X-Sbx-Gone"
UPSTREAM, RUNTIME, TOKEN = "X-Sbx-Upstream", "X-Sbx-Runtime", "X-Sbx-Upstream-Token"

_owners: dict = {"fetched_at": 0.0, "by_id": {}, "state": {}, "app": {}, "info": {}}
_last_access: dict = {}  # sandbox id -> time the gate last let a request through
_resume_locks: dict = {}
# Sandboxes a preview request resumed, until they pause or are used
# otherwise (keeper): an extension that pauses its sandboxes when idle lets
# these run out their timeout (extensions.Use). An open dev-server socket is
# one request, so the sandbox looked unused after a few minutes, paused, the
# browser reconnected and resumed it: a 6 GB snapshot every 5 minutes.
_resumed_for_preview: set = set()


def last_access(sandbox_id: str) -> float:
    return _last_access.get(sandbox_id, 0)


def resumed_for_preview(sandbox_id: str) -> bool:
    return sandbox_id in _resumed_for_preview


def forget_preview_resume(sandbox_id: str):
    _resumed_for_preview.discard(sandbox_id)


def sandbox_owner(sandbox_id: str, fresh=False):
    if fresh or sandbox_id not in _owners["by_id"] or time.time() - _owners["fetched_at"] > 20:
        infos = core.sandboxes()
        _owners["by_id"] = {i.sandbox_id: (i.metadata or {}).get("owner") for i in infos}
        _owners["info"] = {i.sandbox_id: i for i in infos}
        _owners["app"] = {i.sandbox_id: core.app_of(i) for i in infos}
        _owners["state"] = {i.sandbox_id: core.runtime_state(i) for i in infos}
        _owners["fetched_at"] = time.time()
    return _owners["by_id"].get(sandbox_id)


def ensure_running(sandbox_id: str):
    """Resume a paused sandbox before its request goes through: Embed's
    client-proxy does not resume on traffic. One resume per sandbox at a
    time; the others wait for it."""
    idle_for = time.time() - _last_access.get(sandbox_id, 0)
    _last_access[sandbox_id] = time.time()
    if idle_for > 60:
        sandbox_owner(sandbox_id, fresh=True)  # it may have paused since the last look
    if _owners["state"].get(sandbox_id) != "paused":
        return
    lock = _resume_locks.setdefault(sandbox_id, threading.Lock())
    with lock:
        sandbox_owner(sandbox_id, fresh=True)
        if _owners["state"].get(sandbox_id) == "paused":
            info = _owners["info"][sandbox_id]
            core.attach((info.metadata or {})["sbx"], "for a preview request", info=info)
            _owners["state"][sandbox_id] = "running"
            _resumed_for_preview.add(sandbox_id)


def route(port: int, sandbox_id: str, *, preflight: bool = False) -> dict:
    """Where nginx sends a let-through request for a sandbox's port: the
    Host its runtime routes on, the runtime's kind (nginx's map picks its
    ingress from it) and, for a runtime that wants one, the sandbox's token.
    A preflight wakes nothing: it takes only a token the runtime kept, and
    without one it is refused like anything not routable (the browser's
    real request resumes the sandbox and fetches it). A sandbox the runtime
    no longer has is not found either (404)."""
    try:
        info = _owners["info"].get(sandbox_id)
        kind = runtime.kind_of(info) if info is not None else runtime.of(sandbox_id).kind
        found = {UPSTREAM: addressing.upstream(port, sandbox_id), RUNTIME: kind}
        token = runtime.upstream_token(sandbox_id, kind, fetch=not preflight)
    except runtime.NotRunning:
        raise HTTPException(404) from None
    except runtime.Failed as error:
        # The runtime did not hand over the sandbox's token (it did not answer,
        # or answered without one): refused like anything not routable now,
        # never a 500. Its backend keeps the failure briefly, so an outage is
        # not a call to the provider for every request.
        print(f"gate: no upstream token for {sandbox_id}: {error}", flush=True)
        raise HTTPException(404) from None
    if token:
        if not token[1]:
            raise HTTPException(404)
        found[TOKEN] = token[1]
    return found


@router.get("/gate")
def gate(request: Request):
    """nginx's auth_request passes on 2xx, 401 and 403 only and turns anything
    else into a 500, so "nothing there" is a 403 marked GONE."""
    try:
        return _gate(request)
    except HTTPException as error:
        if error.status_code == 404:
            raise HTTPException(403, headers={GONE: "1"})
        raise


def gone(sandbox_id: str) -> bool:
    """Whether the listed VM's sandbox is being deleted, was deleted, or is
    another VM than the one its name is bound to now: the runtime lists a
    deleted VM for a while."""
    info = _owners["info"].get(sandbox_id)
    name = (info.metadata or {}).get("sbx") if info else None
    current = lifecycle.get(name, sandbox_id=sandbox_id) if name else None
    return bool(current) and (current["phase"] in ("deleting", "deleted") or not current["binding_matches"])


def _gate(request: Request):
    public_port = request.headers.get('x-original-preview-port')
    ip_route = None
    if public_port:
        if not public_port.isdecimal() or request.headers.get('x-original-host', '').strip('[]') != ip_previews.address():
            raise HTTPException(403)
        ip_route = ip_previews.route(int(public_port))
        if ip_route is None:
            raise HTTPException(404)
    # A CORS preflight carries no credentials: let it through to a declared
    # preview without a login, waking nothing; the request that follows is gated.
    preflight = request.headers.get('x-original-method') == 'OPTIONS'
    own_kind, own = runtime.own_route(request.headers.get("x-original-host", "").lower(),
                                      request.headers.get("x-original-sandbox-id", ""),
                                      request.headers.get("x-original-sandbox-port") or "") or ("", None)
    if own is True:
        return Response(status_code=200, headers={RUNTIME: own_kind})
    header = request.headers.get("authorization", "")
    email = auth.token_email(header[7:].strip()) if header.lower().startswith("bearer ") else None
    # bearer: nginx strips the header, our token; otherwise it is the app's.
    kind = "bearer" if email else "cookie"
    email = email or auth.session_email(request)
    host = request.headers.get("x-original-host", "").lower()
    if host in addressing.service_hosts():   # the operator's services: a login, preflight or not
        if not email:
            raise HTTPException(401)
        return Response(status_code=200, headers={"X-Sbx-User": email, "X-Sbx-Auth": kind})
    if not email and not preflight:
        headers = {'X-Sbx-Login': ip_previews.login_url(int(public_port), request.headers.get('x-original-uri', '/'))} if ip_route else None
        raise HTTPException(401, headers=headers)
    if ip_route:
        slug, port = ip_route
        target = routing.slugs().get(slug)
        if target is None:
            raise HTTPException(404)
        sandbox_id = target[0]
    elif own:
        port, sandbox_id = own   # the runtime's own host routes by header
    else:
        found = addressing.raw_host(host) or routing.readable_host(host)
        if found is None:
            raise HTTPException(404)
        port, sandbox_id = found
    owner = sandbox_owner(sandbox_id)
    if owner is None or gone(sandbox_id):
        raise HTTPException(404)
    # Only what the app's sbx.yaml and the settings expose and, unless
    # detected_previews is off, what a server listens on on 0.0.0.0; also on
    # raw <port>-<id> hosts. A team-shared sandbox's owner-only previews (the
    # desktop) are the team's.
    a = _owners["app"][sandbox_id]
    preview = addressing.preview_on(a, port)
    if preview is None and not (preflight and not ip_route):   # a domain host's preflight: declared ones only
        preview = routing.detected_preview(sandbox_id, port, a, running=_owners["state"].get(sandbox_id) == "running")
    if preview is None:
        raise HTTPException(404)
    shared = core.shared(_owners["info"][sandbox_id].metadata)
    if not preflight and preview["access"] == "owner" and not auth.may_open_owner_preview(owner, shared, email):
        raise HTTPException(403)
    if not preflight:
        ensure_running(sandbox_id)
    # nginx sends the request to the sandbox's runtime under the host it names (upstream).
    headers = {"X-Sbx-User": email or '', **route(port, sandbox_id, preflight=preflight), "X-Sbx-Auth": kind}
    if ip_route:
        headers['X-Sbx-Cookies'] = ip_previews.app_cookies(request.headers.get('cookie', ''))
    return Response(status_code=200, headers=headers)
