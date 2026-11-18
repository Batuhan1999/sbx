"""This server's self-signed certificate's pin in every sandbox: what calls
sbx from inside them (the git credential helper) pins the key of now, so a
new key means their runtime layers again."""
from __future__ import annotations

from . import delivery, state

# The pin delivered last: {"pin", "queued": [names], "complete": bool}.
# complete: every sandbox has it (a listing of every runtime queued them);
# else queued names those a listing that missed a runtime's queued it for.
DELIVERED = state.Setting("tls_pin", encoded=True)
UNREAD = "(unread)"   # observed's answer when the pin can't be read: never a pin, so its round stays incomplete


def observed() -> str:
    """The pin now, read before the sandboxes are listed (redeliver's seen); UNREAD when it can't be read."""
    from . import templates
    try:
        return templates.pinned_key()
    except Exception:   # redeliver says why
        return UNREAD


def redeliver(names, complete: bool = True, seen: str | None = None) -> bool:
    """When the key changed (or the sandboxes do not have it yet): queue
    their runtime layers again, without an app restart. names: the
    sandboxes listed; running ones get it at this round's deliveries, paused
    ones when they resume. A listing that missed a runtime (complete False)
    queues those it has, once each, and the pin counts as delivered only
    after a complete one; so does one that began under another pin (seen),
    as a sandbox made while it listed holds that one. Whether it queued any."""
    from . import templates
    pin, record = templates.pinned_key(), DELIVERED.get()
    complete = complete and seen in (None, pin)
    if record is None and not pin:
        DELIVERED.set({"pin": pin, "queued": [], "complete": True})
        return False
    if record and record["pin"] == pin and record["complete"]:
        return False
    done = set(record["queued"]) if record and record["pin"] == pin else set()
    fresh = sorted({name for name in names if name} - done)
    for name in fresh:
        delivery.queue(name, [], restart=False)
    DELIVERED.set({"pin": pin, "queued": [] if complete else sorted(done | set(fresh)), "complete": complete})
    return bool(fresh)
