"""What the API, the CLI and doctor say about the endpoints: each one's
repos, users and state (on, applying, failed), and the managed servers that
could have one."""
from __future__ import annotations

from .. import host_requests, settings
from . import certs
from .model import REVISION, Endpoint, address, domain, find, label, repos_of, rows
from .nginx import applied


def _not_picked_up() -> str:
    """Why the request failed when the root side never took it (host_requests.stuck); "" otherwise."""
    return ("the server did not pick up the change: check sbx-apply-endpoints.path "
            "(systemctl status sbx-apply-endpoints.path)") if host_requests.stuck(settings.HostUnit.ENDPOINTS) else ""


def _state(revision: int, record: dict) -> tuple[str, str]:
    if record.get("ok_revision", 0) >= revision:
        return "on", ""
    if record.get("revision", 0) >= revision and not record.get("ok"):
        return "failed", record.get("message") or "the server could not apply it"
    stuck = _not_picked_up()
    return ("failed", stuck) if stuck else ("applying", "")


def _payload(row: Endpoint, record: dict, using: dict, kinds: dict) -> dict:
    state_now, message = ("failed", row.unroutable) if row.unroutable else _state(row.revision, record)
    return {"name": row.name, "address": address(row.name), "kind": row.kind, "server": row.server,
            "server_kind": kinds.get(row.server, ""), "target": row.target, "repos": repos_of(row),
            "state": state_now, "message": message, "used_by": using.get(row.name, [])}


def _servers() -> list[dict]:
    from .. import hosted_services
    return hosted_services.records()


def one(name: str) -> dict:
    return _payload(find(name), applied(), certs.used_by(), {s["name"]: s["kind"] for s in _servers()})


def describe() -> dict:
    """GET /api/endpoints: {endpoints, candidates, domain, needed, applying, failure}. Each endpoint's
    repos: who may use it (an internal host's grants; the repos attached to a managed server); used_by:
    the repos whose sandboxes DELETE is refused for. candidates: the managed servers without an endpoint,
    with the address one would get; needed: an enabled runtime's sandboxes don't reach this host's
    private network; applying: a change (an added or removed endpoint) is not applied yet; failure: why
    the last change failed ("" when it did not)."""
    from .. import hosted_services
    found, record, using, servers = rows(), applied(), certs.used_by(), _servers()
    kinds, exposed, name = {s["name"]: s["kind"] for s in servers}, {r.server for r in found if r.server}, domain()
    candidates = [{"server": s["name"], "kind": s["kind"], "address": address(label(s["name"])) if name else "",
                   "target": hosted_services.target(s["name"]) or ""} for s in servers if s["name"] not in exposed]
    overall, message = _state(int(REVISION.get("0")), record)
    return {"endpoints": [_payload(r, record, using, kinds) for r in found], "candidates": candidates, "domain": name,
            "needed": needed(), "applying": overall == "applying", "failure": message if overall == "failed" else ""}


def needed() -> bool:
    """An enabled runtime's sandboxes don't reach this host's private network."""
    from .. import runtime
    return any(not runtime.capabilities(kind).private_networks for kind in runtime.enabled())


def doctor_lines(found: dict) -> list[str]:
    """sbx-admin doctor: describe()'s endpoints, one line each."""
    return [f"  {e['state']:<9} {e['address']} -> {e['target']} ({e['server'] or 'internal'}; "
            f"for {', '.join(e['repos']) or 'no repo yet'})" + (f": {e['message']}" if e["message"] else "")
            for e in found["endpoints"]]
