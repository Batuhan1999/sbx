"""Root-side effects: what sbx-control asks the host to do as root, and how
it went. One mechanism for every effect (settings.HostUnit).

sbx-control asks with queue(): <run>/apply/<key>, holding the effect's
payload (an upgrade's tag, a runtime change's JSON) or a timestamp. Its path
unit (sbx-apply-<key>.path, host.py) runs `sbx-apply KEY`, which is run():
under the effect's lock (<state>/<key>.lock) the request is taken (read,
then removed) and handed to the effect's handler (host.EFFECTS). An effect
that reports back (an upgrade, a runtime change) writes its record
(<state>/<key>.json) with record().

What the files say now (status): requested (the request is there), applying
(the lock is held), stuck (asked PICKUP_SECONDS ago and never taken: its path
unit is not running), and the last record."""
from __future__ import annotations

import contextlib
import fcntl
import json
import pathlib
import time

from . import paths

PICKUP_SECONDS = 120


def request_file(key: str) -> pathlib.Path:
    return paths.run() / "apply" / str(key)


def _lock_file(key: str) -> pathlib.Path:
    return paths.state_dir() / f"{key}.lock"


def _record_file(key: str) -> pathlib.Path:
    return paths.state_dir() / f"{key}.json"


def write_atomic(path: pathlib.Path, text: str, *, private: bool = False) -> None:
    """Whole, or not at all (a path unit starts as soon as the name exists);
    private: readable by its owner alone, from the start."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    if private:
        temporary.touch(mode=0o600)
        temporary.chmod(0o600)
    temporary.write_text(text)
    temporary.replace(path)


def queue(key: str, payload: str | None = None) -> None:
    """Ask the host for the effect; OSError where there is no /run/sbx."""
    write_atomic(request_file(key), f"{time.time():.0f}\n" if payload is None else payload, private=True)


def requested(key: str) -> bool:
    return request_file(key).exists()


def pending() -> list[str]:
    """The effects asked for and not taken yet."""
    folder = paths.run() / "apply"
    return sorted(p.name for p in folder.glob("*") if not p.name.startswith(".")) if folder.exists() else []


@contextlib.contextmanager
def lock(key: str, *, blocking: bool = True):
    """The effect's lock: held by run() while the effect applies. Not
    blocking: BlockingIOError while another holds it."""
    path = _lock_file(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def applying(key: str) -> bool:
    """Whether run() holds the effect's lock now."""
    try:
        with open(_lock_file(key), "a") as held:
            fcntl.flock(held, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    return False


def overdue(key: str) -> bool:
    """The request there now was asked PICKUP_SECONDS ago (held lock(key): nothing is taking it)."""
    try:
        age = time.time() - request_file(key).stat().st_mtime
    except OSError:
        return False
    return age > PICKUP_SECONDS


def stuck(key: str) -> bool:
    """The single pick-up rule: asked PICKUP_SECONDS ago, and nothing took it."""
    return overdue(key) and not applying(key)


def take(key: str) -> str | None:
    """The request's payload, and the request gone (None: there was none). Call it holding lock(key)."""
    try:
        payload = request_file(key).read_text()
    except FileNotFoundError:
        return None
    request_file(key).unlink(missing_ok=True)
    return payload


def run(key: str, handler) -> int:
    """sbx-apply KEY, as root: under the effect's lock, the request taken
    before the handler runs (so one made meanwhile triggers it again), then
    handler(payload); payload is None when nothing was asked."""
    with lock(key):
        return handler(take(key))


def record(key: str, found: dict) -> None:
    write_atomic(_record_file(key), json.dumps(found, indent=2) + "\n", private=True)


def last(key: str) -> dict:
    """The effect's last record ({} when none)."""
    try:
        return json.loads(_record_file(key).read_text())
    except (OSError, ValueError):
        return {}
