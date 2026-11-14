"""First-run control-plane setup: an address, admin access, and live runtime
checks. Repository and provider connections follow after the server is ready."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import pathlib
import secrets
import sqlite3

from . import embed_storage, paths, settings, vault
from .state import Setting

COMPLETE = Setting("setup_complete")
TOKEN_HASH = Setting("setup_token_hash")


def token_file():
    return paths.etc() / "setup-token"


def complete() -> bool:
    """Set by the wizard's last step, or by infer_complete() for an
    installation configured without it."""
    try:
        return COMPLETE.get() == "1"
    except sqlite3.OperationalError:  # no database yet
        return False


def configured() -> bool:
    from .connections import sign_in
    s = settings.current()
    return bool(s.domain.name or s.domain.address) and sign_in.ready()


def infer_complete() -> bool:
    """sbx-admin setup-infer (host-install.sh on every deploy, and
    install-report.sh): an installation that is configured counts as set
    up. Returns whether it marked anything."""
    if complete() or not configured():
        return False
    mark_complete("infer")
    return True


def mark_complete(by: str):
    COMPLETE.set("1")
    revoke_token()


def reset(sign_in: bool = False, by: str = "sbx-admin"):
    """Back to 'being set up': the gate closes, a new link opens the wizard.
    The domain, its certificate and the connections stay; with sign_in the
    sign-in (back to a password, none set, nobody else listed) and the admins
    are cleared too, so the wizard names the admin again."""
    COMPLETE.delete()
    if sign_in:
        s = settings.current()
        settings.update("sign_in", {"method": "password"}, by=by)
        settings.update("access", {**s.access.model_dump(), "admins": []}, by=by)
        vault.delete(vault.LOGIN_PASSWORD, by=by)
        vault.delete(vault.OIDC_CLIENT_SECRET, by=by)
    return mint_token()


def mint_token() -> str:
    """A new one-time token: its hash in the DB, the token in the file for
    `sbx-admin setup-link`; the old one stops working."""
    token = secrets.token_urlsafe(32)
    TOKEN_HASH.set(hashlib.sha256(token.encode()).hexdigest())
    token_file().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_file().write_text(token + "\n")
    token_file().chmod(0o600)
    return token


def revoke_token():
    TOKEN_HASH.delete()
    token_file().unlink(missing_ok=True)


def token_ok(token: str) -> bool:
    if complete() or not token:
        return False
    found = TOKEN_HASH.get()
    return bool(found) and hmac.compare_digest(found, hashlib.sha256(token.encode()).hexdigest())


def link(token: str | None = None, address: str = "") -> str:
    """Where the operator finishes the setup: the domain's sign-in host once
    the certificate is there, else the public address (self-signed)."""
    from .connections import domain
    s = settings.current()
    host = f"{s.domain.login_host}.{s.domain.name}" if s.domain.name and domain.certificate_ready() else (
        s.domain.address or ip_address(address) or domain.public_ip())
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}/setup" + (f"#t={token}" if token else "")


def host_facts() -> dict:
    import os
    facts = {"kvm": os.path.exists("/dev/kvm")}
    try:
        with open("/proc/meminfo") as f:
            facts["memory_gb"] = round(int(f.readline().split()[1]) / 1024 / 1024)
    except (OSError, ValueError, IndexError):
        pass
    disk = embed_storage.disk()
    if disk:
        facts["disk_free_gb"] = disk["free_gb"]
    return facts


def ip_address(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value.strip("[]")))
    except ValueError:
        return ""


def address_ready() -> bool:
    from .connections import domain
    return domain.certificate_ready()


def readiness() -> dict:
    from .connections import Check, runtime
    host = host_facts()
    checks = [Check("Virtualization", host["kvm"], "KVM is available" if host["kvm"] else
                    "KVM is unavailable", "The server needs /dev/kvm; rerun install.sh after enabling it."),
              Check("Runtime configured", bool(vault.runtime().get("E2B_API_KEY")),
                    "Sandbox runtime credentials", "Run install.sh to finish installing the runtime.")]
    checks.extend(runtime.Runtime().test())
    return {"ok": all(c.ok for c in checks), "checks": [c.__dict__ for c in checks]}


def state(email: str | None, address: str = "") -> dict:
    from . import connections
    from .connections import domain as domain_connection, sign_in as sign_in_connection
    from .connections.domain import ip_certificate_error
    s = settings.current()
    domain = connections.get("domain").status()
    sign_in = connections.get("sign_in").status()
    steps = [{"step": "address", "done": address_ready(), "optional": False, "summary": domain.summary},
             {"step": "sign_in", "done": sign_in_connection.ready(),
              "optional": False, "summary": sign_in.summary}]
    signed_in = bool(email) and email != "anonymous"
    can_complete = steps[0]["done"] and steps[1]["done"] and (signed_in or s.sign_in.method == "none")
    server_address = s.domain.address or ip_address(address) or domain_connection.public_ip()
    return {"complete": complete(), "steps": steps, "signed_in": signed_in, "email": email,
            "can_complete": can_complete, "host": {**host_facts(), "address": server_address},
            "access": {"mode": "domain" if s.domain.name else "ip", "domain": s.domain.name,
                       "url": s.base_url, "certificate": s.domain.acme,
                       "certificate_email": s.domain.acme_email, "certificate_ready": address_ready(),
                       "needs_ports": list(s.domain.needs_ports()),
                       "certificate_error": ip_certificate_error() if s.domain.address and not s.domain.name else ""},
            "link": link(address=server_address),
            "admins": list(s.access.admins), "version": version()}


def first_admin(email: str):
    s = settings.current()
    if not s.access.admins and not complete():
        settings.update("access", {**s.access.model_dump(), "admins": [email.lower()]}, by=f"setup ({email})")


def version() -> str:
    try:   # the release this runs from (control/release.sh writes it)
        return (pathlib.Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
    except OSError:
        return "dev"
