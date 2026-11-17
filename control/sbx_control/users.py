"""The people of an installation and their passwords.

A person is a row: their email, a password hash when they have one (password
sign-in; with OIDC the provider checks who they are), and a generation that
every password change and removal moves on. A session carries the generation
it was signed in with, so changing a password or removing someone ends their
sessions everywhere. Whether they are an admin is the settings' access.admins.

Nobody sends mail: an admin hands a new person (or one who forgot their
password) a one-time link to set it, good for LINK_DAYS."""
from __future__ import annotations

import hashlib
import re
import secrets
import time

from pwdlib import PasswordHash

from . import settings, state
from .state import db

LINK_DAYS = 7
MIN_PASSWORD, MAX_PASSWORD = 8, 1024
_hasher = PasswordHash.recommended()  # argon2id


class Refused(Exception):
    pass


def valid_email(email: str) -> str:
    """email, lowercased; Refused when it is not one."""
    email = (email or "").strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise Refused("Enter a valid email address.")
    return email


def _check_password(password: str):
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise Refused(f"Use a password between {MIN_PASSWORD} and {MAX_PASSWORD} characters.")


def _hash(password: str) -> str:
    return _hasher.hash(password)


def _matches(password: str, stored: str | None) -> bool:
    if not stored:
        _hasher.hash(password)  # the same work either way: no timing hint that the email has no password
        return False
    return _hasher.verify(password, stored)


def get(email: str) -> dict | None:
    with db() as conn:
        row = conn.execute("select email, password_hash is not null, generation, created_at, created_by, "
                           "last_sign_in_at from users where email = ?", (email.lower(),)).fetchone()
    return _row(row) if row else None


def _row(row) -> dict:
    return {"email": row[0], "password": bool(row[1]), "generation": row[2], "created_at": row[3],
            "created_by": row[4], "last_sign_in_at": row[5]}


def ls() -> list[dict]:
    """Everyone: the rows, and the admins the settings name without one
    (install.sh --admin), each with admin and how many API tokens they hold."""
    admins = settings.current().admins
    with db() as conn:
        rows = [_row(r) for r in conn.execute(
            "select email, password_hash is not null, generation, created_at, created_by, last_sign_in_at "
            "from users order by email").fetchall()]
        counts = dict(conn.execute("select email, count(*) from tokens group by email").fetchall())
    known = {r["email"] for r in rows}
    rows += [{"email": a, "password": False, "generation": 0, "created_at": None, "created_by": None,
              "last_sign_in_at": None} for a in sorted(admins - known)]
    return sorted(({**r, "admin": r["email"] in admins, "tokens": counts.get(r["email"], 0)} for r in rows),
                  key=lambda r: (not r["admin"], r["email"]))


def ensure(email: str, *, by: str) -> dict:
    """email's row, made when there is none."""
    with db() as conn:
        conn.execute("insert into users (email, created_by) values (?, ?) on conflict do nothing",
                     (email.lower(), by))
    return get(email)


def add(email: str, *, admin: bool = False, by: str) -> dict:
    email = valid_email(email)
    if get(email):
        raise Refused(f"{email} is already here.")
    ensure(email, by=by)
    if admin:
        set_admin(email, True, by=by)
    return get(email)


def set_admin(email: str, admin: bool, *, by: str):
    email = email.lower()
    s = settings.current()
    admins = [a for a in s.access.admins if a.lower() != email]
    if admin:
        admins.append(email)
    elif not admins:
        raise Refused("Keep at least one admin.")
    settings.update("access", {**s.access.model_dump(), "admins": admins}, by=by)


def remove(email: str, *, by: str):
    """Their row, their admin role, their API tokens and set-password links;
    their sessions end (session_valid)."""
    email = email.lower()
    if email in settings.current().admins:
        set_admin(email, False, by=by)
    with db() as conn:
        conn.execute("delete from users where email = ?", (email,))
        conn.execute("delete from tokens where email = ?", (email,))
        conn.execute("delete from user_links where email = ?", (email,))
    forget()
    from . import auth
    auth.forget_tokens()


def set_password(email: str, password: str, *, by: str) -> int:
    """A new password for email (made a person if they are not yet); their
    other sessions end. Returns the new generation, for this one."""
    _check_password(password)
    email = ensure(email, by=by)["email"]
    with db() as conn:
        generation = conn.execute("update users set password_hash = ?, generation = generation + 1 "
                                  "where email = ? returning generation", (_hash(password), email)).fetchone()[0]
        conn.execute("delete from user_links where email = ?", (email,))
    forget()
    return generation


def check_password(email: str, password: str) -> int | None:
    """email's generation when password is theirs, else None."""
    with db() as conn:
        row = conn.execute("select password_hash, generation from users where email = ?",
                           (email.lower(),)).fetchone()
    return row[1] if _matches(password, row[0] if row else None) else None


def signed_in(email: str, *, by: str) -> int:
    """Notes the sign-in (an OIDC person becomes a row); their generation."""
    ensure(email, by=by)
    with db() as conn:
        return conn.execute("update users set last_sign_in_at = current_timestamp where email = ? "
                            "returning generation", (email.lower(),)).fetchone()[0]


def any_password() -> bool:
    with db() as conn:
        return bool(conn.execute("select 1 from users where password_hash is not null limit 1").fetchone())


def clear(*, by: str):
    """Everyone gone (sbx-admin setup-reset --sign-in)."""
    with db() as conn:
        conn.execute("delete from users")
        conn.execute("delete from user_links")
    forget()


_generations: dict = {}  # email -> (generation or None, fetched_at)


def generation(email: str) -> int | None:
    """email's generation, None when they are not a person here; from memory
    for a few seconds: the gate asks on every request a page load makes."""
    cached = _generations.get(email)
    if cached and time.time() - cached[1] < 5:
        return cached[0]
    found = get(email)
    _generations[email] = (found["generation"] if found else None, time.time())
    return _generations[email][0]


def forget():
    _generations.clear()


def mint_link(email: str, *, by: str) -> str:
    """A one-time token that sets email's password; the person is made when
    they are not one yet, and an older link of theirs stops working."""
    email = ensure(valid_email(email), by=by)["email"]
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute("delete from user_links where email = ?", (email,))
        conn.execute("insert into user_links (hash, email, expires_at, created_by) values (?, ?, ?, ?)",
                     (hashlib.sha256(token.encode()).hexdigest(), email, time.time() + LINK_DAYS * 86400, by))
    return token


def link_url(token: str) -> str:
    return f"{settings.current().base_url}/app/set-password#t={token}"


def link_email(token: str) -> str | None:
    """Whose link it is while it is good."""
    with db() as conn:
        row = conn.execute("select email, expires_at from user_links where hash = ?",
                           (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    return row[0] if row and row[1] > time.time() else None


def use_link(token: str, password: str) -> tuple[str, int]:
    """Sets the password the link is for and spends it: (email, generation)."""
    email = link_email(token)
    if not email:
        raise Refused("This link is used or expired. Ask an admin for a new one.")
    _check_password(password)
    return email, set_password(email, password, by=email)


SCHEMA = state.Schema(tables="""
create table if not exists users (
    email text primary key, password_hash text, generation integer not null default 0,
    created_at text not null default current_timestamp, created_by text not null default '',
    last_sign_in_at text);
create table if not exists user_links (
    hash text primary key, email text not null, expires_at real not null, created_by text not null);
""")
