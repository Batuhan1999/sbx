"""Who is signed in and what the clients need to know (/api/me), and the
installation's settings, a section at a time (/api/settings)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import extensions, reach, settings, setup, variables
from .auth import api_user, is_admin
from .setup_api import admin_or_first

router = APIRouter()


@router.get("/api/me")
def api_me(email: str = Depends(api_user)):
    """Who I am and what the clients need: admin or not, the sign-in method,
    the domain, whether the setup is complete; for the CLI the agents a
    developer brings their own login for, and who env values may go to."""
    s = settings.current()
    return {"email": email, "admin": is_admin(email), "login": s.sign_in.method, "domain": s.domain.name,
            "base_url": s.base_url, "setup_complete": setup.complete(), "version": setup.version(),
            "repos": list(s.repos), "agents": extensions.logins(), "audiences": variables.AUDIENCES}


@router.get("/api/settings")
def api_settings(email: str = Depends(admin_or_first)):
    """The settings, and which of them the runtime can enforce (offered): a
    client offers no other."""
    s = settings.current()
    return {"revision": s.revision, **{section: getattr(s, section).model_dump(mode="json") for section in settings.SECTIONS},
            "repos": list(s.repos), "offered": reach.offered()}


class SectionIn(BaseModel):
    value: dict
    revision: int | None = None


@router.put("/api/settings/{section}")
def api_settings_put(section: str, body: SectionIn, email: str = Depends(admin_or_first)):
    if section not in settings.SECTIONS:
        raise HTTPException(404, f"no section {section}")
    try:
        s = settings.update(section, body.value, by=email, expect_revision=body.revision)
    except settings.Conflict as error:
        raise HTTPException(409, str(error))
    except settings.Invalid as error:
        raise HTTPException(400, str(error))
    return {"revision": s.revision, section: getattr(s, section).model_dump(mode="json")}
