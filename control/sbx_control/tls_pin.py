"""This server's self-signed certificate's pin in every sandbox: what calls
sbx from inside them (the git credential helper) pins the key of now, so a
new key means their runtime layers again."""
from __future__ import annotations

from . import delivery, state

# The pin delivered last: {"pin", "queued": [names], "complete": bool}.
# complete: every sandbox has it (a listing of every runtime queued them);
# else queued names those a listing that missed a runtime's queued it for.
DELIVERED = state.Setting("tls_pin", encoded=True)


def redeliver(names, complete: bool = True) -> bool:
    """When the key changed (or the sandboxes do not have it yet): queue
    their runtime layers again, without an app restart. names: the
    sandboxes listed; running ones get it at this round's deliveries, paused
    ones when they resume. A listing that missed a runtime (complete False)
    queues those it has, once each, and the pin counts as delivered only
    after a complete one. Whether it queued any."""
    from . import templates
    pin, record = templates.pinned_key(), DELIVERED.get()
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
