"""The gate in front of every preview.

nginx on the Embed host (and on this host, for the operator's services) asks
GET /gate before every request to *.<domain>, with the original Host in
X-Original-Host. 200 lets it through, 401 sends a browser to the login, 403
refuses; nginx's auth_request passes on nothing else. Every 200 carries the
Cookie header the upstream gets (X-Sbx-Upstream-Cookie: the browser's
cookies without sbx's session, plus a cookie runtime's token). A 200 for a
sandbox names the Host nginx sends it to the runtime with (X-Sbx-Upstream)
and that runtime's kind (X-Sbx-Runtime), which nginx maps to the runtime's
ingress; for a runtime that answers a sandbox's ports only with a token
(E2B Cloud's header, Boat's per-port cookie), also the token
(X-Sbx-Upstream-Token), which nginx adds to that runtime's requests only.
nginx keeps /gate itself from the public (tls.sh, control.inc): only its own
subrequests reach it. Answers come from memory: a page load makes hundreds.

A preview link that is right from sbx's side always shows a page, never a
bare error: a signed-out visitor goes to the login for every host (so it
learns nothing of which sandboxes exist); for a signed-in one every state
the app can't answer in (starting, waking, the app stopped, failed, deleted,
no such name, no access, nothing on the port) is a 403 naming the page
(X-Sbx-Page), which nginx turns into /gate/page; an upstream's 502 or 504
goes there too (reason "unavailable"). A paused sandbox starts resuming on
its first request (waking), which waits briefly and otherwise answers with
the starting page; nothing blocks nginx while it resumes. The page polls
/gate/status (the preview host's /.sbx/status) until the app answers.

A decision has two halves: _resolve finds the sandbox and port a request
names and whether the visitor may open it; _answer says whether it can be
let through now or which page stands in the way."""
import dataclasses
import enum
import time
from typing import Mapping

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import (addressing, auth, core, ip_previews, lifecycle, preview_pages, preview_proxy, routing, runtime, settings,
               waking)
from .preview_pages import Page
from .runtime.listening import listening

router = APIRouter()
GONE = "X-Sbx-Gone"
UPSTREAM, RUNTIME, TOKEN, COOKIE = "X-Sbx-Upstream", "X-Sbx-Runtime", "X-Sbx-Upstream-Token", "X-Sbx-Upstream-Cookie"
PAGE, WAIT = "X-Sbx-Page", "X-Sbx-Wait"
LISTING_SECONDS = 20         # the longest the gate answers from one listing
STATUS_FRESH_SECONDS = 5     # the poll looks inside a sandbox only if a listing this recent says it runs
LISTEN_CACHE_SECONDS = 2


# What the gate knows of the sandboxes: one listing, replaced whole.


@dataclasses.dataclass(frozen=True)
class Listed:
    """A sandbox as the last listing had it."""
    info: object
    owner: str | None
    app: object
    state: str

    @property
    def name(self) -> str:
        return (self.info.metadata or {}).get("sbx", "")


@dataclasses.dataclass(frozen=True)
class Snapshot:
    fetched_at: float
    by_id: Mapping[str, Listed]


_snapshot = Snapshot(0.0, {})
_listening: dict = {}    # (sandbox id, port) -> (when, listens)


def listed(sandbox_id: str, fresh: bool = False) -> Listed | None:
    """The sandbox as a listing at most LISTING_SECONDS old has it (fresh:
    a new one); a sandbox missing from the last listing is asked for again."""
    global _snapshot
    found = _snapshot
    if fresh or sandbox_id not in found.by_id or time.time() - found.fetched_at > LISTING_SECONDS:
        infos = core.sandboxes()
        found = _snapshot = Snapshot(time.time(), {
            i.sandbox_id: Listed(i, (i.metadata or {}).get("owner"), core.app_of(i), core.runtime_state(i))
            for i in infos})
    return found.by_id.get(sandbox_id)


