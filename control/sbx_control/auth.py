"""Who is asking, and what they may do.

Sign-in: the settings' sign_in method is oidc (the operator's identity provider),
password (one shared password; whoever holds it signs in as any listed email,
the admins' included, so teams use OIDC), or
none (no login: everyone is ANONYMOUS and every preview is open; for a
private network only). The CLI and host tools use API tokens.

The access policy, in one place for every route, the gate and the views:
admins (the settings' access.admins; everyone with login: none) manage the
installation; a sandbox's owner uses it; a sandbox its creator shared with
the team (core.shared: a PR's preview) anyone signed in may open."""
import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
import urllib.parse
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import addressing, core, ip_previews, platform, sandboxes, settings, setup, state, vault
from .state import db

ANONYMOUS = "anonymous"
SESSION_DAYS = 14

router = APIRouter()


class Sessions:
    """The signed session cookie. It covers every *.<domain> host, so the
    gate in front of the sandboxes (control/tls.sh) can check it; that nginx
    removes the cookie before a request reaches a sandbox, where an agent
    could read it. Its key and domain are read when the first request
    arrives; a domain change restarts sbx-control (HostUnit.CONTROL)."""

    def __init__(self, app):
        self.app = app
        self._sessions = None

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if self._sessions is None:
            domain = addressing.domain()
            self._sessions = SessionMiddleware(
                self.app, secret_key=platform.session_key(), https_only=True, same_site="lax",
                session_cookie=platform.SESSION_COOKIE, max_age=SESSION_DAYS * 24 * 3600,
                **({"domain": f".{domain}"} if domain else {}))
        await self._sessions(scope, receive, send)


def session_email(request: Request):
    return ANONYMOUS if settings.current().sign_in.method == "none" else request.session.get("email")


def mint_token(email: str, label: str) -> str:
    """A new API token for email; only its hash is kept."""
    token = "sbx_" + secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute("insert into tokens (hash, email, label) values (?, ?, ?)",
                     (hashlib.sha256(token.encode()).hexdigest(), email, label))
    return token


