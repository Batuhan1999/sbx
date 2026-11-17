"""The gate in front of every preview.

nginx on the Embed host (and on this host, for the operator's services) asks
GET /gate before every request to *.<domain>, with the original Host in
X-Original-Host. 200 lets it through, 401 sends a browser to the login, 403
refuses; nginx's auth_request passes on nothing else. A 200 for a sandbox
names the Host nginx sends it to the runtime with (X-Sbx-Upstream) and that
runtime's kind (X-Sbx-Runtime), which nginx maps to the runtime's ingress;
for a runtime that answers a sandbox's ports only with a token (E2B Cloud's
header, Boat's per-port cookie), also the token (X-Sbx-Upstream-Token),
which nginx adds to that runtime's requests only. nginx keeps /gate itself
from the public (tls.sh, control.inc): only its own subrequests reach it.
Answers come from memory: a page load makes hundreds.

A preview link that is right from sbx's side always shows a page, never a
bare error: a signed-out visitor goes to the login for every host (so it
learns nothing of which sandboxes exist); for a signed-in one every state
the app can't answer in (starting, waking, the app stopped, failed, deleted,
no such name, no access, nothing on the port) is a 403 naming the page
(X-Sbx-Page), which nginx turns into /gate/page; an upstream's 502 or 504
goes there too (reason "unavailable"). A paused sandbox starts resuming on
its first request, which waits briefly and otherwise answers with the
starting page; nothing blocks nginx while it resumes. The page polls
/gate/status (the preview host's /.sbx/status) until the app answers."""
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import addressing, auth, core, ip_previews, lifecycle, preview_pages, routing, runtime, settings
from .preview_pages import Page

router = APIRouter()
GONE = "X-Sbx-Gone"
UPSTREAM, RUNTIME, TOKEN, COOKIE = "X-Sbx-Upstream", "X-Sbx-Runtime", "X-Sbx-Upstream-Token", "X-Sbx-Upstream-Cookie"
PAGE, WAIT = "X-Sbx-Page", "X-Sbx-Wait"
WAKE_WAIT_SECONDS = 2.5      # how long a request waits for a resume before the starting page answers
STATUS_FRESH_SECONDS = 5     # the poll looks inside a sandbox only if a listing this recent says it runs
LISTEN_CACHE_SECONDS = 2

_owners: dict = {"fetched_at": 0.0, "by_id": {}, "state": {}, "app": {}, "info": {}}
_last_access: dict = {}  # sandbox id -> time the gate last let a request through
_resume_locks: dict = {}
_waking: dict = {}       # sandbox id -> the thread resuming it for a preview
_wake_errors: dict = {}  # sandbox id -> why the last resume for a preview failed
_listening: dict = {}    # (sandbox id, port) -> (when, listens)
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


def _resume(sandbox_id: str):
    """Resume a paused sandbox for a preview: one at a time per sandbox."""
    lock = _resume_locks.setdefault(sandbox_id, threading.Lock())
    with lock:
        sandbox_owner(sandbox_id, fresh=True)
        if _owners["state"].get(sandbox_id) == "paused":
            info = _owners["info"][sandbox_id]
            core.attach((info.metadata or {})["sbx"], "for a preview request", info=info)
            _owners["state"][sandbox_id] = "running"
            _resumed_for_preview.add(sandbox_id)


def _resume_in_background(sandbox_id: str):
    try:
        _resume(sandbox_id)
        _wake_errors.pop(sandbox_id, None)
    except Exception as error:
        _wake_errors[sandbox_id] = f"{type(error).__name__}: {error}"
        print(f"gate: resuming {sandbox_id} for a preview failed: {error!r}", flush=True)


def wake(sandbox_id: str, wait: float = WAKE_WAIT_SECONDS) -> bool:
    """Start resuming a paused sandbox (once; Embed's client-proxy does not
    resume on traffic) and wait up to wait seconds: True when it runs now,
    False while it is still waking (the starting page answers meanwhile)."""
    idle_for = time.time() - _last_access.get(sandbox_id, 0)
    _last_access[sandbox_id] = time.time()
    if idle_for > 60:
        sandbox_owner(sandbox_id, fresh=True)  # it may have paused since the last look
    thread = _waking.get(sandbox_id)
    if _owners["state"].get(sandbox_id) != "paused" and not (thread and thread.is_alive()):
        return True
    if thread is None or not thread.is_alive():
        _wake_errors.pop(sandbox_id, None)
        thread = _waking[sandbox_id] = threading.Thread(target=_resume_in_background, args=(sandbox_id,),
                                                        daemon=True, name=f"wake-{sandbox_id}")
        thread.start()
    thread.join(wait)
    return not thread.is_alive() and sandbox_id not in _wake_errors


