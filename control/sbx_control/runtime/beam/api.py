"""Beam's Gateway HTTP API (https://docs.beam.cloud/v2/reference/api.md): the
JSON form of the gRPC services Beam's own SDK speaks, at
https://app.beam.cloud/api/v1/gateway, with `Authorization: Bearer <token>`.
Plain HTTPS from the standard library: Beam's Python SDK (beam-client, over
beta9) would bring gRPC, FastAPI and some twenty more packages, reads
BEAM_TOKEN from the process environment and keeps one process-global
channel, which a backend may not (runtime/base.py).

The routes are the google.api.http annotations of Beam's protos
(github.com/beam-cloud/beta9: pkg/abstractions/pod/pod.proto,
pkg/gateway/gateway.proto, pkg/abstractions/image/image.proto, read
2026-11-19), which the published OpenAPI files repeat in part. Fields go out
and come back in lowerCamelCase (protobuf's JSON mapping), bytes as base64.

Seen from the live service without an account (2026-11-19): a route asked
with a bad token answers 401 {"code": 16, "message": "invalid or missing
token"}, and POST /auth/authorize answers 200 {"ok": false, "errorMsg":
"Invalid token"}. Everything past the token check is from Beam's
documentation and SDK source, not from a live account (docs/howto/beam.md).

Beam reports a failure at two layers: an HTTP status with {"code",
"message"} (gRPC's status), or 200 with "ok": false and "errorMsg" ("errMsg"
on stubs). Api.call raises both as BeamError."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..base import Failed

DEFAULT_API_URL = "https://app.beam.cloud/api/v1/gateway"
POLL_SECONDS = 2
TRANSIENT_TRIES = 3     # how often a read is sent when the answer was a transient failure
MESSAGE_CHARS = 300
# What Beam answers while a new sandbox's process manager is not up yet
# (beta9's SANDBOX_EXEC_READINESS_ERRORS, which its SDK retries for 90 s).
NOT_READY = ("failed to connect to sandbox", "process manager not ready", "sandbox process manager is not ready",
             "process manager failed to become ready", "sandbox process manager client not ready",
             "deadline exceeded while waiting for connections to become ready")
READY_SECONDS = 90


class BeamError(Failed):
    """Beam answered with an error: status is the HTTP status (200 for an
    "ok": false answer, None when it did not answer), code gRPC's status
    code when it gave one."""

    def __init__(self, message: str, *, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status, self.code = status, code

    def unauthorized(self) -> bool:
        return self.status in (401, 403) or self.code in (7, 16)

    def missing(self) -> bool:
        return self.status == 404 or self.code == 5

    def transient(self) -> bool:
        """A gateway's or an overloaded server's answer, or none: the same call may work a moment later."""
        return self.status is None or self.status == 429 or 500 <= self.status < 600

    def not_ready(self) -> bool:
        return self.status == 200 and any(marker in str(self).lower() for marker in NOT_READY)


def _open(method: str, url: str, headers: dict, data: bytes | None, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class Api:
    """One backend's connection: its URL and token, passed on every request
    (nothing in the environment or the module). transport stands in for the
    HTTP client in tests: (method, url, headers, data, timeout) -> (status, body)."""

    def __init__(self, url: str, token: str, transport=None):
        self.url, self._token, self.transport = url.rstrip("/"), token, transport

    def send(self, method: str, url: str, *, headers: dict | None = None, data: bytes | None = None,
             timeout: float = 60) -> tuple[int, bytes]:
        try:
            return (self.transport or _open)(method, url, dict(headers or {}), data, timeout)
        except Failed:
            raise
        except (OSError, ValueError) as error:   # no connection, a timeout, TLS: Beam did not answer
            raise BeamError(f"Beam did not answer: {type(error).__name__}: {str(error)[:MESSAGE_CHARS]}") from error

    def _once(self, method: str, path: str, body, query, timeout: float) -> dict:
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        status, raw = self.send(method, url, headers=headers, data=data, timeout=timeout)
        try:
            found = json.loads(raw or b"{}")
        except (ValueError, RecursionError):
            found = None
        if status >= 400 or not isinstance(found, dict):
            said = found.get("message") if isinstance(found, dict) else None
            code = found.get("code") if isinstance(found, dict) and isinstance(found.get("code"), int) else None
            text = said if isinstance(said, str) and said else raw.decode(errors="replace")[:MESSAGE_CHARS]
            raise BeamError(f"Beam answered {status}: {text}".strip(), status=status, code=code)
        if found.get("ok") is False:
            said = found.get("errorMsg") or found.get("errMsg") or "it gave no reason"
            raise BeamError(str(said)[:MESSAGE_CHARS], status=200)
        return found

    def call(self, method: str, path: str, body=None, *, query: dict | None = None, timeout: float = 60,
             read: bool = False, ready_seconds: float = 0) -> dict:
        """The answer's JSON object; BeamError for either layer's failure.
        read: the call only reads, so a transient failure is sent again
        (TRANSIENT_TRIES at most). ready_seconds: how long an answer that
        says the sandbox is still starting (NOT_READY) is asked again; such
        an answer means the call did not run, so it is safe for any call."""
        tries, deadline = 0, time.monotonic() + ready_seconds
        while True:
            try:
                return self._once(method, path, body, query, timeout)
            except BeamError as error:
                if error.not_ready() and time.monotonic() < deadline:
                    time.sleep(0.5)
                    continue
                tries += 1
                if read and error.transient() and tries < TRANSIENT_TRIES:
                    time.sleep(POLL_SECONDS)
                    continue
                raise

    def lines(self, path: str, body: dict, *, timeout: float = 3600):
        """A server-streaming route's messages, in order. grpc-gateway sends
        one JSON object a line, {"result": message} or {"error": status};
        read whole once the stream ends (builds say little)."""
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                   "Content-Type": "application/json"}
        status, raw = self.send("POST", self.url + path, headers=headers, data=json.dumps(body).encode(),
                                timeout=timeout)
        if status >= 400:
            raise BeamError(f"Beam answered {status}: {raw.decode(errors='replace')[:MESSAGE_CHARS]}", status=status)
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                found = json.loads(line)
            except ValueError:
                raise BeamError(f"Beam's stream was not JSON: {line[:80]!r}", status=status) from None
            if isinstance(found, dict) and isinstance(found.get("error"), dict):
                raise BeamError(str(found["error"].get("message") or "the stream failed")[:MESSAGE_CHARS],
                                status=status, code=found["error"].get("code"))
            yield found.get("result", found) if isinstance(found, dict) else {}
