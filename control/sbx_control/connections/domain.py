"""The domain and its wildcard certificate: the names, how the DNS-01
challenge is answered, and the certificate nginx serves."""
from __future__ import annotations

import datetime
import ipaddress
import pathlib
import secrets as pysecrets
import socket
import subprocess
import urllib.request

from .. import acme_dns, howto, paths, settings, vault
from .base import Check, Connection, Field, Refused, Status



ACME_OPTIONS = (("delegated", "DNS delegation (no API credentials)"), ("cloudflare", "Cloudflare API"),
                ("route53", "Amazon Route 53"), ("none", "None (self-signed)"))


def live() -> pathlib.Path:
    """Where certbot keeps the certificates tls.sh serves."""
    return paths.tls() / "letsencrypt" / "live"


class Domain(Connection):
    kind = "domain"
    title = "Domain & certificate"

    def status(self) -> Status:
        s = settings.current().domain
        expiry = _expiry(s.name)
        facts: dict = {"login": f"{s.login_host}.{s.name}" if s.name else "", "acme": s.acme}
        if not s.name:
            ip_expiry = _ip_expiry(s.address)
            if not s.address:
                state, summary = "not_connected", "No address yet"
            elif ip_expiry and ip_expiry > datetime.datetime.now(datetime.timezone.utc):
                error = ip_certificate_error()
                state = "connected" if not error and (ip_expiry - datetime.datetime.now(datetime.timezone.utc)).total_seconds() > 86400 else "attention"
                summary = f"{s.address} · certificate valid until {ip_expiry:%Y-%m-%d}"
                if error:
                    summary += " · renewal needs attention"
                    facts["certificate_error"] = error
                facts["certificate_expires"] = ip_expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                state, summary = "attention", f"{s.address} · waiting for a trusted IP certificate"
            facts["login"] = s.address
        elif expiry is None:
            state, summary = "attention", f"{s.name} · no certificate yet"
            if error := certificate_error():
                facts["certificate_error"] = error
        else:
            days = (expiry - datetime.datetime.now(datetime.timezone.utc)).days
            facts["certificate_expires"] = expiry.strftime("%Y-%m-%d")
            state = "connected" if days > 14 else "attention"
            summary = f"{s.name} · certificate valid until {expiry:%Y-%m-%d}"
        aws = vault.facts(vault.AWS_ACCESS_KEY_ID)
        cf = vault.facts(vault.CLOUDFLARE_API_TOKEN)
        route53 = (("acme", ("route53",)),)
        fields = (Field("name", "Domain", value=s.name, kind="hostname", placeholder="sbx.example.com"),
                  Field("acme_email", "Certificate email", value=s.acme_email, kind="email", optional=True,
                        help="The Let's Encrypt account's contact; it sends no expiry emails.", advanced=True),
                  Field("acme", "Certificate verification", value=s.acme, kind="choice", options=ACME_OPTIONS),
                  Field("cloudflare_api_token", "Cloudflare API token", secret=True, set=cf.set,
                        when=(("acme", ("cloudflare",)),)),
                  Field("aws_access_key_id", "AWS access key ID", value="" if not aws.set else "(set)", optional=True,
                        help="Empty uses the server's instance role.", when=route53),
                  Field("aws_secret_access_key", "AWS secret access key", secret=True, optional=True,
                        set=vault.facts(vault.AWS_SECRET_ACCESS_KEY).set, when=route53))
        return Status(self.kind, self.title, state, summary, facts, fields, ("connect", "test"))

    def test(self, values: dict | None = None) -> list[Check]:
        s = settings.current().domain
        v = values or {}
        name = (v.get("name") or s.name).strip().lower()
        if not name:
            return [Check("domain", False, "none given", "")]
        checks = []
        login = f"{s.login_host}.{name}"
        probe = f"sbx-check-{pysecrets.token_hex(3)}.{name}"
        login_ips, probe_ips = _resolve(login), _resolve(probe)
        checks.append(Check(f"{login} resolves", bool(login_ips), ", ".join(login_ips) or "no address",
                            f"an A record for {login} (or *.{name}) pointing at this host", help_url=howto.DNS_RECORDS))
        checks.append(Check(f"*.{name} resolves", bool(probe_ips), ", ".join(probe_ips) or "no address",
                            f"a wildcard A record *.{name} pointing at this host", help_url=howto.DNS_RECORDS))
        mine = _host_ips()
        if mine and login_ips:
            hit = sorted(set(login_ips) & mine)
            checks.append(Check("points at this host", bool(hit), f"{', '.join(hit)} is this host" if hit else
                                f"this host is {', '.join(sorted(mine))}", "the records should point at this host's public address",
                                help_url=howto.DNS_RECORDS))
        expiry = _expiry(name)
        mode = v.get("acme") or s.acme
        checks.append(Check("certificate", expiry is not None and expiry > datetime.datetime.now(datetime.timezone.utc),
                            f"valid until {expiry:%Y-%m-%d}" if expiry else "none issued",
                            "tls.sh issues it (sudo sbx-apply tls) once the records resolve",
                            help_url={"delegated": howto.DELEGATED_DNS, "cloudflare": howto.CLOUDFLARE_TOKEN}.get(mode, "")))
        challenge = f"_acme-challenge.{name}"
        ns = _ns(challenge) if mode != "none" else []
        if mode == "delegated":
            ok = any(x.rstrip(".") == name for x in ns)
            checks.append(Check("delegation", ok, f"NS {', '.join(ns) or 'none'}" if ns else "no NS record yet",
                                f"an NS record: {challenge} NS {name}.", help_url=howto.DELEGATED_DNS))
        if mode != "none":
            checks.append(_stray_records(challenge, name, mode, ns))
        if mode == "delegated" and s.name == name and s.acme == "delegated":
            # Only once saved: before, the responder does not answer for the
            # name and the firewall keeps 53 closed (tls.sh probes then).
            ok, detail, fix = acme_dns.probe()
            checks.append(Check("port 53", ok, detail, fix, help_url=howto.DELEGATED_DNS))
        if mode == "cloudflare":
            has = bool(v.get("cloudflare_api_token")) or vault.facts(vault.CLOUDFLARE_API_TOKEN).set
            checks.append(Check("Cloudflare token", has, "set" if has else "missing", "a token that may edit the zone's DNS",
                                help_url=howto.CLOUDFLARE_TOKEN))
        return checks

    def connect(self, values: dict, *, by: str) -> Status:
        s = settings.current().domain
        new = {"name": (values.get("name") or s.name).strip().lower(),
               "address": values.get("address") or s.address,
               "acme_email": (values.get("acme_email") or s.acme_email).strip(),
               "acme": values.get("acme") or s.acme}
        if not new["name"]:
            raise Refused("a domain is needed")
        try:
            new = settings.Domain.model_validate(new).model_dump()
        except ValueError as error:
            raise Refused(str(error))
        checks = [c for c in self.test({**values, **new}) if c.name.endswith("resolves") or
                  c.name in ("points at this host", "delegation", STRAY, "port 53", "Cloudflare token")]
        if not all(c.ok for c in checks):
            raise Refused("Check the DNS records and certificate settings below.", checks)
        for field, key in (("aws_access_key_id", vault.AWS_ACCESS_KEY_ID),
                           ("aws_secret_access_key", vault.AWS_SECRET_ACCESS_KEY),
                           ("cloudflare_api_token", vault.CLOUDFLARE_API_TOKEN)):
            if values.get(field):
                vault.put(key, values[field].strip(), by=by)
        before = settings.current()
        after = settings.update("domain", new, by=by)
        if new["acme"] != "none" and _expiry(new["name"]) is None:
            # A new attempt: the last one's error no longer applies, and
            # saving the same values again (Retry) asks for issuance again.
            certificate_error_file().unlink(missing_ok=True)
            if after.revision == before.revision:
                settings.request_effects(settings.HostUnit.TLS)
        return self.status()


