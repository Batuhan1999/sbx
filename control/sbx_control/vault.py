"""Secrets of the installation itself: the platform's (sign-in, deploy key,
DNS provider credentials), any an extension keeps in a namespace of its own
(Key("ENVIRONMENT_SECRET", "claude")), and the runtime's credentials in
runtime.env. The `sealed` table holds each one's facts and a reference into
the secret store; what the store itself needs to be reached is kept local."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import secrets as secrets_module
import shlex

from . import paths, secret_store, settings, state
from .settings import HostUnit


@dataclasses.dataclass(frozen=True)
class Key:
    name: str
    namespace: str = "platform"      # platform, extra (any other name), or an extension's
    applies: tuple[str, ...] = ()    # host effects a change asks for

    @property
    def row(self) -> str:
        return self.name if self.namespace == "platform" else f"{self.namespace}:{self.name}"

    @property
    def bootstrap(self) -> bool:
        return self.namespace == "platform" and self.name in BOOTSTRAP


LOGIN_PASSWORD = Key("LOGIN_PASSWORD")
OIDC_CLIENT_SECRET = Key("OIDC_CLIENT_SECRET")
GITHUB_DEPLOY_KEY = Key("GITHUB_DEPLOY_KEY")
AWS_ACCESS_KEY_ID = Key("AWS_ACCESS_KEY_ID", applies=(HostUnit.TLS,))       # route53 DNS-01 (tls.sh)
AWS_SECRET_ACCESS_KEY = Key("AWS_SECRET_ACCESS_KEY", applies=(HostUnit.TLS,))
CLOUDFLARE_API_TOKEN = Key("CLOUDFLARE_API_TOKEN", applies=(HostUnit.TLS,))
INFISICAL_CLIENT_ID = Key("INFISICAL_CLIENT_ID")
INFISICAL_CLIENT_SECRET = Key("INFISICAL_CLIENT_SECRET")
INFISICAL_WEBHOOK_TOKEN = Key("INFISICAL_WEBHOOK_TOKEN")
PLATFORM = (LOGIN_PASSWORD, OIDC_CLIENT_SECRET, GITHUB_DEPLOY_KEY, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
            CLOUDFLARE_API_TOKEN, INFISICAL_CLIENT_ID, INFISICAL_CLIENT_SECRET, INFISICAL_WEBHOOK_TOKEN)
BOOTSTRAP = {"INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET", "INFISICAL_WEBHOOK_TOKEN"}
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def key(text: str) -> Key:
    """A platform key by name, `<namespace>:NAME` for an extension's, else an extra one."""
    namespace, sep, name = text.partition(":")
    if sep:
        return Key(name, namespace)
    return next((k for k in PLATFORM if k.name == text), Key(text, "extra"))


@dataclasses.dataclass(frozen=True)
class Facts:
    name: str
    set: bool
    updated_by: str | None = None
    updated_at: str | None = None
    fingerprint: str | None = None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:6]


def put(k: Key, value: str, *, by: str) -> Facts:
    if not NAME_RE.match(k.name):
        raise ValueError(f"{k.name} is not a secret name")
    if k.bootstrap:
        secret_store.local().put(secret_store.BOOTSTRAP, k.name, value)
        reference = secret_store.local().seal(json.dumps(value), k.row)
    else:
        reference = secret_store.seal(value, key=f"platform/{k.row}")
    with state.db() as conn:
        old = conn.execute("select value from sealed where key = ?", (k.row,)).fetchone()
        conn.execute("insert into sealed (key, value, updated_by) values (?, ?, ?) on conflict (key) do update set "
                     "value = excluded.value, updated_by = excluded.updated_by, updated_at = current_timestamp",
                     (k.row, reference, by))
    if old and old[0] != reference:
        secret_store.discard(old[0])
    settings.request_effects(*k.applies)
    return facts(k)


def get(k: Key) -> str | None:
    """Server-side only (connections, the shell CLI, platform_secrets()). Never from a router."""
    if k.bootstrap:
        return secret_store.local().get(secret_store.BOOTSTRAP, k.name)
    with state.db() as conn:
        row = conn.execute("select value from sealed where key = ?", (k.row,)).fetchone()
    return secret_store.unseal(row[0]) if row else None


def delete(k: Key, *, by: str) -> bool:
    if k.bootstrap:
        secret_store.local().delete(secret_store.BOOTSTRAP, k.name)
    with state.db() as conn:
        gone = secret_store.delete_rows(conn, "sealed", "key = ?", (k.row,)) > 0
    if gone:
        settings.request_effects(*k.applies)
    return gone


def facts(k: Key) -> Facts:
    """Whether k is set, from where get() reads it: a bootstrap key is set by
    its local value alone (the Infisical setup writes only that)."""
    with state.db() as conn:
        row = conn.execute("select updated_by, updated_at from sealed where key = ?", (k.row,)).fetchone()
    value = get(k) if row or k.bootstrap else None
    if value is None:
        return Facts(k.name, False)
    return Facts(k.name, True, *(row or (None, None)), _fingerprint(value))


