"""The installation's connections: a connection package's values saved once
under a key (a Postgres server, a Neon project, an AWS role), which the
repos' attachments bind their resources to. A connection is saved only
after its package's check passes; while anything uses it, its endpoint is
fixed and it cannot be removed. A managed server (hosted_services.py) is
one way to make one."""
from __future__ import annotations

from urllib.parse import urlsplit

from . import PackageError, attachments, check as run_check, code, get, listing as packages, resources

SECRET_FIELDS = ("password", "api_key", "token", "secret", "client_secret")


def users(key: str) -> list:
    return [*attachments.bound_in_repos(key), *resources.users(key)]


def listing() -> list:
    """Every connection with its package and users; an input's value only
    when its package declares it and not as secret."""
    by_name = {p.name: p for p in packages("connection")}
    out = []
    for row in attachments.connections():
        package = by_name.get(row["package"])
        inputs = {k: (v if package and k in package.inputs and not package.secret(k) else "(set)")
                  for k, v in row["inputs"].items()}
        out.append({**{k: v for k, v in row.items() if k != "connections"}, "inputs": inputs,
                    "title": package.title if package else row["package"], "logo": package.logo_url() if package else "",
                    "known": package is not None,
                    "used_by": users(row["key"])})
    return out


def _endpoint(package, values: dict) -> dict:
    """What the connection reaches: its package's identity inputs, else every
    URL's host and every input that is not a credential."""
    if package.identity:
        return {field: values.get(field) for field in package.identity}
    found = {}
    for field, value in values.items():
        parsed = urlsplit(value) if isinstance(value, str) and "://" in value else None
        if parsed and parsed.hostname:
            found[field] = (parsed.scheme, parsed.hostname, parsed.port, parsed.path, parsed.query)
        elif not package.secret(field) and field not in SECRET_FIELDS:
            found[field] = value
    return found


def save(key: str, name: str, inputs: dict, *, by: str) -> dict:
    """Connect: the package's check runs with the values first, and nothing
    is stored when it fails. Saving again updates its values, not its
    endpoint while something uses it."""
    from . import NAME_RE
    if not NAME_RE.match(key or ""):
        raise PackageError(f"{key!r} is not a connection key (a-z, 0-9, - and _)")
    package = get(name)
    if package.kind != "connection":
        raise PackageError(f"{package.title} is a {package.kind} package, not a connection")
    with attachments.lock():
        existing = attachments.connection(key)
        before = existing["inputs"] if existing else {}
        given = {**before, **inputs}
        if package.connect:
            try:
                given = code(package, package.connect).connect(dict(before), dict(inputs))
            except ValueError as error:
                raise PackageError(f"{package.title}: {error}") from error
        values = package.with_defaults(given, f"connection {key}")
        if existing and users(key) and (existing["package"] != name or _endpoint(package, before) != _endpoint(package, values)):
            raise PackageError("this connection owns resources; add a new connection before changing its endpoint")
        why = run_check(package, values)
        if why:
            raise PackageError(f"{package.title}: {why}. Nothing was saved.")
        attachments.save_connection(key, name, values, by)
    return next(c for c in listing() if c["key"] == key)


def check(key: str) -> str:
    """Its package's check with its values now; "" when it passes."""
    found = attachments.connection(key)
    if not found:
        raise PackageError(f"no connection {key}")
    return run_check(get(found["package"]), found["inputs"])


def remove(key: str) -> bool:
    with attachments.lock():
        using = users(key)
        if using:
            raise PackageError(f"{key} is used by {', '.join(using)}")
        return attachments.delete_connection(key)