STRAY = "other _acme-challenge records"


def _stray_records(challenge: str, name: str, mode: str, ns: list[str]) -> Check:
    """Records at _acme-challenge.<name> that send Let's Encrypt elsewhere: a
    CNAME, or an NS other than the delegation to this host (delegated);
    with a DNS API (cloudflare, route53) any NS or CNAME, since sbx writes
    the TXT record in the zone itself."""
    stray = [f"CNAME {target}" for target in _cname(challenge)]
    stray += [f"NS {target}" for target in ns if mode != "delegated" or target.rstrip(".") != name]
    if not stray:
        return Check(STRAY, True, "none")
    keep = (f"; keep only {challenge} NS {name}." if mode == "delegated" else
            f": with {mode}, sbx writes the TXT record in the zone itself")
    return Check(STRAY, False, f"{challenge} {', '.join(stray)}",
                 f"remove {'it' if len(stray) == 1 else 'them'} at your DNS provider{keep}",
                 help_url=howto.DELEGATED_DNS if mode == "delegated" else howto.DNS_RECORDS)


PUBLIC_RESOLVER = "1.1.1.1"  # what the world sees, not a cloud's private zone for the same names


def _dig(kind: str, name: str) -> list[str]:
    try:  # a delegation shows in the authority section until the child answers itself
        out = subprocess.run(["dig", f"@{PUBLIC_RESOLVER}", "+time=4", "+tries=1", "+noall", "+answer", "+authority", kind, name],
                             capture_output=True, text=True, timeout=10).stdout
        return sorted({line.split()[-1] for line in out.splitlines() if f"\t{kind}\t" in line or f" {kind} " in line})
    except (OSError, subprocess.SubprocessError):
        return []