def sandbox_owner(sandbox_id: str, fresh=False):
    found = listed(sandbox_id, fresh)
    return found.owner if found else None


def ensure_running(sandbox_id: str):
    """Resume a paused sandbox and wait for it (what the gate did before the
    starting page; still the way for callers that must have it running)."""
    waking.ensure_running(sandbox_id, lambda fresh: listed(sandbox_id, fresh))


def route(port: int, sandbox_id: str, *, preflight: bool = False) -> dict:
    """Where nginx sends a let-through request for a sandbox's port: the
    Host its runtime routes on, the runtime's kind (nginx's map picks its
    ingress from it) and, for a runtime that wants one, the token for the
    sandbox's port. A preflight wakes nothing: it takes only a token the
    runtime kept, and without one it is refused like anything not routable
    (the browser's real request resumes the sandbox and fetches it). A
    sandbox the runtime no longer has is not found either (404)."""
    try:
        found = _snapshot.by_id.get(sandbox_id)
        kind = runtime.kind_of(found.info) if found is not None else runtime.of(sandbox_id).kind
        token = runtime.upstream_token(sandbox_id, port, kind, fetch=not preflight)
        headers = {UPSTREAM: addressing.upstream(port, sandbox_id), RUNTIME: kind}
    except runtime.NotRunning:
        raise HTTPException(404) from None
    except runtime.Failed as error:
        # The runtime did not hand over the token (it did not answer, or
        # answered without one): refused like anything not routable now,
        # never a 500. Its backend keeps the failure briefly, so an outage is
        # not a call to the provider for every request.
        print(f"gate: no upstream token for {sandbox_id}: {error}", flush=True)
        raise HTTPException(404) from None
    if token:
        if not token[1]:
            raise HTTPException(404)
        headers[TOKEN] = token[1]
    return headers


def gone(sandbox_id: str) -> bool:
    """Whether the listed VM's sandbox is being deleted, was deleted, or is
    another VM than the one its name is bound to now: the runtime lists a
    deleted VM for a while."""
    found = _snapshot.by_id.get(sandbox_id)
    name = found.name if found else None
    current = lifecycle.get(name, sandbox_id=sandbox_id) if name else None
    return bool(current) and (current["phase"] in ("deleting", "deleted") or not current["binding_matches"])


def _unlisted(host_label: str, port: int | None, email: str) -> Page:
    """A host no listed sandbox answers to: one being made, one that failed
    or was deleted, one whose port is not a preview, or no such name. Only
    whoever may view the sandbox (its owner, admins, the team for a shared
    one) learns which; anyone else gets what an unknown name gets."""
    from . import sandboxes
    unknown = Page("missing", host_label)
    rows = sandboxes.rows()
    by_slug = {row.get("slug") or name: name for name, row in rows.items()}
    cuts = [len(host_label)] + [i for i in range(len(host_label) - 1, 0, -1) if host_label[i] == "-"]
    for cut in cuts:
        slug, suffix = host_label[:cut], host_label[cut:]
        name = by_slug.get(slug) or (slug if slug in rows else None)
        if name is None:
            record = lifecycle.get(slug) if cut == len(host_label) else None
            if record and record["phase"] == "deleted":   # its registration went with it: admins only
                return Page("deleted", slug) if auth.is_admin(email) else unknown
            continue
        row = rows[name]
        if not auth.may_view(row.get("owner"), core.shared(row.get("metadata") or {}), email):
            return unknown
        page = preview_pages.page_for(name, lifecycle.get(name), row.get("owner") == email)
        if page is not None:
            return page
        if core.find(name) is None:
            return Page("failed", name, detail="Its VM is gone. Delete it, then create it again.")
        return Page("no_listener", name, port=core.suffix_port(suffix) if suffix else None)
    return unknown


