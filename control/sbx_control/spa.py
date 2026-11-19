"""Serve the web app and enforce same-origin writes."""
import pathlib

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse

import ipaddress

from . import addressing, setup

router = APIRouter()


async def same_origin_writes(request: Request, call_next):
    """Middleware, the one cross-origin rule: a write that rides the login
    cookie must come from the web app's own origin (preview hosts share the
    cookie, and SameSite alone does not prevent writes). A bearer token is
    not sent by browsers on their own, so those requests pass. During the
    onboarding the page may still be at the server's IP address after the
    domain is saved (to try the certificate again, and to move on): its
    writes to that same address pass until the onboarding is complete."""
    origin = request.headers.get("origin")
    asked = f"{request.url.scheme}://{request.url.netloc}"
    own = addressing.control_origin(asked)
    if (request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != own
            and not (origin == asked and _ip(request.url.hostname) and not setup.complete())
            and not request.headers.get("authorization", "").lower().startswith("bearer ")):
        return PlainTextResponse(f"requests from {origin} are not accepted here", status_code=403)
    return await call_next(request)


def _ip(host) -> bool:
    try:
        ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return False
    return True


DIST = pathlib.Path(__file__).parent / "web_dist"


# Scripts only from this server. Inline styles are allowed: the UI libraries (Radix, Sonner) add
# their own <style> at runtime, and React escapes all text, so no outside text becomes markup.
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src {connect}; "
       "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://github.com")


def headers() -> dict:
    """The page's headers. A page's policy is fixed when it loads, and the onboarding page loads at the
    server's IP address before a domain is chosen: it then asks its new address whether it answers the
    browser before it moves there (SetupPage's askOrigin). So until the onboarding is complete the page may
    ask any https address; afterwards only this server."""
    connect = "'self'" if setup.complete() else "'self' https:"
    return {"Content-Security-Policy": CSP.format(connect=connect),
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin"}


TYPES = {".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".svg": "image/svg+xml",
         ".png": "image/png", ".map": "application/json"}


@router.get("/assets/{path:path}", include_in_schema=False)
def spa_asset(path: str):
    target = (DIST / "assets" / path).resolve()
    if not target.is_relative_to((DIST / "assets").resolve()) or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target, media_type=TYPES.get(target.suffix, "application/octet-stream"),
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


def index() -> HTMLResponse:
    page = DIST / "index.html"
    if not page.exists():
        return HTMLResponse("<!doctype html><title>sbx</title><p>The web app is not built: "
                            "<code>cd web && npm run build</code>.</p>", status_code=503, headers=headers())
    return HTMLResponse(page.read_text(), headers={**headers(), "Cache-Control": "no-store"})


@router.get("/app", include_in_schema=False)
@router.get("/app/{path:path}", include_in_schema=False)
def spa_index(path: str = ""):
    return index()


@router.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/app/repos")


@router.get("/onboarding", include_in_schema=False)
def onboarding():
    """The onboarding page (setup.link), until the onboarding is complete (setup_api.setup_gate)."""
    return index()
