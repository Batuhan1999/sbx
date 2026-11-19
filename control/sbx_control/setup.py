"""First-run control-plane setup (the onboarding page): the admin account
first, then an address, a sandbox runtime (added here when install.sh
enabled none) and live runtime checks. Nothing but the first admin's account
is open without signing in, and that only while nobody can sign in; every
later step is an admin's. Repository and provider connections follow after
the server is ready.

Templates build as soon as there is a runtime and an address, during the
setup too: every template has every coding agent's layer (Amp's in the
base), so choosing coding agents (choose_agents) is optional: it records
which agents' connections to suggest, and nothing waits for it. The setup's
reachability check needs a built base template."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import pathlib
import secrets
import sqlite3
import sys
import time

from . import runtime, settings, users, vault
from .state import Setting

COMPLETE = Setting("setup_complete")
# The page's move from the IP address to the domain: one sign-in carried over (handoff, enter).
HANDOFF = Setting("setup_handoff", encoded=True)
HANDOFF_SECONDS = 120
# The coding agents chosen (optional): a list of extension kinds ([]: none); unset: not chosen.
AGENTS = Setting("setup_agents", encoded=True)


def complete() -> bool:
    """Set by the wizard's last step, or by infer_complete() for an
    installation configured without it."""
    try:
        return COMPLETE.get() == "1"
    except sqlite3.OperationalError:  # no database yet
        return False


def configured() -> bool:
    """An address, a way to sign in, and a sandbox runtime."""
    from .connections import sign_in
    s = settings.current()
    return bool(s.domain.name or s.domain.address) and sign_in.ready() and bool(runtime.enabled())


def infer_complete() -> bool:
    """sbx-admin setup-infer (host-install.sh on every deploy, and
    install-report.sh): an installation that is configured (an address, a
    way to sign in, a runtime) counts as set up; what the onboarding page
    has left then is optional. Returns whether it marked anything."""
    if complete() or not configured():
        return False
    mark_complete("infer")
    return True


def offered_agents() -> list:
    """The extensions the setup offers as coding agents (Connector.choosable)."""
    from . import extensions
    return [c for c in extensions.connectors() if c.choosable]


def agents_chosen() -> bool:
    """Whether coding agents were chosen (optional): in the setup, or the
    setup is complete (they are enabled under Extensions since)."""
    return complete() or AGENTS.get() is not None


def choose_agents(kinds, by: str) -> list:
    """Record the coding agents (kinds of offered_agents) whose connections
    to suggest; each one's own key may come later (Connector.choose). Every
    template has every agent's layer whatever is chosen."""
    offered = {c.kind: c for c in offered_agents()}
    kinds = sorted({k.strip().lower() for k in kinds if k.strip()})
    unknown = [k for k in kinds if k not in offered]
    if unknown:
        raise ValueError(f"no coding agent {', '.join(unknown)} here; choose from: {', '.join(offered) or 'none'}")
    for kind, connector in offered.items():
        connector.choose(kind in kinds, by=by)
    AGENTS.set(kinds)
    return kinds


def mark_complete(by: str):
    COMPLETE.set("1")
    HANDOFF.delete()


def reset(sign_in: bool = False, by: str = "sbx-admin"):
    """Back to 'being set up': the onboarding page opens again. The domain,
    its certificate and the connections stay; with sign_in the sign-in (back
    to passwords, nobody listed), the people and the admins are cleared too,
    so the page asks for the first admin again."""
    COMPLETE.delete()
    if sign_in:
        s = settings.current()
        settings.update("sign_in", {"method": "password"}, by=by)
        settings.update("access", {**s.access.model_dump(), "admins": []}, by=by)
        users.clear(by=by)
        vault.delete(vault.OIDC_CLIENT_SECRET, by=by)


def handoff(email: str) -> str:
    """A one-time code that signs email in once more, within two minutes:
    the onboarding page takes it from the server's IP address to its domain,
    where the IP address's session cookie does not reach. Its hash is kept,
    and a new one replaces the last."""
    code = secrets.token_urlsafe(32)
    HANDOFF.set({"hash": hashlib.sha256(code.encode()).hexdigest(), "email": email,
                 "expires": time.time() + HANDOFF_SECONDS})
    return code


def enter(code: str) -> str:
    """Whom handoff()'s code signs in, once ("" when it is not the current
    one, is used or is too old). A wrong guess does not spend the real one."""
    found = HANDOFF.get()
    if not found or not code:
        return ""
    if time.time() > found["expires"]:
        HANDOFF.delete()
        return ""
    if not hmac.compare_digest(found["hash"], hashlib.sha256(code.encode()).hexdigest()):
        return ""
    HANDOFF.delete()
    return found["email"]