def _listens(sandbox_id: str, port: int) -> bool:
    """Whether something in the sandbox listens on the port now (a few seconds' memory)."""
    when, found = _listening.get((sandbox_id, port), (0.0, False))
    if time.monotonic() - when < LISTEN_CACHE_SECONDS:
        return found
    try:
        found = listening(runtime.handle(sandbox_id), port, timeout=10) is not None
    except Exception as error:   # not answering now: not ready
        print(f"gate: could not look at port {port} in {sandbox_id}: {error!r}", flush=True)
        found = False
    _listening[(sandbox_id, port)] = (time.monotonic(), found)
    return found


# The decision


class Mode(enum.Enum):
    REQUEST = "request"       # a request nginx asks about; it may wake the sandbox
    PREFLIGHT = "preflight"   # a CORS preflight: no credentials, wakes nothing, declared previews only
    POLL = "poll"             # the page's poll: a sign-in whatever its method, wakes nothing


@dataclasses.dataclass(frozen=True)
class Target:
    """A sandbox's port a signed-in visitor may open."""
    sandbox_id: str
    port: int
    listed: Listed
    email: str
    auth: str                 # "bearer" (our token, which nginx strips) or "cookie"
    ip: bool                  # an IP preview's port

    @property
    def name(self) -> str:
        return self.listed.name

    @property
    def mine(self) -> bool:
        return self.listed.owner == self.email


@dataclasses.dataclass(frozen=True)
class Allow:
    headers: dict
    target: Target | None = None


@dataclasses.dataclass(frozen=True)
class Login:
    headers: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Refuse:
    """Nothing is there (a preflight's answer, a 404)."""


@dataclasses.dataclass(frozen=True)
class Show:
    page: Page


@dataclasses.dataclass(frozen=True)
class Desktop:
    """The provider's own desktop, which /gate/page redirects to."""
    sandbox_id: str


Outcome = Allow | Login | Refuse | Show | Desktop


def _mode(request: Request, status: bool) -> Mode:
    if status:
        return Mode.POLL
    return Mode.PREFLIGHT if request.headers.get("x-original-method") == "OPTIONS" else Mode.REQUEST


def _signed_in_as(request: Request) -> tuple[str | None, str]:
    """(the visitor's email, how it signed in): a bearer token of ours, else the session cookie."""
    header = request.headers.get("authorization", "")
    email = auth.token_email(header[7:].strip()) if header.lower().startswith("bearer ") else None
    return (email, "bearer") if email else (auth.session_email(request), "cookie")


def _cookie(request: Request, kind: str | None = None, token: str = "") -> dict:
    """The Cookie header the upstream gets: the browser's without sbx's
    session, with the token for a runtime that wants it in a cookie."""
    place = runtime.upstream_token_places().get(kind) if kind else None
    name = place.name if place and place.place == "cookie" else None
    return {COOKIE: preview_proxy.upstream_cookie(request.headers.get("cookie", ""), name, token)}