def rows() -> list[str]:
    """Every sealed secret by its row: platform names, extra:NAME, and each
    extension's <namespace>:NAME."""
    with state.db() as conn:
        return sorted(r[0] for r in conn.execute("select key from sealed"))


def extras() -> list[str]:
    """Secrets set by name with no platform key (sbx-admin secret set), for ${secret:NAME}."""
    with state.db() as conn:
        return sorted(r[0][6:] for r in conn.execute("select key from sealed where key like 'extra:%'"))


def _runtime_file(keys=None) -> dict:
    """runtime.env as written: plain values, or references into the store,
    of the keys the enabled runtimes read (Runtime.credential_keys)."""
    from . import runtime as sandbox_runtime
    keys = sandbox_runtime.credential_keys() if keys is None else keys
    file = paths.runtime_env()
    out = {}
    for line in file.read_text().splitlines() if file.exists() else ():
        name, sep, value = line.partition("=")
        if not sep or line.lstrip().startswith("#") or name not in keys:
            continue
        out[name] = shlex.split(value)[0] if value.strip() else ""
    return out


def runtime(keys=None) -> dict:
    """This host's sandbox runtime credentials (runtime.env, written by
    install.sh): the enabled runtimes' keys, or these; a value may be a
    reference into the store."""
    return {name: secret_store.unseal(value) if secret_store.is_reference(value) else value
            for name, value in _runtime_file(keys).items()}


def runtime_sealed() -> bool:
    """Whether runtime.env points into the store (store enable sealed it)."""
    return any(secret_store.is_reference(value) for value in _runtime_file().values())


def seal_runtime_file() -> int:
    """Each enabled runtime's secret (Runtime.secret_key) as a reference into
    the store, once that is Infisical (store enable; sbx-admin runtime add
    and install.sh when it is already): the lines that still hold a plain
    value are rewritten in place; every other line of runtime.env (comments,
    the runtimes' selection, other keys, secrets sealed already) stays as it
    is. 1 when it sealed any."""
    from . import runtime as sandbox_runtime
    raw, found = _runtime_file(), runtime()
    secrets = {b.secret_key for b in sandbox_runtime.backends()
               if found.get(b.secret_key) and not secret_store.is_reference(raw.get(b.secret_key, ""))}
    if not secrets:
        return 0
    # Each value under a key of its own: an upgrade's backup of runtime.env
    # (rollback.sh) keeps naming the object the value before is in.
    edit_runtime_file({s: secret_store.seal(found[s], key=f"runtime/{s}/{secrets_module.token_hex(6)}")
                       for s in secrets})
    return 1


def sealing() -> bool:
    """Whether runtime.env's secrets go into the store: once it is Infisical
    (the local store's values are no references runtime() could follow)."""
    return settings.current().store.mode == "infisical"


def edit_runtime_file(values: dict) -> None:
    """Set these keys of runtime.env (None: remove its line) in place; one
    it does not have yet is added at the end. Every other line stays as it
    is, and the file stays root-only."""
    file = paths.runtime_env()
    out, done = [], set()
    for line in file.read_text().splitlines() if file.exists() else ():
        name, sep, _ = line.partition("=")
        name = name.strip()
        if sep and not line.lstrip().startswith("#") and name in values:
            if name not in done and values[name] is not None:
                out.append(f"{name}={shlex.quote(values[name])}")
            done.add(name)
            continue
        out.append(line)
    out += [f"{name}={shlex.quote(value)}" for name, value in values.items() if name not in done and value is not None]
    temporary = file.with_suffix(".new")
    temporary.touch(mode=0o600)
    temporary.chmod(0o600)
    temporary.write_text("\n".join(out) + "\n")
    temporary.replace(file)


def platform_secrets() -> dict:
    """Every platform value under its env name, for the shell scripts and
    ${secret:NAME} in installation variables: the runtime's, the platform's
    and extra ones, and the sign-in settings. An extension's are its own."""
    out = runtime()
    with state.db() as conn:
        rows = conn.execute("select key, value from sealed").fetchall()
    for row, _ in rows:
        namespace, sep, name = row.partition(":")
        if not sep:
            out[row] = get(Key(row))
        elif namespace == "extra":
            out[name] = get(Key(name, "extra"))
    s = settings.current().sign_in
    if s.oidc_issuer:
        out["OIDC_ISSUER"], out["OIDC_CLIENT_ID"] = s.oidc_issuer, s.oidc_client_id
    return {k: v for k, v in out.items() if v is not None}


SCHEMA = state.Schema(tables="""
create table if not exists sealed (
    key text primary key, value text not null, updated_by text not null,
    updated_at text not null default current_timestamp);
""", sealed=(("sealed", "value"),))