def link(address: str = "") -> str:
    """Where the owner does the onboarding, the web app's onboarding page: the
    domain's sign-in host once the certificate is there, else the address
    (its trusted IP certificate once issued, else self-signed). It carries
    no credential: the page's first step makes the first admin's account."""
    from .connections import domain
    s = settings.current()
    host = f"{s.domain.login_host}.{s.domain.name}" if s.domain.name and domain.certificate_ready() else (
        s.domain.address or ip_address(address) or domain.public_ip())
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}/onboarding"


def onboarding() -> dict:
    """install.sh's last line while the setup is open (install-report.sh):
    the onboarding page's address, and whether the browser trusts the
    certificate it opens on."""
    from .connections import domain
    if complete():
        raise RuntimeError("the setup is complete")
    return {"link": link(), "trusted": domain.certificate_ready(), "domain": settings.current().domain.name}


def open_state(signed_in: bool, address: str = "") -> dict:
    """What anyone may know before signing in (GET /api/setup/open): whether
    the onboarding is done, whether someone can sign in yet (else the page's
    first step makes the first admin), and where the page is now."""
    from .connections import sign_in
    return {"complete": complete(), "account": sign_in.ready(), "signed_in": signed_in,
            "link": link(address), "version": version()}


# install.sh waits this long for the IP certificate before it prints the onboarding address.
IP_CERTIFICATE_SECONDS = 300


def _public(address: str) -> bool:
    """An address Let's Encrypt can reach: a global one, not a private or documentation range."""
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def _port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def first_certificate(timeout: float = IP_CERTIFICATE_SECONDS, *, sleep=None, clock=None) -> tuple[bool, str]:
    """install.sh, before it prints the onboarding address of a server with no
    address yet: IP access with its trusted certificate, as the setup's
    "IP access" choice makes it (the address in the settings; tls.sh has
    Certbot validate it over HTTP on port 80), so the first visit opens
    without a browser warning. Only with a public IP and port 80 free on
    this host; the provider's firewall shows only in Certbot's answer. Waits
    for the host's tls effect up to timeout seconds. Returns (trusted, what
    happened). Without the certificate, setup keeps the address step open
    with Certbot's error and its retry; a domain can still be chosen there."""
    import time
    from . import host_requests
    from .connections import domain
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    s = settings.current()
    if complete():
        return domain.certificate_ready(), "the setup is complete"
    if s.domain.name:
        return domain.certificate_ready(), f"the domain {s.domain.name} gets its own certificate"
    if s.domain.address:   # chosen before: its renewal (the sbx-tls-renew timer) keeps trying
        if domain.certificate_ready():
            return True, f"trusted certificate for {s.domain.address}"
        return False, f"no trusted certificate for {s.domain.address} yet: {_last_line(domain.ip_certificate_error())}"
    address = domain.public_ip()
    if not _public(address):
        return False, "this server has no public IP address (none seen from outside)"
    if not _port_free(80):
        return False, "port 80 is in use on this server; the IP certificate needs it"
    settings.update("domain", {**s.domain.model_dump(), "address": address}, by="install.sh")
    deadline = clock() + timeout
    while host_requests.requested("tls") or host_requests.applying("tls"):
        if clock() > deadline:
            return False, f"no certificate for {address} within {int(timeout)} s; the renewal keeps trying"
        sleep(2)
    if domain.certificate_ready():
        return True, f"trusted certificate for {address} (Let's Encrypt)"
    return False, f"no trusted certificate for {address}: {_last_line(domain.ip_certificate_error())}"


def _last_line(error: str) -> str:
    lines = [line.strip() for line in error.splitlines() if line.strip()]
    return lines[-1] if lines else "inbound TCP port 80 must reach this server, in any cloud firewall too"


def main(argv=None) -> int:
    """python -m sbx_control.setup onboarding | first-certificate (install.sh, as root)."""
    import argparse
    import shlex
    parser = argparse.ArgumentParser(prog="python -m sbx_control.setup")
    parser.add_argument("command", choices=["onboarding", "first-certificate"])
    parser.add_argument("--timeout", type=int, default=IP_CERTIFICATE_SECONDS)
    a = parser.parse_args(argv)
    if a.command == "first-certificate":
        if complete():   # an upgrade of a server that is set up: nothing to say
            return 0
        trusted, said = first_certificate(a.timeout)
        print(f"{'ok' if trusted else 'self-signed for now'}: {said}")
        return 0
    try:
        found = onboarding()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 3
    print(f"link={shlex.quote(found['link'])}\ntrusted={'1' if found['trusted'] else ''}\n"
          f"domain={shlex.quote(found['domain'])}")
    return 0


