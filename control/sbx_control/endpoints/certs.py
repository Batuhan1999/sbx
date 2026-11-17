"""The endpoints' CAs and the sandboxes' client certificates: an endpoint's
CA is made with it (its key sealed in the secret store), a sandbox's
certificate for it lasts CERT_HOURS; which sandbox holds certificates for
which endpoints, and the credential its forwarder renews them with."""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import time

from .. import secret_store, state

CERT_HOURS = 24
RENEW_HOURS = 12                        # the keeper renews a running sandbox's certificates with less than this left


def _pem(cert, key) -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


def _usage(*, sign: bool):
    from cryptography import x509
    return x509.KeyUsage(digital_signature=not sign, content_commitment=False, key_encipherment=False,
                         data_encipherment=False, key_agreement=False, key_cert_sign=sign, crl_sign=sign,
                         encipher_only=False, decipher_only=False)


def make_ca(endpoint: str) -> tuple[str, str]:
    """(a new CA's certificate, its key) for the endpoint."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         f"sbx endpoint {endpoint} CA {state.installation_id()[:8]}")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(_usage(sign=True), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return _pem(cert, key)


def _ca_of(endpoint: str) -> tuple[str, str]:
    with state.db() as conn:
        found = conn.execute("select ca_cert, ca_key from endpoints where name = ?", (endpoint,)).fetchone()
    if not found:
        raise LookupError(f"no endpoint {endpoint}")
    return found[0], secret_store.unseal(found[1])["key"]


def issue(sandbox: str, endpoint: str, hours: float = CERT_HOURS) -> tuple[str, str, float]:
    """(a client certificate for the sandbox at the endpoint, its key, when it
    runs out as a timestamp): from that endpoint's CA, for client
    authentication only."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    ca_pem, ca_key_pem = _ca_of(endpoint)
    authority = x509.load_pem_x509_certificate(ca_pem.encode())
    signer = serialization.load_pem_private_key(ca_key_pem.encode(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    until = now + datetime.timedelta(hours=hours)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"sbx sandbox {sandbox}")]))
            .issuer_name(authority.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(until)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(_usage(sign=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
            .sign(signer, hashes.SHA256()))
    return (*_pem(cert, key), until.timestamp())


def issue_all(sandbox: str, repo: str, endpoints: list[str]) -> dict:
    """endpoint name -> (certificate, key, not_after) for each, recorded for the sandbox."""
    issued = {name: issue(sandbox, name) for name in endpoints}
    record(sandbox, repo, sorted(issued), min((v[2] for v in issued.values()), default=0))
    return issued


# What the sandboxes hold


def record(sandbox: str, repo: str, endpoints: list, not_after: float):
    with state.db() as conn:
        conn.execute("insert into endpoint_certs (sandbox, repo, endpoints, not_after) values (?, ?, ?, ?) "
                     "on conflict (sandbox) do update set repo = excluded.repo, endpoints = excluded.endpoints, "
                     "not_after = excluded.not_after", (sandbox, (repo or "").lower(), json.dumps(endpoints), not_after))


def forget(sandbox: str):
    """It holds none now and has no credential (it may use no endpoint, or it is gone)."""
    with state.db() as conn:
        conn.execute("delete from endpoint_certs where sandbox = ?", (sandbox,))
        conn.execute("delete from endpoint_credentials where sandbox = ?", (sandbox,))


def holders() -> set:
    """The registered sandboxes that hold endpoint certificates (have a forwarder config)."""
    with state.db() as conn:
        return {r[0] for r in conn.execute("select c.sandbox from endpoint_certs c join sandboxes s "
                                           "on s.name = c.sandbox")}


def due(now: float | None = None) -> list[str]:
    """The registered sandboxes whose certificates run out within RENEW_HOURS."""
    limit = (now or time.time()) + RENEW_HOURS * 3600
    with state.db() as conn:
        return [r[0] for r in conn.execute("select c.sandbox from endpoint_certs c join sandboxes s "
                                           "on s.name = c.sandbox where c.not_after < ? order by c.sandbox",
                                           (limit,))]


def used_by() -> dict:
    """endpoint name -> the repos whose registered sandboxes hold a certificate for it."""
    out: dict = {}
    with state.db() as conn:
        found = conn.execute("select c.repo, c.endpoints from endpoint_certs c "
                             "join sandboxes s on s.name = c.sandbox").fetchall()
    for repo, names in found:
        for name in json.loads(names or "[]"):
            repos = out.setdefault(name, [])
            if repo and repo not in repos:
                repos.append(repo)
    return {k: sorted(v) for k, v in out.items()}


# The forwarder's credential: random, made with each runtime write and bound
# to the sandbox's repo, so a sandbox made again under the name, or for
# another repo, has another; only its digest is kept.


def _digest(credential: str) -> str:
    return hashlib.sha256(credential.encode()).hexdigest()


def bind(sandbox: str, repo: str, credential: str):
    """The credential the sandbox's runtime now holds (after its write)."""
    with state.db() as conn:
        conn.execute("insert into endpoint_credentials (sandbox, digest, repo) values (?, ?, ?) on conflict (sandbox) "
                     "do update set digest = excluded.digest, repo = excluded.repo",
                     (sandbox, _digest(credential), (repo or "").lower()))


def bound_repo(sandbox: str) -> str | None:
    with state.db() as conn:
        found = conn.execute("select repo from endpoint_credentials where sandbox = ?", (sandbox,)).fetchone()
    return found[0] if found else None


def sandbox_of(authorization: str) -> str | None:
    """The sandbox a callback's `Bearer NAME:CREDENTIAL` proves (its latest credential), or None."""
    name, _, credential = authorization.removeprefix("Bearer ").partition(":")
    if not name or not credential:
        return None
    with state.db() as conn:
        found = conn.execute("select digest from endpoint_credentials where sandbox = ?", (name,)).fetchone()
    if not found or not hmac.compare_digest(found[0], _digest(credential)):
        return None
    return name
