"""The env variables people set for a repo's sandboxes, and the values a
sandbox generates for itself (${random:N}).

A variable's scope is the installation (""), a repo ("repo:owner/repo") or
one person's own in a repo ("repo:owner/repo|user:<email>"), which goes
over the repo's in their sandboxes only. Each is a secret (write-only:
never shown or sent back) or plain; goes to all of the repo's sandboxes or
to its PR previews or its agent sessions only (audience); and agents may
read it or only the app gets it (agent). The `secrets` table holds those
facts; the values are in the secret store, a namespace per scope. A value
set in the store directly (Infisical's UI) counts as a secret for all."""
from __future__ import annotations

import base64
import fnmatch
import re
import secrets

from . import delivery, secret_store, settings, state, vault
from .secret_store import parse_scope, scope_of

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
URL_PASSWORD = re.compile(r"(://[^:/\s@]+:)[^@\s/]+@")
AUDIENCES = {"all": "all sandboxes", "previews": "PR previews", "sessions": "agent sessions"}


def installation_layers(repo: str) -> list[dict]:
    """The installation's variables (settings variables.by_pattern) that
    apply to the repo, lowest first: those for "*", then each pattern that
    matches it."""
    by_pattern = settings.current().variables.by_pattern
    return [by_pattern.get("*") or {}] + [values or {} for pattern, values in by_pattern.items()
                                          if pattern != "*" and fnmatch.fnmatchcase(repo, pattern.lower())]


def people(repo: str) -> list:
    with state.db() as conn:
        scopes = [r[0] for r in conn.execute("select distinct scope from secrets where scope like ?",
                                               (scope_of(repo, "%"),))]
    return [parse_scope(scope)[1] for scope in scopes]


def repos() -> set:
    """The repos some value is set for (theirs, or a person's own there)."""
    with state.db() as conn:
        scopes = [row[0] for row in conn.execute("select distinct scope from secrets")]
    return {found[0] for found in map(parse_scope, scopes) if found}


def _personal(scope: str) -> bool:
    return bool((parse_scope(scope) or ("", ""))[1])


def has_values() -> bool:
    with state.db() as conn:
        return conn.execute("select 1 from secrets limit 1").fetchone() is not None


@settings.check("store")
def _keep_values(before, after):
    if secret_store.moves(before, after) and has_values():
        raise ValueError("This Infisical store holds env values; restore or move them before changing it")


def put(repo: str | None, name: str, value: str | None, who: str, secret: bool = True,
        audience: str = "all", agent: bool = True, person: str | None = None):
    """value None keeps the one set before (to change only how it is given)."""
    if not NAME_RE.match(name):
        raise ValueError(f"{name} is not a variable name")
    if audience not in AUDIENCES:
        raise ValueError(f"who gets it: one of {', '.join(AUDIENCES)}")
    scope, store = scope_of(repo, person), secret_store.store()
    if value is None:
        with state.db() as conn:
            known = conn.execute("select 1 from secrets where scope = ? and name = ?", (scope, name)).fetchone()
        if not known and store.get(scope, name) is None:
            raise ValueError(f"{name} has no value yet")
    else:
        store.put(scope, name, value)
    with state.db() as conn:
        conn.execute("insert into secrets (scope, name, updated_by, secret, audience, agent) values (?, ?, ?, ?, ?, ?) "
                     "on conflict (scope, name) do update set updated_by = excluded.updated_by, secret = excluded.secret, "
                     "audience = excluded.audience, agent = excluded.agent, updated_at = current_timestamp",
                     (scope, name, who, int(secret), audience, int(agent)))


def delete(repo: str | None, name: str, person: str | None = None) -> bool:
    scope = scope_of(repo, person)
    gone = secret_store.store().delete(scope, name)
    with state.db() as conn:
        return conn.execute("delete from secrets where scope = ? and name = ?", (scope, name)).rowcount > 0 or gone


def names(repo: str | None) -> list:
    """Names and who set them when; never values."""
    scope = scope_of(repo)
    with state.db() as conn:
        rows = conn.execute("select name, updated_by, updated_at from secrets where scope = ? order by name",
                            (scope,)).fetchall()
    found = [{"name": n, "updated_by": by, "updated_at": at} for n, by, at in rows]
    have = {f["name"] for f in found}
    found += [{"name": n, "updated_by": "the store", "updated_at": ""}
              for n in sorted(secret_store.store().list(scope)) if n not in have]
    return sorted(found, key=lambda f: f["name"])


def _rows(repo: str | None, person: str | None) -> list:
    """(scope, name, value, secret, audience, agent, updated_by, updated_at)
    of the repo's values and the person's own; a value whose facts are gone
    or that the store lost is left out, one set in the store alone added."""
    scopes = [scope_of(repo)] + ([scope_of(repo, person)] if person and repo else [])
    store = secret_store.store()
    held = {scope: store.list(scope) for scope in scopes}
    with state.db() as conn:
        rows = conn.execute(f"select scope, name, secret, audience, agent, updated_by, updated_at from secrets "
                            f"where scope in ({','.join('?' * len(scopes))}) order by name", scopes).fetchall()
    out = [(scope, name, held[scope][name], secret, audience, agent, by, at)
           for scope, name, secret, audience, agent, by, at in rows if name in held[scope]]
    named = {(r[0], r[1]) for r in out}
    out += [(scope, name, value, 1, "all", 0, "the store", "")
            for scope, values in held.items() for name, value in sorted(values.items()) if (scope, name) not in named]
    return out


