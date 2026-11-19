"""/api/setup: the onboarding page's state and completion. Before anybody can
sign in, one thing is open to whoever reaches the server first: making the
first admin's account (POST /api/setup/admin). Every other step, here and on
/api/connections, /api/runtimes and /api/templates, is an admin's, as
everywhere after the onboarding."""
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, SecretStr

from . import auth, settings, setup, template_scheduler, users
from .connections import sign_in

router = APIRouter()
_setup_lock = threading.Lock()

# Until the onboarding is complete (setup.py), this host serves the
# onboarding and nothing else.
SETUP_OPEN = ("/onboarding", "/app", "/assets/", "/api/setup", "/api/connections", "/api/settings", "/api/catalog",
              "/api/runtimes", "/api/secret-requests", "/keys/", "/connections/github", "/auth/", "/cli/")
# Open by exact path: the release string, which the setup's reachability
# check fetches from inside a sandbox (reachability.PATH); what a template
# build downloads under its one-time token (/bundles: templates build during
# the onboarding, the base's Amp tools among them); the sign-in page
# and sign-out; the desktop CLI's installer and `sbx login` (/cli/: a session
# or a one-time code), so an agent signs in as the admin once the account
# exists and does the rest itself; /api/me answers only a sign-in;
# /api/templates (admins) says how the first builds stand.
SETUP_OPEN_EXACT = ("/dist/version", "/bundles", "/login", "/logout", "/install", "/dist/sbx.tgz", "/dist/sbx.tgz.sha256", "/api/me",
                    "/api/templates")
PAGE = "/onboarding"


async def setup_gate(request: Request, call_next):
    """Middleware: only the onboarding until it is complete, and never again after."""
    path = request.url.path
    if path == "/gate":
        return PlainTextResponse("setting up", status_code=403) if not setup.complete() else await call_next(request)
    if not setup.complete() and path in ("/", "/app", "/app/"):
        return RedirectResponse(PAGE)
    if setup.complete() and path == PAGE:
        return RedirectResponse("/app/repos")
    if not setup.complete() and path not in SETUP_OPEN_EXACT and not any(path == p or path.startswith(p)
                                                                         for p in SETUP_OPEN):
        said = f"This installation is being set up. Finish at {PAGE}."
        # The API's answer is JSON like any other: the web app tells a 503 in text from the proxy's own.
        return JSONResponse({"detail": said}, status_code=503) if path.startswith("/api/") else PlainTextResponse(said, status_code=503)
    return await call_next(request)


def admin_or_first(request: Request) -> str:
    """Who may change the installation: an admin; while the onboarding is
    open and no admin is named yet, whoever signed in (with single sign-on
    the first to sign in becomes the admin)."""
    try:
        email = auth.api_user(request)
    except HTTPException:
        if not setup.complete():
            raise HTTPException(401, "sign in as an admin (the onboarding page's first step makes the first one)")
        raise
    if auth.is_admin(email) or (not setup.complete() and not settings.current().admins):
        return email
    raise HTTPException(403, f"{email} is not an admin of this installation")


def _email(who: str) -> str | None:
    """Who is signed in, for the state; nobody on an installation without sign-in."""
    return None if who == auth.ANONYMOUS else who


@router.get("/api/setup/open")
def api_setup_open(request: Request):
    """Open to anyone: {complete, account, signed_in, link, version}. account: someone can sign in already,
    else the page's first step makes the first admin; signed_in: this request is an admin's."""
    try:
        admin_or_first(request)
        signed_in = True
    except HTTPException:
        signed_in = False
    return setup.open_state(signed_in, request.url.hostname or "")


@router.get("/api/setup")
def api_setup(request: Request, who: str = Depends(admin_or_first)):
    return setup.state(_email(who), request.url.hostname or "")


@router.get("/api/setup/checks")
def api_setup_checks(who: str = Depends(admin_or_first)):
    return setup.readiness()


class Address(BaseModel):
    address: str


