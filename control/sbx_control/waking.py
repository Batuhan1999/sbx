"""Waking a paused sandbox for a preview request: one resume at a time per
sandbox, in the background, so nginx never waits on it (the gate answers
with the starting page meanwhile). Embed's client-proxy does not resume on
traffic; sbx does, here.

A caller hands over look(fresh) -> the sandbox as the gate's listing has it
(gate.Listed, None when not listed), fresh asking the runtimes again."""
import threading
import time
from typing import Callable

from . import core, runtime

WAKE_WAIT_SECONDS = 2.5      # how long a request waits for a resume before the starting page answers

_last_access: dict = {}  # sandbox id -> time the gate last let a request through
_resume_locks: dict = {}
_waking: dict = {}       # sandbox id -> the thread resuming it for a preview
_wake_errors: dict = {}  # sandbox id -> why the last resume for a preview failed
# Sandboxes a preview request resumed, until they pause or are used
# otherwise (keeper): an extension that pauses its sandboxes when idle lets
# these run out their timeout (extensions.Use). An open dev-server socket is
# one request, so the sandbox looked unused after a few minutes, paused, the
# browser reconnected and resumed it: a 6 GB snapshot every 5 minutes.
_resumed_for_preview: set = set()

Look = Callable[[bool], object]


def last_access(sandbox_id: str) -> float:
    return _last_access.get(sandbox_id, 0)


def resumed_for_preview(sandbox_id: str) -> bool:
    return sandbox_id in _resumed_for_preview


def forget_preview_resume(sandbox_id: str):
    _resumed_for_preview.discard(sandbox_id)


def failed(sandbox_id: str) -> str | None:
    """Why the last resume for a preview failed, if it did."""
    return _wake_errors.get(sandbox_id)


def busy(sandbox_id: str) -> bool:
    """Whether a resume for a preview runs now."""
    thread = _waking.get(sandbox_id)
    return bool(thread and thread.is_alive())


def _state(listed) -> str | None:
    return listed.state if listed is not None else None


def _resume(sandbox_id: str, look: Look):
    """Resume a paused sandbox for a preview: one at a time per sandbox. The
    listing is asked again afterwards, so the gate sees it running."""
    lock = _resume_locks.setdefault(sandbox_id, threading.Lock())
    with lock:
        listed = look(True)
        if _state(listed) == "paused":
            core.attach((listed.info.metadata or {})["sbx"], "for a preview request", info=listed.info)
            _resumed_for_preview.add(sandbox_id)
            look(True)


def _resume_in_background(sandbox_id: str, look: Look):
    try:
        _resume(sandbox_id, look)
        _wake_errors.pop(sandbox_id, None)
    except Exception as error:
        _wake_errors[sandbox_id] = f"{type(error).__name__}: {error}"
        print(f"gate: resuming {sandbox_id} for a preview failed: {error!r}", flush=True)


def wake(sandbox_id: str, look: Look, wait: float | None = None) -> bool:
    """Start resuming a paused sandbox (once) and wait up to wait seconds:
    True when it runs now, False while it is still waking (the starting
    page answers meanwhile)."""
    idle_for = time.time() - _last_access.get(sandbox_id, 0)
    _last_access[sandbox_id] = time.time()
    listed = look(idle_for > 60)  # it may have paused since the last look
    thread = _waking.get(sandbox_id)
    if _state(listed) != "paused" and not (thread and thread.is_alive()):
        if _state(listed) == "running":
            _wake_errors.pop(sandbox_id, None)   # one the last resume left (its last look failed) is over
        return True
    if thread is None or not thread.is_alive():
        _wake_errors.pop(sandbox_id, None)
        thread = _waking[sandbox_id] = threading.Thread(target=_resume_in_background, args=(sandbox_id, look),
                                                        daemon=True, name=f"wake-{sandbox_id}")
        thread.start()
    thread.join(WAKE_WAIT_SECONDS if wait is None else wait)
    return not thread.is_alive() and sandbox_id not in _wake_errors


def ensure_running(sandbox_id: str, look: Look):
    """Resume a paused sandbox and wait for it (for callers that must have it running)."""
    while not wake(sandbox_id, look, wait=60):
        if sandbox_id in _wake_errors:
            raise runtime.Failed(_wake_errors[sandbox_id])