def ensure_running(sandbox_id: str):
    """Resume a paused sandbox and wait for it (what the gate did before the
    starting page; still the way for callers that must have it running)."""
    while not wake(sandbox_id, wait=60):
        if sandbox_id in _wake_errors:
            raise runtime.Failed(_wake_errors[sandbox_id])


def route(port: int, sandbox_id: str, *, preflight: bool = False, cookies: str | None = None) -> dict:
    """Where nginx sends a let-through request for a sandbox's port: the
    Host its runtime routes on, the runtime's kind (nginx's map picks its
    ingress from it) and, for a runtime that wants one, the token for the
    sandbox's port. A preflight wakes nothing: it takes only a token the
    runtime kept, and without one it is refused like anything not routable
    (the browser's real request resumes the sandbox and fetches it). A
    sandbox the runtime no longer has is not found either (404). For a
    runtime that wants the token in a cookie, X-Sbx-Upstream-Cookie is the
    whole Cookie header nginx sends it: the browser's cookies (cookies)
    without sbx's session and without any of that name, then the token."""
    try:
        info = _owners["info"].get(sandbox_id)
        kind = runtime.kind_of(info) if info is not None else runtime.of(sandbox_id).kind
        token = runtime.upstream_token(sandbox_id, port, kind, fetch=not preflight)
        found = {UPSTREAM: addressing.upstream(port, sandbox_id), RUNTIME: kind}
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
        found[TOKEN] = token[1]
        cookie = runtime.upstream_token_cookies().get(kind)
        if cookie:
            kept = [part.strip() for part in ip_previews.app_cookies(cookies or "").split(";")
                    if part.strip() and part.partition("=")[0].strip() != cookie]
            found[COOKIE] = "; ".join([*kept, f"{cookie}={token[1]}"])
    return found


def gone(sandbox_id: str) -> bool:
    """Whether the listed VM's sandbox is being deleted, was deleted, or is
    another VM than the one its name is bound to now: the runtime lists a
    deleted VM for a while."""
    info = _owners["info"].get(sandbox_id)
    name = (info.metadata or {}).get("sbx") if info else None
    current = lifecycle.get(name, sandbox_id=sandbox_id) if name else None
    return bool(current) and (current["phase"] in ("deleting", "deleted") or not current["binding_matches"])


# What a sandbox is doing, in a few plain words

STEPS = {"allocating": "Creating the sandbox", "provisioning_services": "Starting its services",
         "preparing_workspace": "Preparing the workspace", "workspace_ready": "Starting the app",
         "services_ready": "Starting the app", "setting_up": "Running setup", "starting_app": "Starting the app",
         "restoring_runtime": "Waking up", "pausing": "Pausing"}
FIRST_STEPS = {"create": "Creating the sandbox", "resume": "Waking up", "restart": "Restarting the app",
               "prepare": "Preparing the workspace", "env_update": "Updating its environment", "pause": "Pausing"}


def step_of(record: dict) -> str:
    if record["phase"] == "queued":
        return FIRST_STEPS.get(record["operation"], "Starting")
    return STEPS.get(record["phase"], "Starting")


def _what_failed(record: dict) -> str:
    """The failed operation's step, as the page's line: what happened and what to do."""
    error = record.get("error") or ""
    during = error.partition(" during ")[2].partition(" ")[0]
    if error.startswith("the app stopped"):
        return "Its app stopped. Restart it in sbx."
    if during in STEPS:
        return f"It failed while {STEPS[during][0].lower()}{STEPS[during][1:].lower()}. Restart or delete it in sbx."
    if record.get("phase") == "interrupted":
        return "sbx restarted while it was busy. Restart or delete it in sbx."
    return "Restart or delete it in sbx."


def _open_in_sbx(name: str, label: str = "Open in sbx") -> tuple:
    return (label, f"{settings.current().base_url}/sandboxes/{name}")


