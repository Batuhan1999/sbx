"""The `attachments` table: the installation's connections (scope
'installation': a connection package's values, saved once under a key) and
each repo's attachments (scope 'repo': a package attached here, or values
for a key the repo's sbx.yaml declares under `services:`).

A repo's effective attachments are its sbx.yaml declarations merged with
these records, the records' values over the declaration's `with:`. Inputs
are sealed (secret_store); none comes back through effective()."""
from __future__ import annotations

import contextlib
import fcntl
import json

from .. import paths, recipes, secret_store, state
from . import NAME_RE, MissingConnection, Package, PackageError, check, get
from . import resolve as resolve_package


@contextlib.contextmanager
def lock(shared=False):
    """Held exclusively while a connection changes, shared while a resource
    is made or deleted on one."""
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    with (paths.state_dir() / "service-connections.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield


def _unseal(reference) -> dict:
    return secret_store.unseal(reference) if reference else {}


def _rows(scope: str, repo: str = "", *, values: bool = True) -> list:
    """values: with the inputs unsealed (each a request to the secret store)."""
    with state.db() as conn:
        rows = conn.execute("select id, key, package, inputs, connections, disabled, check_status, check_detail, "
                            "added_by, added_at from attachments where scope = ? and repo = ? order by id",
                            (scope, repo)).fetchall()
    return [{"id": r[0], "key": r[1], "package": r[2], "inputs": _unseal(r[3]) if values else {},
             "connections": json.loads(r[4] or "{}"),
             "disabled": bool(r[5]), "check_status": r[6], "check_detail": r[7], "added_by": r[8], "added_at": r[9]}
            for r in rows]


def connections() -> list:
    """Every connection with all its inputs, secret ones included: server-side only."""
    return _rows("installation")


def connection(key: str) -> dict | None:
    return next((r for r in connections() if r["key"] == key), None)


def save_connection(key: str, package: str, values: dict, by: str):
    """The row of a checked connection (connections.save is the writer)."""
    reference = secret_store.seal(values, key=f"connection/installation//{key}")
    with state.db() as conn:
        old = conn.execute("select inputs from attachments where scope = 'installation' and key = ?", (key,)).fetchone()
        conn.execute("insert into attachments (scope, repo, key, package, inputs, check_status, added_by) "
                     "values ('installation', '', ?, ?, ?, 'ok', ?) on conflict (scope, repo, key) do update set "
                     "package = excluded.package, inputs = excluded.inputs, check_status = 'ok', check_detail = '', "
                     "updated_at = current_timestamp", (key, package, reference, by))
    if old and old[0] != reference:
        secret_store.discard(old[0])


def delete_connection(key: str) -> bool:
    with state.db() as conn:
        return secret_store.delete_rows(conn, "attachments", "scope = 'installation' and key = ?", (key,)) > 0


def resolve(package: Package, bound: dict, label: str = "") -> tuple[dict, dict]:
    """(slot -> the bound connection's inputs, secret ones included; the env
    its scripts get from them) for connections already chosen."""
    inputs, env = {}, {}
    for slot, key in bound.items():
        found = connection(key)
        if not found or found["disabled"]:
            raise MissingConnection(f"{label + ': ' if label else ''}the connection {key} it uses is missing or "
                                    "disabled; reconnect it")
        inputs[slot] = found["inputs"]
        expose = (package.requires.get(slot) or {"expose": {}})["expose"]
        env.update({name: found["inputs"][field] for field, name in expose.items() if field in found["inputs"]})
    return inputs, env


def bind(package: Package, chosen: dict, label: str) -> tuple[dict, dict]:
    """(slot -> connection key, the env its scripts get from them). One
    connection of the kind is bound by itself; of several, the attachment
    names one."""
    if not package.requires:
        return {}, {}
    found = connections()
    bound = {}
    for slot, req in package.requires.items():
        of_kind = [c for c in found if c["package"] == req["kind"]]
        pick = chosen.get(slot)
        picked = next((c for c in of_kind if c["key"] == pick), None) if pick else (of_kind[0] if len(of_kind) == 1 else None)
        if picked is None:
            if pick:
                have = ", ".join(c["key"] for c in of_kind) or "none"
                raise MissingConnection(f"{label}: names the {req['kind']} connection {pick}, which this server "
                                        f"doesn't have (it has: {have}); name one with connections: {{{slot}: <key>}}")
            if not of_kind:
                raise MissingConnection(f"{label}: needs a {req['kind']} connection; add one in Extensions")
            raise MissingConnection(f"{label}: several {req['kind']} connections ({', '.join(c['key'] for c in of_kind)}); "
                                    f"name one with connections: {{{slot}: <key>}}")
        bound[slot] = picked["key"]
    return bound, resolve(package, bound, label)[1]


def repos() -> list:
    """The repos that may have attachments: a recipe, or a record here."""
    with state.db() as conn:
        recorded = {r[0] for r in conn.execute("select distinct repo from attachments where scope = 'repo'")}
    return sorted(set(recipes.with_recipe()) | recorded)


def used_by() -> dict:
    """package name -> the repos attaching it."""
    out: dict = {}
    for repo in repos():
        for att in effective(repo):
            out.setdefault(att["package"], set()).add(repo)
    return out


def merged(repo: str, *, values: bool = True) -> list:
    """(key, uses, with, chosen connections, disabled, origin, record) for
    each attachment: sbx.yaml's declarations in order, then the records'
    own; a record's values over the declaration's (without values: the
    declaration's only, and the store is not asked)."""
    declared = {key: use for key, use in (recipes.load(repo).spec.get("services") or {}).items()
                if isinstance(use, dict) and isinstance(use.get("uses"), str)}
    records = {r["key"]: r for r in _rows("repo", repo, values=values)}
    out = []
    for key in [*declared, *(k for k in records if k not in declared)]:
        decl, rec = declared.get(key) or {}, records.get(key) or {}
        uses = rec.get("package") or decl.get("uses")
        if not uses:
            continue
        with_ = {**{k: str(v) for k, v in (decl.get("with") or {}).items()}, **(rec.get("inputs") or {})}
        chosen = {**dict(decl.get("connections") or {}), **(rec.get("connections") or {})}
        origin = "both" if decl and rec else "sbx.yaml" if decl else "here"
        out.append((key, uses, with_, chosen, bool(rec.get("disabled")), origin, rec))
    return out


def effective(repo: str) -> list:
    """The repo's attachments for people: key, package, kind, title, logo, origin
    (sbx.yaml | here | both), inputs (non-secret), secret inputs by name
    (set?), connections, exports (the env names it gives), state and a
    problem when it has one."""
    out = []
    for key, uses, with_, chosen, disabled, origin, rec in merged(repo):
        project = uses.startswith("./")
        item = {"key": key, "package": uses, "kind": "provision", "title": uses, "logo": "", "origin": origin, "inputs": {},
                "secret_inputs": {}, "connections": {}, "exports": [], "disabled": disabled, "state": "ready",
                "problem": None, "project": project}
        try:
            package = resolve_package(uses, repo)
        except PackageError as error:
            item.update(state="error", problem=str(error))
            out.append(item)
            continue
        item.update(kind=package.kind, title=package.title, logo=package.logo_url(), exports=list(package.env),
                    connections=chosen,
                    inputs={k: v for k, v in with_.items() if not package.secret(k)},
                    secret_inputs={k: k in with_ for k, v in package.inputs.items() if v["secret"]})
        try:
            package.with_defaults(with_, key)
            item["connections"] = {**chosen, **bind(package, chosen, key)[0]}
        except PackageError as error:
            item.update(state="needs_connection" if isinstance(error, MissingConnection) else "needs_values",
                        problem=str(error).split(": ", 1)[-1])
        if disabled:
            item["state"] = "disabled"
        if rec.get("check_status") == "failed" and item["state"] == "ready":
            item.update(state="check_failed", problem=rec.get("check_detail") or "its check failed")
        out.append(item)
    return out


def for_create(repo: str) -> list:
    """(key, uses, with, chosen connections) of the repo's enabled
    attachments, in order; secret values included (the scripts need them),
    never through the API."""
    return [(key, uses, with_, chosen) for key, uses, with_, chosen, disabled, *_ in merged(repo) if not disabled]


def enabled(repo: str) -> list:
    """(key, uses) of the repo's enabled attachments, in order, without
    their values: what template plans and the scheduler read, so the secret
    store is not asked."""
    return [(key, uses) for key, uses, _, _, disabled, *_ in merged(repo, values=False) if not disabled]


def bound_in_repos(key: str) -> list:
    """repo:attachment of what a repo binds to the connection, by choice or
    as the only one of its kind."""
    return [f"{repo}:{att['key']}" for repo in repos() for att in effective(repo)
            if att["kind"] == "provision" and key in att["connections"].values()]


def attach(repo: str, name: str, key: str | None, inputs: dict, chosen: dict, *, by: str, run_check: bool = True) -> dict:
    """Attach a catalog package to a repo, or give a declared key its values.
    The values are checked with its defaults and its connections bound first;
    nothing is saved when that, or its check, fails."""
    package = get(name)
    key = key or package.name
    if not NAME_RE.match(key):
        raise PackageError(f"{key!r} is not a key (a-z, 0-9, - and _)")
    declared = next((m for m in merged(repo) if m[0] == key), None)
    given = {k: v for k, v in inputs.items() if v != ""}
    # A blank secret keeps what is saved; any other input given blank drops its saved value, so it goes
    # back to what sbx.yaml gives it, else its default. The check sees what merged() will give the scripts.
    cleared = {k for k, v in inputs.items() if v == "" and not package.secret(k)}
    use = (recipes.load(repo).spec.get("services") or {}).get(key)
    recipe = {k: str(v) for k, v in ((use.get("with") if isinstance(use, dict) else None) or {}).items()}
    saved = (declared[6].get("inputs") or {}) if declared else {}
    values = package.with_defaults({**recipe, **{k: v for k, v in saved.items() if k not in cleared}, **given}, key)
    bind(package, {**(declared[3] if declared else {}), **chosen}, key)
    why = check(package, values) if run_check else ""
    if why:
        raise PackageError(f"{package.title}: {why}. Nothing was saved.")
    with state.db() as conn:
        row = conn.execute("select inputs, connections from attachments where scope = 'repo' and repo = ? and key = ?",
                           (repo, key)).fetchone()
        before_inputs, before_chosen = _unseal(row[0]) if row else {}, json.loads(row[1] or "{}") if row else {}
        kept = {k: v for k, v in before_inputs.items() if k not in cleared}
        reference = secret_store.seal({**kept, **given}, key=f"connection/repo/{repo}/{key}")
        conn.execute("insert into attachments (scope, repo, key, package, inputs, connections, added_by) "
                     "values ('repo', ?, ?, ?, ?, ?, ?) on conflict (scope, repo, key) do update set "
                     "package = excluded.package, inputs = excluded.inputs, connections = excluded.connections, disabled = 0, "
                     "check_status = null, check_detail = '', updated_at = current_timestamp",
                     (repo, key, name, reference, json.dumps({**before_chosen, **chosen}), by))
    if row and row[0] and row[0] != reference:
        secret_store.discard(row[0])
    return next(a for a in effective(repo) if a["key"] == key)


def detach(repo: str, key: str, *, by: str) -> str:
    """A key sbx.yaml declares is disabled (sbx.yaml is where it is
    removed); one attached here is removed. Returns which."""
    declared = (recipes.load(repo).spec.get("services") or {}).get(key)
    with state.db() as conn:
        row = conn.execute("select package from attachments where scope = 'repo' and repo = ? and key = ?",
                           (repo, key)).fetchone()
        if declared is not None:
            conn.execute("insert into attachments (scope, repo, key, package, disabled, added_by) values ('repo', ?, ?, ?, 1, ?) "
                         "on conflict (scope, repo, key) do update set disabled = 1, updated_at = current_timestamp",
                         (repo, key, row[0] if row else (declared or {}).get("uses", ""), by))
            return "disabled"
        if not row:
            raise PackageError(f"{repo} has no {key}")
        secret_store.delete_rows(conn, "attachments", "scope = 'repo' and repo = ? and key = ?", (repo, key))
    return "removed"


def owned_names(repo: str) -> dict:
    """Variable name -> the attachment (key, title) that gives it, so the
    repo's env list leaves it to the attachment's card."""
    out = {}
    for att in effective(repo):
        for name in att["exports"]:
            out.setdefault(name, {"key": att["key"], "title": att["title"], "kind": att["kind"]})
    return out


SCHEMA = state.Schema(tables="""
create table if not exists attachments (
    id integer primary key, scope text not null, repo text not null default '', key text not null,
    package text not null, inputs text not null default '', connections text not null default '{}',
    disabled integer not null default 0,
    check_status text, check_detail text not null default '',
    added_by text not null, added_at text not null default current_timestamp,
    updated_at text not null default current_timestamp,
    unique (scope, repo, key));
""", sealed=(("attachments", "inputs"),))
