"""Serve the web app and enforce same-origin writes."""
import pathlib

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse

from . import addressing

router = APIRouter()


async def same_origin_writes(request: Request, call_next):
    """Middleware, the one cross-origin rule: a write that rides the login
    cookie must come from the web app's own origin (preview hosts share the
    cookie, and SameSite alone does not prevent writes). Bearer and setup
    tokens are not sent by browsers on their own, so those requests pass."""
    origin = request.headers.get("origin")
    own = addressing.control_origin(f"{request.url.scheme}://{request.url.netloc}")
    if (request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != own
            and not request.headers.get("authorization", "").lower().startswith(("bearer ", "setup "))):
        return PlainTextResponse(f"requests from {origin} are not accepted here", status_code=403)
    return await call_next(request)


DIST = pathlib.Path(__file__).parent / "web_dist"


# Scripts only from this server. Inline styles are allowed: the UI libraries (Radix, Sonner) add
# their own <style> at runtime, and React escapes all text, so no outside text becomes markup.
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
       "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://github.com")
HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin"}
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
                            "<code>cd web && npm run build</code>.</p>", status_code=503, headers=HEADERS)
    return HTMLResponse(page.read_text(), headers={**HEADERS, "Cache-Control": "no-store"})


@router.get("/app", include_in_schema=False)
@router.get("/app/{path:path}", include_in_schema=False)
def spa_index(path: str = ""):
    return index()


@router.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/app/repos")


@router.get("/setup", include_in_schema=False)
def setup_link():
    """The first-run link install.sh prints (setup.link); its #t= token stays with the browser."""
    return RedirectResponse("/app/setup")