def _record_page(name: str, record: dict | None, owner_email: str | None, email: str) -> Page | None:
    """The page sbx's own record of the sandbox calls for; None when it says nothing stands in the way."""
    if record is None:
        return None
    mine = owner_email == email
    if record["phase"] in ("deleting", "deleted"):
        return Page("deleted", name)
    if record["active"]:
        return Page("waking" if record["operation"] in ("resume", "pause") else "starting", name, step=step_of(record))
    if record["phase"] in ("failed", "interrupted"):
        return Page("failed", name, detail=_what_failed(record), action=_open_in_sbx(name) if mine else ())
    if record.get("application") == "failed":
        return Page("app_stopped", name, action=_open_in_sbx(name, "Restart") if mine else ())
    return None


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
        page = _record_page(name, lifecycle.get(name), row.get("owner"), email)
        if page is not None:
            return page
        found = core.find(name)
        if found is None:
            return Page("failed", name, detail="Its VM is gone. Delete it, then create it again.")
        number = core.suffix_port(suffix) if suffix else None
        return Page("no_listener", name, port=number)
    return unknown


def _listens(sandbox_id: str, port: int) -> bool:
    """Whether something in the sandbox listens on the port now (a few seconds' memory)."""
    when, found = _listening.get((sandbox_id, port), (0.0, False))
    if time.monotonic() - when < LISTEN_CACHE_SECONDS:
        return found
    try:
        out = runtime.connect(sandbox_id).commands.run(f"ss -Hltn 'sport = :{int(port)}'", timeout=10).stdout
        found = bool(out.strip())
    except Exception as error:   # not answering now: not ready
        print(f"gate: could not look at port {port} in {sandbox_id}: {error!r}", flush=True)
        found = False
    _listening[(sandbox_id, port)] = (time.monotonic(), found)
    return found


# The decision


class Outcome:
    """What the gate decides for a request: allow (headers for nginx),
    login, a page, or refuse (nothing is there: a preflight, a 404)."""

    def __init__(self, kind: str, headers: dict | None = None, page: Page | None = None, target=None):
        self.kind, self.headers, self.page, self.target = kind, headers or {}, page, target


