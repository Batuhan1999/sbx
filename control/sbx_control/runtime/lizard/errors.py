"""Lizard's HTTP API and its answers as sbx's errors. Every request goes
through Api.call: plain HTTPS with the key in X-API-Key (lizard-sdk
config.py), no SDK. A refusal is raised as the error sbx acts on: NotRunning
(the sandbox is gone), LimitRefused or NoRoom (a limit of the account's, by
Lizard's code), else LizardError with the code and HTTP status, so a caller
tests the code, never the message."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from ..base import Failed, LimitRefused, NoRoom, NotRunning, Timeout
from .plans import API_KEY, KIND, TRIAL_NOTE

POLL_SECONDS = 2
TRANSIENT_TRIES = 3   # how often a read is sent when the answer was a transient failure
MESSAGE_CHARS = 300
HTTP_SECONDS = 60     # lizard-sdk config.py HTTP_TIMEOUT_S: above every server-side wait
_CODE = re.compile(r"[A-Z][A-Z0-9_]*")


class LizardError(Failed):
    """Lizard answered with an error: code is Lizard's ("" when it named
    none), status the HTTP status (None when it did not answer)."""

    def __init__(self, message: str, *, code: str = "", status: int | None = None, body: dict | None = None):
        super().__init__(message)
        self.code, self.status, self.body = code, status, body or {}

    def gone(self) -> bool:
        return self.status == 404


def read_error(raw) -> tuple[str, str, dict]:
    """An error answer's (message, code, body). Lizard has two shapes
    (lizard-sdk errors.py handle_api_error): {error: "text", code?} and
    {error: "SCREAMING_CODE", message: "text"}; anything else (a gateway's
    page) is its first characters and no code."""
    text = raw.decode(errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    try:
        body = json.loads(text)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return text[:MESSAGE_CHARS], "", {}

    def said(key):
        value = body.get(key)
        return value if isinstance(value, str) and value else ""

    error = said("error")
    is_code = bool(error and _CODE.fullmatch(error))
    message = (said("message") or error) if is_code else (error or said("message"))
    return (message or text[:MESSAGE_CHARS]), said("code") or (error if is_code else ""), body


def refused(status: int, code: str, message: str, body: dict, asked: dict | None = None) -> Failed | None:
    """Lizard's refusal of a limit of the account's as sbx's error; None for
    any other answer. The codes are lizard-sdk's (docs/sandboxes.md, Errors)."""
    asked = asked or {}
    if code == "region_unavailable":
        return NoRoom(f"Lizard takes no new sandboxes in this region now ({message}); try again in a few minutes")
    if status == 402:
        url = next((body[k] for k in ("invoiceUrl", "subscribeUrl", "billingUrl", "topupUrl")
                    if isinstance(body.get(k), str) and body[k]), "https://lizard.build")
        return LimitRefused(f"Lizard needs payment first ({message}): {url}", field="plan")
    if code in ("SANDBOX_LIMIT_REACHED", "WORKSPACE_SANDBOX_LIMIT", "WORKSPACE_USAGE_LIMIT", "TRIAL_SIZE_RUNNING"):
        unit = "vCPUs" if body.get("unit") == "vcpu" else "sandboxes"
        counted = (f": {body.get('running')} of {body.get('limit')} {unit} are running"
                   if isinstance(body.get("limit"), (int, float)) else "")
        return LimitRefused(f"Lizard refused another sandbox{counted} ({message})", field="sandboxes")
    if code == "SANDBOX_RATE_LIMITED":
        wait = body.get("retryAfterS")
        return LimitRefused(f"Lizard refused the start: your plan's starts are used ({message})"
                            + (f"; try again in {int(wait)} s" if isinstance(wait, (int, float)) else ""),
                            field="starts")
    if code in ("TRIAL_SANDBOX_SIZE", "SANDBOX_SIZE_NOT_ALLOWED"):
        return LimitRefused(f"Lizard refused that sandbox size: your plan doesn't include it ({message})",
                            field="cpu", asked=asked.get("cpu"))
    if code == "TRIAL_SANDBOX_LIFETIME":
        return LimitRefused(f"Lizard refused the lifetime ({message}): the trial allows 1 hour",
                            field="timeout_seconds", asked=asked.get("timeout_seconds"), allowed=3600)
    if code.startswith("TRIAL_"):
        return LimitRefused(f"Lizard's trial refused it ({message}). {TRIAL_NOTE}", field="plan")
    return None


class Api:
    """The HTTP part of the Lizard backend (mixed into Lizard)."""

    def _send(self, method: str, url: str, headers: dict, data: bytes | None, timeout: float):
        """(status, body) of one request; the tests' transport stands in here."""
        if self.transport is not None:
            return self.transport(method, url, headers, data, timeout)
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def call(self, method: str, path: str, body=None, *, query: dict | None = None, sandbox_id: str | None = None,
             asked: dict | None = None, read: bool | None = None, timeout: float = HTTP_SECONDS, raw: bool = False):
        """The answer's JSON (its bytes with raw); Lizard's errors as sbx's.
        read (default: a GET): the call only reads, so a transient failure
        (no answer, a 5xx that names no code) is sent again, TRANSIENT_TRIES
        times at most. sandbox_id: a 404 means that sandbox is gone."""
        if not self._key:
            raise Failed(f"Lizard is not configured: no {API_KEY} (sbx-admin runtime add {KIND})")
        read = method == "GET" if read is None else read
        url = self._url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"X-API-Key": self._key, "Accept": "application/json"}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        tries = 0
        while True:
            tries += 1
            try:
                status, answer = self._send(method, url, headers, data, timeout)
            except (TimeoutError, OSError) as error:   # urllib's URLError is an OSError
                if read and tries < TRANSIENT_TRIES:
                    time.sleep(POLL_SECONDS)
                    continue
                if isinstance(error, TimeoutError) or "timed out" in str(error).lower():
                    raise Timeout(f"Lizard did not answer {method} {path} within {int(timeout)} s") from error
                raise LizardError(f"Lizard did not answer: {type(error).__name__}: {str(error)[:MESSAGE_CHARS]}") \
                    from error
            if status < 400:
                if raw:
                    return answer
                try:
                    return json.loads(answer) if answer else None
                except ValueError as error:
                    raise LizardError(f"Lizard's answer to {method} {path} was not JSON", status=status) from error
            message, code, found = read_error(answer)
            if read and status >= 500 and not code and tries < TRANSIENT_TRIES:
                time.sleep(POLL_SECONDS)
                continue
            limit = refused(status, code, message, found, asked)
            if limit is not None:
                raise limit
            if sandbox_id and status == 404:
                raise NotRunning(f"Lizard has no sandbox {sandbox_id}: {message}")
            if status in (408, 504):
                raise Timeout(f"Lizard answered {status} to {method} {path}: {message}")
            if status in (401, 403) and not code:
                message += f" (check {API_KEY} and the project it may use)"
            raise LizardError(f"Lizard answered {status} {code}: {message}".replace("  ", " "), code=code,
                              status=status, body=found)