def _ns(name: str) -> list[str]:
    return _dig("NS", name)


def _cname(name: str) -> list[str]:
    return _dig("CNAME", name)


def _resolve(name: str) -> list[str]:
    found = _dig("A", name)
    if found:
        return found
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)})
    except socket.gaierror:
        return []


def public_ip() -> str:
    """This host's address as the internet sees it, or "" offline."""
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=5) as r:
            return r.read().decode().strip()
    except Exception:
        return ""


def _host_ips() -> set:
    """The host's public address and its primary interface's (inside a cloud
    network the public name resolves to the private address)."""
    return {public_ip(), acme_dns.primary_address()} - {""}


def certificate_ready() -> bool:
    """Whether the certificate the installation answers with is valid now:
    the domain's, or before there is one its address's."""
    d = settings.current().domain
    if not (d.name or d.address):
        return False
    expiry = _expiry(d.name) if d.name else _ip_expiry(d.address)
    return expiry is not None and expiry > datetime.datetime.now(datetime.timezone.utc)


def _expiry(name: str):
    path = live() / name / "fullchain.pem"
    if not name or not path.exists():
        return None
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    return cert.not_valid_after_utc


def _ip_expiry(address: str):
    """Only report an IP certificate after the renewal service loaded it in nginx."""
    if not address:
        return None
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    try:
        cert = x509.load_pem_x509_certificate((live() / address / "fullchain.pem").read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        active = (paths.tls() / "ip-certificate.fingerprint").read_text().split("=")[-1].replace(":", "").strip().lower()
        if cert.issuer == cert.subject or ipaddress.ip_address(address) not in san.get_values_for_type(x509.IPAddress):
            return None
        if active != cert.fingerprint(hashes.SHA256()).hex():
            return None
        return cert.not_valid_after_utc
    except (OSError, ValueError, x509.ExtensionNotFound):
        return None


def ip_certificate_error() -> str:
    try:
        return (paths.tls() / "ip-certificate-error").read_text()[-2000:].strip()
    except OSError:
        return ""


def certificate_error_file() -> pathlib.Path:
    """Why the domain's last issuance failed (tls.sh's renewal script writes
    it: the port 53 probe's finding or certbot's last lines)."""
    return paths.tls() / "certificate-error"


def certificate_error() -> str:
    try:
        return certificate_error_file().read_text()[-2000:].strip()
    except OSError:
        return ""