def _decide(request: Request, *, status: bool = False) -> Outcome:
    """status: for the page's poll, which needs a sign-in whatever its
    method, never wakes a sandbox and connects only to one listed running
    moments ago."""
    original_host = request.headers.get("x-original-host", "").lower()
    public_port = request.headers.get('x-original-preview-port')
    ip_route = None
    if public_port:
        if not public_port.isdecimal() or original_host.strip('[]') != ip_previews.address():
            return Outcome("refuse")
        ip_route = ip_previews.route(int(public_port))
    # A CORS preflight carries no credentials: let it through to a declared
    # preview without a login, waking nothing; the request that follows is gated.
    preflight = not status and request.headers.get('x-original-method') == 'OPTIONS'
    own_kind, own = runtime.own_route(original_host, request.headers.get("x-original-sandbox-id", ""),
                                      request.headers.get("x-original-sandbox-port") or "") or ("", None)
    if own is True:
        return Outcome("allow", {RUNTIME: own_kind})
    header = request.headers.get("authorization", "")
    email = auth.token_email(header[7:].strip()) if header.lower().startswith("bearer ") else None
    # bearer: nginx strips the header, our token; otherwise it is the app's.
    kind = "bearer" if email else "cookie"
    email = email or auth.session_email(request)
    if original_host in addressing.service_hosts():   # the operator's services: a login, preflight or not
        if not email:
            return Outcome("login")
        return Outcome("allow", {"X-Sbx-User": email, "X-Sbx-Auth": kind})
    if not email and not preflight:
        headers = ({'X-Sbx-Login': ip_previews.login_url(int(public_port), request.headers.get('x-original-uri', '/'))}
                   if public_port and public_port.isdecimal() else {})
        return Outcome("login", headers)
    if public_port and ip_route is None:
        return Outcome("refuse") if preflight else Outcome("page", page=Page("missing"))
    if ip_route:
        slug, port = ip_route
        target = routing.slugs().get(slug)
        if target is None:
            return Outcome("refuse") if preflight else Outcome("page", page=_unlisted(slug, port, email))
        sandbox_id = target[0]
    elif own:
        port, sandbox_id = own   # the runtime's own host routes by header
    else:
        found = addressing.raw_host(original_host) or routing.readable_host(original_host)
        if found is None:
            if preflight:
                return Outcome("refuse")
            label = addressing.host_label(original_host)
            return Outcome("page", page=_unlisted(label, None, email))
        port, sandbox_id = found
    owner = sandbox_owner(sandbox_id)
    info = _owners["info"].get(sandbox_id)
    name = (info.metadata or {}).get("sbx", "") if info else ""
    if owner is None or gone(sandbox_id):
        return Outcome("refuse") if preflight else Outcome("page", page=_unlisted(
            name or addressing.host_label(original_host), port, email))
    # Only what the app's sbx.yaml and the settings expose and, unless
    # detected_previews is off, what a server listens on on 0.0.0.0; also on
    # raw <port>-<id> hosts. A team-shared sandbox's owner-only previews (the
    # desktop) are the team's.
    a = _owners["app"][sandbox_id]
    preview = addressing.preview_on(a, port)
    if preview is None and not (preflight and not ip_route):   # a domain host's preflight: declared ones only
        preview = routing.detected_preview(sandbox_id, port, a, running=_owners["state"].get(sandbox_id) == "running")
    if preview is None:
        return Outcome("refuse") if preflight else Outcome("page", page=Page("no_listener", name, port=port))
    shared = core.shared(info.metadata)
    if not preflight and preview["access"] == "owner" and not auth.may_open_owner_preview(owner, shared, email):
        return Outcome("page", page=Page("forbidden", name))
    if not preflight:
        record = lifecycle.get(name, sandbox_id=sandbox_id) if name else None
        page = _record_page(name, record, owner, email)   # an operation runs, or the last one failed
        if page is not None:
            return Outcome("page", page=page)
        state = _owners["state"].get(sandbox_id)
        if state == runtime.FAILED:
            return Outcome("page", page=Page("failed", name, detail="Its runtime reports an error. Restart or "
                                             "delete it in sbx.", action=_open_in_sbx(name) if owner == email else ()))
        if status:
            sandbox_owner(sandbox_id, fresh=time.time() - _owners["fetched_at"] > STATUS_FRESH_SECONDS)
            state = _owners["state"].get(sandbox_id)
        if not status and not wake(sandbox_id):
            if sandbox_id in _wake_errors:
                return Outcome("page", page=Page("failed", name, detail="It could not wake up. Restart it in sbx.",
                                                 action=_open_in_sbx(name) if owner == email else ()))
            return Outcome("page", page=Page("waking", name, step="Waking up"))
        if status and sandbox_id in _wake_errors:
            return Outcome("page", page=Page("failed", name, detail="It could not wake up. Restart it in sbx.",
                                             action=_open_in_sbx(name) if owner == email else ()))
        if status and state != "running":
            waking = _waking.get(sandbox_id)
            # Nothing resuming it (it paused again since): the page reloads, and that navigation wakes it.
            return Outcome("page", page=Page("waking", name, step="Waking up",
                                             reload=not (waking and waking.is_alive())))
        record = lifecycle.get(name, sandbox_id=sandbox_id) if name else None
        page = _record_page(name, record, owner, email)   # the resume may have started a restart
        if page is not None:
            return Outcome("page", page=page)
        if port == _desktop_port(a) and runtime.desktop(runtime.kind_of(info)) == "provider":
            return Outcome("desktop", target=sandbox_id)
        # A browser's navigation waits on the page until the app's port
        # listens (the runtime's own "nothing there" is not shown); other
        # requests go through and get whatever answers.
        if not status and preview_pages.wants_html(request.headers.get("x-original-method", "GET"),
                                                   request.headers.get("accept", ""),
                                                   request.headers.get("x-original-upgrade", "")) \
                and not _listens(sandbox_id, port):
            return Outcome("page", page=_not_listening(sandbox_id, port, name))
    # nginx sends the request to the sandbox's runtime under the host it names (upstream).
    try:
        headers = {"X-Sbx-User": email or '', **route(port, sandbox_id, preflight=preflight,
                                                      cookies=request.headers.get("cookie", "")),
                   "X-Sbx-Auth": kind}
    except HTTPException:
        if preflight:
            return Outcome("refuse")
        return Outcome("page", page=Page("starting", name, step="Starting the app", port=port))
    if ip_route:
        headers['X-Sbx-Cookies'] = ip_previews.app_cookies(request.headers.get('cookie', ''))
    return Outcome("allow", headers, target=(sandbox_id, port, name, owner, email))


def _not_listening(sandbox_id: str, port: int, name: str) -> Page:
    """The app's main port: it is starting; any other: nothing listens there."""
    app_port = addressing.ports(_owners["app"][sandbox_id]).get("") if sandbox_id in _owners["app"] else None
    if port == app_port:
        return Page("starting", name, step="Starting the app", port=port)
    return Page("no_listener", name, port=port)


def _desktop_port(a) -> int | None:
    """The desktop preview's port (its -desktop suffix), if the app has one."""
    found = addressing.previews(a).get("-desktop")
    return found["port"] if found else None