def revoke_token(token: str):
    with db() as conn:
        conn.execute("delete from tokens where hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))
    _tokens.clear()


TOKEN_ID_LENGTH = 12  # a token's id: the start of its hash, which is all sbx keeps


def tokens(email: str | None = None) -> list:
    """Every API token (one email's), without values: {id, email, label, created_at, last_used_at}."""
    with db() as conn:
        rows = conn.execute("select hash, email, label, created_at, last_used_at from tokens "
                            "where ? is null or email = ? order by created_at", (email, email)).fetchall()
    return [{"id": row[0][:TOKEN_ID_LENGTH], "email": row[1], "label": row[2], "created_at": row[3],
             "last_used_at": row[4]} for row in rows]


def revoke_token_id(token_id: str) -> dict:
    """Revoke the one token whose id (tokens()) is token_id; ValueError for none or several."""
    if len(token_id) < 8 or any(c not in "0123456789abcdef" for c in token_id):
        raise ValueError("a token id is the hex shown by sbx-admin token ls")
    with db() as conn:
        rows = conn.execute("select hash, email, label from tokens where hash like ?", (token_id + "%",)).fetchall()
        if len(rows) != 1:
            raise ValueError(f"{'no' if not rows else 'more than one'} token has the id {token_id}")
        conn.execute("delete from tokens where hash = ?", (rows[0][0],))
    _tokens.clear()
    return {"id": rows[0][0][:TOKEN_ID_LENGTH], "email": rows[0][1], "label": rows[0][2]}


_tokens: dict = {}  # sha256 -> (email or None, fetched_at)


def token_email(token: str):
    """Whose token it is, from memory for a minute: the gate asks on every
    request a page load makes."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    cached = _tokens.get(digest)
    if cached and time.time() - cached[1] < 60:
        return cached[0]
    with db() as conn:
        row = conn.execute("select email from tokens where hash = ?", (digest,)).fetchone()
    _tokens[digest] = (row[0] if row else None, time.time())
    return _tokens[digest][0]


def api_user(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        digest = hashlib.sha256(header[7:].strip().encode()).hexdigest()
        with db() as conn:
            row = conn.execute("update tokens set last_used_at = current_timestamp where hash = ? returning email",
                               (digest,)).fetchone()
        if row:
            return row[0]
    email = session_email(request)
    if email:
        return email
    raise HTTPException(401, "sign in: sbx login")


def is_admin(email: str) -> bool:
    return settings.current().sign_in.method == "none" or email in settings.current().admins


def admin_user(email: str = Depends(api_user)) -> str:
    if not is_admin(email):
        raise HTTPException(403, f"{email} is not an admin of this installation")
    return email


def require_may_set(email: str, mine: bool):
    """A repo's values are the admins'; anyone sets their own (mine)."""
    if not mine and not is_admin(email):
        raise HTTPException(403, "only admins set the repo's values; set your own with mine")


def may_view(owner: str | None, shared: bool, email: str) -> bool:
    """A sandbox's page: its owner, admins, and anyone for a team-shared one."""
    return owner == email or shared or is_admin(email)


def may_open_owner_preview(owner: str | None, shared: bool, email: str) -> bool:
    """An owner-only preview (the desktop): its owner; a team-shared one's are the team's."""
    return owner == email or shared


def require_viewable(name: str, email: str):
    """The runtime's sandbox, when email may view it (may_view)."""
    info = core.find(name)
    if not info:
        raise HTTPException(404, f"no sandbox {name}")
    meta = info.metadata or {}
    if not may_view(meta.get("owner"), core.shared(meta), email):
        raise HTTPException(403, f"{name} belongs to someone else")
    return info


def require_registered(name: str, email: str):
    if sandboxes.registered_owner(name) != email:
        raise HTTPException(404, f"no sandbox {name}")


def require_owned(name: str, email: str):
    """The runtime's sandbox, when this controller registered it for email."""
    require_registered(name, email)
    info = core.find(name)
    if not info:
        raise HTTPException(404, f"no sandbox {name}")
    if (info.metadata or {}).get("owner") != email:
        raise HTTPException(403, f"{name} belongs to someone else")
    return info


def require_loopback(request: Request):
    """Only from this host: nginx in front of the service always sets X-Forwarded-For."""
    if request.client.host != "127.0.0.1" or "x-forwarded-for" in request.headers:
        raise HTTPException(404)


_oidc: dict = {"for": None, "client": None}


def oidc_client():
    """The OIDC client for the sign-in settings as they are now (the Sign-in
    connection may change them while running); None when they are incomplete."""
    found = vault.platform_secrets()
    issuer, client_id, secret = found.get("OIDC_ISSUER", ""), found.get("OIDC_CLIENT_ID", ""), found.get("OIDC_CLIENT_SECRET", "")
    if not (issuer and client_id and secret):
        return None
    key = (issuer, client_id, secret)
    if _oidc["for"] != key:
        oauth = OAuth()
        oauth.register("oidc", client_id=client_id, client_secret=secret,
                       server_metadata_url=issuer.rstrip("/") + "/.well-known/openid-configuration",
                       client_kwargs={"scope": "openid email profile"})
        _oidc.update({"for": key, "client": oauth.oidc})
    return _oidc["client"]


@router.get("/auth/login")
async def auth_login(request: Request, next: str = "/"):
    target = urllib.parse.urlparse(next)
    local_path = next.startswith("/") and not next.startswith("//") and not target.netloc and "\\" not in next and not any(ord(c) < 32 or ord(c) == 127 for c in next)
    request.session["next"] = next if local_path or addressing.on_our_hosts(target) or ip_previews.allowed_url(next) else "/"
    if settings.current().sign_in.method == "oidc":
        client = oidc_client()
        if client is None:
            raise HTTPException(503, "sign-in is OIDC, but its issuer, client id or secret is not set: "
                                     "sbx-admin connect sign_in ...")
        return await client.authorize_redirect(request, f"{settings.current().base_url}/auth/callback")
    if settings.current().sign_in.method == "none":
        return RedirectResponse(request.session.pop("next", "/"))
    return RedirectResponse("/app/sign-in")



def may_sign_in(email: str) -> bool:
    """The sign-in method's allow-list (settings.SignIn.admits): OIDC only
    whom allowed_emails names, the shared password only the admins and
    allowed_emails."""
    s = settings.current()
    return s.sign_in.admits(email, s.admins)

def password_admits(email: str, password: str) -> bool:
    """The shared password sign-in: the password is the installation's and
    the sign-in method admits email (may_sign_in). The web's sign-in form, and
    sbx setup finish --admin (the first-run setup's last step, without a
    browser)."""
    expected = vault.platform_secrets().get("LOGIN_PASSWORD", "")
    if settings.current().sign_in.method != "password" or not expected or "@" not in email:
        return False
    return hmac.compare_digest(password.encode(), expected.encode()) and may_sign_in(email)


@router.post("/auth/login")
async def auth_password(request: Request):
    if settings.current().sign_in.method != "password":
        raise HTTPException(404)
    form = urllib.parse.parse_qs((await request.body()).decode())
    email = (form.get("email") or [""])[0].strip().lower()
    password = (form.get("password") or [""])[0]
    if not password_admits(email, password):
        await asyncio.sleep(1)  # slows guessing
        return JSONResponse({"detail": "The email or password is incorrect."}, status_code=401)
    request.session["email"] = email
    request.session["name"] = email
    setup.first_admin(email)
    target = request.session.pop("next", "/")
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"next": target})
    return RedirectResponse(target, status_code=303)


