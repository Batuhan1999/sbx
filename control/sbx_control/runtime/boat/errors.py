"""Boat's answers as sbx's errors. Every call to Boat's API goes through
call(), which retries what Boat says to retry and raises what it refused as
the error sbx acts on: NotRunning, LimitRefused or NoRoom, else BoatError
with Boat's code and HTTP status, so a caller tests the code, never the
message."""
from __future__ import annotations

import json
import time

from ..base import Failed, LimitRefused, NoRoom, NotRunning
from .plans import TRIAL_NOTE

POLL_SECONDS = 2
TRANSIENT_STATUSES = (500, 502, 503, 504)
TRANSIENT_TRIES = 3   # how often a read is sent when Boat's answer was a transient failure


class BoatError(Failed):
    """Boat answered with an error: code is Boat's ("" when it named none),
    status the HTTP status."""

    def __init__(self, message: str, *, code: str = "", status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status

    def gone(self) -> bool:
        """Whether Boat answered that what was asked for is not there."""
        return self.status == 404

    def gone_snapshot(self) -> bool:
        """Whether Boat answered that a saved snapshot is not there (deleted,
        or its data expired): the template is gone."""
        return self.code in ("snapshot_not_found", "snapshot_data_expired") or self.gone()


def _body(error) -> dict:
    try:
        found = json.loads(getattr(error, "body", None) or "{}")
        return found if isinstance(found, dict) else {}
    except (TypeError, ValueError):
        return {}


def _part(found, name: str) -> dict:
    """found[name] when it is an object, else {}: an answer that is not
    Boat's envelope (a proxy's {"error": "Bad Gateway"}, a string where an
    object goes) is read as no code, never as an AttributeError."""
    value = found.get(name) if isinstance(found, dict) else None
    return value if isinstance(value, dict) else {}


def code(error) -> str:
    """Boat's error code ("" for anything that is not an answer of Boat's)."""
    body = _body(error)
    return str(body.get("code") or _part(body, "error").get("code") or "")


def status(error) -> int | None:
    found = getattr(error, "status", None)
    return found if isinstance(found, int) else None


def _message(error) -> str:
    body = _body(error)
    said = body.get("error")
    return str(body.get("message") or _part(body, "error").get("message") or (said if isinstance(said, str) else "")
               or getattr(error, "reason", "") or error)


def _retryable(error, replays: bool = True) -> bool:
    """Whether Boat says to send the call again. A machine's failed call
    (sandbox_direct_failed) is sent again when Boat says so, but a command
    (replays False) only while the sandbox was restoring: else it may
    have run."""
    details = _part(_part(_body(error), "error"), "details")
    if code(error) == "sandbox_direct_failed":
        restoring = "sandbox_restoring" in (_message(error), details.get("message"))
        return restoring or (replays and bool(details.get("retryable")))
    return code(error) in ("sandbox_starting", "sandbox_resuming", "idempotency_in_progress")


def refused(error, asked: dict | None = None) -> Failed | None:
    """Boat's refusal of a limit of the account's as sbx's error: LimitRefused
    naming what to change, NoRoom while Boat has no machine; None for any
    other error. asked: what sbx asked for, by Limits field."""
    asked = asked or {}
    found, said = code(error), _message(error)
    if found in ("out_of_capacity", "no_ready_machine"):
        return NoRoom(f"Boat has no machine free now ({said}); try again in a few minutes")
    if found in ("limit_reached", "member_limit_reached"):
        return LimitRefused(f"Boat refused another sandbox: your plan's sandboxes at once are running ({said})",
                            field="sandboxes")
    if found == "rate_limited":
        return LimitRefused(f"Boat refused the start: your plan's starts per {_body(error).get('window') or 'minute'} "
                            f"are used ({said}); try again later or move to a bigger plan", field="starts")
    if found in ("machine_class_plan_required", "trial_machine_class_not_allowed"):
        return LimitRefused(f"Boat refused that sandbox size: your plan doesn't include it ({said})", field="cpu",
                            asked=asked.get("cpu"))
    if found in ("trial_auto_stop_required", "invalid_timeout"):
        return LimitRefused(f"Boat refused the timeout ({said}): the trial allows 2 hours, paid plans 30 days",
                            field="timeout_seconds", asked=asked.get("timeout_seconds"))
    if found.startswith("trial_"):
        return LimitRefused(f"Boat's free trial refused it ({said}). {TRIAL_NOTE}", field="plan")
    if found in ("billing_required", "team_member_cap_reached"):
        url = _body(error).get("billingUrl") or "https://boat.dev/dashboard?tab=billing"
        return LimitRefused(f"Boat needs billing first ({said}): {url}", field="plan")
    return None


def is_failure(error) -> bool:
    """Whether the error is the SDK's or its HTTP client's, or its models'
    on an answer they could not read: Boat did not answer as asked."""
    return type(error).__module__.split(".")[0] in ("boat_sdk", "urllib3", "pydantic", "pydantic_core")


def _transient(error) -> bool:
    """A gateway's or an overloaded server's answer that names no code of
    Boat's: the same call may well work a moment later."""
    return status(error) in TRANSIENT_STATUSES and not code(error)


def call(function, *, sandbox_id: str | None = None, asked: dict | None = None, retry_seconds: float = 180,
         replays: bool = True, read: bool = False):
    """function's result; Boat's errors as sbx's: a sandbox that has no
    machine, or that Boat no longer has, as NotRunning, a limit as
    LimitRefused or NoRoom, anything else as BoatError. Answers Boat says to
    retry (a sandbox still starting or restoring its disk) are retried;
    replays False for a command, which is not sent again once it may have run.
    read: the call only reads, so a transient failure (a gateway's 5xx, an
    answer the SDK could not read) is sent again, TRANSIENT_TRIES times at most."""
    deadline = time.monotonic() + retry_seconds
    tries = 0
    while True:
        tries += 1
        try:
            return function()
        except Failed:
            raise
        except Exception as error:
            if status(error) is None:
                if not is_failure(error):
                    raise
                if read and tries < TRANSIENT_TRIES:
                    time.sleep(POLL_SECONDS)
                    continue
                raise BoatError(f"Boat did not answer as asked: {type(error).__name__}: {str(error)[:300]}") \
                    from error
            if read and _transient(error) and tries < TRANSIENT_TRIES:
                time.sleep(POLL_SECONDS)
                continue
            if _retryable(error, replays) and time.monotonic() < deadline:
                time.sleep(POLL_SECONDS)
                continue
            found = refused(error, asked)
            if found is not None:
                raise found from error
            if sandbox_id and (status(error) == 404 or code(error) in (
                    "machine_not_running", "sandbox_not_ready", "snapshot_data_expired")):
                raise NotRunning(f"Boat sandbox {sandbox_id} is not running: {_message(error)}") from error
            raise BoatError(f"Boat answered {status(error)} {code(error) or ''}: {_message(error)}".replace("  ", " "),
                            code=code(error), status=status(error)) from error