def _resolve(request: Request, mode: Mode) -> Target | Outcome:
    """The sandbox and port the request names and whether the visitor may open it."""
    original_host = request.headers.get("x-original-host", "").lower()
    public_port = request.headers.get('x-original-preview-port')
    preflight = mode is Mode.PREFLIGHT
    ip_route = None
    if public_port:
        if not public_port.isdecimal() or original_host.strip('[]') != ip_previews.address():
            return Refuse()
        ip_route = ip_previews.route(int(public_port))
    own_kind, own = runtime.own_route(original_host, request.headers.get("x-original-sandbox-id", ""),
                                      request.headers.get("x-original-sandbox-port") or "") or ("", None)
    if own is True:
        return Allow({RUNTIME: own_kind, **_cookie(request)})
    email, how = _signed_in_as(request)
    if original_host in addressing.service_hosts():   # the operator's services: a login, preflight or not
        if not email:
            return Login()
        return Allow({"X-Sbx-User": email, "X-Sbx-Auth": how, **_cookie(request)})
    if not email and not preflight:
        return Login({'X-Sbx-Login': ip_previews.login_url(int(public_port), request.headers.get('x-original-uri', '/'))}
                     if public_port and public_port.isdecimal() else {})

    def nothing(page) -> Outcome:
        """Nothing to let through: a preflight is refused, anything else gets the page (built only then)."""
        return Refuse() if preflight else Show(page())

    if public_port and ip_route is None:
        return nothing(lambda: Page("missing"))
    if ip_route:
        slug, port = ip_route
        found = routing.slugs().get(slug)
        if found is None:
            return nothing(lambda: _unlisted(slug, port, email))
        sandbox_id = found[0]
    elif own:
        port, sandbox_id = own   # the runtime's own host routes by header
    else:
        found = addressing.raw_host(original_host) or routing.readable_host(original_host)
        if found is None:
            return nothing(lambda: _unlisted(addressing.host_label(original_host), None, email))
        port, sandbox_id = found
    sandbox = listed(sandbox_id)
    if sandbox is None or sandbox.owner is None or gone(sandbox_id):
        name = sandbox.name if sandbox else ""
        return nothing(lambda: _unlisted(name or addressing.host_label(original_host), port, email))
    # Only what the app's sbx.yaml and the settings expose and, unless
    # detected_previews is off, what a server listens on on 0.0.0.0; also on
    # raw <port>-<host id> hosts. A team-shared sandbox's owner-only previews (the
    # desktop) are the team's.
    preview = addressing.preview_on(sandbox.app, port)
    if preview is None and not (preflight and not ip_route):   # a domain host's preflight: declared ones only
        preview = routing.detected_preview(sandbox_id, port, sandbox.app, running=sandbox.state == "running")
    if preview is None:
        return nothing(lambda: Page("no_listener", sandbox.name, port=port))
    if not preflight and preview["access"] == "owner" and \
            not auth.may_open_owner_preview(sandbox.owner, core.shared(sandbox.info.metadata), email):
        return Show(Page("forbidden", sandbox.name))
    return Target(sandbox_id, port, sandbox, email or "", how, ip_route is not None)


# Operations during which the VM is being made, going away or coming back:
# the SSH bridge waits on them like any preview.
_VM_OPERATIONS = ("create", "pause", "resume", "delete")


def _record_page(target: Target, record: dict | None) -> Page | None:
    """The page sbx's record of the sandbox calls for. The SSH bridge is
    the platform's, not the app's: it answers while sbx prepares or restarts
    the app (creating a sandbox returns once SSH works, and the developer's
    agent logins go in then) and after the app or its preparation failed,
    when SSH is how to look; only while its VM is made, pauses, resumes or
    is deleted does it wait like any preview."""
    if record is not None and target.port == addressing.ports(target.listed.app).get("-ssh") and \
            record["phase"] not in ("deleting", "deleted") and \
            not (record["active"] and record["operation"] in _VM_OPERATIONS):
        return None
    return preview_pages.page_for(target.name, record, target.mine)