@router.post("/api/setup/address")
def api_setup_address(body: Address, request: Request, who: str = Depends(admin_or_first)):
    if setup.complete():
        raise HTTPException(409, "Server setup is complete. Manage its address in Settings.")
    address = setup.ip_address(body.address)
    if not address:
        raise HTTPException(400, "Use this server's IP address.")
    s = settings.current()
    if s.domain.name:
        raise HTTPException(409, "A domain is already configured. Finish there, or change it in Settings.")
    settings.update("domain", {**s.domain.model_dump(), "address": address}, by=who)
    return setup.state(_email(who), address)


class Admin(BaseModel):
    email: str
    password: SecretStr
    # A client without a browser (sbx setup admin): also an API token for this admin, shown once.
    token: bool = False


@router.post("/api/setup/admin")
def api_setup_admin(body: Admin, request: Request):
    """The first admin's login, open to whoever asks while nobody can sign
    in: a password for email, who joins the admins (the installer may have
    named some already, with no way to sign in yet). Once someone can sign
    in it is closed for good; more people are added by an admin."""
    with _setup_lock:
        s = settings.current()
        if setup.complete() or sign_in.ready():
            raise HTTPException(409, "This server already has an admin. Sign in to go on.")
        try:
            email = users.valid_email(body.email)
            generation = users.set_password(email, body.password.get_secret_value(), by=f"setup ({email})")
        except users.Refused as error:
            raise HTTPException(400, str(error))
        admins = list(dict.fromkeys([*(a.lower() for a in s.access.admins), email]))
        settings.update("access", {**s.access.model_dump(), "admins": admins}, by=f"setup ({email})")
        settings.update("sign_in", {**s.sign_in.model_dump(), "method": "password"}, by=f"setup ({email})")
        auth.start_session(request, email, generation)
        found = setup.state(email, request.url.hostname or "")
        return {**found, "token": auth.mint_token(email, "setup")} if body.token else found


@router.post("/api/setup/handoff")
def api_setup_handoff(who: str = Depends(admin_or_first)):
    """{code}: signs this admin in once more within two minutes (POST /api/setup/enter), for the page's
    move from the server's IP address to its domain."""
    if not _email(who):
        raise HTTPException(400, "Nobody signs in on this installation.")
    return {"code": setup.handoff(who)}


class Enter(BaseModel):
    code: str


@router.post("/api/setup/enter")
def api_setup_enter(body: Enter, request: Request):
    """Sign in with a handoff's code, at the address this is asked on. 403 for a code that is used, replaced or
    older than two minutes: the page then asks to sign in."""
    email = setup.enter(body.code)
    if not email or not auth.may_sign_in(email) or not auth.is_admin(email):
        raise HTTPException(403, "That sign-in has expired. Sign in to go on.")
    auth.start_session(request, email, users.generation(email))
    return setup.open_state(True, request.url.hostname or "")


class Agents(BaseModel):
    agents: list[str] = []


@router.put("/api/setup/agents")
def api_setup_agents(body: Agents, request: Request, who: str = Depends(admin_or_first)):
    """Optional: the coding agents whose connections to suggest (state()'s
    agents: the ones offered; [] for none); their own keys may come later,
    and every template has every agent's layer whatever is chosen. Under the
    setup lock, as finishing is: never saved after the setup completed."""
    with _setup_lock:
        if setup.complete():
            raise HTTPException(409, "Server setup is complete. Turn coding agents on or off under Extensions "
                                     "(sbx extensions).")
        try:
            setup.choose_agents(body.agents, by=who)
        except ValueError as error:
            raise HTTPException(400, str(error))
    return setup.state(_email(who), request.url.hostname or "")


@router.post("/api/setup/complete")
def api_setup_complete(request: Request, who: str = Depends(admin_or_first)):
    with _setup_lock:   # one finish at a time
        s = setup.state(_email(who), request.url.hostname or "")
        if needed := setup.missing(s):
            raise HTTPException(400, needed)
        # No checks here: a click waits for this. Each step checked its part when it was done (the records,
        # the certificate, the runtime's key); GET /api/setup/checks and sbx-admin doctor run them all.
        setup.mark_complete(who)
    template_scheduler.kick()   # what is still stale builds now, not on the scheduler's next round
    return setup.state(_email(who), request.url.hostname or "")