def host_facts() -> dict:
    import os
    facts = {"kvm": os.path.exists("/dev/kvm")}
    try:
        with open("/proc/meminfo") as f:
            facts["memory_gb"] = round(int(f.readline().split()[1]) / 1024 / 1024)
    except (OSError, ValueError, IndexError):
        pass
    disk = runtime.disk()
    if disk:
        facts["disk_free_gb"] = disk["free_gb"]
    else:   # no runtime keeps its disk here: the root filesystem's, as install.sh measures it
        import shutil
        try:
            facts["disk_free_gb"] = shutil.disk_usage("/").free // 1024 ** 3
        except OSError:
            pass
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
    """The setup's checks. The reachability check starts a sandbox from the
    base template, so a base not built yet (its address or runtime just
    chosen) starts building now rather than on the scheduler's next round."""
    from . import template_scheduler
    from .connections import runtime as runtime_connection
    template_scheduler.kick()
    checks = [*runtime.readiness(host_facts()), *runtime_connection.Runtime().test()]
    return {"ok": all(c.ok for c in checks), "checks": [c.__dict__ for c in checks]}


# What finishing says while a step is not done, in the onboarding page's order.
NEEDED = {"sign_in": "Create the admin account first.",
          "address": "Choose the server address first.",
          "runtime": "Add a sandbox runtime first: sandboxes need one to run on."}


def missing(found: dict) -> str:
    """Why state() can't finish yet ("" when every required step is done): the first not done, in NEEDED's
    order (choosing coding agents is optional)."""
    undone = {step["step"] for step in found["steps"] if not step["done"]}
    return next((message for step, message in NEEDED.items() if step in undone), "")


def state(email: str | None, address: str = "") -> dict:
    from . import connections
    from .connections import domain as domain_connection, sign_in as sign_in_connection
    s = settings.current()
    domain = connections.get("domain").status()
    sign_in = connections.get("sign_in").status()
    kinds = runtime.enabled()
    steps = [{"step": "address", "done": address_ready(), "optional": False, "summary": domain.summary},
             {"step": "sign_in", "done": sign_in_connection.ready(),
              "optional": False, "summary": sign_in.summary},
             {"step": "runtime", "done": bool(kinds), "optional": False,
              "summary": ", ".join(runtime.title(k) for k in kinds) if kinds else runtime.NO_RUNTIME},
             {"step": "agents", "done": agents_chosen(), "optional": True, "summary": _agents_summary()}]
    signed_in = bool(email) and email != "anonymous"
    can_complete = all(step["done"] or step["optional"] for step in steps) and (signed_in or s.sign_in.method == "none")
    server_address = s.domain.address or ip_address(address) or domain_connection.public_ip()
    return {"complete": complete(), "steps": steps, "signed_in": signed_in, "email": email,
            "open_access": s.sign_in.method == "none",
            "can_complete": can_complete, "host": {**host_facts(), "address": server_address},
            "access": {"mode": "domain" if s.domain.name else "ip", "domain": s.domain.name,
                       "url": s.base_url, "certificate": s.domain.acme,
                       "certificate_email": s.domain.acme_email, "certificate_ready": address_ready(),
                       "needs_ports": list(s.domain.needs_ports()),
                       "certificate_error": _certificate_error(s.domain)},
            "link": link(address=server_address),
            "admins": list(s.access.admins), "version": version(),
            "agents": [{"kind": c.kind, "title": c.title, "chosen": c.kind in (AGENTS.get() or [])}
                       for c in offered_agents()]}


def _certificate_error(d) -> str:
    """Why the address's certificate is not there yet, from its last failed
    issuance ("" when none failed, or once it is there): the IP's, or the
    domain's (the port 53 probe's finding or Let's Encrypt's error)."""
    from .connections.domain import certificate_error, ip_certificate_error
    if d.name:
        return "" if d.acme == "none" or address_ready() else certificate_error()
    return ip_certificate_error() if d.address else ""


def _agents_summary() -> str:
    chosen = AGENTS.get()
    if chosen is None:
        return "as enabled under Extensions" if complete() else "optional: none chosen"
    titles = {c.kind: c.title for c in offered_agents()}
    return ", ".join(titles.get(k, k) for k in chosen) or "none"


def first_admin(email: str):
    s = settings.current()
    if not s.access.admins and not complete():
        settings.update("access", {**s.access.model_dump(), "admins": [email.lower()]}, by=f"setup ({email})")


def version() -> str:
    try:   # the release this runs from (control/release.sh writes it)
        return (pathlib.Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
    except OSError:
        return "dev"


if __name__ == "__main__":
    raise SystemExit(main())