def _waiting(target: Target, mode: Mode, request: Request) -> Outcome | None:
    """What keeps the request from the app now: an operation of sbx's, the
    runtime's error, a resume, the provider's desktop, a navigation before
    the app's port listens; None when nothing does."""
    sandbox_id, name, mine = target.sandbox_id, target.name, target.mine
    record = lifecycle.get(name, sandbox_id=sandbox_id) if name else None
    page = _record_page(target, record)   # an operation runs, or the last one failed
    if page is not None:
        return Show(page)
    sandbox = _snapshot.by_id.get(sandbox_id) or target.listed
    if sandbox.state == "running" and record and record.get("runtime") == "paused":
        # sbx paused it since the last listing: look again, so it is woken
        # here (the page) rather than by whatever connects to it next.
        sandbox = listed(sandbox_id, fresh=True) or sandbox
    if record and record["active"] and sandbox.state != "running" and record.get("runtime") == "running":
        # The restart resumed it since the last listing: look again.
        sandbox = listed(sandbox_id, fresh=True) or sandbox
    if sandbox.state == runtime.FAILED:
        return Show(preview_pages.failed(name, "Its runtime reports an error. Restart or delete it in sbx.", mine))
    if record and record["active"] and sandbox.state != "running":
        # The SSH bridge while sbx works on a paused VM (a restart resumes
        # it): wait for that, since a resume of our own would collide with it.
        return Show(Page("waking", name, step="Waking up"))
    could_not_wake = Show(preview_pages.failed(name, "It could not wake up. Restart it in sbx.", mine))
    if mode is Mode.POLL:
        sandbox = listed(sandbox_id, fresh=time.time() - _snapshot.fetched_at > STATUS_FRESH_SECONDS) or sandbox
        if waking.failed(sandbox_id):
            return could_not_wake
        if sandbox.state != "running":
            # Nothing resuming it (it paused again since): the page reloads, and that navigation wakes it.
            return Show(Page("waking", name, step="Waking up", reload=not waking.busy(sandbox_id)))
    elif not waking.wake(sandbox_id, lambda fresh: listed(sandbox_id, fresh)):
        return could_not_wake if waking.failed(sandbox_id) else Show(Page("waking", name, step="Waking up"))
    record = lifecycle.get(name, sandbox_id=sandbox_id) if name else None
    page = _record_page(target, record)   # the resume may have started a restart
    if page is not None:
        return Show(page)
    if target.port == _desktop_port(target.listed.app) and runtime.desktop(runtime.kind_of(target.listed.info)) == "provider":
        return Desktop(sandbox_id)
    # A browser's navigation waits on the page until the app's port
    # listens (the runtime's own "nothing there" is not shown); other
    # requests go through and get whatever answers.
    if mode is Mode.REQUEST and preview_pages.wants_html(request.headers.get("x-original-method", "GET"),
                                                         request.headers.get("accept", ""),
                                                         request.headers.get("x-original-upgrade", "")) \
            and not _listens(sandbox_id, target.port):
        return Show(_not_listening(target))
    return None


def _answer(target: Target, mode: Mode, request: Request) -> Outcome:
    """Let the request through to the sandbox's runtime, or the page that stands in the way."""
    if mode is not Mode.PREFLIGHT:
        found = _waiting(target, mode, request)
        if found is not None:
            return found
    # nginx sends the request to the sandbox's runtime under the host it names (upstream).
    try:
        found = route(target.port, target.sandbox_id, preflight=mode is Mode.PREFLIGHT)
    except HTTPException:
        if mode is Mode.PREFLIGHT:
            return Refuse()
        return Show(Page("starting", target.name, step="Starting the app", port=target.port))
    return Allow({"X-Sbx-User": target.email, **found, "X-Sbx-Auth": target.auth,
                  **_cookie(request, found[RUNTIME], found.get(TOKEN, ""))}, target)


def _decide(request: Request, *, status: bool = False) -> Outcome:
    mode = _mode(request, status)
    found = _resolve(request, mode)
    return _answer(found, mode, request) if isinstance(found, Target) else found


def _not_listening(target: Target) -> Page:
    """The app's main port: it is starting; any other: nothing listens there."""
    if target.port == addressing.ports(target.listed.app).get(""):
        return Page("starting", target.name, step="Starting the app", port=target.port)
    return Page("no_listener", target.name, port=target.port)


def _desktop_port(a) -> int | None:
    """The desktop preview's port (its -desktop suffix), if the app has one."""
    found = addressing.previews(a).get("-desktop")
    return found["port"] if found else None


@router.get("/gate")
def gate(request: Request):
    """nginx's auth_request passes on 2xx, 401 and 403 only and turns anything
    else into a 500: a page is a 403 naming it (X-Sbx-Page), which nginx
    turns into /gate/page; "nothing there" is a 403 marked GONE."""
    match _decide(request):
        case Allow(headers=headers):
            return Response(status_code=200, headers=headers)
        case Login(headers=headers):
            raise HTTPException(401, headers=headers or None)
        case Refuse():
            raise HTTPException(403, headers={GONE: "1"})
        case Show(page=page):
            # A page to wait on is never cached: the next request may find it running.
            raise HTTPException(403, headers={PAGE: page.state, **({WAIT: "1"} if page.waiting else {})})
        case Desktop():
            raise HTTPException(403, headers={PAGE: "desktop", WAIT: "1"})