@router.get("/auth/callback")
async def auth_callback(request: Request):
    if settings.current().sign_in.method != "oidc":
        raise HTTPException(404)
    client = oidc_client()
    if client is None:
        raise HTTPException(503, "sign-in is OIDC, but its settings are incomplete")
    token = await client.authorize_access_token(request)
    claims = token.get("userinfo") or {}
    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(403, "the account has no email")
    if str(claims.get("email_verified", "")).lower() == "false":  # some providers send the string
        raise HTTPException(403, f"the provider has not verified {email}")
    if not may_sign_in(email):
        raise HTTPException(403, f"{email} may not sign in here")
    request.session["email"] = email
    request.session["name"] = claims.get("name", email)
    setup.first_admin(email)
    return RedirectResponse(request.session.pop("next", "/"))


@router.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


# `sbx login`: the browser hands the CLI's loopback listener a code good for
# two minutes and once; the CLI exchanges it by POST for an API token, with
# the verifier whose hash it sent first (PKCE). The token itself never sits
# in a URL, so neither browser history nor a page that only navigates here
# gets one.

CLI_CODE_SECONDS = 120
_cli_codes: dict = {}  # sha256(code) -> (email, challenge, expires_at)
_cli_lock = threading.Lock()


@router.get("/cli/login")
async def cli_login(request: Request, port: int, state: str, challenge: str):
    email = session_email(request)
    query = urlencode({"port": port, "state": state, "challenge": challenge})
    if not email:
        return RedirectResponse(f"/auth/login?{urlencode({'next': f'/cli/login?{query}'})}")
    if not 0 < port < 65536 or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
        raise HTTPException(400, "sbx login sends a loopback port and a challenge")
    code = secrets.token_urlsafe(32)
    now = time.time()
    with _cli_lock:
        for key in [k for k, (_, _, expires) in _cli_codes.items() if expires < now]:
            del _cli_codes[key]
        _cli_codes[hashlib.sha256(code.encode()).hexdigest()] = (email, challenge, now + CLI_CODE_SECONDS)
    return RedirectResponse(f"http://127.0.0.1:{port}/callback?{urlencode({'state': state, 'code': code})}")


class CliCode(BaseModel):
    code: str
    verifier: str


@router.post("/cli/token")
def cli_token(body: CliCode):
    with _cli_lock:
        found = _cli_codes.pop(hashlib.sha256(body.code.encode()).hexdigest(), None)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(body.verifier.encode()).digest()).rstrip(b"=").decode()
    if not found or found[2] < time.time() or not hmac.compare_digest(found[1], challenge):
        raise HTTPException(400, "the sign-in code is unknown, used or expired: sbx login")
    return JSONResponse({"token": mint_token(found[0], "cli")}, headers={"Cache-Control": "no-store"})


SCHEMA = state.Schema(tables="""
create table if not exists tokens (
    hash text primary key, email text not null, label text not null,
    created_at text not null default current_timestamp, last_used_at text);
""")
