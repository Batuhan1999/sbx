"""How people sign in: the operator's OIDC provider, one shared password, or
no login at all (a private network). The shared password admits the admins
and allowed_emails, and whoever holds it can sign in as any of them: teams
use OIDC. app.py reads this per request, so a change takes effect without a
restart."""
from __future__ import annotations

import json
import secrets as pysecrets
import urllib.request

from .. import settings, vault
from .base import Check, Connection, Field, Refused, Status



def ready() -> bool:
    """Whether people can sign in as the settings say: the OIDC provider's
    values or the password are there, or no sign-in is wanted."""
    s = settings.current().sign_in
    if s.method == "oidc":
        return bool(s.oidc_issuer and s.oidc_client_id and vault.facts(vault.OIDC_CLIENT_SECRET).set)
    if s.method == "password":
        return vault.facts(vault.LOGIN_PASSWORD).set
    return True


class SignIn(Connection):
    kind = "sign_in"
    title = "Sign-in"
    shown = ("method", "redirect_uri")

    def status(self) -> Status:
        s = settings.current().sign_in
        oidc_secret = vault.facts(vault.OIDC_CLIENT_SECRET)
        password = vault.facts(vault.LOGIN_PASSWORD)
        n = len(s.allowed_emails)
        listed = "anyone" if "*" in s.allowed_emails else f"{n} email pattern{'s' if n != 1 else ''}"
        if s.method == "oidc":
            ok = ready()
            state = "connected" if ok and n else "attention"
            summary = (f"OIDC · {_issuer_name(s.oidc_issuer)} · " + (listed if n else "nobody yet: name who may sign in")
                       if ok else "OIDC · issuer, client id or secret missing")
        elif s.method == "password":
            state = "connected" if ready() else "attention"
            summary = (f"one shared password · the admins{' and ' + listed if n else ''}"
                       if password.set else "password · none set yet")
        else:
            state, summary = "attention", "none · everyone who reaches this host is an admin"
        fields = (Field("method", "Method", value=s.method, help="oidc, password or none"),
                  Field("oidc_issuer", "OIDC issuer", value=s.oidc_issuer),
                  Field("oidc_client_id", "OIDC client id", value=s.oidc_client_id),
                  Field("oidc_client_secret", "OIDC client secret", secret=True, set=oidc_secret.set),
                  Field("password", "Shared password", secret=True, set=password.set,
                        help="leave empty to have one generated"),
                  Field("allowed_emails", "Who may sign in", value=", ".join(s.allowed_emails),
                        help="patterns such as *@example.com, comma separated; * for anyone the provider admits. "
                             "With the password: who may sign in besides the admins"))
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
            has = bool(v.get("password")) or vault.facts(vault.LOGIN_PASSWORD).set
            return [Check("password", has, "set" if has else "none yet", "one is generated on connect")]
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
        generated = ""
        if method == "password" and not values.get("password") and not vault.facts(vault.LOGIN_PASSWORD).set:
            generated = pysecrets.token_urlsafe(18)
        checks = self.test({**values, **new, **({"password": generated} if generated else {})})
        if not all(c.ok for c in checks):
            raise Refused("not connected: a check failed", checks)
        if values.get("oidc_client_secret"):
            vault.put(vault.OIDC_CLIENT_SECRET, values["oidc_client_secret"].strip(), by=by)
        if values.get("password") or generated:
            vault.put(vault.LOGIN_PASSWORD, values.get("password") or generated, by=by)
        settings.update("sign_in", new, by=by)
        status = self.status()
        if generated:  # shown once, to whoever connected
            return Status(status.kind, status.title, status.state, status.summary,
                          {**status.facts, "generated_password": generated}, status.fields, status.actions)
        return status


def use_password(password: str = "", *, by: str) -> Status:
    """Password sign-in as a first install sets it, through install.sh
    (`sbx-admin sign-in password`) or the setup wizard alike: one shared
    password (given, kept, or generated and shown once in the status' facts)
    that admits the admins and allowed_emails only. Refused while there is
    no admin: nobody could sign in."""
    if not settings.current().admins:
        raise Refused("name an admin first (install.sh --admin EMAIL): password sign-in admits only "
                      "the admins and allowed_emails")
    return SignIn().connect({"method": "password", **({"password": password} if password else {})}, by=by)


def _emails(given, before) -> tuple:
    if given is None:
        return tuple(before)
    if isinstance(given, str):
        given = [p.strip() for p in given.split(",")]
    return tuple(p.lower() for p in given if p)


def _issuer_name(issuer: str) -> str:
    host = issuer.split("//", 1)[-1].split("/", 1)[0]
    return {"accounts.google.com": "Google"}.get(host, host)