def _signed_in(request: Request) -> bool:
    return bool(_signed_in_as(request)[0])


def _ready_or_page(found: Allow) -> Page:
    """For a request the gate lets through: Ready when the app's port
    listens, else what keeps it (the app starting, nothing on the port)."""
    target = found.target
    if target is None:
        return Page("ready")
    if _listens(target.sandbox_id, target.port):
        return Page("ready", target.name)
    return _not_listening(target)


def _login() -> str:
    """The sign-in page a poll that lost its session goes to."""
    return f"{settings.current().base_url}/login"


def _home() -> str:
    """The web app's sandboxes page, on the login host."""
    return f"{settings.current().base_url}/app/sandboxes"


def _shown(page: Page, request: Request) -> Page:
    """Names only for a signed-in visitor; anyone else sees the neutral page."""
    return page if _signed_in(request) else page.neutral()


@router.get("/gate/page")
def gate_page(request: Request):
    """The page nginx shows instead of a refused request or an upstream's 502
    or 504 (X-Sbx-Reason: denied or unavailable): HTML for a browser's
    navigation, the status with Retry-After for anything else."""
    reason = "unavailable" if request.headers.get("x-sbx-reason") == "unavailable" else "denied"
    match _decide(request):
        case Login(headers=headers):
            return Response(status_code=401, headers=headers)
        case Desktop(sandbox_id=sandbox_id):
            try:
                return RedirectResponse(runtime.desktop_url(sandbox_id), status_code=302,
                                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            except runtime.Failed:
                page = Page("starting", step="Opening the desktop")
        case Refuse():
            page = Page("missing")
        case Allow() as found:
            page = _ready_or_page(found)
            if page.state == "ready" and reason == "unavailable":   # it answers now; an upstream error is the app's own
                page = Page("starting", page.name, step="Starting the app",
                            port=found.target.port if found.target else None)   # none: a service's, a runtime's own
        case Show(page=page):
            pass
    page = _shown(page, request)
    method = request.headers.get("x-original-method", "GET")
    if page.state == "ready":
        if preview_pages.wants_html(method, request.headers.get("accept", "")):
            return HTMLResponse(preview_pages.render(Page("starting", page.name, step="Opening the app"),
                                                     reason=reason, login=_login(), home=_home()), status_code=503,
                                headers=preview_pages.headers(Page("starting")))
        return JSONResponse({"state": "ready"}, status_code=503, headers=preview_pages.headers(Page("starting")))
    if preview_pages.wants_html(method, request.headers.get("accept", ""), request.headers.get("x-original-upgrade", "")):
        return HTMLResponse(preview_pages.render(page, reason=reason, login=_login(), home=_home()),
                            status_code=page.status,
                            headers=preview_pages.headers(page))
    return Response(preview_pages.plain(page), status_code=page.status, media_type="application/json",
                    headers=preview_pages.headers(page))


@router.get("/gate/status")
def gate_status(request: Request):
    """The page's poll (the preview host's /.sbx/status): where the
    sandbox and its app are now, "ready" once the app's port answers. A
    sign-in for every method (no preflight here), nothing woken; whoever
    may not open the preview gets what an unknown name gets."""
    match _decide(request, status=True):
        case Login():
            return JSONResponse({"state": "login"}, status_code=401, headers={"Cache-Control": "no-store"})
        case Desktop():
            page = Page("ready")
        case Refuse():
            page = Page("missing")
        case Show(page=page) if page.state == "forbidden":
            page = Page("missing")
        case Show(page=page):
            pass
        case Allow() as found:
            page = _ready_or_page(found)
    return JSONResponse(_shown(page, request).view(), headers={"Cache-Control": "no-store"})
