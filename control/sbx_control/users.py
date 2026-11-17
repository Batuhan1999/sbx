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
    """email's row, made when there is none. It starts at a random generation, so a
    session from before a removal can't match the row made when they come back."""
    with db() as conn:
        conn.execute("insert into users (email, created_by, generation) values (?, ?, ?) on conflict do nothing",
                     (email.lower(), by, secrets.randbelow(2 ** 31)))
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
    elif s.sign_in.method == "password" and not admins_with_password(admins):
        raise Refused("Keep an admin who has a password while sign-in uses passwords.")
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


def change_password(email: str, generation: int, password: str) -> int | None:
    """email's own new password, only while their row is still at generation (the one their
    current password was checked at): a removal or another change in between wins. Never
    makes a person. The new generation, or None when it lost."""
    _check_password(password)
    with db() as conn:
        row = conn.execute("update users set password_hash = ?, generation = generation + 1 "
                           "where email = ? and generation = ? returning generation",
                           (_hash(password), email.lower(), generation)).fetchone()
    forget()
    return row[0] if row else None


def signed_in(email: str):
    """Notes the sign-in time on email's row, when they have one. Never makes a person:
    someone OIDC admits only through allowed_emails or the admins stays without a row,
    so editing those lists still decides whether they get in."""
    with db() as conn:
        conn.execute("update users set last_sign_in_at = current_timestamp where email = ?", (email.lower(),))


def any_password() -> bool:
    return password_count() > 0


def password_count() -> int:
    with db() as conn:
        return conn.execute("select count(*) from users where password_hash is not null").fetchone()[0]


def admin_has_password() -> bool:
    """Whether an admin can sign in with a password: switching to passwords must not lock them out."""
    return admins_with_password(settings.current().admins)


def admins_with_password(admins) -> bool:
    """Whether one of these admins has a password."""
    admins = sorted(a.lower() for a in admins)
    if not admins:
        return False
    with db() as conn:
        return bool(conn.execute(f"select 1 from users where password_hash is not null and email in "
                                 f"({','.join('?' * len(admins))}) limit 1", admins).fetchone())


def clear(*, by: str):
    """Everyone gone (sbx-admin setup-reset --sign-in), their API tokens too."""
    with db() as conn:
        conn.execute("delete from users")
        conn.execute("delete from user_links")
        conn.execute("delete from tokens")
    forget()
    from . import auth
    auth.forget_tokens()


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
    """Sets the password the link is for and spends it: (email, generation). The link is
    taken in one statement, so two requests with it cannot both succeed."""
    _check_password(password)
    with db() as conn:
        row = conn.execute("delete from user_links where hash = ? and expires_at > ? returning email",
                           (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    if not row:
        raise Refused("This link is used or expired. Ask an admin for a new one.")
    return row[0], set_password(row[0], password, by=row[0])


SHARED_PASSWORD = "LOGIN_PASSWORD"   # vault row of the one password sign-in had before people had their own


def _carry_shared_password(conn):
    """Upgrading from one shared password: everyone who signed in with it (the admins and the
    emails allowed_emails names exactly) keeps it as their own, unless they have one already;
    then the shared one's row is deleted. Its payload stays until no upgrade backup names it
    (a rollback brings the row back). A store that can't be read leaves it for the next start;
    one whose payload is gone has nothing to carry over."""
    import sys
    from . import secret_store
    row = conn.execute("select value from sealed where key = ?", (SHARED_PASSWORD,)).fetchone()
    if not row:
        return
    try:
        try:
            password = secret_store.unseal(row[0])
        except secret_store.StoreError as error:
            if error.status != 404:
                raise
            print("the shared password's stored value is gone: nothing to carry over", file=sys.stderr)
            password = None
        s = settings.current()
        people = [*s.access.admins, *(e for e in s.sign_in.allowed_emails if "*" not in e and "?" not in e)]
        if s.sign_in.method == "password" and password:
            for email in dict.fromkeys(e.lower() for e in people):
                conn.execute("insert into users (email, password_hash, generation, created_by) values (?, ?, ?, 'upgrade') "
                             "on conflict (email) do update set password_hash = excluded.password_hash, "
                             "generation = users.generation + 1 where users.password_hash is null",
                             (email, _hash(password), secrets.randbelow(2 ** 31)))
        conn.execute("delete from sealed where key = ?", (SHARED_PASSWORD,))
        secret_store.discard_after_rollbacks(conn, row[0])
    except Exception as error:  # noqa: BLE001 - sbx still starts; the next start retries
        print(f"keeping the shared password for now: {error}", file=sys.stderr)
    forget()


SCHEMA = state.Schema(upgrade=_carry_shared_password, tables="""
create table if not exists users (
    email text primary key, password_hash text, generation integer not null default 0,
    created_at text not null default current_timestamp, created_by text not null default '',
    last_sign_in_at text);
create table if not exists user_links (
    hash text primary key, email text not null, expires_at real not null, created_by text not null);
""")
