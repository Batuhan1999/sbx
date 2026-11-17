"""How people sign in: the operator's OIDC provider, each person's own
password (users.py), or no login at all (a private network). app.py reads
this per request, so a change takes effect without a restart."""
from __future__ import annotations

import json
import secrets as pysecrets
import urllib.request

from .. import settings, users, vault
from .base import Check, Connection, Field, Refused, Status



def ready() -> bool:
    """Whether people can sign in as the settings say: the OIDC provider's
    values or the password are there, or no sign-in is wanted."""
    s = settings.current().sign_in
    if s.method == "oidc":
        return bool(s.oidc_issuer and s.oidc_client_id and vault.facts(vault.OIDC_CLIENT_SECRET).set)
    if s.method == "password":
        return users.admin_has_password()
    return True


class SignIn(Connection):
    kind = "sign_in"
    title = "Sign-in"
    shown = ("method", "redirect_uri")

    def status(self) -> Status:
        s = settings.current().sign_in
        oidc_secret = vault.facts(vault.OIDC_CLIENT_SECRET)
        n = len(s.allowed_emails)
        listed = "anyone" if "*" in s.allowed_emails else f"{n} email pattern{'s' if n != 1 else ''}"
        if s.method == "oidc":
            ok = ready()
            state = "connected" if ok else "attention"
            summary = (f"OIDC · {_issuer_name(s.oidc_issuer)} · " + (listed + " and the people added" if n else "the people added")
                       if ok else "OIDC · issuer, client id or secret missing")
        elif s.method == "password":
            state = "connected" if ready() else "attention"
            people = users.password_count()
            summary = (f"passwords · {people} {'person has' if people == 1 else 'people have'} one"
                       if people else "passwords · nobody has one yet")
        else:
            state, summary = "attention", "none · everyone who reaches this host is an admin"
        fields = (Field("method", "Method", value=s.method, help="oidc, password or none"),
                  Field("oidc_issuer", "OIDC issuer", value=s.oidc_issuer),
                  Field("oidc_client_id", "OIDC client id", value=s.oidc_client_id),
                  Field("oidc_client_secret", "OIDC client secret", secret=True, set=oidc_secret.set),
                  Field("allowed_emails", "Who may sign in", value=", ".join(s.allowed_emails),
                        help="OIDC: patterns such as *@example.com, comma separated; * for anyone the provider "
                             "admits. The people added under Settings → People may sign in too"))
        facts = {"method": s.method, "redirect_uri": f"{settings.current().base_url}/auth/callback"}
        return Status(self.kind, self.title, state, summary, facts, fields, ("connect", "test"))

    def test(self, values: dict | None = None) -> list[Check]:
        s = settings.current().sign_in
        v = values or {}
        method = v.get("method", s.method)
        if method == "oidc":
            issuer = (v.get("oidc_issuer") or s.oidc_issuer).rstrip("/")
            if not issuer:
                return [Check("OIDC issuer", False, "none given", "the provider's issuer URL")]
            try:
                with urllib.request.urlopen(f"{issuer}/.well-known/openid-configuration", timeout=10) as r:
                    doc = json.loads(r.read())
                ok = bool(doc.get("authorization_endpoint") and doc.get("token_endpoint"))
                checks = [Check("OIDC discovery", ok, f"{issuer} answers" if ok else "no endpoints in its discovery document",
                                "check the issuer URL")]
            except Exception as error:
                checks = [Check("OIDC discovery", False, f"{issuer}: {error}", "check the issuer URL")]
            has_secret = bool(v.get("oidc_client_secret")) or vault.facts(vault.OIDC_CLIENT_SECRET).set
            checks.append(Check("client id and secret", bool(v.get("oidc_client_id") or s.oidc_client_id) and has_secret,
                                "set" if has_secret else "missing", f"register {settings.current().base_url}/auth/callback at the provider"))
            return checks
        if method == "password":
            has = users.admin_has_password()
            return [Check("an admin with a password", has, "yes" if has else "no admin has one yet",
                          "give an admin a password first: Settings → People, or sbx-admin user link EMAIL")]
        return [Check("open access", True, "no sign-in: everyone is an admin", "use oidc or password on a reachable host")]

    def connect(self, values: dict, *, by: str) -> Status:
        s = settings.current().sign_in
        method = values.get("method", s.method)
        new = {"method": method, "oidc_issuer": (values.get("oidc_issuer") or s.oidc_issuer).strip(),
               "oidc_client_id": (values.get("oidc_client_id") or s.oidc_client_id).strip(),
               "allowed_emails": _emails(values.get("allowed_emails"), s.allowed_emails)}
        try:
            model = settings.SignIn.model_validate(new)
        except ValueError as error:
            raise Refused(str(error)) from error
        if model.refusal():  # settings.update refuses it too; before any secret is stored
            raise Refused(model.refusal())
        new = model.model_dump()
        checks = self.test({**values, **new})
        if not all(c.ok for c in checks):
            raise Refused("not connected: a check failed", checks)
        if values.get("oidc_client_secret"):
            vault.put(vault.OIDC_CLIENT_SECRET, values["oidc_client_secret"].strip(), by=by)
        settings.update("sign_in", new, by=by)
        return self.status()


def use_password(*, by: str) -> dict:
    """Password sign-in as a first install sets it (install.sh: `sbx-admin
    sign-in password`): every admin without a password gets a generated one,
    returned here ({email: password}) and shown once. Refused while there is
    no admin: nobody could sign in."""
    admins = sorted(settings.current().admins)
    if not admins:
        raise Refused("name an admin first (install.sh --admin EMAIL)")
    generated = {}
    for email in admins:
        found = users.get(email)
        if not (found and found["password"]):
            generated[email] = pysecrets.token_urlsafe(18)
            users.set_password(email, generated[email], by=by)
    settings.update("sign_in", {**settings.current().sign_in.model_dump(), "method": "password"}, by=by)
    return generated


def _emails(given, before) -> tuple:
    if given is None:
        return tuple(before)
    if isinstance(given, str):
        given = [p.strip() for p in given.split(",")]
    return tuple(p.lower() for p in given if p)


def _issuer_name(issuer: str) -> str:
    host = issuer.split("//", 1)[-1].split("/", 1)[0]
    return {"accounts.google.com": "Google"}.get(host, host)
