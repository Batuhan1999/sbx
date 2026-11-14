"""The CLI's downloads and version, and the template builder's repo bundles.

Public on purpose: the CLI holds no secrets; using them takes a sign-in.
host-install.sh packs them into dist/."""
import hashlib
import pathlib
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from . import settings, templates

DIST = pathlib.Path(__file__).resolve().parent.parent / "dist"

router = APIRouter()


def dist_version() -> str:
    """The CLI release host-install built, or dev when there is none (a test)."""
    path = DIST / "VERSION"
    return path.read_text().strip() if path.exists() else "dev"


async def version_header(request: Request, call_next):
    """Middleware: every API answer names the release, for the CLI's update check."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["X-Sbx-Version"] = dist_version()
    return response


@router.get("/install", response_class=PlainTextResponse)
def install_script():
    return (DIST / "install.sh").read_text().replace("@SBX_CONTROL@", settings.current().base_url)


@router.get("/dist/version", response_class=PlainTextResponse)
def version():
    return dist_version()


@router.get("/bundles")
def bundle(request: Request):
    """A repo bundle or artifact for a template build (templates.fetch); its
    one-time token is the only key, and the build deletes the file when it
    is done."""
    token = request.headers.get(templates.TOKEN_HEADER, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", token):
        raise HTTPException(404)
    path = templates.bundles() / f"{token}.bundle"
    try:
        expired = time.time() - path.stat().st_mtime > templates.BUNDLE_TTL
    except OSError:
        raise HTTPException(404) from None
    if expired:
        path.unlink(missing_ok=True)
        raise HTTPException(404)
    return FileResponse(path, media_type="application/octet-stream", headers={"cache-control": "no-store"})


@router.get("/dist/sbx.tgz")
def dist_tarball():
    return FileResponse(DIST / "sbx.tgz", media_type="application/gzip", filename="sbx.tgz")


@router.get("/dist/sbx.tgz.sha256", response_class=PlainTextResponse)
def dist_digest():
    """What the installer and `sbx update` check the tarball against before unpacking it."""
    try:
        return hashlib.sha256((DIST / "sbx.tgz").read_bytes()).hexdigest()
    except OSError:
        raise HTTPException(404, "this server has no CLI release") from None