def entries(repo: str, person: str | None = None) -> list:
    """The repo's values, and one person's own: settings and who set them
    when, and the value only where it is not a secret."""
    return [{"name": name, "secret": bool(secret), "audience": audience, "agent": bool(agent),
             "mine": _personal(scope), "updated_by": by, "updated_at": at, **({} if secret else {"value": value})}
            for scope, name, value, secret, audience, agent, by, at in sorted(_rows(repo, person), key=lambda r: r[1])]


def values(repo: str | None) -> dict:
    """The repo's values by name (the installation's when repo is None)."""
    return {name: value for scope, name, value, *_ in _rows(repo, None)}


def granted_secrets(repo: str, owner: str, kind: str, referenced: set) -> list:
    """(name, value) of each secret a trusted repo's sandbox of kind may
    hold, each kept where one name shadows another: the repo's and its
    owner's own for every sandbox or kind, the repo's named in its env
    (${secret:NAME}), and the installation's named there that the repo has
    no value of that name for. None of the installation's others."""
    rows = _rows(repo, owner or None)
    out = [(name, value) for scope, name, value, secret, audience, *_ in rows
           if secret and isinstance(value, str)
           and (audience in ("all", kind) or (name in referenced and not _personal(scope)))]
    repos = {name for scope, name, *_ in rows if not _personal(scope)}
    out += [(name, value) for _scope, name, value, secret, *_ in _rows(None, None)
            if secret and isinstance(value, str) and name in referenced and name not in repos]
    return out


def mask(text: str, repo: str | None, known: dict | None = None) -> str:
    """text, as a sandbox's logs, with the values sbx knows (the platform's,
    the installation's, a trusted repo's, and known: the rest one sandbox
    gets, core.mask) and URL passwords hidden: for whoever may view the
    sandbox."""
    known = {**vault.platform_secrets(), **values(None),
             **(values(repo) if repo and settings.trust(repo) == "trusted" else {}), **(known or {})}
    for value in sorted({v for v in known.values() if isinstance(v, str) and len(v) >= 6}, key=len, reverse=True):
        text = text.replace(value, "••••")
    return URL_PASSWORD.sub(r"\1••••@", text)


def for_sandbox(repo: str, kind: str, owner: str = "") -> tuple[dict, set]:
    """(the values a sandbox of the repo gets, the names only its app gets):
    the repo's for every sandbox or for its kind (previews, sessions or
    other), then its owner's own over them."""
    found, hidden = {}, set()
    for scope, name, value, _secret, audience, agent, *_ in sorted(_rows(repo, owner or None),
                                                                    key=lambda r: _personal(r[0])):
        if audience != "all" and audience != kind:
            continue
        found[name] = value
        hidden.discard(name)
        if not agent:
            hidden.add(name)
    return found, hidden


def random_value(sandbox: str, name: str, hex_: bool, length: int, base64_: bool = False) -> str:
    """${random:N} / ${random:hexN} in a repo's sbx.yaml env: a value made
    once per sandbox and variable, the same across resumes and env
    refreshes (sealed), gone with the sandbox."""
    with state.db() as conn:
        row = conn.execute("select value from sandbox_random where sandbox = ? and name = ?", (sandbox, name)).fetchone()
    if row:
        return secret_store.unseal(row[0])
    value = (base64.b64encode(secrets.token_bytes(max(1, length))).decode() if base64_ else
             secrets.token_hex(max(1, length // 2)) if hex_ else secrets.token_urlsafe(max(1, length))[:length])
    reference = secret_store.seal(value, key=f"random/{sandbox}/{name}")
    with state.db() as conn:
        conn.execute("insert into sandbox_random (sandbox, name, value) values (?, ?, ?) "
                     "on conflict (sandbox, name) do nothing", (sandbox, name, reference))
        row = conn.execute("select value from sandbox_random where sandbox = ? and name = ?", (sandbox, name)).fetchone()
    if row[0] != reference:  # another writer won: its value stands
        secret_store.discard(reference)
    return secret_store.unseal(row[0])


def generated(sandbox: str) -> dict:
    """The values the sandbox generated (random_value), by name."""
    with state.db() as conn:
        rows = conn.execute("select name, value from sandbox_random where sandbox = ?", (sandbox,)).fetchall()
    return {name: secret_store.unseal(reference) for name, reference in rows}


def forget(sandbox: str):
    """What a gone sandbox kept: its generated values and pending deliveries."""
    with state.db() as conn:
        secret_store.delete_rows(conn, "sandbox_random", "sandbox = ?", (sandbox,))
    delivery.forget(sandbox)


def kept() -> set:
    with state.db() as conn:
        return {r[0] for r in conn.execute("select sandbox from sandbox_random")} | delivery.waiting()


SCHEMA = state.Schema(tables="""
create table if not exists secrets (
    scope text not null, name text not null, updated_by text not null,
    updated_at text not null default current_timestamp, primary key (scope, name));
create table if not exists sandbox_random (
    sandbox text not null, name text not null, value text not null, primary key (sandbox, name));
""", columns=(("secrets", "secret", "integer not null default 1"),
              ("secrets", "audience", "text not null default 'all'"),
              ("secrets", "agent", "integer not null default 1")),
    sealed=(("sandbox_random", "value"),))
