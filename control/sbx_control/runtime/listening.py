"""How a port listens in a sandbox, through any runtime's handle: what the
gate waits on before it opens a preview and what a runtime that relays
loopback-only ports (Boat) exposes it by."""
from __future__ import annotations

LOOPBACK, REACHABLE = "loopback", "reachable"


def is_loopback(address: str) -> bool:
    """Whether an `ss` local address (without its port) is on loopback only:
    127.0.0.0/8, ::1, also bracketed, IPv4-mapped or with a %interface."""
    address = address.strip("[]").partition("%")[0].removeprefix("::ffff:")
    return address.startswith("127.") or address in ("::1", "localhost")


def listening(handle, port: int, *, timeout: int = 15) -> str | None:
    """LOOPBACK when only loopback addresses listen on the port, REACHABLE
    when one the runtime reaches does, None when nothing listens. The
    command's errors (the sandbox not answering) are the caller's."""
    out = handle.commands.run(f"ss -Hltn 'sport = :{int(port)}'", timeout=timeout).stdout
    addresses = [fields[3].rpartition(":")[0] for fields in map(str.split, out.splitlines()) if len(fields) > 3]
    if not addresses:
        return None
    return LOOPBACK if all(is_loopback(a) for a in addresses) else REACHABLE