@router.get("/gate")
def gate(request: Request):
    """nginx's auth_request passes on 2xx, 401 and 403 only and turns anything
    else into a 500: a page is a 403 naming it (X-Sbx-Page), which nginx
    turns into /gate/page; "nothing there" is a 403 marked GONE."""
    found = _decide(request)
    if found.kind == "allow":
        return Response(status_code=200, headers=found.headers)
    if found.kind == "login":
        raise HTTPException(401, headers=found.headers or None)
    if found.kind == "refuse":
        raise HTTPException(403, headers={GONE: "1"})
    state = found.page.state if found.page else "desktop"
    headers = {PAGE: state}
    if found.page is None or found.page.waiting:
        headers[WAIT] = "1"   # never cached: the next request may find it running
    raise HTTPException(403, headers=headers)


def _signed_in(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    return bool((header.lower().startswith("bearer ") and auth.token_email(header[7:].strip()))
                or auth.session_email(request))


def _ready_or_page(found: Outcome, request: Request) -> Page:
    """For a request the gate lets through: Ready when the app's port
    listens, else what keeps it (the app starting, nothing on the port)."""
    if found.kind != "allow" or found.target is None:
        return found.page or Page("ready")
    sandbox_id, port, name, owner, email = found.target
    if _listens(sandbox_id, port):
        return Page("ready", name)
    return _not_listening(sandbox_id, port, name)


def _login() -> str:
    """The sign-in page a poll that lost its session goes to."""
    return f"{settings.current().base_url}/auth/login"


def _shown(page: Page, request: Request) -> Page:
    """Names only for a signed-in visitor; anyone else sees the neutral page."""
    return page if _signed_in(request) else page.neutral()


@router.get("/gate/page")
def gate_page(request: Request):
    """The page nginx shows instead of a refused request or an upstream's 502
    or 504 (X-Sbx-Reason: denied or unavailable): HTML for a browser's
    navigation, the status with Retry-After for anything else."""
    reason = "unavailable" if request.headers.get("x-sbx-reason") == "unavailable" else "denied"
    found = _decide(request)
    if found.kind == "login":
        return Response(status_code=401, headers=found.headers)
    if found.kind == "desktop":
        try:
            return RedirectResponse(runtime.desktop_url(found.target), status_code=302,
                                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        except runtime.Failed:
            page = Page("starting", step="Opening the desktop")
    elif found.kind == "refuse":
        page = Page("missing")
    elif found.kind == "allow":
        page = _ready_or_page(found, request)
        if page.state == "ready":   # it answers now; an upstream error is the app's own
            page = Page("starting", page.name, step="Starting the app", port=found.target[1]) \
                if reason == "unavailable" else page
    else:
        page = found.page
    page = _shown(page, request)
    method = request.headers.get("x-original-method", "GET")
    if page.state == "ready":
        if preview_pages.wants_html(method, request.headers.get("accept", "")):
            return HTMLResponse(preview_pages.render(Page("starting", page.name, step="Opening the app"),
                                                     reason=reason, login=_login()), status_code=503,
                                headers=preview_pages.headers(Page("starting")))
        return JSONResponse({"state": "ready"}, status_code=503, headers=preview_pages.headers(Page("starting")))
    if preview_pages.wants_html(method, request.headers.get("accept", ""), request.headers.get("x-original-upgrade", "")):
        return HTMLResponse(preview_pages.render(page, reason=reason, login=_login()), status_code=page.status,
                            headers=preview_pages.headers(page))
    return Response(preview_pages.plain(page), status_code=page.status, media_type="application/json",
                    headers=preview_pages.headers(page))


@router.get("/gate/status")
def gate_status(request: Request):
    """The page's poll (the preview host's /.sbx/status): where the
    sandbox and its app are now, "ready" once the app's port answers. A
    sign-in for every method (no preflight here), nothing woken; whoever
    may not open the preview gets what an unknown name gets."""
    found = _decide(request, status=True)
    if found.kind == "login":
        return JSONResponse({"state": "login"}, status_code=401, headers={"Cache-Control": "no-store"})
    if found.kind == "desktop":
        page = Page("ready")
    elif found.kind == "refuse" or (found.page and found.page.state == "forbidden"):
        page = Page("missing")
    else:
        page = _ready_or_page(found, request)
    page = _shown(page, request)
    return JSONResponse(page.view(), headers={"Cache-Control": "no-store"})
