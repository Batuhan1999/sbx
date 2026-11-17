"""/api/users: the people, for admins; /api/me/password: one's own.

  GET    /api/users                  everyone: email, admin, password set, last sign-in, API tokens
  POST   /api/users                  {email, admin} add one; the answer has a one-time set-password link
  PUT    /api/users/{email}          {admin} make them an admin or not
  POST   /api/users/{email}/link     a new set-password link; the older one stops working
  DELETE /api/users/{email}          remove them (not yourself): sessions, API tokens and admin role end
  POST   /api/me/password            {current, password} change your own (password sign-in)"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, SecretStr

from . import auth, settings, users

router = APIRouter()


def _view() -> dict:
    return {"users": [{k: v for k, v in u.items() if k != "generation"} for u in users.ls()],
            "login": settings.current().sign_in.method}


def _known(email: str) -> str:
    email = email.lower()
    if not users.get(email) and email not in settings.current().admins:
        raise HTTPException(404, f"{email} is not here")
    return email


def _link(email: str, by: str) -> dict:
    return {"email": email, "link": users.link_url(users.mint_link(email, by=by)), "days": users.LINK_DAYS}


@router.get("/api/users")
def api_users(_admin: str = Depends(auth.admin_user)):
    return _view()


class NewUser(BaseModel):
    email: str
    admin: bool = False


@router.post("/api/users")
def api_users_add(body: NewUser, admin: str = Depends(auth.admin_user)):
    try:
        email = users.add(body.email, admin=body.admin, by=admin)["email"]
    except users.Refused as error:
        raise HTTPException(400, str(error))
    return {**_view(), **_link(email, admin)}


class Role(BaseModel):
    admin: bool


@router.put("/api/users/{email}")
def api_users_role(email: str, body: Role, admin: str = Depends(auth.admin_user)):
    email = _known(email)
    try:
        users.ensure(email, by=admin)
        users.set_admin(email, body.admin, by=admin)
    except users.Refused as error:
        raise HTTPException(400, str(error))
    return _view()


@router.post("/api/users/{email}/link")
def api_users_link(email: str, admin: str = Depends(auth.admin_user)):
    return _link(_known(email), admin)


@router.delete("/api/users/{email}")
def api_users_remove(email: str, admin: str = Depends(auth.admin_user)):
    email = _known(email)
    if email == admin:
        raise HTTPException(400, "You cannot remove yourself; another admin can.")
    try:
        users.remove(email, by=admin)
    except users.Refused as error:
        raise HTTPException(400, str(error))
    return _view()


class NewPassword(BaseModel):
    current: SecretStr
    password: SecretStr


@router.post("/api/me/password")
def api_me_password(body: NewPassword, request: Request):
    email = auth.session_email(request)
    if not email or email == auth.ANONYMOUS or settings.current().sign_in.method != "password":
        raise HTTPException(400, "Only someone signed in with a password changes it here.")
    if users.check_password(email, body.current.get_secret_value()) is None:
        raise HTTPException(400, "The current password is incorrect.")
    try:
        generation = users.set_password(email, body.password.get_secret_value(), by=email)
    except users.Refused as error:
        raise HTTPException(400, str(error))
    auth.start_session(request, email, generation, request.session.get("name", ""))
    return {"email": email}
