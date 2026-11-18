"""/api/setup: the first-run wizard's state and completion. Before the setup
is complete the one-time token (`Authorization: Setup <token>`, from the
link install.sh printed) stands in for an admin here and on
/api/connections; afterwards admins only, as everywhere."""
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, SecretStr

from . import auth, settings, setup, users
from .connections import sign_in

router = APIRouter()
_setup_lock = threading.Lock()

# Until the first-run setup is complete (setup.py), this host serves the
# setup and nothing else: there is no sign-in yet to protect the rest.
SETUP_OPEN = ("/setup", "/app", "/assets/", "/api/setup", "/api/connections", "/api/settings", "/api/catalog",
              "/api/runtimes", "/api/secret-requests", "/keys/", "/connections/github", "/auth/")
# Open by exact path: the release string, which the setup's reachability
# check fetches from inside a sandbox (reachability.PATH).
SETUP_OPEN_EXACT = ("/dist/version",)


async def setup_gate(request: Request, call_next):
    """Middleware: only the setup until it is complete."""
    path = request.url.path
    if path == "/gate":
        return PlainTextResponse("setting up", status_code=403) if not setup.complete() else await call_next(request)
    if not setup.complete() and path in ("/", "/app", "/app/"):
        return RedirectResponse("/app/setup")
    if not setup.complete() and path not in SETUP_OPEN_EXACT and not any(path == p or path.startswith(p)
                                                                         for p in SETUP_OPEN):
        return PlainTextResponse("This installation is being set up. Finish at /setup (the link install.sh printed).",
                                 status_code=503)
    return await call_next(request)


def setup_or_admin(request: Request) -> str:
    """Who may run the setup: the token's holder while it is incomplete, or an admin."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("setup ") and setup.token_ok(header[6:].strip()):
        return "setup"
    try:
        email = auth.api_user(request)
    except HTTPException:
        if not setup.complete():
            raise HTTPException(401, "the setup token from the install's link, or sign in")
        raise
    if auth.is_admin(email) or (not setup.complete() and not settings.current().admins):
        return email
    raise HTTPException(403, f"{email} is not an admin of this installation")


def _email(request: Request, who: str) -> str | None:
    if who != "setup":
        return who
    email = auth.session_email(request)
    return email if email in settings.current().admins else None


@router.get("/api/setup")
def api_setup(request: Request, who: str = Depends(setup_or_admin)):
    return setup.state(_email(request, who), request.url.hostname or "")


@router.get("/api/setup/checks")
def api_setup_checks(who: str = Depends(setup_or_admin)):
    return setup.readiness()


class Address(BaseModel):
    address: str


@router.post("/api/setup/address")
def api_setup_address(body: Address, request: Request, who: str = Depends(setup_or_admin)):
    if setup.complete():
        raise HTTPException(409, "Server setup is complete. Manage its address in Settings.")
    address = setup.ip_address(body.address)
    if not address:
        raise HTTPException(400, "Use this server's IP address.")
    s = settings.current()
    if s.domain.name:
        raise HTTPException(409, "A domain is already configured. Finish there, or change it in Settings.")
    settings.update("domain", {**s.domain.model_dump(), "address": address}, by=who)
    return setup.state(_email(request, who), address)


class Admin(BaseModel):
    email: str
    password: SecretStr


@router.post("/api/setup/admin")
def api_setup_admin(body: Admin, request: Request, who: str = Depends(setup_or_admin)):
    """The admin's login: a password for email, who joins the admins (the
    installer may have named some already, with no way to sign in yet)."""
    with _setup_lock:
        s = settings.current()
        if setup.complete() or sign_in.ready():
            raise HTTPException(409, "Admin access is already configured. Sign in to finish setup.")
        if not setup.address_ready():
            raise HTTPException(400, "Finish choosing the server address first.")
        try:
            email = users.valid_email(body.email)
            generation = users.set_password(email, body.password.get_secret_value(), by=f"setup ({email})")
        except users.Refused as error:
            raise HTTPException(400, str(error))
        admins = list(dict.fromkeys([*(a.lower() for a in s.access.admins), email]))
        settings.update("access", {**s.access.model_dump(), "admins": admins}, by=f"setup ({email})")
        settings.update("sign_in", {**s.sign_in.model_dump(), "method": "password"}, by=f"setup ({email})")
        auth.start_session(request, email, generation)
        return setup.state(email, request.url.hostname or "")


class Finish(BaseModel):
    """An admin's sign-in with their own password, for a client without a
    browser session (sbx setup finish --admin)."""
    email: str
    password: SecretStr


def finishing_admin(request: Request, body: Finish | None = None, who: str = Depends(setup_or_admin)) -> str | None:
    """The admin who finishes: the session's, or one who gives their own password (Finish); None: nobody yet."""
    email = _email(request, who)
    if email or body is None:
        return email
    given = body.email.strip().lower()
    s = settings.current()
    if (users.check_password(given, body.password.get_secret_value()) is None or s.sign_in.method != "password"
            or given not in s.admins):
        raise HTTPException(403, "The email or password is incorrect.")
    return given


@router.post("/api/setup/complete")
def api_setup_complete(request: Request, email: str | None = Depends(finishing_admin),
                       who: str = Depends(setup_or_admin)):
    s = setup.state(email, request.url.hostname or "")
    if needed := setup.missing(s):
        raise HTTPException(400, needed)
    if settings.current().sign_in.method != "none" and not email:
        raise HTTPException(400, "sign in with the method you chose; that makes you the first admin, then finish")
    checks = setup.readiness()
    if not checks["ok"]:
        raise HTTPException(400, {"message": "The server needs attention before setup can finish.",
                                  "checks": checks["checks"]})
    setup.mark_complete(email or who)
    return setup.state(email, request.url.hostname or "")
